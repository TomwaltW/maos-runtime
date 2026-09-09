"""RTV Settlement Agent —— 终态观察岗：把**两个**外部权威的回执问出来。

本域相对 ap / refund 域的增量就在这里（契约抬头那张表）：ap 域只有一个外部权威
（银行回单），本域有两个 —— 「供应商认不认这笔退货」和「钱到没到账」是两件独立
的事，由两个不同的外部系统说了算。C-R3 的 `AUTHORITATIVE_STATES` 因此有两个元素，
`AUTHORITATIVE_RECEIPT_STATE` 给出各自的判据，两者同增同减。

## 一个角色两个 skill，但 `run()` 只调其中一个

`allowed_skills` 按 C-R7 有两个（`rtv.observe` / `rtv.compensate`），
可 `run()` 里只有 `rtv.observe`：

  · `rtv.observe` 是本域**唯一**的权威写入方（C-R3 的 `AUTHORITATIVE_WRITER`）；
  · `rtv.compensate` 是**人做出决定之后**的收口动作，由编排层用**本角色的
    identity** 经 `SkillInvoker` 发起（见 `maos/flows/scenario_12.py::compensate`）。

为什么不让 Agent 自己在 `run()` 里决定要不要补偿：那是一次业务判定，
一旦写进来，「换域只换 Skill」就不成立了。也不为补偿另起第六个角色 ——
那会让「补偿是谁做的」这个问题多一个含糊的答案；权限已经在本角色的白名单里，
编排层复用它就够了，审计链照旧落在 `SkillInvoked` 上。

## 收口与否也不在这里判

没问出权威回执时要不要停下来等人，判据在 `rtv.observe` 里，本文件把 skill 返回的
`open_questions` **原样搬出去**。挂了 open_questions 的任务会落 BLOCKED 等人处置 ——
机器不该替供应商或 AP 下结论，也不该假装什么都没发生就往下走（铁律 8）。

`status` 一律照 skill 的成败给：`AgentOutput.status` 说的是「这一步跑完了没有」，
不是「业务成功了没有」。失败路径上五个 Agent 全回 ok，而案子确实没成 ——
这正是场景 12 失败路径要演的那句话。
"""

from __future__ import annotations

from maos.agents.base import AgentIdentity, AgentOutput, BaseAgent, TaskContext, register
from maos.model.client import Tier

from ._base import KIND_RTV_SETTLEMENT_ADVICE, artifact, extras_of, failed

SKILL_OBSERVE = "rtv.observe"

#: 补偿 skill 的名字。**本文件不调用它** —— 见模块 docstring「一个角色两个 skill」。
#: 常量留在这里是给编排层按名取，不在场景里抄字面量。
SKILL_COMPENSATE = "rtv.compensate"

TOOL_SUPPLIER_CREDIT_QUERY = "supplier.credit_query"
TOOL_AP_ADJUST_QUERY = "ap.adjust_query"


@register
class RtvSettlementAgent(BaseAgent):
    identity = AgentIdentity(
        agent_id="rtv-settlement",
        role="rtv_settlement",
        duty="轮询取得终态回执（credited 与 settled 都只能由观察得到）",
        allowed_skills=frozenset({"rtv.observe", "rtv.compensate"}),
        allowed_tools=frozenset({"supplier.credit_query", "ap.adjust_query",
                                 "supplier.rma_submit"}),
        write_scope=frozenset({"artifact"}),
        max_risk="M",
        model_tier=Tier.LIGHT,
        max_self_repair=0,
    )

    def run(self, ctx: TaskContext) -> AgentOutput:
        self.check_risk(ctx.risk_level)
        # 两个只读工具各过一次闸。`supplier.rma_submit` 是补偿那条路上的写工具，
        # 本次调用不碰它，所以这里不过闸 —— 过一遍没用到的闸只会让白名单看起来
        # 比实际用到的更大。
        self.check_tool(TOOL_SUPPLIER_CREDIT_QUERY)
        self.check_tool(TOOL_AP_ADJUST_QUERY)
        self.check_write("artifact")

        res = self.skills.invoke(SKILL_OBSERVE, {
            "tenant_id": ctx.inputs.get("tenant_id"),
            "case_id": ctx.inputs.get("case_id"),
            "max_polls": ctx.inputs.get("max_polls"),
        }, extras=extras_of(self, ctx))
        if res.status != "ok" or not isinstance(res.output, dict):
            return AgentOutput(status="failed", error=failed(res, SKILL_OBSERVE))

        # `rtv.observe` **一次只问一个外部系统**（问哪个由案子当前的业务状态决定，
        # 见 `observe._STAGES`）—— 两个权威终态由 DAG 上两个观察任务分别问，
        # 不在这里循环着替它跳第二跳：那一跳该不该跳是 skill 的判据，
        # 在 Agent 里循环等于把它抄第二遍。
        out = res.output
        # `advanced` 是 skill 说的「这次有没有推进」。没推进就把它挂出来给人看，
        # 但一个字都不改状态 —— 判据仍在 skill 里，这里只是把它翻成一句人话。
        questions = [] if out["advanced"] else [
            f"问了 {out['poll_count']} 次，{out['system']} 侧仍回 "
            f"{out['observed_state']}：{out['message']}；"
            f"业务状态一个字都没动，需人处置 —— 不得据此自行收口"
        ]
        return AgentOutput(
            status="ok",
            open_questions=questions,
            artifacts=[artifact(KIND_RTV_SETTLEMENT_ADVICE, dict(out), summary=(
                f"向 {out['system']} 侧问终态回执（问了 {out['poll_count']} 次）："
                f"对方回 {out['observed_state']}，可对账外部单号 "
                f"{out['reference']!r}；biz_status={out['biz_status']} —— "
                f"两个权威终态各有各的外部来源，一个都不是本地推断"
            ))],
            metrics={"poll_count": out["poll_count"],
                     "system": out["system"],
                     "observed_state": out["observed_state"],
                     "advanced": out["advanced"],
                     "is_rework": ctx.is_rework},
        )
