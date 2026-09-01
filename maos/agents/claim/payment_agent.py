"""Claim Payment Agent —— 发起赔付，再把到账**问**出来。

一个角色两个 skill，顺序不可换、职责不可合：

  · `claim.pay` 只产生赔付指令，写得出 payment_requested，
    **写不出 paid**（guard 会抛 AuthoritativeFactViolation）；
  · `claim.observe` 轮询赔付方，是全系统唯一写得进 paid 的地方。

合成一个 skill 就等于承认「我发出去了所以它到账了」—— 那正是铁律 8 禁止的推断。
分成两个之后，「凭什么说赔款到账了」这个问题在代码结构上就有答案：因为
`claim.observe` 问到了到账回执，且回执与状态更新在同一个事务里落了库。

observe 没问出终态时**不失败**：轮询到顶仍是 processing 是一个正常的中间态，
判 failed 会触发重试，而重试意味着再走一遍 pay —— 幂等键挡得住第二笔赔款，
但「以为拒付了」这个错误结论会一路传下去。

## 本 Agent 在失败路径上照样返回 status="ok"

这是场景 8 失败路径要买的第二样东西：**「Agent 都说完成了」不等于业务成功。**
赔付方拒付、或轮询到顶问不出终态，本 Agent 都如实交出回执产物并返回 ok ——
它确实完成了它该做的事（发指令、问结果、把看到的记下来）。案子没成这件事，
由 `claim_case.biz_status`、`claim_payment_observation` 的行数与 Plan 终态说，
不由 Agent 的 status 说。把这里改成 failed 反而会掩盖那句话。
"""

from __future__ import annotations

from maos.agents.base import AgentIdentity, AgentOutput, BaseAgent, TaskContext, register
from maos.model.client import Tier
from maos.tools import claim_codes

from ._base import (
    KIND_PAYER_RECEIPT,
    KIND_PAYMENT_INSTRUCTION,
    RECEIPT_FIELD,
    artifact,
    extras_of,
    failed,
)

SKILL_PAY = "claim.pay"
SKILL_OBSERVE = "claim.observe"

#: 四种处置各一句给人看的话。**判据不在这里算** —— 码 -> recourse 由
#: `maos/tools/claim_codes.py` 定一次，本表只把那个结论翻译成人话。
#: 反过来在这里再写一套 code -> 措辞的映射，码表加一条码时只有码表会跟着变，
#: 这句话会悄悄开始说错，而且没有症状（日志照样正常）。
_RECOURSE_PHRASE = {
    claim_codes.RECOURSE_NONE:
        "赔付方给的是终态拒赔，补什么都改不了结论，重报无意义 —— 走补偿并向被保险人解释",
    claim_codes.RECOURSE_RESUBMIT:
        "申报件缺信息或缺单据，补齐之后可以重报 —— 这一格才允许再报一次",
    claim_codes.RECOURSE_OTHER_PAYER:
        "该送给另一个赔付方，不是这一个；机器不许自行改投，先由人定下送给谁",
    claim_codes.RECOURSE_HUMAN:
        "只有人能推进（申诉 / 补授权 / 线下沟通），机器重报无意义",
}

#: 未知码单独一句：它与「终态拒赔」的处置看起来一样，但**理由**完全不同
#: （一个是查过表判成这样，一个是压根不在表里）。混成一句话，拿到它的人会以为
#: 这个码已经被官方文档核对过。
_UNKNOWN_CODE_PHRASE = (
    "赔付方回执带的 CARC 不在已核对的 X12 官方清单内，按未知外部状态处置 —— "
    "先核出处再决定，不许兜底重报")


@register
class ClaimPaymentAgent(BaseAgent):
    identity = AgentIdentity(
        agent_id="claim-payment",
        role="claim_payment",
        duty="核对审批后向赔付方发起赔付指令，并轮询取得到账回执（paid 只能由观察得到）",
        allowed_skills=frozenset({"claim.pay", "claim.observe"}),
        allowed_tools=frozenset({"payer.submit", "payer.query"}),
        write_scope=frozenset({"artifact"}),
        max_risk="M",
        model_tier=Tier.LIGHT,
        max_self_repair=0,
    )

    def run(self, ctx: TaskContext) -> AgentOutput:
        self.check_risk(ctx.risk_level)
        self.check_tool("payer.submit")
        self.check_write("artifact")

        tenant_id = ctx.inputs.get("tenant_id")
        claim_id = ctx.inputs.get("claim_id")
        payer = ctx.inputs.get("payer")

        pay_res = self.skills.invoke(SKILL_PAY, {
            "tenant_id": tenant_id, "claim_id": claim_id, "payer": payer,
            "payee": ctx.inputs.get("payee"),
        }, extras=extras_of(self, ctx))
        if pay_res.status != "ok" or not isinstance(pay_res.output, dict):
            return AgentOutput(status="failed", error=failed(pay_res, SKILL_PAY))
        sent = pay_res.output

        self.check_tool("payer.query")
        obs_res = self.skills.invoke(SKILL_OBSERVE, {
            "tenant_id": tenant_id, "claim_id": claim_id, "payer": payer,
            "request_id": sent["request_id"],
            "max_polls": ctx.inputs.get("max_polls"),
        }, extras=extras_of(self, ctx))
        if obs_res.status != "ok" or not isinstance(obs_res.output, dict):
            return AgentOutput(status="failed", error=failed(obs_res, SKILL_OBSERVE))
        seen = obs_res.output

        artifacts = [
            artifact(KIND_PAYMENT_INSTRUCTION, {
                "request_id": sent["request_id"],
                "idempotency_key": sent["idempotency_key"],
                "amount": sent["amount"],
                "primary_rule": sent["primary_rule"],
                "terms_version": sent["terms_version"],
                "rule_refs": sent["rule_refs"],
                # 键名是 payer_receipt 不是 receipt —— 见 _base.RECEIPT_FIELD 的说明。
                RECEIPT_FIELD: sent["payer_receipt"],
                "biz_status_after_pay": sent["biz_status"],
                "invocation_id": sent["invocation_id"],
            }, summary=(
                f"已向赔付方发起赔付 {sent['amount']}（request_id={sent['request_id']}，"
                f"幂等键 {sent['idempotency_key']}）；受理回执 "
                f"{sent['payer_receipt']['status']}，**非到账**，到账须经 query 观察；"
                f"依据 {sent['primary_rule']}@v{sent['terms_version']}"
            )),
            artifact(KIND_PAYER_RECEIPT, {
                RECEIPT_FIELD: seen["payer_receipt"],
                "observed_state": seen["observed_state"],
                "poll_count": seen["poll_count"],
                "paid": seen["paid"],
                "needs_compensation": seen["needs_compensation"],
                "biz_status": seen["biz_status"],
                "carc_code": seen["carc_code"],
                "group_code": seen["group_code"],
                "remark_codes": seen["remark_codes"],
                "description": seen["description"],
                "recourse": seen["recourse"],
                "source": seen["source"],
                "fetched_at": seen["fetched_at"],
                "invocation_id": seen["invocation_id"],
            }, summary=(
                f"观察到赔付方回执 {seen['observed_state']}（问了 {seen['poll_count']} 次）；"
                f"biz_status={seen['biz_status']} —— 该状态来自回执，不是本地推断"
                + (f"；CARC {seen['carc_code']}（{seen['group_code']}）"
                   if seen["carc_code"] else "")
            )),
        ]

        return AgentOutput(
            status="ok",
            open_questions=self._open_questions(seen),
            artifacts=artifacts,
            metrics={"poll_count": seen["poll_count"], "paid": seen["paid"],
                     "needs_compensation": seen["needs_compensation"],
                     "carc_code": seen["carc_code"],
                     "idempotency_key": sent["idempotency_key"],
                     "is_rework": ctx.is_rework},
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _open_questions(seen: dict) -> list[str]:
        """没到 paid 就把处置显式挂出来给人看 —— 但一律不改任务状态。

        **判据与码表同源**：措辞按 `claim_codes.recourse_of(code)` 分档，而不是按
        `needs_compensation` 那一个 bool 分「拒付 / 轮询到顶」两句。同一份回执被两套
        判据各判一次，码表将来加一条码或改一个 recourse，只有码表会跟着变，这句话会
        悄悄漂 —— 而这类漂**没有症状**：日志照样正常，只是那句话开始说错。
        """
        if seen["paid"]:
            return []

        carc = str(seen.get("carc_code") or "")
        if not carc:
            # 没带码：赔付方一个调整都没报，纯粹是还没问出终态。这一档才是真正的
            # 「我问累了」，与四种处置任何一格都不是一回事。
            return [f"轮询 {seen['poll_count']} 次仍未取得终态"
                    f"（当前 {seen['observed_state']}）；这不是拒付，需继续观察或转人工"]

        try:
            recourse = claim_codes.recourse_of(carc)
        except KeyError:
            return [f"{_UNKNOWN_CODE_PHRASE}（carc={carc}，已问 {seen['poll_count']} 次）"]

        head = _RECOURSE_PHRASE[recourse]
        desc = seen.get("description") or ""
        tail = f"；官方描述原文：{desc}" if desc else ""
        return [f"{head}（CARC {carc} / 组码 {seen.get('group_code')}，"
                f"备注码 {seen.get('remark_codes')}，已问 {seen['poll_count']} 次）{tail}"]
