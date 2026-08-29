"""沙箱降级必须看得见 —— 探测别误判，降级别静默。

本文件守的是**一类假绿**：报告全绿、判据全 PASS，而「容器隔离」这句话当场不成立。

它的机理很短：``_docker_ready()`` 误判一次，``sandbox_pytest_run`` 就退回裸
subprocess，``--network none / --read-only / --user 1000:1000`` 三项一起失效。
靶场里那条 ``test_no_network`` 探针于是从 passed 变成 **skipped**，而 skipped
不进 passed / failed / errors 任何一个计数 —— 报告上唯一的差别是「4 过」变成
「3 过」，没有任何一个字段说得出为什么。原先这件事只有一条 ``log.warning``，
而日志不进证据。

所以本文件按两段来验：

1. **探测别误判**（``_docker_ready``）：镜像引用必须带显式 tag。裸仓库名靠 daemon
   自己补 ``:latest``，这一步在 Docker 29.6.1 上实测会连三次报 ``No such image``，
   而同一时刻 ``docker image ls`` 列得出、``docker run`` 也跑得通。
2. **降级别静默**（``sandbox_pytest_run`` -> ``trace.py`` -> ``verify.py``）：执行路径
   必须跟报告一起落盘、进 trace.json、在核验器上印出来。

探测那几条一律 monkeypatch ``subprocess.run``：真去问 docker 的话，这些断言在
装了 / 没装 Docker 的机器上结论相反，等于没验。降级那几条走真靶场真 pytest。
"""

from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys
import types

import pytest

from maos.artifacts import KIND_TEST_REPORT, validate_artifact
from maos.core.store import SqliteStore
from maos.obs.trace import MODE_SUBPROCESS, MODE_UNRECORDED, export_trace
from maos.tools.sandbox import (
    IMAGE,
    IMAGE_REF,
    MODE_CONTAINER,
    MODE_NOT_RUN,
    _docker_ready,
    prepare_sandbox_workdir,
    sandbox_pytest_run,
)

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _load_verify() -> types.ModuleType:
    """``scripts/verify.py`` 不是包，只能按路径加载（idiom 同 test_trace_evidence）。"""
    spec = importlib.util.spec_from_file_location("_x4_verify", ROOT / "scripts" / "verify.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_x4_verify"] = mod
    spec.loader.exec_module(mod)
    return mod


verify = _load_verify()


def _proc(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


@pytest.fixture(autouse=True)
def _no_force_flag(monkeypatch):
    """探测用例要走到真正的探测分支，所以先摘掉 CI 那个强制降级开关。"""
    monkeypatch.delenv("MAOS_SANDBOX_FORCE_SUBPROCESS", raising=False)


# ---------------------------------------------------------------------------
# 第一段：探测别误判
# ---------------------------------------------------------------------------
def test_probe_asks_for_an_explicit_tag(monkeypatch):
    """探测的镜像引用必须带 tag。

    这一条盯的是真实事故：``docker image inspect maos-sandbox`` 在 Docker 29.6.1 上
    连三次 exit=1 报 ``No such image: maos-sandbox``，而 ``maos-sandbox:latest``
    三次全 exit=0，同一台机器同一时刻。省掉 tag 换来的是沙箱静默降级。
    """
    seen: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        seen.append(list(cmd))
        return _proc(0)

    monkeypatch.setattr("maos.tools.sandbox.shutil.which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert _docker_ready() == (True, "")
    assert seen, "根本没去探测"
    assert IMAGE_REF in seen[0], f"探测没带 tag: {seen[0]}"
    assert IMAGE_REF.endswith(":latest"), "IMAGE_REF 必须是带 tag 的完整引用"


def test_probe_mismatch_is_named_instead_of_swallowed(monkeypatch):
    """inspect 说没有、image ls 说有 —— 这个矛盾必须写进原因，不许只说「不可用」。

    不点名的话，现场会照着提示去重跑一次 ``docker build``，而那是白跑的：
    镜像本来就在，坏的是探测。
    """
    def fake_run(cmd, **kwargs):
        if "inspect" in cmd:
            return _proc(1, stderr=f"Error response from daemon: No such image: {IMAGE}")
        return _proc(0, stdout="3bef9ca53c10\n")

    monkeypatch.setattr("maos.tools.sandbox.shutil.which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(subprocess, "run", fake_run)

    usable, why = _docker_ready()
    assert usable is False, "探测不一致时保守降级，不赌"
    assert "探测不一致" in why
    assert "3bef9ca53c10" in why, "得把 image ls 给出的 id 亮出来，否则没法判断谁在骗人"
    assert "docker build" not in why, "镜像明明在，不许把人往重建上引"


def test_probe_missing_image_still_points_at_the_build_command(monkeypatch):
    """镜像是真没有时，原因要说得出下一步做什么 —— 别把两种情况说成一句话。"""
    def fake_run(cmd, **kwargs):
        if "inspect" in cmd:
            return _proc(1, stderr=f"Error response from daemon: No such image: {IMAGE}")
        return _proc(0, stdout="")          # image ls 也查不到

    monkeypatch.setattr("maos.tools.sandbox.shutil.which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(subprocess, "run", fake_run)

    usable, why = _docker_ready()
    assert usable is False
    assert "docker build" in why
    assert "探测不一致" not in why


# ---------------------------------------------------------------------------
# 第二段：降级别静默
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def degraded_report(tmp_path_factory) -> dict:
    """真靶场、真 pytest、强制降级跑一次。模块级只跑一次。"""
    path = prepare_sandbox_workdir(str(tmp_path_factory.mktemp("x4") / "repo"))
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("MAOS_SANDBOX_FORCE_SUBPROCESS", "1")
        return sandbox_pytest_run(path)


def test_degraded_report_says_so_in_its_own_summary(degraded_report):
    """降级原因要在报告自己身上，不是只在日志里。

    ``summary`` 是随 artifact 一起落库、一起进证据的字段；``log.warning`` 不是。
    """
    assert degraded_report["sandbox_mode"] == MODE_SUBPROCESS
    assert degraded_report["degraded_reason"]
    assert degraded_report["tool_error"] is None, "降级是跑成了，不是工具炸了"

    summary = degraded_report["summary"]
    assert "沙箱降级" in summary
    # 三个 flag 逐个点名 —— 写「隔离未生效」这种概括，现场判不出具体丢了什么。
    for flag in ("--network none", "--read-only", "--user 1000:1000"):
        assert flag in summary, f"summary 没点名 {flag}: {summary}"
    assert degraded_report["degraded_reason"] in summary


def test_degraded_run_really_loses_the_network_probe(degraded_report):
    """降级不是「换个地方跑」，是真的少验了东西 —— 这条是上面那句话的实际内容。

    ``test_no_network`` 在容器里 passed、降级路径 skipped，而 skipped 不进
    passed / failed / errors 任何一个计数。所以报告上只有「4 过」变「3 过」，
    没有任何字段说得出少的是隔离探针 —— 这正是要靠 summary 补上的那句话。
    """
    probe = [c for c in degraded_report["cases"] if c["id"].endswith("test_no_network")]
    assert len(probe) == 1, [c["id"] for c in degraded_report["cases"]]
    assert probe[0]["status"] == "skipped"
    assert probe[0]["status"] not in ("passed", "failed")


def test_new_fields_do_not_break_the_frozen_artifact_shape(degraded_report):
    """三个新键是 C-7 六键之外的增量，直接落 test_report artifact 必须仍然合法。"""
    assert validate_artifact(KIND_TEST_REPORT, degraded_report) == []


def test_tool_error_path_also_records_where_it_tried_to_run(tmp_path):
    """工具炸了的报告同样要说清它当时在哪条路径上 —— 否则查不出是不是降级害的。"""
    report = sandbox_pytest_run(str(tmp_path / "does-not-exist"))
    assert report["tool_error"]
    assert report["sandbox_mode"] == MODE_NOT_RUN, "压根没跑，不许说成 container"
    assert report["summary"], "Gate 的 evidence 闸要求每份产物都有变更说明"


# ---------------------------------------------------------------------------
# 第三段：执行路径要进 trace.json
# ---------------------------------------------------------------------------
PLAN_ID, TASK_ID = "plan_x4", "task_x4"

_BASE_REPORT = {"passed": 1, "failed": 0, "errors": 0, "cases": [],
                "duration": 0.1, "tool_error": None, "summary": "回归"}


def _build_db(path) -> SqliteStore:
    """三份 test_report：容器跑的、降级跑的、没记执行路径的。"""
    store = SqliteStore(str(path))
    store.init_schema()
    store.insert_plan({"plan_id": PLAN_ID, "trace_id": "tr_x4",
                       "goal": "沙箱模式 fixture", "state": "DONE"})
    store.insert_task({"task_id": TASK_ID, "plan_id": PLAN_ID, "trace_id": "tr_x4",
                       "role": "testing", "title": "回归", "state": "DONE",
                       "attempt": 1, "risk_level": "L", "effect_risk": "L"})
    for aid, extra in (
            ("art_container", {"sandbox_mode": MODE_CONTAINER, "degraded_reason": None}),
            ("art_degraded", {"sandbox_mode": MODE_SUBPROCESS,
                              "degraded_reason": "找不到 docker 命令"}),
            ("art_silent", {}),                 # 旧路径：字段被上层丢掉了
    ):
        store.insert_artifact({"artifact_id": aid, "task_id": TASK_ID, "plan_id": PLAN_ID,
                               "kind": KIND_TEST_REPORT, "version": 1,
                               "content": {**_BASE_REPORT, **extra}})
    return store


def test_trace_counts_degraded_and_unrecorded_separately(tmp_path):
    """两种形态分开计数。合并成一个数就把「更糟的那种」藏进「已知的那种」里了。

    * ``degraded`` = 知道降级了，原因也在。
    * ``unrecorded`` = 连有没有降级都查不到 —— 这一种更糟，因为它连问题都提不出来。
    """
    store = _build_db(tmp_path / "x4.db")
    doc = export_trace(store, PLAN_ID)

    assert doc["summary"]["degraded_sandbox_reports"] == 1
    assert doc["summary"]["unrecorded_sandbox_reports"] == 1

    modes = {s["attributes"]["maos.artifact.id"]:
             s["attributes"]["maos.artifact.sandbox.mode"]
             for s in doc["spans"] if s["kind"] == "artifact"}
    assert modes == {"art_container": MODE_CONTAINER,
                     "art_degraded": MODE_SUBPROCESS,
                     "art_silent": MODE_UNRECORDED}

    degraded = next(s for s in doc["spans"]
                    if s["attributes"].get("maos.artifact.id") == "art_degraded")
    assert degraded["attributes"]["maos.artifact.sandbox.degraded_reason"] == "找不到 docker 命令"
    ok = next(s for s in doc["spans"]
              if s["attributes"].get("maos.artifact.id") == "art_container")
    assert ok["attributes"]["maos.artifact.sandbox.note"] is None, "容器路径不该有告警语"


def test_non_test_report_artifacts_get_no_sandbox_fields(tmp_path):
    """别给 patch_set 之类安一个它本来就没有的字段（铁律 8：只读，不推断）。"""
    store = _build_db(tmp_path / "x4b.db")
    store.insert_artifact({"artifact_id": "art_patch", "task_id": TASK_ID,
                           "plan_id": PLAN_ID, "kind": "patch_set", "version": 1,
                           "content": {"files": [], "summary": "s"}})
    doc = export_trace(store, PLAN_ID)
    patch = next(s for s in doc["spans"]
                 if s["attributes"].get("maos.artifact.id") == "art_patch")
    assert patch["attributes"]["maos.artifact.sandbox.mode"] is None
    assert doc["summary"]["unrecorded_sandbox_reports"] == 1, "非报告不该被算进这个数"


# ---------------------------------------------------------------------------
# 第四段：核验器的措辞（改错措辞和判错一样坏 —— 评委只读得到这句话）
# ---------------------------------------------------------------------------
def _case(**over) -> object:
    base = {"name": "scenario-x", "directory": "", "db_path": "", "conn": None,
            "tables": set(), "trace": {}, "result": {}}
    base.update(over)
    return verify.Case(**base)


def test_stray_warn_separates_pre_plan_calls_from_dangling_ones():
    """``plan_id`` 空串 = 建 Plan 之前发生的调用，不是事件丢了。

    规划期检索发生在 ``create_plan`` 之前，那一刻还没有 plan_id 可写。原措辞
    「指不到任何 plan」读起来像事件丢了，会让人去查一个不存在的故障。
    """
    chk = verify.Check("t", "t")
    verify._warn_stray_events(chk, _case(trace={"stray_events": [
        {"event_type": "KbRetrieved", "plan_id": ""},
        {"event_type": "SkillInvoked", "plan_id": ""},
    ]}))
    assert len(chk.notes) == 1, "一个 case 仍然只出一条 warn"
    note = chk.notes[0]
    assert "建 Plan 之前" in note
    assert "不是事件丢了" in note
    assert "这一种要查" not in note, "没有悬空事件时不许暗示有"


def test_stray_warn_still_singles_out_a_genuinely_dangling_event():
    """``plan_id`` 非空却指不到 plan，那是真的该查 —— 不许被上面那条解释盖住。

    这一条是防「措辞改软了顺手把判据也放宽了」：多认一种形态，不是少认。
    """
    chk = verify.Check("t", "t")
    verify._warn_stray_events(chk, _case(trace={"stray_events": [
        {"event_type": "KbRetrieved", "plan_id": ""},
        {"event_type": "StateTransition", "plan_id": "plan_ghost"},
    ]}))
    note = chk.notes[0]
    assert "建 Plan 之前" in note
    assert "这一种要查" in note
    assert "1 条 plan_id 非空却指不到任何 plan" in note


def test_unaudited_warn_no_longer_calls_a_real_report_fake():
    """第 6 项不许再断言「场景预置件，非实跑产出」—— 那句话现在是错的。

    场景 1/2 的报告由 ``flows/common.py::patch_verifier`` 真跑沙箱产出，只是入库时
    绕开 ``on_task_result``。把「入库路径证不了」印成「内容是假的」，会让评委把
    已经兑现的外部判据重新读成脚手架。
    """
    chk = verify.check_business_outcome([_case(
        conn=_FakeConn([{"plan_id": PLAN_ID, "state": "DONE"}]),
        result={"plans": [{"plan_id": PLAN_ID, "state": "DONE", "business_outcome": {
            "status": "succeeded",
            "external_evidence": [{"kind": "test_report"}],
            "unaudited_evidence_count": 1,
        }}]},
    )])
    note = next(n for n in chk.notes if "无来源事件" in n)
    assert "非实跑产出" not in note, "这是核验器证不了的断言"
    assert "预置件，非实跑" not in note
    assert "不是内容真伪" in note
    # 前半句「来源未审计」原样保留：它说的是入库路径，本来就没错，而且
    # test_trace_evidence.py 的第 6 项正锁着这个词。错的只是括号里那句论断。
    assert "来源未审计" in note
    assert "patch_verifier" in note and "seed_scripted_report" in note, "两类都要点名"
    assert chk.status == verify.PASS, "措辞变了，判定不许变（warn 不判负）"


def test_sandbox_warn_quotes_the_degradation_reason():
    """降级 warn 要把原因原文带出来 —— 只说「降级了」，现场还是查不下去。"""
    chk = verify.Check("t", "t")
    verify._warn_sandbox_path(chk, _case(trace={
        "summary": {"degraded_sandbox_reports": 1, "unrecorded_sandbox_reports": 0},
        "traces": [{"spans": [{"attributes": {
            "maos.artifact.sandbox.mode": MODE_SUBPROCESS,
            "maos.artifact.sandbox.degraded_reason": "找不到 docker 命令",
        }}]}],
    }))
    assert len(chk.notes) == 1
    assert "找不到 docker 命令" in chk.notes[0]
    assert "--network none" in chk.notes[0]


def test_sandbox_warn_reports_unrecorded_as_its_own_problem():
    """「查不出在哪儿跑的」要单独报，不许并进降级那条里含糊过去。"""
    chk = verify.Check("t", "t")
    verify._warn_sandbox_path(chk, _case(trace={
        "summary": {"degraded_sandbox_reports": 0, "unrecorded_sandbox_reports": 2},
        "traces": [],
    }))
    assert len(chk.notes) == 1
    assert "执行路径不可审计" in chk.notes[0]
    assert "task-X4" in chk.notes[0], "得指得到账本上那一条，否则没人会去修"


def test_clean_case_stays_quiet():
    """全在容器里跑的场景一条 warn 都不许出 —— 否则这几条噪音很快就没人看了。"""
    chk = verify.Check("t", "t")
    verify._warn_sandbox_path(chk, _case(trace={
        "summary": {"degraded_sandbox_reports": 0, "unrecorded_sandbox_reports": 0},
        "traces": [],
    }))
    verify._warn_stray_events(chk, _case(trace={"stray_events": []}))
    assert chk.notes == []


class _FakeConn:
    """只需要 ``execute`` 返回 plan 行 —— 第 6 项对 conn 的用法就这一处。"""

    def __init__(self, rows: list[dict]):
        self._rows = rows

    def execute(self, sql, *args):
        return list(self._rows)
