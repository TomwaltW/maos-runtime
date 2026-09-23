"""Amazon SP-API 订单适配器的验收 —— `maos/tools/commerce/amazon_sp.py`。

B 档平台：现在拿不到企业审核后的凭据，所以这里**一条都不打真网**。
请求构造、LWA 令牌换取、字段映射全部用 `FakeTransport` + 官方文档的响应样例测
（响应体照 ordersV0.json 里 getOrder 的沙箱 200 样例改写：只换了订单号、状态、
金额与 `LastUpdateDate`）。凭据到位后只是换个 transport 实跑。

凭据一律 `monkeypatch.setenv` 造 `FAKE-<用途>-0000` 形状的假值（铁律 6）。

**FakeTransport 的坑**：没预置的请求它直接抛 `KeyError`。走 `query()` 的每一条用例
都要同时预置「令牌 POST」与「查单 GET」—— 否则还没到 GET 就 `KeyError` 了，
而一条漏了令牌预置的 404 用例也会因为 `KeyError` 而「通过」。所以 404 那条要
`match="里没有订单"`（基座 `query()` 的措辞），并断言 GET 确实发出去了。
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse

import pytest

from maos.tools.commerce import (
    CommerceError, CredentialMissing, FakeTransport, UnmappedOrderStatus,
)
from maos.tools.commerce.amazon_sp import LWA_TOKEN_URL, AmazonSpAdapter
from maos.tools.order import ALL_ORDER_STATUSES


_CLIENT_ID = "FAKE-client-id-0000"
_CLIENT_SECRET = "FAKE-client-secret-0000"
_REFRESH_TOKEN = "FAKE-refresh-token-0000"
_ACCESS_TOKEN = "FAKE-access-token-0000"

_ORDER_ID = "902-3159896-1390916"
_HOST_NA = "sellingpartnerapi-na.amazon.com"
_GET_URL = f"https://{_HOST_NA}/orders/v0/orders/{_ORDER_ID}"

#: 2026-09-15 14:30:00 UTC 的 epoch 毫秒（`derive_version("2026-09-15T14:30:00Z")` 实算）。
_VERSION = 1789482600000

_DROP = object()


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("AMAZON_SP_CLIENT_ID", _CLIENT_ID)
    monkeypatch.setenv("AMAZON_SP_CLIENT_SECRET", _CLIENT_SECRET)
    monkeypatch.setenv("AMAZON_SP_REFRESH_TOKEN", _REFRESH_TOKEN)
    monkeypatch.setenv("AMAZON_SP_REGION", "na")


def _order_body(**overrides) -> str:
    """getOrder 的 200 响应：`{"payload": {...}}`。传 `_DROP` 删掉某个字段。"""
    order = {
        "AmazonOrderId": _ORDER_ID,
        "PurchaseDate": "2026-09-14T09:12:00Z",
        "LastUpdateDate": "2026-09-15T14:30:00Z",
        "OrderStatus": "Shipped",
        "FulfillmentChannel": "MFN",
        "SalesChannel": "Amazon.com",
        "ShipServiceLevel": "Std US D2D Dom",
        "OrderTotal": {"CurrencyCode": "USD", "Amount": "128.50"},
        "NumberOfItemsShipped": 1,
        "NumberOfItemsUnshipped": 0,
        "PaymentMethod": "Other",
        "MarketplaceId": "ATVPDKIKX0DER",
        "ShipmentServiceLevelCategory": "Standard",
        "OrderType": "StandardOrder",
        "IsBusinessOrder": False,
        "IsPrime": False,
    }
    for key, value in overrides.items():
        if value is _DROP:
            order.pop(key, None)
        else:
            order[key] = value
    return json.dumps({"payload": order})


def _token_body(token: str = _ACCESS_TOKEN, expires_in: object = 3600) -> str:
    return json.dumps({"access_token": token, "token_type": "bearer",
                       "expires_in": expires_in, "refresh_token": _REFRESH_TOKEN})


def _errors_body(code: str, message: str) -> str:
    return json.dumps({"errors": [{"code": code, "message": message, "details": ""}]})


def _fake(order_body: str | None = None, *, get_status: int = 200,
          get_headers: dict | None = None, get_url: str = _GET_URL) -> FakeTransport:
    """同时预置令牌 POST 与查单 GET。"""
    return (FakeTransport()
            .expect("POST", LWA_TOKEN_URL, body=_token_body())
            .expect("GET", get_url, status=get_status, headers=get_headers,
                    body=order_body if order_body is not None else _order_body()))


def _calls(fake: FakeTransport, method: str) -> list[dict]:
    return [c for c in fake.calls if c["method"] == method]


# ---------------------------------------------------------------------------
# 契约 §G 点名的四条
# ---------------------------------------------------------------------------

def test_query_happy_path_five_fields(creds):
    fake = _fake(_order_body(OrderStatus="Shipped"))
    order = AmazonSpAdapter(transport=fake, account="A1SELLER").query(_ORDER_ID)

    assert order.order_id == "902-3159896-1390916"
    # 2026-09-15 14:30:00 UTC 的 epoch 毫秒
    assert order.version == 1789482600000
    assert order.status == "shipped"
    # 写死字符串：取成整个 OrderTotal dict 也不会抛，只会变成它的 str —— 只断言「没抛」抓不住
    assert order.amount == "128.50"
    assert order.updated_at == "2026-09-15T14:30:00Z"


def test_not_found_raises_keyerror(creds):
    fake = _fake(_errors_body("NotFound", "The resource specified does not exist."),
                 get_status=404)
    with pytest.raises(KeyError, match="里没有订单"):
        AmazonSpAdapter(transport=fake).query(_ORDER_ID)
    # 确实走到了查单那一步 —— 不是因为漏预置令牌而在 POST 上 KeyError
    assert [c["url"] for c in _calls(fake, "GET")] == [_GET_URL]


def test_unknown_status_raises_unmapped(creds):
    fake = _fake(_order_body(OrderStatus="SomeNewStatus"))
    with pytest.raises(UnmappedOrderStatus, match="SomeNewStatus"):
        AmazonSpAdapter(transport=fake).query(_ORDER_ID)


def test_secrets_not_in_repr_or_exception(creds):
    fake = _fake(_errors_body("Unauthorized", "Access to requested resource is denied."),
                 get_status=403)
    adapter = AmazonSpAdapter(transport=fake, account="A1SELLER")

    text = repr(adapter)
    assert "A1SELLER" in text                  # 账号标识不是密钥，排查要看
    with pytest.raises(CommerceError) as info:
        adapter.query(_ORDER_ID)
    err = str(info.value)
    assert info.value.status == 403
    for secret in (_CLIENT_ID, _CLIENT_SECRET, _REFRESH_TOKEN, _ACCESS_TOKEN):
        assert secret not in text
        assert secret not in err


# ---------------------------------------------------------------------------
# 状态映射
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("Pending", "UNMAPPED"),              # 付款未授权 —— 映射成 paid 就是被禁止的兜底
    ("Unshipped", "paid"),
    ("PartiallyShipped", "shipped"),
    ("Shipped", "shipped"),
    ("Canceled", "cancelled"),
    ("Unfulfillable", "UNMAPPED"),        # 多渠道配送专用态，四态里没有对应语义
    ("InvoiceUnconfirmed", "shipped"),
    ("PendingAvailability", "UNMAPPED"),  # 预售、付款未授权
])
def test_order_status_table(creds, raw, expected):
    fake = _fake(_order_body(OrderStatus=raw))
    adapter = AmazonSpAdapter(transport=fake)
    if expected == "UNMAPPED":
        with pytest.raises(UnmappedOrderStatus):
            adapter.query(_ORDER_ID)
    else:
        assert adapter.query(_ORDER_ID).status == expected


def test_canceled_rule_is_first_and_maps_to_cancelled(creds):
    # 顺序即优先级（契约 §C 第 3 条）：规则表第一条就是取消
    assert AmazonSpAdapter.status_rules[0].target == "cancelled"
    fake = _fake(_order_body(OrderStatus="Canceled"))
    assert AmazonSpAdapter(transport=fake).query(_ORDER_ID).status == "cancelled"


def test_status_rules_are_sourced_and_within_four_states():
    for rule in AmazonSpAdapter.status_rules:
        assert rule.target in ALL_ORDER_STATUSES
        assert "SP-API" in rule.source and rule.source.count(" / ") >= 2
        assert rule.values == tuple(v.lower() for v in rule.values)


# ---------------------------------------------------------------------------
# LWA 令牌
# ---------------------------------------------------------------------------

def test_lwa_token_request_shape(creds):
    fake = _fake()
    AmazonSpAdapter(transport=fake).query(_ORDER_ID)

    post, get = fake.calls
    assert post["method"] == "POST"
    assert post["url"] == "https://api.amazon.com/auth/o2/token"
    assert post["headers"]["Content-Type"] == "application/x-www-form-urlencoded;charset=UTF-8"
    form = urllib.parse.parse_qs(post["body"].decode("utf-8"))
    assert form == {
        "grant_type": ["refresh_token"],
        "refresh_token": [_REFRESH_TOKEN],
        "client_id": [_CLIENT_ID],
        "client_secret": [_CLIENT_SECRET],
    }

    assert get["method"] == "GET"
    assert get["url"] == _GET_URL
    assert get["headers"]["x-amz-access-token"] == _ACCESS_TOKEN
    assert get["headers"]["host"] == _HOST_NA
    assert re.fullmatch(r"\d{8}T\d{6}Z", get["headers"]["x-amz-date"])
    assert get["headers"]["user-agent"]


def test_lwa_token_reused_within_validity(creds):
    fake = _fake()
    adapter = AmazonSpAdapter(transport=fake, now=lambda: 0.0)
    adapter.query(_ORDER_ID)
    adapter.query(_ORDER_ID)
    assert len(_calls(fake, "POST")) == 1
    assert len(_calls(fake, "GET")) == 2


def test_lwa_token_refreshed_60s_before_expiry(creds):
    clock = [0.0]
    fake = _fake()
    adapter = AmazonSpAdapter(transport=fake, now=lambda: clock[0])

    adapter.query(_ORDER_ID)                    # t=0 换到令牌，expires_in=3600
    assert len(_calls(fake, "POST")) == 1

    clock[0] = 3539.0                           # 离过期还有 61 s：复用
    adapter.query(_ORDER_ID)
    assert len(_calls(fake, "POST")) == 1

    clock[0] = 3540.0                           # 离过期只剩 60 s：换新
    adapter.query(_ORDER_ID)
    assert len(_calls(fake, "POST")) == 2


def test_lwa_token_expiry_follows_expires_in(creds):
    """有效期取响应的 expires_in，不写死 3600。"""
    clock = [0.0]
    fake = _fake()
    fake.expect("POST", LWA_TOKEN_URL, body=_token_body(expires_in=600))
    adapter = AmazonSpAdapter(transport=fake, now=lambda: clock[0])
    adapter.query(_ORDER_ID)
    clock[0] = 539.0
    adapter.query(_ORDER_ID)
    assert len(_calls(fake, "POST")) == 1
    clock[0] = 540.0
    adapter.query(_ORDER_ID)
    assert len(_calls(fake, "POST")) == 2


def test_lwa_token_not_in_repr_exception_or_logs(creds, caplog):
    caplog.set_level(logging.DEBUG)
    fake = _fake()
    adapter = AmazonSpAdapter(transport=fake)
    adapter.query(_ORDER_ID)                    # 换到令牌，并记一条换新日志

    fake.expect("GET", _GET_URL, status=403,
                body=_errors_body("Unauthorized", "Access to requested resource is denied."))
    with pytest.raises(CommerceError) as info:
        adapter.query(_ORDER_ID)

    assert "LWA access token 已换新" in caplog.text   # 日志确实记了，只是不带令牌
    for secret in (_ACCESS_TOKEN, _REFRESH_TOKEN, _CLIENT_SECRET):
        assert secret not in repr(adapter)
        assert secret not in str(info.value)
        assert secret not in caplog.text


def test_lwa_failure_not_reported_as_order_failure(creds):
    fake = FakeTransport().expect(
        "POST", LWA_TOKEN_URL, status=400,
        body=json.dumps({"error": "invalid_grant",
                         "error_description": "The request has an invalid grant parameter : "
                                              f"refresh_token {_REFRESH_TOKEN}"}))
    with pytest.raises(CommerceError) as info:
        AmazonSpAdapter(transport=fake).query(_ORDER_ID)

    err = str(info.value)
    assert info.value.code == "invalid_grant"
    assert "LWA" in err
    assert _calls(fake, "GET") == []            # 令牌没换到，查单请求一次都不发
    for secret in (_REFRESH_TOKEN, _CLIENT_SECRET, _CLIENT_ID):
        assert secret not in err                # 平台回显的原值也要抹掉


def test_lwa_response_without_expires_in_raises(creds):
    fake = _fake()
    fake.expect("POST", LWA_TOKEN_URL,
                body=json.dumps({"access_token": _ACCESS_TOKEN, "token_type": "bearer"}))
    with pytest.raises(CommerceError, match="LWA") as info:
        AmazonSpAdapter(transport=fake).query(_ORDER_ID)
    assert info.value.code == "LWA_BAD_RESPONSE"
    assert _ACCESS_TOKEN not in str(info.value)
    assert _calls(fake, "GET") == []


def test_forbidden_drops_cached_token(creds):
    """403 之后丢掉缓存的令牌：被平台作废的令牌不该在缓存里挂满剩下的有效期。"""
    fake = _fake()
    adapter = AmazonSpAdapter(transport=fake, now=lambda: 0.0)
    adapter.query(_ORDER_ID)
    fake.expect("GET", _GET_URL, status=403,
                body=_errors_body("Unauthorized", "Access to requested resource is denied."))
    with pytest.raises(CommerceError):
        adapter.query(_ORDER_ID)
    fake.expect("GET", _GET_URL, body=_order_body())
    adapter.query(_ORDER_ID)
    assert len(_calls(fake, "POST")) == 2


# ---------------------------------------------------------------------------
# 金额、限流、区域、凭据
# ---------------------------------------------------------------------------

def test_missing_order_total_raises_not_zero(creds):
    # 必须用会命中规则的状态：UnmappedOrderStatus 也是 ValueError 的子类，
    # 用 Pending 会让这条因为错误的原因通过
    fake = _fake(_order_body(OrderStatus="Shipped", OrderTotal=_DROP))
    with pytest.raises(ValueError) as info:
        AmazonSpAdapter(transport=fake).query(_ORDER_ID)
    assert not isinstance(info.value, UnmappedOrderStatus)
    assert "金额" in str(info.value) or "OrderTotal" in str(info.value)


def test_rate_limited_429_explained(creds):
    fake = _fake(_errors_body("QuotaExceeded", "You exceeded your quota for the requested resource."),
                 get_status=429, get_headers={"x-amzn-RateLimit-Limit": "0.5"})
    with pytest.raises(CommerceError) as info:
        AmazonSpAdapter(transport=fake).query(_ORDER_ID)
    err = str(info.value)
    assert info.value.status == 429
    assert "限流" in err
    assert "x-amzn-RateLimit-Limit=0.5" in err
    assert len(_calls(fake, "GET")) == 1        # 适配器自己不重试


@pytest.mark.parametrize("region,host", [
    ("na", "sellingpartnerapi-na.amazon.com"),
    ("eu", "sellingpartnerapi-eu.amazon.com"),
    ("fe", "sellingpartnerapi-fe.amazon.com"),
])
def test_region_selects_endpoint(creds, monkeypatch, region, host):
    monkeypatch.setenv("AMAZON_SP_REGION", region)
    url = f"https://{host}/orders/v0/orders/{_ORDER_ID}"
    fake = _fake(get_url=url)
    assert AmazonSpAdapter(transport=fake).query(_ORDER_ID).order_id == _ORDER_ID
    assert _calls(fake, "GET")[0]["url"] == url


def test_unknown_region_raises_without_request(creds, monkeypatch):
    monkeypatch.setenv("AMAZON_SP_REGION", "us")
    fake = _fake()
    with pytest.raises(ValueError, match="AMAZON_SP_REGION"):
        AmazonSpAdapter(transport=fake).query(_ORDER_ID)
    assert fake.calls == []                     # 不缺省成 na，也不先去换令牌


@pytest.mark.parametrize("missing", [
    "AMAZON_SP_REGION", "AMAZON_SP_CLIENT_ID",
    "AMAZON_SP_CLIENT_SECRET", "AMAZON_SP_REFRESH_TOKEN",
])
def test_missing_credential_raises_before_any_request(creds, monkeypatch, missing):
    monkeypatch.delenv(missing, raising=False)
    fake = _fake()
    with pytest.raises(CredentialMissing, match=missing):
        AmazonSpAdapter(transport=fake).query(_ORDER_ID)
    assert fake.calls == []
