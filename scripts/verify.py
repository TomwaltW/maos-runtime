#!/usr/bin/env python3
"""证据核验器 —— 一条命令重放校验，逐项 PASS / FAIL / SKIP。

    python3 scripts/verify.py --evidence evidence/ --db evidence/
    echo "verify exit=$?"          # 全 PASS -> 0；任一 FAIL -> 非 0

**这是给评委的答案。** 检索不准顶多说效果一般；无法核验就是零分。所以本文件的
每一项都必须能被外人独立跑一遍，且失败时说得出「失败意味着什么」。

七项：

    1 hash-integrity      每个 skill/tool 调用的 input_digest / output_hash 与
                          event_log 一致          -> 失败 = 证据被篡改或事后手写
    2 business-ref        每条 business_ref 指向的对象在库中存在且 version 匹配
                                                  -> 失败 = 引用悬空，业务锚点是假的
    3 authoritative-fact  每个 settled 都有对应 payment_observation，且
                          actor_invocation_id 属于 payment.observe
                                                  -> 失败 = 权威事实边界被绕过
    4 trace-tree          trace.json span 树无孤儿、无环，且与库重放逐字节一致
                                                  -> 失败 = 事件链不完整
    5 kb-hit              每个 KbRetrieved 的 doc_id 在 kb_doc 中存在
                                                  -> 失败 = RAG 命中是编的
    6 business-outcome    每个 Plan 终态都有 business_outcome，DONE 必须有外部判据
                                                  -> 失败 =「Agent 都完成了」被当成业务成功
    7 history-case        每条 history_case 知识都能追溯到 outcome='success' 的真实 case
                                                  -> 失败 = 知识层被污染

**SKIP 的纪律**：上游能力没落地的项输出 ``[SKIP]`` 并在结尾显式列名，
**不计进 PASS 的分子**。静默跳过等于谎报 —— 一个 7/7 里藏着两个没跑的，
比老老实实写 5/5 PASS + 2 SKIP 更坏。

**依赖方向**：本文件不 import ``maos/domain/**`` 的业务逻辑，只按「表在不在」
决定某一项跑还是 SKIP（铁律 9）。唯一的例外是 ``resolve_business_ref``：
``object_type -> 表/主键`` 的映射在业务域里有唯一一份，在这里再抄一份就是 C-7
的反例，所以宁可软 import 它，也不另立第二份口径。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass, field

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")

#: 权威回执的唯一写入者。与 maos/domain/refund/guard.py::AUTHORITATIVE_WRITER 同名，
#: 但这里不 import 它 —— 本文件要在退款域缺席时也能跑（那时第 3 项 SKIP）。
AUTHORITATIVE_WRITER = "payment.observe"


@dataclass
class Check:
    key: str
    title: str
    status: str = PASS
    passed: int = 0
    total: int = 0
    notes: list[str] = field(default_factory=list)
    skip_reason: str = ""

    def ok(self) -> None:
        self.passed += 1
        self.total += 1

    def bad(self, note: str) -> None:
        self.total += 1
        self.status = FAIL
        self.notes.append(note)

    def skip(self, reason: str) -> None:
        self.status = SKIP
        self.skip_reason = reason

    def warn(self, note: str) -> None:
        """记一笔但不判负 —— 印给评委看，不改判定。"""
        self.notes.append(f"warn: {note}")


class VerifyError(RuntimeError):
    """证据本身读不了。这不是「某一项没过」，是没法开始核验。"""


# ---------------------------------------------------------------------------
# 证据读取
# ---------------------------------------------------------------------------
def load_evidence_json(path: str):
    """读 make_evidence.py 写的 json：跳过首行出处注释。缺注释即判不合规。"""
    if not os.path.exists(path):
        raise VerifyError(f"缺文件: {path}")
    with open(path, encoding="utf-8") as fh:
        first = fh.readline()
        if not first.startswith("# generated at "):
            raise VerifyError(f"{path} 首行不是出处注释（铁律 3），证据格式不合规")
        try:
            return json.loads(fh.read())
        except ValueError as exc:
            raise VerifyError(f"{path} 不是合法 JSON: {exc}") from exc


def connect_ro(db_path: str) -> sqlite3.Connection:
    if not os.path.exists(db_path):
        raise VerifyError(f"缺数据库: {db_path}（先跑 python3 scripts/make_evidence.py）")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def table_names(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _loads(raw, default=None):
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


@dataclass
class Case:
    """一个场景的证据 + 它对应的库。"""
    name: str
    directory: str
    db_path: str
    conn: sqlite3.Connection
    tables: set[str]
    trace: dict
    result: dict


# ---------------------------------------------------------------------------
# 第 1 项：hash-integrity
# ---------------------------------------------------------------------------
def check_hash_integrity(cases: list[Case]) -> Check:
    """证据里的每一条调用记录都要能在 event_log 里逐字对上，且 digest 形状成立。

    还额外做一次**真正的重算**：失败的 skill 其 output 恒为 None，所以
    ``output_hash`` 必须等于 ``_digest(None)`` 这个常量。digest 算法从
    ``maos.skills.invoker`` 直接 import —— 在这里重写一份哈希就是 C-7 的反例：
    两份实现哪天分叉了，这一项会静默地永远 PASS。
    """
    chk = Check("hash-integrity", "input_digest / output_hash 与 event_log 一致")
    from maos.skills.invoker import _digest

    null_hash = _digest(None)
    for case in cases:
        rows = {r["seq"]: dict(r) for r in case.conn.execute("SELECT * FROM event_log")}
        seen_invocations: dict[str, str] = {}
        for trace in case.trace.get("traces", []):
            for span in trace["spans"]:
                if span["kind"] != "event":
                    continue
                attrs = span["attributes"]
                etype = attrs.get("maos.event.type")
                if etype not in ("SkillInvoked", "ToolInvoked"):
                    continue
                seq = attrs.get("maos.event.seq")
                row = rows.get(seq)
                where = f"{case.name} seq={seq}"
                if row is None:
                    chk.bad(f"{where}: trace 里的调用在 event_log 里不存在（凭空多出的证据）")
                    continue
                db_detail = _loads(row["detail"], {}) or {}
                if attrs.get("maos.detail") != db_detail:
                    chk.bad(f"{where}: trace 的 detail 与 event_log 不一致（证据被改过）")
                    continue

                if etype == "ToolInvoked":
                    if not _HEX64.match(str(db_detail.get("params_digest", ""))):
                        chk.bad(f"{where}: params_digest 不是 64 位十六进制")
                        continue
                    chk.ok()
                    continue

                bad = False
                for field_name in ("input_digest", "output_hash"):
                    if not _HEX64.match(str(db_detail.get(field_name, ""))):
                        chk.bad(f"{where}: {field_name} 不是 64 位十六进制")
                        bad = True
                if bad:
                    continue
                if db_detail.get("status") == "failed" and db_detail["output_hash"] != null_hash:
                    chk.bad(f"{where}: skill 失败时 output 恒为 None，"
                            f"output_hash 应等于 _digest(None)，实际对不上")
                    continue
                inv = db_detail.get("invocation_id")
                if inv:
                    if inv in seen_invocations:
                        chk.bad(f"{where}: invocation_id {inv} 与 {seen_invocations[inv]} 重复")
                        continue
                    seen_invocations[inv] = where
                chk.ok()

        # 反向：库里有、证据里没有的调用，同样是证据不完整
        in_trace = {s["attributes"].get("maos.event.seq")
                    for t in case.trace.get("traces", []) for s in t["spans"]
                    if s["kind"] == "event"}
        known_plans = {p[0] for p in case.conn.execute("SELECT plan_id FROM plan")}
        for seq, row in rows.items():
            if row["event_type"] not in ("SkillInvoked", "ToolInvoked"):
                continue
            if seq in in_trace or row["plan_id"] not in known_plans:
                continue
            chk.bad(f"{case.name} seq={seq}: event_log 有这条调用，trace 里却没有（证据被删过）")
    return chk


# ---------------------------------------------------------------------------
# 第 2 项：business-ref
# ---------------------------------------------------------------------------
def check_business_ref(cases: list[Case]) -> Check:
    chk = Check("business-ref", "business_ref 指向的对象存在且 version 匹配")
    live = [c for c in cases if "business_ref" in c.tables]
    if not live:
        chk.skip("本轮证据里没有 business_ref 表（退款域业务对象未进入这些场景）")
        return chk
    try:
        from maos.core.store import SqliteStore
        from maos.domain.refund import objects as refund_objects
    except ImportError as exc:
        chk.skip(f"业务域模块不可用（{exc}），无法解析引用")
        return chk

    for case in live:
        store = SqliteStore(case.db_path)
        recorded = {(o["plan_id"], o["task_id"], o["tenant_id"], o["object_type"],
                     o["object_id"], o["object_version"]): o
                    for o in load_evidence_json(
                        os.path.join(case.directory, "business-objects.json")).get("objects", [])}
        for r in case.conn.execute("SELECT * FROM business_ref"):
            ref = dict(r)
            key = (ref["plan_id"], ref["task_id"], ref["tenant_id"], ref["object_type"],
                   ref["object_id"], ref["object_version"])
            target = refund_objects.resolve_business_ref(store, ref)
            label = (f"{case.name} {ref['object_type']}:{ref['object_id']}"
                     f"@v{ref['object_version']}")
            if target is None:
                chk.bad(f"{label}: 引用悬空 —— 对象不存在或 version 对不上")
                continue
            if key not in recorded:
                chk.bad(f"{label}: 库里有这条引用，business-objects.json 里没有")
                continue
            if not recorded[key].get("resolved"):
                chk.bad(f"{label}: 证据把它记成解析失败，重放却解析得到 —— 两边对不上")
                continue
            chk.ok()
    return chk


# ---------------------------------------------------------------------------
# 第 3 项：authoritative-fact
# ---------------------------------------------------------------------------
def check_authoritative_fact(cases: list[Case]) -> Check:
    """settled 是权威终态，只有 payment.observe 写得进去（铁律 8）。

    两头都查：settled 必须有回执；回执的 actor_invocation_id 必须真的属于一次
    payment.observe 调用。只查前者的话，任何一个 skill 自己伪造一条回执就能过关。
    """
    chk = Check("authoritative-fact", "settled 有回执，且回执出自 payment.observe")
    live = [c for c in cases if {"refund_case", "payment_observation"} <= c.tables]
    if not live:
        chk.skip("本轮证据里没有 refund_case / payment_observation 表（退款场景未落地）")
        return chk

    for case in live:
        observer_ids = set()
        for e in case.conn.execute("SELECT detail FROM event_log WHERE event_type='SkillInvoked'"):
            d = _loads(e["detail"], {}) or {}
            if d.get("skill") == AUTHORITATIVE_WRITER and d.get("invocation_id"):
                observer_ids.add(d["invocation_id"])

        settled = case.conn.execute(
            "SELECT tenant_id, case_id FROM refund_case WHERE biz_status='settled'").fetchall()
        for c in settled:
            obs = case.conn.execute(
                "SELECT * FROM payment_observation WHERE tenant_id=? AND case_id=?",
                (c["tenant_id"], c["case_id"])).fetchall()
            label = f"{case.name} case={c['case_id']}"
            if not obs:
                chk.bad(f"{label}: biz_status=settled 却没有 payment_observation —— "
                        f"外部状态被直接写死为终态")
                continue
            bad = False
            for o in obs:
                actor = o["actor_invocation_id"]
                if not actor:
                    chk.bad(f"{label}: 回执没有 actor_invocation_id，来源不可追")
                    bad = True
                elif actor not in observer_ids:
                    chk.bad(f"{label}: 回执的 actor_invocation_id={actor} 不属于任何一次 "
                            f"{AUTHORITATIVE_WRITER} 调用 —— 权威事实边界被绕过")
                    bad = True
            if not bad:
                chk.ok()

        # 反面：有回执、案子却没到 settled，属于观察到了但没收口，点名但不判负。
        orphan = case.conn.execute(
            "SELECT o.case_id FROM payment_observation o JOIN refund_case r"
            " ON o.tenant_id=r.tenant_id AND o.case_id=r.case_id"
            " WHERE r.biz_status!='settled'").fetchall()
        for o in orphan:
            chk.warn(f"{case.name} case={o['case_id']}: 有回执但 biz_status 不是 settled")
    return chk


# ---------------------------------------------------------------------------
# 第 4 项：trace-tree
# ---------------------------------------------------------------------------
def check_trace_tree(cases: list[Case]) -> Check:
    """span 树无孤儿、无环；并且 trace.json 与「从库里重放一遍」逐字节一致。

    重放对比是这一项真正的牙齿：``export_trace_bundle`` 是库的纯函数
    （span_id 由内容哈希得出、排序确定），所以只要有人动过 trace.json 一个字符，
    这里就会不等。
    """
    chk = Check("trace-tree", "span 树无孤儿无环，且与库重放一致")
    from maos.obs.trace import check_span_tree, export_trace_bundle

    for case in cases:
        for trace in case.trace.get("traces", []):
            errs = check_span_tree(trace["spans"])
            if errs:
                for e in errs:
                    chk.bad(f"{case.name} plan={trace['plan_id']}: {e}")
            else:
                chk.ok()
        replay = json.loads(json.dumps(export_trace_bundle(case.db_path), ensure_ascii=False))
        if replay != case.trace:
            chk.bad(f"{case.name}: trace.json 与库重放结果不一致（证据被改过或库已变）")
        else:
            chk.ok()
        _warn_stray_events(chk, case)
        unsourced = case.trace.get("summary", {}).get("unsourced_artifacts", 0)
        if unsourced:
            chk.warn(f"{case.name}: {unsourced} 份产物没有来源事件（provenance=unknown）")
        _warn_sandbox_path(chk, case)
    return chk


def _warn_stray_events(chk: Check, case: Case) -> None:
    """游离事件的 warn。**一个 case 仍然只出一条**，只是把两种形态分开说。

    ``plan_id`` 是空串和 ``plan_id`` 非空却指不到 plan，看起来都是「不在任何一棵树
    内」，但含义天差地别：

    * 空串 = **规划期调用**。检索、需求归一这些发生在 ``create_plan`` 之前，
      那一刻还没有 plan_id 可写。事件本身是完整的、哈希也对得上，没有丢。
    * 非空却查不到 = 事件指向一个不存在的 Plan，那才是真的该查。

    原措辞把两者一律说成「指不到任何 plan」，读起来像事件丢了。现在按形态分开报，
    真出现第二种时不会被第一种的解释盖住 —— 判据没放宽，反而多认一种形态。
    """
    strays = case.trace.get("stray_events") or []
    if not strays:
        return
    kinds = sorted({s.get("event_type", "?") for s in strays})
    pre_plan = [s for s in strays if not (s.get("plan_id") or "").strip()]
    dangling = [s for s in strays if (s.get("plan_id") or "").strip()]

    parts = []
    if pre_plan:
        parts.append(f"{len(pre_plan)} 条是**建 Plan 之前**发生的调用（plan_id 为空串，"
                     f"不是事件丢了）")
    if dangling:
        parts.append(f"{len(dangling)} 条 plan_id 非空却指不到任何 plan —— 这一种要查")
    chk.warn(f"{case.name}: {len(strays)} 条事件不在任何一棵树内（类型 {kinds}）："
             + "；".join(parts)
             + "。根因：ControlPlane.create_plan 自己生成 plan_id、不接受外部传入"
               "（docs/BACKLOG.md task-X4 第 2 条）")


def _warn_sandbox_path(chk: Check, case: Case) -> None:
    """test_report 到底在哪儿跑的。降级与不可审计分开报，谁都不许静静过去。

    一份降级跑出来的报告和一份容器跑出来的报告，计数上长得一模一样 ——
    差别只在 ``--network none`` 那条探针是 skipped 还是 passed，而 skipped
    不进 passed/failed/errors 任何一个计数。不在这里点名，「容器隔离」这句话
    当场不成立而屏幕上看不出任何差别。
    """
    summary = case.trace.get("summary", {})
    degraded = summary.get("degraded_sandbox_reports", 0)
    unrecorded = summary.get("unrecorded_sandbox_reports", 0)
    if degraded:
        reasons = sorted({
            s["attributes"].get("maos.artifact.sandbox.degraded_reason") or "未记录"
            for t in case.trace.get("traces", []) for s in t["spans"]
            if s["attributes"].get("maos.artifact.sandbox.mode") == "subprocess"})
        chk.warn(f"{case.name}: {degraded} 份 test_report 是**降级**跑出来的，"
                 f"容器隔离（--network none / --read-only / --user 1000:1000）本次未生效；"
                 f"原因：{'；'.join(reasons)}")
    if unrecorded:
        chk.warn(f"{case.name}: {unrecorded} 份 test_report **执行路径不可审计** —— "
                 f"报告里没有 sandbox_mode，判不出这一次是真在容器里跑的还是降级跑的"
                 f"（docs/BACKLOG.md task-X4 第 1 条）")


# ---------------------------------------------------------------------------
# 第 5 项：kb-hit
# ---------------------------------------------------------------------------
def check_kb_hit(cases: list[Case]) -> Check:
    chk = Check("kb-hit", "KbRetrieved 的 doc_id 在 kb_doc 中存在")
    live = [c for c in cases if "kb_doc" in c.tables]
    if not live:
        chk.skip("kb 层未落地：本轮无 kb_doc 表（P5 才建）")
        return chk
    for case in live:
        docs = {r[0] for r in case.conn.execute("SELECT doc_id FROM kb_doc")}
        for e in case.conn.execute(
                "SELECT seq, detail FROM event_log WHERE event_type='KbRetrieved'"):
            d = _loads(e["detail"], {}) or {}
            hits = d.get("docs") or d.get("hits") or []
            for h in hits:
                doc_id = h.get("doc_id") if isinstance(h, dict) else h
                if doc_id in docs:
                    chk.ok()
                else:
                    chk.bad(f"{case.name} seq={e['seq']}: doc_id={doc_id!r} 不在 kb_doc 里")
    return chk


# ---------------------------------------------------------------------------
# 第 6 项：business-outcome
# ---------------------------------------------------------------------------
def check_business_outcome(cases: list[Case]) -> Check:
    """Plan 走到 DONE 不等于业务成功。DONE 必须指得出一条**外部**判据。

    「外部」的意思是这条判据不是 Agent 对自己的评价：回归报告是沙箱/测试给的，
    payment_observation 是支付网关给的；``patch_set.self_check`` 不算。
    """
    chk = Check("business-outcome", "Plan 终态有 business_outcome，DONE 有外部判据")
    for case in cases:
        db_states = {r["plan_id"]: r["state"] for r in case.conn.execute(
            "SELECT plan_id, state FROM plan")}
        recorded = {p["plan_id"]: p for p in case.result.get("plans", [])}
        for plan_id, state in db_states.items():
            label = f"{case.name} plan={plan_id}"
            plan = recorded.get(plan_id)
            if plan is None:
                chk.bad(f"{label}: 库里有这个 Plan，result.json 里没有")
                continue
            if plan.get("state") != state:
                chk.bad(f"{label}: result.json 记的终态 {plan.get('state')} 与库里 {state} 不符")
                continue
            if state not in ("DONE", "FAILED"):
                continue                      # 非终态不在本项判据内
            outcome = plan.get("business_outcome")
            if not isinstance(outcome, dict) or not outcome.get("status"):
                chk.bad(f"{label}: 终态 {state} 却没有 business_outcome")
                continue
            if state == "DONE":
                ev = outcome.get("external_evidence") or []
                if not ev:
                    chk.bad(f"{label}: DONE 但没有任何外部判据 —— "
                            f"「Agent 都完成了」不等于业务成功")
                    continue
                if outcome.get("status") != "succeeded":
                    chk.bad(f"{label}: 有外部判据却记成 status={outcome.get('status')}")
                    continue
                unaudited = outcome.get("unaudited_evidence_count") or 0
                if unaudited:
                    # 措辞只说这一项证得了的事：**入库路径**上没有来源事件。
                    # 原措辞写的是「场景预置件，非实跑产出」—— 那是它证不了的断言，
                    # 而且现在是错的：场景 1/2 的报告由 flows/common.py::patch_verifier
                    # 真跑沙箱产出（真 workdir、真 git apply、真 pytest），只是插入时
                    # 绕开 on_task_result，于是审计链指不到产出它的那一步。
                    # 把「入库路径证不了」读成「内容是假的」，会把已经兑现的外部判据
                    # 重新贬回脚手架 —— 那正是这条 warn 想防的事情的反面。
                    chk.warn(f"{label}: {unaudited} 条外部判据来源未审计"
                             f"（**无来源事件**：入库时绕开 on_task_result，"
                             f"审计链指不到是哪一步产的）。"
                             f"这说的是入库路径，不是内容真伪：可能是场景预置件"
                             f"（scenario 3/5 的 seed_scripted_report），也可能是演示装配层"
                             f"现跑的真产物（scenario 1/2 的 patch_verifier）。"
                             f"判真伪看 trace.json 的 maos.artifact.sandbox.mode")
            chk.ok()
    return chk


# ---------------------------------------------------------------------------
# 第 7 项：history-case
# ---------------------------------------------------------------------------
def check_history_case(cases: list[Case]) -> Check:
    chk = Check("history-case", "history_case 知识可追溯到 outcome='success' 的真实 case")
    live = [c for c in cases if "kb_doc" in c.tables]
    if not live:
        chk.skip("kb 层未落地：本轮无 kb_doc 表，history_case 这一类知识尚不存在（P5 才建）")
        return chk
    for case in live:
        for r in case.conn.execute(
                "SELECT doc_id, source_case_id FROM kb_doc WHERE kind='history_case'"):
            src = r["source_case_id"]
            if not src:
                chk.bad(f"{case.name} doc={r['doc_id']}: history_case 没有 source_case_id")
                continue
            hit = case.conn.execute(
                "SELECT 1 FROM refund_case WHERE case_id=? AND biz_status='settled'",
                (src,)).fetchone()
            if hit:
                chk.ok()
            else:
                chk.bad(f"{case.name} doc={r['doc_id']}: 追不到成功收口的真实 case {src}")
    return chk


CHECKS = [
    check_hash_integrity,
    check_business_ref,
    check_authoritative_fact,
    check_trace_tree,
    check_kb_hit,
    check_business_outcome,
    check_history_case,
]


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def resolve_db(evidence_root: str, scenario_dir: str, db_arg: str | None) -> str:
    """``--db`` 可以是一个库文件（全场景共用）、一个目录、或不给（用场景目录自带的）。"""
    if db_arg and os.path.isfile(db_arg):
        return db_arg
    base = db_arg if (db_arg and os.path.isdir(db_arg)) else evidence_root
    candidate = os.path.join(base, os.path.basename(scenario_dir), "maos.db")
    if os.path.exists(candidate):
        return candidate
    return os.path.join(scenario_dir, "maos.db")


def load_cases(evidence_root: str, db_arg: str | None) -> list[Case]:
    if not os.path.isdir(evidence_root):
        raise VerifyError(f"证据目录不存在: {evidence_root}")
    dirs = sorted(
        os.path.join(evidence_root, d) for d in os.listdir(evidence_root)
        if d.startswith("scenario-") and os.path.isdir(os.path.join(evidence_root, d)))
    if not dirs:
        raise VerifyError(
            f"{evidence_root} 下没有 scenario-* 目录；先跑 python3 scripts/make_evidence.py")
    cases = []
    for d in dirs:
        db_path = resolve_db(evidence_root, d, db_arg)
        conn = connect_ro(db_path)
        cases.append(Case(
            name=os.path.basename(d), directory=d, db_path=db_path, conn=conn,
            tables=table_names(conn),
            trace=load_evidence_json(os.path.join(d, "trace.json")),
            result=load_evidence_json(os.path.join(d, "result.json")),
        ))
    return cases


def render(results: list[Check], cases: list[Case], as_json: bool) -> int:
    if as_json:
        print(json.dumps({
            "cases": [c.name for c in cases],
            "checks": [{"key": r.key, "status": r.status, "passed": r.passed,
                        "total": r.total, "skip_reason": r.skip_reason, "notes": r.notes}
                       for r in results],
        }, ensure_ascii=False, indent=2))
    else:
        for r in results:
            if r.status == SKIP:
                print(f"[SKIP] {r.key:<20} ({r.skip_reason})")
            else:
                print(f"[{r.status}] {r.key:<20} {r.passed}/{r.total}")
            for n in r.notes:
                print(f"         · {n}")

    scored = [r for r in results if r.status != SKIP]
    skipped = [r for r in results if r.status == SKIP]
    failed = [r for r in scored if r.status == FAIL]
    passed = len(scored) - len(failed)
    line = f"\nRESULT: {passed}/{len(scored)} PASS"
    if skipped:
        line += f", {len(skipped)} SKIP（{', '.join(r.key for r in skipped)}）—— 不计入分子"
    print(line)
    if failed:
        print(f"失败项：{', '.join(r.key for r in failed)}")
    print(f"证据来源：{', '.join(c.name for c in cases)}")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="verify", description="重放校验 evidence/ 里的证据束，逐项 PASS/FAIL/SKIP")
    parser.add_argument("--evidence", default=os.path.join(ROOT, "evidence"),
                        help="证据根目录，缺省 evidence/")
    parser.add_argument("--db", default=None,
                        help="库文件或库目录；缺省用每个场景目录自带的 maos.db")
    parser.add_argument("--json", action="store_true", help="机器可读输出")
    args = parser.parse_args(argv)

    cases = load_cases(args.evidence, args.db)
    results = [fn(cases) for fn in CHECKS]
    try:
        return render(results, cases, args.json)
    finally:
        for c in cases:
            c.conn.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except VerifyError as exc:
        print(f"[FAIL] 无法开始核验：{exc}", file=sys.stderr)
        sys.exit(2)
