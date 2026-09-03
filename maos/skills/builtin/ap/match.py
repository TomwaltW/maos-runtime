"""ap.match —— 三单匹配：发票 × 采购订单 × 收货单，逐行比数量、单价，再逐项验勾稽。

## 本 skill 存在的理由

「这张发票该不该付」是应付账款里唯一真正的判断题。判错的两个方向都很贵：
该拒的付了是资金损失，该付的拒了是供应商关系与滞纳金。所以判据必须**可核对**——
每一条拒付理由都挂一个**真实存在的规则编号**（`BR-xx` / `PEPPOL-EN16931-xx`），
对方拿着编号去 Peppol BIS Billing 3.0 的规则页能查到原文。

编号一律从 `maos/tools/ap_codes.py` 的常量取，不在本文件写字面量 ——
写字面量就没有任何机制保证它是**存在的**规则，打错一个字照样跑，而拒付理由挂着
一个查不到的编号。`maos/tests/test_ap_codes.py` 有一条用例扫本域源码钉住这件事。

## 三类判据，各自的容差不是一回事

| 类 | 比什么 | 容差 | 为什么要容差 |
| :-- | :-- | :-- | :-- |
| 数量 | 发票行数量 vs 收货合格数 | `TOLERANCE_QUANTITY` | 散装/称重商品的计量误差 |
| 单价 | 发票行单价 vs 订单单价 | `TOLERANCE_UNIT_PRICE` | 汇率折算、分币进位 |
| 税额 | 算出来的税额 vs 发票报的税额 | `TOLERANCE_TAX` | 各行分别进位与总额进位的差 |

**三个容差不许合并成一个**。合并的后果不是少写两行代码，是判据失去意义：
数量容差要按件给（0.5 件），单价容差要按钱给（0.01 元），量纲都不同。
给一个统一的「1%」看起来优雅，实际是让 6800 元的单价漂 68 元也算过。

容差**只给数量、单价、税额**这三项，勾稽等式（BR-CO-10/13/15/16）一律零容差：
那几条是加减法，对不上就是发票自己算错了，不是计量误差。

## 匹配不上时**不抛异常**

匹配不过是一个**结论**，不是一次失败。抛异常会让它变成「任务执行失败」，于是
返工、再返工 —— 而数据一个字都没变，重试一万次结论一样（`intake.py` 抬头写了
这两类失败的分界）。所以本 skill 一律返回 `matched=False` + findings，
由上层（Agent → Gate → 人）决定怎么处置。

## 金额一律 Decimal

勾稽是等式比对，`0.1 + 0.2 != 0.3` 会直接变成一条**假的拒付理由**：一张完全正确
的发票被拒付，而理由挂着一个真实的规则编号，看起来毫无破绽。
换算口径集中在 `objects.money()`，本文件不自己转。
"""

from __future__ import annotations

import json
from decimal import Decimal

from maos.domain.ap import guard, objects
from maos.skills.contract import Skill, SkillContext, SkillContract
from maos.skills.registry import register_skill
from maos.tools import ap_codes

from . import _common as C

# ---------------------------------------------------------------- 容差缺省值
#: 数量容差，按**件**给。0.5 件：散装/称重商品的计量误差。
TOLERANCE_QUANTITY = Decimal("0.5")

#: 单价容差，按**钱**给。0.01 元：汇率折算与分币进位。
TOLERANCE_UNIT_PRICE = Decimal("0.01")

#: 税额容差，按**钱**给。0.02 元：各行分别按 BR-CO-17 进位之后，与发票按总额
#: 一次进位的差，最多差各行进位的累计 —— 两行的场景给两分钱。
TOLERANCE_TAX = Decimal("0.02")

#: 容差在 payload 里的键名。场景与测试用它调容差演「差异在容差内 / 外」两种收口。
TOLERANCE_KEYS = ("quantity", "unit_price", "tax")

_DEFAULT_TOLERANCE = {
    "quantity": TOLERANCE_QUANTITY,
    "unit_price": TOLERANCE_UNIT_PRICE,
    "tax": TOLERANCE_TAX,
}

#: 勾稽等式的容差。**恒零**，见模块 docstring。写成常量而不是字面 0，
#: 是为了让「这里为什么没有容差」在代码里有个可以挂注释的地方。
TOLERANCE_EXACT = Decimal("0")

SEVERITY_BLOCK = "block"
"""拒付。这一条不解决就不许付款。"""

SEVERITY_INFO = "info"
"""只报不拦。当前没有这一档的判据，保留是为了将来加「提示类」差异时有位置放。"""


def _finding(rule_id: str, *, message: str, severity: str = SEVERITY_BLOCK,
             **fields) -> dict:
    """折一条拒付理由。

    `ap_codes.cite()` 会去 `RULES` 里取，编号不存在**当场抛** —— 自造编号在这里
    死掉，而不是流到产物上让人以为它可核对。
    """
    return {"severity": severity, "message": message, **ap_codes.cite(rule_id), **fields}


@register_skill
class ApMatchSkill(Skill):
    contract = SkillContract(
        name="ap.match",
        version="1.0.0",
        purpose="三单匹配：逐行比数量与单价（各自容差），再按 Peppol/EN16931 规则验勾稽，"
                "产出可核对的拒付理由与应付金额",
        input_schema={
            "tenant_id": "str",
            "case_id": "str",
            "attempt": "int（可选，落 match_result 的主键之一，缺省 1）",
            "tolerance": "dict（可选，覆盖 quantity/unit_price/tax 三个容差）",
        },
        output_schema={
            "matched": "bool（匹配通过与否 —— 不通过**不是**执行失败）",
            "payable_amount": f"str（通过时按 {ap_codes.RULE_AMOUNT_DUE} 算出的应付额；"
                              f"不通过为空串）",
            "findings": "list[dict]（每条带 rule_id / text / source，可核对）",
            "checked": "list[str]（本次跑过的判据编号 —— 证明没判的和判过的分得开）",
            "tolerance": "dict（本次实际用的三个容差）",
            "biz_status": "str（通过则推进到 matched，否则原样不动）",
            "invocation_id": "str",
        },
        preconditions=["tenant_id", "case_id"],
        depends_tools=[],
        failure_policy="escalate",
        max_retries=0,
        security_boundary=(
            "只读三单、只写 match_result 与（通过时）ap_case.biz_status；"
            "biz_status 一律经 guard.update_biz_status，写不出 settled。"
            "拒付理由的 rule_id 必须来自 maos/tools/ap_codes.py 的已核对清单，"
            "自造编号在 ap_codes.require_rule 里当场抛"
        ),
        reuse_note="任何「拿外部单据互相勾稽」的域都该照此写：判据挂外部规范编号，"
                   "容差按量纲分别给，等式类判据零容差",
        owner_roles=["ap_match"],
    )

    def run(self, payload: dict, ctx: SkillContext) -> dict:
        store = C.ensure_schema(ctx)
        invocation_id = C.invocation_id_of(ctx)
        tenant_id, case_id = C.required(payload, "tenant_id", "case_id")
        attempt = int(payload.get("attempt") or 1)
        tolerance = self._tolerance(payload.get("tolerance"))

        case = guard.get_case(store, tenant_id, case_id)
        if case is None:
            raise LookupError(f"没有这个 case：tenant={tenant_id} case={case_id}")

        invoice = objects.get_invoice(store, tenant_id, case["invoice_id"])
        if invoice is None:
            raise LookupError(f"发票 {case['invoice_id']} 不在库里")
        inv_lines = objects.invoice_lines(store, tenant_id, case["invoice_id"])
        po_lines = objects.po_lines(store, tenant_id, case["po_id"], case["po_version"])
        gr_lines = objects.gr_lines(store, tenant_id, case["gr_id"])

        findings: list[dict] = []
        checked: list[str] = []

        findings += self._check_header(invoice, case, checked)
        findings += self._check_lines(inv_lines, po_lines, gr_lines, tolerance, checked)
        totals = self._check_totals(invoice, inv_lines, tolerance, checked)
        findings += totals["findings"]

        blocking = [f for f in findings if f["severity"] == SEVERITY_BLOCK]
        matched = not blocking
        payable = totals["amount_due"] if matched else ""

        # ---- 落匹配结论。**通过与否都落** —— 「第一次为什么没过」要在库里查得到 ----
        objects.execute(
            store,
            "INSERT OR REPLACE INTO match_result (tenant_id, case_id, attempt, matched,"
            " payable_amount, findings_json, tolerance_json, matched_by, matched_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (tenant_id, case_id, attempt, 1 if matched else 0, payable,
             json.dumps(findings, ensure_ascii=False, sort_keys=True),
             json.dumps({k: str(v) for k, v in tolerance.items()},
                        ensure_ascii=False, sort_keys=True),
             self.contract.name, C.now_iso()),
        )

        biz_status = case["biz_status"]
        if matched and biz_status == guard.INITIAL_STATUS:
            biz_status = guard.update_biz_status(
                store, tenant_id, case_id, "matched",
                self.contract.name, invocation_id,
                reason=f"三单匹配通过，应付 {payable} {case['currency']}"
                       f"（判据 {len(checked)} 条，容差内）")["biz_status"]

        return {
            "matched": matched,
            "payable_amount": payable,
            "findings": findings,
            "checked": checked,
            "tolerance": {k: str(v) for k, v in tolerance.items()},
            "biz_status": biz_status,
            "line_count": len(inv_lines),
            "invocation_id": invocation_id,
        }

    # ------------------------------------------------------------------ 容差
    @staticmethod
    def _tolerance(raw: object) -> dict[str, Decimal]:
        """取本次的三个容差。缺省用模块常量，键写错**抛**而不是静默忽略。

        静默忽略的后果是「我调了容差」和「容差没生效」看起来一模一样 ——
        场景演「差异在容差内」时会以为演成了，实际走的是缺省值。
        """
        out = dict(_DEFAULT_TOLERANCE)
        if raw is None:
            return out
        if not isinstance(raw, dict):
            raise ValueError(f"tolerance 必须是 dict，实际 {type(raw).__name__}")
        unknown = sorted(set(raw) - set(TOLERANCE_KEYS))
        if unknown:
            raise ValueError(
                f"未知的容差键 {unknown}；只认 {list(TOLERANCE_KEYS)} —— "
                f"三个容差量纲不同，不许合并成一个（见模块 docstring）")
        for key, value in raw.items():
            out[key] = objects.money(value)
        return out

    # ------------------------------------------------------------ 抬头级判据
    @staticmethod
    def _check_header(invoice: dict, case: dict, checked: list[str]) -> list[dict]:
        """抬头级：发票类型码、采购订单引用。两条都是「码/引用合不合规」，不是算术。"""
        out: list[dict] = []

        checked.append(ap_codes.RULE_INVOICE_TYPE_CODED)
        type_code = str(invoice["invoice_type_code"])
        if not ap_codes.is_valid_code(ap_codes.LIST_INVOICE_TYPE, type_code):
            out.append(_finding(
                ap_codes.RULE_INVOICE_TYPE_CODED,
                message=f"发票类型码 {type_code!r} 不在 UNCL1001 发票子集内",
                observed=type_code))

        checked.append(ap_codes.RULE_ORDER_REFERENCE)
        if not str(invoice["po_id"] or "").strip():
            out.append(_finding(
                ap_codes.RULE_ORDER_REFERENCE,
                message="发票没有采购订单引用 —— 三单匹配无从下手",
                observed=""))
        elif str(invoice["po_id"]) != str(case["po_id"]):
            out.append(_finding(
                ap_codes.RULE_ORDER_REFERENCE,
                message=f"发票引用的订单 {invoice['po_id']!r} 与本案的订单 "
                        f"{case['po_id']!r} 不是同一张",
                observed=str(invoice["po_id"]), expected=str(case["po_id"])))
        return out

    # -------------------------------------------------------------- 行级判据
    def _check_lines(self, inv_lines: list[dict], po_lines: list[dict],
                     gr_lines: list[dict], tolerance: dict[str, Decimal],
                     checked: list[str]) -> list[dict]:
        """逐行比数量与单价，并验行金额勾稽。

        **按 (line_no, sku) 配对，不按行序**。按行序配对在「发票少一行」时会把后面
        每一行都错位比一遍，产出一串互相矛盾的差异，真正的原因（少了一行）反而
        淹没在噪声里。
        """
        out: list[dict] = []
        po_by_key = {(int(r["line_no"]), str(r["sku"])): r for r in po_lines}
        gr_by_key = {(int(r["line_no"]), str(r["sku"])): r for r in gr_lines}

        checked.extend([ap_codes.RULE_INVOICED_QUANTITY, ap_codes.RULE_ITEM_NET_PRICE,
                        ap_codes.RULE_LINE_NET_AMOUNT, ap_codes.RULE_TAX_CATEGORY_CODED])

        for line in inv_lines:
            key = (int(line["line_no"]), str(line["sku"]))
            where = f"第 {key[0]} 行（{key[1]}）"

            # ---- 数量：发票 vs 收货**合格数** ----
            gr = gr_by_key.get(key)
            if gr is None:
                out.append(_finding(
                    ap_codes.RULE_INVOICED_QUANTITY,
                    message=f"{where}：发票开了这一行，收货单上没有对应行 —— 货没收到",
                    line_no=key[0], sku=key[1]))
            else:
                accepted = objects.money(objects.accepted_quantity(gr))
                invoiced = objects.money(line["quantity"])
                delta = abs(invoiced - accepted)
                if delta > tolerance["quantity"]:
                    out.append(_finding(
                        ap_codes.RULE_INVOICED_QUANTITY,
                        message=f"{where}：发票数量 {invoiced} 与收货合格数 {accepted} "
                                f"相差 {delta}，超出数量容差 {tolerance['quantity']}",
                        line_no=key[0], sku=key[1], invoiced=str(invoiced),
                        accepted=str(accepted), delta=str(delta),
                        tolerance=str(tolerance["quantity"])))

            # ---- 单价：发票 vs 订单 ----
            po = po_by_key.get(key)
            if po is None:
                out.append(_finding(
                    ap_codes.RULE_ITEM_NET_PRICE,
                    message=f"{where}：发票开了这一行，采购订单上没有对应行 —— 没有订过",
                    line_no=key[0], sku=key[1]))
            else:
                inv_price = objects.money(line["unit_price"])
                po_price = objects.money(po["unit_price"])
                delta = abs(inv_price - po_price)
                if delta > tolerance["unit_price"]:
                    out.append(_finding(
                        ap_codes.RULE_ITEM_NET_PRICE,
                        message=f"{where}：发票单价 {inv_price} 与订单单价 {po_price} "
                                f"相差 {delta}，超出单价容差 {tolerance['unit_price']}",
                        line_no=key[0], sku=key[1], invoiced=str(inv_price),
                        ordered=str(po_price), delta=str(delta),
                        tolerance=str(tolerance["unit_price"])))

            # ---- 行金额勾稽：净额 = 数量 × 单价（PEPPOL-EN16931-R120）----
            # 零容差：这是乘法，对不上就是发票自己算错了。本域没有行级折扣与
            # 附加费，所以 R120 的算式退化成两项相乘；有的话要在这里补进去。
            expect = (objects.money(line["quantity"])
                      * objects.money(line["unit_price"])).quantize(Decimal("0.01"))
            actual = objects.money(line["line_net"])
            if abs(actual - expect) > TOLERANCE_EXACT:
                out.append(_finding(
                    ap_codes.RULE_LINE_NET_AMOUNT,
                    message=f"{where}：行净额 {actual} ≠ 数量 × 单价 = {expect}",
                    line_no=key[0], sku=key[1], observed=str(actual),
                    expected=str(expect)))

            # ---- 税种码必须在 UNCL5305 里（BR-CL-17）----
            tax_code = str(line["tax_category_code"])
            if not ap_codes.is_valid_code(ap_codes.LIST_TAX_CATEGORY, tax_code):
                out.append(_finding(
                    ap_codes.RULE_TAX_CATEGORY_CODED,
                    message=f"{where}：税种码 {tax_code!r} 不在 UNCL5305 内",
                    line_no=key[0], sku=key[1], observed=tax_code))

        # 反向：订单/收货上有、发票上没有的行**不报** —— 那是「还没开票」，
        # 不是「这张发票有问题」。分期开票是正常业务，报出来就成了噪声。
        return out

    # -------------------------------------------------------------- 合计勾稽
    def _check_totals(self, invoice: dict, inv_lines: list[dict],
                      tolerance: dict[str, Decimal], checked: list[str]) -> dict:
        """按 BR-CO-10 / 17 / 13 / 15 / 16 逐条验合计，并算出应付额。

        **算出来的应付额不取发票自称的那个** —— 发票报的 `amount_due` 是待验证的
        输入，不是结论。全部判据通过时两者必然相等（BR-CO-16 就在判它），
        所以取算出来的那个不会改变数额，但会改变**依据**：付出去的钱是我们自己
        按规则算出来的，不是抄发票上的数字。
        """
        out: list[dict] = []
        checked.extend([ap_codes.RULE_SUM_LINE_NET, ap_codes.RULE_VAT_AMOUNT,
                        ap_codes.RULE_TOTAL_WITHOUT_VAT, ap_codes.RULE_TOTAL_WITH_VAT,
                        ap_codes.RULE_AMOUNT_DUE])

        cents = Decimal("0.01")
        line_net_sum = sum((objects.money(r["line_net"]) for r in inv_lines),
                           Decimal("0")).quantize(cents)

        # BR-CO-10：行净额合计 = Σ 各行净额。零容差。
        claimed_line_total = objects.money(invoice["line_net_total"])
        if abs(claimed_line_total - line_net_sum) > TOLERANCE_EXACT:
            out.append(_finding(
                ap_codes.RULE_SUM_LINE_NET,
                message=f"发票的行净额合计 {claimed_line_total} ≠ Σ 各行净额 {line_net_sum}",
                observed=str(claimed_line_total), expected=str(line_net_sum)))

        # BR-CO-17：按税种分组算税额，各组分别按两位小数进位。
        # **分组算而不是总额乘一个税率**：一张发票可以有多个税种（S 标准税率 +
        # Z 零税率），拿加权平均税率去乘会把两组的进位误差揉在一起，
        # 而那个误差恰好落在税额容差的量级上，判据就此失去分辨力。
        by_category: dict[tuple[str, str], Decimal] = {}
        for row in inv_lines:
            key = (str(row["tax_category_code"]), str(objects.money(row["tax_rate"])))
            by_category[key] = by_category.get(key, Decimal("0")) + objects.money(row["line_net"])
        vat_sum = Decimal("0")
        breakdown = []
        for (code, rate), taxable in sorted(by_category.items()):
            amount = (taxable * objects.money(rate) / Decimal("100")).quantize(cents)
            vat_sum += amount
            breakdown.append({"tax_category_code": code, "tax_rate": rate,
                              "taxable_amount": str(taxable.quantize(cents)),
                              "tax_amount": str(amount)})
        vat_sum = vat_sum.quantize(cents)

        claimed_vat = objects.money(invoice["total_vat"])
        if abs(claimed_vat - vat_sum) > tolerance["tax"]:
            out.append(_finding(
                ap_codes.RULE_VAT_AMOUNT,
                message=f"发票税额 {claimed_vat} 与按 "
                        f"{ap_codes.RULE_VAT_AMOUNT} 分税种算出的 {vat_sum} "
                        f"相差 {abs(claimed_vat - vat_sum)}，超出税额容差 "
                        f"{tolerance['tax']}",
                observed=str(claimed_vat), expected=str(vat_sum),
                breakdown=breakdown, tolerance=str(tolerance["tax"])))

        # BR-CO-13：不含税总额 = Σ 行净额 − 单据级折扣 + 单据级附加费。
        # 本域没有单据级折扣/附加费，退化成「= Σ 行净额」。零容差。
        claimed_excl = objects.money(invoice["total_excl_vat"])
        if abs(claimed_excl - line_net_sum) > TOLERANCE_EXACT:
            out.append(_finding(
                ap_codes.RULE_TOTAL_WITHOUT_VAT,
                message=f"不含税总额 {claimed_excl} ≠ Σ 行净额 {line_net_sum}"
                        f"（本域无单据级折扣与附加费）",
                observed=str(claimed_excl), expected=str(line_net_sum)))

        # BR-CO-15：含税总额 = 不含税总额 + 税额。零容差。
        # 这里用**发票自称的**不含税与税额去验它自称的含税额 —— 判的是发票内部
        # 自洽不自洽。用我们算出来的去验会把上面两条已经报过的差异再报一遍。
        expect_incl = (claimed_excl + claimed_vat).quantize(cents)
        claimed_incl = objects.money(invoice["total_incl_vat"])
        if abs(claimed_incl - expect_incl) > TOLERANCE_EXACT:
            out.append(_finding(
                ap_codes.RULE_TOTAL_WITH_VAT,
                message=f"含税总额 {claimed_incl} ≠ 不含税 {claimed_excl} + 税额 "
                        f"{claimed_vat} = {expect_incl}",
                observed=str(claimed_incl), expected=str(expect_incl)))

        # BR-CO-16：应付 = 含税总额 − 已付 + 舍入。本域没有舍入项。零容差。
        prepaid = objects.money(invoice["prepaid_amount"])
        expect_due = (claimed_incl - prepaid).quantize(cents)
        claimed_due = objects.money(invoice["amount_due"])
        if abs(claimed_due - expect_due) > TOLERANCE_EXACT:
            out.append(_finding(
                ap_codes.RULE_AMOUNT_DUE,
                message=f"应付金额 {claimed_due} ≠ 含税总额 {claimed_incl} − 已付 "
                        f"{prepaid} = {expect_due}",
                observed=str(claimed_due), expected=str(expect_due)))

        return {"findings": out, "amount_due": str(expect_due),
                "vat_breakdown": breakdown, "line_net_sum": str(line_net_sum)}
