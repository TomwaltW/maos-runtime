"""Shopee 适配器的验收 —— `maos/tools/commerce/shopee.py`。

**golden 值一律在被测模块之外算好、以字面量贴进来**（含 FakeTransport 预置 URL 里的
`sign=`），理由同 `test_commerce_tiktok.py` 模块头。

本文件没有 webhook 用例：Shopee Push 的签名规则本轮核不到出处，verifier 没有实现
（见 `shopee.py` 模块头与 `docs/DECISIONS.md` 的 `## task-t165`）。
"""

from __future__ import annotations

import json

import pytest

from maos.tools.commerce import (
    CommerceError, CredentialMissing, FakeTransport, UnmappedOrderStatus, map_status,
)
from maos.tools.commerce import shopee as sp
from maos.tools.order import ExternalOrder


PARTNER_ID = "2001887"
PARTNER_KEY = "sp-partner-key-FAKE"
ACCESS_TOKEN = "sp-token-FAKE"
SHOP_ID = "600001"
ORDER_SN = "260915FAKESN01"
NOW = 1789482600                    # 2026-09-15T14:30:00Z，与 test_commerce_base 同一时刻

# 模块外算好的请求签名：
#   hex(HMAC-SHA256(b"sp-partner-key-FAKE",
#       b"2001887/api/v2/order/get_order_detail1789482600sp-token-FAKE600001"))
REQUEST_SIGN = "8ed37bb5dc8a9d571e84a8265e25269f96a4596bad71d9938da6fa3489c4f40e"

URL = ("https://partner.shopeemobile.com/api/v2/order/get_order_detail"
       "?partner_id=2001887&timestamp=1789482600&access_token=sp-token-FAKE&shop_id=600001"
       "&sign=8ed37bb5dc8a9d571e84a8265e25269f96a4596bad71d9938da6fa3489c4f40e"
       "&order_sn_list=260915FAKESN01&response_optional_fields=total_amount")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("SHOPEE_PARTNER_ID", PARTNER_ID)
    monkeypatch.setenv("SHOPEE_PARTNER_KEY", PARTNER_KEY)
    monkeypatch.setenv("SHOPEE_ACCESS_TOKEN", ACCESS_TOKEN)
    monkeypatch.setenv("SHOPEE_SHOP_ID", SHOP_ID)


def _adapter(fake: FakeTransport) -> sp.ShopeeAdapter:
    return sp.ShopeeAdapter(transport=fake, account="shop-600001", clock=lambda: NOW)


def _ok_body(status: str = "SHIPPED") -> str:
    """get_order_detail 响应的形状：error / message / request_id / response.order_list[]。"""
    return json.dumps({
        "error": "", "message": "", "request_id": "fake0000000000000000000000000001",
        "response": {"order_list": [{
            "order_sn": ORDER_SN, "region": "SG", "currency": "SGD",
            "order_status": status, "total_amount": 128.5, "update_time": NOW,
        }]},
    })


_BUSINESS_ERROR = json.dumps({
    "error": "error_sign", "message": "Wrong sign.",
    "request_id": "fake0000000000000000000000000002", "response": None,
})


def _error_texts() -> list[str]:
    """404 / 4xx / 200 业务错误三条错误路径各跑一次，收集异常文本。"""
    texts = []
    cases = [
        (404, json.dumps({"error": "", "message": "not found"}), KeyError),
        (403, json.dumps({"error": "error_auth", "message": "Invalid access_token."}), CommerceError),
        (200, _BUSINESS_ERROR, CommerceError),
    ]
    for status, body, exc_type in cases:
        fake = FakeTransport().expect("GET", URL, status=status, body=body)
        with pytest.raises(exc_type) as info:
            _adapter(fake).query(ORDER_SN)
        texts.append(str(info.value))
    return texts


# ---------------------------------------------------------------------------
# 查单
# ---------------------------------------------------------------------------

def test_shopee_query_returns_external_order_five_fields():
    fake = FakeTransport().expect("GET", URL, body=_ok_body())
    order = _adapter(fake).query(ORDER_SN)

    assert isinstance(order, ExternalOrder)
    assert order.order_id == "260915FAKESN01"
    assert order.version == 1789482600000
    assert order.status == "shipped"
    assert order.amount == "128.5"                  # float 原样交，基座用 repr 保精度
    assert order.updated_at == "1789482600"
    assert fake.calls[0]["body"] is None


def test_shopee_http_404_raises_key_error():
    fake = FakeTransport().expect("GET", URL, status=404,
                                  body=json.dumps({"error": "", "message": "not found"}))
    with pytest.raises(KeyError, match="里没有订单"):
        _adapter(fake).query(ORDER_SN)


def test_shopee_empty_order_list_raises_key_error():
    body = json.dumps({"error": "", "message": "", "response": {"order_list": []}})
    fake = FakeTransport().expect("GET", URL, body=body)
    with pytest.raises(KeyError, match="里没有订单"):
        _adapter(fake).query(ORDER_SN)


def test_shopee_business_error_raises_commerce_error():
    fake = FakeTransport().expect("GET", URL, body=_BUSINESS_ERROR)
    with pytest.raises(CommerceError) as info:
        _adapter(fake).query(ORDER_SN)
    assert info.value.code == "error_sign"
    assert info.value.status == 200
    assert info.value.platform == "shopee"


def test_shopee_unknown_status_raises_unmapped():
    fake = FakeTransport().expect("GET", URL, body=_ok_body(status="RISK_REVIEW_FUTURE"))
    with pytest.raises(UnmappedOrderStatus):
        _adapter(fake).query(ORDER_SN)


def test_shopee_secrets_absent_from_repr_and_errors():
    text = repr(_adapter(FakeTransport()))
    assert PARTNER_KEY not in text and ACCESS_TOKEN not in text
    assert "shop-600001" in text                    # 账号标识不是密钥，排查要看
    for message in _error_texts():
        assert PARTNER_KEY not in message
        assert ACCESS_TOKEN not in message


def test_shopee_error_not_found_raises_key_error():
    """get_order_detail 的 error_list 里有 error_not_found —— 它就是「查无此单」。"""
    body = json.dumps({"error": "error_not_found",
                       "message": "Wrong parameters, detail: the order is not found."})
    fake = FakeTransport().expect("GET", URL, body=body)
    with pytest.raises(KeyError, match="里没有订单"):
        _adapter(fake).query(ORDER_SN)


def test_shopee_error_param_is_not_read_as_not_found():
    """error_param 可能是参数拼错也可能是查不到 —— 分不清就按错误抛，不说成「订单不存在」。"""
    body = json.dumps({"error": "error_param", "message": "Wrong parameters, detail: data not exist"})
    fake = FakeTransport().expect("GET", URL, body=body)
    with pytest.raises(CommerceError) as info:
        _adapter(fake).query(ORDER_SN)
    assert info.value.code == "error_param"


# ---------------------------------------------------------------------------
# 签名
# ---------------------------------------------------------------------------

def test_shopee_sign_base_string_literal():
    assert sp.sign_base_string(PARTNER_ID, "/api/v2/order/get_order_detail", "1789482600",
                               ACCESS_TOKEN, SHOP_ID) == (
        "2001887/api/v2/order/get_order_detail1789482600sp-token-FAKE600001")


def test_shopee_sign_golden_vector():
    base = sp.sign_base_string(PARTNER_ID, "/api/v2/order/get_order_detail", "1789482600",
                               ACCESS_TOKEN, SHOP_ID)
    assert sp.sign(base, PARTNER_KEY) == REQUEST_SIGN


def test_shopee_signature_absent_from_error_text():
    for message in _error_texts():
        assert REQUEST_SIGN not in message
        assert "sign=" not in message


# ---------------------------------------------------------------------------
# 状态归一表
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,target", [
    ("CANCELLED", "cancelled"),
    ("SHIPPED", "shipped"),
    ("COMPLETED", "shipped"),
])
def test_shopee_status_rules_table(raw, target):
    rules = sp.ShopeeAdapter.status_rules
    assert map_status(rules, {"order_status": raw}, platform="shopee") == target


@pytest.mark.parametrize("raw", [
    "UNPAID", "READY_TO_SHIP", "PROCESSED", "IN_CANCEL", "INVOICE_PENDING", "PENDING",
])
def test_shopee_statuses_left_out_by_design(raw):
    """这些是有意不进表的（理由见模块里 `_RULES` 的注释）—— 它们必须抛，不许兜底。"""
    with pytest.raises(UnmappedOrderStatus):
        map_status(sp.ShopeeAdapter.status_rules, {"order_status": raw}, platform="shopee")


# ---------------------------------------------------------------------------
# 配置缺失
# ---------------------------------------------------------------------------

def test_shopee_missing_credential_raises(monkeypatch):
    monkeypatch.delenv("SHOPEE_PARTNER_KEY", raising=False)
    fake = FakeTransport()
    with pytest.raises(CredentialMissing, match="SHOPEE_PARTNER_KEY"):
        _adapter(fake).query(ORDER_SN)
    assert fake.calls == []
