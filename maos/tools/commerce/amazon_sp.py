"""Amazon Selling Partner API（SP-API）订单适配器 —— Orders API v0 的 `getOrder`。

## 与别的平台不一样的两处

### 一、查单前要先换令牌（LWA）

SP-API 不收长期 token。每次查单要带 `x-amz-access-token`，而那个 access token
只能拿 refresh token 去 Login with Amazon（LWA）换，有效期由响应的 `expires_in`
给（文档样例是 3600 秒）。所以本适配器多了一层别轨没有的东西：

    POST https://api.amazon.com/auth/o2/token
         grant_type=refresh_token & refresh_token & client_id & client_secret
    → {"access_token": ..., "token_type": "bearer", "expires_in": 3600, ...}

- 令牌**缓存在适配器实例上**，带过期时刻；提前 60 秒换新（`_EARLY_REFRESH`），
  免得一个还剩 2 秒的令牌在路上过期、平台回 403、人去查「凭据是不是错了」。
- 换令牌也走 `self.transport`，于是测试里它同样是 `FakeTransport` 的一条预置。
- **令牌与密钥同级**：不进 repr、不进日志、不进异常。换令牌请求的 body 里有
  refresh token 与 client secret，body 同样不进异常；LWA 回的 `error_description`
  进异常前也先抹掉三样凭据的原值（防它原样回显）。
- **换令牌失败单独报，不吞成「查单失败」**：抛 `CommerceError`，`code` 是 LWA 的
  `error`（`invalid_grant` / `invalid_client` …），文字写明是 LWA 那一步坏了、
  往哪查；此时**不发**查单的 GET。两件事的排查方向完全相反 —— 前者去 Seller
  Central 重新授权或核 client 配置，后者才去看订单号。

### 二、429 是限流窗口，不是错误

`getOrder` 的 usage plan 是 0.5 req/s、burst 30（Orders API v0 模型 ordersV0.json
里 getOrder 的 Usage Plan 表；`getOrders` 列表接口才是 0.0167 req/s、burst 20）。
令牌桶空了平台回 429，响应头 `x-amzn-RateLimit-Limit` 给出本账号本应用的实际速率。

- **不许为了跑得快而调高 `max_retries`** —— 桶是按时间回填的，撞得越勤桶越空，
  只会把限流窗口撞得更死。
- 退避在 transport 层（`RETRIABLE_STATUS` 含 429，按 `Retry-After` 与指数退避取大者，
  封顶 30 秒）。本模块**不构造 transport**、不写自己的重试或 sleep 循环：
  transport 由装配处注入，缺省由基类给。
- `read_error` 在 429 时把上面这段话（带速率与响应头原值）写进异常，人一看就知道
  该等，而不是该修。

## 不做的事

- 不接 webhook：SP-API 的订单通知走 Notifications API（投 SQS / EventBridge），
  不在本批。所以本模块不注册 verifier。
- 不做 AWS Signature Version 4：SP-API 自 2023-10-02 起不再要求 IAM 与 SigV4
  （changelog「SP-API no longer requires AWS IAM or AWS Signature Version 4」：
  带了 SigV4 签名的请求平台也会忽略签名、只认 LWA）。
- 不接退款 / 出款 API（本批只读订单）。

## 订单不存在 → 404（不改 `not_found_status`）

ordersV0.json 里 getOrder 的响应表：`404` 是「The resource specified does not exist.」，
`400` 是「Request has missing or invalid parameters and cannot be parsed.」。
基类注释里「Amazon 用 400 + InvalidInput 表达找不到单」与模型不符 —— 400 +
`InvalidInput` 是参数错（沙箱的 TEST_CASE_400 用例就是它），不是查无此单。
所以沿用基类缺省 `{404}`：把 400 也算成「单不存在」会让参数错被当成「订单不见了」。

## Orders API v0 已宣布弃用

SP-API Deprecations Schedule：`getOrder` 等六个 v0 操作 2026-01-28 宣布弃用，
**2027-03-27 起调用失败**，接替者是 Orders API v2026-01-01。本适配器按 v0 写
（字段名 `AmazonOrderId` / `OrderStatus` / `OrderTotal` / `LastUpdateDate`），
迁移记在 docs/BACKLOG.md 的 task-t164 小节。

## 凭据

四个环境变量，全部经 `read_credential()` 在**用到的那一刻**读 —— 导入期、构造期
一个都不读（`scripts/gen_docs.py` 与运行时装配都会逐个导入 `maos.tools` 下的模块，
那时环境里没有 `AMAZON_SP_*`）：

    AMAZON_SP_CLIENT_ID      LWA 应用的 client id
    AMAZON_SP_CLIENT_SECRET  LWA 应用的 client secret
    AMAZON_SP_REFRESH_TOKEN  卖家授权后拿到的 refresh token
    AMAZON_SP_REGION         na / eu / fe（决定 sellingpartnerapi-<region>.amazon.com）
"""

from __future__ import annotations

import json
import logging
import sys
import time
import urllib.parse

from maos.tools.commerce.base import (
    CommerceAdapter, CommerceError, QueryRequest, RawOrder, read_credential,
)
from maos.tools.commerce.mapping import StatusRule, dig
from maos.tools.commerce.transport import HttpResponse, HttpTransport

log = logging.getLogger("maos.tools.commerce.amazon_sp")

#: LWA 令牌端点（「Connect to the SP-API」第 1 步：Request a Login with Amazon access token）。
LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"

#: 令牌过期前提前多少秒换新。`now() >= 取得时刻 + expires_in - 60` 即换。
_EARLY_REFRESH = 60.0

#: 区域 → SP-API 生产端点（「SP-API Endpoints」表）。不在表里的区域直接抛，不缺省成 na。
_REGION_HOSTS = {
    "na": "sellingpartnerapi-na.amazon.com",
    "eu": "sellingpartnerapi-eu.amazon.com",
    "fe": "sellingpartnerapi-fe.amazon.com",
}

#: getOrder 的 usage plan（ordersV0.json / getOrder / Usage Plan）。只用于 429 的说明文字，
#: 不参与任何节流计算 —— 节流是平台的事，退避是 transport 的事。
_GET_ORDER_RATE = "0.5 req/s、burst 30"

#: 「Connect to the SP-API」要求的 user-agent：应用名、版本、语言。
_USER_AGENT = "MAOS-order-query/1.0 (Language=Python/%d.%d)" % sys.version_info[:2]


# 顺序即优先级：Canceled 排第一（契约 §C 第 3 条）。OrderStatus 是一维枚举，同一取值
# 只会命中一条，所以「取消优先」在这里是结构性的声明：规则表第一条就是取消。
# values 一律小写 —— map_status() 先 .lower() 再比。注意拼写：Amazon 的枚举是
# Canceled（一个 l），目标态是 cancelled（两个 l）。
#
# 没进表的三个值是有意的，让它们抛 UnmappedOrderStatus（契约 §C.1）：
#   Pending             下单了、付款未授权 —— 映射成 paid 正是被禁止的那种兜底
#   PendingAvailability 预售、付款未授权、发售日在未来 —— 同上
#   Unfulfillable       多渠道配送（MCF）专用的「无法履约」—— 四态里没有对应语义
_RULES = (
    StatusRule(
        "OrderStatus", ("canceled",), "cancelled",
        source="Amazon SP-API Orders API v0 model (ordersV0.json) / Order object / "
               "OrderStatus: Canceled — The order has been canceled."),
    StatusRule(
        "OrderStatus", ("shipped",), "shipped",
        source="Amazon SP-API Orders API v0 model (ordersV0.json) / Order object / "
               "OrderStatus: Shipped — All items in the order have been shipped."),
    StatusRule(
        "OrderStatus", ("invoiceunconfirmed",), "shipped",
        source="Amazon SP-API Orders API v0 model (ordersV0.json) / Order object / "
               "OrderStatus: InvoiceUnconfirmed — All items in the order have been shipped. "
               "The seller has not yet given confirmation to Amazon that the invoice "
               "has been shipped to the buyer."),
    # 部分发货归 shipped：货已经有一部分离仓，按「未发货、可直接退款」的 paid 处理
    # 会少算一段逆向物流；归 shipped 是偏保守的一侧。损失的是「只发了一部分」
    # 这层信息，取舍记在 docs/DECISIONS.md 的 task-t164 小节。
    StatusRule(
        "OrderStatus", ("partiallyshipped",), "shipped",
        source="Amazon SP-API Orders API v0 model (ordersV0.json) / Order object / "
               "OrderStatus: PartiallyShipped — One or more, but not all, items in the "
               "order have been shipped."),
    StatusRule(
        "OrderStatus", ("unshipped",), "paid",
        source="Amazon SP-API Orders API v0 model (ordersV0.json) / Order object / "
               "OrderStatus: Unshipped — Payment has been authorized and the order is ready "
               "for shipment, but no items in the order have been shipped."),
)


class AmazonSpAdapter(CommerceAdapter):
    """Amazon SP-API 的 `OrderSystemPort` 实现。只填三个钩子，外加一层 LWA 令牌缓存。"""

    platform = "amazon_sp"
    status_rules = _RULES

    def __init__(self, *, transport: HttpTransport | None = None, account: str = "",
                 now: object = None) -> None:
        super().__init__(transport=transport, account=account)
        # 时钟可注入，口径同 transport 的 sleep 注入：令牌什么时候换新要在测试里
        # 确定性地验，而真等 3540 秒不现实。只用于令牌过期判断，所以用单调时钟。
        self._now = now if callable(now) else time.monotonic
        self._access_token: str | None = None
        self._token_refresh_at = 0.0

    # ---- 钩子一：拼查单请求 ---------------------------------------------------

    def build_query_request(self, order_id: str) -> QueryRequest:
        host = self._host()
        token = self._token()
        path = "/orders/v0/orders/" + urllib.parse.quote(str(order_id), safe="")
        return QueryRequest(
            method="GET",
            url=f"https://{host}{path}",
            headers={
                "host": host,
                "x-amz-access-token": token,
                "x-amz-date": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
                "user-agent": _USER_AGENT,
            },
        )

    # ---- 钩子二：取字段 -------------------------------------------------------

    def parse_order(self, payload: dict) -> RawOrder:
        # getOrder 的响应包了一层：{"payload": {...订单...}}。规则表的 field 相对订单对象写。
        order = payload.get("payload")
        if not isinstance(order, dict):
            raise CommerceError(
                self.platform, 200, "BAD_SHAPE",
                "getOrder 响应里没有 payload 对象（GetOrderResponse 应为 "
                f'{{"payload": {{...}}}}），实际顶层键：{sorted(payload)}')
        order_id = order.get("AmazonOrderId")
        if not order_id:
            raise CommerceError(self.platform, 200, "BAD_SHAPE",
                                "getOrder 的 payload 里没有 AmazonOrderId")
        return RawOrder(
            order_id=order_id,
            status_payload=order,
            # OrderTotal 是嵌套的 {"CurrencyCode", "Amount"}，取出标量再交。
            # 它在模型里不是 required：缺了 dig 返回 None，normalize_amount 会抛，
            # **不兜底成 "0"** —— 零元订单与「读不到金额」是两回事。
            amount=dig(order, "OrderTotal.Amount"),
            # ISO8601 带 Z，原样交出去：它同时是 version 的来源。
            updated_at=order.get("LastUpdateDate"),
        )

    # ---- 钩子三：读错误（不许抛） ---------------------------------------------

    def read_error(self, response: HttpResponse) -> tuple[str, str]:
        try:
            code, message = _first_error(response)
            if response.status == 429:
                limit = response.header("x-amzn-RateLimit-Limit")
                message = (
                    f"{message}（限流：这不是错误，是 getOrder 的限流窗口 —— usage plan "
                    f"{_GET_ORDER_RATE}"
                    + (f"，本次响应 x-amzn-RateLimit-Limit={limit}" if limit else "")
                    + "。等令牌桶回填后再查；退避由 transport 层负责，"
                    "不要为了跑得快调高 max_retries，那只会把窗口撞得更死）"
                )
            elif response.status in (401, 403):
                # 令牌可能已被平台作废（卖家撤销授权等）。丢掉缓存，下一次查单重新换，
                # 免得一个死令牌在缓存里再挂满剩下的有效期。
                self._access_token = None
                message = (f"{message}（access token 已丢弃，下次查单会重新向 LWA 换；"
                           "反复 403 请核对应用的角色授权与卖家授权是否仍有效）")
            return code, message
        except Exception:                                   # noqa: BLE001
            # 这个钩子在处理一个已经出错的响应，它再抛会盖掉平台原话（base.read_error 的约定）。
            return "", response.text()[:500]

    # ---- LWA 令牌 -------------------------------------------------------------

    def _host(self) -> str:
        region = read_credential(
            "AMAZON_SP_REGION", platform=self.platform,
            purpose="SP-API 区域：na / eu / fe，决定 sellingpartnerapi-<region>.amazon.com",
        ).lower()
        if region not in _REGION_HOSTS:
            raise ValueError(
                f"{self.platform} 的 AMAZON_SP_REGION={region!r} 不认识：只接受 "
                f"{sorted(_REGION_HOSTS)}（SP-API Endpoints 表）。不缺省成 na —— "
                "区域错了，查到的是另一个站点的账本")
        return _REGION_HOSTS[region]

    def _token(self) -> str:
        """取一个有效的 access token：缓存里的还没到换新时刻就复用，否则向 LWA 换。"""
        now = self._now()
        if self._access_token is not None and now < self._token_refresh_at:
            return self._access_token

        client_id = read_credential(
            "AMAZON_SP_CLIENT_ID", platform=self.platform,
            purpose="LWA 应用的 client id（Developer Central 的 LWA credentials）")
        client_secret = read_credential(
            "AMAZON_SP_CLIENT_SECRET", platform=self.platform,
            purpose="LWA 应用的 client secret（Developer Central 的 LWA credentials）")
        refresh_token = read_credential(
            "AMAZON_SP_REFRESH_TOKEN", platform=self.platform,
            purpose="卖家授权应用后拿到的 LWA refresh token")
        secrets = (client_id, client_secret, refresh_token)

        body = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        }).encode("utf-8")
        response = self.transport.request(
            "POST", LWA_TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
            body=body)

        data = _json_object(response)
        if response.status >= 400:
            error = _scrub(str(data.get("error") or ""), secrets)
            description = _scrub(str(data.get("error_description") or ""), secrets)[:300]
            raise CommerceError(
                self.platform, response.status, error or f"LWA_HTTP_{response.status}",
                f"LWA 令牌换取失败（POST {LWA_TOKEN_URL}）：{description or '无 error_description'}。"
                "这一步还没到查单，订单号没有问题。invalid_grant 多半是 AMAZON_SP_REFRESH_TOKEN "
                "过期或被撤销（卖家取消了授权、或应用被重新授权后旧 token 作废），去 Seller "
                "Central 重新授权换新的；invalid_client / unauthorized_client 是 "
                "AMAZON_SP_CLIENT_ID / AMAZON_SP_CLIENT_SECRET 配错，或 client secret 已轮换")

        token = data.get("access_token")
        expires_in = data.get("expires_in")
        if (not isinstance(token, str) or not token
                or isinstance(expires_in, bool) or not isinstance(expires_in, (int, float))
                or expires_in <= 0):
            raise CommerceError(
                self.platform, response.status, "LWA_BAD_RESPONSE",
                "LWA 令牌换取的响应缺 access_token 或 expires_in 不是正数"
                f"（收到的键：{sorted(data)}）。不按 3600 秒兜底 —— 有效期以响应为准")

        self._access_token = token
        self._token_refresh_at = now + float(expires_in) - _EARLY_REFRESH
        log.info("amazon_sp: LWA access token 已换新（expires_in=%ss，提前 %ss 换新）",
                 expires_in, int(_EARLY_REFRESH))
        return token


def _json_object(response: HttpResponse) -> dict:
    """把响应体解成 dict；解不动返回空 dict（调用方按缺字段处理，不在这里抛）。"""
    try:
        data = json.loads(response.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _first_error(response: HttpResponse) -> tuple[str, str]:
    """读 SP-API 的错误体 `{"errors": [{"code", "message", "details"}]}` 的第一条。"""
    errors = _json_object(response).get("errors")
    if not isinstance(errors, list) or not errors or not isinstance(errors[0], dict):
        return "", response.text()[:500]
    first = errors[0]
    message = str(first.get("message") or "")
    details = str(first.get("details") or "")
    if details:
        message = f"{message}（{details}）"
    return str(first.get("code") or ""), message


def _scrub(text: str, secrets: tuple[str, ...]) -> str:
    """把凭据原值从平台回显的文字里抹掉，再让它进异常。"""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
    return text
