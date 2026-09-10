#!/usr/bin/env python3
"""圆桌回放 —— **零模型**，只读 `event_log`，把一单的五岗顺序重建出来。

    python3 scripts/replay_roundtable.py --db /tmp/rt.db --case RC-ORD-2026-0004
    python3 scripts/replay_roundtable.py --db /tmp/rt.db --case RC-ORD-2026-0004 --json
    python3 scripts/replay_roundtable.py --db /tmp/rt.db --list

## 它回答的那个问题

评委问「这五岗到底调了什么、说了什么」时，从前我们只有截图。截图证明不了顺序、
证明不了哪几句是模型说的、更证明不了两次跑的是不是同一段话。这个脚本把答案从
库里读出来：谁在第几位、这句话是模型复述的还是原样的事实卡、没用模型是因为什么、
最后合议建议了什么。

**一次模型都不调**，也不 import 圆桌引擎 —— 回放要能在一台没配 key、甚至没并入
圆桌代码的机器上跑，否则它证明的就不是「库里有什么」，而是「引擎现在算出什么」。

## 顺序从哪来

`event_log.seq` 是自增主键，写入顺序就是发生顺序。一轮的形状是

    RoundtableRound → 5 × RoundtableSeatSpoke → RoundtableVerdict

`SkillInvoked`（证据核验与风险筛查那两条）夹在座位之间，本脚本按 `task_id` 归到
那一轮里一并报 —— 「这一岗说话之前真的跑过 skill 吗」正是事件链要证的另一半。

同一单跑过两轮（复检那条路）时就有两组，按 seq 先后各报一段。

## 退出码

  0  回放出至少一轮
  2  库读不到 / 这个 case 在库里一条事件都没有（数据问题，人能自己修）
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

#: 圆桌那一摊行的 plan_id 前缀（`maos/roundtable/team.py::PLAN_PREFIX`）。
#: 抄字面量而不是 import：见模块抬头「不 import 圆桌引擎」那一条。
PLAN_PREFIX = "roundtable:"

EXIT_OK = 0
EXIT_DATA = 2

#: 一轮的三类事件。顺序就是它们该出现的顺序。
ROUND_EVENT = "RoundtableRound"
SPOKE_EVENT = "RoundtableSeatSpoke"
VERDICT_EVENT = "RoundtableVerdict"
SKILL_EVENT = "SkillInvoked"

#: 回退原因的人话。取值域是 `maos/roundtable/speaker.py` 的五个 `FALLBACK_*`；
#: **认不出来的原样打印**，不猜 —— 引擎哪天多一种回退，这里该显出那个新字面量，
#: 而不是把它归进「其它」里悄悄抹掉。
FALLBACK_CN = {
    "": "",
    "no_model": "这台机器没配模型",
    "skipped": "事实卡只有总结句，按 SKIP_MODEL_WHEN_FLAT 跳过模型",
    "error": "调模型抛异常",
    "empty": "模型回了空话",
    "validation": "两稿都没过门（数字/结构/格式）",
}


def connect(db_path) -> sqlite3.Connection:             # noqa: ANN001
    """只读连一个库。库不存在时 `mode=ro` 会当场抛，而不是凭空建一个空库 ——
    后者的症状是「回放说这一单没有事件」，而事实是路径打错了。"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def plan_id_of(case_id: str) -> str:
    """`RC-xxx` -> `roundtable:RC-xxx`。已经带前缀的原样返回 —— 人会两种都敲。"""
    case = str(case_id or "")
    return case if case.startswith(PLAN_PREFIX) else f"{PLAN_PREFIX}{case}"


def list_plans(conn: sqlite3.Connection) -> list[dict]:
    """库里有哪几摊圆桌行。给 `--list` 用，也给「case 打错了」时的提示用。"""
    rows = conn.execute(
        "SELECT plan_id, COUNT(*) AS n, MIN(created_at) AS first_at,"
        " MAX(created_at) AS last_at FROM event_log WHERE plan_id LIKE ?"
        " GROUP BY plan_id ORDER BY MIN(seq)", (f"{PLAN_PREFIX}%",)).fetchall()
    return [dict(r) for r in rows]


def read_events(conn: sqlite3.Connection, plan_id: str) -> list[dict]:
    """一摊行，按 seq 升序。`detail` 就地解析成 dict。"""
    out: list[dict] = []
    for r in conn.execute(
            "SELECT seq, plan_id, task_id, event_type, detail, created_at"
            " FROM event_log WHERE plan_id=? ORDER BY seq", (plan_id,)):
        row = dict(r)
        try:
            row["detail"] = json.loads(row["detail"] or "{}")
        except ValueError:                              # 那一列不是合法 JSON
            row["detail"] = {"_unparsed": row["detail"]}
        out.append(row)
    return out


def rebuild(events: list[dict]) -> list[dict]:
    """把一摊事件切成一轮一轮。

    **以 `RoundtableRound` 开一轮**，其后的座位、skill、合议都归它，直到下一条
    `RoundtableRound`。开头就没有 `RoundtableRound`（引擎版本更早、或那条落库失败）
    时起一个「无头轮」把后面的事件收住 —— 丢掉它们等于回放悄悄少报几岗，
    而少报与「本来就没说」在屏幕上长得一模一样。
    """
    rounds: list[dict] = []

    def _open(detail: dict, seq) -> dict:               # noqa: ANN001
        rounds.append({
            "seq": seq, "entry": str(detail.get("entry") or ""),
            "round_no": detail.get("round_no"),
            "tenant_id": str(detail.get("tenant_id") or ""),
            "case_id": str(detail.get("case_id") or ""),
            "sheet_digest": str(detail.get("sheet_digest") or ""),
            "seats_expected": list(detail.get("seats") or []),
            "seats": [], "skills": [], "verdict": None, "headless": not detail,
        })
        return rounds[-1]

    current: dict | None = None
    for e in events:
        kind = e["event_type"]
        detail = e["detail"] if isinstance(e["detail"], dict) else {}
        if kind == ROUND_EVENT:
            current = _open(detail, e["seq"])
            continue
        if current is None:
            current = _open({}, e["seq"])
        if kind == SPOKE_EVENT:
            current["seats"].append({
                "seq": e["seq"], "seat": str(detail.get("seat") or ""),
                "spoken_by_model": bool(detail.get("spoken_by_model")),
                "fallback_reason": str(detail.get("fallback_reason") or ""),
                "facts_digest": str(detail.get("facts_digest") or ""),
                "speech_digest": str(detail.get("speech_digest") or ""),
                "speech_len": detail.get("speech_len"),
                "model_call_id": str(detail.get("model_call_id") or ""),
            })
        elif kind == SKILL_EVENT:
            current["skills"].append({
                "seq": e["seq"], "skill": str(detail.get("skill") or ""),
                "status": str(detail.get("status") or ""),
                "task_id": e.get("task_id"),
                "duration_ms": detail.get("duration_ms"),
                "invocation_id": str(detail.get("invocation_id") or ""),
            })
        elif kind == VERDICT_EVENT:
            current["verdict"] = {
                "seq": e["seq"],
                "recommend": str(detail.get("recommend") or ""),
                "approver_role": str(detail.get("approver_role") or ""),
                "blockers": list(detail.get("blockers") or []),
                "seats": list(detail.get("seats") or []),
            }
    return rounds


def _seat_line(index: int, seat: dict) -> str:
    """一岗一行。**模型发言与事实卡要一眼分得开** —— 那是房间里唯一能自证 R1 的信息。"""
    who = "模型复述" if seat["spoken_by_model"] else "事实卡"
    reason = seat["fallback_reason"]
    why = FALLBACK_CN.get(reason, reason)
    tail = f"　回退：{why}" if why else ""
    return (f"  {index}. {seat['seat']:<16} [{who}]　"
            f"facts {seat['facts_digest']} → speech {seat['speech_digest']}"
            f"（{seat['speech_len']} 字）{tail}")


def render(plan_id: str, rounds: list[dict], *, db_path, out) -> None:  # noqa: ANN001
    print(f"圆桌回放 ｜ 库 {Path(db_path).name} ｜ {plan_id} ｜ {len(rounds)} 轮",
          file=out)
    for i, rnd in enumerate(rounds, 1):
        head = f"\n第 {i} 段 ｜ 入口 {rnd['entry'] or '未知'}"
        if rnd["round_no"] is not None:
            head += f" ｜ 引擎记的第 {rnd['round_no']} 轮"
        if rnd["case_id"]:
            head += f" ｜ case {rnd['case_id']}"
        if rnd["sheet_digest"]:
            head += f" ｜ 整表 {rnd['sheet_digest']}"
        if rnd["tenant_id"]:
            head += f" ｜ 租户 {rnd['tenant_id']}"
        if rnd["headless"]:
            head += " ｜ ⚠ 没有 RoundtableRound 开头，这一段是收拢出来的"
        print(head, file=out)

        expected = rnd["seats_expected"]
        got = [s["seat"] for s in rnd["seats"]]
        print(f"  座次：{'、'.join(got) or '（一岗都没有）'}", file=out)
        if expected and expected != got:
            # 名册与实际发言对不上是真该查的一种：某一岗在房间里凭空消失过。
            print(f"  ⚠ 名册是 {'、'.join(expected)}，与实际发言对不上", file=out)
        for index, seat in enumerate(rnd["seats"], 1):
            print(_seat_line(index, seat), file=out)

        if rnd["skills"]:
            print("  这一轮跑过的 skill：", file=out)
            for s in rnd["skills"]:
                print(f"    · {s['skill']} [{s['status']}] "
                      f"{s['duration_ms']} ms　task={s['task_id']}", file=out)

        v = rnd["verdict"]
        if v is None:
            print("  合议：（这一轮没有 RoundtableVerdict）", file=out)
        else:
            blockers = "、".join(str(b) for b in v["blockers"]) or "无"
            print(f"  合议：建议 {v['recommend'] or '未给'}"
                  f" ｜ 审批角色 {v['approver_role'] or '未指定'}"
                  f" ｜ 拦路项 {blockers}", file=out)


def _no_such_case(plan_id: str, plans: list[dict], *, out) -> int:  # noqa: ANN001
    """这个 case 在库里没有事件。**把库里有什么一并报出来** —— 只说「没找到」，
    人下一步只能自己去写 SQL，而十有八九是 case 号敲错了一位。"""
    print(f"库里没有 {plan_id} 的圆桌事件。", file=out)
    if plans:
        print("库里有这几摊：", file=out)
        for p in plans:
            print(f"  · {p['plan_id']}（{p['n']} 条）", file=out)
    else:
        print("这个库里一条圆桌事件都没有 —— 跑冒烟时带上 --db 才会落库："
              "python3 scripts/room_team_smoke.py --db <库>", file=out)
    return EXIT_DATA


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="replay_roundtable",
        description="从 event_log 回放一单圆桌的五岗顺序与合议结论（零模型）")
    parser.add_argument("--db", required=True, metavar="路径",
                        help="room_team_smoke.py --db 或 MAOS_INGRESS_DB 落下来的库")
    parser.add_argument("--case", metavar="case_id", default=None,
                        help="要回放的 case（`RC-…`，也接受完整的 `roundtable:RC-…`）")
    parser.add_argument("--list", action="store_true", dest="as_list",
                        help="只列出库里有哪几摊圆桌行，不回放")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="打成 JSON，供机器比对")
    args = parser.parse_args(argv)
    if not args.as_list and not args.case:
        parser.error("要么给 --case，要么给 --list")

    try:
        conn = connect(args.db)
    except sqlite3.Error as exc:
        print(f"读不到库：{exc}", file=sys.stderr)
        return EXIT_DATA

    try:
        plans = list_plans(conn)
        if args.as_list:
            if args.as_json:
                print(json.dumps({"db": Path(args.db).name, "plans": plans},
                                 ensure_ascii=False, indent=2))
            else:
                print(f"库 {Path(args.db).name} 里的圆桌行：", file=sys.stdout)
                for p in plans:
                    print(f"  · {p['plan_id']}（{p['n']} 条，"
                          f"{p['first_at']} → {p['last_at']}）")
                if not plans:
                    print("  （一条都没有）")
            return EXIT_OK

        plan_id = plan_id_of(args.case)
        events = read_events(conn, plan_id)
    except sqlite3.Error as exc:
        print(f"读不到事件：{exc}", file=sys.stderr)
        return EXIT_DATA
    finally:
        conn.close()

    if not events:
        return _no_such_case(plan_id, plans, out=sys.stdout)

    rounds = rebuild(events)
    if args.as_json:
        print(json.dumps({"db": Path(args.db).name, "plan_id": plan_id,
                          "event_count": len(events), "rounds": rounds},
                         ensure_ascii=False, indent=2))
    else:
        render(plan_id, rounds, db_path=args.db, out=sys.stdout)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
