"""Shopify 订单适配器 + webhook 验签器 —— p11 七个平台里唯一一个个人当天就能拿到真凭据的。

## 查单走 REST Admin API 的 Order 资源

    GET https://{SHOPIFY_SHOP_DOMAIN}/admin/api/{API_VERSION}/orders/{order_id}.json
    Header: X-Shopify-Access-Token
    响应:   {"order": {...}}

Shopify 自 2024-10-01 起把 REST Admin API 标为 legacy，2025-04-01 起**新的公开应用**
必须用 GraphQL Admin API（shopify.dev / REST Admin API / Order 页顶部的公告）。
但 Order 资源在当前最新稳定版 2026-07 下仍在提供，custom app / 开发店照常可用，
而跨轨契约 §A/§C 的形状（`{"order": {...}}`、`cancelled_at` / `financial_status`
字段名、404 抛 KeyError）是按 REST 写的 —— 所以本轮走 REST，GraphQL 迁移记在 BACKLOG。
GraphQL 查不到单时回 200 + `null`，与基座「404 抛 KeyError」的形状不同，
迁它是要先改契约的事，不是在本文件里悄悄换掉的事。

`SHOPIFY_SHOP_DOMAIN` 形如 `my-shop.myshopify.com`，**已含域名后缀**，这里不再拼一次。
它与 access token 一样在拼请求的那一刻才从环境变量读（经基座的凭据读取函数）：
模块导入期不读任何环境变量，因为 T166 的一致性测试与 `scripts/gen_docs.py`
都会在空环境里动态导入本包下的每个模块，导入期一抛就是整张表一起炸。

## 状态是二维的：取消 > 发货 > 付款

Shopify 的订单状态分在三个字段上：`cancelled_at`（非空即已取消）、
`fulfillment_status`（发货维度）、`financial_status`（付款维度）。规则表按
「取消 > 发货 > 付款」排：付过款又取消的单，答案只能是 `cancelled`（契约 §C.3）。

**`partially_refunded` / `refunded` / `partially_paid` / `authorized` / `pending` /
`voided` 刻意不进表**，一律让基座抛 `UnmappedOrderStatus`。理由写在
`docs/DECISIONS.md ## task-t161`，要点是：退过款的单意味着钱已经动过，四态里没有一个词
说得出「钱已经退回去过一部分」—— 归成 `paid` 会重复退款，归成 `amended` 也拦不住
（`refund.snapshot_check` 只比版本与金额，不看状态）。抛出去之后
`snapshot_check` 把读失败判成漂移、转人工，这才是真正能停下来的那条路。

**已知缺口**：规则只能正向命中，一笔「已发货 + 部分退款」的单会先命中发货那条、
归成 `shipped`，退款维度被发货维度盖住。单字段规则表表达不了「退过款即拦」，
这件事记在 BACKLOG，不在 `parse_order` 里加分支绕过去（契约 §C.1）。

## webhook：`order_ref` 按 topic 取

投递体是「该 topic 对应的完整 REST 资源」（shopify.dev / Webhooks / Delivery structure），
所以 `orders/*` 的顶层 `id` 是订单号，而 `refunds/create` 的投递体是 **Refund 资源**，
顶层 `id` 是退款单号、订单号在 `order_id`。契约 §E 代码块里的 `str(body["id"])`
是按 `orders/*` 写的示意，照抄的话偏偏是退款事件会拿退款单号去回源 —— 本文件按 topic 取。
没核实过投递体形状的 topic（例如 `orders/edited`，它的资源是 order_edit 不是 order）
一律给空串：`should_resync` 自然为 False，落库照常，不会拿错的号去回源。
"""

from __future__ import annotations

import json
from urllib.parse import quote

from maos.ingress.crypto import VerifyError
from maos.ingress.shop_callback import (
    VerifiedCallback, header, register_verifier, verify_hmac_header,
)
from maos.tools.commerce import (
    ANY_NON_EMPTY, CommerceAdapter, CommerceError, HttpResponse, QueryRequest,
    RawOrder, StatusRule, read_credential,
)


#: REST Admin API 版本。Shopify 每季度（01/04/07/10 月）出一版、每版至少支持 12 个月
#: （shopify.dev / API versioning / Release schedule）。2026-07 是 2026-09-23 核实时的
#: 最新稳定版，可访问到 2027-07-16。升版只改这一行 —— URL 里一律用它拼，不在别处写死。
API_VERSION = "2026-07"


_RULES = (
    # 顺序即优先级，第一条命中即返回：取消 > 发货 > 付款（契约 §C.3）。
    StatusRule(
        "cancelled_at", ANY_NON_EMPTY, "cancelled",
        source="Shopify REST Admin API 2026-07 / Order / cancelled_at: "
               "The date and time when the order was canceled. "
               "Returns null if the order isn't canceled."),
    StatusRule(
        "fulfillment_status", ("restocked",), "cancelled",
        source="Shopify REST Admin API 2026-07 / Order / fulfillment_status: restocked - "
               "Every line item in the order has been restocked and the order canceled."),
    # partial 也归 shipped：至少有一件已经出库，退款要按「货已离仓」的口径处理。
    StatusRule(
        "fulfillment_status", ("fulfilled", "partial"), "shipped",
        source="Shopify REST Admin API 2026-07 / Order / fulfillment_status: "
               "fulfilled - Every line item in the order has been fulfilled; "
               "partial - At least one line item in the order has been fulfilled."),
    StatusRule(
        "financial_status", ("paid",), "paid",
        source="Shopify REST Admin API 2026-07 / Order / financial_status: paid - "
               "The payments have been paid."),
)


class ShopifyAdapter(CommerceAdapter):
    """Shopify 的 `OrderSystemPort` 实现。只填三个钩子，`query()` 是基座的成品流程。"""

    platform = "shopify"
    status_rules = _RULES

    def build_query_request(self, order_id: str) -> QueryRequest:
        shop = read_credential(
            "SHOPIFY_SHOP_DOMAIN", platform=self.platform,
            purpose="店铺域名，形如 my-shop.myshopify.com（已含 .myshopify.com 后缀）")
        token = read_credential(
            "SHOPIFY_ACCESS_TOKEN", platform=self.platform,
            purpose="Admin API access token（scope: read_orders）")
        # order_id 来自上游入参，整段转义：带斜杠的单号不许把请求改道到别的资源上。
        path = quote(str(order_id), safe="")
        return QueryRequest(
            method="GET",
            url=f"https://{shop}/admin/api/{API_VERSION}/orders/{path}.json",
            headers={"X-Shopify-Access-Token": token, "Accept": "application/json"},
        )

    def parse_order(self, payload: dict) -> RawOrder:
        order = payload.get("order")
        if not isinstance(order, dict):
            raise CommerceError(self.platform, 200, "BAD_SHAPE",
                                "响应体里没有 order 对象（REST Order 资源应为 {\"order\": {...}}）")
        # 订单主键缺了抛 CommerceError 而不是 KeyError：query() 的调用方把 KeyError
        # 读作「订单不存在」，响应缺字段与订单不存在是两回事。
        if order.get("id") in (None, ""):
            raise CommerceError(self.platform, 200, "BAD_SHAPE", "order 对象里没有 id")
        # 主键是 id，不是 order_number（给客户看的单号）。金额与修改时刻原样交出去，
        # 缺了由基座的归一函数各自抛，不在这里填默认值。
        return RawOrder(
            order_id=str(order["id"]),
            status_payload=order,
            amount=order.get("total_price"),
            updated_at=order.get("updated_at"),
        )

    def read_error(self, response: HttpResponse) -> tuple[str, str]:
        """Shopify REST 的错误体是 `{"errors": ...}`（422 也可能是 `error`），
        取值可以是字符串、也可以是「字段 → 消息列表」的对象；REST 没有业务错误码，
        code 一律给空串。**不抛**：这里是在处理一个已经出错的响应。"""
        text = response.text()
        try:
            body = json.loads(text)
        except ValueError:
            return "", text[:500]
        if isinstance(body, dict):
            detail = body.get("errors", body.get("error"))
            if isinstance(detail, str):
                return "", detail[:500]
            if detail is not None:
                return "", json.dumps(detail, ensure_ascii=False)[:500]
        return "", text[:500]


#: 投递体是订单资源本身的 topic（顶层 id 即订单号）。只列核过的；
#: orders/edited 的资源是 order_edit，形状不同，刻意不在这里。
_ORDER_TOPICS = frozenset({
    "orders/create", "orders/updated", "orders/cancelled", "orders/paid",
    "orders/fulfilled", "orders/partially_fulfilled", "orders/delete",
})

#: 投递体是 Refund 资源的 topic：顶层 id 是退款单号，订单号在 order_id。
_REFUND_TOPICS = frozenset({"refunds/create"})


def _order_ref(topic: str, body: dict) -> str:
    """按 topic 取这条回调指向的订单号。不指向订单（或形状没核过）的 topic 给空串。"""
    name = topic.strip().lower()
    if name in _ORDER_TOPICS:
        field = "id"
    elif name in _REFUND_TOPICS:
        field = "order_id"
    else:
        return ""
    value = body.get(field)
    if value is None or not str(value).strip():
        raise VerifyError(f"{name} 的投递体里没有 {field}，认不出指向哪笔订单")
    return str(value).strip()


def verify_shopify(*, raw: bytes, headers: dict) -> VerifiedCallback:
    """Shopify webhook 验签器。**只认、不判**：不回源、不写库。

    签名是 `base64(HMAC-SHA256(secret, 原始字节))`，放在 `X-Shopify-Hmac-Sha256`
    （shopify.dev / Webhooks / Verify deliveries）。对原始字节算、常量时间比对，
    两件事都交给基座的 `verify_hmac_header`。去重键取 `X-Shopify-Webhook-Id`
    （每次投递唯一；`X-Shopify-Event-Id` 是同一次商家动作的多次投递共用的，不拿来去重）。
    缺投递 id 时这里不抛、照常返回空串，由 `ingest()` 判 `no_event_id`。
    """
    secret = read_credential(
        "SHOPIFY_WEBHOOK_SECRET", platform="shopify",
        purpose="webhook 签名密钥（应用的 client secret）")
    verify_hmac_header(secret, raw, header(headers, "X-Shopify-Hmac-Sha256"))
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise VerifyError(f"签名对得上但投递体不是 JSON：{type(exc).__name__}") from exc
    if not isinstance(body, dict):
        raise VerifyError(f"投递体顶层不是对象，是 {type(body).__name__}")
    topic = header(headers, "X-Shopify-Topic")
    return VerifiedCallback(
        event_id=header(headers, "X-Shopify-Webhook-Id"),
        topic=topic,
        order_ref=_order_ref(topic, body),
    )


register_verifier("shopify", verify_shopify)
