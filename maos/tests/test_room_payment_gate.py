"""房间里那一次 `/approve` 不替人签「跑起来之后才出现的闸」（T135）。

被测的是**一次签字的边界**。房间里的人按 `/approve` 的那一刻，付款还没发起、
网关还没回执 —— 他签的是「这一单可以去退」。而网关回了终态失败之后控制面落下的
那道闸问的是另一件事：「钱没退出去，这一步还算不算完成」。两件事发生在不同的
时刻，答案也可能相反。

改造前 `handle_execute` 传的是恒 `approve=True`，于是那一次签字把两个都签了：

    AWAITING_REVIEW -> BLOCKED [gate_needs_human] {"await": "human_decision"}
    BLOCKED         -> DONE    [human_approve]    {"operator": "沈思锴（supervisor）"}

`operator` 甚至不是房间里按键的那个人 —— 它是 CLI 代跑用的写死名字。Plan 因此
收在 **DONE**，而同一张回帖卡的下半截说「这一单钱没退出去，已开人工补偿工单」。
「所有 Agent 都回复完成不代表业务成功」，这一跳就是那句话的标本。

## 三条刻意钉住的边界

1. **只扣「跑起来之后才出现的」那一类。** 判据是这一次进 BLOCKED 那一跳的
   ``detail["await"]``，不是角色、不是 ``effect_risk``——后两者规划期就定了，
   而这一类闸规划期还不存在。判错方向的代价是整条 happy 路径都停下来等人。
2. **扣住不等于判死。** 付款那一步停在 BLOCKED，Plan 停在 RUNNING，`biz_status`
   一个字节不动 —— 钱退没退仍然只认 `payment_observation`（铁律 8）。
3. **第二次决定要真的能把 Plan 推下去。** 房间这边没有长驻运行时，所以
   `handle_gate_decision` 会把运行时重新装配一遍跑在**同一个库**上。推不动的话，
   那一单就从「静默收在 DONE」换成了「静默停在 BLOCKED」，一样没人知道。
"""

from __future__ import annotations

import pytest

from maos.contracts.states import PlanState, TaskState
from maos.core.control_plane import AWAIT_HUMAN_DECISION
from maos.domain.refund import objects, projection
from maos.flows import custom_case
from maos.ingress import router as R
from maos.ingress.router import IngressRouter
from maos.skills.builtin.refund import compensate as CP
from maos.tests.test_room_outcome_commands import (
    APPROVERS, BOSS, CASE, FAIL_CODE, ORDER, OUTSIDER, TENANT, TICKET,
    FakeAdapter, _ledger_file, _msg, _payload, _room_store, _router,
)

#: 付款那一步的 task_id —— 由 case_id 推出（`flows/contrast.py::plan_tasks`）。
PAYMENT_TASK = f"task-{CASE.lower()}-payment"
FINANCE_TASK = f"task-{CASE.lower()}-finance"


def _stopped(tmp_path):
    """跑到付款闸停住的一单，**不做**第二次决定。返回 (router, store, 回帖)。"""
    store = _room_store()
    r, _ad = _router(store, _ledger_file(tmp_path, {ORDER: FAIL_CODE}))
    r.handle(_msg(f"/refund {ORDER} 质量问题", msg_id="g1"))
    return r, store, r.handle(_msg(f"/approve {CASE}", msg_id="g2"))


@pytest.fixture(scope="module")
def stopped(tmp_path_factory):
    return _stopped(tmp_path_factory.mktemp("gate"))


# ==========================================================================
# 1. 那一次签字签不到这道闸
# ==========================================================================
def test_the_payment_gate_is_not_signed_by_the_room_approval(stopped):
    """🔴 付款那一步停在 BLOCKED，`event_log` 里**没有**代签那一跳。

    查 event_log 而不是回帖措辞：措辞可以改，而「这道闸有没有被人签过」是审计
    问题 —— 它只有一个答案，就在迁移事件里。
    """
    _r, store, _out = stopped
    plan_id = objects.query(store, "SELECT plan_id FROM refund_case WHERE case_id=?",
                            (CASE,))[0]["plan_id"]

    task = store.get_task(PAYMENT_TASK)
    assert task["state"] == TaskState.BLOCKED, (
        f"付款那一步收在 {task['state']} —— 那一次 /approve 又把它签掉了")

    hops = [e for e in store.list_event_log(plan_id)
            if e.get("task_id") == PAYMENT_TASK and e.get("to_state")]
    assert hops[-1]["to_state"] == TaskState.BLOCKED
    assert not any(str(e.get("reason") or "").startswith("human_") for e in hops), (
        "付款闸上有一跳人工决定 —— 房间里没有人做过它")

    # 扣住的判据就是那一跳自己声明的 `await`，不是别的地方猜出来的。
    assert custom_case.await_kind(store, plan_id, PAYMENT_TASK) == AWAIT_HUMAN_DECISION


def test_the_plan_does_not_settle_while_someone_still_has_to_decide(stopped):
    """🔴 Plan 停在 RUNNING —— 不收 DONE，也不替人判 FAILED。

    两头都要守：收 DONE 是改造前那个 bug；当场判 FAILED 则是另一个方向的代做决定
    （`control_plane._escalate_to_human` 的 docstring：「plan 的死活由人的决定说了
    算，闸当场把 plan 判死就是替人做了那个决定」）。
    """
    _r, store, out = stopped
    plan_id = objects.query(store, "SELECT plan_id FROM refund_case WHERE case_id=?",
                            (CASE,))[0]["plan_id"]

    assert store.get_plan(plan_id)["state"] == PlanState.RUNNING
    assert "收在 RUNNING" in out


def test_the_finance_gate_is_still_signed_the_old_way(stopped):
    """🔴 `effect_risk=H` 那一道照旧由处置流程代跑 —— T135 一个字没动它。

    这一道规划期就知道会来，「这一单可以去退」包得住它。把它一起扣住，房间里
    每一单都要多按一次键，而那一按毫无新信息。
    """
    _r, store, _out = stopped

    assert store.get_task(FINANCE_TASK)["state"] == TaskState.DONE
    assert store.get_task(FINANCE_TASK)["effect_risk"] == "H"


def test_business_status_is_untouched_by_holding_the_gate(stopped):
    """🔴 扣住闸**不改**任何业务事实：钱退没退仍只认观察行（铁律 8）。

    扣的是「这一步算不算完成」，不是「钱有没有退出去」。这两件事混一起的症状是
    一次人工驳回把外部支付状态写成了失败 —— 而那是外部系统的权威。
    """
    _r, store, _out = stopped

    case = objects.query(store, "SELECT * FROM refund_case WHERE case_id=?", (CASE,))[0]
    assert case["biz_status"] == "gateway_accepted", (
        "扣住一道闸把业务状态也改了 —— 那就不是「停下来问人」了")

    obs = objects.query(store, "SELECT * FROM payment_observation WHERE case_id=?"
                        " ORDER BY observed_at", (CASE,))
    assert [o["observed_state"] for o in obs] == ["failed"]
    assert obs[-1]["gateway_code"] == FAIL_CODE


def test_no_compensation_ticket_before_the_decision(stopped):
    """🔴 工单**还没开**：补偿是「看过事实之后的决定」，那个决定还没做出来。"""
    _r, store, out = stopped

    assert CP.ticket_of(store, TENANT, CASE) is None
    assert "已开人工补偿工单" not in out


# ==========================================================================
# 2. 这道闸有人捞得到
# ==========================================================================
def test_pending_lists_the_gate_and_survives_a_fresh_router(stopped, tmp_path):
    """🔴 `/pending` 看得见它，**换一个 router 实例照样看得见**。

    `handle_execute` 一跑完就把待办摘了，重启之后内存里更是什么都没有。只认内存
    的话那一单永远等不到第二次决定 —— 与改造前静默收在 DONE 一样难查，只是换了
    个地方静默。所以判据必须落在库上。
    """
    _r, store, _out = stopped

    fresh = IngressRouter({FakeAdapter().name: FakeAdapter()}, store=store,
                          ledger_path=_ledger_file(tmp_path, {ORDER: FAIL_CODE}),
                          approvers=lambda: frozenset(APPROVERS))
    out = fresh.handle(_msg("/pending", msg_id="g9"))

    assert "等你再决定一次的闸" in out and CASE in out
    assert "发起退款并观察网关终态" in out


def test_the_card_says_what_has_to_be_decided_and_how(stopped):
    """🔴 卡上要说清**要决定什么**、怎么决定，且把网关那句话说成观察到的事实。

    只说「停在人工闸上」而不给命令，等于把那一单留在原地；而 `gateway_code`
    取自观察行的最后一条，一个字都不是这里判出来的。
    """
    _r, _store, out = stopped

    assert "停在人工闸上" in out and FAIL_CODE in out and "钱没退出去" in out
    assert "再决定一次" in out
    assert f"/reject  {CASE}" in out and f"/approve {CASE}" in out
    # 这一条是 T135 的核心那句话：两次签字不是同一层。
    assert "签不到这里" in out


# ==========================================================================
# 3. 第二次决定
# ==========================================================================
@pytest.fixture(scope="module")
def rejected(tmp_path_factory):
    """跑到闸上、然后**驳回**的一单。"""
    r, store, card = _stopped(tmp_path_factory.mktemp("reject"))
    out = r.handle(_msg(f"/reject {CASE} 钱没退出去，不签这一步", msg_id="g3"))
    return {"router": r, "store": store, "card": card, "said": out}


def test_rejecting_the_gate_records_the_person_who_pressed_the_key(rejected):
    """🔴 这一跳的操作者是**房间里按键的那个人**，不是 CLI 代跑那个写死的名字。

    这是 HITL trace 上唯一一条「人在现场做的决定」。署名署错了，这条链就只剩形式
    —— 事后问「谁决定不签这一步」，库里给的是一个当时根本不在场的名字。
    """
    store = rejected["store"]
    plan_id = objects.query(store, "SELECT plan_id FROM refund_case WHERE case_id=?",
                            (CASE,))[0]["plan_id"]

    hop = [e for e in store.list_event_log(plan_id)
           if e.get("task_id") == PAYMENT_TASK and e.get("reason") == "human_reject"]
    assert len(hop) == 1, "驳回那一跳不在 event_log 里，或落了两次"
    assert hop[0]["detail"]["operator"] == BOSS, (
        f"操作者写成了 {hop[0]['detail'].get('operator')!r} —— 那个人不在现场")
    assert store.get_task(PAYMENT_TASK)["state"] == TaskState.FAILED


def test_rejecting_settles_the_plan_as_failed(rejected):
    """🔴 Plan 如实收在 FAILED —— 与证据束 `gateway_fail` 那条路径同一个终态。"""
    store = rejected["store"]
    plan_id = objects.query(store, "SELECT plan_id FROM refund_case WHERE case_id=?",
                            (CASE,))[0]["plan_id"]

    assert store.get_plan(plan_id)["state"] == PlanState.FAILED
    assert "收在 FAILED" in rejected["said"]


def test_the_ticket_opens_after_the_decision_not_before(rejected):
    """🔴 工单在第二次决定**之后**才开，且下一步说得清清楚楚。"""
    store, out = rejected["store"], rejected["said"]

    assert CP.ticket_of(store, TENANT, CASE) is not None
    assert "已开人工补偿工单" in out and TICKET in out
    assert f"/assign {TICKET} payment_ops" in out


def test_the_public_line_still_comes_only_from_the_projection(rejected):
    """🔴 对外那一行仍然**只经** `projection` 取值，router 一个新字面值都不拼。

    判据不是「在五个字面值里」（那个松到等于没判，见 T126 的教训），而是**现算
    一遍**：从库里的事实重新算出 `public_status`，再走 `public_status_line()`
    渲染，逐字比对卡上那一行。两边对不上就说明 router 里长出了第二处措辞。
    """
    store, out = rejected["store"], rejected["said"]

    case = objects.query(store, "SELECT * FROM refund_case WHERE case_id=?", (CASE,))[0]
    obs = objects.query(store, "SELECT * FROM payment_observation WHERE case_id=?"
                        " ORDER BY observed_at", (CASE,))
    has_req = bool(objects.query(store, "SELECT 1 FROM refund_request WHERE case_id=?",
                                 (CASE,)))
    expected = R.public_status_line(
        projection.public_status(case["biz_status"], has_req,
                                 projection.observed_state_of(obs)),
        case_id=CASE)

    assert expected and expected in out, f"卡上那一行不是算出来的：\n{out}"
    assert expected.endswith(projection.PUBLIC_COMPENSATED)


def test_a_second_decision_on_the_same_gate_replies_in_plain_words(rejected):
    """🔴 重复决定收到的是人话，不是一个栈。

    房间里两个人同时看见那条提示、同时按键是常事。第二个人该被告知「已经有人
    决定过了」，而**不许**因此把第一次那个已经生效的决定说成失败。
    """
    out = rejected["router"].handle(_msg(f"/reject {CASE} 再按一次", msg_id="g4"))

    assert "Error" not in out and "Traceback" not in out
    # 闸已经不在 BLOCKED 上了，于是这一句走的是「没有待办」那条既有出路。
    assert CASE in out


def test_the_gate_lane_is_behind_the_approver_list(stopped, tmp_path):
    """🔴 名单外的人碰不了第二档，且这次越权要留痕 —— 与另几条命令同一道闸。"""
    store = stopped[1]
    fresh = IngressRouter({FakeAdapter().name: FakeAdapter()}, store=store,
                          ledger_path=_ledger_file(tmp_path, {ORDER: FAIL_CODE}),
                          approvers=lambda: frozenset(APPROVERS))

    out = fresh.handle(_msg(f"/reject {CASE} 我说了算", sender=OUTSIDER, msg_id="g5"))

    assert "无审批权限" in out and OUTSIDER in out
    assert store.get_task(PAYMENT_TASK)["state"] == TaskState.BLOCKED, (
        "名单外的人把这道闸签掉了")


# ==========================================================================
# 4. 少见的那一支：仍然放行
# ==========================================================================
def test_approving_the_gate_pushes_the_plan_through(tmp_path):
    """🔴 第二次决定选「仍然放行」时，Plan 要真的跑完 —— 下游任务接着跑。

    房间这边没有长驻运行时，`handle_execute` 那次调用返回之后跑它的那套 cp/bus/gate
    就没了。推不动的话，那一单就从「静默收在 DONE」换成了「静默停在 BLOCKED」，
    一样没人知道 —— 所以这一条钉的是**重新装配那套运行时真的有效**。
    """
    r, store, _card = _stopped(tmp_path)

    out = r.handle(_msg(f"/approve {CASE} 钱已通过别的渠道到账", msg_id="g6"))
    plan_id = objects.query(store, "SELECT plan_id FROM refund_case WHERE case_id=?",
                            (CASE,))[0]["plan_id"]

    assert store.get_task(PAYMENT_TASK)["state"] == TaskState.DONE
    assert store.get_plan(plan_id)["state"] == PlanState.DONE
    assert "已放行" in out and "收在 DONE" in out
    # 下游那一步真的跑了 —— 不是把付款任务翻成 DONE 就完事。
    assert store.get_task(f"task-{CASE.lower()}-notify")["state"] == TaskState.DONE

    # 放行是人的决定，署名同样是按键的那个人。
    hop = [e for e in store.list_event_log(plan_id)
           if e.get("task_id") == PAYMENT_TASK and e.get("reason") == "human_approve"]
    assert hop and hop[0]["detail"]["operator"] == BOSS

    # **但钱仍然没退出去**：放行改的是「这一步算不算完成」，观察行一个字节没动。
    obs = objects.query(store, "SELECT * FROM payment_observation WHERE case_id=?"
                        " ORDER BY observed_at", (CASE,))
    assert [o["observed_state"] for o in obs] == ["failed"], (
        "一次人工放行把外部支付状态写成了到账 —— 铁律 8 说那不归 MAOS 判")


# ==========================================================================
# 5. 别的调用者一个字节都没变
# ==========================================================================
def test_run_payload_still_signs_everything_by_default():
    """🔴 `hold_awaits` 缺省空元组：CLI 那几条路径逐字节同 T135 之前。

    `scripts/run_case.py` 与证据束都走这条缺省。缺省一变，四条路径的终态全跟着变，
    而那几束是要进评审材料的。
    """
    store = _room_store()
    row = custom_case.run_payload(_payload(ORDER, {ORDER: FAIL_CODE}),
                                  approve=True, verbose=False, store=store)

    assert row["plan_state"] == PlanState.DONE
    assert [e["decision"] for e in row["human_exits"]] == ["approved", "approved"]


def test_a_runner_that_does_not_know_the_flag_never_receives_it(tmp_path):
    """🔴 注入的处置器不认 `hold_awaits` 时**一个字都不多收**。

    不认的关键字会当场 TypeError，落进调用方的 except —— 症状是房间里一片安静，
    而日志里只有一行「处置失败」。口径同 `_accepted_extra`：探不到就不传。
    """
    seen: list[dict] = []

    def narrow(payload, *, approve=True, verbose=True, store=None):
        seen.append({"approve": approve})
        return {"case_id": CASE, "decision": "approve", "why": "x",
                "amount_approved": "1.00", "policy_version_used": 1,
                "rule_refs": "AS-002@v1", "biz_status": "submitted",
                "settled_observations": 0, "payment_observations": [],
                "human_exits": [], "plan_id": "plan-n", "plan_state": "DONE",
                "public_status": "", "tenant_id": TENANT, "tasks": []}

    store = _room_store()
    r, _ad = _router(store, _ledger_file(tmp_path), adapter=None)
    r._runner = narrow
    r.handle(_msg(f"/refund {ORDER} 质量问题", msg_id="n1"))
    out = r.handle(_msg(f"/approve {CASE}", msg_id="n2"))

    assert seen == [{"approve": True}], "窄签名的处置器收到了它不认的关键字"
    assert "已放行" in out


def test_a_runner_with_kwargs_does_receive_it(tmp_path):
    """🔴 反过来：`**kw` 的转发壳收得下，就必须真的传给它。

    `_default_runner(payload, **kw)` 正是这种壳，而 `_keyword_params` 对它返回的是
    ``{"payload"}``（非空，于是不算「问不出来」）—— 照那个函数判，房间路径永远
    拿不到这个模式，T135 整条改造静默失效。
    """
    seen: list[dict] = []

    def wide(payload, **kw):
        seen.append(dict(kw))
        return {"case_id": CASE, "decision": "approve", "why": "x",
                "amount_approved": "1.00", "policy_version_used": 1,
                "rule_refs": "AS-002@v1", "biz_status": "submitted",
                "settled_observations": 0, "payment_observations": [],
                "human_exits": [], "plan_id": "plan-w", "plan_state": "DONE",
                "public_status": "", "tenant_id": TENANT, "tasks": []}

    store = _room_store()
    r, _ad = _router(store, _ledger_file(tmp_path), adapter=None)
    r._runner = wide
    r.handle(_msg(f"/refund {ORDER} 质量问题", msg_id="w1"))
    r.handle(_msg(f"/approve {CASE}", msg_id="w2"))

    assert seen and seen[0].get("hold_awaits") == R.HOLD_AWAITS
    assert R.HOLD_AWAITS == (AWAIT_HUMAN_DECISION,), "这个模式的字面值只许有一处出处"
