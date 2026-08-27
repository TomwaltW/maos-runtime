"""Task-0 骨架的机器验收 —— 冻结契约 C-1/C-2/C-4/C-5/C-6 与附录 A-5/A-7/A-8。

这些断言是给**后面五条并行轨**用的：谁把动态发现改回显式清单、谁调换了 build()
的返回位序、谁擅自给 ManagerAgent 挂上 @register，都会在这里立刻变红。
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

from maos.agents import AGENT_POOL
from maos.agents.base import PermissionDenied
from maos.agents.coding import CodingAgent
from maos.agents.manager import ManagerAgent
from maos.artifacts import KIND_COMPENSATION, resolve_patch_ref, validate_artifact
from maos.core.control_plane import ControlPlane
from maos.core.eventbus import InMemoryEventBus
from maos.core.store import SqliteStore
from maos.flows.common import build
from maos.model.client import ModelClient, ScriptedModelClient
from maos.runtime.gate import ReviewerGate
from maos.runtime.worker import WorkerRuntime
from maos.skills import builtin, registry
from maos.skills.invoker import SkillInvoker
from maos.skills.registry import SKILL_REGISTRY

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
BUILTIN_DIR = pathlib.Path(builtin.__file__).parent

PROBE_NAME = "probe_autodiscovery_tmp"          # 不能下划线开头，否则 discover 会跳过
PROBE_SKILL = "probe.autodiscovery"
PROBE_SOURCE = '''"""测试期临时投放的探针 skill，跑完即删。"""
from maos.skills.contract import Skill, SkillContract
from maos.skills.registry import register_skill


@register_skill
class ProbeSkill(Skill):
    contract = SkillContract(name="probe.autodiscovery", version="1.0.0",
                             purpose="验证 builtin 动态发现")

    def run(self, payload, ctx):
        return {"echo": payload}
'''


@pytest.fixture
def probe_module():
    """往 builtin/ 投一个新 skill 文件；退出时连模块缓存和注册表一起清干净。"""
    path = BUILTIN_DIR / f"{PROBE_NAME}.py"
    path.write_text(PROBE_SOURCE, encoding="utf-8")
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)
        sys.modules.pop(f"maos.skills.builtin.{PROBE_NAME}", None)
        SKILL_REGISTRY.pop(PROBE_SKILL, None)
        for stale in (BUILTIN_DIR / "__pycache__").glob(f"{PROBE_NAME}*"):
            stale.unlink(missing_ok=True)


# --- C-1 builtin 动态发现 --------------------------------------------------
def test_builtin_discovers_new_skill_without_touching_init(probe_module):
    init_py = BUILTIN_DIR / "__init__.py"
    before = init_py.read_bytes()

    assert registry.get(PROBE_SKILL) is None, "投放后、discover 前不应已注册"

    found = builtin.discover()

    assert PROBE_NAME in found, f"discover() 没扫到新投放的模块：{found}"
    cls = registry.get(PROBE_SKILL)
    assert cls is not None and cls.contract.version == "1.0.0"
    assert init_py.read_bytes() == before, "动态发现绝不该改动 builtin/__init__.py（C-1）"


def test_discover_is_idempotent(probe_module):
    assert builtin.discover() == builtin.discover()


# --- C-2 AGENT_POOL 注册口径 -----------------------------------------------
def test_agent_pool_is_exactly_coding():
    assert sorted(AGENT_POOL) == ["coding"], (
        "AGENT_POOL 口径变了。ManagerAgent 刻意不挂 @register（它不经 worker 分发），"
        "不要'顺手'注册它；新增 Agent 请只投文件。"
    )
    assert AGENT_POOL["coding"] is CodingAgent
    assert ManagerAgent.identity.role not in AGENT_POOL


# --- C-4 build() 返回契约 ---------------------------------------------------
def test_build_returns_frozen_six_tuple():
    result = build({})
    assert len(result) == 6, "build() 返回六元组，位序冻结（C-4）"

    store, bus, cp, model, worker, gate = result
    assert isinstance(store, SqliteStore)
    assert isinstance(bus, InMemoryEventBus)
    assert isinstance(cp, ControlPlane)
    assert isinstance(model, ModelClient) and isinstance(model, ScriptedModelClient)
    assert isinstance(worker, WorkerRuntime)
    assert isinstance(gate, ReviewerGate)

    assert worker.worker_id == "w1"
    assert store.list_event_log("no-such-plan") == [], "build() 返回的 store 必须已 init_schema"


def test_build_injects_given_model():
    injected = ScriptedModelClient({"x": "y"})
    _, _, _, model, _, _ = build({"ignored": "z"}, model=injected)
    assert model is injected, "传入 model 实例时必须原样注入，不得按 script 另造一个"


# --- C-6 Task-0 期 matrix 恒回退 -------------------------------------------
def test_build_matrix_falls_back_to_inner_bus():
    _, bus, _, _, _, _ = build({}, matrix=True)
    assert isinstance(bus, InMemoryEventBus), (
        "hiclaw/matrix_bus.py 落地前，matrix=True 必须告警回退进程内总线，不许抛异常"
    )


# --- A-5 invoker 的两条不对称 ----------------------------------------------
def test_unlisted_skill_raises_permission_denied():
    inv = SkillInvoker(CodingAgent.identity, None)
    with pytest.raises(PermissionDenied):
        inv.invoke("req.normalize", {})          # manager 的 skill，不在 coding 白名单


def test_unregistered_skill_returns_soft_failure():
    inv = SkillInvoker(CodingAgent.identity, None)
    res = inv.invoke("code.repo-patch", {})      # 在白名单内，但还没人实现
    assert res.status == "failed"
    assert res.error == "skill_not_found:code.repo-patch", (
        "未注册的 skill 必须软兜底成 failed，不能抛 —— 跨轨按名互调要能先行"
    )


def test_skill_invocation_writes_event_log_row():
    store = SqliteStore()
    store.init_schema()
    inv = SkillInvoker(CodingAgent.identity, store)

    inv.invoke("kb.retrieve", {"keyword": "x"}, extras={"plan_id": "plan-test"})

    rows = store.list_event_log("plan-test")
    assert len(rows) == 1
    row = rows[0]
    assert row["event_type"] == "SkillInvoked", "落的是 event_log 行，不是总线事件"
    assert row["from_state"] == "" and row["to_state"] == ""
    assert set(row["detail"]) == {
        "skill", "version", "status", "duration_ms",
        "input_digest", "output_hash", "usage",
    }
    assert row["detail"]["skill"] == "kb.retrieve"


# --- C-5 / A-7 补偿 golden fixture 与引用解析 -------------------------------
def test_compensation_golden_fixture_shape():
    golden = json.loads((FIXTURES / "compensation_golden.json").read_text(encoding="utf-8"))
    assert golden["kind"] == KIND_COMPENSATION
    assert golden["content"]["mode"] == "reverse"
    assert golden["content"]["patch_ref"] == {
        "task_id": "task-cmp-golden-001", "kind": "patch_set", "attempt": 1,
    }
    assert validate_artifact(golden["kind"], golden["content"]) == []


def test_resolve_patch_ref_positive_and_negative():
    golden = json.loads((FIXTURES / "compensation_golden.json").read_text(encoding="utf-8"))
    ref = golden["content"]["patch_ref"]
    store = SqliteStore()
    store.init_schema()

    assert resolve_patch_ref(store, ref) is None, "没有对应 patch_set 时必须返回 None"

    store.insert_artifact({
        "artifact_id": "art-golden", "task_id": ref["task_id"], "plan_id": "plan-1",
        "kind": "patch_set", "version": ref["attempt"],
        "content": {"files": [{"path": "a.py", "diff": "@@"}], "summary": "s",
                    "self_check": {"build": "pass", "lint": "pass"}},
    })
    got = resolve_patch_ref(store, ref)
    assert got is not None and got["artifact_id"] == "art-golden"

    assert resolve_patch_ref(store, {**ref, "attempt": 2}) is None, "attempt 不匹配不该命中"


# --- A-8 knowledge 新增表 ---------------------------------------------------
def test_knowledge_insert_and_filter():
    store = SqliteStore()
    store.init_schema()
    store.insert_knowledge({"id": "kn-1", "plan_id": "plan-1", "kind": "rule",
                            "title": "补丁不许改测试", "body": "改测试让测试过是禁止的",
                            "tags": ["security", "patch"]})
    store.insert_knowledge({"id": "kn-2", "plan_id": "plan-1", "kind": "case",
                            "title": "会话提前过期", "body": "本地时区导致的判定错误",
                            "tags": ["bug"]})

    assert len(store.list_knowledge()) == 2
    assert [r["id"] for r in store.list_knowledge(tags=["bug"])] == ["kn-2"]
    assert [r["id"] for r in store.list_knowledge(keyword="时区")] == ["kn-2"]
    assert store.list_knowledge(tags=["nope"]) == []
    assert store.list_knowledge()[0]["tags"] == ["security", "patch"], "tags 应解回 list"
