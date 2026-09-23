"""TikTok Shop 适配器的验收 —— `maos/tools/commerce/tiktok_shop.py`。

**golden 值一律在被测模块之外算好、以字面量贴进来**（含 FakeTransport 预置 URL 里的
`sign=`）。用模块自己的签名函数现算期望值，拼串顺序错了照样是绿的 —— 那是恒真判据。
官方文档的两个签名样例也原样钉在这里：它们是「我们对文档的理解没走样」的外部证据。

webhook 的三条用例在 fixture 里**自己重新登记** verifier，不依赖导入时那次登记：
`test_shop_callback.py` 的 autouse fixture 每条用例前后都会 `reset_verifiers()`。
"""

from __future__ import annotations

import json

import pytest

from maos.core.store import SqliteStore
from maos.ingress import shop_callback as sc
from maos.tools.commerce import (
    CommerceError, CredentialMissing, FakeTransport, UnmappedOrderStatus, map_status,
)
from maos.tools.commerce import tiktok_shop as tt
from maos.tools.order import ExternalOrder


APP_KEY = "tt-key-FAKE"
APP_SECRET = "tt-secret-FAKE"
ACCESS_TOKEN = "tt-token-FAKE"
SHOP_CIPHER = "ROW_FAKEcipher"
ORDER_ID = "576461413038785752"
NOW = 1789482600                    # 2026-09-15T14:30:00Z，与 test_commerce_base 同一时刻

# 模块外算好的请求签名：
#   hex(HMAC-SHA256(b"tt-secret-FAKE", b"tt-secret-FAKE" + b"/order/202309/orders"
#       + b"app_keytt-key-FAKEids576461413038785752shop_cipherROW_FAKEciphertimestamp1789482600"
#       + b"tt-secret-FAKE"))
REQUEST_SIGN = "4e10eb35ef54d05749d8e8acab61b053b69d07bc684029bfcb9e886d90fde44b"

URL = ("https://open-api.tiktokglobalshop.com/order/202309/orders"
       "?app_key=tt-key-FAKE&ids=576461413038785752&shop_cipher=ROW_FAKEcipher"
       "&timestamp=1789482600&sign=4e10eb35ef54d05749d8e8acab61b053b69d07bc684029bfcb9e886d90fde44b")

# webhook：两个不同的 body，各自在模块外算好签名（hex(HMAC(app_secret, app_key + 原始体))）。
BODY_A = (b'{"type":1,"tts_notification_id":"7327112393057371910","shop_id":"7494049642642441621",'
          b'"timestamp":1789482600,"data":{"order_id":"576461413038785752","order_status":"IN_TRANSIT",'
          b'"is_on_hold_order":false,"update_time":1789482600}}')
SIG_A = "590f840a05908b7b415bd9ba32f86a806c3674750d28b4a2af6bdac6120a2fa0"

# 同一条通知去掉 tts_notification_id —— 签名对、但没有投递 id。
BODY_B = (b'{"type":1,"shop_id":"7494049642642441621",'
          b'"timestamp":1789482600,"data":{"order_id":"576461413038785752","order_status":"IN_TRANSIT",'
          b'"is_on_hold_order":false,"update_time":1789482600}}')
SIG_B = "24f3b1a94cb3c2465e22678a2dbf549069f387c7228766719f97ec2433f15a6a"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("TIKTOK_SHOP_APP_KEY", APP_KEY)
    monkeypatch.setenv("TIKTOK_SHOP_APP_SECRET", APP_SECRET)
    monkeypatch.setenv("TIKTOK_SHOP_ACCESS_TOKEN", ACCESS_TOKEN)
    sc._schema_ready.clear()
    sc.register_verifier("tiktok_shop", tt.verify_webhook)
    yield
    # 不 reset_verifiers()：那会把别的平台的登记一起清掉。


def _adapter(fake: FakeTransport) -> tt.TikTokShopAdapter:
    return tt.TikTokShopAdapter(transport=fake, account=SHOP_CIPHER, clock=lambda: NOW)


def _ok_body(status: str = "IN_TRANSIT") -> str:
    """Get Order Detail (202309) 响应的形状：code / message / request_id / data.orders[]。"""
    return json.dumps({
        "code": 0, "message": "Success", "request_id": "20260915143000FAKE",
        "data": {"orders": [{
            "id": ORDER_ID, "status": status,
            "create_time": 1789400000, "update_time": NOW,
            "payment": {"currency": "USD", "total_amount": "128.50"},
        }]},
    })


_BUSINESS_ERROR = json.dumps({
    "code": 106001, "message": "Invalid credentials. The sign query parameter is invalid.",
    "request_id": "20260915143000FAKE", "data": None,
})


def _error_texts() -> list[str]:
    """404 / 4xx / 200 业务错误三条错误路径各跑一次，收集异常文本。"""
    texts = []
    cases = [
        (404, json.dumps({"code": 36009009, "message": "Invalid path"}), KeyError),
        (401, json.dumps({"code": 105002, "message": "Expired credentials."}), CommerceError),
        (200, _BUSINESS_ERROR, CommerceError),
    ]
    for status, body, exc_type in cases:
        fake = FakeTransport().expect("GET", URL, status=status, body=body)
        with pytest.raises(exc_type) as info:
            _adapter(fake).query(ORDER_ID)
        texts.append(str(info.value))
    return texts


def _store():
    s = SqliteStore()
    s.init_schema()
    return s


def _rows(store) -> list[dict]:
    cur = store._conn.execute(
        "SELECT platform, event_id, topic, verify_result FROM shop_callback ORDER BY id")
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# 查单
# ---------------------------------------------------------------------------

def test_tiktok_query_returns_external_order_five_fields():
    fake = FakeTransport().expect("GET", URL, body=_ok_body())
    order = _adapter(fake).query(ORDER_ID)

    assert isinstance(order, ExternalOrder)
    assert order.order_id == "576461413038785752"
    assert order.version == 1789482600000
    assert order.status == "shipped"
    assert order.amount == "128.50"
    assert order.updated_at == "1789482600"

    call = fake.calls[0]
    assert call["headers"]["x-tts-access-token"] == ACCESS_TOKEN     # 202309 起走请求头
    assert call["headers"]["content-type"] == "application/json"
    assert call["body"] is None


def test_tiktok_http_404_raises_key_error():
    fake = FakeTransport().expect("GET", URL, status=404,
                                  body=json.dumps({"code": 36009009, "message": "Invalid path"}))
    with pytest.raises(KeyError, match="里没有订单"):
        _adapter(fake).query(ORDER_ID)


def test_tiktok_empty_order_list_raises_key_error():
    body = json.dumps({"code": 0, "message": "Success", "data": {"orders": []}})
    fake = FakeTransport().expect("GET", URL, body=body)
    with pytest.raises(KeyError, match="里没有订单"):
        _adapter(fake).query(ORDER_ID)


def test_tiktok_business_error_raises_commerce_error():
    fake = FakeTransport().expect("GET", URL, body=_BUSINESS_ERROR)
    with pytest.raises(CommerceError) as info:
        _adapter(fake).query(ORDER_ID)
    assert info.value.code == "106001"
    assert info.value.status == 200
    assert info.value.platform == "tiktok_shop"


def test_tiktok_unknown_status_raises_unmapped():
    fake = FakeTransport().expect("GET", URL, body=_ok_body(status="RISK_REVIEW_FUTURE"))
    with pytest.raises(UnmappedOrderStatus):
        _adapter(fake).query(ORDER_ID)


def test_tiktok_secrets_absent_from_repr_and_errors():
    text = repr(_adapter(FakeTransport()))
    assert APP_SECRET not in text and ACCESS_TOKEN not in text
    assert SHOP_CIPHER in text                      # 店铺标识不是密钥，排查要看
    for message in _error_texts():
        assert APP_SECRET not in message
        assert ACCESS_TOKEN not in message


# ---------------------------------------------------------------------------
# 签名
# ---------------------------------------------------------------------------

def test_tiktok_sign_base_string_literal():
    params = {"timestamp": "1789482600", "shop_cipher": SHOP_CIPHER, "ids": ORDER_ID,
              "app_key": APP_KEY, "sign": "stale", "access_token": "must-not-be-signed"}
    assert tt.sign_base_string("/order/202309/orders", params, APP_SECRET) == (
        b"tt-secret-FAKE/order/202309/orders"
        b"app_keytt-key-FAKEids576461413038785752shop_cipherROW_FAKEciphertimestamp1789482600"
        b"tt-secret-FAKE")


def test_tiktok_sign_golden_vector():
    # 官方样例：Sign your API request（app_secret e59af819cc，路径 /authorization/202309/shops）
    # https://partner.tiktokshop.com/docv2/page/sign-your-api-request
    doc_base = tt.sign_base_string("/authorization/202309/shops",
                                   {"app_key": "29a39d", "timestamp": "1623812664"}, "e59af819cc")
    assert doc_base == b"e59af819cc/authorization/202309/shopsapp_key29a39dtimestamp1623812664e59af819cc"
    assert tt.sign(doc_base, "e59af819cc") == (
        "b596b73e0cc6de07ac26f036364178ab16b0a907af13d43f0a0cd2345f582dc8")

    params = {"app_key": APP_KEY, "ids": ORDER_ID, "shop_cipher": SHOP_CIPHER,
              "timestamp": "1789482600"}
    assert tt.sign(tt.sign_base_string("/order/202309/orders", params, APP_SECRET),
                   APP_SECRET) == REQUEST_SIGN


def test_tiktok_signature_absent_from_error_text():
    for message in _error_texts():
        assert REQUEST_SIGN not in message
        assert "sign=" not in message


def test_tiktok_sign_appends_non_multipart_body():
    """第 4 步：content-type 不是 multipart/form-data 时，请求体原始字节接在参数串后面。"""
    base = tt.sign_base_string("/p", {"b": "2", "a": "1"}, "k", body=b'{"x":1}')
    assert base == b'k/pa1b2{"x":1}k'


# ---------------------------------------------------------------------------
# 状态归一表
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,target", [
    ("CANCELLED", "cancelled"),
    ("IN_TRANSIT", "shipped"),
    ("DELIVERED", "shipped"),
    ("COMPLETED", "shipped"),
    ("AWAITING_SHIPMENT", "paid"),
    ("AWAITING_COLLECTION", "paid"),
])
def test_tiktok_status_rules_table(raw, target):
    rules = tt.TikTokShopAdapter.status_rules
    assert map_status(rules, {"status": raw}, platform="tiktok_shop") == target


@pytest.mark.parametrize("raw", ["UNPAID", "ON_HOLD", "PARTIALLY_SHIPPING"])
def test_tiktok_statuses_left_out_by_design(raw):
    """这三个是有意不进表的（理由见模块里 `_RULES` 的注释）—— 它们必须抛，不许兜底。"""
    with pytest.raises(UnmappedOrderStatus):
        map_status(tt.TikTokShopAdapter.status_rules, {"status": raw}, platform="tiktok_shop")


# ---------------------------------------------------------------------------
# 配置缺失
# ---------------------------------------------------------------------------

def test_tiktok_missing_shop_cipher_is_rejected_before_request():
    fake = FakeTransport()
    adapter = tt.TikTokShopAdapter(transport=fake, clock=lambda: NOW)
    with pytest.raises(ValueError, match="shop_cipher"):
        adapter.query(ORDER_ID)
    assert fake.calls == []


def test_tiktok_missing_credential_raises(monkeypatch):
    monkeypatch.delenv("TIKTOK_SHOP_ACCESS_TOKEN", raising=False)
    with pytest.raises(CredentialMissing, match="TIKTOK_SHOP_ACCESS_TOKEN"):
        _adapter(FakeTransport()).query(ORDER_ID)


# ---------------------------------------------------------------------------
# webhook
# ---------------------------------------------------------------------------

def test_tiktok_webhook_doc_sample_signature():
    """官方样例：Webhooks Overview（App key abcdef、App secret 123）。
    https://partner.tiktokshop.com/docv2/page/tts-webhooks-overview"""
    raw = (b'{"type":1,"tts_notification_id":"7380066284010030890","shop_id":"7495540735365777507",'
           b'"timestamp":1718305585,"data":{"is_on_hold_order":true,"order_id":"576653688135258178",'
           b'"order_status":"UNPAID","update_time":1718305585}}')
    assert tt.sign(tt.webhook_base_string("abcdef", raw), "123") == (
        "5dec0f11ec2f6783b8deee53c9ffbf8d024302f7c7e7fa55a35d17629031ac05")


def test_tiktok_webhook_duplicate_resyncs_once():
    store = _store()
    headers = {"Authorization": SIG_A, "Content-Type": "application/json"}
    first = sc.ingest("tiktok_shop", BODY_A, headers, store=store)
    second = sc.ingest("tiktok_shop", BODY_A, headers, store=store)

    assert first.accepted is True and first.http_status == 200
    assert first.verify_result == sc.VERIFY_OK
    assert first.event_id == "7327112393057371910"
    assert first.topic == "1"
    assert first.order_ref == ORDER_ID
    assert first.duplicate is False and first.should_resync is True
    assert second.duplicate is True and second.should_resync is False
    assert second.accepted is True and second.http_status == 200
    assert len(_rows(store)) == 2


def test_tiktok_webhook_bad_signature_recorded_400():
    store = _store()
    verdict = sc.ingest("tiktok_shop", BODY_A, {"Authorization": SIG_B}, store=store)

    assert verdict.accepted is False
    assert verdict.http_status == 400
    assert verdict.verify_result == sc.VERIFY_BAD_SIGNATURE
    assert verdict.should_resync is False
    rows = _rows(store)
    assert len(rows) == 1
    assert rows[0]["verify_result"] == sc.VERIFY_BAD_SIGNATURE


def test_tiktok_webhook_missing_event_id_400():
    store = _store()
    verdict = sc.ingest("tiktok_shop", BODY_B, {"Authorization": SIG_B}, store=store)

    assert verdict.accepted is False
    assert verdict.http_status == 400
    assert verdict.verify_result == sc.VERIFY_NO_EVENT_ID
    assert _rows(store)[0]["verify_result"] == sc.VERIFY_NO_EVENT_ID


def test_tiktok_webhook_missing_signature_header_is_bad_signature():
    store = _store()
    verdict = sc.ingest("tiktok_shop", BODY_A, {"Content-Type": "application/json"}, store=store)
    assert verdict.http_status == 400
    assert verdict.verify_result == sc.VERIFY_BAD_SIGNATURE
    assert len(_rows(store)) == 1
