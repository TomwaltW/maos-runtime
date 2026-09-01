"""采购退货域的靶场数据 —— **源单据与退货案子的唯一构造路径**。

场景（`maos/flows/scenario_11.py`，T64 的产出）与测试（`maos/tests/test_rtv_*.py`）
都从这里落数据，不各写一份。理由与 `maos/flows/common.py` 抬头那句一样：
**留第二条构造路径，两条一定会漂**。漂了之后的症状很难认 —— 测试全绿而场景红，
或者反过来，而两边看起来都在造「同一套退货单据」。

## 🔴 这里**不预置**贷项通知单与到账回执

`credit_note` 与 `rtv_settlement_observation` 两张表是**外部权威事实**（铁律 8）。
预置一行等于「演示开始前供应商就已经认账了」，把本域要证明的那件事直接架空 ——
整场演示会变成「MAOS 把自己写进去的东西又读了出来」。

这两张表也确实**写不进来**：`objects.execute()` 对它们的写入一律拒
（`objects._GUARDED_TABLES`），唯一入口是 `guard.update_biz_status()`。
所以这条不是靠自觉，是机器挡着的。

要演失败路径（供应商收到货但不认账），让 stub 工具回 `acknowledged` ——
守卫第 ④ 道会拒，案子推不到 `credited`，这正是要给评委看的东西。

## 上游五张表不归这里建

`seed_supplier()` / `seed_source_documents()` 写的是 `ap` 域持有的那五张表，
本域**只引用不重建**（见 `schema.sql` 抬头）。调用前必须先让持有方把 schema 建出来，
否则 `objects.require_upstream_tables()` 会抛 `UpstreamSchemaMissing` 并指出去处。
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from . import objects

CENTS = Decimal("0.01")

#: 一行源单据的形状。六元组，顺序即「订单 -> 收货」：
#:
#:     (line_no, sku, 订单数量, 订单单价, 收货到货数, 收货不合格数)
#:
#: 用元组而不是 dataclass：靶场数据在场景与用例里是**成排写**的，
#: 元组一行一条读得出对齐关系，dataclass 会让同样的信息占五行。
#: 口径同 ap 域 `fixtures.ThreeWayLine`，少了发票那两列 —— 本域没有发票。
SourceLine = tuple


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def seed_supplier(store: Any, *, tenant_id: str, supplier_id: str, name: str,
                  payment_means_code: str = "30", payment_terms: str = "",
                  bank_account: str = "") -> None:
    """供应商主数据。写的是 `ap` 域持有的 `supplier` 表，只引用不重建。"""
    objects.ensure_schema(store)
    objects.require_upstream_tables(store)
    objects.execute(
        store,
        "INSERT OR REPLACE INTO supplier (tenant_id, supplier_id, name,"
        " payment_means_code, payment_terms, bank_account) VALUES (?,?,?,?,?,?)",
        (tenant_id, supplier_id, name, payment_means_code, payment_terms, bank_account))


def seed_source_documents(
    store: Any,
    *,
    tenant_id: str,
    supplier_id: str,
    po_id: str,
    gr_id: str,
    lines: list[SourceLine],
    ordered_at: str,
    currency: str = "CNY",
    po_version: int = 1,
    warehouse: str = "WH-1",
) -> dict:
    """落一张 PO（带行）与一张收货单（带行）。返回可退明细的汇总。

    两份单据都是**外部系统里读到的那一版**（`read_at` 记下读的时刻），
    不是那些系统的当前值 —— 这是铁律 8 在数据层的样子。

    收货行的 `quantity_rejected` 就是本域的可退来源：验收不合格的那部分该退货，
    合格的那部分该付钱（ap 域的 `accepted_quantity()`）。两者相加等于到货数。
    """
    objects.ensure_schema(store)
    objects.require_upstream_tables(store)
    objects.execute(
        store,
        "INSERT OR REPLACE INTO purchase_order (tenant_id, po_id, version, supplier_id,"
        " currency, ordered_at, payload_json, read_at) VALUES (?,?,?,?,?,?,?,?)",
        (tenant_id, po_id, po_version, supplier_id, currency, ordered_at, "{}", _now()))
    objects.execute(
        store,
        "INSERT OR REPLACE INTO goods_receipt (tenant_id, gr_id, po_id, po_version,"
        " received_at, warehouse, payload_json, read_at) VALUES (?,?,?,?,?,?,?,?)",
        (tenant_id, gr_id, po_id, po_version, ordered_at, warehouse, "{}", _now()))

    rejected: list[dict] = []
    for line_no, sku, po_qty, po_price, gr_recv, gr_rej in lines:
        objects.execute(
            store,
            "INSERT OR REPLACE INTO purchase_order_line (tenant_id, po_id, version,"
            " line_no, sku, quantity, unit_price, tax_category_code, tax_rate)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (tenant_id, po_id, po_version, line_no, sku, po_qty,
             objects.money_str(po_price), "S", 13))
        objects.execute(
            store,
            "INSERT OR REPLACE INTO goods_receipt_line (tenant_id, gr_id, line_no, sku,"
            " quantity_received, quantity_rejected) VALUES (?,?,?,?,?,?)",
            (tenant_id, gr_id, line_no, sku, gr_recv, gr_rej))
        if float(gr_rej) > 0:
            rejected.append({"gr_line_no": line_no, "sku": sku,
                             "quantity_returned": float(gr_rej),
                             "unit_price": objects.money_str(po_price)})

    if not rejected:
        # 一条不合格行都没有的靶场数据造不出退货案子，而症状会出现在很后面
        # （建案成功、金额 0.00、对账「对上了」）。在造数据这一步就响。
        raise ValueError(
            "这套源单据一行 quantity_rejected 都没有，造不出 RTV 案子；"
            "至少让一行的收货不合格数 > 0"
        )
    return {"lines": rejected,
            "amount_claimed": str(_sum_amount(rejected))}


def _sum_amount(rejected: list[dict]) -> Decimal:
    total = Decimal("0")
    for line in rejected:
        total += (objects.money(line["quantity_returned"])
                  * objects.money(line["unit_price"])).quantize(CENTS)
    return total


def intake_kwargs(
    *,
    tenant_id: str,
    case_id: str,
    supplier_id: str,
    po_id: str,
    gr_id: str,
    amount_claimed: Any,
    plan_id: str,
    actor_skill: str = "rtv.intake",
    invocation_id: str = "iv-rtv-intake",
    po_version: int = 1,
    currency: str = "CNY",
) -> dict:
    """一个**待建**的 RTV 案子的入参 —— 直接展开给 `guard.create_case()`。

    刻意只造入参、不替调用方建案：建案要经守卫，靶场不该有第二条建案路径
    （`biz_status` 不接受指定，正是那条路必须走守卫的原因）。
    """
    return {
        "tenant_id": tenant_id, "case_id": case_id, "supplier_id": supplier_id,
        "po_id": po_id, "po_version": po_version, "gr_id": gr_id,
        "amount_claimed": objects.money_str(amount_claimed), "plan_id": plan_id,
        "actor_skill": actor_skill, "invocation_id": invocation_id,
        "currency": currency,
    }


def seed_return_lines(store: Any, *, tenant_id: str, case_id: str,
                      rejected: list[dict], reason_code: str) -> list[dict]:
    """按 `seed_source_documents()` 算出来的可退明细落退货行。

    `reason_code` 由调用方给：取值域在 `maos/tools/rtv_codes.py::RETURN_REASONS`
    （T62 的产出），本域不持有那份码表（见 `objects.add_return_line`）。
    """
    rows = []
    for idx, line in enumerate(rejected, start=1):
        rows.append(objects.add_return_line(
            store, tenant_id=tenant_id, case_id=case_id, line_no=idx,
            gr_line_no=line["gr_line_no"], sku=line["sku"],
            quantity_returned=line["quantity_returned"],
            unit_price=line["unit_price"], reason_code=reason_code))
    return rows


#: SOP 五步跑得通的一套默认靶场数据。场景与测试都从这里取，改一处两边同时变。
#:
#: 两行货，第 2 行验收不合格 3 件 —— 「到货 10 件、其中 3 件不合格」是 RTV 最常见的
#: 起因（PeopleSoft 口径里收货单的四个量本来就分开记）。第 1 行全合格，
#: 用来钉住「合格的那部分不该进退货单」。
DEMO_TENANT = "tnt-rtv-demo"
DEMO_SUPPLIER = "SUP-RTV-1"
DEMO_PO = "PO-RTV-1"
DEMO_GR = "GR-RTV-1"
DEMO_LINES: list[SourceLine] = [
    (1, "SKU-A", 20, "50.00", 20, 0),
    (2, "SKU-B", 10, "120.00", 10, 3),
]
