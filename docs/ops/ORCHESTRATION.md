# ORCHESTRATION —— MAOS 并行构建编排看板

维护者：编排总管会话（唯一可写本文件的会话；业务代码一律派单给子会话）。
事实源优先级：`docs/parallel/contracts.md`（Task-0 产出后）> 本看板 > `REVIEW.md` > `docs/BACKLOG.md`。
任务定义原始出处：`docs/superpowers/plans/parallel-build-plan.md`（gitignored 操作剧本，下称 plan）+ REVIEW.md 审计修正。
最后更新：2026-08-29 · **整合轮 5 收口（`integrate/round-5` @ `147df03`，9 轨合入，尚未并回主干）**。本次动作：§0 的 Y-1…Y-4 四行与 Z-1…Z-5 一行全部改写为 MERGED（含各轨合并提交、三点 diff、实测数字）、§3 加整合轮 5 实测一条、§7 加三行（Z 轮派单 / 整合轮 5 合 8 轨 / 补合 Y-4）。当前值：**596 passed**、replan **19 条**、`run.py` exit=0、`gen_docs --check` exit=0、`verify` **7/7 exit=0**（8 来源、warn **11 行 3 类**）、证据束出处 `caf45d2`、冻结面 diff 空。✅ **已并回主干**：人类授权后先打 tag `pre-round5-merge`@`f42ea83`，再 `merge --ff-only` 快进到 **`956e6af`**（31 个提交，纯 FF）；主干实测 596 passed / exit=0 / 工作区 0 行 / **`ahead 94`，仍未 push**（推送归人类）。同批清掉 `task-w5`/`task-x1..x4`/`integrate` 六个已合并 worktree 与分支；**Y/Z 九个刻意留着**（会话仍开着），**C/D 六个是另一会话 12:55 新建的，未碰**。
历史最后更新：2026-08-28 · **R-1 / R-3 合入主干（`f63de8b`），R 轮三个 worktree 重建到该 sha**。本次动作：§0 状态刷新（B/C/E/D 补记 MERGED、R 轮五行新增）、§2 增 G5（R 轮开工闸，已过）、§3 探针刷到 `f63de8b`（301 passed）、§4 增 R 轮任务卡、§7 补两行、§8 增 R 轮派单存档指针；另刷 `review/paste-{R0,R2,omega}.md` 与 `dispatch-common-p3.md` 抬头里写死的旧基线 `90251b3`→`f63de8b`。执行者为**非编排总管会话**（从 `~` 启动、hook 未挂载），**人类当场授权**——沿用 2026-08-27 20:49 那次的口径（本文件第 3 行的单写者规则）。合并方案由编排总管会话提出并试合验证，本会话按人类指派执行并逐项复跑。上一次实质动作见 §7 「2026-08-28 B/C/E/D 四轨派单」行。
**2026-08-28 16:30 增补**（同为非编排总管会话执行、人类当场授权「你代刷吧」）：核实 R-2 / Ω **早已开工**、R-0 是「只拿到一行下一步」的半轨已收口于 `cc0495b`（318 passed）→ §0 三行 `NOT_STARTED` 全部作废并改写；§3 补三个 worktree 当前实况；§4 三张 R 轮卡改写（R-0 卡记全派单三处修订）；§7 补一行；§8 存档例外由一条扩为两条。同批修订 `review/dispatch-R0.md`（94→155 行）与 `paste-R0.md`。**代码零改动。**
**2026-08-28 18:52 增补**（同为非编排总管会话执行、人类当场授权）：补 **W 轮**与**整合轮 3** —— §0 增 W-1…W-7 七行、§4 增「W 轮任务卡」一段（七张卡）、§7 增整合轮 3 一行。整合本身由**另一并行会话**执行（18:39–18:49，五轨并入 `fd3f7cc`），本会话只做**记录 + 当场复核**：五个尖端的 `merge-base --is-ancestor`、主干 **455 passed** / `run.py` exit=0、冻结面对 `01bc8d8` 零改动，均为本会话实跑，非转述。**代码零改动，只动本文件。**
前任编排会话（从 `~` 启动、hook 未挂载）产出的 Task-0 派单 v1 只在其会话输出里、未存档即失联，作废；本会话重拟为 v2（§8）。

状态机：`NOT_STARTED → DISPATCHED → DELIVERED → VERIFIED → MERGED`；`BLOCKED` = 需人类介入。
验收铁律：只认可复现的命令输出，不认自述；无输出回执停留 DELIVERED。

---

## 0. 状态总览

| 轨 | 状态 | 一句话 |
|---|---|---|
| Task-0 | **MERGED** | 2026-08-27 17:00 收口于 `0d0ccfe`，在主检出直接落地（未开分支）。冻结契约落 `docs/parallel/contracts.md`（八条 + 附录 A/B/C/D），pytest 100 passed。旧执行线 `task/0-contracts`@`5c70140` **已作废**：其正文增量已由 `e08cd49` 逐条并入本文件，分支已删、`.claude/worktrees/task-0` 已清。G2 过闸，五轨可开 |
| Task-A | **MERGED** | 2026-08-28 05:12 `f9bcd50` 并入主干、`59196ba` 记取舍。skill 层 + 真模型分支 + 两个 builtin skill + coding 经 invoker，另含五轨 fix 合流。**A 先于 B 落地**（派 B 时 B 尚未开工），不影响 D-03 余下顺序 |
| Task-B | **MERGED** | 2026-08-28 `af1e438` 并入主干（分支 `task/b-sandbox`）。合并期接缝修复见 `989d877`（B×C：带 tool_error 的报告不算真报告） |
| Task-C | **MERGED** | 2026-08-28 14:43 `b2319df` 并入主干（分支 `task/c-agents`） |
| Task-E | **MERGED** | 2026-08-28 14:54 `cf8c7ca` 并入主干（分支 `task/e-matrix`）。接缝修复 `8778081`（E×C：房间审批 fixture 补预置验收报告） |
| Task-D | **MERGED** | 2026-08-28 14:57 `fe4c77c` 并入主干（分支 `task/d-governance`）。接缝修复 `1900c0d`（D×C / D×B）、`90251b3`（补偿 workdir 缺省改必填，与 C-5 同口径）。四轨收口于 `90251b3`，pytest 257 passed |
| R-0 | **MERGED（部分交付）** | 2026-08-28 17:0x 经 `integrate/round-2` 并入主干。**只交了步骤 3/4**（第六道闸 `_gate_finance` + 注册表两条存量账，`cc0495b`）；**步骤 1/2/5 仍未做**（真 workdir、真 `GOOD_PATCH`、删 `seed_scripted_report`）—— 已转由 **W-7 `task/w7-software-seal`** 承接。worktree 与分支已于 17:06 清理（三项复核全过：无会话目录、0 独有提交、工作区干净） |
| R-1 | **MERGED** | 2026-08-28 `c5965d4` 并入主干（分支 `task/r1-refund-core` @ `946befd`，三点 diff **10 files / +1081**）。退款域地基：14 张业务表 + settled guard + D-05 场景编号扩至 1..7。合并后实测 **271 passed**、`run.py` exit=0。**worktree 与分支已于 2026-08-28 16:35 清理**（`git worktree remove` + `git branch -d`；删前实测工作区干净、`git rev-list --count HEAD..<该轨 HEAD>` = 0、无会话从该目录起过） |
| R-2 | **MERGED** | 2026-08-28 17:0x 经 `integrate/round-2` 并入主干（分支 `task/r2-refund-skills` @ `bc7ce36`，三点 diff **18 files / +2533**）。退款域 6 Skill + 4 Agent + 场景 6，内核零改动。合并期接缝修复见下方整合轮 2 三条。⚠️ worktree 与分支**暂未清理** —— 17:06 复核时该目录仍有活跃会话（16:58 有记录） |
| R-3 | **MERGED** | 2026-08-28 `f63de8b` 并入主干（分支 `task/r3-gateway` @ `3a2e473`，三点 diff **5 files / +1208**）。支付网关 + 错误码表。合并后实测 **301 passed**、`run.py` exit=0。**worktree 与分支已于 2026-08-28 16:35 清理**（`git worktree remove` + `git branch -d`；删前实测工作区干净、`git rev-list --count HEAD..<该轨 HEAD>` = 0、无会话从该目录起过） |
| Task-Ω | **MERGED** | 2026-08-28 17:0x 经 `integrate/round-2` 并入主干（分支 `task/omega-evidence` @ `9a7579b`，三点 diff **39 files / +6022**）。Trace + 证据束 + `verify.py` 七项 + compose。证据已按最终主线重跑，出处 `sha=9e5fd52`，**verify 5/5 PASS + 2 SKIP、exit=0**。⚠️ worktree 与分支**暂未清理**，同 R-2（16:58 仍有活跃会话） |
| W-1 | **MERGED** | 2026-08-28 18:39 经 `integrate/round-3` 并入主干（分支 `task/w1-refund-corpus` @ `15f79e8`，1 commit，三点 diff **10 files / +1667**）。退款域政策语料 + 三组对照数据集（`scenarios/refund/{policy,history,cases}`），**零代码**。合并期**无冲突**（五轨里唯一一次干净合入）。⚠️ worktree 与分支未清 —— 会话仍开着 |
| W-2 | **MERGED** | 2026-08-28 18:40 `cd21681`（分支 `task/w2-storeport` @ `683fa24`，三点 diff **7 files / +782**）。StorePort 抽象 + sqlite/pg 适配器，补 P1 第 7 步欠账。⚠️ worktree 与分支未清（会话仍开着） |
| W-3 | **MERGED** | 2026-08-28 18:47 `fa57c97`（分支 `task/w3-kb-rag` @ `e3c5a68`，2 commits，三点 diff **54 files / +6652 −963**）。两阶段检索 + 三条护栏 + RAG 有无对照实验（R5，砍序表**永不砍项**）+ 证据束全量重跑（新增 `scenario-R5`、补齐 `scenario-7`）→ `verify.py` 由 5/5 PASS + 2 SKIP 转 **7/7 PASS**。⚠️ worktree 与分支未清（会话仍开着） |
| W-4 | **NOT_STARTED** | 分支 `task/w4-matrix-room` @ `ec63983` 仍在，但 **worktree `.worktrees/task-w4` 已不存在**（2026-08-29 11:40 实测：`git worktree list` 无此项、目录不存在；何时清的无记录）。0 独有提交，**落后主干 18 个提交**（此前记「落后 2」是 08-28 快照）。派单 `review/paste-W4.md` 已备好未粘 —— 阻塞点仍是**需人类先注册 Synapse 账号** |
| W-5 | **MERGED** | 2026-08-29 11:11 经 `integrate/round-4` 并入主干，合并提交 `df96fa8`（分支 `task/w5-docs` @ `919f1f4`，1 commit，三点 diff **13 files / +2470 −43**）。文档生成器 `scripts/gen_docs.py` + 领域可移植性/权威事实/映射三份 + README 重写。⚠️ **此前本行记 `NOT_STARTED` 是错的** —— 08-28 写看板时确属未开工，其后开工并合入，看板未跟上。worktree 与分支未清（会话仍开着，工作区 0 行）|
| W-6 | **MERGED** | 2026-08-28 18:40 `e710262`（分支 `task/w6-refund-failure` @ `5bbbbb8`，三点 diff **6 files / +1049 −4**）。场景 7 退款失败路径：轮询超时 → 主管驳回 → 域内补偿收口，全程未进 settled。**worktree 与分支已于 18:46 清理**（`git worktree remove` + `git branch -d`；本会话复核：目录不存在、`task/w6-*` 分支不存在、尖端已是主干祖先） |
| W-7 | **MERGED** | 2026-08-28 18:41 `ec63983`（分支 `task/w7-software-seal` @ `0ee10d3`，三点 diff **8 files / +629 −171**）。软件域封版：真 workdir、真补丁、删 `testing.py` 的假绿回落、隔离探针不进业务判据 —— **R-0 欠的步骤 1/2/5 由此补齐**（见上方 R-0 行「已转由 W-7 承接」）。⚠️ worktree 与分支未清（会话仍开着） |
| X-1 | **MERGED** | 2026-08-29 11:11 经 `integrate/round-4` 并入主干，合并提交 `f3e48b5`（分支 `task/x1-demo-seal` @ `65d75aa`，1 commit，三点 diff **6 files / +255 −10**）。演示链路收口：场景 7 进 `DEFAULT_SCENARIOS`、场景 6 规划期接上 RAG。⚠️ worktree 与分支未清（会话仍开着，工作区 0 行）|
| X-2 | **MERGED** | 2026-08-29 11:11 同上，合并提交 `f602898`（分支 `task/x2-replan-gateway` @ `f86c94a`，1 commit，三点 diff **5 files / +778 −8**）。replan 第三条触发线：第七道闸认网关回执，四象限只有 `retriable+failed` 允许换渠道。⚠️ worktree 与分支未清 |
| X-3 | **MERGED** | 2026-08-29 11:11 同上，合并提交 `ada3885`（分支 `task/x3-rag-quality` @ `edb1934`，1 commit，三点 diff **6 files / +841 −77**）。RAG 检索质量收口：FTS 通道改按名次归一、R5 接上 W-1 语料、StorePort 分支首次真跑。⚠️ worktree 与分支未清 |
| X-4 | **MERGED** | 2026-08-29 11:11 同上，合并提交 `c0fced8`（分支 `task/x4-antifake` @ `8307a20`，1 commit，三点 diff **6 files / +637 −22**）。沙箱降级不再静默：探测补 tag、执行路径进证据、两处审计措辞纠错。⚠️ worktree 与分支未清 |
| Y-1 | **MERGED** | 2026-08-29 12:0x 经 `integrate/round-5` 并入，合并提交 `01f8ab7`（分支 `task/y1-exec-path` @ `9a01a1b`，1 commit，三点 diff **5 files / +494 −7**）。执行路径进证据：`test_report` 补 `sandbox_mode`，`verify.py` 的 4 条「执行路径不可审计」warn **归零**（本会话实测：合并前后各跑一次证据束对照）。⚠️ 此前那次在制品被冲掉的重做已完成，无残留 |
| Y-2 | **MERGED** | 同上，合并提交 `988513b`（分支 `task/y2-kb-provenance` @ `9853424`，1 commit，**8 files / +443 −46**）。场景 6 播上 W-1 语料（`candidate_count` 0 → **3**、`hit_count` 3）+ 规划期调用归树（3 条游离事件 → **0**）。`kb-hit` 由 4/4 涨到 **7/7** |
| Y-3 | **MERGED** | 同上，合并提交 `934df18`（分支 `task/y3-repro-path` @ `77df2c5`，1 commit，**5 files / +518 −18**）。复现路径收敛成**两条命令**（`make_evidence.py` 缺省一并产 R5）、缺库提示按目录名分支、第 5/7 项分母为 0 判 `[SKIP]` 不判 PASS。⚠️ **未消解 `-dirty` 缺口**（本会话全新克隆实测：R5 的 7 个文件首行 sha 仍带 `-dirty`，成因从「双命令」变成「同一进程里先写 1-7」），自查单该节已改写而非删除 |
| Y-4 | **MERGED** | 2026-08-29 12:3x 经 `integrate/round-5` 补合，合并提交 `783d9dd`（分支 `task/y4-gateway-demo` @ `63c4ba8`，1 commit，**9 files / +493 −39**）。场景 7 把换渠道重试演出来：40005 触发 replan 换渠道、`ACQ.SYSTEM_ERROR` 一票否决落人工。⚠️ **含 1 处超出派单白名单的改动**（`skills/builtin/refund/payment_execute.py` 新增 DELETE 悬空 `business_ref`，修 `business-ref 33/34`），**人类看过完整 diff 后当面授权合入**；另三处收窄断言属已授权范围 |
| Z-1…Z-5 | **全部 MERGED** | 2026-08-29 12:0x 经 `integrate/round-5` 并入，合并提交 `43d1c20`(Z-1) / `6ab01f4`(Z-2) / `0fa0d72`(Z-3) / `f50aac7`(Z-4) / `8f47ce5`(Z-5)。五轨均**只动材料面**（`docs/**` + `README.md`），与 Y 轮代码面**零文件交集**。Z-1 `docs/ppt-outline.md`（15 页六件套 + 十三条双向对照，**3 files / +663**）；Z-2 `demo-script.md` 封版（8 镜实跑掐表、总长 4:25，**3 files / +299 −78**）；Z-3 `domain-portability.md` 换端点（区间 A `90251b3..4a70cb0` 下 `contracts/` 与 `core/` **双零**，**3 files / +215 −25**）；Z-4 自查单重排 + 新增 `docs/open-questions.md`（**4 files / +329 −56**）；Z-5 新克隆冒烟 + README 修正（2 commits，**4 files / +300 −3**）|
| C-1…C-4 / D-1…D-2 | **另一会话新建，本会话未参与** | 2026-08-29 **12:55** 实测新出现六个 worktree：`task-c1`(`task/c1-matrix-room`)、`task-c2`(`c2-nio-live`)、`task-c3`(`c3-room-wiring`)、`task-c4`(`c4-matrix-evidence`) 基线 `f42ea83`；`task-d1`(`d1-human-exit`)、`task-d2`(`d2-plan-gate`) 基线 **`956e6af`**（= 整合轮 5 并回主干后的 sha，说明 D 两轨是**合并之后**建的）。六者均 **0 独有提交、工作区 0 行**，`review/paste-C*.md` / `paste-D*.md` **尚未落盘**。本行只记「已建轨」这一条实测事实 —— **范围与派单内容由建轨的那个会话补，别照分支名推断它们要做什么**（同 Z 轮建轨时的口径）|

---

## 1. 冻结决策表（人类已拍板，既定事实，不再讨论）

| # | 决策 | 内容 |
|---|---|---|
| D-01 | AGENT_POOL 注册口径 | `agents/__init__.py` 用 pkgutil 自动发现，Task-0 一次改完并冻结；同时删除 `main.py:16` 的手动注册行。理由：`worker.py:34` 在构造时读 `AGENT_POOL.items()`，注册必须早于 `build()`，包级 import 是唯一保证时机的位置；与 `skills/builtin/` 同一套 pkgutil 模式，一个概念不要两种写法。已否决：各 scenario 顶部 import（重复且易漂移）。 |
| D-02 | build() 签名 | 冻结为 `build(script, *, matrix=False, model=None)`；`model=None` 时按 script 构造 `ScriptedModelClient`。返回值六元组 `(store, bus, cp, model, worker, gate)` 顺序一并冻结（REVIEW High-2 的规格缺失由此补齐）。纯加法，不破坏现有三处六元组解包。已否决：scenario_2 内联拼装（留第二条构造路径必漂）。 |
| D-03 | 合并顺序 | **Task-0 → B → A → C → E → D → Ω**。B 先落地 sandbox 真实现，同时解掉 C（干跑闸）与 D（真实还原）的越界依赖。注意此顺序覆盖 plan §五的旧顺序（0→A→B→C→E→D→Ω），以 D-03 为准。**A 已于 08-28 先行 MERGED**（派 B 时 B 未开工），余下顺序不变：B → C → E → D → Ω。 |
| D-04 | 跨轨「阶段性断言」归属 | `maos/tests/test_registry_autodiscovery.py` 原则上四轨都不碰，但其中两条断言是写死在 Task-0 那个时点的，各轨落地后必红：`:170` `sorted(AGENT_POOL) == ["coding"]` 归 **Task-C**；`:257` `test_build_matrix_falls_back_to_inner_bus` 归 **Task-E**。裁决：点名开两个口子，**只许改被点名的那一处**，其余一行不动、不许 reformat；两处相距 87 行，C 先 E 后合并 git 可自动解。已否决：①由编排预先改 —— 改法取决于落地形态，改早了是瞎猜；②让它红着合并 —— 红测试进主干等于放弃回归闸。 |

---

## 2. 门禁

| 门禁 | 内容 | 状态 |
|---|---|---|
| G0 | REVIEW.md 顶部状态标记已刷新（五条已完成项标 ✅） | ✅ 2026-08-27；**2026-08-27 17:35 该段已整块下线**（Task-0 已 MERGED，不再维护第三层过时快照），REVIEW.md 顶部改为一行指向本看板 |
| G1 | D-01 / D-02 已注入本看板冻结决策表 | ✅ 2026-08-27（§1） |
| G2 | Task-0 已 MERGED，且 contracts.md 存在、条目齐全（七条 + C-8 gitignore = 八条，见 §5） → 未过不许开任何 worktree | ✅ **2026-08-27 17:00 过闸**（Task-0 MERGED @ `0d0ccfe`；`docs/parallel/contracts.md` 八条齐全 + 附录 A/B/C/D；pytest 100 绿） |
| G3 | `docker info` 已执行、结果已写进环境探针 → 未过不许派 Task-B | ✅ **2026-08-28 复测过闸**（`docker info` exit=0，Docker Desktop 在跑；`docker image ls maos-sandbox` 无该镜像，归 Task-B 第 1 步 build）。08-27 09:57 的 ❌ 回退由此解除 |
| G4 | 合并严格按 D-03 顺序；前一轨未 VERIFIED 不许合下一轨 | 持续执行中 |
| G5 | **R 轮开工闸**：R-1 / R-3 已并入主干、三个 R 轮 worktree 已重建到该 sha 且工作区干净、全量测试绿 → 未过不许粘 R-0 / R-2 / Ω 派单 | ✅ **2026-08-28 过闸**（主干 `f63de8b`，`pytest` **301 passed**、`run.py` exit=0；`.worktrees/task-r0\|task-r2\|task-omega` 三者 `git rev-parse HEAD` 均为 `f63de8b`、`git status --porcelain` 均空；`paste-*.md` 抬头基线已同步刷新） |

---

## 3. 环境探针（2026-08-27 编排会话实测）

- **2026-08-29 12:0x–12:4x 整合轮 5 合入后实测（`integrate/round-5` @ `147df03`，尚未并回主干）**：`python3 -m pytest maos/tests -q` → **596 passed in 8.70s**；`python3 -m pytest maos/tests/test_replan_gateway.py -q` → **19 passed**（X-2 那 19 条一条没少）；`python3 run.py` → **exit=0**；`python3 scripts/gen_docs.py --check` → **exit=0**（本轮重生成过两次 `docs/agent-identity.md`：Y-1 令 `testing.py` 行号 86→107、Y-4 令 `payment_agent.py` 行号 35→58）；`python3 scripts/verify.py` → **7/7 PASS、exit=0**，`hash-integrity 81/81` / `business-ref 33/33` / `kb-hit 7/7`，另有 **11 行 warn / 3 类**；证据束已按合并后 HEAD 重跑并入库，出处 `sha=caf45d2`（此前一直停在 `df96fa8`）；`git diff --stat f42ea83 HEAD -- maos/contracts/ .contracts.lock maos/artifacts.py scripts/guard_bash.py` → **空**（冻结面零改动）。**全新克隆冒烟**（无任何 API key，显式 unset 全部 `MAOS_*`/`MATRIX_*`/key）：`clone → make_evidence.py → verify.py` **5.4 秒**到 `RESULT: 7/7 PASS`，七条全跑满 **18.3 秒**，距自查单 15 分钟预算差两个数量级。⚠️ **warn 第 3 类是 Y-4 带来的新增**：`authoritative-fact` 项下「scenario-7 case=case-s7-0001: 有回执但 biz_status 不是 settled」—— 主渠道那笔真收到过回执而全案落人工审批，**预期内、判定不受影响**；但 `docs/submission-checklist.md` A-2 刚立的判据写的是「就是这 10 行 2 类，多出来的才要查」，照它执行会把这条判成回归，本轮已改。⚠️ 本会话从 `~` 启动，项目级守卫 hook 未加载（已知形态）

- **2026-08-29 11:35–11:42 整合轮 4 合入后实测（主干 `42822fc`）**：`python3 -m pytest maos/tests -q` → **521 passed in 7.00s**、exit=0；`python3 run.py` → **exit=0**，且跑完 `git status --porcelain` 仍 **0 行**（`run.py` 不写 `evidence/`）；`python3 scripts/verify.py` → **exit=2**，报 `缺数据库: evidence/scenario-7/maos.db` —— **不是回归**，`*.db` 按设计不入库，要在主检出复现那一屏必须先跑 `python3 scripts/make_evidence.py`（同 08-28 18:39 那条）。冻结面自验：`git diff --name-only 4a70cb0 002e4af`（全量 **78 个文件**）里命中 `docs/parallel/contracts.md` / `maos/artifacts.py` / `scripts/guard_bash.py` / `.contracts.lock` 的 = **0**。证据束出处仍是 `sha=df96fa8`，比主干少两个提交（`002e4af` 纯文档 + `42822fc` 只改 `CLAUDE.md`）—— 实测 `git diff --name-only df96fa8 42822fc` **零个代码文件**，故证据束**未过期**，留到下一整合轮再重跑即可。⚠️ 本会话从 `~` 启动，项目级守卫 hook 未加载（已知形态），故全程只动 `docs/ops/ORCHESTRATION.md` 与 `CLAUDE.md` 一行，业务代码零改动
- **2026-08-28 16:30 三个 R 轮 worktree 实况（本次动作，当场实测）**：`task-r0` 已前移到 **`cc0495b`**（R-0 半轨交付，`pytest` **318 passed** / `run.py` exit=0 / `git status --porcelain` 空，均在该 worktree 内实跑）；`task-r2` 仍 `f63de8b`、0 commit，未入库产出 `maos/skills/builtin/refund/` + `maos/agents/refund/`；`task-omega` 仍 `f63de8b`、0 commit，未入库产出 `maos/obs/trace.py`、`scripts/{make_evidence,verify}.py`、`maos/tests/test_trace_evidence.py`、`deploy/docker-compose.yml`、`evidence/scenario-1..5/`+`INDEX.json`。**下一条（G5 过闸那条）里「三者 HEAD 均为 `f63de8b`」是 08-28 过闸当时的快照，对 `task-r0` 已不再成立**，按「结论性断言改、过程记录留」口径原样保留。
- **2026-08-28 R-1/R-3 合并后实测（主干 `f63de8b`）**：`python3 -m pytest maos/tests -q` → **301 passed**（合 R-1 后 271、再叠 R-3 后 301，两步各自实测）；`python3 run.py` → exit=0；`docker image ls maos-sandbox` → `maos-sandbox:latest` 247MB **在**；`git status -sb` → `ahead 19`，**仍未 push**；`git diff --stat 90251b3 f63de8b -- maos/contracts/ .contracts.lock maos/artifacts.py docs/parallel/contracts.md` → **空**（冻结面零改动）。三个 R 轮 worktree 均 `f63de8b`、`git status --porcelain` 均空；在 `.worktrees/task-r2` 内复跑同样 **301 passed / exit=0**。回滚锚点：合并前主干 `90251b3`，已打 tag `pre-r1r3-merge`。⚠️ **口径说明**：三个 worktree 钉在 `f63de8b`（= R-3 的合并提交，也是本轮的**代码基线**），而主干 HEAD 会因本次看板更新等纯文档提交继续前移 —— 二者不追平是**有意的**：文档提交不影响任何一轨执行，每刷一次文档就平移一次 worktree 只会制造无穷回归。派单抬头里写的基线 `f63de8b` 指的就是代码基线，与主干 HEAD 不必逐字相等。
- `docker info` → **2026-08-28 编排会话复测 exit=0**，daemon 可达，**G3 过闸**；`docker image ls maos-sandbox` → 无该镜像（归 Task-B 第 1 步 build）。历史：08-27 早间 exit=0、09:57 复测 exit=1（`Cannot connect to the Docker daemon`）致 G3 回退 ❌，该回退今已解除。
- `python3 -m pytest maos/tests -q`（仓库根）→ **134 passed**（2026-08-28 编排会话实测于 `59196ba`）；同基线 `python3 run.py` → exit=0；`git diff --stat maos/contracts/` 为空（冻结契约未被动过）。以下为历史值，保留不删：
  - **101 passed**（2026-08-27 20:49 实测于 `fe6cfff`）。构成实测：test_contracts 9 + test_contracts_frozen 2 + test_guard_bash 73 + test_registry_autodiscovery 17 = 101。**此前此处的 101 系「100 存量 + 1 新增」推算、未实测**，且构成里 guard_bash 记 72（四项和为 100，与 101 对不上），今按实测改写。09:57 的 83、plan §三「11 条测试全绿」均已过时。
- HEAD = `59196ba`（2026-08-28 实测）。branch `goai-restructure` **本地领先 origin 24 个提交**（`git status -sb` → `ahead 24`）—— track-a 与五轨 fix 的合并全部只在本地，**未 push**。
  - 历史：2026-08-27 20:49 时 HEAD = `fe6cfff`（REVIEW 审计基线 `f104161` + 15 commits；`76ea101` 之后的六个见 §7 补记），与 origin 同步。
- 三重守卫：deny 13 条 + PreToolUse hook（`scripts/guard_bash.py`）+ 指纹（`.contracts.lock` 已入库 `9d3fe4d`）= **3/3 生效**（`docs/BACKLOG.md:7` 实测记录）。
- `maos/tools/sandbox.py` 桩已存在（`9d3fe4d`），两签名与 plan §二.14 逐字段一致，NotImplementedError 桩。
- ⚠️ **本轮（2026-08-28）编排会话同样从 `~` 启动，hook 未挂载** —— 与前任同一形态（BACKLOG.md:8）。故本轮编排只写 `review/` 派单与本看板，**未碰任何业务代码**；派单文件当场存档，不重蹈前任「派单只在会话输出里、未存档即失联」的覆辙。
- 前任编排会话曾从 `~` 启动（hook 未挂载，BACKLOG.md:8）。**现任编排会话（maos-da）从仓库根启动，09:55 实测 Read `scripts/guard_bash.py` 被拦 → hook 对编排会话生效**。「一切子会话必须从仓库根 / worktree 根启动 + 开工先探守卫」仍是派单硬要求。
- ✅ **`.claude/settings.json` 已入库**（`3a36f37`，2026-08-27 13:08）→ §六.3 收口完成，新开的 worktree 里 deny + hook 两重守卫随仓库分发，不再只剩指纹一重。`settings.local.json` 属本机私有，仍不入库。
- **保护面已收口（2026-08-27 17:35）**：`docs/parallel/contracts.md` 与 `maos/artifacts.py` 加进 `scripts/guard_bash.py` 的 `PROT_PATHS`，同时进 `READ_OK` —— **写拦、读放行**（执行器要照着契约写代码）。这推翻了 §7 里 14:08 那条「contracts.md 不在守卫面」的记录，人类终端 fallback 那套 cp 手法不再需要。`CLAUDE.md`「必须问的四类」第 1 条已同步扩为同样四条路径。把守测试：`maos/tests/test_guard_bash.py::test_contract_docs_and_artifacts_are_write_blocked_read_allowed`。
- `.contracts.lock` 已 relock（2026-08-27 17:35）：补进 Task-0 在 `e9b0e2f` 新增的 `knowledge` 表指纹，表数 5 → 6。此前该表未被指纹覆盖，改其 DDL 不会被 `test_contracts_frozen` 发现。

---

## 4. 任务卡

### Task-0 · 冻结契约 + 入口分发器重构（串行首位）
- 状态：**MERGED**（2026-08-27 17:00 @ `0d0ccfe`）｜ 执行位置：主仓 `/Users/shensikai/Documents/MAOS`，branch goai-restructure，**未开 worktree**
- scope（plan §一 Task-0 + REVIEW 修正 + D-01/D-02）：
  - 新建：`maos/skills/contract.py`、`registry.py`、`invoker.py`、`builtin/__init__.py`（pkgutil 动态发现）、`maos/tools/port.py`、`maos/artifacts.py`、`maos/flows/`（`__init__.py` + `common.py` + `scenario_1..4.py` 迁移 + `scenario_5.py` 占位）、`contracts.md`（仓库根；2026-08-27 按人类手写单口径定为根目录，原 docs/parallel/ 口径作废）
  - 修改：`maos/core/store.py`（唯一一次：+knowledge 表）、`maos/agents/base.py`（挂 SkillInvoker）、`maos/runtime/worker.py`（传 store）、`maos/agents/__init__.py`（D-01 pkgutil）、`maos/main.py`（分发器 + 删 :16 手动注册）、`run.py`、`maos/model/client.py`（select_model_client 最小实现，随后移交 A）、`.gitignore`（+`!.env.example` +`!deploy/.env.example`，REVIEW 阻断项 5 的修复）
  - 核对不重建：`maos/tools/sandbox.py` 桩（已存在，写进 contracts.md 并声明移交 B）
- 依赖：无
- 验收（已达成，实测 **100 passed**）：存量 83 条 + 新增测试全绿；`python3 run.py` 四场景输出不变；`python3 run.py --scenario 3` 单跑正常；contracts.md 七条齐全（§5）；「新文件放进 builtin/ 无需改 `__init__` 即被注册」有证明测试
- 验证命令：`python3 -m pytest maos/tests -q` ； `python3 run.py` ； `python3 run.py --scenario 3`

### Task-B · 容器沙箱 2 ToolPort + 演示靶场 + test.verify
- 状态：NOT_STARTED ｜ worktree `.worktrees/task-b`，branch `task/b-sandbox`
- 独占文件（plan §一）：`maos/tools/sandbox.py`（接收 Task-0 移交，填实现）、`deploy/sandbox.Dockerfile`、`scenarios/fixture-repo/**`、`maos/skills/builtin/test_verify.py`、`maos/tests/test_sandbox_isolation.py`
- 依赖：Task-0（G2）；docker daemon（G3 已过）
- 验收（plan §三）：镜像可 build；隔离三探针（降级路径 no_network 允许 skip，其余两条必绿）；conftest 拒改负例；工具错/用例错分报；fixture-repo 按 §二.16 冻结口径造（`auth/session.py::is_session_valid` 时区 bug；`tests/test_session.py::test_expired_session` 打补丁前挂、`test_valid_session` 过）
- 验证命令：`docker build -t maos-sandbox -f deploy/sandbox.Dockerfile .` ； `python3 -m pytest maos/tests -q -k sandbox`

### Task-A · 真模型客户端 + 首发 2 Skill + Coding 经 invoker
- 状态：NOT_STARTED ｜ worktree `.worktrees/task-a`，branch `task/a-skills`
- 独占文件（plan §一）：`maos/model/client.py`（接收 Task-0 移交）、`maos/skills/builtin/req_normalize.py`、`code_repo_patch.py`、`maos/agents/coding.py`、`maos/tests/test_skills.py`
- 依赖：Task-0（G2）
- 验收（plan §三）：≥5 条新测试（注册/取版本/越权/retry/落库）；无 key Scripted 通；有 key 场景 1 真模型通；异常不回显 key；env 口径按 `docs/phases/phase-1.md:32-33`（`MAOS_LLM_BASE_URL / MAOS_LLM_API_KEY / MAOS_LLM_MODEL`，timeout 默认 120s）
- 验证命令：`python3 -m pytest maos/tests -q` ； `python3 run.py` ； `MAOS_LLM_API_KEY=... python3 run.py --scenario 1`（有 key 项可后补，只影响该单项验收）

### Task-C · 四 Agent + Gate 严格化 + 补偿干跑闸 + 场景 1/2 新 DAG
- 状态：NOT_STARTED ｜ worktree `.worktrees/task-c`，branch `task/c-agents`
- 独占文件（plan §一）：`maos/agents/requirement.py`、`architecture.py`、`testing.py`、`reviewer.py`、`maos/runtime/gate.py`、`maos/flows/scenario_1.py`、`scenario_2.py`、`maos/tests/test_agents_gate.py`
- 依赖：Task-0（G2）。运行期依赖 B 的沙箱、A/D 的 skill——并行期按冻结签名 import 桩 + 按名调用（未注册→failed 结果兜底），合并后联调
- 验收（plan §三 + REVIEW High-3 修正）：代码类无 test_report 即 blocker；非代码类沿用 self_check；effect_risk=H 走干跑闸（并行期用 contracts.md 的 compensation golden fixture 手工构造验，**合并在 B 之后（D-03），合并期即可真联调**）；kb.retrieve/issue.aggregate 缺席不阻塞；Agent 注册按 D-01（pkgutil 自动发现，**不改** `agents/__init__.py`，放文件即注册）；场景 2 迁移用 D-02 的 `build(..., model=...)` 注入 FlakyModel，不内联拼装
- 验证命令：`python3 -m pytest maos/tests -q -k "agents or gate"` ； `python3 run.py --scenario 1`（Scripted）

### Task-E · Matrix 镜像总线 + 房间审批
- 状态：NOT_STARTED ｜ worktree `.worktrees/task-e`，branch `task/e-matrix`
- 独占文件（plan §一）：`hiclaw/matrix_bus.py`、`.env.example`（根目录；Task-0 已修 .gitignore 才交付得进）、`maos/tests/test_matrix_bus.py`
- 依赖：Task-0（G2；含 MatrixEventBus config 形状契约，contracts.md 第 7 条）
- 验收（plan §三）：降级模式行为与 inner bus 完全一致；`/approve /reject` 解析（合法/非法/越权）；不装 [e2e]，遇加密房降级 log-only
- 验证命令：`python3 -m pytest maos/tests -q -k matrix`

### Task-D · 聚合/知识/补偿执行/Replan/场景 5
- 状态：NOT_STARTED ｜ worktree `.worktrees/task-d`，branch `task/d-governance`
- 独占文件（plan §一）：`maos/skills/builtin/issue_aggregate.py`、`kb_sink.py`、`kb_retrieve.py`、`scenarios/inputs/**`、`maos/core/control_plane.py`、`maos/runtime/plan_finalizer.py`、`maos/flows/scenario_5.py`、`maos/tests/test_governance.py`
- 依赖：Task-0（G2）。运行期靠 B 的反向 apply——**按 D-03 D 排最后，合并时 B 早已 MERGED，「reject→补偿→文件真实还原」可在合并期完整验收，无需拆段**（REVIEW Critical-2 的时序矛盾由 D-03 化解；并行开发期本地测试仍需用 golden fixture + 桩）
- 验收（plan §三）：场景 5 无 key 确定性 DONE；reject→补偿→文件真实还原；干跑失败负例；replan 三边界；knowledge 有沉淀；补偿引用自动附着按 plan §二.15（Control Plane 侧，记 DECISIONS）
- 验证命令：`python3 -m pytest maos/tests -q -k governance` ； `python3 run.py --scenario 5`

### Task-Ω · 可观测 + 证据束 + 部署 + 集成联调（串行收口）
- 状态：NOT_STARTED ｜ 执行位置：主仓（A–E 全部 MERGED 后）
- 独占文件（plan §一）：`maos/obs/trace.py`、`obs/otel.py`（可选）、`scripts/make_evidence.py`、`deploy/docker-compose.yml`、`deploy/.env.example`
- 依赖：A–E 全部 MERGED
- 验收（plan §三）：五场景证据落盘（首行时间戳+sha、脱敏、真实 subprocess）；trace.json 可被 jq 解析；compose 起场景 1
- 验证命令：`python3 scripts/make_evidence.py` ； `docker compose -f deploy/docker-compose.yml up` ； 全量 pytest

### R 轮任务卡（P3 · 2026-08-28）

> 五轨的 scope / 独占文件清单 / 验收命令**原文在 `review/dispatch-{R0,R2,omega}.md`**（该目录由 `.git/info/exclude` 排除、不入库）。此处只记状态与指针，**不复制正文**，避免两处漂移。

#### R-1 · 退款域地基（14 张业务表 + settled guard + D-05）
- 状态：**MERGED**（2026-08-28 `c5965d4`）｜ 分支 `task/r1-refund-core` @ `946befd`（3 commits；三点 diff 10 files / +1081）
- 落地面：新建 `maos/domain/__init__.py`、`maos/domain/refund/{__init__,guard,objects}.py` + `schema.sql`、`maos/tests/test_refund_domain.py`、`maos/tests/fixtures/refund/case_r1.json`；改 `maos/main.py` **一行**（D-05，`ALL_SCENARIOS=(1..7)`）
- 合并期事实：其基线选在 `b2319df`（当时只并了 B/C，**该基线本身 2 failed**），故单跑是红的；合到 `90251b3` 后 **271 passed**，继承的红全消 —— 红不是 R-1 造成的。BACKLOG / DECISIONS 的 `## task-R1` 小节已随合并入库
- 冲突面：仅 `docs/BACKLOG.md` + `docs/DECISIONS.md` 尾部追加，按「两侧都留、HEAD 在前」解，**代码零冲突**

#### R-3 · 支付网关 + 错误码表
- 状态：**MERGED**（2026-08-28 `f63de8b`）｜ 分支 `task/r3-gateway` @ `3a2e473`（三点 diff 5 files / +1208）
- 落地面：新建 `maos/tools/gateway.py`、`maos/tools/gateway_codes.py`、`maos/tests/test_gateway.py`；`docs/DECISIONS.md` 追加 `## task-R3`
- 合并后 **301 passed**、`run.py` exit=0；冲突面同 R-1（仅两份 docs 尾部）

#### R-0 · 软件域封版：演示链路真连沙箱 + Gate 收口
- 状态：**DISPATCHED（部分）**｜ worktree `.worktrees/task-r0` @ `task/r0-seal` @ **`cc0495b`** ｜ **合并顺位第 1**
- 派单：`review/dispatch-R0.md`（粘贴版 `review/paste-R0.md`）—— **2026-08-28 16:30 已修订，见下**
- **已交付的半轨（`cc0495b`，5 files / +320）**：步骤 3 第六道闸 `_gate_finance`（复核合 F-1，`runtime/` 无 `domain` import）+ 步骤 4 注册表改名与过时注释；含 `test_gate.py` +205 行与两份 docs 的 `## task-R0` 小节。该会话**只拿到一行「下一步」、没拿到派单**，故步骤 1/2/5 一件没做。已让它收口提交并停，工作树干净
- **派单三处修订**（原版按字面执行会走死）：
  1. 「删 `seed_scripted_report` 及其**三处**调用点、全仓 grep 零残留」**是错的** —— 实测 **5 处调用跨 4 个场景 + 1 个测试**（`scenario_1:98`、`scenario_2:133/135`、`scenario_3:30`、`scenario_5:127`、`test_matrix_bus.py:340`），后三处不在 R-0 独占文件里，而 R-2 / Ω 派单都禁改 `flows/` → **本轮无人拥有**。照原版执行必违反边界第 1 条或卡死在验收 grep 上
  2. 改为按判据划线：**宣称「真跑」的场景（1/2）不许有脚本化报告；不跑测试的场景（3 审批 / 5 补偿）报告是前置条件，可预置** —— 依据是 `scenario_3.py:11-13` 自述。故 **删** `testing.py::_report_from` 的 `scripted_report` 回落（假绿路径：沙箱挂了换假报告交出去，演示当天 Docker 一挂屏幕照样全绿而我们不知道），**留** `seed_scripted_report()` 函数本体（Ω 的 `trace.py` 正为「预置件无来源事件」写绕行判据，删了它那套当场作废）
  3. 新增 `4bis 不许回退`：R-0 要重写的三个文件正压着 `fix-2` / `fix-3` 的成果，而这三处当初都是「改回去也不会红」的缺口 → 钉死 `gate.py` 的 `self_check` 非 dict 不抛、`scenario_1.py:88-89` 的 `model=select_model_client(script)` 注入、`scenario_2.py:100-114` 的 `FlakyModel` 按「返工」二字分派，各补一条回归断言
- **重粘时的自检期望值**（编排侧已在该 worktree 内当场实测）：`git log --oneline -1` → `cc0495b`；`pytest` → **318 passed**；`run.py` → exit=0；`git status --porcelain` → 空。派单顶部已加「基线覆盖」一节写死这四条
- 它要收的账见 BACKLOG `## merge-p2` 第一条：场景 1/2 的 `workdir` 硬编码 `/tmp/maos-sandbox` 而全仓无人准备、`GOOD_PATCH` 是打不上的假 diff（靶场文件叫 `auth/session.py`）、隔离探针混进 `test.verify` 报告 —— **三件必须同批做，拆开做场景就红**

#### R-2 · 退款域 6 Skill + 4 Agent + 场景 6
- 状态：**DISPATCHED**（已开工，0 commit）｜ worktree `.worktrees/task-r2` @ `task/r2-refund-skills` @ `f63de8b` ｜ **合并顺位第 2**
- 已有未入库产出：`maos/skills/builtin/refund/`、`maos/agents/refund/`。粘的是 16:12 刷新前旧版（抬头 `90251b3`/257），该会话已自证实为 `f63de8b`/301 并自行纠正 → **不重粘**
- 派单：`review/dispatch-R2.md`（粘贴版 `review/paste-R2.md`）；其开篇「R-1 的地基已在主干」的前提**已于本次合并成立**（见 §0）
- 开工前须知（BACKLOG `## task-R1` 第 5 条）：`schema.sql` 由 `ensure_schema()` 一次性 `executescript`、全是 `CREATE TABLE IF NOT EXISTS`，**没有迁移路径** —— 加表可以，**改列静默无效**。本轮要动这 14 张表的列就直接改 `schema.sql` 并重建库（演示期都是 `:memory:`，无历史数据）

#### R-Ω · Trace + Evidence Bundle + verify.py + 部署
- 状态：**DISPATCHED**（已开工，0 commit）｜ worktree `.worktrees/task-omega` @ `task/omega-evidence` @ `f63de8b` ｜ **合并顺位第 3（末位收口）**
- 已有未入库产出：`maos/obs/trace.py`、`scripts/{make_evidence,verify}.py`、`maos/tests/test_trace_evidence.py`、`deploy/docker-compose.yml`、`evidence/scenario-1..5/` + `INDEX.json`。同 R-2，粘的是刷新前旧版，已自行纠正 → **不重粘**
- ⚠️ 与 R-0 的接缝：`trace.py` 的 provenance 判据是为「预置 test_report 没有来源事件」写的。R-0 将删掉**场景 1/2** 的预置、保留 `seed_scripted_report()` 供场景 3/5 用 → 该判据**继续成立**，但场景 1/2 的证据束会从「预置件」变成真产物，合并期需复跑 `verify.py` 复核
- 派单：`review/dispatch-omega.md`（粘贴版 `review/paste-omega.md`）
- 注意上方「Task-Ω」那张卡是 **P2 轮口径**（依赖写的是「A–E 全部 MERGED」），本轮以本卡与派单原文为准

### W 轮任务卡（P4 / P5 · 2026-08-28）

> 七轨的 scope / 独占文件清单 / 验收命令**原文在 `review/paste-W{1..7}.md`**（该目录由 `.git/info/exclude` 排除、不入库，靠粘贴交付）。此处只记状态与指针，**不复制正文**。W 轮**没有 `dispatch-W*.md`**，粘贴版即原文。
> 派单节奏（本会话实读文件 mtime 与各 worktree 会话首条消息）：七份派单 **17:38** 落盘；W-1 / W-2 / W-3 / W-7 四轨 **17:54–17:55** 粘贴开工；W-6 亦已交付；**W-4 / W-5 从未粘贴**。
> 共同基线 **`01bc8d8`**（五个已合轨的 `git merge-base` 实测均为它）。⚠️ §7 上一行（17:05–17:07）写的平移目标是 `01824f2` —— 那是**当时快照**，其后 worktree 又随看板提交 `01bc8d8` 前移一格，以本行为准。

#### W-1 · 退款域政策语料与三组对照数据集
- 状态：**MERGED**（18:39 `823dc54`）｜ 分支 `task/w1-refund-corpus` @ `15f79e8`（1 commit；三点 diff 10 files / +1667）
- 落地面：新建 `scenarios/refund/README.md` + `policy/policy_rules.json` + `history/history_cases.json` + `cases/case_{r3a,r3b,r4a,r4b,r6}.json`；两本账追加 `## task-W1`
- **本轨零代码**，合并期零冲突。BACKLOG `## task-W1` 记了三条不当场处理的账，均在其可改面之外

#### W-2 · StorePort 抽象与 sqlite 适配器（补 P1 第 7 步欠账）
- 状态：**MERGED**（18:40 `cd21681`）｜ 分支 `task/w2-storeport` @ `683fa24`（三点 diff 7 files / +782）
- 落地面：新建 `maos/store/{__init__,port,sqlite_store,pg_store}.py`、`maos/tests/test_store_port.py`
- 冲突面：仅两本账尾部，**代码零冲突**
- ⚠️ BACKLOG `## task-W2` 六条账里的**前两条是 SQLite FTS5 自身行为、都不报错** —— 其原话是「W-3 的检索器不知道就会踩」。W-3 已在其后合入，**是否踩到未核**

#### W-3 · KB/RAG 层：两阶段检索 + 三条护栏 + R5 对照实验（永不砍项）
- 状态：**MERGED**（18:47 `fa57c97`；其后 `fd3f7cc` 按 `fa57c97` 重跑证据束）｜ 分支 `task/w3-kb-rag` @ `e3c5a68`（2 commits；三点 diff 54 files / +6652 −963）
- 落地面：新建 `maos/kb/{__init__,retriever,guardrails,experiment}.py` + `kb/schema.sql`、`maos/skills/builtin/kb_retrieve.py`、`maos/tests/test_kb_retriever.py`；改 `maos/agents/manager.py`；`evidence/` 八个场景目录全量重写（新增 `scenario-R5`、补齐 `scenario-7`）
- **兑现点**：`verify.py` 第 5 项（KbRetrieved 的 doc_id 可查）与第 7 项（history_case 可追溯）由 SKIP 转 PASS → **7/7 PASS**（本会话在 `.worktrees/integrate` 内同 sha 复跑 exit=0）
- ⚠️ BACKLOG `## task-W3` 首条：`flows/scenario_6.py:228` 仍用 `ManagerAgent(model)` 老写法、`SkillInvoker.store is None` → **规划期检索在场景 6 恒返回空**，`MAOS_KB_ENABLED` 开关对它没有任何影响。**本轮无人认领**

#### W-4 · Matrix 真房间联通 + 审批监听 + 状态迁移镜像
- 状态：**NOT_STARTED**（0 commit、工作区干净、无会话目录）｜ worktree `.worktrees/task-w4` @ `task/w4-matrix-room` @ `ec63983`（**落后主干 2 格**）
- 派单：`review/paste-W4.md`（已备好，**未粘**）。**阻塞点：需人类先注册 Synapse 账号** —— 派单里写明「卡在这一步就停下来问，不要自己瞎试」
- 重粘前须刷抬头基线（现写死 `01bc8d8` / 383 passed）并把 worktree 平移到当时的主干

#### W-5 · 文档生成器 + 材料骨架（Phase 7）
- 状态：**NOT_STARTED**，同 W-4（`.worktrees/task-w5` @ `task/w5-docs` @ `ec63983`，0 commit、干净、无会话目录）
- 派单：`review/paste-W5.md`（已备好，**未粘**）。无外部阻塞，抬头基线同样待刷

#### W-6 · 场景 7 退款失败路径（Demo 分镜主线 · 永不砍项）
- 状态：**MERGED**（18:40 `e710262`）｜ 分支 `task/w6-refund-failure` @ `5bbbbb8`（三点 diff 6 files / +1049 −4）｜ **worktree 与分支已于 18:46 清理**
- 落地面：新建 `maos/flows/scenario_7.py`、`maos/tests/test_refund_failure.py`；改 `maos/skills/builtin/refund/{__init__,compensate}.py`
- ⚠️ **演示缺口（本会话当场实读源码）**：`maos/main.py:26` 的 `ALL_SCENARIOS` 已含 7，但 `:29` 的 `DEFAULT_SCENARIOS` 仍是 `(1,2,3,4,5,6)` —— **`python3 run.py` 无参跑不到场景 7**，而场景 7 正是 Demo 分镜主线。`main.py` 是冻结面、W-6 派单明写「一个字不许动」，所以这一格只能由编排侧解冻来补；与 17:05 那次 `(1,2,3,4)`→`(1..6)` **同一形态**
- ⚠️ BACKLOG `## task-W6` 首条：手册 R2 的「网关可重试错误码 → replan 换渠道 → 达上限 → needs_human」这一段**没有落地**，要改 `ControlPlane._should_replan`（在其边界外）

#### W-7 · 软件域封版：真 workdir + 真补丁 + 删假绿路径（本轮最高优先级）
- 状态：**MERGED**（18:41 `ec63983`）｜ 分支 `task/w7-software-seal` @ `0ee10d3`（三点 diff 8 files / +629 −171）
- 落地面：改 `maos/flows/{common,scenario_1,scenario_2}.py`、`maos/agents/testing.py`、`maos/runtime/gate.py`、`maos/tests/test_agents_gate.py`
- **它了结的是 R-0 的欠账**：R-0 派单步骤 1/2/5（现造真 workdir、真打得上的 `GOOD_PATCH`、删 `testing.py::_report_from` 的 `scripted_report` 假绿回落）到本轨才落地，`## merge-p2` 第 1、2 条一并收口
- ⚠️ **待核**：合并后在 `fd3f7cc` 上跑 `verify.py`，business-outcome 项对 **scenario-1 / scenario-2 各仍报一条 warn**「1 条外部判据来源未审计（场景预置件，非实跑产出）」（同类 warn 也出现在 scenario-3/5，那两个是**允许**预置的）。该项整体 PASS、不影响 7/7，但与「软件域已封版、判据来自真跑」的说法有出入，**本会话未追到底**

冻结备注：`flows/scenario_3.py` / `scenario_4.py` 在 Task-0 之后冻结，无人再碰（REVIEW Medium-3 的归属澄清）。

---

## 5. contracts.md 八条清单（G2 验收口径 · 2026-08-27 与人类手写单合并后）

REVIEW.md 全部「必须写进 contracts.md / 由 Task-0 定死」修正项的增量核对单（与 plan §二 的 16 条规格并存，不互斥）：

1. `build()` 入参签名：`build(script, *, matrix=False, model=None)`（D-02）
2. `build()` 返回契约：六元组 `(store, bus, cp, model, worker, gate)` 顺序冻结（REVIEW High-2）
3. Agent 注册口径：`agents/__init__.py` pkgutil 自动发现，放文件即注册，任何任务不得改此文件（D-01）
4. `skills/builtin/__init__.py` pkgutil 动态发现 + 自带证明测试（REVIEW Critical-3）
5. sandbox 两签名（`sandbox_git_apply` / `sandbox_pytest_run`，桩已建）+ 所有权移交 Task-B（REVIEW Critical-1/2）
6. compensation artifact golden fixture（C 干跑闸 / D 执行器共同测试基准）（REVIEW High-3）
7. `MatrixEventBus(inner_bus, config)` 的 config 形状（dataclass 或明确 dict 键集）+ 必须实现 publish/subscribe/drain 三方法（REVIEW Medium-1）
8. `.gitignore` 补 `!.env.example` 与 `!deploy/.env.example`，一次改完冻结（REVIEW 阻断项 5；2026-08-27 人类手写单 C-8 升格为契约条目）

另（人类手写单要求）：每条在 contracts.md 中按「约定 / 反例 / 验证方式」三行成文，反例写最容易犯的那个错。

---

## 6. 待拍板

**T-01 · Task-0 的范围**（2026-08-27，由人类手写 Task-0 单与看板任务卡冲突引发）：
- **选项 A（编排建议）**：维持看板全量范围 —— 契约冻结 + skills 框架三件（contract/registry/invoker）+ flows/ 四场景迁移 + store.py +knowledge 表 + worker/base 改造 + run.py/main.py 分发器。理由：这些文件是 A/C/D 的开工前置（C 的独占清单里 `flows/scenario_1/2.py`、A 的 invoker 链路、D 的 knowledge 沉淀都依赖它们），收窄后五轨开工即撞缺失文件。
- **选项 B**：按手写单收窄为「纯契约冻结」（contracts.md + 注册自动发现 + gitignore + 一条测试）。若选 B，必须同时拍板：skills 框架、flows 迁移、knowledge 表**另归谁做、何时做**，否则 G2 过闸即放五轨进场撞墙。
- 未拍板前 Task-0 不派单。
- 2026-08-27 13:12 进展：人类改走自有框架文件路线（`~/maos-dispatch/task-0.md`，编排只做展开/核对/出 diff）。范围之争细化为差异清单 **D-1..D-12**（见 §7 日志 13:12 行与编排会话输出）；人类逐条裁决完毕即视为 T-01 了结。
- 2026-08-27 13:44 **T-01 关闭**：人类定稿指令裁决——D-1~D-6 扩入白名单（只做骨架不做实现）、D-7=契约收 C-1~C-8+附录 A/B/C/D、D-8=docs/parallel/、D-9/D-10=python3+maos/ 实测口径、D-11 保留、D-12 回执模板内联派单 §8。

---

## 7. 派单与验收日志

| 时间 | 轨 | 动作 | 备注 |
|---|---|---|---|
| 2026-08-27 上午 | — | 看板创建；G0/G1/G3 过闸；环境探针落盘 | 五条已完成项已标进 REVIEW.md 状态标记段 |
| 2026-08-27 上午 | Task-0 | 派单草案已产出，等人类确认 | 见编排会话输出；确认后转 DISPATCHED |
| 2026-08-27 09:58 | — | 编排接手（maos-da）：复跑探针 hook ✅ / docker ❌（G3 回退）；点名重复编排会话 `670fc38d`（同提示词、零产出，待人类关闭）与主检出活跃会话 `c752217d`（合并清理/守卫放行，Task-0 开工前须先收口） | 前任 v1 派单未存档，作废 |
| 2026-08-27 09:58 | Task-0 | 派单 v2 存档于 §8，等人类确认 | 确认后转 DISPATCHED |
| 2026-08-27 10:05 | Task-0 | 收到人类手写 Task-0 单（非 plan 原文，疑为前任 v1 底稿/手拟；其「§4 回执模板」为悬空引用）。未执行（铁律 1：总管不改业务代码）。问题：白名单缺 skills 框架/flows 迁移/store+knowledge 等 A/C/D 前置；`python` 本机不存在、`from agents` 应为 `from maos.agents`、根级 main.py/tests/ 不存在；REVIEW 标记段已完成不可重做。吸收其质量要求升级派单 v2→v2.1（八条契约、三行格式、逐位类型、fixture 路径、check-ignore 验收）；范围之争立 T-01 | 等拍板 |
| 2026-08-27 13:12 | Task-0 | 人类指派三件事于 `~/maos-dispatch/task-0.md`；实测该文件全机不存在（目录缺失、Spotlight/find 无命中、近 4h transcript 仅本会话提及）→ 按人类 08-27 手贴原文逐字重建底稿（基线存 scratchpad/task-0.orig.md）。完成：①交付物一按 REVIEW.md 逐条展开（12 条，含行号出处与 C-x 对照）②白名单删 REVIEW.md（归编排）③差异清单 D-1..D-12 已报（乙有甲无 7 条 / 口径差 3 条 / 良性+悬空 2 条），按指示未改文件。diff 已交人类 | 另：plan:102 证实「六段 prompt 在对话正文」——该单出处系 plan 生成对话，解释了 §4 模板悬空与根路径口径。仓库另见 settings.json+.gitignore(+settings.local.json) 已 staged 未 commit |
| 2026-08-27 13:44-14:08 | Task-0 | 人类定稿 → 出交接包：task-0.md 重写为自包含定稿（回执模板内联；期望值写死：基线 83、AGENT_POOL==['coding']、check-ignore exit=1、场景比对按状态迁移序列而非逐字节）；新著冻结契约（C-1~C-8 三行格式 + 附录 A 骨架签名 13 条 + B 六 skill IO + C 场景 2 口径 + D 协作纪律。关键堵洞：SkillInvoked/ToolInvoked 全仓 grep 零命中 → 契约明写落 store.event_log 行、不新增总线事件类型）；handoff-snapshot.md 出具 | 类型/行号均实读源码核实（main/base/eventbus/worker/gate/client/coding/manager/sandbox/run + phase-1/3） |
| 2026-08-27 14:08 | Task-0 | 人类四处补丁完成：①三件套落盘确认 ②cp 守卫合法性（只读探针实测 docs/parallel/contracts.md 不在守卫面，§2 写明 + 人类终端 fallback）③红线补 `~/maos-dispatch/**` 禁改 ④契约附录 D 补两条冻结声明（main.py 完工后冻结、C 只动 scenario_1/2；scenario_3/4 无人再碰）。**转 DISPATCHED，编排静默期开始** | 下一动作：人类开零上下文会话执行 task-0.md |
| 2026-08-27 09:14 | Task-0 | commit `2afa5ef` `docs: 入库 parallel-build-plan 只读架构审计（基线 f104161）` | 补记 |
| 2026-08-27 13:08 | Task-0 | commit `3a36f37` `feat(p0): 守卫配置入库，PreToolUse hook 随仓库分发` | 补记；解掉 §3 「settings.json untracked」那条 ⚠️，worktree 内不再缺 deny+hook |
| 2026-08-27 16:02 | Task-0 | commit `e9b0e2f` `feat(p0): 冻结契约入库，Skill/Tool/Artifact 骨架与场景分层落地` | 补记；contracts.md 落 `docs/parallel/`（非仓库根，§8 存档口径作废）、新建 `maos/artifacts.py`、`store.py` +knowledge 表 |
| 2026-08-27 16:12 | Task-0 | commit `e08cd49` `docs(p0): 契约并稿，收窄轨正文增量并入八条，C-8 验证行去 -v` | 补记；`5c70140` 的正文增量逐条并入，**该执行线自此作废**，附录 A/B/C/D 未动 |
| 2026-08-27 16:17 | Task-0 | commit `9190fde` `test(p0): 补 C-2 import 顺序回归闸与 C-1 私有模块把守，95->97` | 补记 |
| 2026-08-27 17:00 | Task-0 | commit `0d0ccfe` `fix(p0): G2 收口四处缺口 —— compensation mode 锁死 reverse + 三条冻结闸` | 补记；**Task-0 → MERGED，G2 过闸**，pytest 100 passed。旧分支 `task/0-contracts` 已删、`.claude/worktrees/task-0` 已清 |
| 2026-08-27 17:35 | — | 保护面收口 + 三份文档对齐 `0d0ccfe`：`docs/parallel/contracts.md`、`maos/artifacts.py` 进 `PROT_PATHS`+`READ_OK`（写拦读放行，新增一条把守测试）；CLAUDE.md 必须问四类第 1 条扩为同样四条路径；relock 补进 `knowledge` 表指纹（5→6）；REVIEW.md「状态标记」20 行下线，改为一行指向本看板 | pytest **101 passed**（100 存量 + 1 新增）。守卫盲区复验：`-c` 内联载荷 exit=2，已拦，非盲区 |
| 2026-08-27 20:49 | — | §3 环境探针数字校正：pytest 由「100 存量 + 1 新增」推算值改为 **101 passed** 实测（构成 `test_guard_bash` 72→73，四项和 100→101 对齐）；HEAD `0d0ccfe` + 14 commits → `fe6cfff` + 15 commits（`git rev-list --count f104161..fe6cfff` 实测）；「与 origin 同步」补 fetch 实测背书；`:6` 更新戳同步 | **由非编排总管会话执行，人类当场授权**（本文件第 3 行的单写者规则）。`grep -n '0d0ccfe'` 复验：§3 内 0 处，全文剩 5 处（`:18` `:44` `:67` `:167` `:168`）均为过程记录，按「结论性断言改、过程记录留」口径原样保留 |
| 2026-08-28 05:12 | Task-A | commit `f9bcd50` `merge(p1): track-a 并入主干 —— skill 层 + 五轨 fix 全量合流` + `59196ba` `docs(p1): DECISIONS 记取舍` → **Task-A MERGED**；合并后实测 pytest **134 passed**、`run.py` 四场景 exit=0、`maos/contracts/**` 与 `.contracts.lock` 零改动 | 补记。同会话另做收尾：删 6 个空转 worktree、`review-brief.md` 从 `docs/` 归位 `review/`，主工作树已干净。执行者非编排总管会话 |
| 2026-08-28 | — | 编排接手（本会话，从 `~` 启动、hook 未挂载）：复跑探针 pytest **134** / `run.py` exit=0 / `docker info` **exit=0（G3 过闸）** / `contracts/` 零改动；核出两条阶段性断言将被 C、E 落地打红 → 立 **D-04** 裁决 | 四项探针均为本会话当场实测，非转述 |
| 2026-08-28 | B/C/E/D | **四轨同时派单，转 DISPATCHED**：建 4 个 worktree（`.worktrees/task-b\|c\|e\|d`，均基线 `59196ba`），派单存档 `review/dispatch-common-p2.md` + `dispatch-{B,C,E,D}.md`，粘贴版 `paste-{B,C,E,D}.md`（见 §8） | 顺带清理：`.worktrees/task-b` 原挂 `track-b`@`fe6cfff`，实测 `git merge-base --is-ancestor track-b goai-restructure` 成立、工作区无未入库产物，确认无遗留工作后移除重建。`review/` 由 `.git/info/exclude` 排除、不入库 → 新 worktree 里看不到派单文件，**靠粘贴交付**，与上一轮五轨同一手法 |
| 2026-08-28 15:52–15:58 | R-1 / R-3 | 编排总管会话完成**试合验证**：R-1 合上主干 → 271 passed / exit=0，再叠 R-3 → 301 passed / exit=0，两次均只有 `docs/BACKLOG.md` + `docs/DECISIONS.md` 尾部冲突、**代码零冲突**；并核出 R-1 单跑的红继承自其基线 `b2319df`。随后写出 R-0 / R-2 / Ω 三份派单，提出「合入主干 + 重建三个 worktree + 刷看板」方案等人类拍板 | 探针 worktree 用后已清理。派单文件当场落盘 `review/`，未重蹈「只在会话输出里」的覆辙 |
| 2026-08-28 | R-1 / R-3 | **合入主干**：`c5965d4`（R-1 → 271 passed）、`f63de8b`（R-3 → 301 passed），`run.py` 两次均 exit=0；冲突按「两侧都留、HEAD 在前」解，两份 docs 的 `## task-R1` / `## task-R3` 小节完整保留。三个 R 轮 worktree 用 `git reset --hard f63de8b` **平移**（三分支各 0 commit、工作区干净，故不删目录重建 —— 删目录会踩掉可能已开在里面的窗口）；`review/paste-{R0,R2,omega}.md` + `dispatch-common-p3.md` 抬头基线 `90251b3`→`f63de8b`、257→301；本文件 §0/§2(G5)/§3/§4/§7/§8 刷新 | 由**非编排总管会话**执行、人类当场授权（同 08-27 20:49 口径）。合并前打 tag **`pre-r1r3-merge`@`90251b3`** 作回滚锚点。三项验收（301 passed / exit=0 / docker 镜像 247MB 在）与 `task-r2` worktree 内的复跑均为本会话当场实测，非转述；冻结面 `maos/contracts/`+`.contracts.lock`+`artifacts.py`+`contracts.md` 实测零改动 |
| 2026-08-28 16:30 | R-0 / R-2 / Ω | **三轨状态核实 + R-0 派单修订**。核实：R-2、Ω 两个会话**早已开工**（各自 worktree 内已有未入库产出），看板此前三行 `NOT_STARTED` 全错；两者粘的都是 16:12 刷新前的旧版抬头（`90251b3`/257），均已自证并自行纠正到 `f63de8b`/301，**不重粘**。R-0 则是**只拿到一行「下一步」就开工**的半轨：做完步骤 3/4 后已收口提交 `cc0495b`（`pytest` **318 passed**、`run.py` exit=0、工作树干净），步骤 1/2/5 未做，**待按修订版重粘**。修订 `review/dispatch-R0.md`（94→155 行）并重拼 `paste-R0.md`：①删脚手架范围写错（原「三处调用点/全仓零残留」实为 **5 处跨 4 场景 + 1 测试**，其中 `scenario_3/5`、`test_matrix_bus` 本轮**无人拥有**，照原版必违反边界第 1 条）②改按判据划线：删 `testing.py::_report_from` 的 `scripted_report` 假绿回落、**留** `seed_scripted_report()` 本体 ③新增 `4bis 不许回退`（`fix-2`@`353a8a2`、`fix-3`@`db20e6d` **早已在主干**、无需「并进来」，风险是重写时静默改回，三处各钉一条回归断言） | **由非编排总管会话执行、人类当场授权**（「你代刷吧」，沿用 08-27 20:49 口径）。三轨 worktree 状态、`cc0495b` 的 318 passed / exit=0、`fix-2`/`fix-3` 的 `merge-base --is-ancestor` 归属、5 处 `seed_scripted_report` 调用点行号，**均为本会话当场实测/实读源码，非转述**。代码零改动，只动 `review/**` 与本文件 |
| 2026-08-28 16:35 | R-1 / R-3 | **收尾清理**：`.worktrees/task-r1`@`946befd`、`.worktrees/task-r3`@`3a2e473` 两个已合并 worktree 移除，分支 `task/r1-refund-core`、`task/r3-gateway` 用 `git branch -d`（安全删，未合并会拒）删除。删前三项复核：两者 `git status --porcelain` 均空、`git rev-list --count HEAD..<各自 HEAD>` 均为 0（无独有提交）、`~/.claude/projects/` 下无对应 worktree 项目目录（没有会话开在里面）。回滚锚点 tag `pre-r1r3-merge`@`90251b3` **保留不删** | 由非编排总管会话执行，人类当场指派（「你先清掉，我已经派单了」）。`.worktrees/` 剩 `integrate`/`review-ab`/`review-c`/`review-registry`/`task-r0`/`task-r2`/`task-omega` 七个 |
| 2026-08-28 16:45–17:05 | R-0 / R-2 / Ω | **三轨整合（`integrate/round-2`，基线 `93529b4`）**。合并顺位 R-0→R-2→Ω，代码面**零重叠**，两份账本追加冲突按「两侧都留、先到者在前」解。整合暴露**三处接缝**（各轨自测全绿、合并才现）：①R-2 的 `test_kernel_does_not_know_the_refund_domain` 按子串扫 `"domain.refund"`，把第六道闸 docstring 里「不许 import」那句自我说明判成违例 —— **假阳性**，改走 AST 认 import 语句，扫描范围一行未缩；②`test_agent_pool_is_exactly_five_roles` 断「恰好五角色」，R-2 投放四个退款 Agent 后必红 —— 按 R-2 BACKLOG 建议改**子集口径**并改名 `..._contains_the_five_kernel_roles`；③**唯一真缺陷**：`verify.py` 第 3 项 authoritative-fact FAIL，`payment_observation.actor_invocation_id` 与 `SkillInvoked` 事件的 id 不同一 —— R-2 精确预告过但 `invoker.py` 不在其边界内、Ω 只写核验器不改被验对象，**两轨都修不了**，按 R-2 给的一行修法改 `invoker.py`（`extras={**extras, "invocation_id": invocation_id}`），该项转 1/1 PASS。快进合入主干 `92456ba` | 由**非编排总管会话**执行、人类当场授权（「现在你要开始整合」）。回滚锚点 tag **`pre-r0r2omega-merge`@`93529b4`**。三项验收均**主线上实测**：**383 passed**、场景 1-6 逐个 `exit=0`、`verify.py` **5/5 PASS + 2 SKIP** `exit=0`；冻结面 `contracts/`+`store.py`+`artifacts.py`+`.contracts.lock` 实测零改动。⚠️ 本会话 cwd 不在仓库根，**项目级守卫 hook 未加载**（已知项），全程以显式跑 `test_contracts_frozen.py` + 冻结面 diff 替代 |
| 2026-08-28 17:05–17:07 | 收尾 | **三件**：①人类授权**解冻 `maos/main.py`** 补演示缺口 —— `DEFAULT_SCENARIOS` 此前是 `(1,2,3,4)` 且注释还写着「场景 5 未实现」，`run.py` 无参跑不到场景 5/6，**演示只跑 `run.py` 会整个漏掉退款域链路且不报错**；改为 `(1..6)` 并同步 docstring / argparse help / `run.py` / `CLAUDE.md:75` 四处措辞，排除标准写成「模块不存在」而非「谁负责」，故场景 7 一落地自动进（`9e5fd52`）。②证据按改后主线**重跑**，出处 `sha=9e5fd52`（`01824f2`）。③`task-r0` worktree 与分支清理；**W-1…W-7 七个新轨**（16:58–16:59 建，各 0 commit、工作区干净、无会话开在里面）用 `git merge --ff-only` **平移**至 `01824f2`，与主线同基线 | 平移用 ff-only 而非 `reset --hard`：有任何独有提交它会拒绝而不是丢弃。R-2 / Ω 两个 worktree **刻意留着** —— 17:06 复核时其会话 6 分钟前还在活动，删目录会踩掉正开着的窗口（同 08-28 16:35 口径）|
| 2026-08-28 18:39–18:49 | W-1/W-2/W-3/W-6/W-7 | **整合轮 3（`integrate/round-3`，基线 `01bc8d8`）**：五轨按 **W-1 → W-2 → W-6 → W-7 → W-3** 顺序合入，合并提交 `823dc54` / `cd21681` / `e710262` / `ec63983` / `fa57c97`；随后 `fd3f7cc` 按 `fa57c97` 重跑证据束（补齐 `scenario-7`、新增 `scenario-R5`，`verify` 转 7/7）。冲突面：**W-1 无冲突**，另四轨的合并提交 `# Conflicts:` 段一律只有 `docs/BACKLOG.md` + `docs/DECISIONS.md`，**代码零冲突**、也无整合轮 2 那种接缝修复提交。`integrate/round-3` 与 `goai-restructure` 现同为 **`fd3f7cc`**（`ahead 48`，仍未 push） | **整合由另一并行会话执行**（本会话只做记录与复核，代码零改动）。以下均为本会话**当场实跑**、非转述：五个尖端 `git merge-base --is-ancestor <tip> HEAD` **全部成立**；主干 `fd3f7cc` 上 `python3 -m pytest maos/tests -q` → **455 passed**、`python3 run.py` → **exit=0**；`git diff --stat 01bc8d8 HEAD -- maos/contracts/ .contracts.lock maos/artifacts.py docs/parallel/contracts.md maos/main.py` → **空**（冻结面零改动）。⚠️ `python3 scripts/verify.py` 在**主检出**上 **exit=2**（`缺 evidence/scenario-7/maos.db`）—— 不是回归：`*.db` 按设计不入库（`.gitignore:40`，`make_evidence.py:456` 把库落在临时目录再整体 `os.replace` 挪位），主检出上次自跑 `make_evidence.py` 停在 17:05，故只有老的 1–6 有库、新的 7 / R5 没有；在 `.worktrees/integrate` 内同一 sha 复跑 **7/7 PASS、exit=0**。**要在主检出复现那一屏，必须先 `python3 scripts/make_evidence.py`** —— 演示前务必先跑这一步。⚠️ 本会话从 `~` 启动，项目级守卫 hook 未加载（已知形态） |
| 2026-08-29 11:11–11:26 | X-1/X-2/X-3/X-4/W-5 | **整合轮 4（`integrate/round-4`，基线 `4a70cb0`）**：五轨按 X-1 → X-2 → X-3 → X-4 → W-5 顺序合入，合并提交 `f3e48b5` / `f602898` / `ada3885` / `c0fced8` / `df96fa8`；随后 `8c2d598` 按 `df96fa8` 重跑证据束（business-ref 23→33、kb-hit 1→4，`verify` 仍 **7/7 PASS**）、`002e4af` 刷九份文档的过期事实。冲突面：仅 `docs/BACKLOG.md` + `docs/DECISIONS.md`，第 2..5 轨各冲 1 块（both-added-at-end），**代码零冲突**、无整合轮 2 那种接缝修复提交。主干 `git merge --ff-only integrate/round-4` 快进到 `002e4af`，其上另有 `42822fc`（移除 `CLAUDE.md` 的「回答结尾规范」一节）| **整合与合入均由另一并行会话执行**，本会话只做记录与复核，代码零改动。以下均为本会话**当场实跑**、非转述：五个尖端 `git merge-base --is-ancestor <tip> HEAD` **全部成立**；主干 `42822fc` 上 **521 passed / `run.py` exit=0 / 工作区 0 行**；冻结面四条路径在 `4a70cb0..002e4af` 的变更清单里**零命中**。⚠️ **本轮未打回滚 tag** —— 前三轮都打了（`pre-r1r3-merge` / `pre-r0r2omega-merge` / `pre-w-merge`），本轮漏了，回滚锚点目前只剩 reflog `HEAD@{2}`=`4a70cb0` |
| 2026-08-29 11:2x | Y-1…Y-4 | **四轨 worktree 重建 + 派单基线刷新**（`df96fa8` → `42822fc`），由另一并行会话执行。**代价：Y-1 的在制品被冲掉** —— 重建前 Y-1 有两处未提交改动（`agents/testing.py` 三个透传点 + `flows/common.py` 两处调用点，当时 521 passed），重建后 `git stash` 空、`task/y1-exec-path` 0 独有提交、新工作区干净，**无处可捞**，只能按刷新后的派单重做 | 本会话实测：四个 worktree 现均 `42822fc`、`git status --porcelain` 均 0 行；四份 `review/paste-Y*.md` 抬头基线均已是 `42822fc`。**教训入账**：重建（而非 `merge --ff-only` 平移）worktree 前，必须先查该目录 `git status --porcelain` 是否为空；非空则先 `git stash` 或提交到本轨分支再动 —— 08-28 17:05 那条「平移用 ff-only 而非 `reset --hard`，有独有提交它会拒绝而不是丢弃」防的是已提交的，防不住未提交的 |
| 2026-08-29 11:42 | 看板 | **本次刷看板**：§0 补 X-1…X-4 四行、Y-1…Y-4 四行、Z 轮一行；修 **W-5**（`NOT_STARTED` → **MERGED**@`df96fa8`，08-28 记错）与 **W-4**（落后 2 → **18** 个提交、worktree 已不存在）；§3 加 `42822fc` 实测一条；§7 加本三行。另修 `CLAUDE.md:59`「场景 1-6 端到端」→ **1-7**（`maos/main.py:29` 实为 `DEFAULT_SCENARIOS = (1,…,7)`，而四个子会话开工都会自动加载这句，照它复核 `run.py` 会把场景 7 当异常）| 由**非编排总管会话**执行、人类当场授权（「按你的建议来办」，并就撞车嫌疑点名确认后才动手，沿用 08-27 20:49 口径）。本会话从 `~` 启动，守卫 hook 未加载，故只动 `docs/ops/ORCHESTRATION.md` 与 `CLAUDE.md` 一行，**业务代码零改动** |
| 2026-08-29 11:4x–11:5x | Z-1…Z-5 | **Z 轮五轨派单**（材料面：PPT 大纲 / Demo 分镜 / 可移植性换端点 / 自查单重排 / 新克隆冒烟），worktree 全部基线 `42822fc`，派单存档 `review/paste-Z{1..5}.md`。编排侧建轨前在 `task-z1`/`task-z5` 内**当场实跑**取期望值写进派单：521 passed / `run.py` exit=0 4.1s / `gen_docs --check` exit=0 / 证据链 4.5s / `verify` 7/7 + 4 warn。两处**官方口径不在仓库里**（评审四维名称与权重、Demo 视频规格）在五份派单里一律写死红线「一个字不许编」，收口进 `docs/open-questions.md` OQ-1/OQ-2 | 编排侧另在派单里为每轨钉死「Y 轮易变点」+ 要求产出 `## 待整合轮 5 回填` 清单 —— 因 Y 轮四轨与 Z 轮同时在跑，Z 轮写的一部分说法会被 Y 轮推翻。事后看这一步是本轮回填能逐条对账的前提 |
| 2026-08-29 12:0x–12:2x | Y-1/Y-2/Y-3 + Z-1…Z-5 | **整合轮 5（`integrate/round-5`，基线 `f42ea83`）合入 8 轨**：顺序 Y-1 → Y-2 → Y-3 → Z-3 → Z-1 → Z-2 → Z-4 → Z-5，合并提交 `01f8ab7` / `988513b` / `934df18` / `0fa0d72` / `43d1c20` / `6ab01f4` / `f50aac7` / `8f47ce5`。冲突面：**仅 `docs/BACKLOG.md` + `docs/DECISIONS.md` 尾部**，共 16 块 both-added-at-end，按「两侧都留、HEAD 在前」解（写了个只认「两侧都以 `## ` 开头」才自动解的小脚本，不满足就拒绝改人工看）；**代码零冲突**。随后 `706bd52` 重生成 `agent-identity.md`、`33924d1`+`5ea6890`+`fb8a10e`+`f853063` 按各轨回填清单刷五份材料文档 | 逐步验收均**当场实跑**：合 Y-1 后 537 passed、合 Y-2 后 548、合 Y-3 后 **571**。两处**实测推翻转发结论**：①一度误判 provenance warn 从 1 涨到 6 是回归 —— 复跑基线证伪，是自己 `tail` 截断看漏，基线本就 6 条；②Z-4 回填清单断言「Y-3 收敛后 `-dirty` 缺口自动消失、整节可删」**不成立**，全新克隆实测 R5 的 7 个文件仍带 `-dirty`，该节改写而非删除 |
| 2026-08-29 12:2x–12:4x | Y-4 | **补合 Y-4 进整合轮 5**（`783d9dd`），随后 `9964f17` 按 `caf45d2` 重跑证据束、`5c3d21b` 刷 README 数字、`147df03` 记 BACKLOG 三条。合入前把那处越权改动（`payment_execute.py` 的 DELETE 悬空 `business_ref`）完整 diff 摆给人类，**当面授权后才合**。合并后 596 passed / replan 19 条一条没少 / `verify` 7/7 exit=0 | **由并行会话 `integrate5-f3` 执行**（派单 INT-5b）。⚠️ **本轮两个会话一度同时写同一个工作树** —— 编排侧（本会话）在 `.worktrees/integrate5` 里做回填的同时，`integrate5-f3` 正在同一目录补合 Y-4，编排侧写的文件被它 commit（它一度误判成有别的会话在实时写）。**教训入账**：整合轮的工作树同一时间只许一个会话写；派 INT-5b 这类补单前，先 `ListAgents` 确认原整合会话已退出。本会话发现后即退出该工作区并发消息交接，无内容丢失 |
| 2026-08-29 12:5x | 看板 + 材料 | **整合轮 5 第二批回填 + 看板收口**（`f4b2ada` / `956e6af`）。四份材料文档按合入 Y-4 后实测刷：warn **10 行 2 类 → 11 行 3 类**、`pytest` 571 → **596**、`hash-integrity` 77 → **81**、可移植性区间 B 的 `agents`/`skills`/`flows` 三行重算。`demo-script` 镜 5 的 A/B 两版合并成一版（`[2]`/`[3]` 两段输出与状态迁移轨迹照实跑粘贴），念词 139 → **190 字**、分配 40 → **55s**，镜 6/7/8 各顺延 15s，总长 4:25 → **4:40**。另把自查单 **D-4 ① 从「勾不上」改成「可勾」** —— 原判据「`INDEX.json` 的 `git_sha` == HEAD」是**不动点问题**（写进证据束的 sha 必是重跑那刻的 HEAD，commit 它又产生新 HEAD），改判「证据束记录的 sha 到 HEAD 之间有没有动过代码」，实测 `caf45d2..HEAD` 4 个提交、代码差异 **0** | 🔴 **本轮最该记住的一条**：上一批回填在 A-2 立的判据是「就是这 **10 行 2 类**，多出来的才要查」，Y-4 合入后 warn 变 11 行 3 类，**照旧判据执行会把一条预期内的 warn 判成回归**。教训：把「当前实测值」写成判据时，必须同时写清**它随哪一轨变**，否则判据本身会变成假警报源。另：Z-4 回填清单断言「Y-3 收敛成一条命令后 `-dirty` 缺口自动消失」**经全新克隆实测证伪**，该节改写而非删除 |
| 2026-08-29 13:0x | 收尾 | **`integrate/round-5` 并回主干 + 清六个 worktree**。人类当场授权：先打回滚 tag **`pre-round5-merge`@`f42ea83`**（补上整合轮 4 漏打那笔），再 `git merge --ff-only integrate/round-5` 快进到 **`956e6af`**（31 个提交，纯 FF）。主干实测：**596 passed** / `run.py` exit=0 / `gen_docs --check` exit=0 / 工作区 0 行 / `git status -sb` → **`ahead 94`，仍未 push**。随后清理 `task-w5`、`task-x1..x4`、`integrate`（round-4）六个 worktree 与对应分支（`git worktree remove` + `git branch -d` 安全删） | 清理前逐个核三项：`git status --porcelain` 全 0、`git rev-list --count goai-restructure..<branch>` 全 0、`~/.claude/projects/` 下对应目录**最后活动 11:12–11:41**（约 1.5h 前）且 `ListAgents` 里已无对应会话。⚠️ **Y/Z 九个 worktree 刻意留着** —— 九个会话 `ListAgents` 显示仍 alive（idle），删目录会踩掉正开着的窗口（同 08-28 17:05 口径）。⚠️ **`.worktrees/task-c1..c4` + `task-d1..d2` 六个 12:55 由另一会话新建，一个都没碰**，见 §0 末行 |

---

## 8. 派单存档

> 本段是派单**原文存档**，内含的「83 基线」「contracts.md 在仓库根」等数字与口径属当时快照，**不再更新**；现状一律看 §0 / §2 / §3。

### R 轮派单（2026-08-28 · R-0 / R-2 / Ω，三轨同时开工）

正文同样存于 `review/`（`.git/info/exclude` 排除、**不入库**，新建 worktree 里看不到，靠粘贴交付）：

| 文件 | 内容 |
|---|---|
| `review/dispatch-common-p3.md` | 三轨共用抬头：基线 `f63de8b` / 301 passed / docker 镜像在；v4 手册 `docs/EXECUTION.md` 为准（⚠️ `docs/phases/phase-<N>.md` 是 **v3 编号**，与本轮对不上、不要看）；D-05 已由 R-1 落地故 `main.py` **重新冻结、本轮谁都不许碰**；守卫两条；回执格式 |
| `review/dispatch-R0.md` | 软件域封版：演示链路真连沙箱 + Gate 收口 |
| `review/dispatch-R2.md` | 退款域 6 Skill + 4 Agent + 场景 6 |
| `review/dispatch-omega.md` | Trace + Evidence Bundle + `verify.py` 七项核验 + compose 部署 |
| `review/paste-{R0,R2,omega}.md` | 共用抬头 + 各轨正文的**粘贴版**（开子会话时整份贴进去） |

- 合并顺位：**R-0 → R-2 → Ω**；R-1 / R-3 已先行 MERGED（见 §0、§4）。
- ⚠️ 与本段开头「存档不更新」的口径有**两处有意的例外**，理由同一条：**未粘出去的派单，粘的那一刻必须是对的**；已粘出去的 P2 存档一概不动。
  1. 2026-08-28 16:12 四份抬头的基线由 `90251b3` / 257 passed 刷成 `f63de8b` / 301 passed。**未完全生效** —— R-2 / Ω 在刷新前就已粘出旧版（两者均已自行纠正，见 §0）。
  2. 2026-08-28 16:30 `dispatch-R0.md` 正文修订（94→155 行）+ `paste-R0.md` 重拼：加「基线覆盖」（R-0 的基线是 `cc0495b` / **318 passed**，不是 `f63de8b` / 301）、订正删脚手架范围、加 `4bis 不许回退`。详见 §4 的 R-0 卡与 §7 16:30 行。

### P2 四轨派单（2026-08-28 · Task-B / C / E / D，四轨同时开工）

正文不内联在此 —— 存于 `review/`（该目录由 `.git/info/exclude` 排除，**不入库**，故新建的 worktree 里看不到，靠粘贴交付）：

| 文件 | 内容 |
|---|---|
| `review/dispatch-common-p2.md` | 四轨共用抬头：基线 `59196ba` / 134 passed / `docker info` exit=0、事实源优先级、边界七条、docs 尾部追加规则、守卫两条、**D-04 裁决表**、回执格式 |
| `review/dispatch-B.md` | 容器沙箱 2 ToolPort + 演示靶场 + `test.verify` |
| `review/dispatch-C.md` | 四 Agent + Gate 判据改读真实测试报告 + 补偿干跑闸 + 场景 1/2 新 DAG |
| `review/dispatch-E.md` | Matrix 镜像总线 + 房间审批 + `.env.example` |
| `review/dispatch-D.md` | 聚合/知识 skill + 补偿执行器 + 确定性 replan + 场景 5 |
| `review/paste-{B,C,E,D}.md` | 共用抬头 + 各轨正文的**粘贴版**（开子会话时整份贴进去） |

- 合并顺序：D-03 去掉已 MERGED 的 A → **B → C → E → D → Ω**。
- 文件所有权互斥性已核（contracts.md 附录 D 速查表）：sandbox.py→B；gate.py + scenario_1/2→C；hiclaw/** + 根 .env.example→E；control_plane.py + plan_finalizer + scenario_5→D。四轨独占清单无交集。
- 唯二的共享面：①`docs/DECISIONS.md` / `docs/BACKLOG.md` —— 强制尾部另起 `## task-<X>` 小节，纯追加，git 自动解；②`maos/tests/test_registry_autodiscovery.py` 的两条阶段性断言 —— 按 D-04 点名开口，C 改 `:170`、E 改 `:257`，相距 87 行。

### Task-0 派单 v2.1（2026-08-27 · 等 T-01 拍板 + 确认；v2 作废）

v2 → v2.1 增量：吸收人类手写单的「八条契约（+C-8 gitignore）、每条约定/反例/验证方式三行、C-4 六元组逐位类型、C-5 fixture 路径、test_registry_autodiscovery.py 命名、check-ignore 验收、C-7 的 D 验收拆两段」；修正其 `python`/根路径口径；contracts.md 定仓库根；范围维持看板全量（T-01 若拍板收窄则另出 v3）。

```markdown
# 派单 Task-0 —— 冻结契约 + 入口分发器（串行首位 · v2.1 · 2026-08-27）

你在主分支上工作，你合并之后其余五轨才会开。你的产出一旦冻结，五个并行会话都会照抄——写错的代价是五份返工。

## 0. 开场自检（必须先做，不过不许动任何文件）
- `pwd && git branch --show-current` → 必须是 /Users/shensikai/Documents/MAOS 与 goai-restructure。
- 本会话必须是从仓库根启动的。验证：用 Read 工具读 scripts/guard_bash.py ——
  **被拦（blocked: 该操作触碰受保护面）= 守卫正常，继续**；能读到内容 = 守卫没挂上，停下报告人类，什么都别改。
- `python3 -m pytest maos/tests -q` → 必须全绿（09:57 基线 83 passed；总数略有出入但全绿则记录数字继续；有 fail 立即停下报告）。
- `git status --short` 记下开工前已存在的脏文件（REVIEW.md、docs/ops/、.claude/ 可能未提交，**都不是你的文件，禁止 add/commit 它们**）。

## 1. 身份与红线
- 你是 Task-0 子会话，只做本派单列出的事。工作在主检出，不开 worktree、不建分支。
- 本机没有 `python` 命令，一律 `python3`。
- **禁止 push**；只本地 commit，格式 `feat(p0): <一句话>`；`git add` 逐文件点名（白名单内），禁止 `git add -A` / `git add .` / `git commit -a`。
- **禁改**：maos/contracts/events.py、maos/contracts/states.py、scripts/guard_bash.py、.claude/**、.contracts.lock（守卫会拦；被拦不是让你绕，是让你停）。
- maos/core/store.py 现有五表（plan/task/artifact/event_log/processed_key）DDL 一字不动，只允许**新增** knowledge 表。
- 要动白名单外的文件、或发现契约冲突需要拍板 → 立刻停下，回执写 BLOCKED + 两个候选方案。不许自选一个继续。
- 手册没覆盖而你自行判断之处，追加 docs/DECISIONS.md 一行：`<日期> | Phase 0 | 情境 | 选择 | 理由`。

## 2. 任务定义原始出处（都在你的工作目录里，开工先读）
- docs/superpowers/plans/parallel-build-plan.md：§一（Task-0 文件清单）、§二（16 条契约规格，contracts.md 的底本）、§三（Task-0 验收）。
- REVIEW.md 顶部「⚡ 状态标记」段：五条已完成项**不要重做**；正文其余是 08-26 审计快照，结论以标记段为准。
- REVIEW.md 里归 Task-0 的修正项已全部折进本单 §4/§6，照单执行即可；不要自行解读 REVIEW 扩大或缩小范围，也**不要再动 REVIEW.md**（状态标记段已由编排会话完成）。

## 3. 已完成、勿重做（截至 2026-08-27 09:57 实测）
- .contracts.lock 已入库；三重守卫（deny+hook+指纹）3/3 生效；存量测试 83 绿。
- maos/tools/sandbox.py 桩已存在（commit 9d3fe4d），两签名：
      sandbox_git_apply(patch_set, workdir, *, reverse=False, check_only=False)
      sandbox_pytest_run(workdir)
  你只核对并写进 contracts.md、声明所有权移交 Task-B。**不要重建、不要填实现。**

## 4. 文件白名单（只许动这些）
新建：
- maos/skills/contract.py、maos/skills/registry.py、maos/skills/invoker.py
- maos/skills/builtin/__init__.py —— 必须 pkgutil.iter_modules 动态发现，禁止显式 import 清单
- maos/tools/port.py、maos/artifacts.py
- maos/flows/__init__.py（空 docstring，不做注册点）、maos/flows/common.py（承载 build()，D-02 签名）、
  maos/flows/scenario_1..4.py（自 maos/main.py 现行为原样迁移）、maos/flows/scenario_5.py（占位）
- contracts.md（仓库根）
- maos/tests/test_registry_autodiscovery.py（新建：含「新放一个文件进 skills/builtin/ 无需改 __init__ 即被注册」的证明测试）及其它必要新增测试（一律放 maos/tests/ 下，不存在根级 tests/）
修改：
- maos/core/store.py（唯一改动：+knowledge 表）
- maos/agents/base.py（挂 SkillInvoker）
- maos/runtime/worker.py（传 store）
- maos/agents/__init__.py（按 D-01 改 pkgutil 自动发现）
- maos/main.py（改造为入口分发器；删除第 16 行 `import maos.agents.coding` 手动注册）
- run.py（薄入口对接分发器，支持 --scenario N）
- maos/model/client.py（select_model_client 最小实现；文件随后移交 Task-A）
- .gitignore（追加 `!.env.example` 与 `!deploy/.env.example` 两行）
- docs/DECISIONS.md（按需追加）

## 5. 已冻结决策（照此执行，不要重新讨论）
- D-01 AGENT_POOL 注册：agents/__init__.py 用 pkgutil 自动发现并冻结；同时删除 main.py:16 手动注册行。
  理由：worker.py:34 构造时读 AGENT_POOL.items()，注册必须早于 build()；与 skills/builtin/ 同一套 pkgutil 模式。
- D-02 build() 签名冻结：`build(script, *, matrix=False, model=None)`；model=None 时按 script 构造 ScriptedModelClient；
  返回值六元组 `(store, bus, cp, model, worker, gate)` 顺序一并冻结，现有三处解包不许破坏。
  场景 2 迁移：把 main.py:110-116 的 FlakyModel 搬进 scenario_2.py，经 `build(..., model=<FlakyModel 实例>)` 注入，
  禁止在场景文件里手工拼装六件套。
- D-03 合并顺序：Task-0 → B → A → C → E → D → Ω（写进 contracts.md 协作说明，不影响你的实现）。

## 6. contracts.md 必须写死的八条（缺一不合格）
以 plan §二 16 条规格为底本整理成文。**每条按「约定 / 反例 / 验证方式」三行成文**：反例写最容易犯的那个错（下面已给参考），验证方式给可执行命令或测试名。
- C-1 `skills/builtin/__init__.py` pkgutil 动态发现。反例：写成显式 import 清单（A/B/D 三方都要改同一文件，三路冲突）。验证：`maos/tests/test_registry_autodiscovery.py`。
- C-2 AGENT_POOL 注册口径 = D-01：`agents/__init__.py` pkgutil 自动发现，`main.py:16` 删除，放文件即注册，任何任务不得改此文件。反例：各 scenario 顶部 import 注册（已否决：重复且易漂移）。验证：`python3 -c "from maos.agents import AGENT_POOL; print(sorted(AGENT_POOL))"` 非空。
- C-3 build() 入参签名 = D-02，冻结。反例：scenario_2 内联拼装六件套绕过 build()（已否决：留第二条构造路径一定会漂）。
- C-4 build() 返回六元组顺序冻结：**逐位写明变量名与具体类型**（以现行 maos/main.py 返回处的真实类型为准，附 file:line 佐证）。反例：只冻入参不冻返回值，C/D 各自按位置解包必漂。
- C-5 compensation artifact golden fixture：定死 fixture 文件路径（建议 `maos/tests/fixtures/compensation_golden.json`，另择路径需记 DECISIONS.md）与完整内容示例，C（干跑闸）/D（执行器）双方都对它写测试。反例：C/D 各自手搓 artifact，合并时字段对不上。
- C-6 `MatrixEventBus(inner_bus, config)` 的 config 形状：dataclass 或明确键集+类型，**不许留「dict 随便传」**；并写明必须实现 publish/subscribe/drain 三方法（读 hiclaw/ 现状与 docs/phases/phase-3.md 后起草）。
- C-7 sandbox.py 所有权移交 Task-B（两签名照抄本单 §3）。**Task-D 验收拆两段写死**：并行期只验「补偿事件与 patch_ref 正确生成」；合并 B 后验「文件真实还原」（按 D-03，D 合并时 B 必已 MERGED，合并期即可完整验）。
- C-8 `.gitignore` 补 `!.env.example` 与 `!deploy/.env.example`，一次改完冻结（Task-E/Ω 的 .env.example 交付依赖此条）。
其中 C-5/C-6 由你起草，人类验收终审；其余照抄冻结决策与本单，不得改动。

## 7. 验收（全部通过才许 commit；输出原文粘进回执）
    python3 -m pytest maos/tests -q                                   # 存量 + 新增全绿
    python3 -m pytest maos/tests/test_registry_autodiscovery.py -v    # 自动发现证明测试
    python3 -c "from maos.agents import AGENT_POOL; print(sorted(AGENT_POOL))"   # 期望非空，输出原文粘回执
    git check-ignore -v .env.example deploy/.env.example ; echo "exit=$?"        # 期望无匹配输出且 exit=1
    python3 run.py                       # 四场景输出与迁移前一致
    python3 run.py --scenario 3          # 单场景分发正常
（口径提醒：一律 `python3` / `python3 -m pytest`，本机没有 `python` 命令；测试在 `maos/tests/`，包名是 `maos.agents`，不存在根级 `tests/`、`agents/`。）
- 「新放一个文件进 skills/builtin/ 无需改 __init__ 即被注册」证明测试在 test_registry_autodiscovery.py 中且绿。
- contracts.md 八条齐全、每条三行格式完整（按 §6 逐条自检）。

## 8. 回执格式（原样贴回编排会话）
1)【结果】DONE 或 BLOCKED（+两个候选方案）
2)【命令输出】§7 全部六条命令的完整原文输出
3)【变更清单】`git status --short` 与 `git log --oneline -n <你的提交数>`
4)【八条对照】contracts.md C-1..C-8 逐条 ✓/✗ 及所在小节
5)【自行判断处】DECISIONS.md 新增行原文（没有写「无」）
```
