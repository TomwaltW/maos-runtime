"""场景 9 两条路径的测试，以及本域 skill 的边界。

本文件的第一断言是整条轨存在的理由：

    失败路径收口之后 `investigation_case.biz_status == 'compensated'`，
    且该案子的 `resolution_observation` 里 `observed_state == 'returned'` 的行数为 0，
    **而四个 Agent 在人做决定之前一个都没失败**。

翻成人话：「所有 Agent 都回复完成」这件事发生了，但业务确实没成功，
而系统如实记录了这一点 —— 没有伪造一条 returned 观察来让链路看起来跑完了。

第二类断言守的是**措辞**：清算方说「撤销成功」（`cancellation_confirmed`）与
清算方说「不给撤」（`rejected`）必须落成不同的记录。把前者写成后者是本域最贵的
一种 bug —— 那笔钱可能已经在退回途中，而对账的人会从错误的方向开始查。

标了 `# 论证：` 的断言是复赛材料里那几句话的机器化版本，评审时可按前缀捞出来对。
"""

from __future__ import annotations

import pytest

from maos.agents.base import AgentIdentity
from maos.agents.investigation import INVESTIGATION_ROLES
from maos.contracts.states import TASK_TRANSITIONS, PlanState, TaskState
from maos.core.store import SqliteStore
from maos.domain.investigation import guard, objects
from maos.flows import scenario_9 as s9
from maos.model.client import Tier
from maos.skills import registry
from maos.skills.builtin.investigation import INVESTIGATION_SKILLS
from maos.skills.builtin.investigation import _common as C
from maos.skills.builtin.investigation.cancel import idempotency_key_of
from maos.skills.builtin.investigation.compensate import (
    EVENT_COMPENSATION_EXECUTED,
    KIND_CANCELLATION_WITHDRAWN,
    KIND_MANUAL_TICKET,
)
from maos.skills.invoker import SkillInvoker
from maos.tools.investigation import (
    SCRIPT_CONFIRMED_ONLY,
    SCRIPT_REJECTED,
    SCRIPT_RETURNED,
    SCRIPT_SILENT,
    MockClearingHouse,
)

TEST_CLEARING = "test-clr-s9"

#: 单测用 identity：授权本域全部 skill。要验的是 skill 自己的行为，
#: 白名单校验由 `test_agents_and_skills_are_registered` 与 invoker 自己的测试守。
FULL_IDENTITY = AgentIdentity(
    agent_id="test-investigation-desk",
    role="investigation_test",
    duty="单测用：授权本域全部 skill",
    allowed_skills=frozenset(INVESTIGATION_SKILLS),
    allowed_tools=frozenset({"clearing.cancel", "clearing.resolution"}),
    write_scope=frozenset(),
    max_risk="H",
    model_tier=Tier.LIGHT,
)

TENANT = "tnt-flow"
MSG = "MSG-FLOW-1"


# --------------------------------------------------------------- 装配
@pytest.fixture
def rig():
    """一套现成的 store + 清算方 + invoker。每个用例一套，账本不互相串。"""
    store = SqliteStore()
    store.init_schema()
    objects.ensure_schema(store)
    objects.put_payment_snapshot(
        store, tenant_id=TENANT, original_msg_id=MSG, version=1,
        end_to_end_id="E2E-FLOW-1", interbank_amount=12500.00, currency="EUR",
        value_date="2026-08-20", debtor_agent="DEUTDEFFXXX",
        creditor_agent="BNPAFRPPXXX")
    C.reset_clearing()
    yield store
    C.reset_clearing()


def _register(script):
    C.register_clearing(TEST_CLEARING, MockClearingHouse(
        resolve_after=2, script={MSG: script}))


def _run(store, skill, payload, *, plan_id="p-flow"):
    invoker = SkillInvoker(FULL_IDENTITY, store)
    res = invoker.invoke(skill, payload, extras={"plan_id": plan_id})
    if res.status != "ok":
        raise AssertionError(f"{skill} 失败：{res.error}")
    return res.output


def _walk_to_sent(store, case_id, *, script, approve=True):
    """把一个案子推到「camt.056 已发出」。"""
    _register(script)
    _run(store, "investigation.file", {
        "tenant_id": TENANT, "case_id": case_id, "original_msg_id": MSG})
    _run(store, "investigation.classify", {
        "tenant_id": TENANT, "case_id": case_id, "classification": "duplicate_payment"})
    if approve:
        C.record_adjustment_approval(store, tenant_id=TENANT, case_id=case_id,
                                     approver="@boss-bank:maos.local",
                                     decision="approved", reason="已核对")
    return _run(store, "investigation.cancel", {
        "tenant_id": TENANT, "case_id": case_id, "clearing": TEST_CLEARING})


# ================================================================ 注册与契约
def test_agents_and_skills_are_registered():
    """# 论证：投放即注册 —— 两个 __init__.py 一个字都没改。"""
    import maos.agents            # noqa: F401 —— import 即触发注册
    import maos.skills.builtin    # noqa: F401
    from maos.agents.base import AGENT_POOL

    for role in INVESTIGATION_ROLES:
        assert role in AGENT_POOL, f"角色 {role} 没进 AGENT_POOL"
    for name in INVESTIGATION_SKILLS:
        assert registry.get(name) is not None, f"skill {name} 没进注册表"


def test_observe_is_the_only_authoritative_writer():
    """# 论证：本域的权威写入方只有一个，且各 skill 的安全边界写明了这件事。"""
    assert guard.AUTHORITATIVE_WRITER == "investigation.observe"
    for name in INVESTIGATION_SKILLS:
        cls = registry.get(name)
        boundary = cls.contract.security_boundary
        assert boundary, f"{name} 没有写安全边界"
        if name != guard.AUTHORITATIVE_WRITER:
            assert "returned" in boundary, (
                f"{name} 的安全边界没有交代它写不出 returned")


# ================================================================ HITL 硬闸
def test_cancel_refuses_without_human_approval(rig):
    """# 论证：人工调账必须人批 —— 没有审批记录就发不出 camt.056（监管硬闸）。"""
    store = rig
    with pytest.raises(AssertionError) as exc:
        _walk_to_sent(store, "case-noapprove", script=SCRIPT_RETURNED, approve=False)
    assert "没有 approved 的人工调账审批记录" in str(exc.value)
    # 一份报文都没发出去。
    assert objects.query(store, "SELECT COUNT(*) AS n FROM cancellation_request",
                         ())[0]["n"] == 0
    assert guard.get_case(store, TENANT, "case-noapprove")["biz_status"] == "classified"


def test_rejected_approval_does_not_count(rig):
    """驳回的审批不是审批 —— 只认 decision='approved' 的那一条。"""
    store = rig
    _register(SCRIPT_RETURNED)
    _run(store, "investigation.file", {
        "tenant_id": TENANT, "case_id": "case-rej", "original_msg_id": MSG})
    _run(store, "investigation.classify", {
        "tenant_id": TENANT, "case_id": "case-rej"})
    C.record_adjustment_approval(store, tenant_id=TENANT, case_id="case-rej",
                                 approver="@boss-bank:maos.local",
                                 decision="rejected", reason="金额存疑")
    with pytest.raises(AssertionError):
        _run(store, "investigation.cancel", {
            "tenant_id": TENANT, "case_id": "case-rej", "clearing": TEST_CLEARING})


def test_cancel_refuses_without_classification(rig):
    """没定性就没有原因码，camt.056 发不出去。"""
    store = rig
    _register(SCRIPT_RETURNED)
    _run(store, "investigation.file", {
        "tenant_id": TENANT, "case_id": "case-nocls", "original_msg_id": MSG})
    C.record_adjustment_approval(store, tenant_id=TENANT, case_id="case-nocls",
                                 approver="@a", decision="approved")
    with pytest.raises(AssertionError) as exc:
        _run(store, "investigation.cancel", {
            "tenant_id": TENANT, "case_id": "case-nocls", "clearing": TEST_CLEARING})
    assert "还没定性" in str(exc.value)


# ================================================================ observe 分档
def test_observe_writes_returned_only_from_pacs004(rig):
    """# 论证：顺利路径经过一次 CNCL，而 returned 只由 pacs.004 促成。"""
    store = rig
    _walk_to_sent(store, "case-ok", script=SCRIPT_RETURNED)
    out = _run(store, "investigation.observe", {
        "tenant_id": TENANT, "case_id": "case-ok", "clearing": TEST_CLEARING,
        "max_polls": 5})

    assert out["observed_state"] == guard.OBS_RETURNED
    assert out["funds_returned"] is True
    assert out["biz_status"] == "returned"
    assert out["poll_count"] == 3

    obs = guard.observations_of(store, TENANT, "case-ok")
    assert [o["observed_state"] for o in obs] == [
        guard.OBS_PENDING, guard.OBS_CANCELLATION_CONFIRMED, guard.OBS_RETURNED]
    # 中间那一步真的被记下来了 —— 没有它，「看见肯定答复却没写」就没有证据。
    assert obs[1]["confirmation_code"] == "CNCL"
    assert obs[1]["returned_amount"] is None, "camt.029 观察不许带退回金额"
    assert obs[2]["message_type"].startswith("pacs.004")
    assert obs[2]["returned_amount"] == 12500.00


def test_observe_never_writes_returned_on_confirmation_only(rig):
    """# 论证：清算方一直说「撤销成功」，系统一个字都不写。**本轨核心。**"""
    store = rig
    _walk_to_sent(store, "case-stuck", script=SCRIPT_CONFIRMED_ONLY)
    out = _run(store, "investigation.observe", {
        "tenant_id": TENANT, "case_id": "case-stuck", "clearing": TEST_CLEARING,
        "max_polls": 5})

    assert out["observed_state"] == guard.OBS_CANCELLATION_CONFIRMED
    # 请求有结论了，但钱没回来 —— 两个正交的布尔。
    assert out["request_resolved"] is True
    assert out["funds_returned"] is False
    assert out["needs_compensation"] is True
    # 状态一步都没往前推。
    assert out["biz_status"] == "cancellation_sent"
    assert guard.get_case(store, TENANT, "case-stuck")["biz_status"] == "cancellation_sent"
    # returned 观察 0 条。
    assert guard.observations_of(store, TENANT, "case-stuck",
                                 observed_state=guard.OBS_RETURNED) == []
    # 但真实观察一条不少，且**不是**伪造的失败。
    obs = guard.observations_of(store, TENANT, "case-stuck")
    assert len(obs) == 5
    assert {o["observed_state"] for o in obs} == {
        guard.OBS_PENDING, guard.OBS_CANCELLATION_CONFIRMED}
    assert guard.OBS_REJECTED not in {o["observed_state"] for o in obs}, (
        "「我问累了」不许写成「清算方说不行」")


def test_observe_records_rejection_but_does_not_close_the_case(rig):
    """明确被拒也不推进状态 —— 收口是补偿的事，不是观察的事。"""
    store = rig
    _walk_to_sent(store, "case-rjcr", script=SCRIPT_REJECTED)
    out = _run(store, "investigation.observe", {
        "tenant_id": TENANT, "case_id": "case-rjcr", "clearing": TEST_CLEARING,
        "max_polls": 5})
    assert out["observed_state"] == guard.OBS_REJECTED
    assert out["needs_compensation"] is True
    assert out["biz_status"] == "cancellation_sent", "观察不许替补偿宣布收口"
    obs = guard.observations_of(store, TENANT, "case-rjcr")
    assert obs[-1]["rejection_code"], "否定决议必须带拒绝原因码"


def test_observe_on_silence_is_pending_not_failure(rig):
    """一直问不出来 → pending，不是 rejected。"""
    store = rig
    _walk_to_sent(store, "case-silent", script=SCRIPT_SILENT)
    out = _run(store, "investigation.observe", {
        "tenant_id": TENANT, "case_id": "case-silent", "clearing": TEST_CLEARING,
        "max_polls": 4})
    assert out["observed_state"] == guard.OBS_PENDING
    assert out["request_resolved"] is False
    assert guard.observations_of(store, TENANT, "case-silent",
                                 observed_state=guard.OBS_RETURNED) == []


def test_observe_requires_a_request_to_observe(rig):
    """没发过 camt.056 就没有可观察的对象。"""
    store = rig
    _register(SCRIPT_RETURNED)
    _run(store, "investigation.file", {
        "tenant_id": TENANT, "case_id": "case-noreq", "original_msg_id": MSG})
    with pytest.raises(AssertionError) as exc:
        _run(store, "investigation.observe", {
            "tenant_id": TENANT, "case_id": "case-noreq", "clearing": TEST_CLEARING})
    assert "没有 cancellation_request" in str(exc.value)


# ================================================================ 补偿
def test_compensate_does_not_declare_the_money_gone(rig):
    """# 论证：补偿不宣布那笔钱没退回来。

    `last_observed_state` 必须是 `cancellation_confirmed` 而不是 `rejected` ——
    清算方明明确认撤销了，写成被拒会让对账的人从错误的方向开始查。
    """
    store = rig
    _walk_to_sent(store, "case-comp", script=SCRIPT_CONFIRMED_ONLY)
    _run(store, "investigation.observe", {
        "tenant_id": TENANT, "case_id": "case-comp", "clearing": TEST_CLEARING,
        "max_polls": 5})
    out = _run(store, "investigation.compensate", {
        "tenant_id": TENANT, "case_id": "case-comp", "operator": "@boss",
        "reason": "资金下落未明，转人工对账"})

    assert out["biz_status"] == "compensated"
    assert out["last_observed_state"] == guard.OBS_CANCELLATION_CONFIRMED
    assert out["withdrawn"][0]["last_observed_state"] == guard.OBS_CANCELLATION_CONFIRMED
    assert "不再推进" in out["withdrawn"][0]["meaning"]
    # 工单的第一条待办要指向「查资金是不是在退回途中」，不是「重发」。
    assert "退回途中" in out["ticket"]["todo"][0]
    assert "不要重发" in " ".join(out["ticket"]["todo"])
    # 两种记录都在。
    kinds = {r["kind"] for r in objects.query(
        store, "SELECT kind FROM investigation_compensation WHERE case_id=?",
        ("case-comp",))}
    assert kinds == {KIND_CANCELLATION_WITHDRAWN, KIND_MANUAL_TICKET}


def test_compensate_todo_differs_by_last_observation(rig):
    """不同的最后观察给不同的对账起点 —— 不是装饰。"""
    store = rig
    _walk_to_sent(store, "case-c1", script=SCRIPT_REJECTED)
    _run(store, "investigation.observe", {
        "tenant_id": TENANT, "case_id": "case-c1", "clearing": TEST_CLEARING,
        "max_polls": 5})
    out = _run(store, "investigation.compensate", {
        "tenant_id": TENANT, "case_id": "case-c1", "operator": "@boss",
        "reason": "清算方拒绝"})
    assert out["last_observed_state"] == guard.OBS_REJECTED
    assert "明确拒绝" in out["ticket"]["todo"][0]


def test_compensate_refuses_a_returned_case(rig):
    """钱已经确认退回来了，再走补偿是数据被改坏的信号，不许静默跳过。"""
    store = rig
    _walk_to_sent(store, "case-done", script=SCRIPT_RETURNED)
    _run(store, "investigation.observe", {
        "tenant_id": TENANT, "case_id": "case-done", "clearing": TEST_CLEARING,
        "max_polls": 5})
    with pytest.raises(AssertionError) as exc:
        _run(store, "investigation.compensate", {
            "tenant_id": TENANT, "case_id": "case-done", "operator": "@boss",
            "reason": "手滑"})
    assert "不许补偿" in str(exc.value)


def test_compensation_leaves_an_event(rig):
    """补偿执行必须落 CompensationExecuted，否则这件事只活在日志里。"""
    store = rig
    _walk_to_sent(store, "case-ev", script=SCRIPT_CONFIRMED_ONLY)
    _run(store, "investigation.observe", {
        "tenant_id": TENANT, "case_id": "case-ev", "clearing": TEST_CLEARING,
        "max_polls": 3})
    _run(store, "investigation.compensate", {
        "tenant_id": TENANT, "case_id": "case-ev", "operator": "@boss",
        "reason": "转人工"})
    evs = [e for e in store.list_event_log("p-flow")
           if e["event_type"] == EVENT_COMPENSATION_EXECUTED]
    assert len(evs) == 1
    # domain 键区分本条是哪个域的补偿 —— 三种补偿共用事件名。
    assert evs[0]["detail"]["domain"] == C.BIZ_TYPE


# ================================================================ 受理与幂等
def test_intake_amount_comes_from_the_snapshot(rig):
    """金额以快照为准；递进来一个对不上的值当场抛。"""
    store = rig
    _register(SCRIPT_RETURNED)
    out = _run(store, "investigation.file", {
        "tenant_id": TENANT, "case_id": "case-amt", "original_msg_id": MSG,
        "claimed_amount": 12500.00})
    assert out["case"]["amount"] == 12500.00
    with pytest.raises(AssertionError) as exc:
        _run(store, "investigation.file", {
            "tenant_id": TENANT, "case_id": "case-amt2", "original_msg_id": MSG,
            "claimed_amount": 9999.00})
    assert "对不上" in str(exc.value)


def test_intake_requires_a_snapshot(rig):
    """案子必须挂在一份读到过的原始支付上。"""
    store = rig
    _register(SCRIPT_RETURNED)
    with pytest.raises(AssertionError) as exc:
        _run(store, "investigation.file", {
            "tenant_id": TENANT, "case_id": "case-nosnap",
            "original_msg_id": "MSG-NOT-READ"})
    assert "没有原始支付快照" in str(exc.value)


def test_intake_replay_is_idempotent(rig):
    """受理重跑不新建、不倒退，并把「这是重放」告诉上层。"""
    store = rig
    _walk_to_sent(store, "case-replay", script=SCRIPT_RETURNED)
    out = _run(store, "investigation.file", {
        "tenant_id": TENANT, "case_id": "case-replay", "original_msg_id": MSG})
    assert out["idempotent_replay"] is True
    assert out["biz_status"] == "cancellation_sent", "重放不许把案子倒回 filed"


def test_one_case_sends_exactly_one_camt056(rig):
    """# 论证：幂等键由 (tenant, case) 定，重跑不会发出第二份 camt.056。"""
    store = rig
    first = _walk_to_sent(store, "case-idem", script=SCRIPT_RETURNED)
    again = _run(store, "investigation.cancel", {
        "tenant_id": TENANT, "case_id": "case-idem", "clearing": TEST_CLEARING})
    assert first["request_id"] == again["request_id"]
    assert first["idempotency_key"] == idempotency_key_of(TENANT, "case-idem")
    n = objects.query(store, "SELECT COUNT(*) AS n FROM cancellation_request"
                             " WHERE tenant_id=? AND case_id=?",
                      (TENANT, "case-idem"))[0]["n"]
    assert n == 1


def test_classification_carries_checkable_rule_refs(rig):
    """# 论证：裁定判据带可核对的规则编号 —— 官方码 + 定义原文 + 出处 URL。"""
    store = rig
    _register(SCRIPT_RETURNED)
    _run(store, "investigation.file", {
        "tenant_id": TENANT, "case_id": "case-rule", "original_msg_id": MSG})
    out = _run(store, "investigation.classify", {
        "tenant_id": TENANT, "case_id": "case-rule",
        "classification": "duplicate_payment"})
    ref = out["rule_refs"][0]
    assert ref["code"] == "DUPL"
    assert ref["code_set"] == "ExternalCancellationReason1Code"
    assert "duplicate" in ref["definition"].lower()
    assert ref["source"].startswith("https://www.iso20022.org/")


def test_classify_rejects_made_up_reason_code(rig):
    """编造的原因码进不去 —— 那会让发出的 camt.056 不合规。"""
    store = rig
    _register(SCRIPT_RETURNED)
    _run(store, "investigation.file", {
        "tenant_id": TENANT, "case_id": "case-badcode", "original_msg_id": MSG})
    with pytest.raises(AssertionError):
        _run(store, "investigation.classify", {
            "tenant_id": TENANT, "case_id": "case-badcode", "reason_code": "ZZZZ"})


# ================================================================ 场景 9 端到端
@pytest.fixture(scope="module")
def scenario():
    """跑一次完整场景，两条路径共用同一套运行时（口径同场景本身）。"""
    C.reset_clearing()
    ok = s9.drive_success()
    bad = s9.drive_failure(store=ok["store"], bus=ok["bus"], cp=ok["cp"],
                           gate=ok["gate"], hq=ok["hq"])
    yield ok, bad
    C.reset_clearing()


def test_success_path_closes_on_pacs004(scenario):
    """# 论证：顺利路径经过 CNCL 却直到 pacs.004 才收口。"""
    ok, _ = scenario
    store, cp = ok["store"], ok["cp"]
    case = guard.get_case(store, s9.TENANT_ID, s9.CASE_ID)
    assert case["biz_status"] == "returned"

    rows = guard.observations_of(store, s9.TENANT_ID, s9.CASE_ID,
                                 observed_state=guard.OBS_RETURNED)
    assert len(rows) == 1
    assert rows[0]["message_type"].startswith("pacs.004")
    assert rows[0]["return_reason_code"]
    assert rows[0]["returned_amount"] == s9.AMOUNT

    trail = [o["observed_state"] for o in
             guard.observations_of(store, s9.TENANT_ID, s9.CASE_ID)]
    assert trail == [guard.OBS_PENDING, guard.OBS_CANCELLATION_CONFIRMED,
                     guard.OBS_RETURNED]
    assert cp.store.get_plan(ok["plan_id"])["state"] == PlanState.DONE


def test_failure_path_writes_nothing(scenario):
    """# 论证：失败路径 returned 观察 0 条，业务状态 compensated，全程未进 returned。

    **本文件第一断言。**
    """
    ok, bad = scenario
    store, cp = ok["store"], ok["cp"]
    case = guard.get_case(store, s9.TENANT_ID, s9.CASE_ID_2)
    assert case["biz_status"] == "compensated"
    assert guard.observations_of(store, s9.TENANT_ID, s9.CASE_ID_2,
                                 observed_state=guard.OBS_RETURNED) == []
    assert cp.store.get_plan(bad["plan_id"])["state"] == PlanState.FAILED


def test_failure_path_all_agents_reported_ok(scenario):
    """# 论证：「Agent 都说完成了」不等于业务成功。

    在人做决定之前那一刻，四个 Agent 一个都没失败 —— 而钱一分没回来。
    """
    _, bad = scenario
    states = bad["agent_states"]
    assert TaskState.FAILED not in states.values()
    assert states[s9.TASK_OBSERVE_2] == TaskState.BLOCKED
    assert [s for t, s in states.items() if t != s9.TASK_OBSERVE_2] == \
        [TaskState.DONE] * 3
    assert bad["resolution"]["funds_returned"] is False
    assert bad["resolution"]["request_resolved"] is True


def test_failure_path_observations_are_real_not_fabricated(scenario):
    """问了几次就有几条观察，且没有一条是伪造的失败。"""
    _, bad = scenario
    obs = bad["observations"]
    assert len(obs) == s9.MAX_POLLS_STUCK
    assert {o["observed_state"] for o in obs} == {
        guard.OBS_PENDING, guard.OBS_CANCELLATION_CONFIRMED}
    assert all(o["returned_amount"] is None for o in obs)


def test_both_paths_share_the_same_cncl(scenario):
    """# 论证：两条路径共用同一句肯定答复，逐字相同。

    差别不在 Agent 说了什么，在于外部权威到底给没给出资金证据。
    """
    ok, _ = scenario
    store = ok["store"]
    a = [o for o in guard.observations_of(store, s9.TENANT_ID, s9.CASE_ID)
         if o["observed_state"] == guard.OBS_CANCELLATION_CONFIRMED]
    b = [o for o in guard.observations_of(store, s9.TENANT_ID, s9.CASE_ID_2)
         if o["observed_state"] == guard.OBS_CANCELLATION_CONFIRMED]
    assert a and b
    assert a[0]["confirmation_code"] == b[0]["confirmation_code"] == "CNCL"
    assert a[0]["message_type"] == b[0]["message_type"]


def test_business_status_never_becomes_a_task_state(scenario):
    """# 论证（铁律 9）：业务状态不进 Task 状态机，也没有新开迁移。"""
    ok, bad = scenario
    cp = ok["cp"]
    known = {v for k, v in vars(TaskState).items()
             if not k.startswith("_") and isinstance(v, str)}
    for pid in (ok["plan_id"], bad["plan_id"]):
        states = {t["state"] for t in cp.store.list_tasks(pid)}
        assert states <= known
        assert not ({"returned", "compensated", "cancellation_sent", "classified"}
                    & states)
        moves = {(e["from_state"], e["to_state"])
                 for e in cp.store.list_event_log(pid)
                 if e["event_type"] == "StateTransition"}
        assert moves <= set(TASK_TRANSITIONS), (
            f"出现了不在冻结迁移表里的迁移：{sorted(moves - set(TASK_TRANSITIONS))}")


def test_scenario_run_is_green():
    """整段 run() 自带断言，跑通即绿。**独立跑一次**，不复用 module fixture ——

    fixture 那份只跑了两个 drive，run() 里还有十几条收口断言没被执行到。
    """
    C.reset_clearing()
    try:
        assert s9.run() == 0
    finally:
        C.reset_clearing()
