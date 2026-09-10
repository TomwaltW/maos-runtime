#!/usr/bin/env python3
"""客户侧入站命令 —— 把「客户确认」和「客户投诉」从靶场事件变成真的入站通道。

    python3 scripts/case_inbound.py --db evidence/scenario-6/maos.db --confirm case-s6-0001
    python3 scripts/case_inbound.py --db <库> --dispute  case-s6-0001
    python3 scripts/case_inbound.py --db <库> --complain case-s6-0001 "退款到账了但少了 50 元"
    python3 scripts/case_inbound.py --db <库> --show     case-s6-0001

## 为什么要有这个入口

评委第三条要用「退款到账、**客户确认**、人工纠错和**投诉结果**」验证整条 DAG。
在此之前，客户确认在系统里的样子是对照实验里的一句
`UPDATE notification SET ack_at=...`（`kb/experiment.py::_ack_notifications`，
那个函数的 docstring 自己就写着「靶场事件，与 MockGateway 同性质」），
而投诉**全仓零命中** —— 一张表都没有，一条命令都没有。

四判据里有两条只能由客户产生，那它们就必须有一条客户走得进来的路。本文件是这条路
在 CLI 上的样子；房间 / IM 侧的 `/confirm` `/complain` 由 T117 做成模块、整合期接线，
两边最终调的是 `domain/refund/outcome.py` 里同一组函数，不各写一套。

## 它不碰权威事实

`--confirm` 只写「客户说收到了」（`notification.ack_at` + `case_outcome`），
**不碰 `arrival`**：钱到没到账归网关说了算，客户说收到了也不行（铁律 8）。
所以确认过的案子照样可能 `arrival=unknown` —— 这不是矛盾，这正是四判据要分开的原因。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from maos.core.store import SqliteStore                              # noqa: E402
from maos.domain.refund import objects, outcome                      # noqa: E402

#: 找不到库时的退出码。与「参数不对」（2）分开：库不在是环境问题，不是打错命令。
EXIT_NO_DB = 3
#: 库里没有这个 case。同上，与参数错误分开 —— 打对了命令、指错了案子。
EXIT_NO_CASE = 4


def _open(db_path: str) -> SqliteStore:
    if not os.path.exists(db_path):
        raise FileNotFoundError(
            f"库不在：{db_path}\n"
            f"  证据束里的库由 `python3 scripts/make_evidence.py` 产出，"
            f"自定义 case 的库由 `scripts/run_case.py` 现跑现丢（内存库）。")
    store = SqliteStore(db_path)
    store.init_schema()
    objects.ensure_schema(store)
    outcome.ensure_outcome_schema(store)
    return store


def _resolve_case(store: SqliteStore, case_id: str, tenant_id: str | None) -> tuple[str, str]:
    """把 `case_id` 定位到 `(tenant_id, case_id)`。

    不给租户时从库里查。**查出多个租户共用同一个 case_id 就报错而不是挑第一个**：
    退款域所有表的主键都以 tenant_id 打头，挑错租户等于把一位客户的确认记到另一位
    头上，而那是查不出来的一类脏数据。
    """
    rows = objects.query(
        store, "SELECT tenant_id, case_id, plan_id, biz_status FROM refund_case WHERE case_id=?",
        (case_id,))
    if tenant_id:
        rows = [r for r in rows if r["tenant_id"] == tenant_id]
    if not rows:
        raise LookupError(f"库里没有 case {case_id}"
                          + (f"（租户 {tenant_id}）" if tenant_id else ""))
    tenants = {r["tenant_id"] for r in rows}
    if len(tenants) > 1:
        raise LookupError(
            f"case {case_id} 在 {len(tenants)} 个租户下都存在（{sorted(tenants)}），"
            f"用 --tenant 指明是哪一个 —— 挑错租户会把确认记到别的客户头上")
    return rows[0]["tenant_id"], rows[0]["plan_id"]


def _print_outcome(row: dict, *, head: str) -> None:
    print(f"\n{head}")
    print(f"  到账      : {row.get('arrival')}"
          f"（依据 {row.get('arrival_basis') or '无观察行 —— 网关没问出终态'}）")
    print(f"  客户确认  : {row.get('customer_confirmation')}")
    print(f"  人工纠错  : {row.get('manual_correction')}")
    print(f"  投诉      : {row.get('complaint')}")
    print(f"  证据完整  : {bool(row.get('evidence_complete'))}")
    print(f"  业务成功  : {bool(row.get('business_success'))}"
          f"  <- (到账==settled) 且 (确认!=disputed) 且 (投诉!=open)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="case_inbound",
        description="客户侧入站：确认 / 异议 / 投诉，落库后重算业务四判据")
    parser.add_argument("--db", required=True, help="maos.db 路径")
    parser.add_argument("--tenant", default=None,
                        help="租户号；同一 case_id 跨租户重名时必须给")
    parser.add_argument("--channel", default="cli",
                        help="入站渠道名，落进 complaint.channel，缺省 cli")
    parser.add_argument("--json", action="store_true", help="机器可读输出")

    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--confirm", metavar="CASE", help="客户确认收到退款")
    action.add_argument("--dispute", metavar="CASE", help="客户提出异议（一票否决业务成功）")
    action.add_argument("--complain", nargs=2, metavar=("CASE", "TEXT"),
                        help="客户投诉；正文只存摘要，原文不落库")
    action.add_argument("--close-complaint", nargs=3,
                        metavar=("CASE", "DIGEST", "RESOLUTION"),
                        help="关闭一条投诉（DIGEST 取自 --show 的输出）")
    action.add_argument("--show", metavar="CASE", help="只看当前四判据，不写任何东西")
    args = parser.parse_args(argv)

    case_id = (args.confirm or args.dispute or args.show
               or (args.complain or args.close_complaint or [None])[0])
    try:
        store = _open(args.db)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return EXIT_NO_DB
    try:
        tenant_id, plan_id = _resolve_case(store, str(case_id), args.tenant)
    except LookupError as exc:
        print(exc, file=sys.stderr)
        return EXIT_NO_CASE

    try:
        if args.confirm:
            row = outcome.record_confirmation(
                store, tenant_id=tenant_id, case_id=case_id, plan_id=plan_id,
                decision=outcome.CONFIRMATION_CONFIRMED, channel=args.channel)
            head = f"客户已确认 —— case {case_id} 四判据"
        elif args.dispute:
            row = outcome.record_confirmation(
                store, tenant_id=tenant_id, case_id=case_id, plan_id=plan_id,
                decision=outcome.CONFIRMATION_DISPUTED, channel=args.channel)
            head = f"客户提出异议 —— case {case_id} 四判据"
        elif args.complain:
            row = outcome.record_complaint(
                store, tenant_id=tenant_id, case_id=case_id, plan_id=plan_id,
                content=args.complain[1], channel=args.channel)
            head = f"投诉已受理（open 一票否决业务成功）—— case {case_id} 四判据"
        elif args.close_complaint:
            row = outcome.close_complaint(
                store, tenant_id=tenant_id, case_id=case_id, plan_id=plan_id,
                content_digest=args.close_complaint[1],
                resolution=args.close_complaint[2], channel=args.channel)
            head = f"投诉已关闭 —— case {case_id} 四判据"
        else:
            # 从没算过就现算一次并落库。**标题里如实说是哪一种** —— 写着「只读」
            # 却顺手插了一行，正是这一轨在拆的那类表述。
            row = outcome.read_case_outcome(store, tenant_id=tenant_id, case_id=case_id)
            head = f"case {case_id} 四判据（读已落库的那一行）"
            if row is None:
                row = outcome.record_case_outcome(
                    store, tenant_id=tenant_id, case_id=case_id, plan_id=plan_id)
                head = f"case {case_id} 四判据（此前没算过，本次现算并落库）"
    except outcome.OutcomeError as exc:
        print(f"做不了：{exc}", file=sys.stderr)
        return 2

    complaints = outcome.list_complaints(store, tenant_id=tenant_id, case_id=case_id)
    if args.json:
        print(json.dumps({"tenant_id": tenant_id, "case_id": case_id, "plan_id": plan_id,
                          "case_outcome": dict(row),
                          "complaints": [dict(c) for c in complaints]},
                         ensure_ascii=False, indent=2))
        return 0

    _print_outcome(dict(row), head=head)
    if complaints:
        print(f"  投诉明细  : {len(complaints)} 条")
        for c in complaints:
            state = "已关闭" if c.get("closed_at") else "**开着**"
            print(f"              · {c['channel']}/{c['content_digest']} {state}"
                  + (f" —— {c['resolution']}" if c.get("resolution") else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
