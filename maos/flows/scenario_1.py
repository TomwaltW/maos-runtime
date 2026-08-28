"""场景 1：正常闭环 —— 四角色 DAG 跑到 PlanState.DONE。

DAG：requirement -> architecture -> coding -> testing，
Reviewer 的语义审查挂在 **Gate 之后、人工审批之前**（本场景 effect_risk=L，无审批环节）。

跑完看 event_log：每一次状态迁移都有一条记录，这就是后面 Trace 的数据来源。

与 Phase 1 的差别不在多了两个角色，在**验收证据换了地基**：coding 任务过闸靠的
不再是它自己写的 self_check，而是一份 test_report（见 runtime/gate.py 的验收闸）。
把下面 PASS_REPORT 里任何一条用例改成 failed，这个场景立刻变成场景 2。
"""

from __future__ import annotations

import json

from maos.agents.manager import ManagerAgent
from maos.agents.reviewer import ReviewerAgent, review_after_gate
from maos.agents.testing import make_test_report, seed_scripted_report
from maos.contracts.events import new_id
from maos.contracts.states import PlanState
from maos.flows.common import GOOD_PATCH, build, dump, run_until_settled
from maos.model.client import select_model_client

GOAL = "修复 demo/app 的 token 校验缺失"

T_REQ, T_ARCH, T_CODE, T_TEST = "s1-req", "s1-arch", "s1-code", "s1-test"

# 沙箱回归报告：Task-B 合并前由场景预置（Scripted 演示模式，见 agents/testing.py），
# 合并后换成 Testing Agent 经 test.verify 真跑的产物 —— Gate 一行不改。
PASS_REPORT = make_test_report(
    passed=2, failed=0, errors=0, duration=0.42,
    cases=[
        {"id": "tests/test_session.py::test_valid_token", "status": "passed", "msg": ""},
        {"id": "tests/test_session.py::test_expired_token", "status": "passed", "msg": ""},
    ],
    summary="沙箱回归：2 过 0 挂 0 错",
)

PLAN_DAG = json.dumps({"tasks": [
    {"task_id": T_REQ, "role": "requirement", "title": "归一化目标并给出验收标准",
     "inputs": {"goal": GOAL, "repo": "demo/app", "issue": "#42"},
     "acceptance": ["产出可机器判定的验收标准"], "depends_on": [], "risk_level": "L"},
    {"task_id": T_ARCH, "role": "architecture", "title": "产出架构契约",
     "inputs": {"endpoint": "POST /session/verify", "effect_risk": "L", "title": "token 校验"},
     "acceptance": ["契约含 api / 幂等 / 审计 / 可逆性四项"],
     "depends_on": [T_REQ], "risk_level": "L"},
    {"task_id": T_CODE, "role": "coding", "title": GOAL,
     "inputs": {"repo": "demo/app", "issue": "#42"},
     "acceptance": ["build 通过", "lint 通过", "有变更说明"],
     "depends_on": [T_ARCH], "risk_level": "L"},
    {"task_id": T_TEST, "role": "testing", "title": "沙箱回归验证",
     "inputs": {"workdir": "/tmp/maos-sandbox", "verify_target": T_CODE,
                "scripted_report": PASS_REPORT},
     "acceptance": ["全部用例通过"], "depends_on": [T_CODE], "risk_level": "M"},
]}, ensure_ascii=False)

NORMALIZED = json.dumps({
    "normalized_goal": GOAL,
    "constraints": ["不改测试", "补丁只落在 demo/app"],
    "acceptance_suggestions": ["会话过期判定使用 UTC", "token 校验分支有用例覆盖"],
}, ensure_ascii=False)

REVIEW_NOTE = json.dumps({
    "defects": [],
    "conclusion": "契约与补丁名实相符，回归全过，可放行",
}, ensure_ascii=False)


def _script() -> dict[str, str]:
    """关键字 -> 应答。顺序即优先级（ScriptedModelClient 取第一个命中的键）。

    语义审查的提示词里嵌了全部产物 JSON，最容易误命中别的关键字，所以排最前。
    """
    return {
        "语义审查产物清单": REVIEW_NOTE,
        "用户请求": PLAN_DAG,
        "原始目标": NORMALIZED,
        "任务输入": GOOD_PATCH,
    }


def run(*, matrix: bool = False) -> int:
    # 真模型接线点（ORCHESTRATION.md:88 验收「有 key 场景 1 真模型通」唯一指定处）。
    # 走 C-3 明文允许的 model= 注入口，build() 一行不改：无 key 时
    # select_model_client 降级返回 ScriptedModelClient(script)，行为与接线前等价。
    script = _script()
    store, bus, cp, model, worker, gate = build(
        script, matrix=matrix, model=select_model_client(script))

    mgr = ManagerAgent(model)
    plan_id = cp.create_plan(goal=GOAL, trace_id=new_id("trace"), tasks=mgr.plan(GOAL))

    # coding 任务的验收证据。DAG 里 testing 依赖 coding，coding 过闸时 testing 还没跑，
    # 所以演示期由场景预置（真沙箱就位后改由 Testing Agent 产出，见 agents/testing.py）。
    for task in cp.store.list_tasks(plan_id):
        if task["role"] == "coding":
            seed_scripted_report(store, plan_id=plan_id, task_id=task["task_id"],
                                 attempt=1, report=PASS_REPORT)

    cp.start_plan(plan_id)
    run_until_settled(bus, gate, cp, plan_id)

    # Gate 之后、审批之前：模型语义审查。它不是第五道闸，只产意见书。
    tasks = cp.store.list_tasks(plan_id)
    note = review_after_gate(ReviewerAgent(model, store=store), cp, plan_id,
                             host_task=tasks[-1])

    dump(cp, plan_id, "场景 1：正常闭环（需求 -> 架构 -> 编码 -> 测试）")
    print(f"  Reviewer 语义审查: status={note.status} "
          f"结论={note.artifacts[0]['content']['conclusion'] if note.artifacts else note.open_questions}")

    assert cp.store.get_plan(plan_id)["state"] == PlanState.DONE
    return 0
