"""Trace 导出 —— 把 ``event_log`` 织成 OTel 语义的 span 树。

字段对齐 OpenTelemetry：``trace_id / span_id / parent_span_id / name / start / end /
attributes``。**不做真 OTel 导出**：没有 SDK 依赖、没有 collector、不发一个字节出去。
这一层只负责把「库里已经有的事实」重排成评委与 ``scripts/verify.py`` 都能读的形状，
**不产生任何新事实**。

一条纪律写在最前面（铁律 8）：本模块只读，不推断业务状态。span 上出现的每一个字段
都能在 ``plan / task / artifact / event_log`` 四张表里逐字找到出处。找不到出处的产物
**不会**被悄悄挂到某个 span 下面充数，而是显式标成 ``provenance="unknown"`` ——
审计链有洞就要让洞看得见；把洞填平是上游的事，不是导出器的事。

绕开 ``on_task_result`` 落库的旁路有三条。它们**都还在**（每一条都有非走不可的
理由，见各自的 docstring），区别只在会不会自报来源：

1. 场景 3 / 5 的 ``agents/testing.py::seed_scripted_report()`` —— 预置的
   test_report。落库后补一条 ``ArtifactSeeded``。
2. 场景 1 / 2 的 ``flows/common.py::patch_verifier`` —— 现跑沙箱回归的真报告。
   同样补 ``ArtifactSeeded``。
3. ``agents/reviewer.py::review_after_gate()`` 直接 ``store.insert_artifact``
   落 review_note（场景 1 / 2 / 6 / 7）。同样补 ``ArtifactSeeded``。

三条**都**经 ``_seeded_index()`` **点名**认领，标 ``provenance="artifact_seeded"``、
计进 ``summary.seeded_artifacts``。``summary.unsourced_artifacts`` 因此在当前四个
场景上恒为 0 —— 这个计数**不删**：它是留给下一条旁路的哨兵，谁再绕开
``on_task_result`` 又不补事件，它就从 0 变回非 0，A 类 warn 当场回来。
两个数始终分开，是因为两件事分得开：一个是「审计链指得到，只是没走正路」，
一个是「审计链指不到」。

``artifact_seeded`` **绝不能压成** ``task_result``：这些产物确实没走
``on_task_result``，冒充正路等于把洞抹掉而不是补上。旁路那件事照旧写在脸上
（``provenance.note`` / ``provenance.source``），补上的只是「指不到是谁产的」。

**provenance 说的是入库路径，不是内容真伪。** 第 2 类是**真跑**沙箱回归的产物
（真 workdir、真 ``git apply``、真 pytest），第 1 类是一次都没跑过的预置件 ——
两者的 provenance 一样，差别在 ``maos.artifact.sandbox.mode``（container /
subprocess / not-run）和 ``provenance.source`` 点的那个函数名。把它们一律读成
「预置的假报告」，会把已经兑现的外部判据重新贬成脚手架。

树有**三族**，不是一族（T134 加了第三族）：

* ``traces`` —— 每条真 ``plan`` 行一棵，事件按 plan_id 归、用量按非空 trace_id 归。
* ``roundtable_traces`` —— 圆桌那一段。它跑在 ``create_plan`` **之前**，不建 plan、
  不建 task，所以既没有真 plan 行也没有 trace_id；它那一摊行靠 ``plan_id`` 的
  ``roundtable:`` 前缀认（:data:`ROUNDTABLE_PLAN_PREFIX`）。**不给它造 plan 行**：
  那会让 DAG 的证据束里凭空多出几棵不是任务的树（理由写在
  ``maos/roundtable/speaker.py`` 的模块 docstring 里，那是个设计决定，不是欠账）。
* ``cs_traces`` —— 客服前台那一族（p14 · T176），``plan_id`` 以 ``cs:`` 开头。
  **库里一条 cs 行都没有时这个键不出现**：既有证据束的 trace.json 逐字节不变。
* ``stray_events`` / ``unattributed_usage`` —— 上面几族一条都没收走的剩余。

三族互斥且穷尽，所以「换个地方挂」不等于「消失」：圆桌从游离清单搬进自己的树之后，
条数在 ``summary`` 里照旧数得到。判据是「**被树收走的**不算游离」，不是「plan_id
非空就不算游离」—— 后者会把「指向一个不存在的 Plan」那种真该查的形态一起藏掉。

依赖方向：``maos/obs`` 只 import ``maos.core.store``，不 import 任何业务域
（铁律 9）。换域时本文件一行不改 —— 圆桌那一族也是按**前缀**认的，
不 import ``maos.roundtable``。
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
#: 圆桌那一段的两种 kind（T134）。它们**不出现在任何 plan 树里** —— 圆桌不属于
#: 任何 Plan，所以它自成一族树，见 :func:`roundtable_traces`。
KIND_ROUNDTABLE = "roundtable"
KIND_ROUNDTABLE_ROUND = "roundtable-round"

#: 圆桌落库那些行的 ``plan_id`` 前缀。与 ``maos/roundtable/team.py::PLAN_PREFIX``
#: 同值，**照抄而不 import**：本模块只 import ``maos.core.store``（见模块 docstring
#: 末条的依赖纪律），口径同 ``scripts/replay_roundtable.py`` 抄同一个字面量的理由。
#:
#: 这个前缀是圆桌那一摊行**唯一**的归属键：`plan` 表里永远查不到它（圆桌不建
#: plan、不建 task，`maos/roundtable/speaker.py` 的模块 docstring 把「给圆桌造
#: plan 行」这条路明令堵死了），而 `plan_id LIKE 'roundtable:%'` 一句就能把它
#: 整摊捞出来。T134 之前这一摊只能落进 ``stray_events`` / ``unattributed_usage``，
#: 也就是「如实承认它不在任何一棵树里」；现在它有自己的树了。
ROUNDTABLE_PLAN_PREFIX = "roundtable:"

#: 客服前台那一族（p14 · T176）。``plan_id`` 以 ``cs:`` 开头即属 cs 家族：
#: ``cs:<会话 id>``（会话对象、cs.* skill、查单 ToolInvoked、会诊卡）与 ``cs:mcp``
#: （只读 MCP 连接器）。与 ``maos/domain/cs/types.py::CS_PLAN_PREFIX`` 同值，**照抄而不
#: import**，理由同 :data:`ROUNDTABLE_PLAN_PREFIX`：本模块不 import 任何业务域（铁律 9）。
#: 判前缀用 ``substr`` 精确比，不用 ``LIKE``（SQLite 的 LIKE 对 ASCII 不分大小写，
#: ``CS:`` 会被误收）。
CS_PLAN_PREFIX = "cs:"
KIND_CS = "cs"
KIND_CS_TURN = "cs-turn"

# 产物来源。前三种都能在 event_log 里指到具体一行；指不到的一律 unknown ——
# 不猜，也不因为「看着像」就给它安一个来源。
PROV_TASK_RESULT = "task_result"
PROV_COMPENSATION = "compensation_attached"
#: 绕开 ``on_task_result`` 落库、但**自报了来源**的产物（``ArtifactSeeded``）。
#: 与 ``task_result`` 分开是要紧的：它记的是审计链指得到了，不是这份产物走了正路。
#: 压成 task_result 就等于让旁路冒充正路 —— 那是把洞抹掉，不是把洞补上。
PROV_ARTIFACT_SEEDED = "artifact_seeded"
PROV_UNKNOWN = "unknown"

#: 旁路产物自报来源的事件类型。与 ``maos/agents/testing.py::SEEDED_EVENT`` 同值，
#: 照抄而不 import：本模块只 import ``maos.core.store``（见模块 docstring）。
SEEDED_EVENT = "ArtifactSeeded"

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
    SEEDED_EVENT: lambda e: f"artifact-seeded:{(e.get('detail') or {}).get('kind', '?')}",
    # 圆桌那三类（T134）。只在圆桌树里出现 —— 它们的 plan_id 恒带
    # ``roundtable:`` 前缀，进不了任何 plan 树。名字里带 seat / round 而不是
    # 沿用 ``event:<type>``：评委在页面上读的就是「谁第几个说的」。
    "RoundtableRound": lambda e: (
        f"roundtable-round:{(e.get('detail') or {}).get('round_no', '?')}"),
    "RoundtableSeatSpoke": lambda e: f"seat:{(e.get('detail') or {}).get('seat', '?')}",
    "RoundtableVerdict": lambda e: (
        f"roundtable-verdict:{(e.get('detail') or {}).get('recommend', '?')}"),
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


def _event_attrs(e: dict) -> dict[str, Any]:
    """一条 ``event_log`` 行 → span 的 attributes。

    键的顺序即落盘顺序，**不许动**：``verify.py`` 第 4 项拿库重放一遍与
    ``trace.json`` 逐字节比对，重排一个键就会让那一项在没人动过证据的情况下红。
    抽成函数是为了让圆桌树（:func:`roundtable_traces`）与 plan 树落出**同一个
    形状** —— 两处各写一遍的话，哪天只改一处，两族树的事件长得就不一样了。
    """
    return {
        "maos.event.seq": e.get("seq"),
        "maos.event.type": e.get("event_type", "?"),
        "maos.event.id": e.get("event_id") or None,
        "maos.task_id": e.get("task_id"),
        "maos.reason": e.get("reason"),
        "maos.detail": e.get("detail") or {},
    }


def _event_name(e: dict) -> str:
    """事件 → span 名。认不出来的走 ``event:<type>`` —— 不丢、也不假装认识它。"""
    etype = e.get("event_type", "?")
    namer = _EVENT_NAME.get(etype)
    return namer(e) if namer else f"event:{etype}"


def _event_end(e: dict, start: str | None) -> str | None:
    """有 ``duration_ms`` 的事件才有真实跨度，其余 ``end == start``（不编时长）。"""
    if e.get("event_type") in _TIMED_EVENTS:
        return _plus_ms(start, (e.get("detail") or {}).get("duration_ms"))
    return start


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

    如今旁路产物先被 ``_seeded_index()`` 点名认走，不再进这里竞争名额，上面那记
    暗坑因此打不着了。窗口照旧留着：``ArtifactSeeded`` 是**上游自愿**补的，
    哪天有第四条旁路忘了补，这套判定要能照旧把它判成无来源，而不是靠「没人抢名额」
    蒙对。判据不能建立在别人一定会守规矩上。
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


def _seeded_index(events: list[dict]) -> dict[str, dict]:
    """``artifact_id -> 声明它来源的 ``ArtifactSeeded`` 事件``。

    与 ``_submit_index`` 那套时间窗不同，这里是**点名**：事件的
    ``detail.artifact_id`` 写死了认领哪一份，不靠「落库时间在窗口内」去猜。
    旁路入库本来就没有窗口可言（``patch_verifier`` 跑在 AWAITING_REVIEW 之后，
    ``seed_scripted_report`` 跑在 Plan 开跑之前），拿窗口去套只会套空。

    同一个 artifact_id 出现两条时取**第一条**：来源是一次性的事实，后来的重复
    声明不该改写它，也不该让认领结果依赖遍历顺序。
    """
    out: dict[str, dict] = {}
    for e in events:
        if e.get("event_type") != SEEDED_EVENT:
            continue
        detail = e.get("detail") or {}
        aid = detail.get("artifact_id")
        if aid and str(aid) not in out:
            out[str(aid)] = {"span_id": _sid("event", e.get("seq")),
                             "source": detail.get("source"),
                             "scripted": detail.get("scripted")}
    return out


# ---------------------------------------------------------------------------
# 成本视图（T29）—— 按 trace_id 聚合 model_usage
# ---------------------------------------------------------------------------
#: 这句话必须跟着数字一起进证据束。缺省路径全是 ``ScriptedModelClient``，它的
#: token 数是 ``len(user) // 4``（``maos/model/client.py``）—— 字符数除以 4 的估算，
#: 不是任何一家的计费口径。把它印成「本次演示花了多少钱」，是在评委面前给出一个
#: 虚假的精确信号，比不做成本量化更坏。所以 ``estimated`` 逐行落库，这里逐层汇总。
ESTIMATED_NOTE = (
    "estimated=1 的行是估算不是计费：缺省路径用 ScriptedModelClient，其 token 数为 "
    "len(user)//4（maos/model/client.py），非任何计费口径。all_estimated=true 时"
    "这些数字只能读作调用规模，不能读作金额。"
)

#: 一条用量都没记到时**必须**跟着的一句话。``calls=0`` 有两种成因，而它们在屏幕上
#: 长得一模一样：真的一次模型都没调，和调了但没记。后者的成因是构造 Agent 时没传
#: ``store=``（``BaseAgent.__init__`` 的 store 缺省 None，此时
#: ``record_model_usage`` 直接跳过），演示主线上确实还有这么写的场景。
#: 不加这句，``calls=0`` 就会被读成「这条链路没花钱」—— 那是本模块最不该产生的
#: 那类假象（同 ``verify.py`` 文件头「空转也算没跑」）。
ZERO_CALLS_NOTE = (
    "本 trace 一条用量都没记到。这不等于没花：构造 Agent 时未传 store= 的场景里，"
    "模型照常被调用，只是记账被跳过（见 docs/BACKLOG.md ## task-T29）。"
    "「真的没调」与「调了没记」在这个数字上分不开，不要读成零成本。"
)

FAILURE_NOTE = (
    "失败的模型调用单列在这里，不并进上面的 calls / tokens。它们照样烧掉了输入侧的 "
    "token，但网关没回用量，所以这里一个 token 数都不给 —— 不知道就是不知道。"
    "只读成本视图会把「这条链路很省」和「这条链路一直在失败」读成同一件事。"
)


def _bucket(dst: dict, key: Any, row: dict) -> None:
    b = dst.setdefault(key, {"calls": 0, "tokens_in": 0, "tokens_out": 0,
                             "tokens_total": 0, "latency_ms": 0})
    b["calls"] += 1
    b["tokens_in"] += int(row.get("tokens_in") or 0)
    b["tokens_out"] += int(row.get("tokens_out") or 0)
    b["tokens_total"] = b["tokens_in"] + b["tokens_out"]
    b["latency_ms"] += int(row.get("latency_ms") or 0)


def _ranked(buckets: dict, label: str) -> list[dict]:
    """按 tokens_total 降序、同分按 key 升序 —— 排序必须确定。

    ``verify.py`` 第 4 项拿库重放一遍与 ``trace.json`` **逐字节**比对，
    任何不稳定的顺序都会让那一项在没人动过证据的情况下变红。
    """
    return [{label: k, **v} for k, v in sorted(
        buckets.items(), key=lambda kv: (-kv[1]["tokens_total"], str(kv[0])))]


def failure_view(rows: list[dict] | None, *, unavailable: str = "") -> dict:
    """一组 ``model_call_failure`` 行的聚合（T54）。形状恒定，口径同 ``cost_view``。

    失败的调用**没有 token 数**（网关根本没回），所以这里一个 token 字段都不出 ——
    出了就得编。它出的是「有过几次、烧了多少墙钟、错在哪一类、哪个调用点在错」。

    为什么非要单列一块：一次超时或被限流的调用照样烧掉了输入侧的钱，而它在
    ``cost_view`` 里**完全不存在**。只看成本视图的人会把「这条链路很省」和
    「这条链路一直在失败重试」读成同一件事。
    """
    if rows is None:
        return {"available": False,
                "unavailable_reason": unavailable or "未提供失败行",
                "calls": 0, "latency_ms": 0, "by_error_kind": [], "by_call_site": [],
                "note": FAILURE_NOTE}
    kinds: dict = {}
    sites: dict = {}
    latency = 0
    for r in rows:
        kinds[r.get("error_kind") or "unknown"] = (
            kinds.get(r.get("error_kind") or "unknown", 0) + 1)
        sites[r.get("call_site") or "unknown"] = (
            sites.get(r.get("call_site") or "unknown", 0) + 1)
        latency += int(r.get("latency_ms") or 0)
    return {
        "available": not unavailable,
        "unavailable_reason": unavailable or None,
        "calls": len(rows),
        "latency_ms": latency,
        # 恒按 (次数降序, 名字升序) —— 与 _ranked 同一个排序口径，可复现。
        "by_error_kind": [{"error_kind": k, "calls": v}
                          for k, v in sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0]))],
        "by_call_site": [{"call_site": k, "calls": v}
                         for k, v in sorted(sites.items(), key=lambda kv: (-kv[1], kv[0]))],
        "note": FAILURE_NOTE,
    }


def cost_view(rows: list[dict], *, unavailable: str = "",
              failures: list[dict] | None = None,
              failures_unavailable: str = "") -> dict:
    """一组 ``model_usage`` 行的成本聚合。形状恒定，取不到就 ``available=false``。

    「取不到」和「一次都没花」必须分得开：两者都能让总数是 0，但前者是**不知道**，
    后者是**知道且为零**。合成一个 0 会让「成本记账没接上」长得像「这条链路很省」。

    ``failures`` 是 T54 加的第二本账（``model_call_failure`` 行）。它**不并进**
    ``calls`` / ``tokens_*`` 任何一个数 —— 成功与失败混算会让「花了多少」不可读；
    它挂在 ``failures`` 键下单列。缺省 None 是「没人告诉我」，不是「一次没失败」。
    """
    roles: dict = {}
    tasks: dict = {}
    sites: dict = {}
    totals = {"calls": 0, "tokens_in": 0, "tokens_out": 0, "tokens_total": 0,
              "latency_ms": 0}
    estimated = 0
    for r in rows:
        _bucket(roles, r.get("agent_role") or "unknown", r)
        _bucket(sites, r.get("call_site") or "unknown", r)
        if r.get("task_id"):
            _bucket(tasks, r["task_id"], r)
        totals["calls"] += 1
        totals["tokens_in"] += int(r.get("tokens_in") or 0)
        totals["tokens_out"] += int(r.get("tokens_out") or 0)
        totals["latency_ms"] += int(r.get("latency_ms") or 0)
        estimated += 1 if r.get("estimated") else 0
    totals["tokens_total"] = totals["tokens_in"] + totals["tokens_out"]

    by_task = _ranked(tasks, "task_id")
    return {
        "available": not unavailable,
        "unavailable_reason": unavailable or None,
        **totals,
        "estimated_calls": estimated,
        "measured_calls": totals["calls"] - estimated,
        # 全是估算时这面旗子必须为真 —— 它是「别把这串数字当钱读」的机器可读版本。
        "all_estimated": totals["calls"] > 0 and estimated == totals["calls"],
        "by_role": _ranked(roles, "role"),
        "by_call_site": _ranked(sites, "call_site"),
        "by_task": by_task,
        # 「哪个 task 最贵」。task_id 为空的行进不了这张榜（它们连 task 都归不上），
        # 但仍然计进 totals —— 榜上无名不等于没花。
        "top_task": by_task[0] if by_task else None,
        "note": ESTIMATED_NOTE,
        # 只在 calls==0 时非空 —— 形状恒定，有话说的时候才有话。
        "zero_calls_note": None if (unavailable or totals["calls"]) else ZERO_CALLS_NOTE,
        # 失败的调用单列，一个 token 都不并进上面的数（见 failure_view）。
        "failures": failure_view(failures, unavailable=failures_unavailable),
    }


def _cost_rows(store: Any, trace_id: str) -> tuple[list[dict], str]:
    """取一条 trace 的用量行。返回 ``(rows, 取不到的理由)``，理由为空即取到了。

    三种取不到都如实说：后端没有这个方法（早于 T29 的实现）、表还没建（旧库）、
    以及 plan 自己就没有 trace_id。最后一种尤其要拦住 —— 拿空串去查会把所有
    **归属不上**的用量行一股脑算进这棵树，那正好是本模块最不该干的事：给指不到
    出处的东西安一个出处。
    """
    if not trace_id:
        return [], "plan 没有 trace_id，无法按 Run id 归集用量"
    fn = getattr(store, "list_model_usage", None)
    if fn is None:
        return [], "store 未实现 list_model_usage（后端早于 T29 的成本记账）"
    try:
        return list(fn(trace_id=trace_id)), ""
    except Exception as exc:                    # noqa: BLE001 —— 旧库没有 model_usage 表
        return [], f"读 model_usage 失败：{type(exc).__name__}"


def _failure_rows(store: Any, trace_id: str) -> tuple[list[dict] | None, str]:
    """取一条 trace 的失败调用行。返回 ``(rows, 取不到的理由)``，口径同 ``_cost_rows``。

    取不到时返回的是 ``None`` 而不是 ``[]`` —— 空列表是「查过了，一次没失败」，
    None 是「没查成」。两者在 ``failure_view`` 里显示成不同的 ``available``，
    合成一个空列表就会让「这个后端没有失败记账」长得像「这条链路从没失败过」。
    """
    if not trace_id:
        return None, "plan 没有 trace_id，无法按 Run id 归集失败调用"
    fn = getattr(store, "list_model_call_failures", None)
    if fn is None:
        return None, "store 未实现 list_model_call_failures（后端早于 T54 的失败记账）"
    try:
        return list(fn(trace_id=trace_id)), ""
    except Exception as exc:                    # noqa: BLE001 —— 旧库没有这张表
        return None, f"读 model_call_failure 失败：{type(exc).__name__}"


def export_trace(store: Any, plan_id: str) -> dict:
    """把一个 Plan 的全部事件与产物导成 span 树。

    ``store`` 用到 ``get_plan / list_tasks / list_artifacts / list_event_log`` 四个
    只读方法 —— 任何实现了 ``maos.core.store.Store`` 的后端都能喂进来。第五个
    ``list_model_usage`` 是**可选**的（成本视图，T29）：后端没有它就把 ``cost``
    标成 ``available=false`` 并说明理由，不影响 span 树本身。
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
        tid = e.get("task_id")
        parent = task_span.get(tid) if tid else root_id
        attrs = _event_attrs(e)
        if tid and parent is None:
            # 事件指向一个 task 表里没有的 task_id：挂到 plan 根上并留记号，
            # 不丢事件、也不假装它属于某个已知任务。
            parent = root_id
            attrs["maos.task_id.resolved"] = False
            unknown_task_events += 1
        start = e.get("created_at")
        spans.append(_span(
            trace_id=e.get("trace_id") or trace_id,
            span_id=_sid("event", e.get("seq")), parent_span_id=parent,
            name=_event_name(e), kind=KIND_EVENT,
            start=start, end=_event_end(e, start), attributes=attrs,
        ))

    submits = _submit_index(events, tasks)
    comp_spans: dict[str, list[tuple[str, Any]]] = {}
    for e in events:
        if e.get("event_type") == "CompensationAttached" and e.get("task_id"):
            comp_spans.setdefault(e["task_id"], []).append(
                (_sid("event", e.get("seq")), (e.get("detail") or {}).get("patch_ref")))

    seeded = _seeded_index(events)

    unsourced = 0
    seeded_artifacts = 0
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
            seed = seeded.get(str(art.get("artifact_id")))

            if kind == "compensation":
                for sid, patch_ref in comp_spans.get(tid, []):
                    if patch_ref == (art.get("content") or {}).get("patch_ref"):
                        prov, parent, ref_span = PROV_COMPENSATION, sid, sid
                        break
            elif seed is not None:
                # 点名的来源优先于时间窗猜测：``ArtifactSeeded`` 指名道姓说了
                # 这份是它落的，没有哪个窗口比这更有资格认领它。
                prov = PROV_ARTIFACT_SEEDED
                parent = ref_span = seed["span_id"]
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
            elif prov == PROV_ARTIFACT_SEEDED:
                seeded_artifacts += 1

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
                    # 产它的那个函数，原样搬自 ``ArtifactSeeded`` 的 detail.source。
                    # 只有旁路产物有；正路产物的来源就是那条 submit 事件本身。
                    "maos.artifact.provenance.source": (
                        (seed or {}).get("source") if prov == PROV_ARTIFACT_SEEDED else None),
                    "maos.artifact.provenance.note": (
                        "无来源事件：该产物未经 on_task_result 入库，"
                        "审计链上查不到是谁在哪一步产的（docs/BACKLOG.md task-C 第 5 条）"
                        if prov == PROV_UNKNOWN else
                        "旁路入库：未经 on_task_result，但来源由 ArtifactSeeded 事件"
                        "点名声明（见 event_span / source）。这说的是入库路径，"
                        "不是内容真伪 —— 判真伪看下面的 sandbox.mode"
                        if prov == PROV_ARTIFACT_SEEDED else None
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
    usage_rows, usage_gap = _cost_rows(store, trace_id)
    failure_rows, failure_gap = _failure_rows(store, trace_id)
    return {
        "schema": SCHEMA,
        "plan_id": plan_id,
        "trace_id": trace_id,
        "plan_state": plan.get("state"),
        "goal": plan.get("goal"),
        "spans": spans,
        # 成本挂在 trace_id 上，与 span 树、event_log 同一个关联键 —— 复赛规则要的
        # 「trace / Log / Metrics 关联到同一个 Run id」就是这一个键，不另造。
        "cost": cost_view(usage_rows, unavailable=usage_gap,
                          failures=failure_rows, failures_unavailable=failure_gap),
        "summary": {
            "span_count": len(spans),
            "task_count": len(tasks),
            "event_count": len(events),
            "artifact_count": sum(1 for s in spans if s["kind"] == KIND_ARTIFACT),
            "unsourced_artifacts": unsourced,
            # 走旁路入库、但自报了来源的份数。与 unsourced 分开数：审计链补上了，
            # 「这些没走 on_task_result」这件事仍然要一眼看得见，不许随着洞被补上
            # 一起消失 —— 那样等于用一次修复换掉一条判据。
            "seeded_artifacts": seeded_artifacts,
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


# ---------------------------------------------------------------------------
# 第三族树：圆桌（T134）
# ---------------------------------------------------------------------------
#: 每棵圆桌树都跟着这句话。圆桌**不是** Plan 的一部分，读者必须知道它为什么
#: 单列一族 —— 否则「这几棵树怎么没有 task」会被读成导出漏了东西。
ROUNDTABLE_NOTE = (
    "圆桌是旁路观察者：它跑在 create_plan 之前，不建 plan、不建 task，所以没有 "
    "trace_id、也进不了任何一棵 plan 树（给它造一条 plan 行会让 DAG 凭空多出几棵"
    "不是任务的树，见 maos/roundtable/speaker.py 模块 docstring）。这一族树按 "
    "event_log.plan_id 的 roundtable: 前缀聚出来，顺序即 seq 顺序，与 "
    "scripts/replay_roundtable.py 从同一批行重建出来的一致。"
)

#: 一段圆桌一条用量都没记到时跟着的话。比 :data:`ZERO_CALLS_NOTE` 说得更准 ——
#: 圆桌这一侧「调了没记」的成因是**知道**的，不必把它和「真的没调」混着说。
ROUNDTABLE_ZERO_CALLS_NOTE = (
    "这一段圆桌没有模型用量行。成因是确定的：Speaker.complete 只在 live 为真时"
    "记账（maos/roundtable/speaker.py），而缺省的 ScriptedModelClient 不算 live。"
    "所以这不是「五岗没说话」—— 发言照旧落在 RoundtableSeatSpoke 里，"
    "看那几条的 spoken_by_model。要真实 token 数就跑 --live-model 那一束。"
)

#: 圆桌不记失败调用：``maos/roundtable/speaker.py`` 全包只有一处
#: ``record_model_usage``，没有 ``record_model_call_failure``。如实说「没这项记账」，
#: 而不是给一个空列表让它长得像「一次没失败过」（口径同 :func:`_failure_rows`）。
ROUNDTABLE_NO_FAILURE_LEDGER = "圆桌不记失败调用：speaker.py 只调 record_model_usage"

#: 一轮的三类事件，与 ``scripts/replay_roundtable.py`` 的同名常量同值。
_RT_ROUND = "RoundtableRound"
_RT_SPOKE = "RoundtableSeatSpoke"
_RT_VERDICT = "RoundtableVerdict"


def _rt_event_rows(conn: sqlite3.Connection) -> list[dict]:
    """圆桌那一摊事件行，按 seq 升序，``detail`` 就地解析。

    两个条件**都要**：带前缀，且 ``plan`` 表里查不到。后一条是防重复计数的 ——
    万一哪天真有一条 plan_id 带这个前缀的 plan 行（红线说不许，但判据不建立在
    别人守规矩上），那它已经有自己的 plan 树了，不该再被圆桌收一遍。
    """
    rows = conn.execute(
        "SELECT seq, event_id, trace_id, plan_id, task_id, event_type, from_state,"
        " to_state, reason, detail, created_at FROM event_log"
        " WHERE plan_id LIKE ? AND plan_id NOT IN (SELECT plan_id FROM plan)"
        " ORDER BY seq", (f"{ROUNDTABLE_PLAN_PREFIX}%",)).fetchall()
    out: list[dict] = []
    for r in rows:
        row = dict(r)
        try:
            row["detail"] = json.loads(row["detail"] or "{}")
        except ValueError:                              # 那一列不是合法 JSON
            row["detail"] = {"_unparsed": row["detail"]}
        if not isinstance(row["detail"], dict):
            row["detail"] = {"_unparsed": row["detail"]}
        out.append(row)
    return out


def _rt_usage_rows(conn: sqlite3.Connection) -> list[dict]:
    """圆桌那一摊用量行，按 seq 升序。

    ``trace_id=''`` 这一条**必须留着**：它让圆桌树与 plan 树的用量行**没有交集**
    （plan 树按非空 trace_id 归集），否则同一行会被两边各算一次，而
    ``summary.model_calls`` 那个数看不出来是重了。

    表不存在（早于 T29 的库）返回空清单，口径同 :func:`unattributed_usage`。
    """
    try:
        rows = conn.execute(
            "SELECT seq, plan_id, task_id, agent_role, call_site, model, tier,"
            " tokens_in, tokens_out, latency_ms, estimated, created_at FROM model_usage"
            " WHERE trace_id='' AND plan_id LIKE ? ORDER BY seq",
            (f"{ROUNDTABLE_PLAN_PREFIX}%",)).fetchall()
    except sqlite3.Error:
        return []
    return [dict(r) for r in rows]


def _rt_rounds(events: list[dict]) -> list[dict]:
    """把一摊圆桌事件切成一轮一轮。**口径与 ``scripts/replay_roundtable.py::rebuild``
    一致**（那个脚本已经对了，本函数拿它当对照，不 import：它在 ``scripts/`` 下，
    而本模块只 import ``maos.core.store``）。

    以 ``RoundtableRound`` 开一轮，其后的座位、skill、合议都归它，直到下一条
    ``RoundtableRound``。开头就没有 ``RoundtableRound``（引擎更早的版本、或那条
    落库失败）时起一个**无头轮**把后面的事件收住 —— 丢掉它们等于这一段少报几岗，
    而少报与「本来就没说」在屏幕上长得一模一样。
    """
    rounds: list[dict] = []

    def _open(e: dict, *, headless: bool) -> dict:
        detail = e["detail"] if not headless else {}
        rounds.append({
            "span_id": (_sid("event", e["seq"]) if not headless
                        else _sid("roundtable-round", e["plan_id"], e["seq"])),
            "seq": e["seq"],
            "round_no": detail.get("round_no"),
            "entry": str(detail.get("entry") or ""),
            "tenant_id": str(detail.get("tenant_id") or ""),
            "case_id": str(detail.get("case_id") or ""),
            "sheet_digest": str(detail.get("sheet_digest") or ""),
            "seats_expected": list(detail.get("seats") or []),
            "headless": headless,
            "events": [] if headless else [e],
            "seats": [], "skills": [], "verdict": None,
        })
        return rounds[-1]

    current: dict | None = None
    for e in events:
        kind = e["event_type"]
        detail = e["detail"]
        if kind == _RT_ROUND:
            current = _open(e, headless=False)
            continue
        if current is None:
            current = _open(e, headless=True)
        current["events"].append(e)
        if kind == _RT_SPOKE:
            current["seats"].append({
                "seq": e["seq"], "seat": str(detail.get("seat") or ""),
                "spoken_by_model": bool(detail.get("spoken_by_model")),
                "fallback_reason": str(detail.get("fallback_reason") or ""),
                "facts_digest": str(detail.get("facts_digest") or ""),
                "speech_digest": str(detail.get("speech_digest") or ""),
                "speech_len": detail.get("speech_len"),
            })
        elif kind == "SkillInvoked":
            current["skills"].append({
                "seq": e["seq"], "skill": str(detail.get("skill") or ""),
                "status": str(detail.get("status") or ""),
                "task_id": e.get("task_id"),
                "duration_ms": detail.get("duration_ms"),
                "invocation_id": str(detail.get("invocation_id") or ""),
            })
        elif kind == _RT_VERDICT:
            current["verdict"] = {
                "seq": e["seq"],
                "recommend": str(detail.get("recommend") or ""),
                "approver_role": str(detail.get("approver_role") or ""),
                "blockers": list(detail.get("blockers") or []),
            }
    return rounds


def _rt_spans(plan_id: str, case_id: str, rounds: list[dict],
              events: list[dict]) -> list[dict]:
    """圆桌那一段的 span 树：``roundtable`` 根 → 每轮一个 ``roundtable-round``
    → 每条事件一个 ``event`` 叶子。

    **每一条**收进来的事件都会得到一个叶子 span，认不认识它的类型都一样
    （名字走 :func:`_event_name` 的 ``event:<type>`` 兜底）。这一条是要紧的：
    ``stray_events`` 之后按「有没有被树收走」判游离，漏掉一条事件就等于把它
    **既**从树里丢了、**又**从游离清单里藏了 —— 两头都看不见才是真没了。
    """
    spans: list[dict] = []
    root_id = _sid(KIND_ROUNDTABLE, plan_id)
    stamps = [e["created_at"] for e in events if e.get("created_at")]
    spans.append(_span(
        trace_id="", span_id=root_id, parent_span_id=None,
        name=f"{KIND_ROUNDTABLE}:{case_id or '(无 case_id)'}", kind=KIND_ROUNDTABLE,
        start=min(stamps) if stamps else None, end=max(stamps) if stamps else None,
        attributes={
            # 伪 plan_id 原样落进来。它是这一摊行在库里的**真实归属键**，
            # 不是这里编出来的展示串 —— 拿它去 event_log / model_usage 一查就有。
            "maos.plan_id": plan_id,
            "maos.roundtable.case_id": case_id,
            "maos.roundtable.rounds": len(rounds),
            "maos.roundtable.tenant_id": next(
                (r["tenant_id"] for r in rounds if r["tenant_id"]), ""),
            "maos.roundtable.note": ROUNDTABLE_NOTE,
        },
    ))

    for rnd in rounds:
        kids = rnd["events"]
        ends = [_event_end(e, e.get("created_at")) for e in kids]
        ends = [x for x in ends if x]
        head = next((e for e in kids if e["event_type"] == _RT_ROUND), None)
        attrs: dict[str, Any] = (_event_attrs(head) if head is not None else {
            "maos.event.seq": rnd["seq"],
            "maos.event.type": None,
            "maos.event.id": None,
            "maos.task_id": None,
            "maos.reason": None,
            "maos.detail": {},
        })
        attrs["maos.roundtable.round_no"] = rnd["round_no"]
        attrs["maos.roundtable.entry"] = rnd["entry"]
        attrs["maos.roundtable.seats_expected"] = rnd["seats_expected"]
        attrs["maos.roundtable.seats_spoke"] = [s["seat"] for s in rnd["seats"]]
        attrs["maos.roundtable.headless"] = rnd["headless"]
        # 无头轮必须在树上说出来：它意味着 RoundtableRound 那条没落下来，
        # 而「这一轮的名册是什么」就此查不到了。静静收拢等于把缺口抹掉。
        attrs["maos.roundtable.headless.note"] = (
            "这一段没有 RoundtableRound 开头，是按 seq 收拢出来的：名册（seats）"
            "与轮次号查不到，只能看实际发言的那几岗" if rnd["headless"] else None)
        spans.append(_span(
            trace_id="", span_id=rnd["span_id"], parent_span_id=root_id,
            name=(_event_name(head) if head is not None
                  else f"{KIND_ROUNDTABLE_ROUND}:(无 RoundtableRound 开头)"),
            kind=KIND_ROUNDTABLE_ROUND,
            start=(head or kids[0])["created_at"] if kids else None,
            end=max(ends) if ends else None, attributes=attrs,
        ))
        for e in kids:
            if e is head:
                continue                # 这一轮的头已经是那个 round span 本身
            start = e.get("created_at")
            spans.append(_span(
                trace_id="", span_id=_sid("event", e["seq"]),
                parent_span_id=rnd["span_id"], name=_event_name(e), kind=KIND_EVENT,
                start=start, end=_event_end(e, start), attributes=_event_attrs(e),
            ))

    spans.sort(key=lambda s: (s.get("start") or "", s["kind"], s["span_id"]))
    return spans


def roundtable_traces(db_path: str) -> list[dict]:
    """圆桌那一段，每个伪 plan_id 一棵树。**这是 ``traces`` 之外的第三族**。

    为什么非要单列一族，而不是并进 ``traces``：``traces`` 的每一棵都对应一条真的
    ``plan`` 行（``plan_state`` / ``task_count`` / 按 trace_id 归集的成本都从那儿来），
    而圆桌一条都没有。混进去的话，下游每一个「按 plan 读」的消费者都得先判断这棵树
    是不是真 plan —— 漏判一处就会印出一棵 ``plan_state: null`` 的树，看着像库坏了。

    形状与 plan 树刻意**平行**（``spans`` / ``cost`` / ``summary`` 同名同义），所以
    ``check_span_tree``、渲染器的树组件、``verify.py`` 的重放比对都能原样吃。
    """
    conn = _connect_ro(db_path)
    try:
        events = _rt_event_rows(conn)
        usage = _rt_usage_rows(conn)
    finally:
        conn.close()

    by_plan: dict[str, list[dict]] = {}
    for e in events:
        by_plan.setdefault(e["plan_id"], []).append(e)
    usage_by_plan: dict[str, list[dict]] = {}
    for r in usage:
        usage_by_plan.setdefault(r["plan_id"], []).append(r)

    out: list[dict] = []
    # 只有用量行、一条事件都没有的伪 plan 也要成树 —— 否则那几次调用会既不在树里、
    # 又被下面的「已收走」判据从 unattributed_usage 里摘掉，两头都看不见。
    for plan_id in sorted(set(by_plan) | set(usage_by_plan)):
        plan_events = by_plan.get(plan_id, [])
        rows = usage_by_plan.get(plan_id, [])
        case_id = plan_id[len(ROUNDTABLE_PLAN_PREFIX):]
        rounds = _rt_rounds(plan_events)
        spans = _rt_spans(plan_id, case_id, rounds, plan_events)
        cost = cost_view(rows, failures=None,
                         failures_unavailable=ROUNDTABLE_NO_FAILURE_LEDGER)
        if not rows:
            # 比 cost_view 的缺省那句说得准（见 ROUNDTABLE_ZERO_CALLS_NOTE）。
            cost["zero_calls_note"] = ROUNDTABLE_ZERO_CALLS_NOTE
        by_type: dict[str, int] = {}
        for e in plan_events:
            by_type[e["event_type"]] = by_type.get(e["event_type"], 0) + 1
        out.append({
            "schema": SCHEMA,
            "kind": KIND_ROUNDTABLE,
            "plan_id": plan_id,
            "case_id": case_id,
            # 圆桌不属于任何 Run。空串是**如实记录**，不是漏填 —— 编一个
            # trace_id 让它看起来有归属，正是本模块最不该干的那件事。
            "trace_id": "",
            "note": ROUNDTABLE_NOTE,
            "spans": spans,
            "rounds": [{k: v for k, v in r.items() if k != "events"} for r in rounds],
            # 这一段的用量行原样列出（投影同 unattributed_usage），既给页面按岗位
            # 印明细，也让 verify.py 能逐条核「这几行确实被这棵树收走了」。
            "model_usage": [{k: v for k, v in r.items() if k != "plan_id"} for r in rows],
            "cost": cost,
            "summary": {
                "span_count": len(spans),
                "event_count": len(plan_events),
                "round_count": len(rounds),
                "headless_rounds": sum(1 for r in rounds if r["headless"]),
                "seat_spoke_count": sum(len(r["seats"]) for r in rounds),
                "seats_by_model": sum(1 for r in rounds for s in r["seats"]
                                      if s["spoken_by_model"]),
                "skill_invoked_count": sum(len(r["skills"]) for r in rounds),
                "verdict_count": sum(1 for r in rounds if r["verdict"] is not None),
                "by_event_type": dict(sorted(by_type.items())),
                "model_calls": cost["calls"],
                "tokens_total": cost["tokens_total"],
                "tree_errors": check_span_tree(spans),
            },
        })
    return out


# ---------------------------------------------------------------------------
# 第四族：客服前台（p14 · T176）
# ---------------------------------------------------------------------------
#: 每棵 cs 树都跟着这句话，理由同 :data:`ROUNDTABLE_NOTE`。
CS_NOTE = (
    "客服前台不建 plan、不建 task：会话进度是会话对象自己的字段（cs_conversation.stage），"
    "不是 Task 状态（铁律 9）。所以这一族没有 trace_id，按 event_log.plan_id 的 cs: 前缀"
    "聚出来（cs:<会话 id> 或 cs:mcp），一轮一个 cs-turn 节点（task_id 即轮次 id），"
    "顺序即 seq 顺序。审计行只落摘要与枚举，客户原文住在 cs_turn 里。"
)

#: 一段 cs 一条用量都没记到时跟着的话（缺省 Scripted / 确定性理解层不调模型）。
CS_ZERO_CALLS_NOTE = (
    "这一段客服前台没有模型用量行：理解层确定性优先，只有注入真模型且规则判不出意图时"
    "才调模型；缺省 Scripted / None 下零 model_usage 行（review/p13-cs-contracts.md T173）。"
)

#: cs 那一族不记失败调用的专表口径，同圆桌：如实说，不给空列表冒充「一次没失败过」。
CS_NO_FAILURE_LEDGER = "客服前台的失败调用不进本树：cs 家族的 cost 只归集 model_usage 行"


def _cs_event_rows(conn: sqlite3.Connection) -> list[dict]:
    """cs 那一摊事件行，按 seq 升序、``detail`` 就地解析（口径同 :func:`_rt_event_rows`）。"""
    n = len(CS_PLAN_PREFIX)
    rows = conn.execute(
        "SELECT seq, event_id, trace_id, plan_id, task_id, event_type, from_state,"
        " to_state, reason, detail, created_at FROM event_log"
        " WHERE substr(plan_id, 1, ?) = ? AND plan_id NOT IN (SELECT plan_id FROM plan)"
        " ORDER BY seq", (n, CS_PLAN_PREFIX)).fetchall()
    out: list[dict] = []
    for r in rows:
        row = dict(r)
        try:
            row["detail"] = json.loads(row["detail"] or "{}")
        except ValueError:
            row["detail"] = {"_unparsed": row["detail"]}
        if not isinstance(row["detail"], dict):
            row["detail"] = {"_unparsed": row["detail"]}
        out.append(row)
    return out


def _cs_usage_rows(conn: sqlite3.Connection) -> list[dict]:
    """cs 那一摊用量行：``trace_id=''`` 且前缀对得上（与 plan 树的用量没有交集）。"""
    n = len(CS_PLAN_PREFIX)
    try:
        rows = conn.execute(
            "SELECT seq, plan_id, task_id, agent_role, call_site, model, tier,"
            " tokens_in, tokens_out, latency_ms, estimated, created_at FROM model_usage"
            " WHERE trace_id='' AND substr(plan_id, 1, ?) = ? ORDER BY seq",
            (n, CS_PLAN_PREFIX)).fetchall()
    except sqlite3.Error:
        return []
    return [dict(r) for r in rows]


def _cs_spans(plan_id: str, events: list[dict]) -> list[dict]:
    """cs 那一段的 span 树：``cs`` 根 → 每个 task_id（轮次 / MCP 调用）一个 ``cs-turn``
    → 每条事件一个 ``event`` 叶子。**每一条**事件都落成叶子（理由同 :func:`_rt_spans`）。"""
    spans: list[dict] = []
    root_id = _sid(KIND_CS, plan_id)
    stamps = [e["created_at"] for e in events if e.get("created_at")]
    turns: dict[str, list[dict]] = {}
    for e in events:
        turns.setdefault(str(e.get("task_id") or ""), []).append(e)
    spans.append(_span(
        trace_id="", span_id=root_id, parent_span_id=None,
        name=f"{KIND_CS}:{plan_id[len(CS_PLAN_PREFIX):]}", kind=KIND_CS,
        start=min(stamps) if stamps else None, end=max(stamps) if stamps else None,
        attributes={"maos.plan_id": plan_id, "maos.cs.turns": len(turns),
                    "maos.cs.note": CS_NOTE},
    ))
    for task_id, kids in turns.items():
        turn_id = _sid(KIND_CS_TURN, plan_id, task_id)
        k_stamps = [e["created_at"] for e in kids if e.get("created_at")]
        ends = [x for x in (_event_end(e, e.get("created_at")) for e in kids) if x]
        spans.append(_span(
            trace_id="", span_id=turn_id, parent_span_id=root_id,
            name=f"{KIND_CS_TURN}:{task_id or '(无 task_id)'}", kind=KIND_CS_TURN,
            start=min(k_stamps) if k_stamps else None, end=max(ends) if ends else None,
            attributes={"maos.task_id": task_id, "maos.cs.event_count": len(kids)},
        ))
        for e in kids:
            start = e.get("created_at")
            spans.append(_span(
                trace_id="", span_id=_sid("event", e["seq"]), parent_span_id=turn_id,
                name=_event_name(e), kind=KIND_EVENT, start=start,
                end=_event_end(e, start), attributes=_event_attrs(e),
            ))
    spans.sort(key=lambda s: (s.get("start") or "", s["kind"], s["span_id"]))
    return spans


def cs_traces(db_path: str) -> list[dict]:
    """客服前台那一族，每个 ``cs:`` plan_id 一棵树（p14 · T176）。结构照 :func:`roundtable_traces`：
    ``plan_id`` / ``events`` / ``model_usage`` / ``spans`` / ``cost`` / ``summary``。
    库里没有 cs 行时返回空清单（导出方据此**不写** ``cs_traces`` 键）。"""
    conn = _connect_ro(db_path)
    try:
        events = _cs_event_rows(conn)
        usage = _cs_usage_rows(conn)
    finally:
        conn.close()

    by_plan: dict[str, list[dict]] = {}
    for e in events:
        by_plan.setdefault(e["plan_id"], []).append(e)
    usage_by_plan: dict[str, list[dict]] = {}
    for r in usage:
        usage_by_plan.setdefault(r["plan_id"], []).append(r)

    out: list[dict] = []
    for plan_id in sorted(set(by_plan) | set(usage_by_plan)):
        plan_events = by_plan.get(plan_id, [])
        rows = usage_by_plan.get(plan_id, [])
        spans = _cs_spans(plan_id, plan_events)
        cost = cost_view(rows, failures=None, failures_unavailable=CS_NO_FAILURE_LEDGER)
        if not rows:
            cost["zero_calls_note"] = CS_ZERO_CALLS_NOTE
        by_type: dict[str, int] = {}
        for e in plan_events:
            by_type[e["event_type"]] = by_type.get(e["event_type"], 0) + 1
        out.append({
            "schema": SCHEMA,
            "kind": KIND_CS,
            "plan_id": plan_id,
            # 客服前台不属于任何 Run：空串是如实记录（同圆桌）。
            "trace_id": "",
            "note": CS_NOTE,
            "events": [{"seq": e["seq"], "event_type": e["event_type"],
                        "task_id": e["task_id"], "created_at": e["created_at"]}
                       for e in plan_events],
            "spans": spans,
            "model_usage": [{k: v for k, v in r.items() if k != "plan_id"} for r in rows],
            "cost": cost,
            "summary": {
                "span_count": len(spans),
                "event_count": len(plan_events),
                "turn_count": len({str(e.get("task_id") or "") for e in plan_events}),
                "by_event_type": dict(sorted(by_type.items())),
                "model_calls": cost["calls"],
                "tokens_total": cost["tokens_total"],
                "tree_errors": check_span_tree(spans),
            },
        })
    return out


def stray_events(db_path: str, *, claimed: Any = frozenset()) -> list[dict]:
    """``plan_id`` 指不到任何 plan 行、**又没被任何一族树收走**的事件。

    现实里确实有：``flows/scenario_5.py`` 的 ``issue.aggregate`` 跑在 create_plan
    之前，落的 SkillInvoked 行 ``plan_id`` 是空串。这类事件按 plan 查永远查不到，
    所以在这里单独点名，而不是让它们静静消失（**该例已于 P5 D 轨并进树**，
    此处留作判据来由，不是现存例子）。

    ``claimed`` 是**已经被某一族树收走的 seq 集合**（T134：圆桌树收走它那一摊）。
    判据是「被树收走的不算游离」，**不是**「plan_id 非空就不算游离」—— 后者会把
    第二种形态（``plan_id`` 指向一个不存在的 Plan，``verify.py`` 的
    ``_warn_stray_events`` 里那条「这一种要查」）一起藏掉，而那正是真该查的一种。
    按 seq 认而不按 plan_id 前缀认，也是同一个道理：圆桌树万一漏收一条事件，
    它照旧在这里被点名，而不是靠「前缀对得上」蒙一个豁免。
    """
    conn = _connect_ro(db_path)
    try:
        rows = conn.execute(
            "SELECT seq, event_type, plan_id, task_id, trace_id, created_at FROM event_log"
            " WHERE plan_id NOT IN (SELECT plan_id FROM plan) ORDER BY seq").fetchall()
        return [dict(r) for r in rows if r["seq"] not in claimed]
    finally:
        conn.close()


def unattributed_usage(db_path: str, *, claimed: Any = frozenset()) -> list[dict]:
    """``trace_id`` 为空、**又没被任何一族树收走**的用量行（口径同 ``stray_events``）。

    现实里确实有：``ManagerAgent.plan()`` 跑在 ``create_plan`` **之前**
    （``flows/scenario_1.py`` 等把 ``mgr.plan(GOAL)`` 当作 ``create_plan`` 的入参），
    那一刻还没有 plan 行、也没有 trace_id 可挂。这些调用照旧花掉了 token，
    所以既不丢掉、也不硬安一个 trace_id，而是在这里单独点名。

    ``claimed`` 同 :func:`stray_events`：T134 之后圆桌那几次调用归到圆桌树的
    ``cost`` 里，不再算「归属不上」。**剩下的照旧逐条点名** —— 上面那条
    ``ManagerAgent.plan()`` 的行 ``plan_id`` 也是空串，前缀对不上、也不在任何一棵
    圆桌树里，所以它一条都不会被这次收窄带走。

    表不存在（早于 T29 的库）返回空清单 —— 那是「没有这项记账」，由每棵树自己的
    ``cost.available=false`` 说清楚，不在这里重复报错。
    """
    conn = _connect_ro(db_path)
    try:
        rows = conn.execute(
            "SELECT seq, agent_role, call_site, model, tier, tokens_in, tokens_out,"
            " latency_ms, estimated, created_at FROM model_usage WHERE trace_id=''"
            " ORDER BY seq").fetchall()
        return [dict(r) for r in rows if r["seq"] not in claimed]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def export_trace_bundle(db_path: str, *, store_factory: Any = None) -> dict:
    """整库导出：每个 plan 一棵树、圆桌那一段各自一棵树，外加真正游离的清单。

    这就是写进 ``evidence/scenario-<N>/trace.json`` 的那份文档。

    三族东西的边界是**互斥且穷尽**的，这一点是本函数的骨架：

    * ``traces``：每条真 ``plan`` 行一棵。事件按 plan_id 归，用量按**非空**
      trace_id 归。
    * ``roundtable_traces``：``plan_id`` 带 ``roundtable:`` 前缀那一摊（T134）。
      事件按前缀归，用量按「trace_id 为空**且**前缀对得上」归。
    * ``cs_traces``：``plan_id`` 以 ``cs:`` 开头那一摊（p14 · T176），口径同圆桌；
      库里没有 cs 行时这个键（与 summary 的 cs_* 计数）不出现。
    * ``stray_events`` / ``unattributed_usage``：上面两族**一条都没收走**的剩余。

    所以「圆桌被收走了」不会让任何一条记录凭空消失：它换了个地方，而那个地方
    在同一份文档里、在同一个 ``summary`` 里数得到。
    """
    from maos.core.store import SqliteStore

    if not os.path.exists(db_path):
        raise TraceError(f"数据库不存在: {db_path}")
    factory = store_factory or SqliteStore
    store = factory(db_path)
    traces = [export_trace(store, pid) for pid in list_plan_ids(db_path)]
    rt_traces = roundtable_traces(db_path)
    # 「已被树收走」按 seq 认，认的是**真的进了 span 树**的那些事件（见 _rt_spans
    # 的 docstring）—— 不是「前缀对得上就豁免」。漏收一条就该在游离清单里现身。
    rt_event_seqs = frozenset(
        s["attributes"].get("maos.event.seq") for t in rt_traces for s in t["spans"]
        if s["kind"] in (KIND_EVENT, KIND_ROUNDTABLE_ROUND)
        and s["attributes"].get("maos.event.type") is not None)
    rt_usage_seqs = frozenset(r["seq"] for t in rt_traces for r in t["model_usage"])
    # 客服前台那一族（p14 · T176），认领口径同圆桌：按 seq、只认真落成了 span 的事件。
    c_traces = cs_traces(db_path)
    cs_event_seqs = frozenset(
        s["attributes"].get("maos.event.seq") for t in c_traces for s in t["spans"]
        if s["kind"] == KIND_EVENT and s["attributes"].get("maos.event.type") is not None)
    cs_usage_seqs = frozenset(r["seq"] for t in c_traces for r in t["model_usage"])
    strays = stray_events(db_path, claimed=rt_event_seqs | cs_event_seqs)
    orphan_usage = unattributed_usage(db_path, claimed=rt_usage_seqs | cs_usage_seqs)
    attributed = [t["cost"] for t in traces if t["cost"]["available"]]
    rt_costs = [t["cost"] for t in rt_traces if t["cost"]["available"]]
    cs_costs = [t["cost"] for t in c_traces if t["cost"]["available"]]
    estimated_calls = (sum(c["estimated_calls"] for c in attributed)
                       + sum(c["estimated_calls"] for c in rt_costs)
                       + sum(c["estimated_calls"] for c in cs_costs)
                       + sum(1 for r in orphan_usage if r.get("estimated")))
    plan_calls = sum(c["calls"] for c in attributed)
    rt_calls = sum(c["calls"] for c in rt_costs)
    cs_calls = sum(c["calls"] for c in cs_costs)
    cs_tokens = sum(c["tokens_total"] for c in cs_costs)
    total_calls = plan_calls + rt_calls + cs_calls + len(orphan_usage)
    doc = {
        "schema": SCHEMA,
        "db": os.path.basename(db_path),
        "plan_count": len(traces),
        "traces": traces,
        # 圆桌那一段（T134）。与 traces 并列而不是混进去 —— 见 roundtable_traces。
        "roundtable_traces": rt_traces,
        "stray_events": strays,
        # 归属不上的用量单列，不并进任何一棵树的 cost（见 unattributed_usage）。
        "unattributed_usage": orphan_usage,
        "summary": {
            "span_count": sum(t["summary"]["span_count"] for t in traces),
            # 成本汇总。`model_calls` 是**三族之和**，其余三个数把它拆开：
            # 挂在 Run id 上的（plan 树）、挂在圆桌伪 plan 上的、以及一个都挂不上的。
            # 拆开是要紧的：T134 之前「圆桌那 5 次」只能在 unattributed 里看见，
            # 于是「真模型跑一单花多少 token」这个问题的答案在 attributed 里查不到。
            "model_calls": total_calls,
            "attributed_model_calls": plan_calls + rt_calls + cs_calls,
            "plan_model_calls": plan_calls,
            "roundtable_model_calls": rt_calls,
            "unattributed_model_calls": len(orphan_usage),
            "attributed_tokens_total": (sum(c["tokens_total"] for c in attributed)
                                        + sum(c["tokens_total"] for c in rt_costs)
                                        + cs_tokens),
            "plan_tokens_total": sum(c["tokens_total"] for c in attributed),
            "roundtable_tokens_total": sum(c["tokens_total"] for c in rt_costs),
            "estimated_model_calls": estimated_calls,
            "measured_model_calls": total_calls - estimated_calls,
            "all_estimated": total_calls > 0 and estimated_calls == total_calls,
            "cost_note": ESTIMATED_NOTE,
            # 圆桌那一族的规模。与 span_count / event_count 分开数：那两个数是
            # plan 树的，圆桌并进去会让「这一单有多少任务事件」当场读不准。
            "roundtable_tree_count": len(rt_traces),
            "roundtable_span_count": sum(t["summary"]["span_count"] for t in rt_traces),
            "roundtable_event_count": sum(t["summary"]["event_count"] for t in rt_traces),
            "roundtable_round_count": sum(t["summary"]["round_count"] for t in rt_traces),
            "roundtable_seat_spoke_count": sum(
                t["summary"]["seat_spoke_count"] for t in rt_traces),
            "roundtable_skill_invoked_count": sum(
                t["summary"]["skill_invoked_count"] for t in rt_traces),
            "roundtable_note": ROUNDTABLE_NOTE if rt_traces else None,
            # 失败的模型调用（T54）。只数**查得到**的那些树 —— 后端没有这张表时
            # 每棵树自己的 failures.available=false 已经说清了，这里不把
            # 「没查成」混成 0。与 model_calls 分开列：成功与失败不是一本账。
            "failed_model_calls": sum(
                t["cost"]["failures"]["calls"] for t in traces
                if t["cost"]["failures"]["available"]),
            "failure_note": FAILURE_NOTE,
            "event_count": sum(t["summary"]["event_count"] for t in traces),
            "unsourced_artifacts": sum(t["summary"]["unsourced_artifacts"] for t in traces),
            "seeded_artifacts": sum(t["summary"]["seeded_artifacts"] for t in traces),
            "degraded_sandbox_reports": sum(
                t["summary"]["degraded_sandbox_reports"] for t in traces),
            "unrecorded_sandbox_reports": sum(
                t["summary"]["unrecorded_sandbox_reports"] for t in traces),
            "stray_event_count": len(strays),
            # 两族树的错误并成一张清单：verify.py 第 4 项与页面的「审计链完整性」
            # 都读这一个数，圆桌树的孤儿/环不该有一个躲得过去的地方。
            "tree_errors": ([e for t in traces for e in t["summary"]["tree_errors"]]
                            + [e for t in rt_traces for e in t["summary"]["tree_errors"]]
                            + [e for t in c_traces for e in t["summary"]["tree_errors"]]),
        },
    }
    if not c_traces:
        # 库里没有 cs 行：一个键都不加，既有证据束的 trace.json 逐字节不变（契约 p14 §2 T176）。
        return doc
    # 有 cs 行才出现 cs_traces（紧跟 roundtable_traces，与它并列）与 summary 里的 cs_* 计数。
    ordered: dict = {}
    for key, value in doc.items():
        ordered[key] = value
        if key == "roundtable_traces":
            ordered["cs_traces"] = c_traces
    ordered["summary"].update({
        "cs_tree_count": len(c_traces),
        "cs_span_count": sum(t["summary"]["span_count"] for t in c_traces),
        "cs_event_count": sum(t["summary"]["event_count"] for t in c_traces),
        "cs_turn_count": sum(t["summary"]["turn_count"] for t in c_traces),
        "cs_model_calls": cs_calls,
        "cs_tokens_total": cs_tokens,
        "cs_note": CS_NOTE,
    })
    return ordered


def to_json(doc: dict) -> str:
    return json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=False)
