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
from maos.config import get_config_source
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

# -- 网关回执：replan 的第三条触发线（手册 R2 / Demo 分镜 02:30）----------------
# 判据**不在这里算**。四象限由 ReviewerGate 的第七道闸按
# maos/tools/gateway_codes.py 的官方码表算出，随 finding 一起送进来；控制面只认
# disposition 这个字段，自己一次 lookup 都不做。理由是判据要单点：控制面再推断一遍，
# 两处口径迟早分叉，而分叉那天的症状是「该转人工的自旋了」—— 正是本条要防的事。
GATEWAY_GATE = "gateway"

#: retriable=True + outcome=failed —— 网关在入口就拒了，业务确定没执行，
#: 重发不会造成第二笔。**四格里只有这一格允许触发重规划换渠道。**
GW_REPLAN_CHANNEL = "replan_channel"
#: retriable=True + outcome=unknown —— 能再发一次，但那一笔的下落网关自己说不清。
#: 直接重发就可能造成第二笔退款，必须先 gateway.query（铁律 8）。
GW_QUERY_FIRST = "query_first"
#: retriable=False + outcome=failed —— 终态失败，原样重发没有意义，转人工或改单。
GW_HUMAN_TERMINAL = "human_terminal"
#: retriable=False + outcome=unknown —— 最危险的一档：既不能原样重发，下落也不明。
#: 未知错误码（不在已核对官方表里）一并归到这一档，不许兜底成「可重试」。
GW_QUERY_OR_HUMAN = "query_or_human"

#: 这三格一律不许自旋：出现任意一条就**一票否决**重规划，且否决先于下面那两条
#: 既有触发线判。重规划会把任务重新派发出去，那等价于重发 —— 而这三格恰恰是
#: 「不许重发」的三格。少了这条优先级，一轮里凑够两个别的 blocker 就能把一笔
#: 下落不明的退款重新发一次。
GW_NO_REPLAN = frozenset({GW_QUERY_FIRST, GW_HUMAN_TERMINAL, GW_QUERY_OR_HUMAN})

# -- 第三出口：机器返工修不好的，一次干净转人工（跨轨冻结契约 D-1）--------------
# 原先 rework 只有两个出口：重规划，或者普通返工到 max_attempts 耗尽后 FAILED。
# 收敛是对的（不自旋、不假绿），但**收敛的姿势不对** —— 一笔「交易不存在」会被
# 原样重发两次才失败，而这两次重发从第一次就注定不可能成功
# （docs/BACKLOG.md 的 ## task-X2）。第三出口就是把这一类在**第一次**就停到人手上。
#
# 判据同样不在这里算：闸负责说「这条 finding 机器返工修不好」（产 disposition 与
# scope），控制面只负责把它路由到人。口径同 GW_* 那一段，理由也同 —— 两处推断迟早分叉。

#: 控制面声明「这一跳在等人裁决」的**唯一**标记，写进 ``detail["await"]``。
#: 下游按它捞人（``HumanApprovalQueue.pending()``、``kb/experiment.py``），
#: 与 ``effect_risk`` 无关 —— 理由见 docs/DECISIONS.md 的 ## task-D1 设计点 3。
#:
#: 为什么收成一个常量 + 一个出口（``_escalate_to_human``）：控制面有**两条**
#: 「机器已经没有别的招了」的分支（第三出口 / replan 上限），改造前两条各自手写
#: 一遍这个字面量。字面量各写一套，就是同一条保证有两份实现 —— 改一处漏一处时
#: 不会报错，只会**静默漏捞**：任务停在 BLOCKED 而没有任何人捞得到，
#: 比明确的 FAILED 更糟（docs/BACKLOG.md 的 ## task-D1 第 1 条预言的正是这件事）。
AWAIT_HUMAN_DECISION = "human_decision"

#: 网关回执判成终态失败 / 说不清且不可重发。机器把同一份产物再交一遍，
#: 撞的还是同一个码（``ACQ.TRADE_NOT_EXIST`` 不会因为重发就变成存在）。
HUMAN_EXIT_GATEWAY = "gateway_needs_human"
#: 缺陷落在**方案**上而不是这一次产出上。返工只会拿同一份规格再做一遍。
HUMAN_EXIT_PLAN_DEFECT = "plan_defect"

#: 四象限里走第三出口的两格 —— 共同点是 ``retriable=False``：原样重发没有意义。
#: 与 GW_NO_REPLAN 的分界要看清：那一条答的是「许不许换渠道重发」，
#: 这一条答的是「机器还有没有别的招」。``GW_QUERY_FIRST`` 两条都不许重发，
#: 但它还有一招 —— ``gateway.query`` 去问，那是机器动作，不该占人的时间
#: （详见 docs/DECISIONS.md 的 ## task-D1 设计点 1）。
GW_HUMAN_EXIT = frozenset({GW_HUMAN_TERMINAL, GW_QUERY_OR_HUMAN})

#: finding 的 ``scope``：**缺省不写即为任务级**，闸产 plan 级 finding 时显式写它。
#: 任务级缺陷返工能修（换个写法、补一份证据），plan 级不能 —— 方案错了，
#: 拿同一份规格再做一遍还是错的。
SCOPE_PLAN = "plan"

#: 与 ``gate.SEVERITY_INFO`` 同一个字面量，刻意不共享一处定义：import 方向是
#: gate -> control_plane，反向 import 会成环。字面量重复两处好过循环依赖。
SEVERITY_INFO = "info"

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


#: 一条 finding 上足以回答「为什么转人工」的字段。缺的键不写进去 —— 一串
#: ``"scope": null`` 会让 event_log 里真正有值的那几个字段淹掉。
_FINDING_REF_KEYS = ("gate", "id", "code", "severity", "disposition", "scope")


def _finding_ref(finding: dict) -> dict:
    """把一条 finding 压成可落 event_log 的引用。``message`` 那段长文案不带走。"""
    return {k: finding[k] for k in _FINDING_REF_KEYS
            if finding.get(k) is not None}


def _comp_order(art: dict) -> tuple[int, str]:
    """compensation 的确定性排序键：先看它指向第几次 attempt，再拿 artifact_id 兜全序。

    ``attempt`` 相同只发生在同一次 TaskResult 交回多份 patch_set 时，那几条
    compensation 的 content 本就一模一样，选哪条都等价 —— 补 artifact_id 是为了让
    「选中哪条」这件事完全不依赖 ``list_artifacts`` 的返回顺序，而不是为了分优劣。

    缺 patch_ref 的排到最末位（attempt=-1）而不在这里炸：形状校验归
    ``_execute_compensation``，它对**选中的**那条硬失败（C-5 反例：补偿绝不兜底成静默
    不执行）。排序阶段就炸会让一条坏数据连累掉本来选得对的那次回滚。
    """
    ref = art["content"].get("patch_ref") or {}
    attempt = ref.get("attempt")
    return (attempt if isinstance(attempt, int) else -1, art["artifact_id"])


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

    def _escalate_to_human(self, task: dict, *, event_id: str, findings: list[dict],
                           detail: dict, reason: str, **extra) -> dict:
        """「机器已经没有别的招了」的**唯一**出口：AWAITING_REVIEW -> BLOCKED。

        两条分支共用它 —— 第三出口（``HUMAN_EXIT_*``）与 replan 上限
        （``replan_limit_exceeded``）。两条都**不动 plan 状态**：plan 的死活由人的
        决定说了算，闸当场把 plan 判死就是替人做了那个决定
        （docs/DECISIONS.md 的 ## task-D1 设计点 4）。

        ``reason`` 之外的差异走 ``**extra`` 各写各的（第三出口写 ``evidence``，
        replan 上限写 ``replan_used``）—— 差异本就该差异，同源的是**转人工这件事
        怎么声明**：``await`` 标记只在这里写一次，下游只需要认一个字面量。
        """
        return self._transit(task, TaskState.BLOCKED, event_id=event_id,
                             findings=findings,
                             detail={**detail, "await": AWAIT_HUMAN_DECISION,
                                     "reason": reason, **extra})

    # ------------------------------------------------------------------
    # 计划与任务创建（Manager Agent 调用）
    # ------------------------------------------------------------------
    def create_plan(self, *, goal: str, trace_id: str, tasks: list[dict],
                    plan_id: str | None = None) -> str:
        """建 Plan 与其下的任务，返回 plan_id。

        ``plan_id`` 可选，缺省仍自己生成 —— 既有调用点一行都不用改。给了就用它：
        **规划期**发生的调用（Manager 规划前的知识检索、``flows/scenario_5.py`` 的
        ``issue.aggregate`` 需求归一）跑在建 Plan **之前**，那一刻还没有 plan_id
        可写，事件只能落空串，于是 trace 把它们列进 ``stray_events`` —— 一次真实
        发生的检索挂不到任何一棵树上（docs/BACKLOG.md ``## task-X4`` 第 2 条）。
        调用方先 ``E.new_id("plan")`` 拿到 id、规划期带着它跑，再原样传进来，
        那些事件就归到了它们真正属于的那棵树。

        另起一个「规划期」伪 plan 是另一条路，**没走**：为了消 warn 在 trace 里
        造出一棵不存在的树，是拿假绿换绿。
        """
        plan_id = plan_id or E.new_id("plan")
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
        """认领一次派发。**状态校验在幂等闸之前**，顺序反了任务会永久卡死。

        幂等键一旦消费就不回滚（store 只有 claim/finish，没有撤销口）。所以校验必须
        先跑：Worker 抢在 dispatch 之前认领一次，任务尚为 PENDING，认领理应失败 ——
        可若失败前 key 已被烧掉，等 dispatch 真发出来，**同一 attempt 的合法认领**
        会被当成重复投递拒掉，任务停在 DISPATCHED 再没人能领走它。

        与本模块铁律 3（先过幂等闸）不冲突：闸门仍挡在**状态变更**前面，挪到它前面的
        只是一次不消费任何东西的只读前置校验。并发安全也没丢 —— 两个 Worker 同时过了
        状态校验后，仍要争同一个 key 的原子写入，只有一个拿得到 None。
        """
        task = self.store.get_task(task_id)
        if task["state"] != TaskState.DISPATCHED:
            log.warning("[%s] 状态是 %s，不可认领", task_id, task["state"])
            return None
        key = f"claim:{task_id}:{attempt}"
        if self.store.claim_idempotency(key, "claim", task_id) is not None:
            log.info("[%s] 重复认领，忽略", task_id)
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
            human_exit = self._human_exit(findings)
            if human_exit is not None:
                # 第三出口。**必须排在 max_attempts 之前** —— 排在后面的话最后一轮
                # 仍然 FAILED，等于白改；而这一单买的正是「少重发那两次」。
                # 姿势同下面的 replan_limit_exceeded：AWAITING_REVIEW -> BLOCKED，
                # 不动 plan 状态（plan 的死活由人的决定说了算，不由闸说了算）。
                # 「同姿势」不再靠两处各写一遍，而是共用 _escalate_to_human。
                reason, evidence = human_exit
                log.warning("[%s] %s —— 机器返工修不好，一次转人工，不再重发",
                            task["task_id"], reason)
                self._escalate_to_human(task, event_id=env.event_id, findings=findings,
                                        detail=detail, reason=reason, evidence=evidence)
            elif env.attempt >= task["max_attempts"]:
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
                    self._escalate_to_human(
                        task, event_id=env.event_id, findings=findings, detail=detail,
                        reason="replan_limit_exceeded",
                        replan_used=self._replan_used(task["plan_id"]))
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
    # 第三出口：判定同样是纯函数，与 _should_replan 并列
    # ------------------------------------------------------------------
    @staticmethod
    def _human_exit(findings: list[dict]) -> tuple[str, list[dict]] | None:
        """这一轮的 findings 里有没有「机器返工修不好」的？有就返回 (reason, 证据)。

        两条判据，按此顺序定 ``reason`` —— 顺序有意义而不是随手排的：网关那条是
        **外部事实**（那笔交易不存在，重发多少次都不存在），plan 那条是**内部判断**
        （方案写错了）。两条同时命中时报外部事实，因为它是不可谈判的那一条，
        人拿到工单先要知道的是它。

        · ``gate == GATEWAY_GATE`` 且 ``disposition`` 落在 ``GW_HUMAN_EXIT``
          —— 四象限里 ``retriable=False`` 的两格。重发不会有不同结果。
        · ``scope == SCOPE_PLAN`` 且 ``severity != info`` —— 缺陷在方案上，
          返工是拿同一份规格再做一遍。``scope`` 缺省不写即任务级，所以这一条
          **只对显式声明了 plan 级的 finding 生效**，不会误伤既有的六道闸。

        返回的证据是 findings 的**投影**而不是原文：完整 findings 已经随
        ``_transit(findings=...)`` 落在任务行上了，detail 里再存一份长文案只是噪声。
        投影保留的五个字段够一个人判断「为什么轮到我」，也够审计对回码表。

        判定与路由分开（同 ``_should_replan`` 的口径）：本方法不碰 store、不发事件，
        因此边界可以脱开整条链路单测 —— 见 ``maos/tests/test_human_exit.py``。
        """
        gateway_hits, plan_hits = [], []
        for f in findings:
            if not isinstance(f, dict):
                continue
            # 两条判据各自独立地扫，写成 if/elif 行为完全一样（elif 只在 (a) 已命中时
            # 跳过 (b)，而那一轮 (a) 本就赢下优先级）—— 写成两个独立 if 是为了跟契约
            # §4.2 的措辞一一对上：那里是两条并列的「任一 finding……」，顺序只决定
            # reason 报哪一个，不决定谁参与判定。哪天优先级改了，这里不用跟着重排。
            if f.get("gate") == GATEWAY_GATE and f.get("disposition") in GW_HUMAN_EXIT:
                gateway_hits.append(f)
            if f.get("scope") == SCOPE_PLAN and f.get("severity") != SEVERITY_INFO:
                plan_hits.append(f)

        if gateway_hits:
            return HUMAN_EXIT_GATEWAY, [_finding_ref(f) for f in gateway_hits]
        if plan_hits:
            return HUMAN_EXIT_PLAN_DEFECT, [_finding_ref(f) for f in plan_hits]
        return None

    # ------------------------------------------------------------------
    # Replan：判定与执行分开 —— 判定是纯函数，可以脱开重规划回调单独验边界
    # ------------------------------------------------------------------
    def _should_replan(self, task: dict, findings: list[dict]) -> bool:
        """三条触发线，任一命中即重规划；网关回执另有一条**一票否决**。

        · **网关回执**（第三条，手册 R2）：``{"gate": "gateway"}`` 的 finding 带
          ``disposition``，四象限见本文件 GW_* 常量。只有 ``replan_channel``
          （retriable=True 且 outcome=failed）才允许换渠道重试；另外三格一律否决，
          **而且否决先于下面两条线判**。理由是 retriable 与 outcome 正交：前者答
          「能不能再发一次」，后者答「这一笔到底执行了没有」（铁律 8，MAOS 不持有
          权威事实）。重规划会把任务重新派发，等价于重发 —— outcome=unknown 时
          那可能造出第二笔退款，retriable=False 时重发则纯属自旋。
        · 单轮 findings 中 blocker >= 2：一轮里堵住两处，问题多半在方案本身，
          拿同一份规格再返工一次是浪费一个 attempt。
        · 同一任务第 2 次 rework：第一次返工没解决，说明规格没描述清楚。

        「第几次 rework」从 event_log 数，不另存计数器：event_log 是 Trace 与审计的
        唯一来源（本文件铁律 4），再维护一个内存计数器就有了第二份事实，进程重启即失真。
        判定发生在本次 REWORK 落库**之前**，所以历史里有 1 条就意味着这将是第 2 次。
        """
        dispositions = {f.get("disposition") for f in findings
                        if isinstance(f, dict) and f.get("gate") == GATEWAY_GATE}
        vetoed = dispositions & GW_NO_REPLAN
        if vetoed:
            log.info("[%s] 网关回执处置为 %s，不许自旋 —— 否决重规划",
                     task["task_id"], sorted(vetoed))
            return False
        if GW_REPLAN_CHANNEL in dispositions:
            log.info("[%s] 网关回执可重发且业务确定未执行，触发重规划换渠道",
                     task["task_id"])
            return True

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
        """MAOS_MAX_REPLAN，默认 2。非法值回退默认并告警，不让配置笔误变成自旋。

        走 `maos.config` 的配置面而不是直接读 `os.environ`（T28）：缺省源就是
        `os.environ.get`，取值逐字节不变。
        """
        raw = get_config_source().get(ENV_MAX_REPLAN, "").strip()
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
        if not specs:
            # 重规划一个规格都没给出（模型输出空、或调用异常被上游吞成了空列表）——
            # open_tasks 已被 _apply_replan 全部冻结，此刻没有任何任务会再往前走。
            # 放弃不是完成：哪怕此前有别的任务做完了，这个计划的目标也没达成，
            # 收成 DONE 就是拿「Agent 都回完话」冒充业务成功。
            # 必须先 start_plan 再落 FAILED —— PENDING->FAILED 不在迁移表里，
            # 只有 RUNNING->FAILED 有（铁律 1：不许为这条路新增迁移）。
            log.warning("[%s] 重规划未产出任何新规格，计划收敛为 FAILED", plan_id)
            self._fail_plan(plan_id)

    def _apply_replan(self, plan_id: str, open_tasks: list[dict], specs: list[dict]) -> None:
        """新规格接管未完成的任务；接管不下的旧任务冻结，多出来的新规格建新任务。

        逐位覆写而不是「旧的全冻结 + 新的全新建」：覆写让 task_id、attempt 与
        event_log 的因果链连续，一条任务的完整经历（含重规划前那次失败）仍串在
        同一个 task_id 上；全新建会把轨迹断成两截，Trace 上看不出前后是同一件事。

        **findings 保留不清**：新规格加上「上一版为什么不行」一起喂给下一轮，
        比只给新规格更有信息量（dispatch_ready 会把它作为 rework_findings 发出去）。
        它靠**不出现在下面这次 update_task 里**来保留，别把它补进去。

        **缺省一律保留原值**，整个覆写分支一个口径。原先 title/risk_level 保留原值而
        inputs/acceptance/depends_on 缺省成空，同一次调用里混了两套语义：重规划只想
        换个标题，任务就被清成空输入重新派发，depends_on 被清空还会让它抢在依赖项前面
        跑。规格没提到的字段就是「这块不改」，不是「这块清空」。
        """
        for task, spec in zip(open_tasks, specs):
            self.store.update_task(
                task["task_id"],
                # role 原先根本不在覆写里，于是「重规划换角色」做不到 —— reviewer 的
                # 规格被安在 coding 任务上，照旧交给 coding 去做。
                role=spec.get("role", task["role"]),
                title=spec.get("title", task["title"]),
                inputs=spec.get("inputs", task["inputs"]),
                acceptance=spec.get("acceptance", task["acceptance"]),
                depends_on=spec.get("depends_on", task["depends_on"]),
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
        """人工审批。补偿是**有外部副作用**的动作，两道闸都必须挡在它前面。

        原先 ``_execute_compensation()`` 直接跑在 ``_transit()`` 之前，而状态机守卫在
        ``_transit()`` 里 —— 重复投递一次驳回，补偿先完整执行完，异常才抛出：
        守卫拦得住状态，拦不住副作用。``git apply -R`` 对同一份补丁反着打两遍，
        是实打实的重复外部动作（铁律 8）。on_task_result / on_review_verdict 都过了
        幂等闸，唯独人工决策这条路没过。

        补偿仍在 ``_transit`` **之前**执行（phase-4.md:20 的顺序不动）：往前挪的是守卫，
        不是把补偿往后挪 —— 状态一旦落 FAILED，「产物还在外面」就没人记得了。

        两道闸的分工：
          · ``assert_transition`` 挡**顺序**重复 —— 任务已是终态，第二次驳回当场抛，
            一行副作用都没发生。放在幂等闸前面同 claim()：非法调用不该烧掉 key，
            烧了会让这个任务此后再也审批不了。
          · 幂等键挡**并发** —— 两个操作员同时驳回，都过了状态校验，仍要争同一个 key。

        key 取 ``human:<task_id>`` 而不带决策：一个任务只可能被人工决策一次
        （DONE / FAILED 都是终态）。带上决策的话，「先批准后驳回」会拿到一个没被消费过
        的新 key，补偿照跑一遍，非法迁移才在后面抛 —— 同一个 bug 换扇门进来。
        """
        task = self.store.get_task(task_id)
        dst = TaskState.DONE if approved else TaskState.FAILED
        assert_transition(task["state"], dst)

        key = f"human:{task_id}"
        if self.store.claim_idempotency(key, "human", task_id) is not None:
            log.info("[%s] 重复人工决策，短路", task_id)
            return

        if not approved:
            # 先回滚再改状态（phase-4.md:20 的顺序）：状态一旦落 FAILED，
            # 「这个任务的产物还在外面」这件事就没人记得了。
            self._execute_compensation(task, operator=operator, note=note)
        self._transit(task, dst, detail={"operator": operator, "note": note})
        self.store.finish_idempotency(key, {"approved": approved, "operator": operator})
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

        **缺 workdir 同样硬失败**，同一个口径。原先缺省取 ``"."``：Task-B 合并前
        ``sandbox_git_apply`` 恒抛 ``NotImplementedError``，取什么都无所谓；合并后它是
        真实现，缺省值就成了「拿补丁对**本仓库工作区**跑 ``git apply -R``」。它至今
        没出事只是因为补丁都恰好打不上 —— 而那正是最坏的失效形态：真打上了才会知道，
        且日志上看是一次成功的补偿。要回滚哪里必须有人明说，猜一个是不允许的。

        注意与「工具没跑成」的分界：env 没设 = 配置缺失，**连试都试不了**，抛；
        env 设了但目录不可用 = 试过了、工具如实报错，走 ``ok=False`` 落进 event_log。
        前者没有可记的事实，后者有 —— 混为一谈会让「没人配」和「回滚失败」看起来一样。
        """
        comps = [a for a in self.store.list_artifacts(task["task_id"])
                 if a["kind"] == KIND_COMPENSATION]
        if not comps:
            log.info("[%s] 无补偿引用，跳过回滚", task["task_id"])
            return None

        # 多轮 attempt 会附着多条 compensation，语义是「回滚最近一次落地的那份补丁」。
        # 不能写 comps[-1]：COMPENSATION_VERSION 恒为 0，而 list_artifacts 只
        # ORDER BY version —— 同值行的相对顺序 SQL 不保证。现在拿到的顺序是 SQLite
        # 隐式 rowid 的副产品，换后端或加索引就可能翻转，而翻转的后果是**回滚了错误
        # attempt 的补丁**。改按 patch_ref.attempt 选：排序依据来自内容本身，与
        # list_artifacts 的返回顺序无关（排序口径归 store，那是另一轨的面，不去动它）。
        content = max(comps, key=_comp_order)["content"]
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

        workdir = os.environ.get(ENV_SANDBOX_WORKDIR) or ""
        if not workdir:
            raise ValueError(
                f"[{task['task_id']}] 补偿要回滚，但没人说该回滚到哪个工作目录 —— "
                f"请设 {ENV_SANDBOX_WORKDIR}。缺省取 '.'（仓库根）已废止：那会拿补丁"
                f"对本仓库工作区跑 git apply -R，且补丁恰好打不上时看起来一切正常")
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
            if not tasks:
                # 一条活任务都不剩：全冻结了，而新方案一个都没接手（重规划返回空规格）。
                # 这不是完成，是没做成 —— 收 DONE 正好撞上本项目最核心的那句话
                # 「所有 Agent 都回复完成 ≠ 业务成功」。走既有迁移
                # RUNNING->FAILED("task_failed")，不加状态、不加迁移（铁律 1）。
                log.warning("[%s] 已无可推进的任务（全部被重规划冻结），收敛为 FAILED",
                            plan_id)
                self._fail_plan(plan_id)
            elif all(t["state"] == TaskState.DONE for t in tasks):
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
