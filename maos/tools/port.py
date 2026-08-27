"""ToolPort —— 工具的九要素声明，以及带审计的统一调用口。

工具是 Agent 唯一能碰外部世界的地方，所以声明必须比 skill 更严：
failure_modes 与 security_boundary 不是文档，是评审时会被逐条对的东西。

调用一律走 invoke_tool()，不要直接调 port.entry —— 直接调就没有 ToolInvoked 审计行，
出事之后查不到是谁、什么参数、跑了多久。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger("maos.tools")


@dataclass
class ToolPort:
    name: str
    purpose: str
    entry: Callable[..., Any]
    params_schema: dict = field(default_factory=dict)
    returns_schema: dict = field(default_factory=dict)
    failure_modes: list[str] = field(default_factory=list)
    security_boundary: str = ""
    rate_limit: str = ""                                # 如 "10/min"；空 = 未设限
    owner: str = ""


def _digest(obj: Any) -> str:
    try:
        raw = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        raw = repr(obj)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def invoke_tool(port: ToolPort, params: dict, *, store: Any = None,
                extras: dict | None = None) -> Any:
    """调 ``port.entry(**params)``，落一条 ToolInvoked event_log 行，返回原始返回值。

    工具抛异常时**先落审计再原样抛出** —— 工具失败要被上层的状态机接住并记录，
    不能在这里被吞成一个 None。
    """
    extras = dict(extras or {})
    started = time.perf_counter()
    status, error = "ok", None
    try:
        return port.entry(**params)
    except Exception as exc:                            # noqa: BLE001
        status, error = "failed", f"{type(exc).__name__}: {exc}"
        raise
    finally:
        duration_ms = int((time.perf_counter() - started) * 1000)
        if store is not None:
            store.append_event_log({
                "event_id": extras.get("event_id", ""),
                "trace_id": extras.get("trace_id", ""),
                "plan_id": extras.get("plan_id", ""),
                "task_id": extras.get("task_id"),
                "event_type": "ToolInvoked",
                "from_state": "",
                "to_state": "",
                "detail": {
                    "tool": port.name,
                    "status": status,
                    "duration_ms": duration_ms,
                    "params_digest": _digest(params),
                    "error": error,
                },
            })
