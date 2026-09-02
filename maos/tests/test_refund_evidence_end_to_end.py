"""整合轮接缝守卫 —— 「交一张图和不交，金额不同」这句话的端到端证明。

## 为什么这条测试属于整合轮，而不属于 T74 或 T75

T74 只验到 `policy.match` **产出** `eligibility`（自己查证据表）；
T75 只验到 `finance.settle` **消费** `ineffective_rules`（自造 eligibility 入参，
按跨轨契约 §1.1 的形状写 fixture，不跑 policy.match）。

两轨各自全绿，但**接缝上一条测试都没有**：谁也没有把真实的 `policy.match` 出参
直接喂进 `finance.settle`。形状对不上、方向反了、键名写错了，两轨的测试都不会红。

T75 自己在 `docs/BACKLOG.md ## task-T75` 里记了这件事：

    本轨在 T74 合并前**验不到端到端**：`eligibility` 恒不存在，走的全是缺省分支。
    真正的端到端要等整合轮，那时候要**重跑一次**本轨的第 1 / 2 条对照。

这个文件就是那次重跑，且是拿**真出参**而不是 fixture 跑的。

## 它钉住的那句话

跨轨契约 §0 把本轮要买的东西定义成一句可证伪的话：

    同一个案子，交一张图和不交，裁定结论**必须不同**。

改造前这句话不成立（三个证据判据字段在 `maos/**/*.py` 里 grep 零命中，
交图和不交图逐字节相同且不报错）。下面 `test_one_photo_changes_the_settled_amount`
就是它成立的证据 —— 两次调用只差一行 `customer_evidence`，金额一个 0 一个全额。

## 方向（跨轨契约 §2 R1）

AS-003 是 `effect: "exclude"`、`refund_ratio: "0"` —— 商家**靠它免责**，
`requires_evidence_kinds: ["image"]` 是说「认定人为损坏需要图片支撑」。所以：

    没图 → 认定不成立 → 排除规则**不予适用** → 客户**照退全额**
    有图 → 认定成立   → 排除规则生效       → 退 0

`test_missing_photo_does_not_shift_the_burden_to_the_customer` 钉的正是这个方向：
把它写反（没交图就拒赔）在数值上同样"能跑"，只有方向词能拦住。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from maos.agents.base import AgentIdentity
from maos.model.client import Tier
from maos.skills.invoker import SkillInvoker

# 靶场直接复用 T74 的：那里已经有 AS-003 租户（只挂一条证据判据规则），
# 重造一份只会让两处的 body 慢慢漂开。
from maos.tests.test_refund_policy_eligibility import (  # noqa: E402
    AS_003_BODY,
    PLAN_ID,
    T_EVIDENCE,
    TASK_ID,
    _add_evidence,
    _seed_tenant,
)
from maos.core.store import SqliteStore  # noqa: E402
from maos.domain.refund import objects  # noqa: E402

AS_003_REF = "AS-003@v1"

#: 本域两个 skill 都要授权 —— 接缝测试的意义就在于**一次调用链**跑通两个。
IDENTITY = AgentIdentity(
    agent_id="test-integrate-evidence-seam",
    role="test_refund",
    duty="整合轮接缝：policy.match → finance.settle 串联",
    allowed_skills=frozenset({"policy.match", "finance.settle"}),
    allowed_tools=frozenset(),
    write_scope=frozenset({"artifact"}),
    max_risk="L",
    model_tier=Tier.LIGHT,
)


@pytest.fixture
def store():
    st = SqliteStore()
    st.init_schema()
    objects.ensure_schema(st)
    _seed_tenant(st, T_EVIDENCE, rules=[("AS-003", 1, AS_003_BODY)])
    return st


@pytest.fixture
def invoker(store):
    return SkillInvoker(IDENTITY, store)


def _extras() -> dict:
    return {"plan_id": PLAN_ID, "task_id": TASK_ID, "trace_id": "trace-seam", "attempt": 1}


def _case_id() -> str:
    return f"case-{T_EVIDENCE}"


def _run_chain(invoker) -> tuple[dict, dict]:
    """跑一次真链路：policy.match 的**真实出参**直接喂进 finance.settle。

    刻意不在中间插任何加工 —— 接缝上少一个字段、多一层嵌套，这里就该炸。
    """
    pol = invoker.invoke(
        "policy.match",
        {"tenant_id": T_EVIDENCE, "case_id": _case_id()},
        version="1.0.0", extras=_extras())
    assert pol.status == "ok", pol.error

    fin = invoker.invoke(
        "finance.settle",
        {"tenant_id": T_EVIDENCE, "case_id": _case_id(), "policy": pol.output},
        extras=_extras())
    assert fin.status == "ok", fin.error
    return pol.output, fin.output


# ======================================================================
# 契约 §0 那句话：交一张图和不交，结论必须不同
# ======================================================================

def test_one_photo_changes_the_settled_amount(invoker, store):
    """一行 `customer_evidence` 的差别，必须一路走到金额上。

    这是本轮六轨买的东西的**唯一**端到端证据。它红了说明接缝断了，
    而两轨各自的测试**都不会**跟着红 —— 那正是这条存在的理由。
    """
    _, fin_without = _run_chain(invoker)

    _add_evidence(store, T_EVIDENCE, _case_id(), "image")
    _, fin_with = _run_chain(invoker)

    assert fin_without["amount_approved"] != fin_with["amount_approved"], (
        "交图与不交图算出同一个金额 —— 证据判据又变回了摆设（跨轨契约 §0）")


def test_missing_photo_does_not_shift_the_burden_to_the_customer(invoker):
    """方向（契约 §2 R1）：**没图 = 免责不成立 = 客户照退**，不是拒赔。

    数值上「没图退 0」也跑得通，所以这条不能只断言「两次不同」，
    必须把哪一侧大写死。
    """
    pol, fin = _run_chain(invoker)

    assert AS_003_REF in pol["eligibility"]["ineffective_rules"], (
        "举证不足时 AS-003 应当**不予适用**，而不是照常生效")
    assert Decimal(str(fin["amount_approved"])) > 0, (
        "没交图反而退 0 —— 举证责任被倒置到客户身上了（契约 §2 R1）")


def test_supplied_photo_lets_the_exclusion_take_effect(invoker, store):
    """反向：图交齐了，免责规则**该生效就生效** —— 判定器不是只会放行。"""
    _add_evidence(store, T_EVIDENCE, _case_id(), "image")
    pol, fin = _run_chain(invoker)

    assert pol["eligibility"]["eligible"] is True
    assert pol["eligibility"]["ineffective_rules"] == []
    assert Decimal(str(fin["amount_approved"])) == 0, (
        "证据齐备时 AS-003（refund_ratio=0）应当生效")


# ======================================================================
# 接缝本身的形状：两轨对 eligibility 的理解必须是同一个
# ======================================================================

def test_policy_output_carries_the_shape_finance_expects(invoker):
    """T74 产出的键，正好是 T75 消费的键 —— 契约 §1.1 的形状在真链路上成立。

    T75 的测试全部拿 fixture 自造入参，一旦 T74 的真出参与那份 fixture 分叉，
    只有这条会红。
    """
    pol, _ = _run_chain(invoker)
    elig = pol["eligibility"]

    for key in ("eligible", "unmet", "evidence_seen", "checked_rules", "ineffective_rules"):
        assert key in elig, f"eligibility 缺 {key} —— 与跨轨契约 §1.1 分叉了"

    for item in elig["unmet"]:
        assert item["requirement"] in (
            "min_evidence_count", "requires_evidence_kinds",
            "no_reason_days", "warranty_basis"), (
            f"requirement 出现封闭枚举外的值 {item['requirement']!r}（契约 §1.2）")


def test_exclusion_reason_survives_the_seam(invoker):
    """「为什么排除」这条线跨过接缝不许断（T75 §5.2：reason 从 unmet 搬，不重算）。

    留痕在 `breakdown` 里而不是出参顶层 —— 那是核算明细该待的地方，
    且它随 `finance_entry.breakdown_json` 一起落库，对账时查得到。
    """
    pol, fin = _run_chain(invoker)

    excluded = fin["breakdown"]["excluded_rules"]
    assert excluded, "AS-003 被剔除了，但 finance 侧没留下任何排除记录"

    refs = {e["rule_ref"] for e in excluded}
    assert AS_003_REF in refs
    assert pol["eligibility"]["unmet"], "policy 侧应当有 unmet 才谈得上搬运"

    # reason 是搬来的、不是重算的：policy 侧每一条 unmet 的判据名都要能在里面找到。
    reason = next(e["reason"] for e in excluded if e["rule_ref"] == AS_003_REF)
    for item in pol["eligibility"]["unmet"]:
        assert item["requirement"] in reason, (
            f"unmet 里的 {item['requirement']} 没被搬进 finance 的排除原因 —— "
            "两侧各算一套，「为什么排除」这条线就分叉了")

    # 排除了就不该同时算作已适用。
    assert AS_003_REF not in fin["breakdown"]["applied_rules"]


def test_matched_rules_still_include_the_ineffective_rule(invoker):
    """契约 §1.3：「命中」与「该不该退」是两个问题，合并后仍然分得开。

    AS-003 举证不足，但它**照样在 matched_rules 里**（它确实命中了），
    只是同时出现在 ineffective_rules。混成一个字段之后审计就说不清
    是规则没命中还是条件不满足（`docs/BACKLOG.md:185`）。
    """
    pol, _ = _run_chain(invoker)

    assert AS_003_REF in pol["rule_refs"], "命中集合不该因为条件不满足而被剔空"
    assert AS_003_REF in pol["eligibility"]["ineffective_rules"]
    assert pol["decision"] == "approve", (
        "decision 只表达「有没有命中适用规则」，不许被 eligible 翻转（契约 §1.3）")
