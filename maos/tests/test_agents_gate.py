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
from maos.agents.testing import SKILL_VERIFY, make_test_report, seed_scripted_report
from maos.agents.testing import TestingAgent as _TestingAgent
from maos.artifacts import (
    KIND_ARCH_CONTRACT,
    KIND_PATCH_SET,
    KIND_REVIEW_NOTE,
    KIND_TEST_REPORT,
    validate_artifact,
)
from maos.contracts.events import Topic
from maos.contracts.states import PlanState, Risk, TaskState
from maos.core.control_plane import ControlPlane
from maos.core.eventbus import EventBus
from maos.core.store import SqliteStore
from maos.flows.common import BAD_PATCH, GOOD_PATCH, build, run_until_settled
from maos.model.client import ModelResponse, ScriptedModelClient, Tier
from maos.runtime.gate import ISOLATION_FINDING_ID, ISOLATION_PROBE_PREFIX, ReviewerGate
from maos.skills import registry
from maos.tools.sandbox import prepare_sandbox_workdir

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


def test_tool_error_report_is_handed_over_instead_of_a_scripted_stand_in():
    """假绿路径已删：沙箱没跑成时**不许**从 inputs 里换一份能过闸的报告交出去。

    这条守的是本轨存在的理由。原先 `_report_from` 有一级 `scripted_report` 回落：
    沙箱挂了就把预置报告当成本轮证据，于是演示当天 Docker 一挂，屏幕照样全绿
    而没有人知道。现在唯一的出口是带 tool_error 的报告，Gate 判 blocker、当场变红。

    inputs 里**故意仍然塞一份合法的脚本化报告**：它必须被无视。
    """
    stand_in = make_test_report(
        passed=2, failed=0, cases=[{"id": "t::a", "status": "passed", "msg": ""}],
        summary="脚本化回归：2 过 0 挂")
    agent = _TestingAgent(ScriptedModelClient({}))
    out = agent.run(_ctx(risk_level="M", inputs={
        "workdir": "/tmp/definitely-not-a-workdir", "scripted_report": stand_in}))

    content = out.artifacts[0]["content"]
    assert content["tool_error"], \
        "沙箱没跑成却交出了不带 tool_error 的报告 —— 假绿回落被加回来了"
    assert content["passed"] == 0 and content["cases"] == [], \
        f"没跑成的报告里出现了脚本化报告的内容：{content}"


def test_testing_agent_produces_a_real_report_and_marks_target(tmp_path, monkeypatch):
    """真跑一遍靶场：报告是 pytest 的产物，并标明验的是谁的哪一次 attempt。

    靶场打补丁前本来就红一条（B 埋的时区 bug），所以 `failed == 1` 同时证明了
    两件事：报告真的来自这次执行，而不是谁预置的；`target_*` 两个字段也真的
    落在了报告上 —— Gate 靠它们把报告认领到被验任务的验收闸上。
    """
    monkeypatch.setenv("MAOS_SANDBOX_FORCE_SUBPROCESS", "1")
    workdir = prepare_sandbox_workdir(str(tmp_path / "repo"))

    out = _TestingAgent(ScriptedModelClient({})).run(_ctx(
        risk_level="M",
        inputs={"workdir": workdir, "verify_target": "t-code", "verify_attempt": 2}))

    content = out.artifacts[0]["content"]
    assert content["tool_error"] is None, content["tool_error"]
    assert content["failed"] == 1, f"靶场打补丁前该红一条，实得 {content}"
    assert any(c["id"].endswith("test_expired_session") and c["status"] == "failed"
               for c in content["cases"]), f"报告里没有靶场那条真失败：{content['cases']}"
    assert content["target_task_id"] == "t-code" and content["target_attempt"] == 2


def test_scenarios_1_and_2_carry_no_scripted_report_anymore():
    """宣称真跑的场景不许有脚本化报告 —— 判据不是「全仓不许有」。

    场景 3（审批）/ 5（补偿）不跑测试，报告在那里是前置条件不是产物，
    `seed_scripted_report()` 函数本体与它们的调用点都必须还在：删了函数，
    `maos/obs/trace.py` 那套「预置件无来源事件」的 provenance 判据当场作废。
    """
    flows = AGENTS_DIR.parent / "flows"
    for name in ("scenario_1.py", "scenario_2.py"):
        source = (flows / name).read_text(encoding="utf-8")
        assert "seed_scripted_report" not in source, f"{name} 又把预置报告加回来了"
        assert "scripted_report" not in source, f"{name} 里出现了脚本化报告字段"

    assert callable(seed_scripted_report), "seed_scripted_report 被删了"
    for name in ("scenario_3.py", "scenario_5.py"):
        source = (flows / name).read_text(encoding="utf-8")
        assert "seed_scripted_report" in source, \
            f"{name} 的前置报告被误删了 —— 它不跑测试，报告是前置条件不是产物"


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


# ======================================================================
# 隔离探针不进业务判据：挡闸要挡，但不喂给模型
# ======================================================================
class _RecordingBus(EventBus):
    """只记不发 —— 这里要看的是 Gate 的判定，不是状态机跟着跑。"""

    def __init__(self) -> None:
        self.published: list[tuple[str, object]] = []

    def publish(self, topic, env) -> None:
        self.published.append((topic, env))

    def subscribe(self, topic, group, handler) -> None:
        pass

    def drain(self, max_rounds: int = 1000) -> int:
        return 0


def _gate_verdict(report: dict) -> dict:
    """把一份 test_report 挂到 AWAITING_REVIEW 的代码任务上，跑 Gate，返回 verdict。"""
    store = SqliteStore()
    store.init_schema()
    bus = _RecordingBus()
    gate = ReviewerGate(store, bus, ControlPlane(store, bus))

    store.insert_plan({"plan_id": "p1", "trace_id": "tr", "goal": "g",
                       "state": PlanState.RUNNING})
    store.insert_task({"task_id": "t1", "plan_id": "p1", "trace_id": "tr", "role": "coding",
                       "title": "改点东西", "state": TaskState.AWAITING_REVIEW, "attempt": 1})
    store.insert_artifact({"artifact_id": "a0", "task_id": "t1", "plan_id": "p1",
                           "kind": KIND_TEST_REPORT, "version": 1, "content": report})

    assert gate.review_pending("p1") == 1
    topic, env = bus.published[-1]
    assert topic == Topic.REVIEW_VERDICT
    return env.payload


def _report(cases: list[dict]) -> dict:
    failed = sum(1 for c in cases if c["status"] in ("failed", "error"))
    return make_test_report(passed=len(cases) - failed, failed=failed, cases=cases,
                            summary="沙箱回归")


_PROBE_CASE = {"id": f"{ISOLATION_PROBE_PREFIX}test_no_network",
               "status": "failed", "msg": "socket 连上了 1.1.1.1"}
_BIZ_CASE = {"id": "tests.test_session::test_expired_session",
             "status": "failed", "msg": "没到 TTL 就被判过期了"}


def test_isolation_probe_failure_still_blocks_the_gate():
    """隔离失效比一条用例挂严重得多 —— 探针红了照样不许放行。"""
    payload = _gate_verdict(_report([_PROBE_CASE]))

    assert payload["verdict"] == "rework", "隔离探针挂了竟然放行了"
    blockers = [f for f in payload["findings"] if f.get("severity") == "blocker"]
    assert len(blockers) == 1 and blockers[0]["id"] == ISOLATION_FINDING_ID, \
        f"探针失败没有压成一条 blocker：{payload['findings']}"


def test_isolation_probe_cases_never_reach_the_coding_findings():
    """探针的用例名不许出现在喂回 Coding 的 findings 里 —— 模型会去改它读不懂的东西。

    findings 会被 `code_repo_patch._build_prompt` 原样 json.dumps 进返工提示词，
    所以判据就按序列化后的整串来看：探针的用例 id 一个字都不许出现。
    """
    payload = _gate_verdict(_report([_PROBE_CASE, _BIZ_CASE]))
    findings = payload["findings"]

    assert _PROBE_CASE["id"] not in json.dumps(findings, ensure_ascii=False), \
        f"探针用例名进了喂回 Coding 的 findings：{findings}"

    majors = [f for f in findings if f.get("severity") == "major"]
    assert [f["id"] for f in majors] == [_BIZ_CASE["id"]], \
        f"业务用例的逐条 finding 被探针带偏了：{majors}"
    assert _BIZ_CASE["msg"] in majors[0]["msg"], "业务失败的 msg 没有原样喂回"


def test_all_green_report_passes_even_with_probes_present():
    """探针只在**挂了**的时候特殊 —— 全绿报告里它们不该留下任何 finding。"""
    payload = _gate_verdict(_report([
        {"id": f"{ISOLATION_PROBE_PREFIX}test_no_host_secrets", "status": "passed", "msg": ""},
        {"id": "tests.test_session::test_valid_session", "status": "passed", "msg": ""},
    ]))
    assert payload["verdict"] == "pass", payload["findings"]


# ======================================================================
# 回归钉子：这三处当初「改回去也不会红」
# ======================================================================
def test_gate_does_not_raise_on_non_dict_self_check():
    """self_check 不是 dict 时 Gate 判 finding，**不抛** —— review_pending 是裸调用。

    异常从这里逃出去，flows/common.py 的驱动循环当场崩，整个 plan 连退化成
    一次 rework 都做不到。
    """
    store = SqliteStore()
    store.init_schema()
    bus = _RecordingBus()
    gate = ReviewerGate(store, bus, ControlPlane(store, bus))
    store.insert_plan({"plan_id": "p1", "trace_id": "tr", "goal": "g",
                       "state": PlanState.RUNNING})
    store.insert_task({"task_id": "t1", "plan_id": "p1", "trace_id": "tr",
                       "role": "architecture", "title": "契约",
                       "state": TaskState.AWAITING_REVIEW, "attempt": 1})
    store.insert_artifact({"artifact_id": "a0", "task_id": "t1", "plan_id": "p1",
                           "kind": KIND_ARCH_CONTRACT, "version": 1,
                           "content": {"self_check": None, "summary": "契约"}})

    try:
        assert gate.review_pending("p1") == 1
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"self_check 为 null 时 Gate 抛了 {exc!r}，驱动循环会被掀掉")

    payload = bus.published[-1][1].payload
    assert any(f["gate"] == "acceptance" for f in payload["findings"]), \
        "self_check 为 null 被当成自检通过放行了"


def test_scenario_1_injects_the_selected_model_client_into_build(monkeypatch):
    """场景 1 的真模型接线点：`model=select_model_client(script)` 必须进 build()。

    这一处被改回 `build(script)` 也不会红任何存量用例 —— 无 key 时
    select_model_client 本来就降级成 ScriptedModelClient，行为一模一样，
    只有在有 key 的机器上才看得出「真模型根本没接上」。所以钉在这里。
    """
    from maos.flows import scenario_1

    class _Stop(Exception):
        pass

    sentinel = ScriptedModelClient({})
    seen: dict = {}
    monkeypatch.setattr(scenario_1, "select_model_client", lambda script: sentinel)

    def _fake_build(script, *, matrix=False, model=None):
        seen["model"] = model
        raise _Stop

    monkeypatch.setattr(scenario_1, "build", _fake_build)
    with pytest.raises(_Stop):
        scenario_1.run()

    assert seen["model"] is sentinel, \
        "场景 1 没把 select_model_client 的结果注入 build()，真模型接线点断了"


def test_scenario_2_flaky_model_dispatches_on_the_rework_marker():
    """FlakyModel 按「返工」二字分派，不按调用序数。

    按序数计数对新增的 model 调用不设防（requirement 与 reviewer 都会调模型），
    改回去当天场景 2 会在一个看不出因果的地方失真，所以钉住。
    """
    from maos.flows.scenario_2 import FlakyModel

    model = FlakyModel({})
    rework = model.complete(system="", tier=Tier.MEDIUM,
                            user="任务：修复\n\n这是第 2 次返工，必须逐条解决以下问题：[]")
    first = model.complete(system="", tier=Tier.MEDIUM, user="任务：修复\n\n验收标准：[]")

    assert rework.text == GOOD_PATCH, "带「返工」的提示词没拿到修好的补丁"
    assert first.text == BAD_PATCH, "第一轮没拿到那份修不好的补丁"


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
