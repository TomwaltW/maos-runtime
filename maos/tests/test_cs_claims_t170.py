"""T170 · 回复后置校验（契约 §1.4 check_reply 的五条规则）与本轮命中读回。

每条规则都有放行与拦下两面。夹具里那条手写的编造回复是本文件的锚：
p12 没有任何观察来源，前台说出「已到账」「预计 3 天」就是在替外部世界宣布一件
没人观察到的事（铁律 8），必须拦下。
"""

from __future__ import annotations

import pytest

from maos.core.store import SqliteStore
from maos.domain.cs import types as T
from maos.domain.cs.claims import (
    check_reply,
    status_spans,
    turn_kb_doc_ids,
)
from maos.domain.refund import projection
from maos.kb.retriever import emit_kb_retrieved

#: 手写的编造回复：一句话两处状态断言，一条 claim 都没有。
FABRICATED_REPLY_T170 = "您的退款已到账，预计3天送达"


def _kinds_t170(result: T.CheckResult) -> list[str]:
    return [v.kind for v in result.violations]


def _draft_t170(text: str, *claims: tuple[str, str], citations: tuple[str, ...] = ()) -> T.ReplyDraft:
    return T.ReplyDraft(text=text, claims=tuple(T.Claim(lit, basis) for lit, basis in claims),
                        citations=citations)


# ---------------------------------------------------------------- status_spans
def test_status_spans_finds_words_and_eta_forms_t170():
    spans = status_spans(FABRICATED_REPLY_T170)
    assert [s[2] for s in spans] == ["已到账", "预计3天"]
    for start, end, frag in spans:
        assert FABRICATED_REPLY_T170[start:end] == frag
    assert status_spans("尚未发货，还没到账，审核通过后处理") == []
    assert status_spans("") == []
    # 同一个词出现两处 = 两处
    assert len(status_spans("赔偿……赔偿")) == 2


# ---------------------------------------------------------------- 编造回复（锚）
def test_fabricated_reply_without_claims_is_blocked_t170():
    result = check_reply(T.ReplyDraft(text=FABRICATED_REPLY_T170))
    assert result.ok is False
    # 两处状态字眼各报一次：「已到账」与「预计3天」
    assert _kinds_t170(result) == [T.VIOLATION_UNBACKED_STATUS, T.VIOLATION_UNBACKED_STATUS]
    assert "已到账" in result.violations[0].detail
    assert "预计3天" in result.violations[1].detail


def test_fabricated_reply_with_dangling_claims_is_blocked_t170():
    """只写了 claims、引用却悬空 —— 形式上「有依据」，实际本轮什么都没观察到。"""
    draft = _draft_t170(FABRICATED_REPLY_T170, ("退款已到账", "obs:pay-obs-1"),
                        ("预计3天", "obs:eta-1"))
    result = check_reply(draft)                     # observations 为空
    assert result.ok is False
    kinds = _kinds_t170(result)
    assert kinds.count(T.VIOLATION_DANGLING_BASIS) == 2
    assert kinds.count(T.VIOLATION_UNBACKED_STATUS) == 2


# ---------------------------------------------------------------- 规则 1
def test_rule1_literal_must_be_nonempty_substring_t170():
    ok = check_reply(_draft_t170("请按页面指引提交申请", ("按页面指引", "kb:d1")),
                     kb_doc_ids=frozenset({"d1"}))
    assert ok.ok, ok
    bad = check_reply(_draft_t170("请按页面指引提交申请", ("按客服指引", "kb:d1")),
                      kb_doc_ids=frozenset({"d1"}))
    assert _kinds_t170(bad) == [T.VIOLATION_LITERAL_NOT_IN_TEXT]
    empty = check_reply(_draft_t170("请按页面指引提交申请", ("", "kb:d1")),
                        kb_doc_ids=frozenset({"d1"}))
    assert _kinds_t170(empty) == [T.VIOLATION_LITERAL_NOT_IN_TEXT]


# ---------------------------------------------------------------- 规则 2
@pytest.mark.parametrize("basis", ["obs:o-missing", "kb:d-missing", "obs:", "kb:", "o1", "", "doc:d1",
                                   # 前缀与集合错配：obs: 只查观察、kb: 只查命中，不许并起来查
                                   "obs:d1", "kb:o1"])
def test_rule2_dangling_basis_blocked_t170(basis):
    result = check_reply(_draft_t170("请按页面指引提交申请", ("页面指引", basis)),
                         observations=frozenset({"o1"}), kb_doc_ids=frozenset({"d1"}))
    assert _kinds_t170(result) == [T.VIOLATION_DANGLING_BASIS]


def test_rule2_kb_doc_filed_under_obs_cannot_back_status_t170():
    """一篇话术的 doc_id 挂在 obs: 前缀下也撑不起状态（铁律 8：kb: 撑不起状态，换前缀也不行）。"""
    doc = "kb-cs-tnt-demo-PAY-001"
    result = check_reply(_draft_t170(projection.PUBLIC_SETTLED,
                                     (projection.PUBLIC_SETTLED, f"obs:{doc}")),
                         kb_doc_ids=frozenset({doc}))
    assert result.ok is False
    assert _kinds_t170(result) == [T.VIOLATION_DANGLING_BASIS, T.VIOLATION_UNBACKED_STATUS]


@pytest.mark.parametrize("basis", ["obs:o1", "kb:d1"])
def test_rule2_basis_in_this_turn_passes_t170(basis):
    result = check_reply(_draft_t170("请按页面指引提交申请", ("页面指引", basis)),
                         observations=frozenset({"o1"}), kb_doc_ids=frozenset({"d1"}))
    assert result.ok, result


# ---------------------------------------------------------------- 规则 3
def test_rule3_public_settled_with_valid_obs_passes_t170():
    text = f"您好，{projection.PUBLIC_SETTLED}。"
    result = check_reply(_draft_t170(text, (projection.PUBLIC_SETTLED, "obs:pay-obs-7")),
                         observations=frozenset({"pay-obs-7"}))
    assert result.ok, result


def test_rule3_same_sentence_backed_by_kb_is_blocked_t170():
    """kb: 撑不起状态：话术库说的是规则，不是这一单。"""
    text = f"您好，{projection.PUBLIC_SETTLED}。"
    result = check_reply(_draft_t170(text, (projection.PUBLIC_SETTLED, "kb:kb-cs-tnt-demo-PAY-001")),
                         kb_doc_ids=frozenset({"kb-cs-tnt-demo-PAY-001"}))
    assert result.ok is False
    # 同一处（「退款已到账」里含「已到账」）只报一次
    assert _kinds_t170(result) == [T.VIOLATION_UNBACKED_STATUS]


def test_rule3_claim_must_cover_the_whole_place_t170():
    """claim 只盖住后半截「已到账」，前半截「退款」的对外字面值那一处仍然没撑住。"""
    text = projection.PUBLIC_SETTLED
    result = check_reply(_draft_t170(text, ("已到账", "obs:o1")), observations=frozenset({"o1"}))
    assert T.VIOLATION_UNBACKED_STATUS in _kinds_t170(result)


def test_rule3_obs_claim_backs_only_its_own_occurrence_t170():
    text = "订单甲已发货；订单乙已发货"
    one = check_reply(_draft_t170(text, ("订单甲已发货", "obs:o1")), observations=frozenset({"o1"}))
    assert _kinds_t170(one) == [T.VIOLATION_UNBACKED_STATUS]
    assert "[10,13)" in one.violations[0].detail
    both = check_reply(_draft_t170(text, ("订单甲已发货", "obs:o1"), ("订单乙已发货", "obs:o2")),
                       observations=frozenset({"o1", "o2"}))
    assert both.ok, both


def test_rule3_one_obs_claim_backs_every_occurrence_of_its_literal_t170():
    """复核 L2-3：同一个 literal 在正文里出现多处时，一条有效 obs claim 撑住**每一处**。

    这是契约 §1.4 规则 3 原文的读法（「落在某条有效 obs: claim 的 literal 在 text 中的出现
    区间里」，Claim 没有偏移）。p12 观察恒为空，没有实际影响；p13 有了观察以后，一条短
    literal 能替同句里别的同名状态作保 —— 风险记在 BACKLOG task-t170，口径由主会话定，
    改口径时这条跟着改。上面那条「只撑自己那一处」靠的是两条 literal 不同。
    """
    text = "订单甲已发货；订单乙也已发货"
    one = check_reply(_draft_t170(text, ("已发货", "obs:o1")), observations=frozenset({"o1"}))
    assert one.ok, one
    bare = check_reply(_draft_t170(text))
    assert _kinds_t170(bare) == [T.VIOLATION_UNBACKED_STATUS] * 2


@pytest.mark.parametrize("literal", projection.PUBLIC_STATUSES)
def test_rule3_every_public_literal_without_basis_is_blocked_t170(literal):
    """五个对外字面值都是退款状态 —— 其中三句不含任何 STATUS_WORDS，照样要拦。"""
    result = check_reply(T.ReplyDraft(text=f"您的申请{literal}"))
    assert result.ok is False
    assert _kinds_t170(result) == [T.VIOLATION_UNBACKED_STATUS]


@pytest.mark.parametrize("text", [
    "您的包裹尚未发货，我们会尽快安排",
    "如果退款还没到账，请以支付渠道的处理为准",
    "退款会按原支付方式退回，具体时间以支付渠道为准",
    "七天无理由退货需要商品不影响二次销售",
    "预计",
])
def test_rule3_negations_and_policy_text_pass_t170(text):
    assert check_reply(T.ReplyDraft(text=text)).ok


@pytest.mark.parametrize("text", ["已签收", "订单已取消", "预计 3 个工作日", "预计两日内", "可以申请补偿"])
def test_rule3_each_status_form_blocked_without_obs_t170(text):
    assert T.VIOLATION_UNBACKED_STATUS in _kinds_t170(check_reply(T.ReplyDraft(text=text)))


def test_adjacent_status_words_are_two_places_t170():
    """「同一处只报一次」的边界：重叠才合并，紧挨着的两处各算一处、各自可被撑住。"""
    text = "已发货已签收"
    assert status_spans(text) == [(0, 3, "已发货"), (3, 6, "已签收")]
    both = check_reply(_draft_t170(text, ("已发货", "obs:a"), ("已签收", "obs:b")),
                       observations=frozenset({"a", "b"}))
    assert both.ok, both
    one = check_reply(_draft_t170(text, ("已发货", "obs:a")), observations=frozenset({"a"}))
    assert _kinds_t170(one) == [T.VIOLATION_UNBACKED_STATUS]
    assert "已签收" in one.violations[0].detail


#: 冻结词表之外的自然说法（DECISIONS task-t170：补充模式）。空观察、零 claim 一律要拦。
NATURAL_FABRICATIONS_T170 = (
    "您的退款已经到账了",
    "您的退款已到帐",
    "您的申请被驳回了",
    "款项已原路退回您的账户",
    "我们已打款给您",
    "您的包裹已发出",
    "您的包裹已寄出",
    "您的包裹已送达",
    "快递显示已经签收",
    "订单已经取消",
    "已为您取消订单",
    "预计3-5个工作日到账",
    "预计三到五个工作日到账",
    "预计1~3天到账",
    "退款一般1-3个工作日原路退回",
    "3天内到账",
    "下单后48小时内发货",
    "预计明天送达",
)


@pytest.mark.parametrize("text", NATURAL_FABRICATIONS_T170)
def test_rule3_natural_status_variants_blocked_t170(text):
    result = check_reply(T.ReplyDraft(text=text))
    assert result.ok is False, text
    assert _kinds_t170(result) == [T.VIOLATION_UNBACKED_STATUS], result


#: 看不见或不改变读法的字符拆不开状态字眼（零宽、软连字符、空白、间隔号、数字等价形）。
OBFUSCATED_FABRICATIONS_T170 = (
    "您的退款已​到账",          # 零宽空格
    "您的退款已‍到账",          # 零宽连接符
    "您的退款已­到账",          # 软连字符
    "您的退款支付​处理中",       # 对外字面值中间插零宽
    "您的申请已​驳回",
    "预计​3天到账",
    "您的退款已 到账",
    "您的退款已·到账",
    "预计③天到账",              # ③
    "预计\U0001d7d1天到账",          # 数学粗体 3
    "预计٣天到账",              # 阿拉伯-印度数字 3
    "预计３天",                  # 全角 3
)


@pytest.mark.parametrize("text", OBFUSCATED_FABRICATIONS_T170)
def test_rule3_obfuscated_status_words_blocked_t170(text):
    result = check_reply(T.ReplyDraft(text=text))
    assert result.ok is False, repr(text)
    assert _kinds_t170(result) == [T.VIOLATION_UNBACKED_STATUS], result


def test_obfuscated_span_maps_back_to_original_offsets_t170():
    text = "您的退款已​到账"
    assert status_spans(text) == [(4, 8, "已​到账")]
    # 原文里的整段（含零宽字符）被一条有效 obs claim 盖住时放行 —— 但措辞不是对外字面值
    backed = check_reply(_draft_t170(text, ("退款已​到账", "obs:o1")),
                         observations=frozenset({"o1"}))
    assert _kinds_t170(backed) == [T.VIOLATION_FOREIGN_LITERAL]


@pytest.mark.parametrize("text", [
    "退款会按原支付方式原路退回，到账时间以支付渠道为准",
    "签收后7天内，商品不影响二次销售可申请无理由退货",
    "签收后七天内可以申请退货",
    "15天内可换货",
    "人工客服服务时间为每天9:00-21:00",
    "周一至周五人工在线",
    "7×24小时在线",
    "满99元包邮，偏远地区除外",
    "退货寄回后请在订单页填写快递单号",
    "发货时效以商品页面标注为准",
    "已为您转接人工客服，请稍候",
    "您的问题已记录，人工客服会尽快联系您",
])
def test_supplementary_patterns_leave_policy_text_alone_t170(text):
    """补充模式只收完成态断言与时限承诺；政策说法、服务时间、转人工过渡语不误拦。"""
    assert check_reply(T.ReplyDraft(text=text)).ok, text


# ---------------------------------------------------------------- 规则 4
def test_rule4_citations_must_be_retrieved_this_turn_t170():
    hit = "kb-cs-tnt-demo-RET-001"
    ok = check_reply(T.ReplyDraft(text="七天无理由退货需要商品完好", citations=(hit,)),
                     kb_doc_ids=frozenset({hit}))
    assert ok.ok
    bad = check_reply(T.ReplyDraft(text="七天无理由退货需要商品完好",
                                   citations=(hit, "kb-cs-tnt-demo-RET-002")),
                      kb_doc_ids=frozenset({hit}))
    assert _kinds_t170(bad) == [T.VIOLATION_UNCITED_RULE]
    assert "RET-002" in bad.violations[0].detail


# ---------------------------------------------------------------- 规则 5
def test_rule5_non_public_refund_wording_with_obs_is_foreign_t170():
    text = "钱已退款给您"
    result = check_reply(_draft_t170(text, (text, "obs:o1")), observations=frozenset({"o1"}))
    assert _kinds_t170(result) == [T.VIOLATION_FOREIGN_LITERAL]


@pytest.mark.parametrize("literal", [
    "赔偿已到账", "已补偿", "补偿金", "款项已到账",
    # 「逐字等于」不是「包含」：对外字面值外面多一个字也不行
    "您的退款已到账啦", f"{projection.PUBLIC_SETTLED}，请查收", f"您的申请{projection.PUBLIC_REJECTED}了",
    # 契约四个触发词里「赔偿」单独出现（不夹带别的触发词）
    "我们将为您赔偿",
    # 退款到账类的自然变体（补充模式）：带 obs 也必须用对外字面值
    "退款已经到账", "款项已原路退回", "已打款给您", "退款已到帐",
    # 半角括号的「已补偿(未到账)」不是那一句
    "已补偿(未到账)",
])
def test_rule5_other_refund_literals_are_foreign_t170(literal):
    result = check_reply(_draft_t170(literal, (literal, "obs:o1")), observations=frozenset({"o1"}))
    assert T.VIOLATION_FOREIGN_LITERAL in _kinds_t170(result), (literal, result)


@pytest.mark.parametrize("literal", projection.PUBLIC_STATUSES)
def test_rule5_public_literals_with_obs_pass_t170(literal):
    result = check_reply(_draft_t170(literal, (literal, "obs:o1")), observations=frozenset({"o1"}))
    assert result.ok, result


def test_rule5_uses_projection_not_a_copy_t170():
    """五个字面值从 projection 取，claims.py 里没有第二份（不以字符串常量出现）。"""
    import ast
    import pathlib

    from maos.domain.cs import claims as C
    assert C.PUBLIC_STATUSES is projection.PUBLIC_STATUSES
    tree = ast.parse(pathlib.Path(C.__file__).read_text(encoding="utf-8"))
    consts = {n.value for n in ast.walk(tree)
              if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert not consts & set(projection.PUBLIC_STATUSES)


# ---------------------------------------------------------------- 综合
def test_empty_draft_passes_and_result_is_deterministic_t170():
    assert check_reply(T.ReplyDraft(text="")) == T.CheckResult(ok=True)
    a = check_reply(T.ReplyDraft(text=FABRICATED_REPLY_T170, citations=("x",)))
    b = check_reply(T.ReplyDraft(text=FABRICATED_REPLY_T170, citations=("x",)))
    assert a == b and a.to_json() == b.to_json()


# ---------------------------------------------------------------- turn_kb_doc_ids
def _emit_t170(store, conv: str, turn: str, doc_ids: list[str]) -> None:
    hits = [{"doc_id": d, "score": 0.9, "title": d, "kind": T.CS_KB_KIND, "channels": {}}
            for d in doc_ids]
    emit_kb_retrieved(store, hits, query={"tenant_id": "tnt-demo", "keyword": "sha256:x"},
                      plan_id=T.plan_id_for(conv), task_id=turn, trace_id="")


def test_turn_kb_doc_ids_reads_only_this_turn_t170():
    store = SqliteStore()
    store.init_schema()
    conv = T.conversation_id_for("tnt-demo", T.CHANNEL_WECHAT_KF, "wk_1", "wm_a")
    other = T.conversation_id_for("tnt-demo", T.CHANNEL_WECHAT_KF, "wk_1", "wm_b")
    t1, t2 = T.turn_id_for(conv, 1), T.turn_id_for(conv, 2)

    _emit_t170(store, conv, t1, ["kb-cs-tnt-demo-LOG-001"])
    _emit_t170(store, conv, t2, ["kb-cs-tnt-demo-RET-001", "kb-cs-tnt-demo-RET-002"])
    _emit_t170(store, conv, t2, ["kb-cs-tnt-demo-GEN-001"])            # 同一轮第二次检索
    _emit_t170(store, conv, t2, [])                                   # 命中为空也落
    # 别的会话、同一个 turn 序号
    _emit_t170(store, other, T.turn_id_for(other, 2), ["kb-cs-tnt-demo-PAY-005"])
    # 同 plan、同 task 但不是 KbRetrieved 的行
    store.append_event_log({"event_id": "", "trace_id": "", "plan_id": T.plan_id_for(conv),
                            "task_id": t2, "event_type": T.EVENT_TURN_RECORDED,
                            "detail": {"docs": [{"doc_id": "kb-cs-tnt-demo-LOG-006"}]}})

    assert turn_kb_doc_ids(store, conversation_id=conv, turn_id=t2) == frozenset(
        {"kb-cs-tnt-demo-RET-001", "kb-cs-tnt-demo-RET-002", "kb-cs-tnt-demo-GEN-001"})
    assert turn_kb_doc_ids(store, conversation_id=conv, turn_id=t1) == frozenset(
        {"kb-cs-tnt-demo-LOG-001"})
    assert turn_kb_doc_ids(store, conversation_id=conv, turn_id=T.turn_id_for(conv, 3)) == frozenset()
    assert turn_kb_doc_ids(store, conversation_id=other, turn_id=t2) == frozenset()
    assert turn_kb_doc_ids(store, conversation_id=other,
                           turn_id=T.turn_id_for(other, 2)) == frozenset({"kb-cs-tnt-demo-PAY-005"})


def test_turn_kb_doc_ids_accepts_hits_and_string_entries_t170():
    """口径同 scripts/verify.py 第 5 项：docs 缺席认 hits；条目可以是字符串。"""
    store = SqliteStore()
    store.init_schema()
    conv = T.conversation_id_for("tnt-demo", T.CHANNEL_WECHAT_KF, "wk_1", "wm_c")
    turn = T.turn_id_for(conv, 1)
    store.append_event_log({"event_id": "", "trace_id": "", "plan_id": T.plan_id_for(conv),
                            "task_id": turn, "event_type": "KbRetrieved",
                            "detail": {"hits": ["kb-cs-tnt-demo-LOG-002",
                                                {"doc_id": "kb-cs-tnt-demo-LOG-003"}]}})
    assert turn_kb_doc_ids(store, conversation_id=conv, turn_id=turn) == frozenset(
        {"kb-cs-tnt-demo-LOG-002", "kb-cs-tnt-demo-LOG-003"})


def test_turn_kb_doc_ids_feeds_check_reply_end_to_end_t170():
    """读回的命中就是 check_reply 的 kb_doc_ids：本轮检出的能引、上一轮的不能。"""
    store = SqliteStore()
    store.init_schema()
    conv = T.conversation_id_for("tnt-demo", T.CHANNEL_WECHAT_KF, "wk_1", "wm_d")
    t1, t2 = T.turn_id_for(conv, 1), T.turn_id_for(conv, 2)
    _emit_t170(store, conv, t1, ["kb-cs-tnt-demo-RET-001"])
    _emit_t170(store, conv, t2, ["kb-cs-tnt-demo-RET-002"])
    draft = T.ReplyDraft(text="退货请在订单页提交申请", citations=("kb-cs-tnt-demo-RET-001",))
    assert check_reply(draft, kb_doc_ids=turn_kb_doc_ids(store, conversation_id=conv,
                                                         turn_id=t1)).ok
    stale = check_reply(draft, kb_doc_ids=turn_kb_doc_ids(store, conversation_id=conv,
                                                          turn_id=t2))
    assert _kinds_t170(stale) == [T.VIOLATION_UNCITED_RULE]


def test_turn_kb_doc_ids_without_store_is_empty_t170():
    assert turn_kb_doc_ids(None, conversation_id="csc-x", turn_id="csc-x-t0001") == frozenset()
