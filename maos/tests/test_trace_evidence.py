"""Trace 导出 / 证据束生成 / verify.py 七项核验的行为契约。

三条贯穿全篇的取向：

1. **负例比正例重要。** 「生成器在上游失败时抛错且不留下半份目录」、「篡改一个字符
   verify 就转非零」这两条，比任何一条 happy path 都值钱 —— 它们才是「可核验」
   这句话的实际内容。
2. **不依赖场景。** 退款域的第 2/3 项本轮还没有场景数据（scenario_6/7 未落地），
   所以正负例一律用手搭的 fixture 库跑。等 R-2 的场景合进来，这些断言一个字不改。
3. **正例走真接口。** settled 的正例用 `guard.create_case` / `guard.update_biz_status`
   真的走一遍，而不是拿 SQL 摆一个「看着像」的状态 —— 这样才验得到 verify.py 的
   判据与守卫实际写进库的东西是同一个口径。负例才用裸 SQL 伪造（模拟守卫被绕过）。
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import types

import pytest

from maos.core.store import SqliteStore
from maos.obs.trace import (
    PROV_COMPENSATION,
    PROV_TASK_RESULT,
    PROV_UNKNOWN,
    TraceError,
    check_span_tree,
    export_trace,
    export_trace_bundle,
)
from maos.skills.invoker import _digest

ROOT = pathlib.Path(__file__).resolve().parents[2]

SENTINEL = "sk-omega-SENTINEL-do-not-leak-9f3c2a"


def _load_script(name: str) -> types.ModuleType:
    """把 ``scripts/<name>.py`` 当模块加载 —— scripts/ 不是包，只能这样进来。

    先塞进 ``sys.modules`` 再 exec：``@dataclass`` 装饰器要按 ``__module__`` 回查
    模块的命名空间，模块没登记的话它拿到 None 直接炸，而报错信息离原因极远。
    """
    key = f"_omega_{name}"
    spec = importlib.util.spec_from_file_location(key, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


make_evidence = _load_script("make_evidence")
verify = _load_script("verify")


PASS_REPORT = {
    "passed": 2, "failed": 0, "errors": 0, "duration": 0.11, "tool_error": None,
    "cases": [{"id": "tests/test_x.py::test_ok", "status": "passed", "msg": ""}],
    "summary": "回归 2 过 0 挂", "self_check": {"build": "pass", "lint": "pass"},
}
PATCH_SET = {
    "files": [{"path": "src/a.py", "diff": "@@ -1 +1,2 @@\n+ok"}],
    "summary": "补上校验", "self_check": {"build": "pass", "lint": "pass"},
}

PLAN_ID, TASK_ID, TRACE_ID = "plan_fixture", "task_fixture", "trace_fixture"
SKILL_INVOCATION = "inv0000000000000000000000000001"


# ---------------------------------------------------------------------------
# fixture 库：一条走完的正常链路 + 两份没有来源的产物
# ---------------------------------------------------------------------------
def build_db(path) -> SqliteStore:
    """手搭一个「跑完了」的库。

    调用顺序即时间顺序，因此产物落库时刻与事件时刻的先后关系是真的 ——
    trace 的 provenance 判据正是靠这个先后关系，摆一堆固定时间戳是验不到的。
    """
    store = SqliteStore(str(path))
    store.init_schema()
    store.insert_plan({"plan_id": PLAN_ID, "trace_id": TRACE_ID,
                       "goal": "fixture 目标", "state": "PENDING"})
    store.insert_task({"task_id": TASK_ID, "plan_id": PLAN_ID, "trace_id": TRACE_ID,
                       "role": "coding", "title": "fixture 任务", "state": "PENDING",
                       "attempt": 0, "risk_level": "L", "effect_risk": "L"})

    # ① 场景预置件：Plan 还没开跑就落库，没有任何事件能指到它 -> provenance unknown
    store.insert_artifact({"artifact_id": "art_seeded", "task_id": TASK_ID,
                           "plan_id": PLAN_ID, "kind": "test_report", "version": 1,
                           "content": PASS_REPORT})

    _ev(store, event_type="PlanTransition", from_state="PENDING", to_state="RUNNING")
    _ev(store, task_id=TASK_ID, event_type="StateTransition",
        from_state="PENDING", to_state="DISPATCHED", reason="dispatch")
    _ev(store, task_id=TASK_ID, event_type="StateTransition",
        from_state="DISPATCHED", to_state="RUNNING", reason="claim")
    _ev(store, task_id=TASK_ID, event_type="SkillInvoked", detail={
        "skill": "code.repo.patch", "version": "1.0.0", "status": "ok", "duration_ms": 3,
        "input_digest": _digest({"repo": "demo"}), "output_hash": _digest(PATCH_SET),
        "usage": None, "invocation_id": SKILL_INVOCATION,
    })

    # ② 真正由任务结果带回来的产物：落在 claim 与 submit 之间 -> provenance task_result
    store.insert_artifact({"artifact_id": "art_patch", "task_id": TASK_ID,
                           "plan_id": PLAN_ID, "kind": "patch_set", "version": 1,
                           "content": PATCH_SET})

    _ev(store, task_id=TASK_ID, event_type="StateTransition", from_state="RUNNING",
        to_state="AWAITING_REVIEW", reason="submit_result", detail={"artifacts": 1})
    _ev(store, task_id=TASK_ID, event_type="StateTransition", from_state="AWAITING_REVIEW",
        to_state="DONE", reason="gate_pass")
    _ev(store, event_type="PlanTransition", from_state="RUNNING", to_state="DONE")

    # ③ review_after_gate 的意见书：落在最后一条迁移之后 -> provenance unknown
    store.insert_artifact({"artifact_id": "art_review", "task_id": TASK_ID,
                           "plan_id": PLAN_ID, "kind": "review_note", "version": 1,
                           "content": {"defects": [], "conclusion": "放行"}})

    store.update_task(TASK_ID, state="DONE", attempt=1)
    store.update_plan_state(PLAN_ID, "DONE")
    return store


def _ev(store, **row) -> None:
    row.setdefault("plan_id", PLAN_ID)
    row.setdefault("trace_id", TRACE_ID)
    store.append_event_log(row)


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "maos.db"
    build_db(path)
    return str(path)


@pytest.fixture()
def bundle_dir(tmp_path, db):
    """一套完整证据目录（用 fixture 库，不跑真场景，快且确定）。"""
    out = tmp_path / "scenario-fixture"
    out.mkdir()
    make_evidence.write_bundle(db, str(out), scenario=1, exit_code=0, wall_ms=1,
                               log="fixture run\n", sha="deadbeef", secrets={})
    shutil.copy(db, out / "maos.db")
    return out


# ===========================================================================
# 1. export_trace：span 树的形状
# ===========================================================================
def test_span_tree_has_no_orphan_and_no_cycle(db):
    doc = export_trace(SqliteStore(db), PLAN_ID)
    assert check_span_tree(doc["spans"]) == []
    assert doc["summary"]["tree_errors"] == []
    roots = [s for s in doc["spans"] if s["parent_span_id"] is None]
    assert len(roots) == 1 and roots[0]["kind"] == "plan"


def test_every_span_has_otel_fields(db):
    doc = export_trace(SqliteStore(db), PLAN_ID)
    for s in doc["spans"]:
        for field in ("trace_id", "span_id", "parent_span_id", "name",
                      "start", "end", "attributes"):
            assert field in s, f"span 缺 OTel 字段 {field}: {s}"
        assert isinstance(s["attributes"], dict)
        assert s["start"] is not None and s["end"] is not None
        assert s["end"] >= s["start"], "span 的 end 不能早于 start"


def test_export_trace_is_deterministic(db):
    """同一份库导两次必须逐字节一致 —— verify.py 第 4 项的重放对比全靠这条。"""
    a = export_trace(SqliteStore(db), PLAN_ID)
    b = export_trace(SqliteStore(db), PLAN_ID)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_unknown_plan_raises_instead_of_empty_tree(db):
    with pytest.raises(TraceError):
        export_trace(SqliteStore(db), "plan_不存在")


def test_orphan_and_cycle_are_detected():
    """check_span_tree 自己的负例：造一棵坏树，必须报出来。"""
    orphan = [{"span_id": "a", "parent_span_id": None, "name": "root", "kind": "plan"},
              {"span_id": "b", "parent_span_id": "missing", "name": "x", "kind": "event"}]
    assert any("孤儿" in e for e in check_span_tree(orphan))

    cyclic = [{"span_id": "a", "parent_span_id": "b", "name": "x", "kind": "event"},
              {"span_id": "b", "parent_span_id": "a", "name": "y", "kind": "event"}]
    assert any("成环" in e for e in check_span_tree(cyclic))

    dup = [{"span_id": "a", "parent_span_id": None, "name": "r", "kind": "plan"},
           {"span_id": "a", "parent_span_id": None, "name": "r2", "kind": "plan"}]
    assert any("重复" in e for e in check_span_tree(dup))


# ===========================================================================
# 2. provenance：没有来源的产物必须看得见
# ===========================================================================
def test_unsourced_artifacts_are_labelled_not_hidden(db):
    """review_note 与场景预置件都没有 StateTransition 可指，必须标 unknown 且计数。

    这是派单点名的那处已知洞（docs/BACKLOG.md task-C 第 5 条）：本模块负责让它
    在 trace 里可见且注明来源不明，不负责去上游补审计行。
    """
    doc = export_trace(SqliteStore(db), PLAN_ID)
    prov = {s["attributes"]["maos.artifact.id"]: s["attributes"]["maos.artifact.provenance"]
            for s in doc["spans"] if s["kind"] == "artifact"}
    assert prov == {"art_seeded": PROV_UNKNOWN,
                    "art_patch": PROV_TASK_RESULT,
                    "art_review": PROV_UNKNOWN}
    assert doc["summary"]["unsourced_artifacts"] == 2

    for s in doc["spans"]:
        if s["kind"] != "artifact":
            continue
        note = s["attributes"]["maos.artifact.provenance.note"]
        if s["attributes"]["maos.artifact.provenance"] == PROV_UNKNOWN:
            assert note and "无来源" in note
        else:
            assert note is None
            assert s["attributes"]["maos.artifact.provenance.event_span"]


def test_seeded_artifact_does_not_steal_the_real_one_slot(db):
    """预置件排在真产物前面，若只按顺序取名额，两顶帽子会正好戴反。

    这条盯的是一类特别难发现的错：两边计数都对得上（1 有来源 / 1 无来源），
    只有点名到具体 artifact_id 才看得出戴反了。
    """
    doc = export_trace(SqliteStore(db), PLAN_ID)
    by_id = {s["attributes"]["maos.artifact.id"]: s
             for s in doc["spans"] if s["kind"] == "artifact"}
    assert by_id["art_patch"]["attributes"]["maos.artifact.provenance"] == PROV_TASK_RESULT
    assert by_id["art_seeded"]["attributes"]["maos.artifact.provenance"] == PROV_UNKNOWN


def test_compensation_artifact_is_traced_to_its_event(tmp_path):
    """补偿件由控制面在 on_task_result 里附着，靠 CompensationAttached 事件认领。"""
    path = tmp_path / "c.db"
    store = build_db(path)
    ref = {"task_id": TASK_ID, "kind": "patch_set", "attempt": 1}
    store.insert_artifact({"artifact_id": "art_comp", "task_id": TASK_ID, "plan_id": PLAN_ID,
                           "kind": "compensation", "version": 0,
                           "content": {"mode": "reverse", "patch_ref": ref}})
    _ev(store, task_id=TASK_ID, event_type="CompensationAttached",
        detail={"patch_ref": ref, "mode": "reverse"})

    doc = export_trace(SqliteStore(str(path)), PLAN_ID)
    comp = next(s for s in doc["spans"]
                if s["attributes"].get("maos.artifact.id") == "art_comp")
    assert comp["attributes"]["maos.artifact.provenance"] == PROV_COMPENSATION
    assert comp["parent_span_id"] == comp["attributes"]["maos.artifact.provenance.event_span"]


def test_stray_events_are_reported_not_swallowed(tmp_path):
    """plan_id 指不到任何 plan 的事件（如 scenario_5 建 Plan 之前的 issue.aggregate）。"""
    path = tmp_path / "s.db"
    store = build_db(path)
    store.append_event_log({"plan_id": "", "trace_id": "", "event_type": "SkillInvoked",
                            "detail": {"skill": "issue.aggregate", "status": "ok"}})
    bundle = export_trace_bundle(str(path))
    assert bundle["summary"]["stray_event_count"] == 1
    assert bundle["stray_events"][0]["event_type"] == "SkillInvoked"


# ===========================================================================
# 3. 证据文件：首行出处 + 可被 json 解析
# ===========================================================================
EVIDENCE_FILES = ("run.log", "trace.json", "result.json",
                  "business-objects.json", "kb-hits.json", "kb-dump.json")


def test_every_evidence_file_carries_provenance_header(bundle_dir):
    import re
    pattern = re.compile(
        r"^# generated at \d{4}-\d{2}-\d{2}T[\d:.]+\+\d{2}:\d{2} from [0-9a-f]+(-dirty)?$")
    for name in EVIDENCE_FILES:
        head = (bundle_dir / name).read_text(encoding="utf-8").splitlines()[0]
        assert pattern.match(head), f"{name} 首行出处格式不对: {head!r}"


def test_trace_json_parses_and_has_required_fields(bundle_dir):
    doc = make_evidence.load_evidence_json(str(bundle_dir / "trace.json"))
    assert doc["schema"] == "maos.trace/v1"
    assert doc["plan_count"] == 1
    for key in ("traces", "stray_events", "summary"):
        assert key in doc
    trace = doc["traces"][0]
    assert trace["plan_id"] == PLAN_ID and trace["spans"]
    assert check_span_tree(trace["spans"]) == []


def test_loader_rejects_a_file_without_the_header(tmp_path):
    """首行注释是铁律 3 的落点。没有它就不是本流水线产的证据，宁可读不出来。"""
    bad = tmp_path / "x.json"
    bad.write_text('{"a": 1}', encoding="utf-8")
    with pytest.raises(make_evidence.EvidenceError):
        make_evidence.load_evidence_json(str(bad))


def test_kb_files_say_empty_rather_than_fake_data(bundle_dir):
    hits = make_evidence.load_evidence_json(str(bundle_dir / "kb-hits.json"))
    assert hits["hits"] == [] and hits["note"]
    assert hits["has_kb_doc_table"] is False
    objs = make_evidence.load_evidence_json(str(bundle_dir / "business-objects.json"))
    assert objs["objects"] == [] and objs["note"]


# ===========================================================================
# 4. 生成器的负例：失败即报错退出，且不留下半份目录
# ===========================================================================
def _fake_proc(returncode: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(["fake"], returncode, stdout=stdout, stderr=stderr)


def test_child_failure_leaves_no_half_directory(tmp_path, monkeypatch):
    """上游场景挂了 -> 抛 EvidenceError，且既没有 scenario-N/ 也没有临时目录残留。

    半份目录比没有目录更坏：它看起来像跑通了，而缺的那半恰好是最关键的判据。
    """
    monkeypatch.setattr(make_evidence.subprocess, "run",
                        lambda *a, **k: _fake_proc(1, stderr="boom"))
    with pytest.raises(make_evidence.EvidenceError, match="退出码 1"):
        make_evidence.build_scenario(1, str(tmp_path), sha="abc", secrets={}, timeout=10)
    assert os.listdir(tmp_path) == []


def test_missing_db_also_leaves_nothing(tmp_path, monkeypatch):
    """子进程返回 0 却没落库，同样算失败 —— 不许拿一份没有库的目录充数。"""
    monkeypatch.setattr(make_evidence.subprocess, "run", lambda *a, **k: _fake_proc(0))
    with pytest.raises(make_evidence.EvidenceError):
        make_evidence.build_scenario(1, str(tmp_path), sha="abc", secrets={}, timeout=10)
    assert os.listdir(tmp_path) == []


def test_existing_directory_survives_a_failed_regeneration(tmp_path, monkeypatch):
    """重跑失败不许把上一次的好证据抹掉 —— 先攒后挪就是为了这个。"""
    keep = tmp_path / "scenario-1"
    keep.mkdir()
    (keep / "trace.json").write_text("old", encoding="utf-8")
    monkeypatch.setattr(make_evidence.subprocess, "run", lambda *a, **k: _fake_proc(1))
    with pytest.raises(make_evidence.EvidenceError):
        make_evidence.build_scenario(1, str(tmp_path), sha="abc", secrets={}, timeout=10)
    assert (keep / "trace.json").read_text(encoding="utf-8") == "old"


# ===========================================================================
# 5. 脱敏：出口替换 + 哨兵反查，反查命中即销毁目录
# ===========================================================================
def test_redact_replaces_secret_values():
    out = make_evidence.redact(f"key={SENTINEL} end", {"MAOS_LLM_API_KEY": SENTINEL})
    assert SENTINEL not in out
    assert "***REDACTED:MAOS_LLM_API_KEY***" in out


def test_scan_finds_sentinel_even_inside_a_binary_file(tmp_path):
    """按字节查而不是按行读文本：sqlite 库就在同一目录，按文本读会解码失败而跳过。"""
    (tmp_path / "blob.db").write_bytes(b"\x00\x01" + SENTINEL.encode() + b"\xff")
    hits = make_evidence.scan_for_secrets(str(tmp_path), {"MATRIX_TOKEN": SENTINEL})
    assert hits and "MATRIX_TOKEN" in hits[0]


def test_secret_names_are_recognised_and_short_values_ignored():
    got = make_evidence.secret_values({
        "MAOS_LLM_API_KEY": SENTINEL, "MATRIX_TOKEN": SENTINEL,
        "SOME_SECRET": "longenoughvalue", "MY_PASSWORD": "hunter2xx",
        "PATH": "/usr/bin", "SHORT_TOKEN": "ab",
    })
    assert set(got) == {"MAOS_LLM_API_KEY", "MATRIX_TOKEN", "SOME_SECRET", "MY_PASSWORD"}


def test_leak_destroys_the_directory_and_fails(tmp_path, monkeypatch, db):
    """出口脱敏漏了一条时，哨兵反查必须兜住：目录销毁 + 抛错，不许留下泄漏的证据。

    这里刻意把 redact 打成恒等函数来模拟「__repr__ 那类出口管不到的入口」。
    """
    def fake_run(cmd, **kwargs):
        shutil.copy(db, cmd[cmd.index("--_db") + 1])
        return _fake_proc(0, stdout=f"模型初始化 key={SENTINEL}\n")

    out = tmp_path / "out"          # 与 db fixture 的 maos.db 分开，免得误判残留
    out.mkdir()
    monkeypatch.setattr(make_evidence.subprocess, "run", fake_run)
    monkeypatch.setattr(make_evidence, "redact", lambda text, secrets: text)
    with pytest.raises(make_evidence.EvidenceError, match="敏感值明文"):
        make_evidence.build_scenario(1, str(out), sha="abc",
                                     secrets={"MAOS_LLM_API_KEY": SENTINEL}, timeout=10)
    assert os.listdir(out) == []


def test_pipeline_with_a_sentinel_key_in_env_leaks_nothing(tmp_path, db, monkeypatch):
    """派单验收那条的等价断言：灌哨兵进 env，跑完 grep 整个目录必须零命中。"""
    def fake_run(cmd, **kwargs):
        shutil.copy(db, cmd[cmd.index("--_db") + 1])
        return _fake_proc(0, stdout=f"启动 MAOS_LLM_API_KEY={SENTINEL}\n")

    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(make_evidence.subprocess, "run", fake_run)
    monkeypatch.setenv("MAOS_LLM_API_KEY", SENTINEL)
    make_evidence.build_scenario(1, str(out), sha="abc",
                                 secrets=make_evidence.secret_values(), timeout=10)
    assert make_evidence.scan_for_secrets(str(out), {"K": SENTINEL}) == []
    log = (out / "scenario-1" / "run.log").read_text(encoding="utf-8")
    assert "***REDACTED:MAOS_LLM_API_KEY***" in log


# ===========================================================================
# 6. verify.py：七项各自的正负例
# ===========================================================================
def _run_verify(evidence_root, *, as_json=True):
    cases = verify.load_cases(str(evidence_root), None)
    try:
        return {c.key: c for c in (fn(cases) for fn in verify.CHECKS)}
    finally:
        for c in cases:
            c.conn.close()


@pytest.fixture()
def evidence_root(tmp_path, bundle_dir):
    root = tmp_path / "evidence"
    root.mkdir()
    shutil.copytree(bundle_dir, root / "scenario-1")
    return root


def _rewrite(path, mutate):
    """改一份证据文件的 JSON 正文，保留首行出处 —— 模拟「事后手写」。"""
    text = path.read_text(encoding="utf-8")
    header, body = text.split("\n", 1)
    doc = json.loads(body)
    mutate(doc)
    path.write_text(header + "\n" + json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


# -- 第 1 项 ---------------------------------------------------------------
def test_1_hash_integrity_passes_on_clean_evidence(evidence_root):
    chk = _run_verify(evidence_root)["hash-integrity"]
    assert chk.status == verify.PASS and chk.passed == chk.total > 0


def test_1_hash_integrity_catches_a_doctored_digest(evidence_root):
    def tamper(doc):
        for span in doc["traces"][0]["spans"]:
            if span["attributes"].get("maos.event.type") == "SkillInvoked":
                span["attributes"]["maos.detail"]["output_hash"] = "0" * 64
                return
        raise AssertionError("fixture 里没有 SkillInvoked span")

    _rewrite(evidence_root / "scenario-1" / "trace.json", tamper)
    chk = _run_verify(evidence_root)["hash-integrity"]
    assert chk.status == verify.FAIL
    assert any("不一致" in n for n in chk.notes)


def test_1_hash_integrity_catches_a_deleted_invocation(evidence_root):
    """库里有、证据里没有 —— 删证据和改证据一样要抓。"""
    def tamper(doc):
        doc["traces"][0]["spans"] = [
            s for s in doc["traces"][0]["spans"]
            if s["attributes"].get("maos.event.type") != "SkillInvoked"]

    _rewrite(evidence_root / "scenario-1" / "trace.json", tamper)
    chk = _run_verify(evidence_root)["hash-integrity"]
    assert chk.status == verify.FAIL
    assert any("证据被删过" in n for n in chk.notes)


def test_1_hash_integrity_catches_a_wrong_failed_output_hash(tmp_path):
    """失败的 skill 其 output 恒为 None，output_hash 必须等于 _digest(None)。"""
    root = tmp_path / "evidence"
    (root / "scenario-1").mkdir(parents=True)
    path = tmp_path / "m.db"
    store = build_db(path)
    _ev(store, task_id=TASK_ID, event_type="SkillInvoked", detail={
        "skill": "code.repo.patch", "version": "1.0.0", "status": "failed", "duration_ms": 1,
        "input_digest": _digest({"x": 1}), "output_hash": _digest({"编的": True}),
        "usage": None, "invocation_id": "inv0000000000000000000000000002",
    })
    make_evidence.write_bundle(str(path), str(root / "scenario-1"), scenario=1, exit_code=0,
                               wall_ms=1, log="", sha="abc", secrets={})
    shutil.copy(path, root / "scenario-1" / "maos.db")
    chk = _run_verify(root)["hash-integrity"]
    assert chk.status == verify.FAIL
    assert any("_digest(None)" in n for n in chk.notes)


# -- 第 4 项 ---------------------------------------------------------------
def test_4_trace_tree_passes_on_clean_evidence(evidence_root):
    chk = _run_verify(evidence_root)["trace-tree"]
    assert chk.status == verify.PASS
    assert any("没有来源事件" in n for n in chk.notes), "无来源产物必须印出来"


def test_4_trace_tree_catches_an_orphan_span(evidence_root):
    _rewrite(evidence_root / "scenario-1" / "trace.json",
             lambda d: d["traces"][0]["spans"][-1].__setitem__("parent_span_id", "不存在"))
    chk = _run_verify(evidence_root)["trace-tree"]
    assert chk.status == verify.FAIL
    assert any("孤儿" in n for n in chk.notes)


def test_4_trace_tree_catches_a_silently_edited_span(evidence_root):
    """只改一个不参与哈希的字段，重放对比照样抓得住。"""
    _rewrite(evidence_root / "scenario-1" / "trace.json",
             lambda d: d["traces"][0]["spans"][0].__setitem__("name", "plan:伪造的目标"))
    chk = _run_verify(evidence_root)["trace-tree"]
    assert chk.status == verify.FAIL
    assert any("与库重放结果不一致" in n for n in chk.notes)


# -- 第 6 项 ---------------------------------------------------------------
def test_6_business_outcome_passes_with_external_evidence(evidence_root):
    chk = _run_verify(evidence_root)["business-outcome"]
    assert chk.status == verify.PASS and chk.total > 0
    assert any("来源未审计" in n for n in chk.notes), "预置判据要被点名，不能默默算数"


def test_6_done_without_external_evidence_fails(evidence_root):
    def tamper(doc):
        doc["plans"][0]["business_outcome"]["external_evidence"] = []

    _rewrite(evidence_root / "scenario-1" / "result.json", tamper)
    chk = _run_verify(evidence_root)["business-outcome"]
    assert chk.status == verify.FAIL
    assert any("没有任何外部判据" in n for n in chk.notes)


def test_6_state_mismatch_between_evidence_and_db_fails(evidence_root):
    _rewrite(evidence_root / "scenario-1" / "result.json",
             lambda d: d["plans"][0].__setitem__("state", "FAILED"))
    chk = _run_verify(evidence_root)["business-outcome"]
    assert chk.status == verify.FAIL
    assert any("与库里" in n for n in chk.notes)


def test_6_non_terminal_plan_is_not_judged(tmp_path):
    """场景 4 那种停在 RUNNING 的 Plan 不进第 6 项判据 —— 也不许因此判负。"""
    root = tmp_path / "evidence"
    (root / "scenario-1").mkdir(parents=True)
    path = tmp_path / "r.db"
    store = SqliteStore(str(path))
    store.init_schema()
    store.insert_plan({"plan_id": PLAN_ID, "trace_id": TRACE_ID, "goal": "跑一半",
                       "state": "RUNNING"})
    store.append_event_log({"plan_id": PLAN_ID, "trace_id": TRACE_ID,
                            "event_type": "PlanTransition",
                            "from_state": "PENDING", "to_state": "RUNNING"})
    make_evidence.write_bundle(str(path), str(root / "scenario-1"), scenario=4, exit_code=0,
                               wall_ms=1, log="", sha="abc", secrets={})
    shutil.copy(path, root / "scenario-1" / "maos.db")
    chk = _run_verify(root)["business-outcome"]
    assert chk.status == verify.PASS and chk.total == 0


# -- 第 5、7 项：SKIP 语义 --------------------------------------------------
def test_5_and_7_skip_when_kb_layer_is_absent(evidence_root):
    checks = _run_verify(evidence_root)
    for key in ("kb-hit", "history-case"):
        chk = checks[key]
        assert chk.status == verify.SKIP
        assert chk.skip_reason and "kb" in chk.skip_reason
        assert chk.passed == 0 and chk.total == 0


def test_skipped_checks_are_named_and_excluded_from_the_numerator(evidence_root, capsys):
    """SKIP 不许进分子，且必须在结尾点名 —— 静默跳过等于谎报。"""
    cases = verify.load_cases(str(evidence_root), None)
    try:
        results = [fn(cases) for fn in verify.CHECKS]
        code = verify.render(results, cases, as_json=False)
    finally:
        for c in cases:
            c.conn.close()
    out = capsys.readouterr().out
    scored = [r for r in results if r.status != verify.SKIP]
    skipped = [r for r in results if r.status == verify.SKIP]
    assert f"RESULT: {len(scored)}/{len(scored)} PASS" in out
    assert f"{len(skipped)} SKIP" in out and "不计入分子" in out
    for r in skipped:
        assert r.key in out, "被跳过的项必须在总结里点名"
    assert code == 0


# ===========================================================================
# 7. 第 2、3 项：退款域的正负例（手搭 fixture，不等场景 6/7）
# ===========================================================================
TENANT, CASE_ID, ORDER_ID = "t1", "case-1", "ord-1"
OBSERVE_INVOCATION = "inv0000000000000000000000000009"


def _refund_base(store):
    """租户 / 订单快照 / business_ref —— 第 2 项的正例底座。"""
    from maos.domain.refund import objects as ro

    ro.ensure_schema(store)
    ro.execute(store, "INSERT INTO tenant (tenant_id, name) VALUES (?,?)", (TENANT, "租户一"))
    ro.execute(store, "INSERT INTO order_snapshot (tenant_id, order_id, version, sku,"
                      " amount_paid, paid_at, channel_id, policy_version_at_order, read_at)"
                      " VALUES (?,?,?,?,?,?,?,?,?)",
               (TENANT, ORDER_ID, 3, "sku-1", 100.0, "2026-01-01T00:00:00+00:00", "ch1", 1,
                "2026-01-02T00:00:00+00:00"))
    ro.attach_business_ref(store, plan_id=PLAN_ID, task_id=TASK_ID, tenant_id=TENANT,
                           object_type="order_snapshot", object_id=ORDER_ID,
                           object_version=3, purpose="核对订单")


def _settle_through_the_guard(store):
    """走真守卫把 case 推到 settled —— 正例必须用生产路径，不能拿 SQL 摆样子。"""
    from maos.domain.refund import guard

    guard.create_case(store, tenant_id=TENANT, case_id=CASE_ID, channel_id="ch1",
                      order_id=ORDER_ID, order_version=3, sku="sku-1", reason_code="quality",
                      amount_claimed=100.0, plan_id=PLAN_ID, actor_skill="refund.intake",
                      invocation_id="inv0000000000000000000000000003")
    for nxt in ("approved", "gateway_accepted", "processing"):
        guard.update_biz_status(store, TENANT, CASE_ID, nxt, "refund.flow",
                                "inv0000000000000000000000000004")
    guard.update_biz_status(
        store, TENANT, CASE_ID, "settled", "payment.observe", OBSERVE_INVOCATION,
        observation={"request_id": "req-1", "gateway_code": "SUCCESS",
                     "observed_state": "settled"})
    _ev(store, task_id=TASK_ID, event_type="SkillInvoked", detail={
        "skill": "payment.observe", "version": "1.0.0", "status": "ok", "duration_ms": 2,
        "input_digest": _digest({"request_id": "req-1"}), "output_hash": _digest({"ok": True}),
        "usage": None, "invocation_id": OBSERVE_INVOCATION,
    })


def _refund_evidence(tmp_path, prepare) -> pathlib.Path:
    root = tmp_path / "evidence"
    (root / "scenario-1").mkdir(parents=True)
    path = tmp_path / "refund.db"
    store = build_db(path)
    prepare(store)
    make_evidence.write_bundle(str(path), str(root / "scenario-1"), scenario=6, exit_code=0,
                               wall_ms=1, log="", sha="abc", secrets={})
    shutil.copy(path, root / "scenario-1" / "maos.db")
    return root


def test_2_business_ref_passes_when_the_object_resolves(tmp_path):
    root = _refund_evidence(tmp_path, _refund_base)
    chk = _run_verify(root)["business-ref"]
    assert chk.status == verify.PASS and chk.passed == 1


def test_2_dangling_business_ref_fails(tmp_path):
    def prepare(store):
        from maos.domain.refund import objects as ro
        ro.ensure_schema(store)
        ro.attach_business_ref(store, plan_id=PLAN_ID, task_id=TASK_ID, tenant_id=TENANT,
                               object_type="order_snapshot", object_id="ord-不存在",
                               object_version=1, purpose="核对订单")

    chk = _run_verify(_refund_evidence(tmp_path, prepare))["business-ref"]
    assert chk.status == verify.FAIL
    assert any("引用悬空" in n for n in chk.notes)


def test_2_version_mismatch_fails(tmp_path):
    """对象在、版本不对 —— 这是「业务锚点是假的」最常见的一种。"""
    def prepare(store):
        from maos.domain.refund import objects as ro
        _refund_base(store)
        ro.execute(store, "DELETE FROM business_ref")
        ro.attach_business_ref(store, plan_id=PLAN_ID, task_id=TASK_ID, tenant_id=TENANT,
                               object_type="order_snapshot", object_id=ORDER_ID,
                               object_version=99, purpose="核对订单")

    chk = _run_verify(_refund_evidence(tmp_path, prepare))["business-ref"]
    assert chk.status == verify.FAIL
    assert any("引用悬空" in n for n in chk.notes)


def test_3_authoritative_fact_passes_for_a_properly_settled_case(tmp_path):
    def prepare(store):
        _refund_base(store)
        _settle_through_the_guard(store)

    chk = _run_verify(_refund_evidence(tmp_path, prepare))["authoritative-fact"]
    assert chk.status == verify.PASS and chk.passed == 1


def test_3_settled_without_a_receipt_fails(tmp_path):
    """守卫挡得住这条路，所以只能裸 SQL 伪造 —— 模拟守卫被绕过后的库。

    verify.py 是守卫之外的第二道：守卫管「写不进去」，它管「写进去了也查得出来」。
    """
    def prepare(store):
        _refund_base(store)
        _settle_through_the_guard(store)
        store._conn.execute("DELETE FROM payment_observation")
        store._conn.commit()

    chk = _run_verify(_refund_evidence(tmp_path, prepare))["authoritative-fact"]
    assert chk.status == verify.FAIL
    assert any("没有 payment_observation" in n for n in chk.notes)


def test_3_receipt_from_a_non_observer_fails(tmp_path):
    """回执在，但 actor_invocation_id 指不到任何一次 payment.observe 调用。"""
    def prepare(store):
        _refund_base(store)
        _settle_through_the_guard(store)
        store._conn.execute(
            "UPDATE payment_observation SET actor_invocation_id='inv-伪造'")
        store._conn.commit()

    chk = _run_verify(_refund_evidence(tmp_path, prepare))["authoritative-fact"]
    assert chk.status == verify.FAIL
    assert any("权威事实边界被绕过" in n for n in chk.notes)


def test_3_skips_cleanly_when_the_refund_domain_is_absent(evidence_root):
    chk = _run_verify(evidence_root)["authoritative-fact"]
    assert chk.status == verify.SKIP and chk.passed == 0


# ===========================================================================
# 8. verify.py 的退出码 —— 评委会直接看它
# ===========================================================================
def _verify_cli(evidence_root) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "verify.py"),
         "--evidence", str(evidence_root), "--db", str(evidence_root)],
        capture_output=True, text=True, cwd=str(ROOT))


def test_exit_code_is_zero_when_nothing_fails(evidence_root):
    proc = _verify_cli(evidence_root)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "RESULT:" in proc.stdout and "SKIP" in proc.stdout


def test_exit_code_is_non_zero_after_a_single_tampered_character(evidence_root):
    _rewrite(evidence_root / "scenario-1" / "trace.json",
             lambda d: d["traces"][0]["spans"][0].__setitem__("name", "plan:改了一个字"))
    proc = _verify_cli(evidence_root)
    assert proc.returncode != 0, proc.stdout
    assert "[FAIL] trace-tree" in proc.stdout


def test_missing_database_fails_loudly_instead_of_reporting_all_pass(evidence_root):
    """库不在时绝不能因为「没有东西可查」而报全过 —— 那是最坏的一种假象。"""
    (evidence_root / "scenario-1" / "maos.db").unlink()
    proc = _verify_cli(evidence_root)
    assert proc.returncode != 0
    assert "缺数据库" in proc.stderr
