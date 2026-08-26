"""契约边界测试 —— 这些行为在分轨前必须锁死。

两条轨道各自改代码时，只要跑挂了这里任何一条，说明动到了共享契约，
必须先同步确认，不能单方面改。

Phase 0 迁移说明：原 python/tests/test_contracts.py 的 9 条契约断言逐条保留，
仅把自带的 @case 收集器换成 pytest 原生形式，断言语义一字未改。
"""

from __future__ import annotations

import json

import pytest

import maos.agents.coding  # noqa: F401 —— import 即注册进 AGENT_POOL
from maos.agents.base import PermissionDenied, TaskContext
from maos.agents.coding import CodingAgent
from maos.contracts import events as E
from maos.contracts.events import Topic
from maos.contracts.states import IllegalTransition, TaskState, assert_transition
from maos.core.control_plane import ControlPlane
from maos.core.eventbus import InMemoryEventBus
from maos.core.store import SqliteStore
from maos.model.client import ModelResponse, ScriptedModelClient
from maos.runtime.worker import WorkerRuntime


def _boot():
    store = SqliteStore(); store.init_schema()
    bus = InMemoryEventBus(); cp = ControlPlane(store, bus)
    return store, bus, cp


# ---------------------------------------------------------------- 状态机
def test_illegal_transition_raises():
    """跳过 Gate 直接置 DONE 必须抛异常，不能静默通过。"""
    with pytest.raises(IllegalTransition):
        assert_transition(TaskState.RUNNING, TaskState.DONE)


def test_all_states_reachable():
    """每个状态都必须至少有一条入边和出边（终态除外），否则是死状态。"""
    from maos.contracts.states import TASK_TRANSITIONS, TERMINAL_STATES
    srcs = {a for a, _ in TASK_TRANSITIONS}
    dsts = {b for _, b in TASK_TRANSITIONS}
    all_states = {v for k, v in vars(TaskState).items() if not k.startswith("_")}
    for s in all_states:
        if s != TaskState.PENDING:
            assert s in dsts, f"{s} 没有任何入边，永远到不了"
        if s not in TERMINAL_STATES:
            assert s in srcs, f"{s} 没有任何出边，进去就卡死"


# ---------------------------------------------------------------- 权限
def test_agent_rejects_over_risk():
    agent = CodingAgent(ScriptedModelClient())
    with pytest.raises(PermissionDenied):
        agent.check_risk("H")


def test_agent_rejects_unlisted_tool():
    agent = CodingAgent(ScriptedModelClient())
    with pytest.raises(PermissionDenied):
        agent.check_tool("ci-mcp")


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
def test_duplicate_claim_ignored():
    store, bus, cp = _boot()
    pid = cp.create_plan(goal="g", trace_id="tr", tasks=[
        {"role": "coding", "title": "t", "inputs": {}, "acceptance": []}])
    cp.start_plan(pid)
    tid = store.list_tasks(pid)[0]["task_id"]
    assert cp.claim(tid, "w1", 1) is not None, "首次认领应成功"
    assert cp.claim(tid, "w2", 1) is None, "同一 attempt 重复认领应被拒绝"


def test_retry_exhausted_goes_failed():
    """连续失败到 max_attempts 后必须落 FAILED，不能无限重试。"""
    class AlwaysBroken(ScriptedModelClient):
        def complete(self, **kw):
            return ModelResponse(text="这不是 JSON")

    store, bus, cp = _boot()
    WorkerRuntime(worker_id="w1", bus=bus, control_plane=cp, model=AlwaysBroken())
    pid = cp.create_plan(goal="g", trace_id="tr", tasks=[
        {"role": "coding", "title": "t", "inputs": {}, "acceptance": [], "max_attempts": 3}])
    cp.start_plan(pid)
    bus.drain()
    task = store.list_tasks(pid)[0]
    assert task["state"] == TaskState.FAILED, f"期望 FAILED，实际 {task['state']}"
    assert task["attempt"] == 3, f"期望重试到 3 次，实际 {task['attempt']}"


def test_invalid_event_rejected():
    """契约校验失败的事件必须被拒绝，最终进死信，而不是污染状态。"""
    store, bus, cp = _boot()
    bad = E.task_result(plan_id="p", task_id="t", attempt=1, trace_id="tr", status="ok")
    bad.payload["status"] = "whatever"          # 非法 status
    bus.publish(Topic.TASK_RESULT, bad)
    bus.drain()
    assert bus.dead_letters, "非法事件没有进死信队列"


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
