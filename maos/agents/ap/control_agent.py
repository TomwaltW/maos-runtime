"""AP Control Agent —— 应付控制岗：出付款计划，交给人批。

这个角色产出的那份 artifact 就是**主管在 Matrix 房间里看到的东西**。所以它的
职责边界很窄也很硬：把匹配结论翻成一份可核对的付款计划，然后停下来。

它不发指令、不碰银行（`allowed_tools` 是空的），也不写审批记录 —— 审批是人的
动作。它唯一的产出是「要付多少、按什么依据、怎么付、什么时候付」这四个问题的
答案，每一个都挂着规范编号。

## 为什么 effect_risk=H 挂在这个任务上

`effect_risk` 说的是**产物落地**的风险，不是 Agent 执行的风险。本 Agent 的执行是
只读的（`risk_level=L` 就够），但它的产物一旦被批准，下一步就是把钱打出去 ——
不可逆。所以派任务时给它 `effect_risk=H`，Gate 过了也停在 BLOCKED 等人放行。

这不是本文件能决定的事（`effect_risk` 在任务规格上，见 `flows/scenario_10.py`），
写在这里是因为读到这个 Agent 的人会问「审批卡在哪一步」。
"""

from __future__ import annotations

from maos.agents.base import AgentIdentity, AgentOutput, BaseAgent, TaskContext, register
from maos.model.client import Tier

from ._base import KIND_PAYMENT_PLAN, artifact, extras_of, failed

SKILL_PLAN = "ap.plan-payment"


@register
class ApControlAgent(BaseAgent):
    identity = AgentIdentity(
        agent_id="ap-control",
        role="ap_control",
        duty="按三单匹配的结论出付款计划（金额/付款方式/到期日/依据），交人工审批",
        allowed_skills=frozenset({"ap.plan-payment"}),
        allowed_tools=frozenset(),          # 出计划不碰银行
        write_scope=frozenset({"artifact"}),
        max_risk="M",
        model_tier=Tier.LIGHT,
        max_self_repair=0,
    )

    def run(self, ctx: TaskContext) -> AgentOutput:
        self.check_risk(ctx.risk_level)
        self.check_write("artifact")

        res = self.skills.invoke(SKILL_PLAN, {
            "tenant_id": ctx.inputs.get("tenant_id"),
            "case_id": ctx.inputs.get("case_id"),
            "attempt": ctx.inputs.get("match_attempt"),
        }, extras=extras_of(self, ctx))
        if res.status != "ok" or not isinstance(res.output, dict):
            return AgentOutput(status="failed", error=failed(res, SKILL_PLAN))

        out = res.output
        plan = out["plan"]
        rules = ", ".join(c["rule_id"] for c in out["citations"])
        return AgentOutput(
            status="ok",
            artifacts=[artifact(KIND_PAYMENT_PLAN, {
                "plan": plan,
                "payable_amount": out["payable_amount"],
                "citations": out["citations"],
                "needs_human_approval": out["needs_human_approval"],
                "biz_status": out["biz_status"],
                "invocation_id": out["invocation_id"],
            }, summary=(
                f"付款计划：付 {plan['supplier_name']}（{plan['supplier_id']}）"
                f"{plan['amount']} {plan['currency']}，方式 "
                f"{plan['payment_means_code']} {plan['payment_means_name']}，"
                f"到期 {plan['due_at'] or '未注明'}；金额依据 {rules}；"
                f"发票 {plan['invoice_id']} / 订单 {plan['po_id']}；"
                f"出账不可逆，须人工放行"
            ))],
            metrics={"payable_amount": out["payable_amount"],
                     "needs_human_approval": out["needs_human_approval"],
                     "is_rework": ctx.is_rework},
        )
