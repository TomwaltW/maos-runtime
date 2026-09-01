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
import re

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
from maos.runtime.gate import (
    DEFAULT_FINANCE_THRESHOLD,
    FINANCE_THRESHOLD_ENV,
    SEVERITY_INFO,
    ReviewerGate,
)

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


# ======================================================================
# 财务复核闸（第六道闸）—— 判据是跨轨冻结契约 F-1，两轨照同一份
# ======================================================================
# 写闸的一轨与产数的一轨只要有一处口径分叉，症状就是「两轨各自都绿、合并后闸恒
# blocker 或恒 pass」，而且要到跑退款场景才暴露。所以 F-1 的每一条都在下面有一条
# 断言把守：触发面（biz_type + 阈值）、判据（非空 dict）、以及闸不许认识退款域。

# 产数那一轨用什么 kind 挂 finance_entry，F-1 没有规定（它只说"任一 artifact"）。
# 这里刻意用一个闸侧不认识的 kind：判定跟着数据形状走，不跟着 kind 走 ——
# 哪天产数侧改了 kind 名，这道闸不该跟着红。
FINANCE_CARRIER_KIND = "refund_settlement"

REFUND_OVER = {"biz_type": "refund", "amount_claimed": 9000}


@pytest.fixture
def default_threshold(monkeypatch):
    """把阈值 env 摘干净再断默认值。

    本机若外挂了 MAOS_FINANCE_THRESHOLD，"默认 5000" 那几条会假红/假绿，
    而假绿是更坏的一种：闸看起来在守，其实守的是别人的阈值。
    """
    monkeypatch.delenv(FINANCE_THRESHOLD_ENV, raising=False)
    return DEFAULT_FINANCE_THRESHOLD


def _finance_entry_artifact(entry) -> dict:
    # 带 summary 是为了让 evidence 闸别在这几条测试里另判一条 minor，
    # 那会把 verdict 搅成 rework，掩盖财务闸自己的判定。
    return {"kind": FINANCE_CARRIER_KIND,
            "content": {"finance_entry": entry, "summary": "财务核算"}}


def _finance_payload(inputs, extra_artifacts=None) -> dict:
    """跑一遍 Gate。底料是一份全绿的补丁 + 报告，让前五道闸都不出声。"""
    base = [
        {"kind": KIND_PATCH_SET, "content": _patch_content({"build": "pass", "lint": "pass"})},
        {"kind": KIND_TEST_REPORT, "content": _bare_report(
            passed=1, cases=[{"id": "t::a", "status": "passed", "msg": ""}])},
    ]
    return _run_gate(base + list(extra_artifacts or []),
                     task_over={"inputs": inputs})


def test_finance_gate_is_silent_for_non_refund_task(default_threshold):
    """场景 1-5 的形状：inputs 里没有 biz_type -> 这道闸恒不触发。

    金额字段故意给一个远超阈值的数：不是 refund 就不该看金额，
    否则任何带 amount 字样的普通任务都会被误伤。
    """
    payload = _finance_payload({"workdir": "/tmp/probe", "amount_claimed": 999999})
    assert _findings(payload, "finance") == [], "非退款任务被财务闸拦下了"
    assert payload["gate_results"]["finance"] == "pass"
    assert payload["verdict"] == "pass", "加了第六道闸之后，原本该过的任务过不去了"


def test_scenario_flows_1_to_5_never_set_biz_type():
    """把"场景 1-5 恒不触发"钉在源码上，而不是只钉在造出来的 dict 上。

    上一条测的是"没有 biz_type 就不触发"，这一条测"场景 1-5 确实没有 biz_type"。
    两条合起来才是完整的回归闸：少了后者，哪天有人给存量场景塞了退款输入，
    前者依然绿，而演示当场从 DONE 变 BLOCKED。
    只点名 1-5：退款场景（6/7）本来就该带 biz_type，不该被这条误伤。
    """
    flows = pathlib.Path(__file__).resolve().parents[1] / "flows"
    for n in range(1, 6):
        src = (flows / f"scenario_{n}.py").read_text(encoding="utf-8")
        assert "biz_type" not in src, \
            f"scenario_{n}.py 出现了 biz_type —— 存量场景会开始触发财务复核闸"


def test_finance_gate_blocks_refund_over_threshold_without_entry(default_threshold):
    """退款 + 超阈值 + 没有财务凭据 -> blocker。这道闸的正例。"""
    payload = _finance_payload(REFUND_OVER)
    fs = _findings(payload, "finance")
    assert fs and all(f["severity"] == "blocker" for f in fs), \
        f"漏掉财务复核的高额退款竟然被放行，findings={fs}"
    assert "finance_entry" in fs[0]["message"], "finding 没说清缺的是什么，模型修不了"
    assert payload["verdict"] == "rework"


def test_finance_gate_passes_with_non_empty_entry(default_threshold):
    """同 attempt 里有一份带非空 finance_entry 的 artifact -> pass。"""
    payload = _finance_payload(
        REFUND_OVER,
        [_finance_entry_artifact({"amount_approved": 9000, "rule_refs": ["R-3"]})],
    )
    assert _findings(payload, "finance") == [], "有财务凭据却被判了 finding"
    assert payload["verdict"] == "pass"


@pytest.mark.parametrize("entry,label", [
    ({}, "空 dict"),
    (None, "null"),
    ("已核算", "字符串"),
    ([{"amount_approved": 9000}], "列表"),
])
def test_finance_gate_rejects_non_dict_or_empty_entry(entry, label, default_threshold):
    """"键在" 不等于 "算过了"。

    空 dict 是"跑过了但什么都没算出来"，字符串是模型直接吐了一句自述 ——
    放行任何一种，判据就从"有没有财务凭据"降级成"有没有这个键"，
    而后者是产数侧一行 setdefault 就能满足的，闸等于空转。
    """
    payload = _finance_payload(REFUND_OVER, [_finance_entry_artifact(entry)])
    fs = _findings(payload, "finance")
    assert fs and all(f["severity"] == "blocker" for f in fs), \
        f"finance_entry 是{label}竟然被当成财务凭据放行"


def test_finance_gate_is_silent_at_exactly_threshold(default_threshold):
    """判据是"大于阈值"，不是"大于等于"。等于阈值的那一笔不触发。

    钉住边界是因为 5000 这个数会被两轨各写一次（闸一次、产数一次），
    差一个等号就是"两轨各自都绿、合起来判反"。
    """
    payload = _finance_payload({"biz_type": "refund",
                                "amount_claimed": default_threshold})
    assert _findings(payload, "finance") == [], "金额恰好等于阈值不该触发财务复核闸"


def test_finance_gate_reads_threshold_from_env(monkeypatch):
    """阈值现读 env，不在 import 时固化 —— 否则改阈值得重启进程。"""
    inputs = {"biz_type": "refund", "amount_claimed": 200}

    monkeypatch.delenv(FINANCE_THRESHOLD_ENV, raising=False)
    assert _findings(_finance_payload(inputs), "finance") == [], \
        "200 元在默认阈值 5000 之下，不该触发"

    monkeypatch.setenv(FINANCE_THRESHOLD_ENV, "100")
    assert _findings(_finance_payload(inputs), "finance"), \
        "env 把阈值压到 100 之后，200 元仍然没触发 —— 阈值被固化了"


def test_finance_gate_bad_env_threshold_falls_back_and_does_not_raise(monkeypatch):
    """阈值写错（"五千"）-> 回落默认值并继续判，不抛、也不放开闸。

    回落方向必须是收严：宁可多拦一次，也不能因为配置写错就漏掉财务复核。
    Gate 抛异常的代价另有一层 —— review_pending() 在 flows/common.py 是裸调用，
    异常逃出去整个 plan 当场崩。
    """
    monkeypatch.setenv(FINANCE_THRESHOLD_ENV, "五千")
    assert _findings(_finance_payload(REFUND_OVER), "finance"), \
        "阈值配错之后闸被放开了"
    assert _findings(_finance_payload({"biz_type": "refund", "amount_claimed": 100}),
                     "finance") == [], "回落的阈值不是默认值 5000"


def test_finance_gate_unparseable_amount_is_not_silently_passed(default_threshold):
    """金额解析不出数 -> 按触发处理，不按 0 放过。

    float("六千") 会抛；吞掉当 0，一笔字段脏掉的高额退款就悄悄绕过了财务复核。
    这与把 tool_error 读成"0 条失败"是同一类假绿：出问题的那条恰恰最该拦。
    """
    fs = _findings(_finance_payload({"biz_type": "refund", "amount_claimed": "六千"}),
                   "finance")
    assert fs and all(f["severity"] == "blocker" for f in fs), \
        "金额解析不出数值竟然被当成 0 元放过了"
    assert "六千" in fs[0]["message"], "finding 得指出是哪个值解析不了"


@pytest.mark.parametrize("inputs,label", [
    (None, "inputs 为 null"),
    ("refund", "inputs 是字符串"),
    ({"biz_type": "refund", "amount_claimed": {"n": 9000}}, "金额是 dict"),
    ({"biz_type": None, "amount_claimed": 9000}, "biz_type 为 null"),
])
def test_finance_gate_does_not_raise_on_odd_inputs(inputs, label, default_threshold):
    """形状怪的 inputs 一律不许抛 —— Gate 是独立判定面，不能假设上游收敛过形状。"""
    try:
        payload = _finance_payload(inputs)
    except Exception as exc:  # noqa: BLE001 —— 这里要的就是"任何异常都不许有"
        pytest.fail(f"{label} 时 Gate 抛了 {exc!r}；review_pending() 是裸调用，"
                    f"异常逃出即整个 plan 崩")
    assert "finance" in payload["gate_results"], "第六道闸没有出现在 gate_results 里"


def test_runtime_and_core_do_not_import_refund_domain():
    """铁律 9 推论：运行时内核不许 import 业务域，财务闸也不例外。

    这道闸是最容易破这条规矩的地方 —— 手册正文写的是"Gate 会查 finance_entry 表"，
    照着写就得 import maos.domain.refund。一旦 import，"换域只换
    Skill/ToolPort/业务对象"这句话当场作废，而那是复赛材料里最核心的一句。
    所以判据落在 artifact 的 content 上，并把这条钉成断言。
    """
    # 认 import 语句，不认字面量：闸的 docstring 里就写着"不许 import
    # maos.domain.refund"，按子串扫会把这句注释本身判成违例。
    imports_domain = re.compile(r"^\s*(?:from|import)\s+maos\.domain", re.MULTILINE)
    pkg = pathlib.Path(__file__).resolve().parents[1]
    offenders = [
        f"{p.parent.name}/{p.name}"
        for d in ("runtime", "core")
        for p in sorted((pkg / d).glob("*.py"))
        if imports_domain.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == [], f"运行时内核 import 了业务域：{offenders}"


# ======================================================================
# 第六道闸的阈值留痕（T48）—— 一个合法配置值不许把这道闸静默停用
# ======================================================================
# 补这一节之前的洞：MAOS_FINANCE_THRESHOLD=99999999 解析得通（不走
# _finance_threshold() 那条回落 WARNING），闸照判 pass 且一声不吭 ——
# 读 gate_results 的人分不出「这道闸没话说」和「这道闸被一个大数配置掉了」。
# 留痕用的是 _review 里现成的三态：info 不挡闸，但把结果从 pass 抬成 noted。
#
# 五条不许被改松的口径，各有一条断言把守：
#   · 不设变量 -> findings 逐条不变（缺省行为逐字节不变，143 条回归网的前提）；
#   · 合法但非缺省 -> noted + 一条 info，正文里两个数都在；
#   · 解析不出数 -> 只剩那条回落 WARNING，不许叠一层 info；
#   · 判定不许被改 -> 阈值调大之后该 pass 还是 pass，不是 fail；
#   · 两个调用点共用一条痕 -> 同一次 _review 里至多出现一条。

RAISED_THRESHOLD = "99999999"


def _info_findings(payload: dict, gate: str) -> list[dict]:
    return [f for f in _findings(payload, gate) if f.get("severity") == SEVERITY_INFO]


def test_default_threshold_leaves_the_finance_gate_byte_for_byte_unchanged(
        default_threshold):
    """不设变量 -> 第六道闸 pass，且一条 finding 都不多出来。

    这条钉的是「缺省行为逐字节不变」：留痕那条 info 只许在阈值非缺省时出现。
    多吐一条，存量回归网里按 findings 计数 / 按 gate_results 断言的那一批当场变红，
    而那不是成果，是回归。
    """
    payload = _finance_payload(
        REFUND_OVER,
        [_finance_entry_artifact({"amount_approved": 9000, "rule_refs": ["R-3"]})],
    )
    assert _findings(payload, "finance") == [], "缺省阈值下第六道闸多吐了 finding"
    assert payload["gate_results"]["finance"] == "pass"
    assert payload["verdict"] == "pass"


def test_threshold_set_to_the_default_value_is_not_a_trace_worth_leaving(monkeypatch):
    """显式配成 5000 也不留痕 —— 留痕说的是「判定用的数不是缺省」。

    「有人动过这个变量」不是留痕的理由：取值即缺省时判定与不设变量逐字节相同，
    没有任何东西被改变。按「变量在不在」判会让这条 info 变成一句正确的废话。
    """
    monkeypatch.setenv(FINANCE_THRESHOLD_ENV, str(DEFAULT_FINANCE_THRESHOLD))
    payload = _finance_payload(
        REFUND_OVER,
        [_finance_entry_artifact({"amount_approved": 9000, "rule_refs": ["R-3"]})],
    )
    assert _findings(payload, "finance") == [], "阈值取值即缺省，却留了痕"
    assert payload["gate_results"]["finance"] == "pass"


def test_a_raised_threshold_is_visible_in_gate_results(monkeypatch):
    """阈值被调大 -> 第六道闸从 pass 抬成 noted，正文里两个数都读得到。

    9000 元的退款在缺省阈值 5000 下要交财务凭据，在 99999999 下不用 —— 判定确实
    该 pass（见下一条），但这一轮闸是按一个非缺省的数说的话，必须留得下。
    """
    monkeypatch.setenv(FINANCE_THRESHOLD_ENV, RAISED_THRESHOLD)
    payload = _finance_payload(REFUND_OVER)          # 故意不给 finance_entry

    infos = _info_findings(payload, "finance")
    assert len(infos) == 1, f"阈值留痕应当恰好一条，实得 {infos}"
    message = infos[0]["message"]
    assert "99999999" in message, "finding 没说清本轮按哪个阈值判的"
    assert str(DEFAULT_FINANCE_THRESHOLD) in message, "finding 没说清缺省阈值是多少"
    assert payload["gate_results"]["finance"] == "noted", \
        "阈值被调过，gate_results 里却和「这道闸没话说」长得一模一样"


def test_a_raised_threshold_does_not_turn_the_verdict_into_rework(monkeypatch):
    """留痕不是收严：阈值调大之后该放行的照样放行。

    这条一旦红了，说明「运维调阈值」被改成了「运维改不动阈值」——
    比不留痕更坏，因为它把一个合法运维动作变成了 rework 风暴。
    """
    monkeypatch.setenv(FINANCE_THRESHOLD_ENV, RAISED_THRESHOLD)
    payload = _finance_payload(REFUND_OVER)
    blocking = [f for f in _findings(payload, "finance")
                if f.get("severity") != SEVERITY_INFO]
    assert blocking == [], f"info 之外还多了挡闸的 finding：{blocking}"
    assert payload["verdict"] == "pass", "阈值调大之后判定被改成了 rework"


def test_unparseable_threshold_keeps_only_its_own_warning(monkeypatch, caplog):
    """解析不出数 -> 回落 + 那条 WARNING，不许再叠一层 info。

    解析失败已经有它自己的处理（回落收严 + 告警），而回落之后判定用的就是缺省
    阈值 —— 再吐一条「本轮按非缺省阈值判的」既重复又不实。
    """
    monkeypatch.setenv(FINANCE_THRESHOLD_ENV, "abc")
    with caplog.at_level("WARNING", logger="maos.gate"):
        payload = _finance_payload(
            REFUND_OVER,
            [_finance_entry_artifact({"amount_approved": 9000, "rule_refs": ["R-3"]})],
        )
    assert _findings(payload, "finance") == [], "解析失败那一档被叠了第二层 finding"
    assert payload["gate_results"]["finance"] == "pass"
    assert any(FINANCE_THRESHOLD_ENV in r.getMessage() for r in caplog.records), \
        "回落那条 WARNING 不见了 —— 解析失败反而比以前更安静"


def test_a_raised_threshold_is_silent_on_non_refund_tasks(monkeypatch):
    """非退款任务不留痕 —— 场景 1-5 的每一个任务不该跟着变 noted。

    这道闸对它们本来就不看阈值，挂一条「阈值被调过」是与本轮判定无关的噪声。
    """
    monkeypatch.setenv(FINANCE_THRESHOLD_ENV, RAISED_THRESHOLD)
    payload = _finance_payload({"workdir": "/tmp/probe", "amount_claimed": 999999})
    assert _findings(payload, "finance") == [], "非退款任务被挂上了阈值留痕"
    assert payload["gate_results"]["finance"] == "pass"


def test_threshold_notice_fires_once_even_though_two_call_sites_read_it(monkeypatch):
    """两个调用点共用一条留痕：漏排财务复核的形状下也只吐一条。

    `_finance_threshold()` 有两个调用点（`_gate_finance_task` / `_gate_finance_plan`），
    留痕挂在它们共同的入口上：两处都覆盖到，而同一次 _review 只出现一条。
    同一条 info 出现两遍，读结果的人会以为有两个问题。
    """
    monkeypatch.setenv(FINANCE_THRESHOLD_ENV, RAISED_THRESHOLD)
    # 顶层没有 amount_claimed：这正是 plan 级判据开口的那一面（金额没进闸的视野）。
    payload = _finance_payload(
        {"biz_type": "refund", "case_seed": {"amount_claimed": 9000}})
    assert len(_info_findings(payload, "finance")) == 1, \
        "两个调用点各留了一条痕，读的人会以为有两个问题"
