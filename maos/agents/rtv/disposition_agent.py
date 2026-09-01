"""RTV Disposition Agent —— 面向合同条款：裁定 credit / exchange / replacement。

三种处置类型不是自创的，出处是 PeopleSoft FSCM《Understanding the RTV Business
Process》的 return action 三选一（契约 §7）。**裁定要留规则出处**：
`rtv_disposition.rationale_json` 每条必带 `rule_id`，口径同 ap 域
`match_result.findings_json` —— 「凭什么这么裁」在事后要查得到。

🔴 **裁定的人不碰承运商**（C-R7 那条红字）：`allowed_tools` 是空的。
与 refund 域「算钱和付钱是两个角色」同一条理由 —— 裁定者若同时能发运，
「这批货为什么被退回去」就少了一个独立的第二人称。

薄壳：裁哪一种、依据哪条规则，全在 `rtv.dispose` 里；本文件只搬运和包装。
"""

from __future__ import annotations

from maos.agents.base import AgentIdentity, AgentOutput, BaseAgent, TaskContext, register
from maos.model.client import Tier

from ._base import KIND_RTV_DISPOSITION, artifact, extras_of, failed

SKILL_DISPOSE = "rtv.dispose"


@register
class RtvDispositionAgent(BaseAgent):
    identity = AgentIdentity(
        agent_id="rtv-disposition",
        role="rtv_disposition",
        duty="按退货理由与合同条款裁定 credit/exchange/replacement，并保留规则出处",
        allowed_skills=frozenset({"rtv.dispose"}),
        allowed_tools=frozenset(),          # 裁定的人不碰承运商
        write_scope=frozenset({"artifact"}),
        max_risk="L",
        model_tier=Tier.LIGHT,
        max_self_repair=0,
    )

    def run(self, ctx: TaskContext) -> AgentOutput:
        self.check_risk(ctx.risk_level)
        self.check_write("artifact")

        res = self.skills.invoke(SKILL_DISPOSE, {
            "tenant_id": ctx.inputs.get("tenant_id"),
            "case_id": ctx.inputs.get("case_id"),
            "decided_by": self.identity.agent_id,
        }, extras=extras_of(self, ctx))
        if res.status != "ok" or not isinstance(res.output, dict):
            return AgentOutput(status="failed", error=failed(res, SKILL_DISPOSE))

        out = res.output
        d = out["disposition"]
        return AgentOutput(
            status="ok",
            artifacts=[artifact(KIND_RTV_DISPOSITION, dict(out), summary=(
                f"处置裁定 {d['return_action']}（第 {d['attempt']} 次裁定，"
                f"裁定人 {d['decided_by']}）；依据 "
                f"{[r['rule_id'] for r in d['rationale']]} —— 编号取自码表，"
                f"不是自然语言理由；biz_status={out['biz_status']}"
            ))],
            metrics={"rationale": len(d["rationale"]), "attempt": d["attempt"],
                     "is_rework": ctx.is_rework},
        )
