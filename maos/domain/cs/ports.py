"""客服前台 p13 的冻结端口与常量（p13 跨轨契约 §1.2 的代码形态）。

前台（``maos/domain/cs/**``、``maos/skills/builtin/cs/**``）**一个工具都不碰** —— p12 的
静态守卫不放宽。p13 要做的查单与退款预检，实现在扫描范围外（``maos/ingress/cs_ports.py``），
由装配处**注入**进前台。本模块只定义缝的形状：三个 Protocol、三个结果类型、几张冻结的表。

零依赖（只用标准库），理由同 ``types.py``：五条轨都 import 它。
改它要回主会话改契约 review/p13-cs-contracts.md，同一个提交里改
``maos/tests/test_cs_contract_p13.py``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# 语种、槽位
# ---------------------------------------------------------------------------
LANG_ZH = "zh"
LANG_EN = "en"
LANGS = (LANG_ZH, LANG_EN)

#: 参数提取的五个槽位（ADP 课程 3.3 参数提取节点的 MAOS 版本）。
SLOT_ORDER_NO = "order_no"
SLOT_PRODUCT = "product"
SLOT_PROBLEM = "problem"
SLOT_REQUEST = "request"
SLOT_EMOTION = "emotion"
SLOT_KEYS = (SLOT_ORDER_NO, SLOT_PRODUCT, SLOT_PROBLEM, SLOT_REQUEST, SLOT_EMOTION)

#: ``request`` 槽位的闭集取值。
REQUEST_REFUND = "refund"
REQUEST_RETURN = "return"
REQUEST_EXCHANGE = "exchange"
REQUEST_TRACK = "track"
REQUEST_OTHER = "other"
REQUEST_VALUES = (REQUEST_REFUND, REQUEST_RETURN, REQUEST_EXCHANGE, REQUEST_TRACK, REQUEST_OTHER)

#: ``emotion`` 槽位的闭集取值。
EMOTION_CALM = "calm"
EMOTION_UPSET = "upset"
EMOTION_ANGRY = "angry"
EMOTION_VALUES = (EMOTION_CALM, EMOTION_UPSET, EMOTION_ANGRY)

#: 槽位来源。
SLOT_SOURCE_RULE = "rule"
SLOT_SOURCE_MODEL = "model"
SLOT_SOURCES = (SLOT_SOURCE_RULE, SLOT_SOURCE_MODEL)

#: 同一槽位最多追问几次，第 N+1 次仍缺就转人工。
MAX_ASKS_PER_SLOT = 2

# ---------------------------------------------------------------------------
# 订单状态与对外措辞
# ---------------------------------------------------------------------------
#: 与 ``maos.tools.order.ALL_ORDER_STATUSES`` 逐字相等（契约测试钉）。
#: 这里抄字面量是为了本模块零依赖、也为了 cs 扫描范围不 import maos.tools。
ORDER_PAID = "paid"
ORDER_SHIPPED = "shipped"
ORDER_CANCELLED = "cancelled"
ORDER_AMENDED = "amended"
ORDER_STATUSES = (ORDER_PAID, ORDER_SHIPPED, ORDER_CANCELLED, ORDER_AMENDED)

#: 订单状态的**唯一**对外措辞。表里没有的状态（amended、平台不映射）一律不说、转人工。
#: 「已签收」永远不说：各平台把签收 / 完成都折进 shipped，撑不住这句。
#: 金额、到账 / 送达时间也永远不说。
ORDER_STATUS_WORDING: dict[str, dict[str, str]] = {
    LANG_ZH: {
        ORDER_PAID: "您的订单已付款，暂未发货",
        ORDER_SHIPPED: "您的订单已发货（如有多件，可能分批发出）",
        ORDER_CANCELLED: "您的订单已取消",
    },
    LANG_EN: {
        ORDER_PAID: "Your order is paid and has not shipped yet.",
        ORDER_SHIPPED: "Your order has shipped (multi-item orders may ship in parts).",
        ORDER_CANCELLED: "Your order has been cancelled.",
    },
}

# ---------------------------------------------------------------------------
# 查单结果
# ---------------------------------------------------------------------------
LOOKUP_OK = "ok"
LOOKUP_NOT_FOUND = "not_found"
LOOKUP_UNMAPPED = "unmapped_status"
LOOKUP_AMENDED = "amended"
LOOKUP_MISCONFIGURED = "system_misconfigured"
LOOKUP_PLATFORM_ERROR = "platform_error"
LOOKUP_OUTCOMES = (LOOKUP_OK, LOOKUP_NOT_FOUND, LOOKUP_UNMAPPED, LOOKUP_AMENDED,
                   LOOKUP_MISCONFIGURED, LOOKUP_PLATFORM_ERROR)

#: 观察行的种类（cs_observation.kind）。
OBS_ORDER_LOOKUP = "order_lookup"
OBS_KINDS = (OBS_ORDER_LOOKUP,)

#: 绑定行的来源：只有内部路径写得进来，外部渠道永远不行。
BINDING_SEED = "seed"
BINDING_TEST = "test"
BINDING_INTERNAL = "internal"
BINDING_SOURCES = (BINDING_SEED, BINDING_TEST, BINDING_INTERNAL)


def observation_id_for(turn_id: str, n: int) -> str:
    """观察 id：``<turn_id>-o<两位序号>``，序号从 1 起。「本轮的观察」由此在结构上可判。"""
    if n < 1:
        raise ValueError(f"观察序号从 1 起，收到 {n}")
    return f"{turn_id}-o{n:02d}"


@dataclass(frozen=True)
class Binding:
    """绑定表一行：这位客户可以查这一单。**是授权，不是订单事实**（铁律 8）。"""
    tenant_id: str
    channel: str
    external_userid: str
    display_no: str      # 客户看得到、会打出来的订单号
    system_name: str     # 已登记的订单系统名
    query_key: str       # 查单用的键（Shopify 用内部 id，不等于客户看到的单号）
    source: str
    bound_at: str = ""


@dataclass(frozen=True)
class LookupResult:
    """一次只读查单的结果。**不含异常原文**（原文里列着别的订单号）。

    ``outcome == 'ok'`` 时 ``status`` 必在 ORDER_STATUSES 里；其余情况 status 为空，
    ``error_kind`` 是异常类名（只类名、不带消息）。
    """
    outcome: str
    system_name: str
    query_key: str
    status: str = ""
    version: int = 0
    updated_at: str = ""
    error_kind: str = ""


@dataclass(frozen=True)
class PrecheckResult:
    """退款只读预检的结果（给内部卡片用；客户侧一个字都不转述）。

    ``ok`` 为 True 时 ``command_line`` 是一行现成的 ``/refund <订单号> <原因>``，
    由内部同事**以自己的名义**发出去才生成工单；前台永远不自己发。
    """
    ok: bool
    decision: str = ""
    rule_ref: str = ""
    reason_code: str = ""
    command_line: str = ""
    summary: str = ""
    refused_why: str = ""


@runtime_checkable
class IdentityVerifier(Protocol):
    """这位客户能不能查这一单。失败即关：查不到绑定就返回 None，前台不查单、转人工。"""

    def resolve(self, store: Any, *, tenant_id: str, channel: str, external_userid: str,
                display_no: str) -> Binding | None: ...


@runtime_checkable
class OrderLookup(Protocol):
    """只读查一单。恰好一次 order.query（经 invoke_tool，落一行 ToolInvoked）；永不抛。"""

    def lookup(self, store: Any, binding: Binding, *, plan_id: str,
               task_id: str) -> LookupResult: ...


@runtime_checkable
class RefundPrecheck(Protocol):
    """退款只读预检。永不抛；条件不满足就 ok=False 并写 refused_why。"""

    def precheck(self, *, tenant_id: str, order_no: str, reason_text: str,
                 now: str) -> PrecheckResult: ...
