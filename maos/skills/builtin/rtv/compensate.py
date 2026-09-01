"""rtv.compensate —— 失败路径的收口：把「这笔为什么没退成」落成一条可查的记录。

补偿不是「重试一次」，是**做完善后动作之后**才把案子收到 `compensated`。
状态机上它只从 `disposed` / `shipped` / `credited` 过来（契约 C-R2）——
`received` 阶段还没有任何对外动作，没有什么要补偿的，那一档走 `rejected`。

## 什么时候该走到这里

    供应商 disputed        对方明确不认这笔退货
    AP 调整凭单 voided     凭单被作废，钱不会回来
    承运商 exception       货在路上出事（丢件/被退回）

三种都是**外部明确说了一件坏事**，不是「还没问出来」。后者不许走补偿 ——
问不出来时正确动作是继续问、问不出来转人工，那条路由 `effect_risk=H` 的人工
审批走。

## 🔴 unknown 一律不许重发申请

`supplier.rma_submit` 是本域唯一的写工具，也是本 skill 唯一依赖的工具
（契约 C-R4）。供应商侧回 `unknown` 时**那笔申请可能已经受理了**，只是查不到 ——
这时重发会造出第二笔退货申请，而两笔申请对同一批货，对账那一步会看到双倍金额。
所以 `observed_state="unknown"` 且 `resubmit=True` 的组合在这里直接抛，
不是记一条警告放过去。

## 补偿记录先落，状态后推

`rtv_compensation_record` 是「补偿做过了」的证据。先落记录再推状态：反过来的话，
状态已经收口而记录写失败，案子看起来收得干干净净，却说不出为什么。
"""

from __future__ import annotations

import json

from maos.skills.contract import Skill, SkillContext, SkillContract
from maos.skills.registry import register_skill

from . import _common as C


@register_skill
class RtvCompensateSkill(Skill):
    contract = SkillContract(
        name="rtv.compensate",
        version="1.0.0",
        purpose="失败路径收口：写 rtv_compensation_record，必要时补提 RMA，并推进到 compensated",
        input_schema={
            "tenant_id": "str",
            "case_id": "str",
            "reason": "str（为什么要补偿，必填 —— 收口没有理由等于没收口）",
            "observed_state": "str（可选，触发补偿的那次观察结果；unknown 时禁止 resubmit）",
            "detail": "dict（可选，随记录落库的上下文）",
            "resubmit": "bool（可选，默认 False；True 才调 supplier.rma_submit）",
        },
        output_schema={
            "record": "dict（rtv_compensation_record 那一行）",
            "seq": "int（本案第几次补偿，历史保留）",
            "resubmitted": "bool（有没有真的补提 RMA）",
            "rma": "dict（补提时供应商侧的回执，未补提为空 dict）",
            "biz_status": "str（compensated）",
            "invocation_id": "str",
        },
        preconditions=["tenant_id", "case_id", "reason"],
        depends_tools=["supplier.rma_submit"],
        failure_policy="escalate",
        max_retries=0,
        security_boundary=(
            "唯一会调写工具（supplier.rma_submit）的 RTV skill，且只在调用方显式 "
            "resubmit=True 时调；observed_state=unknown 时一律拒绝重发 —— 那笔申请"
            "可能已经受理，重发会造出第二笔。只写 rtv_compensation_record 与 "
            "biz_status -> compensated，credited / settled 在本 skill 里没有写入路径"
        ),
        reuse_note="任何域的失败收口都该照此写：先落补偿记录再推状态，"
                   "「问不出来」不许走补偿，写操作在不确定时一律不重发",
        owner_roles=["rtv_settlement"],
    )

    def run(self, payload: dict, ctx: SkillContext) -> dict:
        store = C.ensure_schema(ctx)
        invocation_id = C.invocation_id_of(ctx)
        tenant_id, case_id, reason = C.required(payload, "tenant_id", "case_id", "reason")

        case = C.get_case(store, tenant_id, case_id)
        if case is None:
            raise LookupError(f"没有这个 case：tenant={tenant_id} case={case_id}")

        observed_state = str(payload.get("observed_state") or "")
        resubmit = bool(payload.get("resubmit"))
        if resubmit and observed_state == "unknown":
            raise ValueError(
                "供应商侧回 unknown 时不许补提 RMA：那笔申请可能已经受理，"
                "重发会造出第二笔退货申请。问不出来就继续问，问不出来转人工")

        rma: dict = {}
        if resubmit:
            rma = C.call_tool(ctx, store, C.TOOL_RMA_SUBMIT, {
                "tenant_id": tenant_id, "case_id": case_id,
                "reason": str(reason), "compensation": True,
            })

        detail = dict(payload.get("detail") or {})
        detail.update({"observed_state": observed_state, "resubmitted": resubmit,
                       "from_status": str(case["biz_status"])})
        if rma:
            detail["rma"] = rma

        seq = C.next_attempt(store, "rtv_compensation_record", tenant_id, case_id)
        C.execute(
            store,
            "INSERT OR REPLACE INTO rtv_compensation_record (tenant_id, case_id, seq,"
            " reason, detail_json, compensated_by, compensated_at) VALUES (?,?,?,?,?,?,?)",
            (tenant_id, case_id, seq, str(reason),
             json.dumps(detail, ensure_ascii=False, sort_keys=True, default=str),
             self.contract.name, C.now_iso()))

        case = C.update_biz_status(
            store, tenant_id, case_id, "compensated", self.contract.name, invocation_id,
            reason=f"补偿收口（第 {seq} 次）：{reason}")

        rows = C.query(store, "SELECT * FROM rtv_compensation_record WHERE tenant_id=?"
                              " AND case_id=? AND seq=?", (tenant_id, case_id, seq))
        return {
            "record": rows[0] if rows else {},
            "seq": seq,
            "resubmitted": resubmit,
            "rma": rma,
            "biz_status": str(case["biz_status"]),
            "invocation_id": invocation_id,
        }
