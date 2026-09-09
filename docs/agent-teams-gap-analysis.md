# Agent Teams 差距分析 —— 在 MAOS 上补「点对点协作 + 自我认领」能力

- 基线：`goai-restructure` @ `8a6c2f9`，2026-09-07 只读测绘，本文未改任何代码、未跑迁移。
- 行号均为该基线下 `cat -n` / `grep -n` 实读；行号会漂，核对请用同一 sha。
- 写 `UNKNOWN` 的地方是没找到或被守卫拦读，不是推测。
- 派单原文第一步的第 4、5 项与第二步的第 9、10 项在粘贴时被截断，按孪生会话（6e0963cf，已中断）里的完整原文补齐：4 = Worker Runtime，5 = 七门 G1–G7，9 = 信任边界，10 = 生命周期 hook。

## 结论摘要

**该做，但要缩小范围。** 理由三条：

1. 「共享任务列表 + 认领互斥 + 依赖解阻塞」MAOS 已有七成：任务表带 `depends_on` / `worker_id`（`maos/core/store.py:127-147`），`dispatch_ready` 按依赖全部 DONE 才派（`maos/core/control_plane.py:305`），`claim` 用幂等键做认领互斥（`control_plane.py:342-345`）。缺的是**认领租约/超时**（`claim_timeout` 迁移在冻结表里躺着、零实现，`maos/contracts/states.py:29`）和**跨 plan 的可认领视图**。
2. 「点对点消息 / 成员名册 / 空闲通知 / 生命周期 hook」基本没有，但仓库有一条成文路径能零契约改动地补：新建表 + `event_log` 自由 `event_type` + 新模块（先例 `maos/config/audit.py:9-27`）。**一条冻结契约都不用碰**，五个改造方向里没有一个需要动 `maos/contracts/**`、既有表 DDL 或 `Envelope`。
3. 要砍掉的三样：不许 agent 间消息成为第二条状态路径（Control Plane 仍是唯一写者，`docs/architecture.md:109-110`）；第一期不做多线程/多进程并发（MAOS 对整类撞车问题的免疫「是单线程送的」，`docs/refs/cumora-coordination.md:162-169`，并发化要连带四层闸一起上）；不给 agent 间消息套七门（七门判的是 artifact 结构，`maos/runtime/gate.py:261-292`）。

可拆成 5 个相互独立的大方向并行做（末节），其中「任务板与认领租约」是其余四个的地基，但不阻塞它们各自开工。

---

## 第一步：现状测绘

### 1. 任务 / 工作项

| 问题 | 定位 | 说明 |
| :-- | :-- | :-- |
| 数据结构 | `maos/core/store.py:127-147` | `task` 表：`state` / `attempt` / `max_attempts` / `risk_level` / `effect_risk` / `depends_on`(JSON) / `inputs` / `acceptance` / `findings` / `worker_id` / `last_error` 等 18 列。DDL 被 `.contracts.lock` 指纹锁死（`maos/tests/test_contracts_frozen.py:35-50`），只许新增表 |
| 状态集合 | `maos/contracts/states.py:11-19` | 8 态：PENDING / DISPATCHED / RUNNING / AWAITING_REVIEW / REWORK / BLOCKED / DONE / FAILED；终态 `:22` |
| 合法迁移 | `maos/contracts/states.py:26-43` | 16 条显式迁移表；`assert_transition` `:69-72` 非法即抛 `IllegalTransition` `:61` |
| 迁移表里**零实现**的两条 | `states.py:29`（`claim_timeout` DISPATCHED→PENDING）、`:40`（`human_resume` BLOCKED→PENDING） | 生产代码 `grep claim_timeout\|human_resume` 零实现，只在注释出现（`maos/runtime/gate.py:862-864`、`maos/kb/experiment.py:326`）。对 Agent Teams 这是要害：**认领后没有超时接管**，人工也没有「带反馈退回重做」的出口 |
| Plan 状态 | `states.py:46-58` | PENDING / RUNNING / DONE / FAILED，含 `replan`（RUNNING→PENDING，`:57`） |
| 谁有写权限 | `maos/core/control_plane.py:1-8`（铁律）、`:202-218`（`_transit` 唯一出口）、`:220-227`（plan） | 只有 `ControlPlane` 写 task/plan；`Store` 抽象类 docstring「Agent 永远不直接碰这一层」（`store.py:40-41`）。Manager 的 Identity `write_scope={plan,task}`（`maos/agents/manager.py:40`），但它只产规格，由流程层调 `create_plan`（`control_plane.py:250-287`） |
| 依赖解阻塞 | `control_plane.py:296-315` | `dispatch_ready` 扫全 plan：PENDING 且未冻结（`:303`）且 `depends_on ⊆ DONE`（`:305`）才派；`_advance` `:923-937` 每次 DONE 后重扫。**是拉式重扫，不是被阻塞任务被通知** |

### 2. Control Plane 状态权威

- 状态写入口只有两个：`_transit`（`control_plane.py:202-218`）与 `_transit_plan`（`:220-227`），都先 `assert_transition` 再 `store.update_task` 再 `append_event_log`。
- `control_plane.py` 内部绕开 `_transit` 直接 `update_task` / `insert_task` 的三处：`_apply_replan` `:698-710`（覆写规格）、`:712-725`（新建任务）、`:728`（`last_error=frozen_by_replan` 冻结）。改的是规格与冻结标记，不是 `state`。
- 控制面**之外**的旁路（子代理全仓 grep）：`update_task` / `insert_task` / `update_plan_state` / `insert_plan` 只有一处命中 —— `maos/flows/scenario_11.py:534` 直接 `store.update_task(TASK_INV_FILE, inputs=inputs)`，注释 `:527-530` 自辩「任务输入本来就归编排层」。它写的是 `inputs` 不是 `state`，但不落任何事件。直写 task/plan 表的 SQL 在 `store.py` 之外零命中（四处都在 `store.py:235/249/262/335`）。
- 一个**结构性**口子：Worker 把裸 store 交给每个 Agent（`maos/runtime/worker.py:34` `cls(model, store=self.cp.store)` → `maos/skills/invoker.py:51-53` `self.store = store`），Agent 经 `self.skills.store` 拿得到 `update_task`。今天没人这么用（`base.py:198/209` 只用它记账），所以「Agent 不碰 store」是纪律不是机制。加 agent 间通道后这条要变成机制（第三步 D3）。
- 产物旁路（不是状态）：`review_after_gate` 直接 `insert_artifact`（`maos/agents/reviewer.py:181-185`）、`patch_verifier`（`maos/flows/common.py:301-305`），都补 `ArtifactSeeded` 留痕（`reviewer.py:186-199`、`common.py:306-317`）。
- 幂等闸四处：`claim` `:342`、`on_task_result` `:356`、`on_review_verdict` `:452`、`human_decision` `:760`，都走 `processed_key` 主键冲突（`store.py:394-415`）。
- `maos/store/`（`StorePort` 适配器，`maos/store/port.py:36`、`sqlite_store.py:118`）是**包核心 store 的外壳且生产链路零 import**（`flows/common.py:50,96` 直接用 `maos.core.store.SqliteStore`；`docs/architecture.md:183-185` 如实写明）。

### 3. EventBus

| 问题 | 定位 | 说明 |
| :-- | :-- | :-- |
| 消息结构 | `maos/contracts/events.py:48-65` | `Envelope`：`event_type / plan_id / task_id / idempotency_key / payload / event_id / trace_id / attempt / occurred_at`。**无 sender / recipient / addressee / reply_to**；唯一「收件人」语义是 `TaskAssignment.payload.role`（`events.py:100`），Worker 按 role 取执行者（`worker.py:43-47`） |
| Topic / EventType | `events.py:19-24`、`:30-34` | 5 个 topic、4 个事件类型，文件冻结 |
| 广播还是定向 | `maos/core/eventbus.py:66-68` | 同一 topic 的**所有**订阅者都收到每条消息（广播）；`group` 只用于重投计数键（`:70`） |
| 订阅注册 | `eventbus.py:51-52`（内存）、`:364-368`（RocketMQ） | `subscribe(topic, group, handler)`。订阅者：CP `control_plane.py:188-189`，Worker `worker.py:35`（group=`worker-<id>`）。Gate **不订阅**，轮询 store（`gate.py:252-259`） |
| push 还是 poll | `eventbus.py:54-83`；驱动循环 `maos/flows/common.py:108-130` | 内存版是**显式 drain 的 poll**：`run_until_settled` 每轮 `bus.drain()` → `before_review` → `gate.review_pending` → `bus.drain()`；单线程串行是刻意的（`eventbus.py:8-9`）。RocketMQ 版 `drain` 用长轮询（`:384-459`），仍由调用方驱动。`:112` 明写「换 RocketMQ 后这个循环消失」 |
| 房间镜像 | `hiclaw/matrix_bus.py:893-895`（装饰器）、`publish` `:946` → `_mirror` `:973` | 先委托 inner 再镜像；`summarize` `:158-170` 对未知 event_type 有通用兜底（`:170`）。房间→进程是 push（nio 回调 `room_ingress.py:421-456`、`matrix_bus.py:554-653`），但推的是 ingress 的 router，不进任务总线 |
| 新增 topic 的约束 | `maos/config/audit.py:9-27`（引 `maos/agents/testing.py:50`、`maos/kb/retriever.py:571`） | 成文纪律：新事件走 `append_event_log` 的自由 `event_type`，**不进 contracts 的 Topic、不加新 Topic**。`publish(topic: str)` 技术上收任意字符串（`eventbus.py:47`），测试也没钉 topic 集合，但 `validate()` 对未知 event_type 报错（`events.py:210-213`） |

### 4. Worker Runtime

- 进程 / 线程 / 协程：都不是。`WorkerRuntime` 是同进程对象（`worker.py:27-36`），`on_assignment` 在 `bus.drain()` 调用栈里同步执行（`eventbus.py:66-68`）。全仓生产路径只构造一个：`flows/common.py:103` `worker_id="w1"`（写死字符串；C-4 冻结返回契约把 `worker_id=="w1"` 写进断言，`docs/parallel/contracts.md:40`）。
- 拉起与回收：`build()` 构造（`common.py:81-105`），没有 start / stop / close；生命周期 = 进程生命周期。只有总线（`eventbus.py:507-518`）与房间通道（`matrix_bus.py:826`）有 `close()`。
- 生命周期边界：一次 `on_assignment` = 契约校验 `:39-41` → 按 role 取 agent `:43-47` → `cp.claim` `:50` → 构 `TaskContext` `:54-59` → `agent.run` `:60/:64` → `_reply` 发 TaskResult `:73-79`。Agent 实例每 role 一个、跨任务复用（`:34`），归属靠 ContextVar（`maos/agents/base.py:39-56`）。
- Worker 之间能不能通信：**不能**。Worker 只订阅 `TASK_ASSIGNMENT`、只发 `TASK_RESULT`（`worker.py:1-7`）；Agent 构造只收 model 与 store（`base.py:151-155`），没有 bus / cp 引用；`TaskContext` 只有本任务字段（`base.py:72-82`，「看不到全局状态」`:74`）。现有唯一「agent 听得到别的 agent」的地方是圆桌的只读 `history` 列表（`maos/roundtable/team.py:180`、`:153`），且限于一轮之内。
- 并发原语：主链路 bus→worker→CP→gate **零线程零 asyncio**；并发全在外围 —— ingress 工作线程 `maos/ingress/server.py:70-96`、router 的 `Lock` / `threading.local`（`maos/ingress/router.py:284,288`）、Nacos 事件循环线程 `maos/config/nacos_source.py:188-201`、Matrix `_NioChannel` 私有循环线程 `matrix_bus.py:412-413`、`hiclaw/transition_mirror.py:188-207` 轮询线程、MCP 子进程 pump `maos/tools/mcp/client.py:105-106`。store 连接 `check_same_thread=False` 跨线程共享，靠一把 RLock（`store.py:108-110`）；一次写一 commit，核心 store **没有跨方法事务**（唯一多语句事务在未接线的适配器 `maos/store/sqlite_store.py:231-249`）。
- **异构 worker 的隐患**（本轮实读发现）：role 不在本 worker 池里时，`on_assignment` **不认领就回一条 `failed` TaskResult**（`worker.py:45-47`）；`on_task_result` 既不比对 `payload.worker_id` 与 `task.worker_id`，也不比对 `env.attempt` 与 `task.attempt`（`control_plane.py:351-391`），会把它当真失败：任务 DISPATCHED→PENDING（走的正是 `claim_timeout` 那条迁移）或 RUNNING→PENDING（`retry`），且 `result:<task_id>:<attempt>` 幂等键（`events.py:129`）被它先烧掉，真正认领方稍后交回的结果被当重复丢弃（`:356-358`）。当前不咬人只因所有 worker 都持全池（`worker.py:34`）；一旦「队友类型不同」就是活 bug。

### 5. 七门 G1–G7

| 门 | 拦截函数 | 判什么 |
| :-- | :-- | :-- |
| G1 schema | `maos/runtime/gate.py:296` | 有无 artifact；patch_set 有无 files |
| G2 acceptance | `:307`（代码类 `:339`，非代码类 `:400`） | test_report 或 self_check |
| G3 security | `:438` | diff 里明文凭证（`:443` 四个关键字） |
| G4 evidence | `:449` | summary 变更说明 |
| G5 compensation | `:457`（干跑 `:510`） | 高风险补偿引用可反向应用 |
| G6 finance | `:537`（任务级 `:626`、plan 级 `:671`） | 超阈金额有无 finance_entry；plan 级漏排（`scope=plan` `:738`） |
| G7 gateway | `:748` | 网关回执四象限 → `disposition` |

- 串行还是可配置：**硬串行、顺序写死**在 `_review` 的元组（`gate.py:267-275`），逐门全跑、不短路，聚合 findings 后一次出 verdict（`:284-292`）。没有开关、没有按 role / kind 选门的配置；唯一可调的是 G6 阈值 `MAOS_FINANCE_THRESHOLD`（`:97`）。
- 作用对象：**只看 artifact**（`:262-263` 按 `version == attempt` 取本轮产物），不看消息、不看 Envelope。
- 触发：轮询 AWAITING_REVIEW（`:252-259`），由驱动循环调（`common.py:123`）。

### 6. Evidence Bundle

- 写入时机：`scripts/make_evidence.py::write_bundle` `:567`，由 `build_scenario` `:605` 等在子进程跑完场景后落 `run.log / trace.json / result.json / business-objects.json / kb-*.json`；首行 `# generated at … from <sha>` 由 `header_line` `:142-143` 构造、`write_text` `:220-223` 写入，sha 在 `main` `:910` 动文件前钉住。
- hash-chain：**没有**。`event_log` 表无 `prev_hash` 列（`store.py:160-173`），`append_event_log` 不读上一条（`:367-379`）；仓库里的 sha256 只有两类互不串接的 —— 每条 SkillInvoked / ToolInvoked 的单条摘要（`maos/skills/invoker.py:38-43`、`:139-140`；`maos/tools/port.py:74`）与 span_id 内容寻址（`maos/obs/trace.py:115-121`）。防篡改靠 `scripts/verify.py::check_trace_tree` `:480-509` 整库重放后与 `trace.json` 逐字节比对（`:499-503`），前提是 `export_trace_bundle`（`trace.py:735`）是纯函数；`check_hash_integrity` `:268-340` 逐条比对 detail 摘要。
- 顺序权威：`event_log.seq` AUTOINCREMENT（`store.py:161`），INSERT 在 `SqliteStore._lock` 内串行（`:110`、`:367-379`），单进程下 seq 就是全序。
- 谁能追加：`Store.append_event_log` 是公开方法，生产代码 16 处直接调用（控制面 `control_plane.py:206/224/421/662/910`，`invoker.py:126`，`port.py:61`，`agents/testing.py:89`，四个域的 guard 与 compensate skill，`hiclaw/matrix_bus.py:1141`），外加鸭子类型 sink（`kb/retriever.py:620`、`config/audit.py:75`）。**没有写入方鉴权**，event_log 可信度来自「进程内只有这些代码」，不是签名。
- trace 挂靠：事件 parent 由 `event_log.task_id` 决定（`trace.py:512`），空 `task_id` 挂 plan 根，无 plan 归 `stray_events`（`:694`）。

### 7. Reviewer Gate（HITL）

- 阻塞点三条，都在控制面落 BLOCKED：Gate pass 但 `effect_risk=H`（`control_plane.py:461-465`，`detail.await="human_approval"`）；机器没招了的第三出口 / replan 上限（`_escalate_to_human` `:229-245`，`detail.await="human_decision"` `:90`）；Worker 自报 `blocked`（`:376-379`）。
- 捞人：`HumanApprovalQueue.pending` `gate.py:844-875` 按 event_log 的 `await` 标记 + `effect_risk` 捞；`compensation_tickets` `:877-896`。
- 回灌链：房间 `RoomApprovalBridge.handle_message` `hiclaw/matrix_bus.py:1083` → `queue.decide` `:1100` → `HumanApprovalQueue.decide` `gate.py:898-899` → `ControlPlane.human_decision` `control_plane.py:734-783`：`assert_transition` `:758` → `human:<task_id>` 幂等键 `:760-763` → 驳回先跑补偿 `:769` → `_transit` DONE/FAILED `:778` → `_advance` / `_fail_plan` `:780-783`。**只有批准 / 驳回两个出口**，`human_resume` 零实现（第 1 项）。
- 名单：`MAOS_APPROVERS`（`matrix_bus.py:62`），每条命令现读 `_effective_approvers` `:1114-1129`；越权落 `ApprovalDenied` 事件（`:1130`，常量 `:1027`）；IM 侧同源 `router.py:291-299`。
- 驱动方式：场景脚本在两次 `run_until_settled` 之间手工调 `decide`（`flows/scenario_3.py:38-39`）—— HITL 是「驱动循环停下、人做决定、再跑」，不是异步。
- 已记录隐患：放行不绑定操作人看到的那一版 attempt（`docs/BACKLOG.md:1514`、`docs/refs/cumora-coordination.md:224-236`），多 worker / 异步审批后变活 bug。

### 8. 守卫层

- PreToolUse 注册位置：`.claude/settings.json` —— **被守卫本身拦读**（本会话 `cat .claude/settings.json` 报 `blocked: 该操作触碰受保护面 .claude/settings.json（读取位置）`），行号 UNKNOWN。可证实的：hook 命令是 `python3 "$CLAUDE_PROJECT_DIR/scripts/guard_bash.py"`（拦截报错原文），Read 与 Bash 各被拦一次；受保护面含 `scripts/guard_bash.py`、`.claude/settings.json`、`.contracts.lock`、`scripts/relock_contracts.py`（后两者子代理实测被拦）。三重机制的成文出处 `docs/phases/common.md:12`。
- `.contracts.lock` 指纹校验触发点：只有 pytest 跑 `maos/tests/test_contracts_frozen.py`。`FROZEN_FILES` 只有 `maos/contracts/events.py`、`states.py` 两个文件（`test_contracts_frozen.py:7-10`），文件指纹 `sha256(read_bytes())` `:21`；表指纹 = 空白归一后的 DDL sha256 `:43`，取自临时库 `init_schema()` `:35`，**只校验 lock 里已登记的表**（`:46`），新增表不触发（`docs/parallel/contracts.md:118`）。relock 侧算法 UNKNOWN（`scripts/relock_contracts.py` 被拦读）。
- 对本次改造的含义：`control_plane.py` / `worker.py` / `gate.py` / `eventbus.py` **不在指纹面**，可改；`store.py` 可加表、加方法，不可改既有 DDL；`events.py` / `states.py` / `maos/artifacts.py` / `docs/parallel/contracts.md` 不可写。

### 9. 配置与身份

- Agent 有稳定 ID：`AgentIdentity.agent_id` / `role`（`maos/agents/base.py:59-69`），25 个带 Identity 的类、24 个进 `AGENT_POOL`（`docs/agent-identity.md:7`，表 `:17-43`）。`AGENT_POOL` 是 `role → 类` 的 dict（`base.py:221`，`@register` 以 `identity.role` 为键 `:225`）。**这是「角色目录」，不是「成员名册」**：键是 role 不是 agent_id，值是类不是实例，没有在线状态，没有按名字找同伴的接口。
- Worker 有 ID：`worker_id` 由构造方给（`worker.py:28-30`），落在 `task.worker_id`（`control_plane.py:346`）与 `TaskResult.payload.worker_id`（`events.py:119/135`）。全仓只有 `"w1"`。
- 名册的雏形只在圆桌层，且是写死的：`maos/roundtable/team.py:26` `TEAM_ORDER`（注释「名册顺序就是发言顺序」）、`:31` `TITLES`、`:40` `ROLE_OF`；实例内 `_speakers` 按 agent_id 索引（`:128-131`）；`roster()` `:334`，经 `/team` 渲染（`maos/ingress/router.py:428-438`、`render_roster` `:1336`）。它**复用 `AgentIdentity`**（`team.py:18`，`identity_of` `:82` 从 `AGENT_POOL` 取）但**不继承 `BaseAgent`**，直接 `model.complete`（`maos/roundtable/speaker.py:86-89`），所以这五岗的模型调用**不落 `model_usage`**。成员之间不能互相点名：发言顺序写死轮询（`_round` `:177-183`）；`answer()` `:290` 只由人经 `router._mention` `:842`（`parse_mention` `:1379`）触发。
- 房间身份：每岗可有独立 Matrix 账号（`hiclaw/room_voices.py:57` `env_keys_of`，`own_identity` `:385`），否则主通道代言加名牌 `【title · agent_id】`（`:234-236`）；代价是五岗 = 五条通道五个事件循环（`docs/BACKLOG.md:1676`）。
- 按 agent 分的配置：**没有**。治理键只有 4 个全局旋钮 `maos/config/source.py:96-101`（`GOVERNED_KEYS`），`maos/config/*.py` 里 role / agent_id 零命中；agent 差异化只在代码里的 `AgentIdentity`。

---

## 第二步：对标差距

判定口径：已有 = 现在就能用；部分有 = 机制在、缺一半；没有 = 零实现。

| # | 能力 | 判定 | 对应位置 | 缺什么 |
| :-- | :-- | :-- | :-- | :-- |
| 1 | 共享任务列表（三态 + 依赖 + 自动解阻塞） | **部分有** | 任务表 + `depends_on`（`store.py:127-147`）；解阻塞 `dispatch_ready` `control_plane.py:296-315` + `_advance` `:923-937`；8 态可折三态（pending = PENDING∪DISPATCHED，in_progress = RUNNING∪AWAITING_REVIEW∪REWORK∪BLOCKED，completed = DONE∪FAILED） | 列表是 **per-plan** 的（`store.list_tasks(plan_id)` `store.py:288-293`），没有跨 plan 的「任务板」查询；解阻塞是 DONE 后重扫，不是被阻塞任务被通知 |
| 2 | 双模分配（lead 指派 + 队友自认领） | **部分有** | 指派 = `TaskAssignment.payload.role`（`events.py:100`）按 role 路由；认领 = `ControlPlane.claim` `control_plane.py:320-346`，DISPATCHED 本义就是「等 Worker 认领」（`states.py:13`） | 指派只到 role 不到具体成员；认领是「谁先收到派发谁领」，不是「队友自己挑未分配且未阻塞的任务」；没有「未分配」概念（`task.worker_id` 只在 claim 后写，`control_plane.py:346`） |
| 3 | 认领防竞态 | **部分有** | `claim:<task_id>:<attempt>` 幂等键走 `processed_key` 主键冲突（`control_plane.py:342-345`；`store.py:394-415`），并发下只有一个拿到 None；状态校验前置的理由 `:321-336` | 没有租约 / 超时：`claim_timeout` 零实现（`states.py:29`）；认领方硬崩即永久 RUNNING（`docs/BACKLOG.md:1510`，`docs/refs/cumora-runtime.md:222-230`）；PG 上无 RLock 等价物（`BACKLOG.md:1511`） |
| 4 | 点对点消息（按名字发、自动投递、免轮询） | **没有** | Envelope 无收件人字段（`events.py:48-65`）；总线按 topic 广播（`eventbus.py:66-68`）；Worker 只发 TaskResult（`worker.py:73-79`）；Agent 无 bus 引用（`base.py:151-155`） | 全部。最近的替代物：`TaskAssignment.payload.inputs`（`events.py:85`，lead→worker 单向）、`rework_findings` 回灌（`control_plane.py:312`）、圆桌一轮内的只读 `history`（`team.py:180`）。房间层 `@点名` 是人→agent（`router.py:1379`、`:842`），不是 agent→agent |
| 5 | 空闲通知 | **没有** | 最接近的是 `TaskResult` 本身：交回即触发 `dispatch_ready`（`control_plane.py:389`、`:468`） | 没有 idle 概念、没有「我空了」事件；Worker 没有主循环，不存在「即将闲置」时刻（`worker.py:38-60` 是被动回调） |
| 6 | 独立 context（不继承 lead 历史，但加载项目级配置） | **已有** | `TaskContext` 只含本任务字段（`base.py:72-82`）；`ask()` 每次独立 `complete(system,user)`（`base.py:194-195`；接口 `maos/model/client.py:49-51` 无 messages/history，`GatewayModelClient.complete` `:252-256` 每次现拼两条）；项目级配置走 `maos.config` 配置面（`control_plane.py:615`、`gate.py:189`） | 无。注意「独立」是彻底的：连同一 plan 里兄弟任务的产物都看不到（`docs/refs/cumora-coordination.md:196-213`） |
| 7 | 计划审批（只读出计划 → 批准才许写 → 驳回带反馈重提） | **部分有** | `PlanState.PENDING` 就是「建了没跑」（`states.py:47,54`），`create_plan` 与 `start_plan` 是两步（`control_plane.py:250-287`、`:289-291`）；Manager `allowed_tools` 为空、`write_scope={plan,task}`（`manager.py:39-40`）；带反馈重规划的回调已存在（`_replan` `:632-679`、`Replanner` `:143`、`_apply_replan` `:681-729`） | 所有场景 `create_plan` 后**立刻** `start_plan`（如 `flows/scenario_1.py:127-133`），中间没有审批停靠；`create_plan` 不校验调用方 Identity；plan 级只有事后的 G6 plan 判据（`gate.py:671-744`）没有事前审批队列 |
| 8 | 生命周期（spawn / 优雅 shutdown / 可拒绝并说明理由） | **没有** | `WorkerRuntime` 无 start/stop（`worker.py:27-36`）；只有总线与房间通道有 `close()`（`eventbus.py:507-518`、`matrix_bus.py:826`） | 全部。「拒绝 shutdown」需要 agent→lead 回话通道，即依赖第 4 项 |
| 9 | 信任边界（agent 间消息标记「来自 agent」；被拒操作不能转发绕过） | **部分有（靠结构免疫）** | 三查 `PermissionDenied` 运行时强制（`base.py:161-178`、`invoker.py:61-66`）；`TaskResult` 带 `worker_id`（`events.py:135`）；`SkillInvoked` 带 `invocation_id` 做 actor 溯源（`invoker.py:142`）；人工决策带 `operator`（`control_plane.py:775`）；房间越权 `/approve` 落 `ApprovalDenied`（`matrix_bus.py:1130`）；监听侧忽略自家 bot（`room_voices.py:274`、`matrix_bus.py:330`） | 「免疫」只因 agent 之间没有通道。有通道后要补：消息 `from_agent` 由投递层写、不信发送方自述；收件方对「请你帮我调 X」不得继承发送方白名单 —— 三查按**执行者**的 Identity 判（`base.py:161`）本来就对，但要加测试钉住；再堵上第一步第 2 项那个「Agent 拿得到裸 store」的口子 |
| 10 | 生命周期 hook（任务创建 / 完成 / 即将空闲，可否决并回传） | **没有（可否决的挂点零个）** | 现有回调 4 处且都不能否决：`before_review`（`common.py:108-122`，只能补产物）、`decision_hook`（`flows/scenario_7.py:566-570`，人类决策注入）、配置订阅 `maos/config/source.py:157-181`（异常吞掉）、圆桌钩子 `on_preflight/on_sheet/on_execute`（`router.py:381-405`，回帖发出**之后**才 fire，异常吞成 warning） | 任务创建 / 完成的可否决挂点与回灌。现有唯一「否决 + 回灌」形态是 Gate 的 rework findings（`control_plane.py:504-512`），但那是产物评审不是 hook |
| 11 | 成员发现（名册可读、含名字与类型） | **部分有** | `AGENT_POOL`（`base.py:221`）+ `AgentIdentity`（`:59-69`）能回答「有哪些 role、各自能干什么」；圆桌 `roster()`（`team.py:334`）经 `/team` 可读（`router.py:428-438`） | 没有**实例**级名册：谁在线、谁忙、`worker_id` 是什么、一个 role 有几个成员。圆桌名册是写死的 `TEAM_ORDER`（`team.py:26`）不从 `AGENT_POOL` 枚举；`members / teammates / directory` 全仓零命中 |

---

## 第三步：改造方案

### 3.0 总原则（不动冻结面）

- 不加状态、不加迁移、不加 Topic、不加 Envelope 字段（铁律 1 / 9；`events.py` / `states.py` 在指纹面 `test_contracts_frozen.py:7-10`）。
- 新概念一律落成**新表 + `event_log` 自由 `event_type` + 新模块**，与 `SkillInvoked` / `ConfigChanged` 同一条路（`maos/config/audit.py:9-27`）。
- Control Plane 仍是唯一状态写者（`docs/architecture.md:109-110`）：新模块可以**读** store、可以**请求** CP 的公开方法，不许写 task / plan。

### 3.1 按模块归档

| 模块 | 类别 | 改动 | 影响文件数 | 破坏现有契约 | 数据库迁移 |
| :-- | :-- | :-- | :-- | :-- | :-- |
| 任务表 / 状态机 | **复用** | 8 态折三态只是视图；`depends_on` / `worker_id` 直接用 | 0 | 否 | 否 |
| `ControlPlane.claim` | **扩展** | 加租约：认领时写新表 `claim_lease(task_id, attempt, worker_id, expires_at)`；新增 `reap_expired_leases()`：过期则合成一条 `failed` TaskResult（`error="lease_expired"`）走既有 RUNNING→PENDING(`retry`) 或 DISPATCHED→PENDING(`claim_timeout`），两条迁移都在表里（`states.py:29,32`） | 2（`control_plane.py`、`store.py` 新表 + 方法） | 否 | **新增表**，不改既有表 |
| `ControlPlane.on_task_result` | **扩展** | 加两条只读校验：`env.attempt == task.attempt`、`payload.worker_id == task.worker_id`，不符则落 `StaleResultDropped` 事件并短路。修第一步第 4 项的异构 worker 隐患 | 1 | 否 | 否 |
| 任务板查询 | **新建** | `store.list_claimable(roles) -> [task]`：跨 plan 查 DISPATCHED 且无有效租约的任务；`Store` 加抽象方法 | 1（`store.py` 仅加方法） | 否（方法不是 DDL；但 `store.py` 是 T3/T29 的面，派单要点名授权） | 否 |
| `WorkerRuntime` | **扩展** | ① role 不匹配时**静默跳过**而不是回 failed（`worker.py:45-47`）；② `WorkerRuntime(roles=…)` 只装子集 = 异构队友；③ `pull_and_claim()`：按名册里自己的 roles 从任务板挑一条 → `cp.claim`；④ `start()/stop(reason)`；⑤ 完成一条后落 `WorkerIdle` 事件行 | 1 | 否（C-4 断言的是 `build()` 返回的那个 `worker_id=="w1"`，新增 worker 不经 `build()`） | 否 |
| Mailbox（点对点） | **新建** | 新建 maos/core/mailbox.py + 新表 `agent_message(msg_id, plan_id, task_id, from_agent, to_agent, kind, body, created_at, read_at)`；`send()` 由投递层写 `from_agent`（取调用者 Identity，不信 payload）；`deliver()` 在 `on_assignment` / `pull_and_claim` 前把未读消息塞进 `TaskContext.inputs["inbox"]`；每条落 `AgentMessage` event_log 行；发送时做 schema + 明文凭证扫描 | 3（新模块、`store.py` 新表、`worker.py` 接线） | 否（`TaskContext` 不在冻结面；若加字段也只是 `base.py`） | **新增表** |
| 名册 | **新建** | 新建 maos/core/roster.py：`register(worker_id, roles, agent_ids)` / `members()` / `find(name)`；idle / busy 由 `claim` 与 `TaskResult` 事件推导（读 event_log，不另存事实，理由同 `gate.py:858-860`）；统一今天的四份身份来源（`AGENT_POOL`、圆桌 `TEAM_ORDER/TITLES/ROLE_OF`、`RoomVoices` 账号映射、`worker_id`）；`/team` 改读它（`router.py:438`） | 2（新模块 + `router.py` 一处） | 否 | 可选新表 `roster_member`（单进程可纯内存） |
| Hook 挂点 | **新建** | 新建 maos/runtime/hooks.py：`HookRegistry.on("task_created"|"task_completed"|"worker_idle", fn)`，fn 返回 `Veto(reason)` 即否决并把 reason 落 event_log。挂在 CP：`create_plan` 落库前、`on_review_verdict` pass 落 DONE 前、`WorkerIdle` 事件后 | 2（新模块 + `control_plane.py` 三处调用） | 否 | 否 |
| 计划审批 | **扩展** | `PlanApprovalQueue`：`create_plan` 后停在 `PlanState.PENDING`（已有）；`approve(plan_id)` → `start_plan`；`reject(plan_id, feedback)` → 调 `Replanner`（`control_plane.py:143`）带 feedback 重出规格 → `_apply_replan`。房间加 `/approve-plan` | 2（`gate.py` 或新文件、`control_plane.py`） | 否 | 否 |
| 七门 | **复用（不作用于消息）** | 见 3.2 Q4 | 0 | 否 | 否 |
| Evidence / trace | **扩展** | `trace.py` 给 `AgentMessage` 建 span（parent 按 `task_id`，`trace.py:512` 已覆盖）；`verify.py` 加一项「消息 `from_agent` 在名册内且摘要与库一致」 | 2 | 否 | 否 |
| 房间镜像 | **复用** | `summarize` 对未知 event_type 有兜底（`matrix_bus.py:170`）；消息若也进房间加一行人话即可 | 1（可选） | 否 | 否 |

### 3.2 五个必答问题

**Q1 点对点消息：扩展 EventBus 加 recipient，还是新建 mailbox？—— 新建 mailbox。**

- 扩展总线要么改 `Envelope`（冻结，`events.py:48-65`），要么把收件人塞进 `payload` 并新起 topic 字符串。后者技术上可行（`publish(topic: str)` 不校验 topic 集合，`eventbus.py:47-49`；测试没钉 topic 集合），但撞上成文纪律「不加新 Topic」（`audit.py:9-27`），且总线是广播语义（`eventbus.py:66-68`）：N 个收件人 = N 个订阅者各自过滤；RocketMQ 后端还要预建 topic（`eventbus.py:332`）。
- 对现有订阅者的破坏面：扩展总线 = 三个订阅者都要学会忽略不是给自己的消息（CP `control_plane.py:188-189`、Worker `worker.py:35`、Matrix 镜像 `matrix_bus.py:946`）；mailbox = **零**，现有订阅者一行不动。
- mailbox 怎么做到「免轮询」：单线程 drain 模型里，「自动投递」= Worker 在下一次拿到执行机会时（`on_assignment` 入口或 `pull_and_claim`）由 runtime 把未读消息塞进 `TaskContext`，Agent 自己不轮询；收件人不在线时消息躺在表里等它下一次被调度。真正的推送要等并发化（末节）。

**Q2 自我认领 vs 状态权威：不冲突。**

- 现在的认领就已经是「Worker 发起、CP 裁决」：Worker 收到广播后调 `cp.claim`（`worker.py:50`），CP 校验状态 + 烧幂等键 + 迁移（`control_plane.py:339-346`）。自我认领只是把「Worker 发起」的触发源从「收到派发」换成「自己从任务板挑」，裁决链一字不变。
- 调用链：`Worker.pull_and_claim()` → `store.list_claimable(roles)`（只读） → 挑一条 → `cp.claim(task_id, worker_id, attempt)`（唯一写口） → 成功则 `agent.run` → `_reply` TaskResult。**任务板是只读投影，认领是 CP 的方法**，不变量「只有 CP 写 state」保住。
- 还要堵一个口：lead 显式指派也必须经 CP ——「指派给某成员」不是新状态，写进 `task.inputs["assignee"]`（已有 JSON 列）或新表 `task_assignment`，由 `list_claimable` 过滤；`dispatch_ready` 不改。
- 派发与认领并存的语义：`dispatch_ready` 仍把 DISPATCHED 广播出去（`control_plane.py:309-313`），持该 role 的 worker 可以立刻领；没人领的留在任务板上等 `pull_and_claim`。两条路终点都是 `claim`。

**Q3 agent 粒度：复用 `WorkerRuntime`，不建更重的实体。判据三条。**

- 判据一：Agent Teams 的「队友」= 独立上下文 + 稳定名字 + 能领任务 + 能收发消息。前两条 `WorkerRuntime` + `AgentIdentity` 已具备（`worker.py:28-30`、`base.py:59-69`、`:72-82`）；后两条是本方案给 `WorkerRuntime` 加的方法，不需要新类型。
- 判据二：更重的长生命周期实体（常驻线程、自带主循环）真正带来的只有「并发」与「推送」，而这两样在 MAOS 里归总线与驱动循环（`common.py:112` 明写换 RocketMQ 后循环消失、消费者常驻），不归 Worker。把并发塞进 Worker 会出现第二条驱动路径，与 C-3「不许绕过 build() 拼装」同类（`common.py:7-8`）。
- 判据三：仓库里已经有一个「每 agent 一个重实体」的样本 —— 圆桌五岗各开一条 Matrix 通道、一个事件循环、一条守护线程（`docs/BACKLOG.md:1676`），线性涨且堆在启动那一刻。这是反例，不是模板。
- 不建议的反面：让每个 `BaseAgent` 实例自己带 mailbox 与生命周期。Agent 实例按 role 复用（`worker.py:34`），归属靠 ContextVar（`base.py:39-56`），给它加实例态会把 A 任务的收件箱串到 B 任务上。

**Q4 七门的作用域：不作用于 agent 间消息；只借两道的判据。**

- 七门的输入是「本 attempt 的 artifacts」（`gate.py:262-263`），产出是 verdict → 状态迁移（`:288-292` → `control_plane.py:447-517`）。消息不是产物、不改状态，套上七门等于给「说话」发 rework，会让消息也烧 attempt（`control_plane.py:307`）。
- 不该作用的五道：G2 acceptance（无验收标准）、G5 compensation（无落地）、G6 finance（无金额）、G7 gateway（无回执）、G4 evidence（消息本身就是留痕）。
- 该借判据但不进七门的两道：G1 schema 的「形状必须合契约」→ mailbox `send()` 入口校验 `kind ∈ {ask, inform, handoff, shutdown_request, shutdown_reply}`；G3 security 的明文凭证扫描（`gate.py:443` 那四个关键字）→ 消息体也扫，命中即拒发并落 `SecurityEvent`。两者都是**发送时同步拒绝**，不是事后 verdict。
- 真正需要闸的是**消息引发的动作**：收件方据消息去调 skill / tool 时，三查照旧按收件方自己的 Identity 判（`base.py:161-178`、`invoker.py:61-66`）。这一条就是第 9 项「不能转发绕过」的全部实现，不需要新闸。

**Q5 Evidence：agent 间通信要进，且只进 event_log；hash-chain 的顺序问题目前不存在，因为没有 chain。**

- 进：每条消息落一行 `event_type="AgentMessage"`，`detail={msg_id, from, to, kind, body_digest}`，带 `plan_id` / `task_id`（否则掉进 `stray_events`，`trace.py:694`）。`verify.py` 现有的 `check_hash_integrity` 模式（逐条 `detail` 与库比对 + 摘要为 64 位 hex，`verify.py:268-340`）可直接复用。
- 顺序：仓库里**没有 hash-chain**（第一步第 6 项），顺序权威就是 `event_log.seq`（`store.py:161`），INSERT 在 `SqliteStore._lock` 内（`:110`、`:367-379`）。单进程下并发写入被这把 RLock 串行化，seq 即全序，无需另定。
- 将来真要加 chain：链在 seq 上、算在同一把锁内（`prev_hash = hash(row[seq-1])`），并发写入自然串行；跨进程（PG）时这把锁不存在（`BACKLOG.md:1511`），要换成 `SELECT … FOR UPDATE` 的计数行 —— 这是并发化那一轨的账，不是 mailbox 的账。
- 不进的：消息正文不进 `trace.json`（只进摘要），理由同 `_finding_ref` 不带长文案（`control_plane.py:151-159`）；正文在 `agent_message` 表按 `msg_id` 回查。

---

## 第四步：官方实现暴露过的问题 vs MAOS

| 问题 | 判定 | 证据与说明 |
| :-- | :-- | :-- |
| agent 忘记把任务标记为完成 → 依赖它的任务永久卡死 | **天然免疫（现在）／需要额外防护（加租约后）** | 完成不由 agent 标：Worker 交回 `TaskResult` 是 runtime 的 `_reply`（`worker.py:73-79`），Agent 抛任何异常都转成 `failed` 交回（`:62-71`），DONE 由 Gate verdict 触发（`control_plane.py:467`）。真正的卡死口是**认领后进程硬崩**：`claim_timeout` 零实现（`states.py:29`）、幂等键无撤销（`BACKLOG.md:1510`）。加租约即补上；但必须同时给 `on_task_result` 加 attempt / worker_id 校验，否则僵尸结果会污染新一轮（第一步第 4 项） |
| lead 在所有任务实际完成前判定收工 | **天然免疫** | Plan DONE 只在 `_advance` 判「非冻结任务全部 DONE」（`control_plane.py:936-937`），且「全冻结」= FAILED（`:927-935`）；Manager 没有收工权（`manager.py:171-172`，它连派单都不接）。仓库口号就是「所有 Agent 都回复完成 ≠ 业务成功」（`gate.py:9-12`；`verify.py::check_business_outcome` `:821`） |
| 两个 agent 编辑同一文件互相覆盖 | **会同样中招（一旦并行）** | 库里不会覆盖：产物按 `(task_id, version)` 隔离（`store.py:149-158`）。落地面会：`sandbox_git_apply(patch, workdir)` 对同一 `workdir` 没有锁（`maos/tools/sandbox.py:439`，全文件无 Lock），`patch_verifier` 每轮先整个还原再打补丁（`common.py:246-247`）—— 两个 coding 任务并行打同一副本会互相还原。防护：每任务独立 `workdir`（`sandbox_workdir()` `common.py:218-228` 已是按 run 现造，改成按 task 现造）+ 名册级「同一 repo 路径同时只租给一个任务」 |
| agent 遇到错误后停住不自恢复 | **天然免疫（有上限的恢复）** | 失败进状态机重派：`failed` 未耗尽 → PENDING → `dispatch_ready`（`control_plane.py:386-389`）；Gate 不过 → REWORK → 带 findings 重派（`:504-512`）；四条止损防自旋（`:432-446`）。缺 cumora 的「新事实重置预算」那一半（`docs/refs/cumora-coordination.md:245`）；Agent Teams 里「同伴发来新消息」正是新事实，mailbox 落地后可把「收到 handoff / inform」纳入重置判据，但那是复赛后的事 |
| shutdown 缓慢：agent 要先做完当前工具调用才能退出 | **会同样中招（且更慢）** | 单线程同步：`agent.run` 在 `drain` 调用栈里（`eventbus.py:68`），没有取消点；模型调用只有超时（`maos/model/client.py:267-278`），沙箱 pytest 有超时（`sandbox.py:511`）。房间通道的 `close()` 也是等回调跑完（`docs/DECISIONS.md:1604` 改成 `shutdown(wait=True)`）。任何 `stop()` 只能是「下一条任务前停」，不可能中断当前 `run`。防护：`stop` 语义定义为「不再认领」+ 租约到期自然回收，不承诺打断 |
| token 成本随 agent 数量线性增长 | **会同样中招，但看得见 —— 除了圆桌** | 每次 `ask()` 落 `model_usage`（`base.py:208-216`，表 `store.py:194-209`），trace 按 trace_id 聚合（`trace.py:281-330`），`verify.py::check_cost_attribution` `:997` 核归属。**盲区**：现有唯一的「团队」（圆桌五岗）直接 `model.complete`（`speaker.py:86-89`），不经 `ask()`，一行成本都不记。没有预算闸：`ToolPort.rate_limit` 是死字段（`cumora-coordination.md:337-343`）。防护：名册加 `budget_tokens`，`ask()` 前查（`base.py:180`），超了返回 `blocked`（`AgentOutput` 允许，`base.py:120-124`）；圆桌五岗改走 `ask()` |

---

## 可并行的独立大方向（每个都可再拆多轨）

五个方向两两之间只共享「读 store / 调 CP 公开方法」，没有编译期依赖；建议基线钉同一 sha，各自只加文件、少改内核，合并顺序 D1 → 其余任意。

| 方向 | 交付物 | 触碰的既有文件 | 与其他方向的关系 |
| :-- | :-- | :-- | :-- |
| **D1 任务板与认领租约** | `claim_lease` 表、`list_claimable`、`reap_expired_leases`、`on_task_result` 的 attempt / worker_id 校验、`worker.py:45-47` 改静默跳过、`WorkerRuntime(roles=…)`、`pull_and_claim` | `control_plane.py`、`store.py`（加表加方法）、`worker.py` | 地基。D2–D5 不依赖它就能各自跑测试，但「多队友」端到端演示要它 |
| **D2 邮箱与点对点消息（含证据接入）** | 新建 maos/core/mailbox.py、`agent_message` 表、`AgentMessage` 事件、`TaskContext` 收件箱接线、`trace.py` / `verify.py` 各加一项、发送时 schema + 凭证扫描 | `store.py`（加表）、`worker.py`（接线一处）、`obs/trace.py`、`scripts/verify.py` | 独立。D4 的 shutdown 请求 / 应答复用它的 `kind` |
| **D3 名册与信任边界** | 新建 maos/core/roster.py、统一四份身份来源、`from_agent` 由投递层写、「收件方按自己 Identity 三查」与「Agent 拿不到可写 store」的钉死测试、`/team` 改读名册 | `worker.py`（`store=` 改传只读视图或 skills 专用句柄）、`router.py:438`、`maos/tests/` | 独立。D2 的 `to_agent` 校验先用 stub |
| **D4 生命周期、空闲通知与 hook 挂点** | `WorkerRuntime.start/stop`、`WorkerIdle` 事件、新建 maos/runtime/hooks.py（三个挂点 + `Veto`）、CP 三处调用 | `worker.py`、`control_plane.py`（三行调用） | 独立。「拒绝 shutdown」的应答走 D2，D2 未合并前用返回值代替 |
| **D5 计划审批** | `PlanApprovalQueue`（approve → `start_plan`；reject(feedback) → `Replanner` → `_apply_replan`）、场景里 `create_plan` 与 `start_plan` 之间插审批点、房间 `/approve-plan` | `gate.py`（或新文件）、`control_plane.py`（复用 `_apply_replan`）、一个演示场景 | 独立。不动 `PlanState` |

**明确不在本期的**：并发化（多线程 drain / 多进程 worker / RocketMQ 常驻消费者）。理由：它要同时带上并发信号量、spawn 间隔、限流退避、审批绑定展示版本四层（`docs/refs/cumora-coordination.md:162-169`、`:232-248` #1 / #3 / #7），以及 store 并发语义（`BACKLOG.md:1511`）、`task.state` 双用途（`docs/failure-posture.md:76-84`）。它是 D1–D5 之后的独立一轨，不是其中任何一个的一部分；D1–D5 全部在单线程 drain 下可测可演示（两个 `WorkerRuntime` 订阅同一 topic 时，`eventbus.py:66-68` 会顺序调用两个 handler，认领竞态在单线程下就能确定性复现）。

---

## 本轮顺手发现、待记 BACKLOG（铁律 4：本轮只读，未写 BACKLOG）

1. 异构 worker 的 `failed` 回包会烧掉真正认领方的幂等键（`worker.py:45-47` + `control_plane.py:351-391` 无 worker_id / attempt 校验）。当前不咬人（全池 worker）。
2. `claim_timeout`（`states.py:29`）与 `human_resume`（`:40`）两条冻结迁移零实现；`BLOCKED` 只有 DONE / FAILED 两个人工出口（`control_plane.py:757`）。
3. `on_task_result` 不校验 `env.attempt == task.attempt`：一旦有重派，陈旧 attempt 的 `ok` 结果会以旧 version 入库并把任务推到 AWAITING_REVIEW，Gate 随后判「本轮无产物」。
4. Agent 经 `self.skills.store` 拿得到可写的裸 store（`worker.py:34`、`invoker.py:51-53`）；「Agent 永远不直接碰这一层」（`store.py:41`）是纪律不是机制。
5. 圆桌五岗的模型调用不经 `BaseAgent.ask`（`speaker.py:86-89`），不落 `model_usage`，成本视图对它们是盲的。
6. `maos/flows/scenario_11.py:534` 直接 `store.update_task(..., inputs=...)`，编排层唯一的 store 直写点，不落事件。

## UNKNOWN 清单

- `.claude/settings.json` 里 hook 注册的精确行号：被守卫拦读。
- `scripts/relock_contracts.py` 的指纹算法、`.contracts.lock` 的实际内容与表数：被守卫拦读；只能从 `test_contracts_frozen.py` 消费侧反推两套算法，两侧是否一致未直接验证。
- `scripts/guard_bash.py` 的判定逻辑：被拦读；`PROT_PATHS` 含哪些路径只从报错文本与 grep 泄出片段得知。
- `hiclaw/room_demo.py`（969 行）与 `hiclaw/ap_room.py` 未逐行通读，只按 grep 定位了审批桥与发声面挂载点；`room_ingress.py:36` 提到「任务级 `/approve <task_id>` 在本进程无处可落」的具体处置行号未追到。
- `maos/store/pg_store.py` 内部 SQL 未逐行审计（已确认生产链路无人 import）。
- `maos/domain/*/guard.py` 与 `maos/skills/builtin/**` 对业务表的直写是否应受 Control Plane 管辖，属设计判断，本文未下结论（铁律 9 的口径是业务状态归业务对象）。
- hash-chain 的「不存在」结论覆盖 `maos/ hiclaw/ scripts/` 三个目录的 `*.py`；`legacy-ts/`、`review/`、`deploy/` 未扫。
