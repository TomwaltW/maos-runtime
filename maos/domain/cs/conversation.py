"""客服会话对象 —— 取或建会话、取号、落轮次、改阶段、转人工卡片（跨轨契约 §1.4 T167）。

## 三条口径

1. **会话进度是会话对象自己的字段**（铁律 9）：``cs_conversation.stage`` 与
   ``cs_handoff.delivery``。迁移表是 ``types.STAGE_FLOW``，本模块不碰
   ``maos/contracts/**``、不建 Plan / Task。
2. **原文只住在会话表里**：客户原文、回复原文、external_userid、open_kfid 都在
   ``cs_conversation`` / ``cs_turn`` / ``cs_handoff.card_json`` 里。四种 Cs* 事件的
   ``event_log`` 行只带摘要（``types.text_digest``）、枚举值、doc_id、计数、违例种类
   —— 审计行会进更多人的视野，客户身份不该明文躺在那里（契约 §2 R5）。
3. **轮次归属**（契约 §1.2）：``plan_id = types.plan_id_for(conversation_id)``、
   ``task_id = turn_id``、``trace_id = ""``。不编非空 trace_id（查不到 plan 会让
   verify 第 8 项判负）。

## 事务边界

会话表上的「读 → 算 → 写」一律在 ``objects.transaction`` 里做（整块持锁、同生共死）。
``event_log`` 行在事务**提交之后**再落，写法照 ``maos/domain/refund/guard.py`` 写
``RefundBizStatusChanged`` 那段：event_log 是核心 Store 的冻结表，走
``store.append_event_log``（它自己 commit）；PG 后端时会话表与 event_log 甚至不在
同一个库，拉不进同一个事务。于是顺序是「先落事实、再落审计」：审计写失败时事实已在，
不会出现「审计说写了、表里没有」。

时间戳一律 Python 侧 ``datetime.now(timezone.utc).isoformat()``，每个写入口都收
``now=`` 以便测试注入。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from maos.domain.cs import objects
from maos.domain.cs.types import (
    CARD_RECENT_TURNS,
    DELIVERIES,
    DELIVERY_PENDING,
    EVENT_HANDOFF_RAISED,
    EVENT_REPLY_REJECTED,
    EVENT_STAGE_CHANGED,
    EVENT_TURN_RECORDED,
    HANDOFF_REASONS,
    INTENTS,
    ROUTE_ANSWER,
    ROUTE_FALLBACK,
    ROUTE_HANDOFF,
    ROUTE_SILENT,
    ROUTES,
    STAGE_ACTIVE,
    STAGE_FLOW,
    STAGES,
    VIOLATION_KINDS,
    CheckResult,
    HandoffCard,
    ReplyDraft,
    conversation_id_for,
    plan_id_for,
    text_digest,
    turn_id_for,
)


@dataclass(frozen=True)
class Conversation:
    """一段会话。字段 = ``cs_conversation`` 的列，同名同序。"""
    tenant_id: str
    conversation_id: str
    channel: str
    open_kfid: str
    external_userid: str
    stage: str
    fallback_streak: int
    turn_count: int
    opened_at: str
    updated_at: str


_CONV_COLUMNS = ("tenant_id", "conversation_id", "channel", "open_kfid", "external_userid",
                 "stage", "fallback_streak", "turn_count", "opened_at", "updated_at")


def _conv_from_row(row: dict) -> Conversation:
    return Conversation(
        tenant_id=str(row["tenant_id"]), conversation_id=str(row["conversation_id"]),
        channel=str(row["channel"]), open_kfid=str(row["open_kfid"]),
        external_userid=str(row["external_userid"]), stage=str(row["stage"]),
        fallback_streak=int(row["fallback_streak"]), turn_count=int(row["turn_count"]),
        opened_at=str(row["opened_at"]), updated_at=str(row["updated_at"]),
    )


_SELECT_CONV = ("SELECT " + ", ".join(_CONV_COLUMNS) + " FROM cs_conversation"
                " WHERE tenant_id=? AND conversation_id=?")


def _missing(tenant_id: str, conversation_id: str) -> LookupError:
    return LookupError(f"会话不存在：tenant={tenant_id!r} conversation={conversation_id}"
                       "（先 open_conversation）")


# ------------------------------------------------------------------ event_log
def _emit(store: Any, *, conversation_id: str, turn_id: str, event_type: str, reason: str,
          detail: dict, from_state: str | None = None, to_state: str | None = None) -> None:
    """落一条 Cs* 事件。归属三元组照契约 §1.2，detail 由调用方只装白名单里的东西。"""
    store.append_event_log({
        "event_id": "",
        "trace_id": "",
        "plan_id": plan_id_for(conversation_id),
        "task_id": turn_id,
        "event_type": event_type,
        "from_state": from_state,
        "to_state": to_state,
        "reason": reason,
        "detail": detail,
    })


def _violation_kinds(check: CheckResult) -> list[str]:
    """违例种类。不在 VIOLATION_KINDS 里的种类只落摘要 —— 种类以外的字眼不进审计行。"""
    return [v.kind if v.kind in VIOLATION_KINDS else text_digest(v.kind)
            for v in check.violations]


# ------------------------------------------------------------------ 会话
def open_conversation(store: Any, *, tenant_id: str, channel: str, open_kfid: str,
                      external_userid: str, now: str | None = None) -> Conversation:
    """取或建。新建的会话 ``stage=active``、计数全 0；已存在就原样返回、不改任何字段。

    id 由 ``types.conversation_id_for`` 算：同一个客户找两个客服账号（open_kfid 不同）
    是两段会话。租户映射不到时 tenant_id 是空串，照样建得出来（不猜默认租户）。
    """
    objects.ensure_schema(store)
    cid = conversation_id_for(tenant_id, channel, open_kfid, external_userid)
    ts = now or objects._now()
    with objects.transaction(store) as db:
        rows = db.query(_SELECT_CONV, (tenant_id, cid))
        if rows:
            return _conv_from_row(rows[0])
        db.execute(
            "INSERT INTO cs_conversation (tenant_id, conversation_id, channel, open_kfid,"
            " external_userid, stage, fallback_streak, turn_count, opened_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?, ?)",
            (tenant_id, cid, channel, open_kfid, external_userid, STAGE_ACTIVE, ts, ts))
    return Conversation(tenant_id=tenant_id, conversation_id=cid, channel=channel,
                        open_kfid=open_kfid, external_userid=external_userid,
                        stage=STAGE_ACTIVE, fallback_streak=0, turn_count=0,
                        opened_at=ts, updated_at=ts)


def get_conversation(store: Any, tenant_id: str, conversation_id: str) -> Conversation | None:
    """按 (租户, 会话 id) 读；不存在返回 None。"""
    objects.ensure_schema(store)
    rows = objects.query(store, _SELECT_CONV, (tenant_id, conversation_id))
    return _conv_from_row(rows[0]) if rows else None


def allocate_turn(store: Any, conv: Conversation) -> tuple[str, int]:
    """取下一轮的号：库里的 ``turn_count`` 原子 +1，返回 ``(turn_id, seq)``。

    号以**库里**的计数为准，不以传进来的 ``conv.turn_count`` 为准（那可能是旧快照）。
    「+1 再读回」在一个持锁事务里完成，同进程并发取号不重不漏。
    """
    objects.ensure_schema(store)
    with objects.transaction(store) as db:
        db.execute("UPDATE cs_conversation SET turn_count = turn_count + 1"
                   " WHERE tenant_id=? AND conversation_id=?",
                   (conv.tenant_id, conv.conversation_id))
        rows = db.query("SELECT turn_count FROM cs_conversation"
                        " WHERE tenant_id=? AND conversation_id=?",
                        (conv.tenant_id, conv.conversation_id))
        if not rows:
            raise _missing(conv.tenant_id, conv.conversation_id)
        seq = int(rows[0]["turn_count"])
    return turn_id_for(conv.conversation_id, seq), seq


def _next_streak(route: str, current: int) -> int:
    """fallback_streak 的更新规则（契约 §1.4）：兜底 +1，答上 / 转人工清零，静默不变。"""
    if route == ROUTE_FALLBACK:
        return current + 1
    if route in (ROUTE_ANSWER, ROUTE_HANDOFF):
        return 0
    if route == ROUTE_SILENT:
        return current
    raise ValueError(f"未知的 route {route!r}，只认 {ROUTES}")


def record_turn(store: Any, conv: Conversation, *, turn_id: str, seq: int, msg_dedup_key: str,
                inbound_text: str, reply_text: str, route: str, intent: str,
                handoff_reason: str, draft: ReplyDraft, check: CheckResult,
                now: str | None = None) -> Conversation:
    """落一轮：插 ``cs_turn``、按规则更新 ``fallback_streak`` 与 ``updated_at``（同一个事务），
    提交后落恰好一条 ``CsTurnRecorded``；返回更新后的会话。

    枚举在进库前先校验（``ValueError``）：库上的 CHECK 是最后一道，不是第一道。
    ``turn_id`` 必须是 ``turn_id_for(conv.conversation_id, seq)``（即 ``allocate_turn`` 给的那一对）。
    """
    if route not in ROUTES:
        raise ValueError(f"未知的 route {route!r}，只认 {ROUTES}")
    if handoff_reason and handoff_reason not in HANDOFF_REASONS:
        raise ValueError(f"未知的 handoff_reason {handoff_reason!r}，只认空串或 {HANDOFF_REASONS}")
    if intent and intent not in INTENTS:
        raise ValueError(f"未知的 intent {intent!r}，只认空串或 {INTENTS}")
    if turn_id != turn_id_for(conv.conversation_id, seq):
        raise ValueError(f"turn_id {turn_id!r} 与 seq={seq} 对不上"
                         f"（应为 {turn_id_for(conv.conversation_id, seq)!r}）")

    objects.ensure_schema(store)
    ts = now or objects._now()
    with objects.transaction(store) as db:
        rows = db.query("SELECT fallback_streak, stage FROM cs_conversation"
                        " WHERE tenant_id=? AND conversation_id=?",
                        (conv.tenant_id, conv.conversation_id))
        if not rows:
            raise _missing(conv.tenant_id, conv.conversation_id)
        streak = _next_streak(route, int(rows[0]["fallback_streak"]))
        stage = str(rows[0]["stage"])
        db.execute(
            "INSERT INTO cs_turn (tenant_id, conversation_id, turn_id, seq, msg_dedup_key,"
            " inbound_text, reply_text, route, intent, handoff_reason, draft_json, check_json,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (conv.tenant_id, conv.conversation_id, turn_id, int(seq), msg_dedup_key or "",
             inbound_text, reply_text or "", route, intent or "", handoff_reason or "",
             json.dumps(draft.to_json(), ensure_ascii=False),
             json.dumps(check.to_json(), ensure_ascii=False), ts))
        db.execute("UPDATE cs_conversation SET fallback_streak=?, updated_at=?"
                   " WHERE tenant_id=? AND conversation_id=?",
                   (streak, ts, conv.tenant_id, conv.conversation_id))

    _emit(store, conversation_id=conv.conversation_id, turn_id=turn_id,
          event_type=EVENT_TURN_RECORDED, reason=route,
          detail={
              "seq": int(seq),
              "route": route,
              "intent": intent or "",
              "handoff_reason": handoff_reason or "",
              "stage": stage,
              "inbound_digest": text_digest(inbound_text),
              "reply_digest": text_digest(reply_text or ""),
              "citations": list(draft.citations),
              "claim_count": len(draft.claims),
              "check_ok": bool(check.ok),
              "violation_kinds": _violation_kinds(check),
              "fallback_streak": streak,
          })
    updated = get_conversation(store, conv.tenant_id, conv.conversation_id)
    assert updated is not None
    return updated


def change_stage(store: Any, conv: Conversation, to_stage: str, *, turn_id: str, reason: str,
                 now: str | None = None) -> Conversation:
    """按 ``types.STAGE_FLOW`` 迁移会话阶段；非法迁移（含自迁移、未知阶段）抛 ``ValueError``。

    起点以**库里**的阶段为准。成功时落恰好一条 ``CsConversationStageChanged``
    （from_state / to_state 是会话阶段，不是 Task 状态）。``reason`` 进审计行，
    调用方只传枚举值（通常取 ``HANDOFF_REASONS``），不传原文。
    """
    if to_stage not in STAGES:
        raise ValueError(f"未知的会话阶段 {to_stage!r}，只认 {STAGES}")
    objects.ensure_schema(store)
    ts = now or objects._now()
    with objects.transaction(store) as db:
        rows = db.query("SELECT stage, fallback_streak, turn_count FROM cs_conversation"
                        " WHERE tenant_id=? AND conversation_id=?",
                        (conv.tenant_id, conv.conversation_id))
        if not rows:
            raise _missing(conv.tenant_id, conv.conversation_id)
        cur = str(rows[0]["stage"])
        allowed = STAGE_FLOW.get(cur, frozenset())
        if to_stage not in allowed:
            raise ValueError(f"会话阶段不许从 {cur} 迁到 {to_stage}；"
                             f"{cur} 的合法去向：{sorted(allowed) or '无（吸收态）'}")
        db.execute("UPDATE cs_conversation SET stage=?, updated_at=?"
                   " WHERE tenant_id=? AND conversation_id=? AND stage=?",
                   (to_stage, ts, conv.tenant_id, conv.conversation_id, cur))
        streak, turns = int(rows[0]["fallback_streak"]), int(rows[0]["turn_count"])

    _emit(store, conversation_id=conv.conversation_id, turn_id=turn_id,
          event_type=EVENT_STAGE_CHANGED, reason=reason,
          from_state=cur, to_state=to_stage,
          detail={"from_stage": cur, "to_stage": to_stage, "reason": reason,
                  "fallback_streak": streak, "turn_count": turns})
    updated = get_conversation(store, conv.tenant_id, conv.conversation_id)
    assert updated is not None
    return updated


def recent_turns(store: Any, tenant_id: str, conversation_id: str, *,
                 limit: int = CARD_RECENT_TURNS) -> list[tuple[str, str]]:
    """``[(客户原文, 回复原文)]``，按 seq 升序、取最后 ``limit`` 轮。``limit <= 0`` 返回空。"""
    if limit <= 0:
        return []
    objects.ensure_schema(store)
    rows = objects.query(
        store,
        "SELECT inbound_text, reply_text FROM cs_turn WHERE tenant_id=? AND conversation_id=?"
        " ORDER BY seq DESC LIMIT ?",
        (tenant_id, conversation_id, int(limit)))
    return [(str(r["inbound_text"]), str(r["reply_text"])) for r in reversed(rows)]


# ------------------------------------------------------------------ 转人工卡片
def record_handoff(store: Any, card: HandoffCard, *, delivery: str = DELIVERY_PENDING,
                   now: str | None = None) -> None:
    """插 ``cs_handoff``（卡片原样存 ``card_json``），落恰好一条 ``CsHandoffRaised``。

    ``handoff_id`` 必须等于 ``turn_id``（契约 §1.3：一轮至多一张卡），同一轮第二张卡撞主键。
    """
    if delivery not in DELIVERIES:
        raise ValueError(f"未知的投递状态 {delivery!r}，只认 {DELIVERIES}")
    if card.reason not in HANDOFF_REASONS:
        raise ValueError(f"未知的转人工原因 {card.reason!r}，只认 {HANDOFF_REASONS}")
    if card.intent and card.intent not in INTENTS:
        raise ValueError(f"未知的 intent {card.intent!r}，只认空串或 {INTENTS}")
    if card.handoff_id != card.turn_id:
        raise ValueError(f"handoff_id {card.handoff_id!r} 必须与 turn_id {card.turn_id!r} 同值")

    objects.ensure_schema(store)
    ts = now or objects._now()
    objects.execute(
        store,
        "INSERT INTO cs_handoff (tenant_id, handoff_id, conversation_id, turn_id, reason,"
        " card_json, delivery, delivered_to, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, '', ?, ?)",
        (card.tenant_id, card.handoff_id, card.conversation_id, card.turn_id, card.reason,
         json.dumps(card.to_json(), ensure_ascii=False), delivery, ts, ts))

    _emit(store, conversation_id=card.conversation_id, turn_id=card.turn_id,
          event_type=EVENT_HANDOFF_RAISED, reason=card.reason,
          detail={
              "reason": card.reason,
              "intent": card.intent,
              "channel": card.channel,
              "delivery": delivery,
              "citations": list(card.citations),
              "recent_turn_count": len(card.recent_turns),
              "slot_count": len(card.slots),
              "customer_text_digest": text_digest(card.customer_text),
              "suggestion_digest": text_digest(card.suggestion),
          })


def mark_handoff_delivery(store: Any, tenant_id: str, handoff_id: str, delivery: str, *,
                          delivered_to: str = "", now: str | None = None) -> None:
    """回写投递结果：只改 ``delivery`` / ``delivered_to`` / ``updated_at``。

    非法投递状态抛 ``ValueError``；卡片不存在抛 ``LookupError``（回写一张不存在的卡
    多半是调用方拿错了 id，静默吞掉就查不出来）。
    """
    if delivery not in DELIVERIES:
        raise ValueError(f"未知的投递状态 {delivery!r}，只认 {DELIVERIES}")
    objects.ensure_schema(store)
    ts = now or objects._now()
    with objects.transaction(store) as db:
        rows = db.query("SELECT handoff_id FROM cs_handoff WHERE tenant_id=? AND handoff_id=?",
                        (tenant_id, handoff_id))
        if not rows:
            raise LookupError(f"转人工卡片不存在：tenant={tenant_id!r} handoff={handoff_id}")
        db.execute("UPDATE cs_handoff SET delivery=?, delivered_to=?, updated_at=?"
                   " WHERE tenant_id=? AND handoff_id=?",
                   (delivery, delivered_to or "", ts, tenant_id, handoff_id))


def list_handoffs(store: Any, tenant_id: str, *,
                  delivery: str | None = None) -> list[tuple[HandoffCard, str]]:
    """本租户的转人工卡片 ``[(卡片, 投递状态)]``，按落库先后；可按投递状态过滤。"""
    if delivery is not None and delivery not in DELIVERIES:
        raise ValueError(f"未知的投递状态 {delivery!r}，只认 {DELIVERIES}")
    objects.ensure_schema(store)
    sql = "SELECT card_json, delivery FROM cs_handoff WHERE tenant_id=?"
    params: tuple = (tenant_id,)
    if delivery is not None:
        sql += " AND delivery=?"
        params += (delivery,)
    sql += " ORDER BY created_at, handoff_id"
    return [(HandoffCard.from_json(json.loads(r["card_json"])), str(r["delivery"]))
            for r in objects.query(store, sql, params)]


# ------------------------------------------------------------------ 后置校验
def record_reply_rejected(store: Any, conv: Conversation, *, turn_id: str,
                          check: CheckResult) -> None:
    """后置校验拦下一版回复时落一条 ``CsReplyRejected``：只落违例种类、计数与摘要。

    ``Violation.detail`` 是自由文本（可能引着被拦的那句回复），所以只落它的摘要。
    ``check.ok`` 为真时没有东西可拒，抛 ``ValueError``。
    """
    if check.ok:
        raise ValueError("check.ok 为真：这版回复没有被拦，不该落 CsReplyRejected")
    objects.ensure_schema(store)
    kinds = _violation_kinds(check)
    _emit(store, conversation_id=conv.conversation_id, turn_id=turn_id,
          event_type=EVENT_REPLY_REJECTED, reason=",".join(kinds),
          detail={"violation_kinds": kinds,
                  "violation_count": len(check.violations),
                  "violation_digests": [text_digest(v.detail) for v in check.violations]})
