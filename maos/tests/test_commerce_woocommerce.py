"""WooCommerce 适配器与 webhook 验签的验收 —— `maos/tools/commerce/woocommerce.py`。

形状照两份样板：查单照 `test_commerce_base.py`，webhook 照 `test_shop_callback.py`。
与样板不同的一点：本文件**不定义任何适配器子类、也不重新导入模块** —— 那会造出第二个
``platform="woocommerce"`` 的子类，T166 的「platform 唯一」检查在整合时会红。
要换行为一律用 `monkeypatch`。

测试一律 `FakeTransport`，不打真网；凭据一律是一眼看得出的假值，用 `monkeypatch.setenv` 造。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest

from maos.core.store import SqliteStore
from maos.ingress import shop_callback as sc
from maos.tools.commerce import (
    CommerceError, CredentialMissing, FakeTransport, UnmappedOrderStatus,
)
from maos.tools.commerce import woocommerce as woo


SITE = "https://shop.example.test"
URL = "https://shop.example.test/wp-json/wc/v3/orders/727"
KEY = "ck_fake_test"
SECRET = "cs_fake_test_secret"
# base64("ck_fake_test:cs_fake_test_secret")，2026-09-23 用标准库独立算出：
#   python3 -c "import base64; print(base64.b64encode(b'ck_fake_test:cs_fake_test_secret').decode())"
BASIC = "Basic Y2tfZmFrZV90ZXN0OmNzX2Zha2VfdGVzdF9zZWNyZXQ="

# 每个值都是某条反向验证要咬住的东西，不许改（status 除外，见 paste-T162 §1.6）。
_ORDER = {
    "id": 727,
    "status": "processing",
    "total": "128.50",                      # 末尾的 0 是故意的：float() 会把它变成 "128.5"
    "date_modified": "2026-09-15T22:30:00",  # UTC+8 站点的本地时间 —— 适配器不许用它
    "date_modified_gmt": "2026-09-15T14:30:00",
}

# 2026-09-15T14:30:00Z 的 epoch 毫秒，与 test_commerce_base.py 钉的是同一个时刻。
VERSION_GMT = 1789482600000
# 错用 date_modified（站点本地时间）补 Z 的结果，差 8 小时。
VERSION_IF_LOCAL = 1789511400000

WEBHOOK_SECRET = "woo-webhook-test-secret"
BODY = b'{"id":727,  "status":"processing"}'   # 紧凑 + 两个空格：json.dumps(json.loads(BODY)) 一定和它不一样
SIGNATURE = "RPrYqg8cgRFAGMaI23qYjqJPkXxEN/029zHMLVx41qA="
# 独立复算命令（期望值不调被测模块的任何函数去算）：
#   python3 -c "import hmac,hashlib,base64; print(base64.b64encode(hmac.new(b'woo-webhook-test-secret', b'{\"id\":727,  \"status\":\"processing\"}', hashlib.sha256).digest()).decode())"

# Woo 建 webhook 时发的 ping 就是这个 body（WC_Webhook::deliver_ping：'webhook_id=' . id）。
NON_JSON_BODY = b"webhook_id=15"
# 同一把密钥对这 13 个字节的正确签名，2026-09-23 独立算出。
NON_JSON_SIGNATURE = "ntf/lBwyVzpj5HRQ/lS4ikeyOZgECjvDrjkjgdAg9o8="


@pytest.fixture(autouse=True)
def _woo_verifier(monkeypatch):
    # 登记表是模块级的，test_shop_callback.py 的 autouse fixture 每条用例前后都清空它；
    # 本模块只在第一次 import 时登记一次。所以这里必须自己重新登记，不能靠导入顺序。
    # _schema_ready 按 id(store) 缓存，旧 store 回收后 id 可能被新 store 复用 → 新库没建表。
    sc.reset_verifiers()
    sc._schema_ready.clear()
    sc.register_verifier("woocommerce", woo.verify_woocommerce)
    monkeypatch.setenv("WOO_WEBHOOK_SECRET", WEBHOOK_SECRET)
    yield
    sc.reset_verifiers()


def _adapter(transport: FakeTransport, monkeypatch) -> woo.WooCommerceAdapter:
    monkeypatch.setenv("WOO_SITE_URL", SITE)
    monkeypatch.setenv("WOO_CONSUMER_KEY", KEY)
    monkeypatch.setenv("WOO_CONSUMER_SECRET", SECRET)
    return woo.WooCommerceAdapter(transport=transport, account="shop.example.test")


def _serving(order: dict) -> FakeTransport:
    return FakeTransport().expect("GET", URL, body=json.dumps(order))


def _error_body(code: str, message: str, status: int) -> str:
    # 形状照 WooCommerce REST API v3 文档 / Introduction / Errors 的示例。
    return json.dumps({"code": code, "message": message, "data": {"status": status}})


def _store():
    s = SqliteStore()
    s.init_schema()
    return s


def _rows(store) -> list[dict]:
    cur = store._conn.execute(
        "SELECT platform, event_id, topic, verify_result, note, raw_body_b64,"
        " headers_json FROM shop_callback ORDER BY id")
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _hooks(*, delivery_id: str | None = "d-001", webhook_id: str = "15",
           topic: str = "order.updated", signature: str = SIGNATURE) -> dict:
    h = {
        "X-WC-Webhook-Source": "https://shop.example.test/",
        "X-WC-Webhook-Topic": topic,
        "X-WC-Webhook-Resource": topic.split(".")[0],
        "X-WC-Webhook-Event": topic.split(".")[-1],
        "X-WC-Webhook-Signature": signature,
        "X-WC-Webhook-ID": webhook_id,
        "Content-Type": "application/json",
    }
    if delivery_id is not None:
        h["X-WC-Webhook-Delivery-ID"] = delivery_id
    return h


def _sign(body: bytes) -> str:
    """参照签名：标准库 hmac + base64，不经过被测模块。"""
    digest = hmac.new(WEBHOOK_SECRET.encode("utf-8"), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


# ---------------------------------------------------------------------------
# 查单：五字段、404、未知状态、密钥、naive 时间、认证头
# ---------------------------------------------------------------------------

def test_query_maps_all_five_fields_with_exact_version(monkeypatch):
    order = _adapter(_serving(_ORDER), monkeypatch).query("727")
    assert order.order_id == "727"
    assert order.version == 1789482600000
    assert order.status == "paid"
    assert order.amount == "128.50"
    assert order.updated_at == "2026-09-15T14:30:00Z"


def test_query_404_raises_keyerror(monkeypatch):
    """查不到抛 KeyError，不返回 v1 空订单。

    match 用基座 404 分支的措辞：FakeTransport 没预置的 URL 也抛 KeyError，
    不按措辞区分的话，URL 拼错了这条也会是绿的。
    """
    fake = FakeTransport().expect(
        "GET", URL, status=404,
        body=_error_body("woocommerce_rest_shop_order_invalid_id", "Invalid ID.", 404))
    with pytest.raises(KeyError, match="没有订单"):
        _adapter(fake, monkeypatch).query("727")


def test_unknown_status_raises_unmapped_order_status(monkeypatch):
    """表里没有的状态（例如插件加的自定义状态）→ 抛，不兜底成 paid。"""
    fake = _serving(dict(_ORDER, status="awaiting-shipment"))
    with pytest.raises(UnmappedOrderStatus, match="woocommerce"):
        _adapter(fake, monkeypatch).query("727")


def test_secrets_absent_from_repr_and_exception_text(monkeypatch):
    fake = FakeTransport()
    adapter = _adapter(fake, monkeypatch)
    text = repr(adapter)
    assert KEY not in text and SECRET not in text
    assert "shop.example.test" in text          # 店铺标识不是密钥，排查要看

    fake.expect("GET", URL, status=404,
                body=_error_body("woocommerce_rest_shop_order_invalid_id", "Invalid ID.", 404))
    with pytest.raises(KeyError) as missing:
        adapter.query("727")
    assert KEY not in str(missing.value) and SECRET not in str(missing.value)

    fake.expect("GET", URL, status=401,
                body=_error_body("woocommerce_rest_cannot_view",
                                 "Sorry, you cannot view this resource.", 401))
    with pytest.raises(CommerceError) as denied:
        adapter.query("727")
    assert KEY not in str(denied.value) and SECRET not in str(denied.value)


def test_naive_gmt_timestamp_is_pinned_to_utc(monkeypatch):
    """date_modified_gmt 不带时区，按文档语义补 Z；不许错用站点本地时间的 date_modified。"""
    assert _ORDER["date_modified"] != _ORDER["date_modified_gmt"]
    order = _adapter(_serving(_ORDER), monkeypatch).query("727")
    assert order.version == VERSION_GMT
    assert order.version != VERSION_IF_LOCAL


def test_query_uses_basic_auth_header_not_query_string(monkeypatch):
    """凭据走 Authorization 头，不走 Woo 文档那条 query 参数退路（进 URL 就会进日志）。"""
    fake = _serving(_ORDER)
    _adapter(fake, monkeypatch).query("727")
    call = fake.calls[0]
    assert call["method"] == "GET"
    assert call["url"] == URL
    assert "?" not in call["url"] and "consumer_key" not in call["url"]
    assert KEY not in call["url"] and SECRET not in call["url"]
    assert call["headers"]["Authorization"] == BASIC


# ---------------------------------------------------------------------------
# 查单：规则表逐条钉住（映射的与故意不映射的）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("woo_status,expected", [
    ("processing", "paid"),
    ("completed", "shipped"),
    ("cancelled", "cancelled"),
])
def test_documented_statuses_map_to_their_target(monkeypatch, woo_status, expected):
    order = _adapter(_serving(dict(_ORDER, status=woo_status)), monkeypatch).query("727")
    assert order.status == expected


@pytest.mark.parametrize("woo_status", ["pending", "on-hold", "failed", "refunded", "trash"])
def test_statuses_without_a_documented_target_raise(monkeypatch, woo_status):
    """这五个故意不进表（理由见 docs/DECISIONS.md 的 task-t162 小节）。
    哪天有人给其中一个补了规则，这条会红，逼着他同时去改账。"""
    with pytest.raises(UnmappedOrderStatus):
        _adapter(_serving(dict(_ORDER, status=woo_status)), monkeypatch).query("727")


# ---------------------------------------------------------------------------
# 查单：错误与凭据
# ---------------------------------------------------------------------------

def test_401_raises_commerce_error_with_platform_code(monkeypatch):
    fake = FakeTransport().expect(
        "GET", URL, status=401,
        body=_error_body("woocommerce_rest_cannot_view",
                         "Sorry, you cannot view this resource.", 401))
    with pytest.raises(CommerceError) as denied:
        _adapter(fake, monkeypatch).query("727")
    assert denied.value.platform == "woocommerce"
    assert denied.value.status == 401
    assert denied.value.code == "woocommerce_rest_cannot_view"


def test_missing_credential_raises_before_any_request(monkeypatch):
    fake = FakeTransport()
    adapter = _adapter(fake, monkeypatch)
    monkeypatch.delenv("WOO_CONSUMER_SECRET")
    with pytest.raises(CredentialMissing, match="WOO_CONSUMER_SECRET"):
        adapter.query("727")
    assert fake.calls == []


def test_non_https_site_url_is_refused_before_any_request(monkeypatch):
    """明文 HTTP 上发 Basic 头等于把 secret 交出去；Woo 对纯 HTTP 站点要求 OAuth 1.0a（本批不做）。"""
    fake = FakeTransport()
    adapter = _adapter(fake, monkeypatch)
    monkeypatch.setenv("WOO_SITE_URL", "http://shop.example.test")
    with pytest.raises(ValueError, match="https://"):
        adapter.query("727")
    assert fake.calls == []


def test_payload_without_id_is_commerce_error_not_keyerror(monkeypatch):
    """KeyError 在 OrderSystemPort 里专指「订单不存在」，形状坏掉的 200 不许被读成它。"""
    broken = {k: v for k, v in _ORDER.items() if k != "id"}
    with pytest.raises(CommerceError) as bad:
        _adapter(_serving(broken), monkeypatch).query("727")
    assert not isinstance(bad.value, KeyError)
    assert bad.value.code == "BAD_SHAPE"


# ---------------------------------------------------------------------------
# webhook：验签、去重、两个长得很像的头、非 JSON body
# ---------------------------------------------------------------------------

def test_webhook_valid_signature_over_raw_bytes_is_accepted():
    assert json.dumps(json.loads(BODY)).encode() != BODY
    store = _store()
    verdict = sc.ingest("woocommerce", BODY, _hooks(delivery_id="d-001", webhook_id="15"),
                        store=store)
    assert verdict.accepted is True
    assert verdict.http_status == 200
    assert verdict.verify_result == sc.VERIFY_OK
    assert verdict.event_id == "d-001"
    assert verdict.topic == "order.updated"
    assert verdict.order_ref == "727"
    assert verdict.should_resync is True


def test_webhook_duplicate_delivery_id_resyncs_once():
    store = _store()
    first = sc.ingest("woocommerce", BODY, _hooks(delivery_id="d-001"), store=store)
    again = sc.ingest("woocommerce", BODY, _hooks(delivery_id="d-001"), store=store)
    assert first.should_resync is True
    assert again.duplicate is True
    assert again.should_resync is False
    assert len(_rows(store)) == 2


def test_webhook_distinct_delivery_ids_of_same_webhook_both_resync():
    """同一个 webhook（X-WC-Webhook-ID 相同）的两次投递，Delivery-ID 不同 → 都要回源。
    拿 X-WC-Webhook-ID 当 event_id 的话，第二次起全被判成重投、永不回源。"""
    store = _store()
    first = sc.ingest("woocommerce", BODY, _hooks(delivery_id="d-001", webhook_id="15"),
                      store=store)
    second = sc.ingest("woocommerce", BODY, _hooks(delivery_id="d-002", webhook_id="15"),
                       store=store)
    assert first.duplicate is False and first.should_resync is True
    assert second.duplicate is False and second.should_resync is True


def test_webhook_bad_signature_is_recorded_and_400():
    store = _store()
    verdict = sc.ingest("woocommerce", BODY,
                        _hooks(signature=_sign(b'{"id":728,"status":"processing"}')),
                        store=store)
    assert verdict.accepted is False
    assert verdict.http_status == 400
    assert verdict.verify_result == sc.VERIFY_BAD_SIGNATURE
    rows = _rows(store)
    assert len(rows) == 1
    assert rows[0]["verify_result"] == "bad_signature"


def test_webhook_missing_delivery_id_is_400():
    store = _store()
    absent = sc.ingest("woocommerce", BODY, _hooks(delivery_id=None), store=store)
    blank = sc.ingest("woocommerce", BODY, _hooks(delivery_id="   "), store=store)
    for verdict in (absent, blank):
        assert verdict.accepted is False
        assert verdict.http_status == 400
        assert verdict.verify_result == sc.VERIFY_NO_EVENT_ID


def test_webhook_signed_non_json_body_is_rejected_not_raised():
    """签名对、body 不是 JSON → VerifyError → 400，不许 JSONDecodeError 从 ingest() 冒出去。"""
    store = _store()
    verdict = sc.ingest("woocommerce", NON_JSON_BODY,
                        _hooks(signature=NON_JSON_SIGNATURE), store=store)
    assert verdict.accepted is False
    assert verdict.http_status == 400
    assert verdict.verify_result == sc.VERIFY_BAD_SIGNATURE
    rows = _rows(store)
    assert len(rows) == 1
    # 钉住走的是「不是 JSON」那条分支，而不是签名本身没对上 —— 否则签名字面量抄错了也是绿的。
    assert "JSON" in rows[0]["note"]
    assert rows[0]["note"] != "签名不匹配"


def test_webhook_non_order_topic_does_not_resync():
    """同一个接收地址也收了 product.* 时，body 里的 id 是商品 id，不许拿去回源查单。"""
    body = b'{"id":99,"name":"T-shirt"}'
    store = _store()
    verdict = sc.ingest("woocommerce", body,
                        _hooks(topic="product.updated", signature=_sign(body)), store=store)
    assert verdict.accepted is True
    assert verdict.topic == "product.updated"
    assert verdict.order_ref == ""
    assert verdict.should_resync is False
