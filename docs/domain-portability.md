# 领域可移植性 —— 换域只换 Skill / ToolPort / 业务对象

MAOS 不是为某个行业写的工作流引擎，是**领域无关的编排内核**。本仓库在软件交付域
和四个业务域上给出可运行实证：

- **软件交付域**：外部权威判据 = 沙箱里真跑出来的 `pytest` 结果（场景 1–5）
- **制造售后退款域**：外部权威判据 = 支付网关的到账回执（场景 6–7）
- **保险理赔域**：外部权威判据 = 赔付方的到账回执（场景 8）
- **银行差错处理域**：外部权威判据 = 清算方的 `pacs.004` 资金退回报文；
  `camt.029/CNCL` 只确认撤销指令，不能证明钱已退回（场景 9）
- **应付账款域**：外部权威判据 = 银行的已付款回单及 `bank_reference` 流水号（场景 10）
- **跨域协同**：应付账款付出去之后，供应商申报重复支付 → 向付款行发起差错撤销。
  一条业务链跨两个域、挂在同一个 Plan 上（场景 11）。它证明的是**编排**，
  与上面五条证明的**可移植**是两个命题，见 §1.2

场景 8–11 使用各域的 Mock ToolPort，证明的是观察、审批与失败收口的编排边界，
尚不代表真实赔付方、清算方或银行已接通。缺省 CLI 仍只跑 1–7，新增域需显式指定
`python3 run.py --scenario 8`（或 9、10、11）；缺省证据仍是 1–7 + R5 的 8 束冻结口径。
扩展证据与核验显式运行，默认另存到 `evidence/domains/`：

```bash
python3 scripts/make_evidence.py --domains
python3 scripts/verify.py --evidence evidence/domains --domains
```

扩展根目录含 12 个场景。场景 8、10 的失败路径各自另建运行时，完整证据另存于
对应场景的 `runtime-2/` 子束；核验器同时读取父束和子束，保留每个原始库的隔离。

这句话如果只是写在 PPT 上，评委没有理由信。所以本文件只做一件事：**把它拆成
可以逐条去查的断言**，每条都给出代码位置、可复现命令、以及数字。

---

## 1. 对照表：同一个内核，四个业务域 + 软件交付域

| 层 | 软件交付域 | 制造售后退款域 | 保险理赔域 | 银行差错处理域 | 应付账款域 | 共用范围与证据 |
| :-- | :-- | :-- | :-- | :-- | :-- | :-- |
| **事件契约** | `Envelope` / `EventType` / `Topic` | 同一事件契约 | 同一事件契约 | 同一事件契约 | 同一事件契约 | ✅ 同一份 `maos/contracts/events.py`，三新域未增加字段或枚举 |
| **Task 状态机** | 既有 Task 状态与迁移 | 业务状态留在 `refund_case` | `paid` 留在 `claim_case` | `returned` 留在 `investigation_case` | `settled` 留在 `ap_case` | ✅ 同一份 `maos/contracts/states.py`；场景 8–10 逐条校验 `TASK_TRANSITIONS` |
| **Control Plane** | 状态迁移、幂等去重、版本冲突拒绝 | 同一 `ControlPlane` | 同一 `ControlPlane` | 同一 `ControlPlane` | 同一 `ControlPlane` | ✅ 五域均由 `maos/flows/common.py::build` 装配，无域分支 |
| **Worker Runtime** | 从 `AGENT_POOL` 按 role 执行 | 同一 Worker，退款角色 | 同一 Worker，理赔角色 | 同一 Worker，差错角色 | 同一 Worker，AP 角色 | ✅ 同一份 `maos/runtime/worker.py`，域 Agent 经既有发现机制注册 |
| **Gate** | 七道闸；代码验收认 `test_report` | 第六道认财务凭据，第七道认网关码 | ⚠️ 复用七闸框架，理赔回执不进网关码闸 | ⚠️ 复用七闸框架，清算报文不进网关码闸 | ⚠️ 复用七闸框架，银行回单不进网关码闸 | ⚠️ `maos/runtime/gate.py` 未改，但三新域也不触发财务闸；业务判据由域 Skill / guard / 收口断言负责，详见 §1.1 |
| **replan** | blocker / rework 触发，`MAOS_MAX_REPLAN` 封顶 | 场景 7 演示换渠道 | ⚠️ 未注入 replanner，未演练 | ⚠️ 未注入 replanner，未演练 | ⚠️ 未注入 replanner，未演练 | ⚠️ 共用实现仍在 `maos/core/control_plane.py`，场景 8–10 没有接入或证明重规划 |
| **HITL 审批** | `effect_risk=H` 停 `BLOCKED` 等人决定 | 退款主管批准 / 驳回 | 核算与付款收口审批 | 调账授权与观察收口审批 | 付款计划与付款收口审批 | ✅ 同一份 `HumanApprovalQueue` 与控制面人工决策入口；各域审批记录另行落库 |
| **补偿** | 逆补丁与补偿干跑 | 撤销退款请求 + 人工工单 | 作废赔付指令 + 人工工单 | 留档最后观察 + 人工对账工单 | 作废本地付款指令 + 对账工单 | ⚠️ 复用 `SkillInvoker` / `CompensationExecuted` 审计口径；三新域的域内补偿不经过逆补丁干跑闸，也不证明外部资金已撤回 |
| **Skill** | `req.normalize` / `code.repo-patch` / `test.verify` / `issue.aggregate` / `kb.*` | `refund.intake` / `policy.match` / `finance.settle` / `payment.*` / `refund.compensate` / `notify.customer` | `claim.intake` / `claim.adjudicate` / `claim.settle` / `claim.pay` / `claim.observe` / `claim.compensate` | `investigation.file` / `investigation.classify` / `investigation.cancel` / `investigation.observe` / `investigation.compensate` | `ap.intake` / `ap.match` / `ap.plan-payment` / `ap.execute` / `ap.observe` / `ap.compensate` | ❌ 按域实现，同一份 `SkillContract` 九要素契约 |
| **ToolPort** | `sandbox.git_apply` / `sandbox.pytest_run` | `gateway.refund` / `gateway.query` | `payer.submit` / `payer.query` | `clearing.cancel` / `clearing.resolution` | `bank.pay` / `bank.query` | ❌ 按域实现，同一份 `ToolPort` 九要素契约 |
| **业务对象** | 补丁集 / 测试报告 / 架构契约 | `refund_case` / `payment_observation` | `claim_case` / `claim_payment_observation` | `investigation_case` / `resolution_observation` | `ap_case` / `ap_payment_observation` | ❌ 按域定义；业务表以 `plan_id` 关联编排，退款 / 理赔 / AP 另有各自的 business-ref 表 |
| **Agent 角色** | requirement / architecture / coding / testing / reviewer | refund-intake / refund-policy / refund-finance / refund-payment | claim_intake / claim_adjudicator / claim_settlement / claim_payment | investigation_intake / investigation_classify / investigation_cancel / investigation_observe | ap_intake / ap_match / ap_control / ap_treasury | ❌ 按域实现，共用 `AgentIdentity` 与 `AGENT_POOL`；场景 9–10 直接创建 DAG，未演示 Manager 规划 |

✅ 表示已核实三新域复用既有实现且接域提交没有修改该内核面；⚠️ 表示代码虽共用，
但判据范围或场景覆盖不足以宣称同等能力；❌ 表示按域新增实现。退款首次上线时
增加财务闸的历史代价仍按 §2 单列，不能读成所有历史区间都零改动。

### 1.1 三个新域：逐行核验的落点与限制

**事件、状态机、Control Plane、Worker、Gate 的代码复用**有两层证据。
首先，场景 8 的 `drive_happy` / `drive_failure`、场景 9 的 `drive_success`、
场景 10 的两条 `drive_*` 都调用 `maos/flows/common.py::build`；它直接构造同一个
`ControlPlane`、`WorkerRuntime`、`ReviewerGate`，不按业务域分支。
Worker 经 `maos/contracts/events.py` 校验派单并构造结果事件；三个 Agent 子包的
`__init__.py` 均以 import 触发既有 `@register`，没有另建 Worker。
三个场景的 `run` 收口还把实际 `StateTransition` 与冻结 `TASK_TRANSITIONS` 比对，
确认 `paid` / `returned` / `settled` / `compensated` 没进入 Task 状态机。

其次，分别对接入理赔的提交、接入调查与 AP 的合并提交相对第一父提交核过以下差异，
三条命令输出均为空；这项历史证据覆盖事件契约、Task 状态机、控制面与整个运行时：

```bash
git diff --stat 242117a^ 242117a -- maos/contracts/ maos/core/ maos/runtime/
git diff --stat a2639b1^ a2639b1 -- maos/contracts/ maos/core/ maos/runtime/
git diff --stat b36e043^ b36e043 -- maos/contracts/ maos/core/ maos/runtime/
```

**Gate 标 ⚠️**：`maos/runtime/gate.py::_review` 仍逐个调用七道闸，
但 `_gate_finance_task` / `_gate_finance_plan` 都只接受 `FINANCE_BIZ_TYPE = "refund"`。
新域分别使用 `claim` / `investigation` / `ap`，因此不能宣称财务闸已替它们验收。
`_gate_gateway` 又只认 `content.receipt.code` 并查支付网关码表：理赔使用
`payer_receipt`（`maos/agents/claim/_base.py`），AP 使用 `bank_advice`
（`maos/agents/ap/treasury_agent.py`）；调查虽使用 `receipt`，却是
`confirmation_code` / `return_reason_code` 等清算字段，没有通用 `code`
（`maos/tools/investigation.py::ResolutionReceipt.to_dict`）。三新域的业务回执
由各自的观察 Skill 与 guard 核验，七道闸的框架复用不等于其业务判据已泛化。

**replan 标 ⚠️**：`maos/core/control_plane.py::ControlPlane` 的 replanner 缺省为
`None`，`build` 不注入；场景 8–10 也没有调用 `set_replanner`。保留同一份
`_should_replan` / `_max_replan`，并不能证明这些场景已经演练过重规划。

**HITL 标 ✅**：三个场景均给关键任务设置 `effect_risk="H"`，通过
`HumanApprovalQueue.pending` / `decide` 批准或驳回；真正的 `BLOCKED` 路由在
`maos/core/control_plane.py::on_review_verdict`，人工处置经 `human_decision`，
均无新域分支。理赔阈值、调账授权和 AP 付款批准记录仍在域层处理。

**补偿标 ⚠️**：三个场景的 `compensate` 函数经最小授权 Identity 的 `SkillInvoker`
调用各域的补偿 Skill，后者都记录 `CompensationExecuted`。
但 `maos/runtime/gate.py::_gate_compensation` 只干跑带 `patch_ref` 的补偿产物，
三新域没有产出这种逆补丁，不能宣称共用的干跑闸验证过这些域内补偿。
`compensated` 仅表示本地停止推进、留档并交人工，不等于外部资金已退回。

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
> for c in 90251b3 4a70cb0 147df03 2474c56; do \
>   printf '%s: ' "$c"; git show $c:maos/runtime/gate.py | grep -c 'def _gate_'; done
> # 90251b3: 5   ← 退款域上线前
> # 4a70cb0: 6   ← 区间 A 之后，多了 _gate_finance
> # 147df03: 7   ← 区间 B 之后，多了 _gate_gateway（Y-4 复用它，没有再加闸）
> # 2474c56: 9   ← D 轮之后。⚠ **仍然是七道闸**，这个 grep 数的是函数定义数、不是闸数：
> #               D-2 把 _gate_finance 拆成了 _gate_finance（分发）+ _gate_finance_task
> #               （F-1 原文一字未动）+ _gate_finance_plan（新增的 plan 级判据）三个函数。
> #               判据表 `_review` 里仍是七个条目 —— 要数闸就数那张表，别数 def。
> ```

---

## 1.2 跨域协同（场景 11）：这一节证明的**不是**可移植性

上面整张表证明的是**可移植**：同一套内核，换四次域，每次只换 Skill / ToolPort /
业务对象。但它有一个自己说不出口的边界 —— 逐个数过
`maos/flows/scenario_*.py` 里的 `maos.domain.*` 引用，**十个场景，每个只碰一个域**。
换句话说，§1 那张表的横向对比，证的是「同一套内核复制着跑了四遍」，
而不是「一条业务链跨两个域，编排内核负责协调」。

场景 11 是唯一一条跨域链路，它买的是后面那句话：

```bash
python3 run.py --scenario 11
```

    ap 域（应付账款）                       investigation 域（银行差错处理）
    收票 → 三单匹配 → 付款计划(人批)
    → 发指令 → 观察到 settled
            └─ 人：供应商报来「这笔是重复支付」
               编排层翻译 ──────────────→ 受理差错案 → 定性(人批) → camt.056
                                          → PDCR → CNCL → pacs.004 → returned

### 与前面四个域在证明什么上的区别

| | §1 的四个域 | §1.2 的场景 11 |
| :-- | :-- | :-- |
| 命题 | **可移植**：换域只换三层，内核一行不改 | **可编排**：一条业务链跨两个域，由内核协调 |
| 证据形状 | 四个场景横向对比，每个各跑各的 | 一个 `plan_id` 同时挂 `ap_case` 与 `investigation_case` |
| 域之间的关系 | 互不相干（各自独立的库、Plan、租户） | 互不**认识**，但被编排层接在一条链上 |
| 会被什么推翻 | 内核里出现按域分支 | 两个域出现互相 import；或两个域各挂一个 Plan |

第三行是关键，也是这一轨最容易做歪的地方：把接缝代码塞进任一个域，链路照样跑通，
而「可编排」这个命题当场塌回「一个域顺手调了另一个域」。所以设计判断写成了机器判据：

```bash
grep -rn 'investigation' maos/domain/ap/ --include='*.py'        # 期望零命中
grep -rn 'from maos.domain.ap' maos/domain/investigation/        # 期望零命中
```

两条 grep 是 `maos/tests/test_cross_domain_flow.py` 里的用例
（`test_ap_domain_never_mentions_investigation` 及其反向那条），
不是提交前靠人记得跑一次的自查。翻译代码只此一处：
`maos/flows/scenario_11.py::handoff_to_investigation`（约 100 行，含注释）。

### 接缝上搬了哪几个字段

字段对齐是**天然存在**的，不是为了演示凑出来的 —— 两个域的 schema 各自按 ISO 20022
与 EN 16931 写，说的本来就是同一笔钱：

| ap 域读到的行 | → investigation 域写入的字段 |
| :-- | :-- |
| `ap_payment_observation.bank_reference` | `original_payment_snapshot.original_msg_id` |
| `ap_payment_observation.value_date` | `value_date`（起息日） |
| `payment_instruction.instruction_id` | `end_to_end_id`（端到端参考号） |
| `payment_instruction.amount` / `currency` | `interbank_amount` / `currency` |
| `payment_instruction.bank` | `debtor_agent`（付款行 BIC） |
| —— 两个域都不持有 —— | `creditor_agent`（收款行 BIC，编排层路由表） |

最后一行是这张表里最说明问题的一格：收款行 BIC ap 域没有（它存的是账号），
investigation 域也没有（它要别人告诉它）。它属于编排层 —— **翻译层因此不是可以
省掉的胶水，是这条链路上唯一有地方放它的地方**。

### 三条不许松的边界

1. **触发点是人发起，不是系统自动发现**。ap 域没有重复发票检测器
   （`grep duplicate maos/domain/ap/` 零命中），本轨**也没有给它加一个**：
   「这张发票重复了」的权威在财务与供应商，不在 MAOS（铁律 8）。差错申报是人递进来的
   一份输入，走既有 `HumanApprovalQueue`。没有申报，翻译层当场抛、快照一行不写
   —— 用例 `test_no_filing_no_handoff` 钉的就是这条。
2. **权威边界一格没放宽**。`settled` 仍然只有 `ap.observe` 写得进，`returned` 仍然
   只有 `investigation.observe` 写得进，且仍然只认 `pacs.004`。链路中间那句
   `camt.029/CNCL`（清算方确认撤销成功）照旧**一个字都不写**。
3. **跨域不是新的 Task 状态**（铁律 9）。八个任务的状态与迁移全部落在既有
   `TaskState` / `TASK_TRANSITIONS` 里，`maos/contracts/states.py` 一个字没改。

### 核验器：一个 Plan 挂两个域

`maos/domain/__init__.py::DOMAIN_REGISTRY` 是**一个域一份 spec**，而
`scripts/verify.py` 的三条域级判据都按「该域的 case 表在不在这个库里」选核验对象。
于是场景 11 那一束被**两个域各核验一次**，实测计数从 `1/1` 涨到 `2/2`
（`ap/authoritative-fact`、`investigation/authoritative-fact` 同此），
`business-outcome` 从 `17/17` 涨到 `18/18` —— 多出来的正是跨域那一条，不是空转。
`verify.py` 因此**一行未改**；四个域各自的旧核验结果逐字节不变（明细逐条 diff 过）。

这个行为容易被"优化"掉（给判据加一句「找到第一个匹配的域就 break」），
而少核验一遍不会变红、只会变松，所以它被
`test_one_plan_two_domains_is_verified_once_per_domain` 与
`test_one_plan_two_domains_fails_loud_when_either_side_is_forged` 两条用例钉住。

### 这一节不吹的部分

- 场景 11 用的仍是 Mock ToolPort（`MockBank` / `MockClearingHouse`），
  它证明的是**编排边界**，不是真实银行与清算方已接通。
- 两个域跑在**同一个进程、同一个库**里。跨进程、跨库、跨组织的协同（真实场景里
  应付账款系统与银行差错处理系统多半不在一处）本轨没有证明。
- 只有一条跨域链路。「任意两个域都能这样接起来」是推论，不是实证。

---

## 2. 数字：两个区间，分开算

上退款域的代价，和上退款域**之后**内核又长出来的通用能力，是两笔账。混在一个区间里
算，读者会把后者误读成「上退款域的代价」。所以这里拆成两段：

| 区间 | 端点 | 装的是什么 |
| :-- | :-- | :-- |
| **A** | `90251b3` → `4a70cb0` | **上退款域**：六 Skill / 四 Agent / 第六道闸 / 场景 6+7 / RAG 语料 |
| **B** | `4a70cb0` → `2474c56` | **X/Y/D 轮之后的增量**：内核侧是网关码四象限、第七道闸、沙箱降级可见化、rework 第三出口（D-1）、第六道闸的 plan 级判据（D-2）；域侧是 Y-4 把场景 7 的换渠道演出来、D-1 让第二段演第三出口（见 §2.3 分两类读）|

端点是什么，自己核：

```bash
git log --oneline -1 90251b3   # fix(p2): 补偿 workdir 缺省值改必填 —— 退款域上线前
git log --oneline -1 4a70cb0   # docs(ops): 看板补 W 轮七行与整合轮 3 —— 整合轮 3 收口，X 轮一行未开工
git log --oneline -1 147df03   # docs(p7): BACKLOG 记整合轮 5 三条 —— 整合轮 5 的 HEAD
git log --oneline -1 2474c56   # feat(p7): 证据束按合并态全量重跑 —— 整合轮 6 合 D-1+D-2 后的端点
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
它没有依赖退款域模块或业务表，但触发口径仍限定退款：

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

- ⚠️ 仅把申报金额放进 `task["inputs"]`、把核算凭据放进 artifact content，
  **不足以让新域触发此闸**；还必须满足 `biz_type == FINANCE_BIZ_TYPE`（当前为
  `refund`）。场景 8–10 没有冒用这个值，实际覆盖范围见 §1.1。

可复现：

```bash
python3 -m pytest maos/tests -q -k "not_import_refund_domain or does_not_know_the_refund_domain"
# 2 passed
```

### 2.3 区间 B：X 轮之后的内核增量（**不是**上退款域的代价）

**以下数字全部按整合轮 6 的 `2474c56` 实测**；右端点是主干 HEAD，会随后续轮次变化，
复算见文末台账。

```bash
git diff --shortstat 4a70cb0 2474c56 -- <path>
for p in contracts core runtime agents skills tools domain flows kb; do \
  printf '%-10s ' "$p"; git diff --shortstat 4a70cb0 2474c56 -- maos/$p/; echo; done
```

| 面 | 改动（按 `2474c56` 实测） | 是什么 |
| :-- | :-- | :-- |
| `maos/contracts/` | **（空）** | 契约面在两个区间下都是零 |
| `maos/core/` | 1 file, +162 / −5 | `control_plane.py`：网关码四象限的重规划否决判据（X-2）＋ 规划期调用的 `plan_id` 归属（Y-2）＋ rework 的**第三出口**：终态失败一次干净转人工（D-1） |
| `maos/runtime/` | 1 file, +364 / −25 | `gate.py`：第七道闸 `_gate_gateway`（X-2）＋ 第三出口的 `disposition` / `scope` 与放宽后的 `HumanApprovalQueue.pending()`（D-1）＋ 第六道闸的 **plan 级判据** `_gate_finance_plan`（D-2） |
| `maos/agents/` | 3 files, +112 / −19 | `testing.py` 透传 `sandbox_mode`（Y-1）、`manager.py` 规划期检索归属（Y-2）—— 这两处是**软件交付域/通用侧**；`refund/payment_agent.py` 换渠道重发（Y-4）—— 这一处是**退款域侧** |
| `maos/skills/` | 2 files, +32 / −4 | `refund/compensate.py`、`refund/payment_execute.py`（Y-4），**退款域侧** |
| `maos/tools/` | 1 file, +111 / −13 | `sandbox.py`：沙箱降级可见化（X-4） |
| `maos/domain/` | **（空）** | 退款**业务对象与表**在区间 B 一行没动（改的是 skill / agent 行为，不是对象定义） |
| `maos/flows/` | 4 files, +456 / −29 | `scenario_6.py`（X-1/Y-2）、`scenario_5.py`（Y-2）、`common.py`（Y-1）、`scenario_7.py` 演换渠道（Y-4）＋ 第二段演第三出口（D-1） |
| `maos/kb/` | 2 files, +426 / −109 | `experiment.py` / `retriever.py`：对照实验与检索（X-3/Y-2）＋ 第六道闸口径改写与 without_kb 段的人工处置（D-2 / 整合轮 6） |

区间 B 里 `agents/` / `skills/` / `flows/` 都不是空。**要分两类读，别混着算**：

- **通用侧**（`agents/testing.py`、`agents/manager.py`、`flows/common.py`、
  `flows/scenario_5,6.py`、`kb/**`）：测试报告装配、规划期检索归属、检索质量。
- **退款域侧**（`agents/refund/payment_agent.py`、`skills/builtin/refund/*.py`、
  `flows/scenario_7.py`）：Y-4 让场景 7 演换渠道重试，**这些本来就是按域实现的面**
  （见 §1 表里标 ❌ 的那几行），落在这里完全合规。

关键是**内核三个子包（`contracts/` / `core/` / `runtime/`）不 import 业务域模块**：
区间 B 的改动归为下面五块能力，且 §3 的两条守卫对区间 B 新增的每一行同样
生效、复跑仍 **2 passed**（整合轮 6 合入 D-1 + D-2 后复跑，见文末台账）。
这五块都**不是**上退款域的代价：

- **网关码四象限**（`core/` 的 +162 中的一部分）判的是「外部系统回了什么码，该不该重规划」。
  判据落在 `GW_REPLAN_CHANNEL` / `GW_QUERY_FIRST` / `GW_HUMAN_TERMINAL` /
  `GW_QUERY_OR_HUMAN` 四个常量上（`control_plane.py:54–62`），任何有外部系统
  回执的域都用得上，与「退款」两个字无关。
- **第七道闸**（`runtime/` 的一部分）与第六道闸同构：判据落在 artifact 的数据形状上。
- **沙箱降级可见化**（`tools/` +111）是软件交付域那一侧的工具，与退款域无关。
- **rework 的第三出口**（D-1，`core/` 与 `runtime/` 各一部分）判的是「机器返工还修不修得好」。
  判据是 finding 的 `disposition` 与 `scope` 两个字段，**不看 `effect_risk`、不看业务域**；
  `HumanApprovalQueue.pending()` 随之放宽成「H **或** 控制面声明在等人决定」。
  任何有「机器已经没有别的招」这一档的域都用得上。
- **第六道闸的 plan 级判据**（D-2，`runtime/` 的主体）判的是「计划里排没排财务复核这一步」。
  它按 `FINANCE_AMOUNT_FIELD` 在任务 inputs 树里扫描，另有
  `FINANCE_BIZ_TYPE = "refund"` 的触发限制。AST 守卫证明没有 import 业务域，
  不能证明换域只改金额字段就能触发；三新域当前均未触发，见 §1.1。

关键在于：**它们同样被 §3 的两条守卫钉着** —— `core/` 与 `runtime/` 不许 import
`maos.domain.**` 这条约束，对区间 B 新增的每一行同样生效。
这证明模块依赖隔离；第六道闸的业务类型限制、第七道闸的码表适用范围仍须分别核验。

### 2.4 两个区间的合计（≠ 两段简单相加）

`git diff` 不可加：区间 A 加的行有一部分在区间 B 被改写，所以 `126+364 ≠ 470`。
合计必须单独跑：

```bash
for p in contracts core runtime agents tools flows kb; do \
  printf '%-10s ' "$p"; git diff --shortstat 90251b3 2474c56 -- maos/$p/; echo; done
```

| 面 | 区间 A | 区间 B | **合计（`90251b3..2474c56`）** |
| :-- | :-- | :-- | :-- |
| `maos/contracts/` | 空 | 空 | **空** |
| `maos/core/` | 空 | +162 / −5 | 1 file, +162 / −5 |
| `maos/runtime/` | +126 / −4 | +364 / −25 | 1 file, **+470 / −9**（≠ 490 / −29） |
| `maos/agents/` | 8 files, +604 / −27 | 3 files, +112 / −19 | 8 files, **+703 / −33** |
| `maos/skills/` | 11 files, +1536 / −25 | 2 files, +32 / −4 | 11 files, **+1564 / −25** |
| `maos/tools/` | 2 files, +752 | 1 file, +111 / −13 | 3 files, +863 / −13 |
| `maos/flows/` | 5 files, +1023 / −123 | 4 files, +456 / −29 | 6 files, **+1454 / −127** |
| `maos/kb/` | 5 files, +1568 | 2 files, +426 / −109 | 5 files, **+1885** |

---

## 3. 三条支撑论证的机器守卫

「领域无关」这句话在本仓库有三道机器闸守着，任何一道红，这句话当场作废：

| # | 守卫 | 位置 | 守的是什么 |
| :-- | :-- | :-- | :-- |
| 1 | 契约指纹锁 | `maos/tests/test_contracts_frozen.py` + `.contracts.lock` | `contracts/events.py` 与 `contracts/states.py` 的 sha256，加上 Phase 0 那 5 张既有表的 DDL。退款域新增了 **14 张表**（`maos/domain/refund/schema.sql`），**指纹一个字节没变** —— 只新增、不改既有 |
| 2 | 内核不识域 | `test_gate.py:561`、`test_refund_flow.py:454` | 内核三个子包不许 import `maos.domain.**`，**区间 A 与区间 B 新增的行一视同仁** |
| 3 | 权威事实边界 | `test_refund_flow.py::test_no_bypass_writes_settled` + `scripts/verify.py` 第 3 项 | 退款的 `settled` 只能由 `payment.observe` 写入；其他域各自的权威写入者与状态由 `maos/domain/__init__.py` 注册并核验。详见 [`authoritative-facts.md`](authoritative-facts.md) |

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

按场景 8–10 的接入方式，领域实现可通过新增文件接入既有内核：

1. `maos/domain/<域>/objects.py`：业务对象与它们的表（**新增表，不改既有表**）。
2. `maos/skills/builtin/<域>/*.py`：每个 skill 一个模块，类上打 `@register_skill`
   —— 投放即注册，`builtin/__init__.py` 一个字都不用改（冻结契约 C-1）。
3. `maos/tools/<域>.py`：外部系统的 ToolPort 九要素声明，调用一律走 `invoke_tool()`。
4. `maos/agents/<域>/*.py`：角色 Identity + `@register`
   —— 同样是投放即注册（冻结契约 C-2）。
5. `maos/flows/scenario_<N>.py`：演示流程。

**三新域接入没有动**：`contracts/`、`core/`、`runtime/`、`artifacts.py`。
对外运行与证据还需接入 `maos/main.py` 的显式场景列表、证据生成脚本和域核验注册表；
这正是 C1 补齐的部分。`DEFAULT_SCENARIOS` 保持 1–7，不能为了展示新域改动冻结缺省口径。

退款域就是照这份清单落的，实测：**区间 A 下 `contracts/` 与 `core/` 的 diff 都是空的**
（见 §2.1 与 §2.2）。`runtime/` 那一处 +126 是首次上线退款域时增加的第六道闸，
历史代价必须保留，不能与三新域零改动的区间混算。

---

## 5. 这份论证的边界（不吹的部分）

- **软件交付 + 四个业务域，不等于任意领域都已验证。** 三新域证明了既有内核可承载
  各自的业务对象、观察与人工收口；Gate、replan 与补偿干跑的覆盖限制见 §1.1。
  §3 的机器守卫证明模块依赖隔离，不能替代每个域的业务验收。
- **`runtime/` 不是零。** 区间 A 下 `contracts/` 与 `core/` 是真零，但 `gate.py`
  实实在在多了 126 行。它没有 import 退款业务模块，但带有退款触发口径；
  §2.2 与 §3 支撑依赖隔离，不能据此宣称首次上退款域时运行时零改动。
- ~~**第六道闸的注释与实际拦点不完全一致。**~~ **已修（task-D2，2026-08-29）。**
  原坑：`gate.py` 的注释写「没检索到历史案例 → 计划里漏排财务复核 → 在这里被拦下」，
  而实测漏排时闸没有可判的对象（没有任何任务带申报金额），真实拦点在 `payment.execute`
  的「没有 finance_entry 不许发起付款」—— 注释描述的链路一次都没走过。
  修法取 `docs/BACKLOG.md ## task-W3` 第 3 条的路 ②：给闸加一条 **plan 级判据**
  （`_gate_finance_plan`，`gate.py:578` 起），判「这个 Plan 报了超阈金额却没有任何任务
  把它带进闸的视野」。**这条判据仍然领域无关**：它只读 `store.list_tasks` 拿到的
  `task["inputs"]`，按 F-1 那一个字段名（`FINANCE_AMOUNT_FIELD`）在 inputs 树里任意深度
  扫，不写死 `case_seed` 这种嵌套路径、不 import 业务域，§3 的两条 AST 守卫照常绿。
  注释同步改成实测口径（`gate.py:522`），并把「Phase 3–7 之间这句话是假的」写进正文。
  代价如实记在 `maos/kb/experiment.py` 的模块 docstring 与 `## task-D2`：R5 对照实验的
  without_kb 段拦点前移到闸，权威边界那条运行时演示由
  `maos/tests/test_plan_gate.py` 的断言接住。
- **`flows/` 与 `kb/` 的改动量很大**（**按区间 A**：`flows/` +1023 / −123、
  `kb/` +1568），这两处**本来就是按域写的**（演示流程与知识语料），不在「内核零改动」
  的主张范围内。把它们算进内核会让数字好看，但那是偷换。
  **当前 HEAD（`2474c56`）下这两个数字更大** —— `flows/` 6 files +1454 / −127、`kb/` +1885，
  因为 X-1 接了场景 6 的规划期检索、X-3 扩了对照实验与语料，整合轮 5 的 Y-1/Y-2
  又动了 `flows/common.py` 与 `kb/experiment.py`、Y-4 又把场景 7 的换渠道演出来，
  整合轮 6 的 D-1 再给 `scenario_7.py` 加了第二段（+228）、D-2 与本轮改了
  `kb/experiment.py`（+127，见 §2.4 合计表）。

---

## 整合轮 5 收口台账（2026-08-29）

以下台账保留当时的端点、数字与结论措辞。涉及「一行领域知识都没有」或
「换域只动一个字段」的历史表述，当前应按 §1.1 的触发条件与覆盖限制解读。

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

**task-D2（2026-08-29）之后的第三批**：第六道闸补了 plan 级判据，`gate.py` 内部行号整体下移，
两条 AST 守卫所在的测试文件本轨没动，行号原样。

| 引用 | 旧 | 新 |
| :-- | :-- | :-- |
| `gate.py` 第六道闸的「RAG 有无」注释 | `:454` | **`:522`** |
| `gate.py` plan 级判据 `_gate_finance_plan` | — | **`:578`**（新增） |
| `test_gate.py:561`、`test_refund_flow.py:454` | — | **未漂，复核过** |

---

## 补合 Y-4 后的第二批回填（2026-08-29）

Y-4 已并入（`783d9dd`），**区间 A 仍然一个数字都没动**（端点在过去，钉死了）。
区间 B 右端点推到 `147df03`，四行变了、两行新出现：

| 面 | 合 Y-4 前 | 合 Y-4 后 | 变化来自 |
| :-- | :-- | :-- | :-- |
| `maos/agents/` | 2 files, +62 / −10 | **3 files, +112 / −19** | Y-4 的 `refund/payment_agent.py` |
| `maos/skills/` | （空） | **2 files, +32 / −4** | Y-4 的 `refund/compensate.py`、`refund/payment_execute.py` |
| `maos/flows/` | 3 files, +94 / −9 | **4 files, +232 / −29** | Y-4 的 `scenario_7.py` |
| `maos/core/`／`runtime/`／`tools/`／`kb/` | — | **未变** | Y-4 没碰内核与工具 |
| `contracts/`／`domain/` | 空 | **仍空** | — |

🔴 **§2.3 那句「两处都是软件交付域/通用侧」已经失真，本轮改写。** Y-4 往
`agents/refund/` 与 `skills/builtin/refund/` 里加了行 —— 区间 B 现在**既有通用侧也有退款域侧**。
该节已改成分两类读，并把主张收紧到它真正证得了的那句：
**内核三个子包里一行退款域知识都没有**（§3 两条守卫复跑仍 2 passed）。
把「区间 B 只有内核通用能力」这句留着才是错的 —— 它现在不成立。

---

## 整合轮 6 收口台账（2026-08-29）

本轮合入 **D-1**（rework 第三出口）与 **D-2**（第六道闸 plan 级判据）。
**区间 A（`90251b3..4a70cb0`）仍然一个数字都没动** —— 已复跑复核：`contracts/` 空、
`core/` 空、`runtime/` +126 / −4，与前两轮逐字对上。

**区间 B 的右端点已从 `147df03` 推到 `2474c56`**，四行变了、五行未变：

| 面 | 旧值（`147df03`） | 新值（`2474c56`） | 变化来自 |
| :-- | :-- | :-- | :-- |
| `maos/core/` | 1 file, +62 / −4 | **1 file, +162 / −5** | D-1 第三出口的路由（`control_plane.py` +101） |
| `maos/runtime/` | 1 file, +150 / −6 | **1 file, +364 / −25** | D-1 的 `disposition`/`scope` 与 `pending()` 放宽（+33）＋ D-2 的 plan 级判据（+200） |
| `maos/flows/` | 4 files, +232 / −29 | **4 files, +456 / −29** | D-1 让场景 7 第二段演第三出口（`scenario_7.py` +228） |
| `maos/kb/` | 2 files, +325 / −83 | **2 files, +426 / −109** | D-2 改第六道闸口径 ＋ 整合轮 6 给 without_kb 段补人工处置（`experiment.py` +127） |
| `agents/`／`skills/`／`tools/` | — | **未变** | D 轮没碰 |
| `contracts/`／`domain/` | 空 | **仍空** | — |

**§2.3 的结论没塌，但那段「三块通用能力」已经不全，本轮改写成五块。**
D-1 的第三出口与 D-2 的 plan 级判据都落在内核（`core/` + `runtime/`），
留着「只有三块」那句话会和表格自相矛盾。两条 AST 守卫对这 300 多行新增同样生效，
本轮复跑 `-k "not_import_refund_domain or does_not_know_the_refund_domain"`
仍 **2 passed** —— 「内核三个子包里一行退款域知识都没有」这句主张本身没有被稀释。

⚠️ **唯一带域词汇的是 D-2 的 `FINANCE_AMOUNT_FIELD`**。它是个常量，换域时只动这一处，
与任务级判据同一个 —— 这一点已写进 §2.3 那段的第五块，别在后续轮次里把它读成「内核
开始认识退款域了」。

后续要复跑的只有一种情况：**主干再前进**。届时区间 B 与合计表按文中给的
`git diff --shortstat` 命令逐行重跑即可，**区间 A 永远不用回填。**

---

## RTV 域（第 5 个域）—— 两个权威源的增量

**本节 2026-09-02 由 T65 追加，基线 `4c956a8`。以上 393 行一个字节都没动** ——
那些数字与它们各自的历史端点绑定，改一个就得把整节重跑一遍。本节只做加法。

RTV（Return to Vendor，采购退货退款）是 `maos/domain/` 下的**第五个业务域**。
现有四个自己数：

```bash
ls maos/domain/ | grep -v '^__'
# ap
# claim
# investigation
# refund
```

### R1 增量是什么：从「一个外部权威源」到「两个」

前四个域都只有**一个**外部权威源、**一个**权威终态：

```bash
grep -n "^AUTHORITATIVE_STATES" maos/domain/ap/guard.py
# 67:AUTHORITATIVE_STATES = frozenset({"settled"})
```

RTV 域是**两个**（冻结契约 `review/rtv-contracts.md` 的 C-R3）：

| 权威终态 | 外部权威源 | 回执判据 |
| :-- | :-- | :-- |
| `credited` | 供应商开出贷项通知单 | `observed_state == "issued"` + `credit_note_id` |
| `settled` | AP / 银行到账 | `observed_state == "settled"` + `adjustment_id` |

为什么这是「增量」而不只是「多一个状态」：**两个权威源是两件独立的事**。
供应商认账（`credited`）和钱回到账上（`settled`）中间可以隔很久，也可以永远不发生。
前四个域的 guard 只需要判「这一个终态的回执对不对」，RTV 的 guard 必须让
`AUTHORITATIVE_STATES` 与 `AUTHORITATIVE_RECEIPT_STATE` **同增同减** ——
见到没有判据的权威终态直接拒（漏配不放行）。这是铁律 8 在本仓库被逼到最紧的一次：
一个域里有两个「MAOS 写不了、只能问」的终态。

SOP 全文与评委问答的形状见 [`sop-rtv.md`](sop-rtv.md)。

### R2 复用面有多大

**五张表复用，一张不重建。** `supplier` / `purchase_order` / `purchase_order_line` /
`goods_receipt` / `goods_receipt_line` 直接用 `ap` 域的既有定义：

```bash
grep -nE "^CREATE TABLE IF NOT EXISTS (supplier|purchase_order|purchase_order_line|goods_receipt|goods_receipt_line) " maos/domain/ap/schema.sql
# 48:CREATE TABLE IF NOT EXISTS supplier (
# 61:CREATE TABLE IF NOT EXISTS purchase_order (
# 73:CREATE TABLE IF NOT EXISTS purchase_order_line (
# 88:CREATE TABLE IF NOT EXISTS goods_receipt (
# 100:CREATE TABLE IF NOT EXISTS goods_receipt_line (
```

而冻结契约给 RTV 定的十张新表里，这五张一张都没有：

```bash
grep -c "^CREATE TABLE" review/rtv-contracts.md
# 10
grep -cE "^CREATE TABLE IF NOT EXISTS (supplier|purchase_order|purchase_order_line|goods_receipt|goods_receipt_line) " review/rtv-contracts.md
# 0
```

按 §4「换一个新域要做什么」那份清单，RTV 域要新增的是：业务对象与表、六个 skill、
五个 ToolPort、五个角色、一条演示场景（分别对应契约的 C-R1 / C-R4 / C-R5 / C-R7）。
**不需要动**的仍是那五处：`maos/contracts/`、`maos/core/`、`maos/runtime/`、
`maos/artifacts.py`、`maos/main.py`。

### R3 🔴 「内核零改动」这句话现在**还没有**被跑过

本节成稿时（基线 `4c956a8`），RTV 域的代码由四条并行轨在各自 worktree 里落地，
**一行都还没有合进来**。所以这里不写「内核零改动」，只写**待整合期核验**，
并给出该跑的命令：

```bash
# 端点：<RTV 四轨合入前的 sha> → <合入后的 sha>，届时填真值
git diff --shortstat <before> <after> -- maos/contracts/ maos/core/
# 期望：无输出（真零，口径同 §2.2）
git diff --shortstat <before> <after> -- maos/runtime/
# 期望：无输出。RTV 域**不加第八道闸** —— 若这里非空，§4 那份清单就要改，
#       且必须像 §2.2 那样逐条论证新增的判据是领域无关的
for p in contracts core runtime agents skills tools domain flows; do \
  printf '%-10s ' "$p"; git diff --shortstat <before> <after> -- maos/$p/; echo; done
# 两条 AST 守卫对 RTV 域同样要绿（它们扫的是 maos.domain.** 整个命名空间）：
python3 -m pytest maos/tests -q -k "not_import_refund_domain or does_not_know_the_refund_domain"
# 期望：2 passed
```

**编不出来就写不知道。** §5「不吹的部分」那一节的可信度全靠这一条 ——
先把「零改动」写下来再补证据，和手写证据没有区别。届时若 `runtime/` 非空，
如实回填并论证，别把数字往「零」上凑。

### R4 与既有五节的关系

- 既有五节（§1–§5）的每一个数字都是**历史 HEAD 上的实测**，端点在过去、钉死了。
  本节**不覆盖、不修订、不稀释**它们中的任何一条。
- 区间 A（`90251b3..4a70cb0`，上退款域的代价）**永远不用回填** —— 两个端点都在过去。
- RTV 域合入后要动的只有一处：**新起一段 RTV 的区间**，按 R3 的命令实测。
  不要把 RTV 的改动并进区间 B —— 那是「X/Y/D 轮之后的内核增量」，混进去会让
  「上一个业务域的代价」这件事第二次被稀释（§2 开头那段说的就是这个坑）。
- §1 的对照表现在覆盖两个域。RTV 合入后它变成三列还是另起一张表，留给整合期决定；
  本节不动那张表。
