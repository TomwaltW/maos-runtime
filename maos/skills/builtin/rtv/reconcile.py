"""rtv.reconcile —— 三方对账：退货行 × 贷项通知单 × 到账。

SOP 第 ④ 步。这是 RTV 域对应 `ap` 域「三单匹配」的那一步，也是本域最容易被做错
的一处 ——

🔴 **本 skill 不推进任何业务状态。**

    对账是**推断**：按退货行算出「供应商应该退我 1200 元」。
    开票是**观察**：供应商真的开出了一张 1200 元的贷项通知单。

两者差着一次外部确认。算出应退 1200 不等于供应商认了这 1200 —— 把 `credited`
这一跳交给 `rtv.observe`，正是本域两个权威源分得开的关键（契约 C-R3）。
所以本文件里没有 `update_biz_status` 的任何调用，一次都没有。

## 三条腿分别从哪里来，为什么不一样

    ① 退货行合计    `rtv_line`                    我方数据，本地算
    ② 贷项通知单    `credit_note`                 **只认库里由 rtv.observe 落的那份**
    ③ 到账          `rtv_settlement_observation`  同上，只认已观察到的

第 ② 条腿最要紧：本 skill 依赖 `supplier.credit_query`（契约 C-R4），会去问一次
供应商侧的当前状态，但**问到的东西只用来解释缺口，不当作凭据**。供应商侧回
`issued` 而库里还没有 `credit_note` 时，结论是「等 rtv.observe 去观察」
（`RTV-R-11`），不是「那就算它开了吧」。想在这里顺手落一行 `credit_note` 是走不通
的 —— `_common.execute()` 直接拒（`BypassedGuardError`）。

第 ③ 条腿刻意**不问 AP**：`ap.adjust_query` 不在本 skill 的 `depends_tools` 里
（契约 C-R4）。问 AP 是 `rtv.observe` 的事，两处都问会让「钱到账了没有」有两个
答案，而它们迟早不一致。

## 顺利路径上第一次对账必然对不上，这是对的

`intake -> dispose -> ship -> reconcile -> observe` 的顺序里，对账跑在开票与到账
之前，所以第一次跑 `reconciled=0`，findings 是 `RTV-R-11` / `RTV-R-12` 两条
「还缺这条腿」。这不是失败，是如实报告：三方里我方这一方算清楚了，另外两方还没
说话。返工重跑保留历史（`attempt` 递增），对账结论的演进因此看得见。
"""

from __future__ import annotations

import json

from maos.skills.contract import Skill, SkillContext, SkillContract
from maos.skills.registry import register_skill

from . import _common as C

#: 默认容差：绝对金额 0.01（一分钱）。写成可配是因为不同供应商的合同容差不同；
#: 默认给最严的那档 —— 放宽是要有出处的决定，不是默认值该替人做的事。
DEFAULT_TOLERANCE = {"absolute": "0.01"}


@register_skill
class RtvReconcileSkill(Skill):
    contract = SkillContract(
        name="rtv.reconcile",
        version="1.0.0",
        purpose="退货行 × 贷项通知单 × 到账三方对账，写 rtv_reconciliation；**不推进业务状态**",
        input_schema={
            "tenant_id": "str",
            "case_id": "str",
            "tolerance": "dict（可选，{'absolute': '0.01'}）",
            "reconciled_by": "str（可选，缺省记 skill 名）",
        },
        output_schema={
            "reconciled": "bool（三条腿齐备且金额在容差内才为 True）",
            "attempt": "int（本次对账序号，历史保留）",
            "amount_claimed": "str（我方按退货行算出来的应收）",
            "amount_credited": "str（库里贷项通知单的合计；没有则空串）",
            "creditable_amount": "str（对上时的应收金额，对不上时空串）",
            "supplier_status": "submitted|acknowledged|issued|disputed|unknown（问到的当前状态）",
            "settled_observed": "bool（库里有没有 AP 侧的 settled 观察）",
            "findings": "list[dict]（每项带 rule_id，可核对）",
            "biz_status": "str（**与调用前相同** —— 本 skill 不推进状态）",
            "invocation_id": "str",
        },
        preconditions=["tenant_id", "case_id"],
        depends_tools=["supplier.credit_query"],
        failure_policy="retry",
        max_retries=1,
        security_boundary=(
            "只读三方数据、只写 rtv_reconciliation：不推进 biz_status（对账是推断，"
            "开票是观察），也写不动 credit_note / rtv_settlement_observation —— "
            "那两张表只有 rtv.observe 写得动，从这里写会被 _common.execute 的 "
            "BypassedGuardError 拦下。供应商查询经 invoke_tool 留审计行"
        ),
        reuse_note="任何「本地算出应得、外部确认实得」的域都该照此分：算出来的那一份"
                   "永远不许直接当成对方认了的那一份",
        owner_roles=["rtv_reconcile"],
    )

    def run(self, payload: dict, ctx: SkillContext) -> dict:
        store = C.ensure_schema(ctx)
        invocation_id = C.invocation_id_of(ctx)
        tenant_id, case_id = C.required(payload, "tenant_id", "case_id")

        case = C.get_case(store, tenant_id, case_id)
        if case is None:
            raise LookupError(f"没有这个 case：tenant={tenant_id} case={case_id}")
        lines = C.lines_of(store, tenant_id, case_id)
        if not lines:
            raise LookupError(f"case={case_id} 没有任何退货行，无从对账；先跑 rtv.intake")

        tolerance = dict(payload.get("tolerance") or DEFAULT_TOLERANCE)
        limit = C.money(tolerance.get("absolute") or "0")

        # ---- 第 ① 条腿：我方按退货行算 --------------------------------------
        claimed = C.money("0")
        for line in lines:
            claimed += C.money(line["unit_price"]) * C.money(str(line["quantity_returned"]))
        amount_claimed = C.money_str(claimed)

        # ---- 问一次供应商侧的当前状态。只用来解释缺口，不当凭据 ---------------
        advice = C.call_tool(ctx, store, C.TOOL_CREDIT_QUERY, {
            "tenant_id": tenant_id, "case_id": case_id,
        })
        supplier_status = C.require_state("supplier", advice.get("status"))

        # ---- 第 ② 条腿：只认库里由 rtv.observe 落下的贷项通知单 --------------
        notes = C.credit_notes_of(store, tenant_id, case_id)
        credited = C.money("0")
        for note in notes:
            credited += C.money(note["amount_credited"])
        amount_credited = C.money_str(credited) if notes else ""

        # ---- 第 ③ 条腿：只认库里 AP 侧的 settled 观察 -------------------------
        settled_rows = [r for r in C.settlement_observations_of(store, tenant_id, case_id)
                        if str(r["observed_state"]) == "settled"]

        findings = []
        if supplier_status == "disputed":
            findings.append(C.cite("RTV-R-13", "供应商侧回 disputed，这笔退货对方不认"))
        if not notes:
            detail = f"供应商侧当前状态 {supplier_status}"
            if supplier_status == "issued":
                detail += "；对方说已开票，但贷项通知单尚未由 rtv.observe 观察落库 —— " \
                          "在这里替它落一行等于自己给自己开发票"
            findings.append(C.cite("RTV-R-11", detail))
        elif abs(claimed - credited) > limit:
            findings.append(C.cite(
                "RTV-R-10",
                f"我方算出 {amount_claimed}，贷项通知单合计 {amount_credited}，"
                f"差额 {C.money_str(abs(claimed - credited))} 超出容差 {C.money_str(limit)}"))
        if not settled_rows:
            findings.append(C.cite("RTV-R-12", "库里没有 AP 侧的 settled 观察"))

        reconciled = not findings
        creditable_amount = amount_claimed if reconciled else ""

        attempt = C.next_attempt(store, "rtv_reconciliation", tenant_id, case_id)
        C.execute(
            store,
            "INSERT OR REPLACE INTO rtv_reconciliation (tenant_id, case_id, attempt,"
            " reconciled, creditable_amount, findings_json, tolerance_json,"
            " reconciled_by, reconciled_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (tenant_id, case_id, attempt, 1 if reconciled else 0, creditable_amount,
             json.dumps(findings, ensure_ascii=False, sort_keys=True),
             json.dumps(tolerance, ensure_ascii=False, sort_keys=True),
             str(payload.get("reconciled_by") or self.contract.name), C.now_iso()))

        # 🔴 到此为止。**不推进业务状态** —— 见模块 docstring 第一段。
        return {
            "reconciled": reconciled,
            "attempt": attempt,
            "amount_claimed": amount_claimed,
            "amount_credited": amount_credited,
            "creditable_amount": creditable_amount,
            "supplier_status": supplier_status,
            "settled_observed": bool(settled_rows),
            "findings": findings,
            "biz_status": str(case["biz_status"]),
            "invocation_id": invocation_id,
        }
