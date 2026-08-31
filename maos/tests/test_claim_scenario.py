"""场景 8 端到端 —— 顺利路径到账、失败路径「一个字都不写」。

`run()` 里已经带了逐条自证，本文件不重抄那些断言（抄一遍就是维护两份判据，
两份迟早漂）。这里补的是 `run()` 覆盖不到、或需要按库里的行下断言的那几条：

  · **四个 Agent 全回 ok 而业务没成** —— 本域失败路径的第二样卖点；
  · **失败路径全库 `paid` 观察 0 条**，且两段的观察行形状不同（denied 1 条 / 0 条）；
  · **第七道闸没有对理赔回执产生任何 finding** —— 两张码表不混查；
  · **换域零改动**：本场景没有为理赔域新增 Task 状态或迁移。

本文件**不改** `maos/main.py::DEFAULT_SCENARIOS`（同期三轨都在新增 flow，谁改谁冲突），
所以场景 8 的唯一调用方就是这里。
"""

from __future__ import annotations

import pytest

from maos.contracts.states import PlanState, TaskState
from maos.domain.claim import guard, objects
from maos.flows import scenario_8 as S
from maos.runtime.gate import GATEWAY_GATE
from maos.skills.builtin.claim import CLAIM_SKILLS
from maos.skills.builtin.claim.compensate import (
    KIND_MANUAL_TICKET,
    KIND_PAYMENT_REVOKED,
)
from maos.skills.builtin.claim.observe import UNOBSERVED
from maos.skills.registry import get as get_skill


@pytest.fixture(scope="module")
def happy():
    """顺利路径跑一次，模块内共用。

    module scope 是有意的：这条链路要跑四个任务两次审批，每条用例各跑一遍会把
    这个文件的耗时翻好几倍，而它们看的是同一次运行的产物。
    """
    out = S.drive_happy()
    S._assert_happy(out)
    return out


@pytest.fixture(scope="module")
def failure():
    out = S.drive_failure()
    S._assert_failure(out)
    return out


# ------------------------------------------------------------------ 顺利路径
def test_happy_path_reaches_paid_only_through_observation(happy):
    """论证：paid 是**问**出来的 —— 恰好一条到账观察，且指得回哪次调用。"""
    store = happy["store"]
    case = guard.get_case(store, S.TENANT_ID, S.CLAIM_ID)
    assert case["biz_status"] == "paid"

    obs = objects.query(
        store, "SELECT * FROM claim_payment_observation WHERE tenant_id=? AND claim_id=?",
        (S.TENANT_ID, S.CLAIM_ID))
    assert len(obs) == 1 and obs[0]["observed_state"] == "paid"
    assert obs[0]["actor_invocation_id"]
    assert happy["receipt"]["poll_count"] == S.SETTLE_AFTER, (
        "一次 query 就到账的话，claim.observe 就没有存在理由了")


def test_happy_path_amount_follows_the_terms_pinned_at_binding(happy):
    """论证：赔款按投保当时锁定的条款版本算出来，不是当前最新版本。"""
    store = happy["store"]
    adj = objects.query(
        store, "SELECT * FROM adjudication WHERE tenant_id=? AND claim_id=?",
        (S.TENANT_ID, S.CLAIM_ID))[0]
    assert adj["rule_no"] == "CL-01"
    assert int(adj["terms_version"]) == S.TERMS_VERSION_AT_BIND
    assert f"{float(adj['allowed_amount']):.2f}" == S.EXPECTED_ALLOWED
    assert S.EXPECTED_ALLOWED != S.IF_LATEST_TERMS_ALLOWED, (
        "两版条款算出同一个数的话，这个场景就证明不了版本锁定")


def test_happy_path_plan_is_done(happy):
    assert happy["cp"].store.get_plan(happy["plan_id"])["state"] == PlanState.DONE


# ------------------------------------------------------------------ 失败路径
def test_failure_path_writes_not_a_single_paid_observation(failure):
    """论证：失败路径全库 `paid` 观察 **0 条** —— 本域最硬的一条断言。

    有一条就说明有人在没问出到账的情况下把外部状态写死为终态了（铁律 8）。
    """
    assert S.paid_observations(failure["store"]) == 0


def test_failure_path_two_legs_have_different_observation_shapes(failure):
    """论证：两段的「一个字都不写」不是同一件事，分别校。

      · 拒付段：赔付方**说了话**（CARC 96），所以留下恰好一条 denied 观察；
      · 沉默段：赔付方一句话都没说，所以**一行都不写** —— 把空表读成拒付，
        正是本域通篇在防的那个推断。
    """
    denied = failure["denied"]["obs_rows"]
    assert len(denied) == 1
    assert denied[0]["observed_state"] == "denied"
    assert denied[0]["carc_code"] == S.DENIAL_CARC

    assert failure["unobserved"]["obs_rows"] == []
    assert failure["unobserved"]["compensation"]["last_observed_state"] == UNOBSERVED, (
        "没问出终态时最后观察必须是 unobserved —— 写成 denied 就是替赔付方"
        "下了它自己都没下的结论")


def test_failure_path_all_agents_reported_ok(failure):
    """论证：**四个 Agent 全部回报 ok，而案子确实没成。**

    这是本域失败路径要买的第二样东西。把付款 Agent 改成 failed 会让这句话消失 ——
    那时「业务没成」就只是「有个 Agent 失败了」，不再是「所有人都说完成了，
    但业务确实没成功」。
    """
    for label in ("denied", "unobserved"):
        ok, total = failure[label]["agents_ok"]
        assert total == 4, f"{label}：应有四个任务，实际 {total}"
        assert ok == 4, f"{label}：四个 Agent 应全部回报 ok，实际 {ok}"
        assert failure[label]["case"]["biz_status"] == "compensated"
        assert failure[label]["plan"]["state"] == PlanState.FAILED


def test_failure_path_leaves_a_revocation_and_a_manual_ticket(failure):
    """论证：补偿真发生过 —— 作废记录与人工工单都在，事件也落了。"""
    store = failure["store"]
    for label in ("denied", "unobserved"):
        kinds = {r["kind"] for r in failure[label]["comp_rows"]}
        assert kinds == {KIND_PAYMENT_REVOKED, KIND_MANUAL_TICKET}
        events = [e for e in store.list_event_log(failure[label]["plan_id"])
                  if e["event_type"] == "CompensationExecuted"]
        assert len(events) == 1
        assert events[0]["detail"]["domain"] == "claim", (
            "三种补偿共用一个事件名，靠 detail.domain 分流；漏了这个键，"
            "「这个 Plan 补偿过没有」就要查两处")


def test_denied_leg_carries_the_x12_evidence_end_to_end(failure):
    """论证：拒付回执把 X12 的三样东西一路带到了工单上 —— 码、组码、出处。"""
    receipt = failure["denied"]["receipt"]
    assert receipt["carc_code"] == S.DENIAL_CARC
    assert receipt["group_code"] == S.DENIAL_GROUP
    assert list(receipt["remark_codes"]) == list(S.DENIAL_RARC), (
        "CARC 96 的官方描述要求至少一条 Remark Code，回执缺了就不合 X12 规范")
    assert receipt["source"].startswith("https://x12.org/")
    assert receipt["fetched_at"]
    assert "Non-covered charge(s)" in receipt["description"]

    ticket = failure["denied"]["compensation"]["ticket"]
    assert ticket["last_carc"] == S.DENIAL_CARC
    assert any("重报无意义" in line for line in ticket["todo"]), (
        "工单要按码表的 recourse 告诉人下一步，而 96 那一格是终态拒赔")


# ------------------------------------------------- 第七道闸不认理赔回执
def test_gateway_gate_produces_nothing_for_claim_artifacts(failure):
    """论证：第七道闸（支付宝码表）对理赔回执**一条 finding 都没产**。

    闸按 `content["receipt"]` 的数据形状触发，然后拿那个 code 去查
    `maos/tools/gateway_codes.py`。CARC `96` 送进去只会得到一条「未知错误码」的
    blocker —— 理赔任务会因为一条完全正确的拒付回执被判不合格。
    本域把回执挂在 `payer_receipt` 上就是为了让两张码表不混查。

    判据取 event_log 里真实落下的 gate 结果，不是重新调一次闸 —— 重新调等于
    自己再造一套输入，证不了「实际跑的那一次没触发」。
    """
    store = failure["store"]
    for label in ("denied", "unobserved"):
        plan_id = failure[label]["plan_id"]
        hits = []
        for e in store.list_event_log(plan_id):
            detail = e.get("detail") or {}
            for finding in _findings_in(detail):
                if finding.get("gate") == GATEWAY_GATE:
                    hits.append((e["task_id"], finding))
        assert not hits, (
            f"{label}：第七道闸对理赔回执产出了 finding {hits} —— "
            "说明理赔回执占用了 receipt 键，被拿去查了支付宝的码表")


def _findings_in(detail: dict) -> list[dict]:
    """把一条事件 detail 里可能藏着的 findings 摊出来。形状随事件类型而异。"""
    out = []
    for key in ("findings", "gate_results"):
        value = detail.get(key)
        if isinstance(value, list):
            out.extend(f for f in value if isinstance(f, dict))
        elif isinstance(value, dict):
            for sub in value.values():
                if isinstance(sub, list):
                    out.extend(f for f in sub if isinstance(f, dict))
    return out


def test_claim_receipt_key_is_not_the_gateway_gate_key():
    """论证：这条边界是**常量级**的，不靠记性。

    有人把 RECEIPT_FIELD 改回 "receipt" 时，这条当场红。
    """
    from maos.agents.claim import RECEIPT_FIELD
    from maos.runtime.gate import GATEWAY_RECEIPT_FIELD

    assert RECEIPT_FIELD != GATEWAY_RECEIPT_FIELD, (
        f"理赔回执的键名不能与第七道闸的触发键相同（都是 {RECEIPT_FIELD!r}）—— "
        "X12 的 CARC 会被拿去查支付宝码表，判出来的任何结论都是错的")


# ------------------------------------------------------------ 换域零改动
def test_no_new_task_states_or_transitions(happy, failure):
    """论证：铁律 9 —— 业务状态是 claim_case 自己的字段，不是 Task 状态。"""
    S._assert_no_new_task_states(happy["cp"], happy["plan_id"])
    for label in ("denied", "unobserved"):
        S._assert_no_new_task_states(failure["cp"], failure[label]["plan_id"])


def test_business_statuses_never_leak_into_task_states(failure):
    """论证：`compensated` 这类词一次都没出现在 Task 状态里。"""
    known = {v for k, v in vars(TaskState).items()
             if not k.startswith("_") and isinstance(v, str)}
    assert set(guard.BIZ_STATUS_FLOW) & known == set(), (
        f"业务状态与 Task 状态的取值域不许相交，实际相交于 "
        f"{sorted(set(guard.BIZ_STATUS_FLOW) & known)}")


def test_all_six_skills_are_registered_by_being_dropped_in():
    """论证：投放即注册 —— `builtin/__init__.py` 一个字都没改。"""
    for name in CLAIM_SKILLS:
        assert get_skill(name) is not None, f"{name} 没进注册表"


def test_four_roles_are_registered_by_being_dropped_in():
    """论证：Agent 同样是投放即注册 —— `agents/__init__.py` 一个字都没改。"""
    from maos.agents.base import AGENT_POOL
    from maos.agents.claim import CLAIM_ROLES

    for role in CLAIM_ROLES:
        assert role in AGENT_POOL, f"{role} 没进 AGENT_POOL"
