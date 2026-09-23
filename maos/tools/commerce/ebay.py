"""eBay 订单适配器 + 通知验签 —— 七个平台里**唯一不是 HMAC** 的那一家。

## 查单：Sell Fulfillment API v1 / getOrder

    GET https://api.ebay.com/sell/fulfillment/v1/order/{orderId}          # production
    GET https://api.sandbox.ebay.com/sell/fulfillment/v1/order/{orderId}  # sandbox
    Authorization: Bearer <EBAY_OAUTH_TOKEN>

`EBAY_OAUTH_TOKEN` 是**用户令牌**（authorization code grant，scope
`sell.fulfillment` 或 `sell.fulfillment.readonly`）—— getOrder 读的是某个卖家的单，
应用令牌读不了。

状态是**三维**的：`orderFulfillmentStatus` × `orderPaymentStatus` × `cancelStatus`。
规则表的顺序即优先级：取消 > 发货 > 付款。发货排在付款前面，是因为 eBay 的单
几乎都是同时 `PAID` 又 `FULFILLED`，付款排前面的话每一笔发了货的单都会被判成
`paid`。

**取消看 `cancelStatus.cancelledDate`，不看 `cancelState`。** 后者「没人申请取消」时
的取值 `NONE_REQUESTED` 本身就是非空串，写成「非空即取消」会把每一笔正常单判成
`cancelled`；而它的完整枚举表（`CancelStateEnum`）这一轮没能从官方文档核到，
核不到的取值一条都不写。`cancelledDate` 在 `CancelStatus` 类型里写明是「订单被取消的
时刻，if applicable」—— 与 Shopify 的 `cancelled_at` 同形，非空即命中。

## 通知：Notification API 的 ECDSA 验签

`X-EBAY-SIGNATURE` 头是 base64 编码的一段 JSON：
``{"alg": "ecdsa", "kid": "<公钥 id>", "signature": "<base64 签名>", "digest": "SHA1"}``。
拿 `kid` 调 Notification API 的 getPublicKey 取公钥，再用 SHA1 + ECDSA 对**原始请求体**
验签（官方 Java 示例是 ``sig.update(messagePayload.getBytes())``，签的就是原文）。

取公钥要的是**应用令牌**（client credentials grant，scope `api_scope`），与查单的
用户令牌不是同一种 —— 所以另起一个环境变量 `EBAY_APP_TOKEN`，不复用
`EBAY_OAUTH_TOKEN`。公钥按 `kid` 缓存在**进程内**（官方建议缓存一小时左右，别每条
通知都去取，会撞调用限额），不落盘。

ECDSA 需要可选依赖 `cryptography`（`pyproject.toml` 的 `ingress` extra），**在函数里
惰性 import**，模块导入不吃依赖；缺了抛含「依赖」二字的 `VerifyError`，
`shop_callback._classify()` 据此归到 `dep_missing`。绝不静默回落成「验签通过」。

## 凭据与环境变量一律在**调用时**读

模块导入时不读任何环境变量 —— T166 的一致性测试会动态导入本包下的全部模块，
导入期副作用会在整合时炸。`EBAY_ENV` 有缺省值（sandbox），所以不走
`read_credential()`（那个缺了就抛），在用到的地方用 `os.environ.get` 读。
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import quote

from maos.ingress.crypto import VerifyError, equal
from maos.ingress.shop_callback import VerifiedCallback, header, register_verifier
from maos.tools.commerce import (
    ANY_NON_EMPTY, CommerceAdapter, CommerceError, CredentialMissing, HttpResponse,
    HttpTransport, QueryRequest, RawOrder, StatusRule, UrllibTransport, dig,
    read_credential,
)


PLATFORM = "ebay"

#: `EBAY_ENV` 的两个取值 → API 主机。**缺省 sandbox**：默认指向生产是这类适配器
#: 最贵的默认 —— 一次手滑就是拿真凭据去读真卖家的单。
_HOSTS = {
    "sandbox": "https://api.sandbox.ebay.com",
    "production": "https://api.ebay.com",
}

#: 查单用的用户令牌（契约 §D.2 表里的名字）。
ORDER_TOKEN_ENV = "EBAY_OAUTH_TOKEN"

#: 取通知公钥用的应用令牌。契约表里没有它：getPublicKey 文档要求 client credentials
#: grant 签出的令牌，而查单要的是 authorization code grant 的用户令牌，两者不能互换。
APP_TOKEN_ENV = "EBAY_APP_TOKEN"

SIGNATURE_HEADER = "X-EBAY-SIGNATURE"

#: 公钥在进程内缓存多久（秒）。官方建议「临时但合理」的缓存，举例一小时。
PUBLIC_KEY_TTL = 3600.0


def _api_host() -> str:
    """按 `EBAY_ENV` 选主机。**调用时读**，认不出的值抛，不回落。

    空串按「没设」处理（口径同 `read_credential` 把空白当缺失）；
    写了 `prod` / `live` 这种认不出的值则抛 —— 回落到 sandbox 会让人以为自己在打生产，
    回落到生产更糟。
    """
    raw = os.environ.get("EBAY_ENV", "")
    env = raw.strip().lower() or "sandbox"
    host = _HOSTS.get(env)
    if host is None:
        raise ValueError(
            f"EBAY_ENV={raw!r} 认不出：只收 sandbox / production（不设即 sandbox）。"
            "不回落到任何一边 —— 猜错方向的代价是拿真凭据打错环境"
        )
    return host


# ---------------------------------------------------------------------------
# 查单
# ---------------------------------------------------------------------------

_RULES = (
    # 顺序即优先级（契约 §C.3）：取消 > 发货 > 付款。
    # 付过款又取消的单，答案只能是 cancelled；发了货的单几乎都同时是 PAID，
    # 所以发货必须排在付款之前，否则每一笔发了货的单都会被判成 paid。
    StatusRule(
        "cancelStatus.cancelledDate", ANY_NON_EMPTY, "cancelled",
        source="eBay Sell Fulfillment API v1 / getOrder / cancelStatus.cancelledDate"
               "（CancelStatus 类型：The date and time the order was cancelled, if applicable）",
    ),
    StatusRule(
        "orderFulfillmentStatus", ("fulfilled",), "shipped",
        source="eBay Sell Fulfillment API v1 / getOrder / orderFulfillmentStatus / "
               "OrderFulfillmentStatus.FULFILLED（line item 已处理、打包并发出）",
    ),
    StatusRule(
        "orderFulfillmentStatus", ("in_progress",), "shipped",
        source="eBay Sell Fulfillment API v1 / getOrder / orderFulfillmentStatus / "
               "OrderFulfillmentStatus.IN_PROGRESS（多 line item 的单：卖家已开始发货、尚未全部发出）",
    ),
    StatusRule(
        "orderPaymentStatus", ("paid",), "paid",
        source="eBay Sell Fulfillment API v1 / getOrder / orderPaymentStatus / "
               "OrderPaymentStatusEnum.PAID（订单已全额付款）",
    ),
    # 刻意不写：NOT_STARTED（没发货不是结论，交给付款维度判）、PENDING / FAILED
    # （付款没完成）、FULLY_REFUNDED / PARTIALLY_REFUNDED（退过款）。它们落到这里
    # 就抛 UnmappedOrderStatus —— 退过款的单恰恰是最不该被当成「可以退」往下走的。
)


class EbayAdapter(CommerceAdapter):
    """eBay Sell Fulfillment API 的查单适配器。只填三个钩子，`query()` 用基座的。"""

    platform = PLATFORM
    status_rules = _RULES

    def build_query_request(self, order_id: str) -> QueryRequest:
        host = _api_host()
        token = read_credential(
            ORDER_TOKEN_ENV, platform=self.platform,
            purpose="Sell Fulfillment API 用户令牌（authorization code grant，"
                    "scope: sell.fulfillment.readonly）",
        )
        return QueryRequest(
            method="GET",
            url=f"{host}/sell/fulfillment/v1/order/{quote(str(order_id), safe='')}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )

    def parse_order(self, payload: dict) -> RawOrder:
        order_id = payload.get("orderId")
        if not isinstance(order_id, str) or not order_id.strip():
            # 不抛 KeyError：那是 OrderSystemPort 的「查无此单」，响应缺字段是另一回事。
            raise CommerceError(
                self.platform, 200, "BAD_SHAPE",
                "getOrder 响应里没有 orderId —— 不填默认值，取不到就停")
        return RawOrder(
            order_id=order_id,
            status_payload=payload,
            # 金额与时间取不到时原样交 None，由基座的 normalize_amount /
            # derive_version 抛 —— 不在这里填 "0" 或 now()。
            amount=dig(payload, "pricingSummary.total.value"),
            # 原样交出（ISO 8601 UTC 带毫秒与 Z），它同时是 version 的来源。
            updated_at=payload.get("lastModifiedDate"),
        )

    def read_error(self, response: HttpResponse) -> tuple[str, str]:
        """eBay REST 的错误体是 ``{"errors": [{"errorId", "message", "longMessage", ...}]}``。
        读不出来就返回原文前 500 字节，**不抛**（基座钩子的约定）。"""
        try:
            body = json.loads(response.body)
        except (ValueError, UnicodeDecodeError):
            return "", response.text()[:500]
        errors = body.get("errors") if isinstance(body, dict) else None
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            first = errors[0]
            message = first.get("longMessage") or first.get("message") or ""
            return str(first.get("errorId", "")), str(message)
        return "", response.text()[:500]


# ---------------------------------------------------------------------------
# 通知验签
# ---------------------------------------------------------------------------

#: 验签后端：``(公钥 DER, 签名字节, 原始请求体) -> 对不对得上``。
EcdsaVerify = Callable[[bytes, bytes, bytes], bool]


def load_ecdsa_backend() -> EcdsaVerify:
    """惰性 import `cryptography`，返回 SHA1 + ECDSA 的验签函数。

    缺依赖时 `ImportError` **原样冒出去**，由验签器转成含「依赖」的 `VerifyError` ——
    转换那一步不在这里，于是测试把这个函数整个替换掉（或把 `cryptography` 从
    `sys.modules` 里拿掉）时，「缺依赖 → 拒绝」这条路径照样走的是生产代码。

    **每次调用都重新 import，不在模块级缓存结果**：`sys.modules` 让重复 import 几乎
    不花钱，而缓存会让「这台机器装了 cryptography、先跑过一次正向验签」之后，
    缺依赖的那条路径再也强制不出来。
    """
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    def _verify(public_key_der: bytes, signature: bytes, raw: bytes) -> bool:
        key = serialization.load_der_public_key(public_key_der)
        if not isinstance(key, ec.EllipticCurvePublicKey):
            raise ValueError(f"getPublicKey 回的不是 EC 公钥：{type(key).__name__}")
        try:
            # 签名是 DER 编码的 (r, s)，与 Java 的 SHA1withECDSA 默认格式一致。
            key.verify(signature, raw, ec.ECDSA(hashes.SHA1()))
        except InvalidSignature:
            return False
        return True

    return _verify


@dataclass(frozen=True)
class _SignatureEnvelope:
    kid: str
    signature: bytes


def _parse_signature_header(value: str) -> _SignatureEnvelope:
    """拆 `X-EBAY-SIGNATURE`。**在惰性 import 之前做**：缺头、头不是合法信封这类
    结构性错误不需要密码学库就判得出，本机没装 `cryptography` 也测得到。

    这里抛的 `VerifyError` 消息里不许出现「依赖」「时间戳」—— `_classify()`
    按关键词分档，混进去就分错了。
    """
    if not value.strip():
        raise VerifyError(f"请求没带 {SIGNATURE_HEADER} 签名头")
    try:
        packed = json.loads(base64.b64decode(value.strip()))
    except (ValueError, UnicodeDecodeError) as exc:
        raise VerifyError(f"{SIGNATURE_HEADER} 不是 base64 编码的 JSON 信封") from exc
    if not isinstance(packed, dict):
        raise VerifyError(f"{SIGNATURE_HEADER} 解出来不是 JSON 对象")
    fields = {k: packed.get(k) for k in ("alg", "kid", "signature", "digest")}
    missing = sorted(k for k, v in fields.items() if not isinstance(v, str) or not v.strip())
    if missing:
        raise VerifyError(f"{SIGNATURE_HEADER} 信封缺字段：{missing}")
    if not equal(fields["alg"].strip().lower(), "ecdsa"):
        raise VerifyError(f"{SIGNATURE_HEADER} 的 alg={fields['alg']!r}，只认 ECDSA")
    if not equal(fields["digest"].strip().upper(), "SHA1"):
        raise VerifyError(f"{SIGNATURE_HEADER} 的 digest={fields['digest']!r}，只认 SHA1")
    try:
        signature = base64.b64decode(fields["signature"].strip())
    except ValueError as exc:
        raise VerifyError(f"{SIGNATURE_HEADER} 里的 signature 不是合法 base64") from exc
    if not signature:
        raise VerifyError(f"{SIGNATURE_HEADER} 里的 signature 为空")
    return _SignatureEnvelope(kid=fields["kid"].strip(), signature=signature)


_PEM_MARKER = re.compile(r"-----(?:BEGIN|END) PUBLIC KEY-----")


def _public_key_der(key_text: str) -> bytes:
    """getPublicKey 回的 `key` 是**单行** PEM（``-----BEGIN PUBLIC KEY-----MFkw...-----END
    PUBLIC KEY-----``，中间不换行），`load_pem_public_key` 不一定认。官方 Java 示例是
    剥掉包裹、base64 解成 X.509 SubjectPublicKeyInfo —— 这里照做，单行多行都吃。"""
    body = "".join(_PEM_MARKER.sub("", key_text).split())
    try:
        der = base64.b64decode(body, validate=True)
    except ValueError as exc:
        raise VerifyError("getPublicKey 回的 key 不是合法的 PEM / base64") from exc
    if not der:
        raise VerifyError("getPublicKey 回的 key 为空")
    return der


def _text(value: object) -> str:
    return str(value).strip() if isinstance(value, (str, int)) else ""


def make_ebay_verifier(*, transport: HttpTransport,
                       backend: Callable[[], EcdsaVerify] = load_ecdsa_backend,
                       clock: Callable[[], float] = time.monotonic,
                       key_ttl: float = PUBLIC_KEY_TTL):
    """造一个 eBay 通知验签器（签名形状同 `shop_callback.CallbackVerifier`）。

    `transport` 用来取公钥，测试注入 `FakeTransport`；`backend` 是「惰性 import +
    ECDSA 验签」整步，测试可以注入一个不 import 的替身。**工厂本身不读任何环境变量、
    不发任何请求** —— 模块顶层会调它一次。

    顺序是钉死的：拆头 → 惰性 import → 取公钥（读凭据、发请求）→ 验签 → 认内容。
    缺依赖时一个凭据都不读、一个请求都不发。取公钥不是「回源」：契约 §E 第 4 条
    禁的是回源订单与写库，这里只读 eBay 发布的公钥。
    """
    #: (主机, kid) -> (取到的时刻, 公钥 DER)。只活在本进程里，不落盘。
    cache: dict[tuple[str, str], tuple[float, bytes]] = {}

    def _public_key(kid: str) -> bytes:
        try:
            host = _api_host()
            hit = cache.get((host, kid))
            now = clock()
            if hit is not None and now - hit[0] < key_ttl:
                return hit[1]
            token = read_credential(
                APP_TOKEN_ENV, platform=PLATFORM,
                purpose="Notification API 应用令牌（client credentials grant，"
                        "scope: api_scope），用于 getPublicKey",
            )
            response = transport.request(
                "GET", f"{host}/commerce/notification/v1/public_key/{quote(kid, safe='')}",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
        except (CredentialMissing, ValueError) as exc:
            raise VerifyError(f"取 eBay 公钥失败：{exc}") from exc
        except Exception as exc:                              # noqa: BLE001
            # TransportError / FakeTransport 的 KeyError / 其它：一律转成 VerifyError，
            # 否则它会穿出 ingest()（只接 VerifyError），这条回调也就不落库了。
            # 只带类型名：别家异常的原文里若恰好有分档关键词，会被分错档。
            raise VerifyError(f"取 eBay 公钥失败（{type(exc).__name__}），kid={kid!r}") from exc
        if response.status != 200:
            raise VerifyError(f"取 eBay 公钥失败：getPublicKey 回 HTTP {response.status}，kid={kid!r}")
        try:
            body = json.loads(response.body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise VerifyError("getPublicKey 响应不是合法 JSON") from exc
        key_text = body.get("key") if isinstance(body, dict) else None
        if not isinstance(key_text, str) or not key_text.strip():
            raise VerifyError("getPublicKey 响应里没有 key")
        for field, expected in (("algorithm", "ECDSA"), ("digest", "SHA1")):
            got = body.get(field)
            if isinstance(got, str) and not equal(got.strip().upper(), expected):
                raise VerifyError(f"getPublicKey 回的 {field}={got!r}，只认 {expected}")
        der = _public_key_der(key_text)
        cache[(host, kid)] = (now, der)
        return der

    def verify_ebay(*, raw: bytes, headers: dict[str, str]) -> VerifiedCallback:
        envelope = _parse_signature_header(header(headers, SIGNATURE_HEADER))
        try:
            ecdsa_verify = backend()
        except ImportError as exc:
            raise VerifyError(
                "缺依赖 cryptography：eBay 通知验签需要 ECDSA（SHA1），"
                "装 python3 -m pip install 'maos[ingress]'。不回落成验签通过"
            ) from exc
        public_key = _public_key(envelope.kid)
        try:
            # 对**原始字节**验，不 loads 再 dumps（契约 §E 第 1 条）。
            matched = ecdsa_verify(public_key, envelope.signature, raw)
        except Exception as exc:                              # noqa: BLE001
            raise VerifyError(f"eBay 公钥无法用于验签（{type(exc).__name__}），"
                              f"kid={envelope.kid!r}") from exc
        if not matched:
            raise VerifyError(f"签名不匹配：{SIGNATURE_HEADER} 与原始请求体对不上")

        try:
            body = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise VerifyError("签名对得上，但通知体不是合法 JSON") from exc
        if not isinstance(body, dict):
            raise VerifyError("签名对得上，但通知体顶层不是 JSON 对象")
        # 信封：metadata.topic / notification.notificationId / notification.data。
        # 订单号：ORDER_CONFIRMATION 挂在 data.order.orderId，
        # ITEM_MARKED_SHIPPED 平铺在 data.orderId；两处都没有就是与订单无关的 topic。
        order_ref = (dig(body, "notification.data.order.orderId")
                     or dig(body, "notification.data.orderId"))
        return VerifiedCallback(
            event_id=_text(dig(body, "notification.notificationId")),
            topic=_text(dig(body, "metadata.topic")),
            order_ref=_text(order_ref),
        )

    return verify_ebay


#: 生产验签器。工厂在导入时只建一个空缓存和一个 urllib 出口，不读环境变量、不发请求。
verify_ebay = make_ebay_verifier(transport=UrllibTransport())

# 分支 A：Notification API 有携带订单事件的 topic（ORDER_CONFIRMATION、
# ITEM_MARKED_SHIPPED），所以在导入时登记。测试不靠这一次登记 —— 见测试文件的 fixture。
register_verifier("ebay", verify_ebay)
