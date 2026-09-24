"""cs.answer —— 客服前台一轮的「检索 + 组稿 + 后置校验」（p12 契约 §1.4 第 4、6 步 / §1.7）。

入参 ``{tenant_id, conversation_id, turn_id, text}``；出参
``{draft, check, hits: [doc_id…], route, intent, handoff_reason}``：

* 检索走 ``maos.domain.cs.scripts.match_scripts``，plan_id / task_id 用 ``ctx.extras`` 里
  前台给的那一对（缺了才按会话现算）—— 恰好落一条 ``KbRetrieved``，客户原文在审计行里
  只有摘要；
* ``hits[0].score >= MIN_SCRIPT_SCORE`` 才算命中：
  - 带转人工标记（needs_order_lookup）→ ``route=handoff``、回该篇的标准话术（它本身就是
    过渡话术）；
  - 不带标记 → ``route=answer``、回标准话术；
  两种都 ``citations = (该篇 doc_id,)``；
* 没命中 → ``route=fallback``、意图 unknown、回前台的兜底话术；
* 组好的稿子过 ``claims.check_reply``：observations 恒为空（p12 没有观察来源），
  kb_doc_ids 是从 event_log **读回**的本轮命中（``claims.turn_kb_doc_ids``），不信
  本函数手里那份 —— 审计行里没有的命中，引用了也不算数。

不在这里决定「校验没过怎么办」「连续兜底要不要转人工」：那是前台编排的事
（``maos.domain.cs.desk``），本 skill 只如实报出这一版稿子与它的校验结果。
"""

from __future__ import annotations

from typing import Any

from maos.domain.cs import claims, scripts
from maos.domain.cs.desk import REPLY_FALLBACK
from maos.domain.cs.types import (
    INTENT_UNKNOWN,
    ROUTE_ANSWER,
    ROUTE_FALLBACK,
    ROUTE_HANDOFF,
    ReplyDraft,
    plan_id_for,
)
from maos.skills.contract import Skill, SkillContext, SkillContract
from maos.skills.registry import register_skill


def compose(hits: list, *, min_score: float) -> tuple[str, str, str, ReplyDraft]:
    """按契约 §1.4 第 4 步把检索结果组成一版稿子：``(route, intent, handoff_reason, draft)``。"""
    top = hits[0] if hits and hits[0].score >= min_score else None
    if top is None:
        return ROUTE_FALLBACK, INTENT_UNKNOWN, "", ReplyDraft(text=REPLY_FALLBACK)
    draft = ReplyDraft(text=top.script, citations=(top.doc_id,))
    if top.handoff:
        return ROUTE_HANDOFF, top.intent, top.handoff, draft
    return ROUTE_ANSWER, top.intent, "", draft


@register_skill
class CsAnswerSkill(Skill):
    contract = SkillContract(
        name="cs.answer",
        version="1.0.0",
        purpose="客服前台一轮的检索 + 组稿 + 后置校验：按客户原文检索话术库（kind=cs_script），"
                "命中就照标准话术组稿（带引用），没命中给兜底话术，再过确定性后置校验",
        input_schema={
            "tenant_id": "str —— 租户（客服账号映射得到，非空）",
            "conversation_id": "str —— 会话 id（csc-…）",
            "turn_id": "str —— 本轮 id（<会话>-tNNNN）",
            "text": "str —— 客户本轮原文（只用于检索，审计行里只落摘要）",
        },
        output_schema={
            "draft": "ReplyDraft.to_json() —— 这一版回复（text / claims / citations）",
            "check": "CheckResult.to_json() —— 空观察下的后置校验结果",
            "hits": "list[str] —— 本次检出的话术 doc_id（按分数降序）",
            "route": "answer | fallback | handoff",
            "intent": "str —— 命中话术的意图；没命中为 unknown",
            "handoff_reason": "str —— route=handoff 时为话术的转人工标记，否则空串",
        },
        preconditions=["tenant_id", "conversation_id", "turn_id", "text"],
        depends_tools=[],
        failure_policy="escalate",
        max_retries=0,
        security_boundary="只读、只落 cs_ 表、不调任何工具：只检索 kind=cs_script 的话术、"
                          "只从 event_log 读回本轮命中；不写任何业务表（唯一的写是检索落的一条 "
                          "KbRetrieved 审计行，客户原文只落摘要）；不调模型、不查单，"
                          "不碰审批 / 放款 / 补偿 / 工单任何一条路。失败直接上报不重试："
                          "重试会重复落 KbRetrieved。",
        reuse_note="检索口径在 maos/domain/cs/scripts.py，校验口径在 maos/domain/cs/claims.py；"
                   "本 skill 只把两者按契约 §1.4 的判定顺序接起来",
        owner_roles=[],
    )

    def run(self, payload: dict, ctx: SkillContext) -> Any:
        store = ctx.store
        tenant_id = str(payload["tenant_id"])
        conversation_id = str(payload["conversation_id"])
        turn_id = str(payload["turn_id"])
        text = str(payload["text"])
        extras = ctx.extras or {}
        plan_id = str(extras.get("plan_id") or plan_id_for(conversation_id))
        task_id = str(extras.get("task_id") or turn_id)

        hits = scripts.match_scripts(store, tenant_id=tenant_id, text=text,
                                     plan_id=plan_id, task_id=task_id)
        route, intent, reason, draft = compose(hits, min_score=scripts.MIN_SCRIPT_SCORE)
        check = claims.check_reply(
            draft, observations=frozenset(),
            kb_doc_ids=claims.turn_kb_doc_ids(store, conversation_id=conversation_id,
                                              turn_id=turn_id))
        return {
            "draft": draft.to_json(),
            "check": check.to_json(),
            "hits": [h.doc_id for h in hits],
            "route": route,
            "intent": intent,
            "handoff_reason": reason,
        }
