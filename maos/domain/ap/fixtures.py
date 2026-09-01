"""应付账款域的靶场数据 —— **三单的唯一构造路径**。

场景（`maos/flows/scenario_10.py`）与测试（`maos/tests/test_ap_*.py`）都从这里落数据，
不各写一份。理由与 `maos/flows/common.py` 抬头那句一样：**留第二条构造路径，
两条一定会漂**。漂了之后的症状很难认 —— 测试全绿而场景红，或者反过来，
而两边看起来都在造「同一套三单」。

## 发票的四个合计是**算出来的**，不是手填的

`seed_three_way()` 按 EN 16931 的算式现算 `line_net_total` / `total_excl_vat` /
`total_vat` / `total_incl_vat` / `amount_due` 再写库。手填的数字与行明细对不上时，
症状是一条**本来不该出现的拒付理由**，而排查方向会指向匹配逻辑 —— 那是最费时间
的一种误导。

## 要造一张「有问题的发票」怎么办

用 `header_overrides` 显式覆盖某个合计字段。这是刻意做成**显式**的：
测试要演「BR-CO-15 勾稽不上」，就得明写 `header_overrides={"total_incl_vat": ...}`，
读用例的人一眼看到破坏点在哪，而不是去数某个魔法参数改了哪一位。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from . import objects

CENTS = Decimal("0.01")

#: 一行三单的形状。八元组，顺序即「订单 -> 收货 -> 发票」：
#:
#:     (line_no, sku, 订单数量, 订单单价, 收货到货数, 收货不合格数, 发票数量, 发票单价)
#:
#: 用元组而不是 dataclass：靶场数据在场景与用例里是**成排写**的，
#: 元组一行一条读得出对齐关系，dataclass 会让同样的信息占五行。
ThreeWayLine = tuple


def seed_supplier(store: Any, *, tenant_id: str, supplier_id: str, name: str,
                  payment_means_code: str, payment_terms: str = "",
                  bank_account: str = "") -> None:
    """供应商主数据。`payment_means_code` 取 UNCL4461，会被 ap.plan-payment 验。"""
    objects.ensure_schema(store)
    objects.execute(
        store,
        "INSERT OR REPLACE INTO supplier (tenant_id, supplier_id, name,"
        " payment_means_code, payment_terms, bank_account) VALUES (?,?,?,?,?,?)",
        (tenant_id, supplier_id, name, payment_means_code, payment_terms, bank_account))


def seed_three_way(
    store: Any,
    *,
    tenant_id: str,
    supplier_id: str,
    po_id: str,
    gr_id: str,
    invoice_id: str,
    lines: list[ThreeWayLine],
    tax_category: str,
    tax_rate: float,
    invoice_type: str,
    issued_at: str,
    due_at: str = "",
    currency: str = "CNY",
    po_version: int = 1,
    prepaid: str = "0",
    header_overrides: dict[str, Any] | None = None,
) -> dict:
    """落一套三单，返回算出来的四个合计。

    三份单据都是**外部系统里读到的那一版**（`read_at` 记下读的时刻），
    不是那些系统的当前值 —— 这是铁律 8 在数据层的样子。
    """
    objects.ensure_schema(store)
    objects.execute(
        store,
        "INSERT OR REPLACE INTO purchase_order (tenant_id, po_id, version, supplier_id,"
        " currency, ordered_at, payload_json, read_at) VALUES (?,?,?,?,?,?,?,?)",
        (tenant_id, po_id, po_version, supplier_id, currency, issued_at, "{}",
         _now(store)))
    objects.execute(
        store,
        "INSERT OR REPLACE INTO goods_receipt (tenant_id, gr_id, po_id, po_version,"
        " received_at, warehouse, payload_json, read_at) VALUES (?,?,?,?,?,?,?,?)",
        (tenant_id, gr_id, po_id, po_version, issued_at, "WH-1", "{}", _now(store)))

    line_net_total = Decimal("0")
    for (line_no, sku, po_qty, po_price, gr_recv, gr_rej,
         inv_qty, inv_price) in lines:
        objects.execute(
            store,
            "INSERT OR REPLACE INTO purchase_order_line (tenant_id, po_id, version,"
            " line_no, sku, quantity, unit_price, tax_category_code, tax_rate)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (tenant_id, po_id, po_version, line_no, sku, po_qty, po_price,
             tax_category, tax_rate))
        objects.execute(
            store,
            "INSERT OR REPLACE INTO goods_receipt_line (tenant_id, gr_id, line_no, sku,"
            " quantity_received, quantity_rejected) VALUES (?,?,?,?,?,?)",
            (tenant_id, gr_id, line_no, sku, gr_recv, gr_rej))
        net = (objects.money(inv_qty) * objects.money(inv_price)).quantize(CENTS)
        line_net_total += net
        objects.execute(
            store,
            "INSERT OR REPLACE INTO supplier_invoice_line (tenant_id, invoice_id,"
            " line_no, sku, quantity, unit_price, line_net, tax_category_code, tax_rate)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (tenant_id, invoice_id, line_no, sku, inv_qty, inv_price, str(net),
             tax_category, tax_rate))

    vat = (line_net_total * objects.money(tax_rate) / Decimal("100")).quantize(CENTS)
    incl = (line_net_total + vat).quantize(CENTS)
    due = (incl - objects.money(prepaid)).quantize(CENTS)

    header = {
        "invoice_type_code": invoice_type,
        "line_net_total": str(line_net_total),
        "total_excl_vat": str(line_net_total),
        "total_vat": str(vat),
        "total_incl_vat": str(incl),
        "prepaid_amount": str(prepaid),
        "amount_due": str(due),
    }
    unknown = sorted(set(header_overrides or {}) - set(header))
    if unknown:
        # 覆盖一个不存在的字段是静默无效的 —— 用例会以为造了一张坏发票，
        # 实际造的是一张好发票，而断言「应该有拒付理由」当场变红且原因难认。
        raise ValueError(f"header_overrides 里有未知字段 {unknown}；可覆盖的是 "
                         f"{sorted(header)}")
    header.update(header_overrides or {})

    objects.execute(
        store,
        "INSERT OR REPLACE INTO supplier_invoice (tenant_id, invoice_id, supplier_id,"
        " po_id, invoice_type_code, currency, issued_at, due_at, line_net_total,"
        " total_excl_vat, total_vat, total_incl_vat, prepaid_amount, amount_due,"
        " payload_json, read_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (tenant_id, invoice_id, supplier_id, po_id, header["invoice_type_code"],
         currency, issued_at, due_at, header["line_net_total"],
         header["total_excl_vat"], header["total_vat"], header["total_incl_vat"],
         header["prepaid_amount"], header["amount_due"], "{}", _now(store)))
    return header


def _now(_store: Any) -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
