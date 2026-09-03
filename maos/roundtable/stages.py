"""五岗的事实卡 —— **全是规则代码，零模型**。

这是 R1（事实只来自规则代码）落地的地方：本模块每个函数都只读入参，算出一段
纯文本 `facts` 和一份结构化 `data`，模型拿到 `facts` 之后只许复述。房间里出现
事实卡里没有的订单号、金额、规则号，是 bug 不是文案问题。

三条口径值得单独说清：

**中文映射经 `scripts/run_requests.py` 取，不另抄一份。** 诉求类型、裁定、业务状态
三张中文表已经在那个文件里、且被 `test_request_sheet.py` 钉着。另抄一份的症状是
「CSV 里写『坏了』能跑、群里写『坏了』不认」，且两边都不报错。

**证据岗要的带 `params` 规则在自己的 `:memory:` 库上重算。** `preflight()` 返回的
`matched_rules` 只有 `AS-001@v1` 这样的 ref 串，没有 `no_reason_days` 之类的参数。
重算走的是与 `preflight` **同一批**函数（`fixtures.seed_case` + `contrast.policy_view`），
不是另一套口径；库用完即弃。让 T88 改 `preflight` 的返回形状是更大的面，且那是别人的文件。

**`checked["decision"] == "reject"` 时不做核算预演。** 实测：同一张单子走
`refund.intake` → `policy.match` → `finance.settle` 三步，即便裁定是驳回也照样算出
6800.00 —— 因为 `policy.match` 是「前缀命中即 approve」，窗口判定在
`contrast.evaluate_eligibility` 里，那一步不在这三步中。真跑 `run_payload` 退的是
0.00。不看 `checked` 就预演，房间里会报一个真跑拿不到的金额。
"""

from __future__ import annotations

import json
import logging
import re
from decimal import Decimal, InvalidOperation

log = logging.getLogger("maos.roundtable")

#: 核算预演在 `:memory:` 库上跑时用的 plan/task 归属。`refund.intake` 要求这两个
#: extras 非空（业务引用要挂到 DAG 上），而预演不属于任何真 Plan —— 给确定性字面量，
#: 不是 `new_id()`：预演连跑两次的产出要逐字一致，才谈得上和真跑对账。
PREVIEW_PLAN_ID = "preview"

#: 预演措辞里必须原样保留的三个字眼（铁律 8 / R8）。放行前只有「预演」，没有「已退款」。
PREVIEW_WORDING = "以上是核算预演，未落账，放行后按同一段代码正式核算"


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def _run_requests():
    """借 `scripts/run_requests.py` 的中文映射。加载与缓存都由 router 那份负责。"""
    from maos.ingress.router import _load_run_requests
    return _load_run_requests()


def reason_cn_of(reason_code: str) -> str:
    """诉求类型反查中文。一个 code 对多个说法，取表里第一个 —— 那是最常见的写法。"""
    if not reason_code:
        return ""
    for cn, code in _run_requests().REASONS.items():
        if code == reason_code:
            return cn
    return reason_code


def decision_cn_of(decision: str) -> str:
    return _run_requests().DECISION_CN.get(decision, decision or "")


def status_cn_of(status: str) -> str:
    return _run_requests().STATUS_CN.get(status, status or "")


def _dec(value) -> Decimal | None:                      # noqa: ANN001
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _money(value) -> str:                               # noqa: ANN001
    """金额的显示形态。`6800.0` 与 `6800.00` 是同一个数，统一到两位小数。"""
    d = _dec(value)
    return "未知" if d is None else f"{d:.2f}"


def _refs_text(rule_refs) -> str:                       # noqa: ANN001
    """依据的显示形态。`run_payload` 回的 `rule_refs` 是 `finance_entry` 表里那一列，
    即一个 **JSON 字符串**；原样贴进房间就是一串方括号加引号。解不开就照原样发，
    不编 —— 解析失败时把原文吞掉，房间里读到的是一条没有依据的核算。"""
    if isinstance(rule_refs, str):
        try:
            parsed = json.loads(rule_refs)
        except (TypeError, ValueError):
            return rule_refs
        rule_refs = parsed
    if isinstance(rule_refs, (list, tuple)):
        return "、".join(str(r) for r in rule_refs) or "无"
    return str(rule_refs) if rule_refs else "无"


def _order_row(rows, order_id: str) -> dict:            # noqa: ANN001
    """`order_snapshot` 里这一单的最高 version 行。取法与 `build_case` 一致。"""
    hits = [r for r in (rows or []) if isinstance(r, dict) and r.get("order_id") == order_id]
    if not hits:
        return {}
    return max(hits, key=lambda r: int(r.get("version") or 0))


def _order_payload(row: dict) -> dict:
    """订单快照行的 `payload_json`。非法 JSON 当空对象 + 一行 WARNING。

    这里**不抛**：底账是外部系统的快照，一行 JSON 写坏不该让整个圆桌哑掉。
    """
    raw = row.get("payload_json") if isinstance(row, dict) else None
    if raw in (None, ""):
        return {}
    try:
        obj = json.loads(raw)
    except (TypeError, ValueError) as exc:              # noqa: BLE001
        log.warning("订单 %s 的 payload_json 不是合法 JSON（%s: %s），按空对象处理",
                    (row or {}).get("order_id"), type(exc).__name__, exc)
        return {}
    return obj if isinstance(obj, dict) else {}


def _identity_of(role: str):                            # noqa: ANN001, ANN201
    """取岗位 identity。**函数内 import**：`team` 模块级要 import 本模块，反过来
    在模块级引它就成环。放在函数里，两个方向都只在调用时解析。"""
    from maos.roundtable.team import identity_of
    return identity_of(role)


def _invoke(role: str, skill_name: str, payload: dict, *, task_id: str):
    """按名调一个 skill。取不到类时 invoker 自己回 `skill_not_found:<name>`。"""
    from maos.skills.invoker import SkillInvoker
    return SkillInvoker(_identity_of(role)).invoke(
        skill_name, payload,
        extras={"plan_id": PREVIEW_PLAN_ID, "task_id": task_id})


def _memory_store():
    from maos.core.store import SqliteStore
    store = SqliteStore(":memory:")
    store.init_schema()
    return store


def _rules_of(payload: dict) -> list[dict]:
    """本单适用的规则（带 `params`）。与 `preflight` 同一批函数，库用完即弃。"""
    from maos.domain.refund import fixtures
    from maos.flows import contrast

    store = _memory_store()
    fixtures.seed_case(store, payload)
    seed = fixtures.case_seed_of(payload)
    view = contrast.policy_view(store, tenant_id=str(seed["tenant_id"]),
                                order_id=str(seed["order_id"]),
                                order_version=int(seed["order_version"]))
    return list(view["rules"])


# --------------------------------------------------------------------------
# 逐单预检：五岗各一条
# --------------------------------------------------------------------------
def facts_intake(payload: dict, checked: dict, evidence_count: int) -> tuple[str, dict]:
    """申请受理岗：这一单是谁的、要退多少、材料齐不齐。"""
    case = dict(payload.get("case") or {})
    order_id = str(case.get("order_id") or checked.get("order_id") or "")
    row = _order_row(payload.get("order_snapshot"), order_id)
    amount_paid = row.get("amount_paid")
    amount_claimed = case.get("amount_claimed", checked.get("amount_claimed"))
    claimed_d, paid_d = _dec(amount_claimed), _dec(amount_paid)
    over_paid = bool(claimed_d is not None and paid_d is not None and claimed_d > paid_d)
    same_as_paid = bool(claimed_d is not None and paid_d is not None and claimed_d == paid_d)
    reason_code = str(case.get("reason_code") or "")
    requested_at = str(checked.get("requested_at") or "")

    claimed_line = f"申报金额：{_money(amount_claimed)}"
    if same_as_paid:
        claimed_line += "（与订单实付一致；申请表没填金额时就按实付计）"
    lines = [
        f"订单号：{order_id}",
        f"商品：{case.get('sku') or '未知'}",
        f"订单实付：{_money(amount_paid)}",
        claimed_line,
        f"诉求类型：{reason_cn_of(reason_code)}（{reason_code or '未填'}）",
        f"申请日期：{requested_at or '日期未填，按今天'}",
        f"随案证据：{evidence_count} 份",
    ]
    if over_paid:
        lines.append("申报金额高于订单实付，核算会封顶到实付")
    return "\n".join(lines), {
        "order_id": order_id, "sku": case.get("sku"),
        "amount_paid": amount_paid, "amount_claimed": amount_claimed,
        "over_paid": over_paid, "evidence_count": evidence_count,
    }


def facts_policy(checked: dict) -> tuple[str, dict]:
    """规则审核岗：按下单锁定的政策版本，这一单该不该退。"""
    keys = ("decision", "deciding_rule", "matched_rules", "elapsed_days",
            "pinned_policy_version", "approver_role", "why")
    data = {k: checked.get(k) for k in keys}
    matched = list(data["matched_rules"] or [])
    lines = [
        f"裁定：{decision_cn_of(str(data['decision'] or ''))}",
        f"决定性规则：{data['deciding_rule'] or '无单条规则决定，按基线裁定'}",
        f"命中规则：{len(matched)} 条（{'、'.join(matched) or '无'}）",
        f"下单锁定的政策版本：v{data['pinned_policy_version']}",
        f"距付款：{data['elapsed_days']} 天",
        f"放行需要的审批角色：{data['approver_role'] or '未指定'}",
        f"判定理由：{data['why'] or '无'}",
    ]
    return "\n".join(lines), data


def facts_evidence(payload: dict, checked: dict, ledger: dict) -> tuple[str, dict]:
    """证据核验岗：随案材料够不够、与订单事实自不自洽。

    skill 没装载是**主路径而不是边角**：整合前 `refund.evidence_check` 根本不在
    注册表里。没装载就照实说没装载，仍然发一条言 —— 一个岗位在房间里凭空消失，
    比它说「我这儿装备还没到」更难排查。
    """
    from maos.skills import registry

    if registry.get("refund.evidence_check") is None:
        return ("证据核验 skill 未装载（refund.evidence_check 不在注册表里），"
                "本单证据无法核验，随案材料请人工过目"), {"verdict": "unavailable"}

    from maos.domain.refund import fixtures

    seed = fixtures.case_seed_of(payload)
    order_id = str(seed.get("order_id") or "")
    row = _order_row(ledger.get("order_snapshot") if ledger else None, order_id) \
        or _order_row(payload.get("order_snapshot"), order_id)
    order_json = _order_payload(row)
    order_facts = {k: order_json[k] for k in ("logistics", "qc_report") if k in order_json}

    res = _invoke("refund_evidence", "refund.evidence_check", {
        "case_seed": seed,
        "customer_evidence": payload.get("customer_evidence") or [],
        "rules": _rules_of(payload),
        "order_facts": order_facts,
        "requested_at": str(checked.get("requested_at") or ""),
    }, task_id="preview-evidence")

    if res.status != "ok" or not isinstance(res.output, dict):
        return (f"证据核验失败：refund.evidence_check: {res.error or '出参不是 dict'}，"
                "本单证据请人工过目"), {"verdict": "unavailable", "error": res.error}

    out = dict(res.output)
    items = list(out.get("items") or [])
    gaps = list(out.get("gaps") or [])
    lines = [
        f"证据核验结论：{out.get('verdict')}",
        f"逐份核验：{len(items)} 份材料，其中通过 {sum(1 for i in items if i.get('ok'))} 份",
        f"规则要求的证据类型：{'、'.join(out.get('required_kinds') or []) or '无明确要求'}"
        f"，最少份数 {out.get('min_count')}",
        f"缺口：{'；'.join(gaps) or '无'}",
    ]
    checks = list(out.get("consistency") or [])
    if checks:
        bad = [c.get("check") for c in checks if not c.get("ok")]
        lines.append(f"交叉核对：{len(checks)} 项，未通过 {len(bad)} 项"
                     f"（{'、'.join(str(b) for b in bad) or '无'}）")
    return "\n".join(lines), out


def facts_risk(payload: dict, checked: dict, ledger: dict) -> tuple[str, dict]:
    """风险反欺诈岗：这个客户、这一单，有没有重复退款或异常频次。"""
    from maos.skills import registry

    if registry.get("refund.risk_screen") is None:
        return ("风险筛查 skill 未装载（refund.risk_screen 不在注册表里），"
                "本单风险未经筛查，放行前请人工看一眼客户历史"), {"level": "unavailable"}

    from maos.domain.refund import fixtures

    seed = fixtures.case_seed_of(payload)
    order_id = str(seed.get("order_id") or "")
    rows = (ledger.get("order_snapshot") if ledger else None) or payload.get("order_snapshot") or []
    order = _order_row(rows, order_id)
    customer_id = str(_order_payload(order).get("customer_id") or "")
    if customer_id:
        customer_orders = [r for r in rows if isinstance(r, dict)
                           and str(_order_payload(r).get("customer_id") or "") == customer_id]
    else:
        # 底账还没有 customer_id 时，「同账号的其它订单」这件事无从谈起 ——
        # 拿全表当同一个客户会把风险分算成天文数字。只认本单。
        customer_orders = [order] if order else []

    res = _invoke("refund_risk", "refund.risk_screen", {
        "case_seed": seed,
        "order": order,
        "customer_orders": customer_orders,
        "refund_history": (ledger or {}).get("refund_history") or [],
        "requested_at": str(checked.get("requested_at") or ""),
    }, task_id="preview-risk")

    if res.status != "ok" or not isinstance(res.output, dict):
        return (f"风险筛查失败：refund.risk_screen: {res.error or '出参不是 dict'}，"
                "本单风险未经筛查"), {"level": "unavailable", "error": res.error}

    out = dict(res.output)
    signals = dict(out.get("signals") or {})
    reasons = list(out.get("reasons") or [])
    lines = [
        f"风险档位：{out.get('level')}（评分 {out.get('score')}）",
        f"命中信号：{'；'.join(reasons) or '无'}",
        f"同一订单是否已有退款记录：{'是' if signals.get('already_refunded') else '否'}",
        f"同账号关联订单：{signals.get('multi_order_same_account')} 单，"
        f"近 30 天退款申请 {signals.get('frequency_30d')} 次",
    ]
    return "\n".join(lines), out


def facts_finance_preview(payload: dict, checked: dict) -> tuple[str, dict]:
    """财务执行岗（放行前）：核算**预演**，未落账。

    走的是与 DAG 里逐字相同的三个 skill，只是库换成 `:memory:` 的一次性副本。
    另写一套算法的症状是「群里预演说 6800、真跑退了 5390」，而两边各自都自洽、
    都不报错 —— 所以宁可多灌一次库，也不另算。
    """
    from maos.domain.refund import fixtures

    seed = fixtures.case_seed_of(payload)
    case_id = str(seed.get("case_id") or checked.get("case_id") or "")
    approver = str(checked.get("approver_role") or "")
    data: dict = {"preview_ran": False, "amount_approved": None, "breakdown": None,
                  "rule_refs": None, "policy_version": None, "error": None}

    if str(checked.get("decision") or "") == "reject":
        return ("裁定驳回，无需核算：本单不进入核算与付款环节。"
                f"理由：{checked.get('why') or '无适用售后规则'}"), data

    from maos.flows import contrast

    store = _memory_store()
    fixtures.seed_case(store, payload)
    tenant_id = str(seed["tenant_id"])
    steps = (
        ("refund.intake", "refund_intake", "preview-intake",
         lambda _prev: {"signals": contrast._signals_of(payload), "case_seed": seed}),
        ("policy.match", "refund_finance", "preview-policy",
         lambda _prev: {"tenant_id": tenant_id, "case_id": case_id}),
        ("finance.settle", "refund_finance", "preview-settle",
         lambda prev: {"tenant_id": tenant_id, "case_id": case_id, "policy": prev}),
    )

    from maos.skills.invoker import SkillInvoker

    prev: dict | None = None
    for name, role, task_id, build in steps:
        res = SkillInvoker(_identity_of(role), store=store).invoke(
            name, build(prev), extras={"plan_id": PREVIEW_PLAN_ID, "task_id": task_id})
        if res.status != "ok" or not isinstance(res.output, dict):
            data["error"] = f"{name}: {res.error or '出参不是 dict'}"
            return ("\n".join([
                f"核算预演失败：{data['error']}",
                f"这一单裁定是{decision_cn_of(str(checked.get('decision') or ''))}，"
                "金额要等核算跑通才算得出来",
                f"放行请审批人（{approver or '未指定'}）发 /approve {case_id}",
            ]), data)
        prev = dict(res.output)
        if name == "policy.match":
            data["policy_version"] = prev.get("policy_version")

    settle = prev or {}
    breakdown = dict(settle.get("breakdown") or {})
    rule_refs = list(settle.get("rule_refs") or [])
    data.update({"preview_ran": True,
                 "amount_approved": settle.get("amount_approved"),
                 "breakdown": breakdown, "rule_refs": rule_refs})
    lines = [
        f"核算预演金额：{data['amount_approved']}",
        f"计算过程：订单实付 {breakdown.get('amount_paid')}，申报 {breakdown.get('amount_claimed')}，"
        f"退款比例 {breakdown.get('refund_ratio')}，扣费 {breakdown.get('deduct_fee')}"
        f"{'，已按实付封顶' if breakdown.get('capped_by_paid') else ''}",
        f"依据：{'、'.join(rule_refs) or '缺省全额口径'}（政策 v{data['policy_version']}）",
        PREVIEW_WORDING,
        f"放行请审批人（{approver or '未指定'}）发 /approve {case_id}",
    ]
    return "\n".join(lines), data


# --------------------------------------------------------------------------
# 放行之后：只有财务执行岗发言
# --------------------------------------------------------------------------
def facts_finance_result(result: dict) -> tuple[str, dict]:
    """财务执行岗（放行后）：核算落了什么、付款受理到哪一步。

    **铁律 8 / R8 在这里落地**：MAOS 不持有退款的权威状态，只持有对网关的观察。
    只有真的观察到 `settled` 才允许出现「到账」二字；观察到了但不是 settled，
    说「已受理，未确认到账」；一条观察都没有，说「未走到付款」。措辞之外没有别的
    机制拦得住这件事 —— 下游读到的就是这段文本。
    """
    keys = ("amount_approved", "policy_version_used", "rule_refs", "biz_status",
            "settled_observations", "payment_observations", "human_exits", "plan_state")
    data = {k: result.get(k) for k in keys}
    settled = int(data["settled_observations"] or 0)
    observations = list(data["payment_observations"] or [])
    exits = list(data["human_exits"] or [])
    biz_status = str(data["biz_status"] or "")

    if settled > 0:
        payment_line = (f"付款观察：{len(observations)} 条，其中确认结算 {settled} 条 —— "
                        "已观察到账")
    elif observations:
        payment_line = (f"付款观察：{len(observations)} 条，其中确认结算 {settled} 条 —— "
                        "已受理，未确认到账，仍在向网关问终态")
    else:
        payment_line = "付款观察：一条都没有 —— 本单未走到付款环节"

    lines = [
        f"核准金额：{data['amount_approved']}",
        f"政策版本：v{data['policy_version_used']}",
        f"依据：{_refs_text(data['rule_refs'])}",
        f"业务状态：{status_cn_of(biz_status)}（{biz_status or '未知'}）",
        payment_line,
        f"Plan 内任务级审批点：{len(exits)} 个"
        f"（{'、'.join(str(e.get('title') or '') for e in exits) or '无'}）",
        f"Plan 状态：{data['plan_state']}",
    ]
    return "\n".join(lines), data


# --------------------------------------------------------------------------
# 一张表：每岗只汇总一次
# --------------------------------------------------------------------------
def sheet_stats(rows: list[dict]) -> dict:
    """一张申请表的行统计。五个岗共用同一份计数 —— 各岗自己数一遍，
    数出五个不一样的「合法行数」，房间里没人知道该信哪个。"""
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    ok = [r for r in rows if not r.get("error") and isinstance(r.get("checked"), dict)]
    bad = [r for r in rows if r.get("error")]
    approve = [r for r in ok if str((r["checked"] or {}).get("decision") or "") == "approve"]
    reject = [r for r in ok if str((r["checked"] or {}).get("decision") or "") == "reject"]
    return {
        "total": len(rows), "valid": len(ok), "invalid": len(bad),
        "approve": len(approve), "reject": len(reject),
        "problem_rows": len([r for r in rows if r.get("problems")]),
        "warning_rows": len([r for r in rows if r.get("warnings")]),
        # 「需证据」= 会走到证据核验的行。裁定驳回的单子不进这一步。
        "need_evidence": len(approve),
        "pending_case_ids": [str((r["checked"] or {}).get("case_id") or "") for r in approve],
    }


def _pending_line(stats: dict) -> str:
    ids = stats["pending_case_ids"]
    return f"待放行 {len(ids)} 单：{'、'.join(ids) or '无'}"


def facts_sheet_intake(rows: list[dict]) -> tuple[str, dict]:
    stats = sheet_stats(rows)
    lines = [
        f"这张表共 {stats['total']} 行，其中能建案的 {stats['valid']} 行、填错的 {stats['invalid']} 行",
        f"有填写问题的 {stats['problem_rows']} 行，有提示的 {stats['warning_rows']} 行",
        "填错的行不会进入后续环节，改好再拖一次表即可",
    ]
    return "\n".join(lines), stats


def facts_sheet_policy(rows: list[dict]) -> tuple[str, dict]:
    stats = sheet_stats(rows)
    lines = [
        f"按下单锁定的政策版本逐行裁定：{stats['valid']} 行有结论，"
        f"批准 {stats['approve']} 行、驳回 {stats['reject']} 行",
        f"另有 {stats['invalid']} 行因填写问题没有裁定",
        _pending_line(stats),
    ]
    return "\n".join(lines), stats


def facts_sheet_evidence(rows: list[dict]) -> tuple[str, dict]:
    from maos.skills import registry

    stats = sheet_stats(rows)
    loaded = registry.get("refund.evidence_check") is not None
    lines = [
        f"进入证据核验范围的有 {stats['need_evidence']} 单（裁定驳回的 {stats['reject']} 单不看证据）",
        "证据核验 skill 已装载，逐单预检时按单核验" if loaded
        else "证据核验 skill 未装载，这一批的证据无法核验，请人工过目",
    ]
    return "\n".join(lines), {**stats, "skill_loaded": loaded}


def facts_sheet_risk(rows: list[dict]) -> tuple[str, dict]:
    from maos.skills import registry

    stats = sheet_stats(rows)
    loaded = registry.get("refund.risk_screen") is not None
    lines = [
        f"待筛查 {stats['valid']} 单，其中 {stats['approve']} 单已裁定批准、会走到付款",
        "风险筛查 skill 已装载，逐单预检时按单筛查" if loaded
        else "风险筛查 skill 未装载，这一批未经风险筛查，放行前请人工看一眼客户历史",
    ]
    return "\n".join(lines), {**stats, "skill_loaded": loaded}


def facts_sheet_finance(rows: list[dict]) -> tuple[str, dict]:
    stats = sheet_stats(rows)
    lines = [
        f"需要核算的有 {stats['approve']} 单，驳回的 {stats['reject']} 单无需核算",
        "整表核算金额要逐单预演才算得出来，这里不给合计 —— 合计一个没预演过的数字，"
        "群里会当成已经算完的账",
        _pending_line(stats),
    ]
    return "\n".join(lines), stats


#: 数字白名单的取数口径。测试按它断言「facts 里的数字都能在入参里找到」。
#: 放在这里而不是测试文件里，是为了让口径和事实卡长在同一个文件 —— 加一行事实卡
#: 却忘了它的数字从哪来，改这里的时候就会被问一次。
def numbers_in(text: str) -> set[Decimal]:
    """一段文本里出现的全部数字。`6800` / `6800.0` / `6800.00` 视作同一个数。"""
    out: set[Decimal] = set()
    for token in re.findall(r"\d+(?:\.\d+)?", text or ""):
        d = _dec(token)
        if d is not None:
            out.add(d)
    return out
