"""refund.compensate —— 退款走不通之后的**域内**补偿收口。

## 为什么退款域要有自己的补偿，而不是复用控制面那个

`ControlPlane._execute_compensation` 是**逆补丁补偿**：读 `compensation` artifact 的
`patch_ref`，把正向 `patch_set` 在沙箱里反着打一遍。它的前提是「产物是代码」。
退款域的产物不是补丁 —— 落地的是一笔**发给外部支付网关的请求**，没有可以 `git apply -R`
的东西。所以 `human_decision(approved=False)` 走到 `_execute_compensation` 时会拿到
「无补偿引用，跳过回滚」并返回 None，那是**正确行为，不是缺陷**：代码域没有东西要还原。

要还原的是业务侧的账：这笔退款请求作废、留一条补偿记录、开一张人工工单。
那三件事只有本域知道怎么做，所以落在本 skill 里。

## 本 skill 最要紧的一条：**不许宣布那笔钱没退出去**

走到补偿的典型场景是网关回了 `ACQ.SYSTEM_ERROR` / `20000` 这类
`retriable=True, outcome=unknown` 的码 —— 官方 remedy 原文是「保持参数不变重试
**或查询执行结果**」，也就是说**网关自己都不知道那一笔到底执行了没有**。

此时把 `refund_request` 标成「已撤销、钱没出去」就是把外部状态写死为终态（铁律 8），
而且是最贵的一种写死：真退过的话，账面上会凭空少一笔。所以本 skill 落的
`refund_request_revoked` 记录，语义严格限定为

    「MAOS 侧不再推进这笔请求，并把它连同最后一次观察到的下落交给人」

而**不是**「这笔钱确认没退」。最后一次观察到的下落原样抄进 `detail_json`
（`last_observed_state`，没观察到就是 `unobserved`），人工工单据此去外部系统对账。
这也是为什么补偿必须配一张工单：本系统能做的到此为止，剩下的只有人能做。

## 与 `payment.observe` 的分工

`payment.observe` 见到网关明确 `failed` 时只落一条 `payment_observation`，
**不推进业务状态** —— 它在源码里写明了理由：「走到 compensated 意味着补偿已经做完，
而补偿是失败路径场景的事，在这里替它宣布收口就是又一次把状态写死」。
本 skill 就是它让出来的那一步：补偿真做完了，才把案子推到 `compensated`。

`settled` 的案子一律拒绝补偿：钱已经确认退出去了，再走补偿是数据被改坏的信号，
不是一次可以静默吞掉的空操作。
"""

from __future__ import annotations

import json

from maos.domain.refund import guard, objects
from maos.skills.contract import Skill, SkillContext, SkillContract
from maos.skills.registry import register_skill

from . import _common as C

#: 补偿记录的两种 kind。测试与场景按名取，不在各处抄字面量。
KIND_REQUEST_REVOKED = "refund_request_revoked"
KIND_MANUAL_TICKET = "manual_ticket"

#: 一次也没观察到时的占位。**不写成 "failed"** —— 没问出来和问出失败是两回事，
#: 混起来就等于替网关下了结论。
UNOBSERVED = "unobserved"

#: 事件类型与控制面的逆补丁补偿共用一个名字：对审计与 Trace 来说，
#: 「补偿执行过了」是同一件事，按 `detail.domain` 区分是哪一种。
#: 另起一个名字会让「这个 Plan 到底补偿过没有」要查两处，漏一处就是假绿。
EVENT_COMPENSATION_EXECUTED = "CompensationExecuted"


@register_skill
class RefundCompensateSkill(Skill):
    contract = SkillContract(
        name="refund.compensate",
        version="1.0.0",
        purpose="退款被驳回或走不通后的域内补偿收口：作废退款请求、写补偿记录与人工工单，"
                "把案子推进到 compensated",
        input_schema={
            "tenant_id": "str",
            "case_id": "str",
            "operator": "str（做出驳回/收口决定的人）",
            "reason": "str（为什么走补偿，原样进补偿记录与事件）",
            "assignee": "str（可选，人工工单的接单人，缺省同 operator）",
        },
        output_schema={
            "biz_status": "compensated",
            "revoked": "list[dict]（每笔作废的 refund_request 及其最后观察到的下落）",
            "ticket": "dict（人工工单：单号、接单人、要人去做什么）",
            "records": "int（落进 compensation_record 的行数）",
            "last_observed_state": "str（settled|failed|processing|unknown|unobserved）",
            "invocation_id": "str",
        },
        preconditions=["tenant_id", "case_id", "operator", "reason"],
        depends_tools=[],
        # 不重试：补偿是写账动作，重试一次就多一条记录。失败要人看见，不要自己再来一遍。
        failure_policy="escalate",
        max_retries=0,
        security_boundary=(
            "写 compensation_record 与 biz_status(compensated)；"
            "**不写 settled**（那是 payment.observe 的权威边界，guard 会抛）；"
            "**不宣布外部资金结果** —— 作废记录只表示 MAOS 侧不再推进，"
            "最后一次观察到的下落原样留档交人工对账；"
            "已 settled 的案子拒绝补偿，不静默跳过"
        ),
        reuse_note=(
            "任何「外部已经收到请求、但本地要收口」的域都该照此写：先留档最后一次观察、"
            "再开人工工单、最后才推进本地状态；三步顺序不可换"
        ),
        owner_roles=["refund_payment"],
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

        # 已确认退款成功的案子不许补偿。这不是防御性编程，是数据一致性的报警器：
        # 走到这里说明有人拿一笔已经成功的退款去做撤销，静默跳过会把错误埋掉。
        if case["biz_status"] == "settled":
            raise ValueError(
                f"case={case_id} 已经是 settled（退款确认成功），不许补偿；"
                "补偿是给走不通的案子收口用的，对成功的案子撤销等于凭空抹掉一笔已发生的资金动作")

        now = C.now_iso()
        last_state = self._last_observed_state(store, tenant_id, case_id)
        requests = objects.query(
            store,
            "SELECT * FROM refund_request WHERE tenant_id=? AND case_id=?"
            " ORDER BY submitted_at", (tenant_id, case_id))

        # ---- 第一步：把每一笔退款请求连同**最后观察到的下落**留档 ----------
        # 顺序不可换：先留档再推状态。状态一旦落 compensated，「外面还有一笔下落
        # 不明的请求」这件事就没人记得了 —— 与 control_plane.human_decision
        # 「先回滚再改状态」同一个理由。
        revoked = []
        for row in requests:
            detail = {
                "request_id": row["request_id"],
                "idempotency_key": row["idempotency_key"],
                "gateway": row["gateway"],
                "amount": row["amount"],
                # 这一行是整条记录的题眼：作废的是**我们这边的推进**，
                # 不是外部那笔钱的结论。unknown / unobserved 时尤其不许改写成 failed。
                "last_observed_state": last_state,
                "meaning": "MAOS 侧不再推进本请求；外部资金下落以 last_observed_state 为准，"
                           "需人工到支付渠道对账后确认",
                "reason": reason,
            }
            self._record(store, tenant_id=tenant_id, case_id=case_id,
                         kind=KIND_REQUEST_REVOKED, detail=detail,
                         executed_at=now, operator=operator)
            revoked.append(detail)

        # ---- 第二步：开人工工单 —— 本系统能做的到此为止 ---------------------
        ticket = {
            "ticket_id": f"MT-{case_id}",
            "assignee": str(payload.get("assignee") or operator),
            "case_id": case_id,
            "reason": reason,
            "last_observed_state": last_state,
            "todo": [
                "到支付渠道后台按 idempotency_key 核对这笔退款的真实下落",
                "下落为已退款则补记账；未退款则按人工流程重新发起或改单",
                "对客户回访并关闭本案",
            ],
            "requests": [r["request_id"] for r in requests],
            "opened_at": now,
        }
        self._record(store, tenant_id=tenant_id, case_id=case_id,
                     kind=KIND_MANUAL_TICKET, detail=ticket,
                     executed_at=now, operator=operator)

        # ---- 第三步：推进业务状态。guard 是唯一入口，越权写 settled 会被它拦 ----
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
                # domain 键区分本条是域内补偿还是控制面的逆补丁补偿 ——
                # 两者共用事件名，审计时按这个键分流。
                "domain": C.BIZ_TYPE,
                "tenant_id": tenant_id,
                "case_id": case_id,
                "operator": operator,
                "revoked_requests": [r["request_id"] for r in revoked],
                "last_observed_state": last_state,
                "ticket_id": ticket["ticket_id"],
                "records": len(revoked) + 1,
                "invocation_id": invocation_id,
            },
        })

        return {
            "biz_status": case["biz_status"],
            "revoked": revoked,
            "ticket": ticket,
            "records": len(revoked) + 1,
            "last_observed_state": last_state,
            "invocation_id": invocation_id,
        }

    # ------------------------------------------------------------------
    @staticmethod
    def _last_observed_state(store, tenant_id: str, case_id: str) -> str:
        """最后一次**观察到**的下落。一次都没观察到返回 UNOBSERVED。

        刻意不兜底成 "failed"：轮询到顶没问出终态时 `payment.observe` 一行观察都不写
        （它在源码里写明「还没问出来不是一个可以落库的结论」），此时表是空的 ——
        把空表读成「失败」正是本 skill 通篇在防的那个推断。
        """
        rows = objects.query(
            store,
            "SELECT observed_state FROM payment_observation WHERE tenant_id=? AND case_id=?"
            " ORDER BY observed_at DESC", (tenant_id, case_id))
        return str(rows[0]["observed_state"]) if rows else UNOBSERVED

    @staticmethod
    def _record(store, *, tenant_id: str, case_id: str, kind: str, detail: dict,
                executed_at: str, operator: str) -> None:
        objects.execute(
            store,
            "INSERT OR REPLACE INTO compensation_record (tenant_id, case_id, kind,"
            " detail_json, executed_at, operator) VALUES (?,?,?,?,?,?)",
            (tenant_id, case_id, kind,
             json.dumps(detail, ensure_ascii=False, sort_keys=True),
             executed_at, operator),
        )
