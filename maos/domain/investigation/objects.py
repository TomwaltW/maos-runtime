"""银行差错处理域的读写口径 —— 建表、迁移、原始支付快照。

**为什么这里自带一层 SQL 访问器**：`maos/core/store.py` 是冻结面（铁律 1），
而 `Store` 抽象基类只有 plan/task/artifact/event_log 那几个具名方法，没有通用 execute。
本域的 6 张业务表（外加 1 张迁移记账表）是**新增表**，只能从 `SqliteStore` 的连接上走。
因此本模块提供 `execute()` / `query()` 两个薄壳，本域的所有 SQL 都从这里过 ——
store.py 一个字不改。

口径整体照抄 `maos/domain/refund/objects.py`（那是本仓已经跑绿的形状），
差别只在守的是哪张表：

`execute()` **拒绝任何对 `investigation_case` 的写入**：那张表只有 `guard.py` 写得动。
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

#: `investigation_case` 的写入必须走 guard.create_case / guard.update_biz_status。
_CASE_WRITE = re.compile(
    r"\b(?:insert\s+(?:or\s+\w+\s+)?into|update|delete\s+from|replace\s+into)\s+"
    r"[\"'`\[]?investigation_case[\"'`\]]?\b",
    re.IGNORECASE,
)


class BypassedGuardError(RuntimeError):
    """有人试图绕开 `guard.py` 直接写 `investigation_case`。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn(store: Any) -> sqlite3.Connection:
    """取底层连接。只认暴露了 `_conn` 的 Store 实现（当前是 `SqliteStore`）。"""
    conn = getattr(store, "_conn", None)
    if conn is None:
        raise TypeError(
            f"{type(store).__name__} 没有暴露 sqlite 连接，差错处理域的新增表无处落库。"
            " 换后端时在这里加一条分支，不要去改冻结的 store.py。"
        )
    return conn


def lock_of(store: Any) -> Any:
    """借 Store 自己的锁。

    `SqliteStore` 的连接是**共享**的（`check_same_thread=False` + 一把 RLock）。
    本域绕过 store.py 直接用这条连接，就必须一并用它那把锁：否则别的线程在
    `insert_task` 里一次 `commit()`，就把 guard 这边只写了观察、还没改状态的
    事务提交掉了 —— 「returned 与观察同事务」当场破，而且是偶发的。
    """
    lock = getattr(store, "_lock", None)
    return lock if lock is not None else contextlib.nullcontext()


def _guarded(sql: str) -> str:
    if _CASE_WRITE.search(sql):
        raise BypassedGuardError(
            "investigation_case 的写入必须走 guard.create_case / guard.update_biz_status，"
            "不许经 objects.execute 旁路（铁律 8）"
        )
    return sql


def execute(store: Any, sql: str, params: tuple | list = ()) -> None:
    """本域的写入口径。对 `investigation_case` 的写入一律拒绝。"""
    conn = _conn(store)
    with lock_of(store):
        conn.execute(_guarded(sql), tuple(params))
        conn.commit()


def query(store: Any, sql: str, params: tuple | list = ()) -> list[dict]:
    """本域的读取口径。读不设限 —— 守的是写入方，不是读取方。"""
    with lock_of(store):
        rows = _conn(store).execute(sql, tuple(params)).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ schema 迁移
#: 迁移那组「同生共死」语句用的保存点名。**带域前缀**：退款域用的是
#: `refund_schema_migrate`、知识层用 `kb_schema_migrate`，两个域的迁移嵌套跑时
#: 保存点不能重名。
_MIGRATE_SAVEPOINT = "investigation_schema_migrate"


def _atomic(store: Any, statements: list[tuple[str, tuple]]) -> None:
    """一组语句同生共死，**DDL 也算在内**。理由与退款域逐字相同：

    「删表 → 建表 → 重灌」这种三步走，断在中间而前两步已落盘的话，表在、列全、
    **一行数据都没有** —— 下一次迁移探针看到列已存在于是跳过，那张表从此恒空且不报错。

    **光靠 `rollback()` 撤不回 DDL**：Python 的 sqlite3 在传统模式下只为 DML 隐式开
    事务，`DROP TABLE` 是在自动提交下跑的，一发就落盘。显式发一句 `SAVEPOINT` 才能
    把 DDL 拉进事务；用 SAVEPOINT 而不是 BEGIN，是因为外层已在事务里时 BEGIN 会报
    cannot start a transaction within a transaction。

    **每条语句照样过 `_guarded()`**（铁律 8）。迁移绕开 `execute()` 直连底层连接是为了
    拿事务，不是为了拿豁免权。
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

    理由同退款域：PRAGMA 是 SQLite 方言（换后端不保证有），且
    `PRAGMA table_info` **不列生成列**，拿它判一个生成列在不在会每次都去 ALTER，
    每次都撞 duplicate column name。

    异常一律当「没这列」：真是连接坏了，紧随其后的 ALTER 会自己响。
    """
    try:
        query(store, f"SELECT {column} FROM {table} LIMIT 1")
        return True
    except Exception:                                  # noqa: BLE001 —— 探针不该炸
        return False


#: 迁移步骤表，**按版本号升序**，每项是 `(版本号, 说明, 步骤函数)`，
#: 步骤函数签名 `step(store, script)`。
#:
#: **当前是空的，这是对的**：本域是第一次落地，一列都还没改过。凭空造一次迁移
#: 等于给老库跑一段没人验证过的搬运，风险白担（口径同退款域 `_MIGRATIONS`）。
#:
#: 加一步就在末尾追加一条，**不要改已有的那几条**；每一步都必须**自带探针**、
#: 在已是目标形状的库上是 no-op —— 新库靠这条（新库刚建完记账表同样是空的）。
_MIGRATIONS: tuple[tuple[int, str, Any], ...] = ()

#: 本域 schema 的当前版本。跟着 `_MIGRATIONS` 算，**不手写**。
INVESTIGATION_SCHEMA_VERSION = max((v for v, _label, _step in _MIGRATIONS), default=0)


def applied_schema_version(store: Any) -> int:
    """这库已经升到第几版。没有记账行就是 0。"""
    rows = query(store, "SELECT MAX(version) AS version FROM investigation_schema_version")
    version = rows[0]["version"] if rows else None
    return int(version) if version is not None else 0


def _migrate(store: Any, script: str) -> None:
    """把库升到 `INVESTIGATION_SCHEMA_VERSION`，并逐条记账。

    版本号是**快路径**不是判据：已经记到最新就直接返回，省掉每次都发几条探针。
    真正决定做不做的是每一步自己的探针 —— 版本表在新库上同样是空的，裸信它会把
    新库当老库。记账写在步骤之后：中途失败就不记，下次重跑同一步。
    """
    applied = applied_schema_version(store)
    if applied >= INVESTIGATION_SCHEMA_VERSION:
        return
    for version, _label, step in _MIGRATIONS:
        if version <= applied:
            continue
        step(store, script)
        execute(store, "INSERT INTO investigation_schema_version (version, applied_at)"
                       " VALUES (?, ?)", (version, _now()))


# ---------------------------------------------------------------------- 建表
def ensure_schema(store: Any) -> None:
    """建表 + 迁移到最新版本。幂等，可连跑。

    **两段，缺一不可**：`schema.sql` 那段全是 `IF NOT EXISTS`，只管「表不在就建」，
    对已经存在的表一个字都改不动；`_migrate()` 那段才管「表在但形状旧」。
    只有第一段的时候，改列是静默无效的。
    """
    script = _SCHEMA_PATH.read_text(encoding="utf-8")
    conn = _conn(store)
    with lock_of(store):
        conn.executescript(script)
        conn.commit()
    _migrate(store, script)


# ------------------------------------------------------------ 原始支付快照
def put_payment_snapshot(
    store: Any,
    *,
    tenant_id: str,
    original_msg_id: str,
    version: int,
    end_to_end_id: str,
    interbank_amount: float,
    currency: str,
    value_date: str,
    debtor_agent: str,
    creditor_agent: str,
    settlement_method: str = "",
    payload_json: str = "{}",
) -> dict:
    """落一份原始支付快照 —— **MAOS 执行前读到的那一版**，不是清算系统的当前值。

    `read_at` 由本函数写，记下读的时刻。快照带版本号：同一笔原始支付被重新读过一次
    （比如清算方补发了更正报文），是**新增一版**，不是覆盖旧版 —— 覆盖会让
    「我们当时是按哪一版判的」这个问题永远答不上来。
    """
    row = {
        "tenant_id": tenant_id, "original_msg_id": original_msg_id,
        "version": int(version), "end_to_end_id": end_to_end_id,
        "interbank_amount": float(interbank_amount), "currency": currency,
        "value_date": value_date, "debtor_agent": debtor_agent,
        "creditor_agent": creditor_agent, "settlement_method": settlement_method,
        "payload_json": payload_json, "read_at": _now(),
    }
    execute(
        store,
        "INSERT OR REPLACE INTO original_payment_snapshot (tenant_id, original_msg_id,"
        " version, end_to_end_id, interbank_amount, currency, value_date, debtor_agent,"
        " creditor_agent, settlement_method, payload_json, read_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (row["tenant_id"], row["original_msg_id"], row["version"], row["end_to_end_id"],
         row["interbank_amount"], row["currency"], row["value_date"], row["debtor_agent"],
         row["creditor_agent"], row["settlement_method"], row["payload_json"],
         row["read_at"]),
    )
    return row


def get_payment_snapshot(store: Any, *, tenant_id: str, original_msg_id: str,
                         version: int) -> dict:
    """按 (租户, 原报文号, 版本) 取快照；取不到就抛。

    **不返回 None**：调用方拿到 None 之后最可能的动作是「那就用默认值继续」，
    而这里的默认值是金额和币种 —— 那是往一笔差错处理里凭空填数字。
    """
    rows = query(
        store,
        "SELECT * FROM original_payment_snapshot"
        " WHERE tenant_id=? AND original_msg_id=? AND version=?",
        (tenant_id, original_msg_id, int(version)),
    )
    if not rows:
        raise LookupError(
            f"没有原始支付快照 tenant={tenant_id} msg={original_msg_id} v{version}；"
            "差错案件必须挂在一份读到过的原始支付上 —— 先落快照再受理"
        )
    return rows[0]


def latest_snapshot_version(store: Any, *, tenant_id: str, original_msg_id: str) -> int:
    """这笔原始支付最新读到的是第几版。一版都没有就是 0。"""
    rows = query(
        store,
        "SELECT MAX(version) AS v FROM original_payment_snapshot"
        " WHERE tenant_id=? AND original_msg_id=?",
        (tenant_id, original_msg_id),
    )
    v = rows[0]["v"] if rows else None
    return int(v) if v is not None else 0
