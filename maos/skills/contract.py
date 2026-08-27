"""Skill 层的基本类型 —— 契约、结果、上下文、基类。

SkillContract 是 skill 的自述：做什么、要什么、给什么、失败了怎么办、边界在哪。
Agent 不读 skill 的实现，只读这份契约来决定要不要调、怎么兜底 ——
所以契约字段缺一不可，尤其 failure_policy 与 security_boundary。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from maos.model.client import ModelClient

FAILURE_POLICIES = ("retry", "fallback", "escalate")


@dataclass
class SkillContract:
    """skill 的冻结自述。名字 + 版本是注册表的主键（见 registry.py）。"""

    name: str
    version: str                                    # semver，如 "1.0.0"
    purpose: str
    input_schema: dict = field(default_factory=dict)
    output_schema: dict = field(default_factory=dict)
    preconditions: list[str] = field(default_factory=list)
    depends_tools: list[str] = field(default_factory=list)
    failure_policy: str = "escalate"                # retry | fallback | escalate
    max_retries: int = 0
    security_boundary: str = ""
    reuse_note: str = ""
    owner_roles: list[str] = field(default_factory=list)


@dataclass
class SkillResult:
    """skill 调用的统一返回。invoker 负责组装，skill 自己只管返回 output。

    invocation_id 是这次调用的唯一标识（invoker 生成 uuid4().hex），同时写进
    event_log 的 SkillInvoked.detail —— 后续 Phase 的权威事实守卫靠它把一条
    产物回溯到究竟是哪个 agent 的哪一次 skill 调用产生的（actor 溯源）。
    调用方拿到的 SkillResult 与落库那行由它对上号，缺了就断链。
    """

    status: str                                     # ok | failed
    output: Any = None
    error: str | None = None
    duration_ms: int = 0
    usage: dict | None = None
    invocation_id: str = ""                         # uuid4().hex，由 invoker 生成


@dataclass
class SkillContext:
    """skill 执行期能看到的全部东西 —— 拿不到全局状态，只有这四样。

    model 不由 invoker 持有，而是从 ``extras["model"]`` 取（Task-A 给 Coding
    接线时传 ``extras={"model": self.model}``）；Task-0 阶段恒为 None。
    """

    model: ModelClient | None = None
    store: Any = None
    identity: Any = None
    extras: dict = field(default_factory=dict)


class Skill(ABC):
    """所有 skill 的基类：一份 contract + 一个 run()。

    与 agents 的 ``@register`` 同构：类上挂 contract，用 ``@register_skill`` 注册。
    run() 只返回**原始 output**（形状见附录 B），包成 SkillResult 是 invoker 的事 ——
    skill 里不要自己造 SkillResult，否则重试和落库口径会各写一套。
    """

    contract: SkillContract

    @abstractmethod
    def run(self, payload: dict, ctx: SkillContext) -> Any: ...
