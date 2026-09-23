"""TikTok Shop 适配器 —— Order API 202309 查单 + webhook 验签。

## 出处

本文件每一处平台细节都来自 TikTok Shop Partner Center 的官方文档（2026-09-23 核对）：

- 签名：「Sign your API request」
  https://partner.tiktokshop.com/docv2/page/sign-your-api-request
- 查单：「Get Order Detail」（202309）
  https://partner.tiktokshop.com/docv2/page/get-order-detail-202309
- 公共参数：「Common parameters」
  https://partner.tiktokshop.com/docv2/page/common-parameters
- webhook：「Webhooks Overview」与「(1) Order status change」
  https://partner.tiktokshop.com/docv2/page/tts-webhooks-overview
  https://partner.tiktokshop.com/docv2/page/650300b8a57708028b430b4a

## 签名是十六进制，不是 base64

请求签名与 webhook 签名都是 HMAC-SHA256 的**小写十六进制**摘要（两页文档原文都这么写，
官方样例在本机复算逐字一致）。跨轨契约 §E.5 把 TikTok 列进「HMAC-SHA256 + base64 三家共用」
是写错了，所以这里**不用** `hmac_sha256_base64` / `verify_hmac_header`（它们只做 base64），
自己算 hex、再走 `crypto.equal` 常量时间比对。

## 凭据只在调用时读

导入本模块、实例化适配器都不读任何环境变量：`scripts/gen_docs.py` 会 import
`maos.tools` 下每个模块，一致性测试与整合期会在没有凭据的环境里实例化全部适配器。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse
from typing import Callable, Mapping

from maos.ingress.crypto import VerifyError, equal
from maos.ingress.shop_callback import VerifiedCallback, header, register_verifier
from maos.tools.commerce import (
    CommerceAdapter, CommerceError, HttpResponse, QueryRequest, RawOrder, StatusRule,
    read_credential,
)


PLATFORM = "tiktok_shop"

#: Get Order Detail 请求示例里的主机。
API_HOST = "https://open-api.tiktokglobalshop.com"
ORDER_DETAIL_PATH = "/order/202309/orders"

#: 签名串里要排除的 query 参数（Sign your API request 第 1 步）。
_UNSIGNED_PARAMS = frozenset({"sign", "access_token"})

_DOC_ORDER = "https://partner.tiktokshop.com/docv2/page/get-order-detail-202309"


def _enum_source(value: str, meaning: str) -> str:
    return f"TikTok Shop Get Order Detail (202309) / data.orders[].status 枚举 {value}「{meaning}」/ {_DOC_ORDER}"


#: 状态归一表。field 是 Get Order Detail 响应里订单对象的 `status`。
#:
#: 顺序即优先级，取消排最前（契约 §C.3）。**不进表**的枚举值：
#: `UNPAID`（没付款，不属于四态任何一态）、`ON_HOLD`（文档原话「买家仍可不经卖家同意取消」，
#: 正是契约 §C.1 说的最不该往下走的状态）、`PARTIALLY_SHIPPING`（部分发货，归 shipped 会把
#: 「还有货没发」说成「货已发出」）。它们走 `UnmappedOrderStatus`，那是设计。
#: 超出文档字面语义的四条（delivered / completed → shipped，awaiting_shipment /
#: awaiting_collection → paid）理由见 `docs/DECISIONS.md` 的 `## task-t165`。
_RULES = (
    StatusRule("status", ("cancelled",), "cancelled",
               source=_enum_source("CANCELLED", "The order is cancelled")),
    StatusRule("status", ("in_transit",), "shipped",
               source=_enum_source("IN_TRANSIT", "The package is collected by the carrier "
                                                 "and delivery is in progress")),
    StatusRule("status", ("delivered",), "shipped",
               source=_enum_source("DELIVERED", "The package is delivered to buyer")),
    StatusRule("status", ("completed",), "shipped",
               source=_enum_source("COMPLETED", "The order is completed, and no further "
                                                "returns or refunds are allowed")),
    StatusRule("status", ("awaiting_shipment",), "paid",
               source=_enum_source("AWAITING_SHIPMENT", "The order is ready for shipment, "
                                                        "but no items are shipped yet")),
    StatusRule("status", ("awaiting_collection",), "paid",
               source=_enum_source("AWAITING_COLLECTION", "The shipment is arranged, but the "
                                                          "package is waiting to be collected "
                                                          "by the carrier")),
)


# ---- 签名：两个纯函数，各自可单测 ------------------------------------------------

def sign_base_string(path: str, params: Mapping[str, str], app_secret: str,
                     body: bytes = b"") -> bytes:
    """拼请求签名串（Sign your API request 第 1–5 步），返回**已被 app_secret 包裹**的整串。

    1. query 参数去掉 `sign` / `access_token`，按 key 字典序排；
    2. 拼成 `{key}{value}`；
    3. 前面接请求路径；
    4. content-type 不是 multipart/form-data 时，后面接请求体原始字节（GET 没有体，接空）；
    5. 整串前后各包一层 app_secret。
    """
    keys = sorted(k for k in params if k not in _UNSIGNED_PARAMS)
    joined = "".join(f"{k}{params[k]}" for k in keys)
    secret = app_secret.encode("utf-8")
    return secret + path.encode("utf-8") + joined.encode("utf-8") + body + secret


def sign(base_string: bytes, app_secret: str) -> str:
    """HMAC-SHA256（key 是 app_secret），输出小写十六进制。

    请求签名（第 6 步）与 webhook 签名是同一个形状，只是被签的串不同。
    """
    return hmac.new(app_secret.encode("utf-8"), base_string, hashlib.sha256).hexdigest()


def webhook_base_string(app_key: str, raw: bytes) -> bytes:
    """webhook 签名串：`app_key + 原始请求体`（Webhooks Overview）。

    对**原始字节**拼，不对解析后的 JSON 重新序列化（契约 §E.1）。
    """
    return app_key.encode("utf-8") + raw


# ---- 适配器 -------------------------------------------------------------------

class TikTokShopAdapter(CommerceAdapter):
    """TikTok Shop 查单。`account` 传 **shop_cipher**（202309 版 Get Order Detail 的必填
    query 参数）—— 它是店铺标识不是密钥，进 repr 正好方便排查「哪个店」。"""

    platform = PLATFORM
    status_rules = _RULES

    def __init__(self, *, transport=None, account: str = "",
                 clock: Callable[[], float] | None = None) -> None:
        super().__init__(transport=transport, account=account)
        # clock 可注入 —— 签名串里有 timestamp，不注入的话 golden 用例就没法写。
        self._clock = clock if callable(clock) else time.time

    def build_query_request(self, order_id: str) -> QueryRequest:
        if not self.account:
            raise ValueError(
                "TikTok Shop 202309 查单必须带 shop_cipher（Get Order Detail 的必填 query 参数），"
                "请在构造适配器时用 account= 传入")
        app_key = read_credential("TIKTOK_SHOP_APP_KEY", platform=self.platform,
                                  purpose="Partner Center 应用的 app_key")
        app_secret = read_credential("TIKTOK_SHOP_APP_SECRET", platform=self.platform,
                                     purpose="Partner Center 应用的 app_secret（请求签名与 webhook 验签共用）")
        token = read_credential("TIKTOK_SHOP_ACCESS_TOKEN", platform=self.platform,
                                purpose="店铺授权的 access token（202309 起走 x-tts-access-token 头）")
        params = {
            "app_key": app_key,
            "ids": str(order_id),
            "shop_cipher": self.account,
            "timestamp": str(int(self._clock())),
        }
        params["sign"] = sign(sign_base_string(ORDER_DETAIL_PATH, params, app_secret), app_secret)
        return QueryRequest(
            method="GET",
            url=f"{API_HOST}{ORDER_DETAIL_PATH}?{urllib.parse.urlencode(params)}",
            headers={"x-tts-access-token": token, "content-type": "application/json"},
        )

    def parse_order(self, payload: dict) -> RawOrder:
        # 信封：code 0 是成功；非 0 是业务错误，HTTP 码可能仍是 200。
        code = payload.get("code")
        if code != 0:
            raise CommerceError(self.platform, 200, "" if code is None else str(code),
                                str(payload.get("message", "")))
        data = payload.get("data")
        orders = data.get("orders") if isinstance(data, dict) else None
        if orders is not None and not isinstance(orders, list):
            raise CommerceError(self.platform, 200, "BAD_SHAPE",
                                f"data.orders 不是数组，是 {type(orders).__name__}")
        if not orders:
            # 文档没有「查无此单」的专用错误码；code 0 而订单数组为空就是没查到。
            raise KeyError(f"{self.platform} 里没有订单（HTTP 200、code 0，但 data.orders 为空）")
        order = orders[0]
        if not isinstance(order, dict) or order.get("id") in (None, ""):
            # 不让它以 KeyError 冒出去 —— 那会被调用方读成「订单不存在」。
            raise CommerceError(self.platform, 200, "BAD_SHAPE", "data.orders[0] 缺订单 id")
        payment = order.get("payment")
        return RawOrder(
            order_id=order["id"],
            status_payload=order,
            # total_amount 是字符串，原样交给 normalize_amount；payment 缺了就交 None 让基座抛。
            amount=payment.get("total_amount") if isinstance(payment, dict) else None,
            # update_time 是 epoch 秒（文档：Unix timestamp），原样交出去。
            updated_at=order.get("update_time"),
        )

    def read_error(self, response: HttpResponse) -> tuple[str, str]:
        try:
            body = json.loads(response.body)
        except (ValueError, UnicodeDecodeError):
            return "", response.text()[:500]
        if not isinstance(body, dict):
            return "", response.text()[:500]
        return str(body.get("code", "")), str(body.get("message", ""))


# ---- webhook 验签 ---------------------------------------------------------------

def verify_webhook(*, raw: bytes, headers: dict) -> VerifiedCallback:
    """认一条 TikTok Shop webhook。只认、不判、不回源、不写库（契约 §E.4）。

    签名在 `Authorization` 头里，值是 `hex(HMAC-SHA256(app_secret, app_key + 原始体))`。
    投递 id 是 body 的 `tts_notification_id`；签名对而缺它时返回空 `event_id`，
    由 `ingest` 判 400 / `no_event_id`（抛 `VerifyError` 会被归成 `bad_signature`）。
    """
    supplied = header(headers, "Authorization")
    if not supplied:
        raise VerifyError("请求没带签名头 Authorization")
    app_key = read_credential("TIKTOK_SHOP_APP_KEY", platform=PLATFORM,
                              purpose="Partner Center 应用的 app_key（webhook 签名串的前缀）")
    app_secret = read_credential("TIKTOK_SHOP_APP_SECRET", platform=PLATFORM,
                                 purpose="Partner Center 应用的 app_secret（webhook 验签的 HMAC key）")
    expected = sign(webhook_base_string(app_key, raw), app_secret)
    if not equal(expected, supplied):
        raise VerifyError("签名不匹配")

    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise VerifyError("回调体不是合法 JSON") from None
    if not isinstance(body, dict):
        raise VerifyError("回调体顶层不是对象")
    data = body.get("data")
    if data is not None and not isinstance(data, dict):
        raise VerifyError("回调体的 data 不是对象")
    fields = {
        "tts_notification_id": body.get("tts_notification_id"),
        "type": body.get("type"),
        "data.order_id": (data or {}).get("order_id"),
    }
    for name, value in fields.items():
        if value is not None and (isinstance(value, bool) or not isinstance(value, (str, int))):
            raise VerifyError(f"回调体字段 {name} 类型不对：{type(value).__name__}")
    return VerifiedCallback(
        event_id="" if fields["tts_notification_id"] is None else str(fields["tts_notification_id"]),
        topic="" if fields["type"] is None else str(fields["type"]),
        order_ref="" if fields["data.order_id"] is None else str(fields["data.order_id"]),
    )


register_verifier(PLATFORM, verify_webhook)
