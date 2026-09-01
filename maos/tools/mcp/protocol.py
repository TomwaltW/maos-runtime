"""MCP over stdio 的线格式 —— 客户端与服务端共用这一份，避免两边各写一套。

为什么手写而不是装官方 ``mcp`` SDK：``pyproject.toml`` 的 ``dependencies = []``
是被当契约维护的（核心零依赖），官方 SDK 会拉进 pydantic / httpx / anyio 一整串。
同样的取舍在 ``maos/model/client.py`` 已经做过一次（OpenAI 兼容协议用 urllib 手写）。

传输层就是 JSON-RPC 2.0 + 换行分帧：一条消息一行 JSON，行内不许出现裸换行。
这不是简化版协议，是 MCP stdio transport 本来的样子。
"""

from __future__ import annotations

import json
from typing import Any

#: 对齐的 MCP 修订号。写死一个具体日期而不是 "latest" —— 握手时两边要拿它比对，
#: 一个会随时间漂移的值会让「昨天跑通的握手今天为什么不通」无从查起。
PROTOCOL_VERSION = "2025-06-18"

JSONRPC = "2.0"

# JSON-RPC 2.0 标准错误码。不许自己发明取值，否则客户端没法按码分流。
E_PARSE = -32700
E_INVALID_REQUEST = -32600
E_METHOD_NOT_FOUND = -32601
E_INVALID_PARAMS = -32602
E_INTERNAL = -32603


class McpError(RuntimeError):
    """MCP 层的失败：连不上、握手不通、服务端回 error、超时。

    一律抛，**不返回退化结果**。工具连不上就该让 ``invoke_tool`` 落一条
    ``status=failed`` 的审计行并把异常抛给状态机；悄悄降级成本地函数，
    会让「这一步到底走没走 MCP」在证据里查不出来 —— 那比不接更坏。
    口径同 ``AlipaySandboxAdapter`` 抛 NotImplementedError 而非返假数据。
    """

    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


def encode(msg: dict[str, Any]) -> bytes:
    """一条消息 -> 一行 UTF-8 字节。

    ``ensure_ascii=False`` 与「行内不许有裸换行」并不冲突：``json.dumps`` 会把
    真换行转义掉，所以中文内容照样是安全的单行。
    """
    text = json.dumps(msg, ensure_ascii=False, separators=(",", ":"))
    return (text + "\n").encode("utf-8")


def decode(line: str | bytes) -> dict[str, Any]:
    if isinstance(line, bytes):
        line = line.decode("utf-8", errors="replace")
    line = line.strip()
    if not line:
        raise McpError("收到空行，不是合法 JSON-RPC 帧", code=E_PARSE)
    try:
        msg = json.loads(line)
    except json.JSONDecodeError as exc:
        raise McpError(f"JSON-RPC 帧解析失败: {exc}", code=E_PARSE) from None
    if not isinstance(msg, dict):
        raise McpError(f"JSON-RPC 帧应为对象，实际 {type(msg).__name__}",
                       code=E_INVALID_REQUEST)
    return msg


def request(req_id: int, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    msg: dict[str, Any] = {"jsonrpc": JSONRPC, "id": req_id, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def notification(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    msg: dict[str, Any] = {"jsonrpc": JSONRPC, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def result(req_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC, "id": req_id, "result": payload}


def error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC, "id": req_id, "error": {"code": code, "message": message}}
