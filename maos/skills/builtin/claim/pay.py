"""claim.pay —— 发起赔付指令，把案子推到 payment_requested。

**这个 skill 永远写不出 paid。** 不是靠自觉，是靠 `guard.update_biz_status()`：
它一见 `new_status == "paid"` 且 `actor_skill != "claim.observe"`，就落一条
`AuthoritativeFactViolation` 事件并抛异常。那是**预期行为，不是 bug** ——
如果哪天这里能写 paid 了，说明守卫被改坏了，不是这里该改。

为什么发起方不能宣布到账：`submit()` 的返回值只说明「赔付指令被受理了」。
一个不抛异常的调用**不等于**赔款到了被保险人账上 —— 那是外部系统的权威事实，
只能问出来（`claim.observe`），不能推断出来（铁律 8）。

发起前先核对审批记录：`claim_approval` 由**人**在 CLI 或 Matrix 房间落下，
本 skill 只读不写。让付款方自己写下「我被批准了」，等于没有审批。
"""

from __future__ import annotations

import json

from maos.domain.claim import guard, objects
from maos.skills.contract import Skill, SkillContext, SkillContract
from maos.skills.registry import register_skill
from maos.tools.claim import PAYER_SUBMIT_PORT, STATUS_PAID, receipt_json
from maos.tools.port import invoke_tool

from . import _common as C


def idempotency_key_of(tenant_id: str, claim_id: str) -> str:
    """一个案子只允许有一笔在途赔付。幂等键由 (tenant, claim) 唯一确定 ——
    重跑、重试、重复投递都撞同一个键，不会产生第二笔。"""
    return f"clm-{tenant_id}-{claim_id}"


@register_skill
class ClaimPaySkill(Skill):
    contract = SkillContract(
        name="claim.pay",
        version="1.0.0",
        purpose="核对审批后向赔付方发起赔付指令，写 claim_payment_request 并推进到 payment_requested",
        input_schema={
            "tenant_id": "str",
            "claim_id": "str",
            "payer": "str（已 register_payer 的名字，默认 'demo'）",
            "payee": "str（可选，收款方；进幂等比对面）",
        },
        output_schema={
            "payer_receipt": "dict（赔付方回执，**非 paid**）",
            "request_id": "str（claim.observe 用它去 query）",
            "idempotency_key": "str",
            "amount": "str",
            "biz_status": "payment_requested —— **永远不是 paid**",
            "needs_query": "bool（恒 True：到账只能问出来）",
            "invocation_id": "str",
        },
        preconditions=["tenant_id", "claim_id"],
        depends_tools=["payer.submit"],
        # 不重试：赔付指令的重试语义由幂等键与赔付方侧承担，在这里重试只会
        # 掩盖「上一次到底发出去没有」这个必须查清楚的问题。
        failure_policy="escalate",
        max_retries=0,
        security_boundary=(
            "写 claim_payment_request 与 biz_status(payment_requested)；"
            "**无权写 paid** —— guard 会抛 AuthoritativeFactViolation；"
            "发起前必须存在 approved 的 claim_approval，本 skill 只读不写审批记录；"
            "赔付方调用一律经 invoke_tool，留 ToolInvoked 审计行；"
            "回执挂在 payer_receipt 键上，不占用 receipt（那个键归第七道闸的支付宝码表）"
        ),
        reuse_note="发起与观察分离：本 skill 只产生指令，到账一律由 claim.observe 观察得到",
        owner_roles=["claim_payment"],
    )

    def run(self, payload: dict, ctx: SkillContext) -> dict:
        store = C.ensure_schema(ctx)
        invocation_id = C.invocation_id_of(ctx)
        extras = getattr(ctx, "extras", None) or {}
        tenant_id, claim_id = C.required(payload, "tenant_id", "claim_id")

        case = guard.get_case(store, tenant_id, claim_id)
        if case is None:
            raise LookupError(f"没有这个 case：tenant={tenant_id} claim={claim_id}")

        approvals = C.approvals_of(store, tenant_id=tenant_id, claim_id=claim_id)
        if not approvals:
            raise PermissionError(
                f"claim={claim_id} 没有 approved 的审批记录，不许发起赔付；"
                "审批是人的动作，付款方不得自行补记")

        adjs = objects.query(
            store,
            "SELECT * FROM adjudication WHERE tenant_id=? AND claim_id=?"
            " AND decision='approve' ORDER BY adjudicated_at DESC",
            (tenant_id, claim_id))
        if not adjs:
            raise LookupError(
                f"claim={claim_id} 没有 approve 的 adjudication，责任未经裁定，不许发起赔付")
        adj = adjs[0]
        allowed = float(adj["allowed_amount"] or 0.0)
        if allowed <= 0:
            raise ValueError(
                f"claim={claim_id} 的 adjudication.allowed_amount={allowed}，金额未经核算，"
                "不许发起赔付 —— 0 元赔付指令发出去只会在对账时变成一条查不清的记录")
        # 金额转成字符串再交给赔付方：PaymentInstruction.amount 是 str，
        # 「金额永远不进浮点」这条口径照做。
        amount = f"{allowed:.2f}"

        if case["biz_status"] not in ("adjudicated", "payment_requested"):
            raise ValueError(
                f"claim={claim_id} 当前 biz_status={case['biz_status']}，"
                "不在可发起赔付的状态上")

        # ---- 发起赔付：一律经 invoke_tool，直接调没有 ToolInvoked 审计行 --------
        key = idempotency_key_of(tenant_id, claim_id)
        payer = C.get_payer(payload.get("payer"))
        receipt = invoke_tool(PAYER_SUBMIT_PORT, {
            "payer": payer,
            "claim_ref": claim_id,
            "amount": amount,
            "idempotency_key": key,
            "payee": str(payload.get("payee") or case["payer_id"]),
            "memo": case.get("loss_type", ""),
        }, store=store, extras=C.tool_extras(ctx))

        # 断言而不是注释：MockPayer 保证 submit() 永不返回 paid，这里把它变成守卫。
        # 哪天赔付方实现被换成「一步到 paid」的桩，这条会当场炸，而不是让
        # claim.observe 悄悄失去存在理由。
        if receipt.get("status") == STATUS_PAID:
            raise AssertionError(
                "赔付方在 submit() 里直接返回了 paid —— "
                "这会让「观察与推断分离」失去落点，检查赔付方实现")

        objects.execute(
            store,
            "INSERT OR REPLACE INTO claim_payment_request (tenant_id, claim_id, request_id,"
            " amount, payer_id, idempotency_key, submitted_at) VALUES (?,?,?,?,?,?,?)",
            (tenant_id, claim_id, receipt["request_id"], allowed,
             str(payload.get("payer") or C.DEFAULT_PAYER), key, C.now_iso()))

        plan_id = str(extras.get("plan_id") or "")
        task_id = str(extras.get("task_id") or "")
        if plan_id and task_id:
            # 重发时先摘掉上一笔的引用。`claim_payment_request` 上有
            # `UNIQUE (tenant_id, idempotency_key)` —— 「一个案子只允许有一笔在途赔付」
            # 的落点 —— 所以同一个案子重发会把上一行**挤掉**，指向旧 request_id 的
            # 引用当场悬空。而引用的规矩是「只存引用，读的时候一定读到当前那一份」，
            # 指着一个已经不在库里的对象正是它要防的那件事。
            objects.execute(
                store,
                "DELETE FROM claim_business_ref WHERE plan_id=? AND task_id=? AND tenant_id=?"
                " AND object_type='claim_payment_request' AND object_id<>?",
                (plan_id, task_id, tenant_id, receipt["request_id"]))
            objects.attach_business_ref(
                store, plan_id=plan_id, task_id=task_id, tenant_id=tenant_id,
                object_type="claim_payment_request", object_id=receipt["request_id"],
                purpose="向赔付方发起的赔付指令")

        # ---- 状态推进：受理，**到此为止** -------------------------------------
        # 无论回执是 processing / unknown / denied 都推到 payment_requested：
        # 这个状态描述的是「MAOS 这边已经把指令发出去了」，是本地事实，不是外部事实。
        # 拒付回执**不**在这里收口成 rejected —— 那要走补偿，是失败路径的事。
        if case["biz_status"] == "adjudicated":
            case = guard.update_biz_status(
                store, tenant_id, claim_id, "payment_requested",
                self.contract.name, invocation_id,
                reason=f"赔付方受理 request_id={receipt['request_id']} "
                       f"status={receipt['status']}")

        return {
            "payer_receipt": receipt,
            "request_id": receipt["request_id"],
            "idempotency_key": key,
            "amount": amount,
            "biz_status": case["biz_status"],
            "needs_query": True,
            "rule_refs": json.loads(adj.get("rule_refs") or "[]"),
            "terms_version": int(adj["terms_version"]),
            "primary_rule": adj["rule_no"],
            "raw_receipt_json": receipt_json(receipt),
            "invocation_id": invocation_id,
        }
