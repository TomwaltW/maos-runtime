#!/usr/bin/env python3
"""按一张**退款申请表**批量处置 —— 这是给业务方（不写代码的人）的入口。

    python3 scripts/run_requests.py scenarios/custom/refund-requests.csv

## 分工：谁给什么

  · **底账**（`scenarios/custom/ledger.json`）：客户、渠道、商品、订单快照、公司的
    售后政策。IT / 顾问配一次，真实落地时由 ERP 导出，**不用天天动**。
  · **申请表**（CSV，Excel 存一下就有）：老板/客服每天给的东西，一单一行，四列：
    `订单号, 诉求类型, 申报金额, 申请日期`（外加一列随便写的说明）。

订单号一填，租户、渠道、商品、下单当时锁定的政策版本全部**从底账里查出来** ——
这些是外部系统的事实，不该让人每次手抄一遍（抄错一次，裁定就错一次）。

诉求类型写中文即可（质量问题 / 七天无理由 / 发错货），也接受英文 code。
申报金额留空 = 按订单实付金额。申请日期留空 = 按今天算。

跑完给一张中文结果表：每单批不批、退多少、钱到没到账、依据哪条政策、几次转人工。
`--csv out.csv` 可把这张表存成 Excel 能打开的文件。
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from maos.flows.custom_case import CaseFileError, load, run_payload  # noqa: E402

DEFAULT_LEDGER = Path(__file__).resolve().parents[1] / "scenarios" / "custom" / "ledger.json"

#: 老板会写的说法 -> 系统里的诉求类型。写不在表里的词会当场报错并列出可选项，
#: **不猜**：猜错一个词，套用的就是另一条政策。
REASONS: dict[str, str] = {
    "质量问题": "quality_defect", "质量缺陷": "quality_defect", "有质量问题": "quality_defect",
    "坏了": "quality_defect", "损坏": "quality_defect",
    "七天无理由": "no_reason_return", "无理由": "no_reason_return",
    "无理由退货": "no_reason_return", "不想要了": "no_reason_return",
    "买错了": "no_reason_return", "买错型号": "no_reason_return",
    "发错货": "wrong_item", "发错型号": "wrong_item", "错发": "wrong_item",
}

#: 表头别名。CSV 是人手填的，列名叫法不会统一。
COLUMNS: dict[str, tuple[str, ...]] = {
    "order_id": ("订单号", "订单编号", "order_id", "order"),
    "reason": ("诉求类型", "退款原因", "原因", "reason", "reason_code"),
    "amount": ("申报金额", "退款金额", "金额", "amount", "amount_claimed"),
    "date": ("申请日期", "申请时间", "日期", "date", "requested_at"),
    "note": ("说明", "备注", "note", "remark"),
}

DECISION_CN = {"approve": "批准", "reject": "驳回"}
STATUS_CN = {
    "settled": "已到账", "gateway_accepted": "已提交网关·未确认", "processing": "网关处理中",
    "submitted": "未发起退款", "approved": "已批准·未发起", "compensated": "已补偿",
    "rejected": "已驳回",
}
DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M", "%Y.%m.%d")


class RequestSheetError(ValueError):
    """申请表里有填不对的地方。消息直接给人看。"""


def _pick(row: dict, key: str) -> str:
    for name in COLUMNS[key]:
        for raw_key, value in row.items():
            if raw_key and raw_key.strip().lstrip("﻿") == name:
                return (value or "").strip()
    return ""


def _reason_code(raw: str) -> str:
    text = raw.strip()
    if not text:
        raise RequestSheetError("诉求类型不能空 —— 不知道为什么退，就套不上任何一条政策")
    if text in REASONS:
        return REASONS[text]
    if text in set(REASONS.values()):
        return text                                  # 直接写英文 code 也认
    raise RequestSheetError(
        f"看不懂的诉求类型 {text!r}。可以写：{'、'.join(sorted(set(REASONS)))}；"
        f"或直接写 {'、'.join(sorted(set(REASONS.values())))}")


def _parse_date(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _iso(raw: str) -> str:
    """把人写的日期变成**带时区**的 ISO8601。空 = 现在。看不懂就报错，不猜。

    补时区那一步不能省：`2026-07-10` 解析出来是 naive，而订单快照的 `paid_at`
    一律带时区，两者相减当场抛 TypeError —— 且抛在流程中段，报错指着
    `contrast.elapsed_days`，跟填表的人写了什么完全对不上。
    """
    text = raw.strip()
    if not text:
        return datetime.now(timezone.utc).isoformat()
    dt = _parse_date(text)
    if dt is None:
        raise RequestSheetError(f"看不懂的日期 {text!r}，写成 2026-07-10 这样就行")
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).isoformat()


def read_sheet(path: str | Path) -> list[dict]:
    """读申请表。每行返回 `{order_id, reason, amount, requested_at, note}`。"""
    p = Path(path)
    if not p.exists():
        raise RequestSheetError(f"找不到申请表：{p}")
    with p.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise RequestSheetError(f"{p} 里一行申请都没有")

    out: list[dict] = []
    for lineno, row in enumerate(rows, start=2):     # 第 1 行是表头
        order_id = _pick(row, "order_id")
        if not order_id:
            raise RequestSheetError(f"第 {lineno} 行没有订单号 —— 订单号是查出其余一切的钥匙")
        amount = _pick(row, "amount").replace(",", "")
        try:
            out.append({
                "line": lineno, "order_id": order_id,
                "reason_raw": _pick(row, "reason"),
                "reason": _reason_code(_pick(row, "reason")),
                "amount": float(amount) if amount else None,
                "requested_at": _iso(_pick(row, "date")),
                "note": _pick(row, "note"),
            })
        except RequestSheetError as exc:
            raise RequestSheetError(f"第 {lineno} 行（订单 {order_id}）：{exc}") from exc
        except ValueError as exc:
            raise RequestSheetError(
                f"第 {lineno} 行（订单 {order_id}）金额 {amount!r} 不是数字：{exc}") from exc
    return out


def build_case(ledger: dict, req: dict) -> dict:
    """把一行申请 + 底账拼成一份完整 case。

    订单号是**唯一**要人填的钥匙：租户、渠道、商品、订单版本、实付金额全部从
    `order_snapshot` 查出来。让人手抄这些字段，抄错一个裁定就错一次，而且不会报错。
    """
    orders = [o for o in ledger.get("order_snapshot", []) if o["order_id"] == req["order_id"]]
    if not orders:
        raise RequestSheetError(
            f"底账里没有订单 {req['order_id']} —— 先让它进 ledger.json 的 order_snapshot")
    order = max(orders, key=lambda o: int(o["version"]))

    payload = dict(ledger)
    payload["requested_at"] = req["requested_at"]
    payload["case"] = {
        "tenant_id": order["tenant_id"],
        "case_id": f"RC-{req['order_id']}",
        "channel_id": order["channel_id"],
        "order_id": order["order_id"],
        "order_version": int(order["version"]),
        "sku": order["sku"],
        "reason_code": req["reason"],
        "amount_claimed": req["amount"] if req["amount"] is not None else float(order["amount_paid"]),
    }
    return payload


# ------------------------------------------------------------------ 结果表输出
def _w(text: str) -> int:
    """显示宽度：中日韩字符占两列。不算这个，表格会歪得没法看。"""
    return sum(2 if ord(c) > 0x2E80 else 1 for c in str(text))


def _pad(text: str, width: int) -> str:
    return str(text) + " " * max(0, width - _w(text))


HEADERS = ("订单号", "诉求", "裁定", "核准金额", "退款状态", "依据", "转人工")


def as_table(rows: list[dict]) -> str:
    body = [[r["order_id"], r["reason_raw"] or r["reason"], r["decision_cn"],
             r["amount_approved"], r["status_cn"], r["basis"], str(r["human_exits"])]
            for r in rows]
    widths = [max(_w(h), *(_w(c[i]) for c in body)) for i, h in enumerate(HEADERS)]
    line = "  ".join(_pad(h, w) for h, w in zip(HEADERS, widths)).rstrip()
    out = [line, "-" * _w(line)]
    out += ["  ".join(_pad(c, w) for c, w in zip(cells, widths)).rstrip() for cells in body]
    return "\n".join(out)


def summarize(rows: list[dict]) -> str:
    ok = [r for r in rows if r["decision"] == "approve"]
    paid = sum(float(r["amount_approved"]) for r in rows if r["status"] == "settled")
    settled = sum(1 for r in rows if r["status"] == "settled")
    humans = sum(r["human_exits"] for r in rows)
    return (f"共 {len(rows)} 单：批准 {len(ok)}、驳回 {len(rows) - len(ok)}；"
            f"已到账 {settled} 单合计 {paid:.2f} 元；期间 {humans} 次停下来等人放行。")


def run_sheet(sheet: str | Path, ledger_path: str | Path, *, approve: bool = True,
              matrix: bool = False, allow_degraded: bool = False) -> list[dict]:
    ledger = load(ledger_path, require_case=False)
    results: list[dict] = []
    for req in read_sheet(sheet):
        print(f"处理 {req['order_id']}（{req['reason_raw'] or req['reason']}）…")
        row = run_payload(build_case(ledger, req), approve=approve, verbose=False,
                          matrix=matrix, allow_degraded=allow_degraded)
        results.append({
            "order_id": req["order_id"], "reason_raw": req["reason_raw"],
            "reason": row["reason_code"], "note": req["note"],
            "decision": row["decision"], "decision_cn": DECISION_CN.get(row["decision"], "?"),
            "amount_claimed": row["amount_claimed"],
            "amount_approved": row["amount_approved"],
            "status": row["biz_status"],
            "status_cn": STATUS_CN.get(row["biz_status"] or "", row["biz_status"] or "—"),
            # 「依据」只写**决定批不批的那一条**。没有时限规则可判时（比如质量问题）
            # 裁定走的是基线，把命中的规则全列出来反而看不出是谁定的。
            "basis": (row["deciding_rule"]
                      or (f"按基线（命中 {len(row['matched_rules'])} 条售后规则）"
                          if row["matched_rules"] else "无适用政策")),
            "why": row["why"],
            "human_exits": len(row["human_exits"]),
            "plan_state": row["plan_state"],
        })
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_requests", description="按一张退款申请表批量处置（无 key、零出网）")
    parser.add_argument("sheet", nargs="?",
                        default=str(DEFAULT_LEDGER.parent / "refund-requests.csv"),
                        help="申请表 CSV 路径")
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER),
                        help="底账 JSON（客户/渠道/商品/订单/政策），缺省用 scenarios/custom/ledger.json")
    parser.add_argument("--reject", action="store_true", help="主管一律驳回（演示驳回路径）")
    parser.add_argument("--csv", metavar="OUT", default=None, help="结果表另存成 CSV")
    parser.add_argument("--matrix", action="store_true", help="事件链镜像进 Matrix 房间")
    parser.add_argument("--allow-degraded", action="store_true",
                        help="--matrix 没接通房间时照跑不误")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING,
                        format="%(levelname)-5s %(name)-12s %(message)s")

    try:
        rows = run_sheet(args.sheet, args.ledger, approve=not args.reject,
                         matrix=args.matrix, allow_degraded=args.allow_degraded)
    except (RequestSheetError, CaseFileError) as exc:
        print(f"表填得不对：{exc}", file=sys.stderr)
        return 2

    print(f"\n{'=' * 78}\n退款处置结果\n{'=' * 78}")
    print(as_table(rows))
    print(f"\n{summarize(rows)}")
    for row in rows:
        print(f"  · {row['order_id']}：{row['why']}")

    if args.csv:
        with Path(args.csv).open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(("订单号", "诉求", "裁定", "申报金额", "核准金额",
                             "退款状态", "依据", "转人工次数", "理由"))
            for row in rows:
                writer.writerow((row["order_id"], row["reason_raw"], row["decision_cn"],
                                 row["amount_claimed"], row["amount_approved"],
                                 row["status_cn"], row["basis"], row["human_exits"],
                                 row["why"]))
        print(f"\n结果表已另存：{args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
