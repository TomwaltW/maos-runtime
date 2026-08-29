# Demo 分镜（4 分 30 秒）

**主线：退款失败路径（场景 7）。** 不演「一切顺利」—— 顺利路径谁都能演，
这个 Demo 要证明的是**业务没成功的时候系统怎么如实记录**。

录制前先把整条链路跑一遍确认无红，本文件每一镜都标了确切命令。

---

## 录制前置（人肉，5 分钟）

```bash
cd <repo>
python3 -m pytest maos/tests -q          # 应 455 passed
python3 run.py                           # 场景 1-6，exit=0
python3 run.py --scenario 7              # 失败路径，exit=0
python3 scripts/make_evidence.py         # 落 evidence/scenario-1..7
python3 -m maos.kb.experiment            # 落 evidence/scenario-R5
python3 scripts/verify.py                # 应 7/7 PASS
```

终端准备：字号调大到后排能看清，窗口宽度 ≥ 100 列（状态迁移轨迹那一屏会换行）。

⚠️ **三处与手册原分镜不一致，录制前必须先定**（详见文末「录制前必须确认的三件事」）：
Element 房间未接通、replan 换渠道那一段尚未落地、审批由场景脚本自动驱动。
本分镜按**当前代码的真实行为**写，不按手册想象写。

---

## 00:00 — 00:40　多源诉求进来，DAG 长出来

**命令**

```bash
python3 run.py --scenario 7
```

**镜头**：终端顶部那几行。

**要念的**：一条轴承订单的退款诉求，同时来自工单、客服记录、客户图片三处。
受理 Agent 把它们去重聚合成一个 case，Manager 规划出 4 个任务的 DAG。

**画面上要指出的**：

```
Plan: FAILED  |  处理客户对轴承订单的退款诉求：政策与金额均无异议，但支付渠道回执异常
  · 受理多源退款诉求并聚合证据                        DONE             attempt=1 risk=L
  · 按下单锁定的政策版本裁定退款资格                     DONE             attempt=1 risk=L
  · 核算退款金额并写财务分录                         DONE             attempt=1 risk=M
  · 发起退款并观察网关终态                          FAILED           attempt=1 risk=M
```

**一句钩子**：注意 Plan 的终态是 `FAILED` —— 记住这个，最后一镜要回来对它。

---

## 00:40 — 01:20　政策裁定：命中规则编号，不是「AI 觉得可以退」

**操作**：用编辑器打开两个证据文件，镜头给到文件内容（比在终端 `cat` 一大段 JSON 好看）：

```
evidence/scenario-7/result.json            # 政策任务那一段
evidence/scenario-7/business-objects.json  # 政策裁定产物：规则编号 + 版本 + 依据
```

> 提示：证据文件**首行是 `# generated at ...` 注释**，用 `json.load` 直接读会炸。
> 仓库里的读取一律走 `verify.py::load_evidence_json`（它会跳过首行）。演示时打开
> 看就行，不需要解析。

**镜头**：`result.json` 里政策任务那一段 + 业务对象里的政策裁定产物。

**要念的**：政策裁定给出的是**规则编号 + 版本**（`AS-01@v1`），依据的是**下单当时
锁定的政策版本**，不是今天生效的那一版 —— 政策改版不会追溯改判历史订单。

---

## 01:20 — 02:00　RAG：历史案例改变了计划本身

**命令**

```bash
python3 -m maos.kb.experiment
```

**镜头**：这条命令的三段输出，重点在最后两行。

```
[2/3] without_kb：MAOS_KB_ENABLED=0，计划漏排财务核算
  4 个任务，KbRetrieved 0 条，第六道闸 not_triggered，Plan FAILED，业务状态 submitted
  拦点：发起退款并观察网关终态 -> LookupError: case=... 没有 finance_entry，金额未经核算，不许发起付款

[3/3] with_kb：MAOS_KB_ENABLED=1，同一份计划脚本
  5 个任务，KbRetrieved 1 条，第六道闸 pass，Plan DONE，业务状态 settled

差异：delta_tasks=['refund_finance.refund_finance']，触发文档=[('kb-r5-history-0001', 0.719031)]
```

**要念的**：唯一的变量是 `MAOS_KB_ENABLED`，同一份计划脚本。关掉检索，计划漏排了
财务核算，付款那一步被「金额未经核算不许发起」拦下；打开检索，命中一条历史案例，
计划**自己补上了财务核算这一环**，走到到账。

**这就是「历史流程知识改善规划质量」的可核验形态** —— 不是「我们接了 RAG」一句话，
是两版 DAG 的 diff（`evidence/scenario-R5/dag-diff.json`）。

特写：`evidence/scenario-R5/kb-hits.json`，命中的 doc_id 与分数。

> ⚠️ **本镜的数字会变**：检索质量收口（FTS5 修复 + 真语料接入）仍在进行中，
> 语料从最小集换成完整语料后，`candidate_count` 与命中分数都会变，
> 也可能出现多条命中。**录制当天现跑一次，以当时输出为准，不要照抄本文件的数字。**

---

## 02:00 — 02:35　第六道闸：漏了财务凭据就过不去

**镜头**：回到主终端（场景 7 的输出），指状态迁移轨迹里的这两行。

```
    task-s7-finance  AWAITING_REVIEW  -> BLOCKED          [gate_needs_human]
    task-s7-finance  BLOCKED          -> DONE             [human_approve]
```

**要念的**：六道闸按顺序跑 —— schema、验收、安全、证据、补偿干跑、财务复核。
第六道闸只认两个数据形状：任务入参里的申报金额，和产物里的财务凭据。
**它不查退款域的任何一张表、不 import 业务域**，所以换个域这道闸一行都不用改。

金额超阈值 + 影响面高 → 闸全过也停 `BLOCKED`，等人。

---

## 02:35 — 03:10　支付执行：网关说不清，系统就不写

**镜头**：主终端的付款任务那一段。

```
    task-s7-payment  AWAITING_REVIEW  -> BLOCKED          [gate_needs_human]
    task-s7-payment  BLOCKED          -> FAILED           [human_reject]
```

**要念的**：付款发出去了，网关返 `ACQ.SYSTEM_ERROR` —— 这个错误码的语义是
**「说不清结果」**，不是「失败」。`payment.observe` 轮询三次，仍然问不出终态。

**这时候系统做了什么？什么都没做。** 不写状态、不写观察行、不猜「大概率成了」。
`refund()` 永远不返回终态，终态只能由 `query()` 观察得到 —— 这条在
`gateway.refund` 的 ToolPort 安全边界里写死了。

> ⚠️ 手册原分镜这里是「replan 换渠道重试 → 达上限 → 转人工」。
> **当前代码是「一次就转人工」** —— 网关错误码接成 replan 触发线这一段尚未落地
> （`docs/BACKLOG.md ## task-W6` 第 1 条）。这一段正在补，**录制当天以实跑为准**：
> 如果已经合并，这一镜多 15 秒演「系统自己试过换渠道」；没合并就按上面这版念，
> 不要演不存在的东西。

---

## 03:10 — 03:50　补偿与收口：从未进入 settled

**镜头**：主终端最后五行 —— 这是整个 Demo 的题眼，停久一点。

```
  业务状态  : compensated（全程没有经过 settled）
  settled 观察: 0 条 —— 没问出终态就一条都不该有
  补偿记录  : 2 行 ['manual_ticket', 'refund_request_revoked']
  补偿事件  : 1 条 CompensationExecuted
  Plan 终态 : FAILED（主管驳回，业务确实没成功）
```

**要念的**：主管驳回，补偿执行 —— 撤销退款请求、开人工工单。业务状态收在
`compensated`，**全程没有经过 `settled`**。

四个 Agent 全都回复了「完成」。而这一单**没有成功**，系统如实这么记了。
这就是评委那句「所有 Agent 都回复完成 ≠ 业务成功」的正面回答。

**顺带指一句**：`settled` 这个状态全系统只有 `payment.observe` 一个 skill 写得进去，
而且必须同事务附上它读到的那份回执。越权写入不是静默失败，是抛异常 + 落一条
`AuthoritativeFactViolation` 事件 —— 因为「系统拒绝了一次越权写入」本身就是证据。

---

## 03:50 — 04:20　一条命令，评委自己核验

**命令**

```bash
python3 scripts/verify.py
```

**镜头**：七行 PASS。

```
[PASS] hash-integrity       74/74
[PASS] business-ref         23/23
[PASS] authoritative-fact   3/3
[PASS] trace-tree           18/18
[PASS] kb-hit               1/1
[PASS] business-outcome     9/9
[PASS] history-case         1/1

RESULT: 7/7 PASS
```

**要念的**：这不是我们自己说跑通了。第 1 项重放每一次调用的哈希，改一个字节就红；
第 3 项验每一个 `settled` 都有回执、且回执出自 `payment.observe`；第 6 项验每个 Plan
终态都有外部判据。**任一项 FAIL，退出码非零。**

**一句要点**：检索不准顶多说效果一般，无法核验就是零分。所以这条命令是可以交到
评委手里、由他自己跑的。

> 数字随证据束重跑会变（比如新增一个场景），**录制当天以现跑输出为准**。

---

## 04:20 — 04:30　一屏总结：同一个内核，两个域

**镜头**：切到 `docs/domain-portability.md` 的对照表，或一张事先做好的图。

**要念的**：

> 本仓库在两个完全不同的领域上给出可运行实证：软件交付域（外部判据 = 真实测试结果）
> 与制造售后退款域（外部判据 = 支付到账回执）。**换域只换 Skill、ToolPort 与业务对象**，
> `contracts/` 与 `core/` **零改动**。

最后一屏打这一条命令的输出：

```bash
git diff --stat 90251b3 4a70cb0 -- maos/contracts/ maos/core/
```

（空输出 = 退款域上线前后，事件契约、状态机、Control Plane 一个字节没变。）

---

## 录制前必须确认的三件事

| # | 事项 | 现状 | 影响哪一镜 | 处置 |
| :-- | :-- | :-- | :-- | :-- |
| 1 | **Element / Matrix 房间** | 未接通（Synapse 账号需人工注册） | 手册原分镜的 00:00「房间里看拆解」与 02:00「审批卡 /approve」 | 接通了就补两镜房间画面（`run.py --scenario 7 --matrix`）；没接通就按本分镜走终端，**不要摆拍一个假房间** |
| 2 | **网关错误码 → replan 换渠道** | 未落地，当前一次就转人工 | 02:35 那一镜 | 合并了就加 15 秒演「系统自己试过」；没合并按现状念 |
| 3 | **审批由谁驱动** | 场景脚本里直接调 `hq.decide()`（`maos/flows/scenario_7.py:318` / `:351`），不是真人在房间里发命令 | 02:00 与 03:10 两镜 | 念词用「主管审批」没问题（那确实是 HITL 停点），但**别说「我现在在聊天室里点一下」** —— 除非 ①已解决并真的现场发命令 |

## 与手册原分镜的对照

手册（`docs/EXECUTION.md:724` 起）给的是 8 个镜头，本分镜是 8 镜的落地版。
差异只有两处，都是**代码现状导致的，不是删戏**：

- 手册 02:30 的「replan → 换渠道 → 达上限」压缩成「问不出终态 → 转人工」（见上表 2）
- 手册的 Element 画面全部改走终端（见上表 1）

其余六镜与手册一一对应，时间点前后差不超过 15 秒。
