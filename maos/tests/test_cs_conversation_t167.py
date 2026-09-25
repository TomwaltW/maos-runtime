"""T167 会话对象的机器验收（二）—— 取或建、取号、落轮次、阶段迁移、转人工卡片、审计行。

跨轨契约 review/p12-cs-contracts.md §1.2 / §1.4 / §2 R5。最要紧的两条：

* 四种 Cs* 事件的条数与归属三元组（plan_id = cs:<会话>、task_id = 轮次、trace_id = ""）；
* **哨兵**：客户原文、回复原文、external_userid、open_kfid 四样东西，在 event_log 的
  任何一行、任何一列里都不出现 —— 同时断言它们确实在会话表里（否则判据空转）。
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import threading

import pytest

from maos.core.store import SqliteStore
from maos.domain.cs import conversation as C
from maos.domain.cs import objects
from maos.domain.cs import types as T

NOW_T167 = "2026-09-24T08:00:00+00:00"
LATER_T167 = "2026-09-24T09:30:00+00:00"


def _store_t167() -> SqliteStore:
    s = SqliteStore()
    s.init_schema()
    return s


def _open_t167(store, *, tenant_id="tnt-demo", channel=T.CHANNEL_WECHAT_KF, open_kfid="wk_1",
               external_userid="wm_customer_1", now=NOW_T167) -> C.Conversation:
    return C.open_conversation(store, tenant_id=tenant_id, channel=channel,
                               open_kfid=open_kfid, external_userid=external_userid, now=now)


def _turn_t167(store, conv, route, *, inbound="你好", reply="您好，请问有什么可以帮您",
               intent=T.INTENT_GENERAL, handoff_reason="", draft=None, check=None, now=None):
    """取号 + 落一轮，返回 (turn_id, 更新后的会话)。"""
    turn_id, seq = C.allocate_turn(store, conv)
    conv = C.record_turn(
        store, conv, turn_id=turn_id, seq=seq, msg_dedup_key=f"ingress:wechat_kf:m{seq}",
        inbound_text=inbound, reply_text=reply, route=route, intent=intent,
        handoff_reason=handoff_reason, draft=draft or T.ReplyDraft(text=reply),
        check=check or T.CheckResult(ok=True), now=now)
    return turn_id, conv


def _events_t167(store, conv, event_type=None) -> list[dict]:
    rows = store.list_event_log(T.plan_id_for(conv.conversation_id))
    return [r for r in rows if event_type is None or r["event_type"] == event_type]


def _card_t167(conv, turn_id, *, reason=T.HANDOFF_REQUESTED, customer_text="我要人工",
               recent=(("你好", "您好"),), suggestion="客户点名要人工，请接手",
               citations=()) -> T.HandoffCard:
    return T.HandoffCard(
        handoff_id=turn_id, tenant_id=conv.tenant_id, conversation_id=conv.conversation_id,
        turn_id=turn_id, channel=conv.channel, reason=reason,
        intent=T.INTENT_HANDOFF_REQUEST, customer_ref=T.mask_customer(conv.external_userid),
        customer_text=customer_text, recent_turns=tuple(recent), suggestion=suggestion,
        citations=tuple(citations), created_at=NOW_T167)


# ------------------------------------------------------------------ 取或建
def test_conversation_fields_mirror_table_columns_t167():
    s = _store_t167()
    objects.ensure_schema(s)
    cols = [r["name"] for r in objects.query(s, "PRAGMA table_info(cs_conversation)")]
    assert [f.name for f in dataclasses.fields(C.Conversation)] == cols
    assert C.Conversation.__dataclass_params__.frozen


def test_open_conversation_creates_active_then_returns_existing_unchanged_t167():
    s = _store_t167()
    conv = _open_t167(s)
    assert conv.conversation_id == T.conversation_id_for(
        "tnt-demo", T.CHANNEL_WECHAT_KF, "wk_1", "wm_customer_1")
    assert (conv.stage, conv.fallback_streak, conv.turn_count) == (T.STAGE_ACTIVE, 0, 0)
    assert conv.opened_at == conv.updated_at == NOW_T167
    assert C.get_conversation(s, "tnt-demo", conv.conversation_id) == conv

    # 已存在：原样返回，换了 now 也不改任何字段。
    again = _open_t167(s, now=LATER_T167)
    assert again == conv
    # 会话往前走了之后再取：返回库里的现状，不被「重建」回初值。
    _turn_t167(s, conv, T.ROUTE_FALLBACK, now=LATER_T167)
    moved = _open_t167(s, now="2026-09-25T00:00:00+00:00")
    assert (moved.fallback_streak, moved.turn_count, moved.updated_at) == (1, 1, LATER_T167)
    assert moved.opened_at == NOW_T167
    assert objects.query(s, "SELECT COUNT(*) AS n FROM cs_conversation") == [{"n": 1}]


def test_same_customer_different_open_kfid_is_two_conversations_t167():
    s = _store_t167()
    a = _open_t167(s, open_kfid="wk_1")
    b = _open_t167(s, open_kfid="wk_2")
    c = _open_t167(s, tenant_id="tnt-b", open_kfid="wk_1")
    assert len({a.conversation_id, b.conversation_id, c.conversation_id}) == 3
    assert (a.open_kfid, b.open_kfid) == ("wk_1", "wk_2")
    assert objects.query(s, "SELECT COUNT(*) AS n FROM cs_conversation") == [{"n": 3}]


def test_empty_tenant_still_opens_a_conversation_t167():
    s = _store_t167()
    conv = _open_t167(s, tenant_id="")
    assert conv.tenant_id == ""
    assert conv.conversation_id == T.conversation_id_for(
        "", T.CHANNEL_WECHAT_KF, "wk_1", "wm_customer_1")
    assert C.get_conversation(s, "", conv.conversation_id) == conv
    turn_id, conv2 = _turn_t167(s, conv, T.ROUTE_HANDOFF, intent=T.INTENT_UNKNOWN,
                                handoff_reason=T.HANDOFF_TENANT_UNMAPPED)
    assert conv2.turn_count == 1
    assert C.get_conversation(s, "tnt-demo", conv.conversation_id) is None


def test_get_conversation_missing_is_none_t167():
    s = _store_t167()
    assert C.get_conversation(s, "tnt-demo", "csc-0000000000000000") is None


# ------------------------------------------------------------------ 取号
def test_allocate_turn_is_sequential_and_uses_db_count_t167():
    s = _store_t167()
    conv = _open_t167(s)
    got = [C.allocate_turn(s, conv) for _ in range(5)]   # conv 始终是 turn_count=0 的旧快照
    assert got == [(T.turn_id_for(conv.conversation_id, n), n) for n in range(1, 6)]
    assert got[0][0] == conv.conversation_id + "-t0001"
    assert C.get_conversation(s, conv.tenant_id, conv.conversation_id).turn_count == 5


def test_allocate_turn_is_atomic_under_threads_t167():
    s = _store_t167()
    conv = _open_t167(s)
    seqs: list[int] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker():
        try:
            for _ in range(30):
                _tid, seq = C.allocate_turn(s, conv)
                with lock:
                    seqs.append(seq)
        except BaseException as exc:                      # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert sorted(seqs) == list(range(1, 241))
    assert C.get_conversation(s, conv.tenant_id, conv.conversation_id).turn_count == 240


def test_allocate_turn_on_unknown_conversation_raises_t167():
    s = _store_t167()
    conv = _open_t167(s)
    ghost = dataclasses.replace(conv, conversation_id="csc-ffffffffffffffff")
    with pytest.raises(LookupError):
        C.allocate_turn(s, ghost)


# ------------------------------------------------------------------ 落轮次
def test_fallback_streak_follows_route_rules_t167():
    """兜底 +1、答上 / 转人工清零、静默不变 —— 按顺序走一遍，每步读回库里的值。"""
    s = _store_t167()
    conv = _open_t167(s)
    plan = [(T.ROUTE_FALLBACK, 1), (T.ROUTE_FALLBACK, 2), (T.ROUTE_SILENT, 2),
            (T.ROUTE_ANSWER, 0), (T.ROUTE_FALLBACK, 1), (T.ROUTE_HANDOFF, 0),
            (T.ROUTE_SILENT, 0), (T.ROUTE_FALLBACK, 1), (T.ROUTE_SILENT, 1)]
    for route, expected in plan:
        reason = T.HANDOFF_REQUESTED if route == T.ROUTE_HANDOFF else ""
        _tid, conv = _turn_t167(s, conv, route, handoff_reason=reason)
        assert conv.fallback_streak == expected, route
        stored = C.get_conversation(s, conv.tenant_id, conv.conversation_id)
        assert stored.fallback_streak == expected


@pytest.mark.parametrize("route,expected", [
    (T.ROUTE_FALLBACK, 4), (T.ROUTE_ANSWER, 0), (T.ROUTE_HANDOFF, 0), (T.ROUTE_SILENT, 3)])
def test_fallback_streak_each_route_from_three_t167(route, expected):
    s = _store_t167()
    conv = _open_t167(s)
    for _ in range(3):
        _tid, conv = _turn_t167(s, conv, T.ROUTE_FALLBACK)
    assert conv.fallback_streak == 3
    reason = T.HANDOFF_REPEATED_FALLBACK if route == T.ROUTE_HANDOFF else ""
    _tid, conv = _turn_t167(s, conv, route, handoff_reason=reason)
    assert conv.fallback_streak == expected


def test_record_turn_persists_row_and_bumps_updated_at_t167():
    s = _store_t167()
    conv = _open_t167(s)
    draft = T.ReplyDraft(text="七天无理由退货的条件如下",
                         claims=(T.Claim(literal="七天无理由", basis_ref="kb:kb-cs-tnt-demo-RET-001"),),
                         citations=("kb-cs-tnt-demo-RET-001",))
    check = T.CheckResult(ok=True)
    turn_id, conv2 = _turn_t167(s, conv, T.ROUTE_ANSWER, inbound="能七天无理由吗",
                                reply=draft.text, intent=T.INTENT_RETURN_EXCHANGE,
                                draft=draft, check=check, now=LATER_T167)
    assert conv2.updated_at == LATER_T167 and conv2.opened_at == NOW_T167
    row = objects.query(s, "SELECT * FROM cs_turn WHERE tenant_id=? AND turn_id=?",
                        (conv.tenant_id, turn_id))[0]
    assert (row["conversation_id"], row["seq"], row["route"], row["intent"],
            row["handoff_reason"], row["msg_dedup_key"], row["created_at"]) == (
        conv.conversation_id, 1, "answer", "return_exchange", "",
        "ingress:wechat_kf:m1", LATER_T167)
    assert (row["inbound_text"], row["reply_text"]) == ("能七天无理由吗", draft.text)
    assert T.ReplyDraft.from_json(json.loads(row["draft_json"])) == draft
    assert json.loads(row["check_json"]) == {"ok": True, "violations": []}


@pytest.mark.parametrize("field,value", [
    ("route", "bogus"), ("handoff_reason", "bogus"), ("intent", "shopping"),
    ("turn_id", "csc-wrong-t0001")])
def test_record_turn_rejects_bad_values_and_writes_nothing_t167(field, value):
    s = _store_t167()
    conv = _open_t167(s)
    turn_id, seq = C.allocate_turn(s, conv)
    kwargs = dict(turn_id=turn_id, seq=seq, msg_dedup_key="", inbound_text="x",
                  reply_text="y", route=T.ROUTE_ANSWER, intent=T.INTENT_GENERAL,
                  handoff_reason="", draft=T.ReplyDraft(text="y"),
                  check=T.CheckResult(ok=True))
    kwargs[field] = value
    with pytest.raises(ValueError):
        C.record_turn(s, conv, **kwargs)
    assert objects.query(s, "SELECT COUNT(*) AS n FROM cs_turn") == [{"n": 0}]
    assert _events_t167(s, conv) == []


def test_record_turn_twice_for_same_turn_raises_without_second_event_t167():
    """同一轮落两次：第二次撞 cs_turn 主键抛错，不重复 +1、不重复落 CsTurnRecorded。

    这条只钉「撞主键不重复记账」。插入在更新之前，撞主键时更新根本没执行，所以它
    测不出有没有事务 —— 事务由下一条（更新失败时插入也要回滚）钉。
    """
    s = _store_t167()
    conv = _open_t167(s)
    turn_id, seq = C.allocate_turn(s, conv)
    args = dict(turn_id=turn_id, seq=seq, msg_dedup_key="", inbound_text="x", reply_text="y",
                route=T.ROUTE_FALLBACK, intent=T.INTENT_UNKNOWN, handoff_reason="",
                draft=T.ReplyDraft(text="y"), check=T.CheckResult(ok=True))
    C.record_turn(s, conv, **args)
    with pytest.raises(sqlite3.IntegrityError):
        C.record_turn(s, conv, **args)
    assert C.get_conversation(s, conv.tenant_id, conv.conversation_id).fallback_streak == 1
    assert objects.query(s, "SELECT COUNT(*) AS n FROM cs_turn") == [{"n": 1}]
    assert len(_events_t167(s, conv, T.EVENT_TURN_RECORDED)) == 1


def test_record_turn_is_atomic_when_conversation_update_fails_t167():
    """插 cs_turn 与改 cs_conversation 同生共死：更新失败时，已插的那一行也要回滚。

    用一个 ``BEFORE UPDATE OF fallback_streak`` 触发器让更新那一句必失败（插入已经
    执行过了）。没有事务、每句自动提交的实现在这里会留下一行孤儿 cs_turn。
    """
    s = _store_t167()
    conv = _open_t167(s)
    _tid, conv = _turn_t167(s, conv, T.ROUTE_FALLBACK, now=NOW_T167)   # streak = 1、一行
    turn_id, seq = C.allocate_turn(s, conv)
    objects.execute(s, "CREATE TRIGGER cs_probe_block_streak_t167"
                       " BEFORE UPDATE OF fallback_streak ON cs_conversation"
                       " BEGIN SELECT RAISE(ABORT, 'probe_t167: update blocked'); END")
    with pytest.raises(sqlite3.IntegrityError, match="probe_t167"):
        C.record_turn(s, conv, turn_id=turn_id, seq=seq, msg_dedup_key="", inbound_text="x",
                      reply_text="y", route=T.ROUTE_FALLBACK, intent=T.INTENT_UNKNOWN,
                      handoff_reason="", draft=T.ReplyDraft(text="y"),
                      check=T.CheckResult(ok=True), now=LATER_T167)
    rows = objects.query(s, "SELECT turn_id FROM cs_turn ORDER BY seq")
    assert [r["turn_id"] for r in rows] == [_tid]                 # 没有孤儿行
    stored = C.get_conversation(s, conv.tenant_id, conv.conversation_id)
    assert (stored.fallback_streak, stored.updated_at) == (1, NOW_T167)
    assert [r["task_id"] for r in _events_t167(s, conv, T.EVENT_TURN_RECORDED)] == [_tid]

    # 连接没有被留在半截事务里：撤掉触发器，同一轮照常落得进去。
    objects.execute(s, "DROP TRIGGER cs_probe_block_streak_t167")
    conv = C.record_turn(s, conv, turn_id=turn_id, seq=seq, msg_dedup_key="", inbound_text="x",
                         reply_text="y", route=T.ROUTE_FALLBACK, intent=T.INTENT_UNKNOWN,
                         handoff_reason="", draft=T.ReplyDraft(text="y"),
                         check=T.CheckResult(ok=True), now=LATER_T167)
    assert (conv.fallback_streak, conv.updated_at) == (2, LATER_T167)
    assert objects.query(s, "SELECT COUNT(*) AS n FROM cs_turn") == [{"n": 2}]


def test_record_turn_streak_uses_db_value_not_stale_snapshot_t167():
    """拿同一个旧快照（streak=0）连落两轮兜底，库里的 streak 必须是 2 —— 否则
    FALLBACK_STREAK_HANDOFF=2 的转人工阈值会被漏掉。"""
    s = _store_t167()
    stale = _open_t167(s)
    for _ in range(2):
        turn_id, seq = C.allocate_turn(s, stale)
        out = C.record_turn(s, stale, turn_id=turn_id, seq=seq, msg_dedup_key="",
                            inbound_text="？", reply_text="兜底", route=T.ROUTE_FALLBACK,
                            intent=T.INTENT_UNKNOWN, handoff_reason="",
                            draft=T.ReplyDraft(text="兜底"), check=T.CheckResult(ok=True))
    assert stale.fallback_streak == 0
    assert out.fallback_streak == 2 == T.FALLBACK_STREAK_HANDOFF
    assert C.get_conversation(s, stale.tenant_id, stale.conversation_id).fallback_streak == 2
    details = [r["detail"] for r in _events_t167(s, stale, T.EVENT_TURN_RECORDED)]
    assert [d["fallback_streak"] for d in details] == [1, 2]


# ------------------------------------------------------------------ 阶段迁移
def _reach_t167(store, conv, stage):
    if stage == T.STAGE_ACTIVE:
        return conv
    return C.change_stage(store, conv, stage, turn_id=conv.conversation_id + "-t0000",
                          reason="setup")


@pytest.mark.parametrize("src", T.STAGES)
@pytest.mark.parametrize("dst", T.STAGES)
def test_stage_flow_every_pair_t167(src, dst):
    s = _store_t167()
    conv = _reach_t167(s, _open_t167(s), src)
    assert conv.stage == src
    before = _events_t167(s, conv, T.EVENT_STAGE_CHANGED)
    turn_id = conv.conversation_id + "-t0001"
    if dst in T.STAGE_FLOW[src]:
        out = C.change_stage(s, conv, dst, turn_id=turn_id, reason=T.HANDOFF_REQUESTED,
                             now=LATER_T167)
        assert out.stage == dst and out.updated_at == LATER_T167
        after = _events_t167(s, conv, T.EVENT_STAGE_CHANGED)
        assert len(after) == len(before) + 1
        ev = after[-1]
        assert (ev["from_state"], ev["to_state"], ev["task_id"]) == (src, dst, turn_id)
        assert (ev["detail"]["from_stage"], ev["detail"]["to_stage"]) == (src, dst)
        assert ev["detail"]["reason"] == T.HANDOFF_REQUESTED
    else:
        with pytest.raises(ValueError):
            C.change_stage(s, conv, dst, turn_id=turn_id, reason=T.HANDOFF_REQUESTED)
        assert C.get_conversation(s, conv.tenant_id, conv.conversation_id).stage == src
        assert len(_events_t167(s, conv, T.EVENT_STAGE_CHANGED)) == len(before)


def test_stage_flow_pairs_cover_the_frozen_table_t167():
    """上一条参数化的「合法对」恰好 4 个（active↔handed_off、两者 → closed），closed 吸收。"""
    legal = {(a, b) for a in T.STAGES for b in T.STAGES if b in T.STAGE_FLOW[a]}
    assert legal == {("active", "handed_off"), ("active", "closed"),
                     ("handed_off", "active"), ("handed_off", "closed")}


def test_change_stage_rejects_unknown_stage_t167():
    s = _store_t167()
    conv = _open_t167(s)
    with pytest.raises(ValueError):
        C.change_stage(s, conv, "escalated", turn_id="t", reason="x")
    assert _events_t167(s, conv) == []


def test_change_stage_checks_the_db_stage_not_the_snapshot_t167():
    s = _store_t167()
    stale = _open_t167(s)
    C.change_stage(s, stale, T.STAGE_HANDED_OFF, turn_id="t1", reason=T.HANDOFF_ANGER)
    with pytest.raises(ValueError):          # 库里已是 handed_off：再迁 handed_off 是自迁移
        C.change_stage(s, stale, T.STAGE_HANDED_OFF, turn_id="t2", reason=T.HANDOFF_ANGER)
    back = C.change_stage(s, stale, T.STAGE_ACTIVE, turn_id="t3", reason="human_released")
    assert back.stage == T.STAGE_ACTIVE


SENTINEL_STAGE_REASON_T167 = "客户说：我家住在梧桐路七号 SNTL-STAGE-REASON-K4"


def test_change_stage_reason_enum_verbatim_free_text_digested_t167():
    """reason 在 HANDOFF_REASONS / STAGE_EXTRA_REASONS 里原样落；其余（比如误传的客户原话）
    在 event_log.reason 与 detail.reason 里都只剩摘要。"""
    s = _store_t167()
    conv = _open_t167(s)
    conv = C.change_stage(s, conv, T.STAGE_HANDED_OFF, turn_id="t1", reason=T.HANDOFF_ANGER)
    conv = C.change_stage(s, conv, T.STAGE_ACTIVE, turn_id="t2", reason="human_released")
    conv = C.change_stage(s, conv, T.STAGE_HANDED_OFF, turn_id="t3",
                          reason=SENTINEL_STAGE_REASON_T167)
    assert conv.stage == T.STAGE_HANDED_OFF                         # 迁移本身照做
    rows = _events_t167(s, conv, T.EVENT_STAGE_CHANGED)
    expected = [T.HANDOFF_ANGER, "human_released", T.text_digest(SENTINEL_STAGE_REASON_T167)]
    assert [r["reason"] for r in rows] == expected
    assert [r["detail"]["reason"] for r in rows] == expected
    assert C.STAGE_EXTRA_REASONS == ("human_released",)
    raw = objects.query(s, "SELECT * FROM event_log ORDER BY seq")
    dump = json.dumps(raw, ensure_ascii=False) + json.dumps(rows, ensure_ascii=False)
    for needle in (SENTINEL_STAGE_REASON_T167, "梧桐路", "SNTL-STAGE-REASON-K4"):
        assert needle not in dump, needle


# ------------------------------------------------------------------ 转人工卡片
def test_handoff_cards_persist_roundtrip_and_delivery_writeback_t167():
    s = _store_t167()
    conv = _open_t167(s)
    cards = []
    for i, delivery in enumerate((T.DELIVERY_PENDING, T.DELIVERY_PENDING,
                                  T.DELIVERY_UNCONFIGURED)):
        turn_id, conv = _turn_t167(s, conv, T.ROUTE_HANDOFF, intent=T.INTENT_HANDOFF_REQUEST,
                                   handoff_reason=T.HANDOFF_REQUESTED)
        card = _card_t167(conv, turn_id, citations=(f"kb-cs-tnt-demo-GEN-00{i + 1}",),
                          recent=C.recent_turns(s, conv.tenant_id, conv.conversation_id))
        C.record_handoff(s, card, delivery=delivery,
                         now=f"2026-09-24T08:0{i}:00+00:00")
        cards.append(card)

    listed = C.list_handoffs(s, conv.tenant_id)
    assert [c for c, _d in listed] == cards                     # JSON 往返逐字段相等
    assert [d for _c, d in listed] == ["pending", "pending", "unconfigured"]
    row = objects.query(s, "SELECT card_json FROM cs_handoff WHERE handoff_id=?",
                        (cards[0].handoff_id,))[0]
    assert T.HandoffCard.from_json(json.loads(row["card_json"])) == cards[0]

    def _row(hid):
        return objects.query(s, "SELECT * FROM cs_handoff WHERE tenant_id=? AND handoff_id=?",
                             (conv.tenant_id, hid))[0]

    before = _row(cards[0].handoff_id)
    C.mark_handoff_delivery(s, conv.tenant_id, cards[0].handoff_id, T.DELIVERY_DELIVERED,
                            delivered_to="feishu:oc_room", now=LATER_T167)
    after = _row(cards[0].handoff_id)
    changed = {k for k in before if before[k] != after[k]}
    assert changed == {"delivery", "delivered_to", "updated_at"}
    assert (after["delivery"], after["delivered_to"], after["updated_at"]) == (
        "delivered", "feishu:oc_room", LATER_T167)
    C.mark_handoff_delivery(s, conv.tenant_id, cards[1].handoff_id, T.DELIVERY_FAILED)

    by = {d: [c.handoff_id for c, _ in C.list_handoffs(s, conv.tenant_id, delivery=d)]
          for d in T.DELIVERIES}
    assert by == {"pending": [], "delivered": [cards[0].handoff_id],
                  "failed": [cards[1].handoff_id], "unconfigured": [cards[2].handoff_id]}
    assert C.list_handoffs(s, "tnt-other") == []
    raised = _events_t167(s, conv, T.EVENT_HANDOFF_RAISED)
    assert len(raised) == 3                                   # 回写投递不落事件
    # CsHandoffRaised 记的是**落库那一刻**的投递状态（含 unconfigured），不被后来的回写改。
    assert [(r["task_id"], r["detail"]["delivery"], r["detail"]["intent"],
             r["detail"]["channel"]) for r in raised] == [
        (cards[0].handoff_id, "pending", "handoff_request", "wechat_kf"),
        (cards[1].handoff_id, "pending", "handoff_request", "wechat_kf"),
        (cards[2].handoff_id, "unconfigured", "handoff_request", "wechat_kf")]


def test_handoff_timestamps_follow_injected_now_t167():
    """``record_handoff`` 的 created_at / updated_at 取注入的 ``now``；``list_handoffs`` 按
    created_at 排（同刻再按 handoff_id）。

    注入的时刻故意**倒着走**、且远早于真实时钟：实现若忽略 now 改取真实时钟，时间戳
    对不上，列表顺序也会退回落库先后（= handoff_id 升序），两处都红。
    """
    s = _store_t167()
    conv = _open_t167(s)
    stamps = ("2001-01-01T00:00:03+00:00", "2001-01-01T00:00:02+00:00",
              "2001-01-01T00:00:01+00:00")
    cards = []
    for ts in stamps:
        turn_id, conv = _turn_t167(s, conv, T.ROUTE_HANDOFF, intent=T.INTENT_HANDOFF_REQUEST,
                                   handoff_reason=T.HANDOFF_REQUESTED)
        card = _card_t167(conv, turn_id)
        C.record_handoff(s, card, now=ts)
        cards.append(card)

    rows = objects.query(s, "SELECT handoff_id, created_at, updated_at FROM cs_handoff"
                            " WHERE tenant_id=? ORDER BY handoff_id", (conv.tenant_id,))
    assert [(r["handoff_id"], r["created_at"], r["updated_at"]) for r in rows] == [
        (c.handoff_id, ts, ts) for c, ts in zip(cards, stamps)]
    assert [c for c, _d in C.list_handoffs(s, conv.tenant_id)] == cards[::-1]
    assert [c for c, _d in C.list_handoffs(s, conv.tenant_id,
                                           delivery=T.DELIVERY_PENDING)] == cards[::-1]

    # 回写投递：updated_at 取注入的 now，created_at（排序键）不动，顺序不变。
    C.mark_handoff_delivery(s, conv.tenant_id, cards[2].handoff_id, T.DELIVERY_DELIVERED,
                            now="2001-01-01T00:00:09+00:00")
    (row,) = objects.query(s, "SELECT created_at, updated_at FROM cs_handoff"
                              " WHERE tenant_id=? AND handoff_id=?",
                           (conv.tenant_id, cards[2].handoff_id))
    assert (row["created_at"], row["updated_at"]) == (stamps[2], "2001-01-01T00:00:09+00:00")
    assert [c for c, _d in C.list_handoffs(s, conv.tenant_id)] == cards[::-1]


def test_handoff_rejects_bad_values_t167():
    s = _store_t167()
    conv = _open_t167(s)
    turn_id, conv = _turn_t167(s, conv, T.ROUTE_HANDOFF, handoff_reason=T.HANDOFF_ANGER)
    card = _card_t167(conv, turn_id, reason=T.HANDOFF_ANGER)
    with pytest.raises(ValueError):
        C.record_handoff(s, card, delivery="sent")
    with pytest.raises(ValueError):
        C.record_handoff(s, dataclasses.replace(card, reason="bogus"))
    with pytest.raises(ValueError):
        C.record_handoff(s, dataclasses.replace(card, handoff_id="h-other"))
    with pytest.raises(ValueError):
        C.record_handoff(s, dataclasses.replace(card, intent="shopping"))
    assert C.list_handoffs(s, conv.tenant_id) == []
    assert objects.query(s, "SELECT COUNT(*) AS n FROM cs_handoff") == [{"n": 0}]
    assert _events_t167(s, conv, T.EVENT_HANDOFF_RAISED) == []
    C.record_handoff(s, card)
    with pytest.raises(sqlite3.IntegrityError):                   # 一轮至多一张卡
        C.record_handoff(s, card)
    with pytest.raises(ValueError):
        C.mark_handoff_delivery(s, conv.tenant_id, card.handoff_id, "sent")
    with pytest.raises(LookupError):
        C.mark_handoff_delivery(s, conv.tenant_id, "no-such-card", T.DELIVERY_DELIVERED)
    with pytest.raises(ValueError):
        C.list_handoffs(s, conv.tenant_id, delivery="sent")
    assert [d for _c, d in C.list_handoffs(s, conv.tenant_id)] == ["pending"]
    assert len(_events_t167(s, conv, T.EVENT_HANDOFF_RAISED)) == 1


# ------------------------------------------------------------------ 事件条数与归属
#: 四种事件 detail 的键集，写死（不从实现里读）。加键要先过契约 §1.4 的白名单。
DETAIL_KEYS_T167 = {
    T.EVENT_TURN_RECORDED: {"seq", "route", "intent", "handoff_reason", "stage",
                            "inbound_digest", "reply_digest", "citations", "claim_count",
                            "check_ok", "violation_kinds", "fallback_streak",
                            # p13 契约 §1.3（T171）：两个枚举、两个库里读的计数。
                            "lang", "lookup_outcome", "observation_count", "slot_count"},
    T.EVENT_STAGE_CHANGED: {"from_stage", "to_stage", "reason", "fallback_streak",
                            "turn_count"},
    T.EVENT_HANDOFF_RAISED: {"reason", "intent", "channel", "delivery", "citations",
                             "recent_turn_count", "slot_count", "customer_text_digest",
                             "suggestion_digest"},
    T.EVENT_REPLY_REJECTED: {"violation_kinds", "violation_count", "violation_digests"},
}


def _script_t167(store, *, inbound, reply, open_kfid, external_userid):
    """一段完整会话：答上 → 兜底 → 被拦 + 转人工出卡 + 转阶段 → 静默。返回 (conv, 轮次 id)。"""
    conv = _open_t167(store, open_kfid=open_kfid, external_userid=external_userid)
    t1, conv = _turn_t167(store, conv, T.ROUTE_ANSWER, inbound=inbound, reply=reply,
                          draft=T.ReplyDraft(text=reply, citations=("kb-cs-tnt-demo-LOG-001",),
                                             claims=(T.Claim(literal=reply,
                                                             basis_ref="kb:kb-cs-tnt-demo-LOG-001"),)))
    t2, conv = _turn_t167(store, conv, T.ROUTE_FALLBACK, inbound=inbound + "？", reply=reply,
                          intent=T.INTENT_UNKNOWN)
    t3, seq3 = C.allocate_turn(store, conv)
    rejected = T.CheckResult(ok=False, violations=(
        T.Violation(kind=T.VIOLATION_UNBACKED_STATUS, detail=f"「{reply}」里的状态字眼没有观察"),
        T.Violation(kind=T.VIOLATION_UNCITED_RULE, detail=f"{inbound} 引了没检出的话术"),
    ))
    C.record_reply_rejected(store, conv, turn_id=t3, check=rejected)
    conv = C.record_turn(store, conv, turn_id=t3, seq=seq3, msg_dedup_key="",
                         inbound_text=inbound, reply_text=reply, route=T.ROUTE_HANDOFF,
                         intent=T.INTENT_LOGISTICS,
                         handoff_reason=T.HANDOFF_UNVERIFIED_CLAIM,
                         draft=T.ReplyDraft(text=reply), check=rejected)
    card = T.HandoffCard(
        handoff_id=t3, tenant_id=conv.tenant_id, conversation_id=conv.conversation_id,
        turn_id=t3, channel=conv.channel, reason=T.HANDOFF_UNVERIFIED_CLAIM,
        intent=T.INTENT_LOGISTICS, customer_ref=T.mask_customer(external_userid),
        customer_text=inbound,
        recent_turns=tuple(C.recent_turns(store, conv.tenant_id, conv.conversation_id)),
        suggestion=f"客户原话：{inbound}；机器人拟回：{reply}",
        citations=("kb-cs-tnt-demo-LOG-001",), created_at=NOW_T167)
    C.record_handoff(store, card)
    conv = C.change_stage(store, conv, T.STAGE_HANDED_OFF, turn_id=t3,
                          reason=T.HANDOFF_UNVERIFIED_CLAIM)
    t4, conv = _turn_t167(store, conv, T.ROUTE_SILENT, inbound=inbound + "！", reply="",
                          intent="")
    C.mark_handoff_delivery(store, conv.tenant_id, t3, T.DELIVERY_DELIVERED,
                            delivered_to="feishu:oc_room")
    return conv, (t1, t2, t3, t4)


def test_event_counts_and_attribution_t167():
    s = _store_t167()
    conv, (t1, t2, t3, t4) = _script_t167(s, inbound="我的耳机什么时候发货", reply="一般 48 小时内发出",
                                          open_kfid="wk_1", external_userid="wm_customer_1")
    rows = _events_t167(s, conv)
    counts = {et: sum(1 for r in rows if r["event_type"] == et) for et in T.CS_EVENT_TYPES}
    assert counts == {T.EVENT_TURN_RECORDED: 4, T.EVENT_STAGE_CHANGED: 1,
                      T.EVENT_HANDOFF_RAISED: 1, T.EVENT_REPLY_REJECTED: 1}
    assert len(rows) == 7                                     # 没有别的事件类型
    for r in rows:
        assert r["plan_id"] == "cs:" + conv.conversation_id == T.plan_id_for(conv.conversation_id)
        assert r["trace_id"] == ""
    turn_rows = [r for r in rows if r["event_type"] == T.EVENT_TURN_RECORDED]
    assert [r["task_id"] for r in turn_rows] == [t1, t2, t3, t4]
    assert [r["detail"]["route"] for r in turn_rows] == ["answer", "fallback", "handoff", "silent"]
    assert [r["detail"]["seq"] for r in turn_rows] == [1, 2, 3, 4]
    assert turn_rows[0]["detail"]["citations"] == ["kb-cs-tnt-demo-LOG-001"]
    assert turn_rows[2]["detail"]["handoff_reason"] == "unverified_claim"
    assert turn_rows[2]["detail"]["violation_kinds"] == ["unbacked_status", "uncited_rule"]
    for et in (T.EVENT_STAGE_CHANGED, T.EVENT_HANDOFF_RAISED, T.EVENT_REPLY_REJECTED):
        (row,) = [r for r in rows if r["event_type"] == et]
        assert row["task_id"] == t3, et
    (rej,) = [r for r in rows if r["event_type"] == T.EVENT_REPLY_REJECTED]
    assert rej["detail"]["violation_kinds"] == ["unbacked_status", "uncited_rule"]
    assert rej["detail"]["violation_count"] == 2
    (raised,) = [r for r in rows if r["event_type"] == T.EVENT_HANDOFF_RAISED]
    assert raised["detail"]["reason"] == "unverified_claim"
    assert raised["detail"]["citations"] == ["kb-cs-tnt-demo-LOG-001"]
    assert raised["detail"]["recent_turn_count"] == 3
    # detail 的键集按事件类型写死（契约 §1.4「只许」）：多一个键就红。
    for r in rows:
        assert set(r["detail"]) == DETAIL_KEYS_T167[r["event_type"]], r["event_type"]
    assert set(DETAIL_KEYS_T167) == set(T.CS_EVENT_TYPES)
    # 七行逐行逐值写死（不只键集）：枚举值、计数、摘要写错一个就红。
    assert [_audit_t167(r) for r in rows] == _expected_audit_t167(
        inbound="我的耳机什么时候发货", reply="一般 48 小时内发出", turns=(t1, t2, t3, t4))
    # 别的会话的行不串进来：另开一段，本会话的条数不变。
    _open_t167(s, external_userid="wm_customer_2")
    assert len(_events_t167(s, conv)) == 7


def _audit_t167(row: dict) -> tuple:
    return (row["event_type"], row["task_id"], row["from_state"], row["to_state"],
            row["reason"], row["detail"])


def _expected_audit_t167(*, inbound: str, reply: str, turns: tuple) -> list[tuple]:
    """``_script_t167`` 那段会话应落的七行审计，按落库顺序、逐值写死。

    摘要用冻结的 ``types.text_digest`` 现算（它本身由契约测试钉）；其余全是字面量。
    """
    t1, t2, t3, t4 = turns
    d = T.text_digest
    log = "kb-cs-tnt-demo-LOG-001"
    kinds = ["unbacked_status", "uncited_rule"]

    def turn(tid, seq, route, intent, reason, stage, inb, rep, cites, claims, ok, vk, streak):
        return ("CsTurnRecorded", tid, None, None, route, {
            "seq": seq, "route": route, "intent": intent, "handoff_reason": reason,
            "stage": stage, "inbound_digest": d(inb), "reply_digest": d(rep),
            "citations": cites, "claim_count": claims, "check_ok": ok,
            "violation_kinds": vk, "fallback_streak": streak,
            # p13（T171）：p12 式调用走缺省 —— 中文、没查单、本轮零观察、会话零槽位。
            "lang": "zh", "lookup_outcome": "", "observation_count": 0, "slot_count": 0})

    return [
        turn(t1, 1, "answer", "general", "", "active", inbound, reply, [log], 1, True, [], 0),
        turn(t2, 2, "fallback", "unknown", "", "active", inbound + "？", reply, [], 0, True,
             [], 1),
        ("CsReplyRejected", t3, None, None, "unbacked_status,uncited_rule", {
            "violation_kinds": kinds, "violation_count": 2,
            "violation_digests": [d(f"「{reply}」里的状态字眼没有观察"),
                                  d(f"{inbound} 引了没检出的话术")]}),
        turn(t3, 3, "handoff", "logistics", "unverified_claim", "active", inbound, reply, [], 0,
             False, kinds, 0),
        ("CsHandoffRaised", t3, None, None, "unverified_claim", {
            "reason": "unverified_claim", "intent": "logistics", "channel": "wechat_kf",
            "delivery": "pending", "citations": [log], "recent_turn_count": 3,
            "slot_count": 0, "customer_text_digest": d(inbound),
            "suggestion_digest": d(f"客户原话：{inbound}；机器人拟回：{reply}")}),
        ("CsConversationStageChanged", t3, "active", "handed_off", "unverified_claim", {
            "from_stage": "active", "to_stage": "handed_off", "reason": "unverified_claim",
            "fallback_streak": 0, "turn_count": 3}),
        # 已转人工后的静默轮：stage 是库里的 handed_off，回复原文是空串。
        turn(t4, 4, "silent", "", "", "handed_off", inbound + "！", "", [], 0, True, [], 0),
    ]


def test_stage_changed_detail_snapshots_counters_from_db_t167():
    """CsConversationStageChanged 的 detail 带迁移那一刻库里的 fallback_streak / turn_count
    （非零），from_stage / to_stage 与 event_log 的 from_state / to_state 一致。"""
    s = _store_t167()
    stale = _open_t167(s)
    conv = stale
    for _ in range(2):
        _tid, conv = _turn_t167(s, conv, T.ROUTE_FALLBACK, intent=T.INTENT_UNKNOWN)
    C.allocate_turn(s, conv)                                  # 取了号还没落：turn_count=3
    out = C.change_stage(s, stale, T.STAGE_CLOSED, turn_id="t-close",
                         reason=T.HANDOFF_REPEATED_FALLBACK)  # 故意传旧快照
    assert (out.stage, out.fallback_streak, out.turn_count) == ("closed", 2, 3)
    (ev,) = _events_t167(s, conv, T.EVENT_STAGE_CHANGED)
    assert _audit_t167(ev) == (
        "CsConversationStageChanged", "t-close", "active", "closed", "repeated_fallback",
        {"from_stage": "active", "to_stage": "closed", "reason": "repeated_fallback",
         "fallback_streak": 2, "turn_count": 3})


def test_record_reply_rejected_requires_a_failed_check_t167():
    s = _store_t167()
    conv = _open_t167(s)
    with pytest.raises(ValueError):
        C.record_reply_rejected(s, conv, turn_id=conv.conversation_id + "-t0001",
                                check=T.CheckResult(ok=True))
    assert _events_t167(s, conv) == []


# ------------------------------------------------------------------ 哨兵
SENTINEL_INBOUND_T167 = "哨兵客户原文·我家住在梧桐路七号 SNTL-INBOUND-Q7"
SENTINEL_REPLY_T167 = "哨兵回复原文·亲这边帮您看一下 SNTL-REPLY-Z9"
SENTINEL_USERID_T167 = "wm_SNTL_EXTERNAL_USERID_QXKZWV"
SENTINEL_KFID_T167 = "wk_SNTL_OPEN_KFID_JMPRTQ"


def test_sentinels_never_reach_event_log_t167():
    s = _store_t167()
    conv, _turns = _script_t167(s, inbound=SENTINEL_INBOUND_T167, reply=SENTINEL_REPLY_T167,
                                open_kfid=SENTINEL_KFID_T167,
                                external_userid=SENTINEL_USERID_T167)
    # 判据不空转：四种事件都落了，哨兵也确实住在会话表里。
    raw = objects.query(s, "SELECT * FROM event_log ORDER BY seq")
    assert {r["event_type"] for r in raw} == set(T.CS_EVENT_TYPES)
    conv_dump = json.dumps(objects.query(s, "SELECT * FROM cs_conversation"), ensure_ascii=False)
    turn_dump = json.dumps(objects.query(s, "SELECT * FROM cs_turn"), ensure_ascii=False)
    card_dump = json.dumps(objects.query(s, "SELECT * FROM cs_handoff"), ensure_ascii=False)
    assert SENTINEL_USERID_T167 in conv_dump and SENTINEL_KFID_T167 in conv_dump
    assert SENTINEL_INBOUND_T167 in turn_dump and SENTINEL_REPLY_T167 in turn_dump
    assert SENTINEL_INBOUND_T167 in card_dump and SENTINEL_REPLY_T167 in card_dump
    assert SENTINEL_USERID_T167[-6:] in card_dump             # 卡片里有打码后的客户标识

    # event_log 的每一行、每一列（原始库文本 + 两种 JSON 序列化）都不许出现。
    dumps = (
        json.dumps(raw, ensure_ascii=False),
        json.dumps(raw, ensure_ascii=True),
        json.dumps(s.list_event_log(T.plan_id_for(conv.conversation_id)), ensure_ascii=False),
        "\n".join(str(v) for r in raw for v in r.values()),
    )
    needles = (SENTINEL_INBOUND_T167, SENTINEL_REPLY_T167, SENTINEL_USERID_T167,
               SENTINEL_KFID_T167,
               "SNTL-INBOUND-Q7", "SNTL-REPLY-Z9", "梧桐路", "帮您看一下",
               SENTINEL_USERID_T167[-6:], SENTINEL_KFID_T167[-6:])
    for dump in dumps:
        for needle in needles:
            assert needle not in dump, needle
    # 摘要是落了的（能对上号）。
    turn_details = [json.loads(r["detail"]) for r in raw if r["event_type"] == T.EVENT_TURN_RECORDED]
    assert turn_details[0]["inbound_digest"] == T.text_digest(SENTINEL_INBOUND_T167)
    assert turn_details[0]["reply_digest"] == T.text_digest(SENTINEL_REPLY_T167)


SENTINEL_KIND_T167 = "哨兵违例种类·梧桐路 SNTL-KIND-W3"


def test_unknown_violation_kind_is_digested_everywhere_t167():
    """不在 VIOLATION_KINDS 里的种类（可能是一句自由文本）在 CsTurnRecorded 与
    CsReplyRejected 里都只落摘要；已知种类原样落。"""
    s = _store_t167()
    conv = _open_t167(s)
    check = T.CheckResult(ok=False, violations=(
        T.Violation(kind=T.VIOLATION_DANGLING_BASIS, detail="d1"),
        T.Violation(kind=SENTINEL_KIND_T167, detail="d2")))
    turn_id, seq = C.allocate_turn(s, conv)
    C.record_reply_rejected(s, conv, turn_id=turn_id, check=check)
    C.record_turn(s, conv, turn_id=turn_id, seq=seq, msg_dedup_key="", inbound_text="x",
                  reply_text="y", route=T.ROUTE_HANDOFF, intent=T.INTENT_UNKNOWN,
                  handoff_reason=T.HANDOFF_UNVERIFIED_CLAIM, draft=T.ReplyDraft(text="y"),
                  check=check)
    # 判据不空转：种类原文确实在 cs_turn.check_json 里。
    assert SENTINEL_KIND_T167 in json.dumps(objects.query(s, "SELECT check_json FROM cs_turn"),
                                            ensure_ascii=False)
    expected = [T.VIOLATION_DANGLING_BASIS, T.text_digest(SENTINEL_KIND_T167)]
    (rej,) = _events_t167(s, conv, T.EVENT_REPLY_REJECTED)
    (rec,) = _events_t167(s, conv, T.EVENT_TURN_RECORDED)
    assert rej["detail"]["violation_kinds"] == expected == rec["detail"]["violation_kinds"]
    assert rej["reason"] == ",".join(expected)
    raw = objects.query(s, "SELECT * FROM event_log ORDER BY seq")
    for dump in (json.dumps(raw, ensure_ascii=False), json.dumps(raw, ensure_ascii=True)):
        for needle in (SENTINEL_KIND_T167, "SNTL-KIND-W3", "梧桐路"):
            assert needle not in dump, needle


# ------------------------------------------------------------------ 空库直调
def test_every_entry_works_on_an_empty_db_without_prior_ensure_schema_t167():
    """契约「每个写入口先调 ensure_schema」：空库上（cs 表一张都没有）直接调卡片与
    最近几轮这几条路径，不报 no such table。"""
    def fresh_t167():
        s = _store_t167()
        assert objects.query(s, "SELECT name FROM sqlite_master WHERE name LIKE 'cs_%'") == []
        return s

    assert C.list_handoffs(fresh_t167(), "tnt-demo") == []
    assert C.list_handoffs(fresh_t167(), "tnt-demo", delivery=T.DELIVERY_PENDING) == []
    assert C.recent_turns(fresh_t167(), "tnt-demo", "csc-0000000000000000") == []
    assert C.get_conversation(fresh_t167(), "tnt-demo", "csc-0000000000000000") is None
    with pytest.raises(LookupError):
        C.mark_handoff_delivery(fresh_t167(), "tnt-demo", "no-such", T.DELIVERY_DELIVERED)

    s = fresh_t167()
    cid = T.conversation_id_for("tnt-demo", T.CHANNEL_WECHAT_KF, "wk_1", "wm_customer_1")
    tid = T.turn_id_for(cid, 1)
    card = T.HandoffCard(
        handoff_id=tid, tenant_id="tnt-demo", conversation_id=cid, turn_id=tid,
        channel=T.CHANNEL_WECHAT_KF, reason=T.HANDOFF_REQUESTED, intent=T.INTENT_HANDOFF_REQUEST,
        customer_ref=T.mask_customer("wm_customer_1"), customer_text="我要人工",
        recent_turns=(), suggestion="请接手", citations=(), created_at=NOW_T167)
    C.record_handoff(s, card)
    assert C.list_handoffs(s, "tnt-demo") == [(card, T.DELIVERY_PENDING)]

    s = fresh_t167()
    conv = C.Conversation(tenant_id="tnt-demo", conversation_id=cid, channel=T.CHANNEL_WECHAT_KF,
                          open_kfid="wk_1", external_userid="wm_customer_1",
                          stage=T.STAGE_ACTIVE, fallback_streak=0, turn_count=0,
                          opened_at=NOW_T167, updated_at=NOW_T167)
    for call in (lambda: C.allocate_turn(s, conv),
                 lambda: C.change_stage(s, conv, T.STAGE_CLOSED, turn_id=tid,
                                        reason=T.HANDOFF_REQUESTED),
                 lambda: C.record_turn(s, conv, turn_id=tid, seq=1, msg_dedup_key="",
                                       inbound_text="x", reply_text="y",
                                       route=T.ROUTE_ANSWER, intent=T.INTENT_GENERAL,
                                       handoff_reason="", draft=T.ReplyDraft(text="y"),
                                       check=T.CheckResult(ok=True))):
        with pytest.raises(LookupError):                  # 会话不存在，不是 no such table
            call()


# ------------------------------------------------------------------ 最近几轮
def test_recent_turns_order_and_truncation_t167():
    s = _store_t167()
    conv = _open_t167(s)
    other = _open_t167(s, external_userid="wm_customer_2")
    _turn_t167(s, other, T.ROUTE_ANSWER, inbound="别的会话", reply="别的回复")

    allocated = [C.allocate_turn(s, conv) for _ in range(7)]
    # 故意乱序落库：顺序要按 seq，不按插入先后。
    for turn_id, seq in [allocated[i] for i in (2, 0, 6, 1, 5, 3, 4)]:
        C.record_turn(s, conv, turn_id=turn_id, seq=seq, msg_dedup_key="",
                      inbound_text=f"问{seq}", reply_text=f"答{seq}", route=T.ROUTE_ANSWER,
                      intent=T.INTENT_GENERAL, handoff_reason="",
                      draft=T.ReplyDraft(text=f"答{seq}"), check=T.CheckResult(ok=True))

    def pairs(ns):
        return [(f"问{n}", f"答{n}") for n in ns]

    assert C.recent_turns(s, conv.tenant_id, conv.conversation_id) == pairs(range(3, 8))
    assert T.CARD_RECENT_TURNS == 5
    assert C.recent_turns(s, conv.tenant_id, conv.conversation_id, limit=2) == pairs((6, 7))
    assert C.recent_turns(s, conv.tenant_id, conv.conversation_id, limit=1) == pairs((7,))
    assert C.recent_turns(s, conv.tenant_id, conv.conversation_id, limit=50) == pairs(range(1, 8))
    assert C.recent_turns(s, conv.tenant_id, conv.conversation_id, limit=0) == []
    assert C.recent_turns(s, "tnt-other", conv.conversation_id) == []
    assert C.recent_turns(s, other.tenant_id, other.conversation_id) == [("别的会话", "别的回复")]
