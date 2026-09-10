"""refund.compensation_close —— 人工把工单处理完之后，**把观察回填进系统**。

## 这个 skill 补的是哪一处断裂

从前的补偿链到工单为止：`refund.compensate` 作废退款请求、开一张 `manual_ticket`、
把案子推到 `compensated`，然后就结束了。人在支付渠道后台把那笔钱线下退掉之后，
这个事实**回不到系统里** —— MAOS 永远停在「最后一次观察到的下落 = unobserved」。

于是「流程卡住后应由谁补偿」只答得出前半句（有人接单），答不出后半句
（他做完了，系统怎么知道）。本 skill 补的就是后半句。

## 最要紧的一条：它一个字都不写 `settled`

诱惑是显然的 —— 人都说退成功了，直接把 `biz_status` 改成 `settled` 不就完了。
**那是错的**，而且是本域最贵的一种错：`payment.observe` 是全系统唯一的
`settled` 写者（`guard.AUTHORITATIVE_WRITER`），开第二条路径等于把那道闸变成摆设，
下一个人就会开第三条。

正确的建模是把人工凭证看成**它本来的样子**：一份来自外部的回执。人到支付宝后台
看到「已退款」，与 MAOS 自己 query 到 `settled`，是同一类事实的两种取得方式，
区别只在取得的渠道是人眼还是 API。所以本 skill 做的是：

    线下凭证摘要 -> ManualReceiptAdapter 包成一份 gateway='manual' 的回执
                 -> 经 payment.observe 这一条**唯一**通道落 payment_observation
                 -> 把那条观察的引用回填进工单的 resolution_observation_id
                 -> 落一条 CompensationResolved

`payment_observation` 那一行的 `actor_invocation_id` 指回的是一次**真实的
`payment.observe` 调用**（本 skill 经 `SkillInvoker` 调它，留 `SkillInvoked` 审计行），
所以 `scripts/verify.py` 第 3 项那条「回执出自 payment.observe」的反查照样成立。

## 案子已经 compensated 了，观察落得进去吗

落得进去，**但状态不动**。`compensated` 在 `guard.BIZ_STATUS_FLOW` 里是终态，
铁律 9 不许为此加一条新迁移。`payment.observe` 的人工分支因此只落观察、不推状态
（理由与它见到网关明确失败时一模一样：观察 ⇐ 终态，反过来不成立）。

这不是妥协，是正确的：`biz_status` 记的是**本系统这条流程走到哪了**——它确实
补偿收口了；钱到没到账记在 `payment_observation` 上，那才是外部权威说的话。
两者分开，正是铁律 8 要的那条线。对外投影怎么把这两件事合成一句人话，
由 T116 的 `projection.public_status` 负责（跨轨契约 §D），不在这里拼字符串。

## 关单结论不进状态机

`resolution_kind`（settled / not_settled）是 `compensation_record` 自己的列，
不是 `biz_status`，也不是 Task 状态（铁律 9）。
"""

from __future__ import annotations

from maos.domain.refund import objects
from maos.skills.contract import Skill, SkillContext, SkillContract
from maos.skills.invoker import SkillInvoker
from maos.skills.registry import register_skill
from maos.tools.gateway import GATEWAY_MANUAL, ManualReceiptAdapter
from maos.tools.gateway_codes import OUTCOME_FAILED, OUTCOME_SUCCESS

from . import _common as C
from . import compensate as CP

#: 被调方。写成常量而不是散在各处的字面量 —— 这一行是本 skill 与权威边界的接缝，
#: 改它就等于改「谁能写 settled」，该一眼看得见。
SKILL_OBSERVE = "payment.observe"

#: 关单结论 -> 人工凭证的 outcome。两张表同增同减：多一种结论就要说清它对应
#: 外部系统的哪种下落，否则「关单了」和「钱怎么样了」之间又会出现一段没人定义的空白。
_OUTCOME_OF: dict[str, str] = {
    CP.RESOLUTION_SETTLED: OUTCOME_SUCCESS,
    CP.RESOLUTION_NOT_SETTLED: OUTCOME_FAILED,
}


def manual_gateway_name(request_id: str) -> str:
    """人工凭证适配器在网关注册表里的名字，**按请求收窄**。

    不共用一个 `"manual"`：注册表是进程级的，同一个进程里两笔工单先后关单时，
    后一次 `register_gateway("manual", ...)` 会把前一次那个实例连同它收下的凭证
    一起替换掉。按 request_id 分名之后，两笔之间不可能串账 —— 与
    `_common.get_gateway` 「取不到就抛、不兜底」同一个取向：宁可少一个实例，
    不要一个装着别人凭证的实例。
    """
    return f"{GATEWAY_MANUAL}:{request_id}"


@register_skill
class RefundCompensationCloseSkill(Skill):
    contract = SkillContract(
        name="refund.compensation_close",
        version="1.0.0",
        purpose="人工工单处理完之后的关单：把线下凭证摘要经 payment.observe 落成一条"
                "payment_observation，回填工单的 resolution_observation_id，落 CompensationResolved",
        input_schema={
            "tenant_id": "str",
            "case_id": "str",
            "operator": "str（提交凭证的人；应是工单的承接人）",
            "evidence_ref": "str（外部凭证引用，如渠道后台流水号 —— 这是它作为外部事实的出处）",
            "summary": "str（可选，凭证摘要原文，原样进回执 detail 与事件）",
            "resolution_kind": f"str（{CP.RESOLUTION_SETTLED}|{CP.RESOLUTION_NOT_SETTLED}，"
                               f"缺省 {CP.RESOLUTION_SETTLED}）",
        },
        output_schema={
            "ticket": "dict（关单后的工单行）",
            "resolution_kind": "settled|not_settled",
            "observation_id": "str（回填的那条 payment_observation 的自然键 request_id@observed_at）",
            "observed_state": "str（那条观察的 observed_state，来自 payment.observe 的出参）",
            "biz_status": "str（**通常不变** —— 案子已 compensated，观察不推终态）",
            "observe_invocation_id": "str（哪一次 payment.observe 落的那条观察）",
            "invocation_id": "str",
        },
        preconditions=["tenant_id", "case_id", "operator", "evidence_ref"],
        depends_tools=["gateway.query"],
        # 不重试：关单会写观察行与工单列，重试一次就多一条观察。
        # 失败要人看见 —— 与 refund.compensate 同一个理由。
        failure_policy="escalate",
        max_retries=0,
        security_boundary=(
            "**不写 settled，也不写任何 biz_status** —— 观察一律经 payment.observe 落库，"
            "本 skill 只写 compensation_record 的关单列；"
            "关单必须带 evidence_ref 与回填的观察引用，指不到观察的结论不许落；"
            "已关闭的工单拒绝二次关单，不静默覆盖；"
            "调用方的 identity 必须同时授权 refund.compensation_close 与 payment.observe，"
            "缺授权时 SkillInvoker 抛 PermissionDenied，**不降级成本地直写**"
        ),
        reuse_note=(
            "任何「人在系统外把事办了，要把结果收回来」的域都该照此写："
            "把人工凭证建模成一份外部回执，走该域既有的那条权威观察通道，"
            "不为人工另开一条写终态的路"
        ),
        owner_roles=["refund_payment"],
    )

    def run(self, payload: dict, ctx: SkillContext) -> dict:
        store = C.ensure_schema(ctx)
        CP.ensure_ticket_schema(store)
        invocation_id = C.invocation_id_of(ctx)
        extras = dict(getattr(ctx, "extras", None) or {})
        tenant_id, case_id, operator, evidence_ref = C.required(
            payload, "tenant_id", "case_id", "operator", "evidence_ref")

        resolution = str(payload.get("resolution_kind") or CP.RESOLUTION_SETTLED)
        outcome = _OUTCOME_OF.get(resolution)
        if outcome is None:
            raise ValueError(
                f"关单结论只能是 {sorted(_OUTCOME_OF)}，实际 {resolution!r}；"
                "「还没查出来」不是一种关单结论 —— 那种情况工单就该开着")

        # ---- 第一步：先确认这张单还开着 ------------------------------------
        # 放在提交凭证之前：已经关掉的单再收一份凭证，那份凭证会落成一条观察行
        # 却没有任何工单指向它 —— 一条没人认领的外部事实，比拒绝掉更难查。
        ticket = CP.require_ticket(store, tenant_id, case_id)
        if str(ticket.get("resolved_at") or ""):
            raise ValueError(
                f"工单 {CP.ticket_id_of(case_id)} 已于 {ticket['resolved_at']} 关闭"
                f"（结论 {ticket.get('resolution_kind')!r}）；重复关单会盖掉已回填的观察")

        # ---- 第二步：这份凭证说的是哪一笔请求 --------------------------------
        rows = objects.query(
            store,
            "SELECT request_id, idempotency_key FROM refund_request"
            " WHERE tenant_id=? AND case_id=? ORDER BY submitted_at DESC",
            (tenant_id, case_id))
        if not rows:
            # 没有 refund_request 就没有可观察的对象。口径同 payment.observe ——
            # 不造一个假的 request_id 把观察挂上去：那条观察指不到任何真实请求，
            # 人工对账时反而多一条误导。
            raise LookupError(
                f"case={case_id} 没有 refund_request，人工凭证无处回填；"
                "这个案子从来没走到付款那一步，关单要走别的口径")
        request_id = str(rows[0]["request_id"])

        # ---- 第三步：人工凭证 -> 一份 gateway='manual' 的回执 ------------------
        adapter = ManualReceiptAdapter()
        adapter.submit(request_id=request_id,
                       idempotency_key=str(rows[0]["idempotency_key"] or ""),
                       outcome=outcome, evidence_ref=str(evidence_ref),
                       summary=str(payload.get("summary") or ""),
                       submitted_by=str(operator), submitted_at=C.now_iso())
        gateway_name = manual_gateway_name(request_id)
        C.register_gateway(gateway_name, adapter)

        # ---- 第四步：经 payment.observe 落观察。**这是本 skill 唯一的落库路径** ----
        # 走 SkillInvoker 而不是直接 `PaymentObserveSkill().run()`：白名单校验与
        # SkillInvoked 审计行都在 invoker 里，而那条审计行正是 verify.py 反查
        # 「这条回执出自哪一次 observe 调用」的依据。直接调就没有它。
        #
        # max_polls=1 是如实的，不是省事：人工这条路上「问一次」就是全部 ——
        # 人已经把外部系统看完了才来提交凭证，再问第二次没有新信息。
        # 回执里的 poll_count 仍由适配器如实计数，不写死。
        observe_extras = {k: v for k, v in extras.items() if k != "invocation_id"}
        res = SkillInvoker(ctx.identity, store).invoke(SKILL_OBSERVE, {
            "tenant_id": tenant_id, "case_id": case_id,
            "gateway": gateway_name, "request_id": request_id, "max_polls": 1,
        }, extras=observe_extras)
        if res.status != "ok" or not isinstance(res.output, dict):
            raise RuntimeError(
                f"人工凭证没能经 {SKILL_OBSERVE} 落成观察：{res.error}；"
                "关单不允许降级 —— 绕过它自己写一条观察，等于给权威边界开第二条路径")
        observed = res.output
        observe_invocation_id = str(res.invocation_id or "")

        # ---- 第五步：把那条观察找回来，回填进工单 -----------------------------
        observation = self._observation_of(store, tenant_id, case_id, request_id,
                                           expect_code=str(
                                               (observed.get("receipt") or {}).get("code") or ""))
        observation_id = f"{observation['request_id']}@{observation['observed_at']}"
        ticket = CP.resolve_ticket(store, tenant_id=tenant_id, case_id=case_id,
                                   resolution_kind=resolution,
                                   observation_id=observation_id)

        store.append_event_log({
            "trace_id": str(extras.get("trace_id") or ""),
            "plan_id": str(extras.get("plan_id") or ""),
            "task_id": str(extras.get("task_id") or ""),
            "event_type": CP.EVENT_COMPENSATION_RESOLVED,
            "reason": f"{operator} 提交线下凭证关单：{resolution}",
            "detail": {
                "domain": C.BIZ_TYPE,
                "tenant_id": tenant_id,
                "case_id": case_id,
                "ticket_id": CP.ticket_id_of(case_id),
                "operator": operator,
                "assignee_role": ticket.get("assignee_role", ""),
                "assignee": ticket.get("assignee", ""),
                "resolution_kind": resolution,
                "evidence_ref": str(evidence_ref),
                "observation_id": observation_id,
                # 观察到什么原样抄进来，**不在这里改写措辞**：这条事件是审计读的，
                # 它说的必须与 payment_observation 那一行逐字一致。
                "observed_state": str(observation["observed_state"]),
                "gateway": GATEWAY_MANUAL,
                "observe_invocation_id": observe_invocation_id,
                "invocation_id": invocation_id,
            },
        })

        return {
            "ticket": ticket,
            "resolution_kind": resolution,
            "observation_id": observation_id,
            "observed_state": str(observation["observed_state"]),
            "biz_status": str(observed.get("biz_status") or ""),
            "observe_invocation_id": observe_invocation_id,
            "invocation_id": invocation_id,
        }

    # ------------------------------------------------------------------
    @staticmethod
    def _observation_of(store, tenant_id: str, case_id: str, request_id: str,
                        *, expect_code: str) -> dict:
        """取刚刚落下的那条观察。**按回执码核对过才认**。

        为什么要核对：`payment_observation` 按 `observed_at` 追加，同一笔请求上
        可能已经躺着一条更早的观察（场景 7 那笔在网关入口被拒时就落过一条 failed）。
        单取「最新一行」在正常情况下没问题，但一旦 `payment.observe` 因为某种原因
        **一行都没写**，最新一行就会是那条旧的 —— 于是工单指向一条与本次关单
        毫无关系的观察，而且一路不报错。这正是本轨要买掉的那类静默失效。

        核对判据用回执码而不是时间戳：`MANUAL.SETTLED` / `MANUAL.NOT_SETTLED`
        只可能由本次提交的人工凭证产生，比「时间足够近」这种判据硬得多。
        """
        rows = objects.query(
            store,
            "SELECT * FROM payment_observation WHERE tenant_id=? AND case_id=? AND request_id=?"
            " ORDER BY observed_at DESC",
            (tenant_id, case_id, request_id))
        if not rows:
            raise RuntimeError(
                f"{SKILL_OBSERVE} 报成功却没有落下任何 payment_observation"
                f"（tenant={tenant_id} case={case_id} request={request_id}）")
        latest = rows[0]
        if expect_code and str(latest["gateway_code"]) != expect_code:
            raise RuntimeError(
                f"最新一条观察的 gateway_code={latest['gateway_code']!r}，"
                f"不是本次人工凭证的 {expect_code!r} —— 本次关单没有落下自己的观察，"
                "不许把工单指到别人那条上")
        return latest
