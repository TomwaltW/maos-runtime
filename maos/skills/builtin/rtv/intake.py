"""rtv.intake —— 受理退货：把一笔退货诉求定位到**源 PO 与源收货单**，再建案。

SOP 第 ① 步（PeopleSoft《Understanding the RTV Business Process》）。这一步
**不做任何裁定**，只做三件事：

  1. 确认退的货指得回去：源采购订单（含版本）与源收货单都在库里，且每一条退货行
     都指得到一条真实的收货行；
  2. 建 `rtv_case`（`received`）与 `rtv_line`，幂等；
  3. 把 Task 挂到业务对象上（`rtv_business_ref`，只存引用不存副本）。

## 为什么「指不指得回去」在这里判，「该不该退」在 rtv.dispose 判

两者是不同性质的失败，口径同 `ap/intake.py`：

  · **指不回去** —— 源单没到齐或行号对不上，重试可能就好了（WMS 的收货单还在同步）。
    这一档抛异常，任务失败，可返工。
  · **该不该退、退成什么** —— 数据齐了但要按合同条款裁定，重试一万次也一样。
    这一档不抛异常，而是产出一份带 `RTV-R-xx` 编号的裁定（见 `dispose.py`）。

混在一处的后果是「收货单晚到」被当成「不予退货」，或者反过来。

## 为什么退货量在这里就要卡住不超过合格数

`goods_receipt_line.quantity_received` 是**审计量、永不变**（PeopleSoft 口径），
退货量单独记在 `rtv_line`。退得比收得多不是一个可裁定的分歧，是数据错 ——
放它过去，对账那步算出来的应退金额会凭空多出一截，而那份金额会被拿去和供应商的
贷项通知单比，差异理由会挂在供应商头上。

## return_action 建案时恒为空串

受理的人不该替裁定的人拍板（契约 C-R1）。`create_case()` 不接受调用方指定它，
也不接受指定 `biz_status` —— 想直接建成 credited 的路必须从一开始就不存在。
"""

from __future__ import annotations

from maos.skills.contract import Skill, SkillContext, SkillContract
from maos.skills.registry import register_skill

from . import _common as C


@register_skill
class RtvIntakeSkill(Skill):
    contract = SkillContract(
        name="rtv.intake",
        version="1.0.0",
        purpose="受理退货诉求，定位源 PO 与收货单，建出 rtv_case（received）并挂业务对象引用",
        input_schema={
            "tenant_id": "str",
            "case_id": "str",
            "po_id": "str（源采购订单号）",
            "po_version": "int（订单快照版本 —— 权威在 ERP，我们存的是读到的那一版）",
            "gr_id": "str（源收货单号）",
            "lines": "list[dict]：gr_line_no / sku / quantity_returned / unit_price /"
                     " reason_code（理由码必在 RETURN_REASONS 里）",
            "currency": "str（可选，缺省 CNY）",
        },
        output_schema={
            "case": "dict（rtv_case 当前那一行）",
            "lines": "list[dict]（落库的 rtv_line）",
            "source": "dict（源单定位结果：po_id/po_version/gr_id 与各自的行数）",
            "amount_claimed": "str（按退货行算出来的自称应退金额，两位小数）",
            "refs": "list[dict]（挂上去的 rtv_business_ref）",
            "invocation_id": "str",
        },
        preconditions=["tenant_id", "case_id", "po_id", "gr_id", "lines"],
        depends_tools=[],
        failure_policy="escalate",
        max_retries=0,
        security_boundary=(
            "只建案不裁定：biz_status 一律由 create_case 落成 received、return_action "
            "恒为空串，调用方指定不了；credited / settled 在本 skill 里没有任何写入路径。"
            "源单只读不写 —— supplier / purchase_order / goods_receipt 五张表复用 ap 域"
            "的定义，本域一行都不往里写"
        ),
        reuse_note="任何「先把诉求定位到外部源单、再建本地案子」的域都该照此分层："
                   "指不指得回去是可重试的失败，该不该办是要裁定的结论",
        owner_roles=["rtv_intake"],
    )

    def run(self, payload: dict, ctx: SkillContext) -> dict:
        store = C.ensure_schema(ctx)
        invocation_id = C.invocation_id_of(ctx)
        extras = getattr(ctx, "extras", None) or {}
        tenant_id, case_id, po_id, gr_id, lines = C.required(
            payload, "tenant_id", "case_id", "po_id", "gr_id", "lines")
        po_version = int(payload.get("po_version") or 1)

        # ---- 1. 源单定位 ---------------------------------------------------
        po_rows = C.query(
            store, "SELECT * FROM purchase_order WHERE tenant_id=? AND po_id=? AND version=?",
            (tenant_id, po_id, po_version))
        if not po_rows:
            raise LookupError(
                f"采购订单 {po_id} v{po_version} 不在库里（tenant={tenant_id}）——"
                " 退货指不回源头，后面的对账就无从勾稽")
        po = po_rows[0]

        gr_rows = C.query(
            store, "SELECT * FROM goods_receipt WHERE tenant_id=? AND gr_id=?",
            (tenant_id, gr_id))
        if not gr_rows:
            raise LookupError(f"收货单 {gr_id} 不在库里 —— 没收过的货退不了")
        if str(gr_rows[0]["po_id"]) != str(po_id):
            raise ValueError(
                f"收货单 {gr_id} 挂的是 {gr_rows[0]['po_id']}，不是 {po_id}；"
                "源单对不上，这一步不许放过")

        gr_lines = C.query(
            store, "SELECT * FROM goods_receipt_line WHERE tenant_id=? AND gr_id=?"
                   " ORDER BY line_no", (tenant_id, gr_id))
        if not gr_lines:
            raise LookupError(f"收货单 {gr_id} 没有任何行")
        by_line_no = {int(r["line_no"]): r for r in gr_lines}

        # ---- 2. 逐条退货行核对：指得回去、品名一致、量不超合格数 -------------
        prepared, total = [], C.money("0")
        for idx, item in enumerate(lines, start=1):
            gr_line_no = int(item["gr_line_no"])
            source = by_line_no.get(gr_line_no)
            if source is None:
                raise LookupError(
                    f"退货行 {idx} 指的收货行 {gr_line_no} 不在收货单 {gr_id} 里"
                    f"（该单的行号：{sorted(by_line_no)}）")
            sku = str(item["sku"])
            if sku != str(source["sku"]):
                raise ValueError(
                    f"退货行 {idx} 的 sku {sku!r} 与收货行 {gr_line_no} 的 "
                    f"{source['sku']!r} 不一致 —— 退的不是收的那件货")
            quantity = float(item["quantity_returned"])
            accepted = float(source["quantity_received"]) - float(
                source.get("quantity_rejected") or 0)
            if quantity <= 0:
                raise ValueError(f"退货行 {idx} 的退货量必须为正，实际 {quantity}")
            if quantity > accepted:
                raise ValueError(
                    f"退货行 {idx} 要退 {quantity}，而收货行 {gr_line_no} 的合格数只有 "
                    f"{accepted} —— 退得比收得多是数据错，不是可裁定的分歧")
            reason_code = str(item["reason_code"])
            C.require_reason(reason_code)          # 码不在表里就抛，不悄悄放过
            unit_price = C.money_str(item["unit_price"])
            total += C.money(unit_price) * C.money(str(quantity))
            prepared.append({
                "tenant_id": tenant_id, "case_id": case_id, "line_no": idx,
                "gr_line_no": gr_line_no, "sku": sku,
                "quantity_returned": quantity, "unit_price": unit_price,
                "reason_code": reason_code,
            })

        amount_claimed = C.money_str(total)

        # ---- 3. 建案（幂等，口径全在守卫里）---------------------------------
        case = C.create_case(
            store, tenant_id=tenant_id, case_id=case_id,
            supplier_id=str(po["supplier_id"]), po_id=po_id, po_version=po_version,
            gr_id=gr_id, amount_claimed=amount_claimed,
            currency=str(payload.get("currency") or po["currency"] or "CNY"),
            plan_id=str(extras.get("plan_id") or ""),
            actor_skill=self.contract.name, invocation_id=invocation_id)

        for row in prepared:
            C.execute(
                store,
                "INSERT OR REPLACE INTO rtv_line (tenant_id, case_id, line_no, gr_line_no,"
                " sku, quantity_returned, unit_price, reason_code) VALUES (?,?,?,?,?,?,?,?)",
                (row["tenant_id"], row["case_id"], row["line_no"], row["gr_line_no"],
                 row["sku"], row["quantity_returned"], row["unit_price"],
                 row["reason_code"]))

        # ---- 4. 挂引用（只存引用，不存副本）---------------------------------
        refs = []
        plan_id = str(extras.get("plan_id") or "")
        task_id = str(extras.get("task_id") or "")
        if plan_id and task_id:
            for object_table, object_id, version, purpose in (
                ("rtv_case", case_id, 0, "本次退货处理的案子"),
                ("purchase_order", po_id, po_version, "源头之一：采购订单（读到的那一版）"),
                ("goods_receipt", gr_id, 0, "源头之二：收货单"),
            ):
                refs.append(C.attach_business_ref(
                    store, plan_id=plan_id, task_id=task_id, object_table=object_table,
                    object_id=object_id, object_version=version, purpose=purpose))

        return {
            "case": case,
            "lines": C.lines_of(store, tenant_id, case_id),
            "source": {
                "po_id": po_id, "po_version": po_version, "gr_id": gr_id,
                "supplier_id": str(po["supplier_id"]),
                "gr_lines": len(gr_lines), "rtv_lines": len(prepared),
            },
            "amount_claimed": amount_claimed,
            "refs": refs,
            "invocation_id": invocation_id,
        }
