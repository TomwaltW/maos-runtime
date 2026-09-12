# Phase 10 —— 退款纵切深化：把评委三段建议做成可核验产物

> 状态：**草案**（2026-09-10 主会话起草；§6 五项决定由人类拍板后开工）。
> 批次号 `p10`，轨号 **T113–T121**（T112 是当前最高）。
> **Wave A 基线 = `0ad62b5`**（2026-09-10 已建 5 个 worktree、已落派单）。
> **波次调整（2026-09-10）**：T113（圆桌落库）与另一会话正在改的
> `maos/roundtable/**` / `hiclaw/**` / `maos/ingress/router.py` 直接撞车，
> 已把 **T117 提到 Wave A 顶替 T113**，T113 退到 Wave B —— 它的基线要等那轮 commit 落地。
> **波次调整二（2026-09-10 晚）**：圆桌那轮已作为 `7af9022` 落地，**T113 提前开工**、与 Wave A 五轨并行，
> worktree 钉 **7af9022**（派单 `review/paste-T113.md`，基线已实跑）；Wave B 只剩 **T114 / T119**，
> 基线 = Wave A 整合后主干，派单 `review/paste-T114.md` / `review/paste-T119.md` 已写好、`【刷` 占位整合期刷；
> Wave B 契约 `review/p10b-contracts.md`（与 A 版并存，只加不改）。
> **Wave A 已整合（2026-09-10 晚，比计划早四天）**：五轨回执全收、按 T115 → T116 → T120 → T118 → T117 合进
> `integrate/p10-a` 并快进主干，**Wave B 基线 = `c2bc75f`**；T114 / T119 worktree 已钉此 sha、派单占位已刷。
> T113 仍在 7af9022 上跑，收工后单独合（`maos/roundtable/**` 以它为准）。整合期未接的三处见 `docs/BACKLOG.md`「整合期 p10-a」。
> 复赛现场 2026-09-22/23（杭州）。本手册只管 **9/11 → 9/21**。
> 全局铁律九条见 `CLAUDE.md`；本期新增的跨轨约定在 §4，派单时抽成 `review/p10-contracts.md`。
> 写法约定：**加反引号的路径 = 现存文件**（`scripts/check_docs.py` 会核存在性）；
> **不加反引号、标（新）的路径 = 本期要新建的文件**，派单里按此建。

## 0. 一页总览

评委三段建议在 9/2 前已按最小口径覆盖过一遍（`README.md:297-309` 映射表）。本期不是再覆盖一遍，
而是把每一条从**名义覆盖**做到**可核验**：一条脱敏真实退款案例，从 IM 入口跑到退款到账，
业务对象在 PolarDB 表里、事件链在 `event_log` 里、证据束一条命令重产、`verify.py` 全过。

| 主线 | 复赛现场能给评委看的东西 | 轨 |
|---|---|---|
| 一、多 Agent 协同 | `evidence/case-real-01/`：五岗圆桌 + DAG **同一条时间线的事件链**；8 个 Skill 各自的 `SkillInvoked` 行；计划审批 + 任务审批 + 返工 + 补偿的 HITL trace；真模型 token/时延；`verify.py` ≥ 10/10 | T113 T114 |
| 二、PolarDB 业务纵切 | 同一案例的 10 个业务对象（带版本）在 PolarDB 表里；DAG/Task/Artifact 经 `business_ref` 指到对象+版本；付款前读外部当前订单版本、漂移即停；「已提出 / 处理中 / 已到账」三态投影；工单有人接、有关闭 | T115 T116 T117 |
| 二、PolarDB RAG + 结果验证 | 九类流程知识进 `kb_doc`（七维预过滤 + 四通道）；Planner 给出必要任务 / 审批人 / 异常分支并带引用（对照实验 R8）；`case_outcome` 四判据判业务成功；只有证据完整且外部结果明确的案例自动晋升，失败实例聚成「渠道 × 返回码 × 政策」提示 | T118 T119 T120 |
| 三、材料 | README 映射表、架构边界段、答辩稿、PPT 加三页、≤ 8 分钟视频重录 | T121 |

## 1. 现状盘点（2026-09-10 四路只读普查，后续会话不必重查）

**已经有、直接挂上去的锚点**
- 退款域 16 张表全带 `tenant_id`（`maos/domain/refund/schema.sql`），权威守卫四道闸（`guard.py:279-347`），
  `payment.observe` 是 `settled` 唯一写者（`guard.py:29`），`business_ref` 桥（`schema.sql:197-207`，`objects.py:214-280`）。
- 12 个退款 skill（`maos/skills/builtin/refund/`），网关 ToolPort + 支付宝码表四象限（`maos/tools/gateway.py`, `gateway_codes.py`）。
- 场景 6/7（顺利 / 失败-返工-驳回-补偿），R3/R4/R6 对照，`custom_case.py` 自定义案例入口，`scenarios/bulk/` 60 单底账。
- 圆桌五岗（`maos/roundtable/`）：事实卡零模型、模型只复述、合议纯函数；Matrix 真房间入口 `hiclaw/room_ingress.py`。
- 知识层两阶段检索已实现：七维硬过滤 `tenant→biz_type→channel→region→sku→policy_version→workflow_version`
  （`maos/kb/retriever.py:70-73`），四通道 rule_no/.35 gateway_code/.25 fts/.20 vector/.20，`KbRetrieved` 事件；
  护栏三条断言（`maos/kb/guardrails.py`）；晋升规则 `classify_case`（`guardrails.py:362-384`）。
- PG 后端真实现 `maos/store/pg_store.py`（`MAOS_STORE_BACKEND=postgres` + `MAOS_PG_DSN`，不回落 SQLite）；
  阿里云 PolarDB PG 16.14 真实例 2026-08-30 五步冒烟 5/5、zhparser 已装（`deploy/polardb-live.md`）。
- 证据链：`scripts/make_evidence.py`（首行 `# generated at … from <sha>`）+ `scripts/verify.py` 9 项。
- HITL 机器侧完整：`BLOCKED→DONE [human_approve]` / `→FAILED [human_reject]`、返工 `REWORK`、replan 上限、补偿工单
  （`evidence/scenario-7/run.log`）；计划级审批停靠 `maos/runtime/plan_approval.py`（T111，**已并入未接线**）。

**缺口（评委三条都卡在这些点上）**
1. 五岗圆桌**完全不落库**：`StageReport`/`Verdict` 不进 `event_log`；`stages._invoke` 建 `SkillInvoker` 不带 store，
   `refund.evidence_check` / `refund.risk_screen` 在全部证据里 **0 条** `SkillInvoked`；`Speaker` 绕过 `model_usage`，真模型无 token/时延。
2. 业务表**全锁 SQLite**：`objects._conn()` 硬取 `SqliteStore._conn`（`objects.py:41-49`）；PG 上只有 `kb_doc_pg`
   参考形状（无租户列，`pg_schema.sql:9-12`），七维预过滤在 PG 上从未跑过。
3. `business_ref` 只覆盖 4/10（refund_case / order_snapshot / policy_rule / refund_request）；六类对象无版本字段；
   Artifact 不带 order/product 版本；没有一份把 10 个对象串起来的案例文件（`case_r*.json` 只有 6 块）。
4. **没有"执行前读外部当前版本"**：订单/商品快照全由 fixtures 预置，无 `order.query` ToolPort，无漂移检查。
5. 三态没有字面落地：七态 `biz_status`（`guard.py:55-63`），「已提出/处理中/已到账」只在通知文案里拼。
6. 人工补偿无闭环：`manual_ticket` 一行记录，审批人 = 字符串 + `MAOS_APPROVERS`，无角色目录、无关闭、无回填观察。
7. 知识层只有 `policy` / `history_case` 两类，`failure_hint` / `error_code_playbook` 零文档；8 条失败案例 kind 标错；
   `workflow_version` 字符串 vs INTEGER 列；政策投影丢 `channel_id`（BACKLOG:342）。Planner prompt 只喂命中标题，
   审批人/异常分支不来自知识层。
8. 结果判据只认到账（`make_evidence.py:417-480`）；客户确认靠靶场 `UPDATE ack_at`；投诉零命中；晋升只在 R5 手动调。
9. 证据束落后：主束 `3cdd344`（9/6），`make_evidence.py` 在带 `MAOS_LLM_*` 的 shell 里跑不成（BACKLOG:2290-2293）；
   真人审批退款案例的房间证据 **没有**（`evidence/room/` 是 8/29 软件域）。

## 2. 评委建议 → 现状 → 缺口 → 轨

| # | 评委原话（压缩） | 现状 | 本期做什么 | 轨 |
|---|---|---|---|---|
| 1.1 | 一条脱敏真实退款需求的可执行纵向切片 | 场景 6/7 合成案例 | 10 块齐的 `case_real_01.json`，一条命令跑四条路径出证据束 | T116 T114 |
| 1.2 | AgentTeams 事件链 | DAG 层有，圆桌层无 | 圆桌发言/合议/skill 调用/模型用量全部落 `event_log` + `model_usage`，可回放 | T113 |
| 1.3 | 8 个 Skill 中关键 Skill 的真实调用 | 10 个退款 skill，3 个零证据 | 钉死 8 个 Skill 清单（§4.3），每个在证据束里有 `SkillInvoked` | T113 T114 |
| 1.4 | 返工 / HITL Trace | 机器侧有，真人退款审批无 | 计划审批停靠接线 + 真房间真人 `/approve` 采集进束 | T114 + 真跑日 |
| 1.5 | Evidence Bundle | 8 束旧 sha | `evidence/case-real-01/`，`verify.py` ≥ 10/10 | T114 |
| 2.1 | 10 个业务对象关联到同一案例，DAG/Task/Artifact/Evidence 引用对象及其版本 | 引用 4/10，6 类无版本 | `business_ref` 10/10，六类对象加版本/修订，Artifact 带快照版本 | T116 |
| 2.2 | 外部系统保留权威事实；执行前读当前版本，返回后记观察；区分已提出/处理中/已到账 | 观察侧有，读前无，三态无字面 | `order.query` ToolPort + `refund.snapshot_check` 漂移即停；`public_status()` 三态投影 | T116 |
| 2.3 | 流程卡住后应由谁补偿 | 一行工单 | 角色目录 + 工单 assign/resolve + 人工处理后回填观察（仍走 `payment.observe`） | T117 |
| 2.4 | 以 PolarDB 为业务纵切载体 | 业务表 SQLite | 退款域 16 表 + `kb_doc` 七维在 PG/PolarDB 上跑同一套代码 | T115 |
| 2.5 | PolarDB RAG：九类流程知识；先过滤再组合规则编号/错误码/全文/语义 | 两类文档；PG 上预过滤未验 | 语料扩到九类（生成器）、修三处失真；PG 上七维预过滤 + zhparser 全文 | T118 T115 |
| 2.6 | Planner 推荐必要任务、审批人、异常分支；减少漏财务复核/错套政策/无限重试 | prompt 喂标题 | `PlanAdvice{required_tasks, approver_role, exception_branches, retry_budget, citations}`，对照实验 R8 | T119 |
| 2.7 | 历史只辅助规划，不替代当前事实与人工授权 | 护栏三条已有 | 保持；T119 加断言测试 | T119 |
| 3.1 | 以到账、客户确认、人工纠错、投诉验证整条 DAG；「都回复完成」≠ 业务成功 | 只认到账 | `case_outcome` 四判据 + `business_success`；「全 DONE 但观察 unknown」路径证据 | T120 |
| 3.2 | 只有证据完整且外部结果明确的案例进默认知识层；失败实例提示哪类组合需额外步骤 | `classify_case` 手动 | Plan 终态自动晋升；`failure_hint_index(channel × gateway_code × rule_no)` 供 Planner 消费 | T120 T119 |

## 3. 轨定义

每轨交付 = 代码 + 测试 + 两本账各一小节 + 回执。白名单以派单文件为准，这里只给方向。
所有新事件走 `event_log.event_type` 字符串（同 `RefundBizStatusChanged` 的做法），**不碰 `maos/contracts/**`**。
所有新表/新列走各轨自己的 SQL 片段（§4.1），**不改 `maos/core/store.py`**。

### Wave A（互不依赖，基线 = `0ad62b5`；已派单：T115 T116 T117 T118 T120）


**T115 业务层上 PolarDB —— 同一套代码跑 SQLite / PG**
- 目标：退款域 16 表 + `kb_doc`（七维）在 PostgreSQL / PolarDB 上跑通；SQLite 仍为缺省，无 DSN 时行为零变化。
- 交付：
  1. maos/domain/_dbport.py（新）：`DomainConn` 双后端（sqlite3 / psycopg3），`?`→`%s` 翻译、`_atomic` 两方言 SAVEPOINT、`executescript` 走 **DDL 翻译器** `to_pg_ddl()`（`AUTOINCREMENT`、类型、`datetime('now')` 默认值、`IF NOT EXISTS`）。**不手写第二份 DDL**，这样 T116/T120 追加的片段自动跟上。`objects._conn()` 改走它；选择由 `MAOS_DOMAIN_BACKEND=sqlite|postgres` + `MAOS_PG_DSN`。
  2. `kb_doc` 在 PG 上与 `maos/kb/schema.sql` 同构（七维 + rule_no + gateway_code + kind + outcome），`kb_doc_pg` 参考形状退役或对齐；阶段一预过滤 SQL 在 PG 跑通；全文 `MAOS_PG_FTS_CONFIG=zhcfg`（PolarDB 已装 zhparser；本机 `pgvector/pgvector:pg16` 没有 zhparser 就 `simple` + 说明，不许假装中文分词跑通）。
  3. `fixtures.seed_case / seed_history_kb` 双后端。
  4. 门禁测试（有 `MAOS_PG_DSN` 才跑，否则 skip）：`test_refund_domain_pg.py`（建表、四道闸、`business_ref` resolve、`pinned_policy_version`）、`test_kb_pg_prefilter.py`（无租户返回空、跨租户不召回）。
  5. `deploy/polardb.md` 加「退款域上 PolarDB 三步」；`scripts/polardb_smoke.py` 加第 6 步（退款域建表 + 一条 case 往返，脱敏输出）。
- 验收：`docker compose -f deploy/docker-compose.yml --profile pg up -d` 后 `MAOS_PG_DSN=… python3 -m pytest maos/tests -q -k pg` 全绿；`MAOS_DOMAIN_BACKEND=postgres MAOS_PG_DSN=… env -u MAOS_LLM_API_KEY -u MAOS_LLM_BASE_URL -u MAOS_LLM_MODEL python3 run.py --scenario 6` exit 0，且脚本打印 PG 里 `payment_observation` 行数 ≥ 1；无 DSN 全量与基线同数。
- 不做：`plan/task/artifact/event_log` 上 PG（§8）。白名单方向：`maos/domain/refund/objects.py`（**只许动连接/执行层**：`_conn/execute/query/_atomic/ensure_schema`）、maos/domain/_dbport.py、`fixtures.py`、`maos/store/{pg_store.py,pg_schema.sql}`、`maos/kb/__init__.py`（只动建表执行路径）、`deploy/**`、`scripts/polardb_smoke.py`、测试。

**T116 十对象单案例 + 版本 + 执行前读外部 + 三态投影**
- 交付：
  1. scenarios/refund/cases/case_real_01.json：10 块齐（申请 / 订单与商品快照 / 规则 / 客户证据 / 主管审批预期 / 财务核算预期 / 退款请求 / 网关观察预期 / 通知 / 补偿），`scenarios/refund/README.md` 加脱敏规范（客户名、手机号、订单号、地址打码规则）。人类给真案例前用**构造的制造企业案例**占位（例：电动工具厂商 → 经销渠道 → 无刷角磨机 15 天内质量问题 → 视频 + 照片 + 购买凭证 → AS-002@v2 全额 → 售后主管审批 + 财务复核 → 原路退款 → processing → settled → 企微通知），字段形状与真案例一致，替换只换数据。加载器 maos/domain/refund/case_pack.py（新），不改 `fixtures.py`。
  2. 六类对象加版本：`customer_evidence.version`、`approval_record.revision`、`finance_entry.revision`（二次核算 supersedes）、`refund_request.version`、`notification.revision`、`compensation_record.revision`。片段 `schema_p10_t116.sql` + `_MIGRATIONS` 追加。
  3. `business_ref` 10/10：`_REF_TARGETS` / `_VERSIONED_REF_TABLES` 扩到全部；各 skill 写对象时 `attach_business_ref`（product_snapshot 带 version）；Artifact content 带 `order_version / product_version / policy_version`；`applicant_ref` 悬空修掉。
  4. 执行前读外部当前版本：maos/tools/order.py 的 `order.query` ToolPort（`MockOrderSystem` 自带 `ext_order(order_id, version, status, amount, updated_at)`，可注入「外部改单」）；新 skill `refund.snapshot_check`：比对 `order_snapshot.version` 与外部当前 version，漂移 → 出参 `drift=true` + 事件 `SnapshotDrift{case_id, snapshot_version, current_version}`，付款任务走现有 `gate_needs_human` 出口落 `BLOCKED`（不加状态）。场景 6 与 `custom_case.py` 在付款前插这一步；`scripts/run_case.py --drift` 注入漂移。
  5. 三态投影 `maos/domain/refund/projection.py::public_status(biz_status, has_request, observed_state)` → 字面值固定五个（§4.4）；`notify.py` 与 `/pending` 卡片改用它。**不动 `maos/roundtable/**`**（财务岗卡片接投影是整合期的事）。
- 验收：`env -u … python3 scripts/run_case.py scenarios/refund/cases/case_real_01.json` exit 0，导出的 `business-objects.json` resolved 10/10 且每条带版本；`--drift` 跑出 `BLOCKED` + `SnapshotDrift`；`--reject` 路径 `approval_record.revision` 正确；测试 ≥ 30 条。
- 白名单方向：`scenarios/refund/**`、`maos/domain/refund/{schema_p10_t116.sql,case_pack.py,projection.py}`（新）、`objects.py`（**只许动 `_REF_TARGETS` / `_VERSIONED_REF_TABLES` / `_MIGRATIONS`**）、maos/tools/order.py（新）、`maos/skills/builtin/refund/{snapshot_check.py(新), intake/policy/finance/payment_execute/payment_observe/notify/compensate.py 的 attach 段}`、`maos/flows/{scenario_6,custom_case}.py`、`scripts/run_case.py`（`--drift`）、测试。

**T117 人工补偿闭环 + 审批人目录 —— "卡住后由谁补偿"**
- 交付：
  1. maos/domain/refund/roles.py + scenarios/refund/roles.json：角色目录 `after_sales_supervisor / finance_reviewer / payment_ops / region_manager`，`MAOS_APPROVERS` 里的账号映射到角色；`verdict.approver_role` 与 `policy_rule.params.approver_role` 用同一套名字。
  2. `compensation_record` 加 `assignee_role / assignee / opened_at / resolved_at / resolution_kind / resolution_observation_id`（片段 `schema_p10_t117.sql`）；工单派给谁由角色目录决定（默认 `payment_ops`），事件 `CompensationAssigned / CompensationResolved`。
  3. 人工处理后回填观察：新 skill `refund.compensation_close`，入参线下凭证摘要，**通过 `payment.observe` + `gateway='manual'` 适配器**写 `payment_observation(settled)`（铁律 8：外部凭证是权威事实，`settled` 仍只由 observe 写）。
  4. 房间/CLI 命令：`/assign <ticket> <role>`、`/resolve <ticket> <digest>`、`/confirm <case>`、`/complain <case> <text>`，新模块 maos/ingress/outcome_commands.py。**不碰 `router.py`**（另一会话在改），注册那一行留给整合期。
- 验收：场景 7 路径末尾 ticket → assign → resolve 后 `case_outcome.manual_correction=compensated`、`arrival=settled`（basis 指向 manual 观察）；未 resolve 时 `arrival=unknown`；名单外账号 `/assign` 被拒并落事件；测试 ≥ 20 条。
- 依赖：**无**（Wave A 化：验收判据改为直查 `compensation_record` 与 `payment_observation`，不依赖 T120 的 `case_outcome`）。白名单方向：`maos/domain/refund/{roles.py,schema_p10_t117.sql}`、scenarios/refund/roles.json、`maos/skills/builtin/refund/{compensate.py,compensation_close.py(新),payment_observe.py(manual 适配)}`、`maos/tools/gateway.py`（`ManualReceiptAdapter`）、`maos/ingress/{outcome_commands.py(新),router.py(一行)}`、测试。

**T118 知识层扩容 —— 九类流程知识 + 三处失真**
- 交付：
  1. scripts/gen_refund_kb.py（确定性，从 `scenarios/bulk/ledger-bulk.json` + 码表推导）产出 `scenarios/refund/kb/*.json`，覆盖评委九类：售后政策及生效范围（policy，含 channel/sku scope）、产品与渠道差异（policy 变体）、历史退款原因（history_case）、任务拆分模式（**新 kind** `task_pattern`）、人工驳回（**新 kind** `rejection`）、支付错误码（`error_code_playbook`，11 条码 × 处置/退避/补偿路径）、超时与补偿路径（进 playbook）、客户沟通结果（**新 kind** `comms_result`）、真实到账结果（**新 kind** `arrival_result`）。总量 ≥ 80 条，每类 ≥ 5。
  2. 修失真：8 条失败案例 kind → `failure_hint`；`workflow_version` 整数化；政策投影补 `channel_id`（BACKLOG:342）。
  3. `maos/kb/schema.sql` kind CHECK 扩四个；`maos/kb/__init__.py` 只动 kind 集合常量；`retriever.py` 加薄封装 `retrieve_playbook(gateway_code)`、`retrieve_task_patterns(ctx)`（**预过滤顺序与四通道权重不动**）；R5 装载新语料。
- 验收：`python3 scripts/gen_refund_kb.py --check` 两次生成 diff 为空；`python3 -m pytest maos/tests -q -k kb` 全绿且漏斗测试更新；`env -u … python3 run.py --scenario R5`（或 `--contrast`）的 `kb-hits.json` 出现 `error_code_playbook`。
- 白名单方向：`scenarios/refund/{kb/**,README.md,history/**,policy/**}`、`maos/kb/{schema.sql,__init__.py(kind 常量),retriever.py(薄封装),experiment.py}`、scripts/gen_refund_kb.py、测试。

**T120 结果验证与知识沉淀 —— 四判据 + 自动晋升 + 失败聚合**
- 交付：
  1. 片段 `schema_p10_t120.sql`：`case_outcome(tenant_id, case_id, arrival, arrival_basis, customer_confirmation, manual_correction, complaint, evidence_complete, business_success, computed_at)`、`complaint(tenant_id, case_id, channel, content_digest, opened_at, closed_at, resolution)`。枚举值见 §4.5。
  2. maos/domain/refund/outcome.py：`compute_case_outcome()` 纯函数 + 落表 + 事件 `CaseOutcomeComputed`；`record_confirmation()` / `record_complaint()`；CLI `scripts/case_inbound.py --confirm|--complain`（真入站命令由 T117 接进房间）。
  3. `make_evidence.business_outcome` 改从 `case_outcome` 推导四判据；`verify.py` 第 6 项升级（DONE 且 arrival≠settled ⇒ 必须 `business_success=false`），新增第 10 项 `check_case_outcome`（四判据齐、basis 可回查）。
  4. 自动晋升：maos/kb/promotion.py，`PlanFinalizer` 终态钩子调 `classify_case` → `business_success && evidence_complete` 才写 `kb_doc(history_case, success)`；否则 `failure_hint`；聚合表 `failure_hint_index(tenant_id, channel_id, gateway_code, rule_no, extra_steps, count)`。语料导入也走 `classify_case`（不许绕过）。
  5. 「全 DONE 但观察 unknown」路径：`scripts/run_case.py --stall`（MockGateway 永不终态 + `max_polls` 到顶）⇒ `business_success=false` 证据。
- 验收：`env -u … python3 run.py --scenario 6` 的 `result.json.business_outcome` 含四判据；`--stall` 路径 `business_success=false` 且 verify 第 6 项 PASS；`python3 scripts/verify.py` `RESULT: 10/10 PASS`；`failure_hint_index` 在场景 7 后有 ≥ 1 行。
- 白名单方向：`maos/domain/refund/{schema_p10_t120.sql,outcome.py}`（新）、`maos/kb/{guardrails.py(classify_case 扩四判据),promotion.py(新)}`、`maos/runtime/plan_finalizer.py`、`scripts/{make_evidence.py(business_outcome 段),verify.py(第 6/10 项),run_case.py(--stall),case_inbound.py}`、`docs/authoritative-facts.md`（四判据段）、测试。

### Wave B（T113 已于 9/10 晚提前开工、基线 `7af9022`；T114 / T119 基线 = `c2bc75f`，Wave A 整合后主干）

**T113 圆桌落库 —— AgentTeams 事件链可核验**
- 目标：五岗每次发言、合议、skill 调用、模型用量进 store，圆桌成为可回放的事件链。
- 交付：
  1. `RefundRoundtable` / `stages._invoke` 接 store；`SkillInvoker(identity, store=…)` → `refund.evidence_check`、`refund.risk_screen` 首次留 `SkillInvoked`。
  2. 三类事件行：`RoundtableRound{case_id, entry, round_no}`、`RoundtableSeatSpoke{case_id, seat, spoken_by_model, fallback_reason, facts_digest, speech_digest, model_call_id}`、`RoundtableVerdict{case_id, recommend, approver_role, blockers, seats}`。
  3. `Speaker` 每次真模型调用写 `model_usage`（tokens / latency_ms / model）。
  4. `hiclaw/room_ingress.wire()` 的 `:memory:` store 可由 `MAOS_INGRESS_DB=<path>` 换成文件库（真房间跑完有库可导）。
  5. `scripts/replay_roundtable.py --db <db> --case <id>`：零模型从 `event_log` 重建五岗顺序 + 合议；`room_team_smoke.py --evidence-out` 改从 store 导出，带首行 header。
- 验收：`python3 scripts/room_team_smoke.py --db /tmp/rt.db` 后查 `event_log`：`RoundtableSeatSpoke` = 5 × 轮数，`SkillInvoked` 含上述两个 skill；`replay_roundtable.py` 输出与 smoke 的座次一致；新测试 `test_roundtable_persist.py`。
- 依赖：**必须等另一会话把圆桌那轮（发声门禁 + 上传按钮）commit 落地**，基线钉在那个 commit 之上。白名单方向：`maos/roundtable/**`、`maos/ingress/router.py`（只加 store 透传）、`hiclaw/room_ingress.py`（只加 env）、`scripts/{room_team_smoke,replay_roundtable}.py`、测试。

**T114 单案例端到端证据束 —— 评委的第一眼**
- 交付：
  1. `scripts/make_case_bundle.py --case scenarios/refund/cases/case_real_01.json --path happy|drift|gateway_fail|reject [--all-paths] [--live-model] [--room-transcript <file>]`：入口（sheet/chat）→ 圆桌 → verdict → **计划审批停靠（接线 T111 `plan_approval`）** → `/approve`（CLI 或采集来的房间记录）→ DAG（含 `snapshot_check`）→ 网关 → 观察 → 通知 → `case_outcome` → 晋升。
  2. 输出 `evidence/case-real-01/<path>/`：`INDEX.json`、`business-objects.json`（10/10 + 版本）、`event-chain.json`（DAG + 圆桌事件按时间线合并：`KbRetrieved / SkillInvoked / RoundtableSeatSpoke / StateTransition / RefundBizStatusChanged / SnapshotDrift / CaseOutcomeComputed`）、`skills.json`（§4.3 的 8 个 Skill 各自 `invocation_id / input_digest / output_hash / version`）、`hitl-trace.json`（计划审批 + `BLOCKED` 审批 + 返工 + 补偿，操作者与时间）、`model-usage.json`、`outcome.json`、`kb-hits.json`、`run.log`、`trace.json`、`room-transcript.md`（有则附）。全部首行 header，密钥脱敏 + 哨兵反查。
  3. `verify.py` 认识该束（provenance 锚 + 第 10 项覆盖）。
  4. 修 `make_evidence.py` 在真模型 env 下失败的根因（BACKLOG:2293 三个落点：空补丁集应是合法结论）。**改动 ≤ 40 行，超了停下来报告**。
- 验收：`env -u … python3 scripts/make_case_bundle.py --all-paths` exit 0 → `python3 scripts/verify.py` `RESULT: 10/10 PASS`；`source ~/.maos.env && python3 scripts/make_case_bundle.py --path happy --live-model` exit 0 且 `model-usage.json` 有 ≥ 5 条真调用；`env -u … python3 scripts/make_evidence.py` 与带 key 的 shell 下都 exit 0。
- 依赖：T113 T116 T120。白名单方向：`scripts/{make_case_bundle.py(新),make_evidence.py,verify.py,capture_room_transcript.py}`、`maos/flows/custom_case.py`（接 plan_approval）、`maos/skills/builtin/code_repo_patch.py` / `maos/tools/sandbox.py` / `maos/runtime/gate.py`（**只许改根因那三处**）、`evidence/case-real-01/**`、测试。


**T119 Planner 建议 —— 知识层驱动必要任务 / 审批人 / 异常分支**
- 交付：
  1. `maos/kb/plan_advice.py::advise(ctx) -> PlanAdvice{required_tasks[], approver_role, exception_branches[], retry_budget, citations[]}`；来源 = `policy.params(extra_tasks/approver_role)` + `task_pattern` + `error_code_playbook` + `failure_hint_index(channel × gateway_code × rule_no)`；每条建议带 `doc_id`。
  2. `ManagerAgent._user_message` 喂结构化建议（不只是标题）；`contrast.policy_directives()` 改从 advice 取；`_should_replan` 的重试预算读 `advice.retry_budget`（上限仍 `MAOS_MAX_REPLAN`，只许更紧不许更松）；事件 `PlanAdvised{citations}`。
  3. 护栏不变且加断言测试：建议不能删任务、不能带订单事实字段、不能跳审批、不能把 `effect_risk` 降级、**不能替代当前订单快照与人工授权**。
  4. 对照实验 R8（沿 R5 骨架）：无建议 → 漏财务复核 / 错套渠道政策 / 重试到顶；有建议 → 补上 `finance_review`、换成 `region_manager`、预算 1 次即转人工。evidence/contrast-R8/dag-diff.json（新）。
- 验收：`env -u … python3 run.py --contrast` 多出 R8 行且 exit 0；`dag-diff.json` 的 `required_tasks` 差异非空并带 `citations`；护栏断言测试 ≥ 10 条全绿。
- 依赖：T118 T120。白名单方向：`maos/kb/{plan_advice.py(新),guardrails.py(断言),experiment.py(R8)}`、`maos/agents/manager.py`、`maos/flows/contrast.py`、`maos/core/control_plane.py`（**只许动 `_should_replan` 读预算那几行**）、测试。

### Wave D（主会话 + 人类）

**T121 材料**：README 映射表按 `evidence/case-real-01/` 逐行重指（8/8→≥10/10 等过期数字一起刷）；`docs/architecture.md` 加边界段「业务对象与知识层在 PolarDB，控制面在本地 SQLite」并删掉 `pg_store.py 是空壳` 那句；`docs/defense-brief.md` 加「评委三条 → 证据路径」；`docs/ppt-outline.md` 加三页（单案例事件链时间线图 / PolarDB 上的十对象与版本 / 四判据与晋升漏斗）；`docs/demo-script.md` 改为单案例失败路径主线（≤ 8 分钟，必含 Agent 协作 + Skill 调用 + AgentTeams 状态）；重录视频；`bash scripts/make_release.sh`。

## 4. 跨轨契约（派单时抽成 `review/p10-contracts.md`，任何轨不得改）

1. **新表/新列**：各轨放自己的 `maos/domain/refund/schema_p10_t<NN>.sql`（SQLite 方言，`IF NOT EXISTS`），由自己的模块在首次使用时 `ensure_<name>_schema(conn)`，**不改 `objects.ensure_schema`、不改 `schema.sql` 主文件**；PG 方言由 T115 的翻译器统一处理，片段里不许用翻译器不认识的 SQLite 特性（`AUTOINCREMENT`、`WITHOUT ROWID`、`datetime('now')` 以外的函数默认值）。
2. **`objects.py` 分区**：T115 只动 `_conn / execute / query / _atomic / ensure_schema`；T116 只动 `_REF_TARGETS / _VERSIONED_REF_TABLES / _MIGRATIONS`；其他新逻辑一律进新模块。
3. **8 个 Skill 清单（固定，证据束逐个核）**：`refund.intake`、`refund.evidence_check`、`policy.match`、`refund.risk_screen`、`finance.settle`、`payment.execute`、`payment.observe`、`notify.customer`。失败路径附带第 9 个 `refund.compensate`；受理期 `refund.reason_classify` 算模型辅助不算 Skill 清单。
4. **三态投影字面值**：`已提出退款`（approved 且 refund_request 已写，或 gateway_accepted）、`支付处理中`（processing）、`退款已到账`（settled，basis = observation）、`已驳回`、`已补偿（未到账）`。禁词表（`verdict.py:27`、`router.py:19`）不变：没观察到就不许说「已到账」。
5. **`case_outcome` 枚举**：`arrival ∈ {settled, unsettled, unknown}`、`customer_confirmation ∈ {confirmed, disputed, none}`、`manual_correction ∈ {none, overridden, compensated}`、`complaint ∈ {none, open, closed}`；`business_success = arrival==settled and customer_confirmation!=disputed and complaint!=open`；`evidence_complete` = 10 类对象引用都能 resolve。
6. **事件类型名**：`RoundtableRound / RoundtableSeatSpoke / RoundtableVerdict`（T113）、`SnapshotDrift`（T116）、`CaseOutcomeComputed / CasePromoted`（T120）、`CompensationAssigned / CompensationResolved`（T117）、`PlanAdvised`（T119）。detail 里一律带 `tenant_id, case_id`。
7. **`PlanAdvice` 字段**：`required_tasks: [{role, title, reason, doc_id}]`、`approver_role: str`、`exception_branches: [{trigger(gateway_code|drift|timeout), action, doc_id}]`、`retry_budget: int`、`citations: [doc_id]`。
8. **`failure_hint_index` 键**：`(tenant_id, channel_id, gateway_code, rule_no)`，值 `extra_steps: [str]`、`count`。
9. **`scripts/run_case.py` 旗标归属**：`--drift`（T116）、`--stall`（T120）；各自只加自己的 `add_argument` 与分支。
10. **环境变量**：`MAOS_DOMAIN_BACKEND`（新，T115）、`MAOS_INGRESS_DB`（新，T113）；`MAOS_PG_DSN / MAOS_PG_FTS_CONFIG / MAOS_STORE_BACKEND` 沿用。密钥只读 env（铁律 6）。
11. **谁都不许动**：`maos/contracts/**`、`.contracts.lock`、`docs/parallel/contracts.md`、`maos/artifacts.py`、`scripts/guard_bash.py`、`.claude/**`、`maos/core/store.py` 现有表；`docs/expected-metrics.json` 与 `docs/submission-checklist.md:24` 锚点**整合期统一刷**；`maos/roundtable/**` 除 T113（T117 只改 `verdict.py` 角色名常量）。
12. **Scripted 口径已机器化**（T125，2026-09-11 Wave C）：`MAOS_FORCE_SCRIPTED=1` 由 `run.py`、`scripts/demo_preflight.sh`、`scripts/make_evidence.py` 的子进程与 `maos/tests/conftest.py` 缺省设上，`select_model_client()` 见到它一律返回 `ScriptedModelClient`、无视 `MAOS_LLM_*`。**`--live-model` 是唯一的显式出口**，它压得过环境里已有的值；房间入口（`hiclaw/room_ingress.py`）与 `make_case_bundle.py --live-model` 不设。于是 `env -u MAOS_LLM_API_KEY -u MAOS_LLM_BASE_URL -u MAOS_LLM_MODEL` 这个前缀**不再是必需的**（带上也无害，Wave C 之前的命令原样能跑）。真模型口径仍是 `set -a; . ~/.maos.env; set +a`。

## 5. 时间线与波次（9/10 周四起）

| 日期 | 事 | 门 |
|---|---|---|
| 9/10 四 | 人类拍板 §6；另一会话把圆桌发声门禁 commit；主会话建 5 个 worktree（钉 sha）+ `p10-contracts.md` + 5 份派单，在一个 worktree 实跑基线写进「开场自检期望值」 | `claude-fleet t115 t116 t117 t118 t120`（已开）；圆桌 `7af9022` 落地后 T113 worktree 钉 7af9022 + 派单 → `claude-fleet t113` |
| 9/11 五 – 9/13 日 | **Wave A** 五轨并行 | 各轨回执 |
| ~~9/14 一~~ **9/10 四晚（提前）** | Wave A 整合**已完成**：`integrate/p10-a` 从 `7af9022` 起，合并顺序 T115 → T116 → T120 → T118 → T117，3 处冲突手工合；刷 `expected-metrics`（3541 / 79 / PG 门控 64 / verify 10/10）；本机 docker PG 有库档 3604 passed；干净树重产 8 束 + domains；快进主干 = `c2bc75f`；T114 / T119 worktree 已建、派单已刷。T113 收工后单独合 | `claude-fleet t114 t119` |
| 9/11 五 – 9/13 日 | **Wave B** 两轨并行（T114 / T119）+ T113 收尾；三轨回执 | 各轨回执 |
| ~~9/14 一 – 9/15 二~~ **9/11 五上午（提前三天）** | Wave B 整合**已完成**：`integrate/p10-b` 从 `137c960` 起，按 T114 → T119 → T113 合并，三次零冲突；整合期只接了 `scenario_6.py` 的 `kb_context` 带 `case_id` 那一行；`pytest_passed_nopg` 3541 → 3636；干净树重产 8 束 + domains + `--all-paths` + `happy --live-model` + R8，verify 10/10；代码 `283ce68` + 证据 `4756832`，快进主干并 push。**点名留给下一波的三处接线**（`outcome_commands` 进 router、财务岗卡片接投影、`verdict.py` 角色名对齐）当时未接，记进 BACKLOG | 整合验收（§9） |
| 9/11 五上午 | **Wave C** 五轨并行派单，基线 `4756832`，契约 `review/p10c-contracts.md`：**T121** 材料重指单案例束 · **T122** 四条结果面命令进 router + 处置与命令共用一个库 · **T123** 圆桌财务岗接三态投影 + 审批角色名收成一套 · **T124** 同渠道重试不留悬空引用、`gateway_fail` 长出 `REWORK` · **T125** Scripted 口径机器化 + coding 岗补丁预检自修复。兑现的是上面那三处接线加 T114 / T119 的欠账 | `claude-fleet t121..t125` |
| 9/11 五下午 | **Wave C 整合**：`integrate/p10-c` 从 `4756832` 起，按 T125 → T122 → T123 → T124 → T121 合并，五次零冲突（两本账靠本地 `.git/info/attributes` 的 `merge=union`）；五轨各出一份独立审查回执，整合期修掉审查点出的四处（`/approve` 回帖自相矛盾、`make_evidence --live-model` 到不了子进程、`MockGateway.fail_times ≥ 2` 改判不了、`MAOS_LLM_BASE_URL` 没进脱敏哨兵）+ 底账加一单失败网关码；刷 `expected-metrics`、重跑 `gen_docs.py`；文档与代码**先**全部 commit，**再**在干净树上重产全部证据束 | 整合验收（§9） |
| 9/11 五下午 | **Wave D** 六轨并行派单，基线 `6bfa117`，契约 `review/p10d-contracts.md`：**T126** 业务对象落 PolarDB/PG 产一整束 · **T127** `report.html` 收进单案例束 · **T128** 真模型束与 Scripted 束彻底分根 + 核验器认得出来 · **T129** 房间命令面收口（两张嘴一个口径、补个 `/compensate` 救自动开单失败的案子）· **T130** 缺省审批岗两套写法收成一条机器判据 · **T131** `GOVERNED_KEYS` 补齐到八个 + coding 岗重问带上一版补丁正文 | `claude-fleet t126..t131` |
| 9/11 五傍晚 | **Wave D 整合**：`integrate/p10-d` 从 `6bfa117` 起，按轨号序 T126 → T131 合并，六次零冲突。主会话逐 hunk 读六轨非测试 diff，整合期处理三件事：① T126 的 PG 束 `business_outcome` 是空的（`result.json` 那条路没换业务库读取口径），把 `biz` 口径接到 `make_evidence` 那条链上；② `verify.py` 认 `INDEX.json` 的 `domain_backend`，业务表在外部后端的束不计进 `business-outcome` / `case-outcome` 的分子、结尾点名；③ PG 束让 `check_docs` 的简写路径解析出现歧义，三处改全路径。刷 `expected-metrics`（3746/76/64 → **3819/79/67**）；代码 `cc86ed0` + 证据 `0fb74b5`，快进主干，**未 push**。PG 束只有 `happy` —— PG 上的补偿路径撞 `compensate.py` 的 `PRAGMA` 探针，与三份加列助手的去重一起记进 BACKLOG | 整合验收（§9） |
| 9/11 五晚 | **Wave E** 五轨并行派单，基线 `6f26b31`，契约 `review/p10e-contracts.md`。选轨依据不再是 §3 的原计划（T113–T121 早已做完），而是 `docs/BACKLOG.md` 里积下来的账：**T132** 知识层真的上 PolarDB（`architecture.md` §5 那半句此前三处与实况不符）· **T133** 两份加列助手去重、PG 上跑通四条路径 · **T134** 圆桌那一段挂上时间线（8 条 stray + 5 次无归属调用是同一个洞）· **T135** 房间那次 `/approve` 不替人签「跑起来之后才出现的闸」· **T136** 收尾四小件 | `claude-fleet t132..t136` |
| 9/12 六凌晨 | **Wave E 整合**：`integrate/p10-e` 从 `6f26b31` 起，按轨号序 T132 → T136 合并，五次零冲突。**整合期一处真 bug 都没捞出来**，反而三轨纠正了派单里的错并以实跑为准（T133 发现 `_dbport.add_column_if_missing` 早就存在且实测了「照搬回落会静默不加列」；T134 发现圆桌用量行的归属键实为全 None；T132 如实报告本机 PG 无中文分词器、不绕过 `_CJK` 检查）。整合期只做三件：刷真源（3819/79/67 → **3874/90/78**）、写两本账、记下两条边界（契约 §A 未给测试文件分区；铁律 9 在 `kb/plan_advice.py` 上「取值可局部 import + 兜底、断言不行」）。代码 `4a53a5c` + 证据 `b472097`，快进主干，**未 push**。成果：PG 束四条路径全通且逐条同构、`verify` trace-tree 分母 39 → 125、`report.html` 14 束/818 span → 17 束/989 span | 整合验收（§9） |
| 9/12 六上午 | **Wave F** 五轨并行派单，基线 `c0d8303`，契约 `review/p10f-contracts.md`：**T137** 驳回之后的三件（客户通知 / 审批表那一行 / 闸上的署名）· **T138** 三条「绿得没有信息」的判据补盲 · **T139** PG 上的检索真成立（向量走 HNSW、错误码查得到、中文分两档）· **T140** 读证据的人会看到什么（note 改回现况、两边都空不判可疑、治理键数目、跨实例比对）· **T141** 真跑日总装（preflight 探前置、runbook、采集链路离线演练） | `claude-fleet t137..t141` |
| 9/12 六下午 | **Wave F 整合**：`integrate/p10-f` 从 `c0d8303` 起，按轨号序 T137 → T141 合并，五次零冲突。**本波首次每轨派独立审查员 + 每条 finding 三视角对抗验证**（code / repro / intent，默认立场「它是误报」，≥2 票驳不倒才算成立）：捞出 5 条 major + 4 条 minor/nit，**全部 3/3 存活**，整合期当场修掉 8 条 —— ① T137 的铁律 8（`/compensate` 那条路上把一件没观察到的资金结果写进了发给客户的正文）② T137 那条双向哑弹的测试 ③ T138 的 AST 判据只认链式写法 ④ T139 的「向量真走 HNSW」判据没绑到生产 SQL（改回退化形状，修复前全量 3978 条没有一条拦得住）⑤ T139 的一条恒真断言 ⑥ T140 的 Scripted 束 note 自打脸 + 那条空转测试 ⑦ T141 的 runbook 漏 `source room.env`（真跑日必报错）⑧ T141 的离线演练夹具偏离真房间。**对抗验证还纠正了主会话自己的一处修法**：出口 IP 不是「查询被接管」，是 `dig` 走了 IPv6，`dig -4` 稳定拿得到 —— 编出来的病因已全部删掉。刷真源（3874/90/78 → **3958/101/88**，门控是 88 不是 89：中文分词那条是双重门控，有库也 skip，不进两档差值）、写两本账、`## task-t140` 的标题补回（被 `merge=union` 并进了上一节）。代码 `<CODE_SHA>` + 证据 `<EVID_SHA>`，快进主干，**未 push** | 整合验收（§9） |
| 9/18 五 – 9/19 六 | **真跑日**（人类 + 主会话）：PolarDB 真实例（白名单 + DSN）跑 `polardb_smoke.py` 6 步 + 单案例四路径，截图三张（实例详情页 / 终端 / 表行数）；真 Matrix 房间真模型五岗 + 真人 `/approve` 与 `/reject` 各一次，`capture_room_transcript.py` 采进束；`--live-model` 束带 `model-usage.json` | 证据束首行 sha 干净、无 `-dirty` |
| 9/20 日 – 9/21 一 | **T121** 材料 + 视频重录 + 彩排 + `make_release.sh`；9/21 下午留白当缓冲 | checklist 全可勾 |
| 9/22 二 – 9/23 三 | 复赛现场 | — |

## 6. 需要人类决定的五件事（缺省假设已写；不答按缺省走，只有前两条会卡真跑日）

1. **PolarDB 实例还在不在**（控制台自己看，我不开阿里域名）。在：把当前出口 IP 加白名单、确认 `~/.config/maos/pg.env` 的 DSN 仍可连；不在：**9/16 前重建**（8/30 实测按量付费约 ¥183/月，用完删）。缺省：Wave A/B 全程只用本机 `pgvector/pgvector:pg16`，真实例只在 9/18 真跑日用；若届时没有，PPT 口径退回「PolarDB 8/30 实测 + 本机 PG 同构证据」（按 `submission-checklist.md:215` 的许可说法）。
2. **脱敏真实退款需求**：9/14 前给一份（订单、商品、渠道、原因、证据材料清单、金额、时间线、谁审批、最终怎么退的）。缺省：T116 先用构造的制造企业案例占位，格式一致，9/17 前替换只换数据。
3. **是否上真向量模型**（当前 64 维 3-gram 哈希）。推荐**不上**：12 天内收益小，DeepSeek 无 embeddings 端点，另配供应商要开 key、要重算全部向量。缺省：不上，`retriever.embed()` 那处保留「替换一处」的说明。
4. **模型 tool-call 选 Skill**（Planner 用 function calling 挑 skill）。推荐不进本期，记 BACKLOG。
5. **控制面四表是否也上 PolarDB**。推荐不上；口径写成「业务对象与知识层在 PolarDB，控制面在本地 SQLite」进 `architecture.md`。

## 7. 风险与退路 / 砍序

- **PolarDB 白名单出口 IP 漂移**（症状 TCP 静默超时，BACKLOG:1367）：真跑日先跑 `polardb_smoke.py`，不通就当天改白名单，改不了退本机 PG。
- **真模型不稳**（key / 证书 / 空补丁集）：`verify.py` 只认 Scripted 束；`--live-model` 束单独产、单独标。**Wave C 之后这条退路是机器缺省**（§4.12）：不给 `--live-model` 就一定是 Scripted，不再靠人记得加 `env -u` 前缀。
- **`objects.py` 合并冲突**：按 §4.2 分区；整合顺序 T115 先合。
- **圆桌改动撞车**：T113 基线必须包含发声门禁那次 commit；T116/T117 不碰 `maos/roundtable/**`（T117 只改常量）。
- **时间不够时的砍序**：先砍 T119（检索 + 护栏已有，少的是"建议"这层和 R8 对照）→ 再砍 T117 到最小（只留 assign/resolve，不做 manual 观察）→ T114 的 `--live-model` 与房间采集可降级为 Scripted 束 + 截图。**T115、T116、T120 不砍**，那是评委第二条的骨架。

## 8. 本期明确不做

模型 tool-call 选 Skill；控制面表上 PG；支付宝真沙箱（`AlipaySandboxAdapter` 仍空壳，口径不变）；真向量模型；并发化；Element 深度集成的任何新说法（`agentteams-mapping.md:91-97` 的三条不许说照旧）。

## 9. 整合期收工检查单（两次整合都过一遍）

- `python3 -m pytest maos/tests -q` 只剩 `expected_metrics` 那条红 → 回填后 0 failed（带 key 的 shell 下环境红另计）
- `git diff <基线> -- maos/contracts/ maos/core/store.py` 为空；`test_contracts_frozen.py` 全绿
- `env -u … python3 run.py` exit 0，`Plan:` 7 行；`--contrast` 含 R8
- `MAOS_PG_DSN=… python3 -m pytest maos/tests -q -k pg` 全绿（本机 docker）
- 干净工作区重产证据，首行 sha 无 `-dirty`；`python3 scripts/verify.py` ≥ 10/10
- `python3 scripts/check_docs.py` 阻断 0；`python3 scripts/gen_docs.py --check` 绿
- 两本账各轨小节齐；`docs/expected-metrics.json` + `submission-checklist.md:24` 锚点已刷
- 只本地 commit，不 push

## 10. 下一步

Wave A / B / C 共十四轨全部整合进 `goai-restructure`（Wave C 于 2026-09-11 下午合入，**未 push**）。剩下的是 §6 与真跑日：人类回复 §6 的 1、2 两条（其余按缺省）；**9/18 – 9/19 真跑日**跑 PolarDB 真实例与真 Matrix 房间真人审批 —— 材料里那两处占位（`docs/defense-brief.md` 的实话节、`docs/demo-script.md` 镜 7）等的就是它；**9/20 – 9/21** T121 材料定稿 + 视频重录 + 彩排。Wave C 审查留下的账都在 `docs/BACKLOG.md` 的`## 整合期 p10-c` 小节里，没有一条卡真跑日。
