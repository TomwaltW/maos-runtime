"""Requirement Agent —— 把用户目标归一成「可执行 + 可验收」的需求。

照 coding.py 的模式：``@register`` + Identity + 经 ``SkillInvoker`` 调 skill。
放文件即注册（C-2 pkgutil 自动发现），**不要**去改 ``maos/agents/__init__.py``。

这个角色唯一的判断权在 open_questions：说不清的地方必须**显式挂出来**，
而不是自己替人拍板。挂出来就 ``status="blocked"`` —— 走状态机既有的
``worker_blocked``（RUNNING -> BLOCKED）路径，不新增任何状态。

为什么「猜」比「停」危险：需求含糊时替用户拍板，错误会一路穿透到架构契约、
补丁、测试报告，最后在验收现场才暴露，且已经没人记得当初是哪一步猜的。
停在 BLOCKED 是可见的、可追溯的、成本最低的失败。
"""

from __future__ import annotations

from maos.agents.base import AgentIdentity, AgentOutput, BaseAgent, TaskContext, register
from maos.model.client import Tier

SKILL_NORMALIZE = "req.normalize"

# 本轨新增的 artifact kind。**刻意不进 maos/artifacts.py 的 ALL_KINDS** ——
# 那份清单是跨轨冻结口径（A-7），单轨往里加会和 D/E 撞；而 Gate 对非代码类
# 产物只按 self_check 判，不查 kind 白名单，所以这里用字面量是安全的。
# 缺口已记 BACKLOG：requirement 产物的形状校验尚无 checker。
KIND_REQUIREMENT = "requirement"


@register
class RequirementAgent(BaseAgent):
    identity = AgentIdentity(
        agent_id="requirement",
        role="requirement",
        duty="把用户目标归一成可执行、可验收的需求；说不清的地方挂成 open_questions 而不是替人拍板",
        allowed_skills=frozenset({"req.normalize"}),
        allowed_tools=frozenset(),          # 需求澄清不碰任何业务工具
        write_scope=frozenset({"artifact"}),
        max_risk="L",
        model_tier=Tier.STRONG,
        max_self_repair=1,
    )

    def run(self, ctx: TaskContext) -> AgentOutput:
        self.check_risk(ctx.risk_level)
        self.check_write("artifact")

        extras = {
            "model": self.model,                 # invoker 不持有 model，从这里取（A-3）
            "tier": self.identity.model_tier,
            "plan_id": ctx.plan_id,
            "task_id": ctx.task_id,
            "trace_id": ctx.trace_id,
            "attempt": ctx.attempt,
        }
        goal = str(ctx.inputs.get("goal") or ctx.inputs.get("title") or "").strip()
        context = {k: v for k, v in ctx.inputs.items() if k not in ("goal", "open_questions")}

        res = self.skills.invoke(
            SKILL_NORMALIZE, {"goal": goal, "context": context}, extras=extras)
        if res.status != "ok" or not isinstance(res.output, dict):
            # req.normalize 是 Task-A 已落地的真 skill，失败就是真失败（模型输出不合契约），
            # 不做「自己编一份需求」的降级 —— 那会把模型坏掉伪装成需求本来就长这样。
            return AgentOutput(status="failed",
                               error=res.error or f"{SKILL_NORMALIZE} 未产出归一结果")

        norm = res.output
        acceptance = self._merge_acceptance(ctx.acceptance, norm.get("acceptance_suggestions"))
        questions = self._open_questions(goal, acceptance, ctx)
        if questions:
            return AgentOutput(status="blocked", open_questions=questions,
                               metrics={"open_questions": len(questions)})

        return AgentOutput(
            status="ok",
            artifacts=[{"kind": KIND_REQUIREMENT, "content": {
                "normalized_goal": norm.get("normalized_goal", goal),
                "constraints": list(norm.get("constraints") or []),
                "acceptance": acceptance,
                "summary": f"需求归一完成，产出 {len(acceptance)} 条可机器判定的验收标准",
                # 非代码类产物的验收证据仍是 self_check（Gate 对这一类保留原口径）。
                "self_check": {"build": "pass", "lint": "pass"},
            }}],
            metrics={"acceptance": len(acceptance), "is_rework": ctx.is_rework},
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _merge_acceptance(existing: list[str], suggestions) -> list[str]:
        """任务自带的验收在前、归一建议在后，去重但保序。

        不覆盖任务自带的 acceptance：那是派单人写死的判据，
        模型的建议只能补充，不能顶替。
        """
        out = [str(a).strip() for a in (existing or []) if str(a).strip()]
        for s in suggestions or []:
            text = str(s).strip()
            if text and text not in out:
                out.append(text)
        return out

    @staticmethod
    def _open_questions(goal: str, acceptance: list[str], ctx: TaskContext) -> list[str]:
        """三个来源合一，去重保序。任一非空即整个任务 blocked。"""
        questions: list[str] = []
        for q in ctx.inputs.get("open_questions") or []:
            text = str(q).strip()
            if text and text not in questions:
                questions.append(text)
        if not goal:
            questions.append("任务没有给出目标（inputs.goal 为空），无法归一成可验收需求")
        if not acceptance:
            questions.append("归一后一条可机器判定的验收标准都没有，无法据此判定完成")
        return questions
