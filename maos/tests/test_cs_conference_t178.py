"""T178 · 复杂投诉圆桌会诊卡（review/p14-cs-contracts.md §2「T178」）。

* 契约面：SeatFinding / ConferenceCard / SEATS / RECOMMENDATIONS 的名字、字段、顺序；
* 决策表：``recommend_from_flags`` 逐格钉，每个 RECOMMENDATIONS 值至少一条正例、优先级各一条；
* 真前台（FrontDesk + evaluate.fixture_ports）造数据：每个建议值端到端至少一条；
* event_log：恰好一行 CsConferenceHeld，归属三元组、detail 键集照契约，哨兵原文一个都不进；
* 零模型、零工具、不写 cs_ 表；convene 永不抛（降级卡）；
* router：投到同一个 handoff_target，投递规则照 ``_cs_deliver``；会诊失败客户回话逐字不变；
  原因不在集合 / cs=None 时一个会诊都不开。
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from maos.core.store import SqliteStore
from maos.domain.cs import conversation, evaluate, objects, records
from maos.domain.cs.corpus import seed_cs_kb
from maos.domain.cs.desk import CsConfig, FrontDesk, render_card_text
from maos.domain.cs.evaluate import (
    EvalCase, EvalExpect, EvalFixtures, FixtureBinding, FixtureOrder,
)
from maos.domain.cs.ports import EMOTION_ANGRY, SLOT_EMOTION, SLOT_SOURCE_MODEL, PrecheckResult
from maos.domain.cs.types import (
    CONFERENCE_REASONS, EVENT_CONFERENCE_HELD, EVENT_REPLY_REJECTED, HANDOFF_REASONS,
    ROUTE_ANSWER, ROUTE_HANDOFF,
    STAGE_ACTIVE, DeskResult, HandoffCard, ReplyDraft, plan_id_for,
)
from maos.ingress import router as R
from maos.ingress.contracts import (
    CHANNEL_FEISHU, CHANNEL_MATRIX, CHANNEL_WECHAT_KF, CHANNEL_WECOM, InboundMessage,
)
from maos.roundtable import cs_conference as C

CLOCK_T178 = "2026-09-25T09:00:00+00:00"
KFID_T178 = "wk_eval"
ROOM_T178 = "oc_room_t178"
#: 哨兵：客户能看到的单号与内部 query_key 故意不同，都不许进 event_log。
DISPLAY_NO_T178 = "Q9Z771"
QUERY_KEY_T178 = "QKSECRET5531"


# ---------------------------------------------------------------------------
# 造数据：真前台 + 夹具端口（照 test_cs_eval_p13_t174 的造法）
# ---------------------------------------------------------------------------
def _case_t178(case_id: str, fixtures: EvalFixtures | None = None) -> EvalCase:
    return EvalCase(id=case_id, turns=("x",), expect=(EvalExpect(route="fallback",
                                                                 intent="unknown"),),
                    fixtures=fixtures)


def _desk_t178(case: EvalCase, *, store=None, target=None) -> FrontDesk:
    if store is None:
        store = SqliteStore(":memory:")
        seed_cs_kb(store)
    ports = evaluate.fixture_ports(case, tenant_id="tnt-demo")
    return FrontDesk(store, CsConfig(tenants={KFID_T178: "tnt-demo"}, handoff_target=target),
                     clock=lambda: CLOCK_T178, **ports)


def _say_t178(desk: FrontDesk, case: EvalCase, *texts: str) -> list[DeskResult]:
    return [desk.handle(evaluate.inbound_for(case, i, t)) for i, t in enumerate(texts, start=1)]


def _refund_fixtures_t178(*, ok: bool = True) -> EvalFixtures:
    pre = (PrecheckResult(ok=True, decision="approve", rule_ref="R-7D",
                          reason_code="no_reason_return",
                          command_line=f"/refund {QUERY_KEY_T178} no_reason_return",
                          summary="七天内、未超窗口") if ok
           else PrecheckResult(ok=False, refused_why="order_not_in_ledger"))
    return EvalFixtures(
        bindings=(FixtureBinding(DISPLAY_NO_T178, "demo-orders", QUERY_KEY_T178),),
        orders=((QUERY_KEY_T178, FixtureOrder(outcome="ok", status="shipped")),),
        precheck=((QUERY_KEY_T178, pre),))


def _held_rows_t178(store, conversation_id: str) -> list[dict]:
    return [r for r in store.list_event_log(plan_id_for(conversation_id))
            if r["event_type"] == EVENT_CONFERENCE_HELD]


def _cs_tables_t178(store) -> dict[str, list]:
    names = [r["name"] for r in objects.query(
        store, "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'cs_%'"
               " ORDER BY name")]
    return {n: [tuple(dict(r).items()) for r in objects.query(store, f"SELECT * FROM {n}")]
            for n in names}


def _seat_t178(card, seat: str):
    (found,) = [s for s in card.seats if s.seat == seat]
    return found


# ---------------------------------------------------------------------------
# 契约面
# ---------------------------------------------------------------------------
def test_contract_surface_t178():
    assert C.SEATS == ("intake", "order", "policy", "risk")
    assert C.RECOMMENDATIONS == ("send_refund_command", "supervisor_review", "callback_soothe",
                                 "verify_identity", "manual_lookup", "standard_followup")
    assert [f.name for f in dataclasses.fields(C.SeatFinding)] == [
        "seat", "summary", "basis_refs", "flags"]
    assert [f.name for f in dataclasses.fields(C.ConferenceCard)] == [
        "tenant_id", "conversation_id", "turn_id", "handoff_id", "reason", "seats",
        "recommendation", "open_questions"]
    assert C.SeatFinding("risk", "s", ()).flags == ()
    with pytest.raises(dataclasses.FrozenInstanceError):
        C.SeatFinding("risk", "s", ()).seat = "x"          # type: ignore[misc]
    assert set(C.SEAT_FLAGS) == set(C.SEATS)
    assert CONFERENCE_REASONS <= set(HANDOFF_REASONS)


# ---------------------------------------------------------------------------
# 决策表：逐格
# ---------------------------------------------------------------------------
TABLE_T178 = [
    # 每条规则的正例
    ((C.FLAG_COMPENSATION,), "supervisor_review"),
    ((C.FLAG_EXPOSURE,), "supervisor_review"),
    ((C.FLAG_SEAT_ERROR,), "manual_lookup"),
    ((C.FLAG_REFUND_COMMAND_READY,), "send_refund_command"),
    ((C.FLAG_ORDER_UNVERIFIED,), "verify_identity"),
    ((C.FLAG_LOOKUP_FAILED,), "manual_lookup"),
    ((C.FLAG_STATUS_UNMAPPED,), "manual_lookup"),
    ((C.FLAG_REFUND_REFUSED,), "manual_lookup"),
    ((C.FLAG_ANGER,), "callback_soothe"),
    ((C.FLAG_EMOTION_ANGRY,), "callback_soothe"),
    ((C.FLAG_FALLBACK_STREAK,), "callback_soothe"),
    ((), "standard_followup"),
    ((C.FLAG_COMPLAINT, C.FLAG_ORDER_NO_MISSING, C.FLAG_PROBLEM_MISSING, C.FLAG_LANG_EN,
      C.FLAG_NOT_LOOKED_UP, C.FLAG_OBSERVED, C.FLAG_SCRIPTS_CITED, C.FLAG_CLARIFY_ASKED,
      C.FLAG_REPLY_REJECTED), "standard_followup"),
    # 优先级：上一条压下一条
    ((C.FLAG_EXPOSURE, C.FLAG_SEAT_ERROR, C.FLAG_REFUND_COMMAND_READY), "supervisor_review"),
    ((C.FLAG_SEAT_ERROR, C.FLAG_REFUND_COMMAND_READY), "manual_lookup"),
    ((C.FLAG_ORDER_UNVERIFIED, C.FLAG_REFUND_COMMAND_READY, C.FLAG_ANGER), "verify_identity"),
    ((C.FLAG_REFUND_COMMAND_READY, C.FLAG_LOOKUP_FAILED, C.FLAG_ANGER), "send_refund_command"),
    ((C.FLAG_ORDER_UNVERIFIED, C.FLAG_LOOKUP_FAILED), "verify_identity"),
    ((C.FLAG_LOOKUP_FAILED, C.FLAG_ANGER, C.FLAG_EMOTION_ANGRY), "manual_lookup"),
    ((C.FLAG_ANGER, C.FLAG_COMPLAINT), "callback_soothe"),
]


@pytest.mark.parametrize("flags,expected", TABLE_T178)
def test_decision_table_cells_t178(flags, expected):
    assert C.recommend_from_flags(flags) == expected
    assert C.recommend_from_flags(reversed(flags)) == expected      # 与 flags 顺序无关


def test_decision_table_covers_every_recommendation_t178():
    assert {want for _, want in TABLE_T178} == set(C.RECOMMENDATIONS)
    every = {f for flags in C.SEAT_FLAGS.values() for f in flags}
    assert {f for flags, _ in TABLE_T178 for f in flags} == every


# ---------------------------------------------------------------------------
# should_convene
# ---------------------------------------------------------------------------
def _result_t178(route: str, reason: str, *, card: bool = True) -> DeskResult:
    hc = HandoffCard(handoff_id="c-t0001", tenant_id="t", conversation_id="c", turn_id="c-t0001",
                     channel=CHANNEL_WECHAT_KF, reason=reason, intent="complaint",
                     customer_ref="…", customer_text="", recent_turns=(), suggestion="")
    return DeskResult(reply_text="r", tenant_id="t", conversation_id="c", turn_id="c-t0001",
                      route=route, intent="complaint", draft=ReplyDraft(text="r"),
                      handoff=hc if card else None, handoff_reason=reason)


@pytest.mark.parametrize("reason", sorted(HANDOFF_REASONS))
def test_should_convene_only_for_conference_reasons_with_a_card_t178(reason):
    assert C.should_convene(_result_t178(ROUTE_HANDOFF, reason)) is (reason in CONFERENCE_REASONS)
    assert C.should_convene(_result_t178(ROUTE_HANDOFF, reason, card=False)) is False
    assert C.should_convene(_result_t178(ROUTE_ANSWER, reason)) is False


def test_should_convene_never_raises_on_odd_objects_t178():
    assert C.should_convene(None) is False                    # type: ignore[arg-type]
    assert C.should_convene(object()) is False               # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 端到端：真前台造数据，每个建议值至少一条
# ---------------------------------------------------------------------------
def test_refund_bridge_ok_recommends_send_refund_command_t178():
    case = _case_t178("T178-REFUND", _refund_fixtures_t178())
    desk = _desk_t178(case)
    (res,) = _say_t178(desk, case, f"{DISPLAY_NO_T178} 这件外套尺码不合适，我要退货")
    assert res.handoff_reason == "refund_request" and C.should_convene(res)
    card = C.convene(desk.store, res, now=CLOCK_T178)
    assert [s.seat for s in card.seats] == list(C.SEATS)
    assert card.recommendation == "send_refund_command"
    assert (card.tenant_id, card.conversation_id, card.turn_id, card.handoff_id, card.reason) == (
        "tnt-demo", res.conversation_id, res.turn_id, res.handoff.handoff_id, "refund_request")
    order, policy = _seat_t178(card, "order"), _seat_t178(card, "policy")
    assert C.FLAG_OBSERVED in order.flags and C.FLAG_NOT_LOOKED_UP not in order.flags
    assert f"obs:{res.turn_id}-o01" in order.basis_refs
    assert C.FLAG_REFUND_COMMAND_READY in policy.flags
    assert f"bridge:{res.turn_id}" in policy.basis_refs
    # 有 command_line 就原样带上（只对内）
    assert f"/refund {QUERY_KEY_T178} no_reason_return" in policy.summary
    assert "R-7D" in policy.summary
    assert f"/refund {QUERY_KEY_T178} no_reason_return" in C.render_conference_text(card)


def test_refund_bridge_refused_is_not_a_conference_but_the_seat_reads_it_t178():
    # 预检不 ok → needs_order_lookup，不开会；但 policy 座的读法在直接会诊时照样认得出拒绝。
    case = _case_t178("T178-REFUSED", _refund_fixtures_t178(ok=False))
    desk = _desk_t178(case)
    (res,) = _say_t178(desk, case, f"{DISPLAY_NO_T178} 这件外套尺码不合适，我要退货")
    assert res.handoff_reason == "needs_order_lookup" and not C.should_convene(res)
    card = C.convene(desk.store, res, now=CLOCK_T178)
    policy = _seat_t178(card, "policy")
    assert C.FLAG_REFUND_REFUSED in policy.flags and "order_not_in_ledger" in policy.summary
    assert card.recommendation == "manual_lookup"
    # 复核 L2-2：预检被拒要落成一条待补问
    assert any("退款预检未通过" in q and "order_not_in_ledger" in q for q in card.open_questions)


def test_exposure_complaint_recommends_supervisor_review_t178():
    case = _case_t178("T178-EXPOSE")
    desk = _desk_t178(case)
    (res,) = _say_t178(desk, case, "再不处理我就去12315投诉你们")
    assert res.handoff_reason == "complaint" and C.should_convene(res)
    card = C.convene(desk.store, res, now=CLOCK_T178)
    risk = _seat_t178(card, "risk")
    assert {C.FLAG_COMPLAINT, C.FLAG_EXPOSURE} <= set(risk.flags)
    assert card.recommendation == "supervisor_review"
    order = _seat_t178(card, "order")
    assert "未查单" in order.summary and C.FLAG_NOT_LOOKED_UP in order.flags


def test_compensation_recommends_supervisor_review_t178():
    case = _case_t178("T178-COMP")
    desk = _desk_t178(case)
    (res,) = _say_t178(desk, case, "你们必须赔偿我的损失")
    assert res.handoff_reason == "compensation" and C.should_convene(res)
    card = C.convene(desk.store, res, now=CLOCK_T178)
    assert C.FLAG_COMPENSATION in _seat_t178(card, "risk").flags
    assert card.recommendation == "supervisor_review"
    assert any("赔偿" in q for q in card.open_questions)


def test_anger_recommends_callback_soothe_t178():
    case = _case_t178("T178-ANGER")
    desk = _desk_t178(case)
    (res,) = _say_t178(desk, case, "你们这服务太垃圾了！！！")
    assert res.handoff_reason == "anger" and C.should_convene(res)
    card = C.convene(desk.store, res, now=CLOCK_T178)
    assert C.FLAG_ANGER in _seat_t178(card, "risk").flags
    assert card.recommendation == "callback_soothe"


def test_plain_complaint_recommends_standard_followup_t178():
    case = _case_t178("T178-PLAIN")
    desk = _desk_t178(case)
    (res,) = _say_t178(desk, case, "我要投诉你们")
    card = C.convene(desk.store, res, now=CLOCK_T178)
    assert card.recommendation == "standard_followup"
    intake = _seat_t178(card, "intake")
    assert {C.FLAG_ORDER_NO_MISSING, C.FLAG_PROBLEM_MISSING} <= set(intake.flags)
    assert "补问订单号" in card.open_questions


def test_order_no_without_verification_recommends_verify_identity_t178():
    fx = EvalFixtures(bindings=(FixtureBinding("B2727", "demo-orders", "B2727"),),
                      orders=(("B2727", FixtureOrder(outcome="ok", status="paid")),))
    case = _case_t178("T178-VERIFY", fx)
    desk = _desk_t178(case)
    first, res = _say_t178(desk, case, "我的订单号是 B2727", "我要投诉你们")
    assert first.route == "fallback" and res.handoff_reason == "complaint"
    card = C.convene(desk.store, res, now=CLOCK_T178)
    order, intake, risk = (_seat_t178(card, s) for s in ("order", "intake", "risk"))
    assert {C.FLAG_NOT_LOOKED_UP, C.FLAG_ORDER_UNVERIFIED} <= set(order.flags)
    assert "slot:order_no" in intake.basis_refs and C.FLAG_ORDER_NO_MISSING not in intake.flags
    assert C.FLAG_FALLBACK_STREAK in risk.flags                  # 上一轮是兜底
    assert card.recommendation == "verify_identity"


def test_failed_lookup_then_complaint_recommends_manual_lookup_t178():
    # 查单失败 → 转人工；人工把会话交还机器人（handed_off → active，STAGE_FLOW 合法）后客户投诉。
    fx = EvalFixtures(bindings=(FixtureBinding("N3131", "demo-orders", "N3131"),))
    case = _case_t178("T178-LOOKUP", fx)
    desk = _desk_t178(case)
    (first,) = _say_t178(desk, case, "我那单 N3131 发了没")
    assert first.handoff_reason == "lookup_failed" and first.lookup_outcome == "not_found"
    conv = conversation.get_conversation(desk.store, "tnt-demo", first.conversation_id)
    conversation.change_stage(desk.store, conv, STAGE_ACTIVE, turn_id=first.turn_id,
                              reason="returned_by_agent")
    res = desk.handle(evaluate.inbound_for(case, 2, "我要投诉你们"))
    assert res.handoff_reason == "complaint"
    card = C.convene(desk.store, res, now=CLOCK_T178)
    order = _seat_t178(card, "order")
    assert C.FLAG_LOOKUP_FAILED in order.flags and f"turn:{first.turn_id}" in order.basis_refs
    assert card.recommendation == "manual_lookup"


def test_english_turn_marks_lang_en_t178():
    case = _case_t178("T178-EN", EvalFixtures())
    desk = _desk_t178(case)
    (res,) = _say_t178(desk, case, "I will complain, this is unacceptable")
    assert res.handoff_reason == "complaint"
    card = C.convene(desk.store, res, now=CLOCK_T178)
    assert C.FLAG_LANG_EN in _seat_t178(card, "intake").flags


# ---------------------------------------------------------------------------
# 座级判据（复核 L2-1 / L2-2 / L2-3 / L2-4 / L2-5）：每个从库里推出来的 flag 都有正例
# ---------------------------------------------------------------------------
def test_risk_reads_the_emotion_slot_t178():
    # 脚本前台不填情绪槽；用 records 的公开写口把它写成 angry（前台的模型抽槽会写同一张表）。
    case = _case_t178("T178-EMO")
    desk = _desk_t178(case)
    (res,) = _say_t178(desk, case, "我要投诉你们")
    plain = C.convene(desk.store, res, now=CLOCK_T178)
    assert C.FLAG_EMOTION_ANGRY not in _seat_t178(plain, "risk").flags
    assert plain.recommendation == "standard_followup"
    conv = conversation.get_conversation(desk.store, "tnt-demo", res.conversation_id)
    records.set_slot(desk.store, conv, key=SLOT_EMOTION, value=EMOTION_ANGRY,
                     turn_id=res.turn_id, source=SLOT_SOURCE_MODEL, now=CLOCK_T178)
    card = C.convene(desk.store, res, now=CLOCK_T178)
    risk = _seat_t178(card, "risk")
    assert C.FLAG_EMOTION_ANGRY in risk.flags and "slot:emotion" in risk.basis_refs
    assert "情绪槽 angry" in risk.summary
    assert card.recommendation == "callback_soothe"


def test_risk_counts_clarify_questions_t178():
    case = _case_t178("T178-ASK", EvalFixtures())
    desk = _desk_t178(case)
    ask, res = _say_t178(desk, case, "帮我看看我的快递到哪了", "我要投诉你们")
    assert ask.route == "clarify" and res.handoff_reason == "complaint"
    risk = _seat_t178(C.convene(desk.store, res, now=CLOCK_T178), "risk")
    assert C.FLAG_CLARIFY_ASKED in risk.flags and "追问 1 次" in risk.summary


def test_risk_counts_rejected_replies_t178():
    case = _case_t178("T178-REJ")
    desk = _desk_t178(case)
    (res,) = _say_t178(desk, case, "我要投诉你们")
    before = _seat_t178(C.convene(desk.store, res, now=CLOCK_T178), "risk")
    assert C.FLAG_REPLY_REJECTED not in before.flags
    desk.store.append_event_log({
        "event_id": "", "trace_id": "", "plan_id": plan_id_for(res.conversation_id),
        "task_id": res.turn_id, "event_type": EVENT_REPLY_REJECTED, "from_state": None,
        "to_state": None, "reason": "", "detail": {}})
    risk = _seat_t178(C.convene(desk.store, res, now=CLOCK_T178), "risk")
    assert C.FLAG_REPLY_REJECTED in risk.flags and "拦下 1 次" in risk.summary


def test_fallback_streak_skips_clarify_turns_t178():
    case = _case_t178("T178-STREAK", EvalFixtures())
    desk = _desk_t178(case)
    fb, ask, res = _say_t178(desk, case, "啊啊啊", "帮我看看我的快递到哪了", "我要投诉你们")
    assert (fb.route, ask.route, res.handoff_reason) == ("fallback", "clarify", "complaint")
    card = C.convene(desk.store, res, now=CLOCK_T178)
    risk = _seat_t178(card, "risk")
    assert C.FLAG_FALLBACK_STREAK in risk.flags and "连续兜底 1 轮" in risk.summary
    assert card.recommendation == "callback_soothe"


def test_compensation_reason_alone_sets_the_claim_flag_t178():
    # 原因是 compensation，但本轮原文里没有任何赔偿词：flag 照样有（原因本身就是依据）。
    case = _case_t178("T178-COMPREASON")
    desk = _desk_t178(case)
    (res,) = _say_t178(desk, case, "我要投诉你们")
    assert "赔" not in res.handoff.customer_text
    forced = dataclasses.replace(res, handoff_reason="compensation")
    card = C.convene(desk.store, forced, now=CLOCK_T178)
    assert C.FLAG_COMPENSATION in _seat_t178(card, "risk").flags
    assert card.recommendation == "supervisor_review"


@pytest.mark.parametrize("text", ["I want to complain, I will sue!",
                                  "I want to complain, see you in court.",
                                  "I want to complain; the media will hear about this, ok?"])
def test_english_exposure_words_match_before_punctuation_t178(text):
    case = _case_t178("T178-SUE")
    desk = _desk_t178(case)
    (res,) = _say_t178(desk, case, text)
    assert res.handoff_reason == "complaint"
    card = C.convene(desk.store, res, now=CLOCK_T178)
    assert C.FLAG_EXPOSURE in _seat_t178(card, "risk").flags
    assert card.recommendation == "supervisor_review"


def test_english_exposure_words_are_whole_words_t178():
    assert " sue " in C._norm("I will sue!") and " sue " not in C._norm("I pursue it")
    assert " court " not in C._norm("courtesy please")


def test_policy_lists_cited_scripts_t178():
    case = _case_t178("T178-CITE")
    desk = _desk_t178(case)
    ans, res = _say_t178(desk, case, "你们一般下单后多久能发货呀", "我要投诉你们")
    assert ans.route == "answer" and ans.draft.citations
    card = C.convene(desk.store, res, now=CLOCK_T178)
    policy = _seat_t178(card, "policy")
    assert C.FLAG_SCRIPTS_CITED in policy.flags
    assert [f"kb:{c}" for c in ans.draft.citations] == [
        r for r in policy.basis_refs if r.startswith("kb:")]
    assert all(c in policy.summary for c in ans.draft.citations)


def test_policy_tolerates_bad_draft_json_t178():
    assert C._citations_of("{not json") == []
    assert C._citations_of(None) == []
    assert C._citations_of('{"citations": "kb-x"}') == []       # 不是列表：不认成逐字
    assert C._citations_of('[1, 2]') == []
    assert C._citations_of('{"citations": ["kb-a", 3, "", "kb-b"]}') == ["kb-a", "kb-b"]
    case = _case_t178("T178-BADJSON")
    desk = _desk_t178(case)
    ans, res = _say_t178(desk, case, "你们一般下单后多久能发货呀", "我要投诉你们")
    objects.execute(desk.store, "UPDATE cs_turn SET draft_json=? WHERE turn_id=?",
                    ("{broken", ans.turn_id))
    policy = _seat_t178(C.convene(desk.store, res, now=CLOCK_T178), "policy")
    assert C.FLAG_SEAT_ERROR not in policy.flags and C.FLAG_SCRIPTS_CITED not in policy.flags


def _two_bridge_fixtures_t178() -> EvalFixtures:
    refused = PrecheckResult(ok=False, refused_why="order_not_in_ledger")
    ok = PrecheckResult(ok=True, decision="approve", rule_ref="R-7D",
                        reason_code="no_reason_return",
                        command_line="/refund QKB2222 no_reason_return", summary="ok")
    return EvalFixtures(
        bindings=(FixtureBinding("A1111", "demo-orders", "QKA1111"),
                  FixtureBinding("A2222", "demo-orders", "QKB2222")),
        orders=(("QKA1111", FixtureOrder(outcome="ok", status="shipped")),
                ("QKB2222", FixtureOrder(outcome="ok", status="shipped"))),
        precheck=(("QKA1111", refused), ("QKB2222", ok)))


def test_policy_reads_the_latest_bridge_t178():
    # 先一单预检被拒（needs_order_lookup，转人工），人工交还后另一单预检通过：读后一行。
    case = _case_t178("T178-2BRIDGE", _two_bridge_fixtures_t178())
    desk = _desk_t178(case)
    (first,) = _say_t178(desk, case, "A1111 这件外套尺码不合适，我要退货")
    assert first.handoff_reason == "needs_order_lookup"
    conv = conversation.get_conversation(desk.store, "tnt-demo", first.conversation_id)
    conversation.change_stage(desk.store, conv, STAGE_ACTIVE, turn_id=first.turn_id,
                              reason="returned_by_agent")
    res = desk.handle(evaluate.inbound_for(case, 2, "A2222 这件外套尺码不合适，我要退货"))
    assert res.handoff_reason == "refund_request"
    card = C.convene(desk.store, res, now=CLOCK_T178)
    policy = _seat_t178(card, "policy")
    assert [r for r in policy.basis_refs if r.startswith("bridge:")] == [f"bridge:{res.turn_id}"]
    assert C.FLAG_REFUND_COMMAND_READY in policy.flags
    assert C.FLAG_REFUND_REFUSED not in policy.flags
    assert card.recommendation == "send_refund_command"


def test_switching_to_an_unverified_order_recommends_verify_identity_t178():
    # 复核 L2-3：A 单核验过、查过；客户改报 B 单没过核验 → 按当前单号认，B 未核验。
    fx = EvalFixtures(bindings=(FixtureBinding("B2727", "demo-orders", "B2727"),),
                      orders=(("B2727", FixtureOrder(outcome="ok", status="paid")),))
    case = _case_t178("T178-SWITCH", fx)
    desk = _desk_t178(case)
    first, second = _say_t178(desk, case, "我那单 B2727 发了没", "我另一单 X9999 发了没")
    assert (first.lookup_outcome, second.handoff_reason) == ("ok", "identity_unverified")
    conv = conversation.get_conversation(desk.store, "tnt-demo", first.conversation_id)
    conversation.change_stage(desk.store, conv, STAGE_ACTIVE, turn_id=second.turn_id,
                              reason="returned_by_agent")
    res = desk.handle(evaluate.inbound_for(case, 3, "我要投诉你们"))
    assert res.handoff_reason == "complaint"
    card = C.convene(desk.store, res, now=CLOCK_T178)
    order = _seat_t178(card, "order")
    assert {C.FLAG_OBSERVED, C.FLAG_ORDER_UNVERIFIED} <= set(order.flags)
    assert C.FLAG_NOT_LOOKED_UP not in order.flags
    assert card.recommendation == "verify_identity"


def test_verified_order_is_not_flagged_unverified_t178():
    fx = EvalFixtures(bindings=(FixtureBinding("B2727", "demo-orders", "B2727"),),
                      orders=(("B2727", FixtureOrder(outcome="ok", status="paid")),))
    case = _case_t178("T178-VERIFIED", fx)
    desk = _desk_t178(case)
    first, res = _say_t178(desk, case, "我那单 B2727 发了没", "我要投诉你们")
    assert first.lookup_outcome == "ok" and res.handoff_reason == "complaint"
    card = C.convene(desk.store, res, now=CLOCK_T178)
    assert C.FLAG_ORDER_UNVERIFIED not in _seat_t178(card, "order").flags
    assert card.recommendation == "standard_followup"


def _facts_t178(turns, *, ext, slots, slot_turns, turn_id):
    return C._Facts(tenant_id="t", conversation_id="c", turn_id=turn_id,
                    turns=[{"turn_id": tid, "inbound_text": txt} for tid, txt in turns],
                    ext=ext, slots=slots, slot_turns=slot_turns)


def test_order_number_is_matched_as_a_whole_token_t178():
    # 复核 L2-1：查过 123456 不等于 12345 核验过；原文里整号出现才算。
    turns = [("c-t0001", "订单 123456 到哪了"), ("c-t0002", "不对，是 12345"),
             ("c-t0003", "我要投诉")]
    ext = {"c-t0001": {"lookup_outcome": "ok"}}
    prefix = _facts_t178(turns, ext=ext, slots={"order_no": "12345"},
                         slot_turns={"order_no": "c-t0002"}, turn_id="c-t0003")
    assert C._current_order_verified(prefix, prefix.turns) is False
    same = _facts_t178(turns, ext=ext, slots={"order_no": "123456"},
                       slot_turns={"order_no": "c-t0002"}, turn_id="c-t0003")
    assert C._current_order_verified(same, same.turns) is True
    glued = _facts_t178([("c-t0001", "订单123456到哪了")], ext=ext,
                        slots={"order_no": "123456"}, slot_turns={}, turn_id="c-t0001")
    assert C._current_order_verified(glued, glued.turns) is True    # 紧挨汉字照认
    suffix = _facts_t178([("c-t0001", "订单 A123456 到哪了")], ext=ext,
                         slots={"order_no": "123456"}, slot_turns={}, turn_id="c-t0001")
    assert C._current_order_verified(suffix, suffix.turns) is False


def test_ready_bridge_for_an_old_order_yields_to_verify_identity_t178():
    # 复核 L3-2：旧单退款桥 ok，客户改报一个没过核验的单 → 先核验，不据旧桥发命令。
    case = _case_t178("T178-OLDBRIDGE", _refund_fixtures_t178())
    desk = _desk_t178(case)
    (first,) = _say_t178(desk, case, f"{DISPLAY_NO_T178} 这件外套尺码不合适，我要退货")
    assert first.handoff_reason == "refund_request"
    conv = conversation.get_conversation(desk.store, "tnt-demo", first.conversation_id)
    conversation.change_stage(desk.store, conv, STAGE_ACTIVE, turn_id=first.turn_id,
                              reason="returned_by_agent")
    second = desk.handle(evaluate.inbound_for(case, 2, "我另一单 X9999 发了没"))
    assert second.handoff_reason == "identity_unverified"
    conv = conversation.get_conversation(desk.store, "tnt-demo", first.conversation_id)
    conversation.change_stage(desk.store, conv, STAGE_ACTIVE, turn_id=second.turn_id,
                              reason="returned_by_agent")
    res = desk.handle(evaluate.inbound_for(case, 3, "我要投诉你们"))
    assert res.handoff_reason == "complaint"
    card = C.convene(desk.store, res, now=CLOCK_T178)
    assert C.FLAG_REFUND_COMMAND_READY in _seat_t178(card, "policy").flags
    assert C.FLAG_ORDER_UNVERIFIED in _seat_t178(card, "order").flags
    assert card.recommendation == "verify_identity"


def test_later_turns_do_not_count_when_convening_an_earlier_turn_t178():
    # 复核 L2-3：会诊的是第 1 轮，第 2 轮（查单、观察、被拦的回复）四座都不许算进来。
    fx = EvalFixtures(bindings=(FixtureBinding("B2727", "demo-orders", "B2727"),),
                      orders=(("B2727", FixtureOrder(outcome="ok", status="paid")),))
    case = _case_t178("T178-UPTO", fx)
    desk = _desk_t178(case)
    (first,) = _say_t178(desk, case, "我要投诉你们")
    assert first.handoff_reason == "complaint"
    conv = conversation.get_conversation(desk.store, "tnt-demo", first.conversation_id)
    conversation.change_stage(desk.store, conv, STAGE_ACTIVE, turn_id=first.turn_id,
                              reason="returned_by_agent")
    later = desk.handle(evaluate.inbound_for(case, 2, "我那单 B2727 发了没"))
    assert later.lookup_outcome == "ok"
    desk.store.append_event_log({
        "event_id": "", "trace_id": "", "plan_id": plan_id_for(first.conversation_id),
        "task_id": later.turn_id, "event_type": EVENT_REPLY_REJECTED, "from_state": None,
        "to_state": None, "reason": "", "detail": {}})
    card = C.convene(desk.store, first, now=CLOCK_T178)
    order, risk, intake = (_seat_t178(card, s) for s in ("order", "risk", "intake"))
    assert C.FLAG_NOT_LOOKED_UP in order.flags
    assert C.FLAG_OBSERVED not in order.flags
    assert not any(r.startswith("obs:") or r == f"turn:{later.turn_id}" for r in order.basis_refs)
    assert C.FLAG_REPLY_REJECTED not in risk.flags and "拦下 0 次" in risk.summary
    assert "共 1 轮" in intake.summary
    # 对照：会诊第 2 轮时，这些都算
    now_card = C.convene(desk.store, later, now=CLOCK_T178)
    assert C.FLAG_OBSERVED in _seat_t178(now_card, "order").flags
    assert C.FLAG_REPLY_REJECTED in _seat_t178(now_card, "risk").flags


# ---------------------------------------------------------------------------
# event_log：一行、归属、detail 键集、哨兵
# ---------------------------------------------------------------------------
def test_event_log_row_shape_and_no_sentinels_t178():
    case = _case_t178("T178-SENTINEL", _refund_fixtures_t178())
    desk = _desk_t178(case)
    first_text = "这件外套尺码不合适 哨兵原文甲"
    t1, res = _say_t178(desk, case, first_text, f"{DISPLAY_NO_T178} 我要退货")
    assert res.handoff_reason == "refund_request"
    assert _held_rows_t178(desk.store, res.conversation_id) == []
    card = C.convene(desk.store, res, now=CLOCK_T178)
    (row,) = _held_rows_t178(desk.store, res.conversation_id)
    assert (row["plan_id"], row["task_id"], row["trace_id"]) == (
        f"cs:{res.conversation_id}", res.turn_id, "")
    detail = row["detail"]
    assert set(detail) == {"handoff_id", "reason", "seats", "recommendation",
                           "open_question_count"}
    assert [set(s) for s in detail["seats"]] == [{"seat", "basis_refs", "flags"}] * 4
    assert [s["seat"] for s in detail["seats"]] == list(C.SEATS)
    assert detail["recommendation"] == card.recommendation == "send_refund_command"
    assert detail["open_question_count"] == len(card.open_questions)
    assert detail["handoff_id"] == res.handoff.handoff_id and detail["reason"] == "refund_request"
    blob = json.dumps(row, ensure_ascii=False)
    assert res.reply_text and t1.reply_text
    for sentinel in (first_text, "哨兵原文甲", case.customer, DISPLAY_NO_T178, QUERY_KEY_T178,
                     "/refund", "R-7D", "七天内", KFID_T178, res.reply_text, t1.reply_text):
        assert sentinel not in blob, sentinel
    for seat in card.seats:
        assert seat.summary not in blob
    for q in card.open_questions:
        assert q not in blob


def test_event_log_open_question_count_is_real_t178():
    # 复核 L2-2：待补问非空时 detail 的计数必须跟卡对得上（0 == 0 不算数）。
    case = _case_t178("T178-ASKCOUNT")
    desk = _desk_t178(case)
    (res,) = _say_t178(desk, case, "我要投诉你们 哨兵原文乙")
    card = C.convene(desk.store, res, now=CLOCK_T178)
    assert len(card.open_questions) >= 2 and "补问订单号" in card.open_questions
    (row,) = _held_rows_t178(desk.store, res.conversation_id)
    assert row["detail"]["open_question_count"] == len(card.open_questions)
    blob = json.dumps(row, ensure_ascii=False)
    for sentinel in ("哨兵原文乙", case.customer, KFID_T178, res.reply_text, *card.open_questions):
        assert sentinel not in blob, sentinel


def test_zero_model_zero_tool_and_no_cs_writes_t178():
    case = _case_t178("T178-PURE", _refund_fixtures_t178())
    desk = _desk_t178(case)
    (res,) = _say_t178(desk, case, f"{DISPLAY_NO_T178} 这件外套尺码不合适，我要退货")
    store = desk.store
    before_tables = _cs_tables_t178(store)
    before_log = store.list_event_log(plan_id_for(res.conversation_id))
    before_usage = store._conn.execute("SELECT COUNT(*) FROM model_usage").fetchone()[0]
    C.convene(store, res, now=CLOCK_T178)
    assert _cs_tables_t178(store) == before_tables
    after_log = store.list_event_log(plan_id_for(res.conversation_id))
    added = after_log[len(before_log):]
    assert [r["event_type"] for r in added] == [EVENT_CONFERENCE_HELD]
    assert store._conn.execute("SELECT COUNT(*) FROM model_usage").fetchone()[0] == before_usage


# ---------------------------------------------------------------------------
# 永不抛：降级卡
# ---------------------------------------------------------------------------
class _BrokenStore_t178:
    """什么都读不了、什么都写不进的库。"""

    def __getattr__(self, name):
        raise RuntimeError("store down")


def test_convene_never_raises_on_a_broken_store_t178():
    res = _result_t178(ROUTE_HANDOFF, "complaint")
    card = C.convene(_BrokenStore_t178(), res, now=CLOCK_T178)
    assert [s.seat for s in card.seats] == list(C.SEATS)
    assert all(s.flags == (C.FLAG_SEAT_ERROR,) for s in card.seats)
    assert card.recommendation == "manual_lookup"
    assert card.reason == "complaint" and card.handoff_id == "c-t0001"
    assert "RuntimeError" in card.seats[0].summary and "store down" not in card.seats[0].summary


def test_convene_degrades_when_the_turn_is_not_in_the_store_t178():
    store = SqliteStore(":memory:")
    seed_cs_kb(store)
    store.init_schema()
    objects.ensure_schema(store)
    card = C.convene(store, _result_t178(ROUTE_HANDOFF, "anger"), now=CLOCK_T178)
    assert all(C.FLAG_SEAT_ERROR in s.flags for s in card.seats)
    assert card.recommendation == "manual_lookup"
    assert len(_held_rows_t178(store, "c")) == 1                   # 降级卡照样留一行审计


def test_one_seat_failing_does_not_take_down_the_others_t178(monkeypatch):
    case = _case_t178("T178-ONESEAT")
    desk = _desk_t178(case)
    (res,) = _say_t178(desk, case, "我要投诉你们")
    monkeypatch.setattr(desk.store, "list_event_log", lambda *a, **k: 1 / 0)
    monkeypatch.setattr(desk.store, "append_event_log", lambda *a, **k: 1 / 0)
    card = C.convene(desk.store, res, now=CLOCK_T178)
    assert _seat_t178(card, "risk").flags == (C.FLAG_SEAT_ERROR,)
    assert C.FLAG_SEAT_ERROR not in _seat_t178(card, "intake").flags
    assert card.recommendation == "manual_lookup"


def test_render_conference_text_lists_all_seats_in_order_t178():
    case = _case_t178("T178-RENDER")
    desk = _desk_t178(case)
    (res,) = _say_t178(desk, case, "再不处理我就去12315投诉你们")
    text = C.render_conference_text(C.convene(desk.store, res, now=CLOCK_T178))
    pos = [text.index(f"[{s}]") for s in C.SEATS]
    assert pos == sorted(pos)
    assert "supervisor_review" in text and "接手前待补" in text


# ---------------------------------------------------------------------------
# router
# ---------------------------------------------------------------------------
class _Adapter_t178:
    configured = True

    def __init__(self, name: str, *, boom: bool = False):
        self.name, self.sent, self.boom = name, [], boom

    def send(self, msg) -> None:
        if self.boom and "圆桌会诊" in msg.text:
            raise RuntimeError("room down")
        self.sent.append(msg)


def _router_t178(desk, *, adapters=None) -> R.IngressRouter:
    if adapters is None:
        adapters = {n: _Adapter_t178(n) for n in (CHANNEL_FEISHU, CHANNEL_WECOM,
                                                   CHANNEL_WECHAT_KF, CHANNEL_MATRIX)}
    return R.IngressRouter(adapters, store=desk.store if desk is not None else _bare_store_t178(),
                           cs=desk)


def _msg_t178(text: str, *, user: str = "wm_t178_customer", msg_id: str = "m-1") -> InboundMessage:
    return InboundMessage(channel=CHANNEL_WECHAT_KF, chat_id=user, sender=user, text=text,
                          msg_id=msg_id, raw={"open_kfid": KFID_T178})


def _real_desk_t178(target=(CHANNEL_FEISHU, ROOM_T178)) -> FrontDesk:
    store = SqliteStore(":memory:")
    seed_cs_kb(store)
    return FrontDesk(store, CsConfig(tenants={KFID_T178: "tnt-demo"}, handoff_target=target),
                     clock=lambda: CLOCK_T178)


def _turn_row_t178(store) -> dict:
    (row,) = objects.query(store, "SELECT conversation_id, turn_id, reply_text FROM cs_turn")
    return dict(row)


def test_router_delivers_conference_after_the_handoff_card_t178():
    desk = _real_desk_t178()
    router = _router_t178(desk)
    out = router.handle(_msg_t178("再不处理我就去12315投诉你们"))
    row = _turn_row_t178(desk.store)
    assert out == row["reply_text"] and out
    assert [m.text for m in router.adapters[CHANNEL_WECHAT_KF].sent] == [out]
    room = router.adapters[CHANNEL_FEISHU].sent
    assert [m.chat_id for m in room] == [ROOM_T178, ROOM_T178]
    (card_json,) = [r["card_json"] for r in objects.query(desk.store,
                                                          "SELECT card_json FROM cs_handoff")]
    assert room[0].text == render_card_text(HandoffCard.from_json(json.loads(card_json)))
    assert room[1].text.startswith("【圆桌会诊】") and "supervisor_review" in room[1].text
    assert len(_held_rows_t178(desk.store, row["conversation_id"])) == 1
    for name in (CHANNEL_WECOM, CHANNEL_MATRIX):
        assert router.adapters[name].sent == []


def test_router_conference_failure_leaves_customer_reply_untouched_t178(monkeypatch):
    baseline_desk = _real_desk_t178()
    baseline = _router_t178(baseline_desk)
    want = baseline.handle(_msg_t178("你们必须赔偿我的损失"))

    desk = _real_desk_t178()
    router = _router_t178(desk)

    def boom(*a, **k):
        raise RuntimeError("conference down")

    monkeypatch.setattr(C, "convene", boom)
    out = router.handle(_msg_t178("你们必须赔偿我的损失"))
    assert out == want
    assert [m.text for m in router.adapters[CHANNEL_WECHAT_KF].sent] == [want]
    room = router.adapters[CHANNEL_FEISHU].sent
    assert len(room) == 1 and "圆桌会诊" not in room[0].text           # 转人工卡照投
    (d,) = objects.query(desk.store, "SELECT delivery FROM cs_handoff")
    assert d["delivery"] == "delivered"


def test_router_conference_send_failure_is_only_logged_t178():
    desk = _real_desk_t178()
    adapters = {n: _Adapter_t178(n, boom=(n == CHANNEL_FEISHU))
                for n in (CHANNEL_FEISHU, CHANNEL_WECHAT_KF)}
    router = _router_t178(desk, adapters=adapters)
    out = router.handle(_msg_t178("你们这服务太垃圾了！！！"))
    assert out and [m.text for m in adapters[CHANNEL_WECHAT_KF].sent] == [out]
    assert len(adapters[CHANNEL_FEISHU].sent) == 1                   # 只有转人工卡


@pytest.mark.parametrize("target,present", [
    (None, (CHANNEL_FEISHU,)),                                      # 未配置
    ((CHANNEL_WECHAT_KF, "wm_other"), (CHANNEL_FEISHU, CHANNEL_WECHAT_KF)),   # 外部渠道
    ((CHANNEL_MATRIX, "!room"), (CHANNEL_FEISHU, CHANNEL_WECHAT_KF)),         # 无 adapter
])
def test_router_conference_delivery_rules_follow_cs_deliver_t178(target, present):
    desk = _real_desk_t178(target)
    adapters = {n: _Adapter_t178(n) for n in present}
    adapters.setdefault(CHANNEL_WECHAT_KF, _Adapter_t178(CHANNEL_WECHAT_KF))
    router = _router_t178(desk, adapters=adapters)
    out = router.handle(_msg_t178("我要投诉你们"))
    row = _turn_row_t178(desk.store)
    assert out == row["reply_text"]
    assert [m.text for m in adapters[CHANNEL_WECHAT_KF].sent] == [out]   # 只有客户那句
    for name, a in adapters.items():
        assert not any("圆桌会诊" in m.text for m in a.sent), name
    # 会诊照开（审计行在），只是不发
    assert len(_held_rows_t178(desk.store, row["conversation_id"])) == 1


def test_router_non_conference_reason_does_not_convene_t178(monkeypatch):
    calls = []
    monkeypatch.setattr(C, "convene", lambda *a, **k: calls.append(a))
    desk = _real_desk_t178()
    router = _router_t178(desk)
    out = router.handle(_msg_t178("转人工"))
    row = _turn_row_t178(desk.store)
    assert out == row["reply_text"] and calls == []
    assert len(router.adapters[CHANNEL_FEISHU].sent) == 1
    assert _held_rows_t178(desk.store, row["conversation_id"]) == []


def test_router_without_cs_never_reaches_the_conference_t178(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("cs=None 不许走到会诊")

    monkeypatch.setattr(R.IngressRouter, "_cs_conference", boom)
    monkeypatch.setattr(C, "should_convene", boom)
    router = _router_t178(None)
    assert router.handle(_msg_t178("我要投诉你们")) == ""            # 与 cs=None 从前一致：闲聊不回
    assert all(a.sent == [] for a in router.adapters.values())


def _bare_store_t178() -> SqliteStore:
    s = SqliteStore()
    s.init_schema()
    return s
