"""finance.settle —— 按命中的政策规则核算退款金额，写 finance_entry。

两件事必须**同时**做完（跨轨冻结契约 F-1）：

  1. 写 `finance_entry` 表那一行；
  2. 产出的 artifact 其 `content` 带 `finance_entry` 键，值就是那一行。

缺任何一件，R-0 的第六道财务复核闸都会判错 —— 而且是**合并后**才发作：
一边按 `business_ref` 查表判、一边按 artifact content 判，两轨各自都绿，
合到一起闸恒 blocker 或恒 pass，症状要跑到场景 6 才出现。所以这两件事写在
同一个 return 里，中间不留分支。

金额一律走 `Decimal`，不进浮点：`6800 * 0.85` 在二进制浮点下是 5779.999999999999，
四舍五入成分位后大多数时候看不出来，直到某个数字恰好落在半分上 —— 那是财务对账
最难查的一类差异。`finance_entry.amount_approved` 落库时才转成 float（列是 REAL），
breakdown 里保留字符串原值，审计时看到的是算式本身。
"""

from __future__ import annotations

import json
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from maos.domain.refund import guard, objects
from maos.skills.contract import Skill, SkillContext, SkillContract
from maos.skills.registry import register_skill

from . import _common as C
from .policy import DECISION_APPROVE

CENT = Decimal("0.01")

#: 规则没给参数时的缺省口径：全额退、不扣费。
#: 写死成「全额」而不是「按某个默认比例打折」—— 少退是要赔的，多退是可追的，
#: 而一个凭空猜出来的比例两头都解释不了。
DEFAULT_RATIO = Decimal("1")
DEFAULT_FEE = Decimal("0")


def _dec(value, default: Decimal) -> Decimal:
    """把规则参数收敛成 Decimal。转不动就用缺省，不抛 —— 政策 body 是人维护的。"""
    if value is None:
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default


def _q(value: Decimal) -> Decimal:
    """分位四舍五入。ROUND_HALF_UP 是财务口径，不用 Python 默认的银行家舍入。"""
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


@register_skill
class FinanceSettleSkill(Skill):
    contract = SkillContract(
        name="finance.settle",
        version="1.0.0",
        purpose="按命中的政策规则核算退款金额，写 finance_entry 表并产出带 finance_entry 键的产物",
        input_schema={
            "tenant_id": "str",
            "case_id": "str",
            "policy": "dict（policy.match 的出参：decision / matched_rules / rule_refs）",
        },
        output_schema={
            "finance_entry": "dict（= 写进 finance_entry 表那一行，F-1 判据）",
            "breakdown": "dict（核算过程，金额为字符串）",
            "rule_refs": "list[str]",
            "invocation_id": "str",
        },
        preconditions=["tenant_id", "case_id", "policy"],
        depends_tools=[],
        failure_policy="escalate",
        max_retries=0,
        security_boundary=(
            "只读 refund_case / order_snapshot，只写 finance_entry；"
            "不改 biz_status、不调模型、不碰支付网关；"
            "金额只按政策规则参数计算，不接受调用方直接指定 amount_approved"
        ),
        reuse_note="F-1：产出的 content 必带 finance_entry 键，且与库表同一份数据",
        owner_roles=["refund_finance"],
    )

    #: `eligibility.unmet[*].requirement` 的封闭取值域（跨轨契约 §1.2 定义，本轨只读）。
    #: 枚举外的取值一律抛错而不是忽略：忽略会让「政策侧新加了一类条件判据」表现成
    #: 「金额悄悄按老口径算了」—— 而金额算错要到对账时才暴露。
    #: 写成类属性而不是模块常量，是为了不把本类的定义行推下去 —— `docs/skill-catalog.md`
    #: 是从代码生成的投影，里面记着这一行的行号，而三份生成物本轮由整合轮统一重跑。
    REQUIREMENT_KINDS = frozenset({
        "min_evidence_count",
        "requires_evidence_kinds",
        "no_reason_days",
        "warranty_basis",
    })

    def run(self, payload: dict, ctx: SkillContext) -> dict:
        store = C.ensure_schema(ctx)
        invocation_id = C.invocation_id_of(ctx)
        tenant_id, case_id = C.required(payload, "tenant_id", "case_id")

        policy = payload.get("policy")
        if not isinstance(policy, dict):
            raise ValueError(
                f"finance.settle 入参 policy 必须是 policy.match 的出参 dict，"
                f"实际 {type(policy).__name__}")
        if policy.get("decision") != DECISION_APPROVE:
            # 裁定不通过就不核算。给一个 0 元的 finance_entry 会让下游误以为
            # 「核算过了，只是金额为零」，而事实是这笔根本不该进付款环节。
            raise ValueError(
                f"政策裁定为 {policy.get('decision')!r}，不予核算："
                f"{policy.get('reason') or '无适用售后规则'}")

        case = guard.get_case(store, tenant_id, case_id)
        if case is None:
            raise LookupError(f"没有这个 case：tenant={tenant_id} case={case_id}")

        rows = objects.query(
            store,
            "SELECT amount_paid FROM order_snapshot WHERE tenant_id=? AND order_id=? AND version=?",
            (tenant_id, case["order_id"], int(case["order_version"])))
        if not rows:
            raise LookupError(
                f"没有订单快照 tenant={tenant_id} order={case['order_id']} "
                f"v{case['order_version']}，金额无从核算")

        claimed = Decimal(str(case["amount_claimed"]))
        paid = Decimal(str(rows[0]["amount_paid"]))

        # 诉求金额不得超过实付：客户可以少要，不能多要。上限取实付而不是报错 ——
        # 多写一位数是常见笔误，按上限收敛并在 breakdown 里写清楚，比直接失败可用。
        base = min(claimed, paid)
        ratio, fee, applied, excluded = self._params_of(policy)
        gross = _q(base * ratio)
        approved = _q(max(gross - fee, Decimal("0")))

        rule_refs = [str(r) for r in (policy.get("rule_refs") or [])]
        breakdown = {
            "amount_claimed": str(_q(claimed)),
            "amount_paid": str(_q(paid)),
            "base": str(_q(base)),
            "capped_by_paid": claimed > paid,
            "refund_ratio": str(ratio),
            "gross": str(gross),
            "deduct_fee": str(_q(fee)),
            "amount_approved": str(approved),
            "policy_version": policy.get("policy_version"),
            "applied_rules": applied,
            "excluded_rules": excluded,
        }

        entry = {
            "tenant_id": tenant_id,
            "case_id": case_id,
            "amount_approved": float(approved),
            "breakdown_json": json.dumps(breakdown, ensure_ascii=False, sort_keys=True),
            "rule_refs": json.dumps(rule_refs, ensure_ascii=False),
            "checked_by": getattr(getattr(ctx, "identity", None), "agent_id", "") or
                          self.contract.name,
            "checked_at": C.now_iso(),
        }

        # 库表与产物同一份数据：下面这两处都用 entry，谁也不许各造一份（F-1 反例）。
        objects.execute(
            store,
            "INSERT OR REPLACE INTO finance_entry (tenant_id, case_id, amount_approved,"
            " breakdown_json, rule_refs, checked_by, checked_at) VALUES (?,?,?,?,?,?,?)",
            (entry["tenant_id"], entry["case_id"], entry["amount_approved"],
             entry["breakdown_json"], entry["rule_refs"], entry["checked_by"],
             entry["checked_at"]),
        )

        return {
            "finance_entry": entry,
            "breakdown": breakdown,
            "rule_refs": rule_refs,
            "amount_approved": str(approved),
            "invocation_id": invocation_id,
        }

    # ------------------------------------------------------------------
    @staticmethod
    def _reason_of(item: dict) -> str:
        """把一条 `unmet` 渲染成人读的排除原因。

        **只搬 `unmet` 里已有的字段，本轨一个判据都不重算。** 两边各算一套的话，
        「这条为什么被排除」在整合后就会分叉：政策侧说差一张图，金额侧说差两天，
        对账的人无从判断哪一边是对的。
        """
        return (f"{item.get('requirement')} {item.get('required')} "
                f"> actual {item.get('actual')}")

    @staticmethod
    def _ineffective_reasons(policy: dict) -> dict[str, str] | None:
        """从 `policy["eligibility"]` 取「不予适用的规则 → 原因」。

        返回 ``None`` 表示入参**没有** `eligibility` 键 —— 旧 Plan、v1.0.0 的历史
        调用都是这个形状，一律按「全部规则均适用」处理，行为与引入本函数之前一致。

        键在、但形状不对时**抛异常，不静默当成空**（跨轨契约 R5）：静默会把
        「上游把结构改坏了」表现成「金额悄悄算错了」，而后者是本仓最难查的一类缺陷。
        """
        if "eligibility" not in policy:
            return None

        elig = policy.get("eligibility")
        if not isinstance(elig, dict):
            raise ValueError(
                f"policy['eligibility'] 必须是 dict，实际 {type(elig).__name__}")
        refs = elig.get("ineffective_rules")
        if not isinstance(refs, list):
            raise ValueError(
                "policy['eligibility'] 缺少 list 型的 ineffective_rules，"
                f"实际 {type(refs).__name__}")
        unmet = elig.get("unmet", [])
        if not isinstance(unmet, list):
            raise ValueError(
                f"policy['eligibility']['unmet'] 必须是 list，实际 {type(unmet).__name__}")

        reasons: dict[str, list[str]] = {}
        for item in unmet:
            if not isinstance(item, dict):
                raise ValueError(
                    f"eligibility.unmet 的元素必须是 dict，实际 {type(item).__name__}")
            requirement = item.get("requirement")
            if requirement not in FinanceSettleSkill.REQUIREMENT_KINDS:
                raise ValueError(
                    f"eligibility.unmet 出现取值域外的 requirement={requirement!r}；"
                    f"新增取值要先改跨轨契约再改代码，取值域：{sorted(FinanceSettleSkill.REQUIREMENT_KINDS)}")
            reasons.setdefault(str(item.get("rule_ref")), []).append(
                FinanceSettleSkill._reason_of(item))

        out: dict[str, str] = {}
        for ref in refs:
            ref = str(ref)
            if ref not in reasons:
                # 排除了却说不出为什么 —— 这样的 excluded_rules 在对账时等于没有。
                raise ValueError(
                    f"eligibility.ineffective_rules 列了 {ref}，但 unmet 里没有对应条目，"
                    f"无从说明排除原因")
            out[ref] = "；".join(reasons[ref])
        return out

    @staticmethod
    def _params_of(policy: dict) -> tuple[Decimal, Decimal, list[str], list[dict]]:
        """把**真正适用**的规则的参数合成一组核算参数。

        条件不满足的规则（`eligibility.ineffective_rules`）先剔出去再合成：一条
        「人为损坏免责」的排除规则在举证不足时本就不予适用，让它照样参与合成，
        它那个 `refund_ratio: "0"` 会把金额一路压到零 —— 举证责任就这么被倒置了。

        剩下的规则同时命中时：比例取**最不利于商家**的一条（最大 ratio），扣费取最大 ——
        这不是随手定的口径，而是「政策对客户的承诺是并集」的直接后果：
        任何一条当时生效的规则承诺了全额，商家就不能按另一条只退八成。
        """
        ratio, fee = DEFAULT_RATIO, DEFAULT_FEE
        applied: list[str] = []
        excluded: list[dict] = []
        rules = policy.get("matched_rules") or []
        reasons = FinanceSettleSkill._ineffective_reasons(policy)
        if not rules:
            return ratio, fee, applied, excluded
        ratios, fees = [], []
        for r in rules:
            params = r.get("params") if isinstance(r, dict) else None
            params = params if isinstance(params, dict) else {}
            ref = f"{r.get('rule_no')}@v{r.get('version')}"
            if reasons is not None and ref in reasons:
                excluded.append({"rule_ref": ref, "reason": reasons[ref]})
                continue
            ratios.append(_dec(params.get("refund_ratio"), DEFAULT_RATIO))
            fees.append(_dec(params.get("deduct_fee"), DEFAULT_FEE))
            applied.append(ref)
        if not ratios:
            # 命中的规则全被剔光：落回缺省口径（全额、不扣费），而不是返回 0。
            # 「一条都不适用」的意思是没有任何规则限制退款，不是没得退。
            return DEFAULT_RATIO, DEFAULT_FEE, applied, excluded
        return max(ratios), max(fees), applied, excluded
