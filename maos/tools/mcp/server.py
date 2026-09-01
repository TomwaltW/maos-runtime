"""最小 MCP server —— 通过 stdio 暴露**只读** git 查询。

跑法（客户端会自己拉起它，手工核验时也可以直接跑）::

    python3 -m maos.tools.mcp.server --root scenarios/fixture-repo

三个工具，全部只读：``git_baseline`` / ``git_ls_files`` / ``git_show_file``。

安全边界（这一节是评审会逐条对的东西，不是注释）：

1. **只读**。三个工具都不写仓库：不 commit、不 apply、不 checkout。
   写操作归沙箱（``sandbox.git_apply``，容器 ``--network none --read-only``）——
   两处都能改仓库，会让「这次改动是谁做的」失去唯一答案。
2. **路径关押**。``--root`` 是硬边界：``git_show_file`` 的 ``path`` 解析真实路径后
   必须仍在 root 之内，``..`` 与符号链接一律先 resolve 再比对，越界即 E_INVALID_PARAMS。
   判定用 ``Path.relative_to``，不用 ``startswith`` —— 后者会把 ``/w-evil``
   判成 ``/w`` 的子路径（同一个坑 ``code_repo_patch.py`` 的受保护目录清单踩过一次）。
3. **不打网络**。只 fork ``git`` 子进程，没有任何 socket；``git`` 本身也只跑本地
   查询子命令（rev-parse / status / ls-files / show），不跑 fetch / push / clone。
4. **不读环境变量**。除了 PATH 之外不依赖任何 env，也不回显 env —— 密钥不可能
   从这条链路漏进证据束。
5. **单帧上限**。``git_show_file`` 的返回被 ``MAX_FILE_BYTES`` 截断并显式标注
   ``truncated``；一条 JSON-RPC 帧就是一行，不设上限的话一个大文件能把管道撑爆。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Any

from maos.tools.mcp.protocol import (
    E_INTERNAL,
    E_INVALID_PARAMS,
    E_METHOD_NOT_FOUND,
    PROTOCOL_VERSION,
    decode,
    encode,
    error,
    result,
)

SERVER_NAME = "maos-git-mcp"
SERVER_VERSION = "0.1.0"

#: 单个文件回传上限。超过即截断并把 truncated 标出来 —— 静默截断等于伪造文件内容。
MAX_FILE_BYTES = 64 * 1024

#: git 子进程超时。服务端自己也要有超时：客户端那道超时管不到已经 fork 出去的 git。
GIT_TIMEOUT = 15


# ---------------------------------------------------------------------------
# 工具清单（tools/list 的返回体，inputSchema 是真 JSON Schema）
# ---------------------------------------------------------------------------
TOOLS: list[dict[str, Any]] = [
    {
        "name": "git_baseline",
        "description": "返回仓库基线：HEAD sha、分支、工作树是否干净、被跟踪文件数",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "git_ls_files",
        "description": "列出 root 下被 git 跟踪的文件（可按前缀过滤）",
        "inputSchema": {
            "type": "object",
            "properties": {"prefix": {"type": "string", "description": "路径前缀，缺省列全部"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "git_show_file",
        "description": "读取 HEAD 版本的单个文件内容（只读，超过 64KiB 截断）",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "相对 root 的路径"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    },
]


class ToolFailure(Exception):
    """工具级失败。转成 isError=True 的 tools/call 结果，不是 JSON-RPC 层的 error。

    这个区分是 MCP 的规定动作：协议层错（方法不存在、参数非法）走 error，
    工具自己跑失败（git 不是仓库、文件不存在）走 isError —— 客户端据此
    分得清「协议接错了」和「工具告诉我这件事做不到」。
    """


# ---------------------------------------------------------------------------
# git 调用
# ---------------------------------------------------------------------------
def _git(root: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, timeout=GIT_TIMEOUT,
        )
    except FileNotFoundError:
        raise ToolFailure("找不到 git 可执行文件") from None
    except subprocess.TimeoutExpired:
        raise ToolFailure(f"git {' '.join(args)} 超时（>{GIT_TIMEOUT}s）") from None
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise ToolFailure(f"git {' '.join(args)} 退出码 {proc.returncode}: "
                          f"{detail[0] if detail else '无输出'}")
    return proc.stdout


def _resolve_in_root(root: Path, raw: str) -> Path:
    """把相对路径解析成真实路径，并确认它没跑出 root。越界即抛。"""
    if not isinstance(raw, str) or not raw:
        raise ToolFailure("path 必须是非空字符串")
    candidate = (root / raw).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise ToolFailure(f"路径越出 root 边界: {raw}") from None
    return candidate


# ---------------------------------------------------------------------------
# 三个工具
# ---------------------------------------------------------------------------
def tool_git_baseline(root: Path, _args: dict) -> dict[str, Any]:
    top = _git(root, "rev-parse", "--show-toplevel").strip()
    head = _git(root, "rev-parse", "HEAD").strip()
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD").strip()
    # --porcelain 只看 root 之内：靶场目录嵌在大仓里时，不该把整仓的脏文件算进来。
    dirty_lines = [ln for ln in _git(root, "status", "--porcelain", "--", ".").splitlines() if ln.strip()]
    tracked = [ln for ln in _git(root, "ls-files").splitlines() if ln.strip()]
    return {
        "repo_root": top,
        "repo_name": Path(top).name if top else "",
        "head": head,
        "head_short": head[:7],
        "branch": branch,
        "dirty": bool(dirty_lines),
        "dirty_count": len(dirty_lines),
        "tracked_count": len(tracked),
    }


def tool_git_ls_files(root: Path, args: dict) -> dict[str, Any]:
    prefix = args.get("prefix") or ""
    if not isinstance(prefix, str):
        raise ToolFailure("prefix 必须是字符串")
    files = [ln for ln in _git(root, "ls-files").splitlines() if ln.strip()]
    if prefix:
        files = [f for f in files if f.startswith(prefix)]
    return {"files": files, "count": len(files)}


def tool_git_show_file(root: Path, args: dict) -> dict[str, Any]:
    rel = args.get("path")
    _resolve_in_root(root, rel if isinstance(rel, str) else "")   # 边界判定，返回值不用
    raw = _git(root, "show", f"HEAD:./{rel}")
    encoded = raw.encode("utf-8")
    truncated = len(encoded) > MAX_FILE_BYTES
    if truncated:
        raw = encoded[:MAX_FILE_BYTES].decode("utf-8", errors="ignore")
    return {"path": rel, "content": raw, "bytes": len(encoded), "truncated": truncated}


DISPATCH = {
    "git_baseline": tool_git_baseline,
    "git_ls_files": tool_git_ls_files,
    "git_show_file": tool_git_show_file,
}


# ---------------------------------------------------------------------------
# JSON-RPC 方法
# ---------------------------------------------------------------------------
def handle(msg: dict[str, Any], root: Path) -> dict[str, Any] | None:
    """返回要回给客户端的一帧；通知（无 id）返回 None。"""
    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params") or {}

    if req_id is None:                      # 通知：处理完不回帧（JSON-RPC 规定）
        return None

    if method == "initialize":
        return result(req_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })

    if method == "tools/list":
        return result(req_id, {"tools": TOOLS})

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return error(req_id, E_INVALID_PARAMS, "arguments 必须是对象")
        fn = DISPATCH.get(name)
        if fn is None:
            return error(req_id, E_INVALID_PARAMS, f"未知工具: {name}")
        try:
            payload = fn(root, args)
        except ToolFailure as exc:
            # 工具失败走 isError，不走 JSON-RPC error —— 见 ToolFailure 的 docstring。
            return result(req_id, {"content": [{"type": "text", "text": str(exc)}],
                                   "isError": True})
        except Exception as exc:            # noqa: BLE001 —— 兜底不能让 server 静默退出
            return error(req_id, E_INTERNAL, f"{type(exc).__name__}: {exc}")
        return result(req_id, {
            "content": [{"type": "text", "text": _as_text(payload)}],
            "structuredContent": payload,
            "isError": False,
        })

    return error(req_id, E_METHOD_NOT_FOUND, f"未实现的方法: {method}")


def _as_text(payload: dict[str, Any]) -> str:
    import json
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def serve(root: Path, stdin=None, stdout=None) -> int:
    """读一行处理一行，直到 EOF。参数化 stdin/stdout 是为了测试能直接驱动它。"""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout.buffer
    for line in stdin:
        if not line.strip():
            continue
        try:
            msg = decode(line)
        except Exception as exc:            # noqa: BLE001
            stdout.write(encode(error(None, -32700, str(exc))))
            stdout.flush()
            continue
        reply = handle(msg, root)
        if reply is not None:
            stdout.write(encode(reply))
            stdout.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="MAOS 只读 git MCP server（stdio）")
    ap.add_argument("--root", required=True, help="仓库根；所有路径都关押在它之内")
    ns = ap.parse_args(argv)
    root = Path(ns.root).resolve()
    if not root.is_dir():
        print(f"--root 不是目录: {root}", file=sys.stderr)
        return 2
    return serve(root)


if __name__ == "__main__":                  # pragma: no cover —— 由 client 拉起
    raise SystemExit(main())
