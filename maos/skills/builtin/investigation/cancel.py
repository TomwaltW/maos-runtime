"""investigation.cancel —— 向清算方发出 camt.056 撤销请求。

本 skill 写得出 `cancellation_sent`，**写不出 `returned`**（guard 会抛
`AuthoritativeFactViolation`）。这条分工是本域论证的骨架：

    发请求的人不许宣布结果。

合成一个 skill 就等于承认「我发出去了所以它成功了」—— 那正是铁律 8 禁止的推断。
分成两个之后，「凭什么说钱退回来了」这个问题在代码结构上就有答案：因为
`investigation.observe` 问到了 pacs.004，且那份观察与状态更新在同一个事务里落了库。

## 人工调账审批是硬闸，不是可选项

差错处理里的人工调账必须有人批，这是**监管要求**。所以本 skill 在发报文之前
先查 `adjustment_approval`：没有一条 `approved` 就拒绝发出。

审批记录由**人**写（Matrix 房间里的 `/approve`），本 skill 只读不写 ——
让调账方自己写下「我被批准了」，等于没有审批。

## 幂等键由 (租户, 案号) 定

一个案子只允许有一份在途的 camt.056。幂等键写死成 `(tenant, case)` 的函数而不是
每次现生成一个 uuid：重跑这一步时 uuid 会变，于是清算方收到第二份 camt.056、
开出第二个 case，两条对话各自往下走而资金只有一笔。
"""

from __future__ import annotations

from maos.domain.investigation import guard, objects
from maos.skills.contract import Skill, SkillContext, SkillContract
from maos.skills.registry import register_skill
from maos.tools.investigation import CLEARING_CANCEL_PORT, MSG_CANCELLATION_REQUEST
from maos.tools.port import invoke_tool

from . import _common as C


def idempotency_key_of(tenant_id: str, case_id: str) -> str:
    """一个案子一个指派号。**不带随机数** —— 见模块 docstring 末段。"""
    return f"ASSGN-{tenant_id}-{case_id}"


@register_skill
class InvestigationCancelSkill(Skill):
    contract = SkillContract(
        name="investigation.cancel",
        version="1.0.0",
        purpose="核对人工调账审批后向清算方发出 camt.056 撤销请求，落 cancellation_sent",
        input_schema={
            "tenant_id": "str",
            "case_id": "str",
            "clearing": "str（已 register_clearing 的名字，缺省 'demo'）",
        },
        output_schema={
            "request_id": "str（清算方受理号）",
            "idempotency_key": "str（camt.056 的 Assgnmt/Id）",
            "message_type": "camt.056.001.08",
            "receipt": "dict（受理回执，**非终态**）",
            "biz_status": "cancellation_sent",
            "reason_code": "str（报文里填的撤销原因码）",
            "invocation_id": "str",
        },
        preconditions=["tenant_id", "case_id"],
        depends_tools=["clearing.cancel"],
        failure_policy="escalate",
        max_retries=0,
        security_boundary=(
            "发出 camt.056 并写 cancellation_sent；"
            "**写不出 returned** —— 那是 investigation.observe 的权威边界，guard 会抛；"
            "发报文前必须读到一条 approved 的 adjustment_approval（人工调账的监管硬闸），"
            "本 skill 只读审批不写审批；"
            "幂等键由 (tenant, case) 定，重跑不会产生第二份 camt.056；"
            "清算方调用一律经 invoke_tool 留审计行"
        ),
        reuse_note="任何「对外发一份不可撤回的指令」的域都该照此写：先查人批、再发、"
                   "只写『发出去了』不写『成功了』",
        owner_roles=["investigation_cancel"],
    )

    def run(self, payload: dict, ctx: SkillContext) -> dict:
        store = C.ensure_schema(ctx)
        invocation_id = C.invocation_id_of(ctx)
        extras = getattr(ctx, "extras", None) or {}
        tenant_id, case_id = C.required(payload, "tenant_id", "case_id")

        case = guard.get_case(store, tenant_id, case_id)
        if case is None:
            raise LookupError(f"没有这个 case：tenant={tenant_id} case={case_id}")

        reason_code = str(case["cancellation_reason_code"] or "").strip()
        if not reason_code:
            raise ValueError(
                f"case={case_id} 还没定性（cancellation_reason_code 为空），发不出 camt.056；"
                "先跑 investigation.classify —— 空原因码的撤销请求是一份不合规报文")

        # ---- 监管硬闸：没有人批就不许发 ------------------------------------
        approvals = C.adjustment_approvals(store, tenant_id=tenant_id, case_id=case_id)
        if not approvals:
            raise PermissionError(
                f"case={case_id} 没有 approved 的人工调账审批记录，拒绝发出 camt.056。"
                "差错处理的人工调账必须有人批（监管要求），这道闸不是可选项；"
                "审批由人在房间里做出并落 adjustment_approval，本 skill 只读不写")

        clearing = C.get_clearing(payload.get("clearing"))
        key = idempotency_key_of(tenant_id, case_id)
        tool_extras = {
            "plan_id": extras.get("plan_id", ""),
            "task_id": extras.get("task_id"),
            "trace_id": extras.get("trace_id", ""),
        }

        receipt = invoke_tool(CLEARING_CANCEL_PORT, {
            "clearing": clearing,
            "original_msg_id": case["original_msg_id"],
            "end_to_end_id": case["end_to_end_id"],
            # 金额转成字符串再进报文：金额永远不进浮点（同 tools 层口径）。
            "amount": f"{float(case['amount']):.2f}",
            "currency": case["currency"],
            "reason_code": reason_code,
            "idempotency_key": key,
            "case_id": case_id,
        }, store=store, extras=tool_extras)

        # 受理回执必须是非终态。这一条不是防御性断言，是对 mock/适配器的契约校验：
        # 哪天有人把清算方换成「一步返回 CNCL」的实现，这里当场响，
        # 而不是让 observe 失去存在理由之后无人察觉。
        if receipt.get("is_terminal") or receipt.get("funds_settled"):
            raise RuntimeError(
                f"清算方在受理 camt.056 时就返回了终态（{receipt.get('message_type')}）；"
                "撤销的决议必须经 clearing.resolution 问出来 —— "
                "一步到终态会让『观察与推断分离』这条论证塌掉")

        objects.execute(
            store,
            "INSERT OR REPLACE INTO cancellation_request (tenant_id, case_id, request_id,"
            " message_type, assignment_id, reason_code, amount, currency,"
            " idempotency_key, clearing_house, sent_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (tenant_id, case_id, receipt["request_id"], MSG_CANCELLATION_REQUEST,
             key, reason_code, float(case["amount"]), case["currency"], key,
             str(payload.get("clearing") or C.DEFAULT_CLEARING), C.now_iso()),
        )

        # 只有第一次发出时推进状态；重跑（幂等返回同一份受理）时案子已经是
        # cancellation_sent，再迁一次会撞 BizStatusTransitionError。
        if case["biz_status"] != "cancellation_sent":
            case = guard.update_biz_status(
                store, tenant_id, case_id, "cancellation_sent",
                self.contract.name, invocation_id,
                reason=(f"已发出 camt.056（原因码 {reason_code}，指派号 {key}）；"
                        "决议与资金下落均未知，须经 investigation.observe 问出来"))

        return {
            "request_id": receipt["request_id"],
            "idempotency_key": key,
            "message_type": receipt["message_type"],
            "receipt": receipt,
            "biz_status": case["biz_status"],
            "reason_code": reason_code,
            "approved_by": [a["approver"] for a in approvals],
            "invocation_id": invocation_id,
        }
