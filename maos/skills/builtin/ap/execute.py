"""ap.execute —— 向银行发出付款指令。**写得出 payment_requested，写不出 settled。**

这个 skill 与 `ap.observe` 的分工是本域「观察与推断分离」的落点：

  · 本 skill 只产生一条**指令**，业务状态推到 `payment_requested`
    —— 「我们已经让银行去付了」；
  · `ap.observe` 轮询银行回单，是全系统唯一写得进 `settled` 的地方
    —— 「银行说钱划走了」。

合成一个 skill 就等于承认「我发出去了所以它付掉了」—— 那正是铁律 8 禁止的推断。
分成两个之后，「凭什么说这笔货款付出去了」在代码结构上就有答案：因为
`ap.observe` 问到了带银行流水号的终态回单，且回单与状态更新在同一个事务里落了库。

## 三道前置，缺一不可

1. **匹配通过**：`biz_status` 必须已经是 `matched`。不查这一条，一张三单对不上的
   发票也能付出去。
2. **有人批过**：`payment_approval` 里必须有一条 `approved`。本 skill **只读不写**
   审批记录 —— 让付款方自己写下「我被批准了」，等于没有审批。
3. **幂等键**：由 `(tenant, case)` 唯一确定，返工重跑落在同一个键上。
   `MockBank` 与 `payment_instruction` 表的唯一索引是同一件事的两道防线。

## 银行回执永远不是终态

`bank.pay` 的返回值永远是 `accepted`（`maos/tools/ap.py` 对这条有断言）。
本 skill 拿到它之后**不做任何成败推断**，只把它原样落进产物。
"""

from __future__ import annotations

import json

from maos.domain.ap import guard, objects
from maos.skills.contract import Skill, SkillContext, SkillContract
from maos.skills.registry import register_skill
from maos.tools.ap import BANK_PAY_PORT, TERMINAL_STATUSES, PaymentInstruction
from maos.tools.port import invoke_tool

from . import _common as C


@register_skill
class ApExecuteSkill(Skill):
    contract = SkillContract(
        name="ap.execute",
        version="1.0.0",
        purpose="核对匹配与审批后向银行发出付款指令，落 payment_instruction 并把业务状态"
                "推到 payment_requested；**写不出 settled**",
        input_schema={
            "tenant_id": "str",
            "case_id": "str",
            "bank": "str（已 register_bank 的名字，缺省 'demo'）",
            "amount": "str（可选，缺省取最近一轮匹配算出的应付额）",
        },
        output_schema={
            "instruction_id": "str（银行侧指令 id，ap.observe 用它）",
            "idempotency_key": "str",
            "amount": "str",
            "bank_advice": "dict（受理回单，**永远不是终态**）",
            "biz_status": "payment_requested",
            "invocation_id": "str",
        },
        preconditions=["tenant_id", "case_id"],
        depends_tools=["bank.pay"],
        failure_policy="escalate",
        max_retries=0,
        security_boundary=(
            "三道前置：匹配已通过（biz_status=matched）、有一条 approved 的人工审批、"
            "幂等键由 (tenant, case) 唯一确定。审批记录只读不写。"
            "本 skill 不是 guard.AUTHORITATIVE_WRITER —— 试图写 settled 会被 guard "
            "抛 AuthoritativeFactViolation 并落一条事件。银行调用一律经 invoke_tool 留审计行"
        ),
        reuse_note="任何「发出去 ≠ 成功了」的域都该照此拆两步：执行一步、观察一步，"
                   "终态只有观察那一步写得进",
        owner_roles=["ap_treasury"],
    )

    def run(self, payload: dict, ctx: SkillContext) -> dict:
        store = C.ensure_schema(ctx)
        invocation_id = C.invocation_id_of(ctx)
        tenant_id, case_id = C.required(payload, "tenant_id", "case_id")

        case = guard.get_case(store, tenant_id, case_id)
        if case is None:
            raise LookupError(f"没有这个 case：tenant={tenant_id} case={case_id}")

        # ---- 前置 1：匹配通过 ----------------------------------------------
        if case["biz_status"] != "matched":
            raise ValueError(
                f"case={case_id} 当前 biz_status={case['biz_status']}，只有 matched "
                f"才允许发起付款 —— 三单没对上就付钱是本域要拦的头一件事")

        # ---- 前置 2：有人批过（只读，不写）---------------------------------
        approvals = C.approvals_of(store, tenant_id=tenant_id, case_id=case_id)
        if not approvals:
            raise ValueError(
                f"case={case_id} 没有任何 approved 的付款审批；付款是不可逆动作，"
                f"必须有人批过。**本 skill 不写审批记录** —— 让付款方自己写下"
                f"「我被批准了」等于没有审批")

        # ---- 金额：默认取匹配算出来的那个，不取发票自称的 -------------------
        amount = payload.get("amount")
        if amount is None:
            rows = objects.query(
                store, "SELECT payable_amount FROM match_result WHERE tenant_id=? AND"
                       " case_id=? AND matched=1 ORDER BY attempt DESC LIMIT 1",
                (tenant_id, case_id))
            if not rows:
                raise LookupError(f"case={case_id} 没有通过的匹配结论，取不到应付金额")
            amount = rows[0]["payable_amount"]
        amount = objects.money_str(amount)

        supplier = objects.get_supplier(store, tenant_id, case["supplier_id"])
        if supplier is None:
            raise LookupError(f"供应商 {case['supplier_id']} 不在库里")

        # ---- 前置 3：幂等键 -------------------------------------------------
        key = C.idempotency_key(tenant_id, case_id)
        bank_name = str(payload.get("bank") or C.DEFAULT_BANK)
        bank = C.get_bank(bank_name)

        instruction = PaymentInstruction(
            supplier_id=str(case["supplier_id"]),
            invoice_id=str(case["invoice_id"]),
            amount=amount,
            currency=str(case["currency"]),
            payment_means_code=str(supplier["payment_means_code"]),
            idempotency_key=key,
            remittance_info=f"{case['invoice_id']} / {case['po_id']}",
        )
        advice = invoke_tool(BANK_PAY_PORT, {"bank": bank, "instruction": instruction},
                             store=store, extras=C.tool_extras(ctx))

        # 银行受理回单**不许**是终态。这不是防御性编程 —— 换成真银行适配器时，
        # 一个把「受理」当「已付」返回的实现会让 ap.observe 失去存在理由，
        # 而症状是「一切正常，钱好像也付了」。所以在这里当场断。
        if advice["status"] in TERMINAL_STATUSES:
            raise RuntimeError(
                f"银行受理回单不该是终态，实际 {advice['status']!r} —— "
                f"付款终态只能由 ap.observe 从 bank.query 问出来（铁律 8）")

        objects.execute(
            store,
            "INSERT OR REPLACE INTO payment_instruction (tenant_id, case_id, instruction_id,"
            " amount, currency, payment_means_code, bank, idempotency_key, revoked,"
            " submitted_at) VALUES (?,?,?,?,?,?,?,?,0,?)",
            (tenant_id, case_id, advice["instruction_id"], amount, case["currency"],
             instruction.payment_means_code, bank_name, key, C.now_iso()),
        )

        biz = guard.update_biz_status(
            store, tenant_id, case_id, "payment_requested",
            self.contract.name, invocation_id,
            reason=f"已向银行发出付款指令 {advice['instruction_id']}（{amount} "
                   f"{case['currency']}，方式 {advice['payment_means_name']}）；"
                   f"受理回单非终态，终态须经 bank.query 观察")

        return {
            "instruction_id": advice["instruction_id"],
            "idempotency_key": key,
            "amount": amount,
            "currency": case["currency"],
            "bank": bank_name,
            C.ADVICE_FIELD: advice,
            "biz_status": biz["biz_status"],
            "approved_by": [a["approver"] for a in approvals],
            "invocation_id": invocation_id,
        }
