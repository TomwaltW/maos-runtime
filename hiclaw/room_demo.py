"""房间审批独立入口 —— 一次完整的「高风险任务停在 BLOCKED，等房间里的人放行」。

    ~/.maos-matrix/venv/bin/python -m hiclaw.room_demo --case approve [--timeout 300]
    ~/.maos-matrix/venv/bin/python -m hiclaw.room_demo --case reject  [--timeout 300]
    python3 -m hiclaw.room_demo --case approve --auto-approve   # 无房间自检，降级走完全程

**要进真房间就必须用那个 venv 解释器**：系统 python3 没装 matrix-nio，通道构造即
失败、当场降级 —— 而降级的终端输出与真房间的输出**形态一模一样**。本入口因此在
「MATRIX_* 配齐了却没接通房间」时**非 0 退出**（``EXIT_NO_ROOM``），不再跑完 exit=0；
真要做无房间自检，显式加 ``--allow-degraded``。

## 为什么是独立入口，而不是给场景 3 加超时等待

``docs/BACKLOG.md`` ``## task-E`` 第 5 条留了个口子：场景 3 现在同步跑完就退出，
接了房间审批就得阻塞等人。**编排侧定案：都不选，走独立入口。**
理由是**文件归属而非技术优劣** —— ``maos/flows/**`` 本轮四个文件全在 Y 轨手里
（Y-1 ``common.py``、Y-2 ``scenario_5/6.py``+``control_plane.py``、Y-4 ``scenario_7.py``），
任何改动都是必撞的合并冲突。

## 三个必须一起成立的东西

1. **审批口径照库内现成写法**：``HumanApprovalQueue(store, cp)`` 构造，
   ``hq.decide(task_id, approved=..., operator=..., note=...)`` 调用。形状对不上就
   等于没接 —— 库里三处对照见 ``scenario_3.py:34/38``、``scenario_6.py:245/260``、
   ``scenario_7.py:301/318/351``。
2. **降级必须能走完全程**：没房间时（``channel is None``）把本该发进房间的每一条
   按原文打到 stdout，``--auto-approve`` 内置模拟审批走完，exit=0。这不是方便，
   是判据：它让 runbook 和测试都不依赖 Synapse 起没起来。
3. **超时不许伪装成成功**：等不到审批就非 0 退出。

## ``--case reject`` 为什么开工就检查 MAOS_SANDBOX_WORKDIR

驳回会走到 ``ControlPlane._execute_compensation``，而它**缺 workdir 一律硬失败**
（``docs/BACKLOG.md`` 的 ``## merge-p2`` 第 3 条：缺省改必填，与 C-5「补偿必须硬失败」
同口径，有回归测试守着）。不在启动时拦，症状就变成「人在 Element 里打完 /reject
才收到一句『审批未生效』」—— 演示当天最不该出现的那种发现时机。
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from html import escape as _esc
from pathlib import Path
from typing import Callable

from maos.agents.testing import make_test_report, seed_scripted_report
from maos.config import attach_config_audit
from maos.contracts.events import Envelope, new_id
from maos.contracts.states import TaskState
from maos.core.control_plane import ENV_SANDBOX_WORKDIR
from maos.flows.common import GOOD_PATCH, build, run_until_settled
from maos.runtime.gate import HumanApprovalQueue

from hiclaw.matrix_bus import (DEGRADE_CONNECT, DEGRADE_DEPS, DEGRADE_ENV, VENV_PYTHON,
                               MatrixBusConfig, describe_exc, render_mirror,
                               RoomApprovalBridge, RoomSendTimeout)
from hiclaw.transition_mirror import TransitionMirror

log = logging.getLogger("maos.matrix")

#: 降级 + ``--auto-approve`` 时用的模拟审批人。只在**没有房间**时才注入到本进程
#: 自己的 config 副本里，绝不写文件、也不碰真房间的任何 ACL。
DEMO_APPROVER = "@demo:local"

GOAL = "高风险变更需人工放行（房间审批演示）"
TASK_TITLE = "变更生产环境配置"

#: 与 ``flows/scenario_3.py`` 同性质的预置报告：本入口演的是**审批闸**，
#: 测试链路不是它要证明的东西。本地定义而不 import scenario_3 —— 那个文件归 Y-4/Y-2，
#: 从它身上取常量等于给本轨接一条随时会被别人改动的隐性依赖。
PASS_REPORT = make_test_report(
    passed=1, failed=0, errors=0, duration=0.11,
    cases=[{"id": "tests/test_config.py::test_prod_config", "status": "passed", "msg": ""}],
    summary="沙箱回归：1 过 0 挂 0 错",
)

EXIT_OK = 0
EXIT_TIMEOUT = 2
EXIT_PRECONDITION = 3
#: 想进房间、但没进成。与 ``EXIT_PRECONDITION`` 分开是因为处置完全不同：3 是
#: 「你还没准备好」（补个 env、建个目录），4 是「你以为进房间了，其实没有」——
#: 后者最贵的地方在于**它原来返回 0**。
EXIT_NO_ROOM = 4

#: 真证据入口允许 Matrix 自己的 30s 发送超时完整落地，再留 15s 收口余量。
_EVIDENCE_SETTLE_TIMEOUT = 45.0

_REPO_ROOT = Path(__file__).resolve().parents[1]

#: 每种降级原因对应的一句人话与下一步。``DEGRADE_ENV`` 不在表里：那一档是明确的
#: 降级意图（四个 env 一个都没配），照旧放行。
_DEGRADE_SAYS = {
    DEGRADE_DEPS: ("当前解释器没装 matrix-nio，房间根本没接通。\n"
                   f"  改用装了它的那个重跑：{VENV_PYTHON} -m hiclaw.room_demo ..."),
    DEGRADE_CONNECT: ("MATRIX_* 配齐了，但房间没接通（连不上 / token 失效 / 撞加密房）。\n"
                      "  照 docs/matrix-room-runbook.md §7 逐条对。"),
}


def selfcheck_line() -> str:
    """开工第一行：把「这一轮是用哪个解释器跑的」摆到台面上。

    这套链路唯一一个**看终端分辨不出来**的失效形态就是解释器用错 —— 降级后的输出
    与真房间的输出形态一模一样（runbook 抬头那一节）。不靠人记得去查，开工就打。
    """
    try:
        import nio                                       # noqa: F401 —— 只为验能不能导入
    except ImportError as exc:
        return (f"[自检] 解释器 {sys.executable}\n"
                f"       matrix-nio 不可导入（{describe_exc(exc)}）—— 进不了真房间")
    return (f"[自检] 解释器 {sys.executable}\n"
            f"       matrix-nio {_nio_version()} 可导入")


def _nio_version() -> str:
    """装的是哪个版本。走 importlib.metadata 而不是 ``nio.__version__`` ——
    matrix-nio 这个包**没有** ``__version__`` 属性，``getattr(..., '?')`` 会恒取到
    问号，而自检行是要被截进证据的：一个恒定的 `?` 等于这一栏白写。
    """
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("matrix-nio")
    except PackageNotFoundError:                         # 源码树里直接跑，没装成包
        return "版本未知"


# --------------------------------------------------------------------------
# 降级通道
# --------------------------------------------------------------------------
class StdoutChannel:
    """降级通道：把本该发进房间的每一条**按原文**打到 stdout。

    形状与 ``MirrorChannel`` 一致（``send`` / ``close``），另有一个 no-op 的
    ``listen`` —— 没有房间就没有消息进来，但调用方不该为此分两条代码路径写。

    打的是 ``plain`` 全文（含折叠的 JSON），不是摘要行：降级模式是 C-4 写 runbook
    的唯一依据，摘要行看不出 Envelope 里到底有什么。
    """

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, plain: str, html: str) -> None:
        self.sent.append((plain, html))
        print(f"\n----- 房间消息 -----\n{plain}")

    def listen(self, on_message) -> None:               # noqa: ANN001 —— 形状对齐即可
        log.info("降级模式：无房间可监听，listen() 为 no-op")

    def close(self) -> None:
        pass


def bus_channel(bus):                                   # noqa: ANN001
    """从总线上取房间通道；取不到返回 None（= 降级）。

    ``channel`` 只读属性由 C-2 在本轮补上，落地前只有私有 ``_channel``。两边都试，
    是为了 C-2 的提交先到或后到本轨都不该红。
    """
    channel = getattr(bus, "channel", None)
    if channel is None:
        channel = getattr(bus, "_channel", None)
    return channel


# --------------------------------------------------------------------------
# 审批卡
# --------------------------------------------------------------------------
def approval_card(task: dict, *, expected_approved: bool | None = None) -> tuple[str, str]:
    """待审批任务的房间卡片：一行人话 + 折叠 Envelope JSON + 明确写出可用指令。

    指令必须**逐字写在卡片里**：房间里的人不会去翻文档，也不该去猜 task_id 从哪抄。
    渲染复用 ``matrix_bus.render_mirror``，房间里所有消息因此长得一模一样。
    """
    task_id = task["task_id"]
    env = Envelope(
        event_type="HumanApprovalRequired",
        plan_id=task["plan_id"],
        task_id=task_id,
        idempotency_key=f"approval:{task_id}",
        payload={
            "title": task["title"],
            "state": task["state"],
            "risk_level": task["risk_level"],
            "effect_risk": task["effect_risk"],
            "acceptance": task.get("acceptance", []),
            "inputs": task.get("inputs", {}),
        },
        trace_id=task.get("trace_id", ""),
        attempt=task.get("attempt", 1),
    )
    plain, html = render_mirror("待人工审批", env)
    if expected_approved is None:
        plain += (f"\n可用指令：\n"
                  f"  /approve {task_id}\n"
                  f"  /reject {task_id} [原因]")
        html += ("<p>可用指令：</p><ul>"
                 f"<li><code>/approve {_esc(task_id)}</code></li>"
                 f"<li><code>/reject {_esc(task_id)} [原因]</code></li></ul>")
    elif expected_approved:
        plain += (f"\nP8 采证前置：先由名单外账号发送 /approve {task_id}，"
                  f"确认收到“无审批权限”；再由审批人执行：\n"
                  f"  /approve {task_id} [备注]")
        html += (f"<p>P8 采证前置：先由名单外账号发送 "
                 f"<code>/approve {_esc(task_id)}</code>，确认收到“无审批权限”；"
                 f"再由审批人执行：</p><ul>"
                 f"<li><code>/approve {_esc(task_id)} [备注]</code></li></ul>")
    else:
        plain += f"\n本剧情可用指令：\n  /reject {task_id} <原因，必填>"
        html += ("<p>本剧情可用指令：</p><ul>"
                 f"<li><code>/reject {_esc(task_id)} &lt;原因，必填&gt;</code></li></ul>")
    return plain, html


# --------------------------------------------------------------------------
# 演示本体
# --------------------------------------------------------------------------
def seed_blocked_task(store, cp, bus, gate) -> tuple[str, dict]:   # noqa: ANN001
    """起一个 ``effect_risk=H`` 的任务，跑到 Gate 过闸后停在 BLOCKED。

    公开而不是 ``_`` 打头：``test_room_wiring.py`` 要拿它造同一个前置状态。
    测试自己再抄一遍等于留第二条构造路径，两条一定会漂。
    """
    plan_id = cp.create_plan(goal=GOAL, trace_id=new_id("trace"), tasks=[{
        "role": "coding", "title": TASK_TITLE,
        "inputs": {"repo": "demo/app"}, "acceptance": ["build 通过"],
        "risk_level": "M",     # Agent 产出补丁是 M 级，在其授权内
        "effect_risk": "H",    # 但这个补丁合进生产是 H 级，必须人工放行
    }])
    for task in cp.store.list_tasks(plan_id):
        seed_scripted_report(store, plan_id=plan_id, task_id=task["task_id"],
                             attempt=1, report=PASS_REPORT)
    cp.start_plan(plan_id)
    run_until_settled(bus, gate, cp, plan_id)

    hq = HumanApprovalQueue(store, cp)
    pending = hq.pending(plan_id)
    if len(pending) != 1:
        raise RuntimeError(f"高风险任务应恰好停在 1 个 BLOCKED，实际 {len(pending)} 个")
    return plan_id, pending[0]


def _check_preconditions(case: str) -> str:
    """启动即检查前置条件；不满足返回一句人话，满足返回 ``""``。

    只在 ``--case reject`` 上检查 workdir：approve 走不到补偿，拦它是无谓的门槛。
    目录**必须已存在**：``sandbox_git_apply`` 对不存在的目录返回
    ``stage=prepare`` 的失败，那会让驳回演示看起来像「补偿坏了」而不是「没配目录」。
    """
    if case != "reject":
        return ""
    workdir = (os.environ.get(ENV_SANDBOX_WORKDIR) or "").strip()
    if not workdir:
        return (f"--case reject 必须先设 {ENV_SANDBOX_WORKDIR} —— 驳回会走补偿执行器，"
                f"而它缺 workdir 一律硬失败（这是有意设计，不是 bug）。\n"
                f"  export {ENV_SANDBOX_WORKDIR}=/private/tmp/maos-sb-c3 "
                f"&& mkdir -p /private/tmp/maos-sb-c3")
    if not os.path.isdir(workdir):
        return (f"{ENV_SANDBOX_WORKDIR}={workdir} 不是一个已存在的目录 —— "
                f"补偿会在 stage=prepare 上失败，看起来像补偿坏了。先 mkdir -p 它。")
    return ""


#: 「等审批」的轮询粒度。它同时是超时判定的粒度：太粗会让 ``--timeout`` 明显不准，
#: 太细在 300s 预算上纯属空转。
_DECISION_POLL = 0.2


def _left_blocked(store, task_id: str) -> bool:          # noqa: ANN001
    """库里那个任务还在不在 BLOCKED。**这是判定是否生效的唯一判据。**"""
    task = store.get_task(task_id)
    return bool(task and task["state"] != TaskState.BLOCKED)


def wait_for_decision(store, task_id: str,               # noqa: ANN001
                      decided: threading.Event, timeout: float) -> bool:
    """等审批落地。**判据是库里的任务状态，不是回调跑完了没有。**

    ``on_message`` 里的 ``decided.set()`` 排在 ``bridge.handle_message()`` **之后**，
    而后者要同步把回执发进房间 —— Synapse 的 ``rc_message`` 限流下这一步实测撞满
    30s ``RoomSendTimeout``（三幕演示九次判定，次次如此）。只等 ``decided`` 的话，
    **落在预算最后 30s 里的审批会被报成超时，而它其实早就生效了**：``hq.decide()``
    在发回执之前就跑完了，库里那一刻已经是终态。

    这正是本入口最不该有的那种失效形态 —— 与抬头第 3 条「超时不许伪装成成功」
    互为反面：成功也不许伪装成超时。两者的共同判据都是**库**，不是终端上的话术。

    所以这里两个判据取先到的那个：回调置位，或库里任务已经离开 BLOCKED。
    """
    deadline = time.monotonic() + timeout
    while True:
        remaining = max(deadline - time.monotonic(), 0.0)
        if decided.wait(min(_DECISION_POLL, remaining)):
            return True
        if _left_blocked(store, task_id):
            return True
        if time.monotonic() >= deadline:
            return False


class RefundRoomDecisionTimeout(TimeoutError):
    """退款任务在预算内没有收到预期的房间人工命令。"""


class _RefundDecisionQueue:
    """把通用房间桥的 ``decide`` 形状接到场景 7 自己的业务收口。"""

    def __init__(self, store, task_id: str, expected_approved: bool,  # noqa: ANN001
                 apply_decision: Callable[[bool, str, str], None],
                 on_applied: Callable[[str, bool, str, str], None]) -> None:
        self.store = store
        self.task_id = task_id
        self.expected_approved = expected_approved
        self.apply_decision = apply_decision
        self.on_applied = on_applied
        self._consumed = False

    def decide(self, task_id: str, approved: bool, operator: str, note: str = "") -> None:
        if task_id != self.task_id:
            raise LookupError(f"当前待审批任务是 {self.task_id}，不是 {task_id}")
        if approved != self.expected_approved:
            expected = "/approve" if self.expected_approved else "/reject"
            raise ValueError(f"当前剧情只接受 {expected} {self.task_id}")
        if not approved and not note.strip():
            raise ValueError(f"/reject {self.task_id} 必须携带处置原因")
        if self._consumed:
            raise RuntimeError(f"{self.task_id} 已消费过一条合法房间命令，拒绝重复执行")
        # executor 单线程串行调用；在任何业务写入之前先烧掉本卡的一次性闸。
        # apply_decision 若中途失败也不重开，避免第二次命令叠加半份审批记录。
        self._consumed = True
        self.apply_decision(approved, operator, note)
        self.on_applied(task_id, approved, operator, note)


class RefundRoomDecisionDriver:
    """把场景 7 的两个既有 BLOCKED 点交给同一间 Matrix 房间。

    场景层通过 ``decision_hook`` 把真正的业务收口函数交进来；本类只负责发卡、
    收命令、名单校验、等待库状态与迁移镜像，不复制任何退款 DAG 或网关逻辑。
    """

    def __init__(self, *, store, bus, channel, config: MatrixBusConfig,  # noqa: ANN001
                 timeout: float) -> None:
        self.store = store
        self.bus = bus
        self.channel = channel
        self.config = config
        self.timeout = timeout
        self._bridge: RoomApprovalBridge | None = None
        self._task_id = ""
        self._decided = threading.Event()
        self._mirror: TransitionMirror | None = None
        self._finished = False
        self._condition = threading.Condition()
        self._inflight = 0
        self._accepted_room_decisions: list[dict] = []
        # nio 在自己的事件循环线程调用 on_message。完整的命令处理（包括业务状态推进
        # 与房间回执）都必须离开那个线程；单 worker 同时保留 Matrix 到达顺序。
        self._command_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="matrix-refund-command")

    def _record_accepted(self, task_id: str, approved: bool,
                         operator: str, note: str) -> None:
        with self._condition:
            self._accepted_room_decisions.append({
                "task_id": task_id,
                "approved": approved,
                "operator": operator,
                "note": note,
            })

    def on_message(self, sender: str, body: str) -> None:
        """nio 回调只入单线程队列；业务与回话都不占用 nio 事件循环。"""
        with self._condition:
            bridge = self._bridge
            task_id = self._task_id
            if bridge is None:
                return
            self._inflight += 1
        try:
            self._command_executor.submit(
                self._process_message, bridge, task_id, sender, body)
        except BaseException:
            with self._condition:
                self._inflight -= 1
                self._condition.notify_all()
            raise

    def _process_message(self, bridge: RoomApprovalBridge, task_id: str,
                         sender: str, body: str) -> None:
        """在专用 worker 中串行处理一条房间消息。"""
        try:
            reply = bridge.handle_message(sender, body)
        except Exception as exc:                         # noqa: BLE001
            log.warning("处理退款房间命令失败（%s），监听继续", describe_exc(exc))
        else:
            if reply:
                print(f"[房间回执] {reply}")
        finally:
            try:
                current = self.store.get_task(task_id)
                if current and current["state"] != TaskState.BLOCKED:
                    self._decided.set()
            finally:
                with self._condition:
                    self._inflight -= 1
                    self._condition.notify_all()

    def __call__(self, task: dict, expected_approved: bool,
                 apply_decision: Callable[[bool, str, str], None]) -> None:
        """发一张真实退款审批卡并阻塞，直到库内任务离开 BLOCKED。"""
        plan_id, task_id = task["plan_id"], task["task_id"]
        with self._condition:
            if self._bridge is not None:
                raise RuntimeError(f"上一张审批卡 {self._task_id} 尚未收口")

        if self._mirror is None:
            self._mirror = TransitionMirror(self.store, plan_id, self.channel)
            self._mirror.poll_once()
            self._mirror.start()
        elif self._mirror.plan_id != plan_id:
            raise RuntimeError("退款房间驱动不允许在同一轮里切换 plan_id")

        queue = _RefundDecisionQueue(
            self.store, task_id, expected_approved, apply_decision, self._record_accepted)
        with self._condition:
            self._task_id = task_id
            self._decided = threading.Event()
            # handle_message 会推进业务并同步回话；on_message 已把它整体移出 nio 线程。
            self._bridge = RoomApprovalBridge(queue, self.config, channel=self.channel)

        if expected_approved:
            print(f"\n[真人房间待命] 先由名单外账号发送 /approve {task_id}，"
                  f"确认“无审批权限”；再由审批人发送 /approve {task_id} [备注]")
        else:
            print(f"\n[真人房间待命] 请发送 /reject {task_id} <原因，必填>")
        try:
            self.channel.send(*approval_card(task, expected_approved=expected_approved))
        except RoomSendTimeout as exc:
            log.warning("退款审批卡发送超时（%s）；继续按库状态等待，勿重复发命令",
                        describe_exc(exc))

        got = wait_for_decision(
            self.store, task_id, self._decided, self.timeout)
        # 先关入口，再等已经进入业务落库的回调完整收口。否则可能先报超时，
        # 随后回调才把 approval_record 与任务状态写进库，形成半审计。
        with self._condition:
            self._bridge = None
            while self._inflight:
                self._condition.wait(_DECISION_POLL)
        if not got and _left_blocked(self.store, task_id):
            got = True
        if not got:
            raise RefundRoomDecisionTimeout(
                f"{task_id} 在 {self.timeout:.0f}s 内未收到预期的真人房间命令")

        expected_state = TaskState.DONE if expected_approved else TaskState.FAILED
        actual = self.store.get_task(task_id)["state"]
        if actual != expected_state:
            raise RuntimeError(
                f"{task_id} 房间决策剧情不符：期望 {expected_state}，实际 {actual}")

    def finish(self) -> None:
        """幂等收口并补推审批后的最后几条迁移与后台发送。"""
        if self._finished:
            return
        with self._condition:
            self._bridge = None
            while self._inflight:
                self._condition.wait(_DECISION_POLL)
            self._finished = True
        self._command_executor.shutdown(wait=True)
        try:
            self.bus.drain()
        finally:
            if self._mirror is not None:
                quiescent = self._mirror.stop(timeout=_EVIDENCE_SETTLE_TIMEOUT)
                if not quiescent:
                    raise RuntimeError(
                        "迁移镜像未在 45s 内静止，拒绝生成退款房间审计")
        # ``_NioChannel.send`` 超时不取消底层协程：429 退避后消息仍可能送达。
        # 审计必须排在这些后台发送之后，否则审计已落盘而 transcript 还少尾消息。
        flush = getattr(self.channel, "flush_pending_sends", None)
        if flush is not None and not flush(timeout=_EVIDENCE_SETTLE_TIMEOUT):
            raise RuntimeError("Matrix 后台发送未在 45s 内收口，拒绝生成退款房间审计")

    @property
    def mirrored(self) -> int:
        return self._mirror.mirrored if self._mirror is not None else 0

    @property
    def accepted_room_decisions(self) -> list[dict]:
        with self._condition:
            return [dict(row) for row in self._accepted_room_decisions]


def _evidence_sha() -> str:
    """取当前提交；工作树非空时显式带 ``-dirty``，不伪装成干净运行。"""
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True,
        cwd=_REPO_ROOT,
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain"], check=True, capture_output=True, text=True,
        cwd=_REPO_ROOT,
    ).stdout.strip()
    return f"{sha}-dirty" if dirty else sha


def validate_refund_room_result(store, plan_id: str,                       # noqa: ANN001
                                decisions: list[dict]) -> dict:
    """联合核验命令、业务记录与状态机；缺任一锚点都不允许写成功审计。"""
    from maos.domain.refund import objects
    from maos.flows import scenario_7 as s7

    expected = [
        (s7.TASK_FINANCE_2, True),
        (s7.TASK_PAYMENT_2, False),
    ]
    actual = [(row.get("task_id"), row.get("approved")) for row in decisions]
    if actual != expected:
        raise RuntimeError(f"真人房间命令序列应为 {expected}，实际 {actual}")
    if any(not str(row.get("operator") or "").strip() for row in decisions):
        raise RuntimeError("真人房间命令缺 Matrix sender")
    if not str(decisions[1].get("note") or "").strip():
        raise RuntimeError("task-s7b-payment 的 /reject 必须携带处置原因")

    finance = store.get_task(s7.TASK_FINANCE_2)
    payment = store.get_task(s7.TASK_PAYMENT_2)
    plan = store.get_plan(plan_id)
    if not finance or finance["state"] != TaskState.DONE:
        raise RuntimeError("task-s7b-finance 未由真人 /approve 收口到 DONE")
    if not payment or payment["state"] != TaskState.FAILED:
        raise RuntimeError("task-s7b-payment 未由真人 /reject 收口到 FAILED")
    if not plan or plan["state"] != "FAILED":
        raise RuntimeError(f"s7b Plan 应收口到 FAILED，实际 {(plan or {}).get('state')}")

    events = store.list_event_log(plan_id)

    def require_transition(task_id: str | None, from_state: str, to_state: str,
                           reason: str | None = None) -> dict:
        matches = [
            row for row in events
            if row.get("event_type") == "StateTransition"
            and row.get("task_id") == task_id
            and row.get("from_state") == from_state
            and row.get("to_state") == to_state
            and (reason is None or row.get("reason") == reason)
        ]
        if len(matches) != 1:
            raise RuntimeError(
                f"迁移证据应恰有一条 {task_id} {from_state}->{to_state} "
                f"reason={reason}，实际 {len(matches)} 条")
        return matches[0]

    finance_blocked = require_transition(
        s7.TASK_FINANCE_2, "AWAITING_REVIEW", "BLOCKED", "gate_needs_human")
    if finance_blocked.get("detail", {}).get("await") != "human_approval":
        raise RuntimeError("task-s7b-finance 的 BLOCKED 事件缺 human_approval 锚点")
    payment_blocked = require_transition(
        s7.TASK_PAYMENT_2, "AWAITING_REVIEW", "BLOCKED", "gate_needs_human")
    payment_blocked_detail = payment_blocked.get("detail", {})
    evidence = payment_blocked_detail.get("evidence") or []
    if (payment_blocked_detail.get("await") != "human_decision"
            or payment_blocked_detail.get("reason") != "gateway_needs_human"
            or not any(row.get("code") == s7.GATEWAY_TERMINAL_CODE for row in evidence)):
        raise RuntimeError("task-s7b-payment 的第三出口 BLOCKED 证据不完整")

    for decision, to_state, reason in (
        (decisions[0], TaskState.DONE, "human_approve"),
        (decisions[1], TaskState.FAILED, "human_reject"),
    ):
        transition = require_transition(
            decision["task_id"], TaskState.BLOCKED, to_state, reason)
        detail = transition.get("detail", {})
        if (detail.get("operator") != decision["operator"]
                or detail.get("note", "") != decision.get("note", "")):
            raise RuntimeError(f"{decision['task_id']} 的人工迁移与房间 sender/note 不一致")

    plan_failed = [
        row for row in events
        if row.get("event_type") == "PlanTransition"
        and row.get("from_state") == "RUNNING"
        and row.get("to_state") == "FAILED"
    ]
    if len(plan_failed) != 1:
        raise RuntimeError(f"s7b 缺 RUNNING->FAILED PlanTransition，实际 {len(plan_failed)} 条")

    approval_records = objects.query(
        store,
        "SELECT * FROM approval_record WHERE tenant_id=? AND case_id=? "
        "ORDER BY decided_at, rowid",
        (s7.TENANT_ID, s7.CASE_ID_2),
    )
    if len(approval_records) != 2:
        raise RuntimeError(f"s7b 应恰有两条业务审批记录，实际 {len(approval_records)} 条")
    for row, decision, expected_decision in zip(
            approval_records, decisions, ("approved", "rejected"), strict=True):
        if (row.get("decision") != expected_decision
                or row.get("approver") != decision["operator"]
                or row.get("reason", "") != decision.get("note", "")
                or not row.get("decided_at")):
            raise RuntimeError(f"s7b {expected_decision} 业务审批记录与房间命令不一致")

    denied = [
        row for row in events
        if row.get("event_type") == "ApprovalDenied"
        and row.get("task_id") == s7.TASK_FINANCE_2
        and row.get("reason") == "sender 不在 MAOS_APPROVERS 名单内"
        and row.get("detail", {}).get("command") == "approve"
        and row.get("detail", {}).get("task_id") == s7.TASK_FINANCE_2
        and row.get("detail", {}).get("sender")
    ]
    if not denied:
        raise RuntimeError("本轮缺名单外账号针对 task-s7b-finance 的 ApprovalDenied")

    return {
        "plan": plan,
        "events": events,
        "approval_records": approval_records,
        "denied": denied,
    }


def write_refund_audit(path: str, *, store, plan_id: str, mirrored: int,
                       decisions: list[dict]) -> None:  # noqa: ANN001
    """把 s7b 库侧收口证据原子落盘；任何失败都不留下临时文件。"""
    from maos.domain.refund import objects
    from maos.flows import scenario_7 as s7

    events = store.list_event_log(plan_id)
    relevant = [
        row for row in events
        if (row.get("event_type") == "ApprovalDenied"
            or row.get("event_type") == "PlanTransition"
            or row.get("task_id") in (s7.TASK_FINANCE_2, s7.TASK_PAYMENT_2))
    ]
    payload = {
        "scenario": 7,
        "path": "refund-s7b-room-hitl",
        "plan": store.get_plan(plan_id),
        "tasks": [store.get_task(s7.TASK_FINANCE_2), store.get_task(s7.TASK_PAYMENT_2)],
        "accepted_room_decisions": decisions,
        "approval_records": objects.query(
            store,
            "SELECT * FROM approval_record WHERE tenant_id=? AND case_id=? ORDER BY decided_at",
            (s7.TENANT_ID, s7.CASE_ID_2),
        ),
        "event_log_query": {
            "where": ("plan_id=<s7b plan> AND (event_type IN "
                      "('PlanTransition','ApprovalDenied') OR task_id IN "
                      "('task-s7b-finance','task-s7b-payment'))"),
            "rows": relevant,
        },
        "transition_mirror_confirmed_lower_bound": mirrored,
    }
    header = (f"# generated at {datetime.now(timezone.utc).isoformat()} "
              f"from {_evidence_sha()}\n")
    rendered = header + json.dumps(payload, ensure_ascii=False, indent=2) + "\n"

    token = (os.environ.get("MATRIX_TOKEN") or "").strip()
    if token and token in rendered:
        raise RuntimeError("审计证据命中 MATRIX_TOKEN 哨兵，拒绝落盘")

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=target.name + ".tmp.", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        # hard-link 只在 target 不存在时成功；与 exists() 预检之间即使有人抢写，
        # 也不会覆盖旧证据。临时 inode 已完整 fsync，link 后即是完整文件。
        os.link(temporary, target)
        directory_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def run_refund_demo(*, timeout: float, audit_out: str | None) -> int:
    """在真 Matrix 房间里运行场景 7 的 s7b 两个人工决策点。"""
    print(selfcheck_line(), flush=True)
    if not audit_out:
        print("[前置条件不满足] refund-s7b 必须显式给 --audit-out，避免成功运行无库侧证据。",
              file=sys.stderr)
        return EXIT_PRECONDITION
    if Path(audit_out).exists():
        print(f"[前置条件不满足] 审计目标已存在，拒绝覆盖旧证据：{audit_out}", file=sys.stderr)
        return EXIT_PRECONDITION

    target = Path(audit_out)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_key = hashlib.sha256(str(target.resolve()).encode("utf-8")).hexdigest()[:24]
    lock = Path(tempfile.gettempdir()) / f"maos-refund-audit-{lock_key}.lock"
    lock_fd = os.open(lock, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock_fd)
        print(f"[前置条件不满足] 同一审计目标已有运行占用：{audit_out}", file=sys.stderr)
        return EXIT_PRECONDITION
    try:
        return _run_refund_demo_reserved(timeout=timeout, audit_out=audit_out)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _run_refund_demo_reserved(*, timeout: float, audit_out: str) -> int:
    """已独占 audit 目标后的实际运行；调用方负责释放 run lock。"""
    from maos.flows import scenario_7 as s7

    out = s7.drive(matrix=True)
    store, bus = out["store"], out["bus"]
    channel = bus_channel(bus)
    if channel is None:
        print("[没进房间] refund-s7b 不提供降级自检；本轮没有生成任何审计证据。",
              file=sys.stderr)
        _close(bus, StdoutChannel(), True)
        return EXIT_NO_ROOM

    config = getattr(bus, "config", None) or MatrixBusConfig.from_env()
    listen = getattr(channel, "listen", None)
    if listen is None:
        print("[没进房间] 当前通道不能监听真人命令。", file=sys.stderr)
        _close(bus, channel, False)
        return EXIT_NO_ROOM

    driver = RefundRoomDecisionDriver(
        store=store, bus=bus, channel=channel, config=config, timeout=timeout)
    try:
        listen(driver.on_message)
        result = s7.drive_human_exit(
            store=store, bus=bus, cp=out["cp"], gate=out["gate"], hq=out["hq"],
            decision_hook=driver)
        driver.finish()
        plan_id = result["plan_id"]
        validated = validate_refund_room_result(
            store, plan_id, driver.accepted_room_decisions)
        write_refund_audit(
            audit_out, store=store, plan_id=plan_id, mirrored=driver.mirrored,
            decisions=driver.accepted_room_decisions)
        print(f"[库侧证据] {audit_out}")
        print(f"\n退款房间终态: finance=DONE  payment=FAILED  "
              f"plan={validated['plan']['state']}  "
              f"ApprovalDenied={len(validated['denied'])}  镜像确认下界={driver.mirrored}")
        return EXIT_OK
    except RefundRoomDecisionTimeout as exc:
        print(f"[超时] {exc}；本轮没有生成任何新审计证据。", file=sys.stderr)
        return EXIT_TIMEOUT
    except Exception as exc:                            # noqa: BLE001
        print(f"[退款房间运行失败] {describe_exc(exc)}；不写半份审计证据。", file=sys.stderr)
        return EXIT_PRECONDITION
    finally:
        try:
            driver.finish()
        finally:
            _close(bus, channel, False)


def run_demo(case: str, *, timeout: float, auto_approve: bool,
             allow_degraded: bool = False) -> int:
    problem = _check_preconditions(case)
    if problem:
        print(f"[前置条件不满足] {problem}", file=sys.stderr)
        return EXIT_PRECONDITION

    # flush：自检行必须**第一个**落到屏幕上。stdout 在管道里是块缓冲的，而报错走
    # 无缓冲的 stderr —— 不 flush 的话「用哪个解释器跑的」会被挤到报错后面，
    # 而它正是读那条报错时要先知道的东西。
    print(selfcheck_line(), flush=True)

    # 装配照 flows/common.py::build(matrix=True)，其内部就是 _wrap_matrix ——
    # 不自拼第二条构造路径（common.py 抬头：两条一定会漂）。
    store, bus, cp, model, worker, gate = build({"任务输入": GOOD_PATCH}, matrix=True)
    room = bus_channel(bus)
    degraded = room is None
    channel = StdoutChannel() if degraded else room

    if degraded:
        # 「本来就没打算进房间」和「打算进但没进成」在 log_only 这个字段上是同一个
        # True，但退出码必须不同。前者是自检常态；后者跑完 exit=0 就等于让一次
        # **没进房间**的运行看起来像一次成功的取证 —— 而两边的终端输出一模一样。
        reason = getattr(bus, "degrade_reason", DEGRADE_ENV)
        detail = getattr(bus, "degrade_detail", "")
        says = _DEGRADE_SAYS.get(reason)
        if says and not allow_degraded:
            print(f"\n[没进房间] {says}", file=sys.stderr)
            if detail:
                print(f"  原因：{detail}", file=sys.stderr)
            print("  本次**不继续**跑降级流程：降级的终端输出与真房间的形态无法分辨，\n"
                  "  跑完 exit=0 会让这一轮看起来像取到了证。\n"
                  "  确实只想做无房间自检，显式加 --allow-degraded。", file=sys.stderr)
            _close(bus, channel, degraded)
            return EXIT_NO_ROOM
        if says:
            print(f"\n[降级放行] --allow-degraded 已指定；{says}", file=sys.stderr)
        print("\n[降级] 未接通 Matrix 房间，本该发进房间的消息改打 stdout；"
              "行为与真房间一致，只是没进房间。")
        if not auto_approve:
            print(f"[提示] 降级模式下没有人能发命令；要走完全程请加 --auto-approve"
                  f"（或配齐 MATRIX_* 后重跑）。", file=sys.stderr)
    elif auto_approve:
        print("[拒绝] --auto-approve 只用于降级自检；已接通真房间时请在 Element 里"
              "真打 /approve 或 /reject。", file=sys.stderr)
        return EXIT_PRECONDITION

    # 取总线自己那份 config，不重新 from_env()：重读一次会把「配置缺 …」那条降级
    # 告警又打一遍，演示台上看起来像出了两次问题。回退分支是给 hiclaw 不可导入时
    # 那条裸 InMemoryEventBus 留的。
    config = getattr(bus, "config", None) or MatrixBusConfig.from_env()
    if degraded and auto_approve and not config.approvers:
        # 降级自检里没人配 MAOS_APPROVERS，而 bridge 的名单校验是它要证明的东西之一。
        # 只改本进程这一份 config 副本，且把这件事明说出来。
        config = replace(config, approvers=frozenset({DEMO_APPROVER}))
        print(f"[降级自检] MAOS_APPROVERS 未配置，临时以 {DEMO_APPROVER} 作为模拟审批人。")

    plan_id, task = seed_blocked_task(store, cp, bus, gate)
    task_id = task["task_id"]
    print(f"\n待人工审批: {task['title']}（{task_id}，effect_risk="
          f"{task['effect_risk']}，state={task['state']}）")

    # 审批人名单在演示途中被改掉时，把这件事落成一条 event_log 的 ConfigChanged
    # （T28 §5.3）。挂在这次演示的 plan_id 上，`list_event_log(plan_id)` 一把捞得出
    # 「谁在什么时候把名单从 X 改成 Y」，与状态迁移在同一条时间线上。
    #
    # **缺省什么都不会落**：`MAOS_CONFIG_SOURCE` 未设时配置源是 env，名单不会中途变，
    # 也就没有变更可记 —— 这一行不改变任何现有演示的输出。
    detach_audit = attach_config_audit(store, plan_id=plan_id)

    mirror = TransitionMirror(store, plan_id, channel)
    mirror.poll_once()                      # 先把停到 BLOCKED 为止的轨迹补齐
    channel.send(*approval_card(task))
    mirror.start()

    hq = HumanApprovalQueue(store, cp)
    bridge = RoomApprovalBridge(hq, config, channel=channel)
    decided = threading.Event()

    def on_message(sender: str, body: str) -> None:
        """房间消息回调。**绝不抛**：异常逃出去会掀掉 nio 的 sync 循环。"""
        try:
            reply = bridge.handle_message(sender, body)
        except Exception as exc:            # noqa: BLE001
            log.warning("处理房间消息失败（%s），监听继续", describe_exc(exc))
            return
        if reply and not degraded:
            # 降级时 StdoutChannel 已经原样打过一遍了，不重复。
            print(f"[房间回执] {reply}")
        current = store.get_task(task_id)
        if current and current["state"] != TaskState.BLOCKED:
            decided.set()

    listen = getattr(channel, "listen", None)
    if listen is not None:
        listen(on_message)
    else:
        log.warning("通道没有 listen()，无法接收房间命令（只能靠 --auto-approve）")

    if degraded and auto_approve:
        command = f"/approve {task_id}" if case == "approve" else f"/reject {task_id} 演示驳回"
        print(f"\n[模拟审批] {DEMO_APPROVER} 发出：{command}")
        on_message(next(iter(config.approvers), DEMO_APPROVER), command)

    got = wait_for_decision(store, task_id, decided, timeout)
    bus.drain()
    mirror.stop()                            # flush：把 BLOCKED -> 终态那几行补进房间

    # 复查一次。``mirror.stop()`` 自己就要 1s 上下（它等镜像线程收口），审批完全
    # 可能落在 wait 返回之后、下面这句判词之前。不复查就会打出「未等到审批 ……
    # 任务仍停在 DONE」这种**自相矛盾**的行，并把一次实际成功的运行报成
    # EXIT_TIMEOUT —— 实测出现过（2026-08-31 三幕演示第二幕）。
    if not got and _left_blocked(store, task_id):
        print(f"\n[迟到] 审批在 {timeout:.0f}s 预算之外才落地，但**已生效** —— "
              f"判据是库里的状态，不是等待有没有超时。", file=sys.stderr)
        got = True

    if not got:
        print(f"\n未等到审批（{timeout:.0f}s 超时）—— 任务仍停在 "
              f"{store.get_task(task_id)['state']}", file=sys.stderr)
        detach_audit()
        _close(bus, channel, degraded)
        return EXIT_TIMEOUT

    final_task = store.get_task(task_id)
    final_plan = store.get_plan(plan_id)
    print(f"\n终态: task={final_task['state']}  plan={final_plan['state']}  "
          f"（镜像发出 {mirror.mirrored} 条迁移）")
    detach_audit()
    _close(bus, channel, degraded)
    return EXIT_OK


def _close(bus, channel, degraded: bool) -> None:        # noqa: ANN001
    """收口。关不掉也不许把异常带出去 —— 演示已经跑完了。

    ``getattr`` 取 ``close`` 而不是直接调：``_wrap_matrix`` 在 hiclaw 不可导入时
    回退的是裸 ``InMemoryEventBus``，它没有 close()。
    """
    target = channel if degraded else bus
    closer = getattr(target, "close", None)
    if closer is None:
        return
    try:
        closer()
    except Exception as exc:                # noqa: BLE001
        log.debug("收口异常（已忽略）：%s", describe_exc(exc))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hiclaw.room_demo",
        description="房间审批演示：高风险任务停在 BLOCKED，等 Matrix 房间里的人放行")
    parser.add_argument("--case", choices=("approve", "reject", "refund-s7b"), required=True,
                        help="approve/reject = 软件域审批；refund-s7b = 场景 7 退款核心审批链"
                             f"（软件域 reject 需先设 {ENV_SANDBOX_WORKDIR}）")
    parser.add_argument("--timeout", type=float, default=300.0,
                        help="等审批的秒数，超时非 0 退出（缺省 300）")
    parser.add_argument("--auto-approve", action="store_true",
                        help="降级自检专用：内置模拟审批，无房间也走完全程")
    parser.add_argument("--allow-degraded", action="store_true",
                        help=f"承认这一轮不进房间。缺省下「MATRIX_* 配齐却没接通」"
                             f"直接 exit {EXIT_NO_ROOM}，不跑降级流程")
    parser.add_argument("--audit-out",
                        help="refund-s7b 成功后原子写出的库侧审计证据路径")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)-5s %(name)-12s %(message)s")
    logging.getLogger("maos.bus").setLevel(logging.WARNING)
    if args.case == "refund-s7b":
        if args.auto_approve or args.allow_degraded:
            print("[拒绝] refund-s7b 只接受真房间真人命令，不支持自动审批或降级模式。",
                  file=sys.stderr)
            return EXIT_PRECONDITION
        return run_refund_demo(timeout=args.timeout, audit_out=args.audit_out)
    if args.audit_out:
        print("[拒绝] --audit-out 只用于 --case refund-s7b。", file=sys.stderr)
        return EXIT_PRECONDITION
    return run_demo(args.case, timeout=args.timeout, auto_approve=args.auto_approve,
                    allow_degraded=args.allow_degraded)


if __name__ == "__main__":
    sys.exit(main())
