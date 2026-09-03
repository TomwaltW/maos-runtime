"""配置变更审计 —— 把每一次旋钮变更落成一条 `event_log`。

复赛 30% 维度（工程落地与安全审计）原文要的是「审计日志能追溯**谁**在**什么时候**、
做了**什么操作**」。四个旋钮此前是进程启动时读一次环境变量：改审批人名单要重启，
而且**没有任何一条记录说明是谁改的**。本模块补的就是后半句。

`MAOS_APPROVERS` 尤其对味 —— 它是**审批权限名单**，动它属于安全事件。

## 为什么不新增表、不新增 Topic

三条约束叠在一起，只剩一条路：

* `maos/core/store.py` 的表结构禁改（铁律 1），且这一轮它归 T29，我这一轨不许碰；
* `maos/contracts/events.py` 是冻结契约，`EventType` 那四个不许加第五个（铁律 1）；
* 审计必须真的落库，不能只打日志。

于是走**现有 `append_event_log` 的自由 `event_type`** —— 这不是本模块的发明，是仓库
里已经用了很多轮的成文写法，`maos/agents/testing.py:50` 把它写成了纪律：

    「走 ``append_event_log`` 的自由 ``event_type``，**不进 contracts/events.py 的
      Topic**（铁律 1）—— ``SkillInvoked`` / ``ToolInvoked`` /
      ``AuthoritativeFactViolation`` 都是这么加的。」

`maos/kb/retriever.py:571` 的 `KbRetrieved` 是同一条路上最近的一个先例，那里也写着
「走现有 `append_event_log`，**不加新 Topic** —— 冻结的事件契约里没有这个类型，
也不许为此去加」。`ConfigChanged` 是这条队伍里的下一个，**一个字节的契约都没动**。

## 默认不接线

`attach_config_audit()` 不被调用时，本模块一行都不跑，`event_log` 里一条
`ConfigChanged` 都不会多。缺省路径（`MAOS_CONFIG_SOURCE` 未设、没人订阅）因此与
本包出现之前逐字节一致 —— 这是 §5.0 那条压倒一切的约束的一部分。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from maos.config.source import ConfigChange, redact, subscribe

log = logging.getLogger("maos.config.audit")

__all__ = [
    "CONFIG_CHANGED_EVENT",
    "ConfigAuditor",
    "attach_config_audit",
]

#: `event_log.event_type` 的字面量。**不进 `contracts/events.py`**，理由见模块抬头。
#: 与 `SkillInvoked` / `ToolInvoked` / `KbRetrieved` / `ArtifactSeeded` 同类。
CONFIG_CHANGED_EVENT = "ConfigChanged"


class ConfigAuditor:
    """把 `ConfigChange` 写成一条 `event_log`。

    `sink` 只需要有 `append_event_log(row)` —— 核心 `Store` 有，`StorePort` 的
    五方法契约里**没有**（事件日志是核心 Store 的冻结表，不是端口的职责，
    出处 `maos/kb/retriever.py:577`）。所以这里按鸭子类型收，收不到就 WARNING
    一次并把这条审计丢掉，**不抛** —— 配置面的旁路不该掀掉主链路。
    """

    def __init__(self, sink: Any, *, plan_id: str = "", trace_id: str = "") -> None:
        self.sink = sink
        self.plan_id = plan_id
        self.trace_id = trace_id
        self._warned = False

    def __call__(self, change: ConfigChange) -> None:
        self.record(change)

    def record(self, change: ConfigChange) -> dict | None:
        """落一条 `ConfigChanged`，返回落库用的那一行；落不下去返回 `None`。"""
        append = getattr(self.sink, "append_event_log", None)
        if append is None:
            if not self._warned:
                self._warned = True
                log.warning("%s 没有 append_event_log，配置变更审计落不下去",
                            type(self.sink).__name__)
            return None

        row = {
            "event_id": "",
            "trace_id": self.trace_id,
            "plan_id": self.plan_id,
            "task_id": None,
            "event_type": CONFIG_CHANGED_EVENT,
            # from_state / to_state 是状态机的列，不借来装配置值：`maos/obs/trace.py`
            # 按 event_type 认领这两列，借用会让配置变更混进状态轨迹里。
            "from_state": "",
            "to_state": "",
            # 被闸门拒掉的那一次，`reason` 这一行必须自己说清楚 —— 否则一眼看去
            # 它和一次真的生效了的变更长得一模一样，而 `detail.rejected` 藏在 JSON 里
            # （T35 §5.2）。
            "reason": (f"{change.key}: {redact(change.key, change.old)!r}"
                       f" -> {redact(change.key, change.new)!r}"
                       + ("（已拒绝采用，沿用旧值）"
                          if change.detail.get("rejected") else "")),
            "detail": change.as_detail(),
        }
        try:
            append(row)
        except Exception as exc:                        # noqa: BLE001
            log.warning("配置变更审计写入失败（%s）：%s", exc, change.key)
            return None
        log.info("已落审计 %s：%s", CONFIG_CHANGED_EVENT, row["reason"])
        return row


def attach_config_audit(sink: Any, *, plan_id: str = "",
                        trace_id: str = "") -> Callable[[], None]:
    """订阅配置变更并逐条落审计。返回取消订阅的函数。

    `plan_id` 缺省是空串 —— 配置变更是**进程级**的事，不天然属于某个 plan。
    演示与排障时想让它跟着某次运行走（`list_event_log(plan_id)` 一把捞出来），
    把那次的 plan_id 传进来即可，`hiclaw/room_demo.py` 就是这么接的。
    """
    return subscribe(ConfigAuditor(sink, plan_id=plan_id, trace_id=trace_id))
