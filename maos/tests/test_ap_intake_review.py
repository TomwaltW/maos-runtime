"""待复核表（`maos/flows/ap_intake_review.py`）的守卫 —— 图片进付款链路前的那道闸门。

这一层卖的是**一条不许被绕过的规则**：抽取结果不直接进付款决策（跨轨契约 R1）。
所以要钉住的就是「闸门关得死」和「对不上时不猜」：

  1. 🔴 置信度全 high 的一张发票，`需人工确认` 仍是「是」 —— 这是本轨的钉子。
     置信度高只让它排在后面，不让它自动放行；
  2. 采购单号底账里没有 -> 标「底账无此采购单」，不静默跳过（跳过等于这一行没核对）；
  3. `PO-2026-000l` **不**被匹配到 `PO-2026-0001` —— 差一个字符就是另一个供应商；
  4. 字段抽空 -> 单元格留空 + 标「未抽到」，不填默认值（填了就分不清「模型没看清」
     和「发票上真的是这个值」）；
  5. 底账文件不存在 -> 当场报错并说清要哪个文件，不降级成「跳过核对」；
  6. 低置信的行排在前面 —— 人的注意力有限，最可能错的先看。

全部离线：抽取结果一律用假数据构造，**不 import T71 的 `maos/tools/invoice_extract.py`**
（并行期它根本不存在），一行网络都不走。
"""

from __future__ import annotations

import csv
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from maos.flows import ap_intake_review as air

ROOT = Path(__file__).resolve().parents[2]


def _load_script(name: str):
    key = f"_test_{name}"
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


ri = _load_script("review_invoices")

#: 底账（契约 §1.1 的三段），只留核对用得上的那些字段。
LEDGER = {
    "suppliers": [{"tenant_id": "T-01", "supplier_id": "SUP-A", "name": "杭州甲电子",
                   "payment_means_code": "42", "payment_terms": "NET30"}],
    "purchase_orders": [
        {"po_id": "PO-2026-0001", "version": 1, "supplier_id": "SUP-A", "currency": "CNY",
         "lines": [{"line_no": 1, "sku": "SKU-A", "quantity": 100, "unit_price": "12.50"},
                   {"line_no": 2, "sku": "SKU-B", "quantity": 50, "unit_price": "30.00"}]},
    ],
    "goods_receipts": [],
}

ALL_HIGH = {"发票号": "high", "采购单号": "high", "开票日期": "high", "到期日": "high",
            "税率": "high", "行号": "high", "SKU": "high", "数量": "high", "单价": "high"}


def _invoice(*, po: str = "PO-2026-0001", source: str = "images/inv-0001.png",
             fields: dict | None = None, lines: list | None = None,
             confidence: dict | None = None) -> air.ExtractedInvoice:
    """造一张抽取结果。默认是一张**字段齐全、置信度全 high** 的干净发票。"""
    base = {"发票号": "INV-2026-0001", "采购单号": po,
            "开票日期": "2026-07-25", "到期日": "2026-08-24", "税率": "0.13"}
    base.update(fields or {})
    return air.ExtractedInvoice(
        source=source, fields=base,
        lines=[{"行号": "1", "SKU": "SKU-A", "数量": "100", "单价": "12.50"}]
        if lines is None else lines,
        confidence=dict(ALL_HIGH, **(confidence or {})),
        raw='{"模型原始返回": "..."}')


# ------------------------------------------------------- 🔴 R1：本轨的钉子测试
def test_r1_all_high_confidence_still_needs_human_review():
    """字段齐全、逐字段 high —— `需人工确认` 仍然是「是」。

    这是整轨存在的理由。视觉模型跑的是 `deepseek-v4-flash-vision-exp`（名字里
    就写着实验版），它说自己有把握不等于它对。一旦这里能写出「否」，
    闸门就等于交给模型自己开了 —— 与自然语言层的 R1、与铁律 8 同源：
    **理解可以错，授权不许错。**
    """
    rows = air.build_review([_invoice()], LEDGER)

    assert len(rows) == 1
    row = rows[0]
    assert row.low_fields == (), "这张发票逐字段 high，不该有低置信字段"
    assert row.ledger_status == air.LEDGER_OK, "采购单号在底账里查得到"
    assert row.needs_review == "是"
    assert row.cells["需人工确认"] == "是"


def test_r1_no_code_path_ever_writes_no():
    """连「否」这个值都只该由人写出来 —— 代码里恒为 `NEEDS_REVIEW`。

    把置信度全部拉到 high、底账全对得上、什么毛病都没有的一批，逐行查一遍：
    只要有任何一条捷径能写出「否」，这条断言就红。
    """
    clean = [_invoice(source=f"images/inv-{i}.png") for i in range(5)]
    rows = air.build_review(clean, LEDGER)

    assert air.NEEDS_REVIEW == "是"
    assert {r.cells["需人工确认"] for r in rows} == {"是"}


# --------------------------------------------------------------- 与底账核对
def test_po_missing_from_ledger_is_marked_not_guessed():
    rows = air.build_review([_invoice(po="PO-9999-8888")], LEDGER)

    row = rows[0]
    assert row.ledger_status == air.LEDGER_PO_MISSING
    assert air.MARK_PO_MISSING in row.cells["说明"]
    assert "PO-9999-8888" in row.cells["说明"], "说明里要写清是哪一张单查不到"
    assert row.cells["采购单号"] == "PO-9999-8888", "查不到不等于要把人抽到的值抹掉"
    assert row.cells["需人工确认"] == "是"


def test_lookalike_po_is_not_fuzzy_matched():
    """`PO-2026-000l`（末位小写 L）不许被匹配到 `PO-2026-0001`（末位数字 1）。

    两张单子背后是两个供应商。猜「最像的那个」就是把钱付给另一个人，
    而且一路绿灯不报错 —— 这类 bug 最贵，所以这里连一次编辑距离都不许算。
    """
    rows = air.build_review([_invoice(po="PO-2026-000l")], LEDGER)

    row = rows[0]
    assert row.ledger_status == air.LEDGER_PO_MISSING
    assert f"{air.MARK_PO_MISSING}：PO-2026-000l" in row.cells["说明"]
    # 没有被悄悄换成底账里那张
    assert row.cells["采购单号"] == "PO-2026-000l"
    assert "PO-2026-0001" not in row.cells["说明"]
    assert "订单数量" not in row.cells["说明"], "没匹配上就不该带出任何订单侧数据"


def test_matched_po_brings_order_side_numbers_for_comparison():
    """对得上时，把订单侧数量单价并进来，人一眼能和发票上的数对照。"""
    rows = air.build_review([_invoice()], LEDGER)

    note = rows[0].cells["说明"]
    assert "订单数量 100" in note and "订单单价 12.50" in note


def test_line_number_absent_from_po_is_marked():
    """采购单在、但没有这一行号 —— 也要说出来，不许当成对上了。"""
    rows = air.build_review(
        [_invoice(lines=[{"行号": "7", "SKU": "SKU-A", "数量": "20", "单价": "12.50"}])],
        LEDGER)

    row = rows[0]
    assert row.ledger_status == air.LEDGER_LINE_MISSING
    assert air.MARK_LINE_MISSING in row.cells["说明"]


def test_blank_field_stays_blank_and_is_marked():
    """抽空的字段留空 + 标「未抽到」，**不填默认值**。

    填了默认值，人就分不清「模型没看清」和「发票上真的是这个值」——
    而这两件事对下一步该干什么的指示完全相反。
    """
    rows = air.build_review(
        [_invoice(fields={"开票日期": "", "税率": None},
                  lines=[{"行号": "1", "SKU": "SKU-A", "数量": "100", "单价": ""}])],
        LEDGER)

    row = rows[0]
    assert row.cells["开票日期"] == ""
    assert row.cells["税率"] == ""
    assert row.cells["发票单价"] == ""
    assert air.MARK_NOT_EXTRACTED in row.cells["说明"]
    for name in ("开票日期", "税率", "发票单价"):
        assert name in row.cells["说明"], f"{name} 没抽到要点名，人才知道去看哪一格"


def test_invoice_without_any_line_still_produces_a_row():
    """一行都没抽到的发票也要落一行 —— 少一行 = 这张发票悄悄没人看。"""
    rows = air.build_review([_invoice(lines=[])], LEDGER)

    assert len(rows) == 1
    assert f"{air.MARK_NOT_EXTRACTED}：行项目" in rows[0].cells["说明"]
    assert rows[0].cells["行号"] == ""
    assert rows[0].cells["需人工确认"] == "是"


def test_missing_ledger_file_raises_and_names_the_file(tmp_path):
    """底账不在 -> 报错退出，**不静默跳过核对**。"""
    missing = tmp_path / "没有这个 ap-ledger.json"
    with pytest.raises(air.LedgerError) as exc:
        air.load_ledger(missing)

    assert str(missing) in str(exc.value), "报错要说清楚缺的是哪个文件"
    assert "ap-ledger.json" in str(exc.value)


def test_ledger_without_purchase_orders_is_refused(tmp_path):
    p = tmp_path / "ap-ledger.json"
    p.write_text(json.dumps({"suppliers": [], "goods_receipts": []}), encoding="utf-8")
    with pytest.raises(air.LedgerError, match="purchase_orders"):
        air.load_ledger(p)


def test_ledger_takes_highest_version_of_a_po():
    """同号多版本取版本号最大的那一版 —— 别的版本不参与匹配。"""
    ledger = {"purchase_orders": [
        {"po_id": "PO-A", "version": 1, "lines": [{"line_no": 1, "quantity": 1, "unit_price": "1"}]},
        {"po_id": "PO-A", "version": 3, "lines": [{"line_no": 1, "quantity": 9, "unit_price": "9"}]},
    ]}
    assert air.index_purchase_orders(ledger)["PO-A"]["version"] == 3


# ------------------------------------------------------------------- 排序
def test_low_confidence_rows_come_first():
    """低置信排前面 —— 但这只改变顺序，不改变任何一行的放行结论。"""
    clean = _invoice(source="images/clean.png")
    shaky = _invoice(source="images/shaky.png",
                     confidence={"税率": "low", "单价": "low", "到期日": "low"})
    rows = air.build_review([clean, shaky], LEDGER)

    assert [r.source for r in rows] == ["images/shaky.png", "images/clean.png"]
    assert len(rows[0].low_fields) == 3 and rows[1].low_fields == ()
    assert {r.cells["需人工确认"] for r in rows} == {"是"}, "排序不是放行"


def test_missing_confidence_entry_counts_as_low():
    """抽取器没给置信度的字段按 low 记 —— 没表态不等于有把握。"""
    inv = air.ExtractedInvoice(
        source="images/no-conf.png",
        fields={"发票号": "INV-1", "采购单号": "PO-2026-0001", "开票日期": "2026-07-25",
                "到期日": "2026-08-24", "税率": "0.13"},
        lines=[{"行号": "1", "SKU": "SKU-A", "数量": "100", "单价": "12.50"}],
        confidence={})
    row = air.build_review([inv], LEDGER)[0]

    assert set(row.low_fields) == {"发票号", "采购单号", "开票日期", "到期日", "税率",
                                   "行号", "SKU", "发票数量", "发票单价"}


def test_per_line_confidence_override():
    """行级置信度可以逐行区分：键写成 `<行号>.<字段>`。"""
    inv = _invoice(lines=[{"行号": "1", "SKU": "SKU-A", "数量": "100", "单价": "12.50"},
                          {"行号": "2", "SKU": "SKU-B", "数量": "50", "单价": "30.00"}],
                   confidence={"2.单价": "low"})
    rows = air.build_review([inv], LEDGER)

    assert rows[0].cells["行号"] == "2" and rows[0].low_fields == ("发票单价",)
    assert rows[1].cells["行号"] == "1" and rows[1].low_fields == ()


# --------------------------------------------------------------- 表与摘要
def test_csv_columns_match_the_contract(tmp_path):
    """列 = 申请表十列 + `来源` + `需人工确认`，一列不多一列不少（契约 §1.4）。

    多开一列，人改完「是」→「否」的那张表就喂不进付款入口了 —— 这道闸门
    只有在「同一张表能直接往下走」时才用得起来。
    """
    out = air.write_review_csv(air.build_review([_invoice()], LEDGER), tmp_path / "待复核.csv")
    with out.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))

    assert tuple(rows[0]) == air.REVIEW_COLUMNS
    assert air.REVIEW_COLUMNS[:10] == air.INVOICE_COLUMNS
    assert air.REVIEW_COLUMNS[10:] == ("来源", "需人工确认")
    assert rows[1][-1] == "是"
    assert rows[1][air.REVIEW_COLUMNS.index("来源")] == "images/inv-0001.png"


def test_summary_lists_low_fields_and_ends_with_the_next_step(tmp_path):
    rows = air.build_review(
        [_invoice(), _invoice(source="images/b.png", po="PO-9999-8888",
                              confidence={"税率": "low"})], LEDGER)
    text = air.summarize(rows, images=["a.png", "b.png"],
                         extracted=[1, 2], out=tmp_path / "待复核.csv")

    assert "共 2 张图" in text and "抽到 2 张" in text
    assert "对不上 1 行" in text
    assert "税率" in text.split("低置信字段：")[1].splitlines()[0]
    assert text.strip().splitlines()[-1].startswith("下一步：")
    assert "改成「否」" in text


# ----------------------------------------------------------------- 人用入口
def _write(tmp_path: Path, name: str, payload) -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


def test_entry_end_to_end_offline(tmp_path, capsys):
    """入口整条跑通：读底账 -> 读抽取结果 -> 核对 -> 落表 -> 中文摘要。零出网。"""
    ledger = _write(tmp_path, "ap-ledger.json", LEDGER)
    extracted = _write(tmp_path, "extracted.json", [{
        "source": "images/inv-0001.png",
        "fields": {"发票号": "INV-2026-0001", "采购单号": "PO-2026-0001",
                   "开票日期": "2026-07-25", "到期日": "2026-08-24", "税率": "0.13"},
        "lines": [{"行号": "1", "SKU": "SKU-A", "数量": "100", "单价": "12.50"}],
        "confidence": ALL_HIGH, "raw": "{}"}])
    out = tmp_path / "待复核.csv"

    code = ri.main(["--ledger", str(ledger), "--extracted", str(extracted), "--out", str(out)])

    assert code == 0
    text = capsys.readouterr().out
    assert "一行都没有自动放行" in text
    assert text.strip().splitlines()[-1].startswith("下一步：")
    with out.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["需人工确认"] for r in rows] == ["是"], "全 high 的一跑也不许自动放行"


def test_entry_refuses_when_ledger_missing(tmp_path, capsys):
    extracted = _write(tmp_path, "extracted.json", [])
    code = ri.main(["--ledger", str(tmp_path / "不存在.json"),
                    "--extracted", str(extracted), "--out", str(tmp_path / "o.csv")])

    assert code == 2
    assert "找不到底账文件" in capsys.readouterr().err
    assert not (tmp_path / "o.csv").exists(), "核对不了就不该落表"


def test_entry_refuses_when_extractor_not_wired(tmp_path, capsys):
    """抽取器没接上时报错退出，**不返回空表装作抽过了**。"""
    ledger = _write(tmp_path, "ap-ledger.json", LEDGER)
    images = tmp_path / "images"
    images.mkdir()
    (images / "inv.png").write_bytes(b"not a real png")

    code = ri.main([str(images), "--ledger", str(ledger), "--out", str(tmp_path / "o.csv")])

    assert code == 3
    err = capsys.readouterr().err
    assert "抽取器还没接上" in err and "--extracted" in err
    assert not (tmp_path / "o.csv").exists()


def test_entry_does_not_import_t71():
    """本轨不许 import T71 的抽取器（并行期它不存在，import 即红）。"""
    src = (ROOT / "maos" / "flows" / "ap_intake_review.py").read_text(encoding="utf-8")
    entry = (ROOT / "scripts" / "review_invoices.py").read_text(encoding="utf-8")

    for text in (src, entry):
        assert "import maos.tools.invoice_extract" not in text
        assert "from maos.tools.invoice_extract" not in text
    assert "INTEGRATION-POINT" in src and "INTEGRATION-POINT" in entry
