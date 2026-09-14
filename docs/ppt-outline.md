# 方案 PPT 逐页大纲

> 这份文件是 PPT 的**文字骨架**，不是 PPT 本身。人类照它去做版。
>
> **基线**：`147df03`（整合轮 5，Y 轮四轨 + Z 轮五轨全部合入）。全文每一条「可核验证据」
> 都在该基线上逐条打开核对过，核不到的断言已就地改弱 —— 见文末「核对台账」。
> 初版按 `42822fc` 写，两批回填台账记在文末。
>
> ✅ **OQ-1 已由人类答复（T 轮派单 §0.2 转述复赛规则原文，2026-08-29）**：评审维度与权重为
> **场景价值与复用性 20% ／ 多 Agent 协同 25% ／ Skill 工程体系 20% ／
> 工程落地与安全审计 30% ／ 开源贡献 5%**；交付形式 PPT 或 PDF，必要内容含
> 核心解决思路、技术亮点、**风险边界**、可复现路径；截止 **9 月 3 日 18:00**。
> 本文件此前一律不猜、不写预估权重的做法到此为止 —— 现在有权威口径，按它填。
> **十三条评委要求的对照表（表 A / 表 B）继续保留**（出处 `README.md` **§8
> 「与提案 / 比赛要求的映射」那张表** —— 按标题找，不按行号找：README 多轮膨胀，
> 写死的行号已经指偏过两次），
> 两套口径并行不冲突：十三条是「答没答到」，四维是「按什么打分」。
> 四维 → 页的承接见文末「T 轮渲染台账」的表 C。
>
> **页锚 P1–P14 由编排侧钉死，不许改编号与页名** —— `docs/submission-checklist.md`
> B 段的「PPT 页」列要按同一套锚填。P8 因内容过挤已拆成 P8a / P8b（编排侧允许的
> 子页形式），两者合起来仍是原 P8 一页的范围。

---

## P1 · 封面 · 一句话主张

**一句话主张**
领域无关的编排内核 + 可核验的运行证据 —— 换域零改动，每一步都能被外人重放。

**画面要素**
纯字封面。主标题一行，副标题两行，最下方一行等宽字体的命令：
`python3 scripts/verify.py`。不放架构图、不放 logo 墙。

**讲稿**
MAOS 是一个多 Agent 协作运行时。它解决的问题只有一个：让"多个 Agent 干完了一件事"
这句话可以被验证，而不是只能被相信。所以这页最下面这条命令，评委可以自己跑，
十项证据逐项重放，全绿才是零。

**可核验证据**
`README.md:1-18`（标题、副标题与那条 `verify.py` 命令逐字对应本页文案）

**对应评委要求编号**
—（封面页，不单独扛要求）

**不许说的话**
- 不许说「基于 AutoGen 构建」—— 只能说「AutoGen 是可插拔内核之一，未在复赛演示中启用」
  （`docs/submission-checklist.md §A-4`、`docs/agentteams-mapping.md:69-79`）。

---

## P2 · 评委三段反馈，正面接住

**一句话主张**
三条诊断我们没有绕开，每一条都指向一页具体的、可核验的落点。

**画面要素**
三行表。左列是评委原话诊断，中列是落在哪一页，右列是那一页给得出的证据。
右列全部是等宽字体的命令或路径，不写形容词。

| 评委诊断 | 落在 | 证据 |
| :-- | :-- | :-- |
| 没有可执行制品和运行证据 | **P11** | `python3 scripts/verify.py` → 10/10 PASS |
| 现实业务锚点不足 | **P3 + P10** | 退款域纵切（场景 6 / 7），不是软件域自证 demo |
| 「所有 Agent 都回复完成」≠ 业务成功 | **P10** | 场景 7：`biz_status=compensated`，`settled` 观察 **0 条** |

**讲稿**
上一轮的三条反馈，我们没有换个说法绕过去。第一条要可执行制品，我们给一条命令；
第二条要现实业务锚点，我们把整个演示换成制造企业售后退款；第三条最要害，
它正是我们这一版的主线，第十页专讲。

**可核验证据**
`docs/submission-checklist.md §B`（「三条诊断」表 —— 三条诊断的原文与要求指向的落点）

**对应评委要求编号**
—（本页扛的是三段反馈诊断，不是十三条要求）

**不许说的话**
- 不许把三条诊断说成「已全部解决」。第二条第三条有代码有证据，
  第一条是评委自己跑了才算数 —— 这一页只承诺「指得出落点」。

---

## P3 · 从一条退款说起

**一句话主张**
一条真实形态的售后退款诉求，从三处来源进来，走完受理、裁定、核算、放行、支付、通知。

**画面要素**
`README.md:28-40` 那张八行表照搬上版：左列「谁」，中列「干了什么」，右列「留下了什么」。
右列全部是落库的对象名（`refund_case` / `plan` + 5 个 `task` / `finance_entry` /
`payment_observation`），不是描述性文字。底部一行小字标数据口径。

**讲稿**
客户报修一批轴承，要求退款六千八，诉求同时来自工单、客服聊天和照片三处。
受理 Agent 去重聚合成一个 case，Manager 规划出 DAG —— 这个 Manager
和软件交付域是同一个，零改动。往下每一步都往库里落一个真实业务对象。

**可核验证据**
- `README.md:21-43`（本页叙事与表格的原文）
- 一条可跑的命令：`python3 run.py --scenario 6`

**对应评委要求编号**
**1**（脱敏真实退款需求的可执行纵向切片）、**3**（关键 Skill 的真实调用）、
**6**（业务对象关联到同一案例）

**不许说的话**
- 不许说「真实企业政策」—— 只能说「按行业惯例构造的合成数据」（`docs/submission-checklist.md §A-4`）。
- 不许说「接入了支付宝」—— 只能说「错误码与异步时序对齐支付宝开放平台公开规范；
  演示用模拟实现」（`docs/submission-checklist.md §A-4`）。

---

## P4 · 架构一眼

**一句话主张**
四块：冻结契约、领域无关内核、按域实现、旁路 —— 换域时只有第三块动。

**画面要素**
🔴 **必须直接用 `docs/architecture.md:12-56` 的那张 mermaid 分层图**，
不许另画一版。四个 subgraph 的配色区分开：`FROZEN` 冷灰、`KERNEL` 主色、
`PLUG` 亮色（这是换域时唯一动的一块）、`SIDE` 淡色描边。

**讲稿**
从上往下四块。最上面是冻结契约，事件和状态机，换域时 git diff 严格为零。
中间是领域无关内核，Control Plane、Worker、Gate。第三块才是按域实现的
Skill、ToolPort 和业务对象。最下面是旁路 —— 缺席也不阻塞主链路。

**可核验证据**
`docs/architecture.md:12-56`（分层图源码；`README.md:61-88` 是同一张图的精简版，
两者的分块与命名一致）

**对应评委要求编号**
—（架构页是后续各页的地基，不单独扛要求）

**不许说的话**
- 不许说「PolarDB 上生产可用」——**该实例当前 `ssl=off`，公网链路明文，未做加固**
  （出处 `docs/BACKLOG.md` 的 `## polardb-live` 第 1 条）；也不许说「本仓库缺省支持中文分词检索」——**`zhparser 2.2` 已在 PolarDB 实例上装成、`zhcfg` 检索配置已建、中文召回已实测**（`zhcfg` 全文 8/10、向量 top-5 10/10、`simple` 通道全部抛错），
  **但仓库缺省未切到 `zhcfg`**（`MAOS_PG_FTS_CONFIG` 不指它），切过去会让 `test_chinese_query_raises_instead_of_silently_missing` 变红（实测 `39 passed, 1 failed`）；`pg_jieba` / `pg_bigm` / `pgroonga` 仍只验到「在可用列表里」，一个都没装（出处 `docs/BACKLOG.md` 的 `## polardb-live-r2` 第 1 条、`deploy/polardb-live.md` §1.4）。
  只能说「PG 后端已在本机 Docker PostgreSQL 16.15 + pgvector 0.8.6 上实测跑通
  （`maos/tests/test_pg_store_live.py` 22 条）；**阿里云 PolarDB PostgreSQL 版真实例也已连通实测**
  （2026-08-30，冒烟五步 5/5，`ts_rank` 与向量距离与本机 Docker 逐字节相同）。后端不可用时抛
  `PgBackendUnavailable(NotImplementedError)`，**仍不回落 sqlite**」
  （`docs/submission-checklist.md §A-4`）。
- 图里 `KERNEL` 那块不许标成「零改动」。零改动的是 `maos/contracts/`；
  `core/` 与 `runtime/` 不是零，读法见 P12。

---

## P5 · 状态机与七道闸

**一句话主张**
全系统只有一个出口能改状态，非法迁移抛异常；产物过七道闸，任何一道有 blocker 就返工。

**画面要素**
左半张状态迁移图（`PENDING → DISPATCHED → RUNNING → AWAITING_REVIEW →
DONE / BLOCKED / FAILED`），右半张七道闸竖排列表，第六第七道用不同颜色标出
（那两道是退款域上线时新增的、但仍领域无关的判据）。底部贴场景 7 的真实迁移轨迹片段。

**讲稿**
状态迁移只有一个出口，`_transit`，每一次都过断言、每一次都落一行 event_log。
产物要过七道闸：schema、验收、安全、证据、补偿、财务、网关。第六道管财务凭据，
第七道管网关回执。任何一道判 blocker，任务回去返工，不是记个警告就放行。

**可核验证据**
- `maos/contracts/states.py:26`（`TASK_TRANSITIONS` 迁移表）
- `maos/core/control_plane.py:188-192`（注释「唯一的状态迁移出口」+ `_transit`，
  首行即 `assert_transition`）
- `maos/runtime/gate.py:254-260`（七道闸的元组，顺序即执行顺序）

**对应评委要求编号**
**4**（返工 / HITL Trace）、**10**（减少遗漏财务复核、错误套用政策、无限重试）

**不许说的话**
- 🔴 不许说「自动换渠道**重试到上限**」。整合轮 5 合入 Y-4 后，场景 7 **确实演到了
  换渠道**，但只 replan **1 次** —— 撞第二个码（`ACQ.SYSTEM_ERROR`）就一票否决落人工，
  `MAOS_MAX_REPLAN` 默认 2，**根本没打满**。可以说的原话见
  `docs/submission-checklist.md` A-4 的 replan 行。
- 🔴 不许说「换了渠道就成功了」。换完仍未成功，业务状态收在 `compensated`。

---

## P6 · AgentTeams 事件链

**一句话主张**
AgentTeams 的五个概念逐项落到代码位置；事件链的权威记录不在房间里，在 `event_log` 表。

**画面要素**
`docs/agentteams-mapping.md` 的「五项映射」那张五行表照搬，保留「代码位置」与「状态」两列。
（状态列**现在五行确实全是 ✅**，照搬即可 —— 但它统一成 ✅ 不是为了好看：第 1／3／4 行的 ✅
后面各自跟着一句实测出处（「真房间已接通」「真房间实测：两轮跑出 41 条房间消息」
「三种在真房间各实测一次」），依据是 `evidence/room/` 的 5 张 Element 截图 + 逐字副本。
🔴 **照搬时必须把那些出处一起搬过去** —— 只搬 ✅ 不搬出处，这一页就变成了没依据的自吹。）
右下角一个小方块写「当前真实状态」三行。

**讲稿**
Team 对应一个 Matrix 房间，Member 对应可插拔的 Agent 池，事件链走镜像发布，
HITL 是房间里的斜杠命令，第五项是可观测。第五项要单说：事件链的权威记录
不在房间里，在 event_log 表里，房间挂了不影响重放。

**可核验证据**
- `docs/agentteams-mapping.md` 的「五项映射」表（实测在 `:16-24`），每项带 `文件::符号`
  —— 那张表 T31 起已从 `文件:行号` 改成 `file::symbol`，因为 16 处行号里 15 处已漂
- `docs/agentteams-mapping.md` 的「当前真实状态（不吹）」小节（实测在 `:58-92`；
  上一版写的 `:52-63` 已漂，那两行现在落在 C 档选型表里）
- `evidence/room/`：5 张 Element 截图 + `README.md` + 逐字副本 `transcript.md`
  —— 这一页所有「已接通」的说法都指得到这里

**对应评委要求编号**
**2**（AgentTeams 事件链）

**不许说的话**
- 🔴 不许说「**退款**全过程在 Element 里跑通」。房间里跑的是 `room_demo`，一个
  `role=coding` 的软件域任务（标题「变更生产环境配置」）；退款域场景 6／7 的证据是
  机器侧的 `evidence/scenario-6,7/`，**没进过房间**。
  能说的是：「镜像层已实现，降级路径实测等价；真房间三条路径已实测跑通，证据在
  `evidence/room/`」（`docs/submission-checklist.md §A-4`、`docs/agentteams-mapping.md`
  的「当前真实状态（不吹）」小节）。
- 🔴 不许说「`/reject` 之后补偿在房间里可见」。`CompensationExecuted` 只落 `event_log`、
  从不 publish，**永不进房间**；`04-reject-compensation.png` 的文件名比它能证明的东西大，
  它证明的是「驳回生效 + Plan 落 FAILED」。补偿的证据在 `evidence/scenario-7/`。
- 🔴 不许说「房间镜像稳定可靠」。Synapse 默认限流实测打穿过（一轮 approve 4 条 429，
  `_NioChannel` 的 10s 超时因此误报过「房间回话失败」而消息其实送达了）。
- 这一页要**主动把房间实拍摆出来**（见「留给整合轮的一件事」那节的 slot），不要等评委问。
  🔴 **自曝的对象变了**：上一版自曝的是「真房间未接通」——那句现在已被自己的证据推翻，
  再讲会让评委怀疑我们不清楚自己仓库里有什么。现在该主动交代的是**演示当天的前置**：
  系统 `python3` 没装 matrix-nio，必须用 `~/.maos-matrix/venv/bin/python` 才走得到活路径，
  拿系统解释器起房间会**静默降级成 log-only**（终端照刷「房间消息」，房间里一条都没有）。
  这句自己先说，比被追问「那你现场开一个」再解释强。

---

## P6b · 单案例事件链时间线（圆桌与 DAG 在同一条线上）

**一句话主张**
五岗圆桌先议、五岗六任务的 DAG 再执行 —— 它们不是两套日志，是 `event_log` 上**一条**按 `seq` 升序的时间线。

**画面要素**
一条自上而下的时间轴，左侧标 `seq`，右侧标事件名；圆桌段与 DAG 段用两种底色分开，
**但轴本身中间不断开**（断开就把这一页要证的事讲反了）。轴上钉七段锚点。

> 🔴 **做图时 `seq` 的具体数字现场从 `event-chain.json` 抄，别从这张表抄。**
> 证据束每重产一次，`seq` 都可能整体平移；这张表只钉**顺序与结构**，那才是不会过期的部分。

| 段 | 事件 | 这一格在说什么 |
| :-- | :-- | :-- |
| 开头两条 | `ToolInvoked` → `SkillInvoked refund.snapshot_check` | 议事之前先读外部当前订单版本 |
| 圆桌段 | `RoundtableRound` → 5 × `RoundtableSeatSpoke` | 受理 / 规则 / 证据 / 风险 / 财务五岗依次发言，中间夹着 `refund.evidence_check` 与 `refund.risk_screen` 两条真调用 |
| 检索段 | `KbRetrieved` → `SkillInvoked kb.retrieve` → `PlanAdvised` | 检索九类流程知识，Planner 据此给必要任务与审批人 |
| 审批那一跳 | `PlanApproved` → `PlanTransition PENDING→RUNNING` | 计划审批是人做的，事件里带操作者 |
| DAG 段 | 25 × `StateTransition`，中间夹着 **8** × `SkillInvoked` | 五岗六任务逐个执行，每次调用带 `invocation_id` |
| 付款那一串 | 4 × `RefundBizStatusChanged` | `submitted` → `approved` → `gateway_accepted` → `processing` → `settled`，每一跳的 `reason` 里带网关回执 |
| 收口三条 | `CaseOutcomeComputed` → `CasePromoted` → `SkillInvoked kb.sink` | 四判据算完才谈晋升 |

> **「DAG 段」那一格的两个数怎么数出来的**（`evidence/case-real-01/happy/event-chain.json`，做图前自己复核一遍）：
> `StateTransition` 全线 **25** 条，全部落在 DAG 段里 —— 第一条到最后一条之间就是这一段的边界；
> 落在这段边界内的 `SkillInvoked` 是 **8** 条。**全线 `SkillInvoked` 是 15 条**，另外 7 条在段外：
> 圆桌段 6 条（`refund.snapshot_check` / 两次 `refund.evidence_check` / 两次 `refund.risk_screen` /
> `kb.retrieve`）、收口 1 条（`kb.sink`）。**别把 15 写成「夹在 25 条里」** —— 那是把全线总数
> 安到了一段上，当场翻文件就露。

右下角小字：**一条命令重产，条数以 `event-chain.json` 的 `count` 字段为准**。

**讲稿**
评委问 AgentTeams 事件链，我们给的不是截图，是一张表里的每一行。圆桌五岗说完话，
计划才送审批；审批过了 DAG 才开跑 —— 这两段共用同一个 `seq` 序列，所以「谁先谁后」
不是我们讲出来的，是库里排好的。中间这四次业务状态变更，每一次都指得到是哪个网关回执。

**数据从哪来**
- `evidence/case-real-01/happy/event-chain.json` —— `events` 按 `event_log.seq` 升序，
  `count` 给总条数、`by_type` 给各类计数（`RoundtableSeatSpoke` / `SkillInvoked` /
  `StateTransition` / `RefundBizStatusChanged` / `PlanAdvised` / `CasePromoted` 逐类都在里面，
  **上图前照这个字段抄，不要照本页抄**）
- `evidence/case-real-01/happy/result.json` —— `plans[0].tasks`，DAG 那一侧的**六个任务、
  五个角色**从这里数（受理岗担受理与通知两条，政策、渠道、财务、付款各一）
- 同束的 `roundtable.json` —— 五岗发言全文，讲解时的备份画面
- `scripts/replay_roundtable.py` —— 零模型、只读 `event_log` 重建五岗顺序的回放器

**对应评委要求编号**
**2**（AgentTeams 事件链）、**3**（关键 Skill 的真实调用）

**不许说的话**
- 🔴 不许说「这条时间线能在 `evidence/report.html` 里看到」。渲染器只扫 `scenario-*`
  （`scripts/render_trace.py` 里那个 glob），单案例束与 R8 都进不去。要看这条线，
  打开 `evidence/case-real-01/happy/event-chain.json`。
- 🔴 不许在这一页现场回放**这条案例**的圆桌。`scripts/make_case_bundle.py` 的库建在临时目录、
  跑完即销毁，**束里没有 `maos.db`**，而 `scripts/replay_roundtable.py` 要的是库。
  真要现场演回放，先 `python3 scripts/room_team_smoke.py --db <路径>` 落一个库再回放 ——
  那演的是圆桌机制本身，不是这一单。
- 🔴 不许把 `RoundtableSeatSpoke` 说成「五个 Agent 各自调了模型」。这一束是 Scripted 跑的，
  `roundtable.json` 里每一岗的 `spoken_by_model` 都是 `false`、`fallback_reason` 是 `no_model`,
  发言内容是**确定性事实卡**。真模型那一版在 `evidence/case-real-01/happy-live/`，
  且 `scripts/verify.py` 按口径跳过它（输出末尾自己列名）。
- 🔴 **返工只在 `gateway_fail` 这一束，别说「happy 束里也有」。** 顺利路径钱一次就退成了，
  本来就不该返工，它的 `hitl-trace.json` 里没有 `kind=rework` 是对的。返工那条在
  `evidence/case-real-01/gateway_fail/hitl-trace.json`：`kind=rework`、`actor=gate`、
  `transition` 字面值 `AWAITING_REVIEW->REWORK [gate_rework]`，挂在付款任务上。
- 🔴 **讲返工时别说「重试到上限」，也别说「换渠道重发」。** 实跑是网关先回可重试码
  `40005`、闸判 blocker、**同渠道同幂等键重发一次**（`skills.json` 里 `payment.execute`
  的 `invocations` 是 2），网关第二次改口 `ACQ.SELLER_BALANCE_NOT_ENOUGH`（终态失败），
  于是一次转人工、不再重发。`outcome.json` 里三条付款观察共用同一个 `request_id` ——
  「同一笔」这件事当场翻得出来。（场景 7 那条线**确实**有「改派备用渠道」，别把两条混讲。）

---

## P7 · Skill / ToolPort 九要素契约

**一句话主张**
Skill 与 ToolPort 都是九要素声明，投放一个文件即注册；两份目录文档由代码生成，不是手写的。

**画面要素**
左右分栏：左栏 `SkillContract` 九要素，右栏 `ToolPort` 九要素，字段名用等宽字体。
底部一行横条：「13 个 skill / 5 个已实现工具（`git-mcp` 走 MCP stdio）/ 11 个 Agent
Identity —— 三份文档全部代码生成」，
配一条 `python3 scripts/gen_docs.py --check` 的终端输出截图。
表格里 `entry` 行加浅绿底色 —— 它是换 MCP 的唯一替换点，但**行内不加字**：
这一格每行只容 15 个字，多一个短语就折行、把底部的「投放即注册」卡顶出页外
（2026-09-01 渲染实测，30px 裁切）。

**讲稿**
Skill 和 ToolPort 都是九要素 dataclass，失败形态和安全边界是必填项 —— 失败被吞掉
就等于没有边界。注册靠类装饰器，模块被 import 就进注册表，投放即注册。
这三份目录文档是从运行时代码生成的，`--check` 不一致就非零退出。
五个工具里有一个（`git-mcp`）的入口走 MCP —— 换传输层只换 `entry` 一项，
审计行和白名单一个字不动。

**可核验证据**
- `maos/skills/contract.py:23-34`（12 个字段；`docs/skill-catalog.md:7` 说明
  `name+version` 是主键、其余 10 个合成 9 项要素，`failure_policy` 与 `max_retries` 同属一项）
- `maos/tools/port.py:23-32`（ToolPort 九要素字段）
- `maos/skills/registry.py:33-40`（`register_skill` 类装饰器，docstring 明写「模块被 import 即注册」）
- 一条可跑的命令：`python3 scripts/gen_docs.py --check` → `exit=0`，`3 份文档与代码逐字节一致`

**对应评委要求编号**
**3**（关键 Skill 的真实调用 —— 退款域 7 个 skill 见 `docs/skill-catalog.md:15-29` 表中「制造售后退款域」行）

**不许说的话**
- 不许说「接入了支付宝」。`gateway.refund` / `gateway.query` 两个 ToolPort 是
  对齐公开规范的模拟实现（`docs/submission-checklist.md §A-4`）。

---

## P8a · RAG 面向 workflow 规划（一）：两阶段检索

**一句话主张**
阶段一是硬约束不是打分项，按评委给的字段顺序结构化预过滤；阶段二才是四通道混合召回。

**画面要素**
一张漏斗图。上半漏斗标七个过滤字段，从左到右依次收窄：
`tenant_id → biz_type → channel_id → region → sku → policy_version → workflow_version`，
`tenant_id` 那一格加粗标红。下半漏斗四条并行通道汇成一个分。

**讲稿**
检索分两阶段。阶段一按评委给的字段顺序做结构化预过滤，是硬约束不是打分项 ——
不产生分数，只决定谁有资格进阶段二；查询不带租户直接返回空。
阶段二才打分，规则编号、错误码、全文、语义四通道加权融合。

**可核验证据**
- `maos/kb/retriever.py:51-55`（`#: 阶段一的过滤顺序。**顺序即语义**` + 字段元组，
  与评委给的顺序逐字一致）
- `maos/kb/retriever.py:151-163`（`prefilter`，docstring 明写「这是硬约束不是打分项」；
  `tenant_id` 缺失返回空）
- `maos/kb/retriever.py:179`（阶段二四通道混合召回段起点）

**对应评委要求编号**
**8**（RAG 面向 workflow 规划 —— 机制侧）、**9**（先结构化过滤再组合召回）

**不许说的话**
- 不许说「后端已可插拔切 PolarDB」（`docs/submission-checklist.md §A-4`）。
- 不许把场景 6 的检索说成「召回准」。见 P8b 的易变提示。

---

## P8b · RAG 面向 workflow 规划（二）：改变了计划，且被护栏挡住

**一句话主张**
RAG 的价值要用「有无对照」证明 —— 两版 DAG 的 diff；而检索结果只能补任务，不能替代事实或跳审批。

**画面要素**
左半：`evidence/scenario-R5/dag-diff.json` 的有无两版 DAG 对照（关无 RAG 一版少一个
财务复核任务）。右半：三条护栏竖排 —— 只增不删 / 不替代事实 / 不跳审批，
每条配一句负例。

**讲稿**
说 RAG 有用，不能靠形容词，要靠对照。关掉检索跑一遍、打开再跑一遍，把两版 DAG 做 diff，
差异就是它的价值。同时检索结果不许乱来：只能往 DAG 里补任务不能删，
不许携带订单事实字段，不许把风险等级降下来跳过审批。

**可核验证据**
- `maos/kb/guardrails.py:1-16`（模块 docstring 引评委原话，拆成三条断言；
  代码里是 4 个 assert 函数 —— `assert_no_dependency_removed` 是第 1 条「只增不删」
  的依赖侧半条，`check_all` 在 `:149` 一次跑完）
- `maos/kb/experiment.py:788`（写出 `dag-diff.json`）
- `README.md:250`（证据索引里 `dag-diff.json` 的定位：「对照实验的判定面」）
- 一条可跑的命令：`python3 -m maos.kb.experiment`

**对应评委要求编号**
**8**（RAG 面向 workflow 规划 —— 效果侧）、**11**（历史流程不能替代当前订单事实和人工授权）、
**13**（只有证据完整且外部结果明确的案例进默认知识层）

**不许说的话**
- 🔴 不许说「场景 6 演示了 RAG 召回得很准」。整合轮 5 后场景 6 实测
  `candidate_count=3` / `hit_count=3`（Y-2 把 W-1 语料播了进去，此前是 0），
  「RAG 接上了」在场景 6 上证明到的是**链路通 + 确实召回到东西**，
  证明不到**召回准** —— 后者的证据只在 `evidence/scenario-R5/` 的有无对照实验里。

---

## P9 · 权威事实边界

**一句话主张**
MAOS 不持有权威事实。全系统只有 `payment.observe` 写得进 `settled`，且必须同事务附回执 —— 越权抛异常并落证据。

**画面要素**
一张边界图：左边「MAOS 持有的：观察与推断」，右边「外部系统持有的：订单、支付、库存的权威状态」，
中间一道竖线，线上只开一个小口标 `payment.observe`。右下角贴核验器抓到那次绕过的
FAIL 输出片段。

**讲稿**
这是我们的第八条铁律：MAOS 只持有观察和推断，权威状态永远归外部系统。
`settled` 这个终态，全系统只有一个 skill 写得进去，而且必须同事务附上网关回执。
没有回执的 settled 就是把外部状态写死为终态，那是 bug 不是功能，直接抛异常。

**可核验证据**
- `maos/domain/refund/guard.py:33`（`AUTHORITATIVE_STATES = frozenset({"settled"})`）
- `maos/domain/refund/guard.py:307-315`（「③ 权威终态必须有回执」+ 缺字段时
  `_log_violation` 落库并 `raise AuthoritativeFactViolation`）
- `docs/authoritative-facts.md:113-145`（**实况**：核验器第 3 项真的抓到过一次绕过，
  含根因、为什么两轨各自全绿、以及那一行修法）

**对应评委要求编号**
**7**（外部系统保留权威事实，区分已提出 / 处理中 / 已到账）

**不许说的话**
- 不许说「接入了支付宝」（`docs/submission-checklist.md §A-4`）。
- 不许把「抓到过一次绕过」讲成「我们从没出过错」。这一页最值钱的恰恰是
  **核验器自己抓到了**，讲的时候要把它当正面证据讲，不要淡化。

---

## P9b · PolarDB 上的十对象与版本

**一句话主张**
同一个 case 挂十类业务对象，每一条都带版本、都能顺着 `business_ref` 从 DAG 指回去；
这套表可以整体搬到 PolarDB —— **控制面不搬**。

**画面要素**
中间一张十行表（对象类型 × 条数 × 版本 × 由哪个任务挂上），左边一列 DAG **六个任务**，
箭头从任务指向对象、箭头上标 `business_ref`。**六条里有五条出箭头** —— 「渠道商核销」
那一条不挂业务对象，画成没有出边的那一格即可（`business-objects.json` 里
`task_id` 只出现 intake / policy / finance / payment / notify 五种，数一遍就知道）。
右下角一块小图画**两个库的切分线**：
业务对象 + `kb_doc` → PolarDB；`plan` / `task` / `artifact` / `event_log` → 本地 SQLite。

| 对象类型 | 顺利路径条数 | 版本 | 挂它的任务 |
| :-- | --: | :-- | :-- |
| `refund_case` | 1 | v0 | intake |
| `order_snapshot` | 1 | v1 | intake |
| `product_snapshot` | 1 | v1 | intake |
| `customer_evidence` | 4 | v1 | intake |
| `policy_rule` | 8 | v2 | policy / finance |
| `approval_record` | 1 | v1 | finance |
| `finance_entry` | 1 | v1 | finance |
| `refund_request` | 1 | v1 | payment |
| `payment_observation` | 1 | v0 | payment |
| `notification` | 1 | v1 | notify |
| **合计** | **20 条** | `resolved 20` / `dangling 0` | |

**讲稿**
十类对象、20 条引用，一条都不悬空。政策规则挂的是 v2 —— 那是下单那天锁定的版本，
不是今天最新的版本，「错误套用政策」这件事在这里就被版本挡住了。失败路径上
`approval_record` 会出现 v1 和 v2 两条：财务闸放行一次、付款闸拒签一次，两次审批各自留痕。
这套表跑在 SQLite 上，也跑得了 PolarDB —— 同一份 DDL，PG 侧现翻，不手抄第二份。

**数据从哪来**
- `evidence/case-real-01/happy/business-objects.json` —— 20 条 `object_type` / `object_id` /
  `object_version` / `resolved`，末尾 `resolved 20`、`dangling 0`
- `evidence/case-real-01/gateway_fail/business-objects.json` —— 21 条，`approval_record`
  两个版本、多一条 `compensation_record`
- `deploy/polardb.md`（三步怎么迁）与 `deploy/polardb-live.md`（真实例上跑通了哪几条，含没跑通的）
- `maos/domain/_dbport.py`（`MAOS_DOMAIN_BACKEND` 双后端，DDL 只写一份）
- `docs/architecture.md` §5 的切分线那一段

**对应评委要求编号**
**6**（业务对象关联到同一案例）、**7**（外部系统保留权威事实，区分三态）

**不许说的话**
- 🔴 不许说「全部控制面上 PolarDB」。可切的是**业务对象 + 知识层**；
  `plan` / `task` / `artifact` / `event_log` 四张控制面表仍写死本地 SQLite
  （`maos/flows/common.py` 的 `build()`）。
- 🔴 不许说「跑在 PolarDB 上」。全部证据束跑的都是本地 SQLite；PolarDB 的口径是
  **本机 Docker 同构验证 + 真实例冒烟**（2026-08-30，高权限账号五步 5/5、
  控制台建的普通账号 2/5），逐条实录在 `deploy/polardb-live.md`。
- 🔴 不许说「十类齐」。顺利路径的 `business_ref_coverage` 是 **9/10** —— 缺的是补偿记录，
  因为钱退成了的案子本来就不该有补偿；第十类在 `evidence/case-real-01/gateway_fail/` 那一束
  （那束反过来缺 `notification`）。为凑数造一条补偿记录，会让这条引用指向一条本不该存在的
  记录，理由记在 `docs/DECISIONS.md`。
- 🔴 不许说「缺省支持中文分词检索」（`docs/submission-checklist.md §A-4`）—— 缺省走 `simple`。

---

## P10 · 失败路径纵切（场景 7）

**一句话主张**
四个 Agent 全部回复「完成」，而这一单没有成功 —— 系统如实这么记了。

**画面要素**
整屏贴 `python3 run.py --scenario 7` 的真实终端输出尾部：状态迁移轨迹
（`task-s7-payment  BLOCKED → FAILED  [human_reject]` 那一行加高亮）+ 底部五行汇总。
不加任何美化边框，就是终端原样。

**讲稿**
同样的诉求，网关返 `ACQ.SYSTEM_ERROR`，轮询三次仍问不出结果。看这五行：
业务状态 compensated，全程没进过 settled；settled 观察零条 —— 没问出终态就一条都不该有；
补偿两行；Plan 终态 FAILED。四个 Agent 都回复完成了，而这一单确实没成功。

**可核验证据**
- 一条可跑的命令：`python3 run.py --scenario 7`
- `maos/flows/scenario_7.py:692-696`（本场景存在的理由，两条断言：
  `biz_status == "compensated"`、`settled_rows == 0`）
- `README.md:43`（「四个 Agent 全部回复「完成」，而这一单没有成功，系统如实这么记了。」）

编排侧在 `147df03` 上实跑到的原文（可直接进讲稿，逐字未改）：

```text
  业务状态  : compensated（全程没有经过 settled）
  settled 观察: 0 条 —— 没问出终态就一条都不该有
  补偿记录  : 2 行 ['manual_ticket', 'refund_request_revoked']
  补偿事件  : 1 条 CompensationExecuted
  Plan 终态 : FAILED（主管驳回，业务确实没成功）
  换渠道重试: 1 次 replan（40005 触发，ACQ.SYSTEM_ERROR 一票否决，没有自旋）
```

**最后一行是 Y-4 合入后新增的**，且它正好把这一页的主张钉死：
系统不是「不会重试」，是**试过一次、判出不该再试就停手**。
念的时候切勿说成「重试到上限」—— 上限是 2，它只用了 1 次。

**对应评委要求编号**
**1**（可执行纵向切片 —— 失败路径侧）、**12**（以退款到账 / 客户确认 / 人工纠错验证 DAG）

**不许说的话**
- 🔴 不许说「七个场景都跑成功了」。场景 7 的 Plan 终态是 FAILED，**那正是它要演的**
  （`docs/submission-checklist.md §A-4`）。
- 🔴 不许说「重试到上限」。整合轮 5 合入 Y-4 后场景 7 **能演换渠道**了，屏幕上打的是
  `换渠道重试: 1 次 replan（40005 触发，ACQ.SYSTEM_ERROR 一票否决，没有自旋）`，
  状态轨迹里有 `AWAITING_REVIEW -> REWORK [gate_rework]` → `REWORK -> PENDING [requeue]`。
  **1 次，不是上限**；最终仍走 `effect_risk=H` 那条 HITL 入口落人工驳回。

---

## P11 · 一条命令核验

**一句话主张**
检索不准顶多说效果一般；无法核验就是零分 —— 所以十项证据每一项都能被外人独立跑一遍。

**画面要素**
整屏终端输出，十行 PASS + 一行 RESULT，等宽大字号。左侧配一张十行小表：
每项失败**意味着什么**（`README.md` §3 那张表）。

**讲稿**
这一节是给评委的。两条命令：一条生成全部证据束（七个场景 + RAG 对照），一条逐项重放校验。
十项分别验证据没被篡改、业务锚点不悬空、权威边界没被绕过、事件链完整、
RAG 命中是真的、Agent 完成没被当成业务成功、知识层没被污染、成本归得到人头、
这束证据是不是当前代码跑的、四判据本身是不是真的。全绿退出 0。

**可核验证据**
- `scripts/verify.py::CHECKS`（函数清单，顺序即输出顺序）
- `README.md` §3（一条命令核验：两条命令、当场实跑输出、十项各验什么）
- 现行读数不写死在本文件里，**台上以当场输出为准**；离线对数看 `docs/expected-metrics.json`
  的 `verify_result_line`。

🔴 **下面这块是史料，不是现行读数。** 它是**八项时代**（基线 `dd3edff`）的输出，
今天跑出来是**十项**（多了 `provenance` 与 `case-outcome` 两行），分母也不是 8。
留着它是为了说明「读数变过几轮、每轮都有出处」，**照抄上台就是念过期数字**。

编排侧在 `dd3edff` 上实跑到的八行（2026-08-31 主机与容器各跑一次，两次一致；
读数变过四轮，**只认这一份**：`business-ref 23/23`+`kb-hit 1/1` 是 X 轮读数、
`74/74`+`4/4` 是 Y 轮前读数、`77/77`+`7/7` 是合 Y-4 前读数，
**七行没有 `cost-attribution` 那一行**的都是第 8 项落地前的旧读数，都别照抄）：

```text
[PASS] hash-integrity       86/86
[PASS] business-ref         35/35
[PASS] authoritative-fact   3/3
[PASS] trace-tree           29/29
[PASS] kb-hit               7/7
[PASS] business-outcome     10/10
[PASS] history-case         1/1
[PASS] cost-attribution     39/39

RESULT: 8/8 PASS
```

**对应评委要求编号**
**5**（Evidence Bundle）、**6**（业务对象关联到同一案例 —— verify 第 2 项）、
**12**（以到账 / 客户确认 / 人工纠错验证 DAG —— verify 第 6 项）

**不许说的话**
- 不许说「七个场景都跑成功了」（`docs/submission-checklist.md §A-4`）。
- 🔴 不许念本页那块 `dd3edff` 的八行当现行读数 —— 它是史料，今天是十项。
- 不许把 `warn:` 行藏起来。`authoritative-fact` / `trace-tree` / `business-outcome`
  三项会附若干 `warn:`，它们不改判定但是真的（`README.md` §3 那段括注）。台上被问到要能直接答。
- 不许说「新克隆的仓库直接跑 `verify.py` 就有满分」—— `*.db` 不入库，
  直接跑会报缺数据库并退出 2，**这是设计行为**（`README.md` §3「①② 两条缺一不可」那段）。

（整合轮 6 合入 D-1 + D-2 后实测：`warn:` 共 **12 行 / 3 类** —— `trace-tree` 下 6 行、
`business-outcome` 下 4 行、`authoritative-fact` 下 2 行。此前的 17 行 / 4 类里，
B 类「执行路径不可审计」与 C 类「事件不在任何一棵树内」已由 Y-1、Y-2 归零。
`authoritative-fact` 那 2 行都是**预期内**的：scenario-7 的 `case-s7-0001`（Y-4 带来，
主渠道那笔真收到过回执而全案落人工审批、`biz_status` 收在 `compensated`）与
`case-s7-0002`（D-1 带来，第二笔撞终态失败码后走第三出口转人工、被主管驳回）。
🔴 **这一类的行数按「场景 7 里未 settled 的退款 case 数」走** —— 每加一笔演示就 +1，
不是回归。写死一个数当判据，下一轮加演示时它自己会变成假警报源。）

---

## P11b · 四判据与晋升漏斗

**一句话主张**
所有 Agent 都回复完成 ≠ 业务成功。成没成看四判据；只有成了且证据完整的案子才进默认知识层，
没成的聚成「渠道 × 返回码 × 规则号」的失败提示。

**画面要素**
左半页一张四判据对照卡（三条路径并排，读数取自实测），右半页一个三级漏斗。

| 判据（`case_outcome`） | happy | gateway_fail | drift |
| :-- | :-- | :-- | :-- |
| `arrival` | `settled` | `unsettled` | `unknown` |
| `customer_confirmation` | `none` | `none` | `none` |
| `manual_correction` | `none` | `compensated` | `none` |
| `complaint` | `none` | `none` | `none` |
| `evidence_complete` | `true` | `false` | `false` |
| **`business_success`** | **`true`** | **`false`** | **`false`** |

漏斗三级：**跑完的案子** → **业务成功且证据完整**（`CasePromoted` → `history_case`）
→ **没成的那些**（`failure_hint` + `failure_hint_index`，只作提示、不作规划正例）。

**讲稿**
这一页是第三条反馈的正面回答。四个判据里，到账只认支付观察行 —— `arrival_basis` 指回
具体哪一条回执，指不回去的到账不叫判据、叫说法。四个值算出 `business_success`，公式钉在
核验器里，报告自己填一个 `true` 进去会被第 10 项当场抓出来。成了的进默认知识层，
没成的不进 —— 但也不丢，聚成「这个渠道撞这个返回码、在这条规则下要多做哪几步」，
下一单规划时直接吃到。

**数据从哪来**
- `evidence/case-real-01/happy/outcome.json`、`evidence/case-real-01/gateway_fail/outcome.json`、
  `evidence/case-real-01/drift/outcome.json` —— `case_outcome` 六个字段 + `arrival_basis`
- `evidence/case-real-01/happy/event-chain.json` —— `CaseOutcomeComputed` 与 `CasePromoted` 各 1 条
- `evidence/scenario-7/` 的库 —— `kb_doc` 里两条 `failure_hint`
  （`auto-failure_hint-case-s7-0001` / `auto-failure_hint-case-s7-0002`）；
  `failure_hint_index` 两行，列是 `tenant_id` / `channel_id` / `gateway_code` / `rule_no` /
  `extra_steps` / `count`
- `python3 scripts/verify.py` 的第 10 项 `case-outcome`
- 那条「都完成了却没成功」的招牌路径：`scripts/run_case.py` 的 `--stall`

**对应评委要求编号**
**12**（以到账 / 客户确认 / 人工纠错验证 DAG）、
**13**（只有证据完整且外部结果明确的案例进默认知识层）

**不许说的话**
- 🔴 不许说「`failure_hint` 会被当成规划正例」。它只用于提示「哪类渠道 × 返回码 × 政策组合
  需要额外步骤」，正例集合里没有它。
- 🔴 不许在干净 clone 上现场打开 `failure_hint` —— 它在 `evidence/scenario-7/` 的 `maos.db` 里，
  而 `*.db` 不入 git。台上要演就先跑 `python3 scripts/make_evidence.py`。
- 🔴 不许把 `drift` 那束的 `arrival=unknown` 说成「失败」。它是**问不出来**：案子停在
  `submitted`，一条付款观察行都没有，`public_status` 因此是空串 ——「问不出终态就什么都不写」
  是设计，不是漏填。
- 🔴 不许说「客户确认过了」。四条路径的 `customer_confirmation` 全是 `none`。
  房间里的 `/confirm <案号>` 命令**已经落在 router 里**（与 `/assign` `/resolve` `/complain`
  同一批，和命令行处置共用一个库），但**真人在真房间敲它、把回执采进证据束这件事还没做** ——
  等 9/18 真跑日。「命令能用」和「已经有人用过」是两句话，别说成一句。

---

## P12 · 同一个内核，两个域

**一句话主张**
软件交付域与制造售后退款域共用同一份契约、Control Plane、Worker、Gate；换域只换 Skill / ToolPort / 业务对象。

**画面要素**
`docs/domain-portability.md:16-29` 那张对照表照搬（三列：层 / 软件交付域 / 退款域 +
「是否共用」列的 ✅ ⚠️ ❌ 三态原样保留）。右下角贴 `git diff --stat` 的真实输出块。

**讲稿**
同一个内核跑两个域。事件契约一个字段没加，Task 状态机没加新状态没加新迁移，
Control Plane 和 Worker 是同一份。换域换的是 Skill、ToolPort 和业务对象 ——
它们是新增文件，不是改内核。三条机器守卫钉着这个论证，不是我们自己说的。

**可核验证据**
- `docs/domain-portability.md:16-29`（对照表，逐层标注共用 / 按域实现）
- `docs/domain-portability.md:40-44`（`git diff --stat 90251b3 df96fa8 --
  maos/contracts/ maos/runtime/` 的真实输出：`contracts/` 零改动，`runtime/` 只有 `gate.py`）
- `docs/domain-portability.md:93-97`（三条机器守卫：契约指纹锁 / 内核不识域 / 权威事实边界）

**对应评委要求编号**
—（可移植性论证支撑全篇，不单独扛十三条中的某一条）

**不许说的话**
- 🔴 **不许说「内核零改动」这个笼统说法。** 严格为零的是 `maos/contracts/`；
  `maos/core/` 是 +46/−2、`maos/runtime/` 是 +273/−7，且这两笔的出处要点明
  （整合轮 4 / X-2 的网关码四象限、第六第七道闸），**不是退款域改的**
  （`docs/domain-portability.md:50-52`、`docs/BACKLOG.md:317`）。
- 不许说「基于 AutoGen 构建」、不许说「后端已可插拔切 PolarDB」
  （`docs/submission-checklist.md §A-4`）。

---

## P13 · 数据口径与边界

**一句话主张**
合成数据、公开规范、模拟实现 —— 三者分清楚，这是最容易被问穿也最伤的一处。

**画面要素**
`docs/submission-checklist.md §A-4` 那张表照搬上版，但**只留两列**：
「只能这么说」和「不许这么说」，右列全部灰掉加删除线。这一页是全场唯一一页
主动列自己不能说什么的页，视觉上要显得坦白，不要藏在角落。

**讲稿**
三件事分清楚。政策和历史案例是按行业惯例构造的合成数据，不是某家企业的真实政策。
网关错误码与异步时序取自支付宝开放平台公开规范，逐条核对写进代码。
演示用对齐该规范的模拟实现，沙箱账号未接通。

**可核验证据**
- `README.md` **§8 的「数据口径（必须写明，不含糊）」小节**（合成数据 / 公开规范·模拟实现 /
  Matrix 真房间 三条 —— 按小节标题找，别按行号：这一节被上游几次插表整体推移过）
- `docs/submission-checklist.md §A-4`（A-4 口径一致性八行表，本页即这张表的上版；
  2026-09-01 增 MCP 行成八行）
- `maos/tools/gateway_codes.py`（错误码逐条核对后写入的落点；
  README 那一节明写「禁止凭记忆编造」）

**对应评委要求编号**
—（口径页，不扛具体要求；但它是全篇每一条断言可信的前提）

**不许说的话**
这一页就是「不许说的话」的总表，A-4 八行全部适用：
不许说「真实企业政策」/「接入了支付宝」/「全过程在 Element 里跑通」/
「后端已可插拔切 PolarDB」/「基于 AutoGen 构建」/「演示里能看到它自动换渠道重试到上限」/
「七个场景都跑成功了」/「工具层已全面 MCP 化」（5 个工具里只有 `git-mcp` 一个真走 MCP）。

---

## P14 · 复现指引

**一句话主张**
评委从零到 8/8：克隆、装无依赖、跑测试、跑场景、生成证据、核验 —— 不需要任何 API key。

**画面要素**
一列编号命令块，等宽字体，每条右侧标预期输出。最后一行标红：
「新克隆的仓库直接跑 `verify.py` 会报缺数据库并退出 2 —— 这是设计行为，先跑 ①②」。

```bash
git clone <repo> maos && cd maos
python3 -m pytest maos/tests -q     # 1069 passed
python3 run.py                      # 场景 1-7 端到端，exit=0

python3 scripts/make_evidence.py    # ① 产 evidence/scenario-1..7/ 与 -R5/（缺省一并产 R5）
python3 scripts/verify.py           # ② 十项逐条重放校验（读数以当场输出为准）
```

🔴 **是 ①② 两条，不是三条。** `python3 -m maos.kb.experiment` 曾经要单独敲才产
`scenario-R5`，Y-3 之后 ① 已缺省一并产出（`--no-r5` 可显式跳过，届时 verify 第 5、7 项按
`[SKIP]` 计，**不会**冒充 PASS）。口径出处 `README.md:96-98`、`README.md:119`。

**讲稿**
不需要任何 API key，核心零依赖，只要 Python 三点十以上。缺省走 Scripted 模式，
一行网络都不走，状态迁移序列在任何机器上逐条一致。生成证据是①一条，
核验是②，两条缺一不可、顺序不能换。全新克隆到 8/8，实测五秒四。

**可核验证据**
- `README.md:161-177`（§4 5 分钟快速开始：pytest + run.py + 证据链两条 + `exit=0`）
- `README.md:96-98`（①② 两条命令原文）
- `README.md:119`（**①② 两条缺一不可，顺序不能换**；直接跑 ② 会报缺数据库并退出 2）
- 编排侧整合轮 10 实跑：`python3 -m pytest maos/tests -q` → **802 passed**；
  `python3 run.py` → **exit=0**，跑完 `git status --porcelain` 仍 0 行；
  全新克隆 + 无任何 API key，`clone → ① → ②` **5.4 秒**到 `RESULT: 7/7 PASS`
  （**这是整合轮 10 的读数，那时核验器是 7 项**；第 8 项 `cost-attribution` 落地后，
  2026-08-31 在 `784aad7` 上主机与容器各跑一次都是 `RESULT: 8/8 PASS`，
  台上引用请说 **8/8**，5.4 秒那个耗时本轮没有重新掐表）

**对应评委要求编号**
—（复现指引页，不单独扛要求；它是 P11 那条命令能被评委真的跑起来的前提）

**不许说的话**
- 不许说「一条命令就能从零跑到 8/8」。当前是两条，且顺序不能换
  （`README.md` §3「①② 两条缺一不可，顺序不能换」那段）。
- 不许说「七个场景都跑成功了」（`docs/submission-checklist.md §A-4`）。

---

## 表 A · 评委要求 → 页

十三条出自 **`README.md` §8「与提案 / 比赛要求的映射」那张表**（**以此为准**；
这里不写死行号 —— README 每改一次都会漂，照行号翻只会翻到别的段）。
`docs/EXECUTION.md` 附 C 是 v4 手册原文，条数一致（13 条），
但里面写的是 `scenario-R1/R2`、`Phase 5`、「退款域 6 Skill」这类**已改名的旧编号**，
只用来确认「一条不漏」，**不要照抄其落点**。

> 🔴 **本表末列一律不写核验读数。** `verify.py` 各项的分子分母随证据束增减而变，
> 写死过一次就再没刷对过。要现行期望值只有两个出处：`docs/expected-metrics.json`
> （唯一真源）与**当场跑出来的 `RESULT` 行**。下表末列因此只给「打开哪个文件、
> 数哪个字段」。

| # | 评委要求（README §8 原文） | 主页 | 辅页 | 该页给出的证据 |
| :-- | :-- | :-- | :-- | :-- |
| 1 | 用一条脱敏真实退款需求完成可执行纵向切片 | **P3** | P10 | 单案例束 `evidence/case-real-01/`（`happy` / `drift` / `gateway_fail` / `reject` 四条路径，总账在 `INDEX.json`），一条 `python3 scripts/make_case_bundle.py --all-paths` 重产 |
| 2 | AgentTeams 事件链 | **P6** | P6b | `docs/agentteams-mapping.md` 五项映射（每项带落点）＋ `case-real-01/happy/event-chain.json` |
| 3 | 关键 Skill 的真实调用 | **P7** | P3 | `case-real-01/happy/skills.json` 的 `contract_skills` —— **跨轨契约钉的是 8 个退款 Skill**，顺利路径 `present`／`total` 两个字段当场读；`docs/skill-catalog.md` 是注册表全量（远不止 8 个），两个数别混 |
| 4 | 返工 / HITL Trace | **P5** | P10 | `case-real-01/gateway_fail/hitl-trace.json` 的 `kind=rework`（`transition` 字面值 `AWAITING_REVIEW->REWORK [gate_rework]`）＋ 同束 `BLOCKED → FAILED` 轨迹 |
| 5 | Evidence Bundle | **P11** | P14 | `scripts/verify.py::CHECKS` → 当场跑出的 `RESULT` 行；期望值见 `docs/expected-metrics.json` |
| 6 | 业务对象关联到同一案例 | **P11** | P3 | verify 的 `business-ref` 项（分子分母当场读）＋ `case-real-01/happy/business-objects.json` 的 `resolved` / `dangling` |
| 7 | 外部系统保留权威事实，区分已提出 / 处理中 / 已到账 | **P9** | P10 | `maos/domain/refund/guard.py` 的 settled guard ＋ `maos/domain/refund/projection.py` 的对外三态 |
| 8 | RAG 面向 workflow 规划 | **P8a** | P8b | `maos/kb/retriever.py` 两阶段检索 + `evidence/scenario-R5/dag-diff.json` |
| 9 | 先按租户/业务/地区/渠道/商品/政策/版本过滤，再组合规则编号、错误码、全文、语义 | **P8a** | — | `maos/kb/retriever.py` 的阶段一过滤顺序（与评委原话逐字一致） |
| 10 | 减少遗漏财务复核、错误套用政策、无限重试 | **P5** | P8b | 第六道闸 `maos/runtime/gate.py` + 政策版本锁定 + `MAOS_MAX_REPLAN`；「无限重试」的实证是 `gateway_fail` 那束**只重发一次就转人工** |
| 11 | 历史流程不能替代当前订单事实和人工授权 | **P8b** | P9 | `maos/kb/guardrails.py` 三条护栏 + `check_all` |
| 12 | 以退款到账 / 客户确认 / 人工纠错验证 DAG | **P10** | P11 | `case-real-01/*/outcome.json` 的 `case_outcome` 四判据 ＋ verify 的 `case-outcome` 项（当场读） |
| 13 | 只有证据完整且外部结果明确的案例进默认知识层 | **P8b** | P11 | 晋升规则 `promote_history_case` ＋ `evidence/case-real-01/happy/event-chain.json` 里的 `CasePromoted` ＋ verify 的 `history-case` 项（当场读） |

**十三条零空行。** 每一条至少命中一个页锚，且该页在自己的「可核验证据」小节里
给出了对应的 `文件:行号` 或可跑命令。

---

## 表 B · 页 → 评委要求

| 页锚 | 页名 | 扛哪几条 | 说明 |
| :-- | :-- | :-- | :-- |
| P1 | 封面 · 一句话主张 | — | 封面页。只出主张与那条 `verify.py` 命令，不承载论证 |
| P2 | 评委三段反馈，正面接住 | —（扛**三段反馈诊断**） | 十三条之外的三条诊断，落点见本页表：P11 / P3+P10 / P10 |
| P3 | 从一条退款说起 | 1、3、6 | 业务纵切正例（场景 6），把「现实业务锚点」立住 |
| P4 | 架构一眼 | — | 地基页。为 P5–P9 提供分块语汇，本身不扛要求 |
| P5 | 状态机与七道闸 | 4、10 | 唯一状态迁移出口 + 七道闸；第六道闸即「不漏财务复核」 |
| P6 | AgentTeams 事件链 | 2 | 五项映射 + 房间实拍（`evidence/room/`）；自曝的对象已从「真房间未接通」换成**演示当天的 venv 前置**与 `04` 那张图的边界 |
| P7 | Skill / ToolPort 九要素契约 | 3 | 契约面；与 P3 一起构成「关键 Skill 真调」的完整证据 |
| P8a | RAG（一）两阶段检索 | 8、9 | 机制侧。要求 9 的字段顺序在代码里逐字对得上 |
| P8b | RAG（二）改变了计划，且被护栏挡住 | 8、11、13 | 效果侧 + 护栏 + 晋升规则 |
| P9 | 权威事实边界 | 7 | settled guard；核验器抓到的那次绕过是本页最强证据 |
| P10 | 失败路径纵切（场景 7） | 1、12 | 全篇主线页。三段反馈第三条落在这里 |
| P11 | 一条命令核验 | 5、6、12 | 三段反馈第一条落在这里 |
| P12 | 同一个内核，两个域 | — | 可移植性论证支撑全篇；不对应十三条中的某一条 |
| P13 | 数据口径与边界 | — | 口径页。不扛要求，但它是全篇每一条断言可信的前提 |
| P14 | 复现指引 | — | 让 P11 那条命令能被评委真的跑起来 |

**P1 / P4 / P12 / P13 / P14 五页写「—」的理由**已逐页在上表「说明」列写明：
封面、地基、支撑论证、口径、复现指引 —— 都不是十三条中某一条的落点，
但删掉任何一页都会让其余各页的断言失去前提。

---

## 整合轮 5 收口台账（2026-08-29）

Y-1 / Y-2 / Y-3 已并入，下面四条**已按实跑回填**：

| # | 页锚 | 原来写的 | 现在 | 依据 |
| :-- | :-- | :-- | :-- | :-- |
| 1 | **P11** | 「会附若干 `warn:` 行」，条数未定 | **10 行 / 2 类**（`trace-tree` 6、`business-outcome` 4）；旧的 17 行 / 4 类里 B、C 两类归零 | Y-1 + Y-2 |
| 2 | **P8** | 场景 6 `candidate_count=0`，只证明到链路通 | 场景 6 实测 `candidate_count=3` / `hit_count=3`，证明到「链路通 + 确实召回到东西」；「召回准」仍只由 R5 对照实验扛 | Y-2 |
| 3 | **P8a / P8b** | 未提 `plan_id` 归属 | Y-2 修好归属，场景 6 的 `KbRetrieved` 已挂到实 plan 上（此前 `plan_id=''`），`kb-hits.json` 可读性变好 —— **若 P8b 用到该文件的截图，录制前重截一张** | Y-2 |
| 4 | **P11 / P14** | 「①②③ 三条命令缺一不可」 | 收敛成 **①② 两条**；P11 第三条禁语与 P14 讲稿、证据锚、禁语全部改写。全新克隆实测 **5.4 秒**到 7/7 | Y-3 |

**数字口径**：本文件所有数字（935 passed / 7 项 / 35 / 7 / 86 / 29 / 10 / 1 / +62−4 / +273−7）
都来自**整合轮 13 合入 T21…T26 六轨后**的真实输出，不是基线 `42822fc` 的旧值。
pytest 条数由 802 涨到 860（整合轮 11：T7 +15 / T11 +15 / T12 +17 / T13 +11，加法自洽；T8/T9/T14 不改源码，0 条），
再由 860 涨到 **903**（整合轮 12：T15 +5 / T16 +10 / T17 +23 / T20 +5，加法自洽；T19 不改源码 0 条；
T18 的 7 条是 PG live 测试，**无库时 skip 不计入 passed**，所以 `skipped` 同步由 22 涨到 29 ——
配了 `MAOS_PG_DSN` 时全量是 `932 passed, 0 skipped`）；
再由 903 涨到 **935**（整合轮 13：T24 +7 / T25 +13 / T26 +12，加法自洽；T21/T22/T23 不改测试 0 条；
`skipped` 仍是 29，配了 `MAOS_PG_DSN` 时全量是 `964 passed, 0 skipped` —— 本轮实跑验过，
见 `docs/BACKLOG.md` 的 `## integrate-round-13`）；
verify 七项里**六项一个没变**（86 / 35 / 3 / 7 / 10 / 1），只有 `trace-tree` 由 19/19 涨到
**29/29** —— 那是判据**加强**了（T12 给三条旁路补审计链，同批加了 `_check_seeded_provenance`），
不是分母放宽。warn 由 12 行 3 类收到 **1 行 1 类**。
⚠️ 末尾两组 diff 统计（`+62−4` / `+273−7`）本轮**没有重算** —— D-2 给 `maos/runtime/gate.py`
加了约 +200 行，这两个数多半已偏小。已记进 `docs/BACKLOG.md` 的 `## integrate-round-6`。

---

## 补合 Y-4 后的第二批回填（2026-08-29）

Y-4 已并入（`783d9dd`），下面三条**已按实跑回填**：

| # | 页锚 | 原来写的 | 现在 | 依据 |
| :-- | :-- | :-- | :-- | :-- |
| 1 | **P5** | 「机制已落地并有 19 条测试守着，但演示里没有场景走这条路」 | 改口为「演到了，但只 replan **1 次**」；禁语从「演不出来」翻成「**别说成重试到上限**」 | Y-4 |
| 2 | **P10** | 「场景 7 走的是 `effect_risk=H` 那条 HITL 入口，不是换渠道 replan」 | 屏幕上真有换渠道那一段（原话与状态轨迹已贴进页内）；最终仍落 HITL 驳回 | Y-4 |
| 3 | **P11** | warn 10 行 / 2 类 | **11 行 / 3 类**，新增的 1 行是 scenario-7「有回执但 `biz_status` 不是 settled」——**预期内**，正是这一页要讲的东西 | Y-4 |
| 5 | **P11** | warn 11 行 / 3 类；七行读数 `81/81` `33/33` `18/18` `9/9` | **12 行 / 3 类**（`authoritative-fact` 由 1 行变 2 行，新增 `case-s7-0002`）；读数 **`86/86` `35/35` `19/19` `10/10`** | 整合轮 6 合 D-1 + D-2 |

🔴 **台上最容易讲错的一句已经反向了**：以前的风险是「把没做到的说成做到」，
现在反过来 —— 换渠道**确实演到了**，风险变成「把 1 次 replan 说成重试到上限」。
`MAOS_MAX_REPLAN` 默认 2，这一镜没打满。P5 与 P10 的禁语都已按这个方向改写。

---

## 待整合轮 6 回填

Y 轮四轨与 Z 轮五轨全部合入，本文件的断言**本轮已全部对齐实测**。剩余只有人类那一项：

| # | 位置 | 等什么 |
| :-- | :-- | :-- |
| 1 | 表 A 的「四维」列 | 官方评审四维口径，见 [`docs/open-questions.md`](open-questions.md) **OQ-1** |

**回填时的纪律**：每一条都要**先实跑拿到新输出**再改文案，只认实跑，不认推断。

---

## T 轮渲染台账（T5 轨 · 2026-08-29 · 基线 `27c9e18`）

本轮把这份大纲**渲染成了可交的演示稿**，不是重写内容：

| 产物 | 说明 |
| :-- | :-- |
| `artifacts/maos-proposal.html` | **正本**。自包含单文件，15 页，16:9，无任何外链 |
| `artifacts/maos-proposal.pdf` | **提交件**。无头 Chrome 导出，15 页，`/MediaBox [0 0 960 540]` |
| `artifacts/README.md` | 重导 PDF 的两条路子、截图 slot 回填步骤、改稿三条硬约束 |

页锚 P1–P14（P8 拆 P8a/P8b）**一一对应，一页不多一页不少**，编号与页名未改。

### 表 C · 评审四维 → 页承接

口径来自 T 轮派单 §0.2（人类转述的复赛规则原文），即 OQ-1 的答复。
演示稿每一页的页眉右上角都印着它承接的维度徽章，评委翻到哪页就看得见。

| 评审维度 | 权重 | 承接页 | 页数 |
| :-- | :-- | :-- | :-- |
| 场景价值与复用性 | 20% | P3、P10、P12 | 3 |
| 多 Agent 协同 | 25% | P5、P6、P10 | 3 |
| Skill 工程体系 | 20% | P7、P8a、P8b | 3 |
| **工程落地与安全审计** | **30%** | **P5、P9、P11、P13、P14** | **5** |
| 开源贡献 | 5% | P14 | 1 |

**30% 那一维承接 5 页 > 25% 那一维的 3 页**，权重最高的一维页数最多，符合编排侧的验收判据。
不扛维度的四页（P1 封面、P2 三段反馈、P4 架构地基、P8b 已计入 Skill 维）各自的理由见表 B。

### 本轮改了大纲哪几处（全部属 §0.1 情形②：读数过期）

大纲基线是 `147df03`，主干已到 `27c9e18`，中间经 D 轮与 H 轮八轨。
**12 处代码/文档锚点行号漂移**，逐条当场打开核对后改成实测值：

| # | 锚点 | 原写 | 实测 | 那一行现在是什么 |
| :-- | :-- | :-- | :-- | :-- |
| 1 | `maos/core/control_plane.py` | `:119-123` | **`:172-176`** | 「唯一的状态迁移出口」+ `_transit` + `assert_transition` |
| 2 | `maos/runtime/gate.py` | `:164-170` | **`:254-260`** | 七道闸元组（`schema`…`GATEWAY_GATE`） |
| 3 | `maos/runtime/gate.py` | `:169` | **`:259`** | 第六道闸 `finance` |
| 4 | `maos/domain/refund/guard.py` | `:31` | **`:33`** | `AUTHORITATIVE_STATES = frozenset({"settled"})`（`:31` 是它上面的注释行） |
| 5 | `maos/domain/refund/guard.py` | `:178-187` | **`:307-315`** | 「③ 权威终态必须有回执」+ `_log_violation` + `raise` |
| 6 | `maos/flows/scenario_7.py` | `:383-386` | **`:692-696`** | 「本场景存在的理由」两条断言 |
| 7 | `maos/kb/experiment.py` | `:687` | **`:788`** | 写出 `dag-diff.json` |
| 8 | `maos/kb/guardrails.py` | `:149-156` | **`:149`** | `check_all` 定义行 |
| 9 | `scripts/verify.py` | `:514-522` | **`:859-866`** | `CHECKS` 七项函数清单 |
| 10 | `docs/submission-checklist.md` | `:88-93` | **`:183-190`** | 评委三段反馈的诊断与回应落点 |
| 11 | `docs/submission-checklist.md` | `:55-63` | **`:119-137`** | A-4 口径一致性七行表 |
| 12 | README 共 13 处 | 见上文 | 已逐条改 | §1/§3/§4/§6/§7/§8 各段 |

**另一处口径改判（同属情形②）**：P14 的复现命令由 **三条收敛成两条** ——
`make_evidence.py` 缺省一并产 `scenario-R5`，`python3 -m maos.kb.experiment` 不再需要单独敲
（出处 `README.md:96-98`、`README.md:119`）。这一条最要害：**漏掉 ① 直接跑 ② 会报缺数据库并退出 2**，
是评委最容易踩的坑，演示稿 P11 与 P14 两页都写了。

**未改**：情形①（仓库里不存在）与情形③（塞不下要拆页）本轮**一处都没有触发** ——
大纲写的每一页在仓库里都找得到落点，15 页也都装得下。

### 活数字复跑（不抄大纲，逐条自己跑）

| 数字 | 跑的命令 | 实测 | 大纲原值 |
| :-- | :-- | :-- | :-- |
| 测试条数 | `python3 -m pytest maos/tests -q` | **1069 passed**，exit=0 | 903 ⚠️ 已刷为 1069（整合轮 13 六轨 +32 到 935，整合轮 14 T27–T30 再 +134）|
| 证据束 | `ls evidence/` | **8 束**（`scenario-1..7` + `-R5`）／**50** 个证据文件 | 8 ✅ 一致 |
| Skill 数 | `docs/skill-catalog.md:7` | **13** | 13 ✅ 一致 |
| ToolPort 数 | `docs/toolport-contract.md:7` | **4** 个已实现 | 4 ✅ 一致 |
| Agent 数 | `docs/agent-identity.md:9` | **10** Identity，**9** 个可派单 | ✅ 一致 |
| 场景 7 汇总 | `python3 run.py --scenario 7` | `compensated` ／ settled **0 条** ／ **1 次** replan | ✅ 逐字一致 |
| 七道闸 | `grep -n '_gate_' maos/runtime/gate.py` | 判据表 **7 个条目**（`:254-260`；`def _gate_` 有 9 个是 D-2 拆函数所致） | ✅ 仍是七道 |
| `contracts/` 跨域 | `git diff --shortstat 90251b3 27c9e18 -- maos/contracts/` | **空输出（零改动）** | ✅ 且比大纲更强：到当前 HEAD 仍为零 |
| 区间 A 内核代价 | `git diff --shortstat 90251b3 4a70cb0 -- maos/<面>` | `contracts/` 零、`core/` **零**、`runtime/` 只有 `gate.py` **+126/−4** | 大纲未分区间，本轮按 `docs/domain-portability.md:60-80` 的两区间口径写 |
| 全区间内核增量 | `git diff --shortstat 90251b3 27c9e18 -- maos/core/ maos/runtime/` | `core/` **+194/−10**、`runtime/` **+497/−9** | ⚠️ 大纲写的 `+46/−2` / `+273/−7` **已偏小**，大纲自己预警过；演示稿 P12 用实测值 |
| 按域实现四面 | `git diff --shortstat 90251b3 4a70cb0 -- maos/{agents,skills,tools,domain}/` | **26 files, +3548/−52** | 新增，演示稿 P12 用 |

✅ **整合轮 11 已当场复跑，读数确实变了，P11 与本文件都已按实测改**（这正是上一版
留的那句「整合轮如果重跑出不同读数，以那次为准改 P11」所指的情形）：
七行读数（整合轮 11 那一刻）为 `86/86` `35/35` `3/3` **`29/29`** `7/7` `10/10` `1/1`，
warn 由 12 行 3 类收到 **1 行 1 类**。
⚠️ **这是七项时代的读数**：第 8 项 `cost-attribution 39/39` 于 2026-08-30 落地，
当前是**八行**，逐行以本文件上方 P11 那一段（基线 `dd3edff`）为准。`trace-tree` 涨的是**判据数**不是分母放宽 —— T12 给三条绕开
`on_task_result` 的旁路补上 `ArtifactSeeded`，同批加了 `_check_seeded_provenance`
（自称有来源却指不出来即判负）。上一版说不能复跑的理由（T5 轨禁跑 `make_evidence.py`）
在整合轮不成立：证据束本来就该在这一轮全量重跑。

### 演示稿的三条自我约束（写进了 `artifacts/README.md`）

1. **自包含**：`grep -c "cdn|unpkg|cdnjs|@import url("` = **0**，全文**一个 `http(s)://` 都没有**。
2. **一页就是一页**：`.body` 是 `overflow:hidden`，内容超高会被**静默裁掉**。
   本轮实测 15 页 `bodyOverflow` 全为 **0**；等价判据是把 `overflow` 改成 `visible` 重导 PDF，
   **页数仍是 15**（若有页溢出会变 16）。P7/P12/P13 三页带实测收敛出的 `zoom`（`.94`/`.93`/`.96`）。
3. **每句断言指得出证据**：每页页脚固定一行「可核验证据」，给 `文件:行号` 或可当场跑的命令。

### 留给整合轮的一件事

**P6 的房间实拍 slot**（`<!-- SLOT: room-screenshot -->`）。

**当时（T5 交稿）的处置，作为历史说明保留**：那一刻 T4 轨仍在采真房间证据，
所以 P6 正文只写**代码事实**（镜像层、降级等价性、审批命令解析、越权落库），
并主动自曝「真房间未接通」，没有写「已在真房间跑通」。
**在当时那是对的** —— 手上确实没有房间证据，写「已跑通」就是编。

**现在（T53，2026-09-01）这个 slot 可以填了。** T4 轨已交付：`evidence/room/` 下
5 张 Element 截图 + `README.md` + 861 行逐字副本 `transcript.md`，出处头指向 `27c9e18`。

- **填哪一张：`01-approval-card.png`。** 理由：P6 扛的是「AgentTeams 事件链 + HITL」，
  这张图一帧之内同时给出**人话摘要**、**展开的 Envelope JSON**（可见 `effect_risk: "H"`、
  `state: "BLOCKED"`、`title: "变更生产环境配置"`）和**逐字列出的 `/approve` `/reject`
  两条可用指令** —— 评委不用听讲解就能看懂「这里是一个停下来等人的点，人在房间里怎么放行」。
  另外四张各缺一半：`02` 只有迁移流水、`03`／`04` 是结果回执（且 `04` 证明不了补偿），
  `05` 讲的是越权边界，不是事件链主线。
- **配图那行小字必须带前置**，不能只放图：「本机自建 Synapse 非加密房，
  入口 `~/.maos-matrix/venv/bin/python -m hiclaw.room_demo`；系统 `python3` 没装
  matrix-nio，用它起房间会静默降级 log-only。」
- 🔴 **别给这张图配「补偿在房间里可见」之类的话** —— 那是 `04` 的边界问题，见「不许说的话」。

回填步骤（含 base64 内联，不许外链图片）写在 `artifacts/README.md` 第 2 节。
**本轨不改 `artifacts/**`**（不在可改面内），实际内联由收尾轨执行。

---

## MCP 落地回填台账（2026-09-01）

`git-mcp` ToolPort 落地（`maos/tools/mcp/`，entry 真走 MCP stdio）当天，本文件同步四处：

| 改动 | 位置 | 说明 |
| :-- | :-- | :-- |
| P7 横条 4 → 5 | P7 画面要素 | 与 HTML 正本、`gen_docs` 扫描结果同步；讲稿补一句「换传输层只换 entry」 |
| P7 版式禁区 | P7 画面要素 | ToolPort 表 `entry` 行**只加底色不加字** —— 渲染实测那格每行 15 字，加短语即折行、把「投放即注册」卡顶出页外 30px（`.slide` 是 `overflow:hidden`，溢出不分页、直接裁） |
| P13 不许说 +1 | P13 不许说的话 | 「工具层已全面 MCP 化」入列；A-4 表七行 → 八行 |
| README 行号刷新 | 文头、P13、P14 证据 | §7 表增 MCP 行、§8 表后增工具链段 → 后续行号整体下移；顺带修正旧值 `280-294`（README 多轮膨胀后早已指偏，与本轮无关）→ `295-309` |

依据：`docs/DECISIONS.md` 的 `## task-mcp-2026-09-01`、`docs/BACKLOG.md` 的 `## task-mcp`。
