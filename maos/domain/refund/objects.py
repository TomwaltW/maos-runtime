"""退款域的读写口径 —— 建表、业务引用、政策版本锁定。

**为什么这里自带一层 SQL 访问器**：`maos/core/store.py` 是冻结面（派单「一行不动」），
而 `Store` 抽象基类只有 plan/task/artifact/event_log 那几个具名方法，没有通用 execute。
退款域的 14 张表是**新增表**，只能从 `SqliteStore` 的连接上走。因此本模块提供
`execute()` / `query()` 两个薄壳，退款域的所有 SQL 都从这里过 —— store.py 一个字不改。

`execute()` **拒绝任何对 `refund_case` 的写入**：那张表只有 `guard.py` 写得动。
这是把「不留第二条路径」从 grep 自查升级成代码级拦截 —— grep 挡的是提交进仓库的旁路，
这一条挡的是运行时的旁路。
"""

from __future__ import annotations

import contextlib
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")

#: `refund_case` 的写入必须走 guard.update_biz_status / guard.create_case。
_REFUND_CASE_WRITE = re.compile(
    r"\b(?:insert\s+(?:or\s+\w+\s+)?into|update|delete\s+from|replace\s+into)\s+"
    r"[\"'`\[]?refund_case[\"'`\]]?\b",
    re.IGNORECASE,
)


class BypassedGuardError(RuntimeError):
    """有人试图绕开 `guard.py` 直接写 `refund_case`。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn(store: Any) -> sqlite3.Connection:
    """取底层连接。只认暴露了 `_conn` 的 Store 实现（当前是 `SqliteStore`）。"""
    conn = getattr(store, "_conn", None)
    if conn is None:
        raise TypeError(
            f"{type(store).__name__} 没有暴露 sqlite 连接，退款域的新增表无处落库。"
            " 换后端时在这里加一条分支，不要去改冻结的 store.py。"
        )
    return conn


def lock_of(store: Any) -> Any:
    """借 Store 自己的锁。

    `SqliteStore` 的连接是**共享**的（`check_same_thread=False` + 一把 RLock）。
    退款域绕过 store.py 直接用这条连接，就必须一并用它那把锁：否则别的线程在
    `insert_task` 里一次 `commit()`，就把 guard 这边只写了回执、还没改状态的
    事务提交掉了 —— 「settled 与回执同事务」当场破，而且是偶发的。
    """
    lock = getattr(store, "_lock", None)
    return lock if lock is not None else contextlib.nullcontext()


def _guarded(sql: str) -> str:
    if _REFUND_CASE_WRITE.search(sql):
        raise BypassedGuardError(
            "refund_case 的写入必须走 guard.create_case / guard.update_biz_status，"
            "不许经 objects.execute 旁路（铁律 8）"
        )
    return sql


def execute(store: Any, sql: str, params: tuple | list = ()) -> None:
    """退款域的写入口径。对 `refund_case` 的写入一律拒绝。"""
    conn = _conn(store)
    with lock_of(store):
        conn.execute(_guarded(sql), tuple(params))
        conn.commit()


def query(store: Any, sql: str, params: tuple | list = ()) -> list[dict]:
    """退款域的读取口径。读不设限 —— 守的是写入方，不是读取方。"""
    with lock_of(store):
        rows = _conn(store).execute(sql, tuple(params)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------- 建表
def ensure_schema(store: Any) -> None:
    """读 `schema.sql` 建表。幂等（全部 `CREATE TABLE IF NOT EXISTS`），可连跑。"""
    conn = _conn(store)
    with lock_of(store):
        conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.commit()


# ------------------------------------------------------------ DAG -> 业务对象
def attach_business_ref(
    store: Any,
    *,
    plan_id: str,
    task_id: str,
    tenant_id: str,
    object_type: str,
    object_id: str,
    object_version: int = 0,
    purpose: str = "",
) -> dict:
    """把一个 Task 挂到一个业务对象上 —— **只存引用，不存副本**。

    存副本会立刻产生第二份事实：业务对象改了，Task 里那份不会跟着改，
    而下游分不清哪份是真的。引用只指路，读的时候一定读到当前那一份。
    """
    row = {
        "plan_id": plan_id, "task_id": task_id, "tenant_id": tenant_id,
        "object_type": object_type, "object_id": object_id,
        "object_version": int(object_version), "purpose": purpose,
        "created_at": _now(),
    }
    execute(
        store,
        "INSERT OR REPLACE INTO business_ref (plan_id, task_id, tenant_id, object_type,"
        " object_id, object_version, purpose, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (row["plan_id"], row["task_id"], row["tenant_id"], row["object_type"],
         row["object_id"], row["object_version"], row["purpose"], row["created_at"]),
    )
    return row


def list_business_refs(store: Any, *, plan_id: str, task_id: str | None = None) -> list[dict]:
    if task_id is None:
        return query(store, "SELECT * FROM business_ref WHERE plan_id=?"
                            " ORDER BY task_id, object_type, object_id", (plan_id,))
    return query(store, "SELECT * FROM business_ref WHERE plan_id=? AND task_id=?"
                        " ORDER BY object_type, object_id", (plan_id, task_id))


def resolve_business_ref(store: Any, ref: dict) -> dict | None:
    """按引用取回被指对象；指不到（对象不存在或版本对不上）返回 None。

    `business_ref` 不带外键 —— 它跨的是「编排层对象」与「业务对象」两个世界，
    完整性靠这个函数在读的时候查，不靠数据库替我们保证。
    """
    table, key = _REF_TARGETS.get(ref["object_type"], (None, None))
    if table is None:
        return None
    sql = f"SELECT * FROM {table} WHERE tenant_id=? AND {key}=?"
    params: list[Any] = [ref["tenant_id"], ref["object_id"]]
    if table in _VERSIONED_REF_TABLES:
        sql += " AND version=?"
        params.append(ref["object_version"])
    rows = query(store, sql, params)
    return rows[0] if rows else None


#: object_type -> (表名, 主键列名)
_REF_TARGETS: dict[str, tuple[str, str]] = {
    "refund_case":      ("refund_case", "case_id"),
    "order_snapshot":   ("order_snapshot", "order_id"),
    "product_snapshot": ("product_snapshot", "sku"),
    "policy_rule":      ("policy_rule", "rule_no"),
    "refund_request":   ("refund_request", "request_id"),
}
_VERSIONED_REF_TABLES = {"order_snapshot", "product_snapshot", "policy_rule"}


# ------------------------------------------------------------------ 政策版本
def pinned_policy_version(store: Any, *, tenant_id: str, order_id: str,
                          order_version: int) -> int:
    """取订单**下单当时**锁定的政策版本号。

    这是退款域最容易写错的一处：用当前最新政策去判一笔历史订单，
    等于拿今天的规则追溯昨天的交易 —— 客户按当时公示的政策下的单。
    权威在订单快照上，不在 policy_rule 表的 max(version) 上。
    """
    rows = query(
        store,
        "SELECT policy_version_at_order FROM order_snapshot"
        " WHERE tenant_id=? AND order_id=? AND version=?",
        (tenant_id, order_id, int(order_version)),
    )
    if not rows:
        raise LookupError(
            f"没有订单快照 tenant={tenant_id} order={order_id} v{order_version}，"
            "政策版本无从锁定 —— 先落快照再判政策"
        )
    return int(rows[0]["policy_version_at_order"])


def policy_rules_at_order(
    store: Any,
    *,
    tenant_id: str,
    order_id: str,
    order_version: int,
    sku: str | None = None,
    channel_id: str | None = None,
) -> list[dict]:
    """按订单锁定的政策版本取适用规则。**R-2 的 `policy.match` 直接调这个，不要另写一套。**

    每条 `rule_no` 取「版本号 ≤ 锁定版本」中的最大一版：规则可能在锁定版本之前就定稿、
    之后一直没改，那它当时生效的就是那个旧版本，不是它自己的最新版。
    再按 `channel_scope` / `sku_scope`（`*` 通配）与订单支付时刻的生效区间过滤。
    """
    pinned = pinned_policy_version(store, tenant_id=tenant_id, order_id=order_id,
                                   order_version=order_version)
    snap = query(
        store,
        "SELECT sku, channel_id, paid_at FROM order_snapshot"
        " WHERE tenant_id=? AND order_id=? AND version=?",
        (tenant_id, order_id, int(order_version)),
    )[0]
    want_sku = sku if sku is not None else snap["sku"]
    want_channel = channel_id if channel_id is not None else snap["channel_id"]
    paid_at = snap["paid_at"]

    rows = query(
        store,
        "SELECT r.* FROM policy_rule r"
        " JOIN (SELECT rule_no, MAX(version) AS v FROM policy_rule"
        "        WHERE tenant_id=? AND version<=? GROUP BY rule_no) m"
        "   ON r.rule_no=m.rule_no AND r.version=m.v"
        " WHERE r.tenant_id=?"
        "   AND (r.channel_scope='*' OR r.channel_scope=?)"
        "   AND (r.sku_scope='*'     OR r.sku_scope=?)"
        "   AND r.effective_from<=?"
        "   AND (r.effective_to IS NULL OR r.effective_to>?)"
        " ORDER BY r.rule_no",
        (tenant_id, pinned, tenant_id, want_channel, want_sku, paid_at, paid_at),
    )
    return rows
