"""AP Intake Agent —— 面向供应商那一端：收票、确认三单齐备、建案。

角色边界按**面向谁**划分，不按流程位置划分。收票面对的是供应商与发票池；
匹配面对的是三份单据之间的分歧；付款面对的是银行。三者的证据口径与失败处置
完全不同，所以是三个角色，不是一个角色的三个步骤。

薄壳：Identity + 经 SkillInvoker 调 skill，不写业务逻辑。
放文件即注册（C-2 pkgutil 自动发现），不碰 `maos/agents/__init__.py`。
"""

from __future__ import annotations

from maos.agents.base import AgentIdentity, AgentOutput, BaseAgent, TaskContext, register
from maos.model.client import Tier

from ._base import KIND_INVOICE_INTAKE, artifact, extras_of, failed

SKILL_INTAKE = "ap.intake"


@register
class ApIntakeAgent(BaseAgent):
    identity = AgentIdentity(
        agent_id="ap-intake",
        role="ap_intake",
        duty="收供应商发票，确认采购订单与收货单齐备，建出应付案子并挂上业务对象引用",
        allowed_skills=frozenset({"ap.intake"}),
        allowed_tools=frozenset(),          # 收票不碰银行
        write_scope=frozenset({"artifact"}),
        max_risk="L",
        model_tier=Tier.LIGHT,
        max_self_repair=0,
    )

    def run(self, ctx: TaskContext) -> AgentOutput:
        self.check_risk(ctx.risk_level)
        self.check_write("artifact")

        res = self.skills.invoke(SKILL_INTAKE, {
            "tenant_id": ctx.inputs.get("tenant_id"),
            "case_id": ctx.inputs.get("case_id"),
            "invoice_id": ctx.inputs.get("invoice_id"),
            "po_id": ctx.inputs.get("po_id"),
            "po_version": ctx.inputs.get("po_version"),
            "gr_id": ctx.inputs.get("gr_id"),
        }, extras=extras_of(self, ctx))
        if res.status != "ok" or not isinstance(res.output, dict):
            return AgentOutput(status="failed", error=failed(res, SKILL_INTAKE))

        out = res.output
        case, inv, three = out["case"], out["invoice"], out["three_way"]
        return AgentOutput(
            status="ok",
            artifacts=[artifact(KIND_INVOICE_INTAKE, {
                "case": case,
                "invoice": inv,
                "three_way": three,
                "refs": out["refs"],
                "invocation_id": out["invocation_id"],
            }, summary=(
                f"收票 {inv['invoice_id']}（类型 {inv['invoice_type_code']} "
                f"{inv['invoice_type_name']}，供应商 {inv['supplier_id']}）；"
                f"三单齐备：发票 {three['invoice_lines']} 行 / 订单 "
                f"{three['po_lines']} 行（v{three['po_version']}）/ 收货 "
                f"{three['gr_lines']} 行；biz_status={case['biz_status']}"
            ))],
            metrics={"invoice_lines": three["invoice_lines"],
                     "po_lines": three["po_lines"], "gr_lines": three["gr_lines"],
                     "refs": len(out["refs"]), "is_rework": ctx.is_rework},
        )
