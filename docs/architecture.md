# 架构与数据流

一句话：**Control Plane 是唯一的状态持有者，其余全是可插拔件。**

Agent 不改状态，只产出；Gate 不改状态，只出判定；Skill 不碰外部世界，
碰外部世界的一律经 ToolPort；外部系统的终态不进 MAOS 的库，只以「观察」的形式进。

---

## 1. 分层

```mermaid
flowchart TB
  subgraph FROZEN["冻结契约（禁改 · maos/contracts/）"]
    EV["events.py<br/>Envelope / Topic / EventType"]
    ST["states.py<br/>TaskState / PlanState / 迁移表"]
  end

  subgraph KERNEL["领域无关内核（换域零改动）"]
    CP["Control Plane<br/>maos/core/control_plane.py<br/>唯一的状态迁移持有者"]
    BUS["EventBus<br/>maos/core/eventbus.py"]
    STORE["Store<br/>maos/core/store.py<br/>plan/task/artifact/event_log/…"]
    WK["Worker Runtime<br/>maos/runtime/worker.py<br/>Identity 三查"]
    GATE["Reviewer Gate<br/>maos/runtime/gate.py<br/>六道闸"]
    FIN["Plan Finalizer<br/>maos/runtime/plan_finalizer.py"]
  end

  subgraph PLUG["按域实现（新增文件，不改内核）"]
    AG["Agents<br/>maos/agents/**<br/>软件域 6 + 退款域 4"]
    SK["Skills<br/>maos/skills/builtin/**<br/>九要素契约"]
    TL["ToolPorts<br/>maos/tools/**<br/>sandbox · gateway"]
    DM["业务对象<br/>maos/domain/refund/**<br/>14 张新表 + settled guard"]
  end

  subgraph SIDE["旁路（可缺席，不阻塞主链路）"]
    MX["MatrixEventBus<br/>hiclaw/matrix_bus.py<br/>镜像 + 房间审批"]
    KB["知识层 / RAG<br/>maos/kb/**<br/>两阶段检索 + 三条护栏"]
    OBS["Trace<br/>maos/obs/trace.py<br/>OTel 对齐 span 树"]
  end

  EV -.契约.-> KERNEL
  ST -.契约.-> KERNEL
  CP <--> BUS
  CP --> STORE
  BUS <--> WK
  BUS <--> GATE
  WK --> AG
  AG --> SK
  SK --> TL
  SK --> DM
  GATE --> STORE
  FIN --> STORE
  BUS -.装饰器包一层.-> MX
  AG -.规划期检索.-> KB
  STORE -.重放.-> OBS
```

**读法**：`FROZEN` 与 `KERNEL` 两块在退款域上线前后 `git diff` 为零
（`maos/core/` 空、`maos/runtime/` 只有第六道闸的 +126 行），
论证见 [`domain-portability.md`](domain-portability.md)。

---

## 2. 一个任务的生命周期

```mermaid
sequenceDiagram
  participant M as Manager<br/>（规划者，不进 AGENT_POOL）
  participant CP as Control Plane
  participant B as EventBus
  participant W as Worker Runtime
  participant A as Agent（按 role 取）
  participant S as Skill / ToolPort
  participant G as Reviewer Gate
  participant H as 人（Element 房间）

  M->>CP: create_plan(DAG)
  CP->>B: TaskAssignment（依赖已满足的任务）
  Note over CP: PENDING → DISPATCHED
  B->>W: 派发
  W->>A: TaskContext（只看得到本任务）
  Note over W: Identity 三查<br/>工具白名单 / 风险级 / 写资源
  Note over CP: DISPATCHED → RUNNING
  A->>S: SkillInvoker.invoke()
  Note over S: 落 SkillInvoked<br/>（input_digest / output_hash）
  S-->>A: SkillResult
  A-->>W: AgentOutput（artifacts）
  W->>B: TaskResult
  Note over CP: RUNNING → AWAITING_REVIEW
  B->>G: 轮询 AWAITING_REVIEW
  G->>G: 六道闸<br/>schema→acceptance→security→evidence→compensation→finance
  alt 全过 且 effect_risk ≠ H
    G->>B: ReviewVerdict(pass)
    Note over CP: AWAITING_REVIEW → DONE
  else 有 finding
    G->>B: ReviewVerdict(rework + 结构化 findings)
    Note over CP: → REWORK → 重新派发（带 findings）
  else effect_risk = H
    G->>B: ReviewVerdict(needs_human)
    Note over CP: → BLOCKED
    H->>CP: /approve 或 /reject
    Note over CP: BLOCKED → DONE / FAILED
  end
```

三条不变量：

1. **只有 Control Plane 改状态。** Agent、Gate、Skill 一律通过发事件请求迁移，
   自己不写 `task.state`。迁移合法性查 `maos/contracts/states.py` 的迁移表。
2. **每一次迁移落一条 `event_log`。** 这是 Trace 与证据束唯一的数据来源 ——
   没有第二份记录，也就没有「日志和库对不上」这种事。
3. **幂等与版本冲突在 Control Plane 一处收口。** 重复投递同一个 `TaskResult`
   不产生第二次迁移（场景 4 就在验这个），换 MQ 的前提就是这一条。

---

## 3. 数据流：一条退款 case 从诉求到收口

```mermaid
flowchart LR
  IN["多源诉求<br/>工单 + 客服记录 + 图片证据"] --> INTAKE["refund.intake<br/>（复用 issue.aggregate 去重）"]
  INTAKE --> CASE[("refund_case<br/>biz_status=submitted")]
  INTAKE --> PLAN["Manager 规划 DAG<br/>（零改动复用）"]
  PLAN -.可选.-> RAG["两阶段检索<br/>结构化预过滤 + 混合召回"]
  PLAN --> POLICY["policy.match<br/>按订单锁定的政策版本"]
  POLICY --> FINANCE["finance.settle<br/>写 finance_entry"]
  FINANCE --> GATE6{"第六道闸<br/>_gate_finance"}
  GATE6 -->|无凭据| BLOCK["blocker → rework"]
  GATE6 -->|有凭据| APPROVE{"effect_risk=H<br/>停 BLOCKED"}
  APPROVE -->|主管 /approve| PAY["payment.execute<br/>→ gateway.refund"]
  APPROVE -->|主管 /reject| COMP
  PAY --> OBS["payment.observe<br/>→ gateway.query 轮询"]
  OBS -->|问出 settled| SETTLED[("biz_status=settled<br/>+ payment_observation<br/>同事务")]
  OBS -->|问不出终态| NOTHING["什么都不写<br/>任务 FAILED"]
  NOTHING --> COMP["refund.compensate<br/>撤销请求 + 开人工工单"]
  COMP --> COMPED[("biz_status=compensated<br/>从未进入 settled")]
  SETTLED --> NOTIFY["notify.customer<br/>ack 未确认不阻塞"]
```

**题眼在 `OBS` 那个分叉**：问不出终态时，系统**什么都不写** —— 不猜、不推断。
详见 [`authoritative-facts.md`](authoritative-facts.md)。

---

## 4. 证据是怎么长出来的

```mermaid
flowchart LR
  RUN["python3 run.py --scenario N"] --> ELOG[("event_log<br/>每次迁移一行<br/>SkillInvoked / ToolInvoked")]
  ELOG --> MK["scripts/make_evidence.py<br/>子进程跑 + 落库到文件"]
  MK --> BUNDLE["evidence/scenario-N/<br/>run.log · result.json · trace.json<br/>business-objects.json · kb-*.json · maos.db"]
  BUNDLE --> VER["scripts/verify.py<br/>七项重放校验"]
  VER --> OUT["PASS / FAIL / SKIP<br/>逐项，非零退出"]
```

三条纪律（铁律 3 / 铁律 6）：

- 每个证据文件首行 `# generated at <ISO8601> from <git sha>`，生成脚本自动写入；
  工作区不干净时 sha 带 `-dirty` 后缀。
- **上游命令失败即报错退出，绝不写占位数据。** 先在临时目录攒齐、全部成功才挪到位；
  中途失败连临时目录一起删 —— 宁可目录缺一半，也不许留半份看起来像跑通了的产物。
- **脱敏走两道**：写入时把敏感环境变量的值替换成 `***REDACTED:<VAR>***`，
  写完再拿这些值当哨兵把整个目录反查一遍，命中即销毁目录并失败。

---

## 5. 存储

| 表 | 归属 | 说明 |
| :-- | :-- | :-- |
| `plan` / `task` / `artifact` / `event_log` / `processed_key` | 内核（Phase 0） | **DDL 被 `.contracts.lock` 指纹锁死**，禁改 |
| `knowledge` | 内核（Phase 4 新增表） | 复盘条目 |
| `kb_doc` | 知识层（`maos/kb/`） | 结构化知识：政策 / 历史案例 / 失败提示 / 错误码手册 |
| 退款域 14 张 | `maos/domain/refund/schema.sql` | `refund_case` / `refund_request` / `payment_observation` / `finance_entry` / `business_ref` / … |

后端可插拔：`maos/store/port.py` 定义 `StorePort`，`sqlite_store.py` 是默认实现，
`pg_store.py` 是 Postgres / PolarDB 分支的**空壳**（当前一律
`NotImplementedError`，`maos/store/pg_store.py:45`）。**空壳阶段就定死了一条：
选 postgres 后端一律抛错，绝不静默回落 sqlite**（`maos/store/__init__.py:70`）——
回落是这类后端最容易出的无症状错误，先把口子堵上，再谈填实现。

⚠️ 如实说明：`maos/store/**` 是纯新增包，**主链路目前一个 import 都没接**
（`maos/flows/common.py::build()` 里仍是写死 `:memory:` 的 `SqliteStore`）。
所以「后端可插拔」当前是**有地基、未接线**，已记 `docs/BACKLOG.md ## task-W2`。

---

## 6. 模型在哪里、不在哪里

- **在**：Agent 的语义工作（需求归一、架构契约、写补丁、语义审查、政策条款匹配）。
- **不在**：状态迁移、Gate 判定、replan 触发、审批、补偿、权威事实写入。

> **replan、补偿、审批是控制面行为，其正确性不得依赖模型的智力表现。**

所以 Gate 是**规则驱动**而不是模型驱动（`maos/runtime/gate.py:6` 写明），
需要模型参与的语义审查走 Reviewer Agent，挂在 Gate 之后、审批之前。

无 key 也能完整跑：`ScriptedModelClient` 按关键字返回预置应答，场景 1–7 与全部测试
恒走这条路 —— 演示不依赖任何外部 API。
