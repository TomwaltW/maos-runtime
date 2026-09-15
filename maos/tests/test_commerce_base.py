"""跨境电商接入基座的验收 —— `maos/tools/commerce/` 的 transport / mapping / base。

这份用例同时是**七个平台轨的样板**：每轨的 `test_<platform>.py` 照这个形状写，
把 `_RefAdapter` 换成真适配器、把 `_REF_RULES` 换成该平台带 `source` 的规则表。

本文件只测基座，**不测任何真实平台** —— 平台各自的规则表由各轨自己钉。
"""

from __future__ import annotations

import json

import pytest

from maos.tools.commerce import (
    ANY_NON_EMPTY, CommerceAdapter, CommerceError, CredentialMissing,
    FakeTransport, HttpResponse, QueryRequest, RawOrder, StatusRule,
    TransportError, UnmappedOrderStatus, UnparsableTimestamp, UrllibTransport,
    derive_version, dig, map_status, normalize_amount, read_credential, redact_url,
)
from maos.tools.order import ALL_ORDER_STATUSES, ExternalOrder


# ---------------------------------------------------------------------------
# 参照适配器 —— 只给本文件用，示范三个钩子怎么填
# ---------------------------------------------------------------------------

_REF_RULES = (
    # 顺序即优先级：取消优先于付款状态。见 mapping.StatusRule 的类 docstring。
    StatusRule("cancelled_at", ANY_NON_EMPTY, "cancelled", source="参照平台文档 §取消"),
    StatusRule("fulfillment_status", ("fulfilled", "partial"), "shipped",
               source="参照平台文档 §发货"),
    StatusRule("financial_status", ("paid", "partially_paid"), "paid",
               source="参照平台文档 §付款"),
)


class _RefAdapter(CommerceAdapter):
    platform = "refmart"
    status_rules = _REF_RULES

    def build_query_request(self, order_id: str) -> QueryRequest:
        token = read_credential("REFMART_TOKEN", platform=self.platform,
                                purpose="参照平台的 Admin API token")
        return QueryRequest(
            method="GET",
            url=f"https://{self.account}/api/orders/{order_id}.json",
            headers={"X-Refmart-Token": token},
        )

    def parse_order(self, payload: dict) -> RawOrder:
        order = payload["order"]
        return RawOrder(order_id=order["id"], status_payload=order,
                        amount=order["total_price"], updated_at=order["updated_at"])

    def read_error(self, response: HttpResponse) -> tuple[str, str]:
        try:
            body = json.loads(response.body)
        except ValueError:
            return "", response.text()[:500]
        return str(body.get("code", "")), str(body.get("message", ""))


def _ref(transport: FakeTransport, monkeypatch) -> _RefAdapter:
    monkeypatch.setenv("REFMART_TOKEN", "tok-secret-value")
    return _RefAdapter(transport=transport, account="demo.refmart.test")


_OK_BODY = json.dumps({"order": {
    "id": "R-1001", "total_price": "128.50",
    "financial_status": "paid", "fulfillment_status": None, "cancelled_at": None,
    "updated_at": "2026-09-15T10:30:00-04:00",
}})

_URL = "https://demo.refmart.test/api/orders/R-1001.json"


# ---------------------------------------------------------------------------
# mapping：version 派生
# ---------------------------------------------------------------------------

def test_derive_version_iso_with_offset_is_epoch_millis():
    # 2026-09-15T10:30:00-04:00 == 14:30:00Z
    assert derive_version("2026-09-15T10:30:00-04:00") == 1789482600000


def test_derive_version_accepts_trailing_z():
    assert derive_version("2026-09-15T14:30:00Z") == 1789482600000


def test_derive_version_same_instant_across_notations_is_identical():
    """两种写法同一时刻必须派生出同一个 version —— 否则同一笔单换个平台就"漂移"了。"""
    assert derive_version("2026-09-15T10:30:00-04:00") == derive_version("2026-09-15T14:30:00Z")


@pytest.mark.parametrize("raw,expected", [
    (1789482600, 1789482600000),        # epoch 秒（Shopee 的 update_time）
    ("1789482600", 1789482600000),
    (1789482600000, 1789482600000),     # epoch 毫秒
    ("1789482600000", 1789482600000),
])
def test_derive_version_accepts_epoch_seconds_and_millis(raw, expected):
    assert derive_version(raw) == expected


def test_derive_version_is_millisecond_resolution():
    """秒级分辨率会把同一秒内的两次改单压成一版，漂移就漏报了 —— 这是最不能出的错。"""
    a = derive_version("2026-09-15T14:30:00.100Z")
    b = derive_version("2026-09-15T14:30:00.900Z")
    assert a != b and b - a == 800


def test_derive_version_rejects_naive_timestamp():
    """不带时区的串按本地 TZ 解会让同一笔单在两台机器上差几小时 —— 判据当场失效。"""
    with pytest.raises(UnparsableTimestamp, match="不带时区"):
        derive_version("2026-09-15T10:30:00", platform="refmart")


def test_derive_version_rejects_missing():
    with pytest.raises(UnparsableTimestamp, match="没给 updated_at"):
        derive_version(None, platform="refmart")
    with pytest.raises(UnparsableTimestamp):
        derive_version("   ", platform="refmart")


def test_derive_version_rejects_garbage():
    with pytest.raises(UnparsableTimestamp):
        derive_version("last tuesday", platform="refmart")


def test_derive_version_monotonic_with_real_time():
    """漂移判据全靠这条：外部改一次单，派生值必须变大。"""
    assert derive_version("2026-09-15T14:30:00Z") < derive_version("2026-09-15T14:30:01Z")


# ---------------------------------------------------------------------------
# mapping：状态归一
# ---------------------------------------------------------------------------

def test_map_status_first_matching_rule_wins():
    payload = {"cancelled_at": "2026-09-15T12:00:00Z",
               "financial_status": "paid", "fulfillment_status": "fulfilled"}
    # 三条规则同时命中，取消排最前 —— 付过款又取消了的单，答案只能是 cancelled
    assert map_status(_REF_RULES, payload, platform="refmart") == "cancelled"


def test_map_status_skips_none_and_null_strings():
    payload = {"cancelled_at": None, "fulfillment_status": "null",
               "financial_status": "paid"}
    assert map_status(_REF_RULES, payload, platform="refmart") == "paid"


def test_map_status_raises_on_unknown_value():
    """没见过的状态最可能是 partially_refunded / 风控态 —— 恰恰是不该往下走的那些。"""
    payload = {"financial_status": "partially_refunded",
               "fulfillment_status": None, "cancelled_at": None}
    with pytest.raises(UnmappedOrderStatus) as err:
        map_status(_REF_RULES, payload, platform="refmart")
    # 异常必须带平台名与读到的字段，否则人拿什么去查官方文档
    assert "refmart" in str(err.value)
    assert "partially_refunded" in str(err.value)


def test_map_status_raises_on_empty_payload():
    with pytest.raises(UnmappedOrderStatus):
        map_status(_REF_RULES, {}, platform="refmart")


def test_status_rule_rejects_target_outside_four_states():
    with pytest.raises(ValueError, match="不在 ExternalOrder 的四态里"):
        StatusRule("x", ("y",), "refunded", source="编的")


def test_status_rule_rejects_empty_source():
    """核不到出处的规则不许进表 —— 口径同 gateway_codes.GatewayCode.source。"""
    with pytest.raises(ValueError, match="缺 source"):
        StatusRule("x", ("y",), "paid", source="  ")


def test_status_rule_targets_stay_within_order_contract():
    """规则表的 target 全集不许超出 order.py 的四态（防归一层偷偷造第五个状态）。"""
    assert {r.target for r in _REF_RULES} <= set(ALL_ORDER_STATUSES)


# ---------------------------------------------------------------------------
# mapping：dig 与金额
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path,expected", [
    ("a", {"b": [{"c": 7}]}),
    ("a.b[0].c", 7),
    ("a.b[1].c", None),         # 越界返回 None，不抛
    ("a.missing", None),
    ("a.b[0].c.d", None),       # 往标量里继续钻，返回 None
])
def test_dig_walks_paths_without_raising(path, expected):
    assert dig({"a": {"b": [{"c": 7}]}}, path) == expected


def test_normalize_amount_keeps_string_form():
    assert normalize_amount("128.50") == "128.50"
    assert normalize_amount(" 128.50 ") == "128.50"


def test_normalize_amount_rejects_missing():
    """零元订单与「读不到金额」是两回事，后者应该让上层停下来。"""
    with pytest.raises(ValueError, match="没给订单金额"):
        normalize_amount(None, platform="refmart")


def test_normalize_amount_never_returns_float():
    assert isinstance(normalize_amount(128.5, platform="refmart"), str)


# ---------------------------------------------------------------------------
# transport
# ---------------------------------------------------------------------------

def test_fake_transport_raises_on_unprepared_request():
    """不兜底成 200 空响应 —— 那会把「URL 拼错了」伪装成「平台没返回数据」。"""
    fake = FakeTransport().expect("GET", "https://x.test/a", body="{}")
    with pytest.raises(KeyError, match="没有预置"):
        fake.request("GET", "https://x.test/b")


def test_fake_transport_records_calls_for_assertions():
    fake = FakeTransport().expect("GET", "https://x.test/a", body="{}")
    fake.request("GET", "https://x.test/a", headers={"H": "v"})
    assert fake.calls[0]["method"] == "GET"
    assert fake.calls[0]["headers"]["H"] == "v"


def test_http_response_header_lookup_is_case_insensitive():
    """各平台头名大小写写法不一致，按字面 key 取会漏。"""
    resp = HttpResponse(200, {"X-Shopify-Shop-Api-Call-Limit": "1/40"}, b"")
    assert resp.header("x-shopify-shop-api-call-limit") == "1/40"
    assert resp.header("missing", "fallback") == "fallback"


def test_redact_url_strips_query_signature():
    """Lazada / Shopee 把签名拼在 query 上，进 traceback 就等于泄密（铁律 6）。"""
    out = redact_url("https://api.lazada.com/rest?app_key=123&sign=DEADBEEF")
    assert "DEADBEEF" not in out and "app_key" not in out
    assert out.startswith("https://api.lazada.com/rest")


def test_redact_url_keeps_plain_url():
    assert redact_url("https://x.test/a/b") == "https://x.test/a/b"


def test_urllib_transport_retries_only_retriable_status(monkeypatch):
    """401 重试只会把同一个错误再犯两遍，还会撞平台风控 —— 所以不重试。"""
    slept: list[float] = []
    calls: list[int] = []

    class _Boom(Exception):
        pass

    transport = UrllibTransport(max_retries=3, sleep=slept.append)

    def _fake_urlopen(req, timeout=None):
        calls.append(1)
        raise _Boom()

    # 401 走 HTTPError 分支：构造一个最小替身，验证「不重试、原样返回」
    import urllib.error

    def _unauth(req, timeout=None):
        calls.append(1)
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", _unauth)
    resp = transport.request("GET", "https://x.test/a")
    assert resp.status == 401
    assert len(calls) == 1 and slept == []


def test_urllib_transport_retries_429_then_returns(monkeypatch):
    import urllib.error
    slept: list[float] = []
    attempts: list[int] = []
    transport = UrllibTransport(max_retries=3, sleep=slept.append)

    def _rate_limited(req, timeout=None):
        attempts.append(1)
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", _rate_limited)
    resp = transport.request("GET", "https://x.test/a")
    assert resp.status == 429
    assert len(attempts) == 3                 # 首次 + 两次重试，到顶后原样返回
    assert len(slept) == 2                    # 只在重试之间睡
    assert slept == [1.0, 2.0]                # 指数退避，且**不带 jitter**（可复现）


def test_urllib_transport_raises_transport_error_on_network_failure(monkeypatch):
    import urllib.error
    transport = UrllibTransport(max_retries=2, sleep=lambda _s: None)

    def _down(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", _down)
    with pytest.raises(TransportError, match="传输失败"):
        transport.request("GET", "https://x.test/a?sign=SECRET")


def test_transport_error_message_has_no_query_secrets(monkeypatch):
    import urllib.error
    transport = UrllibTransport(max_retries=1, sleep=lambda _s: None)
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout=None: (_ for _ in ()).throw(
                            urllib.error.URLError("down")))
    with pytest.raises(TransportError) as err:
        transport.request("GET", "https://x.test/a?sign=DEADBEEF")
    assert "DEADBEEF" not in str(err.value)


# ---------------------------------------------------------------------------
# base：成品流程
# ---------------------------------------------------------------------------

def test_adapter_query_returns_external_order(monkeypatch):
    fake = FakeTransport().expect("GET", _URL, body=_OK_BODY)
    order = _ref(fake, monkeypatch).query("R-1001")
    assert isinstance(order, ExternalOrder)
    assert order.order_id == "R-1001"
    assert order.status == "paid"
    assert order.amount == "128.50"
    assert order.version == 1789482600000
    assert order.updated_at == "2026-09-15T10:30:00-04:00"


def test_adapter_sends_credential_header(monkeypatch):
    fake = FakeTransport().expect("GET", _URL, body=_OK_BODY)
    _ref(fake, monkeypatch).query("R-1001")
    assert fake.calls[0]["headers"]["X-Refmart-Token"] == "tok-secret-value"


def test_adapter_missing_order_raises_key_error(monkeypatch):
    """OrderSystemPort 的契约：查不到抛 KeyError，不返回一个 v1 空订单 ——
    那会把「订单不见了」伪装成「版本一致，放行」。"""
    fake = FakeTransport().expect("GET", _URL, status=404, body='{"code":"not_found"}')
    with pytest.raises(KeyError, match="R-1001"):
        _ref(fake, monkeypatch).query("R-1001")


def test_adapter_error_response_raises_commerce_error(monkeypatch):
    fake = FakeTransport().expect("GET", _URL, status=422,
                                  body='{"code":"invalid_scope","message":"missing read_orders"}')
    with pytest.raises(CommerceError) as err:
        _ref(fake, monkeypatch).query("R-1001")
    assert err.value.status == 422
    assert err.value.code == "invalid_scope"
    assert "missing read_orders" in str(err.value)


def test_adapter_bad_json_raises_instead_of_empty_dict(monkeypatch):
    fake = FakeTransport().expect("GET", _URL, body="<html>502 bad gateway</html>")
    with pytest.raises(CommerceError, match="BAD_JSON"):
        _ref(fake, monkeypatch).query("R-1001")


def test_adapter_non_object_json_raises(monkeypatch):
    fake = FakeTransport().expect("GET", _URL, body="[1,2,3]")
    with pytest.raises(CommerceError, match="BAD_SHAPE"):
        _ref(fake, monkeypatch).query("R-1001")


def test_adapter_unmapped_status_propagates(monkeypatch):
    """归一失败必须冒到调用方，不许在 query() 里被兜底掉。"""
    body = json.loads(_OK_BODY)
    body["order"]["financial_status"] = "authorized"
    fake = FakeTransport().expect("GET", _URL, body=json.dumps(body))
    with pytest.raises(UnmappedOrderStatus):
        _ref(fake, monkeypatch).query("R-1001")


def test_adapter_repr_hides_credentials(monkeypatch):
    fake = FakeTransport()
    text = repr(_ref(fake, monkeypatch))
    assert "tok-secret-value" not in text
    assert "demo.refmart.test" in text          # 账号标识不是密钥，排查要看


def test_missing_credential_raises_distinct_error(monkeypatch):
    """「没带钥匙出门」与「钥匙不对」的排查方向完全相反，不许混成一件事。"""
    monkeypatch.delenv("REFMART_TOKEN", raising=False)
    adapter = _RefAdapter(transport=FakeTransport(), account="demo.refmart.test")
    with pytest.raises(CredentialMissing, match="REFMART_TOKEN"):
        adapter.query("R-1001")


def test_read_credential_rejects_blank(monkeypatch):
    monkeypatch.setenv("BLANK_KEY", "   ")
    with pytest.raises(CredentialMissing):
        read_credential("BLANK_KEY", platform="refmart", purpose="测试")


# ---------------------------------------------------------------------------
# base：子类纪律
# ---------------------------------------------------------------------------

def test_subclass_cannot_override_query():
    """query() 是成品流程，覆盖它等于绕开 404/归一/浮点那三条纪律。"""
    with pytest.raises(TypeError, match="覆盖了 query"):
        class _Bad(CommerceAdapter):
            platform = "bad"
            status_rules = _REF_RULES

            def query(self, order_id):       # noqa: D102
                return None


def test_subclass_without_platform_is_rejected():
    class _NoPlatform(CommerceAdapter):
        status_rules = _REF_RULES

    with pytest.raises(TypeError, match="没声明 platform"):
        _NoPlatform(transport=FakeTransport())


def test_subclass_without_rules_is_rejected():
    """空规则表会对每笔单都抛 —— 那不是「暂时没填」的合法状态，是漏了。"""
    class _NoRules(CommerceAdapter):
        platform = "norules"

    with pytest.raises(TypeError, match="没声明 status_rules"):
        _NoRules(transport=FakeTransport())


def test_adapter_satisfies_order_system_port(monkeypatch):
    """鸭子类型也要真的对得上 —— 装配处会把它直接 register_order_system 进去。"""
    from maos.tools.order import register_order_system, get_order_system, reset_order_systems

    fake = FakeTransport().expect("GET", _URL, body=_OK_BODY)
    adapter = _ref(fake, monkeypatch)
    try:
        register_order_system("refmart-test", adapter)
        assert get_order_system("refmart-test").query("R-1001").order_id == "R-1001"
    finally:
        reset_order_systems()
