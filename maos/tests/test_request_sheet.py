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


def test_missing_reason_column_is_reported_as_a_header_problem(tmp_path):
    """整列没认出来 -> 说表头，别说「第 2 行不能空」。

    两条入口同一句话（申请表进群那条复用 `scan_header` / `missing_column_message`）：
    错在表头一处，报到表头一处。逐行喊「不能空」会让人去改一堆本来填好的行。
    """
    csv_path = tmp_path / "退货订单.csv"
    csv_path.write_text("订单号,退货说明,申报金额,申请日期,备注\n"
                        "ORD-2026-0001,质量问题,6800,2026-07-10,\n", encoding="utf-8")

    with pytest.raises(rr.RequestSheetError) as exc:
        rr.read_sheet(csv_path)

    assert "表头里没有「诉求类型」这一列" in str(exc.value)
    assert "退货说明" in str(exc.value)                  # 你写的哪一列落空了
    assert "不能空" not in str(exc.value)


@pytest.mark.parametrize("name", ["诉求类型", "退货原因", "退款原因", "原因", "理由"])
def test_the_reason_column_is_recognised_by_the_names_a_boss_writes(name, tmp_path):
    """别名是**全等**匹配：「退货原因」不会因为含「原因」二字就命中「原因」那条。

    真房间里撞到过 —— 老板的表写「退货原因」，五行全被判成「诉求类型不能空」。
    """
    csv_path = tmp_path / "退货订单.csv"
    csv_path.write_text(f"订单号,{name},申报金额,申请日期,备注\n"
                        "ORD-2026-0001,质量问题,6800,2026-07-10,\n", encoding="utf-8")

    row, = rr.read_sheet(csv_path)
    assert row["reason"] == "quality_defect" and row["reason_raw"] == "质量问题"


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


# --------------------------------------------------------------- 判不准的行
# 这三条守的是一次真实发生过的回归（见 docs/DECISIONS.md 整合修复那节）：
# read_sheet 原先对词表外的诉求当场抛，改成「返回一行带 needs_human_intake 的记录」
# 之后，同文件的 run_sheet 跟着挑走了它们，而 scripts/room_team_smoke.py 这个
# 跨文件调用方没跟着改 —— 它拿到行就直接送进预检，于是一个诉求类型判不出来的单子
# 在房间演示里被基线规则**批准**了。unknown 套不上任何一条政策，
# evaluate_eligibility 走基线就是放行，而漏检查那个标记不会有任何报错。
def _sheet_with_an_unknown_reason(tmp_path):
    csv_path = tmp_path / "混着判不准的.csv"
    csv_path.write_text("订单号,诉求类型,申报金额,申请日期,说明\n"
                        "ORD-2026-0001,质量问题,6800,2026-07-10,词表内\n"
                        "ORD-2026-0002,漏发了两个,1280,2026-08-15,词表外\n",
                        encoding="utf-8")
    return csv_path


def test_pending_rows_are_not_returned_unless_asked(tmp_path):
    """缺省不返回判不准的行 —— 新调用方即使什么都不知道也是安全的。"""
    rows = rr.read_sheet(_sheet_with_an_unknown_reason(tmp_path))

    assert [r["order_id"] for r in rows] == ["ORD-2026-0001"], (
        "判不准的行漏进了缺省返回值 —— 调用方一旦不检查 needs_human_intake，"
        "它就会被送进预检并按基线规则批准")
    assert all(not r["needs_human_intake"] for r in rows)


def test_pending_rows_come_back_when_explicitly_asked(tmp_path):
    """显式要才给 —— run_sheet 要把它们印进结果表并在收口行点名。"""
    rows = rr.read_sheet(_sheet_with_an_unknown_reason(tmp_path), include_pending=True)

    assert [r["order_id"] for r in rows] == ["ORD-2026-0001", "ORD-2026-0002"]
    pending = [r for r in rows if r["needs_human_intake"]]
    assert [r["order_id"] for r in pending] == ["ORD-2026-0002"]
    assert pending[0]["reason"] == "unknown", "判不出来就是 unknown，不许猜一个 code"


def test_a_sheet_with_no_pending_rows_is_unaffected(tmp_path):
    """全在词表内时，两种调用逐字同一份结果 —— 这条守的是缺省值没有顺手改掉别的行为。"""
    csv_path = tmp_path / "全在词表内.csv"
    csv_path.write_text("订单号,诉求类型,申报金额,申请日期,说明\n"
                        "ORD-2026-0001,质量问题,6800,2026-07-10,\n",
                        encoding="utf-8")

    assert rr.read_sheet(csv_path) == rr.read_sheet(csv_path, include_pending=True)
