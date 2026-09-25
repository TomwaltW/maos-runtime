"""T177 · 评测批量化 scripts/cs_eval.py（review/p14-cs-contracts.md §2「T177」）。

钉住的是 CLI 的**形状**，不钉留出集读数（读数由主会话在留出集测试里钉）：

* 退出码 0 / 1 / 2（全部 meets / 有集不达标 / 用法错）；holdout14 文件不存在 → SKIP、不报错；
* 只出聚合数与 id：留出集用自造的临时文件替换路径常量、塞哨兵句子，stdout / ``--json`` /
  ``--out`` 里一个哨兵都不出现，没对上的轮只以 ``<id>#<轮次>`` 出现；
* ``--out`` 首行 ``# generated at <ISO8601> from <git sha>``，其余是 JSON；
* ``--db``：所有前台共享一个库，跑完库里有 cs_turn / cs_observation 行；同一个库连跑两次不报错、
  读数与内存库逐项相同、话术库不重复落行；
* holdout12 两条路径都跑，p13 路径注入的是空夹具端口；dev12 只走 run_eval、dev13 只走
  run_eval_p13，且整份文件都跑到；一条路径都没跑的集不算达标；
* ``--set all``（也是缺省）四集依次跑；
* ``--out`` 首行的 sha 就是 make_evidence.git_sha()；
* 集文件格式不对 → 该集 ERROR、只出异常类名，文件里的原值（期望标签、夹具值、门槛值）不外泄。

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
                        "status_fabrication_max": 0, "wording_accuracy": 1.0, "wrong_status_max": 0,
                        "_note": f"{s1} 门槛说明"},
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


@pytest.mark.parametrize("set_name", ["holdout12", "holdout14"])
def test_frontdesk_errors_do_not_leak_sentences_to_stderr_t177(cli_t177, capsys, tmp_path,
                                                                monkeypatch, set_name):
    """前台内部出错（异常消息里带客户原文）：stderr 同样不出句子，只出打码后的一行。"""
    from maos.domain.cs import desk

    def _boom_t177(text, *args, **kwargs):
        raise ValueError(f"boom on {text}")

    # 触发词检测是两条路径（注不注入端口）都会走的一环
    monkeypatch.setattr(desk, "detect", _boom_t177)
    doc_path = _write_t177(tmp_path / f"{set_name}.json", _sentinel_doc_t177())
    monkeypatch.setattr(cli_t177, set_name.upper() + "_PATH", doc_path)
    code = cli_t177.main(["--set", set_name, "--json"])
    captured = capsys.readouterr()
    assert code == 1
    assert "[cs_eval] ERROR maos.cs exc=ValueError" in captured.err, captured.err
    for blob in (captured.out, captured.err):
        for sentinel in SENTINELS_T177:
            assert sentinel not in blob, sentinel
        assert "boom on" not in blob and "Traceback" not in blob
    # 跑完还原 maos 这一支 logger
    import logging
    assert logging.getLogger("maos").propagate is True


_BOOM_DRIVER_T177 = """
import importlib.util, pathlib, sys
from maos.domain.cs import desk
def _boom(text, *a, **k):
    raise ValueError("boom on " + text)
desk.detect = _boom
spec = importlib.util.spec_from_file_location("cs_eval_boom_t177", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod.HOLDOUT14_PATH = pathlib.Path(sys.argv[2])
sys.exit(mod.main(["--set", "holdout14", "--out", sys.argv[3]]))
"""


def test_frontdesk_errors_do_not_leak_as_a_process_t177(tmp_path):
    """进程级：没有 pytest 的日志接管时（lastResort 会把堆栈写到 stderr），stderr 也不出句子。"""
    doc_path = _write_t177(tmp_path / "h14.json", _sentinel_doc_t177())
    out_file = tmp_path / "boom.json"
    proc = subprocess.run([sys.executable, "-c", _BOOM_DRIVER_T177, str(SCRIPT_T177),
                           str(doc_path), str(out_file)],
                          cwd=ROOT_T177, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 1
    for blob in (proc.stdout, proc.stderr, out_file.read_text(encoding="utf-8")):
        for sentinel in SENTINELS_T177:
            assert sentinel not in blob, sentinel
        assert "boom on" not in blob and "Traceback" not in blob
    assert "exc=ValueError" in proc.stderr


def _threshold_doc_t177(ratio: float) -> dict:
    """两个 case：一个照抄开发集里答得对的 CS13-001，一个故意对不上；门槛由参数定。"""
    good = next(c for c in evaluate.load_document(evaluate.P13_EVAL_PATH)["cases"]
                if c["id"] == "CS13-001")
    good = dict(good, id="T177T-001")
    bad = {"id": "T177T-002", "synthetic": True, "open_kfid": "wk_eval", "turns": ["hello there"],
           "expect": [{"route": "answer", "intent": "logistics", "lang": "zh"}]}
    return {"_thresholds": {"intent_accuracy": ratio, "route_accuracy": ratio,
                            "handoff_recall": 0.0, "wording_accuracy": 0.0,
                            "status_fabrication_max": 0, "wrong_status_max": 0},
            "cases": [good, bad]}


@pytest.mark.parametrize("ratio,expected_code", [(0.4, 0), (1.0, 1)])
def test_thresholds_come_from_the_set_file_t177(cli_t177, capsys, tmp_path, monkeypatch,
                                                ratio, expected_code):
    """门槛取各文件的 _thresholds：同一份两轮答对一轮的集，宽门槛 PASS、1.0 门槛 FAIL。"""
    doc = _threshold_doc_t177(ratio)
    monkeypatch.setattr(cli_t177, "HOLDOUT14_PATH", _write_t177(tmp_path / "h14.json", doc))
    code, out = _run_t177(cli_t177, capsys, "--set", "holdout14", "--json")
    (only,) = json.loads(out)["sets"]
    (run,) = only["runs"]
    assert run["misses"] == ["T177T-002#1"]                  # 两轮恰好错一轮
    assert only["thresholds"] == doc["_thresholds"]
    assert code == expected_code
    assert only["status"] == ("PASS" if expected_code == 0 else "FAIL")


def test_report_thresholds_keep_numeric_keys_only_t177(cli_t177, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(cli_t177, "HOLDOUT14_PATH",
                        _write_t177(tmp_path / "h14.json", _sentinel_doc_t177()))
    _, out = _run_t177(cli_t177, capsys, "--set", "holdout14", "--json")
    (only,) = json.loads(out)["sets"]
    assert "_note" not in only["thresholds"]
    assert all(isinstance(v, (int, float)) for v in only["thresholds"].values())


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
    # 出处就是当前代码：sha 与 make_evidence.git_sha() 同一口径（不许写死或冒充）
    from scripts.make_evidence import git_sha
    assert first.rsplit(" from ", 1)[1] == git_sha()


# ---------------------------------------------------------------------------
# 各集的跑法（哪个集走哪条路径）与 --set all
# ---------------------------------------------------------------------------
def _spy_paths_t177(monkeypatch) -> list[str]:
    """包一层 evaluate.run_eval / run_eval_p13（照常委托），记下调用顺序。"""
    calls: list[str] = []
    real_p12, real_p13 = evaluate.run_eval, evaluate.run_eval_p13

    def p12(*a, **k):
        calls.append("run_eval")
        return real_p12(*a, **k)

    def p13(*a, **k):
        calls.append("run_eval_p13")
        return real_p13(*a, **k)

    monkeypatch.setattr(evaluate, "run_eval", p12)
    monkeypatch.setattr(evaluate, "run_eval_p13", p13)
    return calls


@pytest.mark.parametrize("set_name,dev_path,expected", [
    ("dev12", evaluate.EVAL_PATH, ["run_eval"]),
    ("dev13", evaluate.P13_EVAL_PATH, ["run_eval_p13"]),
])
def test_dev_set_runs_its_own_path_over_the_whole_file_t177(cli_t177, capsys, monkeypatch,
                                                            set_name, dev_path, expected):
    """dev12 只走 p12 路径（run_eval、不注入端口），dev13 只走 run_eval_p13；且整份文件都跑到。"""
    calls = _spy_paths_t177(monkeypatch)
    code, out = _run_t177(cli_t177, capsys, "--set", set_name, "--json")
    doc = json.loads(out)
    (only,) = doc["sets"]
    assert calls == expected
    (run,) = only["runs"]
    assert run["path"] == ("p12" if set_name == "dev12" else "p13")
    raw = evaluate.load_document(dev_path)["cases"]
    assert run["cases"] == len(raw)
    assert run["turns"] == sum(len(c["turns"]) for c in raw) > 0
    assert code == doc["exit_code"] == (0 if only["meets"] else 1)


def test_holdout12_calls_both_paths_t177(cli_t177, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(cli_t177, "HOLDOUT12_PATH",
                        _write_t177(tmp_path / "h12.json", _sentinel_doc_t177()))
    calls = _spy_paths_t177(monkeypatch)
    _run_t177(cli_t177, capsys, "--set", "holdout12", "--json")
    assert calls == ["run_eval", "run_eval_p13"]


def test_no_runs_never_meets_t177(cli_t177):
    """一条路径都没跑的集不许靠 all([]) 空转出 PASS。"""
    assert cli_t177.runs_meet([]) is False
    assert cli_t177.runs_meet([{"meets": True}]) is True
    assert cli_t177.runs_meet([{"meets": True}, {"meets": False}]) is False


def test_set_all_runs_the_four_sets_in_order_and_is_the_default_t177(cli_t177, capsys, tmp_path,
                                                                     monkeypatch):
    """--set all（也是缺省）四集依次跑；holdout14 不存在 → SKIP；退出码与各集结论一致。"""
    monkeypatch.setattr(cli_t177, "HOLDOUT12_PATH",
                        _write_t177(tmp_path / "h12.json", _sentinel_doc_t177()))
    code_all, out_all = _run_t177(cli_t177, capsys, "--set", "all", "--json")
    code_def, out_def = _run_t177(cli_t177, capsys, "--json")
    doc_all, doc_def = json.loads(out_all), json.loads(out_def)
    assert [s["set"] for s in doc_all["sets"]] == ["dev12", "dev13", "holdout12", "holdout14"]
    assert [s["status"] for s in doc_all["sets"]][3] == "SKIP"
    assert all(s["status"] in ("PASS", "FAIL") for s in doc_all["sets"][:3])
    assert code_all == doc_all["exit_code"] == cli_t177.exit_code_of(doc_all["sets"])
    assert doc_all["sets"][2]["status"] == "FAIL" and code_all == 1   # 哨兵集故意对不上
    assert (code_def, doc_def["set"], doc_def["sets"]) == (code_all, "all", doc_all["sets"])
    for sentinel in SENTINELS_T177:
        assert sentinel not in out_all


# ---------------------------------------------------------------------------
# 集文件本身格式不对：ERROR、只出类名，文件里的原值一个都不外泄
# ---------------------------------------------------------------------------
_BAD_T177 = "ZQXBAD哨兵T177"


def _malformed_doc_t177(kind: str) -> dict:
    doc = _sentinel_doc_t177()
    case = doc["cases"][0]
    if kind == "route":
        case["expect"][0]["route"] = _BAD_T177
    elif kind == "fixture":
        case["fixtures"] = {"bindings": [{"display_no": [_BAD_T177], "system_name": "x",
                                          "query_key": "k"}]}
    elif kind == "threshold":
        doc["_thresholds"]["intent_accuracy"] = _BAD_T177
    return doc


@pytest.mark.parametrize("set_name", ["holdout12", "holdout14"])
@pytest.mark.parametrize("kind", ["route", "fixture", "threshold"])
def test_malformed_set_is_error_and_leaks_nothing_t177(cli_t177, capsys, tmp_path, monkeypatch,
                                                      set_name, kind):
    monkeypatch.setattr(cli_t177, set_name.upper() + "_PATH",
                        _write_t177(tmp_path / f"{set_name}.json", _malformed_doc_t177(kind)))
    out_file = tmp_path / "bad.json"
    code = cli_t177.main(["--set", set_name, "--json", "--out", str(out_file)])
    captured = capsys.readouterr()
    code_text = cli_t177.main(["--set", set_name])
    captured_text = capsys.readouterr()
    assert code == code_text == 1
    written = out_file.read_text(encoding="utf-8")            # --out 照写
    (only,) = json.loads(captured.out)["sets"]
    assert (only["status"], only["error"], only["runs"]) == ("ERROR", "ValueError", [])
    assert "[" + set_name + "] ERROR" in captured_text.out and "error=ValueError" in captured_text.out
    for blob in (captured.out, captured.err, written, captured_text.out, captured_text.err):
        assert _BAD_T177 not in blob
        for sentinel in SENTINELS_T177:
            assert sentinel not in blob, sentinel
        assert "Traceback" not in blob


_BAD_DRIVER_T177 = """
import importlib.util, pathlib, sys
spec = importlib.util.spec_from_file_location("cs_eval_bad_t177", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod.HOLDOUT12_PATH = pathlib.Path(sys.argv[2]) / "absent12.json"
mod.HOLDOUT14_PATH = pathlib.Path(sys.argv[2]) / "h14.json"
sys.exit(mod.main(["--set", "holdout14", "--out", sys.argv[3]]))
"""


def test_malformed_set_leaks_nothing_as_a_process_t177(tmp_path):
    """进程级：坏文件不带出未捕获的堆栈（堆栈里的 ValueError 消息会带出原值）。"""
    _write_t177(tmp_path / "h14.json", _malformed_doc_t177("route"))
    out_file = tmp_path / "bad.json"
    proc = subprocess.run([sys.executable, "-c", _BAD_DRIVER_T177, str(SCRIPT_T177),
                           str(tmp_path), str(out_file)],
                          cwd=ROOT_T177, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 1, proc.stderr[-200:].replace(_BAD_T177, "<redacted>")
    for blob in (proc.stdout, proc.stderr, out_file.read_text(encoding="utf-8")):
        assert _BAD_T177 not in blob and "Traceback" not in blob
    assert "error=ValueError" in proc.stdout


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
