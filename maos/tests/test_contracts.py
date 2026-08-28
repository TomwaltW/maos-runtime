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
from maos.skills.builtin.code_repo_patch import CodeRepoPatchSkill, ProtectedPathViolation
from maos.skills.contract import SkillContext


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


# ------------------------------------------------- 受保护路径判定 & 出参收敛
def _run_patch_skill(files: list, **overrides):
    """直接驱动 code.repo-patch，不经 Agent / invoker。

    判定就在 skill 里，而 invoker 会把异常压成 "<类名>: <消息>" 字符串 ——
    经 Agent 只能断言「失败了」，断言不到是哪条路径、因为什么失败。
    上面那条 test_protected_path_blocked 守的是另一件事（异常到 AgentOutput
    的翻译），两条不重复。
    """
    patch = {"files": files, "summary": "s", "self_check": {"build": "pass"}}
    patch.update(overrides)
    model = ScriptedModelClient({"任务输入": json.dumps(patch)})
    return CodeRepoPatchSkill().run(
        {"title": "t", "inputs": {}, "acceptance": []}, SkillContext(model=model))


@pytest.mark.parametrize("path", [
    "infra/main.tf",
    ".github/workflows/ci.yml",
    "secrets/prod.env",
    "tests/test_auth.py",
])
def test_declared_protected_paths_are_actually_blocked(path):
    """声明拦的四项逐条实拦 —— 一项一个样本，不许再靠一个样本代表四条。

    上一版清单存前缀 ("/infra", "/.github", "tests/", "/secrets") 配
    startswith / 子串判定，四条里只有 tests/ 真生效：仓库相对路径
    "infra/main.tf" 不带前导斜杠，startswith("/infra") 恒 False，
    "/infra" 也不是它的子串。而当年把守闸只喂了 tests/test_auth.py 一个
    样本，正好是唯一生效的那条 —— 于是三条漏拦一直是绿的。
    """
    with pytest.raises(ProtectedPathViolation):
        _run_patch_skill([{"path": path, "diff": "+x"}])


@pytest.mark.parametrize("path", ["maos/infrastructure/db.py", "src/contests/r1.py"])
def test_lookalike_paths_are_not_blocked(path):
    """含受保护词作子串的正常路径必须放行：infrastructure 含 infra、contests 含 tests。

    误拦比漏拦更难查：contract 是 escalate + max_retries=0，当场终止不重试，
    还被 coding.py 标成 security_event —— 现象长得像「真踩了安全边界」，
    排查时最不容易怀疑到匹配式本身。分段相等就是为了从根上消掉这一类。
    """
    out = _run_patch_skill([{"path": path, "diff": "+x"}])
    assert [f["path"] for f in out["files"]] == [path]


@pytest.mark.parametrize("path", [
    "./infra/main.tf",              # ./ 前缀
    "/secrets/prod.env",            # 前导斜杠：声明里就这么写的，照抄不该反而放行
    "maos/../tests/test_x.py",      # .. 回溯
    "../secrets/prod.env",          # normpath 消不掉开头的 ..，得单独滤
    "Secrets/prod.env",             # 大小写：本机 APFS 下与 secrets/ 是同一个文件
    ".github\\workflows\\ci.yml",   # 反斜杠分隔，不归一就整条算一个段
])
def test_protected_paths_survive_path_normalization(path):
    """规范化的每一件事都对应一个绕过口，少做哪件哪件就能绕过去。

    这几条钉的是 docs/DECISIONS.md ## fix-1 里记的判定口径，
    口径要改先改那里 —— 不是改这里让它变绿。
    """
    with pytest.raises(ProtectedPathViolation):
        _run_patch_skill([{"path": path, "diff": "+x"}])


@pytest.mark.parametrize("item", [
    {"path": "a.py"},                       # 缺 diff
    {"path": "a.py", "diff": None},         # diff 为 null
    {"path": "a.py", "diff": 12},           # diff 非 str
])
def test_patch_item_without_valid_diff_is_rejected(item):
    """只校验 path 不校验 diff，代价落在零模型补偿链。

    artifacts.py 反向打补丁时拿不到 diff，补偿会「成功」地什么都没还原 ——
    静默失败，没有任何报错，所以必须在 skill 出口就挡住。
    """
    with pytest.raises(ValueError, match="path/diff"):
        _run_patch_skill([item])


@pytest.mark.parametrize("dirty", [None, "pass", 0, ["build"]])
def test_dirty_self_check_coerced_to_dict_at_skill_exit(dirty):
    """脏 self_check 在 skill 出口就必须是 dict。

    setdefault 只在键缺失时填缺省，键在则原样保留 —— self_check: null 和
    self_check: "pass" 会照原样穿透到 Gate，在 gate.py 的 check.get() 上抛
    AttributeError。gate.review_pending() 在 flows/common.py 是裸调用，
    异常逃出后整个 plan 驱动循环当场崩，连一次返工都退化不出来。
    """
    out = _run_patch_skill([{"path": "a.py", "diff": "+x"}], self_check=dirty)
    assert out["self_check"] == {}


@pytest.mark.parametrize("dirty", [None, 0, {"text": "s"}])
def test_dirty_summary_coerced_to_str_at_skill_exit(dirty):
    out = _run_patch_skill([{"path": "a.py", "diff": "+x"}], summary=dirty)
    assert out["summary"] == ""


def test_valid_self_check_passes_through_untouched():
    """收敛的是类型不是取值：build=fail 必须原样到 Gate。

    skill 抢着判取值，Gate 就永远见不到失败样本，场景 2 的返工链当场断掉。
    """
    out = _run_patch_skill([{"path": "a.py", "diff": "+x"}],
                           self_check={"build": "fail", "lint": "pass"})
    assert out["self_check"] == {"build": "fail", "lint": "pass"}


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
