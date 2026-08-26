"""端到端跑通入口 —— 第一步要验证的就是这三件事跑得对。

场景 1：正常闭环      PENDING -> ... -> DONE
场景 2：返工闭环      Gate 判 rework -> 带 findings 重跑 -> DONE
场景 3：高风险审批    Gate 过了也停在 BLOCKED，等人工放行

跑完看 event_log：每一次状态迁移都有一条记录，这就是后面 Trace 的数据来源。
"""

from __future__ import annotations

import json
import logging
import sys

import maos.agents.coding  # noqa: F401 —— import 即注册进 AGENT_POOL
from maos.agents.manager import ManagerAgent
from maos.contracts.events import new_id
from maos.contracts.states import PlanState, TaskState
from maos.core.control_plane import ControlPlane
from maos.core.eventbus import InMemoryEventBus
from maos.core.store import SqliteStore
from maos.model.client import ScriptedModelClient
from maos.runtime.gate import HumanApprovalQueue, ReviewerGate
from maos.runtime.worker import WorkerRuntime

logging.basicConfig(level=logging.INFO, format="%(levelname)-5s %(name)-12s %(message)s")
log = logging.getLogger("maos.main")


def build(script: dict[str, str]):
    store = SqliteStore()
    store.init_schema()
    bus = InMemoryEventBus()
    cp = ControlPlane(store, bus)
    model = ScriptedModelClient(script)
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


# --------------------------------------------------------------------------
def scenario_happy() -> None:
    store, bus, cp, model, worker, gate = build({"用户请求": PLAN_JSON, "任务输入": GOOD_PATCH})
    mgr = ManagerAgent(model)
    trace = new_id("trace")
    plan_id = cp.create_plan(goal="修复 demo/app 的 token 校验缺失",
                            trace_id=trace, tasks=mgr.plan("修复 token 校验缺失"))
    cp.start_plan(plan_id)
    run_until_settled(bus, gate, cp, plan_id)
    dump(cp, plan_id, "场景 1：正常闭环")
    assert cp.store.get_plan(plan_id)["state"] == PlanState.DONE


def scenario_rework() -> None:
    """第一次自检失败 + 无变更说明 -> Gate 判 rework -> 第二次带 findings 修好。"""
    calls = {"n": 0}

    class FlakyModel(ScriptedModelClient):
        def complete(self, *, system, user, tier):
            if "用户请求" in user:
                return super().complete(system=system, user=user, tier=tier)
            calls["n"] += 1
            from maos.model.client import ModelResponse
            return ModelResponse(text=BAD_PATCH if calls["n"] == 1 else GOOD_PATCH)

    store = SqliteStore(); store.init_schema()
    bus = InMemoryEventBus(); cp = ControlPlane(store, bus)
    model = FlakyModel({"用户请求": PLAN_JSON})
    WorkerRuntime(worker_id="w1", bus=bus, control_plane=cp, model=model)
    gate = ReviewerGate(store, bus, cp)

    mgr = ManagerAgent(model)
    plan_id = cp.create_plan(goal="返工路径验证", trace_id=new_id("trace"),
                            tasks=mgr.plan("修复 token 校验缺失"))
    cp.start_plan(plan_id)
    run_until_settled(bus, gate, cp, plan_id)
    dump(cp, plan_id, "场景 2：返工闭环（第 1 次自检失败，第 2 次修好）")
    task = cp.store.list_tasks(plan_id)[0]
    assert task["state"] == TaskState.DONE and task["attempt"] == 2


def scenario_human_approval() -> None:
    store, bus, cp, model, worker, gate = build({"任务输入": GOOD_PATCH})
    plan_id = cp.create_plan(goal="高风险变更需人工放行", trace_id=new_id("trace"), tasks=[{
        "role": "coding", "title": "变更生产环境配置",
        "inputs": {"repo": "demo/app"}, "acceptance": ["build 通过"],
        "risk_level": "M",     # Agent 执行（产出补丁）是 M 级，在其授权内
        "effect_risk": "H",    # 但这个补丁合进生产是 H 级，必须人工放行
    }])
    cp.start_plan(plan_id)
    run_until_settled(bus, gate, cp, plan_id)
    hq = HumanApprovalQueue(store, cp)
    pending = hq.pending(plan_id)
    print(f"\n待人工审批: {[t['title'] for t in pending]}")
    assert len(pending) == 1, "高风险任务应停在 BLOCKED"
    hq.decide(pending[0]["task_id"], approved=True, operator="沈思锴", note="已核对")
    bus.drain()
    dump(cp, plan_id, "场景 3：高风险人工审批")
    assert cp.store.get_plan(plan_id)["state"] == PlanState.DONE


def scenario_idempotency() -> None:
    """重复投递同一个 TaskResult，状态不能被改第二次 —— 这是换 MQ 的前提。"""
    store, bus, cp, model, worker, gate = build({"任务输入": GOOD_PATCH})
    plan_id = cp.create_plan(goal="幂等验证", trace_id=new_id("trace"), tasks=[{
        "role": "coding", "title": "幂等测试任务", "inputs": {}, "acceptance": [],
    }])
    cp.start_plan(plan_id)
    bus.drain()
    task = cp.store.list_tasks(plan_id)[0]
    before = len(cp.store.list_event_log(plan_id))

    from maos.contracts import events as E
    from maos.contracts.events import Topic
    dup = E.task_result(plan_id=plan_id, task_id=task["task_id"], attempt=task["attempt"],
                        trace_id=task["trace_id"], status="ok",
                        artifacts=[{"kind": "patch_set", "content": json.loads(GOOD_PATCH)}])
    bus.publish(Topic.TASK_RESULT, dup)
    bus.publish(Topic.TASK_RESULT, dup)   # 故意重投两次
    bus.drain()

    after = len(cp.store.list_event_log(plan_id))
    print(f"\n{'=' * 68}\n场景 4：幂等验证\n{'=' * 68}")
    print(f"重复投递 2 次 TaskResult，新增日志条数 = {after - before}（期望 0）")
    assert after == before, "重复投递导致了额外的状态迁移，幂等失效"


def main() -> int:
    logging.getLogger("maos.bus").setLevel(logging.WARNING)
    for fn in (scenario_happy, scenario_rework, scenario_human_approval, scenario_idempotency):
        fn()
    print("\n全部场景通过：事件契约与状态机在真实链路上成立，可以进入并行分轨。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
