"""EventBus —— 语义刻意对齐 RocketMQ 消费者模型。

刻意保留的语义（换 RocketMQ 时不用改上层代码）：
  · publish 只保证"至少一次"投递 —— 所以下游必须靠 idempotency_key 去重
  · handler 抛异常 = nack，消息回队重试
  · 重试超过 max_redelivery 进死信 topic

现在是单线程串行 drain，是刻意的：MVP 阶段要的是可复现的执行顺序，
出了问题能一眼看出是哪一步的状态迁移错了，而不是被并发掩盖。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from typing import Callable

from maos.contracts.events import Envelope, Topic

log = logging.getLogger("maos.bus")

Handler = Callable[[Envelope], None]


class EventBus(ABC):
    @abstractmethod
    def publish(self, topic: str, env: Envelope) -> None: ...

    @abstractmethod
    def subscribe(self, topic: str, group: str, handler: Handler) -> None: ...

    @abstractmethod
    def drain(self, max_rounds: int = 1000) -> int: ...


class InMemoryEventBus(EventBus):
    def __init__(self, max_redelivery: int = 2) -> None:
        self._queues: dict[str, deque[Envelope]] = defaultdict(deque)
        self._subs: dict[str, list[tuple[str, Handler]]] = defaultdict(list)
        self._redelivery: dict[str, int] = defaultdict(int)
        self._max_redelivery = max_redelivery
        self.dead_letters: list[Envelope] = []

    def publish(self, topic: str, env: Envelope) -> None:
        log.debug("publish %s -> %s (%s)", env.event_type, topic, env.idempotency_key)
        self._queues[topic].append(env)

    def subscribe(self, topic: str, group: str, handler: Handler) -> None:
        self._subs[topic].append((group, handler))

    def drain(self, max_rounds: int = 1000) -> int:
        """把所有队列跑到空。返回处理的消息数。

        max_rounds 是防呆：状态机写错导致事件互相无限触发时，不会挂死，
        而是直接抛出来让你看到环在哪。
        """
        processed = 0
        for _ in range(max_rounds):
            topic = next((t for t, q in self._queues.items() if q), None)
            if topic is None:
                return processed
            env = self._queues[topic].popleft()
            for group, handler in self._subs.get(topic, []):
                try:
                    handler(env)
                except Exception as exc:  # noqa: BLE001 —— 等价于 RocketMQ 的 nack
                    key = f"{topic}:{group}:{env.event_id}"
                    self._redelivery[key] += 1
                    if self._redelivery[key] > self._max_redelivery:
                        log.error("进入死信: %s (%s)", env.idempotency_key, exc)
                        self.dead_letters.append(env)
                        self._queues[Topic.DEAD_LETTER].append(env)
                    else:
                        log.warning("重投 %s 第 %d 次: %s",
                                    env.idempotency_key, self._redelivery[key], exc)
                        self._queues[topic].append(env)
            processed += 1
        raise RuntimeError(
            f"drain 超过 {max_rounds} 轮仍未收敛 —— 大概率是状态机里有事件环路"
        )
