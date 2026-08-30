# RocketMQ 真 broker —— 实跑记录

这份文档只回答一个问题：**「换 RocketMQ 时上层代码不用改」这句话，在一台真的
RocketMQ broker 上，到底哪几条兑现了、哪几条没有。**

它存在的理由是：仓库里那三处声明（`maos/core/eventbus.py` 的模块 docstring、
`maos/contracts/events.py` 第 4 行、`maos/flows/common.py:104`）此前都只是**设计
声明**。声明不是实证。本文件负责把它们换成一条一条可复核的记录，**包括没兑现的**。

## 与 `deploy/rocketmq.md` 的分工（别把两份文档混起来）

| 文件 | 回答什么 | 天花板 |
| :-- | :-- | :-- |
| [`deploy/rocketmq.md`](rocketmq.md) | MAOS 的 RocketMQ 后端**怎么接**、怎么配、怎么降级 | 本机 Docker `apache/rocketmq:5.3.2` |
| **本文件** | **真 broker 上，等价性实际跑通了哪几条** | 同上；**没连过阿里云 RocketMQ 实例** |

🔴 **一条硬纪律：本文件里「本机 Docker broker 跑通」和「云上托管 RocketMQ 跑通」
是两件事。** 后者本轨**一次都没跑过**，见 §2.1。前者只能证明协议与语义没问题，
证明不了云上那台实例行不行。

---

## 一、已实测

环境：macOS 15 (Darwin 25.5.0) / Apple Silicon，Docker 29.6.1，2026-08-30。

```
镜像      apache/rocketmq:5.3.2   arch=arm64  os=linux  size=193509756
broker    V5_3_2   DefaultCluster / broker-a / rmqbroker:10911
客户端    rocketmq-python-client 5.1.1
          grpcio 1.83.1 / protobuf 7.36.0 / opentelemetry-sdk 1.44.0
```

### 1.1 三容器起得来 ✅

```
$ docker compose -f deploy/rocketmq/docker-compose.yml up -d --wait
 Container rmqnamesrv Healthy
 Container rmqbroker Healthy
 Container rmqproxy Healthy
up --wait exit=0  耗时=28秒

$ docker compose -f deploy/rocketmq/docker-compose.yml ps
rmqbroker    Up (healthy)  0.0.0.0:10909->10909/tcp, 0.0.0.0:10911-10912->10911-10912/tcp
rmqnamesrv   Up (healthy)  0.0.0.0:9876->9876/tcp
rmqproxy     Up (healthy)  0.0.0.0:8081->8081/tcp, 0.0.0.0:8090->8080/tcp
```

28 秒是从 `down -v` 起算到三个都 healthy（镜像已在本地）。复用卷再起是 9 秒。

### 1.2 建 topic ✅

```
$ bash deploy/rocketmq/create-topics.sh
  ok    maos_task_assignment
  ok    maos_task_result
  ok    maos_review_verdict
  ok    maos_task_rework
  ok    maos_dlq
  ok    t27_bus_a
  ok    t27_bus_b
exit=0
```

每条都用 `mqadmin topicRoute` 回查过 —— 不是看 `updateTopic` 的退出码，理由见 §3.1。

### 1.3 客户端行为：四个问题，四条实测答案

| 问题 | 实测答案 |
| :-- | :-- |
| Q1 全新消费者组看不看得见「它启动之前」发的消息 | **看得见**（从最早位点开始） |
| Q2 单队列下顺序是不是逐条对得上 | **是**，8 条投递顺序 = 消费顺序 |
| Q3 `delivery_attempt` 随重投怎么涨 | **1 → 2 → 3 → 4**，由 broker 给 |
| Q4 长轮询空返回耗多久 | **恒定 4.97s**，且有 5 秒下限 |

Q2 原始输出：

```
投递用到的 queue_id 集合：{'broker-a.t27_equiv_a.0'}
投递顺序：['m00', 'm01', 'm02', 'm03', 'm04', 'm05', 'm06', 'm07']
消费顺序：['m00', 'm01', 'm02', 'm03', 'm04', 'm05', 'm06', 'm07']
Q2 结论：逐条一致 = True
```

Q4 —— **窗口下限是 5 秒，传更小的值没用**：

```
await_duration | 空返回耗时（3 次）
       1s      | 4.97s  4.96s  4.97s
       3s      | 4.97s  4.97s  4.97s
      10s      | 9.97s  9.96s  9.97s
      20s      | 19.98s  19.97s  19.98s
```

**有消息时长轮询立刻返回**，这条是 drain 判据成立的关键：

```
=== 有消息时的返回延迟（await_duration=1）===
  发 1 条后 receive: 1 条，耗时 0.010s
  发 1 条后 receive: 1 条，耗时 0.005s
  发 1 条后 receive: 1 条，耗时 0.004s
```

nack 之后要等一个窗口才回得来 —— 所以 drain 必须显式记着在途重投：

```
=== nack(invisible=0) 之后多久能再收到 ===
  首投 attempt=1
  第 0 次 receive 拿回来了 attempt=2，距 nack 4.962s
```

### 1.4 等价性：三条判据的实跑结果

```
$ .../venv-rmq/bin/python -m pytest maos/tests/test_eventbus_rocketmq.py -q
.................                                                        [100%]
17 passed in 116.69s (0:01:56)
```

无 broker 的同一份文件（系统 python3，未装客户端）：

```
$ python3 -m pytest maos/tests/test_eventbus_rocketmq.py -q
.........ssssssss                                                        [100%]
9 passed, 8 skipped in 0.10s
```

逐条对照：

| §5.3 判据 | 实测结果 |
| :-- | :-- |
| 调用序列（单 topic 8 条） | ✅ 逐条一致 |
| 调用序列（双 topic 各 4 条） | ✅ 逐条一致，**含全局顺序**（有前提，见 §2.2） |
| Envelope 往返 | ✅ `to_json()` 逐字节相同 |
| 幂等：同一 key 投 3 次 | ✅ 两边都是 `(迁移 1 次, 处理 3 条)` |
| 死信：handler 恒抛 | ✅ 两边都是 handler 调 **3 次**、死信集合 `['assign:t1:1']` |
| `drain()` 返回的条数 | ✅ 两边相同 |

单 topic 原始输出：

```
=== 单 topic 顺序对比（8 条）===
  内存版     ['s0','s1','s2','s3','s4','s5','s6','s7']  drain=8 耗时=0.00s
  RocketMQ  ['s0','s1','s2','s3','s4','s5','s6','s7']  drain=8 耗时=5.03s
  逐条一致 ? True
```

双 topic（A/B 交替投，每 topic 4 条）：

```
=== 跨 topic 全局顺序对比 ===
  内存版     消费顺序 ['a','a','a','a','b','b','b','b']
  内存版     键序列   ['a0','a1','a2','a3','b0','b1','b2','b3']
  RocketMQ  消费顺序 ['a','a','a','a','b','b','b','b']
  RocketMQ  键序列   ['a0','a1','a2','a3','b0','b1','b2','b3']

  全局顺序逐条一致 ? True
  集合一致          ? True
  topic a 内部顺序一致 ? True
  topic b 内部顺序一致 ? True
  purge耗时=10.33s  本次drain耗时=10.01s（内存版 0.00s）
```

### 1.5 缺省路径没被动过 ✅

这是本轨的红线，单列一节。

```
$ python3 -m pytest maos/tests -q
944 passed, 37 skipped in 17.81s          # 基线 935 passed / 29 skipped + 本轨 9/8
pytest_exit=0
$ python3 run.py > /dev/null; echo $?
0
$ python3 scripts/gen_docs.py --check; echo $?
0
$ git diff --stat maos/contracts/
（空）
```

有 broker + 有客户端的隔离 venv 下跑全量：

```
$ .../venv-rmq/bin/python -m pytest maos/tests -q
951 passed, 30 skipped in 137.50s (0:02:17)
```

⚠️ **951/30 与 944/37 的差不全是本轨带来的。** 本轨在有 broker 下多 8 条 passed
（8 条从 skip 转 pass），但那个隔离 venv 里没装 `psycopg`，所以
`maos/tests/test_kb_port.py:193`（「没装驱动时这条无从验证」）在那边多 skip 了一条。
逐条 diff 过两边的 skip 清单，**差异只有这一条**，与本轨无关。

---

## 二、未实测 / 没兑现的

### 2.1 🔴 云上托管 RocketMQ 实例 —— 一次都没连过

全部实测跑在**本机 Docker** 上。阿里云 RocketMQ 5.x 实例的这些面一条没验：

- 实例接入点与 **AK/SK 鉴权**（本地 broker 不要凭证，`Credentials("","")` 走通的）
- **命名空间**（`MAOS_ROCKETMQ_NAMESPACE` 这个旋钮**从没在真实例上生效过**）
- 公网 / VPC 内网接入、白名单
- 云上是否同样禁止 topic 名带点号（本地是禁止的，云上**推断**一致，未验）
- 云上默认队列数（本地靠 `-r 1 -w 1` 钉成 1，云上控制台建的 topic 缺省不是 1，
  而**顺序一致性依赖单队列**，见 §3.3）

这一栏与 `deploy/polardb-live.md` 的处境正好相反：那一轨连过真 PolarDB 实例，
本轨**只有对照组，没有云上组**。别把本页的绿当成云上的绿。

### 2.2 全局顺序「一致」是有前提的 —— 前提不成立时会分叉

§1.4 那个 `全局顺序逐条一致 ? True` 不是无条件的。

内存版 `drain` 扫的是 `_queues` 的插入顺序（= **首次 publish 的顺序**）；
本层扫的是 **subscribe 的顺序**。§1.4 那组里两者恰好相同，所以对上了。
故意让它们不同 —— 先订阅 B 再订阅 A、但先往 A 投：

```
=== 订阅顺序 B,A 而投递顺序 A 先 ===
  内存版     ['a0', 'a1', 'a2', 'b0', 'b1', 'b2']
  RocketMQ  ['b0', 'b1', 'b2', 'a0', 'a1', 'a2']

  全局顺序逐条一致 ? False
  集合一致          ? True
  topic a 内部顺序一致 ? True
  topic b 内部顺序一致 ? True
```

**这一条没对上，如实记在这里。** 判断：不去调判据凑绿，因为

1. 集合一致与 topic 内顺序一致在**任何**情况下都成立，这两条才是「换 MQ 不改上层」
   真正依赖的性质 —— 每个 topic 有各自的消费者，跨 topic 的全局先后本来就不是
   MAOS 依赖的东西；
2. 要凑绿就得让本层去猜「哪个 topic 先被 publish 过」，那是把内存版的实现细节
   （defaultdict 的插入顺序）当成契约来抄，比不一致更糟。

所以 `maos/tests/test_eventbus_rocketmq.py::test_two_topics_same_multiset_and_per_topic_order`
断言的是集合 + topic 内顺序，**不断言全局顺序**，并在 docstring 里指回本节。

### 2.3 还没验的其它面

- **并发**：全程单线程单消费者。多进程同组分摊、rebalance 一条没测。
- **`PushConsumer`**：只用了 `SimpleConsumer`。常驻消费者（`flows/common.py` 注释里
  说「换 RocketMQ 后这个循环消失」指的就是它）**没有实现，也没有测**。
  本轨兑现的是「drain 语义等价」，不是「驱动循环真的消失了」。
- **主链路接入**：`create_event_bus()` 造出来的总线**没有接进 `maos/flows/`**
  （那是别轨的面，本轨白名单外）。也就是说七个场景仍然只跑内存版 ——
  本轨证明的是「总线层可替换」，不是「场景已经跑在 RocketMQ 上」。
- 消息体积上限、消息堆积、broker 重启后的位点恢复。
- 事务消息、定时/延时消息、顺序消息（FIFO topic）—— 一个都没用到。

---

## 三、已知差异 / 局限（都是踩出来的）

### 3.1 `mqadmin` 建 topic 失败仍然 `exit 0`

第一版 `create-topics.sh` 拿退出码当判据，结果**全绿而一个 topic 都没建上**：

```
$ docker exec rmqbroker sh mqadmin updateTopic ... -t maos.task.assignment ...
  at org.apache.rocketmq.client.impl.MQClientAPIImpl.createTopic(MQClientAPIImpl.java:480)
  ...
rc=0                                    # ← 报了一屏异常，退出码仍然是 0

$ docker exec rmqbroker sh mqadmin topicList -n namesrv:9876 | grep maos
（无输出）
```

现在脚本逐条回查 `topicRoute`，不信退出码。**这类「失败但退出码为 0」是最难查的
一种**：脚本绿、日志绿，只有第一条业务消息会报一句看起来像网络问题的错。

### 3.2 🔴 RocketMQ 不允许 topic 名带点号 —— 契约里五个 Topic 常量全带点

客户端侧：

```
rocketmq.v5.exception.client_exception.IllegalArgumentException:
topic does not match the regex [regex=re.compile('^[%a-zA-Z0-9_-]+$')].
```

broker 侧同样拒绝（见 §3.1 那段 `createTopic` 异常，用的就是带点的名字）。

契约文件是冻结面，不许为了迁就 MQ 去改它。所以映射做在 `rmq_topic_name`：
非法字符换下划线，`maos.task.assignment -> maos_task_assignment`。

**后果要说清**：契约第 4 行「Envelope 直接序列化成消息体，字段一一对应，不需要
再改」——**消息体那半句实测成立**（往返逐字节相同），**topic 名那半句不成立**。
「换 MQ 零改动」应当收窄成「零改动**上层代码**，总线层需要一个名字映射」。
这是本轨最重要的一处口径修正。

### 3.3 `autoCreateTopicEnable` 对 gRPC 客户端不生效

`broker.conf` 里写着 `autoCreateTopicEnable = true`，但 5.x 的 Python 客户端发消息前
先拉路由，路由不存在就直接抛：

```
Exception: failed to fetch topic:t27_spike_61efcd route.
```

不会触发自动建 topic。所以 `create-topics.sh` 是**必须步骤**，不是便利脚本。

### 3.4 🔴 一个 consumer 订阅多个 topic 时，`receive` 是**轮转**的

这条是本轨最贵的一个坑，直接把 7 条测试判错过一轮。

第一版实现是「一个 SimpleConsumer 订阅所有 topic，一次空返回 = 队列全空」。
实测两个 topic、只有一个有消息时，receive 序列长这样：

```
  # 0  空返回          耗时 4.98s   <== 此刻 t27_bus_a 上还剩 5 条没取
  # 1  topic=t27_bus_a  耗时 0.01s
  # 2  空返回          耗时 4.96s   <== 此刻 t27_bus_a 上还剩 4 条没取
  # 3  topic=t27_bus_a  耗时 0.01s
  # 4  空返回          耗时 4.98s   <== 此刻 t27_bus_a 上还剩 3 条没取
```

严格轮转，空返回只是「轮到了另一个空 topic」。于是「一次空返回」根本不等于
「全空」，drain 每次只吃一条就返回，测试的 purge 阶段清不干净、后面全乱。

**改法**：一个 topic 一个 SimpleConsumer，drain 按 topic 顺序扫 ——
这恰好也是内存版 `drain` 的语义（挑第一个非空队列、弹一条、从头重扫）。
判空回到确定形式：**每个 topic 各自长轮询空返回一次**。
代价是每个 topic 每次 drain 付一个 5 秒窗口（见 §3.6）。

一个走过的弯路也记在这：先怀疑是「消费者刚 startup 时还没就绪」，
实测**推翻**了 —— 全新组的第一次 receive 就是 0.003s 拿到消息，没有预热空返回。

### 3.5 两条只在真 broker 上现形的测试缺陷

内存版每次是干净的，broker 带着往次历史。两条都是**测试自己的**问题，不是实现问题，
但只有连上真 broker 才会现形：

1. **探针的宽 `except Exception` 把自己的 bug 吞了。** 第一版 `_live_endpoints()`
   调用了定义在它下面的 `_envelope`，而它在模块导入期就执行 → `NameError` →
   被 `except Exception` 一并吞掉 → 返回 None → **8 条测试静默全 skip、通篇绿灯**。
   「探测不到就 skip」这种结构天生看不出这种失败。现在只吞
   `RocketMQBackendUnavailable`：把「后端没准备好」和「探针写错了」分开。
2. **只重置计数器、不重置去重集合。** 幂等测试的 handler 在 purge 阶段吃到了
   历史消息（前一条测试投的 `assign:t1:1`），key 进了去重集合，正式投的三条全被
   判成「见过」，迁移数成 0。现在 `IdempotentHandler.reset()` 把两样一起清，
   幂等键也改成每次跑随机。

第 1 条值得单独记：**在「探测不到就 skip」的测试结构里，宽口径的 except 是有害的**，
它让「一个都没跑」和「全都跑过了」在输出上完全一样。

### 3.6 drain 的地板成本：每个 topic 每次 5 秒

| | 内存版 | RocketMQ |
| :-- | :-- | :-- |
| 单 topic drain | 0.00s | **5.03s** |
| 双 topic drain | 0.00s | **10.01s** |

来源是 §1.3 Q4：长轮询窗口有 5 秒下限，而「空」在 broker 上没有即时信号。
有消息时不付这个钱（receive 3~10ms），慢的只是「确认真的空了」这一下。

`maos/flows/common.py` 的驱动循环每轮 drain 两次，所以七个场景真跑在 RocketMQ 上
的地板成本会是 `轮数 × 2 × 5s × topic 数` —— 这也是为什么 §2.3 说「驱动循环消失」
要靠 `PushConsumer` 而不是靠 drain，本轨没做那一步。

### 3.7 死信没走 broker 的 `%DLQ%`

RocketMQ 自带 `%DLQ%<group>`。本层没用它，而是在 `delivery_attempt > max_redelivery`
时自己投进 `Topic.DEAD_LETTER` 并记进 `dead_letters` —— 这样才和内存版的死信语义
逐条对得上，等价性证明才成立。代价是 broker 控制台的死信视图看不到这些消息。

**这是一个刻意的取舍，不是遗漏**：用 broker 的 DLQ 会更「原生」，但那样两个后端的
死信集合就没法逐条比对了，而本轨的全部价值就在那个比对上。

---

## 四、复跑方式

```bash
docker compose -f deploy/rocketmq/docker-compose.yml up -d --wait
bash deploy/rocketmq/create-topics.sh

python3 -m venv /tmp/venv-rmq                     # 隔离 venv，不污染系统解释器
/tmp/venv-rmq/bin/pip install rocketmq-python-client pytest
/tmp/venv-rmq/bin/python -m pytest maos/tests/test_eventbus_rocketmq.py -q

docker compose -f deploy/rocketmq/docker-compose.yml down -v
```

无 broker 那一份（评委的环境）不需要任何准备：

```bash
python3 -m pytest maos/tests -q      # 新增测试自动 skip，944 passed / 37 skipped
python3 run.py                       # 7/7 PASS，不受影响
```

⚠️ 靶 topic 上会累积往次消息（消费位点是 broker 状态，见 `rocketmq.md` 局限 5）。
测试每次用随机消费者组 + 先 drain 一遍吃掉历史，所以重复跑是安全的；
要彻底干净就 `down -v`，或者删掉 `t27_bus_a` / `t27_bus_b` 再跑建 topic 脚本。

---

## 五、安全声明（铁律 6 / 7）

- 本页与 `deploy/rocketmq.md` **不含任何真实 AK/SK、接入点或实例 ID**。
  本地 broker 全程不需要凭证（`Credentials("", "")`）。
- 凭证只从 `MAOS_ROCKETMQ_AK` / `MAOS_ROCKETMQ_SK` 读，不落文件、不进日志、
  不进异常消息 —— `RocketMQBackendUnavailable` 的报错里只回显 endpoints 与
  consumer group，两者都不是秘密。
- 本页所有代码块都来自真实命令输出，没有手写或编造的数字。
