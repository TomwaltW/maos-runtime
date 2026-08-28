"""Finance Agent —— 核算退款金额，产出财务复核闸要的那份凭据。

**它自己重跑一遍 `policy.match`，不采信上游传来的裁定结论。**

这不是不信任 Policy Agent，是财务复核的基本口径：核算依据必须由核算方自己按
订单锁定的政策版本取一次。采信上游传话有两个后果 —— 一是依据从「订单快照锁定的
版本」退化成「上一个节点当时读到的东西」，二是这份依据要经 artifact 在任务之间
搬运，中途任何一次形状改动都会让金额悄悄算在另一版政策上。重跑的成本是几行只读
SQL，零模型、确定性，换来的是「金额是按哪一条哪一版算的」这件事永远可当场复现。

产出**必须**带 `finance_entry` 键（跨轨冻结契约 F-1）——
R-0 的第六道财务复核闸就认这一个键，缺了闸判 blocker。
"""

from __future__ import annotations

from maos.agents.base import AgentIdentity, AgentOutput, BaseAgent, TaskContext, register
from maos.model.client import Tier

from ._base import KIND_FINANCE_SETTLEMENT, artifact, extras_of, failed

SKILL_POLICY = "policy.match"
SKILL_SETTLE = "finance.settle"


@register
class RefundFinanceAgent(BaseAgent):
    identity = AgentIdentity(
        agent_id="refund-finance",
        role="refund_finance",
        duty="按锁定政策自行复核规则并核算退款金额，写 finance_entry 并产出复核凭据",
        allowed_skills=frozenset({"policy.match", "finance.settle"}),
        allowed_tools=frozenset(),          # 核算不碰支付网关：算钱和付钱是两个角色
        write_scope=frozenset({"artifact"}),
        max_risk="M",
        model_tier=Tier.LIGHT,
        max_self_repair=0,
    )

    def run(self, ctx: TaskContext) -> AgentOutput:
        self.check_risk(ctx.risk_level)
        self.check_write("artifact")

        tenant_id = ctx.inputs.get("tenant_id")
        case_id = ctx.inputs.get("case_id")

        policy_res = self.skills.invoke(SKILL_POLICY, {
            "tenant_id": tenant_id, "case_id": case_id,
            "rule_prefix": ctx.inputs.get("rule_prefix"),
        }, extras=extras_of(self, ctx))
        if policy_res.status != "ok" or not isinstance(policy_res.output, dict):
            return AgentOutput(status="failed", error=failed(policy_res, SKILL_POLICY))

        settle_res = self.skills.invoke(SKILL_SETTLE, {
            "tenant_id": tenant_id, "case_id": case_id, "policy": policy_res.output,
        }, extras=extras_of(self, ctx))
        if settle_res.status != "ok" or not isinstance(settle_res.output, dict):
            return AgentOutput(status="failed", error=failed(settle_res, SKILL_SETTLE))

        out = settle_res.output
        entry = out["finance_entry"]
        return AgentOutput(
            status="ok",
            # content 里的 finance_entry 就是写进 finance_entry 表那一行（F-1）。
            # 两处同一个 dict，不各造一份 —— 各造一份的症状是闸恒 blocker 或恒 pass。
            artifacts=[artifact(KIND_FINANCE_SETTLEMENT, {
                "finance_entry": entry,
                "breakdown": out["breakdown"],
                "rule_refs": out["rule_refs"],
                "policy_version": policy_res.output["policy_version"],
                "invocation_id": out["invocation_id"],
            }, summary=(
                f"财务核算完成：核准 {out['amount_approved']}（政策 "
                f"v{policy_res.output['policy_version']}，依据 "
                f"{'、'.join(out['rule_refs']) or '缺省全额口径'}）"
            ))],
            metrics={"amount_approved": entry["amount_approved"],
                     "rule_refs": len(out["rule_refs"]),
                     "policy_version": policy_res.output["policy_version"],
                     "is_rework": ctx.is_rework},
        )
