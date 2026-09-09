"""自定义应付账款 case —— 把一份**Excel 导出的付款数据**跑成一次真实付款处置。

    python3 scripts/run_ap.py scenarios/custom/ap-invoices.csv

## 与场景 10（`maos/flows/scenario_10.py`）的分工

场景 10 跑的是**输入写死**的演示：三单、容差、银行全钉在文件里，连跑两次输出逐条
一致 —— 它回答「这台引擎是怎么工作的」。本模块回答另一个问题：
「**换成我的数据，它会怎么判**」。于是：

  · **不比对任何期望值** —— 你的数据没有标准答案。要带判据的对照，那是
    `run.py --contrast` 的事（同口径见 `maos/flows/custom_case.py` 抬头）；
  · 引擎一行不改：四任务 DAG、四个角色、两处 `effect_risk=H` 的审批停点全部复用
    `scenario_10._tasks`，本模块不另抄一份 —— 抄了之后审批停点会漂，而漂了不报错；
  · 三单落库只走 `maos/domain/ap/fixtures.py`，本模块一条 SQL 都不写。

## 输入分两份，因为它们的变更频率差两个数量级

  · **底账** `scenarios/custom/ap-ledger.json`：供应商主数据、采购订单、收货单。
    IT / 顾问配一次，真实落地由 ERP / WMS 导出，不用天天动。
  · **申请表** `scenarios/custom/ap-invoices.csv`：业务方每天给的东西，一行一个
    发票行项目。发票号一填，供应商 / 币种 / 订单数量单价 / 收货数量全部从底账查 ——
    人只填发票上真实印着的东西。抄错一个数，匹配结论就错一次，而且不会报错。

## 输出里哪些是观察、哪些是推断

`biz_status == "settled"` **只可能由 `ap.observe` 写入**（铁律 8）。银行问不出终态时
本模块什么都不写，案子停在 `payment_requested`，输出里 `settled_observations`
就是 0 —— 这不是 bug，是设计。本模块也**不做域内补偿**（那是场景 10 失败路径的面），
它只如实报出案子停在哪、Plan 收在什么状态。

## 底账里有三个字段本轮不下传

`ordered_at` / `received_at` / `warehouse` 记在底账里（它们是 ERP 导出的真实字段），
但 `fixtures.seed_three_way` 目前统一拿开票日期当三单的读取时刻、仓库写死 `WH-1`。
下传它们要改 fixtures，那是引擎面 —— 记进 `docs/BACKLOG.md`，本轮不动。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from maos.contracts.events import new_id
from maos.domain.ap import fixtures, guard, objects
from maos.flows import scenario_10 as s10
from maos.flows.common import build, dump, run_until_settled
# `_blocked_reason` 与域无关（判据取自 event_log 里最后一次进 BLOCKED 的那一跳），
# 刻意直接复用而不是照抄：「这个任务为什么停下来等人」有两份定义，两份迟早不一致。
from maos.flows.custom_case import _blocked_reason
from maos.model.client import select_model_client
from maos.runtime.gate import HumanApprovalQueue
from maos.skills.builtin.ap import _common as C
from maos.tools import ap_codes
from maos.tools.ap import MockBank

#: 底账的三段。少任何一段都不是「补个缺省值继续跑」的事 —— 少一段意味着三单缺一份，
#: 而匹配的前提就变了。补默认值会让它照常跑绿，跑绿的错结论比报错难查得多。
LEDGER_SECTIONS = ("suppliers", "purchase_orders", "goods_receipts")

#: 税种码与发票类型码不在申请表里 —— 一张普通的增值税专用发票这两项恒定。
#: 要演零税率 / 贷记单要给申请表加列，记在 `docs/BACKLOG.md`。
DEFAULT_TAX_CATEGORY = ap_codes.CODE_TAX_STANDARD
DEFAULT_INVOICE_TYPE = ap_codes.CODE_COMMERCIAL_INVOICE

#: 银行行为。`settle_after > 1` 才能证明「一次 query 不一定够」—— 终态是问出来的，
#: 不是一步返回的。底账可用可选的 `bank` 段覆盖这两个数。
DEFAULT_SETTLE_AFTER = 2
DEFAULT_MAX_POLLS = 5

#: 审批是人的动作。CLI 代跑时名字写死，两次跑输出一致。
APPROVER = "沈思锴"
APPROVER_ROLE = "应付主管"

#: 审批捞几轮。付款计划批了之后付款任务还会停一次（两处都是 effect_risk=H），
#: 单轮只捞第一批就会把剩下的静默挂着 —— 口径同 `custom_case.MAX_APPROVAL_ROUNDS`。
MAX_APPROVAL_ROUNDS = 5

GOAL_TEMPLATE = ("支付{supplier_name} {invoice_id} 货款：三单匹配后按 "
                 f"{ap_codes.RULE_AMOUNT_DUE} 出账")


class ApLedgerError(ValueError):
    """底账或申请表里有对不上的地方。消息直接给人看，不用翻栈。"""


# --------------------------------------------------------------------- 读底账
def load(path: str | Path) -> dict:
    """读底账，并把「缺什么」当场说清楚。缺一段不补缺省值，见 `LEDGER_SECTIONS`。"""
    p = Path(path)
    if not p.exists():
        raise ApLedgerError(f"找不到底账文件：{p}")
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ApLedgerError(f"{p} 不是合法 JSON：{exc}") from exc
    if not isinstance(payload, dict):
        raise ApLedgerError(f"{p} 的顶层必须是对象（顶层键即段名）")

    missing = [s for s in LEDGER_SECTIONS if not payload.get(s)]
    if missing:
        raise ApLedgerError(
            f"{p} 缺这几段：{'、'.join(missing)}。形状照 "
            "scenarios/custom/ap-ledger.json（字段对齐 maos/domain/ap/fixtures.py "
            "的 seed_supplier / seed_three_way 形参）")
    return payload


def _bank_config(ledger: dict) -> dict:
    """银行行为。底账没写就用缺省 —— 缺省下 `ap.observe` 问 2 次拿到终态。"""
    raw = ledger.get("bank") if isinstance(ledger.get("bank"), dict) else {}
    try:
        settle_after = int(raw.get("settle_after", DEFAULT_SETTLE_AFTER))
        max_polls = int(raw.get("max_polls", DEFAULT_MAX_POLLS))
    except (TypeError, ValueError) as exc:
        raise ApLedgerError(f"底账 bank 段里的 settle_after / max_polls 必须是整数：{exc}") from exc
    if settle_after < 1 or max_polls < 1:
        raise ApLedgerError("底账 bank 段里的 settle_after / max_polls 必须 >= 1")
    return {"settle_after": settle_after, "max_polls": max_polls}


# ------------------------------------------------------------------- 底账索引
def _po_of(ledger: dict, po_id: str) -> dict:
    """按采购单号取订单。查不到**当场报错并列出可选项**，不猜。

    猜一张最像的订单出来，套用的就是另一批数量与单价，而匹配照样给出一个
    看起来毫无破绽的结论。同一张单号有多版时取版本号最大的那一版 ——
    那是「执行前从 ERP 读到的最新一版」（铁律 8）。
    """
    hits = [o for o in ledger["purchase_orders"] if str(o.get("po_id")) == po_id]
    if not hits:
        available = sorted({str(o.get("po_id")) for o in ledger["purchase_orders"]})
        raise ApLedgerError(
            f"底账里没有采购单 {po_id} —— 可选的是：{'、'.join(available)}。"
            f"先让它进 ap-ledger.json 的 purchase_orders")
    return max(hits, key=lambda o: int(o.get("version") or 1))


def _gr_of(ledger: dict, po_id: str, po_version: int) -> dict:
    """按 (采购单号, 版本) 取收货单。0 份或 2 份都报错 —— 挑一份就是替人做了决定。"""
    hits = [g for g in ledger["goods_receipts"]
            if str(g.get("po_id")) == po_id
            and int(g.get("po_version") or 1) == po_version]
    if not hits:
        raise ApLedgerError(
            f"底账里没有 {po_id} v{po_version} 的收货单 —— 货还没收到就收到票，"
            f"这一步不许放过")
    if len(hits) > 1:
        names = "、".join(sorted(str(g.get("gr_id")) for g in hits))
        raise ApLedgerError(
            f"{po_id} v{po_version} 在底账里有 {len(hits)} 份收货单（{names}）—— "
            f"分批收货要分批开票，本入口一张发票只对一份收货单，不替你挑")
    return hits[0]


def _supplier_of(ledger: dict, supplier_id: str) -> dict:
    hits = [s for s in ledger["suppliers"] if str(s.get("supplier_id")) == supplier_id]
    if not hits:
        available = sorted({str(s.get("supplier_id")) for s in ledger["suppliers"]})
        raise ApLedgerError(
            f"底账里没有供应商 {supplier_id} —— 可选的是：{'、'.join(available)}")
    return hits[0]


def _lines_by_no(rows: list[dict], *, what: str, doc_id: str) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for row in rows or []:
        try:
            line_no = int(row.get("line_no"))
        except (TypeError, ValueError) as exc:
            raise ApLedgerError(f"{what} {doc_id} 有一行的 line_no 不是整数：{row!r}") from exc
        if line_no in out:
            raise ApLedgerError(f"{what} {doc_id} 的第 {line_no} 行出现了两次")
        out[line_no] = row
    if not out:
        raise ApLedgerError(f"{what} {doc_id} 一行都没有")
    return out


# --------------------------------------------------------------- 表格 -> 三单
def build_case(ledger: dict, invoice: dict) -> dict:
    """把「一张发票（申请表里的若干行）+ 底账」拼成 `run_payload()` 的入参。

    发票号是**唯一**该由人填的钥匙：供应商、币种、订单数量与单价、收货数量全部从
    底账查出来。让人手抄这些字段，抄错一个匹配结论就错一次，而且不会报错。

    产出的 `lines` 是 `fixtures.seed_three_way` 要的**八元组**，顺序即
    「订单 -> 收货 -> 发票」：

        (line_no, sku, 订单数量, 订单单价, 收货到货数, 收货不合格数, 发票数量, 发票单价)
    """
    invoice_id = str(invoice["invoice_id"])
    po = _po_of(ledger, str(invoice["po_id"]))
    po_id, po_version = str(po["po_id"]), int(po.get("version") or 1)
    gr = _gr_of(ledger, po_id, po_version)
    gr_id = str(gr["gr_id"])
    supplier = _supplier_of(ledger, str(po["supplier_id"]))

    po_lines = _lines_by_no(po.get("lines"), what="采购单", doc_id=f"{po_id} v{po_version}")
    gr_lines = _lines_by_no(gr.get("lines"), what="收货单", doc_id=gr_id)

    lines: list[tuple] = []
    for row in invoice["lines"]:
        line_no = int(row["line_no"])
        sku = str(row["sku"])
        po_line = po_lines.get(line_no)
        if po_line is None:
            avail = "、".join(f"第 {n} 行（{po_lines[n].get('sku')}）" for n in sorted(po_lines))
            raise ApLedgerError(
                f"发票 {invoice_id} 开了第 {line_no} 行，采购单 {po_id} v{po_version} "
                f"上没有这一行 —— 它有：{avail}")
        gr_line = gr_lines.get(line_no)
        if gr_line is None:
            avail = "、".join(f"第 {n} 行（{gr_lines[n].get('sku')}）" for n in sorted(gr_lines))
            raise ApLedgerError(
                f"发票 {invoice_id} 开了第 {line_no} 行，收货单 {gr_id} 上没有这一行 —— "
                f"它有：{avail}")
        # SKU 对不上在这里就喊，不放它进匹配：放进去之后 `ap.match` 按
        # (行号, SKU) 配对会同时报「订单上没订过」和「货没收到」两条拒付理由，
        # 而真正的原因（填表的人抄错了料号）淹没在噪声里。
        for doc, doc_id, line in (("采购单", f"{po_id} v{po_version}", po_line),
                                  ("收货单", gr_id, gr_line)):
            if str(line.get("sku")) != sku:
                raise ApLedgerError(
                    f"发票 {invoice_id} 第 {line_no} 行的 SKU 是 {sku}，"
                    f"而{doc} {doc_id} 同一行是 {line.get('sku')} —— 对不上就不往下走")
        lines.append((
            line_no, sku,
            float(po_line["quantity"]), str(po_line["unit_price"]),
            float(gr_line.get("received") or 0), float(gr_line.get("rejected") or 0),
            float(row["quantity"]), str(row["unit_price"]),
        ))

    return {
        "tenant_id": str(supplier["tenant_id"]),
        "supplier": supplier,
        "case_id": f"AP-{invoice_id}",
        "invoice_id": invoice_id,
        "po_id": po_id,
        "po_version": po_version,
        "gr_id": gr_id,
        "currency": str(po.get("currency") or "CNY"),
        "lines": lines,
        "tax_rate": float(invoice["tax_rate"]),
        "tax_category": DEFAULT_TAX_CATEGORY,
        "invoice_type": DEFAULT_INVOICE_TYPE,
        "issued_at": str(invoice["issued_at"]),
        "due_at": str(invoice.get("due_at") or ""),
        "bank": _bank_config(ledger),
        "notes": [n for n in (r.get("note") for r in invoice["lines"]) if n],
    }


# --------------------------------------------------------------------- 跑一次
def _task_ids(case_id: str) -> tuple[str, str, str, str]:
    return (f"task-{case_id}-intake", f"task-{case_id}-match",
            f"task-{case_id}-plan", f"task-{case_id}-pay")


def _tasks(payload: dict, *, bank: str, ids: tuple[str, str, str, str]) -> list[dict]:
    """四任务 DAG **直接复用 `scenario_10._tasks`** —— 角色、验收判据、两处
    `effect_risk=H` 的审批停点是引擎的口径，本模块只换数据不换骨架。

    只覆盖两处：租户来自底账（引擎里写死的是它自己的演示租户），订单版本来自底账
    （引擎里写死 1）。另抄一份 DAG 省不了几行，代价是审批停点会漂，且漂了不报错。
    """
    tasks = s10._tasks(
        case_id=payload["case_id"], po_id=payload["po_id"], gr_id=payload["gr_id"],
        invoice_id=payload["invoice_id"], bank=bank,
        max_polls=payload["bank"]["max_polls"], ids=ids)
    for task in tasks:
        task["inputs"]["tenant_id"] = payload["tenant_id"]
        if "po_version" in task["inputs"]:
            task["inputs"]["po_version"] = payload["po_version"]
    return tasks


def run_payload(payload: dict, *, verbose: bool = False) -> dict:
    """跑一张发票，返回**观测到的事实**（不含任何期望值）。"""
    tenant_id, case_id = payload["tenant_id"], payload["case_id"]
    supplier = payload["supplier"]

    # 语义审查的应答直接取场景 10 那一份：Reviewer 问的是同一件事，
    # 另写一份就是本域的第二套审查口径。copy 一份，不改原 dict。
    script = dict(s10.SCRIPT)
    model = select_model_client(script, force_scripted=True)
    store, bus, cp, model, worker, gate = build(script, model=model)

    # ---- 落库：唯一路径是 domain/ap/fixtures.py（R2）----
    fixtures.seed_supplier(
        store, tenant_id=tenant_id, supplier_id=str(supplier["supplier_id"]),
        name=str(supplier.get("name") or supplier["supplier_id"]),
        payment_means_code=str(supplier["payment_means_code"]),
        payment_terms=str(supplier.get("payment_terms") or ""),
        bank_account=str(supplier.get("bank_account") or ""))
    totals = fixtures.seed_three_way(
        store, tenant_id=tenant_id, supplier_id=str(supplier["supplier_id"]),
        po_id=payload["po_id"], gr_id=payload["gr_id"],
        invoice_id=payload["invoice_id"], lines=payload["lines"],
        tax_category=payload["tax_category"], tax_rate=payload["tax_rate"],
        invoice_type=payload["invoice_type"], issued_at=payload["issued_at"],
        due_at=payload["due_at"], currency=payload["currency"],
        po_version=payload["po_version"])

    # 银行按名取：task.inputs 会被 json.dumps，实例塞不进去（见 ap/_common.py）。
    bank_name = f"ap-custom-{case_id}"
    C.reset_banks()
    C.register_bank(bank_name, MockBank(settle_after=payload["bank"]["settle_after"]))

    ids = _task_ids(case_id)
    t_plan = ids[2]
    trace_id, plan_id = new_id("trace"), new_id("plan")
    goal = GOAL_TEMPLATE.format(supplier_name=supplier.get("name") or "",
                                invoice_id=payload["invoice_id"])
    cp.create_plan(goal=goal, trace_id=trace_id, plan_id=plan_id,
                   tasks=_tasks(payload, bank=bank_name, ids=ids))
    cp.start_plan(plan_id)
    run_until_settled(bus, gate, cp, plan_id)

    # ---- 人工审批：停在 BLOCKED 的任务都在等人，CLI 代跑人的那一半 ----
    # **必须循环**：付款计划批了之后付款任务还会停一次，单轮只捞第一批。
    hq = HumanApprovalQueue(store, cp)
    who = f"{APPROVER}（{APPROVER_ROLE}）"
    human_exits: list[dict] = []
    for _ in range(MAX_APPROVAL_ROUNDS):
        pending = hq.pending(plan_id)
        if not pending:
            break
        for blocked in pending:
            human_exits.append({
                "task_id": blocked["task_id"], "title": blocked["title"],
                "effect_risk": blocked.get("effect_risk"),
                "why": _blocked_reason(cp, plan_id, blocked["task_id"]),
                "decision": "approved",
            })
            # 顺序不可换：先落 payment_approval（人的决定），再放行任务 ——
            # ap.execute 会核对审批记录，没有它就拒绝发起付款。
            if blocked["task_id"] == t_plan:
                C.record_approval(
                    store, tenant_id=tenant_id, case_id=case_id, approver=who,
                    decision="approved",
                    reason=f"金额按 {ap_codes.RULE_AMOUNT_DUE} 从三单算出，与发票一致")
            hq.decide(blocked["task_id"], approved=True, operator=who,
                      note=f"按{APPROVER_ROLE}权限放行")
        run_until_settled(bus, gate, cp, plan_id)

    if verbose:
        dump(cp, plan_id, f"自定义应付 case {case_id}")
    return _observe(store, cp, plan_id, payload=payload, totals=totals,
                    human_exits=human_exits, ids=ids)


def _observe(store, cp, plan_id: str, *, payload: dict, totals: dict,
             human_exits: list[dict], ids: tuple[str, str, str, str]) -> dict:
    """把这一跑的事实收成一份字典。只读库，不做任何判定，也不比对任何期望值。"""
    tenant_id, case_id = payload["tenant_id"], payload["case_id"]
    _, t_match, t_plan, t_pay = ids

    case = guard.get_case(store, tenant_id, case_id) or {}
    obs = guard.observations_of(store, tenant_id, case_id)
    match = _artifact(store, t_match, "ap_match_result")
    plan_art = _artifact(store, t_plan, "ap_payment_plan")
    advice = _artifact(store, t_pay, "ap_bank_advice")

    tasks = cp.store.list_tasks(plan_id)
    return {
        "invoice_id": payload["invoice_id"], "case_id": case_id, "tenant_id": tenant_id,
        "supplier_id": str(payload["supplier"]["supplier_id"]),
        "supplier_name": str(payload["supplier"].get("name") or ""),
        "po_id": payload["po_id"], "po_version": payload["po_version"],
        "gr_id": payload["gr_id"], "currency": payload["currency"],
        "line_count": len(payload["lines"]),
        # 发票自称的应付额。**它是待验证的输入，不是结论** —— 结论是 payable_amount。
        "invoice_amount_due": totals["amount_due"],
        "matched": None if match is None else bool(match["matched"]),
        "findings": [] if match is None else match["findings"],
        "checked": [] if match is None else match["checked"],
        "tolerance": {} if match is None else match["tolerance"],
        "payable_amount": "" if match is None else match["payable_amount"],
        "payment_plan": None if plan_art is None else plan_art["plan"],
        "plan_citations": [] if plan_art is None else
                          [c["rule_id"] for c in plan_art["citations"]],
        "biz_status": case.get("biz_status"),
        "bank_advice": None if advice is None else {
            "observed_state": advice["observed_state"],
            "poll_count": advice["poll_count"],
            "bank_reference": advice.get("bank_reference"),
            "value_date": advice.get("value_date"),
        },
        "payment_observations": [
            {"observed_state": o["observed_state"],
             "bank_reference": o.get("bank_reference")} for o in obs],
        "settled_observations": sum(1 for o in obs if o["observed_state"] == "settled"),
        "human_exits": human_exits,
        "failed_tasks": [{"task_id": t["task_id"], "title": t["title"],
                          "last_error": t.get("last_error") or ""}
                         for t in tasks if t["state"] == "FAILED"],
        "business_refs": len(objects.list_business_refs(store, plan_id=plan_id)),
        "plan_id": plan_id, "plan_state": cp.store.get_plan(plan_id)["state"],
        "tasks": [{"task_id": t["task_id"], "role": t["role"], "title": t["title"],
                   "state": t["state"], "attempt": t["attempt"]} for t in tasks],
    }


def _artifact(store, task_id: str, kind: str) -> Any | None:
    """取某任务最近一轮的某类产物，**没有就是 None**。

    没产出不是异常：匹配没过就没有付款计划，付款没发起就没有银行回单 ——
    那正是结果表要如实报出来的东西。
    """
    try:
        return s10.artifact_of(store, task_id, kind)
    except LookupError:
        return None


def run_invoice(ledger: dict, invoice: dict, **kw: Any) -> dict:
    """一张发票：拼 case 再跑。`build_case()` 的报错原样上抛，由 CLI 翻成人话。"""
    return run_payload(build_case(ledger, invoice), **kw)
