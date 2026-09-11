"""T124 —— 同渠道重试不留悬空引用，以及 MockGateway 的「失败 N 次后改判」注入。

## 这一组测试买的是什么

两件事，它们在同一条路上：

**一、摘旧引用的判据。** `payment.execute` 每发起一次退款就按当时的版本挂一条
`business_ref`，发起之前先摘掉上一笔的。原先那条 DELETE 只比 `object_id`，
于是：

  · **换渠道**重发 —— `request_id` 变了，id 不同，摘得掉。看着是对的。
  · **同渠道**重试（闸判返工 -> requeue -> 同一个幂等键重发）—— 网关按幂等
    原样返回**同一笔**，`request_id` 一个字没变，而 `next_version` 原地 1->2->3。
    id 相同，一条都摘不掉；`business_ref` 主键含 `object_version`
    （`domain/refund/schema.sql`），v1/v2 就这么合法地留了下来，指着一个已经
    变成 v3 的对象。`resolve_business_ref` 对本表按 `AND version=?` 收窄，
    当场全部指不到。

症状是**静默的**：付款照跑、补偿照落，只有 `scripts/verify.py` 的 business-ref
那一项会数出悬空。所以这里把它钉成单测 —— 判据改回去，第 1 条立刻红。

**二、注入选项。** 没有「失败 N 次后改判」，同渠道重试在 mock 上根本演不出来：
账本原样返回上一次的观察，重试多少趟都是同一个失败，Trace 上只剩「试到次数耗尽」。
`evidence/case-real-01/gateway_fail` 那条 `REWORK` 就是靠它长出来的。

注入只在**一格**里安全，第 5、6 条守的就是这条边界：只有
`retriable=True + outcome=failed`（网关在入口拒了、**业务确定没执行**）才许在
同一个 `out_request_no` 上改判重发。别的格子重发可能造出第二笔退款（铁律 8）。
"""

from __future__ import annotations

import pytest

from maos.agents.base import AgentIdentity
from maos.domain.refund import objects
from maos.flows import scenario_6 as s6
from maos.model.client import Tier
from maos.skills.builtin.refund import REFUND_SKILLS
from maos.skills.builtin.refund import _common as C
from maos.skills.invoker import SkillInvoker
from maos.core.store import SqliteStore
from maos.tools.gateway import MockGateway, RefundRequest

TEST_GATEWAY = "t124-gw"
OTHER_GATEWAY = "t124-gw-alt"

ALL_SKILLS_IDENTITY = AgentIdentity(
    agent_id="test-t124",
    role="test_refund",
    duty="测试夹具：授权退款域全部 skill",
    allowed_skills=frozenset(set(REFUND_SKILLS) | {"issue.aggregate"}),
    allowed_tools=frozenset({"gateway.refund", "gateway.query"}),
    write_scope=frozenset({"artifact"}),
    max_risk="M",
    model_tier=Tier.LIGHT,
)

PLAN_ID = "plan-t124"
TASK_ID = "task-t124-payment"


@pytest.fixture
def store():
    st = SqliteStore()
    st.init_schema()
    s6.seed_domain(st)
    return st


@pytest.fixture
def gateways():
    """两个网关：同渠道重试用前一个，换渠道那条用后一个。"""
    C.reset_gateways()
    C.register_gateway(TEST_GATEWAY, MockGateway(settle_after=s6.SETTLE_AFTER))
    C.register_gateway(OTHER_GATEWAY, MockGateway(settle_after=s6.SETTLE_AFTER))
    yield
    C.reset_gateways()


@pytest.fixture
def invoker(store):
    return SkillInvoker(ALL_SKILLS_IDENTITY, store)


def _extras(**over) -> dict:
    base = {"plan_id": PLAN_ID, "task_id": TASK_ID, "trace_id": "trace-t124", "attempt": 1}
    base.update(over)
    return base


def _ready(invoker, store) -> None:
    """把案子推到「可以发起付款」那一格：受理 -> 裁定 -> 核算 -> 主管审批。"""
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
                      approver="测试主管", decision="approved", reason="单测放行")


def _execute(invoker, *, gateway: str = TEST_GATEWAY):
    res = invoker.invoke("payment.execute", {
        "tenant_id": s6.TENANT_ID, "case_id": s6.CASE_ID, "gateway": gateway,
    }, extras=_extras())
    assert res.status == "ok", res.error
    return res.output


def _request_refs(store) -> list[dict]:
    return [r for r in objects.list_business_refs(store, plan_id=PLAN_ID, task_id=TASK_ID)
            if r["object_type"] == "refund_request"]


def _req(out_trade_no: str = s6.ORDER_ID, *, key: str = "t124-key",
         amount: str = "100.00") -> RefundRequest:
    return RefundRequest(out_trade_no=out_trade_no, refund_amount=amount,
                         idempotency_key=key, reason="单测")


# ======================================================================
# 一、摘旧引用的判据：两种重发都要收干净
# ======================================================================
def test_same_channel_retry_leaves_exactly_one_resolvable_ref(invoker, store, gateways):
    """同渠道重试：`request_id` 不变、版本 1->2，引用必须**恰好一条**且指得到。

    这是 T124 修的那条。DELETE 的判据退回只比 `object_id` 的话，v1 会留下来，
    而表上已经是 v2 —— 两条引用里有一条当场悬空，且**不报错**。
    """
    _ready(invoker, store)
    first = _execute(invoker)
    second = _execute(invoker)

    assert first["request_id"] == second["request_id"], (
        "同一个幂等键必须还是同一笔退款 —— 变了说明幂等被破坏，本用例的前提就没了")

    rows = objects.query(store, "SELECT * FROM refund_request WHERE tenant_id=? AND case_id=?",
                         (s6.TENANT_ID, s6.CASE_ID))
    assert len(rows) == 1, "一个案子只允许有一笔退款请求（UNIQUE 幂等键）"
    assert rows[0]["version"] == 2, "第二次发起必须把版本推到 2，否则测不到这条缺陷"

    refs = _request_refs(store)
    assert len(refs) == 1, (
        f"同渠道重试后 refund_request 的引用应恰好一条，实际 {len(refs)} 条："
        f"{[(r['object_id'], r['object_version']) for r in refs]} —— "
        f"多出来的那条指着已经被改版的对象，是悬空引用")
    assert refs[0]["object_version"] == 2
    assert objects.resolve_business_ref(store, refs[0]) is not None, (
        "留下来的那条引用必须指得到当前那一份（resolve 返回 None 是静默失效）")


def test_switch_channel_resend_still_leaves_one_ref(invoker, store, gateways):
    """换渠道重发：既有行为一个字不变 —— 仍然恰好一条，且指向新的 request_id。"""
    _ready(invoker, store)
    first = _execute(invoker)
    second = _execute(invoker, gateway=OTHER_GATEWAY)

    assert first["request_id"] != second["request_id"], (
        "换了渠道就是另一个网关的账本，request_id 必须是新的")

    refs = _request_refs(store)
    assert len(refs) == 1, (
        f"换渠道重发后应只剩一条引用，实际 {len(refs)} 条 —— 旧渠道那条没摘掉")
    assert refs[0]["object_id"] == second["request_id"]
    assert objects.resolve_business_ref(store, refs[0]) is not None


# ======================================================================
# 二、MockGateway 的注入选项
# ======================================================================
def test_fail_times_then_succeeds_on_retry():
    """失败 N 次后改判：前 N 次落注入的码，第 N+1 次受理，终态仍要 query 问。"""
    gw = MockGateway(settle_after=2, script={s6.ORDER_ID: "40005"},
                     fail_times={s6.ORDER_ID: 1})
    first = gw.refund(_req())
    assert first.status == "failed" and first.code == "40005"

    second = gw.refund(_req())
    assert second.status == "processing", (
        "注入次数用完之后这一次必须被受理 —— 还是 failed 说明改判没生效")
    assert second.code == "10000"
    assert second.request_id == first.request_id, (
        "改判改的是同一笔请求的下落，**不是新开一笔**（幂等键就是 out_request_no）")
    assert gw.refund_count == 1
    assert not second.is_terminal, "refund() 永远不返回终态"

    # 终态照旧只能问出来，注入选项不许在这条上开口子。
    assert gw.query(first.request_id).status == "processing"
    assert gw.query(first.request_id).status == "settled"


def test_fail_times_zero_is_the_same_as_no_injection():
    """N=0 等于不注入：第一次就落改判后的码，注入的那个码一次都不出现。"""
    injected = MockGateway(settle_after=2, script={s6.ORDER_ID: "40005"},
                           fail_times={s6.ORDER_ID: 0})
    plain = MockGateway(settle_after=2)

    first = injected.refund(_req())
    assert first.status == "processing" and first.code == "10000"
    assert first.status == plain.refund(_req()).status


def test_after_fail_can_switch_to_a_terminal_code():
    """改判的码可以是终态失败码 —— `evidence/case-real-01/gateway_fail` 演的就是它。

    改判**只发生一次**：第三次重发落回幂等返回，不会在两个码之间翻烧饼。
    """
    gw = MockGateway(settle_after=2, script={s6.ORDER_ID: "40005"},
                     fail_times={s6.ORDER_ID: 1},
                     after_fail={s6.ORDER_ID: "ACQ.SELLER_BALANCE_NOT_ENOUGH"})
    assert gw.refund(_req()).code == "40005"
    second = gw.refund(_req())
    assert second.code == "ACQ.SELLER_BALANCE_NOT_ENOUGH"
    assert second.status == "failed"
    assert gw.refund(_req()).code == "ACQ.SELLER_BALANCE_NOT_ENOUGH", (
        "改判后的码是终态失败，第三次重发必须原样返回它，不许再翻回 40005")
    assert gw.refund_count == 1


def test_injection_still_refuses_codes_outside_the_table():
    """码表不认的码照旧当场拒 —— 注入选项不许成为绕开 `lookup` 的后门。"""
    with pytest.raises(KeyError):
        MockGateway(script={s6.ORDER_ID: "ACQ.NOT_A_REAL_CODE"})
    with pytest.raises(KeyError):
        MockGateway(script={s6.ORDER_ID: "40005"}, fail_times={s6.ORDER_ID: 1},
                    after_fail={s6.ORDER_ID: "ACQ.NOT_A_REAL_CODE"})


def test_injection_refuses_unsafe_and_incoherent_config():
    """构造时就把不安全 / 说不通的注入拒掉，不留到调用时才炸。

    最要紧的是第一条（铁律 8）：`outcome=unknown` 的码表示「网关自己都说不清这一笔
    到底执行了没有」，在同一个 `out_request_no` 上改判重发就可能造出第二笔退款。
    """
    for unsafe in ("ACQ.SYSTEM_ERROR", "20000", "ACQ.SELLER_BALANCE_NOT_ENOUGH"):
        with pytest.raises(ValueError, match="铁律 8"):
            MockGateway(script={s6.ORDER_ID: unsafe}, fail_times={s6.ORDER_ID: 1})

    with pytest.raises(ValueError, match="不许为负"):
        MockGateway(script={s6.ORDER_ID: "40005"}, fail_times={s6.ORDER_ID: -1})

    with pytest.raises(ValueError, match="script 里没有它的码"):
        MockGateway(script={}, fail_times={s6.ORDER_ID: 1})

    with pytest.raises(ValueError, match="没配 fail_times"):
        MockGateway(script={s6.ORDER_ID: "40005"},
                    after_fail={s6.ORDER_ID: "ACQ.SELLER_BALANCE_NOT_ENOUGH"})


def test_plain_script_gateway_behaviour_is_untouched():
    """不配 `fail_times` 的网关：重发恒返回同一个失败观察，一个字节没变。"""
    gw = MockGateway(settle_after=2, script={s6.ORDER_ID: "40005"})
    first, second, third = gw.refund(_req()), gw.refund(_req()), gw.refund(_req())
    assert {r.code for r in (first, second, third)} == {"40005"}
    assert {r.status for r in (first, second, third)} == {"failed"}
    assert gw.refund_count == 1
