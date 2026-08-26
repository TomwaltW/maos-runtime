"""事件契约 —— Track A 与 Track B 之间唯一的约定面。

这个文件是"冻结契约"。任何一方要改，必须双方同步确认后再改。
换成 RocketMQ 之后，Envelope 直接序列化成消息体，字段一一对应，不需要再改。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any


# --------------------------------------------------------------------------
# Topic 定义（对应 RocketMQ 的 Topic，现在是内存 EventBus 的 key）
# --------------------------------------------------------------------------
class Topic:
    TASK_ASSIGNMENT = "maos.task.assignment"   # Control Plane -> Worker
    TASK_RESULT = "maos.task.result"           # Worker -> Control Plane
    REVIEW_VERDICT = "maos.review.verdict"     # Reviewer Gate -> Control Plane
    REWORK = "maos.task.rework"                # Control Plane -> Worker（返工）
    DEAD_LETTER = "maos.dlq"                   # 重试耗尽


# --------------------------------------------------------------------------
# 事件类型
# --------------------------------------------------------------------------
class EventType:
    TASK_ASSIGNMENT = "TaskAssignment"
    TASK_RESULT = "TaskResult"
    REVIEW_VERDICT = "ReviewVerdict"
    REWORK = "Rework"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# --------------------------------------------------------------------------
# 统一信封
# --------------------------------------------------------------------------
@dataclass
class Envelope:
    """所有事件共用的信封。payload 由具体事件类型决定。

    idempotency_key 是幂等的唯一依据：Control Plane 见过同一个 key 就直接返回上次
    的处理结果，不再做状态迁移。RocketMQ 的重投、Worker 的重启都靠这个字段兜住。
    """

    event_type: str
    plan_id: str
    task_id: str
    idempotency_key: str
    payload: dict[str, Any] = field(default_factory=dict)

    event_id: str = field(default_factory=lambda: new_id("evt"))
    trace_id: str = ""
    attempt: int = 1
    occurred_at: str = field(default_factory=_now)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @staticmethod
    def from_json(raw: str) -> "Envelope":
        return Envelope(**json.loads(raw))


# --------------------------------------------------------------------------
# 四类核心事件的 payload 构造器
# 用函数而不是类，是为了让 payload 永远是纯 dict —— 换 MQ 的时候零成本序列化
# --------------------------------------------------------------------------
def task_assignment(
    *,
    plan_id: str,
    task_id: str,
    role: str,
    attempt: int,
    trace_id: str,
    inputs: dict[str, Any],
    acceptance: list[str],
    risk_level: str = "L",
    rework_findings: list[dict] | None = None,
) -> Envelope:
    """Control Plane 派发任务。role 决定由哪个 Agent 消费。"""
    return Envelope(
        event_type=EventType.TASK_ASSIGNMENT,
        plan_id=plan_id,
        task_id=task_id,
        trace_id=trace_id,
        attempt=attempt,
        idempotency_key=f"assign:{task_id}:{attempt}",
        payload={
            "role": role,
            "inputs": inputs,
            "acceptance": acceptance,
            "risk_level": risk_level,
            "rework_findings": rework_findings or [],
        },
    )


def task_result(
    *,
    plan_id: str,
    task_id: str,
    attempt: int,
    trace_id: str,
    status: str,                      # ok | failed | blocked
    artifacts: list[dict] | None = None,
    open_questions: list[str] | None = None,
    error: str | None = None,
    worker_id: str = "",
    metrics: dict | None = None,
) -> Envelope:
    """Worker 交回执行结果。"""
    return Envelope(
        event_type=EventType.TASK_RESULT,
        plan_id=plan_id,
        task_id=task_id,
        trace_id=trace_id,
        attempt=attempt,
        idempotency_key=f"result:{task_id}:{attempt}",
        payload={
            "status": status,
            "artifacts": artifacts or [],
            "open_questions": open_questions or [],
            "error": error,
            "worker_id": worker_id,
            "metrics": metrics or {},
        },
    )


def review_verdict(
    *,
    plan_id: str,
    task_id: str,
    attempt: int,
    trace_id: str,
    verdict: str,                     # pass | rework | block
    findings: list[dict] | None = None,
    gate_results: dict | None = None,
) -> Envelope:
    """Reviewer Gate 的判定结果。findings 必须结构化，Coding Agent 要能直接消费。"""
    return Envelope(
        event_type=EventType.REVIEW_VERDICT,
        plan_id=plan_id,
        task_id=task_id,
        trace_id=trace_id,
        attempt=attempt,
        idempotency_key=f"verdict:{task_id}:{attempt}",
        payload={
            "verdict": verdict,
            "findings": findings or [],
            "gate_results": gate_results or {},
        },
    )


def rework(
    *,
    plan_id: str,
    task_id: str,
    next_attempt: int,
    trace_id: str,
    findings: list[dict],
    reason: str,
) -> Envelope:
    """Gate 不通过后的返工事件。next_attempt 是新一轮的 attempt 号。"""
    return Envelope(
        event_type=EventType.REWORK,
        plan_id=plan_id,
        task_id=task_id,
        trace_id=trace_id,
        attempt=next_attempt,
        idempotency_key=f"rework:{task_id}:{next_attempt}",
        payload={"findings": findings, "reason": reason},
    )


# --------------------------------------------------------------------------
# Schema 校验（Gate 的第一道闸，也是契约的可执行版本）
# --------------------------------------------------------------------------
_REQUIRED_PAYLOAD_FIELDS = {
    EventType.TASK_ASSIGNMENT: {"role", "inputs", "acceptance", "risk_level"},
    EventType.TASK_RESULT: {"status", "artifacts"},
    EventType.REVIEW_VERDICT: {"verdict"},
    EventType.REWORK: {"findings", "reason"},
}

_VALID_RESULT_STATUS = {"ok", "failed", "blocked"}
_VALID_VERDICT = {"pass", "rework", "block"}


def validate(env: Envelope) -> list[str]:
    """返回错误列表，空列表表示通过。跑通之后这里直接换 jsonschema 也行。"""
    errs: list[str] = []

    for f in ("plan_id", "task_id", "idempotency_key", "event_type"):
        if not getattr(env, f, None):
            errs.append(f"envelope.{f} 不能为空")

    required = _REQUIRED_PAYLOAD_FIELDS.get(env.event_type)
    if required is None:
        errs.append(f"未知 event_type: {env.event_type}")
        return errs

    missing = required - set(env.payload)
    if missing:
        errs.append(f"payload 缺字段: {sorted(missing)}")

    if env.event_type == EventType.TASK_RESULT:
        st = env.payload.get("status")
        if st not in _VALID_RESULT_STATUS:
            errs.append(f"status 非法: {st}")
    if env.event_type == EventType.REVIEW_VERDICT:
        v = env.payload.get("verdict")
        if v not in _VALID_VERDICT:
            errs.append(f"verdict 非法: {v}")

    return errs
