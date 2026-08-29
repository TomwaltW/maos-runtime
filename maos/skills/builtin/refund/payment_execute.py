"""payment.execute —— 发起退款，把案子推到 gateway_accepted / processing。

**这个 skill 永远写不出 settled。** 不是靠自觉，是靠 `guard.update_biz_status()`：
它一见 `new_status == "settled"` 且 `actor_skill != "payment.observe"`，就落一条
`AuthoritativeFactViolation` 事件并抛异常。那是**预期行为，不是 bug** ——
如果哪天这里能写 settled 了，说明守卫被改坏了，不是这里该改。

为什么发起方不能宣布成功：`refund()` 的返回值只说明「请求被受理了」。
一个不抛异常的调用**不等于**钱退到了客户账上 —— 那是外部系统的权威事实，
只能问出来（`payment.observe`），不能推断出来（铁律 8）。

付款前先核对审批记录：`approval_record` 由**人**在 CLI（本轮）或 Matrix 房间（P4）
落下，本 skill 只读不写。让付款方自己写下「我被批准了」，等于没有审批。
"""

from __future__ import annotations

import json

from maos.domain.refund import guard, objects
from maos.skills.contract import Skill, SkillContext, SkillContract
from maos.skills.registry import register_skill
from maos.tools.gateway import GATEWAY_REFUND_PORT
from maos.tools.port import invoke_tool

from . import _common as C

#: 一个案子只允许有一笔退款。幂等键对应支付宝的 out_request_no，
#: 由 (tenant, case) 唯一确定 —— 重跑、重试、重复投递都撞同一个键，不会产生第二笔。
def idempotency_key_of(tenant_id: str, case_id: str) -> str:
    return f"rfd-{tenant_id}-{case_id}"


@register_skill
class PaymentExecuteSkill(Skill):
    contract = SkillContract(
        name="payment.execute",
        version="1.0.0",
        purpose="核对审批后向支付网关发起退款，写 refund_request 并推进到 gateway_accepted/processing",
        input_schema={
            "tenant_id": "str",
            "case_id": "str",
            "gateway": "str（已 register_gateway 的名字，默认 'demo'）",
        },
        output_schema={
            "receipt": "dict（网关回执，非终态）",
            "request_id": "str（payment.observe 用它去 query）",
            "idempotency_key": "str",
            "biz_status": "gateway_accepted|processing —— **永远不是 settled**",
            "needs_query": "bool（恒 True：终态只能问出来）",
            "invocation_id": "str",
        },
        preconditions=["tenant_id", "case_id"],
        depends_tools=["gateway.refund"],
        # 不重试：退款请求的重试语义由幂等键与网关侧承担，在这里重试只会
        # 掩盖「上一次到底发出去没有」这个必须查清楚的问题。
        failure_policy="escalate",
        max_retries=0,
        security_boundary=(
            "写 refund_request 与 biz_status(approved/gateway_accepted/processing)；"
            "**无权写 settled** —— guard 会抛 AuthoritativeFactViolation；"
            "付款前必须存在 approved 的 approval_record，本 skill 只读不写审批记录；"
            "网关调用一律经 invoke_tool，留 ToolInvoked 审计行"
        ),
        reuse_note="发起与观察分离：本 skill 只产生请求，终态一律由 payment.observe 观察得到",
        owner_roles=["refund_payment"],
    )

    def run(self, payload: dict, ctx: SkillContext) -> dict:
        store = C.ensure_schema(ctx)
        invocation_id = C.invocation_id_of(ctx)
        extras = getattr(ctx, "extras", None) or {}
        tenant_id, case_id = C.required(payload, "tenant_id", "case_id")

        case = guard.get_case(store, tenant_id, case_id)
        if case is None:
            raise LookupError(f"没有这个 case：tenant={tenant_id} case={case_id}")

        approvals = C.approvals_of(store, tenant_id=tenant_id, case_id=case_id)
        if not approvals:
            raise PermissionError(
                f"case={case_id} 没有 approved 的审批记录，不许发起付款；"
                "审批是人的动作，付款方不得自行补记")

        entries = objects.query(
            store, "SELECT * FROM finance_entry WHERE tenant_id=? AND case_id=?",
            (tenant_id, case_id))
        if not entries:
            raise LookupError(
                f"case={case_id} 没有 finance_entry，金额未经核算，不许发起付款")
        entry = entries[0]
        # 金额转成字符串再交给网关：RefundRequest.refund_amount 是 str，
        # 「金额永远不进浮点」这条口径由 R-3 定，这里照做。
        amount = f"{float(entry['amount_approved']):.2f}"

        # ---- 审批结果落到业务对象上（submitted -> approved）--------------------
        if case["biz_status"] == "submitted":
            approver = approvals[-1]["approver"]
            case = guard.update_biz_status(
                store, tenant_id, case_id, "approved",
                self.contract.name, invocation_id,
                reason=f"主管 {approver} 审批通过，金额 {amount}")

        if case["biz_status"] not in ("approved", "gateway_accepted", "processing"):
            raise ValueError(
                f"case={case_id} 当前 biz_status={case['biz_status']}，不在可发起付款的状态上")

        # ---- 发起退款：一律经 invoke_tool，直接调没有 ToolInvoked 审计行 --------
        key = idempotency_key_of(tenant_id, case_id)
        gateway = C.get_gateway(payload.get("gateway"))
        receipt = invoke_tool(GATEWAY_REFUND_PORT, {
            "gateway": gateway,
            "out_trade_no": case["order_id"],
            "refund_amount": amount,
            "idempotency_key": key,
            "reason": case.get("reason_code", ""),
        }, store=store, extras={
            "plan_id": extras.get("plan_id", ""),
            "task_id": extras.get("task_id"),
            "trace_id": extras.get("trace_id", ""),
        })

        # 断言而不是注释：R-3 保证 refund() 永不返回终态，这里把它变成本轨的守卫。
        # 哪天网关实现被换成「一步到 settled」的桩，这条会当场炸，而不是让
        # payment.observe 悄悄失去存在理由。
        if receipt.get("is_terminal") and receipt.get("status") == "settled":
            raise AssertionError(
                "网关在 refund() 里直接返回了终态 settled —— "
                "这会让「观察与推断分离」失去落点，检查网关实现")

        objects.execute(
            store,
            "INSERT OR REPLACE INTO refund_request (tenant_id, case_id, request_id, amount,"
            " gateway, idempotency_key, submitted_at) VALUES (?,?,?,?,?,?,?)",
            (tenant_id, case_id, receipt["request_id"], float(entry["amount_approved"]),
             str(payload.get("gateway") or C.DEFAULT_GATEWAY), key, C.now_iso()),
        )

        plan_id = str(extras.get("plan_id") or "")
        task_id = str(extras.get("task_id") or "")
        if plan_id and task_id:
            # 换渠道重发时先摘掉上一笔的引用。`refund_request` 上有
            # `UNIQUE (tenant_id, idempotency_key)` —— 「一个案子只允许有一笔退款」
            # 的落点 —— 所以同一个案子重发会把上一行**挤掉**，指向旧 request_id 的
            # 引用当场悬空。而 `attach_business_ref` 的规矩写得很清楚：只存引用，
            # 「读的时候一定读到当前那一份」；指着一个已经不在库里的对象，正是它
            # 要防的那件事。不删的症状是**静默的**：付款照跑、补偿照落，只有
            # `scripts/verify.py` 的 business-ref 那一项会数出一条悬空引用。
            objects.execute(
                store,
                "DELETE FROM business_ref WHERE plan_id=? AND task_id=? AND tenant_id=?"
                " AND object_type='refund_request' AND object_id<>?",
                (plan_id, task_id, tenant_id, receipt["request_id"]))
            objects.attach_business_ref(
                store, plan_id=plan_id, task_id=task_id, tenant_id=tenant_id,
                object_type="refund_request", object_id=receipt["request_id"],
                purpose="向网关发起的退款请求")

        # ---- 状态推进：受理 -> 处理中，**到此为止** ---------------------------
        if case["biz_status"] == "approved":
            case = guard.update_biz_status(
                store, tenant_id, case_id, "gateway_accepted",
                self.contract.name, invocation_id,
                reason=f"网关受理 request_id={receipt['request_id']} code={receipt['code']}")

        if receipt["status"] == "processing" and case["biz_status"] == "gateway_accepted":
            case = guard.update_biz_status(
                store, tenant_id, case_id, "processing",
                self.contract.name, invocation_id,
                reason=f"网关回执 processing（{receipt['message']}）")
        # status == "unknown" 时**停在 gateway_accepted**：网关自己都说不清结果，
        # 本地不许乐观推进成 processing。下落由 payment.observe 问出来。

        return {
            "receipt": receipt,
            "request_id": receipt["request_id"],
            "idempotency_key": key,
            "amount": amount,
            "biz_status": case["biz_status"],
            "needs_query": True,
            "finance_entry_ref": {"tenant_id": tenant_id, "case_id": case_id},
            "rule_refs": json.loads(entry.get("rule_refs") or "[]"),
            "invocation_id": invocation_id,
        }
