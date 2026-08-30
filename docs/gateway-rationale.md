# 推荐工具链口径 —— 八个组件用没用、为什么、接的话改哪里

这份文档回答赛题「推荐工具链与资源使用要求」那一条：

> 推荐项目和产品**不按使用数量评分**，评审重点不在于「堆工具」，而在于参赛方案
> 是否说明清楚设计理念、接口契约、必要性、可观测性、权限边界、端到端评估证据
> **和迁移路径**。替代方案可以使用，但**需说明理由、兼容性、替换原因与迁移路径**。

所以这里**不论证「我们用了多少」**，只论证每一个组件的四件事，顺序固定：

| # | 问题 | 判据要求 |
| :-- | :-- | :-- |
| ① | **用没用** | 三态之一，见下 |
| ② | **为什么** | 必要性或不必要性，要指得出仓库里的出处 |
| ③ | **等价机制是什么** | 没用的话，同样的能力现在靠什么兜住 |
| ④ | **接的话改哪一个文件** | 迁移点唯一性 —— 说不出唯一替换点的，抽象就是没做对 |

## 三态的定义（写死，不许模糊）

| 态 | 含义 | 门槛 |
| :-- | :-- | :-- |
| ✅ **已接通实测** | 真跑过，输出落进 `evidence/` 或文档里有可复核的实录 | 有命令、有输出、有 sha |
| 🟡 **接口预留未接** | 替换点已存在且唯一，但**没跑过** | 必须写明「这是接口层面的推论，不是已跑通的事实」 |
| ❌ **完全没碰** | 代码里没有对应实现 | 必须说清是「评估后不接」还是「本轮未评估」 |

🔴 **纪律：指不出证据的断言就删掉，不留着凑篇幅**（`artifacts/README.md` §3③）。
本文档里凡是推论，都会当场标成推论 —— 混着写就等于把「跑通了」的份量借给没跑过的东西。

## 一眼总表

| # | 组件 | 状态 | 等价机制 | 迁移点（唯一替换处） |
| :-- | :-- | :-- | :-- | :-- |
| 1 | [AgentTeams / HiClaw(Matrix)](#1-agentteams--hiclawmatrix) | ✅ 已接通实测 | —（旁路降级 `log_only`） | `hiclaw/matrix_bus.py` + 四个环境变量 |
| 2 | [PolarDB for PostgreSQL](#2-polardb-for-postgresql) | ✅ 部分实测 | SQLite 缺省后端 | `maos/store/__init__.py:62` + `MAOS_STORE_BACKEND` |
| 3 | [RocketMQ](#3-rocketmq) | 🔒 T27 接入中 | 见冻结节 | 见冻结节 |
| 4 | [Nacos](#4-nacos) | 🔒 T28 接入中 | 见冻结节 | 见冻结节 |
| 5 | [Higress](#5-higress) | ❌ 本轮不接（已评估） | `GatewayModelClient` 客户端侧五道 | `maos/model/client.py` 一个文件 |
| 6 | [UnifiedModel](#6-unifiedmodel) | ❌ **本轮未评估** | `ModelClient` 抽象 + 单一构造入口 | `maos/model/client.py` 一个文件 |
| 7 | [LoongSuite / AgentScope Studio / AgentLoop](#7-loongsuite--agentscope-studio--agentloop) | ❌ 未接 | 自研 `maos/obs/trace.py`（字段对齐 OTel） | `maos/obs/trace.py` 加 exporter |
| 8 | [MCP](#8-mcp) | ❌ 未迁移（口径已完整） | 进程内 `ToolPort` 九要素 | `entry` 一个字段 |

**没有一个组件是「为了凑数」接的，也没有一个是「因为麻烦」不接的。** 下面逐条给判据。

---

## 1. AgentTeams / HiClaw(Matrix)

### ① 用没用 —— ✅ 已接通实测

本机自建 Synapse v1.159.0（容器 `maos-synapse`）的**非加密**房间「MAOS 审批」，
`@boss` / `@intern` / `@maos-bot` 三账号在房，三个账号全部由
`register_new_matrix_user` 注册，没有一步需要人类点 GUI。

证据（都在仓库里，可当场核）：

- `evidence/room/` 五张截图：审批卡片 / 迁移逐条镜像 / `/approve` 生效 /
  `/reject` 补偿 / 越权被拒
- `evidence/room/transcript.md` —— 41 条房间消息的**逐字副本**，含每条完整 Envelope JSON
- 五项映射逐条落点见 [`docs/agentteams-mapping.md`](agentteams-mapping.md)

⚠️ **有一个坑写在这里**：`_NioChannel` 那条活路径只有用
`~/.maos-matrix/venv/` 里的解释器才走得到 —— 系统 `python3` 至今没装 matrix-nio
（`pip show matrix-nio` → `Package(s) not found`），拿它跑就是 `ImportError` → 降级。
详见 [`docs/matrix-room-runbook.md`](matrix-room-runbook.md) 抬头。

### ② 为什么 —— 这是必须项，不是选做

赛题明列「AgentTeams 事件链」：多 Agent 的协作过程要在一个**人能看见、能介入**的
地方留下完整链路，而不是藏在日志里。这一条没有等价物可谈 —— 日志不是「人能介入的地方」。

MAOS 的实现是**装饰器镜像**：进程内 EventBus 照常跑，`MatrixEventBus` 包在外面，
把每条事件顺带镜像进房间；人在 Element 里看全过程，并直接在房间里
`/approve <task_id>`、`/reject <task_id> [原因]`。

### ③ 等价机制 —— 不适用，但降级路径本身就是等价性论证

已接通，所以不需要「等价机制」。但有一件相关的事必须写明：

**主路径是进程内总线，房间是旁路。** 房间连不上、matrix-nio 没装、撞上加密房，
一律自动降级 `log_only`，流水线照跑。这条**有测试钉着**：同样的 publish 序列，
`MatrixEventBus` 降级模式与 inner bus 的 `drain` 结果必须完全一致。

所以「Matrix 挂了会不会毁掉演示」的答案是不会 —— 只是房间里没消息。

### ④ 接的话改哪一个文件 —— 换 homeserver 只动环境变量

| 要换什么 | 改哪里 | 改几行 |
| :-- | :-- | :-- |
| 换 homeserver（自建 Synapse ↔ matrix.org ↔ HiClaw 自带） | `MATRIX_HOMESERVER` / `MATRIX_USER` / `MATRIX_TOKEN` / `MATRIX_ROOM_ID` 四个环境变量 | **0 行代码** |
| 换传输实现（nio ↔ 别的 client） | `hiclaw/matrix_bus.py:258` 的 `_NioChannel` | 一个类 |

三档接入方案（HiClaw 原生扩展点 / HiClaw 自带 homeserver / 自建 Synapse）
**对五项映射表零影响** —— 映射落在 Matrix 协议上，不落在 homeserver 是谁。
本轮锁定第三档，理由是时间盒不是技术受限，见
[`docs/agentteams-mapping.md`](agentteams-mapping.md) 的「最终采用哪一档」。

### ⑤ 一条边界（不写会被问穿）

**房间不是证据，`event_log` 才是。** 房间消息可以被删、被编辑、被折叠，用它当审计链
就等于把权威放在聊天软件里。所以口径是：房间 = 人的可见性与介入面；
`event_log` + `trace.json` = 审计与回放的权威记录（第 5 项映射）。
`_mirror()` 吞掉任何异常只记日志，镜像失败不影响后者。

`docs/agentteams-mapping.md` 末尾还列了**三句不许说的话**（房间里跑的是软件域任务
不是退款域、补偿从不进房间、Synapse 默认限流会打穿），照那份为准。

---

## 2. PolarDB for PostgreSQL

### ① 用没用 —— ✅ 部分实测，且两份记录分开读

🔴 **「本机 pgvector 跑通」和「PolarDB 跑通」是两件事**，仓库里刻意分成两份文档：

| 文件 | 回答什么 | 天花板 |
| :-- | :-- | :-- |
| [`deploy/polardb.md`](../deploy/polardb.md) | **MAOS 这侧怎么接**（怎么配、怎么降级） | 本机 Docker `pgvector/pgvector:pg16` |
| [`deploy/polardb-live.md`](../deploy/polardb-live.md) | **真 PolarDB 实例上跑通了哪几条**（含没跑通的） | 不碰 MAOS 源码，只验数据库那一侧 |

真实例上的地基冒烟**五步全绿**（`polardb-live.md` §1.2），中文分词 zhparser 装成了
（§1.4），HNSW 向量索引的规模性能与召回也测了（§1.5）。**没跑通 / 没覆盖的同样写在
里面**（§2、§3.4）。其中最硬的一条是 §3.5 的 SSL：该实例起初未启用 SSL、公网链路
明文，2026-08-31 已在控制台开启并实测生效（`ssl_in_use = True`）；但**开启之前的
明文期不可撤销，且口令未轮换**，这条残留风险仍记在案，没有当成已闭环。

### ② 为什么 —— RAG 那一维的地基

赛题要求「RAG 面向 workflow 规划」「先按租户/业务/地区/渠道/商品/政策/版本过滤，
再组合规则编号、错误码、全文、语义」。四通道融合里的**全文**与**语义**两条，
SQLite 给不出来：前者要 `to_tsvector` + 中文分词，后者要 `pgvector`。

所以这不是「用个云数据库显得正规」，是检索能力本身的前置条件。

### ③ 等价机制 —— SQLite 作为缺省后端，走同一个 `StorePort`

无库环境（评委裸 clone 的那台机器）跑的是 SQLite，**不需要任何外部依赖**。
两条端口通道退化时，检索退回 `rule_no` / `gateway_code` 两条精确通道。

读数对照（整合轮 13 合并态实测）：**无库 935 passed / 29 skipped；有库 964 passed / 0 skipped**，
935 + 29 = 964 —— 那 29 条正是 22 条 live + 7 条 parity，一条都不许被饿死。

⚠️ 已知缺口，都有账：

- `deploy/polardb.md` 的读数对照表仍写着旧数（932 / 903），已记 `docs/BACKLOG.md`
  的 `## integrate-round-13`，归 PolarDB 运维轨与 SSL 加固一起改
- 两条通道都退化时 `retrieve()` **静默返回空列表**，调用方分不清「没命中」和「通道全死」
  —— 已记 BACKLOG，归 skills 层那一轨
- 退款域 `maos/domain/refund/objects.py` 的 `_conn()` 取 `SqliteStore` 的私有属性，
  **整层绑死 SQLite、上不了 PG** —— 已记 BACKLOG

### ④ 接的话改哪一个文件

| 要换什么 | 改哪里 |
| :-- | :-- |
| SQLite → PG / PolarDB | `maos/store/__init__.py:62` 的 `create_store(store=None, *, backend=None)`；不传就读 `MAOS_STORE_BACKEND`，再不然 `sqlite` |
| PG 侧表结构 | `maos/store/pg_schema.sql`（已存在） |
| PG 侧实现 | `maos/store/pg_store.py`（已存在，不是占位） |
| 连接串 | 环境变量，**不进任何文件**（铁律 6） |

`StorePort`（`maos/store/port.py`）是那个唯一替换点。上层拿到的是端口不是具体实现，
所以换后端时业务代码零改动 —— **除了上面点名的退款域那一处**，它没跟上。
把没跟上的那处写在这里，比说「全都端口化了」更经得起核。

---

## 3. RocketMQ

<!-- FROZEN: awaiting T27 -->
本节结论待 T27 轨实测回填。当前状态：接入中。
回填时必须同时更新：本节四问、README §8 的口径表、docs/ppt-outline.md 对应页。
<!-- /FROZEN -->

### ① 用没用

⏸ 待 T27 回填。

### ② 为什么

⏸ 待 T27 回填。

### ③ 等价机制是什么

⏸ 待 T27 回填。

### ④ 接的话改哪一个文件

⏸ 待 T27 回填。

---

## 4. Nacos

<!-- FROZEN: awaiting T28 -->
本节结论待 T28 轨实测回填。当前状态：接入中。
回填时必须同时更新：本节四问、README §8 的口径表、docs/ppt-outline.md 对应页。
<!-- /FROZEN -->

### ① 用没用

⏸ 待 T28 回填。

### ② 为什么

⏸ 待 T28 回填。

### ③ 等价机制是什么

⏸ 待 T28 回填。

### ④ 接的话改哪一个文件

⏸ 待 T28 回填。

---

## 5. Higress

### ① 用没用 —— ❌ 完全没碰

`maos/model/client.py:74` 的 `HigressModelClient` 是**占位类**：
`complete()` 一进来就 `raise NotImplementedError("Track B：接入 Higress 时实现")`。
它至今没被实现，是一个明确的决定（`docs/decisions/task-a.md` 的 A-12），不是遗漏。

### ② 为什么不接 —— 三条判据，都是实测不是判断

| 理由 | 判据（可当场跑 / 可当场翻） |
| :-- | :-- |
| **本机没有任何 LLM key** | `env \| grep -c MAOS_LLM` → `0` |
| **主路径根本不打网络** | 场景 1–7 与全部测试走 `select_model_client(SCRIPT, force_scripted=True)`：`maos/flows/scenario_6.py:261`、`maos/flows/scenario_7.py:457`。该函数签名与语义由 A-12 冻结，`force_scripted=True` 恒返 `ScriptedModelClient`，**一行网络都不走** |
| **加分点与核心论证不咬合** | Higress 的加分在鉴权 / 路由 / 限流；MAOS 的核心论证在状态机、证据链、权威事实边界（铁律 8）。三个候选组件里投入产出比最低 |

第二条值得多说一句：**这不是「没钱买 key 所以不接」，是「演示必须确定性可复现」。**
评委裸 clone 之后跑 `python3 run.py` 要七个场景全过，不能依赖任何外部服务在线，
也不能依赖模型输出的随机性（`temperature=0` 都不够 —— 网关本身可能不在）。
把网关插进主路径，等于把演示的成败押在一个我们控制不了的东西上。

### ③ 等价机制 —— 客户端侧五道防线已经做完，网关侧三样没有等价物

**做完的（`GatewayModelClient`，`maos/model/client.py:169`，不是占位）**：

| 网关能力 | 客户端侧怎么兜的 | 代码位置 |
| :-- | :-- | :-- |
| 统一协议入口 | OpenAI 兼容协议，`POST {base_url}/chat/completions` | `client.py:204` |
| 鉴权 | `Authorization: Bearer`，key **只读环境变量**、不进任何文件、不进 evidence（铁律 6） | `client.py:272` `select_model_client` |
| 路由 | tier 不选模型，只作为 `X-MAOS-Tier` 请求头交给网关 —— 「tier → 具体模型」是治理决策，属于网关不属于 Agent | `client.py:1`（模块 docstring）、A-12 |
| 凭据不外泄 | 四道：`_api_key` 私有属性、不含 key 的 `__repr__`、`_scrub()` 抹异常文本、`from None` 掐断异常链 | `client.py:169` 起 |
| **凭据不出 origin** | `_SameOriginRedirectHandler`：`scheme`+`hostname`+`port` 三者全等才跟随 3xx。urllib 默认会把 `Authorization` **原样搬到**跳转后的新请求上 —— 前四道防的是「key 进日志」，这一道防的是「key 进别人的服务器」 | `client.py:148` |
| 配置缺失时的行为 | 三个必填变量缺任一 → 降级 `ScriptedModelClient`，只记**缺失的变量名**不记值 | `client.py:272` |

**没有等价物的（诚实写明）**：

- **限流 / 熔断**：一个都没有。`MAOS_MAX_REPLAN` 限的是重规划次数，不是请求速率，
  两回事，别混说。
- **多租户配额与成本治理**：没有。
- **多模型灰度 / 故障转移**：没有。`MAOS_LLM_MODEL` 是单值。

这三样正是 Higress 真正的加分点，也正是本轮**没有**兑现的东西。

### ④ 接的话改哪一个文件 —— 一个文件，而且可能一行代码都不用改

`maos/model/client.py`。模块 docstring 第一行就写着「后续接 Higress AI Gateway 时
**只改这一个文件**」，`maos/README.md:58` 的分轨接手点也点的是这一处。

更精确的迁移路径分两种情况：

| 情况 | 改动 | 判据类型 |
| :-- | :-- | :-- |
| Higress 暴露 **OpenAI 兼容**入口 | **0 行代码** —— 把 `MAOS_LLM_BASE_URL` 指向 Higress 即可，`GatewayModelClient` 原样跑。此时 `HigressModelClient` 这个占位类是多余的 | 🟡 **接口层面的推论，不是已跑通的事实**。协议兼容性没有实测过 |
| Higress 要求私有协议 | 实现 `HigressModelClient.complete()`（约 40 行，照 `GatewayModelClient` 抄），并在 `select_model_client` 里加一个分支 | 🟡 同上 |

两种情况的替换点都在同一个文件里，且 `select_model_client` 是上层**唯一**的构造入口
（签名与语义由 A-12 冻结）—— 这就是迁移点唯一性的全部内容。

### ⑤ 接之前必须先定的一件事

`docs/BACKLOG.md:39` 挂着一条 P1：**同 origin 的 301/302/303 会被 urllib 默认 handler
把 POST 静默改写成 GET**（只剥 `Content-*` 头，造一个不带 body 的 GET），
于是网关一个补斜杠的 302 就会让 `messages` 整个丢掉，而我们拿那个 GET 的响应当
completion 解析。不是密钥问题（没换主机），是「请求内容静默变了而调用方无感」。

候选修法是同 origin 也只放行 307/308（这两个规范要求保持方法与 body）。
**这一轮仍然不改** —— 该修法会缩小兼容面，BACKLOG 原文写着「需要拿真 Higress 的
行为定」，而本轮明确不接 Higress，所以它现在依然定不了。在没有判据的情况下拍板，
比留着这条账更糟。

---

## 6. UnifiedModel

### ① 用没用 —— ❌ 完全没碰，代码里零提及

可当场核：

```bash
grep -rniE "UnifiedModel|统一模型" --include=*.py .     # → 0 行
```

**代码零提及**：没有实现、没有占位类、没有适配层。这一点与 §5 的 Higress 不同 ——
后者至少有一个写明归属的占位类。

全仓范围内唯一提到它的是**本文档**和 `docs/DECISIONS.md` 里本轮那条记录，
两处都是在说明「为什么没做」，不是实现。写在这里免得下一个人跑上面那条
grep 的全仓版本、看见几处命中、以为已经接了什么。

### ② 为什么 —— 🔴 本轮**未评估**，不是「评估后认为不适用」

这两件事差别很大，所以写清楚是哪一件：**本轮没有对 UnifiedModel 做过技术评估。**
仓库里既没有支持它的证据，也没有反对它的证据。

不评估的理由是**这一轮拿不出可复核的实测记录**：本机没有任何 LLM key
（`env | grep -c MAOS_LLM` → `0`），主路径 `force_scripted=True` 不打网络
（见 §5②）。任何多模型统一接入层在这个证据条件下都只能写成纸面论证，
而本文档抬头那条纪律是「指不出证据的断言删掉」。

**所以这一节没有必要性论证，也没有不必要性论证 —— 有的只是「没做」。**
硬写一段像模像样的评估会比空着更糟：评委会拿这份文档逐条对仓库，
对不上的那一条会把其余七节的可信度一起带走。

### ③ 等价机制 —— 有一个极小的统一接入面，但别把它说成 UnifiedModel

现在**确实存在**的是：

| 有的 | 位置 |
| :-- | :-- |
| `ModelClient` 抽象基类（唯一方法 `complete(system, user, tier)`） | `maos/model/client.py:49` |
| 三个实现：`ScriptedModelClient` / `GatewayModelClient` / `HigressModelClient`（占位） | 同文件 |
| **唯一构造入口** `select_model_client()`，签名与语义由 A-12 冻结 | `client.py:272` |
| tier 三档（`strong` / `medium` / `light`），作为路由头而非选模型依据 | `client.py:34` |
| Agent 侧对后端**完全无感** —— 只知道 tier，不知道背后是通义、DeepSeek 还是 OpenAI | 模块 docstring 第 3 行 |

**差距同样写明**（这些是 UnifiedModel 该给而这里没有的）：

- 供给侧只有**一个** OpenAI 兼容后端，没有多供应商适配层
- 没有配额、成本、速率的治理
- 没有故障转移与降级链（唯一的降级是「配置缺失 → Scripted」，那是**冷启动**降级不是**运行时**降级）
- tier 到模型的映射被刻意**推给网关**了 —— 这是设计决定（A-12），但也意味着
  这一侧没有任何模型选择逻辑可言

### ④ 接的话改哪一个文件

`maos/model/client.py` 一个文件：新增一个 `ModelClient` 子类 + 在
`select_model_client` 里加一个分支。上层零改动 —— 因为上层从来只见 `ModelClient`
这个抽象，且构造入口只有一个。

这个论证与 §5④、§8 是**同一条**：抽象做对了，替换点就唯一；替换点不唯一，
说明抽象在别处漏了。

---

## 7. LoongSuite / AgentScope Studio / AgentLoop

### ① 用没用 —— ❌ 未接

仓库里唯一一处提及是 `maos/README.md:63`，写在「后续叠加（不阻塞主线）」里：
「`_transit()` 里挂 OpenTelemetry span；`event_log` 表直接喂 AgentScope Studio」。
LoongSuite 与 AgentLoop **零提及**。没有 SDK 依赖、没有 collector、不发一个字节出去。

### ② 为什么 —— 权威记录必须可重放、可逐字节比对

赛题要的可观测性，MAOS 的口径是：**可观测 ≠ 有个看板。**
证据链的权威记录必须能被第三方**重放**并**逐字节比对** —— 外部 collector 给的是
可视化，不是判据；而判据是这一维真正要买的东西。

所以 `scripts/verify.py` 的第 4 项（`verify.py:484`）校验的是
「`trace.json` 的 span 树无孤儿、无环，且与库重放**逐字节一致**」。
这条断言的前提是 trace 由**我们自己**从 `event_log` 确定性重建 —— 走外部 SDK 就没法这么校。

### ③ 等价机制 —— 自研 `maos/obs/trace.py`，字段对齐 OTel。「对齐到什么程度」逐条说

**对齐的**：

| OTel 语义 | 这里有没有 | 判据 |
| :-- | :-- | :-- |
| `trace_id` / `span_id` / `parent_span_id` / `name` / `start` / `end` / `attributes` | ✅ 七个字段全有 | `trace.py:145` `_span()` |
| `span_id` 宽度 16 位十六进制 | ✅ 与 OTel 一致 | `trace.py:118`，实测 `14c43806d6c7b02f` |
| span 树自校验（孤儿 / 环 / 重复 id） | ✅ **比 OTel 多的** | `trace.py:161` `check_span_tree()` |
| span_id 确定性 | ✅ `sha256(…)[:16]`，同一份库导出多少次都同值 | `trace.py:115` |

**没对齐的（差在哪，逐条）**：

| 差异 | 实际是什么 | 影响 |
| :-- | :-- | :-- |
| **不做真 OTel 导出** | 没有 SDK 依赖、没有 collector、没有 OTLP exporter | 接不上任何现成后端 |
| `trace_id` 不是 OTel 的 32 位十六进制 | 实测 `trace_b81e77f2522c` —— 业务前缀 + 12 位十六进制，共 18 字符 | 接 OTLP 前**必须先补成 32 hex**，否则 collector 直接拒 |
| `kind` 不是 OTel 的 `SpanKind` | 这里的 `kind` 是 `plan` / `task` / `event` / `artifact`，表达「这条 span 描述什么」；`trace.py:55` 明写「与 OTel 的 SpanKind 不是一回事」 | 语义要重映射 |
| 没有 W3C `traceparent` 传播 | 单进程，没有跨服务上下文传递的需求 | 分布式部署时要补 |
| 没有 `Status` / `Events` / `Links` | 三个字段一个都没有 | 错误语义现在挂在 `attributes` 里 |

**有的是 OTel 没有的**，而且这几样正是为这次评审做的：

- `provenance` 字段：每个 span 的产物指得到入库路径；指不到的显式标
  `provenance="unknown"`，**不悄悄挂到某个 span 下面充数** —— 审计链有洞要让洞看得见
- `summary.unsourced_artifacts` 哨兵：当前恒为 0，但**不删** —— 谁再绕开
  `on_task_result` 又不补事件，它就从 0 变回非 0，A 类 warn 当场回来
- 铁律 8 的纪律写进模块 docstring 第一段：本模块**只读，不推断业务状态**，
  span 上的每个字段都能在 `plan` / `task` / `artifact` / `event_log` 四张表里逐字找到出处

实测规模（逐场景读 `evidence/scenario-<N>/trace.json`）：

| 场景 | 1 | 2 | 3 | 4 | 5 | 6 | 7 |
| :-- | --: | --: | --: | --: | --: | --: | --: |
| span 数 | 36 | 46 | 16 | 9 | 30 | 54 | 103 |
| `unsourced_artifacts` | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `tree_errors` | — | — | — | — | — | — | — |

场景 6 的 54 条 span 里四种 kind 齐全；场景 7 是唯一有两个 plan 的（replan），
两棵树各自成树。两个哨兵七场七零，证据束 8/8 PASS。

### ④ 接的话改哪一个文件

`maos/obs/trace.py` —— 加一个 exporter，把现成的 span 列表转成 OTLP。
`maos/README.md:62` 已经把这一步写进「后续叠加」。

前置一步不能跳：**`trace_id` 得先补成 32 位十六进制**（见上表），
这是当前形状与 OTel 之间唯一的硬阻塞。

依赖方向上不会有麻烦：`maos/obs` 只 import `maos.core.store`，
不 import 任何业务域（铁律 9），换域时本文件一行不改。

---

## 8. MCP

### ① 用没用 —— ❌ 没有做 MCP 迁移

当前 4 个工具（`gateway.query` / `gateway.refund` / `sandbox.git_apply` /
`sandbox.pytest_run`）的 `entry` 都是**进程内函数**。

### ②③④ —— 口径已经完整，写在别处，这里只给指针

**迁移路径的完整论证在 [`docs/toolport-contract.md`](toolport-contract.md) 的
「迁移到 MCP」一节**，包括：迁移的实质是什么、九要素里哪一个要换、
审计行 / `verify.py` 第 1 项 / Identity 白名单为什么全部原样成立，
以及那段论证本身是「接口层面的推论，不是已跑通的事实」这句自我限定。

🔴 **这里刻意不复制那段内容。** `docs/toolport-contract.md` 是
`scripts/gen_docs.py` 的**生成物**（文件头写着「请勿手改」），
复制出来的副本会在下一次 `gen_docs` 之后过期，然后两份文档说的话开始不一样 ——
那比没写更糟。核验生成物与代码是否一致：

```bash
python3 scripts/gen_docs.py --check    # 不一致即非零退出
```

### ⑤ 为什么八个组件里只有它已经有完整口径

因为**没做 MCP 迁移**恰好是评分规则点名的重大失分项之一
（「未使用 Metrics、RAG、MCP，且未说明理由」），而那条的分水岭是
**「且未说明理由」**。工具层的迁移点唯一性（`entry` 是 `Callable`，
替换点只有一个）此前已经论证过，所以这一节只需要把指针给准。

**其余七个组件的理由此前散在代码注释与 `docs/BACKLOG.md` 里，没有一个集中出处
—— 这份文档就是为补那个洞写的。**

---

## 附：这份文档与哪些文件是同一件事的不同侧面

| 文件 | 关系 |
| :-- | :-- |
| `README.md` §8「与提案 / 比赛要求的映射」+「数据口径」 | **口径上位法**。本文档不得与它冲突 |
| [`docs/agentteams-mapping.md`](agentteams-mapping.md)「当前真实状态」 | 同上，第 1 节以它为准 |
| [`docs/toolport-contract.md`](toolport-contract.md) | 第 8 节的正文在那里（代码生成物，只引不抄） |
| [`deploy/polardb.md`](../deploy/polardb.md) / [`deploy/polardb-live.md`](../deploy/polardb-live.md) | 第 2 节的实录在那里，两份别混着读 |
| `docs/BACKLOG.md` / `docs/DECISIONS.md` | 本文档里每一条「已知缺口」都在账本上有对应行 |

> ⚠️ 撰写本文档时发现 `README.md` §8 的数据口径与
> `docs/agentteams-mapping.md` 的「当前真实状态」**互相矛盾**（前者写「Matrix 真房间
> 未接通」，后者写「真房间已接通」并有 `evidence/room/` 兜底）。本文档第 1 节按
> **后者**写 —— 它是那一节的口径上位法，且有证据。矛盾本身已记入
> `docs/BACKLOG.md` 的 `## task-T30`，由整合轮统一改 README，本轨不动它。
