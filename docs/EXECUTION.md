# MAOS 复赛改造 · Claude Code 执行手册 v4（业务纵切版）

配套文档：《MAOS-GOAI-复赛总体方案.md》（架构与"为什么"在那边，本手册只管"怎么做"）
执行窗口：8.27 → 9.2，9.3 提交
仓库：github.com/TomwaltW/maos-runtime（本地克隆上操作）
分支：goai-restructure（沿用 v3，不新开）

---

> ## 入库说明（**非手册原文**，入库时添加 · 读正文前先读这段）
>
> **来源**：Google Doc《MAOS-执行手册-v4-业务纵切版》
> `docId=1rJ6vuh6EbK-vZ2Ousz_kzcIbiLMSaYnkJeR0sERiiZs`，Doc 修改时间 `2026-08-27T13:27:15Z`。
> 入库时间 `2026-08-28`。正文**逐字保真**，仅修复 Google Doc 导出的格式损伤
> （表头错位、有序列表被压平成 `1.`、代码块被打散成段落）。**一处措辞都没改。**
>
> ### 1. 场景编号映射（裁决 D-05）——正文里的 `--scenario R1` 不能直接敲
>
> `maos/main.py` 的 `--scenario` 是 `type=int` + `choices=ALL_SCENARIOS`，`--scenario R1` 会被
> argparse 直接拒掉。裁决：退款域场景走整数编号。
>
> | 手册正文写的 | 仓库实际入口 | 含义 | 落地轨 |
> | :-- | :-- | :-- | :-- |
> | `--scenario R1` | `--scenario 6` | 退款顺利路径 | R-2（`maos/flows/scenario_6.py`） |
> | `--scenario R2` | `--scenario 7` | 退款失败 → 补偿 | R-2 / P4 |
> | `--scenario R5` | **未裁决** | RAG 有无对照 | P5，本轮不涉及 |
> | `R3a/R3b`、`R4a/R4b`、`R6` | **未裁决** | 租户/渠道/政策版本对照 | P5，本轮不涉及 |
>
> `ALL_SCENARIOS` 扩为 `(1,2,3,4,5,6,7)`；`DEFAULT_SCENARIOS` **保持 `(1,2,3,4)` 不变**。
> 这是 `main.py` 冻结后**唯一一次**修改，**归 R-1 一次改完**，其余轨一律不碰。
>
> ### 2. 本机执行：正文的 `python` 一律敲 `python3`
>
> 这台 Mac 没有 `python` 命令。正文的 `python -m pytest` 等字样**按原样保留**（它是手册原文），
> **执行时替换为 `python3` 即可**——不要为了统一去批量改写正文。
>
> ### 3. `docs/phases/` 是 v3 编号，与本手册对不上
>
> 仓库里的 `docs/phases/phase-<N>.md` 是 **v3** 编号（其 phase-3 = HiClaw、phase-5 = 可观测），
> 与本手册的 Phase 编号**不是一回事**。退款域轮**一律以本文件为准**，不看 `docs/phases/`。
>
> ### 4. 事实源优先级（冲突时按此顺序，不要自己权衡）
>
> `docs/parallel/contracts.md`（冻结契约，只读） > 当轮派单 > **本文件** > `docs/ops/ORCHESTRATION.md` > `docs/BACKLOG.md`
>
> 契约与派单冲突 → **停下报告**，不要自选一个继续。
>
> ### 5. 手册已写、但仓库尚未落地的地基（记账，勿当既成事实）
>
> - **P1 第 7 步的 `maos/store/port.py`（StorePort 抽象）从未落地**，`maos/store/` 目录不存在。
>   P5 的「RAG 后端可插拔 SQLite / Postgres+pgvector / PolarDB」**没有地基**。
>   不阻塞退款域本轮，但 **P5 之前必须补**。见 `docs/BACKLOG.md`。

---

## 0. 本版相对 v3 改了什么（先读这段）

评委三段反馈的诊断只有三条，处方可以换，诊断换不掉：

1. 没有可执行制品和运行证据 → v3 已经在解决
2. **现实业务锚点不足** → v3 完全没解决：软件域是自证式 demo
3. **"所有 Agent 都回复完成" ≠ 业务成功** → v3 没有外部成功判据

v4 的改动全部指向第 2、3 条，且**采取"加轨道"而非"换轨道"**：

| 改动 | 说明 |
| :-- | :-- |
| **新增退款业务域轨道** | 制造企业售后退款纵切，复用现有 Control Plane / Skill / ToolPort / Gate / 补偿 / replan 内核，一行内核代码不改 |
| **知识层升级为 RAG** | v3 的 knowledge 表 → 两阶段检索（结构化预过滤 + 混合召回），后端可插拔 SQLite / Postgres+pgvector / PolarDB |
| **新增 settled guard** | 外部权威事实边界：只有 `payment.observe` 能把退款置为 settled，任何 Agent 无权写 |
| **Evidence Bundle 加 verify.py** | 一条命令重放校验全部 hash、业务对象引用、权威事实归属 |
| **软件域封版** | 场景 1/2/3 保留（真 git apply + 真 pytest 是最强的外部判据），场景 5 replan 移到退款域演示 |
| **HiClaw 压缩** | 从整天压到半天，直接锁 C 档，审批场景改用**退款主管审批**（比批代码补丁有说服力得多） |

**为什么保留软件域不砍**：`sandbox.pytest_run` 的结果是 MAOS 控制不了的外部事实，这本身就是评委要的"外部权威判据"，而且已经写完了。砍掉是净损失。两个域并存，材料里的论证是：

> MAOS 不是为某个行业写的工作流引擎，是领域无关的编排内核。本仓库在两个完全不同的领域上给出可运行实证：软件交付域（外部判据 = 真实测试结果）与制造售后退款域（外部判据 = 支付到账回执）。换域只换 Skill、ToolPort 与业务对象层，`contracts/` 与 `runtime/` 零改动。

---

## 0.A 使用方法

**每个 Phase 开一个 Claude Code 会话**，kickoff prompt 统一用：

```
读取仓库根目录的 CLAUDE.md 和 docs/EXECUTION.md（本手册 v4）。

执行 Phase <N>，严格按其中的步骤、验收标准和禁区执行。

每完成一个步骤跑一次验收命令；全部通过后按提交规范 commit。

遇到与手册冲突或手册没覆盖的决策，停下来问我，不要自行发挥。
```

**全局铁律**（写入 CLAUDE.md，每个会话自动生效）：

1. **冻结契约**：`maos/contracts/events.py` 与 `maos/contracts/states.py` 禁止任何修改。退款域**不许加新状态、不许加新迁移**——它必须用现有状态机跑通，这是"领域无关"的证明，做不到说明抽象错了。
2. `store.py` 现有表结构禁改，只允许**新增**表。退款域的所有表都是新增表。
3. 每个 Phase 结束时：`python -m pytest maos/tests -q` 必须全绿（存量 + 本 Phase 新增）。
4. **证据必须真实**：`evidence/` 下所有文件必须来自真实命令输出，禁止手写或编造。`make_evidence.py` 失败即报错退出，绝不写占位数据。
5. 不做手册范围外的"顺手优化"；发现问题记入 `docs/BACKLOG.md`，不当场改。
6. 提交规范：`feat(p<N>): <一句话>`，一个 Phase 至少一个 commit，验收全绿才许 commit。
7. 任何需要真实密钥的配置只读环境变量，禁止把密钥写进任何文件。
8. **权威事实铁律**：MAOS 不持有权威事实，只持有观察与推断。订单、支付、库存的权威状态永远归属外部系统。任何把外部状态直接写死为终态的代码都是 bug。

**开工前的人肉准备清单**（Claude Code 做不了）：

- LLM API Key（OpenAI 兼容接口，推荐 DashScope/Qwen——阿里系比赛叙事顺）：`MAOS_LLM_BASE_URL` / `MAOS_LLM_API_KEY` / `MAOS_LLM_MODEL`
- Docker Desktop 可用（≥2 核 4GB）
- **售后政策文本**：能从 OPC 人脉拿到一份真实制造企业售后政策最好（哪怕只是政策原文 + 一条脱敏流水，材料可信度换一个量级）；拿不到用附录 B 的模板，但材料里必须写明"政策数据为按行业惯例构造，支付回执与错误码取自真实网关规范"
- 手机/OBS 准备好，D8 录 Demo

---

## 0.B 砍序表（现在写死，贴墙上）

七天里一定会有一个晚上要做取舍，那时候判断力最差。所以顺序现在定，到时候不许现场讨论：

**砍的顺序**

1. `obs/otel.py` 真 span（v3 已标可选）
2. 软件域场景 4
3. PolarDB 上云 → 降级 Docker Postgres+pgvector，材料写"PolarDB PostgreSQL 版兼容，部署仅换连接串"
4. 政策版本对照 case（R6）
5. 渠道对照 case（R4），保留租户对照
6. 软件域场景 3 的 reject 补偿（退款域已有补偿，不重复）

**永不砍**（砍了就不用交了）

- settled guard 及其单测
- RAG 有无对照实验（R5）
- Evidence Bundle + verify.py
- 场景 R1（退款顺利）+ R2（退款失败→补偿）
- 软件域场景 1、2

理由：评委三段建议的核心诉求就三个字——**可核验**。RAG 对照证明"知识影响了规划"，verify.py 证明"这一切能被别人独立重放"，settled guard 证明"你知道权威边界在哪"。其余都是丰满度。

---

## 0.C 排期总表

| Phase | 日期 | 内容 | 状态 |
| :-- | :-- | :-- | :-- |
| P0 | D1 · 8.26 | 仓库重组 + HiClaw 摸底 | 已完成 |
| P1 | D2 · 8.27 | Skill 契约层 + 真模型客户端 + **StorePort 抽象** | 今天 |
| P2 | D3 · 8.28 | 沙箱工具链 + 补全 4 Agent + Gate 读真报告（软件域封版） | |
| P3 | D4 · 8.29 | **退款域（上）**：业务对象层 + 6 Skill + settled guard + 顺利路径 | |
| P4 | D5 · 8.30 | **退款域（下）**：失败路径 + replan + 补偿 + Matrix 主管审批 | |
| P5 | D6 · 8.31 | **RAG 层** + 有无对照实验 + 知识晋升 | |
| P6 | D7 · 9.1 | Trace + Evidence Bundle + **verify.py** + docker-compose | |
| P7 | D8 · 9.2 | 文档 + Demo 脚本 + 提交清单 | |

**文档不要等到 D8**：`scripts/gen_docs.py` 从代码生成的部分（Identity 清单、Skill 目录、ToolPort 契约）在 P1/P3 写完对应代码时顺手生成一次；人写的部分（架构、映射、README）从 P4 开始每天收工写 20 分钟。D8 只做汇总和冒烟。

**落后 ≥ 半天** → 立刻执行 §0.B 砍序表，不拖到明天。

---

## Phase 1（D2 · 8.27）Skill 层 + 真模型客户端 + StorePort

### 目标

Skill 从权限字符串变成实体层；模型换真 LLM；**为业务域和 RAG 预留存储抽象**。

### 步骤

1. `maos/skills/contract.py`：

   - `@dataclass SkillContract`：name / version(semver) / purpose / input_schema / output_schema / preconditions(list[str]) / depends_tools(list[str]) / failure_policy（枚举 `retry|fallback|escalate`，含 max_retries） / security_boundary / reuse_note / owner_roles
   - `@dataclass SkillResult`：status(`ok|failed`) / output / error / duration_ms

2. `maos/skills/registry.py`：`@register_skill` 装饰器；`get(name, version=None)` 默认取最高版本；**保留历史版本**（`dict[name][version]`），这是"发布/回滚"叙事的代码依据。

3. `maos/skills/invoker.py`：`SkillInvoker(identity, store)`，调用流程：

   a. 校验 `skill.name ∈ identity.allowed_skills`，不在 → 抛 `PermissionDenied`
   b. 逐条检查 preconditions
   c. 执行，按 failure_policy 处理失败
   d. 无论成败，`store.append_event_log({... event_type: "SkillInvoked", detail: {skill, version, status, duration_ms, input_digest, output_hash, invocation_id}})`

   **新增要求**：`invocation_id`（uuid4）必须返回给调用方，Phase 3 的 settled guard 要用它做 actor 溯源。用现有 `append_event_log`，不新增 Topic，不碰 `events.py`。

4. 首发 2 个 Skill 到 `maos/skills/builtin/`：

   - `req.normalize v1.0.0`：包住 Requirement 的模型调用
   - `code.repo-patch v1.0.0`：包住 Coding 现有逻辑（路径白名单校验保留在 security_boundary 执行处）

5. 改 `maos/agents/base.py`：`BaseAgent` 持有 `self.skills = SkillInvoker(self.identity, store)`；Coding 的 `run()` 改为经 invoker 调 `code.repo-patch`。Manager 暂不动。

6. `maos/model/client.py` 新增 `OpenAICompatClient`：

   - 只用标准库 `urllib.request`（保持核心零依赖），读三个环境变量
   - 30s 超时，失败重试 1 次，再失败抛异常（上层 Agent 已有 failed 兜底）
   - `maos/main.py` 按环境变量选择：有 key 用真模型，没有回落 `ScriptedModelClient` 并打印醒目提示
   - **`maos/tests/` 一律强制 `ScriptedModelClient`**，测试不许发网络请求

7. **【v4 新增】`maos/store/port.py`：StorePort 抽象**

   这是整个 v4 的地基，做不好后面全塌。定义：

   ```python
   class StorePort(Protocol):
       def execute(self, sql: str, params: tuple) -> None: ...
       def query(self, sql: str, params: tuple) -> list[dict]: ...
       def fts_search(self, table: str, field: str, q: str, limit: int) -> list[tuple[str, float]]: ...
       def vector_search(self, table: str, field: str, vec: list[float], limit: int) -> list[tuple[str, float]]: ...
       def dialect(self) -> str: ...   # "sqlite" | "postgres"
   ```

   - `maos/store/sqlite_store.py`：包住现有 `store.py`，FTS5 做全文，向量用纯 Python 余弦（数据量 <1000 条，够用且零依赖）
   - `maos/store/pg_store.py`：**本 Phase 只写空壳 + NotImplementedError**，P5 再填。留位置就行。
   - `MAOS_STORE_BACKEND` 环境变量选择，默认 sqlite

   **禁区**：不许改现有 `store.py` 的任何方法签名。SQLiteStore 是适配器，不是重写。

8. 新增测试 `maos/tests/test_skills.py`：注册/取版本/越权拒绝/失败策略 retry 生效/SkillInvoked 事件落库/invocation_id 非空，≥6 条。
   新增 `maos/tests/test_store_port.py`：SQLite 后端的 execute/query/fts_search 基本行为，≥3 条。

### 验收

```bash
python -m pytest maos/tests -q                    # 旧 9 条 + 新 ≥9 条全绿
python run.py                                     # 无 key：Scripted 跑通
MAOS_LLM_API_KEY=... python run.py                # 有 key：场景 1 真模型跑通
sqlite3 <db> "select count(*) from event_log where event_type='SkillInvoked'"   # >0
```

### 提交

```
feat(p1): skill contract/registry/invoker + real LLM client + StorePort abstraction
```

---

## Phase 2（D3 · 8.28）沙箱真实工具链 + 补全四个 Agent（软件域封版）

### 目标

补丁是真补丁、测试是真测试；六角色到齐。**本 Phase 结束后软件域封版，后面不再动它。**

### 步骤

1. `maos/tools/port.py`：`@dataclass ToolPort`：tool_name / entrypoint / param_schema / return_schema / scope / retry / idempotency / audit(bool) / degrade(str)。每次调用写 `event_log(event_type="ToolInvoked")`，同样返回 `invocation_id`。

2. `maos/tools/sandbox.py` 两个 ToolPort：

   - `sandbox.git_apply(patch_set, workdir)`：复制 `scenarios/fixture-repo/` 到临时目录 → 逐文件校验路径白名单（沿用 `PROTECTED_PATHS`，`tests/` 禁改）→ `git apply`；失败返回结构化错误（哪个 hunk、什么原因）
   - `sandbox.pytest_run(workdir)`：subprocess 跑 pytest，产出 `{passed, failed, errors, cases:[{id,status,msg}], duration}`；**工具执行失败（环境错）与用例失败（业务错）分开上报**

3. 演示靶场 `scenarios/fixture-repo/`：小 Python 项目，`auth/session.py` 的 `is_session_valid()` 有真实时区 bug（用本地时区导致会话提前过期），`tests/test_session.py` 两条用例：打补丁前 1 挂 1 过，打对补丁后全过。README 说明这是演示靶场。

4. Skill 落地：`test.verify v1.0.0`（调 `sandbox.pytest_run`）。

5. 补全 4 个 Agent（照 `coding.py` 模式：`@register` + Identity + 经 SkillInvoker）：

   - `requirement.py`：经 `req.normalize`，产出 acceptance + open_questions，非空 → status=blocked
   - `architecture.py`：产出 architecture_contract artifact（API/幂等/审计/**回滚字段必填**）
   - `testing.py`：经 `test.verify`，产出 test_report artifact
   - `reviewer.py`：模型语义审查全部产物，产出 review_note；超时 → needs_human

6. 改 `maos/runtime/gate.py::_gate_acceptance`：从读 self_check 改为读同 attempt 的 test_report artifact——无报告 = blocker；有 failed 用例 = major，逐条转结构化 findings。self_check 保留为降级判据。

7. 更新场景 1/2：Plan DAG 变为 `requirement → architecture → coding → testing`（reviewer 挂在 Gate 后、审批前）。场景 2 的返工改为第一轮故意给不完整契约导致真实用例挂，findings 喂回后第二轮修好。

### 验收

```bash
python -m pytest maos/tests -q
MAOS_LLM_API_KEY=... python run.py --scenario 1     # 真补丁 + 真 pytest 全过 → DONE
MAOS_LLM_API_KEY=... python run.py --scenario 2     # 第一轮真实用例挂 → rework → 第二轮 DONE
git -C /tmp/<sandbox-dir> log --oneline             # 真实 apply 记录
```

### 提交

```
feat(p2): sandbox git/pytest toolports, fixture repo, 4 agents, real-report gate
```

---

## Phase 3（D4 · 8.29）退款业务纵切（上）：对象层 + Skill + settled guard

### 目标

证明**同一编排内核能推动一个真实企业业务**。本 Phase 跑通顺利路径。

### 禁区（比别的 Phase 更严）

- `contracts/states.py` **不许加任何新状态、新迁移**。退款流程必须用现有状态机表达（PENDING/RUNNING/AWAITING_REVIEW/BLOCKED/DONE/FAILED + 已有迁移）。做不到就停下来问我，不要自己加状态——退款域的业务状态是**业务对象自己的字段**，不是 Task 状态。这个区分是整个论证的核心。
- 不许改 `store.py` 现有表，只新增。

### 步骤

1. **`maos/domain/refund/schema.sql`：业务对象层（全部新增表）**

   所有表以 `(tenant_id, ...)` 开头，三个维度是主键的一部分而不是配置项：

   ```sql
   -- 租户与渠道
   tenant(tenant_id PK, name, region)
   channel(tenant_id, channel_id, kind, name, PK(tenant_id, channel_id))

   -- 外部系统快照（权威事实归外部，MAOS 只存执行前读到的版本）
   order_snapshot(tenant_id, order_id, version, sku, amount_paid, paid_at,
                  channel_id, policy_version_at_order, payload_json, read_at,
                  PK(tenant_id, order_id, version))
   product_snapshot(tenant_id, sku, version, name, category, warranty_months,
                    payload_json, PK(tenant_id, sku, version))

   -- 政策（带版本与生效范围）
   policy_rule(tenant_id, rule_no, version, title, body, effective_from, effective_to,
               channel_scope, sku_scope, PK(tenant_id, rule_no, version))

   -- 业务案例主体
   refund_case(tenant_id, case_id, channel_id, order_id, order_version, sku,
               reason_code, amount_claimed, biz_status, plan_id, created_at,
               PK(tenant_id, case_id))
   customer_evidence(tenant_id, case_id, evidence_id, kind, uri, digest, submitted_at)
   approval_record(tenant_id, case_id, approver, decision, reason, decided_at)
   finance_entry(tenant_id, case_id, amount_approved, breakdown_json, rule_refs,
                 checked_by, checked_at)
   refund_request(tenant_id, case_id, request_id, amount, gateway,
                  idempotency_key, submitted_at)
   payment_observation(tenant_id, case_id, request_id, gateway_code, raw_receipt_json,
                       observed_state, observed_at, actor_invocation_id)
   notification(tenant_id, case_id, channel, content_digest, sent_at, ack_at)
   compensation_record(tenant_id, case_id, kind, detail_json, executed_at, operator)

   -- DAG/Task/Artifact 到业务对象的引用（不存副本，只存引用）
   business_ref(plan_id, task_id, tenant_id, object_type, object_id, object_version,
                purpose, created_at)
   ```

   `refund_case.biz_status` 枚举严格三段 + 分支：
   `submitted → approved → gateway_accepted → processing → settled`
   分支：`rejected` / `compensated`

2. **`maos/domain/refund/guard.py`：settled guard（永不砍项）**

   ```python
   AUTHORITATIVE_WRITER = "payment.observe"

   def update_biz_status(store, tenant_id, case_id, new_status, actor_skill, invocation_id):
       if new_status in ("settled",) and actor_skill != AUTHORITATIVE_WRITER:
           store.append_event_log({
               "event_type": "AuthoritativeFactViolation",
               "detail": {"case_id": case_id, "attempted": new_status,
                          "actor": actor_skill, "invocation_id": invocation_id}
           })
           raise AuthoritativeFactViolation(...)
       ...
   ```

   实现要点：

   - `settled` 只能由 `payment.observe` 写入，且必须同时插入一条 `payment_observation`（同事务）
   - 任何 Agent 直接调 `store.execute("update refund_case set biz_status='settled'")` 的路径必须被堵死——`refund_case` 的写入统一走 `update_biz_status()`，代码审查时 grep 确认无旁路
   - 复用 `guard_bash.py` 的思路：**违规不是静默失败，是抛异常 + 落事件**，因为"系统拒绝了一次越权写入"本身就是给评委看的证据

3. **6 个退款域 Skill**（`maos/skills/builtin/refund/`）：

   | Skill | 职责 | 关键点 |
   | :-- | :-- | :-- |
   | `refund.intake v1.0.0` | 聚合客户诉求 + 证据，去重，产出 `{case_draft, evidence_refs}` | 复用 `issue.aggregate` 的去重逻辑 |
   | `policy.match v1.0.0` | 读 `order_snapshot.policy_version_at_order` 锁定政策版本 → 检索适用规则 → 裁定 | **按订单快照锁定的版本判定，不用当前最新版本**——这是政策版本对照的技术基础 |
   | `finance.settle v1.0.0` | 计算退款金额，产出 `breakdown_json` + `rule_refs` | 必须写 `finance_entry`，Gate 会查它是否存在 |
   | `payment.execute v1.0.0` | 调 `gateway.refund` ToolPort，写 `refund_request`，把状态推到 gateway_accepted/processing | **不许写 settled** |
   | `payment.observe v1.0.0` | 读回执 → 写 `payment_observation` → 唯一有权写 settled 的 Skill | 观察与推断分离的落点 |
   | `notify.customer v1.0.0` | 通知客户，记 `notification`，等 ack | ack 缺失不阻塞，记 needs_followup |

4. **`maos/tools/gateway.py`：支付网关 ToolPort（时间盒设计）**

   ```python
   class GatewayPort(Protocol):
       def refund(self, request) -> GatewayReceipt: ...
       def query(self, request_id) -> GatewayReceipt: ...
   ```

   两个实现：

   - `MockGateway`：**错误码与异步时序对齐支付宝开放平台退款接口文档**。错误码表由 Claude Code 从官方文档核对后写进 `maos/tools/gateway_codes.py`，禁止凭记忆编造。至少覆盖：系统错误、交易不存在、重复请求不一致、余额不足、渠道繁忙。异步时序：`refund()` 返回 processing，需要 `query()` 轮询才拿到终态。
   - `AlipaySandboxAdapter`：**D5 晚上截止**。通了就切真的，没通就 mock 发车，材料明写"网关适配层已实现，演示环境使用模拟实现，错误码与时序对齐支付宝开放平台文档"。

   这个降级损失比看上去小：评委那句"外部系统保留各自权威事实"考的是**架构有没有把权威边界划对**，不是网关是真是假。settled guard 拿到这个分。

5. **4 个退款域 Agent**（`maos/agents/refund/`，照现有模式，每个都是薄壳：Identity + 经 SkillInvoker 调 1–2 个 Skill）：
   `intake_agent` / `policy_agent` / `finance_agent` / `payment_agent`
   Reviewer 与 Manager **直接复用**，不新写——这正是"角色抽象是配置不是代码"的证明。

6. **Gate 加第六道闸 `_gate_finance`**：`biz_type=refund` 且金额 > 阈值时，无 `finance_entry` = blocker。（P5 的 RAG 对照实验要用这道闸做对比。）

7. **场景 R1（退款顺利路径）**，`run.py --scenario R1`：

   > 入库注：仓库实际入口为 `--scenario 6`，见文件头「入库说明 §1」。

   ```
   多源诉求(工单+客服记录+图片证据)
     → refund.intake（去重聚合）
     → Manager 规划 DAG
     → policy.match（锁 v1 政策，命中 AS- 规则）
     → finance.settle（核算 + finance_entry）
     → Gate 六道闸
     → BLOCKED，等主管审批（P4 接 Matrix，本 Phase 用 CLI 审批）
     → payment.execute（gateway_accepted → processing）
     → payment.observe（读回执 → settled）
     → notify.customer
     → Plan DONE
   ```

8. 新增测试 `maos/tests/test_refund_domain.py`（≥8 条）：

   - finance_agent 试图写 settled → 抛 `AuthoritativeFactViolation` + 事件落库（**这条最重要**）
   - `payment.execute` 后状态是 processing 不是 settled
   - `payment.observe` 写入后状态才是 settled，且 `payment_observation` 同时存在
   - `policy.match` 用订单快照版本而非最新版本
   - `business_ref` 引用完整性
   - 退款域跑完 Task 状态迁移全部落在既有状态机内（断言无新状态）

### 验收

```bash
python -m pytest maos/tests -q
MAOS_LLM_API_KEY=... python run.py --scenario R1     # → Plan DONE
sqlite3 <db> "select biz_status from refund_case"     # settled
sqlite3 <db> "select count(*) from payment_observation"  # >0
sqlite3 <db> "select count(*) from business_ref"      # >0

# 手工验证：
grep -rn "biz_status.*=.*'settled'" maos/ | grep -v guard.py | grep -v observe
#   → 必须无输出（无旁路写入）
```

### 提交

```
feat(p3): refund domain objects, 6 skills, gateway toolport, settled guard
```

---

## Phase 4（D5 · 8.30）退款域（下）：失败路径 + replan + 补偿 + Matrix 审批

### 目标

**失败路径的价值高于顺利路径**——它展示 HITL 和补偿不是 PPT 上的框。同时把审批搬进 Matrix 房间。

### 步骤

1. **补偿回滚**（复用 v3 设计，落到退款域）：

   - architecture_contract 的回滚字段约束在退款域变成：高 effect_risk 的退款任务（金额超阈值 / 渠道为经销商）必须产出 compensation artifact
   - Gate 加 `_gate_compensation`：高 effect_risk 且无 compensation → blocker
   - `control_plane.human_decision(approved=False)` 处：若已提交 `refund_request` 且未 settled → 执行补偿（撤销退款请求 / 转人工工单）→ 写 `compensation_record` + `append_event_log(event_type="CompensationExecuted")` → 走既有 BLOCKED→FAILED(human_reject) 迁移
   - **`states.py` 不加新状态、不加新迁移**

2. **Replan 触发**：在 `on_review_verdict` 的 rework 分支加判定——同一任务第 2 次 rework，或单轮 findings 中 blocker ≥ 2，或**网关返回可重试错误码**（如渠道繁忙）→ Plan RUNNING→PENDING("replan")（已有迁移）→ 冻结未派发任务 → Manager 带全部 findings 重规划 → `start_plan` 重启。

   **关键**：无限重试是评委点名的反模式。replan 必须有上限（`MAOS_MAX_REPLAN=2`），超限 → needs_human，绝不自旋。

3. **场景 R2（退款失败 → 补偿）**，`run.py --scenario R2`：

   > 入库注：仓库实际入口为 `--scenario 7`，见文件头「入库说明 §1」。

   ```
   诉求聚合 → 政策裁定通过 → 财务核算 → 主管审批通过
     → payment.execute → 网关返 <可重试错误码>（processing 卡住）
     → payment.observe 轮询超时 → findings
     → replan（第 1 次）→ 换渠道重试 → 仍失败
     → 达 replan 上限 → needs_human → BLOCKED
     → 主管在 Element 里 /reject <task_id> 渠道异常，转人工
     → 补偿执行：撤销 refund_request + 写 compensation_record + 转人工工单
     → Plan FAILED(human_reject)
     → refund_case.biz_status = 'compensated'，**从未进入 settled**
   ```

   最后一行是整个场景的题眼：**"所有 Agent 都回复完成"没有发生，因为业务确实没成功，系统如实记录了这一点。**

4. **HiClaw / Matrix 对接（压缩版，半天，直接锁 C 档）**

   - `hiclaw/matrix_bus.py`：`MatrixEventBus(inner_bus, config)` 装饰器包住现有 EventBus
     - `publish()` 先走 inner，再镜像进 Matrix 房间：一行人话摘要 + 折叠的 Envelope JSON
     - 状态迁移镜像：外挂 event_log 轮询器把 StateTransition 也发进房间——**不改 `control_plane.py`**
     - 监听 `/approve <task_id>` 与 `/reject <task_id> [原因]` → 调 `HumanApprovalQueue.decide()`；只接受 `MAOS_APPROVERS` 名单内用户，其余回"无审批权限"并记 event_log
     - 依赖 matrix-nio；**连接失败自动降级 log-only**，场景照跑，测试和 CI 永远用降级模式
   - **房间来源直接锁 C 档**，不做 A/B 探索：`docker run` 官方 Synapse，注册 maos-bot + 你，建房。半小时的事。真起不来退 matrix.org 私密房间。
   - `docs/hiclaw-probe.md` 补一行：最终选了哪档、为什么（时间盒决策，非技术受限）
   - `run.py --matrix` 开关

5. 新增测试（≥6 条）：MatrixEventBus 降级模式行为与 inner bus 完全一致；审批命令解析（合法/非法/越权）；replan 触发三种边界 + 上限生效；reject 触发补偿且 `compensation_record` 落库；补偿后 `biz_status='compensated'` 且**无 settled 记录**。

### 验收

```bash
python -m pytest maos/tests -q

MAOS_LLM_API_KEY=... python run.py --scenario R1 --matrix
# Element 里看到全过程；发 /approve → DONE。截图存 evidence/room/（01–03，命名见该目录 README）

MAOS_LLM_API_KEY=... python run.py --scenario R2 --matrix
# 网关失败 → replan → 达上限 → /reject → 补偿。截图存 evidence/room/（04-reject-compensation.png）

sqlite3 <db> "select biz_status from refund_case where case_id='<R2 的 case>'"   # compensated
sqlite3 <db> "select count(*) from payment_observation where observed_state='settled'"  # 0
sqlite3 <db> "select count(*) from event_log where event_type='CompensationExecuted'"   # >0
```

🔴 **截图落点不许写成 `evidence/scenario-R1/` / `evidence/scenario-R2/`（这两行原来就是那么写的，是错的）。**
`scripts/verify.py::load_cases` 按 **`scenario-` 前缀**扫 `evidence/` 下的目录——只认前缀，不认场景号——
凡是扫到的都当成一个证据束，逐个要求 `maos.db` + `trace.json` + `result.json`。截图目录这三样一样没有，
于是**整个 verify 进不去核验**（不是某一项 FAIL，是七项一项都跑不到，「7/7 PASS」这条头号卖点当场没）。
本仓库 `1131795` 实测，先 `python3 scripts/make_evidence.py` 再 `python3 scripts/verify.py`：

```
不建 scenario-R1/                    RESULT: 7/7 PASS                                    exit=0
建 scenario-R1/ 只放一张截图         [FAIL] 无法开始核验：缺数据库: …/scenario-R1/maos.db   exit=2
```

`evidence/room/` 不以 `scenario-` 开头，对 `verify.py` 完全透明，所以房间侧人机交互证据走它。
机器侧数据证据仍在 `evidence/scenario-6,7/`（R1→6、R2→7，见文件头「入库说明 §1」的编号映射），
两者互补、缺一不可，对照关系见 `evidence/room/README.md` 的「命名对照」一节。

### 提交

```
feat(p4): refund failure path, replan with cap, compensation, matrix approval
```

---

## Phase 5（D6 · 8.31）PolarDB RAG 层 + 对照实验

### 目标

证明**历史流程知识能改善规划质量**，且这个改善是可核验的（不是"我们接了 RAG"一句话）。

### 步骤

1. **`maos/kb/schema.sql`：知识表**

   ```sql
   kb_doc(tenant_id, doc_id, biz_type, channel_id, region, sku, policy_version,
          workflow_version, rule_no, gateway_code, kind, title, body,
          embedding, outcome, source_case_id, created_at,
          PK(tenant_id, doc_id))
   -- kind: policy | history_case | failure_hint | error_code_playbook
   -- outcome: success | failed | null
   ```

2. **`maos/kb/retriever.py`：两阶段检索**

   **阶段一 · 结构化预过滤**（SQL where，按评委给的顺序）：
   `tenant_id → biz_type → channel_id → region → sku → policy_version → workflow_version`

   过滤是硬约束不是打分项——跨租户的知识**永远不能**被召回，这条要有单测。

   **阶段二 · 混合召回**（在候选集内）：

   | 通道 | 实现 | 默认权重 |
   | :-- | :-- | :-- |
   | 规则编号精确 | `rule_no = ?` | 0.35 |
   | 支付错误码精确 | `gateway_code = ?` | 0.25 |
   | 全文 BM25 | SQLite FTS5 / PG tsvector | 0.20 |
   | 语义向量 | 纯 Python 余弦 / pgvector | 0.20 |

   加权融合排序，权重可配（`MAOS_KB_WEIGHTS`）。

   **命中的 `doc_id` 和分数必须写进 `event_log(event_type="KbRetrieved")`**，并进 Evidence Bundle。这是整个 RAG 部分能不能得分的关键——比检索质量本身重要得多。检索不准评委顶多说效果一般，无法核验就是零分。

3. **`maos/store/pg_store.py` 填实**：tsvector + pgvector，连接串走 `MAOS_PG_DSN`。

   - 开发用 `docker run` 的 `pgvector/pgvector:pg16`
   - `deploy/polardb.md` 写一页：PolarDB PostgreSQL 版建库、装 pgvector 扩展、改连接串，**仅此三步**
   - 时间够就实际连一次 PolarDB 跑通场景 R1 并截图；不够就按砍序表第 3 条降级，材料写"兼容验证待复赛后执行"

4. **Planner 接入**：Manager 规划前先 `kb.retrieve`，检索结果进 prompt 上下文，作为"建议任务 / 建议审批人 / 已知异常分支"。

   **护栏**（评委原话"历史流程只能帮助规划，不能替代当前订单事实和人工授权"）：

   - 检索结果只能**增加**任务，不能删除必要任务
   - 检索结果**不能**替代 `order_snapshot` 的事实判定
   - 检索结果**不能**跳过任何人工审批

   这三条写成 `kb/guardrails.py` 的断言，违反抛异常。

5. **场景 R5：RAG 有无对照实验（永不砍项）**

   > 入库注：R5 的整数编号**尚未裁决**（D-05 只裁决了 R1→6、R2→7）。P5 开工前需先裁决，见文件头「入库说明 §1」。

   同一个 case，跑两次：

   ```bash
   MAOS_KB_ENABLED=0 python run.py --scenario R5   # Planner 漏掉财务复核 → Gate _gate_finance 判 blocker
   MAOS_KB_ENABLED=1 python run.py --scenario R5   # 命中历史案例 → 规划时补上财务复核 → 一次通过
   ```

   `scripts/make_evidence.py` 自动跑这两次并 diff 两版 DAG，产出 `evidence/scenario-R5/dag-diff.json`：

   ```json
   {
     "without_kb": {"tasks": ["..."], "gate_result": "blocker", "rework_count": 1},
     "with_kb":    {"tasks": ["..."], "gate_result": "pass",    "rework_count": 0},
     "delta_tasks": ["finance.settle"],
     "triggering_docs": [{"doc_id": "...", "score": 0.81, "title": "..."}]
   }
   ```

   **这一个对照实验，抵得上把七个过滤维度全部实现。**

6. **知识晋升（手动，不做调度器）**：

   - `kb.sink v1.0.0`：Plan 终态时把 findings + verdicts 复盘成 1–3 条写入
   - **晋升规则按评委原话**：只有**证据完整且外部结果明确**的案例（有 `payment_observation` 且 `observed_state='settled'` 且有客户 ack）才进 `kind='history_case'`, `outcome='success'` 默认知识层；失败实例进 `kind='failure_hint'`, `outcome='failed'`，只用于提示"哪类渠道 / 支付返回 / 政策组合需要额外步骤"，**不作为规划正例**
   - PlanFinalizer 轮询到终态后调 `kb.sink`——**不把模型调用塞进 Control Plane**
   - 自动晋升调度器写进 `docs/BACKLOG.md`，不实现

7. **对照 case 数据**（`scenarios/refund/`，每个维度只造一个反例）：

   | 维度 | 对照设计 | case |
   | :-- | :-- | :-- |
   | 租户 | 同商品同诉求，租户 A 命中 30 天无理由，租户 B 命中 7 天 → 一通过一驳回 | R3a / R3b |
   | 渠道 | 同商品，自营 vs 经销商 → 经销商多一个渠道商核销任务、审批人不同 | R4a / R4b |
   | 政策版本 | 下单时 v1，执行时已升 v2 → 按订单快照锁定的 v1 判定 | R6 |

   按砍序表，R6 先砍，R4 次之，租户对照 R3 最后砍。

8. 新增测试（≥8 条）：跨租户绝不召回（**最重要**）；四通道各自命中；加权融合排序正确；KbRetrieved 事件含 doc_id 与分数；三条护栏各自生效；失败案例不进正例知识层。

### 验收

```bash
python -m pytest maos/tests -q
MAOS_KB_ENABLED=0 python run.py --scenario R5   # blocker
MAOS_KB_ENABLED=1 python run.py --scenario R5   # pass
cat evidence/scenario-R5/dag-diff.json | jq .delta_tasks    # ["finance.settle"]
sqlite3 <db> "select event_type, json_extract(detail,'$.docs') from event_log where event_type='KbRetrieved'"
MAOS_STORE_BACKEND=postgres MAOS_PG_DSN=... python run.py --scenario R1   # PG 后端跑通
```

### 提交

```
feat(p5): two-stage kb retriever, pg/pgvector backend, rag ablation scenario, knowledge promotion
```

---

## Phase 6（D7 · 9.1）Trace + Evidence Bundle + verify.py + 部署

### 目标

一条命令起全套，一条命令验真伪。**verify.py 是给评委的答案。**

### 步骤

1. `maos/obs/trace.py`：`export_trace(plan_id) -> trace.json`——从 event_log 把该 plan 的事件按 trace_id 织成 span 树（StateTransition / SkillInvoked / ToolInvoked / KbRetrieved 各成 span，parent 按 task 归属），字段对齐 OTel 语义（trace_id/span_id/parent_span_id/name/start/end/attributes）。

2. `maos/obs/otel.py`（可选，时间不够直接跳过，见砍序表第 1 条）。

3. **`scripts/make_evidence.py`**：一键跑全部场景，每场景产出 `evidence/scenario-<N>/`：

   - `run.log`（完整 stdout）
   - `trace.json`
   - `result.json`（终态 + 关键指标：耗时 / rework 次数 / replan 次数 / 事件数 / **business_outcome**）
   - `business-objects.json`（本 case 引用的全部业务对象及版本号）
   - `kb-hits.json`（RAG 命中条目与分数）
   - `kb-dump.json`（本轮沉淀的知识条目）
   - 场景 R1/R2 留 `SCREENSHOT-HERE.md` 提示放 Element 截图

   **脚本失败即报错退出，绝不写占位假数据。**

4. **`scripts/verify.py`（永不砍项）**——一条命令重放校验，逐项输出 PASS/FAIL：

   | # | 校验项 | 失败意味着 |
   | :-- | :-- | :-- |
   | 1 | 每个 task 的 input_digest/output_hash 与 event_log 一致 | 证据被篡改或事后手写 |
   | 2 | 每条 business_ref 指向的对象在库中存在且 version 匹配 | 引用悬空，业务锚点是假的 |
   | 3 | **每个 settled 都有对应 payment_observation 且 actor_invocation_id 属于 payment.observe** | 权威事实边界被绕过 |
   | 4 | trace.json span 树无孤儿、无环 | 事件链不完整 |
   | 5 | 每个 KbRetrieved 的 doc_id 在 kb_doc 中存在 | RAG 命中是编的 |
   | 6 | 每个 Plan 终态都有对应 business_outcome，且 DONE 必须有外部判据（测试通过 / 退款到账） | "Agent 都完成了"被当成业务成功 |
   | 7 | 每条 history_case 知识都能追溯到一个 outcome='success' 的真实 case | 知识层被污染 |

   ```bash
   python scripts/verify.py --evidence evidence/ --db <db>
   # [PASS] hash-integrity        142/142
   # [PASS] business-ref          38/38
   # [PASS] authoritative-fact    3/3
   # ...
   # RESULT: 7/7 PASS
   ```

   README 里把这条命令放在最显眼的位置。**评委能自己跑一遍，这就是"可核验"的全部含义。**

5. `deploy/docker-compose.yml`：maos 服务 + pgvector 服务 + 注释指引 Synapse 怎么并排起；`deploy/.env.example` 全量样例；`deploy/polardb.md` 三步迁移说明。

6. `.gitignore` 确认：`evidence/` 提交（它就是交付物），沙箱临时目录 / 数据库文件不提交。

### 验收

```bash
MAOS_LLM_API_KEY=... python scripts/make_evidence.py   # 全部场景目录齐
python scripts/verify.py --evidence evidence/ --db <db>  # 7/7 PASS
docker compose -f deploy/docker-compose.yml up          # 容器内场景 R1 跑通
python -m pytest maos/tests -q
```

### 提交

```
feat(p6): otel-aligned trace, evidence generator, verify.py, docker-compose + polardb guide
```

---

## Phase 7（D8 · 9.2）文档 + 提交材料

前面几个 Phase 已经边做边写，本日只做汇总和冒烟。

### 步骤

1. `scripts/gen_docs.py` 生成（保证文档和代码永不打架）：

   - `docs/agent-identity.md`：**十一角色**清单（软件域 6 + 退款域 5），从代码 Identity 生成，按参赛手册附录 A 字段顺序。
     ⚠️ 「十一」是**带 Identity 的角色总数**，与代码里 `len(AGENT_POOL) == 10` 不矛盾——这是两个数、两件事：
     11 个角色都有 Identity（白名单同样被 `SkillInvoker` 强制），其中 **10 个注册进 `AGENT_POOL`**（Worker 收到
     TaskAssignment 后按 role 找得到的执行者），`manager` 是规划者、由流程层直接构造并调 `plan()`、不接派单，
     所以有 Identity 但不进池。软件域 6 = 5 个可派单（architecture / coding / requirement / reviewer / testing）+ manager。
     生成物自己 `docs/agent-identity.md:7` 会如实印出「11 个 / 10 个 / 1 个」并解释差在哪，**引用时别把 10 直接挂在 `AGENT_POOL` 后面**
   - `docs/skill-catalog.md`：全部 Skill × 九要素，从 SkillContract 实例生成；末尾加"版本 / 发布 / 回滚 / 质量评估"一节
   - `docs/toolport-contract.md`：ToolPort 九要素 + 已实现工具契约表 + "迁移到 MCP = 换 entrypoint 传输层，schema 与审计不变"

2. 人写：

   - `docs/domain-portability.md`（**v4 新增，最重要的一份**）：一张表列出软件域与退款域的对照——同样的 Control Plane、同样的状态机、同样的 Gate、同样的补偿与 replan，不同的只有 Skill / ToolPort / 业务对象。附 `contracts/` 与 `runtime/` 在两个域上线前后的 `git diff --stat`（应为零）。
   - `docs/authoritative-facts.md`：权威事实边界说明 + settled guard 代码链接 + verify.py 第 3 项校验说明
   - `docs/agentteams-mapping.md`：五项映射表，每行补代码文件 + 行号，说明最终采用哪档
   - `docs/architecture.md`：mermaid 图 + 数据流

3. 重写根 `README.md`，顺序：
   业务场景故事（**从退款 case 讲起，不要从"AI 写代码"讲起**）→ 架构图 → verify.py 一条命令 → 5 分钟快速开始（含无 key 的 Scripted 模式，评审没有 key 也能跑）→ 场景说明表 → evidence 索引 → 安全边界 → 与提案 / 比赛要求的映射索引

4. `docs/demo-script.md`：Demo 分镜（3–5 分钟，**R2 失败路径为主线**）：

   - 00:00 多源诉求聚合，Element 房间里看拆解
   - 00:45 政策裁定命中规则编号 + RAG 命中历史案例（特写 kb-hits.json）
   - 01:30 财务核算 → Gate 六道闸 → BLOCKED
   - 02:00 主管在 Element 里看到审批卡，/approve
   - 02:30 支付执行 → 网关返错误码 → replan → 达上限 → needs_human
   - 03:15 /reject → 补偿执行 → biz_status=compensated，**特写：从未进入 settled**
   - 04:00 `python scripts/verify.py` → 7/7 PASS
   - 04:30 一屏总结：同一内核，两个域

   每个镜头标注要执行的确切命令。

5. `docs/submission-checklist.md`：复赛三件套自查（PPT 逐页 ↔ 评审四维对照、仓库链接自查、视频规格），全部打勾才提交。

6. **新克隆冒烟**：git clone 到全新目录，严格按新 README 从零跑场景 R1（Scripted 模式）+ verify.py，掐表 ≤15 分钟，过不了就改 README 直到过。

### 验收

新克隆冒烟通过；`python scripts/gen_docs.py --check` 文档与代码一致；`ls docs/` 七份齐。

### 提交

```
docs(p7): domain portability, authoritative facts, mapping, demo script, README rewrite
```

---

## 附 A：每日收工检查单

- `python -m pytest maos/tests -q` 全绿
- `python run.py`（Scripted 模式）老场景没被改坏
- `git diff --stat maos/contracts/` 为空（冻结契约未被动过）
- `grep -rn "biz_status.*settled" maos/ | grep -v guard.py | grep -v observe` 无输出
- 今日 Phase 的验收命令逐条跑过
- commit 已按规范落盘，推送远端
- 落后 ≥ 半天？→ 打开 §0.B 砍序表执行，不拖明天

---

## 附 B：政策数据构造模板（拿不到真实政策时用）

构造原则：**业务数据可以合成，外部系统的返回必须真实或对齐真实规范。**

四类规则，每类给两个租户不同版本：

| 规则编号 | 标题 | 关键差异点 |
| :-- | :-- | :-- |
| AS-001 | 无理由退货期 | 租户 A：30 天；租户 B：7 天 |
| AS-002 | 质保期内质量问题 | 按 `product_snapshot.warranty_months` |
| AS-003 | 人为损坏免责 | 需 `customer_evidence` 中有图片证据 |
| AS-004 | 渠道差异 | 经销商渠道需增加"渠道商核销"任务，审批人为区域经理 |

每条规则两个版本（v1 / v2，`effective_from` 不同），供政策版本对照 case 使用。

历史案例 20–30 条，其中约 1/3 标 `outcome='failed'`，覆盖：渠道繁忙、余额不足、交易不存在、重复请求不一致、客户拒收退款。

**材料里必须明写数据来源与口径**，不许把合成数据说成真实数据——这是最容易被评委问穿、也最伤的一处。

---

## 附 C：与评委三段建议的逐条对照（写材料时直接用）

| 评委要求 | v4 落点 | 可核验证据 |
| :-- | :-- | :-- |
| 用一条脱敏真实退款需求完成可执行纵向切片 | Phase 3–4，场景 R1 / R2 | `evidence/scenario-6,7/`（R1→6、R2→7，见文件头「入库说明 §1」）+ `evidence/room/` |
| AgentTeams 事件链 | Phase 4 MatrixEventBus 镜像 | Element 截图 + trace.json |
| 关键 Skill 的真实调用 | 退款域 6 Skill 全部真调 | event_log 中 SkillInvoked |
| 返工 / HITL Trace | Phase 4 replan + Matrix 审批 | `evidence/scenario-7/trace.json`（R2 即场景 7） |
| Evidence Bundle | Phase 6 | verify.py 7/7 PASS |
| 业务对象关联到同一案例 | Phase 3 business_ref | verify.py 第 2 项 |
| 外部系统保留权威事实，区分已提出 / 处理中 / 已到账 | Phase 3 settled guard + 三段 biz_status | verify.py 第 3 项 + 越权拒绝单测 |
| PolarDB RAG 面向 workflow 规划 | Phase 5 两阶段检索 | kb-hits.json |
| 先按租户 / 业务 / 地区 / 渠道 / 商品 / 政策 / 版本过滤，再组合规则编号、错误码、全文、语义 | Phase 5 阶段一 + 四通道融合 | 跨租户不召回单测 |
| 减少遗漏财务复核、错误套用政策、无限重试 | `_gate_finance` + 政策版本锁定 + replan 上限 | 场景 R5 对照 |
| 历史流程不能替代当前订单事实和人工授权 | `kb/guardrails.py` 三条断言 | 护栏单测 |
| 以退款到账 / 客户确认 / 人工纠错验证 DAG | result.json 的 business_outcome | verify.py 第 6 项 |
| 只有证据完整且外部结果明确的案例进默认知识层 | Phase 5 晋升规则 | verify.py 第 7 项 |

---

**v4 结束。9.3 只做提交，不写代码。**
