"""Claim Settlement Agent —— 核算赔款。

薄壳：先取回本案的裁定（`claim.adjudicate` 的产物），再调 `claim.settle` 算钱。

**为什么裁定从产物里取，而不是让 Agent 重新调一次 adjudicate**：重新调一次会在
`adjudication` 表上再写一行，两次裁定之间条款表要是动过，同一个案子会留下两份
互相矛盾的依据 —— 而「按哪一条判的」正是本域最不能含糊的那个字段。
从上游产物里取，则这一步与裁定那一步引用的是同一次判定。

取不到裁定时**硬失败，不兜底**：没有裁定就核算，等于凭空给一个案子定赔款，
而它每一步都会「成功」。
"""

from __future__ import annotations

from maos.agents.base import AgentIdentity, AgentOutput, BaseAgent, TaskContext, register
from maos.model.client import Tier

from ._base import KIND_ADJUDICATION, KIND_SETTLEMENT, artifact, extras_of, failed

SKILL_SETTLE = "claim.settle"


@register
class ClaimSettlementAgent(BaseAgent):
    identity = AgentIdentity(
        agent_id="claim-settlement",
        role="claim_settlement",
        duty="按裁定命中的条款逐行核算赔款，写 claim_line.amount_allowed 与裁定行上的赔付额",
        allowed_skills=frozenset({"claim.settle"}),
        allowed_tools=frozenset(),          # 核算不碰赔付方
        write_scope=frozenset({"artifact"}),
        max_risk="M",
        model_tier=Tier.LIGHT,
        max_self_repair=0,
    )

    def run(self, ctx: TaskContext) -> AgentOutput:
        self.check_risk(ctx.risk_level)
        self.check_write("artifact")

        adjudication = self._adjudication_of(ctx)
        if adjudication is None:
            return AgentOutput(
                status="failed",
                error=("取不到本案的裁定产物（kind=" + KIND_ADJUDICATION + "）——"
                       "没有裁定就核算等于凭空定赔款，不兜底"))

        res = self.skills.invoke(SKILL_SETTLE, {
            "tenant_id": ctx.inputs.get("tenant_id"),
            "claim_id": ctx.inputs.get("claim_id"),
            "adjudication": adjudication,
        }, extras=extras_of(self, ctx))
        if res.status != "ok" or not isinstance(res.output, dict):
            return AgentOutput(status="failed", error=failed(res, SKILL_SETTLE))

        out = res.output
        bd = out["breakdown"]
        return AgentOutput(
            status="ok",
            artifacts=[artifact(KIND_SETTLEMENT, {
                # 库表与产物同一份数据：下游与断言都读这一个键，谁也不许各造一份。
                "settlement": out["settlement"],
                "allowed_amount": out["allowed_amount"],
                "lines": out["lines"],
                "breakdown": bd,
                "primary_rule": bd["primary_rule"],
                "terms_version": bd["terms_version"],
                "rule_refs": out["rule_refs"],
                "invocation_id": out["invocation_id"],
            }, summary=(
                f"赔款核算 {out['allowed_amount']}：申报 {bd['base']} 扣起付线 "
                f"{bd['deductible']} 后按赔付比例 {bd['coinsurance_rate']} 计得 "
                f"{bd['after_ratio']}，保额 {bd['sum_insured']} 封顶"
                f"{'（已触顶）' if bd['capped_by_sum_insured'] else ''}；"
                f"依据 {bd['primary_rule']}@v{bd['terms_version']}"
            ))],
            metrics={"allowed_amount": out["allowed_amount"],
                     "lines": len(out["lines"]),
                     "capped": bd["capped_by_sum_insured"],
                     "terms_version": bd["terms_version"], "is_rework": ctx.is_rework},
        )

    # ------------------------------------------------------------------
    def _adjudication_of(self, ctx: TaskContext) -> dict | None:
        """从上游的裁定产物里取回 `claim.adjudicate` 的出参。

        走 `self.skills.store`（invoker 持有的那个）而不是另开一条读库路径 ——
        Agent 本来就只有 invoker 这一个通往 store 的口子。
        store 为 None（老写法 `cls(model)`）时返回 None，由调用处硬失败。
        """
        store = getattr(self.skills, "store", None)
        if store is None:
            return None
        best = None
        for task in store.list_tasks(ctx.plan_id):
            for art in store.list_artifacts(task["task_id"]):
                if art["kind"] != KIND_ADJUDICATION:
                    continue
                # 同一个任务多轮返工会留多份，取 version（= attempt）最大的那一份：
                # 核算的依据只能是最后一次裁定，取第一份会去校验一份已经被推翻的裁定。
                if best is None or art["version"] > best["version"]:
                    best = art
        if best is None:
            return None
        return best["content"].get("adjudication")
