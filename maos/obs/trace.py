"""Trace 导出 —— 把 ``event_log`` 织成 OTel 语义的 span 树。

字段对齐 OpenTelemetry：``trace_id / span_id / parent_span_id / name / start / end /
attributes``。**不做真 OTel 导出**：没有 SDK 依赖、没有 collector、不发一个字节出去。
这一层只负责把「库里已经有的事实」重排成评委与 ``scripts/verify.py`` 都能读的形状，
**不产生任何新事实**。

一条纪律写在最前面（铁律 8）：本模块只读，不推断业务状态。span 上出现的每一个字段
都能在 ``plan / task / artifact / event_log`` 四张表里逐字找到出处。找不到出处的产物
**不会**被悄悄挂到某个 span 下面充数，而是显式标成 ``provenance="unknown"`` ——
审计链有洞就要让洞看得见；把洞填平是上游的事，不是导出器的事。

已知的三处洞（都在 ``docs/BACKLOG.md`` 有案，本模块只负责让它们可见）：

1. ``review_after_gate()`` 直接 ``store.insert_artifact`` 落 review_note，不经
   ``on_task_result``，因此没有 StateTransition 可挂 —— trace 里会凭空多出一份产物。
2. 场景 3 / 5 的 ``seed_scripted_report()`` 同理：预置的 test_report 没有来源事件。
3. 场景 1 / 2 的 ``flows/common.py::patch_verifier`` 也走同一条旁路入库。

三者都会被标成 ``provenance="unknown"`` 并计进 ``summary.unsourced_artifacts``。

**``provenance="unknown"`` 说的是入库路径，不是内容真伪。** 第 3 类是**真跑**
沙箱回归的产物（真 workdir、真 ``git apply``、真 pytest），只是插入时绕开了
``on_task_result``，于是没有事件可指。把这三类一律读成「预置的假报告」，会把已经
兑现的外部判据重新贬成脚手架 —— 判「这份报告是不是真跑的」要看下面那条
``maos.artifact.sandbox.mode``，不是看 provenance。

依赖方向：``maos/obs`` 只 import ``maos.core.store``，不 import 任何业务域
（铁律 9）。换域时本文件一行不改。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timedelta
from typing import Any

SCHEMA = "maos.trace/v1"

# span 的 kind：与 OTel 的 SpanKind 不是一回事，这里表达的是「这条 span 描述什么」。
KIND_PLAN = "plan"
KIND_TASK = "task"
KIND_EVENT = "event"
KIND_ARTIFACT = "artifact"

# 产物来源。只有 task_result / compensation 两种能在 event_log 里指到具体一行；
# 其余一律 unknown —— 不猜，也不因为「看着像」就给它安一个来源。
PROV_TASK_RESULT = "task_result"
PROV_COMPENSATION = "compensation_attached"
PROV_UNKNOWN = "unknown"

# 与 ``maos/artifacts.py::KIND_TEST_REPORT`` 同值。不 import 是为了守住本模块
# 「只 import maos.core.store」那条依赖纪律（见模块 docstring）。
_KIND_TEST_REPORT = "test_report"

# 一份 test_report 是在容器里跑的还是降级成裸 subprocess 跑的，写在它自己的
# content 里（``maos/tools/sandbox.py::sandbox_pytest_run``）。这里只**读**，
# 读不到就标 unrecorded —— 不按「跑绿了应该就是容器」去补一个值，那种补法
# 正好把这一层要暴露的洞填掉：降级跑出来的全绿和容器跑出来的全绿长得一模一样，
# 差别只在 --network none 那条用例是 skipped 还是 passed，而计数里看不见 skipped。
MODE_SUBPROCESS = "subprocess"
MODE_UNRECORDED = "unrecorded"

# 有 duration_ms 的事件类型：span 的 end 由 start + duration_ms 得出，其余 end == start。
_TIMED_EVENTS = ("SkillInvoked", "ToolInvoked", "KbRetrieved")

# 事件 -> span 名的前缀。未列出的类型走 ``event:<type>``，不丢事件、也不假装认识它。
_EVENT_NAME = {
    "StateTransition": lambda e: f"state:{e.get('from_state')}->{e.get('to_state')}",
    "PlanTransition": lambda e: f"plan-state:{e.get('from_state')}->{e.get('to_state')}",
    "SkillInvoked": lambda e: f"skill:{(e.get('detail') or {}).get('skill', '?')}",
    "ToolInvoked": lambda e: f"tool:{(e.get('detail') or {}).get('tool', '?')}",
    "KbRetrieved": lambda e: "kb:retrieve",
    "CompensationAttached": lambda e: "compensation:attach",
    "Replanned": lambda e: "replan",
    # 业务状态是业务对象自己的字段，不是 Task 状态（铁律 9）——名字里刻意带 biz，
    # 免得在 trace 上跟 state:/plan-state: 那两类混成一谈。
    "RefundBizStatusChanged": lambda e: (
        f"biz:{e.get('from_state')}->{e.get('to_state')}"),
    "AuthoritativeFactViolation": lambda e: "biz:authoritative-violation",
}


class TraceError(RuntimeError):
    """导出不下去时抛这个 —— 绝不返回一份「差不多」的树。"""


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _sid(*parts: Any) -> str:
    """确定性 span_id：同一份库导出多少次都是同一个值，否则 verify.py 没法比对。

    16 位十六进制，与 OTel 的 span id 宽度一致。
    """
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _plus_ms(iso: str | None, ms: Any) -> str | None:
    """``start + duration_ms``。解析不了就原样返回 start —— 不编时间。"""
    if not iso or not isinstance(ms, (int, float)):
        return iso
    try:
        return (datetime.fromisoformat(iso) + timedelta(milliseconds=float(ms))).isoformat()
    except ValueError:
        return iso


def _within(ts: str | None, lo: str | None, hi: str | None) -> bool:
    """``lo <= ts <= hi``。三者都是同一个 ``_now()`` 产的 ISO8601，字符串序即时间序。

    边界取闭区间：同一微秒内落库与迁移的情况确实存在（内存库快得很），
    开区间会把真产物判成无来源，而「份数上限」那条已经挡住了多认。
    """
    if ts is None or lo is None or hi is None:
        return False
    return lo <= ts <= hi


def _span(*, trace_id, span_id, parent_span_id, name, kind, start, end, attributes) -> dict:
    return {
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": parent_span_id,
        "name": name,
        "kind": kind,
        "start": start,
        "end": end,
        "attributes": attributes,
    }


# ---------------------------------------------------------------------------
# 树的自校验：孤儿 / 环 / 重复 id
# ---------------------------------------------------------------------------
def check_span_tree(spans: list[dict], *, expect_single_root: bool = True) -> list[str]:
    """返回**错误列表**（空 = 通过），与 ``maos/artifacts.py::validate_artifact`` 同构。

    上层拿到的是可以直接印给评委看的东西，不是一个异常。
    """
    errs: list[str] = []
    ids: set[str] = set()
    for s in spans:
        sid = s.get("span_id")
        if not sid:
            errs.append(f"span 缺 span_id: name={s.get('name')!r}")
            continue
        if sid in ids:
            errs.append(f"span_id 重复: {sid}")
        ids.add(sid)

    roots = [s for s in spans if s.get("parent_span_id") is None]
    for s in spans:
        parent = s.get("parent_span_id")
        if parent is not None and parent not in ids:
            errs.append(f"孤儿 span: {s.get('span_id')} 的 parent {parent} 不在树内")

    if expect_single_root and len(roots) != 1:
        errs.append(f"根 span 应恰好 1 条，实际 {len(roots)} 条")

    # 环检测：沿 parent 往上爬，爬到 None 或已判定安全的节点为止。
    by_id = {s["span_id"]: s for s in spans if s.get("span_id")}
    safe: set[str] = set()
    for sid in by_id:
        seen: list[str] = []
        cur: str | None = sid
        while cur is not None and cur in by_id and cur not in safe:
            if cur in seen:
                errs.append(f"span 树成环: {' -> '.join(seen[seen.index(cur):] + [cur])}")
                break
            seen.append(cur)
            cur = by_id[cur].get("parent_span_id")
        else:
            safe.update(seen)
    return errs


# ---------------------------------------------------------------------------
# 单个 Plan 的 span 树
# ---------------------------------------------------------------------------
def _submit_index(events: list[dict], tasks: list[dict]) -> dict[str, list[dict]]:
    """按 task 重建每一次 submit_result 的 ``(attempt, span, 声明份数, 时间窗)``。

    两件事都从库里的事实还原，不猜：

    * **attempt**：``event_log`` 行里没有这个字段，但推进规则是确定的 ——
      每一次 ``* -> DISPATCHED`` 就 +1（``control_plane.py::dispatch_ready``）。
    * **时间窗**：``on_task_result`` 是「先逐份 ``insert_artifact``，再迁到
      AWAITING_REVIEW」，所以这一轮真正由任务结果带回来的产物，其 ``created_at``
      必然落在「本 task 上一条**状态迁移**」到「这条 submit 事件」之间。

      下界只认 StateTransition，不认任意事件：``on_task_result`` 自己就会在插完
      patch_set 之后、迁移之前补一条 ``CompensationAttached``（高风险任务的补偿
      附着）。拿它当下界，patch_set 就被挤到窗口外面去了 —— 场景 3 正是这样。

    为什么非要时间窗、光按 version 排序取前 N 份不行：场景层的
    ``seed_scripted_report()`` 会在 Plan 还没开跑时就预置一份同 version 的
    test_report，它排在真产物**前面**。只按顺序取名额的话，预置件会顶掉真产物的
    位置，于是「有来源」和「无来源」两顶帽子正好戴反 —— 而两边计数都对得上，
    看输出根本发现不了。
    """
    prev_time = {t["task_id"]: t.get("created_at") for t in tasks}
    out: dict[str, list[dict]] = {}
    attempt: dict[str, int] = {}
    for e in events:
        tid = e.get("task_id")
        if not tid:
            continue
        if e.get("event_type") != "StateTransition":
            continue
        if e.get("to_state") == "DISPATCHED":
            attempt[tid] = attempt.get(tid, 0) + 1
        elif e.get("to_state") == "AWAITING_REVIEW":
            declared = (e.get("detail") or {}).get("artifacts", 0)
            out.setdefault(tid, []).append({
                "attempt": attempt.get(tid, 0),
                "span_id": _sid("event", e.get("seq")),
                "declared": declared if isinstance(declared, int) else 0,
                "lo": prev_time.get(tid),
                "hi": e.get("created_at"),
            })
        prev_time[tid] = e.get("created_at")
    return out


def export_trace(store: Any, plan_id: str) -> dict:
    """把一个 Plan 的全部事件与产物导成 span 树。

    ``store`` 只用到 ``get_plan / list_tasks / list_artifacts / list_event_log`` 四个
    只读方法 —— 任何实现了 ``maos.core.store.Store`` 的后端都能喂进来。
    """
    plan = store.get_plan(plan_id)
    if plan is None:
        raise TraceError(f"plan 不存在: {plan_id}")

    tasks = store.list_tasks(plan_id)
    events = store.list_event_log(plan_id)
    trace_id = plan.get("trace_id") or ""

    spans: list[dict] = []
    root_id = _sid("plan", plan_id)
    spans.append(_span(
        trace_id=trace_id, span_id=root_id, parent_span_id=None,
        name=f"plan:{plan.get('goal', '')}", kind=KIND_PLAN,
        start=plan.get("created_at"), end=plan.get("updated_at"),
        attributes={
            "maos.plan_id": plan_id,
            "maos.plan.state": plan.get("state"),
            "maos.plan.goal": plan.get("goal"),
        },
    ))

    task_span: dict[str, str] = {}
    for t in tasks:
        sid = _sid("task", t["task_id"])
        task_span[t["task_id"]] = sid
        spans.append(_span(
            trace_id=trace_id, span_id=sid, parent_span_id=root_id,
            name=f"task:{t.get('role')}:{t.get('title')}", kind=KIND_TASK,
            start=t.get("created_at"), end=t.get("updated_at"),
            attributes={
                "maos.task_id": t["task_id"],
                "maos.task.role": t.get("role"),
                "maos.task.state": t.get("state"),
                "maos.task.attempt": t.get("attempt"),
                "maos.task.risk_level": t.get("risk_level"),
                "maos.task.effect_risk": t.get("effect_risk"),
                "maos.task.depends_on": t.get("depends_on"),
            },
        ))

    unknown_task_events = 0
    for e in events:
        etype = e.get("event_type", "?")
        tid = e.get("task_id")
        parent = task_span.get(tid) if tid else root_id
        attrs: dict[str, Any] = {
            "maos.event.seq": e.get("seq"),
            "maos.event.type": etype,
            "maos.event.id": e.get("event_id") or None,
            "maos.task_id": tid,
            "maos.reason": e.get("reason"),
            "maos.detail": e.get("detail") or {},
        }
        if tid and parent is None:
            # 事件指向一个 task 表里没有的 task_id：挂到 plan 根上并留记号，
            # 不丢事件、也不假装它属于某个已知任务。
            parent = root_id
            attrs["maos.task_id.resolved"] = False
            unknown_task_events += 1
        start = e.get("created_at")
        end = start
        if etype in _TIMED_EVENTS:
            end = _plus_ms(start, (e.get("detail") or {}).get("duration_ms"))
        namer = _EVENT_NAME.get(etype)
        spans.append(_span(
            trace_id=e.get("trace_id") or trace_id,
            span_id=_sid("event", e.get("seq")), parent_span_id=parent,
            name=namer(e) if namer else f"event:{etype}", kind=KIND_EVENT,
            start=start, end=end, attributes=attrs,
        ))

    submits = _submit_index(events, tasks)
    comp_spans: dict[str, list[tuple[str, Any]]] = {}
    for e in events:
        if e.get("event_type") == "CompensationAttached" and e.get("task_id"):
            comp_spans.setdefault(e["task_id"], []).append(
                (_sid("event", e.get("seq")), (e.get("detail") or {}).get("patch_ref")))

    unsourced = 0
    degraded_reports = 0
    unrecorded_reports = 0
    for t in tasks:
        tid = t["task_id"]
        # 归属条件三条同时成立才认：version 对得上这一次 attempt、落库时间在这次
        # submit 的执行窗口内、且该次 submit 声明的份数还没用完。三条缺一，
        # 这份产物就没有来源事件可指 —— 那正是要暴露给评委的东西。
        used: dict[str, int] = {}
        for art in store.list_artifacts(tid):
            kind, version, born = art.get("kind"), art.get("version"), art.get("created_at")
            prov, parent, ref_span = PROV_UNKNOWN, task_span[tid], None

            if kind == "compensation":
                for sid, patch_ref in comp_spans.get(tid, []):
                    if patch_ref == (art.get("content") or {}).get("patch_ref"):
                        prov, parent, ref_span = PROV_COMPENSATION, sid, sid
                        break
            else:
                for sub in submits.get(tid, []):
                    if sub["attempt"] != version:
                        continue
                    if not _within(born, sub["lo"], sub["hi"]):
                        continue
                    if used.get(sub["span_id"], 0) >= sub["declared"]:
                        continue
                    used[sub["span_id"]] = used.get(sub["span_id"], 0) + 1
                    prov, parent, ref_span = PROV_TASK_RESULT, sub["span_id"], sub["span_id"]
                    break

            if prov == PROV_UNKNOWN:
                unsourced += 1

            # 测试报告额外带一条「这一次到底在哪儿跑的」。非 test_report 一律 None，
            # 不给别的产物安一个它本来就没有的字段。
            sandbox_mode = sandbox_reason = None
            if kind == _KIND_TEST_REPORT:
                content = art.get("content") or {}
                sandbox_mode = content.get("sandbox_mode") or MODE_UNRECORDED
                sandbox_reason = content.get("degraded_reason")
                if sandbox_mode == MODE_SUBPROCESS:
                    degraded_reports += 1
                elif sandbox_mode == MODE_UNRECORDED:
                    unrecorded_reports += 1

            spans.append(_span(
                trace_id=trace_id, span_id=_sid("artifact", art.get("artifact_id")),
                parent_span_id=parent, name=f"artifact:{kind}", kind=KIND_ARTIFACT,
                start=art.get("created_at"), end=art.get("created_at"),
                attributes={
                    "maos.artifact.id": art.get("artifact_id"),
                    "maos.artifact.kind": kind,
                    "maos.artifact.version": version,
                    "maos.task_id": tid,
                    "maos.artifact.provenance": prov,
                    "maos.artifact.provenance.event_span": ref_span,
                    "maos.artifact.provenance.note": (
                        None if prov != PROV_UNKNOWN else
                        "无来源事件：该产物未经 on_task_result 入库，"
                        "审计链上查不到是谁在哪一步产的（docs/BACKLOG.md task-C 第 5 条）"
                    ),
                    "maos.artifact.sandbox.mode": sandbox_mode,
                    "maos.artifact.sandbox.degraded_reason": sandbox_reason,
                    "maos.artifact.sandbox.note": (
                        None if sandbox_mode not in (MODE_SUBPROCESS, MODE_UNRECORDED) else
                        "容器隔离本次未生效：报告是裸 subprocess 跑出来的"
                        if sandbox_mode == MODE_SUBPROCESS else
                        "执行路径不可审计：报告里没记 sandbox_mode，无从判断这一次"
                        "是不是在容器里跑的（docs/BACKLOG.md task-X4 第 1 条）"
                    ),
                },
            ))

    spans.sort(key=lambda s: (s.get("start") or "", s["kind"], s["span_id"]))
    errors = check_span_tree(spans)
    return {
        "schema": SCHEMA,
        "plan_id": plan_id,
        "trace_id": trace_id,
        "plan_state": plan.get("state"),
        "goal": plan.get("goal"),
        "spans": spans,
        "summary": {
            "span_count": len(spans),
            "task_count": len(tasks),
            "event_count": len(events),
            "artifact_count": sum(1 for s in spans if s["kind"] == KIND_ARTIFACT),
            "unsourced_artifacts": unsourced,
            "unresolved_task_events": unknown_task_events,
            # 「跑了但没真跑」的两种形态，分开计数：前者是知道降级了，
            # 后者是连有没有降级都查不到 —— 后者比前者更糟，不能合并成一个数。
            "degraded_sandbox_reports": degraded_reports,
            "unrecorded_sandbox_reports": unrecorded_reports,
            "tree_errors": errors,
        },
    }


# ---------------------------------------------------------------------------
# 整库导出（供 scripts/ 用）
# ---------------------------------------------------------------------------
def list_plan_ids(db_path: str) -> list[str]:
    conn = _connect_ro(db_path)
    try:
        return [r[0] for r in conn.execute("SELECT plan_id FROM plan ORDER BY created_at")]
    finally:
        conn.close()


def _connect_ro(db_path: str) -> sqlite3.Connection:
    """只读连接。库不存在就抛 —— ``sqlite3.connect`` 会凭空建一个空库，
    那样「库丢了」会伪装成「库是空的」，是最难查的一类假象。"""
    if not os.path.exists(db_path):
        raise TraceError(f"数据库不存在: {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def stray_events(db_path: str) -> list[dict]:
    """``plan_id`` 指不到任何 plan 行的事件 —— 它们不属于任何一棵树。

    现实里确实有：``flows/scenario_5.py`` 的 ``issue.aggregate`` 跑在 create_plan
    之前，落的 SkillInvoked 行 ``plan_id`` 是空串。这类事件按 plan 查永远查不到，
    所以在这里单独点名，而不是让它们静静消失。
    """
    conn = _connect_ro(db_path)
    try:
        rows = conn.execute(
            "SELECT seq, event_type, plan_id, task_id, trace_id, created_at FROM event_log"
            " WHERE plan_id NOT IN (SELECT plan_id FROM plan) ORDER BY seq").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def export_trace_bundle(db_path: str, *, store_factory: Any = None) -> dict:
    """整库导出：每个 plan 一棵树，外加不属于任何 plan 的游离事件清单。

    这就是写进 ``evidence/scenario-<N>/trace.json`` 的那份文档。
    """
    from maos.core.store import SqliteStore

    if not os.path.exists(db_path):
        raise TraceError(f"数据库不存在: {db_path}")
    factory = store_factory or SqliteStore
    store = factory(db_path)
    traces = [export_trace(store, pid) for pid in list_plan_ids(db_path)]
    strays = stray_events(db_path)
    return {
        "schema": SCHEMA,
        "db": os.path.basename(db_path),
        "plan_count": len(traces),
        "traces": traces,
        "stray_events": strays,
        "summary": {
            "span_count": sum(t["summary"]["span_count"] for t in traces),
            "event_count": sum(t["summary"]["event_count"] for t in traces),
            "unsourced_artifacts": sum(t["summary"]["unsourced_artifacts"] for t in traces),
            "degraded_sandbox_reports": sum(
                t["summary"]["degraded_sandbox_reports"] for t in traces),
            "unrecorded_sandbox_reports": sum(
                t["summary"]["unrecorded_sandbox_reports"] for t in traces),
            "stray_event_count": len(strays),
            "tree_errors": [e for t in traces for e in t["summary"]["tree_errors"]],
        },
    }


def to_json(doc: dict) -> str:
    return json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=False)
