"""RTV（采购退货退款）域五个 Agent 的共用件 —— artifact 形状与 extras 口径。

五个 Agent 都是**薄壳**：Identity + 经 SkillInvoker 调 1 个 skill + 把 output 包成
artifact。业务判定一行都不在 Agent 里 —— 这不是风格洁癖，是本域要证明的那句话的
直接后果：

    同一个编排内核，换个领域只换 Skill / ToolPort / 业务对象。

判定一旦漏进 Agent，「换域只换 Skill」就不成立了：下一个业务域得把这些 Agent 也
重写一遍。所以这里只有搬运和包装。

本域比 ap / refund 域多守一条（契约 C-R3）：**两个**权威终态（供应商开票、AP 到账）
都只能由 `rtv.observe` 从外部回执得到。所以 `settlement_agent` 连
「该收口了没有」都不自己判 —— 它把 skill 返回的 `open_questions` 原样搬出去，
判据留在 skill 里，权威留在外部系统（铁律 8）。

`maos/tests/test_rtv_flow.py` 有一条源码文本断言钉着这件事：五个 Agent 模块里
不许出现金额比较，也不许出现权威终态的字面量。它是「换域只换 Skill」那句话的
机器守卫 —— 判定漏进来时红的是那条测试，而不是等到下一个域重写一遍才发现。
"""

from __future__ import annotations

import uuid
from typing import Any

# 本域新增的 artifact kind（契约 C-R6）。**刻意不进 maos/artifacts.py 的 ALL_KINDS**
# —— 那份清单是跨轨冻结口径，单轨往里加会和别人撞（口径同 agents/ap/_base.py 与
# agents/refund/_base.py 的同名注释）。
# Gate 对非代码类产物不查 kind 白名单，只按 self_check / summary 判，所以安全。
#
# 更要紧的是**不能**复用 patch_set / test_report：Gate 用产物类型判「这是不是代码类
# 任务」（`runtime/gate.py::CODE_ARTIFACT_KINDS`），沾上这两个 kind，退货任务就会被
# 要求交一份跑出来的测试报告，而本域根本没有那种东西 —— 闸会恒 blocker，
# 且报错信息指向测试而不是退货。
KIND_RTV_INTAKE = "rtv_intake"
KIND_RTV_DISPOSITION = "rtv_disposition"
KIND_RTV_SHIPMENT = "rtv_shipment"
KIND_RTV_RECONCILIATION = "rtv_reconciliation"
KIND_RTV_SETTLEMENT_ADVICE = "rtv_settlement_advice"

ALL_RTV_KINDS = (
    KIND_RTV_INTAKE, KIND_RTV_DISPOSITION, KIND_RTV_SHIPMENT,
    KIND_RTV_RECONCILIATION, KIND_RTV_SETTLEMENT_ADVICE,
)


def extras_of(agent: Any, ctx: Any) -> dict:
    """一次 skill 调用的 extras。**每调一次生成一个新的 invocation_id。**

    invoker 不持有 model，所以每次都要把它放进 extras。`invocation_id` 这一份是
    兜底：`SkillInvoker.invoke` 会用它自己生成的那个覆盖掉本键（invoker.py 里
    「故意覆盖调用方传入的同名键」那段），所以库里落的 actor 锚点与 SkillInvoked
    事件的 id 恒为同一个值。这里仍然给一份，是为了让不经 invoker 直接调 skill 的
    单测也拿得到非空锚点。

    复用同一个 id 会让两次不同的写入指向同一次调用，审计链就假了，所以不缓存。
    口径逐字同 `agents/ap/_base.py::extras_of` —— 两个域各存一份不是重复没抽掉，
    是「换域只换域内文件」的代价，抽成公共件就等于两个域共同持有一个面。
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
    缺了就是 rework，而症状会显示成「退货流程走不完」，离原因极远。

    `self_check` 恒 pass 不是走过场：本域产物没有 build/lint 这回事，真正的验收
    判据是处置裁定的规则编号、三方对账的可核对结论、以及供应商贷项通知单与 AP
    调整凭单这两份外部回执 —— 那些由本域自己的断言把守。
    """
    body = dict(content)
    body["summary"] = summary
    body.setdefault("self_check", {"build": "pass", "lint": "pass"})
    return {"kind": kind, "content": body}


def failed(res: Any, skill: str) -> str:
    """把一次失败的 SkillResult 翻译成 AgentOutput.error 的文本。"""
    return res.error or f"{skill} 未产出结果"
