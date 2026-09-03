"""Claim Intake Agent —— 报案受理：三源信号聚合去重，建案，挂证据。

薄壳：Identity + 经 SkillInvoker 调 skill，不写业务逻辑。
放文件即注册（C-2 pkgutil 自动发现），不碰 `maos/agents/__init__.py`。
"""

from __future__ import annotations

from maos.agents.base import AgentIdentity, AgentOutput, BaseAgent, TaskContext, register
from maos.model.client import Tier

from ._base import KIND_CLAIM_DRAFT, artifact, extras_of, failed

SKILL_INTAKE = "claim.intake"


@register
class ClaimIntakeAgent(BaseAgent):
    identity = AgentIdentity(
        agent_id="claim-intake",
        role="claim_intake",
        duty="受理三源报案信号（工单 / 客服记录 / 定损照片），聚合去重后建案并挂上证据",
        # issue.aggregate 在白名单里是必需的：claim.intake 经 SkillInvoker 复用它做去重，
        # 而 invoker 校验的是**调用方的 identity**。最小授权就该在这里表达，
        # 不该由被调方自己放行（invoker 的越权是抛异常，不是软失败）。
        allowed_skills=frozenset({"claim.intake", "issue.aggregate"}),
        allowed_tools=frozenset(),          # 受理不碰赔付方
        write_scope=frozenset({"artifact"}),
        max_risk="L",
        model_tier=Tier.LIGHT,
        max_self_repair=0,
    )

    def run(self, ctx: TaskContext) -> AgentOutput:
        self.check_risk(ctx.risk_level)
        self.check_write("artifact")

        res = self.skills.invoke(SKILL_INTAKE, {
            "signals": ctx.inputs.get("signals") or [],
            "case_seed": ctx.inputs.get("case_seed") or {},
            "claim_lines": ctx.inputs.get("claim_lines"),
            "reported_at": ctx.inputs.get("reported_at"),
        }, extras=extras_of(self, ctx))
        if res.status != "ok" or not isinstance(res.output, dict):
            return AgentOutput(status="failed", error=failed(res, SKILL_INTAKE))

        out = res.output
        case = out["case_draft"]
        dedup = out["dedup"]
        return AgentOutput(
            status="ok",
            artifacts=[artifact(KIND_CLAIM_DRAFT, {
                "case_draft": case,
                "evidence_refs": out["evidence_refs"],
                "claim_lines": out["claim_lines"],
                "issues": out["issues"],
                "dedup": dedup,
                "invocation_id": out["invocation_id"],
            }, summary=(
                f"受理理赔案 {case['claim_id']}：{dedup['signals']} 条三源报案去重为 "
                f"{dedup['issues']} 个 issue（合并 {dedup['merged']} 条），"
                f"证据 {len(out['evidence_refs'])} 份、明细 {len(out['claim_lines'])} 行，"
                f"biz_status={case['biz_status']}"
            ))],
            metrics={"issues": dedup["issues"], "merged": dedup["merged"],
                     "evidence": len(out["evidence_refs"]),
                     "lines": len(out["claim_lines"]), "is_rework": ctx.is_rework},
        )
