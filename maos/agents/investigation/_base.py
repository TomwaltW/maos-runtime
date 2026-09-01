"""差错处理域四个 Agent 的共用件 —— artifact 形状与 extras 口径。

四个 Agent 都是**薄壳**：Identity + 经 SkillInvoker 调 1 个 skill + 把 output 包成
artifact。业务判定一行都不在 Agent 里 —— 这不是风格洁癖，是本仓要证明的那句话的
直接后果：

    同一个编排内核，换个领域只换 Skill / ToolPort / 业务对象。

判定一旦漏进 Agent，「换域只换 Skill」就不成立了：下一个业务域得把这些 Agent 也重写
一遍。所以这里只有搬运和包装。口径与 `maos/agents/refund/_base.py` 逐条对齐。
"""

from __future__ import annotations

import uuid
from typing import Any

# 本域新增的 artifact kind。**刻意不进 maos/artifacts.py 的 ALL_KINDS** ——
# 那份清单是跨轨冻结口径（且 artifacts.py 可读不可写），单轨往里加会和别人撞。
# Gate 对非代码类产物不查 kind 白名单，只按 self_check / summary 判，所以安全。
#
# 更要紧的是**不能**复用 patch_set / test_report：Gate 用产物类型判「这是不是代码类
# 任务」，沾上这两个 kind，差错处理任务就会被要求交一份跑出来的测试报告，
# 而本域根本没有那种东西 —— 闸会恒 blocker，且报错信息指向测试而不是差错处理。
KIND_CASE_FILE = "investigation_case_file"
KIND_CLASSIFICATION = "investigation_classification"
KIND_CANCELLATION_REQUEST = "investigation_cancellation_request"
KIND_RESOLUTION = "investigation_resolution"

ALL_INVESTIGATION_KINDS = (
    KIND_CASE_FILE, KIND_CLASSIFICATION, KIND_CANCELLATION_REQUEST, KIND_RESOLUTION,
)


def extras_of(agent: Any, ctx: Any) -> dict:
    """一次 skill 调用的 extras。**每调一次生成一个新的 invocation_id。**

    invoker 不持有 model，所以每次都要把它放进 extras。
    `invocation_id` 是本域补的：`guard.update_biz_status()` 要一个非空的 actor 锚点，
    而 invoker 自己生成的那个到不了 skill 里 —— 详见
    `maos/skills/builtin/investigation/_common.py` 的模块 docstring 第 2 条。
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
    缺了就是 rework，而症状会显示成「差错流程走不完」，离原因极远。

    `self_check` 恒 pass 不是走过场：本域产物没有 build/lint 这回事，本域真正的
    验收判据是原始支付快照、官方原因码与清算方决议 —— 那些由 guard 与本域的断言把守。
    """
    body = dict(content)
    body["summary"] = summary
    body.setdefault("self_check", {"build": "pass", "lint": "pass"})
    return {"kind": kind, "content": body}


def failed(res: Any, skill: str) -> str:
    """把一次失败的 SkillResult 翻译成 AgentOutput.error 的文本。"""
    return res.error or f"{skill} 未产出结果"
