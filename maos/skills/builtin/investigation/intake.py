"""investigation.file —— 受理一件差错，建 `investigation_case`，落 `filed`。

受理这一步会被返工重跑（Gate 判 rework 之后同一个任务再跑一次），而
`investigation_case` 的主键是 `(tenant_id, case_id)` —— 所以本 skill 必须是幂等的，
幂等语义由 `guard.create_case()` 定（逐字段相同才算重放，对不上就抛
`CaseIdentityConflict`）。这里不自己再写一套。

## 案子必须挂在一份**读到过的**原始支付快照上

差错处理的第一句话是「哪一笔支付出了问题」。金额、币种、收付款行都取自
`original_payment_snapshot` —— 那是 MAOS 执行前读到的那一版，不是清算系统的当前值
（铁律 8）。调用方递进来的金额**不作数**：它只用来与快照核对，对不上就抛。

反过来（信调用方、不查快照）会让一个手滑输错的金额一路走到 camt.056 上，
而 camt.056 的金额是要和原始报文对上的 —— 对不上的撤销请求会被清算方直接拒，
但那时候已经发出去了，案子也建歪了。
"""

from __future__ import annotations

from maos.domain.investigation import guard, objects
from maos.skills.contract import Skill, SkillContext, SkillContract
from maos.skills.registry import register_skill

from . import _common as C


@register_skill
class InvestigationFileSkill(Skill):
    contract = SkillContract(
        name="investigation.file",
        version="1.0.0",
        purpose="受理一件支付差错，核对原始支付快照后建 investigation_case（filed）",
        input_schema={
            "tenant_id": "str",
            "case_id": "str（与清算方对话的案号，对应 camt.056 的 Case/Id）",
            "original_msg_id": "str（被质疑那笔支付的原报文号）",
            "original_version": "int（可选，缺省取该报文最新读到的那一版）",
            "creator_agent": "str（发起方 BIC）",
            "assignee_agent": "str（被指派方 BIC）",
            "claimed_amount": "float|str（可选，递进来就与快照核对，对不上即抛）",
        },
        output_schema={
            "case": "dict（建成或既有的那一行）",
            "biz_status": "filed",
            "snapshot": "dict（案子挂着的原始支付快照）",
            "idempotent_replay": "bool（True = 案号已在库且业务字段逐字段相同）",
            "invocation_id": "str",
        },
        preconditions=["tenant_id", "case_id", "original_msg_id"],
        depends_tools=[],
        failure_policy="escalate",
        max_retries=0,
        security_boundary=(
            "只经 guard.create_case 写 investigation_case，落 filed；"
            "**写不出 returned**（那是 investigation.observe 的权威边界，guard 会抛）；"
            "金额币种一律以 original_payment_snapshot 为准，调用方递的值只用于核对"
        ),
        reuse_note="任何「案件挂在一份外部快照上」的域都该照此写：先查快照、再核对、最后建案",
        owner_roles=["investigation_intake"],
    )

    def run(self, payload: dict, ctx: SkillContext) -> dict:
        store = C.ensure_schema(ctx)
        invocation_id = C.invocation_id_of(ctx)
        extras = getattr(ctx, "extras", None) or {}
        tenant_id, case_id, original_msg_id = C.required(
            payload, "tenant_id", "case_id", "original_msg_id")

        version = payload.get("original_version")
        if version in (None, ""):
            version = objects.latest_snapshot_version(
                store, tenant_id=tenant_id, original_msg_id=original_msg_id)
        snapshot = objects.get_payment_snapshot(
            store, tenant_id=tenant_id, original_msg_id=original_msg_id,
            version=int(version))

        # 递进来的金额只用于**核对**，不用于写库。对不上当场抛：一个错的金额
        # 走到 camt.056 上会被清算方拒，而那时候报文已经发出去了。
        claimed = payload.get("claimed_amount")
        if claimed not in (None, ""):
            if abs(float(claimed) - float(snapshot["interbank_amount"])) > 1e-9:
                raise ValueError(
                    f"受理金额 {claimed} 与原始支付快照 {snapshot['interbank_amount']} "
                    f"对不上（msg={original_msg_id} v{version}）；"
                    "差错案件的金额以快照为准 —— 对不上说明质疑的不是这一笔，先查清再受理")

        existing = guard.get_case(store, tenant_id, case_id)
        case = guard.create_case(
            store,
            tenant_id=tenant_id,
            case_id=case_id,
            creator_agent=str(payload.get("creator_agent") or snapshot["debtor_agent"]),
            assignee_agent=str(payload.get("assignee_agent") or snapshot["creditor_agent"]),
            original_msg_id=original_msg_id,
            original_version=int(version),
            end_to_end_id=str(snapshot["end_to_end_id"]),
            amount=float(snapshot["interbank_amount"]),
            currency=str(snapshot["currency"]),
            plan_id=str(extras.get("plan_id") or ""),
            actor_skill=self.contract.name,
            invocation_id=invocation_id,
        )

        return {
            "case": dict(case),
            "biz_status": case["biz_status"],
            "snapshot": dict(snapshot),
            # 幂等重放要能被上层看见：返工重跑时产物应当说「这是重放」，
            # 而不是让人以为又新建了一个案子。
            "idempotent_replay": existing is not None,
            "invocation_id": invocation_id,
        }
