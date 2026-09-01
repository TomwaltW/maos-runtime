"""状态迁移镜像 —— 把 event_log 里的迁移轨迹轮询进 Matrix 房间。

补的是 ``docs/BACKLOG.md`` ``## task-E`` 第 3 条：``MatrixEventBus`` 只镜像了
EventBus 三方法经手的**事件流**（TaskAssignment / TaskResult / ReviewVerdict /
Rework），而 ``RUNNING → AWAITING_REVIEW`` 这类**迁移轨迹**根本不走事件总线 ——
它由 ``ControlPlane._transit`` 直接写进 ``event_log``。结果就是房间里看得见事件、
看不见状态机在走。

挂法取「外挂轮询」而不是「给 ControlPlane 加回调」：**它一行生产代码都不动**。
``control_plane.py`` 本轮被 Y-2 持有，任何改动都是必撞的合并冲突（见
``docs/DECISIONS.md`` 的 ``## task-C3``）。

三条不变量，和 ``matrix_bus`` 抬头那三条同源：

1. **旁路，不是主路。** 轮询在独立守护线程里跑，任何异常都吞掉记日志。房间挂了、
   channel 抛了、store 读不出来了，流水线照跑 —— 镜像本来就只是给人看的。
2. **幂等靠行序，不靠时间戳。** 断点是 ``event_log.seq``（``list_event_log`` 已按
   它排序）。时间戳会撞（同一毫秒多条）、会回拨，而 seq 是 AUTOINCREMENT 主键。
3. **降级是 no-op，不是报错。** ``channel is None`` 时连 store 都不去查 ——
   降级模式是测试与 CI 的常态路径，在那条路上白烧一次全表扫描没有意义。
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Iterable, Sequence

from maos.contracts.events import Envelope

from hiclaw.matrix_bus import (MirrorChannel, RoomSendTimeout, describe_exc,
                               render_mirror)

log = logging.getLogger("maos.matrix")

#: 默认镜像哪些 event_log 事件类型。
#:
#: 两个都要：``StateTransition`` 是 Task 的迁移（BACKLOG 点名的那条），
#: ``PlanTransition`` 是 Plan 的迁移 —— 演示当天房间里最该看到的最后一行正是
#: ``PENDING → RUNNING → DONE``，少了它人类不知道这一轮到底收没收口。
#: 别的事件类型（SkillInvoked / CompensationAttached / ApprovalDenied）不在此列：
#: 它们不是迁移，混进来会把轨迹淹掉。
MIRRORED_EVENT_TYPES: tuple[str, ...] = ("StateTransition", "PlanTransition")

#: 轮询间隔。取 0.5s 不是为了实时，是为了 ``stop()`` 能在 1 秒内退出 ——
#: 演示当天 Ctrl-C 要干净，而线程醒来的粒度就是这个值的上界。
DEFAULT_INTERVAL = 0.5


def _seq_of(row: dict, index: int) -> int:
    """取这一行的断点值：优先 ``seq``，没有就退回 1-based 行序。

    退路是给「测试里塞的假 store」留的：``Store.list_event_log`` 的契约只保证
    **有序**，没保证每行都带 seq。两种断点在同一个 store 上恒定二选一，不会混。
    """
    seq = row.get("seq")
    return seq if isinstance(seq, int) else index


def render_transition(row: dict, *, attempt: int = 1) -> tuple[str, str]:
    """把一行 event_log 渲染成 ``(plain, html)``：一行人话 + 折叠 JSON。

    复用 ``matrix_bus.render_mirror``（内部即 ``summarize`` + 折叠 JSON），
    办法是拿这一行拼一个**合成 Envelope**：``event_log`` 的行不是 Envelope，
    但 ``Envelope`` 是纯 dataclass、``event_type`` 是自由 str，拼一个不碰冻结契约。
    这样房间里迁移消息和事件消息长得一模一样，人眼不用切换两套格式。

    ``topic`` 位塞的是迁移本身，于是摘要行读作::

        [task_xxx] StateTransition → RUNNING → AWAITING_REVIEW attempt=1

    ``attempt`` 由调用方给：``event_log`` 没有这一列（``_transit`` 把 attempt 当
    **任务字段**更新，没写进 detail），所以它只能从任务当前值读，取不到就退 1。
    这是摘要行里唯一一个**可能过期**的字段（读的是轮询那一刻的值，不是迁移那一刻），
    权威数据一律在折叠的 JSON 里 —— 那份是 event_log 的原样。
    """
    subject = row.get("task_id") or row.get("plan_id") or ""
    payload = {k: row.get(k) for k in
               ("seq", "from_state", "to_state", "reason", "detail", "created_at")}
    env = Envelope(
        event_type=row.get("event_type") or "",
        plan_id=row.get("plan_id") or "",
        task_id=subject,
        idempotency_key=f"mirror:{row.get('seq')}",
        payload=payload,
        event_id=row.get("event_id") or "",
        trace_id=row.get("trace_id") or "",
        attempt=attempt,
        occurred_at=row.get("created_at") or "",
    )
    return render_mirror(f"{row.get('from_state')} → {row.get('to_state')}", env)


class TransitionMirror:
    """event_log 迁移轮询器。``start()`` 起守护线程，``stop()`` 一秒内退。

    不持有 bus，只持有 ``store`` + ``channel``：轮询读的是库，跟总线没关系。
    要从一条 ``MatrixEventBus`` 上取 channel，用 :meth:`from_bus`。
    """

    def __init__(self, store: Any, plan_id: str, channel: MirrorChannel | None, *,
                 interval: float = DEFAULT_INTERVAL,
                 event_types: Sequence[str] = MIRRORED_EVENT_TYPES) -> None:
        self.store = store
        self.plan_id = plan_id
        self.channel = channel
        self.interval = interval
        self.event_types = tuple(event_types)
        self.mirrored = 0                 # 累计发出条数，测试与回执用
        self._cursor = 0                  # 已镜像到第几个 seq（含）
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- 构造 -------------------------------------------------------------
    @classmethod
    def from_bus(cls, bus: Any, store: Any, plan_id: str, **kw: Any) -> "TransitionMirror":
        """从 ``MatrixEventBus`` 上取 channel 构造；取不到就是降级，返回 no-op 实例。

        ``channel`` 只读属性由 C-2 在本轮补上，落地前只有私有 ``_channel``。
        两边都试是为了**两个次序都能跑**：C-2 的提交先到或后到，这一轨都不该红。
        """
        channel = getattr(bus, "channel", None)
        if channel is None:
            channel = getattr(bus, "_channel", None)
        return cls(store, plan_id, channel, **kw)

    # -- 单次轮询 ---------------------------------------------------------
    @property
    def muted(self) -> bool:
        return self.channel is None

    def poll_once(self) -> int:
        """镜像自上次以来的增量，返回**本次发出的条数**。任何异常都吞掉。

        断点在**每一行发完后**推进，且发失败也推进 —— 与
        ``MatrixEventBus._mirror`` 同口径：镜像失败就是丢一行，不重投。
        不这么做的话，一条发不出去的消息会在每一轮里重发，把房间刷成失败墙，
        而「重复轮询不重发同一条」这条幂等承诺当场破掉。
        """
        if self.muted:
            return 0
        try:
            rows = self.store.list_event_log(self.plan_id)
        except Exception as exc:                        # noqa: BLE001 —— 见不变量 1
            log.warning("迁移镜像读取 event_log 失败（%s），本轮跳过", describe_exc(exc))
            return 0

        sent = 0
        for index, row in enumerate(rows, start=1):
            # 断点先推进再发：发失败也算这一行处理过了，见本方法 docstring。
            # 整个循环体裹在 try 里 —— 一行坏数据（row 不是 dict、seq 是字符串）
            # 不该让后面那些好行也一起哑掉。
            try:
                seq = _seq_of(row, index)
                if seq <= self._cursor:
                    continue
                self._cursor = seq
                if row.get("event_type") not in self.event_types:
                    continue
                plain, html = render_transition(row, attempt=self._attempt_of(row))
                self.channel.send(plain, html)          # type: ignore[union-attr]
                sent += 1
            except RoomSendTimeout as exc:
                # 断点已经推进过了，所以不会重发 —— 这一条只决定**怎么记数**。
                # 不计进 sent：超时只说明「我不等了」，nio 后台大概率把它送到了，
                # 但「大概率」不能写进回执。于是 mirrored 是个**下界**，
                # 与 runbook 那条口径一致：唯一算数的判据是去房间里数消息。
                log.warning("迁移镜像超时（%s）；未计入条数，房间里很可能有这一条",
                            describe_exc(exc))
            except Exception as exc:                    # noqa: BLE001 —— 见不变量 1
                log.warning("迁移镜像发送失败（%s），已跳过该行，流水线不受影响", describe_exc(exc))
        self.mirrored += sent
        return sent

    def _attempt_of(self, row: dict) -> int:
        """读任务当前 attempt 给摘要行用；读不到一律退 1，绝不让它抛。"""
        task_id = row.get("task_id")
        if not task_id:
            return 1
        try:
            task = self.store.get_task(task_id)
        except Exception:                               # noqa: BLE001
            return 1
        attempt = (task or {}).get("attempt")
        return attempt if isinstance(attempt, int) and attempt > 0 else 1

    # -- 起停 -------------------------------------------------------------
    def start(self) -> None:
        """起守护线程。降级时直接返回 —— 起一条只会空转的线程没有意义。"""
        if self.muted or self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="matrix-transition-mirror", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        # wait() 而不是 sleep()：stop() 一置位立刻醒，退出延迟不受 interval 影响。
        while not self._stop.wait(self.interval):
            self.poll_once()

    def stop(self, *, timeout: float = 1.0, flush: bool = True) -> bool:
        """停轮询。``flush=True`` 时**退出前补最后一次轮询**。

        补这一次是必需的而非好看：审批放行到进程退出之间往往不足一个 interval，
        少了它房间里最后停在 ``BLOCKED``，看不到 ``BLOCKED → DONE`` 和 Plan 收口 ——
        而那正是演示要给人看的那一行。
        """
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():
                log.warning("迁移镜像线程 %.1fs 内未退出（守护线程，不阻塞进程退出）", timeout)
                # 活线程可能正卡在 channel.send；此刻补 poll 会与它并发，且调用方
                # 无从知道是否已经静止。保留线程引用，允许证据入口据返回值 fail closed。
                return False
            self._thread = None
        if flush:
            self.poll_once()
        return True

    # -- 上下文管理 -------------------------------------------------------
    def __enter__(self) -> "TransitionMirror":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()


def mirror_lines(rows: Iterable[dict], *,
                 event_types: Sequence[str] = MIRRORED_EVENT_TYPES,
                 attempt_of: Callable[[dict], int] | None = None) -> list[tuple[str, str]]:
    """把若干 event_log 行渲染成 ``(plain, html)`` 列表 —— 给降级模式打印用。

    没有房间时 ``room_demo`` 要把「本该发进房间的每一条」按原文打到 stdout，
    走这里就能保证**打印的和发的是同一份渲染**，而不是另写一遍格式。
    """
    out = []
    for row in rows:
        if row.get("event_type") not in event_types:
            continue
        attempt = attempt_of(row) if attempt_of is not None else 1
        out.append(render_transition(row, attempt=attempt))
    return out
