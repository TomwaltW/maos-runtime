"""Control Plane —— 系统里唯一的状态权威。

铁律（这是整个架构成立的前提，实现时不要为了方便破例）：
  1. 任何组件都不直接写 task/plan 表，只能调这里的方法
  2. 任何状态迁移都要过 assert_transition()，非法迁移抛异常而不是静默改写
  3. 每个外部进来的事件都先过幂等闸门，重复投递直接短路返回
  4. 每次迁移都写一条 event_log，这是 Trace 和审计的唯一来源
"""

from __future__ import annotations

import logging

from contracts import events as E
from contracts.events import Envelope, Topic
from contracts.states import (
    NEEDS_HUMAN_APPROVAL,
    PlanState,
    PLAN_TRANSITIONS,
    TaskState,
    assert_transition,
)
from core.eventbus import EventBus
from core.store import Store

log = logging.getLogger("maos.cp")


class ControlPlane:
    def __init__(self, store: Store, bus: EventBus) -> None:
        self.store = store
        self.bus = bus
        bus.subscribe(Topic.TASK_RESULT, "control-plane", self.on_task_result)
        bus.subscribe(Topic.REVIEW_VERDICT, "control-plane", self.on_review_verdict)

    # ------------------------------------------------------------------
    # 内部：唯一的状态迁移出口
    # ------------------------------------------------------------------
    def _transit(self, task: dict, dst: str, *, event_id: str = "", detail: dict | None = None,
                 **fields) -> dict:
        reason = assert_transition(task["state"], dst)
        self.store.update_task(task["task_id"], state=dst, **fields)
        self.store.append_event_log({
            "event_id": event_id,
            "trace_id": task["trace_id"],
            "plan_id": task["plan_id"],
            "task_id": task["task_id"],
            "event_type": "StateTransition",
            "from_state": task["state"],
            "to_state": dst,
            "reason": reason,
            "detail": detail or {},
        })
        log.info("[%s] %s -> %s (%s)", task["task_id"], task["state"], dst, reason)
        return self.store.get_task(task["task_id"])

    def _transit_plan(self, plan_id: str, dst: str) -> None:
        plan = self.store.get_plan(plan_id)
        assert_transition(plan["state"], dst, PLAN_TRANSITIONS)
        self.store.update_plan_state(plan_id, dst)
        self.store.append_event_log({
            "trace_id": plan["trace_id"], "plan_id": plan_id,
            "event_type": "PlanTransition", "from_state": plan["state"], "to_state": dst,
        })

    # ------------------------------------------------------------------
    # 计划与任务创建（Manager Agent 调用）
    # ------------------------------------------------------------------
    def create_plan(self, *, goal: str, trace_id: str, tasks: list[dict]) -> str:
        plan_id = E.new_id("plan")
        self.store.insert_plan({
            "plan_id": plan_id, "trace_id": trace_id, "goal": goal, "state": PlanState.PENDING,
        })
        for t in tasks:
            self.store.insert_task({
                "task_id": t.get("task_id") or E.new_id("task"),
                "plan_id": plan_id,
                "trace_id": trace_id,
                "role": t["role"],
                "title": t["title"],
                "state": TaskState.PENDING,
                "attempt": 0,
                "max_attempts": t.get("max_attempts", 3),
                "risk_level": t.get("risk_level", "L"),
                "effect_risk": t.get("effect_risk", "L"),
                "depends_on": t.get("depends_on", []),
                "inputs": t.get("inputs", {}),
                "acceptance": t.get("acceptance", []),
                "findings": [],
            })
        log.info("创建计划 %s，共 %d 个任务", plan_id, len(tasks))
        return plan_id

    def start_plan(self, plan_id: str) -> None:
        self._transit_plan(plan_id, PlanState.RUNNING)
        self.dispatch_ready(plan_id)

    # ------------------------------------------------------------------
    # 派发：依赖满足的 PENDING 任务 -> DISPATCHED + 发 TaskAssignment
    # ------------------------------------------------------------------
    def dispatch_ready(self, plan_id: str) -> int:
        tasks = self.store.list_tasks(plan_id)
        done = {t["task_id"] for t in tasks if t["state"] == TaskState.DONE}
        n = 0
        for t in tasks:
            if t["state"] != TaskState.PENDING:
                continue
            if not set(t["depends_on"]).issubset(done):
                continue
            attempt = t["attempt"] + 1
            t = self._transit(t, TaskState.DISPATCHED, attempt=attempt)
            self.bus.publish(Topic.TASK_ASSIGNMENT, E.task_assignment(
                plan_id=plan_id, task_id=t["task_id"], role=t["role"], attempt=attempt,
                trace_id=t["trace_id"], inputs=t["inputs"], acceptance=t["acceptance"],
                risk_level=t["risk_level"], rework_findings=t["findings"],
            ))
            n += 1
        return n

    # ------------------------------------------------------------------
    # Worker 认领任务
    # ------------------------------------------------------------------
    def claim(self, task_id: str, worker_id: str, attempt: int) -> dict | None:
        key = f"claim:{task_id}:{attempt}"
        if self.store.claim_idempotency(key, "claim", task_id) is not None:
            log.info("[%s] 重复认领，忽略", task_id)
            return None
        task = self.store.get_task(task_id)
        if task["state"] != TaskState.DISPATCHED:
            log.warning("[%s] 状态是 %s，不可认领", task_id, task["state"])
            return None
        return self._transit(task, TaskState.RUNNING, worker_id=worker_id)

    # ------------------------------------------------------------------
    # 事件回调：TaskResult
    # ------------------------------------------------------------------
    def on_task_result(self, env: Envelope) -> None:
        errs = E.validate(env)
        if errs:
            raise ValueError(f"TaskResult 契约校验失败: {errs}")

        if self.store.claim_idempotency(env.idempotency_key, "result", env.task_id) is not None:
            log.info("[%s] 重复 TaskResult，短路", env.task_id)
            return

        task = self.store.get_task(env.task_id)
        p = env.payload

        if p["status"] == "ok":
            for i, art in enumerate(p["artifacts"]):
                self.store.insert_artifact({
                    "artifact_id": E.new_id("art"), "task_id": task["task_id"],
                    "plan_id": task["plan_id"], "kind": art.get("kind", "generic"),
                    "version": env.attempt, "content": art.get("content", {}),
                })
            self._transit(task, TaskState.AWAITING_REVIEW, event_id=env.event_id,
                          detail={"artifacts": len(p["artifacts"])})

        elif p["status"] == "blocked":
            self._transit(task, TaskState.BLOCKED, event_id=env.event_id,
                          detail={"open_questions": p["open_questions"]},
                          last_error="open_questions 未澄清")

        else:  # failed
            if env.attempt >= task["max_attempts"]:
                self._transit(task, TaskState.FAILED, event_id=env.event_id,
                              last_error=p.get("error"))
                self._fail_plan(task["plan_id"])
            else:
                self._transit(task, TaskState.PENDING, event_id=env.event_id,
                              last_error=p.get("error"))
                self.dispatch_ready(task["plan_id"])

        self.store.finish_idempotency(env.idempotency_key, {"handled": True})

    # ------------------------------------------------------------------
    # 事件回调：ReviewVerdict
    # ------------------------------------------------------------------
    def on_review_verdict(self, env: Envelope) -> None:
        errs = E.validate(env)
        if errs:
            raise ValueError(f"ReviewVerdict 契约校验失败: {errs}")

        if self.store.claim_idempotency(env.idempotency_key, "verdict", env.task_id) is not None:
            log.info("[%s] 重复 ReviewVerdict，短路", env.task_id)
            return

        task = self.store.get_task(env.task_id)
        verdict = env.payload["verdict"]
        detail = {"gate_results": env.payload.get("gate_results", {})}

        if verdict == "pass":
            if task["effect_risk"] in NEEDS_HUMAN_APPROVAL:
                # 产物落地是高风险动作：Gate 过了也不自动放行，转人工审批
                # 注意区分 risk_level（Agent 执行风险）与 effect_risk（产物落地风险）
                self._transit(task, TaskState.BLOCKED, event_id=env.event_id,
                              detail={**detail, "await": "human_approval"})
            else:
                self._transit(task, TaskState.DONE, event_id=env.event_id, detail=detail)
                self._advance(task["plan_id"])

        elif verdict == "rework":
            findings = env.payload.get("findings", [])
            if env.attempt >= task["max_attempts"]:
                self._transit(task, TaskState.FAILED, event_id=env.event_id,
                              last_error="返工次数耗尽")
                self._fail_plan(task["plan_id"])
            else:
                task = self._transit(task, TaskState.REWORK, event_id=env.event_id,
                                     findings=findings, detail=detail)
                self.bus.publish(Topic.REWORK, E.rework(
                    plan_id=task["plan_id"], task_id=task["task_id"],
                    next_attempt=env.attempt + 1, trace_id=task["trace_id"],
                    findings=findings, reason="gate_rework",
                ))
                self._transit(task, TaskState.PENDING)
                self.dispatch_ready(task["plan_id"])

        else:  # block
            self._transit(task, TaskState.BLOCKED, event_id=env.event_id, detail=detail)

        self.store.finish_idempotency(env.idempotency_key, {"verdict": verdict})

    # ------------------------------------------------------------------
    # 人工审批
    # ------------------------------------------------------------------
    def human_decision(self, task_id: str, approved: bool, operator: str, note: str = "") -> None:
        task = self.store.get_task(task_id)
        dst = TaskState.DONE if approved else TaskState.FAILED
        self._transit(task, dst, detail={"operator": operator, "note": note})
        if approved:
            self._advance(task["plan_id"])
        else:
            self._fail_plan(task["plan_id"])

    # ------------------------------------------------------------------
    def _advance(self, plan_id: str) -> None:
        if self.dispatch_ready(plan_id) == 0:
            tasks = self.store.list_tasks(plan_id)
            if all(t["state"] == TaskState.DONE for t in tasks):
                self._transit_plan(plan_id, PlanState.DONE)

    def _fail_plan(self, plan_id: str) -> None:
        self._transit_plan(plan_id, PlanState.FAILED)

    # ------------------------------------------------------------------
    def snapshot(self, plan_id: str) -> dict:
        return {
            "plan": self.store.get_plan(plan_id),
            "tasks": self.store.list_tasks(plan_id),
            "log": self.store.list_event_log(plan_id),
        }
