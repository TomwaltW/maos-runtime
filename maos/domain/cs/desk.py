"""客服前台编排（p12 跨轨契约 §1.4 T169）：外部渠道的一句话 → 一轮会话。

一轮严格按契约的判定顺序走（:meth:`FrontDesk.handle`）：

0. 取租户（``open_kfid`` 取自 ``msg.raw``，映射不到就是空串、不猜默认租户）→ 取或建会话 →
   取号 → 本轮归属 ``extras = {plan_id: plan_id_for(会话), task_id: 轮次, trace_id: ""}``；
1. 会话已转人工 → ``silent``：只记录，不回话、不出卡（意图 unknown）；
2. 租户为空 → ``handoff`` / ``tenant_unmapped``，**不检索**（意图 unknown）；
3. 触发词（:mod:`maos.domain.cs.triggers`）→ ``handoff`` / 该原因；
4. 经 ``SkillInvoker(CS_FRONT_DESK_IDENTITY).invoke("cs.answer")`` 检索 + 组稿 + 后置校验：
   命中且带转人工标记 → ``handoff`` / 该标记；命中 → ``answer``；没命中 → ``fallback``
   （检索只看 :func:`retrieval_query`：去掉长数字串、截到 ``MAX_QUERY_CHARS`` 字）；
5. 本轮兜底使 ``fallback_streak`` 达到 ``FALLBACK_STREAK_HANDOFF`` → ``handoff`` /
   ``repeated_fallback``（意图 unknown）；
6. 出门的那版回复没过 ``claims.check_reply`` → 落 ``CsReplyRejected``，改发兜底 +
   ``handoff`` / ``unverified_claim``。

走 ``handoff`` 的轮：组一张 :class:`HandoffCard`（最近几轮含本轮、客户标识打码、处理建议
写清要人做什么），经 ``cs.handoff`` 落库（没配投递目标 ``delivery=unconfigured``，否则
``pending``，真投递是 router 的 ``_cs_deliver``），再把会话阶段 ``active → handed_off``。
每轮最后 ``record_turn``，恰好一条 ``CsTurnRecorded``。卡片渲染成文字时（:func:`render_card_text`），
客户说的话压成一行、平台标记字符换成全角 —— 客户造不出一行假的「处理建议」，也 @ 不了全员。

## 前台说不出任何状态

p12 没有观察来源（不查单、不读支付观察），``check_reply`` 的 observations 恒为空，于是
这里的每一句固定话术也要在空观察下过校验：不说「已发货 / 已到账」，不承诺时限、金额、
结果，不露内部口径（斜杠写法、内部岗位名、规则编号、系统名）。赔偿类的过渡话术连
「赔偿 / 补偿」两个词都不出现 —— 说出来就是在接对方的诉求。测试逐句过真的校验器。

## 永不抛

``handle`` 里任何异常都收成「记日志（客户标识打码）+ 出错话术 + ``handoff``」，并尽量把
卡片与轮次落下（落不下也不抛）：webhook 循环里抛出去，平台收不到 200 会无限重推。

## 零授权

身份 :data:`CS_FRONT_DESK_IDENTITY` 只持 ``cs.answer`` / ``cs.handoff`` / ``cs.understand`` 三个
skill、零工具、最高风险 L。前台不 import 审批、放款、补偿、查单的任何一条路（静态守卫
``maos/tests/test_cs_guard_t170.py`` 全仓扫）。

## p13：单工作流（review/p13-cs-contracts.md §2，T174）

``FrontDesk(store, config, *, clock=None, verifier=None, lookup=None, precheck=None, model=None)``。
三个端口（``ports.IdentityVerifier`` / ``OrderLookup`` / ``RefundPrecheck``）由装配处**注入**，
前台自己一个工具都不碰。一轮按契约 §2 走：

0. 语种 :func:`~maos.domain.cs.lang.detect_lang`，``DeskResult.lang`` 照填；
1–3. 同 p12（已转人工 → silent；租户空 → tenant_unmapped；触发词）；
4. 理解：经 ``SkillInvoker`` 调 ``cs.understand``（extras 带 plan_id / task_id / trace_id="" 与
   model），合并后的槽位里变了的写进 ``cs_slot``（跨轮累积，新值覆盖旧值）；
5. **三个端口全为 None** → 不调理解、照 p12 的第 4–6 步走，给客户的固定话术也照 p12（中文）——
   p12 的开发集、留出集与全部 p12 测试逐字节不变（DECISIONS task-t174）；
6. 「要看具体订单」（:func:`order_need`：本轮诉求是 track / refund / return / exchange，或本轮
   有进度线索、或本轮只补了单号而会话里的诉求是这几种之一）：缺单号 → ``clarify`` 追问
   （同一槽位已追问 ``MAX_ASKS_PER_SLOT`` 次仍缺 → needs_order_lookup）；身份核验不过 →
   identity_unverified（**不查单**）；查单 ok 且状态在措辞表 → 落 ``cs_observation`` →
   ``answer``，正文就是措辞表那一句、claim 挂 ``obs:<本轮观察 id>``；amended / 平台不映射 →
   order_unmapped；其余 → lookup_failed。退款 / 退货且查单成功 → 预检 → 落 ``cs_refund_bridge``：
   ok → refund_request（卡片带预检摘要与一行现成命令，由内部同事以自己的名义发出）；不 ok →
   needs_order_lookup（卡片写拒绝原因）。换货查单成功 → needs_order_lookup（卡片带观察）；
7. 其余同 p12（检索时把理解出的意图当 ``intent_hint``）；英文的政策问题不检中文话术，回英文兜底；
8. 出门前两道校验（观察与命中一律**从库里读回**）：``check_reply`` 与
   ``check_observation_wording``，任一不过 → ``CsReplyRejected`` + 兜底 + unverified_claim；
9. 每轮 ``record_turn``（lang、lookup_outcome）+ ``record_turn_ext``（再加 ask_slot、ask_count）。

给客户的话一个状态字都不说，唯一的例外是第 6 步那三句（措辞表、挂本轮观察）。订单号、
query_key、槽位值不进日志与 event_log（R5）。

## p15：判定顺序（review/p15-cs-contracts.md §1 C1–C4，T184）

只动注入了端口的路径（p12 路径逐字节不变，测试按基线指纹钉）：

* C4 最先判：假设 / 泛问句（:func:`hypothetical_question`）不进查单、不追问，走政策检索；
* C1 / C2：:func:`order_need` 认中文「查物流一类」与英文订单状态问法（:func:`asks_order_status`，
  英文按词认）→ track；检索落到「看这一单」的查单篇（:data:`ORDER_VIEW_SCHEMES`）时也改走查单分支，
  不凭话术篇的转人工标记直接 needs_order_lookup；
* C3：上一轮在追问单号，本轮只回了单号、或仍没给但仍在说这一单（:func:`still_on_order`）→ 查单分支
  （追问没用尽再问，用尽 needs_order_lookup）。触发词仍在这几处之前；连续兜底规则不动。
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Callable, Mapping

from maos.agents.base import AgentIdentity
from maos.domain.cs import claims, conversation, objects, records, scripts
from maos.domain.cs import understand as cs_understand
from maos.domain.cs import lang as cs_lang
from maos.domain.cs.lang import detect_lang
from maos.domain.cs.ports import (
    LANG_EN,
    LANG_ZH,
    LOOKUP_AMENDED,
    LOOKUP_MISCONFIGURED,
    LOOKUP_OK,
    LOOKUP_OUTCOMES,
    LOOKUP_PLATFORM_ERROR,
    LOOKUP_UNMAPPED,
    MAX_ASKS_PER_SLOT,
    ORDER_STATUS_WORDING,
    REQUEST_EXCHANGE,
    REQUEST_REFUND,
    REQUEST_RETURN,
    REQUEST_TRACK,
    SLOT_KEYS,
    SLOT_ORDER_NO,
    SLOT_PROBLEM,
    SLOT_REQUEST,
    SLOT_SOURCE_RULE,
    LookupResult,
    PrecheckResult,
)
from maos.domain.cs.triggers import detect
from maos.domain.cs.types import (
    BASIS_OBS,
    CARD_RECENT_TURNS,
    CHANNEL_WECHAT_KF,
    DELIVERY_PENDING,
    DELIVERY_UNCONFIGURED,
    FALLBACK_STREAK_HANDOFF,
    HANDOFF_ANGER,
    HANDOFF_COMPENSATION,
    HANDOFF_COMPLAINT,
    HANDOFF_IDENTITY_UNVERIFIED,
    HANDOFF_LOOKUP_FAILED,
    HANDOFF_NEEDS_ORDER_LOOKUP,
    HANDOFF_ORDER_UNMAPPED,
    HANDOFF_PRIVACY,
    HANDOFF_REASONS,
    HANDOFF_REFUND_REQUEST,
    HANDOFF_REPEATED_FALLBACK,
    HANDOFF_REQUESTED,
    HANDOFF_TENANT_UNMAPPED,
    HANDOFF_UNVERIFIED_CLAIM,
    INTENT_LOGISTICS,
    INTENT_REFUND_PAYMENT,
    INTENT_RETURN_EXCHANGE,
    INTENT_UNKNOWN,
    INTENTS,
    ROUTE_ANSWER,
    ROUTE_CLARIFY,
    ROUTE_FALLBACK,
    ROUTE_HANDOFF,
    ROUTE_SILENT,
    STAGE_ACTIVE,
    STAGE_HANDED_OFF,
    CheckResult,
    Claim,
    DeskResult,
    HandoffCard,
    ReplyDraft,
    Violation,
    mask_customer,
    plan_id_for,
)
from maos.ingress.contracts import InboundMessage
from maos.model.client import Tier
from maos.skills.invoker import SkillInvoker

log = logging.getLogger("maos.cs")

# ---------------------------------------------------------------------------
# 身份
# ---------------------------------------------------------------------------
#: 前台这一个身份的 skill 名。identity 里仍写字面量（静态守卫要能直接求值）。
SKILL_ANSWER = "cs.answer"
SKILL_HANDOFF = "cs.handoff"
SKILL_UNDERSTAND = "cs.understand"

#: 客服前台的身份。**最小授权**：三个只读 / 只落 cs_ 表的 skill、零工具、风险上限 L。
#: 不进 AGENT_POOL（不是可被派单的岗位），口径同 ``outcome_commands.TICKET_DESK_IDENTITY``。
#: 查单与预检不是 skill、也不是前台的工具：它们是装配处注入的端口（p13 契约 §1.2）。
CS_FRONT_DESK_IDENTITY = AgentIdentity(
    agent_id="cs-front-desk",
    role="cs_front_desk",
    duty="外部渠道客服前台：按话术库答政策问题、答不上就兜底、该转人工就出卡片；"
         "只读、只转述、只转人工，不碰钱、审批、工单",
    allowed_skills=frozenset({"cs.answer", "cs.handoff", "cs.understand"}),
    allowed_tools=frozenset(),
    write_scope=frozenset(),
    max_risk="L",
    model_tier=Tier.LIGHT,
)

# ---------------------------------------------------------------------------
# 给客户的固定话术（每句都在空观察下过 check_reply，测试逐句钉）
# ---------------------------------------------------------------------------
#: 知识不足时的兜底：不编、请客户换个说法。
REPLY_FALLBACK = ("抱歉，这个问题我暂时没能找到准确的答案。"
                  "您可以换个说法描述一下，或者告诉我您想咨询的具体事项。")

#: 通用的转人工过渡话术（原因没有专门话术时用）。
REPLY_HANDOFF = "好的，我这边已为您转接人工客服，请您稍候，人工客服会继续和您沟通。"

#: 按转人工原因的过渡话术。needs_order_lookup 不在表里：它回话术库那篇的标准话术。
REPLY_BY_REASON: Mapping[str, str] = MappingProxyType({
    HANDOFF_REQUESTED: "好的，我这边已为您转接人工客服，请您稍候，人工客服接入后会和您继续沟通。",
    HANDOFF_COMPLAINT: ("非常抱歉给您带来了不好的体验。您反映的情况我这边已为您转接人工客服，"
                        "请您稍候，人工客服会认真了解并跟进。"),
    HANDOFF_ANGER: ("非常抱歉让您不愉快了。我这边已为您转接人工客服，"
                    "请您稍候，人工客服会认真听您说明情况。"),
    HANDOFF_COMPENSATION: ("您提出的诉求需要人工客服结合具体情况和您沟通，"
                           "我这边已为您转接人工客服，请您稍候。"),
    HANDOFF_PRIVACY: ("涉及个人信息的问题需要由人工客服为您处理，请不要在对话中发送证件号码等敏感信息。"
                      "我这边已为您转接人工客服，请您稍候。"),
    HANDOFF_REPEATED_FALLBACK: ("抱歉，我暂时没能理解您的问题，"
                                "我这边已为您转接人工客服，请您稍候，人工客服会继续为您解答。"),
    HANDOFF_UNVERIFIED_CLAIM: ("抱歉，这个问题需要人工客服为您确认，"
                               "我这边已为您转接人工客服，请您稍候。"),
    HANDOFF_TENANT_UNMAPPED: ("您好，当前咨询暂时无法为您自动解答，"
                              "我这边已为您转接人工客服，请您稍候。"),
    # p13 骨架补的四条（review/p13-cs-contracts.md §1.1）；措辞由 T174 接手时可再打磨，
    # 但照样要在空观察下过 check_reply、不说任何状态。
    HANDOFF_IDENTITY_UNVERIFIED: ("为了保护您的订单信息，这一单需要人工客服先核实您的身份，"
                                  "我这边已为您转接人工客服，请您稍候。"),
    HANDOFF_ORDER_UNMAPPED: ("您这一单的情况需要人工客服进一步核实，"
                             "我这边已为您转接人工客服，请您稍候。"),
    HANDOFF_LOOKUP_FAILED: ("抱歉，暂时没能查到这一单的信息，"
                            "我这边已为您转接人工客服，请您稍候。"),
    HANDOFF_REFUND_REQUEST: ("您的诉求我这边已整理好转交售后专员，"
                             "稍后会有同事与您联系，请您稍候。"),
})

#: 前台内部出错、但转人工卡片已落下时的话术。
REPLY_INTERNAL_ERROR = "抱歉，刚才没能处理好您的消息，我这边已为您转接人工客服，请您稍候。"

#: 前台内部出错、连卡片都没落下时的话术（router 兜底也用它）：不说「已转接」—— 没转成。
REPLY_DESK_UNAVAILABLE = "抱歉，刚才没能处理好您的消息，请您稍后再发一次，或者直接回复「人工」。"

#: p13：追问订单号（route=clarify）。
REPLY_ASK_ORDER_NO = "为了帮您查询这一单，请告诉我您的订单号，可以在订单详情页找到。"

#: p13：注入了端口、要看具体订单却办不下去时的 needs_order_lookup 过渡话术（追问两次仍缺单号、
#: 退款预检没过、换货）。p12 路径里这个原因回的是话术库那篇的标准话术，不用它。
REPLY_NEEDS_ORDER_LOOKUP = "您这一单的诉求需要人工客服进一步处理，我这边已为您转接人工客服，请您稍候。"

# ---- 英文固定话术（p13 契约 §2 第 0 步：中英各一套；只在注入了端口时用，见模块头第 5 步）----
#: 每一句同样在空观察下过（开了英文扫描的）check_reply：不说 shipped / refunded / cancelled /
#: delivered / paid 之类的状态，不承诺时间、金额、结果，不露斜杠写法、内部岗位名、规则编号。
REPLY_FALLBACK_EN = ("Sorry, I could not find an accurate answer to this question. Could you "
                     "describe it in another way, or tell me what you would like to ask about?")
REPLY_HANDOFF_EN = ("Sure, I am transferring you to a human agent. Please hold on, and a colleague "
                    "will continue the conversation with you.")
REPLY_BY_REASON_EN: Mapping[str, str] = MappingProxyType({
    HANDOFF_REQUESTED: ("Sure, I am transferring you to a human agent. Please hold on, and a "
                        "colleague will join the conversation."),
    HANDOFF_COMPLAINT: ("We are very sorry for this experience. I am transferring you to a human "
                        "agent, who will look into what you described. Please hold on."),
    HANDOFF_ANGER: ("I am sorry this has upset you. I am transferring you to a human agent, who "
                    "will listen to your concerns. Please hold on."),
    HANDOFF_COMPENSATION: ("Your request needs a human agent to discuss the details with you. "
                           "I am transferring you to a human agent, please hold on."),
    HANDOFF_PRIVACY: ("Questions about personal information need to be handled by a human agent. "
                      "Please do not send identity numbers or other sensitive details in this chat. "
                      "I am transferring you to a human agent, please hold on."),
    HANDOFF_NEEDS_ORDER_LOOKUP: ("This order needs a human agent to look into it. I am "
                                 "transferring you to a human agent, please hold on."),
    HANDOFF_UNVERIFIED_CLAIM: ("Sorry, this needs to be confirmed by a human agent. I am "
                               "transferring you to a human agent, please hold on."),
    HANDOFF_REPEATED_FALLBACK: ("Sorry, I could not understand your question. I am transferring "
                                "you to a human agent, who will help you further. Please hold on."),
    HANDOFF_TENANT_UNMAPPED: ("Hello, this inquiry cannot be answered automatically at the moment. "
                              "I am transferring you to a human agent, please hold on."),
    HANDOFF_IDENTITY_UNVERIFIED: ("To protect your order information, a human agent needs to "
                                  "verify your identity for this order first. I am transferring "
                                  "you to a human agent, please hold on."),
    HANDOFF_ORDER_UNMAPPED: ("The details of this order need to be checked further by a human "
                             "agent. I am transferring you to a human agent, please hold on."),
    HANDOFF_LOOKUP_FAILED: ("Sorry, I could not retrieve the information for this order. I am "
                            "transferring you to a human agent, please hold on."),
    HANDOFF_REFUND_REQUEST: ("I have passed your request on to our after-sales team, and a "
                             "colleague will contact you. Please hold on."),
})
REPLY_INTERNAL_ERROR_EN = ("Sorry, something went wrong while handling your message. I am "
                           "transferring you to a human agent, please hold on.")
REPLY_DESK_UNAVAILABLE_EN = ("Sorry, something went wrong while handling your message. Please "
                             "send it again later, or reply \"human\" to reach a human agent.")
REPLY_ASK_ORDER_NO_EN = ("To look into this order for you, please tell me your order number. "
                         "You can find it on the order details page.")

#: 按语种取的固定话术（p13）。键：``fallback`` / ``internal_error`` / ``desk_unavailable`` /
#: ``ask_order_no`` / ``needs_order_lookup`` 与各转人工原因。中文的各原因就是 p12 那几句。
REPLIES: Mapping[str, Mapping[str, str]] = MappingProxyType({
    LANG_ZH: MappingProxyType({
        "fallback": REPLY_FALLBACK, "internal_error": REPLY_INTERNAL_ERROR,
        "desk_unavailable": REPLY_DESK_UNAVAILABLE, "ask_order_no": REPLY_ASK_ORDER_NO,
        **REPLY_BY_REASON, HANDOFF_NEEDS_ORDER_LOOKUP: REPLY_NEEDS_ORDER_LOOKUP,
    }),
    LANG_EN: MappingProxyType({
        "fallback": REPLY_FALLBACK_EN, "internal_error": REPLY_INTERNAL_ERROR_EN,
        "desk_unavailable": REPLY_DESK_UNAVAILABLE_EN, "ask_order_no": REPLY_ASK_ORDER_NO_EN,
        **REPLY_BY_REASON_EN,
    }),
})


def reply_for(lang: str, key: str) -> str:
    """某语种的一句固定话术；语种不认就按中文（缺省语种）。"""
    table = REPLIES.get(lang) or REPLIES[LANG_ZH]
    return table[key]


#: 全部给客户的固定话术（测试逐句过 check_reply 与禁词）。p12 的在前，p13 的中英各句在后。
CUSTOMER_REPLIES: tuple[str, ...] = (
    REPLY_FALLBACK, REPLY_HANDOFF, *REPLY_BY_REASON.values(),
    REPLY_INTERNAL_ERROR, REPLY_DESK_UNAVAILABLE,
    REPLY_ASK_ORDER_NO, REPLY_NEEDS_ORDER_LOOKUP,
    REPLY_FALLBACK_EN, REPLY_HANDOFF_EN, *REPLY_BY_REASON_EN.values(),
    REPLY_INTERNAL_ERROR_EN, REPLY_DESK_UNAVAILABLE_EN, REPLY_ASK_ORDER_NO_EN,
)

# ---------------------------------------------------------------------------
# 给人工的处理建议（进卡片、不进 event_log —— 审计行只落它的摘要）
# ---------------------------------------------------------------------------
SUGGESTION_BY_REASON: Mapping[str, str] = MappingProxyType({
    HANDOFF_REQUESTED: "客户明确要求人工客服。请接入会话，先确认客户想办理的具体事项。",
    HANDOFF_COMPLAINT: ("客户表达了投诉 / 维权意向。请先致歉并了解事情经过，按投诉流程登记跟进；"
                        "在核实之前不要对处理结果做任何承诺。"),
    HANDOFF_ANGER: "客户情绪激动。请先安抚情绪，再了解具体问题，按实际情况处理。",
    HANDOFF_COMPENSATION: ("客户提出赔偿 / 补偿诉求。金额与方案只能由人工按政策判断：请先核实订单与问题，"
                           "再与客户沟通；机器人没有做任何承诺。"),
    HANDOFF_PRIVACY: ("客户的问题涉及个人信息。请先核实身份，再按隐私处理规范答复；"
                      "不要在对话里回显证件号、手机号、住址等敏感信息。"),
    HANDOFF_NEEDS_ORDER_LOOKUP: ("客户的问题要看具体订单才能答复（本期前台不查单）。"
                                 "请核实订单、物流 / 支付记录后与客户沟通。"),
    HANDOFF_UNVERIFIED_CLAIM: ("机器人拟好的回复被后置校验拦下（没有本轮观察撑的状态说法或引用），"
                               "已改发兜底话术。请核实实际情况后答复客户。"),
    HANDOFF_REPEATED_FALLBACK: (f"机器人连续 {FALLBACK_STREAK_HANDOFF} 轮没能理解客户的问题。"
                                "请看最近几轮原文，了解客户想咨询什么。"),
    HANDOFF_TENANT_UNMAPPED: ("这个客服账号没有绑定租户（MAOS_CS_TENANTS 里没有它），机器人没有作答。"
                              "请人工接待，并请运维补齐绑定。"),
    HANDOFF_IDENTITY_UNVERIFIED: ("客户报的订单号没有绑定到这位客户，机器人没有查单。"
                                  "请先核实身份与订单归属，再决定能否告知订单情况。"),
    HANDOFF_ORDER_UNMAPPED: ("查到了订单，但平台状态不在对外措辞表里（改单或平台状态未映射），"
                             "机器人没有说状态。请到平台后台核实后答复客户。"),
    HANDOFF_LOOKUP_FAILED: ("只读查单没有成功（查不到、平台出错或订单系统没配），机器人没有说状态。"
                            "请核实订单号与订单系统后答复客户。"),
    HANDOFF_REFUND_REQUEST: ("客户提出退款 / 退货诉求，身份与查单已通过、只读预检已算好。"
                             "如同意受理，请由你本人发出卡片上那一行 /refund 命令，走正常审批。"),
})

#: p13：needs_order_lookup 在注入了端口的路径上有三种来由，各一条建议（SUGGESTION_BY_REASON 那条
#: 说的是「本期前台不查单」，只对 p12 路径成立）。
SUGGESTION_ASK_EXHAUSTED = (f"客户要办具体订单，但追问 {MAX_ASKS_PER_SLOT} 次仍没有给出订单号，机器人没有查单。"
                            "请向客户核实订单号与身份后处理。")
SUGGESTION_REFUND_REFUSED = ("客户提出退款 / 退货诉求，身份核验与只读查单已通过，但只读预检没有通过，"
                             "机器人没有出采纳命令。请核实订单与诉求后按正常流程处理。")
SUGGESTION_EXCHANGE = ("客户提出换货诉求，身份核验与只读查单已通过（本期前台不办换货）。"
                       "请核实订单与商品情况后与客户沟通换货事宜。")

#: 卡片处理建议里前台自己另起一行写的几段（:func:`render_card_text` 按这几个前缀认，各占一行）。
CARD_SECTION_OBSERVATION = "查单观察："
CARD_SECTION_SUMMARY = "预检摘要："
CARD_SECTION_COMMAND = "采纳命令："
CARD_SECTION_REFUSED = "预检未通过："
#: 退款桥预检用的内部单号（= 绑定的 query_key）；只在它与客户报的单号不同时另起一行写出。
CARD_SECTION_LEDGER_NO = "内部单号："
CARD_SECTIONS = (CARD_SECTION_OBSERVATION, CARD_SECTION_SUMMARY, CARD_SECTION_COMMAND,
                 CARD_SECTION_REFUSED, CARD_SECTION_LEDGER_NO)

#: 预检端口没注入时，退款桥里记的拒绝原因。
REFUSED_PRECHECK_UNCONFIGURED = "precheck_unconfigured"
#: 预检端口抛了（端口承诺不抛，这里再兜一层）时记的拒绝原因。
REFUSED_PRECHECK_ERROR = "precheck_error"

REASON_LABELS: Mapping[str, str] = MappingProxyType({
    HANDOFF_REQUESTED: "客户要求人工",
    HANDOFF_COMPLAINT: "投诉升级",
    HANDOFF_ANGER: "情绪激烈",
    HANDOFF_COMPENSATION: "赔偿 / 补偿诉求",
    HANDOFF_PRIVACY: "涉及个人信息",
    HANDOFF_NEEDS_ORDER_LOOKUP: "要查具体订单",
    HANDOFF_UNVERIFIED_CLAIM: "回复未通过校验",
    HANDOFF_REPEATED_FALLBACK: "连续答不上",
    HANDOFF_TENANT_UNMAPPED: "客服账号未绑定租户",
    HANDOFF_IDENTITY_UNVERIFIED: "订单未绑定到客户",
    HANDOFF_ORDER_UNMAPPED: "订单状态说不准",
    HANDOFF_LOOKUP_FAILED: "查单失败",
    HANDOFF_REFUND_REQUEST: "退款待采纳",
})


# ---------------------------------------------------------------------------
# 检索用的那句话
# ---------------------------------------------------------------------------
#: 交给检索的客户原文最多这么多字（复核 L3-2）。检索的重排与知识层全文通道的开销随
#: 句长近似平方增长，而前台跑在 ingress 唯一的工作线程上 —— 一条上万字的消息能把内部
#: 渠道的审批一起堵住。触发词仍看全文（线性、宁可多转），会话表与卡片里存的也是全文。
MAX_QUERY_CHARS = 200

#: 连续这么多位及以上的数字串（订单号、运单号、手机号）不进检索：话术库的说法里没有
#: 长数字，这种串只会给原文凭空添一串谁也对不上的二元组，把覆盖率压下去 —— 带着订单号
#: 来办具体订单的客户（恰恰该转人工核实的那类）反而检不到话术、掉进兜底。
_LONG_DIGITS_RE = re.compile(r"\d{5,}")


def retrieval_query(text: str) -> str:
    """本轮交给 ``cs.answer`` 检索的那句话：去掉长数字串，再截到 :data:`MAX_QUERY_CHARS` 字。"""
    return _LONG_DIGITS_RE.sub(" ", text or "")[:MAX_QUERY_CHARS]


# ---------------------------------------------------------------------------
# 「要看具体订单」（p13 契约 §2 第 6 步）
# ---------------------------------------------------------------------------
#: 要看具体订单的诉求（``request == other`` 不算）。
ORDER_REQUESTS = frozenset({REQUEST_TRACK, REQUEST_REFUND, REQUEST_RETURN, REQUEST_EXCHANGE})
#: 查单成功后要走退款桥的诉求。
BRIDGE_REQUESTS = frozenset({REQUEST_REFUND, REQUEST_RETURN})
#: 能落到查单分支上的业务意图。
ORDER_INTENTS = frozenset({INTENT_LOGISTICS, INTENT_REFUND_PAYMENT, INTENT_RETURN_EXCHANGE})


def order_need(text: str, *, fresh: Mapping[str, str], merged: Mapping[str, str],
               intent: str) -> str:
    """本轮要不要看具体订单：返回诉求（track / refund / return / exchange），不要就返回空串。

    契约的口径是「request ∈ {track, refund, return, exchange}」（other 不算）。理解层只把**本轮**
    明说的诉求放进 ``fresh``，下面三种它不给、却同样是在办具体订单（DECISIONS task-t174）：

    * 本轮没说诉求、但带着进度线索（「帮我查查它现在到哪了」，单号是上一轮给的）→ track；
    * 本轮只补了单号（追问之后的回答「订单号是 S1818」）→ 会话里已有的诉求；
    * 本轮报了单号、意图是物流或带着查询的说法（「我想查一下 G3333 的物流」「could you check
      where my order A5001 is」，:data:`_ORDER_QUERY_RE`）→ track。

    本轮的诉求是 other（撤回、改地址一类）→ 不看订单。只报了单号、没说要办什么（「我的订单号是
    B2727」）也不看：单号记进槽位，下一轮说了诉求再查。

    p15 T184（review/p15-cs-contracts.md §1 / §2 T184）在上面这套之外再定两条：

    * C4：假设 / 泛问句（「要是退货的话运费谁出」「如果包裹一直没到怎么办」「what if …」）问的是
      规则，**最先**判、一律不看订单（:func:`hypothetical_question`）—— 哪怕句子里有诉求词或进度线索；
    * C1 / C2：没有诉求词也没有进度线索、但在要看这一单（「看下我包裹」「物流查一下」「is order
      A1001 on its way」「has my parcel shipped」，:func:`asks_order_status`）→ track。
    """
    if hypothetical_question(text):
        return ""
    req = str(fresh.get(SLOT_REQUEST) or "")
    if req in ORDER_REQUESTS:
        return req
    if req:
        return ""
    if scripts.detect_cue(text) == scripts.CUE_PROGRESS:
        return REQUEST_TRACK
    if asks_order_status(text):
        return REQUEST_TRACK
    if fresh.get(SLOT_ORDER_NO):
        prior = str(merged.get(SLOT_REQUEST) or "")
        if prior in ORDER_REQUESTS:
            return prior
        if intent == INTENT_LOGISTICS or _ORDER_QUERY_RE.search(text or ""):
            return REQUEST_TRACK
    return ""


#: 报了单号时「是在要查这一单」的说法（中文按子串、英文按词）。
_ORDER_QUERY_RE = re.compile(
    r"查|看看|看下|看一下|到哪|物流|快递|状态"
    r"|(?<![A-Za-z])(?:where|track|tracking|check|status)(?![A-Za-z])", re.IGNORECASE)


# ---- p15 T184：判定顺序的几张说法表（只在注入了端口的路径上用；p12 路径一个字不看）----------------
def _t184_norm(text: str) -> str:
    """说法表用的规范化：NFKC、小写、弯引号换直引号、空白压成一个空格。"""
    s = unicodedata.normalize("NFKC", text or "").lower().replace("’", "'").replace("‘", "'")
    return re.sub(r"\s+", " ", s).strip()


def _t184_compact(text: str) -> str:
    """中文说法表用：:func:`_t184_norm` 再去掉空白。"""
    return _t184_norm(text).replace(" ", "")


#: 英文按词认：前后不挨 ASCII 字母数字（「checkout」「tracksuit」「shipping」里认不出 check / track / ship）。
_EN_B = r"(?<![a-z0-9])"
_EN_E = r"(?![a-z0-9])"

#: C4：假设的说法。「如果可以的话 / 方便的话 / if possible」是客气话，不是假设，先抹掉再判。
_ZH_POLITE_IF_RE = re.compile(
    r"(?:如果|要是|假如|若是)?(?:可以|方便|能|行|不麻烦)的话|(?:如果|要是)(?:可以|方便|能行|不麻烦|有空)")
_EN_POLITE_IF_RE = re.compile(
    _EN_B + r"if (?:possible|you (?:can|could|may|don't mind|do not mind|have time)|it's possible|"
    r"that's ok(?:ay)?|convenient)" + _EN_E)
_ZH_HYPOTHETICAL_RE = re.compile(
    r"如果|要是|假如|假设|假使|万一|若是|倘若|如若|的话"
    # 泛问：「一般 / 通常 / 正常情况下」问的是常规（「我一般……」是在说自己，不算）
    r"|(?<!我)(?:一般|通常|正常)(?:来说|来讲|情况下?)")
_EN_HYPOTHETICAL_RE = re.compile(
    _EN_B + r"(?:what if|if|in case|suppose|supposing|hypothetically|generally|in general|usually|normally"
    r"|typically)" + _EN_E)
#: C4：问句的形状（问办法 / 谁出 / 是非 / 时长）。假设之后跟着要办的祈使句（「要是今天不发货就给我
#: 退款」）不是在问规则，不算。
_ZH_QUESTION_RE = re.compile(
    r"怎么|怎样|如何|咋|谁出|谁付|谁承担|谁来|哪|什么|啥|多久|多长|几天|几个|多少|能不能|可不可以|能否|是否"
    r"|有没有|会不会|要不要|需不需要|是不是|可以吗|能吗|行吗|[吗嘛么呢][?!.~。…]*$|[吗嘛么呢](?=[,，。.!！?？;；~～…])"
    r"|[?？]")
_EN_QUESTION_RE = re.compile(
    r"\?|" + _EN_B + r"(?:how|what|who|which|when|where|why|can|could|will|would|do|does|is|are|should|"
    r"may|am)" + _EN_E)


def hypothetical_question(text: str) -> bool:
    """C4：假设 / 泛问句 —— 问的是规则，不是这一单（「要是退货的话运费谁出」「如果我想换货要怎么弄」
    「what if my parcel gets lost」）。要同时有假设说法与问句形状；客气话里的「如果可以」不算假设。"""
    norm = _t184_norm(text)
    zh = _ZH_POLITE_IF_RE.sub("，", norm.replace(" ", ""))
    en = _EN_POLITE_IF_RE.sub(",", norm)
    if _ZH_HYPOTHETICAL_RE.search(zh) and _ZH_QUESTION_RE.search(zh):
        return True
    return bool(_EN_HYPOTHETICAL_RE.search(en) and _EN_QUESTION_RE.search(en))


#: C1：中文「查物流一类」的说法 —— 查 / 看 / 追踪 +（一下 / 个 / 我的）+ 物流 / 快递 / 包裹 / 订单，
#: 或倒过来「物流查一下」「快递帮我看看」。「快递费」「快递公司」是问规则，不算。
_ZH_LOOKUP_NOUN = r"(?:物流|快递|包裹|订单|单子|运单)(?!费|公司|员|柜|点|单号?规则)"
_ZH_LOOKUP_ASK_RE = re.compile(
    r"(?:查询|查查|查|看看|看|瞅瞅|瞅|瞧瞧|瞧|追踪|跟踪|跟进)(?:一下|一查|一看|下|个)?"
    r"(?:我|俺|咱)?(?:的|那个|这个|那|这)?" + _ZH_LOOKUP_NOUN
    + r"|(?:物流|快递|包裹|订单|单子)(?:信息|情况|状态|进度|动态)?(?:帮我|给我|麻烦|帮忙)?"
      r"(?:查询|查查|查|看看|看|瞅瞅|瞅)(?:一下|一查|一看|下)?(?![a-z0-9])(?!费|运费)")
#: 问怎么查（「怎么查物流」「在哪里看快递」）是在问办法，不算要看这一单。
_ZH_HOWTO_LOOKUP_RE = re.compile(
    r"(?:怎么|怎样|如何|咋|在哪|哪里|哪儿|去哪|哪个页面|哪个地方)(?:里|儿)?(?:可以|能|才能|去|才)?"
    r"(?:查询|查看|查|看)")

#: C2：英文的订单状态问法。宾语是「我的 / 这个」订单、包裹，代词 it，或一个单号形状的串。
_EN_OBJ = (r"(?:(?:my|the|this|that|our|your) )?(?:(?:order|package|parcel|shipment|delivery|item|items|stuff"
           r"|purchase|goods|box)(?: (?:no\.?|number|#))?(?: ?#?[a-z]*\d[a-z0-9-]*)?|it|#?[a-z]*\d[a-z0-9-]{2,})")
_EN_STATUS_PATTERNS: tuple[str, ...] = (
    # is order A1001 on its way / is my parcel still in transit / is it coming
    r"(?:is|are|was) " + _EN_OBJ + r" (?:still |already |even )?(?:on (?:its|the|their) way|coming|shipped"
    r"|dispatched|sent(?: out)?|out for delivery|in transit|delivered|arriving|here yet|en route|on route"
    r"|processed|packed)",
    # has A1001 shipped / did my package ship / has my order been dispatched yet / did it arrive
    r"(?:has|have|did) " + _EN_OBJ + r" (?:been |already |even |actually )*(?:ship|shipped|dispatch|dispatched"
    r"|sent|send|left|leave|gone out|go out|arrive|arrived|deliver|delivered|come|came|get sent|got sent)",
    # where's my package / where is order A1001 / where my order is
    r"where(?:'s| is| are|s) (?:my |the |this |that |our )?(?:order|package|parcel|shipment|delivery|item|items"
    r"|stuff|purchase|goods|box|#?[a-z]*\d[a-z0-9-]{2,})",
    r"where (?:my|the|this|that|our) (?:order|package|parcel|shipment|delivery|item|items|stuff|purchase|goods"
    r"|box)s? (?:is|are|went|has gone|got to)",
    # any update on my order / the status of order A1001
    r"(?:any|an|the latest|latest) (?:update|updates|news|progress) (?:on|about|for|regarding|with) " + _EN_OBJ,
    r"status (?:of|on|for) " + _EN_OBJ,
    # check / track my order, look up order A1001
    r"(?:check|track|trace|look up|lookup|look into|follow up on|find) (?:on )?" + _EN_OBJ,
)
_EN_STATUS_RE = re.compile("|".join(f"{_EN_B}(?:{p}){_EN_E}" for p in _EN_STATUS_PATTERNS))


def asks_order_status(text: str) -> bool:
    """C1 / C2：没有诉求词也没有进度线索、但在要看这一单：中文「看下我包裹」「物流查一下」
    （问怎么查不算），英文按词认的订单状态问法（「is order A1001 on its way」「has my parcel shipped」
    「where's my package」「any update on my order」）。"""
    norm = _t184_norm(text)
    if _EN_STATUS_RE.search(norm):
        return True
    zh = norm.replace(" ", "")
    return bool(_ZH_LOOKUP_ASK_RE.search(zh) and not _ZH_HOWTO_LOOKUP_RE.search(zh))


def asks_how_to_look(text: str) -> bool:
    """问怎么查 / 在哪看（「在哪里可以看物流」）：问的是办法，不是这一单。"""
    return bool(_ZH_HOWTO_LOOKUP_RE.search(_t184_compact(text)))


#: C3：上一轮在追问单号，本轮没给单号、但仍在说这一单（给不出单号、让我们直接查 / 直接办）。
_ZH_STILL_ON_ORDER_RE = re.compile(
    r"单号|订单|单子|这单|那单|这一单|找不到|不知道|不清楚|不记得|记不得|记不住|忘了|忘记|没有号|查不到|看不到|"
    r"没存|删了|直接|告诉我|在哪|到哪|查|看|帮我|快递|物流|包裹|没收到|没到")
_EN_STILL_ON_ORDER_RE = re.compile(
    _EN_B + r"(?:order|number|no idea|don't know|dont know|do not know|not sure|can't find|cannot find"
    r"|can not find|couldn't find|could not find|forgot|forget|lost|don't have|dont have|do not have|just"
    r"|where|track|check|package|parcel|shipment|delivery)" + _EN_E)


def still_on_order(text: str) -> bool:
    """C3：本轮（上一轮刚追问过单号）仍在说这一单 —— 给不出单号、要我们直接查 / 直接办。"""
    norm = _t184_norm(text)
    return bool(_ZH_STILL_ON_ORDER_RE.search(norm.replace(" ", ""))
                or _EN_STILL_ON_ORDER_RE.search(norm))


#: C1：话术库里「看这一单的进度 / 异常」那几篇（带 needs_order_lookup 标记的篇里，除了改地址
#: LOG-005 与为某单申请退换 RET-005 —— 那两篇是要办事，不是要看状态）。注入端口时检索落到这几篇，
#: 不再凭话术篇的转人工标记直接转人工，改走查单分支（缺单号就追问）。
ORDER_VIEW_SCHEMES = frozenset({"LOG-004", "LOG-006", "PAY-003", "PAY-004"})


def cites_order_view(citations: tuple[str, ...]) -> bool:
    """本轮检索命中的是 :data:`ORDER_VIEW_SCHEMES` 里的一篇（doc_id 形如 ``kb-cs-<租户>-<编号>``）。"""
    return any("-".join(str(c).split("-")[-2:]) in ORDER_VIEW_SCHEMES for c in citations)


def has_lang_signal(text: str) -> bool:
    """本轮原文有没有语种信号：有 CJK，或拿掉编码串（单号、型号）后还剩字母。

    只回一个单号（「A1001」「SO-2026-000123」）、纯数字、纯符号：没有信号，``detect_lang`` 只是
    回了缺省 zh —— 注入端口的路径上改沿用会话上一轮的语种（复核 L2-2）。
    """
    norm = unicodedata.normalize("NFKC", text or "")
    if any(cs_lang.is_cjk(ch) for ch in norm):
        return True
    return any(unicodedata.category(ch).startswith("L") for ch in cs_lang.drop_codes(norm))


def only_order_no(fresh: Mapping[str, str]) -> bool:
    """本轮只报了单号（顺带的情绪不算）：没有诉求、商品、问题。"""
    return bool(fresh.get(SLOT_ORDER_NO)) and not (set(fresh) - {SLOT_ORDER_NO, "emotion"})


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
ENV_TENANTS = "MAOS_CS_TENANTS"
ENV_HANDOFF_TARGET = "MAOS_CS_HANDOFF_TARGET"


def parse_tenants(raw: str) -> dict[str, str]:
    """``"wk_1=tnt-demo,wk_2=tnt-b"`` → ``{open_kfid: tenant_id}``。

    空串 → 空表。每一段必须是 ``kfid=tenant`` 且两边非空、只有一个 ``=``；同一个 kfid
    配两次抛 ``ValueError``（后一个悄悄盖掉前一个，就是把 A 租户的客户算给 B）。
    逗号前后的空白与空段（结尾多一个逗号）忽略。
    """
    out: dict[str, str] = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        pieces = part.split("=")
        if len(pieces) != 2 or not pieces[0].strip() or not pieces[1].strip():
            raise ValueError(f"{ENV_TENANTS} 的一段不是 kfid=tenant：{part!r}")
        kfid, tenant = pieces[0].strip(), pieces[1].strip()
        if kfid in out and out[kfid] != tenant:
            raise ValueError(f"{ENV_TENANTS} 里 {kfid!r} 配了两个租户：{out[kfid]!r} 与 {tenant!r}")
        out[kfid] = tenant
    return out


def parse_handoff_target(raw: str) -> tuple[str, str] | None:
    """``"feishu:oc_xxx"`` → ``("feishu", "oc_xxx")``；空串 → None（卡片只落库）。

    按**第一个**冒号切（Matrix 房间 id 自带冒号）。缺冒号、任一边为空抛 ``ValueError``；
    目标是外部渠道（微信客服）也抛 —— 内部卡片带着客户原文与处理建议，投到客户那边就是泄露。
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    channel, sep, chat_id = raw.partition(":")
    channel, chat_id = channel.strip(), chat_id.strip()
    if not sep or not channel or not chat_id:
        raise ValueError(f"{ENV_HANDOFF_TARGET} 不是 渠道:chat_id 的形状：{raw!r}")
    if channel == CHANNEL_WECHAT_KF:
        raise ValueError(f"{ENV_HANDOFF_TARGET} 不许指向外部渠道 {channel}：转人工卡片是内部信息")
    return channel, chat_id


@dataclass(frozen=True)
class CsConfig:
    """前台配置：客服账号 → 租户的映射，与转人工卡片的投递目标。"""
    tenants: Mapping[str, str] = field(default_factory=dict)
    handoff_target: tuple[str, str] | None = None

    def __post_init__(self) -> None:
        # 冻结成只读映射：配置在进程里被人就地改掉，同一段会话前后会落到两个租户。
        object.__setattr__(self, "tenants", MappingProxyType(dict(self.tenants or {})))
        if self.handoff_target is not None:
            object.__setattr__(self, "handoff_target", tuple(self.handoff_target))

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "CsConfig":
        """``MAOS_CS_TENANTS="wk_1=tnt-demo,wk_2=tnt-b"``；``MAOS_CS_HANDOFF_TARGET="feishu:oc_xxx"``。"""
        return cls(tenants=parse_tenants(env.get(ENV_TENANTS, "")),
                   handoff_target=parse_handoff_target(env.get(ENV_HANDOFF_TARGET, "")))

    def tenant_of(self, open_kfid: str) -> str:
        return self.tenants.get(open_kfid or "", "")


# ---------------------------------------------------------------------------
# 前台
# ---------------------------------------------------------------------------
@dataclass
class _Turn:
    """一轮进行到哪了：出错时据此决定还要补落什么。"""
    tenant_id: str = ""
    conv: Any = None
    turn_id: str = ""
    seq: int = 0
    intent: str = INTENT_UNKNOWN
    reason: str = ""
    card: Any = None                      # 已落库的那张卡（cs.handoff 成功之后才有）
    card_recorded: bool = False
    staged: bool = False
    turn_recorded: bool = False
    # p13
    lang: str = LANG_ZH                   # 本轮语种（DeskResult.lang、cs_turn_ext）
    say: str = LANG_ZH                    # 给客户的固定话术用哪种语言（无端口时恒为中文）
    lookup_outcome: str = ""
    ask_slot: str = ""
    ask_count: int = 0
    ext_recorded: bool = False


@dataclass(frozen=True)
class _Plan:
    """本轮定下来的出口（校验之前）。"""
    route: str
    intent: str
    draft: ReplyDraft
    reason: str = ""
    check: CheckResult | None = None      # cs.answer 已校验过的，就带着它的结果
    # p13：卡片上要带的东西（p12 路径恒为缺省）
    suggestion: str = ""                  # 非空就替掉 SUGGESTION_BY_REASON 那条
    sections: tuple[str, ...] = ()        # 处理建议下面前台另起一行写的几段（CARD_SECTIONS 起头）
    slots: tuple[tuple[str, str], ...] = ()


class FrontDesk:
    """客服前台。``handle`` 一次处理一条外部消息，**永不抛**。"""

    def __init__(self, store: Any, config: CsConfig, *,
                 clock: Callable[[], Any] | None = None,
                 verifier: Any = None, lookup: Any = None, precheck: Any = None,
                 model: Any = None) -> None:
        # import 即注册 cs.answer / cs.handoff / cs.understand（不指望 builtin 的动态发现恰好跑过）。
        # 放在函数体里：skill 模块要取本模块的话术常量，模块级 import 会成环。
        from maos.skills.builtin import cs as _cs_skills  # noqa: F401
        self.store = store
        self.config = config
        self._clock = clock
        #: p13 的三个端口（``ports.py`` 的 Protocol，装配处注入）与理解层的模型。
        self.verifier = verifier
        self.lookup = lookup
        self.precheck = precheck
        self.model = model
        self._invoker = SkillInvoker(CS_FRONT_DESK_IDENTITY, store)
        # 本前台要写 event_log：核心五表不在就建（幂等）。
        store.init_schema()

    @property
    def ports_injected(self) -> bool:
        """三个端口有没有注入任何一个。一个都没有 → 走 p12 路径（契约 §2 第 5 步）。"""
        return any(p is not None for p in (self.verifier, self.lookup, self.precheck))

    # ------------------------------------------------------------ 入口
    def handle(self, msg: InboundMessage) -> DeskResult:
        turn = _Turn()
        try:
            return self._handle(msg, turn)
        except Exception as exc:                          # noqa: BLE001 —— 永不抛
            return self._recover(msg, turn, exc)

    def _previous_lang(self, turn: _Turn) -> str:
        """会话上一轮（按 seq）记在 cs_turn_ext 里的语种；没有就空串。"""
        if not turn.tenant_id:
            return ""
        objects.ensure_schema(self.store)
        rows = objects.query(
            self.store,
            "SELECT e.lang AS lang FROM cs_turn_ext e JOIN cs_turn t"
            " ON t.tenant_id = e.tenant_id AND t.turn_id = e.turn_id"
            " WHERE e.tenant_id=? AND e.conversation_id=? AND e.turn_id<>?"
            " ORDER BY t.seq DESC LIMIT 1",
            (turn.tenant_id, turn.conv.conversation_id, turn.turn_id))
        lang = str(rows[0]["lang"]) if rows else ""
        return lang if lang in (LANG_ZH, LANG_EN) else ""

    def _stamp(self) -> str:
        if self._clock is not None:
            got = self._clock()
            return got.isoformat() if isinstance(got, datetime) else str(got)
        return datetime.now(timezone.utc).isoformat()

    def _handle(self, msg: Any, turn: _Turn) -> DeskResult:
        now = self._stamp()
        raw = msg.raw or {}
        open_kfid = str(raw.get("open_kfid") or "")
        turn.tenant_id = self.config.tenant_of(open_kfid)
        turn.conv = conversation.open_conversation(
            self.store, tenant_id=turn.tenant_id, channel=msg.channel, open_kfid=open_kfid,
            external_userid=msg.chat_id, now=now)
        turn.turn_id, turn.seq = conversation.allocate_turn(self.store, turn.conv)
        extras = {"plan_id": plan_id_for(turn.conv.conversation_id),
                  "task_id": turn.turn_id, "trace_id": ""}
        text = msg.text or ""

        # 0. 语种。没注入端口时给客户的固定话术照 p12（中文），见模块头第 5 步。
        turn.lang = detect_lang(text)
        if self.ports_injected and not has_lang_signal(text):
            # 只回了一个单号（「A1001」）之类：没有语种信号，沿用会话上一轮的语种
            # （DECISIONS task-t174 复核 L2-2）。p12 路径不动。
            turn.lang = self._previous_lang(turn) or turn.lang
        turn.say = turn.lang if self.ports_injected else LANG_ZH

        # 1. 已转人工：只记录。
        if turn.conv.stage != STAGE_ACTIVE:
            return self._silent(msg, turn, now)

        # 2. 租户映射不到：不检索，直接转人工。
        if not turn.tenant_id:
            plan = _Plan(ROUTE_HANDOFF, INTENT_UNKNOWN,
                         ReplyDraft(text=reply_for(turn.say, HANDOFF_TENANT_UNMAPPED)),
                         reason=HANDOFF_TENANT_UNMAPPED)
            return self._finish(msg, turn, plan, extras, now,
                                note=f"客服账号：{open_kfid or '（空）'}")

        # 3. 触发词。
        hit = detect(text)
        if hit is not None:
            reason, intent = hit
            plan = _Plan(ROUTE_HANDOFF, intent, ReplyDraft(text=reply_for(turn.say, reason)),
                         reason=reason)
            return self._finish(msg, turn, plan, extras, now)

        # 5.（先判）没注入端口：不调理解，照 p12 的第 4–6 步。
        if not self.ports_injected:
            return self._policy(msg, turn, text, extras, now, intent_hint="")

        # 4. 理解：槽位并进 cs_slot。
        understood, prior = self._understand(turn, text, extras, now)
        fresh = cs_understand.extract_slots(text, lang=understood.lang)
        slots = tuple(records.get_slots(self.store, turn.tenant_id,
                                        turn.conv.conversation_id).items())

        # 6. 要看具体订单。
        need = order_need(text, fresh=fresh, merged=understood.slots, intent=understood.intent)
        hypothetical = hypothetical_question(text)
        if (not need and not hypothetical and not fresh.get(SLOT_REQUEST)
                and (only_order_no(fresh) if fresh.get(SLOT_ORDER_NO) else still_on_order(text))
                and self._pending_order_ask(turn)):
            # p15 T184 C3：上一轮刚追问过单号，本轮仍没给、但仍在说这一单（「找不到单号，你直接告诉我
            # 在哪」）→ 还是查单分支：没追问够就再问一次，追问已用尽就 needs_order_lookup 转人工。
            # 本轮只回了单号（追问的回答）同理进查单 —— C1 / C2 那几类说法的诉求不进槽位，
            # 回答那一轮要靠「上一轮在追问」接上。
            prior = str(understood.slots.get(SLOT_REQUEST) or "")
            need = prior if prior in ORDER_REQUESTS else REQUEST_TRACK
        if need:
            return self._order(msg, turn, text, extras, now, need=need,
                               understood=understood, slots=slots)

        # 7. 其余同 p12；英文不检中文话术，回英文兜底。只报了单号、没说要办什么：兜底请客户说明
        #    （单号已进槽位，下一轮说了诉求就查）—— 不拿「订单号」三个字去检索查单篇转人工。
        #    p15 T184 C4：假设 / 泛问句带着单号（「A1001 要是退货运费谁出」）问的也是规则，照样检索。
        if turn.lang == LANG_EN or (only_order_no(fresh) and not hypothetical):
            plan = _Plan(ROUTE_FALLBACK, INTENT_UNKNOWN,
                         ReplyDraft(text=reply_for(turn.say, "fallback")))
            return self._finish(msg, turn, self._streak(turn, plan), extras, now)
        plan = self._answer(turn, retrieval_query(text), extras, intent_hint=understood.intent)
        if (plan.route == ROUTE_HANDOFF and plan.reason == HANDOFF_NEEDS_ORDER_LOOKUP
                and not hypothetical and not fresh.get(SLOT_REQUEST)
                and not asks_how_to_look(text) and cites_order_view(plan.draft.citations)):
            # p15 T184 C1：检索落到「看这一单的进度 / 异常」那几篇 —— 注入了端口就不凭话术篇的转人工
            # 标记直接转人工，改走查单分支（缺单号先追问；会话里已有单号就查）。
            intent = plan.intent if plan.intent in ORDER_INTENTS else understood.intent
            return self._order(msg, turn, text, extras, now, need=REQUEST_TRACK,
                               understood=cs_understand.Understanding(
                                   lang=understood.lang, intent=intent, slots=understood.slots,
                                   source=understood.source),
                               slots=slots)
        turn.intent = plan.intent
        return self._finish(msg, turn, self._streak(turn, plan), extras, now)

    def _pending_order_ask(self, turn: _Turn) -> bool:
        """会话上一轮（按 seq）是不是在追问单号（cs_turn_ext.ask_slot == order_no）。"""
        objects.ensure_schema(self.store)
        rows = objects.query(
            self.store,
            "SELECT e.ask_slot AS ask_slot FROM cs_turn_ext e JOIN cs_turn t"
            " ON t.tenant_id = e.tenant_id AND t.turn_id = e.turn_id"
            " WHERE e.tenant_id=? AND e.conversation_id=? AND e.turn_id<>?"
            " ORDER BY t.seq DESC LIMIT 1",
            (turn.tenant_id, turn.conv.conversation_id, turn.turn_id))
        return bool(rows) and str(rows[0]["ask_slot"]) == SLOT_ORDER_NO

    # ------------------------------------------------------------ 各步
    def _policy(self, msg: Any, turn: _Turn, text: str, extras: dict, now: str, *,
                intent_hint: str) -> DeskResult:
        """p12 的第 4–5 步：检索 + 组稿 + 后置校验（cs.answer），再看连续兜底。"""
        plan = self._answer(turn, retrieval_query(text), extras, intent_hint=intent_hint)
        turn.intent = plan.intent
        return self._finish(msg, turn, self._streak(turn, plan), extras, now)

    def _streak(self, turn: _Turn, plan: _Plan) -> _Plan:
        """本轮兜底使 fallback_streak 达到门槛 → 改走 repeated_fallback（意图 unknown）。"""
        if (plan.route == ROUTE_FALLBACK
                and turn.conv.fallback_streak + 1 >= FALLBACK_STREAK_HANDOFF):
            return _Plan(ROUTE_HANDOFF, INTENT_UNKNOWN,
                         ReplyDraft(text=reply_for(turn.say, HANDOFF_REPEATED_FALLBACK)),
                         reason=HANDOFF_REPEATED_FALLBACK)
        return plan

    def _understand(self, turn: _Turn, text: str, extras: dict,
                    now: str) -> tuple[Any, dict[str, str]]:
        """第 4 步：经 cs.understand 理解本轮；合并后的槽位里变了的写进 cs_slot。"""
        conv = turn.conv
        prior = records.get_slots(self.store, turn.tenant_id, conv.conversation_id)
        res = self._invoker.invoke(SKILL_UNDERSTAND, {
            "tenant_id": turn.tenant_id,
            "conversation_id": conv.conversation_id,
            "turn_id": turn.turn_id,
            "text": text,
            "prior_slots": dict(prior),
        }, extras={**extras, "model": self.model})
        if res.status != "ok" or not isinstance(res.output, dict):
            raise RuntimeError(f"{SKILL_UNDERSTAND} 失败：{res.error}")
        understood = cs_understand.Understanding.from_json(res.output)
        for key in SLOT_KEYS:
            value = understood.slots.get(key)
            if value and prior.get(key) != value:
                records.set_slot(self.store, conv, key=key, value=value, turn_id=turn.turn_id,
                                 source=SLOT_SOURCE_RULE, now=now)
        turn.intent = understood.intent
        return understood, prior

    def _order_intent(self, turn: _Turn, intent: str, need: str) -> str:
        """查单分支这一轮记的意图：理解层给的是业务意图就用它；否则退款 / 退货 / 换货记
        return_exchange，查进度记会话里最近一轮的业务意图（没有就 logistics）。"""
        if intent in ORDER_INTENTS:
            return intent
        if need != REQUEST_TRACK:
            return INTENT_RETURN_EXCHANGE
        rows = objects.query(
            self.store, "SELECT intent FROM cs_turn WHERE tenant_id=? AND conversation_id=?"
                        " ORDER BY seq DESC", (turn.tenant_id, turn.conv.conversation_id))
        for row in rows:
            if row["intent"] in ORDER_INTENTS:
                return str(row["intent"])
        return INTENT_LOGISTICS

    def _order(self, msg: Any, turn: _Turn, text: str, extras: dict, now: str, *,
               need: str, understood: Any, slots: tuple[tuple[str, str], ...]) -> DeskResult:
        """第 6 步：追问 → 身份核验 → 只读查单 →（退款 / 退货）只读预检。"""
        conv = turn.conv
        intent = self._order_intent(turn, understood.intent, need)
        turn.intent = intent
        order_no = str(understood.slots.get(SLOT_ORDER_NO) or "")

        def handoff(reason: str, *, suggestion: str = "", sections: tuple[str, ...] = (),
                    draft: ReplyDraft | None = None) -> DeskResult:
            plan = _Plan(ROUTE_HANDOFF, intent, draft or ReplyDraft(text=reply_for(turn.say, reason)),
                         reason=reason, suggestion=suggestion, sections=sections, slots=slots)
            return self._finish(msg, turn, plan, extras, now)

        # a. 缺单号：追问；已追问够次数仍缺 → 转人工。
        if not order_no:
            asked = records.ask_count(self.store, turn.tenant_id, conv.conversation_id,
                                      SLOT_ORDER_NO)
            if asked >= MAX_ASKS_PER_SLOT:
                return handoff(HANDOFF_NEEDS_ORDER_LOOKUP, suggestion=SUGGESTION_ASK_EXHAUSTED)
            turn.ask_slot, turn.ask_count = SLOT_ORDER_NO, asked + 1
            plan = _Plan(ROUTE_CLARIFY, intent, ReplyDraft(text=reply_for(turn.say, "ask_order_no")))
            return self._finish(msg, turn, plan, extras, now)

        # b. 身份核验：查不到绑定（或核验器缺席 / 出错）一律不查单。
        binding = None
        if self.verifier is not None:
            try:
                binding = self.verifier.resolve(
                    self.store, tenant_id=turn.tenant_id, channel=msg.channel,
                    external_userid=msg.chat_id, display_no=order_no)
            except Exception as exc:                      # noqa: BLE001 —— 失败即关
                log.warning("身份核验出错（客户 %s，轮次 %s）：%s",
                            mask_customer(msg.chat_id), turn.turn_id, type(exc).__name__)
                binding = None
        if binding is None:
            return handoff(HANDOFF_IDENTITY_UNVERIFIED)

        # c. 只读查单。
        result = self._lookup_once(turn, binding, extras)
        turn.lookup_outcome = result.outcome
        wording = ORDER_STATUS_WORDING[turn.say]
        if result.outcome == LOOKUP_OK and result.status in wording:
            obs_id = records.record_observation(self.store, conv, turn_id=turn.turn_id,
                                                result=result, now=now)
            observed = (f"{CARD_SECTION_OBSERVATION}{ORDER_STATUS_WORDING[LANG_ZH][result.status]}"
                        f"（{obs_id}）",)
            # d. 退款 / 退货：只读预检 → 退款桥。给客户的只有过渡话术，一个状态字都不说。
            if need in BRIDGE_REQUESTS:
                # 预检与 /refund 认的是台账 / 订单系统里的单号 = 绑定解析出的 query_key，
                # 不是客户报的 display_no（两者可以不同，见 DECISIONS task-t174 复核 L2-1）。
                ledger_no = str(getattr(binding, "query_key", "") or "") or order_no
                if ledger_no != order_no:
                    observed += (f"{CARD_SECTION_LEDGER_NO}{ledger_no}（客户报的是 {order_no}）",)
                pre = self._precheck_once(turn, ledger_no, text, slots)
                records.record_bridge(self.store, conv, turn_id=turn.turn_id, order_no=ledger_no,
                                      result=pre, now=now)
                if pre.ok:
                    return handoff(HANDOFF_REFUND_REQUEST, sections=observed + (
                        f"{CARD_SECTION_SUMMARY}{pre.summary or pre.decision}",
                        f"{CARD_SECTION_COMMAND}{pre.command_line}"))
                return handoff(HANDOFF_NEEDS_ORDER_LOOKUP, suggestion=SUGGESTION_REFUND_REFUSED,
                               sections=observed + (
                                   f"{CARD_SECTION_REFUSED}{pre.refused_why or '（未说明）'}",))
            if need == REQUEST_EXCHANGE:
                return handoff(HANDOFF_NEEDS_ORDER_LOOKUP, suggestion=SUGGESTION_EXCHANGE,
                               sections=observed)
            sentence = wording[result.status]
            plan = _Plan(ROUTE_ANSWER, intent, ReplyDraft(
                text=sentence, claims=(Claim(literal=sentence, basis_ref=BASIS_OBS + obs_id),)))
            return self._finish(msg, turn, plan, extras, now)
        note = f"查单结果：{result.outcome}" + (f"（{result.error_kind}）" if result.error_kind else "")
        if result.outcome in (LOOKUP_AMENDED, LOOKUP_UNMAPPED, LOOKUP_OK):
            return handoff(HANDOFF_ORDER_UNMAPPED, sections=(f"{CARD_SECTION_OBSERVATION}{note}",))
        return handoff(HANDOFF_LOOKUP_FAILED, sections=(f"{CARD_SECTION_OBSERVATION}{note}",))

    def _lookup_once(self, turn: _Turn, binding: Any, extras: dict) -> LookupResult:
        """调一次查单端口；端口缺席记 system_misconfigured，端口抛了记 platform_error（只记类名）。"""
        system_name = str(getattr(binding, "system_name", "") or "")
        query_key = str(getattr(binding, "query_key", "") or "")
        if self.lookup is None:
            return LookupResult(outcome=LOOKUP_MISCONFIGURED, system_name=system_name,
                                query_key=query_key)
        try:
            result = self.lookup.lookup(self.store, binding, plan_id=extras["plan_id"],
                                        task_id=turn.turn_id)
        except Exception as exc:                          # noqa: BLE001 —— 端口承诺不抛，再兜一层
            return LookupResult(outcome=LOOKUP_PLATFORM_ERROR, system_name=system_name,
                                query_key=query_key, error_kind=type(exc).__name__)
        if not isinstance(result, LookupResult) or result.outcome not in LOOKUP_OUTCOMES:
            return LookupResult(outcome=LOOKUP_PLATFORM_ERROR, system_name=system_name,
                                query_key=query_key, error_kind="InvalidLookupResult")
        return result

    def _precheck_once(self, turn: _Turn, order_no: str, text: str,
                       slots: tuple[tuple[str, str], ...]) -> PrecheckResult:
        """调一次预检端口。原因文本 = 本轮原文 + 会话里的问题槽位（客户常在上一轮说原因）。"""
        if self.precheck is None:
            return PrecheckResult(ok=False, refused_why=REFUSED_PRECHECK_UNCONFIGURED)
        problem = dict(slots).get(SLOT_PROBLEM, "")
        reason_text = text if not problem or problem in text else f"{text} {problem}"
        try:
            pre = self.precheck.precheck(tenant_id=turn.tenant_id, order_no=order_no,
                                         reason_text=reason_text, now=self._stamp())
        except Exception as exc:                          # noqa: BLE001 —— 端口承诺不抛，再兜一层
            log.warning("退款预检出错（轮次 %s）：%s", turn.turn_id, type(exc).__name__)
            return PrecheckResult(ok=False, refused_why=REFUSED_PRECHECK_ERROR)
        if not isinstance(pre, PrecheckResult):
            return PrecheckResult(ok=False, refused_why=REFUSED_PRECHECK_ERROR)
        if pre.ok and not pre.command_line:
            # 采纳命令是卡片存在的理由；ok 却没有命令就按没通过算（失败即关）。
            return PrecheckResult(ok=False, decision=pre.decision, rule_ref=pre.rule_ref,
                                  reason_code=pre.reason_code, refused_why=REFUSED_PRECHECK_ERROR)
        return pre

    def _answer(self, turn: _Turn, text: str, extras: dict, *, intent_hint: str = "") -> _Plan:
        payload = {
            "tenant_id": turn.tenant_id,
            "conversation_id": turn.conv.conversation_id,
            "turn_id": turn.turn_id,
            "text": text,
        }
        if intent_hint:
            # 只在给了提示时才带这个键：p12 路径的入参（与它的 SkillInvoked 摘要）逐字节不变。
            payload["intent_hint"] = intent_hint
        res = self._invoker.invoke(SKILL_ANSWER, payload, extras=extras)
        if res.status != "ok" or not isinstance(res.output, dict):
            raise RuntimeError(f"{SKILL_ANSWER} 失败：{res.error}")
        out = res.output
        draft = ReplyDraft.from_json(out.get("draft") or {})
        check = _check_from_json(out.get("check") or {})
        route = str(out.get("route") or "")
        intent = str(out.get("intent") or INTENT_UNKNOWN)
        reason = str(out.get("handoff_reason") or "")
        if route not in (ROUTE_ANSWER, ROUTE_FALLBACK, ROUTE_HANDOFF) or intent not in INTENTS \
                or (route == ROUTE_HANDOFF) != bool(reason) \
                or (reason and reason not in HANDOFF_REASONS):
            raise RuntimeError(f"{SKILL_ANSWER} 的输出不合契约：route={route!r} "
                               f"intent={intent!r} handoff_reason={reason!r}")
        return _Plan(route, intent, draft, reason=reason, check=check)

    def _verify(self, turn: _Turn, plan: _Plan) -> tuple[_Plan, CheckResult, str]:
        """第 6 步：出门前校验。没过就落 CsReplyRejected，改发兜底 + 转人工。

        返回（最终出口，最终那版回复的校验结果，给卡片的附注）。``cs_turn`` 里存的是
        **出门的那一版**与它自己的校验结果；被拦下的那一版只以违例种类进
        ``CsReplyRejected`` 与卡片附注。
        """
        conv_id = turn.conv.conversation_id
        check = plan.check
        if check is None:
            # 固定话术也过一遍：守住「每一句出门的回复都过校验」这个不变量。观察与命中都从库里读回
            # （p13 第 8 步），不信本函数手里那份。
            check = claims.check_reply(
                plan.draft,
                observations=records.turn_observation_ids(self.store, conversation_id=conv_id,
                                                          turn_id=turn.turn_id),
                kb_doc_ids=self._kb_ids(turn))
        rejected = check
        if check.ok:
            # 第二道：obs: claim 说的就是那条观察（措辞表、本轮语种、一条观察只撑一处）。
            wording = claims.check_observation_wording(
                plan.draft,
                records.observations_for_turn(self.store, conversation_id=conv_id,
                                              turn_id=turn.turn_id),
                lang=turn.say)
            if wording.ok:
                return plan, check, ""
            rejected = wording
        conversation.record_reply_rejected(self.store, turn.conv, turn_id=turn.turn_id,
                                           check=rejected)
        kinds = ",".join(sorted({v.kind for v in rejected.violations}))
        log.warning("前台回复被后置校验拦下（客户 %s，轮次 %s）：%s",
                    mask_customer(turn.conv.external_userid), turn.turn_id, kinds)
        fallback = ReplyDraft(text=reply_for(turn.say, HANDOFF_UNVERIFIED_CLAIM))
        final = claims.check_reply(fallback, observations=frozenset(), kb_doc_ids=frozenset())
        return (_Plan(ROUTE_HANDOFF, plan.intent, fallback, reason=HANDOFF_UNVERIFIED_CLAIM,
                      check=final, slots=plan.slots),
                final, f"被拦下的违例：{kinds}")

    def _kb_ids(self, turn: _Turn) -> frozenset[str]:
        return claims.turn_kb_doc_ids(self.store, conversation_id=turn.conv.conversation_id,
                                      turn_id=turn.turn_id)

    def _finish(self, msg: Any, turn: _Turn, plan: _Plan, extras: dict, now: str, *,
                note: str = "") -> DeskResult:
        plan, check, why = self._verify(turn, plan)
        note = "；".join(p for p in (note, why) if p)
        turn.intent = plan.intent
        card = None
        reply = plan.draft.text
        if plan.route == ROUTE_HANDOFF:
            card = self._raise_card(msg, turn, reason=plan.reason, intent=plan.intent,
                                    reply=reply, citations=plan.draft.citations,
                                    extras=extras, now=now, note=note,
                                    suggestion=plan.suggestion, sections=plan.sections,
                                    slots=plan.slots)
        self._record(msg, turn, route=plan.route, intent=plan.intent, reason=plan.reason,
                     reply=reply, draft=plan.draft, check=check, now=now)
        return self._result(turn, route=plan.route, intent=plan.intent, draft=plan.draft,
                            card=card, reason=plan.reason, check=check)

    def _record(self, msg: Any, turn: _Turn, *, route: str, intent: str, reason: str,
                reply: str, draft: ReplyDraft, check: CheckResult, now: str) -> None:
        """第 9 步：record_turn（lang、lookup_outcome）+ record_turn_ext（再加 ask_slot、ask_count）。"""
        turn.conv = conversation.record_turn(
            self.store, turn.conv, turn_id=turn.turn_id, seq=turn.seq,
            msg_dedup_key=msg.dedup_key, inbound_text=msg.text or "", reply_text=reply,
            route=route, intent=intent, handoff_reason=reason,
            draft=draft, check=check, now=now, lang=turn.lang,
            lookup_outcome=turn.lookup_outcome)
        turn.turn_recorded = True
        self._record_ext(turn)

    def _record_ext(self, turn: _Turn) -> None:
        if turn.ext_recorded:
            return
        records.record_turn_ext(self.store, turn.conv, turn_id=turn.turn_id, lang=turn.lang,
                                lookup_outcome=turn.lookup_outcome, ask_slot=turn.ask_slot,
                                ask_count=turn.ask_count)
        turn.ext_recorded = True

    @staticmethod
    def _result(turn: _Turn, *, route: str, intent: str, draft: ReplyDraft,
                card: HandoffCard | None, reason: str, check: CheckResult) -> DeskResult:
        return DeskResult(reply_text=draft.text, tenant_id=turn.tenant_id,
                          conversation_id=turn.conv.conversation_id, turn_id=turn.turn_id,
                          route=route, intent=intent, draft=draft,
                          handoff=card, handoff_reason=reason, check=check,
                          lang=turn.lang, lookup_outcome=turn.lookup_outcome,
                          ask_slot=turn.ask_slot)

    def _silent(self, msg: Any, turn: _Turn, now: str) -> DeskResult:
        draft = ReplyDraft(text="")
        check = CheckResult(ok=True)
        self._record(msg, turn, route=ROUTE_SILENT, intent=INTENT_UNKNOWN, reason="",
                     reply="", draft=draft, check=check, now=now)
        return self._result(turn, route=ROUTE_SILENT, intent=INTENT_UNKNOWN, draft=draft,
                            card=None, reason="", check=check)

    def _raise_card(self, msg: Any, turn: _Turn, *, reason: str, intent: str, reply: str,
                    citations: tuple[str, ...], extras: dict, now: str,
                    note: str = "", suggestion: str = "", sections: tuple[str, ...] = (),
                    slots: tuple[tuple[str, str], ...] = ()) -> HandoffCard:
        """组卡片 → 经 cs.handoff 落库 → 会话转 handed_off。

        p13：``suggestion`` 非空就替掉按原因取的那条；``sections``（查单观察、预检摘要、采纳命令、
        预检未通过）在处理建议后面各起一行（:func:`render_card_text` 各渲染成一行）；``slots`` 是
        会话当前的槽位。
        """
        conv = turn.conv
        earlier = conversation.recent_turns(self.store, conv.tenant_id, conv.conversation_id,
                                            limit=CARD_RECENT_TURNS - 1)
        advice = suggestion or SUGGESTION_BY_REASON.get(reason, "请人工接手这段会话。")
        if note:
            advice = f"{advice}（{note}）"
        advice = "\n".join((advice, *(_LINEBREAKS_RE.sub(" ", s) for s in sections
                                      if s.startswith(CARD_SECTIONS))))
        card = HandoffCard(
            handoff_id=turn.turn_id, tenant_id=conv.tenant_id,
            conversation_id=conv.conversation_id, turn_id=turn.turn_id,
            channel=msg.channel, reason=reason, intent=intent,
            customer_ref=mask_customer(msg.chat_id), customer_text=msg.text or "",
            recent_turns=tuple(earlier) + ((msg.text or "", reply),),
            suggestion=advice, citations=tuple(citations), slots=tuple(slots), created_at=now)
        delivery = DELIVERY_PENDING if self.config.handoff_target else DELIVERY_UNCONFIGURED
        res = self._invoker.invoke(SKILL_HANDOFF, {"card": card.to_json(),
                                                   "delivery": delivery, "now": now},
                                   extras=extras)
        if res.status != "ok":
            raise RuntimeError(f"{SKILL_HANDOFF} 失败：{res.error}")
        turn.card_recorded = True
        turn.card = card
        turn.reason = reason
        turn.conv = conversation.change_stage(self.store, conv, STAGE_HANDED_OFF,
                                              turn_id=turn.turn_id, reason=reason, now=now)
        turn.staged = True
        return card

    # ------------------------------------------------------------ 出错
    def _recover(self, msg: Any, turn: _Turn, exc: Exception) -> DeskResult:
        """内部出错：记日志（客户标识打码）、尽量落卡片与轮次、回出错话术、route=handoff。"""
        who = mask_customer(str(getattr(msg, "chat_id", "") or ""))
        log.error("客服前台处理失败（客户 %s，轮次 %s）：%s", who, turn.turn_id or "?",
                  type(exc).__name__, exc_info=True)
        now = self._stamp()
        extras: dict = {}
        card: HandoffCard | None = turn.card
        try:
            if turn.conv is None:
                raw = getattr(msg, "raw", None) or {}
                open_kfid = str(raw.get("open_kfid") or "")
                turn.tenant_id = self.config.tenant_of(open_kfid)
                turn.conv = conversation.open_conversation(
                    self.store, tenant_id=turn.tenant_id, channel=msg.channel,
                    open_kfid=open_kfid, external_userid=msg.chat_id, now=now)
            if not turn.turn_id:
                turn.turn_id, turn.seq = conversation.allocate_turn(self.store, turn.conv)
            extras = {"plan_id": plan_id_for(turn.conv.conversation_id),
                      "task_id": turn.turn_id, "trace_id": ""}
        except Exception:                                 # noqa: BLE001
            log.error("客服前台出错后连会话都没建成（客户 %s），本轮不落库", who, exc_info=True)
            return self._error_result(turn, card=None)

        if turn.conv.stage != STAGE_ACTIVE and not turn.card_recorded:
            # 已转人工的会话里出错：保持静默，不再出卡。
            try:
                if not turn.turn_recorded:
                    return self._silent(msg, turn, now)
                self._record_ext(turn)
            except Exception:                             # noqa: BLE001
                log.error("客服前台出错后静默轮也没落下（客户 %s）", who, exc_info=True)
            return DeskResult(reply_text="", tenant_id=turn.tenant_id,
                              conversation_id=turn.conv.conversation_id, turn_id=turn.turn_id,
                              route=ROUTE_SILENT, intent=INTENT_UNKNOWN,
                              draft=ReplyDraft(text=""), lang=turn.lang,
                              lookup_outcome=turn.lookup_outcome, ask_slot=turn.ask_slot)

        reply = reply_for(turn.say, "internal_error")
        if not turn.card_recorded:
            try:
                card = self._raise_card(
                    msg, turn, reason=HANDOFF_UNVERIFIED_CLAIM, intent=turn.intent,
                    reply=reply, citations=(), extras=extras, now=now,
                    note=f"前台内部出错：{type(exc).__name__}，本轮没有产出经校验的回复，请通知运维查日志")
            except Exception:                             # noqa: BLE001
                log.error("客服前台出错后转人工卡片没落下（客户 %s）", who, exc_info=True)
        elif not turn.staged:
            try:
                turn.conv = conversation.change_stage(
                    self.store, turn.conv, STAGE_HANDED_OFF, turn_id=turn.turn_id,
                    reason=turn.reason or HANDOFF_UNVERIFIED_CLAIM, now=now)
                turn.staged = True
            except Exception:                             # noqa: BLE001
                log.error("客服前台出错后会话阶段没改成（客户 %s）", who, exc_info=True)
        if not turn.card_recorded:
            reply = reply_for(turn.say, "desk_unavailable")
        draft = ReplyDraft(text=reply)
        check = CheckResult(ok=True)
        if turn.ask_slot and not turn.turn_recorded:
            # 追问那一轮没落成：这一轮不算追问过（ask_count 只数真的问出去的）。
            turn.ask_slot, turn.ask_count = "", 0
        if not turn.turn_recorded:
            try:
                turn.conv = conversation.record_turn(
                    self.store, turn.conv, turn_id=turn.turn_id, seq=turn.seq,
                    msg_dedup_key=msg.dedup_key, inbound_text=msg.text or "",
                    reply_text=reply, route=ROUTE_HANDOFF,
                    intent=turn.intent if turn.intent in INTENTS else INTENT_UNKNOWN,
                    handoff_reason=turn.reason or HANDOFF_UNVERIFIED_CLAIM,
                    draft=draft, check=check, now=now, lang=turn.lang,
                    lookup_outcome=turn.lookup_outcome)
                turn.turn_recorded = True
            except Exception:                             # noqa: BLE001
                log.error("客服前台出错后本轮没落下（客户 %s）", who, exc_info=True)
        if turn.turn_recorded:
            try:
                self._record_ext(turn)
            except Exception:                             # noqa: BLE001
                log.error("客服前台出错后本轮扩展字段没落下（客户 %s）", who, exc_info=True)
        return self._error_result(turn, card=card, reply=reply)

    @staticmethod
    def _error_result(turn: _Turn, *, card: HandoffCard | None,
                      reply: str | None = None) -> DeskResult:
        conv_id = turn.conv.conversation_id if turn.conv is not None else ""
        reply = reply_for(turn.say, "desk_unavailable") if reply is None else reply
        return DeskResult(reply_text=reply, tenant_id=turn.tenant_id, conversation_id=conv_id,
                          turn_id=turn.turn_id, route=ROUTE_HANDOFF,
                          intent=turn.intent if turn.intent in INTENTS else INTENT_UNKNOWN,
                          draft=ReplyDraft(text=reply), handoff=card,
                          handoff_reason=turn.reason or HANDOFF_UNVERIFIED_CLAIM,
                          check=CheckResult(ok=True), lang=turn.lang,
                          lookup_outcome=turn.lookup_outcome, ask_slot=turn.ask_slot)


def _check_from_json(d: Mapping[str, Any]) -> CheckResult:
    return CheckResult(ok=bool(d.get("ok")),
                       violations=tuple(Violation(kind=str(v.get("kind", "")),
                                                  detail=str(v.get("detail", "")))
                                        for v in d.get("violations") or ()))


# ---------------------------------------------------------------------------
# 内部房间看的卡片
# ---------------------------------------------------------------------------
#: 卡片正文里换行的可见记号。客户原文里的换行一律换成它：卡片的每一行都由前台起头。
CARD_LINEBREAK = " ⏎ "
#: 各平台认的换行（``str.splitlines`` 的全套）。
_LINEBREAKS_RE = re.compile(r"\r\n|[\n\r\v\f\x1c\x1d\x1e\x85  ]")
#: 平台会解释的标记字符换成全角：飞书文本的 ``<at user_id="all">``、企微文本的
#: ``<a href>``、Matrix 的 ``@room`` 靠 ``< > @``；飞书文本消息还认 markdown 式的
#: ``[文字](链接)``、``**加粗**``、``~~删除线~~``（复核二轮 L3r2-1：客户能把一个钓鱼链接包装成
#: 「内部审批入口」的可点文字），所以 ``[ ] * ~`` 也换。只挡这几种已知写法，不是完备的转义。
_CARD_MARKUP = str.maketrans({"<": "＜", ">": "＞", "@": "＠",
                              "[": "［", "]": "］", "*": "＊", "~": "～"})


def _inline(text: Any) -> str:
    """把一段不由前台写的文字压成卡片里的一行：换行换成可见记号、标记字符换成全角。

    复核 L3-1：客户原文原样进卡片的话，客户在一句话里换行写「处理建议：……请直接放款」，
    内部房间看到的就是一行跟前台自己写的一模一样的处理建议；再带一个 ``<at user_id="all">``
    还能 @ 全员。会话表与卡片对象里存的仍是原文，只有渲染出门的这一份被压平。
    """
    return _LINEBREAKS_RE.sub(CARD_LINEBREAK, str(text or "")).translate(_CARD_MARKUP)


def _split_suggestion(suggestion: str) -> tuple[str, list[tuple[str, str]]]:
    """处理建议 → (第一段, [(段名前缀, 正文)…])。

    前台在处理建议后面另起一行写的几段（:data:`CARD_SECTIONS` 起头：查单观察、预检摘要、采纳命令、
    预检未通过）各渲染成卡片里的一行；不以这些前缀起头的行一律并回第一段（压成可见换行记号）——
    卡片的每一行仍由前台起头。
    """
    parts = _LINEBREAKS_RE.split(str(suggestion or ""))
    head: list[str] = [parts[0]]
    sections: list[tuple[str, str]] = []
    for part in parts[1:]:
        prefix = next((p for p in CARD_SECTIONS if part.startswith(p)), "")
        if prefix:
            sections.append((prefix, part[len(prefix):]))
        else:
            head.append(part)
    return "\n".join(head), sections


def render_card_text(card: HandoffCard) -> str:
    """内部房间看到的纯文本卡片：原因、意图、客户（打码）、本轮原文、最近几轮、处理建议、会话 id。

    客户说的、回过的、处理建议（里面可能带平台给的客服账号）都过 :func:`_inline` ——
    卡片的每一行都从前台写的固定前缀起头，客户造不出一行假的。
    """
    label = REASON_LABELS.get(card.reason, card.reason)
    lines = [
        f"【转人工】{label}（{card.reason}） · 意图 {card.intent or INTENT_UNKNOWN}",
        f"客户：{_inline(card.customer_ref) or '（未知）'} · 渠道：{card.channel}",
        f"本轮原文：{_inline(card.customer_text)}",
    ]
    if card.recent_turns:
        lines.append(f"最近 {len(card.recent_turns)} 轮：")
        for i, (said, replied) in enumerate(card.recent_turns, start=1):
            lines.append(f"  {i}. 客户：{_inline(said)}")
            lines.append(f"     回复：{_inline(replied) or '（未回复）'}")
    advice, sections = _split_suggestion(card.suggestion)
    lines.append(f"处理建议：{_inline(advice)}")
    for prefix, body in sections:
        lines.append(f"{prefix}{_inline(body)}")
    if card.slots:
        lines.append("槽位：" + "、".join(f"{_inline(k)}={_inline(v)}" for k, v in card.slots))
    if card.citations:
        lines.append(f"引用话术：{'、'.join(card.citations)}")
    lines.append(f"会话：{card.conversation_id} · 轮次：{card.turn_id}"
                 + (f" · 租户：{card.tenant_id}" if card.tenant_id else " · 租户：（未绑定）"))
    return "\n".join(lines)
