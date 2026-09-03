"""业务方入口（`scripts/run_requests.py`）的守卫 —— 一张 CSV 进，一张结果表出。

这条入口卖的是：**不写代码的人只需要给四列**（订单号、诉求类型、申报金额、申请日期），
其余全部从底账查。所以要钉住的正是「其余全部查出来」和「查不到 / 看不懂时当场喊」：

  1. 中文诉求类型认得（质量问题 / 七天无理由 / 发错货），看不懂的**报错不猜** ——
     猜错一个词，套用的就是另一条政策；
  2. 只给订单号，租户 / 渠道 / SKU / 订单版本全部从底账补齐，金额留空取订单实付；
  3. 日期补时区。`2026-07-10` 是 naive，订单 `paid_at` 带时区，不补就在流程中段炸；
  4. 订单号不在底账里 -> 当场报错，不静默跳过（跳过等于这一单悄悄没处理）；
  5. 端到端：样例三单跑出「批准 2 / 驳回 1」，且驳回那单一分钱都没退。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CUSTOM = ROOT / "scenarios" / "custom"


def _load_script(name: str):
    key = f"_test_{name}"
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


rr = _load_script("run_requests")


@pytest.fixture(scope="module")
def ledger() -> dict:
    from maos.flows.custom_case import load
    return load(CUSTOM / "ledger.json", require_case=False)


def test_sheet_reads_chinese_reasons_and_blank_amount():
    rows = rr.read_sheet(CUSTOM / "refund-requests.csv")

    assert len(rows) == 3
    assert [r["reason"] for r in rows] == [
        "quality_defect", "no_reason_return", "quality_defect"]
    assert rows[2]["amount"] is None, "金额留空要保持 None，取实付是下一步的事"
    # 补时区：不补的话与订单 paid_at 相减当场 TypeError，且报错指不到填表的人
    assert rows[0]["requested_at"].endswith("+00:00")


def test_unknown_reason_is_refused_not_guessed():
    with pytest.raises(rr.RequestSheetError, match="看不懂的诉求类型"):
        rr._reason_code("客户心情不好")
    with pytest.raises(rr.RequestSheetError, match="看不懂的日期"):
        rr._iso("上个月")


def test_order_id_is_the_only_key_a_human_types(ledger):
    req = {"order_id": "ORD-2026-0003", "reason": "quality_defect",
           "amount": None, "requested_at": "2026-08-25T00:00:00+00:00",
           "reason_raw": "质量问题", "note": ""}

    case = rr.build_case(ledger, req)["case"]

    # 这四个字段人一个都没填，全部从 order_snapshot 查出来
    assert case["tenant_id"] == "tnt-demo"
    assert case["channel_id"] == "ch-online"
    assert case["sku"] == "SKU-CPL-330"
    assert case["order_version"] == 1
    assert case["amount_claimed"] == 24000.0, "金额留空 = 按订单实付"
    assert case["case_id"] == "RC-ORD-2026-0003"


def test_unknown_order_is_refused(ledger):
    req = {"order_id": "ORD-NOT-THERE", "reason": "quality_defect", "amount": 1.0,
           "requested_at": "2026-08-25T00:00:00+00:00", "reason_raw": "质量问题", "note": ""}
    with pytest.raises(rr.RequestSheetError, match="底账里没有订单"):
        rr.build_case(ledger, req)


def test_sample_sheet_end_to_end():
    rows = rr.run_sheet(CUSTOM / "refund-requests.csv", CUSTOM / "ledger.json")

    by_order = {r["order_id"]: r for r in rows}
    assert by_order["ORD-2026-0001"]["decision"] == "approve"
    assert by_order["ORD-2026-0001"]["amount_approved"] == "6800.00"
    assert by_order["ORD-2026-0001"]["status"] == "settled"

    # 超窗那一单：驳回，且一分钱都没退 —— 核准金额 0.00 且业务状态没进 settled
    rejected = by_order["ORD-2026-0002"]
    assert rejected["decision"] == "reject"
    assert rejected["amount_approved"] == "0.00"
    assert rejected["status"] != "settled"

    # 金额留空那一单按订单实付核算
    assert by_order["ORD-2026-0003"]["amount_approved"] == "24000.00"

    assert "批准 2、驳回 1" in rr.summarize(rows)
    assert "订单号" in rr.as_table(rows)
