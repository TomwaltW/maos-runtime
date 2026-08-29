# 领域可移植性 —— 换域只换 Skill / ToolPort / 业务对象

MAOS 不是为某个行业写的工作流引擎，是**领域无关的编排内核**。本仓库在两个完全
不同的领域上给出可运行实证：

- **软件交付域**：外部权威判据 = 沙箱里真跑出来的 `pytest` 结果（场景 1–5）
- **制造售后退款域**：外部权威判据 = 支付网关的到账回执（场景 6–7）

这句话如果只是写在 PPT 上，评委没有理由信。所以本文件只做一件事：**把它拆成
可以逐条去查的断言**，每条都给出代码位置、可复现命令、以及数字。

---

## 1. 对照表：同一个内核，两个域

| 层 | 软件交付域 | 制造售后退款域 | 是否共用 |
| :-- | :-- | :-- | :-- |
| **事件契约** | `Envelope` / `EventType` / `Topic` | 同左，**一个字段都没加** | ✅ 同一份 `maos/contracts/events.py` |
| **Task 状态机** | `PENDING → DISPATCHED → RUNNING → AWAITING_REVIEW → DONE/BLOCKED/FAILED` | 同左，**没加新状态、没加新迁移** | ✅ 同一份 `maos/contracts/states.py` |
| **Control Plane** | 唯一的状态迁移持有者、幂等去重、版本冲突拒绝 | 同左 | ✅ 同一份 `maos/core/control_plane.py` |
| **Worker Runtime** | 按 role 从 `AGENT_POOL` 取执行者，跑 Identity 三查 | 同左 | ✅ 同一份 `maos/runtime/worker.py` |
| **Gate** | 七道闸；代码类任务看 `test_report` | 七道闸；退款任务在第六道闸看财务凭据、第七道闸看网关回执 | ✅ 同一份 `maos/runtime/gate.py`（+2 道闸） |
| **replan** | 单轮 blocker ≥ 2 / 同一任务第 2 次 rework，上限 `MAOS_MAX_REPLAN`（默认 2） | 同左，同一份实现 | ✅ `maos/core/control_plane.py:366` |
| **HITL 审批** | `effect_risk=H` 停 `BLOCKED`，等 `/approve`｜`/reject` | 同左，审批人换成退款主管 | ✅ 同一份 `HumanApprovalQueue` |
| **补偿** | 逆补丁：`sandbox.git_apply(reverse=True)` | 域内补偿：撤销 `refund_request` + 开人工工单 | ⚠️ **机制共用**（`_gate_compensation` 干跑闸 + `CompensationExecuted` 事件），**手段按域实现** |
| **Skill** | `req.normalize` / `code.repo-patch` / `test.verify` / `issue.aggregate` / `kb.*` | `refund.intake` / `policy.match` / `finance.settle` / `payment.execute` / `payment.observe` / `refund.compensate` / `notify.customer` | ❌ **按域实现**（同一个 `SkillContract` 九要素契约） |
| **ToolPort** | `sandbox.git_apply` / `sandbox.pytest_run` | `gateway.refund` / `gateway.query` | ❌ **按域实现**（同一个 `ToolPort` 九要素契约） |
| **业务对象** | 补丁集 / 测试报告 / 架构契约（`maos/artifacts.py`） | `refund_case` / `refund_request` / `payment_observation` / `finance_entry`（`maos/domain/refund/objects.py`） | ❌ **按域实现**（都经 `business_ref` 挂到同一个 DAG 上） |
| **Agent 角色** | requirement / architecture / coding / testing / reviewer | refund-intake / refund-policy / refund-finance / refund-payment | ❌ **按域实现**（同一个 `AgentIdentity`；manager 两域共用） |

一句话：**表格里 ✅ 的那些一行都没改，❌ 的那些是新增文件**。下面是数字。

---

## 2. 数字：退款域上线前后的 `git diff --stat`

- `90251b3` = P2 四轨收口，**退款域上线前**
- `df96fa8` = 当前主干（软件域 + 退款域 + RAG + 证据束 + 整合轮 4 五轨全部在内）

```
$ git diff --stat 90251b3 df96fa8 -- maos/contracts/ maos/runtime/
 maos/runtime/gate.py | 280 +++++++++++++++++++++++++++++++++++++++++++++++++--
 1 file changed, 273 insertions(+), 7 deletions(-)
```

拆开逐面看（`git diff --shortstat 90251b3 df96fa8 -- <path>`，空行 = 零改动）：

| 面 | 改动 | 读法 |
| :-- | :-- | :-- |
| `maos/contracts/` | **（空，零改动）** | 事件契约与状态机一个字节没动 —— 铁律 1 与铁律 9 兑现 |
| `maos/core/` | 1 file, +46 / −2 | **只有 `control_plane.py`** 的网关码四象限判据（整合轮 4 / X-2）；EventBus、Store 一个字节没动。不是零，读法见下节 |
| `maos/runtime/` | 1 file, +273 / −7 | **只有 `gate.py`**，即第六道闸与第七道闸；`worker.py` / `plan_finalizer.py` 零改动 |
| `maos/agents/` | 8 files, +604 / −27 | 退款域 4 个新角色 + 共用基类 |
| `maos/skills/` | 11 files, +1536 / −25 | 退款域 7 个新 skill（含 `notify.customer`） |
| `maos/tools/` | 3 files, +863 / −13 | `gateway.py` / `gateway_codes.py` 两个新 ToolPort；另 +111/−13 是 `sandbox.py` 的降级可见化（整合轮 4 / X-4），与退款域无关 |
| `maos/domain/` | 5 files, +656 | 退款业务对象与 settled guard，**纯新增目录** |

### 关于 `core/` 与 `runtime/` 那两块不是零 —— 如实说清楚

`maos/runtime/` 那一侧**不是零**，是 `gate.py` 的 +273 行：第六道闸 `_gate_finance`
与第七道闸 `_gate_gateway`。`maos/core/` 那一侧**也不是零**，是 `control_plane.py`
的 +46 行：网关码四象限的重规划否决判据（`GW_*` 常量与 `_should_replan` 的入口）。
两处都不是反例，理由是它们**领域无关**：

- 它的判据只落在两个数据形状上：`task["inputs"]` 里的 `biz_type` + `amount_claimed`，
  和 artifact `content` 里的 `finance_entry` 键。**不查退款域的任何一张表**。
- 它**不 import `maos.domain.refund`**。这不是自觉，是被两条测试钉住的：

  | 测试 | 位置 | 判据 |
  | :-- | :-- | :-- |
  | `test_runtime_and_core_do_not_import_refund_domain` | `maos/tests/test_gate.py:561` | 正则扫 `maos/runtime/*.py` 与 `maos/core/*.py` 的 import 语句 |
  | `test_kernel_does_not_know_the_refund_domain` | `maos/tests/test_refund_flow.py:454` | **AST 扫描**，递归 `runtime/` + `core/` + `contracts/` 三个子包 |

  两条都认 import 语句、不认字面量 —— 因为闸自己的 docstring 里就写着「不许 import
  `maos.domain.refund`」，按子串扫会把这句自我说明判成违例（这个坑真踩过，
  见 `maos/tests/test_refund_flow.py:460` 的注释）。

- 换第三个域（比如保修、换货）时，这道闸**一行都不用改**：任何域只要把「申报金额」
  放进 `task["inputs"]`、把「核算凭据」放进 artifact content，闸就照样成立。

可复现：

```bash
python3 -m pytest maos/tests -q -k "not_import_refund_domain or does_not_know_the_refund_domain"
```

---

## 3. 三条支撑论证的机器守卫

「领域无关」这句话在本仓库有三道机器闸守着，任何一道红，这句话当场作废：

| # | 守卫 | 位置 | 守的是什么 |
| :-- | :-- | :-- | :-- |
| 1 | 契约指纹锁 | `maos/tests/test_contracts_frozen.py` + `.contracts.lock` | `contracts/events.py` 与 `contracts/states.py` 的 sha256，加上 Phase 0 那 5 张既有表的 DDL。退款域新增了 **14 张表**（`maos/domain/refund/schema.sql`），**指纹一个字节没变** —— 只新增、不改既有 |
| 2 | 内核不识域 | `test_gate.py:561`、`test_refund_flow.py:454` | 内核三个子包不许 import `maos.domain.**` |
| 3 | 权威事实边界 | `test_refund_flow.py::test_no_bypass_writes_settled` + `scripts/verify.py` 第 3 项 | 全仓只有 `payment.observe` 写得进 `settled`。详见 [`authoritative-facts.md`](authoritative-facts.md) |

---

## 4. 换一个新域要做什么（照着抄的清单）

按当前代码结构，新增一个业务域**只需要新增文件**，不需要改任何既有内核文件：

1. `maos/domain/<域>/objects.py`：业务对象与它们的表（**新增表，不改既有表**）。
2. `maos/skills/builtin/<域>/*.py`：每个 skill 一个模块，类上打 `@register_skill`
   —— 投放即注册，`builtin/__init__.py` 一个字都不用改（冻结契约 C-1）。
3. `maos/tools/<域>.py`：外部系统的 ToolPort 九要素声明，调用一律走 `invoke_tool()`。
4. `maos/agents/<域>/*.py`：角色 Identity + `@register`
   —— 同样是投放即注册（冻结契约 C-2）。
5. `maos/flows/scenario_<N>.py`：演示流程。

**不需要动**：`contracts/`、`core/`、`runtime/`、`artifacts.py`、`main.py`。

退款域就是照这份清单落的，实测：`contracts/` 与 `core/` 的 diff 是空的（见 §2）。

---

## 5. 这份论证的边界（不吹的部分）

- **两个域，不是 N 个域。** 两个不同领域跑通不等于「任意领域可移植」，它证明的是
  「内核里没有软件交付域的特化」这件否定式的事 —— 而这恰好是靠 §3 的机器守卫钉住的，
  不是靠两个域的样本量说话。
- **第六道闸的注释与实际拦点不完全一致。** `gate.py:363` 的注释写「没检索到历史案例
  → 计划里漏排财务复核 → 在这里被拦下」，实测漏排时闸没有可判的对象（没有任何任务带
  申报金额），真实拦点在 `payment.execute` 的「没有 finance_entry 不许发起付款」。
  已记 `docs/BACKLOG.md ## task-W3`。
- **`flows/` 与 `kb/` 的改动量很大**（+1023 / +1568），这两处**本来就是按域写的**
  （演示流程与知识语料），不在「内核零改动」的主张范围内。把它们算进内核会让数字好看，
  但那是偷换。
