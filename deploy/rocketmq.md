# 从内存 EventBus 换到 RocketMQ

仓库里有三处写着「换 RocketMQ 时上层代码不用改」：`maos/core/eventbus.py` 的模块
docstring、`maos/contracts/events.py` 第 4 行、`maos/flows/common.py:104`。
这一页负责说清**怎么接**；那三句话到底兑现了几条，在
[`deploy/rocketmq-live.md`](rocketmq-live.md) 里逐条记着，**含没兑现的**。

> 🔴 **本页不含任何真实 AK/SK 或云上接入点。** 一律写成 `<ak>` / `<endpoint>` 这种
> 占位形式。凭证只从环境变量 `MAOS_ROCKETMQ_AK` / `MAOS_ROCKETMQ_SK` 读，
> 禁止落进任何文件（铁律 6）。本地 broker 不需要凭证，留空即可。

---

## 🔴 先读这条：RocketMQ 是可选项，不是前提

**`MAOS_EVENTBUS_BACKEND` 未设 = `InMemoryEventBus`，行为与今天逐字节一致。**

本仓库最大的卖点是「无需任何 API key、裸 clone 跑到 8/8 PASS」，那正是复赛 30%
维度红线「无法在合理环境中复现」的正面防守。RocketMQ 一旦变成跑通场景的前提，
这个卖点当场作废，得不偿失。所以：

| | 口径 |
| :-- | :-- |
| 缺省后端 | `InMemoryEventBus`，一个字节不变 |
| 客户端依赖 | **不进 `pyproject.toml`**，只在本页「可选依赖」一节 |
| 新增测试 | 探测不到 broker 就整个 skip，**绝不红** |
| 没装 / 连不上 | 抛 `RocketMQBackendUnavailable`，**绝不回落内存版** |

最后一条与 `maos.store` 的 `PgBackendUnavailable` 是同一条口径：回落的后果是
「RocketMQ 后端看起来跑通了」而其实一条消息都没进 broker —— 等真上生产那天，
所有以为验过的路径都得重验，且没有任何东西提示你该重验。

---

## 接入三步

### 第 1 步 · 起 broker

**三个服务，缺一不可**：namesrv（路由注册）、broker（存消息）、proxy（gRPC 接入层）。

```bash
docker compose -f deploy/rocketmq/docker-compose.yml up -d --wait
```

🔴 **5.x 的 Python 客户端连的是 proxy 的 8081，不是 namesrv 的 9876。** 这是配错
最多的一处：写成 9876 会连上一个说 remoting 协议的端口，症状是 gRPC 握手超时，
不是「拒绝连接」，很容易误判成网络故障。

实测本机从 `down -v` 到三个容器全 healthy 用 **28 秒**（镜像已在本地，`--wait` exit=0）。

### 第 2 步 · 建 topic

```bash
bash deploy/rocketmq/create-topics.sh
```

🔴 **这一步不能省，而且不能靠 `autoCreateTopicEnable`。** 实测：`broker.conf` 里
`autoCreateTopicEnable = true` 对 5.x 的 gRPC 客户端**不生效**，客户端发消息前先拉
路由，路由不存在就直接抛 `failed to fetch topic:<name> route.`，不会触发自动建。

🔴 **RocketMQ 不允许 topic 名里有点号**，而契约里的 Topic 常量全是
`maos.task.assignment` 这种带点的形式。契约是冻结面不许改，所以映射做在总线里
（`rmq_topic_name`）：非法字符一律换成下划线。脚本建的是映射之后的名字。

```
maos.task.assignment  ->  maos_task_assignment
maos.dlq              ->  maos_dlq
```

### 第 3 步 · 换环境变量

```bash
export MAOS_EVENTBUS_BACKEND=rocketmq
export MAOS_ROCKETMQ_ENDPOINTS=localhost:8081     # proxy 的 gRPC 口
```

**上层代码一行都不用改** —— `create_event_bus()` 按这两个变量选后端，
`RocketMQEventBus` 的三个方法签名与 `EventBus` ABC 逐字对齐。

全部旋钮：

| 环境变量 | 缺省 | 作用 |
| :-- | :-- | :-- |
| `MAOS_EVENTBUS_BACKEND` | `memory` | `memory` / `rocketmq`。别的值抛，不回落 |
| `MAOS_ROCKETMQ_ENDPOINTS` | `localhost:8081` | proxy 的 gRPC 接入点 |
| `MAOS_ROCKETMQ_GROUP` | `maos_default` | 消费者组 |
| `MAOS_ROCKETMQ_AK` / `_SK` | 空 | 凭证。只读环境变量（铁律 6），本地 broker 不需要 |
| `MAOS_ROCKETMQ_NAMESPACE` | 空 | 云上实例的命名空间 |
| `MAOS_ROCKETMQ_TOPIC_PREFIX` | 空 | topic 前缀，多环境共用一个实例时隔离用 |
| `MAOS_ROCKETMQ_AWAIT_SECONDS` | `1` | 长轮询窗口。**实测有 5 秒下限**，见下 |
| `MAOS_ROCKETMQ_INVISIBLE_SECONDS` | `30` | 取走后的不可见期 |
| `MAOS_ROCKETMQ_EMPTY_ROUNDS` | `1` | 判空要连续多少次「整轮全空」 |
| `MAOS_ROCKETMQ_NACK_GRACE` | `2` | 有在途重投时，判空门槛额外抬高几轮 |

数值型变量拼错**当场抛**，不静默用缺省 —— 同一条「不回落」口径。

---

## 可选依赖：装哪个包

```bash
pip install rocketmq-python-client        # 5.x 的 gRPC 客户端
```

⚠️ **别装 `rocketmq-client-python`（4.x）。** 那个包依赖 librocketmq C++ SDK，
Apple Silicon 上大概率编不过。两者是不同的包，名字只差一个词序。

实测装的是 **5.1.1**，纯 Python + gRPC，**不需要 C++ SDK，没有编译步骤**。
它会顺带拉进来 `grpcio` / `protobuf` / `opentelemetry-*` 一整套
（实测 `opentelemetry-sdk 1.44.0`、`grpcio 1.83.1`、`protobuf 7.36.0`）。

### 为什么不进主依赖

`pyproject.toml` 的 `dependencies = []` 是**核心零运行时依赖**，与
`psycopg`（PG 后端）是同一条口径。把一个拉 20 多个传递依赖、含 gRPC 与
OpenTelemetry 全家桶的包放进主依赖，等于让每个只想跑 `python3 run.py` 的人
都为一个可选后端付出装包时间和版本冲突风险。

判据很直接：**没装客户端、没起 broker 的机器上，`python3 -m pytest maos/tests -q`
必须照常全绿**（新增测试自动 skip），`python3 run.py` 必须照常七个场景全过、exit=0。

---

## 降级行为：连不上会怎样

| 情况 | 行为 |
| :-- | :-- |
| `MAOS_EVENTBUS_BACKEND` 未设 | 拿到 `InMemoryEventBus`，与今天逐字节一致 |
| 设成 `rocketmq` 但没装客户端 | 抛 `RocketMQBackendUnavailable`，报错里点名装哪个包 |
| 设成 `rocketmq` 但 proxy 连不上 | 抛 `RocketMQBackendUnavailable`，报错里带起 broker 的命令 |
| topic 没建 | 抛 `RocketMQBackendUnavailable`，报错里点名 `create-topics.sh` |
| 后端名拼错 | 抛 `ValueError`，**不回落** |
| handler 抛异常 | = nack，重投；超过 `max_redelivery` 进死信 topic |

**注意最后三行之外，本层不做任何自动降级。** 与 Matrix 总线（连不上就 log-only
继续跑）刻意不同：Matrix 是旁路镜像，掉了不影响主链路；EventBus 是主链路本身，
「悄悄换回内存版」等于让你以为验过了 RocketMQ。

---

## 已实测

环境：本机 Docker `apache/rocketmq:5.3.2`（arm64），客户端 `rocketmq-python-client 5.1.1`，
2026-08-30。逐条实录与没跑通的部分见 [`deploy/rocketmq-live.md`](rocketmq-live.md)。

| 项 | 实测结果 |
| :-- | :-- |
| 三容器启动 | `up -d --wait` exit=0，**28 秒**全 healthy |
| 建 topic | 8/8，逐条回查 `topicRoute` 成功 |
| `publish` / `drain` 往返 | Envelope 过一趟 broker 后**逐字节相同** |
| 调用序列（单 topic，8 条） | 两个后端**逐条一致** |
| 调用序列（双 topic，各 4 条） | 两个后端**逐条一致**（含全局顺序，有前提，见 live §2.2） |
| 幂等（同一 key 投 3 次） | 两边都是「迁移 1 次、处理 3 条」 |
| 死信 | 两边都是 handler 调 3 次、死信集合 `['assign:t1:1']` |
| 本轨测试 | `test_eventbus_rocketmq.py` **17 passed**（有 broker）/ **9 passed, 8 skipped**（无） |
| 全量（无 broker） | **944 passed, 37 skipped**，exit=0 |
| 全量（有 broker） | **951 passed, 30 skipped**，exit=0 |
| `python3 run.py` | exit=0，七个场景不受影响 |

---

## 已知差异 / 局限

### 1. 🔴 topic 名需要一层翻译，「字段一一对应」只对消息体成立

契约第 4 行写的是「换成 RocketMQ 之后，Envelope 直接序列化成消息体，字段一一对应，
不需要再改」。**消息体这半句实测成立**（`to_json()` 往返逐字节相同）；
**topic 名这半句不成立** —— RocketMQ 的 topic 正则是 `^[%a-zA-Z0-9_-]+$`，
点号非法，而契约里五个 Topic 常量全带点。

翻译层做在 `rmq_topic_name` 里，所以**上层调用方仍然一行不用改**，
但「零成本换 MQ」这个说法要收窄成「零改动**上层代码**，总线层需要一个名字映射」。
这是本轨最重要的一处口径修正。

### 2. drain 的成本：每个 topic 每次 drain 付一个 5 秒长轮询

内存版 `drain` 是 0.00s；RocketMQ 版单 topic **5.03s**、双 topic **10.01s**。
原因是「空」在 broker 上没有即时信号，只能靠长轮询空返回来断定，
而**长轮询窗口有 5 秒下限**（传 1 或 3 实测都是 4.97s）。

有消息时不付这个代价：实测 receive 3~10 毫秒返回。所以慢的只是「确认真的空了」
这一下，不是每条消息。`maos/flows/common.py` 的驱动循环每轮 drain 两次，
换成 RocketMQ 后一轮的地板成本是 `2 × 5s × topic 数`。

**这不是固定 sleep。** 判据的完整论证在 `RocketMQEventBus.drain` 的 docstring 里。

### 3. 扫描顺序的口径不同：内存版按首次 publish，本层按 subscribe

内存版 `drain` 扫的是 `_queues` 的插入顺序 —— 也就是**首次 publish 的顺序**；
本层扫的是 **subscribe 的顺序**。两者一致时全局顺序逐条相同（实测），
不一致时会分叉：实测「先订阅 B 再订阅 A、但先往 A 投」这一组，
内存版给 `a,a,a,b,b,b`，RocketMQ 给 `b,b,b,a,a,a`。

**集合一致、每个 topic 内部顺序一致**这两条在任何情况下都成立，
而跨 topic 的全局顺序本来就不是 MAOS 依赖的性质（每个 topic 有各自的消费者）。
详见 live §2.2。

### 4. 重投计数的粒度不同

内存版按 `(topic, group, event_id)` 计数，每个订阅组各算各的；RocketMQ 的
`delivery_attempt` 是**每条消息**一个数，与订阅组无关。一个 topic 上挂多个 group
且只有部分 group 失败时，两边会分叉。MAOS 现在每个 topic 只挂一个 group，
碰不到这条。

### 5. 消费位点是 broker 上的持久状态

内存版每次 `InMemoryEventBus()` 就是干净的；同名消费者组连上同一个 broker
会接着上次的位点走。而**全新消费者组从最早的位点开始**（实测），
所以 broker 上留着的往次消息会被当成本次的输入。

测试因此每次用新的随机组名 + 一次「先 drain 一遍吃掉历史」。真链路要注意的是
反过来那一面：换个组名重启会**从头重放**，不是从当前位置继续。

### 6. 死信走的是本层实现，不是 broker 的 `%DLQ%`

RocketMQ 自己有一套 `%DLQ%<group>` 机制。本层没用它，而是在
`delivery_attempt > max_redelivery` 时自己投进 `Topic.DEAD_LETTER` 并记进
`dead_letters` —— **这样才和内存版的死信语义逐条对得上**，也是等价性证明能成立的
前提。代价是 broker 控制台的死信视图看不到这些消息，要去 `maos_dlq` 这个普通
topic 里看。

### 7. 单队列是等价性的前提，不是缺省

`create-topics.sh` 建 topic 时写死 `-r 1 -w 1`。RocketMQ 普通消息只保证
**单队列内有序**，多队列下同一串 publish 会被散列到不同队列，消费顺序与投递顺序
不一致 —— 那时比的就不是「后端语义」而是「队列数」。生产上要吞吐就得加队列，
**加了之后 topic 内的顺序一致性不再成立**，这条要提前知道。
