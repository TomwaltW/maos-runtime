"""Intake Agent —— 受理一件支付差错。

薄壳：核对原始支付快照、建案。业务判定在 `investigation.file` 里，这里只搬运。

**产物不带自己算出来的金额**：金额一律以快照为准，artifact 里回显的是快照那一份，
不是任务入参那一份 —— 让评委在产物上就能看出这个数字是从哪读来的。
"""

from __future__ import annotations

from maos.agents.base import AgentIdentity, AgentOutput, BaseAgent, TaskContext, register
from maos.model.client import Tier

from ._base import KIND_CASE_FILE, artifact, extras_of, failed

SKILL_FILE = "investigation.file"


@register
class InvestigationIntakeAgent(BaseAgent):
    identity = AgentIdentity(
        agent_id="investigation-intake",
        role="investigation_intake",
        duty="受理支付差错诉求，核对原始支付快照后建案（filed）",
        allowed_skills=frozenset({SKILL_FILE}),
        allowed_tools=frozenset(),
        write_scope=frozenset({"artifact"}),
        max_risk="L",
        model_tier=Tier.LIGHT,
        max_self_repair=0,
    )

    def run(self, ctx: TaskContext) -> AgentOutput:
        self.check_risk(ctx.risk_level)
        self.check_write("artifact")

        res = self.skills.invoke(SKILL_FILE, {
            "tenant_id": ctx.inputs.get("tenant_id"),
            "case_id": ctx.inputs.get("case_id"),
            "original_msg_id": ctx.inputs.get("original_msg_id"),
            "original_version": ctx.inputs.get("original_version"),
            "creator_agent": ctx.inputs.get("creator_agent"),
            "assignee_agent": ctx.inputs.get("assignee_agent"),
            "claimed_amount": ctx.inputs.get("claimed_amount"),
        }, extras=extras_of(self, ctx))
        if res.status != "ok" or not isinstance(res.output, dict):
            return AgentOutput(status="failed", error=failed(res, SKILL_FILE))
        out = res.output
        snap = out["snapshot"]

        return AgentOutput(
            status="ok",
            artifacts=[artifact(KIND_CASE_FILE, {
                "case_id": out["case"]["case_id"],
                "biz_status": out["biz_status"],
                "original_msg_id": snap["original_msg_id"],
                "original_version": snap["version"],
                "end_to_end_id": snap["end_to_end_id"],
                # 金额币种回显快照那一份 —— 产物上就能看出数字是从哪读来的。
                "amount": snap["interbank_amount"],
                "currency": snap["currency"],
                "snapshot_read_at": snap["read_at"],
                "idempotent_replay": out["idempotent_replay"],
                "invocation_id": out["invocation_id"],
            }, summary=(
                f"已受理差错案件 {out['case']['case_id']}（{snap['currency']} "
                f"{snap['interbank_amount']}，原报文 {snap['original_msg_id']} "
                f"v{snap['version']}）；金额币种取自 {snap['read_at']} 读到的快照，"
                f"不是清算系统当前值"
            ))],
            metrics={"biz_status": out["biz_status"],
                     "idempotent_replay": out["idempotent_replay"],
                     "is_rework": ctx.is_rework},
        )
