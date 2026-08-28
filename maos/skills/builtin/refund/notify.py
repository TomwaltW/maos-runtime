"""notify.customer —— 把处理结果告知客户，记 `notification`，等回执确认。

**ack 缺失不阻塞。** 客户看没看那条短信，不在 MAOS 的控制范围内，也不是退款是否
完成的判据 —— 钱已经退了，客户三天没点开通知，不该让整个 Plan 卡在这里。
所以缺 ack 的处置是记一条 `needs_followup` 继续走，而不是 blocked。

反过来也要守住：**不许把「发出去了」记成「客户确认了」**。`ack_at` 为空就是为空，
不拿 `sent_at` 顶替 —— 顶替之后，「有多少客户其实没收到」这个数字就永远查不出来了。
"""

from __future__ import annotations

from maos.domain.refund import guard, objects
from maos.skills.contract import Skill, SkillContext, SkillContract
from maos.skills.registry import register_skill

from . import _common as C

DEFAULT_CHANNEL = "sms"


@register_skill
class NotifyCustomerSkill(Skill):
    contract = SkillContract(
        name="notify.customer",
        version="1.0.0",
        purpose="通知客户退款处理结果，记 notification；ack 缺失记 needs_followup 但不阻塞",
        input_schema={
            "tenant_id": "str",
            "case_id": "str",
            "content": "str（通知正文；缺省按案子状态生成）",
            "channel": "str（默认 sms）",
            "ack": "bool|str（可选：客户回执时间或 True）",
        },
        output_schema={
            "notification": "dict{tenant_id,case_id,channel,content_digest,sent_at,ack_at}",
            "acked": "bool",
            "needs_followup": "bool（ack 缺失即 True，但不阻塞 Plan）",
            "invocation_id": "str",
        },
        preconditions=["tenant_id", "case_id"],
        depends_tools=[],
        # 通知失败是可重试的典型形态（对端瞬时不可用），且重发同一份内容不会
        # 产生第二条记录 —— content_digest 进了主键。
        failure_policy="retry",
        max_retries=2,
        security_boundary=(
            "只写 notification；不改 biz_status、不调模型、不碰支付网关；"
            "正文只含案子编号与金额结论，不带证据原文与任何凭证"
        ),
        reuse_note="任何「通知了但对端未确认」的场景都可照此写：记 needs_followup，不阻塞主流程",
        owner_roles=["refund_intake"],
    )

    def run(self, payload: dict, ctx: SkillContext) -> dict:
        store = C.ensure_schema(ctx)
        invocation_id = C.invocation_id_of(ctx)
        tenant_id, case_id = C.required(payload, "tenant_id", "case_id")

        case = guard.get_case(store, tenant_id, case_id)
        if case is None:
            raise LookupError(f"没有这个 case：tenant={tenant_id} case={case_id}")

        channel = str(payload.get("channel") or DEFAULT_CHANNEL)
        content = str(payload.get("content") or "").strip() or self._default_content(case)

        ack = payload.get("ack")
        ack_at = None
        if ack:
            ack_at = ack if isinstance(ack, str) and ack.strip() else C.now_iso()

        row = {
            "tenant_id": tenant_id,
            "case_id": case_id,
            "channel": channel,
            "content_digest": C.digest(content),
            "sent_at": C.now_iso(),
            "ack_at": ack_at,
        }
        objects.execute(
            store,
            "INSERT OR REPLACE INTO notification (tenant_id, case_id, channel,"
            " content_digest, sent_at, ack_at) VALUES (?,?,?,?,?,?)",
            (row["tenant_id"], row["case_id"], row["channel"], row["content_digest"],
             row["sent_at"], row["ack_at"]),
        )

        return {
            "notification": row,
            "content": content,
            "acked": ack_at is not None,
            "needs_followup": ack_at is None,
            "invocation_id": invocation_id,
        }

    # ------------------------------------------------------------------
    @staticmethod
    def _default_content(case: dict) -> str:
        """正文按案子当前状态生成 —— 状态是什么就说什么，不预告还没发生的事。"""
        status = case["biz_status"]
        said = {
            "submitted": "已受理，正在核定",
            "approved": "已通过审核，等待付款",
            "gateway_accepted": "退款已提交至支付渠道",
            "processing": "退款处理中，请留意到账通知",
            "settled": "退款已到账",
            "rejected": "经核定不符合退款政策",
            "compensated": "退款未能完成，已为您做冲正处理",
        }.get(status, status)
        return f"您的退款申请（{case['case_id']}）{said}。如有疑问请回复本条消息。"
