"""Cancel Agent —— 发出 camt.056 撤销请求。**这是本域的高风险落地动作。**

一个角色一个 skill，且**不带 observe**。与退款域的 `RefundPaymentAgent`
（execute + observe 两个 skill 一个角色）刻意不同：

本域的撤销请求发出去之后，决议可能几小时到几天才回来，中间还要人工调账审批。
把 observe 焊在同一个 Agent 里，等于要求这个任务一直等着 —— 而真实差错处理里
「发出去」和「问结果」本来就是两次值班、两个人。拆开之后 DAG 上看得见这一点。

更要紧的是职责：**发请求的人不许宣布结果**。
`investigation.cancel` 写得出 `cancellation_sent`，写不出 `returned`（guard 会抛）。
合成一个角色就等于承认「我发出去了所以它成功了」—— 铁律 8 禁止的推断。

## effect_risk=H 由派单方设定，不在这里

任务的 `effect_risk` 是**任务属性**，写在 DAG 里（`maos/flows/scenario_9.py`），
不是 Agent 属性。本 Agent 的 `max_risk="M"` 说的是「这个角色最高能执行 M 级任务」，
两者不是一回事（`risk_level` = Agent 执行风险，`effect_risk` = 产物落地风险）。
差错处理的人工调账必须人批是监管硬要求，那道闸落在 `effect_risk=H` 上。
"""

from __future__ import annotations

from maos.agents.base import AgentIdentity, AgentOutput, BaseAgent, TaskContext, register
from maos.model.client import Tier

from ._base import KIND_CANCELLATION_REQUEST, artifact, extras_of, failed

SKILL_CANCEL = "investigation.cancel"


@register
class InvestigationCancelAgent(BaseAgent):
    identity = AgentIdentity(
        agent_id="investigation-cancel",
        role="investigation_cancel",
        duty="核对人工调账审批后向清算方发出 camt.056 撤销请求（写不出 returned）",
        allowed_skills=frozenset({SKILL_CANCEL}),
        allowed_tools=frozenset({"clearing.cancel"}),
        write_scope=frozenset({"artifact"}),
        max_risk="M",
        model_tier=Tier.LIGHT,
        max_self_repair=0,
    )

    def run(self, ctx: TaskContext) -> AgentOutput:
        self.check_risk(ctx.risk_level)
        self.check_tool("clearing.cancel")
        self.check_write("artifact")

        res = self.skills.invoke(SKILL_CANCEL, {
            "tenant_id": ctx.inputs.get("tenant_id"),
            "case_id": ctx.inputs.get("case_id"),
            "clearing": ctx.inputs.get("clearing"),
        }, extras=extras_of(self, ctx))
        if res.status != "ok" or not isinstance(res.output, dict):
            return AgentOutput(status="failed", error=failed(res, SKILL_CANCEL))
        out = res.output

        return AgentOutput(
            status="ok",
            open_questions=[
                f"camt.056 已发出（指派号 {out['idempotency_key']}），"
                f"清算方尚未给出决议；撤销是否成功、资金是否退回**均未知**，"
                f"须由 investigation.observe 问出来"
            ],
            artifacts=[artifact(KIND_CANCELLATION_REQUEST, {
                "case_id": ctx.inputs.get("case_id"),
                "request_id": out["request_id"],
                "idempotency_key": out["idempotency_key"],
                "message_type": out["message_type"],
                "reason_code": out["reason_code"],
                "approved_by": out["approved_by"],
                "receipt": out["receipt"],
                "biz_status": out["biz_status"],
                "invocation_id": out["invocation_id"],
            }, summary=(
                f"已向清算方发出 {out['message_type']} 撤销请求"
                f"（request_id={out['request_id']}，指派号 {out['idempotency_key']}，"
                f"原因码 {out['reason_code']}，经 {out['approved_by']} 批准）；"
                f"受理回执**非终态** —— 决议须经 clearing.resolution 问出来"
            ))],
            metrics={"biz_status": out["biz_status"],
                     "idempotency_key": out["idempotency_key"],
                     "is_rework": ctx.is_rework},
        )
