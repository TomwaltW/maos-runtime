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
