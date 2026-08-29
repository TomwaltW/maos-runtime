# Skill 目录

<!-- 本文件由 scripts/gen_docs.py 从运行时代码生成，**请勿手改**。
     改了代码就重跑 `python3 scripts/gen_docs.py`；
     `python3 scripts/gen_docs.py --check` 不一致即非零退出。 -->

注册表里共 **13 个 skill / 13 个版本条目**。契约共 12 个字段（maos/skills/contract.py:19）：`name + version` 是注册表主键，其余 10 个字段合成 **9 项要素**（`failure_policy` 与 `max_retries` 同属「失败策略」一项）。字段与顺序取自 `dataclasses.fields(SkillContract)`，本文件不另抄。

失败策略取值域冻结为 `retry`、`fallback`、`escalate`（maos/skills/contract.py:16）。

调用一律走 `SkillInvoker.invoke()`（maos/skills/invoker.py:50）：先校验 `name ∈ identity.allowed_skills`，越权抛 `PermissionDenied`；未注册返回 `failed:skill_not_found:<name>` 而不抛；成败都落一条 `SkillInvoked` event_log 行（`detail` 带 `input_digest` / `output_hash`，`scripts/verify.py` 第 1 项据此校验证据未被篡改）。

## 一览

| skill | 版本 | 域 | 归属角色 | 失败策略 | 依赖工具 | 声明位置 |
| :-- | :-- | :-- | :-- | :-- | :-- | :-- |
| `code.repo-patch` | `1.0.0` | 软件交付域 | `coding` | escalate | `git-mcp`、`sandbox` | `maos/skills/builtin/code_repo_patch.py:146` |
| `finance.settle` | `1.0.0` | 制造售后退款域 | `refund_finance` | escalate | （空） | `maos/skills/builtin/refund/finance.py:55` |
| `issue.aggregate` | `1.0.0` | 软件交付域 | `manager` | escalate | （空） | `maos/skills/builtin/issue_aggregate.py:66` |
| `kb.retrieve` | `1.1.0` | 软件交付域 | `manager`、`coding` | escalate | （空） | `maos/skills/builtin/kb_retrieve.py:73` |
| `kb.sink` | `1.0.0` | 软件交付域 | `manager` | escalate | （空） | `maos/skills/builtin/kb_sink.py:29` |
| `notify.customer` | `1.0.0` | 制造售后退款域 | `refund_intake` | retry（≤2 次） | （空） | `maos/skills/builtin/refund/notify.py:22` |
| `payment.execute` | `1.0.0` | 制造售后退款域 | `refund_payment` | escalate | `gateway.refund` | `maos/skills/builtin/refund/payment_execute.py:34` |
| `payment.observe` | `1.0.0` | 制造售后退款域 | `refund_payment` | escalate | `gateway.query` | `maos/skills/builtin/refund/payment_observe.py:36` |
| `policy.match` | `1.0.0` | 制造售后退款域 | `refund_policy` | escalate | （空） | `maos/skills/builtin/refund/policy.py:62` |
| `refund.compensate` | `1.0.0` | 制造售后退款域 | `refund_payment` | escalate | （空） | `maos/skills/builtin/refund/compensate.py:65` |
| `refund.intake` | `1.0.0` | 制造售后退款域 | `refund_intake` | escalate | （空） | `maos/skills/builtin/refund/intake.py:59` |
| `req.normalize` | `1.0.0` | 软件交付域 | `manager` | retry（≤1 次） | （空） | `maos/skills/builtin/req_normalize.py:46` |
| `test.verify` | `1.0.0` | 软件交付域 | `testing` | escalate | `sandbox` | `maos/skills/builtin/test_verify.py:30` |

## 逐个 skill × 九要素

### code.repo-patch @ 1.0.0

实现：`CodeRepoPatchSkill` @ `maos/skills/builtin/code_repo_patch.py:146`

| 要素 | 含义 | 值 |
| :-- | :-- | :-- |
| `purpose` | ① 用途 | 按任务契约产出补丁集，返回前完成受保护路径校验 |
| `input_schema` | ② 输入 | `title`: str<br>`inputs`: dict<br>`acceptance`: list[str]<br>`rework_findings`: list[dict] |
| `output_schema` | ③ 输出 | `files`: list[{path:str,diff:str}]<br>`summary`: str<br>`self_check`: {build:'pass\|fail', lint:'pass\|fail'} |
| `preconditions` | ④ 前置条件 | `title`、`inputs`、`acceptance` |
| `depends_tools` | ⑤ 依赖工具 | `git-mcp`、`sandbox` |
| `failure_policy` | ⑥ 失败策略 | escalate |
| `max_retries` | ⑥ 失败策略 · 重试上限 | 0 |
| `security_boundary` | ⑦ 安全边界 | 受保护路径判定：补丁路径规范化后按 / 分段，任一段命中 PROTECTED_SEGMENTS（infra / .github / secrets / tests，任意层级、大小写不敏感）立即抛 ProtectedPathViolation，不重试、不降级；skill 自身不落盘、不执行补丁 |
| `reuse_note` | ⑧ 复用说明 | Coding 角色唯一的补丁产出入口；返工走同一入口，findings 从 payload 进 |
| `owner_roles` | ⑨ 归属角色 | `coding` |

### finance.settle @ 1.0.0

实现：`FinanceSettleSkill` @ `maos/skills/builtin/refund/finance.py:55`

| 要素 | 含义 | 值 |
| :-- | :-- | :-- |
| `purpose` | ① 用途 | 按命中的政策规则核算退款金额，写 finance_entry 表并产出带 finance_entry 键的产物 |
| `input_schema` | ② 输入 | `tenant_id`: str<br>`case_id`: str<br>`policy`: dict（policy.match 的出参：decision / matched_rules / rule_refs） |
| `output_schema` | ③ 输出 | `finance_entry`: dict（= 写进 finance_entry 表那一行，F-1 判据）<br>`breakdown`: dict（核算过程，金额为字符串）<br>`rule_refs`: list[str]<br>`invocation_id`: str |
| `preconditions` | ④ 前置条件 | `tenant_id`、`case_id`、`policy` |
| `depends_tools` | ⑤ 依赖工具 | （空） |
| `failure_policy` | ⑥ 失败策略 | escalate |
| `max_retries` | ⑥ 失败策略 · 重试上限 | 0 |
| `security_boundary` | ⑦ 安全边界 | 只读 refund_case / order_snapshot，只写 finance_entry；不改 biz_status、不调模型、不碰支付网关；金额只按政策规则参数计算，不接受调用方直接指定 amount_approved |
| `reuse_note` | ⑧ 复用说明 | F-1：产出的 content 必带 finance_entry 键，且与库表同一份数据 |
| `owner_roles` | ⑨ 归属角色 | `refund_finance` |

### issue.aggregate @ 1.0.0

实现：`IssueAggregateSkill` @ `maos/skills/builtin/issue_aggregate.py:66`

| 要素 | 含义 | 值 |
| :-- | :-- | :-- |
| `purpose` | ① 用途 | 把多源信号里的 findings 聚合去重成结构化 issue 清单 |
| `input_schema` | ② 输入 | `findings`: list[dict] |
| `output_schema` | ③ 输出 | `issues`: list[{id:str,severity:str,title:str,detail:str,source:str}]<br>`summary`: str |
| `preconditions` | ④ 前置条件 | `findings` |
| `depends_tools` | ⑤ 依赖工具 | （空） |
| `failure_policy` | ⑥ 失败策略 | escalate |
| `max_retries` | ⑥ 失败策略 · 重试上限 | 0 |
| `security_boundary` | ⑦ 安全边界 | 只读入参，不写任何资源、不调用任何工具、不调用模型；不落盘 |
| `reuse_note` | ⑧ 复用说明 | 任何角色要把多源 findings 收成 issue 都复用它；判定零模型，结果可复现 |
| `owner_roles` | ⑨ 归属角色 | `manager` |

### kb.retrieve @ 1.1.0

实现：`KbRetrieveSkill` @ `maos/skills/builtin/kb_retrieve.py:73`

| 要素 | 含义 | 值 |
| :-- | :-- | :-- |
| `purpose` | ① 用途 | 两阶段检索沉淀过的经验与结构化知识，供 Agent 规划/执行前带入上下文 |
| `input_schema` | ② 输入 | `tags`: list[str]?<br>`keyword`: str?<br>`limit`: int?<br>`tenant_id`: str?<br>`biz_type`: str?<br>`channel_id`: str?<br>`region`: str?<br>`sku`: str?<br>`policy_version`: int?<br>`workflow_version`: int?<br>`rule_no`: str?<br>`gateway_code`: str? |
| `output_schema` | ③ 输出 | `items`: list[knowledge 行]<br>`count`: int<br>`docs`: list[{doc_id, score, title, kind, channels}]<br>`doc_count`: int |
| `preconditions` | ④ 前置条件 | （空） |
| `depends_tools` | ⑤ 依赖工具 | （空） |
| `failure_policy` | ⑥ 失败策略 | escalate |
| `max_retries` | ⑥ 失败策略 · 重试上限 | 0 |
| `security_boundary` | ⑦ 安全边界 | 只读 knowledge 与 kb_doc 两张表；写入仅限一条 KbRetrieved 事件日志（走现有 append_event_log，不加新 Topic）；不写任何业务资源、不调模型、不落盘 |
| `reuse_note` | ⑧ 复用说明 | Manager 规划前与 Coding 执行前的检索入口（两者白名单已含本 skill）；空结果不阻塞 |
| `owner_roles` | ⑨ 归属角色 | `manager`、`coding` |

### kb.sink @ 1.0.0

实现：`KbSinkSkill` @ `maos/skills/builtin/kb_sink.py:29`

| 要素 | 含义 | 值 |
| :-- | :-- | :-- |
| `purpose` | ① 用途 | 把复盘结论作为一条 rule 或 case 沉淀进知识库 |
| `input_schema` | ② 输入 | `plan_id`: str<br>`kind`: rule\|case<br>`title`: str<br>`body`: str<br>`tags`: list[str] |
| `output_schema` | ③ 输出 | `knowledge_id`: str |
| `preconditions` | ④ 前置条件 | `plan_id`、`kind`、`title`、`body` |
| `depends_tools` | ⑤ 依赖工具 | （空） |
| `failure_policy` | ⑥ 失败策略 | escalate |
| `max_retries` | ⑥ 失败策略 · 重试上限 | 0 |
| `security_boundary` | ⑦ 安全边界 | 只写 knowledge 表一张，经 store.insert_knowledge；不碰其余五表、不落盘、不调模型 |
| `reuse_note` | ⑧ 复用说明 | Plan 复盘的唯一写入口；任何角色要沉淀经验都走它，不要各自拼 INSERT |
| `owner_roles` | ⑨ 归属角色 | `manager` |

### notify.customer @ 1.0.0

实现：`NotifyCustomerSkill` @ `maos/skills/builtin/refund/notify.py:22`

| 要素 | 含义 | 值 |
| :-- | :-- | :-- |
| `purpose` | ① 用途 | 通知客户退款处理结果，记 notification；ack 缺失记 needs_followup 但不阻塞 |
| `input_schema` | ② 输入 | `tenant_id`: str<br>`case_id`: str<br>`content`: str（通知正文；缺省按案子状态生成）<br>`channel`: str（默认 sms）<br>`ack`: bool\|str（可选：客户回执时间或 True） |
| `output_schema` | ③ 输出 | `notification`: dict{tenant_id,case_id,channel,content_digest,sent_at,ack_at}<br>`acked`: bool<br>`needs_followup`: bool（ack 缺失即 True，但不阻塞 Plan）<br>`invocation_id`: str |
| `preconditions` | ④ 前置条件 | `tenant_id`、`case_id` |
| `depends_tools` | ⑤ 依赖工具 | （空） |
| `failure_policy` | ⑥ 失败策略 | retry |
| `max_retries` | ⑥ 失败策略 · 重试上限 | 2 |
| `security_boundary` | ⑦ 安全边界 | 只写 notification；不改 biz_status、不调模型、不碰支付网关；正文只含案子编号与金额结论，不带证据原文与任何凭证 |
| `reuse_note` | ⑧ 复用说明 | 任何「通知了但对端未确认」的场景都可照此写：记 needs_followup，不阻塞主流程 |
| `owner_roles` | ⑨ 归属角色 | `refund_intake` |

### payment.execute @ 1.0.0

实现：`PaymentExecuteSkill` @ `maos/skills/builtin/refund/payment_execute.py:34`

| 要素 | 含义 | 值 |
| :-- | :-- | :-- |
| `purpose` | ① 用途 | 核对审批后向支付网关发起退款，写 refund_request 并推进到 gateway_accepted/processing |
| `input_schema` | ② 输入 | `tenant_id`: str<br>`case_id`: str<br>`gateway`: str（已 register_gateway 的名字，默认 'demo'） |
| `output_schema` | ③ 输出 | `receipt`: dict（网关回执，非终态）<br>`request_id`: str（payment.observe 用它去 query）<br>`idempotency_key`: str<br>`biz_status`: gateway_accepted\|processing —— **永远不是 settled**<br>`needs_query`: bool（恒 True：终态只能问出来）<br>`invocation_id`: str |
| `preconditions` | ④ 前置条件 | `tenant_id`、`case_id` |
| `depends_tools` | ⑤ 依赖工具 | `gateway.refund` |
| `failure_policy` | ⑥ 失败策略 | escalate |
| `max_retries` | ⑥ 失败策略 · 重试上限 | 0 |
| `security_boundary` | ⑦ 安全边界 | 写 refund_request 与 biz_status(approved/gateway_accepted/processing)；**无权写 settled** —— guard 会抛 AuthoritativeFactViolation；付款前必须存在 approved 的 approval_record，本 skill 只读不写审批记录；网关调用一律经 invoke_tool，留 ToolInvoked 审计行 |
| `reuse_note` | ⑧ 复用说明 | 发起与观察分离：本 skill 只产生请求，终态一律由 payment.observe 观察得到 |
| `owner_roles` | ⑨ 归属角色 | `refund_payment` |

### payment.observe @ 1.0.0

实现：`PaymentObserveSkill` @ `maos/skills/builtin/refund/payment_observe.py:36`

| 要素 | 含义 | 值 |
| :-- | :-- | :-- |
| `purpose` | ① 用途 | 轮询支付网关取得终态回执，写 payment_observation 并（仅在此处）写 settled |
| `input_schema` | ② 输入 | `tenant_id`: str<br>`case_id`: str<br>`gateway`: str（已 register_gateway 的名字，默认 'demo'）<br>`request_id`: str（可选，缺省取该案子最近一笔 refund_request）<br>`max_polls`: int（可选，默认 5） |
| `output_schema` | ③ 输出 | `receipt`: dict（终态回执，或到顶时的最后一次观察）<br>`observed_state`: settled\|failed\|processing\|unknown<br>`poll_count`: int（问了几次 —— 终态是问出来的证据）<br>`biz_status`: str（settled 只可能由本 skill 写入）<br>`settled`: bool<br>`needs_compensation`: bool（网关明确失败时为 True，收口归失败路径场景）<br>`invocation_id`: str |
| `preconditions` | ④ 前置条件 | `tenant_id`、`case_id` |
| `depends_tools` | ⑤ 依赖工具 | `gateway.query` |
| `failure_policy` | ⑥ 失败策略 | escalate |
| `max_retries` | ⑥ 失败策略 · 重试上限 | 0 |
| `security_boundary` | ⑦ 安全边界 | 本 skill 是 guard.AUTHORITATIVE_WRITER —— 全系统唯一可写 settled 的 actor，且写入必须同事务附回执，缺字段由 guard 抛 AuthoritativeFactViolation；非终态一律不推进状态；网关调用一律经 invoke_tool 留审计行 |
| `reuse_note` | ⑧ 复用说明 | 任何「权威在外部系统」的终态都该照此写：先观察、再落库，两件事同一个事务 |
| `owner_roles` | ⑨ 归属角色 | `refund_payment` |

### policy.match @ 1.0.0

实现：`PolicyMatchSkill` @ `maos/skills/builtin/refund/policy.py:62`

| 要素 | 含义 | 值 |
| :-- | :-- | :-- |
| `purpose` | ① 用途 | 按订单快照锁定的政策版本检索适用规则并裁定退款资格（零模型，可复现） |
| `input_schema` | ② 输入 | `tenant_id`: str<br>`case_id`: str<br>`rule_prefix`: str（可选，默认 'AS-'） |
| `output_schema` | ③ 输出 | `policy_version`: int（订单锁定的版本，**不是**当前最新版本）<br>`matched_rules`: list[dict{rule_no,version,title,params}]<br>`rule_refs`: list[str]（形如 AS-01@v1）<br>`decision`: approve\|reject<br>`reason`: str<br>`invocation_id`: str |
| `preconditions` | ④ 前置条件 | `tenant_id`、`case_id` |
| `depends_tools` | ⑤ 依赖工具 | （空） |
| `failure_policy` | ⑥ 失败策略 | escalate |
| `max_retries` | ⑥ 失败策略 · 重试上限 | 0 |
| `security_boundary` | ⑦ 安全边界 | 只读 refund_case / order_snapshot / policy_rule，只写 business_ref；不改 biz_status、不调模型、不碰支付网关；政策版本一律取自订单快照，禁止使用 policy_rule 的最新版本 |
| `reuse_note` | ⑧ 复用说明 | 任何「按快照锁定的版本判定」的场景都可照此复用 objects.policy_rules_at_order |
| `owner_roles` | ⑨ 归属角色 | `refund_policy` |

### refund.compensate @ 1.0.0

实现：`RefundCompensateSkill` @ `maos/skills/builtin/refund/compensate.py:65`

| 要素 | 含义 | 值 |
| :-- | :-- | :-- |
| `purpose` | ① 用途 | 退款被驳回或走不通后的域内补偿收口：作废退款请求、写补偿记录与人工工单，把案子推进到 compensated |
| `input_schema` | ② 输入 | `tenant_id`: str<br>`case_id`: str<br>`operator`: str（做出驳回/收口决定的人）<br>`reason`: str（为什么走补偿，原样进补偿记录与事件）<br>`assignee`: str（可选，人工工单的接单人，缺省同 operator） |
| `output_schema` | ③ 输出 | `biz_status`: compensated<br>`revoked`: list[dict]（每笔作废的 refund_request 及其最后观察到的下落）<br>`ticket`: dict（人工工单：单号、接单人、要人去做什么）<br>`records`: int（落进 compensation_record 的行数）<br>`last_observed_state`: str（settled\|failed\|processing\|unknown\|unobserved）<br>`invocation_id`: str |
| `preconditions` | ④ 前置条件 | `tenant_id`、`case_id`、`operator`、`reason` |
| `depends_tools` | ⑤ 依赖工具 | （空） |
| `failure_policy` | ⑥ 失败策略 | escalate |
| `max_retries` | ⑥ 失败策略 · 重试上限 | 0 |
| `security_boundary` | ⑦ 安全边界 | 写 compensation_record 与 biz_status(compensated)；**不写 settled**（那是 payment.observe 的权威边界，guard 会抛）；**不宣布外部资金结果** —— 作废记录只表示 MAOS 侧不再推进，最后一次观察到的下落原样留档交人工对账；已 settled 的案子拒绝补偿，不静默跳过 |
| `reuse_note` | ⑧ 复用说明 | 任何「外部已经收到请求、但本地要收口」的域都该照此写：先留档最后一次观察、再开人工工单、最后才推进本地状态；三步顺序不可换 |
| `owner_roles` | ⑨ 归属角色 | `refund_payment` |

### refund.intake @ 1.0.0

实现：`RefundIntakeSkill` @ `maos/skills/builtin/refund/intake.py:59`

| 要素 | 含义 | 值 |
| :-- | :-- | :-- |
| `purpose` | ① 用途 | 聚合多源退款诉求与证据，去重后建 refund_case 并挂上证据引用 |
| `input_schema` | ② 输入 | `signals`: list[dict]（工单 / 客服记录 / 客户上传，形状同 issue.aggregate 的 findings）<br>`case_seed`: dict{tenant_id,case_id,channel_id,order_id,order_version,sku,reason_code,amount_claimed} |
| `output_schema` | ③ 输出 | `case_draft`: dict（refund_case 那一行，biz_status=submitted）<br>`evidence_refs`: list[dict{evidence_id,kind,uri,digest,source}]<br>`issues`: list[dict]（issue.aggregate 的去重结果）<br>`dedup`: dict{signals:int,issues:int,merged:int}<br>`invocation_id`: str（本次写入的 actor 锚点） |
| `preconditions` | ④ 前置条件 | `signals`、`case_seed` |
| `depends_tools` | ⑤ 依赖工具 | （空） |
| `failure_policy` | ⑥ 失败策略 | escalate |
| `max_retries` | ⑥ 失败策略 · 重试上限 | 0 |
| `security_boundary` | ⑦ 安全边界 | 只写 refund_case（经 guard.create_case）/ customer_evidence / business_ref；不调模型、不碰支付网关；去重经 SkillInvoker 复用 issue.aggregate，调用方 identity 必须同时授予该 skill，否则 PermissionDenied |
| `reuse_note` | ⑧ 复用说明 | 任何业务域要把多源诉求收成一个案子都可照此复用 issue.aggregate，不另写去重 |
| `owner_roles` | ⑨ 归属角色 | `refund_intake` |

### req.normalize @ 1.0.0

实现：`ReqNormalizeSkill` @ `maos/skills/builtin/req_normalize.py:46`

| 要素 | 含义 | 值 |
| :-- | :-- | :-- |
| `purpose` | ① 用途 | 把自然语言目标归一成可执行、可验收的结构化需求 |
| `input_schema` | ② 输入 | `goal`: str<br>`context`: dict? |
| `output_schema` | ③ 输出 | `normalized_goal`: str<br>`constraints`: list[str]<br>`acceptance_suggestions`: list[str] |
| `preconditions` | ④ 前置条件 | `goal` |
| `depends_tools` | ⑤ 依赖工具 | （空） |
| `failure_policy` | ⑥ 失败策略 | retry |
| `max_retries` | ⑥ 失败策略 · 重试上限 | 1 |
| `security_boundary` | ⑦ 安全边界 | 只读入参，不写任何资源、不调用任何工具；context 原样透传给模型，不落盘 |
| `reuse_note` | ⑧ 复用说明 | Manager 规划前的统一入口；任何角色要澄清目标都复用它，不要各写一份归一逻辑 |
| `owner_roles` | ⑨ 归属角色 | `manager` |

### test.verify @ 1.0.0

实现：`TestVerifySkill` @ `maos/skills/builtin/test_verify.py:30`

| 要素 | 含义 | 值 |
| :-- | :-- | :-- |
| `purpose` | ① 用途 | 在容器沙箱里跑 workdir 的测试，产出结构化 test_report |
| `input_schema` | ② 输入 | `workdir`: str |
| `output_schema` | ③ 输出 | `passed`: int<br>`failed`: int<br>`errors`: int<br>`cases`: list[{id:str,status:str,msg:str}]<br>`duration`: float<br>`tool_error`: str \| None |
| `preconditions` | ④ 前置条件 | `workdir` |
| `depends_tools` | ⑤ 依赖工具 | `sandbox` |
| `failure_policy` | ⑥ 失败策略 | escalate |
| `max_retries` | ⑥ 失败策略 · 重试上限 | 0 |
| `security_boundary` | ⑦ 安全边界 | 不自己执行任何东西，一律经 sandbox.pytest_run 这个 ToolPort：主路径容器（断网、只读、非 root、内存/CPU/进程数限额、不继承宿主 env），降级路径裸 subprocess 但 env 按白名单重建。skill 自身不落盘、不改 workdir |
| `reuse_note` | ⑧ 复用说明 | Testing 角色唯一的测试执行入口；Gate 判代码类任务读的就是它的产物 |
| `owner_roles` | ⑨ 归属角色 | `testing` |

## 版本 / 发布 / 回滚 / 质量评估

**注册表按 `dict[name][version]` 保留历史版本**（maos/skills/registry.py:16），这是发布与回滚叙事的代码依据，不是一句设想：

- **发布**：`@register_skill`（maos/skills/registry.py:33）按 `contract.name` / `.version` 入表。投放一个新模块即注册，`maos/skills/builtin/__init__.py` 一个字都不用改 —— 多轨并行时不会撞同一处清单。
- **取版**：`get(name, version=None)`（maos/skills/registry.py:69）缺省返回**最高版本**，按段数值比大小（`_semver_key`，maos/skills/registry.py:24），所以 `1.10.0 > 1.9.0` 而不是字符串序。
- **回滚**：旧版本从不被覆盖，`get(name, "1.0.0")` 永远拿得到当年那一个。在册版本用 `versions(name)` 列（maos/skills/registry.py:84）。升级期间在跑的旧 Plan 因此行为可复现 —— 这是保留历史版本的**唯一**理由。
- **质量评估**：每次调用落一条 `SkillInvoked`，`detail` 带 `status` / `duration_ms` / `input_digest` / `output_hash` / `usage`；按 `skill + version` 聚合 event_log 即可得到成功率与耗时分布，无需另建埋点。证据侧由 `scripts/verify.py` 第 1 项做哈希一致性重放。

当前在册的 13 个 skill 中，有多版本的：**一个都没有** —— 各只有 1 个版本，回滚路径尚未在演示链路上被真实用过。机制本身有单测守着：`maos/tests/test_skills.py:76` 断言同名三版共存时 `versions()` 返回 `["1.0.0", "1.9.0", "1.10.0"]`（按数值序，非字符串序）。
