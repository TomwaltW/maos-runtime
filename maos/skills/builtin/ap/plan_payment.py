"""ap.plan-payment —— 出付款计划：付多少、怎么付、什么时候付。

这一步是**人要批的那一份东西**。付款计划任务挂 `effect_risk=H`，Gate 过了也停在
`BLOCKED` 等人放行 —— 主管在 Matrix 房间里看到的卡片，内容就是本 skill 的产物。

## 为什么它是独立一步，而不是并进 ap.execute

因为「批准付款」批的是**计划**，不是**动作**。如果计划与执行合成一步，人能看到的
只有「要不要按下发送键」，看不到金额是怎么算出来的、按哪条税种、什么付款方式、
到期日是哪天。审批就退化成一个确认框。

分开之后，审批的对象是一份可核对的清单：金额挂 `BR-CO-16`，税额分解挂
`UNCL5305` 的税种码，付款方式挂 `UNCL4461` 的码 —— 主管批的是这些，不是一个按钮。

## 不信发票自称的金额

`payable_amount` 取 `ap.match` **算出来**的那个，不取发票自称的 `amount_due`。
两者在匹配全过时必然相等（`BR-CO-16` 就在判它），所以数额不会变；变的是**依据**。
匹配没过就一律不出计划 —— 那种情况下发票金额是未经验证的输入。
"""

from __future__ import annotations

import json

from maos.domain.ap import guard, objects
from maos.skills.contract import Skill, SkillContext, SkillContract
from maos.skills.registry import register_skill
from maos.tools import ap_codes

from . import _common as C


@register_skill
class ApPlanPaymentSkill(Skill):
    contract = SkillContract(
        name="ap.plan-payment",
        version="1.0.0",
        purpose="按三单匹配的结论出一份可核对的付款计划（金额/付款方式/到期日/税种分解），"
                "供 effect_risk=H 的人工审批",
        input_schema={
            "tenant_id": "str",
            "case_id": "str",
            "attempt": "int（可选，取哪一轮的匹配结论，缺省取最近一轮）",
        },
        output_schema={
            "plan": "dict（付款计划：amount / currency / payment_means_code / due_at）",
            "payable_amount": "str（取 ap.match 算出来的那个，不取发票自称的）",
            "citations": "list[dict]（金额与码表各自的规范引用）",
            "needs_human_approval": "bool（恒 True —— 出账是不可逆动作）",
            "biz_status": "str",
            "invocation_id": "str",
        },
        preconditions=["tenant_id", "case_id"],
        depends_tools=[],
        failure_policy="escalate",
        max_retries=0,
        security_boundary=(
            "只读不写业务状态：本 skill 不推进 biz_status，也不碰银行。"
            "匹配没通过一律拒绝出计划 —— 未经验证的金额不许进入审批视野。"
            f"付款方式码不在 {ap_codes.LIST_PAYMENT_MEANS} 内当场抛"
            f"（{ap_codes.RULE_PAYMENT_MEANS_CODED}）"
        ),
        reuse_note="任何「人要批一份计划而不是一个按钮」的域都该照此分步："
                   "把审批对象做成可核对的清单，而不是一次确认",
        owner_roles=["ap_control"],
    )

    def run(self, payload: dict, ctx: SkillContext) -> dict:
        store = C.ensure_schema(ctx)
        invocation_id = C.invocation_id_of(ctx)
        tenant_id, case_id = C.required(payload, "tenant_id", "case_id")

        case = guard.get_case(store, tenant_id, case_id)
        if case is None:
            raise LookupError(f"没有这个 case：tenant={tenant_id} case={case_id}")

        result = self._latest_match(store, tenant_id, case_id, payload.get("attempt"))
        if not result["matched"]:
            findings = json.loads(result["findings_json"] or "[]")
            rules = sorted({f.get("rule_id", "?") for f in findings})
            raise ValueError(
                f"case={case_id} 的三单匹配没有通过（拒付理由 {len(findings)} 条，"
                f"涉及 {rules}），不许出付款计划 —— 未经验证的金额不该进入审批视野"
            )

        supplier = objects.get_supplier(store, tenant_id, case["supplier_id"])
        if supplier is None:
            raise LookupError(
                f"供应商 {case['supplier_id']} 不在库里 —— 付给谁、怎么付都无从确定")

        # 付款方式码当场核（BR-CL-16）。表外的码在这里抛，不留到发指令那一步 ——
        # 那时候钱已经在路上了。
        means_code = str(supplier["payment_means_code"])
        means = ap_codes.require_code(ap_codes.LIST_PAYMENT_MEANS, means_code)

        invoice = objects.get_invoice(store, tenant_id, case["invoice_id"])
        amount = objects.money_str(result["payable_amount"])

        plan = {
            "tenant_id": tenant_id,
            "case_id": case_id,
            "supplier_id": case["supplier_id"],
            "supplier_name": supplier["name"],
            "invoice_id": case["invoice_id"],
            "po_id": case["po_id"],
            "amount": amount,
            "currency": case["currency"],
            "payment_means_code": means_code,
            "payment_means_name": means.name,
            "payment_terms": supplier["payment_terms"],
            "due_at": (invoice or {}).get("due_at", ""),
            "bank_account": supplier["bank_account"],
            "idempotency_key": C.idempotency_key(tenant_id, case_id),
            "matched_attempt": result["attempt"],
        }

        return {
            "plan": plan,
            "payable_amount": amount,
            # 引用挂在计划上，房间里的人不必回到源码就能核对每个数字的依据。
            "citations": [
                ap_codes.cite(ap_codes.RULE_AMOUNT_DUE),
                ap_codes.cite(ap_codes.RULE_PAYMENT_MEANS_CODED),
                ap_codes.cite(ap_codes.RULE_TAX_CATEGORY_CODED),
            ],
            # 恒 True：把钱付出去是不可逆的落地动作。这个值不由金额大小决定 ——
            # 「小额免批」是配置策略，不是本 skill 的判断，写进来就成了硬编码的口子。
            "needs_human_approval": True,
            "biz_status": case["biz_status"],
            "invocation_id": invocation_id,
        }

    # ------------------------------------------------------------------
    @staticmethod
    def _latest_match(store, tenant_id: str, case_id: str, attempt: object) -> dict:
        """取匹配结论。指定了 attempt 就取那一轮，否则取**最近**一轮。

        取最近一轮而不是「任意一轮通过就算过」：返工重匹配之后，结论是最后那一次
        的。拿一轮旧的通过结论去出计划，等于用一份已经被推翻的判定付钱。
        """
        if attempt is not None:
            rows = objects.query(
                store, "SELECT * FROM match_result WHERE tenant_id=? AND case_id=?"
                       " AND attempt=?", (tenant_id, case_id, int(attempt)))
        else:
            rows = objects.query(
                store, "SELECT * FROM match_result WHERE tenant_id=? AND case_id=?"
                       " ORDER BY attempt DESC LIMIT 1", (tenant_id, case_id))
        if not rows:
            raise LookupError(
                f"case={case_id} 还没有任何三单匹配结论 —— 先跑 ap.match")
        return rows[0]
