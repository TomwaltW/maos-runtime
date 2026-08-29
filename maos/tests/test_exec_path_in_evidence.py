"""执行路径必须跟着报告走进证据 —— 装配层不许把它丢了。

X-4 让 ``sandbox_pytest_run`` 返回了 ``sandbox_mode`` / ``degraded_reason``，
可这两个字段在装配层被丢掉：``make_test_report`` 按固定具名参数收，收不下的
就没了。后果不是「少两个键」，而是**「容器隔离」这句话只在日志里成立**：
真实证据束里每一份 test_report 都判 ``unrecorded``，谁也说不出这一次是在容器里
跑的还是降级跑的，而 X-4 做的「降级可见」那条分支在真实证据上一次都触发不了。

本文件守三件事：

1. **缺省逐字节不变**。C-7 是冻结形状，加参数不许把默认产出改样子。
2. **传了才加键**。``{"sandbox_mode": None}`` 会让「没人记过」和「记过、值是 None」
   在证据里长成一个样，而下游 ``obs/trace.py`` 正是靠键在不在区分 ``unrecorded``。
3. **三条入库路径一条都不许漏**：``verify_patch_in_sandbox`` 的两条出口
   （补丁没落进沙箱 / pytest 跑完）、``TestingAgent`` 的两条出口（真报告 /
   软兜底）、以及 ``seed_scripted_report`` 的预置件。漏一条，核验器第 4 项
   就会重新印出「执行路径不可审计」。

最后一段直接问核验器本人：``verify._warn_sandbox_path`` 对着这几条路径产出的
trace 一句话都印不出来。**warn 必须是被这些改动消掉的，不是被核验器忽略掉的**
—— 所以这里连一个字都不改 ``scripts/verify.py``，只喂真产出给它判。
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import shutil
import sys
import types

import pytest

from maos.agents.testing import (
    SCRIPTED_REASON,
    make_test_report,
    seed_scripted_report,
)
from maos.agents.testing import TestingAgent as _TestingAgent
from maos.agents.base import TaskContext
from maos.artifacts import KIND_TEST_REPORT, validate_artifact
from maos.core.store import SqliteStore
from maos.flows import common as flows_common
from maos.flows.common import GOOD_PATCH, verify_patch_in_sandbox
from maos.model.client import ScriptedModelClient
from maos.obs.trace import MODE_UNRECORDED, export_trace
from maos.skills.contract import SkillResult
from maos.tools.sandbox import (
    MODE_CONTAINER,
    MODE_NOT_RUN,
    MODE_SUBPROCESS,
    prepare_sandbox_workdir,
)

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _load_verify() -> types.ModuleType:
    """``scripts/verify.py`` 不是包，只能按路径加载（idiom 同 test_trace_evidence）。"""
    spec = importlib.util.spec_from_file_location("_y1_verify", ROOT / "scripts" / "verify.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_y1_verify"] = mod
    spec.loader.exec_module(mod)
    return mod


verify = _load_verify()


# ---------------------------------------------------------------------------
# 第一段：缺省逐字节不变（C-7 冻结形状）
# ---------------------------------------------------------------------------
#: 改动前 ``make_test_report()`` 的产出，逐键逐值抄在这里。
#: 抄成字面量而不是拿函数自己算，是因为这条断言要挡的正是「函数变了」。
_FROZEN_DEFAULT = {
    "passed": 0,
    "failed": 0,
    "errors": 0,
    "cases": [],
    "duration": 0.0,
    "tool_error": None,
    "summary": "测试报告：0 过 / 0 挂 / 0 错",
}


def test_default_report_is_byte_identical_to_the_frozen_shape():
    """不传新参数时，产出必须与加参数之前**完全一致**：键集、键序、取值。

    加两个可选参数最省事的写法是无条件塞进 dict，那会让 521 条存量测试里
    每一份「手搓一份期望报告再比对」的断言集体变红 —— 更要紧的是，C-7 是
    冻结形状，多两个恒为 None 的键就是形状变了。所以这里连**键序**都锁：
    json 落盘按插入序走，键序一变，evidence/ 下每份报告的 sha256 全部改写。
    """
    report = make_test_report()
    assert report == _FROZEN_DEFAULT, f"缺省产出变了：{report}"
    assert list(report) == list(_FROZEN_DEFAULT), \
        f"键序变了，evidence 的 hash-integrity 会整束改写：{list(report)}"
    assert validate_artifact(KIND_TEST_REPORT, report) == []


def test_default_report_keeps_target_fields_last():
    """``target_*`` 仍然排在最后 —— 新键插在 summary 与它们之间，不许插到尾巴后面。"""
    report = make_test_report(target_task_id="t-code", target_attempt=2)
    assert list(report) == [*_FROZEN_DEFAULT, "target_task_id", "target_attempt"]


# ---------------------------------------------------------------------------
# 第二段：传了才加键
# ---------------------------------------------------------------------------
def test_sandbox_mode_none_adds_no_key_at_all():
    """``None`` 不许落成 ``{"sandbox_mode": None}``。

    这不是洁癖：``obs/trace.py`` 读 ``content.get("sandbox_mode") or MODE_UNRECORDED``，
    塞一个 None 进去照样判 unrecorded，看起来「没差」—— 差在**证据本身**。
    键在而值为 None 读作「记过，只是没记住」，键不在读作「压根没人记」。
    第二种才是真相，也才是该被核验器点名的那一种。
    """
    report = make_test_report(sandbox_mode=None, degraded_reason="不该出现")
    assert "sandbox_mode" not in report
    assert "degraded_reason" not in report
    assert report == _FROZEN_DEFAULT, "只给了 reason 没给 mode，产出就该原样不动"


def test_container_mode_carries_both_keys_with_reason_none():
    """给了 mode 就两个键同时在。容器路径的 reason 是 None ——

    「记过，这一次没什么可说的」，与「没人记过」不是一回事。
    """
    report = make_test_report(passed=5, sandbox_mode=MODE_CONTAINER)
    assert report["sandbox_mode"] == MODE_CONTAINER
    assert report["degraded_reason"] is None
    assert "degraded_reason" in report, "mode 与 reason 同进同退"
    assert validate_artifact(KIND_TEST_REPORT, report) == [], "多两个键仍要合 C-7"


def test_empty_string_mode_is_not_folded_into_missing():
    """空串是「上游记错了」，不是「上游没记」—— 不许在装配层被抹平。

    写成 ``raw.get("sandbox_mode") or None`` 就会把空串折成 None，于是一个
    真实的上游 bug 变成一条「不可审计」的告警，查的人被指去查错的地方。
    """
    report = make_test_report(sandbox_mode="")
    assert report["sandbox_mode"] == "", f"空串被抹平了：{report}"


# ---------------------------------------------------------------------------
# 第三段：verify_patch_in_sandbox 的两条出口
# ---------------------------------------------------------------------------
def _good_patch_set() -> dict:
    """``GOOD_PATCH`` 是 Coding Agent 那一侧的 JSON 串；入沙箱的是它解出来的 patch_set。

    真实链路上这一步由 ``patch_verifier`` 从 artifact 的 content 里取（那里已经是
    dict），这里补上同一次解析 —— 拿串去打补丁的话，每条断言都会撞在
    ``stage=validate`` 上，把「执行路径搬没搬过来」验成了「补丁形状对不对」。
    """
    return json.loads(GOOD_PATCH)


@pytest.fixture
def workdir(tmp_path):
    wd = prepare_sandbox_workdir(str(tmp_path / "repo"))
    yield wd
    shutil.rmtree(wd, ignore_errors=True)


def test_patch_that_never_landed_reports_not_run(workdir):
    """补丁没落进沙箱 —— pytest 压根没被调用过，那就照实说 ``not-run``。

    这一条是派单点名的那处：**工具没跑成时也有执行路径可报**。
    「降级之后裸 subprocess 跑挂了」与「容器里跑挂了」是两件事，而
    「根本没跑到 pytest」是第三件。三件事在证据里必须分得开。
    """
    report = verify_patch_in_sandbox(
        {"summary": "打不上", "files": [{"path": "auth/session.py", "diff": "不是 diff\n"}]},
        workdir)

    assert report["tool_error"], "补丁没落进沙箱就该带 tool_error"
    assert report["sandbox_mode"] == MODE_NOT_RUN
    assert "pytest 未被调用" in report["degraded_reason"]
    assert report["failed"] == 0, "没跑成不许伪造失败数（tool_error 与 failed 分开报）"


def test_degraded_run_carries_mode_and_reason_into_the_report(workdir, monkeypatch):
    """真跑一次降级：执行路径与降级原因都要落在报告上。

    走真沙箱真 pytest（强制降级，无 Docker 的机器也跑得动）。这一条红了，
    说明「降级不再静默」这句话又退回只在日志里成立。
    """
    monkeypatch.setenv("MAOS_SANDBOX_FORCE_SUBPROCESS", "1")
    report = verify_patch_in_sandbox(_good_patch_set(), workdir)

    assert report["tool_error"] is None, report["tool_error"]
    assert report["sandbox_mode"] == MODE_SUBPROCESS
    assert "MAOS_SANDBOX_FORCE_SUBPROCESS" in report["degraded_reason"]
    assert "[沙箱降级]" in report["summary"], \
        f"降级那句话没跟着 summary 进报告：{report['summary']!r}"
    assert validate_artifact(KIND_TEST_REPORT, report) == []


def test_container_run_summary_says_container_isolation(workdir, monkeypatch):
    """容器路径的 summary 口径与 ``test.verify`` 那条路径对齐。

    对齐之前 ``patch_verifier`` 这条路径不传 summary，报告上印的是
    ``make_test_report`` 的默认文案「测试报告：N 过 / M 挂」；而走 skill 的那份
    印的是「沙箱回归（容器隔离）：…」。同一件事两种说法，读证据的人会以为
    是两种执行路径。这里 monkeypatch 沙箱返回而不去问真 Docker：真去问的话，
    这条断言在装了 / 没装 Docker 的机器上结论相反，等于没验。
    """
    monkeypatch.setattr(flows_common, "sandbox_pytest_run", lambda _wd: {
        "passed": 5, "failed": 0, "errors": 0, "cases": [], "duration": 0.3,
        "tool_error": None, "summary": "沙箱回归（容器隔离）：5 过 / 0 挂 / 0 错",
        "sandbox_mode": MODE_CONTAINER, "degraded_reason": None,
    })
    report = verify_patch_in_sandbox(_good_patch_set(), workdir)

    assert report["summary"] == "沙箱回归（容器隔离）：5 过 / 0 挂 / 0 错"
    assert report["sandbox_mode"] == MODE_CONTAINER
    assert report["degraded_reason"] is None


# ---------------------------------------------------------------------------
# 第四段：TestingAgent 的两条出口
# ---------------------------------------------------------------------------
def _ctx(**over) -> TaskContext:
    base = dict(plan_id="p-y1", task_id="t-y1", trace_id="tr-y1", attempt=1,
                inputs={}, acceptance=[], risk_level="M")
    base.update(over)
    return TaskContext(**base)


def test_testing_agent_passes_the_execution_path_through(tmp_path, monkeypatch):
    """走 ``test.verify`` 的真报告：``_normalize`` 不许把执行路径洗掉。"""
    monkeypatch.setenv("MAOS_SANDBOX_FORCE_SUBPROCESS", "1")
    wd = prepare_sandbox_workdir(str(tmp_path / "repo"))

    content = _TestingAgent(ScriptedModelClient({})).run(
        _ctx(inputs={"workdir": wd})).artifacts[0]["content"]

    assert content["sandbox_mode"] == MODE_SUBPROCESS
    assert content["degraded_reason"], "降级了却没记原因，现场查不下去"


def test_tool_error_report_still_says_where_it_did_not_run():
    """工具跑了一段才炸的那一种：``tool_error`` 与执行路径要同时在。

    workdir 不存在，``sandbox.pytest_run`` 进得去、也照实报了 ``not-run``。
    派单点名的正是这条：**没跑成也有执行路径可报** —— 「降级之后裸 subprocess
    跑挂了」「容器里跑挂了」「压根没跑到 pytest」是三件事，报告里分不开，
    现场就只能靠猜。
    """
    content = _TestingAgent(ScriptedModelClient({})).run(
        _ctx(inputs={"workdir": "/tmp/definitely-not-a-workdir-y1"})).artifacts[0]["content"]

    assert content["tool_error"], "没跑成必须带 tool_error"
    assert content["sandbox_mode"] == MODE_NOT_RUN, \
        f"工具报了执行路径却在装配层丢了：{content.get('sandbox_mode')!r}"
    assert content["failed"] == 0, "没跑成不许伪造失败数"


class _NoSuchSkill:
    """``test.verify`` 未注册时 invoker 的软兜底返回（A-5）。"""

    def invoke(self, name, _payload, extras=None):        # noqa: D102, ANN001
        return SkillResult(status="failed", output=None,
                           error=f"skill_not_found:{name}")


def test_unregistered_skill_leaves_no_execution_path_to_claim():
    """skill 压根没注册那一种**不许**编一个 mode 出来。

    没人跑过，就没人观察过 —— 键不在才是实话，这份报告本来就该被核验器
    点名为「执行路径不可审计」。补一个 ``not-run`` 上去会让「工具确实跑了
    一段」和「压根没这个工具」长成一个样，那是把铁律 8 反过来用：MAOS 只
    持有观察与推断，没观察到的不许写成观察到了。
    """
    agent = _TestingAgent(ScriptedModelClient({}))
    agent.skills = _NoSuchSkill()
    content = agent.run(_ctx(inputs={"workdir": "/tmp/x"})).artifacts[0]["content"]

    assert "skill_not_found" in content["tool_error"]
    assert "sandbox_mode" not in content, \
        f"没人跑过却报出了执行路径：{content.get('sandbox_mode')!r}"


# ---------------------------------------------------------------------------
# 第五段：预置件也要有执行路径
# ---------------------------------------------------------------------------
PLAN_ID, TASK_ID = "plan_y1", "task_y1"


def _store(path) -> SqliteStore:
    store = SqliteStore(str(path))
    store.init_schema()
    store.insert_plan({"plan_id": PLAN_ID, "trace_id": "tr_y1",
                       "goal": "执行路径 fixture", "state": "DONE"})
    store.insert_task({"task_id": TASK_ID, "plan_id": PLAN_ID, "trace_id": "tr_y1",
                       "role": "testing", "title": "回归", "state": "DONE",
                       "attempt": 1, "risk_level": "L", "effect_risk": "L"})
    return store


def test_scripted_report_declares_it_never_ran(tmp_path):
    """场景 3/5 的预置件确实一次沙箱都没跑过 —— 就照实写 ``not-run``。

    不写的话它在证据里与「跑过、但上层把字段丢了」判成同一种（都是
    ``unrecorded``），而那两件事天差地别：一个是脚手架，一个是 bug。
    补完之后 ``passed=1`` 配 ``sandbox_mode=not-run`` 这个组合本身就是标记 ——
    **这些计数背后没有一次真实执行**，比一句「不可审计」说得更准。
    """
    store = _store(tmp_path / "seed.db")
    seed_scripted_report(store, plan_id=PLAN_ID, task_id=TASK_ID, attempt=1,
                         report=make_test_report(passed=1, summary="沙箱回归：1 过 0 挂 0 错"))

    content = store.list_artifacts(TASK_ID)[0]["content"]
    assert content["sandbox_mode"] == MODE_NOT_RUN
    assert content["degraded_reason"] == SCRIPTED_REASON
    assert "seed_scripted_report" in content["degraded_reason"], \
        "读到这行字的人手上只有 trace.json，得说清是谁预置的"
    assert content["passed"] == 1, "补执行路径不许动计数"


def test_scripted_report_does_not_overwrite_a_recorded_mode(tmp_path):
    """调用方已经写了执行路径就不覆盖：这里补的是缺省，不是权威。"""
    store = _store(tmp_path / "seed2.db")
    seed_scripted_report(store, plan_id=PLAN_ID, task_id=TASK_ID, attempt=1,
                         report=make_test_report(passed=1, sandbox_mode=MODE_CONTAINER))

    content = store.list_artifacts(TASK_ID)[0]["content"]
    assert content["sandbox_mode"] == MODE_CONTAINER
    assert content["degraded_reason"] is None


def test_seeding_does_not_mutate_the_callers_report(tmp_path):
    """场景把 ``PASS_REPORT`` 建在模块级、两轮 attempt 共用一份 —— 不许就地改它。"""
    store = _store(tmp_path / "seed3.db")
    shared = make_test_report(passed=1)
    seed_scripted_report(store, plan_id=PLAN_ID, task_id=TASK_ID, attempt=1, report=shared)

    assert "sandbox_mode" not in shared, f"调用方那份被就地改了：{shared}"


# ---------------------------------------------------------------------------
# 第六段：直接问核验器 —— warn 是被改动消掉的，不是被核验器忽略掉的
# ---------------------------------------------------------------------------
def _case(trace: dict) -> object:
    base = {"name": "scenario-y1", "directory": "", "db_path": "", "conn": None,
            "tables": set(), "trace": trace, "result": {}}
    return verify.Case(**base)


def test_verifier_prints_nothing_once_every_path_records_its_mode(tmp_path):
    """三条入库路径各来一份报告，核验器第 4 项一句话都印不出来。

    这是本轨的判据本身。它故意**不碰** ``scripts/verify.py``：喂给它的是三条
    生产路径的真产出，印不出 warn 才算「执行路径可审计」这件事真的成立了。
    改核验器让 warn 消失是造假，这条测试正是那条红线的机器版本。
    """
    store = _store(tmp_path / "all.db")
    # ① 预置件；② patch_verifier 的补丁没落进沙箱那一条；③ 容器跑出来的真报告。
    seed_scripted_report(store, plan_id=PLAN_ID, task_id=TASK_ID, attempt=1,
                         report=make_test_report(passed=1))
    for version, report in (
            (1, make_test_report(tool_error="补丁没能落进沙箱", sandbox_mode=MODE_NOT_RUN,
                                 degraded_reason="补丁没落进沙箱，pytest 未被调用")),
            (1, make_test_report(passed=5, sandbox_mode=MODE_CONTAINER)),
    ):
        store.insert_artifact({"artifact_id": f"art_y1_{id(report)}", "task_id": TASK_ID,
                               "plan_id": PLAN_ID, "kind": KIND_TEST_REPORT,
                               "version": version, "content": report})

    doc = export_trace(store, PLAN_ID)
    assert doc["summary"]["unrecorded_sandbox_reports"] == 0, \
        "还有报告说不出自己在哪儿跑的"
    assert doc["summary"]["degraded_sandbox_reports"] == 0

    chk = verify.Check("t", "t")
    verify._warn_sandbox_path(chk, _case({"summary": doc["summary"],
                                          "traces": [{"spans": doc["spans"]}]}))
    assert chk.notes == [], f"核验器仍在告警：{chk.notes}"


def test_verifier_still_catches_a_report_that_forgot_to_record(tmp_path):
    """反向锁：漏记一份，第 4 项必须立刻重新印出来。

    没有这条，上一条测试可以靠「让核验器永远闭嘴」通过。**多认一种形态，
    不是少认** —— 判据必须还认得出真正的缺口。
    """
    store = _store(tmp_path / "gap.db")
    store.insert_artifact({"artifact_id": "art_y1_silent", "task_id": TASK_ID,
                           "plan_id": PLAN_ID, "kind": KIND_TEST_REPORT,
                           "version": 1, "content": make_test_report(passed=1)})

    doc = export_trace(store, PLAN_ID)
    assert doc["summary"]["unrecorded_sandbox_reports"] == 1
    modes = {s["attributes"]["maos.artifact.sandbox.mode"]
             for s in doc["spans"] if s["kind"] == "artifact"}
    assert modes == {MODE_UNRECORDED}

    chk = verify.Check("t", "t")
    verify._warn_sandbox_path(chk, _case({"summary": doc["summary"],
                                          "traces": [{"spans": doc["spans"]}]}))
    assert len(chk.notes) == 1
    assert "执行路径不可审计" in chk.notes[0]
