"""四个新 Agent 的行为，以及它们与 Gate 合起来要成立的那句话：

    所有 Agent 都回复完成 ≠ 业务成功。

最后一个测试是这句话的机器版本：一条计划里每个 Agent 都返回 status=ok、
补丁的 self_check 全 pass、变更说明齐全，而计划仍然不会 DONE ——
因为没有一份跑出来的测试报告。这条断言红了，就说明验收又退回"自述制"了。
"""

from __future__ import annotations

import json
import pathlib

import pytest

from maos.agents import AGENT_POOL
from maos.agents.architecture import ArchitectureAgent, validate_architecture_contract
from maos.agents.base import AgentIdentity, TaskContext
from maos.agents.coding import CodingAgent
from maos.agents.requirement import KIND_REQUIREMENT, RequirementAgent
from maos.agents.reviewer import ReviewerAgent
# TestingAgent 改名导入：裸名以 Test 开头会被 pytest 当成测试类去收集并告警
from maos.agents.testing import SKILL_VERIFY, make_test_report
from maos.agents.testing import TestingAgent as _TestingAgent
from maos.artifacts import (
    KIND_ARCH_CONTRACT,
    KIND_PATCH_SET,
    KIND_REVIEW_NOTE,
    KIND_TEST_REPORT,
    validate_artifact,
)
from maos.contracts.states import PlanState, Risk, TaskState
from maos.flows.common import GOOD_PATCH, build, run_until_settled
from maos.model.client import ModelResponse, ScriptedModelClient
from maos.skills import registry

AGENTS_DIR = pathlib.Path(__file__).resolve().parents[1] / "agents"

NORMALIZED = json.dumps({
    "normalized_goal": "修复 token 校验缺失",
    "constraints": ["不改测试"],
    "acceptance_suggestions": ["token 过期分支有用例覆盖"],
}, ensure_ascii=False)

REVIEW_NOTE = json.dumps({
    "defects": [{"path": "src/auth.py", "severity": "minor", "note": "缺注释"}],
    "conclusion": "可放行",
}, ensure_ascii=False)


def _ctx(**over) -> TaskContext:
    base = dict(plan_id="p1", task_id="t1", trace_id="tr", attempt=1,
                inputs={}, acceptance=[], risk_level="L")
    base.update(over)
    return TaskContext(**base)


class _RaisingModel(ScriptedModelClient):
    """模型调用超时 —— Reviewer 的 needs_human 路径靠它触发。"""

    def __init__(self, exc: Exception) -> None:
        super().__init__({})
        self.exc = exc

    def complete(self, *, system, user, tier):
        raise self.exc


# ======================================================================
# 注册：投放即生效
# ======================================================================
def test_four_new_agents_are_registered_by_file_drop():
    """放文件即在 AGENT_POOL 里（C-2 pkgutil 自动发现），不改 __init__.py。"""
    expected = {
        "requirement": RequirementAgent,
        "architecture": ArchitectureAgent,
        "testing": _TestingAgent,
        "reviewer": ReviewerAgent,
        "coding": CodingAgent,
    }
    for role, cls in expected.items():
        assert AGENT_POOL.get(role) is cls, f"{role} 没有按 role 注册到 AGENT_POOL"


def test_agents_init_keeps_no_explicit_import_list():
    """__init__.py 里不许出现逐个 Agent 的 import —— 显式清单等于多轨同改一处，合并必冲突。"""
    source = (AGENTS_DIR / "__init__.py").read_text(encoding="utf-8")
    for module in ("requirement", "architecture", "testing", "reviewer", "coding"):
        assert f"import maos.agents.{module}" not in source, \
            f"__init__.py 里出现了对 {module} 的显式 import，自动发现被改回手工清单了"


# ======================================================================
# Requirement：说不清就停，不替人拍板
# ======================================================================
def test_requirement_produces_acceptance():
    agent = RequirementAgent(ScriptedModelClient({"原始目标": NORMALIZED}))
    out = agent.run(_ctx(inputs={"goal": "修复 token 校验缺失"},
                         acceptance=["build 通过"]))

    assert out.status == "ok"
    content = out.artifacts[0]["content"]
    assert out.artifacts[0]["kind"] == KIND_REQUIREMENT
    assert content["acceptance"][0] == "build 通过", "任务自带的验收必须在前且不被顶替"
    assert "token 过期分支有用例覆盖" in content["acceptance"], "归一建议没有被并进来"
    assert content["self_check"] == {"build": "pass", "lint": "pass"}, \
        "非代码类产物的验收证据仍是 self_check，缺了 Gate 会一直判 finding"


def test_requirement_blocks_when_open_questions_present():
    """open_questions 非空 -> status=blocked，走状态机已有的 worker_blocked，不新增状态。"""
    agent = RequirementAgent(ScriptedModelClient({"原始目标": NORMALIZED}))
    out = agent.run(_ctx(inputs={"goal": "修复 token 校验缺失",
                                 "open_questions": ["过期时间以哪个时区为准？"]}))

    assert out.status == "blocked", "有未澄清的问题却没有 blocked，等于替用户拍了板"
    assert out.open_questions == ["过期时间以哪个时区为准？"]
    assert out.artifacts == [], "blocked 时不许顺手产出一份'看起来完成了'的需求产物"


def test_requirement_blocks_on_empty_goal():
    """目标为空同样要停：归一不出东西时编一个，错误会一路穿透到补丁。"""
    agent = RequirementAgent(ScriptedModelClient({"原始目标": NORMALIZED}))
    out = agent.run(_ctx(inputs={}))
    assert out.status == "blocked" and out.open_questions


# ======================================================================
# Architecture：可逆性判死在声明期
# ======================================================================
def _contract(**over) -> dict:
    base = {
        "api": {"endpoint": "x"}, "idempotency": {"key": "k"}, "audit": {"event_log": True},
        "reversibility": {"reversible_kinds": [KIND_PATCH_SET], "irreversible_kinds": []},
    }
    base.update(over)
    return base


def test_contract_missing_reversibility_fails_validation():
    errs = validate_architecture_contract(
        {k: v for k, v in _contract().items() if k != "reversibility"})
    assert errs and any("reversibility" in e for e in errs), \
        f"缺可逆性声明竟然通过了校验：{errs}"


def test_irreversible_artifact_cannot_be_high_effect_risk():
    """不可逆产物禁止标 effect_risk=H 自动执行 —— 判死在声明期，不指望审批人不出错。

    effect_risk=H 的含义是"人一批准就立即落地"。落地不可逆时，审批是唯一且
    不可撤回的一道闸，补偿闸只能空转。
    """
    irreversible = _contract(reversibility={
        "reversible_kinds": [KIND_PATCH_SET], "irreversible_kinds": ["email_sent"]})

    assert validate_architecture_contract(irreversible, effect_risk=Risk.LOW) == [], \
        "低风险下声明不可逆产物本身是合法的，不该被拒"
    errs = validate_architecture_contract(irreversible, effect_risk=Risk.HIGH)
    assert errs and any("effect_risk" in e for e in errs), \
        "不可逆产物标 H 竟然通过了校验"


def test_architecture_agent_emits_valid_contract():
    out = ArchitectureAgent(ScriptedModelClient({})).run(
        _ctx(inputs={"effect_risk": Risk.LOW}, acceptance=["a"]))

    assert out.status == "ok"
    art = out.artifacts[0]
    assert art["kind"] == KIND_ARCH_CONTRACT
    assert validate_artifact(KIND_ARCH_CONTRACT, art["content"]) == []
    assert art["content"]["reversibility"]["reversible_kinds"] == [KIND_PATCH_SET], \
        "git 补丁类必须声明为可逆 —— 零模型补偿的前提就是这条"


def test_architecture_agent_refuses_irreversible_high_risk():
    out = ArchitectureAgent(ScriptedModelClient({})).run(_ctx(inputs={
        "effect_risk": Risk.HIGH,
        "architecture": _contract(reversibility={
            "reversible_kinds": [KIND_PATCH_SET], "irreversible_kinds": ["db_drop"]}),
    }))
    assert out.status == "failed" and "effect_risk" in (out.error or "")
    assert out.artifacts == [], "校验没过还产出契约，下游会当成事实用"


# ======================================================================
# Testing：「没跑成」不是故障，但也不是证据
# ======================================================================
def test_test_verify_is_registered_after_task_b():
    """原哨兵的反向：Task-B 已合并，test.verify 必须在册。

    改向的缘由：这条原本断言「尚未注册」，用于在 B 合并当天报红、提醒把下面的
    软兜底断言换成真调用。B 已并入主干（合并 commit af1e438），哨兵使命完成，
    于是掉头守另一侧 —— 谁把这个 skill 摘了或改了名，下面两条立刻测不到真路径。
    """
    assert registry.get(SKILL_VERIFY) is not None, (
        f"{SKILL_VERIFY} 不在册了 —— Testing 角色没有测试执行入口，"
        f"下面两条会退回并行期的 skill_not_found 路径，测不到真调用"
    )


def test_testing_agent_soft_falls_back_without_raising():
    """工具没跑成 -> 软兜底，产出带 tool_error 的报告，**不抛**。

    抛出去会把整条链路挂在一个环境问题上。而 tool_error 必须留在报告里：
    它和「0 条失败」不是一回事，Gate 靠它判 blocker。

    workdir 给一个不存在的路径，走的是 test.verify **真调用**下工具没跑成那条：
    skill 按契约不抛、返回 ok，把原因写进报告的 tool_error（Task-B 合并前这里
    走的是 skill 未注册的 skill_not_found，两条路径的落点必须一样）。
    """
    agent = _TestingAgent(ScriptedModelClient({}))
    try:
        out = agent.run(_ctx(risk_level="M", inputs={"workdir": "/tmp/x"}))
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"工具没跑成时 Testing Agent 抛了 {exc!r}，软兜底没生效")

    assert out.status == "ok"
    content = out.artifacts[0]["content"]
    assert out.artifacts[0]["kind"] == KIND_TEST_REPORT
    assert content["tool_error"], "工具没跑成，报告必须带 tool_error —— 否则 Gate 判不出 blocker"
    assert "/tmp/x" in content["tool_error"], \
        f"tool_error 应转述工具给的原因，实得 {content['tool_error']!r}"
    assert content["failed"] == 0 and content["cases"] == [], \
        "没跑成的报告不许伪造失败数，判定归 Gate"
    assert validate_artifact(KIND_TEST_REPORT, content) == [], "软兜底报告的形状也必须合契约"


def test_tool_error_report_yields_to_scripted_report():
    """带 tool_error 的报告**不算真报告**，脚本化兜底必须够得着。

    这是 Task-B 合并当天场景 1/2 转红的那个缺口的回归守卫：test.verify 跑不成时
    仍返回 status=ok，若按 status 判「拿到真报告了」，inputs 里预置的 scripted_report
    就永远取不到 —— 报告带着 tool_error 进 Gate，被正确判成 blocker，演示链路全挂。

    判据必须是「报告自己带没带 tool_error」，不是 res.status。
    """
    scripted = make_test_report(
        passed=2, failed=0, cases=[{"id": "t::a", "status": "passed", "msg": ""}],
        summary="脚本化回归：2 过 0 挂")
    agent = _TestingAgent(ScriptedModelClient({}))
    out = agent.run(_ctx(risk_level="M", inputs={
        "workdir": "/tmp/definitely-not-a-workdir", "scripted_report": scripted}))

    content = out.artifacts[0]["content"]
    assert content["tool_error"] is None, \
        "工具没跑成时应让位给 scripted_report，而不是把 tool_error 报告交出去"
    assert content["passed"] == 2 and content["cases"], "让位后取的应是脚本化报告的内容"


def test_testing_agent_uses_scripted_report_and_marks_target():
    """Scripted 演示模式：无沙箱时报告从 inputs 来，并标明验的是谁的哪一次 attempt。"""
    scripted = make_test_report(
        passed=1, failed=1,
        cases=[{"id": "t::a", "status": "failed", "msg": "boom"}],
        summary="脚本化报告")
    out = _TestingAgent(ScriptedModelClient({})).run(_ctx(
        risk_level="M",
        inputs={"scripted_report": scripted, "verify_target": "t-code", "verify_attempt": 2}))

    content = out.artifacts[0]["content"]
    assert content["failed"] == 1 and content["tool_error"] is None
    assert content["target_task_id"] == "t-code" and content["target_attempt"] == 2


# ======================================================================
# Reviewer：审查没做成就是没做成
# ======================================================================
def test_reviewer_emits_review_note():
    out = ReviewerAgent(ScriptedModelClient({"语义审查产物清单": REVIEW_NOTE})).run(
        _ctx(inputs={"artifacts": [{"kind": KIND_PATCH_SET, "content": {}}]}))

    assert out.status == "ok"
    assert out.artifacts[0]["kind"] == KIND_REVIEW_NOTE
    assert out.artifacts[0]["content"]["conclusion"] == "可放行"
    assert len(out.artifacts[0]["content"]["defects"]) == 1


def test_reviewer_timeout_needs_human():
    """超时 -> needs_human，且**不产出**一份空白意见书。

    空白意见书会让下游以为"审过了、没问题"，比没有意见书危险得多。
    """
    out = ReviewerAgent(_RaisingModel(TimeoutError("模型 120s 未返回"))).run(_ctx())
    assert out.status == "blocked"
    assert out.metrics.get("needs_human") is True
    assert out.artifacts == []


def test_reviewer_unparseable_output_needs_human():
    class _Garbage(ScriptedModelClient):
        def complete(self, *, system, user, tier):
            return ModelResponse(text="当然没问题啦")

    out = ReviewerAgent(_Garbage({})).run(_ctx())
    assert out.status == "blocked" and out.metrics.get("needs_human") is True


# ======================================================================
# 题眼的端到端形态
# ======================================================================
def test_all_agents_reply_ok_but_plan_still_fails_without_test_report():
    """全员回复完成、补丁自检全 pass、说明齐全 —— 计划仍然不成功。

    这就是「所有 Agent 都回复完成 ≠ 业务成功」的机器版本。这条红了，
    说明验收又退回了"Agent 自述制"。
    """
    store, bus, cp, model, worker, gate = build({"任务输入": GOOD_PATCH})
    plan_id = cp.create_plan(goal="没有测试报告的代码任务", trace_id="tr", tasks=[{
        "task_id": "t-code", "role": "coding", "title": "改点东西",
        "inputs": {}, "acceptance": ["build 通过"], "risk_level": "L",
    }])
    cp.start_plan(plan_id)
    run_until_settled(bus, gate, cp, plan_id)

    task = cp.store.get_task("t-code")
    assert task["state"] != TaskState.DONE, "没有测试报告的代码任务竟然 DONE 了"
    assert cp.store.get_plan(plan_id)["state"] == PlanState.FAILED
    assert any(a["kind"] == KIND_PATCH_SET
               and a["content"]["self_check"] == {"build": "pass", "lint": "pass"}
               for a in cp.store.list_artifacts("t-code")), \
        "前提不成立：本测试要的正是'自检全 pass 却仍不算成功'"


def test_identity_whitelist_still_bites():
    """新 Agent 的 Identity 不是文档：白名单外的 skill 一律 PermissionDenied（A-5）。"""
    from maos.agents.base import PermissionDenied
    from maos.skills.invoker import SkillInvoker

    inv = SkillInvoker(_TestingAgent.identity, None)
    with pytest.raises(PermissionDenied):
        inv.invoke("code.repo-patch", {})        # coding 的 skill，不在 testing 白名单

    assert isinstance(ArchitectureAgent.identity, AgentIdentity)
    assert ArchitectureAgent.identity.allowed_skills == frozenset(), \
        "架构契约由规则装配，不该给它任何 skill 权限"
