#!/usr/bin/env python3
"""客服前台运营统计（p14 跨轨契约 §2「T180」）。

::

    python3 scripts/cs_stats.py --db PATH [--tenant T] [--since ISO8601] [--top N] [--json]

**只读**：库以 ``file:…?mode=ro`` 打开（同 scripts/replay_roundtable.py）—— 库不存在时当场报错退 2，
不会凭空建一个空库；本脚本不写任何一行。库里一张 cs_ 表都没有 → 全部统计为零并在输出里说明，退 0。
某一张 cs_ 表不在（例如 p12 时代的库没有 cs_turn_ext）→ 只有依赖它的那几项为零。

输出的统计（每项的数据源与口径）：

* 会话数（按 stage）—— ``cs_conversation.stage``；
* 轮数、route 分布 —— ``cs_turn.route``；
* 意图 top-N —— ``cs_turn.intent``，**不含 silent 轮**（已转人工的会话只记录，意图恒为 unknown）；
* 转人工原因分布 —— ``cs_turn.handoff_reason``（非空的那些轮）；
* 兜底率 / 转人工率 —— route=fallback / route=handoff 的轮数除以**应答轮**（非 silent 的轮）；
* 查单结果分布 —— ``cs_turn_ext.lookup_outcome``（非空）；
* 追问按槽位 —— ``cs_turn_ext.ask_slot``（非空）；
* 后置校验拦下按 violation kind —— event_log 的 ``CsReplyRejected``（``detail.violation_kinds``；
  同一次拦截里同种违例只算一次，另报拦截总次数）；
* 引用话术 top-N —— ``cs_turn.draft_json`` 的 ``citations``（doc_id）；
* 会诊卡数与 recommendation 分布 —— event_log 的 ``CsConferenceHeld``（``detail.recommendation``；没有就 0）。

**只出计数与 id**（契约 §0 / §2「T180」）：客户原文（inbound_text / reply_text）、external_userid、
open_kfid、query_key、订单号（order_no / display_no）、槽位值、卡片正文一律不出 —— 本脚本根本不 SELECT
这些列。从库里读出来、会原样进输出的只有枚举与 doc_id，而且都先过白名单：枚举不在冻结集合里的归
``other``，doc_id 不合 ``kb-…`` 形状的归 ``other`` —— 库被写坏也漏不出原文。

``--tenant T``：只统计该租户（cs_ 表按 ``tenant_id``；event_log 行按 plan_id ``cs:<会话>`` 归到
该租户的会话）。``--since``：只统计该时刻及以后的（轮按 ``cs_turn.created_at``、会话按
``updated_at``、事件按 ``event_log.created_at``；不带时区的按 UTC；库里解析不了的时间戳不计入）。

退出码：0 出了统计（含库里没有 cs_ 表）；2 用法错 / 库读不到。
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from maos.domain.cs.ports import LOOKUP_OUTCOMES, SLOT_KEYS          # noqa: E402
from maos.domain.cs.types import (                                   # noqa: E402
    CS_PLAN_PREFIX, EVENT_CONFERENCE_HELD, EVENT_REPLY_REJECTED, HANDOFF_REASONS, INTENTS,
    ROUTE_FALLBACK, ROUTE_HANDOFF, ROUTE_SILENT, ROUTES, STAGES, VIOLATION_KINDS,
)

EXIT_OK = 0
EXIT_USAGE = 2

DEFAULT_TOP = 10

#: 会诊卡的 recommendation 枚举（p14 契约 §2「T178」冻结的 RECOMMENDATIONS）。T178 与本轨并行，
#: 这里抄字面量而不 import maos/roundtable/cs_conference.py；不在这里的值一律归 ``other``。
RECOMMENDATIONS = ("send_refund_command", "supervisor_review", "callback_soothe", "verify_identity",
                   "manual_lookup", "standard_followup")

#: 认不出的枚举值统一归到这一桶（不原样输出库里的值）。
OTHER = "other"

#: 可以原样输出的 doc_id 形状：话术 doc_id 是 ``kb-cs-<租户>-<方案编号>``。
_DOC_ID_RE = re.compile(r"^kb-[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$")

#: 统计要读的 cs_ 表。
CS_TABLES = ("cs_conversation", "cs_turn", "cs_turn_ext")

NOTE_NO_CS = "库里没有 cs_ 表（客服前台从未在这个库上跑过）：以下统计全部为零"


# ---------------------------------------------------------------------------
# 连接与过滤
# ---------------------------------------------------------------------------
def connect_ro(db_path: str) -> sqlite3.Connection:
    """只读连一个库。库不存在时 ``mode=ro`` 当场抛 ``sqlite3.OperationalError``。"""
    uri = pathlib.Path(db_path).expanduser().resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    # 触一下库：不是 sqlite 文件时在这里就抛，而不是查到一半才抛。
    conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
    return conn


def parse_since(raw: str) -> datetime:
    """``--since`` 的值 → 带时区的时刻（不带时区按 UTC）。解析不了抛 ValueError。"""
    dt = datetime.fromisoformat(raw.strip())
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _ts(raw: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(raw or "").strip())
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _in_window(raw: Any, since: datetime | None) -> bool:
    if since is None:
        return True
    dt = _ts(raw)
    return dt is not None and dt >= since


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _enum(value: Any, allowed: Iterable[str]) -> str:
    v = str(value or "")
    return v if v in set(allowed) else OTHER


def _doc_id(value: Any) -> str:
    v = str(value or "")
    return v if _DOC_ID_RE.match(v) else OTHER


def _zeros(keys: Iterable[str]) -> dict[str, int]:
    return {k: 0 for k in keys}


def _bump(bucket: dict[str, int], key: str) -> None:
    bucket[key] = bucket.get(key, 0) + 1


def _top(counter: Counter, n: int) -> list[list[Any]]:
    """按计数降序、同数按键升序（确定性）取前 n。"""
    return [[k, c] for k, c in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:n]]


def _rate(num: int, den: int) -> float:
    return round(num / den, 4) if den else 0.0


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------
def empty_stats(*, tenant: str | None, since: str | None, top: int) -> dict[str, Any]:
    """全零的统计（形状即输出契约）。"""
    return {
        "filters": {"tenant": tenant, "since": since, "top": top},
        "has_cs_tables": False,
        "notes": [],
        "conversations": {"total": 0, "by_stage": _zeros(STAGES)},
        "turns": 0,
        "answered_turns": 0,
        "routes": _zeros(ROUTES),
        "intents_top": [],
        "handoff_reasons": _zeros(HANDOFF_REASONS),
        "rates": {"fallback_rate": 0.0, "handoff_rate": 0.0, "denominator": "answered_turns"},
        "lookup_outcomes": _zeros(LOOKUP_OUTCOMES),
        "clarify_by_slot": _zeros(SLOT_KEYS),
        "rejections": {"total": 0, "by_kind": _zeros(VIOLATION_KINDS)},
        "scripts_top": [],
        "conferences": {"total": 0, "by_recommendation": _zeros(RECOMMENDATIONS)},
    }


def _where(tenant: str | None) -> tuple[str, tuple[Any, ...]]:
    return ("WHERE tenant_id = ?", (tenant,)) if tenant is not None else ("", ())


def collect(conn: sqlite3.Connection, *, tenant: str | None = None, since: str | None = None,
            top: int = DEFAULT_TOP) -> dict[str, Any]:
    """读库出统计。只 SELECT 枚举列、计数列、时间戳与 id 列（见模块说明）。"""
    since_dt = parse_since(since) if since else None
    out = empty_stats(tenant=tenant, since=since, top=top)
    tables = _tables(conn)
    present = [t for t in tables if t.startswith("cs_")]
    if not present:
        out["notes"].append(NOTE_NO_CS)
    else:
        out["has_cs_tables"] = True
        missing = [t for t in CS_TABLES if t not in tables]
        if missing:
            out["notes"].append("库里缺这几张 cs_ 表，依赖它们的统计为零：" + "、".join(missing))
    where, params = _where(tenant)

    # -- 会话 ------------------------------------------------------------
    tenant_convs: set[str] | None = None
    if "cs_conversation" in tables:
        tenant_convs = set()
        for r in conn.execute("SELECT conversation_id, stage, updated_at FROM cs_conversation "
                              + where, params):
            tenant_convs.add(str(r["conversation_id"]))
            if not _in_window(r["updated_at"], since_dt):
                continue
            out["conversations"]["total"] += 1
            _bump(out["conversations"]["by_stage"], _enum(r["stage"], STAGES))

    # -- 轮 ----------------------------------------------------------------
    intents: Counter = Counter()
    scripts: Counter = Counter()
    window_turns: set[str] = set()
    if "cs_turn" in tables:
        for r in conn.execute("SELECT turn_id, route, intent, handoff_reason, draft_json, created_at"
                              " FROM cs_turn " + where, params):
            if not _in_window(r["created_at"], since_dt):
                continue
            window_turns.add(str(r["turn_id"]))
            out["turns"] += 1
            route = _enum(r["route"], ROUTES)
            _bump(out["routes"], route)
            if route != ROUTE_SILENT:
                out["answered_turns"] += 1
                intents[_enum(r["intent"], INTENTS)] += 1
            if r["handoff_reason"]:
                _bump(out["handoff_reasons"], _enum(r["handoff_reason"], HANDOFF_REASONS))
            try:
                draft = json.loads(r["draft_json"] or "{}")
            except ValueError:
                draft = {}
            cites = draft.get("citations") if isinstance(draft, dict) else None
            for c in set(cites if isinstance(cites, list) else ()):
                scripts[_doc_id(c)] += 1
    out["intents_top"] = _top(intents, top)
    out["scripts_top"] = _top(scripts, top)
    answered = out["answered_turns"]
    out["rates"]["fallback_rate"] = _rate(out["routes"].get(ROUTE_FALLBACK, 0), answered)
    out["rates"]["handoff_rate"] = _rate(out["routes"].get(ROUTE_HANDOFF, 0), answered)

    # -- 轮次扩展（查单结果、追问）：只算落在窗口里的轮 --------------------------
    if "cs_turn_ext" in tables:
        for r in conn.execute("SELECT turn_id, lookup_outcome, ask_slot FROM cs_turn_ext "
                              + where, params):
            if "cs_turn" in tables and str(r["turn_id"]) not in window_turns:
                continue
            if r["lookup_outcome"]:
                _bump(out["lookup_outcomes"], _enum(r["lookup_outcome"], LOOKUP_OUTCOMES))
            if r["ask_slot"]:
                _bump(out["clarify_by_slot"], _enum(r["ask_slot"], SLOT_KEYS))

    # -- event_log：拦截与会诊卡 ---------------------------------------------
    if "event_log" in tables:
        for r in conn.execute(
                "SELECT plan_id, event_type, detail, created_at FROM event_log"
                " WHERE event_type IN (?, ?) AND plan_id LIKE ? ORDER BY seq",
                (EVENT_REPLY_REJECTED, EVENT_CONFERENCE_HELD, CS_PLAN_PREFIX + "csc-%")):
            conv = str(r["plan_id"])[len(CS_PLAN_PREFIX):]
            if tenant is not None and (tenant_convs is None or conv not in tenant_convs):
                continue
            if not _in_window(r["created_at"], since_dt):
                continue
            try:
                detail = json.loads(r["detail"] or "{}")
            except ValueError:
                detail = {}
            if not isinstance(detail, dict):
                detail = {}
            if r["event_type"] == EVENT_REPLY_REJECTED:
                out["rejections"]["total"] += 1
                kinds = detail.get("violation_kinds")
                for k in {_enum(k, VIOLATION_KINDS) for k in (kinds if isinstance(kinds, list)
                                                              else ())}:
                    _bump(out["rejections"]["by_kind"], k)
            else:
                out["conferences"]["total"] += 1
                _bump(out["conferences"]["by_recommendation"],
                      _enum(detail.get("recommendation"), RECOMMENDATIONS))
    return out


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------
def _dist(title: str, d: Mapping[str, int]) -> list[str]:
    total = sum(d.values())
    lines = [f"{title}（合计 {total}）"]
    lines += [f"  {k:<22} {v}" for k, v in d.items() if v or k != OTHER]
    return lines


def _toplist(title: str, rows: Sequence[Sequence[Any]], n: int) -> list[str]:
    lines = [f"{title}（top {n}）"]
    lines += [f"  {k:<22} {c}" for k, c in rows] or ["  （无）"]
    return lines


def render_text(stats: Mapping[str, Any]) -> str:
    f = stats["filters"]
    lines = ["客服前台运营统计（只出计数与 id）",
             f"过滤：tenant={'（全部）' if f['tenant'] is None else repr(f['tenant'])}"
             f"  since={f['since'] or '（不限）'}  top={f['top']}"]
    lines += [f"说明：{n}" for n in stats["notes"]]
    conv = stats["conversations"]
    lines += _dist(f"会话数 {conv['total']}，按 stage", conv["by_stage"])
    lines.append(f"轮数 {stats['turns']}（应答轮 {stats['answered_turns']}，不含 silent）")
    lines += _dist("route 分布", stats["routes"])
    r = stats["rates"]
    lines.append(f"兜底率 {r['fallback_rate']:.4f}  转人工率 {r['handoff_rate']:.4f}（分母：应答轮）")
    lines += _toplist("意图（不含 silent 轮）", stats["intents_top"], f["top"])
    lines += _dist("转人工原因", stats["handoff_reasons"])
    lines += _dist("查单结果", stats["lookup_outcomes"])
    lines += _dist("追问（按槽位）", stats["clarify_by_slot"])
    rej = stats["rejections"]
    lines += _dist(f"后置校验拦下 {rej['total']} 次，按 violation kind（每次同种只计一次）",
                   rej["by_kind"])
    lines += _toplist("引用话术 doc_id", stats["scripts_top"], f["top"])
    cf = stats["conferences"]
    lines += _dist(f"会诊卡 {cf['total']} 张，按 recommendation", cf["by_recommendation"])
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cs_stats.py", description="客服前台运营统计（只读、只出计数与 id）")
    p.add_argument("--db", required=True, help="SQLite 库路径（只读打开）")
    p.add_argument("--tenant", default=None, help="只统计这个租户")
    p.add_argument("--since", default=None, help="只统计该时刻及以后（ISO8601；不带时区按 UTC）")
    p.add_argument("--top", type=int, default=DEFAULT_TOP, help=f"top-N 的 N（缺省 {DEFAULT_TOP}）")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:                  # argparse 的用法错 → 2；--help → 0
        return int(exc.code or 0)
    if args.top < 1:
        print("cs_stats: --top 必须 ≥ 1", file=sys.stderr)
        return EXIT_USAGE
    if args.since:
        try:
            parse_since(args.since)
        except ValueError:
            print("cs_stats: --since 不是 ISO8601 时刻", file=sys.stderr)
            return EXIT_USAGE
    try:
        conn = connect_ro(args.db)
    except sqlite3.Error as exc:
        print(f"cs_stats: 库读不到（{type(exc).__name__}）", file=sys.stderr)
        return EXIT_USAGE
    try:
        stats = collect(conn, tenant=args.tenant, since=args.since, top=args.top)
    except sqlite3.Error as exc:
        print(f"cs_stats: 读库出错（{type(exc).__name__}）", file=sys.stderr)
        return EXIT_USAGE
    finally:
        conn.close()
    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=2, sort_keys=False))
    else:
        sys.stdout.write(render_text(stats))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
