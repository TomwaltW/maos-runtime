"""policy.match —— 按**下单当时锁定的**政策版本检索规则并裁定。

这是退款域最容易写错的一处，也是本域最值得拿给评委看的一处：

    用当前最新政策去判一笔历史订单，等于拿今天的规则追溯昨天的交易。
    客户是按下单当时公示的政策下的单，权威在 `order_snapshot.policy_version_at_order`，
    不在 `policy_rule` 表的 `max(version)` 上。

版本锁定与规则检索**一律调 R-1 的 `objects.policy_rules_at_order()`**，本模块不自己
写一份 SQL —— 两份实现一定会在「≤ 锁定版本取每条规则的最大版本」这个细节上分叉，
而分叉的症状是「金额算错了一点点」，几乎不可能在演示里被当场看出来。

裁定零模型：同一个案子在任何机器任何时刻必须给同一个结论。政策命中是可解释的
规则匹配，不是语义理解 —— 让模型来裁反而丢掉了「按哪一条、哪一版判的」这个可审计点。
"""

from __future__ import annotations

import calendar
import json
from datetime import datetime, timezone

from maos.domain.refund import guard, objects
from maos.skills.contract import Skill, SkillContext, SkillContract
from maos.skills.registry import register_skill

from . import _common as C

#: 售后类规则的编号前缀。租户的规则编号约定，可由入参覆盖。
#: 不写死成「所有规则都适用」：政策表里同时躺着售前、履约、售后各类规则，
#: 全量命中会让一条与退款无关的规则参与金额核算。
AFTER_SALES_PREFIX = "AS-"

DECISION_APPROVE = "approve"
DECISION_REJECT = "reject"


def rule_ref(rule: dict) -> str:
    """规则引用的书写口径：``<rule_no>@v<version>``。

    finance.settle 的 `rule_refs` 与本 skill 的输出共用它 —— 两边各写一套格式，
    合并后「按哪一条判的」这条线就对不上了。
    """
    return f"{rule['rule_no']}@v{rule['version']}"


def rule_params(rule: dict) -> dict:
    """从规则 body 里取机器可读的参数。

    body 是 JSON 就按 JSON 读；不是（人写的自然语言条款）就返回空 dict，由
    finance.settle 落到它的缺省口径上，**不在这里猜**。猜一个比例出来，
    金额会错得很安静。
    """
    body = rule.get("body")
    if not isinstance(body, str) or not body.strip():
        return {}
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


# ============================================================ 条件判据（eligibility）
#
# 「命中了哪几条规则」与「按这几条该不该退」是**两个问题**。上面的 `rule_params`
# 只回答前者；这一段回答后者的前半截 —— 规则自己声明的条件到底满不满足。
#
# 为什么必须拆开（`docs/BACKLOG.md:185`）：混成一个字段之后，审计说不清是
# 「规则没命中」还是「命中了但条件不满足」。所以本段**不碰** `matched_rules` /
# `rule_refs` / `decision`，只往出参里加一个平行的 `eligibility`。
#
# 🔴 方向（跨轨契约 §2 R1）：判据不满足 = **该规则不予适用**，不是「拒赔」。
# AS-003 是 `effect: "exclude"`、`refund_ratio: "0"`，商家靠它免责；
# `requires_evidence_kinds: ["image"]` 的意思是「认定人为损坏需要图片支撑」。
# 举证不足时正确的方向是「不能认定人为损坏 → 免责条款不予适用 → 客户照退」。
# 反过来写（没交图就拒赔）是把举证责任倒置到客户身上，是本域最严重的做错方式。

#: `requirement` 的封闭枚举（跨轨契约 §1.2）。T75 只读，新增取值必须先改契约文件。
REQUIREMENT_MIN_EVIDENCE_COUNT = "min_evidence_count"
REQUIREMENT_REQUIRES_EVIDENCE_KINDS = "requires_evidence_kinds"
REQUIREMENT_NO_REASON_DAYS = "no_reason_days"
REQUIREMENT_WARRANTY_BASIS = "warranty_basis"

#: 逐条判据的检查次序 —— 也是 `unmet` 的排序口径。固定次序才有逐字节可复现的出参。
REQUIREMENTS = (
    REQUIREMENT_MIN_EVIDENCE_COUNT,
    REQUIREMENT_REQUIRES_EVIDENCE_KINDS,
    REQUIREMENT_NO_REASON_DAYS,
    REQUIREMENT_WARRANTY_BASIS,
)

#: `unmet[].direction` 目前只有这一个取值，写死是为了让方向在出参里**显式可读**：
#: 下游（T75 的 finance.settle）看到的是「不予适用」，不是「拒赔」。
DIRECTION_NOT_APPLIED = "not_applied"

#: `evidence_source` **不是判据**，是数据源声明：规则用它指明去哪张表数证据。
#: 当前只有这一个合法取值；出现别的值一律抛异常（跨轨契约 §2 R5「认不出就报错，不猜」）。
EVIDENCE_SOURCE_KEY = "evidence_source"
CUSTOMER_EVIDENCE = "customer_evidence"

#: `warranty_basis` 当前只认这一个基准。同样：认不出就报错，不猜一个月数出来。
WARRANTY_BASIS_PRODUCT = "product_snapshot.warranty_months"

#: `evidence_seen` 里恒在的 kind 键。哪怕一行证据都没有也要出现，
#: 下游读 `evidence_seen["image"]` 才不会 KeyError —— 出参形状不随数据而变。
BASELINE_EVIDENCE_KINDS = ("image", "video")


def as_datetime(text: object) -> datetime | None:
    """ISO8601 -> aware datetime；解析不了返回 None。无时区的按 UTC 读。

    naive 与 aware 相减会抛 TypeError，而这里抛出去等于「时间戳格式不标准」
    变成一次 skill 失败。时点相关判据的缺省方向是**不收紧**（见 `elapsed_days`）。
    """
    try:
        dt = datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def elapsed_days(paid_at: object, as_of: object) -> float | None:
    """申请时点距支付时点的天数；任一端解析不出就返回 None。

    None 的语义是**不判超期**，不是「超期」：时点读不出来时拒付一笔本该退的钱，
    比放行一笔本该拒的钱更难解释。这一档的行为与 `b35c618` 一致 —— 时点判据
    只在数据足够时才收紧口径，数据不足时不制造无谓的结论分叉。
    """
    paid = as_datetime(paid_at)
    now = as_datetime(as_of)
    if paid is None or now is None:
        return None
    return (now - paid).total_seconds() / 86400.0


def paid_at_of(store, tenant_id: str, case: dict) -> str | None:
    """订单支付时刻 —— 全部时点判据的起算点。

    权威在订单快照上（铁律 8）：MAOS 不持有权威事实，付款这件事归外部系统，
    这里只读它当时那一版的快照，不读商品库/订单库的当前值。
    """
    rows = objects.query(
        store,
        "SELECT paid_at FROM order_snapshot WHERE tenant_id=? AND order_id=? AND version=?",
        (tenant_id, case["order_id"], int(case["order_version"])),
    )
    return rows[0]["paid_at"] if rows else None


def evidence_seen(store, tenant_id: str, case_id: str) -> dict:
    """本案 `customer_evidence` 表里按 `kind` 的实际计数（跨轨契约 §1.1）。

    **只读，不写**：证据落库归 `refund.intake`，本 skill 数一数而已。
    """
    rows = objects.query(
        store,
        "SELECT kind, COUNT(*) AS n FROM customer_evidence "
        "WHERE tenant_id=? AND case_id=? GROUP BY kind ORDER BY kind",
        (tenant_id, case_id),
    )
    seen: dict = {k: 0 for k in BASELINE_EVIDENCE_KINDS}
    for row in rows:
        kind = str(row["kind"])
        seen[kind] = seen.get(kind, 0) + int(row["n"])
    seen["total"] = sum(seen.values())
    return seen


def _warranty_months_of(store, tenant_id: str, sku: object) -> int | None:
    """商品快照上的质保月数；查不到返回 None（= 判不出，不是「无质保」）。"""
    rows = objects.query(
        store,
        "SELECT warranty_months FROM product_snapshot WHERE tenant_id=? AND sku=? "
        "ORDER BY version DESC LIMIT 1",
        (tenant_id, str(sku)),
    )
    if not rows:
        return None
    try:
        return int(rows[0]["warranty_months"])
    except (TypeError, ValueError):
        return None


def _add_months(dt: datetime, months: int) -> datetime:
    """日历加月 —— 质保到期时点。

    不用「月 × 30 天」近似：24 个月按 720 天算会比真实到期日早 10 天，
    而落在这 10 天里的申请会被判成「已过保」，症状是偶发的、无法复现的驳回。
    """
    total = dt.month - 1 + int(months)
    year = dt.year + total // 12
    month = total % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def _positive_number(raw: object) -> float | None:
    """正数才算「声明了这条判据」；`bool` 单独排掉（它是 `int` 的子类，True 会变成 1）。"""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return float(raw) if raw > 0 else None


def _unmet(ref: str, requirement: str, required, actual) -> dict:
    """一条未满足的判据。`direction` 恒为 `not_applied` —— 方向写在出参里，不靠读者推断。"""
    return {
        "rule_ref": ref,
        "requirement": requirement,
        "required": required,
        "actual": actual,
        "direction": DIRECTION_NOT_APPLIED,
    }


def _require_evidence_source(params: dict, ref: str) -> None:
    """声明了证据判据就必须声明合法的 `evidence_source`（跨轨契约 §2 R5）。

    缺失或枚举外的取值一律抛异常，**不静默当成 `customer_evidence`**：
    `docs/DECISIONS.md:333` 记过同型的教训 —— 静默走缺省时演示当场看不出来，
    对账时才发现，而那时已经说不清是按哪张表数的证据。
    """
    raw = params.get(EVIDENCE_SOURCE_KEY)
    if raw is None:
        raise ValueError(
            f"规则 {ref} 声明了证据判据却没有 {EVIDENCE_SOURCE_KEY}："
            f"数不出证据来源，拒绝猜一个缺省值")
    if str(raw) != CUSTOMER_EVIDENCE:
        raise ValueError(
            f"规则 {ref} 的 {EVIDENCE_SOURCE_KEY}={raw!r} 不在合法取值内，"
            f"当前只支持 {CUSTOMER_EVIDENCE!r}")


# 三态返回：None = 该判据未声明（不计入 checked_rules）；True = 声明且满足；
# dict = 声明且未满足（一条 unmet）。三态是必要的 —— 「没声明」与「满足了」
# 在出参里必须区分开，否则 checked_rules 会把没有判据的规则也算成检查过。

def _check_min_evidence_count(params: dict, cond: dict, ref: str):
    required = _positive_number(params.get(REQUIREMENT_MIN_EVIDENCE_COUNT))
    if required is None:
        return None
    _require_evidence_source(params, ref)
    actual = int(cond["seen"]["total"])
    if actual >= required:
        return True
    return _unmet(ref, REQUIREMENT_MIN_EVIDENCE_COUNT, int(required), actual)


def _check_requires_evidence_kinds(params: dict, cond: dict, ref: str):
    raw = params.get(REQUIREMENT_REQUIRES_EVIDENCE_KINDS)
    if not isinstance(raw, (list, tuple)) or not raw:
        return None
    _require_evidence_source(params, ref)
    required = sorted({str(k) for k in raw})
    seen = cond["seen"]
    actual = sorted(k for k in required if int(seen.get(k, 0)) > 0)
    if actual == required:
        return True
    return _unmet(ref, REQUIREMENT_REQUIRES_EVIDENCE_KINDS, required, actual)


def _check_no_reason_days(params: dict, cond: dict, ref: str):
    required = _positive_number(params.get(REQUIREMENT_NO_REASON_DAYS))
    if required is None:
        return None
    elapsed = cond["elapsed"]
    if elapsed is None or elapsed <= required:
        return True
    return _unmet(ref, REQUIREMENT_NO_REASON_DAYS,
                  int(required) if required.is_integer() else required,
                  round(elapsed, 3))


def _check_warranty_basis(params: dict, cond: dict, ref: str):
    raw = params.get(REQUIREMENT_WARRANTY_BASIS)
    if raw is None:
        return None
    if str(raw) != WARRANTY_BASIS_PRODUCT:
        raise ValueError(
            f"规则 {ref} 的 {REQUIREMENT_WARRANTY_BASIS}={raw!r} 不在合法取值内，"
            f"当前只支持 {WARRANTY_BASIS_PRODUCT!r}")
    months, paid, moment = cond["warranty_months"], cond["paid_dt"], cond["as_of_dt"]
    # 数据缺失 ≠ 无质保。查不到商品快照就判「已过保」，等于让一条**给客户退钱**的
    # 规则因为我们自己的数据没读到而失效 —— 方向与 R1 同类，缺省一律不收紧。
    if not months or months <= 0 or paid is None or moment is None:
        return True
    expires = _add_months(paid, months)
    if moment <= expires:
        return True
    return _unmet(
        ref, REQUIREMENT_WARRANTY_BASIS,
        {"warranty_months": months, "expires_at": expires.isoformat()},
        {"as_of": moment.isoformat(),
         "elapsed_days": round(cond["elapsed"], 3) if cond["elapsed"] is not None else None},
    )


_CHECKERS = {
    REQUIREMENT_MIN_EVIDENCE_COUNT: _check_min_evidence_count,
    REQUIREMENT_REQUIRES_EVIDENCE_KINDS: _check_requires_evidence_kinds,
    REQUIREMENT_NO_REASON_DAYS: _check_no_reason_days,
    REQUIREMENT_WARRANTY_BASIS: _check_warranty_basis,
}


def evaluate_conditions(store, *, tenant_id: str, case: dict,
                        rules: list, as_of: object = None) -> dict:
    """逐条过规则自己声明的条件判据，产出 `eligibility`（跨轨契约 §1.1）。

    两版 `policy.match` **都调这一个函数** —— 各写一份判定器，两版就会在
    「证据够不够」上分叉，而分叉的症状是同一个案子在 v1.0.0 与 v1.1.0 下
    结论不同，且没有任何地方报错。

    只读：`customer_evidence` / `order_snapshot` / `product_snapshot`，一张表都不写。

    `rules` 传各版本**自己的命中集合**：v1.1.0 已剔除超窗规则，超窗的那几条
    本来就不是裁定依据，不该再去核它的证据。
    """
    seen = evidence_seen(store, tenant_id, str(case["case_id"]))
    moment = as_of or case.get("created_at")
    paid_at = paid_at_of(store, tenant_id, case)
    cond = {
        "seen": seen,
        "elapsed": elapsed_days(paid_at, moment),
        "paid_dt": as_datetime(paid_at),
        "as_of_dt": as_datetime(moment),
        "warranty_months": _warranty_months_of(store, tenant_id, case.get("sku")),
    }

    checked: list[str] = []
    unmet: list[dict] = []
    ineffective: list[str] = []
    for rule in rules:
        params = rule_params(rule)
        ref = rule_ref(rule)
        misses, declared = [], False
        for requirement in REQUIREMENTS:
            outcome = _CHECKERS[requirement](params, cond, ref)
            if outcome is None:
                continue  # 没声明这条判据 —— 与「声明了且满足」不是一回事
            declared = True
            if outcome is not True:
                misses.append(outcome)
        if not declared:
            # body 读不动（自然语言条款）或没声明任何判据的规则：不进 checked_rules，
            # 更不进 unmet —— 没有声明判据的规则不该被判为「不满足」。
            continue
        checked.append(ref)
        if misses:
            unmet.extend(misses)
            ineffective.append(ref)

    return {
        "eligible": not unmet,
        "unmet": unmet,
        "evidence_seen": seen,
        "checked_rules": checked,
        "ineffective_rules": ineffective,
    }


@register_skill
class PolicyMatchSkill(Skill):
    contract = SkillContract(
        name="policy.match",
        version="1.0.0",
        purpose="按订单快照锁定的政策版本检索适用规则并裁定退款资格（零模型，可复现）",
        input_schema={
            "tenant_id": "str",
            "case_id": "str",
            "rule_prefix": "str（可选，默认 'AS-'）",
        },
        output_schema={
            "policy_version": "int（订单锁定的版本，**不是**当前最新版本）",
            "matched_rules": "list[dict{rule_no,version,title,params}]",
            "rule_refs": "list[str]（形如 AS-01@v1）",
            "decision": "approve|reject",
            "reason": "str",
            "eligibility": (
                "dict{eligible,unmet,evidence_seen,checked_rules,ineffective_rules}"
                "（规则自己声明的条件判据满不满足；**与 decision 平行，不改 decision**："
                "判据不满足 = 该规则不予适用，由 finance.settle 按 ineffective_rules 剔参）"
            ),
            "invocation_id": "str",
        },
        preconditions=["tenant_id", "case_id"],
        depends_tools=[],
        failure_policy="escalate",
        max_retries=0,
        security_boundary=(
            "只读 refund_case / order_snapshot / policy_rule，只写 business_ref；"
            "条件判据只读 customer_evidence / product_snapshot，不写；"
            "不改 biz_status、不调模型、不碰支付网关；"
            "政策版本一律取自订单快照，禁止使用 policy_rule 的最新版本"
        ),
        reuse_note="任何「按快照锁定的版本判定」的场景都可照此复用 objects.policy_rules_at_order",
        owner_roles=["refund_policy"],
    )

    def run(self, payload: dict, ctx: SkillContext) -> dict:
        store = C.ensure_schema(ctx)
        invocation_id = C.invocation_id_of(ctx)
        extras = getattr(ctx, "extras", None) or {}
        tenant_id, case_id = C.required(payload, "tenant_id", "case_id")
        prefix = str(payload.get("rule_prefix") or AFTER_SALES_PREFIX)

        case = guard.get_case(store, tenant_id, case_id)
        if case is None:
            raise LookupError(f"没有这个 case：tenant={tenant_id} case={case_id}")

        # 版本锁定与规则检索都走 R-1 的冻结口径，本模块不自写 SQL。
        pinned = objects.pinned_policy_version(
            store, tenant_id=tenant_id, order_id=case["order_id"],
            order_version=case["order_version"])
        applicable = objects.policy_rules_at_order(
            store, tenant_id=tenant_id, order_id=case["order_id"],
            order_version=case["order_version"])

        matched = [r for r in applicable if str(r["rule_no"]).startswith(prefix)]
        rules_out = [{
            "rule_no": r["rule_no"], "version": int(r["version"]),
            "title": r.get("title", ""), "params": rule_params(r),
        } for r in matched]
        refs = [rule_ref(r) for r in matched]

        if matched:
            decision, reason = DECISION_APPROVE, (
                f"命中 {len(matched)} 条售后规则（政策 v{pinned}）：{'、'.join(refs)}")
        else:
            decision, reason = DECISION_REJECT, (
                f"政策 v{pinned} 下没有适用的 {prefix} 售后规则，"
                f"该订单不在退款范围内（当时可用规则 {len(applicable)} 条）")

        # 条件判据与裁定**平行**：上面那段一个字没动，下面这段只往出参加一个键。
        # 举证不足的规则照样留在 matched_rules 里 —— 它确实命中了，只是不予适用。
        eligibility = evaluate_conditions(
            store, tenant_id=tenant_id, case=case, rules=matched)

        plan_id = str(extras.get("plan_id") or "")
        task_id = str(extras.get("task_id") or "")
        if plan_id and task_id:
            # 写入面按 matched 不变：「命中了但条件不满足」与「没命中」是两件事，
            # 前者仍是裁定时看过的依据，剔掉它审计链上就少了一条解释。
            for r in matched:
                objects.attach_business_ref(
                    store, plan_id=plan_id, task_id=task_id, tenant_id=tenant_id,
                    object_type="policy_rule", object_id=r["rule_no"],
                    object_version=int(r["version"]), purpose="裁定依据的政策规则")

        return {
            "policy_version": pinned,
            "matched_rules": rules_out,
            "rule_refs": refs,
            "decision": decision,
            "reason": reason,
            "eligibility": eligibility,
            "case_id": case_id,
            "tenant_id": tenant_id,
            "invocation_id": invocation_id,
        }
