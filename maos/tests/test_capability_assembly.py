"""能力装配层的守卫 —— 装配只解析、只收窄，闸与审计一条都不许因此松掉。

这一份守的是四条不许退回去的性质：

1. **缺省零改变** —— 不调 ``assemble()`` 的 Agent，``check_tool`` 行为与装配层
   存在之前逐字节一致。1568 条存量测试压着 ``agents/base.py``，装配必须是纯加法。
2. **只收窄不放宽** —— 往装配里塞一个白名单之外的工具，装配**当场失败**。
   这是本文件最重要的一条：如果装配能往白名单里加名字，``check_tool`` 就从一道闸
   退化成一句注释，往档案里写一行就能给自己提权。
3. **有授权无实现要显形** —— ``coding`` / ``testing`` 授权的 ``sandbox`` 全仓没有
   对应 ToolPort（实际存在的是 ``sandbox.git_apply`` / ``sandbox.pytest_run``）。
   装配遇到它既不许 KeyError，也不许静默跳过，得落进报告里被人看见。
4. **审计不断链** —— 装配只解析对象，调用仍走 ``invoke_tool``，``ToolInvoked``
   审计行照旧。

MCP 那一侧一律用 stub 注入：按角色分配 MCP 工具的注册表是别轨的产出，本层不 import
它 —— 装配的口径与「档案/注册表由谁产」无关，这也正是三个参数都做成注入的原因。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from maos.agents import AGENT_POOL
from maos.agents.base import PermissionDenied
from maos.agents.coding import CodingAgent
# 裸名以 Test 开头会被 pytest 当成测试类去收集并告警，沿用仓库既有的改名导入惯例
from maos.agents.testing import TestingAgent as _TestingAgent
from maos.core.store import SqliteStore
from maos.model.client import ScriptedModelClient
from maos.runtime.assembly import (
    AssemblyError,
    ToolNotAssembled,
    assemble,
    build_report,
    collect_local_ports,
)
from maos.tools.port import ToolPort, invoke_tool

ROOT = Path(__file__).resolve().parents[2]
HEADER = re.compile(r"^# generated at \S+ from ([0-9a-f]{40})(-dirty)?$")


def _store():
    s = SqliteStore()
    s.init_schema()
    return s


def _stub_port(name: str, calls: list | None = None) -> ToolPort:
    """一个假的 ToolPort，冒充别轨注册表里按角色分配下来的 MCP 工具。"""
    def entry(**params):
        if calls is not None:
            calls.append(params)
        return {"ok": True, "tool": name}

    return ToolPort(name=name, purpose=f"stub {name}", entry=entry,
                    params_schema={"x": "str"}, returns_schema={"ok": "bool"},
                    failure_modes=["stub 不会失败"], security_boundary="stub",
                    owner="task-T60-test")


class _Profile:
    """冒充职责能力档案：本层只按鸭子类型取名字集合，不认它的类。"""

    def __init__(self, tools=(), skills=()):
        self.allowed_tools = frozenset(tools)
        self.allowed_skills = frozenset(skills)


# ---------------------------------------------------------------------------
# 1. 缺省零改变
# ---------------------------------------------------------------------------

def test_unassembled_agent_behaves_exactly_as_before():
    """没装配过的 Agent：授权内放行、授权外抛 PermissionDenied，消息里仍带白名单。"""
    agent = CodingAgent(ScriptedModelClient())
    assert agent.assembly is None, "缺省必须是没装配过"

    assert agent.check_tool("git-mcp") is None                  # 授权内：放行

    with pytest.raises(PermissionDenied) as exc:
        agent.check_tool("bank.pay")
    msg = str(exc.value)
    assert msg == (f"{agent.identity.agent_id} 无权调用工具 bank.pay"
                   f"（白名单: {sorted(agent.identity.allowed_tools)}）"), (
        "没装配时异常消息必须与装配层存在之前逐字节一致")


def test_assembly_attribute_is_per_instance_not_per_class():
    """装配写实例属性 —— 同角色的另一个实例不该被顺带装上。"""
    one, other = CodingAgent(ScriptedModelClient()), CodingAgent(ScriptedModelClient())
    assemble(one)
    assert one.assembly is not None
    assert other.assembly is None
    assert CodingAgent.assembly is None


# ---------------------------------------------------------------------------
# 2. 只收窄不放宽 —— 本文件最重要的一条
# ---------------------------------------------------------------------------

def test_injected_tool_outside_whitelist_fails_the_assembly():
    """把一个白名单外的工具注入装配 → 当场失败，不是悄悄放行。"""
    agent = CodingAgent(ScriptedModelClient())
    with pytest.raises(AssemblyError) as exc:
        assemble(agent, mcp_ports=[_stub_port("bank.pay")])
    assert "bank.pay" in str(exc.value)
    assert "不许放宽白名单" in str(exc.value)
    assert agent.assembly is None, "装配失败不许留下半成品"


def test_profile_cannot_widen_the_whitelist():
    """档案点名一个白名单外的能力 → 同样当场失败。档案只能收窄授权。"""
    agent = CodingAgent(ScriptedModelClient())
    with pytest.raises(AssemblyError):
        assemble(agent, profile=_Profile(tools={"git-mcp", "bank.pay"}))
    with pytest.raises(AssemblyError):
        assemble(agent, profile=_Profile(skills={"code.repo-patch", "claim.pay"}))
    assert agent.assembly is None


def test_assembled_set_is_a_subset_of_the_authorized_set():
    """全池 22 个角色逐个装配：装出来的工具集永远是授权集的子集。"""
    for role, cls in sorted(AGENT_POOL.items()):
        report = build_report(cls.identity)
        assert set(report.resolved_tools) <= set(cls.identity.allowed_tools), role


def test_profile_narrows_and_records_what_it_withheld():
    """档案只要一部分能力时，剩下的记进 withheld_by_profile，而不是消失。"""
    agent = CodingAgent(ScriptedModelClient())
    report = assemble(agent, profile=_Profile(tools=set()))
    assert report.resolved_tools == ()
    assert set(report.withheld_by_profile) == set(agent.identity.allowed_tools)


# ---------------------------------------------------------------------------
# 3. 有授权无实现：sandbox 那两处漂移必须显形
# ---------------------------------------------------------------------------

def test_authorized_without_impl_is_reported_not_raised():
    """``sandbox`` 有授权、全仓没实现 —— 落进报告，不抛异常、不静默跳过。"""
    ports = collect_local_ports()
    assert "sandbox" not in ports, "前提变了：全仓出现了叫 sandbox 的 ToolPort"

    for cls in (CodingAgent, _TestingAgent):
        agent = cls(ScriptedModelClient())
        report = assemble(agent)                       # 不抛
        assert "sandbox" in report.authorized_without_impl, cls.__name__
        assert "sandbox" not in report.resolved_tools, cls.__name__

    coding = CodingAgent(ScriptedModelClient())
    assemble(coding)
    assert coding.assembly.resolved_tools == ("git-mcp",)
    assert coding.assembly.sources["git-mcp"] == "local"


def test_resolve_tool_separates_missing_impl_from_privilege_denial():
    """有授权没实现 → ToolNotAssembled；越权 → 仍是 PermissionDenied。两条线索不许混。"""
    agent = CodingAgent(ScriptedModelClient())
    assemble(agent)

    with pytest.raises(ToolNotAssembled):
        agent.resolve_tool("sandbox")
    with pytest.raises(PermissionDenied):
        agent.resolve_tool("bank.pay")

    fresh = CodingAgent(ScriptedModelClient())
    with pytest.raises(ToolNotAssembled):
        fresh.resolve_tool("git-mcp")                  # 没装配过：取不到实现


def test_drift_matches_the_repo_wide_scan():
    """全池扫一遍「有授权无实现」，答案必须恰好是 coding / testing 的 sandbox 两处。"""
    drift = {role: list(build_report(cls.identity).authorized_without_impl)
             for role, cls in AGENT_POOL.items()}
    assert {r: v for r, v in drift.items() if v} == {
        "coding": ["sandbox"], "testing": ["sandbox"]}


# ---------------------------------------------------------------------------
# 4. 越权仍是安全事件 + 审计不断链
# ---------------------------------------------------------------------------

def test_assembly_does_not_soften_the_gate():
    """装配之后调白名单外的工具，仍抛 PermissionDenied；诊断信息只是多一句。"""
    agent = CodingAgent(ScriptedModelClient())
    assemble(agent)
    with pytest.raises(PermissionDenied) as exc:
        agent.check_tool("bank.pay")
    assert "无权调用工具 bank.pay" in str(exc.value)
    assert "越权" in str(exc.value)


def test_invocation_after_assembly_still_leaves_an_audit_row():
    """装配只解析对象，不接管调用 —— 经 invoke_tool 调一次，ToolInvoked 照旧落行。"""
    calls: list = []
    agent = CodingAgent(ScriptedModelClient())
    assemble(agent, mcp_ports={"git-mcp": _stub_port("git-mcp", calls)})

    port = agent.resolve_tool("git-mcp")
    assert agent.assembly.sources["git-mcp"] == "mcp", "显式注入应当优先于目录扫描"

    store = _store()
    out = invoke_tool(port, {"op": "baseline"}, store=store,
                      extras={"trace_id": "t-60", "plan_id": "p-60"})
    assert out["ok"] is True and calls == [{"op": "baseline"}]

    rows = [r for r in store.list_event_log("p-60") if r["event_type"] == "ToolInvoked"]
    assert len(rows) == 1
    assert rows[0]["detail"]["tool"] == "git-mcp"
    assert rows[0]["detail"]["status"] == "ok"


def test_injected_mcp_port_is_still_bound_by_the_whitelist():
    """stub 注入走的也是子集约束 —— 注册表给多了，装配失败而不是自动提权。"""
    agent = CodingAgent(ScriptedModelClient())
    with pytest.raises(AssemblyError):
        assemble(agent, mcp_ports={"git-mcp": _stub_port("git-mcp"),
                                   "clearing.cancel": _stub_port("clearing.cancel")})
    assert agent.assembly is None


def test_non_port_injection_is_rejected():
    """注入的不是 ToolPort 就直接抛 —— 别等到调用那一刻才炸在 entry 上。"""
    agent = CodingAgent(ScriptedModelClient())
    with pytest.raises(AssemblyError):
        assemble(agent, mcp_ports={"git-mcp": object()})


# ---------------------------------------------------------------------------
# 5. 能力矩阵证据
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def matrix(tmp_path_factory):
    """真跑一次生成脚本再读产物 —— 证据必须来自真实命令输出（铁律 3）。

    产到临时目录而不是 ``evidence/`` 的正本：正本得在干净工作区里生成（否则首行
    sha 带 ``-dirty``），而测试每跑一次就重写一次正本的话，跑完测试工作区永远是脏的。
    """
    out = tmp_path_factory.mktemp("matrix") / "capability-matrix.json"
    script = ROOT / "scripts" / "gen_capability_matrix.py"
    proc = subprocess.run([sys.executable, str(script), "--out", str(out)],
                          cwd=ROOT, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    first, body = out.read_text(encoding="utf-8").split("\n", 1)
    return first, json.loads(body)


def test_matrix_header_carries_its_provenance(matrix):
    """首行是出处行，sha 是 40 位十六进制（工作区脏时带 -dirty 后缀，见 DECISIONS）。"""
    first, _ = matrix
    assert HEADER.match(first), first


def test_matrix_covers_every_role_in_the_pool(matrix):
    """22 个在池角色一个不落，且每行都带 duty / model_tier。"""
    _, data = matrix
    roles = {r["role"]: r for r in data["roles"]}
    assert set(roles) == set(AGENT_POOL)
    assert len(roles) == len(AGENT_POOL)
    for role, row in roles.items():
        assert row["duty"] and row["model_tier"]
        assert set(row["assembly"]["resolved"]) <= set(
            t["name"] for t in row["allowed_tools"])


def test_matrix_marks_impl_status_for_every_name(matrix):
    """每个 skill / tool 名都要答「有没有实现」，且 sandbox 那两处如实记着。"""
    _, data = matrix
    roles = {r["role"]: r for r in data["roles"]}
    for row in roles.values():
        for tool in row["allowed_tools"]:
            assert isinstance(tool["implemented"], bool)
            assert tool["backing"] in ("local", "mcp", None)
        for skill in row["allowed_skills"]:
            assert isinstance(skill["implemented"], bool)

    assert roles["coding"]["assembly"]["authorized_without_impl"] == ["sandbox"]
    assert roles["testing"]["assembly"]["authorized_without_impl"] == ["sandbox"]


def test_checked_in_matrix_has_the_same_shape():
    """落库的那份正本也要过同样的判据 —— 首行有出处、22 个角色一个不落、无绝对路径。"""
    path = ROOT / "evidence" / "capability-matrix.json"
    if not path.exists():                    # 还没生成正本时不拦（本轨自己会生成）
        pytest.skip("evidence/capability-matrix.json 尚未生成")
    first, body = path.read_text(encoding="utf-8").split("\n", 1)
    assert HEADER.match(first), first
    data = json.loads(body)
    assert {r["role"] for r in data["roles"]} == set(AGENT_POOL)
    assert "/Users/" not in body


def test_matrix_leaks_no_absolute_paths(matrix):
    """铁律 6：证据里不许出现绝对路径（换台机器就不可比），更不许出现任何密钥。"""
    first, data = matrix
    raw = first + json.dumps(data, ensure_ascii=False)
    assert "/Users/" not in raw
    assert str(ROOT) not in raw
