# 客服前台 p13 · 跨轨契约（T171 / T172 / T173 / T175，W-B：T174）

2026-09-24 立。基线 = p12 整合收尾 `c7d4a1e`（integrate/p12），p13 骨架提交在 integrate/p13。
方案：Drive/MAOS「MAOS-客服系统方案-v1」§6「p13 · 二期 ≈ 单工作流」。
**五轨照同一份定义写，谁都不许自己改口径**；要改先停手回主会话。本文件提交进版本库。
p12 契约 `review/p12-cs-contracts.md`（含 §6 修订）继续有效，本文件只写**增量**；冲突以本文件为准。

冻结的增量：`maos/domain/cs/ports.py`（新）与 `maos/domain/cs/types.py`（枚举增长、DeskResult 三个
带缺省的字段），由 `maos/tests/test_cs_contract_p13.py`（新）与 `maos/tests/test_cs_contract_p12.py`
（钉子同步改）逐字段钉住。骨架还同步改了 `maos/domain/cs/schema.sql` 的三处 CHECK 与
`maos/domain/cs/desk.py` 的三张原因表（新原因各补一条），让 p12 的测试在骨架上仍全绿。

---

## 0. 这一期买什么、不买什么

**买**：ADP 课程 3.3 单工作流的完整链路 —— 参数提取（多轮追问）→ 意图识别 → 分支策略
（话术 + **身份核验后**只读查单）→ 客户回复 + 内部处理建议；退款类诉求转成内部房间里一张
「可一键采纳」的预检卡；中英两种语言的固定话术。另外把 p12 留出集的**预登记门槛**
（intent 0.85 / route 0.80 / handoff 0.90 / 编造 0）作为 p13 理解层的验收目标。

**不买**：
- 前台自己建工单、发 `/refund`、碰 `/approve`（R1 不变：客户侧永远到不了审批与执行）；
- 已签收（平台把签收 / 完成都折成 shipped，撑不住）、金额、到账 / 送达时间；
- 英文话术库（英文政策问题走英文兜底）；评测批量化、verify 新项、MCP、圆桌（p14）。

---

## 1. 数据契约增量

### 1.1 枚举增长（types.py，骨架已提交）

- `ROUTES` 追加 `clarify`：追问必填槽位；**不**计入 fallback_streak（T171 在 record_turn 里照此处理：clarify 与 silent 一样不动 streak）。
- `HANDOFF_REASONS` 追加 `identity_unverified`、`order_unmapped`、`lookup_failed`、`refund_request`。
- `DeskResult` 末尾追加 `lang="zh"`、`lookup_outcome=""`、`ask_slot=""`。
- 为什么现在长枚举：cs_ 表至今**没有过落盘的库**（run_ingress 到 p13 才有 --db），schema.sql 的
  CHECK 对新库直接生效。T171 加一道探针：打开已存在的库、发现 CHECK 不认新值就**抛错**（不静默写失败）。

### 1.2 端口（ports.py，骨架已提交）

读 `maos/domain/cs/ports.py`：三个 Protocol（IdentityVerifier / OrderLookup / RefundPrecheck）、
三个结果类型（Binding / LookupResult / PrecheckResult）、语种 / 槽位 / 诉求 / 情绪 / 查单结果 / 观察种类 /
绑定来源的冻结枚举、订单状态**唯一**对外措辞表 `ORDER_STATUS_WORDING`、`observation_id_for`。

cs 扫描范围（`maos/domain/cs/**`、`maos/skills/builtin/cs/**`）**仍然一个工具都不碰**（守卫不放宽，
反而改成失败即关白名单，见 T175）。查单与预检的实现放 `maos/ingress/cs_ports.py`（T172，扫描范围外），
由装配处（run_ingress）**注入** FrontDesk。

### 1.3 新表（T171 独占，追加进 maos/domain/cs/schema.sql，约定同 p12）

```sql
cs_observation (tenant_id, observation_id,         -- ports.observation_id_for(turn_id, n)
  conversation_id, turn_id, kind CHECK IN ('order_lookup'),
  system_name, query_key, status CHECK IN ('paid','shipped','cancelled','amended'),
  version INTEGER, updated_at, observed_at,
  PRIMARY KEY (tenant_id, observation_id))
cs_order_binding (tenant_id, channel, external_userid, display_no, system_name, query_key,
  source CHECK IN ('seed','test','internal'), bound_at,
  PRIMARY KEY (tenant_id, channel, external_userid, display_no))
cs_slot (tenant_id, conversation_id, slot_key CHECK IN SLOT_KEYS, value, turn_id,
  source CHECK IN ('rule','model'), updated_at,
  PRIMARY KEY (tenant_id, conversation_id, slot_key))
cs_turn_ext (tenant_id, turn_id, conversation_id, lang CHECK IN ('zh','en'),
  lookup_outcome CHECK IN ('' + LOOKUP_OUTCOMES), ask_slot, ask_count INTEGER,
  PRIMARY KEY (tenant_id, turn_id))
cs_refund_bridge (tenant_id, bridge_id,             -- = turn_id
  conversation_id, turn_id, order_no, ok INTEGER, decision, rule_ref, reason_code,
  command_line, refused_why, created_at, PRIMARY KEY (tenant_id, bridge_id))
```

- 只有**成功**的查单（outcome ok 且 status ∈ paid/shipped/cancelled）落 cs_observation；不存金额、不存客户原文。
- 绑定只从内部路径写（启动种子文件、测试）；**外部渠道永远写不进绑定表**（没有任何从 InboundMessage 到 upsert_binding 的路）。
- 槽位值、订单号、query_key 不进 event_log（只落摘要或不落，R5）。
- 新表的 event_log：本期不新增事件类型；观察、绑定、槽位、桥的写入都跟着 CsTurnRecorded 那一轮，
  CsTurnRecorded 的 detail 键集由 T171 追加 `lang`、`lookup_outcome`、`observation_count`、`slot_count`
  （都是枚举或计数），T167 那条「detail 键集写死」的测试同步改。

### 1.4 各轨函数面（签名冻结）

**T171 · maos/domain/cs/records.py、identity.py（新）；schema.sql / objects.py / conversation.py（接手）**
```python
def record_observation(store, conv, *, turn_id: str, result: LookupResult, now=None) -> str
    # 只收 outcome == ok 且 status 在措辞表里的；否则 ValueError。返回 observation_id（本轮第 n 条）
def turn_observation_ids(store, *, conversation_id: str, turn_id: str) -> frozenset[str]   # 库里读回
def observations_for_turn(store, *, conversation_id: str, turn_id: str) -> dict[str, dict]  # id -> 行
def upsert_binding(store, binding: Binding) -> None            # source 必须在 BINDING_SOURCES
def load_bindings_file(store, path) -> int                     # JSON {"bindings":[{...Binding 字段}]}；返回条数
def get_slots(store, tenant_id: str, conversation_id: str) -> dict[str, str]
def set_slot(store, conv, *, key: str, value: str, turn_id: str, source: str, now=None) -> None
def record_turn_ext(store, conv, *, turn_id: str, lang: str, lookup_outcome: str = "",
                    ask_slot: str = "", ask_count: int = 0) -> None
def ask_count(store, tenant_id: str, conversation_id: str, slot_key: str) -> int  # 该会话对该槽位已追问几次
def record_bridge(store, conv, *, turn_id: str, order_no: str, result: PrecheckResult, now=None) -> None
class BindingVerifier:      # IdentityVerifier 的缺省实现：按 (tenant, channel, external_userid, display_no) 精确匹配，失败即关
```

**T172 · maos/ingress/cs_ports.py（新，扫描范围外）**
```python
class CommerceOrderLookup:        # OrderLookup
    def __init__(self, systems: Mapping[str, Any], *, transport_timeout_s: float = 5.0): ...
    def lookup(self, store, binding, *, plan_id, task_id) -> LookupResult
class LedgerRefundPrecheck:       # RefundPrecheck
    def __init__(self, ledger_path, *, ledger_tenant: str): ...
    def precheck(self, *, tenant_id, order_no, reason_text, now) -> PrecheckResult
def build_order_lookup_from_env(env=os.environ, *, ledger_path=None) -> CommerceOrderLookup | None
    # 不配就返回 None（前台照 p12 行为）；配 demo 就用 MockOrderSystem（订单来自台账）
```
- lookup 每次调用前把自己的 systems **重新登记**（`custom_case.run_payload` 会清空全局登记表），
  用 CS 自有的登记名；恰好一次 `invoke_tool(ORDER_QUERY_PORT, …)`，extras 照 p12 §1.2（plan_id=cs:…、task_id=turn_id、trace_id=""）。
- 异常翻译顺序：`UnmappedOrderStatus` → unmapped_status；其它 `ValueError` → platform_error；
  `KeyError` → not_found；其它 `LookupError` → system_misconfigured；status == amended → amended；
  其它异常 → platform_error。空 system_name / query_key 直接 system_misconfigured、不调用。
  **异常原文不出这个函数**（原文里列着别的订单号），LookupResult.error_kind 只放类名。
- 预检只在：订单在台账里、台账租户 = 会话租户、原因文本恰好命中一个原因码、时钟注入 时 ok=True，
  command_line 形如 `/refund <订单号> <原因码>`；否则 ok=False 并写 refused_why。预检复用 router 模块的只读 preflight，**不**建 Ticket。

**T173 · maos/domain/cs/lang.py、understand.py（新）；triggers.py、scripts.py（接手）；
maos/skills/builtin/cs/understand.py（新）与 cs/__init__.py（接手，只加 understand）**
```python
def detect_lang(text: str) -> str               # 确定性：有 CJK 即 zh；否则 ASCII 字母占多数即 en；否则 zh
def extract_slots(text: str, *, lang: str) -> dict[str, str]     # 确定性：订单号正则、诉求 / 情绪 / 商品 / 问题词表
@dataclass(frozen=True)
class Understanding:
    lang: str; intent: str; slots: Mapping[str, str]; source: str   # source ∈ ('rule','model')
def understand(text: str, *, prior_slots: Mapping[str, str], model=None, store=None,
               plan_id: str = "") -> Understanding
```
- `cs.understand` v1.0.0（skill）：入参 tenant_id / conversation_id / turn_id / text / prior_slots；出参 Understanding 的 JSON。
  **确定性优先**；只在确定性判不出意图、且 `ctx.extras["model"]` 是真模型（非 None、非 ScriptedModelClient）时调模型。
  调模型就：模块级 `CALL_SITE` 字面量、登记进 `maos/obs/call_sites.py` 与 `maos/tests/test_cost_metrics.py`
  的 from_source；`record_model_usage` / `record_model_failure` 用 trace_id=""、task_id=None、plan_id=extras["plan_id"]；
  模型输出夹到枚举里（意图不在 INTENTS 就 unknown）。Scripted / None 下零 model_usage 行。
- `scripts.match_scripts` 增加关键字参数 `intent_hint: str = ""`（缺省行为与 p12 逐字节一致）：
  给了就把该意图的话术优先（例如问「多久 / 几天」的政策篇与查「到没到」的查单篇分开）。
- triggers.py：修掉「复合词里含触发词子串」的误伤（本期只告诉你**类别**：商品名、地名等词里夹着情绪词的
  两个字会被误判；不许去读留出集找句子），加英文触发词（human / agent / complaint / lawyer / refund me now 之类的
  自然说法由你定），优先级不变。
- 验收目标（主会话在 p13 整合期**自己**量，不给你看逐轮）：p12 留出集上达到预登记门槛。你**不许读**
  `scenarios/cs/eval/p12_holdout_cases.json`、`maos/tests/test_cs_eval_p12_holdout.py`，也不许写任何
  打印留出集逐轮结果的脚本。只用开发集（p12_cases.json）、T168 的 cs_scripts_holdout.json 和你自己写的例句调。

**T175 · claims.py、evaluate.py、守卫（接手）；p13 评测集（新）**
```python
def check_observation_wording(draft: ReplyDraft, observations: Mapping[str, Mapping],
                              *, lang: str) -> CheckResult
    # 每条 basis 为 obs: 的 claim：literal 必须**逐字**等于 ORDER_STATUS_WORDING[lang][该观察的 status]；
    # 观察不在 observations 里 → dangling_basis；措辞对不上 → foreign_literal。
def run_eval_p13(desk_factory, cases, *, tenant_map=None) -> EvalReport   # desk_factory(ports) -> desk
```
- claims.py 加扫：「已付款 / 已支付」、英文状态说法（shipped / delivered / refunded / cancelled、has been …、will arrive …）。
- 守卫：cs 扫描范围的 `maos.*` import 改**失败即关白名单**（名单 = p12 §2.3 允许清单 + `maos.domain.cs.ports`）。
- p13 评测集 `scenarios/cs/eval/p13_cases.json`（形状见 §3），跑批用 evaluate.py 里的**夹具端口**
  （FixtureVerifier / FixtureLookup / FixturePrecheck，从 case 的 fixtures 构造）；指标在 p12 四项之外加
  `wording_accuracy`（期望 say 的轮：回复里恰有措辞表那句、且挂着有效 obs 依据）与 `wrong_status`
  （回复说的状态与夹具的状态对不上的轮数，必须 0）。

**T174（W-B）· desk.py、skills answer / handoff（接手）；router.py、run_ingress.py（接手）**：按 §2 编排，见 §4 白名单。

---

## 2. 一轮的判定顺序（p13，T174 实现、T175 出题照此推期望）

0. 语种 `detect_lang`；之后所有给客户的固定话术按语种取（中英各一套）。DeskResult.lang 照填。
1. handed_off → silent（同 p12）。
2. 租户空 → handoff / tenant_unmapped（同 p12）。
3. 触发词（同 p12 优先级；英文词在同一张表里）。
4. 理解：`cs.understand` 出槽位与意图；槽位并进 cs_slot（跨轮累积，新值覆盖旧值）。
5. **没注入端口**（verifier / lookup / precheck 全为 None）→ 走 p12 的第 4–6 步，逐字节同 p12。
   这条保证 p12 的开发集、留出集、全部 p12 测试**不改一个期望**仍全绿。
6. 注入了端口、且意图是「要看具体订单」（查物流、查退款进度、要退款 / 退货 / 换货）：
   a. 缺 order_no → `clarify` 追问（ask_slot=order_no）；同一槽位已追问 `MAX_ASKS_PER_SLOT` 次仍缺 → handoff / needs_order_lookup。
   b. `verifier.resolve(...)` 查不到 → handoff / identity_unverified（**不查单**）。
   c. `lookup.lookup(...)`：ok 且状态在措辞表 → 落 cs_observation → `answer`，正文 = 措辞表那句，
      claim = (那句, `obs:<本轮观察 id>`)；amended / unmapped_status → handoff / order_unmapped；其它 → handoff / lookup_failed。
      DeskResult.lookup_outcome 照填。
   d. 诉求是 refund / return 且 c 成功 → `precheck.precheck(...)`；ok → 落 cs_refund_bridge、handoff / refund_request
      （卡片的 suggestion 带预检摘要与 command_line）；不 ok → 落 cs_refund_bridge、handoff / needs_order_lookup（卡里写 refused_why）。
      给客户的只有 refund_request 的过渡话术，**一个状态字都不说**。
7. 其余（政策问题等）同 p12 第 4–6 步；检索时把 understand 的意图作为 `intent_hint` 传给 match_scripts。
8. 出门前两道校验：`check_reply(observations=turn_observation_ids 读回, kb_doc_ids=turn_kb_doc_ids 读回)` 与
   `check_observation_wording(observations=observations_for_turn 读回, lang)`；任一不过 → CsReplyRejected + 兜底 + unverified_claim。
9. 每轮 record_turn + record_turn_ext（lang、lookup_outcome、ask_slot、ask_count）。

---

## 2'. 红线增量

- **R1 更严**：装了前台时，外部渠道的**所有**消息（含 `/xxx`、含附件）都先进前台，不再进命令分支与附件入库
  （收掉 p12 BACKLOG 第 1 条外泄）；`cs=None` 时逐字节不变。前台不建工单、不发命令、不碰审批；
  退款桥只出卡，由内部同事以自己的名义发 `/refund`。
- **R3 增量**：订单状态只许说措辞表里那三句，且必须挂**本轮**观察；已签收、金额、时间永远不说。
- **R5 增量**：槽位值、订单号、query_key 不进 event_log；查单异常原文不出 cs_ports。
- **留出集保持盲**：任何实现轨、复核者都不许读 `scenarios/cs/eval/p12_holdout_cases.json` 与
  `maos/tests/test_cs_eval_p12_holdout.py`，也不许写打印它逐轮结果的东西；它的测试失败消息只给聚合数与 case id。
  留出集由主会话在整合期量一次。

## 3. p13 评测集形状（T175 写、T174 用真前台跑）

```json
{"_note": "…", "_provenance": {"synthetic": true, "written_by": "task-t175", "set": "dev"},
 "_thresholds": {"intent_accuracy": 0.9, "route_accuracy": 0.9, "handoff_recall": 0.95,
                 "status_fabrication_max": 0, "wording_accuracy": 1.0, "wrong_status_max": 0},
 "cases": [{"id": "CS13-001", "synthetic": true, "tags": [...], "open_kfid": "wk_eval",
   "fixtures": {"bindings": [{"display_no": "A1001", "system_name": "demo-orders", "query_key": "A1001"}],
                "orders": {"A1001": {"outcome": "ok", "status": "shipped"}},
                "precheck": {"A1001": {"ok": true, "decision": "approve", "reason_code": "quality"}}},
   "turns": ["我那单 A1001 发了没"],
   "expect": [{"route": "answer", "intent": "logistics", "lang": "zh", "lookup": "ok", "say": "shipped"}]}]}
```
- 绑定一律挂在跑批的客户（`eval-<case.id>`）名下；expect 可选键 lang / lookup / ask（=ask_slot）/ say（=状态）/ cite / reason。
- 覆盖下限：五种查单结果各 ≥2、追问（含追问两次后转人工）、身份核验失败、退款桥 ok 与 refused、英文查单与英文兜底、
  多轮里先问政策后查单、槽位跨轮累积（先说单号后说诉求）、p12 式（不注入端口）用例 ≥5；合计 ≥ 50 轮。
- 这是**开发集**（T174 会拿它调）；泛化由 p12 留出集与 p14 的新留出集量。

## 3'. 测试口径增量

- 查单一律 MockOrderSystem 或 FakeTransport；预检一律测试台账；时钟一律注入。
- 模型路径用桩客户端（非 Scripted 的假「真模型」）测；Scripted / None 下零 model_usage 行。
- p12 的全部测试不许改一个期望值；要改的只有本契约点名的钉子（T167 的 detail 键集、T169 路由测试里被挂钩前移改到的那几条）。
- 反向验证的变异体各用独立 PYTHONPYCACHEPREFIX（integrate-p12-w-a BACKLOG）。

## 4. 轨与文件（零交集）

| 轨 | 波次 | 独占（新 = 本轨新建；接手 = 上一期别轨的文件转交给你） |
| :-- | :-- | :-- |
| T171 存储与绑定 | A | maos/domain/cs/schema.sql、objects.py、conversation.py（接手）；records.py、identity.py（新）；maos/tests/test_cs_schema_t167.py、test_cs_conversation_t167.py（接手，只许改被本轨改到的钉子）；maos/tests/test_cs_records_t171.py（新） |
| T172 外部端口 | A | maos/ingress/cs_ports.py（新）；maos/tests/test_cs_ports_t172.py（新） |
| T173 理解层 | A | maos/domain/cs/lang.py、understand.py（新）；triggers.py、scripts.py（接手）；maos/skills/builtin/cs/__init__.py（接手，只加 understand）、understand.py（新）；maos/obs/call_sites.py 与 maos/tests/test_cost_metrics.py（只加 CS 这一个 call site）；maos/tests/test_capability_profiles.py（skill-unowned +1）；gen_docs 生成文档；scripts/gen_cs_kb.py 与 scenarios/cs/kb/cs_scripts.json（接手：只许补同义词与例句，棘轮照旧）；maos/tests/test_cs_understand_t173.py（新）；T168 / T169 测试里被本轨改到的钉子（只许改钉子） |
| T175 校验与评测 | A | maos/domain/cs/claims.py、evaluate.py（接手）；maos/tests/test_cs_guard_t170.py、test_cs_claims_t170.py、test_cs_eval_runner_t170.py（接手）；scenarios/cs/eval/p13_cases.json（新）；maos/tests/test_cs_wording_t175.py、test_cs_eval_runner_t175.py（新） |
| T174 前台编排 | B | maos/domain/cs/desk.py（接手；加 cs.understand 进 identity）；maos/skills/builtin/cs/answer.py、handoff.py（接手）；maos/ingress/router.py（挂钩前移、退款桥卡片与转人工卡片投递）；scripts/run_ingress.py（MAOS_INGRESS_DB 持久化、MAOS_CS_BINDINGS 种子、端口与模型装配）；maos/tests/test_cs_desk_t174.py、test_cs_router_t174.py、test_cs_eval_p13_t174.py（新）；T169 测试里被挂钩前移改到的钉子（只许改钉子） |

骨架（主会话，已提交）：maos/domain/cs/ports.py、types.py 增量、schema.sql 三处 CHECK、desk.py 三张原因表各补四条、
maos/tests/test_cs_contract_p13.py、test_cs_contract_p12.py 的钉子、test_cs_eval_p12_holdout.py 的失败消息只报聚合、本文件。

两本账同 p12：尾部另起 `## task-t17N（标题，2026-09-2x）`，Phase 写 p13。

## 5. 波次与整合

- **W-A**（基线 = p13 骨架 sha）：T171 / T172 / T173 / T175 并行，互不 import 对方的新模块（只 import types.py / ports.py）。
- W-A 合流 → **W-B**（基线 = W-A 合流 sha）：T174，用真存储、真端口（MockOrderSystem）、真理解层、真校验端到端跑 p13 评测集，
  并保证 p12 开发集与 p12 全部测试照旧全绿。
- 整合期：主会话量 p12 留出集（预登记门槛）→ 刷真源 → 两本账记 `## integrate-p13` → 快进会话分支并推送。
  证据束重产照 p12 口径留给作者 Mac。
