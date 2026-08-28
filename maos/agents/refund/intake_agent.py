"""Intake Agent —— 面向客户的那一端：受理诉求，以及处理完之后通知客户。

为什么受理和通知归同一个角色，而不是按流程顺序切成两个：角色边界按**面向谁**划分，
不按流程位置划分。受理和通知都是与客户的接触点，用的是同一套话术边界与同一份
证据口径；而付款、核算面向的是网关和财务。按流程顺序切，会切出一个「只发短信」的
角色，它除了位置以外没有任何自己的判断，那不是角色，是一个步骤。

薄壳：Identity + 经 SkillInvoker 调 skill，不写业务逻辑。
放文件即注册（C-2 pkgutil 自动发现），不碰 `maos/agents/__init__.py`。
"""

from __future__ import annotations

from maos.agents.base import AgentIdentity, AgentOutput, BaseAgent, TaskContext, register
from maos.model.client import Tier

from ._base import KIND_CASE_DRAFT, KIND_NOTIFICATION, artifact, extras_of, failed

SKILL_INTAKE = "refund.intake"
SKILL_NOTIFY = "notify.customer"

STEP_INTAKE = "intake"
STEP_NOTIFY = "notify"


@register
class RefundIntakeAgent(BaseAgent):
    identity = AgentIdentity(
        agent_id="refund-intake",
        role="refund_intake",
        duty="受理多源退款诉求、聚合去重并建案；处理完成后通知客户并跟踪回执",
        # issue.aggregate 在白名单里是必需的：refund.intake 经 SkillInvoker 复用它做去重，
        # 而 invoker 校验的是**调用方的 identity**。最小授权就该在这里表达，
        # 不该由被调方自己放行（invoker.py 的越权是抛异常，不是软失败）。
        allowed_skills=frozenset({"refund.intake", "issue.aggregate", "notify.customer"}),
        allowed_tools=frozenset(),          # 受理与通知不碰支付网关
        write_scope=frozenset({"artifact"}),
        max_risk="L",
        model_tier=Tier.LIGHT,
        max_self_repair=0,
    )

    def run(self, ctx: TaskContext) -> AgentOutput:
        self.check_risk(ctx.risk_level)
        self.check_write("artifact")

        step = str(ctx.inputs.get("step") or STEP_INTAKE)
        if step == STEP_NOTIFY:
            return self._notify(ctx)
        return self._intake(ctx)

    # ------------------------------------------------------------------
    def _intake(self, ctx: TaskContext) -> AgentOutput:
        res = self.skills.invoke(SKILL_INTAKE, {
            "signals": ctx.inputs.get("signals") or [],
            "case_seed": ctx.inputs.get("case_seed") or {},
        }, extras=extras_of(self, ctx))
        if res.status != "ok" or not isinstance(res.output, dict):
            return AgentOutput(status="failed", error=failed(res, SKILL_INTAKE))

        out = res.output
        case = out["case_draft"]
        dedup = out["dedup"]
        return AgentOutput(
            status="ok",
            artifacts=[artifact(KIND_CASE_DRAFT, {
                "case_draft": case,
                "evidence_refs": out["evidence_refs"],
                "issues": out["issues"],
                "dedup": dedup,
                "invocation_id": out["invocation_id"],
            }, summary=(
                f"受理退款案 {case['case_id']}：{dedup['signals']} 条多源诉求去重为 "
                f"{dedup['issues']} 个 issue（合并 {dedup['merged']} 条），"
                f"证据 {len(out['evidence_refs'])} 份，biz_status={case['biz_status']}"
            ))],
            metrics={"issues": dedup["issues"], "merged": dedup["merged"],
                     "evidence": len(out["evidence_refs"]), "is_rework": ctx.is_rework},
        )

    def _notify(self, ctx: TaskContext) -> AgentOutput:
        res = self.skills.invoke(SKILL_NOTIFY, {
            "tenant_id": ctx.inputs.get("tenant_id"),
            "case_id": ctx.inputs.get("case_id"),
            "channel": ctx.inputs.get("channel"),
            "content": ctx.inputs.get("content"),
            "ack": ctx.inputs.get("ack"),
        }, extras=extras_of(self, ctx))
        if res.status != "ok" or not isinstance(res.output, dict):
            return AgentOutput(status="failed", error=failed(res, SKILL_NOTIFY))

        out = res.output
        # ack 缺失**不阻塞**：客户看没看那条短信不是退款是否完成的判据。
        # 挂成 open_questions 会让任务落 BLOCKED（control_plane.py:220），
        # 整个 Plan 就卡在一件 MAOS 控制不了的事情上。
        followup = out["needs_followup"]
        return AgentOutput(
            status="ok",
            artifacts=[artifact(KIND_NOTIFICATION, {
                "notification": out["notification"],
                "acked": out["acked"],
                "needs_followup": followup,
                "invocation_id": out["invocation_id"],
            }, summary=(
                f"已通知客户（{out['notification']['channel']}）；"
                + ("客户已确认" if out["acked"] else "客户未确认，记 needs_followup 不阻塞")
            ))],
            metrics={"acked": out["acked"], "needs_followup": followup},
        )
