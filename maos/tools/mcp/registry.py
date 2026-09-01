"""MCP server 注册表 —— 一份声明、一次发现、一道对账。

补的是一个**两张表分家**的洞。当前「一个 MCP server 能提供什么」写在
``server.py::TOOLS``（3 个工具 + inputSchema），而「MAOS 认它提供什么」写在
``git_tool.py``（``OPS`` 手写映射 + ``GIT_MCP_PORT`` 手抄的 params_schema /
returns_schema / failure_modes）。两边现在对得上，**但没有任何东西守着它继续
对得上** —— server 那边加一个工具、改一个必填字段，这三处一个都不会跟着变，
也没有一条测试会红。

本模块提供三件事，且**只有这三件**：

* ``SERVERS``            —— 静态声明：装配时「有哪些 server、边界是什么」的唯一出处
* ``discover(spec)``     —— 真拉起一次，问 server 自己暴露了什么（复用 ``client.py``）
* ``reconcile(spec)``    —— 把声明与发现结果对账，差异报成 ``Finding``

刻意不做的事（``maos/tools/mcp/__init__.py`` 文件头那三条边界的直接推论）：

1. **注册表里不登记接管沙箱的 server**（边界一）。``sandbox.git_apply`` /
   ``sandbox.pytest_run`` 的容器隔离论证独立成立，换 MCP 传输要重新论证等价性。
2. **``readonly`` 默认 True**（边界二）。写操作的安全边界归沙箱；登记一个
   ``readonly=False`` 的 server 需要显式论证，且 ``test_mcp_registry.py`` 有一道
   机器闸守着当前这份注册表全只读。
3. **这不是喂给模型的 tool schema**（边界三）。``SERVERS`` 是**给装配用的静态
   声明**，供 ``ports_for()`` 按角色挑出已有的 ToolPort；让模型自己选工具要改
   ``ModelClient.complete`` 的冻结契约 A-12，不在此列。

另外一条边界，是本模块自己的：**不生成 ToolPort**。``ports_for()`` 返回的永远是
``git_tool.py`` 里那个人写的 ``GIT_MCP_PORT`` 对象本身。九要素里的
``failure_modes`` / ``security_boundary`` 是评审会逐条对的东西，机器编不出来 ——
编出来的是假货，比没有更坏。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from maos.tools.mcp.client import StdioMcpClient
from maos.tools.mcp.git_tool import FIXTURE_ROOT, GIT_MCP_PORT, _abs_root
from maos.tools.port import ToolPort

__all__ = [
    "McpServerSpec",
    "Finding",
    "SERVERS",
    "KNOWN_PORTS",
    "DEFAULT_ROLE_SERVERS",
    "discover",
    "reconcile",
    "reconcile_all",
    "ports_for",
]


# ---------------------------------------------------------------------------
# 声明
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class McpServerSpec:
    """一个 MCP server 的静态规格。frozen —— 注册表是声明，不是可变状态。

    ``root`` 一律写**仓库相对路径**，理由抄自 ``git_tool.py`` 的 ``FIXTURE_ROOT``：
    ``invoke_tool`` 会把 params 的 sha256 摘要写进 ``ToolInvoked`` 审计行，传绝对
    路径的话同一次调用在两台机器上摘要不同，而且 ``/Users/<某人>/...`` 会原样落进
    证据束 —— 既不可比，也没必要。解析在 ``_abs_root()`` 里按仓库根做，不按 CWD。

    ``exposes`` 是**对账基准，不是发现结果**：它记的是「我们认为这个 server 该暴露
    什么」，``reconcile()`` 拿它去和 server 真实的 ``tools/list`` 比。两者相等是
    正常态，不等就是有人单边改了却没同步 —— 那正是本模块要抓的东西。
    """

    name: str                                   # 注册表主键
    module: str                                 # 拉起用的 python -m 目标
    root: str                                   # 路径关押边界（仓库相对路径）
    readonly: bool = True                       # 边界二：默认只读，False 需显式论证
    exposes: tuple[str, ...] = ()               # 期望暴露的工具名（对账基准）
    ports: tuple[str, ...] = ()                 # 这个 server 背书的**已有** ToolPort 名
    security_boundary: str = ""                 # 九要素同名项，登记时就要写清
    owner: str = ""

    def argv(self) -> list[str]:
        """拉起子进程的命令行。``module`` 是载荷字段，不是装饰字段 —— 走这条路出去。"""
        return [sys.executable, "-m", self.module, "--root", _abs_root(self.root)]


@dataclass(frozen=True)
class Finding:
    """一条对账差异。``kind`` 只有三种，多一种就说明对账口径又长胖了。"""

    kind: str                                   # undeclared / missing / schema-drift
    server: str
    subject: str                                # 工具名，或 "<工具名>.<参数名>"
    detail: str

    def __str__(self) -> str:                   # 验收命令要直接 print，给个人读的形状
        return f"[{self.kind}] {self.server}::{self.subject} —— {self.detail}"


#: 已有的 ToolPort 对象登记处。**本模块不造 ToolPort**，只是把人写好的指过来。
KNOWN_PORTS: dict[str, ToolPort] = {
    GIT_MCP_PORT.name: GIT_MCP_PORT,
}


#: 全仓 MCP server 注册表。**当前只有一个，就只登记一个。**
#: 不为了「展示扩展性」编第二条进来 —— 注册表里躺着一个拉不起来的条目，
#: 比没有注册表更坏：它会让第一个信它的人白跑一趟，还查不出是声明假的。
SERVERS: dict[str, McpServerSpec] = {
    "git-mcp-server": McpServerSpec(
        name="git-mcp-server",
        module="maos.tools.mcp.server",
        root=FIXTURE_ROOT,
        readonly=True,
        exposes=("git_baseline", "git_ls_files", "git_show_file"),
        ports=(GIT_MCP_PORT.name,),
        security_boundary=(
            "① 全部工具只读：不 commit / 不 apply / 不 checkout，写操作归沙箱，"
            "两处都能改仓库会让「谁改的」失去唯一答案；"
            "② 路径按 --root 关押，show_file 的 path 先 resolve 再用 Path.relative_to 判定"
            "（不用 startswith，后者会把 /w-evil 判成 /w 的子路径）；"
            "③ 不打网络：只 fork git 子进程跑本地查询子命令，不跑 fetch/push/clone；"
            "④ 子进程 env 按白名单重建，只放行 PATH/LANG + 自算的 PYTHONPATH，"
            "按名放行而非按名拦截，新增 *_TOKEN 变量不需要有人记得来加拦截；"
            "⑤ 单帧上限 64KiB，超出显式标 truncated —— 静默截断等于伪造文件内容"
        ),
        owner="task-mcp",
    ),
}


#: 角色 -> server 名。**兜底用的最小映射**，不是权威档案表。
#:
#: 权威的能力档案在别处（按职责决定哪个角色挂哪个 server）；``ports_for()`` 接受
#: ``profiles`` 注入参数，注入了就以注入的为准。这里只保证「没人注入时也能跑」，
#: 且口径与 ``maos/agents/coding.py`` 的 ``allowed_tools``（含 ``git-mcp``）对得上。
DEFAULT_ROLE_SERVERS: dict[str, tuple[str, ...]] = {
    "coding": ("git-mcp-server",),
}


# ---------------------------------------------------------------------------
# 发现
# ---------------------------------------------------------------------------

def discover(spec: McpServerSpec, *, timeout: float | None = None) -> list[dict[str, Any]]:
    """真拉起一次 server，返回它自己报的 ``tools/list``。

    传输**一律复用** ``client.py::StdioMcpClient``：超时、杀进程、stderr 尾三行、
    握手版本对不上就停，那几条已经论证过并跑通了，再写一遍只会多出一份会漂的实现。
    ``with`` 退出即收尸，不留孤儿 server。
    """
    with StdioMcpClient(_abs_root(spec.root), timeout=timeout, argv=spec.argv()) as client:
        return client.list_tools()


# ---------------------------------------------------------------------------
# 对账
# ---------------------------------------------------------------------------

def _required_params(tool: dict[str, Any]) -> list[str]:
    schema = tool.get("inputSchema") or {}
    return [str(p) for p in (schema.get("required") or [])]


def reconcile(spec: McpServerSpec, *, timeout: float | None = None,
              tools: Sequence[dict[str, Any]] | None = None) -> list[Finding]:
    """把 ``discover()`` 的结果与 ``spec.exposes``、以及已注册的 ToolPort 对账。

    三类差异：

    * ``undeclared``   —— server 暴露了，但 ``spec.exposes`` 没登记
    * ``missing``      —— ``spec.exposes`` 登记了，但 server 没暴露
    * ``schema-drift`` —— server 某个工具的**必填**参数，在 ToolPort 的
      ``params_schema`` 里找不到对应的键

    第三类**刻意判得宽**：``params_schema`` 现在是自然语言描述
    （``"op": "str（baseline / ls_files / show_file）"``），做**键名级**对账即可，
    不去解析中文描述。理由：解析描述会让「改一个字就红」，而那个字不影响调用点；
    键名少一个才是真的调不通。判宽的代价是漏报参数语义漂移，那一类归人工评审。

    ``tools`` 可以直接喂进来（测试注入用），缺省则真跑一次 ``discover()``。
    """
    found = list(tools) if tools is not None else discover(spec, timeout=timeout)
    found_names = {str(t.get("name") or "") for t in found}
    declared = set(spec.exposes)

    findings: list[Finding] = []

    for name in sorted(found_names - declared):
        findings.append(Finding(
            kind="undeclared", server=spec.name, subject=name,
            detail=f"server 暴露了 {name}，但 {spec.name} 的 exposes 没登记它",
        ))

    for name in sorted(declared - found_names):
        findings.append(Finding(
            kind="missing", server=spec.name, subject=name,
            detail=f"{spec.name} 的 exposes 登记了 {name}，但 server 的 tools/list 里没有",
        ))

    # 只拿 spec 自己背书的 ToolPort 对账 —— 别的 port 与这个 server 无关。
    port_keys: set[str] = set()
    resolved: list[str] = []
    for port_name in spec.ports:
        port = KNOWN_PORTS.get(port_name)
        if port is None:
            findings.append(Finding(
                kind="missing", server=spec.name, subject=port_name,
                detail=f"{spec.name} 声称背书 ToolPort {port_name}，但 KNOWN_PORTS 里没有",
            ))
            continue
        resolved.append(port_name)
        port_keys |= set(port.params_schema)

    # 一个 port 都没解析出来时不跑参数对账：那会把「port 找不到」这一条放大成
    # 每个必填参数一条 schema-drift，真正的那条 missing 反而淹在噪声里。
    if resolved:
        for tool in sorted(found, key=lambda t: str(t.get("name") or "")):
            tool_name = str(tool.get("name") or "")
            for param in _required_params(tool):
                if param not in port_keys:
                    findings.append(Finding(
                        kind="schema-drift", server=spec.name,
                        subject=f"{tool_name}.{param}",
                        detail=(f"server 要求必填参数 {param}，但背书的 ToolPort "
                                f"{resolved} 的 params_schema 里没有这个键"),
                    ))

    return findings


def reconcile_all(*, timeout: float | None = None) -> dict[str, list[Finding]]:
    """对整张注册表跑一遍对账。返回 server 名 -> findings（零 finding 也留空列表）。"""
    return {name: reconcile(spec, timeout=timeout) for name, spec in SERVERS.items()}


# ---------------------------------------------------------------------------
# 按角色挂载
# ---------------------------------------------------------------------------

def ports_for(role: str, *,
              profiles: Mapping[str, Sequence[str]] | None = None) -> list[ToolPort]:
    """按角色挑出该挂的 ToolPort。**挑，不造。**

    返回的是 ``KNOWN_PORTS`` 里那些人写好的 ToolPort 对象本身，不是新生成的：
    九要素里的 ``failure_modes`` / ``security_boundary`` 机器编不出来。

    ``profiles`` 是注入参数（角色 -> server 名序列），缺省走
    ``DEFAULT_ROLE_SERVERS`` 兜底。**本模块不 import 任何能力档案模块** ——
    档案表是别处的产出，注册表不该反过来依赖它；注入让两边可以各自演进。

    角色不在映射里就返回空列表，不抛异常：「这个角色没有 MCP 能力」是正常态
    （银行角色本来就不该有 git 能力），不是错误。
    """
    table: Mapping[str, Sequence[str]] = (
        profiles if profiles is not None else DEFAULT_ROLE_SERVERS
    )
    ports: list[ToolPort] = []
    seen: set[str] = set()
    for server_name in table.get(role, ()) or ():
        spec = SERVERS.get(server_name)
        if spec is None:
            raise KeyError(
                f"角色 {role} 挂了未注册的 MCP server: {server_name}"
                f"（已注册: {sorted(SERVERS)}）"
            )
        for port_name in spec.ports:
            port = KNOWN_PORTS.get(port_name)
            if port is None or port.name in seen:
                continue
            seen.add(port.name)
            ports.append(port)
    return ports
