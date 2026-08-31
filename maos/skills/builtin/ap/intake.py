"""ap.intake —— 收票：把一张供应商发票连同它引用的采购订单与收货单建成一个应付案子。

这一步**不做任何匹配判定**，只做三件事：

  1. 确认三单都在库里（缺任何一份，匹配就无从谈起）；
  2. 建 `ap_case`（`received`），幂等；
  3. 把 Task 挂到三个业务对象上（`ap_business_ref`，只存引用不存副本）。

## 为什么「三单齐不齐」在这里判，「三单对不对」在 ap.match 判

两者是不同性质的失败：

  · **单据缺失** —— 数据还没到齐，重试可能就好了（供应商晚一天传发票、
    WMS 的收货单还在同步）。这一档抛异常，任务失败，可返工。
  · **单据对不上** —— 数据齐了但内容有分歧，重试一万次也一样。这一档不抛异常，
    而是产出一份带 `BR-xx` 编号的拒付理由清单，交给人看（见 `match.py`）。

混在一处的后果是「发票晚到」被当成「拒付」，或者反过来「拒付」被当成「再等等」。

## 幂等

收票会被返工重跑，而 `ap_case` 的主键是 `(tenant_id, case_id)`。幂等语义与冲突
处置全在 `guard.create_case()` 里，本 skill 只是把 seed 里的字段递进去 ——
**不在这里再写一套 upsert**，那就有了第二条写入路径。
"""

from __future__ import annotations

from maos.domain.ap import guard, objects
from maos.skills.contract import Skill, SkillContext, SkillContract
from maos.skills.registry import register_skill
from maos.tools import ap_codes

from . import _common as C


@register_skill
class ApIntakeSkill(Skill):
    contract = SkillContract(
        name="ap.intake",
        version="1.0.0",
        purpose="收供应商发票，确认三单齐备并建出 ap_case（received），把 Task 挂到业务对象上",
        input_schema={
            "tenant_id": "str",
            "case_id": "str",
            "invoice_id": "str（发票池里那一张）",
            "po_id": "str（采购订单号）",
            "po_version": "int（订单快照版本 —— 权威在 ERP，我们存的是读到的那一版）",
            "gr_id": "str（收货单号）",
        },
        output_schema={
            "case": "dict（ap_case 当前那一行）",
            "invoice": "dict（发票抬头，含 UNCL1001 类型码与其官方名称）",
            "three_way": "dict（三单齐备情况：各自的行数）",
            "refs": "list[dict]（挂上去的 ap_business_ref）",
            "invocation_id": "str",
        },
        preconditions=["tenant_id", "case_id", "invoice_id", "po_id", "gr_id"],
        depends_tools=[],
        failure_policy="escalate",
        max_retries=0,
        security_boundary=(
            "只建案不判定；biz_status 一律由 guard.create_case 落成 received，"
            "调用方指定不了。三单读取一律经 domain/ap/objects.py 的具名读取函数，"
            "本 skill 不自己写 SQL"
        ),
        reuse_note="任何「先确认外部单据齐备、再建本地案子」的域都该照此分层："
                   "齐不齐是可重试的失败，对不对是要人看的结论",
        owner_roles=["ap_intake"],
    )

    def run(self, payload: dict, ctx: SkillContext) -> dict:
        store = C.ensure_schema(ctx)
        invocation_id = C.invocation_id_of(ctx)
        extras = getattr(ctx, "extras", None) or {}
        tenant_id, case_id, invoice_id, po_id, gr_id = C.required(
            payload, "tenant_id", "case_id", "invoice_id", "po_id", "gr_id")
        po_version = int(payload.get("po_version") or 1)

        # ---- 1. 三单齐备 ---------------------------------------------------
        invoice = objects.get_invoice(store, tenant_id, invoice_id)
        if invoice is None:
            raise LookupError(
                f"发票 {invoice_id} 不在库里（tenant={tenant_id}）—— 先把发票池那一版落库")
        po = objects.get_purchase_order(store, tenant_id, po_id, po_version)
        if po is None:
            raise LookupError(
                f"采购订单 {po_id} v{po_version} 不在库里 —— 三单匹配无从谈起")
        gr_rows = objects.gr_lines(store, tenant_id, gr_id)
        if not gr_rows:
            raise LookupError(
                f"收货单 {gr_id} 没有任何行 —— 货还没收到就收到票，这一步不许放过")

        inv_rows = objects.invoice_lines(store, tenant_id, invoice_id)
        if not inv_rows:
            raise LookupError(f"发票 {invoice_id} 没有任何行")
        po_rows = objects.po_lines(store, tenant_id, po_id, po_version)
        if not po_rows:
            raise LookupError(f"采购订单 {po_id} v{po_version} 没有任何行")

        # ---- 2. 建案（幂等，口径全在 guard 里）-------------------------------
        case = guard.create_case(
            store,
            tenant_id=tenant_id, case_id=case_id,
            supplier_id=str(invoice["supplier_id"]),
            po_id=po_id, po_version=po_version,
            invoice_id=invoice_id, gr_id=gr_id,
            amount_claimed=invoice["amount_due"],
            currency=str(invoice["currency"]),
            plan_id=str(extras.get("plan_id") or ""),
            actor_skill=self.contract.name,
            invocation_id=invocation_id,
        )

        # ---- 3. 挂引用（只存引用，不存副本）---------------------------------
        refs = []
        plan_id = str(extras.get("plan_id") or "")
        task_id = str(extras.get("task_id") or "")
        if plan_id and task_id:
            for object_type, object_id, version, purpose in (
                ("ap_case", case_id, 0, "本次应付处理的案子"),
                ("supplier_invoice", invoice_id, 0, "三单之一：供应商发票"),
                ("purchase_order", po_id, po_version, "三单之二：采购订单（读到的那一版）"),
                ("goods_receipt", gr_id, 0, "三单之三：收货单"),
            ):
                refs.append(objects.attach_business_ref(
                    store, plan_id=plan_id, task_id=task_id, tenant_id=tenant_id,
                    object_type=object_type, object_id=object_id,
                    object_version=version, purpose=purpose))

        # 发票类型码原样回显 + 补上官方名称。**码不在表里就抛** ——
        # 不在这里悄悄放过，让它一路走到匹配那步才以另一条理由被拒。
        type_code = str(invoice["invoice_type_code"])
        type_entry = ap_codes.require_code(ap_codes.LIST_INVOICE_TYPE, type_code)

        return {
            "case": case,
            "invoice": {
                "invoice_id": invoice_id,
                "supplier_id": invoice["supplier_id"],
                "po_id": po_id,
                "invoice_type_code": type_code,
                "invoice_type_name": type_entry.name,
                "currency": invoice["currency"],
                "issued_at": invoice["issued_at"],
                "due_at": invoice["due_at"],
                "amount_due": invoice["amount_due"],
            },
            "three_way": {
                "invoice_lines": len(inv_rows),
                "po_lines": len(po_rows),
                "gr_lines": len(gr_rows),
                "po_version": po_version,
            },
            "refs": refs,
            "invocation_id": invocation_id,
        }
