"""场景共用装配层 —— 六件套构造、驱动循环、快照打印、演示常量。

build() 的入参与返回值都是冻结契约（C-3 / C-4）：
返回六元组的**位序与类型**不许调换，解包写法固定为
``store, bus, cp, model, worker, gate = build(...)``。

任何场景都不许绕过 build() 自己拼装 store/bus/cp/model/worker/gate ——
留第二条构造路径，两条一定会漂。
"""

from __future__ import annotations

import json
import logging

from maos.contracts.states import PlanState
from maos.core.control_plane import ControlPlane
from maos.core.eventbus import EventBus, InMemoryEventBus
from maos.core.store import SqliteStore
from maos.model.client import ModelClient, ScriptedModelClient
from maos.runtime.gate import ReviewerGate
from maos.runtime.worker import WorkerRuntime

log = logging.getLogger("maos.flows")


def _wrap_matrix(inner: EventBus) -> EventBus:
    """把 inner bus 包进 MatrixEventBus；任何失败都告警回退，不让演示中断。

    HiClaw 对接层由 Task-E 落地，在那之前这里恒走 ImportError 分支。
    """
    try:
        from hiclaw.matrix_bus import MatrixBusConfig, MatrixEventBus
    except ImportError as exc:
        log.warning("Matrix 总线不可用（%s），回退进程内 EventBus", exc)
        return inner
    try:
        return MatrixEventBus(inner, MatrixBusConfig.from_env())
    except Exception as exc:  # noqa: BLE001 —— 连接/配置失败一律降级
        log.warning("Matrix 总线构造失败（%s），回退进程内 EventBus", exc)
        return inner


def build(script: dict[str, str], *, matrix: bool = False, model: ModelClient | None = None):
    """装配一套完整运行时，返回冻结的六元组（C-4）。

    script：喂给缺省 ScriptedModelClient 的「关键字 -> 应答」表。
    model ：传实例则原样注入（场景 2 的 FlakyModel 由此进入），不再按 script 构造。
    matrix：True 时事件总线经 HiClaw(Matrix) 转发，不可用则自动降级。
    """
    store = SqliteStore()
    store.init_schema()
    bus = InMemoryEventBus()
    if matrix:
        bus = _wrap_matrix(bus)
    cp = ControlPlane(store, bus)
    model = ScriptedModelClient(script) if model is None else model
    worker = WorkerRuntime(worker_id="w1", bus=bus, control_plane=cp, model=model)
    gate = ReviewerGate(store, bus, cp)
    return store, bus, cp, model, worker, gate


def run_until_settled(bus, gate, cp, plan_id: str, max_cycles: int = 20) -> None:
    """驱动循环：drain 队列 -> 跑 Gate -> 再 drain，直到没有新进展。

    换 RocketMQ 后这个循环消失（消费者常驻），但语义完全一样。
    """
    for _ in range(max_cycles):
        bus.drain()
        reviewed = gate.review_pending(plan_id)
        bus.drain()
        plan = cp.store.get_plan(plan_id)
        if plan["state"] in (PlanState.DONE, PlanState.FAILED):
            return
        if reviewed == 0:
            return
    raise RuntimeError("驱动循环未收敛")


def dump(cp, plan_id: str, title: str) -> None:
    snap = cp.snapshot(plan_id)
    print(f"\n{'=' * 68}\n{title}\n{'=' * 68}")
    print(f"Plan: {snap['plan']['state']}  |  {snap['plan']['goal']}")
    for t in snap["tasks"]:
        print(f"  · {t['title'][:34]:36s} {t['state']:16s} attempt={t['attempt']} "
              f"risk={t['risk_level']}")
    print("  状态迁移轨迹:")
    for e in snap["log"]:
        if e["event_type"] == "StateTransition":
            print(f"    {e['task_id']}  {e['from_state']:16s} -> {e['to_state']:16s} "
                  f"[{e['reason']}]")


GOOD_PATCH = json.dumps({
    "files": [{"path": "src/auth.py", "diff": "@@ -12,3 +12,4 @@\n+    verify_token(t)"}],
    "summary": "修复 token 校验缺失",
    "self_check": {"build": "pass", "lint": "pass"},
}, ensure_ascii=False)

BAD_PATCH = json.dumps({
    "files": [{"path": "src/auth.py", "diff": "@@ -12,3 +12,4 @@\n+    pass"}],
    "summary": "",
    "self_check": {"build": "fail", "lint": "pass"},
}, ensure_ascii=False)

PLAN_JSON = json.dumps({"tasks": [{
    "role": "coding", "title": "修复 token 校验缺失",
    "inputs": {"repo": "demo/app", "issue": "#42"},
    "acceptance": ["build 通过", "lint 通过", "有变更说明"],
    "depends_on": [], "risk_level": "L",
}]}, ensure_ascii=False)
