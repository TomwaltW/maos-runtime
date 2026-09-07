"""Agent 之间的第一条**点对点**通道 —— 收件箱。

## 为什么不去扩 EventBus 加一个 recipient 字段

MAOS 今天没有 agent→agent 的通道，三处证据：`Envelope`（冻结的事件契约）只有
`event_type / plan_id / task_id / idempotency_key / payload / event_id / trace_id /
attempt / occurred_at`，**没有 sender / recipient / reply_to**；总线是广播 ——
同一 topic 的所有订阅者都收到每条消息（`maos/core/eventbus.py:66-68`），`group`
只参与重投计数；Agent 构造只收 model 与 store（`maos/agents/base.py:151-155`），
拿不到 bus，`TaskContext` 的 docstring 也明写「看不到全局状态」。

扩总线的三条否决理由，写在这里是为了后来人不必再重问一遍：

1. 改 `Envelope` 要动冻结契约（铁律 1），`.contracts.lock` 的指纹会当场红。
2. 塞进 `payload` 再新起一个 topic 字符串技术上可行（`publish(topic: str)` 不校验
   topic 集合），但撞上仓库的成文纪律：新事件走 `append_event_log` 的自由
   `event_type`，**不加新 Topic**（`maos/config/audit.py` 抬头引了
   `maos/agents/testing.py:50` 与 `maos/kb/retriever.py:571` 两个先例）。
3. 破坏面不对等。扩总线要让现有三处总线接入点 —— `core/control_plane.py:188-189`
   与 `runtime/worker.py:35` 两处订阅、`hiclaw/matrix_bus.py:946` 那层转发 ——
   **每一处**都学会「这条不是给我的，跳过」，漏改一处的症状是别人的私信被当成
   自己的输入。独立收件箱对它们的破坏面是**零**：不 import 本模块的代码，
   行为逐字节不变。

## 表是自己建的，核心 store 一个字节没动

`maos/core/store.py` 的表结构是冻结面（只许新增表）。所以 `agent_message`
由本模块自己建，做法照抄两个先例：`maos/kb/__init__.py`（自己建自己的表）与
`maos/store/sqlite_store.py:118-136`（**借**核心 store 的 `_conn` 与 `_lock`，
组合不继承）。

🔴 借那把 `threading.RLock`（核心 store 里那一把）不是可选项：连接是
`check_same_thread=False` 共享的，另开一把锁或干脆不加锁，别的线程一次
`commit()` 就能把这边只写了一半的事务提交掉。全仓的写互斥只有那一把。

**没有迁移账本，因为这张表是新的**：`_SCHEMA` 全是 `IF NOT EXISTS`，它只管
「表不在就建」，对**已经存在**的表一个字都改不动。今天世上还没有旧形状的
`agent_message`，所以够用。哪天要改列，正确做法是照 `maos/kb/__init__.py`
的 `_MIGRATIONS` 补一步迁移，**不是**去改 `_SCHEMA` —— 改它对老库静默无效，
症状是某条 SELECT 突然报 no such column。

## 自动投递的落点

`deliver_into()` 是「Agent 不轮询」这句话的实现：整合期由 Worker 在执行任务前
调一次，把未读消息取走、标记已读、直接塞进 `TaskContext.inputs["inbox"]`。
本模块**不做接线** —— 接线是整合期的事，这里只保证接得上。
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from typing import Any

from maos.contracts.events import new_id
from maos.skills.invoker import _digest

__all__ = [
    "AGENT_MESSAGE_EVENT",
    "SECRET_MARKERS",
    "SECURITY_EVENT",
    "VALID_KINDS",
    "Mailbox",
    "MailboxError",
    "SecretLeakBlocked",
]

#: `event_log.event_type` 的字面量。**不进冻结事件契约的 Topic**（铁律 1），
#: 与 `SkillInvoked` / `ToolInvoked` / `KbRetrieved` / `ArtifactSeeded` / `ConfigChanged`
#: 同类，理由见 `maos/config/audit.py` 抬头。
AGENT_MESSAGE_EVENT = "AgentMessage"

#: 拒发时落的那条。同样是自由 `event_type`，不进契约。
SECURITY_EVENT = "SecurityEvent"

#: `kind` 白名单。锁死取值域的理由同 `maos/artifacts.py` 锁 `mode` 那段：
#: 放行未知取值不会当场报错，只会让下游走不到分支 —— 症状是「消息静默不生效」，
#: 而排查者手上只有一条看起来完全正常的库记录。
VALID_KINDS = frozenset({"ask", "inform", "handoff", "shutdown_request", "shutdown_reply"})

#: 明文凭证判据。**逐字照抄** `maos/runtime/gate.py` 的 `_gate_security` 那一行，
#: 不另发明一套 —— 两处口径一旦分叉，症状是「补丁通道拦得住、消息通道拦不住」，
#: 而两边各自的测试都是绿的。照抄而不 import 的先例见 `maos/agents/testing.py`
#: 的 `SEEDED_EVENT`（「读侧照抄而不 import」），那里的判据同样是内联在函数里的
#: 字面量，import 不出来。
SECRET_MARKERS = ("AKIA", "-----BEGIN", "password=", "api_key=")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_message (
    msg_id     TEXT PRIMARY KEY,
    plan_id    TEXT NOT NULL DEFAULT '',
    task_id    TEXT,
    from_agent TEXT NOT NULL,
    to_agent   TEXT NOT NULL,
    kind       TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    read_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_message_to ON agent_message(to_agent, read_at);
"""

#: 出库列序。写死一份是为了不用 `SELECT *` —— `sqlite3.Row` 支持下标取列而 dict
#: 不支持，两种形状混着返回的话，上游哪天顺手写了 `row[0]` 只会在一条路径上炸。
_COLUMNS = ("msg_id", "plan_id", "task_id", "from_agent", "to_agent",
            "kind", "body", "created_at", "read_at")


class MailboxError(ValueError):
    """收件箱的入参违约。继承 `ValueError`：调用方按哪个捕获都拦得住。"""


class SecretLeakBlocked(MailboxError):
    """正文里疑似明文凭证，这条消息**没有**发出去。

    单独一个子类是为了让调用方分得清「我参数写错了」和「我差点把密钥发出去」——
    后者要报给人，前者改代码就行。
    """


def _serialize(body: Any) -> str:
    """正文落库形态。解不动的对象降级成 `str`，与 `_digest` 同一套兜底口径。"""
    try:
        return json.dumps(body, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return json.dumps(repr(body), ensure_ascii=False)


class Mailbox:
    """`agent_message` 表的读写口径。构造即建表，幂等，可连开多个实例。

    只接暴露了 `_conn` 的核心 `SqliteStore`。换后端时另写一个适配器，
    **不要**去改冻结的核心 store。
    """

    def __init__(self, store: Any) -> None:
        conn = getattr(store, "_conn", None)
        if conn is None:
            raise TypeError(
                f"{type(store).__name__} 没有暴露 sqlite 连接，收件箱的新增表无处落库。"
                " 本模块只接核心 store 的 SqliteStore；换后端请写新的适配器，"
                " 不要去改冻结的核心 store。"
            )
        self._store = store
        self._conn: sqlite3.Connection = conn
        lock = getattr(store, "_lock", None)
        #: 借核心 Store 自己那把 RLock，理由见模块抬头。拿不到才退化成空上下文
        #: （只有单测里的假 store 会走到），而不是自己新造一把 —— 新造的那把跟
        #: 真正在写库的那把互不相识，等于没锁。
        self._lock: Any = lock if lock is not None else contextlib.nullcontext()
        self.ensure_schema()

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return f"Mailbox(store={type(self._store).__name__})"

    def ensure_schema(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # -- 写 -------------------------------------------------------------------
    def send(self, *, from_agent: str, to_agent: str, kind: str, body: Any,
             plan_id: str = "", task_id: str | None = None, now_iso: str) -> str:
        """投递一条，返回 `msg_id`。四条闸按顺序过，任一条不过都不落库。

        1. **`from_agent` 由投递层写死，不信 `body`。** `body` 里的 `from` /
           `from_agent` 键一律**不参与**发件人判定 —— 收件方看到的「谁发的」必须是
           投递层的判断，不是发送方的自述。正文本身**原样保留**（不删那两个键）：
           删了就是替发送方改写他说过的话，审计时读到的将不是真实报文；防线在于
           发件人只从 `from_agent` 列读，任何人都不该去 `body` 里找身份。
        2. `kind` 白名单，非法值**抛**不返回失败（见 `VALID_KINDS`）。
        3. 明文凭证扫描，命中即拒发并落一条 `SecurityEvent`（见 `SECRET_MARKERS`）。
        4. 落一条 `AgentMessage` 事件，**正文不进 event_log**，只进 `body_digest`。
        """
        if not from_agent or not to_agent:
            raise MailboxError(
                f"from_agent / to_agent 都不能为空，实际 {from_agent!r} -> {to_agent!r}。"
                " 点对点通道的两端必须指名道姓，空串会让这条消息谁都收不到。"
            )
        if kind not in VALID_KINDS:
            raise MailboxError(
                f"kind 必须是 {sorted(VALID_KINDS)} 之一，实际 {kind!r}。"
                " 放行未知取值的后果不是报错，是下游走不到分支、消息静默不生效。"
            )

        raw = _serialize(body)
        hit = next((m for m in SECRET_MARKERS if m in raw), None)
        if hit is not None:
            # 先落审计再抛：拒发这件事本身要留痕，否则「消息没到」查不出原因。
            # 命中的关键字进 detail，**正文不进** —— 正文正是那个不能落盘的东西。
            self._append_event(SECURITY_EVENT, plan_id=plan_id, task_id=task_id,
                               reason="消息正文中疑似出现明文凭证，已拒发",
                               detail={"from": from_agent, "to": to_agent, "kind": kind,
                                       "marker": hit, "body_digest": _digest(body)})
            raise SecretLeakBlocked(
                f"消息正文命中明文凭证判据 {hit!r}，已拒发。判据与补丁安全闸"
                "（gate 的 _gate_security）同一份。"
            )

        msg_id = new_id("msg")
        with self._lock:
            self._conn.execute(
                "INSERT INTO agent_message (msg_id, plan_id, task_id, from_agent, to_agent,"
                " kind, body, created_at, read_at) VALUES (?,?,?,?,?,?,?,?,NULL)",
                (msg_id, plan_id or "", task_id, from_agent, to_agent, kind, raw, now_iso),
            )
            self._conn.commit()

        # detail 里只放摘要，不放正文。理由同 `core/control_plane.py` 的 `_finding_ref`
        # （长文案不带走）。摘要**复用** `skills/invoker._digest`，不自写 sha256 ——
        # 自写就是第二套口径，两边算出来不一样的那天没人知道该信哪个。
        self._append_event(AGENT_MESSAGE_EVENT, plan_id=plan_id, task_id=task_id,
                           reason=None,
                           detail={"msg_id": msg_id, "from": from_agent, "to": to_agent,
                                   "kind": kind, "body_digest": _digest(body)})
        return msg_id

    def mark_read(self, msg_ids: Any, *, now_iso: str) -> int:
        """标记已读，返回实际改动行数。已读的不再改 `read_at`（首次读取时刻才有意义）。"""
        ids = [str(m) for m in msg_ids]
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        with self._lock:
            cur = self._conn.execute(
                "UPDATE agent_message SET read_at=? WHERE read_at IS NULL"
                f" AND msg_id IN ({marks})",
                (now_iso, *ids),
            )
            self._conn.commit()
            return cur.rowcount

    # -- 读 -------------------------------------------------------------------
    def inbox(self, agent_name: str, *, unread_only: bool = True) -> list[dict]:
        """收件箱，按 `created_at` 升序。**只返回投给 `agent_name` 的**，不是广播。

        次级排序键是 `rowid`：同一次批量投递里几条消息的 `created_at` 可能逐字节
        相同（调用方传的是同一个 `now_iso`），只按 `created_at` 排的话它们之间的
        先后每次查询都可能不同，而「谁先说的」是对话语义的一部分。
        """
        sql = ("SELECT " + ", ".join(_COLUMNS) + " FROM agent_message WHERE to_agent=?"
               + (" AND read_at IS NULL" if unread_only else "")
               + " ORDER BY created_at, rowid")
        with self._lock:
            rows = self._conn.execute(sql, (agent_name,)).fetchall()
        return [self._decode(r) for r in rows]

    def deliver_into(self, agent_name: str, *, now_iso: str) -> list[dict]:
        """取未读 → 标记已读 → 返回可直接塞进 `TaskContext.inputs["inbox"]` 的 list。

        这是「自动投递」的落点：整合期由 Worker 在执行前调一次，Agent 自己不轮询
        （`TaskContext` 拿不到 store，它也没法轮询）。**取走即已读**，所以同一批
        消息只会进一次上下文 —— 重复注入的症状是 Agent 反复回应同一条请求。
        """
        msgs = self.inbox(agent_name, unread_only=True)
        if not msgs:
            return []
        self.mark_read([m["msg_id"] for m in msgs], now_iso=now_iso)
        # 返回的是取走那一刻的形态：read_at 补成本次投递时刻，而不是留 None ——
        # 留 None 会让读到这份 list 的人以为这些消息还没读过。
        for m in msgs:
            m["read_at"] = now_iso
        return msgs

    # -- 内部 -----------------------------------------------------------------
    def _decode(self, row: Any) -> dict:
        d = dict(zip(_COLUMNS, tuple(row)))
        try:
            d["body"] = json.loads(d["body"])
        except (TypeError, ValueError):
            pass    # 不是 JSON 就原样给出去，不编内容也不抛
        return d

    def _append_event(self, event_type: str, *, plan_id: str, task_id: str | None,
                      reason: str | None, detail: dict) -> None:
        """落一条 event_log。走核心 store 的自由 `event_type`，不进冻结契约。

        `plan_id` / `task_id` 都要带上：`task_id` 带了，这条事件才挂得到该 task 的
        span 上（`maos/obs/trace.py` 里 `parent = task_span.get(tid) if tid else
        root_id` 那一行）；`plan_id` 带了，它才不会掉进 `stray_events`
        （那边按「plan_id 指不到任何 plan 行」点名）。
        """
        sink = getattr(self._store, "append_event_log", None)
        if sink is None:     # pragma: no cover - 核心 Store 一定有
            return
        sink({"plan_id": plan_id or "", "task_id": task_id, "event_type": event_type,
              "reason": reason, "detail": detail})
