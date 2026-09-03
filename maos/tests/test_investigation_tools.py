"""清算方 ToolPort 的测试 —— 报文时序、幂等，以及两个正交布尔。

本文件守的核心是 tools 层那条硬规矩：

    `send_cancellation()` **永远不返回终态**，
    `funds_settled` **只有 pacs.004 才为 True**。

一步返回结论的 mock 会让 `investigation.observe` 失去存在理由，整条论证跟着塌。
所以这里对 mock 自己也下断言 —— 它是 mock，但它的**时序不是假的**。

标了 `# 论证：` 的断言是复赛材料里那几句话的机器化版本。
"""

from __future__ import annotations

import pytest

from maos.core.store import SqliteStore
from maos.tools import investigation_codes as IC
from maos.tools.investigation import (
    ALL_SCRIPTS,
    CLEARING_CANCEL_PORT,
    CLEARING_RESOLUTION_PORT,
    MSG_CANCELLATION_REQUEST,
    MSG_PAYMENT_RETURN,
    MSG_RESOLUTION,
    SCRIPT_CONFIRMED_ONLY,
    SCRIPT_REJECTED,
    SCRIPT_RETURNED,
    SCRIPT_SILENT,
    CancellationRequest,
    DiscordantCancellationRequest,
    MockClearingHouse,
    SwiftNetworkAdapter,
)
from maos.tools.port import invoke_tool

MSG = "MSG-T-1"


def _req(**over):
    kw = dict(original_msg_id=MSG, end_to_end_id="E2E-T1", amount="12500.00",
              currency="EUR", reason_code="DUPL", idempotency_key="ASSGN-T1",
              case_id="case-t1")
    kw.update(over)
    return CancellationRequest(**kw)


def _house(script=SCRIPT_RETURNED, **kw):
    kw.setdefault("resolve_after", 2)
    return MockClearingHouse(script={MSG: script}, **kw)


def _drain(house, receipt, limit=8):
    """一直问到终态或问够 limit 次，返回全部观察。"""
    out = []
    for _ in range(limit):
        receipt = house.poll_resolution(receipt.request_id)
        out.append(receipt)
        if receipt.is_terminal:
            break
    return out


# --------------------------------------------------------------- 时序
@pytest.mark.parametrize("script", ALL_SCRIPTS)
def test_send_never_returns_terminal(script):
    """# 论证：camt.056 发出去的那一刻不可能有决议。

    一步返回结论的 mock 会让 observe 失去存在理由 —— 这条断言就是那条论证的守卫。
    """
    house = _house(script)
    r = house.send_cancellation(_req())
    assert r.is_terminal is False, f"{script}: send 不许返回终态"
    assert r.funds_settled is False
    assert r.request_resolved is False
    assert r.message_type == MSG_CANCELLATION_REQUEST


def test_returned_path_timeline():
    """顺利路径：PDCR → CNCL → pacs.004。**中间那一步是肯定答复但不是资金证据。**"""
    house = _house(SCRIPT_RETURNED)
    seen = _drain(house, house.send_cancellation(_req()))

    assert [r.message_type for r in seen] == [MSG_RESOLUTION, MSG_RESOLUTION,
                                              MSG_PAYMENT_RETURN]
    assert [r.confirmation_code for r in seen[:2]] == ["PDCR", "CNCL"]
    assert [r.funds_settled for r in seen] == [False, False, True]
    # 第 2 次已经 request_resolved，但 funds_settled 还是 False —— 两个维度正交。
    assert [r.request_resolved for r in seen] == [False, True, True]
    # 而 is_terminal 只在第 3 次为真：CNCL **不算**问到头。
    assert [r.is_terminal for r in seen] == [False, False, True]
    assert seen[-1].returned_amount == "12500.00"
    assert seen[-1].return_reason_code
    assert [r.poll_count for r in seen] == [1, 2, 3]


def test_confirmed_only_path_never_settles_funds():
    """# 论证：清算方一直说「撤销成功」，钱也可能一直不回来。

    这是本域失败路径的形状：`request_resolved` 恒 True、`funds_settled` 恒 False。
    """
    house = _house(SCRIPT_CONFIRMED_ONLY)
    seen = _drain(house, house.send_cancellation(_req()), limit=6)
    assert len(seen) == 6, "confirmed_only 不该出现终态，应当一直问下去"
    assert all(r.message_type == MSG_RESOLUTION for r in seen)
    assert [r.confirmation_code for r in seen] == ["PDCR"] + ["CNCL"] * 5
    assert all(r.funds_settled is False for r in seen), (
        "camt.029 无论重复多少次都不是资金证据")
    assert all(r.is_terminal is False for r in seen)
    assert seen[-1].request_resolved is True


def test_confirmed_only_and_returned_share_the_same_cncl():
    """# 论证：两条路径共用同一句肯定答复，逐字相同。

    本域最值钱的对照 —— 一个把 CNCL 当成业务成功的系统，会在失败路径上报成功。
    """
    # 两条路径各自独立跑，取第 2 次观察逐字比对。
    h1, h2 = _house(SCRIPT_RETURNED), _house(SCRIPT_CONFIRMED_ONLY)
    ok = _drain(h1, h1.send_cancellation(_req()))
    stuck = _drain(h2, h2.send_cancellation(_req()), limit=3)
    a, b = ok[1], stuck[1]
    assert a.confirmation_code == b.confirmation_code == "CNCL"
    assert a.message_type == b.message_type == MSG_RESOLUTION
    assert a.definition == b.definition
    assert a.resolution == b.resolution == IC.RESOLUTION_CONFIRMED
    # 唯一的差别在**下一次**：一条有 pacs.004，另一条没有。
    assert ok[-1].funds_settled is True


def test_rejected_path_carries_official_reason_code():
    """否定决议带拒绝原因码，且原因码来自官方码集。"""
    house = _house(SCRIPT_REJECTED)
    seen = _drain(house, house.send_cancellation(_req()))
    last = seen[-1]
    assert last.confirmation_code == "RJCR"
    assert last.resolution == IC.RESOLUTION_REJECTED
    assert last.is_terminal is True
    assert last.funds_settled is False, "被拒当然不是资金证据"
    entry = IC.rejection_reason(last.rejection_code)
    assert entry.definition == last.detail["rejection_reason"]


def test_silent_path_never_resolves():
    """问不出来就是问不出来 —— mock 不许「问累了就给个结论」。"""
    house = _house(SCRIPT_SILENT)
    seen = _drain(house, house.send_cancellation(_req()), limit=7)
    assert len(seen) == 7
    assert all(r.confirmation_code == "PDCR" for r in seen)
    assert all(r.is_terminal is False for r in seen)
    assert all(r.request_resolved is False for r in seen)


def test_resolve_after_must_be_at_least_one():
    with pytest.raises(ValueError):
        MockClearingHouse(resolve_after=0)


# --------------------------------------------------------------- funds_settled
def test_funds_settled_is_derived_not_settable():
    """# 论证：`funds_settled` 是对 message_type 的判定，不是可以手填的字段。

    做成 property 就杜绝了「构造回执时手填一个 True」这条路。
    """
    house = _house(SCRIPT_RETURNED)
    r = house.send_cancellation(_req())
    with pytest.raises((AttributeError, TypeError)):
        r.funds_settled = True                              # type: ignore[misc]
    # frozen dataclass：连普通字段也改不动。
    with pytest.raises((AttributeError, TypeError)):
        r.message_type = MSG_PAYMENT_RETURN                 # type: ignore[misc]


# --------------------------------------------------------------- 幂等
def test_same_key_same_params_does_not_send_twice():
    """# 论证：同一个指派号不发第二份 camt.056。"""
    house = _house(SCRIPT_RETURNED)
    a = house.send_cancellation(_req())
    b = house.send_cancellation(_req())
    assert house.request_count == 1
    assert a.request_id == b.request_id


def test_same_key_different_params_raises():
    """参数不一致要当场响 —— 清算方会把第二份当成第二个 case，而资金只有一笔。"""
    house = _house(SCRIPT_RETURNED)
    house.send_cancellation(_req())
    with pytest.raises(DiscordantCancellationRequest):
        house.send_cancellation(_req(amount="99999.00"))
    assert house.request_count == 1


def test_reason_code_is_part_of_the_fingerprint():
    """改了撤销原因码就是另一个请求 —— 清算方按原因码决定怎么处置。"""
    house = _house(SCRIPT_RETURNED)
    house.send_cancellation(_req())
    with pytest.raises(DiscordantCancellationRequest):
        house.send_cancellation(_req(reason_code="TECH"))


def test_missing_idempotency_key_raises():
    house = _house()
    with pytest.raises(ValueError):
        house.send_cancellation(_req(idempotency_key=""))


def test_replay_does_not_advance_poll_count():
    """重复 send 不推进问询计数 —— 只有 poll 才是观察。"""
    house = _house(SCRIPT_RETURNED)
    r = house.send_cancellation(_req())
    house.poll_resolution(r.request_id)
    again = house.send_cancellation(_req())
    assert again.poll_count == 1, "重放 send 不该多算一次问询"


# --------------------------------------------------------------- 码值校验
def test_unknown_reason_code_is_rejected_at_send():
    """编造的原因码发不出去 —— 那会是一份不合规报文。"""
    house = _house()
    with pytest.raises(IC.UnknownCodeError):
        house.send_cancellation(_req(reason_code="ZZZZ"))


def test_unknown_script_rejected_at_construction():
    with pytest.raises(ValueError):
        MockClearingHouse(script={MSG: "made-up-script"})


def test_unknown_injected_codes_rejected_at_construction():
    """剧本用的码在**装配阶段**就核，不留到演示当天才炸。"""
    with pytest.raises(IC.UnknownCodeError):
        MockClearingHouse(rejection_code="ZZZZ")
    with pytest.raises(IC.UnknownCodeError):
        MockClearingHouse(return_reason_code="ZZZZ")


def test_unknown_request_id_raises():
    house = _house()
    with pytest.raises(KeyError):
        house.poll_resolution("nope")


# --------------------------------------------------------------- ToolPort 九要素
@pytest.mark.parametrize("port", [CLEARING_CANCEL_PORT, CLEARING_RESOLUTION_PORT])
def test_toolport_declares_nine_elements(port):
    """九要素一个不缺，且失败形态与安全边界不是空话。"""
    assert port.name and port.purpose and callable(port.entry)
    assert port.params_schema and port.returns_schema
    assert len(port.failure_modes) >= 3
    assert port.security_boundary
    assert port.owner


def test_resolution_port_documents_the_dangerous_quadrant():
    """最危险的那一档必须写在 failure_modes 里，不能只活在源码注释中。"""
    modes = " ".join(CLEARING_RESOLUTION_PORT.failure_modes)
    assert "CNCL" in modes
    assert "funds_settled=False" in modes


def test_invoke_tool_leaves_an_audit_row():
    """# 论证：清算方调用一律留 ToolInvoked 审计行。"""
    store = SqliteStore()
    store.init_schema()
    house = _house(SCRIPT_RETURNED)
    r = invoke_tool(CLEARING_CANCEL_PORT, {
        "clearing": house, "original_msg_id": MSG, "end_to_end_id": "E2E-T1",
        "amount": "12500.00", "currency": "EUR", "reason_code": "DUPL",
        "idempotency_key": "ASSGN-T1", "case_id": "case-t1",
    }, store=store, extras={"plan_id": "p1"})
    invoke_tool(CLEARING_RESOLUTION_PORT,
                {"clearing": house, "request_id": r["request_id"]},
                store=store, extras={"plan_id": "p1"})
    rows = [e for e in store.list_event_log("p1") if e["event_type"] == "ToolInvoked"]
    assert [e["detail"]["tool"] for e in rows] == ["clearing.cancel", "clearing.resolution"]
    assert all(e["detail"]["status"] == "ok" for e in rows)
    assert all(e["detail"]["params_digest"] for e in rows)


def test_mock_repr_is_stable_for_digests():
    """repr 不带内存地址 —— 带了会让同样参数每次算出不同的 params_digest。"""
    assert "0x" not in repr(_house())
    assert repr(_house()) == repr(_house())


# --------------------------------------------------------------- 真适配器只留壳
def test_real_adapter_does_not_fake_data():
    """未接通的适配器显式抛，**不静默返回假数据**。

    一个「看起来返回了点什么」的桩会让上层以为接通了，那比没实现危险得多。
    """
    adapter = SwiftNetworkAdapter(bic="DEUTDEFFXXX", endpoint="https://example.invalid",
                                  credential="s3cret")
    with pytest.raises(NotImplementedError):
        adapter.send_cancellation(_req())
    with pytest.raises(NotImplementedError):
        adapter.poll_resolution("x")


def test_real_adapter_repr_hides_credential():
    """凭据不许进 repr（铁律 6）。"""
    adapter = SwiftNetworkAdapter(bic="DEUTDEFFXXX", endpoint="https://example.invalid",
                                  credential="super-secret-token")
    assert "super-secret-token" not in repr(adapter)
