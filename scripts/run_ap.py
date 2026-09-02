#!/usr/bin/env python3
"""按一张**发票明细表**批量处置供应链付款 —— 这是给业务方（不写代码的人）的入口。

    python3 scripts/run_ap.py scenarios/custom/ap-invoices.csv

## 分工：谁给什么

  · **底账**（`scenarios/custom/ap-ledger.json`）：供应商主数据、采购订单、收货单。
    IT / 顾问配一次，真实落地由 ERP / WMS 导出，**不用天天动**。
  · **申请表**（CSV，Excel 存一下就有）：业务方每天给的东西，**一行 = 一个发票行
    项目**，同一张发票多行。列：`发票号, 采购单号, 行号, SKU, 发票数量, 发票单价,
    开票日期, 到期日, 税率`（外加一列随便写的说明）。

发票号一填，**供应商、币种、订单数量与单价、收货数量全部从底账查出来** —— 那些是
外部系统的事实，不该让人每次手抄（抄错一次，匹配结论就错一次，而且不会报错）。
人只填发票上真实印着的东西。

税率写 `13`、`13%`、`0.13` 都认：小于 1 的数按小数形式理解，乘 100。真要写千分位的
`0.13%` 就带上百分号 —— 带号的一律按字面值算，不再乘。

同一张发票的多行必须共享 `采购单号 / 开票日期 / 到期日 / 税率`，**不一致当场报错，
不取第一行也不取多数**：这四项是发票抬头上的东西，一张发票只有一份。

## R1：待复核表不许直接进付款决策

图片抽出来的字段必须先落成**待复核表**（多两列 `来源` / `需人工确认`），人核对完把
「是」改成「否」，才转成正式申请表。本入口**拒绝任何 `需人工确认=是` 的行** ——
视觉模型看错一个小数点 = 多付一笔钱。理解可以错，授权不许错。

跑完给一张中文结果表：每张发票匹配过没过、差在哪一行哪个量、付款计划多少钱、
停在哪个状态等谁批、钱到没到账、几次转人工。`--csv out.csv` 存成 Excel 能打开的文件。

**全程零出网**：模型客户端一律 `force_scripted=True`，配了 key 的机器上也一行网络不走。
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from maos.flows.ap_case import ApLedgerError, build_case, load, run_payload  # noqa: E402

CUSTOM = Path(__file__).resolve().parents[1] / "scenarios" / "custom"
DEFAULT_LEDGER = CUSTOM / "ap-ledger.json"
DEFAULT_SHEET = CUSTOM / "ap-invoices.csv"

#: 表头别名。CSV 是人手填的，列名叫法不会统一。
COLUMNS: dict[str, tuple[str, ...]] = {
    "invoice_id": ("发票号", "发票号码", "发票编号", "invoice_id", "invoice"),
    "po_id": ("采购单号", "采购订单号", "采购订单", "订单号", "po_id", "po"),
    "line_no": ("行号", "序号", "line_no", "line"),
    "sku": ("SKU", "sku", "物料号", "料号", "商品编码"),
    "quantity": ("发票数量", "数量", "quantity", "qty"),
    "unit_price": ("发票单价", "单价", "unit_price", "price"),
    "issued_at": ("开票日期", "开票日", "invoice_date", "issued_at"),
    "due_at": ("到期日", "付款到期日", "due_at", "due_date"),
    "tax_rate": ("税率", "tax_rate", "vat_rate"),
    "note": ("说明", "备注", "note", "remark"),
    # —— 下面两列只出现在**待复核表**上，正式申请表没有。认得出是为了拒绝，见 R1 ——
    "needs_review": ("需人工确认", "待人工确认", "needs_review"),
    "source": ("来源", "来源图片", "source"),
}

#: 必须有的列。缺哪一列就报哪一列 —— 「格式不对」这种话帮不了填表的人。
REQUIRED_COLUMNS = ("invoice_id", "po_id", "line_no", "sku", "quantity",
                    "unit_price", "issued_at", "due_at", "tax_rate")

#: 报错时用的中文列名（别名表的第一个）。
CANON = {key: names[0] for key, names in COLUMNS.items()}

#: 同一张发票的多行必须一致的四项 —— 它们是发票抬头上的东西，一张发票只有一份。
SHARED_FIELDS = ("po_id", "issued_at", "due_at", "tax_rate")

#: R1 的判据。写别的词一律报错，**不猜**：把「待定」当成「否」就是让一张没人核对过的
#: 发票进了付款决策。
REVIEW_YES = {"是", "y", "yes", "true", "1", "待确认", "需确认"}
REVIEW_NO = {"否", "n", "no", "false", "0", ""}

DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M", "%Y.%m.%d",
                "%Y年%m月%d日")

#: 金额里人会顺手带上的东西。
_MONEY_NOISE = (",", "，", "￥", "¥", "元", " ")

STATUS_CN = {
    "received": "已收票·未匹配",
    "matched": "已匹配·待出计划",
    "payment_requested": "已发指令·未确认",
    "settled": "已到账",
    "rejected": "已拒付",
    "compensated": "已转人工对账",
}


class InvoiceSheetError(ValueError):
    """申请表里有填不对的地方。消息直接给人看。"""


# ------------------------------------------------------------------ 单元格解析
def _pick(row: dict, key: str) -> str:
    for name in COLUMNS[key]:
        for raw_key, value in row.items():
            if raw_key and raw_key.strip().lstrip("﻿") == name:
                return (value or "").strip()
    return ""


def _has_column(fieldnames: list[str], key: str) -> bool:
    present = {(f or "").strip().lstrip("﻿") for f in fieldnames}
    return any(name in present for name in COLUMNS[key])


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
    """开票日期 -> **带时区**的 ISO8601。看不懂就报错，不猜。

    补时区那一步不能省：`2026-08-19` 解析出来是 naive，而三单落库之后的时间字段
    一律带时区，两者相减会在流程中段抛 TypeError —— 报错指着引擎内部，
    跟填表的人写了什么完全对不上。
    """
    dt = _parse_date(raw)
    if dt is None:
        raise InvoiceSheetError(f"看不懂的日期 {raw!r}，写成 2026-08-19 这样就行")
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).isoformat()


def _date(raw: str) -> str:
    """到期日 -> `YYYY-MM-DD`。留空就是空 —— 付款条款里没写到期日是常见的。"""
    if not raw:
        return ""
    dt = _parse_date(raw)
    if dt is None:
        raise InvoiceSheetError(f"看不懂的到期日 {raw!r}，写成 2026-09-18 这样就行")
    return dt.date().isoformat()


def _money(raw: str, *, what: str) -> str:
    """单价 -> 规范化的十进制字符串。带 ¥ / 逗号 / 「元」都认，认不出就报错。"""
    text = raw
    for noise in _MONEY_NOISE:
        text = text.replace(noise, "")
    if not text:
        raise InvoiceSheetError(f"{what}不能空")
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise InvoiceSheetError(f"{what} {raw!r} 不是数字：{exc}") from exc
    if value < 0:
        raise InvoiceSheetError(f"{what} {raw!r} 是负数 —— 红字发票要走贷记单，不走这条入口")
    return str(value)


def _quantity(raw: str, *, what: str) -> float:
    text = raw.replace(",", "").replace("，", "").strip()
    if not text:
        raise InvoiceSheetError(f"{what}不能空")
    try:
        value = float(text)
    except ValueError as exc:
        raise InvoiceSheetError(f"{what} {raw!r} 不是数字：{exc}") from exc
    if value < 0:
        raise InvoiceSheetError(f"{what} {raw!r} 是负数")
    return value


def _tax_rate(raw: str) -> float:
    """税率 -> 百分数（13% 记作 13.0）。`13` / `13%` / `0.13` 都认。

    小于 1 且**没带百分号**的数按小数形式理解（0.13 = 13%）—— 这是 Excel 里最常见的
    存法。真要写千分位的 `0.13%` 就带上百分号：带号的一律按字面值算，不再乘 100。
    """
    text = raw.replace(" ", "").replace("％", "%")
    if not text:
        raise InvoiceSheetError("税率不能空 —— 税额算不出来，勾稽就无从谈起")
    percent = text.endswith("%")
    if percent:
        text = text[:-1]
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise InvoiceSheetError(
            f"看不懂的税率 {raw!r}，写成 13、13% 或 0.13 都行：{exc}") from exc
    if value < 0:
        raise InvoiceSheetError(f"税率 {raw!r} 是负数")
    if not percent and value < 1:
        value *= 100
    return float(value)


def _needs_review(raw: str, *, lineno: int) -> bool:
    text = raw.strip().lower()
    if text in REVIEW_YES:
        return True
    if text in REVIEW_NO:
        return False
    raise InvoiceSheetError(
        f"第 {lineno} 行的「{CANON['needs_review']}」写着 {raw!r} —— 只认「是」或「否」，"
        f"不猜：把拿不准的当成「否」，就是让一张没人核对过的发票进了付款决策")


# ------------------------------------------------------------------ 读申请表
def read_sheet(path: str | Path) -> list[dict]:
    """读申请表，按发票号归并。每张发票返回一份 `build_case()` 认得的入参。"""
    p = Path(path)
    if not p.exists():
        raise InvoiceSheetError(f"找不到申请表：{p}")
    with p.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)

    if not fieldnames:
        raise InvoiceSheetError(f"{p} 是空文件 —— 第一行应该是表头："
                                f"{','.join(CANON[k] for k in REQUIRED_COLUMNS)}")
    missing = [CANON[k] for k in REQUIRED_COLUMNS if not _has_column(fieldnames, k)]
    if missing:
        raise InvoiceSheetError(
            f"{p} 的表头缺这几列：{'、'.join(missing)}。完整的一行表头是："
            f"{','.join(CANON[k] for k in REQUIRED_COLUMNS)},说明")
    if not rows:
        raise InvoiceSheetError(f"{p} 里一行发票都没有（只有表头）")

    _refuse_unreviewed(rows, fieldnames)

    parsed = [_read_line(row, lineno) for lineno, row in enumerate(rows, start=2)]
    return _group(parsed)


def _refuse_unreviewed(rows: list[dict], fieldnames: list[str]) -> None:
    """R1：待复核表不许直接喂进来。

    这一条不是「顺手加的校验」：待复核表与正式申请表的列几乎一样，多的那两列
    （`来源` / `需人工确认`）是**唯一**能把「模型抽出来的」和「人核对过的」分开的东西。
    认不出它们，一张视觉模型看错小数点的发票就会一路走到付款指令。

    # INTEGRATION-POINT: 待复核表由 T72 的 `scripts/review_invoices.py` 产出。
    # 并轨期两边都照跨轨契约 §1.4 的列定义写，**不 import 对方** —— 列名对齐靠
    # 契约，不靠代码依赖。整合时把这里的列名换成 T72 那边的常量即可。
    """
    if not _has_column(fieldnames, "needs_review"):
        return
    bad = [lineno for lineno, row in enumerate(rows, start=2)
           if _needs_review(_pick(row, "needs_review"), lineno=lineno)]
    if bad:
        raise InvoiceSheetError(
            f"这是一张**待复核表**：第 {'、'.join(str(n) for n in bad)} 行的"
            f"「{CANON['needs_review']}」还是「是」。抽取结果不许直接进付款决策 —— "
            f"先人工核对，把这一列改成「否」再喂进来（scripts/review_invoices.py "
            f"产出的就是这张表）")


def _read_line(row: dict, lineno: int) -> dict:
    invoice_id = _pick(row, "invoice_id")
    if not invoice_id:
        raise InvoiceSheetError(
            f"第 {lineno} 行没有发票号 —— 发票号是查出其余一切的钥匙")
    where = f"第 {lineno} 行（发票 {invoice_id}）"
    try:
        line_no_raw = _pick(row, "line_no")
        if not line_no_raw:
            raise InvoiceSheetError(f"没有{CANON['line_no']} —— 三单要按行配对")
        return {
            "lineno": lineno,
            "invoice_id": invoice_id,
            "po_id": _require(row, "po_id", where),
            "line_no": int(_quantity(line_no_raw, what=CANON["line_no"])),
            "sku": _require(row, "sku", where),
            "quantity": _quantity(_pick(row, "quantity"), what=CANON["quantity"]),
            "unit_price": _money(_pick(row, "unit_price"), what=CANON["unit_price"]),
            "issued_at": _iso(_require(row, "issued_at", where)),
            "due_at": _date(_pick(row, "due_at")),
            "tax_rate": _tax_rate(_pick(row, "tax_rate")),
            "note": _pick(row, "note"),
            "raw": {key: _pick(row, key) for key in SHARED_FIELDS},
        }
    except InvoiceSheetError as exc:
        raise InvoiceSheetError(f"{where}：{exc}") from exc


def _require(row: dict, key: str, where: str) -> str:
    value = _pick(row, key)
    if not value:
        raise InvoiceSheetError(f"没有{CANON[key]}")
    return value


def _group(parsed: list[dict]) -> list[dict]:
    """按发票号归并，并把「同一张发票的抬头对不上」当场喊出来。

    不取第一行也不取多数：多数决在两行对两行时无解，而取第一行会让「第 3 行的税率
    抄错了」变成一张税额全错的发票 —— 匹配照样给出结论，理由挂着真实的规则编号。
    """
    out: list[dict] = []
    index: dict[str, dict] = {}
    for row in parsed:
        invoice = index.get(row["invoice_id"])
        if invoice is None:
            invoice = {
                "invoice_id": row["invoice_id"], "po_id": row["po_id"],
                "issued_at": row["issued_at"], "due_at": row["due_at"],
                "tax_rate": row["tax_rate"], "first_line": row["lineno"],
                "raw": row["raw"], "lines": [],
            }
            index[row["invoice_id"]] = invoice
            out.append(invoice)
        else:
            for field in SHARED_FIELDS:
                if invoice[field] != row[field]:
                    raise InvoiceSheetError(
                        f"发票 {row['invoice_id']} 的「{CANON[field]}」在第 "
                        f"{invoice['first_line']} 行是 {invoice['raw'][field]!r}、"
                        f"第 {row['lineno']} 行是 {row['raw'][field]!r} —— "
                        f"同一张发票的这一列必须一致，不取第一行也不取多数")
            if any(line["line_no"] == row["line_no"] for line in invoice["lines"]):
                raise InvoiceSheetError(
                    f"发票 {row['invoice_id']} 的第 {row['line_no']} 行出现了两次"
                    f"（表里第 {row['lineno']} 行）")
        invoice["lines"].append({
            "line_no": row["line_no"], "sku": row["sku"], "quantity": row["quantity"],
            "unit_price": row["unit_price"], "note": row["note"],
        })
    return out


# ------------------------------------------------------------------ 结果表输出
# 显示宽度这一对助手与 `scripts/run_requests.py` 里那一对同形。刻意各留一份：
# 它们是纯排版，不是判定口径，而两个入口的表头与列宽本来就不一样。
def _w(text: str) -> int:
    """显示宽度：中日韩字符占两列。不算这个，表格会歪得没法看。"""
    return sum(2 if ord(c) > 0x2E80 else 1 for c in str(text))


def _pad(text: str, width: int) -> str:
    return str(text) + " " * max(0, width - _w(text))


HEADERS = ("发票号", "供应商", "采购单号", "三单匹配", "应付金额", "付款状态", "转人工")


def as_table(rows: list[dict]) -> str:
    body = [[r["invoice_id"], r["supplier_name"], r["po_id"], r["match_cn"],
             r["amount_cn"], r["status_cn"], str(r["human_exits_count"])] for r in rows]
    widths = [max(_w(h), *(_w(c[i]) for c in body)) for i, h in enumerate(HEADERS)]
    line = "  ".join(_pad(h, w) for h, w in zip(HEADERS, widths)).rstrip()
    out = [line, "-" * _w(line)]
    out += ["  ".join(_pad(c, w) for c, w in zip(cells, widths)).rstrip() for cells in body]
    return "\n".join(out)


def detail_lines(row: dict) -> list[str]:
    """一张发票的明细。**差在哪一行哪个量**就在这里 —— 表格那一列放不下。"""
    out = [f"  · {row['invoice_id']}（{row['supplier_name']} / {row['po_id']} "
           f"v{row['po_version']} / 收货单 {row['gr_id']}，{row['line_count']} 个行项目）"]
    tol = row["tolerance"]
    if row["matched"] is None:
        out.append("      三单匹配：没跑到 —— 收票这一步就没过去")
    elif row["matched"]:
        out.append(f"      三单匹配：通过 —— 跑了 {len(row['checked'])} 条判据，容差 "
                   f"数量 {tol.get('quantity')} 件 / 单价 {tol.get('unit_price')} 元 / "
                   f"税额 {tol.get('tax')} 元")
    else:
        out.append(f"      三单匹配：未通过 —— {len(row['findings'])} 条拒付理由，"
                   f"容差 数量 {tol.get('quantity')} 件 / 单价 {tol.get('unit_price')} 元 / "
                   f"税额 {tol.get('tax')} 元")
        for finding in row["findings"]:
            out.append(f"        - [{finding['rule_id']}] {finding['message']}")

    plan = row["payment_plan"]
    if plan:
        out.append(f"      付款计划：{plan['amount']} {plan['currency']}，方式 "
                   f"{plan['payment_means_code']} {plan['payment_means_name']}，"
                   f"到期 {plan['due_at'] or '未写'}；依据 "
                   f"{'、'.join(row['plan_citations'])}")
        out.append(f"      发票自称：{row['invoice_amount_due']} {row['currency']} —— "
                   f"付出去的钱取我们按规则算出来的那个，不取发票上印的")
    else:
        out.append("      付款计划：未出 —— 未经验证的金额不进审批视野")

    for exit_ in row["human_exits"]:
        out.append(f"      人工停点：{exit_['title']}"
                   f"（effect_risk={exit_.get('effect_risk')}，{exit_['why']}）"
                   f" → 已放行")
    if not row["human_exits"]:
        out.append("      人工停点：没停过 —— 走不到出账那一步")

    advice = row["bank_advice"]
    if advice:
        out.append(f"      银行回单：{advice['observed_state']}，流水号 "
                   f"{advice['bank_reference'] or '无'}，起息 "
                   f"{advice['value_date'] or '无'}，问了 {advice['poll_count']} 次")
    out.append(f"      到账观察：{row['settled_observations']} 条 —— settled 只可能由 "
               f"ap.observe 写入，问不出终态就一个字都不写")
    for failed in row["failed_tasks"]:
        out.append(f"      任务失败：{failed['title']} —— {failed['last_error']}")
    out.append(f"      Plan 终态：{row['plan_state']}")
    return out


def summarize(rows: list[dict]) -> str:
    passed = [r for r in rows if r["matched"]]
    settled = [r for r in rows if r["biz_status"] == "settled"]
    paid = sum(Decimal(r["payable_amount"] or "0") for r in settled)
    humans = sum(r["human_exits_count"] for r in rows)
    return (f"共 {len(rows)} 张发票：三单匹配通过 {len(passed)}、未通过 "
            f"{len(rows) - len(passed)}；已到账 {len(settled)} 张合计 {paid} 元；"
            f"期间 {humans} 次停下来等人放行。")


# ---------------------------------------------------------------------- 跑一遍
def run_sheet(sheet: str | Path, ledger_path: str | Path, *,
              verbose: bool = False) -> list[dict]:
    ledger = load(ledger_path)
    results: list[dict] = []
    for invoice in read_sheet(sheet):
        payload = build_case(ledger, invoice)
        print(f"处理 {invoice['invoice_id']}（{payload['supplier'].get('name')}，"
              f"{len(invoice['lines'])} 行）…")
        row = run_payload(payload, verbose=verbose)
        results.append({
            **row,
            "match_cn": ("—" if row["matched"] is None else
                         "通过" if row["matched"] else
                         f"未通过（{len(row['findings'])} 条）"),
            "amount_cn": (f"{row['payable_amount']} {row['currency']}"
                          if row["payable_amount"] else "—"),
            "status_cn": STATUS_CN.get(row["biz_status"] or "", row["biz_status"] or "—"),
            "human_exits_count": len(row["human_exits"]),
            "notes": payload["notes"],
        })
    return results


def _write_csv(rows: list[dict], out: str) -> None:
    with Path(out).open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(("发票号", "供应商", "采购单号", "收货单", "三单匹配",
                         "拒付理由数", "应付金额", "币种", "付款状态", "银行流水号",
                         "转人工次数", "拒付理由", "说明"))
        for row in rows:
            advice = row["bank_advice"] or {}
            writer.writerow((
                row["invoice_id"], row["supplier_name"], row["po_id"], row["gr_id"],
                row["match_cn"], len(row["findings"]), row["payable_amount"],
                row["currency"], row["status_cn"], advice.get("bank_reference") or "",
                row["human_exits_count"],
                " / ".join(f"[{f['rule_id']}] {f['message']}" for f in row["findings"]),
                " / ".join(row["notes"]),
            ))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_ap", description="按一张发票明细表批量处置供应链付款（无 key、零出网）")
    parser.add_argument("sheet", nargs="?", default=str(DEFAULT_SHEET),
                        help="申请表 CSV 路径")
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER),
                        help="底账 JSON（供应商/采购订单/收货单），"
                             "缺省用 scenarios/custom/ap-ledger.json")
    parser.add_argument("--csv", metavar="OUT", default=None, help="结果表另存成 CSV")
    parser.add_argument("--verbose", action="store_true",
                        help="逐张发票打印 Plan 快照与状态迁移轨迹")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING,
                        format="%(levelname)-5s %(name)-12s %(message)s")

    try:
        rows = run_sheet(args.sheet, args.ledger, verbose=args.verbose)
    except (InvoiceSheetError, ApLedgerError) as exc:
        print(f"表填得不对：{exc}", file=sys.stderr)
        return 2

    print(f"\n{'=' * 78}\n供应链付款处置结果\n{'=' * 78}")
    print(as_table(rows))
    print(f"\n{summarize(rows)}")
    for row in rows:
        for line in detail_lines(row):
            print(line)

    if args.csv:
        _write_csv(rows, args.csv)
        print(f"\n结果表已另存：{args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
