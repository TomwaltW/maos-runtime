"""RTV Intake Agent —— 面向退货诉求那一端：定位源 PO 与收货单，建案。

角色边界按**面向谁**划分，不按流程位置划分。受理面对的是退货诉求与源头单据；
裁定面对的是合同条款；发运面对的是承运商；对账面对的是供应商开的贷项通知单；
终态观察面对的是 AP / 银行。五者的证据口径与失败处置完全不同，
所以是五个角色，不是一个角色的五个步骤（契约 C-R7）。

**建案时不写 return_action**（C-R1 里那一列 `DEFAULT ''`）：受理的人不该替裁定的
人拍板。这条口径落在 skill 里，本文件连碰都不碰。

薄壳：Identity + 经 SkillInvoker 调 skill，不写业务逻辑。
放文件即注册（C-2 pkgutil 自动发现），不碰 `maos/agents/__init__.py`。
"""

from __future__ import annotations

from maos.agents.base import AgentIdentity, AgentOutput, BaseAgent, TaskContext, register
from maos.model.client import Tier

from ._base import KIND_RTV_INTAKE, artifact, extras_of, failed

SKILL_INTAKE = "rtv.intake"


@register
class RtvIntakeAgent(BaseAgent):
    identity = AgentIdentity(
        agent_id="rtv-intake",
        role="rtv_intake",
        duty="受理退货诉求，定位源 PO 与收货单，建案并挂上业务对象引用",
        allowed_skills=frozenset({"rtv.intake"}),
        allowed_tools=frozenset(),          # 受理不碰承运商，也不碰供应商门户
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
            "supplier_id": ctx.inputs.get("supplier_id"),
            "po_id": ctx.inputs.get("po_id"),
            "po_version": ctx.inputs.get("po_version"),
            "gr_id": ctx.inputs.get("gr_id"),
            "lines": ctx.inputs.get("lines"),
            "amount_claimed": ctx.inputs.get("amount_claimed"),
            "currency": ctx.inputs.get("currency"),
        }, extras=extras_of(self, ctx))
        if res.status != "ok" or not isinstance(res.output, dict):
            return AgentOutput(status="failed", error=failed(res, SKILL_INTAKE))

        out = res.output
        case = out["case"]
        return AgentOutput(
            status="ok",
            artifacts=[artifact(KIND_RTV_INTAKE, dict(out), summary=(
                f"受理退货案 {case['case_id']}（供应商 {case['supplier_id']}，"
                f"源单 {case['po_id']} v{case['po_version']} / {case['gr_id']}）；"
                f"退货行 {len(out['lines'])} 行，自称应退 "
                f"{case['amount_claimed']} {case['currency']}"
                f"（真正认的金额以供应商贷项通知单为准）；"
                f"return_action={case['return_action']!r} —— 受理不替裁定拍板；"
                f"biz_status={case['biz_status']}"
            ))],
            metrics={"lines": len(out["lines"]), "refs": len(out["refs"]),
                     "is_rework": ctx.is_rework},
        )
