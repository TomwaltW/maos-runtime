"""自然语言接入面的理解层：一句人话 → 结构化 ``Intent``。

本层**只理解，不执行**（跨轨契约 R1）。派发与权限闸在 ``maos.runtime``，
房间收发在 ``hiclaw``，两边都只读引用这里的数据契约。
"""

from maos.nlu.intent import (
    ACTION_APPROVE,
    ACTION_REJECT,
    ACTION_STATUS,
    ACTION_UNKNOWN,
    ACTIONS,
    CONF_HIGH,
    CONF_LOW,
    Intent,
    parse_intent,
)

__all__ = [
    "ACTION_APPROVE",
    "ACTION_REJECT",
    "ACTION_STATUS",
    "ACTION_UNKNOWN",
    "ACTIONS",
    "CONF_HIGH",
    "CONF_LOW",
    "Intent",
    "parse_intent",
]
