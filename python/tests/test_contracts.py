"""契约边界测试 —— 这些行为在分轨前必须锁死。

两条轨道各自改代码时，只要跑挂了这里任何一条，说明动到了共享契约，
必须先同步确认，不能单方面改。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agents.coding  # noqa: F401
from agents.base import PermissionDenied, TaskContext
from agents.coding import CodingAgent
from contracts import events as E
from contracts.events import Topic
from contracts.states import IllegalTransition, TaskState, assert_transition
from core.control_plane import ControlPlane
from core.eventbus import InMemoryEventBus
from core.store import SqliteStore
from model.client import ModelResponse, ScriptedModelClient

PASSED, FAILED = [], []


def case(fn):
    try:
        fn()
        PASSED.append(fn.__name__)
    except AssertionError as exc:
        FAILED.append(f"{fn.__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001
        FAILED.append(f"{fn.__name__}: 未预期异常 {type(exc).__name__}: {exc}")
    return fn


def _boot():
    store = SqliteStore(); store.init_schema()
    bus = InMemoryEventBus(); cp = ControlPlane(store, bus)
    return store, bus, cp


# ---------------------------------------------------------------- 状态机
@case
def test_illegal_transition_raises():
    """跳过 Gate 直接置 DONE 必须抛异常，不能静默通过。"""
    try:
        assert_transition(TaskState.RUNNING, TaskState.DONE)
    except IllegalTransition:
        return
    raise AssertionError("RUNNING -> DONE 竟然被允许了")


@case
def test_all_states_reachable():
    """每个状态都必须至少有一条入边和出边（终态除外），否则是死状态。"""
    from contracts.states import TASK_TRANSITIONS, TERMINAL_STATES
    srcs = {a for a, _ in TASK_TRANSITIONS}
    dsts = {b for _, b in TASK_TRANSITIONS}
    all_states = {v for k, v in vars(TaskState).items() if not k.startswith("_")}
    for s in all_states:
        if s != TaskState.PENDING:
            assert s in dsts, f"{s} 没有任何入边，永远到不了"
        if s not in TERMINAL_STATES:
            assert s in srcs, f"{s} 没有任何出边，进去就卡死"


# ---------------------------------------------------------------- 权限
@case
def test_agent_rejects_over_risk():
    agent = CodingAgent(ScriptedModelClient())
    try:
        agent.check_risk("H")
    except PermissionDenied:
        return
    raise AssertionError("coding(max_risk=M) 竟然接受了 H 级执行")


@case
def test_agent_rejects_unlisted_tool():
    agent = CodingAgent(ScriptedModelClient())
    try:
        agent.check_tool("ci-mcp")
    except PermissionDenied:
        return
    raise AssertionError("coding 竟然能调用白名单外的 ci-mcp")


@case
def test_protected_path_blocked():
    """改测试文件 / 碰 /infra 必须被 Agent 自己挡住，不能进 Gate。"""
    bad = json.dumps({"files": [{"path": "tests/test_auth.py", "diff": "+assert True"}],
                      "summary": "改测试让它过", "self_check": {"build": "pass"}})
    agent = CodingAgent(ScriptedModelClient({"任务输入": bad}))
    out = agent.run(TaskContext(plan_id="p", task_id="t", trace_id="tr", attempt=1,
                                inputs={}, acceptance=[], risk_level="L"))
    assert out.status == "failed", "修改测试文件竟然被放行"
    assert out.metrics.get("security_event") is True, "未标记为安全事件"


# ---------------------------------------------------------------- 幂等与重试
@case
def test_duplicate_claim_ignored():
    store, bus, cp = _boot()
    pid = cp.create_plan(goal="g", trace_id="tr", tasks=[
        {"role": "coding", "title": "t", "inputs": {}, "acceptance": []}])
    cp.start_plan(pid)
    tid = store.list_tasks(pid)[0]["task_id"]
    assert cp.claim(tid, "w1", 1) is not None, "首次认领应成功"
    assert cp.claim(tid, "w2", 1) is None, "同一 attempt 重复认领应被拒绝"


@case
def test_retry_exhausted_goes_failed():
    """连续失败到 max_attempts 后必须落 FAILED，不能无限重试。"""
    class AlwaysBroken(ScriptedModelClient):
        def complete(self, **kw):
            return ModelResponse(text="这不是 JSON")

    store, bus, cp = _boot()
    from runtime.worker import WorkerRuntime
    WorkerRuntime(worker_id="w1", bus=bus, control_plane=cp, model=AlwaysBroken())
    pid = cp.create_plan(goal="g", trace_id="tr", tasks=[
        {"role": "coding", "title": "t", "inputs": {}, "acceptance": [], "max_attempts": 3}])
    cp.start_plan(pid)
    bus.drain()
    task = store.list_tasks(pid)[0]
    assert task["state"] == TaskState.FAILED, f"期望 FAILED，实际 {task['state']}"
    assert task["attempt"] == 3, f"期望重试到 3 次，实际 {task['attempt']}"


@case
def test_invalid_event_rejected():
    """契约校验失败的事件必须被拒绝，最终进死信，而不是污染状态。"""
    store, bus, cp = _boot()
    bad = E.task_result(plan_id="p", task_id="t", attempt=1, trace_id="tr", status="ok")
    bad.payload["status"] = "whatever"          # 非法 status
    bus.publish(Topic.TASK_RESULT, bad)
    bus.drain()
    assert bus.dead_letters, "非法事件没有进死信队列"


@case
def test_rework_findings_reach_agent():
    """返工时 findings 必须真的进到 Agent 的 prompt，否则返工等于重跑。"""
    seen = {}

    class Spy(ScriptedModelClient):
        def complete(self, *, system, user, tier):
            seen["user"] = user
            return ModelResponse(text=json.dumps(
                {"files": [{"path": "a.py", "diff": "+x"}], "summary": "s",
                 "self_check": {"build": "pass", "lint": "pass"}}))

    agent = CodingAgent(Spy())
    agent.run(TaskContext(plan_id="p", task_id="t", trace_id="tr", attempt=2,
                          inputs={}, acceptance=[], risk_level="L",
                          rework_findings=[{"gate": "acceptance", "message": "build 未通过"}]))
    assert "build 未通过" in seen["user"], "返工 findings 没有喂回给 Agent"


if __name__ == "__main__":
    for name in PASSED:
        print(f"  PASS  {name}")
    for msg in FAILED:
        print(f"  FAIL  {msg}")
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    sys.exit(1 if FAILED else 0)
