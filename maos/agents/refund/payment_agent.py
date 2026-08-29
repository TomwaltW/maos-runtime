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
from maos.core.control_plane import (
    GW_HUMAN_TERMINAL,
    GW_QUERY_FIRST,
    GW_QUERY_OR_HUMAN,
    GW_REPLAN_CHANNEL,
)
from maos.model.client import Tier
from maos.runtime.gate import ReviewerGate

from ._base import (
    KIND_PAYMENT_RECEIPT,
    KIND_PAYMENT_REQUEST,
    artifact,
    extras_of,
    failed,
)

SKILL_EXECUTE = "payment.execute"
SKILL_OBSERVE = "payment.observe"

#: 四象限各一句处置说法。**判据不在这里算** —— 码 -> disposition 由第七道闸的
#: `ReviewerGate._gateway_finding` 算一次，本表只把那个结论翻译成给人看的话。
#: 反过来在这里再写一套 code -> 措辞的映射，码表加一条码时只有闸会跟着变，
#: 这句话会悄悄开始说错，而且没有症状（日志照样正常）。
_DISPOSITION_PHRASE = {
    GW_REPLAN_CHANNEL: "网关在入口就拒了、业务确定未执行，可原样重发或换渠道重试",
    GW_QUERY_FIRST: "网关说不清这一笔执行了没有，不许直接重发（会造出第二笔），先 query 再决定",
    GW_HUMAN_TERMINAL: "网关终态失败且重发无意义，需转人工或改单",
    GW_QUERY_OR_HUMAN: "既判不了可重试、也判不了终态失败 —— 最危险的一档，必须 query 或转人工",
}

#: 未知码单独一句：它与 retriable=False + unknown 共用 GW_QUERY_OR_HUMAN，但两者的
#: **理由**完全不同（一个是查过表判成这样，一个是压根不在表里）。混成一句话，
#: 拿到它的人会以为这个码已经被官方文档核对过。
_UNKNOWN_CODE_PHRASE = "网关回执带的码不在已核对的官方清单内，按未知外部状态处置"


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
        """没到 settled 就把处置显式挂出来给人看 —— 但一律不改任务状态。

        **判据与第七道闸同源**：措辞按 ``ReviewerGate._gateway_finding(code)`` 返回的
        ``disposition`` 分档，而不是按 ``needs_compensation`` 分「网关明确失败 /
        轮询到顶」两句。原先那个分法当时也没判错，但它是**另一套判据**：闸看四象限
        （``retriable`` × ``outcome``，见 ``maos/tools/gateway_codes.py``），这里只看
        一个 bool。同一份回执被两套判据各判一次，码表将来加一条码或改一个
        ``outcome``，只有闸会跟着变，这句话会悄悄漂 —— 而这类漂**没有症状**：
        日志照样正常，只是那句话开始说错。所以这里做调用方，不重写映射。
        """
        if seen["settled"]:
            return []

        code = (seen.get("receipt") or {}).get("code")
        finding = (ReviewerGate._gateway_finding(code)
                   if isinstance(code, str) and code else None)

        # finding 为 None = 成功码或压根没带码：网关一个异常都没报，纯粹是还没问出
        # 终态。这一档才是真正的「我问累了」，与上面四象限任何一格都不是一回事。
        if finding is None:
            return [f"轮询 {seen['poll_count']} 次仍未取得终态（当前 {seen['observed_state']}）；"
                    f"这不是失败，需继续观察：{seen['remedy']}"]

        head = (_UNKNOWN_CODE_PHRASE if finding.get("retriable") is None
                else _DISPOSITION_PHRASE[finding["disposition"]])
        remedy = finding.get("remedy") or seen.get("remedy") or ""
        tail = f"；官方处置：{remedy}" if remedy else ""
        return [f"{head}（code={finding['code']} retriable={finding['retriable']} "
                f"outcome={finding['outcome']}，已问 {seen['poll_count']} 次仍未取得终态）"
                f"{tail}"]
