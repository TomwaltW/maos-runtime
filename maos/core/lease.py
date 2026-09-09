"""认领租约与跨 plan 任务板 —— `claim_timeout` 那条冻结迁移的第一个实现。

## 这个模块买的是什么

`maos/contracts/states.py:29` 写着 `(DISPATCHED, PENDING): "claim_timeout"`，
但改造前**全仓生产代码零命中**：认领之后没有任何超时接管。于是

  · Worker 在 `_transit(RUNNING)` 之后、`_reply` 之前硬崩 —— `claim:<task_id>:<attempt>`
    这个幂等键已经烧掉，而 store 只有 claim/finish 没有撤销口
    （`maos/core/control_plane.py::claim` 的 docstring 自陈这一点），
    任务从此永久停在 RUNNING，没有任何机制把它捞回来；
  · 派了一个当时没人能干的 role，任务永久停在 DISPATCHED。

租约就是那个「捞回来」的机制：认领成功登记一条租约，结果交回销掉它；
到期还没销掉的，说明认领方没交代，按任务当前状态走**既有迁移**放回队列。
**一条新状态、一条新迁移都没有加**（铁律 1、铁律 9）—— 这一单买的正是把表里
已经躺着的两条迁移（`claim_timeout` / `retry`）实现出来。

## 为什么自带一层 SQL 访问器

`maos/core/store.py` 是冻结面（表结构禁改，只许**新增**表），而 `Store` 抽象基类
只有 plan/task/artifact/event_log 那几个具名方法，没有通用 execute。`claim_lease`
是新增表，只能从 `SqliteStore` 的连接上走。同一套做法的先例有两处，形态照抄：

  · `maos/kb/__init__.py` 的 `_MIGRATIONS` —— kb 自己建自己的表，不进核心 store；
  · `maos/domain/refund/objects.py` 的 `_conn()` / `lock_of()` —— 借核心 store 的
    连接与锁，**组合不继承**。

🔴 **锁必须借，不能自己另开连接**：`SqliteStore` 的隔离全靠单连接
（`check_same_thread=False`）加一把 `threading.RLock`（`maos/core/store.py:108-110`）。
另开一条连接就绕过了那把锁 —— 别的线程在 `insert_task` 里一次 `commit()`，
就能把这边只写了一半的事务提交掉，而且是偶发的。

## 时间一律从参数进

本模块内部**不取** `datetime.now()`，判定函数只认传进来的 `now_iso`。
理由是可测试性：测试要能构造出「租约已过期」这个状态而**不 sleep**。
调用方（`ControlPlane` / `WorkerRuntime`）可以有一个取当前时间的默认值 ——
那是调用方的事，不是这里的事。`_utc_now_iso()` 就是给调用方用的那个默认值，
本模块自己一次都不调它。
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from maos.contracts.states import TaskState

log = logging.getLogger("maos.lease")

__all__ = [
    "DEFAULT_TTL_S",
    "LeaseBook",
    "canon_iso",
    "parse_utc",
    "utc_now_iso",
]

#: 租约默认时长。取 300s 而不是更短：一次真模型调用（规划 / 编码 / 评审）在演示机上
#: 常见几十秒，TTL 短于它会把**正在干活**的 worker 判成失联，于是同一个任务被两个
#: worker 各做一遍 —— 那比「卡住不动」更难查。真要调短，先量一遍 p99 执行时长。
DEFAULT_TTL_S = 300

_SCHEMA = """
CREATE TABLE IF NOT EXISTS claim_lease (
    task_id    TEXT PRIMARY KEY,
    attempt    INTEGER NOT NULL,
    worker_id  TEXT NOT NULL,
    granted_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_claim_lease_expires ON claim_lease(expires_at);
"""

#: 落库的时间戳格式。**定宽**是刻意的：过期判定走 SQL 的字符串比较
#: （`expires_at <= ?`），只有定宽的 ISO8601 才保证「字典序 == 时间序」。
#:
#: `datetime.isoformat()` 不定宽 —— 微秒为 0 时它整段省略，于是
#: `...T01:02:03+00:00` 与 `...T01:02:03.500000+00:00` 要靠 '+'(0x2B) < '.'(0x2E)
#: 这个巧合才排对。巧合能排对不代表可以依赖：换个时区偏移（`+08:00`）字典序当场失效。
#: 所以入库前一律先 `canon_iso()` 归一到 UTC + 定宽微秒。
_CANON_FMT = "%Y-%m-%dT%H:%M:%S.%f+00:00"


def utc_now_iso() -> str:
    """调用方的缺省时钟。**本模块自己不调**，见抬头「时间一律从参数进」。"""
    return canon_iso(datetime.now(timezone.utc).isoformat())


def parse_utc(value: str) -> datetime:
    """把任意 ISO8601 读成 UTC 的 aware datetime。裸时间（无时区）按 UTC 读。

    裸时间按 UTC 读而不是按本地时区：本仓库落库的时间戳全部出自
    `maos/core/store.py::_now()`（`datetime.now(timezone.utc).isoformat()`），
    带偏移。会走到「裸」这一支的只有测试里手写的字面量，按 UTC 读最不意外 ——
    按本地时区读的话，同一份用例在不同时区的机器上给出不同的过期判定。
    """
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def canon_iso(value: str) -> str:
    """把任意 ISO8601 归一成定宽 UTC 串（落库与比较的唯一形态）。"""
    return parse_utc(value).strftime(_CANON_FMT)


def _is_frozen(task: dict) -> bool:
    """转调 `control_plane._is_frozen` —— **判定只留那一处**。

    为什么不在这里直接比 `last_error == "frozen_by_replan"`：字面量各写一套就是
    同一条判据有两份实现，改一处漏一处不报错、只会静默走岔。而 import 只能是
    函数级的 —— `control_plane` 在模块顶部 import 本模块（`LeaseBook` /
    `canon_iso`），顶部反向 import 会成环，且环的形态是「拿到一个还没定义完的
    模块」，报的是 ImportError 而不是循环依赖，很难看出真因。
    """
    from maos.core.control_plane import _is_frozen as impl
    return impl(task)


def _conn_of(store: Any) -> sqlite3.Connection:
    """取底层连接。只认暴露了 `_conn` 的 Store 实现（当前是 `SqliteStore`）。"""
    conn = getattr(store, "_conn", None)
    if conn is None:
        raise TypeError(
            f"{type(store).__name__} 没有暴露 sqlite 连接，claim_lease 这张新增表无处落库。"
            " 换后端时在这里加一条分支，不要去改冻结的 store.py。"
        )
    return conn


def _lock_of(store: Any) -> Any:
    """借核心 Store 自己那把 RLock，理由见模块抬头的红字。"""
    lock = getattr(store, "_lock", None)
    return lock if lock is not None else contextlib.nullcontext()


class LeaseBook:
    """认领租约账本 + 跨 plan 任务板。

    **它是只读投影，不是认领口**。`claimable()` 回答的是「现在有哪些任务可以领」，
    真正的认领仍然只走 `ControlPlane.claim()` 一个口 —— 那里有幂等键做互斥。
    这条不变量不许破：任务板自己发认领许可的话，两个 worker 从同一份快照上各领一次，
    互斥就没了，而症状是同一个任务被做两遍。
    """

    def __init__(self, store: Any, *, ttl_s: int = DEFAULT_TTL_S) -> None:
        if ttl_s <= 0:
            raise ValueError(f"ttl_s 必须为正，收到 {ttl_s}")
        self._store = store
        self._conn = _conn_of(store)
        self._lock = _lock_of(store)
        self.ttl_s = ttl_s
        self.ensure_schema()

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return f"LeaseBook(store={type(self._store).__name__}, ttl_s={self.ttl_s})"

    # -- schema ---------------------------------------------------------
    def ensure_schema(self) -> None:
        """幂等建表。谁写谁先建，与 `maos/kb` / 退款域同一个范式。"""
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # -- 写 -------------------------------------------------------------
    def grant(self, task_id: str, attempt: int, worker_id: str, *,
              now_iso: str, ttl_s: int | None = None) -> dict:
        """认领成功后登记租约。同一个 task_id 再次 grant 即**覆盖**。

        覆盖而不是报冲突：重派之后是新的 attempt，旧租约描述的那次认领已经作废。
        用 `INSERT OR REPLACE` 而不是先删后插，是为了让它在一条语句里原子完成。
        """
        ttl = self.ttl_s if ttl_s is None else ttl_s
        if ttl <= 0:
            raise ValueError(f"ttl_s 必须为正，收到 {ttl}")
        at = parse_utc(now_iso)
        granted = at.strftime(_CANON_FMT)
        expires = (at + timedelta(seconds=ttl)).strftime(_CANON_FMT)
        row = {"task_id": task_id, "attempt": int(attempt), "worker_id": worker_id,
               "granted_at": granted, "expires_at": expires}
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO claim_lease"
                " (task_id, attempt, worker_id, granted_at, expires_at) VALUES (?,?,?,?,?)",
                (row["task_id"], row["attempt"], row["worker_id"],
                 row["granted_at"], row["expires_at"]),
            )
            self._conn.commit()
        log.debug("租约登记 [%s] attempt=%s worker=%s 到期 %s",
                  task_id, attempt, worker_id, expires)
        return row

    def release(self, task_id: str) -> bool:
        """结果交回（或租约被回收）后销租约。返回是否真的删掉了一行。"""
        with self._lock:
            cur = self._conn.execute("DELETE FROM claim_lease WHERE task_id=?", (task_id,))
            self._conn.commit()
        return cur.rowcount > 0

    # -- 读 -------------------------------------------------------------
    def holder(self, task_id: str) -> dict | None:
        """当前租约行，没有则 None。**不判过期** —— 过期与否是调用方的判断。"""
        with self._lock:
            r = self._conn.execute(
                "SELECT task_id, attempt, worker_id, granted_at, expires_at"
                " FROM claim_lease WHERE task_id=?", (task_id,)
            ).fetchone()
        return dict(r) if r is not None else None

    def expired(self, now_iso: str) -> list[dict]:
        """所有 `expires_at <= now_iso` 的租约行，按到期时间升序。"""
        with self._lock:
            rs = self._conn.execute(
                "SELECT task_id, attempt, worker_id, granted_at, expires_at"
                " FROM claim_lease WHERE expires_at <= ? ORDER BY expires_at, task_id",
                (canon_iso(now_iso),),
            ).fetchall()
        return [dict(r) for r in rs]

    def claimable(self, roles: Iterable[str], now_iso: str) -> list[dict]:
        """**跨 plan 任务板**：现在可以被 `roles` 认领的任务，按 `created_at` 升序。

        判据四条，缺一不可：
          1. `task.state == 'DISPATCHED'` —— 只有派发出去还没人认领的才在板上。
             PENDING 的依赖未必满足，派发是 `dispatch_ready` 的职责，不是这里的。
             漏了这条，RUNNING（有人正在干）与 AWAITING_REVIEW（已经交付待评审）
             会一起挂上任务板 —— 即「已完成的活重新挂出来让人再做一遍」。
          2. `task.role IN roles` —— 异构队友只看得见自己干得了的活。
          3. 无租约，或租约已过期 —— 有效租约意味着有人正在干，别去抢。
          4. 未被重规划冻结 —— 与 `dispatch_ready` 同一条判据（`_is_frozen`）。
             推、拉两条入口对「哪些任务可以做」必须是同一个口径；漏了这条，
             被新方案淘汰掉的任务会从任务板上被领走并真的执行一遍，
             而推那一侧明确拒绝派发它。

        `store.list_tasks(plan_id)` 是 per-plan 的，跨 plan 这一查没有现成方法，
        而「队友自我认领」要的恰恰是跨 plan 的视野 —— 队友属于团队，不属于某个计划。

        `roles` 空集直接返回空表：SQL 的 `IN ()` 在 sqlite 上是语法错误，
        而「一个 role 都不承接的 worker 看不到任何活」本来就是对的答案。

        返回的是**解码过的完整 task 行**（走 `store.get_task`，JSON 字段已解开），
        不是这里自己拼的裸行。这样任务板与 `store` 对「一个 task 长什么样」只有
        一份口径 —— 自己解一遍 JSON 的话，store 哪天多一个 JSON 列，这里会静默漏解。
        """
        wanted = [r for r in dict.fromkeys(roles)]      # 去重且保序，纯为可读的 SQL 参数
        if not wanted:
            return []
        placeholders = ",".join("?" * len(wanted))
        sql = (
            "SELECT t.task_id FROM task t"
            " LEFT JOIN claim_lease l ON l.task_id = t.task_id"
            f" WHERE t.state = ? AND t.role IN ({placeholders})"
            "   AND (l.task_id IS NULL OR l.expires_at <= ?)"
            " ORDER BY t.created_at, t.task_id"
        )
        params = [TaskState.DISPATCHED, *wanted, canon_iso(now_iso)]
        with self._lock:
            ids = [r[0] for r in self._conn.execute(sql, params).fetchall()]
        tasks = [self._store.get_task(tid) for tid in ids]
        return [t for t in tasks if t is not None and not _is_frozen(t)]

    def unclaimed_dispatched(self, now_iso: str, *,
                             ttl_s: int | None = None) -> list[tuple[dict, str]]:
        """**派发之后从来没人认领**的任务，配上它各自的截止时刻。

        这是 `expired()` 之外的第二个超时源，两者互补且不重叠：

          · `expired()` 只看得见 `claim_lease` 表里的行，而租约唯一的登记点是
            `ControlPlane.claim` 里 `_transit(RUNNING)` **成功之后**那一行；
          · 停在 `DISPATCHED` 的任务按定义还没被认领，因此一行租约都没有。

        少了这一支，`(DISPATCHED, PENDING) -> claim_timeout` 那条迁移在真实链路上
        **不可达**（只有测试手工 `grant()` 才造得出它），而它兜的正是「派了一个
        当时没有任何 Worker 承接的 role」—— 那种任务会永久停在 DISPATCHED，
        无死信、无异常、无告警。

        🔴 **时刻比较走 `parse_utc` 而不是 SQL 的字符串比较。** 这里读的是
        `task.updated_at`，它出自 `store._now()`（`datetime.isoformat()`）——
        **不定宽**：微秒为 0 时整段省略。字典序对它不等于时间序，正是本模块抬头
        那段红字说的坑。`claim_lease.expires_at` 能走 SQL 比较，只因为那是本模块
        自己按 `canon_iso` 写进去的。

        不按 role 过滤：回收是控制面的动作，不是某个 worker 的视角。
        """
        ttl = self.ttl_s if ttl_s is None else ttl_s
        if ttl <= 0:
            raise ValueError(f"ttl_s 必须为正，收到 {ttl}")
        sql = (
            "SELECT t.task_id FROM task t"
            " LEFT JOIN claim_lease l ON l.task_id = t.task_id"
            " WHERE t.state = ? AND l.task_id IS NULL"
            " ORDER BY t.created_at, t.task_id"
        )
        with self._lock:
            ids = [r[0] for r in self._conn.execute(sql, (TaskState.DISPATCHED,)).fetchall()]

        now = parse_utc(now_iso)
        out: list[tuple[dict, str]] = []
        for tid in ids:
            task = self._store.get_task(tid)
            if task is None or _is_frozen(task):
                # 冻结的任务不归回收管（它已被重规划淘汰）。放它进来的话，
                # 每一轮 reap 都会给它落一条 LeaseExpired —— 一条永远处置不完的噪声。
                continue
            deadline = parse_utc(task["updated_at"]) + timedelta(seconds=ttl)
            if deadline <= now:
                out.append((task, deadline.strftime(_CANON_FMT)))
        return out
