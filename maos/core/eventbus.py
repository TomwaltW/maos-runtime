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
import os
import re
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


# ==========================================================================
# RocketMQ 后端（T27）—— 以下全是**新增**，上面 InMemoryEventBus 一行没动。
#
# 为什么不许动上面那个：它是等价性证明的**基准侧**。改了它，「两个后端行为一致」
# 这个结论就变成自我循环了。
#
# 🔴 缺省路径一个字节都不变：MAOS_EVENTBUS_BACKEND 未设 = InMemoryEventBus。
# 本仓库最大的卖点是「无需任何 key、裸 clone 跑到 7/7」，RocketMQ 一旦变成前提，
# 这个卖点当场作废。所以客户端是可选依赖（不进 pyproject.toml），没装 / 连不上
# 都只影响显式选了 rocketmq 的调用方。
# ==========================================================================

#: 选后端的环境变量。未设 = memory，与今天逐字节一致。
BACKEND_ENV = "MAOS_EVENTBUS_BACKEND"

MEMORY = "memory"
ROCKETMQ = "rocketmq"
DEFAULT_BACKEND = MEMORY

#: 接入点。5.x 的 Python 客户端连的是 **proxy 的 gRPC 口**，不是 namesrv 的 9876。
ENDPOINTS_ENV = "MAOS_ROCKETMQ_ENDPOINTS"
DEFAULT_ENDPOINTS = "localhost:8081"

#: 消费者组。同一个组的多个进程分摊消息，不同组各收一份。
GROUP_ENV = "MAOS_ROCKETMQ_GROUP"
DEFAULT_GROUP = "maos_default"

#: 鉴权。铁律 6：只读环境变量，禁止落文件，也禁止回显进 evidence/。
ACCESS_KEY_ENV = "MAOS_ROCKETMQ_AK"
SECRET_KEY_ENV = "MAOS_ROCKETMQ_SK"
NAMESPACE_ENV = "MAOS_ROCKETMQ_NAMESPACE"

#: topic 前缀。测试用它把靶 topic 和演示数据隔开，生产一般不设。
TOPIC_PREFIX_ENV = "MAOS_ROCKETMQ_TOPIC_PREFIX"

#: 长轮询窗口（秒）。实测 broker 侧有 **5 秒下限**：传 1 或 3 都是 4.97s，
#: 传 10 才真的等 10s。所以缺省给 1 = 「要 broker 允许的最短窗口」。
AWAIT_SECONDS_ENV = "MAOS_ROCKETMQ_AWAIT_SECONDS"
DEFAULT_AWAIT_SECONDS = 1

#: 消息取走后的不可见期（秒）。handler 跑完之前别让别人再拿到同一条。
INVISIBLE_SECONDS_ENV = "MAOS_ROCKETMQ_INVISIBLE_SECONDS"
DEFAULT_INVISIBLE_SECONDS = 30

#: 判空需要连续多少次空返回。见 RocketMQEventBus.drain 的长注释。
EMPTY_ROUNDS_ENV = "MAOS_ROCKETMQ_EMPTY_ROUNDS"
DEFAULT_EMPTY_ROUNDS = 1

#: 空返回但「已知有在途重投」时，额外多等几轮。
NACK_GRACE_ENV = "MAOS_ROCKETMQ_NACK_GRACE"
DEFAULT_NACK_GRACE = 2

#: RocketMQ 允许的 topic 名字符集（客户端正则 ^[%a-zA-Z0-9_-]+$，broker 侧一致）。
_RMQ_TOPIC_ILLEGAL = re.compile(r"[^%a-zA-Z0-9_-]")


class RocketMQBackendUnavailable(RuntimeError):
    """RocketMQ 此刻用不了：没装客户端 / 连不上 proxy / topic 路由拉不到。

    **绝不回落 InMemoryEventBus** —— 回落的后果是「RocketMQ 后端看起来跑通了」
    而其实一条消息都没进 broker，等真上生产那天，所有以为验过的路径都得重验，
    且没有任何东西提示你该重验。这条口径与 `maos.store` 的 PgBackendUnavailable
    一致，不是本轨的发明。
    """


def rmq_topic_name(topic: str, prefix: str = "") -> str:
    """把 MAOS 的 topic 名翻成 RocketMQ 合法的 topic 名。

    🔴 **这一层是被迫加的，不是设计洁癖。** MAOS 契约里的 Topic 常量长这样：

        maos.task.assignment   maos.task.result   maos.dlq

    而 RocketMQ **不允许 topic 名里有点号** —— 客户端侧正则是 ``^[%a-zA-Z0-9_-]+$``，
    broker 侧 mqadmin 同样拒绝（实测：带点的 updateTopic 报 createTopic 失败，
    而且**退出码仍然是 0**）。契约文件是冻结面，不许为了迁就 MQ 去改它，
    所以映射做在这里：非法字符一律换成下划线。

        maos.task.assignment -> maos_task_assignment

    上层代码因此**仍然一行都不用改** —— 但「Envelope 直接序列化成消息体，字段一一
    对应，不需要再改」这句话只对**消息体**成立，**topic 名需要这一层翻译**。
    这是本轨实测出来的一处口径修正，记在 deploy/rocketmq-live.md §3.2。
    """
    return prefix + _RMQ_TOPIC_ILLEGAL.sub("_", topic)


class RocketMQEventBus(EventBus):
    """EventBus 的 RocketMQ 5.x 实现 —— 三个方法逐条对齐 InMemoryEventBus 的语义。

    | 方法 | 语义 |
    | :-- | :-- |
    | publish | Envelope 序列化成消息体投递；只保证至少一次 |
    | subscribe | 注册消费者组 |
    | drain | 消费到空为止，返回处理条数 |
    | handler 抛异常 | = nack（不 ack），重投；超过 max_redelivery 进死信 topic |

    客户端是**可选依赖**，只在真用到时才 import：

        pip install rocketmq-python-client        # 5.x 的 gRPC 客户端

    ⚠️ 别装 `rocketmq-client-python`（4.x）—— 那个依赖 librocketmq C++ SDK，
    Apple Silicon 上大概率编不过。两者是不同的包。

    ## 三处与内存版对不上的地方（如实列出，不藏）

    1. **跨 topic 的全局顺序不一致。** 内存版 drain 是「按 topic 插入顺序，
       把第一个非空队列抽干再看下一个」；RocketMQ 由 broker 决定先给哪个队列。
       **同一个 topic 内**两边逐条一致（前提是 topic 单队列，见 create-topics.sh）。
    2. **重投计数的粒度不同。** 内存版按 ``(topic, group, event_id)`` 计数，
       每个订阅组各算各的；RocketMQ 的 ``delivery_attempt`` 是**每条消息**一个数，
       与订阅组无关。一个 topic 上挂多个 group 且只有部分 group 失败时，两边会分叉。
       MAOS 现在每个 topic 只挂一个 group，碰不到这条。
    3. **消费位点是 broker 上的持久状态。** 内存版每次新建就是干净的；同名消费者组
       连上同一个 broker 会接着上次的位点走。测试因此每次用新的随机组名。

    ## 凭证

    AK/SK 只从环境变量读（铁律 6），不落任何文件，也不进日志。本地 broker 不需要，
    留空即可。
    """

    def __init__(
        self,
        *,
        endpoints: str | None = None,
        group: str | None = None,
        max_redelivery: int = 2,
        await_seconds: int | None = None,
        invisible_seconds: int | None = None,
        empty_rounds: int | None = None,
        nack_grace: int | None = None,
        topic_prefix: str | None = None,
        drain_dead_letters: bool = True,
    ) -> None:
        self.endpoints = endpoints or os.environ.get(ENDPOINTS_ENV) or DEFAULT_ENDPOINTS
        self.group = group or os.environ.get(GROUP_ENV) or DEFAULT_GROUP
        self.topic_prefix = (
            topic_prefix if topic_prefix is not None
            else os.environ.get(TOPIC_PREFIX_ENV, "")
        )
        self._max_redelivery = max_redelivery
        self._await = _int_env(AWAIT_SECONDS_ENV, DEFAULT_AWAIT_SECONDS, await_seconds)
        self._invisible = _int_env(
            INVISIBLE_SECONDS_ENV, DEFAULT_INVISIBLE_SECONDS, invisible_seconds)
        self._empty_rounds = _int_env(
            EMPTY_ROUNDS_ENV, DEFAULT_EMPTY_ROUNDS, empty_rounds)
        self._nack_grace = _int_env(NACK_GRACE_ENV, DEFAULT_NACK_GRACE, nack_grace)
        self._drain_dead_letters = drain_dead_letters

        self._subs: dict[str, list[tuple[str, Handler]]] = defaultdict(list)
        #: 订阅顺序。drain 按这个顺序扫 topic —— 与内存版「按队列插入顺序扫」对齐。
        self._sub_order: list[str] = []
        #: rmq topic 名 -> MAOS topic 名。反查用，顺带守住重名。
        self._topic_back: dict[str, str] = {}
        #: 已经 nack 出去、还没再收回来的消息数。drain 判空时不许无视它。
        self._pending_nacks = 0
        self.dead_letters: list[Envelope] = []

        self._producer = None
        #: MAOS topic -> 该 topic 专属的 SimpleConsumer。一个 topic 一个，见 drain。
        self._consumers: dict[str, object] = {}
        #: 本次 drain 里已经确认空掉的 topic，省掉重复的 5 秒长轮询。
        self._empty_hint: set[str] = set()

    # -- 依赖与连接 -------------------------------------------------------
    @staticmethod
    def _sdk():
        """惰性 import。没装客户端时给一句说得清怎么修的话，而不是 ImportError。"""
        try:
            import rocketmq  # noqa: PLC0415 —— 可选依赖，不许在模块顶层 import
        except ImportError as exc:
            raise RocketMQBackendUnavailable(
                "没装 RocketMQ 客户端。装：pip install rocketmq-python-client"
                "（5.x 的 gRPC 客户端；别装 4.x 的 rocketmq-client-python，"
                "那个要 librocketmq C++ SDK，Apple Silicon 上编不过）。"
            ) from exc
        return rocketmq

    def _config(self):
        rocketmq = self._sdk()
        # 铁律 6：AK/SK 只从环境变量读，且下面任何日志/异常都不许回显它们。
        creds = rocketmq.Credentials(
            os.environ.get(ACCESS_KEY_ENV, ""), os.environ.get(SECRET_KEY_ENV, ""))
        return rocketmq.ClientConfiguration(
            self.endpoints, creds, os.environ.get(NAMESPACE_ENV, ""),
            request_timeout=10)

    def _rmq(self, topic: str) -> str:
        """MAOS topic -> RocketMQ topic，顺带守住「两个 MAOS topic 撞成同一个」。"""
        name = rmq_topic_name(topic, self.topic_prefix)
        known = self._topic_back.get(name)
        if known is not None and known != topic:
            raise ValueError(
                f"topic 名映射撞车：{topic!r} 与 {known!r} 都映射成 {name!r}。"
                " RocketMQ 不允许点号，本层把非法字符换成下划线（见 rmq_topic_name）——"
                " 请把其中一个改名。"
            )
        self._topic_back[name] = topic
        return name

    def _get_producer(self):
        if self._producer is None:
            rocketmq = self._sdk()
            try:
                producer = rocketmq.Producer(self._config())
                producer.startup()
            except Exception as exc:                    # noqa: BLE001
                raise RocketMQBackendUnavailable(
                    f"连不上 RocketMQ proxy {self.endpoints}：{exc}。"
                    f" 起 broker：docker compose -f deploy/rocketmq/docker-compose.yml"
                    f" up -d --wait"
                ) from exc
            self._producer = producer
        return self._producer

    def _get_consumer(self, topic: str):
        """每个 topic 一个 SimpleConsumer。

        🔴 **为什么不能一个 consumer 订阅多个 topic** —— 这条是实测踩出来的：
        一个 SimpleConsumer 订阅两个 topic 时，``receive`` 在两个 topic 之间**严格
        轮转**，每次只从其中一个取。只有一个 topic 有消息时，实测序列长这样：

            #0 空返回 4.98s   #1 取到 1 条 0.01s   #2 空返回 4.96s   #3 取到 1 条 ...

        那些空返回是轮到空 topic 的那几次。于是「一次空返回」根本不等于「队列全空」——
        第一版 drain 就是这么把 7 条测试判错的：purge 只吃了一条就以为清完了。

        一个 topic 一个 consumer 之后，「空」的判据回到确定的形式：
        **每个 topic 各自长轮询空返回一次**，才算全空。
        """
        consumer = self._consumers.get(topic)
        if consumer is not None:
            return consumer
        rocketmq = self._sdk()
        name = self._rmq(topic)
        try:
            consumer = rocketmq.SimpleConsumer(
                self._config(), self.group,
                subscription={name: rocketmq.FilterExpression("*")},
                await_duration=self._await)
            consumer.startup()
        except Exception as exc:                        # noqa: BLE001
            raise RocketMQBackendUnavailable(
                f"消费者起不来（endpoints={self.endpoints} group={self.group}"
                f" topic={name}）：{exc}。"
                f" topic 建了吗？bash deploy/rocketmq/create-topics.sh"
            ) from exc
        self._consumers[topic] = consumer
        return consumer

    # -- EventBus 三方法（签名逐字对齐上面的 ABC）--------------------------
    def publish(self, topic: str, env: Envelope) -> None:
        rocketmq = self._sdk()
        name = self._rmq(topic)
        log.debug("publish %s -> %s (%s)", env.event_type, topic, env.idempotency_key)
        msg = rocketmq.Message()
        msg.topic = name
        # 契约原话「Envelope 直接序列化成消息体，字段一一对应」—— 这里就是那一行。
        msg.body = env.to_json().encode("utf-8")
        msg.tag = env.event_type
        # keys 是 RocketMQ 控制台按业务键查消息用的。放幂等键，排查时能直接搜。
        msg.keys = env.idempotency_key
        try:
            self._get_producer().send(msg)
        except RocketMQBackendUnavailable:
            raise
        except Exception as exc:                        # noqa: BLE001
            raise RocketMQBackendUnavailable(
                f"投递失败 topic={name}：{exc}。"
                f" topic 建了吗？bash deploy/rocketmq/create-topics.sh"
                f"（RocketMQ 不允许 topic 名带点号，本层已把 {topic!r} 映射成 {name!r}）"
            ) from exc
        # handler 在 drain 途中往一个「已经确认空了」的 topic 投递时，
        # 必须把那条空结论作废 —— 否则这条消息要等到下一次 drain 才被看见，
        # 而内存版是当轮就能看见的。
        self._empty_hint.discard(topic)

    def subscribe(self, topic: str, group: str, handler: Handler) -> None:
        if topic not in self._subs:
            self._sub_order.append(topic)
        self._subs[topic].append((group, handler))
        self._rmq(topic)                                # 顺带做一次重名校验

    def _drain_order(self) -> list[str]:
        """本次 drain 要扫哪些 topic，按什么顺序。

        死信 topic **只在真的产生过死信之后**才进扫描队列 —— 内存版的 ``_queues``
        是 defaultdict，``Topic.DEAD_LETTER`` 那个键在第一条死信出现之前根本不存在，
        drain 自然也扫不到它。这里逐条对齐那个行为：无条件扫 DLQ 的话，每次 drain
        都要多付一个 5 秒的空轮询，而且会把 broker 上遗留的历史死信也吃进来。
        """
        order = list(self._sub_order)
        dlq = Topic.DEAD_LETTER
        if self._drain_dead_letters and self.dead_letters and dlq not in order:
            order.append(dlq)
        return order

    def drain(self, max_rounds: int = 1000) -> int:
        """把 broker 上属于本消费者组的消息跑到空。返回处理的消息数。

        扫描顺序与内存版一字不差：**按 topic 顺序找第一个非空的，取一条，
        然后从头重扫**。内存版的「非空」是 ``len(queue) > 0``，这里是「长轮询取到了」。

        ## 「空」的判据，以及它为什么不会把「还没到」误判成「没有」

        判据是：**每个 topic 各自长轮询空返回一次**（连续 EMPTY_ROUNDS 整轮全空），
        **且**没有已知的在途 nack。

        长轮询空返回**不是客户端 sleep**，是 broker 侧的回答。三条实测支撑它：

        1. **同步 send 返回 = broker 已持久化。** 所以 drain 开始那一刻，此前所有
           publish 出去的消息都已经在 broker 上了，不存在「还在路上」。handler 里
           再 publish 的也一样：send 是同步的，handler 返回前消息已经落到 broker
           （那种情况还会作废该 topic 的空结论，见 publish 末尾）。
        2. **有消息时长轮询立刻返回。** 实测：发一条后 receive 耗时 3~10 毫秒。
           它不是等满窗口才回，是 broker 一有就给。
        3. **空返回才等满窗口。** 实测恒定 4.97s（窗口有 5 秒下限，传 1 或 3 都是
           4.97s，传 10 才真等 10s）。所以一次空返回 = broker 在整整 5 秒里确实没有。

        两处能绕过这三条的，都不是靠等解决的，而是靠**已知的未完成计数**：

        * **在途重投**：handler 抛异常后本层 nack，消息要下一轮才回得来（实测 4.96s，
          正好一个窗口）。这一刻队列在 broker 眼里是空的，但「还没到」是真的。
          所以只要 ``_pending_nacks`` 非零，判空门槛就抬高 ``NACK_GRACE`` 轮。
        * **多 topic**：见 ``_get_consumer`` 的长注释 —— 一个 consumer 订阅多 topic 时
          ``receive`` 会轮转，空返回可能只是「轮到了另一个空 topic」。所以改成
          一个 topic 一个 consumer，判空必须每个 topic 各自空一次。

        没有走固定 sleep：真有消息时这个循环是毫秒级的，只有真空了才付那 5 秒，
        而且每个 topic 每次 drain 只付一次（``_empty_hint`` 记着已经空掉的）。

        ``max_rounds`` 与内存版同义：一轮一条消息，防的是状态机写错导致事件互相
        无限触发。超了就抛，让你看见环在哪，而不是挂死。
        """
        if not self._drain_order():                     # 一个订阅都没有，无事可做
            return 0

        processed = 0
        empty_scans = 0
        self._empty_hint = set()
        for _ in range(max_rounds):
            picked: tuple[str, object] | None = None
            for topic in self._drain_order():
                if topic in self._empty_hint:
                    continue                            # 本次 drain 里已确认空
                view = self._receive_one(topic)
                if view is None:
                    self._empty_hint.add(topic)
                    continue
                picked = (topic, view)
                break

            if picked is None:
                empty_scans += 1
                threshold = self._empty_rounds + (
                    self._nack_grace if self._pending_nacks else 0)
                if empty_scans >= threshold:
                    if self._pending_nacks:
                        # 等过 NACK_GRACE 轮还没回来：如实说一声再退出，
                        # 别假装干净收敛（也别无限等下去）。
                        log.warning(
                            "drain 退出时仍有 %d 条在途重投未回收（已等 %d 轮全空）",
                            self._pending_nacks, empty_scans)
                    return processed
                self._empty_hint.clear()                # 再扫一整轮
                continue

            empty_scans = 0
            self._dispatch(picked[0], picked[1])
            processed += 1
        raise RuntimeError(
            f"drain 超过 {max_rounds} 轮仍未收敛 —— 大概率是状态机里有事件环路"
        )

    def _receive_one(self, topic: str):
        """从一个 topic 长轮询取一条。取不到返回 None。"""
        consumer = self._get_consumer(topic)
        try:
            batch = consumer.receive(1, self._invisible)
        except Exception as exc:                        # noqa: BLE001
            raise RocketMQBackendUnavailable(
                f"receive 失败（endpoints={self.endpoints} group={self.group}"
                f" topic={topic}）：{exc}"
            ) from exc
        return batch[0] if batch else None

    # -- 派发（逐条对齐 InMemoryEventBus.drain 的那段）---------------------
    def _dispatch(self, topic: str, view) -> None:
        consumer = self._consumers[topic]
        env = Envelope.from_json(view.body.decode("utf-8"))
        attempt = int(getattr(view, "delivery_attempt", 1) or 1)
        if attempt > 1:
            self._pending_nacks = max(0, self._pending_nacks - 1)

        failed = False
        for group, handler in self._subs.get(topic, []):
            try:
                handler(env)
            except Exception as exc:                    # noqa: BLE001 —— 等价于 nack
                failed = True
                # 内存版按 (topic, group, event_id) 本地计数；这里用 broker 给的
                # delivery_attempt。门槛写成一样：attempt > max_redelivery 才进死信，
                # 于是 handler 被调用的次数两边完全相同（见类 docstring 第 2 条差异）。
                if attempt > self._max_redelivery:
                    log.error("进入死信: %s (%s)", env.idempotency_key, exc)
                    self.dead_letters.append(env)
                    self.publish(Topic.DEAD_LETTER, env)
                else:
                    log.warning("重投 %s 第 %d 次: %s",
                                env.idempotency_key, attempt, exc)

        if not failed or attempt > self._max_redelivery:
            consumer.ack(view)                          # 成功，或已经进了死信
        else:
            # nack：把不可见期改成 0，让它立刻可以再投。
            consumer.change_invisible_duration(view, 0)
            self._pending_nacks += 1
            self._empty_hint.discard(topic)             # 它还会回来，别当这个 topic 空了

    # -- 收尾 -------------------------------------------------------------
    def close(self) -> None:
        """关掉 producer 与全部 consumer。不在 ABC 里，是本类自己的收尾口。"""
        clients = list(self._consumers.values())
        self._consumers = {}
        if self._producer is not None:
            clients.append(self._producer)
            self._producer = None
        for client in clients:
            try:
                client.shutdown()
            except Exception as exc:                    # noqa: BLE001 —— 关不掉无所谓
                log.debug("客户端关闭异常（已忽略）：%s", exc)


def _int_env(env_name: str, default: int, explicit: int | None = None) -> int:
    """显式传参优先，其次环境变量，再次缺省。拼错的值直接抛，不静默回落。"""
    if explicit is not None:
        return explicit
    raw = os.environ.get(env_name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{env_name}={raw!r} 不是整数") from exc


def create_event_bus(*, backend: str | None = None, **kwargs) -> EventBus:
    """按后端名造一个 EventBus。缺省 memory —— 与今天逐字节一致。

    `backend` 显式传就用传的，否则读 `MAOS_EVENTBUS_BACKEND`，再否则 `memory`。

        export MAOS_EVENTBUS_BACKEND=rocketmq
        export MAOS_ROCKETMQ_ENDPOINTS=localhost:8081     # proxy 的 gRPC 口

    🔴 **未设环境变量时这个函数返回的就是 InMemoryEventBus。** 本仓库「裸 clone 无需
    任何 key 跑到 7/7」的卖点不许因为多了一个 MQ 后端而作废；RocketMQ 是可选项，
    不是前提。

    后端名只认 `memory` 与 `rocketmq` 两个字面量（大小写不敏感，前后空白忽略）。
    别的一律抛 ValueError，**不回落缺省** —— 拼错一个字母就静默跑内存版，比直接
    报错难查一个量级：你会以为在验 RocketMQ，其实一条消息都没进 broker。
    这条口径抄的是 `maos.store.create_store`，不是本轨的发明。
    """
    name = (backend if backend is not None else os.environ.get(BACKEND_ENV)
            or DEFAULT_BACKEND)
    name = name.strip().lower()

    if name == MEMORY:
        return InMemoryEventBus(**kwargs)
    if name == ROCKETMQ:
        return RocketMQEventBus(**kwargs)
    raise ValueError(
        f"未知的 {BACKEND_ENV}={name!r}：只认 {MEMORY!r} 或 {ROCKETMQ!r}。"
        f" 不回落缺省 —— 拼错一个字母就静默跑内存版，比报错难查得多。"
    )
