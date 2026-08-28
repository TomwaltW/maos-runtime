"""场景 3：高风险审批 —— Gate 过了也停在 BLOCKED，等人工放行。"""

from __future__ import annotations

from maos.agents.testing import make_test_report, seed_scripted_report
from maos.contracts.events import new_id
from maos.contracts.states import PlanState
from maos.flows.common import GOOD_PATCH, build, dump, run_until_settled
from maos.runtime.gate import HumanApprovalQueue

# Phase 2 起，代码类任务的验收证据是 test_report 而不是 self_check（见 runtime/gate.py）。
# 本场景演的是**审批闸**，测试链路不是它要证明的东西，所以按 Scripted 演示模式
# 预置一份全过报告 —— 与 common.py 的 GOOD_PATCH 同性质。
PASS_REPORT = make_test_report(
    passed=1, failed=0, errors=0, duration=0.11,
    cases=[{"id": "tests/test_config.py::test_prod_config", "status": "passed", "msg": ""}],
    summary="沙箱回归：1 过 0 挂 0 错",
)


def run(*, matrix: bool = False) -> int:
    store, bus, cp, model, worker, gate = build({"任务输入": GOOD_PATCH}, matrix=matrix)
    plan_id = cp.create_plan(goal="高风险变更需人工放行", trace_id=new_id("trace"), tasks=[{
        "role": "coding", "title": "变更生产环境配置",
        "inputs": {"repo": "demo/app"}, "acceptance": ["build 通过"],
        "risk_level": "M",     # Agent 执行（产出补丁）是 M 级，在其授权内
        "effect_risk": "H",    # 但这个补丁合进生产是 H 级，必须人工放行
    }])
    for task in cp.store.list_tasks(plan_id):
        seed_scripted_report(store, plan_id=plan_id, task_id=task["task_id"],
                             attempt=1, report=PASS_REPORT)
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
    return 0
