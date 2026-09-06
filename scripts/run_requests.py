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
from maos.domain.refund.annotation import needs_human  # noqa: E402
from maos.skills.builtin.refund.reason_classify import LEXICON, UNKNOWN  # noqa: E402
from maos.skills.builtin.sheet_header_map import COLUMNS as SHEET_COLUMNS  # noqa: E402

DEFAULT_LEDGER = Path(__file__).resolve().parents[1] / "scenarios" / "custom" / "ledger.json"

#: 老板会写的说法 -> 系统里的诉求类型。**权威定义在 skill 里**，这里只取过来用：
#: 两份词表并存的症状是它们会各自长大，然后同一个词在命令行与房间里判成两个 code。
#: 词表未命中时不再当场报错 —— 交给 `refund.reason_classify` 的模型兜底（见 `_reason_code`）。
REASONS: dict[str, str] = LEXICON

#: 表头别名。CSV 是人手填的，列名叫法不会统一。**全等**匹配（见 `_pick`）：
#: 「退货原因」不会因为含「原因」二字就命中「原因」那条，差一个字就是整列取不到。
#: 同样只取 skill 里那份权威定义；全等认不出的列由 `sheet.header_map` 兜底。
COLUMNS: dict[str, tuple[str, ...]] = SHEET_COLUMNS

DECISION_CN = {"approve": "批准", "reject": "驳回"}
STATUS_CN = {
    "settled": "已到账", "gateway_accepted": "已提交网关·未确认", "processing": "网关处理中",
    "submitted": "未发起退款", "approved": "已批准·未发起", "compensated": "已补偿",
    "rejected": "已驳回",
}
DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M", "%Y.%m.%d")


class RequestSheetError(ValueError):
    """申请表里有填不对的地方。消息直接给人看。"""


#: 必需列：``key -> 给人看的标准列名``。整列一个别名都没命中时，这一列每行都取到
#: 空串，逐行报「不能空」等于把表头的一处错说成 N 行数据的错 —— 人会照着去改那 N 行，
#: 而那 N 行本来就是填好的。订单号不列在这里：它另有一句更准的话（见 `read_sheet`）。
REQUIRED_COLUMNS: dict[str, str] = {"reason": "诉求类型"}


def _norm_header(text: str) -> str:
    """表头归一：strip + 去 BOM。判据只此一处，`scan_header` 与 `_pick` 共用。"""
    return str(text).strip().lstrip("﻿")


def _pick(row: dict, key: str, mapping: dict | None = None) -> str:
    """取一格。

    `mapping` 是 `sheet.header_map` 补认出来的 `{字段: 表头原文}`，**优先于别名表**：
    它只在别名那一路已经失败过时才存在，这时再让别名先试一遍只是空转。
    不给 mapping 时行为与接线前逐字相同（房间那条入口就是这么调的）。
    """
    if mapping and key in mapping:
        target = _norm_header(mapping[key])
        for raw_key, value in row.items():
            if raw_key and _norm_header(raw_key) == target:
                return (value or "").strip()
    for name in COLUMNS[key]:
        for raw_key, value in row.items():
            if raw_key and _norm_header(raw_key) == name:
                return (value or "").strip()
    return ""


def scan_header(header) -> tuple[list[str], list[str]]:  # noqa: ANN001
    """看表头认出了什么：返回 ``(缺的必需列 key, 没认出来的列名)``。

    判据与 `_pick` 同一套（strip + 去 BOM 后**全等**）—— 两边不一致的话，症状是
    「说缺了这一列，可行里又取到了值」，比不报还难查。申请表进群那条入口
    （`maos.ingress.sheet`）复用本函数，不另抄一份。
    """
    names = {h.strip().lstrip("﻿") for h in header if h}
    known = {alias for aliases in COLUMNS.values() for alias in aliases}
    missing = [key for key in REQUIRED_COLUMNS if not (set(COLUMNS[key]) & names)]
    return missing, [h.strip().lstrip("﻿") for h in header
                     if h and h.strip().lstrip("﻿") not in known]


def missing_column_message(key: str) -> str:
    """整列缺失时那句话。两条入口逐字同一句，改措辞只改这里。"""
    return f"表头里没有「{REQUIRED_COLUMNS[key]}」这一列"


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


def make_classifier():
    """词表/别名认不出时的模型兜底。**拿不到真模型就返回 None，这不是故障。**

    只在三个环境变量齐备、`select_model_client()` 真给出 `GatewayModelClient`
    时才接线。理由是 `ScriptedModelClient.complete()` 恒返 `"{}"`，喂给 skill 只会
    抛 ValueError —— 而本脚本对外的承诺是「无 key、零出网」，不能因为接了模型就
    在没配 key 的机器上开始报错。没接上的后果是词表外的词落 `unknown` 挑去人工，
    其余行照常跑完，这正是「判不准就别猜」该有的结果。

    `store` 传 None：本脚本每单一个 `:memory:` 库，成本账落进去跑完就没了。
    `record_model_usage` 见 None 直接跳过（`core/store.py:572`），不抛。
    """
    from maos.agents.refund.intake_agent import RefundIntakeAgent
    from maos.model.client import GatewayModelClient, select_model_client
    from maos.skills.invoker import SkillInvoker

    model = select_model_client()
    if not isinstance(model, GatewayModelClient):
        return None
    identity = RefundIntakeAgent.identity
    invoker = SkillInvoker(identity, None)
    extras = {"model": model, "tier": identity.model_tier}

    def call(name: str, payload: dict) -> dict | None:
        """调一次 skill。**任何失败都返回 None**，由调用方落回 fallback。

        模型挂了、超时了、输出不合契约（invoker 已按 failure_policy 重试过一次），
        都不该让一张表读不下去 —— 那一列的结果是「这单要人看」，本来就是安全出口。
        """
        try:
            res = invoker.invoke(name, payload, extras=dict(extras))
        except Exception as exc:                       # noqa: BLE001
            logging.getLogger("run_requests").warning("%s 调用失败：%s", name, exc)
            return None
        if res.status != "ok" or not isinstance(res.output, dict):
            logging.getLogger("run_requests").warning("%s 未产出结果：%s", name, res.error)
            return None
        return res.output

    return call


def classify_reason(raw: str, classifier=None) -> dict:  # noqa: ANN001
    """判诉求类型。返回 ``{reason, source, confidence, why, needs_human}``。

    三段，顺序不能反：词表命中直接用（**一次模型都不调**）；认不出才问模型；
    没模型就落 `unknown`。

    **判不出来不抛**：那是「这一单要人看」，不是「这张表填错了」，两者的处置
    完全相反 —— 前者其余行照跑，后者才该让人回去改表。空的诉求类型仍然抛，
    它确实是填错了（一格都没填，模型也无从判起）。
    """
    text = raw.strip()
    if not text:
        raise RequestSheetError("诉求类型不能空 —— 不知道为什么退，就套不上任何一条政策")

    if text in REASONS:
        return _verdict(REASONS[text], 1.0, "词表直接命中", "lexicon")
    if text in set(REASONS.values()):
        return _verdict(text, 1.0, "原文就是一个合法 code", "lexicon")

    if classifier is None:
        return _verdict(UNKNOWN, 0.0, "词表未命中，且没有可用的模型（未配 key）", "fallback")

    out = classifier("refund.reason_classify", {"text": text})
    if out is None:
        return _verdict(UNKNOWN, 0.0, "词表未命中，模型调用没有产出结果", "fallback")
    return _verdict(str(out.get("reason_code") or UNKNOWN),
                    float(out.get("confidence") or 0.0),
                    str(out.get("why") or ""), str(out.get("source") or "model"))


def _verdict(code: str, confidence: float, why: str, source: str) -> dict:
    """一条诉求类型判据。`needs_human` 走 `annotation.needs_human` —— **整仓唯一定义**，
    这里不自己比阈值，否则命令行与房间会各有一套「算不算低置信度」。"""
    return {"reason": code, "source": source, "confidence": confidence, "why": why,
            "needs_human": needs_human(confidence, source, code)}


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


def read_sheet(path: str | Path, *, classifier=None) -> list[dict]:  # noqa: ANN001
    """读申请表。每行返回 `{order_id, reason, amount, requested_at, note}` 加一组判据。

    `classifier` 是词表认不出时的模型兜底（`make_classifier()`），不给就只走词表 ——
    认不出的行不再让整张表读不下去，而是带着 `needs_human_intake=True` 返回，
    由 `run_sheet` 挑出来不送进流程。**判不准与填错是两回事**：前者其余行照跑，
    后者（没订单号、金额不是数字、日期看不懂）仍然当场抛。
    """
    p = Path(path)
    if not p.exists():
        raise RequestSheetError(f"找不到申请表：{p}")
    with p.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
        header = list(reader.fieldnames or ())
    if not rows:
        raise RequestSheetError(f"{p} 里一行申请都没有")

    # 表头先判：整列缺失时下面每一行都会在诉求类型那一格报「不能空」，那句把人
    # 指向一堆本来没填错的行。错在表头一处，就在表头一处说。
    missing, unknown = scan_header(header)
    # 别名全等认不出来的列交给模型补认（`退货原因（必填）` 这类）。补上了就不再是
    # 「表头缺列」——但**只补别名没认出来的那几列**，已经认出来的不给模型碰。
    header_map: dict = {}
    if missing and classifier is not None:
        out = classifier("sheet.header_map", {"header": list(header)})
        if out:
            header_map = dict(out.get("mapping") or {})
            missing = [key for key in missing if key not in header_map]
    for key in missing:
        aliases = "、".join(a for a in COLUMNS[key] if not a.isascii())
        note = f"；表里没认出来的列：{'、'.join(unknown)}" if unknown else ""
        raise RequestSheetError(
            f"{missing_column_message(key)} —— 这一列写成这些名字都认：{aliases}{note}")

    out: list[dict] = []
    for lineno, row in enumerate(rows, start=2):     # 第 1 行是表头
        order_id = _pick(row, "order_id", header_map)
        if not order_id:
            raise RequestSheetError(f"第 {lineno} 行没有订单号 —— 订单号是查出其余一切的钥匙")
        amount = _pick(row, "amount", header_map).replace(",", "")
        try:
            verdict = classify_reason(_pick(row, "reason", header_map), classifier)
            out.append({
                "line": lineno, "order_id": order_id,
                "reason_raw": _pick(row, "reason", header_map),
                "reason": verdict["reason"],
                "reason_source": verdict["source"],
                "reason_confidence": verdict["confidence"],
                "reason_why": verdict["why"],
                "needs_human_intake": verdict["needs_human"],
                "amount": float(amount) if amount else None,
                "requested_at": _iso(_pick(row, "date", header_map)),
                "note": _pick(row, "note", header_map),
            })
        except RequestSheetError as exc:
            raise RequestSheetError(f"第 {lineno} 行（订单 {order_id}）：{exc}") from exc
        except ValueError as exc:
            raise RequestSheetError(
                f"第 {lineno} 行（订单 {order_id}）金额 {amount!r} 不是数字：{exc}") from exc
    return out


def reason_basis(req: dict) -> str:
    """诉求类型这一格是怎么定下来的，给结果表用。

    词表命中不写把握度 —— 它恒等于 1.0，印出来只会让人以为那也是模型估的。
    """
    source = req.get("reason_source") or "lexicon"
    if source in ("lexicon", "alias"):
        return "词表"
    if source == "model":
        return f"模型 {float(req.get('reason_confidence') or 0.0):.2f}"
    return "待人工"


def pending_row(req: dict) -> dict:
    """判不准的那一单在结果表里的样子：**每一格都写"未处置"，不写 0**。

    金额写 0.00 会被当成「算过了，就是零元」；裁定留空会被当成「还没跑完」。
    这一单的真相是「机器没敢判，等人看」，表里就该逐格这么说。
    """
    return {
        "order_id": req["order_id"], "reason_raw": req["reason_raw"],
        "reason": req["reason"], "note": req["note"],
        "reason_basis": reason_basis(req),
        "decision": "pending", "decision_cn": "待人工",
        "amount_claimed": "—", "amount_approved": "—",
        "status": "pending_intake", "status_cn": "未受理",
        "basis": "诉求类型判不准，未套用任何政策",
        "why": req["reason_why"] or "词表与模型都没能判出诉求类型",
        "human_exits": 0, "plan_state": "—",
        "needs_human_intake": True,
    }


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


HEADERS = ("订单号", "诉求", "判据", "裁定", "核准金额", "退款状态", "依据", "转人工")


def as_table(rows: list[dict]) -> str:
    body = [[r["order_id"], r["reason_raw"] or r["reason"], r.get("reason_basis", "词表"),
             r["decision_cn"], r["amount_approved"], r["status_cn"], r["basis"],
             str(r["human_exits"])]
            for r in rows]
    widths = [max(_w(h), *(_w(c[i]) for c in body)) for i, h in enumerate(HEADERS)]
    line = "  ".join(_pad(h, w) for h, w in zip(HEADERS, widths)).rstrip()
    out = [line, "-" * _w(line)]
    out += ["  ".join(_pad(c, w) for c, w in zip(cells, widths)).rstrip() for cells in body]
    return "\n".join(out)


def summarize(rows: list[dict]) -> str:
    """收口行。**没有待人工的单子时逐字不变** —— 这是回归基线断言的那一行，
    多一句少一句都会被当成行为变了。"""
    pending = [r for r in rows if r.get("needs_human_intake")]
    handled = [r for r in rows if not r.get("needs_human_intake")]
    ok = [r for r in handled if r["decision"] == "approve"]
    paid = sum(float(r["amount_approved"]) for r in handled if r["status"] == "settled")
    settled = sum(1 for r in handled if r["status"] == "settled")
    humans = sum(r["human_exits"] for r in handled)
    line = (f"共 {len(handled)} 单：批准 {len(ok)}、驳回 {len(handled) - len(ok)}；"
            f"已到账 {settled} 单合计 {paid:.2f} 元；期间 {humans} 次停下来等人放行。")
    if pending:
        line += (f"\n另有 {len(pending)} 单诉求类型判不准，已挑出等人工确认，未进入处置："
                 f"{'、'.join(r['order_id'] for r in pending)}。")
    return line


def run_sheet(sheet: str | Path, ledger_path: str | Path, *, approve: bool = True,
              matrix: bool = False, allow_degraded: bool = False) -> list[dict]:
    ledger = load(ledger_path, require_case=False)
    # 没配 key 时这里是 None，词表外的诉求一律落 unknown 挑去人工（见 make_classifier）。
    classifier = make_classifier()
    results: list[dict] = []
    for req in read_sheet(sheet, classifier=classifier):
        if req["needs_human_intake"]:
            # 不送进流程：`unknown` 套不上任何一条政策，硬跑会走基线裁定
            # 直接批准 —— 一个判不出诉求类型的单子被自动批款，是这里最坏的失败。
            print(f"挑出 {req['order_id']}（{req['reason_raw']}）—— 诉求类型判不准，等人工")
            results.append(pending_row(req))
            continue
        print(f"处理 {req['order_id']}（{req['reason_raw'] or req['reason']}）…")
        row = run_payload(build_case(ledger, req), approve=approve, verbose=False,
                          matrix=matrix, allow_degraded=allow_degraded)
        results.append({
            "order_id": req["order_id"], "reason_raw": req["reason_raw"],
            "reason_basis": reason_basis(req), "needs_human_intake": False,
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
