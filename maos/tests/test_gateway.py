"""Task-R3 的机器验收 —— 支付网关 ToolPort。

这一轨的铁律是 **8（权威事实）**：MAOS 不持有退款的权威状态，只持有观察与推断。
所以下面的断言分两类，第二类才是重点：

  · 形状类：错误码的 retriable / outcome 标注与官方文档一致
  · **时序类**：``refund()`` 永远拿不到终态，终态只能 ``query()`` 问出来

几条钉死的反例，每条对应一种「看起来绿、上真网关就出事」的失败形态：

  · ``refund()`` 一步返回 settled —— 那样 ``payment.observe`` 就没有存在理由，
    「观察与推断分离」整个论证被抽空。这里用 ``is_terminal`` 直接锁死。
  · 把 ``outcome=unknown`` 当成失败 —— 退款可能**已经发生**，当失败处理就会重发，
    重发就是第二笔真金白银。所以 unknown 必须与 failed 严格分开断言。
  · 同 ``idempotency_key`` 重复调用产生第二笔 —— 用 ``refund_count`` 锁死。
  · 未知错误码被就地兜底成「可重试」—— ``lookup()`` 必须抛，不许返回默认值。
"""

from __future__ import annotations

import uuid

import pytest

from maos.core.store import SqliteStore
from maos.tools import gateway_codes as C
from maos.tools.gateway import (
    GATEWAY_QUERY_PORT,
    GATEWAY_REFUND_PORT,
    STATUS_FAILED,
    STATUS_PROCESSING,
    STATUS_SETTLED,
    STATUS_UNKNOWN,
    TERMINAL_STATUSES,
    AlipaySandboxAdapter,
    MockGateway,
    RefundRequest,
)
from maos.tools.port import invoke_tool


def _req(**kw) -> RefundRequest:
    base = {"out_trade_no": "T-0001", "refund_amount": "12.00",
            "idempotency_key": "R-0001", "reason": "七天无理由"}
    base.update(kw)
    return RefundRequest(**base)


def _store():
    s = SqliteStore()
    s.init_schema()
    return s


# ---------------------------------------------------------------------------
# 1. 五类错误码：形状与 retriable 标注
# ---------------------------------------------------------------------------

def test_required_five_categories_all_present_with_sources():
    """派单点名的五类必须全部在表内，且每条都带出处。

    出处这一条是硬要求：核不到出处的码不许写进去，被问「这个码哪来的」要答得上。
    """
    for category, codes in C.REQUIRED_CATEGORIES.items():
        assert codes, f"{category} 没有对应码"
        for code in codes:
            entry = C.lookup(code)                       # 不在表内会直接抛
            assert entry.source, f"{category}/{code} 缺出处"
            assert entry.source.startswith("https://"), f"{category}/{code} 出处不是 URL"
            assert entry.message, f"{category}/{code} 缺官方描述"
            assert entry.remedy, f"{category}/{code} 缺官方解决方案"


@pytest.mark.parametrize("code,retriable,outcome", [
    # 系统错误：官方 remedy「保持参数不变重试或查询执行结果」-> 可重试，但结果未知
    ("ACQ.SYSTEM_ERROR", True, C.OUTCOME_UNKNOWN),
    # 交易不存在：官方 remedy「检查交易号」-> 要改参数，不是重试
    ("ACQ.TRADE_NOT_EXIST", False, C.OUTCOME_FAILED),
    # 重复请求不一致：官方 remedy 含「或查询历史执行结果」-> 前一笔下落未知
    ("ACQ.DISCORDANT_REPEAT_REQUEST", False, C.OUTCOME_UNKNOWN),
    # 余额不足：官方 remedy「商户账户充值后重新发起」-> 要人工介入
    ("ACQ.SELLER_BALANCE_NOT_ENOUGH", False, C.OUTCOME_FAILED),
    # 渠道繁忙（网关层）：20000 结果未知，40005 在入口被拒、确定没执行
    ("20000", True, C.OUTCOME_UNKNOWN),
    ("40005", True, C.OUTCOME_FAILED),
])
def test_code_table_matches_official_remedy(code, retriable, outcome):
    """retriable 与 outcome 按官方 remedy 原文定，不按语感。

    派单点名：「渠道繁忙可重试、交易不存在不可重试」标反了，
    replan 就会在不该重试的地方自旋。
    """
    entry = C.lookup(code)
    assert entry.retriable is retriable, f"{code} 的 retriable 与官方 remedy 不符"
    assert entry.outcome == outcome, f"{code} 的 outcome 与官方 remedy 不符"


def test_retriable_and_outcome_are_orthogonal():
    """四象限都要有样本 —— 只有一个 retriable bool 是不够的。

    最要紧的是 retriable=True + unknown 这一档：**可以重试，但不许直接重试**，
    必须先 query 确认前一笔的下落，否则就是第二笔退款。
    """
    quadrants = {(c.retriable, c.outcome == C.OUTCOME_UNKNOWN) for c in C.ALL_CODES.values()}
    assert (True, True) in quadrants, "缺「可重试但结果未知」的样本"
    assert (True, False) in quadrants, "缺「可重试且确定没执行」的样本"
    assert (False, False) in quadrants, "缺「不可重试且确定失败」的样本"
    assert (False, True) in quadrants, "缺「不可重试且结果未知」的样本（最危险的一档）"

    # 这两条是「重试前必须先查」的，直接重发就可能产生第二笔
    assert C.needs_query_before_retry("ACQ.SYSTEM_ERROR")
    assert C.needs_query_before_retry("20000")
    # 限流是在网关入口被拒的，业务没执行，可以直接重发
    assert not C.needs_query_before_retry("40005")


def test_unknown_code_raises_instead_of_defaulting():
    """未知码必须抛，不许兜底成「默认可重试」。

    兜底的后果不是报错，是**把没核过出处的码当成已知码处理** —— 正是这张表要防的事。
    """
    with pytest.raises(KeyError, match="未知网关错误码"):
        C.lookup("ACQ.NOT_A_REAL_CODE")
    with pytest.raises(KeyError):
        C.is_retriable("ACQ.NOT_A_REAL_CODE")


def test_code_table_rejects_entry_without_source():
    """构造一条没出处的码就该炸 —— 出处是硬约束，不是文档习惯。"""
    with pytest.raises(ValueError, match="没有出处"):
        C.GatewayCode(code="ACQ.X", message="x", retriable=False,
                      outcome=C.OUTCOME_FAILED, remedy="x",
                      layer=C.LAYER_BUSINESS, source="")


# ---------------------------------------------------------------------------
# 2. 异步时序：refund() 永远拿不到终态
# ---------------------------------------------------------------------------

def test_refund_never_returns_terminal_status():
    """**本文件最重要的一条断言。**

    一步返回 settled 的 mock 会把「观察与推断分离」抽空 —— 那样
    ``payment.observe`` 就没有存在理由了。
    """
    gw = MockGateway()
    r = gw.refund(_req())

    assert r.status == STATUS_PROCESSING
    assert r.status not in TERMINAL_STATUSES
    assert r.is_terminal is False
    assert r.needs_query is True
    assert r.status != STATUS_SETTLED
    assert r.status != STATUS_FAILED


def test_query_polls_until_terminal():
    """终态只能问出来，而且**一次不一定够**。"""
    gw = MockGateway(settle_after=3)
    r = gw.refund(_req())
    assert not r.is_terminal

    seen = []
    for _ in range(2):                    # 前两次仍是处理中
        r = gw.query(r.request_id)
        seen.append(r.status)
        assert not r.is_terminal, "settle_after=3，前两次不该到终态"

    r = gw.query(r.request_id)            # 第三次才结算
    assert r.status == STATUS_SETTLED
    assert r.is_terminal is True
    assert r.outcome == C.OUTCOME_SUCCESS
    assert seen == [STATUS_PROCESSING, STATUS_PROCESSING]

    # poll_count 是审计证据：证明终态是**问出来的**，不是本地推断的
    assert r.poll_count == 3


def test_settle_after_must_be_at_least_one():
    """不允许配一个「零轮询即终态」的 mock —— 那等于关掉异步时序。"""
    with pytest.raises(ValueError, match="不允许一步到终态"):
        MockGateway(settle_after=0)


def test_unknown_status_is_not_terminal_and_not_failure():
    """``ACQ.SYSTEM_ERROR`` 是「说不清」，不是「失败」。

    把它当失败处理 -> 上层重发 -> 第二笔退款。这是本模块防的一号事故。
    """
    gw = MockGateway(script={"T-SYS": "ACQ.SYSTEM_ERROR"})
    r = gw.refund(_req(out_trade_no="T-SYS", idempotency_key="R-SYS"))

    assert r.status == STATUS_UNKNOWN
    assert r.status != STATUS_FAILED, "unknown 不等于 failed —— 退款可能已经发生"
    assert r.is_terminal is False
    assert r.outcome == C.OUTCOME_UNKNOWN
    assert r.retriable is True
    assert C.needs_query_before_retry(r.code), "这一档必须先 query 再决定"

    # 问出来之后才有确定下落
    final = gw.query(r.request_id)
    while not final.is_terminal:
        final = gw.query(final.request_id)
    assert final.is_terminal
    assert final.detail.get("resolved_from") == "ACQ.SYSTEM_ERROR"


def test_terminal_failure_codes_settle_immediately():
    """明确失败的码（交易不存在 / 余额不足）网关当场就能判，不用等轮询。

    与上一条对照：**失败可以当场判，成功不行** —— 成功是异步的。
    """
    for trade_no, key, code in [
        ("T-NOTEXIST", "R-NE", "ACQ.TRADE_NOT_EXIST"),
        ("T-NOBALANCE", "R-NB", "ACQ.SELLER_BALANCE_NOT_ENOUGH"),
    ]:
        gw = MockGateway(script={trade_no: code})
        r = gw.refund(_req(out_trade_no=trade_no, idempotency_key=key))
        assert r.status == STATUS_FAILED
        assert r.is_terminal is True
        assert r.outcome == C.OUTCOME_FAILED
        assert r.retriable is False
        assert r.code == code
        assert r.source.startswith("https://"), "回执要带出处，评委当场能核"


def test_mock_rejects_unscripted_code_at_construction():
    """错误注入用的码也要在表内 —— 构造时就炸，不留到调用时。"""
    with pytest.raises(KeyError, match="未知网关错误码"):
        MockGateway(script={"T-X": "ACQ.MADE_UP"})


# ---------------------------------------------------------------------------
# 3. 幂等
# ---------------------------------------------------------------------------

def test_same_idempotency_key_does_not_create_second_refund():
    """同 ``idempotency_key`` 重复调用**不产生第二笔**。"""
    gw = MockGateway()
    first = gw.refund(_req())
    again = gw.refund(_req())

    assert gw.refund_count == 1, "重复请求产生了第二笔退款"
    assert again.request_id == first.request_id
    assert again.status == first.status
    # 重复调用不推进轮询 —— refund 不是观察动作，只有 query 才是
    assert again.poll_count == first.poll_count == 0


def test_duplicate_key_with_different_params_is_inconsistent_and_unknown():
    """同 key 但参数不一致 -> ``ACQ.DISCORDANT_REPEAT_REQUEST``，且 outcome 是 unknown。

    unknown 而非 failed 的理由在官方 remedy 里：「或查询历史执行结果」——
    之前那一笔可能已经成功了。当 failed 处理会让上层换个单号重发，那就真退了两笔。
    """
    gw = MockGateway()
    first = gw.refund(_req())
    clash = gw.refund(_req(refund_amount="99.00"))      # 同 key，金额变了

    assert clash.code == "ACQ.DISCORDANT_REPEAT_REQUEST"
    assert clash.outcome == C.OUTCOME_UNKNOWN
    assert clash.status == STATUS_UNKNOWN
    assert clash.is_terminal is False
    assert clash.detail["duplicate_of"] == first.request_id
    assert gw.refund_count == 1, "参数不一致的重复请求也不该新建第二笔"


def test_reason_change_alone_is_not_a_param_conflict():
    """只改退款理由不算参数不一致 —— 它不影响资金结果。"""
    gw = MockGateway()
    first = gw.refund(_req())
    same = gw.refund(_req(reason="换个说法"))

    assert same.request_id == first.request_id
    assert same.code != "ACQ.DISCORDANT_REPEAT_REQUEST"
    assert gw.refund_count == 1


def test_refund_requires_idempotency_key():
    gw = MockGateway()
    with pytest.raises(ValueError, match="idempotency_key"):
        gw.refund(_req(idempotency_key=""))


def test_distinct_keys_create_distinct_refunds():
    """幂等不能矫枉过正：不同 key 就是两笔，该建还得建。"""
    gw = MockGateway()
    a = gw.refund(_req(idempotency_key="R-A"))
    b = gw.refund(_req(idempotency_key="R-B"))
    assert a.request_id != b.request_id
    assert gw.refund_count == 2


# ---------------------------------------------------------------------------
# 4. 审计：ToolInvoked 落库
# ---------------------------------------------------------------------------

def test_invoke_tool_writes_tool_invoked_row():
    """调用走 ``invoke_tool`` 才有审计行；invocation_id 由 extras 带入。

    口径说明（DECISIONS R3-02）：派单说「返回 invocation_id」，而 ``port.py`` 是
    冻结面（A-6），``invoke_tool`` 返回的是 entry 的返回值。所以调用标识落在
    ``extras["event_id"]`` 上 —— 不改冻结签名，审计仍然可追。
    """
    store, gw = _store(), MockGateway()
    plan_id, invocation_id = "plan-r3", uuid.uuid4().hex

    out = invoke_tool(
        GATEWAY_REFUND_PORT,
        {"gateway": gw, "out_trade_no": "T-0001", "refund_amount": "12.00",
         "idempotency_key": "R-0001"},
        store=store,
        extras={"event_id": invocation_id, "plan_id": plan_id, "trace_id": "tr-1"},
    )

    assert out["status"] == STATUS_PROCESSING
    assert out["is_terminal"] is False

    rows = [r for r in store.list_event_log(plan_id) if r["event_type"] == "ToolInvoked"]
    assert len(rows) == 1
    row = rows[0]
    assert row["event_id"] == invocation_id and row["event_id"] != ""
    assert row["detail"]["tool"] == "gateway.refund"
    assert row["detail"]["status"] == "ok"
    assert row["detail"]["params_digest"], "缺 params_digest，出事之后查不到参数"
    assert row["detail"]["error"] is None


def test_params_digest_is_stable_across_calls():
    """同样的参数要算出同样的 digest —— 否则审计对不上账。

    这条盯的是 ``MockGateway.__repr__``：带内存地址的话每次 digest 都不同。
    """
    store, gw = _store(), MockGateway()
    params = {"gateway": gw, "out_trade_no": "T-0001", "refund_amount": "12.00",
              "idempotency_key": "R-0001"}
    for _ in range(2):
        invoke_tool(GATEWAY_REFUND_PORT, dict(params), store=store,
                    extras={"event_id": uuid.uuid4().hex, "plan_id": "p"})

    digests = {r["detail"]["params_digest"]
               for r in store.list_event_log("p") if r["event_type"] == "ToolInvoked"}
    assert len(digests) == 1, "同样参数算出了不同的 digest"


def test_query_via_invoke_tool_also_audited():
    store, gw = _store(), MockGateway(settle_after=1)
    r = gw.refund(_req())
    out = invoke_tool(GATEWAY_QUERY_PORT, {"gateway": gw, "request_id": r.request_id},
                      store=store, extras={"event_id": "e2", "plan_id": "p"})

    assert out["status"] == STATUS_SETTLED
    assert out["poll_count"] == 1
    names = [row["detail"]["tool"] for row in store.list_event_log("p")
             if row["event_type"] == "ToolInvoked"]
    assert names == ["gateway.query"]


def test_tool_error_is_audited_then_reraised():
    """工具抛异常时**先落审计再原样抛出** —— 不吞成 None。

    ``port.py`` 已有此语义，这里是回归：真网关超时的那天，审计行必须还在。
    """
    store = _store()
    with pytest.raises(NotImplementedError, match="尚未接通支付宝沙箱"):
        invoke_tool(
            GATEWAY_REFUND_PORT,
            {"gateway": AlipaySandboxAdapter(), "out_trade_no": "T-1",
             "refund_amount": "1.00", "idempotency_key": "R-1"},
            store=store,
            extras={"event_id": "e-fail", "plan_id": "p"},
        )

    rows = [r for r in store.list_event_log("p") if r["event_type"] == "ToolInvoked"]
    assert len(rows) == 1, "异常路径没落审计行"
    assert rows[0]["detail"]["status"] == "failed"
    assert "NotImplementedError" in rows[0]["detail"]["error"]


# ---------------------------------------------------------------------------
# 5. 沙箱适配层：只留壳，且是显式的
# ---------------------------------------------------------------------------

def test_sandbox_adapter_raises_explicitly_not_silently_faking():
    """壳必须显式抛，**不许静默返回假数据**。

    一个「看起来返回了点什么」的桩会让上层以为接通了，比没实现危险得多。
    """
    ad = AlipaySandboxAdapter()
    with pytest.raises(NotImplementedError, match="尚未接通"):
        ad.refund(_req())
    with pytest.raises(NotImplementedError, match="尚未接通"):
        ad.query("gw_whatever")


def test_sandbox_adapter_signature_matches_mock():
    """签名与 MockGateway 一致 —— 沙箱通了就切，上层一行不用改。"""
    import inspect

    for name in ("refund", "query"):
        assert (inspect.signature(getattr(AlipaySandboxAdapter, name)) ==
                inspect.signature(getattr(MockGateway, name))), f"{name} 签名不一致"


def test_sandbox_adapter_never_leaks_private_key_in_repr():
    """密钥不进 repr —— 与 model/client.py 的 GatewayModelClient 同口径。"""
    ad = AlipaySandboxAdapter(app_id="2021000000000000",
                              private_key="MIIEvQIBADANBgkqhkiG9w0BA-SECRET")
    assert "SECRET" not in repr(ad)
    assert "MIIEvQ" not in repr(ad)
    assert not hasattr(ad, "private_key"), "私钥应挂在私有属性上"


# ---------------------------------------------------------------------------
# 6. 回执本身是观察记录，不是事实
# ---------------------------------------------------------------------------

def test_receipt_is_immutable():
    """回执 frozen —— 改它等于篡改「网关当时说了什么」。"""
    gw = MockGateway()
    r = gw.refund(_req())
    with pytest.raises(Exception):
        r.status = STATUS_SETTLED           # type: ignore[misc]


def test_receipt_carries_source_for_every_code():
    """每张回执都带错误码出处，评委问「这个码哪来的」当场能答。"""
    gw = MockGateway(script={"T-SYS": "ACQ.SYSTEM_ERROR"})
    for trade_no, key in [("T-0001", "R-1"), ("T-SYS", "R-2")]:
        r = gw.refund(_req(out_trade_no=trade_no, idempotency_key=key))
        assert r.source in C.SOURCES
        assert r.remedy, "回执要带官方处置建议，直接进 findings 给人看"
