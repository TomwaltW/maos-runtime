"""支付网关 ToolPort —— 退款的发起与查询，以及「观察与推断分离」的落点。

## 这一层存在的理由（铁律 8）

订单、支付、退款的**权威状态永远在支付宝那边**，不在 MAOS 里。MAOS 能做的只有两件事：
发一个请求（``refund``）、问一次结果（``query``）。**任何把外部状态直接写死为终态的
代码都是 bug** —— 包括「调用没抛异常所以退款成功了」这种看起来无害的推断。

所以本模块有一条硬规矩：``refund()`` **永远不返回终态**。

    真实时序：refund() -> processing（受理了，还没结算）
              query()  -> processing … -> settled（这才是终态）

一步返回 ``settled`` 的 mock 会把整个论证抽空 —— 那样 ``payment.observe`` 就没有
存在理由了，评委问「你怎么知道退款成功了」只能答「因为我的 mock 这么写的」。

## 第三种回执：unknown

比「还没结算」更要紧的是「**网关自己也说不清**」。``ACQ.SYSTEM_ERROR`` 的官方
解决方案原文是「保持参数不变重试**或查询执行结果**」，``code=20000`` 是「业务系统
暂不可用」—— 这两种情况下退款**可能已经发生了**，只是回执没拿到。

于是回执状态有四态，其中只有两个是终态：

    processing  受理了，处理中          （非终态）
    unknown     网关说不清，结果未知    （非终态，**必须 query**）
    settled     确定成功                （终态）
    failed      确定失败                （终态）

``unknown`` 时直接重发就可能产生第二笔退款，这是本模块防的第一号事故。
判据不要自己写，用 ``gateway_codes.needs_query_before_retry()``。

## 幂等

商户侧的 ``idempotency_key`` 对应支付宝的 ``out_request_no``（退款请求号）——
这不是我们发明的机制，是支付宝原生的幂等键。同一个 key 重复调 ``refund()``：

  · 参数一致 -> 原样返回**同一笔**的当前回执，不新建第二笔
  · 参数不一致 -> 返回 ``ACQ.DISCORDANT_REPEAT_REQUEST``，且 outcome 是 **unknown**
    （官方 remedy 里有「或查询历史执行结果」—— 之前那一笔可能已经成功了）

## 调用一律走 invoke_tool()

直接调 ``MockGateway.refund()`` 就没有 ToolInvoked 审计行，出事之后查不到是谁、
什么参数、跑了多久。上层请走 ``invoke_tool(GATEWAY_REFUND_PORT, {...}, store=...)``。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from maos.tools.gateway_codes import (
    DISCORDANT_REPEAT_REQUEST,
    OUTCOME_FAILED,
    OUTCOME_SUCCESS,
    OUTCOME_UNKNOWN,
    SUCCESS,
    GatewayCode,
    lookup,
)
from maos.tools.port import ToolPort

log = logging.getLogger("maos.tools.gateway")


# 回执状态。终态只有两个 —— 见 TERMINAL_STATUSES。
STATUS_PROCESSING = "processing"
STATUS_UNKNOWN = "unknown"
STATUS_SETTLED = "settled"
STATUS_FAILED = "failed"

#: 终态集合。``refund()`` 的返回值**永远不在这里面**，测试对这条有断言。
TERMINAL_STATUSES = frozenset({STATUS_SETTLED, STATUS_FAILED})

#: mock 默认轮询几次到终态。>1 才能证明「一次 query 不一定够」。
DEFAULT_SETTLE_AFTER = 2


@dataclass(frozen=True)
class RefundRequest:
    """一笔退款请求。字段名对齐支付宝 alipay.trade.refund 的入参语义。"""

    out_trade_no: str
    """商户侧原交易号。mock 用它来挑要注入哪个错误码。"""

    refund_amount: str
    """退款金额。用字符串不用 float —— 金额永远不进浮点。"""

    idempotency_key: str
    """幂等键，对应支付宝的 ``out_request_no``（退款请求号）。"""

    reason: str = ""

    def fingerprint(self) -> tuple[str, str]:
        """幂等比对面：同一个 key 下，这两项变了就算「重复请求不一致」。

        只比对交易号与金额，不比 reason —— 退款理由改了不影响资金结果，
        按支付宝语义那不算参数不一致。
        """
        return (self.out_trade_no, self.refund_amount)


@dataclass(frozen=True)
class GatewayReceipt:
    """网关回执 —— **一次观察的记录，不是事实本身**。

    frozen=True 是有意的：回执代表「某一时刻网关说了什么」，改它等于篡改观察记录。
    状态推进请用 ``replace()`` 产生新回执，旧的留在审计里。
    """

    request_id: str
    """网关侧请求 id，``query()`` 用它。"""

    idempotency_key: str
    status: str
    """processing / unknown / settled / failed。见模块 docstring。"""

    code: str
    """官方错误码。成功是 "10000"。"""

    message: str
    retriable: bool
    outcome: str
    """业务到底执行了没有：success / failed / unknown。与 status 不是一回事 ——
    status 是「这次观察看到什么」，outcome 是「那一笔的下落」。"""

    remedy: str
    """官方给的处置建议原文，直接进 findings 给人看。"""

    source: str
    """错误码出处。回执里带着它，评委问「这个码哪来的」当场能答。"""

    poll_count: int = 0
    """已经 query 过几次。审计用：证明终态是**问出来的**，不是猜出来的。"""

    detail: dict = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def needs_query(self) -> bool:
        """还需不需要再问一次。非终态一律要问，``unknown`` 尤其不许跳过。"""
        return not self.is_terminal

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "idempotency_key": self.idempotency_key,
            "status": self.status,
            "code": self.code,
            "message": self.message,
            "retriable": self.retriable,
            "outcome": self.outcome,
            "remedy": self.remedy,
            "source": self.source,
            "poll_count": self.poll_count,
            "is_terminal": self.is_terminal,
            "detail": dict(self.detail),
        }


def _receipt(request_id: str, key: str, status: str, code: GatewayCode,
             *, poll_count: int = 0, detail: dict | None = None) -> GatewayReceipt:
    """由码表条目造回执 —— retriable / outcome / remedy / source 全部**从码表来**，
    不在调用处手填，手填就会和官方文档分叉。"""
    return GatewayReceipt(
        request_id=request_id,
        idempotency_key=key,
        status=status,
        code=code.code,
        message=code.message,
        retriable=code.retriable,
        outcome=code.outcome,
        remedy=code.remedy,
        source=code.source,
        poll_count=poll_count,
        detail=dict(detail or {}),
    )


class GatewayPort(Protocol):
    """支付网关的两个动作。签名冻结，上层只按这两个签名写代码。"""

    def refund(self, request: RefundRequest) -> GatewayReceipt:
        """发起退款。**返回值永远不是终态** —— 见模块 docstring。"""
        ...

    def query(self, request_id: str) -> GatewayReceipt:
        """查一笔退款的当前状态。终态只能从这里来。"""
        ...


@dataclass
class _Entry:
    """账本里的一笔。mock 的「已经发生过的事」都记在这里。"""

    request_id: str
    request: RefundRequest
    code: GatewayCode
    """这一笔最终会落到哪个码（成功是 SUCCESS）。"""

    polls: int = 0
    settled: bool = False


class MockGateway:
    """演示用网关 —— 错误码与异步时序都对齐支付宝开放平台官方文档。

    它是 mock，但**时序不是假的**：``refund()`` 一定给非终态，终态一定要
    ``query()`` 问出来。这条时序是「观察与推断分离」能成立的前提，
    换成一步到位的 mock，整个论证就没了。

    错误注入按 ``out_trade_no`` 走 ``script``，让 R-2 的场景可以用特定订单号
    稳定触发特定错误码，不依赖随机数（确定性回放是 D 轨定下的口径）。
    """

    def __init__(self, *, settle_after: int = DEFAULT_SETTLE_AFTER,
                 script: dict[str, str] | None = None) -> None:
        if settle_after < 1:
            raise ValueError("settle_after 至少为 1 —— 退款不允许一步到终态")
        self.settle_after = settle_after
        #: out_trade_no -> 错误码。码必须在码表里，构造时就校验，不留到调用时才炸。
        self.script = dict(script or {})
        for trade_no, code in self.script.items():
            lookup(code)                      # 未知码在这里就抛，见 gateway_codes.lookup
        self._ledger: dict[str, _Entry] = {}  # idempotency_key -> 账本
        self._by_request: dict[str, str] = {}  # request_id -> idempotency_key

    def __repr__(self) -> str:
        # 不带内存地址：这个对象会进 invoke_tool 的 params_digest，
        # 带地址会让同样参数每次算出不同的 digest，审计就对不上了。
        return f"MockGateway(settle_after={self.settle_after}, scripted={len(self.script)})"

    @property
    def refund_count(self) -> int:
        """一共产生了几笔退款。幂等测试断言的就是它。"""
        return len(self._ledger)

    def refund(self, request: RefundRequest) -> GatewayReceipt:
        key = request.idempotency_key
        if not key:
            raise ValueError("退款请求必须带 idempotency_key（对应支付宝 out_request_no）")

        existing = self._ledger.get(key)
        if existing is not None:
            # —— 重复请求：无论如何都**不新建第二笔** ——
            if existing.request.fingerprint() != request.fingerprint():
                # 参数不一致。注意 outcome 是 unknown 不是 failed：
                # 之前那一笔可能已经成功了，官方 remedy 明写「或查询历史执行结果」。
                log.warning("幂等键 %s 重复且参数不一致，返回 %s",
                            key, DISCORDANT_REPEAT_REQUEST.code)
                return _receipt(existing.request_id, key, STATUS_UNKNOWN,
                                DISCORDANT_REPEAT_REQUEST,
                                poll_count=existing.polls,
                                detail={"duplicate_of": existing.request_id})
            # 参数一致：原样返回当前观察，不推进状态、不新建。
            return self._observe(existing, advance=False)

        code = lookup(self.script.get(request.out_trade_no, SUCCESS.code))
        entry = _Entry(request_id=f"gw_{uuid.uuid4().hex[:16]}", request=request, code=code)
        self._ledger[key] = entry
        self._by_request[entry.request_id] = key

        # 终态失败的码（如 TRADE_NOT_EXIST）网关当场就能判，不用等轮询。
        # 但**成功不能当场判** —— 那是异步的，必须 query。
        if code.outcome == OUTCOME_FAILED and code.code != SUCCESS.code:
            entry.settled = True
            return _receipt(entry.request_id, key, STATUS_FAILED, code)

        if code.outcome == OUTCOME_UNKNOWN:
            # 网关说不清。这不是终态，上层必须 query 才能知道下落。
            return _receipt(entry.request_id, key, STATUS_UNKNOWN, code)

        # 正常路径：受理，处理中。**这里绝不能返回 settled。**
        return _receipt(entry.request_id, key, STATUS_PROCESSING, code)

    def query(self, request_id: str) -> GatewayReceipt:
        key = self._by_request.get(request_id)
        if key is None:
            raise KeyError(f"未知 request_id：{request_id!r}")
        return self._observe(self._ledger[key], advance=True)

    def _observe(self, entry: _Entry, *, advance: bool) -> GatewayReceipt:
        """产出一次观察。``advance=True`` 才推进轮询计数（只有 query 会推进）。"""
        if advance and not entry.settled:
            entry.polls += 1
            if entry.polls >= self.settle_after:
                entry.settled = True

        code, key = entry.code, entry.request.idempotency_key

        if code.outcome == OUTCOME_FAILED and code.code != SUCCESS.code:
            return _receipt(entry.request_id, key, STATUS_FAILED, code,
                            poll_count=entry.polls)

        if not entry.settled:
            # 还没到终态。unknown 码在轮询期间仍然报 unknown，
            # 不许在这里「乐观」地当成 processing —— 那就是在推断。
            status = STATUS_UNKNOWN if code.outcome == OUTCOME_UNKNOWN else STATUS_PROCESSING
            return _receipt(entry.request_id, key, status, code, poll_count=entry.polls)

        # 到终态了。轮询轮到头之后，unknown 的那一笔也有了确定下落。
        return _receipt(entry.request_id, key, STATUS_SETTLED, SUCCESS,
                        poll_count=entry.polls,
                        detail={"resolved_from": code.code} if code.code != SUCCESS.code else {})


class AlipaySandboxAdapter:
    """真支付宝沙箱适配层 —— **本轮只留壳**（派单：时间盒任务，通了就切）。

    签名与 ``MockGateway`` 完全一致，切换时上层一行不用改。

    每个方法都显式 ``raise NotImplementedError``，**不静默返回假数据** ——
    一个「看起来返回了点什么」的桩会让上层以为接通了，那比没实现危险得多。
    """

    def __init__(self, *, app_id: str = "", gateway_url: str = "",
                 private_key: str = "") -> None:
        self.app_id = app_id
        self.gateway_url = gateway_url
        # 私钥走私有属性 + 自定义 __repr__，两道防线 —— 与 model/client.py 的
        # GatewayModelClient 同口径，避免密钥进 repr / pytest 对象打印 / traceback。
        self._private_key = private_key

    def __repr__(self) -> str:
        return f"AlipaySandboxAdapter(app_id={self.app_id!r}, gateway_url={self.gateway_url!r})"

    def refund(self, request: RefundRequest) -> GatewayReceipt:
        raise NotImplementedError(
            "AlipaySandboxAdapter.refund 尚未接通支付宝沙箱："
            "本轮为时间盒任务，演示使用 MockGateway（错误码与时序对齐官方文档）"
        )

    def query(self, request_id: str) -> GatewayReceipt:
        raise NotImplementedError(
            "AlipaySandboxAdapter.query 尚未接通支付宝沙箱："
            "本轮为时间盒任务，演示使用 MockGateway（错误码与时序对齐官方文档）"
        )


# ---------------------------------------------------------------------------
# 两个 ToolPort 声明（A-6 九要素）—— 调用一律走 invoke_tool()，直接调没有审计行
# ---------------------------------------------------------------------------

def gateway_refund(*, gateway: Any, out_trade_no: str, refund_amount: str,
                   idempotency_key: str, reason: str = "") -> dict:
    """ToolPort 入口：发起退款，返回回执 dict。

    入参摊平成基本类型而不是收一个 RefundRequest 对象：``invoke_tool`` 会把 params
    做 sha256 进审计行，摊平之后 digest 才对得上「同样的参数」这个直觉。
    """
    req = RefundRequest(out_trade_no=out_trade_no, refund_amount=refund_amount,
                        idempotency_key=idempotency_key, reason=reason)
    return gateway.refund(req).to_dict()


def gateway_query(*, gateway: Any, request_id: str) -> dict:
    """ToolPort 入口：查一笔退款的当前状态。终态只能从这里来。"""
    return gateway.query(request_id).to_dict()


GATEWAY_REFUND_PORT = ToolPort(
    name="gateway.refund",
    purpose="向支付网关发起退款；返回受理回执，**不返回终态**（终态须经 gateway.query 观察）",
    entry=gateway_refund,
    params_schema={"gateway": "GatewayPort", "out_trade_no": "str",
                   "refund_amount": "str（金额不进浮点）", "idempotency_key": "str",
                   "reason": "str（可选）"},
    returns_schema={"request_id": "str", "status": "processing|unknown（非终态）",
                    "code": "str", "retriable": "bool", "outcome": "success|failed|unknown",
                    "remedy": "str", "source": "str（错误码出处）", "is_terminal": "bool"},
    failure_modes=[
        "ValueError: 缺 idempotency_key（对应支付宝 out_request_no）",
        "status=unknown: 网关说不清结果（ACQ.SYSTEM_ERROR / code 20000）——"
        "**不许在本地推断成败**，必须 gateway.query",
        "status=failed: 明确失败（ACQ.TRADE_NOT_EXIST / ACQ.SELLER_BALANCE_NOT_ENOUGH 等）",
        "code=ACQ.DISCORDANT_REPEAT_REQUEST: 同幂等键参数不一致，前一笔下落未知",
        "NotImplementedError: 用了 AlipaySandboxAdapter 而沙箱未接通",
    ],
    security_boundary=(
        "MAOS 不持有退款的权威事实（铁律 8），本工具只产生**观察记录**："
        "refund 永不返回终态，终态一律经 query 取得；"
        "同一 idempotency_key 不产生第二笔退款；"
        "错误码判据全部取自 gateway_codes 的已核对官方表，未知码抛 KeyError 不兜底"
    ),
    rate_limit="",
    owner="task-r3",
)

GATEWAY_QUERY_PORT = ToolPort(
    name="gateway.query",
    purpose="查询一笔退款在支付网关侧的当前状态 —— 终态的唯一合法来源",
    entry=gateway_query,
    params_schema={"gateway": "GatewayPort", "request_id": "str"},
    returns_schema={"status": "processing|unknown|settled|failed",
                    "poll_count": "int（问过几次，证明终态是问出来的）",
                    "outcome": "success|failed|unknown", "is_terminal": "bool"},
    failure_modes=[
        "KeyError: 未知 request_id",
        "status 仍为 processing/unknown: 还没到终态，继续轮询，**不许当成失败**",
        "NotImplementedError: 用了 AlipaySandboxAdapter 而沙箱未接通",
    ],
    security_boundary=(
        "只读观察，不改变网关侧任何状态；"
        "轮询次数落在回执的 poll_count 上，审计可证明终态来自观察而非本地推断"
    ),
    rate_limit="",
    owner="task-r3",
)
