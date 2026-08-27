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

from maos.agents.base import AGENT_POOL, AgentOutput, PermissionDenied, TaskContext
from maos.contracts import events as E
from maos.contracts.events import Envelope, Topic
from maos.core.control_plane import ControlPlane
from maos.core.eventbus import EventBus
from maos.model.client import ModelClient

log = logging.getLogger("maos.worker")


class WorkerRuntime:
    def __init__(self, *, worker_id: str, bus: EventBus, control_plane: ControlPlane,
                 model: ModelClient) -> None:
        self.worker_id = worker_id
        self.bus = bus
        self.cp = control_plane
        self.model = model
        self.agents = {role: cls(model, store=self.cp.store) for role, cls in AGENT_POOL.items()}
        bus.subscribe(Topic.TASK_ASSIGNMENT, f"worker-{worker_id}", self.on_assignment)
        log.info("Worker %s 启动，可插拔 Agent: %s", worker_id, sorted(self.agents))

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
