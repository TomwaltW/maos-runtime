"""客服前台的冻结类型与常量（p12 跨轨契约 §1 的代码形态）。

四条轨都照本文件写，**谁都不改它**：要改，回主会话改契约 review/p12-cs-contracts.md，
同一个提交里改这里和 ``maos/tests/test_cs_contract_p12.py``。字段名、枚举值、id 的
拼法三样一起冻 —— 并行轨各自写对了、合起来对不上，症状是整合期才红。

本模块零依赖（只用标准库）：会话表、话术检索、后置校验、前台编排都 import 它，
它要是反过来 import 任何一个，四条轨就有了环。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# 标识与归属
# ---------------------------------------------------------------------------
#: 会话轮次落 ``event_log`` 时的 plan_id 前缀。先例是圆桌的 ``roundtable:``
#: （``maos/obs/trace.py`` 的 ROUNDTABLE_PLAN_PREFIX）：不建 Plan 的活动也要能按前缀
#: 一眼认出是谁的。**只进 event_log 的 plan_id**：``model_usage.trace_id`` 恒为空串
#: （非空而查不到 plan 会让 verify 第 8 项判负），``task_id`` 在 model_usage 上恒为 None。
CS_PLAN_PREFIX = "cs:"

#: 话术文档在 ``kb_doc.biz_type`` 上的取值。**必须非空**：文档侧 NULL 是通配，
#: 空着的话术会混进每一次退款检索的候选集；反过来退款检索恒带 biz_type='refund'，
#: 于是两边互相检不到对方。
BIZ_TYPE_CS = "cs"

#: 话术的 KB kind。``maos/kb/__init__.py`` 里的常量（T168 加）必须与它逐字相等。
CS_KB_KIND = "cs_script"

#: 首发的外部渠道。与 ``maos.ingress.contracts.CHANNEL_WECHAT_KF`` 逐字相等（契约测试钉），
#: 这里抄一份字面量是为了本模块零依赖。
CHANNEL_WECHAT_KF = "wechat_kf"


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def conversation_id_for(tenant_id: str, channel: str, open_kfid: str,
                        external_userid: str) -> str:
    """会话 id：``csc-`` + sha256(租户 ␟ 渠道 ␟ 客服账号 ␟ 客户)[:16]。

    带 open_kfid：同一个客户找两个客服账号是两段会话（回信必须用对应的那个账号，
    ``WeChatKfAdapter.send`` 缺它就抛）。取哈希而不是拼原文：这个 id 会进
    ``event_log.plan_id``，而 external_userid 是客户身份，不该明文躺在审计行里。
    租户映射不到时 tenant_id 是空串（不猜默认租户），照样算得出一个稳定的 id。
    """
    return "csc-" + _digest(tenant_id, channel, open_kfid, external_userid)[:16]


def turn_id_for(conversation_id: str, seq: int) -> str:
    """轮次 id：``<conversation_id>-t<四位序号>``，序号从 1 起、按会话递增。"""
    if seq < 1:
        raise ValueError(f"轮次序号从 1 起，收到 {seq}")
    return f"{conversation_id}-t{seq:04d}"


def plan_id_for(conversation_id: str) -> str:
    """本会话所有 event_log 行的 plan_id。"""
    return CS_PLAN_PREFIX + conversation_id


def text_digest(text: str) -> str:
    """进 event_log 的客户原文 / 回复原文一律只落这个（sha256 前 16 位）。

    原文住在 ``cs_turn`` 里 —— 那是会话对象本身、转人工卡片要带；审计行只要能对上号，
    口径同圆桌（``maos/roundtable/team.py``「原文一个字都不进 event_log」）。
    """
    return "sha256:" + _digest(text)[:16]


# ---------------------------------------------------------------------------
# 枚举（每个都是 p12 的全集；加值要改契约）
# ---------------------------------------------------------------------------
#: 会话阶段 —— 会话对象自己的字段（铁律 9），不是 Task 状态。
STAGE_ACTIVE = "active"            # 机器人在接待
STAGE_HANDED_OFF = "handed_off"    # 已转人工：机器人只记录、不再回话
STAGE_CLOSED = "closed"            # 结束（p12 没有路径走到这里，留给 p13/p14）
STAGES = (STAGE_ACTIVE, STAGE_HANDED_OFF, STAGE_CLOSED)

#: 合法迁移。``closed`` 是吸收态。
STAGE_FLOW: dict[str, frozenset[str]] = {
    STAGE_ACTIVE: frozenset({STAGE_HANDED_OFF, STAGE_CLOSED}),
    STAGE_HANDED_OFF: frozenset({STAGE_ACTIVE, STAGE_CLOSED}),
    STAGE_CLOSED: frozenset(),
}

#: 一轮走了哪条出口。
ROUTE_ANSWER = "answer"      # 命中话术，照标准话术回
ROUTE_FALLBACK = "fallback"  # 知识不足，兜底话术（不编）
ROUTE_HANDOFF = "handoff"    # 转人工：回一句过渡话术 + 出一张内部卡片
ROUTE_SILENT = "silent"      # 已转人工的会话：只记录，不回话
ROUTE_CLARIFY = "clarify"    # p13：追问必填槽位（不计入 fallback_streak）
ROUTES = (ROUTE_ANSWER, ROUTE_FALLBACK, ROUTE_HANDOFF, ROUTE_SILENT, ROUTE_CLARIFY)

#: 顶层售后意图（ADP 3.3 的四个分支 + 通用 + 四类必转 + 判不准）。
INTENT_LOGISTICS = "logistics"              # 物流
INTENT_REFUND_PAYMENT = "refund_payment"    # 支付 / 退款
INTENT_RETURN_EXCHANGE = "return_exchange"  # 退换货
INTENT_GENERAL = "general"                  # 寒暄、感谢、通用信息
INTENT_HANDOFF_REQUEST = "handoff_request"  # 客户点名要人工
INTENT_COMPLAINT = "complaint"              # 投诉升级 / 情绪激烈
INTENT_COMPENSATION = "compensation"        # 赔偿 / 补偿诉求
INTENT_PRIVACY = "privacy"                  # 个人信息
INTENT_UNKNOWN = "unknown"                  # 判不准
INTENTS = (INTENT_LOGISTICS, INTENT_REFUND_PAYMENT, INTENT_RETURN_EXCHANGE, INTENT_GENERAL,
           INTENT_HANDOFF_REQUEST, INTENT_COMPLAINT, INTENT_COMPENSATION, INTENT_PRIVACY,
           INTENT_UNKNOWN)

#: 转人工原因。
HANDOFF_REQUESTED = "requested"                  # 客户要人工
HANDOFF_COMPLAINT = "complaint"                  # 投诉升级
HANDOFF_ANGER = "anger"                          # 情绪激烈
HANDOFF_COMPENSATION = "compensation"            # 赔偿 / 补偿（金额只有人能谈）
HANDOFF_PRIVACY = "privacy"                      # 隐私
HANDOFF_NEEDS_ORDER_LOOKUP = "needs_order_lookup"  # 要看具体订单 —— p12 不查单
HANDOFF_UNVERIFIED_CLAIM = "unverified_claim"    # 后置校验拦下了状态断言 / 悬空引用
HANDOFF_REPEATED_FALLBACK = "repeated_fallback"  # 连续兜底 = 判不准
HANDOFF_TENANT_UNMAPPED = "tenant_unmapped"      # 客服账号没绑定租户
# p13 增量（review/p13-cs-contracts.md §1.1）：查单与退款桥各自的出口，不再一律并进 needs_order_lookup。
HANDOFF_IDENTITY_UNVERIFIED = "identity_unverified"  # 订单号没绑定到这位客户：不查单
HANDOFF_ORDER_UNMAPPED = "order_unmapped"        # 平台状态不映射 / amended：状态说不准
HANDOFF_LOOKUP_FAILED = "lookup_failed"          # 查不到、平台出错、系统没配
HANDOFF_REFUND_REQUEST = "refund_request"        # 退款预检卡已出，等内部同事采纳
HANDOFF_REASONS = (HANDOFF_REQUESTED, HANDOFF_COMPLAINT, HANDOFF_ANGER, HANDOFF_COMPENSATION,
                   HANDOFF_PRIVACY, HANDOFF_NEEDS_ORDER_LOOKUP, HANDOFF_UNVERIFIED_CLAIM,
                   HANDOFF_REPEATED_FALLBACK, HANDOFF_TENANT_UNMAPPED,
                   HANDOFF_IDENTITY_UNVERIFIED, HANDOFF_ORDER_UNMAPPED, HANDOFF_LOOKUP_FAILED,
                   HANDOFF_REFUND_REQUEST)

#: 连续几轮兜底就转人工（第 N 轮本身走 handoff，原因 repeated_fallback）。
FALLBACK_STREAK_HANDOFF = 2

#: 转人工卡片带最近几轮对话（含本轮）。
CARD_RECENT_TURNS = 5

#: 转人工卡片的投递状态 —— 卡片自己的字段，不是 Task 状态。
DELIVERY_PENDING = "pending"
DELIVERY_DELIVERED = "delivered"
DELIVERY_FAILED = "failed"
DELIVERY_UNCONFIGURED = "unconfigured"   # 没配 MAOS_CS_HANDOFF_TARGET：卡片只落库
DELIVERIES = (DELIVERY_PENDING, DELIVERY_DELIVERED, DELIVERY_FAILED, DELIVERY_UNCONFIGURED)

# ---------------------------------------------------------------------------
# event_log 事件名（字符串 event_type，不是 Topic；maos/contracts/** 一个字不动）
# ---------------------------------------------------------------------------
EVENT_TURN_RECORDED = "CsTurnRecorded"              # 每轮恰好一条
EVENT_STAGE_CHANGED = "CsConversationStageChanged"  # 每次阶段迁移一条
EVENT_HANDOFF_RAISED = "CsHandoffRaised"            # 每张卡片一条
EVENT_REPLY_REJECTED = "CsReplyRejected"            # 后置校验拦下一版回复时一条
CS_EVENT_TYPES = (EVENT_TURN_RECORDED, EVENT_STAGE_CHANGED, EVENT_HANDOFF_RAISED,
                  EVENT_REPLY_REJECTED)

# ---------------------------------------------------------------------------
# 状态字眼（契约 §1.5，后置校验的扫描对象）
# ---------------------------------------------------------------------------
#: 回复里出现这些，就必须有一条 claim 覆盖它、且 basis_ref 指向**本轮**的观察。
#: 顺序无意义；「预计 N 天」按正则认（阿拉伯数字或中文数字，天 / 日 / 个工作日 / 小时）。
STATUS_WORDS = ("已到账", "已退款", "已发货", "已签收", "已取消", "赔偿", "补偿")
STATUS_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(re.escape(w)) for w in STATUS_WORDS
) + (
    re.compile(r"预计\s*[0-9０-９一二三四五六七八九十两半]+\s*(?:个)?\s*(?:工作日|天|日|小时)"),
)

# ---------------------------------------------------------------------------
# 回复草稿与引用
# ---------------------------------------------------------------------------
#: basis_ref 的两种前缀。``obs:<observation_id>`` 指本轮的一条观察行（p12 没有产出者，
#: p13 由查单结果落 cs 自己的观察表）；``kb:<doc_id>`` 指本轮 KbRetrieved 命中的一篇话术。
#: 状态字眼**只认 obs:**，kb: 只能撑规则引用。
BASIS_OBS = "obs:"
BASIS_KB = "kb:"


@dataclass(frozen=True)
class Claim:
    """回复里的一句断言：``literal`` 必须是 ``ReplyDraft.text`` 的子串。"""
    literal: str
    basis_ref: str

    def to_json(self) -> dict[str, str]:
        return {"literal": self.literal, "basis_ref": self.basis_ref}

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Claim":
        return cls(literal=str(d["literal"]), basis_ref=str(d["basis_ref"]))


@dataclass(frozen=True)
class ReplyDraft:
    """一版回复：客户看到的 ``text``、撑它的 ``claims``、引用的话术 ``citations``。

    ``citations`` 是 kb **doc_id**（不是方案编号）：verify 第 5 项与 KbRetrieved
    认的都是 doc_id，引用写成编号就得再 join 一次 kb_doc 才对得上。
    """
    text: str
    claims: tuple[Claim, ...] = ()
    citations: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {"text": self.text,
                "claims": [c.to_json() for c in self.claims],
                "citations": list(self.citations)}

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "ReplyDraft":
        return cls(text=str(d.get("text", "")),
                   claims=tuple(Claim.from_json(c) for c in d.get("claims") or ()),
                   citations=tuple(str(c) for c in d.get("citations") or ()))


@dataclass(frozen=True)
class Violation:
    """后置校验的一条违例。``kind`` 取 VIOLATION_KINDS。"""
    kind: str
    detail: str

    def to_json(self) -> dict[str, str]:
        return {"kind": self.kind, "detail": self.detail}


VIOLATION_UNBACKED_STATUS = "unbacked_status"   # 状态字眼没有本轮观察撑
VIOLATION_DANGLING_BASIS = "dangling_basis"     # claim 指向的观察 / 话术本轮不存在
VIOLATION_LITERAL_NOT_IN_TEXT = "literal_not_in_text"  # claim.literal 不是正文子串
VIOLATION_UNCITED_RULE = "uncited_rule"         # citations 里有本轮没检出的 doc_id
VIOLATION_FOREIGN_LITERAL = "foreign_literal"   # 退款状态没用 projection.py 的五个字面值
VIOLATION_KINDS = (VIOLATION_UNBACKED_STATUS, VIOLATION_DANGLING_BASIS,
                   VIOLATION_LITERAL_NOT_IN_TEXT, VIOLATION_UNCITED_RULE,
                   VIOLATION_FOREIGN_LITERAL)


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    violations: tuple[Violation, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {"ok": self.ok, "violations": [v.to_json() for v in self.violations]}


# ---------------------------------------------------------------------------
# 话术命中（T168 产出，T169 消费）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ScriptHit:
    """一篇被检出的话术。``score`` 在 [0, 1]，由 T168 的排序给出。

    ``handoff`` 非空表示这篇话术的处理原则就是「转人工」（例如要看具体订单），
    取值必在 HANDOFF_REASONS 里。
    """
    doc_id: str
    scheme_no: str
    intent: str
    score: float
    script: str
    principle: str
    handoff: str = ""


# ---------------------------------------------------------------------------
# 转人工卡片与前台一轮的结果
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class HandoffCard:
    """发到内部房间的转人工卡片。人工接手不再从头问：本轮原文、最近几轮、处理建议都在。

    ``customer_ref`` 是打码后的客户标识（external_userid 末 6 位前加 ``…``），
    完整标识在 ``cs_conversation`` 里；卡片会离开本库，所以只带能认出人、认不全的那段。
    ``slots`` p12 恒为空（槽位抽取是 p13），形状先冻上。
    """
    handoff_id: str
    tenant_id: str
    conversation_id: str
    turn_id: str
    channel: str
    reason: str
    intent: str
    customer_ref: str
    customer_text: str
    recent_turns: tuple[tuple[str, str], ...]
    suggestion: str
    citations: tuple[str, ...] = ()
    slots: tuple[tuple[str, str], ...] = ()
    created_at: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "handoff_id": self.handoff_id, "tenant_id": self.tenant_id,
            "conversation_id": self.conversation_id, "turn_id": self.turn_id,
            "channel": self.channel, "reason": self.reason, "intent": self.intent,
            "customer_ref": self.customer_ref, "customer_text": self.customer_text,
            "recent_turns": [list(t) for t in self.recent_turns],
            "suggestion": self.suggestion, "citations": list(self.citations),
            "slots": [list(s) for s in self.slots], "created_at": self.created_at,
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "HandoffCard":
        return cls(
            handoff_id=str(d["handoff_id"]), tenant_id=str(d["tenant_id"]),
            conversation_id=str(d["conversation_id"]), turn_id=str(d["turn_id"]),
            channel=str(d["channel"]), reason=str(d["reason"]), intent=str(d["intent"]),
            customer_ref=str(d["customer_ref"]), customer_text=str(d["customer_text"]),
            recent_turns=tuple((str(a), str(b)) for a, b in d.get("recent_turns") or ()),
            suggestion=str(d.get("suggestion", "")),
            citations=tuple(str(c) for c in d.get("citations") or ()),
            slots=tuple((str(k), str(v)) for k, v in d.get("slots") or ()),
            created_at=str(d.get("created_at", "")),
        )


def mask_customer(external_userid: str) -> str:
    """卡片与日志里用的客户标识：末 6 位，前面一个省略号。不足 6 位整段打星。"""
    s = external_userid or ""
    return "…" + s[-6:] if len(s) > 6 else "*" * len(s)


@dataclass(frozen=True)
class DeskResult:
    """前台处理完一条外部消息的结果（T169 产出；T170 的评测跑批按它比对）。

    ``reply_text`` 为空串只在 ``route == 'silent'`` 时出现。
    """
    reply_text: str
    tenant_id: str
    conversation_id: str
    turn_id: str
    route: str
    intent: str
    draft: ReplyDraft
    handoff: HandoffCard | None = None
    handoff_reason: str = ""
    check: CheckResult = field(default_factory=lambda: CheckResult(ok=True))
    # p13 增量（带缺省，p12 的构造处不用改）：本轮语种、查单结果、追问的是哪个槽位。
    lang: str = "zh"
    lookup_outcome: str = ""
    ask_slot: str = ""
