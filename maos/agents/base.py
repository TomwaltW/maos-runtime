"""Agent 可插拔角色接口 —— 对应方案书附录 A 的 Agent Identity 声明。

Identity 不是文档，是运行时会被强制执行的约束：
Worker Runtime 在调用 Agent 前后都会拿 Identity 校验一遍，
越权调用工具、越权写资源、超出授权风险等级，都会在这里被拦下来。

Track B 加新 Agent 时只做两件事：继承 BaseAgent、在 AGENT_POOL 里注册。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from maos.model.client import ModelClient, Tier


@dataclass(frozen=True)
class AgentIdentity:
    agent_id: str
    role: str
    duty: str
    allowed_skills: frozenset[str] = frozenset()
    allowed_tools: frozenset[str] = frozenset()
    write_scope: frozenset[str] = frozenset()      # 可写资源：artifact / repo_branch / ...
    max_risk: str = "L"
    model_tier: str = Tier.MEDIUM
    max_self_repair: int = 0                       # Agent 内部自修复上限


@dataclass
class TaskContext:
    """Worker 传给 Agent 的执行上下文。Agent 只能看到这些，看不到全局状态。"""
    plan_id: str
    task_id: str
    trace_id: str
    attempt: int
    inputs: dict[str, Any]
    acceptance: list[str]
    risk_level: str
    rework_findings: list[dict] = field(default_factory=list)

    @property
    def is_rework(self) -> bool:
        return self.attempt > 1 and bool(self.rework_findings)


@dataclass
class AgentOutput:
    """Agent 的统一输出契约。所有 Agent 必须返回这个结构。"""
    status: str                                    # ok | failed | blocked
    artifacts: list[dict] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    error: str | None = None
    metrics: dict = field(default_factory=dict)


class PermissionDenied(Exception):
    """Agent 越权。不要 catch，这是安全事件，应当中止并记录。"""


class BaseAgent(ABC):
    identity: AgentIdentity

    def __init__(self, model: ModelClient) -> None:
        self.model = model

    @abstractmethod
    def run(self, ctx: TaskContext) -> AgentOutput: ...

    # ---- Identity 强制执行 ------------------------------------------------
    def check_tool(self, tool: str) -> None:
        if tool not in self.identity.allowed_tools:
            raise PermissionDenied(
                f"{self.identity.agent_id} 无权调用工具 {tool}"
                f"（白名单: {sorted(self.identity.allowed_tools)}）"
            )

    def check_risk(self, risk_level: str) -> None:
        order = {"L": 0, "M": 1, "H": 2}
        if order[risk_level] > order[self.identity.max_risk]:
            raise PermissionDenied(
                f"{self.identity.agent_id} 最高授权 {self.identity.max_risk}，"
                f"不可执行 {risk_level} 级任务"
            )

    def check_write(self, resource: str) -> None:
        if resource not in self.identity.write_scope:
            raise PermissionDenied(f"{self.identity.agent_id} 无权写 {resource}")

    def ask(self, system: str, user: str) -> str:
        return self.model.complete(system=system, user=user, tier=self.identity.model_tier).text


# 可插拔 Agent 池：role -> Agent 类
AGENT_POOL: dict[str, type[BaseAgent]] = {}


def register(cls: type[BaseAgent]) -> type[BaseAgent]:
    AGENT_POOL[cls.identity.role] = cls
    return cls
