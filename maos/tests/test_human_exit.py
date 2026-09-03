"""rework 的第三出口：机器返工修不好的，一次干净转人工（D-1）。

## 这个文件存在的理由

改造前 rework 只有两个出口：重规划，或者普通返工到 `max_attempts` 耗尽后 FAILED。
`docs/BACKLOG.md` 的 `## task-X2` 记的就是这件事：

> 四象限里 `retriable=False + failed`（终态失败）与未知码这两种情形，派单写的处置是
> 「转人工或改单」，但**当前实现只做到「不重规划」**：闸判 blocker -> 普通返工 ->
> 重试到 `max_attempts` 耗尽 -> `FAILED("返工次数耗尽")`，中间没有任何一步停在 BLOCKED 等人。

收敛是对的（不自旋、不假绿），但**收敛的姿势不对**：一笔「交易不存在」会被原样重发
两次才失败，而这两次重发从第一次就注定不可能成功 —— `ACQ.TRADE_NOT_EXIST` 不会
因为重发就变成存在。第三出口把这一类在**第一次**就停到人手上。

## 分工（跨轨冻结契约 D-1）

闸负责说「这条 finding 机器返工修不好」（产 `disposition` 与 `scope`），
控制面负责把它路由到人。本文件验的是**路由**这一半；plan 级 finding 的产出侧
由 D-2 自己单测，端到端合起来跑留整合轮。所以下面 plan 级那几条用的是伪造的
finding —— 那不是偷懒，是契约划的界：路由不该依赖产出侧先落地。

## 本文件的第一断言：这个出口必须排在 max_attempts 之前

排在后面的话最后一轮仍然 FAILED，等于白改 —— 而「少重发那两次」正是这一单
要买的东西。`test_third_exit_fires_even_on_the_last_attempt` 钉的就是这条。
"""

from __future__ import annotations

import pytest

from maos.contracts import events as E
from maos.contracts.events import Topic
from maos.contracts.states import PlanState, Risk, TaskState
from maos.core.control_plane import (
    GATEWAY_GATE,
    GW_HUMAN_EXIT,
    GW_HUMAN_TERMINAL,
    GW_NO_REPLAN,
    GW_QUERY_FIRST,
    GW_QUERY_OR_HUMAN,
    GW_REPLAN_CHANNEL,
    HUMAN_EXIT_GATEWAY,
    HUMAN_EXIT_PLAN_DEFECT,
    SCOPE_PLAN,
    ControlPlane,
)
from maos.core.eventbus import InMemoryEventBus
from maos.core.store import SqliteStore
from maos.flows.common import run_until_settled
from maos.runtime.gate import SEVERITY_INFO, HumanApprovalQueue, ReviewerGate
from maos.tools import gateway_codes as GC

TRACE = "trace-d1"

#: 同 test_replan_gateway.py 的口径：刻意用一个**退款域之外**的产物类型。
#: 第三出口的判据落在 finding 的 `disposition`/`scope` 上，不落在业务域上。
KIND_EXTERNAL_RECEIPT = "external_call_receipt"

CODE_RETRIABLE_FAILED = "40005"                          # 可重试 + 确定没执行
CODE_RETRIABLE_UNKNOWN = "ACQ.SYSTEM_ERROR"              # 可重试 + 说不清
CODE_TERMINAL_FAILED = "ACQ.TRADE_NOT_EXIST"             # 终态失败 —— 本文件的主角
CODE_TERMINAL_UNKNOWN = "ACQ.DISCORDANT_REPEAT_REQUEST"  # 不可重试 + 说不清
CODE_NOT_IN_TABLE = "ACQ.NOT_A_REAL_CODE"                # 未知码
CODE_SUCCESS = GC.SUCCESS.code                           # 成功码，闸不出条


# ======================================================================
# 夹具
# ======================================================================
def _build():
    store = SqliteStore()
    store.init_schema()
    bus = InMemoryEventBus()
    cp = ControlPlane(store, bus)
    gate = ReviewerGate(store, bus, cp)
    return store, bus, cp, gate


def _make_task(cp, *, max_attempts=3, effect_risk="L", title="发起退款") -> tuple[str, str]:
    plan_id = cp.create_plan(goal="退款走网关", trace_id=TRACE, tasks=[{
        "role": "payment", "title": title, "inputs": {}, "acceptance": [],
        "effect_risk": effect_risk, "max_attempts": max_attempts,
    }])
    cp.start_plan(plan_id)
    return plan_id, cp.store.list_tasks(plan_id)[0]["task_id"]


def _receipt_content(code: str, *, request_id: str = "gw_req_1") -> dict:
    """一份形状合法的产物，里面挂一份网关回执。别的闸一律放行。"""
    return {
        "summary": f"向网关发起退款，回执 {code}",
        "self_check": {"build": "pass", "lint": "pass"},
        "receipt": {"request_id": request_id, "code": code},
    }


def _finding_for(code: str) -> dict:
    """从**真闸**取一条 finding。不手搓 —— 手搓等于把判据的产出侧绕过去。"""
    fs = ReviewerGate._gate_gateway({}, [{"content": _receipt_content(code)}])
    assert len(fs) == 1, f"{code} 应恰好产出一条 finding，实际 {len(fs)}"
    return fs[0]


def _plan_finding(severity: str = "blocker") -> dict:
    """伪造一条 plan 级 finding —— 产出侧是 D-2 的面，路由侧不等它。"""
    return {"gate": "acceptance", "severity": severity, "scope": SCOPE_PLAN,
            "id": "plan-defect-1", "path": None,
            "message": "验收标准与目标不符，改这一轮的产出解决不了"}


def _task_finding(severity: str = "blocker") -> dict:
    """任务级 finding：**不写 scope**。缺省不写即任务级（契约 §4.4）。"""
    return {"gate": "security", "severity": severity, "path": "f.py",
            "message": "明文凭证"}


def _round(bus, gate, cp, plan_id, task_id, attempt, code):
    """一轮：交回一份带网关回执的产物，让真闸判，判定走真事件总线。"""
    cp.claim(task_id, "w1", attempt)
    bus.publish(Topic.TASK_RESULT, E.task_result(
        plan_id=plan_id, task_id=task_id, attempt=attempt, trace_id=TRACE, status="ok",
        artifacts=[{"kind": KIND_EXTERNAL_RECEIPT,
                    "content": _receipt_content(code, request_id=f"gw_req_{attempt}")}]))
    bus.drain()
    gate.review_pending(plan_id)
    bus.drain()


def _blocked_events(store, plan_id):
    return [e for e in store.list_event_log(plan_id)
            if e["event_type"] == "StateTransition" and e["to_state"] == TaskState.BLOCKED]


def _rework_count(store, plan_id, task_id) -> int:
    """硬判据之一：这一笔被无谓返工了几次。改造前是 2，改造后必须是 0。"""
    return sum(1 for e in store.list_event_log(plan_id)
               if e["event_type"] == "StateTransition"
               and e["task_id"] == task_id and e["to_state"] == TaskState.REWORK)


# ======================================================================
# 1. 契约常量 —— D-1 定义、D-2 只读不引，两边都不许单方面改
# ======================================================================
def test_contract_constants_are_verbatim():
    """常量值逐字钉死。D-2 的 finding 与本轨的路由靠这两个字符串对上。"""
    assert HUMAN_EXIT_GATEWAY == "gateway_needs_human"
    assert HUMAN_EXIT_PLAN_DEFECT == "plan_defect"
    assert SCOPE_PLAN == "plan"


def test_human_exit_dispositions_are_exactly_the_two_not_retriable_quadrants():
    """走第三出口的是 `retriable=False` 那两格 —— 不多不少。

    与 `GW_NO_REPLAN` 的分界要看清：那一条答「许不许换渠道重发」（三格），
    这一条答「机器还有没有别的招」（两格）。差的那一格是 `GW_QUERY_FIRST`，
    它还有 `gateway.query` 这一招，见下面设计点 1 那两条。
    """
    assert GW_HUMAN_EXIT == {GW_HUMAN_TERMINAL, GW_QUERY_OR_HUMAN}
    assert GW_HUMAN_EXIT < GW_NO_REPLAN, "第三出口这两格必须也在不许重发的那三格里"
    assert GW_QUERY_FIRST in GW_NO_REPLAN - GW_HUMAN_EXIT
    assert GW_REPLAN_CHANNEL not in GW_NO_REPLAN


# ======================================================================
# 2. 判据本身（纯函数）—— 契约 §4.2 的两条，按顺序定 reason
# ======================================================================
@pytest.mark.parametrize("code", [CODE_TERMINAL_FAILED, CODE_TERMINAL_UNKNOWN])
def test_terminal_gateway_codes_route_to_human(code):
    """`retriable=False` 的两格：闸给的 disposition 直接把它路由到人。"""
    f = _finding_for(code)
    assert f["disposition"] in GW_HUMAN_EXIT, "前提没成立：这条码不在第三出口那两格"

    reason, evidence = ControlPlane._human_exit([f])
    assert reason == HUMAN_EXIT_GATEWAY
    assert evidence == [{"gate": GATEWAY_GATE, "id": code, "code": code,
                         "severity": f["severity"], "disposition": f["disposition"]}], \
        "证据要能让人回答「为什么轮到我」，也要能对回码表"


def test_unknown_code_routes_to_human_too():
    """未知码归到最危险那一档，同样走第三出口 —— 不许兜底成可重试再返工。"""
    f = _finding_for(CODE_NOT_IN_TABLE)
    assert f["disposition"] == GW_QUERY_OR_HUMAN
    assert ControlPlane._human_exit([f])[0] == HUMAN_EXIT_GATEWAY


@pytest.mark.parametrize("code", [CODE_RETRIABLE_FAILED, CODE_RETRIABLE_UNKNOWN])
def test_retriable_quadrants_do_not_take_the_third_exit(code):
    """`retriable=True` 的两格都不走第三出口，各有各的去处。

    · `40005`（确定没执行）-> 换渠道重发，那是 replan 的活。
    · `ACQ.SYSTEM_ERROR`（说不清）-> 先 query，那是机器的活。
    把它们拉进第三出口，等于拿人去做机器还能做的事。
    """
    assert ControlPlane._human_exit([_finding_for(code)]) is None


def test_plan_scope_defect_routes_to_human():
    """契约 §4.2 (b)：`scope == "plan"` 且非 info -> plan_defect。"""
    reason, evidence = ControlPlane._human_exit([_plan_finding()])
    assert reason == HUMAN_EXIT_PLAN_DEFECT
    assert evidence == [{"gate": "acceptance", "id": "plan-defect-1",
                         "severity": "blocker", "scope": SCOPE_PLAN}]


def test_plan_scope_info_does_not_route_to_human():
    """info 是「记下来了，但这不是缺陷」。plan 级的 info 同样不该占人的时间。"""
    assert ControlPlane._human_exit([_plan_finding(SEVERITY_INFO)]) is None


def test_missing_scope_means_task_level_and_never_routes_to_human():
    """契约 §4.4：**缺省不写即任务级**。

    这一条是第三出口不误伤既有六道闸的全部保证 —— 那六道闸产的 finding
    一个 `scope` 都不写，全走普通返工，行为与改造前逐字一致。
    """
    assert ControlPlane._human_exit([_task_finding()]) is None
    assert ControlPlane._human_exit([_task_finding(), _task_finding("minor")]) is None
    assert ControlPlane._human_exit([{"gate": "x", "severity": "blocker",
                                      "scope": "task"}]) is None


def test_gateway_reason_wins_when_both_conditions_hit():
    """两条同时命中报网关那条（契约 §4.2 的顺序）。

    网关那条是**外部事实**（那笔交易不存在，重发多少次都不存在），plan 那条是
    **内部判断**。人拿到工单先要知道的是不可谈判的那一条。
    """
    reason, evidence = ControlPlane._human_exit([_plan_finding(), _finding_for(CODE_TERMINAL_FAILED)])
    assert reason == HUMAN_EXIT_GATEWAY
    assert len(evidence) == 1 and evidence[0]["code"] == CODE_TERMINAL_FAILED


def test_malformed_findings_do_not_blow_up_the_control_plane():
    """形状怪的 finding 一律不许抛 —— on_review_verdict 是事件回调，异常逃出即整个 plan 崩。"""
    assert ControlPlane._human_exit([None, "x", 42, [], {}]) is None


# ======================================================================
# 3. 整条链路：终态失败码 -> 一次干净的转人工
# ======================================================================
def test_terminal_failure_blocks_once_instead_of_reworking_twice():
    """本单真正买的东西，一条测试全验完。

    改造前：闸判 blocker -> 普通返工 -> 重发 -> 再撞同一个码 -> 返工次数耗尽 FAILED。
    改造后：第一次就停在 BLOCKED 等人，**一次无谓返工都没有**。
    """
    store, bus, cp, gate = _build()
    plan_id, task_id = _make_task(cp)

    _round(bus, gate, cp, plan_id, task_id, 1, CODE_TERMINAL_FAILED)

    task = store.get_task(task_id)
    assert task["state"] == TaskState.BLOCKED, \
        "终态失败码应当场停在人手上，不是再重发两次"
    assert _rework_count(store, plan_id, task_id) == 0, \
        "一次无谓返工都不许有 —— 改造前这里是 2 次，每一次都注定撞同一个码"
    assert task["attempt"] == 1, "attempt 不该被烧掉第二次"

    detail = _blocked_events(store, plan_id)[-1]["detail"]
    assert detail["reason"] == HUMAN_EXIT_GATEWAY
    assert detail["await"] == "human_decision"
    assert detail["evidence"][0]["code"] == CODE_TERMINAL_FAILED
    assert detail["gate_results"][GATEWAY_GATE] == "fail", \
        "转人工那一刻的证据链要能追回到第七道闸"


def test_third_exit_detail_has_the_same_shape_as_replan_limit_exceeded():
    """契约 §4.3：detail 与既有那条同形。读 event_log 的人不该学第二套形状。"""
    store, bus, cp, gate = _build()
    plan_id, task_id = _make_task(cp)
    _round(bus, gate, cp, plan_id, task_id, 1, CODE_TERMINAL_FAILED)

    detail = _blocked_events(store, plan_id)[-1]["detail"]
    assert set(detail) == {"gate_results", "await", "reason", "evidence"}


def test_third_exit_fires_even_on_the_last_attempt():
    """设计点 2：第三出口必须排在 `max_attempts` 之前。

    排在后面的话最后一轮仍然 `FAILED("返工次数耗尽")`，等于白改。这里把任务
    `max_attempts` 设成 1 —— 进 rework 分支时 `attempt >= max_attempts` 已经成立，
    谁先判谁说了算，一目了然。
    """
    store, bus, cp, gate = _build()
    plan_id, task_id = _make_task(cp, max_attempts=1)

    _round(bus, gate, cp, plan_id, task_id, 1, CODE_TERMINAL_FAILED)

    task = store.get_task(task_id)
    assert task["state"] == TaskState.BLOCKED, \
        "排在 max_attempts 之后的话这里会是 FAILED（返工次数耗尽）—— 那就白改了"
    assert task["last_error"] != "返工次数耗尽"
    assert _blocked_events(store, plan_id)[-1]["detail"]["reason"] == HUMAN_EXIT_GATEWAY


def test_third_exit_does_not_touch_plan_state():
    """设计点 4：不许 `_fail_plan`，姿势同 `replan_limit_exceeded`。

    plan 的死活由**人的决定**说了算 —— 主管可能改单重来，也可能驳回。
    闸当场把 plan 判死，就把那个决定替人做了。
    """
    store, bus, cp, gate = _build()
    plan_id, task_id = _make_task(cp)
    _round(bus, gate, cp, plan_id, task_id, 1, CODE_TERMINAL_FAILED)

    assert store.get_plan(plan_id)["state"] == PlanState.RUNNING
    assert not [e for e in store.list_event_log(plan_id)
                if e["event_type"] == "PlanTransition"
                and e["to_state"] == PlanState.FAILED]

    # 人驳回之后才收敛到 FAILED —— 这一步是人做的，不是闸做的。
    HumanApprovalQueue(store, cp).decide(task_id, approved=False, operator="沈思锴")
    assert store.get_plan(plan_id)["state"] == PlanState.FAILED


def test_third_exit_never_calls_the_replanner():
    """第三出口先于重规划判：终态失败码一次都不许把任务重新派发出去。

    重规划等价于重发，而这两格恰恰是「重发没有意义」的两格。
    `GW_NO_REPLAN` 本来就否决了它，这条钉的是**顺序**：哪天否决那条被改松，
    这里要当场红，而不是悄悄多出一次重发。
    """
    store, bus, cp, gate = _build()
    plan_id, task_id = _make_task(cp)
    cp.set_replanner(lambda *, goal, findings, open_tasks: pytest.fail(
        "终态失败码触发了重规划 —— 换个渠道那笔交易也还是不存在"))

    _round(bus, gate, cp, plan_id, task_id, 1, CODE_TERMINAL_FAILED)
    assert store.get_task(task_id)["state"] == TaskState.BLOCKED
    assert cp._replan_used(plan_id) == 0


def test_plan_defect_routes_end_to_end_without_reworking():
    """契约 §4.2 (b) 的路由侧走完整条链路 —— D-2 产、D-1 路由，这里只验后半条。

    finding 由测试伪造（产出侧是 D-2 的面），但从 `on_review_verdict` 起
    走的全是真控制面。
    """
    store, bus, cp, _ = _build()
    plan_id, task_id = _make_task(cp)
    cp.claim(task_id, "w1", 1)
    bus.publish(Topic.TASK_RESULT, E.task_result(
        plan_id=plan_id, task_id=task_id, attempt=1, trace_id=TRACE, status="ok",
        artifacts=[{"kind": KIND_EXTERNAL_RECEIPT, "content": {"summary": "s"}}]))
    bus.drain()
    bus.publish(Topic.REVIEW_VERDICT, E.review_verdict(
        plan_id=plan_id, task_id=task_id, attempt=1, trace_id=TRACE,
        verdict="rework", findings=[_plan_finding()],
        gate_results={"acceptance": "fail"}))
    bus.drain()

    assert store.get_task(task_id)["state"] == TaskState.BLOCKED
    assert _rework_count(store, plan_id, task_id) == 0
    assert _blocked_events(store, plan_id)[-1]["detail"]["reason"] == HUMAN_EXIT_PLAN_DEFECT


def test_ordinary_task_level_defects_still_rework_exactly_as_before():
    """反面：任务级缺陷一行行为都没变。第三出口只截它该截的那两类。"""
    store, bus, cp, _ = _build()
    plan_id, task_id = _make_task(cp)
    cp.claim(task_id, "w1", 1)
    bus.publish(Topic.TASK_RESULT, E.task_result(
        plan_id=plan_id, task_id=task_id, attempt=1, trace_id=TRACE, status="ok",
        artifacts=[{"kind": KIND_EXTERNAL_RECEIPT, "content": {"summary": "s"}}]))
    bus.drain()
    bus.publish(Topic.REVIEW_VERDICT, E.review_verdict(
        plan_id=plan_id, task_id=task_id, attempt=1, trace_id=TRACE,
        verdict="rework", findings=[_task_finding()], gate_results={"security": "fail"}))
    bus.drain()

    assert _rework_count(store, plan_id, task_id) == 1
    assert store.get_task(task_id)["state"] == TaskState.DISPATCHED, "该返工的照样返工"


def test_run_until_settled_converges_after_the_third_exit():
    """设计点 5：任务停在 BLOCKED 之后驱动循环正常返回，不抛「驱动循环未收敛」。

    `flows/common.py` 的循环在 plan 不是 DONE/FAILED 且 `reviewed == 0` 时 return。
    BLOCKED 不是 AWAITING_REVIEW，下一轮 `review_pending` 返 0 —— 但这件事必须
    实跑确认，不能推理了事。
    """
    store, bus, cp, gate = _build()
    plan_id, task_id = _make_task(cp)
    _round(bus, gate, cp, plan_id, task_id, 1, CODE_TERMINAL_FAILED)

    run_until_settled(bus, gate, cp, plan_id)      # 抛了就是这一条没过
    assert store.get_task(task_id)["state"] == TaskState.BLOCKED
    assert store.get_plan(plan_id)["state"] == PlanState.RUNNING


# ======================================================================
# 4. 设计点 3：BLOCKED 之后谁捞得到
# ======================================================================
def test_human_queue_picks_up_a_low_risk_third_exit_task():
    """选 (a) 的直接理由：第三出口与 `effect_risk` 无关，只按 H 捞就会静默挂起。

    这个任务 `effect_risk=L`。放宽之前它会停在 BLOCKED 且**没有任何人捞得到**
    —— 比改造前那个明确的 FAILED 更糟。
    """
    store, bus, cp, gate = _build()
    plan_id, task_id = _make_task(cp, effect_risk="L")
    _round(bus, gate, cp, plan_id, task_id, 1, CODE_TERMINAL_FAILED)

    task = store.get_task(task_id)
    assert task["effect_risk"] == Risk.LOW, "前提没成立：这条要验的就是非高风险那一路"
    assert task["state"] == TaskState.BLOCKED

    pending = HumanApprovalQueue(store, cp).pending(plan_id)
    assert [t["task_id"] for t in pending] == [task_id], \
        "第三出口停下的任务必须有人捞得到，否则就是静默挂起"


def test_human_queue_still_picks_up_high_risk_approvals():
    """既有语义一行没动：`effect_risk=H` 的 BLOCKED 照旧捞得到。

    走的是 `verdict == "pass"` + 高风险审批那条路，`detail` 里是
    `await: human_approval`（不是 human_decision）—— 放宽后的两类条件里
    它命中的仍是原来那一类。
    """
    store, bus, cp, gate = _build()
    plan_id, task_id = _make_task(cp, effect_risk="H")
    _round(bus, gate, cp, plan_id, task_id, 1, CODE_SUCCESS)

    task = store.get_task(task_id)
    assert task["state"] == TaskState.BLOCKED
    assert _blocked_events(store, plan_id)[-1]["detail"]["await"] == "human_approval"
    assert [t["task_id"] for t in HumanApprovalQueue(store, cp).pending(plan_id)] == [task_id]


def test_human_queue_does_not_pick_up_tasks_that_moved_on():
    """捞的是**当前**停在 BLOCKED 的任务。人处置完就不该再出现在队列里。"""
    store, bus, cp, gate = _build()
    plan_id, task_id = _make_task(cp)
    _round(bus, gate, cp, plan_id, task_id, 1, CODE_TERMINAL_FAILED)
    hq = HumanApprovalQueue(store, cp)
    assert hq.pending(plan_id), "前提没成立：这一步本该捞得到"

    hq.decide(task_id, approved=False, operator="沈思锴", note="交易不存在，改单重来")
    assert hq.pending(plan_id) == []
    assert store.get_task(task_id)["state"] == TaskState.FAILED


# ======================================================================
# 5. 设计点 1：GW_QUERY_FIRST 不入这个出口
# ======================================================================
def test_query_first_alone_never_even_reaches_the_rework_branch():
    """设计点 1 的实跑依据（一）：它是 info，闸判 pass，压根走不到 rework 分支。

    所以「要不要把它拉进第三出口」在单独出现时是个**空问题** —— 改与不改，
    这条链路一个字节都不会不同。
    """
    store, bus, cp, gate = _build()
    plan_id, task_id = _make_task(cp)

    f = _finding_for(CODE_RETRIABLE_UNKNOWN)
    assert f["disposition"] == GW_QUERY_FIRST and f["severity"] == SEVERITY_INFO

    _round(bus, gate, cp, plan_id, task_id, 1, CODE_RETRIABLE_UNKNOWN)
    assert store.get_task(task_id)["state"] == TaskState.DONE, \
        "闸放行且非高风险，应正常收敛 —— 既不返工，也不转人工"
    assert _blocked_events(store, plan_id) == []


def test_query_first_alongside_a_fixable_defect_still_reworks():
    """设计点 1 的实跑依据（二）：与任务级缺陷同轮时，正确动作仍是返工。

    这才是把它拉进第三出口真正会付的代价 —— 一条「先去问一下网关」的观察，
    会把一个**机器修得好**的缺陷（这里是明文凭证）一并升级成人工工单。
    query 是机器动作，不该占人的时间。
    """
    store, bus, cp, _ = _build()
    plan_id, task_id = _make_task(cp)
    findings = [_finding_for(CODE_RETRIABLE_UNKNOWN), _task_finding()]

    assert ControlPlane._human_exit(findings) is None

    cp.claim(task_id, "w1", 1)
    bus.publish(Topic.TASK_RESULT, E.task_result(
        plan_id=plan_id, task_id=task_id, attempt=1, trace_id=TRACE, status="ok",
        artifacts=[{"kind": KIND_EXTERNAL_RECEIPT, "content": {"summary": "s"}}]))
    bus.drain()
    bus.publish(Topic.REVIEW_VERDICT, E.review_verdict(
        plan_id=plan_id, task_id=task_id, attempt=1, trace_id=TRACE,
        verdict="rework", findings=findings,
        gate_results={"security": "fail", GATEWAY_GATE: "noted"}))
    bus.drain()

    assert _rework_count(store, plan_id, task_id) == 1
    assert store.get_task(task_id)["state"] == TaskState.DISPATCHED


def test_a_gateway_finding_can_still_be_a_plan_level_defect():
    """判据 (b) 不因为「这条 finding 来自网关闸」就跳过它。

    第七道闸产的 finding 也可能被 D-2 判成 plan 级（比如「这个渠道根本不该出现在
    方案里」）。两条判据是并列的：`gate` 是哪一道不参与判据 (b)，只有 `scope` 与
    `severity` 参与。这里挑的 disposition 刻意**不在**第三出口那两格，
    确保命中的只可能是 (b)。
    """
    hybrid = dict(_finding_for(CODE_RETRIABLE_FAILED))   # disposition=replan_channel
    hybrid["scope"] = SCOPE_PLAN
    assert hybrid["disposition"] not in GW_HUMAN_EXIT, "前提没成立：这条要的就是不在两格里"

    reason, evidence = ControlPlane._human_exit([hybrid])
    assert reason == HUMAN_EXIT_PLAN_DEFECT
    assert evidence[0]["scope"] == SCOPE_PLAN
