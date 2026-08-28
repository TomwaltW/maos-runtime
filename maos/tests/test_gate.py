"""ReviewerGate 验收闸的口径锁死在这里 —— 两条分支各锁各的。

Phase 2 把验收闸劈成了两半，两半的判据都在本文件里把守：

  · **代码类任务**（本轮产出含 patch_set / test_report）：证据是同 attempt 的
    test_report。没有报告就是 blocker，**不回落 self_check** —— 这是本轮的题眼。
    一份 build/lint 全写 pass 的补丁集，没有跑出来的证据照样过不了闸；否则
    「Agent 自称完成」又被放回验收依据里，这次改造就白做了。
  · **非代码类任务**（requirement / architecture / review_note 产物）：证据仍是
    self_check，口径与改造前一字不变，原有断言的语义全部活在下半部分。

非代码类那半保留了两条老结论，两条都不是洁癖：
  · "没自检"必须判成 finding，不能因为 .get 兜了个 {} 就静默放行；
  · 形状不对（None / 字符串 / 列表）必须按"没自检"处理，**不能抛** ——
    flows/common.py 的驱动循环是裸调 review_pending()，异常逃出去整个 plan
    当场崩，连退化成一次 rework 都做不到。

测试走 review_pending() 而不是直接调 _gate_acceptance()，就是因为要验的正是
"异常会不会从这个入口逃出去"，那才是驱动循环真实的调用形状。
"""

from __future__ import annotations

import json
import pathlib

import pytest

from maos.artifacts import (
    KIND_ARCH_CONTRACT,
    KIND_COMPENSATION,
    KIND_PATCH_SET,
    KIND_TEST_REPORT,
)
from maos.contracts.events import Topic
from maos.contracts.states import PlanState, Risk, TaskState
from maos.core.control_plane import ControlPlane
from maos.core.eventbus import EventBus
from maos.core.store import SqliteStore
from maos.runtime.gate import ReviewerGate

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

_MISSING = object()  # 与 None 区分：键根本不在 vs 键在但值是 null

PATCH_FILES = [{"path": "src/auth.py", "diff": "@@ -12,3 +12,4 @@\n+    verify_token(t)"}]


class _RecordingBus(EventBus):
    """只记不发。Gate 的判定结果从 REVIEW_VERDICT 出来，这里原样收下。

    不用 InMemoryEventBus 是为了不让 ControlPlane 的订阅在 drain 时跟着跑状态迁移 ——
    这里测的是 Gate 的判定，不是状态机。
    """

    def __init__(self) -> None:
        self.published: list[tuple[str, object]] = []

    def publish(self, topic, env) -> None:
        self.published.append((topic, env))

    def subscribe(self, topic, group, handler) -> None:
        pass

    def drain(self, max_rounds: int = 1000) -> int:
        return 0


def _bare_report(**over) -> dict:
    """一份形状合法的 test_report，按需覆盖字段。"""
    report = {"passed": 0, "failed": 0, "errors": 0, "cases": [],
              "duration": 0.0, "tool_error": None, "summary": "报告"}
    report.update(over)
    return report


def _run_gate(artifacts: list[dict], *, task_over: dict | None = None,
              extra_tasks: list[dict] | None = None) -> dict:
    """造一个 AWAITING_REVIEW 的任务 + 若干 artifact，跑 Gate，返回 verdict payload。

    artifacts 里每项形如 {"kind": ..., "content": ..., "task_id"?: ..., "version"?: ...}；
    不给 task_id 就挂在被评审的任务 t1 上。
    """
    store = SqliteStore()
    store.init_schema()
    bus = _RecordingBus()
    gate = ReviewerGate(store, bus, ControlPlane(store, bus))

    store.insert_plan({"plan_id": "p1", "trace_id": "tr",
                       "goal": "g", "state": PlanState.RUNNING})
    task = {"task_id": "t1", "plan_id": "p1", "trace_id": "tr", "role": "coding",
            "title": "修复 token 校验缺失",
            "state": TaskState.AWAITING_REVIEW, "attempt": 1}
    task.update(task_over or {})
    store.insert_task(task)
    for extra in extra_tasks or []:
        store.insert_task({"plan_id": "p1", "trace_id": "tr", "role": "testing",
                           "title": "验证", "state": TaskState.DONE, "attempt": 1, **extra})

    for i, art in enumerate(artifacts):
        store.insert_artifact({
            "artifact_id": f"a{i}", "task_id": art.get("task_id", "t1"), "plan_id": "p1",
            "kind": art["kind"], "version": art.get("version", 1), "content": art["content"],
        })

    assert gate.review_pending("p1") == 1, "AWAITING_REVIEW 的任务没有被 Gate 取到"
    topic, env = bus.published[-1]
    assert topic == Topic.REVIEW_VERDICT, f"Gate 发到了 {topic}，不是 REVIEW_VERDICT"
    return env.payload


def _findings(payload: dict, gate: str) -> list[dict]:
    return [f for f in payload["findings"] if f["gate"] == gate]


# ======================================================================
# 代码类：证据是测试报告，不是自检
# ======================================================================
def _patch_content(self_check=_MISSING) -> dict:
    content = {"files": list(PATCH_FILES), "summary": "修复 token 校验缺失"}
    if self_check is not _MISSING:
        content["self_check"] = self_check
    return content


def test_code_task_without_report_is_blocker_even_if_self_check_all_pass():
    """题眼：自检全 pass 的补丁集，没有测试报告一样是 blocker，且不许回落 self_check。

    回落会让这次改造彻底失效 —— 一个把 build/lint 写成 pass 的模型输出，
    又变回了"任务完成"的充分证据。
    """
    payload = _run_gate([
        {"kind": KIND_PATCH_SET, "content": _patch_content({"build": "pass", "lint": "pass"})},
    ])
    fs = _findings(payload, "acceptance")
    assert fs, "代码类任务没有测试报告竟然被放行"
    assert all(f["severity"] == "blocker" for f in fs), \
        f"无报告必须是 blocker（不是 major、更不是 minor），实际 {[f['severity'] for f in fs]}"
    assert payload["verdict"] == "rework"


def test_code_task_failed_cases_become_one_major_finding_each():
    """有 failed 用例 -> 逐条 major，条数与 cases 里的失败条数一致，每条带 id / msg。

    条数一致这件事不是好看：Coding Agent 拿 findings 逐条修，
    合成一条"有几个用例挂了"等于把返工重新变成猜谜。
    """
    cases = [
        {"id": "tests/test_a.py::test_ok", "status": "passed", "msg": ""},
        {"id": "tests/test_a.py::test_expiry", "status": "failed", "msg": "AssertionError: 早退了"},
        {"id": "tests/test_b.py::test_scope", "status": "failed", "msg": "AssertionError: 越权"},
    ]
    payload = _run_gate([
        {"kind": KIND_PATCH_SET, "content": _patch_content({"build": "pass", "lint": "pass"})},
        {"kind": KIND_TEST_REPORT,
         "content": _bare_report(passed=1, failed=2, cases=cases)},
    ])
    fs = _findings(payload, "acceptance")
    failed_cases = [c for c in cases if c["status"] == "failed"]
    assert len(fs) == len(failed_cases), \
        f"findings 条数 {len(fs)} 与 failed 用例条数 {len(failed_cases)} 不一致"
    assert all(f["severity"] == "major" for f in fs), "失败用例应判 major，不是 blocker"
    assert {f["id"] for f in fs} == {c["id"] for c in failed_cases}
    assert all(f["msg"] for f in fs), "每条 finding 必须带上用例自己的 msg，否则修不了"


def test_code_task_tool_error_is_blocker_not_zero_failures():
    """tool_error 与 failed 是两回事：工具没跑成 = 没有证据 = blocker。

    把"没跑成"读成"0 条失败"是这条链路上最容易造出的假绿 ——
    沙箱挂了、镜像没了、超时被杀，报告里 failed 都是 0。
    """
    payload = _run_gate([
        {"kind": KIND_PATCH_SET, "content": _patch_content({"build": "pass", "lint": "pass"})},
        {"kind": KIND_TEST_REPORT,
         "content": _bare_report(tool_error="skill_not_found:test.verify")},
    ])
    fs = _findings(payload, "acceptance")
    assert fs and all(f["severity"] == "blocker" for f in fs), \
        "带 tool_error 的报告被当成了 0 条失败放行"


def test_code_task_declared_failures_must_be_listed():
    """报告声明 failed=3 却一条 case 都不列 -> 证据不完整，仍要判。

    只按 cases 判会让这种报告静默过闸，而它恰恰是最该拦的一种。
    """
    payload = _run_gate([
        {"kind": KIND_PATCH_SET, "content": _patch_content({"build": "pass", "lint": "pass"})},
        {"kind": KIND_TEST_REPORT, "content": _bare_report(failed=3, cases=[])},
    ])
    assert _findings(payload, "acceptance"), "声明有失败却不列用例的报告被放行了"


def test_code_task_all_green_report_passes():
    """报告全过 -> 验收闸不判。否则场景 1 直接红。"""
    payload = _run_gate([
        {"kind": KIND_PATCH_SET, "content": _patch_content({"build": "fail", "lint": "fail"})},
        {"kind": KIND_TEST_REPORT,
         "content": _bare_report(passed=2,
                                 cases=[{"id": "t::a", "status": "passed", "msg": ""},
                                        {"id": "t::b", "status": "passed", "msg": ""}])},
    ])
    assert _findings(payload, "acceptance") == [], \
        "报告全过却被判了 finding —— 注意 self_check 在代码类分支上不该有任何话语权"


def test_report_from_verifier_task_is_claimed_by_target():
    """报告挂在验证方名下、用 target_task_id 指认被验任务时，验收闸必须认领它。

    这是"验证与产出分属两个任务"时唯一的接头方式：没有这条，
    补丁和报告分居两个 task_id，Gate 永远看不到彼此。
    """
    payload = _run_gate(
        [
            {"kind": KIND_PATCH_SET, "content": _patch_content({"build": "pass", "lint": "pass"})},
            {"kind": KIND_TEST_REPORT, "task_id": "t-verifier", "version": 1,
             "content": _bare_report(passed=1, target_task_id="t1", target_attempt=1,
                                     cases=[{"id": "t::a", "status": "passed", "msg": ""}])},
        ],
        extra_tasks=[{"task_id": "t-verifier"}],
    )
    assert _findings(payload, "acceptance") == [], "验证方挂过来的报告没有被认领"


# ======================================================================
# 非代码类：口径与改造前一字不变
# ======================================================================
def _arch_content(self_check=_MISSING) -> dict:
    content = {"api": {}, "idempotency": {}, "audit": {},
               "reversibility": {"reversible_kinds": [KIND_PATCH_SET]},
               "summary": "架构契约"}
    if self_check is not _MISSING:
        content["self_check"] = self_check
    return content


def _acceptance_findings(self_check) -> list[dict]:
    """只看 acceptance 这道闸的 findings，别的闸（evidence 等）不干扰判定。"""
    return _findings(
        _run_gate([{"kind": KIND_ARCH_CONTRACT, "content": _arch_content(self_check)}],
                  task_over={"role": "architecture"}),
        "acceptance")


def _acceptance_findings_no_raise(self_check, label: str) -> list[dict]:
    """断言"不抛"——不区分异常类型，抛任何东西都是失败。"""
    try:
        return _acceptance_findings(self_check)
    except Exception as exc:  # noqa: BLE001 —— 这里要的就是"任何异常都不许有"
        pytest.fail(f"self_check={label} 时 Gate 抛了 {exc!r}；"
                    f"review_pending() 在 flows/common.py 是裸调用，异常逃出即整个 plan 崩")


# ---------------------------------------------------------------- 缺失半
def test_self_check_missing_is_finding():
    """键缺失必须判 finding。改动前 .get(..., {}) 让循环一次都不进，静默判 pass。"""
    assert _acceptance_findings(_MISSING), "self_check 缺失竟然被当成自检通过放行"


# ---------------------------------------------------------------- 崩溃半
def test_self_check_none_does_not_raise():
    """self_check 为 null：按"没自检"判 finding，不许抛 AttributeError。"""
    assert _acceptance_findings_no_raise(None, "null"), "self_check 为 null 竟然被放行"


def test_self_check_str_does_not_raise():
    """self_check 是字符串（模型直接吐了 "pass"）：同上，按"没自检"处理。"""
    assert _acceptance_findings_no_raise("pass", '"pass"'), "self_check 是字符串竟然被放行"


def test_self_check_list_does_not_raise():
    """self_check 是列表：同上。isinstance 兜的是"不是 dict"，不是"是 None"。"""
    assert _acceptance_findings_no_raise(["pass", "pass"], "[...]"), \
        "self_check 是列表竟然被放行"


# ---------------------------------------------------------------- 防回归
def test_self_check_fail_is_finding():
    """现有行为不许退：build=fail 出 finding，severity 仍是 major（不是 blocker）。"""
    fs = _acceptance_findings({"build": "fail", "lint": "pass"})
    assert fs, "build=fail 没有被判 finding"
    assert all(f["severity"] == "major" for f in fs), \
        "severity 被提成了别的等级，会改变四场景的流转"


def test_self_check_all_pass_is_clean():
    """全 pass 不许判 —— 非代码类产物的验收证据就是它。"""
    assert _acceptance_findings({"build": "pass", "lint": "pass"}) == [], \
        "自检全 pass 竟然被判出 finding"


# ======================================================================
# 补偿干跑闸
# ======================================================================
def _golden_compensation() -> dict:
    """直接加载 C-5 golden fixture —— 不手搓 compensation dict。

    手搓的那份迟早和 fixture 分叉，而分叉是静默的：Gate 读不到 patch_ref
    就当没有补偿，闸空转，日志一片正常。
    """
    return json.loads((FIXTURES / "compensation_golden.json").read_text(encoding="utf-8"))


def test_compensation_dry_run_blocks_when_sandbox_unavailable():
    """沙箱未就位（Task-B 的桩抛 NotImplementedError）= 干跑不过 = blocker。

    "闸还没实现"不等于"这道闸不存在"：高风险任务不放行未经验证的补偿方案。
    """
    golden = _golden_compensation()
    ref = golden["content"]["patch_ref"]
    payload = _run_gate(
        [
            {"kind": KIND_PATCH_SET, "content": _patch_content({"build": "pass", "lint": "pass"})},
            {"kind": KIND_TEST_REPORT, "content": _bare_report(passed=1,
                cases=[{"id": "t::a", "status": "passed", "msg": ""}])},
            {"kind": KIND_COMPENSATION, "content": golden["content"]},
            # patch_ref 指向的原补丁集，供 resolve_patch_ref 命中
            {"kind": KIND_PATCH_SET, "task_id": ref["task_id"], "version": ref["attempt"],
             "content": _patch_content({"build": "pass", "lint": "pass"})},
        ],
        task_over={"effect_risk": Risk.HIGH},
        extra_tasks=[{"task_id": ref["task_id"]}],
    )
    fs = _findings(payload, "compensation")
    assert fs and all(f["severity"] == "blocker" for f in fs), \
        f"干跑不过必须判 blocker，实际 findings={fs}"


def test_compensation_missing_patch_ref_hard_fails():
    """缺 patch_ref 必须硬失败 —— 不许 .get("patch_ref", {}) 兜底。

    兜底的后果是补偿静默不执行、日志一片正常，直到演示现场才发现文件没还原。
    """
    payload = _run_gate(
        [
            {"kind": KIND_PATCH_SET, "content": _patch_content({"build": "pass", "lint": "pass"})},
            {"kind": KIND_TEST_REPORT, "content": _bare_report(passed=1,
                cases=[{"id": "t::a", "status": "passed", "msg": ""}])},
            {"kind": KIND_COMPENSATION, "content": {"mode": "reverse"}},
        ],
        task_over={"effect_risk": Risk.HIGH},
    )
    fs = _findings(payload, "compensation")
    assert fs and all(f["severity"] == "blocker" for f in fs), "缺 patch_ref 竟然没有硬失败"


def test_compensation_unresolvable_ref_is_blocker():
    """patch_ref 形状合法但解析不到原补丁集 -> blocker（补偿无从执行）。"""
    golden = _golden_compensation()
    payload = _run_gate(
        [
            {"kind": KIND_PATCH_SET, "content": _patch_content({"build": "pass", "lint": "pass"})},
            {"kind": KIND_TEST_REPORT, "content": _bare_report(passed=1,
                cases=[{"id": "t::a", "status": "passed", "msg": ""}])},
            {"kind": KIND_COMPENSATION, "content": golden["content"]},
        ],
        task_over={"effect_risk": Risk.HIGH},
    )
    fs = _findings(payload, "compensation")
    assert fs and all(f["severity"] == "blocker" for f in fs), "解析不到原补丁集竟然放行了"


def test_compensation_gate_is_silent_for_low_risk():
    """非高风险任务不跑干跑闸：它挡的是"批准即落地"那条路，低风险任务没有那条路。"""
    golden = _golden_compensation()
    payload = _run_gate([
        {"kind": KIND_PATCH_SET, "content": _patch_content({"build": "pass", "lint": "pass"})},
        {"kind": KIND_TEST_REPORT, "content": _bare_report(passed=1,
            cases=[{"id": "t::a", "status": "passed", "msg": ""}])},
        {"kind": KIND_COMPENSATION, "content": golden["content"]},
    ])
    assert _findings(payload, "compensation") == []
    # 只断这道闸的结果：golden fixture 的 content 没有 summary，evidence 闸会另判一条
    # minor，那是 evidence 的活，不该被算到干跑闸头上。
    assert payload["gate_results"]["compensation"] == "pass"
