# cumora 解析 · 运行时编排与唤醒总线 （T44 · 基线 cumora@1e883f6 / MAOS@926aa7b）

> 面向对象：`server/src/agents/runtime/` 的双客户端契约、`wake-bus.ts` 的唤醒投递、
> `scheduler.ts` 的调度与认领、`orchestrator.ts` 的 pod 生命周期。
> `agenda.ts` 归 T40，`daemon.ts` / `engine.ts` 归 T42，本文读到即止、不下结论。

## 1. 它是怎么做的

**一个接口，两份实现，边界划在「agent 回合需要的全部外部 IO」上。**
`client.ts` 用 391 行只做一件事：把 agent 回合要碰的外部世界收进一个
`AgentRuntimeClient` 接口（`client.ts:193`）。方法分五组 —— 读 agent 状态
（`:194-223`）、身份 prompt（`:225-228`）、状态与在场（`:230-306`）、
可观测三件套 createRun/recordEvent/finishRun（`:308-340`）、忙碌心跳与已读游标
（`:365-390`）。两份实现：`InProcRuntimeClient` 直连 Postgres + Redis
（`inproc-client.ts:126`），`HttpRuntimeClient` 手里只有 fetch 和一个 JWT
（`http-client.ts:60`）。切换点是 31 行的 `select.ts:19-31`：
`CUMORA_RUNTIME_CLIENT=http` 才构造 HTTP 客户端，否则给进程内单例。

接口设计的三条规矩自己写在 `client.ts:19-32`：**所有方法显式传 agentId**
（哪怕 http 侧的 JWT 已经把身份钉死了，为的是「调用点在两侧读起来一模一样」）；
**读方法只返回 JSON 友好的行结构**，不让 pg 的 `Row<T>` 泄漏到接口上；
**写方法一律 `Promise<void>`**，重试与排队藏在实现里，调用方不知道。
这三条合起来的效果是：agent 回合的代码不知道自己跑在服务器进程里还是跑在 pod 里。

**两份实现的真正差异不在方法列表，在失败语义。** http 侧几乎每个非关键方法都把
异常吞掉只打一行 warn：`setStatus`（`http-client.ts:186-191`）、`heartbeatStatus`
（`:199-204`）、`publishTyping`（`:217-225`）、`recordEvent`（`:260-272`）——
最后这条的注释直说「production agents have crashed mid-task that way」。
`finishRun` 是唯一被升级成重试的（4 次、500ms 起指数退避、封顶 5s，`:290-303`），
理由写得很清楚：这行状态转换是**用户可见的运营状态**，不是调试事件。
`claimWork` 失败是 fail-open（`:424-428`，直接返回 `accepted: true`）。
`humanRecentlyActive` 在 http 侧干脆 `return false`（`:463-468`）——
一个「这个信号我实现不了，那就保守答」的诚实降级。
busy 心跳连续失败 5 次才触发 `notifyAlert`（`:39`、`:347-357`），
因为静默降级正是 steering 这个特性当初要修的病，不能让它自己静默地退回去。

**唤醒总线是 Redis pubsub + SSE 的两段式。** `wake-bus.ts` 的 `deliver()` 把事件
publish 到 `cumora:wake:<agentId>`（`:131`、`:226-232`），**返回 Redis 报告的订阅者数** ——
这个返回值本身就是「集群里还有没有活着的 pod」的判据，为 0 就去 `ensurePod` 拉一个。
服务端只在拿到第一个本地 SSE 连接时才 subscribe 那个 channel，最后一个本地订阅断开
才 unsubscribe（`:276-278`、`:321-323`、`:291-308`）。subscribe 刻意排在给 pod 发
`ready` 事件**之前**（`:317-323`），否则中间那个窗口里到达的 wake 会被静默丢掉 ——
注释承认 pod 的初次 drain 能兜住，但「能兜住的东西也别去依赖」。

**唤醒事件只有两种 kind**（`wake-bus.ts:47`）：`wake` 是「你有活了，自己去看收件箱」，
`steer` 是「你正在回合中途，这条消息现在就注入」。`wake` 带一个五选一的 reason
（message.new / idle / manual / background_scan / poll.updated，`:96`）。
**去重不在总线做** —— 总线只给每个事件一个 uuid（`:230`），由 pod 侧按 id 去重
（`:22-25` 的注释说明是为了容忍滚动重启期间的双订阅）。总线自己做的是**背压**：
单个订阅者未发送缓冲超 1MB 直接掐流（`:152`、`:175-180`），异步授权检查排队超 64 条
直接 revoke（`:156`、`:207-210`）。两处的理由是同一句：收件箱才是持久队列，
重连能自愈，不值得为它 OOM。

**合并与防抖在 pod 侧，不在总线。** `wake-options.ts` 的 `mergeWakeTurnOptions`
（`:162-190`）加 `mergeWakeBackgroundBriefs`（`:129-157`）负责把一串唤醒折成一次回合
的一份 options：manual 的卡片简报优先级最高（`:168-179`），两份不同简报会被
**拼接并各分一半字符预算**（`:147-151`），而不是后来的覆盖先到的 —— 注释点明这是
为了「不丢掉一张已指派的卡」。`pod-agent.ts` 的 `drain()`（`:68-91`）才是真正的
串行化：busy 时只置一个 `pendingRerun` 标记，当前回合跑完再 do-while 多跑一轮。
所以「一个 agent 一个 pod、一个 pod 一次一回合」在 cumora 里是**结构上**成立的，
不靠任何锁。

**调度器是一条从 Redis pubsub 到 kubectl 的漏斗，每一层都是一道减法闸。**
`scheduler.ts` 收到 `CH_MESSAGE_NEW` 后，多副本先靠
`SETNX cumora:wake-claim:<messageId>` 选出唯一的处理者（`:454-469`）。
然后 `wake()` 算收件人：`conversations.members` 当**不可信的反范式数据**重新校验
（`:613-633`）、mute 过滤、agent 发的消息给每个收件人扣一个 turn-rate 令牌
（30/min，`:228`、`:641-657`）、人类 @点名时问一次小模型要不要收窄收件人
（`:676-708`，注释强调这个收窄是「唯一会静默出错的操作」所以极度保守）。
再由 `fanOutWake` 在一个信号量下并发（`:41`、`:846`），每个云端收件人先跑一次
小模型 triage，判「不相关」就**根本不唤醒**（`:859-868`）—— 这是省掉一次大模型
回合的地方。最后 `wakeOne` 才决定是 SSE 投递还是去拉 pod（`:308-452`）。
横向的并发协商刻意**没做**成调度器的串行队列：`:710-725` 记了一次失败尝试 ——
串行 @all 「机制上能跑，但不像真人团队会做的事」，最后把协调交给 agent 自己
（`glance` / `claimWork`）。

**唤醒的持久性口径分级，写在 `_shouldRetryEnsurePodFailure`（`:82-101`）。**
`message.new` 的唤醒**不进重试队列**，因为消息本身已经在 `messages` 表里，pod 一挂上
SSE 就无条件 drain 一次把收件箱读干净（`pod-agent.ts:113-117`）；再排一次重试会让
同一条消息触发两次回合、发两条回复 —— 注释直接点名了这个 bug（「Nova says 3 then 1」）。
反过来，`idle` / `background_scan` 这类**合成唤醒没有 DB 行兜底**，所以 `wakeOne` 结尾
挂了一个 20 秒内联轮询，每 500ms 重投一次直到 pod 的 SSE 接上（`:443-451`）。
只有 `manual` 走 Redis zset 上的持久重试队列（`:72-75`、`:107-136`）。
同一个「唤醒」，三种持久性，判据是**有没有可回读的权威事实**。

**pod 生命周期归 orchestrator 管，但关机是 pod 自己决定的。** `ensurePod` 幂等：
同一 agentId 的并发调用合并到一个 in-flight promise（`:626-633`），整个实现外面套一个
180s 看门狗，防止 inFlight map 里留下僵尸条目把后续调用全卡死（`:635-646`、`:658-689`）。
它先探 pod 健康度，卡死的 Pending（ImagePullBackOff / Unschedulable 之类，
`:596-614`）和 Failed / Unknown 先 reap 再 apply（`:691-758`），然后过一道集群级
`/dev/fuse` 配额闸 —— 用量 ≥90% 直接拒绝新建（`:760-774`）。pod 侧的 idle watcher
到点把状态置 `resting` 并 exit 0（`pod-agent.ts:200-222`），另有一条 no-work 快速退出：
boot 后 NO_WORK_MS 内一次真 wake 都没收到就退，把 fuse 槽让给别人（`:44-53`）。
两条 GC 循环兜底：完成态 pod（`orchestrator.ts:1060-1123`）和闲置的 chrome-profile PVC
（`:888-1058`），还有一个持续压力监控在集群憋住 5 分钟后告警（`:1125-1224`）。
这一整层的每一处阈值背后都写着一次事故日期。

## 2. MAOS 的对应物

### 2.1 运行时与唤醒

| cumora 的机制 | MAOS 的对应物 | 判定 |
| :-- | :-- | :-- |
| `AgentRuntimeClient` 一接口两实现，env 变量切换（`client.ts:193` / `select.ts:19-31`） | `ModelClient` ABC + Scripted/Gateway 两实现，`select_model_client()` 按环境变量选（`maos/model/client.py:49-51`、`:272-302`） | **同构**，且 MAOS 的切换判据更严（三个变量缺一即降级，`:292-296`） |
| 接口边界 = agent 回合的**全部外部 IO**（DB / Redis / 状态 / 观测都在内） | MAOS 的接口边界 = **只有模型调用**；Agent 直接持有 store（`maos/runtime/worker.py:34`） | 形似神不同：cumora 的抽象面大得多，见 §3 #1 |
| http 侧非关键方法吞异常打 warn（`http-client.ts:186-272`） | `GatewayModelClient` 所有异常统一转 `RuntimeError` 抛出（`maos/model/client.py:211-233`） | 形似神不同：MAOS 只有一个方法，没有「关键/非关键」之分 |
| `deliver()` 返回订阅者数，0 = 集群里没有活 pod（`wake-bus.ts:226-232`） | `InMemoryEventBus.publish` 只入队，无投递计数（`maos/core/eventbus.py:47-49`） | MAOS 没有 —— 也不需要，消费者是同进程对象 |
| `wake` / `steer` 两种事件 kind（`wake-bus.ts:47`） | Topic 常量分流 TASK_ASSIGNMENT / TASK_RESULT / … | 形似神不同：cumora 按「打断 vs 排队」分，MAOS 按「消息类型」分 |
| pod 侧 `drain()` 的 busy + pendingRerun 串行化（`pod-agent.ts:68-91`） | `run_until_settled` 的 drain→gate→drain 单线程循环（`maos/flows/common.py:108-130`）+ 单线程串行 drain（`maos/core/eventbus.py:54-83`） | **同构**：都是「一次只跑一个」，MAOS 更彻底（整个进程单线程，且刻意如此，见 eventbus 模块 docstring） |
| 唤醒风暴的三道闸：低优先级预算 20/min（`scheduler.ts:215`、`:318-321`）、turn-rate 30/min（`:228`、`:234-243`）、fanout 信号量（`:41`、`:846`） | `max_redelivery=2` 后进死信（`maos/core/eventbus.py:40`、`:69-79`）+ `max_cycles=20` 防不收敛（`maos/flows/common.py:119`、`:130`）+ `max_rounds=1000`（`eventbus.py:54-83`） | 形似神不同：MAOS 防的是**事件环路**（写错状态机），cumora 防的是**流量洪峰**（真实用户 + 崩溃恢复堆积） |
| 合成唤醒需内联重投，durable 唤醒禁止重试（`scheduler.ts:82-101`、`:443-451`） | 所有消息一视同仁重投，靠幂等键去重（`eventbus.py:69-79` + `control_plane.py:326-328`） | 形似神不同：MAOS 把去重收口在幂等闸，cumora 收口在「有没有权威事实可回读」 |
| 多副本 `SETNX` 唤醒去重（`scheduler.ts:454-469`） | 单进程，无对应 | MAOS 没有（不需要） |
| 唤醒前小模型 triage 决定要不要唤醒（`scheduler.ts:777-829`、`:859-868`） | `dispatch_ready` 按 DAG `depends_on` 派发，不问模型（`maos/core/control_plane.py:285-304`、`:294`） | MAOS 没有；**且不该有**，见 §4 #4 |
| 原子认领 `HSETNX`，(taskType, subject) 为去重键（`inproc-client.ts:896-978`） | `ControlPlane.claim` 的 `claim:<task_id>:<attempt>` 幂等键 + store 唯一索引（`control_plane.py:309-329` / `maos/core/store.py:377-389`） | **同构**，MAOS 的约束更强（DB 唯一索引 vs Redis HSETNX） |
| 认领失败 fail-open（`http-client.ts:424-428`、`inproc-client.ts:970-977`） | fail-closed：`claim` 返回 None 就直接不执行（`control_plane.py:326-328` / `worker.py:50-52`） | 形似神不同 —— **方向相反**，见 §3 #6 |
| 认领租约超 ttl 可被后来者接管（`inproc-client.ts:955-968`） | 幂等键**永不过期、无撤销口**（`control_plane.py:312-315` 自陈） | MAOS 没有，见 §3 #4 与附录 A #1 |
| 运行时把原生工具暴露给模型：schema 是纯数据、执行体是独立函数、两者都不含权限判断（`native-tools.ts:25-71` vs `:84-154`） | ToolPort / SkillInvoker，Identity 三查在 Worker 侧先卡（`docs/architecture.md:83`） | 形似神不同：cumora 的 read/write/edit_file **明确不受 persona 目录约束**（`native-tools.ts:5-14`），边界交给容器；MAOS 在调度侧就卡死 |
| FUSE 端点把工作区映射成文件系统，JWT 钉死 `agent_id`，路径过 `isSafePath`（`fs-endpoints.ts:1-25`、`:37-49`、`:78-205`） | artifacts / store 按 task_id 隔离 | 形似神不同：cumora 要跨进程边界才需要这层；MAOS 同进程直接持有对象 |
| run 观测三件套 createRun / recordEvent / finishRun（`client.ts:308-340`） | 每次状态迁移落一条 `event_log`（`docs/architecture.md:111-112`） | **同构**，MAOS 的更紧（迁移与记录是一件事，不存在漏记） |
| 失败告警面 `notifyAlert`（`http-client.ts:347-357`、`orchestrator.ts:846-857` 等多处） | 无告警面，只有 log | MAOS 没有，见 §3 #1 |

### 2.2 错误路径逐条对照（Q5：`agent-error-paths.test.ts` 的失败清单）

| cumora 测的失败场景 | 它怎么处理 | MAOS 遇到这个会怎样 |
| :-- | :-- | :-- |
| LLM 429 配额耗尽（`agent-error-paths.test.ts:136`） | run 记 `skipped`（不是 failed），并往收件箱涉及的每个房间投一条 `kind=system` 通知说明「为什么大家突然不说话了」 | `GatewayModelClient` 把 HTTP 429 转成 `RuntimeError`（`maos/model/client.py:219-222`），Worker 捕获转成 `status=failed` 的 TaskResult（`maos/runtime/worker.py:69-71`）。**状态对，但人在房间里看不到原因** |
| 同一失败在多 agent 同房间刷屏（`:190`） | `dedupeKey` + PostgreSQL 持久幂等标记，跨 Redis/进程故障也只发一条（`client.ts:347-350`） | MAOS 无系统通知面，不存在这个问题（也不存在这个能力） |
| 非配额类 LLM 失败（`:222`、`:270`） | run 记 failed，**同时**落一条用户可见通知，重复唤醒同一输入时去重 | 同上：只进 `TaskResult.error` 与 `event_log` |
| 提供方连接失败（`:294`） | **短重试可以救回本回合，且不落失败通知** | `GatewayModelClient` 没有任何重试 —— 一次 `urlopen`，`URLError` 直接抛（`maos/model/client.py:223-225`）。演示现场一次网络抖动 = 一个任务 failed。见 §3 #2 |
| 上游取不到 image_url（`:341`） | 剥掉所有 `input_image` 块重试一次，第二次的 tool call 继续驱动本回合 | MAOS 无多模态输入路径，不适用 |
| 图片生成返回 URL 而非内联 b64（`:396`） | 回退去 fetch 远端字节再落存储 | 不适用 |
| MAX_HOPS 触顶而模型还在要工具（`:448`） | 发一条 `turn.cap_reached` 事件，不静默截断 | MAOS 的等价物是 `max_attempts` 耗尽（`control_plane.py:365`、`:452`）→ FAILED，有记录。**同构** |
| pod 起不来 / kubectl apply 失败（`orchestrator.ts:846-857`） | 返回 `!ok` + 告警；durable 唤醒靠收件箱自愈，manual 进重试队列（`scheduler.ts:431-434`） | 不适用：MAOS 的 Worker 是进程内对象，不存在「起不来」 |
| pod 卡 Pending 不可自愈（`orchestrator.ts:596-614`、`:697-734`） | 识别为终局态、reap 重建、告警说明「下次还会复发除非有人修」 | 不适用 |
| SSE 断线（`pod-agent.ts:298-302`） | 指数退避重连，**且只有连接真的稳过才重置退避梯子** | 不适用（MAOS 无长连接）。这条思路本身值得记：「连上了」不等于「健康」 |
| 未捕获的 promise rejection（`pod-agent.ts:306-326`） | 装进程级兜底网转告警，**不让它杀死 pod** | Handler 抛异常 = nack 重投，超限进死信（`maos/core/eventbus.py:69-79`）。**同构**，MAOS 的更干净（失败进状态机而不是进日志） |
| JWT 过期 / 伪造 / 跨租户复用（`runtime-server.test.ts:324-497`） | 每条路由在 handler 之前 401；身份一律以 JWT subject 为准，忽略 body 自称 | 不适用（无 HTTP 面）。对应物是 Identity 三查在 Worker 侧（`docs/architecture.md:83`） |

## 3. 可移植清单

**落点** 三档：新增插件（`maos/skills/**` `maos/tools/**` `maos/agents/**` `maos/kb/**`，不碰内核）
／ 动内核（`maos/core/**` `maos/runtime/**`）／ 动冻结契约（`maos/contracts/**`、`maos/core/store.py` 表结构）。
本清单**没有一条落在冻结契约面**。

| # | cumora 的做法 | 出处 `文件:行` | MAOS 现状 | 形态 | 落点 | 成本 | 判断 |
|---|---|---|---|---|---|---|---|
| 1 | 静默降级必须有阈值告警：busy 心跳连续失败 5 次触发告警，告警正文里写明**后果**（"steer routing has degraded to wake-only for this pod"） | `http-client.ts:39`、`:347-357` | `select_model_client` 缺任一环境变量就静默回落 Scripted，只有一行 `log.info`（`maos/model/client.py:292-296`） | 抄思想 | 动内核（`maos/model/client.py` 一处） | 0.5 人天 | **赛前做** —— 复赛现场 key 配错会让「真模型跑通了」变成假结论，是唯一一类会让演示结论失真的静默失败 |
| 2 | 连接类失败短重试能救回本回合，且**不该让用户看见失败**；重试次数与退避写在客户端内部，调用方无感 | `agent-error-paths.test.ts:294`；`http-client.ts:98-116`、`:290-303` | `GatewayModelClient` 一次 `urlopen`，`URLError` 直接转 `RuntimeError`（`maos/model/client.py:223-225`） | 抄接口 | 动内核（`maos/model/client.py`） | 1 人天 | 复赛后 —— 赛前默认路径是 Scripted，真模型只在演示时开；重试会让演示时长不可预测，彩排价值高于健壮性 |
| 3 | 失败要让人看见：模型不可用时往房间投一条 `kind=system` 通知，`dedupeKey` + 持久幂等标记防多 agent 刷屏 | `client.ts:342-363`；`inproc-client.ts:630-748` | 失败只进 `TaskResult.error` 与 `event_log`，Element 房间里一片安静 | 抄思想 | 动内核 | 1.5 人天 | 复赛后 —— HiClaw 对接层已有发言口，但「接在 Worker 还是 Gate」要先定；赛前不动跨层的东西 |
| 4 | 认领租约会过期：`HSETNX` 失败后读持有者 `startedAt`，超 ttl 就 `hdel` 抢占，并处理抢占竞态 | `inproc-client.ts:955-968` | `claim` 幂等键永不过期、无撤销口（`maos/core/control_plane.py:312-315` 自陈） | 抄思想 | 动内核（`control_plane.py` + store 新增表） | 2 人天 | 复赛后 —— 单进程同步执行触发不到；PG store 上进程硬崩会把任务永久卡在 RUNNING（记附录 A #1） |
| 5 | 「连上了」不等于「健康」：SSE 重连的退避梯子**只在连接真的稳过之后**才重置 | `pod-agent.ts:298-302` | MAOS 无长连接；但 RocketMQ 后端的连接判活有同类问题面（`maos/core/eventbus.py:142-149` 明确拒绝回落内存版） | 抄思想 | 新增插件（写进 `deploy/` 的 RocketMQ 手册，不改代码） | 0.5 人天 | 复赛后 —— MAOS 已经把「不许静默回落」这条做对了，这条只是补一句「握手成功不算健康」的判据 |
| 6 | 唤醒合并：多次唤醒折成一次回合的一份 options，两份简报**拼接 + 各分一半预算**而非后覆盖前 | `wake-options.ts:129-157`、`:162-190` | MAOS 一条 TaskAssignment 一次 claim 一次执行，无合并需求 | 不移植 | — | — | **不做** —— MAOS 的任务是 DAG 节点不是消息流，不存在「同一 agent 被短时间戳多次」的形态 |
| 7 | 认领失败 fail-open（Redis 挂了就当抢到，"worst case is two agents do the same thing once"） | `http-client.ts:424-428`；`inproc-client.ts:970-977` | fail-closed：`claim` 返回 None 就不执行（`control_plane.py:326-328`） | 不移植 | — | — | **不做** —— 方向相反且 **MAOS 是对的**。cumora 的最坏后果是重复搜一次网页；MAOS 的最坏后果是同一笔退款被处理两次（铁律 8） |
| 8 | 失败场景清单化：8 条错误路径逐条钉死，含「重试后恢复且**不落**失败通知」这种正向断言 | `agent-error-paths.test.ts:136-448` | 1069 条测试，模型侧失败路径覆盖未逐条核（见 §5 #3） | 抄思想 | 新增插件（`maos/tests/**`） | 1.5 人天 | 复赛后 —— 加测试不改行为，但会挤占本该用于演示彩排的时间 |

**统计**：8 条 —— 赛前做 1 / 复赛后 5 / 不做 2。
落点：新增插件 2（#5 #8）／ 动内核 4（#1 #2 #3 #4）／ 动冻结契约 0；#6 #7 判为不移植，无落点。

## 4. 反向清单 —— 它做了但 MAOS 不该抄

判据一句话：*这个设计在解决我也有的问题，还是在解决它的用户量 / 多租户 / 向后兼容才有的问题？*

1. **整个 K8s pod 编排层**（`orchestrator.ts` 全 1257 行）。它解决的是「每个 agent 一个容器、
   集群 `/dev/fuse` 槽位有限、server 有多个副本」。一人公司单进程，MAOS 的 `WorkerRuntime`
   是个 Python 对象（`maos/runtime/worker.py:27`）。抄这层等于给自己雇一个 SRE。

2. **集群配额闸 + 压力监控 + 两条 GC 循环**（`orchestrator.ts:760-774`、`:1060-1224`）。
   每个阈值背后都写着一次事故日期（`:198` 的注释直接写了 "FUSE-cap incident 2026-05-20"）。
   没有那个规模就没有那个疤，抄疤不抄伤是最典型的规模包袱。

3. **多副本 `SETNX` 唤醒去重**（`scheduler.ts:454-469`）。前提是「N 个 server 副本订阅同一个
   channel」。MAOS 单进程，抄进来是纯负债：多一个 Redis 依赖，换一个永远为真的判断。

4. **唤醒前的小模型 triage**（`scheduler.ts:777-829`、`:859-868`）。它解决的是
   「群聊里约 26% 的唤醒最后回复了空气」（`:676-680` 的注释给了这个生产数字），
   本质是**自然语言的收件人不确定**。MAOS 的收件人由 DAG 的 `depends_on` 唯一确定
   （`maos/core/control_plane.py:294`），零歧义。加一层模型判断是把确定性换成不确定性 ——
   而且它会引入一个新的静默失败面（该唤醒没唤醒，无回复无日志无 run 行，cumora 自己在
   `:682-687` 承认了这是「唯一会静默出错的操作」）。

5. **`wake-options.ts` 的 200 行参数化**（Q3 的正面回答）。逐条拆：
   `MAX_WAKE_PAYLOAD_CHARS` 与三个 brief 上限（`:3-6`）是抗**不可信生产者**，
   而 MAOS 的事件全部由自己的 ControlPlane 铸造；`pollBrief` 的
   「20 个选项 × 50 个投票人」裁剪（`:76-93`）是投票这个业务功能自带的包袱；
   `mergeWakeTurnOptions` 的 manual 优先级（`:168-179`）只有在「同一 agent 被多种来源
   短时间戳多次」时才有意义。**结论：这层旋钮对 MAOS 是过度设计**（对 cumora 不是）。
   唯一可能有价值的是 `mergeWakeBackgroundBriefs` 的**预算切分**思路（`:147-151`）——
   但也要等 MAOS 真出现「两份内容抢同一个上下文预算」的场景，现在没有。

6. **agent 之间的「在想什么」协商面**（`markThinking` / `peekThinking` / `claimWork`，
   `client.ts:247-299`）。它解决的是「多个自主 agent 决定要不要抢同一件活」。
   MAOS 的活是 Manager 规划出的 DAG 节点，谁干哪个是**派单决定的，不是抢的**。
   抄这个等于把确定性调度改成机会主义调度 —— 演示现场反而更难解释「为什么是它接的」。
   （注：`claimWork` 的**原子性实现**值得看，见 §3 #4；不该抄的是「agent 自主抢活」这个语义。）

7. **BYOA / 免费层 / 租户分级的分支**（`scheduler.ts:392-415`）。纯商业模式包袱：
   免费用户不许起托管 pod、BYOA 走用户自己的机器。MAOS 没有这三层身份。

## 5. 我没看懂 / 没时间看的

1. **`server.ts` 935 行没有逐条读处理器实现**。只读了它在测试里被挂载的方式
   （`runtime-server.test.ts:40-57`）和路由清单。特别是 `/cli` 那条通用路径和
   `/inbox-triage/payload`，它们跨到 T42 的 ToolPort 面，我按派单要求止步。
2. **`turn.ts` 没读**。只见到入口签名 `runAgentTurn(agentId, options)`（`pod-agent.ts:80`）
   和 `AgentTurnOptions` 的字段名。**steer 究竟在「hop boundary」的哪一点注入，我没看到** ——
   §1 里那句「回合循环在 LLM 迭代之间 drain」是转述 `scheduler.ts:361-366` 的注释，不是我读到的代码。
3. **MAOS 侧没有逐条核对 1069 条测试对模型失败路径的覆盖**。§3 #8 写的是「覆盖未逐条核」，
   这是诚实的未知，不是「没覆盖」的结论。
4. **`authorization.ts`（172 行）与 `jwt.ts`（83 行）没读** —— 授权面不归本轨。
5. **`agent-lifecycle-events.test.ts` / `agent-scanner.test.ts` 只读了文件名与行数**，没读内容。
   §1 里关于 scanner 的说法全部来自 `scanner.ts` 源码本身（`:29-31`、`:55-76`、`:174-209`）。
6. **`sse-parse.ts` / `pod-agent-exit.ts` / `fs-namespace.ts` / `pod-tools.ts` 没读**，
   只从调用点推断了职责。`decidePodExit` 的具体判据（`pod-agent.ts:215` 调用它）我没验证。
7. **`inproc-client.ts` 1187 行读了约四成**：头部、`claimWork` / `releaseWork` 全段、
   方法骨架清单。中间大段 SQL（`loadInbox` / `loadContext` / `loadMemory`）只扫过结构，
   没有逐行读 —— 那部分是 cumora 的数据模型，不是运行时编排。

## 附录 A · 顺手发现的 MAOS 问题

本轮不改 MAOS 也不追加账本，以下三条留给整合轮统一折进 BACKLOG。

1. **`claim` 幂等键无过期，PG store 上存在永久卡死的可能。**
   `ControlPlane.claim` 烧掉 `claim:<task_id>:<attempt>` 之后没有撤销口
   （`maos/core/control_plane.py:309-329`，注释 `:312-315` 自陈「store 只有 claim/finish，
   没有撤销口」）。Worker 若在 `_transit(RUNNING)` 之后、`_reply` 之前**硬崩**
   （进程被杀 / OOM），该 attempt 再也无法被认领，任务永久停在 RUNNING。
   `SqliteStore(":memory:")` 上库随进程一起消失所以看不出来；**PG store 上会留下卡死的行**。
   *当前不是活 bug* —— 单进程同步执行，且 `_invoke` 兜住了所有 Python 异常
   （`maos/runtime/worker.py:62-71`）—— 但 PG 上生产之后是真缺口。
   cumora 对同一问题的解法是租约超时接管（`inproc-client.ts:955-968`）。

2. **`select_model_client` 的降级只有一行 `log.info`。**
   三个环境变量缺任何一个就静默回落 `ScriptedModelClient`（`maos/model/client.py:292-296`）。
   「以为在跑真模型、其实跑的是假模型」这类错误不会有任何显眼提示，而它恰好会让一切
   验证结论失真。cumora 对同一类静默降级的处理是打阈值告警**并在告警正文里写明后果**
   （`http-client.ts:347-357`）。这条就是 §3 #1。

3. **`GatewayModelClient` 无任何重试。**
   一次 `urlopen`，`URLError` / `TimeoutError` / `OSError` 直接转 `RuntimeError`
   （`maos/model/client.py:223-225`）。演示现场一次网络抖动 = 一个任务 failed，
   而 MAOS 的失败会一路走到 REWORK 或 FAILED，观众看到的是「系统判错了」而不是「网断了」。
   cumora 对同一场景的判断是「连接类失败短重试能救回本回合，且不该落用户可见的失败通知」
   （`agent-error-paths.test.ts:294`）。这条就是 §3 #2。
