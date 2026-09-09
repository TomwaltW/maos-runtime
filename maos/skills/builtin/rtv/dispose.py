"""rtv.dispose —— 裁定这笔退货怎么处置：credit / exchange / replacement 三选一。

SOP 第 ② 步。三种处置类型不是自创的，出处是 PeopleSoft FSCM 的 RTV return action
与 Dynamics 365 Field Service 的 processing action（契约 §7）。

## 裁定是**推断**，不是观察

这一步的结论完全由本地规则算出来：退货理由 -> 规则编号 -> 处置类型。
它没有、也不该有任何外部权威参与 —— 所以它写得动 `return_action` 与
`biz_status: received -> disposed`，但写不动 `credited` / `settled`
（那两个由 `rtv.observe` 观察外部系统得到）。

## 每一条结论都必须挂得上一个编号

`rtv_disposition.rationale_json` 的每一项都带 `rule_id`，且编号必在 `RULES` 里
（契约 C-R1）。这就是「理由可核对」：对方拿着编号能查到判据，这句话才成立。
自造一个 `RTV-R-99` 写进去，被问一句「这条规则是哪来的」就全塌。

## 混合理由不许自动合并

`rtv_case.return_action` 是**案子头上的一个值**（PeopleSoft 的 header 级 return
action 就是一个）。一案里既有「发错货要补发」又有「次品要退款」时，正确动作是
拆成两个案子，而不是在这里挑一个当代表 —— 挑了之后另一半的诉求会静悄悄消失，
且在对账那步以「金额对不上」的面目重新出现。

## 裁定不被诉求牵着走

调用方可以递 `requested_action` 表达诉求，但结论以规则为准：两者不一致时按规则
裁定，并在 rationale 里记下「诉求是什么、为什么没照办」。让申请方指定结论，
等于没有裁定这一步。

## 不予受理走的是 rejected，不落 rtv_disposition

超出退货窗口（`RTV-R-05`）的结论是「不办」，而 `rtv_disposition.return_action`
的 CHECK 只收三种处置类型 —— 硬塞一个 `rejected` 进去要么被 sqlite 拒，要么得放宽
契约里的 CHECK，两条都不行。所以这一档只推状态到 `rejected`，理由落在状态变更
事件与返回值里。
"""

from __future__ import annotations

import json

from maos.skills.contract import Skill, SkillContext, SkillContract
from maos.skills.registry import register_skill

from . import _common as C


@register_skill
class RtvDisposeSkill(Skill):
    contract = SkillContract(
        name="rtv.dispose",
        version="1.0.0",
        purpose="按退货理由与合同条款裁定 credit/exchange/replacement，写 rtv_disposition 并推进到 disposed",
        input_schema={
            "tenant_id": "str",
            "case_id": "str",
            "within_return_window": "bool（可选，默认 True；False 走 RTV-R-05 不予受理）",
            "requested_action": "str（可选，申请方的诉求；与规则不一致时以规则为准）",
            "decided_by": "str（可选，裁定人/角色，缺省记 skill 名）",
        },
        output_schema={
            "case": "dict（rtv_case 当前那一行）",
            "return_action": "credit|exchange|replacement|''（不予受理时为空串）",
            "rejected": "bool",
            "attempt": "int（本次裁定的序号，返工重裁保留历史）",
            "rationale": "list[dict]（每项带 rule_id，可核对）",
            "biz_status": "str（disposed 或 rejected）",
            "invocation_id": "str",
        },
        preconditions=["tenant_id", "case_id"],
        depends_tools=[],
        failure_policy="escalate",
        max_retries=0,
        security_boundary=(
            "只写 rtv_disposition 与 rtv_case.return_action，并把 biz_status 推到 "
            "disposed / rejected；credited 与 settled 在本 skill 里没有任何写入路径，"
            "递进去也会被守卫第 ① 道拒掉（本 skill 不是 AUTHORITATIVE_WRITER）。"
            "裁定依据全部来自本地规则表，不调任何外部工具"
        ),
        reuse_note="任何「本地规则出结论」的一步都该照此写：结论挂编号、编号可查、"
                   "历史结论保留、诉求不等于结论",
        owner_roles=["rtv_disposition"],
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
            raise LookupError(f"case={case_id} 没有任何退货行，无从裁定；先跑 rtv.intake")

        decided_by = str(payload.get("decided_by") or self.contract.name)

        # ---- 不予受理：超出退货窗口 -----------------------------------------
        if payload.get("within_return_window") is False:
            rationale = [C.cite(C.RULE_OUT_OF_WINDOW, "受理时点已超出合同约定的退货窗口")]
            case = C.update_biz_status(
                store, tenant_id, case_id, "rejected", self.contract.name, invocation_id,
                reason=f"{C.RULE_OUT_OF_WINDOW}：超出退货窗口，不予受理")
            return {
                "case": case, "return_action": "", "rejected": True, "attempt": 0,
                "rationale": rationale, "biz_status": case["biz_status"],
                "invocation_id": invocation_id,
            }

        # ---- 按理由裁定：一案一处置，混合理由不许自动合并 ---------------------
        by_action: dict[str, list[dict]] = {}
        for line in lines:
            reason_code = str(line["reason_code"])
            rule_id = C.REASON_RULE.get(reason_code)
            if rule_id is None:
                raise ValueError(
                    f"退货理由 {reason_code!r} 没有对应的裁定规则；"
                    "有理由无规则等于结论没有出处，这里不许猜")
            action, _why = C.require_rule(rule_id)
            if action not in C.RETURN_ACTIONS:
                raise ValueError(
                    f"规则 {rule_id} 的结论是 {action!r}，不是三种处置之一；"
                    "裁定规则与对账规则共用一张表，取错那一组就会写进 CHECK 拒收的值")
            by_action.setdefault(action, []).append(
                {"line_no": int(line["line_no"]), "reason_code": reason_code,
                 "rule_id": rule_id})

        if len(by_action) > 1:
            raise ValueError(
                f"case={case_id} 的退货行指向多种处置：{sorted(by_action)}。"
                "return_action 是案子头上的一个值，这里不许挑一个当代表 —— 请拆案")

        action = next(iter(by_action))
        rationale = [
            C.cite(item["rule_id"],
                   f"退货行 {item['line_no']}：理由 {item['reason_code']}"
                   f"（{C.require_reason(item['reason_code'])[0]}）")
            for item in next(iter(by_action.values()))
        ]

        requested = str(payload.get("requested_action") or "").strip()
        if requested and requested != action:
            # 诉求与规则不一致：按规则裁，但把这件事记进依据里 ——
            # 静悄悄改掉诉求，申请方拿到结论时不知道自己的诉求被驳了。
            rationale.append({
                "rule_id": rationale[0]["rule_id"],
                "outcome": action,
                "why": "诉求与规则不一致时以规则为准",
                "detail": f"申请方要求 {requested!r}，按规则裁定为 {action!r}",
            })

        attempt = C.next_attempt(store, "rtv_disposition", tenant_id, case_id)
        C.execute(
            store,
            "INSERT OR REPLACE INTO rtv_disposition (tenant_id, case_id, attempt,"
            " return_action, rationale_json, decided_by, decided_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (tenant_id, case_id, attempt, action,
             json.dumps(rationale, ensure_ascii=False, sort_keys=True),
             decided_by, C.now_iso()))
        C.set_return_action(store, tenant_id, case_id, action)

        case = C.update_biz_status(
            store, tenant_id, case_id, "disposed", self.contract.name, invocation_id,
            reason=f"裁定处置类型 {action}（第 {attempt} 次，依据 "
                   f"{sorted({item['rule_id'] for item in rationale})}）")

        return {
            "case": case,
            "return_action": action,
            "rejected": False,
            "attempt": attempt,
            "rationale": rationale,
            "biz_status": case["biz_status"],
            "invocation_id": invocation_id,
        }
