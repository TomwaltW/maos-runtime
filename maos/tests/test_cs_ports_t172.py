"""客服前台的两个外部端口 —— ``maos/ingress/cs_ports.py``（p13 · T172）。

契约：review/p13-cs-contracts.md §1.2、§1.4「T172」、§3'。

* 查单一律 ``MockOrderSystem`` 或 ``FakeTransport``（真 ``ShopifyAdapter``），不打网络；
  凭据一律 ``monkeypatch.setenv`` 造一眼看得出是假的值。
* 预检一律用测试台账（``scenarios/custom/ledger.json`` 复制进 tmp，加一行别的租户的单）；
  时钟一律注入。
* 工具侧的订单系统登记表是进程全局的：每条用例换一张空表（autouse），用完原样换回，
  不串别的测试文件的账本。
"""

from __future__ import annotations

import json
import logging
import math
import os
from pathlib import Path

import pytest

from maos.core.store import SqliteStore
from maos.domain.cs import ports as P
from maos.domain.cs.ports import Binding, LookupResult, PrecheckResult
from maos.flows import custom_case
from maos.ingress import cs_ports
from maos.ingress import router as router_mod
from maos.ingress.contracts import InboundMessage
from maos.tools import order as order_tools
from maos.tools.commerce import CommerceAdapter, FakeTransport, UrllibTransport
from maos.tools.commerce import shopify

ROOT = Path(__file__).resolve().parents[2]
DEMO_LEDGER = ROOT / "scenarios" / "custom" / "ledger.json"

TENANT = "tnt-demo"
PLAN = "cs:csc-t172aaaaaaaaaaaa"
TURN = "csc-t172aaaaaaaaaaaa-t0001"
NOW = "2026-07-10T00:00:00+00:00"

#: 账本上**别的**订单号：只该出现在异常原文里，一个字都不许出 cs_ports。
SECRET_A = "ZZ-SECRET-7788"
SECRET_B = "YY-OTHER-4455"

SHOP = "t172-shop.myshopify.com"
SHOP_ORDER_ID = 450789469
SHOP_URL = f"https://{SHOP}/admin/api/{shopify.API_VERSION}/orders/{SHOP_ORDER_ID}.json"
FAKE_TOKEN = "fake-t172-shopify-token-DO-NOT-USE"

#: 任务派单点名的六个平台（动态发现必须全认出来）。
PLATFORMS_T172 = ("shopify", "woocommerce", "ebay", "amazon_sp", "tiktok_shop", "shopee")


# ---------------------------------------------------------------------------
# 夹具与辅助
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_order_registry_t172(monkeypatch):
    # 换一张空的全局登记表；monkeypatch 在用例结束时把原来那张（连同别人登记的系统）换回来。
    monkeypatch.setattr(order_tools, "_SYSTEMS", {})
    yield


def _store_t172(path: str = ":memory:") -> SqliteStore:
    store = SqliteStore(path)
    store.init_schema()
    return store


def _binding_t172(query_key: str = "A1001", *, system_name: str = "demo-orders",
                  display_no: str = "A1001") -> Binding:
    return Binding(tenant_id=TENANT, channel="wechat_kf", external_userid="eval-t172",
                   display_no=display_no, system_name=system_name, query_key=query_key,
                   source=P.BINDING_TEST)


def _mock_t172() -> order_tools.MockOrderSystem:
    system = order_tools.MockOrderSystem()
    stamp = "2026-09-20T10:00:00+00:00"
    system.ext_order("A1001", 1, "paid", "10.00", stamp)
    system.ext_order("B2002", 2, "shipped", "20.00", stamp)
    system.ext_order("C3003", 3, "cancelled", "30.00", stamp)
    system.ext_order("D4004", 1, "paid", "40.00", stamp)
    system.amend("D4004", status="amended", updated_at="2026-09-21T10:00:00+00:00")
    system.ext_order(SECRET_A, 1, "paid", "50.00", stamp)
    system.ext_order(SECRET_B, 1, "paid", "60.00", stamp)
    return system


def _tool_rows_t172(store: SqliteStore) -> list[dict]:
    """全库的 ToolInvoked 行（不按 plan 过滤 —— plan_id 写错的行也要数进来）。"""
    rows = store._conn.execute(
        "SELECT plan_id, task_id, trace_id, detail FROM event_log "
        "WHERE event_type='ToolInvoked' ORDER BY seq").fetchall()
    return [dict(r, detail=json.loads(r["detail"])) for r in rows]


def _row_counts_t172(store: SqliteStore) -> dict[str, int]:
    names = [r[0] for r in store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]
    return {n: store._conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}


def _assert_one_tool_row_t172(store: SqliteStore, *, status: str) -> dict:
    rows = _tool_rows_t172(store)
    assert len(rows) == 1, rows
    row = rows[0]
    assert (row["plan_id"], row["task_id"], row["trace_id"]) == (PLAN, TURN, "")
    assert row["detail"]["tool"] == "order.query"
    assert row["detail"]["status"] == status
    return row


def _shop_order_t172(**overrides) -> dict:
    order = {"id": SHOP_ORDER_ID, "order_number": 1001, "total_price": "598.94",
             "financial_status": "paid", "fulfillment_status": None, "cancelled_at": None,
             "updated_at": "2026-09-15T10:30:00-04:00"}
    order.update(overrides)
    return order


def _shop_lookup_t172(fake: FakeTransport) -> cs_ports.CommerceOrderLookup:
    return cs_ports.CommerceOrderLookup(
        {"shop-main": shopify.ShopifyAdapter(transport=fake, account=SHOP)})


@pytest.fixture
def shop_creds_t172(monkeypatch):
    monkeypatch.setenv("SHOPIFY_SHOP_DOMAIN", SHOP)
    monkeypatch.setenv("SHOPIFY_ACCESS_TOKEN", FAKE_TOKEN)


class _RaisingSystem_t172:
    """query 抛指定异常的订单系统。异常原文里故意列着别的订单号。"""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def query(self, order_id: str):
        raise self.exc


class _Reply_t172:
    def __init__(self, data: dict) -> None:
        self.data = data

    def to_dict(self) -> dict:
        return dict(self.data)


class _WeirdSystem_t172:
    """返回一个不在四态里的状态（例如平台新加的 refunded）。"""

    def query(self, order_id: str):
        return _Reply_t172({"order_id": order_id, "version": 1, "status": "refunded",
                            "amount": "1.00", "updated_at": "2026-09-20T10:00:00+00:00"})


@pytest.fixture
def ledger_t172(tmp_path) -> Path:
    """测试台账：存量演示台账原样复制，再加一行**别的租户**的单。"""
    data = json.loads(DEMO_LEDGER.read_text(encoding="utf-8"))
    other = dict(data["order_snapshot"][0], tenant_id="tnt-other", order_id="ORD-T172-OTHER")
    data["order_snapshot"].append(other)
    path = tmp_path / "ledger_t172.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _precheck_t172(ledger: Path, **kw) -> PrecheckResult:
    args = {"tenant_id": TENANT, "order_no": "ORD-2026-0002", "reason_text": "七天无理由退货",
            "now": NOW}
    args.update(kw)
    return cs_ports.LedgerRefundPrecheck(ledger, ledger_tenant=TENANT).precheck(**args)


# ---------------------------------------------------------------------------
# 形状
# ---------------------------------------------------------------------------

def test_implementations_satisfy_the_frozen_protocols_t172(ledger_t172):
    assert isinstance(cs_ports.CommerceOrderLookup({}), P.OrderLookup)
    assert isinstance(cs_ports.LedgerRefundPrecheck(ledger_t172, ledger_tenant=TENANT),
                      P.RefundPrecheck)
    assert set(cs_ports.REFUSED_WHYS) == {
        "tenant_mismatch", "order_not_in_ledger", "reason_ambiguous", "reason_missing",
        "clock_missing", "preflight_error"}


def test_transport_timeout_must_be_positive_t172():
    for bad in (0, -1, math.inf, math.nan):
        with pytest.raises(ValueError):
            cs_ports.CommerceOrderLookup({}, transport_timeout_s=bad)
    assert cs_ports.CommerceOrderLookup({}).transport_timeout_s == 5.0


# ---------------------------------------------------------------------------
# 查单 · MockOrderSystem
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key,status,version", [
    ("A1001", "paid", 1), ("B2002", "shipped", 2), ("C3003", "cancelled", 3)])
def test_mock_lookup_ok_writes_exactly_one_tool_row_t172(key, status, version):
    store = _store_t172()
    lk = cs_ports.CommerceOrderLookup({"demo-orders": _mock_t172()})
    res = lk.lookup(store, _binding_t172(key), plan_id=PLAN, task_id=TURN)
    assert res == LookupResult(outcome="ok", system_name="demo-orders", query_key=key,
                               status=status, version=version,
                               updated_at="2026-09-20T10:00:00+00:00", error_kind="")
    assert res.status in P.ORDER_STATUS_WORDING["zh"]
    _assert_one_tool_row_t172(store, status="ok")


def test_mock_lookup_amended_has_no_status_t172():
    store = _store_t172()
    lk = cs_ports.CommerceOrderLookup({"demo-orders": _mock_t172()})
    res = lk.lookup(store, _binding_t172("D4004"), plan_id=PLAN, task_id=TURN)
    assert (res.outcome, res.status, res.version, res.error_kind) == ("amended", "", 2, "")
    _assert_one_tool_row_t172(store, status="ok")


def test_mock_lookup_not_found_t172():
    store = _store_t172()
    lk = cs_ports.CommerceOrderLookup({"demo-orders": _mock_t172()})
    res = lk.lookup(store, _binding_t172("Q-MISSING-0001"), plan_id=PLAN, task_id=TURN)
    assert (res.outcome, res.status, res.error_kind) == ("not_found", "", "KeyError")
    _assert_one_tool_row_t172(store, status="failed")


def test_lookup_survives_run_payload_style_registry_reset_t172():
    """``custom_case.run_payload`` 每跑一次都 reset_order_systems()：CS 查单前自己登记回来。"""
    store = _store_t172()
    lk = cs_ports.CommerceOrderLookup({"demo-orders": _mock_t172()})
    assert lk.lookup(store, _binding_t172("A1001"), plan_id=PLAN, task_id=TURN).outcome == "ok"

    order_tools.reset_order_systems()                    # 模拟 run_payload 清表
    res = lk.lookup(store, _binding_t172("B2002"), plan_id=PLAN, task_id=TURN)
    assert (res.outcome, res.status) == ("ok", "shipped")
    assert len(_tool_rows_t172(store)) == 2


def test_lookup_after_reset_before_first_call_t172():
    lk = cs_ports.CommerceOrderLookup({"demo-orders": _mock_t172()})
    order_tools.reset_order_systems()
    store = _store_t172()
    res = lk.lookup(store, _binding_t172("C3003"), plan_id=PLAN, task_id=TURN)
    assert (res.outcome, res.status) == ("ok", "cancelled")
    _assert_one_tool_row_t172(store, status="ok")


def test_lookup_uses_its_own_registry_name_and_leaves_the_refund_one_alone_t172():
    """退款链路登记的是裸名；CS 用 ``cs:`` 前缀，互不顶掉。"""
    refund_side = order_tools.MockOrderSystem()
    order_tools.register_order_system("demo-orders", refund_side)   # run_payload 那一侧
    mine = _mock_t172()
    lk = cs_ports.CommerceOrderLookup({"demo-orders": mine})
    res = lk.lookup(_store_t172(), _binding_t172("A1001"), plan_id=PLAN, task_id=TURN)
    assert res.outcome == "ok"            # refund_side 是空账本，查到了说明没走裸名
    assert order_tools.get_order_system("demo-orders") is refund_side
    assert order_tools.get_order_system("cs:demo-orders") is mine
    assert cs_ports.cs_system_name("demo-orders") == "cs:demo-orders"


# ---------------------------------------------------------------------------
# 查单 · FakeTransport + 真 ShopifyAdapter
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("overrides,status", [
    ({}, "paid"),
    ({"fulfillment_status": "fulfilled"}, "shipped"),
    ({"cancelled_at": "2026-09-15T11:00:00-04:00"}, "cancelled"),
])
def test_shopify_lookup_ok_t172(shop_creds_t172, overrides, status):
    fake = FakeTransport().expect("GET", SHOP_URL,
                                  body=json.dumps({"order": _shop_order_t172(**overrides)}))
    store = _store_t172()
    res = _shop_lookup_t172(fake).lookup(
        store, _binding_t172(str(SHOP_ORDER_ID), system_name="shop-main", display_no="1001"),
        plan_id=PLAN, task_id=TURN)
    assert (res.outcome, res.status, res.system_name) == ("ok", status, "shop-main")
    assert res.version > 0 and res.updated_at == "2026-09-15T10:30:00-04:00"
    assert len(fake.calls) == 1
    _assert_one_tool_row_t172(store, status="ok")


def test_shopify_unmapped_status_comes_from_the_real_adapter_t172(shop_creds_t172):
    """refunded 不在 Shopify 规则表里：真 CommerceAdapter.query 抛 UnmappedOrderStatus。"""
    fake = FakeTransport().expect("GET", SHOP_URL, body=json.dumps(
        {"order": _shop_order_t172(financial_status="refunded")}))
    store = _store_t172()
    res = _shop_lookup_t172(fake).lookup(
        store, _binding_t172(str(SHOP_ORDER_ID), system_name="shop-main"),
        plan_id=PLAN, task_id=TURN)
    assert (res.outcome, res.status, res.error_kind) == (
        "unmapped_status", "", "UnmappedOrderStatus")
    _assert_one_tool_row_t172(store, status="failed")


def test_shopify_404_is_not_found_t172(shop_creds_t172):
    fake = FakeTransport().expect("GET", SHOP_URL, status=404, body='{"errors":"Not Found"}')
    store = _store_t172()
    res = _shop_lookup_t172(fake).lookup(
        store, _binding_t172(str(SHOP_ORDER_ID), system_name="shop-main"),
        plan_id=PLAN, task_id=TURN)
    assert (res.outcome, res.error_kind) == ("not_found", "KeyError")
    _assert_one_tool_row_t172(store, status="failed")


def test_shopify_500_is_platform_error_t172(shop_creds_t172):
    fake = FakeTransport().expect("GET", SHOP_URL, status=500,
                                  body=f'{{"errors":"backend down near {SECRET_A}"}}')
    store = _store_t172()
    res = _shop_lookup_t172(fake).lookup(
        store, _binding_t172(str(SHOP_ORDER_ID), system_name="shop-main"),
        plan_id=PLAN, task_id=TURN)
    assert (res.outcome, res.error_kind) == ("platform_error", "CommerceError")
    assert SECRET_A not in repr(res)
    _assert_one_tool_row_t172(store, status="failed")


def test_shopify_missing_credentials_is_platform_error_t172(monkeypatch):
    monkeypatch.delenv("SHOPIFY_SHOP_DOMAIN", raising=False)
    monkeypatch.delenv("SHOPIFY_ACCESS_TOKEN", raising=False)
    fake = FakeTransport()
    store = _store_t172()
    res = _shop_lookup_t172(fake).lookup(
        store, _binding_t172(str(SHOP_ORDER_ID), system_name="shop-main"),
        plan_id=PLAN, task_id=TURN)
    assert (res.outcome, res.error_kind) == ("platform_error", "CredentialMissing")
    assert fake.calls == []
    _assert_one_tool_row_t172(store, status="failed")


# ---------------------------------------------------------------------------
# 查单 · 配置错与平台错
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("system_name,query_key", [
    ("", "A1001"), ("demo-orders", ""), ("   ", "A1001"), ("demo-orders", "  "),
    ("not-injected", "A1001"),
])
def test_misconfigured_binding_never_calls_invoke_tool_t172(monkeypatch, system_name,
                                                            query_key):
    calls: list = []
    monkeypatch.setattr(cs_ports, "invoke_tool", lambda *a, **kw: calls.append((a, kw)))
    store = _store_t172()
    lk = cs_ports.CommerceOrderLookup({"demo-orders": _mock_t172()})
    res = lk.lookup(store, _binding_t172(query_key, system_name=system_name),
                    plan_id=PLAN, task_id=TURN)
    assert (res.outcome, res.status, res.error_kind) == ("system_misconfigured", "", "")
    assert calls == []
    assert _tool_rows_t172(store) == []


def test_non_binding_input_is_misconfigured_not_a_crash_t172():
    res = cs_ports.CommerceOrderLookup({"demo-orders": _mock_t172()}).lookup(
        _store_t172(), None, plan_id=PLAN, task_id=TURN)
    assert res.outcome == "system_misconfigured"


def test_other_lookup_error_is_misconfigured_t172():
    store = _store_t172()
    lk = cs_ports.CommerceOrderLookup(
        {"demo-orders": _RaisingSystem_t172(LookupError("no such backend"))})
    res = lk.lookup(store, _binding_t172("A1001"), plan_id=PLAN, task_id=TURN)
    assert (res.outcome, res.error_kind) == ("system_misconfigured", "LookupError")
    _assert_one_tool_row_t172(store, status="failed")


@pytest.mark.parametrize("exc,kind", [
    (ValueError("bad amount"), "ValueError"),
    (RuntimeError("socket closed"), "RuntimeError"),
    (TimeoutError("slow"), "TimeoutError"),
])
def test_other_errors_are_platform_error_t172(exc, kind):
    store = _store_t172()
    lk = cs_ports.CommerceOrderLookup({"demo-orders": _RaisingSystem_t172(exc)})
    res = lk.lookup(store, _binding_t172("A1001"), plan_id=PLAN, task_id=TURN)
    assert (res.outcome, res.error_kind) == ("platform_error", kind)
    _assert_one_tool_row_t172(store, status="failed")


def test_status_outside_the_four_is_platform_error_t172():
    store = _store_t172()
    lk = cs_ports.CommerceOrderLookup({"demo-orders": _WeirdSystem_t172()})
    res = lk.lookup(store, _binding_t172("A1001"), plan_id=PLAN, task_id=TURN)
    assert (res.outcome, res.status, res.error_kind) == (
        "platform_error", "", "OrderReplyInvalid")
    _assert_one_tool_row_t172(store, status="ok")


def test_broken_store_does_not_escape_t172():
    class _BrokenStore_t172:
        def append_event_log(self, row):
            raise RuntimeError("disk full")

    res = cs_ports.CommerceOrderLookup({"demo-orders": _mock_t172()}).lookup(
        _BrokenStore_t172(), _binding_t172("A1001"), plan_id=PLAN, task_id=TURN)
    assert (res.outcome, res.error_kind) == ("platform_error", "RuntimeError")


# ---------------------------------------------------------------------------
# 查单 · 异常原文不出 cs_ports
# ---------------------------------------------------------------------------

def _assert_no_leak_t172(res: LookupResult, caplog, *fragments: str) -> None:
    blob = repr(res) + "|".join(str(v) for v in vars(res).values())
    for frag in fragments:
        assert frag not in blob, frag
        assert frag not in caplog.text, frag
    for rec in caplog.records:
        assert rec.exc_info is None


def test_keyerror_text_listing_other_orders_does_not_leak_t172(caplog):
    caplog.set_level(logging.DEBUG)
    store = _store_t172()
    lk = cs_ports.CommerceOrderLookup({"demo-orders": _mock_t172()})
    res = lk.lookup(store, _binding_t172("Q-MISSING-0001"), plan_id=PLAN, task_id=TURN)
    assert (res.outcome, res.error_kind) == ("not_found", "KeyError")
    # MockOrderSystem 的 KeyError 原文里列着「已登记：[…全部订单号…]」。
    _assert_no_leak_t172(res, caplog, SECRET_A, SECRET_B, "B2002", "已登记", "没有这笔单")
    # 日志里查单键只打码后的末 4 位，不打全。
    assert "Q-MISSING-0001" not in caplog.text
    assert cs_ports.mask_query_key("Q-MISSING-0001") in caplog.text
    # 存量行为（DECISIONS / BACKLOG task-t172 已记，p14 处理）：invoke_tool 自己把
    # 「类名: 原文」写进 ToolInvoked.detail.error。这里只钉住它确实是失败行。
    _assert_one_tool_row_t172(store, status="failed")


def test_custom_exception_text_does_not_leak_t172(caplog):
    caplog.set_level(logging.DEBUG)
    msg = f"平台说：别的单 {SECRET_A} 与 {SECRET_B} 都在这一页"
    for exc in (RuntimeError(msg), ValueError(msg), KeyError(msg), LookupError(msg)):
        caplog.clear()
        res = cs_ports.CommerceOrderLookup(
            {"demo-orders": _RaisingSystem_t172(exc)}).lookup(
                _store_t172(), _binding_t172("A1001"), plan_id=PLAN, task_id=TURN)
        assert res.error_kind == type(exc).__name__
        _assert_no_leak_t172(res, caplog, SECRET_A, SECRET_B, "平台说")


def test_unmapped_status_text_does_not_leak_t172(shop_creds_t172, caplog):
    caplog.set_level(logging.DEBUG)
    fake = FakeTransport().expect("GET", SHOP_URL, body=json.dumps(
        {"order": _shop_order_t172(financial_status="refunded")}))
    res = _shop_lookup_t172(fake).lookup(
        _store_t172(), _binding_t172(str(SHOP_ORDER_ID), system_name="shop-main"),
        plan_id=PLAN, task_id=TURN)
    assert res.outcome == "unmapped_status"
    _assert_no_leak_t172(res, caplog, "refunded", "没有命中任何规则", FAKE_TOKEN)


@pytest.mark.parametrize("key,outcome", [("B2002", "ok"), ("D4004", "amended")])
def test_success_log_masks_query_key_t172(caplog, key, outcome):
    """查单成功（最常见的那条路）的日志同样只打打码后的查单键，不打全。"""
    lk = cs_ports.CommerceOrderLookup({"demo-orders": _mock_t172()})
    store = _store_t172()
    caplog.set_level(logging.DEBUG)
    caplog.clear()                    # 造账本时 MockOrderSystem.amend 自己打的「外部改单」不算
    res = lk.lookup(store, _binding_t172(key), plan_id=PLAN, task_id=TURN)
    assert res.outcome == outcome
    mine = [r for r in caplog.records if r.name == cs_ports.log.name]
    assert mine, "成功路径也该落一行查单日志"
    assert key not in caplog.text
    assert cs_ports.mask_query_key(key) in caplog.text


def test_mask_query_key_t172():
    assert cs_ports.mask_query_key("ORD-2026-0001") == "…0001"
    assert cs_ports.mask_query_key("A1001") == "…1001"
    assert cs_ports.mask_query_key("1234") == "****"
    assert cs_ports.mask_query_key("") == ""


# ---------------------------------------------------------------------------
# 退款预检
# ---------------------------------------------------------------------------

def test_precheck_ok_and_command_line_parses_as_refund_t172(ledger_t172):
    res = _precheck_t172(ledger_t172)
    assert res.ok is True and res.refused_why == ""
    assert (res.decision, res.rule_ref, res.reason_code) == (
        "approve", "AS-001@v1", "no_reason_return")
    assert res.command_line == "/refund ORD-2026-0002 no_reason_return"
    assert res.summary and "\n" not in res.summary

    cmd = router_mod.Command.parse(res.command_line)
    assert cmd is not None and cmd.verb == router_mod.CMD_REFUND
    assert cmd.args == ["ORD-2026-0002", "no_reason_return"]
    # 参数顺序与 handle_refund 一致：args[0] 订单号、args[1] 诉求类型（英文 code 也认）。
    assert router_mod._load_run_requests()._reason_code(cmd.args[1]) == res.reason_code


def test_precheck_matches_the_router_preflight_t172(ledger_t172):
    """与 ``/refund`` 同一套口径：同一张台账、同一个时刻，裁定与依据逐字相同。"""
    rr = router_mod._load_run_requests()
    ledger = json.loads(ledger_t172.read_text(encoding="utf-8"))
    for order, text, code in [("ORD-2026-0001", "东西坏了", "quality_defect"),
                              ("ORD-2026-0002", "我买错型号了", "no_reason_return")]:
        res = _precheck_t172(ledger_t172, order_no=order, reason_text=text)
        checked = router_mod.preflight(rr.build_case(ledger, {
            "order_id": order, "reason": code, "amount": None, "requested_at": NOW}))
        assert res.ok and res.reason_code == code
        assert res.decision == checked["decision"]
        assert res.rule_ref == (checked["deciding_rule"] or "")


def test_command_line_is_accepted_by_the_real_refund_handler_t172(ledger_t172):
    res = _precheck_t172(ledger_t172)
    router = router_mod.IngressRouter({}, store=_store_t172(), ledger_path=ledger_t172,
                                      approvers=lambda: frozenset())
    cmd = router_mod.Command.parse(res.command_line)
    msg = InboundMessage(channel="feishu", chat_id="oc_t172", sender="ou_t172",
                         text=res.command_line, msg_id="m-t172")
    text = router.handle_refund(msg, cmd.args)       # 语法不对会抛 CommandError
    assert text.startswith("预检 · ORD-2026-0002") and "RC-ORD-2026-0002" in text


@pytest.mark.parametrize("kw,why", [
    ({"tenant_id": "tnt-b"}, "tenant_mismatch"),
    ({"tenant_id": ""}, "tenant_mismatch"),
    ({"order_no": "ORD-9999-0000"}, "order_not_in_ledger"),
    ({"order_no": "ORD-T172-OTHER"}, "order_not_in_ledger"),      # 台账里有，但是别的租户
    ({"order_no": "ORD-2026-0002 x"}, "order_not_in_ledger"),     # 拼进 /refund 会拆成两参
    ({"order_no": ""}, "order_not_in_ledger"),
    ({"reason_text": "质量问题，而且还发错货了"}, "reason_ambiguous"),
    ({"reason_text": "我想退款"}, "reason_missing"),
    ({"reason_text": ""}, "reason_missing"),
    ({"now": ""}, "clock_missing"),
    ({"now": None}, "clock_missing"),
    ({"now": "明天吧"}, "preflight_error"),
])
def test_precheck_refusals_t172(ledger_t172, kw, why):
    res = _precheck_t172(ledger_t172, **kw)
    assert res == PrecheckResult(ok=False, refused_why=why)


def test_precheck_preflight_error_when_preflight_raises_t172(ledger_t172, monkeypatch):
    def boom(payload):
        raise RuntimeError(f"policy table broken near {SECRET_A}")

    monkeypatch.setattr(router_mod, "preflight", boom)
    res = _precheck_t172(ledger_t172)
    assert res == PrecheckResult(ok=False, refused_why="preflight_error")


def test_precheck_preflight_error_when_ledger_unreadable_t172(tmp_path, ledger_t172):
    path = tmp_path / "no-such-ledger-yet.json"
    pc = cs_ports.LedgerRefundPrecheck(path, ledger_tenant=TENANT)
    args = {"tenant_id": TENANT, "order_no": "ORD-2026-0002", "reason_text": "七天无理由退货",
            "now": NOW}
    assert pc.precheck(**args) == PrecheckResult(ok=False, refused_why="preflight_error")
    # 失败不缓存：台账这时才到位，**同一个实例**的下一次预检照样读得出来。
    path.write_bytes(ledger_t172.read_bytes())
    res = pc.precheck(**args)
    assert (res.ok, res.decision, res.rule_ref) == (True, "approve", "AS-001@v1")


@pytest.mark.parametrize("table", custom_case.REQUIRED_TABLES)
def test_precheck_refuses_a_ledger_the_refund_side_cannot_load_t172(tmp_path, table):
    """与 ``IngressRouter.ledger()`` 同一个读法：少一张外部快照表，/refund 那边读不出来，
    预检也不许给出一个 ok（例如缺 policy_rule 时的「基线驳回、依据为空」）。"""
    data = json.loads(DEMO_LEDGER.read_text(encoding="utf-8"))
    del data[table]
    path = tmp_path / f"ledger_no_{table}_t172.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(custom_case.CaseFileError):
        custom_case.load(path, require_case=False)
    assert _precheck_t172(path) == PrecheckResult(ok=False, refused_why="preflight_error")


@pytest.mark.parametrize("version", [2, 1])
def test_precheck_refuses_an_order_number_shared_with_another_tenant_t172(tmp_path, version):
    """build_case 按单号在全表取最高版本、不看租户：别的租户有同号的行（版本更高时裁定会
    按那一行和它的政策算）→ 失败即关，按不在台账里拒。"""
    data = json.loads(DEMO_LEDGER.read_text(encoding="utf-8"))
    mine = next(o for o in data["order_snapshot"] if o["order_id"] == "ORD-2026-0002")
    base = tmp_path / "ledger_single_tenant_t172.json"
    base.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert _precheck_t172(base).ok is True                  # 对照：只有本租户那一行时是 ok

    data["order_snapshot"].append(dict(mine, tenant_id="tnt-other", version=version))
    path = tmp_path / f"ledger_shared_no_v{version}_t172.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert _precheck_t172(path) == PrecheckResult(ok=False, refused_why="order_not_in_ledger")


def test_precheck_never_raises_on_junk_input_t172(ledger_t172):
    pc = cs_ports.LedgerRefundPrecheck(ledger_t172, ledger_tenant=TENANT)
    res = pc.precheck(tenant_id=None, order_no=None, reason_text=None, now=None)
    assert res.ok is False and res.refused_why in cs_ports.REFUSED_WHYS


def test_precheck_writes_nothing_and_runs_nothing_t172(ledger_t172, tmp_path, monkeypatch):
    """不建 Ticket、不写任何库、不调任何会改状态的函数。"""
    from maos.flows import custom_case

    def _forbidden(*a, **kw):
        raise AssertionError("预检碰了会改状态的函数")

    monkeypatch.setattr(custom_case, "run_payload", _forbidden)
    monkeypatch.setattr(router_mod._load_run_requests(), "run_payload", _forbidden)
    monkeypatch.setattr(router_mod, "_default_runner", _forbidden)
    monkeypatch.setattr(router_mod.IngressRouter, "handle_refund", _forbidden)
    monkeypatch.setattr(router_mod.IngressRouter, "handle_execute", _forbidden)

    db = tmp_path / "desk_t172.sqlite"
    store = _store_t172(str(db))
    lk = cs_ports.CommerceOrderLookup({"demo-orders": _mock_t172()})
    assert lk.lookup(store, _binding_t172("A1001"), plan_id=PLAN, task_id=TURN).outcome == "ok"
    before_rows = _row_counts_t172(store)
    before_files = sorted(p.name for p in tmp_path.iterdir())
    before_ledger = ledger_t172.read_bytes()

    assert _precheck_t172(ledger_t172).ok is True
    assert _precheck_t172(ledger_t172, reason_text="我想退款").ok is False

    assert _row_counts_t172(store) == before_rows
    assert sorted(p.name for p in tmp_path.iterdir()) == before_files
    assert ledger_t172.read_bytes() == before_ledger


def test_match_reason_codes_t172():
    assert cs_ports.match_reason_codes("七天无理由退货") == {"no_reason_return"}
    assert cs_ports.match_reason_codes("买错型号了") == {"no_reason_return"}
    assert cs_ports.match_reason_codes("quality_defect") == {"quality_defect"}
    assert cs_ports.match_reason_codes("坏了，还错发了") == {"quality_defect", "wrong_item"}
    assert cs_ports.match_reason_codes("想退") == frozenset()
    assert cs_ports.match_reason_codes(None) == frozenset()


# ---------------------------------------------------------------------------
# 装配：build_order_lookup_from_env
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("env", [{}, {"MAOS_CS_ORDER_SYSTEMS": ""},
                                 {"MAOS_CS_ORDER_SYSTEMS": "   "}])
def test_env_unset_returns_none_t172(env):
    assert cs_ports.build_order_lookup_from_env(env, ledger_path=DEMO_LEDGER) is None


def test_env_demo_builds_mock_from_ledger_t172(ledger_t172):
    lk = cs_ports.build_order_lookup_from_env({"MAOS_CS_ORDER_SYSTEMS": "demo"},
                                              ledger_path=ledger_t172)
    assert isinstance(lk, cs_ports.CommerceOrderLookup)
    assert lk.system_names == (order_tools.DEFAULT_ORDER_SYSTEM,)
    assert isinstance(lk.system(order_tools.DEFAULT_ORDER_SYSTEM), order_tools.MockOrderSystem)
    store = _store_t172()
    order_tools.reset_order_systems()
    res = lk.lookup(store, _binding_t172("ORD-2026-0001"), plan_id=PLAN, task_id=TURN)
    # 存量台账没有状态列 → paid；修改时刻取 paid_at。
    assert (res.outcome, res.status, res.version, res.updated_at) == (
        "ok", "paid", 1, "2026-07-01T10:00:00+00:00")
    _assert_one_tool_row_t172(store, status="ok")


def test_env_demo_maps_a_ledger_status_field_t172(tmp_path):
    data = json.loads(DEMO_LEDGER.read_text(encoding="utf-8"))
    data["order_snapshot"][0]["status"] = "shipped"
    later = dict(data["order_snapshot"][1], version=2, status="cancelled",
                 updated_at="2026-09-01T00:00:00+00:00")
    data["order_snapshot"].append(later)
    path = tmp_path / "ledger_status_t172.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    lk = cs_ports.build_order_lookup_from_env({"MAOS_CS_ORDER_SYSTEMS": "demo"},
                                              ledger_path=path)
    store = _store_t172()
    first = lk.lookup(store, _binding_t172("ORD-2026-0001"), plan_id=PLAN, task_id=TURN)
    second = lk.lookup(store, _binding_t172("ORD-2026-0002"), plan_id=PLAN, task_id=TURN)
    assert (first.outcome, first.status) == ("ok", "shipped")
    assert (second.outcome, second.status, second.version, second.updated_at) == (
        "ok", "cancelled", 2, "2026-09-01T00:00:00+00:00")


def test_env_demo_bad_config_raises_with_key_names_t172(tmp_path):
    with pytest.raises(ValueError, match="MAOS_CS_ORDER_SYSTEMS"):
        cs_ports.build_order_lookup_from_env({"MAOS_CS_ORDER_SYSTEMS": "demo"})
    with pytest.raises(ValueError, match="ledger_path"):
        cs_ports.build_order_lookup_from_env({"MAOS_CS_ORDER_SYSTEMS": "demo"},
                                             ledger_path=tmp_path / "missing.json")
    data = json.loads(DEMO_LEDGER.read_text(encoding="utf-8"))
    data["order_snapshot"][0]["status"] = "sk-FAKE-T172-delivered"
    path = tmp_path / "bad_status_t172.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match=r"order_snapshot\[0\]\.status") as info:
        cs_ports.build_order_lookup_from_env({"MAOS_CS_ORDER_SYSTEMS": "demo"},
                                             ledger_path=path)
    assert "sk-FAKE-T172" not in str(info.value)


def test_commerce_platforms_are_discovered_t172():
    found = cs_ports.commerce_platforms()
    assert set(PLATFORMS_T172) <= set(found)
    for name, cls in found.items():
        assert issubclass(cls, CommerceAdapter) and cls.platform == name


@pytest.mark.parametrize("platform", PLATFORMS_T172)
def test_env_json_builds_each_platform_without_reading_credentials_t172(monkeypatch,
                                                                        platform):
    # 构造期不读凭据：把进程里可能有的平台变量全部拿掉，照样构造得出来。
    for key in list(os.environ):
        if key.split("_")[0] in {"SHOPIFY", "WOO", "EBAY", "AMAZON", "SPAPI", "TIKTOK",
                                 "SHOPEE", "LWA"}:
            monkeypatch.delenv(key, raising=False)
    env = {"MAOS_CS_ORDER_SYSTEMS": json.dumps({"shop-x": {"platform": platform,
                                                           "account": "acct-t172"}})}
    lk = cs_ports.build_order_lookup_from_env(env)
    adapter = lk.system("shop-x")
    assert adapter.platform == platform and adapter.account == "acct-t172"
    assert isinstance(adapter.transport, UrllibTransport)
    assert adapter.transport.timeout == cs_ports.DEFAULT_TRANSPORT_TIMEOUT_S == 5.0
    assert adapter.transport.max_retries == cs_ports.TRANSPORT_MAX_RETRIES == 2
    assert lk.transport_timeout_s == 5.0


def test_env_json_shopify_end_to_end_with_fake_transport_t172(monkeypatch):
    monkeypatch.delenv("SHOPIFY_SHOP_DOMAIN", raising=False)
    monkeypatch.delenv("SHOPIFY_ACCESS_TOKEN", raising=False)
    env = {"MAOS_CS_ORDER_SYSTEMS": json.dumps({"shop-main": {"platform": "shopify",
                                                              "account": SHOP}})}
    lk = cs_ports.build_order_lookup_from_env(env)     # 凭据还没配：构造照样成功
    fake = FakeTransport().expect("GET", SHOP_URL, body=json.dumps(
        {"order": _shop_order_t172(fulfillment_status="fulfilled")}))
    lk.system("shop-main").transport = fake            # 测试不打网络
    binding = _binding_t172(str(SHOP_ORDER_ID), system_name="shop-main")

    store = _store_t172()
    res = lk.lookup(store, binding, plan_id=PLAN, task_id=TURN)
    assert (res.outcome, res.error_kind) == ("platform_error", "CredentialMissing")

    monkeypatch.setenv("SHOPIFY_SHOP_DOMAIN", SHOP)     # 凭据在调用时读
    monkeypatch.setenv("SHOPIFY_ACCESS_TOKEN", FAKE_TOKEN)
    res = lk.lookup(store, binding, plan_id=PLAN, task_id=TURN)
    assert (res.outcome, res.status) == ("ok", "shipped")
    assert len(_tool_rows_t172(store)) == 2


@pytest.mark.parametrize("platform", PLATFORMS_T172)
def test_env_json_same_platform_twice_is_refused_t172(platform):
    """店铺身份与凭据读进程级环境变量：同平台第二家会查到第一家的店 → 装配期就拒。"""
    raw = json.dumps({"store-a": {"platform": platform, "account": "sk-FAKE-T172-SECRET-A"},
                      "store-b": {"platform": platform, "account": "sk-FAKE-T172-SECRET-B"}})
    with pytest.raises(ValueError) as info:
        cs_ports.build_order_lookup_from_env({"MAOS_CS_ORDER_SYSTEMS": raw})
    msg = str(info.value)
    assert "store-a" in msg and "store-b" in msg
    assert "sk-FAKE-T172" not in msg
    assert info.value.__cause__ is None


def test_env_json_different_platforms_build_side_by_side_t172():
    raw = json.dumps({"store-a": {"platform": "shopify", "account": "a.myshopify.com"},
                      "store-b": {"platform": "woocommerce", "account": "b.example"}})
    lk = cs_ports.build_order_lookup_from_env({"MAOS_CS_ORDER_SYSTEMS": raw})
    assert lk.system_names == ("store-a", "store-b")
    assert (lk.system("store-a").platform, lk.system("store-b").platform) == (
        "shopify", "woocommerce")


@pytest.mark.parametrize("raw,key", [
    ("{not json sk-FAKE-T172-SECRET", "MAOS_CS_ORDER_SYSTEMS"),
    ("[]", "MAOS_CS_ORDER_SYSTEMS"),
    ("{}", "MAOS_CS_ORDER_SYSTEMS"),
    ('{"shop-a": "shopify"}', "shop-a"),
    ('{"shop-a": {"platform": "sk-FAKE-T172-SECRET"}}', "platform"),
    ('{"shop-a": {"account": "acct"}}', "platform"),
    ('{"shop-a": {"platform": "shopify", "token": "sk-FAKE-T172-SECRET"}}', "token"),
    ('{"shop-a": {"platform": "shopify", "account": 42}}', "account"),
    ('{"": {"platform": "shopify"}}', "MAOS_CS_ORDER_SYSTEMS"),
])
def test_env_json_bad_config_names_the_key_not_the_value_t172(raw, key):
    with pytest.raises(ValueError) as info:
        cs_ports.build_order_lookup_from_env({"MAOS_CS_ORDER_SYSTEMS": raw})
    assert key in str(info.value)
    assert "sk-FAKE-T172-SECRET" not in str(info.value)
    assert info.value.__cause__ is None
