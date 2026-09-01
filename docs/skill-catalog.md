# Skill 目录

<!-- 本文件由 scripts/gen_docs.py 从运行时代码生成，**请勿手改**。
     改了代码就重跑 `python3 scripts/gen_docs.py`；
     `python3 scripts/gen_docs.py --check` 不一致即非零退出。 -->

注册表里共 **30 个 skill / 30 个版本条目**。契约共 12 个字段（maos/skills/contract.py:19）：`name + version` 是注册表主键，其余 10 个字段合成 **9 项要素**（`failure_policy` 与 `max_retries` 同属「失败策略」一项）。字段与顺序取自 `dataclasses.fields(SkillContract)`，本文件不另抄。

失败策略取值域冻结为 `retry`、`fallback`、`escalate`（maos/skills/contract.py:16）。

调用一律走 `SkillInvoker.invoke()`（maos/skills/invoker.py:50）：先校验 `name ∈ identity.allowed_skills`，越权抛 `PermissionDenied`；未注册返回 `failed:skill_not_found:<name>` 而不抛；成败都落一条 `SkillInvoked` event_log 行（`detail` 带 `input_digest` / `output_hash`，`scripts/verify.py` 第 1 项据此校验证据未被篡改）。

## 一览

| skill | 版本 | 域 | 归属角色 | 失败策略 | 依赖工具 | 声明位置 |
| :-- | :-- | :-- | :-- | :-- | :-- | :-- |
| `ap.compensate` | `1.0.0` | 软件交付域 | `ap_compensation` | escalate | （空） | `maos/skills/builtin/ap/compensate.py:65` |
| `ap.execute` | `1.0.0` | 软件交付域 | `ap_treasury` | escalate | `bank.pay` | `maos/skills/builtin/ap/execute.py:42` |
| `ap.intake` | `1.0.0` | 软件交付域 | `ap_intake` | escalate | （空） | `maos/skills/builtin/ap/intake.py:37` |
| `ap.match` | `1.0.0` | 软件交付域 | `ap_match` | escalate | （空） | `maos/skills/builtin/ap/match.py:96` |
| `ap.observe` | `1.0.0` | 软件交付域 | `ap_treasury` | escalate | `bank.query` | `maos/skills/builtin/ap/observe.py:50` |
| `ap.plan-payment` | `1.0.0` | 软件交付域 | `ap_control` | escalate | （空） | `maos/skills/builtin/ap/plan_payment.py:34` |
| `claim.adjudicate` | `1.0.0` | 软件交付域 | `claim_adjudicator` | escalate | （空） | `maos/skills/builtin/claim/adjudicate.py:74` |
| `claim.compensate` | `1.0.0` | 软件交付域 | `claim_payment` | escalate | （空） | `maos/skills/builtin/claim/compensate.py:63` |
| `claim.intake` | `1.0.0` | 软件交付域 | `claim_intake` | escalate | （空） | `maos/skills/builtin/claim/intake.py:87` |
| `claim.observe` | `1.0.0` | 软件交付域 | `claim_payment` | escalate | `payer.query` | `maos/skills/builtin/claim/observe.py:58` |
| `claim.pay` | `1.0.0` | 软件交付域 | `claim_payment` | escalate | `payer.submit` | `maos/skills/builtin/claim/pay.py:35` |
| `claim.settle` | `1.0.0` | 软件交付域 | `claim_settlement` | escalate | （空） | `maos/skills/builtin/claim/settle.py:62` |
| `code.repo-patch` | `1.0.0` | 软件交付域 | `coding` | escalate | `git-mcp`、`sandbox` | `maos/skills/builtin/code_repo_patch.py:151` |
| `finance.settle` | `1.0.0` | 制造售后退款域 | `refund_finance` | escalate | （空） | `maos/skills/builtin/refund/finance.py:55` |
| `investigation.cancel` | `1.0.0` | 软件交付域 | `investigation_cancel` | escalate | `clearing.cancel` | `maos/skills/builtin/investigation/cancel.py:43` |
| `investigation.classify` | `1.0.0` | 软件交付域 | `investigation_classify` | escalate | （空） | `maos/skills/builtin/investigation/classify.py:67` |
| `investigation.compensate` | `1.0.0` | 软件交付域 | `investigation_observe` | escalate | （空） | `maos/skills/builtin/investigation/compensate.py:67` |
| `investigation.file` | `1.0.0` | 软件交付域 | `investigation_intake` | escalate | （空） | `maos/skills/builtin/investigation/intake.py:28` |
| `investigation.observe` | `1.0.0` | 软件交付域 | `investigation_observe` | escalate | `clearing.resolution` | `maos/skills/builtin/investigation/observe.py:76` |
| `issue.aggregate` | `1.0.0` | 软件交付域 | `manager` | escalate | （空） | `maos/skills/builtin/issue_aggregate.py:66` |
| `kb.retrieve` | `1.1.0` | 软件交付域 | `manager`、`coding` | escalate | （空） | `maos/skills/builtin/kb_retrieve.py:88` |
| `kb.sink` | `1.0.0` | 软件交付域 | `manager` | escalate | （空） | `maos/skills/builtin/kb_sink.py:29` |
| `notify.customer` | `1.0.0` | 制造售后退款域 | `refund_intake` | retry（≤2 次） | （空） | `maos/skills/builtin/refund/notify.py:22` |
| `payment.execute` | `1.0.0` | 制造售后退款域 | `refund_payment` | escalate | `gateway.refund` | `maos/skills/builtin/refund/payment_execute.py:34` |
| `payment.observe` | `1.0.0` | 制造售后退款域 | `refund_payment` | escalate | `gateway.query` | `maos/skills/builtin/refund/payment_observe.py:36` |
| `policy.match` | `1.0.0` | 制造售后退款域 | `refund_policy` | escalate | （空） | `maos/skills/builtin/refund/policy.py:62` |
| `refund.compensate` | `1.0.0` | 制造售后退款域 | `refund_payment` | escalate | （空） | `maos/skills/builtin/refund/compensate.py:65` |
| `refund.intake` | `1.0.0` | 制造售后退款域 | `refund_intake` | escalate | （空） | `maos/skills/builtin/refund/intake.py:59` |
| `req.normalize` | `1.0.0` | 软件交付域 | `manager` | retry（≤1 次） | （空） | `maos/skills/builtin/req_normalize.py:51` |
| `test.verify` | `1.0.0` | 软件交付域 | `testing` | escalate | `sandbox` | `maos/skills/builtin/test_verify.py:30` |

## 逐个 skill × 九要素

### ap.compensate @ 1.0.0

实现：`ApCompensateSkill` @ `maos/skills/builtin/ap/compensate.py:65`

| 要素 | 含义 | 值 |
| :-- | :-- | :-- |
| `purpose` | ① 用途 | 付款被驳回或问不出回单后的域内补偿收口：作废付款指令、写补偿记录与对账工单，把案子推进到 compensated |
| `input_schema` | ② 输入 | `tenant_id`: str<br>`case_id`: str<br>`operator`: str（做出驳回/收口决定的人）<br>`reason`: str（为什么走补偿，原样进补偿记录与事件）<br>`assignee`: str（可选，对账工单的接单人，缺省同 operator） |
| `output_schema` | ③ 输出 | `biz_status`: compensated<br>`revoked`: list[dict]（作废的付款指令；语义是「不再推进」，**不是**「确认未付」）<br>`last_observed_state`: str（最后一次观察到的下落；没观察到就是 unobserved）<br>`ticket`: dict（对账工单）<br>`invocation_id`: str |
| `preconditions` | ④ 前置条件 | `tenant_id`、`case_id`、`operator` |
| `depends_tools` | ⑤ 依赖工具 | （空） |
| `failure_policy` | ⑥ 失败策略 | escalate |
| `max_retries` | ⑥ 失败策略 · 重试上限 | 0 |
| `security_boundary` | ⑦ 安全边界 | 不碰银行、不写 ap_payment_observation（那是 ap.observe 的专属面）；biz_status 一律经 guard.update_biz_status，写不出 settled。已经 settled 的案子拒绝补偿 —— 那是数据被改坏的信号，不许静默吞掉 |
| `reuse_note` | ⑧ 复用说明 | 任何有外部不可逆动作的域都该有自己的补偿：作废本地意图 + 留下最后观察 + 开一张给人的工单，三件缺一不可 |
| `owner_roles` | ⑨ 归属角色 | `ap_compensation` |

### ap.execute @ 1.0.0

实现：`ApExecuteSkill` @ `maos/skills/builtin/ap/execute.py:42`

| 要素 | 含义 | 值 |
| :-- | :-- | :-- |
| `purpose` | ① 用途 | 核对匹配与审批后向银行发出付款指令，落 payment_instruction 并把业务状态推到 payment_requested；**写不出 settled** |
| `input_schema` | ② 输入 | `tenant_id`: str<br>`case_id`: str<br>`bank`: str（已 register_bank 的名字，缺省 'demo'）<br>`amount`: str（可选，缺省取最近一轮匹配算出的应付额） |
| `output_schema` | ③ 输出 | `instruction_id`: str（银行侧指令 id，ap.observe 用它）<br>`idempotency_key`: str<br>`amount`: str<br>`bank_advice`: dict（受理回单，**永远不是终态**）<br>`biz_status`: payment_requested<br>`invocation_id`: str |
| `preconditions` | ④ 前置条件 | `tenant_id`、`case_id` |
| `depends_tools` | ⑤ 依赖工具 | `bank.pay` |
| `failure_policy` | ⑥ 失败策略 | escalate |
| `max_retries` | ⑥ 失败策略 · 重试上限 | 0 |
| `security_boundary` | ⑦ 安全边界 | 三道前置：匹配已通过（biz_status=matched）、有一条 approved 的人工审批、幂等键由 (tenant, case) 唯一确定。审批记录只读不写。本 skill 不是 guard.AUTHORITATIVE_WRITER —— 试图写 settled 会被 guard 抛 AuthoritativeFactViolation 并落一条事件。银行调用一律经 invoke_tool 留审计行 |
| `reuse_note` | ⑧ 复用说明 | 任何「发出去 ≠ 成功了」的域都该照此拆两步：执行一步、观察一步，终态只有观察那一步写得进 |
| `owner_roles` | ⑨ 归属角色 | `ap_treasury` |

### ap.intake @ 1.0.0

实现：`ApIntakeSkill` @ `maos/skills/builtin/ap/intake.py:37`

| 要素 | 含义 | 值 |
| :-- | :-- | :-- |
| `purpose` | ① 用途 | 收供应商发票，确认三单齐备并建出 ap_case（received），把 Task 挂到业务对象上 |
| `input_schema` | ② 输入 | `tenant_id`: str<br>`case_id`: str<br>`invoice_id`: str（发票池里那一张）<br>`po_id`: str（采购订单号）<br>`po_version`: int（订单快照版本 —— 权威在 ERP，我们存的是读到的那一版）<br>`gr_id`: str（收货单号） |
| `output_schema` | ③ 输出 | `case`: dict（ap_case 当前那一行）<br>`invoice`: dict（发票抬头，含 UNCL1001 类型码与其官方名称）<br>`three_way`: dict（三单齐备情况：各自的行数）<br>`refs`: list[dict]（挂上去的 ap_business_ref）<br>`invocation_id`: str |
| `preconditions` | ④ 前置条件 | `tenant_id`、`case_id`、`invoice_id`、`po_id`、`gr_id` |
| `depends_tools` | ⑤ 依赖工具 | （空） |
| `failure_policy` | ⑥ 失败策略 | escalate |
| `max_retries` | ⑥ 失败策略 · 重试上限 | 0 |
| `security_boundary` | ⑦ 安全边界 | 只建案不判定；biz_status 一律由 guard.create_case 落成 received，调用方指定不了。三单读取一律经 domain/ap/objects.py 的具名读取函数，本 skill 不自己写 SQL |
| `reuse_note` | ⑧ 复用说明 | 任何「先确认外部单据齐备、再建本地案子」的域都该照此分层：齐不齐是可重试的失败，对不对是要人看的结论 |
| `owner_roles` | ⑨ 归属角色 | `ap_intake` |

### ap.match @ 1.0.0

实现：`ApMatchSkill` @ `maos/skills/builtin/ap/match.py:96`

| 要素 | 含义 | 值 |
| :-- | :-- | :-- |
| `purpose` | ① 用途 | 三单匹配：逐行比数量与单价（各自容差），再按 Peppol/EN16931 规则验勾稽，产出可核对的拒付理由与应付金额 |
| `input_schema` | ② 输入 | `tenant_id`: str<br>`case_id`: str<br>`attempt`: int（可选，落 match_result 的主键之一，缺省 1）<br>`tolerance`: dict（可选，覆盖 quantity/unit_price/tax 三个容差） |
| `output_schema` | ③ 输出 | `matched`: bool（匹配通过与否 —— 不通过**不是**执行失败）<br>`payable_amount`: str（通过时按 BR-CO-16 算出的应付额；不通过为空串）<br>`findings`: list[dict]（每条带 rule_id / text / source，可核对）<br>`checked`: list[str]（本次跑过的判据编号 —— 证明没判的和判过的分得开）<br>`tolerance`: dict（本次实际用的三个容差）<br>`biz_status`: str（通过则推进到 matched，否则原样不动）<br>`invocation_id`: str |
| `preconditions` | ④ 前置条件 | `tenant_id`、`case_id` |
| `depends_tools` | ⑤ 依赖工具 | （空） |
| `failure_policy` | ⑥ 失败策略 | escalate |
| `max_retries` | ⑥ 失败策略 · 重试上限 | 0 |
| `security_boundary` | ⑦ 安全边界 | 只读三单、只写 match_result 与（通过时）ap_case.biz_status；biz_status 一律经 guard.update_biz_status，写不出 settled。拒付理由的 rule_id 必须来自 maos/tools/ap_codes.py 的已核对清单，自造编号在 ap_codes.require_rule 里当场抛 |
| `reuse_note` | ⑧ 复用说明 | 任何「拿外部单据互相勾稽」的域都该照此写：判据挂外部规范编号，容差按量纲分别给，等式类判据零容差 |
| `owner_roles` | ⑨ 归属角色 | `ap_match` |

### ap.observe @ 1.0.0

实现：`ApObserveSkill` @ `maos/skills/builtin/ap/observe.py:50`

| 要素 | 含义 | 值 |
| :-- | :-- | :-- |
| `purpose` | ① 用途 | 轮询银行取得付款终态回单，写 ap_payment_observation 并（仅在此处）写 settled |
| `input_schema` | ② 输入 | `tenant_id`: str<br>`case_id`: str<br>`bank`: str（已 register_bank 的名字，缺省 'demo'）<br>`instruction_id`: str（可选，缺省取该案子最近一笔未作废的付款指令）<br>`max_polls`: int（可选，默认 5） |
| `output_schema` | ③ 输出 | `bank_advice`: dict（终态回单，或到顶时的最后一次观察）<br>`observed_state`: accepted\|pending\|unknown\|settled\|failed<br>`poll_count`: int（问了几次 —— 终态是问出来的证据）<br>`bank_reference`: str（仅 settled 才有：可对账的银行流水号）<br>`biz_status`: str（settled 只可能由本 skill 写入）<br>`settled`: bool<br>`needs_compensation`: bool（银行明确失败时为 True）<br>`invocation_id`: str |
| `preconditions` | ④ 前置条件 | `tenant_id`、`case_id` |
| `depends_tools` | ⑤ 依赖工具 | `bank.query` |
| `failure_policy` | ⑥ 失败策略 | escalate |
| `max_retries` | ⑥ 失败策略 · 重试上限 | 0 |
| `security_boundary` | ⑦ 安全边界 | 本 skill 是 maos/domain/ap/guard.py 的 AUTHORITATIVE_WRITER —— 全系统唯一可写 settled 的 actor，且写入必须同事务附带银行流水号的回单，缺字段由 guard 抛 AuthoritativeFactViolation；非终态一律不推进状态；银行调用一律经 invoke_tool 留审计行 |
| `reuse_note` | ⑧ 复用说明 | 任何「权威在外部系统」的终态都该照此写：先观察、再落库，两件事同一个事务；轮询到顶不许改判成失败 |
| `owner_roles` | ⑨ 归属角色 | `ap_treasury` |

### ap.plan-payment @ 1.0.0

实现：`ApPlanPaymentSkill` @ `maos/skills/builtin/ap/plan_payment.py:34`

| 要素 | 含义 | 值 |
| :-- | :-- | :-- |
| `purpose` | ① 用途 | 按三单匹配的结论出一份可核对的付款计划（金额/付款方式/到期日/税种分解），供 effect_risk=H 的人工审批 |
| `input_schema` | ② 输入 | `tenant_id`: str<br>`case_id`: str<br>`attempt`: int（可选，取哪一轮的匹配结论，缺省取最近一轮） |
| `output_schema` | ③ 输出 | `plan`: dict（付款计划：amount / currency / payment_means_code / due_at）<br>`payable_amount`: str（取 ap.match 算出来的那个，不取发票自称的）<br>`citations`: list[dict]（金额与码表各自的规范引用）<br>`needs_human_approval`: bool（恒 True —— 出账是不可逆动作）<br>`biz_status`: str<br>`invocation_id`: str |
| `preconditions` | ④ 前置条件 | `tenant_id`、`case_id` |
| `depends_tools` | ⑤ 依赖工具 | （空） |
| `failure_policy` | ⑥ 失败策略 | escalate |
| `max_retries` | ⑥ 失败策略 · 重试上限 | 0 |
| `security_boundary` | ⑦ 安全边界 | 只读不写业务状态：本 skill 不推进 biz_status，也不碰银行。匹配没通过一律拒绝出计划 —— 未经验证的金额不许进入审批视野。付款方式码不在 UNCL4461 内当场抛（BR-CL-16） |
| `reuse_note` | ⑧ 复用说明 | 任何「人要批一份计划而不是一个按钮」的域都该照此分步：把审批对象做成可核对的清单，而不是一次确认 |
| `owner_roles` | ⑨ 归属角色 | `ap_control` |

### claim.adjudicate @ 1.0.0

实现：`ClaimAdjudicateSkill` @ `maos/skills/builtin/claim/adjudicate.py:74`

| 要素 | 含义 | 值 |
| :-- | :-- | :-- |
| `purpose` | ① 用途 | 按保单快照锁定的条款版本检索适用条款并裁定赔付责任（零模型，可复现）；产出带 rule_no + terms_version 的 adjudication 行 |
| `input_schema` | ② 输入 | `tenant_id`: str<br>`claim_id`: str<br>`rule_prefix`: str（可选，默认 'CL-'） |
| `output_schema` | ③ 输出 | `terms_version`: int（保单锁定的条款版本，**不是**当前最新版本）<br>`policy_version`: int（保单快照版本）<br>`matched_rules`: list[dict{rule_no,version,title,params}]<br>`rule_refs`: list[str]（形如 CL-01@v1）<br>`primary_rule`: str（写进 adjudication.rule_no 那一条）<br>`exclusions`: list[str]（命中的除外责任条款，非空即拒赔）<br>`decision`: approve\|reject<br>`reason`: str<br>`invocation_id`: str |
| `preconditions` | ④ 前置条件 | `tenant_id`、`claim_id` |
| `depends_tools` | ⑤ 依赖工具 | （空） |
| `failure_policy` | ⑥ 失败策略 | escalate |
| `max_retries` | ⑥ 失败策略 · 重试上限 | 0 |
| `security_boundary` | ⑦ 安全边界 | 只读 claim_case / policy_contract / policy_terms，写 adjudication 与 claim_business_ref，并把 biz_status 推进到 adjudicated / rejected；**无权写 paid** —— guard 会抛 AuthoritativeFactViolation；不调模型、不碰赔付方；条款版本一律取自保单快照的 terms_version_at_bind，禁止使用 policy_terms 的最新版本 |
| `reuse_note` | ⑧ 复用说明 | 任何「按快照锁定的版本判定」的场景都可照此复用 objects.terms_at_bind |
| `owner_roles` | ⑨ 归属角色 | `claim_adjudicator` |

### claim.compensate @ 1.0.0

实现：`ClaimCompensateSkill` @ `maos/skills/builtin/claim/compensate.py:63`

| 要素 | 含义 | 值 |
| :-- | :-- | :-- |
| `purpose` | ① 用途 | 赔付被拒或走不通后的域内补偿收口：作废赔付指令、写补偿记录与人工工单，把案子推进到 compensated |
| `input_schema` | ② 输入 | `tenant_id`: str<br>`claim_id`: str<br>`operator`: str（做出驳回/收口决定的人）<br>`reason`: str（为什么走补偿，原样进补偿记录与事件）<br>`assignee`: str（可选，人工工单的接单人，缺省同 operator） |
| `output_schema` | ③ 输出 | `biz_status`: compensated<br>`revoked`: list[dict]（每条作废的赔付指令及其最后观察到的下落）<br>`ticket`: dict（人工工单：单号、接单人、要人去做什么）<br>`records`: int（落进 claim_compensation 的行数）<br>`last_observed_state`: str（paid\|denied\|processing\|unknown\|unobserved）<br>`last_carc`: str（最后一次观察到的 CARC，没观察到就是空）<br>`invocation_id`: str |
| `preconditions` | ④ 前置条件 | `tenant_id`、`claim_id`、`operator`、`reason` |
| `depends_tools` | ⑤ 依赖工具 | （空） |
| `failure_policy` | ⑥ 失败策略 | escalate |
| `max_retries` | ⑥ 失败策略 · 重试上限 | 0 |
| `security_boundary` | ⑦ 安全边界 | 写 claim_compensation 与 biz_status(compensated)；**不写 paid**（那是 claim.observe 的权威边界，guard 会抛）；**不宣布外部资金结果** —— 作废记录只表示 MAOS 侧不再推进，最后一次观察到的下落原样留档交人工对账；已 paid 的案子拒绝补偿，不静默跳过 |
| `reuse_note` | ⑧ 复用说明 | 任何「外部已经收到指令、但本地要收口」的域都该照此写：先留档最后一次观察、再开人工工单、最后才推进本地状态；三步顺序不可换 |
| `owner_roles` | ⑨ 归属角色 | `claim_payment` |

### claim.intake @ 1.0.0

实现：`ClaimIntakeSkill` @ `maos/skills/builtin/claim/intake.py:87`

| 要素 | 含义 | 值 |
| :-- | :-- | :-- |
| `purpose` | ① 用途 | 聚合三源报案信号与证据，去重后建 claim_case 并挂上证据与赔付明细行 |
| `input_schema` | ② 输入 | `signals`: list[dict]（工单 / 客服记录 / 定损照片，形状同 issue.aggregate 的 findings）<br>`case_seed`: dict{tenant_id,claim_id,payer_id,policy_no,policy_version,loss_type,incident_at,amount_claimed}<br>`claim_lines`: list[dict{line_no,item_code,description,amount_claimed}]（可选）<br>`reported_at`: str（可选，报案时点；缺省取当前时刻并就此定死） |
| `output_schema` | ③ 输出 | `case_draft`: dict（claim_case 那一行，biz_status=submitted）<br>`evidence_refs`: list[dict{evidence_id,kind,uri,digest,source}]<br>`claim_lines`: list[dict]（落进 claim_line 的明细行）<br>`issues`: list[dict]（issue.aggregate 的去重结果）<br>`dedup`: dict{signals:int,issues:int,merged:int}<br>`invocation_id`: str（本次写入的 actor 锚点） |
| `preconditions` | ④ 前置条件 | `signals`、`case_seed` |
| `depends_tools` | ⑤ 依赖工具 | （空） |
| `failure_policy` | ⑥ 失败策略 | escalate |
| `max_retries` | ⑥ 失败策略 · 重试上限 | 0 |
| `security_boundary` | ⑦ 安全边界 | 只写 claim_case（经 guard.create_case）/ claim_evidence / claim_line / claim_business_ref；不调模型、不碰赔付方；去重经 SkillInvoker 复用 issue.aggregate，调用方 identity 必须同时授予该 skill，否则 PermissionDenied |
| `reuse_note` | ⑧ 复用说明 | 任何业务域要把多源诉求收成一个案子都可照此复用 issue.aggregate，不另写去重 |
| `owner_roles` | ⑨ 归属角色 | `claim_intake` |

### claim.observe @ 1.0.0

实现：`ClaimObserveSkill` @ `maos/skills/builtin/claim/observe.py:58`

| 要素 | 含义 | 值 |
| :-- | :-- | :-- |
| `purpose` | ① 用途 | 轮询赔付方取得终态回执，写 claim_payment_observation 并（仅在此处）写 paid |
| `input_schema` | ② 输入 | `tenant_id`: str<br>`claim_id`: str<br>`payer`: str（已 register_payer 的名字，默认 'demo'）<br>`request_id`: str（可选，缺省取该案子最近一笔 claim_payment_request）<br>`max_polls`: int（可选，默认 5） |
| `output_schema` | ③ 输出 | `payer_receipt`: dict（终态回执，或到顶时的最后一次观察）<br>`observed_state`: paid\|denied\|processing\|unknown<br>`poll_count`: int（问了几次 —— 终态是问出来的证据）<br>`biz_status`: str（paid 只可能由本 skill 写入）<br>`paid`: bool<br>`needs_compensation`: bool（赔付方明确拒付、或轮询到顶仍问不出终态时为 True）<br>`carc_code`: str（拒付时的 X12 CARC，到账时为空）<br>`invocation_id`: str |
| `preconditions` | ④ 前置条件 | `tenant_id`、`claim_id` |
| `depends_tools` | ⑤ 依赖工具 | `payer.query` |
| `failure_policy` | ⑥ 失败策略 | escalate |
| `max_retries` | ⑥ 失败策略 · 重试上限 | 0 |
| `security_boundary` | ⑦ 安全边界 | 本 skill 是 guard.AUTHORITATIVE_WRITER —— 全系统唯一可写 paid 的 actor，且写入必须同事务附回执，缺字段或回执说的不是 paid 由 guard 抛 AuthoritativeFactViolation；非终态一律不推进状态、不落观察行；拒付只落观察行不推状态（收口归 claim.compensate）；赔付方调用一律经 invoke_tool 留审计行 |
| `reuse_note` | ⑧ 复用说明 | 任何「权威在外部系统」的终态都该照此写：先观察、再落库，两件事同一个事务 |
| `owner_roles` | ⑨ 归属角色 | `claim_payment` |

### claim.pay @ 1.0.0

实现：`ClaimPaySkill` @ `maos/skills/builtin/claim/pay.py:35`

| 要素 | 含义 | 值 |
| :-- | :-- | :-- |
| `purpose` | ① 用途 | 核对审批后向赔付方发起赔付指令，写 claim_payment_request 并推进到 payment_requested |
| `input_schema` | ② 输入 | `tenant_id`: str<br>`claim_id`: str<br>`payer`: str（已 register_payer 的名字，默认 'demo'）<br>`payee`: str（可选，收款方；进幂等比对面） |
| `output_schema` | ③ 输出 | `payer_receipt`: dict（赔付方回执，**非 paid**）<br>`request_id`: str（claim.observe 用它去 query）<br>`idempotency_key`: str<br>`amount`: str<br>`biz_status`: payment_requested —— **永远不是 paid**<br>`needs_query`: bool（恒 True：到账只能问出来）<br>`invocation_id`: str |
| `preconditions` | ④ 前置条件 | `tenant_id`、`claim_id` |
| `depends_tools` | ⑤ 依赖工具 | `payer.submit` |
| `failure_policy` | ⑥ 失败策略 | escalate |
| `max_retries` | ⑥ 失败策略 · 重试上限 | 0 |
| `security_boundary` | ⑦ 安全边界 | 写 claim_payment_request 与 biz_status(payment_requested)；**无权写 paid** —— guard 会抛 AuthoritativeFactViolation；发起前必须存在 approved 的 claim_approval，本 skill 只读不写审批记录；赔付方调用一律经 invoke_tool，留 ToolInvoked 审计行；回执挂在 payer_receipt 键上，不占用 receipt（那个键归第七道闸的支付宝码表） |
| `reuse_note` | ⑧ 复用说明 | 发起与观察分离：本 skill 只产生指令，到账一律由 claim.observe 观察得到 |
| `owner_roles` | ⑨ 归属角色 | `claim_payment` |

### claim.settle @ 1.0.0

实现：`ClaimSettleSkill` @ `maos/skills/builtin/claim/settle.py:62`

| 要素 | 含义 | 值 |
| :-- | :-- | :-- |
| `purpose` | ① 用途 | 按裁定命中的条款逐行核算赔款，写 claim_line.amount_allowed 与 adjudication.allowed_amount，并产出同一份数据的 settlement 产物 |
| `input_schema` | ② 输入 | `tenant_id`: str<br>`claim_id`: str<br>`adjudication`: dict（claim.adjudicate 的出参：decision / matched_rules / rule_refs） |
| `output_schema` | ③ 输出 | `settlement`: dict（= 落库那几行的同一份数据）<br>`allowed_amount`: str（最终赔付额，字符串不进浮点）<br>`lines`: list[dict{line_no,item_code,amount_claimed,amount_allowed}]<br>`breakdown`: dict（三层扣减的算式，金额为字符串）<br>`rule_refs`: list[str]<br>`invocation_id`: str |
| `preconditions` | ④ 前置条件 | `tenant_id`、`claim_id`、`adjudication` |
| `depends_tools` | ⑤ 依赖工具 | （空） |
| `failure_policy` | ⑥ 失败策略 | escalate |
| `max_retries` | ⑥ 失败策略 · 重试上限 | 0 |
| `security_boundary` | ⑦ 安全边界 | 只读 claim_case / policy_contract / claim_line，写 claim_line.amount_allowed 与 adjudication.allowed_amount；**不改 biz_status**、**无权写 paid**；不调模型、不碰赔付方；赔款只按条款参数与保单快照计算，不接受调用方直接指定 allowed_amount |
| `reuse_note` | ⑧ 复用说明 | 产出的 settlement 与库表是同一份数据，两处不许各造一份 |
| `owner_roles` | ⑨ 归属角色 | `claim_settlement` |

### code.repo-patch @ 1.0.0

实现：`CodeRepoPatchSkill` @ `maos/skills/builtin/code_repo_patch.py:151`

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

### investigation.cancel @ 1.0.0

实现：`InvestigationCancelSkill` @ `maos/skills/builtin/investigation/cancel.py:43`

| 要素 | 含义 | 值 |
| :-- | :-- | :-- |
| `purpose` | ① 用途 | 核对人工调账审批后向清算方发出 camt.056 撤销请求，落 cancellation_sent |
| `input_schema` | ② 输入 | `tenant_id`: str<br>`case_id`: str<br>`clearing`: str（已 register_clearing 的名字，缺省 'demo'） |
| `output_schema` | ③ 输出 | `request_id`: str（清算方受理号）<br>`idempotency_key`: str（camt.056 的 Assgnmt/Id）<br>`message_type`: camt.056.001.08<br>`receipt`: dict（受理回执，**非终态**）<br>`biz_status`: cancellation_sent<br>`reason_code`: str（报文里填的撤销原因码）<br>`invocation_id`: str |
| `preconditions` | ④ 前置条件 | `tenant_id`、`case_id` |
| `depends_tools` | ⑤ 依赖工具 | `clearing.cancel` |
| `failure_policy` | ⑥ 失败策略 | escalate |
| `max_retries` | ⑥ 失败策略 · 重试上限 | 0 |
| `security_boundary` | ⑦ 安全边界 | 发出 camt.056 并写 cancellation_sent；**写不出 returned** —— 那是 investigation.observe 的权威边界，guard 会抛；发报文前必须读到一条 approved 的 adjustment_approval（人工调账的监管硬闸），本 skill 只读审批不写审批；幂等键由 (tenant, case) 定，重跑不会产生第二份 camt.056；清算方调用一律经 invoke_tool 留审计行 |
| `reuse_note` | ⑧ 复用说明 | 任何「对外发一份不可撤回的指令」的域都该照此写：先查人批、再发、只写『发出去了』不写『成功了』 |
| `owner_roles` | ⑨ 归属角色 | `investigation_cancel` |

### investigation.classify @ 1.0.0

实现：`InvestigationClassifySkill` @ `maos/skills/builtin/investigation/classify.py:67`

| 要素 | 含义 | 值 |
| :-- | :-- | :-- |
| `purpose` | ① 用途 | 给差错定性并选定官方撤销原因码，把案子推进到 classified |
| `input_schema` | ② 输入 | `tenant_id`: str<br>`case_id`: str<br>`classification`: str（duplicate_payment\|fraudulent\|requested_by_customer\|technical_error\|wrong_amount，缺省 duplicate_payment）<br>`reason_code`: str（可选，直接指定官方码，指定了就不走判据表）<br>`note`: str（可选，人话说明，进裁定结论） |
| `output_schema` | ③ 输出 | `biz_status`: classified<br>`classification`: str（定性类型）<br>`reason_code`: str（ExternalCancellationReason1Code 里的一条）<br>`rule_refs`: list[dict]（官方码 + 官方定义原文 + 出处 URL，逐条可核）<br>`invocation_id`: str |
| `preconditions` | ④ 前置条件 | `tenant_id`、`case_id` |
| `depends_tools` | ⑤ 依赖工具 | （空） |
| `failure_policy` | ⑥ 失败策略 | escalate |
| `max_retries` | ⑥ 失败策略 · 重试上限 | 0 |
| `security_boundary` | ⑦ 安全边界 | 只经 guard.set_classification 写 investigation_case 的原因码与 classified；**写不出 returned**（guard 会抛）；原因码一律经 investigation_codes 校验，未知码抛 UnknownCodeError 不兜底 ——编造的原因码会让发出的 camt.056 成为不合规报文 |
| `reuse_note` | ⑧ 复用说明 | 任何「判断结论要写进对外报文」的域都该照此写：判据表指向官方码，import 时校验码还在，结论带官方定义原文供核对 |
| `owner_roles` | ⑨ 归属角色 | `investigation_classify` |

### investigation.compensate @ 1.0.0

实现：`InvestigationCompensateSkill` @ `maos/skills/builtin/investigation/compensate.py:67`

| 要素 | 含义 | 值 |
| :-- | :-- | :-- |
| `purpose` | ① 用途 | 撤销走不通后的域内补偿收口：撤回 camt.056、写补偿记录与人工对账工单，把案子推进到 compensated |
| `input_schema` | ② 输入 | `tenant_id`: str<br>`case_id`: str<br>`operator`: str（做出驳回/收口决定的人）<br>`reason`: str（为什么走补偿，原样进补偿记录与事件）<br>`assignee`: str（可选，人工工单的接单人，缺省同 operator） |
| `output_schema` | ③ 输出 | `biz_status`: compensated<br>`withdrawn`: list[dict]（每份撤回的 camt.056 及其最后观察到的下落）<br>`ticket`: dict（人工对账工单：单号、接单人、要人去做什么）<br>`records`: int（落进 investigation_compensation 的行数）<br>`last_observed_state`: str（returned\|cancellation_confirmed\|rejected\|pending\|unobserved）<br>`invocation_id`: str |
| `preconditions` | ④ 前置条件 | `tenant_id`、`case_id`、`operator`、`reason` |
| `depends_tools` | ⑤ 依赖工具 | （空） |
| `failure_policy` | ⑥ 失败策略 | escalate |
| `max_retries` | ⑥ 失败策略 · 重试上限 | 0 |
| `security_boundary` | ⑦ 安全边界 | 写 investigation_compensation 与 biz_status(compensated)；**不写 returned**（那是 investigation.observe 的权威边界，guard 会抛）；**不宣布外部资金结果** —— 撤回记录只表示 MAOS 侧不再推进，最后一次观察到的下落原样留档交人工对账；已 returned 的案子拒绝补偿，不静默跳过 |
| `reuse_note` | ⑧ 复用说明 | 任何「外部已经收到指令、但本地要收口」的域都该照此写：先留档最后一次观察、再开人工工单、最后才推进本地状态；三步顺序不可换 |
| `owner_roles` | ⑨ 归属角色 | `investigation_observe` |

### investigation.file @ 1.0.0

实现：`InvestigationFileSkill` @ `maos/skills/builtin/investigation/intake.py:28`

| 要素 | 含义 | 值 |
| :-- | :-- | :-- |
| `purpose` | ① 用途 | 受理一件支付差错，核对原始支付快照后建 investigation_case（filed） |
| `input_schema` | ② 输入 | `tenant_id`: str<br>`case_id`: str（与清算方对话的案号，对应 camt.056 的 Case/Id）<br>`original_msg_id`: str（被质疑那笔支付的原报文号）<br>`original_version`: int（可选，缺省取该报文最新读到的那一版）<br>`creator_agent`: str（发起方 BIC）<br>`assignee_agent`: str（被指派方 BIC）<br>`claimed_amount`: float\|str（可选，递进来就与快照核对，对不上即抛） |
| `output_schema` | ③ 输出 | `case`: dict（建成或既有的那一行）<br>`biz_status`: filed<br>`snapshot`: dict（案子挂着的原始支付快照）<br>`idempotent_replay`: bool（True = 案号已在库且业务字段逐字段相同）<br>`invocation_id`: str |
| `preconditions` | ④ 前置条件 | `tenant_id`、`case_id`、`original_msg_id` |
| `depends_tools` | ⑤ 依赖工具 | （空） |
| `failure_policy` | ⑥ 失败策略 | escalate |
| `max_retries` | ⑥ 失败策略 · 重试上限 | 0 |
| `security_boundary` | ⑦ 安全边界 | 只经 guard.create_case 写 investigation_case，落 filed；**写不出 returned**（那是 investigation.observe 的权威边界，guard 会抛）；金额币种一律以 original_payment_snapshot 为准，调用方递的值只用于核对 |
| `reuse_note` | ⑧ 复用说明 | 任何「案件挂在一份外部快照上」的域都该照此写：先查快照、再核对、最后建案 |
| `owner_roles` | ⑨ 归属角色 | `investigation_intake` |

### investigation.observe @ 1.0.0

实现：`InvestigationObserveSkill` @ `maos/skills/builtin/investigation/observe.py:76`

| 要素 | 含义 | 值 |
| :-- | :-- | :-- |
| `purpose` | ① 用途 | 问询清算方取得决议与资金下落，写 resolution_observation 并（仅在此处、且仅凭 pacs.004）写 returned |
| `input_schema` | ② 输入 | `tenant_id`: str<br>`case_id`: str<br>`clearing`: str（已 register_clearing 的名字，缺省 'demo'）<br>`request_id`: str（可选，缺省取该案子最近一笔 cancellation_request）<br>`max_polls`: int（可选，默认 5） |
| `output_schema` | ③ 输出 | `receipt`: dict（终态回执，或到顶时的最后一次观察）<br>`observed_state`: returned\|cancellation_confirmed\|rejected\|pending<br>`poll_count`: int（问了几次 —— 结论是问出来的证据）<br>`biz_status`: str（returned 只可能由本 skill 写入）<br>`funds_returned`: bool（**只有 pacs.004 才为 True**）<br>`request_resolved`: bool（撤销请求有结论了吗；CNCL 时为 True）<br>`needs_compensation`: bool（明确被拒或问不出资金下落时为 True）<br>`invocation_id`: str |
| `preconditions` | ④ 前置条件 | `tenant_id`、`case_id` |
| `depends_tools` | ⑤ 依赖工具 | `clearing.resolution` |
| `failure_policy` | ⑥ 失败策略 | escalate |
| `max_retries` | ⑥ 失败策略 · 重试上限 | 0 |
| `security_boundary` | ⑦ 安全边界 | 本 skill 是 guard.AUTHORITATIVE_WRITER —— 全系统唯一可写 returned 的 actor，且写入必须同事务附一份 **pacs.004** 观察（带退回原因码与退回金额），缺任一条由 guard 抛 AuthoritativeFactViolation；camt.029 的肯定答复（CNCL）**写不进 returned** —— 它证明的是撤销指令已执行，不是资金已退回；非终态一律不推进状态；清算方调用一律经 invoke_tool 留审计行 |
| `reuse_note` | ⑧ 复用说明 | 任何「权威在外部系统、且肯定答复与业务成功不是一回事」的域都该照此写：先归一观察、再按证据类型分档、只有拿到那一类证据才收口 |
| `owner_roles` | ⑨ 归属角色 | `investigation_observe` |

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

实现：`KbRetrieveSkill` @ `maos/skills/builtin/kb_retrieve.py:88`

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

实现：`ReqNormalizeSkill` @ `maos/skills/builtin/req_normalize.py:51`

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

当前在册的 30 个 skill 中，有多版本的：**一个都没有** —— 各只有 1 个版本，回滚路径尚未在演示链路上被真实用过。机制本身有单测守着：`maos/tests/test_skills.py:76` 断言同名三版共存时 `versions()` 返回 `["1.0.0", "1.9.0", "1.10.0"]`（按数值序，非字符串序）。
