"""claim.settle —— 按裁定命中的条款核算赔款，逐行写 claim_line.amount_allowed。

两件事必须**同时**做完：

  1. 把每一行 `claim_line` 的 `amount_allowed` 算出来落库；
  2. 产出的 output 带 `settlement` 键，其内容与库里那几行**是同一份数据**。

缺任何一件，第六道闸那种「凭据与产物对不上」的坑就会在本域重演一遍：一边按表判、
一边按产物判，两边各自都绿，合起来才发作。所以两件事写在同一个 return 里，
中间不留分支。

金额一律走 `Decimal`，不进浮点：`12000 * 0.85` 在二进制浮点下是 10199.999999999998，
四舍五入成分位后大多数时候看不出来，直到某个数字恰好落在半分上 —— 那是理赔对账
最难查的一类差异。`claim_line.amount_allowed` 落库时才转成 float（列是 REAL），
breakdown 里保留字符串原值，审计时看到的是算式本身。

## 三层扣减的顺序不可换

    起付线（deductible） -> 赔付比例（coinsurance） -> 保额上限（sum_insured）

先扣起付线再乘比例，还是先乘比例再扣起付线，算出来是两个数 —— 前者是行业惯例，
也是保单条款的写法。顺序写反不会报错，只会让每一笔赔款都少算或多算一截。
保额上限**最后**封顶：它约束的是最终赔付额，不是中间量。
"""

from __future__ import annotations

import json
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from maos.domain.claim import guard, objects
from maos.skills.contract import Skill, SkillContext, SkillContract
from maos.skills.registry import register_skill

from . import _common as C
from .adjudicate import DECISION_APPROVE

CENT = Decimal("0.01")

#: 条款没给参数时的缺省口径：全额赔、不扣起付线。
#: 写死成「全额」而不是「按某个默认比例打折」—— 少赔是要赔的，多赔是可追的，
#: 而一个凭空猜出来的比例两头都解释不了（口径同退款域 finance.settle）。
DEFAULT_RATIO = Decimal("1")
DEFAULT_DEDUCTIBLE = Decimal("0")


def _dec(value, default: Decimal) -> Decimal:
    """把条款参数收敛成 Decimal。转不动就用缺省，不抛 —— 条款 body 是人维护的。"""
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
class ClaimSettleSkill(Skill):
    contract = SkillContract(
        name="claim.settle",
        version="1.0.0",
        purpose="按裁定命中的条款逐行核算赔款，写 claim_line.amount_allowed 与"
                " adjudication.allowed_amount，并产出同一份数据的 settlement 产物",
        input_schema={
            "tenant_id": "str",
            "claim_id": "str",
            "adjudication": "dict（claim.adjudicate 的出参：decision / matched_rules / rule_refs）",
        },
        output_schema={
            "settlement": "dict（= 落库那几行的同一份数据）",
            "allowed_amount": "str（最终赔付额，字符串不进浮点）",
            "lines": "list[dict{line_no,item_code,amount_claimed,amount_allowed}]",
            "breakdown": "dict（三层扣减的算式，金额为字符串）",
            "rule_refs": "list[str]",
            "invocation_id": "str",
        },
        preconditions=["tenant_id", "claim_id", "adjudication"],
        depends_tools=[],
        failure_policy="escalate",
        max_retries=0,
        security_boundary=(
            "只读 claim_case / policy_contract / claim_line，写 claim_line.amount_allowed"
            " 与 adjudication.allowed_amount；**不改 biz_status**、**无权写 paid**；"
            "不调模型、不碰赔付方；"
            "赔款只按条款参数与保单快照计算，不接受调用方直接指定 allowed_amount"
        ),
        reuse_note="产出的 settlement 与库表是同一份数据，两处不许各造一份",
        owner_roles=["claim_settlement"],
    )

    def run(self, payload: dict, ctx: SkillContext) -> dict:
        store = C.ensure_schema(ctx)
        invocation_id = C.invocation_id_of(ctx)
        tenant_id, claim_id = C.required(payload, "tenant_id", "claim_id")

        adj = payload.get("adjudication")
        if not isinstance(adj, dict):
            raise ValueError(
                f"claim.settle 入参 adjudication 必须是 claim.adjudicate 的出参 dict，"
                f"实际 {type(adj).__name__}")
        if adj.get("decision") != DECISION_APPROVE:
            # 裁定不通过就不核算。给一个 0 元的核算结果会让下游误以为
            # 「核算过了，只是金额为零」，而事实是这笔根本不该进赔付环节。
            raise ValueError(
                f"裁定为 {adj.get('decision')!r}，不予核算："
                f"{adj.get('reason') or '无适用理赔条款'}")

        case = guard.get_case(store, tenant_id, claim_id)
        if case is None:
            raise LookupError(f"没有这个 case：tenant={tenant_id} claim={claim_id}")

        contracts = objects.query(
            store,
            "SELECT sum_insured, deductible, coinsurance_rate FROM policy_contract"
            " WHERE tenant_id=? AND policy_no=? AND version=?",
            (tenant_id, case["policy_no"], int(case["policy_version"])))
        if not contracts:
            raise LookupError(
                f"没有保单快照 tenant={tenant_id} policy={case['policy_no']} "
                f"v{case['policy_version']}，赔款无从核算")
        contract = contracts[0]

        lines = objects.query(
            store,
            "SELECT * FROM claim_line WHERE tenant_id=? AND claim_id=? ORDER BY line_no",
            (tenant_id, claim_id))
        if not lines:
            raise LookupError(
                f"claim={claim_id} 没有 claim_line，没有可核算的对象；先跑 claim.intake")

        ratio, deductible = self._params_of(adj, contract)
        sum_insured = _dec(contract["sum_insured"], Decimal("0"))

        # ---- 第一层：逐行取「申报 vs 保单」的较小者 ---------------------------
        # 申报金额不得超过保额：被保险人可以少报，不能多报。上限取保额而不是报错 ——
        # 多写一位数是常见笔误，按上限收敛并在 breakdown 里写清楚，比直接失败可用。
        gross = sum(_dec(row["amount_claimed"], Decimal("0")) for row in lines)
        claimed_total = _dec(case["amount_claimed"], Decimal("0"))
        base = min(gross, claimed_total) if claimed_total > 0 else gross

        # ---- 第二层：先扣起付线，再乘赔付比例（顺序不可换，见模块 docstring）---
        after_deductible = max(base - deductible, Decimal("0"))
        after_ratio = _q(after_deductible * ratio)

        # ---- 第三层：保额封顶 -------------------------------------------------
        allowed = _q(min(after_ratio, sum_insured)) if sum_insured > 0 else after_ratio
        capped_by_sum_insured = bool(sum_insured > 0 and after_ratio > sum_insured)

        # 逐行按占比分摊最终赔付额。**最后一行取余数**，不逐行独立四舍五入 ——
        # 独立舍入的分位误差会累积，让各行之和与总额差出几分钱，而理赔对账最先
        # 发现的就是这几分钱。
        out_lines, allocated = [], Decimal("0")
        for i, row in enumerate(lines):
            line_claimed = _dec(row["amount_claimed"], Decimal("0"))
            if i == len(lines) - 1:
                line_allowed = _q(allowed - allocated)
            else:
                share = (line_claimed / gross) if gross > 0 else Decimal("0")
                line_allowed = _q(allowed * share)
            allocated += line_allowed
            objects.execute(
                store,
                "UPDATE claim_line SET amount_allowed=? WHERE tenant_id=? AND claim_id=?"
                " AND line_no=?",
                (float(line_allowed), tenant_id, claim_id, int(row["line_no"])))
            out_lines.append({
                "line_no": int(row["line_no"]), "item_code": row["item_code"],
                "description": row["description"],
                "amount_claimed": str(_q(line_claimed)),
                "amount_allowed": str(line_allowed),
            })

        rule_refs = [str(r) for r in (adj.get("rule_refs") or [])]
        breakdown = {
            "lines_total": str(_q(gross)),
            "amount_claimed": str(_q(claimed_total)),
            "base": str(_q(base)),
            "deductible": str(_q(deductible)),
            "after_deductible": str(_q(after_deductible)),
            "coinsurance_rate": str(ratio),
            "after_ratio": str(after_ratio),
            "sum_insured": str(_q(sum_insured)),
            "capped_by_sum_insured": capped_by_sum_insured,
            "allowed_amount": str(allowed),
            # 「按哪一条、哪一版判的」两个字段原样带下来 —— 核算结果与裁定依据
            # 必须能被同一条链路查到，断开就没法回答「这笔钱凭什么这么算」。
            "primary_rule": adj.get("primary_rule"),
            "terms_version": adj.get("terms_version"),
            "rule_refs": rule_refs,
        }

        # 裁定行上补记最终赔付额：`adjudication` 的主键是
        # (tenant, claim, rule_no, terms_version)，与裁定那一步写的是同一行。
        objects.execute(
            store,
            "UPDATE adjudication SET allowed_amount=? WHERE tenant_id=? AND claim_id=?"
            " AND rule_no=? AND terms_version=?",
            (float(allowed), tenant_id, claim_id, adj.get("primary_rule"),
             int(adj.get("terms_version") or 0)))

        settlement = {
            "tenant_id": tenant_id,
            "claim_id": claim_id,
            "allowed_amount": float(allowed),
            "primary_rule": adj.get("primary_rule"),
            "terms_version": adj.get("terms_version"),
            "rule_refs": json.dumps(rule_refs, ensure_ascii=False),
            "breakdown_json": json.dumps(breakdown, ensure_ascii=False, sort_keys=True),
            "checked_by": getattr(getattr(ctx, "identity", None), "agent_id", "") or
                          self.contract.name,
            "checked_at": C.now_iso(),
        }

        return {
            "settlement": settlement,
            "allowed_amount": str(allowed),
            "lines": out_lines,
            "breakdown": breakdown,
            "rule_refs": rule_refs,
            "invocation_id": invocation_id,
        }

    # ------------------------------------------------------------------
    @staticmethod
    def _params_of(adj: dict, contract: dict) -> tuple[Decimal, Decimal]:
        """把命中条款的参数合成一组核算参数。

        多条条款同时命中时：赔付比例取**最有利于被保险人**的一条（最大 ratio），
        起付线取最小 —— 这不是随手定的口径，而是「条款对被保险人的承诺是并集」的
        直接后果：任何一条当时生效的条款承诺了全额，保险公司就不能按另一条只赔八成。
        与退款域 `finance.settle._params_of` 同一条道理（那边是「最不利于商家」，
        换个视角是同一句话）。

        条款没给参数时回落到**保单快照**上的 deductible / coinsurance_rate ——
        那是投保当时白纸黑字写着的数，比任何默认值都更有依据。

        🔴 **`coinsurance_rate == 0` 在两条路径上不是同一个意思，兜底只许罩住一条**：

          · **条款明写 0** —— 那就是「本项不赔」，是一个有效的业务结论。
            兜底成全额会让一条写着不赔的条款赔出 100%，而且一路无声：金额算得出来、
            每一步都成功、只有钱数错了。
          · **保单快照上是 0** —— `policy_contract.coinsurance_rate` 的列缺省就是 0，
            所以这一侧的 0 分不清「约定了 0%」和「这一列没填」。照 0 算会把**每一笔**
            赔款清零，那是更大的错，所以这一侧兜底成全额。

        两侧的 0 长得一模一样，含义相反 —— 这正是当初把兜底写在 return 上罩住两条
        路径的原因，也是它错的地方。要分辨只能靠「条款有没有显式给这个键」，
        而那个信息只在 `ratios` 这个列表空不空里。
        """
        rules = adj.get("matched_rules") or []
        ratios, deductibles = [], []
        for r in rules:
            params = r.get("params") if isinstance(r, dict) else None
            params = params if isinstance(params, dict) else {}
            if "coinsurance_rate" in params:
                ratios.append(_dec(params.get("coinsurance_rate"), DEFAULT_RATIO))
            if "deductible" in params:
                deductibles.append(_dec(params.get("deductible"), DEFAULT_DEDUCTIBLE))

        if ratios:
            ratio = max(ratios)          # 条款明写的，0 就是 0，不许被兜底改写
        else:
            ratio = _dec(contract.get("coinsurance_rate"), DEFAULT_RATIO)
            if ratio <= 0:               # 快照那一侧的 0 是「没填」，见 docstring
                ratio = DEFAULT_RATIO
        # 起付线没有这个歧义：条款明写 0 与快照上是 0 都是「不扣起付线」，
        # 而那对被保险人有利，两侧同义，所以这里不需要分路。
        deductible = (min(deductibles) if deductibles
                      else _dec(contract.get("deductible"), DEFAULT_DEDUCTIBLE))
        return ratio, deductible
