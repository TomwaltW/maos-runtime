# REVIEW —— parallel-build-plan.md 只读架构审计

审计对象：`docs/superpowers/plans/parallel-build-plan.md`（未入库，`.gitignore:3` 已忽略）
审计基线：`goai-restructure` @ `f104161f72239cfab4ff549aa1f3800b50922765`
审计方式：只读。未编辑/新建/删除任何源码文件，未运行任何 git 写操作。唯一产物为本文件。
审计日期：2026-08-26

---

> **状态快照已下线**：本文正文是 2026-08-26 基线 `f104161` 的只读审计快照，结论多已过时。当前状态、门禁与已完成项一律以 `docs/ops/ORCHESTRATION.md` 为准（Task-0 已 MERGED @ `0d0ccfe`，本文件不再维护第三层快照）。

## 0. 结论（3 行以内）

**不能直接开并行。** 三个人肉前置（§六.1–3）一个都没做完，且当前 `pytest` 是 **2 failed / 9 passed**，
Task-0 的验收标准"11 条测试全绿"在 relock 之前物理上无法达成。
更关键：C 和 D 的**核心验收标准都绕不开 B 独占的 `maos/tools/sandbox.py`**，文件级互斥假设在这里破了。

---

## 1. 阻断项（必须在开 Task-0 前解决）

| 断言原文 | 证据(file:line 或命令输出) | 判定 | 修正建议 |
|---|---|---|---|
| §三 Task-0 验收："11 条测试全绿" | `python3 -m pytest maos/tests -q` → `2 failed, 9 passed in 0.12s`；失败原因 `maos/tests/test_contracts_frozen.py:13` → `AssertionError: .contracts.lock 缺失 —— Phase 0 未正确初始化` | **不符** | 先执行 §六.2（`MAOS_RELOCK=1 python3 scripts/relock_contracts.py`）生成 `.contracts.lock`，再开 Task-0。否则 Task-0 无法判断自己是否破坏了存量。 |
| §六.2 "本地手册 §0.A 跑授权 relock，生成 .contracts.lock" | `ls .contracts.lock` → MISSING（不在 `git ls-files`，也不在工作区） | **不符（未执行）** | 人类在自己终端跑 relock。注意 `scripts/relock_contracts.py:30` 要求 `MAOS_RELOCK=1`。 |
| §六.1 "合并 .claude/settings.json：保留现有 allow，补 deny + PreToolUse hook" | `.claude/settings.json:1-15` 全文只有 `permissions.allow` 九条，**无 `deny` 键、无 `hooks` 键** | **不符（未执行）** | 补 deny + PreToolUse hook 后再开任何会话。 |
| CLAUDE.md 铁律 1："三重机制强制"（deny 规则 + PreToolUse hook + 指纹校验） | deny：`.claude/settings.json` 无此键；hook：同上无 `hooks` 键；指纹：`.contracts.lock` 缺失导致 `test_contracts_frozen.py` 两条全红 | **不符：三重机制当前 0/3 生效** | 这是最危险的一条——五个并行会话将在**完全没有契约守卫**的情况下同时动工。必须在开 worktree 前补齐。 |
| §一 Task-E 交付物 `.env.example`（根目录）；§一 Task-Ω 交付物 `deploy/.env.example` | `git check-ignore -v .env.example deploy/.env.example` → 两者均被 `.gitignore:13` 的 `.env.*` 命中 | **不符** | `.env.example` 会被 git 静默忽略，Task-E / Task-Ω 的该项交付物**永远合不进主干**。需在 `.gitignore` 加 `!.env.example` + `!deploy/.env.example`，或改用 `env.example` 命名。注意 `.gitignore` 无归属任务，属于共享文件，应由 Task-0 一次改完并冻结。 |
| §六.3 "然后 `git push`" | CLAUDE.md 铁律 5："只许本地 commit，**禁止 push**，推送由人类手动做" | **符合（但需标注）** | §六 是人肉清单，由人类在自己终端执行，不违反铁律 5。建议在剧本里显式标注"此步人类执行，Claude 不得代劳"，避免 Task-0 会话误读。 |

---

## 2. 分级问题清单

### Critical —— 会导致并行任务互相覆盖，或 Task-0 冻结后仍需改动共享文件

| 断言原文 | 证据(file:line 或命令输出) | 判定 | 修正建议 |
|---|---|---|---|
| §三 Task-C 验收："effect_risk=H 走干跑闸"；§一 C 独占文件不含 `tools/sandbox.py`；§二.14 沙箱接口"B 实现，签名冻结" | 干跑 = `sandbox_git_apply(..., check_only=True)`（§二.14）。该函数所在文件 `maos/tools/sandbox.py` 当前 **MISSING**，且 §一 Task-0 清单只创建 `maos/tools/port.py`，不创建 `sandbox.py` | **不符（互斥性破裂）** | C 在并行期无法 `import maos.tools.sandbox` —— 模块级 ImportError，不是 §二.5 那种"skill 未注册→failed 结果"的软兜底（那条只覆盖**按名调用的 skill**，不覆盖**直接 import 的函数**）。**修正：Task-0 必须创建 `maos/tools/sandbox.py`，写入 §二.14 两个签名的 `NotImplementedError` 桩，然后把文件所有权移交 B**——与 `model/client.py` 移交 Task-A 完全同一套路（§一 已有此先例）。 |
| §三 Task-D 验收："reject→补偿→**文件真实还原**" | 反向应用 = `sandbox_git_apply(..., reverse=True)`（§二.14），同属 `maos/tools/sandbox.py`（MISSING、B 独占）。§三 D 的"依赖"栏自己写着"运行期靠 B 的反向 apply" | **不符（互斥性破裂）** | D 的**头号验收标准**要求真实还原文件，这在 B 合并前不可能达成。同上修正（Task-0 建桩 + 移交）。另需把 D 的验收拆成两段：并行期验"补偿事件与 patch_ref 正确生成"，合并 B 后再验"文件真实还原"，否则 D 会卡死或被迫去写 B 的文件。 |
| §一 Task-0："skills/builtin/__init__.py（新，**自动发现**）" | Task-A 往该目录加 2 个文件、Task-B 加 1 个、Task-D 加 3 个（§一）。仓库现有各包 `__init__.py` 均为单行 docstring、**无任何 import**（`maos/skills/__init__.py:1`、`maos/agents/__init__.py:1` 等） | **无法验证（文件未创建）→ 硬前置** | 若 Task-0 写成显式 import 清单，A/B/D **三方都要改同一个文件**，三路冲突。必须强制要求 Task-0 用 `pkgutil.iter_modules` 真动态发现，并把这条写进 `contracts.md`。建议 Task-0 自带一条测试证明"新放一个文件进 builtin/ 无需改 __init__ 即被注册"。 |
| Agent 注册链路：§一/§三 全文**未分配** AGENT_POOL 注册责任 | `maos/agents/base.py:100-102` `register()` 靠 import 副作用；`maos/agents/__init__.py:1` 不 import 任何 agent 模块；现行唯一注册点是 `maos/main.py:16` `import maos.agents.coding  # noqa: F401 —— import 即注册进 AGENT_POOL`。而 `main.py` 是 **Task-0 冻结文件**。又：`maos/runtime/worker.py:34` 在**构造时**读 `AGENT_POOL.items()`，注册必须早于 `build()` | **不符（规划遗漏）** | Task-C 新增四个 Agent 后无处注册：改 `main.py` = 碰 Task-0 冻结文件（越界）；改 `agents/__init__.py` = 该文件无归属（共享文件裸奔）。**修正：Task-0 在 `contracts.md` 里明确"Agent 注册由各任务在自己的 flows/scenario_*.py 顶部 import 完成"，或 Task-0 直接把 `agents/__init__.py` 改成 pkgutil 自动发现并冻结。** 二选一，必须在 Task-0 定死。 |

### High —— 会导致某个任务的验收标准无法达成

| 断言原文 | 证据(file:line 或命令输出) | 判定 | 修正建议 |
|---|---|---|---|
| §一 Task-0："flows/（新包）：common.py + scenario_1..4.py（**现行为原样迁移**）"；§二.10 冻结 `build(script, *, matrix=False)` | 场景 2 现实现 `maos/main.py:106-131` **不调用 `build()`**：它在 `main.py:110-116` 定义了 `FlakyModel(ScriptedModelClient)` 子类，然后在 `main.py:118-122` 手工拼装 store/bus/cp/model/worker/gate。冻结签名 `build(script, *, matrix=False)` **没有任何注入自定义 model 的入口** | **不符** | "原样迁移"在场景 2 上不成立。C 拿到 `scenario_2.py`（C 独占）后若发现需要 `build(..., model=...)`，就必须改 `flows/common.py`（Task-0 冻结）→ 触发 §五.5 的"停下报告"，并行直接停摆。**修正：Task-0 把签名冻结为 `build(script, *, matrix=False, model=None)`（model=None 时按 script 建 ScriptedModelClient），或允许 scenario_2 内联拼装并在 contracts.md 写明。** |
| §二.10 未声明 `build()` 的**返回契约** | `maos/main.py:39` `return store, bus, cp, model, worker, gate`（六元组）；`main.py:95`、`main.py:135`、`main.py:156` 三处按六元组解包 | **不符（规格缺失）** | 返回值是 Task-0 与 C/D 之间的实际冻结面，§二.10 只写了入参。Task-0 必须在 `contracts.md` 显式冻结六元组顺序，否则 C 和 D 各自解包会漂移。 |
| §五.3 "补偿 patch_ref 解析不一致（C 干跑 vs D 执行）——统一走 artifacts.resolve_patch_ref" + §二.15 补偿引用由 Control Plane 自动附着（归 Task-D） | 生产端是 D 的 `core/control_plane.py`（D 独占）；消费端是 C 的 `runtime/gate.py`（C 独占）。并行期 C 侧**没有任何真实数据源**能产出该 ref | **不符（验收时序问题）** | C 的 `test_agents_gate.py` 只能手工构造 compensation artifact 来验干跑闸。规划未说明这点，C 会误以为可以端到端验。**修正：在 contracts.md 给出一份 compensation artifact 的 golden fixture，C/D 双方都对它写测试**，这样合并时才有共同基准。 |

### Medium —— 规划描述与代码现状不符，但可在任务内消化

| 断言原文 | 证据(file:line 或命令输出) | 判定 | 修正建议 |
|---|---|---|---|
| §二.10 "懒加载 `hiclaw.matrix_bus.MatrixEventBus(inner_bus, config)`" | `config` 的类型与字段**全文未定义**。`maos/core/eventbus.py:26-34` 的 `EventBus` ABC 仅三方法：`publish/subscribe/drain` | **规格不足** | 这是 Task-0 与 Task-E 之间的冻结面。Task-0 须在 contracts.md 定死 `config` 形状（建议 dataclass 或明确 dict 键集）与 `MatrixEventBus` 必须实现的三方法，否则 E 写完接不上。 |
| §一 Task-0 flows 包清单未列 `flows/__init__.py` | 现有各包均有 `__init__.py`（`maos/core/__init__.py` 等）；`maos/flows/` 当前 MISSING | **遗漏（低风险）** | 补进 Task-0 清单，明确其内容（建议留空 docstring，避免变成第二个共享注册点）。 |
| §一 Task-0 创建 `scenario_1..4.py`，§一 C 独占 `scenario_1.py`/`scenario_2.py`，D 独占 `scenario_5.py` | `scenario_3.py` / `scenario_4.py` 在 Task-0 之后**无归属人** | **描述缺口（良性）** | 实际无人再改，但建议在 §一 显式写"Task-0 后冻结，无人再碰"，避免歧义。 |
| §一 "永不许碰：…；守卫与配置文件" | `.gitignore` 既不在冻结清单、也不属任何任务，但 Task-E 的 `.env.example` 交付**必须改它**（见阻断项） | **归属缺失** | 把 `.gitignore` 的必要改动并入 Task-0 一次做完并冻结。 |

### Low —— 措辞/编号/数字偏差

| 断言原文 | 证据(file:line 或命令输出) | 判定 | 修正建议 |
|---|---|---|---|
| `.claude/settings.json` allow 含 `Bash(python -m:*)` | `.claude/settings.json:6`；但 CLAUDE.md"本机环境注意"与 `docs/DECISIONS.md:14` 均记明本机无 `python`，一律 `python3` | **不符（良性）** | allow 规则匹配不到 `python3 -m pytest`，五个会话会反复弹权限提示。§六.1 合并 settings 时顺手把该条改成 `Bash(python3 -m:*)`。 |
| §三 Task-A 验收命令用 `MAOS_LLM_API_KEY` | 仓库代码中**无任何 `MAOS_LLM_*` 引用**（`rg 'MAOS_[A-Z_]+'` 仅命中 `MAOS_RELOCK`）；但 `docs/phases/phase-1.md:32` 明确规定读 `MAOS_LLM_BASE_URL / MAOS_LLM_API_KEY / MAOS_LLM_MODEL` | **符合（来源在手册，非代码）** | 无需修正。Task-A 实现时以 phase-1.md:32-33 为准（含 `MAOS_LLM_TIMEOUT` 默认 120s）。 |

### 复核为"符合"的关键断言（已逐字段核实，人类不必重查）

| 断言原文 | 证据(file:line) | 判定 |
|---|---|---|
| §二.15 "TaskAssignment payload 无 effect_risk 字段" —— 整条偏离决策的全部理由 | `maos/contracts/events.py:99-105` payload 仅 `{role, inputs, acceptance, risk_level, rework_findings}`；`events.py:192` 必填集合亦无 effect_risk。对照面：`store.py:102` task 表**有** `effect_risk` 列，`control_plane.py:193` 读得到，`gate.py:121` 也读得到 | **符合。偏离决策成立且论证正确**——Agent 侧确实拿不到，控制面确实拿得到，"补偿是控制面行为"在代码上站得住。 |
| §二.5 "抛 PermissionDenied（复用 agents.base 的类）" | `maos/agents/base.py:59` `class PermissionDenied(Exception)` 存在 | **符合** |
| §二.5 "name ∉ identity.allowed_skills" | `maos/agents/base.py:24` `allowed_skills: frozenset[str] = frozenset()` 存在；`coding.py:30`、`manager.py:28` 均已填值 | **符合** |
| §二.9 "BaseAgent.__init__(model, store=None)" 的改造前提 | 现签名 `maos/agents/base.py:66` `def __init__(self, model: ModelClient) -> None` | **符合**（现状确为单参，加默认参数不破坏 `worker.py:34` 的 `cls(model)` 调用） |
| §二.8 "store.py 新增 knowledge 表" | `maos/core/store.py:79-149` DDL 现有五表：plan/task/artifact/event_log/processed_key，**无 knowledge**。且 `test_contracts_frozen.py:29-30` 注释明确"新增表不受影响"、`:46` 只遍历 lock 中已有表 | **符合**（新增表不会触发冻结守卫） |
| §三 Task-0 "11 条测试"、§二.9 "既有 9 条测试" | `pytest --collect-only` → `11 tests collected`：`test_contracts.py` 9 条 + `test_contracts_frozen.py` 2 条 | **数字准确**（但当前 2 条红，见阻断项） |
| §一 所有标"（新）"的文件确实不存在 | 逐个 `test -e` 全部 MISSING：`skills/contract.py`、`registry.py`、`invoker.py`、`builtin/__init__.py`、`tools/port.py`、`artifacts.py`、`flows/`、`docs/parallel/contracts.md`、`tools/sandbox.py`、`deploy/sandbox.Dockerfile`、`scenarios/fixture-repo`、`scenarios/inputs`、`runtime/plan_finalizer.py`、`hiclaw/matrix_bus.py`、`obs/trace.py`、`obs/otel.py`、`scripts/make_evidence.py`、`deploy/docker-compose.yml`、五个新 agent 文件、五个新测试文件 | **符合，无一例外**（不存在"标新却已存在"的冲突源） |
| §一 所有标"修改"的文件确实存在 | `store.py`、`agents/base.py`、`runtime/worker.py`、`main.py`、`run.py`、`model/client.py`、`agents/coding.py`、`core/control_plane.py`、`runtime/gate.py` 均 TRACKED 存在 | **符合** |
| §三 Task-E 前提：`hiclaw/` 包存在 | `hiclaw/__init__.py:1`（TRACKED） | **符合** |
| 文件头："本文件不进仓库（目录已 gitignore）" | `git check-ignore -v` → `.gitignore:3:docs/superpowers/plans/` 命中 | **符合** |
| §六.5 `git worktree add .worktrees/task-a` | `.gitignore:2` 已忽略 `.worktrees/` | **符合** |
| §三 Task-E "不装 [e2e]" | 指 matrix-nio 自身的 extra，非 MAOS 的。`docs/phases/phase-3.md:20`："matrix-nio 需要 [e2e] extra + libolm"；MAOS 侧依赖为 `pyproject.toml:13` `hiclaw = ["matrix-nio"]` | **符合**（措辞正确，无需修正） |
| Task-D Replan 需要 Plan RUNNING→PENDING 迁移 | `maos/contracts/states.py:57` `(PlanState.RUNNING, PlanState.PENDING): "replan"` **已存在于冻结契约中** | **符合**（D 无需改契约，冻结假设成立） |
| §一 `model/client.py` 同时出现在 Task-0 与 Task-A | Task-0 串行在先，§一 已注明"文件随后移交 Task-A 独占" | **符合**（非并行冲突） |
| 基线：HEAD 与 f104161 | `git rev-parse HEAD` = `f104161f722…`；`git diff --stat f104161..HEAD` 空输出；`git status --porcelain` 仅 `?? .claude/` | **符合**（工作区干净，基线成立） |

---

## 3. 无法验证项

| 项 | 缺什么 | 需要补什么 |
|---|---|---|
| §一 Task-0 `skills/builtin/__init__.py` 是否真为动态发现 | 文件尚未创建，无法读取实现 | 这是 Critical 级前置。请在 Task-0 的 prompt 里把"必须 pkgutil 动态发现 + 自带一条证明测试"写成硬要求，我无法从现有代码推断 Task-0 会怎么写。 |
| §二.16 场景 2 编排（`auth/session.py::is_session_valid` 时区 bug、`tests/test_session.py::test_expired_session`） | `scenarios/fixture-repo/` 不存在，无任何可比对物 | 属于"待建规格"而非"与现状冲突"，无需修正；仅提示 B 与 C 必须以 contracts.md 为唯一口径。 |
| §三 Task-B "镜像可 build" / §五.7 "Docker 不可用" | 我未运行 docker（只读约束 + 属环境事实非仓库事实） | 请你在开 B 之前自己确认 `docker info` 可用；若不可用，B 的验收需按 §五.7 全程走降级路径。 |
| `docs/parallel/contracts.md` 内容 | 该文件是 Task-0 的产出物，当前不存在 | 无法预审。建议 Task-0 完成后**再做一次同样的审计**，重点核对本报告 High 级里三条"规格缺失"（build 返回契约、config 形状、compensation golden fixture）是否补齐。 |
| §六 末尾"六段任务 prompt 见对话正文" | prompt 正文不在本文件中 | 若要我审 prompt 与本规划的一致性，请把六段 prompt 贴给我或落到文件里。 |
| §五 合并顺序的运行期正确性（A→B→C→E→D→Ω） | 需要真实执行五个任务后才能验证 | 无法静态验证。但基于本报告的互斥性发现，建议把顺序改为 **Task-0 → B → A → C → E → D → Ω**：B 先落地 `sandbox.py` 真实现，能同时解掉 C 和 D 的越界依赖。 |

---

## 4. 互斥性矩阵

行 = Task A–E；列 = 真实触达的文件（含间接调用与 import 链）。
图例：**W** = 独占写；`r` = 只读依赖（import / 按签名调用）；**⚠W** = **越界**（需写他人独占或 Task-0 冻结文件）；`—` = 无关。

| | `tools/sandbox.py`<br>(B 独占) | `core/control_plane.py`<br>(D 独占) | `flows/common.py`<br>(0 冻结) | `maos/main.py`<br>(0 冻结) | `skills/builtin/__init__.py`<br>(0 冻结) | `agents/__init__.py`<br>(无主) | `.gitignore`<br>(无主) | `agents/base.py`<br>(0 冻结) | `artifacts.py`<br>(0 冻结) | `runtime/gate.py`<br>(C 独占) |
|---|---|---|---|---|---|---|---|---|---|---|
| **A** 模型+2 Skill | — | — | `r` | `r` | `r`（放新文件，不改） | — | — | `r` | `r` | — |
| **B** 沙箱+靶场 | **W** | — | — | — | `r`（放新文件，不改） | — | — | — | `r` | — |
| **C** 四 Agent+Gate | **⚠W / ⚠r** | `r` | **⚠W?** | **⚠W?** | — | **⚠W?** | — | `r` | `r` | **W** |
| **D** 治理+补偿 | **⚠r** | **W** | `r` | `r` | `r`（放新文件，不改） | — | — | `r` | `r` | `r` |
| **E** Matrix 总线 | — | — | `r` | `r` | — | — | **⚠W** | — | — | — |

**越界格子逐条说明：**

1. **C × `tools/sandbox.py` = ⚠** —— C 的验收"effect_risk=H 走干跑闸"需 `sandbox_git_apply(check_only=True)`（§二.14）。文件 MISSING 且 Task-0 不创建 → C 要么建桩（写 B 的独占文件，合并必冲突），要么验收不达标。**规划里最硬的一处互斥性破裂。**
2. **D × `tools/sandbox.py` = ⚠** —— D 的头号验收"文件真实还原"需 `sandbox_git_apply(reverse=True)`。同一文件。D 自己的依赖栏已写"运行期靠 B 的反向 apply"，但验收标准没有相应降级，形成自相矛盾。
3. **C × `flows/common.py` / `maos/main.py` = ⚠?** —— 取决于 Task-0 如何冻结 `build()`。现行场景 2（`main.py:106-131`）绕过 `build()` 自建 FlakyModel；若 Task-0 照 §二.10 的签名原样冻结，C 迁移场景 2 时必然要回头改 common.py。
4. **C × `agents/__init__.py` = ⚠?** —— 四个新 Agent 的 AGENT_POOL 注册无处安放（现行唯一注册点 `main.py:16` 属 Task-0 冻结）。C 若改 `agents/__init__.py`，该文件无归属、无冻结、无守卫。
5. **E × `.gitignore` = ⚠** —— `.env.example` 被 `.gitignore:13` 忽略，E 不改 `.gitignore` 就交付不了该文件；而 `.gitignore` 不属任何任务。

**结论：五行两两之间的"文件名清单无交集"成立，但"真实触达面无交集"不成立。**
上述 1、2 两条必须由 Task-0 建桩移交解决；3、4、5 三条必须由 Task-0 一次性冻结/归属解决。
按这五条修正后，A/B/C/D/E 的并行安全性才真正建立在文件级互斥上。

---

## 附：本次审计执行的只读命令

```bash
git log -1 --format='%H%n%ci%n%s' f104161
git rev-parse HEAD
git status --porcelain=v1 / --untracked-files=all
git diff --stat f104161..HEAD
git ls-files
git check-ignore -v .env.example deploy/.env.example docs/superpowers/plans/parallel-build-plan.md .worktrees/task-a
python3 -m pytest maos/tests -q --collect-only
python3 -m pytest maos/tests -q
rg -n 'effect_risk' maos/
rg -n 'MAOS_[A-Z_]+' (排除 legacy-ts)
rg -n 'MAOS_LLM|e2e|matrix-nio' docs/phases/
读取：contracts/events.py, contracts/states.py, core/store.py, core/control_plane.py,
      core/eventbus.py, agents/base.py, agents/coding.py, agents/manager.py,
      runtime/gate.py, runtime/worker.py, model/client.py, main.py, run.py,
      全部 __init__.py, .gitignore, .claude/settings.json, pyproject.toml,
      scripts/guard_bash.py, scripts/relock_contracts.py,
      maos/tests/test_contracts_frozen.py, docs/DECISIONS.md, docs/BACKLOG.md
```

未运行任何 `git merge` / `git worktree add` / `git add` / `git commit` / 文件写操作（本文件除外）。
