"""银行差错处理域的读写口径 —— 建表、迁移、原始支付快照。

**为什么这里有一层 SQL 访问器**：`maos/core/store.py` 是冻结面（铁律 1），
而 `Store` 抽象基类只有 plan/task/artifact/event_log 那几个具名方法，没有通用 execute。
本域的 6 张业务表（外加 1 张迁移记账表）是**新增表**，只能从 `SqliteStore` 的连接上走。
因此本域提供 `execute()` / `query()` 两个薄壳，本域的所有 SQL 都从这里过 ——
store.py 一个字不改。

`execute()` **拒绝任何对 `investigation_case` 的写入**：那张表只有 `guard.py` 写得动。
这是把「不留第二条路径」从 grep 自查升级成代码级拦截 —— grep 挡的是提交进仓库的旁路，
这一条挡的是运行时的旁路。

## 那层薄壳现在住在 `maos/domain/_case_store.py`

本文件原本整体照抄 `maos/domain/refund/objects.py`，差别只在守的是哪张表。既然
差别只有表名，就由 `make_case_store(case_table="investigation_case", ...)` 现造，
不再各写一份。

**本域不接业务引用三件套**（`attach_business_ref` / `list_business_refs` /
`resolve_business_ref`）：骨架里有，本域不绑。差错处理挂的是**原始支付快照**
（`put_payment_snapshot` 那一套），不是「Task → 业务对象」的引用表，`schema.sql`
里也没有 `investigation_business_ref` 这张表。为了「四个域看起来一样」去硬造一张
空表，是拿一致性换掉真实形状 —— 调用方会以为那里能挂东西。

留在本文件的是真正属于差错处理的那些：原始支付快照的读写与版本、
本域的迁移步骤（`_MIGRATIONS` / `_migrate`）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .._case_store import _now, make_case_store

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")

#: 本域的存储口径。`schema.sql` 在这里一次读进来：`ensure_schema()` 与
#: `_MIGRATIONS` 里每一步拿到的必须是**同一份**脚本文本。
_CASE_STORE = make_case_store(
    case_table="investigation_case",
    schema_sql=_SCHEMA_PATH.read_text(encoding="utf-8"),
)

#: 骨架的通用件，绑成模块级名字 —— 对外的调用形态与下沉前逐字相同。
#: **业务引用那三个刻意不绑**，理由见模块抬头。
BypassedGuardError = _CASE_STORE.BypassedGuardError
_conn = _CASE_STORE._conn
lock_of = _CASE_STORE.lock_of
_guarded = _CASE_STORE._guarded
execute = _CASE_STORE.execute
query = _CASE_STORE.query
_atomic = _CASE_STORE._atomic
_has_column = _CASE_STORE._has_column
applied_schema_version = _CASE_STORE.applied_schema_version
ensure_schema = _CASE_STORE.ensure_schema

#: 迁移那组「同生共死」语句用的保存点名，由 `_case_store` 按域前缀现造
#: （本域是 `investigation_schema_migrate`）。多个域的迁移嵌套跑时保存点不能重名。
_MIGRATE_SAVEPOINT = _CASE_STORE.savepoint


# ------------------------------------------------------------------ schema 迁移
#: 迁移步骤表，**按版本号升序**，每项是 `(版本号, 说明, 步骤函数)`，
#: 步骤函数签名 `step(store, script)`。
#:
#: **留在本域不进骨架**：各域的迁移步骤是各域自己的历史，没有一步是共通的。
#: 骨架只提供步骤要用的两个原语 `_atomic` / `_has_column`。
#:
#: **当前是空的，这是对的**：本域是第一次落地，一列都还没改过。凭空造一次迁移
#: 等于给老库跑一段没人验证过的搬运，风险白担（口径同退款域 `_MIGRATIONS`）。
#:
#: 加一步就在末尾追加一条，**不要改已有的那几条**；每一步都必须**自带探针**、
#: 在已是目标形状的库上是 no-op —— 新库靠这条（新库刚建完记账表同样是空的）。
_MIGRATIONS: tuple[tuple[int, str, Any], ...] = ()

#: 本域 schema 的当前版本。跟着 `_MIGRATIONS` 算，**不手写**。
INVESTIGATION_SCHEMA_VERSION = max((v for v, _label, _step in _MIGRATIONS), default=0)


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


#: 把本域的迁移入口挂回骨架 —— `ensure_schema()` 建完表之后调的就是它。
#: **两段，缺一不可**：`schema.sql` 那段全是 `IF NOT EXISTS`，只管「表不在就建」，
#: 对已经存在的表一个字都改不动；`_migrate()` 那段才管「表在但形状旧」。
_CASE_STORE.migrate = _migrate


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
