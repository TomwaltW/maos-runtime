"""T189 · 篇级「零自信答错」（review/p16-cs-contracts.md §2「T189」）。

口径（evaluate 模块头 p16 节）：**实得** route=answer 的轮里，意图错、或期望给了 cite 而实得引用
不含该篇，计 1（一轮最多计 1）；兜底 / 转人工 / 追问 / 静默不计；没给 cite 期望时只看意图；
前台出错的轮不计。它进 ``metrics()`` / ``describe()`` / cs_eval 的文本与 ``--json``，不进门槛。

钉住：

1. 构造的正反例（两个跑批 run_eval / run_eval_p13 同一口径）；
2. 门槛键集与 ``meets`` 不变（shortfalls 里不出现 confident_wrong）；
3. p12 开发集（真前台）confident_wrong == 0（p13 开发集的 0 钉在 test_cs_eval_p13_t174）；
4. cs_eval.py 文本与 ``--json`` 带上计数与 ``<case id>#<轮次>``，``--db`` 的运行标记还原成原 id，
   句子不出。

本文件不打开任何留出集：CLI 的留出集路径一律换成 tmp 下不存在的文件。
"""

from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

from maos.core.store import SqliteStore
from maos.domain.cs import evaluate
from maos.domain.cs import types as T
from maos.domain.cs.corpus import seed_cs_kb
from maos.domain.cs.desk import CsConfig, FrontDesk
from maos.domain.cs.evaluate import EvalCase, EvalExpect, EvalReport, EvalReportP13

ROOT_T189 = pathlib.Path(__file__).resolve().parents[2]
SCRIPT_T189 = ROOT_T189 / "scripts" / "cs_eval.py"
TENANT_T189 = "tnt-demo"


def _doc_t189(scheme: str) -> str:
    return evaluate.cite_doc_id(TENANT_T189, scheme)


class _ScriptedDeskT189:
    """按「客户原文 → (route, intent, citations, reason)」回 DeskResult 的替身；原文里带 BOOM 就抛。"""

    def __init__(self, script):
        self.script = script
        self.n = 0

    def handle(self, msg):
        self.n += 1
        text = msg.text
        if "BOOM" in text:
            raise RuntimeError("scripted failure")
        route, intent, cites, reason = self.script[text]
        reply = "" if route == T.ROUTE_SILENT else "好的"
        return T.DeskResult(reply_text=reply, tenant_id=TENANT_T189, conversation_id="c",
                            turn_id=f"c-t{self.n:04d}", route=route, intent=intent,
                            draft=T.ReplyDraft(text=reply, citations=tuple(cites)),
                            handoff_reason=reason)


# (case id, 原文, 期望, 实得 (route, intent, citations, reason), 是否计 confident_wrong)
_ROWS_T189 = (
    # 计：意图对、篇错（给了 cite 期望）
    ("CW-01", "句一", EvalExpect(route="answer", intent="logistics", cite="LOG-001"),
     (T.ROUTE_ANSWER, "logistics", (_doc_t189("LOG-002"),), ""), True),
    # 计：意图对、一篇都没引
    ("CW-02", "句二", EvalExpect(route="answer", intent="return_exchange", cite="RET-001"),
     (T.ROUTE_ANSWER, "return_exchange", (), ""), True),
    # 计：意图错（没给 cite 期望）
    ("CW-03", "句三", EvalExpect(route="answer", intent="general"),
     (T.ROUTE_ANSWER, "logistics", (_doc_t189("LOG-001"),), ""), True),
    # 计：意图错 + 篇错，同一轮只计 1
    ("CW-04", "句四", EvalExpect(route="answer", intent="refund_payment", cite="PAY-001"),
     (T.ROUTE_ANSWER, "return_exchange", (_doc_t189("RET-004"),), ""), True),
    # 计：期望本该转人工，实得却自信地答了、意图还错
    ("CW-05", "句五", EvalExpect(route="handoff", intent="complaint", reason="complaint"),
     (T.ROUTE_ANSWER, "logistics", (_doc_t189("LOG-004"),), ""), True),
    # 不计：意图对、篇对（引用里多挂一篇也算含）
    ("OK-01", "句六", EvalExpect(route="answer", intent="logistics", cite="LOG-003"),
     (T.ROUTE_ANSWER, "logistics", (_doc_t189("LOG-001"), _doc_t189("LOG-003")), ""), False),
    # 不计：没给 cite 期望时只看意图（引了哪篇都不管）
    ("OK-02", "句七", EvalExpect(route="answer", intent="general"),
     (T.ROUTE_ANSWER, "general", (_doc_t189("PAY-005"),), ""), False),
    # 不计：兜底（意图、引用都不对也不计）
    ("OK-03", "句八", EvalExpect(route="answer", intent="logistics", cite="LOG-001"),
     (T.ROUTE_FALLBACK, "unknown", (), ""), False),
    # 不计：转人工（意图错）
    ("OK-04", "句九", EvalExpect(route="answer", intent="general", cite="GEN-001"),
     (T.ROUTE_HANDOFF, "complaint", (), "complaint"), False),
    # 不计：追问
    ("OK-05", "句十", EvalExpect(route="answer", intent="logistics", cite="LOG-002"),
     (T.ROUTE_CLARIFY, "refund_payment", (), ""), False),
    # 不计：静默
    ("OK-06", "句十一", EvalExpect(route="answer", intent="general", cite="GEN-002"),
     (T.ROUTE_SILENT, "unknown", (), ""), False),
    # 不计：前台抛异常（没有实得出口）
    ("OK-07", "BOOM 句十二", EvalExpect(route="answer", intent="general", cite="GEN-001"),
     None, False),
)


def _cases_t189(rows=_ROWS_T189) -> tuple[EvalCase, ...]:
    return tuple(EvalCase(id=cid, turns=(text,), expect=(exp,)) for cid, text, exp, _, _ in rows)


def _script_t189(rows=_ROWS_T189) -> dict:
    return {text: got for _, text, _, got, _ in rows if got is not None}


def _want_ids_t189(rows=_ROWS_T189) -> tuple[str, ...]:
    return tuple(f"{cid}#1" for cid, _, _, _, hit in rows if hit)


def _run_t189(runner: str, rows=_ROWS_T189) -> EvalReport:
    script = _script_t189(rows)
    if runner == "p12":
        return evaluate.run_eval(lambda: _ScriptedDeskT189(script), _cases_t189(rows))
    return evaluate.run_eval_p13(lambda ports: _ScriptedDeskT189(script), _cases_t189(rows))


# ---------------------------------------------------------------------------
# 1. 构造的正反例
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("runner", ["p12", "p13"])
def test_confident_wrong_counts_exactly_the_wrong_answers_t189(runner):
    report = _run_t189(runner)
    assert type(report) is (EvalReport if runner == "p12" else EvalReportP13)
    want = _want_ids_t189()
    assert report.confident_wrong_ids() == want, report.describe()
    assert report.confident_wrong == len(want) == 5
    assert report.metrics()["confident_wrong"] == 5


@pytest.mark.parametrize("runner", ["p12", "p13"])
@pytest.mark.parametrize("row", _ROWS_T189, ids=[r[0] for r in _ROWS_T189])
def test_each_row_alone_t189(runner, row):
    """逐行单跑：每一行自己的结论（防的是整批里计数碰巧凑对）。"""
    report = _run_t189(runner, (row,))
    assert report.confident_wrong_ids() == ((f"{row[0]}#1",) if row[4] else ())


@pytest.mark.parametrize("runner", ["p12", "p13"])
def test_right_intent_right_cite_everywhere_is_zero_t189(runner):
    """反例整批：每一轮都 answer、意图对、引用含期望那篇 → 0。"""
    rows = tuple((cid, text, exp, (T.ROUTE_ANSWER, exp.intent,
                                   (_doc_t189(exp.cite),) if exp.cite else (), ""), False)
                 for cid, text, exp, _, _ in _ROWS_T189 if "BOOM" not in text)
    report = _run_t189(runner, rows)
    assert report.confident_wrong == 0 and report.confident_wrong_ids() == ()


def test_cite_follows_tenant_map_t189():
    """引用比的是本租户那篇：别的租户同编号的 doc_id 不算含。"""
    exp = EvalExpect(route="answer", intent="logistics", cite="LOG-001")
    wrong_tenant = evaluate.cite_doc_id("tnt-other", "LOG-001")
    rows = (("CW-T", "句甲", exp, (T.ROUTE_ANSWER, "logistics", (wrong_tenant,), ""), True),)
    assert _run_t189("p12", rows).confident_wrong_ids() == ("CW-T#1",)


def test_multi_turn_ids_carry_the_turn_number_t189():
    script = {"一": (T.ROUTE_ANSWER, "general", (_doc_t189("GEN-001"),), ""),
              "二": (T.ROUTE_ANSWER, "logistics", (_doc_t189("GEN-002"),), "")}
    case = EvalCase(id="CW-M", turns=("一", "二"),
                    expect=(EvalExpect(route="answer", intent="general", cite="GEN-001"),
                            EvalExpect(route="answer", intent="general", cite="GEN-002")))
    report = evaluate.run_eval(lambda: _ScriptedDeskT189(script), (case,))
    assert report.confident_wrong_ids() == ("CW-M#2",)


# ---------------------------------------------------------------------------
# 2. 只报不拦：门槛键集、meets 不变；describe 带上
# ---------------------------------------------------------------------------
def test_threshold_keys_unchanged_t189():
    assert evaluate.THRESHOLD_KEYS == ("intent_accuracy", "route_accuracy", "handoff_recall",
                                       "status_fabrication_max")
    assert evaluate.P13_THRESHOLD_KEYS == evaluate.THRESHOLD_KEYS + ("wording_accuracy",
                                                                     "wrong_status_max")


@pytest.mark.parametrize("runner", ["p12", "p13"])
def test_confident_wrong_does_not_enter_meets_t189(runner):
    report = _run_t189(runner)
    loose = {"intent_accuracy": 0.0, "route_accuracy": 0.0, "handoff_recall": 0.0,
             "status_fabrication_max": 99, "wording_accuracy": 0.0, "wrong_status_max": 99,
             "cite_accuracy": 0.0}
    assert report.confident_wrong == 5
    assert report.shortfalls(loose) == [] and report.meets(loose) is True
    assert all("confident_wrong" not in s for s in report.shortfalls({}))


def test_default_report_has_zero_and_no_ids_t189():
    """手造的报告（不给这一项）缺省为 0：p12 / p13 的旧构造处不用改。"""
    report = EvalReport(cases=0, turns=0, intent_hits=0, route_hits=0, handoff_expected=0,
                        handoff_caught=0, cite_expected=0, cite_hits=0, status_fabrication=0)
    assert report.confident_wrong == 0 and report.confident_wrong_ids() == ()
    assert "confident_wrong=0" in report.describe()


def test_describe_shows_count_and_ids_t189():
    text = _run_t189("p12").describe()
    first = text.splitlines()[0]
    assert "confident_wrong=5" in first
    assert "  confident_wrong: " + " ".join(_want_ids_t189()) in text.splitlines()


# ---------------------------------------------------------------------------
# 3. p12 开发集（真前台）
# ---------------------------------------------------------------------------
def _desk_factory_t189() -> FrontDesk:
    store = SqliteStore(":memory:")
    seed_cs_kb(store)
    return FrontDesk(store, CsConfig(tenants={"wk_eval": TENANT_T189}, handoff_target=None))


def test_p12_dev_set_has_no_confident_wrong_t189():
    """p12 开发集 confident_wrong == 0。KNOWN_GAPS 那一轮（CS12-044#1）实得出口是转人工，不计。"""
    report = evaluate.run_eval(_desk_factory_t189, evaluate.load_cases())
    assert report.turns >= 40
    assert report.confident_wrong == 0, report.describe()
    assert report.confident_wrong_ids() == ()


# ---------------------------------------------------------------------------
# 4. cs_eval.py：文本与 --json 带上计数与 id
# ---------------------------------------------------------------------------
def _load_cli_t189():
    spec = importlib.util.spec_from_file_location("cs_eval_cli_t189", SCRIPT_T189)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _cli_doc_t189() -> dict:
    cases = []
    for cid, text, exp, _, _ in _ROWS_T189:
        cases.append({"id": cid, "synthetic": True, "tags": ["t189"], "turns": [text],
                      "expect": [exp.to_json()]})
    return {"_note": "T189 测试用临时文件", "_provenance": {"synthetic": True},
            "_thresholds": {"intent_accuracy": 1.0, "route_accuracy": 1.0,
                            "handoff_recall": 1.0, "status_fabrication_max": 0},
            "cases": cases}


@pytest.fixture()
def cli_t189(monkeypatch, tmp_path):
    mod = _load_cli_t189()
    monkeypatch.setattr(mod, "HOLDOUT12_PATH", tmp_path / "absent_holdout12.json")
    monkeypatch.setattr(mod, "HOLDOUT14_PATH", tmp_path / "absent_holdout14.json")
    path = tmp_path / "dev12_t189.json"
    path.write_text(json.dumps(_cli_doc_t189(), ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(mod, "DEV12_PATH", path)
    script = _script_t189()
    monkeypatch.setattr(mod, "_p12_factory", lambda stores: (lambda: _ScriptedDeskT189(script)))
    return mod


@pytest.mark.parametrize("with_db", [False, True])
def test_cli_reports_confident_wrong_count_and_ids_t189(cli_t189, capsys, tmp_path, with_db):
    extra = ["--db", str(tmp_path / "shared.sqlite")] if with_db else []
    code_json = cli_t189.main(["--set", "dev12", "--json", *extra])
    js = capsys.readouterr().out
    code_text = cli_t189.main(["--set", "dev12", *extra])
    text = capsys.readouterr().out
    assert code_json == code_text == 1
    run = json.loads(js)["sets"][0]["runs"][0]
    want = list(_want_ids_t189())
    assert run["metrics"]["confident_wrong"] == 5
    assert run["confident_wrong_ids"] == want          # --db 的运行标记已还原成原 id
    assert "confident_wrong=5" in text
    assert f"    confident_wrong (5): {' '.join(want)}" in text.splitlines()
    for blob in (js, text):
        assert "句一" not in blob and "句四" not in blob


def test_cli_omits_the_id_line_when_zero_t189(cli_t189, capsys, monkeypatch):
    rows = tuple((cid, text, exp, (T.ROUTE_ANSWER, exp.intent,
                                   (_doc_t189(exp.cite),) if exp.cite else (), ""), False)
                 for cid, text, exp, _, _ in _ROWS_T189 if "BOOM" not in text)
    script = _script_t189(rows)
    monkeypatch.setattr(cli_t189, "_p12_factory",
                        lambda stores: (lambda: _ScriptedDeskT189(script)))
    cli_t189.main(["--set", "dev12", "--json"])
    run = json.loads(capsys.readouterr().out)["sets"][0]["runs"][0]
    assert run["metrics"]["confident_wrong"] == 0 and run["confident_wrong_ids"] == []
    cli_t189.main(["--set", "dev12"])
    text = capsys.readouterr().out
    assert "confident_wrong=0" in text and "confident_wrong (" not in text
