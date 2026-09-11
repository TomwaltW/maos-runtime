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
# 2b. 圆桌那一族树（T134）
# ===========================================================================
#: 圆桌那一摊行的伪 plan_id。前缀是契约（``maos/obs/trace.py::ROUNDTABLE_PLAN_PREFIX``），
#: 后半段是 case_id。本节**不 import 圆桌引擎** —— trace 侧认的只是这个前缀，
#: 换业务域、甚至圆桌那个包整个不在，这一族树也该照样导得出来（铁律 9）。
RT_PLAN = "roundtable:RC-TEST-0001"


def _roundtable_rows(store, *, plan_id: str = RT_PLAN, verdict: bool = True,
                     seats=("refund-intake", "refund-policy")) -> None:
    """在库里摆一轮圆桌：`RoundtableRound` → 座位 → 中间夹一次 skill → 合议。"""
    store.append_event_log({"plan_id": plan_id, "trace_id": "",
                            "event_type": "RoundtableRound",
                            "detail": {"entry": "preflight", "round_no": 1,
                                       "tenant_id": "tnt-1", "case_id": "RC-TEST-0001",
                                       "seats": list(seats)}})
    for i, seat in enumerate(seats):
        store.append_event_log({"plan_id": plan_id, "trace_id": "",
                                "event_type": "RoundtableSeatSpoke",
                                "detail": {"seat": seat, "spoken_by_model": i == 0,
                                           "fallback_reason": "" if i == 0 else "no_model",
                                           "facts_digest": "f" * 16,
                                           "speech_digest": "s" * 16, "speech_len": 42}})
        if i == 0:
            store.append_event_log({"plan_id": plan_id, "trace_id": "",
                                    "task_id": "preview-evidence",
                                    "event_type": "SkillInvoked",
                                    "detail": {"skill": "refund.evidence_check",
                                               "status": "ok", "duration_ms": 3}})
    if verdict:
        store.append_event_log({"plan_id": plan_id, "trace_id": "",
                                "event_type": "RoundtableVerdict",
                                "detail": {"recommend": "approve",
                                           "approver_role": "refund_finance",
                                           "blockers": []}})


def test_roundtable_events_get_their_own_tree_not_a_plan_tree(tmp_path):
    """圆桌那一段自成一族：``roundtable_traces``，而**不是**第二棵 plan 树。

    混进 ``traces`` 的后果是下游每个「按 plan 读」的消费者都得先判断这棵树是不是
    真 plan —— 漏判一处就印出一棵 ``plan_state: null`` 的树，看着像库坏了。
    """
    path = tmp_path / "rt.db"
    store = build_db(path)
    _roundtable_rows(store)

    bundle = export_trace_bundle(str(path))
    assert bundle["plan_count"] == 1, "圆桌不许让 plan 数变多"
    assert [t["plan_id"] for t in bundle["traces"]] == [PLAN_ID]

    trees = bundle["roundtable_traces"]
    assert len(trees) == 1
    tree = trees[0]
    assert tree["plan_id"] == RT_PLAN and tree["case_id"] == "RC-TEST-0001"
    assert tree["trace_id"] == "", "圆桌不属于任何 Run —— 不许编一个 trace_id"
    assert check_span_tree(tree["spans"]) == [], "圆桌树也得无孤儿无环、单根"
    assert tree["summary"]["by_event_type"] == {
        "RoundtableRound": 1, "RoundtableSeatSpoke": 2,
        "RoundtableVerdict": 1, "SkillInvoked": 1}
    assert tree["summary"]["round_count"] == 1
    assert tree["summary"]["seats_by_model"] == 1, "一岗是模型说的，一岗是事实卡"
    assert tree["summary"]["headless_rounds"] == 0
    # 三层：圆桌根 → 轮 → 事件。轮 span 的父亲是根，事件的父亲是轮。
    kinds = {s["kind"] for s in tree["spans"]}
    assert kinds == {"roundtable", "roundtable-round", "event"}
    root = next(s for s in tree["spans"] if s["parent_span_id"] is None)
    assert root["kind"] == "roundtable"
    rnd = next(s for s in tree["spans"] if s["kind"] == "roundtable-round")
    assert rnd["parent_span_id"] == root["span_id"]
    assert all(s["parent_span_id"] == rnd["span_id"]
               for s in tree["spans"] if s["kind"] == "event")
    # 顺序即 seq 顺序：座位与 skill 交织，「这一岗说话之前跑过 skill 吗」看得出来。
    seqs = [s["attributes"]["maos.event.seq"] for s in tree["spans"]
            if s["kind"] in ("event", "roundtable-round")]
    assert seqs == sorted(seqs)


def test_roundtable_tree_claims_its_events_so_they_are_no_longer_stray(tmp_path):
    """收走了就不再算游离 —— 同一条记录在证据里只出现一个地方。"""
    path = tmp_path / "rt.db"
    store = build_db(path)
    _roundtable_rows(store)

    bundle = export_trace_bundle(str(path))
    assert bundle["stray_events"] == []
    assert bundle["summary"]["stray_event_count"] == 0
    assert bundle["summary"]["roundtable_event_count"] == 5
    assert bundle["summary"]["roundtable_tree_count"] == 1
    assert bundle["summary"]["roundtable_round_count"] == 1
    assert bundle["summary"]["roundtable_seat_spoke_count"] == 2
    assert bundle["summary"]["roundtable_skill_invoked_count"] == 1


def test_claiming_the_roundtable_does_not_excuse_other_strays(tmp_path):
    """🔴 判据是「**被树收走的**不算游离」，不是「plan_id 非空就不算游离」。

    后一种写法也能让 warn 归零，代价是把两类真该查的事件一起藏掉：``plan_id`` 是
    空串的（建 Plan 之前的调用）和指向一个**不存在的** Plan 的
    （``docs/BACKLOG.md ## task-t110`` 那类「被否决的计划」）。两类都必须留在
    ``stray_events`` 里 —— 少一条，这条测试就红。
    """
    path = tmp_path / "rt.db"
    store = build_db(path)
    _roundtable_rows(store)
    store.append_event_log({"plan_id": "", "trace_id": "", "event_type": "SkillInvoked",
                            "detail": {"skill": "issue.aggregate", "status": "ok"}})
    store.append_event_log({"plan_id": "plan_vetoed_never_created", "trace_id": "",
                            "event_type": "TaskCreationVetoed", "detail": {}})

    bundle = export_trace_bundle(str(path))
    strays = bundle["stray_events"]
    assert {s["plan_id"] for s in strays} == {"", "plan_vetoed_never_created"}
    assert bundle["summary"]["stray_event_count"] == 2
    assert bundle["summary"]["roundtable_event_count"] == 5, "圆桌照旧被收走"


def test_roundtable_usage_lands_in_the_tree_cost_not_in_unattributed(tmp_path):
    """圆桌那几次调用归进圆桌树的 ``cost``，成本账上**看得见**（T134 的主目标）。

    改动之前它们全在 ``unattributed_usage`` 里，于是 ``attributed_tokens_total``
    不含圆桌 —— 「真模型跑一单花多少 token」在证据里查不到。
    """
    path = tmp_path / "rt.db"
    store = build_db(path)
    _roundtable_rows(store)
    for role, tin, tout in (("refund_intake", 864, 79), ("refund_policy", 947, 87)):
        store.insert_model_usage({
            "trace_id": "", "plan_id": RT_PLAN, "task_id": None, "agent_role": role,
            "call_site": "maos/roundtable/speaker.py::Speaker.complete",
            "model": "deepseek-flash", "tier": "light", "tokens_in": tin,
            "tokens_out": tout, "latency_ms": 1500, "estimated": 0})
    # 对照组：建 Plan 之前的那一类，trace_id 与 plan_id 都空 —— 它必须留在原地。
    store.insert_model_usage({
        "trace_id": "", "plan_id": "", "task_id": None, "agent_role": "manager",
        "call_site": "maos/agents/manager.py::ManagerAgent.plan", "model": "scripted-strong",
        "tier": "strong", "tokens_in": 100, "tokens_out": 200, "latency_ms": 0,
        "estimated": 1})

    bundle = export_trace_bundle(str(path))
    tree = bundle["roundtable_traces"][0]
    assert tree["cost"]["calls"] == 2
    assert tree["cost"]["tokens_total"] == 864 + 79 + 947 + 87
    assert tree["cost"]["measured_calls"] == 2, "真模型的行不许被当成估算"
    assert tree["cost"]["all_estimated"] is False
    assert {b["role"] for b in tree["cost"]["by_role"]} == {"refund_intake", "refund_policy"}
    assert [r["seq"] for r in tree["model_usage"]] == [1, 2]
    assert tree["cost"]["failures"]["available"] is False, "圆桌没有失败记账，不许装作有"

    sm = bundle["summary"]
    assert sm["roundtable_model_calls"] == 2
    assert sm["roundtable_tokens_total"] == 1977
    assert sm["attributed_model_calls"] == sm["plan_model_calls"] + 2
    assert sm["unattributed_model_calls"] == 1, "manager 那条照旧归属不上"
    assert [r["agent_role"] for r in bundle["unattributed_usage"]] == ["manager"]
    # 一笔花销只算一次：三个数加起来正好是库里的行数。
    assert sm["model_calls"] == sm["plan_model_calls"] + 2 + 1


def test_headless_roundtable_round_is_collected_and_says_so(tmp_path):
    """``RoundtableRound`` 那条没落下来时，后面几岗**照旧收进树**并标明无头。

    丢掉它们等于这一段少报几岗，而少报与「本来就没说」在屏幕上长得一模一样。
    """
    path = tmp_path / "rt.db"
    store = build_db(path)
    store.append_event_log({"plan_id": RT_PLAN, "trace_id": "",
                            "event_type": "RoundtableSeatSpoke",
                            "detail": {"seat": "refund-intake", "spoken_by_model": True}})

    tree = export_trace_bundle(str(path))["roundtable_traces"][0]
    assert tree["summary"]["round_count"] == 1
    assert tree["summary"]["headless_rounds"] == 1
    assert tree["summary"]["seat_spoke_count"] == 1
    assert check_span_tree(tree["spans"]) == []
    rnd = next(s for s in tree["spans"] if s["kind"] == "roundtable-round")
    assert rnd["attributes"]["maos.roundtable.headless"] is True
    assert rnd["attributes"]["maos.roundtable.headless.note"], "无头必须在树上说出来"
    assert export_trace_bundle(str(path))["stray_events"] == []


def test_two_roundtable_cases_get_two_trees(tmp_path):
    """两单圆桌两棵树，不混成一摊（伪 plan_id 不同就是不同一段）。"""
    path = tmp_path / "rt.db"
    store = build_db(path)
    _roundtable_rows(store, plan_id="roundtable:RC-A")
    _roundtable_rows(store, plan_id="roundtable:RC-B", verdict=False)

    trees = export_trace_bundle(str(path))["roundtable_traces"]
    assert [t["plan_id"] for t in trees] == ["roundtable:RC-A", "roundtable:RC-B"]
    assert [t["summary"]["verdict_count"] for t in trees] == [1, 0]
    assert all(check_span_tree(t["spans"]) == [] for t in trees)


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


def test_base_url_is_always_a_sentinel_although_it_is_not_a_key():
    """``MAOS_LLM_BASE_URL`` 必须在恒脱敏名单里，哪怕它不是密钥。

    铁律 6 点名的是「凡是可能回显 env 或 **URL** 的命令，输出前必须过脱敏」，
    而 ``select_model_client()`` 的「启用真模型：base_url=… model=…」那一行
    **会**进 ``run.log`` —— ``--live-model`` 产的每一束都有。名字里没有
    key/secret/token，``_SECRET_NAME`` 那条正则命不中，只能点名。

    内网网关地址进证据本身就是泄漏面；自建网关还常把凭据编在路径里，
    那种情况下这一行就是明文 key。这里用假 URL，真的绝不进测试。
    """
    fake = "https://gw.internal.invalid/compat-mode/v1"
    got = make_evidence.secret_values({"MAOS_LLM_BASE_URL": fake})
    assert got == {"MAOS_LLM_BASE_URL": fake}, (
        "MAOS_LLM_BASE_URL 不在恒脱敏名单里 —— run.log 会把它原样写进证据")

    out = make_evidence.redact(f"启用真模型：base_url={fake} model=x", got)
    assert fake not in out
    assert "***REDACTED:MAOS_LLM_BASE_URL***" in out


def test_live_model_log_line_prints_the_host_not_the_whole_url(caplog, monkeypatch):
    """``select_model_client()`` 那一行只打 host —— 它会跟着 ``run.log`` 进证据束。

    上一条守的是**产物侧**的兜底（落盘前脱敏）；这一条守的是**出口**：兜底生效
    之前，那一行在终端与 CI 日志里是明文，而自建网关常把凭据编在路径里。
    两道都要，缺哪道都有一类去处没人管。

    这条测试挨着上一条放（同一个泄漏面的两半），也因为本轨只许动
    `test_trace_evidence.py` / `test_verify_warn.py` 这两个测试文件。
    **用假 URL，真的绝不进测试**（铁律 6）。
    """
    import logging

    from maos.model import client as client_mod
    from maos.model.client import ENV_API_KEY, ENV_BASE_URL, ENV_FORCE_SCRIPTED, ENV_MODEL

    fake = f"https://robot:{SENTINEL}@gw.internal.invalid:8443/compat-mode/v1"
    monkeypatch.delenv(ENV_FORCE_SCRIPTED, raising=False)
    monkeypatch.setenv(ENV_BASE_URL, fake)
    monkeypatch.setenv(ENV_API_KEY, SENTINEL)
    monkeypatch.setenv(ENV_MODEL, "fake-model")

    # logger 名从模块本身取，不写死 —— 写死的话它改名那天这条测试会静默地一行都收不到。
    with caplog.at_level(logging.INFO, logger=client_mod.log.name):
        client_mod.select_model_client()
    printed = "\n".join(r.getMessage() for r in caplog.records)

    assert "gw.internal.invalid:8443" in printed, "连 host 都不打，日志就不说人话了"
    for leaked in (fake, SENTINEL, "/compat-mode/v1", "robot:"):
        assert leaked not in printed, f"整条 base_url 的这一截进了日志：{leaked!r}"

    # 解析不出 host 时不回退到原串 —— 回退等于在最可疑的那种输入上打全文。
    assert client_mod._url_host("gw.internal.invalid/v1") == "?"
    assert client_mod._url_host("") == "?"


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


# ---------------------------------------------------------------------------
# 5b. 截图：字节扫描核验不了，必须有声地拒收（H-7 §5.1）
# ---------------------------------------------------------------------------
# BACKLOG ## task-C4 把这个洞记成「scan_for_secrets 只扫文本，扫不到 PNG」——
# 归因不成立：它本来就按字节读（上面那条 binary 测试守着）。真实原因是
# **截图里的 token 是像素不是字节**：PNG 把文字压成图像数据，`Bearer <token>`
# 在文件里根本不以该字节序列存在。扫字节扫不到，扫文本一样扫不到，扫得再狠也扫不到。
# 所以判据不能是「扫得更狠」，只能是把静默通过变成显式拒收。
def test_image_in_a_bundle_is_reported_as_unverifiable_not_silently_passed(tmp_path):
    """放一个假 PNG 进去：必须报「无法核验」，而不是默默判干净。

    断言刻意分两层：先证**不是**因为哨兵串出现在文件里才命中（这个 PNG 里
    一个字节的哨兵都没有），再证报出来的话说的是「无法核验」而不是「命中」——
    假装扫过了比不扫更危险，它给的是假的安全感。
    """
    png = tmp_path / "room-screenshot.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)   # 无哨兵串，纯像素状字节
    assert SENTINEL.encode() not in png.read_bytes()

    hits = make_evidence.scan_for_secrets(str(tmp_path), {"MATRIX_TOKEN": SENTINEL})

    assert len(hits) == 1
    assert "room-screenshot.png" in hits[0]
    assert "无法核验" in hits[0]
    assert "命中" not in hits[0]


def test_unverifiable_check_covers_bitmaps_and_pdf_but_not_svg():
    """取值域的边界：位图与 PDF 核验不了；SVG 是文本，哨兵串扫得到，不该混进来。"""
    for name in ("a.png", "a.JPG", "a.jpeg", "a.gif", "a.webp", "a.pdf", "a.TIF"):
        assert make_evidence.is_unverifiable(name), name
    for name in ("a.svg", "a.json", "a.log", "a.db", "a.md", "noext"):
        assert not make_evidence.is_unverifiable(name), name


def test_no_secrets_in_env_means_no_unverifiable_noise(tmp_path):
    """环境里没有要防的密钥，就没有「核验」这回事，也谈不上「无法核验」。

    没有这一条，没配密钥的机器每跑一次都报一屏警告 —— 于是真出事那次没人看。
    """
    (tmp_path / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    assert make_evidence.scan_for_secrets(str(tmp_path), {}) == []


# ---------------------------------------------------------------------------
# 5c. 出处 sha 全束一致（H-7 §5.3）
# ---------------------------------------------------------------------------
def test_pin_sha_survives_a_second_module_instance(tmp_path, monkeypatch):
    """钉住的 sha 必须跨**模块实例**可见 —— 这正是 R5 从前恒带 `-dirty` 的原因。

    `python3 scripts/make_evidence.py` 让脚本成为 `__main__`，而
    `maos/kb/experiment.py` 走 `from scripts.make_evidence import git_sha`，
    那是另一个模块对象、另一份模块全局变量。所以钉在模块全局里对 R5 一侧不可见；
    只有进程级的环境变量两边都看得到。这条测试就是在断言那个选择。
    """
    monkeypatch.delenv(make_evidence._PIN_ENV, raising=False)
    monkeypatch.setattr(make_evidence, "git_sha", lambda: "cafebabe")
    pinned = make_evidence.pin_sha()
    assert pinned == "cafebabe"

    # 第二个实例：同一个源文件再加载一遍，模拟 experiment.py 那侧的 import
    second = _load_script("make_evidence")
    assert second is not make_evidence
    assert second.git_sha() == "cafebabe", "另一个模块实例没看到钉住的 sha"


def test_pin_sha_is_taken_once_and_does_not_drift(monkeypatch):
    """钉过之后工作区再脏，出处也不跟着变：落盘顺序不该改变证据的出处。"""
    monkeypatch.delenv(make_evidence._PIN_ENV, raising=False)
    calls = []

    def drifting():
        calls.append(1)
        return f"sha-{len(calls)}"

    monkeypatch.setattr(make_evidence, "git_sha", drifting)
    assert make_evidence.pin_sha() == "sha-1"
    assert make_evidence.pin_sha() == "sha-1"
    assert len(calls) == 1, "sha 被重复求值，八束会各记各的出处"


# ---------------------------------------------------------------------------
# 5d. INDEX 要登记 evidence/ 下的非 scenario 目录（H-7 §5.2）
# ---------------------------------------------------------------------------
def test_index_registers_non_scenario_dirs_like_room(tmp_path):
    """`evidence/room/` 从前在 INDEX 里一个字都没有 —— 漏登记一整个目录的索引不叫索引。

    同时守住边界：`scenario-*` 归 produced 那半边，临时目录不登记。
    """
    (tmp_path / "scenario-1").mkdir()
    (tmp_path / ".tmp-scenario-9.123").mkdir()
    room = tmp_path / "room"
    room.mkdir()
    (room / "README.md").write_text(
        make_evidence.header_line("abc") + "\n正文\n", encoding="utf-8")
    (room / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    aux = make_evidence.scan_aux_bundles(str(tmp_path))

    assert [b["name"] for b in aux] == ["room"], "只该登记非 scenario、非临时的目录"
    files = {f["name"]: f for f in aux[0]["files"]}
    assert files["README.md"]["sourced"] is True
    assert files["shot.png"]["secret_scan"].startswith("无法核验")
    assert files["README.md"]["secret_scan"] == "可扫"


# ---------------------------------------------------------------------------
# 5e. 真模型束与 Scripted 束不共用一个根（T128）
# ---------------------------------------------------------------------------
#: 缺省那两条路的产出根**逐字节不许变**：`evidence/` 与 `evidence/domains/` 写死在
#: `scripts/demo_preflight.sh`、复赛材料、以及 `verify.py` 的缺省 `--evidence` 里。
SCRIPTED_ROOTS = {
    (False, False): ROOT / "evidence",
    (False, True): ROOT / "evidence" / "domains",
}


class _StopAfterResolve(Exception):
    """产出根算完就停 —— 这几条测试只看路径，一个字节都不该落盘。"""


def _resolved_out(argv, monkeypatch) -> str:
    """跑一遍 ``main()`` 的参数解析，返回它**打算**写到哪。

    截在 ``assert_root_mode`` 上：那是三条分支（缺省 / ``--domains`` /
    ``--contrast``）唯一都会经过、且在 ``os.makedirs`` 之前的一处。截在各分支里
    就得写三份不同的桩，而**漏掉一条分支**正是这一轨要修的那个 bug 的形状。
    """
    monkeypatch.setenv(make_evidence.FORCE_SCRIPTED_ENV, "1")   # 跑完由 monkeypatch 还原
    seen = {}

    def spy(out_root, mode):
        seen["out"], seen["mode"] = out_root, mode
        raise _StopAfterResolve

    monkeypatch.setattr(make_evidence, "assert_root_mode", spy)
    with pytest.raises(_StopAfterResolve):
        make_evidence.main(argv)
    return seen


@pytest.mark.parametrize("domains", [False, True])
def test_scripted_out_root_is_unchanged(domains, monkeypatch):
    """不给 ``--live-model`` 时产出根一个字节不变 —— 这是 T128 的不回归判据。"""
    expect = SCRIPTED_ROOTS[(False, domains)]
    assert pathlib.Path(
        make_evidence.default_out_root(live=False, domains=domains)) == expect

    argv = ["--domains"] if domains else []
    seen = _resolved_out(argv, monkeypatch)
    assert pathlib.Path(seen["out"]) == expect
    assert seen["mode"] == make_evidence.MODE_SCRIPTED


@pytest.mark.parametrize("argv,domains", [
    ([], False),
    (["--domains"], True),
    (["--contrast"], False),          # 对照那条路同样隔离（只修一条入口 = 留一半洞）
])
def test_live_model_never_writes_into_a_scripted_root(argv, domains, monkeypatch):
    """``--live-model`` 的产出根必须在 ``evidence/live/`` 下，三条分支都是。

    从前三条分支都落 Scripted 束的根：一次 ``--live-model`` 就把八束确定性证据
    原地换成真模型产的，而 ``verify.py`` 不看 ``model_mode``，换完照样 10/10 PASS。
    产物上看不出来 —— 那正是铁律 3 要挡的那种「证据不真实」。
    """
    live_root = pathlib.Path(make_evidence.default_out_root(live=True, domains=domains))
    assert live_root not in SCRIPTED_ROOTS.values(), "真模型束落进了 Scripted 束的根"
    assert live_root.parts[-2 if domains else -1] == make_evidence.LIVE_SUBDIR

    seen = _resolved_out([*argv, "--live-model"], monkeypatch)
    assert pathlib.Path(seen["out"]) == live_root
    assert seen["mode"] == make_evidence.MODE_LIVE, (
        "标签与实跑必须同源：这一跑标 live，产出根也得是 live 的那个")


def _index(path, mode):
    path.write_text(
        make_evidence.header_line("abc") + "\n"
        + json.dumps({"git_sha": "abc", "model_mode": mode}, ensure_ascii=False),
        encoding="utf-8")


def test_explicit_out_cannot_overwrite_the_other_mode(tmp_path):
    """显式 ``--out`` 指进另一种模式的根 = 覆盖，当场拦下。

    ``default_out_root`` 分开的只是**缺省**；``--out evidence/ --live-model``
    仍能把真模型束指进 Scripted 束的根。两个方向都拦：反过来用 Scripted 覆盖真模型
    束，丢的是重跑不回来的东西（每跑一次说的话都不一样，且烧的是钱）。
    """
    _index(tmp_path / "INDEX.json", make_evidence.MODE_SCRIPTED)
    with pytest.raises(make_evidence.EvidenceError) as exc:
        make_evidence.assert_root_mode(str(tmp_path), make_evidence.MODE_LIVE)
    assert "不许互相覆盖" in str(exc.value)

    _index(tmp_path / "INDEX.json", make_evidence.MODE_LIVE)
    with pytest.raises(make_evidence.EvidenceError):
        make_evidence.assert_root_mode(str(tmp_path), make_evidence.MODE_SCRIPTED)

    # 同模式、空目录、读不出的索引都放行：本项守的是覆盖，不是证据格式。
    make_evidence.assert_root_mode(str(tmp_path), make_evidence.MODE_LIVE)
    make_evidence.assert_root_mode(str(tmp_path / "nope"), make_evidence.MODE_LIVE)
    (tmp_path / "INDEX.json").write_text("不是 json\n", encoding="utf-8")
    make_evidence.assert_root_mode(str(tmp_path), make_evidence.MODE_LIVE)


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
    assert chk.status == verify.SKIP and chk.total == 0


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


# ===========================================================================
# 9. verify.py：真模型束不进那十项的分子（T128）
# ===========================================================================
#: `bundle_dir` 那套文件的出处 sha。根 INDEX.json 得跟它对上，否则
#: `load_evidence_json(expect_sha=...)` 会先一步判「出处对不上」，测的就不是本节的事了。
FIXTURE_SHA = "deadbeef"


def _root_index(root, produced, *, mode="scripted"):
    """写一份根 `INDEX.json`：`produced[]` 逐束标 `model_mode`，形状同 make_evidence。"""
    (root / "INDEX.json").write_text(
        make_evidence.header_line(FIXTURE_SHA) + "\n" + json.dumps({
            "git_sha": FIXTURE_SHA, "model_mode": mode,
            "produced": [{"scenario": name, "dir": f"evidence/{name}", "model_mode": m}
                         for name, m in produced],
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def test_live_scenario_bundle_is_skipped_not_verified(evidence_root):
    """同一个根里混着一束真模型束时，只核 Scripted 的那束，真模型束点名跳过。

    从前 `verify.py` 根本不看 `model_mode`：`make_evidence.py --live-model` 产的束
    落在同一个 `evidence/scenario-*`，覆盖掉 Scripted 束照样 10/10 PASS。
    """
    shutil.copytree(evidence_root / "scenario-1", evidence_root / "scenario-2")
    _root_index(evidence_root, [("scenario-1", "scripted"), ("scenario-2", "live")])

    cases = verify.load_cases(str(evidence_root), None)
    try:
        assert [c.name for c in cases] == ["scenario-1"], "真模型束被算进了核验对象"
    finally:
        for c in cases:
            c.conn.close()
    assert verify.skipped_live_dirs(str(evidence_root)) == ["scenario-2"], (
        "跳过的束必须点名 —— 静默跳过与「核过了」在屏幕上长得一模一样")


def test_cli_names_the_live_bundle_it_did_not_verify(evidence_root):
    """屏幕上那行「未核验（真模型束…）」是这条口径对评委唯一可见的一面。"""
    shutil.copytree(evidence_root / "scenario-1", evidence_root / "scenario-2")
    _root_index(evidence_root, [("scenario-1", "scripted"), ("scenario-2", "live")])

    proc = _verify_cli(evidence_root)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "未核验（真模型束" in proc.stdout and "scenario-2" in proc.stdout.split(
        "未核验（真模型束")[1]


def test_an_all_live_root_is_refused_with_the_command_that_helps(evidence_root):
    """整根都是真模型束时明确报错，不是 `0/0 PASS`。

    分母为 0 的「全过」是这个核验器能犯的最坏的错（见 verify.py 文件头「空转也算
    没跑」）。报错正文里得有下一步动作，否则读的人只知道不行、不知道往哪走。
    """
    _root_index(evidence_root, [("scenario-1", "live")])
    with pytest.raises(verify.VerifyError) as exc:
        verify.load_cases(str(evidence_root), None)
    assert "真模型束" in str(exc.value) and "scripts/verify.py" in str(exc.value)


def test_live_root_declared_only_at_the_top_level_counts_too(evidence_root):
    """顶层标 live 就整根都是真模型束 —— `evidence/live/` 被直接 `--evidence` 指到的情形。"""
    _root_index(evidence_root, [("scenario-1", "scripted")], mode="live")
    assert verify.live_scenario_names(str(evidence_root)) == {"scenario-1"}
    with pytest.raises(verify.VerifyError):
        verify.load_cases(str(evidence_root), None)


def test_live_bundle_is_not_a_provenance_anchor(evidence_root):
    """第 9 项的分母里也不许有真模型束 —— 否则它是唯一一个把 live 算进分子的地方。

    出处判负要以「一条命令就能重跑」为前提（同 `_MANUAL_BUNDLES` 的理由）。真模型束
    红起来只能靠再烧一次真模型跑才消得掉，而一个消不掉的红灯等于噪音。
    """
    for name, mode in (("live", "live"), ("aux-scripted", "scripted")):
        sub = evidence_root / name
        sub.mkdir()
        (sub / "INDEX.json").write_text(
            make_evidence.header_line(FIXTURE_SHA) + "\n"
            + json.dumps({"git_sha": FIXTURE_SHA, "model_mode": mode}), encoding="utf-8")

    named = {name for name, _ in verify.provenance_anchors(str(evidence_root))}
    assert "aux-scripted" in named, "Scripted 的旁束照旧要查出处"
    assert "live" not in named, "真模型束进了第 9 项的分母"
    assert "live" in verify.skipped_live_dirs(str(evidence_root))


def test_a_bundle_named_live_is_caught_even_without_an_index(tmp_path):
    """`<路径>-live/` 这个目录名口径（T114）照旧认 —— 索引丢了也还剩名字。"""
    (tmp_path / "happy-live").mkdir()
    assert verify.is_live_bundle(str(tmp_path / "happy-live")) is True
    (tmp_path / "happy").mkdir()
    assert verify.is_live_bundle(str(tmp_path / "happy")) is False
