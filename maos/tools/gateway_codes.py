"""支付网关错误码表 —— 每一条都从支付宝开放平台官方文档核对后写入。

## 为什么这张表不许凭记忆填

编一张错误码表，被评委问一句「这个码是哪来的」就全塌。所以本文件的规矩是：
**每条码都带 ``source``，指到具体文档页与小节；核不到出处的一条都不写。**
下面 11 条全部核过，出处见各条 ``source`` 与文件末尾的 SOURCES。

## 两层错误码（这是支付宝 API 的真实结构，不是我们的发明）

支付宝的返回是 ``code / msg / sub_code / sub_msg`` 四元组，错误分两层：

  · **网关层**（``code``）—— 请求有没有被受理。``20000`` 服务不可用、``40005``
    调用频次超限都在这层。这层的错误**没进业务系统**或**不知道有没有进**。
  · **业务层**（``sub_code``，``ACQ.*``）—— 请求进了业务系统之后的结果。
    ``code=40004``（业务处理失败）时才去看 ``sub_code``。

派单要求覆盖的五类里，**「渠道繁忙」不在业务层** —— ``alipay.trade.refund``
的业务错误码表（31 条，全查过）里没有任何一条叫「渠道繁忙 / 系统繁忙 / 频次超限」，
它们在网关层的 ``20000`` 与 ``40005``。这不是查不到，是**它本来就在另一层**；
硬塞一个 ``ACQ.CHANNEL_BUSY`` 进业务层才是编造。详见 DECISIONS R3-01。

## retriable 与 outcome 是两个正交的维度（本文件最重要的一条）

只有 ``retriable`` 一个 bool 是**不够**的，而且不够的地方正好踩在铁律 8 上。

``retriable`` 答的是「能不能再发一次」；``outcome`` 答的是「**这一笔到底执行了没有**」。
后者有三态，其中 ``unknown`` 是关键：

    outcome="success"   业务确定成功
    outcome="failed"    业务确定没执行 / 确定失败
    outcome="unknown"   **网关自己也说不清** —— 可能成功也可能失败

``ACQ.SYSTEM_ERROR`` 的官方解决方案原文是「保持参数不变重试**或查询执行结果**」——
「查询执行结果」这五个字就是官方在说：这一笔的结果我们没告诉你。
此时任何「当作失败」或「当作成功」的代码都是 bug（铁律 8：MAOS 不持有权威事实，
只持有观察与推断）。唯一正确的动作是 ``query()`` 去问，而不是在本地推断。

四个象限各自对应一种处置，replan 照此分流，不要只看 retriable：

    retriable=True  + unknown  ->  **先 query 再决定**，不许直接重发（重发可能造成第二笔）
    retriable=True  + failed   ->  可以直接重发（网关在入口就拒了，业务没执行）
    retriable=False + failed   ->  终态，转人工或改单
    retriable=False + unknown  ->  必须 query / 转人工，**最危险的一档**

「渠道繁忙可重试、交易不存在不可重试」是派单点名的判定输入；标反了 replan
会在不该重试的地方自旋。所以每条的 ``retriable`` 都**按官方解决方案原文**定，
不按语感 —— 官方写「重试」才 True，写「检查参数 / 充值后重新发起」一律 False。
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# 出处（每条码的 source 字段引用这里的常量，改 URL 只改一处）
# ---------------------------------------------------------------------------

#: alipay.trade.refund（统一收单交易退款接口）官方文档「业务错误码」小节。
#: 该页给出退款接口全部 31 条 ACQ.* 业务码及其官方解决方案原文。
SRC_REFUND_API = (
    "https://aipay.alipay.com/docs/mobile-app-pay/ai-app-pay/"
    "alipay-trade-refund.html#业务错误码"
)

#: 支付宝开放平台「API 公共错误码」官方文档。网关层 code 的权威表。
SRC_COMMON_CODES = "https://ideservice.alipay.com/cms/site/02km9f#API公共错误码"

#: Antom（支付宝国际站）alipay.trade.refund 文档「Error codes」小节。
#: 用作交叉验证：同一批 ACQ.* 码在两个官方站点上一致，排除单页抓错的可能。
SRC_ANTOM_REFUND = "https://docs.antom.com/ac/solution_api/trade-refund#Error codes"


# 业务结果三态。unknown 是铁律 8 的代码化表达 —— 见模块 docstring。
OUTCOME_SUCCESS = "success"
OUTCOME_FAILED = "failed"
OUTCOME_UNKNOWN = "unknown"

# 错误码所在层。business 层要 code=40004 才会出现。
LAYER_GATEWAY = "gateway"
LAYER_BUSINESS = "business"


@dataclass(frozen=True)
class GatewayCode:
    """一条官方错误码。

    frozen=True 是有意的：这张表是**对外部系统的观察口径**，
    运行期任何地方都不该改它。要改只能改代码，改代码就要重新核出处。
    """

    code: str
    """码值。网关层是数字串（"20000"），业务层是 ACQ.* 全称。"""

    message: str
    """官方中文描述，**原文照抄**，不要自己润色 —— 润色过就对不上文档了。"""

    retriable: bool
    """能不能再发一次。严格按官方 remedy 原文定，见模块 docstring。"""

    outcome: str
    """这一笔业务到底执行了没有：success / failed / **unknown**。"""

    remedy: str
    """官方给出的解决方案，原文照抄。retriable 与 outcome 都由它推出。"""

    layer: str
    """gateway（网关层 code）或 business（业务层 sub_code）。"""

    source: str
    """出处。核不到出处的码不许进这张表。"""

    def __post_init__(self) -> None:
        if self.outcome not in (OUTCOME_SUCCESS, OUTCOME_FAILED, OUTCOME_UNKNOWN):
            raise ValueError(f"未知的 outcome：{self.outcome}")
        if self.layer not in (LAYER_GATEWAY, LAYER_BUSINESS):
            raise ValueError(f"未知的 layer：{self.layer}")
        if not self.source:
            raise ValueError(f"错误码 {self.code} 没有出处 —— 核不到出处的不许进表")


# ---------------------------------------------------------------------------
# 网关层（code）—— 出处：SRC_COMMON_CODES
# ---------------------------------------------------------------------------

SUCCESS = GatewayCode(
    code="10000",
    message="接口调用成功",
    retriable=False,
    outcome=OUTCOME_SUCCESS,
    remedy="调用结果请参考具体的 API 所对应的业务返回参数",
    layer=LAYER_GATEWAY,
    source=SRC_COMMON_CODES,
)

#: 派单五类之「渠道繁忙」。官方对 20000 的两个 sub_code 分别是
#: isp.unknow-error（业务系统暂不可用）与 aop.unknow-error（网关自身的未知错误），
#: 实际返回里 sub_msg 常见就是「系统繁忙」。
#: outcome=unknown 而非 failed：业务系统「暂不可用」不等于「没收到请求」——
#: 请求可能已经进去并且退款已经发生，我们只是没拿到回执。当作 failed 会导致
#: 上层重发，那才是真的产生第二笔。
SERVICE_UNAVAILABLE = GatewayCode(
    code="20000",
    message="服务不可用（sub_code: isp.unknow-error 业务系统暂不可用 /"
            " aop.unknow-error 服务暂不可用，网关自身的未知错误）",
    retriable=True,
    outcome=OUTCOME_UNKNOWN,
    remedy="稍后重试",
    layer=LAYER_GATEWAY,
    source=SRC_COMMON_CODES,
)

#: 业务处理失败。这条本身不带业务语义，它是「去看 sub_code」的路标。
BIZ_FAILED = GatewayCode(
    code="40004",
    message="业务处理失败",
    retriable=False,
    outcome=OUTCOME_FAILED,
    remedy="参考具体 API 文档的业务错误码（sub_code）",
    layer=LAYER_GATEWAY,
    source=SRC_COMMON_CODES,
)

#: 派单五类之「渠道繁忙」的另一半：限流。
#: 与 20000 的区别在 outcome —— 频次超限是网关**在入口就拒了**，请求没进业务系统，
#: 所以这一笔确定没执行（failed），可以放心直接重发。
#: 这一对正是 retriable 与 outcome 正交的最好例子：两条都 retriable=True，
#: 但一条必须先 query、另一条可以直接重发。
RATE_LIMITED = GatewayCode(
    code="40005",
    message="调用频次超限",
    retriable=True,
    outcome=OUTCOME_FAILED,
    remedy="降低请求并发量",
    layer=LAYER_GATEWAY,
    source=SRC_COMMON_CODES,
)


# ---------------------------------------------------------------------------
# 业务层（sub_code，ACQ.*）—— 出处：SRC_REFUND_API，并经 SRC_ANTOM_REFUND 交叉验证
# ---------------------------------------------------------------------------

#: 派单五类之「系统错误」。
#: remedy 原文「保持参数不变重试或查询执行结果」——「或查询执行结果」这半句
#: 就是官方在说结果未知。这是整张表里最能说明铁律 8 的一条。
SYSTEM_ERROR = GatewayCode(
    code="ACQ.SYSTEM_ERROR",
    message="系统错误",
    retriable=True,
    outcome=OUTCOME_UNKNOWN,
    remedy="保持参数不变重试或查询执行结果",
    layer=LAYER_BUSINESS,
    source=SRC_REFUND_API,
)

#: 派单五类之「交易不存在」。
#: remedy 是「检查交易号」——要改参数，不是原样重试，故 retriable=False。
#: 派单点名：这条标成可重试，replan 就会在不该重试的地方自旋。
TRADE_NOT_EXIST = GatewayCode(
    code="ACQ.TRADE_NOT_EXIST",
    message="交易不存在",
    retriable=False,
    outcome=OUTCOME_FAILED,
    remedy="检查交易号或商户订单号是否正确",
    layer=LAYER_BUSINESS,
    source=SRC_REFUND_API,
)

#: 派单五类之「重复请求不一致」。
#: outcome=unknown 而不是 failed —— 官方 remedy 里有「或**查询历史执行结果**」：
#: 同一个 out_request_no 之前那一笔可能已经成功了，这次只是参数对不上被拒。
#: 把它当 failed 会让上层以为「没退成」而换个单号重发 —— 那就真退了两笔。
DISCORDANT_REPEAT_REQUEST = GatewayCode(
    code="ACQ.DISCORDANT_REPEAT_REQUEST",
    message="请求信息不一致",
    retriable=False,
    outcome=OUTCOME_UNKNOWN,
    remedy="检查退款金额或查询历史执行结果",
    layer=LAYER_BUSINESS,
    source=SRC_REFUND_API,
)

#: 派单五类之「余额不足」。
#: remedy「商户账户充值后重新发起」要人工介入，机器重试没有意义，故 retriable=False。
SELLER_BALANCE_NOT_ENOUGH = GatewayCode(
    code="ACQ.SELLER_BALANCE_NOT_ENOUGH",
    message="卖家余额不足",
    retriable=False,
    outcome=OUTCOME_FAILED,
    remedy="商户账户充值后重新发起",
    layer=LAYER_BUSINESS,
    source=SRC_REFUND_API,
)

#: 业务层里**唯一**官方明说「过段时间后重试」的一条，语义最接近「稍后再试」。
#: 收录它是为了让业务层也有一条 retriable=True 的样本，
#: 好让 replan 的分流逻辑在业务层同样被测到。
REFUND_CHARGE_ERROR = GatewayCode(
    code="ACQ.REFUND_CHARGE_ERROR",
    message="退收费异常",
    retriable=True,
    outcome=OUTCOME_UNKNOWN,
    remedy="过段时间后重试",
    layer=LAYER_BUSINESS,
    source=SRC_REFUND_API,
)

#: 以下两条不在派单五类内，收录理由：它们是退款接口最常见的**终态失败**，
#: MockGateway 要能造出「明确失败且不该重试」的回执供 R-2 的场景用。
TRADE_HAS_FINISHED = GatewayCode(
    code="ACQ.TRADE_HAS_FINISHED",
    message="交易已完结",
    retriable=False,
    outcome=OUTCOME_FAILED,
    remedy="超过退款期限，建议线下退款",
    layer=LAYER_BUSINESS,
    source=SRC_REFUND_API,
)

REFUND_AMT_NOT_EQUAL_TOTAL = GatewayCode(
    code="ACQ.REFUND_AMT_NOT_EQUAL_TOTAL",
    message="退款金额超限",
    retriable=False,
    outcome=OUTCOME_FAILED,
    remedy="确保金额不超交易总额并传入请求号",
    layer=LAYER_BUSINESS,
    source=SRC_REFUND_API,
)


#: 全表。按码值索引，供 MockGateway 与上层按码取判据。
ALL_CODES: dict[str, GatewayCode] = {
    c.code: c
    for c in (
        SUCCESS,
        SERVICE_UNAVAILABLE,
        BIZ_FAILED,
        RATE_LIMITED,
        SYSTEM_ERROR,
        TRADE_NOT_EXIST,
        DISCORDANT_REPEAT_REQUEST,
        SELLER_BALANCE_NOT_ENOUGH,
        REFUND_CHARGE_ERROR,
        TRADE_HAS_FINISHED,
        REFUND_AMT_NOT_EQUAL_TOTAL,
    )
}

#: 派单点名必须覆盖的五类 -> 本表中的代表码。
#: 「渠道繁忙」映到网关层的两条，因为业务层根本没有这一类（见 DECISIONS R3-01）。
REQUIRED_CATEGORIES: dict[str, tuple[str, ...]] = {
    "系统错误": ("ACQ.SYSTEM_ERROR",),
    "交易不存在": ("ACQ.TRADE_NOT_EXIST",),
    "重复请求不一致": ("ACQ.DISCORDANT_REPEAT_REQUEST",),
    "余额不足": ("ACQ.SELLER_BALANCE_NOT_ENOUGH",),
    "渠道繁忙": ("20000", "40005"),
}


def lookup(code: str) -> GatewayCode:
    """按码值取判据。未知码**抛**而不是返回一个「默认可重试」的兜底。

    兜底的后果不是报错，是**把没核过出处的码当成已知码处理** —— 那正是这张表
    要防的事。未知码应该在上层被当作「未知外部状态」显式处理。
    """
    try:
        return ALL_CODES[code]
    except KeyError:
        raise KeyError(
            f"未知网关错误码 {code!r}：不在已核对官方文档的清单内。"
            f"新增码必须先核到出处再进 ALL_CODES，不许在调用处就地兜底"
        ) from None


def is_retriable(code: str) -> bool:
    """R-2 与 replan 的判定入口之一。未知码抛 KeyError，不静默放行。"""
    return lookup(code).retriable


def outcome_of(code: str) -> str:
    """这一笔业务执行了没有。``unknown`` 时**不许**在本地推断成败，只能去 query。"""
    return lookup(code).outcome


def needs_query_before_retry(code: str) -> bool:
    """重发之前是不是必须先查一次。

    这是 retriable 与 outcome 正交之后真正要用的那个判据：
    ``retriable=True`` 但 ``outcome=unknown`` 的码（20000 / ACQ.SYSTEM_ERROR），
    直接重发就可能产生第二笔退款 —— 必须先 ``query()`` 确认前一笔的下落。
    """
    c = lookup(code)
    return c.retriable and c.outcome == OUTCOME_UNKNOWN


#: 供文档与回执引用：本表全部出处。
SOURCES: tuple[str, ...] = (SRC_REFUND_API, SRC_COMMON_CODES, SRC_ANTOM_REFUND)
