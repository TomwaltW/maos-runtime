"""`scripts/render_trace.py` 的行为契约 —— 证据束 → 离线单页 HTML。

这个渲染器和别的脚本不一样的地方在于：**它的产物要交到评委手里，而评委不跑命令。**
所以这里守的四件事全部是「拿到文件那一刻还成不成立」，不是「代码写得对不对」：

1. **零外链**（`test_report_has_no_external_link` 等）—— 产物里一个 ``http://`` /
   ``https://`` 都不许有。断网、`file://` 双击、没装 Python 的机器上都必须完整。
   这条最容易在无意中破掉：引一次 CDN、外链一个字体、或者写一个内联 SVG
   （``xmlns="http://www.w3.org/2000/svg"`` 本身就是一条外链字面量）都会中招，
   而破掉之后**在联网的机器上看不出任何异常**——正是要用机器判据钉住的那种失效。
2. **出处**（`test_first_line_is_provenance_header`）—— 铁律 3 的 generated-at 头。
3. **`--check` 真的有牙**（`test_check_turns_red_when_evidence_changes`）——
   一个从来没红过的守卫等于没有守卫，所以这里**故意改坏一个数字**再跑一次。
   同一组断言还钉住反方向：没动过就必须绿，且 `--check` 一个字节都不许写盘。
4. **洞不许被藏起来**（`test_stray_and_unattributed_are_shown`）——
   ``stray_events`` / ``unattributed_usage`` 非空时页面上必须能看见每一条。
   这两份东西恰恰是「审计链有洞」的那一类，静默吞掉比不导出更坏。

## fixture 为什么是手搭的，不用真 evidence/

`evidence/*.db` 不入库，`evidence/scenario-*/` 每跑一次 `make_evidence.py` 就换一批
id 与时间戳。测试要断言的那几件事（有洞、取不到成本、孤儿 span）在真证据上**恰好
一个都不出现**（八束全绿），拿真证据跑等于把负例测试写成空转。所以负例一律手搭，
正例另有一条 `test_committed_report_is_in_sync` 直接跑真产物。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib
import re
import subprocess
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "render_trace.py"
REPORT = ROOT / "evidence" / "report.html"

EXTERNAL_LINK = re.compile(r"https?://")

#: fixture 文件的出处头。形状与 `make_evidence.py::header_line` 一致（铁律 3）。
HEADER = "# generated at 2026-09-05T00:00:00+00:00 from deadbeefcafe1234\n"


def _load_script(name: str) -> types.ModuleType:
    """把 ``scripts/<name>.py`` 当模块加载 —— scripts/ 不是包（同 test_trace_evidence.py）。"""
    key = f"_omega_{name}"
    spec = importlib.util.spec_from_file_location(key, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


render_trace = _load_script("render_trace")


# ---------------------------------------------------------------------------
# fixture：手搭一个证据根
# ---------------------------------------------------------------------------
def _span(span_id, parent, name, kind, attrs, *, trace_id="trace_fixture01"):
    return {
        "trace_id": trace_id, "span_id": span_id, "parent_span_id": parent,
        "name": name, "kind": kind,
        "start": "2026-09-05T00:00:00.000000+00:00",
        "end": "2026-09-05T00:00:00.010000+00:00",
        "attributes": attrs,
    }


def _event(span_id, parent, name, seq, etype, reason=None, detail=None):
    return _span(span_id, parent, name, "event", {
        "maos.event.seq": seq, "maos.event.type": etype,
        "maos.event.id": f"evt_{span_id}", "maos.task_id": "task-fix-1",
        "maos.reason": reason, "maos.detail": detail or {},
    })


GATES = {"schema": "pass", "acceptance": "pass", "security": "pass",
         "evidence": "pass", "compensation": "pass"}


def _spans() -> list[dict]:
    """一棵四层小树：plan → task → event → artifact，带一次转人工和一次返工。"""
    return [
        _span("aaaa000000000001", None, "plan:修一个漏洞", "plan",
              {"maos.plan_id": "plan_fix", "maos.plan.state": "DONE",
               "maos.plan.goal": "修一个漏洞"}),
        _span("aaaa000000000002", "aaaa000000000001", "task:coding:改代码", "task",
              {"maos.task_id": "task-fix-1", "maos.task.role": "coding",
               "maos.task.state": "DONE", "maos.task.attempt": 2,
               "maos.task.risk_level": "M", "maos.task.effect_risk": "H",
               "maos.task.depends_on": []}),
        _event("aaaa000000000003", "aaaa000000000002",
               "state:AWAITING_REVIEW->REWORK", 1, "StateTransition",
               "gate_rework", {"gate_results": dict(GATES, acceptance="fail")}),
        _event("aaaa000000000004", "aaaa000000000002",
               "state:AWAITING_REVIEW->BLOCKED", 2, "StateTransition",
               "gate_needs_human", {"gate_results": GATES, "await": "human_approval"}),
        _event("aaaa000000000005", "aaaa000000000002",
               "state:BLOCKED->DONE", 3, "StateTransition",
               "human_approve", {"operator": "沈思锴", "note": "已核对金额"}),
        _span("aaaa000000000006", "aaaa000000000005", "artifact:test_report", "artifact",
              {"maos.artifact.id": "art_fixture01", "maos.artifact.kind": "test_report",
               "maos.artifact.version": 1, "maos.task_id": "task-fix-1",
               "maos.artifact.provenance": "artifact_seeded",
               "maos.artifact.provenance.event_span": "aaaa000000000005",
               "maos.artifact.provenance.source": "maos.agents.testing.seed_scripted_report",
               "maos.artifact.provenance.note": "旁路入库，见 trace.py 文件头",
               "maos.artifact.sandbox.mode": "not-run",
               "maos.artifact.sandbox.degraded_reason": "场景预置件，未经沙箱执行",
               "maos.artifact.sandbox.note": None}),
    ]


STRAY = [{"event_type": "SkillInvoked", "event_id": "evt_stray_7", "task_id": "",
          "plan_id": "", "reason": "issue.aggregate 发生在建 Plan 之前"}]
ORPHAN_USAGE = [{"agent_role": "manager", "call_site": "maos/agents/base.py::BaseAgent.ask",
                 "tokens_in": 411, "tokens_out": 27, "latency_ms": 0, "estimated": 1}]


def _trace_doc(*, stray=(), unattributed=(), cost_available=True, calls=2) -> dict:
    spans = _spans()
    cost = {
        "available": cost_available,
        "unavailable_reason": None if cost_available else "store 未实现 list_model_usage",
        "calls": calls, "tokens_in": 700, "tokens_out": 300, "tokens_total": 1000,
        "latency_ms": 0, "estimated_calls": calls, "measured_calls": 0,
        "all_estimated": bool(calls), "by_role": [], "by_call_site": [], "by_task": [],
        "top_task": None, "note": "estimated=1 的行是估算不是计费",
        "zero_calls_note": None if calls else "本 trace 一条用量都没记到。这不等于没花。",
        "failures": {"available": True, "unavailable_reason": None, "calls": 0,
                     "latency_ms": 0, "by_error_kind": [], "by_call_site": [],
                     "note": "失败的模型调用单列在这里"},
    }
    return {
        "schema": "maos.trace/v1", "db": "fixture.db", "plan_count": 1,
        "traces": [{
            "schema": "maos.trace/v1", "plan_id": "plan_fix", "trace_id": "trace_fixture01",
            "plan_state": "DONE", "goal": "修一个漏洞", "spans": spans, "cost": cost,
            "summary": {"span_count": len(spans), "task_count": 1, "event_count": 3,
                        "artifact_count": 1, "unsourced_artifacts": 0,
                        "seeded_artifacts": 1, "unresolved_task_events": 0,
                        "degraded_sandbox_reports": 1, "unrecorded_sandbox_reports": 0,
                        "tree_errors": []},
        }],
        "stray_events": list(stray),
        "unattributed_usage": list(unattributed),
        "summary": {"span_count": len(spans), "model_calls": calls,
                    "attributed_model_calls": calls, "unattributed_model_calls": 0,
                    "attributed_tokens_total": 1000, "estimated_model_calls": calls,
                    "measured_model_calls": 0, "all_estimated": bool(calls),
                    "cost_note": "…", "failed_model_calls": 0, "failure_note": "…",
                    "event_count": 3, "unsourced_artifacts": 0, "seeded_artifacts": 1,
                    "degraded_sandbox_reports": 1, "unrecorded_sandbox_reports": 0,
                    "stray_event_count": len(stray), "tree_errors": []},
    }


def _result_doc() -> dict:
    return {
        "scenario": 1, "exit_code": 0, "wall_ms": 1234, "plan_count": 1,
        "plans": [{
            "plan_id": "plan_fix", "goal": "修一个漏洞", "state": "DONE",
            "trace_id": "trace_fixture01",
            "tasks": [{"task_id": "task-fix-1", "role": "coding", "title": "改代码",
                       "state": "DONE", "attempt": 2, "risk_level": "M",
                       "effect_risk": "H"}],
            "metrics": {"duration_ms": 48, "event_count": 3, "event_types": {},
                        "rework_count": 1, "replan_count": 0, "compensation_count": 0,
                        "skill_invocations": 2, "tool_invocations": 1},
            "business_outcome": {"status": "succeeded", "basis": "external_evidence",
                                 "plan_state": "DONE", "external_evidence": [],
                                 "unaudited_evidence_count": 0,
                                 "source": "derived-from-db-at-export-time",
                                 "note": "MAOS 只持有观察与推断（铁律 8）。"},
        }],
        "totals": {"event_count": 3, "rework_count": 1, "replan_count": 0},
    }


def _write(path: pathlib.Path, doc: dict) -> None:
    path.write_text(HEADER + json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


@pytest.fixture
def evidence_root(tmp_path) -> pathlib.Path:
    """一个干净的手搭证据根：一束、无洞、成本取得到。"""
    root = tmp_path / "evidence"
    bundle = root / "scenario-1"
    bundle.mkdir(parents=True)
    _write(bundle / "trace.json", _trace_doc())
    _write(bundle / "result.json", _result_doc())
    _write(root / "INDEX.json", {"git_sha": "deadbeefcafe1234", "requested": [1],
                                 "produced": []})
    return root


def _run(*args, cwd=ROOT) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True, cwd=str(cwd))


# ===========================================================================
# 1. 零外链 —— 断网可看的机器判据
# ===========================================================================
def test_report_has_no_external_link(evidence_root):
    """产物里一个 http(s) 都不许有。CDN、外链字体、内联 SVG 的 xmlns 全在此列。"""
    html = render_trace.build(str(evidence_root))
    hits = EXTERNAL_LINK.findall(html)
    assert not hits, (
        f"产物里出现了 {len(hits)} 处外链字面量 —— 断网就废了。\n"
        "常见来源：引 CDN、外链 Google Fonts、写内联 SVG 时带上了 "
        'xmlns="http://www.w3.org/2000/svg"。CSS/JS 一律内联，图形拿 CSS 画。')


def test_report_fetches_nothing_at_runtime(evidence_root):
    """静态判据之外再钉一条：页面不许在运行时去取任何东西。

    `http` 字面量拦得住外链，拦不住 `fetch('/x')` 这类相对路径请求 ——
    评委双击的是 `file://`，任何一次运行时取数都会静默失败成空白区块。
    """
    html = render_trace.build(str(evidence_root))
    for banned in ("fetch(", "XMLHttpRequest", "importScripts", "<iframe", "<link ",
                   "@import", "new Worker", "navigator.sendBeacon"):
        assert banned not in html, f"产物里出现了 {banned!r} —— file:// 下取不到东西"


def test_committed_report_has_no_external_link():
    """真产物本身也过一遍同一条判据 —— fixture 绿不代表交出去的那份绿。"""
    if not REPORT.exists():
        pytest.skip("evidence/report.html 还没生成（先跑 python3 scripts/render_trace.py）")
    hits = EXTERNAL_LINK.findall(REPORT.read_text(encoding="utf-8"))
    assert not hits, f"evidence/report.html 里有 {len(hits)} 处外链"


# ===========================================================================
# 2. 出处（铁律 3）
# ===========================================================================
def test_first_line_is_provenance_header(evidence_root, tmp_path):
    out = tmp_path / "report.html"
    proc = _run("--evidence", str(evidence_root), "--out", str(out))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    first = out.read_text(encoding="utf-8").splitlines()[0]
    assert render_trace.HTML_HEADER_RE.match(first), f"首行出处格式不对: {first!r}"
    assert first.startswith("<!-- generated at "), "首行必须是 HTML 注释形式的出处头"


def test_committed_report_carries_header():
    if not REPORT.exists():
        pytest.skip("evidence/report.html 还没生成")
    first = REPORT.read_text(encoding="utf-8").splitlines()[0]
    assert render_trace.HTML_HEADER_RE.match(first), f"首行出处格式不对: {first!r}"


def test_header_is_excluded_from_check_comparison(evidence_root, tmp_path):
    """首行带时间戳，所以它**必须**被排除在比对之外 —— 否则 `--check` 恒红。

    恒红的守卫和没有守卫是一回事：没人会再看它第二眼。
    """
    out = tmp_path / "report.html"
    assert _run("--evidence", str(evidence_root), "--out", str(out)).returncode == 0
    text = out.read_text(encoding="utf-8")
    tampered = text.replace(
        text.splitlines()[0],
        "<!-- generated at 2020-01-01T00:00:00+00:00 from deadbeef -->", 1)
    out.write_text(tampered, encoding="utf-8")
    proc = _run("--evidence", str(evidence_root), "--out", str(out), "--check")
    assert proc.returncode == 0, "只换了首行出处，正文没变，--check 不该红：\n" + proc.stdout


# ===========================================================================
# 3. --check 的牙齿
# ===========================================================================
def test_check_is_green_and_writes_nothing(evidence_root, tmp_path):
    out = tmp_path / "report.html"
    assert _run("--evidence", str(evidence_root), "--out", str(out)).returncode == 0
    before = (out.stat().st_mtime_ns, hashlib.sha256(out.read_bytes()).hexdigest())

    proc = _run("--evidence", str(evidence_root), "--out", str(out), "--check")
    after = (out.stat().st_mtime_ns, hashlib.sha256(out.read_bytes()).hexdigest())

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "[OK]" in proc.stdout
    assert before == after, "`--check` 写盘了 —— 它只许比对"


@pytest.mark.parametrize("mutate", [
    pytest.param(lambda d: d["plans"][0]["metrics"].__setitem__("rework_count", 99),
                 id="metrics.rework_count"),
    pytest.param(lambda d: d["plans"][0]["tasks"][0].__setitem__("attempt", 7),
                 id="tasks[0].attempt"),
    pytest.param(lambda d: d.__setitem__("wall_ms", 999999), id="wall_ms"),
    pytest.param(lambda d: d["plans"][0]["business_outcome"].__setitem__(
        "unaudited_evidence_count", 3), id="unaudited_evidence_count"),
])
def test_check_turns_red_when_evidence_changes(evidence_root, tmp_path, mutate):
    """改 `result.json` 里**任意一个数字**，`--check` 必须变红并给出修复命令。

    参数化的四个位置分属四张不同的表（指标行 / 任务清单 / 场景抬头 / 业务结论）——
    只钉一处的话，某天有人把那一处从页面上删掉，守卫会静默失效而测试照绿。
    """
    out = tmp_path / "report.html"
    assert _run("--evidence", str(evidence_root), "--out", str(out)).returncode == 0
    assert _run("--evidence", str(evidence_root), "--out", str(out),
                "--check").returncode == 0, "起点必须是绿的，否则这条测试是空转"

    path = evidence_root / "scenario-1" / "result.json"
    doc = json.loads(path.read_text(encoding="utf-8").split("\n", 1)[1])
    mutate(doc)
    _write(path, doc)

    proc = _run("--evidence", str(evidence_root), "--out", str(out), "--check")
    assert proc.returncode == 1, "证据变了而 HTML 没重生成，--check 必须红：\n" + proc.stdout
    assert "[STALE]" in proc.stdout
    assert "python3 scripts/render_trace.py" in proc.stdout, "报告要给出修复命令"


def test_check_is_red_when_report_is_missing(evidence_root, tmp_path):
    proc = _run("--evidence", str(evidence_root), "--out", str(tmp_path / "nope.html"),
                "--check")
    assert proc.returncode == 1 and "文件不存在" in proc.stdout


def test_committed_report_is_in_sync():
    """交出去的那份必须等于「现在拿 evidence/ 重跑会得到的东西」。

    这一条会在**跑完 `make_evidence.py` 却忘了重跑本脚本**时变红 —— 那正是它的用途：
    `artifacts/` 下那两份手写 HTML 就是这样过期的，没有任何东西提醒过谁。
    修复只有一条命令：`python3 scripts/render_trace.py`。
    """
    if not REPORT.exists():
        pytest.skip("evidence/report.html 还没生成")
    proc = _run("--check")
    assert proc.returncode == 0, (
        "evidence/report.html 与当前 evidence/ 不一致 —— "
        "多半是重跑了 make_evidence.py 没重跑渲染器。\n"
        "修复：python3 scripts/render_trace.py\n\n" + proc.stdout + proc.stderr)


# ===========================================================================
# 4. 洞不许被藏起来
# ===========================================================================
def test_stray_and_unattributed_are_shown(tmp_path):
    """``stray_events`` / ``unattributed_usage`` 非空时，每一条都要在页面上看得见。

    这两份东西是审计链上的洞。渲染器把它们默默跳过，页面会长得比真相干净 ——
    比不做这个页面更坏。
    """
    root = tmp_path / "evidence"
    bundle = root / "scenario-1"
    bundle.mkdir(parents=True)
    _write(bundle / "trace.json",
           _trace_doc(stray=STRAY, unattributed=ORPHAN_USAGE))
    _write(bundle / "result.json", _result_doc())

    html = render_trace.build(str(root))
    assert "游离事件 1 条" in html
    assert STRAY[0]["event_id"] in html
    assert STRAY[0]["reason"] in html
    assert "归属不上的模型用量 1 条" in html
    assert ORPHAN_USAGE[0]["call_site"] in html
    assert "411" in html, "归属不上的那笔 token 数要印出来"


def test_zero_stray_says_it_was_checked(evidence_root):
    """空的时候也要印一句，且要说清是「已查」——「查过了是 0」和「没查」不一样。"""
    html = render_trace.build(str(evidence_root))
    assert "游离事件：<b>0 条</b>（已查" in html
    assert "归属不上的模型用量：<b>0 条</b>（已查" in html


def test_tree_errors_are_recomputed_not_copied(tmp_path):
    """页面上的树错误是**重跑 check_span_tree** 得到的，不是抄 trace.json 里那个数。

    抄的话，一份被改过 span 的证据能一边挂着 `tree_errors: []` 一边把洞带进页面。
    这里造一棵有孤儿的树，而 summary 仍谎称 `tree_errors: []`。
    """
    root = tmp_path / "evidence"
    bundle = root / "scenario-1"
    bundle.mkdir(parents=True)
    doc = _trace_doc()
    doc["traces"][0]["spans"].append(
        _span("bbbb000000000009", "ffff00000000dead", "task:孤儿", "task",
              {"maos.task_id": "task-orphan", "maos.task.state": "DONE"}))
    _write(bundle / "trace.json", doc)
    _write(bundle / "result.json", _result_doc())

    html = render_trace.build(str(root))
    assert "孤儿 span" in html
    assert "ffff00000000dead" in html, "报出来的错误里要看得见那个指不到的 parent"
    assert "以重算为准" in html, "导出时记的与重算的不一致，页面要说出来"


# ===========================================================================
# 5. 成本口径：「取不到」≠「一次都没花」
# ===========================================================================
def test_unavailable_cost_is_not_rendered_as_zero(tmp_path):
    root = tmp_path / "evidence"
    bundle = root / "scenario-1"
    bundle.mkdir(parents=True)
    _write(bundle / "trace.json", _trace_doc(cost_available=False))
    _write(bundle / "result.json", _result_doc())

    html = render_trace.build(str(root))
    assert "取不到" in html and "store 未实现 list_model_usage" in html
    assert "这不是 0，是不知道" in html


def test_zero_calls_carries_its_note(tmp_path):
    """`calls=0` 必须带上 `zero_calls_note` —— 少了那句就会被读成「这条链路没花钱」。"""
    root = tmp_path / "evidence"
    bundle = root / "scenario-1"
    bundle.mkdir(parents=True)
    _write(bundle / "trace.json", _trace_doc(calls=0))
    _write(bundle / "result.json", _result_doc())

    html = render_trace.build(str(root))
    assert "这不等于没花" in html


def test_failures_are_a_separate_block(evidence_root):
    """失败调用单列，且明说不并进成本 —— 合起来会让「很省」和「一直在失败」同形。"""
    html = render_trace.build(str(evidence_root))
    assert "失败调用（不并进左边任何一个数）" in html
    assert "知道且为零" in html


# ===========================================================================
# 6. 闸门与返工要在页面上标出来
# ===========================================================================
def test_gate_verdicts_are_shown_with_original_reason(evidence_root):
    html = render_trace.build(str(evidence_root))
    for token in ("gate_needs_human", "gate_rework", "human_approval",
                  "acceptance:fail", "转人工", "打回返工"):
        assert token in html, f"页面上找不到 {token!r}"


def test_rework_and_hold_are_marked_red(evidence_root):
    """返工与转人工必须落在标红的那一档 —— 派单点名的两处。"""
    html = render_trace.build(str(evidence_root))
    for state in ("REWORK", "BLOCKED"):
        assert re.search(rf'class="chip t-danger"[^>]*>{state}', html), \
            f"{state} 没落在标红那一档"


def test_artifact_provenance_and_degradation_are_shown(evidence_root):
    html = render_trace.build(str(evidence_root))
    assert "artifact_seeded" in html
    assert "maos.agents.testing.seed_scripted_report" in html
    assert "场景预置件，未经沙箱执行" in html, "沙箱降级原因不许折叠掉就不写"


def test_html_is_escaped(tmp_path):
    """证据里的尖括号必须被转义 —— 不转义的话一条 reason 就能改写整页结构。"""
    root = tmp_path / "evidence"
    bundle = root / "scenario-1"
    bundle.mkdir(parents=True)
    doc = _trace_doc()
    doc["traces"][0]["goal"] = '<script>alert("x")</script> & <b>'
    _write(bundle / "trace.json", doc)
    _write(bundle / "result.json", _result_doc())

    html = render_trace.build(str(root))
    assert '<script>alert' not in html
    assert "&lt;script&gt;alert" in html


# ===========================================================================
# 7. 可选出口：离线 OTLP/JSON
# ===========================================================================
def test_otlp_maps_trace_id_without_touching_the_original(evidence_root):
    """OTel 要 32 hex，MAOS 的 trace_id 是 18 字符业务前缀格式。

    映射只发生在导出这一层：原值必须原样留在 span 属性上，否则映射不可反查；
    而**生成格式一个字都不许改** —— 改了所有既有证据束当场失效。
    """
    bundles = render_trace.load_bundles(str(evidence_root))
    doc = render_trace.to_otlp(bundles)
    spans = doc["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert spans, "导出的 span 不该是空的"
    for span in spans:
        assert re.fullmatch(r"[0-9a-f]{32}", span["traceId"]), span["traceId"]
        assert re.fullmatch(r"[0-9a-f]{16}", span["spanId"]), span["spanId"]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert attrs["maos.trace_id"]["stringValue"] == "trace_fixture01"

    # 同一个业务 trace_id 恒映射到同一个 32 hex，不同的映射到不同的。
    assert render_trace.otlp_trace_id("trace_fixture01") == spans[0]["traceId"]
    assert render_trace.otlp_trace_id("trace_other") != spans[0]["traceId"]


def test_otlp_is_offline_only(evidence_root, tmp_path):
    """边界：只产文件。不引 SDK、不连 collector、不发一个字节出去。"""
    src = SCRIPT.read_text(encoding="utf-8")
    assert "import opentelemetry" not in src and "from opentelemetry" not in src
    for banned in ("urllib.request", "http.client", "requests.", "socket."):
        assert banned not in src, f"渲染器里出现了 {banned!r} —— 它不该有网络能力"

    out = tmp_path / "otlp.json"
    proc = _run("--evidence", str(evidence_root), "--out", str(tmp_path / "r.html"),
                "--otlp", str(out))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    body = out.read_text(encoding="utf-8")
    assert body.startswith("# generated at "), "铁律 3 的出处头对 OTLP 文件同样适用"
    json.loads(body.split("\n", 1)[1])


def test_otlp_is_not_produced_by_default(evidence_root, tmp_path):
    """缺省不产 —— 证据链的缺省行为不许被这个可选出口改变。"""
    out = tmp_path / "r.html"
    assert _run("--evidence", str(evidence_root), "--out", str(out)).returncode == 0
    assert not (tmp_path / "otlp.json").exists()
    assert not (evidence_root / "report-otlp.json").exists()


# ===========================================================================
# 8. 入口契约
# ===========================================================================
def test_empty_evidence_root_fails_loudly(tmp_path):
    """一束都没有时报错退出，不产一份空页面 —— 空页面会被当成「跑通了」。"""
    (tmp_path / "evidence").mkdir()
    proc = _run("--evidence", str(tmp_path / "evidence"),
                "--out", str(tmp_path / "r.html"))
    assert proc.returncode == 2
    assert "make_evidence.py" in proc.stdout
    assert not (tmp_path / "r.html").exists()


def test_render_is_deterministic(evidence_root):
    """同样的证据出同样的字节 —— 否则 `--check` 会在没人动过证据时随机变红。"""
    assert render_trace.build(str(evidence_root)) == render_trace.build(str(evidence_root))


def test_scenario_order_is_stable():
    """数字场景按数字排，R5 之类排在其后 —— 排序不稳定 `--check` 就恒红。"""
    names = ["scenario-10", "scenario-2", "scenario-R5", "scenario-1", "scenario-7"]
    assert sorted(names, key=render_trace.scenario_key) == [
        "scenario-1", "scenario-2", "scenario-7", "scenario-10", "scenario-R5"]
