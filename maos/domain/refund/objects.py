"""退款域的读写口径 —— 建表、业务引用、政策版本锁定。

**为什么这里自带一层 SQL 访问器**：`maos/core/store.py` 是冻结面（派单「一行不动」），
而 `Store` 抽象基类只有 plan/task/artifact/event_log 那几个具名方法，没有通用 execute。
退款域的 14 张业务表（外加 1 张迁移记账表 `refund_schema_version`）是**新增表**，
只能从 `SqliteStore` 的连接上走。因此本模块提供
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


# ------------------------------------------------------------------ schema 迁移
#: 迁移那组「同生共死」语句用的保存点名。带域前缀：知识层那套用的是
#: `kb_schema_migrate`，两个域的迁移嵌套跑时保存点不能重名。
_MIGRATE_SAVEPOINT = "refund_schema_migrate"


def _atomic(store: Any, statements: list[tuple[str, tuple]]) -> None:
    """一组语句同生共死，**DDL 也算在内**。

    迁移非用它不可：像「删表 → 建表 → 重灌」这种三步走，断在中间而前两步已落盘的
    话，表在、列全、**一行数据都没有** —— 下一次跑迁移的探针看到列已存在于是跳过，
    那张表从此恒空且不报错。本轨要买掉的正是这类无症状失效，不能自己再造一个。

    **光靠 `rollback()` 撤不回 DDL**：Python 的 sqlite3 在传统模式下只为 DML 隐式开
    事务，`DROP TABLE` 是在自动提交下跑的，一发就落盘。显式发一句 `SAVEPOINT` 才能
    把 DDL 拉进事务。

    用 `SAVEPOINT` 而不是 `BEGIN`：外层已经在事务里时 `BEGIN` 会报
    cannot start a transaction within a transaction，`SAVEPOINT` 开不开事务都能用。

    **每条语句照样过 `_guarded()`**（铁律 8）。迁移绕开 `execute()` 直连底层连接是为了
    拿事务，不是为了拿豁免权：真有哪一步要搬 `refund_case` 的数据，这里当场响、由人
    决定怎么办，比静默放行一条旁路强。ALTER TABLE 不在 `_guarded()` 的拦截面上，
    所以「给 refund_case 加列」这类正常迁移不受影响。
    """
    for sql, _params in statements:
        _guarded(sql)
    conn = _conn(store)
    with lock_of(store):
        conn.execute(f"SAVEPOINT {_MIGRATE_SAVEPOINT}")
        try:
            for sql, params in statements:
                conn.execute(sql, tuple(params))
        except BaseException:
            conn.execute(f"ROLLBACK TO {_MIGRATE_SAVEPOINT}")
            conn.execute(f"RELEASE {_MIGRATE_SAVEPOINT}")
            conn.rollback()
            raise
        conn.execute(f"RELEASE {_MIGRATE_SAVEPOINT}")
        conn.commit()


def _has_column(store: Any, table: str, column: str) -> bool:
    """探「这张表有没有这一列」。用一条 SELECT，**不用 PRAGMA**。

    两条理由，第二条是坑：

    · PRAGMA 是 SQLite 方言，换后端时不保证有（PG 那边没有）。
    · `PRAGMA table_info` **不列生成列** —— 生成列要 `table_xinfo` 才看得到。
      拿 table_info 判一个生成列在不在，新库上会答「不在」，于是每次都去 ALTER
      一次，每次都撞 duplicate column name。

    表名列名都是本模块的字面量，不是外来输入，所以直接拼进 SQL。
    异常一律当「没这列」：真是连接坏了，紧随其后的 ALTER 会自己响，不会被这层吞掉。
    """
    try:
        query(store, f"SELECT {column} FROM {table} LIMIT 1")
        return True
    except Exception:                                  # noqa: BLE001 —— 探针不该炸
        return False


#: 迁移步骤表，**按版本号升序**，每项是 `(版本号, 说明, 步骤函数)`，
#: 步骤函数签名 `step(store, script)`（`script` 是 schema.sql 原文，需要「按目标形状
#: 重建某张表」时从它现取，不要在步骤里另抄一份建表语句 —— 抄一份的后果是哪天有人
#: 改了 schema.sql，老库重建出来的表和新库不是同一张表，而两边都不报错）。
#:
#: **当前是空的，这是对的**：T26 只装机制，一列都没改（BACKLOG `## task-T17` 第 2 条
#: 原话「建议在往退款域加第一列之前做」）。凭空造一次迁移等于给老库跑一段没人验证过
#: 的搬运，风险白担。
#:
#: 加一步就在末尾追加一条，**不要改已有的那几条** —— 已经跑过的库不会再跑一遍它们，
#: 改了等于新老库形状分叉。每一步都必须**自带探针**、在已是目标形状的库上是 no-op：
#: 新库靠这条（新库刚建完记账表同样是空的，只看版本号会把新库也当老库去 ALTER）。
_MIGRATIONS: tuple[tuple[int, str, Any], ...] = ()

#: 退款域 schema 的当前版本。跟着 `_MIGRATIONS` 算，**不手写** —— 手写的那份迟早和
#: 实际步骤对不上，而对不上的症状是「迁移悄悄不跑了」。
REFUND_SCHEMA_VERSION = max((v for v, _label, _step in _MIGRATIONS), default=0)


def applied_schema_version(store: Any) -> int:
    """这库已经升到第几版。没有记账行就是 0（T26 之前建的老库都在这一档）。"""
    rows = query(store, "SELECT MAX(version) AS version FROM refund_schema_version")
    version = rows[0]["version"] if rows else None
    return int(version) if version is not None else 0


def _migrate(store: Any, script: str) -> None:
    """把库升到 `REFUND_SCHEMA_VERSION`，并逐条记账。

    版本号是**快路径**不是判据：已经记到最新就直接返回，省掉每次都发几条探针
    （`ensure_schema` 挂在写入口上，调用频次很高）。真正决定做不做的是每一步自己的
    探针 —— 版本表在新库上同样是空的，裸信它会把新库当老库。

    记账写在步骤之后：中途失败就不记，下次重跑同一步 —— 步骤是幂等的，重跑安全。
    """
    applied = applied_schema_version(store)
    if applied >= REFUND_SCHEMA_VERSION:
        return
    for version, _label, step in _MIGRATIONS:
        if version <= applied:
            continue
        step(store, script)
        execute(store, "INSERT INTO refund_schema_version (version, applied_at)"
                       " VALUES (?, ?)", (version, _now()))


# ---------------------------------------------------------------------- 建表
def ensure_schema(store: Any) -> None:
    """建表 + 迁移到最新版本。幂等，可连跑。

    **两段，缺一不可**：`schema.sql` 那段全是 `IF NOT EXISTS`，只管「表不在就建」，
    对已经存在的表一个字都改不动；`_migrate()` 那段才管「表在但形状旧」。
    只有第一段的时候，改列是静默无效的 —— 加表可以（新表直接生效），改列不行：
    `IF NOT EXISTS` 直接跳过，跑起来一切正常，直到某条 INSERT 报 no such column。
    """
    script = _SCHEMA_PATH.read_text(encoding="utf-8")
    conn = _conn(store)
    with lock_of(store):
        conn.executescript(script)
        conn.commit()
    _migrate(store, script)


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
