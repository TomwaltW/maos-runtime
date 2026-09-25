"""复杂投诉的圆桌会诊卡（p14 · T178，review/p14-cs-contracts.md §2「T178」）。

一轮转人工、原因在 ``types.CONFERENCE_REASONS`` 里（投诉 / 情绪激烈 / 赔偿 / 退款预检卡）时，
router 在投递转人工卡片**之后**再出一张会诊卡，投到同一个内部房间。

## 编排，不是自由转交

四个座次（:data:`SEATS`）按固定顺序各说一段，每座只读库里的事实：

1. ``intake``：槽位全集、缺什么、本会话几轮、语种（cs_slot / cs_conversation / cs_turn_ext）；
2. ``order``：本会话的观察与查单结果（cs_observation / cs_turn_ext.lookup_outcome）；没查过写「未查单」；
3. ``policy``：本会话引用过的话术（cs_turn.draft_json 的 citations）、退款桥那行的
   decision / rule_ref / refused_why，有 command_line 就原样带上（cs_refund_bridge）；
4. ``risk``：原因、情绪槽、连续兜底、追问次数、赔偿 / 曝光类说法、被后置校验拦下的次数
   （cs_turn / cs_turn_ext / cs_slot / event_log 的 CsReplyRejected）。

``recommendation`` 由 :func:`recommend_from_flags` —— 一张**纯函数决策表** —— 从四座的 flags 得出。

## 红线

* **零模型、零工具、不写 cs_ 表**：本模块只 SELECT，唯一的写是 event_log 一行 ``CsConferenceHeld``
  （plan_id ``cs:<会话>``、task_id 本轮、trace_id ``""``）。
* detail 只放 ``{handoff_id, reason, seats:[{seat, basis_refs, flags}], recommendation,
  open_question_count}``：不放 summary 原文、客户原文、external_userid、query_key、订单号。
* **只对内**：客户那一轮的回话一个字不变（router 在回话定稿之后才调这里）。
* :func:`convene` **永不抛**：某一座读库失败，那一座降级成 ``seat_error``（摘要只写异常类名），
  其余座照出；落 event_log 失败只记日志，卡照样返回（DECISIONS task-t178）。
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable

from maos.domain.cs import objects
from maos.domain.cs.ports import (
    LANG_EN,
    LANG_ZH,
    LOOKUP_AMENDED,
    LOOKUP_MISCONFIGURED,
    LOOKUP_NOT_FOUND,
    LOOKUP_PLATFORM_ERROR,
    LOOKUP_UNMAPPED,
    ORDER_STATUS_WORDING,
    SLOT_EMOTION,
    SLOT_KEYS,
    SLOT_ORDER_NO,
    SLOT_PROBLEM,
    EMOTION_ANGRY,
)
from maos.domain.cs.types import (
    BASIS_KB,
    BASIS_OBS,
    CONFERENCE_REASONS,
    EVENT_CONFERENCE_HELD,
    EVENT_REPLY_REJECTED,
    HANDOFF_ANGER,
    HANDOFF_COMPENSATION,
    HANDOFF_COMPLAINT,
    INTENT_COMPENSATION,
    ROUTE_CLARIFY,
    ROUTE_FALLBACK,
    ROUTE_HANDOFF,
    ROUTE_SILENT,
    DeskResult,
    plan_id_for,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 契约面（§2 T178，名字与签名逐字照契约）
# ---------------------------------------------------------------------------
SEATS = ("intake", "order", "policy", "risk")
RECOMMENDATIONS = ("send_refund_command", "supervisor_review", "callback_soothe", "verify_identity",
                   "manual_lookup", "standard_followup")


@dataclass(frozen=True)
class SeatFinding:
    seat: str                 # 固定座次之一：SEATS
    summary: str              # 内部可读的一两句（中文）；只用库里的事实，不推测
    basis_refs: tuple[str, ...]   # obs:<id> / kb:<doc_id> / turn:<turn_id> / bridge:<bridge_id> / slot:<key>
    flags: tuple[str, ...] = ()   # 该座的风险 / 待补标记（枚举见 SEAT_FLAGS）


@dataclass(frozen=True)
class ConferenceCard:
    tenant_id: str
    conversation_id: str
    turn_id: str
    handoff_id: str
    reason: str               # ∈ CONFERENCE_REASONS
    seats: tuple[SeatFinding, ...]    # 恰好按 SEATS 顺序，每座一条
    recommendation: str       # 建议动作（RECOMMENDATIONS 之一）
    open_questions: tuple[str, ...]   # 人工接手前要补问的


# ---------------------------------------------------------------------------
# basis_ref 前缀（obs: / kb: 与 types 同源）
# ---------------------------------------------------------------------------
REF_OBS = BASIS_OBS
REF_KB = BASIS_KB
REF_TURN = "turn:"
REF_BRIDGE = "bridge:"
REF_SLOT = "slot:"

# ---------------------------------------------------------------------------
# flags 枚举（每座各自的一组；seat_error 四座通用）
# ---------------------------------------------------------------------------
FLAG_SEAT_ERROR = "seat_error"            # 本座读库失败：降级，摘要只写异常类名

FLAG_ORDER_NO_MISSING = "order_no_missing"    # intake：会话里没有订单号槽位
FLAG_PROBLEM_MISSING = "problem_missing"      # intake：没有问题描述槽位
FLAG_LANG_EN = "lang_en"                      # intake：本轮英文

FLAG_NOT_LOOKED_UP = "not_looked_up"          # order：本会话一次查单都没走到
FLAG_OBSERVED = "observed"                    # order：本会话有成功查单的观察行
FLAG_LOOKUP_FAILED = "lookup_failed"          # order：查不到 / 系统没配 / 平台出错
FLAG_STATUS_UNMAPPED = "status_unmapped"      # order：amended / 平台状态不映射
FLAG_ORDER_UNVERIFIED = "order_unverified"    # order：当前 order_no 槽位的单号没过身份核验（按单号认）

FLAG_SCRIPTS_CITED = "scripts_cited"          # policy：本会话引用过话术
FLAG_REFUND_COMMAND_READY = "refund_command_ready"  # policy：退款桥 ok 且有采纳命令
FLAG_REFUND_REFUSED = "refund_refused"        # policy：退款桥预检未通过

FLAG_COMPLAINT = "complaint_escalation"       # risk：原因是投诉
FLAG_ANGER = "anger"                          # risk：原因是情绪激烈
FLAG_COMPENSATION = "compensation_claim"      # risk：原因 / 意图 / 说法里有赔偿、补偿
FLAG_EXPOSURE = "exposure_threat"             # risk：说法里有曝光 / 12315 / 起诉 / 律师一类
FLAG_EMOTION_ANGRY = "emotion_angry"          # risk：情绪槽 = angry
FLAG_FALLBACK_STREAK = "fallback_streak"      # risk：本轮之前紧挨着有兜底轮
FLAG_CLARIFY_ASKED = "clarify_asked"          # risk：本会话被追问过槽位
FLAG_REPLY_REJECTED = "reply_rejected"        # risk：本会话有回复被后置校验拦下

SEAT_FLAGS: dict[str, tuple[str, ...]] = {
    "intake": (FLAG_SEAT_ERROR, FLAG_ORDER_NO_MISSING, FLAG_PROBLEM_MISSING, FLAG_LANG_EN),
    "order": (FLAG_SEAT_ERROR, FLAG_NOT_LOOKED_UP, FLAG_OBSERVED, FLAG_LOOKUP_FAILED,
              FLAG_STATUS_UNMAPPED, FLAG_ORDER_UNVERIFIED),
    "policy": (FLAG_SEAT_ERROR, FLAG_SCRIPTS_CITED, FLAG_REFUND_COMMAND_READY, FLAG_REFUND_REFUSED),
    "risk": (FLAG_SEAT_ERROR, FLAG_COMPLAINT, FLAG_ANGER, FLAG_COMPENSATION, FLAG_EXPOSURE,
             FLAG_EMOTION_ANGRY, FLAG_FALLBACK_STREAK, FLAG_CLARIFY_ASKED, FLAG_REPLY_REJECTED),
}

#: 赔偿 / 补偿类说法、曝光 / 升级到外部渠道类说法（NFKC + 小写、标点与空白一律换成空格后按子串认；
#: 英文整词两侧带空格，所以「sue!」「court.」也认得）。只决定 flag 与 ``turn:<id>`` 引用，原文不出本模块。
COMPENSATION_WORDS = ("赔偿", "补偿", "赔钱", "赔我", "compensat")
EXPOSURE_WORDS = ("曝光", "媒体", "12315", "消协", "起诉", "律师", "法院", "报警", "差评",
                  "lawyer", " sue ", " court ", " media ")

_FAILED_OUTCOMES = frozenset({LOOKUP_NOT_FOUND, LOOKUP_MISCONFIGURED, LOOKUP_PLATFORM_ERROR})
_UNMAPPED_OUTCOMES = frozenset({LOOKUP_AMENDED, LOOKUP_UNMAPPED})

_RECOMMENDATION_CN = {
    "send_refund_command": "采纳退款桥命令（以内部同事自己的名义发出）",
    "supervisor_review": "主管复核（赔偿 / 曝光 / 法律途径类诉求）",
    "callback_soothe": "人工回访安抚",
    "verify_identity": "先核验客户身份与订单绑定",
    "manual_lookup": "人工查单核对",
    "standard_followup": "按常规流程跟进",
}


# ---------------------------------------------------------------------------
# 决策表（纯函数）
# ---------------------------------------------------------------------------
def recommend_from_flags(flags: Iterable[str]) -> str:
    """四座 flags 的并集 → 建议动作。**自上而下第一条命中即返回**（测试逐格钉）：

    1. 赔偿诉求 / 曝光等外部升级说法 → ``supervisor_review``（金额与外部升级只有主管能定）；
    2. 任何一座读库失败 → ``manual_lookup``（事实不全，人工自己核）；
    3. 当前报的单号没过身份核验 → ``verify_identity``（压过退款命令：退款桥可能是旧单的，
       客户现在报的单没核验前不据此发命令；复核 L3-2）；
    4. 退款桥 ok 且有采纳命令 → ``send_refund_command``；
    5. 查单失败 / 状态不映射 / 预检未通过 → ``manual_lookup``；
    6. 情绪激烈 / 情绪槽 angry / 本轮前紧挨着兜底 → ``callback_soothe``；
    7. 其余 → ``standard_followup``。
    """
    f = frozenset(flags)
    if f & {FLAG_COMPENSATION, FLAG_EXPOSURE}:
        return "supervisor_review"
    if FLAG_SEAT_ERROR in f:
        return "manual_lookup"
    if FLAG_ORDER_UNVERIFIED in f:
        return "verify_identity"
    if FLAG_REFUND_COMMAND_READY in f:
        return "send_refund_command"
    if f & {FLAG_LOOKUP_FAILED, FLAG_STATUS_UNMAPPED, FLAG_REFUND_REFUSED}:
        return "manual_lookup"
    if f & {FLAG_ANGER, FLAG_EMOTION_ANGRY, FLAG_FALLBACK_STREAK}:
        return "callback_soothe"
    return "standard_followup"


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def should_convene(result: DeskResult) -> bool:
    """route=handoff 且 handoff_reason ∈ CONFERENCE_REASONS 且有卡片。永不抛。"""
    try:
        return (getattr(result, "route", "") == ROUTE_HANDOFF
                and getattr(result, "handoff_reason", "") in CONFERENCE_REASONS
                and getattr(result, "handoff", None) is not None)
    except Exception:                                   # noqa: BLE001 —— 怪对象也只是「不开会」
        return False


@dataclass
class _Facts:
    """一次会诊读回的库内事实（只读）。"""
    tenant_id: str
    conversation_id: str
    turn_id: str
    turns: list[dict]
    ext: dict[str, dict]
    slots: dict[str, str]
    slot_turns: dict[str, str]      # 槽位 → 最后写它的轮次 id（cs_slot.turn_id）


def convene(store: Any, result: DeskResult, *, now: str) -> ConferenceCard:
    """纯读 cs_ 表 + 落一行 CsConferenceHeld；**永不抛**，坏数据给降级卡。

    ``now`` 是会诊时刻（调用方给；event_log 的 created_at 由 store 自己盖，detail 不收时间键）。
    """
    tenant_id = str(getattr(result, "tenant_id", "") or "")
    conversation_id = str(getattr(result, "conversation_id", "") or "")
    turn_id = str(getattr(result, "turn_id", "") or "")
    reason = str(getattr(result, "handoff_reason", "") or "")
    card = getattr(result, "handoff", None)
    handoff_id = str(getattr(card, "handoff_id", "") or "") or turn_id

    facts: _Facts | None = None
    facts_error = ""
    try:
        facts = _read_facts(store, tenant_id, conversation_id, turn_id)
    except Exception as exc:                            # noqa: BLE001 —— 降级，不抛
        facts_error = type(exc).__name__
        log.warning("会诊读库失败（轮次 %s）：%s", turn_id, facts_error)

    seats: list[SeatFinding] = []
    questions: list[str] = []
    for seat, builder in (("intake", _intake), ("order", _order), ("policy", _policy),
                          ("risk", _risk)):
        try:
            if facts is None:
                raise _FactsUnavailable(facts_error or "FactsUnavailable")
            finding, asks = builder(store, facts, reason)
        except Exception as exc:                        # noqa: BLE001 —— 本座降级，其余照出
            kind = str(exc) if isinstance(exc, _FactsUnavailable) else type(exc).__name__
            log.warning("会诊 %s 座降级（轮次 %s）：%s", seat, turn_id, kind)
            finding = SeatFinding(seat=seat, summary=f"本座读库失败（{kind}），请人工核对。",
                                  basis_refs=(), flags=(FLAG_SEAT_ERROR,))
            asks = (f"{seat} 座的事实没读全，请人工核对",)
        seats.append(finding)
        questions.extend(asks)

    recommendation = recommend_from_flags(f for s in seats for f in s.flags)
    conf = ConferenceCard(tenant_id=tenant_id, conversation_id=conversation_id, turn_id=turn_id,
                          handoff_id=handoff_id, reason=reason, seats=tuple(seats),
                          recommendation=recommendation,
                          open_questions=tuple(dict.fromkeys(questions)))
    _emit(store, conf)
    return conf


def render_conference_text(card: ConferenceCard) -> str:
    """投递到内部房间的文本：头一行原因与建议，四座各一段，末尾待补问。永不抛。"""
    try:
        lines = [f"【圆桌会诊】会话 {card.conversation_id} · 轮次 {card.turn_id}",
                 f"原因：{card.reason}　建议：{card.recommendation}"
                 f"（{_RECOMMENDATION_CN.get(card.recommendation, '')}）"]
        for s in card.seats:
            flags = "、".join(s.flags) if s.flags else "无"
            refs = " ".join(s.basis_refs) if s.basis_refs else "无"
            lines.append(f"[{s.seat}] {s.summary}")
            lines.append(f"    标记：{flags}　依据：{refs}")
        if card.open_questions:
            lines.append("接手前待补：")
            lines.extend(f"  {i}. {q}" for i, q in enumerate(card.open_questions, start=1))
        else:
            lines.append("接手前待补：无")
        return "\n".join(lines)
    except Exception as exc:                            # noqa: BLE001
        log.warning("会诊卡渲染失败：%s", type(exc).__name__)
        return "【圆桌会诊】卡片渲染失败，请在库里查 CsConferenceHeld。"


# ---------------------------------------------------------------------------
# 读库（只 SELECT；不调 ensure_schema —— 那是写 DDL）
# ---------------------------------------------------------------------------
class _FactsUnavailable(Exception):
    pass


def _read_facts(store: Any, tenant_id: str, conversation_id: str, turn_id: str) -> _Facts:
    if not conversation_id or not turn_id:
        raise _FactsUnavailable("MissingIds")
    turns = objects.query(
        store,
        "SELECT turn_id, seq, inbound_text, route, intent, handoff_reason, draft_json"
        " FROM cs_turn WHERE tenant_id=? AND conversation_id=? ORDER BY seq",
        (tenant_id, conversation_id))
    if not any(str(t["turn_id"]) == turn_id for t in turns):
        raise _FactsUnavailable("TurnNotRecorded")
    ext_rows = objects.query(
        store,
        "SELECT turn_id, lang, lookup_outcome, ask_slot, ask_count FROM cs_turn_ext"
        " WHERE tenant_id=? AND conversation_id=?", (tenant_id, conversation_id))
    slot_rows = objects.query(
        store, "SELECT slot_key, value, turn_id FROM cs_slot WHERE tenant_id=? AND conversation_id=?",
        (tenant_id, conversation_id))
    got = {str(r["slot_key"]): str(r["value"]) for r in slot_rows}
    by = {str(r["slot_key"]): str(r["turn_id"] or "") for r in slot_rows}
    return _Facts(tenant_id=tenant_id, conversation_id=conversation_id, turn_id=turn_id,
                  turns=[dict(t) for t in turns],
                  ext={str(r["turn_id"]): dict(r) for r in ext_rows},
                  slots={k: got[k] for k in SLOT_KEYS if k in got},
                  slot_turns={k: by[k] for k in SLOT_KEYS if k in by})


def _turns_upto(facts: _Facts) -> list[dict]:
    """本轮及之前的轮（按 seq）。本轮之后的轮（并发写进来的）不算。"""
    out: list[dict] = []
    for t in facts.turns:
        out.append(t)
        if str(t["turn_id"]) == facts.turn_id:
            break
    return out


def _norm(text: Any) -> str:
    """NFKC + 小写；标点 / 符号 / 空白一律换成一个空格，两端再各补一个（英文词表按整词认）。"""
    raw = unicodedata.normalize("NFKC", str(text or "")).lower()
    return " " + "".join(" " if unicodedata.category(ch)[0] in "PSZC" else ch
                         for ch in raw) + " "


# ------------------------------------------------------------------ intake
def _intake(store: Any, facts: _Facts, reason: str) -> tuple[SeatFinding, tuple[str, ...]]:
    turns = _turns_upto(facts)
    lang = str((facts.ext.get(facts.turn_id) or {}).get("lang") or LANG_ZH)
    have = [k for k in SLOT_KEYS if k in facts.slots]
    missing = [k for k in SLOT_KEYS if k not in facts.slots]
    flags: list[str] = []
    asks: list[str] = []
    if SLOT_ORDER_NO not in facts.slots:
        flags.append(FLAG_ORDER_NO_MISSING)
        asks.append("补问订单号")
    if SLOT_PROBLEM not in facts.slots:
        flags.append(FLAG_PROBLEM_MISSING)
        asks.append("补问具体问题（商品与现象）")
    if lang == LANG_EN:
        flags.append(FLAG_LANG_EN)
    shown = "、".join(f"{k}={facts.slots[k]}" for k in have) or "无"
    summary = (f"本会话共 {len(turns)} 轮，本轮语种 {lang}；已有槽位：{shown}；"
               f"缺：{'、'.join(missing) or '无'}。")
    refs = tuple(REF_SLOT + k for k in have) + (REF_TURN + facts.turn_id,)
    return SeatFinding("intake", summary, refs, tuple(flags)), tuple(asks)


# ------------------------------------------------------------------ order
def _current_order_verified(facts: _Facts, turns: list[dict]) -> bool:
    """**当前** order_no 槽位那个单号是否过过身份核验（按单号认，不按会话认）。

    查单只在绑定核验通过后才发生，所以 ``lookup_outcome`` 非空即核验过。算「这个单号」核验过：
    本轮及之前有一轮 lookup_outcome 非空，且那一轮是最后写 order_no 槽位的轮、或它的客户原文里
    出现了这个单号（NFKC + 小写比，**整号**：两侧不许紧挨字母或数字，复核 L2-1 —— 查过 123456
    不算 12345 核验过）。客户先报 A 查过、后改报 B 没过核验 → B 算未核验。
    """
    value = _norm(facts.slots.get(SLOT_ORDER_NO, "")).strip()
    whole = (re.compile(r"(?<![0-9a-z])" + re.escape(value) + r"(?![0-9a-z])")
             if value else None)
    writer = facts.slot_turns.get(SLOT_ORDER_NO, "")
    for t in turns:
        tid = str(t["turn_id"])
        if not str((facts.ext.get(tid) or {}).get("lookup_outcome") or ""):
            continue
        if (writer and tid == writer) or (whole and whole.search(_norm(t["inbound_text"]))):
            return True
    return False


def _order(store: Any, facts: _Facts, reason: str) -> tuple[SeatFinding, tuple[str, ...]]:
    turns = _turns_upto(facts)
    looked = [(str(t["turn_id"]), str((facts.ext.get(str(t["turn_id"])) or {})
                                      .get("lookup_outcome") or "")) for t in turns]
    looked = [(tid, out) for tid, out in looked if out]
    obs_rows = objects.query(
        store,
        "SELECT observation_id, turn_id, status FROM cs_observation"
        " WHERE tenant_id=? AND conversation_id=? ORDER BY observation_id",
        (facts.tenant_id, facts.conversation_id))
    upto = {str(t["turn_id"]) for t in turns}
    obs_rows = [r for r in obs_rows if str(r["turn_id"]) in upto]

    flags: list[str] = []
    asks: list[str] = []
    refs: list[str] = []
    parts: list[str] = []
    unverified = SLOT_ORDER_NO in facts.slots and not _current_order_verified(facts, turns)
    if not looked:
        flags.append(FLAG_NOT_LOOKED_UP)
        parts.append("未查单")
    if unverified:
        flags.append(FLAG_ORDER_UNVERIFIED)
        parts.append("客户当前报的订单号没有经过身份核验的查单")
        asks.append("核验客户与所报订单号的绑定（未核验前不要据此谈订单）")
    if looked:
        counts: dict[str, int] = {}
        for tid, out in looked:
            counts[out] = counts.get(out, 0) + 1
            refs.append(REF_TURN + tid)
        parts.append("查单 " + "、".join(f"{k}×{v}" for k, v in counts.items()))
        failed = sorted({o for _, o in looked if o in _FAILED_OUTCOMES})
        unmapped = sorted({o for _, o in looked if o in _UNMAPPED_OUTCOMES})
        if failed:
            flags.append(FLAG_LOOKUP_FAILED)
            asks.append(f"查单未成功（{'、'.join(failed)}），请人工在订单系统核对")
        if unmapped:
            flags.append(FLAG_STATUS_UNMAPPED)
            asks.append(f"平台状态说不准（{'、'.join(unmapped)}），请人工在订单系统核对")
    if obs_rows:
        flags.append(FLAG_OBSERVED)
        wording = ORDER_STATUS_WORDING[LANG_ZH]
        said = []
        for r in obs_rows:
            oid = str(r["observation_id"])
            refs.append(REF_OBS + oid)
            said.append(f"{wording.get(str(r['status']), str(r['status']))}（{oid}）")
        parts.append("观察：" + "；".join(said))
    summary = "；".join(parts) + "。"
    return SeatFinding("order", summary, tuple(dict.fromkeys(refs)), tuple(flags)), tuple(asks)


# ------------------------------------------------------------------ policy
def _citations_of(draft_json: Any) -> list[str]:
    try:
        d = json.loads(draft_json or "{}")
    except (TypeError, ValueError):
        return []
    cites = d.get("citations") if isinstance(d, dict) else None
    if not isinstance(cites, (list, tuple)):            # 字符串不许按字符拆成「引用」
        return []
    return [c for c in cites if isinstance(c, str) and c]


def _policy(store: Any, facts: _Facts, reason: str) -> tuple[SeatFinding, tuple[str, ...]]:
    turns = _turns_upto(facts)
    cited: list[str] = []
    for t in turns:
        cited.extend(_citations_of(t.get("draft_json")))
    cited = list(dict.fromkeys(cited))
    seq = {str(t["turn_id"]): i for i, t in enumerate(turns)}
    bridges = [dict(r) for r in objects.query(
        store,
        "SELECT bridge_id, turn_id, ok, decision, rule_ref, reason_code, command_line, refused_why,"
        " created_at FROM cs_refund_bridge WHERE tenant_id=? AND conversation_id=?",
        (facts.tenant_id, facts.conversation_id)) if str(r["turn_id"]) in seq]
    # 「最近的一行」按轮次先后排（同一时钟下 created_at 会并列），再按 created_at。
    bridges.sort(key=lambda b: (seq[str(b["turn_id"])], str(b["created_at"] or "")))

    flags: list[str] = []
    asks: list[str] = []
    refs = [REF_KB + c for c in cited]
    parts = [("引用过话术：" + "、".join(cited)) if cited else "本会话没有引用话术"]
    if cited:
        flags.append(FLAG_SCRIPTS_CITED)
    if not bridges:
        parts.append("无退款桥")
    else:
        b = bridges[-1]                                 # 最近的一行
        refs.append(REF_BRIDGE + str(b["bridge_id"]))
        head = (f"退款桥 {b['bridge_id']}：decision={b['decision'] or '（空）'}、"
                f"rule_ref={b['rule_ref'] or '（空）'}")
        if int(b["ok"] or 0) and str(b["command_line"] or ""):
            flags.append(FLAG_REFUND_COMMAND_READY)
            parts.append(f"{head}；采纳命令：{b['command_line']}")
        else:
            flags.append(FLAG_REFUND_REFUSED)
            why = str(b["refused_why"] or "") or "（未说明）"
            parts.append(f"{head}；预检未通过：{why}")
            asks.append(f"退款预检未通过（{why}），核对后再定")
    summary = "；".join(parts) + "。"
    return SeatFinding("policy", summary, tuple(refs), tuple(flags)), tuple(asks)


# ------------------------------------------------------------------ risk
def _risk(store: Any, facts: _Facts, reason: str) -> tuple[SeatFinding, tuple[str, ...]]:
    turns = _turns_upto(facts)
    flags: list[str] = []
    asks: list[str] = []
    refs: list[str] = [REF_TURN + facts.turn_id]
    parts = [f"原因 {reason or '（空）'}"]

    if reason == HANDOFF_COMPLAINT:
        flags.append(FLAG_COMPLAINT)
    if reason == HANDOFF_ANGER:
        flags.append(FLAG_ANGER)

    emotion = facts.slots.get(SLOT_EMOTION, "")
    parts.append(f"情绪槽 {emotion or '无'}")
    if emotion:
        refs.append(REF_SLOT + SLOT_EMOTION)
    if emotion == EMOTION_ANGRY:
        flags.append(FLAG_EMOTION_ANGRY)

    # 本轮之前紧挨着的兜底轮数（clarify / silent 不动兜底计数，跳过；口径同 conversation）。
    streak = 0
    for t in reversed(turns[:-1]):
        route = str(t["route"])
        if route == ROUTE_FALLBACK:
            streak += 1
        elif route in (ROUTE_CLARIFY, ROUTE_SILENT):
            continue
        else:
            break
    parts.append(f"本轮前连续兜底 {streak} 轮")
    if streak:
        flags.append(FLAG_FALLBACK_STREAK)

    asked = sum(1 for t in turns
                if str((facts.ext.get(str(t["turn_id"])) or {}).get("ask_slot") or ""))
    parts.append(f"追问 {asked} 次")
    if asked:
        flags.append(FLAG_CLARIFY_ASKED)

    comp_turns = [str(t["turn_id"]) for t in turns
                  if str(t["intent"]) == INTENT_COMPENSATION
                  or any(w in _norm(t["inbound_text"]) for w in COMPENSATION_WORDS)]
    expo_turns = [str(t["turn_id"]) for t in turns
                  if any(w in _norm(t["inbound_text"]) for w in EXPOSURE_WORDS)]
    if reason == HANDOFF_COMPENSATION or comp_turns:
        flags.append(FLAG_COMPENSATION)
        parts.append("出现赔偿 / 补偿诉求")
        asks.append("客户提出赔偿 / 补偿诉求：金额只能由人工核定，不要在对话里承诺")
    if expo_turns:
        flags.append(FLAG_EXPOSURE)
        parts.append("出现曝光 / 外部投诉 / 法律途径类说法")
        asks.append("客户提到曝光 / 外部投诉渠道 / 法律途径：先由主管评估再回复")
    refs.extend(REF_TURN + tid for tid in comp_turns + expo_turns)

    # 与其余座同一口径：只数本轮及之前的轮（task_id = 轮次 id；复核 L2-3）。
    upto = {str(t["turn_id"]) for t in turns}
    rejected = sum(1 for r in store.list_event_log(plan_id_for(facts.conversation_id)) or ()
                   if r.get("event_type") == EVENT_REPLY_REJECTED
                   and str(r.get("task_id") or "") in upto)
    parts.append(f"回复被后置校验拦下 {rejected} 次")
    if rejected:
        flags.append(FLAG_REPLY_REJECTED)
    summary = "；".join(parts) + "。"
    return SeatFinding("risk", summary, tuple(dict.fromkeys(refs)), tuple(flags)), tuple(asks)


# ---------------------------------------------------------------------------
# event_log
# ---------------------------------------------------------------------------
def conference_detail(card: ConferenceCard) -> dict:
    """落 event_log 的 detail：只放契约列的键，不放 summary 与 open_questions 原文。"""
    return {
        "handoff_id": card.handoff_id,
        "reason": card.reason,
        "seats": [{"seat": s.seat, "basis_refs": list(s.basis_refs), "flags": list(s.flags)}
                  for s in card.seats],
        "recommendation": card.recommendation,
        "open_question_count": len(card.open_questions),
    }


def _emit(store: Any, card: ConferenceCard) -> None:
    if not card.conversation_id or not card.turn_id:
        log.warning("会诊卡缺会话或轮次 id，不落 event_log")
        return
    try:
        store.append_event_log({
            "event_id": "",
            "trace_id": "",
            "plan_id": plan_id_for(card.conversation_id),
            "task_id": card.turn_id,
            "event_type": EVENT_CONFERENCE_HELD,
            "from_state": None,
            "to_state": None,
            "reason": card.reason if card.reason in CONFERENCE_REASONS else "",
            "detail": conference_detail(card),
        })
    except Exception as exc:                            # noqa: BLE001 —— 只记日志
        log.warning("会诊卡落 event_log 失败（轮次 %s）：%s", card.turn_id, type(exc).__name__)
