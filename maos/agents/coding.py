"""Coding Agent —— 第一步只跑通这一个角色。

Identity 对应方案书附录 A.4。原先写在这里的两条硬约束（不许改测试、不许碰
/infra 与 /.github）已经下沉到 skill ``code.repo-patch`` 的 security_boundary ——
判定只留一处，Agent 这边只负责把它的失败翻译成 AgentOutput。

补丁产出不再直接 self.ask()，一律经 SkillInvoker（A-9）：白名单校验、按 policy
重试、SkillInvoked 审计都在 invoker 里，Agent 绕过去就等于这三样全没了。
invoker 自身不持有 model，所以每次调用都要把它放进 extras（A-3）。
"""

from __future__ import annotations

from maos.agents.base import AgentIdentity, AgentOutput, BaseAgent, TaskContext, register
from maos.model.client import Tier

SKILL_PATCH = "code.repo-patch"
SKILL_KB = "kb.retrieve"

# code.repo-patch 抛的安全异常类名。invoker 把异常压成 "<类名>: <消息>" 字符串，
# 跨模块只能按这个前缀认出安全事件 —— 与 invoker 的 "skill_not_found:<name>"
# 同属字符串协议。改名要两边一起改，test_protected_path_blocked 是把守闸。
SECURITY_ERROR_PREFIX = "ProtectedPathViolation"


def _ensure_builtin_skills() -> None:
    """延迟触发 builtin 动态发现（C-1）。

    刻意不放模块顶部：builtin 包会 import 全部内置 skill，其中任何一个若
    import 了 maos.agents.*，模块级触发就会成环（agents -> builtin -> agents）。
    在 run() 里触发时各模块都已装载完毕，成不了环；import 有缓存，重复调用近乎免费。
    """
    import maos.skills.builtin  # noqa: F401 —— import 即注册


@register
class CodingAgent(BaseAgent):
    identity = AgentIdentity(
        agent_id="coding",
        role="coding",
        duty="按契约生成代码变更，以补丁集形式产出并完成本地自检",
        allowed_skills=frozenset({"code.repo-patch", "kb.retrieve"}),
        allowed_tools=frozenset({"git-mcp", "sandbox"}),
        write_scope=frozenset({"artifact", "repo_branch"}),
        max_risk="M",
        model_tier=Tier.MEDIUM,
        max_self_repair=2,
    )

    def run(self, ctx: TaskContext) -> AgentOutput:
        self.check_risk(ctx.risk_level)
        self.check_tool("git-mcp")
        self.check_write("artifact")
        _ensure_builtin_skills()

        extras = {
            "model": self.model,                     # invoker 不持有 model，从这里取（A-3）
            "tier": self.identity.model_tier,
            "plan_id": ctx.plan_id,
            "task_id": ctx.task_id,
            "trace_id": ctx.trace_id,
            "attempt": ctx.attempt,
        }
        title = str(ctx.inputs.get("title") or ctx.task_id)

        # kb.retrieve 归 Task-D，现在恒未注册 -> invoker 软兜底成
        # failed/skill_not_found，不抛也不阻塞（A-5）。Task-D 合并当天，
        # 这里零改动自动升级为真检索。
        kb = self.skills.invoke(SKILL_KB, {"keyword": title}, extras=extras)
        inputs = dict(ctx.inputs)
        if kb.status == "ok" and kb.output:
            inputs["knowledge"] = kb.output

        res = self.skills.invoke(SKILL_PATCH, {
            "title": title,
            "inputs": inputs,
            "acceptance": ctx.acceptance,
            "rework_findings": ctx.rework_findings,
        }, extras=extras)

        if res.status != "ok":
            error = res.error or f"{SKILL_PATCH} 未产出补丁集"
            if error.startswith(SECURITY_ERROR_PREFIX):
                return AgentOutput(status="failed", error=error,
                                   metrics={"security_event": True})
            return AgentOutput(status="failed", error=error)

        patch = res.output
        return AgentOutput(
            status="ok",
            artifacts=[{"kind": "patch_set", "content": patch}],
            metrics={
                "files_changed": len(patch.get("files", [])),
                "self_check": patch.get("self_check", {}),
                "is_rework": ctx.is_rework,
            },
        )
