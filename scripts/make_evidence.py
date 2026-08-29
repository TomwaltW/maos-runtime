#!/usr/bin/env python3
"""证据束生成器 —— 一键跑全部场景，把每一场的真实产出落成 ``evidence/scenario-<N>/``。

    python3 scripts/make_evidence.py                    # 全部场景 + scenario-R5
    python3 scripts/make_evidence.py --scenarios 1,2    # 只跑指定场景（不含 R5）

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
def git_sha() -> str:
    """当前提交 sha；**被跟踪文件**有改动时带 ``-dirty``。取不到就抛，不给「unknown」兜底。

    脏判定刻意用 ``--untracked-files=no``：``evidence/`` 本身就是这个脚本正在生成的
    未跟踪产物，把它算进「脏」会让每一次生成都自称 dirty，于是这个标记恒为真、
    再也指示不了任何东西 —— 而它要指示的是「跑这批证据的代码与 HEAD 不一致」。
    """
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


def scan_for_secrets(directory: str, secrets: dict[str, str]) -> list[str]:
    """拿哨兵串把目录逐字节反查一遍，返回命中描述（空 = 干净）。

    按字节查而不是按行读文本：证据里可能有二进制（sqlite 库文件就在同一目录），
    按文本读会因为解码失败而**跳过**那个文件，于是漏查得悄无声息。
    """
    hits: list[str] = []
    needles = {name: value.encode("utf-8") for name, value in secrets.items()}
    for base, _dirs, files in os.walk(directory):
        for fn in files:
            path = os.path.join(base, fn)
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
    parser.add_argument("--_child", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--_db", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args._child is not None:
        if not args._db:
            raise SystemExit("--_child 必须配 --_db")
        return run_child(args._child, args._db)

    from maos.main import ALL_SCENARIOS

    if args.scenarios:
        wanted = [int(x) for x in args.scenarios.split(",") if x.strip()]
    else:
        wanted = list(ALL_SCENARIOS)
    # 指定了 --scenarios 就是奔着某几场去的，别拖上 R5；全量跑则缺省带上。
    want_r5 = args.r5 if args.r5 is not None else not args.scenarios

    sha = git_sha()
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

    write_json(os.path.join(args.out, "INDEX.json"), {
        "git_sha": sha,
        "requested": [*wanted, "R5"] if want_r5 else wanted,
        "produced": produced,
        "missing_scenarios": missing,
        "note": ("missing_scenarios 是 ALL_SCENARIOS 声明了但流程模块尚未落地的场景，"
                 "它们不生成目录、也不写占位数据。"
                 "R5 不在 ALL_SCENARIOS 里（由 maos.kb.experiment 产），"
                 "缺省一并产出，--no-r5 可关掉。"),
    }, sha=sha, secrets=secrets)

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
