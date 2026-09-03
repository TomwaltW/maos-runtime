"""证据核验岗 —— 把 `refund.evidence_check` 的结论包成一份产物。

**零业务判定在这里**：verdict 是四态里的哪一态、缺口有几处、哪条判据没满足，
全部由 skill 算。Agent 只做搬运和包装 —— 判定一旦漏进 Agent，「同一个编排内核，
换个领域只换 Skill」这句话就不成立了（见 `_base.py` 模块 docstring）。

这个岗**不裁定、不算钱**：举证不足的正确方向是「免责条款不予适用」，不是拒赔
（`unmet[].direction` 恒 `not_applied`）。裁定归规则审核岗，金额归财务执行岗。
"""

from __future__ import annotations

from maos.agents.base import AgentIdentity, AgentOutput, BaseAgent, TaskContext, register
from maos.model.client import Tier

from ._base import artifact, extras_of, failed

SKILL = "refund.evidence_check"

# 本岗的 artifact kind。**刻意写在本文件而不是 `_base.py`**：`_base.py` 是退款域五个
# Agent 的共享文件，本轨与风险反欺诈岗那轨同时新增角色，两轨同改一处必冲突。
# Gate 对非代码类产物只按 self_check / summary 判，不查 kind 白名单，所以安全。
# 整合轮再决定要不要把它收拢进 ALL_REFUND_KINDS（已记 DECISIONS）。
KIND_EVIDENCE_REPORT = "refund_evidence_report"


@register
class RefundEvidenceAgent(BaseAgent):
    identity = AgentIdentity(
        agent_id="refund-evidence",
        role="refund_evidence",
        duty="只核对随案证据是否满足政策/缺省举证要求并交叉核对物流与质检；不裁定、不算钱",
        allowed_skills=frozenset({SKILL}),
        allowed_tools=frozenset(),          # 只读入参，连附件字节都不取，无工具可调
        write_scope=frozenset({"artifact"}),
        max_risk="L",
        model_tier=Tier.LIGHT,
        max_self_repair=0,
    )

    def run(self, ctx: TaskContext) -> AgentOutput:
        self.check_risk(ctx.risk_level)
        self.check_write("artifact")

        # `case_seed` **刻意不补缺省**：补了就等于把「上游没给案子」变成一次空核验，
        # 而空核验会报 not_required —— 看起来是「不需要举证」，实际是数据没到。
        # 交给 invoker 按 preconditions 判 failed，失败理由才指得到原因。
        res = self.skills.invoke(SKILL, {
            "case_seed": ctx.inputs.get("case_seed"),
            "customer_evidence": ctx.inputs.get("customer_evidence") or [],
            "rules": ctx.inputs.get("rules") or [],
            "order_facts": ctx.inputs.get("order_facts") or {},
            "requested_at": ctx.inputs.get("requested_at") or "",
        }, extras=extras_of(self, ctx))
        if res.status != "ok" or not isinstance(res.output, dict):
            return AgentOutput(status="failed", error=failed(res, SKILL))

        out = res.output
        count = sum(1 for it in out["items"] if it["ok"])
        required = "、".join(out["required_kinds"]) or "不限类型"
        return AgentOutput(
            status="ok",
            artifacts=[artifact(KIND_EVIDENCE_REPORT, {
                "verdict": out["verdict"],
                "requirement_source": out["requirement_source"],
                "required_kinds": out["required_kinds"],
                "min_count": out["min_count"],
                "gaps": out["gaps"],
                "unmet": out["unmet"],
                "items": out["items"],
                "consistency": out["consistency"],
                "invocation_id": out["invocation_id"],
            }, summary=(
                f"证据核验：{out['verdict']}（要求 {required} ≥ {out['min_count']} 份，"
                f"实收 {count} 份，{len(out['gaps'])} 处缺口）"
            ))],
            metrics={"verdict": out["verdict"],
                     "evidence_count": count,
                     "gaps": len(out["gaps"])},
        )
