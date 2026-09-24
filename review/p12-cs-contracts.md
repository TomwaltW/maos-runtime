# 客服前台 p12 · 跨轨契约（T167 / T168 / T169 / T170）

2026-09-24 立。基线 `393dd5b`（goai-restructure，p11 整合收尾）。
方案本体：Drive/MAOS「MAOS-客服系统方案-v1」；执行交接：Drive/MAOS「MAOS-客服系统-执行交接-v1」。
**四轨照同一份定义写，谁都不许自己改口径** —— 改了就是并轨时合不拢。要改先停手回主会话。

本文件是派单的一部分，**提交进版本库**（本 clone 的 review/ 不在 exclude 里；worktree
从 commit 建，看不见未提交的契约 —— p11 吃过这个亏，见 BACKLOG task-t161 那一条）。
它不是 `docs/parallel/contracts.md`（那份是冻结面，只读）。

冻结的类型与常量是 `maos/domain/cs/types.py`，由 `maos/tests/test_cs_contract_p12.py`
逐字段钉住。本文件与那两份不一致时，以代码为准并回主会话报告。

---

## 0. 这一期买什么、不买什么

**买**：外部渠道（先 wechat_kf）的售后政策问答，形状照 ADP 课程 3.2 标准模式（问答对 +
同义词 + 兜底 / 拒答 + 测试集）：

- 客户说一句话 → 找得到话术就照**标准话术**回，找不到就**兜底**（不编）；
- 该转人工的（点名要人工、投诉、情绪、赔偿、隐私、要看具体订单、连续判不准）一律转，
  内部房间收到一张**带齐上下文**的卡片；
- 会话对象落库，每一轮进 event_log；
- 回复出门前过一道**确定性后置校验**：状态字眼挂不上本轮观察就换兜底 + 转人工。

**不买**：
- 不查单、不读支付观察、不写任何外部系统（查单要先核验「这单是不是你的」，那是 p13）；
- **不调模型**（p12 零模型调用：问答对 + 同义词 + 兜底本来就是确定性的；槽位抽取、
  意图模型、多语种在 p13 引入，届时再定 call_site 与成本归属）；
- 不改命令路径：外部渠道上 `/refund` `/help` 等今天的行为逐字不变（外泄问题记 BACKLOG，p13 收口）；
- 不做评测批量化与 verify 新项（p14）。

---

## 1. 数据契约

### 1.1 类型与常量 —— `maos/domain/cs/types.py`（骨架提交，冻结）

`Claim` / `ReplyDraft` / `Violation` / `CheckResult` / `ScriptHit` / `HandoffCard` /
`DeskResult` 七个 frozen dataclass；`STAGES` / `ROUTES` / `INTENTS` / `HANDOFF_REASONS` /
`DELIVERIES` / `VIOLATION_KINDS` / `CS_EVENT_TYPES` 七个枚举；`STATUS_WORDS` /
`STATUS_PATTERNS`；id 函数 `conversation_id_for` / `turn_id_for` / `plan_id_for`；
`text_digest`、`mask_customer`。**读代码，不在这里重抄。** 各轨只许 import，不许改。

### 1.2 轮次归属（开放探针的结论）

| 落点 | plan_id | task_id | trace_id |
| :-- | :-- | :-- | :-- |
| event_log（SkillInvoked / KbRetrieved / Cs* 四种） | `plan_id_for(conversation_id)` 即 `cs:csc-…` | `turn_id` | `""` |
| model_usage | p12 不产生（零模型调用） | — | — |

- 调 `SkillInvoker.invoke` / `retrieve` 时，extras 恒带 `{"plan_id": …, "task_id": turn_id, "trace_id": ""}`。
- **不许**编一个非空 trace_id（查不到 plan → verify 第 8 项判负），**不许**给 model_usage 塞 task_id。
- CS 的 event_log 行**不许进** `scenario-*` / `case-*` 证据束：`cs:` 前缀在 trace.py 里没有树族，
  进了就是游离事件，撞 test_verify_warn 的退役标记。cs 树族与 verify 新项是 p14 的事。

### 1.3 表（T167 独占 DDL）—— maos/domain/cs/schema.sql（新）

SQLite 方言、只 `CREATE TABLE/INDEX IF NOT EXISTS`、全部 `cs_` 前缀、租户首列且进主键、
时间戳由 Python 传入（不用 `datetime('now')`，要能被 `_dbport.to_pg_ddl` 翻译）。
走 `DomainConn`（退款域写法，双后端）。**不进** `maos.domain.DOMAIN_REGISTRY`。

```sql
cs_schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)

cs_conversation (
  tenant_id TEXT NOT NULL,          -- 映射不到就是 ''（不猜默认租户）
  conversation_id TEXT NOT NULL,    -- conversation_id_for(...)
  channel TEXT NOT NULL,
  open_kfid TEXT NOT NULL DEFAULT '',   -- 回信必需
  external_userid TEXT NOT NULL,        -- = InboundMessage.chat_id，回信目标
  stage TEXT NOT NULL CHECK (stage IN ('active','handed_off','closed')),
  fallback_streak INTEGER NOT NULL DEFAULT 0,
  turn_count INTEGER NOT NULL DEFAULT 0,
  opened_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY (tenant_id, conversation_id))

cs_turn (
  tenant_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
  turn_id TEXT NOT NULL, seq INTEGER NOT NULL,
  msg_dedup_key TEXT NOT NULL DEFAULT '',   -- ingress:<channel>:<msg_id>
  inbound_text TEXT NOT NULL,               -- 客户原文（会话对象本身）
  reply_text TEXT NOT NULL DEFAULT '',
  route TEXT NOT NULL CHECK (route IN ('answer','fallback','handoff','silent')),
  intent TEXT NOT NULL DEFAULT '',
  handoff_reason TEXT NOT NULL DEFAULT '',
  draft_json TEXT NOT NULL DEFAULT '{}',    -- ReplyDraft.to_json()（校验后的最终版）
  check_json TEXT NOT NULL DEFAULT '{}',    -- CheckResult.to_json()
  created_at TEXT NOT NULL,
  PRIMARY KEY (tenant_id, turn_id))
  -- 另建索引 (tenant_id, conversation_id, seq)

cs_handoff (
  tenant_id TEXT NOT NULL,
  handoff_id TEXT NOT NULL,         -- 与 turn_id 同值：一轮至多一张卡
  conversation_id TEXT NOT NULL, turn_id TEXT NOT NULL,
  reason TEXT NOT NULL,             -- HANDOFF_REASONS，CHECK 列全
  card_json TEXT NOT NULL,          -- HandoffCard.to_json()
  delivery TEXT NOT NULL CHECK (delivery IN ('pending','delivered','failed','unconfigured')),
  delivered_to TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY (tenant_id, handoff_id))
```

`stage` 与 `delivery` 是业务对象自己的字段（铁律 9），迁移表是 `types.STAGE_FLOW`。

### 1.4 各轨的函数面（签名冻结，实现归各轨）

**T167 · maos/domain/cs/objects.py、maos/domain/cs/conversation.py（新）**

```python
# objects.py —— 照 maos/domain/refund/objects.py：DomainConn、ensure_schema、execute/query、_migrate
def ensure_schema(store) -> None                      # 幂等；每个写入口先调
def execute(store, sql: str, params=()) -> None
def query(store, sql: str, params=()) -> list[dict]

# conversation.py
@dataclass(frozen=True)
class Conversation:  # 字段 = cs_conversation 的列，同名同序
    tenant_id: str; conversation_id: str; channel: str; open_kfid: str; external_userid: str
    stage: str; fallback_streak: int; turn_count: int; opened_at: str; updated_at: str

def open_conversation(store, *, tenant_id, channel, open_kfid, external_userid, now=None) -> Conversation
def get_conversation(store, tenant_id, conversation_id) -> Conversation | None
def allocate_turn(store, conv: Conversation) -> tuple[str, int]      # (turn_id, seq)，turn_count 原子 +1
def record_turn(store, conv, *, turn_id, seq, msg_dedup_key, inbound_text, reply_text, route,
                intent, handoff_reason, draft: ReplyDraft, check: CheckResult, now=None) -> Conversation
    # 插 cs_turn；fallback_streak：route=fallback → +1，answer/handoff → 0，silent → 不变；
    # 落一条 CsTurnRecorded；返回更新后的会话
def change_stage(store, conv, to_stage: str, *, turn_id: str, reason: str, now=None) -> Conversation
    # 按 STAGE_FLOW 校验，非法迁移抛 ValueError；落一条 CsConversationStageChanged
def recent_turns(store, tenant_id, conversation_id, *, limit=CARD_RECENT_TURNS) -> list[tuple[str, str]]
    # [(客户原文, 回复原文)]，按 seq 升序、取最后 limit 轮
def record_handoff(store, card: HandoffCard, *, delivery=DELIVERY_PENDING, now=None) -> None
    # 插 cs_handoff；落一条 CsHandoffRaised
def mark_handoff_delivery(store, tenant_id, handoff_id, delivery: str, *, delivered_to="", now=None) -> None
def list_handoffs(store, tenant_id, *, delivery: str | None = None) -> list[tuple[HandoffCard, str]]
def record_reply_rejected(store, conv, *, turn_id: str, check: CheckResult) -> None
    # 落一条 CsReplyRejected（只落违例种类与摘要）
```

四种 Cs* 事件的 detail **只许**带：摘要（`text_digest`）、枚举值、doc_id、计数、违例种类。
**不许**带客户原文、回复原文、external_userid、open_kfid。

**T168 · maos/domain/cs/corpus.py、maos/domain/cs/scripts.py（新）**

```python
# corpus.py
CORPUS_PATH: pathlib.Path                    # scenarios/cs/kb/cs_scripts.json
def load_corpus(path=CORPUS_PATH) -> list[dict]          # kb_doc 行，键 == kb.DOC_COLUMNS
def seed_cs_kb(store, *, tenant_id: str | None = None) -> int   # upsert；返回条数

# scripts.py
MIN_SCRIPT_SCORE: float                      # 命中门槛，[0,1]
def match_scripts(store, *, tenant_id: str, text: str, plan_id: str, task_id: str,
                  limit: int = 3) -> list[ScriptHit]
    # 只检 kind=cs_script、biz_type='cs'；按 score 降序；KB 关着返回 [] 且不落事件；
    # 否则**恰好落一条** KbRetrieved（plan_id/task_id 照传、trace_id=""），
    # detail.docs 就是返回的这几篇；detail.query 里的客户原文换成 text_digest(text)。
```

**T170 · maos/domain/cs/claims.py、maos/domain/cs/evaluate.py（新）**

```python
# claims.py —— 纯函数，确定性，不调模型
def status_spans(text: str) -> list[tuple[int, int, str]]    # 按 STATUS_PATTERNS 找出的 (起, 止, 原文)
def check_reply(draft: ReplyDraft, *, observations: frozenset[str] = frozenset(),
                kb_doc_ids: frozenset[str] = frozenset()) -> CheckResult
def turn_kb_doc_ids(store, *, conversation_id: str, turn_id: str) -> frozenset[str]
    # 从 event_log 读回本轮（plan_id=plan_id_for(conv) 且 task_id=turn_id）KbRetrieved 的 docs[].doc_id

# evaluate.py
EVAL_PATH: pathlib.Path                      # scenarios/cs/eval/p12_cases.json
def load_cases(path=EVAL_PATH) -> tuple[EvalCase, ...]
def run_eval(desk_factory, cases, *, tenant_map: dict[str, str] | None = None) -> EvalReport
    # desk_factory() 返回任何有 handle(InboundMessage) -> DeskResult 的对象；
    # 每个 case 一段新会话（external_userid = f"eval-{case.id}"，open_kfid 缺省 "wk_eval"）
```

`check_reply` 的五条规则（全部满足才 ok）：

1. `literal_not_in_text`：每条 claim 的 literal 非空且是 text 的子串。
2. `dangling_basis`：basis_ref 必须是 `obs:<id>`（id ∈ observations）或 `kb:<doc_id>`（∈ kb_doc_ids）。
3. `unbacked_status`：text 里每一处状态字眼，都必须落在某条**有效 `obs:`** claim 的 literal 在 text 中的出现区间里。`kb:` 撑不起状态。
4. `uncited_rule`：citations 每一项 ∈ kb_doc_ids。
5. `foreign_literal`：literal 里含「已到账 / 已退款 / 补偿 / 赔偿」的 claim，literal 必须**逐字**等于 `maos.domain.refund.projection` 的五个 `PUBLIC_*` 之一。

p12 没有任何观察来源，observations 恒为空 —— 于是 p12 的回复**说不出任何状态字眼**，
话术库与前台常量都必须在空观察下过校验。这是设计。

**T169 · maos/domain/cs/desk.py、maos/domain/cs/triggers.py（新）**

```python
@dataclass(frozen=True)
class CsConfig:
    tenants: Mapping[str, str]                    # open_kfid → tenant_id
    handoff_target: tuple[str, str] | None        # (渠道, chat_id)，None = 只落库
    @classmethod
    def from_env(cls, env=os.environ) -> "CsConfig"
        # MAOS_CS_TENANTS="wk_1=tnt-demo,wk_2=tnt-b"；MAOS_CS_HANDOFF_TARGET="feishu:oc_xxx"

class FrontDesk:
    def __init__(self, store, config: CsConfig, *, clock=None): ...
    config: CsConfig
    def handle(self, msg: InboundMessage) -> DeskResult     # 永不抛：内部异常 → 兜底 + 转人工
def render_card_text(card: HandoffCard) -> str              # 内部房间看到的纯文本卡片

CS_FRONT_DESK_IDENTITY: AgentIdentity   # agent_id='cs-front-desk', role='cs_front_desk',
    # allowed_skills={'cs.answer','cs.handoff'}, allowed_tools=frozenset(), max_risk='L'
```

一轮的判定顺序（T169 实现，T170 评测照此出题）：

1. 会话 `stage == handed_off` → `silent`（只记录，不回话、不出卡）。
2. 租户映射不到（tenant_id 为空）→ `handoff` / `tenant_unmapped`，不检索。
3. 触发词（优先级从高到低）：`privacy` > `compensation` > `anger` > `complaint` > `requested`
   → `handoff`，意图分别为 privacy / compensation / complaint / complaint / handoff_request。
4. 检索话术：`hits[0].score >= MIN_SCRIPT_SCORE` 且话术带 handoff 标记 → `handoff` / 该标记，
   回话用该话术的标准话术；不带标记 → `answer`，回标准话术、citations = (该篇 doc_id,)。
5. 没命中 → `fallback`；若这一轮使 fallback_streak 达到 `FALLBACK_STREAK_HANDOFF` → 改走
   `handoff` / `repeated_fallback`（意图 unknown）。
6. 出门前 `check_reply`（observations 为空、kb_doc_ids = 本轮读回的命中）；不 ok →
   落 CsReplyRejected，改走兜底 + `handoff` / `unverified_claim`。

触发词最低覆盖（T169 的词表必须认，T170 出题用这些或其自然变体）：

| 原因 | 至少认 |
| :-- | :-- |
| requested | 转人工、人工客服、找人工、真人 |
| complaint | 投诉、12315、消协、曝光、起诉、律师 |
| anger | 垃圾、骗子、气死、滚、连续三个及以上感叹号 |
| compensation | 赔偿、补偿、赔钱、赔我 |
| privacy | 手机号、身份证、住址、个人信息、隐私 |

### 1.5 话术库（T168 写内容，编号目录在此冻结）

kb_doc 行：`kind='cs_script'`、`biz_type='cs'`、`tenant_id='tnt-demo'`、
`doc_id = f"kb-cs-{tenant_id}-{方案编号}"`、`rule_no = 方案编号`、`title = 适用场景`、
`body` = JSON `{scheme_no, scene, principle, script, intent, handoff, synonyms[], examples[], synthetic: true}`、
其余维度 NULL、`embedding` NULL。**标明「合成」**：ADP 课程原文不在本环境，话术全部自写，
格式照 ADP 3.3（方案编号｜适用场景｜处理原则｜标准话术），不照抄课程原文。
**任何一篇的 script 都必须在空观察下过 `check_reply`**（不许出现状态字眼、不许承诺时限 / 金额 / 结果）。

| 编号 | intent | 适用场景 | handoff |
| :-- | :-- | :-- | :-- |
| LOG-001 | logistics | 下单后多久发货（发货时效） | — |
| LOG-002 | logistics | 用哪家快递、配送范围 | — |
| LOG-003 | logistics | 运费规则、包邮条件 | — |
| LOG-004 | logistics | 查某个订单的物流进度、催发货 | needs_order_lookup |
| LOG-005 | logistics | 改收货地址、改约配送 | needs_order_lookup |
| LOG-006 | logistics | 物流显示签收但本人没收到、包裹丢失或破损 | needs_order_lookup |
| PAY-001 | refund_payment | 退款多久能退回（原路退回规则，以支付渠道为准） | — |
| PAY-002 | refund_payment | 支持哪些支付方式 | — |
| PAY-003 | refund_payment | 查某笔退款的进度、钱到没到 | needs_order_lookup |
| PAY-004 | refund_payment | 支付失败、重复扣款 | needs_order_lookup |
| PAY-005 | refund_payment | 开发票 | — |
| RET-001 | return_exchange | 七天无理由退货的条件 | — |
| RET-002 | return_exchange | 退货流程怎么操作 | — |
| RET-003 | return_exchange | 质量问题换货（要提供照片） | — |
| RET-004 | return_exchange | 退货运费谁承担 | — |
| RET-005 | return_exchange | 为某个订单申请退货 / 退款（要办具体订单） | needs_order_lookup |
| GEN-001 | general | 问候 | — |
| GEN-002 | general | 感谢与道别 | — |
| GEN-003 | general | 人工客服的服务时间 | — |

### 1.6 评测集（T170 写内容，形状在此冻结）—— scenarios/cs/eval/p12_cases.json（新）

```json
{
  "_note": "…", "_provenance": {"synthetic": true, "written_by": "task-t170", "basis": "…"},
  "_thresholds": {"intent_accuracy": 1.0, "route_accuracy": 1.0,
                  "handoff_recall": 1.0, "status_fabrication_max": 0},
  "cases": [
    {"id": "CS12-001", "synthetic": true, "tags": ["logistics", "policy"],
     "turns": ["你们一般下单后几天能发出来"],
     "expect": [{"route": "answer", "intent": "logistics", "cite": "LOG-001"}]}
  ]
}
```

- `expect` 与 `turns` 等长；每项必有 `route`、`intent`；`route == "handoff"` 时必有 `reason`；
  `cite`（方案编号）可选，给了就要求该轮 `draft.citations` 含对应 doc_id。
- 可选 `open_kfid`（缺省 `wk_eval`，映射到 `tnt-demo`；给一个没映射的值就是 tenant_unmapped 用例）。
- 覆盖下限：19 个编号每个至少一次（句子**不许**抄话术库的 examples —— T170 看不到它们，天然独立）；
  五类触发词各至少两次；needs_order_lookup 至少四次；连续兜底转人工的两轮用例；
  转人工后再说话走 silent 的用例；tenant_unmapped；英文一句（p12 期望 fallback / unknown）；
  多意图按 §1.4 优先级；口语化与错别字。合计不少于 40 轮。
- 指标：`intent_accuracy` / `route_accuracy`（handoff 连同 reason 一起比）/ `handoff_recall`
  （期望 handoff 的轮里实际 handoff 的比例）/ `status_fabrication`（最终回复在空观察下
  `check_reply` 报 unbacked_status 的轮数）。门槛写在文件里、由测试断言；
  **不进** docs/expected-metrics.json（那边键集被 test_expected_metrics 精确钉住，进真源放 p14）。

### 1.7 skill（T169）—— maos/skills/builtin/cs/（新）

`cs.answer`（检索 + 组稿 + 后置校验）与 `cs.handoff`（卡片落库）。`cs/__init__.py`
**逐个显式 import**（discover 只扫一层，退款包先例）。`owner_roles=[]`（没有在池角色持有，
test_capability_profiles 的 BASELINE 与 skill-unowned 计数由 T169 同步改）。
`security_boundary` 非空。改完跑 `python3 scripts/gen_docs.py` 重产 docs/skill-catalog.md。

### 1.8 router 挂钩（T169）—— 唯一一处

```python
from maos.ingress.contracts import CHANNEL_WECHAT_KF
#: 外部渠道（客户说话的地方）。显式列举、失败即关：新渠道缺省不进。
EXTERNAL_CHANNELS = frozenset({CHANNEL_WECHAT_KF})
# IngressRouter.__init__ 末尾加 cs: Any = None → self.cs
# handle() 的 cmd is None 分支里，在 _text_reply 之前：
#   INTEGRATION-POINT: p12 客服前台
#   外部渠道 + 装了 cs + 有字 → chat_note = self._cs_turn(msg)，不再进 _text_reply
```

- `cs=None`（全仓缺省）时所有渠道**逐字节**不变；装了 cs 时内部渠道也逐字节不变。
- 命令分支一个字不动：外部 `/approve` 照旧被拒、前台不被调用。
- `_cs_turn` 永不抛；回复经 `_reply` 出门（open_kfid 只有它会带）；有卡片就经
  `_cs_deliver` 投到 `config.handoff_target`（同进程的 adapter，失败只记日志）并回写 delivery。
- wecom.py `_sync`：行里带 `origin` 且 `!= 3`（不是客户发的）就丢 —— 防机器人回自己 / 回人工。
- scripts/run_ingress.py：`MAOS_CS_TENANTS` 非空才装前台，并在启动时 `seed_cs_kb`；
  不配就与今天逐字节一致。

---

## 2. 🔴 红线（四轨共同遵守）

### R1 外部渠道零授权
前台只读、只转述、只转人工。任何动钱、动审批、动工单的路，前台一条都不许碰 ——
不 import、不按名字调、不借别人的 identity。静态守卫见 §2.3，由 T170 落地、全仓生效。

### R2 会话进度是会话对象自己的字段（铁律 9）
`cs_conversation.stage`、`cs_handoff.delivery`。不碰 `maos/contracts/**`，不加 Task 状态，
不为会话建 Plan / Task。

### R3 状态字眼必须挂本轮观察（铁律 8）
§1.4 的 `check_reply` 是唯一口径。退款状态只许说 projection.py 的五个字面值。
不许复用 `notify.py::_INTERNAL_SAID`、`run_requests.STATUS_CN`、`roundtable.verdict._clean`。

### R4 冻结面与存量
- `maos/contracts/**`、`.contracts.lock`、`docs/parallel/contracts.md`、`maos/artifacts.py`：git diff 必须空，test_contracts_frozen 必须绿。
- `maos/core/store.py` 一行不动（只新增 cs_ 表，放在 cs 自己的 schema.sql）。
- 不改 `maos/skills/builtin/__init__.py`、`maos/skills/registry.py`、`maos/ingress/chat.py`、`maos/nlu/**`、`maos/roundtable/**`。
- 不改 `docs/expected-metrics.json`、`docs/submission-checklist.md`、`evidence/**`（整合期的事）。

### R5 客户原文不进审计行
event_log 只落摘要；KbRetrieved 的 query 里客户原文换摘要。原文只住 cs_turn / 卡片。
日志里客户标识用 `mask_customer`。

### 2.3 静态守卫清单（T170 实现为测试；四轨写代码时自查）

扫描范围：`maos/domain/cs/**`、`maos/skills/builtin/cs/**`（ast.walk，函数体内 import
也算；相对 import 按包路径解析；扫到的文件集必须非空且含 types.py —— 防空转）。

- **禁 import 前缀**：`maos.runtime`、`maos.core.control_plane`、`maos.flows`、`hiclaw`、
  `maos.ingress.router`、`maos.ingress.outcome_commands`、`maos.ingress.chat`、`maos.nlu`、
  `maos.roundtable`、`maos.tools`（p12 一个工具都不用）、`maos.skills.builtin.<非 cs 的任何包或模块>`、
  `maos.agents.<除 base 外的任何>`、`maos.domain.<除 cs、_dbport、_schema_util、refund.projection 外的任何>`。
- **唯一允许的跨域 import**：`maos.domain.refund.projection`（五个对外字面值，零依赖模块）。
  `maos.domain._dbport` / `maos.domain._schema_util` 是共享底座、不是域，允许。
- 明确允许（列出来免得守卫写过头）：`maos.domain.cs.*`、`maos.kb`、`maos.kb.retriever`、`maos.core.store`、
  `maos.skills.contract`、`maos.skills.registry`（只许 `register_skill`）、`maos.skills.invoker`、
  `maos.agents.base`、`maos.ingress.contracts`、`maos.model.client`（p12 用不上，p13 要）、标准库。
- **禁字符串常量**（任何位置的 `ast.Constant` 与之**相等**即判）：`payment.execute`、`payment.observe`、
  `refund.compensate`、`refund.compensation_close`、`gateway.refund`、`gateway.query`、`order.query`、
  `claim.pay`、`claim.compensate`、`ap.execute`、`ap.compensate`、`rtv.compensate`、
  `investigation.compensate`、`bank.pay`、`payer.submit`、`supplier.rma_submit`、`carrier.ship`、`clearing.cancel`。
- **禁调用名**（属性调用或裸名调用）：`decide`、`human_decision`、`record_approval`、`run_payload`、
  `handle_execute`、`handle_gate_decision`、`_record_gate_approval`、`invoke_tool`。
- 反向验证：往扫描范围里临时放一个违规文件（测试里用 tmp 目录注入扫描函数，不落仓库），守卫必须红。

---

## 3. 测试口径（四轨统一）

- 全部测试确定性：conftest 已强制 `MAOS_FORCE_SCRIPTED=1`；p12 本来就零模型调用。不打网络，渠道用假 adapter。
- 测试文件名带轨号：`test_cs_<主题>_t16N.py`；模块级辅助函数带 `_t16N` 后缀 —— 同包并行轨会撞名。
- pytest 一律加 `--color=no`（本环境可能带 FORCE_COLOR，按行计数会恒为 0）；Bash 里的 grep 一律写 `/usr/bin/grep`
  （缺省 grep 是 ugrep，会静默跳过 ignore 目录）；判据别用管道吃退出码。
- 多行 commit message 先 Write 到文件再 `git commit -F`；Bash 命令里不要写多行内联 python（守卫 hook 解析失败会拦）。
- **本容器的预期红**（基线 393dd5b 实测，不是回归，不许去修）：
  `test_docs_guard.py::test_docs_structure_and_references_are_sound`、
  `test_rtv_sop_doc.py::test_two_docs_pass_check_docs`、`::test_check_docs_cli_still_exits_zero`
  （review/*.md 在作者 Mac 上被 exclude、本 clone 里不存在）、
  `test_room_wiring.py::test_reject_without_workdir_fails_before_assembling_anything`（写死 macOS 的 /private/tmp）、
  `test_verify_warn.py` 三条（没有 docker daemon，沙箱降级多出 trace-tree warn）。
  另加每轨恰好一条 `test_expected_metrics.py::test_collected_count_matches_source_of_truth`（收集数 +N，整合期刷）。
  **除这 8 条外全绿**才许 commit。

---

## 4. 轨与文件（零交集）

| 轨 | 波次 | 独占文件（新 = 本轨新建） |
| :-- | :-- | :-- |
| T167 会话对象 | A | maos/domain/cs/schema.sql（新）、objects.py（新）、conversation.py（新）；maos/tests/test_cs_conversation_t167.py（新）、test_cs_schema_t167.py（新） |
| T168 话术库 | A | `maos/kb/__init__.py`（加 KIND_CS_SCRIPT、进 VALID_KINDS，**不进** POSITIVE_KINDS）、`maos/kb/schema.sql`（CHECK 列表）、`maos/tests/test_kb_corpus.py`（漏斗那条改成「盖全除 cs_script 外的全部类、且不含 cs_script」）；maos/domain/cs/corpus.py（新）、scripts.py（新）；scenarios/cs/kb/cs_scripts.json（新，生成物）、cs_scripts_holdout.json（新，检索泛化用例，不入库）；scripts/gen_cs_kb.py（新，确定性、带 --check）；maos/tests/test_cs_kb_t168.py（新） |
| T170 校验与评测 | A | maos/domain/cs/claims.py（新）、evaluate.py（新）；scenarios/cs/eval/p12_cases.json（新）；maos/tests/test_cs_claims_t170.py（新）、test_cs_guard_t170.py（新）、test_cs_eval_runner_t170.py（新） |
| T169 前台与挂钩 | B | maos/domain/cs/desk.py（新）、triggers.py（新）；maos/skills/builtin/cs/**（新）；`maos/ingress/router.py`（§1.8 那一处 + 两个私有方法 + 一个 import + 一个常量）；`maos/ingress/wecom.py`（`_sync` 的 origin 过滤）；`scripts/run_ingress.py`（serve 装配）；`maos/tests/test_capability_profiles.py`（BASELINE / 计数）；`docs/skill-catalog.md`（gen_docs 重产，若 docs/agent-identity.md 也变则一并）；W-A 合流后转交：scenarios/cs/kb/cs_scripts.json 与 scripts/gen_cs_kb.py（**只许补同义词与例句**，不许抄评测句）；maos/tests/test_cs_desk_t169.py（新）、test_cs_router_t169.py（新）、test_cs_eval_p12_t169.py（新，真前台跑评测集） |

骨架（主会话，已提交、冻结）：maos/domain/cs/__init__.py、maos/domain/cs/types.py、
maos/tests/test_cs_contract_p12.py、本文件。

两本账：四轨都只在 `docs/DECISIONS.md` 与 `docs/BACKLOG.md` **尾部另起**
`## task-t16N（标题，2026-09-2x）` 小节，恰好一个表头一个分隔行，Phase 列写 `p12`，
**不改中间任何一行**。账里提到还没合入的路径写纯文本（不加反引号），否则撞 check_docs 的 E-missing。

白名单外的文件一个都不许动。发现别轨的问题、存量的问题，记本轨 BACKLOG，不当场改。
全量里有白名单外的测试因本轨改动变红：先停手，在回执里报告，不许去改那条测试。

---

## 5. 波次与整合（编排侧的事，子会话不用管）

- **W-A**（基线 = 骨架 sha）：T167、T168、T170 并行，互不 import 对方的新模块（只 import types.py）。
- W-A 合流进 integrate/p12（按轨号序；两本账按「基线原样 + 各轨块按轨序」整份重建，不照 union）。
- **W-B**（基线 = W-A 合流 sha）：T169，用真的会话表、真的检索、真的校验，端到端跑评测集。
- p12 整合收尾：刷 docs/expected-metrics.json 与 docs/submission-checklist.md 锚点、
  两本账记 `## integrate-p12`、证据重产作为最后一个提交。
- 整合点 grep：`INTEGRATION-POINT: p12`。

---

## 6. W-A 合流后的修订（2026-09-24，主会话裁定；T169 照此写）

1. **§2.3 以守卫代码为准**：`maos/tests/test_cs_guard_t170.py` 已比 §2.3 原文严（复核两轮补的）。
   T169 写 cs 代码时额外遵守：只用静态 import；identity 的 `allowed_skills` / `allowed_tools`
   写成字面量集合；扫描范围里不出现 `compile` / `exec` / `eval` 这三个名字、不对导入的模块或对象
   做 `getattr` / `vars` / `setattr` / `__dict__`、不写等于 `allowed_skills` 之类字段名的字符串、
   不 import `importlib` / `subprocess` 等动态加载入口。提交前跑这个守卫测试。
2. 守卫是**纵深防御，不追求完备**：鸭子类型的伪身份、运行时拼出来的字符串这类绕法判不到，
   记 BACKLOG（task-t170 已记）。把 cs 范围内的 `maos.*` import 改成失败即关的白名单，放 p13 的校验轨做。
3. `silent` 轮与 `tenant_unmapped` 轮的 `DeskResult.intent` 一律是 `unknown`（评测集已按此出题）。
4. 后置校验规则 3 的口径：一条有效的 `obs:` claim 撑住它的 literal 在正文里的**每一处**（现行，测试钉住）。
   p13 加「措辞与观察内容对得上」的规则时一并收紧。
5. `claims.py` 里比 `types.STATUS_PATTERNS` 更宽的补充模式留在校验器里，不升进冻结的 types；
   话术自查以**真的** `claims.check_reply` 为准 —— T169 加一条测试：每篇话术的 script 在空观察下过 check_reply。
