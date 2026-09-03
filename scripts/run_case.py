#!/usr/bin/env python3
"""跑一份**你自己的**退款 case —— 输入一个 JSON，输出一次真实处置的结果。

    python3 scripts/run_case.py scenarios/custom/refund-case.json
    python3 scripts/run_case.py <你的.json> --reject            # 主管驳回
    python3 scripts/run_case.py <你的.json> --fail-with ACQ.SYSTEM_ERROR
    python3 scripts/run_case.py <你的.json> --json out.json     # 结果另存一份

改数据不改代码：政策窗口、金额、渠道、政策版本全在 JSON 里，跑出来的结论随它变。
形状说明见 `scenarios/custom/README.md`，流程实现在 `maos/flows/custom_case.py`。

**无 key、零出网**：全程走 ScriptedModelClient 与 MockGateway，与 `run.py` 同一条路。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from maos.flows.custom_case import CaseFileError, RoomNotConnected, run_file  # noqa: E402

BAR = "=" * 68

#: 与 `hiclaw/room_demo.py` 的 `EXIT_NO_ROOM` 同一个数：要了房间却没进去，不许 exit 0。
EXIT_NO_ROOM = 4

#: 装了 matrix-nio 的那个解释器。系统 python3 没装，跑 --matrix 会静默降级 ——
#: 而降级的终端输出与真房间**一模一样**，所以这条提示要给得很具体。
VENV_PYTHON = "~/.maos-matrix/venv/bin/python"


def _fmt_amount(row: dict) -> str:
    if row["decision"] != "approve":
        return "不予退款（裁定 reject，DAG 里不排核算 —— 0 元分录会让下游误以为核算过了）"
    ver = row["policy_version_used"]
    return (f"{row['amount_approved']}（按政策 v{ver}，依据 {row['rule_refs']}）"
            if ver is not None else f"{row['amount_approved']}")


def _fmt_payment(row: dict) -> str:
    """支付这一栏。**没有观察**与**没有发起**是两件事，分开说。

    退款已经发起、却一条观察都没落库，正是铁律 8 的样子：网关问不出终态时
    系统什么都不写。把这一格写成「没走到这一步」会把它说反 —— 钱可能真的动了。
    """
    obs = row["payment_observations"]
    if not obs:
        if row["biz_status"] in ("gateway_accepted", "processing"):
            return ("已发起，但一次终态观察都没落库 —— 网关没问出终态，"
                    "系统就什么都不写（钱的下落归网关，不归这里）")
        return "无 —— 没走到发起退款这一步"
    last = obs[-1]
    tail = f"，网关码 {last['gateway_code']}" if last.get("gateway_code") else ""
    if last.get("resolved_from"):
        tail += f"（先报 {last['resolved_from']}，轮询后才问出下落）"
    return (f"{len(obs)} 条观察，终态 {last['observed_state']}"
            f"（poll_count={last.get('poll_count')}）{tail}")


def _fmt_notify(row: dict) -> str:
    notes = row["notifications"]
    if not notes:
        return "无"
    acked = "已确认" if notes[0]["acked"] else "未确认（needs_followup，不阻塞）"
    return f"{len(notes)} 条，{acked}"


def report(row: dict) -> None:
    """人话摘要。每一行都是**从库里读出来的事实**，不是流程自述。"""
    print(f"\n{BAR}\n自定义 case {row['case_id']} · 处置结果\n{BAR}")
    print(f"  诉求      : {row['reason_code']}，申报 {row['amount_claimed']}"
          f"，付款 {row['paid_at'][:10]} -> 第 {row['elapsed_days']} 天申请")
    print(f"  适用政策  : v{row['pinned_policy_version']}（下单锁定），"
          f"命中 {', '.join(row['matched_rules']) or '无'}")
    print(f"  裁定      : {row['decision']} —— {row['why']}")
    exits = row["human_exits"]
    if not exits:
        print("  人工介入  : 无 —— 没有任务停下来等人")
    else:
        verb = "放行" if row["approved"] else "驳回"
        who = row["approvals"][0] if row["approvals"] else row["approver_role"]
        print(f"  人工介入  : {len(exits)} 次转人工，全部{verb}（{who}）")
        for e in exits:
            print(f"              · {e['title']} —— {e['why']}")
    if row["extra_tasks"]:
        print(f"  政策附加  : {', '.join(row['extra_tasks'])}（由命中规则展开）")
    print(f"  核准金额  : {_fmt_amount(row)}")
    print(f"  支付      : {_fmt_payment(row)}")
    print(f"  业务状态  : {row['biz_status']}"
          f"（settled 只可能由 payment.observe 写入，"
          f"本次 settled 观察 {row['settled_observations']} 条）")
    print(f"  客户通知  : {_fmt_notify(row)}")
    print(f"  Plan      : {row['plan_state']}，{len(row['tasks'])} 个任务，"
          f"business_ref {row['business_refs']} 条")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_case", description="跑一份自定义退款 case（无 key、零出网）")
    parser.add_argument("case", help="case JSON 路径")
    parser.add_argument("--reject", action="store_true",
                        help="主管驳回而不是放行（缺省放行）")
    parser.add_argument("--fail-with", metavar="CODE", default=None,
                        help="给网关注入错误码，如 ACQ.SYSTEM_ERROR；码必须在 gateway_codes 里")
    parser.add_argument("--json", metavar="OUT", default=None,
                        help="把结果另存成 JSON")
    parser.add_argument("--matrix", action="store_true",
                        help=f"事件链镜像进 Matrix 房间。需先 . ~/.maos-matrix/room.env，"
                             f"并用 {VENV_PYTHON} 跑（系统 python3 没装 matrix-nio）")
    parser.add_argument("--allow-degraded", action="store_true",
                        help=f"--matrix 没接通房间时照跑不误，而不是 exit {EXIT_NO_ROOM}")
    parser.add_argument("--quiet", action="store_true",
                        help="不打印状态迁移轨迹，只留结果摘要")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)-5s %(name)-12s %(message)s")
    logging.getLogger("maos.bus").setLevel(logging.WARNING)

    try:
        row = run_file(args.case, approve=not args.reject, fail_with=args.fail_with,
                       matrix=args.matrix, verbose=not args.quiet,
                       allow_degraded=args.allow_degraded)
    except CaseFileError as exc:
        print(f"输入有问题：{exc}", file=sys.stderr)
        return 2
    except RoomNotConnected as exc:
        print(f"{exc}\n"
              f"  终端仍会照常刷「房间消息」，但房间里一条都不会有 —— 所以这里直接停。\n"
              f"  先 . ~/.maos-matrix/room.env，再用 {VENV_PYTHON} 重跑同一条命令。\n"
              f"  确实只想看降级形态，显式加 --allow-degraded。", file=sys.stderr)
        return EXIT_NO_ROOM
    except ValueError as exc:                      # 未收录的网关码等，消息本身够清楚
        print(f"跑不下去：{exc}", file=sys.stderr)
        return 3

    report(row)
    if args.json:
        Path(args.json).write_text(
            json.dumps(row, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"\n结果已另存：{args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
