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
| **Gate** | 七道闸；代码类任务看 `test_report` | 七道闸；退款任务在第六道闸看财务凭据、第七道闸看网关回执 | ✅ 同一份 `maos/runtime/gate.py`（两道新闸分属两个区间，见下方脚注★） |
| **replan** | 单轮 blocker ≥ 2 / 同一任务第 2 次 rework，上限 `MAOS_MAX_REPLAN`（默认 2） | 同左，同一份实现 | ✅ 判据 `maos/core/control_plane.py:380`（`_should_replan`）／上限 `:423`（`_max_replan`） |
| **HITL 审批** | `effect_risk=H` 停 `BLOCKED`，等 `/approve`｜`/reject` | 同左，审批人换成退款主管 | ✅ 同一份 `HumanApprovalQueue` |
| **补偿** | 逆补丁：`sandbox.git_apply(reverse=True)` | 域内补偿：撤销 `refund_request` + 开人工工单 | ⚠️ **机制共用**（`_gate_compensation` 干跑闸 + `CompensationExecuted` 事件），**手段按域实现** |
| **Skill** | `req.normalize` / `code.repo-patch` / `test.verify` / `issue.aggregate` / `kb.*` | `refund.intake` / `policy.match` / `finance.settle` / `payment.execute` / `payment.observe` / `refund.compensate` / `notify.customer` | ❌ **按域实现**（同一个 `SkillContract` 九要素契约） |
| **ToolPort** | `sandbox.git_apply` / `sandbox.pytest_run` | `gateway.refund` / `gateway.query` | ❌ **按域实现**（同一个 `ToolPort` 九要素契约） |
| **业务对象** | 补丁集 / 测试报告 / 架构契约（`maos/artifacts.py`） | `refund_case` / `refund_request` / `payment_observation` / `finance_entry`（`maos/domain/refund/objects.py`） | ❌ **按域实现**（都经 `business_ref` 挂到同一个 DAG 上） |
| **Agent 角色** | requirement / architecture / coding / testing / reviewer | refund-intake / refund-policy / refund-finance / refund-payment | ❌ **按域实现**（同一个 `AgentIdentity`；manager 两域共用） |

一句话：**表格里 ✅ 的那些一行都没改，❌ 的那些是新增文件**。下面是数字。

> ★ **Gate 行的数字注脚。** 「七道闸」是**当前主干的事实**，但这两道新闸不是一起
> 落的，别把它们算成同一笔账：
>
> - **第六道闸** `_gate_finance`（财务凭据）落在**区间 A**，是上退款域的一部分；
> - **第七道闸** `_gate_gateway`（网关回执四象限）落在**区间 B**，X-2 轨加的，
>   与上退款域无关。
>
> 两道闸**都不 import 业务域**，都被 §3 的两条守卫钉着。逐个端点实测闸数：
>
> ```bash
> for c in 90251b3 4a70cb0 42822fc; do \
>   printf '%s: ' "$c"; git show $c:maos/runtime/gate.py | grep -c 'def _gate_'; done
> # 90251b3: 5   ← 退款域上线前
> # 4a70cb0: 6   ← 区间 A 之后，多了 _gate_finance
> # 42822fc: 7   ← 区间 B 之后，多了 _gate_gateway
> ```

---

## 2. 数字：两个区间，分开算

上退款域的代价，和上退款域**之后**内核又长出来的通用能力，是两笔账。混在一个区间里
算，读者会把后者误读成「上退款域的代价」。所以这里拆成两段：

| 区间 | 端点 | 装的是什么 |
| :-- | :-- | :-- |
| **A** | `90251b3` → `4a70cb0` | **上退款域**：六 Skill / 四 Agent / 第六道闸 / 场景 6+7 / RAG 语料 |
| **B** | `4a70cb0` → `42822fc` | **X 轮之后的内核增量**：网关码四象限、第七道闸、沙箱降级可见化 |

端点是什么，自己核：

```bash
git log --oneline -1 90251b3   # fix(p2): 补偿 workdir 缺省值改必填 —— 退款域上线前
git log --oneline -1 4a70cb0   # docs(ops): 看板补 W 轮七行与整合轮 3 —— 整合轮 3 收口，X 轮一行未开工
git log --oneline -1 42822fc   # chore: 移除 CLAUDE.md 的「回答结尾规范」一节 —— 当前主干
```

### 2.1 区间 A：上退款域的真实代价

复跑命令（把 `<path>` 换成表里那一列即可，空输出 = 零改动）：

```bash
git diff --shortstat 90251b3 4a70cb0 -- <path>
# 或一次跑完整张表：
for p in contracts core runtime agents skills tools domain flows kb; do \
  printf '%-10s ' "$p"; git diff --shortstat 90251b3 4a70cb0 -- maos/$p/; echo; done
```

| 面 | 改动 | 读法 |
| :-- | :-- | :-- |
| `maos/contracts/` | **（空，零改动）** | 事件契约与状态机一个字节没动 —— 铁律 1 与铁律 9 兑现 |
| `maos/core/` | **（空，零改动）** | Control Plane、EventBus、Store **一个字节没动** |
| `maos/runtime/` | 1 file, +126 / −4 | **只有 `gate.py`**，即第六道闸 `_gate_finance`；`worker.py` / `plan_finalizer.py` 零改动 |
| `maos/agents/` | 8 files, +604 / −27 | 退款域 4 个新角色 + 共用基类 |
| `maos/skills/` | 11 files, +1536 / −25 | 退款域 7 个新 skill（含 `notify.customer`） |
| `maos/tools/` | 2 files, +752 | `gateway.py`（+414）与 `gateway_codes.py`（+338）两个新 ToolPort，**纯新增，`sandbox.py` 零改动** |
| `maos/domain/` | 5 files, +656 | 退款业务对象与 settled guard，**纯新增目录** |
| `maos/flows/` | 5 files, +1023 / −123 | 演示流程，按域写的，见 §5 |
| `maos/kb/` | 5 files, +1568 | 知识层与语料，按域写的，见 §5 |

`tools/` 与 `runtime/` 的逐文件明细（证明确实只有那几个文件）：

```bash
git diff --stat 90251b3 4a70cb0 -- maos/tools/ maos/runtime/
#  maos/runtime/gate.py        | 130 +++++++++++++-
#  maos/tools/gateway.py       | 414 ++++++++++++++++++++++++++++++++++++++++++++
#  maos/tools/gateway_codes.py | 338 ++++++++++++++++++++++++++++++++++++
#  3 files changed, 878 insertions(+), 4 deletions(-)
```

（`--stat` 的 `130` 是「改动行总数」= 126 + 4，与 `--shortstat` 的 `+126 / −4`
是同一笔；`414 + 338 = 752`，与上表 `tools/` 一栏对得上。）

### 2.2 `contracts/` 与 `core/` 在区间 A 下是**真零**

这是本文件最硬的一条：**上一个完整的业务域，事件契约、状态机、Control Plane、
EventBus、Store 五处合起来改动为零。**不是「几乎为零」，是 `git diff` 输出空行。

```bash
git diff --shortstat 90251b3 4a70cb0 -- maos/contracts/ maos/core/
# （无输出）
```

`maos/runtime/` **不是零**，是 `gate.py` 的 +126 / −4：第六道闸 `_gate_finance`。
这不是反例，理由是它**领域无关**：

- 它的判据只落在两个数据形状上：`task["inputs"]` 里的 `biz_type` + `amount_claimed`，
  和 artifact `content` 里的 `finance_entry` 键。**不查退款域的任何一张表**。
- 它**不 import `maos.domain.refund`**。这不是自觉，是被两条测试钉住的：

  | 测试 | 位置 | 判据 |
  | :-- | :-- | :-- |
  | `test_runtime_and_core_do_not_import_refund_domain` | `maos/tests/test_gate.py:561` | 正则扫 `maos/runtime/*.py` 与 `maos/core/*.py` 的 import 语句 |
  | `test_kernel_does_not_know_the_refund_domain` | `maos/tests/test_refund_flow.py:454` | **AST 扫描**，递归 `runtime/` + `core/` + `contracts/` 三个子包 |

  两条都认 import 语句、不认字面量 —— 因为闸自己的 docstring 里就写着「不许 import
  `maos.domain.refund`」，按子串扫会把这句自我说明判成违例（这个坑真踩过，
  见 `maos/tests/test_refund_flow.py:461` 的注释）。

- 换第三个域（比如保修、换货）时，这道闸**一行都不用改**：任何域只要把「申报金额」
  放进 `task["inputs"]`、把「核算凭据」放进 artifact content，闸就照样成立。

可复现：

```bash
python3 -m pytest maos/tests -q -k "not_import_refund_domain or does_not_know_the_refund_domain"
# 2 passed
```

### 2.3 区间 B：X 轮之后的内核增量（**不是**上退款域的代价）

**以下数字全部按整合轮 5 的 `33924d1` 实测**；右端点是主干 HEAD，会随后续轮次变化，
复算见文末台账。

```bash
git diff --shortstat 4a70cb0 33924d1 -- <path>
for p in contracts core runtime agents skills tools domain flows kb; do \
  printf '%-10s ' "$p"; git diff --shortstat 4a70cb0 33924d1 -- maos/$p/; echo; done
```

| 面 | 改动（按 `33924d1` 实测） | 是什么 |
| :-- | :-- | :-- |
| `maos/contracts/` | **（空）** | 契约面在两个区间下都是零 |
| `maos/core/` | 1 file, +62 / −4 | `control_plane.py`：网关码四象限的重规划否决判据（X-2）＋ 规划期调用的 `plan_id` 归属（Y-2） |
| `maos/runtime/` | 1 file, +150 / −6 | `gate.py`：第七道闸 `_gate_gateway`（X-2） |
| `maos/agents/` | 2 files, +62 / −10 | `testing.py`：`test_report` 透传 `sandbox_mode`（Y-1）；`manager.py`：规划期检索归属（Y-2）。**两处都是软件交付域/通用侧，不是退款角色** |
| `maos/skills/` | **（空）** | — |
| `maos/tools/` | 1 file, +111 / −13 | `sandbox.py`：沙箱降级可见化（X-4） |
| `maos/domain/` | **（空）** | 退款业务对象在区间 B 一行没动 |
| `maos/flows/` | 3 files, +94 / −9 | `scenario_6.py` 接上规划期检索（X-1/Y-2）、`scenario_5.py`（Y-2）、`common.py` 透传执行路径（Y-1） |
| `maos/kb/` | 2 files, +325 / −83 | `experiment.py` / `retriever.py`：对照实验与检索（X-3/Y-2） |

区间 B 里 `agents/` / `flows/` 不再是空 —— 那是整合轮 5 并入的 Y-1/Y-2 落点。
**这不推翻本节的主张**：`agents/testing.py` 与 `flows/common.py` 是软件交付域那一侧的
测试报告装配，`manager.py` / `scenario_5,6.py` 是规划期检索的归属修复，
**没有一处新增 `maos.domain.refund` 的知识**（§3 的两条守卫对它们同样生效，且仍绿）。
下面三块（网关码四象限、第七道闸、沙箱降级可见化）是**通用能力**，不是上退款域的代价：

- **网关码四象限**（`core/` 的 +46 中的主体）判的是「外部系统回了什么码，该不该重规划」。
  判据落在 `GW_REPLAN_CHANNEL` / `GW_QUERY_FIRST` / `GW_HUMAN_TERMINAL` /
  `GW_QUERY_OR_HUMAN` 四个常量上（`control_plane.py:54–62`），任何有外部系统
  回执的域都用得上，与「退款」两个字无关。
- **第七道闸**（`runtime/` +150）与第六道闸同构：判据落在 artifact 的数据形状上。
- **沙箱降级可见化**（`tools/` +111）是软件交付域那一侧的工具，与退款域无关。

关键在于：**它们同样被 §3 的两条守卫钉着** —— `core/` 与 `runtime/` 不许 import
`maos.domain.**` 这条约束，对区间 B 新增的每一行同样生效。所以「换第三个域这道闸
一行都不用改」这句话，现在覆盖**第六和第七两道闸**。

### 2.4 两个区间的合计（≠ 两段简单相加）

`git diff` 不可加：区间 A 加的行有一部分在区间 B 被改写，所以 `126+150 ≠ 273`。
合计必须单独跑：

```bash
for p in contracts core runtime agents tools flows kb; do \
  printf '%-10s ' "$p"; git diff --shortstat 90251b3 33924d1 -- maos/$p/; echo; done
```

| 面 | 区间 A | 区间 B | **合计（`90251b3..33924d1`）** |
| :-- | :-- | :-- | :-- |
| `maos/contracts/` | 空 | 空 | **空** |
| `maos/core/` | 空 | +62 / −4 | 1 file, +62 / −4 |
| `maos/runtime/` | +126 / −4 | +150 / −6 | 1 file, **+273 / −7**（≠ 276 / −10） |
| `maos/agents/` | 8 files, +604 / −27 | 2 files, +62 / −10 | 8 files, **+662 / −33** |
| `maos/tools/` | 2 files, +752 | 1 file, +111 / −13 | 3 files, +863 / −13 |
| `maos/flows/` | 5 files, +1023 / −123 | 3 files, +94 / −9 | 6 files, **+1112 / −127** |
| `maos/kb/` | 5 files, +1568 | 2 files, +325 / −83 | 5 files, **+1810** |

---

## 3. 三条支撑论证的机器守卫

「领域无关」这句话在本仓库有三道机器闸守着，任何一道红，这句话当场作废：

| # | 守卫 | 位置 | 守的是什么 |
| :-- | :-- | :-- | :-- |
| 1 | 契约指纹锁 | `maos/tests/test_contracts_frozen.py` + `.contracts.lock` | `contracts/events.py` 与 `contracts/states.py` 的 sha256，加上 Phase 0 那 5 张既有表的 DDL。退款域新增了 **14 张表**（`maos/domain/refund/schema.sql`），**指纹一个字节没变** —— 只新增、不改既有 |
| 2 | 内核不识域 | `test_gate.py:561`、`test_refund_flow.py:454` | 内核三个子包不许 import `maos.domain.**`，**区间 A 与区间 B 新增的行一视同仁** |
| 3 | 权威事实边界 | `test_refund_flow.py::test_no_bypass_writes_settled` + `scripts/verify.py` 第 3 项 | 全仓只有 `payment.observe` 写得进 `settled`。详见 [`authoritative-facts.md`](authoritative-facts.md) |

那 14 张表自己数：

```bash
grep -c 'CREATE TABLE' maos/domain/refund/schema.sql
# 14
```

依次是 `tenant` / `channel` / `order_snapshot` / `product_snapshot` / `policy_rule` /
`refund_case` / `customer_evidence` / `approval_record` / `finance_entry` /
`refund_request` / `payment_observation` / `notification` / `compensation_record` /
`business_ref`。

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

退款域就是照这份清单落的，实测：**区间 A 下 `contracts/` 与 `core/` 的 diff 都是空的**
（见 §2.1 与 §2.2）。`runtime/` 那一处 +126 是第六道闸，按 §2.2 的三条理由，
它是领域无关的通用判据，不是「为退款域改内核」。

---

## 5. 这份论证的边界（不吹的部分）

- **两个域，不是 N 个域。** 两个不同领域跑通不等于「任意领域可移植」，它证明的是
  「内核里没有软件交付域的特化」这件否定式的事 —— 而这恰好是靠 §3 的机器守卫钉住的，
  不是靠两个域的样本量说话。
- **`runtime/` 不是零。** 区间 A 下 `contracts/` 与 `core/` 是真零，但 `gate.py`
  实实在在多了 126 行。本文件的主张是「这 126 行领域无关」，不是「一行都没加」——
  前者靠 §2.2 的三条理由 + §3 的两条守卫支撑，后者本仓库给不出。
- **第六道闸的注释与实际拦点不完全一致。** `gate.py:454` 的注释写「没检索到历史案例
  → 计划里漏排财务复核 → 在这里被拦下」，实测漏排时闸没有可判的对象（没有任何任务带
  申报金额），真实拦点在 `payment.execute` 的「没有 finance_entry 不许发起付款」。
  已记 `docs/BACKLOG.md ## task-W3`（该条记的行号是 `gate.py:363`，是 W-3 当时的
  行号，第七道闸落地后已漂到 454，坑本身没变）。
- **`flows/` 与 `kb/` 的改动量很大**（**按区间 A**：`flows/` +1023 / −123、
  `kb/` +1568），这两处**本来就是按域写的**（演示流程与知识语料），不在「内核零改动」
  的主张范围内。把它们算进内核会让数字好看，但那是偷换。
  **当前主干（`33924d1`）下这两个数字更大** —— `flows/` 6 files +1112 / −127、`kb/` +1810，
  因为 X-1 接了场景 6 的规划期检索、X-3 扩了对照实验与语料，
  整合轮 5 的 Y-1/Y-2 又动了 `flows/common.py` 与 `kb/experiment.py`（见 §2.4 合计表）。

---

## 整合轮 5 收口台账（2026-08-29）

**区间 A（`90251b3..4a70cb0`）一个数字都没动** —— 两个端点都在过去，钉死了。
合并 Y-1/Y-2/Y-3 后复跑逐条对上：`contracts/` 空、`core/` 空、`runtime/` +126 / −4。
这正是换端点的收益：**上退款域的代价这件事，从此不会再被后续轮次的改动稀释。**

**区间 B 的右端点已从 `42822fc` 推到 `33924d1`**，下面各行按新 HEAD 重跑：

| 面 | 旧值（`42822fc`） | 新值（`33924d1`） | 变化来自 |
| :-- | :-- | :-- | :-- |
| `maos/core/` | +46 / −2 | **+62 / −4** | Y-2 规划期 `plan_id` 归属 |
| `maos/agents/` | （空） | **2 files, +62 / −10** | Y-1 `testing.py`、Y-2 `manager.py` |
| `maos/flows/` | 1 file, +13 / −2 | **3 files, +94 / −9** | Y-1 `common.py`、Y-2 `scenario_5,6.py` |
| `maos/kb/` | 2 files, +293 / −77 | **2 files, +325 / −83** | Y-2 语料与归属 |
| `maos/runtime/`／`tools/` | +150 / −6、+111 / −13 | **未变** | Y 轮没碰 |
| `contracts/`／`skills/`／`domain/` | 空 | **仍空** | — |

§2.3 的结论**没塌但要读对**：区间 B 里 `agents/` 与 `flows/` 从空变成非空，
落点是软件交付域侧的测试报告装配与规划期检索归属，**没有一处新增退款域知识** ——
§3 的两条守卫对它们同样生效，复跑 `-k "not_import_refund_domain or
does_not_know_the_refund_domain"` 仍 **2 passed**。该节已相应改写，不是留着旧话。

**行号复核**（Y-2 动过 `control_plane.py`，两处漂了）：

| 引用 | 旧 | 新 |
| :-- | :-- | :-- |
| `_should_replan` | `:366` | **`:380`** |
| `_max_replan` | `:409` | **`:423`** |
| `GW_*` 四常量 | `:54–68` | **`:54–62`** |
| `test_refund_flow.py` 那条注释 | `:460` | **`:461`** |
| `gate.py` 第六道闸注释、`test_gate.py:561`、`test_refund_flow.py:454` | — | **未漂，复核过** |

---

## 待整合轮 6 回填

**Y-4 尚未合并。** 它只动 `flows/scenario_7.py` 与 `agents/refund/payment_agent.py`
（后者在 `maos/agents/refund/**`，属**退款域侧**）。合并后要复跑的：

| # | 位置 | 复跑命令 |
| :-- | :-- | :-- |
| 1 | §2.3 `agents/`／`flows/` 两行 | `git diff --shortstat 4a70cb0 <HEAD> -- maos/agents/ maos/flows/` |
| 2 | §2.4 合计表 `agents/`／`flows/` 两行 | `git diff --shortstat 90251b3 <HEAD> -- maos/agents/ maos/flows/` |
| 3 | §2.3 那句「两处都是软件交付域/通用侧」 | Y-4 会往 `agents/refund/` 里加行，**该句要改口**：区间 B 的 `agents/` 届时既有通用侧也有退款域侧 |
| 4 | §1 ★ 脚注闸数 | `grep -c 'def _gate_' maos/runtime/gate.py`（当前 **7**，Y-4 不应改变它） |

**区间 A 依旧不用回填。**
