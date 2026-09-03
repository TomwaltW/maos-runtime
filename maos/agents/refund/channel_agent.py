"""Channel Agent —— 经销渠道的核销执行位。

**这个角色的存在本身就是对照组 R4 的结论**：它不是为「经销渠道」写死的分支，
而是政策规则 `AS-004` 的 `extra_tasks[].owner_role` 逐字要求的那个执行者。
自营渠道的订单命中不了 `AS-004`（它的 `channel_scope` 是 `ch-dealer`，
`objects.policy_rules_at_order` 按渠道过滤），于是这一步压根不会被规划出来 ——
差异在**规划期**就分开了，不在这里用 `if channel == dealer` 分。

薄壳，比退款域另外四个还薄：**一个 skill 都不调**。核销这件事发生在经销商自己的
系统里，MAOS 既不持有它的权威状态，也没有调它的通路（铁律 8）。所以本 Agent 只做
一件诚实的事：把「这一单按哪条政策规则要求做渠道核销」记成一份可审计的产物，
`pending_external` 恒为真 —— 它是一张待办，不是一次完成回执。

写成「已核销」才是这里最容易犯、也最贵的错：那等于把外部系统的状态在 MAOS 里
写死为终态，与 `payment.observe` 之外的人写 `settled` 是同一类 bug。

放文件即注册（C-2）。`ROLE_CHANNEL` **刻意不进** `refund/__init__.py` 的
`REFUND_ROLES` —— 那个元组的语义是「跑主干退款流程的四个角色」，
`maos/tests/test_refund_flow.py` 按它做等值断言；核销是政策驱动的可选分支，
不是主干的第五步。
"""

from __future__ import annotations

from maos.agents.base import AgentIdentity, AgentOutput, BaseAgent, TaskContext, register
from maos.model.client import Tier

from ._base import artifact

#: 本角色的 role 名。与政策规则 `AS-004` 的 `extra_tasks[].owner_role` **逐字相同** ——
#: 规划期从政策 body 里读出这个串，Worker 按它派单，两处对不上任务就没有执行者。
ROLE_CHANNEL = "refund_channel"

#: 本角色的产物类型。与 `_base.py` 的六个 kind 同一口径：刻意不进
#: `maos/artifacts.py` 的 `ALL_KINDS`（那是跨轨冻结面），也不复用
#: `patch_set` / `test_report`（沾上就会被 Gate 当成代码类任务要测试报告）。
KIND_CHANNEL_WRITEOFF = "refund_channel_writeoff"


@register
class RefundChannelAgent(BaseAgent):
    identity = AgentIdentity(
        agent_id="refund-channel",
        role=ROLE_CHANNEL,
        duty="按政策规则要求登记经销渠道的核销事项，并保留其规则出处",
        # 一个 skill、一个 tool 都不给：核销发生在经销商自己的系统里，
        # MAOS 没有调它的通路，给了白名单等于声称有。
        allowed_skills=frozenset(),
        allowed_tools=frozenset(),
        write_scope=frozenset({"artifact"}),
        max_risk="M",
        model_tier=Tier.LIGHT,      # 零模型，给 LIGHT 只为留一个统一的声明位
        max_self_repair=0,
    )

    def run(self, ctx: TaskContext) -> AgentOutput:
        self.check_risk(ctx.risk_level)
        self.check_write("artifact")

        # 规则出处由规划期从政策 body 里带下来。带不到就说明这一步不是政策推出来的
        # —— 那正是「差异是 if 出来的」那种形态，宁可失败也不许静默放行。
        rule_ref = str(ctx.inputs.get("rule_ref") or "").strip()
        if not rule_ref:
            return AgentOutput(
                status="failed",
                error="渠道核销任务缺 rule_ref：它必须由某条政策规则推出来，"
                      "拿不出出处的核销任务不该被规划出来")

        writeoff = {
            "tenant_id": ctx.inputs.get("tenant_id"),
            "case_id": ctx.inputs.get("case_id"),
            "channel_id": ctx.inputs.get("channel_id"),
            "task_key": ctx.inputs.get("task_key"),
            "rule_ref": rule_ref,
            # 恒真：这是一张待办，不是一次完成回执。核销的权威在经销商的系统里，
            # MAOS 只登记「按 <rule_ref> 该做这件事」。
            "pending_external": True,
        }
        return AgentOutput(
            status="ok",
            artifacts=[artifact(KIND_CHANNEL_WRITEOFF, {
                "channel_writeoff": writeoff,
            }, summary=(
                f"登记渠道核销事项（依据 {rule_ref}）：渠道 {writeoff['channel_id']}，"
                f"案号 {writeoff['case_id']}；核销在经销商系统内完成，"
                f"MAOS 只持有这条待办与它的规则出处，不写它的完成状态"
            ))],
            metrics={"rule_ref": rule_ref, "pending_external": True,
                     "is_rework": ctx.is_rework},
        )
