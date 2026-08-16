"""任务与计划状态机 —— 显式迁移表。

写成显式表而不是 if/else 散在各处，是因为 Control Plane 是唯一状态权威：
所有迁移都必须过 can_transition()，非法迁移直接抛异常，而不是静默写坏状态。
这个文件和 events.py 一样属于冻结契约。
"""

from __future__ import annotations


class TaskState:
    PENDING = "PENDING"                    # 已创建，等待派发（依赖未满足也停在这）
    DISPATCHED = "DISPATCHED"              # 派发事件已发出，等待 Worker 认领
    RUNNING = "RUNNING"                    # Worker 已认领并执行中
    AWAITING_REVIEW = "AWAITING_REVIEW"    # 产出已交回，等待 Reviewer Gate
    REWORK = "REWORK"                      # Gate 判定返工，等待重新入队
    BLOCKED = "BLOCKED"                    # 阻塞：待人工审批 / open_questions 未澄清
    DONE = "DONE"                          # 终态：成功
    FAILED = "FAILED"                      # 终态：失败（重试已耗尽或人工驳回）


TERMINAL_STATES = frozenset({TaskState.DONE, TaskState.FAILED})


# (from, to) -> 迁移原因标签，用于审计日志和 Studio 展示
TASK_TRANSITIONS: dict[tuple[str, str], str] = {
    (TaskState.PENDING, TaskState.DISPATCHED): "dispatch",
    (TaskState.DISPATCHED, TaskState.RUNNING): "claim",
    (TaskState.DISPATCHED, TaskState.PENDING): "claim_timeout",       # 认领超时，重投
    (TaskState.RUNNING, TaskState.AWAITING_REVIEW): "submit_result",
    (TaskState.RUNNING, TaskState.BLOCKED): "worker_blocked",
    (TaskState.RUNNING, TaskState.PENDING): "retry",                  # 失败且还有重试额度
    (TaskState.RUNNING, TaskState.FAILED): "retry_exhausted",
    (TaskState.AWAITING_REVIEW, TaskState.DONE): "gate_pass",
    (TaskState.AWAITING_REVIEW, TaskState.REWORK): "gate_rework",
    (TaskState.AWAITING_REVIEW, TaskState.BLOCKED): "gate_needs_human",
    (TaskState.AWAITING_REVIEW, TaskState.FAILED): "gate_reject_final",
    (TaskState.REWORK, TaskState.PENDING): "requeue",
    (TaskState.REWORK, TaskState.FAILED): "rework_exhausted",
    (TaskState.BLOCKED, TaskState.PENDING): "human_resume",
    (TaskState.BLOCKED, TaskState.DONE): "human_approve",
    (TaskState.BLOCKED, TaskState.FAILED): "human_reject",
}


class PlanState:
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"


PLAN_TRANSITIONS: dict[tuple[str, str], str] = {
    (PlanState.PENDING, PlanState.RUNNING): "start",
    (PlanState.RUNNING, PlanState.DONE): "all_tasks_done",
    (PlanState.RUNNING, PlanState.FAILED): "task_failed",
    (PlanState.RUNNING, PlanState.PENDING): "replan",
}


class IllegalTransition(Exception):
    """非法状态迁移。出现这个异常说明有代码绕过了状态机，不要 catch 掉。"""


def can_transition(src: str, dst: str, table=TASK_TRANSITIONS) -> bool:
    return (src, dst) in table


def assert_transition(src: str, dst: str, table=TASK_TRANSITIONS) -> str:
    if (src, dst) not in table:
        raise IllegalTransition(f"非法迁移: {src} -> {dst}")
    return table[(src, dst)]


# 风险等级。两种风险必须分开，不要合成一个字段：
#
#   risk_level  = Agent「执行」这个任务的风险 —— 与 Agent Identity 的 max_risk 比对，
#                 超了就是越权，直接拒绝执行。
#   effect_risk = 产物「落地」的风险 —— 合主干、改生产配置属于 H，
#                 但执行这个动作的是平台（在人工批准后），不是 Agent。
#
# 合成一个字段会导致：一个高风险变更任务，Coding Agent 因为 max_risk=M 而拒绝执行，
# 于是重试耗尽直接 FAILED，人工审批环节永远走不到。
class Risk:
    LOW = "L"
    MEDIUM = "M"
    HIGH = "H"


NEEDS_HUMAN_APPROVAL = frozenset({Risk.HIGH})   # 按 effect_risk 判定
