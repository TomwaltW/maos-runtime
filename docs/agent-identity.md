# Agent Identity 清单

<!-- 本文件由 scripts/gen_docs.py 从运行时代码生成，**请勿手改**。
     改了代码就重跑 `python3 scripts/gen_docs.py`；
     `python3 scripts/gen_docs.py --check` 不一致即非零退出。 -->

扫到 **11 个** 带 Identity 的 Agent 类，其中 **10 个**注册进 `AGENT_POOL`（可被 Worker 按 role 派单），**1 个**未注册（由流程层直接构造）。

分域：软件交付域 6 个；制造售后退款域 5 个。

**字段顺序即冻结契约附录 A 的声明顺序**，由 `dataclasses.fields(AgentIdentity)` 取（maos/agents/base.py:59）：`agent_id`、`role`、`duty`、`allowed_skills`、`allowed_tools`、`write_scope`、`max_risk`、`model_tier`、`max_self_repair`。本文件不另抄一份顺序。

Identity 不是文档，是运行时会被执行的约束：`BaseAgent.check_tool / check_risk / check_write`（maos/agents/base.py:103）在越权时抛 `PermissionDenied`；Skill 白名单由 `SkillInvoker` 在调用前校验。

## 一览

| 角色 role | agent_id | 域 | 进 AGENT_POOL | 声明位置 |
| :-- | :-- | :-- | :-- | :-- |
| `architecture` | `architecture` | 软件交付域 | 是 | `maos/agents/architecture.py:71` |
| `coding` | `coding` | 软件交付域 | 是 | `maos/agents/coding.py:26` |
| `manager` | `manager` | 软件交付域 | **否** | `maos/agents/manager.py:33` |
| `requirement` | `requirement` | 软件交付域 | 是 | `maos/agents/requirement.py:29` |
| `reviewer` | `reviewer` | 软件交付域 | 是 | `maos/agents/reviewer.py:33` |
| `testing` | `testing` | 软件交付域 | 是 | `maos/agents/testing.py:158` |
| `refund_channel` | `refund-channel` | 制造售后退款域 | 是 | `maos/agents/refund/channel_agent.py:40` |
| `refund_finance` | `refund-finance` | 制造售后退款域 | 是 | `maos/agents/refund/finance_agent.py:26` |
| `refund_intake` | `refund-intake` | 制造售后退款域 | 是 | `maos/agents/refund/intake_agent.py:26` |
| `refund_payment` | `refund-payment` | 制造售后退款域 | 是 | `maos/agents/refund/payment_agent.py:58` |
| `refund_policy` | `refund-policy` | 制造售后退款域 | 是 | `maos/agents/refund/policy_agent.py:22` |

> **未注册的 1 个（`manager`）不是漏网**：`AGENT_POOL` 的语义是「Worker 收到 TaskAssignment 后按 role 找得到的执行者」（maos/runtime/worker.py:28 一行构造全池）。Manager 是规划者不是执行者，由流程层直接构造并调 `plan()`，不接派单 —— 所以它有 Identity（白名单同样被 `SkillInvoker` 强制），但不进池。手册写「十角色」指的是包含它在内的角色总数。

## 软件交付域（6 个）

### architecture — ArchitectureAgent

声明位置：`maos/agents/architecture.py:71`

| 字段 | 含义 | 值 |
| :-- | :-- | :-- |
| `agent_id` | 实例 id | architecture |
| `role` | 角色名（派单按它路由） | architecture |
| `duty` | 职责边界 | 产出 API / 幂等 / 审计 / 可逆性四项俱全的架构契约，并拒绝不可逆的高风险自动执行 |
| `allowed_skills` | 可调 Skill 白名单 | （空） |
| `allowed_tools` | 可调工具白名单 | （空） |
| `write_scope` | 可写资源 | `artifact` |
| `max_risk` | 最高授权风险级 | M |
| `model_tier` | 模型档位 | strong |
| `max_self_repair` | 自修复上限 | 1 |

### coding — CodingAgent

声明位置：`maos/agents/coding.py:26`

| 字段 | 含义 | 值 |
| :-- | :-- | :-- |
| `agent_id` | 实例 id | coding |
| `role` | 角色名（派单按它路由） | coding |
| `duty` | 职责边界 | 按契约生成代码变更，以补丁集形式产出并完成本地自检 |
| `allowed_skills` | 可调 Skill 白名单 | `code.repo-patch`、`kb.retrieve` |
| `allowed_tools` | 可调工具白名单 | `git-mcp`、`sandbox` |
| `write_scope` | 可写资源 | `artifact`、`repo_branch` |
| `max_risk` | 最高授权风险级 | M |
| `model_tier` | 模型档位 | medium |
| `max_self_repair` | 自修复上限 | 2 |

### manager — ManagerAgent

声明位置：`maos/agents/manager.py:33`（**不进 `AGENT_POOL`**）

| 字段 | 含义 | 值 |
| :-- | :-- | :-- |
| `agent_id` | 实例 id | manager |
| `role` | 角色名（派单按它路由） | manager |
| `duty` | 职责边界 | 把用户请求转化为可执行、可验证的 Plan DAG，并在执行中维持计划有效性 |
| `allowed_skills` | 可调 Skill 白名单 | `kb.retrieve`、`req.normalize` |
| `allowed_tools` | 可调工具白名单 | （空） |
| `write_scope` | 可写资源 | `plan`、`task` |
| `max_risk` | 最高授权风险级 | L |
| `model_tier` | 模型档位 | strong |
| `max_self_repair` | 自修复上限 | 0 |

### requirement — RequirementAgent

声明位置：`maos/agents/requirement.py:29`

| 字段 | 含义 | 值 |
| :-- | :-- | :-- |
| `agent_id` | 实例 id | requirement |
| `role` | 角色名（派单按它路由） | requirement |
| `duty` | 职责边界 | 把用户目标归一成可执行、可验收的需求；说不清的地方挂成 open_questions 而不是替人拍板 |
| `allowed_skills` | 可调 Skill 白名单 | `req.normalize` |
| `allowed_tools` | 可调工具白名单 | （空） |
| `write_scope` | 可写资源 | `artifact` |
| `max_risk` | 最高授权风险级 | L |
| `model_tier` | 模型档位 | strong |
| `max_self_repair` | 自修复上限 | 1 |

### reviewer — ReviewerAgent

声明位置：`maos/agents/reviewer.py:33`

| 字段 | 含义 | 值 |
| :-- | :-- | :-- |
| `agent_id` | 实例 id | reviewer |
| `role` | 角色名（派单按它路由） | reviewer |
| `duty` | 职责边界 | 对全部产物做语义审查，产出缺陷清单与结论，供人工审批参考 |
| `allowed_skills` | 可调 Skill 白名单 | （空） |
| `allowed_tools` | 可调工具白名单 | （空） |
| `write_scope` | 可写资源 | `artifact` |
| `max_risk` | 最高授权风险级 | L |
| `model_tier` | 模型档位 | strong |
| `max_self_repair` | 自修复上限 | 0 |

### testing — TestingAgent

声明位置：`maos/agents/testing.py:158`

| 字段 | 含义 | 值 |
| :-- | :-- | :-- |
| `agent_id` | 实例 id | testing |
| `role` | 角色名（派单按它路由） | testing |
| `duty` | 职责边界 | 在沙箱里跑真实测试并产出结构化报告，为 Gate 提供可验证的验收证据 |
| `allowed_skills` | 可调 Skill 白名单 | `test.verify` |
| `allowed_tools` | 可调工具白名单 | `sandbox` |
| `write_scope` | 可写资源 | `artifact` |
| `max_risk` | 最高授权风险级 | M |
| `model_tier` | 模型档位 | medium |
| `max_self_repair` | 自修复上限 | 0 |

## 制造售后退款域（5 个）

### refund_channel — RefundChannelAgent

声明位置：`maos/agents/refund/channel_agent.py:40`

| 字段 | 含义 | 值 |
| :-- | :-- | :-- |
| `agent_id` | 实例 id | refund-channel |
| `role` | 角色名（派单按它路由） | refund_channel |
| `duty` | 职责边界 | 按政策规则要求登记经销渠道的核销事项，并保留其规则出处 |
| `allowed_skills` | 可调 Skill 白名单 | （空） |
| `allowed_tools` | 可调工具白名单 | （空） |
| `write_scope` | 可写资源 | `artifact` |
| `max_risk` | 最高授权风险级 | M |
| `model_tier` | 模型档位 | light |
| `max_self_repair` | 自修复上限 | 0 |

### refund_finance — RefundFinanceAgent

声明位置：`maos/agents/refund/finance_agent.py:26`

| 字段 | 含义 | 值 |
| :-- | :-- | :-- |
| `agent_id` | 实例 id | refund-finance |
| `role` | 角色名（派单按它路由） | refund_finance |
| `duty` | 职责边界 | 按锁定政策自行复核规则并核算退款金额，写 finance_entry 并产出复核凭据 |
| `allowed_skills` | 可调 Skill 白名单 | `finance.settle`、`policy.match` |
| `allowed_tools` | 可调工具白名单 | （空） |
| `write_scope` | 可写资源 | `artifact` |
| `max_risk` | 最高授权风险级 | M |
| `model_tier` | 模型档位 | light |
| `max_self_repair` | 自修复上限 | 0 |

### refund_intake — RefundIntakeAgent

声明位置：`maos/agents/refund/intake_agent.py:26`

| 字段 | 含义 | 值 |
| :-- | :-- | :-- |
| `agent_id` | 实例 id | refund-intake |
| `role` | 角色名（派单按它路由） | refund_intake |
| `duty` | 职责边界 | 受理多源退款诉求、聚合去重并建案；处理完成后通知客户并跟踪回执 |
| `allowed_skills` | 可调 Skill 白名单 | `issue.aggregate`、`notify.customer`、`refund.intake` |
| `allowed_tools` | 可调工具白名单 | （空） |
| `write_scope` | 可写资源 | `artifact` |
| `max_risk` | 最高授权风险级 | L |
| `model_tier` | 模型档位 | light |
| `max_self_repair` | 自修复上限 | 0 |

### refund_payment — RefundPaymentAgent

声明位置：`maos/agents/refund/payment_agent.py:58`

| 字段 | 含义 | 值 |
| :-- | :-- | :-- |
| `agent_id` | 实例 id | refund-payment |
| `role` | 角色名（派单按它路由） | refund_payment |
| `duty` | 职责边界 | 核对审批后向支付网关发起退款，并轮询取得终态回执（settled 只能由观察得到） |
| `allowed_skills` | 可调 Skill 白名单 | `payment.execute`、`payment.observe` |
| `allowed_tools` | 可调工具白名单 | `gateway.query`、`gateway.refund` |
| `write_scope` | 可写资源 | `artifact` |
| `max_risk` | 最高授权风险级 | M |
| `model_tier` | 模型档位 | light |
| `max_self_repair` | 自修复上限 | 0 |

### refund_policy — RefundPolicyAgent

声明位置：`maos/agents/refund/policy_agent.py:22`

| 字段 | 含义 | 值 |
| :-- | :-- | :-- |
| `agent_id` | 实例 id | refund-policy |
| `role` | 角色名（派单按它路由） | refund_policy |
| `duty` | 职责边界 | 按订单快照锁定的政策版本检索适用规则并裁定退款资格 |
| `allowed_skills` | 可调 Skill 白名单 | `policy.match` |
| `allowed_tools` | 可调工具白名单 | （空） |
| `write_scope` | 可写资源 | `artifact` |
| `max_risk` | 最高授权风险级 | L |
| `model_tier` | 模型档位 | light |
| `max_self_repair` | 自修复上限 | 0 |
