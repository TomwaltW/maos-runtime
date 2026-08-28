"""Policy Agent —— 按订单锁定的政策版本裁定退款资格。

薄壳：调一个 skill，把结论包成产物。裁定逻辑全在 `policy.match` 里，
这里连「命中几条算通过」都不判 —— 那样的判断一旦写进 Agent，
换业务域时就得连 Agent 一起重写。

裁定为 reject 时**不产出空产物、也不 failed**：结论是「不予退款」，那是一个
有效的业务结论，不是一次执行失败。Plan 后续节点由 DAG 决定要不要继续走 ——
Agent 的职责到给出结论为止。
"""

from __future__ import annotations

from maos.agents.base import AgentIdentity, AgentOutput, BaseAgent, TaskContext, register
from maos.model.client import Tier

from ._base import KIND_POLICY_DECISION, artifact, extras_of, failed

SKILL_POLICY = "policy.match"


@register
class RefundPolicyAgent(BaseAgent):
    identity = AgentIdentity(
        agent_id="refund-policy",
        role="refund_policy",
        duty="按订单快照锁定的政策版本检索适用规则并裁定退款资格",
        allowed_skills=frozenset({"policy.match"}),
        allowed_tools=frozenset(),
        write_scope=frozenset({"artifact"}),
        max_risk="L",
        model_tier=Tier.LIGHT,      # 裁定零模型，给 LIGHT 只是为了留一个统一的声明位
        max_self_repair=0,
    )

    def run(self, ctx: TaskContext) -> AgentOutput:
        self.check_risk(ctx.risk_level)
        self.check_write("artifact")

        res = self.skills.invoke(SKILL_POLICY, {
            "tenant_id": ctx.inputs.get("tenant_id"),
            "case_id": ctx.inputs.get("case_id"),
            "rule_prefix": ctx.inputs.get("rule_prefix"),
        }, extras=extras_of(self, ctx))
        if res.status != "ok" or not isinstance(res.output, dict):
            return AgentOutput(status="failed", error=failed(res, SKILL_POLICY))

        out = res.output
        return AgentOutput(
            status="ok",
            artifacts=[artifact(KIND_POLICY_DECISION, {
                "policy_decision": out,
                "policy_version": out["policy_version"],
                "rule_refs": out["rule_refs"],
                "decision": out["decision"],
                "invocation_id": out["invocation_id"],
            }, summary=(
                f"政策裁定 {out['decision']}：按下单锁定的政策 v{out['policy_version']} "
                f"（**非当前最新版本**）命中 {len(out['matched_rules'])} 条规则 —— {out['reason']}"
            ))],
            metrics={"policy_version": out["policy_version"],
                     "matched_rules": len(out["matched_rules"]),
                     "decision": out["decision"], "is_rework": ctx.is_rework},
        )
