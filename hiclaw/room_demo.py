"""房间审批独立入口 —— 一次完整的「高风险任务停在 BLOCKED，等房间里的人放行」。

    python3 -m hiclaw.room_demo --case approve [--timeout 300]
    python3 -m hiclaw.room_demo --case reject  [--timeout 300]
    python3 -m hiclaw.room_demo --case approve --auto-approve   # 无房间自检，降级走完全程

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
import logging
import os
import sys
import threading
from dataclasses import replace
from html import escape as _esc

from maos.agents.testing import make_test_report, seed_scripted_report
from maos.config import attach_config_audit
from maos.contracts.events import Envelope, new_id
from maos.contracts.states import TaskState
from maos.core.control_plane import ENV_SANDBOX_WORKDIR
from maos.flows.common import GOOD_PATCH, build, run_until_settled
from maos.runtime.gate import HumanApprovalQueue

from hiclaw.matrix_bus import MatrixBusConfig, render_mirror, RoomApprovalBridge
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
def approval_card(task: dict) -> tuple[str, str]:
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
    plain += (f"\n可用指令：\n"
              f"  /approve {task_id}\n"
              f"  /reject {task_id} [原因]")
    html += ("<p>可用指令：</p><ul>"
             f"<li><code>/approve {_esc(task_id)}</code></li>"
             f"<li><code>/reject {_esc(task_id)} [原因]</code></li></ul>")
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


def run_demo(case: str, *, timeout: float, auto_approve: bool) -> int:
    problem = _check_preconditions(case)
    if problem:
        print(f"[前置条件不满足] {problem}", file=sys.stderr)
        return EXIT_PRECONDITION

    # 装配照 flows/common.py::build(matrix=True)，其内部就是 _wrap_matrix ——
    # 不自拼第二条构造路径（common.py 抬头：两条一定会漂）。
    store, bus, cp, model, worker, gate = build({"任务输入": GOOD_PATCH}, matrix=True)
    room = bus_channel(bus)
    degraded = room is None
    channel = StdoutChannel() if degraded else room

    if degraded:
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
            log.warning("处理房间消息失败（%s），监听继续", exc)
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

    got = decided.wait(timeout)
    bus.drain()
    mirror.stop()                            # flush：把 BLOCKED -> 终态那几行补进房间

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
        log.debug("收口异常（已忽略）：%s", exc)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hiclaw.room_demo",
        description="房间审批演示：高风险任务停在 BLOCKED，等 Matrix 房间里的人放行")
    parser.add_argument("--case", choices=("approve", "reject"), required=True,
                        help="approve = 演示放行；reject = 演示驳回（需先设 "
                             f"{ENV_SANDBOX_WORKDIR}）")
    parser.add_argument("--timeout", type=float, default=300.0,
                        help="等审批的秒数，超时非 0 退出（缺省 300）")
    parser.add_argument("--auto-approve", action="store_true",
                        help="降级自检专用：内置模拟审批，无房间也走完全程")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)-5s %(name)-12s %(message)s")
    logging.getLogger("maos.bus").setLevel(logging.WARNING)
    return run_demo(args.case, timeout=args.timeout, auto_approve=args.auto_approve)


if __name__ == "__main__":
    sys.exit(main())
