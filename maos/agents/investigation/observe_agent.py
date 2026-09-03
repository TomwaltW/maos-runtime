"""Observe Agent —— 向清算方问出决议。**全系统唯一能促成 returned 的角色。**

薄壳：调 `investigation.observe`，把观察包成产物。判定一行都不在这里 ——
「这份回执算不算资金证据」由 `investigation_codes.is_funds_evidence()` 答，
「能不能写 returned」由 `guard` 判。本 Agent 只负责把结论搬到产物上给人看。

## 这个 Agent 报 ok 不等于业务成功

**本域最要紧的一句话，也是本轨要买的第二样东西。**

`investigation.observe` 问到清算方回 `CNCL`（撤销成功）却始终等不到 pacs.004 时，
skill 正常返回、Agent 报 `ok`、产物齐全、`summary` 写得明明白白 ——
但 `funds_returned` 是 `False`，案子一步都没往 `returned` 走。

于是「四个 Agent 全回 ok」和「这笔钱回来了」在数据上是分开的两件事：
前者看 `AgentOutput.status`，后者看 `investigation_case.biz_status` 与
`resolution_observation` 里有没有一行 pacs.004。演示的失败路径正是踩着这条缝走的。

所以本 Agent **不因为没问出资金就报 failed**：轮询到顶仍无 pacs.004 是一个正常的
中间态，判 failed 会触发重试，而重试意味着再走一遍撤销 —— 幂等键挡得住第二份
camt.056，但「以为失败了」这个错误结论会一路传下去。
"""

from __future__ import annotations

from maos.agents.base import AgentIdentity, AgentOutput, BaseAgent, TaskContext, register
from maos.domain.investigation import guard
from maos.model.client import Tier

from ._base import KIND_RESOLUTION, artifact, extras_of, failed

SKILL_OBSERVE = "investigation.observe"

#: 四种观察各一句处置说法。**判据不在这里算** —— 归一由
#: `observe.observed_state_of()` 做一次，本表只把那个结论翻译成给人看的话。
#: 反过来在这里再写一套映射，归一口径改了只有 skill 会跟着变，
#: 这句话会悄悄开始说错，而且没有症状（日志照样正常）。口径同退款域 payment_agent。
_DISPOSITION_PHRASE = {
    guard.OBS_RETURNED: "清算方已发来退款报文（pacs.004），资金确认退回，案子收口",
    guard.OBS_CANCELLATION_CONFIRMED: (
        "清算方回「撤销成功」（camt.029/CNCL），但**这不是资金证据** —— "
        "退款报文（pacs.004）尚未到达，钱有没有回来仍然未知，不许据此收口"),
    guard.OBS_REJECTED: (
        "清算方明确拒绝了本次撤销（camt.029/RJCR），随附拒绝原因码；"
        "终态，按拒绝原因转人工改单或线下协商，不许原样重发"),
    guard.OBS_PENDING: (
        "清算方尚未给出决议，问到上限仍是未决；这不是失败，需继续观察或转人工"),
}


@register
class InvestigationObserveAgent(BaseAgent):
    identity = AgentIdentity(
        agent_id="investigation-observe",
        role="investigation_observe",
        duty="问询清算方取得决议与资金下落；returned 只能由本角色的观察促成",
        allowed_skills=frozenset({SKILL_OBSERVE, "investigation.compensate"}),
        allowed_tools=frozenset({"clearing.resolution"}),
        write_scope=frozenset({"artifact"}),
        max_risk="M",
        model_tier=Tier.LIGHT,
        max_self_repair=0,
    )

    def run(self, ctx: TaskContext) -> AgentOutput:
        self.check_risk(ctx.risk_level)
        self.check_tool("clearing.resolution")
        self.check_write("artifact")

        res = self.skills.invoke(SKILL_OBSERVE, {
            "tenant_id": ctx.inputs.get("tenant_id"),
            "case_id": ctx.inputs.get("case_id"),
            "clearing": ctx.inputs.get("clearing"),
            "request_id": ctx.inputs.get("request_id"),
            "max_polls": ctx.inputs.get("max_polls"),
        }, extras=extras_of(self, ctx))
        if res.status != "ok" or not isinstance(res.output, dict):
            return AgentOutput(status="failed", error=failed(res, SKILL_OBSERVE))
        seen = res.output
        receipt = seen["receipt"]

        return AgentOutput(
            status="ok",
            open_questions=self._open_questions(seen),
            artifacts=[artifact(KIND_RESOLUTION, {
                "case_id": ctx.inputs.get("case_id"),
                "receipt": receipt,
                "observed_state": seen["observed_state"],
                "poll_count": seen["poll_count"],
                # 两个正交的布尔并排放在产物上：读产物的人一眼能看出
                # 「请求有结论了」和「钱回来了」不是一回事。
                "request_resolved": seen["request_resolved"],
                "funds_returned": seen["funds_returned"],
                "needs_compensation": seen["needs_compensation"],
                "biz_status": seen["biz_status"],
                "definition": seen["definition"],
                "source": seen["source"],
                "invocation_id": seen["invocation_id"],
            }, summary=(
                f"问询清算方 {seen['poll_count']} 次，最后观察到 "
                f"{receipt.get('message_type')}（{seen['observed_state']}）；"
                f"资金已退回={seen['funds_returned']}，"
                f"biz_status={seen['biz_status']} —— 该状态来自观察，不是本地推断"
            ))],
            metrics={"poll_count": seen["poll_count"],
                     "funds_returned": seen["funds_returned"],
                     "request_resolved": seen["request_resolved"],
                     "needs_compensation": seen["needs_compensation"],
                     "observed_state": seen["observed_state"],
                     "is_rework": ctx.is_rework},
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _open_questions(seen: dict) -> list[str]:
        """没拿到资金证据就把处置显式挂出来给人看 —— 但一律不改任务状态。

        `funds_returned` 为 True 时无话可说；为 False 时**必须**说话，
        哪怕 `observed_state` 是 `cancellation_confirmed` 这种看起来成功的档 ——
        那一档恰恰是最需要被人看见的：清算方说撤销成功了，钱却没回来。
        """
        if seen["funds_returned"]:
            return []

        head = _DISPOSITION_PHRASE.get(
            seen["observed_state"],
            "观察到一种未归档的决议状态，按未知外部状态处置")
        receipt = seen.get("receipt") or {}
        bits = [f"observed_state={seen['observed_state']}",
                f"已问 {seen['poll_count']} 次"]
        for key in ("confirmation_code", "rejection_code"):
            if receipt.get(key):
                bits.append(f"{key}={receipt[key]}")
        tail = f"；官方定义：{seen['definition']}" if seen.get("definition") else ""
        return [f"{head}（{'，'.join(bits)}）{tail}"]
