"""场景 5：治理路径闭环 —— 多源聚合 → 撞双 blocker → 确定性重规划 → DONE → 知识沉淀。

**全程强制 ScriptedModelClient**：``select_model_client(script, force_scripted=True)``
（A-12），``--scenario 5`` **忽略** ``MAOS_LLM_API_KEY``，配了 key 的机器上也一行网络都不走。

这不是图省事，是本场景要证明的那件事本身（phase-4.md 原则）：
**replan、补偿、审批是控制面行为，其正确性不得依赖模型的智力表现。**
换句话说，这条路径在任何机器、任何时刻，状态迁移序列必须逐条一致 ——
只要它依赖模型的发挥，这个断言就立不住。所以模型在这里被降级成一张查表：
第一版规划固定产出撞双 blocker 的方案甲，重规划后固定产出通过的方案乙。

⚠️ ``maos/main.py`` 的 ``DEFAULT_SCENARIOS = (1,2,3,4)`` 不含 5，而 main.py 已冻结
（附录 D）。所以 ``python3 run.py`` 无参**仍然不跑本场景**，只有 ``--scenario 5`` 跑 ——
这是预期行为，不要为此去改 main.py。
"""

from __future__ import annotations

import json

from maos.agents.base import AgentIdentity
from maos.agents.manager import ManagerAgent
from maos.agents.testing import make_test_report, seed_scripted_report
from maos.contracts.events import new_id
from maos.contracts.states import PlanState
from maos.flows.common import build, dump, run_until_settled
from maos.model.client import Tier, select_model_client
from maos.runtime.plan_finalizer import PlanFinalizer
from maos.skills.builtin.issue_aggregate import load_signal_findings
from maos.skills.invoker import SkillInvoker

# 任务 id 写死，不用 new_id：本场景的验收是「连跑两次输出逐条一致」，
# 而 dump() 会打印 task_id（flows/common.py:90）。随机 id 会让两次输出必然不同，
# 于是那条验收永远过不了 —— 确定性得管到打印出来的每一个字符。
TASK_ID = "task-s5-payment-001"

# ---- 编排层自己的 identity：它要调 issue.aggregate，而 manager 白名单里没有这一项 ----
# 与其去改 agents/manager.py 的白名单（那是 Task-C 的文件，且会放大 Manager 的权限），
# 不如让编排层带一个只有聚合权限的 identity —— 白名单机制正是用来表达这种最小授权的。
INTAKE_IDENTITY = AgentIdentity(
    agent_id="signal-intake",
    role="intake",
    duty="把多源原始信号聚合去重成 issue 清单，产出本次 Plan 的目标",
    allowed_skills=frozenset({"issue.aggregate"}),
    allowed_tools=frozenset(),
    write_scope=frozenset(),
    max_risk="L",
    model_tier=Tier.LIGHT,
)

# ---- 两版规划：形状同 ManagerAgent.plan() 的出参 ----
# inputs 里必须带 title：CodingAgent 取的是 ``ctx.inputs.get("title")``（coding.py:53），
# 不是 task 表的 title —— TaskAssignment payload 里没有 title 字段。
_TASK_V1 = {
    "task_id": TASK_ID, "role": "coding", "title": "支付回调接入（方案甲）",
    "inputs": {"title": "支付回调接入（方案甲）", "repo": "demo/pay"},
    "acceptance": ["回调签名校验通过", "凭证不落代码"],
    "depends_on": [], "risk_level": "L",
}
_TASK_V2 = {
    "task_id": TASK_ID, "role": "coding", "title": "支付回调接入（方案乙）",
    "inputs": {"title": "支付回调接入（方案乙）", "repo": "demo/pay", "secret_source": "env"},
    "acceptance": ["回调签名校验通过", "凭证不落代码"],
    "depends_on": [], "risk_level": "L",
}
PLAN_V1 = json.dumps({"tasks": [_TASK_V1]}, ensure_ascii=False)
PLAN_V2 = json.dumps({"tasks": [_TASK_V2]}, ensure_ascii=False)

# 方案甲：两个文件各踩一条 ReviewerGate._gate_security 的凭证特征（gate.py:108），
# 于是**恰好**两条 blocker —— 正好压在 REPLAN_BLOCKER_THRESHOLD 上，确定性触发重规划。
# 其余三道闸刻意全部让过：要演示的是「方案本身不可行」，不是「补丁写得糙」。
BLOCKER_PATCH = json.dumps({
    "files": [
        {"path": "src/payment/callback.py",
         "diff": "@@ -1,3 +1,5 @@\n+    conn = connect(password=\"demo-plain-text\")"},
        {"path": "src/payment/settings.py",
         "diff": "@@ -8,2 +8,4 @@\n+    ACCESS_ID = \"AKIAEXAMPLEDEMOONLY\""},
    ],
    "summary": "接入支付回调（方案甲：凭证直接写在代码里）",
    "self_check": {"build": "pass", "lint": "pass"},
}, ensure_ascii=False)

GOOD_PATCH = json.dumps({
    "files": [
        {"path": "src/payment/callback.py",
         "diff": "@@ -1,3 +1,7 @@\n+    secret = os.environ[\"PAY_CALLBACK_SECRET\"]\n"
                 "+    verify_signature(req, secret)"},
    ],
    "summary": "接入支付回调（方案乙：密钥读环境变量并校验回调签名）",
    "self_check": {"build": "pass", "lint": "pass"},
}, ensure_ascii=False)

# 查表顺序即分派规则 —— ScriptedModelClient 返回**第一个**命中的关键字
# （client.py:67-70），所以四个关键字必须互不为子串，且专用的排在通用的前面。
# 「重新规划」刻意不写成「重规划版」之类含在任务标题里的词：那样 Coding 的
# prompt 会命中它、拿回一份 Plan JSON 当补丁解析，症状离原因极远。
SCRIPT = {
    "重新规划": PLAN_V2,        # 重规划回调的 prompt
    "用户请求": PLAN_V1,        # ManagerAgent.plan() 的首次规划
    "方案乙": GOOD_PATCH,       # 重规划后的 Coding 产出
    "方案甲": BLOCKER_PATCH,    # 首版的 Coding 产出
}

# 两版方案共用的回归报告。本场景的 DAG 只有一个 coding 节点、没有 testing 节点，
# 报告不可能由谁跑出来，所以照 scenario_1/3 的做法由场景预置。
#
# 不预置会打破本场景的核心不变量：Task-C 起代码类任务缺 test_report 即 acceptance
# blocker，于是方案甲变成 **3** 条 blocker（安全 2 + 验收 1）而不是设计好的 2 条，
# 上面「恰好压在 REPLAN_BLOCKER_THRESHOLD 上」那句话失真；方案乙则被这条唯一的
# blocker 一路挡到 FAILED，治理路径根本收敛不到 DONE。
PASS_REPORT = make_test_report(
    passed=2, failed=0, duration=0.31,
    cases=[
        {"id": "tests/test_callback.py::test_signature_verified", "status": "passed", "msg": ""},
        {"id": "tests/test_callback.py::test_secret_from_env", "status": "passed", "msg": ""},
    ],
    summary="支付回调回归：2 过 0 挂",
)


def _seed_report(store, plan_id: str, attempt: int) -> None:
    """给 TASK_ID 的第 ``attempt`` 次 attempt 预置回归报告。

    Gate 按 ``version == task["attempt"]`` 取本轮产物，所以每一版方案都要单独预置：
    一次性塞一份是喂不到第二轮的。
    """
    seed_scripted_report(store, plan_id=plan_id, task_id=TASK_ID,
                         attempt=attempt, report=PASS_REPORT)


def _intake_goal(store, *, plan_id: str = "", trace_id: str = "") -> tuple[str, dict]:
    """多源信号 -> issue.aggregate -> 本次 Plan 的目标（零模型，可复现）。

    phase-4.md 第 1 步把这条接线写在场景 1 上，但 ``flows/scenario_1.py`` 归 Task-C
    且已冻结口径，本轨不碰别人的文件，改接在这里（已记 docs/DECISIONS.md）。

    ``plan_id`` / ``trace_id`` 由调用方**在建 Plan 之前**先生成好传进来。这一步
    跑在 ``create_plan`` 之前，不带着 id 走，它落的 SkillInvoked 就是一条谁也认领
    不了的游离事件（plan_id 空串）。归属不是硬凑的：这一步归一出来的正是这个
    Plan 的目标 —— 它本来就属于那棵树。
    """
    invoker = SkillInvoker(INTAKE_IDENTITY, store)
    findings = load_signal_findings()
    res = invoker.invoke("issue.aggregate", {"findings": findings},
                         extras={"plan_id": plan_id, "trace_id": trace_id})
    if res.status != "ok" or not res.output["issues"]:
        # 信号目录缺失时不让场景挂掉：治理路径要演示的是 replan，不是读文件
        return "修复支付回调的安全缺陷", {"issues": [], "summary": "无可用信号，使用兜底目标"}
    top = res.output["issues"][0]
    return f"修复：{top['title']}", res.output


def run(*, matrix: bool = False) -> int:
    print("场景 5：治理路径演示，无模型确定性复现")

    model = select_model_client(SCRIPT, force_scripted=True)
    store, bus, cp, model, worker, gate = build(SCRIPT, matrix=matrix, model=model)

    # 建 Plan 之前先把两个 id 拿到手：需求归一跑在 create_plan 之前，不先生成 id，
    # 它落的 SkillInvoked 就只能挂空串，成为 trace 里认领不了的游离事件。
    trace_id = new_id("trace")
    plan_id = new_id("plan")
    goal, aggregated = _intake_goal(store, plan_id=plan_id, trace_id=trace_id)
    print(f"多源信号聚合：{aggregated['summary']}")

    mgr = ManagerAgent(model)

    def replanner(*, goal: str, findings: list[dict], open_tasks: list[dict]) -> list[dict]:
        """带全部 findings 让 Manager 重规划剩余工作。

        控制面不认识 ManagerAgent，只认这个回调（control_plane.set_replanner）——
        模型调用留在场景层，控制面那边一行模型代码都没有。
        """
        specs = mgr.plan(
            f"重新规划：原目标「{goal}」的首版方案被判定不可行，"
            f"累计 {len(findings)} 条问题，请给出替代方案")
        # 下一轮的报告在这里预置：本回调跑在 _apply_replan 与 start_plan 之前，
        # 此刻 attempt 还是旧值，派发时才 +1（control_plane.py:169）。
        for task in open_tasks:
            if task["role"] == "coding":
                _seed_report(store, task["plan_id"], task["attempt"] + 1)
        return specs

    cp.set_replanner(replanner)

    cp.create_plan(goal=goal, trace_id=trace_id, tasks=mgr.plan(goal), plan_id=plan_id)
    _seed_report(store, plan_id, 1)          # 方案甲这一轮
    cp.start_plan(plan_id)
    run_until_settled(bus, gate, cp, plan_id)

    # Plan 终态后才复盘 —— 模型调用不进 Control Plane，这是 finalizer 独立存在的理由
    finalizer = PlanFinalizer(store, model=model)
    sunk = finalizer.poll(plan_id)

    dump(cp, plan_id, "场景 5：治理路径（重规划）闭环")
    replans = sum(1 for e in cp.store.list_event_log(plan_id)
                  if e["event_type"] == "Replanned")
    print(f"  重规划次数: {replans}（上限 MAOS_MAX_REPLAN，默认 2，超限转人工不自旋）")
    print(f"  知识沉淀: {len(sunk)} 条")
    for row in store.list_knowledge():
        print(f"    [{row['kind']}] {row['title']}")

    plan = cp.store.get_plan(plan_id)
    assert plan["state"] == PlanState.DONE, f"治理路径应收敛到 DONE，实际 {plan['state']}"
    assert replans == 1, f"应恰好重规划 1 次，实际 {replans}"
    return 0
