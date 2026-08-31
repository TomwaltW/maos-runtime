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
