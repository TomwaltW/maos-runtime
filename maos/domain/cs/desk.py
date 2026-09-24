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

身份 :data:`CS_FRONT_DESK_IDENTITY` 只持 ``cs.answer`` / ``cs.handoff`` 两个 skill、零工具、
最高风险 L。前台不 import 审批、放款、补偿、查单的任何一条路（静态守卫
``maos/tests/test_cs_guard_t170.py`` 全仓扫）。
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Callable, Mapping

from maos.agents.base import AgentIdentity
from maos.domain.cs import claims, conversation
from maos.domain.cs.triggers import detect
from maos.domain.cs.types import (
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
    INTENT_UNKNOWN,
    INTENTS,
    ROUTE_ANSWER,
    ROUTE_FALLBACK,
    ROUTE_HANDOFF,
    ROUTE_SILENT,
    STAGE_ACTIVE,
    STAGE_HANDED_OFF,
    CheckResult,
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

#: 客服前台的身份。**最小授权**：两个只读 / 只落 cs_ 表的 skill、零工具、风险上限 L。
#: 不进 AGENT_POOL（不是可被派单的岗位），口径同 ``outcome_commands.TICKET_DESK_IDENTITY``。
CS_FRONT_DESK_IDENTITY = AgentIdentity(
    agent_id="cs-front-desk",
    role="cs_front_desk",
    duty="外部渠道客服前台：按话术库答政策问题、答不上就兜底、该转人工就出卡片；"
         "只读、只转述、只转人工，不碰钱、审批、工单",
    allowed_skills=frozenset({"cs.answer", "cs.handoff"}),
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

#: 全部给客户的固定话术（测试逐句过 check_reply 与禁词）。
CUSTOMER_REPLIES: tuple[str, ...] = (
    REPLY_FALLBACK, REPLY_HANDOFF, *REPLY_BY_REASON.values(),
    REPLY_INTERNAL_ERROR, REPLY_DESK_UNAVAILABLE,
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


@dataclass(frozen=True)
class _Plan:
    """本轮定下来的出口（校验之前）。"""
    route: str
    intent: str
    draft: ReplyDraft
    reason: str = ""
    check: CheckResult | None = None      # cs.answer 已校验过的，就带着它的结果


class FrontDesk:
    """客服前台。``handle`` 一次处理一条外部消息，**永不抛**。"""

    def __init__(self, store: Any, config: CsConfig, *,
                 clock: Callable[[], Any] | None = None) -> None:
        # import 即注册 cs.answer / cs.handoff（不指望 builtin 的动态发现恰好跑过）。
        # 放在函数体里：skill 模块要取本模块的话术常量，模块级 import 会成环。
        from maos.skills.builtin import cs as _cs_skills  # noqa: F401
        self.store = store
        self.config = config
        self._clock = clock
        self._invoker = SkillInvoker(CS_FRONT_DESK_IDENTITY, store)
        # 本前台要写 event_log：核心五表不在就建（幂等）。
        store.init_schema()

    # ------------------------------------------------------------ 入口
    def handle(self, msg: InboundMessage) -> DeskResult:
        turn = _Turn()
        try:
            return self._handle(msg, turn)
        except Exception as exc:                          # noqa: BLE001 —— 永不抛
            return self._recover(msg, turn, exc)

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

        # 1. 已转人工：只记录。
        if turn.conv.stage != STAGE_ACTIVE:
            return self._silent(msg, turn, now)

        # 2. 租户映射不到：不检索，直接转人工。
        if not turn.tenant_id:
            plan = _Plan(ROUTE_HANDOFF, INTENT_UNKNOWN,
                         ReplyDraft(text=REPLY_BY_REASON[HANDOFF_TENANT_UNMAPPED]),
                         reason=HANDOFF_TENANT_UNMAPPED)
            return self._finish(msg, turn, plan, extras, now,
                                note=f"客服账号：{open_kfid or '（空）'}")

        # 3. 触发词。
        hit = detect(text)
        if hit is not None:
            reason, intent = hit
            plan = _Plan(ROUTE_HANDOFF, intent, ReplyDraft(text=REPLY_BY_REASON[reason]),
                         reason=reason)
            return self._finish(msg, turn, plan, extras, now)

        # 4. 检索 + 组稿 + 后置校验（cs.answer）。检索只看截短、去掉长数字串的那句。
        plan = self._answer(turn, retrieval_query(text), extras)
        turn.intent = plan.intent

        # 5. 连续兜底。
        if (plan.route == ROUTE_FALLBACK
                and turn.conv.fallback_streak + 1 >= FALLBACK_STREAK_HANDOFF):
            plan = _Plan(ROUTE_HANDOFF, INTENT_UNKNOWN,
                         ReplyDraft(text=REPLY_BY_REASON[HANDOFF_REPEATED_FALLBACK]),
                         reason=HANDOFF_REPEATED_FALLBACK)
        return self._finish(msg, turn, plan, extras, now)

    # ------------------------------------------------------------ 各步
    def _answer(self, turn: _Turn, text: str, extras: dict) -> _Plan:
        res = self._invoker.invoke(SKILL_ANSWER, {
            "tenant_id": turn.tenant_id,
            "conversation_id": turn.conv.conversation_id,
            "turn_id": turn.turn_id,
            "text": text,
        }, extras=extras)
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
        check = plan.check
        if check is None:
            # 固定话术也过一遍：守住「每一句出门的回复都过校验」这个不变量。
            check = claims.check_reply(plan.draft, observations=frozenset(),
                                       kb_doc_ids=self._kb_ids(turn))
        if check.ok:
            return plan, check, ""
        conversation.record_reply_rejected(self.store, turn.conv, turn_id=turn.turn_id,
                                           check=check)
        kinds = ",".join(sorted({v.kind for v in check.violations}))
        log.warning("前台回复被后置校验拦下（客户 %s，轮次 %s）：%s",
                    mask_customer(turn.conv.external_userid), turn.turn_id, kinds)
        fallback = ReplyDraft(text=REPLY_BY_REASON[HANDOFF_UNVERIFIED_CLAIM])
        final = claims.check_reply(fallback, observations=frozenset(), kb_doc_ids=frozenset())
        return (_Plan(ROUTE_HANDOFF, plan.intent, fallback, reason=HANDOFF_UNVERIFIED_CLAIM,
                      check=final),
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
                                    extras=extras, now=now, note=note)
        turn.conv = conversation.record_turn(
            self.store, turn.conv, turn_id=turn.turn_id, seq=turn.seq,
            msg_dedup_key=msg.dedup_key, inbound_text=msg.text or "", reply_text=reply,
            route=plan.route, intent=plan.intent, handoff_reason=plan.reason,
            draft=plan.draft, check=check, now=now)
        turn.turn_recorded = True
        return DeskResult(reply_text=reply, tenant_id=turn.tenant_id,
                          conversation_id=turn.conv.conversation_id, turn_id=turn.turn_id,
                          route=plan.route, intent=plan.intent, draft=plan.draft,
                          handoff=card, handoff_reason=plan.reason, check=check)

    def _silent(self, msg: Any, turn: _Turn, now: str) -> DeskResult:
        draft = ReplyDraft(text="")
        check = CheckResult(ok=True)
        turn.conv = conversation.record_turn(
            self.store, turn.conv, turn_id=turn.turn_id, seq=turn.seq,
            msg_dedup_key=msg.dedup_key, inbound_text=msg.text or "", reply_text="",
            route=ROUTE_SILENT, intent=INTENT_UNKNOWN, handoff_reason="",
            draft=draft, check=check, now=now)
        turn.turn_recorded = True
        return DeskResult(reply_text="", tenant_id=turn.tenant_id,
                          conversation_id=turn.conv.conversation_id, turn_id=turn.turn_id,
                          route=ROUTE_SILENT, intent=INTENT_UNKNOWN, draft=draft,
                          handoff=None, handoff_reason="", check=check)

    def _raise_card(self, msg: Any, turn: _Turn, *, reason: str, intent: str, reply: str,
                    citations: tuple[str, ...], extras: dict, now: str,
                    note: str = "") -> HandoffCard:
        """组卡片 → 经 cs.handoff 落库 → 会话转 handed_off。"""
        conv = turn.conv
        earlier = conversation.recent_turns(self.store, conv.tenant_id, conv.conversation_id,
                                            limit=CARD_RECENT_TURNS - 1)
        suggestion = SUGGESTION_BY_REASON.get(reason, "请人工接手这段会话。")
        if note:
            suggestion = f"{suggestion}（{note}）"
        card = HandoffCard(
            handoff_id=turn.turn_id, tenant_id=conv.tenant_id,
            conversation_id=conv.conversation_id, turn_id=turn.turn_id,
            channel=msg.channel, reason=reason, intent=intent,
            customer_ref=mask_customer(msg.chat_id), customer_text=msg.text or "",
            recent_turns=tuple(earlier) + ((msg.text or "", reply),),
            suggestion=suggestion, citations=tuple(citations), slots=(), created_at=now)
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
            except Exception:                             # noqa: BLE001
                log.error("客服前台出错后静默轮也没落下（客户 %s）", who, exc_info=True)
            return DeskResult(reply_text="", tenant_id=turn.tenant_id,
                              conversation_id=turn.conv.conversation_id, turn_id=turn.turn_id,
                              route=ROUTE_SILENT, intent=INTENT_UNKNOWN,
                              draft=ReplyDraft(text=""))

        reply = REPLY_INTERNAL_ERROR
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
            reply = REPLY_DESK_UNAVAILABLE
        draft = ReplyDraft(text=reply)
        check = CheckResult(ok=True)
        if not turn.turn_recorded:
            try:
                turn.conv = conversation.record_turn(
                    self.store, turn.conv, turn_id=turn.turn_id, seq=turn.seq,
                    msg_dedup_key=msg.dedup_key, inbound_text=msg.text or "",
                    reply_text=reply, route=ROUTE_HANDOFF,
                    intent=turn.intent if turn.intent in INTENTS else INTENT_UNKNOWN,
                    handoff_reason=turn.reason or HANDOFF_UNVERIFIED_CLAIM,
                    draft=draft, check=check, now=now)
                turn.turn_recorded = True
            except Exception:                             # noqa: BLE001
                log.error("客服前台出错后本轮没落下（客户 %s）", who, exc_info=True)
        return self._error_result(turn, card=card, reply=reply)

    @staticmethod
    def _error_result(turn: _Turn, *, card: HandoffCard | None,
                      reply: str = REPLY_DESK_UNAVAILABLE) -> DeskResult:
        conv_id = turn.conv.conversation_id if turn.conv is not None else ""
        return DeskResult(reply_text=reply, tenant_id=turn.tenant_id, conversation_id=conv_id,
                          turn_id=turn.turn_id, route=ROUTE_HANDOFF,
                          intent=turn.intent if turn.intent in INTENTS else INTENT_UNKNOWN,
                          draft=ReplyDraft(text=reply), handoff=card,
                          handoff_reason=turn.reason or HANDOFF_UNVERIFIED_CLAIM,
                          check=CheckResult(ok=True))


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
    lines.append(f"处理建议：{_inline(card.suggestion)}")
    if card.citations:
        lines.append(f"引用话术：{'、'.join(card.citations)}")
    lines.append(f"会话：{card.conversation_id} · 轮次：{card.turn_id}"
                 + (f" · 租户：{card.tenant_id}" if card.tenant_id else " · 租户：（未绑定）"))
    return "\n".join(lines)
