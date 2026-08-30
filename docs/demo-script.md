# Demo 分镜（九镜 · 总长 5 分 26 秒 / 326s）

**主线：退款失败路径（场景 7）。** 不演「一切顺利」—— 顺利路径谁都能演，
这个 Demo 要证明的是**业务没成功的时候系统怎么如实记录**。

**本分镜按屏幕上真能看到的东西写。** 每一镜的命令都在 `27c9e18`（T 轮基线）上
真跑过，输出是实贴的，耗时是实测的。念词里没有一句是屏幕上找不到的 —— 这是本文件唯一的判准。

---

## 三个必含要素落在哪一镜

规则把下面三样列为 Demo 视频的**必含项**（口径来源见
[`docs/open-questions.md`](open-questions.md) **OQ-1**，**待人类以官方原文核实**）。
逐条对到镜号，录完对着这张表点一遍：

| 必含项 | 落在哪一镜 | 屏幕上看到的是什么 |
| :-- | :-- | :-- |
| **Agent 协作过程** | **镜 1**（主）＋镜 4／5／6 | 镜 1 的 Plan 表：4 个任务 × 状态 × `attempt` × `risk`，一条诉求被 Manager 拆成 DAG；镜 4/5/6 的状态迁移轨迹是这条 DAG 在状态机上真实走过的路 |
| **Skill 调用过程** | **镜 7（T 轮新补）** | 注册表 13 个 skill × 九要素 → `event_log` 里同一条 case 的 10 次 `SkillInvoked`（带版本、`invocation_id`、`duration_ms`）→ 白名单外一律 `PermissionDenied` |
| **AgentTeams 状态展示** | **镜 1 + 镜 4／5**（A 分支，今天就能录）<br>Element 房间实拍（B 分支，待 T4） | A 分支：Team 成员当前在干什么（镜 1 的任务 × 角色 × 状态）＋事件链与 HITL 停点（镜 4/5 轨迹里的 `gate_needs_human` / `human_approve` / `human_reject`）。B 分支见文末「录制前必须确认的三件事」第 1 条 |

> 🔴 **镜 7 是 T 轮补的，补之前这份分镜全文一次都没提到 Skill**（`grep -i skill` 零命中），
> 而「Skill 调用过程」是必含项、「Skill 工程体系」在评审里占一档权重。
> 少这一镜不是润色问题，是**第一眼就少一个必含要素**。

---

## 逐镜耗时表（`27c9e18` 实测，2026-08-29 · T 轮重掐）

中文口播按 **4 字/秒**折算。字数口径：汉字逐字计，阿拉伯数字一串算 1 字，
英文标识符（`payment.observe` 这类）按 1.5 字粗算，标点不计。
「富余」= 分配时长 −（命令耗时 + 念完约需）。

| 镜 | 时间轴 | 命令 | 实测命令耗时 | 讲稿字数 | 念完约需 | 富余 |
| :-- | :-- | :-- | --: | --: | --: | --: |
| 1 | 00:00 — 00:24 (24s) | `run.py --scenario 7` | 0.27s | 75 | 18.8s | +4.9s |
| 2 | 00:24 — 00:50 (26s) | 无（编辑器开证据文件） | — | 91 | 22.8s | +3.2s |
| 3 | 00:50 — 01:38 (48s) | `kb.experiment` + `cat run.log` | 0.52s | 169 | 42.3s | +5.2s |
| 4 | 01:38 — 02:08 (30s) | 无（回看主终端） | — | 107 | 26.8s | +3.2s |
| 5 | 02:08 — 03:00 (52s) | 无（回看主终端） | — | 190 | 47.5s | +4.5s |
| 6 | 03:00 — 03:37 (37s) | 无（回看主终端） | — | 131 | 32.8s | +4.2s |
| **7** | **03:37 — 04:27 (50s)** | **`gen_docs --check` + `sqlite3` + `pytest -k`** | **0.64s** | **182** | **45.4s** | **+4.0s** |
| 8 | 04:27 — 04:58 (31s) | `verify.py` | 0.09s | 111 | 27.8s | +3.1s |
| 9 | 04:58 — 05:26 (28s) | `git diff --stat` | 0.02s | 101 | 25.2s | +2.8s |
| | **合计** | | **1.55s** | **1157** | **289.4s** | |

**总长 5 分 26 秒（326s）**，在 `docs/submission-checklist.md` C 段要求的 3–5 分钟略上方、
但**离规则上限还有富余**（上限见 **OQ-2**，待核实）。

> **上限是上限，不是目标。** T 轮补镜 7 时刻意没有把富余花光：多讲两分钟不会多拿分，
> 评委的注意力才是稀缺资源。要再加内容就**从别的镜里借时间**，不要往后拉总长。

**T 轮的时长改动**（一共 +46s）：

| 改动 | 秒 |
| :-- | --: |
| 新增镜 7（Skill 调用过程，必含项补齐） | **+50** |
| 镜 3 加时：R5 的 `[2/3]` 段实测输出变了，念词从 154 字涨到 169 字 | **+5** |
| 从富余大的镜借回：镜 2 −2、镜 4 −1、镜 5 −3、镜 6 −1、镜 8 −2 | **−9** |
| | **+46** |

命令耗时合计只有 **1.55 秒**，九镜没有一处因等命令而卡顿；
**超时风险全在念词长度上**，每一镜的富余都已留到 ≥ 2.8 秒。
念词是掐着这张表写的 —— **临场加词会超**，要加就先从富余大的镜（3、1、5、6）里借。

> 字数口径说明：T 轮只重数了**被改动的两镜**（镜 3 的念词按实测重写、镜 7 全新），
> 其余七镜沿用上一轮的字数 —— 那几镜的念词一个字没动，重数一遍只会引入抄写误差。

> 参考：`run.py`（场景 1–7 全跑）与 `make_evidence.py` 都在**录制前置**里跑完
> （`bash scripts/demo_preflight.sh`，整条 5 步实测 19.4s），不占分镜时间。

---

## 录制前置（一条命令，实测 19.4s）

```bash
bash scripts/demo_preflight.sh
```

T 轮把原来那 5 条人肉命令收敛成了一条脚本。它**不是跑完就算，而是逐条断言**：

| 步 | 跑什么 | 断言 |
| :-- | :-- | :-- |
| 1 | `python3 -m pytest maos/tests -q` | 从输出**解析**出的条数 == 期望（当前 935），且 0 failed |
| 2 | `python3 run.py` | exit=0 |
| 3 | `python3 run.py --scenario 7` | exit=0，且屏幕上**仍有**镜 5 的 `disposition=replan_channel` 与镜 6 的 `业务状态  : compensated` |
| 4 | `python3 scripts/make_evidence.py` | 落盘 **8 束**（数出来的，不是假定的） |
| 5 | `python3 scripts/verify.py` | 输出含 `RESULT: 7/7 PASS` 且 exit=0 |

**任一条不符 → 非 0 退出，退出码就是出错的步号**，并打印「实际 vs 期望」+ 日志末 5 行
（失败时日志目录保留，成功时清理）。第 3 步那两条 `grep` 是**分镜的哨兵**：
镜 5、镜 6 的画面一旦从代码里消失，前置就红，不会等到录制当天才发现。

期望值可用环境变量覆盖，不必改脚本 —— 这也是自证脚本真会失败的办法：

```bash
MAOS_EXPECT_TESTS=999 bash scripts/demo_preflight.sh     # -> exit=1，指出是第 1 步
MAOS_EXPECT_VERIFY='RESULT: 8/8 PASS' bash scripts/demo_preflight.sh   # -> exit=5
```

**这一跑零出网、不读任何 API key**（全程 `ScriptedModelClient` 路径）。脚本第 0 步会
把这句显式打在屏幕上 —— 评委在没有任何密钥的机器上跑，得到的是同一份确定性结果，
这是卖点，不是免责声明。

🔴 **跑完前置，工作区一定是脏的（`evidence/` 50 行 M）。** 第 4 步一次重写
`evidence/scenario-1..7` 与 `scenario-R5` 全部 50 个文件
（证据首行的 `# generated at <ISO8601> from <sha>` 每跑一次都变，所以**必然**变 M）。
脚本末尾会把脏行数打出来并给二选一提示，但**它不替你做决定**：

- **要么**先把这次重跑 commit 掉，带着干净工作区去录；
- **要么**跑完**立刻** `git checkout -- evidence/` 还原，然后带着干净工作区从头录。

脚本**故意不自动还原** —— 你可能正有未提交的在制品，一条自动还原就把它冲掉了。

> 🔴 **实测的一条非显然事实：还原不会毁掉镜 7。**
> 直觉上「还原 `evidence/`」会把镜 7 要查的 `evidence/scenario-7/maos.db` 一起清掉，
> 于是让人以为必须拖到最后一镜前才敢还原、前八镜只能带着脏工作区录。**不是这样**：
> `maos.db` 是 `.gitignore` 的（未被跟踪），`git checkout -- evidence/` 只还原
> **被跟踪**的证据文件，`.db` 一个字节都不动。T 轮实测：还原后 `git status` 干净，
> 镜 7 的 `sqlite3` 查询照跑不误。
> **所以推荐做法是「跑完前置立刻还原，全程干净工作区」** —— 最后一镜不用临时补动作。

**绝不能带着脏工作区去录最后一镜** —— 那一镜打的是 `git diff --stat`。
虽然那条命令带了 `-- maos/contracts/ maos/core/` 路径限定、脏的 `evidence/` 不会挤进那一屏，
但录制时手滑少打路径限定、或顺手 `git status` 给个空镜头，画面就穿帮：
评委看到的不是「零改动」而是一堆 `evidence/` 行。**干净工作区是这一镜的前提，不是保险。**

🔴 **镜 7 的 `sqlite3` 那条命令依赖前置第 4 步。** `evidence/*/maos.db` 不进 git
（`.gitignore`），干净检出上根本不存在；没跑过 `make_evidence.py` 就敲镜 7 的查询，
屏幕上是（实测原文）：

```
Error: unable to open database "evidence/scenario-7/maos.db": unable to open database file
```

**前置必须先跑完**。
（好消息：`maos.db` 是 ignore 的，它的存在不会让 `git status` 变脏，不影响最后一镜。）

🔴 **`python3 -m maos.kb.experiment` 没有 argparse。** 加任何参数——包括 `--help`——
都不会打用法，而是**直接开跑并写盘**。别指望 `--help` 看用法，也别在录制中途手滑敲它。

终端准备：字号调大到后排能看清，**窗口宽度 ≥ 100 列** —— 状态迁移轨迹（镜 4/5）
与 skill 调用链（镜 7，实测 87 列）两屏都会换行。脚本末尾的检查清单会再提醒一次。

⚠️ **两处与手册原分镜不一致，录制前必须先定**（详见文末「录制前必须确认的三件事」，
已按 `27c9e18` 逐条实跑复核）：Element 房间的状态（现已写成 **A/B 双分支**）、
审批由场景脚本自动驱动。本分镜按**当前代码的真实行为**写，不按手册想象写。

> ✅ 原来的第三处「换渠道 replan 演不出来」**已在整合轮 5 解除** —— Y-4 合入后
> 场景 7 真能演，见镜 5。前置从 6 条 → 5 条（Y-3）→ **T 轮收敛成 1 条脚本**。
> **录制当天仍以实跑为准。**

---

## 00:00 — 00:24　多源诉求进来，DAG 长出来

**命令**

```bash
python3 run.py --scenario 7
```

**镜头**：终端顶部那几行。

**要念的**：一条轴承订单的退款诉求，同时来自工单、客服记录、客户图片三处。
受理 Agent 把它们去重聚合成一个 case，Manager 规划出 4 个任务的 DAG。

**画面上要指出的**（实跑输出）：

```
Plan: FAILED  |  处理客户对轴承订单的退款诉求：政策与金额均无异议，但支付渠道回执异常
  · 受理多源退款诉求并聚合证据                        DONE             attempt=1 risk=L
  · 按下单锁定的政策版本裁定退款资格                     DONE             attempt=1 risk=L
  · 核算退款金额并写财务分录                         DONE             attempt=1 risk=M
  · 发起退款并观察网关终态（改派备用渠道）                  FAILED           attempt=2 risk=M
```

**一句钩子**：注意 Plan 的终态是 `FAILED`，记住这个，最后一镜要回来对它。
第四个任务的名字里带着「改派备用渠道」、`attempt=2` —— 那是**第五镜**的伏笔，
这里不解释，只让它先出现在画面上。

---

## 00:24 — 00:50　政策裁定：命中规则编号，不是「AI 觉得可以退」

**操作**：用编辑器打开证据文件，镜头给到文件内容（比在终端 `cat` 一大段 JSON 好看）：

```
evidence/scenario-7/business-objects.json    # 政策裁定产物：规则编号 + 版本 + 依据
```

> 提示：证据文件**首行是 `# generated at ...` 注释**，用 `json.load` 直接读会炸。
> 仓库里的读取一律走 `verify.py::load_evidence_json`（它会跳过首行）。演示时打开
> 看就行，不需要解析。

🔴 **这个文件里有两组对象，录制时别混。** 场景 7 跑的是两个 plan：`task-s7-*`
（订单 `ord-s7-88231`，`amount_paid` **6800.0**）与 `task-s7b-*`（订单 `ord-s7-88232`，
**5200.0**）。念词里说的是 **6800** 那一组，**政策对象和订单对象必须取同一组**。

🔴🔴 **绝对不要背这个文件的行号 —— 它每次重跑都可能整体换位。**
T 轮实测：连跑三次 `make_evidence.py`，`amount_paid: 6800.0` 分别落在
**第 41 行 → 152 行 → 152 行**，两组对象的先后顺序**不稳定**。而录制前置
（`demo_preflight.sh` 第 4 步）**必然重跑一次** —— 也就是说，**录制当天看到的顺序是掷骰子**。
上一版分镜写的「第 4–22 行附近」在当时的检出上是对的，跑一次前置就可能不对。

**所以录制前现场定位，按 `task_id` 认组，不按行号**：

```bash
grep -n '"task_id"\|"object_type"\|"amount_paid"' evidence/scenario-7/business-objects.json
```

在输出里找到 **`"task_id": "task-s7-finance"`**（注意**不是** `task-s7b-finance`），
它下面紧跟的就是本镜要给的 `policy_rule`；再找 **`"task_id": "task-s7-intake"`** 下的
`order_snapshot`，`amount_paid` 是 **6800.0** 的那个。两者在文件里**相邻**，
编辑器里正好一上一下同屏 —— 相邻这件事是稳定的，**绝对行号不是**。

**镜头**：`business-objects.json` 里这两个对象，一上一下对着看。

政策规则对象（`task-s7-finance` 名下那个；`task-s7-policy` 另引一份，内容相同）：

```json
      "object_type": "policy_rule",
      "object_id": "AS-01",
      "object_version": 1,
      "purpose": "裁定依据的政策规则",
      "resolved": true,
      "object": {
        "rule_no": "AS-01",
        "version": 1,
        "title": "整机质量问题全额退款",
        "body": "{\"deduct_fee\": 0, \"refund_ratio\": 1.0}",
```

订单对象（同一文件内，注意最后那个字段）：

```json
        "order_id": "ord-s7-88231",
        "sku": "SKU-BRG-6204",
        "amount_paid": 6800.0,
        "policy_version_at_order": 1,
```

**要念的**：政策裁定给出的是**规则编号加版本号** —— `rule_no` 是 `AS-01`，
`version` 是 1，`purpose` 那行写着「裁定依据的政策规则」。而订单对象上钉着
`policy_version_at_order` 等于 1：依据的是**下单当时锁定的那一版政策**，
不是今天生效的那一版。政策改版不会追溯改判历史订单。

> 🔴 **不要在这一镜打开 `result.json`。** 实测它里面政策任务那一段只有任务骨架
> （`task_id` / `role` / `title` / `state` / `attempt` / `risk_level` / `effect_risk`），
> **一个字的裁定内容都没有，`AS-01` 在这个文件里出现 0 次**。裁定产物只在
> `business-objects.json` 里。
>
> `AS-01@v1` 这个**带 `@` 的字面写法只出现在终端**（场景 7 输出的「语义审查」那一行：
> `依据 AS-01@v1`），证据文件里是 `rule_no` 与 `version` 两个字段分开写的。
> 念的时候说「规则编号加版本」，别指着 JSON 说「你看这里写着 AS-01@v1」—— 那里没有。

---

## 00:50 — 01:38　RAG：历史案例改变了计划本身

**命令**（两条，缺一不可）

```bash
python3 -m maos.kb.experiment
cat evidence/scenario-R5/run.log
```

🔴 **第一条命令屏幕上只有 3 行。** `27c9e18` 实测（stdout 2 行 + stderr 1 行，
终端上混在一起）：

```
[task-nokb-intake] plan_defect —— 机器返工修不好，一次转人工，不再重发

证据束已落盘：/…/evidence/scenario-R5
```

> ⚠️ **上一版分镜写的是「只打两行」，那已经过期。** 第一行是 stderr 上的一条
> 日志（`logging` 打到 stderr），T 轮实测它确实会上屏 —— 它说的正是 `[2/3]` 段
> 那个「机器返工修不好，转人工」的出口，**别把它当成报错**。
> 重定向验证：`stdout` 2 行、`stderr` 1 行，两者顺序可能因缓冲而互换。

三段对照全部被写进了 `evidence/scenario-R5/run.log`，**不上屏**。
所以必须敲第二条 `cat` 才看得见下面这些内容 —— 第一条命令的屏幕上没有对照实验的任何数字。

**镜头**：`cat run.log` 的输出，重点在 `[2/3]`、`[3/3]` 和最后两行。

```
场景 R5：RAG 有无对照实验，无 key 确定性复现

[1/3] 准备段：跑一条完整成功的退款 case，收口后按晋升规则沉淀知识
  Plan DONE，业务状态 settled，客户 ack 1 条
  晋升：kb-r5-history-0001 kind=history_case outcome=success source_case_id=case-r5-hist

[2/3] without_kb：MAOS_KB_ENABLED=0，计划漏排财务核算
  4 个任务，KbRetrieved 0 条，第六道闸 blocker，Plan FAILED，业务状态 submitted
  第六道闸：计划缺陷 blocker（漏排财务核算，付款前拿不到金额凭据）
  第三出口：受理多源退款诉求并聚合证据 -> BLOCKED（await=human_decision，机器返工修不好，转人工）
  主管裁决：驳回 -> 计划收敛到 FAILED，付款一次都没派发

[3/3] with_kb：MAOS_KB_ENABLED=1，同一份计划脚本
  5 个任务，KbRetrieved 1 条，第六道闸 pass，Plan DONE，业务状态 settled
  人工审批停点：['核算退款金额并写财务分录']

差异：delta_tasks=['refund_finance.refund_finance']，触发文档=[('kb-r5-history-0001', 0.719031), ('kb-policy-tnt-mfg-a-AS-002-v1', 0.410541), ('kb-policy-tnt-mfg-a-AS-004-v1', 0.15), ('kb-policy-tnt-mfg-a-AS-001-v1', 0.1)]
检索漏斗：库存 17 条 -> 同租户 9 条 -> 七维预过滤后 5 条（with_kb 段实测候选集 5 条）
```

**要念的**：唯一的变量是 `MAOS_KB_ENABLED`，同一份计划脚本。关掉检索，计划漏排了
财务核算 —— 第六道闸直接判计划缺陷 `blocker`，走第三出口转人工，主管驳回，
**付款一次都没派发**，Plan 收在 `FAILED`；打开检索，命中历史案例
`kb-r5-history-0001`，分数零点七二，计划**自己补上了财务核算这一环**，
四个任务变五个，走到 `settled`。最后一行是检索漏斗：库存十七条，同租户九条，
七维预过滤后剩五条候选。

> 🔴 **这段念词 T 轮改过，别照上一版念。** 上一版说的是「付款那一步被『金额未经核算
> 不许发起』拦下」，屏幕上曾经确实有一行 `LookupError: ... 没有 finance_entry`。
> `27c9e18` 实测**那行已经没有了**：拦截点从「付款时抛异常」前移成了
> 「第六道闸判计划缺陷 → 第三出口转人工 → 主管驳回」，**付款任务一次都没被派发**。
> 新口径比旧的更强（问题在规划阶段就被发现，而不是等执行到付款才炸），
> 但**旧念词现在是在描述一个屏幕上不存在的画面**，念了就是演不存在的功能。

**这就是「历史流程知识改善规划质量」的可核验形态**：两版 DAG 的 diff。

特写（时间不够就跳过，念词里已带过）：`evidence/scenario-R5/dag-diff.json` 与
`kb-hits.json`，命中的 doc_id 与分数。

> ⚠️ **本镜的数字会变**：检索质量收口（FTS5 修复 + 真语料接入）仍在进行中，
> 语料换一次，命中条数、分数、漏斗三档都会变。**录制当天现跑一次，
> 以当时 `run.log` 为准，不要照抄本文件的数字。**
>
> ⚠️ Y 轮易变：Y-2 轨在补场景 6 的 RAG 检索（当前场景 6 的 `candidate_count=0`）。
> 那一轨只动场景 6，**不改本镜的 R5 对照实验**；合并后若想加演场景 6，本镜要重新掐表。

---

## 01:38 — 02:08　第六道闸：漏了财务凭据就过不去

**镜头**：回到主终端（场景 7 的输出），指状态迁移轨迹里的这两行。

```
    task-s7-finance  AWAITING_REVIEW  -> BLOCKED          [gate_needs_human]
    task-s7-finance  BLOCKED          -> DONE             [human_approve]
```

**要念的**：七道闸按顺序跑 —— schema、验收、安全、证据、补偿干跑、财务复核、网关回执。
第六道闸只认两个数据形状：任务入参里的申报金额，和产物里的财务凭据。
**它不查退款域的任何一张表、不 import 业务域**，所以换个域这道闸一行都不用改。
金额超阈值加影响面高，闸全过也停 `BLOCKED`，等人。

---

## 02:08 — 03:00　支付执行：先换一次渠道，再停手

> 本镜在整合轮 5 合入 Y-4 之前只有「问不出终态就转人工」可演（旧称 A 版）。
> **Y-4 已合入，屏幕上真的有换渠道那一段了**，以下输出全部照实跑粘贴，非构造。

**镜头**：主终端的业务输出 `[2]`、`[3]` 两段 —— 这是本镜的主画面。

```
[2] 第七道闸认出网关回执: code=40005 retriable=True outcome=failed -> disposition=replan_channel
    网关回执 40005（调用频次超限）：retriable=True / outcome=failed —— 网关在入口就拒了，这一笔业务确定没执行，可以换渠道重发。官方处置：降低请求并发量
    → 触发 replan 重规划：付款任务换渠道 s7-primary -> s7-backup；幂等键由 (tenant, case) 定，换渠道不换键，不会造出第二笔

[3] 备用渠道付款回执: observed_state=unknown（问了 3 次仍非终态）
    网关判据: code=ACQ.SYSTEM_ERROR retriable=True outcome=unknown
    官方处置: 保持参数不变重试或查询执行结果
    出处    : https://aipay.alipay.com/docs/mobile-app-pay/ai-app-pay/alipay-trade-refund.html#业务错误码
    重规划   : 已换渠道 1 次（replan 上限 MAOS_MAX_REPLAN 默认 2）；这一格 outcome=unknown 对 replan 一票否决，**不再换第三个渠道** —— 重发可能真退出第二笔，正确出口是人工
```

**再给一眼状态迁移轨迹**（同一屏往下滚，换渠道在状态机上留的痕）：

```
    task-s7-payment  AWAITING_REVIEW  -> REWORK           [gate_rework]
    task-s7-payment  REWORK           -> PENDING          [requeue]
    task-s7-payment  PENDING          -> DISPATCHED       [dispatch]
    task-s7-payment  DISPATCHED       -> RUNNING          [claim]
    task-s7-payment  RUNNING          -> AWAITING_REVIEW  [submit_result]
    task-s7-payment  AWAITING_REVIEW  -> BLOCKED          [gate_needs_human]
    task-s7-payment  BLOCKED          -> FAILED           [human_reject]
```

**要念的**：第一次付款发出去，主渠道返 `40005`，调用频次超限。第七道闸认出这个码：
可重试、而且业务确定没执行，判定换渠道重发。系统自己把付款任务从主渠道改派到备用渠道，
幂等键由租户和案子定，换渠道不换键，不会造出第二笔。备用渠道这一笔返
`ACQ.SYSTEM_ERROR`，语义是说不清结果，不是失败，轮询三次仍问不出终态。
这一格对重规划一票否决，不再换第三个渠道 —— 重发可能真退出第二笔钱，正确出口是人工。
所以屏幕上是换一次渠道就停手，不是重试到上限；上限是二，它只用了一次。

> 🔴 **本镜唯一会讲错的地方：别把它说成「重试到上限」。**
> `MAOS_MAX_REPLAN` 默认 **2**，这一镜只用了 **1** 次就停了 —— 停手不是因为撞上限，
> 是因为第二个错误码 `outcome=unknown` 对换渠道**一票否决**。
> 把「1 次」念成「到上限」是说过头，且正好把这一镜最值钱的判据（**知道什么时候不该重试**）
> 讲丢了。收口那一屏的 `换渠道重试: 1 次 replan（40005 触发，ACQ.SYSTEM_ERROR 一票否决，
> 没有自旋）` 就是这句话的证据，下一镜会给到。

> 录制当天仍要现跑一次照实对屏：`python3 run.py --scenario 7`，
> 上面两段输出必须逐字出现。对不上就以当天输出为准重贴，**不许照本文件念**。

---

## 03:00 — 03:37　补偿与收口：从未进入 settled

**镜头**：主终端最后五行 —— 这是整个 Demo 的题眼，停久一点。

```
  业务状态  : compensated（全程没有经过 settled）
  settled 观察: 0 条 —— 没问出终态就一条都不该有
  补偿记录  : 2 行 ['manual_ticket', 'refund_request_revoked']
  补偿事件  : 1 条 CompensationExecuted
  Plan 终态 : FAILED（主管驳回，业务确实没成功）
```

**要念的**：主管驳回，补偿执行 —— 撤销退款请求、开人工工单。业务状态收在
`compensated`，**全程没有经过 `settled`**。四个 Agent 全都回复了「完成」，
而这一单**没有成功**，系统如实这么记了。这就是评委那句
「所有 Agent 都回复完成不等于业务成功」的正面回答。

**顺带指一句**：`settled` 全系统只有 `payment.observe` 写得进去，还必须同事务附上回执。
越权写入不是静默失败，是抛异常加落一条 `AuthoritativeFactViolation` 事件。

---

## 03:37 — 04:27　Skill 调用过程：注册表、审计链、越权边界

> **T 轮新补的一镜。** 补之前这份分镜全文一次都没提到 Skill，而「Skill 调用过程」
> 是规则的必含项（OQ-1，待核实）。这一镜要在 50 秒里把三件事讲完：
> **Skill 是什么（实体）→ 被怎么调用（审计）→ 调不了什么（边界）**。

**命令**（三条，按顺序敲）

```bash
python3 scripts/gen_docs.py --check
sqlite3 -header -column evidence/scenario-7/maos.db "SELECT seq, task_id, json_extract(detail,'$.skill') AS skill, json_extract(detail,'$.version') AS ver, json_extract(detail,'$.status') AS status, json_extract(detail,'$.duration_ms') AS ms, substr(json_extract(detail,'$.input_digest'),1,10) AS input_digest, substr(json_extract(detail,'$.invocation_id'),1,10) AS invocation_id FROM event_log WHERE event_type='SkillInvoked' AND task_id LIKE 'task-s7-%' ORDER BY seq"
python3 -m pytest maos/tests -k "unlisted_skill or whitelist_still_bites or outside_whitelist" --no-header -v
```

> 第二条太长，**事先写进一个 shell 别名或贴板，录制时粘贴**，不要现敲。
> SQL 里的 `$.skill` 在 bash 双引号里不会被展开（`$` 后面不是合法变量名字符），
> 实测原样传给 sqlite3，**不需要转义**。
> 依赖：`evidence/scenario-7/maos.db` 由录制前置第 4 步产出（见「录制前置」末条）。

---

### 第一段（约 17s）　镜头：`gen_docs --check` 的输出，外加 `docs/skill-catalog.md` 的「一览」表

`27c9e18` 实测（0.15s）：

```
[OK]    docs/agent-identity.md
[OK]    docs/skill-catalog.md
[OK]    docs/toolport-contract.md

3 份文档与代码逐字节一致。
```

编辑器里同时开着 `docs/skill-catalog.md`，镜头给「一览」那张表的表头与前几行 ——
13 行，每行 `skill / 版本 / 域 / 归属角色 / 失败策略 / 依赖工具 / 声明位置`。

**要念的**：Skill 在这里是**注册表实体**，不是提示词。十三个，每个带版本号和九项要素：
输入输出、失败策略、依赖工具、安全边界。这张目录从代码里的契约实例生成，
**改了代码不重跑就红**。

---

### 第二段（约 19s）　镜头：`sqlite3` 那一屏 —— 本镜的主画面

`27c9e18` 实测（0.01s，87 列宽，100 列窗口不换行）：

```
seq  task_id          skill              ver    status  ms  input_digest  invocation_id
---  ---------------  -----------------  -----  ------  --  ------------  -------------
4    task-s7-intake   issue.aggregate    1.0.0  ok      0   940c6d861a    18a47d315c
5    task-s7-intake   refund.intake      1.0.0  ok      3   830207d259    cf3ca5c265
10   task-s7-policy   policy.match       1.0.0  ok      0   4ab7a84eb4    f80cea3639
15   task-s7-finance  policy.match       1.0.0  ok      0   4ab7a84eb4    7b4f3f9820
16   task-s7-finance  finance.settle     1.0.0  ok      0   3567ab004e    4242a25ec5
25   task-s7-payment  payment.execute    1.0.0  ok      3   925b464e09    e8d0707b83
27   task-s7-payment  payment.observe    1.0.0  ok      1   296b1d1016    ca884e546d
37   task-s7-payment  payment.execute    1.0.0  ok      2   c8b314b158    0dffcbd196
41   task-s7-payment  payment.observe    1.0.0  ok      1   faa2211101    cab2044c06
46   task-s7-payment  refund.compensate  1.0.0  ok      1   996d286b93    8e99f2808c
```

**要念的**：这是同一条退款 case 的调用链，从 `event_log` 查出来：**七个 skill、十次调用**，
每次一个 `invocation_id`。注意 `payment.execute` 出现两次 —— 主渠道一次、备用渠道一次，
**就是刚才那次换渠道**。同一个 skill，不同入参，两条独立审计记录。

**镜头上可以指、但不必念的两处**（问到了再答）：

- `seq 25` 与 `seq 37` 的 `input_digest` 是 `925b464e09` 和 `c8b314b158`，**不一样** ——
  换渠道换的是入参，不是换了个 skill。
- `seq 10` 与 `seq 15` 的 `policy.match`：`input_digest` **完全相同**（`4ab7a84eb4`），
  `invocation_id` 却是两个 —— 政策任务和核算任务各自独立调了一次，同样的问题问两遍，
  两次调用各自留痕。

🔴 **`invocation_id` 与 `input_digest` 每次重跑都会变**（`invocation_id` 是 uuid4）。
上面这一屏是 `27c9e18` 某一次跑的实测值，**录制当天以现跑输出为准，不许照抄本文件的十六进制**。
不变的是结构：七个 skill、十次调用、`ver` 全 `1.0.0`、`payment.execute` 两行。

---

### 第三段（约 9s）　镜头：`pytest -k` 的三行测试名

`27c9e18` 实测（0.48s）：

```
maos/tests/test_agents_gate.py::test_identity_whitelist_still_bites PASSED [ 33%]
maos/tests/test_registry_autodiscovery.py::test_unlisted_skill_raises_permission_denied PASSED [ 66%]
maos/tests/test_skills.py::test_invoke_outside_whitelist_raises_permission_denied PASSED [100%]

====================== 3 passed, 746 deselected in 0.32s ======================
```

**要念的**：白名单外的 skill 一律拒绝，三条测试守着。**Agent 干不了什么，
和干了什么一样是设计的一部分。**

> 机制在 `maos/skills/invoker.py`：`invoke()` **第一件事**就是校验
> `name ∈ identity.allowed_skills`，不在则抛 `PermissionDenied` ——
> 校验在注册表查询**之前**，所以「skill 存在但你没权限」和「skill 不存在」
> 是两条不同的路径。`test_identity_whitelist_still_bites` 守的正是这个顺序。

---

> 🔴 **这一镜的红线：只讲屏幕上有的。**
> - **不要**说「Skill 可以热插拔上线」—— 屏幕上没演过注册一个新 skill。
> - **不要**说「越权会告警到房间」—— 屏幕上只有 `PermissionDenied` 和三条绿测试。
> - **不要**把 `duration_ms` 的 0 和 1 说成「性能优化的结果」—— 那只是 Scripted 模式没有网络往返。
> - 版本号全是 `1.0.0`，**不要**说「我们已经迭代过多个版本」；`kb.retrieve` 是唯一
>   的 `1.1.0`，但它不在这条退款链上，这一屏里看不到。

---

## 04:27 — 04:58　一条命令，评委自己核验

**命令**

```bash
python3 scripts/verify.py
```

**镜头**：七行 `[PASS]` 与最后的 `RESULT` 行。

🔴 **屏幕上不止七行。** `27c9e18` 实测这七行之间夹着 **12 条 `· warn:` 缩进行，3 类**：
`authoritative-fact` 下 **2** 条、`trace-tree` 下 **6** 条、`business-outcome` 下 **4** 条。
读数变过三次：17 条（整合轮 5 前）→ 10 条（Y-1/Y-2 各消掉一类）→ 11 条（Y-4）→ **12 条**
（`authoritative-fact` 下从 1 条变 2 条：场景 7 的两个 plan 各留一条
「有回执但 `biz_status` 不是 settled」）。
**别照抄旧读数**；这些行是**预期内**的，判定仍 7/7。
录制时**往下滚到 `RESULT: 7/7 PASS` 那一行再停住**，镜头给结论行。
被问到 warn 就照实答：**warn 不影响判定，七项全 PASS，退出码 0**；
warn 说的是产物来源可审计性，出处已记在 `docs/BACKLOG.md task-X4`。

七行实跑值（`27c9e18` 实测，T 轮重跑复核 —— 与上一轮 `2474c56` 逐字一致）：

```
[PASS] hash-integrity       86/86
[PASS] business-ref         35/35
[PASS] authoritative-fact   3/3
[PASS] trace-tree           29/29
[PASS] kb-hit               7/7
[PASS] business-outcome     10/10
[PASS] history-case         1/1

RESULT: 7/7 PASS
```

末尾还有一行，一并给到镜头：

```
证据来源：scenario-1, scenario-2, scenario-3, scenario-4, scenario-5, scenario-6, scenario-7, scenario-R5
```

**要念的**：这不是我们自己说跑通了。第一项重放每一次调用的哈希，改一个字节就红；
第三项验每一个 `settled` 都有回执、且回执出自 `payment.observe`；第六项验每个 Plan
终态都有外部判据。**任一项 FAIL，退出码非零。**
检索不准顶多说效果一般，无法核验就是零分。这条命令可以交到评委手里、由他自己跑。

> 数字随证据束重跑会变（比如新增一个场景），**录制当天以现跑输出为准**。
>
> ⚠️ **上一版这里挂着「Y 轮易变：Y-1 会让那 4 条 warn 自动消失」—— T 轮实测没消失。**
> `27c9e18` 上 `business-outcome` 下仍是 4 条「外部判据来源未审计」，`trace-tree` 下
> 仍是 6 条「产物没有来源事件」。这类 warn 说的是**产物入库路径的可审计性**，
> 不是核验失败；出处记在 `docs/BACKLOG.md task-X4`。**别把它当成待办追**，
> 也别在录制前指望它归零。

> **衔接上一镜**：第 1 项 `hash-integrity` 重放的哈希里，就包含镜 7 那一屏里
> 每一条 `SkillInvoked` 的 `input_digest` 与 `output_hash` —— 镜 7 给的是调用留痕，
> 这一镜给的是「留痕改一个字节就红」。被问到两镜什么关系，答这句。

---

## 04:58 — 05:26　一屏总结：同一个内核，两个域

**镜头**：切到 `docs/domain-portability.md` 的对照表，或一张事先做好的图。

**要念的**：本仓库在两个完全不同的领域上给出可运行实证：软件交付域看真实测试结果，
退款域看支付到账回执。**换域只换 Skill、ToolPort 与业务对象。**

最后一屏打这一条命令的输出：

```bash
git diff --stat 90251b3 4a70cb0 -- maos/contracts/ maos/core/
```

- `90251b3` = P2 四轨收口，**退款域上线前**
- `4a70cb0` = 整合轮 3 收口，**退款域完整上线（含场景 7 与 RAG）、X 轮尚未开工**

**实测：两个目录都是空输出**（真零改动，不是「差不多没改」）。

**要念的**：契约与 Control Plane 在退款域上线前后**一个字节没变**。
第七道闸是**之后**为通用重规划判据加的，同样不 import 任何业务域。

> 🔴 **不要用旧区间 `90251b3..df96fa8`。** 实测那一对的 `maos/core/` 不是空的
> （`control_plane.py`，`1 file changed, 46 insertions(+), 2 deletions(-)`），
> 因为它混进了 X-2 / X-4 的非退款域改动，上台还得补一句「别把它念成零」。
> 换成上面这一对，两个目录双空，念词干净。
>
> 口径细节以 Z-3 定稿的 `docs/domain-portability.md` §2 为准。
> **本轨不改那个文件**，两轨用的是编排侧钉死的同一对端点；
> 两边措辞需在整合轮 5 对齐一次。

---

## 录制前必须确认的三件事

**已按基线 `27c9e18` 逐条实跑 / Read 代码复核（2026-08-29 · T 轮），不是照抄旧结论。**

| # | 事项 | 复核结论（`27c9e18`） | 影响哪一镜 | 处置 |
| :-- | :-- | :-- | :-- | :-- |
| 1 | **Element / Matrix 房间** | **变了：基建到位，证据未到位。** `hiclaw/room_demo.py` 实测**已存在**（15 KB；`evidence/room/README.md` 里「未交付」那句已过期）；但本机 `import nio` 仍 `ModuleNotFoundError`，`_NioChannel`（`hiclaw/matrix_bus.py:258`）恒走 `ImportError` → 降级 `log_only`；`evidence/room/` 下 **0 张截图**，`transcript.md` 自己写着「🔴 空 —— 房间未接通」 | 手册原分镜的 00:00「房间里看拆解」与 02:00「审批卡 /approve」；本分镜的 **AgentTeams 状态展示**这一必含项 | **写成 A/B 双分支，见下一节。** A 分支今天就能录；B 分支等 T4 轨交出真房间证据后启用 |
| 2 | **网关错误码 → replan 换渠道** | ✅ **已能演，T 轮重跑仍成立。** 实跑 `python3 run.py --scenario 7`（`27c9e18`）屏幕上有完整的换渠道段：`disposition=replan_channel` → `s7-primary -> s7-backup` → 状态轨迹里的 `gate_rework` / `requeue`，收口行打 `换渠道重试: 1 次 replan`。⚠️ **`test_replan_gateway.py` 现在是 23 passed，不是 19** —— 上一版写的 19 已过期 | 02:08 那一镜 | **照该镜念，输出已是实测值。** 红线不变：**别念成「重试到上限」** —— 只 replan 了 1 次，上限是 2 |
| 3 | **审批由谁驱动** | **仍是场景脚本自动驱动，但行号全漂了。** `hq.decide()` 实测在 `maos/flows/scenario_7.py:498`（finance，`approved=True`）与 `:535`（payment，`approved=False`）；另有 `:605` / `:635` 是第二个 plan（`task-s7b-*`）的同一对。`decide` 本体在 `maos/runtime/gate.py:808`。**上一版写的 `:318` / `:351` / `:586` 三个行号全部作废。** 不是真人在房间里发命令 | 01:38 与 03:00 两镜 | 念词用「主管审批」没问题（那确实是 HITL 停点），但**别说「我现在在聊天室里点一下」** —— 除非第 1 项走到了 B 分支并真的现场发命令 |

> ⚠️ **行号会再漂一次。** 上表第 3 行的行号按 `27c9e18` 核准，而 T1 / T2 / T3 轨正在
> 改 `maos/` 下的源码（含 `maos/core/control_plane.py`）。**合并 T1/T2/T3 后行号需重核** ——
> 见文末「待整合轮回填」。核的办法就一条：`grep -n "hq.decide" maos/flows/scenario_7.py`。

---

## 第 1 件事的 A/B 双分支：AgentTeams 状态展示怎么录

必含项里的「AgentTeams 状态展示」有两条路。**A 是保底，今天就能录；B 更强，等 T4。**
两条都掐好了表，切换时不用重排别的镜。

### A 分支（保底 · 今天就能录 · 已计入上面的时间轴）

**不加镜、不加时长。** AgentTeams 状态落在**已有的镜 1 与镜 4／5**上：

| AgentTeams 概念 | 屏幕上是什么 | 哪一镜 |
| :-- | :-- | :-- |
| Team 成员当前在干什么 | 镜 1 Plan 表的 4 行：任务 × 状态 × `attempt` × `risk` | 镜 1 |
| 事件链 / 消息流 | 状态迁移轨迹逐条打印（`dispatch` / `claim` / `submit_result` / `gate_rework` / `requeue`） | 镜 4、镜 5 |
| 人工介入 / HITL 停点 | 同一份轨迹里的 `gate_needs_human` → `human_approve`（镜 4）与 `human_reject`（镜 6） | 镜 4、镜 6 |
| 可观测 / 回放 | `event_log` → span 树，`verify.py` 第 4 项 `trace-tree` 重放校验 | 镜 8 |

**口径只能是**「镜像层已实现、降级路径实测等价、真房间截图待补」。

🔴 **不要摆拍一个假房间**，也不要加 `--matrix` 演 —— 它只会走降级 `log_only`，
屏幕上没有房间。（T 轮实测复核：`import nio` 仍 `ModuleNotFoundError`，这条红线**原样成立**。）

> A 分支要多说的**一句话**（不占额外时长，塞进镜 4 的富余里）：
> 「这些事件同时会镜像进一个 Matrix 房间，人在 Element 里能看见并直接 `/approve`；
> 房间是**旁路**，权威记录在 `event_log` 里 —— 今天演的是权威那一路。」
> 这句每个字都能在 `docs/agentteams-mapping.md` 找到依据，**不涉及任何未实测的画面**。

### B 分支（更强 · 待 T4 交出真房间证据后启用）

**前置条件**（三条全满足才切，缺一条就留在 A）：

1. `evidence/room/` 下有**真截图**：至少 `01-approval-card.png`、`03-approve-effect.png`、
   `04-reject-compensation.png` 三张（README 的清单共 5 张）；
2. `evidence/room/transcript.md` **不再是**「🔴 空 —— 房间未接通」那一节，而是逐字副本；
3. 录制机上 `python3 -c "import nio"` **不报错**，且 `~/.maos-matrix/STATUS` 不是 `PENDING`。

**换成什么命令 / 画面**：

| | A 分支 | B 分支 |
| :-- | :-- | :-- |
| 镜 1 | `python3 run.py --scenario 7`，看终端 Plan 表 | `python3 run.py --scenario 7 --matrix`，**画面切 Element 房间**，看事件逐条刷进来 |
| 镜 4 的审批停点 | 终端轨迹里的 `gate_needs_human` → `human_approve` | 房间里**现场手打** `/approve task-s7-finance`，镜头给回执 `已批准 …（操作人 @boss:…）` |
| 镜 6 的驳回 | 终端轨迹里的 `human_reject` | 房间里现场 `/reject task-s7-payment 回执非终态`，镜头给补偿执行 |

**念词怎么改**：把 A 分支那句「今天演的是权威那一路」换成
「左边是房间，右边是 `event_log` —— 房间给人看，`event_log` 是权威，两边对得上」；
第 3 件事的红线（「别说我现在在聊天室里点一下」）**解除**，因为那时是真在点。

**时长差**：**+12s ~ +18s**（切窗口 2s × 3 次 + 手打命令等回执约 6~12s），
总长从 5:26 变成约 **5:38 ~ 5:44**。仍在规则上限内（OQ-2，待核实），但要
**从镜 3、镜 5 的富余里再借 6s** 才能压回 5:30 附近。

🔴 **B 分支一个字都不许提前写进念词。** 上面这一格是**切换指引**，不是已完成的分镜。
截图没到位就录 A —— 见 A 分支那条红线。

> **本轨（T6）不改 `evidence/room/**`、`docs/matrix-room-runbook.md`、
> `docs/agentteams-mapping.md`** —— 那是 T4 的面。上面对房间现状的判断全部来自**只读实测**。

## 与手册原分镜的对照

手册（`docs/EXECUTION.md:744` 起 —— 上一版写的 `:724` 已过期）给的是 8 个镜头，
本分镜是 **9 镜**。差异三处，都有依据，**没有一处是删戏**：

| # | 差异 | 为什么 |
| :-- | :-- | :-- |
| 1 | **多出一镜**：镜 7「Skill 调用过程」，手册 8 镜里没有 | 规则把「Skill 调用过程」列为**必含项**（OQ-1，待核实），手册那 8 镜一条都没覆盖。这是补必含项，不是加戏 |
| 2 | 手册 02:30 的「replan → 换渠道 → **达上限** → needs_human」在屏幕上是「换渠道 **1 次** → 第二个错误码一票否决 → 转人工」 | Y-4 合入后换渠道真能演了，但**只 replan 了 1 次**（上限是 2）。按手册原话念就是把 1 说成上限，见镜 5 的红线 |
| 3 | 手册的 Element 画面全部改走终端 | 见「三件事」第 1 条与 A/B 双分支；截图到位后按 B 分支切回房间 |

其余七镜与手册一一对应。本版为控制总长重排过时间轴，
每一镜的**内容顺序与手册一致**，起止时间点前后差不超过 40 秒。

---

## 整合轮 5 收口台账（2026-08-29）

> 🔴 **本节及下一节的镜号是当时的 8 镜编号，不是现在的 9 镜编号。**
> T 轮在镜 6 之后插了新的镜 7（Skill），所以：**当时的「镜 7」= 现在的镜 8**（`verify.py`），
> **当时的「镜 8」= 现在的镜 9**（一屏总结）。镜 1–6 编号未变。
> 两节里出现的**时间点**（`00:52`、`03:24` 之类）同样是当时的时间轴，一律以抬头
> 那张耗时表为准。两节都是历史台账，**故意不改** —— 改了就不再是当时的记录了。

Y-1 / Y-2 / Y-3 已并入，下面四条**已回填并实跑复核**：

| # | 镜号 | 原来写的 | 现在 | 依据 |
| :-- | :-- | :-- | :-- | :-- |
| 1 | 录制前置 | 6 条命令（`make_evidence.py` 与 `kb.experiment` 分两条敲） | **5 条**，⑤ 缺省一并产 R5；脏行数写实为 50 行 | Y-3 |
| 2 | 00:52 | R5 命中 4 条（首条 0.719031）、漏斗 17→9→5 | **未变** —— 合并后重跑，整段 `run.log` 与本文件贴的**逐字节一致** | 实跑复核 |
| 3 | 镜 7（当时 03:24） | 「夹着 17 条 warn」 | **10 条**（`trace-tree` 下 6、`business-outcome` 下 4）；B/C 两类已归零 | Y-1 + Y-2 |
| 4 | 镜 7（当时 03:24） | 七行读数 `74/74`…`kb-hit 4/4` | **`77/77`…`kb-hit 7/7`**，全新克隆实测 | 证据束重跑 |

**镜头时长未受影响**：改动只落在「录制前置」（不占分镜时间）与两处屏幕读数，
念词字数没动，**Y-3 这批不改任何镜头时长**，耗时表不因这批重算。

> ⚠️ 本节记的是 **Y-4 合入之前**的状态，当时总长确是 4:25。Y-4 改长镜 5 之后
> 总长已变为 **4:40（280s）**，见下一节第 5 行与抬头耗时表 —— 本行结论只对 Y-3 这批成立。

**一个可选增演（留给录制当天定）**：Y-2 让场景 6 的 RAG 候选集从 0 涨到 3，
现在场景 6 也有检索可演了。要不要加这一镜是内容取舍 —— 加了要重新掐表、其后各镜顺延，
**本轮不擅自改**；不加也不影响任何一句念词的真实性（R5 那一镜已经把 RAG 讲透了）。

---

## 补合 Y-4 后的第二批回填（2026-08-29）

Y-4 已并入（`783d9dd`），下面四条**已按实跑回填**：

| # | 镜号 | 原来写的 | 现在 | 依据 |
| :-- | :-- | :-- | :-- | :-- |
| 1 | 镜 5 | **A/B 两版并存**，B 版屏幕输出「无实测值可贴」 | **合并成一版**，`[2]`/`[3]` 两段业务输出与状态迁移轨迹全部照实跑粘贴 | Y-4 |
| 2 | 镜 1 | 第四个任务写「发起退款并观察网关终态 / attempt=1」 | 实测已变成「（改派备用渠道）/ **attempt=2**」，并把它明写成镜 5 的伏笔 | Y-4 |
| 3 | 镜 7 | warn 10 行 2 类、`hash-integrity 77/77` | **11 行 3 类**、**`81/81`** | Y-4 + 证据束重跑 |
| 6 | 镜 7 | 七行读数 `81/81` `33/33` `18/18` `9/9`（`147df03` 实测） | **`86/86` `35/35` `19/19` `10/10`**（`2474c56` 实测）；warn **12 行 3 类** | 整合轮 6 合 D-1 + D-2 后证据束重跑 |
| 7 | 录制前置 | `应 596 passed` | **`应 645 passed`** | D-1 带 26 条、D-2 带 23 条 |
| 8 | 录制前置 | `应 645 passed` | **`应 703 passed`** | 整合轮 8 收敛：C/E/G1 侧 46 条 + G-2 的 12 条并入 |
| 9 | 录制前置 | `应 703 passed` | **`应 749 passed`** | 整合轮 9 合入 H-1…H-8 八轨 |
| 10 | 录制前置 | `应 749 passed` | **`应 802 passed`** | 整合轮 10 合入 T-1…T-6 六轨（T-1 +21 / T-2 +11 / T-3 +21） |
| 4 | 三件事表第 2 行 | 「机制在，屏幕上演不出来，用 A 版」 | 「✅ 已能演」，红线改方向 | Y-4 |
| 5 | 耗时表 | A 版 265s / B 版 287s（B 版为估算） | 镜 5 实数 190 字 / 47.5s、分配 55s，**总长 280s（4:40）**，镜 6/7/8 各顺延 15s | 重数重算 |

🔴 **本轮红线掉了个头。** 以前防的是「把没做到的说成做到」（不许在屏幕上找换渠道画面）；
现在换渠道**真能演**了，防的变成「把 1 次 replan 说成重试到上限」——
`MAOS_MAX_REPLAN` 默认 2，这一镜只用了 1 次就被第二个错误码一票否决。
说成「到上限」不只是数字错，还会把这一镜最值钱的判据（**知道什么时候不该重试**）讲丢。

---

## T 轮回填台账（2026-08-29 · 基线 `27c9e18`）

本轮**一行 `maos/**` 源码都没改**，只动分镜与新建一个前置脚本。下面每条都实跑复核过：

| # | 镜／节 | 原来写的 | 现在 | 依据 |
| :-- | :-- | :-- | :-- | :-- |
| 1 | **全文** | **一次都没提到 Skill**（`grep -i skill` 零命中） | **新增镜 7**：注册表 → 调用审计链 → 越权边界，50s | 规则必含项，见抬头「三个必含要素落在哪一镜」 |
| 2 | 录制前置 | 5 条人肉命令 + 一段脏工作区注意事项 | **一条 `bash scripts/demo_preflight.sh`**，逐条断言，退出码=出错步号 | 新建脚本，实测 19.4s / exit=0；负例两个也实测过 |
| 3 | 镜 3 | 「第一条命令屏幕上只打两行」 | **3 行**（stdout 2 + stderr 1，多出 `plan_defect —— 机器返工修不好…`） | 重定向分离实测 |
| 4 | 镜 3 贴片与念词 | `第六道闸 not_triggered` + 「付款那一步被 `LookupError` 拦下」 | **`第六道闸 blocker`** + 「第三出口转人工 → 主管驳回 → **付款一次都没派发**」（3 行新输出） | `run.log` 实测，旧念词描述的画面已不存在 |
| 5 | 镜 2 | 「政策规则对象在第 4–22 行附近」 | **删掉一切死行号**，改成「按 `task_id` 现场 `grep` 定位，认组不认行」；并补出「文件里有 `task-s7-*` / `task-s7b-*` 两组，别混」 | 连跑三次 `make_evidence.py`，`amount_paid: 6800.0` 落在 **41 → 152 → 152** 行 —— 两组对象的先后顺序**不稳定**，而录制前置必然重跑一次。**任何写死的行号都会周期性失效**（已记 `docs/BACKLOG.md ## task-T6`） |
| 6 | 镜 8（原 7） | warn **11 条**（`authoritative-fact` 下 1 条） | **12 条 3 类**（`authoritative-fact` 下 **2** 条） | 实跑复核；七行读数 `86/86`…`1/1` 与上一轮**逐字一致** |
| 7 | 镜 8 尾注 | 「Y-1 合并后那 4 条 warn 会自动消失」 | **没消失**，仍是 4 条；改为「别把它当待办追」 | 实跑复核 |
| 8 | 三件事 · 第 2 条 | `test_replan_gateway.py` 仍 **19 passed** | **23 passed** | 实跑 |
| 9 | 三件事 · 第 3 条 | `scenario_7.py:318` / `:351`、`gate.py:586` | **`:498` / `:535`**（另有 `:605` / `:635` 是 s7b 组）、**`gate.py:808`** | `grep -n` 实测，三个行号全部作废 |
| 10 | 三件事 · 第 1 条 | 「Element 房间仍未接通」一段话 | **A/B 双分支**，各自掐表；A 今天可录，B 待 T4 | `hiclaw/room_demo.py` 已存在，但 `import nio` 仍报错、`evidence/room/` 0 张截图 |
| 11 | 手册对照 | 「手册 `:724` 起，8 镜，差异两处」 | **`:744` 起，本分镜 9 镜，差异三处** | `sed -n` 实测 |
| 12 | 耗时表 | `147df03` 实测，8 镜，总长 280s | **`27c9e18` 实测，9 镜，总长 326s（5:26）** | 逐镜重跑掐表 |

---

## 待整合轮回填

| # | 事项 | 等什么 |
| :-- | :-- | :-- |
| 1 | **规则口径本身** | 抬头那张「三个必含要素」表、时长上限、评审维度与权重，全部来自**对规则图的转录**，**不是官方原文**。见 [`docs/open-questions.md`](open-questions.md) **OQ-1 / OQ-2**，**必须由人类拿官方通知核一次** |
| 2 | **`maos/flows/` 与 `maos/runtime/` 的行号** | T1 / T2 / T3 轨正在改 `maos/` 源码。合并后「三件事」第 3 条的 `:498` / `:535` / `:808` 需重核，办法：`grep -n "hq.decide" maos/flows/scenario_7.py` |
| 3 | **AgentTeams 的 B 分支** | T4 轨交出真房间证据（三条前置见 A/B 双分支那节）。到位后切 B，并从镜 3、镜 5 的富余里借 6s |
| 4 | 录制当天现跑对屏 | 每一镜的输出都要当天实跑一次核对；对不上以当天为准重贴，**不许照本文件念**。镜 7 的 `invocation_id` / `input_digest` 每跑必变，尤其不许照抄 |

> 回填时的红线不变：**不许演不存在的功能。** 每一条改完都要实跑一次，
> 屏幕上看得到才许写进念词。本轮第 3、4、5、8、9 条全是「上一版写的画面已经不在屏幕上了」——
> 分镜放几天就会长出这种失真，**每轮都得重跑一遍，不能靠读**。
