"""RTV 五个 ToolPort 与三个进程内模拟器的用例。

钉四件事，一件都不许松：

1. 九要素一项不空 —— ``failure_modes`` 与 ``security_boundary`` 是评审时会被逐条对
   的东西，不是文档；
2. 🔴 **acknowledged 与 issued 分得开** —— 权威闸的判据就靠这一条；
3. 写操作永远不返回终态、终态是问出来的、问不出来时如实返回非终态；
4. 调用姿势：经 ``invoke_tool`` 才有 ToolInvoked 审计行。

全部进程内，一行真网络都不打。
"""

from __future__ import annotations

import importlib
import pkgutil

import pytest

import maos.tools
from maos.tools import rtv_codes as codes
from maos.tools.port import ToolPort, invoke_tool
from maos.tools.rtv import (
    AP_ADJUST_QUERY_PORT,
    CARRIER_SHIP_PORT,
    CARRIER_TRACK_PORT,
    RTV_PORTS,
    SUPPLIER_CREDIT_QUERY_PORT,
    SUPPLIER_RMA_SUBMIT_PORT,
    ApAdjustmentAdvice,
    CarrierAdvice,
    DuplicateRequest,
    MockApSystem,
    MockCarrier,
    MockSupplier,
    RmaRequest,
    ShipmentOrder,
    SupplierAdvice,
)

CASE = "rtv-1001"
KEY = "acme/rtv-1001"


def _request(**over) -> RmaRequest:
    kwargs = dict(
        supplier_id="sup-77", case_id=CASE, po_id="po-9", gr_id="gr-9",
        amount_claimed="1200.00", currency="CNY",
        reason_code=codes.REASON_QUALITY_DEFECT, idempotency_key=KEY,
    )
    kwargs.update(over)
    return RmaRequest(**kwargs)


def _order(rma_id: str = "rma-x", **over) -> ShipmentOrder:
    kwargs = dict(
        case_id=CASE, rma_id=rma_id, carrier="SF", origin="wh-1",
        destination="sup-77", idempotency_key=KEY,
    )
    kwargs.update(over)
    return ShipmentOrder(**kwargs)


# ====================================================== ToolPort 九要素与命名
@pytest.mark.parametrize("port", RTV_PORTS, ids=lambda p: p.name)
def test_ports_declare_all_nine_elements(port):
    """九要素一个都不许空着 —— failure_modes 与 security_boundary 尤其。

    口径与 ``test_ap_tools.py`` 同一条：它们是评审时会被逐条对的东西
    （``maos/tools/port.py`` 抬头）。
    """
    assert port.name and port.purpose and callable(port.entry)
    assert port.params_schema, f"{port.name} 没声明入参"
    assert port.returns_schema, f"{port.name} 没声明返回"
    assert len(port.failure_modes) >= 3, f"{port.name} 的失败模式声明太少"
    assert len(port.security_boundary) > 40, f"{port.name} 的安全边界写得太薄"
    assert port.owner, f"{port.name} 没有 owner"


def test_port_names_are_exactly_the_five_the_contract_froze():
    """契约 C-R5 冻的是名字。多一个少一个都是越界。"""
    assert sorted(p.name for p in RTV_PORTS) == [
        "ap.adjust_query", "carrier.ship", "carrier.track",
        "supplier.credit_query", "supplier.rma_submit",
    ]
    assert len(RTV_PORTS) == 5


def test_port_owners_match_the_role_contract():
    """owner 按契约 C-R7 的角色填 —— 裁定的人不碰承运商，发运的人不碰供应商开票。"""
    owners = {p.name: p.owner for p in RTV_PORTS}
    assert owners == {
        "supplier.rma_submit": "rtv_settlement",
        "supplier.credit_query": "rtv_reconcile",
        "carrier.ship": "rtv_logistics",
        "carrier.track": "rtv_logistics",
        "ap.adjust_query": "rtv_settlement",
    }


def _all_ports_in_repo() -> list[ToolPort]:
    """扫 ``maos/tools`` 里所有模块级 ToolPort 对象，按对象身份去重。

    按身份去重而不是按名字：一个 port 被别的模块 import 过去，会在两个模块的
    ``vars()`` 里各出现一次，但它是同一个对象 —— 按名字去重就把「真的重名」和
    「同一个对象被看见两次」混成了一件事，而前者正是这条用例要抓的。

    只走顶层模块，不进子包：``maos.tools.mcp`` 里的 port 从 ``KNOWN_PORTS`` 取，
    import 一个 server 模块可能有副作用。
    """
    seen: dict[int, ToolPort] = {}
    for info in pkgutil.iter_modules(maos.tools.__path__, "maos.tools."):
        if info.ispkg:
            continue
        mod = importlib.import_module(info.name)
        for obj in vars(mod).values():
            if isinstance(obj, ToolPort):
                seen[id(obj)] = obj
    from maos.tools.mcp.registry import KNOWN_PORTS
    for obj in KNOWN_PORTS.values():
        seen[id(obj)] = obj
    return list(seen.values())


def test_rtv_port_names_collide_with_nothing_in_the_repo():
    """重名的后果不是报错，是**两个 port 抢同一个白名单条目** —— 角色以为自己拿到
    的是这个，实际取到的是那个，而且没有任何症状。
    """
    ports = _all_ports_in_repo()
    names = [p.name for p in ports]
    assert len(names) == len(set(names)), f"全仓 ToolPort 重名：{sorted(names)}"

    # 采样断言：收集确实跑通了（收不到已有 port 的话，上面那条就是空断言）。
    for known in ("bank.pay", "bank.query", "gateway.refund", "gateway.query",
                  "payer.submit", "payer.query", "clearing.cancel",
                  "clearing.resolution", "sandbox.git_apply", "sandbox.pytest_run",
                  "git-mcp"):
        assert known in names, f"没收集到已有 port {known}，这条用例的前提不成立"

    rtv_names = {p.name for p in RTV_PORTS}
    others = {p.name for p in ports if p not in RTV_PORTS}
    assert rtv_names.isdisjoint(others)


def test_ap_adjust_query_declares_itself_read_only():
    """🔴 契约 C-R5 红字：RTV 域不许写 AP 的任何东西。

    这条钉的是**声明**；``MockApSystem`` 没有写方法由下面另一条用例钉。
    """
    assert "只读" in AP_ADJUST_QUERY_PORT.security_boundary


@pytest.mark.parametrize("port", [SUPPLIER_CREDIT_QUERY_PORT, AP_ADJUST_QUERY_PORT],
                         ids=lambda p: p.name)
def test_authority_ports_name_their_only_writer(port):
    """两个权威判据入口的 security_boundary 必须点名 ``rtv.observe``。

    口径抄 ``bank.query``（它点名了「本 port 是 ap.observe 取得权威事实的唯一入口」）：
    那句话是权威链在代码里的**文字证据** —— 没有它，「谁能写终态」这件事就只存在于
    业务域的守卫里，工具这一侧读不出来。
    """
    assert "rtv.observe" in port.security_boundary


# ============================================ 🔴 acknowledged vs issued
def test_acknowledged_is_not_credit_evidence_and_carries_no_credit_note():
    """本轨最硬的一条 —— T61 的权威闸就靠它。

    只 ack 未开票时：状态是 ``acknowledged``、``is_terminal`` 为 False、
    **不带** ``credit_note_id``、``is_credit_evidence`` 为 False。
    四条同时成立才骗不过闸。
    """
    supplier = MockSupplier(ack_after=1, issue_after=3)
    submitted = supplier.rma_submit(_request())

    acked = supplier.credit_query(submitted.rma_id)
    assert acked.status == codes.SUPPLIER_ACKNOWLEDGED
    assert acked.is_terminal is False
    assert acked.credit_note_id == ""
    assert acked.is_credit_evidence is False
    assert acked.amount_credited == "", "供应商还没认金额，这一栏必须是空的"

    as_dict = acked.as_dict()
    assert as_dict["status"] == "acknowledged"
    assert as_dict["is_credit_evidence"] is False
    assert as_dict["credit_note_id"] == ""


def test_issued_is_the_only_credit_evidence():
    """再问到 issued 才算数，而且这时才有贷项通知单与供应商认的金额。"""
    supplier = MockSupplier(ack_after=1, issue_after=3)
    submitted = supplier.rma_submit(_request())
    for _ in range(3):
        advice = supplier.credit_query(submitted.rma_id)

    assert advice.status == codes.SUPPLIER_ISSUED
    assert advice.is_terminal is True
    assert advice.is_credit_evidence is True
    assert advice.credit_note_id
    assert advice.document_type == codes.CODE_CREDIT_NOTE
    assert advice.amount_credited == "1200.00"
    assert advice.issued_at, "供应商开票时间要留着，它与我方观察时间刻意分开"
    assert advice.poll_count == 3, "终态是**问出来的**，一次不够"


def test_a_supplier_that_merges_the_two_states_is_refused_at_construction():
    """``issue_after == ack_after`` 就等于把两个状态合并了 —— 构造时当场拒。

    这条守的是「有人为了让测试快一点，把两个参数设成一样」那种改法：
    它不会报错，只会让 acknowledged 这一档从此不出现，而闸从此没东西可拦。
    """
    with pytest.raises(ValueError, match="issue_after"):
        MockSupplier(ack_after=2, issue_after=2)


def test_a_non_issued_advice_may_never_carry_a_credit_note_id():
    """判据的第二条腿：状态字符串以外，还得看「有没有那张单」。

    一份「acknowledged 但带着单号」的回执能骗过只判状态的闸，所以这里直接在
    回执的构造上封死。
    """
    with pytest.raises(ValueError, match="credit_note_id"):
        SupplierAdvice(
            rma_id="rma-1", idempotency_key=KEY, case_id=CASE,
            status=codes.SUPPLIER_ACKNOWLEDGED,
            amount_claimed="1.00", currency="CNY",
            credit_note_id="cn-forged")


def test_issued_advice_must_carry_a_valid_uncl1001_document_type():
    with pytest.raises(KeyError):
        SupplierAdvice(
            rma_id="rma-1", idempotency_key=KEY, case_id=CASE,
            status=codes.SUPPLIER_ISSUED, amount_claimed="1.00", currency="CNY",
            credit_note_id="cn-1", document_type="380")   # 商业发票，方向反了


# ============================================ 写操作永不返回终态 / 幂等
def test_rma_submit_never_returns_a_terminal_advice():
    """一步返回 issued 的 mock 会把整条论证抽空。"""
    advice = MockSupplier().rma_submit(_request())
    assert advice.status == codes.SUPPLIER_SUBMITTED
    assert advice.is_terminal is False
    assert advice.is_credit_evidence is False
    assert advice.poll_count == 0
    assert advice.credit_note_id == ""


def test_same_key_same_params_does_not_create_a_second_rma():
    """幂等：重复提同一份申请不许产生第二张退货授权。"""
    supplier = MockSupplier()
    first = supplier.rma_submit(_request())
    second = supplier.rma_submit(_request())
    assert second.rma_id == first.rma_id
    assert len(supplier._ledger) == 1


def test_same_key_different_params_is_refused():
    """参数不一致不静默收下也不静默丢弃：收下会开出第二张 RMA。"""
    supplier = MockSupplier()
    supplier.rma_submit(_request())
    with pytest.raises(DuplicateRequest):
        supplier.rma_submit(_request(amount_claimed="9999.00"))


def test_note_is_not_part_of_the_rma_fingerprint():
    """附言改了不影响退货结果，不该被判成「两笔不同的退货」。"""
    supplier = MockSupplier()
    first = supplier.rma_submit(_request())
    again = supplier.rma_submit(_request(note="麻烦尽快处理"))
    assert again.rma_id == first.rma_id


def test_empty_idempotency_key_is_refused():
    with pytest.raises(ValueError):
        MockSupplier().rma_submit(_request(idempotency_key=""))


def test_unknown_return_reason_is_refused_at_construction():
    """理由码当场核，不等到供应商那边才发现。"""
    with pytest.raises(KeyError):
        _request(reason_code="RTV-RSN-99")


# ================================ 轮询到顶仍非终态 -> 如实返回，不改判成失败
def test_stuck_supplier_stays_non_terminal_and_is_never_recast_as_disputed():
    """「我问累了」和「供应商说不认这笔退货」是两回事。"""
    supplier = MockSupplier(script={CASE: codes.SUPPLIER_UNKNOWN})
    submitted = supplier.rma_submit(_request())
    for _ in range(8):
        advice = supplier.credit_query(submitted.rma_id)

    assert advice.status == codes.SUPPLIER_UNKNOWN
    assert advice.is_terminal is False, "unknown 不是终态 —— 说不清不是一种结论"
    assert advice.is_credit_evidence is False
    assert advice.status != codes.SUPPLIER_DISPUTED
    assert advice.poll_count == 8, "问了几次要如实记着"


def test_supplier_that_never_issues_stays_acknowledged_forever():
    """货签收了、供应商却迟迟不开票 —— 最容易被系统假装成已退款的一档。"""
    supplier = MockSupplier(ack_after=1, issue_after=999)
    submitted = supplier.rma_submit(_request())
    for _ in range(20):
        advice = supplier.credit_query(submitted.rma_id)
    assert advice.status == codes.SUPPLIER_ACKNOWLEDGED
    assert advice.is_credit_evidence is False


def test_terminal_supplier_advice_never_changes_again():
    supplier = MockSupplier(ack_after=1, issue_after=2)
    submitted = supplier.rma_submit(_request())
    supplier.credit_query(submitted.rma_id)
    first_terminal = supplier.credit_query(submitted.rma_id)
    assert first_terminal.is_terminal
    assert supplier.credit_query(submitted.rma_id) == first_terminal


def test_unknown_rma_id_raises():
    with pytest.raises(LookupError):
        MockSupplier().credit_query("rma-nope")


def test_disputed_is_terminal_but_carries_no_credit_note():
    supplier = MockSupplier(script={CASE: codes.SUPPLIER_DISPUTED})
    submitted = supplier.rma_submit(_request())
    for _ in range(3):
        advice = supplier.credit_query(submitted.rma_id)
    assert advice.status == codes.SUPPLIER_DISPUTED
    assert advice.is_terminal is True
    assert advice.is_credit_evidence is False
    assert advice.credit_note_id == ""


def test_credit_amount_and_currency_can_diverge_from_the_claim():
    """供应商认的金额 / 币种与我方申报的不一致 —— 三方对账要拦的正是这个。

    两者合并成一处就拦不到了，所以模拟器必须演得出来。
    """
    supplier = MockSupplier(credit_amounts={CASE: "900.00"},
                            credit_currencies={CASE: "USD"})
    submitted = supplier.rma_submit(_request())
    for _ in range(3):
        advice = supplier.credit_query(submitted.rma_id)
    assert advice.amount_claimed == "1200.00"
    assert advice.amount_credited == "900.00"
    assert advice.currency == "USD"


def test_supplier_is_deterministic():
    """同样的入参连跑两次，除了随机单号以外逐条一致。"""
    def run():
        supplier = MockSupplier()
        submitted = supplier.rma_submit(_request())
        return [supplier.credit_query(submitted.rma_id).status for _ in range(4)]
    assert run() == run()


# =========================================================== 承运商
def test_ship_never_returns_a_terminal_advice():
    """建单不等于送达。业务状态 shipped 只能由 track 的回执得到。"""
    advice = MockCarrier().ship(_order())
    assert advice.status == codes.CARRIER_CREATED
    assert advice.is_terminal is False
    assert advice.poll_count == 0


def test_ship_without_an_rma_is_refused():
    """没有退货授权的货会被供应商拒收，而那时货已经在路上。"""
    with pytest.raises(ValueError, match="rma_id"):
        _order(rma_id="")


def test_ship_is_idempotent_on_the_key():
    carrier = MockCarrier()
    first = carrier.ship(_order())
    assert carrier.ship(_order()).tracking_no == first.tracking_no
    with pytest.raises(DuplicateRequest):
        carrier.ship(_order(parcel_count=5))


def test_track_reaches_delivered_only_after_more_than_one_poll():
    carrier = MockCarrier(deliver_after=2)
    shipped = carrier.ship(_order())
    first = carrier.track(shipped.tracking_no)
    assert first.status == codes.CARRIER_IN_TRANSIT
    assert first.is_terminal is False
    second = carrier.track(shipped.tracking_no)
    assert second.status == codes.CARRIER_DELIVERED
    assert second.is_terminal is True
    assert second.delivered_at


def test_stuck_carrier_is_never_recast_as_exception():
    carrier = MockCarrier(deliver_after=999)
    shipped = carrier.ship(_order())
    for _ in range(10):
        advice = carrier.track(shipped.tracking_no)
    assert advice.status == codes.CARRIER_IN_TRANSIT
    assert advice.status != codes.CARRIER_EXCEPTION


def test_carrier_exception_is_terminal_and_has_no_delivery_time():
    carrier = MockCarrier(script={CASE: codes.CARRIER_EXCEPTION})
    shipped = carrier.ship(_order())
    for _ in range(3):
        advice = carrier.track(shipped.tracking_no)
    assert advice.status == codes.CARRIER_EXCEPTION
    assert advice.is_terminal is True
    assert advice.delivered_at == ""


def test_unknown_tracking_no_raises():
    with pytest.raises(LookupError):
        MockCarrier().track("trk-nope")


def test_unknown_carrier_status_is_refused():
    with pytest.raises(ValueError):
        CarrierAdvice(shipment_id="s", tracking_no="t", idempotency_key=KEY,
                      case_id=CASE, carrier="SF", status="teleported")


# =========================================================== AP 系统（只读）
def test_ap_system_has_no_write_method():
    """🔴 契约 C-R5 红字：模拟器**不许提供**任何写 AP 的方法。

    两处都能写会让「这笔调整是谁建的」失去唯一答案。这条按公开方法名扫 ——
    有人加一个 ``create_adjustment`` 进来，这里当场红。
    """
    public = {name for name in dir(MockApSystem) if not name.startswith("_")}
    assert public == {"query", "known_cases"}, (
        f"MockApSystem 只许有只读方法，多出来的：{public - {'query', 'known_cases'}}")
    assert not hasattr(MockApSystem, "build")
    assert not hasattr(MockApSystem, "create_adjustment")
    assert not hasattr(MockApSystem, "settle")


def test_ap_none_is_not_a_failure_and_is_not_terminal():
    """AP 侧还没建凭单 —— 「还没到」，不是「失败了」。"""
    ap = MockApSystem(ledger={})
    advice = ap.query(CASE)
    assert advice.status == codes.AP_NONE
    assert advice.is_terminal is False
    assert advice.is_settlement_evidence is False
    assert advice.adjustment_id == ""
    assert advice.status != codes.AP_VOIDED


def test_ap_settled_takes_more_than_one_poll_and_carries_a_reference():
    ap = MockApSystem(ledger={CASE: {"amount": "1200.00", "currency": "CNY"}},
                      settle_after=3)
    assert ap.query(CASE).status == codes.AP_STAGED
    assert ap.query(CASE).status == codes.AP_BUILT
    advice = ap.query(CASE)
    assert advice.status == codes.AP_SETTLED
    assert advice.is_terminal is True
    assert advice.is_settlement_evidence is True
    assert advice.ap_reference, "钱到账的外部凭据不许为空"
    assert advice.poll_count == 3


def test_ap_stuck_is_never_recast_as_voided():
    ap = MockApSystem(ledger={CASE: {"amount": "1.00"}}, settle_after=999)
    for _ in range(10):
        advice = ap.query(CASE)
    assert advice.status == codes.AP_BUILT
    assert advice.is_terminal is False
    assert advice.status != codes.AP_VOIDED


def test_ap_voided_is_terminal_but_not_settlement_evidence():
    """凭单作废是终态，但**不是**「钱到账」—— 该案子走补偿路径。"""
    ap = MockApSystem(ledger={CASE: {"amount": "1.00"}}, settle_after=2,
                      script={CASE: codes.AP_VOIDED})
    ap.query(CASE)
    advice = ap.query(CASE)
    assert advice.status == codes.AP_VOIDED
    assert advice.is_terminal is True
    assert advice.is_settlement_evidence is False
    assert advice.ap_reference == ""


def test_a_non_settled_advice_may_never_carry_an_ap_reference():
    with pytest.raises(ValueError, match="ap_reference"):
        ApAdjustmentAdvice(case_id=CASE, status=codes.AP_BUILT,
                           ap_reference="apref-forged")


def test_ap_settle_after_one_is_refused():
    with pytest.raises(ValueError, match="settle_after"):
        MockApSystem(settle_after=1)


def test_terminal_ap_advice_never_changes_again():
    ap = MockApSystem(ledger={CASE: {"amount": "1.00"}}, settle_after=2)
    ap.query(CASE)
    first_terminal = ap.query(CASE)
    assert first_terminal.is_terminal
    assert ap.query(CASE) == first_terminal


# =========================================================== 调用姿势
def test_invoke_tool_leaves_an_audit_row():
    """调用一律经 ``invoke_tool`` —— 直接调 ``port.entry`` 就没有 ToolInvoked 审计行，
    出事之后查不到是谁、什么参数、跑了多久。
    """
    from maos.core.store import SqliteStore

    store = SqliteStore()
    store.init_schema()
    supplier = MockSupplier()
    carrier = MockCarrier()
    ap = MockApSystem(ledger={CASE: {"amount": "1200.00"}})
    extras = {"plan_id": "p", "task_id": "t"}

    submitted = invoke_tool(SUPPLIER_RMA_SUBMIT_PORT,
                            {"supplier": supplier, "request": _request()},
                            store=store, extras=extras)
    invoke_tool(SUPPLIER_CREDIT_QUERY_PORT,
                {"supplier": supplier, "rma_id": submitted["rma_id"]},
                store=store, extras=extras)
    shipped = invoke_tool(CARRIER_SHIP_PORT,
                          {"carrier": carrier,
                           "order": _order(rma_id=submitted["rma_id"])},
                          store=store, extras=extras)
    invoke_tool(CARRIER_TRACK_PORT,
                {"carrier": carrier, "tracking_no": shipped["tracking_no"]},
                store=store, extras=extras)
    invoke_tool(AP_ADJUST_QUERY_PORT, {"ap_system": ap, "case_id": CASE},
                store=store, extras=extras)

    rows = [e for e in store.list_event_log("p") if e["event_type"] == "ToolInvoked"]
    assert len(rows) == 5
    assert sorted(r["detail"]["tool"] for r in rows) == [
        "ap.adjust_query", "carrier.ship", "carrier.track",
        "supplier.credit_query", "supplier.rma_submit",
    ]
    assert all(r["detail"]["status"] == "ok" for r in rows)


def test_invoke_tool_records_failures_too():
    """工具失败要被落审计再原样上抛，不能在这一层被吞成 None。"""
    from maos.core.store import SqliteStore

    store = SqliteStore()
    store.init_schema()
    with pytest.raises(LookupError):
        invoke_tool(SUPPLIER_CREDIT_QUERY_PORT,
                    {"supplier": MockSupplier(), "rma_id": "rma-nope"},
                    store=store, extras={"plan_id": "p"})
    rows = [e for e in store.list_event_log("p") if e["event_type"] == "ToolInvoked"]
    assert len(rows) == 1
    assert rows[0]["detail"]["status"] == "failed"
    assert "LookupError" in rows[0]["detail"]["error"]


def test_port_entries_return_json_shaped_dicts():
    """产物要能 json 化 —— entry 返回 dict 而不是 dataclass。"""
    import json

    supplier = MockSupplier()
    out = SUPPLIER_RMA_SUBMIT_PORT.entry(supplier=supplier, request=_request())
    assert isinstance(out, dict)
    json.dumps(out, ensure_ascii=False)


# =========================================================== 零出网自证
def test_the_module_opens_no_socket():
    """五个 port 背后全是进程内模拟器：全套跑完不许建任何网络连接。

    直接把 ``socket.socket`` 换成会抛的东西 —— 比 grep 源码硬：真有人 import 了
    一个自己打网络的库，grep 未必看得见，这条会当场红。
    """
    import socket

    original = socket.socket

    class _Forbidden(socket.socket):        # noqa: D401
        def __init__(self, *a, **kw):
            raise AssertionError("RTV 工具层不许打网络 —— 五个 port 背后全是进程内模拟器")

    socket.socket = _Forbidden
    try:
        supplier = MockSupplier()
        carrier = MockCarrier()
        ap = MockApSystem(ledger={CASE: {"amount": "1.00"}})
        submitted = supplier.rma_submit(_request())
        for _ in range(3):
            supplier.credit_query(submitted.rma_id)
        shipped = carrier.ship(_order(rma_id=submitted.rma_id))
        for _ in range(3):
            carrier.track(shipped.tracking_no)
        for _ in range(3):
            ap.query(CASE)
    finally:
        socket.socket = original
