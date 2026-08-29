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
def builtin_probe_dir(tmp_path, monkeypatch):
    """给 builtin 包临时挂一个额外的搜索目录 —— 探针文件投这里，不碰真源码树。

    为什么不再直接往 maos/skills/builtin/ 写文件：pytest 被 Ctrl-C / OOM / CI 超时
    杀掉时 finally 跑不到，残留的 probe_*.py 会被 builtin/__init__.py 末尾那句模块级
    discover() 自动注册，下一次全量测试就在 :74 那条断言上假红 —— 而红的原因与本次
    真实改动毫无关系，排查方向完全错。（实测复现过：残留一个文件 → 1 failed / 105 passed。）

    包的 __path__ 是个 list，pkgutil.iter_modules 与子模块 import 都按它找模块，
    所以追加一个 tmp_path 目录就够 discover() 扫到探针，且残留随 tmp_path 一起被回收。
    """
    probe_dir = tmp_path / "builtin_probe"
    probe_dir.mkdir()
    # 换成新 list 而不是原地 append：monkeypatch 退出时整体还原，
    # 测试中途抛异常也不会把额外路径留在包对象上污染后续测试。
    monkeypatch.setattr(builtin, "__path__", [*builtin.__path__, str(probe_dir)])

    # 把 registry._discovered 复位成 False，让 :74 那条断言**每次**都真的走一遍
    # get() 的自动发现分支，而不是靠「恰好跑在别的测试后面、标志已被置位」的运气
    # （原来它绿不绿取决于测试执行顺序，这是隐性的）。
    #
    # 复位之后它仍然绿，靠的是 _discover_builtin() 用的是 `import maos.skills.builtin`
    # 而 sys.modules 里已有缓存 —— 那次 import 是空操作，不重扫目录。
    # 这层依赖是承重的，别当它是巧合：谁把 registry.py 里那句 import 改成
    # builtin.discover()（该文件第 47-48 行明令禁止），这里就会扫到探针并注册，
    # :74 当场变红。那正是要的效果 —— 这条断言同时也是那条禁令的守卫。
    monkeypatch.setattr(registry, "_discovered", False)
    return probe_dir


@pytest.fixture
def probe_module(builtin_probe_dir):
    """往临时 builtin 搜索目录投一个新 skill 文件；退出时把注册表和模块缓存清干净。

    不再需要删文件、清 __pycache__：两者都落在 tmp_path 里，由 pytest 负责回收。
    仍然要手工清的是**进程内**状态 —— sys.modules 与 SKILL_REGISTRY 跨测试共享。
    """
    # 进入前先清一次，不只是退出时清。把探针挪进 tmp_path 解决了「我们自己制造残留」，
    # 但解决不了「环境里本来就有残留」：真源码树里若躺着一个旧的 probe_*.py（旧分支
    # 带进来的、或本次改动之前被 Ctrl-C 杀掉留下的），builtin/__init__.py 末尾那句
    # 模块级 discover() 在 import 阶段就把它注册了 —— 于是 :74 拿到的是它，断言照样红，
    # 而报错信息「投放后、discover 前不应已注册」会把人引向完全错误的方向。
    # 测试对环境该是免疫的：起跑线自己划，不继承。
    sys.modules.pop(f"maos.skills.builtin.{PROBE_NAME}", None)
    SKILL_REGISTRY.pop(PROBE_SKILL, None)

    path = builtin_probe_dir / f"{PROBE_NAME}.py"
    path.write_text(PROBE_SOURCE, encoding="utf-8")
    try:
        yield path
    finally:
        sys.modules.pop(f"maos.skills.builtin.{PROBE_NAME}", None)
        SKILL_REGISTRY.pop(PROBE_SKILL, None)


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
    """幂等不只是「两次返回的模块名列表相同」—— 注册表状态也必须一字不差。

    只比返回值挡不住真正会出事的两种走样，而它们都是静默的：
    两次调用之间把 SKILL_REGISTRY[PROBE_SKILL] 换成了另一个类对象（同名不同物，
    已经持有旧引用的调用方行为开始漂移），或者多注册出一个版本（get() 默认取最高版，
    于是悄悄改派到另一个实现）。这两种情况下原来那条断言照样绿。
    """
    first = builtin.discover()
    snapshot = dict(SKILL_REGISTRY[PROBE_SKILL])

    second = builtin.discover()

    assert first == second, "两次 discover() 的模块名列表应一致"
    assert dict(SKILL_REGISTRY[PROBE_SKILL]) == snapshot, (
        "discover() 幂等：版本集合、以及每个版本对应的类对象，都必须原样不变"
    )


def test_private_modules_are_skipped(builtin_probe_dir):
    """下划线开头视为私有、不发现（C-1 补充约定）。

    这条不是风格洁癖：上面那个 probe fixture 之所以敢往 builtin 搜索路径里扔文件，
    靠的就是「不叫下划线就会被发现、叫下划线就不会」这个二分。
    约定只写在 PROBE_NAME 的行内注释里没有把守，改坏了要到别人投放 skill 时才发现。
    """
    path = builtin_probe_dir / "_private_probe.py"
    path.write_text("SHOULD_NOT_BE_DISCOVERED = True\n", encoding="utf-8")
    try:
        found = builtin.discover()

        assert "_private_probe" not in found, f"下划线模块不该被发现：{found}"
        assert "maos.skills.builtin._private_probe" not in sys.modules, (
            "下划线模块连 import 都不该发生 —— 只把它挡在返回值外不够，"
            "import 副作用（注册、建连接）已经跑完了"
        )
    finally:
        # 只清进程内状态。原来这里还要删文件、清 __pycache__、再补跑一次
        # discover() 把真源码树的状态复原 —— 探针挪进 tmp_path 之后，
        # 真源码树自始至终没被动过，那三步都不再需要。
        sys.modules.pop("maos.skills.builtin._private_probe", None)


# --- C-2 AGENT_POOL 注册口径 -----------------------------------------------
def test_agent_pool_contains_the_five_kernel_roles():
    """五个内核角色必须在池中，ManagerAgent 必须不在。业务域角色不在本条管辖内。

    原来这里断的是「恰好五个」。那个口径在退款域投放四个 Agent 之后当场变红 ——
    而投放是对的：注册表的整个设计就是「新增 Agent 只投文件」，一个每加一个业务域
    就要回来改一次的断言，守的不是口径，是「没人加过域」这件事。改成子集之后，
    这条守住的仍是它真正要守的两件：内核五角色一个都不许少（少了说明自动发现坏了），
    ManagerAgent 一个都不许多（它不经 worker 分发，混进池里会被错误地当成可派发角色）。
    """
    kernel_roles = {"architecture", "coding", "requirement", "reviewer", "testing"}
    missing = kernel_roles - set(AGENT_POOL)
    assert not missing, (
        f"内核角色从 AGENT_POOL 里少了：{sorted(missing)}。"
        "Phase 2 起是五个角色：coding 加本轮补齐的 "
        "requirement / architecture / testing / reviewer。"
        "少了通常意味着自动发现坏了，而不是谁故意删的。"
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
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        # 没有 timeout= 时，配合 capture_output=True，子进程一挂就是**静默卡死**：
        # 全量测试既没有输出、也不会超时、更拿不到 stderr，人只能看着它停在这里。
        # 转成带诊断的失败，别让它裸抛一个不含上下文的 TimeoutExpired。
        captured = []
        for label, raw in (("stdout", exc.stdout), ("stderr", exc.stderr)):
            text = raw.decode(errors="replace") if isinstance(raw, bytes) else (raw or "")
            captured.append(f"{label}:\n{text}")
        pytest.fail(
            f"子进程 {exc.timeout}s 未退出，已被杀掉（包级自动发现可能死锁或卡在 import）。\n"
            + "\n".join(captured)
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


# --- C-6 matrix 缺 env 时降级等价（不是「恒回退成 inner 对象」）----------------
def test_build_matrix_falls_back_to_inner_bus():
    """无 env 时 build(matrix=True) 必须降级 log-only，且三方法行为与 inner 完全一致。

    判据在 Task-E 落地后升级过一次。原断言写的是「回退到 InMemoryEventBus 对象」，
    它的前提就写在自己的消息里 ——「hiclaw/matrix_bus.py **落地前**」：那时
    `_wrap_matrix` 恒走 ImportError 分支，返回的确实是 inner 本身。文件一落地，
    该分支不再命中，返回的是 MatrixEventBus，这条判据自然失效。

    语义没有变 —— 缺 env 不许中断 —— 变的是怎么验它：从「比对象类型」升级成
    「比降级模式下的行为」。后者才是当初真正要守的东西。对象类型对了而 drain 的
    返回值不对，演示照样当场垮；反过来只要行为等价，包不包一层根本不影响任何调用方。
    """
    # 函数内 import：本文件其余几十条断言与 hiclaw 无关，不该因为可选依赖层缺席
    # 而在 collection 阶段集体失败。
    import os

    from hiclaw.matrix_bus import MatrixEventBus
    from maos.contracts.events import Envelope

    # 前置断言（H-6）：本条用例的「无 env」是**前提**，不是它自己造出来的 ——
    # build() 里的 MatrixBusConfig.from_env() 读的是进程真环境，起跑线由
    # maos/tests/conftest.py 的 _no_ambient_matrix_env 划。没有这句，起跑线一旦
    # 被破坏（演示机上 export 了真键），失败消息会是下面那句「无 env 必须自动降级」，
    # 把「机器上有键」念成「降级逻辑坏了」—— C-2 与 C-4 当时正是被这个形态误导的，
    # 而且那一次 pytest 已经先把 22 条消息发进真房间了。
    for name in ("MATRIX_HOMESERVER", "MATRIX_USER", "MATRIX_TOKEN", "MATRIX_ROOM_ID"):
        assert not (os.environ.get(name) or "").strip(), (
            f"起跑线被破坏：{name} 还留在进程环境里。本条验的是「无 env 时降级」，"
            "该变量本应由 maos/tests/conftest.py 的 _no_ambient_matrix_env 删掉。"
            "先查那个 fixture 还在不在，别改下面的降级断言。"
        )

    _, bus, _, _, _, _ = build({}, matrix=True)
    assert isinstance(bus, MatrixEventBus), "matrix=True 落地后应返回 MatrixEventBus"
    assert bus.config.log_only is True, "无 env 必须自动降级 log-only，不许抛异常"

    # 用一个没人订阅的探针 topic：build() 已经把 ControlPlane / Worker 挂在了几个
    # 正式 topic 上，借用它们会把状态机的重投与死信也搅进来，验不出总线本身。
    seen: list[Envelope] = []
    bus.subscribe("maos.probe", "probe", seen.append)
    env = Envelope(event_type="Probe", plan_id="p", task_id="t", idempotency_key="probe:1")
    bus.publish("maos.probe", env)
    assert bus.drain() == 1, "降级模式下 drain 的返回值应与 inner 一致"
    assert seen == [env], "降级模式下 publish/subscribe 没有原样委托给 inner"


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
    # 哨兵必须始终查不到。哪天真有人实现了 probe.never-implemented，下面就变成一次
    # **真调用**，而断言照样绿 —— 本测试从此静默失效，软兜底这条路再没人把守。
    # 所以把「哨兵未注册」本身也钉成断言：失效要当场变红，不要等到出事。
    assert registry.get("probe.never-implemented") is None, (
        "probe.never-implemented 被注册了：它是永不实现的哨兵，请给那个 skill 改名"
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

    先摘掉三个 MAOS_LLM_*：真模型分支落地后，**不带 force_scripted** 的那次调用
    取决于环境变量，配齐 key 的机器（演示机就是）会拿到 GatewayModelClient，
    下面的 isinstance 就红了 —— 红的原因与本测试要守的签名冻结毫无关系。
    这里摘的是环境，不是语义：force_scripted=True 那条断言原样还在。
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
