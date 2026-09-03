"""Risk Agent —— 风险反欺诈岗的薄壳：调一次 `refund.risk_screen`，把结论包成风险报告。

与本域另外几个 Agent 同一个姿态（见 `_base.py` 的模块 docstring）：**业务判定一行都不在
这里**。重复退款怎么算、频率窗口多少天、多少分算 high，全在 skill 的类属性上；
这个文件只做三件事 —— 声明 Identity、搬运入参、把 output 包成 artifact。
判定一旦漏进 Agent，「换域只换 Skill」当场不成立。

两处刻意与 finance 岗不同：

- `max_risk="L"`。风控岗只**读**底账、只出观察，不写任何业务对象、不碰钱，
  授权面按它实际需要的取，不按「隔壁是 M 我也 M」抄。
- artifact kind 常量写在本文件里，**不进** `_base.ALL_REFUND_KINDS`。那份清单是本域
  几个既有 Agent 共享的文件，本轮另一轨也在新增自己的 kind，两轨同改一处必冲突；
  而 Gate 对非代码类产物只按 `summary` / `self_check` 判、不查 kind 白名单，
  所以放在这里是安全的。（已记 `docs/DECISIONS.md`）

本 Agent **不进** `run_payload` 的 DAG：圆桌是旁路观察，处置主路径一字不动。
"""

from __future__ import annotations

from maos.agents.base import AgentIdentity, AgentOutput, BaseAgent, TaskContext, register
from maos.model.client import Tier

from ._base import artifact, extras_of, failed

SKILL_RISK_SCREEN = "refund.risk_screen"

#: 本岗的 artifact kind。刻意不进 `_base.ALL_REFUND_KINDS`，理由见模块 docstring。
KIND_RISK_REPORT = "refund_risk_report"

#: 分档的中文说法，只用于 `summary` 那句话。摆在人面前的是「中风险」不是「medium」。
LEVEL_LABELS = {"low": "低风险", "medium": "中风险", "high": "高风险"}

#: skill 的五个入参键。按名搬运，不在 run() 里逐个写字面量 —— 契约改形状时
#: 这里是唯一要跟的地方。`customer_id` 是可选的第六个，单独处理。
RISK_INPUT_KEYS = ("case_seed", "order", "customer_orders", "refund_history", "requested_at")


@register
class RefundRiskAgent(BaseAgent):
    identity = AgentIdentity(
        agent_id="refund-risk",
        role="refund_risk",
        duty=(
            "按底账筛查重复退款、退款频率与金额异常，给出风险分档与逐条理由；"
            "只提示不裁定 —— 是否放行由规则审核岗与审批人决定，本岗不改状态也不动钱"
        ),
        allowed_skills=frozenset({SKILL_RISK_SCREEN}),
        allowed_tools=frozenset(),          # 只读底账：风控不碰支付网关，也不碰沙箱
        write_scope=frozenset({"artifact"}),
        max_risk="L",
        model_tier=Tier.LIGHT,
        max_self_repair=0,
    )

    def run(self, ctx: TaskContext) -> AgentOutput:
        self.check_risk(ctx.risk_level)
        self.check_write("artifact")

        payload = {key: ctx.inputs.get(key) for key in RISK_INPUT_KEYS}
        # 顶层 customer_id 只在调用方真给了的时候才带上：塞一个 None 进去和不塞
        # 在 skill 那边等价，但 payload 的 input_digest 会不一样，审计对账时多一份噪音。
        customer_id = ctx.inputs.get("customer_id")
        if customer_id:
            payload["customer_id"] = customer_id

        res = self.skills.invoke(SKILL_RISK_SCREEN, payload, extras=extras_of(self, ctx))
        if res.status != "ok" or not isinstance(res.output, dict):
            return AgentOutput(status="failed", error=failed(res, SKILL_RISK_SCREEN))

        out = res.output
        level, score, reasons = out["level"], out["score"], out["reasons"]
        return AgentOutput(
            status="ok",
            artifacts=[artifact(KIND_RISK_REPORT, {
                "level": level,
                "score": score,
                "reasons": reasons,
                "signals": out["signals"],
                "invocation_id": out["invocation_id"],
            }, summary=(
                f"风险筛查完成：{LEVEL_LABELS.get(level, level)}"
                f"（{score} 分，{len(reasons)} 条理由）"
                + (f"：{'；'.join(reasons)}" if reasons else "，未命中任何风险信号")
            ))],
            metrics={"level": level, "score": score},
        )
