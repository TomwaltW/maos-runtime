"""MCP stdio 客户端 —— 拉起一个 server 子进程，走完握手，调工具，收工。

零第三方依赖：subprocess + threading + queue，全是标准库。

三条不许省的：

1. **超时是必须的。** ``invoke_tool`` 那一层没有超时（``maos/tools/port.py``），
   进程内函数不需要，跨进程需要 —— 对端卡住而这边没有超时，整条 plan 驱动循环
   就跟着一起卡死，而且 event_log 里连一条 failed 都不会落。
2. **超时后必须杀进程。** 只抛异常不 kill 会攒下一堆孤儿 server，
   下一次 run 的表现取决于上一次留了几个 —— 这类不可复现的坑最难查。
3. **失败一律抛 McpError，不降级。** 连不上就是连不上，不许悄悄回落到本地函数。

env 按白名单重建（口径同 ``maos/tools/sandbox.py`` 的降级路径）：只放行
PATH / LANG，外加本进程自己算出来的 PYTHONPATH。**白名单是「按名放行」不是
「按名拦截」** —— 以后新增一个 ``*_TOKEN`` 变量，不需要有人记得来这里加拦截。
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from maos.tools.mcp.protocol import (
    PROTOCOL_VERSION,
    McpError,
    decode,
    encode,
    notification,
    request,
)

#: 单次请求的超时（秒）。可用 MAOS_MCP_TIMEOUT 覆盖。
ENV_TIMEOUT = "MAOS_MCP_TIMEOUT"
DEFAULT_TIMEOUT = 15.0

#: 子进程只继承这两个变量，其余一律不传（HOME 也不传：三个只读 git 子命令不需要
#: 全局 gitconfig，不传反而少一条能读到用户配置的路径）。
ENV_PASSTHROUGH = ("PATH", "LANG")


def _timeout_from_env(env: dict[str, str] | None = None) -> float:
    raw = (env or os.environ).get(ENV_TIMEOUT, "")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
    return value if value > 0 else DEFAULT_TIMEOUT


def _package_root() -> str:
    """``maos`` 包所在的父目录 —— 子进程靠它 import 得到 maos.tools.mcp.server。"""
    import maos
    return str(Path(maos.__file__).resolve().parent.parent)


def _child_env() -> dict[str, str]:
    env = {k: os.environ[k] for k in ENV_PASSTHROUGH if k in os.environ}
    root = _package_root()
    existing = os.environ.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{root}{os.pathsep}{existing}" if existing else root
    return env


class StdioMcpClient:
    """一次会话 = 一个 server 子进程。用 ``with`` 拉起，退出即收尸。

    刻意不做连接池：本仓当前只有一个工具、一次调用几十毫秒，池化换来的那点开销
    抵不上「谁持有那个长活进程、它什么时候死」多出来的一整类问题。
    """

    def __init__(self, root: str | os.PathLike, *, timeout: float | None = None,
                 argv: list[str] | None = None) -> None:
        self.root = str(Path(root).resolve())
        self.timeout = timeout if timeout is not None else _timeout_from_env()
        self._argv = argv or [sys.executable, "-m", "maos.tools.mcp.server",
                              "--root", self.root]
        self._proc: subprocess.Popen | None = None
        self._lines: queue.Queue = queue.Queue()
        self._stderr: list[str] = []
        self._next_id = 0
        self.server_info: dict[str, Any] = {}

    # -- 生命周期 ---------------------------------------------------------
    def __enter__(self) -> StdioMcpClient:
        self.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def start(self) -> None:
        try:
            self._proc = subprocess.Popen(
                self._argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=_child_env(), text=True, bufsize=1,
            )
        except OSError as exc:
            raise McpError(f"拉起 MCP server 失败: {exc}") from None
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()
        self._handshake()

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()               # 关 stdin = 让 server 读到 EOF 自己退
            proc.wait(timeout=3)
        except Exception:                        # noqa: BLE001 —— 收尸不许再抛
            proc.kill()
            try:
                proc.wait(timeout=3)
            except Exception:                    # noqa: BLE001
                pass

    # -- 管道 -------------------------------------------------------------
    def _pump_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            self._lines.put(line)
        self._lines.put(None)                    # None = 对端关了

    def _pump_stderr(self) -> None:
        """必须有人读 stderr：不读，对端写满管道缓冲区就会卡死，表现成「超时」。"""
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for line in proc.stderr:
            self._stderr.append(line.rstrip("\n"))

    def _stderr_tail(self) -> str:
        return " / ".join(self._stderr[-3:]) if self._stderr else "（stderr 为空）"

    # -- 收发 -------------------------------------------------------------
    def _send(self, msg: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise McpError("MCP server 未启动或已关闭")
        try:
            proc.stdin.write(encode(msg).decode("utf-8"))
            proc.stdin.flush()
        except (BrokenPipeError, ValueError) as exc:
            raise McpError(f"写入 MCP server 失败: {exc}；stderr={self._stderr_tail()}") from None

    def _recv(self) -> dict[str, Any]:
        try:
            line = self._lines.get(timeout=self.timeout)
        except queue.Empty:
            self._kill_now()
            raise McpError(f"等待 MCP server 响应超时（>{self.timeout}s），已杀掉子进程；"
                           f"stderr={self._stderr_tail()}") from None
        if line is None:
            raise McpError(f"MCP server 提前退出；stderr={self._stderr_tail()}")
        return decode(line)

    def _kill_now(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        proc.kill()
        try:
            proc.wait(timeout=3)
        except Exception:                        # noqa: BLE001
            pass

    def _rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next_id += 1
        req_id = self._next_id
        self._send(request(req_id, method, params))
        msg = self._recv()
        if msg.get("id") != req_id:
            raise McpError(f"响应 id 对不上：发的是 {req_id}，回的是 {msg.get('id')}")
        if "error" in msg:
            err = msg["error"] or {}
            raise McpError(f"MCP server 报错: {err.get('message')}", code=err.get("code"))
        payload = msg.get("result")
        if not isinstance(payload, dict):
            raise McpError(f"result 应为对象，实际 {type(payload).__name__}")
        return payload

    # -- 协议 -------------------------------------------------------------
    def _handshake(self) -> None:
        res = self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "maos", "version": "0.1.0"},
        })
        remote = res.get("protocolVersion")
        if remote != PROTOCOL_VERSION:
            # 版本对不上就停，不猜。协议是两边的共同约定，一边单方面容忍
            # 就等于没有约定 —— 出问题的时候没人说得清当时到底按哪一版跑的。
            raise McpError(f"MCP 协议版本不一致：本地 {PROTOCOL_VERSION}，对端 {remote}")
        self.server_info = res.get("serverInfo") or {}
        self._send(notification("notifications/initialized"))

    def list_tools(self) -> list[dict[str, Any]]:
        return list(self._rpc("tools/list").get("tools") or [])

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """调一个工具，返回结构化结果。

        ``isError=True`` 抛 McpError —— 工具说这件事做不到，调用方就该看见异常，
        而不是拿到一个「看上去像结果」的字典。
        """
        res = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        if res.get("isError"):
            text = ""
            for item in res.get("content") or []:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = str(item.get("text") or "")
                    break
            raise McpError(f"工具 {name} 失败: {text or '（无说明）'}")
        payload = res.get("structuredContent")
        if not isinstance(payload, dict):
            raise McpError(f"工具 {name} 没有返回 structuredContent")
        return payload


def call_once(root: str | os.PathLike, tool: str,
              arguments: dict[str, Any] | None = None, *,
              timeout: float | None = None) -> dict[str, Any]:
    """拉起 -> 握手 -> 调一次 -> 收尸。ToolPort 的 entry 走的就是这条路。"""
    with StdioMcpClient(root, timeout=timeout) as client:
        return client.call_tool(tool, arguments)
