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

## params 里只放名字，不放实例

两个 ToolPort 收的是 ``gateway_name``，实例由本模块的注册表持有
（``register_gateway`` / ``get_gateway``）。params 全是标量，所以
``params_digest`` 天然可复现，也跨得了进程 —— 见注册表那一节的完整理由。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from maos.tools.gateway_codes import (
    DISCORDANT_REPEAT_REQUEST,
    LAYER_BUSINESS,
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
    """这一笔当下落在哪个码（成功是 SUCCESS）。

    绝大多数时候它建好就不再变。唯一会变的是 ``fail_times`` 注入用完那一次 ——
    见 ``MockGateway._supersede_due``：上一笔是「网关在入口就拒了、业务确定没执行」，
    重发把**同一笔请求**的下落改了，不是新开一笔。
    """

    polls: int = 0
    settled: bool = False

    attempts: int = 1
    """这个**幂等键**上受理过几次 refund 尝试（建账本算第 1 次）。

    挂在 entry 上而不是按 ``out_trade_no`` 记在网关上，有两个理由：

    1. 幂等重发走的是 ``refund()`` 里 ``existing`` 那一支，**不经过** ``_next_code``
       —— 计数原先记在那里，于是永远停在 1，``fail_times>=2`` 的改判判据
       （``attempts >= budget``）就永不成立：连发多少次都是注入的那个码。
       ``fail_times=1`` 碰巧不受影响（建账本那一次就把计数顶到 1），
       所以既有证据束没暴露它。
    2. 同一个订单可以有多个案子（多个 ``out_request_no``）。按订单号记的话
       甲案发过一次，乙案的**第一次**就直接吃掉了甲案用剩的额度。
    """


class MockGateway:
    """演示用网关 —— 错误码与异步时序都对齐支付宝开放平台官方文档。

    它是 mock，但**时序不是假的**：``refund()`` 一定给非终态，终态一定要
    ``query()`` 问出来。这条时序是「观察与推断分离」能成立的前提，
    换成一步到位的 mock，整个论证就没了。

    错误注入按 ``out_trade_no`` 走 ``script``，让 R-2 的场景可以用特定订单号
    稳定触发特定错误码，不依赖随机数（确定性回放是 D 轨定下的口径）。

    ``script`` 一个人只能演「永远失败」。``fail_times`` 补上第二种时序：**前 N 次
    落注入的码，第 N+1 次改判**（改判成什么由 ``after_fail`` 说，缺省是成功码）。
    没有它就演不出「机器返工重发一次」这件事 —— 同一个幂等键重发时账本原样返回
    上一次的观察，重试多少趟都是同一个失败，Trace 上只剩「试到次数耗尽」。

    改判**只在一格里安全**，构造时就把这条焊死（见 ``_validate_retry_injection``）：
    注入的码必须 ``retriable=True + outcome=failed``，即网关在入口就拒了、
    **业务确定没执行**。``outcome=unknown`` 的两格重发可能造出第二笔退款，
    那是本模块防的第一号事故，不许用注入选项绕过去（铁律 8）。
    """

    def __init__(self, *, settle_after: int = DEFAULT_SETTLE_AFTER,
                 script: dict[str, str] | None = None,
                 fail_times: dict[str, int] | None = None,
                 after_fail: dict[str, str] | None = None) -> None:
        if settle_after < 1:
            raise ValueError("settle_after 至少为 1 —— 退款不允许一步到终态")
        self.settle_after = settle_after
        #: out_trade_no -> 错误码。码必须在码表里，构造时就校验，不留到调用时才炸。
        self.script = dict(script or {})
        for trade_no, code in self.script.items():
            lookup(code)                      # 未知码在这里就抛，见 gateway_codes.lookup
        #: out_trade_no -> 前几次 refund 落 ``script`` 的码。不配就是「永远失败」。
        self.fail_times = {str(k): int(v) for k, v in (fail_times or {}).items()}
        #: out_trade_no -> 次数用完之后改判成哪个码。不配就是成功码。
        self.after_fail = {str(k): str(v) for k, v in (after_fail or {}).items()}
        self._validate_retry_injection()
        self._ledger: dict[str, _Entry] = {}  # idempotency_key -> 账本
        self._by_request: dict[str, str] = {}  # request_id -> idempotency_key
        # 尝试数记在 `_Entry.attempts` 上（按幂等键），不在这里按 out_trade_no 记 ——
        # 理由见 `_Entry.attempts` 的两条。

    def _validate_retry_injection(self) -> None:
        """把「失败 N 次后改判」的注入在构造时校验干净，不留到调用时才炸。

        第三条是铁律 8 的落点，也是本选项唯一危险的地方：同一个 ``out_request_no``
        上改判重发，只有在「网关入口拒了、业务确定没执行」那一格才安全。别的格子
        重发可能造出第二笔退款 —— 与其在注入点写句注释提醒，不如在这里直接拒掉。
        """
        for trade_no in self.after_fail:
            if trade_no not in self.fail_times:
                raise ValueError(
                    f"after_fail 配了 {trade_no!r} 却没配 fail_times —— "
                    f"没有「失败几次」就无从谈起「之后改判成什么」")
        for trade_no, times in self.fail_times.items():
            if times < 0:
                raise ValueError(
                    f"fail_times[{trade_no!r}]={times}：不许为负。"
                    f"0 表示第一次就落改判后的码（等于没注入失败）")
            if trade_no not in self.script:
                raise ValueError(
                    f"fail_times 配了 {trade_no!r}，但 script 里没有它的码 —— "
                    f"没有注入的码就无所谓「失败 N 次」")
            entry = lookup(self.script[trade_no])
            if not (entry.retriable and entry.outcome == OUTCOME_FAILED):
                raise ValueError(
                    f"{entry.code} 是 retriable={entry.retriable} / "
                    f"outcome={entry.outcome}，不许配 fail_times：在同一个 "
                    f"out_request_no 上改判重发，只在「网关入口拒了、业务确定没执行」"
                    f"那一格才安全（铁律 8）")
            lookup(self.after_fail.get(trade_no, SUCCESS.code))   # 改判的码也得在表里

    # 这里曾有一个不带内存地址的 __repr__，理由是「这个对象会进 invoke_tool 的
    # params_digest」。T76 之后**活对象不再进 params**（两个 ToolPort 只收
    # gateway_name），那个理由随之消失，补丁一并拆掉 —— 留着会让下一个人
    # 以为 digest 仍然依赖 repr，从而不敢动别处。digest 的稳定性现在由
    # 「params 全是标量」这条机制保证，不再靠实现方自觉写 __repr__。

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
            # 参数一致的重发才算一次「尝试」：上面那条参数不一致的路网关当场就拒了，
            # 不该吃掉注入额度。计数必须在这里推进 —— 这一支不经过 `_next_code`。
            existing.attempts += 1
            if self._supersede_due(existing, request.out_trade_no):
                # 注入的失败次数用完了。上一笔落的是「网关在入口就拒了、业务确定
                # 没执行」的码 —— 这个 out_request_no 上没有任何真实退款发生过，
                # 所以这次重发不是「再读一遍旧观察」，而是**同一笔请求这一次被受理**。
                # 仍然不新建第二笔：request_id 不变，改的只是这一笔的下落。
                existing.code = self._next_code(request.out_trade_no,
                                                existing.attempts - 1)
                existing.polls, existing.settled = 0, False
                log.info("幂等键 %s 注入的失败次数已用完，本次改判为 %s",
                         key, existing.code.code)
                return self._accept(existing)
            # 参数一致：原样返回当前观察，不推进状态、不新建。
            return self._observe(existing, advance=False)

        entry = _Entry(request_id=f"gw_{uuid.uuid4().hex[:16]}", request=request,
                       code=self._next_code(request.out_trade_no, 0))
        self._ledger[key] = entry
        self._by_request[entry.request_id] = key
        return self._accept(entry)

    def _next_code(self, trade_no: str, attempt: int) -> GatewayCode:
        """第 ``attempt`` 次（0 基）refund 尝试落哪个码。**纯函数，不记账。**

        计数由调用方给（建账本是第 0 次，第 k 次重发是第 k 次）—— 原先它自己记，
        而幂等重发那条路根本不调它，计数于是漏掉一半，见 ``_Entry.attempts``。

        没配 ``fail_times`` 时恒为 ``script`` 里那个码 —— 既有行为一个字节不变。
        """
        injected = self.script.get(trade_no)
        if injected is None:
            return SUCCESS
        budget = self.fail_times.get(trade_no)
        if budget is None or attempt < budget:
            return lookup(injected)
        return lookup(self.after_fail.get(trade_no, SUCCESS.code))

    def _supersede_due(self, entry: _Entry, trade_no: str) -> bool:
        """这一笔该不该改判。没配注入的网关恒 False，幂等那一段一个字节不变。

        第二条判据让改判**只发生一次**：改判之后 ``entry.code`` 要么是成功码
        （``outcome=success``）、要么是终态失败码（``retriable=False``），两者都
        落不进这一条，于是第三次、第四次重发照常走幂等返回，不会反复翻烧饼。

        第三条是 ``attempts > budget`` 而不是 ``>=``：docstring 承诺的是「前 N 次落
        注入的码，第 N+1 次改判」，而 ``attempts`` 进到这里时已经把**本次**算上了。
        ``fail_times=1`` 于是仍在第 2 次改判，与改动前逐字节一致。
        """
        budget = self.fail_times.get(trade_no)
        if budget is None:
            return False
        if entry.code.outcome != OUTCOME_FAILED or not entry.code.retriable:
            return False
        return entry.attempts > budget

    def _accept(self, entry: _Entry) -> GatewayReceipt:
        """按这一笔当下的码出一份受理回执。**这里绝不返回 settled。**"""
        code, key = entry.code, entry.request.idempotency_key

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
# 人工线下凭证适配器（T117）—— **外部凭证也是外部权威事实，不是绕过**
# ---------------------------------------------------------------------------
#
# 补偿开出的人工工单，人在支付渠道后台把那笔钱线下退掉之后，这个事实必须能回到
# 系统里。从前它回不来：`refund.compensate` 把 `last_observed_state` 记成
# `unobserved`（那是对的，铁律 8 不许它替外部宣布结果），然后链路就断了。
#
# 补法**不是**给补偿流程开一条写 `settled` 的口子 —— 那会让全系统出现第二个写权威
# 终态的地方，`guard.AUTHORITATIVE_WRITER` 那道闸当场变成摆设。补法是把人工凭证
# 建模成它本来的样子：**一份来自外部的回执**。人到支付宝后台看到「已退款」，
# 与 MAOS 自己 query 到 `settled`，是同一类事实的两种取得方式，区别只在取得的
# 渠道是人眼还是 API。于是它照旧从 `gateway.query` 这个口进来，照旧经
# `payment.observe` 落库，四道闸一道不少。
#
# 与 `AlipaySandboxAdapter` 的关系：那个是「同一条 API 换个真后端」，这个是
# 「同一份事实换个取得渠道」。两者都实现 `GatewayPort`，所以上层一个字不用改。

#: 本适配器在回执 `detail` 里打的渠道标记。`payment.observe` 认这个键分流，
#: 审计与验收也按它捞行（`payment_observation.raw_receipt_json` 的 `$.detail.gateway`）。
#:
#: 为什么不新开一列：`payment_observation` 是既有表，同期 T116/T120 也在动退款域，
#: 给共用表加列是跨轨风险；而回执原文本来就整份存在 `raw_receipt_json` 里，
#: 标记放进回执自己的 detail，既不动表结构，也保证「标记与回执同生共死」——
#: 单独一列可以和回执内容对不上，detail 里的不会。
GATEWAY_MANUAL = "manual"

#: 人工凭证的两个码。**刻意不进 `gateway_codes` 的官方码表**：那张表的规矩是
#: 「每条码都带 source，核不到出处的一条都不写」，而这两个码根本不是支付宝发的，
#: 是我们给「人看了一眼外部系统」这件事起的名字。混进官方表里，评委问一句
#: 「这个码哪来的」就答不上来了。所以码值带 `MANUAL.` 前缀、`source` 一句话直说
#: 它不是官方码 —— 一眼看得出它跟 `ACQ.*` 不是一回事。
#:
#: `layer` 仍是 `business`，不另造一个「人工层」：两层是**支付宝 API 的真实结构**，
#: 不是我们的分类槽（`GatewayCode.__post_init__` 校验的就是这一点）。人工核对看到的
#: 「这笔退款入没入账」，正是请求进了业务系统之后的结果，与 `ACQ.*` 描述的是同一层
#: 事实，区别只在取得渠道是人眼还是 API —— 这也正是本适配器全部的立论。
MANUAL_SETTLED = GatewayCode(
    code="MANUAL.SETTLED",
    message="人工线下核对：外部支付渠道显示该笔退款已入账",
    retriable=False,
    outcome=OUTCOME_SUCCESS,
    remedy="以外部支付渠道后台记录为准；MAOS 侧只留这一次观察，不改写外部结果",
    layer=LAYER_BUSINESS,
    source="人工提交的线下凭证摘要（**非**支付宝官方码表；出处是提交人与凭证引用，"
           "逐条记在回执 detail 里）",
)

MANUAL_NOT_SETTLED = GatewayCode(
    code="MANUAL.NOT_SETTLED",
    message="人工线下核对：外部支付渠道显示该笔退款未入账",
    retriable=False,
    outcome=OUTCOME_FAILED,
    remedy="按人工流程重新发起或改单；本回执只记录这一次核对的结果",
    layer=LAYER_BUSINESS,
    source="人工提交的线下凭证摘要（**非**支付宝官方码表；出处是提交人与凭证引用，"
           "逐条记在回执 detail 里）",
)

#: 人工凭证只收**两个**结论。`processing` / `unknown` 一律不收 ——
#: 人是在「已经查完外部系统」之后才提交凭证的，「我还没查出来」不是一份凭证，
#: 是凭证的缺席。口径同 `payment.observe` 那条「还没问出来不是一个可以落库的
#: 结论」与 `compensate.UNOBSERVED`：把「没查出来」收成一条回执，等于替外部
#: 系统下了一个谁都没下过的结论。
_MANUAL_CODES: dict[str, GatewayCode] = {
    OUTCOME_SUCCESS: MANUAL_SETTLED,
    OUTCOME_FAILED: MANUAL_NOT_SETTLED,
}
_MANUAL_STATUS: dict[str, str] = {
    OUTCOME_SUCCESS: STATUS_SETTLED,
    OUTCOME_FAILED: STATUS_FAILED,
}


class ManualReceiptAdapter:
    """把人工提交的线下凭证摘要包装成一份网关回执。签名同 ``GatewayPort``。

    用法（``maos/skills/builtin/refund/compensation_close.py`` 就这么用）::

        adapter = ManualReceiptAdapter()
        adapter.submit(request_id=rid, outcome=OUTCOME_SUCCESS,
                       evidence_ref="alipay-console-20260910-0031",
                       summary="支付宝商家后台 7 月 5 日退款流水已入账",
                       submitted_by="@payops:maos.local")
        register_gateway("manual", adapter)      # 之后 payment.observe 照常 query

    ``refund()`` **恒抛**，这是本类最要紧的一条。走到人工凭证这一步，说明退款
    要么已经由人在渠道后台做完了、要么由人判定做不了 —— 本适配器只负责把那个
    结果收下来。留一个能发起退款的方法在这里，迟早有人拿它去补一笔真钱，
    而它绕过的正是幂等键、轮询与错误码那一整套（口径同 ``AlipaySandboxAdapter``：
    没接通的动作显式抛，不静默返回假数据）。
    """

    def __init__(self) -> None:
        #: request_id -> 已提交的凭证回执。一笔请求只留**最后一次**提交的凭证：
        #: 人改口了以最新一次为准，历史留在 `payment_observation` 里（那张表按
        #: `observed_at` 逐次追加），不在本适配器的内存里做第二份账。
        self._receipts: dict[str, GatewayReceipt] = {}
        #: request_id -> 被 query 过几次。落进回执的 `poll_count`，语义与 MockGateway
        #: 一致：**问了几次**。人工这条路上恒为 1（观察一次就是终态），
        #: 但仍然如实计数，不写死 —— 写死的数字骗不了人，只会让审计少一条真信息。
        self._polls: dict[str, int] = {}

    def __repr__(self) -> str:
        return f"ManualReceiptAdapter(submitted={sorted(self._receipts)})"

    # ------------------------------------------------------------------
    def submit(self, *, request_id: str, outcome: str = OUTCOME_SUCCESS,
               evidence_ref: str, summary: str, submitted_by: str,
               submitted_at: str = "", idempotency_key: str = "") -> GatewayReceipt:
        """收下一份线下凭证摘要，返回它包装成的回执。

        四个必填项缺一不可，缺了就抛：**没有提交人、没有凭证引用的「凭证」不是
        凭证**，是一句没有出处的断言 —— 而这条链路买的就是出处。
        """
        rid = str(request_id or "").strip()
        if not rid:
            raise ValueError("人工凭证必须指名它说的是哪一笔 refund_request（request_id）")
        code = _MANUAL_CODES.get(str(outcome))
        if code is None:
            raise ValueError(
                f"人工凭证的结论只能是 {sorted(_MANUAL_CODES)} 之一，实际 {outcome!r}；"
                "「还没查出来」不是一份凭证，不要收成一条回执")
        ref = str(evidence_ref or "").strip()
        who = str(submitted_by or "").strip()
        if not ref or not who:
            raise ValueError(
                "人工凭证必须带 evidence_ref（外部凭证引用）与 submitted_by（提交人）："
                "这两项是它作为**外部事实**的全部出处，缺了就只是一句断言")

        receipt = _receipt(
            rid, str(idempotency_key or ""), _MANUAL_STATUS[str(outcome)], code,
            poll_count=0,
            detail={
                "gateway": GATEWAY_MANUAL,
                "evidence_ref": ref,
                "summary": str(summary or ""),
                "submitted_by": who,
                "submitted_at": str(submitted_at or ""),
            },
        )
        self._receipts[rid] = receipt
        self._polls[rid] = 0
        log.info("收到人工线下凭证 request=%s outcome=%s 提交人=%s", rid, outcome, who)
        return receipt

    # ------------------------------------------------------------------
    def refund(self, request: RefundRequest) -> GatewayReceipt:
        raise NotImplementedError(
            "ManualReceiptAdapter 不发起退款：走到人工凭证这一步，那笔钱已经由人在"
            "支付渠道后台处理过了，本适配器只负责把结果收回系统。"
            "要真发起退款请用 MockGateway / AlipaySandboxAdapter，那条路上有幂等键、"
            "轮询与官方错误码"
        )

    def query(self, request_id: str) -> GatewayReceipt:
        """取这一笔已提交的人工凭证。没提交过就抛 —— **不返回一个空回执**。

        兜底成「还没到终态」会让 `payment.observe` 白轮询几圈然后如实报「问不出来」，
        表面上毫无异常，实际上是「凭证根本没提交」被伪装成了「外部还没结果」。
        """
        rid = str(request_id or "").strip()
        receipt = self._receipts.get(rid)
        if receipt is None:
            raise KeyError(
                f"没有针对 request_id={rid!r} 的人工凭证（已提交：{sorted(self._receipts)}）；"
                "先 submit() 再 query()")
        self._polls[rid] = self._polls.get(rid, 0) + 1
        return replace(receipt, poll_count=self._polls[rid])


# ---------------------------------------------------------------------------
# 工具侧网关注册表 —— params 只带名字，实例由**工具这一侧**持有
# ---------------------------------------------------------------------------
#
# 为什么要有这张表（`docs/BACKLOG.md:1640`）：两个 ToolPort 原先直接收
# ``GatewayPort`` 活对象，而 ``invoke_tool`` 会对 params 算 sha256 落审计行 ——
# 对象进 digest，稳定性就只能靠每个实现自己写一个不带内存地址的 ``__repr__``
# 来维持，**那是打补丁不是机制**。第三个实现只要忘了写，同样的参数每次算出不同
# 的 digest，审计对不上账，而且不报错、无症状。
#
# 改成按名取实例之后，params 全是标量，digest 天然可复现；同时这也是迁 MCP 的
# 前置条件 —— 活对象跨不了进程，而名字可以。届时 **本文件整个搬到 server 侧**，
# 客户端只发 ``gateway_name``。
#
# 这张表**刻意不复用** ``skills/builtin/refund/_common.py`` 里那张同名表：
# tools 层 import skills 层是层间倒挂，迁 MCP 时会把整个 skill 包拖到 server 侧去。
# 两张表各自存在，由装配方各注册一次。

_GATEWAYS: dict[str, Any] = {}


def register_gateway(name: str, gateway: Any) -> Any:
    """把一个网关实现登记成一个名字，供两个 ToolPort 按名取用。"""
    _GATEWAYS[str(name)] = gateway
    return gateway


def get_gateway(name: str) -> Any:
    """按名取网关。**取不到就抛，不兜底成默认网关。**

    口径与 `_common.get_gateway` 一致：自动兜底会把「忘了注册网关」变成
    「悄悄用了一个空账本的 mock」—— 幂等、轮询次数、错误注入全部失真，
    而表面上一路绿灯，只会在演示现场暴露。
    """
    key = str(name or "")
    gateway = _GATEWAYS.get(key)
    if gateway is None:
        raise LookupError(
            f"工具侧没有登记名为 {key!r} 的支付网关（已登记：{sorted(_GATEWAYS)}）；"
            "请在装配处调用 maos.tools.gateway.register_gateway(name, MockGateway(...))"
        )
    return gateway


def reset_gateways() -> None:
    """清空登记表 —— 只给测试用，保证用例之间不互相串账本。"""
    _GATEWAYS.clear()


# ---------------------------------------------------------------------------
# 两个 ToolPort 声明（A-6 九要素）—— 调用一律走 invoke_tool()，直接调没有审计行
# ---------------------------------------------------------------------------

def gateway_refund(*, gateway_name: str, out_trade_no: str, refund_amount: str,
                   idempotency_key: str, reason: str = "") -> dict:
    """ToolPort 入口：发起退款，返回回执 dict。

    入参**全是标量**（网关只给名字，不给实例）：``invoke_tool`` 会把 params
    做 sha256 进审计行，标量化之后 digest 才对得上「同样的参数」这个直觉，
    且跨进程传得过去（迁 MCP 的前置条件）。
    """
    gateway = get_gateway(gateway_name)
    req = RefundRequest(out_trade_no=out_trade_no, refund_amount=refund_amount,
                        idempotency_key=idempotency_key, reason=reason)
    return gateway.refund(req).to_dict()


def gateway_query(*, gateway_name: str, request_id: str) -> dict:
    """ToolPort 入口：查一笔退款的当前状态。终态只能从这里来。"""
    return get_gateway(gateway_name).query(request_id).to_dict()


GATEWAY_REFUND_PORT = ToolPort(
    name="gateway.refund",
    purpose="向支付网关发起退款；返回受理回执，**不返回终态**（终态须经 gateway.query 观察）",
    entry=gateway_refund,
    params_schema={"gateway_name": "str（已 register_gateway 的名字；实例由工具侧持有）",
                   "out_trade_no": "str",
                   "refund_amount": "str（金额不进浮点）", "idempotency_key": "str",
                   "reason": "str（可选）"},
    returns_schema={"request_id": "str", "status": "processing|unknown（非终态）",
                    "code": "str", "retriable": "bool", "outcome": "success|failed|unknown",
                    "remedy": "str", "source": "str（错误码出处）", "is_terminal": "bool"},
    failure_modes=[
        "LookupError: gateway_name 没有登记过 —— **不兜底成默认网关**",
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
    params_schema={"gateway_name": "str（已 register_gateway 的名字；实例由工具侧持有）",
                   "request_id": "str"},
    returns_schema={"status": "processing|unknown|settled|failed",
                    "poll_count": "int（问过几次，证明终态是问出来的）",
                    "outcome": "success|failed|unknown", "is_terminal": "bool"},
    failure_modes=[
        "LookupError: gateway_name 没有登记过 —— **不兜底成默认网关**",
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
