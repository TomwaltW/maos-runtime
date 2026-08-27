"""Task-0 骨架的机器验收 —— 冻结契约 C-1/C-2/C-4/C-5/C-6 与附录 A-5/A-7/A-8。

这些断言是给**后面五条并行轨**用的：谁把动态发现改回显式清单、谁调换了 build()
的返回位序、谁擅自给 ManagerAgent 挂上 @register，都会在这里立刻变红。
"""

from __future__ import annotations

import inspect
import json
import pathlib
import subprocess
import sys

import pytest

from maos.agents import AGENT_POOL
from maos.agents.base import AgentIdentity, PermissionDenied
from maos.agents.coding import CodingAgent
from maos.agents.manager import ManagerAgent
from maos.artifacts import KIND_COMPENSATION, resolve_patch_ref, validate_artifact
from maos.core.control_plane import ControlPlane
from maos.core.eventbus import InMemoryEventBus
from maos.core.store import SqliteStore
from maos.flows.common import build
from maos.model.client import ModelClient, ScriptedModelClient, select_model_client
from maos.runtime.gate import ReviewerGate
from maos.runtime.worker import WorkerRuntime
from maos.skills import builtin, registry
from maos.skills.invoker import SkillInvoker
from maos.skills.registry import SKILL_REGISTRY

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
BUILTIN_DIR = pathlib.Path(builtin.__file__).parent
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

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


def test_private_modules_are_skipped():
    """下划线开头视为私有、不发现（C-1 补充约定）。

    这条不是风格洁癖：上面那个 probe fixture 之所以敢往 builtin/ 里扔文件，
    靠的就是「不叫下划线就会被发现、叫下划线就不会」这个二分。
    约定只写在 PROBE_NAME 的行内注释里没有把守，改坏了要到别人投放 skill 时才发现。
    """
    path = BUILTIN_DIR / "_private_probe.py"
    path.write_text("SHOULD_NOT_BE_DISCOVERED = True\n", encoding="utf-8")
    try:
        found = builtin.discover()

        assert "_private_probe" not in found, f"下划线模块不该被发现：{found}"
        assert "maos.skills.builtin._private_probe" not in sys.modules, (
            "下划线模块连 import 都不该发生 —— 只把它挡在返回值外不够，"
            "import 副作用（注册、建连接）已经跑完了"
        )
    finally:
        path.unlink(missing_ok=True)
        sys.modules.pop("maos.skills.builtin._private_probe", None)
        for stale in (BUILTIN_DIR / "__pycache__").glob("_private_probe*"):
            stale.unlink(missing_ok=True)
        builtin.discover()


# --- C-2 AGENT_POOL 注册口径 -----------------------------------------------
def test_agent_pool_is_exactly_coding():
    assert sorted(AGENT_POOL) == ["coding"], (
        "AGENT_POOL 口径变了。ManagerAgent 刻意不挂 @register（它不经 worker 分发），"
        "不要'顺手'注册它；新增 Agent 请只投文件。"
    )
    assert AGENT_POOL["coding"] is CodingAgent
    assert ManagerAgent.identity.role not in AGENT_POOL


def test_agent_pool_does_not_depend_on_main_import_order():
    """不经 maos.main 也要拿到非空池 —— 删除 main.py:16 的直接回归闸（C-2）。

    本进程里 maos.main 早被别的测试拉起来了，验不出这件事，所以必须开子进程。
    谁把自动发现改回手工 import，主路径（python3 run.py）照样绿，
    只有「不经 main.py 的入口」会漏注册 —— 这条就是那个入口的替身。
    """
    code = (
        "import sys;"
        "from maos.agents import AGENT_POOL;"
        "assert AGENT_POOL, 'AGENT_POOL 为空：包级自动发现没生效';"
        "assert 'maos.main' not in sys.modules, 'maos.main 被意外 import，本测试前提不成立';"
        "print(sorted(AGENT_POOL))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )

    assert proc.returncode == 0, f"子进程失败：\n{proc.stderr}"
    assert "coding" in proc.stdout, f"子进程未发现 coding：{proc.stdout!r}"


# --- C-4 build() 返回契约 ---------------------------------------------------
def test_build_returns_frozen_six_tuple():
    result = build({})
    assert type(result) is tuple, (
        "返回形态冻结为 tuple，不许改成 dataclass / dict / NamedTuple（C-4）。"
        "只断言 len==6 挡不住：dict 与 NamedTuple 同样满足 len==6"
    )
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


def test_build_extra_params_are_keyword_only():
    """matrix / model 必须是 keyword-only（C-3 形态约束）。

    改成位置参数不会当场报错，只会让四处 `store, bus, cp, model, worker, gate = build(...)`
    静默错位 —— 症状离原因很远，所以把它钉在断言里。
    """
    with pytest.raises(TypeError):
        build({}, True)             # 想按位置传 matrix
    with pytest.raises(TypeError):
        build({}, True, None)       # 想按位置传 matrix + model


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


# 专用探针 identity：白名单里那个名字**永远不会有人实现**，所以这条断言不会
# 随某条轨落地真 skill 而变红。别改回 code.repo-patch / kb.retrieve —— 前者
# 是 Task-A 的活、后者是 Task-D 的活，一落地这条就假红。
_PROBE_IDENTITY = AgentIdentity(
    agent_id="probe-unregistered",
    role="probe",
    duty="只为验证「白名单内 + 未注册」这条软兜底路径",
    allowed_skills=frozenset({"probe.never-implemented"}),
)


def test_unregistered_skill_returns_soft_failure():
    assert registry.get("probe.never-implemented") is None, (
        "哨兵名被谁实现了 —— 这条闸就不再走「未注册」分支，下面几条断言变红时"
        "原因会指向别处。换一个没人会实现的名字，别把断言改绿了事"
    )
    inv = SkillInvoker(_PROBE_IDENTITY, None)
    res = inv.invoke("probe.never-implemented", {})   # 在白名单内，但没人实现
    assert res.status == "failed"
    assert res.error == "skill_not_found:probe.never-implemented", (
        "未注册的 skill 必须软兜底成 failed，不能抛 —— 跨轨按名互调要能先行"
    )
    assert res.invocation_id, "早退路径也要带 invocation_id：失败调用同样要可溯源"


def test_skill_invocation_writes_event_log_row():
    store = SqliteStore()
    store.init_schema()
    inv = SkillInvoker(CodingAgent.identity, store)

    res = inv.invoke("kb.retrieve", {"keyword": "x"}, extras={"plan_id": "plan-test"})

    rows = store.list_event_log("plan-test")
    assert len(rows) == 1
    row = rows[0]
    assert row["event_type"] == "SkillInvoked", "落的是 event_log 行，不是总线事件"
    assert row["from_state"] == "" and row["to_state"] == ""
    assert set(row["detail"]) == {
        "skill", "version", "status", "duration_ms",
        "input_digest", "output_hash", "usage", "invocation_id",
    }
    assert row["detail"]["skill"] == "kb.retrieve"
    assert row["detail"]["invocation_id"] == res.invocation_id != "", (
        "落库那行与返回给调用方的 SkillResult 必须是同一个 invocation_id —— "
        "对不上号，权威事实守卫就没法从产物回溯到具体哪次调用"
    )


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


def test_compensation_mode_is_locked_to_reverse():
    """mode 恒为 reverse，别的值一律非法（C-5 字段约束）。

    放行别的 mode 不会当场报错，而是让补偿走不到反向应用分支：
    补偿静默不执行、日志一片正常，要到演示现场才发现文件根本没还原。
    """
    def content(mode):
        return {"mode": mode,
                "patch_ref": {"task_id": "t1", "kind": "patch_set", "attempt": 1}}

    assert validate_artifact(KIND_COMPENSATION, content("reverse")) == []
    for illegal in ("rollback", "forward", "revert", "REVERSE", ""):
        errs = validate_artifact(KIND_COMPENSATION, content(illegal))
        assert errs, f"mode={illegal!r} 必须被拒绝，本阶段不定义第二种补偿模式"


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


# --- A-12 select_model_client 交接闸 -----------------------------------------
def test_select_model_client_signature_is_frozen(monkeypatch):
    """`maos/model/client.py` 在 Task-0 之后移交 Task-A —— 这条是交接闸。

    签名与「恒返确定性模型」的语义都冻结：Task-A 填真模型分支时，
    force_scripted=True 必须仍然拿到 ScriptedModelClient，否则全部测试与场景 5
    会在无 key 的机器上开始打真网络。改坏了这里没有第二处会变红。

    先摘掉三个 ``MAOS_LLM_*``：真模型分支落地后，无参 ``select_model_client()``
    在**配了 key 的机器**（演示机就是）会拿到真客户端，这条当场变红，而红的原因
    与它要守的签名冻结毫无关系。摘的是环境不是语义 —— ``force_scripted=True``
    那条断言原样不动，它守的才是「无论环境如何都要确定性输出」。
    """
    for var in ("MAOS_LLM_BASE_URL", "MAOS_LLM_API_KEY", "MAOS_LLM_MODEL"):
        monkeypatch.delenv(var, raising=False)

    params = inspect.signature(select_model_client).parameters
    assert list(params) == ["script", "force_scripted"]
    assert params["script"].default is None
    assert params["force_scripted"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["force_scripted"].default is False

    assert isinstance(select_model_client(), ScriptedModelClient)
    assert isinstance(select_model_client(force_scripted=True), ScriptedModelClient)

    client = select_model_client({"用户请求": "ok"}, force_scripted=True)
    assert isinstance(client, ModelClient)
    assert client.complete(system="s", user="用户请求", tier="medium").text == "ok", (
        "script 必须真的灌进返回的客户端，而不是被丢掉"
    )
