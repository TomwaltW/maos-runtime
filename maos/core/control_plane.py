"""Control Plane —— 系统里唯一的状态权威。

铁律（这是整个架构成立的前提，实现时不要为了方便破例）：
  1. 任何组件都不直接写 task/plan 表，只能调这里的方法
  2. 任何状态迁移都要过 assert_transition()，非法迁移抛异常而不是静默改写
  3. 每个外部进来的事件都先过幂等闸门，重复投递直接短路返回
  4. 每次迁移都写一条 event_log，这是 Trace 和审计的唯一来源
"""

from __future__ import annotations

import logging
import os
from typing import Callable

from maos.artifacts import (
    KIND_COMPENSATION,
    KIND_PATCH_SET,
    MODE_REVERSE,
    resolve_patch_ref,
    validate_artifact,
)
from maos.contracts import events as E
from maos.contracts.events import Envelope, Topic
from maos.contracts.states import (
    NEEDS_HUMAN_APPROVAL,
    PlanState,
    PLAN_TRANSITIONS,
    TERMINAL_STATES,
    TaskState,
    assert_transition,
)
from maos.core.eventbus import EventBus
from maos.core.store import Store
from maos.tools.sandbox import sandbox_git_apply

log = logging.getLogger("maos.cp")

# -- Replan 治理参数 -----------------------------------------------------
ENV_MAX_REPLAN = "MAOS_MAX_REPLAN"
DEFAULT_MAX_REPLAN = 2
# 单轮 findings 里 blocker 达到这个数，说明方案本身有问题，返工同一份规格是浪费
REPLAN_BLOCKER_THRESHOLD = 2

# 被重规划取代、不再派发的任务，用 last_error 打标。
# 借 last_error 而不是加状态：states.py 是冻结契约（铁律 1），加「冻结态」要动
# 迁移表；而 last_error 本就是「这个任务为什么没往前走」的说明字段，语义相容。
FROZEN_BY_REPLAN = "frozen_by_replan"

# -- 补偿 ----------------------------------------------------------------
ENV_SANDBOX_WORKDIR = "MAOS_SANDBOX_WORKDIR"

# compensation artifact 的 version 恒为 0，**不跟 attempt 走**。
# 它是引用不是产物：指向哪一次 attempt 的信息已经在 patch_ref.attempt 里了，
# 再给它一个产物版本号是重复且会误导的。落到行为上更要紧 ——
# ReviewerGate 按 `version == task["attempt"]` 取「本轮待评审的产物」
# （gate.py:42-43），compensation 不是本轮产物，不该被四道产物闸评判；
# version=0 让这件事自动成立，不需要去动 Task-C 的 gate.py。
# ⚠️ 合并期核对项：C 轨的 _gate_compensation 必须在**全量** list_artifacts(task_id)
# 里按 kind 找 compensation，不能在按 version 过滤后的列表里找，否则找不到。
COMPENSATION_VERSION = 0

# 重规划回调签名：控制面只认这个形状，不认 Manager 这个类。
Replanner = Callable[..., list[dict]]


def _is_frozen(task: dict) -> bool:
    """这个任务是否已被重规划取代。判定只留这一处，三个调用点共用。"""
    return task.get("last_error") == FROZEN_BY_REPLAN


class ControlPlane:
    def __init__(self, store: Store, bus: EventBus, *,
                 replanner: Replanner | None = None) -> None:
        self.store = store
        self.bus = bus
        # 重规划要调 Manager，也就是要调模型。控制面不持有模型、不 import Agent：
        # 注入一个回调，由场景层决定「重规划」具体怎么做（scenario_5 注入的是
        # ScriptedModelClient 驱动的 ManagerAgent，因此结果确定性可复现）。
        # 未注入时 replan 判定照常算，但不执行 —— 退化成普通返工，行为与接线前一致。
        self._replanner = replanner
        bus.subscribe(Topic.TASK_RESULT, "control-plane", self.on_task_result)
        bus.subscribe(Topic.REVIEW_VERDICT, "control-plane", self.on_review_verdict)

    def set_replanner(self, replanner: Replanner | None) -> None:
        """事后注入重规划回调 —— ``build()``（C-3 冻结签名）不传它，只能这样接线。

        回调签名：``(*, goal: str, findings: list[dict], open_tasks: list[dict])
        -> list[dict]``，返回值是任务规格列表，形状同 ``create_plan`` 的 tasks。
        """
        self._replanner = replanner

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
            if _is_frozen(t):
                continue                      # 被重规划取代，不再派发
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
            for art in p["artifacts"]:
                kind = art.get("kind", "generic")
                self.store.insert_artifact({
                    "artifact_id": E.new_id("art"), "task_id": task["task_id"],
                    "plan_id": task["plan_id"], "kind": kind,
                    "version": env.attempt, "content": art.get("content", {}),
                })
                if kind == KIND_PATCH_SET and task["effect_risk"] in NEEDS_HUMAN_APPROVAL:
                    self._attach_compensation(task, env.attempt)
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
    # 补偿引用自动附着（A-13：对手册的偏离，已在 docs/DECISIONS.md 备案）
    # ------------------------------------------------------------------
    def _attach_compensation(self, task: dict, attempt: int) -> dict:
        """给 effect_risk=H 任务本轮的 patch_set 附一条补偿引用，零模型调用。

        为什么在控制面做而不是 Agent 侧（phase-4.md:18 原文写的是 Coding Agent）：
        ``TaskAssignment`` payload **没有 effect_risk 字段**，而 events.py 是冻结
        契约（铁律 1），Agent 根本拿不到这个信息，判不了该不该附。补偿本就属控制面
        行为，挪到这里机制等价 —— 产出 patch_set 的那一刻附着，晚一步都不行：
        Gate 的第五道闸（C 轨）要在评审时就看到它。

        artifact 自身**不含 diff**：它只是指针，正向补丁内容永远只存一份在被引用的
        patch_set 里。这是「零模型补偿」的落点 —— 逆补丁不由模型生成，只做反向应用。
        """
        ref = {"task_id": task["task_id"], "kind": KIND_PATCH_SET, "attempt": attempt}
        content = {"mode": MODE_REVERSE, "patch_ref": ref}

        # 自校验：形状漂了当场炸，而不是等到 reject 那一刻补偿静默不执行。
        errs = validate_artifact(KIND_COMPENSATION, content)
        if errs:
            raise ValueError(f"控制面生成的 compensation 不合形状: {errs}")

        self.store.insert_artifact({
            "artifact_id": E.new_id("art"), "task_id": task["task_id"],
            "plan_id": task["plan_id"], "kind": KIND_COMPENSATION,
            "version": COMPENSATION_VERSION, "content": content,
        })
        self.store.append_event_log({
            "trace_id": task["trace_id"], "plan_id": task["plan_id"],
            "task_id": task["task_id"], "event_type": "CompensationAttached",
            "detail": {"patch_ref": ref, "mode": MODE_REVERSE},
        })
        log.info("[%s] 已附着补偿引用 -> attempt=%d", task["task_id"], attempt)
        return content

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
            elif self._replanner is not None and self._should_replan(task, findings):
                if self._replan_used(task["plan_id"]) >= self._max_replan():
                    # 上限到了就停，转人工 —— **绝不自旋**。无限重试是评委点名的反模式，
                    # 而「再规划一次说不定就好了」正是自旋最常见的伪装。
                    # 迁移走既有的 AWAITING_REVIEW->BLOCKED("gate_needs_human")：
                    # 此刻任务还在 AWAITING_REVIEW，先返工再转人工是走不通的
                    # （PENDING->BLOCKED 不在迁移表里），顺序不能倒。
                    log.warning("[%s] 重规划已达上限 %d，转人工处置",
                                task["plan_id"], self._max_replan())
                    self._transit(task, TaskState.BLOCKED, event_id=env.event_id,
                                  findings=findings,
                                  detail={**detail, "await": "human_decision",
                                          "reason": "replan_limit_exceeded",
                                          "replan_used": self._replan_used(task["plan_id"])})
                else:
                    self._replan(task, findings, env, detail)
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
    # Replan：判定与执行分开 —— 判定是纯函数，可以脱开重规划回调单独验边界
    # ------------------------------------------------------------------
    def _should_replan(self, task: dict, findings: list[dict]) -> bool:
        """两条触发线，任一命中即重规划（phase-4.md 第 4 步）。

        · 单轮 findings 中 blocker >= 2：一轮里堵住两处，问题多半在方案本身，
          拿同一份规格再返工一次是浪费一个 attempt。
        · 同一任务第 2 次 rework：第一次返工没解决，说明规格没描述清楚。

        「第几次 rework」从 event_log 数，不另存计数器：event_log 是 Trace 与审计的
        唯一来源（本文件铁律 4），再维护一个内存计数器就有了第二份事实，进程重启即失真。
        判定发生在本次 REWORK 落库**之前**，所以历史里有 1 条就意味着这将是第 2 次。
        """
        blockers = sum(1 for f in findings
                       if isinstance(f, dict) and f.get("severity") == "blocker")
        if blockers >= REPLAN_BLOCKER_THRESHOLD:
            log.info("[%s] 单轮 blocker=%d，触发重规划", task["task_id"], blockers)
            return True
        prior = sum(1 for e in self.store.list_event_log(task["plan_id"])
                    if e.get("task_id") == task["task_id"]
                    and e.get("to_state") == TaskState.REWORK)
        if prior >= 1:
            log.info("[%s] 第 %d 次返工，触发重规划", task["task_id"], prior + 1)
            return True
        return False

    def _max_replan(self) -> int:
        """MAOS_MAX_REPLAN，默认 2。非法值回退默认并告警，不让配置笔误变成自旋。"""
        raw = (os.environ.get(ENV_MAX_REPLAN) or "").strip()
        if not raw:
            return DEFAULT_MAX_REPLAN
        try:
            value = int(raw)
        except ValueError:
            log.warning("%s=%r 不是整数，回退默认 %d", ENV_MAX_REPLAN, raw, DEFAULT_MAX_REPLAN)
            return DEFAULT_MAX_REPLAN
        return max(value, 0)

    def _replan_used(self, plan_id: str) -> int:
        """已发生过几次重规划 = event_log 里 RUNNING->PENDING 的 PlanTransition 条数。"""
        return sum(1 for e in self.store.list_event_log(plan_id)
                   if e.get("event_type") == "PlanTransition"
                   and e.get("from_state") == PlanState.RUNNING
                   and e.get("to_state") == PlanState.PENDING)

    def _replan(self, task: dict, findings: list[dict], env: Envelope, detail: dict) -> None:
        """Plan RUNNING->PENDING("replan") -> 重规划剩余工作 -> start_plan 重启。

        states.py 一个新状态、一个新迁移都不加（铁律 1 / 铁律 9）：``replan`` 这条
        Plan 迁移 states.py:57 早已存在，本方法只是第一个用它的地方。
        """
        plan_id = task["plan_id"]

        # 1. 当前任务先走完既有返工路径 —— findings 要落库，下一轮才喂得回去
        task = self._transit(task, TaskState.REWORK, event_id=env.event_id,
                             findings=findings, detail=detail)
        self.bus.publish(Topic.REWORK, E.rework(
            plan_id=plan_id, task_id=task["task_id"], next_attempt=env.attempt + 1,
            trace_id=task["trace_id"], findings=findings, reason="replan",
        ))
        self._transit(task, TaskState.PENDING)

        # 2. Plan 退回 PENDING，此刻起不派发任何东西（start_plan 之前无人调 dispatch）
        self._transit_plan(plan_id, PlanState.PENDING)

        # 3. 带**全部**任务的 findings 重规划，不只带当前这一条：
        #    重规划要看的是整个计划为什么走不通，只喂一条就退化成了返工。
        plan = self.store.get_plan(plan_id)
        open_tasks = [t for t in self.store.list_tasks(plan_id)
                      if t["state"] not in TERMINAL_STATES and not _is_frozen(t)]
        all_findings = [f for t in self.store.list_tasks(plan_id) for f in t["findings"]]
        specs = self._replanner(goal=plan["goal"], findings=all_findings,
                                open_tasks=open_tasks) or []
        self._apply_replan(plan_id, open_tasks, specs)

        self.store.append_event_log({
            "trace_id": task["trace_id"], "plan_id": plan_id, "task_id": task["task_id"],
            "event_type": "Replanned",
            "detail": {"findings": len(all_findings), "open_tasks": len(open_tasks),
                       "new_specs": len(specs), "used": self._replan_used(plan_id)},
        })

        # 4. 重启：PENDING -> RUNNING 并派发
        self.start_plan(plan_id)

    def _apply_replan(self, plan_id: str, open_tasks: list[dict], specs: list[dict]) -> None:
        """新规格接管未完成的任务；接管不下的旧任务冻结，多出来的新规格建新任务。

        逐位覆写而不是「旧的全冻结 + 新的全新建」：覆写让 task_id、attempt 与
        event_log 的因果链连续，一条任务的完整经历（含重规划前那次失败）仍串在
        同一个 task_id 上；全新建会把轨迹断成两截，Trace 上看不出前后是同一件事。

        **findings 保留不清**：新规格加上「上一版为什么不行」一起喂给下一轮，
        比只给新规格更有信息量（dispatch_ready 会把它作为 rework_findings 发出去）。
        """
        for task, spec in zip(open_tasks, specs):
            self.store.update_task(
                task["task_id"],
                title=spec.get("title", task["title"]),
                inputs=spec.get("inputs", {}),
                acceptance=spec.get("acceptance", []),
                depends_on=spec.get("depends_on", []),
                risk_level=spec.get("risk_level", task["risk_level"]),
                effect_risk=spec.get("effect_risk", task["effect_risk"]),
                last_error=None,
            )
        for spec in specs[len(open_tasks):]:
            self.store.insert_task({
                "task_id": spec.get("task_id") or E.new_id("task"),
                "plan_id": plan_id,
                "trace_id": open_tasks[0]["trace_id"] if open_tasks else E.new_id("trace"),
                "role": spec["role"], "title": spec["title"],
                "state": TaskState.PENDING, "attempt": 0,
                "max_attempts": spec.get("max_attempts", 3),
                "risk_level": spec.get("risk_level", "L"),
                "effect_risk": spec.get("effect_risk", "L"),
                "depends_on": spec.get("depends_on", []),
                "inputs": spec.get("inputs", {}),
                "acceptance": spec.get("acceptance", []),
                "findings": [],
            })
        for task in open_tasks[len(specs):]:
            # 新方案没给它安排活 —— 冻结，既不派发也不计入 Plan 完成判定
            self.store.update_task(task["task_id"], last_error=FROZEN_BY_REPLAN)
            log.info("[%s] 被重规划取代，冻结", task["task_id"])

    # ------------------------------------------------------------------
    # 人工审批
    # ------------------------------------------------------------------
    def human_decision(self, task_id: str, approved: bool, operator: str, note: str = "") -> None:
        task = self.store.get_task(task_id)
        if not approved:
            # 先回滚再改状态（phase-4.md:20 的顺序）：状态一旦落 FAILED，
            # 「这个任务的产物还在外面」这件事就没人记得了。
            self._execute_compensation(task, operator=operator, note=note)
        dst = TaskState.DONE if approved else TaskState.FAILED
        self._transit(task, dst, detail={"operator": operator, "note": note})
        if approved:
            self._advance(task["plan_id"])
        else:
            self._fail_plan(task["plan_id"])

    # ------------------------------------------------------------------
    # 补偿执行器：零模型 —— 逆补丁不生成，只把正向补丁反着打一遍
    # ------------------------------------------------------------------
    def _execute_compensation(self, task: dict, *, operator: str, note: str = "") -> dict | None:
        """读补偿引用 -> 取回正向补丁 -> 沙箱反向应用 -> 落 CompensationExecuted。

        返回 None 表示这个任务压根没有补偿引用（低风险产物，没有要还原的东西）；
        否则返回 sandbox_git_apply 的结果。

        **缺 patch_ref 一律硬失败**（C-5 反例原文）：这里绝不写
        ``content.get("patch_ref", {})``。兜底的后果不是报错，是补偿**静默不执行** ——
        reject 之后文件没还原，而日志一片正常，直到演示现场才发现。
        解析统一走 ``artifacts.resolve_patch_ref``，本文件不自写一行 ref 解析。
        """
        comps = [a for a in self.store.list_artifacts(task["task_id"])
                 if a["kind"] == KIND_COMPENSATION]
        if not comps:
            log.info("[%s] 无补偿引用，跳过回滚", task["task_id"])
            return None

        content = comps[-1]["content"]              # 多轮 attempt 取最后附着的那条
        errs = validate_artifact(KIND_COMPENSATION, content)
        if errs:
            raise ValueError(
                f"[{task['task_id']}] 补偿引用不合形状，拒绝执行（补偿必须硬失败，"
                f"不许兜底成静默不回滚）: {errs}")

        ref = content["patch_ref"]
        patch_art = resolve_patch_ref(self.store, ref)
        if patch_art is None:
            raise ValueError(
                f"[{task['task_id']}] 补偿引用 {ref} 取不回正向补丁集 —— "
                f"引用在而被引用物不在，数据已不一致，拒绝静默跳过")

        workdir = os.environ.get(ENV_SANDBOX_WORKDIR) or "."
        try:
            result = sandbox_git_apply(patch_art["content"], workdir, reverse=True)
        except NotImplementedError:
            # 并行开发期的预期路径：沙箱实现归 Task-B，合并前这里恒抛（C-7 分段验收）。
            # 记事件不吞事实 —— 「补偿没真跑」必须留在 event_log 里，
            # 否则合并后没人能分清哪些回滚是真执行过的。
            result = {"ok": False, "error": {
                "stage": "sandbox_unavailable", "path": None, "hunk": None,
                "message": "sandbox_git_apply 尚未实现（Task-B 合并前的预期状态）"}}
            log.warning("[%s] 沙箱未就位，补偿只生成事件未真实回滚", task["task_id"])

        self.store.append_event_log({
            "trace_id": task["trace_id"], "plan_id": task["plan_id"],
            "task_id": task["task_id"], "event_type": "CompensationExecuted",
            "detail": {
                "mode": content["mode"], "patch_ref": ref, "workdir": workdir,
                "ok": bool(result.get("ok")), "error": result.get("error"),
                "operator": operator, "note": note,
                "files": len(patch_art["content"].get("files", [])),
            },
        })
        return result

    # ------------------------------------------------------------------
    def _advance(self, plan_id: str) -> None:
        if self.dispatch_ready(plan_id) == 0:
            # 冻结任务被重规划取代了，永远不会 DONE —— 计入完成判定会让 Plan
            # 卡在 RUNNING 上再也出不来。
            tasks = [t for t in self.store.list_tasks(plan_id) if not _is_frozen(t)]
            if tasks and all(t["state"] == TaskState.DONE for t in tasks):
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
