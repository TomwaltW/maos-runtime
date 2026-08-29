# 方案 PPT 逐页大纲

> 这份文件是 PPT 的**文字骨架**，不是 PPT 本身。人类照它去做版。
>
> **基线**：`42822fc`。全文每一条「可核验证据」都在该基线上逐条打开核对过，
> 核不到的断言已就地改弱 —— 见文末「核对台账」。
>
> ⚠️ **「评审四维」的官方名称与权重，仓库里没有，官方通知也还没到。**
> 本文件一律不猜、不按惯例推测、不写预估权重，凡涉及处写
> **（四维口径待确认，见 `docs/open-questions.md` OQ-1）**。对照表改按
> **十三条评委要求**组织（出处 `README.md:249-261`）。这与
> `docs/DECISIONS.md:491`、`docs/BACKLOG.md:306` 已定的口径一致。
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
七项证据逐项重放，全绿才是零。

**可核验证据**
`README.md:1-11`（标题、副标题与那条 `verify.py` 命令逐字对应本页文案）

**对应评委要求编号**
—（封面页，不单独扛要求）

**不许说的话**
- 不许说「基于 AutoGen 构建」—— 只能说「AutoGen 是可插拔内核之一，未在复赛演示中启用」
  （`docs/submission-checklist.md:61`、`docs/agentteams-mapping.md:69-79`）。

---

## P2 · 评委三段反馈，正面接住

**一句话主张**
三条诊断我们没有绕开，每一条都指向一页具体的、可核验的落点。

**画面要素**
三行表。左列是评委原话诊断，中列是落在哪一页，右列是那一页给得出的证据。
右列全部是等宽字体的命令或路径，不写形容词。

| 评委诊断 | 落在 | 证据 |
| :-- | :-- | :-- |
| 没有可执行制品和运行证据 | **P11** | `python3 scripts/verify.py` → 7/7 PASS |
| 现实业务锚点不足 | **P3 + P10** | 退款域纵切（场景 6 / 7），不是软件域自证 demo |
| 「所有 Agent 都回复完成」≠ 业务成功 | **P10** | 场景 7：`biz_status=compensated`，`settled` 观察 **0 条** |

**讲稿**
上一轮的三条反馈，我们没有换个说法绕过去。第一条要可执行制品，我们给一条命令；
第二条要现实业务锚点，我们把整个演示换成制造企业售后退款；第三条最要害，
它正是我们这一版的主线，第十页专讲。

**可核验证据**
`docs/submission-checklist.md:88-93`（三条诊断的原文与要求指向的落点）

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
`README.md:20-30` 那张八行表照搬上版：左列「谁」，中列「干了什么」，右列「留下了什么」。
右列全部是落库的对象名（`refund_case` / `plan` + 5 个 `task` / `finance_entry` /
`payment_observation`），不是描述性文字。底部一行小字标数据口径。

**讲稿**
客户报修一批轴承，要求退款六千八，诉求同时来自工单、客服聊天和照片三处。
受理 Agent 去重聚合成一个 case，Manager 规划出 DAG —— 这个 Manager
和软件交付域是同一个，零改动。往下每一步都往库里落一个真实业务对象。

**可核验证据**
- `README.md:15-43`（本页叙事与表格的原文）
- 一条可跑的命令：`python3 run.py --scenario 6`

**对应评委要求编号**
**1**（脱敏真实退款需求的可执行纵向切片）、**3**（关键 Skill 的真实调用）、
**6**（业务对象关联到同一案例）

**不许说的话**
- 不许说「真实企业政策」—— 只能说「按行业惯例构造的合成数据」（`docs/submission-checklist.md:57`）。
- 不许说「接入了支付宝」—— 只能说「错误码与异步时序对齐支付宝开放平台公开规范；
  演示用模拟实现」（`docs/submission-checklist.md:58`）。

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
`docs/architecture.md:12-56`（分层图源码；`README.md:57-77` 是同一张图的精简版，
两者的分块与命名一致）

**对应评委要求编号**
—（架构页是后续各页的地基，不单独扛要求）

**不许说的话**
- 不许说「后端已可插拔切 PolarDB」—— 只能说「StorePort 有地基、未接线；
  PG 后端是空壳且拒绝回落」（`docs/submission-checklist.md:60`）。
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
- `maos/core/control_plane.py:119-123`（注释「唯一的状态迁移出口」+ `_transit`，
  首行即 `assert_transition`）
- `maos/runtime/gate.py:164-170`（七道闸的元组，顺序即执行顺序）

**对应评委要求编号**
**4**（返工 / HITL Trace）、**10**（减少遗漏财务复核、错误套用政策、无限重试）

**不许说的话**
- 🔴 不许说「演示里能看到它自动换渠道重试到上限」。只能说「机制已落地并有 19 条
  测试守着，但演示里没有场景走这条路」（`docs/submission-checklist.md:62`、
  `docs/BACKLOG.md:262`）。

> ⚠️ Y 轮易变：上面这条「演示里没有场景走这条路」—— 见文末回填清单。

---

## P6 · AgentTeams 事件链

**一句话主张**
AgentTeams 的五个概念逐项落到代码位置；事件链的权威记录不在房间里，在 `event_log` 表。

**画面要素**
`docs/agentteams-mapping.md:18-24` 那张五行表照搬，保留「代码位置」与「状态」两列
（状态列的 ✅ / 「代码就绪，真房间未接通」原样保留，不许统一成 ✅）。
右下角一个小方块写「当前真实状态」三行。

**讲稿**
Team 对应一个 Matrix 房间，Member 对应可插拔的 Agent 池，事件链走镜像发布，
HITL 是房间里的斜杠命令，第五项是可观测。第五项要单说：事件链的权威记录
不在房间里，在 event_log 表里，房间挂了不影响重放。

**可核验证据**
- `docs/agentteams-mapping.md:18-24`（五项映射表，每项都带 `文件:行号`）
- `docs/agentteams-mapping.md:52-63`（「当前真实状态（不吹）」小节）

**对应评委要求编号**
**2**（AgentTeams 事件链）

**不许说的话**
- 🔴 不许说「全过程在 Element 里跑通」。只能说「镜像层已实现，降级路径实测等价，
  真房间待接通」（`docs/submission-checklist.md:59`、`docs/agentteams-mapping.md:63`）。
- 这一页必须自己把「真房间未接通」说出来，不要等评委问。

---

## P7 · Skill / ToolPort 九要素契约

**一句话主张**
Skill 与 ToolPort 都是九要素声明，投放一个文件即注册；两份目录文档由代码生成，不是手写的。

**画面要素**
左右分栏：左栏 `SkillContract` 九要素，右栏 `ToolPort` 九要素，字段名用等宽字体。
底部一行横条：「13 个 skill / 4 个已实现工具 / 10 个 Agent Identity —— 三份文档全部代码生成」，
配一条 `python3 scripts/gen_docs.py --check` 的终端输出截图。

**讲稿**
Skill 和 ToolPort 都是九要素 dataclass，失败形态和安全边界是必填项 —— 失败被吞掉
就等于没有边界。注册靠类装饰器，模块被 import 就进注册表，投放即注册。
这三份目录文档是从运行时代码生成的，`--check` 不一致就非零退出。

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
  对齐公开规范的模拟实现（`docs/submission-checklist.md:58`）。

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
- 不许说「后端已可插拔切 PolarDB」（`docs/submission-checklist.md:60`）。
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
  的依赖侧半条，`check_all` 在 `:149-156` 一次跑完）
- `maos/kb/experiment.py:687`（写出 `dag-diff.json`）
- `README.md:217`（证据索引里 `dag-diff.json` 的定位：「对照实验的判定面」）
- 一条可跑的命令：`python3 -m maos.kb.experiment`

**对应评委要求编号**
**8**（RAG 面向 workflow 规划 —— 效果侧）、**11**（历史流程不能替代当前订单事实和人工授权）、
**13**（只有证据完整且外部结果明确的案例进默认知识层）

**不许说的话**
- 🔴 不许说「场景 6 演示了 RAG 召回得很准」。场景 6 当前 `candidate_count=0`，
  「RAG 接上了」在场景 6 上只证明到**链路通**，证明不到**召回准**；后者的证据
  只在 `evidence/scenario-R5/` 的对照实验里（`docs/BACKLOG.md:248`）。

> ⚠️ Y 轮易变：上面这条「场景 6 当前 `candidate_count=0`」—— 见文末回填清单。

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
- `maos/domain/refund/guard.py:31`（`AUTHORITATIVE_STATES = frozenset({"settled"})`）
- `maos/domain/refund/guard.py:178-187`（「③ 权威终态必须有回执」+ 缺字段时
  `_log_violation` 落库并 `raise AuthoritativeFactViolation`）
- `docs/authoritative-facts.md:113-145`（**实况**：核验器第 3 项真的抓到过一次绕过，
  含根因、为什么两轨各自全绿、以及那一行修法）

**对应评委要求编号**
**7**（外部系统保留权威事实，区分已提出 / 处理中 / 已到账）

**不许说的话**
- 不许说「接入了支付宝」（`docs/submission-checklist.md:58`）。
- 不许把「抓到过一次绕过」讲成「我们从没出过错」。这一页最值钱的恰恰是
  **核验器自己抓到了**，讲的时候要把它当正面证据讲，不要淡化。

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
- `maos/flows/scenario_7.py:383-386`（本场景存在的理由，两条断言：
  `biz_status == "compensated"`、`settled_rows == 0`）
- `README.md:43`（「四个 Agent 全部回复「完成」，而这一单没有成功，系统如实这么记了。」）

编排侧在基线 `42822fc` 上实跑到的原文（可直接进讲稿，逐字未改）：

```
  业务状态  : compensated（全程没有经过 settled）
  settled 观察: 0 条 —— 没问出终态就一条都不该有
  补偿记录  : 2 行 ['manual_ticket', 'refund_request_revoked']
  补偿事件  : 1 条 CompensationExecuted
  Plan 终态 : FAILED（主管驳回，业务确实没成功）
```

**对应评委要求编号**
**1**（可执行纵向切片 —— 失败路径侧）、**12**（以退款到账 / 客户确认 / 人工纠错验证 DAG）

**不许说的话**
- 🔴 不许说「七个场景都跑成功了」。场景 7 的 Plan 终态是 FAILED，**那正是它要演的**
  （`docs/submission-checklist.md:63`）。
- 🔴 不许说「演示里能看到它自动换渠道重试到上限」（`docs/submission-checklist.md:62`）。
  场景 7 走的是 `effect_risk=H` 那条 HITL 入口，不是换渠道 replan 那条。

> ⚠️ Y 轮易变：上面这条「场景 7 演不出换渠道重试」—— 见文末回填清单。

---

## P11 · 一条命令核验

**一句话主张**
检索不准顶多说效果一般；无法核验就是零分 —— 所以七项证据每一项都能被外人独立跑一遍。

**画面要素**
整屏终端输出，七行 PASS + 一行 RESULT，等宽大字号。左侧配一张七行小表：
每项失败**意味着什么**（`README.md:126-136` 那张表）。

**讲稿**
这一节是给评委的。三条命令：生成证据束、跑 RAG 对照实验、逐项重放校验。
七项分别验证据没被篡改、业务锚点不悬空、权威边界没被绕过、事件链完整、
RAG 命中是真的、Agent 完成没被当成业务成功、知识层没被污染。全绿退出 0。

**可核验证据**
- `scripts/verify.py:514-522`（`CHECKS` 七项函数清单，顺序即输出顺序）
- `README.md:83-136`（§3 一条命令核验：三条命令、七行实跑输出、七项各验什么）

编排侧在基线 `42822fc` 上实跑到的七行（`docs/demo-script.md` 里那份是旧的，
`business-ref 23/23`、`kb-hit 1/1` 已过期，**不要照抄旧文档**）：

```
[PASS] hash-integrity       74/74
[PASS] business-ref         33/33
[PASS] authoritative-fact   3/3
[PASS] trace-tree           18/18
[PASS] kb-hit               4/4
[PASS] business-outcome     9/9
[PASS] history-case         1/1

RESULT: 7/7 PASS
```

**对应评委要求编号**
**5**（Evidence Bundle）、**6**（业务对象关联到同一案例 —— verify 第 2 项）、
**12**（以到账 / 客户确认 / 人工纠错验证 DAG —— verify 第 6 项）

**不许说的话**
- 不许说「七个场景都跑成功了」（`docs/submission-checklist.md:63`）。
- 不许把 `warn:` 行藏起来。`trace-tree` 与 `business-outcome` 两项会附若干 `warn:`，
  它们不改判定但是真的（`README.md:110-111`）。台上被问到要能直接答。
- 不许说「新克隆的仓库直接跑 `verify.py` 就有 7/7」—— `*.db` 不入库，
  直接跑会报缺数据库并退出 2，**这是设计行为**（`README.md:113-115`）。

> ⚠️ Y 轮易变：`warn:` 行的条数、以及「①②③ 三条命令」这个说法 —— 见文末回填清单。

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
  （`docs/submission-checklist.md:60-61`）。

---

## P13 · 数据口径与边界

**一句话主张**
合成数据、公开规范、模拟实现 —— 三者分清楚，这是最容易被问穿也最伤的一处。

**画面要素**
`docs/submission-checklist.md:55-63` 的 A-4 表照搬上版，但**只留两列**：
「只能这么说」和「不许这么说」，右列全部灰掉加删除线。这一页是全场唯一一页
主动列自己不能说什么的页，视觉上要显得坦白，不要藏在角落。

**讲稿**
三件事分清楚。政策和历史案例是按行业惯例构造的合成数据，不是某家企业的真实政策。
网关错误码与异步时序取自支付宝开放平台公开规范，逐条核对写进代码。
演示用对齐该规范的模拟实现，沙箱账号未接通。

**可核验证据**
- `README.md:263-270`（§8 数据口径小节：合成数据 / 公开规范 / 模拟实现 / Matrix 未接通 四条）
- `docs/submission-checklist.md:55-63`（A-4 口径一致性七行表，本页即这张表的上版）
- `maos/tools/gateway_codes.py`（错误码逐条核对后写入的落点，
  `README.md:266-268` 明写「禁止凭记忆编造」）

**对应评委要求编号**
—（口径页，不扛具体要求；但它是全篇每一条断言可信的前提）

**不许说的话**
这一页就是「不许说的话」的总表，A-4 七行全部适用：
不许说「真实企业政策」/「接入了支付宝」/「全过程在 Element 里跑通」/
「后端已可插拔切 PolarDB」/「基于 AutoGen 构建」/「演示里能看到它自动换渠道重试到上限」/
「七个场景都跑成功了」。

---

## P14 · 复现指引

**一句话主张**
评委从零到 7/7：克隆、装无依赖、跑测试、跑场景、生成证据、核验 —— 不需要任何 API key。

**画面要素**
一列编号命令块，等宽字体，每条右侧标预期输出。最后一行标红：
「新克隆的仓库直接跑 `verify.py` 会报缺数据库并退出 2 —— 这是设计行为，先跑 ①②」。

```bash
git clone <repo> && cd maos-runtime
python3 -m pytest maos/tests -q     # 521 passed
python3 run.py                      # 场景 1-7 端到端，exit=0

python3 scripts/make_evidence.py    # ① 产 evidence/scenario-1..7/
python3 -m maos.kb.experiment       # ② 产 evidence/scenario-R5/
python3 scripts/verify.py           # ③ 七项逐条重放校验 → 7/7 PASS
```

**讲稿**
不需要任何 API key，核心零依赖，只要 Python 三点十以上。缺省走 Scripted 模式，
一行网络都不走，状态迁移序列在任何机器上逐条一致。生成证据是①②两条，
核验是③，三条缺一不可、顺序不能换。

**可核验证据**
- `README.md:140-148`（§4 5 分钟快速开始：三条命令 + `521 passed` + `exit=0`）
- `README.md:89-92`（①②③ 三条命令原文）
- `README.md:117-121`（⚠️ 只跑 ①③ 会撞的那个坑：`scenario-R5` 归 ② 单产，
  **②不能省**，已记 `docs/BACKLOG.md ## task-W5`）
- 编排侧在基线 `42822fc` 上实跑：`python3 -m pytest maos/tests -q` → **521 passed**；
  `python3 run.py` → **exit=0**，跑完 `git status --porcelain` 仍 0 行

**对应评委要求编号**
—（复现指引页，不单独扛要求；它是 P11 那条命令能被评委真的跑起来的前提）

**不许说的话**
- 不许说「一条命令就能从零跑到 7/7」。当前是三条，且顺序不能换（`README.md:113`）。
- 不许说「七个场景都跑成功了」（`docs/submission-checklist.md:63`）。

> ⚠️ Y 轮易变：上面这条「当前是三条命令」—— 见文末回填清单。

---

# 表 A · 评委要求 → 页

十三条出自 `README.md:249-261`（§8，**以此为准**）。
`docs/EXECUTION.md:788-802` 的附 C 是 v4 手册原文，条数一致（13 条），
但里面写的是 `scenario-R1/R2`、`Phase 5`、「退款域 6 Skill」这类**已改名的旧编号**，
只用来确认「一条不漏」，**不要照抄其落点**。

| # | 评委要求（README §8 原文） | 主页 | 辅页 | 该页给出的证据 |
| :-- | :-- | :-- | :-- | :-- |
| 1 | 用一条脱敏真实退款需求完成可执行纵向切片 | **P3** | P10 | `python3 run.py --scenario 6` / `--scenario 7` |
| 2 | AgentTeams 事件链 | **P6** | — | `docs/agentteams-mapping.md:18-24` 五项映射（每项带行号） |
| 3 | 关键 Skill 的真实调用 | **P7** | P3 | `docs/skill-catalog.md:15-29`（13 skill，含退款域 7 个） |
| 4 | 返工 / HITL Trace | **P5** | P10 | `maos/runtime/gate.py:164-170` + 场景 7 的 `BLOCKED → FAILED` 轨迹 |
| 5 | Evidence Bundle | **P11** | P14 | `scripts/verify.py:514-522` → 7/7 PASS |
| 6 | 业务对象关联到同一案例 | **P11** | P3 | verify 第 2 项 `business-ref 33/33` |
| 7 | 外部系统保留权威事实，区分已提出 / 处理中 / 已到账 | **P9** | P10 | `maos/domain/refund/guard.py:31` + `:178-187` |
| 8 | RAG 面向 workflow 规划 | **P8a** | P8b | `maos/kb/retriever.py:151-163` + `evidence/scenario-R5/dag-diff.json` |
| 9 | 先按租户/业务/地区/渠道/商品/政策/版本过滤，再组合规则编号、错误码、全文、语义 | **P8a** | — | `maos/kb/retriever.py:51-55`（过滤顺序与评委原话逐字一致） |
| 10 | 减少遗漏财务复核、错误套用政策、无限重试 | **P5** | P8b | 第六道闸 `maos/runtime/gate.py:169` + 政策版本锁定 + `MAOS_MAX_REPLAN` |
| 11 | 历史流程不能替代当前订单事实和人工授权 | **P8b** | P9 | `maos/kb/guardrails.py:1-16` 三条护栏 + `:149-156` `check_all` |
| 12 | 以退款到账 / 客户确认 / 人工纠错验证 DAG | **P10** | P11 | `result.json` 的 `business_outcome` / verify 第 6 项 `9/9` |
| 13 | 只有证据完整且外部结果明确的案例进默认知识层 | **P8b** | P11 | 晋升规则 `promote_history_case` / verify 第 7 项 `1/1` |

**十三条零空行。** 每一条至少命中一个页锚，且该页在自己的「可核验证据」小节里
给出了对应的 `文件:行号` 或可跑命令。

---

# 表 B · 页 → 评委要求

| 页锚 | 页名 | 扛哪几条 | 说明 |
| :-- | :-- | :-- | :-- |
| P1 | 封面 · 一句话主张 | — | 封面页。只出主张与那条 `verify.py` 命令，不承载论证 |
| P2 | 评委三段反馈，正面接住 | —（扛**三段反馈诊断**） | 十三条之外的三条诊断，落点见本页表：P11 / P3+P10 / P10 |
| P3 | 从一条退款说起 | 1、3、6 | 业务纵切正例（场景 6），把「现实业务锚点」立住 |
| P4 | 架构一眼 | — | 地基页。为 P5–P9 提供分块语汇，本身不扛要求 |
| P5 | 状态机与七道闸 | 4、10 | 唯一状态迁移出口 + 七道闸；第六道闸即「不漏财务复核」 |
| P6 | AgentTeams 事件链 | 2 | 五项映射 + 必须自曝「真房间未接通」 |
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

# 待整合轮 5 回填

Y 轮四轨正在改代码，下面几处的说法会在整合轮 5 之后变。
**合并后逐条回来改，不要等发现台上讲错了才找。**

| # | 页锚 | 现在写的是什么 | Y 合并后应改成什么 | 依据哪一轨 |
| :-- | :-- | :-- | :-- | :-- |
| 1 | **P11** | 「`trace-tree` 与 `business-outcome` 两项会附若干 `warn:` 行」，且台上要能答得出 warn 的内容 | `test_report` 补 `sandbox_mode` 后，编排侧口径是那 4 条 warn 会自动消失。**消失后要重跑一次 `verify.py` 拿新输出**，并把 P11「不许把 warn 行藏起来」那条改成实际剩余条数；若真的归零，改成「7/7 PASS 且零 warn」 | **Y-1** |
| 2 | **P8b** | 「场景 6 当前 `candidate_count=0`，只证明到链路通、证明不到召回准」（`docs/BACKLOG.md:248`） | 语料播进场景 6 的 `seed_domain()` 后，`candidate_count` 会从 0 变成几十。届时 P8b 可以改用**场景 6 现场演召回**，不必只靠 R5 对照实验。**必须实测新数字再改口，不许推断**；一并确认 `test_kb_switch_does_not_change_the_dag` 仍绿 | **Y-2** |
| 3 | **P8a / P8b** | 未提 `plan_id` 归属问题 | Y-2 修好 `plan_id` 归属后，检索事件能挂到正确的 plan 上，`kb-hits.json` 的可读性会变；若 P8b 的画面要素用到 `kb-hits.json`，需重新截图 | **Y-2** |
| 4 | **P11 / P14** | 「①②③ 三条命令缺一不可、顺序不能换」，且 P14 标红「先跑 ①②」；P11 写「不许说一条命令就能从零跑到 7/7」 | 一条命令能复现全量证据后，P14 的命令块收敛成一条，标红那句删掉；P11 的「不许说的话」第三条整条作废。**改之前先实跑新命令确认 R5 也被产出**（当前 R5 得单独产，正是这条要修的） | **Y-3** |
| 5 | **P10** | 「场景 7 走的是 `effect_risk=H` 那条 HITL 入口，不是换渠道 replan」；「不许说演示里能看到它自动换渠道重试到上限」 | 场景 7 真能在屏幕上演换渠道重试后，P10 的画面要素要加一段换渠道的输出，讲稿相应加一句；`docs/submission-checklist.md:62` 那条 A-4 禁语**届时也要一并改口**（那是 Z-4 的面，需同步告知） | **Y-4** |
| 6 | **P5** | 「机制已落地并有 19 条测试守着，但演示里没有场景走这条路」 | 同第 5 条。Y-4 落地后这句话不再成立，改成实际演到的样子；测试条数若变，以实跑为准 | **Y-4** |

**回填时的纪律**：每一条都要**先实跑拿到新输出**再改文案。
本文件所有数字（521 passed / 7 项 / 33 / 4 / 74 / 18 / 9 / 1 / +46−2 / +273−7）
都来自基线 `42822fc` 的真实输出，回填时同样只认实跑，不认推断。

---

# 核对台账

**基线**：`42822fc`。本文件共 **63 条**去重后的 `文件:行号` 锚（正文中出现 87 次），
外加 **8 条**可跑命令。

- **`文件:行号` 实际打开核对过：63/63 条。** 核法是机器化的：正则抽出全文每一个
  `文件:行号` / `文件:起-止`，逐条打开目标文件、打印首行与末行比对内容，
  **越界 0 条、缺文件 0 条**。首轮核对抓出 **15 条**行号偏差（README 的 §3/§4/数据口径
  各段、`docs/agentteams-mapping.md` 的口径句、`docs/skill-catalog.md` 的表范围、
  `docs/BACKLOG.md` 的两条、以及 4 条尾行落在空行的松区间），已全部按实际行号回改。
- **可跑命令：8 条**。本轨实跑过 3 条 —— `python3 -m pytest maos/tests -q`（521 passed）、
  `python3 run.py`（exit=0，含场景 6 与 7）、`python3 scripts/gen_docs.py --check`（exit=0）。
  其余 5 条未跑的理由见下。
- **因核不到而改弱：1 条** —— P8b 的护栏条数。`README.md:259` 与
  `maos/kb/guardrails.py` 模块 docstring 都写「三条断言」，但 `check_all`
  实际调用 **4 个** assert 函数。未按「三条」照抄，改写成
  「三条护栏（代码里 4 个 assert 函数，`assert_no_dependency_removed` 是第 1 条
  『只增不删』的依赖侧半条）」—— 这样台上被问「到底几条」答得上。已记
  `docs/BACKLOG.md ## task-Z1`。
- **本轨未跑的 5 条命令与理由**：`scripts/verify.py` 在干净工作区上**必然报缺数据库**
  （`*.db` 不入库，设计行为，`README.md:113-115`）；要跑出 7/7 必须先跑
  `scripts/make_evidence.py` + `python3 -m maos.kb.experiment`，而那两条会把
  仓库里已入库的 `evidence/**` 改脏（编排侧实测 7 个文件变 M）。本轨是纯文档轨，
  不该动 `evidence/`，故三条都不跑。`run.py --scenario 6` / `--scenario 7` 未单跑，
  但无参 `run.py` 的缺省序列已含 1–7，两场都实跑到了。
  **本文件 P11 引的七行 verify 输出、P10 引的场景 7 五行汇总，都标明了出处**：
  前者是编排侧在同一基线上实跑的，非本轨自跑；后者本轨自跑复现，与派单逐字一致。

**待确认（本轨拒绝编造的部分）**
- 「评审四维」的官方名称与权重 → `docs/open-questions.md` **OQ-1**（Z-4 建立）。
  仓库全文 grep 确认无任何官方四维口径，与 `docs/BACKLOG.md:306`、
  `docs/DECISIONS.md:491`、`docs/submission-checklist.md:6-7` 已定的「不编」口径一致。
- Demo 视频官方规格（时长上限 / 分辨率 / 格式 / 大小 / 字幕）同属 OQ 范围，
  本文件不涉及（归 `docs/demo-script.md` 与 `docs/submission-checklist.md` C 段）。
