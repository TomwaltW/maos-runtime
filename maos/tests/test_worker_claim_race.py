"""并发认领竞争 —— 把「多 worker 一致性」从纸面变成一条断言。

## 为什么要有这个文件

`maos/core/control_plane.py::claim` 的幂等闸（`processed_key` 的 UNIQUE 约束）此前只有
**顺序**用例守着（`test_contracts.py::test_duplicate_claim_ignored`：先调一次再调一次）。
顺序用例证明不了并发安全 —— 把 `claim_idempotency` 里的
`INSERT ... except IntegrityError` 换成「先 SELECT 再 INSERT」的读改写，
顺序用例照样全绿，而两个线程同时进来会**双双拿到任务**。

本文件让两个及以上线程真的去抢同一个 `(task_id, attempt)`，断言只有一个赢。

## 判据为什么不是「返回值只有一个非 None」那一条

那一条是必要不充分的。真正要守的是**副作用只发生一次**：
`DISPATCHED -> RUNNING` 这条迁移在 `event_log` 里只许有一行。
只断言返回值的话，一个「两个线程都迁移了、但第二个返回 None」的实现也能过 ——
而那种实现会把 `worker_id` 覆盖成后到的那个，任务归属当场失真。

## 与铁律的关系

不新增表、不动契约、不改生产代码，纯新增测试（铁律 1 / 铁律 4）。
`SqliteStore` 用的是 `check_same_thread=False` 的单 connection + `threading.RLock`，
所以本文件测的是**同进程多线程**这一档；跨进程写同一个 db 文件不在本文件的射程内，
那一条仍然没有覆盖（记在 `docs/BACKLOG.md`）。
"""

from __future__ import annotations

import threading

from maos.contracts.states import TaskState
from maos.core.control_plane import ControlPlane
from maos.core.eventbus import InMemoryEventBus
from maos.core.store import SqliteStore

#: 抢同一个 key 的线程数。2 是最小竞争，8 是「压一压」——两档都跑，
#: 因为 2 个线程时很多错误实现靠运气也能过。
RACERS = (2, 8)


def _build() -> tuple[SqliteStore, InMemoryEventBus, ControlPlane]:
    store = SqliteStore()
    store.init_schema()
    bus = InMemoryEventBus()
    return store, bus, ControlPlane(store, bus)


def _dispatched_task(cp: ControlPlane) -> tuple[str, str]:
    """造一个已派发（DISPATCHED、attempt=1）的任务，返回 (plan_id, task_id)。"""
    plan_id = cp.create_plan(goal="并发认领测试", trace_id="trace-race", tasks=[{
        "role": "coding", "title": "被抢的那个任务", "inputs": {},
        "acceptance": [], "effect_risk": "L", "max_attempts": 3,
    }])
    cp.start_plan(plan_id)                      # PENDING -> DISPATCHED，attempt=1
    task = cp.store.list_tasks(plan_id)[0]
    assert task["state"] == TaskState.DISPATCHED, "前置不成立：任务没被派发"
    assert task["attempt"] == 1
    return plan_id, task["task_id"]


def _race_claim(cp: ControlPlane, task_id: str, n: int) -> list[object]:
    """n 个线程卡在同一个栅栏上，一起去 claim 同一个 (task_id, attempt=1)。

    用 `threading.Barrier` 而不是直接起线程：不同步的话线程往往被调度成串行，
    竞争窗口根本没打开，测试会变成一条伪装成并发的顺序用例。
    """
    barrier = threading.Barrier(n)
    results: list[object] = [None] * n
    errors: list[BaseException] = []

    def worker(idx: int) -> None:
        try:
            barrier.wait()
            results[idx] = cp.claim(task_id, f"w{idx}", 1)
        except BaseException as exc:            # noqa: BLE001 —— 线程里的异常要带回主线程
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not any(t.is_alive() for t in threads), "有线程没在 10 秒内退出，疑似死锁"
    assert not errors, f"认领过程抛异常：{errors!r}"
    return results


def _running_transitions(store: SqliteStore, plan_id: str, task_id: str) -> list[dict]:
    return [e for e in store.list_event_log(plan_id)
            if e.get("task_id") == task_id
            and e.get("from_state") == TaskState.DISPATCHED
            and e.get("to_state") == TaskState.RUNNING]


def test_only_one_thread_wins_the_same_claim():
    """返回值层面：n 个线程抢同一个 attempt，恰好一个拿到任务，其余全是 None。"""
    for n in RACERS:
        store, _bus, cp = _build()
        _plan_id, task_id = _dispatched_task(cp)

        results = _race_claim(cp, task_id, n)

        winners = [r for r in results if r is not None]
        assert len(winners) == 1, (
            f"{n} 个线程抢同一个 attempt，赢家应恰好 1 个，实得 {len(winners)} 个 —— "
            f"幂等闸的原子性被破坏了")
        assert winners[0]["task_id"] == task_id


def test_racing_claims_produce_exactly_one_running_transition():
    """副作用层面：`DISPATCHED -> RUNNING` 在 event_log 里只许有一行。

    这一条才是牙齿。只断言返回值的话，「两个线程都迁移了、第二个返回 None」的实现
    也能过上一条用例，而那种实现会把 worker_id 覆盖成后到的那个。
    """
    for n in RACERS:
        store, _bus, cp = _build()
        plan_id, task_id = _dispatched_task(cp)

        results = _race_claim(cp, task_id, n)

        moves = _running_transitions(store, plan_id, task_id)
        assert len(moves) == 1, (
            f"{n} 个线程竞争后 DISPATCHED->RUNNING 落了 {len(moves)} 行，应为 1 行")

        task = store.get_task(task_id)
        assert task["state"] == TaskState.RUNNING
        winner = next(r for r in results if r is not None)
        assert task["worker_id"] == winner["worker_id"], (
            "库里记的 worker_id 与赢家不一致 —— 任务归属被后到的线程覆盖了")


def test_racing_claims_burn_exactly_one_idempotency_key():
    """幂等键本身：竞争一轮后 `processed_key` 里这个 key 只有一行。

    多写一行不会立刻出错，但 `claim:<task>:<attempt>` 的唯一性是后续所有重复投递
    判定的地基；它被写坏时的症状是「偶发的重复认领」，离原因极远。
    """
    store, _bus, cp = _build()
    _plan_id, task_id = _dispatched_task(cp)

    _race_claim(cp, task_id, 8)

    rows = store._conn.execute(
        "SELECT idempotency_key, op FROM processed_key WHERE idempotency_key=?",
        (f"claim:{task_id}:1",)).fetchall()
    assert len(rows) == 1, f"同一个认领 key 落了 {len(rows)} 行，应为 1 行"
    assert rows[0][1] == "claim"


def test_losers_do_not_break_the_next_legal_claim_of_a_new_attempt():
    """输家不许污染下一轮：新 attempt 的合法认领必须仍然成功。

    对应 `claim()` 里那条红字回归守卫的另一半 —— 幂等键一旦被错误地烧掉，
    任务会永久卡死在 DISPATCHED。这里用「竞争一轮后再正常走一次 attempt=2」
    来证明上一轮的失败者没有多烧任何东西。
    """
    store, _bus, cp = _build()
    plan_id, task_id = _dispatched_task(cp)
    _race_claim(cp, task_id, 8)

    # 手动把任务退回 DISPATCHED 的下一轮：走真实迁移，不直接改库
    task = store.get_task(task_id)
    cp._transit(task, TaskState.AWAITING_REVIEW)
    task = store.get_task(task_id)
    cp._transit(task, TaskState.REWORK)
    task = store.get_task(task_id)
    cp._transit(task, TaskState.PENDING)
    assert cp.dispatch_ready(plan_id) == 1
    assert store.get_task(task_id)["attempt"] == 2

    claimed = cp.claim(task_id, "w-next", 2)
    assert claimed is not None, (
        "上一轮竞争的失败者烧掉了不该烧的东西 —— attempt=2 的合法认领被误拒，"
        "任务会永久卡死在 DISPATCHED")
    assert store.get_task(task_id)["state"] == TaskState.RUNNING
