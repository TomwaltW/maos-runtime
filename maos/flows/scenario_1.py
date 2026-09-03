"""场景 1：正常闭环 —— 四角色 DAG 跑到 PlanState.DONE。

DAG：requirement -> architecture -> coding -> testing，
Reviewer 的语义审查挂在 **Gate 之后、人工审批之前**（本场景 effect_risk=L，无审批环节）。

跑完看 event_log：每一次状态迁移都有一条记录，这就是后面 Trace 的数据来源。

与 Phase 1 的差别不在多了两个角色，在**验收证据换了地基**：coding 任务过闸靠的
不再是它自己写的 self_check，而是一份 test_report（见 runtime/gate.py 的验收闸）。

而这份报告是**真跑出来的**，不是预置的：本场景按 run 现造一份靶场工作目录
（``scenarios/fixture-repo/`` 的副本），把 Coding 产出的补丁真 ``git apply`` 进去，
真跑一遍 ``pytest``，把结果落成 test_report。靶场里埋着一个真 bug
（``auth/session.py::is_session_valid`` 用本地时区判过期），补丁真修掉它，
用例才真变绿 —— 「外部权威判据 = 真实 pytest 结果」这句话在这里兑现。

想把这个场景变成场景 2，不要去改报告：把 ``_script()`` 里的补丁换成
``BAD_PATCH``（打得上、修不好），用例会真挂，Gate 会真返工。
"""

from __future__ import annotations

import json

from maos.agents.manager import ManagerAgent
from maos.agents.reviewer import ReviewerAgent, review_after_gate
from maos.artifacts import KIND_TEST_REPORT
from maos.contracts.events import new_id
from maos.contracts.states import PlanState
from maos.flows.common import (
    GOOD_PATCH,
    build,
    dump,
    patch_verifier,
    run_until_settled,
    sandbox_workdir,
)
from maos.model.client import select_model_client

GOAL = "修复 demo/app 的 token 校验缺失"

T_REQ, T_ARCH, T_CODE, T_TEST = "s1-req", "s1-arch", "s1-code", "s1-test"

# testing 节点的 workdir 不写在这里：它按 run 现造，规划产出后由 _with_workdir()
# 注入。写死一个路径正是上一轮的缺口 —— 全仓没有任何一处准备它。
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
     "inputs": {"verify_target": T_CODE},
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


def _with_workdir(tasks: list[dict], workdir: str) -> list[dict]:
    """把本次 run 现造的沙箱工作目录注入 testing 节点。

    注入而不是写进 PLAN_DAG：有 key 时这份 DAG 由真模型产出（见 run() 的接线点），
    模型不可能知道这个临时路径。接线落在规划之后，两种模型走同一条路。
    """
    for task in tasks:
        if task.get("role") == "testing":
            task.setdefault("inputs", {})["workdir"] = workdir
    return tasks


def _print_regression(store, task_id: str) -> None:
    """把真报告里的 cases 逐条打出来 —— 演示时这就是「外部权威判据」本身。"""
    reports = [a["content"] for a in store.list_artifacts(task_id)
               if a["kind"] == KIND_TEST_REPORT]
    if not reports:
        return
    report = reports[-1]
    print(f"  沙箱回归（真跑）: {report['summary']}  duration={report['duration']}s")
    for case in report["cases"]:
        print(f"    · {case['id']:<48s} {case['status']}")


def run(*, matrix: bool = False) -> int:
    # 真模型接线点（ORCHESTRATION.md:88 验收「有 key 场景 1 真模型通」唯一指定处）。
    # 走 C-3 明文允许的 model= 注入口，build() 一行不改：无 key 时
    # select_model_client 降级返回 ScriptedModelClient(script)，行为与接线前等价。
    script = _script()
    with sandbox_workdir() as workdir:
        store, bus, cp, model, worker, gate = build(
            script, matrix=matrix, model=select_model_client(script))

        # 先把两个 id 拿到手，再去规划：`mgr.plan()` 跑在 `create_plan` **之前**
        # （它是 create_plan 的入参），不带着 id 走，这一次规划烧掉的 token 就只能
        # 落空 trace_id，成为 `unattributed_usage` 里认领不了的一行。带 store 构造
        # 才落得下这笔账 —— 不带的话 SkillInvoker.store is None，整条记账直接跳过。
        trace_id, plan_id = new_id("trace"), new_id("plan")
        mgr = ManagerAgent(model, store=store)
        cp.create_plan(
            goal=GOAL, trace_id=trace_id, plan_id=plan_id,
            tasks=_with_workdir(
                mgr.plan(GOAL, context={"plan_id": plan_id, "trace_id": trace_id}),
                workdir))

        cp.start_plan(plan_id)
        # coding 任务的验收证据在这一刻真跑出来：DAG 里 testing 依赖 coding，
        # coding 过闸时 testing 还没派发，报告不可能已经存在（见 common.py 的
        # 模块注释）。所以由驱动循环在「产物已入库、Gate 还没判」那一刻现跑。
        run_until_settled(bus, gate, cp, plan_id,
                          before_review=patch_verifier(store, workdir))

        # Gate 之后、审批之前：模型语义审查。它不是第五道闸，只产意见书。
        tasks = cp.store.list_tasks(plan_id)
        note = review_after_gate(ReviewerAgent(model, store=store), cp, plan_id,
                                 host_task=tasks[-1])

        dump(cp, plan_id, "场景 1：正常闭环（需求 -> 架构 -> 编码 -> 测试）")
        code_task = next(t for t in tasks if t["role"] == "coding")
        _print_regression(store, code_task["task_id"])
        print(f"  Reviewer 语义审查: status={note.status} "
              f"结论={note.artifacts[0]['content']['conclusion'] if note.artifacts else note.open_questions}")

        assert cp.store.get_plan(plan_id)["state"] == PlanState.DONE
    return 0
