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

#: 下游两岗各自要跑的 skill。名字只在这里写一次 —— 单案卡、整表卡、「未装载」判定
#: 三处共用；各写一遍的症状是改名之后一处判「未装载」、另一处照跑。
EVIDENCE_SKILL = "refund.evidence_check"
RISK_SKILL = "refund.risk_screen"

#: 风险档位与证据类型的中文显示。**只管显示**，判定仍按 skill 出参的英文取值。
#: 证据类型的中文只给按钮文案用；事实卡里「缺口」那一句照旧念 skill 的原话
#: （「缺少 image 类证据」），与单案卡逐字同源。
LEVEL_CN = {"low": "低风险", "medium": "中风险", "high": "高风险"}
KIND_CN = {"image": "照片", "video": "视频", "audio": "录音",
           "document": "文件", "attachment": "附件"}


def kinds_cn(kinds) -> str:                             # noqa: ANN001
    """`["image"]` -> `照片`。认不出的照原样，不猜。"""
    return "、".join(KIND_CN.get(str(k), str(k)) for k in (kinds or []))


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
def facts_intake(payload: dict, checked: dict, evidence_count: int,
                 round_no: int = 1) -> tuple[str, dict]:
    """申请受理岗：这一单是谁的、要退多少、材料齐不齐。

    `round_no` 是这一单在本会话里的第几轮，由触发侧数出来传进来（缺省 `1` = 首次
    预检，也是今天全部调用点的形态）。`>= 2` 才多说一行：复检那一轮如果与第一轮
    逐字相同、只有数字变了，房间里的人看不出发生过什么 —— 他会以为机器人把同一单
    重放了一遍。

    **只报轮次，不提上一轮的结论**：上一轮判成什么根本不在入参里，说「证据补齐了」
    「比上一轮更充分」就是编（R1 / 铁律 8）。

    `0`、负数、超大值一律不抛：`>= 2` 判不过就当首轮、一个字都不多说，判得过就照
    给的数字排版。这个数是触发侧数出来的真数字，本函数不校验也不改写 —— 在这里夹
    一层「看着不对就改成 1」，症状是房间里的轮次和触发侧的账对不上，且两边都不报错。
    """
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
    if round_no >= 2:
        lines.append(f"本单第 {round_no} 轮（上一轮之后有新证据进来，按当前材料重新过一遍）")
    if over_paid:
        # **只标记触发，不解释封顶怎么算、封到哪个数**（`SYSTEM_TMPL` 字段归属表：
        # 封顶标记归受理岗，封顶后的应退金额归财务执行岗，逐单预演完才给）。
        # 两岗都说一句「封到实付」的症状：财务预演出来的数与受理岗的口头算法一旦
        # 差一分钱（优惠券、运费另算），房间里就得当场对账，而受理岗手上根本没有账。
        lines.append("申报金额高于订单实付，本单触发封顶")
    return "\n".join(lines), {
        "order_id": order_id, "sku": case.get("sku"),
        "amount_paid": amount_paid, "amount_claimed": amount_claimed,
        "over_paid": over_paid, "evidence_count": evidence_count,
        "round_no": round_no,
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


def facts_evidence(payload: dict, checked: dict, ledger: dict,
                   added: int = 0) -> tuple[str, dict]:
    """证据核验岗：随案材料够不够、与订单事实自不自洽。

    skill 没装载是**主路径而不是边角**：整合前 `refund.evidence_check` 根本不在
    注册表里。没装载就照实说没装载，仍然发一条言 —— 一个岗位在房间里凭空消失，
    比它说「我这儿装备还没到」更难排查。

    `added` 是本轮**新增**几份随案证据，由触发侧数出来传进来（缺省 `0` = 没有新增，
    首次预检恒为 0）。`<= 0` 时一行都不加，输出与不带这个参数时逐字相同；负数、
    超大值同样不抛，口径同 `facts_intake` 的 `round_no`。

    「合计」取的是 `payload["customer_evidence"]` 的长度，**不取调用方另传的那个
    份数**：那两个值在触发侧同源，但这一层能看见的权威只有 payload（本函数本来读的
    就是它）。两处各取一个来源的症状是「受理岗说 2 份、证据岗说 3 份」，而两边都不报错。

    这一行还必须落在两条早返回之后 —— skill 没装载、或核验调用失败时连份数都核不了，
    那种时候报一句「本轮新增 1 份」只会让人以为核过了。
    """
    from maos.skills import registry

    if registry.get("refund.evidence_check") is None:
        return ("证据核验 skill 未装载（refund.evidence_check 不在注册表里），"
                "本单证据无法核验，随案材料请人工过目"), {"verdict": "unavailable"}

    res = _evidence_check(payload, checked, ledger)

    if res.status != "ok" or not isinstance(res.output, dict):
        return (f"证据核验失败：refund.evidence_check: {res.error or '出参不是 dict'}，"
                "本单证据请人工过目"), {"verdict": "unavailable", "error": res.error}

    out = dict(res.output)
    items = list(out.get("items") or [])
    gaps = list(out.get("gaps") or [])
    lines = [f"证据核验结论：{out.get('verdict')}"]
    if added >= 1:
        lines.append(f"本轮新增 {added} 份材料，"
                     f"随案证据合计 {len(payload.get('customer_evidence') or [])} 份")
    lines += [
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
    out["added_evidence"] = added
    # 缺什么材料，结构化地留在 data 里（不进文本：单案卡的措辞被 `test_roundtable_recheck`
    # 与冒烟基线逐字钉着）。发声面按它在本岗发言后面挂「上传材料」按钮。
    from maos.domain.refund import fixtures

    seed = fixtures.case_seed_of(payload)
    out["material_gaps"] = _material_gaps(
        out, order_id=str(seed.get("order_id") or ""),
        case_id=str(checked.get("case_id") or ""), line=None,
        reason_raw=reason_cn_of(str(seed.get("reason_code") or "")))
    return "\n".join(lines), out


def _evidence_check(payload: dict, checked: dict, ledger: dict | None):  # noqa: ANN201
    """跑一次 `refund.evidence_check`。**单案与整表共用这一份入参拼法** —— 两处各拼
    一份的症状是「/refund 说缺照片、读表说证据齐」，而两边各自都不报错。"""
    from maos.domain.refund import fixtures

    seed = fixtures.case_seed_of(payload)
    order_id = str(seed.get("order_id") or "")
    row = _order_row(ledger.get("order_snapshot") if ledger else None, order_id) \
        or _order_row(payload.get("order_snapshot"), order_id)
    order_json = _order_payload(row)
    order_facts = {k: order_json[k] for k in ("logistics", "qc_report") if k in order_json}

    return _invoke("refund_evidence", EVIDENCE_SKILL, {
        "case_seed": seed,
        "customer_evidence": payload.get("customer_evidence") or [],
        "rules": _rules_of(payload),
        "order_facts": order_facts,
        "requested_at": str(checked.get("requested_at") or ""),
    }, task_id="preview-evidence")


def _material_gaps(out: dict, *, order_id: str, case_id: str, line,   # noqa: ANN001
                   reason_raw: str) -> list[dict]:
    """这一单**缺什么材料**，给发声面挂按钮用。没缺就是空列表。

    只看 `verdict` 是 `missing` / `partial` 这两态（取值域见 skill 的
    `output_schema`）：`not_required` 是「不用交」，`complete` 是「交齐了」，
    两者都不该出现一个催人上传的按钮。`kinds` 取「规则要求的类型里还没交的」；
    类型都齐、只是份数不够时退回整个要求清单 —— 按钮总得告诉人传什么。
    """
    verdict = str(out.get("verdict") or "")
    if verdict not in ("missing", "partial"):
        return []
    have = {str(it.get("kind") or "") for it in (out.get("items") or [])
            if isinstance(it, dict) and it.get("ok")}
    required = [str(k) for k in (out.get("required_kinds") or [])]
    kinds = [k for k in required if k not in have] or required
    return [{
        "order_id": order_id, "case_id": case_id, "line": line,
        "reason_raw": reason_raw, "verdict": verdict, "kinds": kinds,
        "gaps": [str(g) for g in (out.get("gaps") or [])],
    }]


def _risk_screen(payload: dict, checked: dict, ledger: dict | None):  # noqa: ANN201
    """跑一次 `refund.risk_screen`。单案与整表共用，理由同 `_evidence_check`。"""
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

    return _invoke("refund_risk", RISK_SKILL, {
        "case_seed": seed,
        "order": order,
        "customer_orders": customer_orders,
        "refund_history": (ledger or {}).get("refund_history") or [],
        "requested_at": str(checked.get("requested_at") or ""),
    }, task_id="preview-risk")


def facts_risk(payload: dict, checked: dict, ledger: dict) -> tuple[str, dict]:
    """风险反欺诈岗：这个客户、这一单，有没有重复退款或异常频次。"""
    from maos.skills import registry

    if registry.get("refund.risk_screen") is None:
        return ("风险筛查 skill 未装载（refund.risk_screen 不在注册表里），"
                "本单风险未经筛查，放行前请人工看一眼客户历史"), {"level": "unavailable"}

    res = _risk_screen(payload, checked, ledger)

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
    数出五个不一样的「合法行数」，房间里没人知道该信哪个。

    **一行只落一个桶，三个桶加起来等于 total。** 契约 §1.4 的行有三态，
    不是两态（`router._sheet_rows` 的 docstring 是这条的出处）：

      · ``checked`` 是 dict        -> ``valid``：预检走通、有裁定
      · ``error`` 非空             -> ``invalid``：进了预检、抛错了
      · ``problems`` 非空          -> ``problem_rows``：**表就填错了，压根没进预检**

    第三态最容易被漏掉，因为它 ``payload`` / ``checked`` / ``error`` 三者都是 None。
    漏掉的症状是整表填错时 ``total=5`` 而 ``valid=invalid=0`` —— 三个数加不平，
    事实卡把「填错 0 行」念给房间，模型只能报「数字对不上」，后面四岗跟着推诿。
    ``problem_rows`` 显式排掉带 ``error`` 的行，是为了让这条不变式与入参无关地成立：
    上游哪天把两个字段同时填上，这里也不会一行数两遍。
    """
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    ok = [r for r in rows if not r.get("error") and isinstance(r.get("checked"), dict)]
    bad = [r for r in rows if r.get("error")]
    unfiled = [r for r in rows if not r.get("error") and r.get("problems")]
    approve = [r for r in ok if str((r["checked"] or {}).get("decision") or "") == "approve"]
    reject = [r for r in ok if str((r["checked"] or {}).get("decision") or "") == "reject"]
    return {
        "total": len(rows), "valid": len(ok), "invalid": len(bad),
        "approve": len(approve), "reject": len(reject),
        "problem_rows": len(unfiled),
        "warning_rows": len([r for r in rows if r.get("warnings")]),
        # 「需证据」= 会走到证据核验的行。裁定驳回的单子不进这一步。
        "need_evidence": len(approve),
        "pending_case_ids": [str((r["checked"] or {}).get("case_id") or "") for r in approve],
    }


def _pending_line(stats: dict) -> str:
    """待放行那一句。**超过 :data:`ROW_CAP` 单就不逐个念 case_id。**

    50 行的表跑下来是 46 个 `RC-ORD-…`，连成一行 900 多字符 —— 房间里读不完，
    更要紧的是它把「哪几单不能过、为什么」这类真问题挤到了看不见的地方。
    数字照报（放行要按它对账），清单指回申请表回帖：那一份是逐行全的，且带每单的判据。
    **只有规则岗念这一句**：待放行数是它的裁定结果，下游转抄一遍就成了复述（规则 2）。
    句尾不带「这里不重复」这类交代 —— 那是关于协作本身的元话语（规则 3）。
    """
    ids = stats["pending_case_ids"]
    if not ids:
        return "待放行 0 单：无"
    if len(ids) > ROW_CAP:
        return f"待放行 {len(ids)} 单：case_id 逐行在申请表回帖里"
    return f"待放行 {len(ids)} 单：{'、'.join(ids)}"


#: 逐条清单最多列几条。房间里是一条消息（Matrix 单事件 64 KB），50 行全列会把它撑爆，
#: 而读的人也读不完。超出的**说出来**并指回申请表回帖 —— 那一份是逐行全的。
ROW_CAP = 12


#: 截断尾句。上游两岗指回申请表回帖 —— 那份是逐行全的。
REPLY_TAIL = "  · 余下的同样在申请表回帖里逐行写着，这里不重复"
#: 下游三岗的截断尾句。**不指回申请表回帖**：那份回帖只有预检裁定，没有证据 /
#: 风险 / 核算的逐单结论，指过去是把人指向一个不存在的东西。逐单起单时
#: （`/refund`，或补材料触发复检）本岗会对那一单单独再说一遍，这才是真的去处。
DOWNSTREAM_TAIL = "  · 余下的这里不逐条列，单独起单（/refund 订单号 诉求）或补材料时本岗逐单再报"


def _clip(items: list[str], tail: str = REPLY_TAIL) -> list[str]:
    """给逐条清单加盖子。

    截掉的**不报数字**：那个差值不在入参里（本模块的数字必须是 rows 的子集，
    见 :func:`numbers_in`），为它多加一个计数器也不值 —— 人要的是
    「还有，去回帖里看」，不是又一个要对账的数。
    """
    if len(items) <= ROW_CAP:
        return items
    return items[:ROW_CAP] + [tail]


def _who(row: dict) -> str:
    """一行的身份：行号 + 订单号（+ 诉求原文）。三处都取原值，不补不猜。"""
    line = row.get("line")
    order = str(row.get("order_id") or "").strip() or "（无订单号）"
    reason = str(row.get("reason_raw") or "").strip()
    head = f"第 {line} 行 {order}" if line is not None else order
    return f"{head}（{reason}）" if reason else head


def _joined(values: object) -> str:
    return "；".join(str(x).strip() for x in (values or []) if str(x).strip())


def _problem_lines(rows: list[dict]) -> list[str]:
    """表格填错、压根没进预检的行 —— 逐条写清是哪一行、错在哪。

    与 `sheet_stats` 的 `problem_rows` 同一个筛法（排掉带 `error` 的），
    两处筛法不同的症状是「卡上说填错 3 行、底下只列出 2 行」。
    """
    out: list[str] = []
    for r in rows or []:
        if not isinstance(r, dict) or r.get("error") or not r.get("problems"):
            continue
        out.append(f"  · {_who(r)}：{_joined(r.get('problems'))}")
    return _clip(out)


def _error_lines(rows: list[dict]) -> list[str]:
    """进了预检才抛错的行。与上一条分开摆：表填得对、是我们这边没跑通，
    催人改表是把人指向一个不存在的问题。"""
    out: list[str] = []
    for r in rows or []:
        if not isinstance(r, dict) or not r.get("error"):
            continue
        out.append(f"  · {_who(r)}：{r['error']}")
    return _clip(out)


def _warning_lines(rows: list[dict]) -> list[str]:
    """带提示但不阻断的行。它们照走，但老板该知道 ——「申报超实付、核算按实付封顶」
    就是一句会改变到账金额的提示，只报「6 行带提示」等于没说。"""
    out: list[str] = []
    for r in rows or []:
        if not isinstance(r, dict) or not r.get("warnings"):
            continue
        out.append(f"  · {_who(r)}：{_joined(r.get('warnings'))}")
    return _clip(out)


def _rejects(rows: list[dict]) -> list[dict]:
    return [r for r in rows or []
            if isinstance(r, dict) and isinstance(r.get("checked"), dict)
            and str(r["checked"].get("decision") or "") == "reject"]


def _reject_lines(rows: list[dict]) -> list[str]:
    """裁定驳回的单 —— **逐条附上判据**。

    老板问的从来不是「几单不能过」，是「为什么这单不能过」。`checked["why"]`
    与 `checked["deciding_rule"]` 是 preflight 当时算出来的原话
    （如「AS-001@v1 窗口 30 天，第 55 天申请，55 > 30」），照搬不改写：
    这一层改写一次，房间里的说法就和申请表回帖对不上了。
    """
    out: list[str] = []
    for r in _rejects(rows):
        c = r["checked"]
        why = str(c.get("why") or "").strip()
        basis = str(c.get("deciding_rule") or "").strip()
        tail = f"{why}；依据 {basis}" if basis and why else (why or basis)
        out.append(f"  · {_who(r)}：{tail}" if tail else f"  · {_who(r)}：裁定驳回")
    return _clip(out)


def facts_sheet_intake(rows: list[dict]) -> tuple[str, dict]:
    """受理岗念**三态**：能建案 / 表格填错 / 预检失败，三个数加起来是总行数。

    「填错」取 `problem_rows`，**不是** `invalid` —— 后者是「进了预检才抛的错」。
    取错的症状不是崩：同一张卡上「填错 0 行」与「有填写问题 5 行」并排念出来，
    房间里没人知道该信哪个，而下一岗接话时只能说「口径不同」。
    """
    stats = sheet_stats(rows)
    lines = [
        f"这张表共 {stats['total']} 行：能建案 {stats['valid']} 行、"
        f"表格填错没进预检 {stats['problem_rows']} 行、预检失败 {stats['invalid']} 行",
        f"另有 {stats['warning_rows']} 行带提示（不阻断）",
        "填错的行不会进入后续环节，改好再拖一次表即可",
    ]
    # 计数之后**逐条写清是哪一行、错在哪**。只报数的症状（2026-09-10 的房间）：
    # 老板问「哪几单不行」，五岗只答得出一个数，人得自己回去翻回帖找 ——
    # 而这些行的问题本来就在入参里（`router._sheet_rows` 的 problems / error / warnings）。
    # 只列**有情况的**行：跑通的没什么可说，列出来只会把消息撑爆。
    problems = _problem_lines(rows)
    if problems:
        lines += ["", "表格填错、没进预检的行（改好再拖一次表）："] + problems
    errors = _error_lines(rows)
    if errors:
        lines += ["", "进了预检才失败的行（表没填错，是这边没跑通）："] + errors
    warned = _warning_lines(rows)
    if warned:
        lines += ["", "带提示但不阻断的行（照走，只是要知道）："] + warned
    return "\n".join(lines), stats


def facts_sheet_policy(rows: list[dict]) -> tuple[str, dict]:
    stats = sheet_stats(rows)
    lines = [
        f"按下单锁定的政策版本逐行裁定：{stats['valid']} 行有结论，"
        f"批准 {stats['approve']} 行、驳回 {stats['reject']} 行",
        # 「因填写问题没有裁定」的正主是 problem_rows。原来这里取 invalid，
        # 与受理岗那句用同一个词、却挂在另一个计数器上 —— 两张卡当场对不上。
        f"另有 {stats['problem_rows']} 行因表格填错没进预检、"
        f"{stats['invalid']} 行预检失败，这两类都没有裁定",
        _pending_line(stats),
    ]
    # 「为什么这单不能过」是老板真正要的那句，判据在 `checked` 里现成 —— 照搬。
    rejects = _reject_lines(rows)
    if rejects:
        lines += ["", "驳回的单，逐条判据："] + rejects
    return "\n".join(lines), stats


def _approved_rows(rows: list[dict]) -> list[dict]:
    """会走到证据核验与核算的行：预检走通、裁定批准、payload 在手。

    与 `sheet_stats["approve"]` 同一个筛法再多一条「payload 是 dict」：那是 skill
    的入参，没有它这一行跑不了。理论上两者同源（有 checked 必有 payload），
    多这一条只为让本函数对任何形状的 rows 都不抛。
    """
    return [r for r in rows or []
            if isinstance(r, dict) and isinstance(r.get("checked"), dict)
            and isinstance(r.get("payload"), dict)
            and str(r["checked"].get("decision") or "") == "approve"]


def _valid_rows(rows: list[dict]) -> list[dict]:
    """预检走通的行（批准 + 驳回），payload 在手。风险筛查的范围。"""
    return [r for r in rows or []
            if isinstance(r, dict) and not r.get("error")
            and isinstance(r.get("checked"), dict) and isinstance(r.get("payload"), dict)]


def facts_sheet_evidence(rows: list[dict], ledger: dict | None = None) -> tuple[str, dict]:
    """证据核验岗对一张表：**逐单真跑** `refund.evidence_check`，不再只报一个范围计数。

    2026-09-10 的房间：这一岗的事实卡只有「进入证据核验范围的有 8 单」一行，
    模型要么编出 8 单的逐单结论、要么（止血之后）念一句空话 —— 后面两岗排队说
    「证据岗还没出结论，我不替它说」，而证据岗自己根本没在算。skill 是确定性
    代码，12 行表整表逐单跑十几毫秒，没有理由不在读表这一刻就算完。

    `ledger` **必须带默认值**（跨轨契约 §3 的同一条理由）：`team.on_sheet` 之外的
    调用点仍按单参调用，缺默认值就是 `TypeError` 落进 router 的 except，
    症状是整个圆桌静默哑掉、回帖照发，没有任何测试会红。

    缺材料的单在 `data["material_gaps"]` 里结构化留一份（订单号、缺的类型、
    行号），发声面按它在本岗发言后面挂「上传材料」按钮 —— 缺口只写在文本里，
    按钮就得靠正则从文案里刮，而文案是会改的。
    """
    from maos.skills import registry

    stats = sheet_stats(rows)
    loaded = registry.get(EVIDENCE_SKILL) is not None
    # 分母（`need_evidence` / `reject`）取自规则岗已发布的同一份 `sheet_stats`，
    # 不在这一层重算 —— 口径对齐是 `SYSTEM_TMPL` 群内发言规则第 5 条。
    lines = [
        f"进入证据核验范围的有 {stats['need_evidence']} 单（裁定驳回的 {stats['reject']} 单不看证据）",
    ]
    data: dict = {**stats, "skill_loaded": loaded, "material_gaps": [],
                  "evidence_complete": 0, "evidence_gaps": 0,
                  "evidence_not_required": 0, "evidence_failed": 0}
    # 装载成功不写进事实卡：它是进度不是结论，念出来就是一条空状态帖（规则 4）。
    # 未装载相反 —— 「这一批没核验过」会改变主管的动作，属于本岗的结论，必须发。
    if not loaded:
        lines.append("证据核验 skill 未装载，这一批的证据无法核验，请人工过目")
        return "\n".join(lines), data

    gap_lines: list[str] = []
    cross_lines: list[str] = []
    fail_lines: list[str] = []
    for r in _approved_rows(rows):
        checked = r["checked"]
        res = _evidence_check(r["payload"], checked, ledger)
        if res.status != "ok" or not isinstance(res.output, dict):
            data["evidence_failed"] += 1
            fail_lines.append(f"  · {_who(r)}：核验失败（{res.error or '出参不是 dict'}），请人工过目")
            continue
        out = res.output
        verdict = str(out.get("verdict") or "")
        gaps = _material_gaps(out, order_id=str(r.get("order_id") or ""),
                              case_id=str(checked.get("case_id") or ""),
                              line=r.get("line"), reason_raw=str(r.get("reason_raw") or ""))
        if gaps:
            data["evidence_gaps"] += 1
            data["material_gaps"].extend(gaps)
            # 缺口那一句**照搬 skill 的原话**（与单案卡「缺口：…」同一份字），不改写。
            gap_lines.append(f"  · {_who(r)}：{'；'.join(gaps[0]['gaps']) or verdict}")
        elif verdict == "not_required":
            data["evidence_not_required"] += 1
        else:
            data["evidence_complete"] += 1
        bad = [str(c.get("note") or c.get("check") or "")
               for c in (out.get("consistency") or []) if isinstance(c, dict) and not c.get("ok")]
        if bad:
            cross_lines.append(f"  · {_who(r)}：{'；'.join(bad)}")

    tally = (f"逐单核验：证据齐 {data['evidence_complete']} 单、"
             f"缺材料 {data['evidence_gaps']} 单、无需举证 {data['evidence_not_required']} 单")
    if data["evidence_failed"]:
        tally += f"、核验没跑通 {data['evidence_failed']} 单"
    lines.append(tally)
    if gap_lines:
        lines += ["", "缺材料的单，逐条缺什么："] + _clip(gap_lines, DOWNSTREAM_TAIL)
    if cross_lines:
        lines += ["", "与订单事实交叉核对没对上的单："] + _clip(cross_lines, DOWNSTREAM_TAIL)
    if fail_lines:
        lines += ["", "核验没跑通的单："] + _clip(fail_lines, DOWNSTREAM_TAIL)
    return "\n".join(lines), data


def facts_sheet_risk(rows: list[dict], ledger: dict | None = None) -> tuple[str, dict]:
    """风险反欺诈岗对一张表：**逐单真跑** `refund.risk_screen`，中高风险的逐条点名。

    低风险的单只计数不点名 —— 12 行「低风险」在房间里是一堵墙，而人要看的是
    那几单不干净的。`ledger` 带默认值的理由同 `facts_sheet_evidence`。
    """
    from maos.skills import registry

    stats = sheet_stats(rows)
    loaded = registry.get(RISK_SKILL) is not None
    lines = [
        f"待筛查 {stats['valid']} 单，其中 {stats['approve']} 单已裁定批准、会走到付款",
    ]
    data: dict = {**stats, "skill_loaded": loaded, "flagged": [],
                  "risk_low": 0, "risk_medium": 0, "risk_high": 0, "risk_failed": 0}
    if not loaded:                                  # 理由同证据岗：只发未装载
        lines.append("风险筛查 skill 未装载，这一批未经风险筛查，放行前请人工看一眼客户历史")
        return "\n".join(lines), data

    flagged_lines: list[str] = []
    fail_lines: list[str] = []
    for r in _valid_rows(rows):
        res = _risk_screen(r["payload"], r["checked"], ledger)
        out = res.output if res.status == "ok" and isinstance(res.output, dict) else None
        level = str((out or {}).get("level") or "")
        if out is None or level not in LEVEL_CN:
            data["risk_failed"] += 1
            why = res.error if out is None else f"档位 {level or '空'} 不在低/中/高里"
            fail_lines.append(f"  · {_who(r)}：筛查失败（{why or '出参不是 dict'}），放行前请人工看一眼客户历史")
            continue
        data[f"risk_{level}"] += 1
        if level == "low":
            continue
        reasons = [str(x) for x in (out.get("reasons") or [])]
        data["flagged"].append({"line": r.get("line"), "order_id": str(r.get("order_id") or ""),
                                "case_id": str(r["checked"].get("case_id") or ""),
                                "level": level, "score": out.get("score"), "reasons": reasons})
        flagged_lines.append(f"  · {_who(r)}：{LEVEL_CN[level]}（评分 {out.get('score')}）"
                             f"—— {'；'.join(reasons) or '无逐条信号'}")

    tally = (f"逐单筛查：低风险 {data['risk_low']} 单、中风险 {data['risk_medium']} 单、"
             f"高风险 {data['risk_high']} 单")
    if data["risk_failed"]:
        tally += f"、筛查没跑通 {data['risk_failed']} 单"
    lines.append(tally)
    if flagged_lines:
        lines += ["", "中高风险的单，逐条信号（只提示，不改裁定）："] + _clip(flagged_lines, DOWNSTREAM_TAIL)
    if fail_lines:
        lines += ["", "筛查没跑通的单："] + _clip(fail_lines, DOWNSTREAM_TAIL)
    return "\n".join(lines), data


def facts_sheet_finance(rows: list[dict]) -> tuple[str, dict]:
    """财务执行岗对一张表：**逐单预演**（与单案 `facts_finance_preview` 同一段代码），
    再给整表合计。

    整表合计归本岗，且只在**所有**待核算的单都预演完才允许发（`SYSTEM_TMPL` 的
    字段归属表）：有一单没跑通，合计就是一个没算完的数，念进群会被当成已经算完的账。
    驳回的单不预演、不点名 —— 那份清单规则岗已经连着判据发过（规则 2）。
    """
    stats = sheet_stats(rows)
    lines = [
        f"需要核算的有 {stats['approve']} 单，驳回的 {stats['reject']} 单无需核算",
    ]
    previews: list[dict] = []
    preview_lines: list[str] = []
    fail_lines: list[str] = []
    total = Decimal(0)
    for r in _approved_rows(rows):
        _text, d = facts_finance_preview(r["payload"], r["checked"])
        amount = _dec(d.get("amount_approved")) if d.get("preview_ran") else None
        if amount is None:
            fail_lines.append(f"  · {_who(r)}：预演没跑通（{d.get('error') or '没算出金额'}）")
            continue
        total += amount
        capped = bool((d.get("breakdown") or {}).get("capped_by_paid"))
        previews.append({"line": r.get("line"), "order_id": str(r.get("order_id") or ""),
                         "case_id": str(r["checked"].get("case_id") or ""),
                         "amount": _money(amount), "capped": capped})
        preview_lines.append(f"  · {_who(r)}：{_money(amount)}{'（已按实付封顶）' if capped else ''}")

    # 键名不叫 `total`：`sheet_stats` 的 `total` 是行数，五岗共用；金额另起一个名。
    data: dict = {**stats, "previews": previews, "preview_failed": len(fail_lines),
                  "amount_total": _money(total) if previews and not fail_lines else None}
    if preview_lines:
        lines += ["", "逐单核算预演，应退金额："] + _clip(preview_lines, DOWNSTREAM_TAIL)
    if fail_lines:
        lines += ["", "预演没跑通的单："] + _clip(fail_lines, DOWNSTREAM_TAIL)
    if data["amount_total"] is not None:
        lines.append(f"整表合计（{len(previews)} 单预演完）：{data['amount_total']}")
    elif fail_lines:
        lines.append("整表合计：有单预演没跑通，合计不出")
    if previews:
        lines.append(PREVIEW_WORDING)
    return "\n".join(lines), data


#: 数字白名单的取数口径。测试按它断言「facts 里的数字都能在入参里找到」，
#: `speaker.Speaker.speak` 的两道门也按它比对 speech 与 facts。
#: 放在这里而不是测试文件里，是为了让口径和事实卡长在同一个文件 —— 加一行事实卡
#: 却忘了它的数字从哪来，改这里的时候就会被问一次。

#: 标识符模式。**从长到短**：`RC-ORD-2026-1000` 含 `ORD-2026-1000`，短的先匹配会切错。
#: `ORD-\d{4}-\d{4}` 既认本表的单号，也认模型编出来的历史单（实测语料里有 `ORD-2025-0887`）。
#: 单独抽成一集是因为它在数字维度上根本不可判：`AS-001@v1` 按数字只出 `{1}`
#: —— `001` 与 `v1` 都塌成 `Decimal(1)`，而 1 几乎必然已在任何一张事实卡里。
ID_RE = re.compile(r"RC-ORD-\d{4}-\d{4}|ORD-\d{4}-\d{4}|AS-\d{3}@v\d")

#: 中文数字。「两」= 2 是口语里最常见的写法（「两行」「两单」），漏了它这道门
#: 就能被一句「另外两单」整条绕过。
_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}

#: 中文数字**只有跟着量词、且前面不是指示词时**才算数。
#:
#: 两侧都收是实测逼出来的：一句再自然不过的「这一批 12 单」里，「一」会被当成
#: 一个事实卡里没有的数字 1，整条发言被拦 —— 门就成了误报机。「一并」「一律」
#: 「第一次」同理。收紧的代价是漏掉「共十二」这种不带量词的写法，认了：
#: 这道门是永久的，误报会把房间打回念事实卡，而漏一个形态只是少拦一次。
_CN_QUANTIFIER = "行单条份次天元个项笔张家批轮遍"
_CN_RUN = re.compile(
    "(?<![这那哪某第每整统唯])"
    "[" + "".join(_CN_DIGITS) + "".join(_CN_UNITS) + "]+"
    "(?=[" + _CN_QUANTIFIER + "])")

#: 千分位逗号。`4,038.78` 不去逗号会被切成 `{4, 38.78}` —— 凭空造出两个
#: 事实卡里没有的数，模型只要加个逗号就能把门变成一台误报机。
_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3})")

#: 阿拉伯数字接中文单位：「4 千」是 4000，不是 4 和 1000 两个数。
#: 不单独处理的症状是凭空多出一个 1000，而那正好是门要拦的东西。
_MIXED_UNIT = re.compile(r"(\d+(?:\.\d+)?)\s*([十百千])")


def _cn_to_int(run: str) -> int | None:
    """一串中文数字转整数。覆盖到「千」，再大的写法（万、亿）当前语料里没有。"""
    total = digit = 0
    seen = False
    for ch in run:
        if ch in _CN_DIGITS:
            digit, seen = _CN_DIGITS[ch], True
        else:
            # 「十二」= 12：单位前面没数字时按 1 算。
            total += (digit or 1) * _CN_UNITS[ch]
            digit, seen = 0, True
    return (total + digit) if seen else None


def _normalize(text: str) -> str:
    """去千分位、混合单位算成一个数、中文数字转阿拉伯。三步都在抽数字**之前**做。

    顺序不可换：混合单位那步要在纯中文数字之前，否则「4 千」的「千」会先被
    单独转成 1000，剩下一个孤零零的 4。
    """
    out = _THOUSANDS.sub("", text or "")
    out = _MIXED_UNIT.sub(
        lambda m: _money(Decimal(m.group(1)) * _CN_UNITS[m.group(2)]), out)
    return _CN_RUN.sub(lambda m: str(_cn_to_int(m.group()) or ""), out)


def numbers_in(text: str) -> tuple[set[str], set[Decimal]]:
    """一段文本里的 `(标识符, 数字)`。`6800` / `6800.0` / `6800.00` 视作同一个数。

    **数字集里剔掉标识符自己的数字**：两集各管一件事，`ORD-2026-1016` 只在
    `ids` 里出现一次，不再往 `nums` 里塞 `2026` 与 `1016`。不剔的症状是同一次
    越界被两道子门各报一遍，而读日志的人分不清究竟是引了单号还是引了金额。
    """
    found = set(ID_RE.findall(text or ""))
    rest = ID_RE.sub(" ", text or "")
    nums: set[Decimal] = set()
    for token in re.findall(r"\d+(?:\.\d+)?", _normalize(rest)):
        d = _dec(token)
        if d is not None:
            nums.add(d)
    return found, nums
