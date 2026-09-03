"""AP Treasury Agent —— 资金岗：发出付款指令，再把终态**问**出来。

一个角色两个 skill，顺序不可换、职责不可合：

  · `ap.execute` 只产生指令，写得出 `payment_requested`，**写不出 settled**
    （guard 会抛 `AuthoritativeFactViolation` 并落一条事件）；
  · `ap.observe` 轮询银行，是全系统唯一写得进 `settled` 的地方。

合成一个 skill 就等于承认「我发出去了所以它付掉了」—— 那正是铁律 8 禁止的推断。
分成两个之后，「凭什么说这笔货款付出去了」在代码结构上就有答案：因为
`ap.observe` 问到了带流水号的终态回单，且回单与状态更新在同一个事务里落了库。

## observe 没问出终态时**不失败**

轮询到顶仍是 `pending` / `unknown` 是一个正常的中间态，判 failed 会触发返工，
而返工意味着再走一遍 `execute` —— 幂等键挡得住第二笔付款，但「以为没付成」这个
错误结论会一路传下去，最后变成对同一张发票的第二次付款流程。

所以这一档 `status=ok` + `open_questions`，任务落 BLOCKED 等人处置。
这就是本轨要买的那句话最直白的样子：**四个 Agent 全回 ok，而这笔钱到底付没付
出去，系统如实说「不知道」。**
"""

from __future__ import annotations

from maos.agents.base import AgentIdentity, AgentOutput, BaseAgent, TaskContext, register
from maos.model.client import Tier
from maos.tools.ap import ADVICE_FIELD, STATUS_FAILED, STATUS_UNKNOWN

from ._base import (
    KIND_BANK_ADVICE,
    KIND_PAYMENT_INSTRUCTION,
    artifact,
    extras_of,
    failed,
)

SKILL_EXECUTE = "ap.execute"
SKILL_OBSERVE = "ap.observe"

#: 三种非终态各一句处置说法。判据在 `maos/tools/ap.py` 的 STATUS_* 上，
#: 这里只把状态翻成给人看的话 —— **不在这里另判一次成败**。
_STATE_PHRASE = {
    STATUS_UNKNOWN: (
        "银行说不清这笔指令的下落，**该笔可能已经划出** —— 不许重发指令"
        "（重发会付出第二笔），只能继续问或转人工对账"),
    "pending": "银行还在清算，尚未给出终态；这不是失败，需继续观察",
    "accepted": "银行只受理了指令，还没有开始清算；这不是失败，需继续观察",
}


@register
class ApTreasuryAgent(BaseAgent):
    identity = AgentIdentity(
        agent_id="ap-treasury",
        role="ap_treasury",
        duty="核对审批后向银行发出付款指令，并轮询取得终态回单（settled 只能由观察得到）",
        allowed_skills=frozenset({"ap.execute", "ap.observe"}),
        allowed_tools=frozenset({"bank.pay", "bank.query"}),
        write_scope=frozenset({"artifact"}),
        max_risk="M",
        model_tier=Tier.LIGHT,
        max_self_repair=0,
    )

    def run(self, ctx: TaskContext) -> AgentOutput:
        self.check_risk(ctx.risk_level)
        self.check_tool("bank.pay")
        self.check_write("artifact")

        tenant_id = ctx.inputs.get("tenant_id")
        case_id = ctx.inputs.get("case_id")
        bank = ctx.inputs.get("bank")

        exec_res = self.skills.invoke(SKILL_EXECUTE, {
            "tenant_id": tenant_id, "case_id": case_id, "bank": bank,
        }, extras=extras_of(self, ctx))
        if exec_res.status != "ok" or not isinstance(exec_res.output, dict):
            return AgentOutput(status="failed", error=failed(exec_res, SKILL_EXECUTE))
        sent = exec_res.output

        self.check_tool("bank.query")
        obs_res = self.skills.invoke(SKILL_OBSERVE, {
            "tenant_id": tenant_id, "case_id": case_id, "bank": bank,
            "instruction_id": sent["instruction_id"],
            "max_polls": ctx.inputs.get("max_polls"),
        }, extras=extras_of(self, ctx))
        if obs_res.status != "ok" or not isinstance(obs_res.output, dict):
            return AgentOutput(status="failed", error=failed(obs_res, SKILL_OBSERVE))
        seen = obs_res.output

        artifacts = [
            artifact(KIND_PAYMENT_INSTRUCTION, {
                "instruction_id": sent["instruction_id"],
                "idempotency_key": sent["idempotency_key"],
                "amount": sent["amount"],
                "currency": sent["currency"],
                "bank": sent["bank"],
                "approved_by": sent["approved_by"],
                ADVICE_FIELD: sent[ADVICE_FIELD],
                "biz_status_after_execute": sent["biz_status"],
                "invocation_id": sent["invocation_id"],
            }, summary=(
                f"已向银行发出付款指令 {sent['amount']} {sent['currency']}"
                f"（instruction_id={sent['instruction_id']}，幂等键 "
                f"{sent['idempotency_key']}，批准人 {sent['approved_by']}）；"
                f"受理回单 {sent[ADVICE_FIELD]['status']}，**非终态**，"
                f"终态须经 bank.query 观察"
            )),
            artifact(KIND_BANK_ADVICE, {
                ADVICE_FIELD: seen[ADVICE_FIELD],
                "observed_state": seen["observed_state"],
                "poll_count": seen["poll_count"],
                "bank_reference": seen["bank_reference"],
                "value_date": seen["value_date"],
                "settled": seen["settled"],
                "needs_compensation": seen["needs_compensation"],
                "biz_status": seen["biz_status"],
                "invocation_id": seen["invocation_id"],
            }, summary=(
                f"观察银行回单 {seen['observed_state']}（问了 {seen['poll_count']} 次）"
                + (f"，流水号 {seen['bank_reference']}" if seen["bank_reference"] else "")
                + f"；biz_status={seen['biz_status']} —— 该状态来自回单，不是本地推断"
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

        挂 open_questions 会让任务落 BLOCKED，也就是停下来等人。这正是这三档
        （accepted / pending / unknown）与 failed 应有的共同出口：机器不该替
        银行下结论，也不该假装什么都没发生就往下走。
        """
        if seen["settled"]:
            return []
        state = str(seen["observed_state"])
        if state == STATUS_FAILED:
            return [f"银行明确拒付（问了 {seen['poll_count']} 次）：{seen['message']}；"
                    f"需走域内补偿并转人工，不得重发指令"]
        phrase = _STATE_PHRASE.get(state, f"回单状态 {state}，不是终态")
        return [f"{phrase}（已问 {seen['poll_count']} 次仍未取得终态，"
                f"当前 {state}）：{seen['message']}"]
