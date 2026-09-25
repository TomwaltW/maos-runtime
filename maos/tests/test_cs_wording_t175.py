"""T175 · 措辞校验（p13 契约 §1.4 T175）：check_observation_wording 与 check_reply 的 p13 补扫。

* ``check_observation_wording`` —— obs: claim 的 literal 必须逐字是措辞表里「该观察的状态」那一句；
  观察不在本轮 → dangling_basis，措辞对不上 / 状态没有对外说法 → foreign_literal；kb: claim 与
  没有 claim 的正文不归它管。
* ``check_reply`` 补扫「已付款 / 已支付」与英文状态说法（大小写、词形、零宽、全角都认；否定不豁免），
  措辞表的中英各句在**有**有效观察撑时放行、没有时拦下。
* p12 语义不变：没有英文状态说法、也不说「已付款 / 已支付」的正文，结果与关掉 p13 补扫时逐字节相同
  （p12 的测试文件一个期望都没改，这里再按「关掉补扫」对照一遍）。
"""

from __future__ import annotations

import pytest

from maos.domain.cs import claims as C
from maos.domain.cs import ports as P
from maos.domain.cs import types as T
from maos.domain.cs.claims import check_observation_wording, check_reply, en_status_spans
from maos.domain.refund import projection

#: 措辞表逐句：(语种, 状态, 那一句)。
WORDING_ROWS_T175 = tuple((lang, status, sentence)
                          for lang, table in P.ORDER_STATUS_WORDING.items()
                          for status, sentence in table.items())


def _kinds_t175(result: T.CheckResult) -> list[str]:
    return [v.kind for v in result.violations]


def _obs_row_t175(status: str, **extra) -> dict:
    """T171 ``observations_for_turn`` 读回的一行的形状（契约 §1.3 的列）。"""
    return {"tenant_id": "tnt-demo", "observation_id": "o1", "conversation_id": "csc-x",
            "turn_id": "csc-x-t0001", "kind": P.OBS_ORDER_LOOKUP, "system_name": "demo-orders",
            "query_key": "A1001", "status": status, "version": 1, "updated_at": "",
            "observed_at": "2026-09-24T00:00:00+00:00", **extra}


def _claimed_t175(text: str, literal: str, basis: str = "obs:o1") -> T.ReplyDraft:
    return T.ReplyDraft(text=text, claims=(T.Claim(literal, basis),))


# ---------------------------------------------------------------- 措辞表：有观察放行、没观察拦下
def test_wording_table_shape_is_what_the_rows_cover_t175():
    assert len(WORDING_ROWS_T175) == 6
    assert {(lang, status) for lang, status, _ in WORDING_ROWS_T175} == {
        (lang, status) for lang in P.LANGS for status in ("paid", "shipped", "cancelled")}


@pytest.mark.parametrize("lang,status,sentence", WORDING_ROWS_T175)
def test_wording_sentence_with_valid_obs_passes_both_checks_t175(lang, status, sentence):
    for text in (sentence, f"您好，{sentence}。" if lang == P.LANG_ZH else f"Hello. {sentence} Thanks."):
        draft = _claimed_t175(text, sentence)
        assert check_reply(draft, observations=frozenset({"o1"})).ok, (text, check_reply(
            draft, observations=frozenset({"o1"})))
        assert check_observation_wording(draft, {"o1": _obs_row_t175(status)}, lang=lang).ok


@pytest.mark.parametrize("lang,status,sentence", WORDING_ROWS_T175)
def test_wording_sentence_without_obs_is_blocked_t175(lang, status, sentence):
    """空观察下措辞表每一句都说不出口（中文「已付款」那句靠 p13 补扫才拦得住）。"""
    bare = check_reply(T.ReplyDraft(text=sentence))
    assert bare.ok is False and set(_kinds_t175(bare)) == {T.VIOLATION_UNBACKED_STATUS}, bare
    # kb: 撑不起状态
    by_kb = check_reply(_claimed_t175(sentence, sentence, "kb:kb-cs-tnt-demo-LOG-004"),
                        kb_doc_ids=frozenset({"kb-cs-tnt-demo-LOG-004"}))
    assert T.VIOLATION_UNBACKED_STATUS in _kinds_t175(by_kb)
    # 悬空的 obs: 也撑不起
    dangling = check_reply(_claimed_t175(sentence, sentence, "obs:o-missing"))
    assert {T.VIOLATION_DANGLING_BASIS, T.VIOLATION_UNBACKED_STATUS} <= set(_kinds_t175(dangling))


# ---------------------------------------------------------------- check_observation_wording
@pytest.mark.parametrize("lang,status,sentence", WORDING_ROWS_T175)
def test_wording_must_match_the_observed_status_t175(lang, status, sentence):
    """挂着观察还不够：说的必须是**那条观察**的状态那一句。"""
    for other in ("paid", "shipped", "cancelled"):
        if other == status:
            continue
        result = check_observation_wording(_claimed_t175(sentence, sentence),
                                           {"o1": _obs_row_t175(other)}, lang=lang)
        assert _kinds_t175(result) == [T.VIOLATION_FOREIGN_LITERAL], (status, other)
        assert other in result.violations[0].detail


@pytest.mark.parametrize("literal", [
    "您的订单已发货",                                 # 截短
    "您的订单已发货（如有多件，可能分批发出）。",       # 多一个字
    "您的订单已发货(如有多件,可能分批发出)",           # 半角标点
    "您的包裹已发货（如有多件，可能分批发出）",
    "Your order has shipped",
    "your order has shipped (multi-item orders may ship in parts).",
])
def test_wording_is_verbatim_not_similar_t175(literal):
    lang = P.LANG_EN if literal.lower().startswith("your") else P.LANG_ZH
    result = check_observation_wording(_claimed_t175(literal, literal),
                                       {"o1": _obs_row_t175("shipped")}, lang=lang)
    assert _kinds_t175(result) == [T.VIOLATION_FOREIGN_LITERAL], literal


def test_wording_in_the_other_language_is_foreign_t175():
    zh = P.ORDER_STATUS_WORDING[P.LANG_ZH]["shipped"]
    en = P.ORDER_STATUS_WORDING[P.LANG_EN]["shipped"]
    rows = {"o1": _obs_row_t175("shipped")}
    assert _kinds_t175(check_observation_wording(_claimed_t175(zh, zh), rows, lang=P.LANG_EN)) == [
        T.VIOLATION_FOREIGN_LITERAL]
    assert _kinds_t175(check_observation_wording(_claimed_t175(en, en), rows, lang=P.LANG_ZH)) == [
        T.VIOLATION_FOREIGN_LITERAL]
    # 措辞表里没有的语种：一句都说不了
    assert _kinds_t175(check_observation_wording(_claimed_t175(zh, zh), rows, lang="ja")) == [
        T.VIOLATION_FOREIGN_LITERAL]


@pytest.mark.parametrize("status", ["amended", "", "delivered", "signed"])
def test_status_without_public_wording_is_foreign_t175(status):
    """amended、平台不映射、签收：措辞表里没有，任何说法都不行（契约 R3 增量）。"""
    sentence = P.ORDER_STATUS_WORDING[P.LANG_ZH]["shipped"]
    result = check_observation_wording(_claimed_t175(sentence, sentence),
                                       {"o1": _obs_row_t175(status)}, lang=P.LANG_ZH)
    assert _kinds_t175(result) == [T.VIOLATION_FOREIGN_LITERAL]


def test_row_without_status_key_is_foreign_t175():
    sentence = P.ORDER_STATUS_WORDING[P.LANG_ZH]["paid"]
    result = check_observation_wording(_claimed_t175(sentence, sentence), {"o1": {}},
                                       lang=P.LANG_ZH)
    assert _kinds_t175(result) == [T.VIOLATION_FOREIGN_LITERAL]


@pytest.mark.parametrize("basis", ["obs:o-missing", "obs:", "obs:O1", "obs: o1"])
def test_observation_not_in_this_turn_is_dangling_t175(basis):
    sentence = P.ORDER_STATUS_WORDING[P.LANG_ZH]["shipped"]
    result = check_observation_wording(_claimed_t175(sentence, sentence, basis),
                                       {"o1": _obs_row_t175("shipped")}, lang=P.LANG_ZH)
    assert _kinds_t175(result) == [T.VIOLATION_DANGLING_BASIS]


def test_empty_or_non_mapping_observations_make_every_obs_claim_dangling_t175():
    sentence = P.ORDER_STATUS_WORDING[P.LANG_ZH]["shipped"]
    draft = _claimed_t175(sentence, sentence)
    for observations in ({}, None, {"o1": "shipped"}):
        result = check_observation_wording(draft, observations, lang=P.LANG_ZH)
        assert _kinds_t175(result) == [T.VIOLATION_DANGLING_BASIS], observations


def test_kb_claims_and_unclaimed_text_are_not_this_checks_business_t175():
    """kb: claim、没有 claim 的正文（哪怕满篇状态字眼）都不归这条管 —— 那是 check_reply 的事。"""
    draft = T.ReplyDraft(text="您的订单已发货，Your order has shipped. 退款已到账",
                         claims=(T.Claim("您的订单", "kb:d1"),))
    assert check_observation_wording(draft, {}, lang=P.LANG_ZH).ok
    assert check_observation_wording(T.ReplyDraft(text="已发货"), {}, lang=P.LANG_ZH).ok


def test_violations_follow_claim_order_and_result_is_deterministic_t175():
    zh = P.ORDER_STATUS_WORDING[P.LANG_ZH]
    text = zh["paid"] + "；" + zh["cancelled"] + "；" + zh["shipped"]
    draft = T.ReplyDraft(text=text, claims=(
        T.Claim(zh["paid"], "obs:o1"),              # 对
        T.Claim(zh["cancelled"], "obs:o9"),         # 悬空
        T.Claim(zh["shipped"], "obs:o2"),           # 观察说的是 paid
        T.Claim("您的订单", "kb:d1"),                # 不归这条管
    ))
    rows = {"o1": _obs_row_t175("paid"), "o2": _obs_row_t175("paid", observation_id="o2")}
    result = check_observation_wording(draft, rows, lang=P.LANG_ZH)
    assert _kinds_t175(result) == [T.VIOLATION_DANGLING_BASIS, T.VIOLATION_FOREIGN_LITERAL]
    assert result.violations[0].detail.startswith("claim[1]")
    assert result.violations[1].detail.startswith("claim[2]")
    assert result == check_observation_wording(draft, rows, lang=P.LANG_ZH)


def test_wording_comes_from_ports_not_a_copy_t175():
    """三句措辞只从 ports 取：claims.py 里没有第二份字面量。"""
    import ast
    import pathlib

    assert C.ORDER_STATUS_WORDING is P.ORDER_STATUS_WORDING
    tree = ast.parse(pathlib.Path(C.__file__).read_text(encoding="utf-8"))
    consts = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)
              and isinstance(n.value, str)}
    assert not consts & {s for _, _, s in WORDING_ROWS_T175}


# ---------------------------------------------------------------- 已付款 / 已支付
@pytest.mark.parametrize("text", [
    "您的订单已付款", "这笔已经付款了", "订单已支付成功", "已为您支付", "您的订单已​付款",
    "已 支 付",
    # 复核 L3-5：付款族的其余完成态
    "您的订单已完成支付", "您的订单已成功付款", "您的订单付款成功", "支付成功", "支付成功！",
    "您的订单已经付了", "您的订单已付", "尾款已付清",
])
def test_paid_wording_blocked_without_obs_t175(text):
    result = check_reply(T.ReplyDraft(text=text))
    assert _kinds_t175(result) == [T.VIOLATION_UNBACKED_STATUS], (text, result)


@pytest.mark.parametrize("text", [
    "请在订单页完成付款", "付款成功后可以在订单页查看进度", "未付款的订单可以直接取消",
    "支持微信、支付宝和银行卡支付", "如果付款失败，请换一张卡再试",
    "支付成功以后就能在订单页看到", "完成支付即可", "请确认支付成功再联系我们",
])
def test_payment_policy_text_passes_t175(text):
    assert check_reply(T.ReplyDraft(text=text)).ok, text


# ---------------------------------------------------------------- 英文状态说法
EN_FABRICATIONS_T175 = (
    "Your order has shipped.",
    "Good news: it SHIPPED this morning.",
    "It was Delivered yesterday.",
    "Your refund was refunded to your card.",
    "Your order was cancelled.",
    "Your order was canceled.",
    "Your refund has been processed.",
    "Your items have been packed.",
    "It will arrive tomorrow.",
    "Your parcel arrives on Friday.",
    "The package arrived at the depot.",
    "Your parcel is arriving soon.",
    "It will ship tomorrow.",
    "It will be dispatched today.",
    "Your parcel is in transit.",
    "It is out for delivery.",
    "Your parcel is on its way.",
    "Your order is paid.",
    "It has not shipped yet.",                       # 否定不豁免：同样是在替外部世界说状态
    "ｓｈｉｐｐｅｄ",                                  # 全角
    "ship​ped",                                 # 零宽空格
    "has  \n been",                             # 各种空白
    "您的订单shipped了",                               # 中英混排
    "Your order’s been processed.",             # 弯撇号
    # 复核 L2-5 / L3-4：现在时、进行时、被动、结果承诺、时限
    "Your order ships today.",
    "Your order delivers tomorrow.",
    "It dispatches from our warehouse tonight.",
    "Your order cancels automatically.",
    "It refunds automatically.",
    "Your order is shipping now.",
    "We are delivering it today.",
    "We are refunding your payment now.",
    "We're cancelling your order.",
    "Your refund is processing.",
    "Your refund will be issued in 3-5 business days.",
    "Your order will be sent tomorrow.",
    "We will refund you.",
    "We'll cancel it for you.",
    "Your request is being processed.",
    "Your order was sent.",
    "Payment confirmed.",
    "Your payment was successful.",
    "It was processed this morning.",
    "Your payment went through.",
    "Refund within 24 hours.",
    "Please allow three to five business days.",
    "You will get it by Friday.",
    "Your parcel is en route.",                       # 复核 L3 探针 G1 / G3 的英文尾巴
    "It will reach you tomorrow.",
    "We sent it yesterday.",
    "We've sent your order.",
)


@pytest.mark.parametrize("text", EN_FABRICATIONS_T175)
def test_english_status_blocked_without_obs_t175(text):
    result = check_reply(T.ReplyDraft(text=text))
    assert result.ok is False, text
    assert set(_kinds_t175(result)) == {T.VIOLATION_UNBACKED_STATUS}, result


@pytest.mark.parametrize("text", [
    "Please share your order number so I can check it.",
    "Shipping fees depend on your region.",
    "Refunds go back to the original payment method.",
    "A colleague will contact you shortly.",
    "Sorry, I could not find an answer to that.",
    "We ship to most countries.",
    "Delivery times vary by carrier.",
    "Thanks for waiting.",
    "Free shipping on orders over $50",               # 复核 L2-5：名词搭配不是「正在发货」
    "What's the shipping cost to Singapore?",
    "Could you share your order number, please?",
    "Thanks, a colleague will follow up with you.",
    "I have sent your request to a colleague.",
    "A colleague will get back to you shortly.",
])
def test_english_policy_text_passes_t175(text):
    assert check_reply(T.ReplyDraft(text=text)).ok, text


def test_every_english_pattern_has_a_blocked_example_t175():
    """EN_STATUS_PATTERNS 每一条都有至少一句空观察判负的例句（新增的模式不许空转）。"""
    normalized = [C._normalize_words(t)[0] for t in EN_FABRICATIONS_T175]
    for idx, pattern in enumerate(C.EN_STATUS_PATTERNS):
        assert any(pattern.search(n) for n in normalized), (idx, pattern.pattern)


def test_english_spans_map_back_to_original_offsets_t175():
    text = "Hi!  Your ORDER has​  been shipped."
    spans = en_status_spans(text)
    assert [frag for _, _, frag in spans] == ["has​  been", "shipped"]
    for start, end, frag in spans:
        assert text[start:end] == frag
    # 一条 obs claim 盖住整句才放行
    literal = "has​  been shipped"
    ok = check_reply(_claimed_t175(text, literal), observations=frozenset({"o1"}))
    assert ok.ok, ok
    half = check_reply(_claimed_t175(text, "shipped"), observations=frozenset({"o1"}))
    assert _kinds_t175(half) == [T.VIOLATION_UNBACKED_STATUS]


def test_english_and_chinese_places_on_the_same_span_count_once_t175():
    """「已发货shipped」两套规范化各找到一处、互不重叠 —— 两处；重叠的才合一。"""
    two = check_reply(T.ReplyDraft(text="已发货shipped"))
    assert _kinds_t175(two) == [T.VIOLATION_UNBACKED_STATUS] * 2


# ---------------------------------------------------------------- p12 语义不变
P12_TEXTS_T175 = (
    "您的退款已到账，预计3天送达", "尚未发货，还没到账，审核通过后处理", "已发货已签收",
    "您的包裹尚未发货，我们会尽快安排", "退款会按原支付方式退回，具体时间以支付渠道为准",
    "预计 3 个工作日", "可以申请补偿", "款项已原路退回您的账户", "预计三到五个工作日到账",
    "下单后48小时内发货", "已为您转接人工客服，请稍候", "您的退款已​到账", "预计③天到账",
    "满99元包邮，偏远地区除外", "人工客服服务时间为每天9:00-21:00", "7×24小时在线",
    *projection.PUBLIC_STATUSES,
)


@pytest.mark.parametrize("text", P12_TEXTS_T175)
def test_p12_results_are_unchanged_by_p13_scans_t175(monkeypatch, text):
    """关掉 p13 的英文补扫后结果逐字节相同（这些正文不说「已付款 / 已支付」）。"""
    drafts = (T.ReplyDraft(text=text), _claimed_t175(text, text))
    now = [check_reply(d, observations=frozenset({"o1"})) for d in drafts]
    monkeypatch.setattr(C, "EN_STATUS_PATTERNS", ())
    before = [check_reply(d, observations=frozenset({"o1"})) for d in drafts]
    assert now == before
    assert C.en_status_spans(text) == []


def test_status_spans_still_follow_frozen_patterns_only_t175():
    """status_spans 的口径不变：只按冻结的 STATUS_PATTERNS，不含补扫与英文。"""
    assert C.status_spans("您的订单已付款，Your order has shipped.") == []
    assert C.status_spans("已发货") == [(0, 3, "已发货")]
