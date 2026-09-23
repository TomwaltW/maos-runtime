"""Shopify 适配器与 webhook 验签器的验收 —— `maos/tools/commerce/shopify.py`（p11 · T161）。

形状照 `test_commerce_base.py`（查单）与 `test_shop_callback.py`（回调）两份样板。
一律 `FakeTransport`，不打真网；凭据一律 `monkeypatch.setenv` 造一眼看得出是假的值。
夹具字段取自 shopify.dev 上 REST Admin API 2026-07 的 Order / Refund 响应样例（裁剪过）。

唯一一条打真网的用例在文件末尾，三个 `SHOPIFY_*` 环境变量缺任何一个就 skip。
"""

from __future__ import annotations

import base64
import json
import os

import pytest

from maos.core.store import SqliteStore
from maos.ingress import shop_callback as sc
from maos.tools.commerce import (
    CommerceError, CredentialMissing, FakeTransport, UnmappedOrderStatus,
)
from maos.tools.commerce import shopify
from maos.tools.order import ExternalOrder


SHOP = "my-shop.myshopify.com"
FAKE_TOKEN = "fake-shopify-admin-token-DO-NOT-USE"
FAKE_SECRET = "fake-shopify-webhook-secret-DO-NOT-USE"

ORDER_ID = 450789469          # 订单主键（shopify.dev Refund 样例里的 order_id）
ORDER_NUMBER = 1001           # 给客户看的单号，刻意与主键不同
REFUND_ID = 509562969         # 退款单号（shopify.dev Refund 样例里的 id）

URL = f"https://{SHOP}/admin/api/{shopify.API_VERSION}/orders/{ORDER_ID}.json"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _shopify_verifier_registered():
    # 只重新登记 shopify 这一个，不清全局登记表：清表会把排在后面的别的平台一起抹掉。
    # 模块 import 时登记过一次，但 test_shop_callback.py 的 teardown 会清空登记表，
    # 反着顺序跑时靠这里补回来。建表缓存按 id(store) 记，CPython 会复用 id，一并清掉。
    sc._schema_ready.clear()
    sc.register_verifier("shopify", shopify.verify_shopify)
    yield


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("SHOPIFY_SHOP_DOMAIN", SHOP)
    monkeypatch.setenv("SHOPIFY_ACCESS_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("SHOPIFY_WEBHOOK_SECRET", FAKE_SECRET)


def _order(**overrides) -> dict:
    order = {
        "id": ORDER_ID,
        "order_number": ORDER_NUMBER,
        "name": f"#{ORDER_NUMBER}",
        "currency": "USD",
        "total_price": "598.94",
        "financial_status": "paid",
        "fulfillment_status": None,
        "cancelled_at": None,
        "cancel_reason": None,
        "created_at": "2026-09-14T09:00:00-04:00",
        "updated_at": "2026-09-15T10:30:00-04:00",
    }
    order.update(overrides)
    return order


def _adapter(fake: FakeTransport) -> shopify.ShopifyAdapter:
    return shopify.ShopifyAdapter(transport=fake, account=SHOP)


def _query(**overrides) -> ExternalOrder:
    fake = FakeTransport().expect("GET", URL, body=json.dumps({"order": _order(**overrides)}))
    return _adapter(fake).query(str(ORDER_ID))


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


def _hook_headers(body: bytes, *, topic: str = "orders/updated",
                  webhook_id: str | None = "b54557e4-bdd9-4b37-8a5f-bf7d70bcd043",
                  secret: str = FAKE_SECRET) -> dict:
    h = {
        "Content-Type": "application/json",
        "X-Shopify-Topic": topic,
        "X-Shopify-Shop-Domain": SHOP,
        "X-Shopify-API-Version": shopify.API_VERSION,
        "X-Shopify-Hmac-Sha256": sc.hmac_sha256_base64(secret, body),
    }
    if webhook_id is not None:
        h["X-Shopify-Webhook-Id"] = webhook_id
    return h


# 投递体是资源本身（不像 REST GET 那样包一层 {"order": ...}）。
ORDER_HOOK = json.dumps(_order()).encode("utf-8")
REFUND_HOOK = json.dumps({
    "id": REFUND_ID,
    "order_id": ORDER_ID,
    "created_at": "2026-07-01T12:17:28-04:00",
    "note": "it broke during shipping",
    "processed_at": "2026-07-01T12:17:28-04:00",
    "refund_line_items": [],
    "transactions": [],
}).encode("utf-8")


# ---------------------------------------------------------------------------
# 查单：点名的几条
# ---------------------------------------------------------------------------

def test_query_returns_external_order_with_exact_version(creds):
    order = _query()
    assert isinstance(order, ExternalOrder)
    assert order.order_id == str(ORDER_ID)
    assert order.order_id != str(ORDER_NUMBER)          # 主键是 id，不是 order_number
    # 2026-09-15T10:30:00-04:00 == 2026-09-15 14:30:00 UTC，epoch 毫秒写死成字面量
    assert order.version == 1789482600000
    assert order.status == "paid"
    assert order.amount == "598.94"
    assert order.updated_at == "2026-09-15T10:30:00-04:00"   # 原样，不重新格式化


def test_missing_order_raises_key_error(creds):
    """对**正确的 URL** 预置 404。match 那句只有基座的 404 分支会说 ——
    URL 拼错时 FakeTransport 自己也抛 KeyError，但文案不同，这条会红。"""
    fake = FakeTransport().expect("GET", URL, status=404, body='{"errors":"Not Found"}')
    with pytest.raises(KeyError, match="里没有订单"):
        _adapter(fake).query(str(ORDER_ID))


def test_unknown_status_raises_unmapped_order_status(creds):
    """只授权未扣款、未发货、未取消 —— 表里没有这个组合，抛，不兜底成 paid。"""
    with pytest.raises(UnmappedOrderStatus) as err:
        _query(financial_status="authorized")
    assert "shopify" in str(err.value)
    assert "authorized" in str(err.value)


def test_cancelled_after_paid_maps_to_cancelled(creds):
    """付过款又取消：取消优先于付款状态。发货维度留空，只让取消与付款两条规则竞争。"""
    order = _query(financial_status="paid", fulfillment_status=None,
                   cancelled_at="2026-09-15T11:00:00-04:00", cancel_reason="customer")
    assert order.status == "cancelled"


def test_query_request_uses_shop_domain_and_api_version(creds):
    fake = FakeTransport().expect("GET", URL, body=json.dumps({"order": _order()}))
    _adapter(fake).query(str(ORDER_ID))
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["method"] == "GET"
    assert call["url"] == (f"https://my-shop.myshopify.com/admin/api/"
                           f"{shopify.API_VERSION}/orders/{ORDER_ID}.json")
    assert ".myshopify.com.myshopify.com" not in call["url"]
    assert call["headers"]["X-Shopify-Access-Token"] == FAKE_TOKEN


def test_secret_absent_from_repr_and_exception_text(creds):
    adapter = _adapter(FakeTransport().expect(
        "GET", URL, status=401,
        body='{"errors":"[API] Invalid API key or access token '
             '(unrecognized login or wrong password)"}'))
    text = repr(adapter)
    assert FAKE_TOKEN not in text and FAKE_SECRET not in text
    assert SHOP in text                                   # 店铺标识不是密钥，排查要看

    with pytest.raises(CommerceError) as err:
        adapter.query(str(ORDER_ID))
    assert err.value.status == 401
    assert FAKE_TOKEN not in str(err.value) and FAKE_SECRET not in str(err.value)

    store = _store()
    sc.ingest("shopify", ORDER_HOOK, _hook_headers(ORDER_HOOK), store=store)
    sc.ingest("shopify", ORDER_HOOK, _hook_headers(ORDER_HOOK, secret="not-the-secret"),
              store=store)
    rows = _rows(store)
    assert [r["verify_result"] for r in rows] == [sc.VERIFY_OK, sc.VERIFY_BAD_SIGNATURE]
    for row in rows:
        for column in ("headers_json", "note"):
            assert FAKE_TOKEN not in row[column]
            assert FAKE_SECRET not in row[column]


# ---------------------------------------------------------------------------
# webhook：点名的几条
# ---------------------------------------------------------------------------

def test_duplicate_webhook_id_resyncs_once(creds):
    store = _store()
    first = sc.ingest("shopify", ORDER_HOOK, _hook_headers(ORDER_HOOK), store=store)
    second = sc.ingest("shopify", ORDER_HOOK, _hook_headers(ORDER_HOOK), store=store)
    assert first.http_status == 200 and second.http_status == 200
    assert first.should_resync is True
    assert first.order_ref == str(ORDER_ID)
    assert second.duplicate is True
    assert second.should_resync is False
    assert len(_rows(store)) == 2                          # 重投也落一行


def test_bad_signature_is_recorded_and_400(creds):
    store = _store()
    verdict = sc.ingest("shopify", ORDER_HOOK,
                        _hook_headers(ORDER_HOOK, secret="not-the-secret"), store=store)
    assert verdict.accepted is False
    assert verdict.http_status == 400
    assert verdict.verify_result == sc.VERIFY_BAD_SIGNATURE
    rows = _rows(store)
    assert len(rows) == 1
    assert rows[0]["verify_result"] == sc.VERIFY_BAD_SIGNATURE
    assert base64.b64decode(rows[0]["raw_body_b64"]) == ORDER_HOOK


def test_missing_webhook_id_is_400(creds):
    for webhook_id in (None, "   "):                      # 没带头 / 头是空白
        store = _store()
        verdict = sc.ingest("shopify", ORDER_HOOK,
                            _hook_headers(ORDER_HOOK, webhook_id=webhook_id), store=store)
        assert verdict.accepted is False
        assert verdict.http_status == 400
        assert verdict.verify_result == sc.VERIFY_NO_EVENT_ID
        assert _rows(store)[0]["verify_result"] == sc.VERIFY_NO_EVENT_ID


def test_valid_signature_is_computed_over_raw_bytes(creds):
    """非规范的空白与键序：重新序列化一遍就不是这串字节了，签名也就对不上。"""
    raw = (b'{"updated_at" : "2026-09-15T10:30:00-04:00",\n'
           b'   "financial_status":"paid",  "id":450789469 }')
    assert json.dumps(json.loads(raw)).encode() != raw
    store = _store()
    verdict = sc.ingest("shopify", raw, _hook_headers(raw), store=store)
    assert verdict.accepted is True
    assert verdict.verify_result == sc.VERIFY_OK
    assert verdict.order_ref == str(ORDER_ID)


def test_refund_webhook_order_ref_is_the_order_id(creds):
    """refunds/create 的投递体是 Refund 资源：顶层 id 是退款单号，订单号在 order_id。"""
    store = _store()
    verdict = sc.ingest("shopify", REFUND_HOOK,
                        _hook_headers(REFUND_HOOK, topic="refunds/create"), store=store)
    assert verdict.accepted is True
    assert verdict.topic == "refunds/create"
    assert verdict.order_ref == str(ORDER_ID)
    assert verdict.order_ref != str(REFUND_ID)
    assert verdict.should_resync is True


# ---------------------------------------------------------------------------
# 查单：规则表的其余取值
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("financial", [
    "partially_refunded", "refunded", "partially_paid", "pending", "voided",
])
def test_financial_states_outside_table_are_not_guessed(creds, financial):
    """退过款 / 没付全 / 没扣款的单一律抛：四态里没有一个词说得清，snapshot_check
    会把读失败判成漂移、转人工 —— 这是唯一真能停下来的路径（DECISIONS task-t161）。"""
    with pytest.raises(UnmappedOrderStatus, match=financial):
        _query(financial_status=financial)


@pytest.mark.parametrize("fulfillment", ["fulfilled", "partial"])
def test_fulfillment_progress_maps_to_shipped(creds, fulfillment):
    assert _query(fulfillment_status=fulfillment).status == "shipped"


def test_restocked_maps_to_cancelled(creds):
    """官方释义：每件都已回库**且订单已取消**。cancelled_at 缺失时也归 cancelled。"""
    assert _query(fulfillment_status="restocked").status == "cancelled"


def test_shipped_order_with_partial_refund_reads_as_shipped(creds):
    """已知缺口，钉住现状：规则只能正向命中，发货维度先命中，退款维度被盖住。
    要改成「退过款即拦」需要基座支持否决规则，记在 BACKLOG task-t161。"""
    order = _query(fulfillment_status="fulfilled",
                   financial_status="partially_refunded")
    assert order.status == "shipped"


def test_validation_errors_object_is_flattened_into_message(creds):
    body = '{"errors":{"order":["is invalid"],"base":["read_orders scope required"]}}'
    fake = FakeTransport().expect("GET", URL, status=422, body=body)
    with pytest.raises(CommerceError) as err:
        _adapter(fake).query(str(ORDER_ID))
    assert err.value.status == 422
    assert "read_orders scope required" in str(err.value)


def test_non_json_error_body_is_passed_through(creds):
    fake = FakeTransport().expect("GET", URL, status=503, body="<html>upstream</html>")
    with pytest.raises(CommerceError, match="upstream"):
        _adapter(fake).query(str(ORDER_ID))


def test_response_without_order_object_is_not_a_key_error(creds):
    """响应缺 order 对象是「形状不对」，不是「订单不存在」—— 不许抛成 KeyError。"""
    fake = FakeTransport().expect("GET", URL, body='{"orders":[]}')
    with pytest.raises(CommerceError, match="BAD_SHAPE"):
        _adapter(fake).query(str(ORDER_ID))


def test_order_id_is_escaped_in_the_path(creds):
    escaped = f"https://{SHOP}/admin/api/{shopify.API_VERSION}/orders/..%2Fshop.json"
    fake = FakeTransport().expect("GET", escaped, status=404, body='{"errors":"Not Found"}')
    with pytest.raises(KeyError, match="里没有订单"):
        _adapter(fake).query("../shop")
    assert fake.calls[0]["url"] == escaped


@pytest.mark.parametrize("env_key", ["SHOPIFY_SHOP_DOMAIN", "SHOPIFY_ACCESS_TOKEN"])
def test_absent_query_credential_raises_credential_missing(creds, monkeypatch, env_key):
    monkeypatch.delenv(env_key)
    adapter = _adapter(FakeTransport())
    with pytest.raises(CredentialMissing, match=env_key):
        adapter.query(str(ORDER_ID))


# ---------------------------------------------------------------------------
# webhook：其余形状
# ---------------------------------------------------------------------------

def test_signed_non_json_body_is_rejected_and_recorded(creds):
    raw = b"not json at all"
    store = _store()
    verdict = sc.ingest("shopify", raw, _hook_headers(raw), store=store)
    assert verdict.accepted is False and verdict.http_status == 400
    assert verdict.verify_result == sc.VERIFY_BAD_SIGNATURE     # fail-closed
    assert len(_rows(store)) == 1


def test_order_topic_without_id_is_rejected(creds):
    raw = json.dumps({"order_number": ORDER_NUMBER}).encode("utf-8")
    store = _store()
    verdict = sc.ingest("shopify", raw, _hook_headers(raw), store=store)
    assert verdict.accepted is False and verdict.http_status == 400
    assert len(_rows(store)) == 1


@pytest.mark.parametrize("topic", ["orders/edited", "app/uninstalled"])
def test_topic_with_unverified_shape_has_no_order_ref(creds, topic):
    """没核过投递体形状的 topic 不猜订单号：照收照落库，只是不触发回源。"""
    store = _store()
    verdict = sc.ingest("shopify", ORDER_HOOK, _hook_headers(ORDER_HOOK, topic=topic),
                        store=store)
    assert verdict.accepted is True
    assert verdict.order_ref == ""
    assert verdict.should_resync is False
    assert len(_rows(store)) == 1


def test_verifier_is_registered_at_import():
    assert sc._VERIFIERS.get("shopify") is shopify.verify_shopify


# ---------------------------------------------------------------------------
# 真凭据实跑（本轮必 skip）
# ---------------------------------------------------------------------------

_LIVE_ENV = ("SHOPIFY_SHOP_DOMAIN", "SHOPIFY_ACCESS_TOKEN", "SHOPIFY_WEBHOOK_SECRET")


@pytest.mark.skipif(
    not all(os.environ.get(k, "").strip() for k in _LIVE_ENV),
    reason="没有 Shopify dev store 凭据：SHOPIFY_SHOP_DOMAIN / SHOPIFY_ACCESS_TOKEN / "
           "SHOPIFY_WEBHOOK_SECRET 缺一即 skip",
)
def test_live_dev_store_answers_404_for_absent_order():
    """打真网：对一个不存在的订单号，凭据对就是 404 → KeyError，凭据错就是 401 → CommerceError。
    一次往返同时验了域名、版本、鉴权头三件事。本机 Python.framework 缺根证书时会报
    CERTIFICATE_VERIFY_FAILED，要先在 shell 里设 SSL_CERT_FILE（见 BACKLOG task-t161）。"""
    with pytest.raises(KeyError, match="里没有订单"):
        shopify.ShopifyAdapter(account=os.environ["SHOPIFY_SHOP_DOMAIN"]).query("1")
