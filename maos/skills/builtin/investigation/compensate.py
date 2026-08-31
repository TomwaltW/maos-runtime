"""investigation.compensate —— 差错处理走不通之后的**域内**补偿收口。

## 为什么本域要有自己的补偿，而不是复用控制面那个

`ControlPlane._execute_compensation` 是**逆补丁补偿**：读 `compensation` artifact 的
`patch_ref`，把正向 `patch_set` 在沙箱里反着打一遍。它的前提是「产物是代码」。
本域的产物不是补丁 —— 落地的是一份**已经发给清算方的 camt.056**，
没有可以 `git apply -R` 的东西。所以驳回走到 `_execute_compensation` 时会拿到
「无补偿引用，跳过回滚」并返回 None，那是**正确行为，不是缺陷**。

要还原的是业务侧的账：撤回这份请求、留一条补偿记录、开一张人工对账工单。
那三件事只有本域知道怎么做，所以落在本 skill 里。

## 本 skill 最要紧的一条：**不许宣布那笔钱没退回来**

走到补偿的典型场景是清算方回了 `CNCL`（撤销确认）却始终没有 pacs.004，
或者一路 `PDCR` 问不出结果 —— 两种情况下**资金到底回没回来，我们不知道**。

此时把案子标成「撤销失败、钱没回来」就是把外部状态写死为终态（铁律 8），
而且是最贵的一种写死：真退回过的话，账面上会凭空多出一笔待处理的差错。

所以本 skill 落的 `cancellation_withdrawn` 记录，语义严格限定为

    「MAOS 侧不再推进这份撤销请求，并把它连同最后一次观察到的下落交给人」

而**不是**「这笔撤销确认失败」。最后一次观察到的下落原样抄进 `detail_json`
（`last_observed_state`，一次都没观察到就是 `unobserved`），人工据此去清算系统对账。

`cancellation_confirmed` 这一档尤其要留全：它意味着**清算方已经确认撤销了**，
人工对账时第一件事就是去查那笔资金是不是已经在路上 —— 把它压成「失败」，
对账的人会从错误的方向开始查。

## 与 `investigation.observe` 的分工

`observe` 见到明确被拒或问不出资金下落时只落观察，**不推进业务状态** ——
它在源码里写明了理由。本 skill 就是它让出来的那一步：补偿真做完了，
才把案子推到 `compensated`。

`returned` 的案子一律拒绝补偿：钱已经确认退回来了，再走补偿是数据被改坏的信号，
不是一次可以静默吞掉的空操作。
"""

from __future__ import annotations

import json

from maos.domain.investigation import guard, objects
from maos.skills.contract import Skill, SkillContext, SkillContract
from maos.skills.registry import register_skill

from . import _common as C

#: 补偿记录的两种 kind。测试与场景按名取，不在各处抄字面量。
KIND_CANCELLATION_WITHDRAWN = "cancellation_withdrawn"
KIND_MANUAL_TICKET = "manual_reconciliation_ticket"

#: 一次也没观察到时的占位。**不写成 "rejected"** —— 没问出来和问出被拒是两回事，
#: 混起来就等于替清算方下了结论。
UNOBSERVED = guard.OBS_UNOBSERVED

#: 事件类型与控制面的逆补丁补偿共用一个名字：对审计与 Trace 来说，
#: 「补偿执行过了」是同一件事，按 `detail.domain` 区分是哪一种。
#: 另起一个名字会让「这个 Plan 到底补偿过没有」要查两处，漏一处就是假绿。
EVENT_COMPENSATION_EXECUTED = "CompensationExecuted"


@register_skill
class InvestigationCompensateSkill(Skill):
    contract = SkillContract(
        name="investigation.compensate",
        version="1.0.0",
        purpose=("撤销走不通后的域内补偿收口：撤回 camt.056、写补偿记录与人工对账工单，"
                 "把案子推进到 compensated"),
        input_schema={
            "tenant_id": "str",
            "case_id": "str",
            "operator": "str（做出驳回/收口决定的人）",
            "reason": "str（为什么走补偿，原样进补偿记录与事件）",
            "assignee": "str（可选，人工工单的接单人，缺省同 operator）",
        },
        output_schema={
            "biz_status": "compensated",
            "withdrawn": "list[dict]（每份撤回的 camt.056 及其最后观察到的下落）",
            "ticket": "dict（人工对账工单：单号、接单人、要人去做什么）",
            "records": "int（落进 investigation_compensation 的行数）",
            "last_observed_state": "str（returned|cancellation_confirmed|rejected|"
                                   "pending|unobserved）",
            "invocation_id": "str",
        },
        preconditions=["tenant_id", "case_id", "operator", "reason"],
        depends_tools=[],
        # 不重试：补偿是写账动作，重试一次就多一条记录。失败要人看见，不要自己再来一遍。
        failure_policy="escalate",
        max_retries=0,
        security_boundary=(
            "写 investigation_compensation 与 biz_status(compensated)；"
            "**不写 returned**（那是 investigation.observe 的权威边界，guard 会抛）；"
            "**不宣布外部资金结果** —— 撤回记录只表示 MAOS 侧不再推进，"
            "最后一次观察到的下落原样留档交人工对账；"
            "已 returned 的案子拒绝补偿，不静默跳过"
        ),
        reuse_note=("任何「外部已经收到指令、但本地要收口」的域都该照此写："
                    "先留档最后一次观察、再开人工工单、最后才推进本地状态；三步顺序不可换"),
        owner_roles=["investigation_observe"],
    )

    def run(self, payload: dict, ctx: SkillContext) -> dict:
        store = C.ensure_schema(ctx)
        invocation_id = C.invocation_id_of(ctx)
        extras = getattr(ctx, "extras", None) or {}
        tenant_id, case_id, operator, reason = C.required(
            payload, "tenant_id", "case_id", "operator", "reason")

        case = guard.get_case(store, tenant_id, case_id)
        if case is None:
            raise LookupError(f"没有这个 case：tenant={tenant_id} case={case_id}")

        # 已确认资金退回的案子不许补偿。这不是防御性编程，是数据一致性的报警器：
        # 走到这里说明有人拿一笔已经退回的款去做撤回，静默跳过会把错误埋掉。
        if case["biz_status"] == "returned":
            raise ValueError(
                f"case={case_id} 已经是 returned（资金确认退回），不许补偿；"
                "补偿是给走不通的案子收口用的，对已退回的案子撤回等于凭空抹掉"
                "一笔已发生的资金动作")

        now = C.now_iso()
        last_state = self._last_observed_state(store, tenant_id, case_id)
        requests = objects.query(
            store,
            "SELECT * FROM cancellation_request WHERE tenant_id=? AND case_id=?"
            " ORDER BY sent_at", (tenant_id, case_id))

        # ---- 第一步：把每份撤销请求连同**最后观察到的下落**留档 ----------------
        # 顺序不可换：先留档再推状态。状态一旦落 compensated，「外面还有一份
        # 下落不明的 camt.056」这件事就没人记得了 —— 与 control_plane.human_decision
        # 「先回滚再改状态」同一个理由。
        withdrawn = []
        for row in requests:
            detail = {
                "request_id": row["request_id"],
                "idempotency_key": row["idempotency_key"],
                "message_type": row["message_type"],
                "reason_code": row["reason_code"],
                "clearing_house": row["clearing_house"],
                "amount": row["amount"],
                "currency": row["currency"],
                # 这一行是整条记录的题眼：撤回的是**我们这边的推进**，
                # 不是那笔资金的结论。cancellation_confirmed / pending / unobserved
                # 时尤其不许改写成 rejected。
                "last_observed_state": last_state,
                "meaning": ("MAOS 侧不再推进本撤销请求；资金下落以 last_observed_state 为准，"
                            "需人工到清算系统按 end-to-end 参考号对账后确认"),
                "reason": reason,
            }
            self._record(store, tenant_id=tenant_id, case_id=case_id,
                         kind=KIND_CANCELLATION_WITHDRAWN, detail=detail,
                         executed_at=now, operator=operator)
            withdrawn.append(detail)

        # ---- 第二步：开人工对账工单 —— 本系统能做的到此为止 --------------------
        ticket = {
            "ticket_id": f"INV-{case_id}",
            "assignee": str(payload.get("assignee") or operator),
            "case_id": case_id,
            "reason": reason,
            "last_observed_state": last_state,
            "todo": self._todo_for(last_state),
            "requests": [r["request_id"] for r in requests],
            "end_to_end_id": case["end_to_end_id"],
            "original_msg_id": case["original_msg_id"],
            "opened_at": now,
        }
        self._record(store, tenant_id=tenant_id, case_id=case_id,
                     kind=KIND_MANUAL_TICKET, detail=ticket,
                     executed_at=now, operator=operator)

        # ---- 第三步：推进业务状态。guard 是唯一入口，越权写 returned 会被它拦 ----
        case = guard.update_biz_status(
            store, tenant_id, case_id, "compensated",
            self.contract.name, invocation_id,
            reason=f"{operator} 驳回后域内补偿收口：{reason}（最后观察到 {last_state}）")

        store.append_event_log({
            "trace_id": str(extras.get("trace_id") or ""),
            "plan_id": str(extras.get("plan_id") or case.get("plan_id") or ""),
            "task_id": str(extras.get("task_id") or ""),
            "event_type": EVENT_COMPENSATION_EXECUTED,
            "reason": reason,
            "detail": {
                # domain 键区分本条是哪个域的补偿 —— 三种补偿共用事件名，
                # 审计时按这个键分流。
                "domain": C.BIZ_TYPE,
                "tenant_id": tenant_id,
                "case_id": case_id,
                "operator": operator,
                "withdrawn_requests": [r["request_id"] for r in withdrawn],
                "last_observed_state": last_state,
                "ticket_id": ticket["ticket_id"],
                "records": len(withdrawn) + 1,
                "invocation_id": invocation_id,
            },
        })

        return {
            "biz_status": case["biz_status"],
            "withdrawn": withdrawn,
            "ticket": ticket,
            "records": len(withdrawn) + 1,
            "last_observed_state": last_state,
            "invocation_id": invocation_id,
        }

    # ------------------------------------------------------------------
    @staticmethod
    def _todo_for(last_state: str) -> list[str]:
        """按最后观察到的下落给人不同的对账起点。

        **不是装饰**：`cancellation_confirmed` 与 `unobserved` 的对账动作完全不同 ——
        前者清算方已经确认撤销、资金很可能在路上，后者连请求收没收到都不知道。
        给一份通用 todo 会让对账的人从错误的方向开始查。
        """
        common_tail = [
            "对客户/对手行回执并关闭本案",
        ]
        if last_state == guard.OBS_CANCELLATION_CONFIRMED:
            return [
                "清算方已回 camt.029/CNCL 确认撤销，但未收到 pacs.004 退款报文 ——"
                "先到清算系统按 end-to-end 参考号查该笔资金是否已在退回途中",
                "已在途：等待 pacs.004 到达后按正常流程收口，不要重发 camt.056",
                "确认未退：按人工调账流程发起退回，并留档说明为何 CNCL 之后资金未到",
            ] + common_tail
        if last_state == guard.OBS_REJECTED:
            return [
                "清算方已明确拒绝本次撤销（见观察里的 rejection_code 与官方定义）——"
                "按拒绝原因判断是改单重发还是走线下协商",
                "不要原样重发同一份 camt.056：拒绝原因不消除，重发必然再次被拒",
            ] + common_tail
        return [
            "未从清算方问出任何结论 —— 先到清算系统按 end-to-end 参考号核对"
            "camt.056 是否已被受理",
            "已受理：继续等待决议，不要重发（重发会开出第二个 case）",
            "未受理：按人工流程重新发起撤销",
        ] + common_tail

    @staticmethod
    def _last_observed_state(store, tenant_id: str, case_id: str) -> str:
        """**收口这一份请求**最后一次观察到的下落。一次都没观察到返回 UNOBSERVED。

        按当前那一份 `cancellation_request` 的 `request_id` 收窄，不按
        `(tenant, case)` 查全表：一个案子可能先后有多份请求，按全表取最新会把
        **上一份的下落**当成这一份的 —— 而这种错没有症状：补偿记录、人工工单、
        事件全都正常，只有「外面那笔钱到底怎么样了」这一格填错，
        偏偏那是人工对账唯一的依据（退款域 `refund.compensate` 踩过同一个坑）。

        刻意不兜底成 "rejected"：问不出结果时表可能是空的 —— 把空表读成「被拒」
        正是本 skill 通篇在防的那个推断。
        """
        current = objects.query(
            store,
            "SELECT request_id FROM cancellation_request WHERE tenant_id=? AND case_id=?"
            " ORDER BY sent_at DESC", (tenant_id, case_id))
        if not current:
            return UNOBSERVED
        rows = objects.query(
            store,
            "SELECT observed_state FROM resolution_observation"
            " WHERE tenant_id=? AND case_id=? AND request_id=?"
            " ORDER BY observed_at DESC",
            (tenant_id, case_id, current[0]["request_id"]))
        return str(rows[0]["observed_state"]) if rows else UNOBSERVED

    @staticmethod
    def _record(store, *, tenant_id: str, case_id: str, kind: str, detail: dict,
                executed_at: str, operator: str) -> None:
        objects.execute(
            store,
            "INSERT OR REPLACE INTO investigation_compensation (tenant_id, case_id, kind,"
            " detail_json, executed_at, operator) VALUES (?,?,?,?,?,?)",
            (tenant_id, case_id, kind,
             json.dumps(detail, ensure_ascii=False, sort_keys=True),
             executed_at, operator),
        )
