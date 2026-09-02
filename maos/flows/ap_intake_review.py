"""发票抽取结果 → **待复核表** —— 图片进付款链路之前的那道人工闸门。

    python3 scripts/review_invoices.py <图片目录> --ledger <底账> --out 待复核.csv

## 为什么要有这一层（跨轨契约 R1）

视觉模型看错一个小数点 = 多付一笔钱。所以抽取结果**不许**直接变成付款申请：
先落成待复核表，人逐行核对完把 `需人工确认` 改成「否」，才允许喂给正式入口。

本模块因此有一条硬规则：**`需人工确认` 恒写「是」**。置信度再高也一样 ——
置信度只决定**怎么排**（低置信排前面，让人先看最可能错的那几行），
不决定放不放行。把闸门交给模型自己开，等于没有闸门。

这与自然语言层的 R1、与铁律 8 同源：**理解可以错，授权不许错。**

## 与底账核对：对不上就说对不上，不猜

抽出来的采购单号拿去底账里**精确**查，四种结果各有各的写法：

  · 查得到      -> 把订单侧数量单价并进 `说明`，人一眼能对照；
  · 查不到      -> 标「底账无此采购单」，**不做模糊匹配**；
  · 抽出来是空  -> 标「未抽到」，单元格留空，**不填默认值**；
  · 底账文件不在 -> `LedgerError` 报错退出，说清楚要哪个文件，不静默跳过核对。

`PO-2026-0001` 和 `PO-2026-000l` 是两张单子。猜「最像的那个」就是把钱付给
另一个供应商，而且不会报错 —— 这类 bug 最贵。

## 列定义

待复核表 = 申请表的十列（契约 §1.2）**多两列**：`来源`（图片路径，取证用）、
`需人工确认`（是/否）。核对结论与低置信提示写进 `说明` ——
抽取结果里本来就没有 `说明` 这一项（契约 §1.3），那一列空着，正好给人看的话用。
不另开新列是为了让人改完「是」→「否」之后，同一张表能直接喂给付款入口。
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

#: 申请表列（契约 §1.2），顺序即 CSV 列序。
INVOICE_COLUMNS: tuple[str, ...] = (
    "发票号", "采购单号", "行号", "SKU", "发票数量", "发票单价",
    "开票日期", "到期日", "税率", "说明")

#: 待复核表列（契约 §1.4）= 上面十列 + 两列。
REVIEW_COLUMNS: tuple[str, ...] = INVOICE_COLUMNS + ("来源", "需人工确认")

#: 🔴 R1：本模块只写得出「是」。「否」是**人**核对之后自己改的，
#: 代码里没有任何一条路径写得出它 —— 有了就是给模型开了后门。
NEEDS_REVIEW = "是"

#: 抽取结果的表头字段（契约 §1.3 的 `fields`）→ 待复核表列名（同名直取）。
HEAD_FIELDS: tuple[str, ...] = ("发票号", "采购单号", "开票日期", "到期日", "税率")

#: 抽取结果的行字段（契约 §1.3 的 `lines`）→ 待复核表列名。
LINE_FIELDS: dict[str, str] = {"行号": "行号", "SKU": "SKU", "数量": "发票数量", "单价": "发票单价"}

#: 核对结论。写进 `说明`，也留在 `ReviewRow.ledger_status` 上供摘要统计。
LEDGER_OK = "matched"
LEDGER_PO_BLANK = "po_blank"          # 采购单号没抽到
LEDGER_PO_MISSING = "po_missing"      # 底账里没有这张采购单
LEDGER_LINE_MISSING = "line_missing"  # 采购单在，但没有这一行

#: 说明列里各种标记的固定措辞。措辞被测试钉住 —— 人是照这几个词筛行的。
MARK_NOT_EXTRACTED = "未抽到"
MARK_PO_MISSING = "底账无此采购单"
MARK_LINE_MISSING = "底账该采购单无此行号"
MARK_LOW = "低置信"

#: 置信度只有这两档（契约 §1.3：模型说不准就是 low）。
CONF_HIGH = "high"
CONF_LOW = "low"


class ReviewInputError(ValueError):
    """抽取结果不合形状。消息直接给人看，不用翻栈。"""


class LedgerError(ValueError):
    """底账读不出来。消息里必须写清楚**要哪个文件** —— 不许静默跳过核对。"""


# INTEGRATION-POINT: 整合时换成 maos.tools.invoice_extract.extract_invoice
# （T71 的产物；并行期它在本 worktree 里根本不存在，import 即红）。
# 下面这个结构照跨轨契约 §1.3 逐字段等价定义，整合时删掉本类、改用 T71 的即可。
@dataclass(frozen=True)
class ExtractedInvoice:
    """一张发票的抽取结果。内存结构，不落库。

    `confidence` 逐字段 `"high" | "low"`。**查不到的字段按 low 记** ——
    抽取器没表态时宁可让人多看一眼，也不假设它有把握。
    行级字段想逐行区分置信度时，键写成 `<行号>.<字段>`（如 `2.单价`），
    没有这个键就回落到字段名本身。
    """

    source: str
    fields: dict = field(default_factory=dict)
    lines: list[dict] = field(default_factory=list)
    confidence: dict = field(default_factory=dict)
    raw: str = ""


@dataclass(frozen=True)
class ReviewRow:
    """待复核表的一行。`cells` 就是 CSV 的十二列，别的字段只给摘要用。"""

    cells: dict[str, str]
    source: str
    low_fields: tuple[str, ...]
    ledger_status: str

    @property
    def needs_review(self) -> str:
        return self.cells["需人工确认"]


# --------------------------------------------------------------------- 读底账
def load_ledger(path: str | Path) -> dict:
    """读底账（契约 §1.1）。读不出来当场报错，**不许**降级成「跳过核对」。

    跳过核对跑出来的待复核表，看上去和核对过的一模一样 —— 人照着它签字，
    等于这一批发票根本没和外部事实对过账。宁可让人补一个文件路径。
    """
    p = Path(path)
    if not p.exists():
        raise LedgerError(
            f"找不到底账文件：{p}。核对采购单号要靠它，没有它这一步只能跳过，"
            "而跳过核对的待复核表和核对过的长得一样 —— 所以这里直接停。"
            "请用 --ledger 指到 ap-ledger.json"
            "（三段：suppliers / purchase_orders / goods_receipts）")
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LedgerError(f"{p} 不是合法 JSON：{exc}") from exc
    if not isinstance(payload, dict):
        raise LedgerError(
            f"{p} 的顶层必须是对象（三段：suppliers / purchase_orders / goods_receipts）")
    if not isinstance(payload.get("purchase_orders"), list):
        raise LedgerError(f"{p} 里没有 purchase_orders 段 —— 采购单号无处可查，核对无从谈起")
    return payload


def index_purchase_orders(ledger: dict) -> dict[str, dict]:
    """底账里的采购单，按 `po_id` 建索引；同号多版本取版本号最大的那一版。

    键是**原样**的 po_id，查的时候也**原样**查：不 strip 大小写、不去横杠、
    不做任何编辑距离。`PO-2026-0001` 与 `PO-2026-000l` 在这里就是两个键。
    """
    out: dict[str, dict] = {}
    for po in ledger.get("purchase_orders") or []:
        if not isinstance(po, dict):
            continue
        po_id = str(po.get("po_id") or "")
        if not po_id:
            continue
        seen = out.get(po_id)
        if seen is None or int(po.get("version") or 1) >= int(seen.get("version") or 1):
            out[po_id] = po
    return out


# ------------------------------------------------------------------- 组装每行
def _cell(value) -> str:
    """单元格取值。空 / None 一律落成空串 —— **不填默认值**。"""
    if value is None:
        return ""
    return str(value).strip()


def _conf(inv: ExtractedInvoice, name: str, *, line_no: str = "") -> str:
    """某个字段的置信度。查不到按 low 记（见 `ExtractedInvoice` 的说明）。"""
    conf = inv.confidence if isinstance(inv.confidence, dict) else {}
    if line_no:
        keyed = conf.get(f"{line_no}.{name}")
        if keyed is not None:
            return str(keyed).strip().lower()
    return str(conf.get(name, CONF_LOW)).strip().lower()


def _po_line(po: dict, line_no: str) -> dict | None:
    """采购单里行号对得上的那一行。行号按字符串比，同样不做近似匹配。"""
    for line in po.get("lines") or []:
        if isinstance(line, dict) and _cell(line.get("line_no")) == line_no:
            return line
    return None


def _ledger_note(po_id: str, line_no: str, pos: dict[str, dict]) -> tuple[str, str]:
    """核对一行，返回 `(说明里的一句话, ledger_status)`。"""
    if not po_id:
        return ("", LEDGER_PO_BLANK)          # 空值的提示由「未抽到」那条统一给
    po = pos.get(po_id)                       # 精确查，查不到就是查不到
    if po is None:
        return (f"{MARK_PO_MISSING}：{po_id}", LEDGER_PO_MISSING)
    if not line_no:
        return (f"底账有此采购单（供应商 {_cell(po.get('supplier_id')) or '未填'}）", LEDGER_OK)
    line = _po_line(po, line_no)
    if line is None:
        return (f"{MARK_LINE_MISSING} {line_no}", LEDGER_LINE_MISSING)
    return (f"底账：订单数量 {_cell(line.get('quantity'))}、"
            f"订单单价 {_cell(line.get('unit_price'))}、SKU {_cell(line.get('sku'))}", LEDGER_OK)


def _row_of(inv: ExtractedInvoice, line: dict | None, pos: dict[str, dict]) -> ReviewRow:
    cells = {col: "" for col in REVIEW_COLUMNS}
    low: list[str] = []
    blank: list[str] = []

    for name in HEAD_FIELDS:
        value = _cell((inv.fields or {}).get(name))
        cells[name] = value
        if not value:
            blank.append(name)
        elif _conf(inv, name) != CONF_HIGH:
            low.append(name)

    line_no = _cell((line or {}).get("行号"))
    for src, col in LINE_FIELDS.items():
        if line is None:
            blank.append(col)
            continue
        value = _cell(line.get(src))
        cells[col] = value
        if not value:
            blank.append(col)
        elif _conf(inv, src, line_no=line_no) != CONF_HIGH:
            low.append(col)

    note, status = _ledger_note(cells["采购单号"], line_no, pos)
    notes: list[str] = []
    if line is None:
        notes.append(f"{MARK_NOT_EXTRACTED}：行项目")
    if blank:
        notes.append(f"{MARK_NOT_EXTRACTED}：" + "、".join(dict.fromkeys(blank)))
    if note:
        notes.append(note)
    if low:
        notes.append(f"{MARK_LOW}：" + "、".join(low))

    cells["说明"] = "；".join(notes)
    cells["来源"] = _cell(inv.source)
    cells["需人工确认"] = NEEDS_REVIEW          # 🔴 R1：只此一处赋值，恒为「是」
    return ReviewRow(cells=cells, source=cells["来源"],
                     low_fields=tuple(low), ledger_status=status)


def build_review(extracted: Sequence[ExtractedInvoice], ledger: dict) -> list[ReviewRow]:
    """一批抽取结果 + 底账 -> 待复核表的行（低置信排前面）。

    排序**只**影响人先看谁，不影响放不放行：每行的 `需人工确认` 都是「是」。
    同样低置信度的行保持抽取顺序（`sorted` 稳定），两次跑输出一致。
    """
    pos = index_purchase_orders(ledger)
    rows: list[ReviewRow] = []
    for inv in extracted:
        if not isinstance(inv, ExtractedInvoice):
            raise ReviewInputError(f"抽取结果必须是 ExtractedInvoice，收到 {type(inv).__name__}")
        for line in (inv.lines or [None]):
            if line is not None and not isinstance(line, dict):
                raise ReviewInputError(
                    f"{inv.source}：行项目必须是 dict，收到 {type(line).__name__}")
            rows.append(_row_of(inv, line, pos))
    return sorted(rows, key=lambda r: -len(r.low_fields))


# ----------------------------------------------------------------- 落表与摘要
def write_review_csv(rows: Iterable[ReviewRow], path: str | Path) -> Path:
    """写待复核表。`utf-8-sig` 是为了 Excel 双击打开不乱码。"""
    p = Path(path)
    if p.parent and not p.parent.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(REVIEW_COLUMNS)
        for row in rows:
            writer.writerow([row.cells[col] for col in REVIEW_COLUMNS])
    return p


def low_confidence_fields(rows: Sequence[ReviewRow]) -> list[tuple[str, int]]:
    """低置信字段清点，按出现次数从多到少 —— 摘要里那句「低置信字段有哪些」。"""
    tally: dict[str, int] = {}
    for row in rows:
        for name in row.low_fields:
            tally[name] = tally.get(name, 0) + 1
    return sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))


def summarize(rows: Sequence[ReviewRow], *, images: Sequence[str],
              extracted: Sequence[ExtractedInvoice], out: str | Path) -> str:
    """跑完给人看的中文摘要。**最后一句必须是下一步该干什么** —— 这道闸门
    只有在人真的去改那一列时才成立，所以话要说到「改哪一列、改完喂给谁」。
    """
    bad = [r for r in rows if r.ledger_status != LEDGER_OK]
    po_missing = sum(1 for r in rows if r.ledger_status == LEDGER_PO_MISSING)
    line_missing = sum(1 for r in rows if r.ledger_status == LEDGER_LINE_MISSING)
    po_blank = sum(1 for r in rows if r.ledger_status == LEDGER_PO_BLANK)
    low = low_confidence_fields(rows)
    lines = [
        f"共 {len(images)} 张图，抽到 {len(extracted)} 张、展开成 {len(rows)} 个待复核行项目。",
        (f"与底账对不上 {len(bad)} 行："
         f"底账无此采购单 {po_missing} 行、采购单有但无此行号 {line_missing} 行、"
         f"采购单号没抽到 {po_blank} 行。对不上的一律照原样留着，没有做任何模糊匹配。"),
        ("低置信字段：" + "、".join(f"{n}（{c} 行）" for n, c in low)) if low
        else "低置信字段：无（但这不改变下面那句 —— 置信度不是放行理由）。",
        f"全部 {len(rows)} 行的『需人工确认』都是「是」，一行都没有自动放行。",
        "",
        (f"下一步：用 Excel 打开 {out} 逐行核对，把每一行的『需人工确认』由「是」改成「否」，"
         "改完再喂给付款入口 —— 只要还留着「是」，付款入口就会拒收那一行。"),
    ]
    return "\n".join(lines)
