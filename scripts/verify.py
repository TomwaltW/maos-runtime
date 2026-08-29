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
    6 business-outcome    每个 Plan 终态都有 business_outcome，DONE 必须有外部判据，
                          且每条判据都在库里回查得到
                                                  -> 失败 =「Agent 都完成了」被当成业务成功
    7 history-case        本库晋升的 history_case 都能追溯到 outcome='success'
                          的真实 case（外部导入的知识不在判据内，但不许全空）
                                                  -> 失败 = 知识层被污染

**SKIP 的纪律**：上游能力没落地的项输出 ``[SKIP]`` 并在结尾显式列名，
**不计进 PASS 的分子**。静默跳过等于谎报 —— 一个 7/7 里藏着两个没跑的，
比老老实实写 5/5 PASS + 2 SKIP 更坏。

**空转也算没跑**：分母为 0 的项一律不判 PASS（``_idle_skip``）。``0/0 PASS``
与「真跑了且全过」在屏幕上长得一模一样，是这个核验器能犯的最坏的错 —— 只跑了
``make_evidence.py`` 而没产 ``scenario-R5`` 的人，会拿到一屏满分，而 RAG 的
两项守卫一次都没执行。SKIP 至少看得出没跑，且不进分子。

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

#: 权威终态要求回执里的 `observed_state` 取什么值。与
#: maos/domain/refund/guard.py::AUTHORITATIVE_RECEIPT_STATE **同源**，
#: 改一边就要改另一边（那边的注释里写着取值域的出处：回执的 `status` 字段，
#: 四态 processing/unknown/settled/failed，不是 `outcome` 的 success/failed/unknown）。
#: 照抄而不 import 的理由与 AUTHORITATIVE_WRITER 同：核验器要在退款域缺席时照跑。
AUTHORITATIVE_RECEIPT_STATE = {"settled": frozenset({"settled"})}

#: 外部判据的取值域。与 ``make_evidence.py::derive_business_outcome`` 里两处
#: ``evidence.append`` 的 ``kind`` 同源 —— 生成侧装得进什么，核验侧才认什么。
#: Agent 对自己的评价（``patch_set`` 里的 ``self_check``）不在其中，README §3 写死了这条。
#: 照抄而不 import 的理由与 AUTHORITATIVE_WRITER 同：核验器要能独立于生成脚本跑。
EXTERNAL_EVIDENCE_KINDS = frozenset({"test_report", "payment_observation"})

#: 终态 -> 生成侧唯一写得出的 ``(status, basis)``，出处 ``make_evidence.py::derive_business_outcome``
#: 那三支 if：``FAILED`` 恒配 ``plan_failed``，有判据的 ``DONE`` 恒配 ``external_evidence``。
#: ``DONE`` 还有第三种取值 ``("undetermined", "no_external_evidence")``（判据为空时），
#: 但它在第 6 项里先被「DONE 但没有任何外部判据」判负，走不到自述比对那一步。
#: 非终态的 ``("in_progress", "plan_not_terminal")`` 同理不在本项判据内。
#: 照抄而不 import 的理由与 AUTHORITATIVE_WRITER 同：核验器要能独立于生成脚本跑。
TERMINAL_OUTCOME = {
    "DONE": ("succeeded", "external_evidence"),
    "FAILED": ("failed", "plan_failed"),
}

#: ``business_outcome.source`` 生成侧写死的唯一取值，出处同 TERMINAL_OUTCOME。
#: 它是这份结论的**出身**声明：改掉它等于声称这些数字不是从库里推出来的。
OUTCOME_SOURCE = "derived-from-db-at-export-time"

#: 出处注释里 sha 的合法后缀 —— `make_evidence.py` 在工作区脏时写 `<sha>-dirty`。
#: `scenario-R5` 恒带这个后缀：它由 `build_r5()` 在场景 1-7 已经把 evidence/ 改脏之后
#: 才自算 sha（其余场景共用主流程开头那一次干净的取值）。这是 submission-checklist.md
#: §A-2 认下的当前口径，不是篡改，所以比对前一律先剥掉它。
_SHA_DIRTY_SUFFIX = "-dirty"


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
#: 出处注释的形状：`# generated at <ISO8601> from <sha>`（make_evidence.py::header_line）。
_HEADER_RE = re.compile(r"^# generated at (?P<at>\S+) from (?P<sha>\S+)\s*$")


def header_sha(path: str, first_line: str) -> str:
    """从出处注释里取出 sha，剥掉 `-dirty` 后缀。首行不成形即判不合规。"""
    match = _HEADER_RE.match(first_line.rstrip("\n"))
    if not match:
        raise VerifyError(f"{path} 首行不是出处注释（铁律 3），证据格式不合规")
    sha = match.group("sha")
    return sha[: -len(_SHA_DIRTY_SUFFIX)] if sha.endswith(_SHA_DIRTY_SUFFIX) else sha


def evidence_sha(evidence_root: str) -> str | None:
    """整束证据自报的出处 sha —— `INDEX.json` 的 `git_sha`，且它自己的首行得与之对上。

    这是全束唯一一处「代码是哪个 commit」的声明，各文件的首行都拿它当锚。
    没有 INDEX.json 就返回 None（只产了单场景的老束），那时退回到只校验首行成形 ——
    宁可少查一层，也不许对着不存在的锚点把一整束正常证据判负。
    """
    path = os.path.join(evidence_root, "INDEX.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        declared = header_sha(path, fh.readline())
        try:
            index = json.loads(fh.read())
        except ValueError as exc:
            raise VerifyError(f"{path} 不是合法 JSON: {exc}") from exc
    recorded = str(index.get("git_sha") or "")
    if not recorded:
        raise VerifyError(f"{path} 没有 git_sha，整束证据没有出处锚点（铁律 3）")
    recorded = (recorded[: -len(_SHA_DIRTY_SUFFIX)]
                if recorded.endswith(_SHA_DIRTY_SUFFIX) else recorded)
    if declared != recorded:
        raise VerifyError(
            f"{path} 首行出处 sha={declared} 与它自己记的 git_sha={recorded} 不一致 ——"
            f" 索引的出处都自相矛盾，这一束证据说不清是哪份代码产的")
    return recorded


def load_evidence_json(path: str, *, expect_sha: str | None = None):
    """读 make_evidence.py 写的 json：校验首行出处注释，再读正文。

    `expect_sha` 非空时，首行的 sha 必须与它一致（`-dirty` 后缀先剥掉，见
    `_SHA_DIRTY_SUFFIX`）。只查「首行以 `# generated at ` 开头」是挡不住事的：
    把整行换成 `# generated at 2020-01-01T00:00:00+00:00 from deadbeef` 一样合格式，
    而这份文件自称出自一份根本不是本次核验对象的代码。**失败意味着**：这个文件的
    出处是编的 —— 它证明不了任何事，与它同束的其余文件也跟着不可信。
    """
    if not os.path.exists(path):
        raise VerifyError(f"缺文件: {path}")
    with open(path, encoding="utf-8") as fh:
        sha = header_sha(path, fh.readline())
        if expect_sha and sha != expect_sha:
            raise VerifyError(
                f"{path} 首行自称出自 {sha}，与 INDEX.json 记的 {expect_sha} 不是同一份代码"
                f" —— 出处对不上的证据不予采信（铁律 3）")
        try:
            return json.loads(fh.read())
        except ValueError as exc:
            raise VerifyError(f"{path} 不是合法 JSON: {exc}") from exc


#: 缺库时该往哪走 —— **产它的命令由目录名决定**。`scenario-R5` 不由
#: `make_evidence.py` 的场景循环产（它按 `maos.main.ALL_SCENARIOS` 跑 1-7），
#: 而由 `maos.kb.experiment` 单独产。对着 R5 印「先跑 make_evidence.py」，
#: 照做的人会拿到一模一样的报错再撞一次 —— 提示指向一条解决不了它的命令，
#: 比没有提示更坏。BACKLOG ## task-W5 第 2 条 / ## task-X3 第 4 条。
_DB_HINT_DEFAULT = "python3 scripts/make_evidence.py"
_DB_HINTS = {"scenario-R5": "python3 -m maos.kb.experiment"}


def missing_db_hint(db_path: str) -> str:
    """缺 ``db_path`` 这个库时，该跑哪条命令把它产出来。"""
    return _DB_HINTS.get(os.path.basename(os.path.dirname(db_path)), _DB_HINT_DEFAULT)


def connect_ro(db_path: str) -> sqlite3.Connection:
    if not os.path.exists(db_path):
        raise VerifyError(f"缺数据库: {db_path}（先跑 {missing_db_hint(db_path)}）")
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
    #: 这一束在 INDEX.json 里自报的 git sha；本目录每个 json 的首行都得与它对上。
    #: None = 没有 INDEX.json，那时只校验首行成形（见 evidence_sha）。
    expect_sha: str | None = None


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
                        os.path.join(case.directory, "business-objects.json"),
                        expect_sha=case.expect_sha).get("objects", [])}
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

    三头都查：settled 必须有回执；回执的 actor_invocation_id 必须真的属于一次
    payment.observe 调用；且回执里至少有一条**说的是到账了**。
    只查第一条，任何一个 skill 自己伪造一条回执就能过关；只查前两条，一条网关明确
    失败的真回执就能给 settled 背书 —— 那时系统持有的是「有一张回执」，
    而不是「网关说到账了」，两者差着这一项的全部意义。

    为什么这一项非有牙不可：`refund_case` / `payment_observation` 两张表**不参与**
    第 4 项的 trace 重放（那一项比对的是 span 树与事件链），所以对这两张表的直接
    篡改，全核验器只有这一项拦得住。
    """
    chk = Check("authoritative-fact", "settled 有回执，回执出自 payment.observe 且说到账了")
    allowed = AUTHORITATIVE_RECEIPT_STATE["settled"]
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
            # 回执得**说的是这件事**。上面两问查的是「有没有回执」「回执是谁递的」，
            # 都问不到回执的内容 —— 一条网关明确失败的观察（observed_state='failed'）
            # 由真的 payment.observe 落库，两问全过，却给 settled 背了书。
            seen = sorted({str(o["observed_state"]) for o in obs})
            if not (set(seen) & allowed):
                chk.bad(f"{label}: 有回执，但没有一条说到账了（observed_state={seen}，"
                        f"要的是 {sorted(allowed)}）—— 「有一张回执」被当成了"
                        f"「网关说到账了」，settled 背后没有外部权威支撑")
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
def _idle_skip(chk: Check, cases: list[Case], what: str) -> None:
    """分母为 0 —— 本项一次都没执行过。判 SKIP，并说清缺的是哪一份证据。

    印成 ``0/0 PASS`` 是这个核验器能犯的最坏的错：它跟「真跑了且全过」在屏幕上
    长得**一模一样**，而守卫其实空转。SKIP 不进分子（见文件头「SKIP 的纪律」），
    至少看得出没跑。RAG 两项的素材全在 ``scenario-R5`` 那一束里，所以缺它时
    直接把补跑的命令印出来 —— 「没核到」要在屏幕上看得见，还要说得出往哪走。
    """
    tail = ""
    if "scenario-R5" not in {c.name for c in cases}:
        tail = ("；本轮没有 scenario-R5，而 RAG 的证据全在那一束里 —— "
                "跑 python3 scripts/make_evidence.py 一并产出，"
                "或 python3 -m maos.kb.experiment 单独补")
    chk.skip(f"空转：{what}，本项判据一次都没执行{tail}")


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
    if chk.total == 0:
        _idle_skip(chk, cases, "证据束里没有一条 KbRetrieved 事件")
    return chk


# ---------------------------------------------------------------------------
# 第 6 项：business-outcome
# ---------------------------------------------------------------------------
def _test_report_backing(case: Case, plan_id: str, item: dict) -> str:
    """``test_report`` 判据回查 ``artifact`` 表。返回失败理由；空串 = 回查得到。

    判据二（``payment_observation``）指的不是产物，走另一个函数 —— 那一类**没有**
    ``artifact_id``，拿同一套字段名去查两种东西是 C-7 的反例。
    """
    artifact_id = item.get("artifact_id")
    if not artifact_id:
        return "一条 test_report 判据没有 artifact_id，无从回查"
    row = case.conn.execute(
        "SELECT plan_id, task_id, kind, version, content FROM artifact"
        " WHERE artifact_id=?", (artifact_id,)).fetchone()
    if row is None:
        return f"artifact_id={artifact_id!r} 在库里查无此物"
    if row["plan_id"] != plan_id:
        return (f"artifact_id={artifact_id!r} 属于 plan={row['plan_id']}，"
                f"不能给本 plan 背书")
    if row["kind"] != "test_report":
        return (f"artifact_id={artifact_id!r} 在库里的 kind 是 {row['kind']!r}，"
                f"不是外部判据类")
    if (item.get("task_id"), item.get("version")) != (row["task_id"], row["version"]):
        return (f"artifact_id={artifact_id!r} 记的 task/version 与库里不符："
                f"记 {item.get('task_id')!r}/{item.get('version')!r}，"
                f"库里 {row['task_id']!r}/{row['version']!r}")
    content = _loads(row["content"], {}) or {}
    if content.get("failed") or content.get("errors") or content.get("tool_error"):
        return (f"artifact_id={artifact_id!r} 这份报告自己就没过"
                f"（failed={content.get('failed')!r} errors={content.get('errors')!r} "
                f"tool_error={content.get('tool_error')!r}），背不了书")
    if item.get("passed") != content.get("passed"):
        return (f"artifact_id={artifact_id!r} 记的 passed={item.get('passed')!r} "
                f"与库里 {content.get('passed')!r} 不符")
    return ""


def _observation_backing(case: Case, plan_id: str, item: dict) -> str:
    """``payment_observation`` 判据回查退款两张表。返回失败理由；空串 = 回查得到。

    生成侧（``derive_business_outcome`` 判据二）只给 ``biz_status='settled'`` 的 case
    记这一类判据，回执字段逐个抄自 ``payment_observation`` 行，所以这里照着倒推。
    """
    if not {"refund_case", "payment_observation"} <= case.tables:
        return ("记了 payment_observation 判据，本库却没有退款那两张表 —— "
                "生成侧根本推不出这一条")
    tenant_id, case_id = item.get("tenant_id"), item.get("case_id")
    request_id = item.get("request_id")
    if not (tenant_id and case_id and request_id):
        return f"一条 payment_observation 判据缺 tenant_id/case_id/request_id：{item!r}"
    row = case.conn.execute(
        "SELECT plan_id, biz_status FROM refund_case WHERE tenant_id=? AND case_id=?",
        (tenant_id, case_id)).fetchone()
    if row is None:
        return f"refund_case ({tenant_id}, {case_id}) 在库里查无此行"
    if row["plan_id"] != plan_id:
        return f"case={case_id} 属于 plan={row['plan_id']}，不能给本 plan 背书"
    if row["biz_status"] != "settled":
        return (f"case={case_id} 的 biz_status 是 {row['biz_status']!r} 而非 settled，"
                f"生成侧只给 settled 记这一类判据")
    hits = case.conn.execute(
        "SELECT gateway_code, observed_state, actor_invocation_id FROM payment_observation"
        " WHERE tenant_id=? AND case_id=? AND request_id=?",
        (tenant_id, case_id, request_id)).fetchall()
    if not hits:
        return f"request_id={request_id!r} 在 payment_observation 里查无此回执"
    if not any((h["gateway_code"], h["observed_state"], h["actor_invocation_id"])
               == (item.get("gateway_code"), item.get("observed_state"),
                   item.get("actor_invocation_id")) for h in hits):
        return (f"request_id={request_id!r} 记的回执与库里没有一行对得上："
                f"记 code={item.get('gateway_code')!r} "
                f"state={item.get('observed_state')!r} "
                f"actor={item.get('actor_invocation_id')!r}")
    return ""


def evidence_backing(case: Case, plan_id: str, item: object) -> str:
    """一条外部判据能不能在**库里**回查到。返回失败理由；空串 = 回查得到。

    为什么非得在这里回查：第 4 项 trace-tree 的牙齿是「trace.json 与库重放逐字节
    一致」，而它**不看 result.json**；第 6 项从前只数这个列表的长度。于是
    ``result.json`` 里的外部判据成了整束证据里唯一一处「写什么就是什么」的地方 ——
    偏偏它就是用来证明「这单业务真的成了」的那一处（G-2）。

    三问同时成立才算一条有效判据：指得到的东西在不在库里、属不属于**这个** plan、
    它的 kind 是不是外部判据类（Agent 自评不算，见 EXTERNAL_EVIDENCE_KINDS）。

    与 ``unaudited_evidence_count`` 那条 warn 是**两个维度**，不要混：那条说的是
    入库路径（绕开 ``on_task_result``，审计链指不到是哪一步产的），这里说的是内容
    对不对得上库。一条判据完全可以「来源未审计（warn）」而「回查得到（PASS）」——
    scenario 1/2/3/5 现在就是这样：真产物，只是入库时没走事件。
    """
    if not isinstance(item, dict):
        return f"外部判据不是一个对象：{item!r}"
    kind = item.get("kind")
    if kind not in EXTERNAL_EVIDENCE_KINDS:
        return (f"kind={kind!r} 不是外部判据类"
                f"（取值域 {sorted(EXTERNAL_EVIDENCE_KINDS)}，出处 make_evidence.py）")
    if kind == "test_report":
        return _test_report_backing(case, plan_id, item)
    return _observation_backing(case, plan_id, item)


def outcome_selfclaim(state: str, outcome: dict, unaudited: int) -> list[str]:
    """``business_outcome`` 那四个自述字段与证据的**就地**比对。返回全部失败理由。

    第 6 项从前只查 ``external_evidence`` 里**指得到的东西**（G-2 把产物和回执做进了
    回查）。可 ``plan_state`` / ``basis`` / ``source`` / ``unaudited_evidence_count``
    这四个字段谁也没查过 —— 它们描述的是「这份结论是怎么来的」。结论本身长了牙齿之后，
    描述结论的那层**元数据**就成了整束证据里最后一处「写什么就是什么」。

    危害不是伪造成功，是**伪造干净**：把 ``unaudited_evidence_count`` 抹成 0，
    第 6 项那条「来源未审计」的 warn 就凭空消失，而 verify 照印 ``7/7 PASS``。
    一屏没有 warn 的 7/7 比有 warn 的 7/7 更像「这套东西没问题」—— 而那条 warn
    恰恰是评委判断「这份报告是不是脚手架」的唯一线索（H-1 实测：warn 12 行掉到 11 行，
    七项读数一个不变）。所以调用侧那条 warn 改按**列表里数出来的**条数印，
    不按报告自述的数字印：判负归判负，warn 一行都不许被判据吃掉（G-2 的口径）。

    四条都不新查库：``state`` 调用侧已经拿到，其余三个在生成侧是死的推导，照着倒推。
    ``provenance`` 本身对不对得上事件链**不在这里判**（那要重算入库路径，是另一件事，
    见 BACKLOG ``## task-H1``）—— 这里只保证「自述的数」等于「列表里数得出来的数」。
    """
    expect_status, expect_basis = TERMINAL_OUTCOME[state]
    wrong: list[str] = []
    if outcome.get("plan_state") != state:
        wrong.append(f"plan_state 自述 {outcome.get('plan_state')!r}，库里是 {state!r}")
    if outcome.get("basis") != expect_basis:
        wrong.append(f"status={expect_status!r} 配的 basis 只能是 {expect_basis!r}"
                     f"（生成侧是死的推导，出处 make_evidence.py），"
                     f"报告写的是 {outcome.get('basis')!r}")
    if outcome.get("source") != OUTCOME_SOURCE:
        wrong.append(f"source 自述 {outcome.get('source')!r}，"
                     f"生成侧只写得出 {OUTCOME_SOURCE!r}")
    if outcome.get("unaudited_evidence_count") != unaudited:
        wrong.append(f"unaudited_evidence_count 自述 "
                     f"{outcome.get('unaudited_evidence_count')!r}，"
                     f"external_evidence 里 provenance='unknown' 的实有 {unaudited} 条"
                     f" —— 抹掉这个数就是抹掉那条 warn")
    return wrong


def check_business_outcome(cases: list[Case]) -> Check:
    """Plan 走到 DONE 不等于业务成功。DONE 必须指得出一条**外部**判据。

    「外部」的意思是这条判据不是 Agent 对自己的评价：回归报告是沙箱/测试给的，
    payment_observation 是支付网关给的；``patch_set.self_check`` 不算。

    FAILED 一侧同样有牙齿。从前这一支只要 ``business_outcome`` 是个非空 dict 就放行，
    于是「库里 FAILED、``result.json`` 也老实记 FAILED（躲开上面那条 state 比对）、
    ``business_outcome.status`` 却写 succeeded」这一手一声不吭（H-1 实测：7/7 PASS、
    exit=0、warn 一行不少）。判负要判在**自称**上，不是判在 state 上 —— 因为
    state 本来就是老实的，那正是这一手能躲过去的原因。
    """
    chk = Check("business-outcome",
                "Plan 终态有 business_outcome，DONE 的外部判据回查得到")
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
            # 来源未审计的条数按**列表里数出来的**印，不按报告自述的数字印：
            # 自述的数一旦被抹成 0，下面那条 warn 就凭空消失，而七项读数一个不变。
            # 自述与实数对不上是判负的事（outcome_selfclaim），但 warn 照印不误。
            ev = outcome.get("external_evidence") or []
            unaudited = sum(1 for e in ev
                            if isinstance(e, dict) and e.get("provenance") == "unknown")
            if state == "DONE":
                if not ev:
                    chk.bad(f"{label}: DONE 但没有任何外部判据 —— "
                            f"「Agent 都完成了」不等于业务成功")
                    continue
                if outcome.get("status") != "succeeded":
                    chk.bad(f"{label}: 有外部判据却记成 status={outcome.get('status')}")
                    continue
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
                # 列表非空还不够 —— 里面装的每一条都得在库里指得到东西。
                broken = [why for why in
                          (evidence_backing(case, plan_id, item) for item in ev) if why]
                if broken:
                    chk.bad(f"{label}: {len(broken)}/{len(ev)} 条外部判据回查不到 —— "
                            f"result.json 里写什么就算什么，那不叫判据："
                            + "；".join(broken))
                    continue
            elif outcome.get("status") != TERMINAL_OUTCOME[state][0]:
                # FAILED 一侧的牙齿：库里老实记了 FAILED，报告自称成功照样判负。
                chk.bad(f"{label}: 库里是 FAILED，报告却自称 "
                        f"status={outcome.get('status')!r} —— 失败的 Plan 没有"
                        f"「业务成功」这一说，生成侧那一支只写得出 "
                        f"{TERMINAL_OUTCOME[state][0]!r}")
                continue
            # 结论有了牙齿，描述结论的那四个字段还得对得上（见 outcome_selfclaim）。
            wrong = outcome_selfclaim(state, outcome, unaudited)
            if wrong:
                chk.bad(f"{label}: business_outcome 的自述字段与证据对不上 —— "
                        f"这一层从前没人查过：" + "；".join(wrong))
                continue
            chk.ok()
    return chk


# ---------------------------------------------------------------------------
# 第 7 项：history-case
# ---------------------------------------------------------------------------
def check_history_case(cases: list[Case]) -> Check:
    """**本库晋升的** history_case 必须回查得到一条 settled 的 refund_case。

    判据只覆盖本库晋升出来的那些，不覆盖外部导入的历史知识（BACKLOG ## task-X3
    第 3 条）：导入的知识按定义没有本库记录，而给它造一条就是伪造证据（铁律 3）。
    原判据要求**每一条** history_case 都回查得到，等于把「外部导入的知识」挡在
    任何证据库之外 —— 规则本身是对的（它守的是「RAG 命中不是编的」），只是太窄。

    区分标志是现成的：本库晋升的 ``source_case_id`` 在 ``refund_case`` 里有行，
    导入的没有。放宽到此为止 —— 「一条都回查不到」仍判负，见函数末尾：
    否则这一项会退化成「库里全是导入知识 -> 0/0 -> 过」，那正是空转。
    """
    chk = Check("history-case", "本库晋升的 history_case 可追溯到 outcome='success' 的真实 case")
    live = [c for c in cases if "kb_doc" in c.tables]
    if not live:
        chk.skip("kb 层未落地：本轮无 kb_doc 表，history_case 这一类知识尚不存在（P5 才建）")
        return chk
    seen = imported = 0
    for case in live:
        local = set()
        if "refund_case" in case.tables:
            local = {r[0] for r in case.conn.execute("SELECT case_id FROM refund_case")}
        for r in case.conn.execute(
                "SELECT doc_id, source_case_id FROM kb_doc WHERE kind='history_case'"):
            seen += 1
            src = r["source_case_id"]
            if not src:
                chk.bad(f"{case.name} doc={r['doc_id']}: history_case 没有 source_case_id")
                continue
            if src not in local:
                imported += 1        # 外部导入：本库没有它的 case 行，不在本项判据内
                continue
            hit = case.conn.execute(
                "SELECT 1 FROM refund_case WHERE case_id=? AND biz_status='settled'",
                (src,)).fetchone()
            if hit:
                chk.ok()
            else:
                chk.bad(f"{case.name} doc={r['doc_id']}: 追不到成功收口的真实 case {src}")
    if imported:
        chk.warn(f"{imported} 条 history_case 的 source_case_id 不在本库 refund_case 里，"
                 f"按**外部导入的历史知识**处理，不在本项判据内 —— "
                 f"给它补一条本库 case 才是伪造证据（铁律 3）")
    if chk.total == 0:
        if seen:
            # 有素材却一条都没进判据 = 放宽放过头的那个形态，判负。
            chk.bad(f"{seen} 条 history_case 全部回查不到本库 refund_case —— "
                    f"放宽是为了放行外部导入的知识，不是让本项退化成空转")
        else:
            _idle_skip(chk, cases, "证据束里没有一条 history_case 知识")
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
    expect_sha = evidence_sha(evidence_root)
    cases = []
    for d in dirs:
        db_path = resolve_db(evidence_root, d, db_arg)
        conn = connect_ro(db_path)
        cases.append(Case(
            name=os.path.basename(d), directory=d, db_path=db_path, conn=conn,
            tables=table_names(conn),
            trace=load_evidence_json(os.path.join(d, "trace.json"), expect_sha=expect_sha),
            result=load_evidence_json(os.path.join(d, "result.json"), expect_sha=expect_sha),
            expect_sha=expect_sha,
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
