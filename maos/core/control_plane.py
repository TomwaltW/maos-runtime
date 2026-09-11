"""Control Plane —— 系统里唯一的状态权威。

铁律（这是整个架构成立的前提，实现时不要为了方便破例）：
  1. 任何组件都不直接写 task/plan 表，只能调这里的方法
  2. 任何状态迁移都要过 assert_transition()，非法迁移抛异常而不是静默改写
  3. 每个外部进来的事件都先过幂等闸门，重复投递直接短路返回
  4. 每次迁移都写一条 event_log，这是 Trace 和审计的唯一来源
"""

from __future__ import annotations

import copy
import logging
import os
from datetime import datetime, timezone
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
from maos.core.lease import LeaseBook, canon_iso, utc_now_iso
from maos.core.store import Store
from maos.runtime.hooks import (
    HOOK_VETO_REASON,
    TASK_COMPLETED,
    TASK_CREATED,
    HookRegistry,
    PlanVetoed,
)
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

# -- 认领租约（T107）-----------------------------------------------------
# 两个新的 `event_log.event_type`。**不进 `contracts/events.py`**（铁律 1）——
# 走 `append_event_log` 的自由 event_type 是仓库成文纪律，先例见
# `maos/agents/testing.py:50` 的 `ArtifactSeeded`、`maos/kb/retriever.py` 的
# `KbRetrieved`、`maos/config/audit.py` 的 `ConfigChanged`。

#: 一条 `TaskResult` 被前置校验判定为「不该处理」时落这一行。
#: 它是**唯一**说明「结果去哪了」的痕迹：丢弃发生在幂等闸之前，
#: 而幂等闸不落库，不写这一行的话，一条结果凭空消失且无迹可查。
STALE_RESULT_DROPPED = "StaleResultDropped"

#: 租约到期被回收时落一行，`reason` 写处置（retry / retry_exhausted /
#: claim_timeout / stale_lease）。它回答的是「这个任务为什么被放回队列」——
#: 同一跳的 `StateTransition` 只说得出状态怎么变，说不出触发者是超时。
LEASE_EXPIRED = "LeaseExpired"

#: `reap_expired_leases` 的四种处置。租约行本身不带处置，处置由**任务当前状态**决定。
REAP_RETRY = "retry"                      # RUNNING -> PENDING，还有重试额度
REAP_RETRY_EXHAUSTED = "retry_exhausted"  # RUNNING -> FAILED，额度已耗尽
REAP_CLAIM_TIMEOUT = "claim_timeout"      # DISPATCHED -> PENDING，没人认领
#: 任务已经往前走了（AWAITING_REVIEW / DONE / …），或它已被重规划取代（冻结）。
#: 只销租约、不迁移 —— 拿一条过期租约去动一个已经不归它管的任务，是在倒放历史。
REAP_STALE = "stale_lease"

#: 回收的**两个超时源**。租约表只登记「认领成功之后」的时钟
#: （唯一的登记点是 `claim` 里 `_transit(RUNNING)` 成功之后那一行），而
#: `DISPATCHED` 的任务按定义还没被认领、因此一行租约都没有。只认租约表的话
#: `REAP_CLAIM_TIMEOUT` 这条分支在真实链路上**根本不可达**：派了一个当时没有
#: 任何 Worker 承接的 role，任务会永久停在 DISPATCHED —— 无死信、无异常、无告警。
#: （改造前 `worker.py` 那条 `status=failed` 是响的，静默跳过把一个吵闹的坏换成了
#: 一个安静的坏；补上这个源才算把它换成「响一次然后自愈」。）
#: 所以派发那一侧要有自己的时钟，读的是 `task.updated_at` —— 进 DISPATCHED
#: 那一刻由 `update_task` 写下的时刻，不需要新增任何列。
REAP_SOURCE_LEASE = "lease"        # 认领之后失联：claim_lease 行到期
REAP_SOURCE_DISPATCH = "dispatch"  # 派发之后无人认领：从头到尾没有租约行

# -- 补偿 ----------------------------------------------------------------
ENV_SANDBOX_WORKDIR = "MAOS_SANDBOX_WORKDIR"

#: 补偿没做成时开出来的人工工单，挂在 FAILED 那一跳的 ``detail`` 上。
#: 键名收成一个常量：下游按它捞单（``HumanApprovalQueue.compensation_tickets()``），
#: 字面量各写一套就是同一条保证有两份实现 —— 改一处漏一处不会报错，只会静默漏捞
#: （理由同 ``AWAIT_HUMAN_DECISION`` 那一段）。
COMPENSATION_TICKET = "compensation_ticket"

#: 单号前缀，沿用退款域 ``refund.compensate`` 的 ``MT-<主键>`` 形状。逆补丁补偿与
#: 域内补偿开出来的工单长同一个样，人不用为两种补偿各记一套看法。
TICKET_PREFIX = "MT-"

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
                 replanner: Replanner | None = None,
                 leases: LeaseBook | None = None,
                 hooks: HookRegistry | None = None) -> None:
        self.store = store
        self.bus = bus
        # 认领租约（T107）。**缺省 None = 今天的路径逐字节不变**：不注入时
        # `claim` 不登记任何东西、`reap_expired_leases` 恒返回 0、`claim_lease`
        # 那张表连建都不建。注入之后才有「认领超时接管」这件事。
        #
        # 为什么是注入而不是内建：租约的 TTL 是一个与部署形态强相关的旋钮
        # （单机演示 300s，真集群另说），而控制面不该替部署做这个决定；
        # 更要紧的是「不注入即不改变行为」这条，它是本轨能安全落地的全部依据。
        self.leases = leases

        # 生命周期 hook（maos/runtime/hooks.py）。**缺省 None 时一次 fire 都不发生**
        # —— 不是「fire 了但没人订阅」，是那几行代码根本不执行：不注入就与本挂点
        # 出现之前逐字节相同（口径同 maos/config/audit.py 的「默认不接线」）。
        # 这条不是洁癖：能否决的挂点一旦默认接上，某个第三方回调抛异常就能改变
        # 全仓既有链路的行为，而那时谁都说不清是谁改的。
        self._hooks = hooks
        # 留痕是这个挂点契约的一半，不该靠调用方记得传参。HookRegistry 的 store 是
        # 可选的（它也要能在不接库的轻量装配里直接用），于是最自然的写法
        # `ControlPlane(store, bus, hooks=HookRegistry())` 不报错、也不留痕：
        # HookFailed / HookVetoed 全部静默丢失，挂点退化成 maos/ingress/router.py
        # 那种「只打日志」的观察者 —— 而控制面手里明明有 store，只是没绑上去。
        # 只在它还没有 store 时补：已经绑了另一本账是调用方的显式选择，不覆盖。
        if hooks is not None and hooks.store is None:
            hooks.store = store
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

        ## 生命周期挂点 ``TASK_CREATED``（注入了 ``hooks`` 才有）

        每个 task spec **落库前**开火，回调返回 ``Veto`` 就不建这一件。三处刻意：

        · **先把所有 spec 问一遍，再落第一条库。** 不是边问边建 —— 「全部被否决」
          那一档要求连 plan 行都不许留下（见下），而 plan 行在旧写法里是循环之前
          就插进去的。顺带也让回调看见的库状态是一致的（问第二件时第一件还没建），
          否则同一份 spec 列表的判定会取决于它在列表里的位置。
        · **全部被否决 -> 抛 ``PlanVetoed``，不建空 plan。** 空 plan 会立刻走到
          ``_advance`` 的「一条活任务都不剩」分支被收敛成 ``FAILED``，那把
          「治理拦下了这个计划」伪装成「计划执行失败」——两件事在 event_log 上
          必须分得开。判据写成「问过且一个都没留下」而不是「一个都没留下」：
          ``create_plan(tasks=[])`` 是既有合法调用（重规划返回空规格时走到这里），
          不许被一起判死。
        · **被否决的 task 若是别人的 ``depends_on`` 目标，不重连依赖。**
          照实建剩下的，``dispatch_ready`` 的 ``issubset(done)`` 于是永远不满足，
          依赖方停在 PENDING。这是有意的：自动跳过被否决的那一环去接线，等于系统
          替人判定「那一环可有可无」，而那恰恰是人刚刚否掉的东西。
        """
        plan_id = plan_id or E.new_id("plan")
        if self._hooks is not None:
            kept: list[dict] = []
            vetoed: list[dict] = []
            for t in tasks:
                # 交给回调的是**副本**，不是控制面手里的活对象 —— 回调只能表达
                # 否决，不能改写。原样传 `t` 的话，一条 `return None`（即明确放行、
                # 不否决）的回调就能把 effect_risk 从 H 改成 L 落库：on_review_verdict
                # 的 NEEDS_HUMAN_APPROVAL 分支与 HumanApprovalQueue.pending 的判据
                # 双双不再命中，不可逆产物无人放行地落 DONE，而 event_log 上一行痕迹
                # 都没有（HookVetoed / HookFailed 只在否决或抛异常时才落）。同一条
                # 回调往 depends_on 里塞一个不存在的 task_id，还能让任务永远停在
                # PENDING。那与「只认 Veto 一种否决形态、别的一律不算」自相矛盾：
                # 否决走不通的路，改字段反而走得通，且不留痕。
                veto = self._hooks.fire(
                    TASK_CREATED,
                    plan_id=plan_id, trace_id=trace_id, goal=goal,
                    role=t["role"], title=t["title"],
                    risk_level=t.get("risk_level", "L"),
                    effect_risk=t.get("effect_risk", "L"),
                    depends_on=list(t.get("depends_on", [])),
                    spec=copy.deepcopy(t),
                )
                if veto is None:
                    kept.append(t)
                    continue
                vetoed.append({"task_id": t.get("task_id"), "title": t["title"],
                               "reason": veto.reason})
                # 否决**为什么**发生已由 HookVetoed 记下；这一条记的是它**造成了什么**
                # ——「这个任务因此没被创建」。两条分开，是因为同一次否决在别的挂点上
                # 造成的后果不一样（TASK_COMPLETED 那边是转人工），后果不该压进 hook 层。
                self.store.append_event_log({
                    "trace_id": trace_id, "plan_id": plan_id,
                    "task_id": t.get("task_id"), "event_type": "TaskCreationVetoed",
                    "reason": veto.reason,
                    "detail": {"title": t["title"], "role": t["role"],
                               "hook_event": TASK_CREATED},
                })
                log.warning("[%s] 任务「%s」被 hook 否决，不创建：%s",
                            plan_id, t["title"], veto.reason)
            if tasks and not kept:
                raise PlanVetoed(plan_id, vetoed)
            tasks = kept
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

        🔴 **回归守卫：这个顺序看起来违反铁律 3，它不违反 —— 理由就在上面两段。**
        下一个读到这里的人很可能「顺手把幂等闸挪回最前面」，那是本模块最常见的写法，
        且挪完之后**全部测试照样绿**（现有用例走的都是 dispatch 已发出的正常时序）。
        要调回去之前，先构造出「Worker 抢在 dispatch 之前认领」那条路径，
        并证明它不会把任务永久卡死在 DISPATCHED —— 构造不出来，就不要动这个顺序。

        **租约登记排在 `_transit` 之后**（T107），这个位置同样不是随手放的：
        认领没成功就登记租约，等于给一个没人在做的任务挂上「有人在做」的牌子，
        `claimable()` 于是把它从任务板上藏起来，直到 TTL 到期才放出来。
        那是凭空多出来的一段停摆，而且看起来像是任务板坏了。
        """
        task = self.store.get_task(task_id)
        if task["state"] != TaskState.DISPATCHED:
            log.warning("[%s] 状态是 %s，不可认领", task_id, task["state"])
            return None
        key = f"claim:{task_id}:{attempt}"
        if self.store.claim_idempotency(key, "claim", task_id) is not None:
            log.info("[%s] 重复认领，忽略", task_id)
            return None
        task = self._transit(task, TaskState.RUNNING, worker_id=worker_id)
        if self.leases is not None:
            self.leases.grant(task_id, attempt, worker_id, now_iso=utc_now_iso())
        return task

    # ------------------------------------------------------------------
    # 事件回调：TaskResult
    # ------------------------------------------------------------------
    def on_task_result(self, env: Envelope) -> None:
        """Worker 交回结果。**两条只读前置校验排在幂等闸之前**，理由同 `claim`。

        校验的是「这条结果该不该被当成这一跳的答案」：

          · `env.attempt != task["attempt"]` —— 上一轮的迟到结果。任务早已重派，
            照单处理会拿旧答案覆盖新一轮的状态。
          · 任务在 RUNNING 且已有认领方，而交回者不是它 —— 旁观者的结果。
            改造前 `worker.py:45-47` 正是这个形态：一个 role 不在自己池里的 Worker
            会回一条 `failed`，于是任务被推回 PENDING，**而真正的认领方稍后交回的
            结果会被幂等闸当成重复投递丢掉**（键已被那条假失败烧掉）。
            今天不咬人只因为所有 worker 都持全池；有了异构队友它就是活 bug。

        🔴 **必须排在幂等闸之前**，和 `claim` 是同一个道理：幂等键一旦消费就不回滚。
        让一条非法结果先烧掉 `result:<task_id>:<attempt>`，这个 attempt 就再也交不回
        任何结果 —— 任务永久停在 RUNNING。**先校验、后消费**，被丢弃的那条从头到尾
        没消费任何东西，认领方稍后交回的合法结果照常被处理。

        `task is None` 时不校验、直接落到下面走老路径：那说明 task_id 根本不存在，
        与本次改造无关的另一种坏，交给原来的地方以原来的方式炸，不在这里改变行为。
        """
        errs = E.validate(env)
        if errs:
            raise ValueError(f"TaskResult 契约校验失败: {errs}")

        task = self.store.get_task(env.task_id)
        if task is not None and self._drop_stale_result(task, env):
            return

        if self.store.claim_idempotency(env.idempotency_key, "result", env.task_id) is not None:
            log.info("[%s] 重复 TaskResult，短路", env.task_id)
            return

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

        # 结果已交代，销租约（T107）。**只有走到这里的结果才销** —— 被前置校验
        # 丢弃的那些在上面就 return 了，销不着认领方的租约。这条很要紧：让一个
        # 旁观者的假失败销掉真正认领方的租约，等于把一个正在被执行的任务重新
        # 挂回任务板，于是同一份活被做两遍。
        #
        # 不销也不会错到失控（到期回收会按 REAP_STALE 只销不迁），但那要拖满一个
        # TTL，期间 claim_lease 表里堆着一批已经交代完的租约 —— 「哪些任务真的
        # 有人在做」这个问题从此答不准。
        if self.leases is not None:
            self.leases.release(env.task_id)
        self.store.finish_idempotency(env.idempotency_key, {"handled": True})

    def _drop_stale_result(self, task: dict, env: Envelope) -> bool:
        """两条只读前置校验。判定要丢弃就落一条 `StaleResultDropped` 并返回 True。

        **只读**是这个方法的全部安全性依据：它不改任何状态、不消费幂等键，
        所以「判错了」的代价上限是一条结果被丢（认领方会因租约到期被重新调度），
        而不是「一个 attempt 从此交不回结果」。

        worker 比对要求交回者**非空**才判负：`E.task_result` 的 `worker_id` 缺省是
        空串，历史上（以及任何不填它的调用方）都是这么发的。空串一律放行，
        判据只用来拦「明确是另一个 worker」这一种，不用来拦「没说自己是谁」——
        后者拦下去会让一批合法的旧调用方集体失声，而那才是真正的回归。
        """
        expected_attempt = task["attempt"]
        expected_worker = task.get("worker_id") or ""
        got_worker = (env.payload.get("worker_id") or "").strip()

        reason = ""
        if env.attempt != expected_attempt:
            reason = "stale_attempt"
        elif (task["state"] == TaskState.RUNNING and expected_worker and got_worker
                and got_worker != expected_worker):
            reason = "not_claimer"
        if not reason:
            return False

        log.warning("[%s] 丢弃 TaskResult（%s）：attempt %s/%s，worker %r/%r",
                    task["task_id"], reason, env.attempt, expected_attempt,
                    got_worker, expected_worker)
        self.store.append_event_log({
            "event_id": env.event_id,
            "trace_id": task["trace_id"],
            "plan_id": task["plan_id"],
            "task_id": task["task_id"],
            "event_type": STALE_RESULT_DROPPED,
            "reason": reason,
            "detail": {
                "expected_attempt": expected_attempt, "got_attempt": env.attempt,
                "expected_worker": expected_worker, "got_worker": got_worker,
                "task_state": task["state"], "status": env.payload.get("status"),
            },
        })
        return True

    # ------------------------------------------------------------------
    # 租约回收：`claim_timeout` 这条冻结迁移的第一个实现（T107）
    # ------------------------------------------------------------------
    def reap_expired_leases(self, *, now_iso: str) -> int:
        """回收超时的任务，按任务当前状态走**既有迁移**。返回处置条数。

        **两个超时源**（见 `REAP_SOURCE_LEASE` / `REAP_SOURCE_DISPATCH`）：

          1. `claim_lease` 行到期 —— 认领成功之后失联；
          2. 任务停在 DISPATCHED 且**没有租约行**、进 DISPATCHED 已超过 TTL ——
             派发出去从头到尾没人认领。只有源 1 的话这一支不可达，而它正是
             `worker.py` 那条静默跳过的兜底。

        处置表（一条新状态、一条新迁移都没加 —— 铁律 1 / 铁律 9）：

        | 任务状态 | 处置 | 迁移 |
        | :-- | :-- | :-- |
        | 已被重规划冻结（任何状态） | 不归它管，只销租约 | 无 |
        | RUNNING，attempt < max_attempts | 放回队列重派 | `RUNNING -> PENDING` (`retry`) |
        | RUNNING，attempt >= max_attempts | 判死并连坐 plan | `RUNNING -> FAILED` (`retry_exhausted`) |
        | DISPATCHED | 没人认领，重投 | `DISPATCHED -> PENDING` (`claim_timeout`) |
        | 其余 | 租约是陈迹，只销不迁 | 无 |

        ⚠️ **「没人能干的 role」只能被无限重投，杀不掉。** 冻结迁移表里
        `DISPATCHED -> FAILED` 与 `PENDING -> FAILED` 都不存在，于是一个全队伍
        都不承接的 role 会在 DISPATCHED/PENDING 之间按 TTL 慢速弹跳，plan 永远
        RUNNING。这比改造前的「静默永久停摆」好在它**响**（每轮一条
        `LeaseExpired`，reason=claim_timeout，detail.source=dispatch，可告警），
        但要真正判死它需要往冻结迁移表里加一条 —— 那要人拍板，已记 BACKLOG。

        `now_iso` 是**必填关键字**，本方法自己不取时钟：过期回收是一个「时间到了
        就动状态」的动作，时钟藏在里面的话，测试要验它就只能 sleep 真实的 TTL。

        `dispatch_ready` 攒到最后按 plan 调一次，不在循环里逐条调：同一个 plan 的
        两条租约同时到期时，循环内调用会在第一条刚回到 PENDING 时就把它派出去，
        第二条还没处置完 —— 派发看到的是一份处置到一半的任务表。

        🔴 **那次派发挂在 `finally` 上**，不是挂在循环之后：循环里任何一条租约把
        异常抛出来，都会连累**同一批里已经合法回到 PENDING 的任务** —— 它们的租约
        已经在循环里销掉了，而 `claim_lease` 表是回收唯一的入口，没有任何机制会再
        碰它们第二次。于是「一条坏租约」变成「一整批任务静默停摆」。异常照旧往外抛
        （非法迁移说明有代码绕过了状态机，不许 catch 掉），但**已经做完的处置必须
        兑现**。
        """
        if self.leases is None:
            return 0

        handled = 0
        touched: list[str] = []                    # 保序去重，让派发顺序可复现

        def _mark(plan_id: str | None) -> None:
            if plan_id is not None and plan_id not in touched:
                touched.append(plan_id)

        try:
            for lease in self.leases.expired(now_iso):
                task = self.store.get_task(lease["task_id"])
                self.leases.release(lease["task_id"])
                handled += 1
                if task is None:
                    # 任务没了租约还在。写不了 event_log（plan_id 无从得知），
                    # 销掉租约就是全部能做的事。
                    log.warning("[%s] 租约到期但任务不存在，只销租约", lease["task_id"])
                    continue
                _mark(self._reap_one(task, lease))

            # 第二个超时源：派发出去始终没人认领的任务。它们**没有租约行**，
            # 上面那个循环永远看不到它们（见 REAP_SOURCE_DISPATCH 的注释）。
            # 放在租约循环之后查，是为了不跟它撞车：这一查要求「没有租约行」，
            # 而刚被回收的那些任务此刻已不是 DISPATCHED，两个源天然不重叠。
            for task, pseudo in self._unclaimed_dispatched(now_iso):
                handled += 1
                _mark(self._reap_one(task, pseudo, source=REAP_SOURCE_DISPATCH))
        finally:
            for plan_id in touched:
                self.dispatch_ready(plan_id)
        return handled

    def _unclaimed_dispatched(self, now_iso: str) -> list[tuple[dict, dict]]:
        """派发后超时仍无人认领的任务，配一条**合成的**租约行给下游复用。

        合成行的 `worker_id` 是空串 —— 这正是这一支与 `REAP_SOURCE_LEASE` 的
        全部差别：没有任何 worker 碰过它，「谁失联了」这个问题没有答案。
        `granted_at` 取任务进 DISPATCHED 的时刻，`expires_at` 取那个时刻 + TTL，
        于是 `LeaseExpired` 那行 detail 的形状两个源完全一致，下游不用分两种读法。
        """
        book = self.leases
        out: list[tuple[dict, dict]] = []
        for task, deadline in book.unclaimed_dispatched(now_iso):
            out.append((task, {
                "task_id": task["task_id"], "attempt": task["attempt"],
                "worker_id": "", "granted_at": canon_iso(task["updated_at"]),
                "expires_at": deadline,
            }))
        return out

    def _reap_one(self, task: dict, lease: dict, *,
                  source: str = REAP_SOURCE_LEASE) -> str | None:
        """处置一条到期租约。返回需要重新派发的 plan_id，不需要则 None。"""
        state = task["state"]
        if source == REAP_SOURCE_DISPATCH:
            last_error = (f"claim_timeout: 派发后无人认领 "
                          f"attempt={lease['attempt']} dispatched_at={lease['granted_at']} "
                          f"deadline={lease['expires_at']}")
        else:
            last_error = (f"lease_expired: worker={lease['worker_id']} "
                          f"attempt={lease['attempt']} expires_at={lease['expires_at']}")

        if _is_frozen(task):
            # 🔴 **冻结判据必须排在状态判断之前。** 被重规划取代的任务
            # （`last_error == FROZEN_BY_REPLAN`）可以停在 RUNNING 或 DISPATCHED 上
            # 且租约还在（`_apply_replan` 只打标、不动状态、不销租约）。落到下面那两支
            # 的话，`_transit` 会把 `last_error` 覆写成 lease_expired —— 而
            # `_is_frozen` 的**唯一**判据就是这个字段：任务当场解冻，本方法末尾的
            # `dispatch_ready` 立刻把它重新派出去。一个已被重规划明确淘汰的任务
            # 于是复活并再执行一遍（补丁再打一遍）。
            # 处置与「任务已经往前走了」同一个口径：只销租约，不迁移。
            disposition, dst = REAP_STALE, None
        elif state == TaskState.RUNNING:
            if task["attempt"] >= task["max_attempts"]:
                disposition, dst = REAP_RETRY_EXHAUSTED, TaskState.FAILED
            else:
                disposition, dst = REAP_RETRY, TaskState.PENDING
            self._transit(task, dst, last_error=last_error)
        elif state == TaskState.DISPATCHED:
            disposition, dst = REAP_CLAIM_TIMEOUT, TaskState.PENDING
            self._transit(task, dst, last_error=last_error)
        else:
            disposition, dst = REAP_STALE, None

        self.store.append_event_log({
            "trace_id": task["trace_id"], "plan_id": task["plan_id"],
            "task_id": task["task_id"], "event_type": LEASE_EXPIRED,
            "from_state": state, "to_state": dst, "reason": disposition,
            "detail": {"worker_id": lease["worker_id"], "attempt": lease["attempt"],
                       "granted_at": lease["granted_at"],
                       "expires_at": lease["expires_at"],
                       "max_attempts": task["max_attempts"],
                       "source": source},
        })

        if disposition == REAP_RETRY_EXHAUSTED:
            # 与 on_task_result 的 failed 分支同一个口径：任务判死，plan 跟着死。
            #
            # 🔴 **plan 已经不在 RUNNING 上就别再判一次。** 同一个 plan 里两条租约
            # 同时耗尽额度时，第一条已经把 plan 迁到 FAILED，第二条再来一次就是
            # `FAILED -> FAILED` —— 那不在 `PLAN_TRANSITIONS` 里，会抛
            # `IllegalTransition` 打断整批回收。RUNNING 是通往 FAILED 的**唯一**
            # 合法来源（`states.py:56`），所以判据就写成它，不写「不是终态」——
            # 后者会把 PENDING 那种同样非法的来源放过去。
            plan = self.store.get_plan(task["plan_id"])
            if plan is not None and plan["state"] == PlanState.RUNNING:
                self._fail_plan(task["plan_id"])
            else:
                log.info("[%s] plan 已是 %s，不再重复判死",
                         task["plan_id"], None if plan is None else plan["state"])
            return None
        return task["plan_id"] if dst == TaskState.PENDING else None

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
    # 🔴 回归守卫：下面 rework 分支里四条止损（第三出口 `_human_exit` / `max_attempts` /
    # `_should_replan` / `_max_replan`）的**相对顺序是判定的一部分，不是代码风格**。
    # 那串 if/elif 看起来可以随便重排、也看起来可以再挂一条 —— 两件事都不成立：
    #
    #   · 重排会静默改变判定。第三出口排到 max_attempts 后面，最后一轮仍然 FAILED，
    #     等于白改，而这一单买的正是「少重发那两次」；replan 的上限判定排到
    #     `_should_replan` 前面，则任务此刻还在 AWAITING_REVIEW，转人工那条迁移走不通。
    #     顺序错了不会抛异常，只会让某个出口永远轮不到 —— 测试也未必红。
    #   · **加第五条之前，先证明现有四条里是哪一条没抓住这个案例。**
    #     说不出是哪一条，就不该有第五条 —— 那说明你要修的不是止损不够，
    #     是某一条的判据写窄了，加一条只会让这张顺序表更难读、更容易被下一个人重排。
    #
    # 顺序的理由此前只写在**其中一条**（第三出口）的注释里，这条禁令是把它提到入口。
    # 出处 docs/refs/cumora-coordination.md §3 #6（cumora 的 `HARD_LOOP_CAP` 同款形态：
    # 「这条兜底被以『AI 原生的优雅』为名删过两次，两次都回归了 —— 不要删」）。
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
                # 生命周期挂点 TASK_COMPLETED：**落 DONE 之前**开火，注入了 hooks 才有。
                # 回调返回 Veto 就不落 DONE，改走既有转人工出口 —— 复用
                # _escalate_to_human 而不是自己再写一次 _transit：那个方法是
                # 「机器已经没有别的招了」的唯一出口（见其 docstring），hook 否决
                # 完成正属于这一类，而唯一出口意味着 `await` 标记只写一处、
                # HumanApprovalQueue.pending 只需认一个字面量。转人工而捞不到人，
                # 比直接 FAILED 更糟（gate.py::HumanApprovalQueue.pending 原话）。
                # 不新增状态、不新增迁移（铁律 1/9）：走既有的
                # AWAITING_REVIEW -> BLOCKED("gate_needs_human")。
                veto = None
                if self._hooks is not None:
                    # gate_results 同样传副本：上面那行 `detail` 与这里取的是
                    # env.payload 里**同一个** dict 对象。原样传进去，一条
                    # `return None` 的回调就能改写它，而写进 DONE 那一跳 detail 的
                    # 正是被改过的值 —— 闸实际发来的结果永久丢失，审计链上留下一个
                    # 从未发生过的闸结果。detail 那边继续持原对象即可：它记的就是
                    # 闸发来的东西，不该被挂点碰到。
                    veto = self._hooks.fire(
                        TASK_COMPLETED,
                        task_id=task["task_id"], plan_id=task["plan_id"],
                        trace_id=task["trace_id"], event_id=env.event_id,
                        attempt=env.attempt, role=task["role"],
                        gate_results=copy.deepcopy(env.payload.get("gate_results", {})),
                        artifact_count=len(self.store.list_artifacts(task["task_id"])),
                    )
                if veto is not None:
                    log.warning("[%s] 完成被 hook 否决，转人工：%s",
                                task["task_id"], veto.reason)
                    # findings 传任务行上现有的那份，不是空列表：它是 _transit 的
                    # 写入字段，传 [] 会把前几轮返工攒下的 finding 抹掉 —— 而人正是
                    # 要看着它们做决定的。
                    self._escalate_to_human(
                        task, event_id=env.event_id, findings=task["findings"],
                        detail=detail, reason=HOOK_VETO_REASON,
                        hook_reason=veto.reason)
                    # plan 状态不动 —— 同 _escalate_to_human 的既有语义。
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
                if self._replan_used(task["plan_id"]) >= self._max_replan(task["plan_id"]):
                    # 上限到了就停，转人工 —— **绝不自旋**。无限重试是评委点名的反模式，
                    # 而「再规划一次说不定就好了」正是自旋最常见的伪装。
                    # 迁移走既有的 AWAITING_REVIEW->BLOCKED("gate_needs_human")：
                    # 此刻任务还在 AWAITING_REVIEW，先返工再转人工是走不通的
                    # （PENDING->BLOCKED 不在迁移表里），顺序不能倒。
                    log.warning("[%s] 重规划已达上限 %d，转人工处置",
                                task["plan_id"], self._max_replan(task["plan_id"]))
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

    def _max_replan(self, plan_id: str | None = None) -> int:
        """这个 plan 还许重规划几次 = `min(MAOS_MAX_REPLAN, 建议的 retry_budget)`。

        env 那一半：默认 2，非法值回退默认并告警，不让配置笔误变成自旋。
        走 `maos.config` 的配置面而不是直接读 `os.environ`（T28）：缺省源就是
        `os.environ.get`，取值逐字节不变。

        建议那一半（T119）：`plan_id` 给了就读它最近一条 `PlanAdvised` 的
        `retry_budget`，取**更小**的那个。只许更紧不许更松 —— 知识层可以说
        「这个组合已经栽过 N 次，别再自旋了」，不能说「再多试几次说不定就好了」。
        护栏 4（`guardrails.assert_advice_within_bounds`）在建议进事件之前就拦住
        放宽的那一侧，这里的 `min` 是第二道：事件是可以被别处写进去的，
        控制面不拿 event_log 里读来的一个数当上限用。

        `plan_id=None` 时逐字节等于 T119 之前的行为（`test_config_source.py::
        test_max_replan_matches_legacy_byte_for_byte` 按无参调用钉着这条）。
        """
        raw = get_config_source().get(ENV_MAX_REPLAN, "").strip()
        if not raw:
            env_budget = DEFAULT_MAX_REPLAN
        else:
            try:
                env_budget = max(int(raw), 0)
            except ValueError:
                log.warning("%s=%r 不是整数，回退默认 %d",
                            ENV_MAX_REPLAN, raw, DEFAULT_MAX_REPLAN)
                env_budget = DEFAULT_MAX_REPLAN
        if not plan_id:
            return env_budget

        from maos.kb.plan_advice import latest_advice
        advised = latest_advice(self.store, plan_id) or {}
        try:
            budget = int(advised["retry_budget"])
        except (KeyError, TypeError, ValueError):
            return env_budget
        return max(0, min(env_budget, budget))

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

        ticket = None
        if not approved:
            # 先回滚再改状态（phase-4.md:20 的顺序）：状态一旦落 FAILED，
            # 「这个任务的产物还在外面」这件事就没人记得了。
            outcome = self._execute_compensation(task, operator=operator, note=note)
            # 回滚**没做成**时不许就这么过去。返回值原先被整个丢弃：事件里那句
            # ``ok=false`` 如实记着，却没有任何人读它 —— 状态照样落 FAILED，
            # 屏幕上一片正常，而产物还在外面。这一支把它变成有人认领的一件事。
            ticket = self._open_compensation_ticket(
                task, outcome, operator=operator, note=note)
        detail = {"operator": operator, "note": note}
        if ticket is not None:
            detail[COMPENSATION_TICKET] = ticket
        self._transit(task, dst, detail=detail)
        self.store.finish_idempotency(key, {"approved": approved, "operator": operator})
        if approved:
            self._advance(task["plan_id"])
        else:
            self._fail_plan(task["plan_id"])

    # ------------------------------------------------------------------
    def _open_compensation_ticket(self, task: dict, outcome: dict | None, *,
                                  operator: str, note: str) -> dict | None:
        """回滚没做成 -> 开一张人工工单。返回 None 表示这一次不该开单。

        三种入参对应三种处置，**只有第三种开单**：

          · ``None`` —— 这个任务压根没有补偿引用（低风险产物，没有要还原的东西）。
            绝不能在这里开单：每一次驳回都会走到这里，而低风险任务占绝大多数，
            开了就是给工单队列灌噪音，真有事的那一条反而被淹掉。
          · ``ok=True`` —— 反向应用真做成了，产物已经不在外面，没有要人做的事。
          · ``ok=False`` —— 补丁对不上 / workdir 不是仓库 / 沙箱不可用：
            **产物还在外面**，而本系统已经没有别的招了。

        **不加新事件类型**（events.py 是冻结契约，铁律 1）：失败这件事早就留痕了，
        ``CompensationExecuted.detail`` 里本来就有 ``ok`` 和 ``error``。这里补的不是
        留痕，是**处置**。工单因此挂在 FAILED 那一跳的 ``detail`` 上，与状态迁移
        同一条记录 —— 问「这个任务为什么完了」时一眼就看见「还有一次回滚没成功」。

        **不加新状态**（铁律 9）：补偿失败不是 Task 的一个状态，它是外部世界里
        一件没收干净的事。任务该落 FAILED 还是落 FAILED。

        **不重试**：``git apply -R`` 打不上的几个原因（补丁对不上、目录不对、沙箱
        不可用）没有一个会因为再打一遍就变了，重试只会让人晚知道。所以这里只叫人，
        并且在日志里用人话说一遍 —— 只落进库里没人看得见，那和没人管差别不大。
        """
        if outcome is None or outcome.get("ok"):
            return None

        error = outcome.get("error") or {}
        workdir = outcome.get("workdir")
        ticket = {
            "ticket_id": f"{TICKET_PREFIX}{task['task_id']}",
            # 驳回的人就是此刻最清楚上下文的人，缺省派给他；换人是人自己的事。
            "assignee": operator,
            "plan_id": task["plan_id"],
            "task_id": task["task_id"],
            "title": task["title"],
            "reason": note,
            "workdir": workdir,
            # error 原样抄进来，不改写、不归纳：stage/path/hunk/message 是沙箱
            # 如实报的，人工对账要的就是这四格（同 refund.compensate 的口径）。
            "error": error,
            "todo": [
                f"到 {workdir} 核对这个任务的产物是不是还在 —— 反向应用没打上",
                "手工还原，或确认无需还原",
                "确认之后关单；本系统不会自己再试一次",
            ],
            "opened_at": datetime.now(timezone.utc).isoformat(),
        }
        log.error("[%s] 回滚没成功（%s：%s）—— 产物可能还留在 %s，"
                  "已开人工工单 %s，等人处理",
                  task["task_id"], error.get("stage"), error.get("message"),
                  workdir, ticket["ticket_id"])
        return ticket

    # ------------------------------------------------------------------
    # 补偿执行器：零模型 —— 逆补丁不生成，只把正向补丁反着打一遍
    # ------------------------------------------------------------------
    def _execute_compensation(self, task: dict, *, operator: str, note: str = "") -> dict | None:
        """读补偿引用 -> 取回正向补丁 -> 沙箱反向应用 -> 落 CompensationExecuted。

        返回 None 表示这个任务压根没有补偿引用（低风险产物，没有要还原的东西）；
        否则返回 sandbox_git_apply 的结果，另附一个 ``workdir``：失败时要人去哪儿
        看产物是工单上最要紧的一格，而调用方读不到这里的 env —— 让它自己再读一遍
        就有了第二份事实，env 中途被改过时两处会指向两个目录。

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
        return {**result, "workdir": workdir}

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
