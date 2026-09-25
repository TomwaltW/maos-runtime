# 客服前台 p14 · 跨轨契约（W-A1：T176 / T177 / T181 / T182，W-A2：T178 / T179 / T180）

写于 2026-09-25，基线 = integrate/p13 收尾 d842457 + 本期骨架提交。本文件是 p12 契约
（review/p12-cs-contracts.md）与 p13 契约（review/p13-cs-contracts.md）的**增量**：前两份照旧有效，
冲突以本文件为准。方案出处：MAOS-客服系统方案-v1 §6「p14 · 三期」。

p14 买四样东西，外加一笔 p13 欠账：

1. **可核验**：verify 认得 cs 家族（`cs:` plan_id），并可选（`--cs`）核一项 cs/claim-basis —— 回复里的每句
   状态断言都回查得到本轮观察行、措辞逐字等于措辞表、引用的话术本轮确实被检出过。**缺省 RESULT 仍 10/10**。
2. **评测批量化**：一条命令跑开发集 / 留出集，出聚合报告，并能把整批落进一个库给 verify --cs 核。
   另盲写一份覆盖 p13 流程（查单、追问、退款桥、英文）的新留出集。
3. **复杂投诉走圆桌**：转人工原因在 `CONFERENCE_REASONS` 里时，投递转人工卡片之后再出一张**确定性**会诊卡
   （固定座次、固定交接条件 —— 课程 4.5 的「工作流编排」，不是自由转交；零模型）。
4. **方案 B**：把查单 / 退款预检 / 转人工列表包成**只读** MCP 连接器（stdio JSON-RPC，形状照 maos/tools/mcp/server.py）。
5. **运营面**：`scripts/cs_stats.py` 出高频意图 / 转人工原因 / 查单结果 / 兜底率等统计，只出计数与 id，不出原文。
6. **欠账**：p12 留出集预登记门槛 p13 没达到（intent 48/70、route 45/70、handoff 28/38）。T182 只拿**误判类别**做理解层泛化。

不买：新渠道、新业务写路径、任何客户侧能触发的执行、expected-metrics.json 的新键（整合期主会话定）、证据束默认集
（仍 8 个，不加 scenario）。

-----

## 0. 硬约束（每轨照抄）

* 铁律 1 / 8 / 9 照旧：`maos/contracts/**`、`.contracts.lock`、`docs/parallel/contracts.md`、`maos/artifacts.py` 不碰；
  store.py 表结构不动；**p14 不新增任何表**。会诊卡只落 event_log 一行 + 投递。
* `maos/domain/cs/**`、`maos/skills/builtin/cs/**` 仍在静态守卫范围（test_cs_guard_t170.py，失败即关白名单）；
  本期新代码除 T182 外**全部在守卫范围外**（roundtable/、tools/mcp/、scripts/），可以 import cs 模块，反之不行。
* **留出集是盲的**：除主会话与 T181 外，任何实现者 / 复核者不许读 `scenarios/cs/eval/p12_holdout_cases.json`、
  `scenarios/cs/eval/p14_holdout_cases.json` 与 `maos/tests/test_cs_eval_p1[24]_holdout.py`。脚本可以按路径常量
  加载它们，但**只许输出聚合数与 case id**，永不输出句子。
* 客户原文（inbound_text / reply_text）、external_userid、query_key、订单号 **不进** event_log、不进 stats 输出、
  不进 MCP 出参（MCP 出参里订单号只回客户自己给的 display_no 原样）。
* 核心零依赖；测试不打真网、Scripted 模型；密钥只读环境变量。

## 1. 数据契约增量（骨架已提交，主会话）

### 1.1 types.py 增量

```python
EVENT_CONFERENCE_HELD = "CsConferenceHeld"        # 不进 CS_EVENT_TYPES
CONFERENCE_REASONS = frozenset({"complaint", "anger", "compensation", "refund_request"})
CS_MCP_PLAN_ID = "cs:mcp"
```

钉子：maos/tests/test_cs_contract_p14.py。

### 1.2 cs 家族（T176 定义判据，其余轨照此落行）

一条 event_log 行属于 cs 家族 ⇔ `plan_id` 以 `cs:` 开头。家族内两种 plan_id：

| plan_id | 谁落 | task_id | trace_id |
| :-- | :-- | :-- | :-- |
| `cs:<会话 id>`（会话 id 以 `csc-` 开头） | 会话对象（p12/p13 已有）、cs.* skill、查单 ToolInvoked、**会诊卡（T178）** | 轮次 id `<会话>-tNNNN` | `""` |
| `cs:mcp` | MCP 连接器（T179）的查单 ToolInvoked | `mcp-<序号或请求 id>`，非空 | `""` |

## 2. 各轨函数面（签名冻结；内部实现自定）

### T176 · verify 认 cs 家族 + `--cs` 可选核验（scripts/verify.py、maos/obs/trace.py）

* `maos/obs/trace.py`：trace.json 里 cs 家族行归到新键 `cs_traces`（按 plan_id 分组：
  `[{"plan_id", "events":[…], "model_usage":[…]}]`，结构照 `roundtable_traces`）。**库里没有 cs 行时 trace.json
  逐字节不变**（不出现 `cs_traces` 键）—— 现有 8 个证据束因此不受影响。
* `scripts/verify.py`：
  * 第 4 项 / stray 口径：cs 家族行不再算 stray（与 `roundtable:` 同待遇）；cs 行进 `cs_traces` 且恰好一处。
  * 第 8 项：`model_usage` 里 trace_id 空、出现在 `cs_traces[].model_usage` 的行算已归属（与圆桌同口径）。
  * 新 CLI 旗 `--cs`：照 `--domains` 的先例**追加**计分项 `cs/claim-basis`（缺省不跑；`len(CHECKS) == 10` 不变；
    缺省 RESULT 仍 `10/10`）。数据源：`--db`（给了就用）否则各证据束的库；没有 cs_ 表 → SKIP。
  * `cs/claim-basis` 的判据（每条都要有反向用例证明能判负）：
    1. 每个 `cs_turn` 行都恰有一条 `CsTurnRecorded`（同 tenant 的 plan_id `cs:<会话>`、task_id = turn_id），反之亦然；
    2. `text_digest(reply_text) == CsTurnRecorded.detail.reply_digest`（业务表与审计行对得上）；
    3. draft_json 里每条 claim 的 `obs:<id>` 在 `cs_observation` 里存在、且那行的 `turn_id` 就是本轮；
    4. 该 claim 的 literal 逐字等于 `ports.ORDER_STATUS_WORDING[观察的 status][本轮 lang]`（lang 取 cs_turn_ext，缺行按 zh）；
    5. draft_json 里每条 `kb:<doc_id>` 引用都在本轮（同 plan_id、task_id）的 `KbRetrieved` 命中里；
    6. route=answer 的轮，reply_text 里 `STATUS_PATTERNS` 扫到的每个状态词都被某条 claim 覆盖。
    verify 可以 import `maos.domain.cs.types` / `maos.domain.cs.ports`（纯数据），**不许** import claims.py / desk.py
    去复用前台自己的校验（同源对账等于没核）。
* 测试：maos/tests/test_verify_cs_t176.py（新）。

### T177 · 评测批量化（scripts/cs_eval.py）

```
python3 scripts/cs_eval.py [--set dev12|dev13|holdout12|holdout14|all] [--db PATH] [--out FILE] [--json]
```

* dev12 → `evaluate.run_eval` 跑 p12_cases.json；dev13 → `run_eval_p13` 跑 p13_cases.json（夹具端口）；
  holdout12 → 两条路径都跑（p12 路径不注入端口、p13 路径注入空夹具端口）；holdout14 → `run_eval_p13`
  跑 p14_holdout_cases.json（文件不存在 → 该集 SKIP 并说明，不报错）。门槛取各文件 `_thresholds`。
* 每集输出：各指标、`meets`、没对上的轮**只列 id**（holdout 集与 dev 集同口径 —— 一律不出句子）。
* `--db PATH`：该次运行的所有前台共享一个 `SqliteStore(PATH)`（种子话术库幂等），跑完后
  `python3 scripts/verify.py --cs --db PATH` 的 cs/claim-basis 应当 PASS（T177 的测试不依赖 T176；
  整合期主会话跑这条组合）。
* `--out FILE`：写 JSON 报告，首行 `# generated at <ISO8601> from <git sha>`（口径同 make_evidence.header_line，
  可 import scripts/make_evidence 的函数或逐字同实现）。
* 退出码：所选集全部 meets → 0；否则 1；用法错 → 2。**p12 留出集当前不达标是已知事实**，所以 `--set all` 现在退 1。
* 测试：maos/tests/test_cs_eval_cli_t177.py（新）；测试里跑 holdout 集只断言「只出 id 不出句子」这类形状，
  不断言读数（读数由主会话在留出集测试里钉）。

### T178 · 复杂投诉圆桌会诊卡（maos/roundtable/cs_conference.py）

```python
@dataclass(frozen=True)
class SeatFinding:
    seat: str                 # 固定座次之一：SEATS
    summary: str              # 内部可读的一两句（中文）；只用库里的事实，不推测
    basis_refs: tuple[str, ...]   # obs:<id> / kb:<doc_id> / turn:<turn_id> / bridge:<bridge_id> / slot:<key>
    flags: tuple[str, ...] = ()   # 该座的风险 / 待补标记（枚举自定，测试钉）

@dataclass(frozen=True)
class ConferenceCard:
    tenant_id: str
    conversation_id: str
    turn_id: str
    handoff_id: str
    reason: str               # ∈ CONFERENCE_REASONS
    seats: tuple[SeatFinding, ...]    # 恰好按 SEATS 顺序，每座一条
    recommendation: str       # 建议动作（枚举，见下）
    open_questions: tuple[str, ...]   # 人工接手前要补问的（槽位缺什么、查单失败原因等）

SEATS = ("intake", "order", "policy", "risk")
RECOMMENDATIONS = ("send_refund_command", "supervisor_review", "callback_soothe", "verify_identity",
                   "manual_lookup", "standard_followup")

def should_convene(result: DeskResult) -> bool             # route=handoff 且 handoff_reason ∈ CONFERENCE_REASONS 且有卡片
def convene(store, result: DeskResult, *, now: str) -> ConferenceCard   # 纯读 cs_ 表 + 落一行 CsConferenceHeld；不抛就返回
def render_conference_text(card: ConferenceCard) -> str     # 投递到内部房间的文本
```

* 座次与交接条件（编排，不是自由转交）：intake（槽位全集、缺失、轮数、语种）→ order（本会话的观察与查单结果；
  没查过就写「未查单」）→ policy（本会话引用过的话术、退款桥那行的 decision / rule_ref / refused_why；
  有 command_line 就原样带上）→ risk（原因、情绪槽、连续兜底、追问次数、赔偿 / 曝光类说法）。
  recommendation 由一张**纯函数决策表**从四座的 flags 得出，测试逐格钉。
* 会诊卡**只对内**：客户那一轮的回话一个字不变。不调模型、不调工具、不写 cs_ 表。
* event_log：`event_type=CsConferenceHeld`，plan_id `cs:<会话>`，task_id 本轮，trace_id `""`，detail 只放
  `{handoff_id, reason, seats:[{seat, basis_refs, flags}], recommendation, open_question_count}` —— 不放 summary 原文。
* 挂钩：maos/ingress/router.py 的 `_cs_turn` 在 `_cs_deliver(card)` 之后，`should_convene(res)` 为真就
  `convene` + 投到同一个 handoff_target（投递规则照 `_cs_deliver`：外部渠道 / 无 adapter / 未配置一律不发；
  会诊失败只记日志，**永不影响客户回话**）。cs=None 或原因不在集合里时 router 行为逐字节不变。
* 测试：maos/tests/test_cs_conference_t178.py（新）；router 相关判据可放同文件。

### T179 · 只读 MCP 连接器（maos/tools/mcp/cs_server.py）

```
python3 -m maos.tools.mcp.cs_server --db PATH [--tenant-map JSON]
```

* 协议与分帧复用 maos/tools/mcp/protocol.py（同 server.py：initialize / tools/list / tools/call，一行一帧，64 KiB 上限）。
* 三个工具（全部只读；唯一的写是查单端口本身落的 ToolInvoked 审计行，plan_id `cs:mcp`）：
  * `cs_order_status{tenant_id, channel, external_userid, display_no, lang?}` → 先 `BindingVerifier.resolve`，
    不过 → `{"outcome": "identity_unverified"}`；过了再用装配出的 `OrderLookup` 查，返回
    `{"outcome", "display_no", "wording"}`，wording 只取 `ports.ORDER_STATUS_WORDING`（查不成 / 不在表里 → 空串）。
    **不回** query_key、金额、时间、平台原始状态。
  * `cs_refund_precheck{tenant_id, channel, external_userid, display_no, reason_text}` → 绑定 + 查单 ok 之后跑
    `RefundPrecheck`，回 `{"ok", "decision", "rule_ref", "reason_code", "refused_why"}`。**不回 command_line**
    （那一行只给内部同事，不给外部平台）。
  * `cs_handoff_list{tenant_id, limit?}` → 最近的转人工卡片 `{handoff_id, conversation_id, reason, delivery, created_at}`，
    不回卡片正文、不回客户标识。
* 装配照 scripts/run_ingress.py 的口径：`build_order_lookup_from_env` / `MAOS_CS_LEDGER_TENANT`；没配查单 →
  前两个工具回 `{"outcome": "system_misconfigured"}`，不抛。
* 注册：maos/tools/mcp/registry.py 的 `SERVERS` 加一条 `readonly=True` 的 spec；`DEFAULT_ROLE_SERVERS` 不动
  （不给任何 agent 角色自动挂上）。`test_every_registered_server_is_readonly` 必须照旧绿。
* 测试：maos/tests/test_mcp_cs_server_t179.py（新）；registry 那条测试若钉了条数，只改钉子。

### T180 · 运营统计（scripts/cs_stats.py）

```
python3 scripts/cs_stats.py --db PATH [--tenant T] [--since ISO8601] [--top N] [--json]
```

* 只读打开（`file:…?mode=ro`，同 replay_roundtable.py）；库里没有 cs_ 表 → 输出全零并说明，退出 0。
* 输出：会话数（按 stage）、轮数、route 分布、意图 top-N、转人工原因分布、兜底率 / 转人工率、查单结果分布、
  追问按槽位、后置校验拦下按 violation kind、引用话术 top-N（doc_id）、会诊卡数与 recommendation 分布
  （读 CsConferenceHeld；没有就 0）。
* **只出计数与 id**：不出 inbound_text / reply_text / external_userid / open_kfid / query_key / order_no。
* 测试：maos/tests/test_cs_stats_t180.py（新；含「库里塞了哨兵原文，输出里一个都不出现」的反向用例）。

### T181 · p14 盲写留出集（scenarios/cs/eval/p14_holdout_cases.json）

* 形状同 p13_cases.json（`run_eval_p13` 读得动：fixtures / turns / expect / `_thresholds`），覆盖 p13 流程：
  查单 ok（三种状态中英）/ not_found / amended / 平台不映射 / 系统没配 / 身份不过、追问两次仍缺单号、
  退款诉求（桥 ok 与拒）、英文、多轮混合。≥ 40 个 case、≥ 90 轮；全部 `"synthetic": true`。
* 预登记门槛写进文件（**一经提交不许改**）：intent 0.85 / route 0.85 / handoff 0.90 / fabrication 0 /
  wording 1.0 / wrong_status 0。
* **盲写**：T181 可以读本契约、ports.py、types.py、evaluate.py 的 case 形状与夹具说明、p13 契约 §2 的判定顺序；
  **不许读** understand.py / triggers.py / lang.py / desk.py / scripts.py、话术库 cs_scripts.json、两份开发集
  （p12_cases.json / p13_cases.json）、p12 留出集。期望照契约的判定顺序推，不照实现推。
* 测试：maos/tests/test_cs_eval_p14_holdout.py（新）—— 只做形状、覆盖地板、门槛钉死、与开发集 / 话术库的
  **近重复棘轮**（失败消息只报 id）。**不跑真前台**：真前台读数由主会话整合期接上。

### T182 · 理解层泛化（p13 欠账）

* 目标：p12 留出集（主会话量，实现者看不到）达到预登记门槛；**不许**把开发集或安全面做坏：
  开发集 p12 / p13 全绿照旧、编造 0、零「自信答错」、触发词召回不低于 p12 地板（R1）。
* 只给误判**类别**（主会话从留出集归纳，不给句子）：
  1. **寒暄开场的口语变体**（语气词、叠字、波浪号、「打扰一下」一类）落了兜底，应走 general 寒暄话术。
  2. **具体订单的进度 / 异常，不含「发货 / 物流 / 退款」这些关键词的说法**：包裹卡在中转站多天不更新、下单后要改收货地址、
     显示已签收但本人没收到、拆开发现破损、商家同意退款但钱没到、重复扣款 —— 这些都要看具体订单
     （p12 路径：needs_order_lookup 转人工；p13 路径：进查单 / 追问单号），现在全落兜底。
  3. **政策问答的口语说法**：七天无理由（吊牌还在 / 拆了外膜还能不能退）、退货流程先后步骤、退货运费谁出、
     质量问题（洗后掉色）能不能换；配送范围与快递公司选择、大件走什么快递、最快哪天发出；退款原路退回到哪、
     钱包支付要多久 —— 话术库里有对应篇，检索召回不到。
  4. **辱骂客服质量**的说法（「破客服」「气疯」一类）没识别成情绪激烈。
  5. 以上落兜底之后，连续兜底两轮就 repeated_fallback 转人工，把后面该答的轮也连带打成 silent —— 修好 1–4 自然消解，**不要**改连续兜底规则。
* 手段限定：同义词 / 例句 / 说法表的**泛化**扩充（按类别写你自己想得到的多种说法，不是凑某几句）、理解层的进度 / 异常线索、
  话术检索的同义归一。话术库扩充照 T173 的棘轮（scripts/gen_cs_kb.py 重产，近重复守卫照旧）。
* 自测只用开发集与你自己按类别写的新例句（写进你自己的测试文件）；留出集的测试会在全量里跑，
  失败消息只有聚合数与 id —— **不许**据此逐个 id 反推句子去凑。
* 顺手两件（BACKLOG integrate-p13）：`lang.py` 公开一个 `has_lang_signal` 同义的公开函数、desk.py 改用它；
  「不 X，我就曝光 / 投诉到平台」类条件威胁识别成 complaint（只认真后果动作，照 T174 复核口径）。

## 3. 测试口径增量

* 每轨新测试文件以 `_t17N` / `_t18N` 结尾命名测试函数（全仓同名冲突守卫照旧）。
* 全量 `python3 -m pytest maos/tests -q`：容器里允许的红只有那 7 条环境性红 + 收集数那 1 条（整合期主会话刷）。
* `python3 scripts/gen_docs.py --check` 绿（改了 skill 契约就重产）。

## 4. 轨与文件（零交集）

| 轨 | 波次 | 独占 |
| :-- | :-- | :-- |
| T176 verify cs 家族 | A1 | scripts/verify.py、maos/obs/trace.py；maos/tests/test_verify_cs_t176.py（新）；现有 verify / trace 测试里被本轨改到的钉子（只许改钉子） |
| T177 评测批量化 | A1 | scripts/cs_eval.py（新）；maos/tests/test_cs_eval_cli_t177.py（新） |
| T181 p14 留出集 | A1 | scenarios/cs/eval/p14_holdout_cases.json（新）；maos/tests/test_cs_eval_p14_holdout.py（新） |
| T182 理解层泛化 | A1 | maos/domain/cs/understand.py、triggers.py、lang.py、scripts.py、desk.py（只许改 has_lang_signal 那一处）；scripts/gen_cs_kb.py、scenarios/cs/kb/cs_scripts.json；maos/tests/test_cs_understand_t182.py（新）；T168 / T173 / T174 测试里被本轨改到的钉子（只许改钉子）；gen_docs 生成文档 |
| T178 圆桌会诊卡 | A2 | maos/roundtable/cs_conference.py（新）；maos/ingress/router.py（只许动 `_cs_turn` 与新增的会诊投递函数）；maos/tests/test_cs_conference_t178.py（新） |
| T179 MCP 连接器 | A2 | maos/tools/mcp/cs_server.py（新）、maos/tools/mcp/registry.py；maos/tests/test_mcp_cs_server_t179.py（新）；test_mcp_registry.py 的钉子（只许改钉子） |
| T180 运营统计 | A2 | scripts/cs_stats.py（新）；maos/tests/test_cs_stats_t180.py（新） |

骨架（主会话，已提交）：types.py §1.1 增量、maos/tests/test_cs_contract_p14.py、本文件。

两本账同 p13：尾部另起 `## task-t1NN（标题，2026-09-25）`，Phase 写 p14。

## 5. 波次与整合

* A1（T176 / T177 / T181 / T182）与 A2（T178 / T179 / T180）文件零交集；A2 从 A1 合流后的 integrate/p14 开。
* 整合期主会话：量 p12 留出集（两条路径）与 p14 留出集（接上真前台读数与地板）；`cs_eval.py --set dev13 --db X`
  之后 `verify.py --cs --db X` 必须 PASS；刷 expected-metrics 与锚点；写 integrate-p14 两本账；快进会话分支推送。
