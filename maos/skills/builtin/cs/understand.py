"""cs.understand —— 客服前台一轮的「语种 + 槽位 + 意图」（p13 契约 §1.4 T173、§2 第 0 / 4 步）。

入参 ``{tenant_id, conversation_id, turn_id, text, prior_slots}``（``prior_slots`` 可缺省，缺省为空）；
出参是 :class:`maos.domain.cs.understand.Understanding` 的 JSON：``{lang, intent, slots, source}``。

* **确定性优先**：语种、槽位、意图都先按规则判（触发词 → 本轮诉求 → 意图示例 → 关键词词表）；
* 只在规则判不出意图、且 ``ctx.extras["model"]`` 是**真模型**（非 None、非 ScriptedModelClient）
  时调一次模型，输出夹到 ``types.INTENTS``。调了就记账：``model_usage`` / ``model_call_failure``
  一行，``trace_id=""``、``task_id=None``、``plan_id=extras["plan_id"]``（照契约原样取；extras 里
  没有就是空串，与同一次调用的 SkillInvoked 行一致 —— 不替调用方现算，漏接线要看得出来，复核 L1-1）。
  记账发生在 :func:`maos.domain.cs.understand.understand` 里（契约把 model / store / plan_id 给了它），
  call_site 是那个模块的 ``CALL_SITE``；本 skill 只是把 extras 接过去。
* Scripted / None 下一次模型都不调，零 ``model_usage`` 行。

只读：除模型账外不写任何表，不落事件（槽位进 ``cs_slot`` 是前台的事，那里也只落到 cs_ 表）；
客户原文、槽位值不进 event_log —— SkillInvoked 只有入参 / 出参的摘要（invoker 的口径）。
"""

from __future__ import annotations

from typing import Any

from maos.domain.cs import understand as cs_understand
from maos.skills.contract import Skill, SkillContext, SkillContract
from maos.skills.registry import register_skill


@register_skill
class CsUnderstandSkill(Skill):
    contract = SkillContract(
        name="cs.understand",
        version="1.0.0",
        purpose="客服前台一轮的理解：判语种、抽槽位（订单号 / 商品 / 问题 / 诉求 / 情绪，跨轮合并）、"
                "判意图（触发词 → 本轮诉求 → 意图示例 → 关键词词表；判不出且注入真模型才问模型）",
        input_schema={
            "tenant_id": "str —— 租户（客服账号映射得到）",
            "conversation_id": "str —— 会话 id（csc-…）",
            "turn_id": "str —— 本轮 id（<会话>-tNNNN）",
            "text": "str —— 客户本轮原文（只用于理解，审计行里只有摘要）",
            "prior_slots": "dict[str, str]? —— 会话已有的槽位（cs_slot 读回），缺省为空",
        },
        output_schema={
            "lang": "zh | en",
            "intent": "str —— types.INTENTS 之一（判不出为 unknown）",
            "slots": "dict[str, str] —— 合并后的全量槽位，键取自 ports.SLOT_KEYS",
            "source": "rule | model —— 意图从哪来",
        },
        preconditions=["tenant_id", "conversation_id", "turn_id", "text"],
        depends_tools=[],
        failure_policy="escalate",
        max_retries=0,
        security_boundary="只读、不调任何工具、不写任何业务表、不落事件：槽位只从词表与正则来"
                          "（不自由抽取，客户的住址、手机号进不了槽位），手机号（含连字符与 +86 写法）"
                          "与 400 / 800 热线不当单号，紧跟在卡号 / QQ / 身份证 / 电话字眼后面的号、"
                          "没有单号字眼的身份证号与银行卡号形态也不当单号；"
                          "只在规则判不出意图且注入真模型时调一次模型，唯一的写是那一行模型账；"
                          "模型出错记一行失败、按规则结果返回，不重试（重试会重复记账）。"
                          "不碰审批 / 放款 / 补偿 / 工单 / 查单任何一条路。",
        reuse_note="规则与词表在 maos/domain/cs/understand.py（语种在 lang.py，触发词在 triggers.py，"
                   "时长 / 进度线索在 scripts.py）；本 skill 只把前台给的 extras 接过去",
        owner_roles=[],
    )

    def run(self, payload: dict, ctx: SkillContext) -> Any:
        extras = ctx.extras or {}
        plan_id = str(extras.get("plan_id") or "")
        prior = payload.get("prior_slots") or {}
        if not isinstance(prior, dict):
            raise ValueError("cs.understand 的 prior_slots 必须是 dict")
        result = cs_understand.understand(
            str(payload["text"]),
            prior_slots={str(k): str(v) for k, v in prior.items()},
            model=extras.get("model"), store=ctx.store, plan_id=plan_id)
        return result.to_json()
