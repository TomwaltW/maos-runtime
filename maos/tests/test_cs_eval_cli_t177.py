"""T177 · 评测批量化 scripts/cs_eval.py（review/p14-cs-contracts.md §2「T177」）。

钉住的是 CLI 的**形状**，不钉留出集读数（读数由主会话在留出集测试里钉）：

* 退出码 0 / 1 / 2（全部 meets / 有集不达标 / 用法错）；holdout14 文件不存在 → SKIP、不报错；
* 只出聚合数与 id：留出集用自造的临时文件替换路径常量、塞哨兵句子，stdout / ``--json`` /
  ``--out`` 里一个哨兵都不出现，没对上的轮只以 ``<id>#<轮次>`` 出现；
* ``--out`` 首行 ``# generated at <ISO8601> from <git sha>``，其余是 JSON；
* ``--db``：所有前台共享一个库，跑完库里有 cs_turn / cs_observation 行；同一个库连跑两次不报错、
  读数与内存库逐项相同、话术库不重复落行；
* holdout12 两条路径都跑，p13 路径注入的是空夹具端口。

本文件**不**打开任何留出集文件（p12_holdout_cases.json / p14_holdout_cases.json）：留出集的
路径常量在每条用到它的测试里都被替换成 tmp 文件。
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import re
import subprocess
import sys

import pytest

from maos.core.store import SqliteStore
from maos.domain.cs import corpus, evaluate

ROOT_T177 = pathlib.Path(__file__).resolve().parents[2]
SCRIPT_T177 = ROOT_T177 / "scripts" / "cs_eval.py"

HEADER_RE_T177 = re.compile(r"^# generated at \d{4}-\d\d-\d\dT\S+ from [0-9a-f]{7,40}(-dirty)?$")

#: 哨兵：塞进临时留出集的句子里，任何输出里都不许出现。
SENTINELS_T177 = ("哨兵甲T177ZQX", "SENTINEL-B-T177-QZX", "哨兵丙T177打扰")


def _load_cli_t177():
    spec = importlib.util.spec_from_file_location("cs_eval_cli_t177", SCRIPT_T177)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def cli_t177(monkeypatch, tmp_path):
    """每条测试一个新加载的脚本模块；两个留出集路径一律先指向 tmp 下不存在的文件（不碰真留出集）。"""
    mod = _load_cli_t177()
    monkeypatch.setattr(mod, "HOLDOUT12_PATH", tmp_path / "absent_holdout12.json")
    monkeypatch.setattr(mod, "HOLDOUT14_PATH", tmp_path / "absent_holdout14.json")
    return mod


def _sentinel_doc_t177() -> dict:
    """自造的「留出集」：句子里带哨兵、期望故意对不上（于是一定有 miss 要报）。"""
    s1, s2, s3 = SENTINELS_T177
    return {
        "_note": "T177 测试用临时文件", "_provenance": {"synthetic": True, "written_by": "task-t177"},
        "_thresholds": {"intent_accuracy": 0.85, "route_accuracy": 0.85, "handoff_recall": 0.9,
                        "status_fabrication_max": 0, "wording_accuracy": 1.0, "wrong_status_max": 0},
        "cases": [
            {"id": "T177H-001", "synthetic": True, "tags": ["sentinel"],
             "turns": [f"{s1} 你们用哪家快递"],
             "expect": [{"route": "handoff", "intent": "privacy", "reason": "privacy"}]},
            {"id": "T177H-002", "synthetic": True, "tags": ["sentinel"],
             "turns": [f"{s2} hello there", f"{s3} 我要投诉"],
             "expect": [{"route": "answer", "intent": "logistics", "cite": "LOG-002"},
                        {"route": "answer", "intent": "general"}]},
        ],
    }


def _write_t177(path: pathlib.Path, doc: dict) -> pathlib.Path:
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return path


def _run_t177(mod, capsys, *argv: str) -> tuple[int, str]:
    code = mod.main(list(argv))
    return code, capsys.readouterr().out


def _misses_t177(doc: dict) -> list[str]:
    return [m for s in doc["sets"] for r in s.get("runs", []) for m in r["misses"]]


# ---------------------------------------------------------------------------
# 退出码
# ---------------------------------------------------------------------------
def test_dev13_meets_and_exits_zero_t177(cli_t177, capsys):
    code, out = _run_t177(cli_t177, capsys, "--set", "dev13")
    assert code == 0, out
    assert "[dev13] PASS" in out and "exit=0" in out


def test_exit_code_follows_meets_t177(cli_t177, capsys):
    """退出码只看 meets：任何一集 FAIL → 1，SKIP 不算失败。不钉 dev12 的读数本身。"""
    code, out = _run_t177(cli_t177, capsys, "--set", "dev12", "--json")
    doc = json.loads(out)
    (only,) = doc["sets"]
    assert code == doc["exit_code"] == (0 if only["meets"] else 1)
    assert cli_t177.exit_code_of([{"status": "PASS"}, {"status": "SKIP"}]) == 0
    assert cli_t177.exit_code_of([{"status": "PASS"}, {"status": "FAIL"}]) == 1


def test_failing_set_exits_one_t177(cli_t177, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(cli_t177, "HOLDOUT14_PATH",
                        _write_t177(tmp_path / "h14.json", _sentinel_doc_t177()))
    code, out = _run_t177(cli_t177, capsys, "--set", "holdout14")
    assert code == 1, out
    assert "[holdout14] FAIL" in out


@pytest.mark.parametrize("argv", [["--set", "bogus"], ["--no-such-flag"], ["--set"]])
def test_usage_error_exits_two_t177(cli_t177, capsys, argv):
    code, _ = _run_t177(cli_t177, capsys, *argv)
    assert code == 2


def test_usage_error_exits_two_as_a_process_t177():
    proc = subprocess.run([sys.executable, str(SCRIPT_T177), "--set", "bogus"],
                          cwd=ROOT_T177, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 2, proc.stderr


def test_holdout14_missing_is_skipped_not_an_error_t177(cli_t177, capsys):
    code, out = _run_t177(cli_t177, capsys, "--set", "holdout14")
    assert code == 0, out
    assert "[holdout14] SKIP" in out and "不存在" in out
    code, out = _run_t177(cli_t177, capsys, "--set", "holdout14", "--json")
    (only,) = json.loads(out)["sets"]
    assert code == 0 and only["status"] == "SKIP" and only["runs"] == []


# ---------------------------------------------------------------------------
# 只出聚合数与 id
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("set_name", ["holdout12", "holdout14"])
def test_holdout_outputs_ids_never_sentences_t177(cli_t177, capsys, tmp_path, monkeypatch,
                                                  set_name):
    doc_path = _write_t177(tmp_path / f"{set_name}.json", _sentinel_doc_t177())
    monkeypatch.setattr(cli_t177, set_name.upper() + "_PATH", doc_path)
    out_file = tmp_path / "report.json"
    code_text, text = _run_t177(cli_t177, capsys, "--set", set_name, "--out", str(out_file))
    code_json, js = _run_t177(cli_t177, capsys, "--set", set_name, "--json")
    written = out_file.read_text(encoding="utf-8")
    assert code_text == code_json == 1
    for blob in (text, js, written):
        for sentinel in SENTINELS_T177:
            assert sentinel not in blob, sentinel
        # 期望 / 实得的明细也不出（只有指标名、计数与 id）
        assert "你们用哪家快递" not in blob and "hello there" not in blob
    misses = _misses_t177(json.loads(js))
    assert misses and all(re.fullmatch(r"T177H-00[12]#\d", m) for m in misses), misses
    assert "T177H-001#1" in misses and "T177H-001#1" in text
    runs = json.loads(js)["sets"][0]["runs"]
    assert [r["path"] for r in runs] == (["p12", "p13"] if set_name == "holdout12" else ["p13"])
    for run in runs:
        assert set(run) == {"path", "cases", "turns", "metrics", "meets", "shortfalls",
                            "misses", "miss_by_problem"}
        assert all(isinstance(v, (int, float)) for v in run["metrics"].values())


def test_dev_sets_use_the_same_ids_only_rule_t177(cli_t177, capsys):
    """dev 集同口径：开发集的客户原文一句都不出现在输出里。"""
    code, out = _run_t177(cli_t177, capsys, "--set", "dev12", "--json")
    doc = json.loads(out)
    turns = [t for c in evaluate.load_document(evaluate.EVAL_PATH)["cases"] for t in c["turns"]]
    leaked = [t for t in turns if len(t) >= 4 and t in out]
    assert leaked == []
    assert all(re.fullmatch(r"CS12-\d{3}#\d+", m) for m in _misses_t177(doc))


def test_holdout12_p13_path_injects_empty_fixture_ports_t177(cli_t177, capsys, tmp_path,
                                                             monkeypatch):
    """p12 路径不注入端口：要看具体订单 → needs_order_lookup（对上）；p13 路径注入空夹具端口：
    没给单号 → 追问单号（clarify，对不上）。两条路径都跑、结论不同。"""
    doc = {"_thresholds": {"intent_accuracy": 1.0, "route_accuracy": 1.0, "handoff_recall": 1.0,
                           "status_fabrication_max": 0},
           "cases": [{"id": "T177P-001", "synthetic": True, "turns": ["麻烦问下，我的包裹到哪了"],
                      "expect": [{"route": "handoff", "intent": "logistics",
                                  "reason": "needs_order_lookup"}]}]}
    monkeypatch.setattr(cli_t177, "HOLDOUT12_PATH", _write_t177(tmp_path / "h12.json", doc))
    code, out = _run_t177(cli_t177, capsys, "--set", "holdout12", "--json")
    (only,) = json.loads(out)["sets"]
    p12, p13 = only["runs"]
    assert (p12["path"], p12["meets"], p12["misses"]) == ("p12", True, [])
    assert (p13["path"], p13["meets"], p13["misses"]) == ("p13", False, ["T177P-001#1"])
    assert code == 1 and only["status"] == "FAIL"


# ---------------------------------------------------------------------------
# --out
# ---------------------------------------------------------------------------
def test_out_file_has_header_then_json_t177(cli_t177, capsys, tmp_path):
    out_file = tmp_path / "sub" / "dev13.json"
    code, js = _run_t177(cli_t177, capsys, "--set", "dev13", "--out", str(out_file), "--json")
    first, rest = out_file.read_text(encoding="utf-8").split("\n", 1)
    assert HEADER_RE_T177.match(first), first
    assert json.loads(rest) == json.loads(js)
    assert code == 0


# ---------------------------------------------------------------------------
# --db 共享库
# ---------------------------------------------------------------------------
def _count_t177(db: pathlib.Path, sql: str) -> int:
    store = SqliteStore(str(db))
    return int(store._conn.execute(sql).fetchone()[0])


def test_shared_db_gets_rows_and_can_run_twice_t177(cli_t177, capsys, tmp_path):
    db = tmp_path / "eval.db"
    code1, out1 = _run_t177(cli_t177, capsys, "--set", "dev13", "--db", str(db), "--json")
    turns = _count_t177(db, "SELECT COUNT(*) FROM cs_turn")
    obs = _count_t177(db, "SELECT COUNT(*) FROM cs_observation")
    docs = _count_t177(db, "SELECT COUNT(*) FROM kb_doc WHERE kind = 'cs_script'")
    code2, out2 = _run_t177(cli_t177, capsys, "--set", "dev13", "--db", str(db), "--json")
    assert code1 == code2 == 0, (out1, out2)
    run1, run2 = (json.loads(o)["sets"][0]["runs"][0] for o in (out1, out2))
    assert run1 == run2                                    # 第二次也是新会话：读数不变
    assert turns == run1["turns"] > 0 and obs > 0
    assert _count_t177(db, "SELECT COUNT(*) FROM cs_turn") == 2 * turns
    assert _count_t177(db, "SELECT COUNT(*) FROM cs_observation") == 2 * obs
    # 话术库幂等：重复 seed 不重复落行
    assert _count_t177(db, "SELECT COUNT(*) FROM kb_doc WHERE kind = 'cs_script'") == docs \
        == len(corpus.load_corpus())


@pytest.mark.parametrize("set_name", ["dev12", "dev13"])
def test_shared_db_reads_the_same_as_memory_t177(cli_t177, capsys, tmp_path, set_name):
    _, mem = _run_t177(cli_t177, capsys, "--set", set_name, "--json")
    _, shared = _run_t177(cli_t177, capsys, "--set", set_name, "--db",
                          str(tmp_path / "x.db"), "--json")
    assert json.loads(mem)["sets"] == json.loads(shared)["sets"]
