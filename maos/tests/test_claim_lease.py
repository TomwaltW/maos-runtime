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
    LEASE_EXPIRED,
    REAP_CLAIM_TIMEOUT,
    REAP_RETRY,
    REAP_RETRY_EXHAUSTED,
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
    assert all(r["state"] == TaskState.DISPATCHED for r in rows)
    assert len(store.list_tasks(p1)) == 2, "前置：p1 里确实有一条 reviewer 任务被滤掉了"


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


def test_two_workers_racing_pull_and_claim_only_one_wins():
    """并发版：两个线程同时 pull_and_claim 同一条任务，合计只处理一条。

    用栅栏而不是直接起线程：不同步的话线程往往被调度成串行，竞争窗口根本没打开，
    这条用例会退化成一条伪装成并发的顺序用例。
    """
    store, bus, cp = _boot()
    plan_id, _task = _dispatched(cp)
    w_a = _worker(bus, cp, worker_id="w-a")
    w_b = _worker(bus, cp, worker_id="w-b")

    barrier = threading.Barrier(2)
    got: list[int] = [0, 0]
    errs: list[BaseException] = []

    def race(idx: int, w: WorkerRuntime) -> None:
        try:
            barrier.wait()
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
    assert sum(got) == 1, f"两个 worker 合计领走 {sum(got)} 条，应为 1 条"
    assert len(_transitions(store, plan_id, TaskState.DISPATCHED, TaskState.RUNNING)) == 1
    assert len(w_a.agents["coding"].calls) + len(w_b.agents["coding"].calls) == 1, (
        "同一条任务被执行了两遍")


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
