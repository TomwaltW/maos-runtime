"""Agent 可插拔角色接口 —— 对应方案书附录 A 的 Agent Identity 声明。

Identity 不是文档，是运行时会被强制执行的约束：
Worker Runtime 在调用 Agent 前后都会拿 Identity 校验一遍，
越权调用工具、越权写资源、超出授权风险等级，都会在这里被拦下来。

Track B 加新 Agent 时只做两件事：继承 BaseAgent、在 AGENT_POOL 里注册。
"""

from __future__ import annotations

import contextvars
import functools
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from maos.core.store import record_model_usage
from maos.model.client import ModelClient, Tier
from maos.skills.invoker import SkillInvoker

#: ``ask()`` 落 ``model_usage`` 时写进 ``call_site`` 列的值。
CALL_SITE_ASK = "maos/agents/base.py::BaseAgent.ask"

#: ``run()`` 执行期间的任务归属（trace_id / plan_id / task_id）。
#:
#: 为什么要这么一个东西：``ask()`` 的签名是 ``(system, user)``，里面看得到
#: ``self.identity`` 却看不到 ``TaskContext``，而 ``trace_id`` 只在后者上。两个调用方
#: （``agents/reviewer.py::ReviewerAgent.run`` 与 ``agents/manager.py::ManagerAgent.plan``）
#: 都不在本轨可改范围内，给 ``ask()`` 加参数也就没人传得进来 —— 所以归属由
#: ``BaseAgent`` 自己在 ``run()`` 进出时绑定，签名一个字节不动。
#:
#: 用 ContextVar 而不是实例属性：``WorkerRuntime`` 每个 role 只建一个 Agent 实例并反复
#: 复用（``runtime/worker.py``），实例属性会在并发/嵌套调用间串味，把 A 任务的成本
#: 记到 B 任务头上。ContextVar 天然按执行上下文隔离，且 ``reset`` 保证退出即还原。
#:
#: 绑不上的照旧是空 —— 见 ``core/store.py::record_model_usage`` 里那段「不许编 trace_id」。
_ATTRIBUTION: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "maos_agent_attribution", default={})


def _bind_attribution(fn):
    """把 ``run(ctx)`` 包一层：进函数绑归属，出函数还原。只读 ctx，不改它。"""
    @functools.wraps(fn)
    def _run(self, ctx, *args, **kwargs):
        token = _ATTRIBUTION.set({
            "trace_id": getattr(ctx, "trace_id", "") or "",
            "plan_id": getattr(ctx, "plan_id", "") or "",
            "task_id": getattr(ctx, "task_id", None),
        })
        try:
            return fn(self, ctx, *args, **kwargs)
        finally:
            _ATTRIBUTION.reset(token)
    return _run


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

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """给每个自己实现了 ``run()`` 的子类装上归属绑定（见 ``_ATTRIBUTION``）。

        装在这里而不是 ``@register`` 上：``ReviewerAgent`` 走的是
        ``reviewer.py::review_after_gate`` 直接 ``reviewer.run(ctx)``，**不经 Worker 队列**，
        而 ``@register`` 只决定进不进 ``AGENT_POOL``，管不到谁来调。绑定必须跟着
        ``run()`` 本身走，才不会漏掉这条旁路。

        抽象声明不包（包了会把 ``__isabstractmethod__`` 抹掉，让没实现 run 的类
        也能被实例化）；中间基类与叶子类各自实现 run 时两层都包，嵌套 set/reset
        是安全的，内层退出即还原成外层。
        """
        super().__init_subclass__(**kwargs)
        fn = cls.__dict__.get("run")
        if callable(fn) and not getattr(fn, "__isabstractmethod__", False):
            cls.run = _bind_attribution(fn)

    def __init__(self, model: ModelClient, store: Any = None) -> None:
        self.model = model
        # skill 调用的唯一入口：白名单校验与 SkillInvoked 审计都在 invoker 里。
        # store 缺省 None（此时只跳过落库），保证 cls(model) 的老写法不改仍可用。
        self.skills = SkillInvoker(self.identity, store)

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
        """调模型并把用量挂到当前 ``trace_id`` 上，然后照旧只返回文本。

        接住整个 ``ModelResponse`` 而不是直接 ``.text``：``tokens_in`` / ``tokens_out``
        原先在这一行被丢掉，全仓因此没有一处成本口径。返回值形状不变，调用方零改动。

        抛异常的调用**不落行**：网关没给回用量，编一行 0 token 只会让「调用失败」
        伪装成「这次很便宜」。失败本身已经由上层的 AgentOutput / 事件链记着。
        """
        started = time.perf_counter()
        resp = self.model.complete(system=system, user=user, tier=self.identity.model_tier)
        attribution = _ATTRIBUTION.get()
        record_model_usage(
            getattr(self.skills, "store", None), resp,
            client=self.model, agent_role=self.identity.role,
            call_site=CALL_SITE_ASK, tier=self.identity.model_tier,
            latency_ms=int((time.perf_counter() - started) * 1000),
            trace_id=attribution.get("trace_id", ""),
            plan_id=attribution.get("plan_id", ""),
            task_id=attribution.get("task_id"),
        )
        return resp.text


# 可插拔 Agent 池：role -> Agent 类
AGENT_POOL: dict[str, type[BaseAgent]] = {}


def register(cls: type[BaseAgent]) -> type[BaseAgent]:
    AGENT_POOL[cls.identity.role] = cls
    return cls
