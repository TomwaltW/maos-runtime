# MAOS 并行构建冻结契约（contracts.md）

- 本文件是 Task-0 与 A/B/C/D/E/Ω 六轨之间**唯一**的跨轨契约。2026-08-27 由人类定稿冻结。
- 冻结含义：任何会话不得修改本文件。执行中发现契约写错或做不到 → 停下、BLOCKED 回执升级人类；解锁只在人类终端发生。
- 安装位置：仓库 `docs/parallel/contracts.md`（由 Task-0 从交接包**原样复制**入库）。
- 全局口径：本机命令一律 `python3`（无 `python`）；包名 `maos.*`；不存在根级 `tests/`、`agents/`；测试统一 `python3 -m pytest maos/tests -q`。

---

## 第一部分 · 八条核心契约（每条：约定 / 反例 / 验证方式）

### C-1 skills/builtin 动态发现
- **约定**：`maos/skills/builtin/__init__.py` 用 `pkgutil.iter_modules` 扫描本包并 import 全部非下划线开头模块（skill 模块 import 时经 `@register_skill` 自注册，见 A-4）；另提供幂等 `discover()` 供重扫。任何任务向 builtin/ 投放新 skill 文件时，**禁止改 `__init__.py`**。
- **反例**：`__init__.py` 写成显式 import 清单——A/B/D 三方都要往这个目录加文件，显式清单等于三方改同一个文件，三路合并冲突。
- **验证**：`maos/tests/test_registry_autodiscovery.py`——测试运行时向 builtin/ 临时写入一个带 `@register_skill` 的模块，调 `discover()` 后断言 `registry.get` 取得到，全程不改 `__init__.py`。

### C-2 AGENT_POOL 注册口径
- **约定**：`maos/agents/__init__.py` 用 `pkgutil.iter_modules` 自动 import 本包全部模块；带 `@register` 的 Agent 类以 `identity.role` 为键进 `AGENT_POOL`（现行机制见 `maos/agents/base.py:97-102`）。`maos/main.py` 第 16 行 `import maos.agents.coding` 手动注册行**删除**。此后新增 Agent 只投文件、不改 `__init__.py`。
- **反例**：在各 scenario 顶部 import 注册（已否决：每个场景重复一份清单，必漂移）；或继续依赖 main.py 手动 import——`worker.py:34` 在构造时读 `AGENT_POOL.items()`，凡不经 main.py 的入口，注册就漏。
- **验证**：`python3 -c "from maos.agents import AGENT_POOL; print(sorted(AGENT_POOL))"` → 现阶段期望**恰为 `['coding']`**（ManagerAgent 刻意未挂 `@register`：它不经 worker 分发，维持现状，不要"顺手"注册它）。

### C-3 build() 入参签名（冻结）
- **约定**：`maos/flows/common.py::build(script, *, matrix=False, model=None)`。`model=None` 时按 script 构造 `ScriptedModelClient(script)`；传入 model 实例则原样注入（场景 2 的 FlakyModel 由此进入）。`matrix=True` 时懒加载 `hiclaw.matrix_bus.MatrixEventBus` 包装 inner bus，ImportError 或连接失败 → 打警告回退 inner bus（Task-E 落地前恒回退）。
- **反例**：scenario_2 内联拼装 store/bus/cp/model/worker/gate 六件套绕过 build()（现行 `main.py:118-122` 即此形态，迁移时必须消除；留第二条构造路径一定漂）。
- **验证**：迁移后 `maos/flows/scenario_2.py` 不再出现 `SqliteStore()` / `ControlPlane(` 等构造调用，只调 `build(..., model=...)`；`python3 run.py` 场景 2 结论不变（task DONE 且 attempt=2）。

### C-4 build() 返回契约（冻结）
- **约定**：返回六元组，**位序与类型**（依据现行 `maos/main.py:31-39`，迁移后位于 flows/common.py）：
  | 位 | 变量名 | 类型 |
  |---|---|---|
  | 0 | store | `maos.core.store.SqliteStore`（已 `init_schema()`） |
  | 1 | bus | `maos.core.eventbus.InMemoryEventBus` |
  | 2 | cp | `maos.core.control_plane.ControlPlane` |
  | 3 | model | `maos.model.client.ModelClient`（缺省 `ScriptedModelClient`，或注入实例） |
  | 4 | worker | `maos.runtime.worker.WorkerRuntime`（worker_id="w1"） |
  | 5 | gate | `maos.runtime.gate.ReviewerGate` |
  解包写法冻结为 `store, bus, cp, model, worker, gate = build(...)`。
- **反例**：只冻入参不冻返回值，C 与 D 各自按位置解包、一方擅自调换位序——运行到深处才炸，合并期最难排查的一类漂移。
- **验证**：`test_registry_autodiscovery.py` 对 `build({})` 的返回逐位 `isinstance` 断言；现有三处解包（原 main.py:95/135/156，迁入 scenario_1/3/4）语义不变。

### C-5 compensation artifact golden fixture
- **约定**：固定文件 `maos/tests/fixtures/compensation_golden.json`，内容**恰为**：
  ```json
  {
    "kind": "compensation",
    "content": {
      "mode": "reverse",
      "patch_ref": {"task_id": "task-cmp-golden-001", "kind": "patch_set", "attempt": 1}
    }
  }
  ```
  compensation content schema 冻结：`{"mode":"reverse","patch_ref":{"task_id":str,"kind":"patch_set","attempt":int}}`。patch_ref 解析**统一走** `maos/artifacts.py::resolve_patch_ref(store, ref)`（A-7），禁止任何一方自写解析。C（干跑闸测试）与 D（执行器测试）都必须直接加载本 fixture 写测试。
- **反例**：C 和 D 各自在测试里手搓 compensation dict——字段名与嵌套层级各凭记忆，合并后 C 的干跑闸读不出 D 产的 ref，联调期才爆。
- **验证**：C、D 两轨的测试文件中均出现对 `fixtures/compensation_golden.json` 的加载；`resolve_patch_ref` 对 golden 的 ref 在配好 patch_set artifact 时返回非 None、缺失时返回 None（正负例各一）。

### C-6 MatrixEventBus 构造契约
- **约定**：`hiclaw/matrix_bus.py::MatrixEventBus(inner_bus, config)`。config 为同文件定义的 dataclass **MatrixBusConfig**：
  | 字段 | 类型 | 来源 env |
  |---|---|---|
  | homeserver | str | MATRIX_HOMESERVER |
  | user | str | MATRIX_USER |
  | token | str | MATRIX_TOKEN |
  | room_id | str | MATRIX_ROOM_ID |
  | approvers | frozenset[str] | MAOS_APPROVERS（逗号分隔） |
  | log_only | bool = False | （连接失败 / 加密房 / 缺必填 env → 自动置 True 降级，不抛异常） |
  提供 `MatrixBusConfig.from_env()`。`MatrixEventBus` 必须实现 `EventBus` 抽象三方法 `publish/subscribe/drain`（`maos/core/eventbus.py:26-34`），降级模式下三方法行为与 inner_bus 完全一致。密钥只读环境变量，禁止写进任何文件。
- **反例**：config 留成「dict 随便传」——E 侧写 `config["hs"]`、flows 侧传 `config["homeserver"]`，键名对不上要到演示现场才发现。
- **验证**：`maos/tests/test_matrix_bus.py` 断言降级模式与 inner bus 行为一致、`from_env` 缺 env 自动降级；Task-0 期 `build(matrix=True)` 恒走 ImportError 告警回退（hiclaw/matrix_bus.py 尚不存在）。

### C-7 sandbox 所有权移交 + Task-D 验收分段
- **约定**：`maos/tools/sandbox.py` 桩已存在（commit 9d3fe4d），所有权自 Task-0 起**移交 Task-B**，其余任务只 import 不修改。两签名冻结（就地照录）：
  - `sandbox_git_apply(patch_set: dict, workdir: str, *, reverse: bool = False, check_only: bool = False) -> {"ok": bool, "error": {"stage","path","hunk","message"} | None}`（reverse=True 即 `git apply -R` 补偿回滚；再加 check_only=True 即补偿干跑闸）
  - `sandbox_pytest_run(workdir: str) -> {"passed": int, "failed": int, "errors": int, "cases": [{"id","status","msg"}], "duration": float, "tool_error": str | None}`（tool_error=环境/工具炸了，failed=用例真挂了，**必须分开上报**，Gate 判定不同）
  **Task-D 验收拆两段写死**：并行开发期只验「补偿事件与 patch_ref 正确生成」（用 C-5 golden fixture + 本桩）；合并期验「文件真实还原」（按附录 D 合并顺序，D 合并时 B 必已 MERGED）。
- **反例**：C 或 D 等不及 B，自己往 sandbox.py 填「临时实现」——合并 B 时整文件冲突，两份实现语义不一致。
- **验证**：git log 中 sandbox.py 仅 Task-B 的提交触碰；D 并行期测试只依赖 golden fixture 与桩的 NotImplementedError 行为。

### C-8 .gitignore 放行 .env.example（一次改完冻结）
- **约定**：`.gitignore` 在 `.env.*` 规则之后追加两行：`!.env.example` 与 `!deploy/.env.example`。由 Task-0 一次改完；此后 `.gitignore` 入冻结面，E/Ω 只投放 .env.example 文件本体，不再碰 `.gitignore`。
- **反例**：Task-E 交付 .env.example 时才发现被 `.env.*` 静默忽略、顺手自己改 .gitignore——共享文件无归属修改，与他轨冲突（此即审计原始阻断项）。
- **验证**：`git check-ignore -v .env.example deploy/.env.example ; echo "exit=$?"` → **无匹配输出且 exit=1**。

---

## 附录 A · 骨架签名规格（Task-0 建骨架，各轨按此填肉）

- **A-1 SkillContract**（`maos/skills/contract.py`，dataclass）：`name:str`；`version:str`(semver)；`purpose:str`；`input_schema:dict`；`output_schema:dict`；`preconditions:list[str]`；`depends_tools:list[str]`；`failure_policy:str`(retry|fallback|escalate) + `max_retries:int`；`security_boundary:str`；`reuse_note:str`；`owner_roles:list[str]`
- **A-2 SkillResult**（同文件）：`status:str`(ok|failed)；`output:Any`；`error:str|None`；`duration_ms:int`；`usage:dict|None`
- **A-3 SkillContext**（同文件）：`model:ModelClient|None` / `store` / `identity` / `extras:dict`。invoker 自身不持有 model；`model` 取自 `extras.get("model")`（Task-A 给 Coding 接线时传 `extras={"model": self.model}`），Task-0 阶段可为 None。
- **A-4 registry**（`maos/skills/registry.py`）：`@register_skill` 装饰器；`get(name, version=None)` 默认返回最高版本；内部 `dict[name][version]` 保留历史版本。
- **A-5 SkillInvoker**（`maos/skills/invoker.py`）：`SkillInvoker(identity, store)`；`invoke(name, payload, *, version=None, extras=None) -> SkillResult`：
  - `name ∉ identity.allowed_skills` → 抛 `PermissionDenied`（**复用** `maos/agents/base.py:59` 的类，不新建）；
  - name 未注册 → 返回 `SkillResult(status="failed", error="skill_not_found:<name>")`，**不抛异常**（跨轨按名调用可先行，合并后自动升级为真实现）；
  - preconditions 逐条检查；failure_policy=retry 时按 max_retries 重试；
  - 成败都落一条 **SkillInvoked**：写 `store.append_event_log` 的 event_log **行**（`event_type="SkillInvoked"`），**不是总线 Envelope**——冻结的 `maos/contracts/events.py` 中没有该事件类型（全仓 grep 零命中），禁止为此改 events.py。行结构对照 `maos/core/control_plane.py:43-50` 的 append_event_log 用法，from_state/to_state 传空串；`detail={skill, version, status, duration_ms, input_digest, output_hash, usage}`（usage 可 null）。`store=None` 时跳过落库。
- **A-6 ToolPort**（`maos/tools/port.py`）：九要素 dataclass（`name/purpose/entry/params_schema/returns_schema/failure_modes/security_boundary/rate_limit/owner`）+ `invoke_tool()`，调用落 **ToolInvoked** event_log 行（同 A-5 落库方式）：`detail={tool, status, duration_ms, params_digest, error}`。
- **A-7 artifacts**（`maos/artifacts.py`）：常量 `KIND_PATCH_SET/TEST_REPORT/ARCH_CONTRACT/REVIEW_NOTE/COMPENSATION`；`validate_artifact(kind, content)`；test_report schema = C-7 的 sandbox_pytest_run 返回；compensation schema = C-5；architecture_contract 必填键 `api/idempotency/audit/reversibility`；`resolve_patch_ref(store, ref)`：按 `ref["task_id"]` 取 artifacts，过滤 `kind=="patch_set"` 且 `version==ref["attempt"]`，命中返回该 artifact dict，否则 None。
- **A-8 knowledge 表**（`maos/core/store.py`，唯一一次修改=新增；现有五表 plan/task/artifact/event_log/processed_key 的 DDL 一字不动）：`knowledge(id, plan_id, kind(rule|case), title, body, tags, created_at)` + `insert_knowledge(row)` / `list_knowledge(*, tags=None, keyword=None)`。指纹锁 `.contracts.lock` 只校验既有表，新增表不触发；**禁用 MAOS_RELOCK**。
- **A-9 BaseAgent**（`maos/agents/base.py`）：`__init__(self, model, store=None)`；`self.skills = SkillInvoker(self.identity, store)`。默认参数保证现行 `cls(model)`（worker.py:34）与既有测试不改仍绿；worker 构造处改为 `cls(model, store=self.cp.store)`。CodingAgent.run() 改经 invoker 是 **Task-A** 的活，Task-0 不动。
- **A-10 flows/common.py**：`build()`（C-3/C-4）；`run_until_settled(bus, gate, cp, plan_id, max_cycles=20)`；`dump(cp, plan_id, title)`；演示常量 `GOOD_PATCH/BAD_PATCH/PLAN_JSON` 原样迁自 main.py:73-90。
- **A-11 入口分发**：`maos/main.py` 的 `main()` 改为分发器：`--scenario N`（缺省顺跑 1–4）、`--matrix`；scenario_5 占位=打印「未实现」并退出码 1。`run.py` 维持薄转发。`python3 run.py` 无参时四场景行为与迁移前一致。
- **A-12 select_model_client**（`maos/model/client.py`）：`select_model_client(script=None, *, force_scripted=False) -> ModelClient`。Task-0 版**恒返** `ScriptedModelClient(script)`；签名与语义冻结：场景 5 与全部测试必须 `force_scripted=True`；真模型分支由 Task-A 填（env：`MAOS_LLM_BASE_URL/MAOS_LLM_API_KEY/MAOS_LLM_MODEL/MAOS_LLM_TIMEOUT`(默认 120s)；异常与日志禁止回显 key）。该文件自此**移交 Task-A**。
- **A-13 手册偏离备案**（归 Task-D 落实，Task-0 仅录入本条）：effect_risk=H 的补偿引用由 Control Plane 在 on_task_result 收到 patch_set 时自动附着——TaskAssignment payload 无 effect_risk 字段且 events.py 冻结，Agent 拿不到；机制等价且补偿本属控制面行为。

## 附录 B · 六个 Skill 的 IO 契约（含 invoker 白名单语义）

白名单语义：invoke 前先校验 `name ∈ identity.allowed_skills`（现行值：coding=`{code.repo-patch, kb.retrieve}`（coding.py:31）、manager=`{req.normalize, kb.retrieve}`（manager.py:28）），违者 `PermissionDenied`。规划原文写「七个」实列六个，冻结按以下六个；若需第七个即契约变更，走 BLOCKED 升级。

| # | skill | 输入 | 输出 |
|---|---|---|---|
| B-1 | req.normalize | `{goal:str, context?:dict}` | `{normalized_goal:str, constraints:list[str], acceptance_suggestions:list[str]}` |
| B-2 | code.repo-patch | `{title:str, inputs:dict, acceptance:list[str], rework_findings:list[dict]}` | patch_set content：`{files:[{path:str,diff:str}], summary:str, self_check:{build:"pass|fail", lint:"pass|fail"}}`（与现行 GOOD_PATCH 形状一致；路径白名单校验留在 skill 的 security_boundary 执行处） |
| B-3 | test.verify | `{workdir:str}` | test_report（= C-7 schema） |
| B-4 | issue.aggregate | `{findings:list[dict]}` | `{issues:[{id:str,severity:str,title:str,detail:str,source:str}], summary:str}` |
| B-5 | kb.sink | `{plan_id:str, kind:"rule"|"case", title:str, body:str, tags:list[str]}` | `{knowledge_id:str}` |
| B-6 | kb.retrieve | `{tags?:list[str], keyword?:str, limit?:int}` | `{items:list[knowledge 行], count:int}` |

## 附录 C · 场景 2 编排冻结（B 造靶场 / C 写流程的共同口径）

fixture-repo 的 bug 固定为：`auth/session.py::is_session_valid` 使用本地时区导致会话被提前判过期；`tests/test_session.py::test_expired_session` 打补丁前必挂、`test_valid_session` 恒过。B 照此造 `scenarios/fixture-repo`，C 照此写场景流程，两侧不得另立口径。

## 附录 D · 协作与合并纪律

- 合并顺序（冻结，覆盖一切旧口径）：**Task-0 → B → A → C → E → D → Ω**；每合并一轨跑全量 pytest + run.py。
- **Task-0 完工后 `maos/main.py` 冻结**：任何后续任务不得再改；Task-C 的入口/场景层工作只许动 `flows/scenario_1.py` 与 `flows/scenario_2.py`。
- `flows/scenario_3.py`、`scenario_4.py` 在 Task-0 之后冻结，无人再碰。
- 文件所有权速查：sandbox.py→B；model/client.py→A；gate.py + scenario_1/2→C；control_plane.py + plan_finalizer + scenario_5→D；hiclaw/** + 根 .env.example→E；obs/** + make_evidence + compose→Ω。
- 跨轨引用只按「冻结签名 + skill 名字」；skill 未注册走 failed 兜底（A-5），合并后自动升级。
- 本文件冻结：执行中发现错误 → BLOCKED 升级人类，不许当场改。
