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
        ratio, fee, applied = self._params_of(policy)
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
    def _params_of(policy: dict) -> tuple[Decimal, Decimal, list[str]]:
        """把命中规则的参数合成一组核算参数。

        多条规则同时命中时：比例取**最不利于商家**的一条（最大 ratio），扣费取最大 ——
        这不是随手定的口径，而是「政策对客户的承诺是并集」的直接后果：
        任何一条当时生效的规则承诺了全额，商家就不能按另一条只退八成。
        """
        ratio, fee = DEFAULT_RATIO, DEFAULT_FEE
        applied: list[str] = []
        rules = policy.get("matched_rules") or []
        if not rules:
            return ratio, fee, applied
        ratios, fees = [], []
        for r in rules:
            params = r.get("params") if isinstance(r, dict) else None
            params = params if isinstance(params, dict) else {}
            ratios.append(_dec(params.get("refund_ratio"), DEFAULT_RATIO))
            fees.append(_dec(params.get("deduct_fee"), DEFAULT_FEE))
            applied.append(f"{r.get('rule_no')}@v{r.get('version')}")
        return max(ratios), max(fees), applied
