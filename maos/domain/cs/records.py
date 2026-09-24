"""p13 的四类会话记录 —— 观察、订单绑定、槽位、轮次扩展、退款桥（p13 契约 §1.3 / §1.4 T171）。

## 口径

1. **观察只来自成功的查单**：``record_observation`` 只收 ``outcome == ok`` 且状态在
   ``ports.ORDER_STATUS_WORDING['zh']`` 的键里（paid / shipped / cancelled）的结果；amended、
   平台不映射、查不到一律 ``ValueError`` —— 那些情况前台说不出状态，也就不该有「撑得起状态」的行。
   观察 id = ``ports.observation_id_for(turn_id, n)``，n 在持锁事务里取（本轮已有条数 + 1）。
2. **绑定只从内部路径写**：本模块的 ``upsert_binding`` / ``load_bindings_file`` 由装配处
   （启动种子文件）与测试调用；没有任何从 ``InboundMessage`` 到这里的路。
3. **R5**：这里的写入**一律不落 event_log**（本期不新增事件类型）。订单号、query_key、
   槽位值、预检的命令行与拒绝原因都只住在 cs_ 表里；那一轮的 ``CsTurnRecorded`` 只带
   ``observation_count`` / ``slot_count`` 两个计数（``conversation.record_turn`` 从库里读）。
4. 枚举在进库前先校验（``ValueError``），库上的 CHECK 是最后一道。
5. 会话进度（槽位、追问次数）是会话对象自己的字段，不是 Task 状态（铁律 9）。

时间戳一律 Python 侧给（``now=`` 可注入），口径同 ``conversation.py``。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from maos.domain.cs import objects
from maos.domain.cs.conversation import Conversation
from maos.domain.cs.identity import normalize_display_no
from maos.domain.cs.ports import (
    BINDING_SEED,
    BINDING_SOURCES,
    EMOTION_VALUES,
    LANG_ZH,
    LANGS,
    LOOKUP_OK,
    LOOKUP_OUTCOMES,
    OBS_ORDER_LOOKUP,
    ORDER_STATUS_WORDING,
    REQUEST_VALUES,
    SLOT_EMOTION,
    SLOT_KEYS,
    SLOT_REQUEST,
    SLOT_SOURCES,
    Binding,
    LookupResult,
    PrecheckResult,
    observation_id_for,
)

_OBS_COLUMNS = ("tenant_id", "observation_id", "conversation_id", "turn_id", "kind",
                "system_name", "query_key", "status", "version", "updated_at", "observed_at")


def _check_turn_of(conv: Conversation, turn_id: str) -> None:
    """``turn_id`` 必须是这段会话的轮次（``types.turn_id_for`` 的形状 ``<会话>-t<序号>``）。

    观察、槽位、扩展、桥都按轮次挂在会话上；挂错会话的行在「本轮」读回里会串号。
    """
    prefix = conv.conversation_id + "-t"
    if not turn_id.startswith(prefix) or not turn_id[len(prefix):].isdigit():
        raise ValueError(f"turn_id {turn_id!r} 不是会话 {conv.conversation_id} 的轮次")


# ------------------------------------------------------------------ 观察
def record_observation(store: Any, conv: Conversation, *, turn_id: str, result: LookupResult,
                       now: str | None = None) -> str:
    """落一条本轮观察，返回 ``observation_id``（本轮第 n 条，n 从 1 起）。

    只收 ``outcome == 'ok'`` 且 ``status`` 在中文措辞表的键里的结果，否则 ``ValueError``、
    一行不写。取号与插入在同一个持锁事务里：同进程并发落同一轮不重号。
    """
    if result.outcome != LOOKUP_OK:
        raise ValueError(f"只有成功的查单才落观察：outcome={result.outcome!r}（要 {LOOKUP_OK!r}）")
    wording = ORDER_STATUS_WORDING[LANG_ZH]
    if result.status not in wording:
        raise ValueError(f"状态 {result.status!r} 不在措辞表里（只认 {sorted(wording)}）："
                         "说不出的状态不落观察，前台转人工")
    _check_turn_of(conv, turn_id)

    objects.ensure_schema(store)
    ts = now or objects._now()
    with objects.transaction(store) as db:
        rows = db.query("SELECT COUNT(*) AS n FROM cs_observation WHERE tenant_id=? AND turn_id=?",
                        (conv.tenant_id, turn_id))
        observation_id = observation_id_for(turn_id, int(rows[0]["n"]) + 1)
        db.execute(
            "INSERT INTO cs_observation (tenant_id, observation_id, conversation_id, turn_id,"
            " kind, system_name, query_key, status, version, updated_at, observed_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (conv.tenant_id, observation_id, conv.conversation_id, turn_id, OBS_ORDER_LOOKUP,
             result.system_name, result.query_key, result.status, int(result.version),
             result.updated_at or "", ts))
    return observation_id


def _observation_rows(store: Any, conversation_id: str, turn_id: str) -> list[dict]:
    objects.ensure_schema(store)
    return objects.query(
        store,
        "SELECT " + ", ".join(_OBS_COLUMNS) + " FROM cs_observation"
        " WHERE conversation_id=? AND turn_id=? ORDER BY observation_id",
        (conversation_id, turn_id))


def turn_observation_ids(store: Any, *, conversation_id: str, turn_id: str) -> frozenset[str]:
    """本轮（这段会话的这一轮）已落的观察 id，从库里读回。别的轮、别的会话不算。"""
    return frozenset(str(r["observation_id"])
                     for r in _observation_rows(store, conversation_id, turn_id))


def observations_for_turn(store: Any, *, conversation_id: str,
                          turn_id: str) -> dict[str, dict]:
    """本轮的观察 ``{observation_id: 整行}``，从库里读回。别的轮、别的会话不算。"""
    return {str(r["observation_id"]): dict(r)
            for r in _observation_rows(store, conversation_id, turn_id)}


# ------------------------------------------------------------------ 绑定
_BINDING_REQUIRED = ("tenant_id", "channel", "external_userid", "display_no", "system_name",
                     "query_key")
_BINDING_OPTIONAL = ("source", "bound_at")


def _validated_binding(binding: Binding) -> Binding:
    """校验并规范化一条绑定：必填项非空、source 在 BINDING_SOURCES、单号取规范形。"""
    required = (("tenant_id", binding.tenant_id), ("channel", binding.channel),
                ("external_userid", binding.external_userid),
                ("display_no", binding.display_no), ("system_name", binding.system_name),
                ("query_key", binding.query_key))
    for name, value in required:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"绑定缺 {name}（必须是非空字符串）")
    if binding.source not in BINDING_SOURCES:
        raise ValueError(f"未知的绑定来源 {binding.source!r}，只认 {BINDING_SOURCES}")
    no = normalize_display_no(binding.display_no)
    if not no:
        raise ValueError("绑定的 display_no 规范化后为空")
    return Binding(tenant_id=binding.tenant_id, channel=binding.channel,
                   external_userid=binding.external_userid, display_no=no,
                   system_name=binding.system_name, query_key=binding.query_key,
                   source=binding.source, bound_at=binding.bound_at or "")


_UPSERT_BINDING = (
    "INSERT OR REPLACE INTO cs_order_binding (tenant_id, channel, external_userid, display_no,"
    " system_name, query_key, source, bound_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)


def _binding_params(b: Binding, ts: str) -> tuple:
    return (b.tenant_id, b.channel, b.external_userid, b.display_no, b.system_name,
            b.query_key, b.source, b.bound_at or ts)


def upsert_binding(store: Any, binding: Binding) -> None:
    """写一条绑定（同主键覆盖）。``source`` 必须在 ``BINDING_SOURCES``；单号按规范形存。

    ``bound_at`` 空就取当前时刻。只给内部路径用（种子文件、测试）。
    """
    b = _validated_binding(binding)
    objects.ensure_schema(store)
    objects.execute(store, _UPSERT_BINDING, _binding_params(b, objects._now()))


def load_bindings_file(store: Any, path: Any) -> int:
    """读种子文件 ``{"bindings": [{tenant_id, channel, external_userid, display_no,
    system_name, query_key, source?, bound_at?}]}``，全部校验通过后在一个事务里写入，返回条数。

    ``source`` 缺省 ``seed``。坏文件（读不了、不是 JSON、形状不对、某条缺键 / 多键 / 空值 /
    来源不认）一律抛带文件名的 ``ValueError``，**一条都不写**。报错只说第几条、哪个键，
    不回显值（值里是订单号与客户标识）。
    """
    label = str(path)
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"绑定种子文件读不了：{label}（{type(exc).__name__}）") from exc
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"绑定种子文件不是合法 JSON：{label}（第 {exc.lineno} 行）") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("bindings"), list):
        raise ValueError(f"绑定种子文件形状不对：{label}（顶层要是含 \"bindings\" 列表的对象）")

    allowed = set(_BINDING_REQUIRED) | set(_BINDING_OPTIONAL)
    parsed: list[Binding] = []
    for idx, entry in enumerate(doc["bindings"], start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"绑定种子文件第 {idx} 条不是对象：{label}")
        extra = sorted(set(entry) - allowed)
        if extra:
            raise ValueError(f"绑定种子文件第 {idx} 条有不认识的键 {extra}：{label}")
        for key in allowed:
            if key in entry and not isinstance(entry[key], str):
                raise ValueError(f"绑定种子文件第 {idx} 条的 {key} 不是字符串：{label}")
        try:
            parsed.append(_validated_binding(Binding(
                tenant_id=entry.get("tenant_id", ""), channel=entry.get("channel", ""),
                external_userid=entry.get("external_userid", ""),
                display_no=entry.get("display_no", ""),
                system_name=entry.get("system_name", ""), query_key=entry.get("query_key", ""),
                source=entry.get("source", BINDING_SEED), bound_at=entry.get("bound_at", ""))))
        except ValueError as exc:
            raise ValueError(f"绑定种子文件第 {idx} 条不合法：{label}：{exc}") from exc

    objects.ensure_schema(store)
    ts = objects._now()
    with objects.transaction(store) as db:
        for b in parsed:
            db.execute(_UPSERT_BINDING, _binding_params(b, ts))
    return len(parsed)


# ------------------------------------------------------------------ 槽位
#: 取值是闭集的槽位。其余三个（order_no / product / problem）是自由文本。
_CLOSED_SLOT_VALUES: dict[str, tuple[str, ...]] = {
    SLOT_REQUEST: REQUEST_VALUES,
    SLOT_EMOTION: EMOTION_VALUES,
}


def get_slots(store: Any, tenant_id: str, conversation_id: str) -> dict[str, str]:
    """本会话当前的槽位 ``{槽位: 值}``，按 ``SLOT_KEYS`` 的顺序排；没有的槽位不出现。"""
    objects.ensure_schema(store)
    rows = objects.query(store, "SELECT slot_key, value FROM cs_slot"
                                " WHERE tenant_id=? AND conversation_id=?",
                         (tenant_id, conversation_id))
    got = {str(r["slot_key"]): str(r["value"]) for r in rows}
    return {k: got[k] for k in SLOT_KEYS if k in got}


def set_slot(store: Any, conv: Conversation, *, key: str, value: str, turn_id: str,
             source: str, now: str | None = None) -> None:
    """写一个槽位（跨轮累积，同一槽位新值覆盖旧值、turn_id 记这一轮）。

    ``key`` 必须在 ``SLOT_KEYS``、``source`` 在 ``SLOT_SOURCES``；request / emotion 的值必须在
    各自的闭集里；值不许是空串或纯空白（「没抽到」就别写）。
    """
    if key not in SLOT_KEYS:
        raise ValueError(f"未知的槽位 {key!r}，只认 {SLOT_KEYS}")
    if source not in SLOT_SOURCES:
        raise ValueError(f"未知的槽位来源 {source!r}，只认 {SLOT_SOURCES}")
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"槽位 {key} 的值必须是非空字符串")
    closed = _CLOSED_SLOT_VALUES.get(key)
    if closed is not None and value not in closed:
        raise ValueError(f"槽位 {key} 的值只认 {closed}")
    _check_turn_of(conv, turn_id)

    objects.ensure_schema(store)
    ts = now or objects._now()
    objects.execute(
        store,
        "INSERT OR REPLACE INTO cs_slot (tenant_id, conversation_id, slot_key, value, turn_id,"
        " source, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (conv.tenant_id, conv.conversation_id, key, value, turn_id, source, ts))


# ------------------------------------------------------------------ 轮次扩展
def record_turn_ext(store: Any, conv: Conversation, *, turn_id: str, lang: str,
                    lookup_outcome: str = "", ask_slot: str = "", ask_count: int = 0) -> None:
    """落一轮的 p13 扩展字段（一轮一行，同一轮第二次撞主键）。

    ``lang`` 在 ``LANGS``；``lookup_outcome`` 空串或在 ``LOOKUP_OUTCOMES``；``ask_slot`` 空串或在
    ``SLOT_KEYS``；``ask_count`` 是非负整数，且没追问（``ask_slot`` 空）时只能是 0。
    """
    if lang not in LANGS:
        raise ValueError(f"未知的 lang {lang!r}，只认 {LANGS}")
    if lookup_outcome and lookup_outcome not in LOOKUP_OUTCOMES:
        raise ValueError(f"未知的 lookup_outcome {lookup_outcome!r}，只认空串或 {LOOKUP_OUTCOMES}")
    if ask_slot and ask_slot not in SLOT_KEYS:
        raise ValueError(f"未知的 ask_slot {ask_slot!r}，只认空串或 {SLOT_KEYS}")
    if isinstance(ask_count, bool) or not isinstance(ask_count, int) or ask_count < 0:
        raise ValueError(f"ask_count 必须是非负整数，收到 {ask_count!r}")
    if not ask_slot and ask_count:
        raise ValueError("没有追问（ask_slot 为空）时 ask_count 只能是 0")
    _check_turn_of(conv, turn_id)

    objects.ensure_schema(store)
    objects.execute(
        store,
        "INSERT INTO cs_turn_ext (tenant_id, turn_id, conversation_id, lang, lookup_outcome,"
        " ask_slot, ask_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (conv.tenant_id, turn_id, conv.conversation_id, lang, lookup_outcome or "",
         ask_slot or "", int(ask_count)))


def ask_count(store: Any, tenant_id: str, conversation_id: str, slot_key: str) -> int:
    """该会话对该槽位已追问几次 = cs_turn_ext 里 ``ask_slot == slot_key`` 的轮数。"""
    if slot_key not in SLOT_KEYS:
        raise ValueError(f"未知的槽位 {slot_key!r}，只认 {SLOT_KEYS}")
    objects.ensure_schema(store)
    rows = objects.query(store, "SELECT COUNT(*) AS n FROM cs_turn_ext"
                                " WHERE tenant_id=? AND conversation_id=? AND ask_slot=?",
                         (tenant_id, conversation_id, slot_key))
    return int(rows[0]["n"])


# ------------------------------------------------------------------ 退款桥
def record_bridge(store: Any, conv: Conversation, *, turn_id: str, order_no: str,
                  result: PrecheckResult, now: str | None = None) -> None:
    """落一行退款桥（``bridge_id = turn_id``，一轮至多一行，第二次撞主键）。

    记的是只读预检的结论（ok / decision / rule_ref / reason_code / command_line /
    refused_why）；``summary`` 不落（它进内部卡片）。前台不建工单、不发命令。
    """
    if not isinstance(order_no, str) or not order_no.strip():
        raise ValueError("退款桥的 order_no 必须是非空字符串")
    _check_turn_of(conv, turn_id)

    objects.ensure_schema(store)
    ts = now or objects._now()
    objects.execute(
        store,
        "INSERT INTO cs_refund_bridge (tenant_id, bridge_id, conversation_id, turn_id, order_no,"
        " ok, decision, rule_ref, reason_code, command_line, refused_why, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (conv.tenant_id, turn_id, conv.conversation_id, turn_id, order_no,
         1 if result.ok else 0, result.decision or "", result.rule_ref or "",
         result.reason_code or "", result.command_line or "", result.refused_why or "", ts))
