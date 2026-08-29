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

**跑之前先确认工作区干净**（`git status --porcelain` 空）—— 第 ⑤⑥ 条会把工作区跑脏，
干净的起点是判断「脏是这次跑出来的」的前提。**跑完必须收尾，见 [D-0](#d-0-证据链跑完必须收尾先看这条)。**

```bash
# ①
python3 -m pytest maos/tests -q          # □ 521 passed，个位数秒
# ②
python3 run.py                           # □ exit=0，个位数秒；跑完 git status 仍空（它不产证据）
# ③
python3 run.py --scenario 7              # □ exit=0，Plan 终态 FAILED（这正是它要演的，不是回归）
# ④
python3 scripts/gen_docs.py --check      # □ exit=0，打印「3 份文档与代码逐字节一致」
# ⑤
python3 scripts/make_evidence.py         # □ 「7 场景落盘，0 场景缺模块」；⚠️ 之后工作区变脏（43 行）
# ⑥
python3 -m maos.kb.experiment            # □ scenario-R5 落盘；⚠️ 之后工作区共 50 行脏
# ⑦
python3 scripts/verify.py                # □ 7/7 PASS，exit=0，另有 17 行 warn（4 类，见 A-2）
# ⑧
git diff --stat maos/contracts/          # □ 空输出（冻结契约未被动过）
```

> ⚠️ **⑤⑥ 的顺序不能反。** 实测：正序只有 `scenario-R5` 的 7 个文件首行 sha 带 `-dirty`；
> 反序（⑥先⑤后）变成 43 个文件带 `-dirty`，**更糟**。原因见 A-2 的「已知缺口」。

> ⚠️ **⑥ 没有 argparse。** `python3 -m maos.kb.experiment --help` 不打印用法，
> **直接开跑并落盘**（实测 exit=0）。别指望用参数控制它。

> ⚠️ **Y 轮易变**：Y-3 正在做「一条命令复现全量证据」。合并后 ⑤⑥ 会收敛成一条，
> 上面两个 ⚠️ 连同 A-2 的已知缺口一起消失。

- [ ] **新克隆冒烟**：`git clone` 到全新目录，严格按 README 从零跑到 `verify.py` 7/7，
      **掐表 ≤ 15 分钟**。过不了就改 README 直到过 —— 改 README，不是改口径。
- [ ] 冒烟用的是**没有任何 API key** 的环境（评审多半没有 key）。

> ⚠️ **Y 轮易变**：Z-5 轨正在实跑新克隆冒烟，可能发现上面命令序列还有别的坑
> （已知的一个：新克隆里只跑 ⑤ 不跑 ⑥ 会 `exit=2`，且错误提示指向一条解决不了它的命令 ——
> 见 `docs/BACKLOG.md ## task-W5` 第 2 条）。它的发现走它自己的报告，整合轮 5 合并。

### A-2 证据束

- [ ] `evidence/` 下每个文件首行都是 `# generated at <ISO8601> from <git sha>`（实测 50 个文件）。
- [ ] `scenario-1..7` 的首行 sha **不带 `-dirty` 后缀**。
- [ ] `INDEX.json` 里的 `git_sha` 与提交的 commit 一致（该键确实存在，实测可读）。
- [ ] 全部 `*.db` **没有**入库（`.gitignore` 挡着，`git status` 里不该看见它们）。
- [ ] 证据束里 grep 不到任何真密钥（生成脚本已做出口脱敏 + 哨兵反查，
      但**提交前再人肉扫一遍** `MAOS_LLM_API_KEY` / `MATRIX_TOKEN` 的值）。

#### verify.py 的 17 行 warn 是**已知缺口，不是回归**

`verify.py` 报 7/7 PASS 的同时会打印 17 行 `· warn:`，分 4 类。
**第一次看到的人会以为是回归 —— 不是。** 逐类对照：

| 类 | 行数 | 内容 | 出处 |
| :-- | :-- | :-- | :-- |
| A | 6 | 产物没有来源事件（`provenance=unknown`）—— scenario-1/2/3/5/6/7 各 1–3 份 | `docs/BACKLOG.md ## task-X4` |
| B | 4 | `test_report` **执行路径不可审计** —— 报告里没有 `sandbox_mode`，判不出这次是真在容器里跑的还是降级跑的（scenario-1/2/3/5） | `## task-X4` 第 1 条 |
| C | 3 | 事件不在任何一棵树内 —— scenario-5 有 1 条、scenario-6 与 R5 各 2 条，全是**建 Plan 之前**发生的调用（`plan_id` 为空串，不是事件丢了） | `## task-X4` 第 2 条 |
| D | 4 | 外部判据来源未审计 —— scenario-1/2/3/5，说的是**入库路径**（绕开 `on_task_result`），不是内容真伪 | `## task-X4` |

- [ ] 跑出来的 warn **就是这 17 行 / 4 类**，没有第 5 类冒出来（多出来的才要查）。

> ⚠️ **Y 轮易变**：Y-1 轨正在补这四类。合并后 warn 会**自动消失或减少** ——
> 届时把上表连同这一条判据一起删掉，别留着一张对不上的表。

#### 已知缺口：`scenario-R5` 的首行 sha 必然带 `-dirty`

实测（基线 `42822fc`，正序跑 A-1 ⑤⑥）：`scenario-R5` 的 **7 个文件**首行 sha 全部是
`42822fc…-dirty`，其余 43 个不带。根因是**架构性的**，不是操作失误：

⑤ 在干净工作区跑 → 产 `scenario-1..7`，sha 干净 → **工作区因此变脏** →
⑥ 接着跑 → 它读到的工作区已经脏了 → R5 的 sha 带 `-dirty`。

两条生成命令连跑，**必然有一批带 `-dirty`**。调换顺序只是把 7 个变成 43 个（实测）。
若要两批都干净，只能「跑一条 → commit → 跑另一条 → commit」，但那样两批的 sha **不同**，
`INDEX.json` 的 `git_sha` 又对不上 HEAD ——
**A-2 这三条判据在当前双命令架构下无法同时满足**，只能三选二。

- [ ] 认下当前口径：**`scenario-1..7` 干净、`scenario-R5` 的 7 个带 `-dirty`**，其余组合都算异常。

> ⚠️ **Y 轮易变**：这条缺口的根治就是 Y-3 的「一条命令复现全量证据」——
> 一次跑完则全量同一个 sha 且都不带 `-dirty`，三条判据同时成立。合并后删掉整个小节，
> A-2 第 2 条改回「**每个**文件都不带 `-dirty`」。已记 `docs/BACKLOG.md ## task-Z4`。

### A-3 文档

- [ ] README 第一屏就能看到 `python3 scripts/verify.py`。
- [ ] README 里所有命令都是 `python3`（评委在 macOS 上敲 `python` 会 command not found）。
- [ ] 七份文档齐：`architecture` / `domain-portability` / `authoritative-facts` /
      `agentteams-mapping` / `agent-identity` / `skill-catalog` / `toolport-contract`。
- [ ] 三份**代码生成**的文档是重新生成过的，不是手改的（`--check` 绿即可）。
- [ ] `docs/BACKLOG.md` / `docs/DECISIONS.md` / `docs/open-questions.md` **保留在仓库里**，
      不要为了好看删掉 ——「知道自己哪里没做完」本身是可信度的一部分。
- [ ] README 的复现段与 A-1 的命令序列**逐字一致**（两处都写死了命令，容易漂）。

> ⚠️ **Y 轮易变**：Y-3 合并后 README 复现段会从两条命令变一条，此处与 A-1 要一起改。

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
| replan 换渠道 | 「机制已落地并有 19 条测试守着，但演示里没有场景走这条路」（见 `docs/BACKLOG.md ## task-X2` 第 2 条） | 「演示里能看到它自动换渠道重试到上限」 | ⚠️ 数字对（`test_replan_gateway.py` 实测 19 个 `test_`），`maos/flows/scenario_7.py:43` 亦明写该段「没有落在本文件里」。**但 Y-4 正在改这件事**，见下 |
| 场景覆盖 | 「`run.py` 无参跑全部七个场景，含失败路径」 | 「七个场景都跑成功了」（场景 7 的 Plan 终态是 FAILED，那正是它要演的） | ✅ `maos/main.py:29` 实测 `DEFAULT_SCENARIOS = (1, 2, 3, 4, 5, 6, 7)` |

> ⚠️ **Y 轮易变**：Y-4 轨正在让**场景 7 能演换渠道重试**。合并后 replan 那一行的
> 「演示里没有场景走这条路」就变成假话，右列的「不许这么说」反而成了可以说的 ——
> 必须回来改，否则是**反向说漏**（把已经做到的说成没做到）。同时影响 §B 第 10 条。

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
| 10 | 减少遗漏财务复核 / 错误套用政策 / 无限重试 | P5 | `OQ-1` | 第六道闸 + 政策版本锁定 + `MAOS_MAX_REPLAN` | ☐ |
| 11 | 历史流程不能替代当前订单事实和人工授权 | P9 | `OQ-1` | `maos/kb/guardrails.py` 三条断言 + 护栏单测 | ☐ |
| 12 | 以到账 / 客户确认 / 人工纠错验证 DAG | P10 | `OQ-1` | `result.json` 的 `business_outcome` | ☐ |
| 13 | 只有证据完整且外部结果明确的案例进默认知识层 | P8 → P11 | `OQ-1` | verify 第 7 项（`history-case 1/1`） | ☐ |

> ⚠️ **Y 轮易变**：第 8 条的证据在 Y-2 合并后会变厚（场景 6 的 RAG 检索届时有话可说，
> 现在 `kb-hit` 只有 4/4）；第 10 条受 Y-4 影响，见 A-4 的 replan 行。

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
- [ ] 录制前跑完该文件的「录制前置」六条命令，全绿。
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

> ⚠️ **Y 轮易变**：Y-4 合并后场景 7 能演换渠道重试，`demo-script.md` 的 02:30 那一镜
> 与「录制前三件事」的第 2 件都要重写。**在 Y-4 合并前不要开录**，否则那一镜必然重录。

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

> 🔴 **当前实测：① 是红的，这是待办不是误报。** 在 `42822fc` 上跑，仓库里**已 commit** 的
> `evidence/` 是 `df96fa8` 生成的 —— 落后 3 个 commit。
> 好消息是那 3 个都只动文档（`docs(int-4)` / `feat(p5)` 的证据刷新 / `chore`），
> 证据内容没过期，**只是 sha 对不上**；坏消息是评委照 ① 一跑就看见不一致。
> **提交前必须重跑一次证据链并 commit**（D-0 选甲），让 ① 变绿。
> ②③ 当前是绿的（②恰好 7 个且全在 R5，③去掉 `-dirty` 后唯一）。
- [ ] PPT 与视频里出现的**每一个数字**（`521 passed` / `7/7 PASS` / `33/33` / `19 条测试` …），
      都能在 [`docs/ppt-outline.md`](ppt-outline.md) 或 [`docs/demo-script.md`](demo-script.md)
      里找到**产出它的那条命令**。找不到命令的数字就是没有出处的数字，删掉或补命令。
- [ ] **改了代码就要回来重录那一镜、重生成那三份文档**（`gen_docs.py --check` 会告诉你哪份漂了）。

---

## 待整合轮 5 回填

Y 轮四轨合并后，本文件下列各处会变成假话，**逐条回来改**。

| # | 位置 | 现在写的是什么 | Y 合并后应改成什么 | 依据 |
| :-- | :-- | :-- | :-- | :-- |
| 1 | A-2「17 行 warn / 4 类」对照表 + 那条判据 | 列了 A/B/C/D 四类共 17 行，说是已知缺口 | warn 消失或减少 → **整张表删掉**，判据改成「无 warn」或按实测新数重列 | Y-1 |
| 2 | A-1 ⑤⑥ 两条命令 + 两个 ⚠️ | 两条生成命令，顺序不能反，`--help` 直接开跑 | 收敛成**一条**；两个 ⚠️ 删掉 | Y-3 |
| 3 | A-2「已知缺口：R5 必然带 `-dirty`」整节 | 三条判据无法同时满足，只能三选二 | **整节删掉**；A-2 第 2 条改回「**每个**文件都不带 `-dirty`」；D-4 ② 的期望值改成「无输出」 | Y-3 |
| 4 | A-3 最后一条的 ⚠️ | README 复现段与 A-1 都要跟着改 | 改完即删该 ⚠️ | Y-3 |
| 5 | A-4 replan 行 + 其后的 ⚠️ | 「机制已落地…但演示里没有场景走这条路」 | 改成「场景 7 演换渠道重试到上限」；右列「不许这么说」那句**转正**；⚠️ 删掉 | Y-4 |
| 6 | §B 第 10 条 | 证据列的是闸 + 政策锁 + `MAX_REPLAN` | 补上场景 7 的实跑换渠道证据 | Y-4 |
| 7 | §B 第 8 条 + 表下 ⚠️ | `kb-hit` 4/4，场景 6 的 RAG 证据薄 | 按 Y-2 合并后的实测数刷新 | Y-2 |
| 8 | §C 的 ⚠️（Y-4 合并前不要开录） | 02:30 那一镜与「录制前三件事」第 2 件会变 | Y-4 合并后重写该镜，⚠️ 删掉 | Y-4 |
| 9 | A-1 新克隆冒烟下方的 ⚠️ | Z-5 可能发现命令序列还有别的坑 | 按 Z-5 的报告补进 A-1 | Z-5（同轮） |
| 10 | 全文 `OQ-1` / `OQ-2` 指针 | 官方口径未到 | 官方通知到手后，按 `docs/open-questions.md` 的回填点表逐处补，并把该文件对应条目标为「已答」 | 人类 |
