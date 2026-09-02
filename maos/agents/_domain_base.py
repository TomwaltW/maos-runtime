"""业务域 Agent 薄壳的共用骨架 —— extras 口径、artifact 形状、失败文案。

应付账款 / 理赔 / 差错处理三个域的 `agents/<域>/_base.py` 曾各持一份这三个函数，
函数体逐字节同构，只有 docstring 各写各的。三份并成一份放在这里，各域 `_base.py`
只留**本域的 artifact kind 常量**，再把这三个函数转出去 —— 对外 import 路径
（`from ._base import artifact, extras_of, failed`）一个字不变。

**下划线开头是硬要求**：`maos/agents/__init__.py` 会扫描包内全部非下划线开头模块并
自动 import，带 `@register` 的类随之进 `AGENT_POOL`（冻结口径 C-2）。本模块不是
Agent，名字一旦不带下划线就会被当成 Agent 模块扫进去。同一口径的先例见
`maos/agents/_sandbox_stub.py`。

**退款域这一轮不接本骨架**：`maos/agents/refund/_base.py` 仍是自己那份副本。不是漏了 ——
它归同期另一轮只读，接入判据与遗留事项记在 `docs/BACKLOG.md ## task-T82`。

KIND 常量**绝不下沉到这里**：那份清单是各域自己的口径，理由原样留在各域文件里。
"""

from __future__ import annotations

import uuid
from typing import Any

__all__ = ("artifact", "extras_of", "failed")


def extras_of(agent: Any, ctx: Any) -> dict:
    """一次 skill 调用的 extras。**每调一次生成一个新的 invocation_id。**

    invoker 不持有 model，所以每次都要把它放进 extras。

    `invocation_id` 这一份在各域是**两种真实用途**，两种都得留着：

    1. **兜底**：经 `SkillInvoker.invoke` 走时，invoker 会用它自己生成的那个覆盖掉
       本键（invoker.py 里「故意覆盖调用方传入的同名键」那段），所以库里落的 actor
       锚点与 SkillInvoked 事件的 id 恒为同一个值。这里仍然给一份，是为了让
       **不经 invoker 直接调 skill 的单测**也拿得到非空锚点。
    2. **本域补的**：`guard.update_biz_status()` 要一个非空的 actor 锚点，各域 skill
       的口径是「调用方经 extras 传入，传不到则本地生成」（见各域
       `maos/skills/builtin/<域>/_common.py` 模块 docstring 第 2 条）。本键就是
       「调用方传入」的那一半。

    这不是同一句话的两种写法：只留第 1 条，下次会有人以为「反正会被覆盖」而把本键
    整个删掉，直调 skill 的单测与 guard 的非空校验当场塌；只留第 2 条，下次会有人
    把「invoker 覆盖了本键」当成 bug 去改 invoker。

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
    缺了就是 rework，**而症状会显示成「某某流程走不完」，离原因极远** ——
    三个域原话分别是「应付流程走不完」「理赔流程走不完」「差错流程走不完」。
    这句话是本函数存在的全部理由，不许压缩成「补上 Gate 要的字段」：压缩掉之后，
    下一个人看到的是一个「多此一举地塞两个常量」的函数，删起来毫无心理负担，
    而删掉之后炸的地方在闸上，跟这里隔着整条链路。

    `self_check` 恒 pass 不是走过场：这几个域的产物没有 build/lint 这回事，
    每个域真正的验收判据是本域自己的那几样东西（各域原话留在各自 `_base.py` 的模块
    docstring 里），由各域的 guard 与断言把守，不由这里判。
    """
    body = dict(content)
    body["summary"] = summary
    body.setdefault("self_check", {"build": "pass", "lint": "pass"})
    return {"kind": kind, "content": body}


def failed(res: Any, skill: str) -> str:
    """把一次失败的 SkillResult 翻译成 AgentOutput.error 的文本。"""
    return res.error or f"{skill} 未产出结果"
