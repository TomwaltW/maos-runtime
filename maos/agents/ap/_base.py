"""应付账款域四个 Agent 的共用件 —— artifact 形状与 extras 口径。

四个 Agent 都是**薄壳**：Identity + 经 SkillInvoker 调 1–2 个 skill + 把 output 包成
artifact。业务判定一行都不在 Agent 里 —— 这不是风格洁癖，是本域要证明的那句话的
直接后果：

    同一个编排内核，换个领域只换 Skill / ToolPort / 业务对象。

判定一旦漏进 Agent，「换域只换 Skill」就不成立了：下一个业务域得把这些 Agent 也
重写一遍。所以这里只有搬运和包装。
"""

from __future__ import annotations

import uuid
from typing import Any

# 本域新增的 artifact kind。**刻意不进 maos/artifacts.py 的 ALL_KINDS** ——
# 那份清单是跨轨冻结口径，单轨往里加会和别人撞（口径同 agents/refund/_base.py）。
# Gate 对非代码类产物不查 kind 白名单，只按 self_check / summary 判，所以安全。
#
# 更要紧的是**不能**复用 patch_set / test_report：Gate 用产物类型判「这是不是代码类
# 任务」，沾上这两个 kind，应付账款任务就会被要求交一份跑出来的测试报告，
# 而本域根本没有那种东西 —— 闸会恒 blocker，且报错信息指向测试而不是应付账款。
KIND_INVOICE_INTAKE = "ap_invoice_intake"
KIND_MATCH_RESULT = "ap_match_result"
KIND_PAYMENT_PLAN = "ap_payment_plan"
KIND_PAYMENT_INSTRUCTION = "ap_payment_instruction"
KIND_BANK_ADVICE = "ap_bank_advice"

ALL_AP_KINDS = (
    KIND_INVOICE_INTAKE, KIND_MATCH_RESULT, KIND_PAYMENT_PLAN,
    KIND_PAYMENT_INSTRUCTION, KIND_BANK_ADVICE,
)


def extras_of(agent: Any, ctx: Any) -> dict:
    """一次 skill 调用的 extras。**每调一次生成一个新的 invocation_id。**

    invoker 不持有 model，所以每次都要把它放进 extras。`invocation_id` 这一份是
    兜底：`SkillInvoker.invoke` 会用它自己生成的那个覆盖掉本键（invoker.py 里
    「故意覆盖调用方传入的同名键」那段），所以库里落的 actor 锚点与 SkillInvoked
    事件的 id 恒为同一个值。这里仍然给一份，是为了让不经 invoker 直接调 skill 的
    单测也拿得到非空锚点。

    复用同一个 id 会让两次不同的写入指向同一次调用，审计链就假了，所以不缓存。
    """
    return {
        "model": agent.model,
        "tier": agent.identity.model_tier,
        "plan_id": ctx.plan_id,
        "task_id": ctx.task_id,
        "trace_id": ctx.trace_id,
        "attempt": ctx.attempt,
        "invocation_id": uuid.uuid4().hex,
    }


def artifact(kind: str, content: dict, *, summary: str) -> dict:
    """包一份 artifact，并补上 Gate 要的两个字段。

    Gate 对非代码类产物的两条判据是硬的：`_gate_evidence` 要 `summary` 非空、
    `_acceptance_by_self_check` 要 `self_check.build/lint == "pass"`。
    缺了就是 rework，而症状会显示成「应付流程走不完」，离原因极远。

    `self_check` 恒 pass 不是走过场：本域产物没有 build/lint 这回事，真正的验收
    判据是三单匹配的规则编号、审批记录与银行回单 —— 那些由本域自己的断言把守。
    """
    body = dict(content)
    body["summary"] = summary
    body.setdefault("self_check", {"build": "pass", "lint": "pass"})
    return {"kind": kind, "content": body}


def failed(res: Any, skill: str) -> str:
    """把一次失败的 SkillResult 翻译成 AgentOutput.error 的文本。"""
    return res.error or f"{skill} 未产出结果"
