"""供应链付款的表格入口（`scripts/run_ap.py` + `maos/flows/ap_case.py`）的守卫。

这条入口卖的是：**不写代码的人只给一张发票明细表**，供应商 / 币种 / 订单数量单价 /
收货数量全部从底账查出来。所以要钉住的正是「其余全部查出来」和「查不到 / 对不上时
当场喊」：

  1. 一张三单对得上的发票：出付款计划、`effect_risk=H` 停 `BLOCKED` 等人批，
     放行之后 `settled` 必须由一条**带流水号**的银行观察兜底（铁律 8）；
  2. 发票数量超出收货合格数的容差 -> 匹配不过，结果里说得出**差在哪一行哪个量**，
     且不出付款计划 —— 未经验证的金额不进审批视野；
  3. 采购单号底账里没有 -> 当场报错**并列出可选项**，不猜；
  4. 同一张发票两行税率不一致 -> 报错，**不取第一行也不取多数**；
  5. 待复核表（带 `需人工确认=是`）-> 拒绝（跨轨契约 R1）；
  6. 空文件 / 缺列 -> 报错说得出缺哪一列。

外加两条源码级守卫，钉住两条红线不会在日后被悄悄绕开：
落库只有 `domain/ap/fixtures.py` 一条路径（R2），模型客户端一律 `force_scripted`。

全部离线：`run_payload` 里的模型客户端显式 `force_scripted=True`，银行是 `MockBank`，
**一行网络都不走**。
"""

from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path

import pytest

from maos.flows import ap_case
from maos.flows.ap_case import ApLedgerError, build_case, load, run_payload

ROOT = Path(__file__).resolve().parents[2]
CUSTOM = ROOT / "scenarios" / "custom"
SHEET = CUSTOM / "ap-invoices.csv"
LEDGER = CUSTOM / "ap-ledger.json"

HEADER = ("发票号,采购单号,行号,SKU,发票数量,发票单价,开票日期,到期日,税率,说明")


def _load_script(name: str):
    """按路径加载 `scripts/` 下的入口脚本 —— 那个目录不是包。口径同 test_request_sheet。"""
    key = f"_test_{name}"
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


ra = _load_script("run_ap")


@pytest.fixture(scope="module")
def ledger() -> dict:
    return load(LEDGER)


@pytest.fixture(scope="module")
def sheet() -> list[dict]:
    return ra.read_sheet(SHEET)


def _invoice(sheet: list[dict], invoice_id: str) -> dict:
    return copy.deepcopy(next(i for i in sheet if i["invoice_id"] == invoice_id))


def _write(tmp_path: Path, *lines: str) -> Path:
    p = tmp_path / "sheet.csv"
    p.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return p


# ------------------------------------------------------- 1. 表格 -> 三单的形状
def test_sheet_groups_lines_into_invoices(sheet):
    """一行 = 一个发票行项目，同发票号多行归成一张。"""
    assert [i["invoice_id"] for i in sheet] == [
        "INV-2026-0901", "INV-2026-0902", "INV-2026-0903"]
    assert len(sheet[0]["lines"]) == 2, "INV-2026-0901 是两个行项目"
    # 税率写 13% 而落到流程里是 13.0（百分数），不是 0.13 —— 差 100 倍的那种错
    assert sheet[0]["tax_rate"] == 13.0
    # 开票日期补时区：不补的话与三单里带时区的时间字段相减会在流程中段炸
    assert sheet[0]["issued_at"].endswith("+00:00")
    assert sheet[0]["due_at"] == "2026-09-18", "到期日按日期存，不带时刻"


def test_tax_rate_accepts_three_spellings_and_percent_escapes():
    assert ra._tax_rate("13") == ra._tax_rate("13%") == ra._tax_rate("0.13") == 13.0
    # 带百分号的一律按字面值算，不再乘 100 —— 真要写千分位就靠这个逃生口
    assert ra._tax_rate("0.13%") == 0.13
    with pytest.raises(ra.InvoiceSheetError, match="看不懂的税率"):
        ra._tax_rate("十三个点")


def test_build_case_fills_everything_from_ledger(ledger, sheet):
    """人只填发票号 / 行号 / SKU / 数量 / 单价，其余全部从底账查出来。"""
    payload = build_case(ledger, _invoice(sheet, "INV-2026-0901"))

    assert payload["tenant_id"] == "tnt-ap-demo"          # 表里没有，底账查出来的
    assert payload["supplier"]["supplier_id"] == "SUP-8801"
    assert payload["gr_id"] == "GR-2026-0901"             # 表里没有收货单号
    assert payload["currency"] == "CNY"
    # 八元组：顺序即「订单 -> 收货 -> 发票」，前四位来自底账、后两位来自表
    assert payload["lines"][0] == (
        1, "SKU-FLG-DN80", 120.0, "86.00", 120.0, 0.0, 120.0, "86.01")
    assert payload["lines"][1] == (
        2, "SKU-GSK-DN80", 240.0, "7.50", 240.3, 0.0, 240.0, "7.50")


def test_po_version_comes_from_ledger_not_hardcoded_one(ledger, sheet):
    """底账里 PO-2026-0903 是第 2 版 —— 引擎 DAG 里写死的 1 必须被覆盖掉。"""
    payload = build_case(ledger, _invoice(sheet, "INV-2026-0903"))
    assert payload["po_version"] == 2
    tasks = ap_case._tasks(payload, bank="b", ids=ap_case._task_ids(payload["case_id"]))
    assert {t["inputs"]["tenant_id"] for t in tasks} == {"tnt-ap-demo"}
    assert tasks[0]["inputs"]["po_version"] == 2


# --------------------------------------------- 2. 正常一张发票：停 BLOCKED 等人批
@pytest.fixture(scope="module")
def happy(ledger, sheet) -> dict:
    return run_payload(build_case(ledger, _invoice(sheet, "INV-2026-0901")))


def test_matched_invoice_plans_payment_and_waits_for_a_human(happy):
    assert happy["matched"] is True
    assert happy["findings"] == []
    assert happy["payable_amount"] == "13696.96"

    # 出账不可逆 -> effect_risk=H -> Gate 过了也停 BLOCKED 等人批。
    # human_exits 的判据取自 event_log 里进 BLOCKED 的那一跳，不是任务行上的字段。
    stops = {e["task_id"]: e for e in happy["human_exits"]}
    plan_task = f"task-{happy['case_id']}-plan"
    assert plan_task in stops, f"付款计划必须停下来等人批，实际停了 {sorted(stops)}"
    assert stops[plan_task]["effect_risk"] == "H"
    assert stops[plan_task]["why"], "停下来的理由要查得到，否则审批链断了"

    # 付出去的钱是按 BR-CO-16 算出来的那个，不是抄发票上的数字
    assert happy["payment_plan"]["amount"] == happy["payable_amount"]
    assert "BR-CO-16" in happy["plan_citations"]
    assert happy["payment_plan"]["payment_means_code"] == "30"


def test_settled_is_observed_not_asserted(happy):
    """铁律 8：settled 只可能由 ap.observe 写入，且必须带一张能对账的银行回单。"""
    assert happy["biz_status"] == "settled"
    assert happy["settled_observations"] == 1
    assert happy["payment_observations"][0]["bank_reference"], (
        "没有流水号的「已付」在财务上对不了账")
    # 终态是**问出来的**：一次 query 不一定够
    assert happy["bank_advice"]["poll_count"] == 2
    assert happy["plan_state"] == "DONE"


# --------------------------------------- 3. 超容差：说得出差在哪一行哪个量
@pytest.fixture(scope="module")
def over_tolerance(ledger, sheet) -> dict:
    return run_payload(build_case(ledger, _invoice(sheet, "INV-2026-0902")))


def test_over_tolerance_quantity_says_which_line_and_which_measure(over_tolerance):
    out = over_tolerance
    assert out["matched"] is False
    assert len(out["findings"]) == 1

    finding = out["findings"][0]
    assert finding["rule_id"] == "BR-22", "拒付理由必须挂真实存在的规则编号"
    assert finding["line_no"] == 1 and finding["sku"] == "SKU-ORING-45"
    # 差在哪个量：发票数量 vs 收货**合格数**（到货 500 − 不合格 20）
    assert finding["invoiced"] == "500.0" and finding["accepted"] == "480.0"
    assert finding["delta"] == "20.0" and finding["tolerance"] == "0.5"
    assert "第 1 行" in finding["message"] and "收货合格数" in finding["message"]


def test_unmatched_invoice_never_reaches_the_approval_view(over_tolerance):
    """匹配没过就不出计划 —— 未经验证的金额不该进入审批视野，也没人被叫醒。"""
    assert over_tolerance["payable_amount"] == ""
    assert over_tolerance["payment_plan"] is None
    assert over_tolerance["human_exits"] == []
    assert over_tolerance["biz_status"] == "received", "没匹配上就不推进业务状态"
    assert over_tolerance["settled_observations"] == 0
    assert over_tolerance["plan_state"] == "FAILED"
    assert any("不许出付款计划" in t["last_error"]
               for t in over_tolerance["failed_tasks"])


# ------------------------------------------- 4. 查不到就报错并列出可选项，不猜
def test_unknown_po_is_refused_with_the_available_options(ledger, sheet):
    invoice = _invoice(sheet, "INV-2026-0901")
    invoice["po_id"] = "PO-9999-9999"
    with pytest.raises(ApLedgerError) as exc:
        build_case(ledger, invoice)
    message = str(exc.value)
    assert "PO-9999-9999" in message
    # 列出可选项：猜一张最像的出来，套用的就是另一批数量与单价
    for po_id in ("PO-2026-0901", "PO-2026-0902", "PO-2026-0903"):
        assert po_id in message


def test_unknown_line_and_sku_mismatch_are_refused(ledger, sheet):
    missing_line = _invoice(sheet, "INV-2026-0902")
    missing_line["lines"][0]["line_no"] = 7
    with pytest.raises(ApLedgerError, match="上没有这一行"):
        build_case(ledger, missing_line)

    wrong_sku = _invoice(sheet, "INV-2026-0902")
    wrong_sku["lines"][0]["sku"] = "SKU-ORING-99"
    with pytest.raises(ApLedgerError, match="对不上就不往下走"):
        build_case(ledger, wrong_sku)


def test_ledger_missing_a_section_is_refused(tmp_path):
    bad = tmp_path / "ledger.json"
    bad.write_text('{"suppliers": [{"supplier_id": "S"}]}', encoding="utf-8")
    with pytest.raises(ApLedgerError, match="purchase_orders"):
        load(bad)


# ------------------------------ 5. 同一张发票的抬头不一致：不取第一行也不取多数
def test_inconsistent_tax_rate_within_one_invoice_is_refused(tmp_path):
    path = _write(
        tmp_path, HEADER,
        "INV-1,PO-2026-0901,1,SKU-FLG-DN80,120,86.00,2026-08-19,2026-09-18,13%,",
        "INV-1,PO-2026-0901,2,SKU-GSK-DN80,240,7.50,2026-08-19,2026-09-18,9%,")
    with pytest.raises(ra.InvoiceSheetError) as exc:
        ra.read_sheet(path)
    message = str(exc.value)
    assert "税率" in message and "不取第一行也不取多数" in message
    assert "'13%'" in message and "'9%'" in message, "两个值都要报出来，人才知道改哪个"


def test_inconsistent_issue_date_is_refused_but_same_date_spelled_differently_is_not(tmp_path):
    same = _write(
        tmp_path, HEADER,
        "INV-1,PO-2026-0901,1,SKU-FLG-DN80,120,86.00,2026-08-19,2026-09-18,13%,",
        "INV-1,PO-2026-0901,2,SKU-GSK-DN80,240,7.50,2026/08/19,2026-09-18,13%,")
    assert len(ra.read_sheet(same)[0]["lines"]) == 2, "同一天写法不同不算不一致"

    other = _write(
        tmp_path, HEADER,
        "INV-1,PO-2026-0901,1,SKU-FLG-DN80,120,86.00,2026-08-19,2026-09-18,13%,",
        "INV-1,PO-2026-0901,2,SKU-GSK-DN80,240,7.50,2026-08-20,2026-09-18,13%,")
    with pytest.raises(ra.InvoiceSheetError, match="开票日期"):
        ra.read_sheet(other)


def test_duplicate_line_number_within_one_invoice_is_refused(tmp_path):
    path = _write(
        tmp_path, HEADER,
        "INV-1,PO-2026-0901,1,SKU-FLG-DN80,120,86.00,2026-08-19,2026-09-18,13%,",
        "INV-1,PO-2026-0901,1,SKU-GSK-DN80,240,7.50,2026-08-19,2026-09-18,13%,")
    with pytest.raises(ra.InvoiceSheetError, match="出现了两次"):
        ra.read_sheet(path)


# --------------------------------------------------- 6. R1：待复核表不许直接喂
def test_sheet_with_pending_review_column_is_refused(tmp_path):
    """跨轨契约 R1：抽取结果必须先人工核对，`需人工确认=是` 一律拒绝。"""
    path = _write(
        tmp_path,
        HEADER + ",来源,需人工确认",
        "INV-1,PO-2026-0901,1,SKU-FLG-DN80,120,86.00,2026-08-19,2026-09-18,13%,,"
        "invoices/1.jpg,是")
    with pytest.raises(ra.InvoiceSheetError) as exc:
        ra.read_sheet(path)
    assert "待复核表" in str(exc.value) and "需人工确认" in str(exc.value)


def test_reviewed_sheet_with_all_no_is_accepted(tmp_path):
    """人核对完把「是」改成「否」，同一张表就能进正式入口 —— 这是契约 §1.4 的流程。"""
    path = _write(
        tmp_path,
        HEADER + ",来源,需人工确认",
        "INV-1,PO-2026-0901,1,SKU-FLG-DN80,120,86.00,2026-08-19,2026-09-18,13%,,"
        "invoices/1.jpg,否")
    assert len(ra.read_sheet(path)) == 1


def test_unrecognised_review_flag_is_refused_not_guessed(tmp_path):
    path = _write(
        tmp_path,
        HEADER + ",需人工确认",
        "INV-1,PO-2026-0901,1,SKU-FLG-DN80,120,86.00,2026-08-19,2026-09-18,13%,,待定")
    with pytest.raises(ra.InvoiceSheetError, match="只认「是」或「否」"):
        ra.read_sheet(path)


# ------------------------------------------------ 7. 空文件 / 缺列：说得出缺哪列
def test_empty_file_is_refused(tmp_path):
    path = _write(tmp_path)
    with pytest.raises(ra.InvoiceSheetError, match="空文件"):
        ra.read_sheet(path)


def test_header_only_file_is_refused(tmp_path):
    with pytest.raises(ra.InvoiceSheetError, match="一行发票都没有"):
        ra.read_sheet(_write(tmp_path, HEADER))


def test_missing_columns_are_named_one_by_one(tmp_path):
    path = _write(
        tmp_path, "发票号,采购单号,行号,SKU,发票数量,开票日期",
        "INV-1,PO-2026-0901,1,SKU-FLG-DN80,120,2026-08-19")
    with pytest.raises(ra.InvoiceSheetError) as exc:
        ra.read_sheet(path)
    message = str(exc.value)
    for column in ("发票单价", "到期日", "税率"):
        assert column in message, f"缺 {column} 要点名说出来"
    assert "发票号" not in message.split("完整的一行表头是")[0], "在的列不该被报成缺"


def test_missing_invoice_id_points_at_the_row(tmp_path):
    path = _write(
        tmp_path, HEADER,
        ",PO-2026-0901,1,SKU-FLG-DN80,120,86.00,2026-08-19,2026-09-18,13%,")
    with pytest.raises(ra.InvoiceSheetError, match="第 2 行没有发票号"):
        ra.read_sheet(path)


def test_missing_sheet_file_is_refused(tmp_path):
    with pytest.raises(ra.InvoiceSheetError, match="找不到申请表"):
        ra.read_sheet(tmp_path / "没有这个文件.csv")


# --------------------------------------------------- 8. 两条红线的源码级守卫
def test_flow_writes_no_sql_of_its_own():
    """R2：三单落库只有 `domain/ap/fixtures.py` 一条路径。

    第二条构造路径不会当场报错，它会在半年后让「同一份数据两个结论」——
    症状是测试全绿而入口红，或者反过来，而两边看起来都在造同一套三单。
    """
    source = (ROOT / "maos" / "flows" / "ap_case.py").read_text(encoding="utf-8").upper()
    for sql in ("INSERT", "UPDATE ", "DELETE ", "REPLACE INTO"):
        assert sql not in source, (
            f"ap_case.py 里出现了 {sql} —— 落库只许走 fixtures.seed_three_way")


def test_entry_never_reaches_the_network():
    """跨轨契约 §3：这台机器配着可用的 key，漏写 force_scripted 会当场打真网络。"""
    source = (ROOT / "maos" / "flows" / "ap_case.py").read_text(encoding="utf-8")
    assert "select_model_client(script, force_scripted=True)" in source
    assert source.count("select_model_client(") == 1, (
        "只该有一处调用 —— 多一处就多一个可能漏掉 force_scripted 的地方")
