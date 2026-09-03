"""MCP stdio 传输层的守卫 —— 协议、超时、路径关押。

这些测试守的是「跨进程」这件事本身多出来的那几类失败：进程起不来、对端提前死、
对端不回话、对端版本对不上、参数想读 root 之外的东西。进程内函数一个都没有，
所以一条都不能省。
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

import pytest

from maos.tools.mcp import protocol as P
from maos.tools.mcp.client import StdioMcpClient
from maos.tools.mcp.protocol import McpError
from maos.tools.mcp.server import TOOLS, handle, serve

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = REPO_ROOT / "scenarios" / "fixture-repo"


# ---------------------------------------------------------------------------
# 1. 线格式
# ---------------------------------------------------------------------------

def test_encode_is_exactly_one_line():
    """一条消息一行 —— 中文与内嵌换行都不许把帧撑成两行，否则对端会读到半条。"""
    raw = P.encode({"jsonrpc": "2.0", "id": 1, "text": "中文\n换行"})
    assert raw.endswith(b"\n")
    assert raw.count(b"\n") == 1


def test_decode_roundtrip_and_rejections():
    assert P.decode(P.encode({"a": 1}))["a"] == 1
    with pytest.raises(McpError):
        P.decode("")                       # 空行不是合法帧
    with pytest.raises(McpError):
        P.decode("[1,2]")                  # 顶层必须是对象
    with pytest.raises(McpError):
        P.decode("{不是 json}")


# ---------------------------------------------------------------------------
# 2. 服务端方法（直接驱动 handle，不起进程）
# ---------------------------------------------------------------------------

def test_initialize_returns_pinned_version():
    res = handle(P.request(1, "initialize", {}), FIXTURE)
    assert res["result"]["protocolVersion"] == P.PROTOCOL_VERSION
    assert res["result"]["serverInfo"]["name"] == "maos-git-mcp"


def test_tools_list_shape_is_real_json_schema():
    """inputSchema 必须是真 JSON Schema，不是给人读的字符串 —— MCP 的规定动作。"""
    res = handle(P.request(1, "tools/list"), FIXTURE)
    tools = res["result"]["tools"]
    assert [t["name"] for t in tools] == [t["name"] for t in TOOLS]
    for t in tools:
        assert t["description"]
        schema = t["inputSchema"]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False


def test_notification_gets_no_reply():
    """通知（无 id）处理完不回帧。回了的话客户端的下一次收发就会整体错位一格。"""
    assert handle(P.notification("notifications/initialized"), FIXTURE) is None


def test_unknown_method_is_protocol_error():
    res = handle(P.request(7, "tools/subscribe"), FIXTURE)
    assert res["error"]["code"] == P.E_METHOD_NOT_FOUND


def test_unknown_tool_is_invalid_params():
    res = handle(P.request(8, "tools/call", {"name": "rm_rf", "arguments": {}}), FIXTURE)
    assert res["error"]["code"] == P.E_INVALID_PARAMS


def test_tool_failure_is_iserror_not_protocol_error():
    """工具跑失败走 isError，协议接错才走 error —— 客户端靠这个分流。"""
    res = handle(P.request(9, "tools/call",
                           {"name": "git_show_file", "arguments": {"path": "不存在.py"}}),
                 FIXTURE)
    assert "error" not in res
    assert res["result"]["isError"] is True


# ---------------------------------------------------------------------------
# 3. 路径关押
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    "../../README.md",                     # 往上跑
    "auth/../../../etc/passwd",            # 绕一圈再往上跑
    "/etc/passwd",                         # 绝对路径
])
def test_path_jail_rejects_escapes(bad):
    res = handle(P.request(1, "tools/call",
                           {"name": "git_show_file", "arguments": {"path": bad}}), FIXTURE)
    assert res["result"]["isError"] is True
    assert "越出 root 边界" in res["result"]["content"][0]["text"]


def test_serve_survives_garbage_line():
    """收到一行垃圾要回 parse error 并继续服务，不许整条链路当场断掉。"""
    inp = io.StringIO("{坏帧}\n" + P.encode(P.request(1, "tools/list")).decode() + "\n")
    out = io.BytesIO()
    serve(FIXTURE, stdin=inp, stdout=out)
    lines = [ln for ln in out.getvalue().decode().splitlines() if ln.strip()]
    assert P.decode(lines[0])["error"]["code"] == P.E_PARSE
    assert P.decode(lines[1])["result"]["tools"]


# ---------------------------------------------------------------------------
# 4. 客户端：真起一个子进程走完全程
# ---------------------------------------------------------------------------

def test_client_end_to_end():
    with StdioMcpClient(FIXTURE) as client:
        assert client.server_info["name"] == "maos-git-mcp"
        assert {t["name"] for t in client.list_tools()} == {t["name"] for t in TOOLS}
        base = client.call_tool("git_baseline")
        assert len(base["head"]) == 40 and base["head_short"] == base["head"][:7]
        assert base["tracked_count"] > 0
        files = client.call_tool("git_ls_files")["files"]
        assert "auth/session.py" in files


def test_client_raises_on_tool_error_not_returns_it():
    """isError 必须抛。返回一个「看上去像结果」的字典，上层就会拿它继续算下去。"""
    with StdioMcpClient(FIXTURE) as client:
        with pytest.raises(McpError, match="越出 root 边界"):
            client.call_tool("git_show_file", {"path": "../../README.md"})


def _fake_server(tmp_path: Path, body: str) -> list[str]:
    script = tmp_path / "fake_server.py"
    script.write_text(body, encoding="utf-8")
    return [sys.executable, str(script)]


def test_version_mismatch_stops_the_handshake(tmp_path):
    """对端版本对不上就停，不猜 —— 一边单方面容忍就等于没有协议。"""
    body = (
        "import sys, json\n"
        "for line in sys.stdin:\n"
        "    msg = json.loads(line)\n"
        "    if msg.get('id') is None:\n"
        "        continue\n"
        "    sys.stdout.write(json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':{\n"
        "        'protocolVersion':'1999-01-01','capabilities':{},\n"
        "        'serverInfo':{'name':'fake','version':'0'}}})+'\\n')\n"
        "    sys.stdout.flush()\n"
    )
    with pytest.raises(McpError, match="协议版本不一致"):
        StdioMcpClient(tmp_path, argv=_fake_server(tmp_path, body)).start()


def test_timeout_raises_and_kills_the_child(tmp_path):
    """对端不回话：必须超时抛错**并且**把子进程杀掉，不许留孤儿。

    只抛不杀会攒下一堆挂着的 server，下一次跑的表现取决于上一次留了几个 ——
    这类不可复现的坑最难查，所以这里连 poll() 一起断言。
    """
    body = "import sys, time\nfor line in sys.stdin:\n    time.sleep(30)\n"
    client = StdioMcpClient(tmp_path, timeout=0.5, argv=_fake_server(tmp_path, body))
    with pytest.raises(McpError, match="超时"):
        client.start()
    assert client._proc is None, "超时后客户端还攥着子进程句柄"


def test_dead_server_is_reported_with_stderr(tmp_path):
    """对端提前死：报错里要带 stderr 尾巴，否则只剩一句「没响应」没法查。"""
    body = "import sys\nsys.stderr.write('起不来：缺依赖\\n')\nsys.exit(3)\n"
    client = StdioMcpClient(tmp_path, timeout=5, argv=_fake_server(tmp_path, body))
    with pytest.raises(McpError, match="提前退出"):
        client.start()


def test_child_env_is_whitelisted_not_blacklisted():
    """子进程 env 按名放行：新增一个 *_TOKEN 不需要有人记得来加拦截。"""
    from maos.tools.mcp.client import ENV_PASSTHROUGH, _child_env

    os.environ["MAOS_FAKE_SECRET_TOKEN"] = "s3cr3t"
    try:
        env = _child_env()
    finally:
        os.environ.pop("MAOS_FAKE_SECRET_TOKEN", None)
    assert "MAOS_FAKE_SECRET_TOKEN" not in env
    assert set(env) <= set(ENV_PASSTHROUGH) | {"PYTHONPATH"}
