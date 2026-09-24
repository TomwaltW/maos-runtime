"""cs.handoff —— 转人工卡片落库（p12 契约 §1.4 / §1.7）。

入参 ``{card: HandoffCard.to_json(), delivery: pending | unconfigured, now?}``，调
``maos.domain.cs.conversation.record_handoff``：插一行 ``cs_handoff``、落恰好一条
``CsHandoffRaised``（审计行只带摘要、枚举、doc_id 与计数，客户原文只住在卡片里）。
出参 ``{handoff_id, delivery}``。

卡片的**投递**不在这里：投到哪个内部房间、投没投成，是 router 在同进程里做的事
（``IngressRouter._cs_deliver``），做完回写 ``delivery``。本 skill 只落「这一轮要人接手」
这件事实 —— 投递失败时卡片照样在库里，人工能从 ``list_handoffs`` 捞回来。
"""

from __future__ import annotations

import json
from typing import Any

from maos.domain.cs import conversation
from maos.domain.cs.types import DELIVERY_PENDING, HandoffCard
from maos.skills.contract import Skill, SkillContext, SkillContract
from maos.skills.registry import register_skill


@register_skill
class CsHandoffSkill(Skill):
    contract = SkillContract(
        name="cs.handoff",
        version="1.0.0",
        purpose="客服前台转人工：把一张带齐上下文的转人工卡片落进 cs_handoff，"
                "并落一条 CsHandoffRaised 审计行",
        input_schema={
            "card": "HandoffCard.to_json() —— handoff_id 必须等于 turn_id（一轮至多一张卡）",
            "delivery": "pending（配了投递目标）| unconfigured（没配，只落库）",
            "now": "str，可选 —— 时间戳（测试注入用）",
        },
        output_schema={
            "handoff_id": "str —— 与本轮 turn_id 同值",
            "delivery": "str —— 落库时的投递状态",
        },
        preconditions=["card", "delivery"],
        depends_tools=[],
        failure_policy="escalate",
        max_retries=0,
        security_boundary="只读、只落 cs_ 表、不调任何工具：唯一的写是 cs_handoff 一行 + 一条 "
                          "CsHandoffRaised 审计行（只带摘要与枚举，不带客户原文与客户标识）；"
                          "不调模型、不查单，不碰审批 / 放款 / 补偿 / 工单任何一条路。"
                          "失败直接上报不重试：重试会撞同一轮卡片的主键、重复落事件。",
        reuse_note="会话表口径在 maos/domain/cs/conversation.py（T167）；本 skill 只是经 "
                   "SkillInvoker 调它，让转人工这一步也落一条 SkillInvoked",
        owner_roles=[],
    )

    def run(self, payload: dict, ctx: SkillContext) -> Any:
        raw = payload["card"]
        card = HandoffCard.from_json(json.loads(raw) if isinstance(raw, str) else dict(raw))
        delivery = str(payload.get("delivery") or DELIVERY_PENDING)
        now = payload.get("now")
        conversation.record_handoff(ctx.store, card, delivery=delivery,
                                    now=str(now) if now else None)
        return {"handoff_id": card.handoff_id, "delivery": delivery}
