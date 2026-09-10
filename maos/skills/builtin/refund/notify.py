"""notify.customer —— 把处理结果告知客户，记 `notification`，等回执确认。

**ack 缺失不阻塞。** 客户看没看那条短信，不在 MAOS 的控制范围内，也不是退款是否
完成的判据 —— 钱已经退了，客户三天没点开通知，不该让整个 Plan 卡在这里。
所以缺 ack 的处置是记一条 `needs_followup` 继续走，而不是 blocked。

反过来也要守住：**不许把「发出去了」记成「客户确认了」**。`ack_at` 为空就是为空，
不拿 `sent_at` 顶替 —— 顶替之后，「有多少客户其实没收到」这个数字就永远查不出来了。
"""

from __future__ import annotations

from maos.domain.refund import case_pack, guard, objects, projection
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
            "notification": "dict{tenant_id,case_id,channel,content_digest,sent_at,"
                            "ack_at,revision}",
            "public_status": "str（对外三态投影，跨轨契约 §D 的五个字面值之一；"
                             "此刻没有可对外说的三态时为空串，正文回落内部措辞）",
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
        public = self._public_status(store, tenant_id, case_id, case)
        content = (str(payload.get("content") or "").strip()
                   or self._default_content(case, public))

        ack = payload.get("ack")
        ack_at = None
        if ack:
            ack_at = ack if isinstance(ack, str) and ack.strip() else C.now_iso()

        # `revision` 是 T116 加的列：重发一次通知就是一次新的修订。
        # 主键里带着 `content_digest`，所以改了措辞的重发会新起一行、同措辞的
        # 重发会 REPLACE 掉原行 —— 两种情况下修订号都往上走，「这个案子对客户
        # 说过几次话」因此查得到。
        revision = case_pack.next_version(store, "notification",
                                          tenant_id=tenant_id, case_id=case_id)
        row = {
            "tenant_id": tenant_id,
            "case_id": case_id,
            "channel": channel,
            "content_digest": C.digest(content),
            "sent_at": C.now_iso(),
            "ack_at": ack_at,
            "revision": revision,
        }
        objects.execute(
            store,
            "INSERT OR REPLACE INTO notification (tenant_id, case_id, channel,"
            " content_digest, sent_at, ack_at, revision) VALUES (?,?,?,?,?,?,?)",
            (row["tenant_id"], row["case_id"], row["channel"], row["content_digest"],
             row["sent_at"], row["ack_at"], row["revision"]),
        )

        # ---- DAG -> 业务对象（T116）------------------------------------------
        # object_id 取 `content_digest` 而不是 case_id：一个案子可能发过多条内容
        # 不同的通知，用 case_id 会让引用指向"其中随便一条"。摘要精确指到
        # **说过的那句话**，而"说过什么"正是客户投诉时要对的第一件事。
        extras = getattr(ctx, "extras", None) or {}
        plan_id = str(extras.get("plan_id") or "")
        task_id = str(extras.get("task_id") or "")
        if plan_id and task_id:
            objects.attach_business_ref(
                store, plan_id=plan_id, task_id=task_id, tenant_id=tenant_id,
                object_type="notification", object_id=row["content_digest"],
                object_version=revision,
                purpose=f"告知客户处理结果（{channel}，第 {revision} 次）")

        return {
            "notification": row,
            "content": content,
            "public_status": public,
            "acked": ack_at is not None,
            "needs_followup": ack_at is None,
            "invocation_id": invocation_id,
        }

    # ------------------------------------------------------------------
    @staticmethod
    def _public_status(store, tenant_id: str, case_id: str, case: dict) -> str:
        """本案此刻的对外三态（跨轨契约 §D）。投不出来返回空串。

        两个入参都从库里现读，**不从 `biz_status` 推**：

        · `has_request` —— 「已提出退款」对客户而言是「退款请求发出去了」，
          不是「审批通过了」。审批通过而请求还没发，对外无话可说。
        · `observed_state` —— 「退款已到账」的 basis 必须是 `payment_observation`
          的行（铁律 8）。库里没有那一行时，`projection.public_status` 会拒绝
          说到账 —— 这一层不是多余的：它让「谁也没观察到，但状态字段写着 settled」
          这种库内自相矛盾说不出话来，而不是照样发一条报喜短信。
        """
        has_request = bool(objects.query(
            store, "SELECT request_id FROM refund_request WHERE tenant_id=? AND case_id=?"
                   " LIMIT 1", (tenant_id, case_id)))
        obs = objects.query(
            store, "SELECT observed_state FROM payment_observation"
                   " WHERE tenant_id=? AND case_id=? ORDER BY observed_at",
            (tenant_id, case_id))
        return projection.public_status(str(case["biz_status"]), has_request,
                                        projection.observed_state_of(obs))

    #: 三态投影投不出来时的回落措辞。**投影优先**：契约 §D 那五个字面值是唯一
    #: 对外口径，这张表只覆盖它没有对应值的那两档（`submitted` / 审批通过但还没
    #: 发起退款的 `approved`）。两张表不是两套口径 —— 这一张说的是内部进度，
    #: 那五句说的是客户能拿去对账的结论。
    _INTERNAL_SAID = {
        "submitted": "已受理，正在核定",
        "approved": "已通过审核，等待付款",
        "gateway_accepted": "退款已提交至支付渠道",
        "processing": "退款处理中，请留意到账通知",
        "settled": "退款已到账",
        "rejected": "经核定不符合退款政策",
        "compensated": "退款未能完成，已为您做冲正处理",
    }

    @classmethod
    def _default_content(cls, case: dict, public: str = "") -> str:
        """正文按案子当前状态生成 —— 状态是什么就说什么，不预告还没发生的事。

        `public` 非空就用它：三态投影是整仓唯一产出对外措辞的地方（契约 §D），
        房间卡片、`/pending` 回帖将来都接同一个函数，措辞才不会在几处之间漂。
        投不出来才回落到 `_INTERNAL_SAID`（见那张表的注释）。
        """
        status = case["biz_status"]
        said = public or cls._INTERNAL_SAID.get(status, status)
        return f"您的退款申请（{case['case_id']}）{said}。如有疑问请回复本条消息。"
