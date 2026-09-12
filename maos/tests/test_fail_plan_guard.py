"""`_fail_plan` 的重复判死护栏（T147）—— 六个调用点共用一份判据。

## 这个文件守的是什么

``ControlPlane._fail_plan`` 有 **6 个调用点**：重试额度耗尽（``on_task_result`` 的
failed 分支）、租约回收的 ``REAP_RETRY_EXHAUSTED``、返工次数耗尽、重规划返回空规格、
人工驳回、``_advance`` 的全冻结分支。而 ``PLAN_TRANSITIONS`` 里通往 ``FAILED`` 的合法
来源**只有 RUNNING**，``_transit_plan`` 第一句就是 ``assert_transition`` —— 于是任何一条
路径撞上「这个 plan 已经 FAILED 了」，当场抛 ``IllegalTransition: FAILED -> FAILED``。

T107 当时只在**租约回收那一个调用点**外面写了护栏（``plan["state"] == RUNNING`` 才调），
另外 5 个全裸着。T147 把判据下沉进 ``_fail_plan`` 的定义体，一份判据管六个口。

**真跑日会同时演驳回、重试耗尽、返工耗尽三条路径**，任意两条落在同一个 plan 上就复现
—— 这个文件的每一条用例都是那一刻的最小复现。

## 🔴 为什么这些用例**直调** `on_task_result` / `on_review_verdict`，不走 bus

走 `bus.publish(...) + bus.drain()` 的话，这几条**变异验证会全绿** —— 把护栏整个拆掉
它们照样过，从头到尾在空转。两层掩盖叠在一起：

  1. `InMemoryEventBus.drain()` 把 handler 的异常 catch 住当 nack（`eventbus.py:69`），
     `IllegalTransition` 逃不出 `drain`；
  2. 重投时 `result:<task_id>:<attempt>` 这个幂等键**已经被第一次调用消费掉了**，
     第二次进来直接短路 return —— 不抛，于是连死信都进不去，`dead_letters` 是空的。

合起来的效果是：屏幕上一切正常，而第一次调用里排在 `_fail_plan` **后面**的代码
（`on_task_result` 末尾那段销租约）永远没跑到。这不是测试写法问题，是这条 bug 在真跑日
的真实形态 —— 它不会当场炸给你看，它只是让后半截静默消失。已记入
`docs/BACKLOG.md` 的 `## task-t147` 小节。

所以这里直调控制面的方法：bus 订阅的 handler 就是它们本身（`control_plane.py:258-259`），
逻辑逐字相同，只是异常能逃回用例，**「不抛」这件事才真的被断言到**。

## 为什么断言不能只写「不抛异常」

重复判死若真的走完 ``_transit_plan``，``event_log`` 里会多出一条 ``PlanTransition``
``FAILED -> FAILED``。那不是无害的冗余：证据链里凭空多一条迁移，读证据的人会以为这个
plan 被判了两次死。所以每条用例都同时断言**事件条数没变**。

## 判据的分界（反向用例守的就是它）

护栏判的是「**已经是终态**」（``DONE`` / ``FAILED`` / plan 不存在），不是「不是 RUNNING」。
``PENDING`` 同样是非法来源，但它代表一个**真 bug**：有人忘了先 ``start_plan``
（重规划返回空规格那条路径正是先 ``start_plan`` 再判死的）。判据放宽成「不是 RUNNING
就 return」会把那个 bug 变成静默的无事发生 —— ``test_a_pending_plan_still_refuses_to_die``
钉的就是这条线。

全文件零模型调用、零网络：补偿那条把 ``MAOS_SANDBOX_WORKDIR`` 指到一个**非 git 目录**，
让沙箱如实报 ``ok=False``。要验的是「plan 被判了几次死」，不是补丁打不打得上。
"""

from __future__ import annotations

import pytest

from maos.artifacts import KIND_PATCH_SET
from maos.contracts import events as E
from maos.contracts.states import IllegalTransition, PlanState, TaskState
from maos.core.control_plane import ENV_SANDBOX_WORKDIR, ControlPlane
from maos.core.eventbus import InMemoryEventBus
from maos.core.store import SqliteStore

TRACE = "trace-t147"

PATCH_CONTENT = {
    "files": [{"path": "src/pay.py", "diff": "@@ -1,2 +1,3 @@\n+    verify(sig)"}],
    "summary": "补丁集样本",
    "self_check": {"build": "pass", "lint": "pass"},
}

SPEC = {
    "role": "coding", "title": "任务", "depends_on": [],
    "inputs": {"case_id": "C-1"}, "acceptance": ["必须过测试"],
    "risk_level": "L", "effect_risk": "L", "max_attempts": 1,
}


# ======================================================================
# 夹具
# ======================================================================
def _build() -> ControlPlane:
    store = SqliteStore()
    store.init_schema()
    return ControlPlane(store, InMemoryEventBus())


def _two_task_plan(cp, *, b_risk: str = "L") -> tuple[str, str, str]:
    """一个 plan 两条任务：甲负责把 plan 判死，乙负责随后再撞一次护栏。"""
    plan_id = cp.create_plan(goal="重复判死", trace_id=TRACE, tasks=[
        {**SPEC, "title": "甲-先把 plan 判死"},
        {**SPEC, "title": "乙-随后再撞一次", "effect_risk": b_risk},
    ])
    cp.start_plan(plan_id)
    a, b = cp.store.list_tasks(plan_id)
    return plan_id, a["task_id"], b["task_id"]


def _plan_transitions(store, plan_id) -> list[dict]:
    return [e for e in store.list_event_log(plan_id) if e["event_type"] == "PlanTransition"]


def _fail_task(cp, plan_id, task_id, attempt=1) -> None:
    """让一条任务交回 failed 且额度已耗尽 —— 走 :547，plan 跟着死。"""
    cp.claim(task_id, f"w-{task_id}", attempt)
    cp.on_task_result(E.task_result(
        plan_id=plan_id, task_id=task_id, attempt=attempt, trace_id=TRACE,
        status="failed", error="造出来的失败"))


def _submit_patch(cp, plan_id, task_id, attempt=1) -> None:
    cp.claim(task_id, f"w-{task_id}", attempt)
    cp.on_task_result(E.task_result(
        plan_id=plan_id, task_id=task_id, attempt=attempt, trace_id=TRACE, status="ok",
        artifacts=[{"kind": KIND_PATCH_SET, "content": PATCH_CONTENT}]))


def _verdict(cp, plan_id, task_id, verdict, findings=(), attempt=1) -> None:
    cp.on_review_verdict(E.review_verdict(
        plan_id=plan_id, task_id=task_id, attempt=attempt, trace_id=TRACE,
        verdict=verdict, findings=list(findings),
        gate_results={"security": "fail"} if findings else {}))


def _kill_plan_first(cp, plan_id, task_a) -> list[dict]:
    """前提：plan 已经被甲判死一次。返回此刻的 PlanTransition 快照。"""
    _fail_task(cp, plan_id, task_a)
    assert cp.store.get_plan(plan_id)["state"] == PlanState.FAILED, \
        "前提没成立：甲没把 plan 判死，后面撞不上护栏"
    before = _plan_transitions(cp.store, plan_id)
    assert len(before) == 2, f"前提没成立：此刻该只有 start + 判死两条，实得 {len(before)}"
    return before


# ======================================================================
# 一、三个真跑日会走的调用点，各撞一次已经 FAILED 的 plan
# ======================================================================
def test_human_reject_does_not_kill_an_already_failed_plan(monkeypatch, tmp_path):
    """人工驳回（control_plane.py:1216）—— 真跑日房间里真人 `/reject` 走的就是这条。

    这一条是三条里最凶的：它**不经过 bus**，房间命令直调 `human_decision`，所以
    `IllegalTransition` 一路逃到调用方，房间侧看到的是一次「命令执行失败」——
    而补偿此刻**已经执行过了**（驳回是先回滚再改状态），任务也已落 FAILED。
    人看到的是「驳回失败」，系统里却是「驳回做了一半」。评委面前炸的就是这一下。
    """
    monkeypatch.setenv(ENV_SANDBOX_WORKDIR, str(tmp_path / "不是一个-git-仓库"))
    cp = _build()
    plan_id, task_a, task_b = _two_task_plan(cp, b_risk="H")

    # 乙先走到「等人工审批」那一刻（effect_risk=H，闸过了也不自动放行）。
    _submit_patch(cp, plan_id, task_b)
    _verdict(cp, plan_id, task_b, "pass")
    assert cp.store.get_task(task_b)["state"] == TaskState.BLOCKED

    before = _kill_plan_first(cp, plan_id, task_a)

    cp.human_decision(task_b, approved=False, operator="沈思锴", note="线下核过，不放行")

    assert cp.store.get_task(task_b)["state"] == TaskState.FAILED, "驳回本身仍要把任务判死"
    assert cp.store.get_plan(plan_id)["state"] == PlanState.FAILED
    assert _plan_transitions(cp.store, plan_id) == before, \
        "plan 被判了第二次死 —— event_log 里多出一条 PlanTransition，证据链被污染"


def test_retry_exhausted_does_not_kill_an_already_failed_plan():
    """重试额度耗尽（control_plane.py:547）—— 同一个 plan 里两条任务先后耗尽额度。

    这是最容易撞上的一条：一个 plan 里多条任务并行推进，谁先耗尽额度谁把 plan 判死，
    第二条交回 failed 时 plan 早已是 FAILED。

    修复前它的形态尤其阴 —— 乙的 `_transit(FAILED)` 排在 `_fail_plan` **前面**，
    已经落了；抛出去之后 `on_task_result` 末尾那段销租约再也跑不到。走 bus 时异常还会
    被 `drain` 吞掉（见本文件开头），于是屏幕上一片正常，而 `claim_lease` 表里躺着一条
    永远不会被销的租约 ——「哪些任务真的有人在做」这个问题从此答不准。
    """
    cp = _build()
    plan_id, task_a, task_b = _two_task_plan(cp)

    before = _kill_plan_first(cp, plan_id, task_a)

    _fail_task(cp, plan_id, task_b)

    assert cp.store.get_task(task_b)["state"] == TaskState.FAILED, "乙自己仍要被判死"
    assert cp.store.get_plan(plan_id)["state"] == PlanState.FAILED
    assert _plan_transitions(cp.store, plan_id) == before, "plan 被判了第二次死"


def test_rework_exhausted_does_not_kill_an_already_failed_plan():
    """返工次数耗尽（control_plane.py:898）—— 闸判 rework 而额度已经用完。

    findings 用普通 `gate=security` 的 blocker：既不命中第三出口 `_human_exit`
    （那会走 AWAITING_REVIEW -> BLOCKED，压根不碰 plan 状态），也没有注入 replanner，
    于是稳稳落在 `elif env.attempt >= task["max_attempts"]` 那一支上 —— 正是要撞的那个口。
    """
    cp = _build()
    plan_id, task_a, task_b = _two_task_plan(cp)

    _submit_patch(cp, plan_id, task_b)
    assert cp.store.get_task(task_b)["state"] == TaskState.AWAITING_REVIEW

    before = _kill_plan_first(cp, plan_id, task_a)

    _verdict(cp, plan_id, task_b, "rework", findings=[
        {"gate": "security", "severity": "blocker", "path": "f0.py", "message": "明文凭证"}])

    assert cp.store.get_task(task_b)["state"] == TaskState.FAILED, "返工额度耗尽，乙仍要被判死"
    assert cp.store.get_plan(plan_id)["state"] == PlanState.FAILED
    assert _plan_transitions(cp.store, plan_id) == before, "plan 被判了第二次死"


# ======================================================================
# 二、反向：护栏只放过终态，PENDING 仍然要炸
# ======================================================================
def test_a_pending_plan_still_refuses_to_die():
    """`PENDING -> FAILED` **仍然抛**。这条钉的是判据的分界，不是护栏本身。

    护栏若写成「不是 RUNNING 就 return」，上面三条照样全绿 —— 而「有人忘了先
    `start_plan`」这个真 bug 会从一声异常变成一次静默的无事发生。重规划返回空规格
    那条路径（:1112）**必须**先 `start_plan` 把 PENDING 抬回 RUNNING 再判死，
    靠的正是这里还会炸。

    放宽判据的人会先看见这条红，那时他要面对的问题是「我凭什么让 PENDING 也算数」，
    而不是「加个 or PENDING 就绿了」。
    """
    cp = _build()
    plan_id = cp.create_plan(goal="没 start 就判死", trace_id=TRACE, tasks=[dict(SPEC)])
    assert cp.store.get_plan(plan_id)["state"] == PlanState.PENDING

    with pytest.raises(IllegalTransition):
        cp._fail_plan(plan_id)

    assert cp.store.get_plan(plan_id)["state"] == PlanState.PENDING, "炸了也不许留下半截迁移"
    assert _plan_transitions(cp.store, plan_id) == []


# ======================================================================
# 三、护栏的另外两条入口：DONE 与「plan 根本不存在」
# ======================================================================
def test_a_done_plan_is_not_reopened_into_failed():
    """`DONE` 也是终态：判死一个已经完成的 plan 是无事发生，不是把它翻成 FAILED。

    `DONE -> FAILED` 同样不在 `PLAN_TRANSITIONS` 里。护栏若只认 FAILED 一种终态，
    这里会抛 —— 而真正该出声的是「谁在一个已完成的计划上调判死」，那是调用方的 bug，
    留给 log。把一个成功的计划翻成失败比抛异常坏得多。
    """
    cp = _build()
    plan_id, task_a, task_b = _two_task_plan(cp)
    for t in (task_a, task_b):
        _submit_patch(cp, plan_id, t)
        _verdict(cp, plan_id, t, "pass")
    assert cp.store.get_plan(plan_id)["state"] == PlanState.DONE
    before = _plan_transitions(cp.store, plan_id)

    cp._fail_plan(plan_id)

    assert cp.store.get_plan(plan_id)["state"] == PlanState.DONE, "已完成的计划被翻成了失败"
    assert _plan_transitions(cp.store, plan_id) == before


def test_an_unknown_plan_id_does_not_explode():
    """plan 取不回来（`get_plan` 返回 None）时不许崩在 `plan["state"]` 上。

    原护栏在租约回收那一处写了 `plan is not None`，下沉时这一半同样要带过来 ——
    丢了的话 `TypeError` 换个地方接着炸，且比 `IllegalTransition` 更难看懂。
    """
    _build()._fail_plan("plan-根本不存在")
