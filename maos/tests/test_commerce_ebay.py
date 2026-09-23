"""eBay 订单适配器 + 通知验签的验收 —— `maos/tools/commerce/ebay.py`。

形状照两份样板：查单那半照 `test_commerce_base.py`，webhook 那半照 `test_shop_callback.py`。

## 为什么本机上有两条 SKIPPED

真 ECDSA 的正反两条要 `cryptography`（可选依赖，本机没装），用运行期 skipif 门控；
**不在模块顶层 importorskip** —— 那会让整个文件在收集阶段消失，连缺依赖那条也一起。
缺依赖那条（`test_verify_dep_missing_is_rejected_not_accepted`）不靠「本机碰巧没装」：
它把 `sys.modules` 里所有 `cryptography*` 都置成 None，装没装都测同一件事。

## 验签器为什么在 fixture 里重新登记

`test_shop_callback.py` 的 autouse fixture 每条用例前后都清登记表。`ebay.py` 导入时
那一次登记，只要那份文件的用例先跑过就没了 —— 所以本文件每条用例自己登记。
"""

from __future__ import annotations

import base64
import importlib.util
import json
import sys

import pytest

from maos.core.store import SqliteStore
from maos.ingress import shop_callback as sc
from maos.tools.commerce import (
    CommerceError, FakeTransport, UnmappedOrderStatus,
)
from maos.tools.commerce.ebay import EbayAdapter, make_ebay_verifier
from maos.tools.order import ExternalOrder


# 假凭据写成一眼就看得出是假的形态（仓库是公开的）。
FAKE_ORDER_TOKEN = "FAKE-EBAY-USER-TOKEN-for-tests-only"
FAKE_APP_TOKEN = "FAKE-EBAY-APP-TOKEN-for-tests-only"

ORDER_ID = "12-34567-89012"
SANDBOX_ORDER_URL = f"https://api.sandbox.ebay.com/sell/fulfillment/v1/order/{ORDER_ID}"
PROD_ORDER_URL = f"https://api.ebay.com/sell/fulfillment/v1/order/{ORDER_ID}"

HAS_CRYPTOGRAPHY = importlib.util.find_spec("cryptography") is not None
NEEDS_CRYPTOGRAPHY = pytest.mark.skipif(
    not HAS_CRYPTOGRAPHY,
    reason="cryptography 未安装（可选依赖 maos[ingress]），真 ECDSA 验签跑不了")


def _order(**overrides) -> dict:
    """getOrder 响应的最小形状。`creationDate` 与 `lastModifiedDate` 故意不同、
    后者带**非零毫秒** —— 取错时间字段、或丢了毫秒，version 断言都会红。"""
    body = {
        "orderId": ORDER_ID,
        "creationDate": "2026-09-14T08:00:00.000Z",
        "lastModifiedDate": "2026-09-15T14:30:00.123Z",
        "orderFulfillmentStatus": "NOT_STARTED",
        "orderPaymentStatus": "PAID",
        "cancelStatus": {"cancelState": "NONE_REQUESTED", "cancelRequests": []},
        "pricingSummary": {"total": {"value": "128.50", "currency": "USD"}},
    }
    body.update(overrides)
    return body


def _adapter(transport: FakeTransport, monkeypatch) -> EbayAdapter:
    monkeypatch.setenv("EBAY_OAUTH_TOKEN", FAKE_ORDER_TOKEN)
    monkeypatch.delenv("EBAY_ENV", raising=False)
    return EbayAdapter(transport=transport, account="seller-demo")


def _query(order: dict, monkeypatch) -> ExternalOrder:
    fake = FakeTransport().expect("GET", SANDBOX_ORDER_URL, body=json.dumps(order))
    return _adapter(fake, monkeypatch).query(ORDER_ID)


# ---------------------------------------------------------------------------
# 查单：点名用例
# ---------------------------------------------------------------------------

def test_query_parses_five_fields_with_exact_version(monkeypatch):
    got = _query(_order(), monkeypatch)
    assert isinstance(got, ExternalOrder)
    assert got.order_id == ORDER_ID
    assert got.status == "paid"
    assert got.amount == "128.50"
    # 2026-09-15T14:30:00.123Z（lastModifiedDate，UTC）的 epoch 毫秒；
    # 取成 creationDate 会是 1789372800000，丢了毫秒会是 1789482600000。
    assert got.version == 1789482600123
    assert got.updated_at == "2026-09-15T14:30:00.123Z"


def test_query_not_found_raises_keyerror(monkeypatch):
    """getOrder 对不存在的单回 HTTP 404 —— 必须是 KeyError，不是一个 v1 空订单。"""
    body = json.dumps({"errors": [{"errorId": 32100, "domain": "API_FULFILLMENT",
                                   "category": "REQUEST", "message": "Invalid order ID"}]})
    fake = FakeTransport().expect("GET", SANDBOX_ORDER_URL, status=404, body=body)
    with pytest.raises(KeyError, match=ORDER_ID):
        _adapter(fake, monkeypatch).query(ORDER_ID)


def test_unknown_status_raises_unmapped(monkeypatch):
    """退过一部分款、还没发货的单：不是 paid，也不许兜底成 paid。"""
    order = _order(orderPaymentStatus="PARTIALLY_REFUNDED")
    with pytest.raises(UnmappedOrderStatus) as err:
        _query(order, monkeypatch)
    assert "ebay" in str(err.value)
    assert "PARTIALLY_REFUNDED" in str(err.value)


def test_secrets_absent_from_repr_and_errors(monkeypatch):
    adapter = _adapter(FakeTransport(), monkeypatch)
    assert FAKE_ORDER_TOKEN not in repr(adapter)
    assert "seller-demo" in repr(adapter)            # 账号标识不是密钥，排查要看

    texts: list[str] = []
    unauthorized = FakeTransport().expect(
        "GET", SANDBOX_ORDER_URL, status=401,
        body=json.dumps({"errors": [{"errorId": 1001, "message": "Invalid access token"}]}))
    with pytest.raises(CommerceError) as err:
        _adapter(unauthorized, monkeypatch).query(ORDER_ID)
    texts.append(str(err.value))
    missing = FakeTransport().expect("GET", SANDBOX_ORDER_URL, status=404, body="{}")
    with pytest.raises(KeyError) as err:
        _adapter(missing, monkeypatch).query(ORDER_ID)
    texts.append(str(err.value))
    with pytest.raises(KeyError) as err:                 # URL 拼错时 FakeTransport 的报错
        _adapter(FakeTransport(), monkeypatch).query(ORDER_ID)
    texts.append(str(err.value))

    assert all(FAKE_ORDER_TOKEN not in t for t in texts)


def test_cancelled_beats_paid(monkeypatch):
    order = _order(cancelStatus={"cancelState": "NONE_REQUESTED",
                                 "cancelledDate": "2026-09-15T12:00:00.000Z",
                                 "cancelRequests": []},
                   orderPaymentStatus="PAID")
    assert _query(order, monkeypatch).status == "cancelled"


def test_no_cancel_request_is_not_cancelled(monkeypatch):
    """`NONE_REQUESTED` 本身是非空串 —— 把 cancelState 写成「非空即取消」就会栽在这里。"""
    order = _order(cancelStatus={"cancelState": "NONE_REQUESTED", "cancelRequests": []},
                   orderPaymentStatus="PAID")
    got = _query(order, monkeypatch)
    assert got.status != "cancelled"
    assert got.status == "paid"


def test_ebay_env_defaults_to_sandbox(monkeypatch):
    fake = FakeTransport().expect("GET", SANDBOX_ORDER_URL, body=json.dumps(_order()))
    _adapter(fake, monkeypatch).query(ORDER_ID)          # _adapter 里 delenv 了 EBAY_ENV
    assert fake.calls[0]["url"].startswith("https://api.sandbox.ebay.com/")


# ---------------------------------------------------------------------------
# 查单：其它
# ---------------------------------------------------------------------------

def test_fulfilled_and_paid_is_shipped_not_paid(monkeypatch):
    """eBay 的单几乎都是同时 PAID 又 FULFILLED —— 付款排在发货前面，每一笔都会被判成 paid。"""
    order = _order(orderFulfillmentStatus="FULFILLED", orderPaymentStatus="PAID")
    assert _query(order, monkeypatch).status == "shipped"


def test_partially_shipped_order_is_shipped(monkeypatch):
    order = _order(orderFulfillmentStatus="IN_PROGRESS", orderPaymentStatus="PAID")
    assert _query(order, monkeypatch).status == "shipped"


def test_request_carries_bearer_token_and_production_host(monkeypatch):
    fake = FakeTransport().expect("GET", PROD_ORDER_URL, body=json.dumps(_order()))
    adapter = _adapter(fake, monkeypatch)
    monkeypatch.setenv("EBAY_ENV", "production")
    adapter.query(ORDER_ID)
    assert fake.calls[0]["headers"]["Authorization"] == f"Bearer {FAKE_ORDER_TOKEN}"


def test_unknown_ebay_env_raises_instead_of_guessing(monkeypatch):
    fake = FakeTransport()
    adapter = _adapter(fake, monkeypatch)
    monkeypatch.setenv("EBAY_ENV", "prod")
    with pytest.raises(ValueError, match="EBAY_ENV"):
        adapter.query(ORDER_ID)
    assert fake.calls == []                              # 认不出就不发请求


def test_error_body_is_read_into_commerce_error(monkeypatch):
    body = json.dumps({"errors": [{"errorId": 32700, "domain": "API_FULFILLMENT",
                                   "message": "Call usage limit has been reached"}]})
    fake = FakeTransport().expect("GET", SANDBOX_ORDER_URL, status=400, body=body)
    with pytest.raises(CommerceError) as err:
        _adapter(fake, monkeypatch).query(ORDER_ID)
    assert err.value.status == 400
    assert err.value.code == "32700"


def test_missing_order_id_is_shape_error_not_keyerror(monkeypatch):
    """响应缺 orderId 不许抛 KeyError —— KeyError 在 OrderSystemPort 里的意思是「查无此单」。"""
    order = _order()
    del order["orderId"]
    with pytest.raises(CommerceError, match="BAD_SHAPE"):
        _query(order, monkeypatch)


# ---------------------------------------------------------------------------
# 通知验签：共用的材料
# ---------------------------------------------------------------------------

KID = "kid-fake-0001"
SANDBOX_KEY_URL = f"https://api.sandbox.ebay.com/commerce/notification/v1/public_key/{KID}"
GOOD_SIG = b"stub-signature-accepted-by-the-test-double"
BAD_SIG = b"stub-signature-the-test-double-rejects"


def _notification(*, notification_id: str | None = "notif-0001",
                  topic: str = "ORDER_CONFIRMATION") -> bytes:
    note: dict = {"eventDate": "2026-09-15T14:30:00.000Z",
                  "publishDate": "2026-09-15T14:30:01.000Z", "publishAttemptCount": 1,
                  "data": {"user": {"userId": "fake-user"},
                           "order": {"orderId": ORDER_ID, "orderLineItems": []}}}
    if notification_id is not None:
        note["notificationId"] = notification_id
    return json.dumps({"metadata": {"topic": topic, "schemaVersion": "1.0",
                                    "deprecated": False},
                       "notification": note}).encode("utf-8")


def _signature_header(signature: bytes, *, kid: str = KID) -> dict:
    packed = {"alg": "ecdsa", "kid": kid,
              "signature": base64.b64encode(signature).decode("ascii"), "digest": "SHA1"}
    return {"X-EBAY-SIGNATURE": base64.b64encode(json.dumps(packed).encode()).decode("ascii"),
            "Content-Type": "application/json"}


def _key_response(key_text: str) -> str:
    return json.dumps({"algorithm": "ECDSA", "digest": "SHA1", "key": key_text})


#: 替身公钥：结构上是合法的 base64，替身后端不看它。
_STUB_KEY = "-----BEGIN PUBLIC KEY-----" + base64.b64encode(b"MAOS-TEST-KEY").decode() \
            + "-----END PUBLIC KEY-----"


def _stub_backend():
    """顶替「惰性 import + ECDSA 验签」整步，不 import cryptography。
    **对不上就拒**：只认 GOOD_SIG，其余一律 False。"""
    return lambda _key_der, signature, _raw: signature == GOOD_SIG


def _key_transport() -> FakeTransport:
    return FakeTransport().expect("GET", SANDBOX_KEY_URL, body=_key_response(_STUB_KEY))


def _stub_verifier(transport: FakeTransport):
    return make_ebay_verifier(transport=transport, backend=_stub_backend)


def _store():
    s = SqliteStore()
    s.init_schema()
    return s


def _rows(store) -> list[dict]:
    cur = store._conn.execute(
        "SELECT platform, event_id, topic, verify_result, note FROM shop_callback ORDER BY id")
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


@pytest.fixture(autouse=True)
def _ebay_registry(monkeypatch):
    sc.reset_verifiers()
    sc._schema_ready.clear()
    monkeypatch.delenv("EBAY_ENV", raising=False)
    sc.register_verifier("ebay", _stub_verifier(_key_transport()))
    yield
    sc.reset_verifiers()


@pytest.fixture
def app_token(monkeypatch):
    """取公钥要的应用令牌（假值）。**不做成 autouse**：缺依赖那条用例一个 `EBAY_*`
    都不许设 —— 读凭据若排到了惰性 import 之前，它就会先撞 CredentialMissing 而变红。"""
    monkeypatch.setenv("EBAY_APP_TOKEN", FAKE_APP_TOKEN)


# ---------------------------------------------------------------------------
# 通知验签：点名用例
# ---------------------------------------------------------------------------

def test_verify_dep_missing_is_rejected_not_accepted(monkeypatch):
    """生产签名路径 + 强制缺依赖：拒绝、400、落库，且一个请求都没发。

    只把顶层 `cryptography` 置 None 拦不住已缓存的子模块（`from cryptography.x import y`
    直接命中子模块缓存，不查父包），所以**所有** `cryptography*` 键都置 None。
    """
    for name in [k for k in sys.modules if k == "cryptography" or k.startswith("cryptography.")]:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, "cryptography", None)
    for name in ("EBAY_OAUTH_TOKEN", "EBAY_APP_TOKEN", "EBAY_ENV"):
        monkeypatch.delenv(name, raising=False)

    transport = FakeTransport()                          # 空的：任何请求都会 KeyError
    sc.register_verifier("ebay", make_ebay_verifier(transport=transport))

    store = _store()
    verdict = sc.ingest("ebay", _notification(), _signature_header(GOOD_SIG), store=store)
    assert verdict.verify_result == sc.VERIFY_DEP_MISSING
    assert verdict.accepted is False
    assert verdict.http_status == 400
    rows = _rows(store)
    assert len(rows) == 1 and rows[0]["verify_result"] == sc.VERIFY_DEP_MISSING
    assert transport.calls == []                         # 缺依赖在取公钥之前就判出


def test_webhook_bad_signature_recorded_400(app_token):
    store = _store()
    body = _notification()

    missing = sc.ingest("ebay", body, {"Content-Type": "application/json"}, store=store)
    garbled = sc.ingest("ebay", body, {"X-EBAY-SIGNATURE": "not-a-json-envelope"}, store=store)
    mismatch = sc.ingest("ebay", body, _signature_header(BAD_SIG), store=store)

    for verdict in (missing, garbled, mismatch):
        assert verdict.verify_result == sc.VERIFY_BAD_SIGNATURE
        assert verdict.accepted is False and verdict.http_status == 400
        assert verdict.should_resync is False
    rows = _rows(store)
    assert len(rows) == 3                                # 失败的也落库：那是攻击证据
    assert {r["verify_result"] for r in rows} == {sc.VERIFY_BAD_SIGNATURE}


def test_webhook_redelivery_is_deduplicated(app_token):
    store = _store()
    body, headers = _notification(), _signature_header(GOOD_SIG)
    first = sc.ingest("ebay", body, headers, store=store)
    second = sc.ingest("ebay", body, headers, store=store)

    assert first.accepted is True and first.duplicate is False
    assert first.event_id == "notif-0001" and first.topic == "ORDER_CONFIRMATION"
    assert first.order_ref == ORDER_ID and first.should_resync is True   # 只回源这一次
    assert second.duplicate is True and second.should_resync is False
    assert second.http_status == 200                     # 让平台停止重投
    assert len(_rows(store)) == 2


def test_webhook_missing_event_id_400(app_token):
    store = _store()
    verdict = sc.ingest("ebay", _notification(notification_id=None),
                        _signature_header(GOOD_SIG), store=store)
    assert verdict.verify_result == sc.VERIFY_NO_EVENT_ID
    assert verdict.accepted is False and verdict.http_status == 400
    assert _rows(store)[0]["verify_result"] == sc.VERIFY_NO_EVENT_ID


def _ecdsa_material(raw: bytes):
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    private = ec.generate_private_key(ec.SECP256R1())
    signature = private.sign(raw, ec.ECDSA(hashes.SHA1()))
    der = private.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    # getPublicKey 回的是**单行** PEM，照那个形状喂，钉住解析路径。
    key_text = ("-----BEGIN PUBLIC KEY-----" + base64.b64encode(der).decode("ascii")
                + "-----END PUBLIC KEY-----")
    return signature, key_text


@NEEDS_CRYPTOGRAPHY
def test_verify_valid_ecdsa_signature_accepted(app_token):
    raw = _notification()
    signature, key_text = _ecdsa_material(raw)
    transport = FakeTransport().expect("GET", SANDBOX_KEY_URL, body=_key_response(key_text))
    sc.register_verifier("ebay", make_ebay_verifier(transport=transport))

    verdict = sc.ingest("ebay", raw, _signature_header(signature), store=_store())
    assert verdict.verify_result == sc.VERIFY_OK and verdict.accepted is True
    assert verdict.order_ref == ORDER_ID


@NEEDS_CRYPTOGRAPHY
def test_verify_tampered_signature_rejected(app_token):
    raw = _notification()
    signature, key_text = _ecdsa_material(raw)
    transport = FakeTransport().expect("GET", SANDBOX_KEY_URL, body=_key_response(key_text))
    sc.register_verifier("ebay", make_ebay_verifier(transport=transport))

    tampered = bytearray(raw)
    tampered[-2] ^= 0x01                                 # 改一个字节
    verdict = sc.ingest("ebay", bytes(tampered), _signature_header(signature), store=_store())
    assert verdict.verify_result == sc.VERIFY_BAD_SIGNATURE
    assert verdict.accepted is False and verdict.http_status == 400


# ---------------------------------------------------------------------------
# 通知验签：其它
# ---------------------------------------------------------------------------

def test_public_key_is_fetched_once_per_kid(app_token):
    """官方建议缓存公钥，别每条通知都去取 —— 会撞调用限额。"""
    transport = _key_transport()
    sc.register_verifier("ebay", _stub_verifier(transport))
    store = _store()
    sc.ingest("ebay", _notification(notification_id="n-1"), _signature_header(GOOD_SIG), store=store)
    sc.ingest("ebay", _notification(notification_id="n-2"), _signature_header(GOOD_SIG), store=store)
    assert len(transport.calls) == 1
    assert transport.calls[0]["headers"]["Authorization"] == f"Bearer {FAKE_APP_TOKEN}"


def test_key_fetch_failure_is_recorded_as_bad_signature(monkeypatch):
    """取公钥失败（没配应用令牌 / 端点不通）要转成 VerifyError，不许穿出 ingest() ——
    穿出去这条回调就不落库了。应用令牌也不许出现在落库的 note 里。"""
    store = _store()
    monkeypatch.delenv("EBAY_APP_TOKEN", raising=False)
    no_token = sc.ingest("ebay", _notification(), _signature_header(GOOD_SIG), store=store)

    monkeypatch.setenv("EBAY_APP_TOKEN", FAKE_APP_TOKEN)
    sc.register_verifier("ebay", _stub_verifier(FakeTransport()))       # 空的：取不到公钥
    unreachable = sc.ingest("ebay", _notification(), _signature_header(GOOD_SIG), store=store)

    for verdict in (no_token, unreachable):
        assert verdict.verify_result == sc.VERIFY_BAD_SIGNATURE
        assert verdict.http_status == 400
    rows = _rows(store)
    assert len(rows) == 2
    assert "EBAY_APP_TOKEN" in rows[0]["note"]
    assert all(FAKE_APP_TOKEN not in r["note"] for r in rows)


def test_marked_shipped_topic_carries_flat_order_id(app_token):
    """ITEM_MARKED_SHIPPED 的 orderId 平铺在 data 下，不在 data.order 里。"""
    raw = json.dumps({"metadata": {"topic": "ITEM_MARKED_SHIPPED", "schemaVersion": "1.0"},
                      "notification": {"notificationId": "notif-ship-1",
                                       "data": {"orderId": ORDER_ID, "trackingNumber": "1Z"}}}
                     ).encode("utf-8")
    verdict = sc.ingest("ebay", raw, _signature_header(GOOD_SIG), store=_store())
    assert verdict.accepted is True
    assert verdict.order_ref == ORDER_ID and verdict.should_resync is True


def test_module_registers_production_verifier_on_import():
    """分支 A：模块导入时登记了生产验签器。

    在子进程里看 —— 本进程的登记表被 fixture 清过；而 `importlib.reload` 会造出第二个
    `EbayAdapter` 类对象，按子类扫描的一致性测试（T166）会把它当成重名平台。
    """
    import subprocess

    probe = ("import maos.tools.commerce.ebay as m, maos.ingress.shop_callback as sc;"
             "print(sc._VERIFIERS.get('ebay') is m.verify_ebay)")
    proc = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                          timeout=60)
    assert proc.returncode == 0, proc.stderr[-500:]
    assert proc.stdout.strip() == "True"
