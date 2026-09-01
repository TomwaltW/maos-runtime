"""ap.compensate —— 付款走不通之后的**域内**补偿收口：撤销付款指令 + 开对账工单。

## 为什么应付账款域要有自己的补偿，而不是复用控制面那个

`ControlPlane._execute_compensation` 是**逆补丁补偿**：读 `compensation` artifact 的
`patch_ref`，把正向 `patch_set` 在沙箱里反着打一遍。它的前提是「产物是代码」。
本域的产物不是补丁 —— 落地的是一条**发给银行的付款指令**，没有可以 `git apply -R`
的东西。所以 `human_decision(approved=False)` 走到 `_execute_compensation` 时会拿到
「无补偿引用，跳过回滚」并返回 None，那是**正确行为，不是缺陷**。

要还原的是业务侧的账：这笔付款指令作废、留一条补偿记录、开一张对账工单。
那三件事只有本域知道怎么做，所以落在本 skill 里。

## 本 skill 最要紧的一条：**不许宣布那笔钱没付出去**

走到补偿的典型场景是银行回单问不出来（轮询到顶仍是 `pending` / `unknown`）——
那一笔**可能已经划出去了**，只是回单没拿到。

此时把 `payment_instruction` 标成「已撤销、钱没出去」就是把外部状态写死为终态
（铁律 8），而且是最贵的一种写死：真付过的话，账面上会凭空少一笔，
而供应商那边收到了钱 —— 下个月对账时这笔差额没有人查得清是哪来的。

所以本 skill 落的 `payment_instruction_revoked` 记录，语义严格限定为

    「MAOS 侧不再推进这笔指令，并把它连同最后一次观察到的下落交给人」

而**不是**「这笔钱确认没付」。最后一次观察到的下落原样抄进 `detail_json`
（`last_observed_state`，一次都没观察到就是 `unobserved`），对账工单据此去银行流水
里核。这也是为什么补偿必须配一张工单：本系统能做的到此为止，剩下的只有人能做。

## 与 ap.observe 的分工

`ap.observe` 见到银行明确 `failed` 时只落一条观察，**不推进业务状态** —— 它在源码
里写明了理由。本 skill 就是它让出来的那一步：补偿真做完了，才把案子推到
`compensated`。

`settled` 的案子一律拒绝补偿：钱已经确认付出去了，再走补偿是数据被改坏的信号，
不是一次可以静默吞掉的空操作。
"""

from __future__ import annotations

import json

from maos.domain.ap import guard, objects
from maos.skills.contract import Skill, SkillContext, SkillContract
from maos.skills.registry import register_skill

from . import _common as C

#: 补偿记录的两种 kind。测试与场景按名取，不在各处抄字面量。
KIND_INSTRUCTION_REVOKED = "payment_instruction_revoked"
KIND_RECONCILIATION_TICKET = "reconciliation_ticket"

#: 一次也没观察到时的占位。**不写成 "failed"** —— 没问出来和问出失败是两回事，
#: 混起来就等于替银行下了结论。
UNOBSERVED = "unobserved"

#: 事件类型与控制面的逆补丁补偿共用一个名字：对审计与 Trace 来说，
#: 「补偿执行过了」是同一件事，按 `detail.domain` 区分是哪一种。
#: 另起一个名字会让「这个 Plan 到底补偿过没有」要查两处，漏一处就是假绿。
EVENT_COMPENSATION_EXECUTED = "CompensationExecuted"


@register_skill
class ApCompensateSkill(Skill):
    contract = SkillContract(
        name="ap.compensate",
        version="1.0.0",
        purpose="付款被驳回或问不出回单后的域内补偿收口：作废付款指令、写补偿记录与"
                "对账工单，把案子推进到 compensated",
        input_schema={
            "tenant_id": "str",
            "case_id": "str",
            "operator": "str（做出驳回/收口决定的人）",
            "reason": "str（为什么走补偿，原样进补偿记录与事件）",
            "assignee": "str（可选，对账工单的接单人，缺省同 operator）",
        },
        output_schema={
            "biz_status": "compensated",
            "revoked": "list[dict]（作废的付款指令；语义是「不再推进」，**不是**「确认未付」）",
            "last_observed_state": "str（最后一次观察到的下落；没观察到就是 unobserved）",
            "ticket": "dict（对账工单）",
            "invocation_id": "str",
        },
        preconditions=["tenant_id", "case_id", "operator"],
        depends_tools=[],
        failure_policy="escalate",
        max_retries=0,
        security_boundary=(
            "不碰银行、不写 ap_payment_observation（那是 ap.observe 的专属面）；"
            "biz_status 一律经 guard.update_biz_status，写不出 settled。"
            "已经 settled 的案子拒绝补偿 —— 那是数据被改坏的信号，不许静默吞掉"
        ),
        reuse_note="任何有外部不可逆动作的域都该有自己的补偿：作废本地意图 + 留下"
                   "最后观察 + 开一张给人的工单，三件缺一不可",
        owner_roles=["ap_compensation"],
    )

    def run(self, payload: dict, ctx: SkillContext) -> dict:
        store = C.ensure_schema(ctx)
        invocation_id = C.invocation_id_of(ctx)
        extras = getattr(ctx, "extras", None) or {}
        tenant_id, case_id, operator = C.required(
            payload, "tenant_id", "case_id", "operator")
        reason = str(payload.get("reason") or "")
        assignee = str(payload.get("assignee") or operator)

        case = guard.get_case(store, tenant_id, case_id)
        if case is None:
            raise LookupError(f"没有这个 case：tenant={tenant_id} case={case_id}")
        if case["biz_status"] == "settled":
            raise ValueError(
                f"case={case_id} 已经 settled（银行给过流水号），不许补偿 —— "
                f"钱确认付出去了还走补偿，说明数据被改坏了，这不是一次空操作")

        # ---- 1. 最后一次观察到的下落。**没有就是 unobserved，不是 failed** ----
        observations = guard.observations_of(store, tenant_id, case_id)
        last_observed = (observations[-1]["observed_state"] if observations
                         else UNOBSERVED)

        # ---- 2. 作废付款指令。语义是「不再推进」，见模块 docstring ------------
        instructions = objects.query(
            store, "SELECT * FROM payment_instruction WHERE tenant_id=? AND case_id=?"
                   " AND revoked=0 ORDER BY submitted_at", (tenant_id, case_id))
        revoked = []
        for ins in instructions:
            objects.execute(
                store, "UPDATE payment_instruction SET revoked=1 WHERE tenant_id=?"
                       " AND case_id=? AND instruction_id=?",
                (tenant_id, case_id, ins["instruction_id"]))
            revoked.append({"instruction_id": ins["instruction_id"],
                            "amount": ins["amount"], "currency": ins["currency"],
                            "bank": ins["bank"],
                            "idempotency_key": ins["idempotency_key"]})

        detail = {
            "reason": reason,
            "last_observed_state": last_observed,
            "observation_count": len(observations),
            "instructions": revoked,
            # 这句话原样进库。补偿记录会被人读，读的人必须一眼看到它**没有**在
            # 宣布那笔钱没付出去。
            "semantics": "MAOS 侧不再推进这些付款指令，并把它们连同最后一次观察到的"
                         "下落交给人；**不代表这笔钱确认未付**",
        }
        objects.execute(
            store,
            "INSERT OR REPLACE INTO ap_compensation_record (tenant_id, case_id, kind,"
            " detail_json, executed_at, operator) VALUES (?,?,?,?,?,?)",
            (tenant_id, case_id, KIND_INSTRUCTION_REVOKED,
             json.dumps(detail, ensure_ascii=False, sort_keys=True),
             C.now_iso(), operator))

        # ---- 3. 对账工单。本系统能做的到此为止，剩下的只有人能做 --------------
        ticket = {
            "ticket_id": f"recon-{tenant_id}-{case_id}",
            "assignee": assignee,
            "case_id": case_id,
            "invoice_id": case["invoice_id"],
            "po_id": case["po_id"],
            "supplier_id": case["supplier_id"],
            "amount_claimed": case["amount_claimed"],
            "currency": case["currency"],
            "last_observed_state": last_observed,
            "todo": ("去银行流水里核这几笔指令到底有没有出账；出账了就在外部系统补记账，"
                     "没出账就重开付款流程。**不要**凭本记录断定钱没付出去"),
            "instructions": [r["instruction_id"] for r in revoked],
        }
        objects.execute(
            store,
            "INSERT OR REPLACE INTO ap_compensation_record (tenant_id, case_id, kind,"
            " detail_json, executed_at, operator) VALUES (?,?,?,?,?,?)",
            (tenant_id, case_id, KIND_RECONCILIATION_TICKET,
             json.dumps(ticket, ensure_ascii=False, sort_keys=True),
             C.now_iso(), operator))

        # ---- 4. 补偿做完了，才推进到 compensated -----------------------------
        biz = guard.update_biz_status(
            store, tenant_id, case_id, "compensated",
            self.contract.name, invocation_id,
            reason=f"域内补偿收口：作废 {len(revoked)} 笔付款指令，最后观察到的下落 "
                   f"{last_observed}，对账工单 {ticket['ticket_id']} -> {assignee}"
                   f"（{reason}）")

        # ---- 5. 落事件。补偿执行过了这件事不能只活在日志里 --------------------
        store.append_event_log({
            "trace_id": str(extras.get("trace_id") or ""),
            "plan_id": str(extras.get("plan_id") or case["plan_id"]),
            "task_id": extras.get("task_id"),
            "event_type": EVENT_COMPENSATION_EXECUTED,
            "reason": reason,
            "detail": {"domain": guard.DOMAIN, "tenant_id": tenant_id,
                       "case_id": case_id, "operator": operator,
                       "revoked": len(revoked), "ticket_id": ticket["ticket_id"],
                       "last_observed_state": last_observed,
                       "invocation_id": invocation_id},
        })

        return {
            "biz_status": biz["biz_status"],
            "revoked": revoked,
            "last_observed_state": last_observed,
            "ticket": ticket,
            "invocation_id": invocation_id,
        }
