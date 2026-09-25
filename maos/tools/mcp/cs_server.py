"""客服前台的只读 MCP 连接器（p14 · T179，方案 B）—— stdio JSON-RPC，形状照 ``server.py``。

跑法::

    python3 -m maos.tools.mcp.cs_server --db PATH [--tenant-map JSON] [--ledger PATH]

三个工具，全部只读（跨轨契约 review/p14-cs-contracts.md §2「T179」）：

* ``cs_order_status``   —— 先过 ``BindingVerifier.resolve``（这位客户能不能查这一单），不过就回
  ``{"outcome": "identity_unverified"}``、**不查单**；过了再用装配出的 ``OrderLookup`` 只读查一单，回
  ``{"outcome", "display_no", "wording"}``。wording 只取 ``ports.ORDER_STATUS_WORDING``。
* ``cs_refund_precheck`` —— 绑定 + 查单 ok 之后跑 ``RefundPrecheck``，回
  ``{"outcome", "ok", "decision", "rule_ref", "reason_code", "refused_why"}``。
* ``cs_handoff_list``   —— 最近的转人工卡片 ``{handoff_id, conversation_id, reason, delivery, created_at}``。

安全边界（评审逐条对）：

1. **只读**。唯一的写是查单端口本身经 ``invoke_tool`` 落的那一行 ToolInvoked 审计行
   （plan_id ``cs:mcp``、task_id ``mcp-<n>``、trace_id ``""``）。不写 cs_ 表、不建工单、不发命令。
2. **身份先于查单**。绑定只从内部路径写（种子文件、测试）；本连接器没有任何写绑定的入口。
3. **出参最小**。不回 query_key、金额、时间、平台原始状态、异常原文；退款预检**不回 command_line**
   （那一行只给内部同事）；转人工列表不回卡片正文、不回客户标识。订单号只回调用方自己给的
   display_no 原样。
4. **不打网络**（demo 装配）/ 凭据只读环境变量（电商适配器装配，由适配器在发请求那一刻读）；
   本模块一个取值都不打印，启动失败只报键名与类名。
5. **单帧上限** 64 KiB：超长的一行直接回 E_INVALID_REQUEST，不解析。
6. **没配查单 → system_misconfigured，不抛**；配坏了（``build_order_lookup_from_env`` 抛）也按没配算，
   只在 stderr 报类名 —— 服务照样起得来，``tools/list`` 照样能被注册表发现。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from maos.domain.cs import objects
from maos.domain.cs import ports as P
from maos.domain.cs.identity import BindingVerifier
from maos.domain.cs.types import CS_MCP_PLAN_ID
from maos.tools.mcp.protocol import (
    E_INTERNAL,
    E_INVALID_PARAMS,
    E_INVALID_REQUEST,
    E_METHOD_NOT_FOUND,
    E_PARSE,
    PROTOCOL_VERSION,
    decode,
    encode,
    error,
    result,
)

SERVER_NAME = "maos-cs-mcp"
SERVER_VERSION = "0.1.0"

#: 一行一帧的上限（字节）。与 server.py 的 64 KiB 同一个数。
MAX_FRAME_BYTES = 64 * 1024

#: 转人工列表的缺省条数与上限。
DEFAULT_HANDOFF_LIMIT = 20
MAX_HANDOFF_LIMIT = 100

#: 退款预检认的台账租户（与 scripts/run_ingress.py 同名同口径）。
ENV_CS_LEDGER_TENANT = "MAOS_CS_LEDGER_TENANT"

#: 缺省台账：与 maos.ingress.router.DEFAULT_LEDGER 同一个文件（不 import router：那一串太重）。
DEFAULT_LEDGER = Path(__file__).resolve().parents[3] / "scenarios" / "custom" / "ledger.json"

OUTCOME_IDENTITY_UNVERIFIED = "identity_unverified"

_ORDER_ARGS = {
    "tenant_id": {"type": "string"},
    "channel": {"type": "string"},
    "external_userid": {"type": "string"},
    "display_no": {"type": "string", "description": "客户自己给的订单号"},
}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "cs_order_status",
        "description": "身份核验通过后只读查一单，回对外措辞表里的那一句（不回金额 / 时间 / 查单键）",
        "inputSchema": {
            "type": "object",
            "properties": dict(_ORDER_ARGS, lang={"type": "string", "enum": list(P.LANGS)}),
            "required": ["tenant_id", "channel", "external_userid", "display_no"],
            "additionalProperties": False,
        },
    },
    {
        "name": "cs_refund_precheck",
        "description": "身份核验 + 查单 ok 之后跑一次只读退款预检（不回命令行、不建工单）",
        "inputSchema": {
            "type": "object",
            "properties": dict(_ORDER_ARGS, reason_text={"type": "string"}),
            "required": ["tenant_id", "channel", "external_userid", "display_no", "reason_text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "cs_handoff_list",
        "description": "最近的转人工卡片（只回 id、原因、投递状态、时刻；不回正文与客户标识）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tenant_id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_HANDOFF_LIMIT},
            },
            "required": ["tenant_id"],
            "additionalProperties": False,
        },
    },
]


class ToolFailure(Exception):
    """工具级失败（参数不对）→ isError=True 的 tools/call 结果；口径同 server.py。"""


class CsMcpContext:
    """一个服务进程的装配：库、身份核验、查单、预检、租户映射。"""

    def __init__(self, store: Any, *, lookup: Any = None, precheck: Any = None,
                 verifier: Any = None, tenant_map: Mapping[str, str] | None = None,
                 clock: Any = None) -> None:
        self.store = store
        self.lookup = lookup
        self.precheck = precheck
        self.verifier = verifier if verifier is not None else BindingVerifier()
        self.tenant_map = dict(tenant_map) if tenant_map is not None else None
        self._clock = clock

    def now(self) -> str:
        if self._clock is not None:
            return str(self._clock())
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def tenant(self, raw: str) -> str:
        """调用方给的租户 → 内部租户。给了映射就只认映射里的键（不认 → 空串，失败即关）。"""
        if self.tenant_map is None:
            return raw
        return str(self.tenant_map.get(raw, "") or "")

    def next_task_id(self) -> str:
        """``mcp-<n>``：本库里 cs:mcp 家族已有的不同 task_id 数 + 1（跨进程也递增）。"""
        rows = self.store.list_event_log(CS_MCP_PLAN_ID)
        return f"mcp-{len({r.get('task_id') for r in rows}) + 1}"


# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------
def _str_arg(args: dict, key: str, *, required: bool = True) -> str:
    value = args.get(key)
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise ToolFailure(f"{key} 必须是非空字符串")
    return value


def _check_keys(args: dict, allowed: set[str]) -> None:
    extra = sorted(set(args) - allowed)
    if extra:
        raise ToolFailure(f"不认识的参数：{', '.join(map(str, extra))}")


def _resolve(ctx: CsMcpContext, args: dict) -> Any:
    """绑定核验。返回 Binding 或 None（失败即关：核验器出错也按不过算）。"""
    tenant = ctx.tenant(args["tenant_id"])
    if not tenant:
        return None
    try:
        return ctx.verifier.resolve(ctx.store, tenant_id=tenant, channel=args["channel"],
                                    external_userid=args["external_userid"],
                                    display_no=args["display_no"])
    except Exception as exc:                            # noqa: BLE001 —— 失败即关
        print(f"cs_server: 身份核验出错 {type(exc).__name__}", file=sys.stderr)
        return None


def _lookup(ctx: CsMcpContext, binding: Any) -> P.LookupResult:
    try:
        res = ctx.lookup.lookup(ctx.store, binding, plan_id=CS_MCP_PLAN_ID,
                                task_id=ctx.next_task_id())
    except Exception as exc:                            # noqa: BLE001 —— 端口承诺不抛，再兜一层
        return P.LookupResult(outcome=P.LOOKUP_PLATFORM_ERROR, system_name="", query_key="",
                              error_kind=type(exc).__name__)
    if not isinstance(res, P.LookupResult) or res.outcome not in P.LOOKUP_OUTCOMES:
        return P.LookupResult(outcome=P.LOOKUP_PLATFORM_ERROR, system_name="", query_key="")
    return res


def _order_args(args: dict, extra: tuple[str, ...]) -> dict[str, str]:
    _check_keys(args, set(_ORDER_ARGS) | set(extra))
    return {k: _str_arg(args, k) for k in _ORDER_ARGS}


# ---------------------------------------------------------------------------
# 三个工具
# ---------------------------------------------------------------------------
def tool_cs_order_status(ctx: CsMcpContext, args: dict) -> dict[str, Any]:
    base = _order_args(args, ("lang",))
    lang = _str_arg(args, "lang", required=False) or P.LANG_ZH
    if lang not in P.LANGS:
        raise ToolFailure(f"lang 只认 {', '.join(P.LANGS)}")
    if ctx.lookup is None:
        return {"outcome": P.LOOKUP_MISCONFIGURED}
    binding = _resolve(ctx, base)
    if binding is None:
        return {"outcome": OUTCOME_IDENTITY_UNVERIFIED}
    res = _lookup(ctx, binding)
    wording = ""
    if res.outcome == P.LOOKUP_OK:
        wording = P.ORDER_STATUS_WORDING[lang].get(res.status, "")
    return {"outcome": res.outcome, "display_no": base["display_no"], "wording": wording}


def tool_cs_refund_precheck(ctx: CsMcpContext, args: dict) -> dict[str, Any]:
    base = _order_args(args, ("reason_text",))
    reason_text = args.get("reason_text")
    if not isinstance(reason_text, str):             # 可以是空串：预检会按 reason_missing 拒
        raise ToolFailure("reason_text 必须是字符串")
    if ctx.lookup is None or ctx.precheck is None:
        return {"outcome": P.LOOKUP_MISCONFIGURED}
    binding = _resolve(ctx, base)
    if binding is None:
        return {"outcome": OUTCOME_IDENTITY_UNVERIFIED}
    res = _lookup(ctx, binding)
    if res.outcome != P.LOOKUP_OK:
        return {"outcome": res.outcome}
    # 台账认的是台账单号：与前台 desk.py 同口径取 query_key（没有才回落 display_no）。
    # 这个号只进预检入参，不进出参。
    ledger_no = str(getattr(binding, "query_key", "") or "") or str(binding.display_no)
    try:
        pre = ctx.precheck.precheck(tenant_id=binding.tenant_id, order_no=ledger_no,
                                    reason_text=reason_text, now=ctx.now())
    except Exception as exc:                            # noqa: BLE001 —— 端口承诺不抛，再兜一层
        print(f"cs_server: 退款预检出错 {type(exc).__name__}", file=sys.stderr)
        pre = P.PrecheckResult(ok=False, refused_why="preflight_error")
    # 逐键挑，不整份转：PrecheckResult 里的 command_line / summary 一个字都不出这里。
    return {"outcome": P.LOOKUP_OK, "ok": bool(pre.ok), "decision": str(pre.decision),
            "rule_ref": str(pre.rule_ref), "reason_code": str(pre.reason_code),
            "refused_why": str(pre.refused_why)}


_HANDOFF_SQL = ("SELECT handoff_id, conversation_id, reason, delivery, created_at FROM cs_handoff"
                " WHERE tenant_id=? ORDER BY created_at DESC, handoff_id DESC LIMIT ?")


def tool_cs_handoff_list(ctx: CsMcpContext, args: dict) -> dict[str, Any]:
    _check_keys(args, {"tenant_id", "limit"})
    raw_tenant = _str_arg(args, "tenant_id")
    limit = args.get("limit", DEFAULT_HANDOFF_LIMIT)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_HANDOFF_LIMIT:
        raise ToolFailure(f"limit 必须是 1..{MAX_HANDOFF_LIMIT} 的整数")
    tenant = ctx.tenant(raw_tenant)
    if not tenant:
        return {"handoffs": [], "count": 0}
    # 只选这五列：card_json（客户原文、打码客户标识、处理建议）一列都不读。
    rows = objects.query(ctx.store, _HANDOFF_SQL, (tenant, limit))
    items = [{"handoff_id": str(r["handoff_id"]), "conversation_id": str(r["conversation_id"]),
              "reason": str(r["reason"]), "delivery": str(r["delivery"]),
              "created_at": str(r["created_at"])} for r in rows]
    return {"handoffs": items, "count": len(items)}


DISPATCH = {
    "cs_order_status": tool_cs_order_status,
    "cs_refund_precheck": tool_cs_refund_precheck,
    "cs_handoff_list": tool_cs_handoff_list,
}


# ---------------------------------------------------------------------------
# JSON-RPC 方法（形状照 server.py::handle）
# ---------------------------------------------------------------------------
def handle(msg: dict[str, Any], ctx: CsMcpContext) -> dict[str, Any] | None:
    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params") or {}
    if req_id is None:
        return None
    if not isinstance(params, dict):
        return error(req_id, E_INVALID_PARAMS, "params 必须是对象")

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
        if not isinstance(name, str):
            return error(req_id, E_INVALID_PARAMS, "name 必须是字符串")
        fn = DISPATCH.get(name)
        if fn is None:
            return error(req_id, E_INVALID_PARAMS, f"未知工具: {name}")
        try:
            payload = fn(ctx, args)
        except ToolFailure as exc:
            return result(req_id, {"content": [{"type": "text", "text": str(exc)}],
                                   "isError": True})
        except Exception as exc:            # noqa: BLE001 —— 只报类名：原文可能带订单号
            return error(req_id, E_INTERNAL, type(exc).__name__)
        return result(req_id, {
            "content": [{"type": "text",
                         "text": json.dumps(payload, ensure_ascii=False, sort_keys=True)}],
            "structuredContent": payload,
            "isError": False,
        })
    return error(req_id, E_METHOD_NOT_FOUND, f"未实现的方法: {method}")


def serve(ctx: CsMcpContext, stdin=None, stdout=None) -> int:
    # 按字节读：帧长直接量字节数；非法 UTF-8 交给 protocol.decode（errors="replace"）→ E_PARSE，
    # 不会在量长度时抛 UnicodeEncodeError 把连接器打死。
    stdin = stdin or getattr(sys.stdin, "buffer", sys.stdin)
    stdout = stdout or sys.stdout.buffer
    for line in stdin:
        if not line.strip():
            continue
        raw = line if isinstance(line, bytes) else line.encode("utf-8", "surrogateescape")
        if len(raw) > MAX_FRAME_BYTES:
            reply: dict[str, Any] | None = error(
                None, E_INVALID_REQUEST, f"单帧超过 {MAX_FRAME_BYTES} 字节上限")
        else:
            try:
                msg = decode(raw)
            except Exception as exc:        # noqa: BLE001
                reply = error(None, getattr(exc, "code", None) or E_PARSE, str(exc))
            else:
                try:
                    reply = handle(msg, ctx)
                except Exception as exc:    # noqa: BLE001 —— 一帧出错不许打死连接器；只报类名
                    reply = error(msg.get("id"), E_INTERNAL, type(exc).__name__)
        if reply is not None:
            stdout.write(encode(reply))
            stdout.flush()
    return 0


# ---------------------------------------------------------------------------
# 装配（口径照 scripts/run_ingress.py::_front_desk）
# ---------------------------------------------------------------------------
def _parse_tenant_map(raw: str | None) -> dict[str, str] | None:
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        raise ValueError("--tenant-map 不是合法 JSON") from None
    if not isinstance(data, dict) or not all(
            isinstance(k, str) and k and isinstance(v, str) and v for k, v in data.items()):
        raise ValueError("--tenant-map 必须是 {调用方租户: 内部租户} 的非空字符串对象")
    return dict(data)


def build_context(store: Any, env: Mapping[str, str] = os.environ, *,
                  ledger_path: str | Path = DEFAULT_LEDGER,
                  tenant_map: Mapping[str, str] | None = None) -> CsMcpContext:
    """装配查单与预检。没配 / 配坏了查单 → lookup=None（工具回 system_misconfigured）。"""
    from maos.ingress.cs_ports import LedgerRefundPrecheck, build_order_lookup_from_env

    try:
        lookup = build_order_lookup_from_env(env, ledger_path=ledger_path)
    except Exception as exc:                            # noqa: BLE001 —— 只报类名，不回显取值
        print(f"cs_server: 查单端口装配失败（{type(exc).__name__}），按没配处理", file=sys.stderr)
        lookup = None
    precheck = None
    tenant = str(env.get(ENV_CS_LEDGER_TENANT) or "").strip()
    if lookup is not None and tenant:
        precheck = LedgerRefundPrecheck(ledger_path, ledger_tenant=tenant)
    return CsMcpContext(store, lookup=lookup, precheck=precheck, tenant_map=tenant_map)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="MAOS 客服只读 MCP server（stdio）")
    ap.add_argument("--db", required=True, help="客服前台的库（SqliteStore 路径或 :memory:）")
    ap.add_argument("--tenant-map", default=None,
                    help='JSON {调用方租户: 内部租户}；给了就只认映射里的租户')
    ap.add_argument("--ledger", default=str(DEFAULT_LEDGER),
                    help="台账路径（demo 查单与退款预检用），缺省 scenarios/custom/ledger.json")
    ns = ap.parse_args(argv)
    try:
        tenant_map = _parse_tenant_map(ns.tenant_map)
    except ValueError as exc:
        print(f"cs_server: {exc}", file=sys.stderr)
        return 2
    from maos.core.store import SqliteStore

    try:
        store = SqliteStore(ns.db)
        store.init_schema()
        objects.ensure_schema(store)
    except Exception as exc:                            # noqa: BLE001
        print(f"cs_server: 库打不开（{type(exc).__name__}）", file=sys.stderr)
        return 2
    ctx = build_context(store, ledger_path=ns.ledger, tenant_map=tenant_map)
    return serve(ctx)


if __name__ == "__main__":                  # pragma: no cover —— 由客户端拉起
    raise SystemExit(main())
