# ORCHESTRATION —— MAOS 并行构建编排看板

维护者：编排总管会话（唯一可写本文件的会话；业务代码一律派单给子会话）。
事实源优先级：`docs/parallel/contracts.md`（Task-0 产出后）> 本看板 > `REVIEW.md` > `docs/BACKLOG.md`。
任务定义原始出处：`docs/superpowers/plans/parallel-build-plan.md`（gitignored 操作剧本，下称 plan）+ REVIEW.md 审计修正。
最后更新：2026-08-28 · **R-1 / R-3 合入主干（`f63de8b`），R 轮三个 worktree 重建到该 sha**。本次动作：§0 状态刷新（B/C/E/D 补记 MERGED、R 轮五行新增）、§2 增 G5（R 轮开工闸，已过）、§3 探针刷到 `f63de8b`（301 passed）、§4 增 R 轮任务卡、§7 补两行、§8 增 R 轮派单存档指针；另刷 `review/paste-{R0,R2,omega}.md` 与 `dispatch-common-p3.md` 抬头里写死的旧基线 `90251b3`→`f63de8b`。执行者为**非编排总管会话**（从 `~` 启动、hook 未挂载），**人类当场授权**——沿用 2026-08-27 20:49 那次的口径（本文件第 3 行的单写者规则）。合并方案由编排总管会话提出并试合验证，本会话按人类指派执行并逐项复跑。上一次实质动作见 §7 「2026-08-28 B/C/E/D 四轨派单」行。
**2026-08-28 16:30 增补**（同为非编排总管会话执行、人类当场授权「你代刷吧」）：核实 R-2 / Ω **早已开工**、R-0 是「只拿到一行下一步」的半轨已收口于 `cc0495b`（318 passed）→ §0 三行 `NOT_STARTED` 全部作废并改写；§3 补三个 worktree 当前实况；§4 三张 R 轮卡改写（R-0 卡记全派单三处修订）；§7 补一行；§8 存档例外由一条扩为两条。同批修订 `review/dispatch-R0.md`（94→155 行）与 `paste-R0.md`。**代码零改动。**
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
