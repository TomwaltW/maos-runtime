"""Classify Agent —— 给差错定性，选定 camt.056 的官方撤销原因码。

薄壳：调 `investigation.classify`，把裁定结论包成产物。

**裁定判据带可核对的规则编号**：产物里的 `rule_refs` 是官方码集名 + 码 + 官方定义
原文 + 出处 URL，评委当场可以拿那个 URL 去对。不是我们自己编的「规则 1/2/3」。
"""

from __future__ import annotations

from maos.agents.base import AgentIdentity, AgentOutput, BaseAgent, TaskContext, register
from maos.model.client import Tier

from ._base import KIND_CLASSIFICATION, artifact, extras_of, failed

SKILL_CLASSIFY = "investigation.classify"


@register
class InvestigationClassifyAgent(BaseAgent):
    identity = AgentIdentity(
        agent_id="investigation-classify",
        role="investigation_classify",
        duty="给支付差错定性并选定官方撤销原因码，推进到 classified",
        allowed_skills=frozenset({SKILL_CLASSIFY}),
        allowed_tools=frozenset(),
        write_scope=frozenset({"artifact"}),
        max_risk="L",
        model_tier=Tier.LIGHT,
        max_self_repair=0,
    )

    def run(self, ctx: TaskContext) -> AgentOutput:
        self.check_risk(ctx.risk_level)
        self.check_write("artifact")

        res = self.skills.invoke(SKILL_CLASSIFY, {
            "tenant_id": ctx.inputs.get("tenant_id"),
            "case_id": ctx.inputs.get("case_id"),
            "classification": ctx.inputs.get("classification"),
            "reason_code": ctx.inputs.get("reason_code"),
            "note": ctx.inputs.get("note"),
        }, extras=extras_of(self, ctx))
        if res.status != "ok" or not isinstance(res.output, dict):
            return AgentOutput(status="failed", error=failed(res, SKILL_CLASSIFY))
        out = res.output
        ref = out["rule_refs"][0]

        return AgentOutput(
            status="ok",
            artifacts=[artifact(KIND_CLASSIFICATION, {
                "case_id": ctx.inputs.get("case_id"),
                "biz_status": out["biz_status"],
                "classification": out["classification"],
                "reason_code": out["reason_code"],
                "rule_refs": out["rule_refs"],
                "note": out["note"],
                "invocation_id": out["invocation_id"],
            }, summary=(
                f"定性为 {out['classification']}，撤销原因码 {out['reason_code']}"
                f"（{ref['name']}：{ref['definition']}）；"
                f"出自 {ref['code_set']}，出处 {ref['source']}"
            ))],
            metrics={"biz_status": out["biz_status"],
                     "reason_code": out["reason_code"],
                     "is_rework": ctx.is_rework},
        )
