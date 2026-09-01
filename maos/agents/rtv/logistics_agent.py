"""RTV Logistics Agent —— 面向承运商：登记退货发运，并把回执**取**回来。

C-R2 的 SOP 第 ③ 步写着 `disposed -> shipped` 由**承运商**说了算。所以本角色持有
两个工具而不是一个：`carrier.ship` 下运单（写），`carrier.track` 取轨迹（只读）。
合成一个就等于承认「我发出去了所以它在路上」—— 那是推断，不是观察。
`rtv_shipment.carrier_status` 那一列的注释说的就是这件事：
承运商是**外部**，这一列是观察结果不是我方决定。

`shipped` 不是权威终态（C-R3 的 `AUTHORITATIVE_STATES` 只有两个），所以它由
`rtv.ship` 推进；但推进的依据仍然是回执，不是「我调用成功了」。

薄壳：两次 `check_tool` 过闸，一次 skill 调用，一份 artifact。
"""

from __future__ import annotations

from maos.agents.base import AgentIdentity, AgentOutput, BaseAgent, TaskContext, register
from maos.model.client import Tier

from ._base import KIND_RTV_SHIPMENT, artifact, extras_of, failed

SKILL_SHIP = "rtv.ship"

TOOL_CARRIER_SHIP = "carrier.ship"
TOOL_CARRIER_TRACK = "carrier.track"


@register
class RtvLogisticsAgent(BaseAgent):
    identity = AgentIdentity(
        agent_id="rtv-logistics",
        role="rtv_logistics",
        duty="登记退货发运并取得承运商回执（shipped 只能由回执得到）",
        allowed_skills=frozenset({"rtv.ship"}),
        allowed_tools=frozenset({"carrier.ship", "carrier.track"}),
        write_scope=frozenset({"artifact"}),
        max_risk="M",
        model_tier=Tier.LIGHT,
        max_self_repair=0,
    )

    def run(self, ctx: TaskContext) -> AgentOutput:
        self.check_risk(ctx.risk_level)
        self.check_tool(TOOL_CARRIER_SHIP)
        self.check_tool(TOOL_CARRIER_TRACK)
        self.check_write("artifact")

        res = self.skills.invoke(SKILL_SHIP, {
            "tenant_id": ctx.inputs.get("tenant_id"),
            "case_id": ctx.inputs.get("case_id"),
            "carrier": ctx.inputs.get("carrier"),
        }, extras=extras_of(self, ctx))
        if res.status != "ok" or not isinstance(res.output, dict):
            return AgentOutput(status="failed", error=failed(res, SKILL_SHIP))

        out = res.output
        s = out["shipment"]
        return AgentOutput(
            status="ok",
            artifacts=[artifact(KIND_RTV_SHIPMENT, dict(out), summary=(
                f"退货发运 {s['shipment_id']}（承运商 {s['carrier']}，运单 "
                f"{s['tracking_no']}，发出 {s['shipped_at']}）；承运商回执 "
                f"{s['carrier_status']}；biz_status={out['biz_status']} —— "
                f"该状态来自承运商回执，不是「我调用成功了」的推断"
            ))],
            metrics={"carrier_status": s["carrier_status"],
                     "is_rework": ctx.is_rework},
        )
