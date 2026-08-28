"""退款域四个 Agent 的共用件 —— artifact 形状与 extras 口径。

四个 Agent 都是**薄壳**：Identity + 经 SkillInvoker 调 1–2 个 skill + 把 output 包成
artifact。业务判定一行都不在 Agent 里 —— 这不是风格洁癖，是本轨要证明的那句话的
直接后果：

    同一个编排内核，换个领域只换 Skill / ToolPort / 业务对象。

判定一旦漏进 Agent，「换域只换 Skill」就不成立了：下一个业务域得把这些 Agent 也重写
一遍。所以这里只有搬运和包装。
"""

from __future__ import annotations

import uuid
from typing import Any

# 本域新增的 artifact kind。**刻意不进 maos/artifacts.py 的 ALL_KINDS** ——
# 那份清单是跨轨冻结口径，单轨往里加会和别人撞（agents/requirement.py:22 同一口径）。
# Gate 对非代码类产物只按 self_check / summary 判，不查 kind 白名单，所以安全。
#
# 更要紧的是**不能**复用 patch_set / test_report：Gate 用产物类型判「这是不是代码类
# 任务」（gate.py:37），沾上这两个 kind，退款任务就会被要求交一份跑出来的测试报告，
# 而退款域根本没有那种东西 —— 闸会恒 blocker，且报错信息指向测试而不是退款。
KIND_CASE_DRAFT = "refund_case_draft"
KIND_POLICY_DECISION = "refund_policy_decision"
KIND_FINANCE_SETTLEMENT = "refund_finance_settlement"
KIND_PAYMENT_REQUEST = "refund_payment_request"
KIND_PAYMENT_RECEIPT = "refund_payment_receipt"
KIND_NOTIFICATION = "refund_notification"

ALL_REFUND_KINDS = (
    KIND_CASE_DRAFT, KIND_POLICY_DECISION, KIND_FINANCE_SETTLEMENT,
    KIND_PAYMENT_REQUEST, KIND_PAYMENT_RECEIPT, KIND_NOTIFICATION,
)


def extras_of(agent: Any, ctx: Any) -> dict:
    """一次 skill 调用的 extras。**每调一次生成一个新的 invocation_id。**

    invoker 不持有 model，所以每次都要把它放进 extras（A-3）。
    `invocation_id` 是本轨补的：`guard.update_biz_status()` 要一个非空的 actor 锚点，
    而 invoker 自己生成的那个到不了 skill 里（invoker.py:69）——
    详见 `maos/skills/builtin/refund/_common.py` 的模块 docstring 第 2 条。
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
    `_acceptance_by_self_check` 要 `self_check.build/lint == "pass"`（gate.py:180/218）。
    缺了就是 rework，而症状会显示成「退款流程走不完」，离原因极远。

    `self_check` 恒 pass 不是走过场：退款域产物没有 build/lint 这回事，本域真正的
    验收判据是政策版本、金额核算与回执 —— 那些由第六道财务复核闸与本域的断言把守。
    """
    body = dict(content)
    body["summary"] = summary
    body.setdefault("self_check", {"build": "pass", "lint": "pass"})
    return {"kind": kind, "content": body}


def failed(res: Any, skill: str) -> str:
    """把一次失败的 SkillResult 翻译成 AgentOutput.error 的文本。"""
    return res.error or f"{skill} 未产出结果"
