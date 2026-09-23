"""WooCommerce 订单适配器 + webhook 验签 —— A 档第二个：WordPress 自建站，零审核。

商家在 Woo 后台生成一对 REST API key（consumer key / consumer secret）就能用，
是国内商家做独立站最常见的那套。本模块只做**读订单**（`OrderSystemPort` 只有 `query`），
退款出款面走 `tools/gateway.py`，不在这里。

## 查单

    GET {WOO_SITE_URL}/wp-json/wc/v3/orders/{id}
    Authorization: Basic base64(consumer_key:consumer_secret)

订单对象**直接在响应顶层**，没有 Shopify 那种 ``{"order": ...}`` 的一层包装。

认证只走 HTTP Basic + HTTPS（WooCommerce REST API v3 文档 / Introduction /
Authentication / Authentication over HTTPS）。文档还给了两条退路，本模块都**不走**：

- 服务器不解析 Authorization 头时，把 key/secret 放进 query 参数 —— 凭据进 URL
  就会进日志、进 traceback，正是 `redact_url()` 要防的事；
- 纯 HTTP 站点要走 OAuth 1.0a —— 本批不做，`WOO_SITE_URL` 不是 ``https://`` 的直接拒，
  因为明文 HTTP 上发 Basic 头等于把 secret 交给链路上的任何人。

## `date_modified_gmt` 是 naive 串（本轨的核心）

Woo 的修改时间给两份：``date_modified``（站点本地时区）与 ``date_modified_gmt``
（GMT），两份都**不带时区标记**，形如 ``2026-09-15T14:30:00``。基座的
`derive_version()` 按契约拒收 naive 串，所以在 `parse_order` 里按文档语义给 GMT 那份
补上 ``Z`` 再交出去（出处见 `_pin_gmt`）。**不用 ``date_modified``**：它随站点时区设置变，
而站点时区是商家随时能在后台改的 —— 改一次，所有订单的 version 就整体平移几个小时。

## 状态：一维枚举，只映射文档说得清的三个

REST API v3 / Orders / Order properties 给的 ``status`` 取值域是
``pending`` / ``processing`` / ``on-hold`` / ``completed`` / ``cancelled`` /
``refunded`` / ``failed`` / ``trash``。各状态的含义以 WooCommerce 文档 Order Statuses
页那张表为准，只有三个能不走样地落进四态：

- ``processing`` → ``paid``：「Payment has been received (paid) … awaiting fulfillment」
- ``completed``  → ``shipped``：「The order has been fulfilled and is complete」
- ``cancelled``  → ``cancelled``

其余一律**不进表**，让 `map_status()` 抛 `UnmappedOrderStatus`（契约 §C.4：那是设计）：
``pending`` / ``on-hold`` / ``failed`` 都是「还没有确认到账」，四态里没有它的位置；
``refunded`` 是「已全额退款」，归到任何一态都是替平台翻译（铁律 8），而它恰恰是
最不该再往下走一笔退款的那种单；``trash`` 文档只在枚举里列了名字、没给含义，
核不到出处的规则不写。插件加的自定义状态同理。

## webhook

``X-WC-Webhook-Signature`` = base64(hmac_sha256(secret, 原始字节))，与 Shopify 同形，
走基座的 `verify_hmac_header()`（常量时间比对）。去重键 / event_id 取
``X-WC-Webhook-Delivery-ID``（每次投递新生成一个），**不是** ``X-WC-Webhook-ID``
（那是 webhook 配置本身的 id，同一个 webhook 的每次投递都一样 —— 拿它去重，
第二次投递起全会被判成重投、永不回源）。出处：WooCommerce REST API 文档 /
Webhooks / Delivery Headers，以及 ``WC_Webhook::deliver()`` 的源码
（Delivery-ID 由 ``get_new_delivery_id()`` 每次调用现算，Webhook-ID 是 ``get_id()``）。
"""

from __future__ import annotations

import base64
import json
from urllib.parse import quote, urlsplit

from maos.ingress.crypto import VerifyError
from maos.ingress.shop_callback import (
    VerifiedCallback, header, register_verifier, verify_hmac_header,
)
from maos.tools.commerce import (
    CommerceAdapter, CommerceError, HttpResponse, QueryRequest, RawOrder,
    StatusRule, read_credential,
)


PLATFORM = "woocommerce"

_STATUS_PAGE = "https://woocommerce.com/document/managing-orders/order-statuses/"

_RULES = (
    # 顺序即优先级。Woo 的状态是一维的，三条取值互斥，顺序不改变任何结论；
    # 仍把取消排第一，是按契约 §C.3 显式声明「取消优先」—— 日后有人往表里加一条
    # 看别的字段的规则时，这个优先级不会被悄悄反过来。
    StatusRule("status", ("cancelled",), "cancelled",
               source="WooCommerce 文档 / Order Statuses / Order statuses in WooCommerce / "
                      "Cancelled: The order was canceled by an admin or the customer. "
                      f"（{_STATUS_PAGE}）"),
    StatusRule("status", ("completed",), "shipped",
               source="WooCommerce 文档 / Order Statuses / Order statuses in WooCommerce / "
                      "Completed: The order has been fulfilled and is complete. "
                      f"（{_STATUS_PAGE}）"),
    StatusRule("status", ("processing",), "paid",
               source="WooCommerce 文档 / Order Statuses / Order statuses in WooCommerce / "
                      "Processing: Payment has been received (paid), and the stock has been "
                      f"reduced. The order is awaiting fulfillment. （{_STATUS_PAGE}）"),
)


class WooCommerceAdapter(CommerceAdapter):
    """WooCommerce REST API v3 的查单适配器。

    `account` 只是进 repr 的店铺标识（建议传站点主机名），**请求 URL 不由它拼** ——
    站点地址是 `WOO_SITE_URL`，与另外两把凭据一起在调用时从环境变量读。
    """

    platform = PLATFORM
    status_rules = _RULES

    def build_query_request(self, order_id: str) -> QueryRequest:
        base = _site_base()
        key = read_credential("WOO_CONSUMER_KEY", platform=self.platform,
                              purpose="REST API consumer key（只读订单即可）")
        secret = read_credential("WOO_CONSUMER_SECRET", platform=self.platform,
                                 purpose="REST API consumer secret（与 consumer key 成对生成）")
        token = base64.b64encode(f"{key}:{secret}".encode("utf-8")).decode("ascii")
        return QueryRequest(
            method="GET",
            # 订单号整段转义：它是路径的一段，带 / 或 .. 的输入不许拼出另一个端点。
            url=f"{base}/wp-json/wc/v3/orders/{quote(str(order_id), safe='')}",
            headers={"Authorization": f"Basic {token}", "Accept": "application/json"},
        )

    def parse_order(self, payload: dict) -> RawOrder:
        if payload.get("id") in (None, ""):
            # 不许让 KeyError 冒出去：OrderSystemPort 的契约里 KeyError 专指「订单不存在」，
            # 一个形状坏掉的 200 响应被读成「没这笔单」，排查方向就反了。
            raise CommerceError(self.platform, 200, "BAD_SHAPE",
                                "订单对象里没有 id 字段 —— 响应不像 Retrieve an order 的返回")
        return RawOrder(
            order_id=payload["id"],
            status_payload=payload,
            # total 是字符串（"128.50"），原样交给基座的 normalize_amount()，不进浮点。
            amount=payload.get("total"),
            updated_at=_pin_gmt(payload.get("date_modified_gmt")),
        )

    def read_error(self, response: HttpResponse) -> tuple[str, str]:
        # Woo 的错误体是 WordPress REST 的统一形状：{"code", "message", "data": {"status"}}
        # （WooCommerce REST API v3 文档 / Introduction / Errors）。解不动就回原文，不抛。
        try:
            body = json.loads(response.body or b"")
        except ValueError:
            return "", response.text()[:500]
        if not isinstance(body, dict):
            return "", response.text()[:500]
        return str(body.get("code", "")), str(body.get("message", ""))


def _site_base() -> str:
    """读 `WOO_SITE_URL` 并去掉末尾斜杠。不是 ``https://`` 的直接拒，不发请求。"""
    site = read_credential("WOO_SITE_URL", platform=PLATFORM,
                           purpose="店铺站点根地址，https:// 开头；WordPress 装在子目录的连子目录一起填")
    parts = urlsplit(site)
    if parts.scheme.lower() != "https" or not parts.netloc:
        # 消息里只报 scheme，不回显整条 URL —— 有人会把 user:pass@ 写进站点地址。
        raise ValueError(
            f"woocommerce 的 WOO_SITE_URL 必须以 https:// 开头（当前 scheme={parts.scheme or '空'}）："
            "本适配器用 HTTP Basic 传 consumer key/secret，明文 HTTP 上等于把密钥交给链路上的任何人；"
            "Woo 文档对纯 HTTP 站点要求 OAuth 1.0a，本批不支持"
        )
    return site.rstrip("/")


def _pin_gmt(value: object) -> object:
    """给 `date_modified_gmt` 补上 ``Z``。

    Woo 的 date_modified_gmt 语义是 GMT 但串里不带时区标记（WooCommerce REST API v3
    文档 / Orders / Order properties：``date_modified_gmt`` 「The date the order was last
    modified, as GMT」；同页响应示例是 ``"2017-03-22T19:28:08"`` 这种形状）。
    按契约 §B.1：naive 串要在适配器里按平台文档写死时区，不许让基座去猜 ——
    基座按本地 TZ 解会让同一笔单在两台机器上差几小时，漂移判据当场失效。

    缺值（None / 空串）原样交出去，由 `derive_version()` 抛 `UnparsableTimestamp`，
    不在这里另造一种异常。日后 Woo 若在这个字段里自带时区，补出来的 ``...ZZ`` 会被
    基座拒收 —— 那是 fail-closed，比悄悄解错强。
    """
    if isinstance(value, str) and value.strip():
        return value + "Z"
    return value


def verify_woocommerce(*, raw: bytes, headers: dict[str, str]) -> VerifiedCallback:
    """验一条 Woo webhook。**只认、不判**：不回源、不写库，认不出来抛 `VerifyError`。"""
    secret = read_credential("WOO_WEBHOOK_SECRET", platform=PLATFORM,
                             purpose="webhook 的 Secret（建 webhook 时填的那一把）")
    # 对原始字节算：json.loads 再 dumps 回去会改键序和空白，签名必然对不上。
    verify_hmac_header(secret, raw, header(headers, "X-WC-Webhook-Signature"))
    try:
        body = json.loads(raw)
    except ValueError as exc:
        # ingest() 只接 VerifyError，JSONDecodeError 冒出去会把整条请求带崩。
        # 这条消息里别写「时间戳」「依赖」：_classify() 按关键词分档。
        raise VerifyError("签名对得上但 body 不是 JSON，认不出这是哪笔单") from exc
    if not isinstance(body, dict):
        raise VerifyError(f"签名对得上但 body 顶层不是 JSON 对象，是 {type(body).__name__}")

    topic = header(headers, "X-WC-Webhook-Topic")
    order_ref = ""
    # 只有 order.* 主题的 id 才是订单号：同一个接收地址若也收了 product.* / customer.*，
    # body 里的 id 是商品 / 客户的，拿去回源查单只会得到一个假的 404。
    if topic.startswith("order.") and body.get("id") not in (None, ""):
        order_ref = str(body["id"])
    return VerifiedCallback(
        event_id=header(headers, "X-WC-Webhook-Delivery-ID"),
        topic=topic,
        order_ref=order_ref,
    )


register_verifier("woocommerce", verify_woocommerce)
