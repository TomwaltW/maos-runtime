#!/usr/bin/env python3
"""把一叠**发票图片**变成一张待复核表 —— 这是给财务（不写代码的人）的入口。

    python3 scripts/review_invoices.py <图片目录或图片> --ledger <底账> --out 待复核.csv

## 它做什么、不做什么

**做**：逐张抽字段 -> 拿采购单号去底账里精确核对 -> 低置信的排前面 ->
落成一张 Excel 能直接打开的待复核表。

**不做**：不发起任何付款，也不替人判断哪一行「看着没问题」。
表里每一行的 `需人工确认` 都是「是」，一行都没有自动放行 ——
视觉模型看错一个小数点就是多付一笔钱，闸门必须握在人手里（跨轨契约 R1）。

## 抽取器还没接上时怎么跑

图片 -> 字段那一步归 `maos/tools/invoice_extract.py`（T71 的产物）。它还没并进来
之前，`--extracted <json>` 直接喂一份已抽好的结果（形状照契约 §1.3），
核对、排序、落表、摘要这条链路照样整条跑得通，也照样零出网。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from maos.flows.ap_intake_review import (  # noqa: E402
    ExtractedInvoice, LedgerError, ReviewInputError,
    build_review, load_ledger, summarize, write_review_csv)

#: 认这些后缀。列目录时排掉 Excel 临时文件之类的东西。
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".pdf")

DEFAULT_OUT = "待复核.csv"


class ExtractorNotWired(RuntimeError):
    """抽取器还没接上。消息直接给人看，并告诉他现在能怎么跑。"""


def collect_images(target: str | Path) -> list[Path]:
    """列出要抽的图。目录就递归找，单文件就是它自己。排序固定，两次跑一致。"""
    p = Path(target)
    if not p.exists():
        raise ReviewInputError(f"找不到图片路径：{p}")
    if p.is_file():
        return [p]
    found = sorted(f for f in p.rglob("*") if f.is_file() and f.suffix.lower() in IMAGE_SUFFIXES)
    if not found:
        raise ReviewInputError(
            f"{p} 下面一张图都没有（认这些后缀：{'、'.join(IMAGE_SUFFIXES)}）")
    return found


def extract_images(images: list[Path]) -> list[ExtractedInvoice]:
    """图片 -> 抽取结果。

    这一步归 T71，本轨不实现、也不 import 它（并行期它在本 worktree 里不存在，
    import 即红）。接上之前老老实实报错，**不许**返回空列表装作抽过了 ——
    空表跑出来的摘要会写「抽到 0 张」，看上去像「这批图没内容」，
    而真相是这一步压根没跑。
    """
    # INTEGRATION-POINT: 整合时换成 maos.tools.invoice_extract.extract_invoice
    # 逐张调用，返回 list[ExtractedInvoice]（契约 §1.3 的结构）。
    raise ExtractorNotWired(
        f"抽取器还没接上（maos/tools/invoice_extract.py 尚未并入），{len(images)} 张图抽不了。"
        "现在可以用 --extracted <json> 喂一份已抽好的结果（形状照跨轨契约 §1.3），"
        "核对与落表这条链路照样整条跑得通。")


def load_extracted(path: str | Path) -> list[ExtractedInvoice]:
    """读一份已抽好的结果（形状照契约 §1.3）。缺字段当场报错，不补默认值。"""
    p = Path(path)
    if not p.exists():
        raise ReviewInputError(f"找不到抽取结果文件：{p}")
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ReviewInputError(f"{p} 不是合法 JSON：{exc}") from exc
    if not isinstance(payload, list):
        raise ReviewInputError(f"{p} 的顶层必须是数组，一个元素 = 一张发票的抽取结果")

    out: list[ExtractedInvoice] = []
    for i, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            raise ReviewInputError(f"{p} 第 {i} 个元素不是对象")
        source = str(item.get("source") or "").strip()
        if not source:
            raise ReviewInputError(f"{p} 第 {i} 个元素没有 source —— 那是取证用的图片路径，不能空")
        out.append(ExtractedInvoice(
            source=source,
            fields=item.get("fields") or {},
            lines=list(item.get("lines") or []),
            confidence=item.get("confidence") or {},
            raw=str(item.get("raw") or "")))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="review_invoices",
        description="发票图片 -> 待复核表（人核对完才准进付款入口）")
    parser.add_argument("target", nargs="?", default=None, help="图片目录或单张图片")
    parser.add_argument("--ledger", required=True,
                        help="底账 JSON（suppliers / purchase_orders / goods_receipts）")
    parser.add_argument("--out", default=DEFAULT_OUT, help=f"待复核表落到哪，缺省 {DEFAULT_OUT}")
    parser.add_argument("--extracted", default=None,
                        help="跳过抽取，直接读一份已抽好的结果 JSON（形状照跨轨契约 §1.3）")
    args = parser.parse_args(argv)

    if not args.target and not args.extracted:
        parser.error("要么给图片目录，要么给 --extracted <json>，总得有一份输入")

    try:
        # 顺序有讲究：底账**先**读。抽取是这条链路里最贵的一步（真接上之后逐张
        # 打视觉模型），底账路径写错却要等抽完才报错，那笔钱就白花了。
        ledger = load_ledger(args.ledger)
        images = [str(p) for p in collect_images(args.target)] if args.target else []
        if args.extracted:
            extracted = load_extracted(args.extracted)
            if not images:
                images = [inv.source for inv in extracted]
        else:
            extracted = extract_images([Path(p) for p in images])
        rows = build_review(extracted, ledger)
        out = write_review_csv(rows, args.out)
    except (LedgerError, ReviewInputError) as exc:
        print(f"输入不对：{exc}", file=sys.stderr)
        return 2
    except ExtractorNotWired as exc:
        print(f"跑不了：{exc}", file=sys.stderr)
        return 3

    print(f"\n{'=' * 78}\n发票抽取 · 待复核表\n{'=' * 78}")
    print(summarize(rows, images=images, extracted=extracted, out=out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
