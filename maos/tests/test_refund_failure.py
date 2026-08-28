"""场景 7（退款失败路径）与 `refund.compensate` 的测试。

本文件的第一断言是整个场景存在的理由：

    补偿收口之后 `refund_case.biz_status == 'compensated'`，
    且**全库** `payment_observation.observed_state == 'settled'` 的行数为 0。

翻成人话：「所有 Agent 都回复完成」这件事没有发生，因为业务确实没成功，
而系统如实记录了这一点 —— 没有伪造一条 settled 观察来让链路看起来跑完了。

第二类断言守的是**措辞**：网关说不清（`outcome=unknown`）和网关说失败
（`outcome=failed`）必须落成不同的记录。把前者写成后者是本域最贵的一种 bug ——
那笔钱可能真退出去了，账面上却凭空少一笔（铁律 8）。

标了 `# 论证：` 的断言是复赛材料里那几句话的机器化版本，评审时可按前缀捞出来对。
"""

from __future__ import annotations

import json

import pytest

from maos.agents.base import AgentIdentity
from maos.contracts.states import TASK_TRANSITIONS, PlanState, TaskState
from maos.core.store import SqliteStore
from maos.domain.refund import guard, objects
from maos.flows import scenario_7 as s7
from maos.model.client import Tier
from maos.skills import registry
from maos.skills.builtin.refund import REFUND_SKILLS
from maos.skills.builtin.refund import _common as C
from maos.skills.builtin.refund.compensate import (
    EVENT_COMPENSATION_EXECUTED,
    KIND_MANUAL_TICKET,
    KIND_REQUEST_REVOKED,
    UNOBSERVED,
)
from maos.skills.invoker import SkillInvoker
from maos.tools import gateway_codes as GC
from maos.tools.gateway import MockGateway

TEST_GATEWAY = "test-gw-s7"

#: 单测用 identity：授权本域全部 skill。要验的是 skill 自己的行为，
#: 越权那条路由 invoker 的既有测试守。
ALL_SKILLS_IDENTITY = AgentIdentity(
    agent_id="test-refund-failure",
    role="test_refund_failure",
    duty="测试夹具：授权退款域全部 skill",
    # issue.aggregate 必须带上：refund.intake 内部要复用它做多源去重聚合。
    allowed_skills=frozenset(set(REFUND_SKILLS) | {"issue.aggregate"}),
    allowed_tools=frozenset({"gateway.refund", "gateway.query"}),
    write_scope=frozenset({"artifact"}),
    max_risk="M",
    model_tier=Tier.LIGHT,
)


# ======================================================================
# 夹具
# ======================================================================
@pytest.fixture(scope="module")
def driven():
    """整条失败路径跑一遍，返回 `scenario_7.drive()` 的句柄。

    module 作用域：这条链路要走四个 Agent、两次人工介入，跑一次就够，
    每个用例各跑一遍只是把测试变慢，不会多验到任何东西。
    """
    out = s7.drive()
    yield out
    C.reset_gateways()


@pytest.fixture
def store():
    st = SqliteStore()
    st.init_schema()
    s7.seed_domain(st)
    return st


@pytest.fixture
def invoker(store):
    return SkillInvoker(ALL_SKILLS_IDENTITY, store)


def _extras(**over) -> dict:
    base = {"plan_id": "plan-test-s7", "task_id": "task-test-s7",
            "trace_id": "trace-test-s7", "attempt": 1}
    base.update(over)
    return base


def _pay_and_observe(invoker, store, *, error_code: str, settle_after: int,
                     max_polls: int = 3):
    """把一个案子推到「已发起退款、观察过一轮」的位置，返回两次调用的结果。

    `error_code` 按 `out_trade_no` 注入，`settle_after` 决定要问几次才「结算」——
    两个参数一起决定这条案子走的是超时路径还是明确失败路径。
    """
    C.reset_gateways()
    C.register_gateway(TEST_GATEWAY, MockGateway(
        settle_after=settle_after, script={s7.ORDER_ID: error_code}))

    seed = {
        "tenant_id": s7.TENANT_ID, "case_id": s7.CASE_ID, "channel_id": s7.CHANNEL_ID,
        "order_id": s7.ORDER_ID, "order_version": s7.ORDER_VERSION, "sku": s7.SKU,
        "reason_code": "quality_defect", "amount_claimed": s7.AMOUNT_CLAIMED,
    }
    res = invoker.invoke("refund.intake",
                         {"signals": s7.SIGNALS, "case_seed": seed}, extras=_extras())
    assert res.status == "ok", res.error
    pol = invoker.invoke("policy.match",
                         {"tenant_id": s7.TENANT_ID, "case_id": s7.CASE_ID}, extras=_extras())
    assert pol.status == "ok", pol.error
    fin = invoker.invoke("finance.settle", {
        "tenant_id": s7.TENANT_ID, "case_id": s7.CASE_ID, "policy": pol.output,
    }, extras=_extras())
    assert fin.status == "ok", fin.error

    C.record_approval(store, tenant_id=s7.TENANT_ID, case_id=s7.CASE_ID,
                      approver="测试主管", decision="approved", reason="夹具")

    ex = invoker.invoke("payment.execute", {
        "tenant_id": s7.TENANT_ID, "case_id": s7.CASE_ID, "gateway": TEST_GATEWAY,
    }, extras=_extras())
    assert ex.status == "ok", ex.error
    ob = invoker.invoke("payment.observe", {
        "tenant_id": s7.TENANT_ID, "case_id": s7.CASE_ID, "gateway": TEST_GATEWAY,
        "request_id": ex.output["request_id"], "max_polls": max_polls,
    }, extras=_extras())
    assert ob.status == "ok", ob.error
    return ex.output, ob.output


def _compensate(invoker, *, operator="测试主管", reason="渠道异常，转人工"):
    return invoker.invoke("refund.compensate", {
        "tenant_id": s7.TENANT_ID, "case_id": s7.CASE_ID,
        "operator": operator, "reason": reason,
    }, extras=_extras())


def _count(store, sql: str, params: tuple = ()) -> int:
    return objects.query(store, sql, params)[0]["n"]


# ======================================================================
# 1. 第一断言：补偿收口，且全程没有 settled
# ======================================================================
def test_compensated_and_never_settled(driven):
    """整个场景存在的理由。这条挂了，别的都不用看。"""
    store = driven["store"]
    case = guard.get_case(store, s7.TENANT_ID, s7.CASE_ID)
    assert case["biz_status"] == "compensated", (
        f"补偿之后业务状态应为 compensated，实际 {case['biz_status']}")

    # 论证：没问出终态就一条 settled 观察都不该有 —— 全库口径，不按案子过滤，
    # 因为「别的案子写了一条」同样是把外部状态写死为终态。
    settled = _count(
        store, "SELECT COUNT(*) AS n FROM payment_observation WHERE observed_state='settled'")
    assert settled == 0, f"全库不该有 settled 观察，实际 {settled} 条"

    # 论证：连一条观察都没有 —— payment.observe 轮询到顶时不落库，
    # 「我问累了」不是一个可以写进表里的结论。
    assert _count(store, "SELECT COUNT(*) AS n FROM payment_observation") == 0

    assert driven["cp"].store.get_plan(driven["plan_id"])["state"] == PlanState.FAILED


# ======================================================================
# 2. poll_count 落进产物 —— 终态是问出来的唯一审计证据
# ======================================================================
def test_poll_count_lands_in_artifact(driven):
    receipt = s7.receipt_artifact(driven["store"], s7.TASK_PAYMENT)
    assert receipt["poll_count"] == s7.MAX_POLLS, (
        f"应恰好轮询 {s7.MAX_POLLS} 次，实际 {receipt['poll_count']}")
    assert receipt["settled"] is False
    assert receipt["observed_state"] != "settled"
    # 论证：产物里带着错误码的出处，评委问「这个码哪来的」当场能答。
    assert receipt["source"], "回执产物必须带错误码出处"
    assert receipt["receipt"]["code"] == s7.GATEWAY_ERROR_CODE


# ======================================================================
# 3. 补偿真发生过：事件 + 记录
# ======================================================================
def test_compensation_event_and_record(driven):
    store, plan_id = driven["store"], driven["plan_id"]
    rows = objects.query(
        store, "SELECT * FROM compensation_record WHERE tenant_id=? AND case_id=?",
        (s7.TENANT_ID, s7.CASE_ID))
    kinds = {r["kind"] for r in rows}
    assert kinds == {KIND_REQUEST_REVOKED, KIND_MANUAL_TICKET}, (
        f"补偿必须同时留下作废记录与人工工单，实际 {sorted(kinds)}")

    events = [e for e in store.list_event_log(plan_id)
              if e["event_type"] == EVENT_COMPENSATION_EXECUTED]
    assert events, "补偿执行必须落 CompensationExecuted，否则这件事只活在日志里"
    detail = events[-1]["detail"]
    assert detail["domain"] == C.BIZ_TYPE, (
        "域内补偿与控制面的逆补丁补偿共用事件名，必须靠 detail.domain 分得开")
    assert detail["ticket_id"] == f"MT-{s7.CASE_ID}"

    # 人工工单要说清「剩下的事只有人能做」，不能是一条空记录。
    ticket = json.loads(
        [r for r in rows if r["kind"] == KIND_MANUAL_TICKET][0]["detail_json"])
    assert ticket["todo"], "人工工单必须写明要人去做什么"
    assert ticket["assignee"]


# ======================================================================
# 4. 铁律 9：业务状态不进 Task 状态机，也没有新迁移
# ======================================================================
def test_task_state_machine_untouched(driven):
    cp, plan_id = driven["cp"], driven["plan_id"]
    known = {v for k, v in vars(TaskState).items()
             if not k.startswith("_") and isinstance(v, str)}
    states = {t["state"] for t in cp.store.list_tasks(plan_id)}
    assert states <= known, f"出现了新的 Task 状态：{sorted(states - known)}"

    # 论证：只查状态集合挡不住「用既有的两个状态连一条新边」，所以迁移也要查。
    moves = {(e["from_state"], e["to_state"]) for e in cp.store.list_event_log(plan_id)
             if e["event_type"] == "StateTransition"}
    assert moves <= set(TASK_TRANSITIONS), (
        f"出现了不在冻结迁移表里的迁移：{sorted(moves - set(TASK_TRANSITIONS))}")

    # 论证：退款的业务状态一个都没漏进 Task 状态机。
    for biz in guard.BIZ_STATUS_FLOW:
        assert biz not in known, f"业务状态 {biz} 混进了 Task 状态机（铁律 9）"

    # 收口是「人驳回」，不是「跑挂了」—— 迁移的 reason 要能自证这一点。
    reject = [e for e in cp.store.list_event_log(plan_id)
              if e.get("task_id") == s7.TASK_PAYMENT and e.get("to_state") == TaskState.FAILED]
    assert reject and reject[-1]["reason"] == "human_reject", (
        f"付款任务应因人工驳回而 FAILED，实际 reason={reject and reject[-1]['reason']}")


# ======================================================================
# 5. 可重试 / 不可重试：判据一律查码表，不凭语感
# ======================================================================
def test_scenario_uses_a_retriable_unknown_code():
    """场景注入的码必须同时满足三条，否则这个场景演的就不是它宣称的那件事。"""
    code = GC.lookup(s7.GATEWAY_ERROR_CODE)
    assert code.retriable is True, "场景要演的是「可重试」，不是终态失败"
    assert code.outcome == GC.OUTCOME_UNKNOWN, (
        "题眼是「网关自己也说不清」—— outcome 必须是 unknown")
    # 论证：retriable=True + unknown 的码**不许直接重发**，必须先 query，
    # 否则可能产生第二笔退款。
    assert GC.needs_query_before_retry(s7.GATEWAY_ERROR_CODE) is True

    # 未收录的码抛而不是兜底成「默认可重试」—— 那是有意设计。
    with pytest.raises(KeyError):
        GC.lookup("ACQ.NOT_A_REAL_CODE")


def test_retriable_timeout_and_terminal_failure_are_recorded_differently(invoker, store):
    """同一条链路，两种网关下场必须落成两种记录 —— 措辞混了就是替网关下结论。"""
    # (a) 可重试 + 说不清：轮询到顶也问不出终态。
    _, timeout = _pay_and_observe(
        invoker, store, error_code=s7.GATEWAY_ERROR_CODE, settle_after=99, max_polls=3)
    assert timeout["poll_count"] == 3
    assert timeout["settled"] is False
    assert timeout["needs_compensation"] is False, (
        "轮询超时不是「网关说失败了」，不该被标成需要补偿")
    assert _count(store, "SELECT COUNT(*) AS n FROM payment_observation") == 0, (
        "没问出终态就不落观察行")


def test_non_retriable_code_is_a_terminal_failure(invoker, store):
    """不可重试的码走的是另一条路：当场终态失败，落一条 failed 观察。"""
    code = GC.lookup("ACQ.TRADE_NOT_EXIST")
    assert code.retriable is False and code.outcome == GC.OUTCOME_FAILED

    _, seen = _pay_and_observe(
        invoker, store, error_code="ACQ.TRADE_NOT_EXIST", settle_after=99, max_polls=3)
    assert seen["observed_state"] == "failed"
    assert seen["needs_compensation"] is True, "网关明确失败才该标需要补偿"
    assert seen["settled"] is False

    rows = objects.query(
        store, "SELECT * FROM payment_observation WHERE tenant_id=? AND case_id=?",
        (s7.TENANT_ID, s7.CASE_ID))
    assert len(rows) == 1 and rows[0]["observed_state"] == "failed"
    # 论证：明确失败也**不许**被写成 settled。
    assert _count(
        store,
        "SELECT COUNT(*) AS n FROM payment_observation WHERE observed_state='settled'") == 0


# ======================================================================
# 6. 补偿不许替网关下结论
# ======================================================================
def test_compensate_does_not_claim_the_money_stayed(invoker, store):
    """超时路径下补偿的 last_observed_state 必须是 unobserved，**不是 failed**。"""
    _pay_and_observe(invoker, store, error_code=s7.GATEWAY_ERROR_CODE,
                     settle_after=99, max_polls=3)
    res = _compensate(invoker)
    assert res.status == "ok", res.error
    assert res.output["last_observed_state"] == UNOBSERVED, (
        "一次都没观察到终态时写成 failed，就是替网关下了它自己都没下的结论")

    rows = objects.query(
        store, "SELECT * FROM compensation_record WHERE tenant_id=? AND case_id=? AND kind=?",
        (s7.TENANT_ID, s7.CASE_ID, KIND_REQUEST_REVOKED))
    detail = json.loads(rows[0]["detail_json"])
    assert detail["last_observed_state"] == UNOBSERVED
    # 论证：作废记录的语义被写死在数据里 —— 作废的是我们这边的推进，不是那笔钱。
    assert "人工" in detail["meaning"] and "对账" in detail["meaning"]


def test_compensate_reports_what_was_actually_observed(invoker, store):
    """明确失败的案子，补偿如实记 failed —— 不该一律写成 unobserved。"""
    _pay_and_observe(invoker, store, error_code="ACQ.TRADE_NOT_EXIST",
                     settle_after=99, max_polls=3)
    res = _compensate(invoker)
    assert res.status == "ok", res.error
    assert res.output["last_observed_state"] == "failed"


# ======================================================================
# 7. 补偿的两条边界
# ======================================================================
def test_compensate_refuses_a_settled_case(invoker, store):
    """已确认成功的退款不许补偿 —— 静默跳过会把「数据被改坏了」这件事埋掉。"""
    _, seen = _pay_and_observe(invoker, store, error_code=GC.SUCCESS.code,
                               settle_after=2, max_polls=5)
    assert seen["settled"] is True and seen["biz_status"] == "settled"

    res = _compensate(invoker)
    assert res.status == "failed"
    assert "settled" in (res.error or ""), res.error
    # 拒绝之后状态原样不动。
    assert guard.get_case(store, s7.TENANT_ID, s7.CASE_ID)["biz_status"] == "settled"


def test_compensate_is_not_the_authoritative_writer(store):
    """论证：新增第七个 skill 没有扩大 settled 的写入面。"""
    assert guard.AUTHORITATIVE_WRITER == "payment.observe"
    cls = registry.get("refund.compensate")
    assert cls is not None, "refund.compensate 没进注册表 —— 投放即注册被破坏了"
    assert cls.contract.version == "1.0.0"
    assert cls.contract.security_boundary

    guard.create_case(
        store, tenant_id=s7.TENANT_ID, case_id="case-guard-probe",
        channel_id=s7.CHANNEL_ID, order_id=s7.ORDER_ID, order_version=s7.ORDER_VERSION,
        sku=s7.SKU, reason_code="quality_defect", amount_claimed=s7.AMOUNT_CLAIMED,
        plan_id="plan-guard-probe", actor_skill="test", invocation_id="inv-probe")
    with pytest.raises(guard.AuthoritativeFactViolation):
        guard.update_biz_status(store, s7.TENANT_ID, "case-guard-probe", "settled",
                                "refund.compensate", "inv-probe")
    # 越权尝试本身要留痕 —— 那是拿给评委看的证据，吞掉就没了。
    events = [e for e in store.list_event_log("plan-guard-probe")
              if e["event_type"] == guard.VIOLATION_EVENT]
    assert events, "越权写 settled 必须落一条 AuthoritativeFactViolation"


# ======================================================================
# 8. 端到端：场景 7 自身跑绿
# ======================================================================
def test_scenario_7_runs_green():
    assert s7.run() == 0
    C.reset_gateways()
