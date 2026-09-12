"""notify.customer —— 把处理结果告知客户，记 `notification`，等回执确认。

**ack 缺失不阻塞。** 客户看没看那条短信，不在 MAOS 的控制范围内，也不是退款是否
完成的判据 —— 钱已经退了，客户三天没点开通知，不该让整个 Plan 卡在这里。
所以缺 ack 的处置是记一条 `needs_followup` 继续走，而不是 blocked。

反过来也要守住：**不许把「发出去了」记成「客户确认了」**。`ack_at` 为空就是为空，
不拿 `sent_at` 顶替 —— 顶替之后，「有多少客户其实没收到」这个数字就永远查不出来了。

## 补偿收口那一档为什么要多说一句（T137）

这个 skill 在 DAG 上挂在付款之后。付款闸被人驳回、任务落 FAILED 之后，它停在
PENDING 再也不跑 —— 于是「钱没退出去、补偿工单开了、也派了也关了」这一整串事
**客户一个字都不知道**。补上的那两个调用点在编排层（`flows/custom_case.py` 的
驳回分支与 `ingress/router.py` 的 `/resolve` 之后），措辞仍然只由本模块产出：
`_compensation_tail()` 在投影句之后补上工单号与凭证引用。

那一句的红线是铁律 8：`payment_observation` 上没有到账观察时，正文里不许出现
任何到账口径。「原路退回未成功，已转线下补偿」说的是**观察与安排**，
不是一个本系统无权宣布的资金结果 —— 但它**本身也是一条观察**，所以只在最后一次
观察确实是 `failed` 时才说得出口。一行观察都没有的那一档（`/compensate` 人工兜底、
或轮询到顶没问出终态）换成「原路退回结果未确认，已转线下补偿」：只说本系统做了
什么，不替外部系统宣布那笔钱的下落（整合期 p10-f 补）。
"""

from __future__ import annotations

import json

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
            "正文只含案子编号、对外三态，以及补偿收口那一档的工单号与凭证引用（T137），"
            "不带客户证据原文；"
            "**没有到账观察就一个字不许说到账**——到账口径只由 projection.public_status 产出"
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
                   or self._default_content(
                       case, public, self._compensation_tail(store, tenant_id, case_id, case)))

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
    def _default_content(cls, case: dict, public: str = "", tail: str = "") -> str:
        """正文按案子当前状态生成 —— 状态是什么就说什么，不预告还没发生的事。

        `public` 非空就用它：三态投影是整仓唯一产出对外措辞的地方（契约 §D），
        房间卡片、`/pending` 回帖将来都接同一个函数，措辞才不会在几处之间漂。
        投不出来才回落到 `_INTERNAL_SAID`（见那张表的注释）。

        `tail` 是补偿收口那一档**另外**要交代的一句（`_compensation_tail`），
        夹在投影句与落款之间。它不改写投影那五个字面值中的任何一个 ——
        契约 §D 说的是「不许自造第六句」，不是「不许多说一句实话」。
        """
        status = case["biz_status"]
        said = public or cls._INTERNAL_SAID.get(status, status)
        extra = f"{tail.strip()}。" if tail.strip() else ""
        return f"您的退款申请（{case['case_id']}）{said}。{extra}如有疑问请回复本条消息。"

    #: 补偿收口之后那一句交代的前半段。**只说观察与安排，一个字不宣布资金结果**
    #: （铁律 8）：`payment_observation` 上最后一次观察是 failed，所以说得出
    #: 「原路退回未成功」；钱有没有通过线下渠道回到客户手里，MAOS 观察不到，
    #: 于是这句话里既没有「已到账」也没有「已退回」——它给的是工单号与凭证引用
    #: 这两个**抓手**，客户拿它们去问，比一句编出来的结论有用得多。
    COMPENSATION_SAID = "原路退回未成功，已转线下补偿"

    #: 同一档的**另一种**说法：补偿开了，但原路那一笔 MAOS 一次都没观察到终态
    #: （`/compensate` 人工兜底那条路就是这一档，`last_observed_state=unobserved`；
    #: 轮询到顶没问出结果的也是）。这时说「原路退回未成功」就是替外部系统宣布了
    #: 一个本系统没观察到的资金结果 —— 正是铁律 8 那一格。所以这一档只说
    #: **本系统做了什么**（转了线下补偿），不说原路那一笔的下落。
    #: 整合期 p10-f 补：此前两档共用上面那句，`/compensate` 那条路上是假话。
    COMPENSATION_SAID_UNOBSERVED = "原路退回结果未确认，已转线下补偿"

    @staticmethod
    def _compensation_tail(store, tenant_id: str, case_id: str, case: dict) -> str:
        """补偿收口那一档要另外交代的一句；不在那一档、或工单查不到时返回空串。

        为什么非有这一段不可：`compensated` 那一档的投影句是「已补偿（未到账）」，
        五个字面值里最短的那句 —— 客户读完只知道钱没到，不知道原路为什么没退成、
        也不知道去哪追。而这条通知存在的全部理由就是把「发生了什么、接下来找谁」
        说清楚（T137）。

        工单查不到就**回落到只说投影句**，不编一个单号：`MT-<case_id>` 是算得出来的，
        正因为算得出来才更要先确认它真的开过 —— 给客户一个不存在的工单号，
        比少说一句话坏得多。

        **原路那一笔的下落要现查**（整合期 p10-f 补，铁律 8）：只有**观察到过**
        一次 `failed` 才说得出「原路退回未成功」。`/compensate` 那条人工兜底路径
        刻意没有那道收窄（`outcome_commands.py` 原文「这条命令没有那道收窄」），
        于是 `payment_observation` 一行都没有时也会走到这里 —— 此前两档共用同一句，
        等于把一件 MAOS 从未观察到的外部资金结果写进了发给客户的正文。

        判据是「有没有观察到过 failed」而**不是**「最后一条是不是 failed」：
        线下关单会补写一条观察（`settled` / `failed`），按最后一条判的话，
        `/resolve` 之后那句本来正确的「原路退回未成功」会被翻成「结果未确认」——
        原路那一笔确实失败过，这是已经观察到的事实，不因为后面补了一条线下观察
        而变得不确定。整合期 p10-f 用 `p10f_probe_tail` 实测过这四档才定的判据。
        """
        if str(case.get("biz_status") or "") != "compensated":
            return ""
        from . import compensate as CP

        ticket = CP.ticket_of(store, tenant_id, case_id)
        if ticket is None:
            return ""
        head = (NotifyCustomerSkill.COMPENSATION_SAID
                if NotifyCustomerSkill._original_attempt_failed(store, tenant_id, case_id)
                else NotifyCustomerSkill.COMPENSATION_SAID_UNOBSERVED)
        said = f"{head}：工单 {CP.ticket_id_of(case_id)}"
        ref = NotifyCustomerSkill._evidence_ref(store, tenant_id, case_id)
        return f"{said}，凭证 {ref}" if ref else said

    @staticmethod
    def _original_attempt_failed(store, tenant_id: str, case_id: str) -> bool:
        """**收口这一笔**上有没有观察到过一次 `failed`。

        收窄口径与 `compensate.RefundCompensateSkill._last_observed_state` 同一套
        （按当前那一笔 `refund_request` 的 `request_id`）：换渠道重试之后一个案子
        会先后有两笔请求，上一笔的失败不是这一笔的下落。

        用 `EXISTS` 而不是「最后一条」：线下关单会在同一笔请求上补写一条观察，
        按最后一条判会把已经观察到的失败抹掉（见 `_compensation_tail` 的说明）。
        """
        current = objects.query(
            store,
            "SELECT request_id FROM refund_request WHERE tenant_id=? AND case_id=?"
            " ORDER BY submitted_at DESC", (tenant_id, case_id))
        if not current:
            return False
        rows = objects.query(
            store,
            "SELECT 1 FROM payment_observation"
            " WHERE tenant_id=? AND case_id=? AND request_id=? AND observed_state=?"
            " LIMIT 1",
            (tenant_id, case_id, current[0]["request_id"], "failed"))
        return bool(rows)

    @staticmethod
    def _evidence_ref(store, tenant_id: str, case_id: str) -> str:
        """最近一份人工线下凭证的外部引用（渠道流水号）；没有返回空串。

        取自 `payment_observation.raw_receipt_json` 的 `detail.evidence_ref` ——
        那是 `ManualReceiptAdapter.submit()` 落下的那个键，也是这份凭证作为
        **外部事实**的出处。不另存一份到别的表：存第二份就有了第二个真相源，
        而两份迟早对不上（口径同 `compensate.py` 那段「ticket_id 只在 detail_json 里」）。
        """
        rows = objects.query(
            store,
            "SELECT raw_receipt_json FROM payment_observation"
            " WHERE tenant_id=? AND case_id=? ORDER BY observed_at DESC",
            (tenant_id, case_id))
        for row in rows:
            try:
                receipt = json.loads(row["raw_receipt_json"] or "{}")
            except (TypeError, ValueError):
                continue
            detail = receipt.get("detail") if isinstance(receipt, dict) else None
            ref = str((detail or {}).get("evidence_ref") or "").strip()
            if ref:
                return ref
        return ""
