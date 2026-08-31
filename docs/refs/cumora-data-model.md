# cumora 解析 · 数据模型与多实例一致性 （T45 · 基线 cumora@1e883f6 / MAOS@926aa7b）

> 参照物：`~/Documents/cumora-ref`（yetone/cumora，MIT，钉 `1e883f6`）。
> 本文所有 `文件:行` 的路径相对 `~/Documents/cumora-ref/`；MAOS 侧路径相对仓库根。
> 本轮不改 `maos/` 任何文件，也不追加 BACKLOG / DECISIONS。

---

## 1. 它是怎么做的

**第一件要说的事：`schema.ts` 不是真相。** drizzle 的 schema 里只声明了 6 张表
（conversations / messages / participants / conversation_counters / calendar_events /
calendar_reminders / calendar_dispatches，`server/src/db/schema.ts:4-132`），而真实的库有
**61 张表**（`grep -c "CREATE TABLE IF NOT EXISTS" server/src/db/migrate.ts` → 61）。
ORM schema 只覆盖那几张需要类型推导的热表，其余全部只存在于 `migrate.ts` 里那个
2000 多行的 DDL 字符串常量（`server/src/db/migrate.ts:8` 起的 `const DDL = ...`）。
读这个仓库时先信 `migrate.ts`，`schema.ts` 只是一个类型投影。

**migration 的形态：没有版本号，只有一份幂等 DDL。** 整个 `migrate.ts` 是一条按时间
追加的流水账 —— 建表全是 `CREATE TABLE IF NOT EXISTS`、加列全是 `ADD COLUMN IF NOT EXISTS`，
每次 boot 整份重放一遍（`server/src/db/migrate.ts:1-8` 的文件头自述「For production we'd use
drizzle-kit migrations; for now we ensure the schema exists via plain DDL」）。这个选择很省事，
代价在 §1 后半段全额付掉了。全文件 **80 条 `ALTER TABLE`**，分布极不均：participants 14 次、
users 10 次、agent_runs 8 次、computers 6 次、messages 5 次、companies 5 次。

**「每次 boot 重放整份 DDL」在生产上炸过至少三次，注释里写得很清楚。**
第一次：两个实例同时 boot，并发 DDL 在系统目录锁上互等，Postgres 报 `40P01 deadlock`
（`server/src/db/migrate.ts:2031-2042`）。修法是把整个迁移包进 **session 级
`pg_advisory_lock`**（`:2051`），只有一个实例真跑，其余排队后发现全是 no-op。
第二次：advisory lock 只解决「迁移 vs 迁移」，解决不了「迁移 vs 活跃写流量」—— 一条
no-op 的 `ALTER` 也要拿 AccessExclusiveLock，在持续写流量下等成死锁，甚至把另一个健康 pod
的 `/api/health` 拖挂，LB 两边都判不健康返回 502（`:2052-2063`）。修法是在拿到 advisory lock
**之后**设 `lock_timeout = '5s'`（跨实例的排队保持无界，DDL 等锁快速失败）。
第三次：`conversations.members` 的 GIN 索引用普通 `CREATE INDEX` 建，构建期锁住热表，
死锁后索引没提交，于是**每个 pod 每次 boot 都重试、谁都起不来**（`:655-660` 的 NOTE 和
`:2260-2279` 的长注释）。修法是把这类重索引挪到 advisory lock 释放之后、事务之外，用
`CREATE INDEX CONCURRENTLY` 尽力而为、失败不致命（`buildConcurrentIndexes`，`:2280-2344`）。

这三次事故还长出了两个很特别的东西。一个是 **sentinel 探针**：DDL 批次撞上 `40P01` / `55P03`
时，先跑一段只读查询确认「最近新增的那批对象都已存在」，是的话就认定这次重放本来就是 no-op，
打个 warning 继续启动而不是崩溃重启（`schemaAlreadyCurrent`，`server/src/db/migrate.ts:2195-2238`）。
它的注释自己承认这套机制反咬过一次：`llm_calls` 建表那天，40P01 走了 sentinel 捷径跳过建表，
结果每个 Observability 请求都 500（`:2213-2219`）。第二个是**归还连接前的 `RESET ALL`**
（`releaseMigrationClient`，`:2153-2179`）—— 迁移会话关掉了 `statement_timeout`、设了 5s
`lock_timeout`，而这条连接是 20 槽连接池的普通成员，`release()` 不重置会话状态，不 RESET 的话
这个进程余生都有一条「没有超时保护、且会用 55P03 打断普通写」的连接在池子里游荡。

**权威事实：Postgres 是 source of truth，但不是所有字段都是。** cumora 对它自己的领域对象
（会话、消息、成员、看板、文档）确实是权威 —— 一条聊天消息不存在「外部权威」，它就是权威。
但在它**确实不拥有**的那些事实上，它做了和 MAOS 铁律 8 同向的区分，只是零散、按字段、写在注释里：

- `participants.status` 是**租约不是真相** ——「Busy statuses are leases, not permanent truth」，
  agent 的每一轮会刷新 `status_updated_at`，读侧发现租约过期就把 busy 退回 `avail`
  （`server/src/db/migrate.ts:449-455`）。
- 人类在线状态是**从活连接推出来的观察**：进程内一个 `Map<userId, count>` 计数
  （`server/src/ws.ts:60-65`），并且 boot 时把上一进程遗留的 `avail` 全部降级为 `resting`
  （`resetHumanPresenceOnBoot`，`server/src/ws.ts:72-115`）—— 因为上一进程留下的 `avail` 是谎话。
- `computers.status` + `last_seen_at`（`server/src/db/migrate.ts:1508-1509`）是外部宿主的心跳租约。
- `email_messages.transport_status` 只有 `queued/sent/failed/received` 四态，权威在 SMTP 服务商，
  库里存的是**服务商回了什么**：`transport_error`、服务商返回的 `smtp_message_id`，
  外加重试记账 `retry_attempts` / `next_retry_at` 和一条只挑「失败且到点」行的部分索引
  （`server/src/db/migrate.ts:1057-1062`、`:1087-1102`）。这就是 MAOS `payment_observation` 的形状。
- `seen-boundary.ts` 的文件头直接把话说死：这是**协调信号，不是正确性不变量**，Redis 挂了就
  fail-open，最坏结果是多一次重复发言，绝不能让 daemon 卡住（`server/src/agents/seen-boundary.ts:17-23`）。

区别在于：cumora 是**按字段、靠注释**做这个区分，没有任何机制阻止某段代码直接把
`transport_status` 写成 `'sent'`；MAOS 把它做成了系统级铁律 + 运行时守卫 + 越权留痕
（`docs/authoritative-facts.md` §2 那四道拦截）。

**多实例一致性：Postgres 管正确，Redis 管快。** N 个无状态 Node 实例各自订阅同一批 Redis
频道（`server/src/ws.ts:871-878`，14 个频道，常量定义在 `server/src/redis.ts:35-63`）。
每个事件都带 `companyId`，WS 侧按它做租户过滤，**没带 companyId 的事件一律丢弃**
（`server/src/redis.ts:65-72` 的约定，`server/src/ws.ts:891-898` 的执行）。三条关键取舍：

1. **投递语义是「至多一次」，而正确性完全不依赖它。** publish 是 fire-and-forget，
   membership 那条路径明说「Redis is the wake accelerator; the committed message is the
   source of truth」，publish 失败只打 warning 不回传错误，免得调用方以为 DB 写失败去重试
   （`server/src/agents/membership.ts:126-140`）；`postDocMentionWake` 同样处理（`server/src/ws.ts:777-779`）。
   Redis 客户端本身配成 `maxRetriesPerRequest: 1` + `enableOfflineQueue: false` +
   `commandTimeout: 2000`，理由是「PostgreSQL COMMIT 之后不许被一个断线的 Redis 客户端无限挂住」
   （`server/src/redis.ts:13-22`）。丢帧的兜底是客户端走 REST 重新拉。
2. **顺序保证不来自 Redis，来自 `messages.sequence`。** 序号由 `conversation_counters` 的
   UPSERT 原子发放（见下）。Redis 侧唯一被显式修掉的乱序是**本地的**：收件人解析要查库
   （`resolveWsEventRecipientUserIds`，`server/src/ws.ts:159-247`），异步的，所以同一个房间的两条
   事件可能倒序送达。修法是一条按 `${companyId}:conversation:${conversationId}` 分桶的
   Promise 串行链，同房间串行、跨房间并行（`server/src/ws.ts:900-928`）。
3. **重复投递靠 SETNX 抢单去重，而且只对副作用去重。** N 个副本都会收到同一条 `message.new`，
   若各自去唤醒 agent pod，pod 会收到 N 份唤醒、K8s API 上还有 N 个 kubectl 打架。
   修法：`SET cumora:wake-claim:<messageId> 1 EX 60 NX`，抢到的那个副本才继续
   （`server/src/agents/scheduler.ts:454-469`）。注意注释里的诚实说明：不去重也是**正确的**
   （drain 会折叠），只是浪费。

顺带一个我认为是真缺口的观察：人类在线计数 `humanConnections` 是**进程内 Map**
（`server/src/ws.ts:65`）。同一个用户在实例 A 和实例 B 各开一个标签页时，关掉 A 上那个会让
A 的计数从 1 → 0 并把状态刷成 `resting`，尽管 B 上还连着。多实例下这看起来会误报离线。
（我没有找到跨实例的 presence 计数器，也没读到相关测试 —— 列在 §5。）

**原子性分三层，边界很清楚。** 这是本轨最值得抄的一节。

*第一层：Postgres 事务 + 行锁 / 条件 UPDATE。* 承载所有正确性不变量。

- **序号发放即临界区**：`INSERT INTO conversation_counters ... ON CONFLICT DO UPDATE SET
  next_sequence = next_sequence + 1 RETURNING next_sequence - 1`
  （`server/src/agents/membership.ts:284-291`）。`ON CONFLICT DO UPDATE` 拿的行锁**持有到 COMMIT**，
  于是这一句同时是「发号」和「把整段临界区串起来」。`cumora reply` 直接靠这个性质在锁内做
  逐字重复检查，闭掉了原来读-写两段之间的 TOCTOU（`server/src/agents/cli.ts:2142-2151` 的注释
  把这个用法讲得很透）。
- **成员数组由 Postgres 自己改，不在 JS 里读-改-写**：`members || to_jsonb(ARRAY[...])`
  和 `members - $2::text`（`server/src/agents/membership.ts:184`、`:244`）。文件头解释了这条为什么
  是血换来的：以前每个调用方都 SELECT 出数组、在 JS 里 splice、整个写回，两次重叠的成员变更
  就是**静默的后写覆盖** —— 而且 `joined` 系统消息照发，于是「记录里加入了，`members @> [agentId]`
  永远匹配不上，那个 agent 从此再也不会被这个会话唤醒」（`server/src/agents/membership.ts:142-157`）。
- **授权谓词写在 UPDATE 的 WHERE 里，不是写在前置 SELECT 里**：路由层的 SELECT 只用来出友好
  错误信息，「它在任何并发 kick / 离职 / 换租户提交的那一刻就已经是陈的」
  （`server/src/agents/membership.ts:159-168`）。而且**故意不做兜底 SELECT** —— 补一条查询会开出一个
  ABA 窗口，让「踢掉又重新邀请」的行为者把一次被拒的写伪装成幂等成功（`:165-168`）。
- **多行加锁按 id 排序**：`SELECT ... WHERE id = ANY($2) ORDER BY id FOR UPDATE`，注释写明
  「IDs are sorted so cross-kicks cannot deadlock by locking actor/target in opposite orders」
  （`server/src/agents/membership.ts:39-63`）。
- **只读授权用 `FOR SHARE`**，让「撤销权限的并发写」必须等这一帧发完
  （`server/src/agents/cli.ts:2168`、`server/src/ws.ts:294`、`server/src/ws.ts:536`）。
- **`pg_advisory_xact_lock(hashtext(a), hashtext(b))`** 用来把「同一对参与者的 DM 创建」跨入口串起来
  （`server/src/ws.ts:695-698`），以及给 doc 提及做 60 秒去抖（`server/src/ws.ts:560-563`）。
- **幂等靠部分唯一索引**：`uniq_messages_client_id ON messages(conversation_id, author_id, client_id)
  WHERE client_id IS NOT NULL`（`server/src/db/schema.ts:45-47`）。两条集成测试盯着它：
  「ack 丢了之后重试一条已提交的消息，返回的是原来那条」和「同一个 clientId 的并发请求只产生一条」
  （`server/src/__integration__/message-delivery.test.ts:49`、`:87`）。

*第二层：Redis + Lua。* 承载**不能进 DB 事务图**的协调状态。

`seen-boundary.ts` 的文件头把「为什么不放库里」写得很值钱：上一版把它放进
`conversation_reads.last_read_at`，而那一列**正是 loadInbox 的游标** —— 把它刷成 NOW() 之后
下一次 loadInbox 返回空，daemon 就静默挂住了。结论是一句可以直接抄走的判据：
**「任何与收件箱游标共享状态的东西，结构上就是不安全的」**（`server/src/agents/seen-boundary.ts:8-15`）。
两段 Lua：单调 SET（`GET` 比较后才 `SET`，并发写永远收敛到较大值，`:33-45`）和一次性 token 的
读-删（`:200-204` 的 `CONSUME_SCRIPT`）。两者**全部 fail-open**，理由同上：这是协调信号，不是不变量
（`:56-62`、`:246-261`）。

*第三层：SETNX 抢单。* 跨副本的副作用去重（`server/src/agents/scheduler.ts:466`、
`server/src/agents/agenda.ts:158`、`server/src/agents/runtime/inproc-client.ts:564`）。

*没有的那一层：乐观锁。* 我在这个仓库里没有找到任何「版本号 + CAS 重试」形态的并发控制。
所有并发要么走行锁 / 条件 UPDATE，要么走唯一索引，要么走 Redis。唯一索引做的是**冲突即失败**
（后写者去读已存在的那条），不是「读版本、算、比对、重试」。

**并发一致性的真相在集成测试里，不在实现里。** `membership-concurrency.test.ts` 1020 行、
24 条用例，覆盖面之细值得单独说：「同时邀请不丢邀请者」（`:133`）、「离开与邀请重叠时两个效果都保留」
（`:161`）、「踢人与邀请重叠不能复活被撤销的成员」（`:184`）、「被撤权的 HTTP 行为者不能完成一条陈旧的
邀请、也不能发出加入通知」（`:251`）、「被撤权的行为者不能完成一条陈旧的文本消息写入」（`:298`）、
「被撤权的邮件回复在调用服务商之前就被拒」（`:340`）、「行为者换租户赢了参与者锁时取消陈旧邀请」（`:507`）、
「目标换租户 / 目标离职 / 行为者离职」三条同构用例（`:541`/`:573`/`:603`）、「被踢的投票发起人不会被
后续的房间计票细节唤醒」（`:655`）、「WebSocket 路由用当前成员关系，只有一条持久化的离场例外」（`:691`）、
「并发踢人不能绕过 --confirm-empty」（`:833`）、「同时邀请同一个 agent 两次只加一次」（`:873`）。
**那条「持久化的离场例外」是整套设计里最漂亮的一处**：成员被移除之后，那条解释「你为什么看不见这个
房间了」的系统消息还得送到他手上，所以 `messages` 上有一列 `delivery_recipient_id`
（`server/src/db/schema.ts:30-32`），WS 的收件人解析里专门有一个 `durable_recipient` 分支，
用 UNION 把这一个人并进当前成员集合（`server/src/ws.ts:203-244`）—— 例外做成了数据，不是做成 if。

**`memory-scope.ts` 的读写契约：一个身份，多个项目。** 文件头直接给了一张真值表
（`server/src/agents/memory-scope.ts:18-24`）：pinned → 永远可见；行上没有 project → 永远可见
（含历史遗留的 `source: null`）；project P 的行只在当前 wake 的 scope 含 P 时可见；跨 P 和 Q 的
wake 就 P、Q 都可见。判据函数只有 9 行（`memoryVisibleInScope`，`:229-238`）。
两条设计取舍写得很清楚：

- **老数据不迁移**。「live writes historically hardcoded `source: null`」，把它们批量塞进某个
  project 是**假隔离** —— 那些行的归属信息从来就没被记录过（`:8-10`）。
- **写入归属不许猜**。`pickWriteProvenance`（`:102-138`）的优先级是「路径里的 project → 显式参数 →
  当前思考的房间」，而最后一档只在**无歧义**时才用：agent 同时在两个 project 的房间里思考时，
  归属回落 GLOBAL 而不是挑一个，因为「混合归属会把笔记钉到错的房间」（`:126-129`）。

---

### 1.x 回到那个大问题：MAOS 的契约冻结是保护还是枷锁？

**判断：是保护，而且是这个项目现阶段最划算的一条铁律。但它保护的是「三周内能交付」，
不是「架构正确」—— 这两件事必须分开说。** 三条理由：

**理由一：cumora 那 80 条 ALTER 里，真·需求增长是少数，大头是返工。**
两类返工特别刺眼。一类是**维度后加**：多租户是补进来的，17 张表同一批 `ADD COLUMN company_id`
（`server/src/db/migrate.ts:528-545`），全部回填成 `'personal'`（`:547-564`），然后发现 agent 域的表
回填错了（应该跟 agent 真实的 company 走，不是占位的 `'personal'`），又写了 6 条 `UPDATE ... FROM
participants` 二次修正（`:566-590`）。另一类是**为了修上一次 migration 而写的 migration**：
participants 的主键从 `(id)` 升到 `(id, company_id)`，用一个内省 `information_schema` 的 `DO $$` 块
实现（`:621-650`）；然后发现这个复合主键**让同一个 agent id 能在多个工作区共存**，而运行时到处
按 id 单独解析 agent，跨租户撞 id 就返回错租户的资料，于是又加了数据修复函数
`renameAgentIdCollisions` + 一条 `WHERE kind = 'agent'` 的部分唯一索引（`:2107-2124`）。
冻结契约把这一整类返工从「改表 + 回填 + 兼容旧行 + 修回填」压成「加一张新表」。

**理由二：冻结的代价在 cumora 里是有标价的，而且贵得离谱。**
`ensureSchema` 现在这一套 —— advisory lock、`lock_timeout`、40P01/55P03 兜底、必须手工维护的
sentinel 清单、`CONCURRENTLY` 旁路索引、归还连接前 `RESET ALL` —— 加起来 200 多行
（`server/src/db/migrate.ts:2031-2344`），**只解决一个问题：一个已经上线、有活跃写流量、多副本
同时启动的库，怎么在不停机的前提下改表**。MAOS 现在没有上线的库，付不起也不需要付这个成本。
反过来说：一旦允许随手改表，这份成本就是迟早要付的，而且是在最不该付的时候（复赛前）。

**理由三：但要诚实说反面 —— 它确实是枷锁，而且已经在两个地方咬到了 MAOS。**
一处是 EventBus：契约里的 topic 名带点号（`maos.task.assignment`），RocketMQ 的 topic 名不允许点号，
契约又不许改，只能在外面加一层名字翻译，`maos/core/eventbus.py:152-170` 的 docstring 自己写着
「这一层是被迫加的，不是设计洁癖」。另一处就是本轨：下面第 3 节的可移植清单里，落点是「动冻结契约」
的条目占了相当比例，判断只能填「复赛后」。

所以准确的表述是：**冻结是保护，代价记在一张明账上（BACKLOG + 本文档），复赛后一次性还。**
枷锁的坏处是慢性的、看得见的、可以排期的；不冻结的坏处是急性的、看不见的 —— cumora 那两次
40P01 让所有 pod 起不来，是硬故障。一人公司 + 三周 + 没有 CI 门禁，选前者，没有第二个选项。

---

## 2. MAOS 的对应物

| cumora 的机制 | MAOS 的对应物（含文件路径） | 关系 |
| :-- | :-- | :-- |
| Postgres 作权威事实（自有领域对象） | 铁律 8：只持有观察与推断，外部状态归外部系统（`docs/authoritative-facts.md`） | **形似神不同** —— 边界由「谁拥有这个对象」划，不是由「谁存了这行」划 |
| `participants.status` 租约 + 读时过期（`server/src/db/migrate.ts:449-455`） | 无。`task.worker_id`（`maos/core/store.py:142`）没有租约时间戳 | **MAOS 没有** |
| `email_messages.transport_status` + `retry_attempts` / `next_retry_at`（`:1057-1062`、`:1087-1097`） | `payment_observation`（带 `poll_count`，见 `docs/authoritative-facts.md` §1） | **同构** —— 都是「外部系统的观察 + 重试记账」 |
| 只有 `payment.observe` 写得进权威终态 | 无对应物；没有任何机制阻止直接写 `transport_status='sent'` | **MAOS 更严**（`AUTHORITATIVE_WRITER` + 越权落事件） |
| `uniq_messages_client_id` 部分唯一索引做幂等（`server/src/db/schema.ts:45-47`） | `processed_key` 表 + INSERT/catch `IntegrityError`（`maos/core/store.py:175-181`、`:377-398`） | **同构** —— MAOS 是第一天就有，cumora 是出事后补 |
| `conversation_counters` UPSERT = 发号 + 临界区（`server/src/agents/membership.ts:284-291`） | `SqliteStore` 单连接 + 进程内 `threading.RLock`（`maos/core/store.py:108-110`） | **形似神不同** —— 同样是串行化，但 RLock 出不了进程 |
| 授权谓词重复写进 UPDATE 的 WHERE（`server/src/agents/membership.ts:159-168`） | Reviewer Gate 在 Python 层判（`maos/runtime/gate.py:235` 的 `ReviewerGate`）；退款域 `_guarded()` 拦 SQL（见 `docs/authoritative-facts.md` 末段） | **形似神不同** —— cumora 把判据下沉到写语句，MAOS 在写语句之上拦 |
| 多行加锁按 id 排序防死锁（`server/src/agents/membership.ts:39-63`） | 无（没有多行加锁场景） | **MAOS 没有** |
| Redis pub/sub 广播 + 14 频道（`server/src/redis.ts:35-63`） | `InMemoryEventBus`（`maos/core/eventbus.py:39-83`）/ `RocketMQEventBus`（`:173` 起） | **形似神不同** —— cumora 是长驻订阅推送，MAOS 是 drain 到空的批式消费 |
| 至少/至多一次 + 下游靠幂等键去重 | 同一条口径写在 `maos/core/eventbus.py:1-10` 的 docstring 里 | **同构** |
| 每事件带 `companyId`，无租户标签一律丢弃（`server/src/ws.ts:891-898`） | kb 的 `tenant_id` 是主键、查询不带租户返回空（`maos/kb/__init__.py:459-460`、`maos/kb/retriever.py:182-183`） | **同构，MAOS 更严** |
| 同房间串行、跨房间并行的 fan-out 队列（`server/src/ws.ts:900-928`） | 全局单线程串行 drain，刻意为之（`maos/core/eventbus.py:8-9`） | **形似神不同** —— MAOS 用「全局串行」换可复现，代价是不能多实例 |
| SETNX 跨副本抢单去重（`server/src/agents/scheduler.ts:466`） | 无（单进程，没有副本） | **MAOS 没有** |
| Redis Lua 单调 SET / 一次性 token，fail-open（`server/src/agents/seen-boundary.ts:33-45`、`:200-204`） | 无跨进程协调信号。但「协调信号 fail-open / 不变量 fail-closed」的区分已存在：端口探测失败退化本地（`maos/store/pg_store.py:58-60`）vs `GuardrailViolation` 不许 catch 成告警（`maos/kb/guardrails.py:60-62`） | **形似神不同** —— MAOS 有这条精神，没写成一句口径 |
| WS 背压两级阈值 + 超限终止（`server/src/ws.ts:56-58`、`:912-925`） | 无。结构上也没有无界扇出 | **MAOS 没有** |
| 有界并发信号量（`server/src/concurrency.ts:25-72`） | 无 | **MAOS 没有** |
| 迁移 advisory lock + lock_timeout + sentinel（`server/src/db/migrate.ts:2031-2063`、`:2195-2238`） | kb 的 `_MIGRATIONS` + `kb_schema_version` 记账（`maos/kb/__init__.py:26-30`），无并发保护 | **形似神不同** —— MAOS 有版本号（更好），没有并发保护（更弱） |
| `CREATE INDEX CONCURRENTLY` 旁路（`server/src/db/migrate.ts:2280-2344`） | 无。`maos/store/pg_store.py` 显式设 `ef_search`（`:86-104`）是同一类「不吃服务端缺省」的思路 | **形似神不同** |
| `memory-scope` 三态过滤：pinned / 无 project / project 内（`server/src/agents/memory-scope.ts:229-238`） | kb 只有 `tenant_id` 一维硬约束（`maos/kb/retriever.py:69`「顺序即语义，最左是 tenant_id」） | **MAOS 隔离更严，但少一个维度** |
| 写入归属歧义时回落 GLOBAL 不猜（`server/src/agents/memory-scope.ts:120-129`） | 结构上不存在此问题：`tenant_id` 是必填主键，缺一即抛（`maos/kb/__init__.py:459-460`） | **MAOS 没有这个问题** |
| `shipping_verifier_not_builder` CHECK：验证人不能是建造者（`server/src/db/migrate.ts:1636-1637`） | Reviewer Gate 的同一条判据，在 Python 层（`maos/runtime/gate.py:235` 的 `ReviewerGate`） | **同构，落点不同** |
| `agent_runs` 成本列含 `cost_estimated`（`server/src/db/migrate.ts:195`） | `model_usage.estimated` 列 + `usage_is_estimated()` 全仓唯一判定（`maos/core/store.py:206`、`:488-505`） | **同构** —— MAOS 第一天就有，cumora 是第 8 次 ALTER 补的 |

---

## 3. 可移植清单

| # | cumora 的做法 | 出处 `文件:行` | MAOS 现状 | 形态 | 落点 | 成本 | 判断 |
|---|---|---|---|---|---|---|---|
| 1 | 记忆三态过滤：pinned 永远可见 / 无归属即全局 / 有归属只在 scope 内可见 | `server/src/agents/memory-scope.ts:18-24`、`:229-238` | kb 只有 `tenant_id` 一维；「跨 plan 通用的政策」与「那一单的经验」召回时同权 | 抄接口（那张真值表直接翻成 Python 判据） | 新增插件（`maos/kb/`，`kb_doc` 是新增表、走 kb 自己的 `_MIGRATIONS`，不碰 `store.py`） | 1.5 人天 | **赛前做** —— 本轨唯一一条既不碰冻结面、又直接改善检索排序的；`maos/kb/retriever.py:69` 的过滤链上加一档即可 |
| 2 | 「协调信号 fail-open，正确性不变量 fail-closed」写成一条明确口径 | `server/src/agents/seen-boundary.ts:17-23` | 两侧行为都已符合（`maos/store/pg_store.py:58-60` vs `maos/kb/guardrails.py:60-62`），但没有一句统一表述 | 抄思想 | 新增插件（口径归并进 `docs/`，不改行为） | 0.5 人天 | **赛前做** —— 成本极低；「我们知道哪些失败该响、哪些该让路」正是评委会追问的那一层 |
| 3 | 「任何与收件箱游标共享状态的东西，结构上就是不安全的」 | `server/src/agents/seen-boundary.ts:8-15` | 无对应记录；MAOS 的 `task.state` 同时是调度游标和展示状态 | 抄思想 | 新增插件（记进设计判据） | 0.5 人天 | **赛前做** —— 一句判据，写下来就有值；不写下来下次还会踩 |
| 4 | 例外做成数据不做成 if：`delivery_recipient_id` + 路由里的 `durable_recipient` UNION 分支 | `server/src/db/schema.ts:30-32`、`server/src/ws.ts:203-244` | 无。MAOS 没有「已经不在 DAG 里的角色仍要收到一条终局说明」这个场景 | 抄思想 | 新增插件 | 1 人天 | **复赛后** —— 好模式，但 MAOS 现在没有需要它的场景，现在加是空转 |
| 5 | 验证人 ≠ 建造者，写成 DB 层 CHECK 约束 | `server/src/db/migrate.ts:1636-1637` | Reviewer Gate 在 Python 层判，没有可以挂约束的表 | 抄思想 | 动内核（要先新增一张「谁建、谁验」的表） | 1 人天 | **复赛后** —— 先得有表才谈得上约束；赛前 Python 层的判定已跑绿，重复实现无净收益 |
| 6 | 多行加锁前按 id 排序，防交叉死锁 | `server/src/agents/membership.ts:39-63` | 无多行加锁场景（单连接 RLock） | 抄代码 | 动内核（未来的 `PolarStore`） | 0.5 人天 | **复赛后** —— 现在没有第二把锁可以交叉 |
| 7 | 授权谓词重复写进 UPDATE 的 WHERE，前置 SELECT 只用来出友好错误 | `server/src/agents/membership.ts:159-168`、`:183-199` | 退款域靠 `_guarded()` 在 SQL 之上拦；判据不在写语句里 | 抄思想 | **动冻结契约**（要给 `refund_case` 的写语句加条件谓词，触碰表结构与守卫面） | 2 人天 | **复赛后** —— 铁律 1；且现在是单进程串行，「陈旧授权」这个窗口结构上不存在 |
| 8 | 「不做兜底 SELECT」：补一条查询会开出 ABA 窗口，把被拒的写伪装成幂等成功 | `server/src/agents/membership.ts:165-168` | `claim_idempotency` 在 `IntegrityError` 后确实做了一次兜底 SELECT（`maos/core/store.py:387-398`） | 抄思想（作为判据复核，不一定要改） | 动内核 | 0.5 人天 | **复赛后** —— MAOS 那次兜底 SELECT 是**必要的**（要返回上次 outcome），语义与 cumora 的场景不同；但值得复赛后确认它在多进程下是否还成立 |
| 9 | 状态租约：`status_updated_at` + 读侧过期退回 | `server/src/db/migrate.ts:449-455` | `task.worker_id` 无租约时间戳，worker 崩了任务永远挂在他名下 | 抄思想 | **动冻结契约**（`task` 表加列）／可退化为新增 `task_lease` 表（铁律 1 允许新增表） | 1 人天 | **复赛后** —— 赛前没有多 worker，租约无处可用 |
| 10 | 外部投递的重试记账：`retry_attempts` + `next_retry_at` + 只挑「失败且到点」行的部分索引 | `server/src/db/migrate.ts:1087-1102` | `payment_observation` 有 `poll_count`，未确认有无「下次何时再看」（见 §5） | 抄接口 | 新增插件（退款域自己的表，不碰 `store.py`） | 1 人天 | **复赛后** —— 演示里观察是同步 poll，不需要后台重试循环 |
| 11 | 迁移用 session 级 advisory lock 串行化，`lock_timeout` 只约束 DDL 等锁 | `server/src/db/migrate.ts:2031-2063` | kb 的 `_MIGRATIONS` 无并发保护；`maos/kb/__init__.py:26-30` 自述演示期库都是 `:memory:` | 抄思想 | 新增插件（`maos/kb/`） | 1 人天 | **复赛后** —— PolarDB 上线且多进程那天才需要；cumora 在这上面付过两次硬故障，值得先记着 |
| 12 | 重索引走 `CREATE INDEX CONCURRENTLY`，事务外、尽力而为、失败不阻塞启动 | `server/src/db/migrate.ts:2260-2344` | 无（SQLite 无此问题） | 抄思想 | 新增插件（`maos/kb/` 的 PG 路径） | 1 人天 | **复赛后** —— 同上，绑定 PolarDB 上线 |
| 13 | 归还池化连接前 `RESET ALL`：会话级设置不会随 `release()` 复位 | `server/src/db/migrate.ts:2153-2179` | `maos/store/pg_store.py` 显式 `SET ef_search`（`:86-104`），未确认是否在同一连接上留下会话状态 | 抄代码 | 动内核（`maos/store/pg_store.py`） | 0.5 人天 | **复赛后** —— 需要先确认 MAOS 是否用连接池；本轨没读到池化代码 |
| 14 | 有界并发信号量（20 行，无外部依赖），把扇出变成背压 | `server/src/concurrency.ts:25-72` | 无。结构上也没有无界扇出（`maos/core/eventbus.py:8-9` 单线程串行） | 抄代码 | 动内核（`maos/runtime/`） | 1 人天 | **复赛后** —— 现在加是空转；等真的并行 worker 落地再说 |
| 15 | 同房间串行 / 跨房间并行的 fan-out 队列 | `server/src/ws.ts:900-928` | 全局单线程串行 drain；RocketMQ 版跨 topic 顺序不保证（`maos/core/eventbus.py:192-194` 如实列出） | 抄思想 | 动内核（`maos/core/eventbus.py`） | 2 人天 | **复赛后** —— 要等真的多实例才有意义；`eventbus.py` 那条已知差异正是这个位置 |
| 16 | 写入归属歧义时回落 GLOBAL，不猜 | `server/src/agents/memory-scope.ts:120-129` | 结构上不存在：`tenant_id` 必填主键，缺一即抛（`maos/kb/__init__.py:459-460`） | —— | —— | —— | **不做** —— MAOS 没有「歧义归属」这个状态，硬搬等于凭空造一个可空维度 |
| 17 | 老数据不迁移进新隔离维度（迁了就是假隔离） | `server/src/agents/memory-scope.ts:8-10` | 结构上不存在：MAOS 没有历史库，演示期库每次新建 | —— | —— | —— | **不做** —— 但这条判据本身要记住，条目 1 落地那天会立刻用上 |

**落点分布（17 条中 15 条有落点，#16 #17 判「不做」无落点）**：

- 新增插件 **7** 条 —— #1 #2 #3 #4 #10 #11 #12
- 动内核 **6** 条 —— #5 #6 #8 #13 #14 #15
- 动冻结契约 **2** 条 —— #7 #9

**判断分布**：赛前做 **3**（#1 #2 #3）／复赛后 **12**（#4–#15）／不做 **2**（#16 #17）。
落点为「动冻结契约」的两条判断都是「复赛后」，符合派单 §6 的硬要求。

---

## 4. 反向清单 —— 它做了但 MAOS 不该抄

判据统一是那一句：*这个设计在解决我也有的问题，还是在解决它的用户量 / 多租户 / 向后兼容才有的问题？*

1. **ORM schema 与真实 DDL 两份来源。** `schema.ts` 6 张表 vs `migrate.ts` 61 张
   （`server/src/db/schema.ts:4-132` vs `grep -c "CREATE TABLE IF NOT EXISTS" server/src/db/migrate.ts` → 61）。
   *判据*：这在解决「drizzle 需要类型推导但历史 DDL 是手写的」这个它自己的历史问题。
   一人公司抄这个 = 两份真相各写一半，且没有任何机制告诉你哪份是对的。MAOS 现在
   `store.py` 一份、`kb/schema.sql` + `_MIGRATIONS` 一份，各自自洽，别再引第三种。

2. **sentinel 探针清单。** `schemaAlreadyCurrent` 那张必须手工维护的「最近新增对象」列表
   （`server/src/db/migrate.ts:2195-2238`，注释自己写着「Keep updating this list whenever a new column lands」，
   并承认 `llm_calls` 那次就是忘了更新导致线上 500）。
   *判据*：它解决的是「已上线库 + 多 pod 同时 boot + 活跃写流量」才有的问题。
   MAOS 没有 pod、没有活跃写流量。抄了就是凭空多一张会腐烂的手工清单。

3. **17 张表同批 `ADD COLUMN company_id` + 两轮回填。**
   （`server/src/db/migrate.ts:528-590`）
   *判据*：这是「单租户上线之后才做多租户」的补票。MAOS 的 kb 第一天就把 `tenant_id`
   放进主键（`maos/kb/__init__.py:459-460`）。该抄的不是补票流程，是**别走到需要补票那一步**。

4. **`participants` 一张表混装 human 与 agent。** 两者的唯一性域根本不同 —— human 用 user_id
   天生跨租户，agent 应当全局唯一 —— 结果复合主键 `(id, company_id)` 让 agent id 跨租户撞车，
   最后靠数据修复函数 + 部分唯一索引补救（`server/src/db/migrate.ts:55-70`、`:621-650`、`:2107-2124`）。
   *判据*：它在解决「一个统一的参与者模型让 UI 好写」这个它的产品需求。
   MAOS 的 agent 角色是代码里的类（`maos/agents/`），不是表里的行 —— 别为了「统一参与者」把它搬进库。

5. **WS 背压两级阈值 + 超限 terminate 连接。**（`server/src/ws.ts:56-58`、`:912-925`）
   *判据*：它解决的是「浏览器长连接 + 高广播率 + 共享 pod 内存」才有的 OOM。
   MAOS 没有面向浏览器的长连接。真正该抄的是它下面那个 20 行的信号量（清单 #14），不是这套阈值。

6. **email / boards / documents / calendar / polls / shipping 六个产品域各自一整套表。**
   （`server/src/db/migrate.ts:1048`、`:1291`、`:1387`、`:1167`、`:1456`、`:1562` 起）
   *判据*：cumora 是产品，MAOS 是引擎。抄域表等于把别人的产品路线图刻进自己的库，
   而铁律 9 明确说业务状态是业务对象自己的字段。唯一例外是 `shipping_*` 那组的**思想**
   （不变量 / 验证 / 证据分三张表，验证人 ≠ 建造者写成 CHECK），已收进清单 #5。

---

## 5. 我没看懂 / 没时间看的

- **`membership-concurrency.test.ts` 只读了 24 条用例名（`:133`–`:1005`），一条正文都没读。**
  本文引用它们只作为「测了什么」的证据，不作为「怎么测的」——「a revoked HTTP actor cannot
  finish a stale invite」具体是怎么造出这个 race 的，我不知道。
- `convene-concurrency.test.ts` 和 `agent-memory.test.ts` 同上，只有用例名。
- **`documents/rooms.ts` 完全没读。** Yjs CRDT 的房间管理、初始状态快照、update 的冲突解决全在那里，
  而 doc 的多实例一致性走的是**另一条路**（不进 `ws.ts` 的租户 fan-out，见 `server/src/ws.ts:867-871`）。
  这一条是本轨最大的空白：cumora 有两套多实例一致性模型，我只解剖了一套。
- **`api/router.ts`（3600+ 行）没读。** 人类侧 `POST /messages` 的幂等实现在那里，我只看了它的
  集成测试断言（`server/src/__integration__/message-delivery.test.ts:49`、`:87`）和 `grep` 到的
  `conversation_counters` 调用点行号（`:2953`、`:3073`、`:3663`）。
- **`migrate.ts` 我实读约 600 行 / 2344 行。** 中段（`llm_calls` / `llm_calls_rollup` / boards /
  documents / polls / push_devices / 2029 行之前的 `renameAgentIdCollisions` 主体）只 grep 了表名和
  ALTER 分布，没读正文。「哪些表被反复 ALTER」这一节的**统计**是可靠的（机器数出来的），
  但每张表的**原因**只有 participants / users / agent_runs / computers / messages / companies
  六张是我真读了上下文的。
- **多实例是不是真的在跑，我没有找到证据。** 没有 k8s manifest、没有副本数配置进我的视野。
  代码里到处是「N replicas」的注释（`server/src/agents/scheduler.ts:454-460`），我按它自述采信，
  但没有独立验证。
- **`humanConnections` 的跨实例缺口是我的推断，不是实测。**（`server/src/ws.ts:65`、`:126-135`）
  我没有找到跨实例 presence 计数器，也没找到相关测试，但不排除在我没读的文件里。
- **MAOS 侧**：`maos/domain/refund/objects.py` 和 `guard.py` 没读，本文对它们的引用全部转述自
  `docs/authoritative-facts.md`，没在源码里复核（清单 #10 的「未确认」由此而来）。
  `maos/kb/retriever.py` 只读了 grep 命中的那几行（`:9-10`、`:69`、`:182-183`、`:342-343`、`:466-467`），
  两阶段检索的完整流程没读 —— 清单 #1 说「过滤链上加一档即可」是基于 `:69` 那条注释的推断，
  真动手前必须先读完 `retriever.py`。
- **MAOS 有没有用连接池，我没查。** 清单 #13 的判断因此挂着一个前置确认。

---

## 附录 A · 顺手发现的 MAOS 问题

（本轮不改 MAOS，也不追加账本；以下三条留给整合轮折进 BACKLOG。）

1. **`maos/core/store.py:1-5` 的开篇承诺对并发语义不成立。** 原话是「换 PolarDB 时只改这一个文件：
   Store 是抽象基类，PolarStore 照着实现同样的方法即可，上层零改动」。但 `SqliteStore` 的隔离
   全靠单连接 + 进程内 `threading.RLock`（`:108-110`），而 PolarStore 上**没有这把锁的等价物**：
   `claim_idempotency` 的 INSERT/catch 还能用（靠唯一约束，`:377-398`），但 `update_task` 的
   读-改-写、以及退款域「写 settled 与插 observation 同事务」（借的正是 Store 这把 RLock，
   见 `docs/authoritative-facts.md` §2 末段）都需要显式事务或行锁。
   建议：那句 docstring 补一句「**并发语义不随文件迁移**」，免得 PolarStore 落地那天按字面理解。
   cumora 在这个位置的解法是清单 #6 #7（排序加锁 + 谓词下沉进 UPDATE）。

2. **kb 的 `_MIGRATIONS` 没有并发保护。** `maos/kb/__init__.py:26-30` 自己说明「演示期的库都是
   `:memory:` 或每次新建，两者的区别看不出来；PolarDB 是持久库，区别就是上线第一天端口通道恒退化」。
   这段说的是**迁移没跑**的后果，还没说**两个进程同时跑迁移**的后果 —— 那正是 cumora 付过两次
   硬故障的地方（`server/src/db/migrate.ts:2031-2042`：40P01 死锁；`:2075-2084`：整批 DDL 一个隐式
   事务锁住 ~30 张表直到提交）。建议记 BACKLOG，与 PolarDB 上线绑定。

3. **kb 缺一个「跨 plan 常驻知识」的维度。** `maos/kb/retriever.py:69` 的过滤链注释是
   「顺序即语义，最左是 tenant_id」，之后没有第二个隔离维度。于是「这条政策所有单子都适用」
   和「这条是那一单的经验」在召回时同权。cumora 的 `pinned` 是一个很便宜的补法
   （`server/src/agents/memory-scope.ts:234`：pinned 无视 scope 永远可见）。
   这一条同时是可移植清单 #1，是本轨唯一判「赛前做」且不碰冻结面的条目。
