"""Payment Agent —— 发起退款，再把终态**问**出来。

一个角色两个 skill，顺序不可换、职责不可合：

  · `payment.execute` 只产生请求，写得出 gateway_accepted / processing，
    **写不出 settled**（guard 会抛 AuthoritativeFactViolation）；
  · `payment.observe` 轮询网关，是全系统唯一写得进 settled 的地方。

合成一个 skill 就等于承认「我发出去了所以它成功了」—— 那正是铁律 8 禁止的推断。
分成两个之后，「凭什么说退款成功了」这个问题在代码结构上就有答案：因为
`payment.observe` 问到了终态回执，且回执与状态更新在同一个事务里落了库。

observe 没问出终态时**不失败**：轮询到顶仍是 processing 是一个正常的中间态，
判 failed 会触发重试，而重试意味着再走一遍 execute —— 幂等键挡得住第二笔退款，
但「以为失败了」这个错误结论会一路传下去。
"""

from __future__ import annotations

from maos.agents.base import AgentIdentity, AgentOutput, BaseAgent, TaskContext, register
from maos.model.client import Tier

from ._base import (
    KIND_PAYMENT_RECEIPT,
    KIND_PAYMENT_REQUEST,
    artifact,
    extras_of,
    failed,
)

SKILL_EXECUTE = "payment.execute"
SKILL_OBSERVE = "payment.observe"


@register
class RefundPaymentAgent(BaseAgent):
    identity = AgentIdentity(
        agent_id="refund-payment",
        role="refund_payment",
        duty="核对审批后向支付网关发起退款，并轮询取得终态回执（settled 只能由观察得到）",
        allowed_skills=frozenset({"payment.execute", "payment.observe"}),
        allowed_tools=frozenset({"gateway.refund", "gateway.query"}),
        write_scope=frozenset({"artifact"}),
        max_risk="M",
        model_tier=Tier.LIGHT,
        max_self_repair=0,
    )

    def run(self, ctx: TaskContext) -> AgentOutput:
        self.check_risk(ctx.risk_level)
        self.check_tool("gateway.refund")
        self.check_write("artifact")

        tenant_id = ctx.inputs.get("tenant_id")
        case_id = ctx.inputs.get("case_id")
        gateway = ctx.inputs.get("gateway")

        exec_res = self.skills.invoke(SKILL_EXECUTE, {
            "tenant_id": tenant_id, "case_id": case_id, "gateway": gateway,
        }, extras=extras_of(self, ctx))
        if exec_res.status != "ok" or not isinstance(exec_res.output, dict):
            return AgentOutput(status="failed", error=failed(exec_res, SKILL_EXECUTE))
        sent = exec_res.output

        self.check_tool("gateway.query")
        obs_res = self.skills.invoke(SKILL_OBSERVE, {
            "tenant_id": tenant_id, "case_id": case_id, "gateway": gateway,
            "request_id": sent["request_id"],
            "max_polls": ctx.inputs.get("max_polls"),
        }, extras=extras_of(self, ctx))
        if obs_res.status != "ok" or not isinstance(obs_res.output, dict):
            return AgentOutput(status="failed", error=failed(obs_res, SKILL_OBSERVE))
        seen = obs_res.output

        artifacts = [
            artifact(KIND_PAYMENT_REQUEST, {
                "request_id": sent["request_id"],
                "idempotency_key": sent["idempotency_key"],
                "amount": sent["amount"],
                "receipt": sent["receipt"],
                "biz_status_after_execute": sent["biz_status"],
                "invocation_id": sent["invocation_id"],
            }, summary=(
                f"已向网关发起退款 {sent['amount']}（request_id={sent['request_id']}，"
                f"幂等键 {sent['idempotency_key']}）；受理回执 "
                f"{sent['receipt']['status']}，**非终态**，终态须经 query 观察"
            )),
            artifact(KIND_PAYMENT_RECEIPT, {
                "receipt": seen["receipt"],
                "observed_state": seen["observed_state"],
                "poll_count": seen["poll_count"],
                "settled": seen["settled"],
                "needs_compensation": seen["needs_compensation"],
                "biz_status": seen["biz_status"],
                "remedy": seen["remedy"],
                "source": seen["source"],
                "invocation_id": seen["invocation_id"],
            }, summary=(
                f"观察到网关终态 {seen['observed_state']}（问了 {seen['poll_count']} 次）；"
                f"biz_status={seen['biz_status']} —— 该状态来自回执，不是本地推断"
            )),
        ]

        return AgentOutput(
            status="ok",
            open_questions=self._open_questions(seen),
            artifacts=artifacts,
            metrics={"poll_count": seen["poll_count"], "settled": seen["settled"],
                     "needs_compensation": seen["needs_compensation"],
                     "idempotency_key": sent["idempotency_key"],
                     "is_rework": ctx.is_rework},
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _open_questions(seen: dict) -> list[str]:
        """没到 settled 的两种情形都要显式挂出来给人看 —— 但都不改任务状态。

        「网关说失败了」与「轮询到顶还没问出来」是两回事，措辞必须分开：
        混成一句，后续处置（补偿 vs 继续观察）就会挑错。
        """
        if seen["settled"]:
            return []
        if seen["needs_compensation"]:
            code = (seen.get("receipt") or {}).get("code")
            return [f"网关明确失败（code={code}）：{seen['remedy']}；需走补偿收口"]
        return [f"轮询 {seen['poll_count']} 次仍未取得终态（当前 {seen['observed_state']}）；"
                f"这不是失败，需继续观察：{seen['remedy']}"]
