"""claim.compensate —— 赔付走不通之后的**域内**补偿收口。

## 为什么理赔域要有自己的补偿，而不是复用控制面那个

`ControlPlane._execute_compensation` 是**逆补丁补偿**：读 `compensation` artifact 的
`patch_ref`，把正向 `patch_set` 在沙箱里反着打一遍。它的前提是「产物是代码」。
理赔域的产物不是补丁 —— 落地的是一条**发给外部赔付方的指令**，没有可以 `git apply -R`
的东西。所以 `human_decision(approved=False)` 走到 `_execute_compensation` 时会拿到
「无补偿引用，跳过回滚」并返回 None，那是**正确行为，不是缺陷**：代码域没有东西要还原。

要还原的是业务侧的账：这条赔付指令作废、留一条补偿记录、开一张人工工单。
那三件事只有本域知道怎么做，所以落在本 skill 里。

## 本 skill 最要紧的一条：**不许宣布那笔钱没赔出去**

走到补偿的两条典型路径，都不允许在这里下结论：

  · **轮询到顶仍问不出终态** —— `claim.observe` 一行观察都不写，表是空的。
    把空表读成「拒付」正是本 skill 通篇在防的那个推断。
  · **赔付方明确拒付（带 CARC）** —— 这一条**有**结论，而且结论要原样带下来：
    `last_carc` 记下是哪一条码、`recourse` 记下码表判出的下一步。人工工单据此
    知道该补件重报、该改送别家、还是只能申诉。

所以本 skill 落的 `claim_payment_revoked` 记录，语义严格限定为

    「MAOS 侧不再推进这条赔付指令，并把它连同最后一次观察到的下落交给人」

而**不是**「这笔钱确认没赔」。最后一次观察到的下落原样抄进 `detail_json`
（`last_observed_state`，没观察到就是 `unobserved`），人工工单据此去赔付方对账。
这也是为什么补偿必须配一张工单：本系统能做的到此为止，剩下的只有人能做。

## 与 `claim.observe` 的分工

`claim.observe` 见到赔付方明确拒付时只落一条观察行，**不推进业务状态** ——
它在源码里写明了理由。本 skill 就是它让出来的那一步：补偿真做完了，才把案子推到
`compensated`。

`paid` 的案子一律拒绝补偿：钱已经确认赔出去了，再走补偿是数据被改坏的信号，
不是一次可以静默吞掉的空操作。
"""

from __future__ import annotations

import json

from maos.domain.claim import guard, objects
from maos.skills.contract import Skill, SkillContext, SkillContract
from maos.skills.registry import register_skill

from . import _common as C
from .observe import UNOBSERVED

#: 补偿记录的两种 kind。测试与场景按名取，不在各处抄字面量。
KIND_PAYMENT_REVOKED = "claim_payment_revoked"
KIND_MANUAL_TICKET = "manual_ticket"

#: 事件类型与控制面的逆补丁补偿共用一个名字：对审计与 Trace 来说，
#: 「补偿执行过了」是同一件事，按 `detail.domain` 区分是哪一种。
#: 另起一个名字会让「这个 Plan 到底补偿过没有」要查两处，漏一处就是假绿。
EVENT_COMPENSATION_EXECUTED = "CompensationExecuted"


@register_skill
class ClaimCompensateSkill(Skill):
    contract = SkillContract(
        name="claim.compensate",
        version="1.0.0",
        purpose="赔付被拒或走不通后的域内补偿收口：作废赔付指令、写补偿记录与人工工单，"
                "把案子推进到 compensated",
        input_schema={
            "tenant_id": "str",
            "claim_id": "str",
            "operator": "str（做出驳回/收口决定的人）",
            "reason": "str（为什么走补偿，原样进补偿记录与事件）",
            "assignee": "str（可选，人工工单的接单人，缺省同 operator）",
        },
        output_schema={
            "biz_status": "compensated",
            "revoked": "list[dict]（每条作废的赔付指令及其最后观察到的下落）",
            "ticket": "dict（人工工单：单号、接单人、要人去做什么）",
            "records": "int（落进 claim_compensation 的行数）",
            "last_observed_state": "str（paid|denied|processing|unknown|unobserved）",
            "last_carc": "str（最后一次观察到的 CARC，没观察到就是空）",
            "invocation_id": "str",
        },
        preconditions=["tenant_id", "claim_id", "operator", "reason"],
        depends_tools=[],
        # 不重试：补偿是写账动作，重试一次就多一条记录。失败要人看见，不要自己再来一遍。
        failure_policy="escalate",
        max_retries=0,
        security_boundary=(
            "写 claim_compensation 与 biz_status(compensated)；"
            "**不写 paid**（那是 claim.observe 的权威边界，guard 会抛）；"
            "**不宣布外部资金结果** —— 作废记录只表示 MAOS 侧不再推进，"
            "最后一次观察到的下落原样留档交人工对账；"
            "已 paid 的案子拒绝补偿，不静默跳过"
        ),
        reuse_note=(
            "任何「外部已经收到指令、但本地要收口」的域都该照此写：先留档最后一次观察、"
            "再开人工工单、最后才推进本地状态；三步顺序不可换"
        ),
        owner_roles=["claim_payment"],
    )

    def run(self, payload: dict, ctx: SkillContext) -> dict:
        store = C.ensure_schema(ctx)
        invocation_id = C.invocation_id_of(ctx)
        extras = getattr(ctx, "extras", None) or {}
        tenant_id, claim_id, operator, reason = C.required(
            payload, "tenant_id", "claim_id", "operator", "reason")

        case = guard.get_case(store, tenant_id, claim_id)
        if case is None:
            raise LookupError(f"没有这个 case：tenant={tenant_id} claim={claim_id}")

        # 已确认赔付成功的案子不许补偿。这不是防御性编程，是数据一致性的报警器：
        # 走到这里说明有人拿一笔已经到账的赔款去做撤销，静默跳过会把错误埋掉。
        if case["biz_status"] == "paid":
            raise ValueError(
                f"claim={claim_id} 已经是 paid（赔款确认到账），不许补偿；"
                "补偿是给走不通的案子收口用的，对已到账的案子撤销等于凭空抹掉一笔"
                "已发生的资金动作")

        now = C.now_iso()
        last_state, last_carc, last_recourse = self._last_observation(
            store, tenant_id, claim_id)
        requests = objects.query(
            store,
            "SELECT * FROM claim_payment_request WHERE tenant_id=? AND claim_id=?"
            " ORDER BY submitted_at", (tenant_id, claim_id))

        # ---- 第一步：把每一条赔付指令连同**最后观察到的下落**留档 ------------
        # 顺序不可换：先留档再推状态。状态一旦落 compensated，「外面还有一条下落
        # 不明的指令」这件事就没人记得了 —— 与 control_plane.human_decision
        # 「先回滚再改状态」同一个理由。
        revoked = []
        for row in requests:
            detail = {
                "request_id": row["request_id"],
                "idempotency_key": row["idempotency_key"],
                "payer_id": row["payer_id"],
                "amount": row["amount"],
                # 这三行是整条记录的题眼：作废的是**我们这边的推进**，
                # 不是外部那笔钱的结论。unknown / unobserved 时尤其不许改写成 denied。
                "last_observed_state": last_state,
                "last_carc": last_carc,
                "recourse": last_recourse,
                "meaning": "MAOS 侧不再推进本条赔付指令；外部资金下落以 "
                           "last_observed_state 为准，需人工到赔付方对账后确认",
                "reason": reason,
            }
            self._record(store, tenant_id=tenant_id, claim_id=claim_id,
                         kind=KIND_PAYMENT_REVOKED, detail=detail,
                         executed_at=now, operator=operator)
            revoked.append(detail)

        # ---- 第二步：开人工工单 —— 本系统能做的到此为止 ---------------------
        ticket = {
            "ticket_id": f"CT-{claim_id}",
            "assignee": str(payload.get("assignee") or operator),
            "claim_id": claim_id,
            "reason": reason,
            "last_observed_state": last_state,
            "last_carc": last_carc,
            "recourse": last_recourse,
            "todo": self._todo(last_state, last_carc, last_recourse),
            "requests": [r["request_id"] for r in requests],
            "opened_at": now,
        }
        self._record(store, tenant_id=tenant_id, claim_id=claim_id,
                     kind=KIND_MANUAL_TICKET, detail=ticket,
                     executed_at=now, operator=operator)

        # ---- 第三步：推进业务状态。guard 是唯一入口，越权写 paid 会被它拦 ----
        case = guard.update_biz_status(
            store, tenant_id, claim_id, "compensated",
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
                "claim_id": claim_id,
                "operator": operator,
                "revoked_requests": [r["request_id"] for r in revoked],
                "last_observed_state": last_state,
                "last_carc": last_carc,
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
            "last_carc": last_carc,
            "recourse": last_recourse,
            "invocation_id": invocation_id,
        }

    # ------------------------------------------------------------------
    @staticmethod
    def _last_observation(store, tenant_id: str, claim_id: str) -> tuple[str, str, str]:
        """**收口这一条**最后一次观察到的下落、CARC 与处置口径。

        按当前那一条 `claim_payment_request` 的 `request_id` 收窄，不按
        `(tenant, claim)` 查全表：一个案子先后可能有两条指令，按全表取最新会把
        **上一条的下落**当成这一条的 —— 而这种错没有症状：补偿记录、人工工单、事件
        全都正常，只有「外面那笔钱到底怎么样了」这一格填错，偏偏那是人工对账唯一
        的依据。

        刻意不兜底成 "denied"：轮询到顶没问出终态时 `claim.observe` 一行观察都不写
        （它在源码里写明「还没问出来不是一个可以落库的结论」），此时表是空的 ——
        把空表读成「拒付」正是本 skill 通篇在防的那个推断。查不到当前指令时同理，
        返回 UNOBSERVED 而不是回退去读别的指令的观察。

        `recourse` 从码表现查，不从观察行里读：观察行落的是回执原文，
        而处置口径是码表的判断 —— 码表哪天改了，这里要跟着改，观察行不该跟着变。
        """
        current = objects.query(
            store,
            "SELECT request_id FROM claim_payment_request WHERE tenant_id=? AND claim_id=?"
            " ORDER BY submitted_at DESC", (tenant_id, claim_id))
        if not current:
            return UNOBSERVED, "", ""
        rows = objects.query(
            store,
            "SELECT observed_state, carc_code FROM claim_payment_observation"
            " WHERE tenant_id=? AND claim_id=? AND request_id=?"
            " ORDER BY observed_at DESC",
            (tenant_id, claim_id, current[0]["request_id"]))
        if not rows:
            return UNOBSERVED, "", ""
        state = str(rows[0]["observed_state"])
        carc = str(rows[0]["carc_code"] or "")
        recourse = ""
        if carc:
            # 未知码不许在这里兜底成某种处置 —— 码表的规矩是抛，这里照办，
            # 只是把「查不到」如实留白，不冒充一个已核对过的判据。
            from maos.tools import claim_codes
            try:
                recourse = claim_codes.recourse_of(carc)
            except KeyError:
                recourse = ""
        return state, carc, recourse

    @staticmethod
    def _todo(last_state: str, last_carc: str, recourse: str) -> list[str]:
        """人工工单要人去做什么。**按码表的 recourse 分档，不在这里另写一套映射。**

        另写一套的后果是：码表加一条码或改一个 recourse，只有码表跟着变，这几句话
        会悄悄开始说错，而且没有症状（工单照样开得出来，只是写错了下一步）。
        """
        from maos.tools import claim_codes

        head = [f"到赔付方后台按 idempotency_key 核对这笔赔款的真实下落"
                f"（MAOS 最后观察到：{last_state}）"]
        if not last_carc:
            return head + [
                "下落为已赔付则补记账；未赔付则按人工流程重新发起或改单",
                "对被保险人回访并关闭本案",
            ]
        head.append(f"赔付方给出的调整码：CARC {last_carc}")
        if recourse == claim_codes.RECOURSE_RESUBMIT:
            tail = ["补齐缺失的信息或单据后重新申报（该码属可补件重报一类）"]
        elif recourse == claim_codes.RECOURSE_OTHER_PAYER:
            tail = ["确认正确的赔付方后改送 —— 这一步要人先定下送给谁，机器不许自行改投"]
        elif recourse == claim_codes.RECOURSE_HUMAN:
            tail = ["按赔付方申诉/补授权流程与对方沟通，机器重报无意义"]
        else:
            tail = ["该码属终态拒赔，重报无意义；按拒赔口径向被保险人解释并结案"]
        return head + tail + ["对被保险人回访并关闭本案"]

    @staticmethod
    def _record(store, *, tenant_id: str, claim_id: str, kind: str, detail: dict,
                executed_at: str, operator: str) -> None:
        objects.execute(
            store,
            "INSERT OR REPLACE INTO claim_compensation (tenant_id, claim_id, kind,"
            " detail_json, executed_at, operator) VALUES (?,?,?,?,?,?)",
            (tenant_id, claim_id, kind,
             json.dumps(detail, ensure_ascii=False, sort_keys=True),
             executed_at, operator),
        )
