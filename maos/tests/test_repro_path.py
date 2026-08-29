"""复现路径的行为契约 —— 「没核到」这件事必须在屏幕上看得见。

三件事，各自的判准（派单 Y-3）：

1. **缺库提示要指向能解决它的那条命令**。``scenario-R5`` 不由 ``make_evidence.py``
   的场景循环产（那一圈按 ``maos.main.ALL_SCENARIOS`` 跑 1-7），对着它印
   「先跑 make_evidence.py」，照做的人会拿到一模一样的报错再撞一次 —— 原地打转，
   而错误信息不告诉他往哪走。
2. **第 7 项判据放宽到「本库晋升的」**，放行外部导入的历史知识（导入的知识按定义
   没有本库记录，给它造一条就是伪造证据，铁律 3）；但「一条都回查不到」**仍判负** ——
   放宽是为了放行导入，不是把判负改成判过。
3. **分母为 0 不许判 PASS**。``0/0 PASS`` 与「真跑了且全过」在屏幕上长得一模一样：
   只跑了一半命令的人拿到一屏满分，而 RAG 两项守卫一次都没执行。

第 4 组守的是 ``--out``：``write_evidence(None)`` 缺省写进仓库 ``evidence/``，
``make_evidence.py --out <仓库外>`` 却偷偷改仓库，是最难发现的一种污染。
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sqlite3
import subprocess
import sys
import types

import pytest

from maos import kb
from maos.core.store import SqliteStore
from maos.domain.refund import objects

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _load_script(name: str) -> types.ModuleType:
    """``scripts/`` 不是包，只能按路径加载（idiom 同 test_trace_evidence）。"""
    key = f"_y3_{name}"
    spec = importlib.util.spec_from_file_location(key, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


make_evidence = _load_script("make_evidence")
verify = _load_script("verify")

TENANT = "tnt-y3"
#: 本库晋升出来的知识：source_case_id 在 refund_case 里有行。
PROMOTED = {"doc_id": "kb-y3-local-0001", "source_case_id": "case-y3-local"}
#: 外部导入的知识：本库没有它的 case 行 —— 这正是原判据挡住的那一类。
IMPORTED = {"doc_id": "kb-y3-import-0001", "source_case_id": "case-from-another-system"}


def _insert_case(conn: sqlite3.Connection, case_id: str, biz_status: str) -> None:
    conn.execute(
        "INSERT INTO refund_case (tenant_id, case_id, channel_id, order_id, order_version,"
        " sku, reason_code, amount_claimed, biz_status, plan_id, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (TENANT, case_id, "ch-1", "ord-1", 1, "sku-1", "quality", 10.0,
         biz_status, "plan-1", "2026-08-29T00:00:00+00:00"))


def _insert_doc(conn: sqlite3.Connection, doc: dict) -> None:
    conn.execute(
        "INSERT INTO kb_doc (tenant_id, doc_id, kind, title, body, outcome,"
        " source_case_id, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (TENANT, doc["doc_id"], doc.get("kind", "history_case"), "t", "b",
         doc.get("outcome", "success"), doc.get("source_case_id"),
         "2026-08-29T00:00:00+00:00"))


def _build_db(path: pathlib.Path, *, docs=(), refund_cases=(), retrieved=(),
              kb_layer: bool = True) -> None:
    store = SqliteStore(str(path))
    store.init_schema()
    if kb_layer:
        kb.ensure_schema(store)
        objects.ensure_schema(store)
    for detail in retrieved:
        store.append_event_log({"plan_id": "plan-1", "trace_id": "trace-1",
                                "event_type": "KbRetrieved", "detail": detail})
    conn = sqlite3.connect(str(path))
    try:
        for case_id, biz_status in refund_cases:
            _insert_case(conn, case_id, biz_status)
        for doc in docs:
            _insert_doc(conn, doc)
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def make_case(tmp_path):
    """造一个 ``verify.Case``。第 5、7 项只读 conn / tables / name，trace 与 result 用不上。"""
    opened = []

    def _make(name: str, **kw) -> "verify.Case":
        db = tmp_path / f"{name}.db"
        _build_db(db, **kw)
        conn = verify.connect_ro(str(db))
        opened.append(conn)
        return verify.Case(name=name, directory=str(tmp_path), db_path=str(db), conn=conn,
                           tables=verify.table_names(conn), trace={}, result={})

    yield _make
    for c in opened:
        c.close()


# ===========================================================================
# 1. 缺库提示：指向一条**能产出它**的命令
# ===========================================================================
@pytest.mark.parametrize("scenario_dir, command", [
    ("scenario-R5", "python3 -m maos.kb.experiment"),
    ("scenario-1", "python3 scripts/make_evidence.py"),
    ("scenario-7", "python3 scripts/make_evidence.py"),
])
def test_missing_db_hint_branches_on_the_directory_name(scenario_dir, command):
    assert verify.missing_db_hint(f"/anywhere/evidence/{scenario_dir}/maos.db") == command


@pytest.mark.parametrize("scenario_dir, command", [
    ("scenario-R5", "python3 -m maos.kb.experiment"),
    ("scenario-1", "python3 scripts/make_evidence.py"),
])
def test_connect_ro_names_the_command_that_can_produce_that_db(tmp_path, scenario_dir, command):
    with pytest.raises(verify.VerifyError) as excinfo:
        verify.connect_ro(str(tmp_path / scenario_dir / "maos.db"))
    message = str(excinfo.value)
    assert "缺数据库" in message and f"先跑 {command}" in message


def _verify_cli(evidence_root: pathlib.Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "verify.py"), "--evidence", str(evidence_root)],
        capture_output=True, text=True, cwd=str(ROOT))


def test_cli_missing_r5_db_stops_the_merry_go_round(tmp_path):
    """R5 目录在、库不在 —— 评委撞的那一脚。从前的提示指向一条解决不了它的命令。"""
    (tmp_path / "scenario-R5").mkdir()
    proc = _verify_cli(tmp_path)
    assert proc.returncode != 0
    assert "缺数据库" in proc.stderr and "python3 -m maos.kb.experiment" in proc.stderr
    assert "先跑 python3 scripts/make_evidence.py" not in proc.stderr


def test_cli_missing_scenario_db_still_points_at_make_evidence(tmp_path):
    (tmp_path / "scenario-1").mkdir()
    proc = _verify_cli(tmp_path)
    assert proc.returncode != 0
    assert "缺数据库" in proc.stderr and "先跑 python3 scripts/make_evidence.py" in proc.stderr


# ===========================================================================
# 2. 第 7 项：放宽到「本库晋升的」，但不许退化成空转
# ===========================================================================
def test_7_imported_knowledge_no_longer_blocks_the_bundle(make_case):
    """导入的历史知识与本库晋升的同库共存：判据只覆盖后者，前者记一笔不判负。"""
    case = make_case("scenario-R5", docs=[PROMOTED, IMPORTED],
                     refund_cases=[(PROMOTED["source_case_id"], "settled")])
    chk = verify.check_history_case([case])
    assert chk.status == verify.PASS
    assert (chk.passed, chk.total) == (1, 1), "导入的那条不进分母，晋升的那条要进"
    assert any("外部导入" in n for n in chk.notes)


def test_7_locally_promoted_must_still_trace_to_a_settled_case(make_case):
    """本库有这条 case，却没走到 settled —— 放宽不碰这一路，照判负。"""
    case = make_case("scenario-R5", docs=[PROMOTED],
                     refund_cases=[(PROMOTED["source_case_id"], "processing")])
    chk = verify.check_history_case([case])
    assert chk.status == verify.FAIL
    assert any("追不到成功收口" in n for n in chk.notes)


def test_7_all_untraceable_still_fails(make_case):
    """「一条都回查不到」仍判负 —— 否则放宽之后这一项永远 0/0 过，正是要修的空转。"""
    case = make_case("scenario-R5", docs=[IMPORTED], refund_cases=[])
    chk = verify.check_history_case([case])
    assert chk.status == verify.FAIL
    assert chk.total > 0, "判负要落在分母上，不许是 0/0"
    assert any("退化成空转" in n for n in chk.notes)


def test_7_history_case_without_source_case_id_still_fails(make_case):
    case = make_case("scenario-R5", docs=[{"doc_id": "kb-y3-orphan", "source_case_id": None}])
    chk = verify.check_history_case([case])
    assert chk.status == verify.FAIL
    assert any("没有 source_case_id" in n for n in chk.notes)


def test_7_denominator_counts_only_promoted_docs(make_case):
    """分母 = 本库晋升的条数。加多少条导入知识都不许把它撑大或压小。"""
    imported = [{"doc_id": f"kb-y3-import-{i:04d}", "source_case_id": f"outside-{i}"}
                for i in range(24)]
    case = make_case("scenario-R5", docs=[PROMOTED, *imported],
                     refund_cases=[(PROMOTED["source_case_id"], "settled")])
    chk = verify.check_history_case([case])
    assert (chk.passed, chk.total) == (1, 1)


# ===========================================================================
# 3. 空转：分母为 0 不许判 PASS
# ===========================================================================
def test_5_and_7_do_not_report_pass_when_nothing_was_checked(make_case):
    """建了 kb 表但一条 RAG 素材都没有 —— 从前印 `0/0 PASS`，跟满分一模一样。"""
    case = make_case("scenario-1")
    for chk in (verify.check_kb_hit([case]), verify.check_history_case([case])):
        assert chk.status != verify.PASS, "空转不许印成 PASS"
        assert chk.status == verify.SKIP and chk.total == 0
        assert "空转" in chk.skip_reason


def test_idle_reason_points_at_the_bundle_that_carries_the_rag_evidence(make_case):
    case = make_case("scenario-3")
    chk = verify.check_kb_hit([case])
    assert "scenario-R5" in chk.skip_reason and "python3 -m maos.kb.experiment" in chk.skip_reason


def test_idle_reason_drops_the_r5_pointer_when_r5_is_already_there(make_case):
    """R5 在场却仍然空转 —— 那不是「少跑了一条命令」，别把人往错的方向指。"""
    case = make_case("scenario-R5")
    chk = verify.check_kb_hit([case])
    assert chk.status == verify.SKIP
    assert "scenario-R5" not in chk.skip_reason


def test_kb_hit_still_passes_when_it_actually_checked_something(make_case):
    """空转判据不许误伤真跑过的那一路。"""
    case = make_case("scenario-R5", docs=[dict(PROMOTED, kind="policy")],
                     retrieved=[{"docs": [{"doc_id": PROMOTED["doc_id"], "score": 1.0}]}])
    chk = verify.check_kb_hit([case])
    assert chk.status == verify.PASS and (chk.passed, chk.total) == (1, 1)


def test_idle_checks_are_excluded_from_the_numerator(capsys):
    """空转项不进 `RESULT: n/7` 的分子，且屏幕上与「真跑了且全过」长得不一样。"""
    idle = verify.Check("kb-hit", "空转的那一项")
    verify._idle_skip(idle, [], "证据束里没有一条 KbRetrieved 事件")
    real = verify.Check("business-outcome", "真跑过的那一项")
    real.ok()

    code = verify.render([real, idle], [], as_json=False)
    out = capsys.readouterr().out
    assert "RESULT: 1/1 PASS" in out, "分子分母都不许含空转项"
    assert "1 SKIP" in out and "kb-hit" in out and "不计入分子" in out
    assert "0/0" not in out, "0/0 正是要消灭的那个形态"
    assert code == 0


# ===========================================================================
# 4. --out 必须透传：不许偷偷写回仓库 evidence/
# ===========================================================================
def _fake_write_evidence(recorder: list):
    def fake(out_root=None):
        recorder.append(out_root)
        final = pathlib.Path(out_root) / "scenario-R5"
        final.mkdir(parents=True, exist_ok=True)
        (final / "trace.json").write_text(
            make_evidence.HEADER_PREFIX + "2026-08-29T00:00:00+00:00 from deadbee\n"
            + json.dumps({"summary": {"span_count": 3, "event_count": 5,
                                      "unsourced_artifacts": 0, "stray_event_count": 0,
                                      "tree_errors": []}}),
            encoding="utf-8")
        return str(final)
    return fake


def test_make_evidence_passes_out_through_to_write_evidence(tmp_path, monkeypatch):
    """``write_evidence(None)`` 缺省写仓库 evidence/ —— ``--out`` 时必须把目录传进去。"""
    recorder: list = []
    monkeypatch.setattr("maos.kb.experiment.write_evidence", _fake_write_evidence(recorder))

    code = make_evidence.main(["--out", str(tmp_path), "--scenarios", "99", "--r5"])

    assert code == 0
    assert recorder == [str(tmp_path)], "out_root 没透传（None 会写回仓库 evidence/）"
    index = make_evidence.load_evidence_json(str(tmp_path / "INDEX.json"))
    assert "R5" in index["requested"]
    assert [p["scenario"] for p in index["produced"]] == ["R5"]


def test_scenarios_flag_does_not_drag_r5_along(tmp_path, monkeypatch):
    """奔着某几场去的时候不拖上 R5；那时第 5、7 项判 SKIP，看得出没跑。"""
    recorder: list = []
    monkeypatch.setattr("maos.kb.experiment.write_evidence", _fake_write_evidence(recorder))

    make_evidence.main(["--out", str(tmp_path), "--scenarios", "99"])

    assert recorder == []
    assert not (tmp_path / "scenario-R5").exists()


def test_no_r5_flag_wins_over_the_full_run_default(tmp_path, monkeypatch):
    recorder: list = []
    monkeypatch.setattr("maos.kb.experiment.write_evidence", _fake_write_evidence(recorder))
    monkeypatch.setattr(make_evidence, "build_scenario",
                        lambda n, out, **kw: make_evidence._bundle_info(
                            n, out, {"summary": {"span_count": 0, "event_count": 0,
                                                 "unsourced_artifacts": 0,
                                                 "stray_event_count": 0, "tree_errors": []}}))

    make_evidence.main(["--out", str(tmp_path), "--no-r5"])

    assert recorder == []


def test_build_r5_lands_a_full_bundle_under_out(tmp_path):
    """真跑一次 R5：核验器把 scenario-R5 当一个 case 读，缺一个文件就连核验都开始不了。"""
    info = make_evidence.build_r5(str(tmp_path), secrets={})

    assert info["scenario"] == "R5" and info["span_count"] > 0
    bundle = tmp_path / "scenario-R5"
    for name in ("maos.db", "trace.json", "result.json", "run.log",
                 "business-objects.json", "kb-hits.json", "kb-dump.json"):
        assert (bundle / name).exists(), f"缺 {name}"


def test_build_r5_reports_the_failure_instead_of_leaving_half_a_bundle(tmp_path, monkeypatch):
    def boom(out_root=None):
        raise RuntimeError("R5 炸了")

    monkeypatch.setattr("maos.kb.experiment.write_evidence", boom)
    with pytest.raises(make_evidence.EvidenceError, match="scenario-R5 生成失败"):
        make_evidence.build_r5(str(tmp_path), secrets={})


# ===========================================================================
# 5. 复现路径本身：两条命令，且第二条认得第一条的产出
# ===========================================================================
def test_full_bundle_needs_only_make_evidence_then_verify(tmp_path, monkeypatch):
    """R5 进了 make_evidence 的缺省路径 —— 复现全量证据从三条命令收敛成两条。"""
    recorder: list = []
    monkeypatch.setattr("maos.kb.experiment.write_evidence", _fake_write_evidence(recorder))
    monkeypatch.setattr(make_evidence, "build_scenario",
                        lambda n, out, **kw: make_evidence._bundle_info(
                            n, out, {"summary": {"span_count": 0, "event_count": 0,
                                                 "unsourced_artifacts": 0,
                                                 "stray_event_count": 0, "tree_errors": []}}))

    make_evidence.main(["--out", str(tmp_path)])

    from maos.main import ALL_SCENARIOS
    index = make_evidence.load_evidence_json(str(tmp_path / "INDEX.json"))
    assert index["requested"] == [*ALL_SCENARIOS, "R5"]
    assert recorder == [str(tmp_path)]
