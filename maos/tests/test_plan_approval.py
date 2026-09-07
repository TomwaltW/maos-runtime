"""计划审批：批准才许开跑，驳回带反馈退回重提（T111）。

## 这个文件在证明什么

改造前，「PENDING」这个 Plan 状态在生产路径上**从来没有停留过一个瞬间** ——
所有场景 `create_plan` 之后立刻 `start_plan`，人没有插进去的缝。
本文件证明的是那条缝被补上了，且**一行内核都没改**（最后一节的静态守卫就是
这句话的机器判据）。

## 三条最容易被改坏的断言，改代码前先看清它们在守什么

1. `test_reject_leaves_the_plan_pending_for_a_second_look` —— **驳回不 start**。
   重规划完就自己跑起来的话，人的那次驳回等于没发生：他驳的是方案，拿到的却是
   「方案改了并且已经在跑了」。这条断言钉的是**状态没动**，不是「新规格应用了」。

2. `test_a_plan_being_replanned_is_not_waiting_for_approval` —— **重规划中的 plan
   不是待审批的 plan**。`replan` 会把跑起来过的 plan 退回 PENDING；只按 state 捞，
   人会被叫去批一个已经开跑过的计划，而他一按批准，重规划期间本不该派发的任务
   就全发出去了。

3. `test_the_idempotency_key_carries_the_round` —— **幂等键必须带轮次**。
   键写成 `plan_approval:<plan_id>`（像 `human:<task_id>` 那样）的话，
   驳回之后的第二轮审批会被当成重复投递当场短路，人再也批不动这个计划，
   而且一声不吭。这条断言是唯一拦得住那次「顺手简化」的东西。
"""

from __future__ import annotations

import ast
import logging
import pathlib

import pytest

from maos.contracts.events import Topic
from maos.contracts.states import PlanState, TaskState
from maos.core.control_plane import FROZEN_BY_REPLAN, SCOPE_PLAN, ControlPlane
from maos.core.eventbus import InMemoryEventBus
from maos.core.store import SqliteStore
from maos.obs.trace import stray_events
from maos.runtime.plan_approval import (
    DEFAULT_MAX_PLAN_REJECT,
    ENV_MAX_PLAN_REJECT,
    EV_APPROVED,
    EV_EXHAUSTED,
    EV_REJECTED,
    EV_REPLANNED,
    IDEMPOTENCY_OP,
    REJECT_GATE,
    PlanApprovalQueue,
    PlanNotAwaitingApproval,
)

TRACE = "trace-t111"
GOAL = "把这笔退款走通"

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
MAOS_PKG = REPO_ROOT / "maos"


# ======================================================================
# 夹具
# ======================================================================
def _build(path: str = ":memory:"):
    store = SqliteStore(path)
    store.init_schema()
    bus = InMemoryEventBus()
    cp = ControlPlane(store, bus)
    return store, bus, cp, PlanApprovalQueue(store, cp)


#: 两个任务、后一个依赖前一个 —— 拓扑投影与「新规格逐位覆写」都要它才验得出来。
def _specs_v1() -> list[dict]:
    return [
        {"role": "coding", "title": "方案甲：直接调网关", "inputs": {"v": 1},
         "acceptance": ["退款到账"], "risk_level": "M", "effect_risk": "H"},
        {"role": "testing", "title": "验一遍", "inputs": {}, "acceptance": ["有回执"]},
    ]


def _make_plan(cp: ControlPlane, tasks: list[dict] | None = None) -> str:
    """建计划但**不** start —— 这一行就是本轨补上的那条缝。"""
    plan_id = cp.create_plan(goal=GOAL, trace_id=TRACE, tasks=tasks or _specs_v1())
    tasks_rows = cp.store.list_tasks(plan_id)
    cp.store.update_task(tasks_rows[1]["task_id"],
                         depends_on=[tasks_rows[0]["task_id"]])
    return plan_id


class _Replanner:
    """记账用的 replanner：它被调了几次、拿到了什么，都留在实例上。"""

    def __init__(self, specs: list[dict] | None = None) -> None:
        self.specs = specs if specs is not None else [
            {"role": "reviewer", "title": "方案乙：先查再退", "inputs": {"v": 2},
             "acceptance": ["先查后退"]},
        ]
        self.calls: list[dict] = []

    def __call__(self, *, goal, findings, open_tasks):
        self.calls.append({"goal": goal, "findings": list(findings),
                           "open_tasks": list(open_tasks)})
        return [dict(s) for s in self.specs]


def _events(store, plan_id, event_type) -> list[dict]:
    return [e for e in store.list_event_log(plan_id) if e["event_type"] == event_type]


def _plan_transitions(store, plan_id) -> list[tuple[str, str]]:
    return [(e["from_state"], e["to_state"]) for e in store.list_event_log(plan_id)
            if e["event_type"] == "PlanTransition"]


# ======================================================================
# 1. 停靠点本身：建了但没跑的计划，有人捞得到
# ======================================================================
def test_a_plan_that_was_created_but_never_started_is_waiting_for_a_human():
    """`create_plan` 之后不 `start_plan`，`pending()` 就捞得到它。

    这是整条链路的前提：捞不到的话，人根本不知道有个计划在等他 —— 而改造前
    这个状态一个瞬间都不存在，所以这条断言同时是「那条缝真的补上了」的判据。
    """
    store, _bus, cp, queue = _build()
    plan_id = _make_plan(cp)

    assert store.get_plan(plan_id)["state"] == PlanState.PENDING
    assert [p["plan_id"] for p in queue.pending()] == [plan_id]
    assert queue.preview(plan_id)["awaiting"] is True
    assert _plan_transitions(store, plan_id) == [], "一次迁移都不该发生 —— 它还没被批"


def test_pending_can_also_be_asked_about_a_known_set_of_plans():
    """缺省去库里枚举，也允许把 plan_id 显式传进来（换后端时的出口）。"""
    store, _bus, cp, queue = _build()
    waiting = _make_plan(cp)
    running = _make_plan(cp)
    cp.start_plan(running)

    assert [p["plan_id"] for p in queue.pending([waiting, running])] == [waiting]
    assert queue.pending([running]) == []
    assert queue.pending(["plan_does_not_exist"]) == [], "问一个不存在的 plan 不该炸"
    assert {p["plan_id"] for p in queue.pending()} == {waiting}


# ======================================================================
# 2. 批准：这才是开跑
# ======================================================================
def test_approve_starts_the_plan_and_dispatches_the_ready_tasks():
    """批准 -> PENDING->RUNNING -> 依赖满足的任务被派发出去。"""
    store, bus, cp, queue = _build()
    plan_id = _make_plan(cp)
    seen = []
    bus.subscribe(Topic.TASK_ASSIGNMENT, "t111-spy", seen.append)

    assert queue.approve(plan_id, "boss") is True
    bus.drain()

    assert store.get_plan(plan_id)["state"] == PlanState.RUNNING
    assert _plan_transitions(store, plan_id) == [(PlanState.PENDING, PlanState.RUNNING)]
    states = [t["state"] for t in store.list_tasks(plan_id)]
    assert states == [TaskState.DISPATCHED, TaskState.PENDING], \
        "第一个任务该派出去，第二个还等着依赖 —— 派发口径没变"
    assert [e.task_id for e in seen] == [store.list_tasks(plan_id)[0]["task_id"]]
    assert queue.pending() == [], "批过的计划不该还挂在待审批队列里"


def test_the_approval_event_is_written_before_the_plan_starts():
    """事件顺序即因果顺序：先有人批准，才有计划开跑。"""
    store, _bus, cp, queue = _build()
    plan_id = _make_plan(cp)
    queue.approve(plan_id, "boss")

    kinds = [e["event_type"] for e in store.list_event_log(plan_id)]
    assert kinds.index(EV_APPROVED) < kinds.index("PlanTransition")


# ======================================================================
# 3. 驳回：题眼 —— 改完方案仍然停在 PENDING
# ======================================================================
def test_reject_leaves_the_plan_pending_for_a_second_look():
    """🔴 本轨的题眼：驳回之后新规格应用上了，但 plan **仍是 PENDING**。

    这里要一起验两件事，缺一不可：
      · 规格真的换了（title / role / inputs / acceptance 逐位覆写）；
      · 状态**一次都没动** —— 一条 PlanTransition 都不许有。

    第二条才是驳回与「机器自己重规划」（`ControlPlane._replan`）的分界：
    那条路末尾会 `start_plan`，这条路不许。复用 `_replan` 就会踩中它。
    """
    store, _bus, cp, queue = _build()
    plan_id = _make_plan(cp)
    replanner = _Replanner()
    cp.set_replanner(replanner)
    before = store.list_tasks(plan_id)

    assert queue.reject(plan_id, "boss", "先查一遍再退，别直接打钱") is True

    plan = store.get_plan(plan_id)
    assert plan["state"] == PlanState.PENDING
    assert _plan_transitions(store, plan_id) == [], \
        "驳回不许触发任何 Plan 迁移 —— 跑起来了的话，人的驳回等于没发生"
    assert [p["plan_id"] for p in queue.pending()] == [plan_id], "要回到队列里等再看一次"

    after = store.list_tasks(plan_id)
    assert after[0]["task_id"] == before[0]["task_id"], "逐位覆写，task_id 不许换"
    assert (after[0]["role"], after[0]["title"]) == ("reviewer", "方案乙：先查再退")
    assert after[0]["inputs"] == {"v": 2} and after[0]["acceptance"] == ["先查后退"]
    assert after[0]["state"] == TaskState.PENDING, "覆写不该把任务推进任何状态"
    assert after[1]["last_error"] == FROZEN_BY_REPLAN, \
        "新方案没给它安排活 —— 该冻结，口径由 _apply_replan 一家说了算"


def test_the_human_feedback_reaches_the_replanner_as_a_plan_scoped_finding():
    """人的意见被压成一条 plan 级 finding 喂进 replanner —— 他驳的是整个方案。"""
    store, _bus, cp, queue = _build()
    plan_id = _make_plan(cp)
    replanner = _Replanner()
    cp.set_replanner(replanner)

    queue.reject(plan_id, "boss", "验收标准没写清楚")

    assert len(replanner.calls) == 1
    call = replanner.calls[0]
    assert call["goal"] == GOAL, "重规划要看的是原目标，不是某一条任务"
    finding = call["findings"][0]
    assert finding["gate"] == REJECT_GATE and finding["scope"] == SCOPE_PLAN
    assert finding["severity"] == "blocker" and finding["message"] == "验收标准没写清楚"
    assert finding["operator"] == "boss" and finding["id"] == "plan-reject-0"
    assert [t["task_id"] for t in call["open_tasks"]] == \
        [t["task_id"] for t in store.list_tasks(plan_id)], "未完成的任务全都要交出去"


def test_reject_without_a_replanner_still_puts_the_feedback_on_the_record():
    """没接 replanner 不是故障：意见留痕、plan 停在 PENDING，但**不许**假称重规划过。"""
    store, _bus, cp, queue = _build()
    plan_id = _make_plan(cp)
    assert cp._replanner is None

    assert queue.reject(plan_id, "boss", "目标就写错了") is True

    assert store.get_plan(plan_id)["state"] == PlanState.PENDING
    assert len(_events(store, plan_id, EV_REJECTED)) == 1
    assert _events(store, plan_id, EV_REPLANNED) == [], \
        "什么都没重规划，落一条说重规划过了就是假绿"
    assert [t["title"] for t in store.list_tasks(plan_id)] == \
        [s["title"] for s in _specs_v1()], "没人重出规格，任务一个字都不该变"


# ======================================================================
# 4. 驳回上限：到顶了也不替人判死
# ======================================================================
def test_reject_stops_replanning_at_the_limit_and_still_leaves_the_plan_to_the_human():
    """默认上限 2：第 3 次驳回不再调 replanner，落 Exhausted，plan 仍 PENDING。

    「到上限就把计划判死」是这里最诱人的错误写法。plan 的死活由人说了算 ——
    口径同 `ControlPlane._escalate_to_human`：闸当场判死就是替人做了那个决定。
    """
    store, _bus, cp, queue = _build()
    plan_id = _make_plan(cp)
    replanner = _Replanner()
    cp.set_replanner(replanner)

    for i in range(3):
        assert queue.reject(plan_id, "boss", f"第 {i + 1} 次不行") is True

    assert len(replanner.calls) == DEFAULT_MAX_PLAN_REJECT == 2, \
        "第 3 次不许再调 replanner —— 该改的是目标，不是再改一次方案"
    assert len(_events(store, plan_id, EV_REJECTED)) == 3, "人确实驳了 3 次，3 次都要留痕"
    assert len(_events(store, plan_id, EV_REPLANNED)) == 2

    exhausted = _events(store, plan_id, EV_EXHAUSTED)
    assert len(exhausted) == 1
    assert exhausted[0]["detail"] == {"rejects": 3, "limit": 2}

    assert store.get_plan(plan_id)["state"] == PlanState.PENDING, "到上限也不自动判死"
    assert [p["plan_id"] for p in queue.pending()] == [plan_id]
    assert queue.preview(plan_id)["exhausted"] is True, "但人得看得见它到顶了"


def test_the_limit_comes_from_its_own_knob_not_from_max_replan(monkeypatch):
    """上限读 `MAOS_MAX_PLAN_REJECT`，且**不**跟着 `MAOS_MAX_REPLAN` 走。

    两件事：那个管「机器自己触发的重规划」，这个管「人驳回了几次」。
    共用一个旋钮的后果是调其中一个把另一个也调了，而调的人不会知道。
    """
    monkeypatch.setenv(ENV_MAX_PLAN_REJECT, "1")
    monkeypatch.setenv("MAOS_MAX_REPLAN", "9")
    store, _bus, cp, queue = _build()
    plan_id = _make_plan(cp)
    replanner = _Replanner()
    cp.set_replanner(replanner)

    queue.reject(plan_id, "boss", "一")
    queue.reject(plan_id, "boss", "二")

    assert len(replanner.calls) == 1, "上限是 1，第 2 次就该停 —— 没有跟着 MAX_REPLAN=9 走"
    assert _events(store, plan_id, EV_EXHAUSTED)[0]["detail"] == {"rejects": 2, "limit": 1}


def test_a_bad_limit_falls_back_to_the_default_with_a_warning(monkeypatch, caplog):
    """非法值回退默认并告警，**不抛** —— 一个配置笔误不该让审批彻底做不了。"""
    monkeypatch.setenv(ENV_MAX_PLAN_REJECT, "两次")
    store, _bus, cp, queue = _build()
    plan_id = _make_plan(cp)
    cp.set_replanner(_Replanner())

    with caplog.at_level(logging.WARNING, logger="maos.plan_approval"):
        assert queue.preview(plan_id)["reject_limit"] == DEFAULT_MAX_PLAN_REJECT
    assert any(ENV_MAX_PLAN_REJECT in r.getMessage() for r in caplog.records), \
        "回退了却不吭声的话，配置笔误会一直活着"

    for i in range(3):
        queue.reject(plan_id, "boss", str(i))
    assert len(_events(store, plan_id, EV_EXHAUSTED)) == 1, "回退到默认 2，第 3 次到顶"


def test_a_zero_limit_means_no_automatic_replan_at_all(monkeypatch):
    """上限 0 是个合法取值：驳回只留痕，一次都不自动重出规格。"""
    monkeypatch.setenv(ENV_MAX_PLAN_REJECT, "0")
    store, _bus, cp, queue = _build()
    plan_id = _make_plan(cp)
    replanner = _Replanner()
    cp.set_replanner(replanner)

    queue.reject(plan_id, "boss", "不行")

    assert replanner.calls == []
    assert _events(store, plan_id, EV_EXHAUSTED)[0]["detail"] == {"rejects": 1, "limit": 0}
    assert store.get_plan(plan_id)["state"] == PlanState.PENDING


# ======================================================================
# 5. 幂等：一个 plan 会被审批**多次**，键里不能只有 plan_id
# ======================================================================
def test_the_idempotency_key_carries_the_round():
    """🔴 驳回之后的第二轮审批必须批得动。

    键写成 `plan_approval:<plan_id>`（像 `human:<task_id>` 那样不带轮次）的话，
    第一轮驳回已经把它烧掉了，这里的 `approve` 会被当成重复投递短路 ——
    人再也批不动这个计划，而且一声不吭。
    """
    store, _bus, cp, queue = _build()
    plan_id = _make_plan(cp)
    cp.set_replanner(_Replanner())

    assert queue.reject(plan_id, "boss", "改一版") is True          # 烧掉 round 0
    assert queue.approve(plan_id, "boss") is True                   # 用的是 round 1
    assert store.get_plan(plan_id)["state"] == PlanState.RUNNING

    keys = [r[0] for r in store._conn.execute(
        "SELECT idempotency_key FROM processed_key WHERE op=? ORDER BY created_at",
        (IDEMPOTENCY_OP,))]
    assert keys == [f"plan_approval:{plan_id}:0", f"plan_approval:{plan_id}:1"], \
        "两轮审批必须落在两个不同的键上"


def test_a_racing_duplicate_approval_is_short_circuited_without_touching_state():
    """并发那一半：键已被同轮的另一个操作员消费掉，这一次直接短路。"""
    store, _bus, cp, queue = _build()
    plan_id = _make_plan(cp)
    # 模拟「另一个操作员抢先一步拿到了这一轮的键」
    assert store.claim_idempotency(f"plan_approval:{plan_id}:0",
                                   IDEMPOTENCY_OP, plan_id) is None

    assert queue.approve(plan_id, "boss") is False
    assert store.get_plan(plan_id)["state"] == PlanState.PENDING
    assert _plan_transitions(store, plan_id) == []
    assert _events(store, plan_id, EV_APPROVED) == []


def test_a_sequential_duplicate_approval_cannot_transition_twice():
    """顺序那一半由状态机挡：批过的计划再批一次当场抛，不许悄悄再跑一遍派发。

    分工同 `ControlPlane.human_decision` 的两道闸：状态校验挡顺序重复，
    幂等键挡并发。校验排在幂等闸**前面** —— 键一旦烧掉就不回滚，
    非法调用先把它烧了，合法的那次审批就再也过不去了。
    """
    store, _bus, cp, queue = _build()
    plan_id = _make_plan(cp)
    queue.approve(plan_id, "boss")

    with pytest.raises(PlanNotAwaitingApproval):
        queue.approve(plan_id, "boss")

    assert _plan_transitions(store, plan_id) == [(PlanState.PENDING, PlanState.RUNNING)]
    assert len(_events(store, plan_id, EV_APPROVED)) == 1


def test_an_illegal_call_does_not_burn_the_key():
    """非法调用不许消费掉这一轮的键 —— 烧了，这个 plan 此后就再也批不了。"""
    store, _bus, cp, queue = _build()
    plan_id = _make_plan(cp)
    cp.start_plan(plan_id)
    cp._transit_plan(plan_id, PlanState.PENDING)                    # 造一个重规划中的 plan

    with pytest.raises(PlanNotAwaitingApproval):
        queue.approve(plan_id, "boss")

    assert store._conn.execute(
        "SELECT COUNT(*) FROM processed_key WHERE op=?", (IDEMPOTENCY_OP,)
    ).fetchone()[0] == 0


# ======================================================================
# 6. 重规划中的 plan 不是待审批的 plan
# ======================================================================
def test_a_plan_being_replanned_is_not_waiting_for_approval():
    """🔴 跑起来过、又被 `replan` 退回 PENDING 的 plan，不许进待审批队列。

    只按 `state == PENDING` 捞的话，人会被叫去批一个其实已经开跑过的计划；
    而他一按批准，重规划期间本不该派发的任务就全发出去了。
    """
    store, _bus, cp, queue = _build()
    replanning = _make_plan(cp)
    cp.start_plan(replanning)
    cp._transit_plan(replanning, PlanState.PENDING)                 # states.py 的 replan 那条
    fresh = _make_plan(cp)

    assert store.get_plan(replanning)["state"] == PlanState.PENDING, "前提：它确实停在 PENDING"
    assert _plan_transitions(store, replanning) == [
        (PlanState.PENDING, PlanState.RUNNING), (PlanState.RUNNING, PlanState.PENDING)]

    assert [p["plan_id"] for p in queue.pending()] == [fresh], "只有没跑过的那个在等人"
    assert queue.preview(replanning)["awaiting"] is False

    with pytest.raises(PlanNotAwaitingApproval, match="重规划中"):
        queue.approve(replanning, "boss")
    with pytest.raises(PlanNotAwaitingApproval, match="重规划中"):
        queue.reject(replanning, "boss", "不行")


def test_a_finished_plan_is_neither_pending_nor_approvable():
    """终态的 plan 同理：谈不上审批。判据挂在冻结的迁移表上，不是硬写 PENDING。"""
    store, _bus, cp, queue = _build()
    plan_id = _make_plan(cp)
    cp.start_plan(plan_id)
    cp._transit_plan(plan_id, PlanState.FAILED)

    assert queue.pending() == []
    with pytest.raises(PlanNotAwaitingApproval):
        queue.approve(plan_id, "boss")


# ======================================================================
# 7. preview：只读投影
# ======================================================================
def test_preview_changes_nothing_at_all():
    """看一眼计划不该留下任何痕迹：plan / tasks / event_log 逐字节相同。"""
    store, _bus, cp, queue = _build()
    plan_id = _make_plan(cp)

    def _boom(**_kwargs):                       # 调模型就当场炸
        raise AssertionError("preview 不许调模型 —— 它是给人看的只读投影")

    cp.set_replanner(_boom)

    before = cp.snapshot(plan_id)
    queue.preview(plan_id)
    queue.preview(plan_id)
    assert cp.snapshot(plan_id) == before
    assert store.list_model_usage() == [], "更不许产生任何模型用量"


def test_preview_shows_what_a_human_needs_to_decide_on():
    """计划全貌 + 依赖拓扑：谁挡着谁、哪几批能并行。"""
    store, _bus, cp, queue = _build()
    plan_id = _make_plan(cp)
    first, second = store.list_tasks(plan_id)

    doc = queue.preview(plan_id)
    assert doc["goal"] == GOAL and doc["trace_id"] == TRACE
    assert doc["round"] == 0 and doc["reject_limit"] == DEFAULT_MAX_PLAN_REJECT
    assert doc["tasks"][0] == {
        "task_id": first["task_id"], "title": "方案甲：直接调网关", "role": "coding",
        "state": TaskState.PENDING, "risk_level": "M", "effect_risk": "H",
        "depends_on": [], "acceptance": ["退款到账"], "frozen": False,
    }
    assert doc["topology"]["layers"] == [[first["task_id"]], [second["task_id"]]]
    assert doc["topology"]["blocks"] == {first["task_id"]: [second["task_id"]],
                                         second["task_id"]: []}
    assert doc["topology"]["cycle"] == [] and doc["topology"]["dangling"] == []


def test_preview_names_the_broken_dependencies_instead_of_hiding_them():
    """环与悬空依赖不抛异常，如实列出来 —— 人工审批这道闸的价值就在这里。

    机器把这两类规划缺陷跑成「永远没有任务 ready」的静默停摆；人一眼就看得出。
    """
    _store, _bus, cp, queue = _build()
    plan_id = cp.create_plan(goal=GOAL, trace_id=TRACE, tasks=[
        {"role": "coding", "title": "甲", "task_id": "task_a", "depends_on": ["task_b"]},
        {"role": "coding", "title": "乙", "task_id": "task_b", "depends_on": ["task_a"]},
        {"role": "coding", "title": "丙", "task_id": "task_c", "depends_on": ["task_ghost"]},
    ])

    topo = queue.preview(plan_id)["topology"]
    assert topo["layers"] == [["task_c"]], "只有丙没有真依赖挡着"
    assert topo["cycle"] == ["task_a", "task_b"]
    assert topo["dangling"] == [{"task_id": "task_c", "depends_on": "task_ghost"}]


def test_preview_marks_the_tasks_frozen_by_a_replan():
    """重规划冻结掉的任务照列并标出来 —— 否则人会以为方案比实际更完整。"""
    store, _bus, cp, queue = _build()
    plan_id = _make_plan(cp)
    cp.set_replanner(_Replanner())
    queue.reject(plan_id, "boss", "换个方案")

    frozen = [t["frozen"] for t in queue.preview(plan_id)["tasks"]]
    assert frozen == [False, True]
    assert store.list_tasks(plan_id)[1]["last_error"] == FROZEN_BY_REPLAN


# ======================================================================
# 8. 事件：四种，形状固定，且都挂得上 span 树
# ======================================================================
def test_the_four_events_have_the_documented_detail_shape(tmp_path):
    store, _bus, cp, queue = _build(str(tmp_path / "t111.db"))
    cp.set_replanner(_Replanner())
    rejected = _make_plan(cp)
    approved = _make_plan(cp)

    for i in range(3):
        queue.reject(rejected, "boss", f"意见 {i}")
    queue.approve(approved, "boss")

    assert _events(store, approved, EV_APPROVED)[0]["detail"] == {
        "operator": "boss", "tasks": 2, "round": 0}
    assert [e["detail"] for e in _events(store, rejected, EV_REJECTED)] == [
        {"operator": "boss", "feedback": f"意见 {i}", "round": i} for i in range(3)]
    assert _events(store, rejected, EV_REPLANNED)[0]["detail"] == {
        "new_specs": 1, "open_tasks": 2, "round": 0}
    assert _events(store, rejected, EV_EXHAUSTED)[0]["detail"] == {
        "rejects": 3, "limit": 2}


def test_every_approval_event_hangs_on_a_span_tree(tmp_path):
    """行里必须带 plan_id 与 trace_id，否则掉进 `stray_events`，等于没发生过。"""
    db = tmp_path / "t111.db"
    store, _bus, cp, queue = _build(str(db))
    cp.set_replanner(_Replanner())
    plan_id = _make_plan(cp)
    queue.reject(plan_id, "boss", "再改改")
    queue.approve(plan_id, "boss")

    ours = [e for e in store.list_event_log(plan_id)
            if e["event_type"] in (EV_APPROVED, EV_REJECTED, EV_REPLANNED, EV_EXHAUSTED)]
    assert len(ours) == 3
    assert all(e["plan_id"] == plan_id and e["trace_id"] == TRACE for e in ours)
    assert stray_events(str(db)) == [], "游离事件挂不上任何一棵树"


# ======================================================================
# 9. 缺省零影响：不装它，什么都不变
# ======================================================================
def test_nothing_in_production_imports_the_approval_queue():
    """没有任何生产模块 import 它 —— 这就是「缺省路径逐字节不变」的机器判据。

    真要接进某个场景，是整合期主会话的事；那一天这条断言会红，红得应该 ——
    它会逼人明确回答「哪条链路从此要过人工审批」，而不是让它悄悄生效。
    """
    offenders = []
    for path in sorted(MAOS_PKG.rglob("*.py")):
        if "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names = set()
            if isinstance(node, ast.Import):
                names = {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom) and not node.level:
                module = node.module or ""
                names = {module} | {f"{module}.{a.name}" for a in node.names}
            if any(n.startswith("maos.runtime.plan_approval") for n in names):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert not offenders, f"计划审批被接进了生产路径：{offenders}"


def test_a_plan_run_without_the_queue_logs_none_of_the_four_events():
    """不装 PlanApprovalQueue 时，既有链路一条审批事件都不该多出来。"""
    store, _bus, cp, _queue = _build()
    plan_id = cp.create_plan(goal=GOAL, trace_id=TRACE, tasks=_specs_v1())
    cp.start_plan(plan_id)

    kinds = {e["event_type"] for e in store.list_event_log(plan_id)}
    assert kinds & {EV_APPROVED, EV_REJECTED, EV_REPLANNED, EV_EXHAUSTED} == set()
    assert store.get_plan(plan_id)["state"] == PlanState.RUNNING
