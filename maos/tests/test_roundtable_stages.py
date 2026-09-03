"""五岗事实卡（`maos/roundtable/stages.py`）—— 全是规则代码，本文件一个模型都不注入。

本文件钉住的是 R1 的下半截：**事实卡里的每个数字都必须能在入参里找到**。
上半截（模型不许在事实卡之外编数字）在 `test_roundtable_team.py`。

数字白名单的口径：把入参 `json.dumps` 之后正则抽出的全部数字，加上几个显式的计数
（随案证据份数、命中规则条数、审批点个数）。用 `Decimal` 比较，`6800` / `6800.0` /
`6800.00` 视作同一个数 —— 事实卡把金额统一成两位小数，而底账里是 `6800.0`。
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path

import pytest

from maos.flows import custom_case
from maos.ingress.router import _load_run_requests, preflight
from maos.roundtable import stages
from maos.skills import registry

ROOT = Path(__file__).resolve().parents[2]
LEDGER = ROOT / "scenarios" / "custom" / "ledger.json"
ORDER = "ORD-2026-0001"
CASE_ID = "RC-ORD-2026-0001"
REQUESTED_AT = "2026-09-03T00:00:00+08:00"


# --------------------------------------------------------------------------
# 语料：真底账 + 真 build_case + 真 preflight，一个假件都不用
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def ledger() -> dict:
    return custom_case.load(str(LEDGER), require_case=False)


def _case(ledger: dict, reason: str) -> tuple[dict, dict]:
    payload = _load_run_requests().build_case(ledger, {
        "order_id": ORDER, "reason": reason, "amount": None, "requested_at": REQUESTED_AT})
    return payload, preflight(payload)


@pytest.fixture(scope="module")
def approved(ledger: dict) -> tuple[dict, dict]:
    """质量问题 → `checked["decision"] == "approve"`。"""
    return _case(ledger, "quality_defect")


@pytest.fixture(scope="module")
def rejected(ledger: dict) -> tuple[dict, dict]:
    """七天无理由、第 63 天才申请 → 超窗，`decision == "reject"`。"""
    return _case(ledger, "no_reason_return")


def _numbers(text: str) -> set[Decimal]:
    return {Decimal(t) for t in re.findall(r"\d+(?:\.\d+)?", text or "")}


def _allowed(*objs: object, extra: tuple = ()) -> set[Decimal]:
    pool: set[Decimal] = set()
    for obj in objs:
        pool |= _numbers(json.dumps(obj, ensure_ascii=False, default=str))
    pool |= {Decimal(str(x)) for x in extra}
    return pool


def _hide(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    """让注册表对指定 skill 名装作没有。

    不依赖「基线上恰好没装载」：那两个 skill 由别的轨新建，合进来之后基线就变了，
    而这几条测试要钉的是**没装载时的姿态**，不是当下的注册表内容。
    """
    real = registry.get

    def fake(name: str, version: str | None = None):
        return None if name in names else real(name, version)

    monkeypatch.setattr(registry, "get", fake)


# --------------------------------------------------------------------------
# 申请受理岗 / 规则审核岗
# --------------------------------------------------------------------------
def test_intake_facts_numbers_are_subset_of_inputs(approved: tuple[dict, dict]) -> None:
    payload, checked = approved
    facts, data = stages.facts_intake(payload, checked, 2)

    allowed = _allowed(payload, checked, extra=(2, len(checked["matched_rules"])))
    assert _numbers(facts) <= allowed, f"事实卡里出现了入参里没有的数字：{_numbers(facts) - allowed}"
    assert data["order_id"] == ORDER
    assert data["evidence_count"] == 2
    assert data["over_paid"] is False
    assert "质量问题" in facts and "SKU-BRG-6204" in facts


def test_intake_facts_warn_when_claimed_amount_is_over_paid(approved: tuple[dict, dict]) -> None:
    """申报高于实付要当场点出来 —— 核算会封顶，不说的话群里以为能退这么多。"""
    payload, checked = approved
    over = json.loads(json.dumps(payload))
    over["case"]["amount_claimed"] = 9999.0

    facts, data = stages.facts_intake(over, checked, 0)
    assert data["over_paid"] is True
    assert "封顶" in facts
    assert _numbers(facts) <= _allowed(over, checked, extra=(0, len(checked["matched_rules"])))


def test_policy_facts_numbers_are_subset_of_checked(approved: tuple[dict, dict]) -> None:
    _payload, checked = approved
    facts, data = stages.facts_policy(checked)

    allowed = _allowed(checked, extra=(len(checked["matched_rules"]),))
    assert _numbers(facts) <= allowed, f"多出来的数字：{_numbers(facts) - allowed}"
    assert set(data) == {"decision", "deciding_rule", "matched_rules", "elapsed_days",
                         "pinned_policy_version", "approver_role", "why"}
    assert "批准" in facts and "supervisor" in facts


# --------------------------------------------------------------------------
# 证据核验岗 / 风险反欺诈岗：未装载是主路径
# --------------------------------------------------------------------------
def test_evidence_facts_say_unavailable_when_skill_is_not_registered(
        approved: tuple[dict, dict], ledger: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    _hide(monkeypatch, "refund.evidence_check")
    payload, checked = approved

    facts, data = stages.facts_evidence(payload, checked, ledger)
    assert data == {"verdict": "unavailable"}
    assert "未装载" in facts
    assert "refund.evidence_check" in facts


def test_risk_facts_say_unavailable_when_skill_is_not_registered(
        approved: tuple[dict, dict], ledger: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    _hide(monkeypatch, "refund.risk_screen")
    payload, checked = approved

    facts, data = stages.facts_risk(payload, checked, ledger)
    assert data == {"level": "unavailable"}
    assert "未装载" in facts
    assert "refund.risk_screen" in facts


# --------------------------------------------------------------------------
# 财务执行岗（放行前）：核算预演
# --------------------------------------------------------------------------
def test_finance_preview_amount_equals_run_payload_amount(approved: tuple[dict, dict]) -> None:
    """**本轨最重要的接缝守卫**：预演金额必须等于真跑金额。

    两边都真跑，不 mock。预演走的是与 DAG 同一批 skill，只是库换成一次性副本 ——
    这条测试是「同一批」这个说法唯一的证据。它一红，房间里报的金额就不是将来会退的
    那个数，而两条路各自都自洽、都不报错。
    """
    payload, checked = approved
    _facts, data = stages.facts_finance_preview(payload, checked)
    real = custom_case.run_payload(payload, approve=True, verbose=False)

    assert data["preview_ran"] is True
    assert data["amount_approved"] == real["amount_approved"] == "6800.00"
    assert data["policy_version"] == real["policy_version_used"]


def test_finance_preview_does_not_run_when_decision_is_reject(
        rejected: tuple[dict, dict]) -> None:
    """裁定驳回就不预演。预演不看 `checked` 的话会报 6800，而真跑退 0.00。"""
    payload, checked = rejected
    assert checked["decision"] == "reject"

    facts, data = stages.facts_finance_preview(payload, checked)
    assert data["preview_ran"] is False
    assert data["amount_approved"] is None
    assert "裁定驳回，无需核算" in facts
    assert "6800" not in facts


def test_finance_preview_keeps_preview_wording_and_approve_hint(
        approved: tuple[dict, dict]) -> None:
    """R8：放行前只许说「预演」。措辞之外没有别的机制拦得住「已退款」这三个字。"""
    payload, checked = approved
    facts, _data = stages.facts_finance_preview(payload, checked)

    assert "核算预演" in facts
    assert "未落账" in facts
    assert f"/approve {CASE_ID}" in facts
    assert checked["approver_role"] in facts
    assert "已退款" not in facts and "已到账" not in facts


def test_finance_preview_failure_is_spoken_not_raised(
        approved: tuple[dict, dict], monkeypatch: pytest.MonkeyPatch) -> None:
    """预演挂了照样发言。一个岗位在房间里凭空消失，比它说「我这儿出错了」更难排查。"""
    _hide(monkeypatch, "finance.settle")
    payload, checked = approved

    facts, data = stages.facts_finance_preview(payload, checked)
    assert data["preview_ran"] is False
    assert data["error"] and data["error"].startswith("finance.settle:")
    assert "核算预演失败：finance.settle" in facts
    assert f"/approve {CASE_ID}" in facts


# --------------------------------------------------------------------------
# 财务执行岗（放行后）：铁律 8 措辞
# --------------------------------------------------------------------------
def test_finance_result_does_not_claim_settled_without_settled_observation() -> None:
    """观察到了但不是 settled → 只许说「未确认到账」，不许出现别的「到账」。

    判据刻意写成「去掉『未确认到账』之后不含『到账』」，而不是「不含『已到账』」——
    后者放过了「已经到账了」「到账 1 笔」这类同义写法。
    """
    from maos.tests.test_ingress_router import RESULT_UNCONFIRMED

    facts, data = stages.facts_finance_result(RESULT_UNCONFIRMED)
    assert data["settled_observations"] == 0
    assert "未确认到账" in facts
    assert "到账" not in facts.replace("未确认到账", "")
    assert "已受理" in facts


def test_finance_result_says_settled_only_with_settled_observation() -> None:
    from maos.tests.test_ingress_router import RESULT_SETTLED

    facts, data = stages.facts_finance_result(RESULT_SETTLED)
    assert data["settled_observations"] == 1
    assert "到账" in facts
    assert set(data) == {"amount_approved", "policy_version_used", "rule_refs", "biz_status",
                         "settled_observations", "payment_observations", "human_exits",
                         "plan_state"}


def test_finance_result_says_no_payment_when_there_is_no_observation() -> None:
    """一条观察都没有 ≠ 没到账，也 ≠ 到账了 —— 是根本没走到付款那一步。"""
    from maos.tests.test_ingress_router import RESULT_SETTLED

    result = {**RESULT_SETTLED, "settled_observations": 0, "payment_observations": [],
              "biz_status": "submitted"}
    facts, _data = stages.facts_finance_result(result)
    assert "未走到付款" in facts
    assert "到账" not in facts


# --------------------------------------------------------------------------
# 一张表：每岗只汇总一次
# --------------------------------------------------------------------------
def test_sheet_facts_summarize_rows_once_per_stage(approved: tuple[dict, dict],
                                                   rejected: tuple[dict, dict]) -> None:
    ok_payload, ok_checked = approved
    no_payload, no_checked = rejected
    rows = [
        {"line": 2, "order_id": ORDER, "reason_raw": "质量问题", "payload": ok_payload,
         "checked": ok_checked, "error": None, "problems": [], "warnings": []},
        {"line": 3, "order_id": ORDER, "reason_raw": "无理由", "payload": no_payload,
         "checked": no_checked, "error": None, "problems": [], "warnings": ["日期未填"]},
        {"line": 4, "order_id": "ORD-9999", "reason_raw": "坏了", "payload": None,
         "checked": None, "error": "底账里没有订单 ORD-9999",
         "problems": ["订单号不存在"], "warnings": []},
    ]
    stats = stages.sheet_stats(rows)
    assert (stats["total"], stats["valid"], stats["invalid"]) == (3, 2, 1)
    assert (stats["approve"], stats["reject"]) == (1, 1)
    assert stats["pending_case_ids"] == [CASE_ID]

    allowed = _allowed(rows, extra=tuple(v for v in stats.values() if isinstance(v, int)))
    for build in (stages.facts_sheet_intake, stages.facts_sheet_policy,
                  stages.facts_sheet_evidence, stages.facts_sheet_risk,
                  stages.facts_sheet_finance):
        facts, data = build(rows)
        assert facts.strip(), f"{build.__name__} 一句话都没说"
        assert data["total"] == 3
        extra = _numbers(facts) - allowed
        assert not extra, f"{build.__name__} 的汇总里出现了 rows 里没有的数字：{extra}"
