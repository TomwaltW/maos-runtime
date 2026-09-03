"""申请表进群 —— 老板把退款申请表（CSV）拖进群里，机器人**逐行**预检并指出填错的地方。

`scripts/run_requests.py` 已经定义了「老板该以什么格式把退款交给 MAOS」：一张四列
CSV（订单号 / 诉求类型 / 申报金额 / 申请日期）。但那条入口要会用命令行，而且是
**fail-fast** 的 —— 第 3 行填错就停在第 3 行，第 4 行到第 8 行的错要改完再跑一遍
才看得见。群里的人要的不是这个：他把表甩进来，想一次知道**哪几行有问题、各是什么
问题、剩下的几单裁定如何**。本模块补的就是这一段：解析与校验逐行独立、错误收集
而不是抛出、合法的行走与 ``/refund`` 同一条只读预检。

## 与 run_requests 的分工

列名别名、中文诉求映射、日期解析、按订单号补齐 case —— 全部**复用**
`scripts/run_requests.py` 的函数（经 `router._load_run_requests`），不另抄一份。
两套口径的症状是「CSV 里写『坏了』能跑、群里发同一张表不认」，且两边都不报错。
本模块只做它没做的事：逐行收集错误、附带不阻断的提醒、以及群里那份回帖的措辞。

## 认表按内容，不按扩展名

与 `attachments.sniff_mime` 同一取向：这一层的输入来自公网回调，扩展名可以随便改。
判据是「能按文本解码、表头里有订单号那一列」。判不出的一律交回附件白名单去拒 ——
一个改名成 .csv 的 ELF 到不了这里。

## 回帖必须装得进一条消息

Matrix 一条事件上限 64 KB（Synapse 回 413 M_TOO_LARGE），而回帖还要以 ``<pre>`` 再抄
一遍进 formatted_body。发不出去的症状与「机器人挂了」无法分辨 —— 正是这条链路
最贵的那种失败。所以：一张表最多处理 :data:`MAX_ROWS` 行（超出的**说出来**），
回显的每个字段最多 :data:`FIELD_MAX` 个字符（一个 2000 字的「说明」不该原样刷回群里）。

## 只读，不动钱

合法的行只做预检、挂待办，与 ``/refund`` 逐字同一条路：一张表进群不该等于
一批付款。放行仍要审批人逐单 ``/approve <case_id>``。
"""

from __future__ import annotations

import csv
import io
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime

log = logging.getLogger("maos.ingress.sheet")

#: 解码顺序。utf-8-sig 吃掉 Excel 的 BOM；gbk 是中文 Windows 上 Excel「另存为 CSV」的
#: 默认编码 —— 这条入口是给不写代码的人用的，不认 gbk 等于不认他们的 Excel。
ENCODINGS = ("utf-8-sig", "gbk")

#: 一张表最多处理多少行。实测每个合法行的回帖约 400 字节，50 行 ≈ 20 KB，
#: 加上 ``<pre>`` 那份仍在 Matrix 64 KB 事件上限之内；也让一条回帖能一眼读完。
#: 超出的行数**说出来**，不静默截断。
MAX_ROWS = 50

#: 回显字段的字符上限。字段是人填的、会原样刷回群里 —— 一个粘了聊天记录的「说明」
#: 或一个上百 KB 的「订单号」都不该把回帖撑爆。截断只影响回显，不影响校验。
FIELD_MAX = 40

_SNIFF_BYTES = 4096
_LINE_BREAK = re.compile(rb"\r\n|\n|\r")


@dataclass
class SheetRow:
    """表里的一行：要么合法（``req`` 非空、``problems`` 为空），要么说清哪里不对。

    ``warnings`` 与 ``problems`` 分开：前者不阻断（金额超实付会被封顶、多余列被忽略），
    后者阻断（订单不存在、诉求看不懂、日期非法、金额为负）。混成一列的话，
    人分不清「这行还能跑吗」。四个原文字段已按 :data:`FIELD_MAX` 截断，只供回显。
    """

    line: int
    order_id: str
    reason_raw: str = ""
    amount_raw: str = ""
    date_raw: str = ""
    req: dict | None = None
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.req is not None and not self.problems


@dataclass
class Sheet:
    filename: str
    encoding: str
    header: list[str]
    rows: list[SheetRow]
    skipped_blank: int = 0
    #: 超出 MAX_ROWS 没看的行数（**约数**：按剩余物理行估，多行单元格会让它偏大）。
    truncated: int = 0

    @property
    def valid(self) -> list[SheetRow]:
        return [r for r in self.rows if r.ok]

    @property
    def invalid(self) -> list[SheetRow]:
        return [r for r in self.rows if not r.ok]


class NotASheet(ValueError):
    """内容不是申请表。调用方拿到它就交回附件白名单，不在这里说什么。"""


class SheetParseError(ValueError):
    """表认出来了，但 csv 层解析不动（超长单元格、引号没闭合到文件尾之类）。措辞给人看。"""


def _run_requests():
    from maos.ingress.router import _load_run_requests
    return _load_run_requests()


def _clip(text: str, limit: int = FIELD_MAX) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _money(value: float) -> str:
    """金额给人看的写法：两位小数、去掉末尾的 0。``:g`` 会把 999999999 写成 1e+09。"""
    text = f"{value:.2f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def decode(data: bytes) -> tuple[str, str]:
    """按 :data:`ENCODINGS` 顺序解码，返回 ``(文本, 用的编码)``；都不行抛 UnicodeDecodeError。"""
    last: UnicodeDecodeError | None = None
    for enc in ENCODINGS:
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError as exc:
            last = exc
    assert last is not None
    raise last


def looks_like_sheet(data: bytes) -> bool:
    """这份字节是不是一张申请表：能解码成文本，且表头里有订单号那一列。

    只看第一行：判据全在表头，读完整个文件只会让一张大表在这里多花一次解码；
    整段切片还可能切在一个多字节字符中间，把合法的表误判成「解码失败」。
    行尾认 ``\\r\\n`` / ``\\n`` / ``\\r`` 三种 —— Excel for Mac 的「CSV (Macintosh)」
    只用 ``\\r``。二进制（PNG / PDF / ELF）解码就失败，直接 False，交回白名单。
    """
    if not data or b"\x00" in data[:_SNIFF_BYTES]:
        return False
    first_bytes = _LINE_BREAK.split(data[:_SNIFF_BYTES], maxsplit=1)[0]
    try:
        first, _ = decode(first_bytes)
    except UnicodeDecodeError:
        return False
    if not first.strip():
        return False
    try:
        cells = next(csv.reader([first]), [])
    except csv.Error:
        return False
    rr = _run_requests()
    names = {c.strip().lstrip("﻿") for c in cells}
    return any(alias in names for alias in rr.COLUMNS["order_id"])


def _find_order(ledger: dict, order_id: str) -> dict | None:
    orders = [o for o in ledger.get("order_snapshot", []) if o.get("order_id") == order_id]
    return max(orders, key=lambda o: int(o["version"])) if orders else None


def _parse_row(rr, ledger: dict, lineno: int, raw: dict) -> SheetRow:  # noqa: ANN001
    """校验一行，**收集**所有问题而不是停在第一个。

    人改表是一次改完再发，所以一行里的三个错要一次说完。金额与日期都要在
    订单查到之后再比（超实付、早于付款日），所以订单查询放在前面。
    校验用原值，回显用截断值：一个 100 KB 的「订单号」查不到底账是对的结论，
    但不该被原样刷回群里。
    """
    order_id = rr._pick(raw, "order_id")
    row = SheetRow(line=lineno, order_id=_clip(order_id),
                   reason_raw=_clip(rr._pick(raw, "reason")),
                   amount_raw=_clip(rr._pick(raw, "amount").replace(",", "")),
                   date_raw=_clip(rr._pick(raw, "date")))
    extras = raw.get(None)
    if extras:
        row.warnings.append(f"多出 {len(extras)} 列已忽略")

    if not order_id:
        row.problems.append("没有订单号 —— 订单号是查出其余一切的钥匙")
        return row

    order = _find_order(ledger, order_id)
    if order is None:
        row.problems.append(f"底账里没有订单 {row.order_id}")

    reason = ""
    try:
        reason = rr._reason_code(row.reason_raw)
    except rr.RequestSheetError as exc:
        row.problems.append(str(exc))

    amount: float | None = None
    if row.amount_raw:
        try:
            amount = float(row.amount_raw)
        except ValueError:
            row.problems.append(f"金额 {row.amount_raw!r} 不是数字")
        else:
            if not math.isfinite(amount):
                # float() 认 'nan' / 'inf' / '1e400'；nan 跟什么比都是 False，
                # 会穿过下面每一道闸，最后在核算里以 Decimal InvalidOperation 炸掉。
                row.problems.append(f"金额 {row.amount_raw!r} 不是数字")
                amount = None
            elif amount < 0:
                row.problems.append(f"金额 {_money(amount)} 是负数 —— 退款金额不能为负")
            elif amount == 0:
                row.problems.append("金额是 0 —— 想按订单实付退就把这格留空")
            elif order is not None and amount > float(order["amount_paid"]):
                row.warnings.append(
                    f"申报 {_money(amount)} 超过订单实付 {_money(float(order['amount_paid']))}，"
                    "核算时会按实付封顶")

    requested_at = ""
    try:
        requested_at = rr._iso(row.date_raw)
    except rr.RequestSheetError as exc:
        row.problems.append(str(exc))
    else:
        if order is not None and row.date_raw:
            paid_at = str(order.get("paid_at") or "")
            try:
                if datetime.fromisoformat(requested_at) < datetime.fromisoformat(paid_at):
                    row.warnings.append(f"申请日期早于该订单付款日 {paid_at[:10]}")
            except (ValueError, TypeError):
                pass                                  # 底账日期形状不对不是这行的错

    if row.problems:
        return row
    row.req = {"order_id": order_id, "reason": reason, "amount": amount,
               "requested_at": requested_at}
    return row


def _records(text: str):
    """逐条产出 ``(Excel 行号, 记录)``。**按记录计行号，不按物理行。**

    csv 模块对「引号里带换行的单元格」（Excel 里 Alt+Enter）是一条记录跨几个物理行，
    对完全空的行产出 ``[]``。Excel 左边那列数的是**记录**（含空行），所以行号也按
    记录数 —— 用 ``reader.line_num`` 的话，一个多行的「说明」会让它后面的每一行都错位，
    还会被误算成「跳过了 N 个空行」。
    """
    reader = csv.reader(io.StringIO(text, newline=""))
    for rowno, rec in enumerate(reader, start=1):
        yield rowno, rec, reader.line_num


def parse(data: bytes, filename: str, ledger: dict) -> Sheet:
    """把一份 CSV 字节解析成 :class:`Sheet`。不是表就抛 :class:`NotASheet`。

    空行跳过并计数（Excel 常在表尾留几行空的）；同一订单出现多次要提醒 ——
    待办按 case_id 存，后一行会覆盖前一行，人得知道是哪一行算数。
    到 :data:`MAX_ROWS` 就**停**，不再把余下几十万行一条条建成 dict；余量按物理行估。
    """
    if not looks_like_sheet(data):
        raise NotASheet("表头里没有订单号那一列")
    text, encoding = decode(data)
    rr = _run_requests()
    total_lines = text.count("\n") + (0 if text.endswith("\n") or not text else 1)

    header: list[str] = []
    rows: list[SheetRow] = []
    skipped = truncated = 0
    try:
        for rowno, rec, line_num in _records(text):
            if not header:
                if rec and any(c.strip() for c in rec):
                    header = [h.strip().lstrip("﻿") for h in rec]
                continue
            if not rec or not any(c.strip() for c in rec):
                skipped += 1
                continue
            if len(rows) >= MAX_ROWS:
                truncated = max(1, total_lines - line_num + 1)
                break
            raw: dict = dict(zip(header, rec))
            for name in header[len(rec):]:
                raw[name] = None                      # 列少了：与 DictReader 的 restval 一致
            if len(rec) > len(header):
                raw[None] = rec[len(header):]         # 列多了：与 DictReader 的 restkey 一致
            rows.append(_parse_row(rr, ledger, rowno, raw))
    except csv.Error as exc:
        # 超长单元格（> csv.field_size_limit）、引号没闭合到文件尾之类。
        # 不是取件问题，措辞要把人指向表本身。
        raise SheetParseError(f"csv 解析失败：{exc}") from exc

    seen: dict[str, int] = {}
    for row in rows:
        if row.order_id:
            seen[row.order_id] = seen.get(row.order_id, 0) + 1
    for row in rows:
        if row.ok and seen.get(row.order_id, 0) > 1:
            row.warnings.append("同一订单在本表出现多次，待办以最后一行为准")

    return Sheet(filename=_clip(filename), encoding=encoding, header=header, rows=rows,
                 skipped_blank=skipped, truncated=truncated)


def render(sheet: Sheet, verdicts: dict[int, dict], errors: dict[int, str], *,
           decision_cn: dict[str, str]) -> str:
    """群里那份回帖。先说总数，再列有问题的行，再列预检结果，末尾说清没动钱。

    ``verdicts`` 是 ``{行号: preflight 结果}``，``errors`` 是 ``{行号: 预检抛的错}``——
    两者由 router 跑出来（预检那条函数在 router 里，本模块不反向 import 它）。
    """
    total = len(sheet.rows)
    lines = [f"申请表 {sheet.filename or '（未命名）'}：共 {total} 行，"
             f"可预检 {len(sheet.valid)} 行，有问题 {len(sheet.invalid)} 行"]
    if sheet.encoding != ENCODINGS[0]:
        lines.append(f"（按 {sheet.encoding} 解码；建议另存为 UTF-8）")
    if sheet.skipped_blank:
        lines.append(f"（跳过 {sheet.skipped_blank} 个空行）")
    if sheet.truncated:
        lines.append(f"（一次只看前 {MAX_ROWS} 行，其余约 {sheet.truncated} 行未看，请拆表再发）")

    if sheet.invalid:
        lines.append("")
        lines.append("有问题的行（改好后整张表再发一次）：")
        for row in sheet.invalid:
            head = f"  · 第 {row.line} 行 {row.order_id or '（无订单号）'}"
            lines.append(f"{head}：{'；'.join(row.problems)}")

    if verdicts or errors:
        lines.append("")
        lines.append("预检结果（只读，未动任何资金）：")
    for row in sheet.valid:
        if row.line in errors:
            lines.append(f"  · 第 {row.line} 行 {row.order_id}：预检失败 —— {errors[row.line]}")
            continue
        c = verdicts.get(row.line)
        if c is None:
            continue
        decision = decision_cn.get(c["decision"], c["decision"])
        basis = c["deciding_rule"] or "基线裁定（无适用的时限规则）"
        lines.append(f"  · 第 {row.line} 行 {row.order_id}（{row.reason_raw}）：{decision} —— "
                     f"{c['why']}；依据 {basis}，付款至申请 {c['elapsed_days']} 天，"
                     f"申报 {c['amount_claimed']}")
        for w in row.warnings:
            lines.append(f"      提醒：{w}")
        if c["decision"] == "approve":
            lines.append(f"      放行：/approve {c['case_id']}（需 {c['approver_role']}）")
        else:
            lines.append("      不予退款，无需执行")

    if not sheet.valid and not sheet.invalid:
        lines.append("表里一行申请都没有")
    return "\n".join(lines)


def summary(sheet: Sheet, verdicts: dict[int, dict], errors: dict[int, str]) -> str:
    """一行摘要，给闲聊回话的【事实】用 —— 让模型知道刚才那张表长什么样。"""
    approve = sum(1 for c in verdicts.values() if c.get("decision") == "approve")
    return (f"{sheet.filename or '（未命名）'}：{len(sheet.rows)} 行，"
            f"{len(sheet.invalid)} 行填错，{len(verdicts)} 行预检完成"
            f"（{approve} 行裁定批准），{len(errors)} 行预检失败")
