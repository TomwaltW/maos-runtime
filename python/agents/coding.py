"""Coding Agent —— 第一步只跑通这一个角色。

Identity 对应方案书附录 A.4。这里刻意保留了两条硬约束：
  · 不允许修改测试用例（防止"改测试让测试通过"）
  · 不允许写受保护路径 /infra、/.github
这两条不是注释，是 run() 里真的会检查并拒绝的。
"""

from __future__ import annotations

import json

from agents.base import AgentIdentity, AgentOutput, BaseAgent, TaskContext, register
from model.client import Tier

PROTECTED_PATHS = ("/infra", "/.github", "tests/", "/secrets")

SYSTEM = """你是 Coding Agent。严格按架构契约产出补丁集。
只输出 JSON，不要任何解释文字，格式：
{"files":[{"path":"...","diff":"..."}],"summary":"...","self_check":{"build":"pass|fail","lint":"pass|fail"}}
禁止修改测试文件。禁止触碰 /infra、/.github、/secrets。"""


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

        prompt = self._build_prompt(ctx)
        raw = self.ask(SYSTEM, prompt)

        try:
            patch = json.loads(raw)
        except json.JSONDecodeError as exc:
            return AgentOutput(status="failed", error=f"模型输出非合法 JSON: {exc}")

        files = patch.get("files", [])
        if not files:
            return AgentOutput(status="failed", error="补丁集为空")

        # 硬约束：受保护路径
        violations = [
            f["path"] for f in files
            if any(f["path"].startswith(p) or p in f["path"] for p in PROTECTED_PATHS)
        ]
        if violations:
            return AgentOutput(
                status="failed",
                error=f"触碰受保护路径，已中止: {violations}",
                metrics={"security_event": True},
            )

        return AgentOutput(
            status="ok",
            artifacts=[{"kind": "patch_set", "content": patch}],
            metrics={
                "files_changed": len(files),
                "self_check": patch.get("self_check", {}),
                "is_rework": ctx.is_rework,
            },
        )

    def _build_prompt(self, ctx: TaskContext) -> str:
        parts = [
            f"任务输入：{json.dumps(ctx.inputs, ensure_ascii=False)}",
            f"验收标准：{json.dumps(ctx.acceptance, ensure_ascii=False)}",
        ]
        if ctx.is_rework:
            # 返工时把结构化 findings 喂回去，而不是让模型重头猜
            parts.append(
                "这是第 %d 次返工，必须逐条解决以下问题：\n%s"
                % (ctx.attempt, json.dumps(ctx.rework_findings, ensure_ascii=False, indent=2))
            )
        return "\n\n".join(parts)
