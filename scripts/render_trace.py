#!/usr/bin/env python3
"""证据束的可视化出口 —— ``evidence/*/trace.json`` → 一张离线单页 HTML。

    python3 scripts/render_trace.py                  # 生成/覆盖 evidence/report.html
    python3 scripts/render_trace.py --check          # 与 evidence/ 不一致即非零退出
    python3 scripts/render_trace.py --otlp           # 另出一份 OTLP/JSON（默认不产）

为什么要有这个脚本：``verify.py`` 的八项重放是这个项目最硬的东西，但它现在**只能读，
不能看**。而 ``evidence/*.db`` 不入库（``.gitignore`` 排掉了 ``*.db``），换台机器直接跑
``verify.py`` 是 4/8 exit 1。一份随证据束落盘的静态 HTML，让人**不跑任何命令**就能看到
全链路 —— 这是本脚本存在的唯一理由。

四条自我约束：

1. **只读 ``evidence/``，不读库、不重算业务事实**。页面上每一个数字都能在
   ``trace.json`` / ``result.json`` / ``INDEX.json`` 里逐字找到出处。这不是洁癖：
   ``*.db`` 不入库，凡是要开库才算得出来的东西，在评委的机器上就是算不出来的。
   ``maos/obs/trace.py`` 的 ``cost_view`` / ``failure_view`` / ``stray_events`` /
   ``unattributed_usage`` 的**产物**早已由 ``export_trace_bundle`` 内嵌进 ``trace.json``
   （分别落在 ``traces[].cost``、``cost.failures``、``stray_events``、
   ``unattributed_usage`` 四个键上），本脚本消费的就是它们 —— 在此之前这四份聚合
   **落盘即终点，零展示消费者**。唯一在渲染时**重跑**的是 ``check_span_tree``：
   它是纯函数，只吃 spans，不需要库。
2. **零外部依赖，断网可看**。生成侧只用标准库；产物侧不许出现任何 ``http://`` /
   ``https://`` —— 不引 CDN、不外链字体、不 fetch、也**不写内联 SVG**
   （``<svg xmlns="http://www.w3.org/2000/svg">`` 本身就是一条外链字面量，
   会让「零外链」这条机器判据变红）。CSS 与 JS 全部内联，图形一律拿 CSS 画。
   ``maos/tests/test_render_trace.py`` 把这条钉成断言。
3. **没有 JavaScript 也必须能读完**。折叠一律用原生 ``<details>``，JS 只做
   「全部展开 / 全部折叠」与锚点跳转这类锦上添花的事。评委双击打开 ``file://``、
   断网、关掉脚本，页面都得是完整的。
4. **它是脚本产物，不是第二份要人工维护的材料**。``artifacts/`` 下那两份手写 HTML
   是前车之鉴：每跑一次场景就要有人回来改，于是没人改。所以本脚本给
   ``--check``（口径同 ``scripts/gen_docs.py``）：证据变了而 HTML 没重生成，它变红。

**``--check`` 比的是第 2 行起的正文，不含首行出处注释。** 首行按铁律 3 写
``<!-- generated at <ISO8601> from <git sha> -->``，里面有时间戳和可能带 ``-dirty``
的 sha —— 把它算进比对，``--check`` 会恒红，那就成了一个永远在响的警报（同
``gen_docs.py`` 自我约束 3 的理由）。页面上**可见**的出处不取自本进程：它取自
``INDEX.json`` 的 ``git_sha`` 与各证据文件自己的首行，那是证据的出处，
不是渲染器的出处 —— 后者放在渲染器自己的注释里就够了。

页面形态刻意**不是**火焰图。整场执行只有几十毫秒（Scripted 模式），按时间比例画
甘特图会退化成一条线，一眼看不出任何东西。所以结构上按 plan → task → event/artifact
的父子关系画树，时间上画**状态迁移的顺序**：顺序是信息，时长不是。
"""

from __future__ import annotations

import argparse
import difflib
import glob
import hashlib
import html
import json
import os
import re
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from maos.obs.trace import (                                    # noqa: E402
    KIND_ARTIFACT,
    KIND_EVENT,
    KIND_PLAN,
    KIND_TASK,
    check_span_tree,
)
from scripts.make_evidence import (                             # noqa: E402
    HEADER_PREFIX,
    git_sha,
    load_evidence_json,
)

DEFAULT_EVIDENCE = os.path.join(ROOT, "evidence")
DEFAULT_OUT = os.path.join(DEFAULT_EVIDENCE, "report.html")
DEFAULT_OTLP = os.path.join(DEFAULT_EVIDENCE, "report-otlp.json")

#: 首行出处注释的形状。``--check`` 认这个前缀，正文比对从第 2 行开始。
HTML_HEADER_PREFIX = "<!-- generated at "
HTML_HEADER_RE = re.compile(r"^<!-- generated at (?P<at>\S+) from (?P<sha>\S+) -->$")

TITLE = "MAOS 证据束 · 全链路视图"


class RenderError(RuntimeError):
    """输入不成立时抛出。宁可什么都不产，也不产一份半真的页面。"""


# ---------------------------------------------------------------------------
# 口径表
# ---------------------------------------------------------------------------
#: 状态 → 配色档。红的两档是派单点名要标红的：返工与人工卡口。
#: FAILED 单独一档（比 BLOCKED 更重），DONE 绿，其余中性 —— 中性档故意占多数，
#: 全页都在闪红等于没有红。
STATE_TONE = {
    "PENDING": "muted",
    "DISPATCHED": "muted",
    "RUNNING": "info",
    "AWAITING_REVIEW": "info",
    "DONE": "ok",
    "REWORK": "danger",
    "BLOCKED": "danger",
    "FAILED": "fatal",
    "CANCELLED": "muted",
}

#: 闸门判定 → 配色档 + 人话。``maos.reason`` 上的原文照抄进页面，这里只加一层解释。
GATE_TONE = {
    "gate_pass": ("ok", "放行"),
    "gate_rework": ("danger", "打回返工"),
    "gate_needs_human": ("danger", "转人工"),
    "gate_fail": ("fatal", "判失败"),
}

#: span kind → 中文标签。四种，与 ``maos/obs/trace.py`` 的 KIND_* 一一对应。
KIND_LABEL = {
    KIND_PLAN: "plan",
    KIND_TASK: "task",
    KIND_EVENT: "event",
    KIND_ARTIFACT: "artifact",
}

#: provenance → 配色档。``unknown`` 是审计链上的洞，必须是最扎眼的一档。
PROV_TONE = {
    "task_result": "ok",
    "compensation_attached": "info",
    "artifact_seeded": "warn",
    "unknown": "fatal",
}


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def esc(value) -> str:
    """任何值 → 可以直接塞进 HTML 的文本。``None`` 印成 em dash，不印 "None"。"""
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return html.escape(json.dumps(value, ensure_ascii=False), quote=True)
    return html.escape(str(value), quote=True)


def num(value) -> str:
    """整数加千分位。取不到就 em dash —— 不拿 0 兜底（0 是一个断言，不是缺省值）。"""
    if value is None:
        return "—"
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return esc(value)


def chip(text: str, tone: str = "muted", *, title: str = "", sup: str = "") -> str:
    attr = f' title="{esc(title)}"' if title else ""
    tail = f'<sup>{esc(sup)}</sup>' if sup else ""
    return f'<span class="chip t-{tone}"{attr}>{esc(text)}{tail}</span>'


def anchor(*parts) -> str:
    """稳定锚点：只留 ASCII 字母数字与连字符，其余压成连字符。"""
    raw = "-".join(str(p) for p in parts)
    return re.sub(r"[^A-Za-z0-9_-]+", "-", raw).strip("-").lower()


def brief(value, limit: int = 160) -> str:
    """长文本截断成一行摘要。截断处印 ``…``，让「这里还有」看得见。"""
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# 读入
# ---------------------------------------------------------------------------
def scenario_key(name: str):
    """``scenario-7`` / ``scenario-R5`` 混排的确定序：数字场景在前，其余按名字。

    排序必须确定 —— ``--check`` 拿字节比对，任何靠 ``os.listdir`` 顺序的东西
    都会让它在没人动过证据的情况下变红。
    """
    tail = name.split("scenario-", 1)[-1]
    return (0, int(tail), "") if tail.isdigit() else (1, 0, tail)


def load_bundles(evidence_root: str) -> list[dict]:
    """扫出 ``<root>/scenario-*/trace.json``，每一束读成一个字典。

    ``result.json`` 缺了不算致命（对照束/子束可能没有），``trace.json`` 缺了就跳过 ——
    没有 trace 就没有这一束可画的东西。两种情况都记在 ``notes`` 里印到页面上，
    不静默吞掉。
    """
    bundles: list[dict] = []
    for path in sorted(glob.glob(os.path.join(evidence_root, "scenario-*")),
                       key=lambda p: scenario_key(os.path.basename(p))):
        if not os.path.isdir(path):
            continue
        name = os.path.basename(path)
        trace_path = os.path.join(path, "trace.json")
        if not os.path.exists(trace_path):
            continue
        notes: list[str] = []
        trace = load_evidence_json(trace_path)
        result = None
        result_path = os.path.join(path, "result.json")
        if os.path.exists(result_path):
            result = load_evidence_json(result_path)
        else:
            notes.append("这一束没有 result.json，任务清单与场景指标缺失")
        bundles.append({
            "name": name,
            "label": name.replace("scenario-", "场景 "),
            "dir": os.path.relpath(path, ROOT),
            "trace": trace,
            "result": result,
            "header": _first_line(trace_path),
            "notes": notes,
        })
    if not bundles:
        raise RenderError(
            f"{evidence_root} 下一束 scenario-*/trace.json 都没有 —— "
            f"先跑 python3 scripts/make_evidence.py")
    return bundles


def load_index(evidence_root: str) -> dict | None:
    path = os.path.join(evidence_root, "INDEX.json")
    if not os.path.exists(path):
        return None
    return {"doc": load_evidence_json(path), "header": _first_line(path)}


def _first_line(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.readline().rstrip("\n")


# ---------------------------------------------------------------------------
# 派生（只做重排，不产生新事实）
# ---------------------------------------------------------------------------
def events_in_order(trace: dict) -> list[dict]:
    """按 ``maos.event.seq`` 升序的事件 span。seq 缺失的排在最后，不丢。"""
    evs = [s for s in trace.get("spans", []) if s.get("kind") == KIND_EVENT]
    return sorted(evs, key=lambda s: (s["attributes"].get("maos.event.seq") is None,
                                      s["attributes"].get("maos.event.seq") or 0))


def state_lanes(trace: dict) -> list[dict]:
    """状态迁移时间线的泳道：plan 一条，每个 task 一条。

    泳道内按 seq 排；``REWORK->PENDING`` 之后同一个 task 会再走一遍
    ``DISPATCHED->RUNNING``，那正是要看见的东西（返工在同一条泳道上重复出现），
    所以**不去重**。
    """
    lanes: dict[str, dict] = {}
    for span in events_in_order(trace):
        attrs = span["attributes"]
        etype = attrs.get("maos.event.type")
        if etype not in ("PlanTransition", "StateTransition"):
            continue
        name = span.get("name", "")
        if ":" not in name or "->" not in name:
            continue
        transition = name.split(":", 1)[1]
        src, _, dst = transition.partition("->")
        key = "«plan»" if etype == "PlanTransition" else (attrs.get("maos.task_id") or "«未归属»")
        lane = lanes.setdefault(key, {"key": key, "is_plan": etype == "PlanTransition",
                                      "first": src, "steps": []})
        lane["steps"].append({
            "seq": attrs.get("maos.event.seq"),
            "state": dst,
            "reason": attrs.get("maos.reason"),
            "detail": attrs.get("maos.detail") or {},
        })
    ordered = [v for v in lanes.values() if v["is_plan"]]
    ordered += [v for v in lanes.values() if not v["is_plan"]]
    return ordered


def gate_decisions(trace: dict) -> list[dict]:
    """每一道闸判了什么。判据是 ``maos.detail`` 里有 ``gate_results``。

    ``reason`` 原文照抄（``gate_pass`` / ``gate_rework`` / ``gate_needs_human``），
    不翻译成自己的词 —— 页面上的判定要和库里的字节对得上。
    """
    out = []
    for span in events_in_order(trace):
        attrs = span["attributes"]
        detail = attrs.get("maos.detail") or {}
        if "gate_results" not in detail:
            continue
        out.append({
            "seq": attrs.get("maos.event.seq"),
            "task_id": attrs.get("maos.task_id"),
            "transition": span.get("name", "").split(":", 1)[-1],
            "reason": attrs.get("maos.reason"),
            "gates": detail.get("gate_results") or {},
            "await": detail.get("await"),
        })
    return out


def artifact_spans(trace: dict) -> list[dict]:
    return [s for s in trace.get("spans", []) if s.get("kind") == KIND_ARTIFACT]


def children_index(spans: list[dict]) -> dict:
    """``parent_span_id`` → 子 span 列表。子列表内的顺序是 spans 的原始顺序。"""
    idx: dict = {}
    for span in spans:
        idx.setdefault(span.get("parent_span_id"), []).append(span)
    return idx


def bundle_stats(bundle: dict) -> dict:
    """一束的横向对比口径。全部取自证据，不重算。"""
    doc = bundle["trace"]
    traces = doc.get("traces", [])
    summary = doc.get("summary") or {}
    gates = [g for t in traces for g in gate_decisions(t)]
    holds = sum(1 for g in gates if g["reason"] == "gate_needs_human")
    reworks = sum(1 for g in gates if g["reason"] == "gate_rework")
    warns = []
    if summary.get("unsourced_artifacts"):
        warns.append(f"来源不明产物 {summary['unsourced_artifacts']}")
    if doc.get("stray_events"):
        warns.append(f"游离事件 {len(doc['stray_events'])}")
    if doc.get("unattributed_usage"):
        warns.append(f"归属不上的用量 {len(doc['unattributed_usage'])}")
    if summary.get("degraded_sandbox_reports"):
        warns.append(f"沙箱降级 {summary['degraded_sandbox_reports']}")
    if summary.get("tree_errors"):
        warns.append(f"树错误 {len(summary['tree_errors'])}")
    return {
        "span_count": summary.get("span_count"),
        "event_count": summary.get("event_count"),
        "trace_count": len(traces),
        "plan_states": [t.get("plan_state") for t in traces],
        "goal": traces[0].get("goal") if traces else None,
        "gate_holds": holds,
        "gate_reworks": reworks,
        "gate_total": len(gates),
        "model_calls": summary.get("model_calls"),
        "warns": warns,
    }


# ---------------------------------------------------------------------------
# 渲染 —— 片段
# ---------------------------------------------------------------------------
def render_overview(bundles: list[dict], stats: list[dict]) -> str:
    rows = []
    for bundle, st in zip(bundles, stats):
        plan_states = " ".join(
            chip(s or "?", STATE_TONE.get(s, "muted")) for s in st["plan_states"]) or "—"
        warn_cell = (" ".join(chip(w, "warn") for w in st["warns"])
                     if st["warns"] else chip("无", "ok"))
        rows.append(f"""      <tr>
        <th scope="row"><a href="#{anchor(bundle['name'])}">{esc(bundle['label'])}</a></th>
        <td class="goal">{esc(brief(st['goal'], 46))}</td>
        <td class="n">{num(st['span_count'])}</td>
        <td class="n">{num(st['event_count'])}</td>
        <td class="n">{num(st['trace_count'])}</td>
        <td>{plan_states}</td>
        <td class="n">{num(st['gate_total'])}</td>
        <td class="n">{_hot(st['gate_holds'])}</td>
        <td class="n">{_hot(st['gate_reworks'])}</td>
        <td class="n">{num(st['model_calls'])}</td>
        <td>{warn_cell}</td>
      </tr>""")
    total_spans = sum(s["span_count"] or 0 for s in stats)
    total_events = sum(s["event_count"] or 0 for s in stats)
    total_traces = sum(s["trace_count"] for s in stats)
    return f"""  <section id="overview">
    <h2>八场横向对比</h2>
    <p class="lede">一行一束证据。「闸门」列是这条链路上被 Gate 判过的次数，
      其中<b>转人工</b>与<b>返工</b>单列 —— 它们是流程被挡住的地方，也是这套东西
      最该被看见的地方。</p>
    <div class="scroll">
    <table class="grid">
      <thead><tr>
        <th scope="col">场景</th><th scope="col">目标</th>
        <th scope="col">span</th><th scope="col">事件</th><th scope="col">plan</th>
        <th scope="col">plan 终态</th>
        <th scope="col">闸门</th><th scope="col">转人工</th><th scope="col">返工</th>
        <th scope="col">模型调用</th><th scope="col">告警</th>
      </tr></thead>
      <tbody>
{chr(10).join(rows)}
      </tbody>
      <tfoot><tr>
        <th scope="row">合计</th><td>{len(bundles)} 束</td>
        <td class="n">{num(total_spans)}</td><td class="n">{num(total_events)}</td>
        <td class="n">{num(total_traces)}</td>
        <td colspan="6"></td>
      </tr></tfoot>
    </table>
    </div>
  </section>"""


def _hot(value) -> str:
    """计数为 0 印中性，非 0 印红。0 也要印出来 —— 「查过了，是 0」和「没查」不一样。"""
    if not value:
        return '<span class="zero">0</span>'
    return f'<b class="hot">{num(value)}</b>'


def render_integrity(bundles: list[dict], recheck: list[dict]) -> str:
    rows = []
    for bundle, chk in zip(bundles, recheck):
        doc = bundle["trace"]
        summary = doc.get("summary") or {}
        recorded = summary.get("tree_errors") or []
        rows.append(f"""      <tr>
        <th scope="row"><a href="#{anchor(bundle['name'])}">{esc(bundle['label'])}</a></th>
        <td>{_errs(recorded)}</td>
        <td>{_errs(chk['errors'])}</td>
        <td class="n">{_hot(len(doc.get('stray_events') or []))}</td>
        <td class="n">{_hot(len(doc.get('unattributed_usage') or []))}</td>
        <td class="n">{_hot(summary.get('unsourced_artifacts'))}</td>
        <td class="n">{num(summary.get('seeded_artifacts'))}</td>
        <td class="n">{_hot(summary.get('degraded_sandbox_reports'))}</td>
      </tr>""")
    disagree = [b["label"] for b, c in zip(bundles, recheck) if c["disagrees"]]
    verdict = (f'<p class="banner bad">导出时记的树错误与本次渲染重算的结果不一致：'
               f'{esc("、".join(disagree))}。以重算为准，并去查 evidence/ 是否被改过。</p>'
               if disagree else
               '<p class="banner good">本次渲染重跑了一遍 <code>check_span_tree</code>，'
               '结论与导出时记录的逐条一致。</p>')
    return f"""  <section id="integrity">
    <h2>审计链完整性</h2>
    <p class="lede">这一块是<b>负面清单</b>：孤儿 span、挂不上树的事件、归属不上的用量、
      来源不明的产物。全是 0 才有资格谈上面那些数字。空也照印 ——
      「查过了，是 0」和「没查」在屏幕上必须分得开。
      「树错误（重算）」列是本页渲染时拿 <code>maos/obs/trace.py::check_span_tree</code>
      对 <code>trace.json</code> 里的 span 重跑一遍的结果，不是抄导出时那个数。</p>
    <div class="scroll">
    <table class="grid">
      <thead><tr>
        <th scope="col">场景</th>
        <th scope="col">树错误（导出时记）</th><th scope="col">树错误（本页重算）</th>
        <th scope="col">游离事件</th><th scope="col">归属不上的用量</th>
        <th scope="col">来源不明产物</th><th scope="col">旁路入库产物</th>
        <th scope="col">沙箱降级</th>
      </tr></thead>
      <tbody>
{chr(10).join(rows)}
      </tbody>
    </table>
    </div>
    {verdict}
  </section>"""


def _errs(errors: list) -> str:
    if not errors:
        return chip("0", "ok")
    items = "".join(f"<li>{esc(e)}</li>" for e in errors)
    return f'<details class="inline"><summary>{chip(str(len(errors)), "fatal")}</summary><ul>{items}</ul></details>'


def render_stray(doc: dict) -> str:
    """游离事件与归属不上的用量 —— 显式展示，不许藏。

    两张表都是「有洞就让洞看得见」那一类。空的时候印一句「已查，0 条」，
    非空的时候把每一行摊开：藏起来等于把审计链上的洞抹平。
    """
    blocks = []
    strays = doc.get("stray_events") or []
    if strays:
        rows = "".join(
            f"<tr><td>{esc(r.get('event_type'))}</td><td class=\"mono\">{esc(r.get('event_id'))}</td>"
            f"<td class=\"mono\">{esc(r.get('task_id'))}</td><td class=\"mono\">{esc(r.get('plan_id'))}</td>"
            f"<td>{esc(brief(r.get('reason') or r.get('detail')))}</td></tr>"
            for r in strays)
        blocks.append(
            f'<h4 class="bad">游离事件 {len(strays)} 条 —— 挂不上任何 span 树</h4>'
            f'<div class="scroll"><table class="grid"><thead><tr><th>事件类型</th><th>event_id</th>'
            f'<th>task_id</th><th>plan_id</th><th>摘要</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div>')
    else:
        blocks.append('<p class="ok-line">游离事件：<b>0 条</b>（已查 '
                      '<code>trace.json → stray_events</code>，不是没查）。</p>')

    orphan = doc.get("unattributed_usage") or []
    if orphan:
        rows = "".join(
            f"<tr><td>{esc(r.get('agent_role'))}</td><td class=\"mono\">{esc(r.get('call_site'))}</td>"
            f"<td class=\"n\">{num(r.get('tokens_in'))}</td><td class=\"n\">{num(r.get('tokens_out'))}</td>"
            f"<td class=\"n\">{num(r.get('latency_ms'))}</td><td>{esc(r.get('estimated'))}</td></tr>"
            for r in orphan)
        blocks.append(
            f'<h4 class="bad">归属不上的模型用量 {len(orphan)} 条 —— 有花销，指不到是谁花的</h4>'
            f'<div class="scroll"><table class="grid"><thead><tr><th>角色</th><th>调用点</th>'
            f'<th>tokens_in</th><th>tokens_out</th><th>latency_ms</th><th>estimated</th>'
            f'</tr></thead><tbody>{rows}</tbody></table></div>')
    else:
        blocks.append('<p class="ok-line">归属不上的模型用量：<b>0 条</b>（已查 '
                      '<code>trace.json → unattributed_usage</code>）。</p>')
    return "".join(blocks)


def render_lanes(trace: dict) -> str:
    lanes = state_lanes(trace)
    if not lanes:
        return '<p class="ok-line">这条 trace 没有状态迁移事件。</p>'
    rows = []
    for lane in lanes:
        chips = [chip(lane["first"], STATE_TONE.get(lane["first"], "muted"), title="起始状态")]
        for step in lane["steps"]:
            tone = STATE_TONE.get(step["state"], "muted")
            hint = f"seq={step['seq']}"
            if step["reason"]:
                hint += f" · {step['reason']}"
            extra = step["detail"].get("operator") or step["detail"].get("await")
            if extra:
                hint += f" · {extra}"
            chips.append('<span class="arrow">→</span>')
            chips.append(chip(step["state"], tone, title=hint, sup=str(step["seq"])))
        label = "计划" if lane["is_plan"] else lane["key"]
        rows.append(f'<tr><th scope="row" class="mono">{esc(label)}</th>'
                    f'<td class="lane">{"".join(chips)}</td></tr>')
    return (f'<div class="scroll"><table class="grid lanes"><tbody>'
            f'{"".join(rows)}</tbody></table></div>')


def render_gates(trace: dict) -> str:
    gates = gate_decisions(trace)
    if not gates:
        return '<p class="ok-line">这条 trace 上没有一次 Gate 判定。</p>'
    rows = []
    for g in gates:
        tone, word = GATE_TONE.get(g["reason"], ("muted", "—"))
        cells = " ".join(
            chip(f"{k}:{v}", "ok" if v == "pass" else "fatal")
            for k, v in g["gates"].items())
        await_cell = chip(g["await"], "danger") if g["await"] else "—"
        rows.append(
            f'<tr><td class="n">{esc(g["seq"])}</td>'
            f'<td class="mono">{esc(g["task_id"])}</td>'
            f'<td class="mono">{esc(g["transition"])}</td>'
            f'<td class="nw">{chip(g["reason"] or "?", tone)}'
            f'<span class="sub">{esc(word)}</span></td>'
            f'<td class="gates">{cells}</td><td>{await_cell}</td></tr>')
    return (f'<div class="scroll"><table class="grid"><thead><tr>'
            f'<th>seq</th><th>task</th><th>迁移</th><th>判定（reason 原文）</th>'
            f'<th>各闸结果</th><th>等谁</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


def render_artifacts(trace: dict) -> str:
    arts = artifact_spans(trace)
    if not arts:
        return '<p class="ok-line">这条 trace 没有产物 span。</p>'
    rows = []
    for span in arts:
        a = span["attributes"]
        prov = a.get("maos.artifact.provenance")
        source = a.get("maos.artifact.provenance.source")
        note = a.get("maos.artifact.provenance.note")
        mode = a.get("maos.artifact.sandbox.mode")
        degraded = a.get("maos.artifact.sandbox.degraded_reason")
        why = ""
        if note:
            why = f'<details class="inline"><summary>入库路径说明</summary><p>{esc(note)}</p></details>'
        deg = (f'<details class="inline"><summary>{chip("降级", "warn")}</summary>'
               f'<p>{esc(degraded)}</p></details>') if degraded else "—"
        rows.append(
            f'<tr><td class="mono">{esc(a.get("maos.artifact.id"))}</td>'
            f'<td>{esc(a.get("maos.artifact.kind"))}</td>'
            f'<td class="n">v{esc(a.get("maos.artifact.version"))}</td>'
            f'<td class="mono">{esc(a.get("maos.task_id"))}</td>'
            f'<td>{chip(prov or "?", PROV_TONE.get(prov, "muted"))}</td>'
            f'<td class="srcfn small">{esc(source)}{why}</td>'
            f'<td>{esc(mode)}</td><td>{deg}</td></tr>')
    return (f'<div class="scroll"><table class="grid"><thead><tr>'
            f'<th>artifact_id</th><th>kind</th><th>版本</th><th>task</th>'
            f'<th>provenance</th><th>来源函数</th><th>沙箱</th><th>降级原因</th>'
            f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div>')


def render_cost(trace: dict) -> str:
    """成本与失败**两块分开**，且「取不到」与「一次都没花」必须分得开。

    这是照抄 ``cost_view`` 的口径，不是这里新发明的：合成一个 0 会让
    「成本记账没接上」长得像「这条链路很省」，而把失败并进成本会让
    「这条链路很省」和「这条链路一直在失败」长成同一件事。
    """
    cost = trace.get("cost") or {}
    fails = cost.get("failures") or {}

    if not cost.get("available", False):
        left_head = (f'<p class="banner bad">成本<b>取不到</b>：'
                     f'{esc(cost.get("unavailable_reason") or "未说明")}。'
                     f'这不是 0，是不知道。</p>')
        left_body = ""
    else:
        calls = cost.get("calls") or 0
        flag = (chip("全部为估算", "warn") if cost.get("all_estimated")
                else chip(f"实测 {num(cost.get('measured_calls'))} 次", "ok"))
        left_head = (
            f'<p class="figures">'
            f'<span><b>{num(calls)}</b><i>调用</i></span>'
            f'<span><b>{num(cost.get("tokens_in"))}</b><i>tokens_in</i></span>'
            f'<span><b>{num(cost.get("tokens_out"))}</b><i>tokens_out</i></span>'
            f'<span><b>{num(cost.get("tokens_total"))}</b><i>tokens 合计</i></span>'
            f'</p><p>{flag}</p>')
        if cost.get("zero_calls_note"):
            left_head += f'<p class="banner warn-b">{esc(cost["zero_calls_note"])}</p>'
        left_body = (_kv_table("角色", "role", cost.get("by_role"))
                     + _kv_table("调用点", "call_site", cost.get("by_call_site"))
                     + _kv_table("任务", "task_id", cost.get("by_task")))
    left_note = f'<p class="note">{esc(cost.get("note"))}</p>' if cost.get("note") else ""

    if not fails.get("available", False):
        right = (f'<p class="banner bad">失败记账<b>取不到</b>：'
                 f'{esc(fails.get("unavailable_reason") or "未说明")}。'
                 f'不等于「一次没失败」。</p>')
    else:
        fcalls = fails.get("calls") or 0
        right = (f'<p class="figures">'
                 f'<span><b class="{"hot" if fcalls else ""}">{num(fcalls)}</b><i>失败调用</i></span>'
                 f'<span><b>{num(fails.get("latency_ms"))}</b><i>失败耗时 ms</i></span></p>')
        if fcalls:
            right += (_rows_table("错误类型", [("error_kind", "错误类型"), ("calls", "次数")],
                                  fails.get("by_error_kind"))
                      + _rows_table("调用点", [("call_site", "调用点"), ("calls", "次数")],
                                    fails.get("by_call_site")))
        else:
            right += ('<p class="ok-line">查过了，<b>0 次失败</b> —— '
                      '这一条是「知道且为零」，不是「不知道」。</p>')
    right_note = f'<p class="note">{esc(fails.get("note"))}</p>' if fails.get("note") else ""

    return f"""<div class="split">
      <div class="pane"><h4>成本（成功调用）</h4>{left_head}{left_body}{left_note}</div>
      <div class="pane"><h4>失败调用（不并进左边任何一个数）</h4>{right}{right_note}</div>
    </div>"""


def _kv_table(title: str, key: str, rows) -> str:
    if not rows:
        return ""
    body = "".join(
        f'<tr><td class="mono">{esc(r.get(key))}</td><td class="n">{num(r.get("calls"))}</td>'
        f'<td class="n">{num(r.get("tokens_in"))}</td><td class="n">{num(r.get("tokens_out"))}</td>'
        f'<td class="n">{num(r.get("tokens_total"))}</td></tr>' for r in rows)
    return (f'<details class="inline"><summary>按{esc(title)}拆（{len(rows)} 行）</summary>'
            f'<div class="scroll"><table class="grid small"><thead><tr><th>{esc(title)}</th>'
            f'<th>调用</th><th>in</th><th>out</th><th>合计</th></tr></thead>'
            f'<tbody>{body}</tbody></table></div></details>')


def _rows_table(title: str, cols: list[tuple], rows) -> str:
    if not rows:
        return ""
    head = "".join(f"<th>{esc(label)}</th>" for _, label in cols)
    body = "".join("<tr>" + "".join(
        f'<td class="mono">{esc(r.get(k))}</td>' for k, _ in cols) + "</tr>" for r in rows)
    return (f'<div class="scroll"><table class="grid small"><thead><tr>{head}</tr></thead>'
            f'<tbody>{body}</tbody></table></div>')


def render_tree(trace: dict) -> str:
    """结构树：plan → task → event → artifact，按 ``parent_span_id`` 递归。

    父子关系一律从数据里来。挂不到任何父亲的 span 单列一段 —— 那说明树有洞，
    洞要让人看见（``check_span_tree`` 会同时把它报成孤儿）。
    """
    spans = trace.get("spans", [])
    by_parent = children_index(spans)
    ids = {s.get("span_id") for s in spans}
    roots = [s for s in spans if s.get("parent_span_id") is None]
    orphans = [s for s in spans
               if s.get("parent_span_id") is not None and s.get("parent_span_id") not in ids]
    out = ['<ul class="tree">']
    for root in roots:
        out.append(_tree_node(root, by_parent, depth=0))
    out.append("</ul>")
    if orphans:
        out.append(f'<h4 class="bad">孤儿 span {len(orphans)} 条 —— 父亲不在树内</h4><ul class="tree">')
        for span in orphans:
            out.append(_tree_node(span, by_parent, depth=0))
        out.append("</ul>")
    return "".join(out)


def _tree_node(span: dict, by_parent: dict, *, depth: int) -> str:
    kids = by_parent.get(span.get("span_id")) or []
    kind = span.get("kind")
    label = (f'<span class="k k-{esc(kind)}">{esc(KIND_LABEL.get(kind, kind))}</span>'
             f'<span class="nm">{esc(span.get("name"))}</span>{_span_tags(span)}')
    if not kids:
        return f'<li class="leaf">{label}{_span_meta(span)}</li>'
    inner = "".join(_tree_node(k, by_parent, depth=depth + 1) for k in kids)
    # 前两层默认展开（plan / task），再往下默认收起 —— 一棵 103 span 的树全摊开没人读得完。
    opened = " open" if depth < 2 else ""
    return (f'<li><details{opened}><summary>{label}'
            f'<span class="cnt">{len(kids)}</span></summary>{_span_meta(span)}'
            f'<ul>{inner}</ul></details></li>')


def _span_tags(span: dict) -> str:
    """挂在节点标题上的几枚要紧标签：状态、风险、判定、provenance。"""
    a = span["attributes"]
    tags = []
    state = a.get("maos.task.state") or a.get("maos.plan.state")
    if state:
        tags.append(chip(state, STATE_TONE.get(state, "muted")))
    if a.get("maos.task.role"):
        tags.append(chip(a["maos.task.role"], "info"))
    if a.get("maos.task.attempt") and a["maos.task.attempt"] > 1:
        tags.append(chip(f"第 {a['maos.task.attempt']} 次", "danger", title="返工后重跑"))
    risk = a.get("maos.task.effect_risk")
    if risk in ("H", "M"):
        tags.append(chip(f"外部影响 {risk}", "warn" if risk == "M" else "danger"))
    reason = a.get("maos.reason")
    if reason in GATE_TONE:
        tags.append(chip(GATE_TONE[reason][1], GATE_TONE[reason][0], title=reason))
    prov = a.get("maos.artifact.provenance")
    if prov:
        tags.append(chip(prov, PROV_TONE.get(prov, "muted")))
    if a.get("maos.artifact.sandbox.degraded_reason"):
        tags.append(chip("沙箱降级", "warn"))
    return "".join(tags)


def _span_meta(span: dict) -> str:
    """节点下的一行细节：人话 reason 优先，其次 detail 摘要。"""
    a = span["attributes"]
    bits = []
    if a.get("maos.reason") and a["maos.reason"] not in GATE_TONE:
        bits.append(f'<span class="why">{esc(a["maos.reason"])}</span>')
    detail = a.get("maos.detail") or {}
    keys = [k for k in ("operator", "note", "await", "status", "error", "actor",
                        "duration_ms", "invocation_id") if detail.get(k) is not None]
    for k in keys:
        bits.append(f'<span class="kv"><i>{esc(k)}</i>{esc(brief(detail[k], 90))}</span>')
    depends = a.get("maos.task.depends_on")
    if depends:
        bits.append(f'<span class="kv"><i>依赖</i>{esc("、".join(depends))}</span>')
    if not bits:
        return ""
    return f'<div class="meta">{"".join(bits)}</div>'


def render_event_log(trace: dict) -> str:
    rows = []
    for span in events_in_order(trace):
        a = span["attributes"]
        detail = a.get("maos.detail") or {}
        keys = [k for k in ("status", "error", "await", "operator", "actor", "duration_ms")
                if detail.get(k) is not None]
        summary = " · ".join(f"{k}={brief(detail[k], 40)}" for k in keys)
        rows.append(
            f'<tr><td class="n">{esc(a.get("maos.event.seq"))}</td>'
            f'<td>{esc(a.get("maos.event.type"))}</td>'
            f'<td class="mono">{esc(span.get("name"))}</td>'
            f'<td class="mono">{esc(a.get("maos.task_id"))}</td>'
            f'<td>{esc(a.get("maos.reason"))}</td>'
            f'<td class="small">{esc(summary)}</td></tr>')
    return (f'<div class="scroll"><table class="grid small"><thead><tr>'
            f'<th>seq</th><th>类型</th><th>事件</th><th>task</th><th>reason</th><th>detail 摘要</th>'
            f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div>')


def render_tasks(result_plan: dict | None) -> str:
    if not result_plan or not result_plan.get("tasks"):
        return ""
    rows = "".join(
        f'<tr><td class="mono">{esc(t.get("task_id"))}</td><td>{esc(t.get("role"))}</td>'
        f'<td>{esc(t.get("title"))}</td>'
        f'<td>{chip(t.get("state") or "?", STATE_TONE.get(t.get("state"), "muted"))}</td>'
        f'<td class="n">{esc(t.get("attempt"))}</td>'
        f'<td>{esc(t.get("risk_level"))} / {esc(t.get("effect_risk"))}</td></tr>'
        for t in result_plan["tasks"])
    return (f'<div class="scroll"><table class="grid"><thead><tr><th>task_id</th><th>角色</th>'
            f'<th>标题</th><th>终态</th><th>尝试</th><th>风险 / 外部影响</th>'
            f'</tr></thead><tbody>{rows}</tbody></table></div>')


#: 每条 plan 印哪几个指标。全部取自 ``result.json`` 的 ``plans[].metrics``。
#: 后三个非 0 就标红 —— 返工、重规划、补偿都是「这条链路走得不顺」的信号，
#: 混在一排中性数字里等于没印。
PLAN_METRICS = (
    ("duration_ms", "墙钟 ms", False),
    ("event_count", "事件", False),
    ("skill_invocations", "skill 调用", False),
    ("tool_invocations", "tool 调用", False),
    ("rework_count", "返工", True),
    ("replan_count", "重规划", True),
    ("compensation_count", "补偿", True),
)


def render_metrics(result_plan: dict | None) -> str:
    metrics = (result_plan or {}).get("metrics") or {}
    if not metrics:
        return ""
    cells = []
    for key, label, hot in PLAN_METRICS:
        if metrics.get(key) is None:
            continue
        cls = ' class="hot"' if hot and metrics.get(key) else ""
        cells.append(f'<span><b{cls}>{num(metrics[key])}</b><i>{esc(label)}</i></span>')
    return f'<p class="figures fig-s">{"".join(cells)}</p>' if cells else ""


def render_outcome(result_plan: dict | None) -> str:
    """业务结论块。铁律 8：MAOS 不持有权威事实，所以这里必须印出 basis 与那句 note。"""
    outcome = (result_plan or {}).get("business_outcome")
    if not outcome:
        return ""
    status = outcome.get("status")
    tone = {"succeeded": "ok", "failed": "fatal"}.get(status, "warn")
    ev = outcome.get("external_evidence") or []
    if ev:
        rows = "".join(
            "<tr>" + "".join(
                f'<td class="mono small">{esc(v)}</td>'
                for v in (e.get("kind"), e.get("artifact_id") or e.get("request_id"),
                          e.get("task_id") or e.get("case_id"),
                          e.get("observed_state") or e.get("passed"),
                          e.get("provenance"))) + "</tr>" for e in ev)
        ev_html = (f'<div class="scroll"><table class="grid small"><thead><tr><th>kind</th>'
                   f'<th>id</th><th>归属</th><th>外部判据</th><th>provenance</th></tr></thead>'
                   f'<tbody>{rows}</tbody></table></div>')
    else:
        ev_html = '<p class="ok-line">没有外部判据行 —— 结论只能靠 plan 自身状态推。</p>'
    return (f'<p class="figures"><span>{chip(status or "?", tone)}<i>业务结论</i></span>'
            f'<span><b class="txt">{esc(outcome.get("basis"))}</b><i>依据</i></span>'
            f'<span><b>{num(outcome.get("unaudited_evidence_count"))}</b><i>未审计判据</i></span>'
            f'</p>{ev_html}<p class="note">{esc(outcome.get("note"))}</p>')


def render_bundle(bundle: dict) -> str:
    doc = bundle["trace"]
    result = bundle.get("result") or {}
    plans = {p.get("plan_id"): p for p in result.get("plans", [])}
    aid = anchor(bundle["name"])
    notes = "".join(f'<p class="banner warn-b">{esc(n)}</p>' for n in bundle["notes"])

    sections = []
    for i, trace in enumerate(doc.get("traces", [])):
        plan = plans.get(trace.get("plan_id"))
        tsum = trace.get("summary") or {}
        state = trace.get("plan_state")
        sections.append(f"""      <article class="trace" id="{aid}-t{i}">
        <h3>{chip(state or "?", STATE_TONE.get(state, "muted"))}
          <span class="goal">{esc(trace.get("goal"))}</span></h3>
        <p class="ids"><code>{esc(trace.get("plan_id"))}</code>
          <code>{esc(trace.get("trace_id"))}</code>
          <span>{num(tsum.get("span_count"))} span · {num(tsum.get("task_count"))} task ·
          {num(tsum.get("event_count"))} 事件 · {num(tsum.get("artifact_count"))} 产物</span></p>
        {render_metrics(plan)}

        <h4>任务清单</h4>
        {render_tasks(plan)}

        <h4>状态迁移时间线</h4>
        <p class="lede">顺序是信息，时长不是 —— 整场执行只有几十毫秒，按时间比例画
          会退化成一条线。返工（<b class="hot">REWORK</b>）与等人（<b class="hot">BLOCKED</b>）
          标红；同一条泳道上重复出现的 DISPATCHED 就是那次返工重跑。
          鼠标停在状态上看 seq 与 reason。</p>
        {render_lanes(trace)}

        <h4>闸门判定</h4>
        {render_gates(trace)}

        <h4>产物溯源</h4>
        {render_artifacts(trace)}

        <h4>成本与失败</h4>
        {render_cost(trace)}

        <h4>业务结论</h4>
        {render_outcome(plan)}

        <details class="block"><summary>结构树（{num(tsum.get("span_count"))} span，
          plan → task → event → artifact）</summary>{render_tree(trace)}</details>
        <details class="block"><summary>完整事件序列（{num(tsum.get("event_count"))} 条）</summary>
          {render_event_log(trace)}</details>
      </article>""")

    wall = (f" · 端到端墙钟 {num(result.get('wall_ms'))} ms · exit={esc(result.get('exit_code'))}"
            if result.get("wall_ms") is not None else "")
    return f"""  <section class="scenario" id="{aid}">
    <h2>{esc(bundle['label'])}<span class="sub">{esc(bundle['dir'])}{wall}</span></h2>
    {notes}
    <p class="prov"><i>出处</i><code>{esc(bundle['header'])}</code></p>
    <h4>挂不上树的东西</h4>
    {render_stray(doc)}
{chr(10).join(sections)}
  </section>"""


# ---------------------------------------------------------------------------
# 渲染 —— 整页
# ---------------------------------------------------------------------------
def render_report(bundles: list[dict], index: dict | None = None) -> str:
    """整页 HTML 的**正文**（不含首行出处注释）。纯函数：同样的证据出同样的字节。"""
    stats = [bundle_stats(b) for b in bundles]
    recheck = []
    for b in bundles:
        errs = []
        for trace in b["trace"].get("traces", []):
            errs.extend(check_span_tree(trace.get("spans", [])))
        recorded = (b["trace"].get("summary") or {}).get("tree_errors") or []
        recheck.append({"errors": errs, "disagrees": bool(errs) != bool(recorded)})

    nav = " ".join(f'<a href="#{anchor(b["name"])}">{esc(b["label"])}</a>' for b in bundles)
    total_spans = sum(s["span_count"] or 0 for s in stats)
    idx_doc = (index or {}).get("doc") or {}
    idx_sha = idx_doc.get("git_sha")

    body = [
        "<!doctype html>",
        '<html lang="zh-CN">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{esc(TITLE)}</title>",
        f"<style>{CSS}</style>",
        "</head>",
        "<body>",
        f"""<header>
  <h1>{esc(TITLE)}</h1>
  <p class="lede">这一页由 <code>scripts/render_trace.py</code> 从
    <code>evidence/scenario-*/trace.json</code> 直接渲染，
    <b>不读数据库、不连网、不引一个外部文件</b>。
    页面上每一个数字都能在 <code>evidence/</code> 里逐字找到出处；
    改了证据不重跑本脚本，<code>python3 scripts/render_trace.py --check</code> 会变红。</p>
  <p class="figures">
    <span><b>{len(bundles)}</b><i>证据束</i></span>
    <span><b>{num(sum(s['trace_count'] for s in stats))}</b><i>plan</i></span>
    <span><b>{num(total_spans)}</b><i>span</i></span>
    <span><b>{num(sum(s['event_count'] or 0 for s in stats))}</b><i>事件</i></span>
    <span><b>{num(sum(s['gate_holds'] for s in stats))}</b><i>转人工</i></span>
    <span><b>{num(sum(s['gate_reworks'] for s in stats))}</b><i>返工</i></span>
  </p>
  <p class="prov"><i>证据出处</i>
    <code>{esc((index or {}).get("header") or idx_sha or "INDEX.json 缺失，出处见各束首行")}</code>
    <span>—— <code>evidence/INDEX.json</code> 首行；每一束自己的出处印在该束标题下</span></p>
  <nav>{nav}</nav>
  <p class="tools"><button type="button" data-all="open">全部展开</button>
    <button type="button" data-all="close">全部折叠</button>
    <span class="hint">没有 JavaScript 也读得完：折叠一律是原生 &lt;details&gt;，
      两个按钮只是省你几次点击。</span></p>
</header>""",
        "<main>",
        render_overview(bundles, stats),
        render_integrity(bundles, recheck),
    ]
    for bundle in bundles:
        body.append(render_bundle(bundle))
    body.append("</main>")
    body.append("""<footer>
  <p>本页是 <code>evidence/</code> 的投影，不是第二份材料。证据一变就该重跑
    <code>python3 scripts/render_trace.py</code>；
    <code>--check</code> 是那道守卫。首行注释里的出处是<b>渲染器</b>跑在哪个 sha 上，
    页面里印的是<b>证据</b>出自哪个 sha —— 两者可以不同，都不许没有。</p>
</footer>""")
    body.append(f"<script>{JS}</script>")
    body.append("</body>")
    body.append("</html>")
    return "\n".join(body) + "\n"


CSS = """
:root{
  --bg:#f6f5f2; --panel:#fff; --ink:#1b1a16; --dim:#6b675e; --line:#dcd8cf;
  --ok:#166534; --ok-bg:#dcfce7; --warn:#92400e; --warn-bg:#fef0c7;
  --danger:#b02020; --danger-bg:#fde2e2; --fatal:#fff; --fatal-bg:#b02020;
  --info:#1e4fa8; --info-bg:#dbe7fe; --muted:#57534e; --muted-bg:#ebe8e2;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;
}
@media (prefers-color-scheme:dark){
  :root{
    --bg:#16151a; --panel:#1e1d24; --ink:#eae7e1; --dim:#a29d94; --line:#343139;
    --ok:#86efac; --ok-bg:#14351f; --warn:#fcd34d; --warn-bg:#3a2c0a;
    --danger:#fca5a5; --danger-bg:#3d1616; --fatal:#fff; --fatal-bg:#8c1d1d;
    --info:#a5c8ff; --info-bg:#152647; --muted:#c6c1b8; --muted-bg:#2a282f;
  }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:14px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB",
  "Microsoft YaHei","Noto Sans CJK SC",system-ui,sans-serif;
  -webkit-font-smoothing:antialiased}
header,main,footer{max-width:1180px;margin:0 auto;padding:0 20px}
header{padding-top:34px;padding-bottom:18px}
h1{font-size:26px;margin:0 0 10px;letter-spacing:-.01em}
h2{font-size:19px;margin:0 0 4px;letter-spacing:-.01em}
h3{font-size:15px;margin:22px 0 6px;font-weight:600}
h4{font-size:13px;margin:20px 0 6px;text-transform:none;color:var(--dim);
  font-weight:700;letter-spacing:.04em}
h4.bad{color:var(--danger)}
p{margin:6px 0}
a{color:var(--info);text-decoration:none;border-bottom:1px solid transparent}
a:hover{border-bottom-color:currentColor}
code{font-family:var(--mono);font-size:.88em;background:var(--muted-bg);
  padding:1px 5px;border-radius:4px}
.lede{color:var(--dim);max-width:78ch}
.note{color:var(--dim);font-size:12px;line-height:1.55;margin-top:8px;
  border-left:2px solid var(--line);padding-left:10px;max-width:88ch}
.sub{color:var(--dim);font-weight:400;font-size:12px;margin-left:8px}
.small{font-size:12px}
.mono{font-family:var(--mono);font-size:12px}
table.grid td.mono,table.grid th.mono,td.nw{white-space:nowrap}
.srcfn{font-family:var(--mono);font-size:12px;word-break:break-all}
.nw .sub{margin-left:6px}
.n{text-align:right;font-variant-numeric:tabular-nums;font-family:var(--mono)}
.zero{color:var(--dim)}
.hot{color:var(--danger)}
.goal{color:var(--ink)}
.prov{font-size:12px;color:var(--dim)}
.prov i{font-style:normal;margin-right:8px;color:var(--dim);
  text-transform:uppercase;letter-spacing:.08em;font-size:10px}
nav{margin:14px 0 6px;display:flex;flex-wrap:wrap;gap:6px}
nav a{background:var(--panel);border:1px solid var(--line);border-radius:999px;
  padding:3px 11px;font-size:12px}
.tools{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:12px}
.tools button{font:inherit;font-size:12px;padding:3px 11px;border-radius:6px;
  border:1px solid var(--line);background:var(--panel);color:var(--ink);cursor:pointer}
.tools button:hover{border-color:var(--info)}
.hint{font-size:11px;color:var(--dim)}
.figures{display:flex;flex-wrap:wrap;gap:22px;margin:14px 0}
.figures span{display:flex;flex-direction:column;gap:1px}
.figures b{font-size:20px;font-family:var(--mono);font-weight:600;
  font-variant-numeric:tabular-nums}
.figures b.txt{font-size:14px}
.figures.fig-s{gap:18px;margin:8px 0 2px}
.figures.fig-s b{font-size:15px}
.figures i{font-style:normal;font-size:11px;color:var(--dim);letter-spacing:.03em}
section{background:var(--panel);border:1px solid var(--line);border-radius:10px;
  padding:18px 20px;margin:18px 0}
.trace{border-top:1px solid var(--line);margin-top:22px;padding-top:6px}
.ids{font-size:12px;color:var(--dim);display:flex;gap:10px;flex-wrap:wrap}
.scroll{overflow-x:auto;margin:6px 0 2px}
table.grid{border-collapse:collapse;width:100%;font-size:13px}
table.grid.small{font-size:12px}
table.grid th,table.grid td{border-bottom:1px solid var(--line);
  padding:5px 9px;text-align:left;vertical-align:top}
table.grid thead th{font-size:11px;color:var(--dim);text-transform:uppercase;
  letter-spacing:.05em;white-space:nowrap;border-bottom:1px solid var(--ink)}
table.grid tbody tr:hover{background:var(--muted-bg)}
table.grid tfoot th,table.grid tfoot td{border-top:1px solid var(--ink);
  border-bottom:none;font-weight:600}
table.lanes th{white-space:nowrap;font-family:var(--mono);font-size:12px;font-weight:500}
.lane{line-height:2.2}
.arrow{color:var(--dim);margin:0 3px}
.chip{display:inline-block;padding:1px 7px;border-radius:5px;font-size:11px;
  font-family:var(--mono);white-space:nowrap;margin-right:3px}
.chip sup{font-size:8px;opacity:.65;margin-left:3px}
.t-ok{background:var(--ok-bg);color:var(--ok)}
.t-warn{background:var(--warn-bg);color:var(--warn)}
.t-danger{background:var(--danger-bg);color:var(--danger);font-weight:600}
.t-fatal{background:var(--fatal-bg);color:var(--fatal);font-weight:600}
.t-info{background:var(--info-bg);color:var(--info)}
.t-muted{background:var(--muted-bg);color:var(--muted)}
.gates .chip{margin-bottom:2px}
.banner{border-radius:7px;padding:8px 12px;font-size:12.5px;margin:10px 0}
.banner.good{background:var(--ok-bg);color:var(--ok)}
.banner.bad{background:var(--danger-bg);color:var(--danger)}
.banner.warn-b{background:var(--warn-bg);color:var(--warn)}
.ok-line{font-size:12.5px;color:var(--dim)}
.split{display:grid;grid-template-columns:1fr 1fr;gap:20px;align-items:start}
@media (max-width:820px){.split{grid-template-columns:1fr}}
.pane{border:1px solid var(--line);border-radius:8px;padding:12px 14px}
.pane h4{margin-top:0}
details.block{margin:14px 0 0;border:1px solid var(--line);border-radius:8px;
  padding:8px 12px;background:var(--bg)}
details.block>summary{cursor:pointer;font-size:12.5px;font-weight:600;color:var(--dim)}
details.inline{display:inline-block}
details.inline>summary{cursor:pointer;font-size:11px;color:var(--info)}
details.inline p,details.inline ul{font-size:12px;color:var(--dim);
  max-width:70ch;margin:4px 0}
ul.tree,ul.tree ul{list-style:none;margin:6px 0 0;padding-left:16px}
ul.tree{padding-left:0}
ul.tree ul{border-left:1px solid var(--line)}
ul.tree li{margin:2px 0}
ul.tree summary{cursor:pointer}
ul.tree summary::marker{color:var(--dim)}
.k{display:inline-block;min-width:52px;font-family:var(--mono);font-size:10px;
  color:var(--dim);text-transform:uppercase;letter-spacing:.05em}
.k-plan{color:var(--info)}
.k-task{color:var(--ok)}
.k-artifact{color:var(--warn)}
.nm{font-family:var(--mono);font-size:12px;margin-right:8px}
.cnt{font-size:10px;color:var(--dim);margin-left:6px;font-family:var(--mono)}
.meta{margin:1px 0 4px 52px;font-size:11.5px;color:var(--dim)}
.why{margin-right:12px}
.kv{margin-right:12px;white-space:nowrap}
.kv i{font-style:normal;opacity:.7;margin-right:4px}
footer{padding:8px 20px 46px;color:var(--dim);font-size:12px;max-width:1180px}
@media print{
  body{background:#fff}
  nav,.tools{display:none}
  details{display:block}
  details>summary{display:none}
  section{break-inside:avoid;border-color:#bbb}
}
"""

JS = """
document.querySelectorAll('[data-all]').forEach(function(btn){
  btn.addEventListener('click', function(){
    var open = btn.dataset.all === 'open';
    document.querySelectorAll('details').forEach(function(d){ d.open = open; });
  });
});
function revealHash(){
  var id = location.hash.slice(1);
  if (!id) return;
  var el = document.getElementById(id);
  while (el) { if (el.tagName === 'DETAILS') el.open = true; el = el.parentElement; }
}
window.addEventListener('hashchange', revealHash);
revealHash();
"""


# ---------------------------------------------------------------------------
# 可选出口：离线 OTLP/JSON
# ---------------------------------------------------------------------------
#: OTel 规范要求 trace_id 是 16 字节（32 hex），而 MAOS 的 ``trace_id`` 是 18 字符的
#: 业务前缀格式（``trace_b81e77f2522c``，见 docs/gateway-rationale.md）。**不改生成格式**
#: —— 改了所有既有证据束当场失效。所以在导出这一层做一次确定性映射，并把原值原样挂在
#: 每条 span 的 ``maos.trace_id`` 属性上，映射随时可反查。span_id 本来就是 16 hex，直接用。
OTLP_TRACE_ID_NOTE = (
    "trace_id = sha256(<maos trace_id>).hexdigest()[:32]；原值保留在每条 span 的 "
    "maos.trace_id 属性里。span_id 原本就是 16 hex，原样使用。")


def otlp_trace_id(trace_id: str) -> str:
    return hashlib.sha256((trace_id or "").encode("utf-8")).hexdigest()[:32]


def _unix_nano(iso: str | None) -> str:
    """ISO8601 → OTLP 的 ``*UnixNano`` 字符串。取不到就 "0" —— 不编时间。"""
    if not iso:
        return "0"
    try:
        return str(int(datetime.fromisoformat(iso).timestamp() * 1_000_000_000))
    except ValueError:
        return "0"


def _otlp_value(value):
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if value is None:
        return {"stringValue": ""}
    if isinstance(value, (dict, list)):
        return {"stringValue": json.dumps(value, ensure_ascii=False)}
    return {"stringValue": str(value)}


def to_otlp(bundles: list[dict]) -> dict:
    """全部证据束 → 一份 OTLP/JSON（``ExportTraceServiceRequest`` 的形状）。

    边界写死在这里：**只产文件，不引 SDK，不发一个字节出去**。没有 collector、
    没有网络调用、没有 ``opentelemetry`` import。这份文件喂给任何 collector 之前
    要先 ``tail -n +2`` 剥掉铁律 3 的出处注释头 —— 那一行是本仓库所有 evidence 文件
    的共同约定（见 ``make_evidence.py::load_evidence_json``），不为这一个出口破例。
    """
    scope_spans = []
    for bundle in bundles:
        for trace in bundle["trace"].get("traces", []):
            tid = otlp_trace_id(trace.get("trace_id"))
            spans = []
            for span in trace.get("spans", []):
                attrs = dict(span.get("attributes") or {})
                attrs["maos.trace_id"] = trace.get("trace_id")
                attrs["maos.span.kind"] = span.get("kind")
                attrs["maos.evidence.bundle"] = bundle["name"]
                out = {
                    "traceId": tid,
                    "spanId": span.get("span_id"),
                    "name": span.get("name"),
                    "kind": 1,                      # SPAN_KIND_INTERNAL
                    "startTimeUnixNano": _unix_nano(span.get("start")),
                    "endTimeUnixNano": _unix_nano(span.get("end") or span.get("start")),
                    "attributes": [{"key": k, "value": _otlp_value(v)}
                                   for k, v in attrs.items()],
                }
                if span.get("parent_span_id"):
                    out["parentSpanId"] = span["parent_span_id"]
                spans.append(out)
            scope_spans.append({
                "scope": {"name": "maos.obs.trace", "version": trace.get("schema", "")},
                "spans": spans,
            })
    return {
        "resourceSpans": [{
            "resource": {"attributes": [
                {"key": "service.name", "value": {"stringValue": "maos"}},
                {"key": "maos.trace_id.mapping",
                 "value": {"stringValue": OTLP_TRACE_ID_NOTE}},
            ]},
            "scopeSpans": scope_spans,
        }]
    }


# ---------------------------------------------------------------------------
# 落盘与比对
# ---------------------------------------------------------------------------
def header_comment(sha: str) -> str:
    return f"{HTML_HEADER_PREFIX}{datetime.now(timezone.utc).isoformat()} from {sha} -->"


def split_header(text: str) -> tuple[str, str]:
    """已落盘的文件 → ``(首行, 正文)``。首行形状不合规就当没有首行。"""
    first, _, rest = text.partition("\n")
    if HTML_HEADER_RE.match(first.strip()):
        return first, rest
    return "", text


def build(evidence_root: str) -> str:
    bundles = load_bundles(evidence_root)
    return render_report(bundles, load_index(evidence_root))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="render_trace",
        description="把 evidence/*/trace.json 渲染成一张离线单页 HTML")
    parser.add_argument("--evidence", default=DEFAULT_EVIDENCE,
                        help="证据束根目录（默认 evidence/）")
    parser.add_argument("--out", default=DEFAULT_OUT,
                        help="HTML 输出路径（默认 evidence/report.html）")
    parser.add_argument("--check", action="store_true",
                        help="只比对不写盘；与当前 evidence/ 不一致即非零退出")
    parser.add_argument("--otlp", nargs="?", const=DEFAULT_OTLP, default=None,
                        metavar="PATH",
                        help="另出一份离线 OTLP/JSON（默认不产；不引 SDK、不发网络）")
    args = parser.parse_args(argv)

    try:
        bundles = load_bundles(args.evidence)
        body = render_report(bundles, load_index(args.evidence))
    except (RenderError, OSError, ValueError) as exc:
        print(f"[FAIL] {exc}")
        return 2

    rel_out = os.path.relpath(args.out, ROOT)
    if args.check:
        if not os.path.exists(args.out):
            print(f"[STALE] {rel_out} —— 文件不存在")
            print("跑 `python3 scripts/render_trace.py` 生成。")
            return 1
        with open(args.out, encoding="utf-8") as fh:
            first, old = split_header(fh.read())
        if not first:
            print(f"[STALE] {rel_out} —— 首行不是 `{HTML_HEADER_PREFIX}…-->` 出处注释")
            return 1
        if old == body:
            print(f"[OK]    {rel_out} 与当前 evidence/ 逐字节一致"
                  f"（正文 {len(body.encode('utf-8')):,} bytes，{len(bundles)} 束）")
            print(f"        出处 {first}")
            return 0
        print(f"[STALE] {rel_out} —— 与当前 evidence/ 不一致")
        diff = list(difflib.unified_diff(
            old.splitlines(), body.splitlines(),
            fromfile=f"{rel_out}（当前）", tofile=f"{rel_out}（按 evidence/ 应有）",
            lineterm="", n=1))
        for line in diff[:24]:
            print(f"        {line}")
        if len(diff) > 24:
            print(f"        …… 另有 {len(diff) - 24} 行差异")
        print("跑 `python3 scripts/render_trace.py` 重新生成。")
        return 1

    sha = git_sha()
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(header_comment(sha) + "\n")
        fh.write(body)
    total = sum((b["trace"].get("summary") or {}).get("span_count") or 0 for b in bundles)
    print(f"[WROTE] {rel_out}  {os.path.getsize(args.out):,} bytes  "
          f"{len(bundles)} 束 / {total} span")

    if args.otlp:
        doc = to_otlp(bundles)
        with open(args.otlp, "w", encoding="utf-8") as fh:
            fh.write(f"{HEADER_PREFIX}{datetime.now(timezone.utc).isoformat()} from {sha}\n")
            fh.write(json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
        n = sum(len(s["spans"]) for s in doc["resourceSpans"][0]["scopeSpans"])
        print(f"[WROTE] {os.path.relpath(args.otlp, ROOT)}  "
              f"{os.path.getsize(args.otlp):,} bytes  {n} span（OTLP/JSON，不发网络）")
        print(f"        {OTLP_TRACE_ID_NOTE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
