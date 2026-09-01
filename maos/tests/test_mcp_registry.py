"""MCP server 注册表的守卫 —— 声明、发现、对账三者不许再分家。

这一份守的不是传输（``test_mcp_transport.py``），也不是单个 ToolPort 的九要素
（``test_mcp_git_tool.py``），而是**注册表这层新加的那一道对账**：

1. 注册表里每条 spec 都只读 —— ``maos/tools/mcp/__init__.py`` 边界二的机器闸；
2. ``discover()`` 真拉起一次 server，问它自己暴露了什么（不看任何手抄的表）；
3. ``reconcile()`` 在**当前仓库状态下零 finding** —— 这是「现在对得上」的钉子；
4. 注入一个假 spec，``reconcile()`` **必须报得出来** —— 这一条才是注册表真正
   买的东西。只有第 3 条的话，一个永远返回空列表的函数也能通过。

真拉子进程的两条硬要求（照 ``client.py`` 已有的姿势）：
不打网络（``security_boundary`` 第 ③ 条，本来就只 fork 本地 git 子命令），
不留孤儿（``StdioMcpClient`` 是 context manager，退出即收尸，``discover()`` 走的就是它）。
"""

from __future__ import annotations

import dataclasses

import pytest

from maos.tools.mcp.git_tool import GIT_MCP_PORT
from maos.tools.mcp.registry import (
    DEFAULT_ROLE_SERVERS,
    KNOWN_PORTS,
    SERVERS,
    Finding,
    McpServerSpec,
    discover,
    ports_for,
    reconcile,
    reconcile_all,
)
from maos.tools.port import ToolPort

GIT_SPEC = SERVERS["git-mcp-server"]
EXPECTED_TOOLS = ("git_baseline", "git_ls_files", "git_show_file")


@pytest.fixture(scope="module")
def discovered() -> list[dict]:
    """真拉起一次 server，整个模块共用这一次握手 —— 每条断言各拉一次纯属浪费。"""
    return discover(GIT_SPEC)


# ---------------------------------------------------------------------------
# 1. 声明面
# ---------------------------------------------------------------------------

def test_spec_is_frozen():
    """注册表是声明，不是可变状态：运行期被改一个字段，装配就再也说不清按哪份跑的。"""
    assert McpServerSpec.__dataclass_params__.frozen is True
    with pytest.raises(dataclasses.FrozenInstanceError):
        GIT_SPEC.readonly = False           # type: ignore[misc]


def test_every_registered_server_is_readonly():
    """边界二的机器闸：写操作的安全边界归沙箱，注册表里不许躺着一个能写仓库的 server。"""
    assert SERVERS, "注册表空了 —— 至少该有 git-mcp-server"
    for name, spec in SERVERS.items():
        assert spec.readonly is True, f"{name} 不是只读，破了 mcp/__init__.py 边界二"


def test_readonly_defaults_to_true():
    """默认值本身也要守：``readonly=False`` 必须是有人显式写下的，不能靠忘了填拿到。"""
    field = {f.name: f for f in dataclasses.fields(McpServerSpec)}["readonly"]
    assert field.default is True


def test_root_is_repo_relative():
    """绝对路径会让 ToolInvoked 的 params_digest 跨机器不同，还把 /Users/<某人> 落进证据束。"""
    for name, spec in SERVERS.items():
        assert not spec.root.startswith("/"), f"{name} 的 root 是绝对路径: {spec.root}"


def test_spec_security_boundary_covers_the_same_five_items():
    """spec 的 ⑤ 条边界必须与 GIT_MCP_PORT.security_boundary 一条不落地对上。"""
    for mark in ("①", "②", "③", "④", "⑤"):
        assert mark in GIT_SPEC.security_boundary, f"spec 的安全边界缺第 {mark} 条"
    for keyword in ("只读", "关押", "不打网络", "白名单", "64KiB"):
        assert keyword in GIT_SPEC.security_boundary, f"spec 的安全边界没点到：{keyword}"
        assert keyword in GIT_MCP_PORT.security_boundary
    assert GIT_SPEC.security_boundary == GIT_MCP_PORT.security_boundary, (
        "server 规格与 ToolPort 的安全边界又分家了 —— 这正是本模块要消灭的那种分家"
    )


def test_registry_declares_only_servers_that_exist():
    """不许为了展示扩展性编一个拉不起来的条目：它会让第一个信它的人白跑一趟。"""
    assert set(SERVERS) == {"git-mcp-server"}, "全仓当前只有一个 MCP server"
    for spec in SERVERS.values():
        for port_name in spec.ports:
            assert port_name in KNOWN_PORTS, f"背书了不存在的 ToolPort: {port_name}"


def test_known_ports_are_the_existing_objects_not_generated_ones():
    """本模块不造 ToolPort：failure_modes / security_boundary 机器编不出来。"""
    assert KNOWN_PORTS["git-mcp"] is GIT_MCP_PORT


# ---------------------------------------------------------------------------
# 2. 发现：真拉起一次
# ---------------------------------------------------------------------------

def test_discover_returns_the_three_readonly_tools(discovered):
    assert [t["name"] for t in discovered] == list(EXPECTED_TOOLS)
    assert len(discovered) == 3
    for tool in discovered:
        assert tool["inputSchema"]["type"] == "object"


def test_discover_goes_through_the_spec_module(discovered):
    """``module`` 是载荷字段：argv 真的由它拼出来，改错了就拉不起来，不是装饰。"""
    argv = GIT_SPEC.argv()
    assert argv[1:3] == ["-m", "maos.tools.mcp.server"]
    assert argv[3] == "--root"
    assert argv[4].endswith("scenarios/fixture-repo")


def test_discovered_names_match_the_declared_exposes(discovered):
    """『server 说的』与『我们登记的』当前对得上 —— 这是本轨钉住的那个现状。"""
    assert {t["name"] for t in discovered} == set(GIT_SPEC.exposes)


# ---------------------------------------------------------------------------
# 3. 对账：现状零 finding
# ---------------------------------------------------------------------------

def test_reconcile_is_clean_for_the_real_server():
    findings = reconcile(GIT_SPEC)
    assert findings == [], [str(f) for f in findings]


def test_reconcile_all_is_clean():
    assert reconcile_all() == {"git-mcp-server": []}


# ---------------------------------------------------------------------------
# 4. 对账：注入假 spec，必须报得出来
#    （只有第 3 节的话，一个永远返回空列表的函数也能通过）
# ---------------------------------------------------------------------------

def _fake(**overrides) -> McpServerSpec:
    return dataclasses.replace(GIT_SPEC, **overrides)


def test_reconcile_reports_missing_when_spec_declares_a_ghost_tool(discovered):
    """spec 多登记一个 server 根本没有的工具 -> missing。"""
    spec = _fake(name="fake-missing", exposes=GIT_SPEC.exposes + ("git_push_hard",))
    findings = reconcile(spec, tools=discovered)
    assert [(f.kind, f.subject) for f in findings] == [("missing", "git_push_hard")]
    assert "tools/list 里没有" in findings[0].detail


def test_reconcile_reports_undeclared_when_spec_forgets_a_tool(discovered):
    """spec 少登记一个 server 真在暴露的工具 -> undeclared。"""
    spec = _fake(name="fake-undeclared", exposes=("git_baseline", "git_ls_files"))
    findings = reconcile(spec, tools=discovered)
    assert [(f.kind, f.subject) for f in findings] == [("undeclared", "git_show_file")]
    assert "exposes 没登记" in findings[0].detail


def test_reconcile_reports_both_directions_at_once(discovered):
    """一次报全，不是发现第一条就返回 —— 半张差异清单比没有清单更误导。"""
    spec = _fake(name="fake-both", exposes=("git_baseline", "git_stash"))
    kinds = sorted((f.kind, f.subject) for f in reconcile(spec, tools=discovered))
    assert kinds == [
        ("missing", "git_stash"),
        ("undeclared", "git_ls_files"),
        ("undeclared", "git_show_file"),
    ]


def test_reconcile_reports_schema_drift_on_a_new_required_param():
    """server 给某个工具加了必填参数，而 ToolPort 的 params_schema 没这个键 -> schema-drift。

    这是三处分家里最难靠肉眼发现的一处：名字全对得上，调用点却会 TypeError。
    """
    drifted = [
        {"name": "git_baseline", "inputSchema": {"type": "object", "properties": {}}},
        {"name": "git_ls_files", "inputSchema": {"type": "object", "properties": {}}},
        {"name": "git_show_file", "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "revision": {"type": "string"}},
            "required": ["path", "revision"],
        }},
    ]
    findings = reconcile(GIT_SPEC, tools=drifted)
    assert [(f.kind, f.subject) for f in findings] == [
        ("schema-drift", "git_show_file.revision"),
    ]
    assert "params_schema 里没有这个键" in findings[0].detail


def test_reconcile_is_key_level_not_description_level(discovered):
    """判宽的那一刀：params_schema 的中文描述改了字，不该红 —— 那个字不影响调用点。"""
    port = ToolPort(
        name="git-mcp", purpose="x", entry=lambda **_: None,
        params_schema={"op": "随便改成别的说法", "root": "…", "path": "…", "prefix": "…"},
    )
    findings = reconcile(GIT_SPEC, tools=discovered)
    assert findings == []
    assert set(port.params_schema) == set(GIT_MCP_PORT.params_schema)


def test_reconcile_flags_a_spec_backing_an_unknown_port(discovered):
    spec = _fake(name="fake-port", ports=("no-such-port",))
    findings = reconcile(spec, tools=discovered)
    assert [(f.kind, f.subject) for f in findings] == [("missing", "no-such-port")]


def test_finding_str_is_human_readable():
    text = str(Finding("missing", "s", "t", "d"))
    assert "[missing]" in text and "s::t" in text and "d" in text


# ---------------------------------------------------------------------------
# 5. 按角色挂载
# ---------------------------------------------------------------------------

def test_ports_for_coding_gets_git_mcp():
    ports = ports_for("coding")
    assert [p.name for p in ports] == ["git-mcp"]
    assert ports[0] is GIT_MCP_PORT, "必须是已有的那个对象，不是新造的九要素"


def test_ports_for_a_banking_role_gets_nothing():
    """银行角色不该有 git 能力。没映射到就是空列表，不是异常 —— 那是正常态。"""
    assert ports_for("ap_treasury") == []
    assert ports_for("manager") == []


def test_ports_for_takes_injected_profiles():
    """档案表是别处的产出，注册表不反过来依赖它 —— 注入的以注入为准。"""
    assert ports_for("ap_treasury", profiles={"ap_treasury": ("git-mcp-server",)}) == [
        GIT_MCP_PORT
    ]
    assert ports_for("coding", profiles={}) == []


def test_ports_for_rejects_an_unregistered_server():
    """注入表里写了个没注册的 server，要当场炸，不是悄悄返回空 —— 静默漏挂最难查。"""
    with pytest.raises(KeyError, match="未注册"):
        ports_for("coding", profiles={"coding": ("ghost-mcp-server",)})


def test_default_role_map_agrees_with_the_coding_whitelist():
    """兜底映射的口径必须与 coding 的 allowed_tools 对得上，否则装配时白名单会拦下自己挂的工具。"""
    from maos.agents.coding import CodingAgent

    allowed = CodingAgent.identity.allowed_tools
    for port in ports_for("coding"):
        assert port.name in allowed, f"挂了但白名单不放行: {port.name}"
    assert "coding" in DEFAULT_ROLE_SERVERS
