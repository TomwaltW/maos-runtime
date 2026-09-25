"""T176 · verify 认 cs 家族与 ``--cs`` 可选核验（review/p14-cs-contracts.md §1.2 / §2 T176）。

正例一律是**真前台**跑出来的真库：``FrontDesk`` + ``evaluate.fixture_ports``（夹具端口）
跑三轮 —— 中文查单（有观察）、政策问答（有话术引用）、英文查单（有观察）。反例是在那份
真库的副本上**逐条篡改一行**，每条判据至少一个，证明它能判负。

测试侧可以用前台造数据；被测的 ``scripts/verify.py`` 只许 import cs 的 types / ports
（最后一条测试按 AST 钉住）。
"""

from __future__ import annotations

import ast
import dataclasses
import json
import os
import pathlib
import shutil
import sqlite3

import pytest

from maos.core.store import SqliteStore
from maos.domain.cs import evaluate
from maos.domain.cs.corpus import seed_cs_kb
from maos.domain.cs.desk import CsConfig, FrontDesk
from maos.domain.cs.types import text_digest
from maos.obs import trace as trace_mod
from scripts import verify

CLOCK_T176 = "2026-09-25T08:00:00+00:00"
TURNS_T176 = ("帮我看下 A1001 这单发货了没有", "退货运费谁出", "Has my order A1001 shipped?")
VERIFY_PATH_T176 = pathlib.Path(verify.__file__)

#: 基线（p13 收尾）时 export_trace_bundle 的顶层键与 summary 键，逐字抄下：没有 cs 行时一个都不许多。
BASE_TOP_KEYS_T176 = ("schema", "db", "plan_count", "traces", "roundtable_traces",
                      "stray_events", "unattributed_usage", "summary")


def _build_db_t176(path: pathlib.Path) -> pathlib.Path:
    store = SqliteStore(str(path))
    seed_cs_kb(store)
    cases = {c.id: c for c in evaluate.load_cases(evaluate.P13_EVAL_PATH)}
    case = dataclasses.replace(cases["CS13-001"], turns=TURNS_T176)
    desk = FrontDesk(store, CsConfig(tenants={"wk_eval": "tnt-demo"}, handoff_target=None),
                     clock=lambda: CLOCK_T176,
                     **evaluate.fixture_ports(case, tenant_id="tnt-demo"))
    routes = [desk.handle(evaluate.inbound_for(case, i, t)).route
              for i, t in enumerate(case.turns, start=1)]
    assert routes == ["answer", "answer", "answer"], routes
    return path


@pytest.fixture(scope="module")
def golden_db_t176(tmp_path_factory) -> pathlib.Path:
    return _build_db_t176(tmp_path_factory.mktemp("cs-t176") / "cs.db")


@pytest.fixture
def cs_db_t176(golden_db_t176, tmp_path) -> pathlib.Path:
    """每条测试一份独立副本，篡改只落在副本上。"""
    dst = tmp_path / "cs.db"
    shutil.copyfile(golden_db_t176, dst)
    return dst


def _exec_t176(db: pathlib.Path, sql: str, params=()) -> int:
    conn = sqlite3.connect(str(db))
    try:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def _one_t176(db: pathlib.Path, sql: str, params=()):
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


def _turn_t176(db: pathlib.Path, n: int) -> sqlite3.Row:
    return _one_t176(db, "SELECT * FROM cs_turn ORDER BY seq LIMIT 1 OFFSET ?", (n - 1,))


def _set_draft_t176(db: pathlib.Path, turn_id: str, draft: dict) -> None:
    assert _exec_t176(db, "UPDATE cs_turn SET draft_json=? WHERE turn_id=?",
                      (json.dumps(draft, ensure_ascii=False), turn_id)) == 1


def _check_t176(db: pathlib.Path) -> verify.Check:
    return verify.check_cs_claim_basis([str(db)])


def _marks_t176(chk: verify.Check) -> set[str]:
    return {n.split("[", 1)[1][0] for n in chk.notes if not n.startswith(("info:", "warn:"))
            and "[" in n}


def _assert_fails_t176(chk: verify.Check, mark: str, *, only: bool = True) -> None:
    assert chk.status == verify.FAIL, chk.notes
    marks = _marks_t176(chk)
    assert mark in marks, chk.notes
    if only:
        assert marks == {mark}, chk.notes


# ---------------------------------------------------------------------------
# 正例与 SKIP
# ---------------------------------------------------------------------------
def test_real_desk_db_passes_all_six_criteria_t176(cs_db_t176):
    chk = _check_t176(cs_db_t176)
    assert chk.key == verify.CS_CHECK_KEY == "cs/claim-basis"
    assert chk.status == verify.PASS, chk.notes
    # 3 轮 ×（判据 1 + 判据 2）+ 两条 obs claim ×（判据 3 + 判据 4）+ 一条引用（判据 5）
    # + 第 1 轮「已发货」一处（判据 6）= 12。判据全部真执行过，不是空转。
    assert (chk.passed, chk.total) == (12, 12)
    assert any(n.startswith("info:") for n in chk.notes)


def test_no_cs_tables_is_skip_t176(tmp_path):
    db = tmp_path / "plain.db"
    SqliteStore(str(db)).init_schema()
    chk = _check_t176(db)
    assert chk.status == verify.SKIP and "cs_" in chk.skip_reason


def test_empty_cs_turn_is_idle_skip_t176(cs_db_t176):
    _exec_t176(cs_db_t176, "DELETE FROM cs_turn")
    _exec_t176(cs_db_t176, "DELETE FROM event_log WHERE event_type='CsTurnRecorded'")
    chk = _check_t176(cs_db_t176)
    assert chk.status == verify.SKIP and "空转" in chk.skip_reason


# ---------------------------------------------------------------------------
# 反例：每条判据至少一个
# ---------------------------------------------------------------------------
def test_c1_missing_turn_recorded_fails_t176(cs_db_t176):
    turn = _turn_t176(cs_db_t176, 2)
    assert _exec_t176(cs_db_t176, "DELETE FROM event_log WHERE event_type='CsTurnRecorded'"
                                  " AND task_id=?", (turn["turn_id"],)) == 1
    _assert_fails_t176(_check_t176(cs_db_t176), "1")


def test_c1_duplicate_turn_recorded_fails_t176(cs_db_t176):
    turn = _turn_t176(cs_db_t176, 1)
    assert _exec_t176(
        cs_db_t176,
        "INSERT INTO event_log (event_id, trace_id, plan_id, task_id, event_type, from_state,"
        " to_state, reason, detail, created_at) SELECT 'dup-t176', trace_id, plan_id, task_id,"
        " event_type, from_state, to_state, reason, detail, created_at FROM event_log"
        " WHERE event_type='CsTurnRecorded' AND task_id=?", (turn["turn_id"],)) == 1
    _assert_fails_t176(_check_t176(cs_db_t176), "1")


def test_c1_orphan_turn_recorded_fails_t176(cs_db_t176):
    """反向：审计行指不回 cs_turn（业务表那一轮被删了）。"""
    turn = _turn_t176(cs_db_t176, 3)
    assert _exec_t176(cs_db_t176, "DELETE FROM cs_turn WHERE turn_id=?", (turn["turn_id"],)) == 1
    chk = _check_t176(cs_db_t176)
    _assert_fails_t176(chk, "1")
    assert any("审计行无主" in n for n in chk.notes), chk.notes


def test_c2_reply_text_edited_fails_t176(cs_db_t176):
    turn = _turn_t176(cs_db_t176, 2)
    assert _exec_t176(cs_db_t176, "UPDATE cs_turn SET reply_text=reply_text || '。' WHERE turn_id=?",
                      (turn["turn_id"],)) == 1
    _assert_fails_t176(_check_t176(cs_db_t176), "2")


def test_c3_dangling_observation_fails_t176(cs_db_t176):
    turn = _turn_t176(cs_db_t176, 1)
    draft = json.loads(turn["draft_json"])
    draft["claims"][0]["basis_ref"] = "obs:" + turn["turn_id"] + "-o09"
    _set_draft_t176(cs_db_t176, turn["turn_id"], draft)
    chk = _check_t176(cs_db_t176)
    _assert_fails_t176(chk, "3", only=False)
    # 悬空的 claim 撑不起「已发货」：判据 6 同时判负是正确的连带。
    assert _marks_t176(chk) == {"3", "6"}, chk.notes


def test_c3_other_turns_observation_fails_t176(cs_db_t176):
    """第 3 轮的 claim 改指第 1 轮的观察（措辞照旧逐字对）：上一轮的观察撑不起这一轮。"""
    t1, t3 = _turn_t176(cs_db_t176, 1), _turn_t176(cs_db_t176, 3)
    draft = json.loads(t3["draft_json"])
    draft["claims"][0]["basis_ref"] = json.loads(t1["draft_json"])["claims"][0]["basis_ref"]
    _set_draft_t176(cs_db_t176, t3["turn_id"], draft)
    chk = _check_t176(cs_db_t176)
    _assert_fails_t176(chk, "3")
    assert any("不是本轮" in n for n in chk.notes), chk.notes


def test_c4_observation_status_changed_fails_t176(cs_db_t176):
    turn = _turn_t176(cs_db_t176, 1)
    assert _exec_t176(cs_db_t176, "UPDATE cs_observation SET status='paid' WHERE turn_id=?",
                      (turn["turn_id"],)) == 1
    _assert_fails_t176(_check_t176(cs_db_t176), "4")


def test_c4_lang_switched_fails_t176(cs_db_t176):
    turn = _turn_t176(cs_db_t176, 1)
    assert _exec_t176(cs_db_t176, "UPDATE cs_turn_ext SET lang='en' WHERE turn_id=?",
                      (turn["turn_id"],)) == 1
    _assert_fails_t176(_check_t176(cs_db_t176), "4")


def test_c4_missing_ext_row_defaults_to_zh_t176(cs_db_t176):
    """英文那一轮的 cs_turn_ext 行没了 → 按 zh 核 → 英文措辞对不上。"""
    turn = _turn_t176(cs_db_t176, 3)
    assert _exec_t176(cs_db_t176, "DELETE FROM cs_turn_ext WHERE turn_id=?",
                      (turn["turn_id"],)) == 1
    _assert_fails_t176(_check_t176(cs_db_t176), "4")


def test_c4_literal_paraphrased_fails_t176(cs_db_t176):
    turn = _turn_t176(cs_db_t176, 3)
    draft = json.loads(turn["draft_json"])
    draft["claims"][0]["literal"] = "Your order has shipped."
    _set_draft_t176(cs_db_t176, turn["turn_id"], draft)
    _assert_fails_t176(_check_t176(cs_db_t176), "4")


def test_c5_uncited_citation_fails_t176(cs_db_t176):
    turn = _turn_t176(cs_db_t176, 2)
    draft = json.loads(turn["draft_json"])
    assert draft["citations"], draft
    draft["citations"] = ["kb-cs-tnt-demo-GEN-001"]
    _set_draft_t176(cs_db_t176, turn["turn_id"], draft)
    _assert_fails_t176(_check_t176(cs_db_t176), "5")


def test_c5_citation_retrieved_in_other_turn_fails_t176(cs_db_t176):
    """第 2 轮检出的话术挂到第 1 轮的 kb: claim 上：别的轮的命中不算。"""
    t1, t2 = _turn_t176(cs_db_t176, 1), _turn_t176(cs_db_t176, 2)
    doc = json.loads(t2["draft_json"])["citations"][0]
    draft = json.loads(t1["draft_json"])
    draft["claims"].append({"literal": "您的订单", "basis_ref": "kb:" + doc})
    _set_draft_t176(cs_db_t176, t1["turn_id"], draft)
    _assert_fails_t176(_check_t176(cs_db_t176), "5")


def test_c6_unbacked_status_word_fails_t176(cs_db_t176):
    """政策那一轮的回复多了一句「已退款」，审计摘要同步改（判据 2 照旧对得上），只剩判据 6 能抓。"""
    turn = _turn_t176(cs_db_t176, 2)
    text = turn["reply_text"] + "您的款项已退款。"
    assert _exec_t176(cs_db_t176, "UPDATE cs_turn SET reply_text=? WHERE turn_id=?",
                      (text, turn["turn_id"])) == 1
    row = _one_t176(cs_db_t176, "SELECT seq, detail FROM event_log WHERE event_type="
                                "'CsTurnRecorded' AND task_id=?", (turn["turn_id"],))
    detail = json.loads(row["detail"])
    detail["reply_digest"] = text_digest(text)
    _exec_t176(cs_db_t176, "UPDATE event_log SET detail=? WHERE seq=?",
               (json.dumps(detail, ensure_ascii=False), row["seq"]))
    _assert_fails_t176(_check_t176(cs_db_t176), "6")


def test_c6_kb_claim_cannot_back_status_t176(cs_db_t176):
    """状态词只认 obs: claim：拿一条 kb:（本轮确实检出过）去「覆盖」已退款，照样判负。"""
    turn = _turn_t176(cs_db_t176, 2)
    text = turn["reply_text"] + "已退款"
    draft = json.loads(turn["draft_json"])
    draft["claims"].append({"literal": "已退款", "basis_ref": "kb:" + draft["citations"][0]})
    _set_draft_t176(cs_db_t176, turn["turn_id"], draft)
    _exec_t176(cs_db_t176, "UPDATE cs_turn SET reply_text=? WHERE turn_id=?",
               (text, turn["turn_id"]))
    row = _one_t176(cs_db_t176, "SELECT seq, detail FROM event_log WHERE event_type="
                                "'CsTurnRecorded' AND task_id=?", (turn["turn_id"],))
    detail = json.loads(row["detail"])
    detail["reply_digest"] = text_digest(text)
    _exec_t176(cs_db_t176, "UPDATE event_log SET detail=? WHERE seq=?",
               (json.dumps(detail, ensure_ascii=False), row["seq"]))
    _assert_fails_t176(_check_t176(cs_db_t176), "6")


def _set_reply_t176(db: pathlib.Path, turn_id: str, text: str) -> None:
    """改 reply_text 并把审计摘要同步改掉（判据 2 照旧对得上，只让被测判据说话）。"""
    assert _exec_t176(db, "UPDATE cs_turn SET reply_text=? WHERE turn_id=?", (text, turn_id)) == 1
    row = _one_t176(db, "SELECT seq, detail FROM event_log WHERE event_type="
                        "'CsTurnRecorded' AND task_id=?", (turn_id,))
    detail = json.loads(row["detail"])
    detail["reply_digest"] = text_digest(text)
    _exec_t176(db, "UPDATE event_log SET detail=? WHERE seq=?",
               (json.dumps(detail, ensure_ascii=False), row["seq"]))


def test_c6_every_occurrence_of_literal_counts_t176(cs_db_t176):
    """正例（复核 L2-2）：同一条有效 obs: claim 的 literal 在正文里出现两次，两处状态词都算覆盖。"""
    turn = _turn_t176(cs_db_t176, 1)
    literal = json.loads(turn["draft_json"])["claims"][0]["literal"]
    assert literal and literal in turn["reply_text"]
    _set_reply_t176(cs_db_t176, turn["turn_id"], turn["reply_text"] + literal)
    chk = _check_t176(cs_db_t176)
    assert chk.status == verify.PASS, chk.notes
    assert (chk.passed, chk.total) == (13, 13), chk.notes      # 判据 6 从一处变两处


def test_c1_drop_cs_turn_table_fails_not_skip_t176(cs_db_t176):
    """反例（复核 L3-1）：整张 cs_turn 被删，别的 cs_ 表与 CsTurnRecorded 还在 —— 判负，不许 SKIP。"""
    _exec_t176(cs_db_t176, "DROP TABLE cs_turn")
    chk = _check_t176(cs_db_t176)
    _assert_fails_t176(chk, "1")
    assert sum("审计行无主" in n for n in chk.notes) == 3, chk.notes


def test_audit_rows_without_any_cs_table_fail_t176(tmp_path):
    """反例（复核 L3-1）：库里一张 cs_ 表都没有，却有 cs: 的 CsTurnRecorded —— 审计行无主，判负。"""
    db = tmp_path / "orphan.db"
    store = SqliteStore(str(db))
    store.init_schema()
    store.append_event_log({"event_id": "", "trace_id": "", "plan_id": "cs:csc-x",
                            "task_id": "t1", "event_type": "CsTurnRecorded", "from_state": None,
                            "to_state": None, "reason": "", "detail": {}})
    _assert_fails_t176(_check_t176(db), "1")


# ---------------------------------------------------------------------------
# CLI：--cs 追加、缺省不跑
# ---------------------------------------------------------------------------
def test_checks_stay_ten_and_cs_not_in_default_t176():
    assert len(verify.CHECKS) == 10
    assert verify.check_cs_claim_basis not in verify.CHECKS


def _run_main_t176(monkeypatch, capsys, argv):
    monkeypatch.setattr(verify, "load_cases", lambda evidence, db: [])
    monkeypatch.setattr(verify, "skipped_live_dirs", lambda evidence: [])
    monkeypatch.setattr(verify, "CHECKS", [])          # 只看 --cs 那一项的去留与计分
    captured = []
    real = verify.check_cs_claim_basis
    monkeypatch.setattr(verify, "check_cs_claim_basis",
                        lambda paths: captured.append(list(paths)) or real(paths))
    code = verify.main(argv)
    return code, capsys.readouterr().out, captured


def test_cli_default_does_not_run_cs_t176(monkeypatch, capsys, cs_db_t176):
    code, out, captured = _run_main_t176(monkeypatch, capsys, ["--db", str(cs_db_t176)])
    assert captured == [] and "cs/claim-basis" not in out, out


def test_cli_cs_flag_appends_scored_item_from_db_t176(monkeypatch, capsys, cs_db_t176):
    code, out, captured = _run_main_t176(monkeypatch, capsys, ["--cs", "--db", str(cs_db_t176)])
    assert captured == [[str(cs_db_t176)]]
    assert "[PASS] cs/claim-basis" in out and "RESULT: 1/1 PASS" in out, out
    assert code == 0


def test_cli_cs_flag_fail_is_scored_t176(monkeypatch, capsys, cs_db_t176):
    _exec_t176(cs_db_t176, "UPDATE cs_observation SET status='cancelled'")
    code, out, _ = _run_main_t176(monkeypatch, capsys, ["--cs", "--db", str(cs_db_t176)])
    assert "[FAIL] cs/claim-basis" in out and code == 1, out


def test_cs_db_paths_prefers_db_file_else_bundle_dbs_t176(cs_db_t176, tmp_path):
    other = tmp_path / "x" / "maos.db"
    fake = [verify.Case(name=n, directory="", db_path=p, conn=None, tables=set(), trace={},
                        result={}) for n, p in (("a", str(other)), ("b", str(other)))]
    assert verify.cs_db_paths(fake, str(cs_db_t176)) == [str(cs_db_t176)]
    assert verify.cs_db_paths(fake, None) == [str(other)]            # 同一个库只核一次


# ---------------------------------------------------------------------------
# trace.py：cs_traces
# ---------------------------------------------------------------------------
def _cs_event_seqs_t176(db) -> set[int]:
    conn = sqlite3.connect(str(db))
    try:
        return {r[0] for r in conn.execute(
            "SELECT seq FROM event_log WHERE substr(plan_id, 1, 3) = 'cs:'")}
    finally:
        conn.close()


def test_cs_rows_go_to_cs_traces_not_stray_t176(cs_db_t176):
    doc = trace_mod.export_trace_bundle(str(cs_db_t176))
    keys = list(doc)
    assert keys.index("cs_traces") == keys.index("roundtable_traces") + 1
    trees = doc["cs_traces"]
    assert trees and all(t["plan_id"].startswith("cs:") and t["trace_id"] == "" for t in trees)
    assert all({"plan_id", "events", "model_usage"} <= set(t) for t in trees)
    in_tree = [e["seq"] for t in trees for e in t["events"]]
    assert sorted(in_tree) == sorted(_cs_event_seqs_t176(cs_db_t176))
    assert not any((s.get("plan_id") or "").startswith("cs:") for s in doc["stray_events"])
    assert doc["summary"]["cs_event_count"] == len(in_tree)
    assert all(t["summary"]["tree_errors"] == [] for t in trees)
    assert doc["summary"]["tree_errors"] == []


def test_no_cs_rows_leaves_bundle_shape_untouched_t176(tmp_path):
    db = tmp_path / "plain.db"
    store = SqliteStore(str(db))
    store.init_schema()
    store.append_event_log({"event_id": "", "trace_id": "", "plan_id": "", "task_id": None,
                            "event_type": "SkillInvoked", "from_state": None, "to_state": None,
                            "reason": "", "detail": {"skill": "x"}})
    doc = trace_mod.export_trace_bundle(str(db))
    assert tuple(doc) == BASE_TOP_KEYS_T176
    assert not any(k.startswith("cs_") for k in doc["summary"])


def test_uppercase_prefix_is_not_cs_family_t176(tmp_path):
    db = tmp_path / "upper.db"
    store = SqliteStore(str(db))
    store.init_schema()
    store.append_event_log({"event_id": "", "trace_id": "", "plan_id": "CS:csc-x",
                            "task_id": "t", "event_type": "CsTurnRecorded", "from_state": None,
                            "to_state": None, "reason": "", "detail": {}})
    doc = trace_mod.export_trace_bundle(str(db))
    assert "cs_traces" not in doc
    assert [s["plan_id"] for s in doc["stray_events"]] == ["CS:csc-x"]


def _add_cs_usage_t176(db, plan_id: str) -> int:
    conn = sqlite3.connect(str(db))
    try:
        cur = conn.execute(
            "INSERT INTO model_usage (trace_id, plan_id, task_id, agent_role, call_site, model,"
            " tier, tokens_in, tokens_out, latency_ms, estimated, created_at)"
            " VALUES ('', ?, NULL, 'cs_front_desk', 'cs.understand', 'scripted-light', 'light',"
            " 3, 4, 0, 1, ?)", (plan_id, CLOCK_T176))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _case_t176(db) -> verify.Case:
    conn = verify.connect_ro(str(db))
    doc = json.loads(json.dumps(trace_mod.export_trace_bundle(str(db)), ensure_ascii=False))
    return verify.Case(name="cs-t176", directory=os.path.dirname(str(db)), db_path=str(db),
                       conn=conn, tables=verify.table_names(conn), trace=doc, result={})


def test_item8_cs_usage_is_attributed_t176(cs_db_t176):
    plan_id = "cs:" + _turn_t176(cs_db_t176, 1)["conversation_id"]
    seq = _add_cs_usage_t176(cs_db_t176, plan_id)
    case = _case_t176(cs_db_t176)
    try:
        assert seq in {r["seq"] for t in case.trace["cs_traces"] for r in t["model_usage"]}
        assert seq not in {r["seq"] for r in case.trace["unattributed_usage"]}
        assert case.trace["summary"]["cs_model_calls"] == 1
        chk = verify.check_cost_attribution([case])
        assert chk.status == verify.PASS, chk.notes
        # 反例：cs 树里抹掉这行、也不列进 unattributed —— 第 8 项判负。
        for t in case.trace["cs_traces"]:
            t["model_usage"] = [r for r in t["model_usage"] if r["seq"] != seq]
        chk = verify.check_cost_attribution([case])
        assert chk.status == verify.FAIL, chk.notes
    finally:
        case.conn.close()


def test_item4_cs_rows_exactly_once_t176(cs_db_t176):
    case = _case_t176(cs_db_t176)
    try:
        chk = verify.check_trace_tree([case])
        assert chk.status == verify.PASS, chk.notes
        # 只核「恰好一处」这一条：从 cs 树里摘掉一条事件、又不列进 stray —— 判负。
        tree = case.trace["cs_traces"][0]
        victim = next(s for s in tree["spans"] if s["kind"] == trace_mod.KIND_EVENT)
        tree["spans"].remove(victim)
        chk = verify.Check("trace-tree", "")
        verify._check_cs_trees(chk, case)
        assert chk.status == verify.FAIL
        assert any("哪儿都找不到" in n for n in chk.notes), chk.notes
        # 两头都在也判负。
        tree["spans"].append(victim)
        case.trace["stray_events"].append({"seq": victim["attributes"]["maos.event.seq"]})
        chk = verify.Check("trace-tree", "")
        verify._check_cs_trees(chk, case)
        assert chk.status == verify.FAIL
        assert any("被数了两次" in n for n in chk.notes), chk.notes
    finally:
        case.conn.close()


def _cs_tree_notes_t176(case: verify.Case) -> list[str]:
    chk = verify.Check("trace-tree", "")
    verify._check_cs_trees(chk, case)
    assert chk.status == verify.FAIL, chk.notes
    return chk.notes


def test_item4_cs_tree_orphan_span_fails_t176(cs_db_t176):
    """复核 L2-1(a)：cs 树里一条 span 的 parent 指到树外 —— 第 4 项报孤儿（按 cs= 那一族报）。"""
    case = _case_t176(cs_db_t176)
    try:
        tree = case.trace["cs_traces"][0]
        victim = next(s for s in tree["spans"] if s["kind"] == trace_mod.KIND_EVENT)
        victim["parent_span_id"] = "nope-t176"
        errs = trace_mod.check_span_tree(tree["spans"])
        assert any("孤儿" in e for e in errs), errs
        chk = verify.check_trace_tree([case])
        assert chk.status == verify.FAIL
        for e in errs:
            assert f"{case.name} cs={tree['plan_id']}: {e}" in chk.notes, chk.notes
    finally:
        case.conn.close()


def test_item4_cs_tree_event_count_is_reconciled_with_db_t176(cs_db_t176):
    """复核 L2-1(b)：cs 树自报的 event_count 多报一条 —— 回库数对不上，判负。"""
    case = _case_t176(cs_db_t176)
    try:
        case.trace["cs_traces"][0]["summary"]["event_count"] += 1
        assert any("数对不上" in n for n in _cs_tree_notes_t176(case))
    finally:
        case.conn.close()


def test_item4_cs_usage_counted_twice_fails_t176(cs_db_t176):
    """复核 L2-1(c)：一条 cs 用量行既在 cs 树的 model_usage、又列进 unattributed_usage —— 判负。"""
    seq = _add_cs_usage_t176(cs_db_t176, "cs:" + _turn_t176(cs_db_t176, 1)["conversation_id"])
    case = _case_t176(cs_db_t176)
    try:
        chk = verify.Check("trace-tree", "")
        verify._check_cs_trees(chk, case)
        assert chk.status == verify.PASS, chk.notes
        case.trace["unattributed_usage"].append({"seq": seq})
        assert any("既算进 cs 树的 cost" in n for n in _cs_tree_notes_t176(case))
        # 两头都不在也判负。
        case.trace["unattributed_usage"].pop()
        for t in case.trace["cs_traces"]:
            t["model_usage"] = [r for r in t["model_usage"] if r["seq"] != seq]
        assert any("既没归进 cs 树" in n for n in _cs_tree_notes_t176(case))
    finally:
        case.conn.close()


def test_item4_cs_tree_with_foreign_event_fails_t176(cs_db_t176):
    """复核 L2-1(d)：cs 树里混进一条库里不是 cs 家族行的事件 —— 判负。"""
    case = _case_t176(cs_db_t176)
    try:
        tree = case.trace["cs_traces"][0]
        victim = next(s for s in tree["spans"] if s["kind"] == trace_mod.KIND_EVENT)
        foreign = json.loads(json.dumps(victim))
        seq = 10_000 + max(_cs_event_seqs_t176(cs_db_t176))
        foreign["span_id"] = "foreign-t176"
        foreign["attributes"]["maos.event.seq"] = seq
        tree["spans"].append(foreign)
        assert any(f"seq={seq}" in n and "不是 cs 家族行" in n for n in _cs_tree_notes_t176(case))
    finally:
        case.conn.close()


# ---------------------------------------------------------------------------
# 同源对账守卫：verify 只许 import cs 的 types / ports
# ---------------------------------------------------------------------------
def test_verify_imports_only_cs_types_and_ports_t176():
    tree = ast.parse(VERIFY_PATH_T176.read_text(encoding="utf-8"))
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
            if node.module == "maos.domain.cs":
                mods.update(f"maos.domain.cs.{a.name}" for a in node.names)
        elif isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
    cs_mods = {m for m in mods if m == "maos.domain.cs" or m.startswith("maos.domain.cs.")}
    assert cs_mods == {"maos.domain.cs.types", "maos.domain.cs.ports"}, cs_mods
