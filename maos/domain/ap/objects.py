"""应付账款域的读写口径 —— 建表、三单读取、业务引用。

**为什么这里自带一层 SQL 访问器**：`maos/core/store.py` 是冻结面（铁律 1），
而 `Store` 抽象基类只有 plan/task/artifact/event_log 那几个具名方法，没有通用
execute。本域的 13 张业务表（外加 1 张迁移记账表 `ap_schema_version`）是**新增表**，
只能从 `SqliteStore` 的连接上走。因此本模块提供 `execute()` / `query()` 两个薄壳，
本域所有 SQL 都从这里过 —— store.py 一个字不改。

`execute()` **拒绝任何对 `ap_case` 的写入**：那张表只有 `guard.py` 写得动。
这是把「不留第二条路径」从 grep 自查升级成代码级拦截 —— grep 挡的是提交进仓库的
旁路，这一条挡的是运行时的旁路。

## 与退款域 `maos/domain/refund/objects.py` 的关系

两个文件形状同构（同一套 `_conn` / `lock_of` / `_guarded` / 迁移机制），但**互不
import**。这不是重复代码没抽掉，是刻意的：抽成公共基类之后，那个基类就成了两个
域共同持有的面 —— 换第三个域时要动它，而动它就等于动另外两个域。
`docs/domain-portability.md` §1 那张表里 `maos/domain/` 一行标的是 ❌「按域实现」，
共用一层就把它变成 ✅ 了，而那句话本仓库给不出证据。

真正共用的是**机制的形状**（守卫、幂等、迁移探针），那是靠文档与测试传递的，
不是靠一个基类。
"""

from __future__ import annotations

import contextlib
import re
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")

#: `ap_case` 的写入必须走 guard.create_case / guard.update_biz_status。
#: 与退款域那条正则同构，只换表名。ALTER TABLE 不在拦截面上 —— 「给 ap_case 加列」
#: 这类正常迁移不受影响。
_AP_CASE_WRITE = re.compile(
    r"\b(?:insert\s+(?:or\s+\w+\s+)?into|update|delete\s+from|replace\s+into)\s+"
    r"[\"'`\[]?ap_case[\"'`\]]?\b",
    re.IGNORECASE,
)


class BypassedGuardError(RuntimeError):
    """有人试图绕开 `guard.py` 直接写 `ap_case`。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn(store: Any) -> sqlite3.Connection:
    """取底层连接。只认暴露了 `_conn` 的 Store 实现（当前是 `SqliteStore`）。"""
    conn = getattr(store, "_conn", None)
    if conn is None:
        raise TypeError(
            f"{type(store).__name__} 没有暴露 sqlite 连接，应付账款域的新增表无处落库。"
            " 换后端时在这里加一条分支，不要去改冻结的 store.py。"
        )
    return conn


def lock_of(store: Any) -> Any:
    """借 Store 自己的锁。

    `SqliteStore` 的连接是**共享**的（`check_same_thread=False` + 一把 RLock）。
    本域绕过 store.py 直接用这条连接，就必须一并用它那把锁：否则别的线程在
    `insert_task` 里一次 `commit()`，就把 guard 这边只写了回单、还没改状态的
    事务提交掉了 —— 「settled 与回单同事务」当场破，而且是偶发的。
    """
    lock = getattr(store, "_lock", None)
    return lock if lock is not None else contextlib.nullcontext()


def _guarded(sql: str) -> str:
    if _AP_CASE_WRITE.search(sql):
        raise BypassedGuardError(
            "ap_case 的写入必须走 guard.create_case / guard.update_biz_status，"
            "不许经 objects.execute 旁路（铁律 8）"
        )
    return sql


def execute(store: Any, sql: str, params: tuple | list = ()) -> None:
    """本域的写入口径。对 `ap_case` 的写入一律拒绝。"""
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
#: 迁移那组「同生共死」语句用的保存点名。带域前缀：退款域用的是
#: `refund_schema_migrate`、知识层是 `kb_schema_migrate`，几个域的迁移嵌套跑时
#: 保存点不能重名。
_MIGRATE_SAVEPOINT = "ap_schema_migrate"


def _atomic(store: Any, statements: list[tuple[str, tuple]]) -> None:
    """一组语句同生共死，**DDL 也算在内**。

    迁移非用它不可：像「删表 → 建表 → 重灌」这种三步走，断在中间而前两步已落盘的
    话，表在、列全、**一行数据都没有** —— 下一次跑迁移的探针看到列已存在于是跳过，
    那张表从此恒空且不报错。

    **光靠 `rollback()` 撤不回 DDL**：Python 的 sqlite3 在传统模式下只为 DML 隐式开
    事务，`DROP TABLE` 是在自动提交下跑的，一发就落盘。显式发一句 `SAVEPOINT` 才能
    把 DDL 拉进事务。用 `SAVEPOINT` 而不是 `BEGIN`：外层已经在事务里时 `BEGIN` 会报
    cannot start a transaction within a transaction。

    **每条语句照样过 `_guarded()`**（铁律 8）。迁移直连底层连接是为了拿事务，
    不是为了拿豁免权。
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


#: 迁移步骤表，**按版本号升序**，每项是 `(版本号, 说明, 步骤函数)`，
#: 步骤函数签名 `step(store, script)`（`script` 是 schema.sql 原文）。
#:
#: **当前是空的，这是对的**：本域刚落地，一列都没改过。凭空造一次迁移等于给老库
#: 跑一段没人验证过的搬运，风险白担（口径同退款域 `## task-T17` 第 2 条）。
#:
#: 加一步就在末尾追加一条，**不要改已有的那几条**。每一步都必须**自带探针**、
#: 在已是目标形状的库上是 no-op：新库靠这条（新库刚建完记账表同样是空的，
#: 只看版本号会把新库也当老库去 ALTER）。
_MIGRATIONS: tuple[tuple[int, str, Any], ...] = ()

#: 本域 schema 的当前版本。跟着 `_MIGRATIONS` 算，**不手写** —— 手写的那份迟早和
#: 实际步骤对不上，而对不上的症状是「迁移悄悄不跑了」。
AP_SCHEMA_VERSION = max((v for v, _label, _step in _MIGRATIONS), default=0)


def applied_schema_version(store: Any) -> int:
    """这库已经升到第几版。没有记账行就是 0。"""
    rows = query(store, "SELECT MAX(version) AS version FROM ap_schema_version")
    version = rows[0]["version"] if rows else None
    return int(version) if version is not None else 0


def _migrate(store: Any, script: str) -> None:
    """把库升到 `AP_SCHEMA_VERSION`，并逐条记账。

    版本号是**快路径**不是判据：已经记到最新就直接返回，省掉每次都发几条探针。
    真正决定做不做的是每一步自己的探针 —— 版本表在新库上同样是空的，裸信它会把
    新库当老库。记账写在步骤之后：中途失败就不记，下次重跑同一步。
    """
    applied = applied_schema_version(store)
    if applied >= AP_SCHEMA_VERSION:
        return
    for version, _label, step in _MIGRATIONS:
        if version <= applied:
            continue
        step(store, script)
        execute(store, "INSERT INTO ap_schema_version (version, applied_at)"
                       " VALUES (?, ?)", (version, _now()))


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


# ---------------------------------------------------------------- 金额口径
#: 金额一律 Decimal，**不进 float**。理由不是洁癖：三单匹配的勾稽判据
#: （BR-CO-13 / BR-CO-15 / BR-CO-17）是等式比对，0.1+0.2 那种误差会直接变成
#: 一条**假的拒付理由** —— 一张完全正确的发票被拒付，而拒付理由挂着一个真实的
#: 规则编号，看起来毫无破绽。
def money(value: Any) -> Decimal:
    """把任意来源的金额折成 Decimal。解析不出来就抛，**不兜底成 0**。

    兜底成 0 的后果是「金额字段是垃圾」被静默处理成「这笔是 0 元」，而 0 元在
    勾稽里往往刚好对得上（0 = 0 × 税率），于是垃圾数据一路绿灯过闸。
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        # 先转 str 再进 Decimal：Decimal(0.1) 会把 float 的二进制误差原样带进来。
        value = repr(value)
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError):
        raise ValueError(f"金额解析不出数值：{value!r}") from None


def money_str(value: Any, places: str = "0.01") -> str:
    """折成两位小数的字符串，供落库与产物使用。落库的金额一律走这里。"""
    return str(money(value).quantize(Decimal(places)))


# ------------------------------------------------------------ 三单读取口径
# 这四个读取函数是三单匹配的**唯一**数据入口。匹配 skill 不自己写 SQL ——
# 写了就有第二份读取口径，而两份口径迟早对「合格数怎么算」这种事产生分歧。
def get_purchase_order(store: Any, tenant_id: str, po_id: str, version: int) -> dict | None:
    rows = query(store, "SELECT * FROM purchase_order WHERE tenant_id=? AND po_id=?"
                        " AND version=?", (tenant_id, po_id, int(version)))
    return rows[0] if rows else None


def po_lines(store: Any, tenant_id: str, po_id: str, version: int) -> list[dict]:
    return query(store, "SELECT * FROM purchase_order_line WHERE tenant_id=? AND po_id=?"
                        " AND version=? ORDER BY line_no",
                 (tenant_id, po_id, int(version)))


def gr_lines(store: Any, tenant_id: str, gr_id: str) -> list[dict]:
    """收货行。注意 `quantity_received` 是**到货数**，合格数要减掉 `quantity_rejected`。

    三单匹配判的是合格数 —— 到了但验收没过的货不该付钱。这个减法只在
    `accepted_quantity()` 一处做，不散在调用点。
    """
    return query(store, "SELECT * FROM goods_receipt_line WHERE tenant_id=? AND gr_id=?"
                        " ORDER BY line_no", (tenant_id, gr_id))


def accepted_quantity(gr_line: dict) -> float:
    """一条收货行的**合格数** = 到货数 − 验收不合格数。"""
    return float(gr_line["quantity_received"]) - float(gr_line.get("quantity_rejected") or 0)


def get_invoice(store: Any, tenant_id: str, invoice_id: str) -> dict | None:
    rows = query(store, "SELECT * FROM supplier_invoice WHERE tenant_id=? AND invoice_id=?",
                 (tenant_id, invoice_id))
    return rows[0] if rows else None


def invoice_lines(store: Any, tenant_id: str, invoice_id: str) -> list[dict]:
    return query(store, "SELECT * FROM supplier_invoice_line WHERE tenant_id=? AND invoice_id=?"
                        " ORDER BY line_no", (tenant_id, invoice_id))


def get_supplier(store: Any, tenant_id: str, supplier_id: str) -> dict | None:
    rows = query(store, "SELECT * FROM supplier WHERE tenant_id=? AND supplier_id=?",
                 (tenant_id, supplier_id))
    return rows[0] if rows else None


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
        "INSERT OR REPLACE INTO ap_business_ref (plan_id, task_id, tenant_id, object_type,"
        " object_id, object_version, purpose, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (row["plan_id"], row["task_id"], row["tenant_id"], row["object_type"],
         row["object_id"], row["object_version"], row["purpose"], row["created_at"]),
    )
    return row


def list_business_refs(store: Any, *, plan_id: str, task_id: str | None = None) -> list[dict]:
    if task_id is None:
        return query(store, "SELECT * FROM ap_business_ref WHERE plan_id=?"
                            " ORDER BY task_id, object_type, object_id", (plan_id,))
    return query(store, "SELECT * FROM ap_business_ref WHERE plan_id=? AND task_id=?"
                        " ORDER BY object_type, object_id", (plan_id, task_id))


#: object_type -> (表名, 主键列名)。本域自己的取值域，与退款域那张表互不相干。
_REF_TARGETS: dict[str, tuple[str, str]] = {
    "ap_case":          ("ap_case", "case_id"),
    "purchase_order":   ("purchase_order", "po_id"),
    "goods_receipt":    ("goods_receipt", "gr_id"),
    "supplier_invoice": ("supplier_invoice", "invoice_id"),
    "payment_instruction": ("payment_instruction", "instruction_id"),
}
_VERSIONED_REF_TABLES = {"purchase_order"}


def resolve_business_ref(store: Any, ref: dict) -> dict | None:
    """按引用取回被指对象；指不到（对象不存在或版本对不上）返回 None。

    `ap_business_ref` 不带外键 —— 它跨的是「编排层对象」与「业务对象」两个世界，
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
