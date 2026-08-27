"""SkillInvoker —— Agent 调 skill 的唯一入口：查权限、查注册、按策略重试、落审计。

两条刻意的不对称（冻结契约 A-5，不要"统一"掉）：
  · 越权（skill 不在 identity.allowed_skills）→ **抛** PermissionDenied。
    这是安全事件，必须炸出来，不能变成一条 failed 记录被吞掉。
  · 未注册（skill 还没人实现）→ **返回** failed + ``skill_not_found:<name>``。
    并行开发期各轨按名互调，被调方还没合并进来是常态，不该炸链路；
    合并后同名 skill 一注册，调用点零改动自动升级为真实现。

审计落的是 event_log **行**（event_type="SkillInvoked"），不是总线 Envelope ——
冻结的 maos/contracts/events.py 里没有这个事件类型，也不许为此去加。

每次 invoke 生成一个 invocation_id（uuid4().hex），**成败都生成**，
既返回给调用方（SkillResult.invocation_id）也写进落库那行的 detail ——
两侧同一个值，后续 Phase 的权威事实守卫靠它做 actor 溯源。
连 skill_not_found / precondition_failed 这两条早退也要带上：
失败调用同样是需要被追溯的事实。

模块内不 import maos.agents：skills 层保持可独立 import，
PermissionDenied 在 invoke() 里延迟取（两边互相引用，见 base.py 的 self.skills）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from typing import Any

from maos.skills import registry
from maos.skills.contract import SkillContext, SkillResult

log = logging.getLogger("maos.skills")


def _digest(obj: Any) -> str:
    try:
        raw = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        raw = repr(obj)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


class SkillInvoker:
    def __init__(self, identity: Any, store: Any = None) -> None:
        self.identity = identity
        self.store = store

    def invoke(self, name: str, payload: dict, *, version: str | None = None,
               extras: dict | None = None) -> SkillResult:
        """按名调用 skill。extras 里可带 model / plan_id / task_id / trace_id。"""
        from maos.agents.base import PermissionDenied   # 延迟 import：见模块 docstring

        extras = dict(extras or {})
        allowed = getattr(self.identity, "allowed_skills", frozenset())
        if name not in allowed:
            raise PermissionDenied(
                f"{getattr(self.identity, 'agent_id', '?')} 无权调用 skill {name}"
                f"（白名单: {sorted(allowed)}）"
            )

        started = time.perf_counter()
        invocation_id = uuid.uuid4().hex
        cls = registry.get(name, version)
        if cls is None:
            return self._settle(name, None, SkillResult(
                status="failed", error=f"skill_not_found:{name}",
                duration_ms=_elapsed_ms(started),
                invocation_id=invocation_id), payload, extras)

        contract = cls.contract
        missing = [c for c in contract.preconditions if payload.get(c) is None]
        if missing:
            return self._settle(name, contract.version, SkillResult(
                status="failed", error="precondition_failed:" + ",".join(missing),
                duration_ms=_elapsed_ms(started),
                invocation_id=invocation_id), payload, extras)

        ctx = SkillContext(model=extras.get("model"), store=self.store,
                           identity=self.identity, extras=extras)
        attempts = contract.max_retries + 1 if contract.failure_policy == "retry" else 1
        skill = cls()
        output: Any = None
        error: str | None = None
        ok = False
        for i in range(attempts):
            try:
                output = skill.run(payload, ctx)
                ok = True
                break
            except PermissionDenied:
                raise                                   # 安全事件：不吞、不重试
            except Exception as exc:                    # noqa: BLE001
                error = f"{type(exc).__name__}: {exc}"
                if i + 1 < attempts:
                    log.warning("skill %s 第 %d 次失败，重试：%s", name, i + 1, error)

        return self._settle(name, contract.version, SkillResult(
            status="ok" if ok else "failed",
            output=output if ok else None,
            error=None if ok else error,
            duration_ms=_elapsed_ms(started),
            usage=extras.get("usage"),
            invocation_id=invocation_id,
        ), payload, extras)

    # ------------------------------------------------------------------
    def _settle(self, name: str, version: str | None, result: SkillResult,
                payload: dict, extras: dict) -> SkillResult:
        """成败都落一条 SkillInvoked。store=None（未接线的 Agent）则跳过。"""
        if self.store is None:
            return result
        self.store.append_event_log({
            "event_id": extras.get("event_id", ""),
            "trace_id": extras.get("trace_id", ""),
            "plan_id": extras.get("plan_id", ""),
            "task_id": extras.get("task_id"),
            "event_type": "SkillInvoked",
            "from_state": "",
            "to_state": "",
            "detail": {
                "skill": name,
                "version": version,
                "status": result.status,
                "duration_ms": result.duration_ms,
                "input_digest": _digest(payload),
                "output_hash": _digest(result.output),
                "usage": result.usage,
                "invocation_id": result.invocation_id,
            },
        })
        return result
