"""Claim Adjudicator Agent —— 按投保当时锁定的条款版本裁定赔付责任。

薄壳：调一个 skill，把结论包成产物。裁定逻辑全在 `claim.adjudicate` 里，
这里连「命中几条算通过」都不判 —— 那样的判断一旦写进 Agent，
换业务域时就得连 Agent 一起重写。

裁定为 reject 时**不产出空产物、也不 failed**：结论是「不予赔付」，那是一个
有效的业务结论，不是一次执行失败。Plan 后续节点由 DAG 决定要不要继续走 ——
Agent 的职责到给出结论为止。
"""

from __future__ import annotations

from maos.agents.base import AgentIdentity, AgentOutput, BaseAgent, TaskContext, register
from maos.model.client import Tier

from ._base import KIND_ADJUDICATION, artifact, extras_of, failed

SKILL_ADJUDICATE = "claim.adjudicate"


@register
class ClaimAdjudicatorAgent(BaseAgent):
    identity = AgentIdentity(
        agent_id="claim-adjudicator",
        role="claim_adjudicator",
        duty="按保单快照锁定的条款版本检索适用条款并裁定赔付责任，产出带条款编号与版本的裁定",
        allowed_skills=frozenset({"claim.adjudicate"}),
        allowed_tools=frozenset(),
        write_scope=frozenset({"artifact"}),
        max_risk="L",
        model_tier=Tier.LIGHT,      # 裁定零模型，给 LIGHT 只是为了留一个统一的声明位
        max_self_repair=0,
    )

    def run(self, ctx: TaskContext) -> AgentOutput:
        self.check_risk(ctx.risk_level)
        self.check_write("artifact")

        res = self.skills.invoke(SKILL_ADJUDICATE, {
            "tenant_id": ctx.inputs.get("tenant_id"),
            "claim_id": ctx.inputs.get("claim_id"),
            "rule_prefix": ctx.inputs.get("rule_prefix"),
        }, extras=extras_of(self, ctx))
        if res.status != "ok" or not isinstance(res.output, dict):
            return AgentOutput(status="failed", error=failed(res, SKILL_ADJUDICATE))

        out = res.output
        return AgentOutput(
            status="ok",
            artifacts=[artifact(KIND_ADJUDICATION, {
                "adjudication": out,
                # 这三个字段摊平到 content 顶层，不只埋在 adjudication 里：
                # 「按哪一条、哪一版判的」是本域的招牌判据，重放校验要能直接读到。
                "primary_rule": out["primary_rule"],
                "terms_version": out["terms_version"],
                "policy_version": out["policy_version"],
                "rule_refs": out["rule_refs"],
                "decision": out["decision"],
                "invocation_id": out["invocation_id"],
            }, summary=(
                f"责任裁定 {out['decision']}：按**投保当时锁定**的条款 "
                f"v{out['terms_version']}（不是当前最新版本）命中 "
                f"{len(out['matched_rules'])} 条，依据 {out['primary_rule']}"
                f"@v{out['terms_version']} —— {out['reason']}"
            ))],
            metrics={"terms_version": out["terms_version"],
                     "matched_rules": len(out["matched_rules"]),
                     "exclusions": len(out["exclusions"]),
                     "decision": out["decision"], "is_rework": ctx.is_rework},
        )
