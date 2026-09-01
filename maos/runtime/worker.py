"""Worker Runtime —— 对应架构图里的 AutoGen Worker Runtime。

职责边界（MVP 阶段就要守住）：
  · 只消费 TaskAssignment，只产出 TaskResult
  · 不直接写数据库，认领任务和交回结果都走 Control Plane / EventBus
  · Agent 抛任何异常都转成 status=failed 的 TaskResult，而不是让消息进死信
    —— 失败要进状态机被记录和重试，不能悄悄消失

换成真 AutoGen 时，只需要把 _invoke 里的直接调用换成 AutoGen 的 agent.run，
上下游契约不变。
"""

from __future__ import annotations

import logging
from typing import Callable

from maos.agents.base import AGENT_POOL, AgentOutput, PermissionDenied, TaskContext
from maos.contracts import events as E
from maos.contracts.events import Envelope, Topic
from maos.core.control_plane import ControlPlane
from maos.core.eventbus import EventBus
from maos.model.client import ModelClient
from maos.model.routed import (
    DEFAULT_ROUTE_SOURCE,
    client_model_name,
    client_provider,
    route_model_client,
)

log = logging.getLogger("maos.worker")

#: 按角色路由留痕的事件类型。**每个被接管的 role 一条，不是每次调用一条** ——
#: 路由归属在一次运行里是不变量，按调用落会把 event_log 冲爆。
EVENT_MODEL_ROUTED = "ModelRouted"


class WorkerRuntime:
    def __init__(self, *, worker_id: str, bus: EventBus, control_plane: ControlPlane,
                 model: ModelClient,
                 model_factory: Callable[[str, str], ModelClient] | None = None) -> None:
        """``model_factory(role, tier) -> ModelClient`` 让每个角色各连各的模型。

        缺省（``None``）时 ``self.agents`` 的构造与加这个参数之前**逐字节相同** ——
        22 个 Agent 仍然共用传进来的那一个 ``model``。这是本改动能安全并入的唯一前提，
        所以缺省分支单独留着那一行原文，而不是让它走一遍新 helper。

        工厂对某个 role 返回 ``None``（或自己抛异常）时**回落到 ``model``**，不中断
        构造 —— 口径同 ``model/client.py::select_model_client`` 的降级：缺配置就降级，
        不崩，但要留声。
        """
        self.worker_id = worker_id
        self.bus = bus
        self.cp = control_plane
        self.model = model
        self.model_factory = model_factory
        if model_factory is None:
            self.agents = {role: cls(model, store=self.cp.store) for role, cls in AGENT_POOL.items()}
        else:
            self.agents = self._build_routed_agents(model_factory)
        bus.subscribe(Topic.TASK_ASSIGNMENT, f"worker-{worker_id}", self.on_assignment)
        log.info("Worker %s 启动，可插拔 Agent: %s", worker_id, sorted(self.agents))

    # -- 按角色注入模型客户端 ------------------------------------------------
    def _build_routed_agents(self, model_factory: Callable[[str, str], ModelClient]) -> dict:
        """逐 role 问工厂要客户端，包上路由归属，并给接管到的 role 各落一条留痕。

        时机不变（C-2）：仍然在 ``__init__`` 里一次铺开 ``self.agents``，
        注册照旧早于 ``build()``，工厂调用没有把这个时刻往后推。
        """
        agents = {}
        for role, cls in AGENT_POOL.items():
            tier = cls.identity.model_tier
            client = self._client_for(role, tier, model_factory)
            if client is None:
                client = self.model
            else:
                client = route_model_client(client, role=role, store=self.cp.store)
                self._record_route(role, tier, client)
            agents[role] = cls(client, store=self.cp.store)
        return agents

    def _client_for(self, role: str, tier: str,
                    model_factory: Callable[[str, str], ModelClient]) -> ModelClient | None:
        try:
            return model_factory(role, tier)
        except Exception as exc:                    # noqa: BLE001 —— 见 __init__ docstring
            log.warning("角色 %s（tier=%s）取模型客户端失败（%s: %s），回落到缺省客户端 —— "
                        "这一跑该角色不走它本该走的模型，与模型相关的结论要按缺省口径读",
                        role, tier, type(exc).__name__, exc)
            return None

    def _record_route(self, role: str, tier: str, client: ModelClient) -> None:
        """落一条 ModelRouted。**detail 里只有名字，一个配置值都没有**（铁律 6）。

        ``base_url`` 不进来 —— 它可能带 query 串里的凭据；key 更不必说。
        走 event_log 而不是给 ``model_usage`` 加列：表结构是冻结面（铁律 1），
        先例是 ``tools/port.py::invoke_tool`` 的 ToolInvoked 行（detail 是自由 JSON）。

        ``plan_id`` 落空串是如实记录：Worker 构造在任何 plan 之前，此刻确实没有归属，
        编一个只会让这条留痕挂到不存在的树上（口径同 ``record_model_usage``）。

        落库失败只 warning：一次已经装配好的 Worker 不该因为审计写失败而起不来。
        """
        store = getattr(self.cp, "store", None)
        if store is None:
            return
        try:
            store.append_event_log({
                "event_id": "", "trace_id": "", "plan_id": "", "task_id": None,
                "event_type": EVENT_MODEL_ROUTED,
                "from_state": "", "to_state": "",
                "reason": f"worker={self.worker_id} 按角色注入模型客户端",
                "detail": {
                    "role": role,
                    "tier": tier,
                    "provider": client_provider(client),
                    "model": client_model_name(client),
                    "route_source": getattr(client, "route_source", "") or DEFAULT_ROUTE_SOURCE,
                    "worker_id": self.worker_id,
                },
            })
        except Exception as exc:                    # noqa: BLE001 —— 见 docstring
            log.warning("角色 %s 的路由留痕落库失败：%s；这次运行的模型归属在证据里查不到",
                        role, exc)

    def on_assignment(self, env: Envelope) -> None:
        errs = E.validate(env)
        if errs:
            raise ValueError(f"TaskAssignment 契约校验失败: {errs}")

        role = env.payload["role"]
        agent = self.agents.get(role)
        if agent is None:
            self._reply(env, AgentOutput(status="failed", error=f"无可用 Agent: role={role}"))
            return

        # 认领：幂等由 Control Plane 兜住，重复投递不会重复执行
        task = self.cp.claim(env.task_id, self.worker_id, env.attempt)
        if task is None:
            return

        ctx = TaskContext(
            plan_id=env.plan_id, task_id=env.task_id, trace_id=env.trace_id,
            attempt=env.attempt, inputs=env.payload["inputs"],
            acceptance=env.payload["acceptance"], risk_level=env.payload["risk_level"],
            rework_findings=env.payload.get("rework_findings", []),
        )
        self._reply(env, self._invoke(agent, ctx))

    def _invoke(self, agent, ctx: TaskContext) -> AgentOutput:
        try:
            return agent.run(ctx)
        except PermissionDenied as exc:
            log.error("安全事件：%s", exc)
            return AgentOutput(status="failed", error=str(exc),
                               metrics={"security_event": True})
        except Exception as exc:  # noqa: BLE001
            log.exception("Agent 执行异常")
            return AgentOutput(status="failed", error=f"{type(exc).__name__}: {exc}")

    def _reply(self, env: Envelope, out: AgentOutput) -> None:
        self.bus.publish(Topic.TASK_RESULT, E.task_result(
            plan_id=env.plan_id, task_id=env.task_id, attempt=env.attempt,
            trace_id=env.trace_id, status=out.status, artifacts=out.artifacts,
            open_questions=out.open_questions, error=out.error,
            worker_id=self.worker_id, metrics=out.metrics,
        ))
