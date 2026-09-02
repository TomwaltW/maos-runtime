"""T76 —— 网关 ToolPort 的参数面：params 里只放名字，不放活对象。

## 这一组测试买的是什么

`docs/BACKLOG.md:1640` 记的那条：`gateway.refund` / `gateway.query` 原先把
`GatewayPort` **活对象本身**当 params 传，而 `invoke_tool` 会对 params 算
sha256 落审计行。对象进 digest 之后，「同样的参数算出同样的 digest」这件事
就只能靠每个网关实现自己写一个不带内存地址的 `__repr__` 来维持 ——
`MockGateway` 曾经就有那么一个补丁。

那是**实现方的自觉，不是机制**：第三个实现只要忘了写 `__repr__`，默认 repr
带 `0x7f...` 地址，同样参数每次算出不同 digest，审计对不上账，而且**不报错、
无症状**。本组测试把「params 全是标量」钉成机器判据，让这条静默失效不再可能。

顺带这也是迁 MCP 的前置条件：活对象跨不了进程，名字可以。

## 为什么第 1 条要去捞真实的 params 而不是只看 digest

`store` 里只落 `params_digest`，不落 params 本身 —— 光看 digest 是 64 位十六进制
这件事，任何输入都成立，等于什么都没验。所以第 1 条把真实传进 `invoke_tool` 的
那个 dict 截下来，直接对它断言「能 json.dumps」并**用它重算出库里那个 digest**，
两头对上才算证明。
"""

from __future__ import annotations

import json

import pytest

from maos.agents.base import AgentIdentity
from maos.domain.refund import guard, objects
from maos.flows import scenario_6 as s6
from maos.model.client import Tier
from maos.skills.builtin.refund import REFUND_SKILLS
from maos.skills.builtin.refund import _common as C
from maos.skills.invoker import SkillInvoker
from maos.core.store import SqliteStore
from maos.tools import gateway as gwtools
from maos.tools.gateway import (
    GATEWAY_QUERY_PORT,
    GATEWAY_REFUND_PORT,
    MockGateway,
)
from maos.tools.port import _digest, invoke_tool

TEST_GATEWAY = "t76-gw"

ALL_SKILLS_IDENTITY = AgentIdentity(
    agent_id="test-t76",
    role="test_refund",
    duty="测试夹具：授权退款域全部 skill",
    allowed_skills=frozenset(set(REFUND_SKILLS) | {"issue.aggregate"}),
    allowed_tools=frozenset({"gateway.refund", "gateway.query"}),
    write_scope=frozenset({"artifact"}),
    max_risk="M",
    model_tier=Tier.LIGHT,
)


@pytest.fixture
def store():
    st = SqliteStore()
    st.init_schema()
    s6.seed_domain(st)
    return st


@pytest.fixture
def gateway():
    """两张表都清干净再登记 —— 工具侧那张是全局的，不清会串账本。"""
    C.reset_gateways()
    gwtools.reset_gateways()
    gw = C.register_gateway(TEST_GATEWAY, MockGateway(settle_after=s6.SETTLE_AFTER))
    yield gw
    C.reset_gateways()
    gwtools.reset_gateways()


@pytest.fixture
def invoker(store):
    return SkillInvoker(ALL_SKILLS_IDENTITY, store)


def _extras(**over) -> dict:
    base = {"plan_id": "plan-t76", "task_id": "task-t76", "trace_id": "trace-t76",
            "attempt": 1}
    base.update(over)
    return base


def _ready_case(invoker, store):
    """把案子推到「已审批、可发起付款」。"""
    seed = {
        "tenant_id": s6.TENANT_ID, "case_id": s6.CASE_ID, "channel_id": s6.CHANNEL_ID,
        "order_id": s6.ORDER_ID, "order_version": s6.ORDER_VERSION, "sku": s6.SKU,
        "reason_code": "quality_defect", "amount_claimed": s6.AMOUNT_CLAIMED,
    }
    res = invoker.invoke("refund.intake",
                         {"signals": s6.SIGNALS, "case_seed": seed}, extras=_extras())
    assert res.status == "ok", res.error
    pol = invoker.invoke("policy.match",
                         {"tenant_id": s6.TENANT_ID, "case_id": s6.CASE_ID},
                         extras=_extras())
    assert pol.status == "ok", pol.error
    fin = invoker.invoke("finance.settle", {
        "tenant_id": s6.TENANT_ID, "case_id": s6.CASE_ID, "policy": pol.output,
    }, extras=_extras())
    assert fin.status == "ok", fin.error
    C.record_approval(store, tenant_id=s6.TENANT_ID, case_id=s6.CASE_ID,
                      approver="测试主管", decision="approved", reason="T76 单测放行")


def _tool_rows(store, tool: str, plan_id: str = "plan-t76") -> list[dict]:
    return [r for r in store.list_event_log(plan_id)
            if r["event_type"] == "ToolInvoked" and r["detail"]["tool"] == tool]


def _capture_params(monkeypatch, module) -> list[dict]:
    """把真正传进 invoke_tool 的 params 截下来，原样转交，不改变行为。"""
    seen: list[dict] = []
    real = module.invoke_tool

    def spy(port, params, **kw):
        seen.append(dict(params))
        return real(port, params, **kw)

    monkeypatch.setattr(module, "invoke_tool", spy)
    return seen


# ======================================================================
# 1. 本轨买的东西：params 里不含活对象
# ======================================================================
def test_params_digest_has_no_object_repr(invoker, store, gateway, monkeypatch):
    """落审计的那份 params **全是可 json.dumps 的标量**，一个活对象都没有。

    三段自证，缺一不可：
      · `json.dumps(params)` 不抛 —— 有活对象就会抛 TypeError；
      · 序列化后的文本里没有类名 / 内存地址的痕迹；
      · 用这份 params 重算的 digest **等于**库里落的那个，证明验的就是落库那份。
    """
    from maos.skills.builtin.refund import payment_execute as pe

    _ready_case(invoker, store)
    seen = _capture_params(monkeypatch, pe)

    res = invoker.invoke("payment.execute", {
        "tenant_id": s6.TENANT_ID, "case_id": s6.CASE_ID, "gateway": TEST_GATEWAY,
    }, extras=_extras())
    assert res.status == "ok", res.error

    assert len(seen) == 1, "payment.execute 应恰好调一次 gateway.refund"
    params = seen[0]

    # (1) 全标量：有活对象这里就是 TypeError
    raw = json.dumps(params, ensure_ascii=False, sort_keys=True)

    # (2) 没有对象痕迹
    assert "MockGateway" not in raw and "0x" not in raw, (
        f"params 里还有对象的 repr 痕迹：{raw}")
    assert params["gateway_name"] == TEST_GATEWAY
    assert "gateway" not in params, "活对象那个键名必须已经不存在"
    for key, value in params.items():
        assert isinstance(value, (str, int, float, bool, type(None))), (
            f"params[{key!r}] 是 {type(value).__name__}，不是标量 —— "
            "活对象又混进 params 了，digest 的可复现性会退回靠 __repr__ 维持")

    # (3) 验的就是落库那份
    rows = _tool_rows(store, "gateway.refund")
    assert len(rows) == 1
    assert rows[0]["detail"]["params_digest"] == _digest(params), (
        "重算的 digest 与库里的对不上 —— 截到的不是真正落审计的那份 params")


def test_same_refund_twice_yields_byte_identical_digest(invoker, store, gateway):
    """同一笔退款调两次，两条审计行的 params_digest **逐字节相同**。

    可复现性没有因为改签名而退化。注意这里走的是幂等路径：第二次不产生第二笔
    退款，但**仍然落一条 ToolInvoked** —— 审计记的是「调用发生过」，不是「产生了新单」。
    """
    _ready_case(invoker, store)
    payload = {"tenant_id": s6.TENANT_ID, "case_id": s6.CASE_ID, "gateway": TEST_GATEWAY}

    first = invoker.invoke("payment.execute", dict(payload), extras=_extras())
    second = invoker.invoke("payment.execute", dict(payload), extras=_extras())
    assert first.status == "ok" and second.status == "ok", second.error

    digests = [r["detail"]["params_digest"] for r in _tool_rows(store, "gateway.refund")]
    assert len(digests) == 2, f"应有两条 gateway.refund 审计行，实际 {len(digests)}"
    assert digests[0] == digests[1], f"同样参数算出了不同的 digest：{digests}"
    assert gateway.refund_count == 1, "幂等口径没变：同幂等键仍只产生一笔退款"


def test_digest_no_longer_depends_on_gateway_repr():
    """`MockGateway` 那个 `__repr__` 补丁已经拆掉，而 digest 照样稳定。

    这条守的是 §5.2：补丁的存在理由（活对象进 digest）消失了，补丁也要跟着走。
    留着它会让下一个人以为 digest 仍然依赖 repr，从而不敢动别处。
    """
    assert "__repr__" not in vars(MockGateway), (
        "MockGateway 又自己写了 __repr__ —— 若理由仍是「进 params_digest」，"
        "那说明活对象传参回潮了，先查两个 ToolPort 的签名")

    gwtools.reset_gateways()
    try:
        params = {"gateway_name": "gw-a", "out_trade_no": "T-1",
                  "refund_amount": "1.00", "idempotency_key": "R-1"}
        # 两个**不同实例**登记到同一个名字下，digest 仍相同 ——
        # 因为 digest 根本不看实例了。
        gwtools.register_gateway("gw-a", MockGateway())
        d1 = _digest(dict(params))
        gwtools.register_gateway("gw-a", MockGateway(settle_after=9))
        d2 = _digest(dict(params))
        assert d1 == d2
    finally:
        gwtools.reset_gateways()


# ======================================================================
# 2. 取不到实例就抛 —— 不许兜底成默认网关
# ======================================================================
def test_unknown_gateway_name_is_fail_closed(gateway):
    """ToolPort 侧收到没登记过的名字：**抛 LookupError**，不悄悄用默认网关。

    自动兜底会把「忘了注册网关」变成「悄悄用了一个空账本的 mock」——
    幂等、轮询次数、错误注入全部失真，而表面上一路绿灯。
    """
    with pytest.raises(LookupError, match="没有登记名为"):
        invoke_tool(GATEWAY_REFUND_PORT, {
            "gateway_name": "没这个网关", "out_trade_no": "T-1",
            "refund_amount": "1.00", "idempotency_key": "R-1"},
            extras={"event_id": "e1", "plan_id": "p"})

    with pytest.raises(LookupError, match="没有登记名为"):
        invoke_tool(GATEWAY_QUERY_PORT,
                    {"gateway_name": "没这个网关", "request_id": "gw_x"},
                    extras={"event_id": "e2", "plan_id": "p"})

    # 空名字同样不许滑到某个默认值上。
    with pytest.raises(LookupError):
        invoke_tool(GATEWAY_QUERY_PORT, {"gateway_name": "", "request_id": "gw_x"},
                    extras={"event_id": "e3", "plan_id": "p"})


def test_unregistered_gateway_in_skill_is_fail_closed(invoker, store, gateway):
    """skill 侧同样 fail-closed：payload 给个没登记的名字，付款不许发生。"""
    _ready_case(invoker, store)
    res = invoker.invoke("payment.execute", {
        "tenant_id": s6.TENANT_ID, "case_id": s6.CASE_ID, "gateway": "没这个网关",
    }, extras=_extras())

    assert res.status == "failed"
    assert "没有登记名为" in (res.error or ""), res.error
    assert not _tool_rows(store, "gateway.refund"), "根本不该走到网关调用"
    assert not objects.query(store, "SELECT * FROM refund_request WHERE case_id=?",
                             (s6.CASE_ID,)), "取不到网关却落了退款请求"


# ======================================================================
# 3. 支付语义一个字没改
# ======================================================================
def test_execute_receipt_stays_non_terminal(invoker, store, gateway):
    """端到端仍拿得到 receipt，且 `refund()` **没有返回终态**（铁律 8）。

    本轨只改参数怎么传，不改支付语义 —— `payment_execute.py` 里那条断言原样还在。
    """
    _ready_case(invoker, store)
    res = invoker.invoke("payment.execute", {
        "tenant_id": s6.TENANT_ID, "case_id": s6.CASE_ID, "gateway": TEST_GATEWAY,
    }, extras=_extras())
    assert res.status == "ok", res.error

    receipt = res.output["receipt"]
    assert receipt["request_id"]
    assert receipt["is_terminal"] is False, "refund() 返回了终态 —— 观察与推断分离没了落点"
    assert receipt["status"] != "settled"
    assert res.output["needs_query"] is True

    case = guard.get_case(store, s6.TENANT_ID, s6.CASE_ID)
    assert case["biz_status"] == "processing"
    assert case["biz_status"] != "settled", "发起方不许宣布成功"

    # 落库的 gateway 列存的是名字，不是对象 —— 表结构没变，写进去的东西也没变。
    rows = objects.query(store, "SELECT gateway FROM refund_request WHERE case_id=?",
                         (s6.CASE_ID,))
    assert rows and rows[0]["gateway"] == TEST_GATEWAY


def test_observe_passes_only_name_and_polling_is_unchanged(invoker, store, gateway,
                                                           monkeypatch):
    """`payment.observe` 侧同样只传名字，且轮询行为未变。"""
    from maos.skills.builtin.refund import payment_observe as po

    _ready_case(invoker, store)
    sent = invoker.invoke("payment.execute", {
        "tenant_id": s6.TENANT_ID, "case_id": s6.CASE_ID, "gateway": TEST_GATEWAY,
    }, extras=_extras())
    assert sent.status == "ok", sent.error

    seen = _capture_params(monkeypatch, po)
    res = invoker.invoke("payment.observe", {
        "tenant_id": s6.TENANT_ID, "case_id": s6.CASE_ID, "gateway": TEST_GATEWAY,
        "request_id": sent.output["request_id"],
    }, extras=_extras())
    assert res.status == "ok", res.error

    assert seen, "payment.observe 一次网关都没问"
    for params in seen:
        assert set(params) == {"gateway_name", "request_id"}
        assert params["gateway_name"] == TEST_GATEWAY
        json.dumps(params)                      # 全标量，有活对象这里就抛

    # 轮询次数一个不多一个不少：终态是问出来的，不是推断出来的。
    assert res.output["settled"] is True
    assert res.output["poll_count"] == s6.SETTLE_AFTER, (
        f"settle_after={s6.SETTLE_AFTER} 时应问 {s6.SETTLE_AFTER} 次，"
        f"实际 {res.output['poll_count']} 次 —— 轮询行为被改动了")
    assert len(seen) == s6.SETTLE_AFTER
    assert guard.get_case(store, s6.TENANT_ID, s6.CASE_ID)["biz_status"] == "settled"
