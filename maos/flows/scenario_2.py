"""场景 2：返工闭环 —— 第一轮不完整契约导致用例真挂，findings 喂回后第二轮修好。

这个场景是「所有 Agent 都回复完成 ≠ 业务成功」的正面演示，所以第一轮的补丁
**刻意把 self_check 写成全 pass**：Coding Agent 自称完工、自检全绿、变更说明齐全，
四道旧闸一条都拦不住它。拦下它的是新的验收闸 —— 同 attempt 的 test_report 里
有一条挂掉的用例，逐条转成结构化 finding 喂回，第二轮据此修好。

叙事上的因果链：架构契约漏了「会话过期按 UTC 判定」这一条（第一轮注入的不完整
契约）-> 补丁照契约写，用了本地时区 -> test_expired_token 真挂 -> findings 回灌 ->
第二轮补齐。并行期沙箱未就位，报告由场景预置（Scripted 演示模式，见
agents/testing.py）；Task-B 合并后换成真跑的产物，本文件与 Gate 都不用改。
"""

from __future__ import annotations

import json

from maos.agents.manager import ManagerAgent
from maos.agents.reviewer import ReviewerAgent, review_after_gate
from maos.agents.testing import make_test_report, seed_scripted_report
from maos.contracts.events import new_id
from maos.contracts.states import PlanState, TaskState
from maos.flows.common import GOOD_PATCH, build, dump, run_until_settled
from maos.model.client import ModelResponse, ScriptedModelClient

GOAL = "修复 demo/app 的会话过期判定"

T_REQ, T_ARCH, T_CODE, T_TEST = "s2-req", "s2-arch", "s2-code", "s2-test"

# 第一轮：一条用例真的挂了。severity 会被验收闸判成 major，逐条带 id / msg 喂回。
FAIL_REPORT = make_test_report(
    passed=1, failed=1, errors=0, duration=0.38,
    cases=[
        {"id": "tests/test_session.py::test_valid_token", "status": "passed", "msg": ""},
        {"id": "tests/test_session.py::test_expired_token", "status": "failed",
         "msg": "AssertionError: 会话在本地时区下提前过期；契约未声明按 UTC 判定"},
    ],
    summary="沙箱回归：1 过 1 挂 0 错",
)

PASS_REPORT = make_test_report(
    passed=2, failed=0, errors=0, duration=0.40,
    cases=[
        {"id": "tests/test_session.py::test_valid_token", "status": "passed", "msg": ""},
        {"id": "tests/test_session.py::test_expired_token", "status": "passed", "msg": ""},
    ],
    summary="沙箱回归：2 过 0 挂 0 错",
)

# 第一轮补丁：**自检全 pass、变更说明齐全**，旧口径下这份补丁是「合格」的。
INCOMPLETE_PATCH = json.dumps({
    "files": [{"path": "src/session.py",
               "diff": "@@ -20,3 +20,4 @@\n+    return now() < expires_at"}],
    "summary": "按契约补上过期判定",
    "self_check": {"build": "pass", "lint": "pass"},
}, ensure_ascii=False)

# 第一轮注入的不完整架构契约：四个必填键齐全（过得了契约校验），
# 但 api 里没有「按 UTC 判定」这一条 —— 补丁照它写就会踩本地时区。
INCOMPLETE_CONTRACT = {
    "api": {"endpoint": "POST /session/verify", "rules": ["会话超时返回 401"]},
    "idempotency": {"key": "task_id+attempt"},
    "audit": {"event_log": True},
    "reversibility": {"reversible_kinds": ["patch_set"], "irreversible_kinds": [],
                      "note": "git 补丁类可逆"},
    "summary": "架构契约（第一轮，缺时区判定口径）",
    "self_check": {"build": "pass", "lint": "pass"},
}

PLAN_DAG = json.dumps({"tasks": [
    {"task_id": T_REQ, "role": "requirement", "title": "归一化目标并给出验收标准",
     "inputs": {"goal": GOAL, "repo": "demo/app"},
     "acceptance": ["产出可机器判定的验收标准"], "depends_on": [], "risk_level": "L"},
    {"task_id": T_ARCH, "role": "architecture", "title": "产出架构契约（第一轮不完整）",
     "inputs": {"effect_risk": "L", "architecture": INCOMPLETE_CONTRACT},
     "acceptance": ["契约含 api / 幂等 / 审计 / 可逆性四项"],
     "depends_on": [T_REQ], "risk_level": "L"},
    {"task_id": T_CODE, "role": "coding", "title": GOAL,
     "inputs": {"repo": "demo/app", "issue": "#57"},
     "acceptance": ["会话过期判定使用 UTC", "有变更说明"],
     "depends_on": [T_ARCH], "risk_level": "L"},
    {"task_id": T_TEST, "role": "testing", "title": "沙箱回归验证",
     "inputs": {"workdir": "/tmp/maos-sandbox", "verify_target": T_CODE,
                "scripted_report": PASS_REPORT},
     "acceptance": ["全部用例通过"], "depends_on": [T_CODE], "risk_level": "M"},
]}, ensure_ascii=False)

NORMALIZED = json.dumps({
    "normalized_goal": GOAL,
    "constraints": ["不改测试"],
    "acceptance_suggestions": ["会话过期判定使用 UTC"],
}, ensure_ascii=False)

REVIEW_NOTE = json.dumps({
    "defects": [],
    "conclusion": "第二轮补丁已按 findings 修正时区判定，回归全过",
}, ensure_ascii=False)


class FlakyModel(ScriptedModelClient):
    """第一轮产不完整补丁、返工后产好补丁 —— 按 **prompt 内容** 分派，不按调用序数。

    判据是 code_repo_patch._build_prompt 只在 ``attempt > 1 且有 findings`` 时才写入的
    「返工」二字。按序数计数对新增的 model 调用不设防：本场景的 DAG 里，
    requirement 走 req.normalize、reviewer 走语义审查，都会调模型，序数早就错位了。

    脚本里命中任一关键字就交回 ScriptedModelClient 原样应答；只有补丁请求
    （脚本里刻意不放它的关键字）才走下面这条分派。
    """

    def complete(self, *, system, user, tier):
        if any(kw in user for kw in self.script):
            return super().complete(system=system, user=user, tier=tier)
        return ModelResponse(text=GOOD_PATCH if "返工" in user else INCOMPLETE_PATCH)


def run(*, matrix: bool = False) -> int:
    # 注入式构造：走 build() 这一条路，不再手工拼装六件套（C-3）
    script = {
        "语义审查产物清单": REVIEW_NOTE,
        "用户请求": PLAN_DAG,
        "原始目标": NORMALIZED,
    }
    store, bus, cp, model, worker, gate = build(script, matrix=matrix, model=FlakyModel(script))

    mgr = ManagerAgent(model)
    plan_id = cp.create_plan(goal=GOAL, trace_id=new_id("trace"), tasks=mgr.plan(GOAL))

    # 两轮的证据各预置一份：第一轮挂、第二轮全过。
    for task in cp.store.list_tasks(plan_id):
        if task["role"] != "coding":
            continue
        seed_scripted_report(store, plan_id=plan_id, task_id=task["task_id"],
                             attempt=1, report=FAIL_REPORT)
        seed_scripted_report(store, plan_id=plan_id, task_id=task["task_id"],
                             attempt=2, report=PASS_REPORT)

    cp.start_plan(plan_id)
    run_until_settled(bus, gate, cp, plan_id)

    tasks = cp.store.list_tasks(plan_id)
    note = review_after_gate(ReviewerAgent(model, store=store), cp, plan_id,
                             host_task=tasks[-1])

    dump(cp, plan_id, "场景 2：返工闭环（第 1 轮用例真挂，findings 喂回后第 2 轮修好）")
    code_task = next(t for t in tasks if t["role"] == "coding")
    print(f"  第 1 轮喂回的 findings: "
          f"{json.dumps(code_task['findings'], ensure_ascii=False)[:200]}")
    print(f"  Reviewer 语义审查: status={note.status}")

    assert code_task["state"] == TaskState.DONE and code_task["attempt"] == 2, (
        f"coding 任务应在第 2 轮修好，实际 state={code_task['state']} "
        f"attempt={code_task['attempt']}"
    )
    assert cp.store.get_plan(plan_id)["state"] == PlanState.DONE
    return 0
