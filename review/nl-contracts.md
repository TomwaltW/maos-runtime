# 自然语言接入面 · 跨轨契约（T66 / T67 / T68 / T69）

2026-09-02 立。基线 `b35c618`。**四轨照同一份定义写，谁都不许自己改口径** ——
改了就是并轨时合不拢。要改先停手问人类。

本文件是**派单的一部分**，不是 `docs/parallel/contracts.md`（那份是冻结面，只读）。

---

## 0. 这一轮到底要买什么

现状：Matrix 房间里的机器人**只认两个词** —— `/approve <task_id>` 和
`/reject <task_id> [原因]`。人说「同意」「批了吧」「这个先别过」一律没反应，
因为 `hiclaw/room_demo.py` 是**一次性进程**：跑完那一条审批就 exit 0，
房间里根本没有常驻监听。

要买的是：**人说人话 → 系统听懂 → 转成结构化动作**。

不买的是：**让模型代替人做决定**。见 §2 的三条红线，尤其 R1。

---

## 1. 数据契约（T66 定义并实现，T67 / T68 只读引用）

落在 `maos/nlu/intent.py`。四轨照抄这段，一个字段名都不许改：

```python
ACTION_APPROVE = "approve"
ACTION_REJECT  = "reject"
ACTION_STATUS  = "status"
ACTION_UNKNOWN = "unknown"
ACTIONS = frozenset({ACTION_APPROVE, ACTION_REJECT, ACTION_STATUS, ACTION_UNKNOWN})

CONF_HIGH = "high"
CONF_LOW  = "low"


@dataclass(frozen=True)
class Intent:
    action: str                    # 必在 ACTIONS 内，否则构造即 ValueError
    task_id: str = ""              # approve / reject 时必填，且必须命中 known_task_ids
    reason: str = ""               # reject 的理由，人说了才有，不许模型编
    confidence: str = CONF_LOW     # CONF_HIGH / CONF_LOW，缺省保守取低
    raw_text: str = ""             # 人的原话，逐字留痕，用于回执与取证
    detail: dict = field(default_factory=dict)   # 模型原始返回，排查用
```

### 1.1 解析接口（T66 实现）

```python
def parse_intent(text: str, *, model: ModelClient,
                 known_task_ids: Sequence[str]) -> Intent:
    """把一句人话转成 Intent。任何认不出的情况都返回 action=ACTION_UNKNOWN，
    **不抛异常、不猜**。"""
```

四条兜底，缺一条都算没做完：

| 情况 | 必须的行为 |
| :-- | :-- |
| 模型返回不是合法 JSON | `Intent(action=UNKNOWN)`，不抛 |
| `action` 不在 `ACTIONS` 内 | `Intent(action=UNKNOWN)`，不抛 |
| `task_id` 不在 `known_task_ids` 里 | `Intent(action=UNKNOWN)`，**绝不放行** |
| `model` 是 `ScriptedModelClient`（无 key 的机器） | 仍要能工作：走关键词兜底，返回 `CONF_LOW` |

### 1.2 派发接口（T68 实现）

落在 `maos/runtime/intent_dispatch.py`：

```python
KIND_CONFIRM = "confirm"    # 需要人再发一次显式指令才生效
KIND_DONE    = "done"       # 显式指令已执行，状态真的迁移了
KIND_DENIED  = "denied"     # 发言人不在 MAOS_APPROVERS 名单
KIND_IGNORED = "ignored"    # unknown / 与编排无关的闲聊


@dataclass(frozen=True)
class DispatchResult:
    kind: str          # 必在上面四个内
    text: str          # 要发回房间的话，人读的
    task_id: str = ""


def dispatch_intent(intent: Intent, *, store, cp, sender: str,
                    approvers: Sequence[str]) -> DispatchResult:
    ...
```

### 1.3 监听接口（T67 实现）

落在 `hiclaw/room_agent.py`。它**只管收发和路由**，不含任何理解逻辑：

```python
class IntentParser(Protocol):
    def __call__(self, text: str, *, known_task_ids: Sequence[str]) -> Intent: ...
```

T67 自带一个 `_KeywordParser` 兜底实现（不调模型），整合时换成 T66 的 `parse_intent`
的偏函数。**T67 不许 import `maos.nlu`** —— 那样两轨就串了，并行没法跑。

---

## 2. 🔴 三条红线（四轨共同遵守，违反即整轨作废）

### R1 自然语言不能授权

`Intent(action=approve)` 或 `reject` **永远只产出 `KIND_CONFIRM`**，回一句：

```
你是想批准 task_997ca4541e66 吗？确认请发：/approve task_997ca4541e66
```

真正调用 `HumanApprovalQueue.decide()` 的唯一入口，仍然是**显式的
`/approve <task_id>` 文本**，与今天的行为一字不差。

**为什么**：模型幻觉一次，等于越权批掉一笔生产变更。这与铁律 8 同源 ——
权威动作不能由推断产生。自然语言层买的是「少打字、看得懂」，
不是「替人做决定」。答辩时这一条是加分项，不是偷懒。

### R2 task_id 不许由模型编

解析出的 `task_id` 必须命中调用方给的 `known_task_ids`。对不上一律降
`ACTION_UNKNOWN`。模型很擅长编出格式完全正确的 id。

### R3 不碰状态机与冻结面

- 不许在 `maos/contracts/states.py` 加任何状态或迁移（铁律 9）
- 不许改 `maos/contracts/**`（铁律 1）
- 不许改 `maos/core/store.py` 现有表结构（只许新增表，本轮不需要新增）
- 自然语言层只产出 `Intent` / `DispatchResult` 两种**进程内**数据，
  不进事件契约、不进库

---

## 3. 测试口径（四轨统一）

- **全部测试显式 `force_scripted=True`**，一行网络都不许走。
  配了 key 的机器上跑测试必须与没配 key 的机器逐字节一致。
- 涉及模型返回的用例，用 `ScriptedModelClient(script)` 喂预设 JSON 字符串，
  包括**畸形输入**：非 JSON、缺字段、`action` 越界、`task_id` 编造。
- 涉及 Matrix 的用例不许连真房间，用假 channel。

---

## 4. 轨与文件（零交集）

| 轨 | 独占文件 | 一句话职责 |
| :-- | :-- | :-- |
| T66 | `maos/nlu/**`、`maos/tests/test_nlu_intent.py` | 一句人话 → `Intent` |
| T67 | `hiclaw/room_agent.py`、`maos/tests/test_room_agent.py` | 房间常驻监听与路由 |
| T68 | `maos/runtime/intent_dispatch.py`、`maos/tests/test_intent_dispatch.py` | `Intent` → 动作，含权限闸 |
| T69 | `docs/nl-interface.md`、`docs/matrix-room-runbook.md`、`scripts/nl_smoke.py`、`maos/tests/test_nl_interface_doc.py` | 文档、runbook 补坑、端到端冒烟 |

四轨都要在 `docs/BACKLOG.md` 与 `docs/DECISIONS.md` **尾部另起** `## task-T<NN>` 小节。

---

## 5. 整合顺序（编排侧的事，子会话不用管）

T66 → T67 / T68 并行 → T69。整合时把 T67 的 `_KeywordParser` 换成 T66 的
`parse_intent`，把 T67 的 `_StubDispatcher` 换成 T68 的 `dispatch_intent`。
两处替换点必须在各自轨里留 `# INTEGRATION-POINT:` 注释，整合时 grep 它。
