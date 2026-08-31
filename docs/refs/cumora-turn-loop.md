# cumora 解析 · Agent turn 循环与上下文管理 （T41 · 基线 cumora@1e883f6 / MAOS@926aa7b）

解析面：多跳工具调用 / 上下文压缩（compaction）/ skill 装载 / 人格注入 / turn 落库幂等。
主战场 `server/src/agents/turn.ts`（3547 行），配套 `turn-compaction.ts`、`turn-stream.ts`、
`tools.ts`、`tools-shared.ts`、`skills.ts`、`personas.ts`、`turn-wake.ts`。
MAOS 对标物 `maos/runtime/worker.py` + `maos/agents/base.py` + `maos/skills/`。

## 1. 它是怎么做的

**一次 turn 是一个近 2000 行的单函数。** `runAgentTurn(agentId, options)` 从
`turn.ts:1571` 一直写到文件末尾，中间没有再分层：唤醒分类 → 指纹去重 → 小模型 triage →
物化 FS 命名空间 → 组装 wake prompt → 多跳循环 → 收尾 relay → `finally` 提交工作区。
它不是「Agent 类的一个方法」，而是**运行时对一个 agent 的一次调度**，persona 只是被
`loadPersona` 读进来的数据（`turn.ts:1572`）。这个形状本身就是一条信息：cumora 把
「一次轮次」当作**运行时的一等实体**（有 `runId`、有 `run` 行、有 stage 机器），
而不是当作 agent 对象的一次方法调用。

**多跳循环的终止条件是三层，不是一层。** 外层 `for (let hop = 0; hop < MAX_HOPS; hop++)`
（`turn.ts:2427`），`MAX_HOPS = 200`（`turn.ts:847`）。但 200 不是真正的终止条件 ——
它上面那段注释把演进史写死在代码里：4 → 8 → 100 → 200，每次都是被生产事故推着涨的
（`turn.ts:841-846` 明写「Iris 的视频下载在 4 跳被砍断」「多步研究任务撞穿 100 跳」）。
作者的结论是：跳数上限不该用来控成本，控成本的是 compaction。所以 200 被当成
「实质无上限」，`loopExitReason` 的四个取值里 `'max_hops'` 是**失败态**而非正常出口
（`turn.ts:1668` 起的注释、`turn.ts:3170` 的收尾把它记成 `finalStatus = 'failed'` 并发
`turn.cap_reached` 事件）。真正的正常出口是第二层：模型显式调 `set_turn_status`。

**turn status 是一份协议，而不是从沉默里推断出来的状态。** 取值域冻结成五个：
`done / continue / needs_clarification / blocked / waiting`（`tools-shared.ts:47`），
`done` 与 `waiting` 是终态（`turn.ts:1118`）。`set_turn_status` 是整个工具集里**唯一的
协议工具**（`tools-shared.ts:315`），`reason` 字段是必填的，缺了直接判 invalid 并把
理由写回给模型：「reason is required so the runtime can audit why the turn stopped」。
系统提示词里同一条规则被复述成人话：「BEFORE YOU INTENTIONALLY STOP, call
`set_turn_status`… Do not rely on silence to mean done.」（`personas.ts:105` 的
`GLOBAL_RULES` 首段）。当模型确实沉默着结束了一跳（既没工具调用也没声明状态），
运行时走的是一套**三级降级**而不是直接收手（`turn.ts:2788` 起）：先注入一条
`Internal protocol check:` 的用户消息把它推回循环，最多推两次；两次之后如果这一轮
已经产生过用户可见的回复，就**推断**成 `done` 并发 `turn.status_inferred`
（`turn.ts:2822`）；如果是 idle / background 这类合成唤醒且完全没有副作用，记
`turn.synthetic_noop` 跳过（`turn.ts:2850`）；都不是，才判
`loopExitReason = 'protocol_violation'` + `finalStatus = 'failed'`（`turn.ts:2868`）。

**终态声明还要过一道语义验证器 —— 由第二个模型判。** 模型说 `done` 不等于运行时接受
`done`。`shouldVerifyTerminalCompletion`（`turn.ts:1128`）在三个条件同时成立时触发复核：
inbox 非空、状态是终态、这一轮**没有**发出过真正的回复但**有**其他副作用。
这个条件的注释说得很直白：贴个 👀 表情也是副作用，但它可能只是「我看到了」而不是
「我干完了」，所以让一个小模型读上下文 + 读结构化副作用列表来判
（`turn.ts:1181` 的 `verifyTerminalCompletion`，用 `OPENAI_COMPACTION_MODEL` 这条小模型
通道、`reasoning: low`、10 秒超时）。判 `complete: false` 时，运行时把
`declaredTurnStatus` 清空、把驳回理由和 `next_step` 作为用户消息压回 history、
状态回退到 `thinking`、`continue` 回循环（`turn.ts:3093` 起到 `3150`）。
一句原文值得抄进 MAOS：「A reaction or short acknowledgement is not the deliverable.」

**上下文压缩是双阈值 + 双阶段 + 三条不变量。** 阈值按**模型的上下文窗口**动态算，不写死：
`contextWindowFor`（`turn.ts:915`）按模型名匹配窗口，软阈值 75%（`turn.ts:930`）、
硬顶 95%（`turn.ts:939`）。注释解释了为什么不写死：「a hardcoded 150K threshold on a
128K model would NEVER trigger and the turn would just hit the hard ceiling and die」。
触发点在**每跳开头、发请求之前**，判据是**上一跳返回的真实 usage**而非本地估算
（`turn.ts:2444`）。压缩本身两阶段（`turn-compaction.ts:144` 的 `compactHistoryWithSummary`）：
阶段一把超大的 `function_call_output` 就地截断到 600 字节（`turn-compaction.ts:94`），
这一步同步、免费、幂等；阶段一不够才进阶段二 —— 把最老的一批工具调用对**送给一个
小模型摘要**，用摘要文本替换掉被丢弃的条目（`turn-compaction.ts:333` 的
`summarizeAndSplice`），而不是塞一个「[earlier work dropped]」占位符。三条不变量写在
`turn-compaction.ts` 的文件头注释里：① `function_call` ↔ `function_call_output` ↔
`item_reference` 共享 `call_id` 的必须**整组**丢弃，否则上游 400；② 打头的非工具条目
（原始 inbox 消息）**永不丢**，丢了就等于抹掉了 agent 要回答的那个问题；
③ 最近 `KEEP_RECENT_PAIRS = 2` 组永不丢（`turn-compaction.ts:99`）。压完还超硬顶，
才 `loopExitReason = 'budget'` 退出（`turn.ts:2508-2513`），注释里叫它
「the ultimate compaction」—— 下一次唤醒重新开始。还有一个细节值得单独记：token 估算器
是 **CJK 感知**的（`turn-compaction.ts:67`，ASCII 按 3.5 字符/token、非 ASCII 按 1 字符/token），
注释说旧的 `length / 3` 把中文低估 3 倍，导致中文场景**从来没触发过 compaction**，
直接撞硬顶而死。MAOS 是中文系统，这条踩坑记录是直接命中的。

**工具集的形状是「一个万能出口 + 一个协议工具」。** `TOOL_DEFS_RESPONSES`
（`tools-shared.ts:77`）里，世界侧只暴露 `bash`，所有能力都是 `cumora` CLI 的子命令
（`cumora reply` / `react` / `dm` / `pull-group` / `kanban` / `doc` / `memory` /
`calendar` / `skills` …），tool schema 的 `description` 字段本身就是一本 CLI 手册。
真正的 tool 只有 `bash` 和 `set_turn_status` 两个（外加一组 `NATIVE_TOOL_DEFS` 文件工具）。
云端与 BYOA（自带 API key、本地跑）两条路径共用 `tools-shared.ts` 这一份定义与
`tBash` 实现（`tools-shared.ts:390`），`tools.ts` 只补云端独有的几个原生工具
（`tools.ts:44` 的 `executeTool` 分发，`tDmWith` / `tPullGroup` / `tReact` / `tPalette`）。
副作用不是靠解析模型输出拿到的，而是 CLI **结构化回吐**的：`bashOutputSideEffects`
（`tools-shared.ts:589`）从 bash 输出里提取 `CliSideEffect[]`，运行时据此知道
「这一轮到底改了世界的什么」，语义验证器和 auto-relay 抑制都吃这份数据。

**skill 是运行时按需装载的，进提示词的只有名字和描述。** `loadSkillsIndex`
（`skills.ts:250`）从 `agent_workspace` 表里捞 `skills/%/SKILL.md`，只解析 YAML
frontmatter 的 `name` + `description` 两个字段，其余一律不读。这份索引在唤醒时和
memory / climate / context 并行加载（`turn.ts:1973-1978`），然后以每行一条
「- 名字 — 描述」的形状进 wake prompt（`turn.ts:2167-2170`），提示词里明写
「these are NAMES + DESCRIPTIONS ONLY. When a task matches one, run
`cumora skills read <name>` to pull the full instructions into your next turn」。
也就是说：**装载决定权在模型手里，代价是一次工具调用**。skill 本身是可安装的 bundle
（`installSkillFromManifest`，`skills.ts:177`），有路径穿越校验、单 skill ≤100 文件、
单文件 ≤256KB 的硬约束，且**拒绝覆盖已存在的同名 skill**——要重装得 agent 自己先删。
skill 存在 agent 私有工作区里，不是全局注册表：agent 之间要共享 skill 得互相发消息，
对方自己拉一份（`skills.ts:17-19` 的模块注释）。

**人格是三段拼接，运行时组装，其中两段由 agent 自己可写。** `buildSystemPrompt`
（`personas.ts:311`）拼的是：① agent 自己工作区里的 `IDENTITY.md` + `SOUL.md`
（`readWorkspaceFile`，`personas.ts:343`）；② `participants.system_prompt` 列里的 style
一行；③ 全局 `GLOBAL_RULES`（`personas.ts:105`，是一个几百行的模板字符串，含 CLI 手册、
反独白规则、引用回复礼仪）；④ 实时拉的团队花名册（`rosterSection`，`personas.ts:273`）。
花名册每次都重拉，注释说是为了「加/减队友立刻生效」。分层的关键在于**可写性梯度**：
`GLOBAL_RULES` 是代码里的常量（人类才能改），style 是 DB 列（管理员改），
`IDENTITY.md` / `SOUL.md` 是 agent 自己 `edit_file` 就能改的工作区文件 ——
提示词里明写「edit it via `edit_file` to evolve」。人格是可自演进的，规则不是。

**幂等靠三件东西，没有一件是数据库唯一约束。** 第一，进程内的
`lastCompletedInbox: Map<agentId, fingerprint>`（`turn.ts:838`）：fingerprint 对普通唤醒
是 inbox 消息 id 拼串，对 idle / 定时 / 投票这类合成唤醒则拼上当前 ISO 时间戳
（`turn.ts:1584`），也就是**合成唤醒故意不去重**。命中就 `turn.skipped` 直接返回，
不调模型。第二，只有**成功完成**的 turn 才写回 fingerprint（`turn.ts:3294`），
失败的 turn 保持可重试 —— 注释写得很明确：「Failed turns do not update the fingerprint,
so they remain retryable instead of disappearing into a silent skip.」第三，
工作区的落库在 `finally` 里，且是**第一件事**：`commitFs` 先跑，其他清理都是 best-effort，
注释说「losing a workspace diff would be data loss」。至于「同一个 turn 被重放两次会
怎样」——答案是：进程重启后那个 Map 是空的，会**真的重放一次**；护栏是 CLI 侧的
反独白闸（「you already posted in <convo> Ns ago and nobody has replied yet」，
`personas.ts:260`）和 `postSystemNotice` 的 `dedupeKey`，而不是 turn 层的幂等键。

## 2. MAOS 的对应物

先说最要紧的一条结论，它决定了后面所有对照怎么读：
**MAOS 的 agent 不跑 LLM 多跳循环。** `BaseAgent.ask()`（`maos/agents/base.py:152`）
调的是 `ModelClient.complete(system, user, tier)`（`maos/model/client.py:51`）——
签名里没有 `messages`、没有 `tools`、没有 history，一次问答返回一段文本。
`CodingAgent.run()`（`maos/agents/coding.py:40`）是**线性两步**：先 `kb.retrieve`
再 `code.repo-patch`（`coding.py:58` / `coding.py:63`），没有循环。
`WorkerRuntime.on_assignment`（`maos/runtime/worker.py:38`）收一条 `TaskAssignment`、
调一次 `agent.run(ctx)`、发一条 `TaskResult`，全文 79 行。
所以下表里凡是标「MAOS 没有」的，多半**不是缺陷，是另一种架构选择的必然结果** ——
MAOS 把「多步」放在 Plan 的任务图上（多个 task，各自一跳），cumora 放在一个 turn 里
（一个 task，多跳）。判断可移植性时必须带着这个前提读。

| cumora 的机制 | MAOS 的对应物（含文件路径） | 判定 |
| :-- | :-- | :-- |
| 「一次轮次」是运行时一等实体：`runId` + run 行 + stage 机器（`turn.ts:1571`） | 一次派发是一等实体：task 行 + `TaskState` 状态机 + event_log（`maos/runtime/worker.py:38`、`maos/core/control_plane.py:334`） | 同构 |
| 多跳工具循环 `for hop < MAX_HOPS`（`turn.ts:2427`，上限 200） | 无。一次 `agent.run(ctx)` 到底（`maos/runtime/worker.py:62`），"多步" 由 Plan 的多个 task 承担 | MAOS 没有 |
| 模型显式声明轮次状态：`set_turn_status` 五值协议（`tools-shared.ts:47`） | `AgentOutput.status` 三值 `ok / failed / blocked`（`maos/agents/base.py:92`），由 **Python 代码**赋值，不是模型声明的 | 形似神不同 |
| 沉默 ≠ done：两次 nudge → 推断 → protocol_violation 三级降级（`turn.ts:2788`） | 无对应。MAOS 的 agent 不会"沉默"，因为它是代码返回值不是模型自述 | MAOS 没有（也不需要） |
| 终态语义验证器：小模型复核 `done` 是否真的交付了（`turn.ts:1181`） | `ReviewerGate`（`maos/runtime/gate.py:235`）+ `AWAITING_REVIEW` 状态。判据更硬（结构化闸门），位置更晚（task 边界而非 turn 内） | 形似神不同 |
| 上下文压缩：双阈值 75%/95%（`turn.ts:930`/`939`）+ 两阶段 + LLM 摘要（`turn-compaction.ts:144`） | 无任何等价物。全仓 grep 不到 compact / truncate / token 预算 | MAOS 没有 |
| CJK 感知的 token 估算（`turn-compaction.ts:67`） | 无。MAOS 只在 `record_model_usage`（`maos/core/store.py:508`）记网关回吐的真实用量，不做本地估算 | MAOS 没有 |
| 每跳工具输出硬限长 8KB（`turn.ts:853`） | 无。`SkillResult.output`（`maos/skills/contract.py:38`）原样进 artifact，不截断 | MAOS 没有 |
| 工具集 = `bash`（万能出口，CLI 手册即 schema）+ `set_turn_status`（`tools-shared.ts:77`） | `SkillContract` 九要素（`maos/skills/contract.py:20`，12 字段合成 9 项）+ `identity.allowed_tools` 白名单 | 形似神不同 |
| 云端 / BYOA 两条路径共用一份工具定义（`tools-shared.ts` 的存在本身） | Scripted / 真模型双模式共用一份 `ModelClient` 抽象（`maos/model/client.py:49`），`select_model_client` 选路（`client.py:272`） | 同构 |
| 工具权限：无。`bash` 全放，靠提示词和 CLI 侧闸门约束 | `check_tool` / `check_risk` / `check_write` 三查 + `PermissionDenied`（`maos/agents/base.py:133-150`），`SkillInvoker.invoke` 白名单再查一次（`maos/skills/invoker.py:55`） | MAOS 更强 |
| 副作用结构化回吐：`CliSideEffect[]`（`tools-shared.ts:589`），运行时据此知道世界被改了什么 | `AgentOutput.artifacts` + `SkillInvoked` event_log 行（`maos/skills/invoker.py:121`，带 `input_digest` / `output_hash` / `invocation_id`） | 同构（MAOS 的溯源链更严） |
| skill 装载：运行时按需，只把 name+description 进提示词（`skills.ts:250`、`turn.ts:2167`） | `@register_skill` import 即注册（`maos/skills/registry.py:33`），`builtin` 包动态发现（`registry.py:43`）；**全量注册，无提示词裁剪**——因为提示词里根本不列 skill | 形似神不同 |
| skill 多版本：拒绝覆盖同名，重装要 agent 自己先删（`skills.ts:221`） | `name → {version → 类}` 双层注册表，按名取最高版本、按名+版本取历史版本（`maos/skills/registry.py:16`/`:69`） | MAOS 更强 |
| skill 归属：agent 私有工作区，agent 之间靠互相发消息共享 | skill 全局注册，归属靠 `contract.owner_roles` + `identity.allowed_skills` 交叉白名单 | 形似神不同 |
| 人格三段拼接，运行时组装（`personas.ts:311`），`IDENTITY.md` / `SOUL.md` **agent 自己可写** | `AgentIdentity` 冻结 dataclass（`maos/agents/base.py:59`），`docs/agent-identity.md` 由 `scripts/gen_docs.py` 从代码生成。agent **不可自改** | 形似神不同 |
| 团队花名册每次唤醒重拉进提示词（`personas.ts:273`） | 无。MAOS 的 agent 不知道有哪些同事，协作全由 Manager 的 Plan 编排 | MAOS 没有（架构差异） |
| 每跳成本记账，按 `purpose` 分通道（`compaction` / `completion-verify` / `steer-summary`） | `record_model_usage`（`maos/core/store.py:508`）+ `_ATTRIBUTION` ContextVar 挂 trace_id（`maos/agents/base.py:39`），按 `call_site` / `agent_role` / `tier` 归因 | 同构 |
| 幂等：进程内 `Map<agentId, fingerprint>`（`turn.ts:838`），进程重启即失效 | `claim_idempotency` 落库幂等键（`maos/core/store.py:80`），`claim`（`control_plane.py:309`）与 `on_task_result`（`control_plane.py:339`）各一道 | MAOS 更强 |
| 失败不写回 fingerprint → 保持可重试（`turn.ts:3294`） | `attempt >= max_attempts` 才 FAILED，否则回 PENDING 重派（`maos/core/control_plane.py:365-372`） | 同构 |
| 中途插话（steer）：跑到一半把新消息 splice 进 history（`turn.ts:2353` 的 `tryDrainSteer`） | 无。MAOS 的 task 一旦 DISPATCHED 就不接受新输入 | MAOS 没有 |
| 全流程观测事件：`recordEvent({kind, level, stage})`，一个 turn 几十条 | `append_event_log`（`maos/core/store.py:350`）+ `TaskState` 迁移记录 | 同构（cumora 粒度细得多） |
</content>
</invoke>

## 3. 可移植清单

| # | cumora 的做法 | 出处 `文件:行` | MAOS 现状 | 形态 | 落点 | 成本 | 判断 |
|---|---|---|---|---|---|---|---|
| 1 | 工具输出超限时**自描述截断**：返回 `{truncated, originalBytes, head, note}`，note 里明写「想要被省掉的尾部就换个更窄的查询」。注释原话：keep the output self-describing instead of silently slicing JSON | `turn.ts:853` 常量 + `turn.ts:867` `modelToolOutputPayload` | `maos/agents/reviewer.py:83` 是 `json.dumps(artifacts, ...)[:8000]` —— 从 JSON 中间裸切一刀，模型收到语法破损的 JSON **且不知道自己被截了** | 抄代码 | 新增插件（`maos/agents/reviewer.py` + 一个共享 helper） | 0.5 人天 | **赛前做** —— 现状是静默的审查盲区：artifact 一多就切在半截，Reviewer 基于残片出意见。改动约 10 行，不碰任何契约 |
| 2 | `reason` 是协议**必填**：缺了直接判 invalid 并把「reason is required so the runtime can audit why the turn stopped」写回给模型 | `tools-shared.ts:315` `tSetTurnStatus` | `AgentOutput.error` / `open_questions` 都允许为空（`maos/agents/base.py:94-95`），「失败但没说为什么」在类型上是合法的 | 抄思想 | 新增插件（`maos/runtime/worker.py` 的 `_reply` 前加一道断言） | 0.5 人天 | **赛前做** —— 一个 if 把它从可能变成不可能。`ReviewerAgent._needs_human`（`maos/agents/reviewer.py:71`）已经是这条思路的局部实现，推广到全体即可 |
| 3 | 沉默 ≠ 完成：模型不声明状态就注入 `Internal protocol check:` 推回循环，**最多两次**；两次后按副作用推断，再不行判 `protocol_violation` 失败 | `turn.ts:2788` / `turn.ts:2822` / `turn.ts:2868` | `AgentIdentity.max_self_repair`（`maos/agents/base.py:69`）声明了「Agent 内部自修复上限」，十个 agent 各自赋了值，**全仓无任何读取点** —— 是个死字段 | 抄思想 | 动内核（`maos/runtime/worker.py`） | 1.5 人天 | **复赛后** —— MAOS 的 agent 是代码返回值，不会「沉默」，所以搬不了原样。真正的价值是给 `max_self_repair` 一个执行点（失败重试在 agent 内做几次），但那要改 worker 主循环，三周内不碰 |
| 4 | 双阈值自动压缩（软 75% / 硬 95%）+ 两阶段（就地截断 → LLM 摘要替换），三条不变量保证 `call_id` 配对不破 | `turn-compaction.ts:144` + 该文件头注释 | 无任何等价物 | 抄接口 | 动内核（`maos/model/` 新增 history 层） | 3 人天 | **复赛后** —— 前置条件是 MAOS 先有多跳工具循环。没有 history 就没有东西可压；现在搬过来是给一个不存在的问题写解法 |
| 5 | CJK 感知的 token 估算：ASCII 按 3.5 字符/token、非 ASCII 按 1 字符/token。注释写明旧的 `length/3` 把中文低估 3 倍，**导致中文场景从来没触发过压缩**，直接撞硬顶而死 | `turn-compaction.ts:67` | 无本地估算，只在 `record_model_usage`（`maos/core/store.py:508`）记网关回吐的真实用量 | 抄代码 | 新增插件（`maos/obs/`） | 0.5 人天 | **复赛后** —— MAOS 是中文系统，这个坑迟早踩；但当下没有任何逻辑消费这个估算，先做等于空转。跟 #4 同批 |
| 6 | 上下文窗口按模型名查表算阈值，不写死数字。注释：写死 150K 阈值在 128K 模型上**永远不会触发**，turn 只会撞硬顶而死 | `turn.ts:915` `contextWindowFor` | `Tier` 三档抽象（`maos/model/client.py:34`）只表达贵/便宜，不带窗口信息 | 抄思想 | 新增插件（`maos/model/`） | 0.5 人天 | **复赛后** —— 与 #4/#5 同一个前置。单独做没有消费方 |
| 7 | 轮次状态五值：`done / continue / needs_clarification / blocked / waiting`。`waiting` 的语义被限定得很死——「你已经采取了行动，正在等外部响应」 | `tools-shared.ts:47` + `personas.ts:105` 的规则原文 | `_VALID_RESULT_STATUS = {"ok","failed","blocked"}`（`maos/contracts/events.py:198`）。`blocked` 一个值混装了两件事：「要问人」（`open_questions`）和「等外部系统回执」 | 抄接口 | 🔴 **动冻结契约**（`maos/contracts/events.py`） | 2 人天 | **复赛后** —— 铁律 1 + 距 9/22 只剩三周，赛前不赌。值得记一笔：cumora 的 `waiting` 和 MAOS 铁律 8「MAOS 不持有权威事实，权威状态归外部系统」是同一个诉求的两种表达，只是 MAOS 把它放在业务对象字段上，cumora 放在轮次状态上 |
| 8 | skill 渐进式披露：提示词里只有 name + description，全文由模型判断需要时用 `cumora skills read <name>` 自己拉进下一轮 | `skills.ts:250` + `turn.ts:2167-2170` | MAOS 提示词里根本不列 skill（skill 由 Python 代码显式调用，模型不选），所以当前无提示词膨胀问题 | 抄思想 | 新增插件（`maos/skills/`） | 2 人天 | **复赛后** —— 只有当 MAOS 要让模型自选 skill 时才有意义。届时「按 role 裁剪 + 两级披露」这套是现成答案，且 `SkillContract.purpose` + `owner_roles`（`maos/skills/contract.py:20`）已经够拼出这份索引，不用新建数据 |
| 9 | skill 安装的硬约束：路径穿越校验、单 skill ≤100 文件、单文件 ≤256KB、**拒绝覆盖同名**（要重装得先显式删） | `skills.ts:130-131` + `skills.ts:221` | `register_skill`（`maos/skills/registry.py:33`）是 `setdefault(...)[version] = cls` —— 同名同版本**静默覆盖**，后 import 的赢，且不报警 | 抄思想 | 新增插件（`maos/skills/registry.py` 加一行冲突检测） | 0.5 人天 | **复赛后** —— MAOS 的 skill 是代码里 import 的，不是运行时装的，撞名的概率远低于 cumora；但静默覆盖仍是个真实的调试陷阱。改动小，只是不紧急 |
| 10 | 终态语义验证器：小模型复核「模型说 done 了，可它真的交付了吗」，判据「表情或短确认不是交付物」 | `turn.ts:1128` / `turn.ts:1181` | 七道闸里 `_gate_schema`（`maos/runtime/gate.py:288`）已挡「零 artifact」，`_gate_acceptance`（`maos/runtime/gate.py:298`）按 `ctx.acceptance` 逐条判 | —— | —— | —— | **不做** —— MAOS 有显式 acceptance 清单，判据比让小模型自由心证硬得多。抄过来是退步。cumora 需要它，是因为它的任务没有验收标准这个字段 |
| 11 | 云端 / BYOA 两条路径共用一份工具定义与执行实现 | `tools-shared.ts` 整个文件的存在 | 已同构：Scripted / 真模型双模式共用 `ModelClient` 抽象（`maos/model/client.py:49`），由 `select_model_client`（`maos/model/client.py:272`）选路 | —— | —— | —— | **不做** —— 已有，且 MAOS 的分界线画得更早（在客户端层而非工具层） |
| 12 | 失败的 turn 不写回幂等指纹，保持可重试 | `turn.ts:3294` | 已同构且更强：幂等键**落库**（`maos/core/store.py:80`），`claim` 与 `on_task_result` 各一道；失败按 `attempt < max_attempts` 回 PENDING 重派（`maos/core/control_plane.py:365-372`） | —— | —— | —— | **不做** —— cumora 那个 `Map` 进程重启就失效，MAOS 这侧本来就是它想要的形态 |

## 4. 反向清单 —— 它做了但 MAOS 不该抄

判据一句话：*这个设计在解决我也有的问题，还是在解决它的用户量 / 多租户 / 向后兼容才有的问题？*

**一、多租户 `companyId` 贯穿全链路。** `turn.ts` 里几乎每一次 `recordEvent` 都要带
`companyId`，`runCompanyId` 的确定本身要走三级 fallback（inbox 行 → 会话查库 →
persona 兜底，`turn.ts:1611-1615`）；`installSkillFromManifest`
（`skills.ts:177`）为了让 Observability 面板能按公司过滤，专门回查一次 `participants`
表拿 tenant 填进 `agent_workspace.company_id`，注释还特意说明「没有它这一列会是 NULL，
面板就看不见文件了」。这是 SaaS 多租户的税。一人公司抄它，就是给每张表加一个
永远等于同一个值的列，外加每条链路多一次查库。**MAOS 现在没有这个字段，保持没有。**

**二、失败通知的小时级熔断（`FAILURE_NOTICE_HOURLY_CAP` + Redis INCR）。**
`postTurnFailureNotices` 里那段最长的注释（`turn.ts:1746-1772`）讲的是一次真实
事故：N 个 agent 在同一个广播房间里因同一个底层原因失败 → 每条失败通知本身又是一条
新消息 → 下一次唤醒的 inbox 变了 → 去重键跟着变 → 再发一条通知，`O(N²)` 级联，
几秒钟房间里堆出 200+ 条系统消息。修法是两层：去重键**过滤掉 system 消息**，
再加一个 Redis 里 1 小时 TTL 的计数器兜底。这个灾难的成因是
「agent 共享一个消息流 + 通知本身就是消息」。**MAOS 结构上不可能发生**：失败进
`TaskState` 状态机和 `event_log`，不进任何会触发再次唤醒的输入流。抄这套熔断，
是给一个不存在的病开药。

**三、人格自演进（`IDENTITY.md` / `SOUL.md` 由 agent 自己 `edit_file` 改）。**
这条不是规模包袱，是**领域错配**，但同样不该抄。cumora 的 IDENTITY/SOUL 描述的是
**风格与价值观**，漂移是产品特性（「数字同事会成长」）。MAOS 的 `AgentIdentity`
（`maos/agents/base.py:59`）描述的是 **权限**：`allowed_skills` / `allowed_tools` /
`write_scope` / `max_risk`，且被 `check_tool` / `check_risk` / `check_write`
（`maos/agents/base.py:133-150`）在运行时强制执行。让 agent 自改这份声明，
等于让它自己提权 —— 这不是「更像人」，是把整个安全边界交出去。
真要抄，只能抄「可写性梯度」这个**分层思想**：把风格与权限拆成两份东西，
风格那份可以让 agent 改。但那是复赛后的重构，不是三周内的事。

**四、`GLOBAL_RULES` 里那几百行社交礼仪。** 反独白闸的解释、引用回复的「↦ addressed
to YOU / not you」礼仪、Skype 表情指南、「先发一句 intent message 再干活否则同事会
撞车」（`personas.ts:105` 起，一直到 `personas.ts:271`）。这些全部在解决
「多个 agent 挤在同一个人类可见的聊天室里互相刷屏」。MAOS 的 agent 不在聊天室里，
它们在任务图上，彼此不可见，协作由 Manager 的 Plan 编排。把这几百行搬进 MAOS 的
系统提示词，是每次调用都付一遍钱去约束一个不存在的行为。

**五、`MAX_HOPS = 200` 这个数字本身。** 它是被 cumora 特定负载（视频下载、多步研究）
一路顶上来的经验值（`turn.ts:841-846` 的演进注释）。要抄的是那个**论断** ——
「跳数上限不该用来控成本，控成本的是压缩」—— 而不是 200 这个数。MAOS 将来真做了
多跳循环，起点该由 MAOS 自己的任务形态决定。

**六、steer（跑到一半把新消息插进 history）—— 标注为「场景不同」而非包袱。**
`tryDrainSteer`（`turn.ts:2353`）解决的是「人盯着 agent 干活时改主意」，配套还有
一整套字节预算、批量摘要、饱和告警。MAOS 的人类介入点在 Gate 与
`HumanApprovalQueue`（`maos/runtime/gate.py:773`），任务本身是秒级跑完的批处理，
人来不及插话。这条不是规模包袱，是场景不同 —— 如果 MAOS 将来做长跑任务，它会重新
变得相关。现在不做，但别把它归档成「垃圾」。

## 5. 我没看懂 / 没时间看的

- **`cli.ts`（6402 行）一行没读。** 它是 cumora 全部世界能力的真正实现（`cumora reply` /
  `react` / `kanban` / `doc` / `skills` 等所有子命令）。我对工具面的理解全部来自
  `tools-shared.ts:77` 那份 tool schema 的 `description` 文本和提示词里的用法示例 ——
  也就是**从说明书反推实现**。`CliSideEffect` 究竟在 CLI 里怎么产生、
  `bashOutputSideEffects`（`tools-shared.ts:589`）解析的是什么格式，我没有验证。
- **三份测试一份没通读。** `__integration__/agent-turn.test.ts`（1649 行）只用
  `grep` 定位过，`__tests__/agents-turn-compaction.test.ts`（778 行）和
  `__integration__/agent-tools.test.ts`（961 行）完全没打开。派单说读测试最省时间，
  这是本轮最该补而没补的一步 —— 我对「作者认为什么是主流程」的判断，全部来自
  代码注释而非测试断言。
- **`turn.ts` 的 wake prompt 组装段（约 1900–2160）只扫读。** `renderContext`
  （`turn.ts:559`，约 155 行）里那套引用回复标记、图片附件物化、`loadFaces` 头像注入、
  日历系统消息渲染，我只知道它们存在，没有跟进逻辑。
- **`turn-stream.ts`（228 行）只读了函数目录和两个超时常量**（`turn-stream.ts:16-17`：
  4 分钟空闲超时 / 6 分钟墙钟超时）。`applyResponseStreamEvent`（`turn-stream.ts:155`）
  和 `reduceResponseStream`（`turn-stream.ts:221`）的归约逻辑没读，所以「一跳的
  assistant text 是怎么从流事件里攒出来的」我说不清。
- **`hydrateFs` / `commitFs` 没读**（在 `server/src/runtime/fs-namespace.ts`，本轮没打开）。
  我只从 `turn.ts` 的调用点知道：turn 开始把工作区物化成真目录、`finally` 里第一件事
  是提交 diff 回 `agent_workspace`。四个持久化根目录的规则来自提示词文本
  （`personas.ts:105` 段），不是从实现确认的。
- **BYOA 那条路径完全没看。** 我只从 `tools-shared.ts` 这个文件名和它的模块注释推断
  「存在云端与 BYOA 两条路径且共用工具定义」，没有读 BYOA 侧的入口。
- **「同一个 turn 被重放两次会怎样」是代码推断，不是实测。** 我的结论（进程重启后
  `lastCompletedInbox` 是空的 → 会真的重放一次 → 护栏在 CLI 侧的反独白闸和
  `postSystemNotice` 的 `dedupeKey`）来自读 `turn.ts:838` / `turn.ts:3294` 的代码路径，
  没有跑过，也没在测试里找到对应用例来印证。这条如果要拿去做决策，得先验。

## 附录 A · 顺手发现的 MAOS 问题

本轮不改 MAOS，也不追加账本。以下四条留给整合轮统一折进 BACKLOG。

1. **`maos/agents/reviewer.py:83` 静默裸切 JSON。** `json.dumps(artifacts, ensure_ascii=False,
   default=str)[:8000]` —— 从 JSON 中间切一刀，Reviewer 模型收到的是语法破损的片段，
   且**不知道自己被截了**。artifact 一多（或某个 patch_set 的 files 字段一大），
   语义审查就基于残片出意见，而 `_parse` 那步只在模型输出不合契约时才兜底
   （`maos/agents/reviewer.py:59-61`）——模型硬着头皮输出了合法 JSON 的话，
   这就是一个完全静默的审查盲区。修法见第 3 节 #1。

2. **`AgentIdentity.max_self_repair` 是死字段。** 定义在 `maos/agents/base.py:69`，
   十个 agent 各自赋了值（`coding.py:37` 是 2、`architecture.py:82` 是 1、
   `testing.py:169` 是 0 并配了「测试不自修复」的注释），
   `scripts/gen_docs.py:158` 还把它当真字段生成了 `docs/agent-identity.md` 里 11 张表的
   一行。但**全仓（含测试）没有任何一处读取它**。要么给它接上执行点，要么删掉 ——
   现状是文档在承诺一个运行时不存在的约束，而且 `docs/agent-identity.md` 因为是
   自动生成的，看上去还很权威。

3. **`register_skill` 同名同版本静默覆盖。** `maos/skills/registry.py:33` 的
   `SKILL_REGISTRY.setdefault(name, {})[version] = cls` —— 两个模块注册同名同版本的
   skill，后 import 的静默赢，不报警。`registry.py` 的模块注释花了很大篇幅解释
   「保留历史版本是为了旧 Plan 可复现」，那么同版本被悄悄换掉恰恰打破这条承诺。
   一行 `if version in versions: log.warning(...)` 即可。

4. **「失败但没说为什么」在类型上合法。** `AgentOutput` 的 `open_questions` 默认空
   列表、`error` 默认 None（`maos/agents/base.py:94-95`），所以
   `AgentOutput(status="failed")` 和 `AgentOutput(status="blocked")` 都是合法构造。
   `ReviewerAgent._needs_human`（`maos/agents/reviewer.py:71`）已经在局部守住了这条
   （注释：产出空白意见书比没有意见书危险得多），但这是 agent 自觉，不是运行时强制。
   修法见第 3 节 #2。

