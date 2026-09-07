"""计划审批 —— 计划先给人看，人批准了才许开跑；驳回带着反馈退回修订重提。

## 这个文件存在的理由

「Agent 先在只读模式出计划、人批准后才允许开跑」这件事，地基**全都已经在了**：

  · 「建了但还没跑」这个状态早就有 —— ``PlanState.PENDING``，
    以及 ``(PENDING, RUNNING): "start"`` 这条迁移；
  · 建与跑本来就是两步 —— ``ControlPlane.create_plan`` 与 ``start_plan``
    是**两个**方法，中间隔着一整个函数边界；
  · 规划者本就只读 —— ``ManagerAgent`` 的 ``allowed_tools`` 是空的、
    ``write_scope`` 只有 plan/task，它连派单都不接；
  · 「带着反馈重出一版规格」的回调也早就有 —— ``Replanner`` 与 ``set_replanner``。

缺的只有**那个停靠点**：所有场景 ``create_plan`` 之后**立刻** ``start_plan``
（``maos/flows/scenario_1.py`` 是标准形态，其余场景同形），于是 PENDING 这个状态
在生产路径上从来没有停留过一个瞬间 —— 计划一建出来就已经在跑了，人没有插进去的缝。

本文件补的就是那一瞬间，**一行内核都不改**：不加 PlanState、不加迁移、
不加 Topic、不加 EventType（铁律 1 / 铁律 9）。四种审批事件一律走
``store.append_event_log`` 的自由 ``event_type``，先例是 ``ArtifactSeeded``
（``maos/agents/testing.py``）、``KbRetrieved``（``maos/kb/retriever.py``）、
``ConfigChanged``（``maos/config/audit.py``）。

生态位同 ``maos/runtime/gate.py`` 的 ``HumanApprovalQueue``：捞出在等人的东西、
把人的决定回灌进控制面。区别只在颗粒度 —— 那个捞 task，这个捞 plan。

## 三处最容易被抄错的地方（改这个文件之前先读完这一节）

### 1. 驳回**不** start —— 所以不能复用 ``ControlPlane._replan``

``_replan`` 末尾会 ``start_plan``。本文件要的是它的**前半段**：拿 feedback 调
replanner、再把新规格应用到任务上，然后**停下**。重规划完就自己跑起来的话，
人的那次驳回等于没发生 —— 他驳的是方案，拿到的却是「方案改了并且已经在跑了」，
第二次审批的机会被系统自己吃掉了。所以 ``reject()`` 之后 plan 仍是 PENDING，
仍会出现在 ``pending()`` 队列里等人再看一次。

### 2. 「重规划中的 plan」不是「待审批的 plan」

``pending()`` 的判据有两条，第二条不能省：state 是 PENDING，**且** event_log 里
从来没有过一次 ``PENDING -> RUNNING`` 的 ``PlanTransition``。

因为 ``replan``（``states.py`` 的 ``(RUNNING, PENDING)``）会把一个**跑起来过**的
plan 退回 PENDING。那种 plan 是「机器正在重规划」，不是「在等人批」。只按 state
捞的话，人会被叫去批准一个其实已经开跑过的计划 —— 而他一按批准，
``start_plan`` 就把重规划期间本不该派发的任务全发出去了。

### 3. 幂等键带 ``round``，与 ``human:<task_id>`` 刻意不同

键的形状是 ``plan_approval:<plan_id>:<round>``，``round`` = 已发生的
``PlanRejected`` 条数（第一次审批是 0）。

``ControlPlane.human_decision`` 的键是 ``human:<task_id>``，不带轮次 —— 因为
一个任务只可能被人工决策**一次**（DONE / FAILED 都是终态）。plan 不一样：
它会被审批**多次**（驳回 -> 重规划 -> 再审批）。键里只有 plan_id 的话，
第二轮审批会被当成重复投递当场短路，人再也批不动这个计划，而且**一声不吭**。

两个键长得像、语义相反，是这个文件最容易抄错的一处。

## 为什么调私有的 ``ControlPlane._apply_replan``

``_apply_replan`` 是全仓**唯一**一份「新规格接管旧任务」的口径：逐位覆写（保住
task_id 与 event_log 的因果链）、findings 保留不清、多出来的新规格建新任务、
接管不下的旧任务冻结。自己再写一份必然与它分叉，而分叉那天的症状是「重规划之后
有的任务用新口径、有的用旧口径」，没人看得出来。

调私有方法是**有意为之**，不是图省事。本轨不许改 ``control_plane.py``
（那是并行轨共用的面），整合期可考虑把它提升为公开方法。这条已记进
``docs/DECISIONS.md``。``_is_frozen`` 同理：冻结判定只留一处，三个调用点共用。
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any, Iterable

from maos.config import get_config_source
from maos.contracts.states import (
    PLAN_TRANSITIONS,
    TERMINAL_STATES,
    PlanState,
    can_transition,
)
from maos.core.control_plane import SCOPE_PLAN, ControlPlane, _is_frozen
from maos.core.store import Store

log = logging.getLogger("maos.plan_approval")

# -- 驳回上限 -----------------------------------------------------------
#: 刻意**不**复用 ``MAOS_MAX_REPLAN``。那个旋钮管的是「机器自己触发了几次重规划」，
#: 这个管的是「人驳回了几次」。两件事的合理阈值不必相同，共用一个旋钮的后果是
#: 调其中一个把另一个也调了 —— 而且调的人不会知道。
ENV_MAX_PLAN_REJECT = "MAOS_MAX_PLAN_REJECT"
DEFAULT_MAX_PLAN_REJECT = 2

# -- 四种审批事件（自由 event_type，不进冻结的 events.py）------------------
EV_APPROVED = "PlanApproved"
EV_REJECTED = "PlanRejected"
#: 与控制面自己那条 ``Replanned`` 刻意不同名：那条是「机器判定要重规划」，
#: 这条是「人驳回之后重出的规格已应用」。混成一个名字，审计时分不出是谁的决定。
EV_REPLANNED = "PlanReplanned"
EV_EXHAUSTED = "PlanApprovalExhausted"

#: ``claim_idempotency`` 的 ``op`` 列取值，与 ``human`` / ``task_result`` 同一格。
IDEMPOTENCY_OP = "plan_approval"

#: 人的驳回意见压成 finding 喂给 replanner 时用的 gate 名。它不是一道闸产出的，
#: 但 replanner 的入参形状就是 finding 列表 —— 与其另发明一个通道，
#: 不如如实标出这条 finding 的来源是人。
REJECT_GATE = "plan_approval"
SEVERITY_BLOCKER = "blocker"


class PlanNotAwaitingApproval(Exception):
    """这个 plan 现在不在等审批。不要 catch 掉 —— 批一个不该批的计划就是开跑。"""


class PlanApprovalQueue:
    """计划审批队列。停在 PENDING 等人的 plan 从这里捞，捞不到的就是没人知道。"""

    def __init__(self, store: Store, cp: ControlPlane) -> None:
        self.store = store
        self.cp = cp

    # ------------------------------------------------------------------
    # 捞
    # ------------------------------------------------------------------
    def pending(self, plan_ids: Iterable[str] | None = None) -> list[dict]:
        """所有**待审批**的 plan。判据两条，第二条不能省（理由见模块 docstring 第 2 点）。

        · ``state == PENDING``；
        · event_log 里没有过 ``PENDING -> RUNNING`` 的 ``PlanTransition``
          —— 跑起来过又被 ``replan`` 退回 PENDING 的，是**重规划中**不是**待审批**。

        ``plan_ids`` 缺省时自己去库里枚举全部 plan。``Store`` 没有「列出所有 plan」
        这个方法，而本轨不许改 ``store.py``（铁律 2），所以借核心 store 的连接与锁
        做一次**只读** ``SELECT``（先例：``maos/store/sqlite_store.py`` 的
        ``SqliteStorePort`` 借 ``_conn`` / ``_lock``；同一条 SQL 也写在
        ``maos/obs/trace.py`` 的 ``list_plan_ids`` 里）。换后端时把 plan_id 显式传进来
        即可，不必等 store 加方法。
        """
        ids = list(plan_ids) if plan_ids is not None else self._all_plan_ids()
        out = []
        for plan_id in ids:
            plan = self.store.get_plan(plan_id)
            if plan is None or plan["state"] != PlanState.PENDING:
                continue
            if self._has_started(plan_id):
                continue        # 重规划中，不是在等人批
            out.append(plan)
        return out

    def preview(self, plan_id: str) -> dict:
        """只读投影：把一份计划摊开给人看。**不调模型、不改任何状态。**

        人要在这里回答的是「这个计划该不该跑」，所以带的不只是任务清单，还有
        依赖拓扑 —— 谁挡着谁、哪几条能并行、有没有环、有没有指向不存在的任务的
        依赖。后两项恰恰是人工审批这道闸最该拦下来的一类规划缺陷：机器把它们
        跑成「永远没有任务 ready」的静默停摆，人一眼就看得出。

        被重规划冻结的任务照样列出来并标 ``frozen`` —— 它们不会被派发，
        看计划的人必须知道哪几条其实是死的，否则会以为方案比实际更完整。
        """
        plan = self._require_plan(plan_id)
        tasks = self.store.list_tasks(plan_id)
        rejects = self._rejects(plan_id)
        limit = self._max_reject()
        return {
            "plan_id": plan["plan_id"],
            "trace_id": plan["trace_id"],
            "goal": plan["goal"],
            "state": plan["state"],
            "round": rejects,
            "reject_limit": limit,
            "exhausted": rejects >= limit,
            "awaiting": plan["state"] == PlanState.PENDING and not self._has_started(plan_id),
            "tasks": [{
                "task_id": t["task_id"],
                "title": t["title"],
                "role": t["role"],
                "state": t["state"],
                "risk_level": t["risk_level"],
                "effect_risk": t["effect_risk"],
                "depends_on": list(t["depends_on"]),
                "acceptance": list(t["acceptance"]),
                "frozen": _is_frozen(t),
            } for t in tasks],
            "topology": self._topology(tasks),
        }

    # ------------------------------------------------------------------
    # 人的决定
    # ------------------------------------------------------------------
    def approve(self, plan_id: str, operator: str) -> bool:
        """批准：落 ``PlanApproved``，然后 ``start_plan``。返回 False 表示重复投递。

        校验（``_require_awaiting``）排在幂等闸**前面**，顺序同
        ``ControlPlane.claim``，理由也同：幂等键一旦消费就不回滚（store 只有
        claim/finish，没有撤销口）。非法调用若先把 key 烧掉，等这个 plan 真的轮到
        审批时，**合法的那次批准**会被当成重复投递短路掉，计划再也开不了跑。
        """
        plan = self._require_awaiting(plan_id)
        rejects = self._rejects(plan_id)
        key = self._key(plan_id, rejects)
        if self.store.claim_idempotency(key, IDEMPOTENCY_OP, plan_id) is not None:
            log.info("[%s] 重复审批（第 %d 轮），短路", plan_id, rejects)
            return False

        tasks = self.store.list_tasks(plan_id)
        self._log(plan, EV_APPROVED,
                  {"operator": operator, "tasks": len(tasks), "round": rejects})
        self.cp.start_plan(plan_id)
        self.store.finish_idempotency(key, {"approved": True, "operator": operator})
        log.info("[%s] %s 批准，%d 个任务开跑", plan_id, operator, len(tasks))
        return True

    def reject(self, plan_id: str, operator: str, feedback: str) -> bool:
        """驳回：落 ``PlanRejected``，带反馈重出规格并应用，**仍停在 PENDING**。

        返回 False 表示重复投递。plan 到最后是死是活由人说了算 —— 到了驳回上限
        也不自动判死，只落一条 ``PlanApprovalExhausted`` 说明「该改的是目标，
        不是再改一次方案」。口径同 ``ControlPlane._escalate_to_human``：
        闸当场把 plan 判死就是替人做了那个决定。
        """
        plan = self._require_awaiting(plan_id)
        rejects = self._rejects(plan_id)
        key = self._key(plan_id, rejects)
        if self.store.claim_idempotency(key, IDEMPOTENCY_OP, plan_id) is not None:
            log.info("[%s] 重复驳回（第 %d 轮），短路", plan_id, rejects)
            return False

        self._log(plan, EV_REJECTED,
                  {"operator": operator, "feedback": feedback, "round": rejects})
        limit = self._max_reject()
        if rejects >= limit:
            # 判定用的是**这次驳回之前**的条数：limit=2 时，第 3 次驳回不再重规划。
            self._log(plan, EV_EXHAUSTED, {"rejects": rejects + 1, "limit": limit})
            log.warning("[%s] 已被驳回 %d 次（上限 %d），不再重规划 —— 需要人改目标",
                        plan_id, rejects + 1, limit)
        elif self.cp._replanner is None:
            # 没接 replanner 不是故障：反馈已经留痕，plan 停在 PENDING 等人改了目标
            # 再提。这里**不落** ``PlanReplanned`` —— 什么都没重规划，落一条说重规划
            # 过了就是假绿。
            log.warning("[%s] 未注入 replanner，驳回只留痕、不重出规格", plan_id)
        else:
            self._replan_after_reject(plan, operator, feedback, rejects)

        self.store.finish_idempotency(key, {"approved": False, "operator": operator})
        return True

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _replan_after_reject(self, plan: dict, operator: str, feedback: str,
                             rejects: int) -> None:
        """带着人的反馈重出规格并应用。**这里不 start** —— 那正是本轨的题眼。"""
        plan_id = plan["plan_id"]
        open_tasks = [t for t in self.store.list_tasks(plan_id)
                      if t["state"] not in TERMINAL_STATES and not _is_frozen(t)]
        # 人的意见排在最前，历史 findings 跟在后面：重规划要看的是「整个计划为什么
        # 走不通」，只喂一条就退化成了返工（口径同 ControlPlane._replan）。
        findings = [self._feedback_finding(operator, feedback, rejects)]
        findings += [f for t in self.store.list_tasks(plan_id) for f in t["findings"]]

        specs = self.cp._replanner(goal=plan["goal"], findings=findings,
                                   open_tasks=open_tasks) or []
        self.cp._apply_replan(plan_id, open_tasks, specs)
        self._log(plan, EV_REPLANNED,
                  {"new_specs": len(specs), "open_tasks": len(open_tasks),
                   "round": rejects})
        log.info("[%s] 按驳回意见重出 %d 条规格，仍停在 PENDING 等再次审批",
                 plan_id, len(specs))

    @staticmethod
    def _feedback_finding(operator: str, feedback: str, rejects: int) -> dict:
        """把人的驳回意见压成一条 finding。``scope`` 是 plan：他驳的是整个方案。"""
        return {
            "gate": REJECT_GATE,
            "severity": SEVERITY_BLOCKER,
            "scope": SCOPE_PLAN,
            "id": f"plan-reject-{rejects}",
            "path": None,
            "operator": operator,
            "message": feedback,
        }

    def _require_plan(self, plan_id: str) -> dict:
        plan = self.store.get_plan(plan_id)
        if plan is None:
            raise PlanNotAwaitingApproval(f"没有这个 plan: {plan_id}")
        return plan

    def _require_awaiting(self, plan_id: str) -> dict:
        """approve / reject 共用的前置校验：这个 plan 现在确实在等人批。

        state 那一条走 ``can_transition`` 而不是硬写 ``== PENDING``：判据挂在冻结的
        迁移表上，表变了这里跟着变，不会留下第二份口径。
        """
        plan = self._require_plan(plan_id)
        if not can_transition(plan["state"], PlanState.RUNNING, PLAN_TRANSITIONS):
            raise PlanNotAwaitingApproval(
                f"{plan_id} 状态是 {plan['state']}，开不了跑，也就谈不上审批")
        if self._has_started(plan_id):
            raise PlanNotAwaitingApproval(
                f"{plan_id} 已经开跑过，现在停在 PENDING 是重规划中，不是在等审批")
        return plan

    def _has_started(self, plan_id: str) -> bool:
        """这个 plan 有没有 ``PENDING -> RUNNING`` 过。判据取自 event_log。

        不在 plan 行上另开一个「批过了」的字段：event_log 是 Trace 与审计的唯一来源
        （control_plane 铁律 4），再存一份就有了第二份事实。
        """
        return any(e.get("event_type") == "PlanTransition"
                   and e.get("from_state") == PlanState.PENDING
                   and e.get("to_state") == PlanState.RUNNING
                   for e in self.store.list_event_log(plan_id))

    def _rejects(self, plan_id: str) -> int:
        """已被驳回几次 = event_log 里 ``PlanRejected`` 的条数。

        不另存计数器，理由同 ``ControlPlane._replan_used``：多一份内存里的事实，
        进程重启即失真，而失真的症状是「上限突然又宽了两次」。
        """
        return sum(1 for e in self.store.list_event_log(plan_id)
                   if e.get("event_type") == EV_REJECTED)

    @staticmethod
    def _key(plan_id: str, rejects: int) -> str:
        """幂等键。带 ``round`` 是必须的 —— 理由见模块 docstring 第 3 点。"""
        return f"plan_approval:{plan_id}:{rejects}"

    def _max_reject(self) -> int:
        """``MAOS_MAX_PLAN_REJECT``，默认 2。非法值回退默认并告警，不抛。

        写法照抄 ``ControlPlane._max_replan``：走 ``maos.config`` 的配置面而不是直接
        读 ``os.environ``，缺省源就是 ``os.environ.get``，取值逐字节不变。
        """
        raw = get_config_source().get(ENV_MAX_PLAN_REJECT, "").strip()
        if not raw:
            return DEFAULT_MAX_PLAN_REJECT
        try:
            value = int(raw)
        except ValueError:
            log.warning("%s=%r 不是整数，回退默认 %d",
                        ENV_MAX_PLAN_REJECT, raw, DEFAULT_MAX_PLAN_REJECT)
            return DEFAULT_MAX_PLAN_REJECT
        return max(value, 0)

    def _log(self, plan: dict, event_type: str, detail: dict) -> None:
        """落一条审批事件。``plan_id`` 与 ``trace_id`` 必须都带上。

        缺 ``plan_id`` 的行会掉进 ``maos/obs/trace.py`` 的 ``stray_events``
        —— 挂不上任何一棵 span 树，等于这次审批在 Trace 上没发生过。
        """
        self.store.append_event_log({
            "trace_id": plan["trace_id"],
            "plan_id": plan["plan_id"],
            "event_type": event_type,
            "detail": detail,
        })

    @staticmethod
    def _topology(tasks: list[dict]) -> dict:
        """依赖拓扑：谁挡着谁、哪几批能并行、有没有环、有没有悬空依赖。

        分层用的是最朴素的削峰：入度为零的一批取出来当一层，删掉再取下一批。
        剩下取不完的就是**环**里的任务 —— 不抛异常，如实列进 ``cycle``：
        人工审批这道闸的价值恰恰是让这类计划在开跑前就被看见，而不是让它跑成
        「永远没有任务 ready」的静默停摆。
        """
        order = [t["task_id"] for t in tasks]
        known = set(order)
        blocks: dict[str, list[str]] = {tid: [] for tid in order}
        dangling: list[dict] = []
        remaining: dict[str, set[str]] = {}
        for t in tasks:
            deps = set()
            for dep in t["depends_on"]:
                if dep in known:
                    deps.add(dep)
                    blocks[dep].append(t["task_id"])
                else:
                    dangling.append({"task_id": t["task_id"], "depends_on": dep})
            remaining[t["task_id"]] = deps

        layers: list[list[str]] = []
        while remaining:
            ready = [tid for tid in order if tid in remaining and not remaining[tid]]
            if not ready:
                break                       # 剩下的全在环里
            layers.append(ready)
            for tid in ready:
                remaining.pop(tid)
            for deps in remaining.values():
                deps.difference_update(ready)

        return {
            "layers": layers,
            "blocks": blocks,
            "cycle": [tid for tid in order if tid in remaining],
            "dangling": dangling,
        }

    def _all_plan_ids(self) -> list[str]:
        """库里全部 plan_id。只读，一条 ``SELECT``。

        借核心 store 的连接与锁，而不是自己 ``sqlite3.connect``：连接是共享的
        （``check_same_thread=False``），另开一条会绕过全仓唯一的写互斥。
        """
        conn: Any = getattr(self.store, "_conn", None)
        if conn is None or not hasattr(conn, "execute"):
            raise TypeError(
                f"{type(self.store).__name__} 没有暴露可执行的连接，列不出全部 plan。"
                " 把 plan_ids 显式传给 pending() 即可；不要为此去改冻结的 store.py。"
            )
        lock = getattr(self.store, "_lock", None) or contextlib.nullcontext()
        with lock:
            return [r[0] for r in
                    conn.execute("SELECT plan_id FROM plan ORDER BY created_at")]
