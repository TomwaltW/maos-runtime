"""采购退货退款域的读写口径 —— 建表、源单据读取、业务引用。

**为什么这里自带一层 SQL 访问器**：`maos/core/store.py` 是冻结面（铁律 1），
而 `Store` 抽象基类只有 plan/task/artifact/event_log 那几个具名方法，没有通用
execute。本域的 9 张业务表（外加 1 张迁移记账表 `rtv_schema_version`）是**新增表**，
只能从 `SqliteStore` 的连接上走。因此本模块提供 `execute()` / `query()` 两个薄壳，
本域所有 SQL 都从这里过 —— store.py 一个字不改。

`execute()` 拒绝三张表的写入：

  · `rtv_case`                  —— 只有 `guard.py` 的两个入口写得动
  · `credit_note`               —— 供应商开的贷项通知单，外部权威事实
  · `rtv_settlement_observation`—— AP / 银行的到账回执，外部权威事实

后两张比 `ap` 域多守了一层：ap 的 `objects.execute()` 对
`ap_payment_observation` 不设限，它那边靠「guard 自己不走 execute」维持，
留了一条「伪造回单」的运行时后门（见 `maos/domain/ap/guard.py::record_observation`
的 docstring 原文）。本域把这条路一并堵上 —— 铁律 8 说的是权威事实不许我方写死，
那么承载权威事实的表就不该有第二条写入路径。守卫自己直连底层连接（`_conn`）
拿事务，不经过这层壳。

## 与 `maos/domain/ap/objects.py` 的关系：口径相同，**互不 import**

`_conn` / `lock_of` / `_guarded` / 迁移机制 / `money()` / `money_str()` /
`attach_business_ref()` 与 `maos/domain/ap/objects.py` 同构，是**照抄的口径**，
不是共享的实现。这份重复是**有意的**：抽成公共基类之后，那个基类就成了两个域共同
持有的面 —— 换第三个域时要动它，而动它就等于动另外两个域。
`docs/domain-portability.md` §1 那张表里 `maos/domain/` 一行标的是 ❌「按域实现」，
共用一层就把它变成 ✅ 了，而那句话本仓库给不出证据。

真正共用的是**机制的形状**（守卫、幂等、迁移探针），那是靠文档与测试传递的，
不是靠一个基类。

## 上游五张表：只读不建

`supplier` / `purchase_order` / `purchase_order_line` / `goods_receipt` /
`goods_receipt_line` 归 `ap` 域持有，本域**只引用不重建**（见 `schema.sql` 抬头）。
所以本域**不是自足的**：跑本域之前必须先让持有方把那五张表建出来。
`require_upstream_tables()` 在真正读写它们之前先探一次，缺表时抛一句指得出去处的错。
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

#: 这三张表的写入必须走 guard.py，不许经 `execute()` 旁路。
#: 与 ap 域那条正则同构，只是本域守的表多两张（见模块 docstring）。
#: ALTER TABLE 不在拦截面上 —— 「给 rtv_case 加列」这类正常迁移不受影响。
_GUARDED_TABLES = ("rtv_case", "credit_note", "rtv_settlement_observation")

_GUARDED_WRITE = re.compile(
    r"\b(?:insert\s+(?:or\s+\w+\s+)?into|update|delete\s+from|replace\s+into)\s+"
    r"[\"'`\[]?(" + "|".join(_GUARDED_TABLES) + r")[\"'`\]]?\b",
    re.IGNORECASE,
)

#: 上游源单据表。**本域不建它们**，建表责任在持有方（`maos/domain/ap/schema.sql`）。
UPSTREAM_TABLES: tuple[str, ...] = (
    "supplier", "purchase_order", "purchase_order_line",
    "goods_receipt", "goods_receipt_line",
)


class BypassedGuardError(RuntimeError):
    """有人试图绕开 `guard.py` 直接写 rtv_case 或两张权威事实表。"""


class UpstreamSchemaMissing(RuntimeError):
    """上游源单据表不在库里。

    单独一个类型而不是复用 `sqlite3.OperationalError`：缺表的正确处置是
    「让持有方先建表」，而不是「本域去把它建出来」—— 后者正是本域一整条红线
    要挡的事（`CREATE TABLE IF NOT EXISTS` 撞名是静默跳过，两份定义漂开之后
    症状离原因非常远）。给一个自己的类型，才能在测试里把这个区分钉死。
    """


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn(store: Any) -> sqlite3.Connection:
    """取底层连接。只认暴露了 `_conn` 的 Store 实现（当前是 `SqliteStore`）。"""
    conn = getattr(store, "_conn", None)
    if conn is None:
        raise TypeError(
            f"{type(store).__name__} 没有暴露 sqlite 连接，采购退货域的新增表无处落库。"
            " 换后端时在这里加一条分支，不要去改冻结的 store.py。"
        )
    return conn


def lock_of(store: Any) -> Any:
    """借 Store 自己的锁。

    `SqliteStore` 的连接是**共享**的（`check_same_thread=False` + 一把 RLock）。
    本域绕过 store.py 直接用这条连接，就必须一并用它那把锁：否则别的线程在
    `insert_task` 里一次 `commit()`，就把 guard 这边只写了贷项通知单、还没改状态的
    事务提交掉了 —— 「credited 与贷项通知单同事务」当场破，而且是偶发的。
    """
    lock = getattr(store, "_lock", None)
    return lock if lock is not None else contextlib.nullcontext()


def _guarded(sql: str) -> str:
    hit = _GUARDED_WRITE.search(sql)
    if hit:
        raise BypassedGuardError(
            f"{hit.group(1)} 的写入必须走 guard.py（create_case / update_biz_status /"
            " record_observation），不许经 objects.execute 旁路（铁律 8）"
        )
    return sql


def execute(store: Any, sql: str, params: tuple | list = ()) -> None:
    """本域的写入口径。对 `_GUARDED_TABLES` 那三张表的写入一律拒绝。"""
    conn = _conn(store)
    with lock_of(store):
        conn.execute(_guarded(sql), tuple(params))
        conn.commit()


def query(store: Any, sql: str, params: tuple | list = ()) -> list[dict]:
    """本域的读取口径。读不设限 —— 守的是写入方，不是读取方。"""
    with lock_of(store):
        rows = _conn(store).execute(sql, tuple(params)).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ 上游表探针
def missing_upstream_tables(store: Any) -> list[str]:
    """哪些上游源单据表还不在库里。全在就是空列表。"""
    rows = query(store, "SELECT name FROM sqlite_master WHERE type='table'")
    present = {r["name"] for r in rows}
    return [t for t in UPSTREAM_TABLES if t not in present]


def require_upstream_tables(store: Any) -> None:
    """读写源单据之前先探一次；缺表就抛，且把去处写在错误里。

    不在这里替持有方建表：本域重建一份「看起来一样」的定义，今天两处一模一样、
    测试全绿；等持有方哪天加一列，本域不会跟着变，而症状会离原因非常远
    （`CREATE TABLE IF NOT EXISTS` 撞名不报错，是**静默跳过**）。

    也不早于此处探：`ensure_schema()` 只管本域自己那 10 张表，不该因为上游没就位
    而建不出来 —— 那两件事互相独立。
    """
    missing = missing_upstream_tables(store)
    if missing:
        raise UpstreamSchemaMissing(
            f"上游源单据表缺失：{missing}。这些表归应付账款域持有，本域只引用不重建 —— "
            "先让持有方把 schema 建出来（maos.domain.ap.objects.ensure_schema），"
            "不要在本域的 schema.sql 里补一份定义"
        )


# ------------------------------------------------------------------ schema 迁移
#: 迁移那组「同生共死」语句用的保存点名。带域前缀：应付账款域用的是
#: `ap_schema_migrate`、退款域是 `refund_schema_migrate`，几个域的迁移嵌套跑时
#: 保存点不能重名。
_MIGRATE_SAVEPOINT = "rtv_schema_migrate"


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
#: 跑一段没人验证过的搬运，风险白担（口径同 ap 域 `_MIGRATIONS` 那段注释）。
#:
#: 加一步就在末尾追加一条，**不要改已有的那几条**。每一步都必须**自带探针**、
#: 在已是目标形状的库上是 no-op：新库靠这条（新库刚建完记账表同样是空的，
#: 只看版本号会把新库也当老库去 ALTER）。
_MIGRATIONS: tuple[tuple[int, str, Any], ...] = ()

#: 本域 schema 的当前版本。跟着 `_MIGRATIONS` 算，**不手写** —— 手写的那份迟早和
#: 实际步骤对不上，而对不上的症状是「迁移悄悄不跑了」。
RTV_SCHEMA_VERSION = max((v for v, _label, _step in _MIGRATIONS), default=0)


def applied_schema_version(store: Any) -> int:
    """这库已经升到第几版。没有记账行就是 0。"""
    rows = query(store, "SELECT MAX(version) AS version FROM rtv_schema_version")
    version = rows[0]["version"] if rows else None
    return int(version) if version is not None else 0


def _migrate(store: Any, script: str) -> None:
    """把库升到 `RTV_SCHEMA_VERSION`，并逐条记账。

    版本号是**快路径**不是判据：已经记到最新就直接返回，省掉每次都发几条探针。
    真正决定做不做的是每一步自己的探针 —— 版本表在新库上同样是空的，裸信它会把
    新库当老库。记账写在步骤之后：中途失败就不记，下次重跑同一步。
    """
    applied = applied_schema_version(store)
    if applied >= RTV_SCHEMA_VERSION:
        return
    for version, _label, step in _MIGRATIONS:
        if version <= applied:
            continue
        step(store, script)
        execute(store, "INSERT INTO rtv_schema_version (version, applied_at)"
                       " VALUES (?, ?)", (version, _now()))


def ensure_schema(store: Any) -> None:
    """建表 + 迁移到最新版本。幂等，可连跑。

    **只建本域那 10 张表**：五张上游源单据表不在这份脚本里，建表责任在持有方
    （见 `require_upstream_tables()`）。

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
#: 金额一律 Decimal，**不进 float**。理由不是洁癖：本域的核心判据是
#: 「自称应退金额 × 供应商贷项通知单金额 × 到账金额」三方相等，0.1+0.2 那种误差
#: 会直接变成一条**假的对账不平** —— 一笔完全正确的退货被判对不上，而理由挂着一个
#: 真实的规则编号，看起来毫无破绽。
#:
#: 口径同 `maos/domain/ap/objects.py::money`，逐字照抄，**不 import 跨域**。
def money(value: Any) -> Decimal:
    """把任意来源的金额折成 Decimal。解析不出来就抛，**不兜底成 0**。

    兜底成 0 的后果是「金额字段是垃圾」被静默处理成「这笔是 0 元」，而 0 元在
    对账里往往刚好对得上（0 = 0 = 0），于是垃圾数据一路绿灯过闸。
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


# ------------------------------------------------------------ 源单据读取口径
# 这几个读取函数是本域读上游五张表的**唯一**入口。对账 skill 不自己写 SQL ——
# 写了就有第二份读取口径，而两份口径迟早对「可退数怎么算」这种事产生分歧。
def get_supplier(store: Any, tenant_id: str, supplier_id: str) -> dict | None:
    require_upstream_tables(store)
    rows = query(store, "SELECT * FROM supplier WHERE tenant_id=? AND supplier_id=?",
                 (tenant_id, supplier_id))
    return rows[0] if rows else None


def get_purchase_order(store: Any, tenant_id: str, po_id: str, version: int) -> dict | None:
    require_upstream_tables(store)
    rows = query(store, "SELECT * FROM purchase_order WHERE tenant_id=? AND po_id=?"
                        " AND version=?", (tenant_id, po_id, int(version)))
    return rows[0] if rows else None


def po_lines(store: Any, tenant_id: str, po_id: str, version: int) -> list[dict]:
    require_upstream_tables(store)
    return query(store, "SELECT * FROM purchase_order_line WHERE tenant_id=? AND po_id=?"
                        " AND version=? ORDER BY line_no",
                 (tenant_id, po_id, int(version)))


def get_goods_receipt(store: Any, tenant_id: str, gr_id: str) -> dict | None:
    require_upstream_tables(store)
    rows = query(store, "SELECT * FROM goods_receipt WHERE tenant_id=? AND gr_id=?",
                 (tenant_id, gr_id))
    return rows[0] if rows else None


def gr_lines(store: Any, tenant_id: str, gr_id: str) -> list[dict]:
    """收货行。**只读** —— `quantity_received` 是审计量、永不变（PeopleSoft 口径）。

    退多少货记在 `rtv_line.quantity_returned`，不回头改收货单。改了的话，
    「当初到了多少货」这件事在库里就没有第二个地方能查，而它是审计要看的量。
    """
    require_upstream_tables(store)
    return query(store, "SELECT * FROM goods_receipt_line WHERE tenant_id=? AND gr_id=?"
                        " ORDER BY line_no", (tenant_id, gr_id))


def rejected_quantity(gr_line: dict) -> float:
    """一条收货行**验收不合格**的数量 —— RTV 的可退来源。

    与 ap 域的 `accepted_quantity()`（到货数 − 不合格数）互补，两者相加等于到货数：
    合格的那部分该付钱（ap），不合格的那部分该退货（rtv）。这个减法只在一处做，
    不散在调用点。
    """
    return float(gr_line.get("quantity_rejected") or 0)


# ------------------------------------------------------------------ 退货行
def add_return_line(
    store: Any,
    *,
    tenant_id: str,
    case_id: str,
    line_no: int,
    gr_line_no: int,
    sku: str,
    quantity_returned: float,
    unit_price: Any,
    reason_code: str,
) -> dict:
    """落一条退货行。`rtv_line` 的唯一写入口径。

    `reason_code` 的取值域在 `maos/tools/rtv_codes.py::RETURN_REASONS`（T62 的产出），
    本域**不校验它** —— 校验放在能拿到那份码表的层（ToolPort / Skill）。
    在这里塞一份「本域自己的码表」就是第二份取值域，两份一定会漂。
    """
    row = {
        "tenant_id": tenant_id, "case_id": case_id, "line_no": int(line_no),
        "gr_line_no": int(gr_line_no), "sku": sku,
        "quantity_returned": float(quantity_returned),
        "unit_price": money_str(unit_price), "reason_code": reason_code,
    }
    execute(
        store,
        "INSERT OR REPLACE INTO rtv_line (tenant_id, case_id, line_no, gr_line_no,"
        " sku, quantity_returned, unit_price, reason_code) VALUES (?,?,?,?,?,?,?,?)",
        (row["tenant_id"], row["case_id"], row["line_no"], row["gr_line_no"],
         row["sku"], row["quantity_returned"], row["unit_price"], row["reason_code"]),
    )
    return row


def rtv_lines(store: Any, tenant_id: str, case_id: str) -> list[dict]:
    return query(store, "SELECT * FROM rtv_line WHERE tenant_id=? AND case_id=?"
                        " ORDER BY line_no", (tenant_id, case_id))


def claimed_amount(lines: list[dict]) -> Decimal:
    """一组退货行的自称应退金额合计 = Σ(退货数 × 单价)。

    **这是我方自称的数，不是供应商认的数。** 供应商认的那个在
    `credit_note.amount_credited`，由 `rtv.observe` 观察得来。两者不一致正是本域
    要拦的事，所以哪怕算法一样也不合并成一处（`schema.sql` 里 `amount_claimed`
    那条注释同一个意思）。
    """
    total = Decimal("0")
    for line in lines:
        total += (money(line["quantity_returned"]) * money(line["unit_price"])
                  ).quantize(Decimal("0.01"))
    return total


# ------------------------------------------------------------ 只读查询：本域对象
def get_credit_note(store: Any, tenant_id: str, case_id: str) -> dict | None:
    """这个案子上供应商开的贷项通知单；没有就是 None。

    「没有」是有意义的答案，不是缺数据：它说明供应商还没认这笔钱，
    而那正是 `credited` 进不去的原因。
    """
    rows = query(store, "SELECT * FROM credit_note WHERE tenant_id=? AND case_id=?"
                        " ORDER BY observed_at", (tenant_id, case_id))
    return rows[0] if rows else None


def credit_notes_of(store: Any, tenant_id: str, case_id: str) -> list[dict]:
    return query(store, "SELECT * FROM credit_note WHERE tenant_id=? AND case_id=?"
                        " ORDER BY observed_at", (tenant_id, case_id))


def observations_of(store: Any, tenant_id: str, case_id: str) -> list[dict]:
    """AP / 银行侧的到账观察，按 seq 升序。"""
    return query(store, "SELECT * FROM rtv_settlement_observation WHERE tenant_id=?"
                        " AND case_id=? ORDER BY seq", (tenant_id, case_id))


def dispositions_of(store: Any, tenant_id: str, case_id: str) -> list[dict]:
    return query(store, "SELECT * FROM rtv_disposition WHERE tenant_id=? AND case_id=?"
                        " ORDER BY attempt", (tenant_id, case_id))


def shipments_of(store: Any, tenant_id: str, case_id: str) -> list[dict]:
    return query(store, "SELECT * FROM rtv_shipment WHERE tenant_id=? AND case_id=?"
                        " ORDER BY shipped_at", (tenant_id, case_id))


def reconciliations_of(store: Any, tenant_id: str, case_id: str) -> list[dict]:
    return query(store, "SELECT * FROM rtv_reconciliation WHERE tenant_id=? AND case_id=?"
                        " ORDER BY attempt", (tenant_id, case_id))


def compensations_of(store: Any, tenant_id: str, case_id: str) -> list[dict]:
    return query(store, "SELECT * FROM rtv_compensation_record WHERE tenant_id=?"
                        " AND case_id=? ORDER BY seq", (tenant_id, case_id))


def get_case(store: Any, tenant_id: str, case_id: str) -> dict | None:
    """按 (tenant_id, case_id) 读一个 case；不存在返回 None。

    读放在这里而不是 guard.py：守卫守的是**写入**，读取不设限（同 `query()`）。
    `guard.get_case` 是本函数的别名，保持与 ap / refund 两域相同的调用面。
    """
    rows = query(store, "SELECT * FROM rtv_case WHERE tenant_id=? AND case_id=?",
                 (tenant_id, case_id))
    return rows[0] if rows else None


# ------------------------------------------------------------ DAG -> 业务对象
def attach_business_ref(
    store: Any,
    *,
    plan_id: str,
    task_id: str,
    object_table: str,
    object_id: str,
    object_version: int = 0,
    purpose: str = "",
) -> dict:
    """把一个 Task 挂到一个业务对象上 —— **只存引用，不存副本**。

    存副本会立刻产生第二份事实：业务对象改了，Task 里那份不会跟着改，
    而下游分不清哪份是真的。引用只指路，读的时候一定读到当前那一份。

    口径同 `maos/domain/ap/objects.py::attach_business_ref`，但**列名按 C-R1 走**：
    本域这张表的第三列叫 `object_table` 而不是 `object_type`，且没有 `tenant_id`
    （契约冻结的形状，不许改）。所以 `resolve_business_ref()` 需要调用方把
    `tenant_id` 单独递进来。
    """
    row = {
        "plan_id": plan_id, "task_id": task_id,
        "object_table": object_table, "object_id": object_id,
        "object_version": int(object_version), "purpose": purpose,
        "created_at": _now(),
    }
    execute(
        store,
        "INSERT INTO rtv_business_ref (plan_id, task_id, object_table, object_id,"
        " object_version, purpose, created_at) VALUES (?,?,?,?,?,?,?)",
        (row["plan_id"], row["task_id"], row["object_table"], row["object_id"],
         row["object_version"], row["purpose"], row["created_at"]),
    )
    return row


def list_business_refs(store: Any, *, plan_id: str, task_id: str | None = None) -> list[dict]:
    if task_id is None:
        return query(store, "SELECT * FROM rtv_business_ref WHERE plan_id=?"
                            " ORDER BY task_id, object_table, object_id", (plan_id,))
    return query(store, "SELECT * FROM rtv_business_ref WHERE plan_id=? AND task_id=?"
                        " ORDER BY object_table, object_id", (plan_id, task_id))


#: object_table -> (表名, 主键列名)。本域自己的取值域，与别的域那几张表互不相干。
#: 上游三张（supplier / purchase_order / goods_receipt）在里面：RTV 的引用天然要
#: 指回源单据，指不回去的退货在对账时勾稽不上（`schema.sql` 里「源头三件套」那段）。
_REF_TARGETS: dict[str, tuple[str, str]] = {
    "rtv_case":       ("rtv_case", "case_id"),
    "supplier":       ("supplier", "supplier_id"),
    "purchase_order": ("purchase_order", "po_id"),
    "goods_receipt":  ("goods_receipt", "gr_id"),
    "credit_note":    ("credit_note", "credit_note_id"),
    "rtv_shipment":   ("rtv_shipment", "shipment_id"),
}
_VERSIONED_REF_TABLES = {"purchase_order"}


def resolve_business_ref(store: Any, ref: dict, *, tenant_id: str) -> dict | None:
    """按引用取回被指对象；指不到（对象不存在或版本对不上）返回 None。

    `rtv_business_ref` 不带外键，也不带 `tenant_id`（C-R1 冻结的形状）——
    它跨的是「编排层对象」与「业务对象」两个世界，完整性靠这个函数在读的时候查，
    不靠数据库替我们保证。租户由调用方显式递进来：本域所有业务表都以 `tenant_id`
    打头，少了它这条查询会跨租户命中。
    """
    table, key = _REF_TARGETS.get(ref["object_table"], (None, None))
    if table is None:
        return None
    if table in UPSTREAM_TABLES:
        # 指向上游源单据的引用，先探一次表在不在 —— 缺表时要报「让持有方建表」，
        # 而不是让 sqlite 抛一句 no such table，那句话指不出去处。
        require_upstream_tables(store)
    sql = f"SELECT * FROM {table} WHERE tenant_id=? AND {key}=?"
    params: list[Any] = [tenant_id, ref["object_id"]]
    if table in _VERSIONED_REF_TABLES:
        sql += " AND version=?"
        params.append(ref["object_version"])
    rows = query(store, sql, params)
    return rows[0] if rows else None
