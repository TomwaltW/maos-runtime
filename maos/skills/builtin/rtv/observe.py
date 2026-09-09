"""rtv.observe —— 向两个外部系统**问**出终态，是全系统唯一写得进 `credited` 与
`settled` 的地方。

这是「观察与推断分离」在本域的落点，也是 RTV 相对 `ap` 域的增量：`ap` 只有一个
外部权威（银行），RTV 有**两个**，而且它们说的是两件不同的事。

    问：你怎么知道供应商认了这笔退货？
    答：不是因为 carrier.track 回了 delivered（那只说明货到了对方仓库），
        是因为 supplier.credit_query 问到第 N 次时供应商回了 `issued` 并给出了
        贷项通知单号，那份凭据连同状态更新在**同一个事务**里落了库（`credit_note`），
        `invocation_id` 指回是哪一次调用观察到的。

    问：你怎么知道钱到账了？
    答：不是因为供应商开了票（开票是对方的会计动作，不是钱的移动），
        是因为 ap.adjust_query 回了 `settled` 并给出 AP 侧的凭单引用，
        那条观察连同状态更新同事务落了库（`rtv_settlement_observation`）。

两件事各归各的外部系统，**一个都不许由另一个推定**。把它们合成一步（「开票即到账」）
是本域最容易犯、也最难在事后发现的错：账面上退款完成，钱其实还在供应商那儿。

## 供应商侧：四种非终态，一个都不许乐观处理

    submitted     已提退货申请          -> 不推进
    acknowledged  供应商收到货了        -> 🔴 不推进（**这不是「认账了」**）
    disputed      供应商不认这笔        -> 不推进，留痕并转补偿/人工
    unknown       供应商侧查不到        -> 不推进，**尤其不许重发 rma_submit**

`acknowledged` 最危险：它的回执字段齐全、形状与终态回执一模一样，差的只是一次
会计确认。放过它，系统持有的就只是「货到了对方仓库」，却在账上写着「对方认了这笔
退款」。守卫第 ④ 道（`AUTHORITATIVE_RECEIPT_STATE["credited"] == {"issued"}`）
是这条防线的底，本 skill 的分支只是第一层 —— 两层都在，是因为分支会被改，
守卫不会被顺手改。

`unknown` 的危险在另一头：那笔退货**可能已经被受理了**，只是查不到。在这里替它下
结论、或者「重发一次申请试试」，就是把外部状态写死为终态（铁律 8）+ 造出第二笔
退货申请。所以本 skill 的 `depends_tools` 里**没有** `supplier.rma_submit` ——
重发那条路在这里根本不存在，不是靠 if 分支拦着。

## AP 侧：三种非终态 + 一种作废

    none / staged / built  凭单还没核销   -> 不推进
    voided                 凭单作废       -> 不推进，留痕并转补偿
    settled                钱到账/冲抵    -> 推进到 settled

## 轮询到顶不改判成失败

到顶仍非终态时**一行状态都不推、一条回执都不写**。「我问累了」和「对方说没这回事」
是两回事，混起来会让一笔实际已经开出贷项通知单的退货在账上变成「没退成」，
然后被再申请一次。轮询次数落在状态变更事件的 detail 里 —— 它是「终态是问出来的」
的证据（契约 C-R1 的两张回执表没有 poll_count 列，而契约冻结，不许加）。
"""

from __future__ import annotations

from maos.skills.contract import Skill, SkillContext, SkillContract
from maos.skills.registry import register_skill

from . import _common as C

#: 轮询上限。到顶仍非终态就如实返回「还没问出来」，**不许**改判成失败。
DEFAULT_MAX_POLLS = 5

#: 当前业务状态 -> (要问哪个系统, 问哪个工具, 问出什么才推进, 推到哪个状态)。
#: 写成表而不是两段 if：加第三个权威源时改这里一行，而不是在分支树里插一层。
_STAGES: dict[str, tuple[str, str, str, str]] = {
    "shipped": ("supplier", C.TOOL_CREDIT_QUERY, "issued", "credited"),
    "credited": ("ap", C.TOOL_AP_ADJUST_QUERY, "settled", "settled"),
}


@register_skill
class RtvObserveSkill(Skill):
    contract = SkillContract(
        name="rtv.observe",
        version="1.0.0",
        purpose="轮询供应商与 AP 取得终态回执，写 credit_note / rtv_settlement_observation，"
                "并（仅在此处）写 credited 与 settled",
        input_schema={
            "tenant_id": "str",
            "case_id": "str",
            "max_polls": "int（可选，默认 5）",
        },
        output_schema={
            "advice": "dict（终态回执，或到顶时的最后一次观察）",
            "system": "supplier|ap（这一次问的是哪个外部系统）",
            "observed_state": "str（对方说的当前状态）",
            "poll_count": "int（问了几次 —— 终态是问出来的证据）",
            "reference": "str（可对账的外部单号：贷项通知单号或 AP 凭单引用）",
            "biz_status": "str（credited / settled 只可能由本 skill 写入）",
            "advanced": "bool（这次调用有没有推进状态）",
            "credited": "bool",
            "settled": "bool",
            "needs_compensation": "bool（供应商 disputed 或 AP 凭单 voided 时为 True）",
            "invocation_id": "str",
        },
        preconditions=["tenant_id", "case_id"],
        depends_tools=["supplier.credit_query", "ap.adjust_query"],
        failure_policy="escalate",
        max_retries=0,
        security_boundary=(
            "本 skill 是本域的 AUTHORITATIVE_WRITER —— 全系统唯一可写 credited / settled 的"
            " actor，且写入必须同事务附带外部单号的回执（credit_note / "
            "rtv_settlement_observation），缺字段或回执说的不是这件事，由守卫抛 "
            "AuthoritativeFactViolation。非终态一律不推进；两个查询工具都是只读，"
            "本 skill 依赖清单里没有任何写工具 —— unknown 时重发申请那条路不存在"
        ),
        reuse_note="任何「权威在外部系统」的终态都该照此写：先观察、再落库，两件事同一个"
                   "事务；有几个外部权威就分几跳，不许由一个推定另一个",
        owner_roles=["rtv_settlement"],
    )

    def run(self, payload: dict, ctx: SkillContext) -> dict:
        store = C.ensure_schema(ctx)
        invocation_id = C.invocation_id_of(ctx)
        tenant_id, case_id = C.required(payload, "tenant_id", "case_id")

        case = C.get_case(store, tenant_id, case_id)
        if case is None:
            raise LookupError(f"没有这个 case：tenant={tenant_id} case={case_id}")
        biz_status = str(case["biz_status"])
        stage = _STAGES.get(biz_status)
        if stage is None:
            # 调错了时机就响，不静默返回一个「什么都没发生」——「观察了但没结果」与
            # 「压根不该在这个状态观察」是两件事，混起来会让编排的错以业务的面目出现。
            raise LookupError(
                f"case={case_id} 当前是 {biz_status}，这个状态没有可观察的外部终态；"
                f"可观察的状态：{sorted(_STAGES)}")
        system, tool_name, want, target = stage

        max_polls = int(payload.get("max_polls") or DEFAULT_MAX_POLLS)
        if max_polls < 1:
            raise ValueError("max_polls 至少为 1 —— 一次都不问就没有观察可言")

        # ---- 轮询。终态由我方按取值域判，不看回执自述的 is_terminal -----------
        advice: dict = {}
        status, poll_count = "", 0
        for _ in range(max_polls):
            poll_count += 1
            advice = C.call_tool(ctx, store, tool_name, {
                "tenant_id": tenant_id, "case_id": case_id,
            })
            status = C.require_state(system, advice.get("status"))
            if C.terminal(system, status):
                break

        if not C.terminal(system, status):
            # 到顶仍非终态：如实返回。不写状态、不写回执 ——
            # 「还没问出来」不是一个可以落库的结论（见模块 docstring）。
            return self._out(advice, system, status, biz_status, poll_count,
                             invocation_id, advanced=False, needs_compensation=False)

        if status != want:
            # 终态，但是坏消息：供应商 disputed / AP 凭单 voided。
            # **不推进状态**：走到 compensated 意味着补偿已经做完，而补偿是
            # `rtv.compensate` 的事，在这里替它宣布收口就是又一次把状态写死。
            # 但要留痕 —— 否则「供应商说不认」这件事只活在日志里。
            C.record_adverse_observation(
                store, tenant_id=tenant_id, case_id=case_id, system=system,
                observed_state=status, actor_skill=self.contract.name,
                invocation_id=invocation_id,
                detail={"poll_count": poll_count,
                        "message": str(advice.get("message") or "")})
            return self._out(advice, system, status, biz_status, poll_count,
                             invocation_id, advanced=False, needs_compensation=True)

        # ---- 终态且是好消息：唯一写 credited / settled 的路径 -----------------
        if target == "credited":
            observation = {
                "credit_note_id": str(advice.get("credit_note_id") or ""),
                "observed_state": status,
                "amount_credited": advice.get("amount_credited"),
                "currency": str(advice.get("currency") or case["currency"]),
                "document_type": str(advice.get("document_type") or "381"),
                "issued_at": str(advice.get("issued_at") or ""),
                "observed_at": C.now_iso(),
            }
            reference = observation["credit_note_id"]
            reason = (f"供应商开出贷项通知单 {reference}（问了 {poll_count} 次，金额 "
                      f"{advice.get('amount_credited')}）")
        else:
            observation = {
                "adjustment_id": str(advice.get("adjustment_id") or ""),
                "observed_state": status,
                "ap_reference": str(advice.get("ap_reference") or ""),
                "observed_at": C.now_iso(),
            }
            reference = observation["ap_reference"]
            reason = (f"AP 调整凭单 {observation['adjustment_id']} 已核销"
                      f"（问了 {poll_count} 次，凭单引用 {reference}）")

        case = C.update_biz_status(
            store, tenant_id, case_id, target, self.contract.name, invocation_id,
            observation=observation, reason=reason, poll_count=poll_count)

        out = self._out(advice, system, status, str(case["biz_status"]), poll_count,
                        invocation_id, advanced=True, needs_compensation=False)
        out["reference"] = reference
        return out

    # ------------------------------------------------------------------
    @staticmethod
    def _out(advice: dict, system: str, status: str, biz_status: str, poll_count: int,
             invocation_id: str, *, advanced: bool, needs_compensation: bool) -> dict:
        return {
            "advice": advice,
            "system": system,
            "observed_state": status,
            "poll_count": poll_count,
            "reference": "",
            "biz_status": biz_status,
            "advanced": advanced,
            "credited": biz_status in ("credited", "settled"),
            "settled": biz_status == "settled",
            "needs_compensation": needs_compensation,
            "message": str(advice.get("message") or ""),
            "invocation_id": invocation_id,
        }
