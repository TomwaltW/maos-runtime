"""finance.settle 消费 `eligibility.ineffective_rules`：举证不足要真的改变金额。

买的是这一条（跨轨契约 §0 / R1）：

    AS-003「人为损坏免责（需图片举证）」是 `effect: exclude`、`refund_ratio: "0"`，
    商家**靠它免责**。举证不足时它**不予适用** —— 客户照退，而不是拒赔。

在制的 `_params_of` 对每一条命中规则一视同仁，于是一条条件根本不满足的排除规则
照样参与 `max()`。本文件的第 1 / 2 条是同一份入参只差 `ineffective_rules`
一个键值的**真对照**：金额从 0.00 变回 6800.00。

入参一律**自造**：按跨轨契约 §1.1 冻结的 `eligibility` 形状写 fixture，不跑
`policy.match`、不 import 政策侧的任何代码 —— 政策侧是另一轨的在制面，
两边耦合起来的话，哪一侧改坏了都会表现成对方的测试红。

关于 `max()` 口径（本轨未动，见 `_params_of` 的 docstring）：比例取最大是
「政策对客户的承诺是并集」的直接后果。它有一个直接推论 ——
一条 `refund_ratio: "0"` 的排除规则**只有在它是唯一命中规则时**才压得动金额，
所以第 1 / 2 条那组对照的命中集合就是单条 AS-003。规则集合不止一条时，
被剔除的规则要靠**扣费**才咬得动金额，那一路由第 7 条守。
"""

from __future__ import annotations

import json

import pytest

from maos.agents.base import AgentIdentity
from maos.core.store import SqliteStore
from maos.domain.refund import objects
from maos.flows import scenario_6 as s6
from maos.model.client import Tier
from maos.skills.invoker import SkillInvoker

IDENTITY = AgentIdentity(
    agent_id="test-t75-finance",
    role="test_refund_finance",
    duty="测试夹具：金额核算面的适用性剔除",
    allowed_skills=frozenset({"refund.intake", "finance.settle", "issue.aggregate"}),
    allowed_tools=frozenset(),
    write_scope=frozenset({"artifact"}),
    max_risk="M",
    model_tier=Tier.LIGHT,
)

#: 靶场里这一单的实付与诉求都是 6800.00，所以核算基数就是它。
#: 断言里写死这个数是有意的：靶场金额一旦被改，本文件的对照会立刻红，
#: 而不是悄悄按新基数算出另一组「看着也对」的金额。
BASE = "6800.00"

AS_003 = {
    "rule_no": "AS-003", "version": 1,
    "title": "人为损坏免责（需图片举证）",
    # 与 scenarios/refund/policy/policy_rules.json 里 AS-003 v1 的 body 同值。
    "params": {"refund_ratio": "0", "deduct_fee": "0", "effect": "exclude"},
}
AS_003_REF = "AS-003@v1"

#: AS-003 举证不足的那一条 `unmet`，形状照抄跨轨契约 §1.1。
UNMET_AS_003 = {
    "rule_ref": AS_003_REF,
    "requirement": "min_evidence_count",
    "required": 1,
    "actual": 0,
    "direction": "not_applied",
}


def _eligibility(*, unmet, ineffective) -> dict:
    return {
        "eligible": not ineffective,
        "unmet": list(unmet),
        "evidence_seen": {"image": 0, "video": 0, "total": 0},
        "checked_rules": [AS_003_REF],
        "ineffective_rules": list(ineffective),
    }


def _policy(rules, eligibility=None) -> dict:
    """自造一份 policy.match 出参。`eligibility=None` 表示**入参里没有这个键**。"""
    out = {
        "policy_version": 1,
        "decision": "approve",
        "reason": "测试夹具",
        "matched_rules": list(rules),
        "rule_refs": [f"{r['rule_no']}@v{r['version']}" for r in rules],
    }
    if eligibility is not None:
        out["eligibility"] = eligibility
    return out


@pytest.fixture
def store():
    st = SqliteStore()
    st.init_schema()
    s6.seed_domain(st)
    return st


@pytest.fixture
def invoker(store):
    iv = SkillInvoker(IDENTITY, store)
    iv.invoke("refund.intake", {
        "signals": s6.SIGNALS,
        "case_seed": {
            "tenant_id": s6.TENANT_ID, "case_id": s6.CASE_ID, "channel_id": s6.CHANNEL_ID,
            "order_id": s6.ORDER_ID, "order_version": s6.ORDER_VERSION, "sku": s6.SKU,
            "reason_code": "artificial_damage", "amount_claimed": s6.AMOUNT_CLAIMED,
        },
    }, extras=_extras())
    return iv


def _extras(**over) -> dict:
    base = {"plan_id": "plan-t75", "task_id": "task-t75", "trace_id": "trace-t75",
            "attempt": 1}
    base.update(over)
    return base


def _settle(invoker, policy):
    return invoker.invoke("finance.settle", {
        "tenant_id": s6.TENANT_ID, "case_id": s6.CASE_ID, "policy": policy,
    }, extras=_extras())


def _amount(invoker, policy) -> str:
    res = _settle(invoker, policy)
    assert res.status == "ok", res.error
    assert res.output["breakdown"]["base"] == BASE, "核算基数变了，对照就不成立了"
    return res.output["amount_approved"]


# ======================================================================
# 第 1 / 2 条：同一份入参，只差 ineffective_rules 一个键值 —— 金额必须不同
# ======================================================================

def test_ineffective_exclusion_does_not_zero_the_amount(invoker):
    """举证不足 → 排除规则不予适用 → **照退**，不是拒赔（跨轨契约 R1）。

    方向写反（没交图就不退）是把举证责任倒置到客户身上，是本轮最严重的做错方式，
    所以这条断的是「金额不是 0」，不是「金额等于某个数」。
    """
    res = _settle(invoker, _policy([AS_003], _eligibility(
        unmet=[UNMET_AS_003], ineffective=[AS_003_REF])))
    assert res.status == "ok", res.error
    out = res.output

    assert out["amount_approved"] != "0.00", (
        "举证不足时把免责规则照样算进去，等于让客户举不出证就拿不到钱 —— "
        "而这条规则本来是给商家免责用的")
    assert out["amount_approved"] == BASE, "剩下没有别的规则，落缺省口径：全额退、不扣费"
    assert out["breakdown"]["refund_ratio"] == "1"
    assert out["breakdown"]["applied_rules"] == [], "AS-003 不该出现在核算依据里"
    assert out["breakdown"]["excluded_rules"] == [
        {"rule_ref": AS_003_REF, "reason": "min_evidence_count 1 > actual 0"}]


def test_effective_exclusion_still_zeroes_the_amount(invoker):
    """对照组：同一份入参，AS-003 **不在** ineffective_rules 里 → 免责生效，金额为 0。

    这条与上一条是一组，缺了它上一条什么都没证明 —— 金额可能本来就不是 0。
    """
    res = _settle(invoker, _policy([AS_003], _eligibility(unmet=[], ineffective=[])))
    assert res.status == "ok", res.error
    out = res.output

    assert out["amount_approved"] == "0.00", "举证充分时免责规则照常生效"
    assert out["breakdown"]["applied_rules"] == [AS_003_REF]
    assert out["breakdown"]["excluded_rules"] == []


# ======================================================================
# 第 3 条：缺省分支 —— 没有 eligibility 键时行为与引入本特性之前一致
# ======================================================================

def test_missing_eligibility_stays_identical_to_baseline(invoker, store):
    """旧 Plan / v1.0.0 的历史调用不带 `eligibility` —— 一律按「全部规则均适用」。

    判据写成「与『显式全部适用』那一支逐字节相同」，而不是写死一个金额：
    这两支本来就是同一件事，哪天有人给缺省分支加了别的语义，这里立刻红。
    """
    without = _settle(invoker, _policy([AS_003]))
    assert without.status == "ok", without.error
    explicit = _settle(invoker, _policy(
        [AS_003], _eligibility(unmet=[], ineffective=[])))
    assert explicit.status == "ok", explicit.error

    assert without.output["amount_approved"] == "0.00"
    assert without.output["breakdown"] == explicit.output["breakdown"]
    assert without.output["breakdown"]["applied_rules"] == [AS_003_REF]

    # 库表与产物同一份数据这条 F-1 义务不因本轨的改动而失效。
    rows = objects.query(
        store, "SELECT * FROM finance_entry WHERE tenant_id=? AND case_id=?",
        (s6.TENANT_ID, s6.CASE_ID))
    assert json.loads(rows[0]["breakdown_json"])["excluded_rules"] == []


# ======================================================================
# 第 4 条：形状不对就报错，不静默兜底（跨轨契约 R5）
# ======================================================================

@pytest.mark.parametrize("eligibility, why", [
    ("AS-003@v1", "不是 dict"),
    ({"unmet": [], "evidence_seen": {}}, "缺 ineffective_rules"),
    ({"ineffective_rules": "AS-003@v1", "unmet": []}, "ineffective_rules 不是 list"),
    ({"ineffective_rules": [], "unmet": {"rule_ref": AS_003_REF}}, "unmet 不是 list"),
    ({"ineffective_rules": [], "unmet": [[AS_003_REF]]}, "unmet 元素不是 dict"),
    ({"ineffective_rules": [AS_003_REF],
      "unmet": [dict(UNMET_AS_003, requirement="min_photo_count")]},
     "requirement 落在取值域外"),
    ({"ineffective_rules": [AS_003_REF], "unmet": []},
     "剔除了却说不出原因"),
])
def test_malformed_eligibility_is_rejected_not_silently_ignored(invoker, eligibility, why):
    """静默兜底会把「上游把结构改坏了」表现成「金额悄悄算错了」—— 后者最难查。"""
    res = _settle(invoker, _policy([AS_003], eligibility))
    assert res.status != "ok", f"{why}：本该报错，却当没看见照常算出了金额"
    # 报错也要报在点子上：别的原因失败同样能让上一条断言绿，那就什么都没验到。
    assert "eligibility" in (res.error or ""), f"{why}：错是错了，但不是本条要验的那个错"


# ======================================================================
# 第 5 条：全部规则都被剔除 —— 落缺省口径，且排除依据列全
# ======================================================================

def test_all_rules_ineffective_falls_back_to_default_terms(invoker):
    """一条规则都不适用 ≠ 没得退：没有任何规则限制退款，就按缺省的全额、不扣费。

    返回 0 会把「政策没说话」讲成「政策说了不退」，那是把沉默当成拒绝。
    """
    depreciation = {
        "rule_no": "AS-007", "version": 2, "title": "超期无理由退货（扣折旧）",
        "params": {"refund_ratio": "0.85", "deduct_fee": "20"},
    }
    unmet_days = {
        "rule_ref": "AS-007@v2", "requirement": "no_reason_days",
        "required": 7, "actual": 31, "direction": "not_applied",
    }
    res = _settle(invoker, _policy([AS_003, depreciation], _eligibility(
        unmet=[UNMET_AS_003, unmet_days],
        ineffective=[AS_003_REF, "AS-007@v2"])))
    assert res.status == "ok", res.error
    bd = res.output["breakdown"]

    assert res.output["amount_approved"] == BASE
    assert bd["refund_ratio"] == "1" and bd["deduct_fee"] == "0.00"
    assert bd["applied_rules"] == []
    assert [e["rule_ref"] for e in bd["excluded_rules"]] == [AS_003_REF, "AS-007@v2"], (
        "被剔掉的规则一条都不能漏 —— 对账时要按它回答「这笔钱为什么这么算」")
    assert bd["excluded_rules"][1]["reason"] == "no_reason_days 7 > actual 31"


# ======================================================================
# 第 6 条：排除原因是搬来的，不是本轨重算的
# ======================================================================

def test_excluded_reason_is_carried_from_unmet_not_recomputed(invoker):
    """原因串只搬 `unmet` 里的字段。两边各算一套，合并后这条线就会分叉。

    夹具故意让 `unmet` 与 `evidence_seen` 互相矛盾（说差 2 张图，却又说看见 5 张）：
    金额侧若自己重算过判据，就会按 evidence_seen 得出别的说法，本条即红。
    """
    contradicting = {
        "eligible": False,
        "unmet": [dict(UNMET_AS_003, required=2, actual=0)],
        "evidence_seen": {"image": 5, "video": 0, "total": 5},
        "checked_rules": [AS_003_REF],
        "ineffective_rules": [AS_003_REF],
    }
    res = _settle(invoker, _policy([AS_003], contradicting))
    assert res.status == "ok", res.error

    excluded = res.output["breakdown"]["excluded_rules"]
    assert excluded == [{"rule_ref": AS_003_REF,
                         "reason": "min_evidence_count 2 > actual 0"}]


def test_multiple_unmet_of_one_rule_are_all_carried(invoker):
    """一条规则差两项判据时，两项都要出现在原因串里，不许只报第一项。"""
    kinds = {
        "rule_ref": AS_003_REF, "requirement": "requires_evidence_kinds",
        "required": ["image"], "actual": [], "direction": "not_applied",
    }
    res = _settle(invoker, _policy([AS_003], _eligibility(
        unmet=[UNMET_AS_003, kinds], ineffective=[AS_003_REF])))
    assert res.status == "ok", res.error

    reason = res.output["breakdown"]["excluded_rules"][0]["reason"]
    assert "min_evidence_count" in reason and "requires_evidence_kinds" in reason


# ======================================================================
# 第 7 条：有其余规则存活时的对照 —— 被剔规则的扣费不该再咬金额
# ======================================================================

def test_ineffective_rule_fee_does_not_shrink_the_amount(invoker):
    """命中集合不止一条时，剔除靠的是**扣费**这一路才看得出来（见模块 docstring）。

    同一份入参只差 `ineffective_rules` 一个键值：
    收了那条不予适用的规则的 500 元扣费 → 5280.00；剔掉它 → 5780.00。
    """
    quality = {"rule_no": "AS-001", "version": 1, "title": "质量问题按 85% 退",
               "params": {"refund_ratio": "0.85", "deduct_fee": "0"}}
    restock = {"rule_no": "AS-009", "version": 1, "title": "超期无理由退货（扣 500 手续费）",
               "params": {"refund_ratio": "0.85", "deduct_fee": "500"}}
    unmet_days = {"rule_ref": "AS-009@v1", "requirement": "no_reason_days",
                  "required": 7, "actual": 31, "direction": "not_applied"}

    applied_all = _amount(invoker, _policy(
        [quality, restock], _eligibility(unmet=[unmet_days], ineffective=[])))
    dropped = _amount(invoker, _policy(
        [quality, restock], _eligibility(unmet=[unmet_days], ineffective=["AS-009@v1"])))

    assert applied_all == "5280.00", "在制口径：不予适用的规则照样扣了 500"
    assert dropped == "5780.00", "剔除之后只按存活的 AS-001 算"
