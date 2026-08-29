# AgentTeams / HiClaw(Matrix) 映射

评委要求「AgentTeams 事件链」—— 多 Agent 的协作过程要在一个人能看见、能介入的
地方留下完整链路，而不是藏在日志里。

MAOS 的做法是**装饰器镜像**：进程内 EventBus 照常跑，`MatrixEventBus` 包在外面，
把每条事件顺带镜像进 Matrix 房间；人在 Element 里看全过程，并直接在房间里
`/approve`、`/reject`。房间就是 AgentTeams 的载体。

**主路径是进程内总线，房间是旁路。** 房间连不上、matrix-nio 没装、撞上加密房，
一律自动降级 `log_only`，流水线照跑 —— 演示当天 Matrix 挂了，场景不受影响，
只是房间里没消息。

---

## 五项映射

| # | AgentTeams 概念 | MAOS 落点 | 代码位置 | 状态 |
| :-- | :-- | :-- | :-- | :-- |
| 1 | **Team / 房间** | 一个 Matrix 房间 = 一条流水线的全部事件；配置四项走环境变量 `MATRIX_HOMESERVER` / `MATRIX_USER` / `MATRIX_TOKEN` / `MATRIX_ROOM_ID`，缺一即降级 | `hiclaw/matrix_bus.py:70`（`MatrixBusConfig`）<br>`hiclaw/matrix_bus.py:85`（`from_env`）<br>`hiclaw/matrix_bus.py:55`（`REQUIRED_ENV`） | ✅ **真房间已接通**（本机自建 Synapse v1.159.0 的非加密房，`evidence/room/` 五张图 + 41 条逐字副本） |
| 2 | **Member / Worker** | 可插拔 Agent 池：role → Agent 类，投放一个文件即注册；Worker 收到 `TaskAssignment` 按 role 取执行者 | `maos/agents/base.py:101`（`AGENT_POOL`）<br>`maos/agents/base.py:104`（`@register`）<br>`maos/runtime/worker.py:34`（一行构造全池） | ✅ 已跑通（10 个 Agent 身份，其中 9 个可被派单，见 [`agent-identity.md`](agent-identity.md)） |
| 3 | **事件链 / 消息流** | `publish()` 先走 inner bus，再镜像进房间：一行人话摘要 + 折叠的 Envelope JSON | `hiclaw/matrix_bus.py:321`（`publish`）<br>`hiclaw/matrix_bus.py:132`（`summarize` 人话摘要）<br>`hiclaw/matrix_bus.py:147`（`render_mirror` 摘要 + JSON） | ✅ **真房间实测**：两轮跑出 41 条房间消息，逐字副本 `evidence/room/transcript.md`；迁移逐条镜像见 `02-transitions.png` |
| 4 | **人工介入 / HITL** | 房间里 `/approve <task_id>`、`/reject <task_id> [原因]` → `HumanApprovalQueue.decide()`；只认 `MAOS_APPROVERS` 名单内的用户，名单外回「无审批权限」**并落一条 event_log** | `hiclaw/matrix_bus.py:420`（`parse_approval_command`）<br>`hiclaw/matrix_bus.py:435`（`RoomApprovalBridge`）<br>`hiclaw/matrix_bus.py:452`（`handle_message` 先查名单再解析）<br>`hiclaw/matrix_bus.py:483`（越权落库） | ✅ **三种在真房间各实测一次**：`/approve`→DONE（`03`）、`/reject`→FAILED（`04`）、intern 越权两次被拒且闲聊零回复（`05`） |
| 5 | **可观测 / 回放** | 事件链的权威记录不在房间里，在 `event_log` 表；`maos/obs/trace.py` 把它转成 OTel 对齐的 span 树，`scripts/verify.py` 第 4 项重放校验「无孤儿、无环、与库逐字节一致」 | `maos/obs/trace.py`<br>`scripts/verify.py:318`（第 4 项 trace-tree） | ✅ 已跑通（7 场景证据束，见 `evidence/`） |

### 为什么第 5 项要单列

房间是**给人看的镜像**，不是证据。房间消息可以被删、被编辑、被折叠，用它当审计
链就等于把权威放在了聊天软件里。所以 MAOS 的口径是：

- **房间** = 人的可见性与介入面（第 1、3、4 项）
- **`event_log` + `trace.json`** = 审计与回放的权威记录（第 5 项）

镜像失败不影响后者 —— `_mirror()` 吞掉任何异常只记日志（`hiclaw/matrix_bus.py:348`）。

---

## 最终采用哪一档

手册给了三档（`docs/EXECUTION.md:487`、`docs/phases/phase-3.md:22`）：

| 档 | 房间来源 | 采用 |
| :-- | :-- | :-- |
| A 档 | HiClaw Worker 原生接入（改 HiClaw 的扩展点） | ❌ 未做 |
| B 档 | HiClaw 起来后用它自带的 Matrix homeserver | ❌ 未做 |
| **C 档** | `docker run` 官方 Synapse，注册 `maos-bot` + 人类账号，自建私密房间 | ✅ **锁定 C 档** |
| C 档保底 2 | matrix.org 公网账号 + 私密房间 | 备用（C 档起不来时） |

**选 C 档的理由是时间盒，不是技术受限**（手册 v4 把 HiClaw 对接从整天压到半天）。
三档对上面那张五项映射表**零影响** —— 映射落在 Matrix 协议上，不落在 homeserver 是谁。

### 当前真实状态（不吹）

截至基线 `27c9e18`（T 轮实跑改写；上一版停在 `df96fa8`，那时确实还没接通）：

- **真房间已接通。** 本机自建 Synapse v1.159.0（容器 `maos-synapse`），
  房间「MAOS 审批」为**非加密**房（`m.room.encryption` 查得 `M_NOT_FOUND`），
  `@boss` / `@intern` / `@maos-bot` 三人在房。三个账号全部由
  `register_new_matrix_user` 脚本注册，**没有一步需要人类点 GUI** ——
  上一版写的「需人类手工注册所以未开工」是个错误前提，`docs/hiclaw-probe.md` §1 已纠正。
- `_NioChannel` 那条活路径（`hiclaw/matrix_bus.py:178`）**走到了**，
  两轮 `room_demo` 实跑证据在 `evidence/room/`。
- ⚠️ **但上一版那句「matrix-nio 未安装，恒走 ImportError」并没有全错，只是漏了主语。**
  精确说法是：**系统 `python3` 至今没装 matrix-nio**（实测
  `pip show matrix-nio` → `Package(s) not found`），拿它跑就是 `ImportError` → 降级；
  matrix-nio 0.26.0 装在 `~/.maos-matrix/venv/`，**用那个解释器才走得到活路径**。
  这一条是本轮最容易把人坑住的地方，已写进
  `docs/matrix-room-runbook.md` 抬头那一节。
- 仍然被测试覆盖的是**降级路径的等价性**：同样的 publish 序列，`MatrixEventBus`
  降级模式与 inner bus 的 `drain` 结果必须完全一致 —— 这条是可断言的、CI 常态路径，
  **且仍然是主路径**（见本文抬头：房间是旁路，房间挂了流水线照跑）。
- 所以演示材料里的口径现在可以是：**「镜像层已实现，降级路径实测等价，
  真房间三条路径实测通过、截图与逐字副本在 `evidence/room/`」**。

**仍然不许说的三句**（实测证不到，说了会被问穿）：

| 不许说 | 实际是什么 |
| :-- | :-- |
| 「退款全过程在 Element 里跑通了」 | 房间里跑的是 `room_demo`，一个 `role=coding` 的**软件域**任务（标题「变更生产环境配置」）。退款域场景 6/7 的证据在 `evidence/scenario-6,7/`，那是**机器侧**证据，没进过房间 |
| 「`/reject` 之后补偿在房间里可见」 | `CompensationExecuted` 只落 `event_log`、从不 publish，**永不进房间**。`04` 那张图证明的是「驳回生效 + Plan FAILED」，不是补偿 |
| 「房间镜像稳定可靠」 | Synapse 默认限流会打穿：一轮 approve 实测 4 条 429，且 `_NioChannel` 的 10s 超时会因此误报 `房间回话失败（）`（消息其实送达了）。见 runbook §7.6 |

> `docs/submission-checklist.md` §A-4「Matrix 房间」那一行仍写着
> 「镜像层已实现，降级路径实测等价，真房间待接通」+ 复核结论「真房间未接通」——
> **该行已过期**，但那份文件是整合轮的面，本轮只读不改，已记
> `docs/BACKLOG.md` 的 `## task-T4`。

---

## AutoGen 的口径（一并写明）

**AutoGen 降级为方案文档里的可选 Worker 内核，复赛代码不集成。**

复赛的模型调用路径是 `maos/model/client.py` 的 OpenAI 兼容客户端 + `SkillInvoker`，
**不经任何 agent 框架**。方案 PPT 与长文档里涉及 AutoGen 的表述统一改口为
「可插拔内核之一（未在复赛演示中启用）」。

理由：Identity 的三查（工具白名单 / 风险等级 / 写资源）与 Skill 白名单是运行时强制的，
放进第三方 agent 框架的循环里就不再是我们说了算 —— 而那三查正是「agent 干不了什么」
这个论证的全部依据。
