# cumora 解析 · 协调与防撞机制 （T40 · 基线 cumora@1e883f6 / MAOS@926aa7b）

## 1. 它是怎么做的

**问题形状先于机制。** cumora 的多智能体协作是「同一台机器上 N 个独立的本地引擎会话，
各自被服务端 SSE 事件唤醒，各自读同一个房间，各自独立决定说什么」
（`docs/COORDINATION.md:31-33`）。作者把失败拆成正交的两类，整份文档都建在这个划分上：
一是 **race collision** —— 两个 agent 同时醒来、都决定发同一句、都 INSERT 进 `messages`，
服务端能靠一次 pre-INSERT 检查抓住；二是 **brain misjudgment** —— agent 看到的状态
是对的，脑子仍然判错，服务端抓不住，只能靠提示词塑形，而提示词有天花板
（`docs/COORDINATION.md:36-45`）。由此得出全篇的总判据，也是最该抄走的一句：
**「代码机制能修的，绝不写成提示词规则；脑子在正确状态面前做出的明确决定，
绝不加代码机制去改」**（`docs/COORDINATION.md:47-49`）。七层防御就是按这条判据分层的 ——
第 1–4 层是基础设施（完全不占脑子注意力），第 5 层是服务端硬闸，第 6 层是廉价模型闸，
第 7 层才是提示词。

**第 1–4 层解决的是「同一个外部配额被 N 个进程同时打爆」，与业务语义无关。**
第 1 层把模型版本钉死在部署环境变量上（`docs/COORDINATION.md:57-69`），起因是本地 CLI
在一次会话中途把默认模型从 opus-4-7 悄悄翻成 4-8，而后者对类提示注入的模式更谨慎、
在多智能体流程里行为不同 —— 不钉死，供应商每发一次模型，所有用户的行为就漂一次。
第 2 层是每台机器的大脑并发信号量（默认 6，`docs/COORDINATION.md:71-88`）：N 个 agent 在
同一次 SSE 扇出里醒来，没有这道闸就会整齐划一地撞上供应商的短窗突发限流
（实测「7 人数数游戏 17 分钟内 130 次限流」）。第 3 层是**确定性**的最小 spawn 间隔
（默认 500ms），它替换掉了更早的 `random(0..1500ms)` 抖动 —— 随机抖动是概率性的，
四个同时醒来的 agent 可以同时掷到低值，仍然整齐撞墙；间隔闸让突发率**按构造**恒为
1/interval（`docs/COORDINATION.md:90-98`）。第 3a 层是同一形状的小脑并发闸（默认 8），
它的存在纯粹是一次事故的产物，见下一段。第 3b 层 AdaptivePacer 在任意一次引擎调用
返回限流时把全局间隔**翻倍**（上限 8s），连续 5 轮干净后**减半**回落，且必须同时挂在
冷启动路径和常驻会话路径上 —— 因为常驻会话的 `session.send` 根本不再经过原来的
spawn 闸（`docs/COORDINATION.md:120-134`）。第 4 层是每个 agent 的限流冷却（60s），
它顺带做一件容易被忽略的事：**把 `byoa_engine_failed` 通知压掉**，因为供应商限流
不是 cumora 的故障，不该冒到聊天里去（`docs/COORDINATION.md:164-172`）；同时**保留未读
收件箱**，让冷却结束后的下一次唤醒自然重试（`docs/COORDINATION.md:175-176`）。

**第 3c 层是唤醒经济学，它决定了「N 条消息 → 几次昂贵推理」。** 首次唤醒起一个 2500ms
的防抖定时器，窗口内到达的唤醒折叠进去，这一轮快照**全部**未读 —— 一串群消息塌成
一次引擎回合而不是 N 次，且判据是内容无关的（`docs/COORDINATION.md:143-147`）。
回合运行中再来的唤醒合并成单次待重跑，重跑会重读收件箱、已处理完就空转。
折叠会伤延迟，所以开了两个逃生口：DM / @提及 / 人类消息在流的安全边界处**注入正在
运行的会话**，让 agent 在任务中途就答；普通群活动只给一条内容无关的「N 条新消息，
瞄一眼，是你的就接」（`docs/COORDINATION.md:150-159`）。另有 20s 的慢轮询兜底，
用来在 SSE 流被静默切断时把漏掉的活捞回来（`docs/COORDINATION.md:161-162`）。

**第 5 层是全篇的核心：seen-cursor freshness preflight，以及它的四个补丁。**
每个 (agent, 会话) 在 Redis 里存一个「已被展示到的最高 peer seq」，10 分钟 TTL，
用 Lua 做原子单调写，两个并发调用永远收敛到较大值、绝不回退
（`server/src/agents/seen-boundary.ts:26`、`:37-45`、`:51-62`）。`cumora reply` 落库前
读这个基线，查 `sequence > baseline` 的非自己消息，有就返回一个 **HELD 信封**（exit code 2），
**把那几条消息原文内联在错误文本里**（`server/src/agents/cli.ts:1948-1990`）。
两个设计点决定了它好不好用：其一，**「展示即已读」** —— HELD 信封自己会把游标推到它
展示的最高 seq（`cli.ts:1971`「Shown ⇒ seen」），所以看过之后**平铺直叙地重发就能过，
不需要任何标志位仪式**；其二，选 Redis 而**不是** `conversation_reads.last_read_at`，
是因为后者是 loadInbox 的 SELECT 游标，把它推到 `NOW()` 会让下一次 loadInbox 返回空行、
守护进程静默假忙（`docs/COORDINATION.md:204-211`、`seen-boundary.ts:8-15`）——
**任何与收件箱游标共享状态的东西在结构上就是不安全的**。作者同样诚实地写下这道闸
**抓不住什么**：Nova 在 Iris 的「5」落库之前就决定发「6」，两人的 preflight 在各自的
INSERT 时刻都通过，这是脑子层面的乱序，服务端无从否决（`docs/COORDINATION.md:216-220`）。

**第 5a 层是一段被撤掉的方案，而它的撤退理由比方案本身值钱。** compose-anchor 是钉在
**回合开始**的时间戳、不被 `glance` 推进的第二道边界，用来堵一个真实的洞：B 的
`cumora glance` 正确地让 B 看见了 A 刚落的帖，但**副作用**是把 B 的 seen 基线推过了 A，
于是 B 的 preflight 判「没有更新的」，重复内容照发（`docs/COORDINATION.md:226-232`、
`seen-boundary.ts:82-99`）。撤掉它是因为它在任何繁忙房间里都**保证第一次尝试必被 HELD**，
哪怕 agent 确实读完了一切 —— 转录里满是「同一个 HELD，那些消息就是我刚 glance 过的
→ send-anyway」，每条回复多烧 1–2 次大模型往返（`docs/COORDINATION.md:234-240`）。
它唯一独占抓到的那类重复，改由不可旁路的逐字重复闸兜住。**一道恒假警的闸，成本是
真的，收益要单独证明。**

**第 5b 层把重复检查放进事务里，第 5d 层把旁路标志变成「承认」而不是「跳过」。**
逐字重复闸在 `pool.connect()` + `BEGIN/COMMIT` 包住的序号申领 + INSERT 块内部、
拿到 `conversation_counters` 行锁之后，重查最近一条非自己 peer 消息与草稿逐字比对，
相同就 `ROLLBACK` + HELD（`docs/COORDINATION.md:246-257`、`cli.ts:2188-2222`）。
放进事务是因为 pre-INSERT 版本有 TOCTOU：相隔 2 秒的两个 agent 可以都通过快照检查、
然后都写进去（`docs/COORDINATION.md:253-257`）。而它**不接受 `--send-anyway` 旁路** ——
理由是「逐字重复前一条 peer 消息没有任何正当用例，哪怕在 DM 里」，与序号闸
（可旁路，因为 agent 可能确实要回一个特定 @提及）形成明确分工（`cli.ts:2188-2196`）。
第 5d 层则是本轨最该被抄走的形状：`--send-anyway` / `--force` 只在服务端**确实给这个
agent 展示过一个 HELD** 时才生效，令牌在展示时写入、消费时用 Lua 做原子 GET+DEL
（`seen-boundary.ts:197-204`、`:222-238`、`:246-261`）。起因是 agent 学会了**抢先**
带上标志省一次往返 —— saga 编完整个故事、零次 glance 直接 `--send-anyway`，
比 nova 发出同一份交付物晚 49 秒，而本该给她看 nova 那条的 preflight 在运行之前就被
绕过了（`docs/COORDINATION.md:309-317`）。第一版加固自己还留了个洞：令牌被「让路」
这个正确动作**存了起来**（只有成功发送才清），3.5 分钟后一个新回合的抢先标志把它消费掉，
一路越过两条没看见的消息（`docs/COORDINATION.md:321-327`）。于是令牌被收紧成
**一个瞬间而不是一段会话**：绑定 HELD 展示过的最高 peer seq（`seq:<n>`），消费时
`cmdReply` 重查是否有更新的、有就令牌作废并返回一个新的 HELD（`cli.ts:1880-1926`）；
回合结束即死（`unmarkThinking` 清掉本回合会话的 `reply:*`）；`cumora ack` 即死
（`cli.ts:1205-1208`）；TTL 从 10 分钟砍到 **2 分钟**，只当崩溃兜底
（`seen-boundary.ts:184-186`）。Redis 出错时 `consumeHold` **反向 fail-open 成 armed** ——
基础设施打嗝必须退化成「今天的行为」，绝不退化成「活干不了」（`seen-boundary.ts:243-245`）。

**第 5c 层是主动唤醒的安全网，它示范了「AI 判断底下压一层确定性地板」。** 聊天安静时，
心跳用便宜 SQL 捞出 [5 分钟, 6 小时] 窗口内的停滞会话，交给分类器判是否 actionable；
判是、且该 agent 抢到 Redis NX 声明（`cumora:nudge:<convoId>`）才唤醒大脑 ——
**每次停滞永远只有一个成员去捅**（`docs/COORDINATION.md:268-275`、
`server/src/agents/agenda.ts:136-174`）。冷却分两档：分类器**说了 yes** 用 45 分钟
（一次就够）；分类器**不可用**时用 5 分钟，因为此时无法把那一个被唤醒 agent 的判断
当作定论（`agenda.ts:115-124`）。分类器 503 时**不是简单 fail-closed** —— 那会让整张
安全网在故障期间静默死掉；而是切出最窄的确定性用例：**恰好一处停滞、别人最后发言、
静默 ≤30 分钟、没有别的卡片/日程**，其余一律仍旧 fail-closed（`agenda.ts:526-564`）。
最后压上 **decline cap**：兜底路径连续 3 次抢到声明而会话没推进，就停止为这处停滞
再触发 —— 三个不同的大脑都说了「不」，再唤醒不会改变结论而钱是真的
（`agenda.ts:140-154`）；计数器在会话里**落任何一条新消息时重置**，新状态 = 新预算
（`agenda.ts:176-183`）。

**第 6 层小脑闸与第 7 层提示词，共同点是「只做一件事，且拒绝枚举场景」。**
小脑是**纯闸**：只判 `actionable` 真假，从不决定谁回、怎么回、说什么 ——
读房间是大脑在回合内自己干的事（`server/src/agents/triage-core.ts:176-182`）。
它的指令是**一条原则而不是清单**：有人类介入或等待 → 永远 actionable（人类对着沉默
伸手是最坏的失败）；唯一压制的是**纯 agent 之间、背后没有权威开放工作**的闲聊；
拿不准就 actionable=true（`triage-core.ts:185-191`）。让这个判断成为**事实**而非猜测的，
是服务端从 DB/Redis 采集、**绝不从消息措辞推断**的信号：worklog 声明（有活跃声明 =
有人在推真活）、人类注意力（消息、表情回应、**读游标活动**三者等价，
`triage-core.ts:211-227`）。而 AI 判断底下压着确定性地板：已声明线程的硬上限
`HARD_LOOP_CAP = 20`，未声明线程用**自伸缩**地板（消息数超过参与的不同 agent 数 =
开始「套圈」= 死循环），agent↔agent DM 每 8 条才跑一次闸（`triage-core.ts:60`、
`:197-209`、`:211-227`）。`HARD_LOOP_CAP` 上方挂着一条罕见的注释：
**「这条兜底被以『AI 原生的优雅』为名删过两次，两次都回归了 —— 不要删」**
（`triage-core.ts:206-208`）。第 7 层的契约是**简洁**：整份系统提示约 5KB，
五条规则、只讲形状（`docs/COORDINATION.md:405-409`、`server/src/agents/glance-protocol.ts:20`）。
支撑这五条的关键设计在 `glance-protocol.ts:8-18`：agent 只能看到**已发布的消息流**
加一个私有游标，**没有**「谁在编、谁排在你前面」的花名册 —— 于是「按位次占坑」
（我是第 3 个声明的所以我发 3）在结构上**不可表达**，那面按场景堆砌的提示词墙
才塌得下来。

## 2. MAOS 的对应物

### 2.1 七层逐层对照（问题 1）

| cumora 的机制 | MAOS 的对应物（含文件路径） | 判定 |
| :-- | :-- | :-- |
| 第 1 层 · 部署级模型钉版（`COORDINATION.md:57-69`） | `maos/model/client.py:301` 从 `ENV_MODEL` 取模型名，走 `maos.config` 配置面 | **同构**（MAOS 本来就显式传模型，不吃 CLI 默认值） |
| 第 2 层 · 大脑并发信号量（`COORDINATION.md:71-88`） | **无。** 驱动循环 `maos/flows/common.py:119-130` 是单线程 `for` | **MAOS 没有**（当下有理，见下） |
| 第 3 层 · 确定性 spawn 间隔（`COORDINATION.md:90-98`） | **无。** `maos/tools/port.py:31` 声明了 `rate_limit` 字段，但**全仓零处执行**，所有实例都填 `""` | **MAOS 没有**（旋钮已存在但是死的） |
| 第 3a 层 · 小脑并发闸（`COORDINATION.md:100-118`） | **无。** MAOS 的「小脑」是零模型的规则闸，不 spawn 进程 | **MAOS 不需要** |
| 第 3b 层 · AdaptivePacer 自适应退避（`COORDINATION.md:120-134`） | **无。** `maos/model/client.py:212-223` 只有超时，没有限流识别与退避 | **MAOS 没有**（多 worker 后必需） |
| 第 3c 层 · 唤醒防抖 / 合并 / 同回合插话（`COORDINATION.md:143-162`） | **无。** MAOS 是派单驱动而非消息驱动，`dispatch_ready` 一次派一个 attempt | **MAOS 不需要**（任务不会「同一件事被叫醒 N 次」） |
| 第 4 层 · 每 agent 限流冷却 + 通知压制（`COORDINATION.md:164-176`） | **形似神不同。** `maos/core/control_plane.py:452-455` 有次数耗尽 → FAILED，但那是**业务判定**耗尽，不是**基础设施**退避 | **形似神不同** |
| 第 5 层 · seen-cursor freshness preflight + HELD（`cli.ts:1948-1990`） | `maos/runtime/gate.py:235-283` 七道闸 → rework；findings 经 `control_plane.py:301` 的 `rework_findings` 回灌下一次派单 | **形似神不同**（见 §2.3 问题 3） |
| 第 5b 层 · 事务内逐字重复闸，不可旁路（`cli.ts:2188-2222`） | `control_plane.py:309-329` 的 `claim` 幂等键 + 状态校验；`control_plane.py:728-731` 的 `human:<task_id>` 幂等键 | **同构**（都是「在拿到锁之后再判一次」+「不可旁路」） |
| 第 5c 层 · NX 声明保证「每处停滞只一个人捅」（`agenda.ts:136-174`） | `control_plane.py:325-328` 的 `claim:<task_id>:<attempt>` 幂等键，保证一次派发只被认领一次 | **同构** |
| 第 5c 层 · 分类器不可用时的**窄确定性兜底**（`agenda.ts:526-564`） | `gate.py:169-188` `_finance_threshold` 读不出数时**回落收严**并告警、不抛 | **同构**（同一条哲学：故障时退化到窄而保守的确定性用例） |
| 第 5c 层 · decline cap + 新消息重置预算（`agenda.ts:140-154`、`:176-183`） | `control_plane.py:452` `max_attempts`、`:577-591` `_max_replan`（默认 2）。**只有硬上限，没有「新事实重置预算」那一半** | **形似神不同** |
| 第 5d 层 · hold token（旁路 = 承认服务端展示过的状态）（`seen-boundary.ts:222-261`） | **无。** MAOS 的 Gate 根本没有旁路标志；人工审批 `control_plane.py:702` 只认 `task_id` | **MAOS 没有**（见 §2.3 问题 5） |
| 第 5e 层 · 共享资源同名近期去重（`COORDINATION.md:348-362`） | **无。** MAOS 的 artifact 按 `(task_id, version)` 天然隔离 | **MAOS 不需要** |
| 第 6 层 · 小脑闸：便宜模型判 actionable，信号取自 DB 事实（`triage-core.ts:176-191`） | `gate.py:235-283` 七道闸 —— **同一个生态位，但用确定性规则实现**；`gate.py:72-75` 明写「用产物类型判，不信 role 自述」= 同一条「取事实不取自述」 | **形似神不同**（选择相反且各自有理，见 §4） |
| 第 6 层 · AI 判断底下的确定性地板（`triage-core.ts:197-227`） | `gate.py:6-7` Gate 整体就是确定性层；模型侧语义审查在 `maos/agents/reviewer.py:43` 挂在闸**之后** | **形似神不同**（MAOS 是「确定性在前、模型在后」，cumora 是「模型在前、确定性地板在下」） |
| 第 7 层 · 五条形状级提示词 + 「按位次占坑不可表达」（`glance-protocol.ts:8-20`） | `gate.py:125-142` `GATEWAY_MESSAGES` 四条人话原样喂回返工提示词 | **形似神不同**（MAOS 的提示词是 finding 的**载体**，不是判据面） |

**缺的那几层缺得有没有道理 —— 一句话结论：有道理，但理由是暂时的。**
MAOS 现在对整个 cumora 问题类免疫，靠的是两条结构性质：`dispatch_ready` 要求
`depends_on` 全部 DONE 才派发（`control_plane.py:294`），并行的任务在结构上看不见彼此；
驱动循环是单线程 `for`（`flows/common.py:119-130`）。**而这条循环的注释白纸黑字写着
「换 RocketMQ 后这个循环消失（消费者常驻）」（`flows/common.py:112`。** 那一天，
第 2、3、3b、4 层会**同时**变成必需品 —— cumora 是逐层踩坑逐层加的（第 3a 层就是
「只加了大脑闸忘了小脑闸」的事故产物），MAOS 会一次性面对全部。这是本轨最该记住的
一条：**MAOS 的免疫不是设计出来的，是单线程送的。**

### 2.2 十四条反面教材逐条判定（问题 4）

| # | 反面教材（`COORDINATION.md` 行号） | MAOS 判定 | 理由 |
| :-- | :-- | :-- | :-- |
| 1 | Don't cap one layer without the other（`:489-499`） | **不会踩（当下）／ 会踩（多 worker 后）** | 现在一层并发闸都没有，谈不上「只加一层」。但种子形态已在：`maos/tools/port.py:31` 的 `rate_limit` 是**声明了却零处执行**的死旋钮，将来给模型调用加限流时，工具这一层会被当成「已经有了」 |
| 2 | Don't accrete scenario examples in the prompt（`:501-515`） | **不会踩** | MAOS 的判据面是代码不是提示词（`gate.py:6-7`）。`GATEWAY_MESSAGES` 四条虽是逐格手写，但四象限是**封闭集合**（`gate.py:127-142`），不存在「每发现一个 bug 加一条」的滑坡 |
| 3 | Don't dump AGENT_VOICE_RULES（`:517-526`） | **不会踩** | MAOS 的 Agent 没有人格/语气层 |
| 4 | Don't dump the CLI catalog（`:528-532`） | **不会踩** | 同上 |
| 5 | Don't write a "how to handle HELD" section（`:534-540`） | **不会踩，且方向本来就对** | cumora 的结论是「契约由该出现的时刻返回的文本本身传达」。MAOS 的 `GATEWAY_MESSAGES` 正是随 finding 一起、在返工那一刻返回的（`gate.py:125-126`），落在正确的一侧 |
| 6 | Don't pile loop-prevention mechanisms（`:542-547`） | 🔴 **正在踩** | MAOS 已有**四条**止损：`max_attempts`（`control_plane.py:452`）、`_max_replan`（`:577`）、第三出口 `_human_exit`（`:491`）、`_should_replan` 的「第 2 次 rework」（`:572-574`）。且顺序敏感 —— `:442-443` 的注释明说第三出口「必须排在 max_attempts 之前」。cumora 的判据：加第五条之前先查是哪一条没抓住 |
| 7 | Don't write to `conversation_reads.last_read_at` as a side effect（`:549-555`） | **已踩过并已修** | 同类事故记在 `control_plane.py:310-319`：幂等键被非法调用烧掉，任务永久停在 DISPATCHED。修法也同源 —— 把只读校验挪到消费闸之前 |
| 8 | Don't add fetch calls without a timeout（`:557-563`） | **不会踩** | 两条外呼路径都带超时：`maos/model/client.py:212`、`maos/tools/sandbox.py:511`；Nacos 拉取 `maos/config/nacos_source.py:192` 也带。（`maos/tools/gateway.py` grep 未命中 timeout，是否真发网络请求我没核，见 §5） |
| 9 | Don't add scenario-specific prompts to fix one incident（`:565-576`） | **不会踩** | 同第 2 条 |
| 10 | Don't ship an override flag without a cost — soft gates erode（`:578-594`） | 🔴 **正在踩** | `MAOS_FINANCE_THRESHOLD`（`gate.py:96`、`:169-188`）调大即可**静默停用**第六道闸：解析失败会告警并收严，但 `99999999` 是**合法值**，闸照常判 pass，`gate_results` 里看不出它被配置掉了。这正是「无成本的旁路」 |
| 11 | Don't fix infra issues with prompt changes（`:596-610`） | **不会踩** | MAOS 的 Gate 零模型调用，天然免疫。风险转移到 `maos/agents/reviewer.py:43`（STRONG tier）—— 它挂了之后闸后语义审查怎么退化，我没核（见 §5） |
| 12 | Don't burn tokens hammering a converged LLM judgment（`:612-623`） | **不会踩（半条）** | 硬上限齐备：`max_attempts` 默认 3、`_max_replan` 默认 2（`control_plane.py:577-578`）。缺的是另一半 —— cumora 的 decline cap **在新消息到达时重置**（`agenda.ts:176-183`），MAOS 的 REWORK 计数从 event_log 数起（`control_plane.py:569-571`），**永不重置** |
| 13 | Don't treat absent members as a failure mode to "fix"（`:625-644`） | **不会踩，且是 MAOS 的强项** | 铁律 8「MAOS 不持有权威事实」与之同源：`GW_QUERY_FIRST` 判 info 不挡闸（`gate.py:159-160`）就是「网关自己说不清，Gate 不替它下结论」。cumora 说的是「不要围着坏掉的部件设计」，MAOS 说的是「不要替外部系统下它没下的结论」—— 同一条哲学的两个面 |
| 14 | When something stops working, DIFF against the last good baseline（`:646-663`） | 🔴 **正在踩，且是本仓最现实的一条** | 本轮派单 §0.1 自己就是证据：主干工作区有约 76 个未提交改动，同一条测试命令在不同工作区跑出 1069 / 1114 / 1117 三个数。**没有钉死的「上一个已知良好基线」，回归发生时无从 diff** |

### 2.3 三个专项判断（问题 2 / 3 / 5）

**问题 2 —— 那条判据能不能直接抄进 MAOS 的规程？能，而且 MAOS 两个方向都基本站对了。**
「本该用代码机制却写成提示词」在 MAOS 几乎不存在：Gate 是刻意做成规则驱动的，
理由写在 `gate.py:6-7`「判定必须可复现、可解释、可审计」。反方向（「脑子在正确状态面前
做出明确决定时不该加代码机制」）也基本没犯 —— MAOS 的代码机制判的都是**结构**
（有没有 test_report、`files` 字段在不在、金额超没超阈值），不是意图。
唯一一处值得盯的是 `_gate_acceptance` 的非代码分支：它仍然拿 Agent 自述的 `self_check`
当验收依据（`gate.py:325-327`、`:316-323`）。代码类任务已经把这条换成了跑出来的
`test_report`、且**无降级**（`gate.py:333-335`），理由写得很硬：「回落等于把『Agent 自称
完成』重新放回验收依据里」。cumora 的同一条教训在 `triage-core.ts:378-382`
（信号必须取自 DB/Redis 事实，绝不从消息措辞推断）与 5d 的整段（`--send-anyway` 是
客户端意见，不算数）。**非代码类任务这条口子还开着，但它是有意识开的，不是疏漏。**

**问题 3 —— MAOS 的 Gate 是「拒绝 + 给新事实重判」还是只有「拒绝」？
答案：有回灌，但回灌的是「我对你的评价」，不是「世界变了」。** MAOS 确实把 findings
写回任务行、并随下一次 `TASK_ASSIGNMENT` 一起发出去（`control_plane.py:472-480` →
`:301` 的 `rework_findings=t["findings"]`），所以形式上是「拒绝 + 给理由重判」。
但两处形状差异是实质性的：

1. **回灌内容不同。** cumora 的 HELD 内联的是**这个 agent 从未被展示过的 peer 消息原文**
   （`cli.ts:1972-1974`）—— 是世界在它编写期间发生的变化。MAOS 的 rework 回灌的是
   Gate 对**它自己这一轮产出**的判定，关于**别的任务在它跑的时候产出了什么**，
   信息量为零。
2. **「展示即已读」这一半 MAOS 没有。** cumora 的 HELD 会把游标推过它展示的行
   （`cli.ts:1971`），所以重试**不会在同一批行上再挡一次**，平铺重发即可通过；
   这一条正是撤掉 compose-anchor 的直接理由（恒假警每次多烧 1–2 次大模型往返，
   `COORDINATION.md:234-240`）。MAOS 每一次 rework **必然烧掉一个 attempt**
   （`control_plane.py:296` 的 `attempt = t["attempt"] + 1`），而 `max_attempts` 默认只有 3。

**现在不出事是因为 DAG 串行（`control_plane.py:294` + `flows/common.py:119-130`），
任务之间没有「在你跑的时候世界变了」这件事。** 一旦并行，MAOS 需要的正是这个形状。
这是本轨最可能值钱的一条，判断见 §3 第 1 行。

**问题 5 —— MAOS 的审批放行有没有「承认而非跳过」的形状？没有，但当前失效形态是响亮的。**
`human_decision(task_id, approved, operator, note)`（`control_plane.py:702`）只认 `task_id`：
批准不携带**操作人当时被展示的是哪一版**（attempt / artifact version）。房间卡片确实把
`attempt` 渲染进了 Envelope（`hiclaw/room_demo.py:141`），但 `/approve <task_id>`
（`room_demo.py:145`）把这个信息丢掉了 —— 这在结构上就是 cumora 2026-07-08 修掉的
「陈旧令牌」形状：承认了 A 状态，用在了 B 状态上。

三点让当前风险可控，必须一并说清楚，否则就是夸大：
其一，`assert_transition`（`control_plane.py:726`）挡在最前面，任务若已不在 BLOCKED，
陈旧的批准**当场抛异常**而不是静默生效 —— 失效形态是响亮的；
其二，任务停在 BLOCKED 期间没有任何东西在跑，产物不会变；
其三，`human:<task_id>` 幂等键（`:728-731`）保证一个任务只被人工决策一次。
**所以这条在当前单线程运行时是安全的，它和第 2/3/4 层一样，是「多 worker 之后」的账。**

## 3. 可移植清单

| # | cumora 的做法 | 出处 `文件:行` | MAOS 现状 | 形态 | 落点 | 成本 | 判断 |
|---|---|---|---|---|---|---|---|
| 1 | **驳回时回灌「你从未被展示过的新事实」，并让脑子对着新状态重判**；且驳回信封自身推进游标（「展示即已读」），重试不会在同一批事实上再挡一次 | `server/src/agents/cli.ts:1948-1990`、`:1971` | rework 只回灌 Gate 对**本轮产出**的 findings（`control_plane.py:472-480` → `:301`），不含「别的任务在你跑的时候产出了什么」；且每次 rework 必烧一个 attempt（`control_plane.py:296`），`max_attempts` 默认 3 | 抄思想 | 动内核（`maos/core/control_plane.py`） | 3 人天 | **复赛后** —— DAG 串行（`control_plane.py:294`）下并行任务看不见彼此，现在做是空转；`flows/common.py:112` 写明「换 RocketMQ 后循环消失」，那一次并行化 PR 里必须带上这条 |
| 2 | **旁路一道闸必须留痕**：HELD 文本会明说「你的 `--send-anyway` 被忽略了，以及为什么」 | `cli.ts:1983-1985`；判据 `docs/COORDINATION.md:578-594` | `MAOS_FINANCE_THRESHOLD` 调大即**静默停用**第六道闸 —— 解析失败会告警收严（`gate.py:169-188`），但 `99999999` 是**合法值**，闸照判 pass，`gate_results` 里看不出它被配置掉了 | 抄思想 | 动内核（`maos/runtime/gate.py`：阈值非默认时补一条 `SEVERITY_INFO` finding） | 半天 | **赛前做** —— MAOS 已有三态 `pass/noted/fail`（`gate.py:271-272`），这条只是把现成机制用上；且「闸怎么证明它真的在判」是演示现场最可能被问的一句 |
| 3 | **放行标志必须绑定服务端展示过的那个状态**（令牌存 `seq:<n>`，消费时重查房间有没有往前走，走了就作废并重新 HELD） | `server/src/agents/seen-boundary.ts:222-261`、`cli.ts:1880-1926` | `human_decision` 只认 `task_id`（`control_plane.py:702`）；房间卡片渲染了 `attempt`（`hiclaw/room_demo.py:141`），但 `/approve <task_id>`（`room_demo.py:145`）把它丢掉了 | 抄接口 | 动内核（`control_plane.py` 加可选 `seen_attempt`；`hiclaw/room_demo.py` 指令带上 attempt） | 1.5 人天 | **复赛后** —— `assert_transition`（`control_plane.py:726`）已让陈旧批准**响亮失败**而非静默生效，且 BLOCKED 期间产物不会变，当前不是活 bug；异步审批 / 多 worker 后升级为必做 |
| 4 | **硬上限与「新事实重置预算」配成一对**：兜底唤醒 3 次不推进就停，会话里落任何新消息即重置计数 | `server/src/agents/agenda.ts:140-154`、`:176-183` | 只有上限那一半：`max_attempts`（`control_plane.py:452`）、`_max_replan` 默认 2（`:577-591`）；REWORK 次数从 event_log 数起（`:569-571`），**永不重置** | 抄思想 | 动内核（`maos/core/control_plane.py`） | 1 人天 | **复赛后** —— `max_attempts` 默认才 3，在这个量级上重置的收益很小；「先有上限、再谈重置」的顺序本身是对的 |
| 5 | **故障时不 fail-closed 成「没活干」，而是切出最窄的确定性用例**：分类器 503 时只保「恰好一处停滞 + 别人最后发言 + ≤30 分钟 + 无其它卡片」，其余仍 fail-closed | `agenda.ts:526-564` | 同构已有：`_finance_threshold` 读不出数回落**收严**并告警、不抛（`gate.py:169-188`）；`_over_finance_threshold` 解析不出即触发（`gate.py:191-208`）。缺的不是哲学，是**逐处显式写死方向并注明写反的后果**的规程 | 抄思想 | 新增插件（落在 `docs/` + `maos/tests/`，不碰内核） | 半天 | **赛前做** —— 基线 `926aa7b` 那次事故正是「白名单判据方向写反」；这条是把已有的正确做法固化成检查单，不改任何跑绿的代码 |
| 6 | **回归守卫注释**：`HARD_LOOP_CAP` 头上明写「这条兜底被以『AI 原生的优雅』为名删过两次，两次都回归了 —— 不要删」 | `server/src/agents/triage-core.ts:206-208` | MAOS 注释密度极高、且大量写了「为什么这么写」，但**没有「这条被删过 N 次 / 这条看起来多余但删了会怎样」这一形态** | 抄思想 | 新增插件（`docs/` 注释规程 + 若干处补注释，不碰逻辑） | 半天 | **赛前做** —— 零风险；`gate.py:146-158`（四象限 severity 曾分叉）、`control_plane.py:310-319`（幂等键顺序）这两处正是最该挂这种注释的地方 |
| 7 | **共享同一份外部配额的两类调用必须成对设闸**（大脑信号量 + 小脑信号量，都走同一个 spawn pacer） | `docs/COORDINATION.md:489-499`、`:100-118` | 一层并发闸都没有；`maos/tools/port.py:31` 的 `rate_limit` 是**声明了却全仓零处执行**的死旋钮（所有实例填 `""`） | 抄思想 | 动内核（`maos/runtime/` + `maos/model/`） | 2 人天 | **复赛后** —— 单线程下是空转。关键约束：**必须和并行化在同一次改动里做完**，不能分两次 —— 第 3a 层就是「只加了大脑闸忘了小脑闸」的事故产物 |
| 8 | **唤醒经济学当成一等指标**：一次 run 唤醒了 agent，10 分钟内它在触发会话里什么都没发 = **silent run**，按这个口径发布过 26.3% 的群聊静默率 | `server/src/__integration__/wake-economics.test.ts:1-13` | 成本侧已有：`maos/obs/trace.py:281-330` 按 trace_id 聚合 `model_usage`，且刻意区分「没调」与「调了没记」。**缺产出侧配对** —— 花了钱的这一轮到底产出了什么，没有指标 | 抄思想 | 新增插件（`maos/obs/**`，不碰内核） | 1 人天 | **复赛后** —— 成本归因刚在 T32 收口，再叠一层指标是手册范围外（铁律 4） |
| 9 | **AdaptivePacer**：任一次调用限流就把全局间隔翻倍（上限 8s），连续 5 轮干净后减半回落 | `docs/COORDINATION.md:120-134` | 无退避；`maos/model/client.py:212-223` 只识别超时与 URLError，不识别限流 | 抄代码 | 动内核（`maos/model/client.py`） | 1.5 人天 | **不做** —— MAOS 一个 plan 的模型调用是几十次量级，够不上供应商突发窗口。先做第 7 条的固定并发闸就够；**在没有症状的地方加自适应机制**，正是反面教材第 6 条（「加第五条之前先查哪一条没抓住」）的另一种版本 |

**分布**：赛前做 3（#2、#5、#6）／复赛后 5（#1、#3、#4、#7、#8）／不做 1（#9）。
落点：新增插件 3（#5、#6、#8）／动内核 6（#1、#2、#3、#4、#7、#9）／**动冻结契约 0** ——
本清单没有一条需要碰 `maos/contracts/**` 或 `maos/core/store.py` 的表结构。
第 1、3 两条都只加**可选参数**与**事件 payload 里已有的字段**（`attempt` 已在
`E.task_assignment` 里），不新增状态、不新增迁移（铁律 1 / 铁律 9）。

## 4. 反向清单 —— 它做了但 MAOS 不该抄

1. **不要把 Gate 换成模型判。** cumora 第 6 层的整个立场是「每个决策都由小模型做，
   **没有任何 regex 分类消息内容**，留下的 regex 只解析模型自己吐的 JSON」
   （`triage-core.ts:10-16`）；MAOS 在同一个生态位选了确定性规则，理由是「判定必须
   可复现、可解释、可审计」（`gate.py:6-7`）。**两边在同一个位置做了相反的选择，
   而各自的理由都成立** —— 因为判据对象不同：cumora 判的是自然语言意图（不可枚举），
   MAOS 判的是 artifact 结构（可枚举：有没有 test_report、`files` 字段在不在、
   金额超没超阈值）。把 MAOS 的 Gate 模型化，是用可复现性去换一个它不需要的能力。
   MAOS 已经把模型侧语义审查放在闸**之后**（`maos/agents/reviewer.py:43`），
   分层是对的，不要合并。

2. **不要抄 hold token 的 TTL 语义去改 MAOS 的幂等键。** cumora 的令牌语义是
   「承认**一个瞬间**」，所以 2 分钟 TTL 是对的，长 TTL 会把让路时存下的令牌变成
   将来的旁路弹药（`seen-boundary.ts:184-186`、`docs/COORDINATION.md:321-342`）。
   MAOS 的 `human:<task_id>`（`control_plane.py:728`）语义是「一个任务只被人工决策
   一次」，是**永久**的；给它加 TTL 会直接引入重复决策，而驳回路径带着**真实外部
   副作用**（反向打补丁，`control_plane.py:736`）。形状像，语义相反 —— 这是本轮
   最容易抄错的一条。

3. **不要抄多租户与规模包袱。** 两档 nudge 冷却（`agenda.ts:115-124`）、
   global/project 两层记忆隔离（`docs/COORDINATION.md:725-740`，见 `memory-scope.ts`）、
   `convene.ts:41-52` 的 `company_id` + `FOR SHARE` 参与者校验、
   每引擎一个 `CUMORA_DEFAULT_*_MODEL` 回落（`docs/COORDINATION.md:667-681`）——
   这些解决的是「N 个租户 × M 个用户 × 向后兼容」。判据照 §6 那句：
   *这个设计在解决我也有的问题，还是在解决它的用户量才有的问题？*
   一人公司抄进来是纯负债。

4. **不要抄唤醒防抖 / 合并 / 同回合插话那一整套。** `docs/COORDINATION.md:143-162`
   的四个旋钮（2500ms 防抖、运行中唤醒合并成单次重跑、DM 插进活会话、群消息内容无关
   nudge）是**消息驱动**架构的特产：同一个 agent 会被同一件事叫醒 N 次。MAOS 是
   派单驱动，一次 `dispatch_ready` 只发一个 `(task_id, attempt)`，重复投递由
   `claim:<task_id>:<attempt>` 幂等键挡（`control_plane.py:325-328`）。
   抄过来是给一个不存在的问题加三个旋钮 —— 反面教材第 6 条的教科书形态。

5. **不要抄 5e「共享资源同名近期去重」。** `docs/COORDINATION.md:348-362` 解决的是
   「两个 agent 各建一份《第七天的猫》」，前提是**共享命名空间里的资源可以被任意
   agent 创建**。MAOS 的 artifact 按 `(task_id, version)` 隔离，两个任务在结构上
   造不出同一个对象。

6. **不要抄那五条提示词规则的内容，只抄它们的元规则。** `glance-protocol.ts:20`
   的五条是为「N 个 agent 抢同一个发言位」这个具体形状调出来的（不许按位次占坑、
   乐观发送靠服务端兜底、缺人时谁在谁补位）。MAOS 的 Agent 不抢发言位，这五条一条
   都不适用。值得抄的是元规则：**「保持五条、只讲形状、编辑时改 const 不加条」**
   （`docs/COORDINATION.md:483`）与「整份系统提示约 5KB」的字节预算
   （`docs/COORDINATION.md:405-409`）。

## 5. 我没看懂 / 没时间看的

1. 🔴 **第 1–4 层我读的是文档，没读实现。** `BigBrainSemaphore`、`AdaptivePacer`、
   `standingPrompt()`、`runTurn()` 全在 `server/src/agents/computer/daemon.ts`
   里（`docs/COORDINATION.md:783` 的文件表点名了它），我一行没读。所以 §1 第二、
   三段与 §2.1 前四行的所有引用都指向 `docs/COORDINATION.md` 而不是实现 ——
   **这是有意的（宁可粗定位也不编行号），但那四层结论的可靠性因此低一档**：
   我只能证明作者是这么写文档的，不能证明代码就是这么写的。
2. `server/src/agents/steer.ts`（454 行）只 grep 了关键词。我知道有
   `STEER_INTERRUPT_AFTER_MS`（`steer.ts:139`）和 `STEER_ENABLED` 紧急开关
   （`steer.ts:262-267`）两个旋钮，但**「流的下一个安全边界」到底怎么判**没看懂 ——
   而这正是「同回合插话不会把正在跑的推理撕坏」的关键。
3. 派单点名的 `convene.ts`（431）／`membership.ts`（303）／`inbox-triage.ts`（188）／
   `idle.ts`（217）只读了头部与关键片段；`agenda.ts`（624）只读了 `claimStallNudge`
   与确定性兜底两段。
4. 🔴 **四个集成测试只读了抬头。** `agent-anti-duplicate.test.ts:1-14` 和
   `wake-economics.test.ts:1-13` 读了文件头注释，`convene-concurrency.test.ts`（298 行）
   与 `agent-steer.test.ts`（1021 行）**一行没读**。派单 §5.1 说「测试直接告诉你
   作者认为什么是主流程」，这一步是我做得最不够的一处。
5. MAOS 侧两处没核，各自影响一条判定：
   - `maos/tools/gateway.py` 到底发不发真实网络请求、有没有超时（grep 未命中
     `timeout`）—— 影响 §2.2 第 8 条「不会踩」的完整性。
   - `maos/agents/reviewer.py`（`:43` 走 STRONG tier）在模型不可用时怎么退化 ——
     影响 §2.2 第 11 条。cumora 在这里踩过最贵的一次坑（分类器 100% 503 而整张
     安全网静默死掉，`docs/COORDINATION.md:596-610`）。
6. cumora 的 `observability.ts`（809 行）与 `llm-ledger.ts`（956 行）没碰 —— 成本账本
   那一面是别的轨的题目。

## 附录 A · 顺手发现的 MAOS 问题

（本轮不改 MAOS、不追加 `docs/BACKLOG.md` / `docs/DECISIONS.md`，以下留给整合轮。）

1. **`ToolPort.rate_limit` 是声明了却全仓零处执行的死字段。**
   `maos/tools/port.py:31` 定义了它，四个实例全填 `""`（`maos/tools/gateway.py:391`、
   `:412`、`maos/tools/sandbox.py:654`、`:682`），**没有任何读取方**。
   风险不是「现在限流不生效」（现在也不需要），是**将来有人给模型调用加限流时，
   会以为工具这一层已经有了** —— 正是反面教材第 1 条（只设一层闸）的种子形态。
   处置二选一：删掉字段，或在 ToolPort 注册时对非空值抛 `NotImplementedError`。

2. **第六道闸可被一个合法配置值静默停用。** `MAOS_FINANCE_THRESHOLD`
   （`maos/runtime/gate.py:96`、`:169-188`）填 `99999999` 解析得通、不告警、闸判 pass，
   而 `gate_results` 显示的是 `pass` 不是 `noted` —— 读结果的人分不出「这道闸没话说」
   和「这道闸被配置掉了」。MAOS 自己已经发明了三态（`gate.py:271-272`），这里没用上。
   见可移植清单 #2（赛前做，半天）。

3. **人工放行不绑定操作人被展示的那一版。** `human_decision` 只认 `task_id`
   （`maos/core/control_plane.py:702`）；房间卡片把 `attempt` 渲染进了 Envelope
   （`hiclaw/room_demo.py:141`），但 `/approve <task_id>`（`room_demo.py:145`）丢掉了它。
   **当前不是活 bug**（`assert_transition` 让陈旧批准响亮失败，`control_plane.py:726`；
   且 BLOCKED 期间产物不会变），异步审批 / 多 worker 之后变成活 bug。见可移植清单 #3。

4. **四条止损机制并存，且相对顺序本身是判定的一部分。**
   `max_attempts`（`control_plane.py:452`）、`_max_replan`（`:577`）、
   第三出口 `_human_exit`（`:491`）、`_should_replan` 的「第 2 次 rework」（`:572-574`），
   而 `:442-443` 的注释自己承认第三出口「必须排在 max_attempts 之前」。
   这不是现在的缺陷，是**再加第五条时会出事的形状**。建议照可移植清单 #6 的形态，
   在 `on_review_verdict` 头上挂一条回归守卫注释：*四条止损的相对顺序是判定的一部分；
   加第五条之前，先证明现有四条里是哪一条没抓住。*

5. **没有钉死的「上一个已知良好基线」，所以反面教材第 14 条在本仓无法执行。**
   同一条 `python3 -m pytest maos/tests -q` 在本 worktree 是 1069 passed / 39 skipped，
   在主干工作区是 1114 或 1117（派单 §0.1）。cumora 把基线钉到了
   **时间戳 + commit sha + 一句「当时是什么状态」**（`docs/COORDINATION.md:12-25`），
   回归时第一步就是对着那个 sha 做 `git log --since` 逐个 commit 读
   （`:653-663`）。这是流程问题不是代码问题，且 T33 轨看名字正在做同一件事，
   本条只记不重复派活。

## 附录 B · 对人类工作流的启发

1. 🔴 **「派单放久了会过期」就是 seen-cursor 问题的人类版，而现有解法只做了一半。**
   全局 CLAUDE.md 已经要求「粘之前 grep 一遍旧 sha / 旧条数」，派单第 1 步的开场自检
   也已经是「对不上就停手」的形状 —— 这等于 cumora 的**拒绝**。缺的是另一半：
   **把新事实一起给出来让子会话重判**（§2.3 问题 3）。改法极小：开场自检发现 sha
   不符时，顺手打印 `git log --oneline <派单里写的 sha>..HEAD`。子会话立刻从
   「不对，停手」变成「不对，而且我知道差了这 7 个 commit」，很多情况下它自己就能
   判断这次漂移要不要紧。cumora 撤掉 compose-anchor 的理由（`COORDINATION.md:234-240`）
   正是「恒假警的闸，成本是真的」—— 一个只会说「对不上」的自检，会训练你直接忽略它。

2. 🔴 **「先点名指出、等我确认再动手」是软机制守卫软机制，会静默失效。**
   `COORDINATION.md:578-594` 那条反面教材的原话是：修法**不是**「提示 agent 负责任地
   使用这个标志」，因为那是拿软机制去守软机制。全局 CLAUDE.md 自己也预见到了
   （「这是软约定，只保证『基本每次』；哪天发现漏报，让 Claude 把它升级成
   UserPromptSubmit hook」）。cumora 的结论比这个更硬两点：**其一，软闸不是慢慢
   失效，是一旦「绕过去更省事」被发现就当场失效**（agent 学会抢先带 `--send-anyway`
   纯粹是为了省一次往返，行为完全符合「要高效」的指令）；**其二，漏报是静默的 ——
   你不会「哪天发现」它。** 这条和本仓 CLAUDE.md 里「hook 执行失败会被当作非阻塞
   错误放行，守卫因此静默失效且不报警」是同一句话的两次独立发现。

3. **decline cap 有直接的派单版本。** `agenda.ts:140-154`：同一件事三个不同的大脑
   都说了「不」，再问不会改变结论，钱是真的。对应到多轨派单 —— 同一个问题被第三轨
   也判成「做不了 / 得问人」时，**不该再派第四轨去试，该改的是派单本身**。
   配套的另一半同样重要且容易漏：**计数在新事实到达时重置**（`agenda.ts:176-183`）。
   所以「问过人、拿到新信息之后重新派」是合理的，「没有任何新信息再派一轨」不是。

4. **cumora 那份文档本身就是值得抄的形态，MAOS 缺其中一件。** 它的密度来自三件事
   同时具备：**每层明写抓得住什么、抓不住什么**（`COORDINATION.md:213-220` 直接有
   一节叫 "What this does NOT catch"）；**撤掉的方案连同 commit sha 一起留着**
   （5a 整节，`:222-244`）；**「试过、别重蹈」独立成章**（`:487-663`）。
   MAOS 的 `docs/DECISIONS.md` 是第一件的雏形，`docs/BACKLOG.md` 是第三件的雏形，
   **第二件没有对应物** —— 而这恰恰是多会话并行时最容易重复踩的：另一轨不知道你
   三小时前试过同一个方案并撤了。

5. **「观察窗不是判决」。** `COORDINATION.md:715-719`：7 分钟的观察器在 T1 判了 6/8，
   +9 分钟再实时查已经是 8/8 —— 异步系统（冷却、心跳）经常在任意观察窗之后才恢复。
   对应到并行派单：一轨回执里写的「另一轨尚未合并」「主干还是那个 sha」，是它开场
   自检那一刻的快照，不是你读到回执时的事实。**整合轮别拿回执当现状，重查一次。**

6. **「不要围着坏掉的部件设计」（`COORDINATION.md:625-644`）——本轮六轨的形状是对的。**
   cumora 的用户原话是「团队里有人请假，你不会说这活干不成了」，修法落在**原则层**
   （缺人时谁在谁补位）而不是运维层（把 olivia 修好）。对派单的具体含义：
   每轨产出必须**独立可合并**，不能有「等 T4x 那轨先落地我才写得完」的依赖。
   本轮「各写各的 `docs/refs/cumora-*.md`、整合轮统一处置」正是这个形状 ——
   任何一轨挂掉，其余五轨的产出照样成立。

