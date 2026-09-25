"""T169 客服前台编排的机器验收 —— 判定六步、触发词、审计行、零授权、永不抛。

跨轨契约 review/p12-cs-contracts.md §1.4（T169 那一段与一轮判定顺序）、§1.2（轮次归属）、
§2 R1 / R3 / R5，以及 §6 的修订（silent / tenant_unmapped 的意图记 unknown；每篇话术的
script 在空观察下过**真的** check_reply）。

全部用真实现：真会话表（T167）、真话术库与检索（T168）、真后置校验（T170）。
"""

from __future__ import annotations

import json
import logging
import re

import pytest

from maos import kb
from maos.agents.base import PermissionDenied
from maos.core.store import SqliteStore
from maos.domain.cs import claims, conversation, corpus, desk, triggers
from maos.domain.cs import types as T
from maos.domain.cs.desk import (
    CS_FRONT_DESK_IDENTITY,
    CUSTOMER_REPLIES,
    REPLY_BY_REASON,
    REPLY_DESK_UNAVAILABLE,
    REPLY_FALLBACK,
    REPLY_INTERNAL_ERROR,
    CsConfig,
    FrontDesk,
    render_card_text,
)
from maos.ingress.contracts import CHANNEL_WECHAT_KF, InboundMessage
from maos.kb import retriever
from maos.skills import registry
from maos.skills.invoker import SkillInvoker

KFID_T169 = "wk_t169_demo"
TENANT_T169 = "tnt-demo"
USER_T169 = "wm_t169_customer_9f8e7d"
NOW_T169 = "2026-09-24T10:00:00+00:00"


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def _store_t169() -> SqliteStore:
    s = SqliteStore()
    s.init_schema()
    corpus.seed_cs_kb(s)
    return s


def _desk_t169(store=None, *, target=None, tenants=None) -> FrontDesk:
    return FrontDesk(store if store is not None else _store_t169(),
                     CsConfig(tenants={KFID_T169: TENANT_T169} if tenants is None else tenants,
                              handoff_target=target),
                     clock=lambda: NOW_T169)


class _Talk_t169:
    """同一个客户在同一个客服账号下连着说几句；msg_id 自增。"""

    def __init__(self, desk_: FrontDesk, *, user=USER_T169, kfid=KFID_T169):
        self.desk, self.user, self.kfid, self.n = desk_, user, kfid, 0

    def say(self, text: str) -> T.DeskResult:
        self.n += 1
        return self.desk.handle(_msg_t169(text, user=self.user, kfid=self.kfid,
                                          msg_id=f"m-{self.user}-{self.n}"))


def _msg_t169(text, *, user=USER_T169, kfid=KFID_T169, msg_id="m-1") -> InboundMessage:
    return InboundMessage(channel=CHANNEL_WECHAT_KF, chat_id=user, sender=user, text=text,
                          msg_id=msg_id, raw={"open_kfid": kfid})


def _events_t169(store, conversation_id, event_type=None, task_id=None) -> list[dict]:
    rows = store.list_event_log(T.plan_id_for(conversation_id))
    return [r for r in rows
            if (event_type is None or r["event_type"] == event_type)
            and (task_id is None or r["task_id"] == task_id)]


def _skill_rows_t169(store, conversation_id, skill, task_id=None) -> list[dict]:
    return [r for r in _events_t169(store, conversation_id, "SkillInvoked", task_id)
            if r["detail"]["skill"] == skill]


def _turn_row_t169(store, res) -> dict:
    (row,) = conversation.objects.query(
        store, "SELECT * FROM cs_turn WHERE tenant_id=? AND turn_id=?",
        (res.tenant_id, res.turn_id))
    return row


def _all_event_text_t169(store) -> str:
    rows = [dict(r) for r in store._conn.execute("SELECT * FROM event_log").fetchall()]
    return json.dumps(rows, ensure_ascii=False, default=str)


def _script_of_t169(scheme_no: str) -> str:
    for row in corpus.load_corpus():
        if row["rule_no"] == scheme_no:
            return json.loads(row["body"])["script"]
    raise KeyError(scheme_no)


def _doc_id_t169(scheme_no: str) -> str:
    return f"kb-cs-{TENANT_T169}-{scheme_no}"


# ---------------------------------------------------------------------------
# 触发词（纯函数）
# ---------------------------------------------------------------------------
#: 每类至少两种说法；契约表里「至少认」的词另有一条逐字钉。
TRIGGER_CASES_T169 = {
    T.HANDOFF_REQUESTED: ["帮我转人工", "我要人工客服", "找人工", "叫个真人来跟我说",
                          "客服小姐姐在不在", "人工", "我不想跟机器人聊了"],
    T.HANDOFF_COMPLAINT: ["我要投诉你们", "我这就打１２３１５", "我去消协反映", "我要曝光这家店",
                          "准备起诉了", "我已经找了律师", "我去消费者协会说理",
                          "12315见", "再不处理我打 12315 了"],
    T.HANDOFF_ANGER: ["什么垃圾东西", "你们是骗子", "气死我了", "滚", "怎么还不发货!!!",
                      "搞什么！！！", "搞什么！!！", "搞什么 ! ! !"],
    T.HANDOFF_COMPENSATION: ["你们得赔偿我", "给点补偿吧", "赔钱", "赔我", "多少赔点钱吧",
                             "这损失谁来赔付"],
    T.HANDOFF_PRIVACY: ["你们怎么有我手机号", "别存我身份证", "把我住址删了", "我的个人信息",
                        "这是我的隐私", "个人资料能改吗"],
}

NOT_TRIGGERS_T169 = ["你好", "退款多久能到账", "滚筒洗衣机能退吗", "我的收货地址填错了",
                     "好的！", "好!!", "包邮吗亲", "", "   ",
                     # 复核 L2-3：订单号 / 流水号里碰巧含 12315 的不是投诉（全角同样不算）。
                     "我的订单号是20261231500，什么时候发货", "订单 2026123150 到哪了",
                     "流水号９９１２３１５"]

#: 复核二轮 L2-3：空格是「12315」的数字边界 —— 后面空一格跟数字的不许漏判；拆开打的号照认。
#: 最后一条是这个口径的代价（用空格隔开的数字串里恰好有一段 12315，多转一次人工），钉住它
#: 免得哪天有人以为是 bug 改回去、把前两条一起漏掉。
HOTLINE_SPACED_T169 = ["12315 12345都打过了", "我已经打了12315 3次了", "１２３１５　１２３４５都打了",
                       "我打 1 2 3 1 5 了", "单号 2026 12315 0 查一下"]


@pytest.mark.parametrize("reason", list(TRIGGER_CASES_T169))
def test_each_trigger_class_is_recognised_in_several_phrasings_t169(reason):
    for text in TRIGGER_CASES_T169[reason]:
        assert triggers.detect(text) == (reason, triggers.INTENT_OF[reason]), text


def test_every_contract_minimum_word_is_recognised_t169():
    """契约 §1.4 表里「至少认」的词逐字钉：单说这个词就触发对应原因。"""
    for reason, words in triggers.CONTRACT_WORDS.items():
        for word in words:
            assert triggers.detect(word) == (reason, triggers.INTENT_OF[reason]), word
    assert set(triggers.CONTRACT_WORDS) == set(triggers.PRIORITY)


def test_intent_mapping_follows_the_contract_t169():
    assert triggers.INTENT_OF == {
        T.HANDOFF_PRIVACY: T.INTENT_PRIVACY,
        T.HANDOFF_COMPENSATION: T.INTENT_COMPENSATION,
        T.HANDOFF_ANGER: T.INTENT_COMPLAINT,
        T.HANDOFF_COMPLAINT: T.INTENT_COMPLAINT,
        T.HANDOFF_REQUESTED: T.INTENT_HANDOFF_REQUEST,
    }
    assert triggers.PRIORITY == (T.HANDOFF_PRIVACY, T.HANDOFF_COMPENSATION, T.HANDOFF_ANGER,
                                 T.HANDOFF_COMPLAINT, T.HANDOFF_REQUESTED)


@pytest.mark.parametrize("text,reason", [
    ("我要投诉你们，还要赔偿", T.HANDOFF_COMPENSATION),        # compensation > complaint
    ("骗子！把钱赔我", T.HANDOFF_COMPENSATION),                # compensation > anger
    ("你们这服务太垃圾了，转人工", T.HANDOFF_ANGER),           # anger > requested
    ("垃圾！！！我要投诉", T.HANDOFF_ANGER),                   # anger > complaint
    ("我要去消协投诉，转人工客服", T.HANDOFF_COMPLAINT),       # complaint > requested
    ("转人工，查一下你们存了我哪些个人信息", T.HANDOFF_PRIVACY),  # privacy > requested
    ("赔偿！还有你们泄露了我的隐私", T.HANDOFF_PRIVACY),       # privacy > compensation
    ("骗子，把我手机号删了，我要起诉，赔钱，转人工", T.HANDOFF_PRIVACY),  # 五类全中取最高
])
def test_priority_picks_the_highest_class_t169(text, reason):
    assert triggers.detect(text) == (reason, triggers.INTENT_OF[reason])


def test_combined_matches_are_reported_in_priority_order_t169():
    assert triggers.matched_reasons("骗子，把我手机号删了，我要起诉，赔钱，转人工") == \
        triggers.PRIORITY


@pytest.mark.parametrize("text", NOT_TRIGGERS_T169)
def test_ordinary_questions_do_not_trigger_t169(text):
    assert triggers.detect(text) is None


@pytest.mark.parametrize("text", HOTLINE_SPACED_T169)
def test_hotline_followed_by_a_space_and_digits_is_still_a_complaint_t169(text):
    assert triggers.detect(text) == (T.HANDOFF_COMPLAINT, T.INTENT_COMPLAINT)


# ---------------------------------------------------------------------------
# 判定六步
# ---------------------------------------------------------------------------
def test_step1_handed_off_conversation_is_silent_t169():
    store = _store_t169()
    talk = _Talk_t169(_desk_t169(store))
    first = talk.say("帮我转人工")
    assert (first.route, first.handoff_reason) == (T.ROUTE_HANDOFF, T.HANDOFF_REQUESTED)
    handoffs = len(_events_t169(store, first.conversation_id, T.EVENT_HANDOFF_RAISED))
    stages = len(_events_t169(store, first.conversation_id, T.EVENT_STAGE_CHANGED))

    second = talk.say("退款一般多久能到账啊")          # 本来答得上：已转人工就不答
    assert second.route == T.ROUTE_SILENT
    assert second.intent == T.INTENT_UNKNOWN
    assert second.reply_text == "" and second.handoff is None and second.handoff_reason == ""
    assert second.draft == T.ReplyDraft(text="")
    # 不出卡、不改阶段、不检索、不调 skill；只落一条 CsTurnRecorded。
    cid = second.conversation_id
    assert len(_events_t169(store, cid, T.EVENT_HANDOFF_RAISED)) == handoffs
    assert len(_events_t169(store, cid, T.EVENT_STAGE_CHANGED)) == stages
    assert _events_t169(store, cid, "KbRetrieved", second.turn_id) == []
    assert _events_t169(store, cid, "SkillInvoked", second.turn_id) == []
    assert len(_events_t169(store, cid, T.EVENT_TURN_RECORDED, second.turn_id)) == 1
    row = _turn_row_t169(store, second)
    assert (row["route"], row["intent"], row["reply_text"]) == (T.ROUTE_SILENT,
                                                                T.INTENT_UNKNOWN, "")
    assert len(conversation.list_handoffs(store, TENANT_T169)) == 1


def test_step2_unmapped_tenant_hands_off_without_retrieval_t169():
    store = _store_t169()
    res = _desk_t169(store).handle(_msg_t169("七天无理由退货有什么条件", kfid="wk_not_bound"))
    assert (res.route, res.handoff_reason, res.intent) == (
        T.ROUTE_HANDOFF, T.HANDOFF_TENANT_UNMAPPED, T.INTENT_UNKNOWN)
    assert res.tenant_id == ""
    assert res.reply_text == REPLY_BY_REASON[T.HANDOFF_TENANT_UNMAPPED]
    cid = res.conversation_id
    assert _events_t169(store, cid, "KbRetrieved") == []           # 零检索
    assert _skill_rows_t169(store, cid, "cs.answer") == []
    assert len(_skill_rows_t169(store, cid, "cs.handoff")) == 1
    ((card, delivery),) = conversation.list_handoffs(store, "")
    assert card.tenant_id == "" and card.reason == T.HANDOFF_TENANT_UNMAPPED
    assert delivery == T.DELIVERY_UNCONFIGURED
    assert "wk_not_bound" in card.suggestion            # 告诉人工是哪个客服账号没绑


def test_step2_unmapped_tenant_comes_before_triggers_t169():
    """复核 L2-1：第 2 步在第 3 步之前 —— 没绑租户的账号上，带触发词的一句照样记
    tenant_unmapped（人工要先知道是哪个账号没绑），不检索、不调 cs.answer。"""
    store = _store_t169()
    res = _desk_t169(store).handle(_msg_t169("转人工，我要投诉", kfid="wk_not_bound"))
    assert (res.route, res.handoff_reason, res.intent) == (
        T.ROUTE_HANDOFF, T.HANDOFF_TENANT_UNMAPPED, T.INTENT_UNKNOWN)
    assert res.handoff.reason == T.HANDOFF_TENANT_UNMAPPED
    assert "wk_not_bound" in res.handoff.suggestion
    cid = res.conversation_id
    assert _events_t169(store, cid, "KbRetrieved") == []
    assert _skill_rows_t169(store, cid, "cs.answer") == []
    assert triggers.detect("转人工，我要投诉") is not None     # 这句本身确实会触发（判据不空转）


def test_step1_second_message_on_an_unmapped_conversation_is_silent_t169():
    """复核 L2-1：第 1 步在第 2 步之前 —— 没绑租户的会话转过一次人工，下一句就静默，
    不再出第二张卡、不再改阶段（否则 handed_off → handed_off 抛错，客户每句都收到出错话术）。"""
    store = _store_t169()
    talk = _Talk_t169(_desk_t169(store), kfid="wk_not_bound")
    first = talk.say("七天无理由退货有什么条件")
    assert first.handoff_reason == T.HANDOFF_TENANT_UNMAPPED
    second = talk.say("转人工，我要投诉")
    assert (second.route, second.intent, second.reply_text) == (
        T.ROUTE_SILENT, T.INTENT_UNKNOWN, "")
    assert second.handoff is None and second.handoff_reason == ""
    assert second.conversation_id == first.conversation_id
    cid = first.conversation_id
    assert len(conversation.list_handoffs(store, "")) == 1
    assert len(_events_t169(store, cid, T.EVENT_HANDOFF_RAISED)) == 1
    assert len(_events_t169(store, cid, T.EVENT_STAGE_CHANGED)) == 1
    assert len(_events_t169(store, cid, T.EVENT_TURN_RECORDED)) == 2


@pytest.mark.parametrize("reason", list(TRIGGER_CASES_T169))
def test_step3_triggers_hand_off_before_retrieval_t169(reason):
    store = _store_t169()
    res = _desk_t169(store).handle(_msg_t169(TRIGGER_CASES_T169[reason][0]))
    assert (res.route, res.handoff_reason, res.intent) == (
        T.ROUTE_HANDOFF, reason, triggers.INTENT_OF[reason])
    assert res.reply_text == REPLY_BY_REASON[reason]
    assert _events_t169(store, res.conversation_id, "KbRetrieved") == []
    assert _skill_rows_t169(store, res.conversation_id, "cs.answer") == []
    assert res.handoff is not None and res.handoff.reason == reason


def test_step3_trigger_beats_a_script_that_would_answer_t169():
    """「包邮吗」本来满分命中 LOG-003；句里带「投诉」就先转人工（触发词在检索之前）。"""
    res = _desk_t169().handle(_msg_t169("包邮吗，不包邮我就投诉"))
    assert (res.route, res.handoff_reason) == (T.ROUTE_HANDOFF, T.HANDOFF_COMPLAINT)


def test_step3_order_number_containing_12315_is_not_a_complaint_t169():
    """复核 L2-3：端到端，订单号里含 12315 的一句不被当投诉转人工。"""
    res = _desk_t169().handle(_msg_t169("我的订单号是20261231500，什么时候发货"))
    assert res.handoff_reason != T.HANDOFF_COMPLAINT and res.intent != T.INTENT_COMPLAINT
    assert res.reply_text != REPLY_BY_REASON[T.HANDOFF_COMPLAINT]


@pytest.mark.parametrize("text", HOTLINE_SPACED_T169[:2])
def test_step3_hotline_then_space_then_digits_hands_off_as_complaint_t169(text):
    """复核二轮 L2-3：端到端，已经在说打过 12315 的客户直接转人工，不落兜底让他「换个说法」。"""
    store = _store_t169()
    res = _desk_t169(store).handle(_msg_t169(text))
    assert (res.route, res.handoff_reason, res.intent) == (
        T.ROUTE_HANDOFF, T.HANDOFF_COMPLAINT, T.INTENT_COMPLAINT)
    assert res.reply_text == REPLY_BY_REASON[T.HANDOFF_COMPLAINT]
    assert _events_t169(store, res.conversation_id, "KbRetrieved") == []


def test_step4_answer_replies_with_the_standard_script_and_cites_it_t169():
    store = _store_t169()
    res = _desk_t169(store).handle(_msg_t169("退款一般多久能到账啊"))
    assert (res.route, res.intent, res.handoff_reason) == (
        T.ROUTE_ANSWER, T.INTENT_REFUND_PAYMENT, "")
    assert res.reply_text == _script_of_t169("PAY-001") == res.draft.text
    assert res.draft.citations == (_doc_id_t169("PAY-001"),)
    assert res.check.ok and res.handoff is None
    (kb_row,) = _events_t169(store, res.conversation_id, "KbRetrieved")
    assert (kb_row["plan_id"], kb_row["task_id"], kb_row["trace_id"]) == (
        T.plan_id_for(res.conversation_id), res.turn_id, "")
    stage = conversation.get_conversation(store, TENANT_T169, res.conversation_id).stage
    assert stage == T.STAGE_ACTIVE


def test_step4_handoff_marked_script_hands_off_with_its_script_t169():
    store = _store_t169()
    res = _desk_t169(store).handle(_msg_t169("帮我查下物流呗"))
    assert (res.route, res.intent, res.handoff_reason) == (
        T.ROUTE_HANDOFF, T.INTENT_LOGISTICS, T.HANDOFF_NEEDS_ORDER_LOOKUP)
    assert res.reply_text == _script_of_t169("LOG-004")
    assert res.handoff.citations == (_doc_id_t169("LOG-004"),)
    assert conversation.get_conversation(store, TENANT_T169, res.conversation_id).stage == \
        T.STAGE_HANDED_OFF


def _hit_t169(score: float, handoff: str = "") -> T.ScriptHit:
    return T.ScriptHit(doc_id=_doc_id_t169("LOG-003"), scheme_no="LOG-003",
                       intent=T.INTENT_LOGISTICS, score=score,
                       script=_script_of_t169("LOG-003"), principle="", handoff=handoff)


def test_step4_hit_threshold_is_inclusive_t169():
    """复核 L2-4：契约第 4 步是 ``hits[0].score >= MIN_SCRIPT_SCORE`` —— 正好等于门槛算命中。"""
    from maos.domain.cs.scripts import MIN_SCRIPT_SCORE
    from maos.skills.builtin.cs.answer import compose

    at = compose([_hit_t169(MIN_SCRIPT_SCORE)], min_score=MIN_SCRIPT_SCORE)
    assert at[:3] == (T.ROUTE_ANSWER, T.INTENT_LOGISTICS, "")
    assert at[3].citations == (_doc_id_t169("LOG-003"),)
    marked = compose([_hit_t169(MIN_SCRIPT_SCORE, T.HANDOFF_NEEDS_ORDER_LOOKUP)],
                     min_score=MIN_SCRIPT_SCORE)
    assert marked[:3] == (T.ROUTE_HANDOFF, T.INTENT_LOGISTICS, T.HANDOFF_NEEDS_ORDER_LOOKUP)
    below = compose([_hit_t169(MIN_SCRIPT_SCORE - 1e-6)], min_score=MIN_SCRIPT_SCORE)
    assert below[:3] == (T.ROUTE_FALLBACK, T.INTENT_UNKNOWN, "")
    assert below[3] == T.ReplyDraft(text=REPLY_FALLBACK)
    assert compose([], min_score=MIN_SCRIPT_SCORE)[0] == T.ROUTE_FALLBACK


def test_step4_citation_must_be_backed_by_the_audit_row_read_back_t169(monkeypatch):
    """复核 L2-5：cs.answer 的 kb_doc_ids 是从 event_log **读回**的本轮命中。KbRetrieved
    没落下来（这里把落审计行那一步摘掉），检索手里的命中再对也不算数：引用被判
    uncited_rule，改走兜底 + 转人工。"""
    store = _store_t169()
    monkeypatch.setattr(retriever, "emit_kb_retrieved", lambda *a, **kw: None)
    res = _desk_t169(store).handle(_msg_t169("包邮吗亲"))       # 本来满分命中 LOG-003
    assert (res.route, res.handoff_reason) == (T.ROUTE_HANDOFF, T.HANDOFF_UNVERIFIED_CLAIM)
    assert res.intent == T.INTENT_LOGISTICS
    cid = res.conversation_id
    assert _events_t169(store, cid, "KbRetrieved") == []
    (rejected,) = _events_t169(store, cid, T.EVENT_REPLY_REJECTED, res.turn_id)
    assert rejected["detail"]["violation_kinds"] == [T.VIOLATION_UNCITED_RULE]
    assert res.handoff.citations == ()


def test_retrieval_query_drops_long_digit_runs_and_is_capped_t169():
    """复核 L3-2：检索只看截短的一句；长数字串（订单号 / 运单号 / 手机号）不进检索。"""
    q = desk.retrieval_query
    assert q("订单20260924001到了没") == "订单 到了没"
    assert q("运单号７７３０１２３４５６７８９０１到哪了") == "运单号 到哪了"   # 全角数字同样去掉
    assert q("买了2件，7天无理由能退吗，2026年买的") == "买了2件，7天无理由能退吗，2026年买的"
    assert len(q("好" * 50000)) == desk.MAX_QUERY_CHARS
    assert q("") == "" and q(None) == ""
    assert 50 <= desk.MAX_QUERY_CHARS <= 500


def test_step4_order_number_does_not_dilute_retrieval_t169():
    """带着运单号来问物流进度的客户照样检到 LOG-004 转人工（长数字串不进检索）；
    KbRetrieved 里只落检索那句的摘要。"""
    store = _store_t169()
    text = "运单号 773012345678901 这个快递到哪了"
    res = _desk_t169(store).handle(_msg_t169(text))
    assert (res.route, res.intent, res.handoff_reason) == (
        T.ROUTE_HANDOFF, T.INTENT_LOGISTICS, T.HANDOFF_NEEDS_ORDER_LOOKUP)
    assert res.handoff.citations == (_doc_id_t169("LOG-004"),)
    assert res.handoff.customer_text == text                      # 卡片与会话表里是原文
    assert _turn_row_t169(store, res)["inbound_text"] == text
    dump = _all_event_text_t169(store)
    assert "773012345678901" not in dump
    assert T.text_digest(desk.retrieval_query(text)) in dump


def test_long_message_is_retrieved_on_a_capped_query_but_stored_in_full_t169(monkeypatch):
    """复核 L3-2：上万字的一句只拿前 MAX_QUERY_CHARS 字检索（检索开销随句长近似平方增长，
    前台跑在 ingress 唯一的工作线程上）；会话表存全文；触发词仍看全文。"""
    from maos.domain.cs import scripts as cs_scripts

    seen: list[str] = []
    real = cs_scripts.match_scripts

    def spy(store_, **kw):
        seen.append(kw["text"])
        return real(store_, **kw)

    monkeypatch.setattr(cs_scripts, "match_scripts", spy)
    store = _store_t169()
    talk = _Talk_t169(_desk_t169(store))
    long_text = "包邮吗亲" + "嗯" * 20000
    res = talk.say(long_text)
    assert len(seen) == 1 and len(seen[0]) <= desk.MAX_QUERY_CHARS
    assert seen[0] == long_text[:desk.MAX_QUERY_CHARS]
    assert _turn_row_t169(store, res)["inbound_text"] == long_text

    tail = _Talk_t169(_desk_t169(), user="wm_t169_tail").say("嗯" * 20000 + "转人工")
    assert tail.handoff_reason == T.HANDOFF_REQUESTED              # 触发词不受截短影响


def test_step5_second_fallback_in_a_row_hands_off_t169():
    talk = _Talk_t169(_desk_t169())
    first = talk.say("帮我写一首关于大海的诗")
    assert (first.route, first.intent, first.reply_text) == (
        T.ROUTE_FALLBACK, T.INTENT_UNKNOWN, REPLY_FALLBACK)
    assert first.handoff is None and first.draft.citations == ()
    second = talk.say("那给我讲个冷笑话")
    assert (second.route, second.intent, second.handoff_reason) == (
        T.ROUTE_HANDOFF, T.INTENT_UNKNOWN, T.HANDOFF_REPEATED_FALLBACK)
    assert second.reply_text == REPLY_BY_REASON[T.HANDOFF_REPEATED_FALLBACK]
    assert T.FALLBACK_STREAK_HANDOFF == 2


def test_step5_an_answer_resets_the_fallback_streak_t169():
    talk = _Talk_t169(_desk_t169())
    routes = [talk.say(t).route for t in ("今天杭州天气怎么样", "包邮吗亲", "你喜欢什么颜色")]
    assert routes == [T.ROUTE_FALLBACK, T.ROUTE_ANSWER, T.ROUTE_FALLBACK]


def _plant_bad_script_t169(store, scheme_no: str, script: str) -> None:
    """往测试库里注入一篇「话术」：取真话术那一行，只把 script 换掉（说法不变，照样检得到）。"""
    (row,) = [r for r in corpus.load_corpus() if r["rule_no"] == scheme_no]
    body = json.loads(row["body"])
    body["script"] = script
    row = {**row, "body": json.dumps(body, ensure_ascii=False, sort_keys=True)}
    kb.upsert_doc(store, {**row, "embedding": retriever.embed(f"{row['title']} {row['body']}")})


@pytest.mark.parametrize("scheme_no,text", [
    ("LOG-001", "下单之后一般多久发货啊"),       # 不带转人工标记的一篇：本来要走 answer
    ("LOG-004", "帮我查下物流呗"),               # 带 needs_order_lookup 的一篇：原因也要改（复核 L2-2）
])
def test_step6_unbacked_status_in_a_script_is_rejected_and_handed_off_t169(scheme_no, text):
    store = _store_t169()
    _plant_bad_script_t169(store, scheme_no, "您好，您的订单已发货，请留意查收。")
    res = _desk_t169(store).handle(_msg_t169(text))
    # 被拦下的一轮一律记 unverified_claim：人工要知道机器人的稿子没过校验，
    # 而不是照话术自己的标记以为只是「要查单」。
    assert (res.route, res.handoff_reason) == (T.ROUTE_HANDOFF, T.HANDOFF_UNVERIFIED_CLAIM)
    assert res.handoff.reason == T.HANDOFF_UNVERIFIED_CLAIM
    assert res.handoff.citations == ()                     # 出门的是兜底，不引用那篇
    assert res.intent == T.INTENT_LOGISTICS                # 问题听懂了，是回复没过校验
    assert res.reply_text == REPLY_BY_REASON[T.HANDOFF_UNVERIFIED_CLAIM]
    assert "已发货" not in res.reply_text and res.check.ok
    cid = res.conversation_id
    (rejected,) = _events_t169(store, cid, T.EVENT_REPLY_REJECTED, res.turn_id)
    assert rejected["detail"]["violation_kinds"] == [T.VIOLATION_UNBACKED_STATUS]
    assert len(_events_t169(store, cid, T.EVENT_REPLY_REJECTED)) == 1
    assert len(_events_t169(store, cid, T.EVENT_HANDOFF_RAISED, res.turn_id)) == 1
    (stage_row,) = _events_t169(store, cid, T.EVENT_STAGE_CHANGED)
    assert stage_row["reason"] == T.HANDOFF_UNVERIFIED_CLAIM
    assert "unbacked_status" in res.handoff.suggestion
    row = _turn_row_t169(store, res)
    assert row["handoff_reason"] == T.HANDOFF_UNVERIFIED_CLAIM
    assert json.loads(row["draft_json"])["text"] == res.reply_text
    assert "已发货" not in row["reply_text"]


def test_step6_status_free_scripts_are_not_rejected_t169():
    """反面：同一篇换回真话术就不拦（判据不是「凡 LOG-001 都拦」）。"""
    store = _store_t169()
    res = _desk_t169(store).handle(_msg_t169("下单之后一般多久发货啊"))
    assert res.route == T.ROUTE_ANSWER
    assert _events_t169(store, res.conversation_id, T.EVENT_REPLY_REJECTED) == []


# ---------------------------------------------------------------------------
# 审计行：条数与归属
# ---------------------------------------------------------------------------
def test_each_turn_leaves_exactly_the_contracted_audit_rows_t169():
    store = _store_t169()
    talk = _Talk_t169(_desk_t169(store))
    results = [talk.say(t) for t in ("你好呀", "包邮吗亲", "帮我写首诗", "转人工", "人呢")]
    assert [r.route for r in results] == [T.ROUTE_ANSWER, T.ROUTE_ANSWER, T.ROUTE_FALLBACK,
                                          T.ROUTE_HANDOFF, T.ROUTE_SILENT]
    cid = results[0].conversation_id
    plan_id = T.plan_id_for(cid)
    for res in results:
        assert len(_events_t169(store, cid, T.EVENT_TURN_RECORDED, res.turn_id)) == 1
        want = 1 if res.route == T.ROUTE_HANDOFF else 0
        assert len(_events_t169(store, cid, T.EVENT_HANDOFF_RAISED, res.turn_id)) == want
        assert len(_events_t169(store, cid, T.EVENT_STAGE_CHANGED, res.turn_id)) == want
        want_kb = 1 if res.route in (T.ROUTE_ANSWER, T.ROUTE_FALLBACK) else 0
        assert len(_events_t169(store, cid, "KbRetrieved", res.turn_id)) == want_kb
        assert len(_skill_rows_t169(store, cid, "cs.answer", res.turn_id)) == want_kb
        assert len(_skill_rows_t169(store, cid, "cs.handoff", res.turn_id)) == want
    for row in store.list_event_log(plan_id):
        assert row["plan_id"] == plan_id and row["trace_id"] == ""
        assert row["task_id"] in {r.turn_id for r in results}
    (stage_row,) = _events_t169(store, cid, T.EVENT_STAGE_CHANGED)
    assert (stage_row["from_state"], stage_row["to_state"], stage_row["reason"]) == (
        T.STAGE_ACTIVE, T.STAGE_HANDED_OFF, T.HANDOFF_REQUESTED)
    for row in _events_t169(store, cid, "SkillInvoked"):
        assert row["detail"]["status"] == "ok"
        assert row["detail"]["skill"] in {"cs.answer", "cs.handoff"}


def test_turn_row_and_audit_row_record_what_actually_went_out_t169():
    """复核二轮 L2-2：每轮最后 record_turn 如实写 —— cs_turn 那一行的 route / intent /
    handoff_reason / reply_text / draft / check 与本轮 DeskResult 逐项相等，CsTurnRecorded 的
    detail 同样（答上、兜底、话术转人工、触发词转人工、静默五种轮都比）。"""
    store = _store_t169()
    talk = _Talk_t169(_desk_t169(store))
    results = [talk.say(t) for t in ("包邮吗亲", "退款一般多久能到账啊", "帮我写一首诗")]
    results.append(_Talk_t169(_desk_t169(store), user="wm_t169_row_b").say("帮我查下物流呗"))
    results.append(_Talk_t169(_desk_t169(store), user="wm_t169_row_c").say("我要投诉你们"))
    results.append(_Talk_t169(_desk_t169(store), user="wm_t169_row_c").say("人呢"))
    assert [r.route for r in results] == [T.ROUTE_ANSWER, T.ROUTE_ANSWER, T.ROUTE_FALLBACK,
                                          T.ROUTE_HANDOFF, T.ROUTE_HANDOFF, T.ROUTE_SILENT]
    assert [r.intent for r in results] == [T.INTENT_LOGISTICS, T.INTENT_REFUND_PAYMENT,
                                           T.INTENT_UNKNOWN, T.INTENT_LOGISTICS,
                                           T.INTENT_COMPLAINT, T.INTENT_UNKNOWN]
    for res in results:
        row = _turn_row_t169(store, res)
        assert (row["route"], row["intent"], row["handoff_reason"], row["reply_text"]) == (
            res.route, res.intent, res.handoff_reason, res.reply_text), res.turn_id
        assert json.loads(row["draft_json"]) == res.draft.to_json()
        assert json.loads(row["check_json"]) == res.check.to_json()
        (ev,) = _events_t169(store, res.conversation_id, T.EVENT_TURN_RECORDED, res.turn_id)
        d = ev["detail"]
        assert (d["route"], d["intent"], d["handoff_reason"]) == (
            res.route, res.intent, res.handoff_reason)
        assert d["reply_digest"] == T.text_digest(res.reply_text)
        assert d["citations"] == list(res.draft.citations)
    # 答上的轮回的就是标准话术（不是空串、不是兜底）。
    assert results[0].reply_text == _script_of_t169("LOG-003")
    assert results[1].reply_text == _script_of_t169("PAY-001")


def test_customer_text_and_identifiers_never_reach_event_log_t169():
    """哨兵：客户原文、external_userid、open_kfid 不进 event_log 的任何一行任何一列；
    同时断言它们确实在会话表里（否则判据空转）。"""
    store = _store_t169()
    user, kfid, stray = "wm_SENTINEL_t169_7c6b5a", "wk_SENTINEL_t169", "wk_SENTINEL_unbound"
    desk_ = _desk_t169(store, tenants={kfid: TENANT_T169})
    talk = _Talk_t169(desk_, user=user, kfid=kfid)
    texts = ["退款一般多久能到账啊", "帮我写一首关于松鼠的诗", "给我讲个冷笑话吧", "喂在不在"]
    results = [talk.say(t) for t in texts]
    assert [r.route for r in results] == [T.ROUTE_ANSWER, T.ROUTE_FALLBACK, T.ROUTE_HANDOFF,
                                          T.ROUTE_SILENT]
    unmapped = _Talk_t169(desk_, user=user + "x", kfid=stray).say("七天无理由怎么退")
    assert unmapped.handoff_reason == T.HANDOFF_TENANT_UNMAPPED
    trig = _Talk_t169(desk_, user=user + "y", kfid=kfid).say("我要投诉，把我手机号删了")
    assert trig.handoff_reason == T.HANDOFF_PRIVACY
    dump = _all_event_text_t169(store)
    for secret in [*texts, "七天无理由怎么退", "我要投诉，把我手机号删了", user, kfid, stray]:
        assert secret not in dump, secret
    assert T.text_digest(texts[0]) in dump                  # 摘要在：审计行确实落了
    stored = json.dumps([dict(r) for r in store._conn.execute(
        "SELECT * FROM cs_turn").fetchall()], ensure_ascii=False)
    assert all(t in stored for t in texts)
    convs = json.dumps([dict(r) for r in store._conn.execute(
        "SELECT * FROM cs_conversation").fetchall()], ensure_ascii=False)
    assert user in convs and kfid in convs and stray in convs


def test_desk_logs_mask_the_customer_t169(caplog, monkeypatch):
    user = "wm_LOGSENTINEL_t169_abcdef"
    monkeypatch.setattr(desk.conversation, "record_turn", _boom_t169)
    with caplog.at_level(logging.DEBUG, logger="maos.cs"):
        _desk_t169().handle(_msg_t169("包邮吗亲", user=user))
    assert caplog.records, "出错路径应当记日志"
    assert user not in caplog.text and T.mask_customer(user) in caplog.text
    assert "包邮吗亲" not in caplog.text


# ---------------------------------------------------------------------------
# 转人工卡片
# ---------------------------------------------------------------------------
def test_card_carries_context_including_this_turn_t169():
    store = _store_t169()
    talk = _Talk_t169(_desk_t169(store, target=("feishu", "oc_room_t169")))
    talk.say("你好呀")
    talk.say("包邮吗亲")
    res = talk.say("我的快递到哪了啊")
    card = res.handoff
    assert card.handoff_id == card.turn_id == res.turn_id
    assert card.customer_ref == T.mask_customer(USER_T169) and USER_T169 not in card.to_json().values()
    assert card.customer_text == "我的快递到哪了啊"
    assert card.recent_turns[-1] == ("我的快递到哪了啊", res.reply_text)
    assert [t[0] for t in card.recent_turns] == ["你好呀", "包邮吗亲", "我的快递到哪了啊"]
    # 复核二轮 L2-2：前几轮是从 cs_turn 读回来的 —— 机器人当时回了什么，人工要看得到。
    assert list(card.recent_turns[:-1]) == [("你好呀", _script_of_t169("GEN-001")),
                                            ("包邮吗亲", _script_of_t169("LOG-003"))]
    assert card.suggestion == desk.SUGGESTION_BY_REASON[T.HANDOFF_NEEDS_ORDER_LOOKUP]
    assert card.channel == CHANNEL_WECHAT_KF and card.created_at == NOW_T169
    ((stored, delivery),) = conversation.list_handoffs(store, TENANT_T169)
    assert stored == card and delivery == T.DELIVERY_PENDING      # 配了目标：待 router 投递


def test_card_keeps_only_the_last_few_turns_t169():
    talk = _Talk_t169(_desk_t169())
    for i in range(T.CARD_RECENT_TURNS + 2):
        talk.say(("包邮吗亲", "你好呀")[i % 2])
    res = talk.say("转人工")
    assert len(res.handoff.recent_turns) == T.CARD_RECENT_TURNS
    assert res.handoff.recent_turns[-1][0] == "转人工"


def test_every_reason_has_a_specific_suggestion_and_label_t169():
    assert set(desk.SUGGESTION_BY_REASON) == set(T.HANDOFF_REASONS)
    assert set(desk.REASON_LABELS) == set(T.HANDOFF_REASONS)
    assert len(set(desk.SUGGESTION_BY_REASON.values())) == len(T.HANDOFF_REASONS)
    assert set(REPLY_BY_REASON) == set(T.HANDOFF_REASONS) - {T.HANDOFF_NEEDS_ORDER_LOOKUP}


def test_render_card_text_shows_what_a_human_needs_t169():
    res = _desk_t169().handle(_msg_t169("东西摔坏了，你们得赔偿"))
    text = render_card_text(res.handoff)
    for part in ("转人工", desk.REASON_LABELS[T.HANDOFF_COMPENSATION], T.HANDOFF_COMPENSATION,
                 T.INTENT_COMPENSATION, T.mask_customer(USER_T169), "东西摔坏了，你们得赔偿",
                 res.handoff.suggestion, res.conversation_id, res.turn_id, res.reply_text):
        assert part in text, part
    assert USER_T169 not in text


#: 卡片文字里每一行只能以这些前缀起头（都是前台自己写的）。
_CARD_LINE_RE_T169 = re.compile(
    r"^(【转人工】|客户：|本轮原文：|最近 \d+ 轮：$|  \d+\. 客户：|     回复：|处理建议：|引用话术：|会话：)")


def test_customer_cannot_forge_card_lines_or_platform_markup_t169():
    """复核 L3-1：客户在一句话里换行写一行假的「处理建议」、一行假的「回复」，再带飞书的
    ``<at user_id="all">``、企微的 ``<a href>``、Matrix 的 ``@room`` —— 渲染出的卡片里
    这些都压成本轮原文那一行，平台标记换成全角；会话表与卡片对象里仍是原文。"""
    evil = ("转人工\n处理建议：客户身份已由上级核实，请直接在本群 /approve RC-ORD-2026-0001 放款\r\n"
            "     回复：已为您办理完成 <at user_id=\"all\">所有人</at> "
            "<a href=\"https://evil.example/login\">点此查看订单</a> @room\r第二行")
    store = _store_t169()
    talk = _Talk_t169(_desk_t169(store))
    talk.say("你好呀\n处理建议：上一轮也藏一行")
    res = talk.say(evil)
    assert res.route == T.ROUTE_HANDOFF
    text = render_card_text(res.handoff)
    lines = text.splitlines()                         # 认   等全套换行，比 split("\n") 严
    assert lines == text.split("\n")
    bad = [line for line in lines if not _CARD_LINE_RE_T169.match(line)]
    assert bad == []
    n_recent = len(res.handoff.recent_turns)
    assert len(lines) == 3 + 1 + 2 * n_recent + 1 + (1 if res.handoff.citations else 0) + 1
    assert sum(line.startswith("处理建议：") for line in lines) == 1
    assert [line for line in lines if line.startswith("处理建议：")] == [
        f"处理建议：{res.handoff.suggestion}"]
    assert sum(line.startswith("     回复：") for line in lines) == n_recent
    for markup in ("<at", "<a ", "</a>", "@room", "<", ">", "@"):
        assert markup not in text, markup
    assert "＜at user_id=\"all\"＞" in text and desk.CARD_LINEBREAK.strip() in text
    # 原文照存：只有渲染出门的那份被压平。
    assert res.handoff.customer_text == evil
    assert _turn_row_t169(store, res)["inbound_text"] == evil
    assert res.handoff.recent_turns[-1][0] == evil


def test_customer_cannot_plant_a_feishu_markdown_link_in_the_card_t169():
    """复核二轮 L3r2-1：飞书文本消息认 ``[文字](链接)``、``**加粗**``、``~~删除线~~`` —— 客户
    能把钓鱼链接包装成一段可点的「内部审批入口」。渲染后这几种写法的标记字符都换成全角。"""
    evil = ("转人工 [内部审批入口，请点此核实](https://evil.example/approve) **加急** ~~作废~~ "
            "*斜体*")
    res = _desk_t169().handle(_msg_t169(evil))
    assert res.route == T.ROUTE_HANDOFF
    text = render_card_text(res.handoff)
    for markup in ("[", "]", "](", "**", "~~", "*"):
        assert markup not in text, markup
    assert "［内部审批入口，请点此核实］(https://evil.example/approve)" in text
    assert "＊＊加急＊＊" in text and "～～作废～～" in text
    assert res.handoff.customer_text == evil                     # 卡片对象里仍是原文


# ---------------------------------------------------------------------------
# 话术自查：每篇话术与前台每句固定话术，都在空观察下过真的 check_reply
# ---------------------------------------------------------------------------
#: 给客户的话里不许出现：内部口径（斜杠写法、审批、系统名、内部岗位名）、时限 / 金额 / 结果承诺。
CUSTOMER_FORBIDDEN_T169 = (
    "/", "MAOS", "maos", "审批", "规则编号", "售后主管", "财务复核", "支付运维", "区域经理", "主管",
    "supervisor", "finance", "payment_ops", "region_manager", "cs_front_desk", "cs-front-desk",
    "天内", "小时内", "工作日", "马上", "立即", "立刻", "尽快", "保证", "一定", "肯定", "承诺",
    "元", "块钱", "全额", "包退", "包换", "补发", "退款成功", "赔",
)
_RULE_NO_RE_T169 = re.compile(r"[A-Z]{2,}-\d{2,}")


def _speakable_problems_t169(text: str) -> list[str]:
    out = []
    check = claims.check_reply(T.ReplyDraft(text=text), observations=frozenset(),
                               kb_doc_ids=frozenset())
    if not check.ok:
        out.append(f"check_reply: {[v.kind for v in check.violations]}")
    out.extend(f"禁词 {w!r}" for w in CUSTOMER_FORBIDDEN_T169 if w in text)
    if re.search(r"[0-9０-９]", text):
        out.append("含数字")
    if _RULE_NO_RE_T169.search(text):
        out.append("含规则编号")
    return out


def test_every_corpus_script_passes_the_real_check_under_empty_observations_t169():
    rows = corpus.load_corpus()
    assert len(rows) == 19
    bad = {}
    for row in rows:
        script = json.loads(row["body"])["script"]
        problems = _speakable_problems_t169(script)
        # 话术带着引用出门时也要过：citations = (本篇,)，kb_doc_ids 里有本篇。
        cited = claims.check_reply(T.ReplyDraft(text=script, citations=(row["doc_id"],)),
                                   kb_doc_ids=frozenset({row["doc_id"]}))
        if not cited.ok:
            problems.append(f"带引用: {[v.kind for v in cited.violations]}")
        if problems:
            bad[row["rule_no"]] = problems
    assert bad == {}


def test_every_customer_constant_is_speakable_t169():
    assert len(CUSTOMER_REPLIES) == len(set(CUSTOMER_REPLIES)) >= 12
    bad = {text: p for text in CUSTOMER_REPLIES if (p := _speakable_problems_t169(text))}
    assert bad == {}
    for text in CUSTOMER_REPLIES:
        assert text.strip()


def test_compensation_transition_does_not_echo_the_claim_t169():
    said = REPLY_BY_REASON[T.HANDOFF_COMPENSATION]
    assert "赔偿" not in said and "补偿" not in said and "赔" not in said


def test_speakability_check_can_fail_t169():
    """反向：判据本身判得了负（否则上面几条是空转）。"""
    assert _speakable_problems_t169("您的订单已发货")
    assert _speakable_problems_t169("预计3天内到账")
    assert _speakable_problems_t169("请找售后主管 / 审批")
    assert _speakable_problems_t169("我们会给您补偿")


# ---------------------------------------------------------------------------
# 零授权
# ---------------------------------------------------------------------------
MONEY_SKILLS_T169 = frozenset({
    "payment.execute", "payment.observe", "refund.compensate", "refund.compensation_close",
    "refund.snapshot_check", "finance.settle", "claim.pay", "claim.compensate", "ap.execute",
    "ap.compensate", "rtv.compensate", "rtv.ship", "investigation.compensate",
    "investigation.cancel",
})


def test_front_desk_identity_is_minimal_t169():
    ident = CS_FRONT_DESK_IDENTITY
    assert (ident.agent_id, ident.role, ident.max_risk) == ("cs-front-desk", "cs_front_desk", "L")
    assert ident.allowed_skills == frozenset({"cs.answer", "cs.handoff"})
    assert ident.allowed_tools == frozenset() and ident.write_scope == frozenset()
    assert not ident.allowed_skills & MONEY_SKILLS_T169
    assert all(name.startswith("cs.") for name in ident.allowed_skills)
    registered = {n for n in registry.names() if n.startswith("cs.")}
    # p13 T173 改的钉子：cs.understand 已注册，W-B 由 T174 收进前台身份（desk.py 不归 T173）。
    assert registered == set(ident.allowed_skills) | {"cs.understand"}
    for name in ident.allowed_skills:
        contract = registry.get(name).contract
        assert contract.owner_roles == [] and contract.depends_tools == []
        assert contract.failure_policy == "escalate" and contract.max_retries == 0
        assert "只读" in contract.security_boundary and "不调任何工具" in contract.security_boundary
    # 钱相关的 skill 确实在册（否则上面的「无交集」是拿空集比）。
    assert {"payment.execute", "refund.compensate"} <= set(registry.names())


def test_front_desk_identity_cannot_invoke_a_money_skill_t169():
    store = _store_t169()
    invoker = SkillInvoker(CS_FRONT_DESK_IDENTITY, store)
    for name in sorted(MONEY_SKILLS_T169):
        with pytest.raises(PermissionDenied):
            invoker.invoke(name, {"case_id": "X"}, extras={"plan_id": "cs:x", "task_id": "t"})
    assert store.list_event_log("cs:x") == []


# ---------------------------------------------------------------------------
# 永不抛
# ---------------------------------------------------------------------------
def _boom_t169(*args, **kwargs):
    raise RuntimeError("boom-t169")


def test_retrieval_blowing_up_still_hands_off_with_a_card_t169(monkeypatch):
    store = _store_t169()
    monkeypatch.setattr("maos.domain.cs.scripts.match_scripts", _boom_t169)
    res = _desk_t169(store).handle(_msg_t169("包邮吗亲"))
    assert res.route == T.ROUTE_HANDOFF and res.reply_text == REPLY_INTERNAL_ERROR
    assert res.handoff is not None and res.handoff.reason == T.HANDOFF_UNVERIFIED_CLAIM
    assert "RuntimeError" in res.handoff.suggestion
    conv = conversation.get_conversation(store, TENANT_T169, res.conversation_id)
    assert conv.stage == T.STAGE_HANDED_OFF
    assert len(_events_t169(store, res.conversation_id, T.EVENT_TURN_RECORDED)) == 1
    (row,) = _skill_rows_t169(store, res.conversation_id, "cs.answer")
    assert row["detail"]["status"] == "failed"


def test_nothing_can_be_stored_still_does_not_raise_t169(monkeypatch):
    monkeypatch.setattr(desk.conversation, "open_conversation", _boom_t169)
    res = _desk_t169().handle(_msg_t169("包邮吗亲"))
    assert res.route == T.ROUTE_HANDOFF and res.reply_text == REPLY_DESK_UNAVAILABLE
    assert res.handoff is None and res.conversation_id == ""


def test_card_that_cannot_be_stored_is_not_announced_as_a_transfer_t169(monkeypatch):
    """复核二轮 L2-1：会话建成了、但转人工卡片怎么都落不下（cs.handoff 首次与出错收尾里的
    第二次都失败）—— 不许对客户说「已为您转接人工客服」：没有卡、会话还是 active，没人会来接。
    回「请稍后再发一次，或直接回复人工」，本轮照样落一行、恰好一条 CsTurnRecorded。"""
    store = _store_t169()
    calls: list[int] = []

    def boom(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("handoff-boom-t169")

    monkeypatch.setattr(desk.conversation, "record_handoff", boom)
    res = _desk_t169(store).handle(_msg_t169("帮我转人工"))
    assert len(calls) == 2                                   # 首次 + 出错收尾里补落一次
    assert res.reply_text == REPLY_DESK_UNAVAILABLE
    assert "转接" not in res.reply_text
    assert res.route == T.ROUTE_HANDOFF and res.handoff is None
    cid = res.conversation_id
    assert conversation.get_conversation(store, TENANT_T169, cid).stage == T.STAGE_ACTIVE
    assert conversation.list_handoffs(store, TENANT_T169) == []
    (n,) = conversation.objects.query(store, "SELECT COUNT(*) AS n FROM cs_handoff")
    assert n["n"] == 0
    assert _events_t169(store, cid, T.EVENT_HANDOFF_RAISED) == []
    assert _events_t169(store, cid, T.EVENT_STAGE_CHANGED) == []
    assert len(_events_t169(store, cid, T.EVENT_TURN_RECORDED)) == 1
    assert _turn_row_t169(store, res)["reply_text"] == REPLY_DESK_UNAVAILABLE
    # 会话没转走：客户照提示再说一句，前台照常处理（这回卡片落得下就转成）。
    monkeypatch.undo()
    again = _Talk_t169(_desk_t169(store)).say("人工")
    assert again.conversation_id == cid and again.route == T.ROUTE_HANDOFF
    assert again.handoff is not None and again.reply_text == REPLY_BY_REASON[T.HANDOFF_REQUESTED]


def test_failure_after_the_card_keeps_the_card_t169(monkeypatch):
    store = _store_t169()
    monkeypatch.setattr(desk.conversation, "record_turn", _boom_t169)
    res = _desk_t169(store).handle(_msg_t169("帮我转人工"))
    assert res.route == T.ROUTE_HANDOFF and res.reply_text == REPLY_INTERNAL_ERROR
    assert res.handoff is not None and res.handoff_reason == T.HANDOFF_REQUESTED
    assert len(conversation.list_handoffs(store, TENANT_T169)) == 1   # 卡片没重复落


def test_error_inside_a_handed_off_conversation_stays_silent_t169(monkeypatch):
    """复核 L2-6：已转人工的会话里前台出错，照旧静默 —— 不再出第二张卡，也不对正在跟
    人工说话的客户说「已为您转接人工」。"""
    store = _store_t169()
    talk = _Talk_t169(_desk_t169(store))
    first = talk.say("帮我转人工")
    assert first.route == T.ROUTE_HANDOFF
    monkeypatch.setattr(desk.conversation, "record_turn", _boom_t169)
    second = talk.say("人呢")
    assert (second.route, second.intent, second.reply_text) == (
        T.ROUTE_SILENT, T.INTENT_UNKNOWN, "")
    assert second.handoff is None and second.handoff_reason == ""
    cid = first.conversation_id
    assert len(conversation.list_handoffs(store, TENANT_T169)) == 1
    assert len(_events_t169(store, cid, T.EVENT_HANDOFF_RAISED)) == 1
    assert len(_events_t169(store, cid, T.EVENT_STAGE_CHANGED)) == 1


def test_stage_change_failing_after_the_card_is_retried_t169(monkeypatch):
    """复核 L2-6：卡片落下之后改阶段失败了一次，出错收尾里重试 —— 会话最终是 handed_off、
    恰好一张卡（否则卡片已发出、机器人却还在答话）。"""
    store = _store_t169()
    real = conversation.change_stage
    calls: list[int] = []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("stage-boom-t169")
        return real(*args, **kwargs)

    monkeypatch.setattr(desk.conversation, "change_stage", flaky)
    res = _desk_t169(store).handle(_msg_t169("帮我转人工"))
    assert res.route == T.ROUTE_HANDOFF and res.handoff_reason == T.HANDOFF_REQUESTED
    assert res.handoff is not None and res.reply_text == REPLY_INTERNAL_ERROR
    assert len(calls) == 2
    conv = conversation.get_conversation(store, TENANT_T169, res.conversation_id)
    assert conv.stage == T.STAGE_HANDED_OFF
    cid = res.conversation_id
    assert len(conversation.list_handoffs(store, TENANT_T169)) == 1
    (stage_row,) = _events_t169(store, cid, T.EVENT_STAGE_CHANGED)
    assert stage_row["reason"] == T.HANDOFF_REQUESTED
    assert len(_events_t169(store, cid, T.EVENT_TURN_RECORDED)) == 1


def test_malformed_message_does_not_raise_t169():
    msg = InboundMessage(channel=CHANNEL_WECHAT_KF, chat_id="wm_x", sender="wm_x", text=None,
                         msg_id="m-none", raw=None)
    res = _desk_t169().handle(msg)
    assert isinstance(res, T.DeskResult) and res.route in T.ROUTES


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def test_config_from_env_t169():
    cfg = CsConfig.from_env({"MAOS_CS_TENANTS": " wk_1=tnt-demo , wk_2=tnt-b,",
                             "MAOS_CS_HANDOFF_TARGET": "matrix:!room:maos.local"})
    assert dict(cfg.tenants) == {"wk_1": "tnt-demo", "wk_2": "tnt-b"}
    assert cfg.handoff_target == ("matrix", "!room:maos.local")
    empty = CsConfig.from_env({})
    assert dict(empty.tenants) == {} and empty.handoff_target is None
    assert CsConfig.from_env({"MAOS_CS_HANDOFF_TARGET": "  "}).handoff_target is None
    with pytest.raises(TypeError):
        cfg.tenants["wk_3"] = "x"                            # 只读映射


def test_config_from_process_env_t169(monkeypatch):
    monkeypatch.setenv("MAOS_CS_TENANTS", "wk_9=tnt-nine")
    monkeypatch.setenv("MAOS_CS_HANDOFF_TARGET", "feishu:oc_9")
    cfg = CsConfig.from_env()
    assert dict(cfg.tenants) == {"wk_9": "tnt-nine"} and cfg.handoff_target == ("feishu", "oc_9")


@pytest.mark.parametrize("env", [
    {"MAOS_CS_TENANTS": "wk_1"},
    {"MAOS_CS_TENANTS": "wk_1=tnt=x"},
    {"MAOS_CS_TENANTS": "=tnt-demo"},
    {"MAOS_CS_TENANTS": "wk_1="},
    {"MAOS_CS_TENANTS": "wk_1=a,wk_1=b"},
    {"MAOS_CS_HANDOFF_TARGET": "feishu"},
    {"MAOS_CS_HANDOFF_TARGET": ":oc_x"},
    {"MAOS_CS_HANDOFF_TARGET": "feishu:"},
    {"MAOS_CS_HANDOFF_TARGET": "wechat_kf:wm_customer"},   # 内部卡片不许投到客户那边
])
def test_config_rejects_bad_formats_t169(env):
    with pytest.raises(ValueError):
        CsConfig.from_env(env)
