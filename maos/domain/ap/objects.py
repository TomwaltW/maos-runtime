"""应付账款域的读写口径 —— 建表、三单读取、业务引用。

**为什么这里有一层 SQL 访问器**：`maos/core/store.py` 是冻结面（铁律 1），
而 `Store` 抽象基类只有 plan/task/artifact/event_log 那几个具名方法，没有通用
execute。本域的 13 张业务表（外加 1 张迁移记账表 `ap_schema_version`）是**新增表**，
只能从 `SqliteStore` 的连接上走。因此本域提供 `execute()` / `query()` 两个薄壳，
本域所有 SQL 都从这里过 —— store.py 一个字不改。

`execute()` **拒绝任何对 `ap_case` 的写入**：那张表只有 `guard.py` 写得动。
这是把「不留第二条路径」从 grep 自查升级成代码级拦截 —— grep 挡的是提交进仓库的
旁路，这一条挡的是运行时的旁路。

## 那层薄壳现在住在 `maos/domain/_case_store.py`

上面这些机制本文件不再自己实现一遍，从 `make_case_store(case_table="ap_case", ...)`
取。原因是实测出来的：本文件与 `maos/domain/claim/objects.py` 的 `_guarded` 逐行
diff 只有 4 处，全部是 `ap_case` → `claim_case` 这类表名字符串 —— 那是可参数化的
重复，不是领域差异。

**这不等于「域与域焊在一起」**：`_case_store` 不属于任何一个域，是四个域共同踩的
地板；本域仍然不 import 别的域，别的域也不 import 本域。换第三个域照旧只新增文件，
多调一次 `make_case_store()` 而已。`docs/domain-portability.md` §1 那张表里
`maos/domain/` 一行标的 ❌「按域实现」讲的是**域之间**，这一条没变。

留在本文件的是真正属于应付账款的那些：金额口径（`money` / `money_str`）、
三单读取（`po_lines` / `gr_lines` / `invoice_lines` / …）、本域的迁移步骤
（`_MIGRATIONS` / `_migrate`）与业务引用取值域（`_REF_TARGETS`）。
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .._case_store import _now, make_case_store

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")

#: 本域的存储口径。`schema.sql` 在这里一次读进来：`ensure_schema()` 与
#: `_MIGRATIONS` 里每一步拿到的必须是**同一份**脚本文本。
_CASE_STORE = make_case_store(
    case_table="ap_case",
    schema_sql=_SCHEMA_PATH.read_text(encoding="utf-8"),
)

#: 下面这组是骨架的通用件，绑成模块级名字 —— 对外的调用形态
#: （`objects.execute(store, sql, params)`）与下沉前逐字相同，调用方一行不用改。
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
attach_business_ref = _CASE_STORE.attach_business_ref
list_business_refs = _CASE_STORE.list_business_refs
resolve_business_ref = _CASE_STORE.resolve_business_ref

#: 迁移那组「同生共死」语句用的保存点名，由 `_case_store` 按域前缀现造
#: （本域是 `ap_schema_migrate`）。几个域的迁移嵌套跑时保存点不能重名。
_MIGRATE_SAVEPOINT = _CASE_STORE.savepoint


# ------------------------------------------------------------------ schema 迁移
#: 迁移步骤表，**按版本号升序**，每项是 `(版本号, 说明, 步骤函数)`，
#: 步骤函数签名 `step(store, script)`（`script` 是 schema.sql 原文）。
#:
#: **留在本域不进骨架**：各域的迁移步骤是各域自己的历史，没有一步是共通的。
#: 骨架只提供步骤要用的两个原语 `_atomic` / `_has_column`。
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


#: 把本域的迁移入口挂回骨架 —— `ensure_schema()` 建完表之后调的就是它。
#: **两段，缺一不可**：`schema.sql` 那段全是 `IF NOT EXISTS`，只管「表不在就建」，
#: 对已经存在的表一个字都改不动；`_migrate()` 那段才管「表在但形状旧」。
_CASE_STORE.migrate = _migrate


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
#: object_type -> (表名, 主键列名)。本域自己的取值域，与别的域那张表互不相干；
#: `resolve_business_ref()` 在骨架里，取值域挂在本域自己身上。
_REF_TARGETS: dict[str, tuple[str, str]] = {
    "ap_case":          ("ap_case", "case_id"),
    "purchase_order":   ("purchase_order", "po_id"),
    "goods_receipt":    ("goods_receipt", "gr_id"),
    "supplier_invoice": ("supplier_invoice", "invoice_id"),
    "payment_instruction": ("payment_instruction", "instruction_id"),
}
_VERSIONED_REF_TABLES = {"purchase_order"}

_CASE_STORE.ref_targets = _REF_TARGETS
_CASE_STORE.versioned_ref_tables = _VERSIONED_REF_TABLES
