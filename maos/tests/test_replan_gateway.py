"""replan 的第三条触发线：网关错误码 -> 换渠道 -> 达上限 -> 转人工（手册 R2）。

改造前 `_should_replan` 只有两条触发线（单轮 blocker >= 2、同一任务第 2 次 rework），
而**没有任何一道闸认识网关回执**，所以手册 R2 与 Demo 分镜 02:30 的这一段
（「网关返可重试错误码 -> replan 换渠道 -> 仍失败 -> 达 replan 上限 -> needs_human」）
连输入都没有。上限机制本身早就在（`_replan_used >= _max_replan()` ->
BLOCKED("replan_limit_exceeded")，场景 5 覆盖），缺的只是把网关错误码接成第三条线。

本文件同时守住这条线的两半：ReviewerGate 的第七道闸产 finding，ControlPlane 消费它。

## 本文件的第一断言：四象限只有一格能重试

BACKLOG 里那条写的是「retriable 为真就 replan」，**这个口径不完整**。
`maos/tools/gateway_codes.py` 自己写明：`retriable` 答的是「能不能再发一次」，
`outcome` 答的是「**这一笔到底执行了没有**」，两者正交，四格各对应一种处置：

    retriable=True  + failed   ->  可以直接重发（网关在入口就拒了，业务没执行）
    retriable=True  + unknown  ->  先 query 再决定，不许直接重发（可能造成第二笔）
    retriable=False + failed   ->  终态，转人工或改单
    retriable=False + unknown  ->  必须 query / 转人工，最危险的一档

**只有第一格允许触发 replan 换渠道**，其余三格一律不许自旋。按 retriable 单条判，
后三格里会有两格被误判成可重试，其中 `outcome=unknown` 那一格重发就可能真退第二笔
（铁律 8：MAOS 不持有权威事实）。逐格断言在 `test_quadrant_*` 四条里。

## 第二断言：不许挡住场景 7

场景 7 注入的 `ACQ.SYSTEM_ERROR` 正落在 retriable=True + unknown 那一格。
第七道闸必须**认出它但不挡闸** —— 网关自己都说不清，判它「本轮产出不合格」就是
替网关下了它没下的结论。那一格的正确出口是 `effect_risk=H` 的人工审批，
不是一次机器返工。`test_scenario_7_*` 两条守这件事，走的是真跑一遍场景 7。
"""

from __future__ import annotations

import pytest

from maos.contracts import events as E
from maos.contracts.events import Topic
from maos.contracts.states import PlanState, TaskState
from maos.core.control_plane import (
    GATEWAY_GATE,
    GW_HUMAN_TERMINAL,
    GW_NO_REPLAN,
    GW_QUERY_FIRST,
    GW_QUERY_OR_HUMAN,
    GW_REPLAN_CHANNEL,
    ControlPlane,
)
from maos.core.eventbus import EventBus, InMemoryEventBus
from maos.core.store import SqliteStore
from maos.flows import scenario_7 as s7
from maos.runtime.gate import SEVERITY_INFO, ReviewerGate
from maos.tools import gateway_codes as GC

TRACE = "trace-x2"

#: 刻意用一个**退款域之外**的产物类型。第七道闸认的是数据形状
#: （`content["receipt"]` 里有 `code`），不是产物 kind —— 换域之后这道闸一行都不用改
#: （铁律 9 推论，与第六道闸同一条规矩）。用退款域的 kind 就验不到这一点了。
KIND_EXTERNAL_RECEIPT = "external_call_receipt"

# 四象限各挑一个官方码。每条测试都先对着码表核一遍它确实在那一格，
# 码表改了这里当场红 —— 不留「测试写死了一个早就变了的假设」这种坑。
CODE_RETRIABLE_FAILED = "40005"                        # 调用频次超限
CODE_RETRIABLE_UNKNOWN = "ACQ.SYSTEM_ERROR"            # 系统错误
CODE_TERMINAL_FAILED = "ACQ.TRADE_NOT_EXIST"           # 交易不存在
CODE_TERMINAL_UNKNOWN = "ACQ.DISCORDANT_REPEAT_REQUEST"  # 请求信息不一致


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


def _make_task(cp, *, max_attempts=9, title="发起退款") -> tuple[str, str]:
    plan_id = cp.create_plan(goal="退款走网关", trace_id=TRACE, tasks=[{
        "role": "payment", "title": title, "inputs": {}, "acceptance": [],
        "effect_risk": "L", "max_attempts": max_attempts,
    }])
    cp.start_plan(plan_id)
    return plan_id, cp.store.list_tasks(plan_id)[0]["task_id"]


def _receipt_content(code: str, *, request_id: str = "gw_req_1") -> dict:
    """一份形状合法的产物，里面挂一份网关回执。

    `summary` 与 `self_check` 是为了让**别的闸**全部放行 —— 本文件要验的是第七道闸，
    别的闸插一条 finding 进来，测的就不是这道闸了。
    """
    return {
        "summary": f"向网关发起退款，回执 {code}",
        "self_check": {"build": "pass", "lint": "pass"},
        "receipt": {"request_id": request_id, "code": code,
                    "message": GC.ALL_CODES[code].message if code in GC.ALL_CODES else ""},
    }


class _RecordingBus(EventBus):
    """只记不发。要看的是闸的判定，不想让 ControlPlane 的订阅跟着跑状态迁移。"""

    def __init__(self) -> None:
        self.published: list[tuple[str, object]] = []

    def publish(self, topic, env) -> None:
        self.published.append((topic, env))

    def subscribe(self, topic, group, handler) -> None:
        pass

    def drain(self, max_rounds: int = 1000) -> int:
        return 0


def _run_gate(contents: list[dict]) -> dict:
    """把若干产物挂到一个 AWAITING_REVIEW 的任务上，跑真闸，返回 verdict payload。"""
    store = SqliteStore()
    store.init_schema()
    bus = _RecordingBus()
    gate = ReviewerGate(store, bus, ControlPlane(store, bus))

    store.insert_plan({"plan_id": "p1", "trace_id": TRACE, "goal": "g",
                       "state": PlanState.RUNNING})
    store.insert_task({"task_id": "t1", "plan_id": "p1", "trace_id": TRACE,
                       "role": "payment", "title": "发起退款",
                       "state": TaskState.AWAITING_REVIEW, "attempt": 1})
    for i, content in enumerate(contents):
        store.insert_artifact({"artifact_id": f"a{i}", "task_id": "t1", "plan_id": "p1",
                               "kind": KIND_EXTERNAL_RECEIPT, "version": 1,
                               "content": content})

    assert gate.review_pending("p1") == 1
    topic, env = bus.published[-1]
    assert topic == Topic.REVIEW_VERDICT, f"闸发到了 {topic}，不是 REVIEW_VERDICT"
    return env.payload


def _gateway_findings(payload: dict) -> list[dict]:
    return [f for f in payload["findings"] if f["gate"] == GATEWAY_GATE]


def _finding_for(code: str) -> dict:
    """单独取一条码的 finding —— 四象限逐格断言用这个，不绕整条链路。"""
    fs = ReviewerGate._gate_gateway({}, [{"content": _receipt_content(code)}])
    assert len(fs) == 1, f"{code} 应恰好产出一条 finding，实际 {len(fs)}"
    return fs[0]


def _blockers(n: int) -> list[dict]:
    return [{"gate": "security", "severity": "blocker", "path": f"f{i}.py",
             "message": "明文凭证"} for i in range(n)]


# ======================================================================
# 1. 四象限 —— 每格一条，只有第一格允许 replan
# ======================================================================
def test_quadrant_retriable_failed_is_the_only_one_that_replans():
    """retriable=True + failed：网关在入口就拒了，业务确定没执行 -> 允许换渠道重发。

    这是四格里**唯一**能触发第三条 replan 触发线的一格。
    """
    entry = GC.ALL_CODES[CODE_RETRIABLE_FAILED]
    assert (entry.retriable, entry.outcome) == (True, GC.OUTCOME_FAILED), \
        "官方码表变了：这条码已经不在 retriable=True + failed 那一格"

    f = _finding_for(CODE_RETRIABLE_FAILED)
    assert f["gate"] == GATEWAY_GATE and f["disposition"] == GW_REPLAN_CHANNEL
    assert f["severity"] == "blocker", "业务确定没执行，这一轮产出确实不合格"

    _, _, cp, _ = _build()
    _, task_id = _make_task(cp)
    assert cp._should_replan(cp.store.get_task(task_id), [f]) is True


def test_quadrant_retriable_unknown_must_query_first_never_replan():
    """retriable=True + unknown：能再发一次，但那一笔的下落网关自己说不清。

    **这一格是 BACKLOG 那条「retriable 为真就 replan」口径最贵的一处错**：
    直接重发就可能造出第二笔退款（铁律 8）。正确动作是先 gateway.query。
    """
    entry = GC.ALL_CODES[CODE_RETRIABLE_UNKNOWN]
    assert (entry.retriable, entry.outcome) == (True, GC.OUTCOME_UNKNOWN)
    assert GC.needs_query_before_retry(CODE_RETRIABLE_UNKNOWN), \
        "码表自己就说这条码重发前必须先查"

    f = _finding_for(CODE_RETRIABLE_UNKNOWN)
    assert f["disposition"] == GW_QUERY_FIRST
    assert f["severity"] == SEVERITY_INFO, \
        "网关自己都说不清，判它「本轮产出不合格」就是替网关下结论（铁律 8）"

    _, _, cp, _ = _build()
    _, task_id = _make_task(cp)
    task = cp.store.get_task(task_id)
    assert cp._should_replan(task, [f]) is False
    # 一票否决：即便本轮另有两个 blocker（原来的第一条触发线），也不许重规划 ——
    # 重规划会把这个任务重新派发出去，那等价于重发。
    assert cp._should_replan(task, [f] + _blockers(2)) is False


def test_quadrant_not_retriable_failed_goes_to_human_never_replan():
    """retriable=False + failed：终态失败，原样重发没有意义，转人工或改单。"""
    entry = GC.ALL_CODES[CODE_TERMINAL_FAILED]
    assert (entry.retriable, entry.outcome) == (False, GC.OUTCOME_FAILED)

    f = _finding_for(CODE_TERMINAL_FAILED)
    assert f["disposition"] == GW_HUMAN_TERMINAL
    assert f["severity"] == "blocker"

    _, _, cp, _ = _build()
    _, task_id = _make_task(cp)
    task = cp.store.get_task(task_id)
    assert cp._should_replan(task, [f]) is False
    assert cp._should_replan(task, [f] + _blockers(2)) is False


def test_quadrant_not_retriable_unknown_is_the_most_dangerous_never_replan():
    """retriable=False + unknown：既不能原样重发、下落也不明 —— 最危险的一档。

    官方 remedy 里有「或查询历史执行结果」：同一个请求号之前那一笔可能已经成功了。
    """
    entry = GC.ALL_CODES[CODE_TERMINAL_UNKNOWN]
    assert (entry.retriable, entry.outcome) == (False, GC.OUTCOME_UNKNOWN)

    f = _finding_for(CODE_TERMINAL_UNKNOWN)
    assert f["disposition"] == GW_QUERY_OR_HUMAN
    assert f["severity"] == SEVERITY_INFO

    _, _, cp, _ = _build()
    _, task_id = _make_task(cp)
    task = cp.store.get_task(task_id)
    assert cp._should_replan(task, [f]) is False
    assert cp._should_replan(task, [f] + _blockers(2)) is False


def test_every_official_code_lands_in_exactly_one_quadrant():
    """全码表逐条走一遍：处置由 (retriable, outcome) 唯一决定，只有一格能 replan。

    挑四个代表码的四条测试挡不住「表里新增了一条码却没人给它定处置」，
    这一条挡得住 —— 判据是码表本身，不是某几个写死的码值。
    """
    _, _, cp, _ = _build()
    _, task_id = _make_task(cp)
    task = cp.store.get_task(task_id)

    expected = {
        (True, GC.OUTCOME_FAILED): GW_REPLAN_CHANNEL,
        (True, GC.OUTCOME_UNKNOWN): GW_QUERY_FIRST,
        (False, GC.OUTCOME_FAILED): GW_HUMAN_TERMINAL,
        (False, GC.OUTCOME_UNKNOWN): GW_QUERY_OR_HUMAN,
    }
    replanned: list[str] = []
    for code, entry in GC.ALL_CODES.items():
        fs = ReviewerGate._gate_gateway({}, [{"content": _receipt_content(code)}])
        if entry.outcome == GC.OUTCOME_SUCCESS:
            assert fs == [], f"{code} 是成功码，第七道闸不该有话说"
            continue
        assert len(fs) == 1, f"{code} 应恰好一条 finding"
        want = expected[(entry.retriable, entry.outcome)]
        assert fs[0]["disposition"] == want, \
            f"{code}（retriable={entry.retriable} outcome={entry.outcome}）处置判成了 " \
            f"{fs[0]['disposition']}，应为 {want}"
        if cp._should_replan(task, fs):
            replanned.append(code)

    assert set(replanned) == {c for c, e in GC.ALL_CODES.items()
                              if e.retriable and e.outcome == GC.OUTCOME_FAILED}, \
        f"允许 replan 的码集合不等于 retriable=True + failed 那一格，实际 {replanned}"
    assert GW_REPLAN_CHANNEL not in GW_NO_REPLAN


def test_unknown_code_is_not_bottomed_out_as_retriable():
    """未知码不许兜底成「可重试」—— 兜底就是把没核过出处的码当已知码处理。

    `gateway_codes.lookup` 对未知码是抛而不是给默认值，第七道闸照办：判 blocker，
    并归到最危险的那一档（既判不了可重试，也判不了终态失败）。
    """
    f = _finding_for("ACQ.NEVER_HEARD_OF_THIS")
    assert f["disposition"] == GW_QUERY_OR_HUMAN
    assert f["severity"] == "blocker"
    assert f["retriable"] is None and f["outcome"] is None, \
        "未知码不许填一个猜出来的 retriable / outcome"

    _, _, cp, _ = _build()
    _, task_id = _make_task(cp)
    assert cp._should_replan(cp.store.get_task(task_id), [f]) is False


# ======================================================================
# 2. 一票否决优先于既有两条触发线
# ======================================================================
def test_gateway_veto_beats_the_second_rework_line():
    """第 2 次返工本来会触发重规划；网关说「不许重发」时它也得让路。

    否决必须先于既有两条线判，否则一个下落不明的付款任务只要返工过一次，
    就会被第二条线重新派发出去 —— 那正是「重发可能造成第二笔」的那一步。
    """
    store, bus, cp, _ = _build()
    plan_id, task_id = _make_task(cp)
    minor = [{"gate": "evidence", "severity": "minor", "message": "缺说明"}]

    # 先落一条 REWORK，凑出「这将是第 2 次返工」
    cp.claim(task_id, "w1", 1)
    bus.publish(Topic.TASK_RESULT, E.task_result(
        plan_id=plan_id, task_id=task_id, attempt=1, trace_id=TRACE, status="ok",
        artifacts=[{"kind": KIND_EXTERNAL_RECEIPT,
                    "content": _receipt_content(CODE_RETRIABLE_UNKNOWN)}]))
    bus.drain()
    bus.publish(Topic.REVIEW_VERDICT, E.review_verdict(
        plan_id=plan_id, task_id=task_id, attempt=1, trace_id=TRACE,
        verdict="rework", findings=minor, gate_results={"evidence": "fail"}))
    bus.drain()

    task = store.get_task(task_id)
    assert cp._should_replan(task, minor) is True, "前提没成立：第 2 次返工本该触发"

    gw = _finding_for(CODE_RETRIABLE_UNKNOWN)
    assert cp._should_replan(task, minor + [gw]) is False, \
        "网关说「不许重发」时，第 2 次返工那条线也不许把它推去重规划"


def test_veto_wins_when_two_receipts_disagree():
    """一轮里同时出现「可换渠道」与「不许重发」两种回执：保守取否决。

    宁可少重试一次，也不许对一笔下落不明的请求重发。
    """
    _, _, cp, _ = _build()
    _, task_id = _make_task(cp)
    findings = [_finding_for(CODE_RETRIABLE_FAILED), _finding_for(CODE_RETRIABLE_UNKNOWN)]
    assert cp._should_replan(cp.store.get_task(task_id), findings) is False


# ======================================================================
# 3. 闸本身：认回执、分严重度、不重复出条
# ======================================================================
def test_gate_blocks_on_a_terminal_failure_receipt():
    payload = _run_gate([_receipt_content(CODE_RETRIABLE_FAILED)])
    fs = _gateway_findings(payload)
    assert len(fs) == 1 and fs[0]["disposition"] == GW_REPLAN_CHANNEL
    assert payload["verdict"] == "rework"
    assert payload["gate_results"][GATEWAY_GATE] == "fail"
    # finding 要能直接消费：码、判据、官方处置、出处一个都不能少
    assert fs[0]["code"] == CODE_RETRIABLE_FAILED
    assert fs[0]["retriable"] is True and fs[0]["outcome"] == GC.OUTCOME_FAILED
    assert fs[0]["remedy"] == GC.ALL_CODES[CODE_RETRIABLE_FAILED].remedy
    assert fs[0]["source"], "回执 finding 必须带错误码出处，评委问「哪来的」要当场能答"


def test_gate_notes_but_does_not_block_on_an_unknown_outcome():
    """outcome=unknown -> 记一条 info，verdict 仍是 pass。场景 7 靠的就是这一条。"""
    payload = _run_gate([_receipt_content(CODE_RETRIABLE_UNKNOWN)])
    fs = _gateway_findings(payload)
    assert len(fs) == 1 and fs[0]["severity"] == SEVERITY_INFO
    assert payload["verdict"] == "pass", \
        "网关说不清不是本轮产出的缺陷，把它判成 rework 就是替网关下结论"
    assert payload["gate_results"][GATEWAY_GATE] == "noted", \
        "「这道闸有话说但没拦」不能被压成 pass 藏起来"


def test_gate_is_silent_on_a_success_receipt():
    payload = _run_gate([_receipt_content(GC.SUCCESS.code)])
    assert _gateway_findings(payload) == []
    assert payload["verdict"] == "pass"
    assert payload["gate_results"][GATEWAY_GATE] == "pass"


def test_gate_does_not_repeat_the_same_receipt_twice():
    """付款任务会产出两份产物（受理回执 + 观察回执），同一笔同一个码只出一条。

    findings 会原样喂回返工提示词，重复条目只是噪声。
    """
    payload = _run_gate([_receipt_content(CODE_RETRIABLE_FAILED),
                         _receipt_content(CODE_RETRIABLE_FAILED)])
    assert len(_gateway_findings(payload)) == 1

    two = _run_gate([_receipt_content(CODE_RETRIABLE_FAILED, request_id="gw_a"),
                     _receipt_content(CODE_TERMINAL_FAILED, request_id="gw_b")])
    assert len(_gateway_findings(two)) == 2, "两笔不同的请求要各出各的"


def test_gate_ignores_artifacts_without_a_gateway_receipt():
    """没有回执的产物一律不碰 —— 这道闸只在有网关回执时才说话。"""
    payload = _run_gate([{"summary": "一份普通产物",
                          "self_check": {"build": "pass", "lint": "pass"}}])
    assert _gateway_findings(payload) == []
    assert payload["verdict"] == "pass"


def test_gate_reads_the_shape_not_the_domain():
    """判据落在 `content["receipt"]["code"]` 这个数据形状上，不落在退款域上。

    第七道闸与第六道闸同一条规矩（铁律 9 推论）：换域之后只要产物里还挂着一份
    网关回执，这道闸一行都不用改。本文件全程用的 kind 就不是退款域的。
    """
    import ast
    import pathlib

    src = pathlib.Path("maos/runtime/gate.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("maos.domain"), \
                "第七道闸把退款域 import 进 Gate 了"
        elif isinstance(node, ast.Import):
            assert not any(a.name.startswith("maos.domain") for a in node.names)


# ======================================================================
# 4. 整条链路：网关失败 -> replan 换渠道 -> 再失败 -> 达上限 -> 转人工
# ======================================================================
def _gateway_round(bus, gate, cp, plan_id, task_id, attempt, code):
    """一轮：交回一份带网关回执的产物，让**真闸**去判，判定结果走真事件总线。

    不手搓 findings —— 手搓等于把第七道闸绕过去，那样验的就只剩下半条链路。
    """
    cp.claim(task_id, "w1", attempt)
    bus.publish(Topic.TASK_RESULT, E.task_result(
        plan_id=plan_id, task_id=task_id, attempt=attempt, trace_id=TRACE, status="ok",
        artifacts=[{"kind": KIND_EXTERNAL_RECEIPT,
                    "content": _receipt_content(code, request_id=f"gw_req_{attempt}")}]))
    bus.drain()
    gate.review_pending(plan_id)
    bus.drain()


def test_gateway_failure_replans_then_escalates_to_human_at_the_limit(monkeypatch):
    """手册 R2 那条链路，一条测试跑完整条：

        网关返 40005（可重试且业务确定没执行）
          -> 第七道闸判 replan_channel
          -> replan 换渠道（第 1 次）
          -> 换了渠道仍返 40005
          -> replan 已达上限 -> BLOCKED("replan_limit_exceeded")，**不自旋**
    """
    monkeypatch.setenv("MAOS_MAX_REPLAN", "1")
    store, bus, cp, gate = _build()
    plan_id, task_id = _make_task(cp, title="走主渠道退款")

    channels: list[str] = []

    def replanner(*, goal, findings, open_tasks):
        gw = [f for f in findings if f.get("gate") == GATEWAY_GATE]
        assert gw, "重规划拿到的 findings 里必须有网关回执，否则它不知道为什么要换渠道"
        channels.append(f"backup-{len(channels) + 1}")
        return [{"role": "payment", "title": f"改走备用渠道 {channels[-1]}",
                 "inputs": {"channel": channels[-1]}, "acceptance": []}]

    cp.set_replanner(replanner)

    _gateway_round(bus, gate, cp, plan_id, task_id, 1, CODE_RETRIABLE_FAILED)
    assert cp._replan_used(plan_id) == 1, "第一次网关失败应触发换渠道重规划"
    task = store.get_task(task_id)
    assert task["title"] == "改走备用渠道 backup-1", "重规划应把任务改成走备用渠道"
    assert task["state"] == TaskState.DISPATCHED

    _gateway_round(bus, gate, cp, plan_id, task_id, 2, CODE_RETRIABLE_FAILED)

    assert cp._replan_used(plan_id) == 1, "上限已到，不许再重规划 —— 自旋就是从这里开始的"
    assert len(channels) == 1, "超限那一轮不该再调重规划回调"
    task = store.get_task(task_id)
    assert task["state"] == TaskState.BLOCKED, "超限应转人工，不是继续转圈"

    blocked = [e for e in store.list_event_log(plan_id)
               if e["to_state"] == TaskState.BLOCKED]
    assert blocked[-1]["detail"]["reason"] == "replan_limit_exceeded"
    assert blocked[-1]["detail"]["await"] == "human_decision"
    assert blocked[-1]["detail"]["gate_results"][GATEWAY_GATE] == "fail", \
        "转人工那一刻的证据链要能追回到第七道闸"


def test_the_whole_chain_is_readable_in_the_event_log(monkeypatch):
    """同一条链路，改从 event_log 读一遍 —— 审计要能复述发生了什么。"""
    monkeypatch.setenv("MAOS_MAX_REPLAN", "1")
    store, bus, cp, gate = _build()
    plan_id, task_id = _make_task(cp)
    cp.set_replanner(lambda *, goal, findings, open_tasks: [
        {"role": "payment", "title": "改走备用渠道", "inputs": {}, "acceptance": []}])

    _gateway_round(bus, gate, cp, plan_id, task_id, 1, CODE_RETRIABLE_FAILED)
    _gateway_round(bus, gate, cp, plan_id, task_id, 2, CODE_RETRIABLE_FAILED)

    log = store.list_event_log(plan_id)
    assert [e["event_type"] for e in log].count("Replanned") == 1
    assert any(e["event_type"] == "PlanTransition"
               and (e["from_state"], e["to_state"]) == (PlanState.RUNNING, PlanState.PENDING)
               for e in log), "缺 replan 那条 Plan 迁移"
    tail = [e for e in log if e["event_type"] == "StateTransition"
            and e["task_id"] == task_id][-1]
    assert (tail["from_state"], tail["to_state"]) == (TaskState.AWAITING_REVIEW,
                                                      TaskState.BLOCKED)
    assert tail["detail"]["reason"] == "replan_limit_exceeded"


def test_unknown_outcome_never_reaches_the_replan_path_end_to_end():
    """同一条链路换成 ACQ.SYSTEM_ERROR：闸放行、不重规划、更不重发。

    这是上面那条链路的反面 —— 「可重试」三个字不足以决定要不要重发。
    """
    store, bus, cp, gate = _build()
    plan_id, task_id = _make_task(cp)
    cp.set_replanner(lambda *, goal, findings, open_tasks: pytest.fail(
        "outcome=unknown 竟然触发了重规划 —— 那一笔可能已经退出去了"))

    _gateway_round(bus, gate, cp, plan_id, task_id, 1, CODE_RETRIABLE_UNKNOWN)

    assert cp._replan_used(plan_id) == 0
    assert store.get_task(task_id)["state"] == TaskState.DONE, \
        "闸放行且非高风险任务，应正常收敛 —— 不是返工，也不是重规划"


# ======================================================================
# 5. 场景 7 回归：第七道闸认出它，但不许挡住它
# ======================================================================
@pytest.fixture(scope="module")
def driven_s7():
    return s7.drive()


def test_scenario_7_error_code_sits_in_the_query_first_quadrant():
    """场景 7 注入的码就落在「可重试但下落不明」那一格 —— 所以它不该 replan。"""
    entry = GC.ALL_CODES[s7.GATEWAY_ERROR_CODE]
    assert (entry.retriable, entry.outcome) == (True, GC.OUTCOME_UNKNOWN)
    assert _finding_for(s7.GATEWAY_ERROR_CODE)["disposition"] == GW_QUERY_FIRST


def test_scenario_7_payment_gate_notes_the_receipt_without_blocking(driven_s7):
    """场景 7 的**收口那一轮**：第七道闸认出了回执（noted），但闸仍放行。

    放行之后走的是 `effect_risk=H` 的人工审批 -> 主管驳回 -> 补偿收口，
    与加这道闸之前一字不差。闸一旦在**这一轮**判 rework，整个失败路径就被改写了。

    断言从「一次 REWORK 都不许有」收窄成「收口那一轮不许返工」：Y-4 之后场景 7
    先撞一次 `40005`（`retriable=True + outcome=failed`）演换渠道，那一格 severity
    恒为 blocker，**必然**产生一次返工 —— 那是要给评委看的那一段，不是回归。
    这条断言原来的形状假设了场景 7 只撞一个码（见 `docs/BACKLOG.md` 的
    `## task-Y4` 第 1 条）。守的东西一点没让：`ACQ.SYSTEM_ERROR` 那一格
    仍然一次都不许返工，而且全场恰好只返工一次 —— 多一次就是自旋。
    """
    store, plan_id = driven_s7["store"], driven_s7["plan_id"]
    moves = [e for e in store.list_event_log(plan_id)
             if e["event_type"] == "StateTransition" and e["task_id"] == s7.TASK_PAYMENT]

    reviewed = [e for e in moves if e["from_state"] == TaskState.AWAITING_REVIEW]
    assert reviewed and reviewed[-1]["to_state"] == TaskState.BLOCKED, (
        f"收口那一轮（{s7.GATEWAY_ERROR_CODE}）被判成了 "
        f"{reviewed[-1]['to_state'] if reviewed else '没过闸'} —— "
        "第七道闸把场景 7 的失败路径改写了")
    assert sum(1 for e in moves if e["to_state"] == TaskState.REWORK) == 1, \
        f"场景 7 应恰好返工一次（{s7.GATEWAY_RETRIABLE_CODE} 触发换渠道），多于一次即自旋"

    blocked = [e for e in moves if e["to_state"] == TaskState.BLOCKED]
    assert blocked, "付款任务应停在 BLOCKED 等人处置"
    results = blocked[-1]["detail"]["gate_results"]
    assert results[GATEWAY_GATE] == "noted", \
        f"第七道闸没认出场景 7 的网关回执，实际 {results.get(GATEWAY_GATE)!r}"
    assert all(v in ("pass", "noted") for v in results.values()), \
        f"场景 7 的付款任务不该有任何一道闸判 fail，实际 {results}"
