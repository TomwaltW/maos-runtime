#!/usr/bin/env python3
"""证据束生成器 —— 一键跑全部场景，把每一场的真实产出落成 ``evidence/scenario-<N>/``。

    python3 scripts/make_evidence.py                    # 全部场景 + scenario-R5
    python3 scripts/make_evidence.py --scenarios 1,2    # 只跑指定场景（不含 R5）
    python3 scripts/make_evidence.py --contrast         # 只产 contrast-R3/R4/R6

``--contrast`` 是**另一条路**，不是第 9、10、11 束：缺省证据束恒为 8 束
（``scenario-1..7`` + ``scenario-R5``）是跨轨冻结口径，``scripts/demo_preflight.sh``
与复赛材料都写死了 8。所以三组对照 **①** 不进 ``maos.main.ALL_SCENARIOS``、
**②** 目录名不叫 ``scenario-*``（``verify.py`` 按这个前缀挑核验对象，对照束由
``scan_aux_bundles`` 登记在册即可）、**③** 带 ``--contrast`` 时**只**产对照束，
不碰任何 ``scenario-*`` 目录、也不重写 ``INDEX.json``（那份索引由缺省全量跑重建，
届时对照束会作为 aux 登记进去）。缺省行为因此一个字节不变。

``scenario-R5``（RAG 有无对照）不在 ``maos.main.ALL_SCENARIOS`` 里，由
``maos.kb.experiment`` 单独产，却是唯一带 ``kb_doc`` 的一束 —— ``verify.py``
第 5、7 两项全靠它。缺省把它一并产出，是为了让「复现全量证据」只剩两条命令：
本脚本 + ``verify.py``。见 ``build_r5``。

三条不许破的规矩（铁律 3 / 铁律 6）：

1. **每个文件首行** ``# generated at <ISO8601> from <git sha>``，由本脚本自动写入。
   工作区不干净时 sha 带 ``-dirty`` 后缀 —— 证据的出处含糊比没有证据更坏。
2. **上游命令失败即报错退出，绝不写占位假数据。** 每一场先在临时目录里攒齐，
   全部成功才 ``os.replace`` 挪到位；中途失败连临时目录一起删干净，
   宁可目录缺一半，也不许留下半份让人误以为跑通了的产物。
3. **脱敏走两道**：写入时把敏感环境变量的值替换成 ``***REDACTED:<VAR>***``（出口），
   写完再拿这些值当哨兵串把整个目录反查一遍（兜底）。反查命中即销毁目录并失败 ——
   出口脱敏管不到 ``__repr__`` 那个入口，只有反查才是真闸。

为什么要在子进程里跑：``maos/flows/common.py::build()`` 用的是 ``SqliteStore()``，
即 ``:memory:``，进程一退库就没了，``verify.py --db`` 无从读起。而 ``flows/**``
与 ``core/**`` 本轨禁改。所以本脚本把自己作为子进程再跑一次（``--_child``），
在**子进程内**把 ``maos.flows.common.SqliteStore`` 换成绑定了文件路径的同一个类 ——
仓库里一个字节不改，落库位置由证据生成器自己提供。已记 docs/DECISIONS.md。
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

HEADER_PREFIX = "# generated at "

#: 名字长这样的环境变量，其值一律当敏感值处理（出口替换 + 反查哨兵）。
_SECRET_NAME = re.compile(
    r"(?i)(api[_-]?key|access[_-]?key|secret|token|password|passwd|credential|private[_-]?key)")
#: 派单点名的两个，无论命名规则是否命中都必须纳入。
_ALWAYS_SECRET = ("MAOS_LLM_API_KEY", "MATRIX_TOKEN")
#: 太短的值当哨兵会把正常文本全打成命中（"1"、"on" 之类），反而掩盖真泄漏。
_MIN_SECRET_LEN = 6


class EvidenceError(RuntimeError):
    """生成过程中的任何硬失败。抛出即意味着这一场不会留下任何产物。"""


# ---------------------------------------------------------------------------
# 出处与脱敏
# ---------------------------------------------------------------------------
#: 钉住的出处 sha 存在**环境变量**里，而不是模块全局变量里。
#: 不得不这样：``python3 scripts/make_evidence.py`` 让本文件成为 ``__main__``，
#: 而 ``maos/kb/experiment.py`` 走的是 ``from scripts.make_evidence import git_sha`` ——
#: 那是**另一个模块实例**，两份各有各的全局变量，钉在模块里对 R5 一侧根本不可见。
#: 环境变量是进程级的，两个实例看到的是同一个值。
_PIN_ENV = "MAOS_EVIDENCE_PINNED_SHA"


def pin_sha() -> str:
    """在**动任何文件之前**取一次 sha 钉住，之后本进程内所有取值都返回它。返回钉住的值。

    非钉不可的理由：``evidence/`` 是**入库**的（``.gitignore`` 只排掉 ``*.db``），
    所以本脚本写完 ``scenario-1..7``，工作区自己就脏了。而 ``scenario-R5`` 由
    ``maos.kb.experiment.write_evidence`` 产，那边自算一次 sha —— 于是同一次生成里，
    前七束记干净 sha，R5 记 ``<sha>-dirty``：八份证据自称出自两个版本，
    而它们明明出自同一次运行。读者每一轮都要被解释一次这个后缀不算数。

    钉住而不是「先全算后全写」：``-dirty`` 要指示的是「跑这批证据的代码与 HEAD 不一致」，
    那是**开跑那一刻**的事实，不是落盘落到一半时的事实。落盘顺序不该改变出处。
    """
    pinned = os.environ.get(_PIN_ENV)
    if not pinned:
        pinned = git_sha()
        os.environ[_PIN_ENV] = pinned
    return pinned


def git_sha() -> str:
    """当前提交 sha；**被跟踪文件**有改动时带 ``-dirty``。取不到就抛，不给「unknown」兜底。

    脏判定刻意用 ``--untracked-files=no``：``evidence/`` 本身就是这个脚本正在生成的
    未跟踪产物，把它算进「脏」会让每一次生成都自称 dirty，于是这个标记恒为真、
    再也指示不了任何东西 —— 而它要指示的是「跑这批证据的代码与 HEAD 不一致」。

    ``pin_sha()`` 钉过之后一律返回钉住的值：同一次生成里落盘有先后，出处不该跟着变。
    """
    pinned = os.environ.get(_PIN_ENV)
    if pinned:
        return pinned
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
                             capture_output=True, text=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"],
                               cwd=ROOT, check=True,
                               capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise EvidenceError(f"取不到 git sha，证据没有出处，拒绝生成: {exc}") from exc
    if not sha:
        raise EvidenceError("git rev-parse HEAD 返回空")
    return f"{sha}-dirty" if dirty else sha


def header_line(sha: str) -> str:
    return f"{HEADER_PREFIX}{datetime.now(timezone.utc).isoformat()} from {sha}"


def secret_values(env: dict | None = None) -> dict[str, str]:
    """环境里所有该被当成敏感值的 ``{变量名: 值}``。"""
    env = os.environ if env is None else env
    out: dict[str, str] = {}
    for name, value in env.items():
        if not value or len(value) < _MIN_SECRET_LEN:
            continue
        if name in _ALWAYS_SECRET or _SECRET_NAME.search(name):
            out[name] = value
    return out


def redact(text: str, secrets: dict[str, str]) -> str:
    """出口脱敏。按值从长到短替换，避免短值先替换把长值切碎。"""
    for name, value in sorted(secrets.items(), key=lambda kv: -len(kv[1])):
        if value in text:
            text = text.replace(value, f"***REDACTED:{name}***")
    return text


#: 这些格式里，密钥可能以**像素**形式存在（截图），字节扫描从原理上就抓不到。
#: 位图把文字压成图像数据，``Bearer <token>`` 在文件里根本不以该字节序列存在 ——
#: 扫字节扫不到，换成扫文本一样扫不到，扫得再狠也扫不到。所以对这些格式
#: 唯一诚实的回答是「无法核验」，不是「没查到」。PDF 同理（文字常在压缩流里）。
#: ``.svg`` 刻意不在此列：它是文本，哨兵串扫得到。
_UNVERIFIABLE_EXT = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".ico", ".pdf"})


def is_unverifiable(filename: str) -> bool:
    """这个文件名的扩展名是否属于「字节扫描核验不了」那一类。"""
    return os.path.splitext(filename)[1].lower() in _UNVERIFIABLE_EXT


def scan_for_secrets(directory: str, secrets: dict[str, str]) -> list[str]:
    """拿哨兵串把目录逐字节反查一遍，返回命中描述（空 = 干净）。

    按字节查而不是按行读文本：证据里可能有二进制（sqlite 库文件就在同一目录），
    按文本读会因为解码失败而**跳过**那个文件，于是漏查得悄无声息。

    **字节扫描堵不住截图**（见 ``_UNVERIFIABLE_EXT``）：位图里的密钥是像素不是字节。
    所以遇到图像/版式格式一律记一条「无法核验」当命中处理 —— 由调用方销毁目录并失败。
    宁可拒收，也不要让一份「扫过了、干净」的报告盖住一个扫不到的洞：
    静默通过比不扫更坏，它给了假的安全感。

    只在**有哨兵串**时才这么判：``secrets`` 为空说明环境里根本没有要防的密钥，
    没有「核验」这回事，也就谈不上「无法核验」—— 否则没配密钥的机器天天报警，
    真出事那次反而没人看。
    """
    hits: list[str] = []
    needles = {name: value.encode("utf-8") for name, value in secrets.items()}
    for base, _dirs, files in os.walk(directory):
        for fn in files:
            path = os.path.join(base, fn)
            if needles and is_unverifiable(fn):
                hits.append(
                    f"{os.path.relpath(path, directory)}: 图像/版式格式，密钥若以像素形式"
                    f"存在于其中，任何字节扫描都查不到 —— 无法核验，拒收")
                continue
            try:
                with open(path, "rb") as fh:
                    blob = fh.read()
            except OSError as exc:
                hits.append(f"{path}: 读不出来，无法确认是否干净（{exc}）")
                continue
            for name, needle in needles.items():
                if needle in blob:
                    hits.append(f"{os.path.relpath(path, directory)}: 命中 {name} 的明文值")
    return hits


# ---------------------------------------------------------------------------
# 文件写入
# ---------------------------------------------------------------------------
def write_text(path: str, body: str, *, sha: str, secrets: dict[str, str]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(header_line(sha) + "\n")
        fh.write(redact(body, secrets))


def write_json(path: str, doc, *, sha: str, secrets: dict[str, str]) -> None:
    write_text(path, json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
               sha=sha, secrets=secrets)


def load_evidence_json(path: str):
    """读回本脚本写的 ``.json``：跳过首行出处注释，其余是严格 JSON。

    注释行是派单的硬要求（每个文件首行都要有出处），而 JSON 不允许注释，
    两者只能这样共存。命令行里想 ``jq`` 的话：``tail -n +2 <file> | jq .``。
    """
    with open(path, encoding="utf-8") as fh:
        first = fh.readline()
        if not first.startswith(HEADER_PREFIX):
            raise EvidenceError(f"{path} 首行不是出处注释，证据格式不合规: {first!r}")
        return json.loads(fh.read())


def evidence_header(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.readline().rstrip("\n")


# ---------------------------------------------------------------------------
# 子进程模式：把场景跑进文件库
# ---------------------------------------------------------------------------
def run_child(scenario: int, db_path: str) -> int:
    """在**本进程**里把场景跑进 ``db_path``。只由 ``--_child`` 入口调用。"""
    import maos.flows.common as common
    from maos.core.store import SqliteStore

    common.SqliteStore = functools.partial(SqliteStore, db_path)
    from maos.main import main as maos_main

    return maos_main(["--scenario", str(scenario)])


#: 子进程把对照的观测与判据先落成这份**无出处首行**的原始 JSON，父进程读走后删掉，
#: 再由 ``write_json`` 带着出处首行写进最终目录 —— 证据文件的首行规矩只有一个执行点。
CONTRAST_RAW = "_contrast-raw.json"


def run_child_contrast(group: str, db_path: str) -> int:
    """在**本进程**里把一组对照跑进 ``db_path``。只由 ``--_contrast`` 入口调用。

    注入手法与 ``run_child`` 同一套，理由也一样：``flows/common.py::build()`` 用的是
    ``:memory:``，进程一退库就没了。**一组里的两个 case 共用同一个库文件** ——
    对照的两侧躺在同一份证据里才比得了，分成两个库反而要读者自己去对。

    R6 的「按最新版判」那条错误路径**不落这个库**：它由 ``contrast`` 在一个一次性的
    内存库里算，只读政策、不建 case、不跑 DAG。错误路径是用来演示陷阱的，
    把它的 plan 写进证据束等于在证据里留下一条本不该发生的执行。
    """
    import maos.flows.common as common
    from maos.core.store import SqliteStore

    common.SqliteStore = functools.partial(SqliteStore, db_path)
    from maos.flows import contrast

    rows = contrast.run_group(group)
    doc = {
        "group": group,
        "dimension": rows[0]["expected"]["dimension"],
        "cases": [{"file": r["file"], "expected": r["expected"],
                   "observed": r["observed"], "mismatch": r["mismatch"]} for r in rows],
        "variable": contrast._variable_of(rows),
        "note": contrast._note_of(rows),
    }
    mismatch = [f"{r['file']}: {m}" for r in rows for m in r["mismatch"]]
    if group == "R6":
        wrong, wrong_bad = contrast._r6_wrong_path()
        doc["wrong_if_latest_observed"] = wrong
        mismatch += wrong_bad
    doc["mismatch"] = mismatch

    with open(os.path.join(os.path.dirname(db_path), CONTRAST_RAW), "w",
              encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)

    if mismatch:
        # 判据不符就让子进程非零退出：父进程按「上游命令失败即报错退出」处理，
        # 不留下半份看起来跑通了的证据束（铁律 3）。
        print("对照结果与 case json 的 _expected 不符：\n  " + "\n  ".join(mismatch),
              file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------------------------
# 库读取（原始 SQL，不 import 任何业务域 —— 铁律 9）
# ---------------------------------------------------------------------------
def connect_ro(db_path: str) -> sqlite3.Connection:
    if not os.path.exists(db_path):
        raise EvidenceError(f"场景没有落库: {db_path}")
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


def collect_result(conn: sqlite3.Connection, *, scenario: int, exit_code: int,
                   wall_ms: int, provenance: dict[str, str]) -> dict:
    """终态 + 关键指标 + business_outcome。

    ``business_outcome`` 一律**推导**而来，且带着推导依据一起写出去（铁律 8）：
    MAOS 不持有权威事实，这里写的是「按库里现有观察能推出什么」，
    不是「业务上到底怎样了」。DONE 而找不到外部判据时状态是 ``undetermined``，
    绝不因为 Plan 走到了 DONE 就把它写成 succeeded —— 那正是 verify.py 第 6 项要抓的。
    """
    tables = table_names(conn)
    plans = []
    for p in conn.execute("SELECT * FROM plan ORDER BY created_at"):
        pid = p["plan_id"]
        evs = conn.execute("SELECT * FROM event_log WHERE plan_id=? ORDER BY seq",
                           (pid,)).fetchall()
        counts: dict[str, int] = {}
        for e in evs:
            counts[e["event_type"]] = counts.get(e["event_type"], 0) + 1
        rework = sum(1 for e in evs
                     if e["event_type"] == "StateTransition" and e["to_state"] == "REWORK")
        tasks = [dict(t) for t in conn.execute(
            "SELECT task_id, role, title, state, attempt, risk_level, effect_risk"
            " FROM task WHERE plan_id=? ORDER BY created_at", (pid,))]
        outcome = derive_business_outcome(conn, pid, p["state"], tables, provenance)
        plans.append({
            "plan_id": pid,
            "goal": p["goal"],
            "state": p["state"],
            "trace_id": p["trace_id"],
            "tasks": tasks,
            "metrics": {
                "duration_ms": _delta_ms(p["created_at"], p["updated_at"]),
                "event_count": len(evs),
                "event_types": counts,
                "rework_count": rework,
                "replan_count": counts.get("Replanned", 0),
                "compensation_count": counts.get("CompensationAttached", 0),
                "skill_invocations": counts.get("SkillInvoked", 0),
                "tool_invocations": counts.get("ToolInvoked", 0),
            },
            "business_outcome": outcome,
        })
    return {
        "scenario": scenario,
        "exit_code": exit_code,
        "wall_ms": wall_ms,
        "plan_count": len(plans),
        "plans": plans,
        "totals": {
            "event_count": sum(p["metrics"]["event_count"] for p in plans),
            "rework_count": sum(p["metrics"]["rework_count"] for p in plans),
            "replan_count": sum(p["metrics"]["replan_count"] for p in plans),
        },
    }


def _delta_ms(a: str, b: str) -> int | None:
    try:
        return int((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() * 1000)
    except (TypeError, ValueError):
        return None


def derive_business_outcome(conn, plan_id: str, plan_state: str, tables: set[str],
                            provenance: dict[str, str]) -> dict:
    """按库里的观察推导业务结局，并把每一条依据的出处一起带出来。"""
    evidence: list[dict] = []

    # 判据一：通过的回归报告。它是「外部判定」而非 Agent 自评（自评在 patch_set.self_check）。
    for a in conn.execute(
            "SELECT artifact_id, task_id, version, content FROM artifact"
            " WHERE plan_id=? AND kind='test_report' ORDER BY version", (plan_id,)):
        c = _loads(a["content"], {}) or {}
        if c.get("failed") == 0 and c.get("errors", 0) == 0 and not c.get("tool_error"):
            evidence.append({
                "kind": "test_report",
                "artifact_id": a["artifact_id"],
                "task_id": a["task_id"],
                "version": a["version"],
                "passed": c.get("passed"),
                "provenance": provenance.get(a["artifact_id"], "unknown"),
            })

    # 判据二：退款到账。settled 只有 payment.observe 写得进去（R-1 的 guard），
    # 所以带回执的 settled 是真正的外部判据。表不在就跳过，不臆造。
    if {"refund_case", "payment_observation"} <= tables:
        for c in conn.execute(
                "SELECT tenant_id, case_id, biz_status FROM refund_case"
                " WHERE plan_id=? AND biz_status='settled'", (plan_id,)):
            for o in conn.execute(
                    "SELECT request_id, gateway_code, observed_state, actor_invocation_id"
                    " FROM payment_observation WHERE tenant_id=? AND case_id=?",
                    (c["tenant_id"], c["case_id"])):
                evidence.append({
                    "kind": "payment_observation",
                    "case_id": c["case_id"],
                    "tenant_id": c["tenant_id"],
                    "request_id": o["request_id"],
                    "gateway_code": o["gateway_code"],
                    "observed_state": o["observed_state"],
                    "actor_invocation_id": o["actor_invocation_id"],
                    "provenance": "payment_observation",
                })

    if plan_state == "FAILED":
        status, basis = "failed", "plan_failed"
    elif plan_state == "DONE":
        status = "succeeded" if evidence else "undetermined"
        basis = "external_evidence" if evidence else "no_external_evidence"
    else:
        status, basis = "in_progress", "plan_not_terminal"

    unaudited = [e for e in evidence if e.get("provenance") == "unknown"]
    return {
        "status": status,
        "basis": basis,
        "plan_state": plan_state,
        "external_evidence": evidence,
        "unaudited_evidence_count": len(unaudited),
        "source": "derived-from-db-at-export-time",
        "note": ("MAOS 只持有观察与推断，权威状态归外部系统（铁律 8）。"
                 "本字段是导出时按库内观察推导的结论，不是外部系统的当前值。"),
    }


def collect_business_objects(db_path: str, conn, tables: set[str]) -> dict:
    """本 case 引用的全部业务对象及版本号。

    解析**只走** ``maos.domain.refund.objects.resolve_business_ref`` ——
    ``object_type -> 表/主键`` 的映射在那里有唯一一份，在这里再抄一份就是 C-7 的反例：
    合并后两份映射行为不一致，而症状要到某个 object_type 加进来才暴露。
    域不在（换域 / 未合入）时软降级为空，并把原因写进 note，不假装有数据。
    """
    if "business_ref" not in tables:
        return {"objects": [], "note": "本库无 business_ref 表（退款域未落地），本轮无业务对象引用"}
    try:
        from maos.core.store import SqliteStore
        from maos.domain.refund import objects as refund_objects
    except ImportError as exc:
        return {"objects": [], "note": f"业务域模块不可用（{exc}），只列引用不解析"}

    store = SqliteStore(db_path)
    out = []
    for r in conn.execute("SELECT * FROM business_ref ORDER BY plan_id, task_id,"
                          " object_type, object_id"):
        ref = dict(r)
        try:
            target = refund_objects.resolve_business_ref(store, ref)
        except Exception as exc:                                    # noqa: BLE001
            ref["resolved"] = False
            ref["resolve_error"] = f"{type(exc).__name__}: {exc}"
            out.append(ref)
            continue
        ref["resolved"] = target is not None
        ref["object"] = target
        out.append(ref)
    return {
        "objects": out,
        "resolved": sum(1 for o in out if o.get("resolved")),
        "dangling": sum(1 for o in out if not o.get("resolved")),
        "note": "resolve 走 maos.domain.refund.objects.resolve_business_ref，本脚本不另立映射",
    }


def collect_kb_hits(conn, tables: set[str]) -> dict:
    """RAG 命中。本轮 RAG 未落地 —— 空数组 + 说明，不是假数据。"""
    hits = []
    for e in conn.execute("SELECT * FROM event_log WHERE event_type='KbRetrieved' ORDER BY seq"):
        d = _loads(e["detail"], {}) or {}
        hits.append({"seq": e["seq"], "plan_id": e["plan_id"], "task_id": e["task_id"],
                     "detail": d})
    invocations = []
    for e in conn.execute("SELECT * FROM event_log WHERE event_type='SkillInvoked' ORDER BY seq"):
        d = _loads(e["detail"], {}) or {}
        if d.get("skill") == "kb.retrieve":
            invocations.append({"seq": e["seq"], "plan_id": e["plan_id"],
                                "task_id": e["task_id"], "status": d.get("status"),
                                "invocation_id": d.get("invocation_id")})
    return {
        "hits": hits,
        "kb_retrieve_invocations": invocations,
        "has_kb_doc_table": "kb_doc" in tables,
        "note": ("本轮无 KbRetrieved 事件、无 kb_doc 表（P5 才建），所以 hits 为空数组。"
                 "kb.retrieve 的调用记录列在 kb_retrieve_invocations 供对照，"
                 "但它不含 doc_id / score，不能当 RAG 命中用。"),
    }


def collect_kb_dump(conn, tables: set[str]) -> dict:
    if "knowledge" not in tables:
        return {"entries": [], "note": "本库无 knowledge 表"}
    rows = []
    for k in conn.execute("SELECT * FROM knowledge ORDER BY created_at"):
        d = dict(k)
        d["tags"] = _loads(d.get("tags"), []) or []
        rows.append(d)
    return {"entries": rows, "count": len(rows),
            "note": "来自 knowledge 表（D 轨 kb.sink 的真实沉淀）"}


# ---------------------------------------------------------------------------
# 单场景
# ---------------------------------------------------------------------------
def scenario_module_exists(n: int) -> bool:
    import importlib.util
    return importlib.util.find_spec(f"maos.flows.scenario_{n}") is not None


def write_bundle(db_path: str, out_dir: str, *, scenario: int, exit_code: int,
                 wall_ms: int, log: str, sha: str, secrets: dict[str, str]) -> dict:
    """把一个已经跑完的库写成一套证据文件，返回 trace bundle。

    与 ``build_scenario`` 分开是为了让测试能拿一个手搭的 fixture 库直接喂进来：
    否则测「第 2/3 项的正负例」就得先有退款场景，而那是别人的轨。
    """
    from maos.obs import trace as trace_mod

    write_text(os.path.join(out_dir, "run.log"), log, sha=sha, secrets=secrets)

    bundle = trace_mod.export_trace_bundle(db_path)
    write_json(os.path.join(out_dir, "trace.json"), bundle, sha=sha, secrets=secrets)

    # artifact_id -> provenance，供 business_outcome 标注判据是否经过审计链
    provenance = {
        s["attributes"]["maos.artifact.id"]: s["attributes"]["maos.artifact.provenance"]
        for t in bundle["traces"] for s in t["spans"] if s["kind"] == "artifact"
    }

    conn = connect_ro(db_path)
    try:
        tables = table_names(conn)
        write_json(os.path.join(out_dir, "result.json"),
                   collect_result(conn, scenario=scenario, exit_code=exit_code,
                                  wall_ms=wall_ms, provenance=provenance),
                   sha=sha, secrets=secrets)
        write_json(os.path.join(out_dir, "business-objects.json"),
                   collect_business_objects(db_path, conn, tables), sha=sha, secrets=secrets)
        write_json(os.path.join(out_dir, "kb-hits.json"),
                   collect_kb_hits(conn, tables), sha=sha, secrets=secrets)
        write_json(os.path.join(out_dir, "kb-dump.json"),
                   collect_kb_dump(conn, tables), sha=sha, secrets=secrets)
    finally:
        conn.close()
    return bundle


def build_scenario(n: int, out_root: str, *, sha: str, secrets: dict[str, str],
                   timeout: int) -> dict:
    """跑一场并攒出 ``evidence/scenario-<n>/``。任何一步失败都不留下半份目录。

    先在 ``.tmp-scenario-<n>.<pid>/`` 里攒齐、脱敏反查过关，才 ``os.replace`` 挪到位。
    中途任何异常（包括 KeyboardInterrupt）都连临时目录一起删 —— 半份目录比没有更坏：
    它看起来像跑通了。
    """
    final = os.path.join(out_root, f"scenario-{n}")
    tmp = os.path.join(out_root, f".tmp-scenario-{n}.{os.getpid()}")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)

    try:
        db_path = os.path.join(tmp, "maos.db")
        started = time.perf_counter()
        proc = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "--_child", str(n), "--_db", db_path],
            cwd=ROOT, capture_output=True, text=True, timeout=timeout,
        )
        wall_ms = int((time.perf_counter() - started) * 1000)
        log = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode != 0:
            raise EvidenceError(
                f"场景 {n} 退出码 {proc.returncode}，不生成任何产物。子进程输出尾部：\n"
                + redact(log[-2000:], secrets))
        if not os.path.exists(db_path):
            # 退出码 0 却没落库：多半是场景没走 build()，或注入点挪了位。
            # 这时候更该大声失败 —— 没有库就没有任何东西可核验，
            # 而一份只有 run.log 的目录看起来跟跑通了一模一样。
            raise EvidenceError(
                f"场景 {n} 退出码为 0 却没有落库（{db_path} 不存在）："
                f"SqliteStore 注入点可能已失效，不生成任何产物")

        bundle = write_bundle(db_path, tmp, scenario=n, exit_code=proc.returncode,
                              wall_ms=wall_ms, log=log, sha=sha, secrets=secrets)

        leaks = scan_for_secrets(tmp, secrets)
        if leaks:
            raise EvidenceError(
                f"场景 {n} 的产物里查到敏感值明文，目录已销毁：\n  " + "\n  ".join(leaks))

        shutil.rmtree(final, ignore_errors=True)
        os.replace(tmp, final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise

    return _bundle_info(n, final, bundle)


def _bundle_info(scenario, final: str, bundle: dict) -> dict:
    return {
        "scenario": scenario,
        "dir": os.path.relpath(final, ROOT),
        "span_count": bundle["summary"]["span_count"],
        "event_count": bundle["summary"]["event_count"],
        "unsourced_artifacts": bundle["summary"]["unsourced_artifacts"],
        "stray_events": bundle["summary"]["stray_event_count"],
        "tree_errors": bundle["summary"]["tree_errors"],
    }


def build_contrast(group: str, out_root: str, *, sha: str, secrets: dict[str, str],
                   timeout: int) -> dict:
    """跑一组对照并攒出 ``evidence/contrast-<组>/``。任何一步失败都不留下半份目录。

    与 ``build_scenario`` 同一套「先在临时目录攒齐、脱敏反查过关、才 ``os.replace``
    挪到位」的规矩，产物也走同一个 ``write_bundle`` —— 格式对齐现有证据束不是靠
    照着抄一遍，是靠调同一个函数。对照束比场景束多一份 ``contrast.json``：
    逐 case 的 `_expected` 与实际观测并排放，附不符项清单（空 = 全对）。
    """
    final = os.path.join(out_root, f"contrast-{group}")
    tmp = os.path.join(out_root, f".tmp-contrast-{group}.{os.getpid()}")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)

    try:
        db_path = os.path.join(tmp, "maos.db")
        started = time.perf_counter()
        proc = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "--_contrast", group,
             "--_db", db_path],
            cwd=ROOT, capture_output=True, text=True, timeout=timeout,
        )
        wall_ms = int((time.perf_counter() - started) * 1000)
        log = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode != 0:
            raise EvidenceError(
                f"对照组 {group} 退出码 {proc.returncode}，不生成任何产物。子进程输出尾部：\n"
                + redact(log[-2000:], secrets))
        if not os.path.exists(db_path):
            raise EvidenceError(
                f"对照组 {group} 退出码为 0 却没有落库（{db_path} 不存在）："
                f"SqliteStore 注入点可能已失效，不生成任何产物")

        raw_path = os.path.join(tmp, CONTRAST_RAW)
        with open(raw_path, encoding="utf-8") as fh:
            doc = json.load(fh)
        os.remove(raw_path)      # 无出处首行的中间文件不许进最终目录

        bundle = write_bundle(db_path, tmp, scenario=f"contrast-{group}",
                              exit_code=proc.returncode, wall_ms=wall_ms, log=log,
                              sha=sha, secrets=secrets)
        write_json(os.path.join(tmp, "contrast.json"), doc, sha=sha, secrets=secrets)

        leaks = scan_for_secrets(tmp, secrets)
        if leaks:
            raise EvidenceError(
                f"对照组 {group} 的产物里查到敏感值明文，目录已销毁：\n  "
                + "\n  ".join(leaks))

        shutil.rmtree(final, ignore_errors=True)
        os.replace(tmp, final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise

    return _bundle_info(f"contrast-{group}", final, bundle)


def scan_aux_bundles(out_root: str) -> list[dict]:
    """登记 ``evidence/`` 下**不叫 scenario-\\* 的那些目录**，按目录名排序。

    ``evidence/room/`` 就是一例：房间侧的人机交互证据由别的流程落盘，从前 INDEX 里
    一个字都没有 —— 一份自称是索引的东西，漏登记了一整个目录。

    索引 ≠ 核验器：``verify.py`` 按 ``scenario-`` 前缀挑核验对象，这些目录本来就不该
    被它当证据束扫（它们没有 ``maos.db``，也没有 trace）。这里只负责**登记在册**，
    让「evidence/ 里有什么」这个问题有一个地方能一次答全。

    每个文件顺带记两件读者关心的事：出处首行有没有（``sourced``），
    以及它是不是字节扫描核验不了的图像（``secret_scan`` 见 ``scan_for_secrets``）。
    """
    aux: list[dict] = []
    for name in sorted(os.listdir(out_root)):
        path = os.path.join(out_root, name)
        if not os.path.isdir(path) or name.startswith("scenario-") or name.startswith("."):
            continue
        files = []
        for fn in sorted(os.listdir(path)):
            fp = os.path.join(path, fn)
            if not os.path.isfile(fp):
                continue
            unverifiable = is_unverifiable(fn)
            sourced = None
            if not unverifiable:
                try:
                    with open(fp, encoding="utf-8") as fh:
                        sourced = fh.readline().startswith(HEADER_PREFIX)
                except (OSError, UnicodeDecodeError):
                    sourced = None
            files.append({
                "name": fn,
                "bytes": os.path.getsize(fp),
                "sourced": sourced,
                "secret_scan": "无法核验（图像，密钥是像素不是字节）" if unverifiable else "可扫",
            })
        aux.append({
            "name": name,
            "dir": os.path.relpath(path, ROOT),
            "file_count": len(files),
            "files": files,
        })
    return aux


def _flag_suffix(info: dict) -> str:
    flags = []
    if info["unsourced_artifacts"]:
        flags.append(f"无来源产物 {info['unsourced_artifacts']}")
    if info["stray_events"]:
        flags.append(f"游离事件 {info['stray_events']}")
    if info["tree_errors"]:
        flags.append(f"span 树错误 {len(info['tree_errors'])}")
    return f"  ⚠ {'，'.join(flags)}" if flags else ""


def build_r5(out_root: str, *, secrets: dict[str, str]) -> dict:
    """把 ``scenario-R5`` 也产出来 —— 它不在 ``ALL_SCENARIOS`` 里，得单独叫一次。

    为什么非并进来不可：R5 是唯一带 ``kb_doc`` 的一束，``verify.py`` 第 5、7 项
    全靠它。而「跑 make_evidence.py，再跑 verify.py」这条最直觉的路径，从前会稳定
    地撞上 ``缺数据库: scenario-R5/maos.db``，提示却还是「先跑 make_evidence.py」——
    照做的人原地打转（BACKLOG ## task-W5 第 2 条 / ## task-X3 第 4 条）。

    ``out_root`` **必须**透传：``write_evidence(None)`` 缺省写进仓库 ``evidence/``，
    ``--out`` 指到仓库外却偷偷改仓库，是最难发现的一种污染。有测试守着这一条。

    这里不像场景 1-7 那样起子进程 —— 起子进程是因为 ``flows/common.py::build()``
    用 ``:memory:`` 库，进程一退就没了；而 ``write_evidence`` 自己就落文件库，
    且 KB 开关走上下文管理器进出成对复原，没有要隔离的进程级状态。
    """
    from maos.kb import experiment as kb_experiment

    try:
        final = kb_experiment.write_evidence(out_root)
    except EvidenceError:
        raise
    except Exception as exc:
        # 带上下文重抛：R5 失败与场景失败要落在同一个出口（不留半份产物、退出码 2）。
        raise EvidenceError(f"scenario-R5 生成失败：{redact(str(exc), secrets)}") from exc
    return _bundle_info("R5", final, load_evidence_json(os.path.join(final, "trace.json")))


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main_contrast(args) -> int:
    """``--contrast`` 那一支：只产 ``contrast-R3/R4/R6``。

    **不碰 ``scenario-*``，也不重写 ``INDEX.json``**。索引由缺省全量跑重建，
    届时对照束会作为 ``aux_bundles`` 登记进去（它们不叫 ``scenario-*``，
    ``verify.py`` 本来就不把它们当证据束扫）。在这里顺手重写索引会把上一次
    全量跑的 ``produced`` 清单抹成三条，那份索引就开始说谎了。
    """
    from maos.flows.contrast import GROUPS

    sha = pin_sha()
    secrets = secret_values()
    os.makedirs(args.out, exist_ok=True)
    groups = [g for g, _dim, _title in GROUPS]
    print(f"对照证据束生成 · sha={sha} · 组={groups} · 输出={args.out}")
    if secrets:
        print(f"脱敏哨兵：{sorted(secrets)}（值不打印）")

    produced = []
    for group in groups:
        info = build_contrast(group, args.out, sha=sha, secrets=secrets,
                              timeout=args.timeout)
        produced.append(info)
        print(f"  [OK] {info['dir']}  spans={info['span_count']} "
              f"events={info['event_count']}{_flag_suffix(info)}")
    print(f"\n完成：{len(produced)} 组对照落盘。scenario-* 与 INDEX.json 未被触碰；"
          f"缺省证据束仍是 8 束。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="make_evidence", description="生成 evidence/scenario-<N>/ 证据束")
    parser.add_argument("--out", default=os.path.join(ROOT, "evidence"),
                        help="输出根目录，缺省 evidence/")
    parser.add_argument("--scenarios", default=None,
                        help="逗号分隔的场景号；缺省取 maos.main.ALL_SCENARIOS")
    parser.add_argument("--timeout", type=int, default=600, help="单场景超时秒数")
    parser.add_argument("--strict-scenarios", action="store_true",
                        help="ALL_SCENARIOS 里声明了但模块还没有的场景，视为错误而不是跳过")
    parser.add_argument("--r5", dest="r5", action="store_true", default=None,
                        help="强制一并产出 scenario-R5（缺省：全量跑时产，指定 --scenarios 时不产）")
    parser.add_argument("--no-r5", dest="r5", action="store_false",
                        help="不产 scenario-R5；verify.py 的第 5、7 项会因此判 SKIP")
    parser.add_argument("--contrast", action="store_true",
                        help="只产三组对照束 contrast-R3/R4/R6；不碰 scenario-* 也不重写 INDEX.json")
    parser.add_argument("--_child", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--_contrast", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--_db", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args._child is not None:
        if not args._db:
            raise SystemExit("--_child 必须配 --_db")
        return run_child(args._child, args._db)

    if args._contrast is not None:
        if not args._db:
            raise SystemExit("--_contrast 必须配 --_db")
        return run_child_contrast(args._contrast, args._db)

    # 对照束走一条**完全独立**的路径：缺省那一支（下面整段）一个字节不变，
    # 缺省仍然恒为 8 束。两条路唯一共用的是 build_contrast 里的 write_bundle。
    if args.contrast:
        return main_contrast(args)

    from maos.main import ALL_SCENARIOS

    if args.scenarios:
        wanted = [int(x) for x in args.scenarios.split(",") if x.strip()]
    else:
        wanted = list(ALL_SCENARIOS)
    # 指定了 --scenarios 就是奔着某几场去的，别拖上 R5；全量跑则缺省带上。
    want_r5 = args.r5 if args.r5 is not None else not args.scenarios

    # 钉在动任何文件**之前**：往下每写一个证据文件，工作区就更脏一分，
    # 而这一批证据的出处只有一个 —— 开跑那一刻的 HEAD。R5 由别的模块自算 sha，
    # 也靠这次钉住跟前七束对齐（见 pin_sha）。
    sha = pin_sha()
    secrets = secret_values()
    os.makedirs(args.out, exist_ok=True)
    print(f"证据束生成 · sha={sha} · 场景={wanted}{' + R5' if want_r5 else ''} · 输出={args.out}")
    if secrets:
        print(f"脱敏哨兵：{sorted(secrets)}（值不打印）")

    produced, missing = [], []
    for n in wanted:
        if not scenario_module_exists(n):
            msg = f"场景 {n}：maos.flows.scenario_{n} 不存在（上游未落地）"
            if args.strict_scenarios:
                raise SystemExit(f"[FAIL] {msg}；--strict-scenarios 下视为错误")
            print(f"  [MISSING] {msg}，跳过且不生成目录")
            missing.append(n)
            continue
        info = build_scenario(n, args.out, sha=sha, secrets=secrets, timeout=args.timeout)
        produced.append(info)
        print(f"  [OK] {info['dir']}  spans={info['span_count']} "
              f"events={info['event_count']}{_flag_suffix(info)}")

    if want_r5:
        info = build_r5(args.out, secrets=secrets)
        produced.append(info)
        print(f"  [OK] {info['dir']}  spans={info['span_count']} "
              f"events={info['event_count']}{_flag_suffix(info)}")

    aux = scan_aux_bundles(args.out)
    write_json(os.path.join(args.out, "INDEX.json"), {
        "git_sha": sha,
        "requested": [*wanted, "R5"] if want_r5 else wanted,
        "produced": produced,
        "missing_scenarios": missing,
        "aux_bundles": aux,
        "note": ("missing_scenarios 是 ALL_SCENARIOS 声明了但流程模块尚未落地的场景，"
                 "它们不生成目录、也不写占位数据。"
                 "R5 不在 ALL_SCENARIOS 里（由 maos.kb.experiment 产），"
                 "缺省一并产出，--no-r5 可关掉。"
                 "aux_bundles 是 evidence/ 下不叫 scenario-* 的目录（如 room/）："
                 "它们不由本脚本产、verify.py 也不把它们当证据束扫，"
                 "但索引要登记在册 —— 漏登记一整个目录的索引不叫索引。"),
    }, sha=sha, secrets=secrets)

    for bundle in aux:
        print(f"  [AUX] {bundle['dir']}  文件 {bundle['file_count']}（不由本脚本产，仅登记）")

    if missing:
        print(f"\n注意：{missing} 已在 ALL_SCENARIOS 中声明但模块未落地，本次未生成其目录。")
    print(f"\n完成：{len(produced)} 场景落盘，{len(missing)} 场景缺模块。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except EvidenceError as exc:
        print(f"\n[FAIL] {exc}", file=sys.stderr)
        sys.exit(2)
