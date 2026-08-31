# cumora 解析 · 成本账本与可观测 （T43 · 基线 cumora@1e883f6 / MAOS@926aa7b）

## 1. 它是怎么做的

**一次出站模型调用 = `llm_calls` 一行，`purpose` 是纪律旋钮。** 设计契约写死在文件头
（`server/src/agents/llm-ledger.ts:14-29`）：每一笔云端花费必须归到唯一一个
`purpose` + tenant + agent + (可选) run。`purpose` 不是自由字符串，是穷举联合类型
（`llm-ledger.ts:51-76`），加一个新调用点**必须**先往这个枚举里加一项 —— 注释
（`llm-ledger.ts:48-50`）明说这就是让账本保持完整画像的那道纪律。漏接一个新调用点由
CI 守卫 `scripts/guard-llm-tracked.mjs` 兜住（`llm-ledger.ts:22-23`）。枚举本身也做过
拆分决策：`avatar-image`（建 agent 时）与 `agent-image`（agent 用图像工具）刻意分成两个
purpose，理由是「合成一个会让『图像为什么花了 $X』这个 rollup 无法回答」
（`llm-ledger.ts:69-73`）—— 粒度是按**能不能回答问题**切的，不是按技术相似度切的。

**记账绝不能弄坏被记的那次调用 —— 但批量回传是例外。** `recordLlmCall` 全程 try/catch，
失败只 `console.warn` 丢弃（`llm-ledger.ts:152-160`），文件头把这条列为设计契约第 2 条
（`llm-ledger.ts:17-18`）。唯一反过来的是 `recordLlmCallsBatch`：它**故意抛**，好让外层
授权事务把整批行一起回滚（`llm-ledger.ts:162-168`）。同一模块里两种相反的错误纪律，
分界线是「这行记账是不是正处在一个必须原子的事务里」。

**云端路径靠 Proxy 自动挂账，不靠调用点自觉。** `getTrackedLlmClient(ctx)` 返回一个
分层 Proxy，只拦 `responses.create` / `chat.completions.create` / `images.generate`，
其余（embeddings、audio、beta）原样透传（`llm-ledger.ts:310-358`）。拦到的调用自动量
latency、解析 usage、写一行。**流式是刻意的空洞**：`stream: true` 时直接把流交回去、
一行都不写（`llm-ledger.ts:255-260`），因为 usage 不在同步返回值上而在
`response.completed` 事件里；流式调用点自己在流消费器里调 `recordLlmCall`
（`llm-ledger.ts:24-29`），用 `readStreamUsage`（`llm-ledger.ts:197-211`）取最终 usage。
注释还给了不写占位行的理由：失败的流会产生两行（这里一行 + `finishAgentRun` 一行），
把 per-purpose rollup 弄脏（`llm-ledger.ts:256-257`）。失败也记账 —— `classifyLlmCallError`
把异常归成 `rate_limited` / `timeout` / `failed`（`llm-ledger.ts:184-195`），错误串截 500 字
入库（`llm-ledger.ts:138`）。

**BYOA 路径：服务端看不到 key，账靠 daemon 回传，防线是「身份与归属」而不是「数字」。**
两个端点：`/runtime/triage` 单条（`server/src/agents/runtime/server.ts:496-550`）、
`/runtime/llm-calls` 每跳批量（`server.ts:560-654`）。防谎报是四层，且**没有一层去验 token 数**：
(1) `companyId` / `agentId` 一律取自 JWT（`server.ts:626-627` 的 `c.companyId` / `c.sub`），
客户端根本没有声明身份的字段；(2) `withRuntimeAgentRunAuthorization` 在一个事务里先
`FOR SHARE` 锁住 participant 行确认这个 agent 还在职，再 `FOR UPDATE` 把声明的 runId 全
锁出来，**取回条数与声明条数不等就整批 ROLLBACK 返回未授权**
（`server/src/agents/runtime/authorization.ts:37-63`，判据在 `:57-60`）；(3) `purpose` 走白名单，
未知值 coerce 成 `agent-turn`，注释明说是「不让未来某个 daemon 版本把自由字符串塞进
rollup」（`server.ts:614-622`）；(4) 批量上限 100 跳，超了 413（`server.ts:53`、`server.ts:589-591`），
外加逐字段类型校验，任一不合法整批 400（`server.ts:593-613`）。**token 数本身不可验证，
cumora 接受了这一点** —— 因为 BYOA 烧的是用户自己的订阅额度，谎报只污染用户自己看的
账，不影响 cumora 的实际账单，威胁模型和计费场景根本不同。

⚠️ **文件头注释已经和代码不一致，不要照抄注释。** `llm-ledger.ts:31-38` 白纸黑字写着
「BYOA 本地调用**不**进 `llm_calls`，那会弄脏 sub2api rollup；真要统一账本是另一次
加法式加宽」。但 `LlmCallSource` 里已经有八个 `byoa-*` 值（`llm-ledger.ts:78`）、
`daemonVersion` 字段就是给 daemon 回传用的（`llm-ledger.ts:106-111`）、`/runtime/triage`
里还有一段注释明说要「**镜像**进通用账本，让 BYOA 本地 triage 和云端花费并排显示在
Observability 页上」（`server.ts:525-528`）。也就是说那次「加法式加宽」后来做了，文件头
没跟着改。README 那句「cloud or BYOA 落进同一本账」描述的是**现在的代码**，不是文件头。

**价格：seed 是估算，只有运维供的才算数；但存在两套互相矛盾的定价时点。**
`cost.ts` 里 seed 了 6 个价位（`cost.ts:52-64`），全部硬写 `verified: false`，注释解释得很
直白：Anthropic 那几档是公开牌价但无法在运行时确认对不对得上具体版本，`gpt-5.*` 是
cumora 自己的云端别名、真实上游费率根本不知道（`cost.ts:44-51`）。只有运维通过
`CUMORA_MODEL_PRICES_JSON` 环境变量供的费率才标 `verified: true`（`cost.ts:69-91`），
其余一律作为估算暴露给 UI（`cost.ts:127-138`）。`priceFor` 做**双向**子串匹配
（`cost.ts:95-109`），注释里记着这条是修 bug 修出来的：只做单向时裸 id `haiku` 匹配不上
seed key `claude-haiku`，掉进 fallback 被按 sonnet 价算（`cost.ts:98-101`）。
真正的裂缝在**什么时候定价**：`llm_calls` 的 `cost_usd` 在 INSERT 时就算好冻结
（`llm-ledger.ts:126-129`，注释承认「改价只影响新行，要回填就一条 UPDATE」），
而 `getTriageEconomics` 反过来 —— **查询时**用当前价格 × 存的 token 数重算，
不读存的 `cost_usd`，好让改价立刻重定价全部历史（`observability.ts:458-463`），
`getWakeEconomics` 同样口径（`observability.ts:673-676`）。同一个仓里两套时点并存，
背后是同一条真理：**token 数是事实，价格是可变的解释**。

**可观测层不是 span 树 —— 是两张事实表 + 三个固化的经济学查询。**
`observability.ts` 809 行里没有任何 trace/span 概念：写侧是 `agent_runs`（一次唤醒一行，
`:45-71` 建、`:170-228` 收口）和 `agent_events`（run 内的事件流，`:73-104`），
读侧是三个直接回答业务问题的聚合。写侧有两处纪律值得单拎：一是 daemon 供的写入一律
把「所有权判据放进同一条 SQL 语句」，注释说明理由是「路由层检查与真正写入之间会有窗口，
猜中的 run id 不能借这个窗口写事件或推别人的 stage」（`observability.ts:106-109`、
`:181-189`）；二是入库前统一 `clip` —— 字符串 24k、JSON 160k、数组只留 50 项、
对象只留 80 键、递归 8 层（`observability.ts:12-38`），观测数据永远不许把库撑爆。
读侧三问：`getTriageEconomics`（小脑 triage 到底比它挡掉的大脑轮次省不省，
`:441-450`）、`getWakeEconomics`（唤醒了却一句话没说的比例和花费，`:687-706`）、
以及 `llm-ledger.ts` 那一组 rollup。**它们对反事实极度克制**：triage 那套只有一个
反事实量 `estimatedNetSavingsUsd`，用每个 agent 自己近期的平均轮次成本作为「省下的一轮」
的价值，并在 UI 里标成估算（`observability.ts:441-445`）；wake 那套干脆**拒绝**做反事实，
注释写「它报告发生了什么，不报告本可以省下什么」，唯一建模的量就是那列美元
（`observability.ts:699-701`）。还有一处口径洁癖：0 token 的孤儿 run 不计入静默唤醒，
理由是「它压根没到达模型，算成静默唤醒会把这个数字灌水」（`observability.ts:713-718`），
测试里专门钉了这条（`server/src/__integration__/wake-economics.test.ts:137-141`）。

**账本的规模包袱是真金白银测出来的，解法是预聚合而不是索引。**
建表注释记录了实测根因：`llm_calls` 约 47 万行、日增约 7 万，30 天窗口就是整张表，
所以看板那 6 个聚合每个都是全表扫描且互相争抢，墙钟 5–25 秒 ——「当 N 天就是整张表时，
没有索引能剪枝 last N days」（`server/src/db/migrate.ts:303-308`）。解法是维护一张小时桶
rollup 表，粒度取所有看板查询需要的最细一档（hour × company × agent × purpose × model ×
source × daemon_version），30 天约 3 万行、快 15 倍，6 查询扇出从 5–25 秒降到约 230 毫秒
（`migrate.ts:310-315`）。此后所有看板查询读 rollup（`llm-ledger.ts:457-459`、`:530-533`），
只有下钻看原始调用行才碰 `llm_calls`（`llm-ledger.ts:852`）。upsert 键用 PG15 的
`NULLS NOT DISTINCT` 唯一索引，因为可空的 company/agent/daemon **是身份的一部分**，
不这样两条 NULL-company 行会重复成两个桶而不是并成一个（`migrate.ts:317-319`、`:340-342`）。
时间列另配 BRIN 而非 btree，理由是这张表只追加且物理有序（`migrate.ts:2311-2318`）。

**告警对成本是零覆盖 —— 这一点必须和「克制」分开说。** `metrics.ts` 的计数器注册表
14 项，全是 email 收发 / 重试 / GC 和 DB GC（`metrics.ts:25-39`），**没有一个 LLM 或成本
相关**；暴露端点在 `METRICS_BEARER_TOKEN` 未设时直接 404，避免裸奔
（`metrics.ts:18-20`）。`alerting.ts` 只接进程级 `unhandledRejection` / `uncaughtException`
（`alerting.ts:1-7`），三条设计目标是绝不抛、绝不阻塞、按 `<label>:<消息前 80 字>` 指纹
去重（`alerting.ts:9-24`），指纹粒度的取舍也写清了：粗到能合并栈位移的重复、细到不会把
无关崩溃并成一条（`alerting.ts:18-21`）。`alert.ts` 的 Discord webhook 明说「省着用，
只发几分钟内你真想知道的事」（`alert.ts:8-11`）。所以「克制」是真的，但**成本这一面根本
不在告警范围内**：没有预算阈值、没有花费突增检测。免费档限的也全是结构性上限 ——
3 个 workspace、10 个活跃 agent、5 个人类成员（`server/src/__integration__/free-tier-limits.test.ts:97-165`），
**一条 token 或花费配额都没有**。账本是**事后可见性**，不是**事前闸门**。

## 2. MAOS 的对应物

| cumora 的机制 | MAOS 的对应物（含文件路径） | 结论 |
| :-- | :-- | :-- |
| `llm_calls` 一次调用一行，21 列（`migrate.ts:262-284`） | `model_usage` 表，13 列（`maos/core/store.py:194-209`） | **同构**，但 MAOS 少 8 类字段（见 §3 #1/#2/#3） |
| `purpose` 穷举枚举 + CI 守卫强制（`llm-ledger.ts:48-76`） | `call_site` 自由字符串（`maos/core/store.py:200`、写入见 `:508-511`） | **形似神不同**：MAOS 无枚举约束、无守卫，新调用点漏接不会有人发现 |
| Proxy 自动挂账，忘了包就 CI 报（`llm-ledger.ts:245-359`） | 手工调 `record_model_usage(...)`，且 `store=None` 直接静默跳过（`maos/core/store.py:508-524`） | **形似神不同**：T32「六处补 store=」补的正是漏接（`maos/flows/scenario_7.py:475-479`） |
| 记账失败绝不弄坏调用（`llm-ledger.ts:152-160`） | 同 —— 落库失败只 warning 不抛（`maos/core/store.py:519-521, 536-538`） | **同构**，连「必须留声，静默吞掉等于成本凭空偏低」的理由都一致 |
| 缓存分层 token：`cached_input_tokens` / `cache_creation_tokens`（`cost.ts:19-28`） | 无 —— 只有 `tokens_in` / `tokens_out`（`maos/core/store.py:203-204`） | **MAOS 没有** |
| provider usage 双口径映射（OpenAI 含缓存需减、Anthropic 已分离，`cost.ts:166-193`） | 单口径 `prompt_tokens` / `completion_tokens`（`maos/model/client.py:235-239`） | **MAOS 没有**，接第二家 provider 时会算错 |
| 价格表 + USD 计价 + `verified` 分级（`cost.ts:44-64, 127-138`） | **全仓没有任何价格表、没有一处 USD**（`grep -rni 'per_1m\|price\|usd' maos/` 无命中） | **MAOS 没有**：只记 token，从不换算成钱 |
| `cost_estimated`＝价格是猜的（`cost.ts:35-37`） | 无对应物 | **MAOS 没有** |
| `measured`＝provider 报没报 usage（`llm-ledger.ts:99-101, 124`） | `estimated` 字段 —— 语义是 token 数是不是编的（`maos/obs/trace.py:283-291`：Scripted 用 `len(user)//4`） | **形似神不同**，两侧的 `estimated` 不是同一件事，别对齐错 |
| `status` / `error`：失败调用也占一行（`llm-ledger.ts:184-195`） | 无 —— 只有成功路径调 `record_model_usage` | **MAOS 没有**：失败前烧掉的 input token 完全不落账 |
| `source`：`cloud` / 八个 `byoa-*`（`llm-ledger.ts:78`） | 无 —— MAOS 目前只有一条执行路径 | **MAOS 没有**（尚不需要，但接外部执行体时就是同一题） |
| BYOA 回传的四层闸门（`server.ts:614-622`、`authorization.ts:37-63`） | 无对应物；最近的亲戚是 `maos/runtime/gate.py` 的准入闸门 | **MAOS 没有** |
| 归属维度 tenant / agent / run / conversation（`llm-ledger.ts:83-92`） | trace_id / plan_id / task_id / agent_role（`maos/core/store.py:196-200`） | **同构**：MAOS 的四维更贴任务编排，cumora 的更贴多租户 |
| 归属不上的调用：`company_id IS NULL` 当成「个人 key」正常查（`llm-ledger.ts:462-464`） | `trace_id=''` 如实落库并被 `unattributed_usage` **单独点名**（`maos/obs/trace.py:639-648`、写入理由 `maos/core/store.py:514-517`） | **形似神不同，MAOS 这侧更硬**：cumora 把它当一个可选筛选值，MAOS 把它当必须被看见的缺口 |
| 「取不到」与「花了零」必须分开 | `cost.available` + `ZERO_CALLS_NOTE`（`maos/obs/trace.py:299-303, 326-331, 371-387`） | **MAOS 独有且更严**：cumora 全靠 `costEstimated` 一面旗，MAOS 把三种取不到的理由分别写明 |
| rollup 预聚合表（47 万行 → 3 万行，`migrate.ts:303-344`） | 无 —— `list_model_usage` 每次全取按 trace_id 过滤（`maos/core/store.py:468-475`） | **MAOS 没有**，但也**还不需要**（见 §4） |
| `daemon_version` 关联发版与成本回归（`llm-ledger.ts:888-905`） | 无 | **MAOS 没有** |
| 观测数据入库前统一截断（`observability.ts:12-38`） | 无统一 clip；事件 detail 直接落库 | **MAOS 没有**（演示规模下暂不咬人） |
| `agent_runs` + `agent_events` 两张事实表，读侧是固化聚合查询 | `maos/obs/trace.py` 的 OTel 对齐 span 树，读侧是通用树导出（`export_trace`，`:390-397`） | **形似神不同** —— 详见下段 |
| 反事实极度克制：只有一个估算量并标注（`observability.ts:441-445, 699-701`） | 同 —— `ESTIMATED_NOTE` 逐层跟着数字进证据束（`maos/obs/trace.py:283-291, 700`） | **同构**，两边独立得出同一条纪律 |
| 告警：进程崩溃去重告警，成本零覆盖（`alerting.ts:9-24`、`metrics.ts:25-39`） | 无告警层 | **MAOS 没有**（且不该抄，见 §4） |

**关于 span 模型（派单问题 5）**：两边不是同构，是**同一批事实的两种投影**。
MAOS 的 `maos/obs/trace.py` 是通用 span 树 —— 从事件日志重放出父子结构、校验树形完整性
（`check_span_tree`，`:161`），成本视图是挂在树上的**一个可选侧栏**：后端没有
`list_model_usage` 就把 `cost` 标成 `available=false`，树本身照出（`:390-397`）。
cumora 反过来 —— 没有树，只有一张扁平的调用表，加上一组**为具体问题写死的聚合**，
成本是这张表的**主语**而不是侧栏。

方向性结论：**MAOS 的 span 树能重放出 cumora 那样的粒度，缺的不是结构而是字段**。
MAOS 已经有 cumora 没有的东西 —— 真正的父子关系、`by_task` / `by_role` / `by_call_site`
三个维度的确定性排序聚合（`maos/obs/trace.py:306-323, 350-364`）、以及把归属不上的用量
单列不并树的纪律（`:687-688`）。cumora 能答而 MAOS 答不了的三个问题，全部卡在字段上：
「这次花费里有多少是缓存命中的」（缺缓存分层列）、「这一笔值多少钱」（缺价格表）、
「失败重试烧了多少」（失败调用不落账）。反过来，cumora 的 `getLlmCalls` 支持按
`extras->>'hopIndex'` 排序来看一次 run 的逐跳轨迹（`llm-ledger.ts:827`），
那个粒度 MAOS 的 span 树本来就有，且是真树不是排序后的平表。
