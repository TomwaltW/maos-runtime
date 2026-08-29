# 提交自查单

复赛三件套：**方案 PPT + 仓库 + Demo 视频**。全部打勾才提交。

> ⚠️ **两处官方口径不在仓库里** —— 内容不在本文件复述，只留指针：
> → 见 [`docs/open-questions.md`](open-questions.md) **OQ-1**（评审四维的名称与权重）、
> **OQ-2**（Demo 视频的官方规格）。
> 拿到官方通知后按那份文件的「回填点」表逐处补，**改一处不是改四处**。

> 📌 **本文件只让每一条「可勾」，不替任何人勾。** 全文 `[ ]` / `☐` 一律保持未勾状态：
> 判据写明确、命令能跑、期望值写清楚，勾是人类照着跑完之后自己的动作。

---

## A. 仓库自查

### A-1 机器验收（逐条跑，全绿才算）

**跑之前先确认工作区干净**（`git status --porcelain` 空）—— 第 ⑤ 条会把工作区跑脏，
干净的起点是判断「脏是这次跑出来的」的前提。**跑完必须收尾，见 [D-0](#d-0-证据链跑完必须收尾先看这条)。**

```bash
# ①
python3 -m pytest maos/tests -q          # □ 596 passed，个位数秒
# ②
python3 run.py                           # □ exit=0，个位数秒；跑完 git status 仍空（它不产证据）
# ③
python3 run.py --scenario 7              # □ exit=0，Plan 终态 FAILED（这正是它要演的，不是回归）
# ④
python3 scripts/gen_docs.py --check      # □ exit=0，打印「3 份文档与代码逐字节一致」
# ⑤
python3 scripts/make_evidence.py         # □ 「8 场景落盘，0 场景缺模块」（含 R5）；⚠️ 之后工作区 50 行脏
# ⑥
python3 scripts/verify.py                # □ 7/7 PASS，exit=0，另有 11 行 warn（3 类，见 A-2）
# ⑦
git diff --stat maos/contracts/          # □ 空输出（冻结契约未被动过）
```

> ⑤ 缺省一并产出 `scenario-R5`（整合轮 5 / Y-3 起）。`--no-r5` 可显式跳过，
> 届时 `verify.py` 第 5、7 项判 `[SKIP]` 而不是 0/0 PASS —— 空转不再印成满分。

- [ ] **新克隆冒烟**：`git clone` 到全新目录，严格按 README 从零跑到 `verify.py` 7/7，
      **掐表 ≤ 15 分钟**。过不了就改 README 直到过 —— 改 README，不是改口径。
- [ ] 冒烟用的是**没有任何 API key** 的环境（评审多半没有 key）。

> ✅ **整合轮 5 已实跑**（Z-5 + 编排侧复跑）：全新克隆 + 无任何 API key，
> `clone → pytest → run.py → run.py --scenario 7 → ⑤ → ⑥` 全程 **18.3 秒**，
> 其中「clone + 证据链两条」最短路径 **5.4 秒** —— 距 15 分钟预算差两个数量级。
> 逐步耗时与卡点见 [`docs/clone-smoke-report.md`](clone-smoke-report.md)。
> 原来那个「只跑 ⑤ 不跑 ⑥ 会 `exit=2`」的坑已由 Y-3 消解（⑤ 缺省一并产 R5）。

### A-2 证据束

- [ ] `evidence/` 下每个文件首行都是 `# generated at <ISO8601> from <git sha>`（实测 50 个文件）。
- [ ] `scenario-1..7` 的首行 sha **不带 `-dirty` 后缀**。
- [ ] `INDEX.json` 里的 `git_sha` 与提交的 commit 一致（该键确实存在，实测可读）。
- [ ] 全部 `*.db` **没有**入库（`.gitignore` 挡着，`git status` 里不该看见它们）。
- [ ] 证据束里 grep 不到任何真密钥（生成脚本已做出口脱敏 + 哨兵反查，
      但**提交前再人肉扫一遍** `MAOS_LLM_API_KEY` / `MATRIX_TOKEN` 的值）。

#### verify.py 的 11 行 warn 是**已知缺口，不是回归**

`verify.py` 报 7/7 PASS 的同时会打印 11 行 `· warn:`，分 3 类（整合轮 5 **合入 Y-4 后**实测）。
**第一次看到的人会以为是回归 —— 不是。** 逐类对照：

| 类 | 行数 | 内容 | 出处 |
| :-- | :-- | :-- | :-- |
| A | 6 | 产物没有来源事件（`provenance=unknown`）—— scenario-1/2/3/5/6/7 各 1–3 份 | `docs/BACKLOG.md ## task-X4` |
| D | 4 | 外部判据来源未审计 —— scenario-1/2/3/5，说的是**入库路径**（绕开 `on_task_result`），不是内容真伪 | `## task-X4` |
| E | 1 | `authoritative-fact` 项下：`scenario-7 case=case-s7-0001: 有回执但 biz_status 不是 settled` | **Y-4 带来的预期内新增**，见下 |

E 类是 Y-4 让场景 7 先撞一次 `40005` 换渠道造成的：**主渠道那笔真收到过回执**，
而全案最终落人工审批、业务状态收在 `compensated`。回执是真的、`biz_status` 不是 `settled`
也是真的，两者并存正是这一镜要演的东西 —— **判定不受影响，`authoritative-fact` 仍 3/3 PASS**。

原来的 B 类（`test_report` 缺 `sandbox_mode`，4 行）与 C 类（事件不在任何一棵树内，3 行）
**已在整合轮 5 归零** —— 分别由 Y-1、Y-2 补掉。
17 行 / 4 类（整合轮 5 之前）与 10 行 / 2 类（合 Y-4 之前）都是旧读数，别照抄旧材料。

- [ ] 跑出来的 warn **就是这 11 行 / 3 类**，B 类与 C 类**不该再出现**（出现了就是回归）。

#### 已知缺口：`scenario-R5` 的首行 sha 仍带 `-dirty`（收敛成一条命令后依然存在）

**整合轮 5 全新克隆实测**：`scenario-R5` 的 **7 个文件**首行 sha 是 `<sha>-dirty`，
其余 43 个不带。合并 Y-3 之前的成因是「⑤⑥ 两条命令连跑，第二条读到的工作区已脏」；
**收敛成一条命令之后成因变了，缺口没变** —— `make_evidence.py` 在同一次进程里
**先写 `scenario-1..7`、再跑 R5**，写完前 7 个场景工作区就已经脏了，R5 于是读到 `-dirty`。

也就是说：这是「边生成边落盘、落盘又反过来污染 sha 读数」的架构性问题，
**不是双命令造成的**，Y-3 的收敛没有（也不可能顺带）解决它。
若要全量干净，得让 `make_evidence.py` 在开跑时**一次性取定 sha**、全程复用，
或先全部产到仓库外再整批搬入。已记 `docs/BACKLOG.md`。

- [ ] 认下当前口径：**`scenario-1..7` 干净、`scenario-R5` 的 7 个带 `-dirty`**，其余组合都算异常。

### A-3 文档

- [ ] README 第一屏就能看到 `python3 scripts/verify.py`。
- [ ] README 里所有命令都是 `python3`（评委在 macOS 上敲 `python` 会 command not found）。
- [ ] 七份文档齐：`architecture` / `domain-portability` / `authoritative-facts` /
      `agentteams-mapping` / `agent-identity` / `skill-catalog` / `toolport-contract`。
- [ ] 三份**代码生成**的文档是重新生成过的，不是手改的（`--check` 绿即可）。
- [ ] `docs/BACKLOG.md` / `docs/DECISIONS.md` / `docs/open-questions.md` **保留在仓库里**，
      不要为了好看删掉 ——「知道自己哪里没做完」本身是可信度的一部分。
- [ ] README 的复现段与 A-1 的命令序列**逐字一致**（两处都写死了命令，容易漂）。
      整合轮 5 已对齐：两处都是 `make_evidence.py` + `verify.py` 两条。

### A-4 口径一致性（最容易被问穿的地方）

逐条确认材料里**没有**把下面任何一条说过头。
**七行已按 `42822fc` 逐条回代码复核**，复核结论写在末列。

| 事实 | 只能这么说 | 不许这么说 | 复核 |
| :-- | :-- | :-- | :-- |
| 政策数据与历史案例 | 「按行业惯例构造的合成数据」 | 「真实企业政策」 | ✅ 仍成立 |
| 支付网关 | 「错误码与异步时序对齐支付宝开放平台公开规范；演示用模拟实现」 | 「接入了支付宝」 | ✅ `maos/tools/gateway.py` 明写字段对齐 `alipay.trade.refund`，未接通时 `raise NotImplementedError`，**不静默返回假数据** |
| Matrix 房间 | 「镜像层已实现，降级路径实测等价，真房间待接通」 | 「全过程在 Element 里跑通」 | ✅ `hiclaw/matrix_bus.py` 在，真房间未接通 |
| StorePort / PolarDB | 「有地基、未接线；PG 后端是空壳且拒绝回落」 | 「后端已可插拔切 PolarDB」 | ✅ `maos/store/pg_store.py` 五个方法全 `raise NotImplementedError`，确实拒绝回落 |
| AutoGen | 「可插拔内核之一，未在复赛演示中启用」 | 「基于 AutoGen 构建」 | ✅ 全仓 `*.py` grep 不到 `autogen` 的实现，只在 `maos/runtime/worker.py` 注释里提及 |
| replan 换渠道 | 「场景 7 演到了：撞 `40005` 触发一次 replan 换备用渠道，再撞 `ACQ.SYSTEM_ERROR` 一票否决落人工，全程没有自旋」 | 「重试到上限才转人工」（**只重试了一次**，不是打满上限）｜「换了渠道就成功了」 | ✅ 整合轮 5 合入 Y-4 后实测：`run.py --scenario 7` 屏幕上打出 `换渠道重试: 1 次 replan（40005 触发，ACQ.SYSTEM_ERROR 一票否决，没有自旋）`，状态轨迹里有 `AWAITING_REVIEW -> REWORK [gate_rework]` → `REWORK -> PENDING [requeue]`；`test_replan_gateway.py` 仍 **19 passed**，一条没少 |
| 场景覆盖 | 「`run.py` 无参跑全部七个场景，含失败路径」 | 「七个场景都跑成功了」（场景 7 的 Plan 终态是 FAILED，那正是它要演的） | ✅ `maos/main.py:29` 实测 `DEFAULT_SCENARIOS = (1, 2, 3, 4, 5, 6, 7)` |

> ✅ 整合轮 5 已合入 Y-4，上表 replan 那一行已改口。新的说漏风险反过来了：
> 屏幕上是 **1 次** replan（撞第二个码就一票否决），**不是「重试到上限」**——
> `MAOS_MAX_REPLAN` 默认 2，这一镜根本没打满。把它念成「重试到上限」是说过头。

---

## B. 方案 PPT ↔ 评委要求逐条对照

每一行都要能在 PPT 里指出**哪一页**，并且那一页给得出**可核验证据**。
（对照表原文见 `docs/EXECUTION.md` 附 C；README §8 是同一张表的仓库版。）

**「PPT 页」列填的是页锚，不是页码。** 页锚与 [`docs/ppt-outline.md`](ppt-outline.md)（Z-1 轨）
共用同一套编号，由编排侧钉死：

```
P1  封面·一句话主张     P2  评委三段反馈      P3  从一条退款说起    P4  架构一眼
P5  状态机与七道闸      P6  AgentTeams 事件链 P7  Skill/ToolPort    P8  RAG 面向规划
P9  权威事实边界        P10 失败路径纵切      P11 一条命令核验      P12 同一内核两个域
P13 数据口径与边界      P14 复现指引
```

页码要等人类真做完版才有，**填了就会过期**；页锚不会。
Z-1 允许拆子页（`P8a`/`P8b`），但**不许改 P1–P14 的编号**，所以本表不会被拆页拆坏。

**「四维」列**全部留 `OQ-1` —— 官方维度名没到之前不许填，见
[`docs/open-questions.md`](open-questions.md) OQ-1。拿到后按那份文件的回填点表逐行补。

| # | 评委要求 | PPT 页 | 四维 | 证据 | ✓ |
| :-- | :-- | :-- | :-- | :-- | :-- |
| 1 | 一条脱敏真实退款需求的可执行纵向切片 | P3 → P10 | `OQ-1` | `evidence/scenario-6,7/` | ☐ |
| 2 | AgentTeams 事件链 | P6 | `OQ-1` | `docs/agentteams-mapping.md` + `trace.json` | ☐ |
| 3 | 关键 Skill 的真实调用 | P7 | `OQ-1` | `event_log` 的 `SkillInvoked` | ☐ |
| 4 | 返工 / HITL Trace | P10 | `OQ-1` | `evidence/scenario-2,3,5,7/trace.json` | ☐ |
| 5 | Evidence Bundle | P11 | `OQ-1` | `verify.py` 7/7 | ☐ |
| 6 | 业务对象关联到同一案例 | P3 / P11 | `OQ-1` | verify 第 2 项（`business-ref 33/33`） | ☐ |
| 7 | 外部系统保留权威事实，区分已提出/处理中/已到账 | P9 | `OQ-1` | verify 第 3 项 + 越权拒绝单测 | ☐ |
| 8 | RAG 面向 workflow 规划 | P8 | `OQ-1` | `kb-hits.json` + `dag-diff.json` | ☐ |
| 9 | 先结构化过滤再组合召回（评委给的字段顺序） | P8 | `OQ-1` | `maos/kb/retriever.py` + 跨租户不召回单测 | ☐ |
| 10 | 减少遗漏财务复核 / 错误套用政策 / 无限重试 | P5 | `OQ-1` | 第六道闸 + 政策版本锁定 + `MAOS_MAX_REPLAN`；**「无限重试」这条现在有实跑证据**：`run.py --scenario 7` 打出 `换渠道重试: 1 次 replan（40005 触发，ACQ.SYSTEM_ERROR 一票否决，没有自旋）`，`test_replan_gateway.py` 19 条守着 | ☐ |
| 11 | 历史流程不能替代当前订单事实和人工授权 | P9 | `OQ-1` | `maos/kb/guardrails.py` 三条断言 + 护栏单测 | ☐ |
| 12 | 以到账 / 客户确认 / 人工纠错验证 DAG | P10 | `OQ-1` | `result.json` 的 `business_outcome` | ☐ |
| 13 | 只有证据完整且外部结果明确的案例进默认知识层 | P8 → P11 | `OQ-1` | verify 第 7 项（`history-case 1/1`） | ☐ |

> ✅ 第 8 条的证据已在整合轮 5 变厚：Y-2 让场景 6 播上 W-1 语料（候选集 0 → 3），
> `kb-hit` 从 4/4 涨到 **7/7**。
> ⚠️ 第 10 条仍受 **Y-4 未合并**影响，见 A-4 的 replan 行。

另外三条是**评委三段反馈的诊断**（PPT 的 **P2** 页正面摆出来），
每条的回应落点写成**页锚 + 一条可当场跑的命令**，与 Z-1 对齐：

| 诊断（P2 提出） | 回应页 | 台上跑这条 | ✓ |
| :-- | :-- | :-- | :-- |
| 没有可执行制品和运行证据 | P11 | `python3 scripts/verify.py` → 7/7 PASS | ☐ |
| 现实业务锚点不足 | P3 | `python3 run.py --scenario 6` → 退款域纵切，不是软件域自证式 demo | ☐ |
| 「所有 Agent 都回复完成」≠ 业务成功 | P10 | `python3 run.py --scenario 7` → Plan 终态 FAILED、`biz_status=compensated`、`settled` 观察 0 条 | ☐ |

### PPT 逐页自查

- [ ] 每一页「我们做到了 X」的断言，都能在仓库里指出**文件 + 行号**或**一条命令**。
- [ ] 架构图与 `docs/architecture.md` 的图**一致**（别是两个版本）。
- [ ] 数据口径页（P13）：合成数据 / 真实规范 / 模拟实现三者分得清清楚楚（见 A-4）。
- [ ] 没有一页写着仓库里不存在的能力。
- [ ] 页锚 P1–P14 与 `docs/ppt-outline.md` 对得上（Z-1 若拆了子页，本表的页锚仍有效）。

---

## C. Demo 视频

- [ ] 按 [`docs/demo-script.md`](demo-script.md) 录，**失败路径为主线**。
- [ ] 录制前跑完该文件的「录制前置」五条命令，全绿。
- [ ] 录制前确认该文件末尾的**三件事**（Element 是否接通 / replan 是否合并 /
      审批由谁驱动），按现状调整念词 —— **不许演不存在的功能**。
- [ ] 时长 3–5 分钟（手册口径）。官方上限 → 见
      [`docs/open-questions.md`](open-questions.md) **OQ-2**。
- [ ] 终端字号够大，后排能看清；窗口 ≥ 100 列。
- [ ] 画面里没有露出任何 key、token、homeserver 地址
      （房间设置页、浏览器开发者工具都别开着录）。
- [ ] 最后一屏是「同一个内核，两个域」+ `git diff --stat` 的空输出。
- [ ] 分辨率 / 格式 / 大小上限 / 是否要字幕 → 见
      [`docs/open-questions.md`](open-questions.md) **OQ-2**，照官方通知补齐后再定稿。

> ✅ Y-4 已于整合轮 5 合入，`demo-script.md` 的 02:06 那一镜已切到 **B 版**
> （屏幕输出按实跑照贴）、「录制前三件事」第 2 件已改成「已能演」。**可以开录了。**

---

## D. 提交前最后一遍

### D-0 证据链跑完必须收尾（先看这条）

🔴 **A-1 与 D-1 会打架**：D-1 要求工作区干净，但 A-1 的 ⑤⑥ 跑完 `git status` **必然有 50 行**
（`evidence/scenario-1..7` 与 `scenario-R5` 全被重写成 `M`）。
这不是回归，是证据链本来就会重写产物。**顺序是：跑证据链 → 收尾 → 再核 D-1。**

收尾二选一：

```bash
# 甲：这次重跑就是要提交的证据 —— 收进 commit
git add evidence/ && git commit -m "chore: 按当前 HEAD 重跑证据束"

# 乙：只是验证一遍，不打算改证据 —— 还原
git checkout -- evidence/ && git clean -fdq evidence/
```

- [ ] 收尾已做，`git status --porcelain | grep evidence/` **无输出**。

> 选甲的话注意：commit 之后 HEAD 变了，而 `INDEX.json` 里的 `git_sha` 记的是**跑的时候**
> 那个 HEAD，于是 D-4 第一条会红。选甲就要**再跑一次证据链再 commit 一次**才收敛，
> 或者接受 `git_sha` 落后一个 commit 并在 PPT/视频里不引用它。选乙没有这个问题。

### D-1 ~ D-3

- [ ] commit 全部落盘，工作区干净（`git status` 空）—— **先做完 D-0**。
- [ ] **推送由人类手动做**（仓库纪律：Claude 只许本地 commit，禁止 push）。
- [ ] 仓库链接可访问，且落地页就是根 README。

### D-4 三件套版本对齐（可跑，不是靠眼力）

原来这条写的是「PPT 里引用的行号、视频里跑出的数字、仓库当前 HEAD 是同一个状态」——
**没法勾**。拆成三条可跑的：

```bash
# ① INDEX.json 的 git_sha == 当前 HEAD？（diff 无输出 = 一致）
python3 -c "import json;f=open('evidence/INDEX.json');f.readline();print(json.load(f)['git_sha'])" > /tmp/idx.sha
git rev-parse HEAD > /tmp/head.sha
diff /tmp/idx.sha /tmp/head.sha && echo ALIGNED

# ② evidence 首行 sha 不带 -dirty？（当前应只列出 scenario-R5 的 7 个，见 A-2 已知缺口）
grep -rl "^# generated at.*-dirty" evidence/

# ③ evidence 里出现过几个不同的 sha？（应当只有一个，忽略 -dirty 后缀）
grep -rh "^# generated at" evidence/ | sed 's/.* from //' | sed 's/-dirty//' | sort -u
```

- [ ] ① 打印 `ALIGNED`。
- [ ] ② 只列出 `evidence/scenario-R5/` 下的 7 个文件（其余任何一个出现都要查）。
- [ ] ③ 只输出**一个** sha，且等于 `git rev-parse HEAD`。

> 🔴 **① 是结构性红的，别指望它变绿 —— 要的是「红得可解释」。**
> 证据束在整合轮 5 已按合并后的代码重跑过（`9964f17`，`git_sha=caf45d2`），
> 但**任何写进 `evidence/` 的重跑，其记录的 sha 必然是重跑那一刻的 HEAD，
> 而把它 commit 又会产生新的 HEAD** —— 这是个不动点问题，跑多少次都差至少 1 个 commit。
>
> **所以判据改成这条，它是可勾的**：
>
> ```bash
> # ①' 证据束记录的 sha 到 HEAD 之间，有没有动过代码？（输出 0 = 证据未过期）
> IDX=$(python3 -c "import json;f=open('evidence/INDEX.json');f.readline();print(json.load(f)['git_sha'])")
> git diff --name-only $IDX HEAD -- maos/ scripts/ run.py scenarios/ fixtures/ | wc -l
> ```
>
> - [ ] ①' 输出 **0**，且 `git log --oneline $IDX..HEAD` 里每一条都只动 `docs/` / `README.md` / `evidence/`。
>
> **整合轮 5 实测**：`IDX=caf45d2`，落后 **4 个 commit** —— `9964f17`（证据束重跑本身，
> 只动 `evidence/`）+ 三条 `docs(p7)`（README 刷数 / BACKLOG 记账 / 第二批回填与看板）。
> 代码文件差异 **0** —— 证据内容没过期。
> 只有当 `maos/**` 或 `scripts/**` 再被动过时，才必须重跑证据链并 commit。
> ②③ 当前是绿的（②恰好 7 个且全在 R5，③去掉 `-dirty` 后唯一）。
- [ ] PPT 与视频里出现的**每一个数字**（`596 passed` / `7/7 PASS` / `33/33` / `19 条测试` …），
      都能在 [`docs/ppt-outline.md`](ppt-outline.md) 或 [`docs/demo-script.md`](demo-script.md)
      里找到**产出它的那条命令**。找不到命令的数字就是没有出处的数字，删掉或补命令。
- [ ] **改了代码就要回来重录那一镜、重生成那三份文档**（`gen_docs.py --check` 会告诉你哪份漂了）。

---

## 整合轮 5 收口台账（2026-08-29）

Y-1 / Y-2 / Y-3 与 Z 轮五轨已并入 `integrate/round-5`，下面七条**已回填完毕**：

| # | 位置 | 原来写的 | 现在 | 依据 |
| :-- | :-- | :-- | :-- | :-- |
| 1 | A-2 warn 对照表 | 17 行 / 4 类 | **10 行 / 2 类**（B、C 类归零） | Y-1 + Y-2 |
| 2 | A-1 ⑤⑥ 两条命令 + 两个 ⚠️ | 分两条敲，顺序不能反 | 收敛成一条 ⑤；两个 ⚠️ 已删 | Y-3 |
| 3 | A-3 最后一条的 ⚠️ | README 与 A-1 待对齐 | 已对齐，⚠️ 已删 | Y-3 |
| 4 | §B 第 8 条表下 ⚠️ | `kb-hit` 4/4，证据薄 | **7/7**，场景 6 候选集 0 → 3 | Y-2 |
| 5 | A-1 新克隆冒烟 ⚠️ | Z-5 待实跑 | 已实跑：全程 **18.3 秒**、最短路径 **5.4 秒** | Z-5 + 编排侧复跑 |
| 6 | A-1 ① 的条数 | 521 passed | **571 passed** | Y 轮三轨新增测试 |
| 7 | D-4 ① 证据 sha 落后 | 落后 3 个 commit，① 是红的 | 本轮末尾按整合后 HEAD 重跑证据链并 commit | 整合轮 5 |

🔴 **一条清单里写「会自动消失」、实测却没消失的**（**不是回归，是原判断错了**）：

| 位置 | 原判断 | 实测 |
| :-- | :-- | :-- |
| A-2「R5 必然带 `-dirty`」整节 | Y-3 收敛成一条命令后三条判据同时成立，**整节可删** | ❌ **缺口仍在**。全新克隆实测 R5 的 7 个文件仍带 `-dirty` —— `make_evidence.py` 在同一次进程里先写 `scenario-1..7`，写完工作区就脏了，R5 于是读到脏 sha。成因从「双命令」变成「边生成边落盘」，**该节已改写而非删除** |

---

## 整合轮 5 补合 Y-4 后的第二批回填（2026-08-29）

Y-4 已并入（合并提交 `783d9dd`），下面四条**已回填并实跑复核**：

| # | 位置 | 原来写的 | 现在 | 依据 |
| :-- | :-- | :-- | :-- | :-- |
| 1 | A-2 warn 对照表 | 10 行 / 2 类 | **11 行 / 3 类**，新增 E 类（`authoritative-fact` 下 scenario-7「有回执但 `biz_status` 不是 settled」） | Y-4 |
| 2 | A-4 replan 行 + ⚠️ | 「演示里没有场景走这条路」 | 改口为「演到了：1 次 replan 换渠道 → 一票否决落人工」；**新的说漏风险反过来了**，见该行下的提示 | Y-4 |
| 3 | §B 第 10 条 | 只有闸 + 政策锁 + `MAX_REPLAN` | 补上场景 7 的实跑输出 | Y-4 |
| 4 | §C 的 ⚠️「Y-4 合并前不要开录」 | 拦着不许开录 | **已解除**，02:06 那一镜已切 B 版 | Y-4 |
| 5 | A-1 ① 的条数 | 571 passed | **596 passed** | Y-4 带 25 条新测试 |

🔴 **这一批里最该记住的一条**：A-2 上一版立的判据是「就是这 **10 行 2 类**，多出来的才要查」。
Y-4 合入后 warn 变成 11 行 3 类，**照旧判据执行会把一条预期内的 warn 判成回归**。
教训：**把「当前实测值」写成判据时，必须同时写清它随哪一轨变** —— 否则判据本身会变成假警报源。

---

## 待整合轮 6 回填

Y 轮与 Z 轮九轨全部合入，**代码面与材料面本轮已对齐**，只剩人类那一项：

| # | 位置 | 等什么 |
| :-- | :-- | :-- |
| 1 | 全文 `OQ-1` / `OQ-2` 指针 | 官方口径到手后，按 [`docs/open-questions.md`](open-questions.md) 的回填点表逐处补，并把该文件对应条目标为「已答」 |
| 2 | `integrate/round-5` 并回 `goai-restructure` | 纯 FF（28 个提交），**涉及共享分支，等人类点头** |
