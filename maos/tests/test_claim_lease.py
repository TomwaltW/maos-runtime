"""认领租约与跨 plan 任务板（T107）—— 把 claim_timeout 那条冻结迁移钉在地上。

## 这个文件守的是什么

三件事，合起来才是「多个 agent 自我认领任务」这件事能成立的地基：

  1. **认领之后有超时接管**。改造前 maos/contracts/states.py 里
     (DISPATCHED, PENDING) -> claim_timeout 是一条零实现的迁移：认领方硬崩之后，
     claim:<task_id>:<attempt> 这个幂等键已经烧掉且没有撤销口，任务从此永久停在
     RUNNING，没有任何机制把它捞回来。
  2. **旁观者不许替系统判任务死刑**。改造前 role 不在自己池里的 Worker 会回一条
     status=failed，控制面照单全收 —— 任务被推回 PENDING，且真正认领方稍后交回的
     结果会被幂等闸当成重复投递丢掉。
  3. **任务板是跨 plan 的只读投影**。store.list_tasks 是 per-plan 的；队友属于团队，
     不属于某个计划。

## 两条最容易被写坏的不变量（本文件的牙齿都长在这两条上）

**其一：前置校验必须排在幂等闸之前。** 幂等键一旦消费就不回滚。让一条非法结果先烧掉
result:<task_id>:<attempt>，这个 attempt 就再也交不回任何结果。
test_result_from_non_claimer_is_dropped_and_the_claimer_result_still_works 是这一条的
判据 —— 它不只断言「非法结果被丢」，还断言随后**同一个幂等键**的合法结果照常被处理。
只断言前半句的话，一个「先烧键再丢弃」的实现照样能过。

**其二：任务板不发认领许可。** claimable() 返回的是一份快照，两个队友可以同时拿到
同一条任务；互斥只发生在 cp.claim 里那个幂等键上。
test_task_board_snapshot_does_not_grant_the_claim 让两个 worker 从**同一份快照**上
各领一次，断言只有一个拿到 —— 这一条挡的是「任务板顺手把 state 改成 RUNNING」那种写法。

## 缺省路径

不注入 LeaseBook 时，一切行为与改造前逐字节一致（铁律 4）。开头四条用例守这个：
claim_lease 那张表连建都不建，reap 恒返回 0，不填 worker_id 的老调用方一条都不受影响。
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

import maos.agents.coding  # noqa: F401 —— import 即注册进 AGENT_POOL
from maos.agents.base import AGENT_POOL, AgentOutput
from maos.contracts import events as E
from maos.contracts.events import Envelope, Topic
from maos.contracts.states import PlanState, TaskState
from maos.core.control_plane import (
    FROZEN_BY_REPLAN,
    LEASE_EXPIRED,
    REAP_CLAIM_TIMEOUT,
    REAP_RETRY,
    REAP_RETRY_EXHAUSTED,
    REAP_SOURCE_DISPATCH,
    REAP_STALE,
    STALE_RESULT_DROPPED,
    ControlPlane,
)
from maos.core.eventbus import InMemoryEventBus
from maos.core.lease import LeaseBook, canon_iso, parse_utc
from maos.core.store import SqliteStore
from maos.model.client import ScriptedModelClient
from maos.runtime.worker import WorkerRuntime

#: 租约 TTL，全文件统一。取 60 只为算术好读，与生产缺省无关。
TTL = 60

#: 固定时钟。**只给「租约也是手工 grant 出来的」那些用例用** —— 那种用例里
#: granted_at 与判定用的 now 都是字面量，整条链自洽，与真实日期无关。
T0 = "2026-09-08T00:00:00.000000+00:00"
T_LATER = "2026-09-08T01:00:00.000000+00:00"      # T0 之后一小时，任何 TTL 都已到期


def _after(seconds: int) -> str:
    """真实时钟之后 N 秒。

    🔴 **凡是租约由 cp.claim 登记的用例，判过期的 now 必须从这里取。**
    claim 内部用的是 utc_now_iso()（真实时钟），拿写死的日期去跟它比，答案取决于
    「今天是几号」：这些用例在写下它们的那天全绿，隔一天集体变红，而红的原因
    与代码毫无关系。本文件第一版就踩了这个 —— 本机是 UTC+8，写用例时 UTC 还停在
    前一天，于是 T_LATER 恰好落在租约到期之后，看着一切正常。
    """
    return canon_iso((datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat())


# ======================================================================
# 脚手架
# ======================================================================
def _boot(*, leases: bool = True) -> tuple[SqliteStore, InMemoryEventBus, ControlPlane]:
    store = SqliteStore()
    store.init_schema()
    bus = InMemoryEventBus()
    book = LeaseBook(store, ttl_s=TTL) if leases else None
    return store, bus, ControlPlane(store, bus, leases=book)


def _plan(cp: ControlPlane, *, roles=("coding",), max_attempts: int = 3,
          trace_id: str = "trace-t107", goal: str = "租约测试") -> str:
    return cp.create_plan(goal=goal, trace_id=trace_id, tasks=[
        {"role": r, "title": f"任务-{i}-{r}", "inputs": {}, "acceptance": [],
         "effect_risk": "L", "max_attempts": max_attempts}
        for i, r in enumerate(roles)
    ])


def _dispatched(cp: ControlPlane, **kw) -> tuple[str, dict]:
    """造一个已派发（DISPATCHED、attempt=1）的单任务 plan。"""
    plan_id = _plan(cp, **kw)
    cp.start_plan(plan_id)
    task = cp.store.list_tasks(plan_id)[0]
    assert task["state"] == TaskState.DISPATCHED, "前置不成立：任务没被派发"
    assert task["attempt"] == 1
    return plan_id, task


def _logs(store: SqliteStore, plan_id: str, event_type: str) -> list[dict]:
    return [e for e in store.list_event_log(plan_id) if e["event_type"] == event_type]


def _transitions(store: SqliteStore, plan_id: str, src: str, dst: str) -> list[dict]:
    return [e for e in store.list_event_log(plan_id)
            if e["event_type"] == "StateTransition"
            and e["from_state"] == src and e["to_state"] == dst]


def _key_rows(store: SqliteStore, key: str) -> list[tuple]:
    return store._conn.execute(
        "SELECT idempotency_key, op FROM processed_key WHERE idempotency_key=?",
        (key,)).fetchall()


def _tables(store: SqliteStore) -> set[str]:
    return {r[0] for r in store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type=?", ("table",)).fetchall()}


class _StubAgent:
    """执行体替身：把 pull_and_claim 的路径从模型行为里摘出来。

    换掉真 Agent 是刻意的 —— 本文件测的是**认领与交回的时序**，不是 Coding Agent
    产出什么。真 Agent 进来的话，一条模型脚本的改动就能让这些用例莫名其妙地红，
    而红的原因与租约毫无关系。
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def run(self, ctx) -> AgentOutput:
        self.calls.append(ctx.task_id)
        return AgentOutput(status="ok", artifacts=[{"kind": "doc", "content": {}}])


def _worker(bus, cp, *, worker_id: str, roles=frozenset({"coding"})) -> WorkerRuntime:
    w = WorkerRuntime(worker_id=worker_id, bus=bus, control_plane=cp,
                      model=ScriptedModelClient(), roles=roles)
    for role in list(w.agents):
        w.agents[role] = _StubAgent()
    return w


# ======================================================================
# 一、缺省路径逐字节不变（铁律 4）
# ======================================================================
def test_default_control_plane_never_creates_the_lease_table():
    """不注入 LeaseBook 时，claim_lease 那张表连建都不建。

    「新增表不影响既有行为」这句话要有判据。表在但空着也算不影响，但**表根本
    不存在**才证明这一层是彻底可选的 —— 任何依赖它存在的代码会当场炸，而不是
    静默走一条没人测过的分支。
    """
    store, _bus, cp = _boot(leases=False)
    assert cp.leases is None
    _plan_id, task = _dispatched(cp)
    cp.claim(task["task_id"], "w1", 1)

    assert "claim_lease" not in _tables(store), (
        "没注入 LeaseBook 却建了 claim_lease 表 —— 缺省路径被改动了")


def test_default_control_plane_reaps_nothing():
    """不注入时 reap_expired_leases 恒返回 0，且不落任何 LeaseExpired。"""
    _store, _bus, cp = _boot(leases=False)
    plan_id, task = _dispatched(cp)
    cp.claim(task["task_id"], "w1", 1)

    assert cp.reap_expired_leases(now_iso=T_LATER) == 0
    assert _logs(cp.store, plan_id, LEASE_EXPIRED) == []


def test_default_path_result_without_worker_id_is_still_handled():
    """不填 worker_id 的老调用方一条都不许受影响。

    E.task_result 的 worker_id 缺省是空串，全仓历史调用方都是这么发的。前置校验
    要是把「没说自己是谁」也当成「不是认领方」，一批合法调用会集体失声 ——
    那才是真正的回归，而且它的症状是任务集体卡在 RUNNING。
    """
    store, _bus, cp = _boot(leases=False)
    plan_id, task = _dispatched(cp)
    cp.claim(task["task_id"], "w1", 1)

    cp.on_task_result(E.task_result(
        plan_id=plan_id, task_id=task["task_id"], attempt=1, trace_id=task["trace_id"],
        status="ok", artifacts=[{"kind": "doc", "content": {}}]))

    assert store.get_task(task["task_id"])["state"] == TaskState.AWAITING_REVIEW
    assert _logs(store, plan_id, STALE_RESULT_DROPPED) == []


def test_anonymous_result_is_still_handled_even_with_leases_on():
    """注入 LeaseBook 之后，空 worker_id 的结果**照样**被处理。

    上一条测的是「没注入时不变」，这一条测的是「注入了也不变」。两条都要有：
    前置校验是无条件跑的（不看 self.leases），所以注入与否都可能被它误伤。
    """
    store, _bus, cp = _boot()
    plan_id, task = _dispatched(cp)
    cp.claim(task["task_id"], "w1", 1)
    assert store.get_task(task["task_id"])["worker_id"] == "w1"

    cp.on_task_result(E.task_result(
        plan_id=plan_id, task_id=task["task_id"], attempt=1, trace_id=task["trace_id"],
        status="ok", artifacts=[]))

    assert store.get_task(task["task_id"])["state"] == TaskState.AWAITING_REVIEW, (
        "空 worker_id 被误判成「不是认领方」—— 所有不填该字段的历史调用方会集体失声")


# ======================================================================
# 二、租约到期接管
# ======================================================================
def test_claim_registers_a_lease():
    """认领成功登记一条租约，attempt 与 worker 都要对得上。"""
    _store, _bus, cp = _boot()
    _plan_id, task = _dispatched(cp)
    cp.claim(task["task_id"], "w1", 1)

    held = cp.leases.holder(task["task_id"])
    assert held is not None, "认领成功却没登记租约 —— 超时接管无从谈起"
    assert (held["attempt"], held["worker_id"]) == (1, "w1")
    assert parse_utc(held["expires_at"]) > parse_utc(held["granted_at"])


def test_result_handback_releases_the_lease():
    """结果交回即销租约 —— 租约描述的是「谁正在做」，不是「谁做过」。

    不销也不会错到失控（到期回收会按 stale_lease 只销不迁），但那要拖满一个 TTL，
    期间 claim_lease 表里堆着一批已经交代完的租约，「哪些任务真的有人在做」
    这个问题从此答不准。
    """
    store, _bus, cp = _boot()
    plan_id, task = _dispatched(cp)
    tid = task["task_id"]
    cp.claim(tid, "w1", 1)
    assert cp.leases.holder(tid) is not None

    cp.on_task_result(E.task_result(
        plan_id=plan_id, task_id=tid, attempt=1, trace_id=task["trace_id"],
        status="ok", artifacts=[], worker_id="w1"))

    assert store.get_task(tid)["state"] == TaskState.AWAITING_REVIEW
    assert cp.leases.holder(tid) is None, "结果交回了租约还挂着，任务板会一直以为有人在做"


def test_dropped_result_does_not_release_the_claimer_lease():
    """被丢弃的结果销不着认领方的租约。

    让一个旁观者的假失败销掉真正认领方的租约，等于把一个正在被执行的任务重新
    挂回任务板 —— 于是同一份活被做两遍，而两边都看不出哪里错了。
    """
    _store, _bus, cp = _boot()
    plan_id, task = _dispatched(cp)
    tid = task["task_id"]
    cp.claim(tid, "w-claimer", 1)

    cp.on_task_result(E.task_result(
        plan_id=plan_id, task_id=tid, attempt=1, trace_id=task["trace_id"],
        status="failed", error="无可用 Agent", worker_id="w-bystander"))

    held = cp.leases.holder(tid)
    assert held is not None and held["worker_id"] == "w-claimer", (
        "旁观者的假失败把认领方的租约销掉了 —— 这条任务会被重新挂上任务板")


def test_expired_lease_on_running_task_retries_and_can_be_reclaimed():
    """RUNNING 的任务租约到期 -> retry 回 PENDING -> 重新派发 -> 新 attempt 可认领。

    这是「认领方硬崩」那条路径的全貌。判据不止是状态回到 PENDING：还要证明
    **新一轮的认领真的能成功**，因为改造前那个洞的症状恰恰是「状态看着对，但
    幂等键已经烧了，谁也领不走」。
    """
    store, _bus, cp = _boot()
    plan_id, task = _dispatched(cp)
    tid = task["task_id"]
    cp.claim(tid, "w-crashed", 1)                # 认领后就此失联，结果永远不交回
    assert store.get_task(tid)["state"] == TaskState.RUNNING

    assert cp.reap_expired_leases(now_iso=_after(TTL + 60)) == 1

    moves = _transitions(store, plan_id, TaskState.RUNNING, TaskState.PENDING)
    assert len(moves) == 1 and moves[0]["reason"] == "retry", (
        f"RUNNING->PENDING 应走既有的 retry 迁移，实得 {moves}")
    expired = _logs(store, plan_id, LEASE_EXPIRED)
    assert len(expired) == 1 and expired[0]["reason"] == REAP_RETRY
    assert cp.leases.holder(tid) is None, "处置完没销租约，下一轮会被重复回收"

    task = store.get_task(tid)
    assert task["state"] == TaskState.DISPATCHED and task["attempt"] == 2, (
        "回收之后没有重新派发 —— 任务回到队列却没人叫它")
    assert cp.claim(tid, "w-next", 2) is not None, (
        "新 attempt 的合法认领被拒 —— 回收过程烧掉了不该烧的幂等键")


def test_expired_lease_on_dispatched_task_uses_claim_timeout():
    """DISPATCHED 的任务租约到期 -> claim_timeout 回 PENDING。

    **这是 (DISPATCHED, PENDING) -> claim_timeout 这条冻结迁移的第一个实现**，
    改造前全仓生产代码零命中。判据钉在 reason 上而不只是状态：走 retry 也能把
    任务从别的状态挪到 PENDING，但那是另一条迁移，语义是「重试」不是「没人认领」。

    租约直接 grant 出来（不经 claim）是刻意的：这一支描述的正是「派发登记了，
    但认领始终没发生」，而 claim 成功的那一刻任务就已经不是 DISPATCHED 了。
    """
    store, _bus, cp = _boot()
    plan_id, task = _dispatched(cp)
    tid = task["task_id"]
    cp.leases.grant(tid, 1, "w-never-showed-up", now_iso=T0)

    assert cp.reap_expired_leases(now_iso=T_LATER) == 1

    moves = _transitions(store, plan_id, TaskState.DISPATCHED, TaskState.PENDING)
    assert len(moves) == 1 and moves[0]["reason"] == "claim_timeout", (
        f"DISPATCHED->PENDING 应走 claim_timeout，实得 {moves}")
    assert _logs(store, plan_id, LEASE_EXPIRED)[0]["reason"] == REAP_CLAIM_TIMEOUT
    assert store.get_task(tid)["attempt"] == 2, "回收后应重新派发并进入下一 attempt"


def test_expired_lease_with_attempts_exhausted_fails_task_and_plan():
    """额度耗尽的过期租约判死任务并连坐 plan —— 绝不无限重试。

    无限重试是评委点名的反模式，而「超时了就再派一次」是它最自然的伪装：
    没有这条上限，一个没人能干的任务会在队列里永远转下去。
    """
    store, _bus, cp = _boot()
    plan_id, task = _dispatched(cp, max_attempts=1)
    tid = task["task_id"]
    cp.claim(tid, "w-crashed", 1)

    assert cp.reap_expired_leases(now_iso=_after(TTL + 60)) == 1

    assert store.get_task(tid)["state"] == TaskState.FAILED
    assert store.get_plan(plan_id)["state"] == PlanState.FAILED, (
        "任务判死了 plan 却还在跑 —— 与 on_task_result 的 failed 分支口径不一致")
    assert _logs(store, plan_id, LEASE_EXPIRED)[0]["reason"] == REAP_RETRY_EXHAUSTED
    assert _transitions(store, plan_id, TaskState.RUNNING, TaskState.PENDING) == [], (
        "额度已耗尽还走了一次 retry —— 上限没生效")


def test_expired_lease_on_advanced_task_only_releases():
    """任务已经往前走了，过期租约是陈迹：只销租约，一步都不迁移。

    拿一条过期租约去动一个已经进了下一环的任务，是在倒放历史 —— 症状是评审
    通过的任务莫名回到队列被重做一遍。
    """
    store, _bus, cp = _boot()
    plan_id, task = _dispatched(cp)
    tid = task["task_id"]
    cp.claim(tid, "w1", 1)
    cp.on_task_result(E.task_result(
        plan_id=plan_id, task_id=tid, attempt=1, trace_id=task["trace_id"],
        status="ok", artifacts=[], worker_id="w1"))
    assert store.get_task(tid)["state"] == TaskState.AWAITING_REVIEW
    cp.leases.grant(tid, 1, "w1", now_iso=T0)     # 没销干净的陈旧租约

    assert cp.reap_expired_leases(now_iso=T_LATER) == 1

    assert store.get_task(tid)["state"] == TaskState.AWAITING_REVIEW, (
        "陈旧租约把一个已经进了下一环的任务拽了回来")
    assert _logs(store, plan_id, LEASE_EXPIRED)[0]["reason"] == REAP_STALE
    assert cp.leases.holder(tid) is None


def test_reap_finishes_all_leases_before_dispatching():
    """同 plan 的多条租约：全部处置完才派发，不在循环里边处置边派发。

    循环内派发时，第一条刚回到 PENDING 就被派出去，而第二条还没处置完 ——
    派发看到的是一份处置到一半的任务表。判据是 event_log 的先后顺序：
    任何一条重新派发都必须排在**最后一条** LeaseExpired 之后。
    """
    store, _bus, cp = _boot()
    plan_id = _plan(cp, roles=("coding", "reviewer"))
    cp.start_plan(plan_id)
    for t in store.list_tasks(plan_id):
        cp.claim(t["task_id"], "w-crashed", 1)

    assert cp.reap_expired_leases(now_iso=_after(TTL + 60)) == 2

    log = store.list_event_log(plan_id)
    last_reap = max(i for i, e in enumerate(log) if e["event_type"] == LEASE_EXPIRED)
    redispatch = [i for i, e in enumerate(log)
                  if e["event_type"] == "StateTransition"
                  and e["from_state"] == TaskState.PENDING
                  and e["to_state"] == TaskState.DISPATCHED
                  and i > last_reap - 4]          # 只看回收这一轮，跳过 start_plan 那两条
    assert redispatch, "两条租约都回收了却没有任何重新派发"
    assert min(redispatch) > last_reap, (
        "在租约还没处置完时就派发了 —— 派发读到的是一份处置到一半的任务表")


def test_reap_survives_a_lease_whose_task_is_gone():
    """任务没了租约还在：销掉租约就是全部能做的事，不许抛。

    回收是后台循环的一环。它对一条脏数据抛异常，整轮回收就停了 —— 后面所有
    正常的过期租约跟着一起收不掉，而症状离原因极远。
    """
    _store, _bus, cp = _boot()
    cp.leases.grant("task-does-not-exist", 1, "w1", now_iso=T0)

    assert cp.reap_expired_leases(now_iso=T_LATER) == 1
    assert cp.leases.holder("task-does-not-exist") is None


def test_live_lease_is_not_reaped():
    """没到期的租约一条都不许动 —— 否则正在干活的 worker 会被判失联。"""
    store, _bus, cp = _boot()
    _plan_id, task = _dispatched(cp)
    cp.claim(task["task_id"], "w1", 1)

    assert cp.reap_expired_leases(now_iso=_after(0)) == 0
    assert store.get_task(task["task_id"])["state"] == TaskState.RUNNING
    assert cp.leases.holder(task["task_id"]) is not None


# ======================================================================
# 三、前置校验：非法结果不许烧幂等键
# ======================================================================
def test_stale_attempt_result_is_dropped_without_burning_the_key():
    """上一轮的迟到结果被丢弃，且它的幂等键**没被消费**。

    键有没有被烧掉是这条用例真正的牙齿。只断言「任务状态没变」的话，一个
    「先烧键再丢弃」的实现照样能过 —— 而那种实现会让这个 attempt 从此交不回
    任何结果。
    """
    store, _bus, cp = _boot()
    plan_id, task = _dispatched(cp)
    tid = task["task_id"]
    cp.claim(tid, "w1", 1)
    cp.reap_expired_leases(now_iso=_after(TTL + 60))   # 任务回到队列并重派成 attempt=2
    assert store.get_task(tid)["attempt"] == 2

    cp.on_task_result(E.task_result(              # attempt=1 的迟到结果
        plan_id=plan_id, task_id=tid, attempt=1, trace_id=task["trace_id"],
        status="ok", artifacts=[{"kind": "doc", "content": {}}], worker_id="w1"))

    assert store.get_task(tid)["state"] == TaskState.DISPATCHED, (
        "上一轮的迟到结果覆盖了新一轮的状态")
    assert _key_rows(store, f"result:{tid}:1") == [], (
        "被丢弃的结果消费了幂等键 —— 丢弃发生在幂等闸之前这条被破坏了")
    dropped = _logs(store, plan_id, STALE_RESULT_DROPPED)
    assert len(dropped) == 1 and dropped[0]["reason"] == "stale_attempt"
    assert dropped[0]["detail"]["expected_attempt"] == 2
    assert dropped[0]["detail"]["got_attempt"] == 1


def test_result_from_non_claimer_is_dropped_and_the_claimer_result_still_works():
    """旁观者的结果被丢弃，而**同一个幂等键**的认领方结果照常被处理。

    这一条是整个前置校验存在的理由，两半缺一不可：

      · 前半：一个 role 不在自己池里的 Worker 回的 failed 不许把任务推回 PENDING；
      · 后半：真正的认领方稍后交回的结果**仍然能被处理** —— 两条结果的
        idempotency_key 是同一个（result:<task_id>:<attempt>），所以只要那条假失败
        烧掉了键，这一半立刻红。改造前正是这个形态。
    """
    store, _bus, cp = _boot()
    plan_id, task = _dispatched(cp)
    tid = task["task_id"]
    cp.claim(tid, "w-claimer", 1)

    cp.on_task_result(E.task_result(              # 旁观者判死刑
        plan_id=plan_id, task_id=tid, attempt=1, trace_id=task["trace_id"],
        status="failed", error="无可用 Agent", worker_id="w-bystander"))

    assert store.get_task(tid)["state"] == TaskState.RUNNING, (
        "旁观者的 failed 把任务推回了队列 —— 它替系统判了一个自己干不了的任务死刑")
    dropped = _logs(store, plan_id, STALE_RESULT_DROPPED)
    assert len(dropped) == 1 and dropped[0]["reason"] == "not_claimer"
    assert dropped[0]["detail"]["expected_worker"] == "w-claimer"
    assert dropped[0]["detail"]["got_worker"] == "w-bystander"

    cp.on_task_result(E.task_result(              # 认领方交回，同一个幂等键
        plan_id=plan_id, task_id=tid, attempt=1, trace_id=task["trace_id"],
        status="ok", artifacts=[{"kind": "doc", "content": {}}], worker_id="w-claimer"))

    assert store.get_task(tid)["state"] == TaskState.AWAITING_REVIEW, (
        "认领方的合法结果被当成重复投递丢弃 —— 幂等键被那条假失败先烧掉了")


def test_genuine_duplicate_result_is_still_short_circuited():
    """幂等闸本身没有被前置校验架空：同一条结果投两次，第二次仍然短路。

    前置校验放到幂等闸前面之后，最容易顺手写坏的就是幂等闸本身 —— 比如把它
    连同校验一起挪走。这条用例是那道回归守卫。
    """
    store, _bus, cp = _boot()
    plan_id, task = _dispatched(cp)
    tid = task["task_id"]
    cp.claim(tid, "w1", 1)
    env = E.task_result(plan_id=plan_id, task_id=tid, attempt=1,
                        trace_id=task["trace_id"], status="ok",
                        artifacts=[{"kind": "doc", "content": {}}], worker_id="w1")

    cp.on_task_result(env)
    cp.on_task_result(env)

    moves = _transitions(store, plan_id, TaskState.RUNNING, TaskState.AWAITING_REVIEW)
    assert len(moves) == 1, f"同一条结果投两次落了 {len(moves)} 次迁移，应为 1 次"
    arts = store.list_artifacts(tid)
    assert len(arts) == 1, "重复投递把 artifact 写了两份"


# ======================================================================
# 四、异构队友：role 不在池里就不承接
# ======================================================================
def test_worker_outside_its_pool_stays_silent():
    """role 不在池里 -> 静默跳过：旁观者在任何账本上都不许留下痕迹。

    改造前这里回一条 status=failed，那是一个旁观者在替系统判任务死刑。

    🔴 **判据不能只看终态。** 本文件第一版就写成了「任务仍是 DISPATCHED」，而那条
    断言在改回 `_reply(failed)` 之后**照样绿**：假失败走的是
    `DISPATCHED -> PENDING`（即 claim_timeout 那条边），紧接着 `dispatch_ready`
    又把它派了出去，终态绕回 DISPATCHED。变异实测把这个漏洞照了出来。

    所以判据钉在**痕迹**上，三条各挡一种：
      · attempt 仍是 1     —— 重派会把它推到 2；
      · 没有 DISPATCHED->PENDING 这一跳 —— 假失败必然留下它；
      · 总线上没有 TaskResult —— 从源头证明旁观者一个字都没说。
    """
    store, bus, cp = _boot()
    plan_id, task = _dispatched(cp)             # coding 任务
    tid = task["task_id"]
    w = _worker(bus, cp, worker_id="w-reviewer-only", roles=frozenset({"reviewer"}))
    seen: list[Envelope] = []
    bus.subscribe(Topic.TASK_RESULT, "spy", seen.append)

    bus.drain()

    assert seen == [], (
        f"旁观者发出了 {len(seen)} 条 TaskResult —— 它在替一个自己干不了的任务表态")
    after = store.get_task(tid)
    assert after["state"] == TaskState.DISPATCHED, (
        "一个干不了这活的 Worker 改变了任务状态 —— 它在替系统做判断")
    assert after["attempt"] == 1, (
        "任务被重派了一轮 —— 旁观者的假失败把它推回队列又派了出来，终态看着没变而已")
    assert _transitions(store, plan_id, TaskState.DISPATCHED, TaskState.PENDING) == [], (
        "任务走了一次 DISPATCHED->PENDING —— 这条边该由租约超时触发，不是由旁观者")
    assert "coding" not in w.agents
    assert _logs(store, plan_id, STALE_RESULT_DROPPED) == [], (
        "旁观者根本不该发出结果，轮不到前置校验去拦")


def test_worker_inside_its_pool_still_handles_assignment():
    """对照组：同一条链路上，池里有这个 role 的 Worker 照常认领并交回。

    没有这条对照，上一条用例被一个「on_assignment 直接 return」的实现也能过。
    """
    store, bus, cp = _boot()
    _plan_id, task = _dispatched(cp)
    w = _worker(bus, cp, worker_id="w-coding", roles=frozenset({"coding"}))

    bus.drain()

    assert w.agents["coding"].calls == [task["task_id"]]
    assert store.get_task(task["task_id"])["state"] == TaskState.AWAITING_REVIEW


def test_full_pool_worker_is_the_default():
    """不传 roles = 今天的行为：装全池。"""
    _store, bus, cp = _boot()
    w = WorkerRuntime(worker_id="w-full", bus=bus, control_plane=cp,
                      model=ScriptedModelClient())
    assert set(w.agents) == set(AGENT_POOL)
    assert w.roles == frozenset(AGENT_POOL)


def test_unknown_role_fails_fast():
    """池里没有的 role 当场炸，不许静默生成一个什么活都领不到的 Worker。"""
    _store, bus, cp = _boot()
    with pytest.raises(ValueError, match="不在 AGENT_POOL"):
        WorkerRuntime(worker_id="w-typo", bus=bus, control_plane=cp,
                      model=ScriptedModelClient(), roles=frozenset({"codign"}))


# ======================================================================
# 五、任务板与自我认领
# ======================================================================
def test_claimable_spans_plans_and_filters_by_role():
    """跨 plan 返回，且只返回自己干得了的 role。

    store.list_tasks 是 per-plan 的，而队友属于团队不属于某个计划 —— 跨 plan
    这一查没有现成方法，这条用例守的就是它。
    """
    store, _bus, cp = _boot()
    p1 = _plan(cp, roles=("coding", "reviewer"), trace_id="tr-1")
    p2 = _plan(cp, roles=("coding",), trace_id="tr-2")
    cp.start_plan(p1)
    cp.start_plan(p2)

    rows = cp.leases.claimable(frozenset({"coding"}), T0)

    assert {r["plan_id"] for r in rows} == {p1, p2}, (
        "任务板没有跨 plan —— 队友只看得见一个计划里的活")
    assert {r["role"] for r in rows} == {"coding"}
    assert len(store.list_tasks(p1)) == 2, "前置：p1 里确实有一条 reviewer 任务被滤掉了"


def test_claimable_excludes_tasks_that_are_no_longer_dispatched():
    """RUNNING（有人正在干）与 AWAITING_REVIEW（已交付待评审）不许上任务板。

    这条守的是 `claimable` 的 `t.state = 'DISPATCHED'` 那半个 WHERE 子句。
    在它之前，那半句**零覆盖**：删掉它全量测试一条都不红 —— 唯一看起来覆盖它的
    那句 `assert all(r["state"] == DISPATCHED for r in rows)` 在自己的 fixture 里
    恒真（那个 fixture 造出来的任务全是 DISPATCHED，没有别的状态可选）。
    所以断言写成「**存在**非 DISPATCHED 的任务，且它没被返回」，不写成
    「返回的都是 DISPATCHED」—— 后者对着一份全 DISPATCHED 的数据永远为真。

    症状说清楚：漏了这半句，已经有人在做的活、以及已经做完待评审的活，会重新
    挂上任务板被第二个人领走再做一遍。今天 `cp.claim` 的状态校验兜住了它不落成
    脏状态，但那是第二道闸 —— 任务板自己的口径必须先对。
    """
    store, bus, cp = _boot()
    plan_id = _plan(cp, roles=("coding", "coding", "coding"))
    cp.start_plan(plan_id)
    still, running, reviewed = store.list_tasks(plan_id)

    cp.claim(running["task_id"], "w-busy", 1)                     # -> RUNNING
    w = _worker(bus, cp, worker_id="w-done")
    cp.claim(reviewed["task_id"], "w-done", 1)
    cp.on_task_result(E.task_result(                              # -> AWAITING_REVIEW
        plan_id=plan_id, task_id=reviewed["task_id"], attempt=1,
        trace_id=reviewed["trace_id"], status="ok",
        artifacts=[{"kind": "doc", "content": {}}], worker_id="w-done"))

    states = {t["task_id"]: t["state"] for t in store.list_tasks(plan_id)}
    assert states[running["task_id"]] == TaskState.RUNNING
    assert states[reviewed["task_id"]] == TaskState.AWAITING_REVIEW
    assert states[still["task_id"]] == TaskState.DISPATCHED, "前置：得留一条真的在板上"

    # 🔴 判过期的 now 必须从 _after 取：这两条租约是 cp.claim 用**真实时钟**登记的。
    #    拿 T_LATER 那种字面量去比，RUNNING 那条会被「租约还没过期」挡住，
    #    于是这条用例测的就成了租约过滤而不是状态过滤 —— 删掉状态过滤它照样绿。
    now = _after(TTL + 60)
    assert {r["task_id"] for r in cp.leases.expired(now)} >= {running["task_id"]}, (
        "前置不成立：RUNNING 那条的租约还没过期，它会被租约过滤挡住，"
        "本用例就测不到状态过滤了")

    rows = cp.leases.claimable(w.roles, now)

    leaked = [r["task_id"] for r in rows if r["state"] != TaskState.DISPATCHED]
    assert leaked == [], (
        f"非 DISPATCHED 的任务上了任务板：{[(states[t], t) for t in leaked]}"
        " —— 已经有人在做、或已经做完的活会被第二个人领走再做一遍")
    assert [r["task_id"] for r in rows] == [still["task_id"]]


def test_claimable_hides_live_lease_and_shows_expired_one():
    """有效租约的任务不在板上；租约一过期它自己回到板上。"""
    _store, _bus, cp = _boot()
    plan_id = _plan(cp, roles=("coding", "coding"))
    cp.start_plan(plan_id)
    held, free = cp.store.list_tasks(plan_id)
    cp.leases.grant(held["task_id"], 1, "w1", now_iso=T0)

    live = {r["task_id"] for r in cp.leases.claimable(frozenset({"coding"}), T0)}
    assert live == {free["task_id"]}, "有人正在干的任务还挂在板上，会被第二个人抢去重做"

    later = {r["task_id"] for r in cp.leases.claimable(frozenset({"coding"}), T_LATER)}
    assert later == {held["task_id"], free["task_id"]}, (
        "租约到期了任务却没回到板上 —— 失联的认领方把这条任务永久锁住了")


def test_claimable_compares_time_after_normalizing_the_offset():
    """判过期的时间比较必须先归一时区，不能拿字面量直接比。

    落库的 expires_at 是定宽 UTC 串，过期判定走的是 SQL 的**字符串**比较。所以
    now_iso 必须先归一到同一形态，否则比的是字面量而不是时刻。

    这条用例的构造是刻意挑的：`2026-09-07T20:01:00-08:00` 实际是 UTC
    `2026-09-08T04:01:00`，比租约的到期时刻（UTC `00:01:00`）**晚**四小时，
    但它的字面量以 `2026-09-07` 开头，字符串比较判它更**早**。两个方向正好相反 ——
    不归一就会把「早就到期」判成「还早着」，于是失联的认领方把任务永久锁住。

    ⚠️ 别用 `+08:00` 那种东半球偏移来测这个：北京时间的字面量前缀恰好仍排在
    正确的一侧，去掉归一照样绿。本文件第一版就是那么写的，变异实测把它照了出来。
    """
    _store, _bus, cp = _boot()
    _plan_id, task = _dispatched(cp)
    tid = task["task_id"]
    cp.leases.grant(tid, 1, "w1", now_iso=T0, ttl_s=60)   # UTC 00:01:00 到期

    west = "2026-09-07T20:01:00-08:00"                    # == UTC 2026-09-08T04:01:00
    assert parse_utc(west) > parse_utc(cp.leases.holder(tid)["expires_at"]), (
        "前置不成立：挑的这个时刻并不晚于租约到期时刻，这条用例就测不到东西")
    assert west < cp.leases.holder(tid)["expires_at"], (
        "前置不成立：它的字面量没有排在到期时刻之前，构造不出字面量与时序相反的情形")

    assert [r["task_id"] for r in cp.leases.expired(west)] == [tid], (
        "带时区偏移的 now_iso 没被归一，过期判定按字面量比出了错误答案")
    assert [r["task_id"] for r in cp.leases.claimable(frozenset({"coding"}), west)] == [tid], (
        "同一个洞在任务板这一侧：过期租约仍把任务藏着，没人领得走它")


def test_claimable_orders_by_created_at():
    """先建的任务排前面 —— 板上的顺序就是排队顺序，不是随机。"""
    store, _bus, cp = _boot()
    p_old = _plan(cp, trace_id="tr-old")
    p_new = _plan(cp, trace_id="tr-new")
    cp.start_plan(p_old)
    cp.start_plan(p_new)
    old = store.list_tasks(p_old)[0]["task_id"]
    new = store.list_tasks(p_new)[0]["task_id"]
    # 同一毫秒内建出来的两条 created_at 可能相同，显式拉开它们再断言排序
    store._conn.execute("UPDATE task SET created_at=? WHERE task_id=?",
                        ("2020-01-01T00:00:00+00:00", old))
    store._conn.execute("UPDATE task SET created_at=? WHERE task_id=?",
                        ("2030-01-01T00:00:00+00:00", new))
    store._conn.commit()

    rows = cp.leases.claimable(frozenset({"coding"}), T0)

    assert [r["task_id"] for r in rows] == [old, new], (
        "任务板没按 created_at 排序 —— 早排队的任务可能被永远插队")


def test_claimable_with_no_roles_is_empty():
    """一个 role 都不承接的 worker 看不到任何活（且不许炸在 SQL 的 IN () 上）。"""
    _store, _bus, cp = _boot()
    plan_id = _plan(cp)
    cp.start_plan(plan_id)
    assert cp.leases.claimable(frozenset(), T0) == []


def test_pull_and_claim_executes_and_replies():
    """拉模式跑通全程：认领 -> 执行 -> 交回，且交回走的是 _reply 那一条路径。"""
    store, bus, cp = _boot()
    _plan_id, task = _dispatched(cp)
    w = _worker(bus, cp, worker_id="w-puller")

    assert w.pull_and_claim(now_iso=T0) == 1

    assert w.agents["coding"].calls == [task["task_id"]]
    assert store.get_task(task["task_id"])["state"] == TaskState.RUNNING
    bus.drain()                                   # 让控制面收下那条 TaskResult
    assert store.get_task(task["task_id"])["state"] == TaskState.AWAITING_REVIEW, (
        "拉模式交回的结果没被控制面接受 —— worker_id 或 attempt 对不上")


def test_pull_and_claim_respects_limit():
    """limit 说了算：板上三条也只领 limit 条。"""
    _store, bus, cp = _boot()
    plan_id = _plan(cp, roles=("coding", "coding", "coding"))
    cp.start_plan(plan_id)
    w = _worker(bus, cp, worker_id="w-puller")

    assert w.pull_and_claim(now_iso=T0, limit=2) == 2
    assert len(w.agents["coding"].calls) == 2


def test_pull_and_claim_only_sees_its_own_roles():
    """拉模式同样只看得见自己干得了的活。"""
    _store, bus, cp = _boot()
    plan_id = _plan(cp, roles=("coding", "reviewer"))
    cp.start_plan(plan_id)
    w = _worker(bus, cp, worker_id="w-reviewer-only", roles=frozenset({"reviewer"}))

    assert w.pull_and_claim(now_iso=T0, limit=5) == 1
    assert w.agents["reviewer"].calls == [
        t["task_id"] for t in cp.store.list_tasks(plan_id) if t["role"] == "reviewer"]


def test_task_board_snapshot_does_not_grant_the_claim():
    """两个 worker 从**同一份快照**上各领一次，只有一个拿到任务。

    这一条挡的是「任务板顺手把 state 改成 RUNNING 并宣布认领成功」那种写法。
    快照是可以重叠的 —— 互斥只发生在 cp.claim 的幂等键上，任务板永远只是投影。
    """
    store, _bus, cp = _boot()
    plan_id, task = _dispatched(cp)
    tid = task["task_id"]

    snapshot_a = cp.leases.claimable(frozenset({"coding"}), T0)
    snapshot_b = cp.leases.claimable(frozenset({"coding"}), T0)
    assert [r["task_id"] for r in snapshot_a] == [tid]
    assert [r["task_id"] for r in snapshot_b] == [tid], (
        "两份快照本来就该重叠 —— 任务板不是认领口，前置不成立说明它已经在发许可了")

    got = [cp.claim(tid, "w-a", 1), cp.claim(tid, "w-b", 1)]

    assert len([g for g in got if g is not None]) == 1, (
        "同一条任务被两个 worker 同时领走 —— 互斥没了，这活会被做两遍")
    assert len(_transitions(store, plan_id, TaskState.DISPATCHED, TaskState.RUNNING)) == 1


def test_two_workers_racing_pull_and_claim_only_one_wins(monkeypatch):
    """并发版：两个线程同时 `pull_and_claim` 同一条任务，合计只处理一条。

    🔴 **栅栏卡在 `claimable` 返回之后、`cp.claim` 之前，不是卡在线程入口。**
    位置差这一步，这条用例的牙齿就掉光了：两者之间隔着 store 那把 RLock，
    先拿到锁的线程往往在后手取快照之前就已经把任务迁到 RUNNING —— 后手的任务板
    于是是**空的**，`pull_and_claim` 的 for 循环一次都没进，
    「抢输了就跳下一条」那条分支（`worker.py` 的 `if claimed is None: continue`）
    根本没被执行到。实测：栅栏在线程入口时，把那条分支变异成「抢输了也照做」，
    本用例 60 次里仍有 51 次绿（窗口只有 58% 打开）。

    栅栏靠**包住 `claimable`** 插进去，而不是把 `pull_and_claim` 的逻辑在测试里
    抄一遍：抄一遍的话这条用例测的是那份抄件，生产代码怎么改它都不会红
    （第一版就是这么写的，变异实测 40 次 0 红把它照了出来）。生产代码里不留
    测试钩子，同步点从它的协作者那一侧插入。
    """
    store, bus, cp = _boot()
    plan_id, _task = _dispatched(cp)
    w_a = _worker(bus, cp, worker_id="w-a")
    w_b = _worker(bus, cp, worker_id="w-b")

    barrier = threading.Barrier(2, timeout=10)
    got: list[int] = [0, 0]
    errs: list[BaseException] = []
    boards: list[int] = [0, 0]
    real_claimable = cp.leases.claimable

    def claimable_then_wait(roles, now_iso):
        """真快照照取，取完在栅栏上会合 —— 两个线程于是从同一份非空快照上同时认领。"""
        rows = real_claimable(roles, now_iso)
        barrier.wait()
        return rows

    monkeypatch.setattr(cp.leases, "claimable", claimable_then_wait)

    def race(idx: int, w: WorkerRuntime) -> None:
        try:
            boards[idx] = len(real_claimable(w.roles, T0))   # 只为记录窗口开没开
            got[idx] = w.pull_and_claim(now_iso=T0)
        except BaseException as exc:              # noqa: BLE001 —— 带回主线程
            errs.append(exc)

    threads = [threading.Thread(target=race, args=(i, w))
               for i, w in enumerate((w_a, w_b))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not any(t.is_alive() for t in threads), "有线程没在 10 秒内退出，疑似死锁"
    assert not errs, f"拉取过程抛异常：{errs!r}"
    assert boards == [1, 1], (
        f"竞争窗口没打开：两个线程各自看到的任务板大小 {boards}，应各有 1 条。"
        " 有一边是空的就说明它压根没进循环，这条用例什么都没测到")
    assert sum(got) == 1, f"两个 worker 合计领走 {sum(got)} 条，应为 1 条"
    assert len(_transitions(store, plan_id, TaskState.DISPATCHED, TaskState.RUNNING)) == 1
    assert len(w_a.agents["coding"].calls) + len(w_b.agents["coding"].calls) == 1, (
        "同一条任务被执行了两遍")


def test_pull_and_claim_losing_the_race_never_invokes_the_agent():
    """确定性版：任务还挂在板上、但认领必败 —— Agent 一次都不许被调。

    钉的是 `worker.py` 里「抢输了就跳下一条」那条分支
    （`if claimed is None: continue`）。上面那条并发用例靠线程调度去撞它，
    本条不靠：变异掉那一行（抢输了也照做执行），本条当场变红。

    **构造的是真实的那半个窗口。** `cp.claim` 的时序是
    「读状态 → 过幂等闸 → `_transit(RUNNING)`」，三步之间没有跨步的锁。抢先者
    刚烧掉 `claim:<tid>:<attempt>`、还没迁移完的那一瞬间，任务仍是 DISPATCHED、
    仍在任务板上，而后手过闸必败 —— 直接烧那个键复刻的就是这一刻。
    （拿「抢先者已经迁到 RUNNING」去构造是不行的：那样任务已经从板上消失，
    for 循环一次都不进，这条分支根本没被执行到。第一版就是这么写的。）

    与 `test_task_board_snapshot_does_not_grant_the_claim` 的差别要说清楚：
    那条直接调 `cp.claim` 两次，覆盖不到 `pull_and_claim` 里「抢输之后怎么办」
    这一段 —— 而「抢输了还是把活干了」正是「同一条任务被两个 worker 各做一遍」
    这个症状的唯一入口。
    """
    store, bus, cp = _boot()
    plan_id, task = _dispatched(cp)
    tid = task["task_id"]
    w = _worker(bus, cp, worker_id="w-late")

    # 抢先者过了幂等闸，尚未 _transit。
    assert store.claim_idempotency(f"claim:{tid}:1", "claim", tid) is None
    assert store.get_task(tid)["state"] == TaskState.DISPATCHED, "前置：任务仍是 DISPATCHED"
    assert [r["task_id"] for r in cp.leases.claimable(w.roles, T0)] == [tid], (
        "前置不成立：任务已不在板上，for 循环进不去，这条用例什么都测不到")

    assert w.pull_and_claim(now_iso=T0) == 0, (
        "认领失败却报告处理了任务 —— 抢输之后它还是把活干了")
    assert w.agents["coding"].calls == [], (
        "认领失败却调了 Agent —— 同一条任务会被两个 worker 各做一遍")
    assert _transitions(store, plan_id, TaskState.DISPATCHED, TaskState.RUNNING) == []
    assert store.get_task(tid)["state"] == TaskState.DISPATCHED


def test_pull_and_claim_without_lease_book_fails_loudly():
    """没接线时当场炸，不许返回 0 把「没接线」伪装成「暂时没活」。

    后者是一个正常状态，没人会去查它 —— 于是一个从没接上任务板的 worker 会
    安安静静地闲置到演示当天。
    """
    _store, bus, cp = _boot(leases=False)
    w = _worker(bus, cp, worker_id="w-unwired")
    with pytest.raises(RuntimeError, match="LeaseBook"):
        w.pull_and_claim(now_iso=T0)


# ======================================================================
# 六、LeaseBook 自身
# ======================================================================
def test_grant_overwrites_instead_of_stacking():
    """同一个 task_id 再次 grant 是覆盖：重派之后旧租约描述的那次认领已经作废。"""
    store, _bus, cp = _boot()
    book = cp.leases
    book.grant("t-1", 1, "w1", now_iso=T0)
    book.grant("t-1", 2, "w2", now_iso=T0)

    rows = store._conn.execute("SELECT COUNT(*) FROM claim_lease WHERE task_id=?",
                               ("t-1",)).fetchone()
    assert rows[0] == 1, "同一个任务堆了多条租约，到期回收会重复处置"
    assert book.holder("t-1")["worker_id"] == "w2"


def test_release_reports_whether_it_removed_anything():
    """release 的返回值要说实话 —— 调用方靠它判断「租约还在不在」。"""
    _store, _bus, cp = _boot()
    book = cp.leases
    book.grant("t-1", 1, "w1", now_iso=T0)
    assert book.release("t-1") is True
    assert book.release("t-1") is False
    assert book.holder("t-1") is None


def test_holder_does_not_judge_expiry():
    """holder 只回答「有没有」，不回答「过没过期」—— 判过期是调用方的事。"""
    _store, _bus, cp = _boot()
    cp.leases.grant("t-1", 1, "w1", now_iso=T0)
    assert cp.leases.holder("t-1") is not None
    assert cp.leases.expired(T_LATER)[0]["task_id"] == "t-1"


def test_expiry_boundary_is_inclusive():
    """expires_at == now 算过期。边界归哪一侧要有定论，否则它是偶发的。"""
    _store, _bus, cp = _boot()
    cp.leases.grant("t-1", 1, "w1", now_iso=T0, ttl_s=60)
    at_boundary = canon_iso("2026-09-08T00:01:00+00:00")
    assert [r["task_id"] for r in cp.leases.expired(at_boundary)] == ["t-1"]
    just_before = canon_iso("2026-09-08T00:00:59+00:00")
    assert cp.leases.expired(just_before) == []


def test_canon_iso_is_fixed_width_so_string_order_is_time_order():
    """定宽是过期判定的地基：SQL 比的是字符串，只有定宽才保证字典序 == 时间序。

    datetime.isoformat 在微秒为 0 时整段省略小数部分，于是同一时刻的两种写法
    长度不同；换个时区偏移（+08:00）字典序当场失效。
    """
    a = canon_iso("2026-09-08T00:00:00+00:00")
    b = canon_iso("2026-09-08T00:00:00.500000+00:00")
    c = canon_iso("2026-09-08T08:00:01+08:00")     # == UTC 00:00:01
    assert len(a) == len(b) == len(c), f"归一后长度不一致：{a} / {b} / {c}"
    assert a < b < c, f"字典序与时间序不一致：{a} / {b} / {c}"
    assert a.endswith("+00:00") and ".000000" in a


def test_lease_book_rejects_non_positive_ttl():
    """TTL 非正 = 租约生下来就过期，等于所有任务永远无人持有。当场炸。"""
    store = SqliteStore()
    store.init_schema()
    with pytest.raises(ValueError):
        LeaseBook(store, ttl_s=0)
    book = LeaseBook(store)
    with pytest.raises(ValueError):
        book.grant("t-1", 1, "w1", now_iso=T0, ttl_s=-1)


def test_lease_book_refuses_a_store_without_a_connection():
    """拿不到底层连接就当场说清楚，别去改冻结的 store.py。"""
    class NoConnStore:
        pass

    with pytest.raises(TypeError, match="claim_lease"):
        LeaseBook(NoConnStore())


# ======================================================================
# 七、收口（T107 复核）：批量回收、冻结任务、派发超时源
# ======================================================================
def test_two_exhausted_leases_in_one_plan_do_not_abort_the_batch():
    """同一个 plan 里两条租约同时耗尽额度 —— 第二条不许把整批回收打断。

    洞在哪：`_reap_one` 的 retry_exhausted 分支无条件调 `_fail_plan`。第一条把
    plan 迁到 FAILED 之后，第二条再来一次就是 `FAILED -> FAILED` —— 那不在
    `PLAN_TRANSITIONS` 里，抛 `IllegalTransition` 打断整批。

    后果比「抛个异常」重得多：**同批里已经合法处置完的任务会静默停摆**。
    它们的租约在循环里已经销掉了，而 `claim_lease` 表是回收唯一的入口，没有任何
    机制会再碰它们第二次；循环之后那次 `dispatch_ready` 也没跑到。
    所以断言分两层 —— 不抛异常，**且**那条无辜的任务真的被重新派出去了。
    """
    store, _bus, cp = _boot()
    plan_id = cp.create_plan(goal="批量回收", trace_id="tr-batch", tasks=[
        {"role": "coding", "title": f"任务-{i}", "inputs": {}, "acceptance": [],
         "effect_risk": "L", "max_attempts": 1} for i in range(3)])
    cp.start_plan(plan_id)
    doomed_a, doomed_b, bystander = store.list_tasks(plan_id)

    # 甲、乙认领后失联；attempt=1 == max_attempts=1，额度已耗尽。
    for t in (doomed_a, doomed_b):
        assert cp.claim(t["task_id"], f"w-{t['title']}", 1) is not None
    # 丙没人认领，手工 grant 一条会过期的租约 -> 它该走 claim_timeout 回队列。
    cp.leases.grant(bystander["task_id"], 1, "w-never-showed-up", now_iso=T0)

    assert cp.reap_expired_leases(now_iso=_after(TTL + 60)) == 3, (
        "有租约没被处置 —— 批量回收在中途断了")

    states = {t["task_id"]: t for t in store.list_tasks(plan_id)}
    assert states[doomed_a["task_id"]]["state"] == TaskState.FAILED
    assert states[doomed_b["task_id"]]["state"] == TaskState.FAILED, (
        "第二条耗尽额度的任务没被判死 —— 回收在它之前就断了")
    assert store.get_plan(plan_id)["state"] == PlanState.FAILED, "plan 只该被判死一次"

    bys = states[bystander["task_id"]]
    assert bys["state"] == TaskState.DISPATCHED and bys["attempt"] == 2, (
        f"无辜的第三条停在 {bys['state']}/attempt={bys['attempt']}：回收把它的租约"
        " 销掉了却没把它重新派出去 —— 再没有任何机制会碰它，它就此静默停摆")
    assert cp.leases.holder(bystander["task_id"]) is None


def test_plan_is_only_failed_once_even_across_two_reap_rounds():
    """跨两轮回收也只判死一次：第二轮撞见的是一个已经 FAILED 的 plan。

    上一条守的是同一批之内，这一条守的是跨批 —— 两者共用同一个判据，
    但只测其一的话，一个「用批内去重表」的实现也能过上一条。
    """
    store, _bus, cp = _boot()
    plan_id = cp.create_plan(goal="跨轮", trace_id="tr-2round", tasks=[
        {"role": "coding", "title": f"任务-{i}", "inputs": {}, "acceptance": [],
         "effect_risk": "L", "max_attempts": 1} for i in range(2)])
    cp.start_plan(plan_id)
    a, b = store.list_tasks(plan_id)

    cp.claim(a["task_id"], "w-a", 1)
    # 乙也认领了（-> RUNNING，派发超时源看不见它），但租约续得很长：第一轮不该动它。
    cp.claim(b["task_id"], "w-b", 1)
    cp.leases.grant(b["task_id"], 1, "w-b", now_iso=_after(0), ttl_s=86400)

    assert cp.reap_expired_leases(now_iso=_after(TTL + 60)) == 1, "第一轮只该处置甲"
    assert store.get_plan(plan_id)["state"] == PlanState.FAILED

    # 第二轮：乙此刻才失联，plan 早已是 FAILED。
    cp.leases.grant(b["task_id"], 1, "w-b", now_iso=T0)      # 租约改成早已到期
    assert cp.reap_expired_leases(now_iso=_after(TTL + 60)) == 1, (
        "第二轮回收抛异常或漏处置 —— plan 被判了第二次死")
    assert store.get_task(b["task_id"])["state"] == TaskState.FAILED


def test_reap_does_not_unfreeze_a_task_that_replan_retired():
    """租约回收不许把被重规划淘汰的任务解冻并重新派出去。

    洞在哪：`_is_frozen(task)` 的**唯一**判据是 `last_error == FROZEN_BY_REPLAN`，
    而 `_reap_one` 会把 `last_error` 覆写成 `lease_expired: ...`。于是租约一到期，
    任务当场解冻，`reap_expired_leases` 末尾那次 `dispatch_ready` 立刻把它重新
    派出去 —— 一个已被新方案明确淘汰的任务复活并再执行一遍（补丁再打一遍）。

    前置成立性：`_apply_replan` 冻结时**只打标**，不动状态、不销租约
    （`control_plane.py` 里 `update_task(..., last_error=FROZEN_BY_REPLAN)` 一行），
    所以「RUNNING + 活租约 + 已冻结」是一个真实可达的组合。
    """
    store, _bus, cp = _boot()
    plan_id, task = _dispatched(cp)
    tid = task["task_id"]
    cp.claim(tid, "w-yi", 1)

    store.update_task(tid, last_error=FROZEN_BY_REPLAN)   # 与 _apply_replan 同一动作
    assert cp.leases.holder(tid) is not None, "前置：冻结不销租约"
    assert cp.dispatch_ready(plan_id) == 0, (
        "前置不成立：dispatch_ready 本来就该拒绝派发冻结任务，两条入口才有口径可比")

    assert cp.reap_expired_leases(now_iso=_after(TTL + 60)) == 1

    after = store.get_task(tid)
    assert after["last_error"] == FROZEN_BY_REPLAN, (
        f"回收把冻结标记覆写成 {after['last_error']!r} —— 任务解冻了")
    assert after["state"] == TaskState.RUNNING and after["attempt"] == 1, (
        f"被淘汰的任务复活成 {after['state']}/attempt={after['attempt']}，会再执行一遍")
    assert _logs(store, plan_id, LEASE_EXPIRED)[0]["reason"] == REAP_STALE, (
        "冻结任务的处置该是 stale_lease（只销租约不迁移），与「任务已经往前走了」同口径")
    assert cp.leases.holder(tid) is None, "租约还是要销的 —— 不然每轮都重复回收它"


def test_claimable_hides_tasks_that_replan_retired():
    """被重规划冻结的任务不许出现在任务板上 —— 推、拉两条入口口径必须一致。

    `dispatch_ready` 明确跳过冻结任务；`claimable` 漏了同一条判据的话，
    队友会从板上把它领走并真的执行一遍。两条入口对「哪些任务可以做」给出
    相反的答案，而只有一条会被人注意到。
    """
    _store, bus, cp = _boot()
    plan_id = _plan(cp, roles=("coding", "coding"))
    cp.start_plan(plan_id)
    frozen, alive = cp.store.list_tasks(plan_id)
    cp.store.update_task(frozen["task_id"], last_error=FROZEN_BY_REPLAN)

    assert [r["task_id"] for r in cp.leases.claimable(frozenset({"coding"}), T0)] == [
        alive["task_id"]], "被重规划淘汰的任务还挂在任务板上，会被队友领走再做一遍"

    w = _worker(bus, cp, worker_id="w-puller")
    assert w.pull_and_claim(now_iso=T0, limit=5) == 1
    assert w.agents["coding"].calls == [alive["task_id"]], (
        "拉模式执行了一个已被重规划淘汰的任务")


def test_dispatched_task_with_no_lease_is_reaped_after_the_ttl():
    """派发之后从没人认领 -> 超时后走 claim_timeout 回队列。**它没有租约行。**

    这条守的是回收的第二个超时源（`REAP_SOURCE_DISPATCH`）。租约唯一的登记点是
    `cp.claim` 成功之后，而 DISPATCHED 的任务按定义还没被认领 —— 只认 claim_lease
    表的话，`(DISPATCHED, PENDING) -> claim_timeout` 这条冻结迁移在真实链路上
    **不可达**，只有测试手工 `grant()` 才造得出来。判据因此钉在
    「claim_lease 表 0 行」上：有租约的那条路径由
    `test_expired_lease_on_dispatched_task_uses_claim_timeout` 守着。
    """
    store, _bus, cp = _boot()
    plan_id, task = _dispatched(cp)
    tid = task["task_id"]

    assert store._conn.execute("SELECT COUNT(*) FROM claim_lease").fetchone()[0] == 0, (
        "前置不成立：这条任务有租约行，那走的是另一个超时源")
    assert cp.reap_expired_leases(now_iso=_after(1)) == 0, (
        "刚派发就被回收 —— TTL 没起作用，正常排队的任务会被当成无人认领")

    assert cp.reap_expired_leases(now_iso=_after(TTL + 60)) == 1

    moves = _transitions(store, plan_id, TaskState.DISPATCHED, TaskState.PENDING)
    assert len(moves) == 1 and moves[0]["reason"] == "claim_timeout"
    row = _logs(store, plan_id, LEASE_EXPIRED)[0]
    assert row["reason"] == REAP_CLAIM_TIMEOUT
    assert row["detail"]["source"] == REAP_SOURCE_DISPATCH, (
        f"超时源标错了：{row['detail']['source']} —— 下游分不清失联与没人来过")
    assert row["detail"]["worker_id"] == "", "没有任何 worker 碰过它，这里不该有名字"
    assert store.get_task(tid)["state"] == TaskState.DISPATCHED, "回收后应重新派发"
    assert store.get_task(tid)["attempt"] == 2


def test_a_role_nobody_can_do_is_rescued_instead_of_stalling_forever():
    """端到端：派了一个全队伍都不承接的 role —— 不许静默永久停摆。

    `on_assignment` 的静默跳过（不再回假 failed）是对的，但它把「立刻失败」换成了
    「没人应答」；换来的这个状态必须有人接管，否则就是把一个吵闹的坏换成了一个
    安静的坏 —— 任务永远 DISPATCHED、plan 永远 RUNNING，无死信、无异常、无告警。

    接管方是派发超时源。判据钉在**可观测**上：处置条数、`LeaseExpired` 那一行、
    以及 attempt 真的往前走了 —— 三者合起来才叫「响了」。
    """
    store, bus, cp = _boot()
    plan_id = cp.create_plan(goal="没人能干", trace_id="tr-orphan", tasks=[
        {"role": "devops", "title": "全队伍都不承接", "inputs": {}, "acceptance": [],
         "effect_risk": "L", "max_attempts": 3}])
    _worker(bus, cp, worker_id="w-coding", roles=frozenset({"coding"}))
    cp.start_plan(plan_id)
    bus.drain()

    tid = store.list_tasks(plan_id)[0]["task_id"]
    assert store.get_task(tid)["state"] == TaskState.DISPATCHED, (
        "前置：旁观者静默跳过，任务停在 DISPATCHED（不是 PENDING、不是 FAILED）")
    assert store._conn.execute("SELECT COUNT(*) FROM claim_lease").fetchone()[0] == 0, (
        "前置：没有任何租约行 —— 认领从没发生过")

    assert cp.reap_expired_leases(now_iso=_after(TTL + 60)) == 1, (
        "没人能干的任务永远捞不回来 —— 静默跳过成了永久静默停摆")

    assert _logs(store, plan_id, LEASE_EXPIRED)[0]["reason"] == REAP_CLAIM_TIMEOUT
    assert store.get_task(tid)["attempt"] == 2, "它得真的往前走一格，才叫响了一声"


def test_unclaimed_dispatched_skips_tasks_that_already_have_a_lease():
    """两个超时源不许重复处置同一条任务。

    有租约行的任务归 `expired()` 管；`unclaimed_dispatched` 必须把它们排除掉，
    否则一条 DISPATCHED + 活租约的任务会被派发超时源提前处置 —— 那等于把 TTL
    这件事做了两遍，而其中一遍不认租约。
    """
    _store, _bus, cp = _boot()
    plan_id = _plan(cp, roles=("coding", "coding"))
    cp.start_plan(plan_id)
    leased, bare = cp.store.list_tasks(plan_id)
    cp.leases.grant(leased["task_id"], 1, "w1", now_iso=T0)

    # now 必须从 _after 取：截止时刻算自 task.updated_at（真实时钟），
    # 拿 T0/T_LATER 那种字面量去比，两条任务都会被判成「还没到期」，
    # 于是返回空表，这条用例会因为错误的理由变绿。
    rows = cp.leases.unclaimed_dispatched(_after(TTL + 60))
    assert [t["task_id"] for t, _deadline in rows] == [bare["task_id"]], (
        "有租约的任务被派发超时源捞走了 —— 同一条任务会被两个源各处置一次")


def test_unclaimed_dispatched_boundary_is_inclusive_and_reads_updated_at():
    """截止时刻 = 进 DISPATCHED 那一刻 + TTL，边界算到期。

    读的是 `task.updated_at`，它出自 `store._now()`（`datetime.isoformat()`）——
    **不定宽**，字典序不等于时间序。所以这里的比较必须走 `parse_utc`，
    不能像 `expires_at` 那样交给 SQL 的字符串比较。
    """
    _store, _bus, cp = _boot()
    _plan_id, task = _dispatched(cp)
    dispatched_at = parse_utc(cp.store.get_task(task["task_id"])["updated_at"])

    just_before = canon_iso((dispatched_at + timedelta(seconds=TTL - 1)).isoformat())
    assert cp.leases.unclaimed_dispatched(just_before) == []

    at_boundary = canon_iso((dispatched_at + timedelta(seconds=TTL)).isoformat())
    assert [t["task_id"] for t, _d in cp.leases.unclaimed_dispatched(at_boundary)] == [
        task["task_id"]]


def test_unclaimed_dispatched_skips_frozen_tasks():
    """冻结任务不进派发超时源 —— 否则每轮回收都给它落一条处置不完的噪声。"""
    _store, _bus, cp = _boot()
    _plan_id, task = _dispatched(cp)
    now = _after(TTL + 60)                    # 理由同上：截止时刻算自真实时钟
    assert [t["task_id"] for t, _d in cp.leases.unclaimed_dispatched(now)] == [
        task["task_id"]], "前置：没冻结时它本来是被捞的，否则这条用例测不到东西"

    cp.store.update_task(task["task_id"], last_error=FROZEN_BY_REPLAN)

    assert cp.leases.unclaimed_dispatched(now) == []
    assert cp.reap_expired_leases(now_iso=now) == 0


def test_a_crash_mid_batch_still_dispatches_what_was_already_reaped(monkeypatch):
    """回收中途抛异常 —— 异常照旧往外抛，但**已经处置完的任务必须被派出去**。

    为什么不能只靠「别抛异常」了事：租约在循环里是**先销后处置**的，而
    `claim_lease` 表是回收唯一的入口。一条租约把异常抛出来，同批里已经合法回到
    PENDING 的任务就再没有任何机制会碰它们第二次 —— 它们的租约没了，`expired()`
    看不见它们，`unclaimed_dispatched` 也看不见（它们不是 DISPATCHED）。
    一条坏租约于是变成一整批任务静默停摆。所以那次 `dispatch_ready` 挂在
    `finally` 上，不是挂在循环之后。

    异常本身**不吞**：非法迁移说明有代码绕过了状态机（`states.py` 的
    `IllegalTransition` 自陈「不要 catch 掉」），吞了它等于把 bug 变成偶发的。
    这里用注入的方式造这一刻 —— 具体是哪种异常不重要，重要的是「有异常」时
    已完成的处置要兑现。
    """
    store, _bus, cp = _boot()
    plan_id = _plan(cp, roles=("coding", "coding"), max_attempts=3)
    cp.start_plan(plan_id)
    good, boom = store.list_tasks(plan_id)
    cp.claim(good["task_id"], "w-good", 1)
    cp.claim(boom["task_id"], "w-boom", 1)

    # 租约改成字面量时刻，好让 expired() 的顺序确定：good 先、boom 后。
    cp.leases.grant(good["task_id"], 1, "w-good", now_iso=T0, ttl_s=60)
    cp.leases.grant(boom["task_id"], 1, "w-boom", now_iso=T0, ttl_s=120)

    real = cp._reap_one

    def exploding(task, lease, **kw):
        if task["task_id"] == boom["task_id"]:
            raise RuntimeError("注入：处置第二条租约时炸了")
        return real(task, lease, **kw)

    monkeypatch.setattr(cp, "_reap_one", exploding)

    with pytest.raises(RuntimeError, match="注入"):
        cp.reap_expired_leases(now_iso=T_LATER)

    after = store.get_task(good["task_id"])
    assert after["state"] == TaskState.DISPATCHED and after["attempt"] == 2, (
        f"已经合法回到队列的任务停在 {after['state']}/attempt={after['attempt']}："
        " 中途那次异常把它的派发一起带走了，而它的租约已经销掉 ——"
        " 再没有任何机制会碰它，它就此静默停摆")
