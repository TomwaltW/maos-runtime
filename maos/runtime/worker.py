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
                 model: ModelClient, roles: frozenset[str] | None = None) -> None:
        """``roles`` 是「异构队友」的**全部**实现 —— 不需要新类型。

        ``None``（缺省）= 今天的行为：装全池，什么活都接得住。给了一组 role，
        就只装这几个 role 的 Agent，别的派发一律不承接。改造前全仓只有一种
        Worker，且每个都持全池（原 ``self.agents`` 那行一次装完 ``AGENT_POOL``），
        「队友类型不同」这件事因此在代码里根本不存在。

        池外的 role 当场报错而不是静默丢弃：``roles`` 是一组裸字符串，写错一个
        字母就得到一个**什么活都领不到的 worker**，而它启动、订阅、打日志一切正常。
        那种静默失效要靠「为什么这个队友一直闲着」反查半天；当场炸只需要读一行报错。
        """
        self.worker_id = worker_id
        self.bus = bus
        self.cp = control_plane
        self.model = model
        if roles is None:
            pool = dict(AGENT_POOL)
        else:
            unknown = sorted(set(roles) - set(AGENT_POOL))
            if unknown:
                raise ValueError(
                    f"Worker {worker_id} 要求的 role {unknown} 不在 AGENT_POOL 里"
                    f"（现有 {sorted(AGENT_POOL)}）。新增角色的办法是往 maos/agents/"
                    " 投一个文件并 @register，不是在这里写一个池里没有的名字。"
                )
            pool = {role: cls for role, cls in AGENT_POOL.items() if role in roles}
        self.agents = {role: cls(model, store=self.cp.store) for role, cls in pool.items()}
        #: 本 Worker 承接的 role 集合。任务板（``LeaseBook.claimable``）按它过滤。
        self.roles = frozenset(self.agents)
        bus.subscribe(Topic.TASK_ASSIGNMENT, f"worker-{worker_id}", self.on_assignment)
        log.info("Worker %s 启动，可插拔 Agent: %s", worker_id, sorted(self.agents))

    def on_assignment(self, env: Envelope) -> None:
        errs = E.validate(env)
        if errs:
            raise ValueError(f"TaskAssignment 契约校验失败: {errs}")

        role = env.payload["role"]
        agent = self.agents.get(role)
        if agent is None:
            # 🔴 **静默跳过，不回 failed**（T107）。改造前这里回一条
            # `status=failed` 的 TaskResult，那是一个旁观者在替系统判任务死刑：
            # 控制面收到假失败会把任务推回 PENDING，且 `result:<task_id>:<attempt>`
            # 幂等键被这条假失败先烧掉 —— 真正能干这活的队友稍后交回的结果会被
            # 当成重复投递丢弃。今天不咬人只因为所有 worker 都持全池。
            #
            # 代价说清楚：派了一个**没人**能干的 role，从「立刻失败」变成「没人应答」。
            # 那正是要的 —— 那种任务该由租约超时接管（`reap_expired_leases`），
            # 而不是由一个碰巧收到广播的旁观者当场判死。
            log.debug("Worker %s 不承接 role=%s，跳过 task=%s（本 Worker 的池：%s）",
                      self.worker_id, role, env.task_id, sorted(self.agents))
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

    # ------------------------------------------------------------------
    # 自我认领：从跨 plan 任务板上挑活（T107）
    # ------------------------------------------------------------------
    def pull_and_claim(self, *, now_iso: str, limit: int = 1) -> int:
        """从任务板挑自己干得了的任务，认领成功就执行并交回。返回实际处理条数。

        这是 `on_assignment`（**推**：控制面广播，谁收到谁看着办）之外的第二条
        入口（**拉**：队友主动去板上找活）。两条入口共用同一段执行与交回代码，
        也共用**同一个认领口**。

        🔴 **认领仍然只走 `cp.claim` 一个口，任务板只是只读投影。**
        这条不变量不许破：`claimable()` 返回的是一份**快照**，两个队友可以同时
        拿到同一条任务 —— 互斥发生在 `cp.claim` 里那个 `claim:<task_id>:<attempt>`
        幂等键上，先到的拿到 task，后到的拿到 None 然后跳到下一条。任务板自己
        发认领许可的话，这个互斥就没了，症状是同一个任务被做两遍。

        `now_iso` 必填：它同时用于「哪些租约算过期」这个判定，藏时钟就没法测。
        """
        book = self.cp.leases
        if book is None:
            raise RuntimeError(
                f"Worker {self.worker_id} 要从任务板拉活，但 ControlPlane 没有注入"
                " LeaseBook（ControlPlane(..., leases=LeaseBook(store))）。"
                " 这里不返回 0 —— 那会把「没接线」伪装成「暂时没活」，"
                "而后者是一个正常状态，没人会去查。"
            )

        handled = 0
        for task in book.claimable(self.roles, now_iso):
            if handled >= limit:
                break
            agent = self.agents.get(task["role"])
            if agent is None:            # 防御：claimable 已按 roles 过滤过
                continue
            claimed = self.cp.claim(task["task_id"], self.worker_id, task["attempt"])
            if claimed is None:
                continue                 # 别人抢先，或状态已经不是 DISPATCHED
            self._reply(self._envelope_of(claimed), self._invoke(agent, self._ctx_of(claimed)))
            handled += 1
        return handled

    @staticmethod
    def _ctx_of(task: dict) -> TaskContext:
        return TaskContext(
            plan_id=task["plan_id"], task_id=task["task_id"], trace_id=task["trace_id"],
            attempt=task["attempt"], inputs=task["inputs"],
            acceptance=task["acceptance"], risk_level=task["risk_level"],
            rework_findings=task["findings"],
        )

    @staticmethod
    def _envelope_of(task: dict) -> Envelope:
        """给 `_reply` 拼一个参数载体。**它不发上总线** —— 发了就是重复派发。

        为什么绕这一圈而不直接 `bus.publish(E.task_result(...))`：交回结果这件事
        必须只有一处实现。两处各写一份的话，将来给 `_reply` 加一个字段（metrics、
        签名、房间镜像），拉这条路径会静默漏掉它，而两边都不报错。
        """
        return E.task_assignment(
            plan_id=task["plan_id"], task_id=task["task_id"], role=task["role"],
            attempt=task["attempt"], trace_id=task["trace_id"], inputs=task["inputs"],
            acceptance=task["acceptance"], risk_level=task["risk_level"],
            rework_findings=task["findings"],
        )

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
