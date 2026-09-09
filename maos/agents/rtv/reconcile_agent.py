"""RTV Reconcile Agent —— 三方对账：退货行 × 贷项通知单 × 到账。

这是本域相对 ap 域的核心差别（契约抬头那张表）：ap 域是三单匹配（PO × GR ×
Invoice），本域是**三方对账**，而三方里有**两个外部权威**（供应商开票、AP 到账）。
对账只产出「对上没对上、可退多少、对不上的理由是什么」，
**不产出终态** —— 终态归 `rtv.observe`（C-R3）。

`allowed_tools` 只有 `supplier.credit_query`（只读）：对账的人不许写供应商门户，
也不碰承运商。查贷项通知单和开贷项通知单是两件事，后者根本不是我方能做的。

薄壳：容差、勾稽、规则编号全在 `rtv.reconcile` 里；本文件只搬运和包装。
"""

from __future__ import annotations

from maos.agents.base import AgentIdentity, AgentOutput, BaseAgent, TaskContext, register
from maos.model.client import Tier

from ._base import KIND_RTV_RECONCILIATION, artifact, extras_of, failed

SKILL_RECONCILE = "rtv.reconcile"

TOOL_SUPPLIER_CREDIT_QUERY = "supplier.credit_query"


@register
class RtvReconcileAgent(BaseAgent):
    identity = AgentIdentity(
        agent_id="rtv-reconcile",
        role="rtv_reconcile",
        duty="退货行 × 贷项通知单 × 到账三方对账，产出可核对的结论",
        allowed_skills=frozenset({"rtv.reconcile"}),
        allowed_tools=frozenset({"supplier.credit_query"}),
        write_scope=frozenset({"artifact"}),
        max_risk="M",
        model_tier=Tier.LIGHT,
        max_self_repair=0,
    )

    def run(self, ctx: TaskContext) -> AgentOutput:
        self.check_risk(ctx.risk_level)
        self.check_tool(TOOL_SUPPLIER_CREDIT_QUERY)
        self.check_write("artifact")

        res = self.skills.invoke(SKILL_RECONCILE, {
            "tenant_id": ctx.inputs.get("tenant_id"),
            "case_id": ctx.inputs.get("case_id"),
            # 容差按案子给（不同供应商的合同容差不同），任务上没写就用 skill 的缺省。
            "tolerance": ctx.inputs.get("tolerance"),
            "reconciled_by": self.identity.agent_id,
        }, extras=extras_of(self, ctx))
        if res.status != "ok" or not isinstance(res.output, dict):
            return AgentOutput(status="failed", error=failed(res, SKILL_RECONCILE))

        # `rtv.reconcile` 的产出是扁平的，不是包在 "reconciliation" 里。
        # 两个金额**都搬出来**：我方按退货行算的那个与供应商贷项通知单认的那个是两个
        # 事实，摘要里合并成一个数，「以外部权威为准」这句话就没地方落了。
        out = res.output
        return AgentOutput(
            status="ok",
            open_questions=list(out.get("open_questions") or []),
            artifacts=[artifact(KIND_RTV_RECONCILIATION, dict(out), summary=(
                f"三方对账第 {out['attempt']} 次：reconciled={out['reconciled']}；"
                f"我方按退货行算 {out['amount_claimed']!r}、供应商贷项通知单认 "
                f"{out['amount_credited']!r}、可动账 {out['creditable_amount']!r}；"
                f"供应商侧 {out['supplier_status']}，AP 侧已观察到核销="
                f"{out['settled_observed']}；findings "
                f"{[f['rule_id'] for f in out['findings']]}；"
                f"biz_status={out['biz_status']} —— 对账只出结论，终态归 rtv.observe"
            ))],
            metrics={"reconciled": out["reconciled"], "findings": len(out["findings"]),
                     "supplier_status": out["supplier_status"],
                     "is_rework": ctx.is_rework},
        )
