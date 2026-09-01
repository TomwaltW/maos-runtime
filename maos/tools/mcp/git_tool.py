"""``git-mcp`` —— 全仓第一个 entry 不是进程内函数的 ToolPort。

它补的是一个**声明与实现分家**的洞：``git-mcp`` 这个名字此前已经出现在
``maos/agents/coding.py`` 的 ``allowed_tools``（并且 ``check_tool("git-mcp")``
真的在跑）、``maos/skills/builtin/code_repo_patch.py`` 的 ``depends_tools`` 里，
但全仓没有任何 ToolPort 叫这个名字 —— 白名单放行了一个不存在的东西。

九要素里换掉的只有 ③ ``entry``：它现在是一次 MCP stdio 往返（拉起 server ->
握手 -> tools/call -> 收尸），其余八项与本地工具一字不差。``invoke_tool`` 与
``ToolInvoked`` 审计行在调用点之上，不关心 entry 背后是本地函数还是一个 MCP
server，所以证据束里那条审计行的形状、``scripts/verify.py`` 的第 1 项校验、
Identity 的 ``allowed_tools`` 白名单，全部原样成立。

**这句话此前是 docs/toolport-contract.md 里的推论，现在有一条跑得出来的链路。**
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from maos.tools.mcp.client import _package_root, call_once
from maos.tools.port import ToolPort

#: op -> MCP 工具名。调用方给 op，不直接给 MCP 工具名 —— 传输层换掉时
#: （比如以后换成 HTTP transport）上层的调用点不用跟着改。
OPS = {
    "baseline": "git_baseline",
    "ls_files": "git_ls_files",
    "show_file": "git_show_file",
}

#: 演示靶场的**仓库相对**路径。与 ``maos.tools.sandbox.FIXTURE_REPO`` 是同一个目录，
#: 由 ``maos/tests/test_mcp_git_tool.py`` 守着不许漂。
#:
#: 为什么调用点传相对路径而不是绝对路径：``invoke_tool`` 会把 params 的 sha256
#: 摘要写进 ToolInvoked 审计行。传绝对路径的话，同一次调用在两台机器上摘要不同，
#: 而且 ``/Users/<某人>/...`` 会原样落进证据束 —— 那既不可比，也没必要。
FIXTURE_ROOT = "scenarios/fixture-repo"


def _abs_root(root: str) -> str:
    """相对路径按仓库根解析，不按 CWD —— 场景跑在哪个目录起的不该影响结果。"""
    p = Path(root)
    return str(p if p.is_absolute() else Path(_package_root()) / p)


def git_mcp(*, op: str, root: str, path: str = "", prefix: str = "") -> dict[str, Any]:
    """经 MCP 跑一次只读 git 查询。

    参数全部是 JSON 可序列化的标量 —— 这一条是刻意的：``invoke_tool`` 会对 params
    取 sha256 摘要落进审计行，params 里混一个活对象（``gateway.refund`` 那样把
    ``GatewayPort`` 实例当参数传）在跨进程之后就走不通了。
    """
    tool = OPS.get(op)
    if tool is None:
        raise ValueError(f"未知的 git-mcp 操作: {op}（可用: {'/'.join(sorted(OPS))}）")
    args: dict[str, Any] = {}
    if op == "show_file":
        args["path"] = path
    elif op == "ls_files" and prefix:
        args["prefix"] = prefix
    return call_once(_abs_root(root), tool, args)


GIT_MCP_PORT = ToolPort(
    name="git-mcp",
    purpose="经 MCP（stdio / JSON-RPC 2.0）做只读 git 查询：仓库基线、文件清单、单文件内容",
    entry=git_mcp,
    params_schema={
        "op": "str（baseline / ls_files / show_file）",
        "root": "str（仓库根，同时是路径关押边界；相对路径按仓库根解析，不按 CWD）",
        "path": "str（仅 show_file：相对 root 的路径）",
        "prefix": "str（仅 ls_files：路径前缀过滤，可空）",
    },
    returns_schema={
        "baseline": "{repo_root:str, repo_name:str, head:str, head_short:str, "
                    "branch:str, dirty:bool, dirty_count:int, tracked_count:int}",
        "ls_files": "{files:list[str], count:int}",
        "show_file": "{path:str, content:str, bytes:int, truncated:bool}",
    },
    failure_modes=[
        "McpError: 拉起 server 失败 / 对端提前退出（stderr 尾三行随异常一起带出）",
        "McpError: 握手协议版本不一致 —— 停，不猜，不按任一版继续跑",
        "McpError: 等待响应超时（MAOS_MCP_TIMEOUT，默认 15s），子进程已被杀掉，不留孤儿",
        "McpError: 工具级失败（root 不是 git 仓 / 文件不在 HEAD 里 / 路径越出 root）",
        "ValueError: op 不在 OPS 里 —— 调用点写错了，不是对端的问题",
        "以上全部原样抛出，不降级回本地 git：悄悄降级会让「这一步走没走 MCP」在证据里查不出来",
    ],
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
    rate_limit="",
    owner="task-mcp",
)
