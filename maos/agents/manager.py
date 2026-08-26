"""Manager Agent —— 把用户请求转成 Plan DAG。

注意它的 Identity：write_scope 只有 plan / task，且 allowed_tools 是空的。
Manager 不直接产出任何业务产物，也不碰 git/ci —— 它只做规划和汇报。
这个边界在 MVP 阶段就要守住，否则后面很容易滑成"Manager 什么都干"。
"""

from __future__ import annotations

import json

from maos.agents.base import AgentIdentity, BaseAgent, TaskContext, AgentOutput
from maos.contracts.events import new_id
from maos.model.client import Tier

SYSTEM = """你是 Manager Agent，负责把用户请求拆成可执行、可验证的任务计划。
只输出 JSON，格式：
{"tasks":[{"role":"coding","title":"...","inputs":{...},"acceptance":["..."],
"depends_on":[],"risk_level":"L|M|H"}]}
每个任务的 acceptance 必须是可机器判定的，不要写"代码质量好"这种。"""


class ManagerAgent(BaseAgent):
    identity = AgentIdentity(
        agent_id="manager",
        role="manager",
        duty="把用户请求转化为可执行、可验证的 Plan DAG，并在执行中维持计划有效性",
        allowed_skills=frozenset({"req.normalize", "kb.retrieve"}),
        allowed_tools=frozenset(),                 # 刻意为空：不给任何业务工具权限
        write_scope=frozenset({"plan", "task"}),
        max_risk="L",
        model_tier=Tier.STRONG,
    )

    def plan(self, goal: str) -> list[dict]:
        raw = self.ask(SYSTEM, f"用户请求：{goal}")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # 规划失败要有确定性兜底，不能让整条链路挂在模型输出上
            return [{
                "task_id": new_id("task"), "role": "coding", "title": goal,
                "inputs": {"goal": goal}, "acceptance": ["产出补丁集且本地自检通过"],
                "depends_on": [], "risk_level": "L",
            }]
        tasks = data.get("tasks", [])
        for t in tasks:
            t.setdefault("task_id", new_id("task"))
            t.setdefault("depends_on", [])
            t.setdefault("risk_level", "L")
        return tasks

    def run(self, ctx: TaskContext) -> AgentOutput:  # Manager 不作为普通 Worker 被调度
        raise NotImplementedError("Manager 由 Control Plane 直接驱动，不走任务队列")
