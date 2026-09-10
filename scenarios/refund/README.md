# 退款域语料与对照数据集（task-W1）

供 P5 的 RAG 对照实验（W-3 轨）与三组对照 case 使用。**本目录只有数据，没有代码，也没有任何 DDL** ——
这一条至今成立（本目录下仍然一个 `.py`、一行 `CREATE TABLE` 都没有）。**但「没有消费方」那一条已经不成立了**：
T7 轨把五份 case 接上了 `maos/flows/contrast.py`，24 条历史案例接上了 `maos/domain/refund/fixtures.py`。
详见文末的「谁在消费它」。

## 数据来源与口径

> 政策规则与历史案例为**按行业惯例构造的合成数据**；支付回执与错误码取自**支付宝开放平台 `alipay.trade.refund` 官方错误码表**（出处见 `maos/tools/gateway_codes.py` 里的 `SOURCES` / `SRC_REFUND_API` 常量，那里记着官方文档来源）。

这段话请原样抄进复赛材料。**不要把合成数据说成真实企业数据** —— 这是最容易被评委问穿、也最伤的一处。

具体分界线：

| 内容 | 性质 | 依据 |
| :-- | :-- | :-- |
| 租户 / 渠道 / 商品 / 订单快照 | 合成 | 按行业惯例构造 |
| AS-001..AS-004 政策规则与其两个版本 | 合成 | 按行业惯例构造，字段对齐 `policy_rule` 表 |
| 24 条历史案例的情节与处置 | 合成 | 按行业惯例构造 |
| 案例里出现的**网关错误码**及其 `outcome` / `retriable` / `message` / `remedy` / `layer` / `source` | **非合成** | 逐字段取自 `maos/tools/gateway_codes.py`，该模块每条码都核过官方出处 |
| `kb/error_code_playbooks.json` 的 `body.gateway` 七个字段 | **非合成** | 同上，由 `scripts/gen_refund_kb.py` 程序化搬运，有测试逐字段比对 |
| `kb/task_patterns.json` 的步骤形状与依赖边 | **非合成** | 逐字段取自 `flows/scenario_6`、`flows/scenario_7`、`flows/contrast` 的真实 DAG |
| `kb/{rejections,comms_results,arrival_results}.json` 里租户 `tnt-demo` 那部分的单号 / 金额 / 时点 | **非合成** | 取自 `scenarios/bulk/ledger-bulk.json` 的 60 单底账 |
| 同上三份里的**处置理由 / ack 情况 / 轮询次数** | 合成 | 底账只给了 `status`，理由按 `gen_refund_kb.py` 里写死的规则从订单事实推导 |
| 两个 mfg 租户的驳回 / 沟通 / 到账案例、政策的渠道与品类差异 | 合成 | 按行业惯例构造 |

错误码那一栏是**程序化搬运**进来的，不是人工转写 —— 手打就等于「凭记忆写」，而凭记忆写正是这张表存在的理由所要防的事。可以当场核：

```bash
# 语料里用到的码 ⊆ ALL_CODES（差集必须为空）
python3 -c "import json,maos.tools.gateway_codes as g; d=json.load(open('scenarios/refund/history/history_cases.json'))['kb_doc']; u={x['gateway_code'] for x in d if x['gateway_code']}; print(sorted(u)); print('diff:', sorted(u-set(g.ALL_CODES)))"
```

## 文件

```
scenarios/refund/
  README.md                      本文件
  policy/policy_rules.json       16 行 policy_rule（AS-001..004 × 租户 A/B × v1/v2）
                                 + 被引用的 tenant / channel / product_snapshot
  history/history_cases.json     24 条 kb_doc（8 条 outcome=failed，正好 1/3）
                                 T118 起失败那 8 条的 kind 直接标 failure_hint，
                                 workflow_version 整数化（1.0.0 -> 1、1.1.0 -> 2）
  kb/*.json                      T118 生成的九类流程知识，93 条，勿手改
                                 （改数据请改 scripts/gen_refund_kb.py 再重跑）
  cases/case_r3a.json            对照组 R3 · 租户维度 · 租户 A → approve
  cases/case_r3b.json            对照组 R3 · 租户维度 · 租户 B → reject
  cases/case_r4a.json            对照组 R4 · 渠道维度 · 自营
  cases/case_r4b.json            对照组 R4 · 渠道维度 · 经销（多一个核销任务）
  cases/case_r6.json             对照组 R6 · 政策版本维度 · 快照 v1 vs 最新 v2
  cases/case_real_01.json        单案例包 · **十类业务对象齐备**（T116，非对照）
```

`case_real_01.json` 与前五份的分工是硬的：那五份各自只放一个变量、带 `_expected` 判据、由 `flows/contrast.py` 跑，回答「结论对不对」；它不做对照，回答的是「**同一个业务案例，十类对象长什么样、怎么串起来**」。加载与校验在 `maos/domain/refund/case_pack.py`，入口：

```bash
python3 scripts/run_case.py scenarios/refund/cases/case_real_01.json
python3 scripts/run_case.py scenarios/refund/cases/case_real_01.json --drift     # 注入一次外部改单
python3 scripts/run_case.py scenarios/refund/cases/case_real_01.json --reject    # 主管驳回
```

## 脱敏规范

> **给拿到真案例的人**：下面这张表是「照这个规则替换即可」的形式。真数据到手之后按它逐字段打码，
> **打完码再进仓库**——原始件一律不进 git，也不进 `evidence/`。
> `case_real_01.json` 里的客户身份四项已经按这张表打过码，可以直接照着形状抄。

| 字段 | 在哪 | 打码规则 | 打完的样子 |
| :-- | :-- | :-- | :-- |
| 客户姓名 | `order_snapshot[].payload_json.customer_name` | **保留姓，名全部替成 `*`**，一个字一个星 | `王小明` → `王**` |
| 手机号 | `order_snapshot[].payload_json.phone` | 保留**前 3 后 4**，中间 4 位替成 `****` | `13812346021` → `138****6021` |
| 收货地址 | `order_snapshot[].payload_json.address` | 保留到**区/县**一级，其后整段替成 `****`（不保留门牌、楼栋、路名） | `浙江省杭州市余杭区文一西路 969 号` → `浙江省杭州市余杭区****` |
| 订单号 | `order_snapshot[].order_id`、`case.order_id` | **整体替成编造号**，保留前缀与位数；两处必须换成同一个值 | `2026082012345678` → `ORD-2026-AG-0413` |
| 单据号 / 快递单号 | `order_snapshot[].payload_json.receipt_no` 等 | 保留**前缀与末 4 位**，中段替成 `****` | `DL-2026-88231-0413` → `DL-2026-****-0413` |
| 金额 | `amount_paid` / `amount_claimed` | **数量级保留、末两位归零或换成邻近值**；同一案子里所有金额按同一个系数改，否则核算对不上 | `1287.35` → `1280.00` |
| 证据 URI | `customer_evidence[].uri` | 换成 `oss://after-sales/<脱敏 case_id>/<描述性文件名>`，**不要留真实 bucket 与签名参数** | `https://real-bucket…?sig=…` → `oss://after-sales/RC-2026-0904-001/unboxing.mp4` |
| 证据摘要 | `customer_evidence[].digest` | 换成 `sha256:demo-<描述>-<序号>`，**不要留真实文件的哈希**（真哈希可反查原件） | `sha256:9f2a…` → `sha256:demo-unboxing-01` |
| 案件号 | `case.case_id` 及所有块里的 `case_id` | 换成 `RC-<日期>-<序号>`；**全文件统一替换**，漏一处就把两个案子串成一个 | `AS20260904000137` → `RC-2026-0904-001` |
| 审批人 | `approval_record[].approver` | 同客户姓名规则，另在括号里保留**角色**（角色不是隐私，且政策判定要用） | `沈思锴（region_manager）` |

三条一起守，缺一条这份规范就漏：

1. **同一个值全文件统一替换。** `case_id` / `order_id` 在十一个块里都出现，替漏一处的症状是「引用指不到对象」——`case_pack.ref_coverage()` 会数出悬空引用，但那已经是跑完之后了。
2. **不要动列名，也不要动 `policy_rule` 那几行。** 政策规则的唯一事实源是 `policy/policy_rules.json`（自校验第 3 条守着两处不分叉），真案例只换**数据**不换**结构**。
3. **金额与日期改了，结论就会跟着变。** 窗口判定按 `paid_at` 与 `requested_at` 现算天数，核算按 `amount_paid` 封顶 —— 这两组改完之后要重跑一次 `run_case.py` 看裁定是不是还是预期的那个，别默认它不变。

五份 case 的形状照 `maos/tests/fixtures/refund/case_r1.json`：顶层键即表名，`_` 前缀键是元数据，`case` 键是 `guard.create_case` 的入参。`maos/tests/test_refund_domain.py::_seed()` 那段盲插循环可以逐行不改地读它们。

## 字段对齐

- **政策规则**：列名逐字对齐 `maos/domain/refund/schema.sql` 的 `policy_rule` 表（`tenant_id / rule_no / version / title / body / effective_from / effective_to / channel_scope / sku_scope`）。
- **历史案例**：列清单对齐 `docs/EXECUTION.md:528` 的 `kb_doc`。**该表 P5 才建（W-3 轨在做）**，本目录不含 DDL，也没碰 `schema.sql`。
- **`embedding` 恒为 `null`**：向量由入库时按当时选定的嵌入实现现算。在语料里预置一串数，等于替 W-3 把「用哪个嵌入模型」这个决定提前做掉了。

### `body` 为什么是 JSON 字符串而不是条款正文

`policy.match::rule_params()` 用 `json.loads` 读 `body`；读不动就返回空 dict，`finance.settle` 随即落到它的缺省口径（`refund_ratio=1`、`deduct_fee=0`，即全额退不扣费）。也就是说 **`body` 写成自然语言条款时金额不会报错，只会静默地按全额算**。所以本语料的 `body` 一律是紧凑 JSON。

`finance.settle` 目前只消费其中两个键：`refund_ratio` 与 `deduct_fee`。其余键（`no_reason_days` / `warranty_basis` / `min_evidence_count` / `extra_tasks` / `approver_role` …）会原样进入 `policy.match` 出参的 `matched_rules[].params`，供下游取用。

> **这段是实测口径，截至 `b35c618`**：`min_evidence_count` / `requires_evidence_kinds` / `evidence_source`
> 三个键在 `maos/**/*.py` 里**没有任何消费方**（`b35c618` 上 grep 零命中；今天唯一的命中是
> `maos/tests/test_refund_corpus_rule_no.py` 那条守卫，它断言这几个键**不该**出现在发错货规则里，
> 不是在读它们）—— 语料定义了举证要求，但**没有任何代码去看它**，
> 于是同一个案子交不交图，裁定结论逐字节相同且不报错。这是一条**静默失效**，不是「功能没做完」。
> 已记进 `docs/BACKLOG.md` 的 `## task-T78`；**政策判定器落地后本段需复核**（届时消费的键不止两个）。

### `effective_to` 为什么全是 `null`

`objects.policy_rules_at_order` 的过滤条件是 `effective_to IS NULL OR effective_to > paid_at`。若把 v1 的 `effective_to` 设成 v2 的生效时刻，那么**锁定 v1 的老订单会一条规则都命中不上** —— 版本对照当场退化成「无规则可用」，证明不了任何东西。版本之间的分界靠的是 `version <= pinned`，不靠时间区间。

## 规则号的作用域：`AS-00x` 只在租户内有意义

> 本文件的 `AS-00x` 编号只在租户 `tnt-mfg-a` / `tnt-mfg-b` 各自范围内有意义。
> 同一个编号在别的租户语料里指的是另一条规则 —— **跨语料引用规则号前先看租户**。

`rule_no` 在 `policy_rule` 表里**不是全局主键**：主键是 `(tenant_id, rule_no, version)`。
所以「AS-003」这四个字单独拿出来讲是没有所指的，必须连着租户一起说。
本仓库现有语料里，同一个 `AS-003` 就指着两件毫不相干的事：

| 语料文件 | 租户 | AS-003 是什么 | `rule_kind` |
| :-- | :-- | :-- | :-- |
| `scenarios/custom/ledger.json` | `tnt-demo` | 发错货全额退并免手续费（`reason_code=wrong_item`） | `wrong_item` |
| `scenarios/refund/policy/policy_rules.json`、`cases/case_r4a.json`、`cases/case_r4b.json` | `tnt-mfg-a` | 人为损坏免责，需图片举证（`reason_code=artificial_damage`）；v2 收紧为「图片或视频 ≥2」 | `artificial_damage_exclusion` |
| `scenarios/refund/policy/policy_rules.json` | `tnt-mfg-b` | 人为损坏免责，需图片举证；v2 另加 `third_party_report_required` | `artificial_damage_exclusion` |

**这为什么是个真会咬人的坑**：拿规则号当口径讲的地方（答辩问答、PPT、`docs/EXECUTION.md` 附 B 的差异表）
会对错人 —— 实跑自定义入口命中的 `AS-003@v1` 是 `tnt-demo` 那条**与证据无关**的发错货规则，
而听众按 `tnt-mfg-a` 的语料理解，会以为「举证判据生效了」。两件事，一个编号。

守着这条口径的测试是 `maos/tests/test_refund_corpus_rule_no.py`：它钉住
「每份含规则号的语料都声明了自己的租户作用域」，也钉住
「两个租户的 `AS-003` 的 `rule_kind` **确实不同**」—— 将来谁把两边改成一样，那条测试会红，
逼改的人先来看这一节，而不是默默假设它们一致。

**编号本身不改**：`evidence/scenario-R5/` 等证据束里冻着 `AS-003` 这个字面量，
而证据必须是真实命令输出、一个字节都不许手改（铁律 3）。改号会让证据束与语料当场对不上。
所以这里买的是「口径说清楚 + 机器守着」，不是重新编号。

## 三组对照

三组的设计原则是**每组只放一个变量**，否则结论说不清是哪个因素造成的。

| 组 | 唯一变量 | 数据安排 | 结论 |
| :-- | :-- | :-- | :-- |
| R3 | `tenant_id` | 同商品、同渠道、同诉求、同第 20 天申请；下单于 2026-05-10（**早于** v2 生效），把版本这个变量摁住 | A 命中 `AS-001@v1` 窗口 30 天 → approve；B 同一条规则窗口 7 天 → reject |
| R4 | `channel_id` | 同租户、同商品、同诉求、同版本 | 自营命中 3 条；经销多命中 `AS-004@v1`，带「渠道商核销」任务、审批人 `region_manager`。**金额两侧相同**（AS-004 的 `refund_ratio=1`、`deduct_fee=0`），差异被隔离在渠道上 |
| R6 | 按哪一版政策判 | 下单于 2026-06-20（**晚于** v2 生效 2026-06-01），`policy_version_at_order=1`，第 20 天申请 | 按快照 `AS-001@v1` 窗口 30 天 → approve；按最新 `AS-001@v2` 窗口 7 天 → reject。**结论相反**，差额 1280.00 元 |

R6 是「错误套用政策」这条评委诉求的唯一证据，所以有一处必须说清：**v2 的 `effective_from` 必须早于订单的 `paid_at`**。否则 `effective_from <= paid_at` 会先把 v2 滤掉，「用最新版判」这条错误路径根本走不出来 —— 陷阱得真踩得进去才叫证据。

R4 与 R6 的差异在**现有匹配器上直接成立**，不需要任何新代码（R4 靠 `channel_scope`，R6 靠 `version <= pinned`）。R3 的「通过 vs 驳回」需要一个评估 `no_reason_days` 窗口的判定器，当前 `policy.match` 还没有 —— 它的 approve/reject 只判「有没有命中 AS- 规则」。这一条已记进 `docs/BACKLOG.md` 的 `## task-W1`，数据侧已按窗口参数造好，`_expected` 里写明了预期结论。

**T7 实跑结果（`python3 run.py --contrast`，exit=0）**：三组六个 case 全部与各自的 `_expected` 一致。

| 组 | 实测 | 差异从哪来 |
| :-- | :-- | :-- |
| R3 | A（`tnt-mfg-a`）命中 `AS-001@v1` 窗口 30 天 → **approve**，DAG 4 个任务；B（`tnt-mfg-b`）同一条规则窗口 7 天 → **reject**，DAG 3 个任务（不排核算） | 两个租户各自 `AS-001` 的 `no_reason_days` 参数。除 `tenant_id` 外两侧输入逐字段相同，有测试守着 |
| R4 | 自营 4 个任务、审批人 `supervisor`；经销 5 个任务、多出 `dealer_writeoff`（role `refund_channel`、标题「渠道商核销」）、审批人 `region_manager`。**核准金额两侧都是 6800.00** | `AS-004` 的 `channel_scope='ch-dealer'` 让自营侧压根取不到这条规则，于是它的 `extra_tasks` / `approver_role` 无从展开 |
| R6 | 按快照锁定的 v1 → **approve**，核准 **1280.00**；按库里最新的 v2 → **reject**，核准 **0.00** | `order_snapshot.policy_version_at_order = 1`，`version <= pinned` 把 v2 挡在外面。错误路径由 `contrast.policy_view_latest` 真跑一遍，不是推断 |

**窗口判定补在流程层**（`contrast.evaluate_eligibility`），不在 `policy.match` 里 —— `maos/skills/**` 不是 T7 的面。补的是**判定**不是**数据**：窗口天数逐字取自 `policy.match` 出参的 `matched_rules[].params`。两个裁定都如实报出（`contrast.json` 的 `policy_baseline` 是 skill 的原话，`decision` 是补上窗口之后的结论），搬回 skill 的改法记在 `docs/BACKLOG.md` 的 `## task-T7`。

### 这三组比「换域」更强

`docs/domain-portability.md` 的论证是「换域只换 Skill / ToolPort / 业务对象，`contracts/` 与 `runtime/` 零改动」。这三组换的是**同一个域内的三个维度**，于是那句话可以说得更硬：

> 换租户、换渠道、换政策版本，**连 Skill 都不用换**。
> `policy.match` / `finance.settle` / `refund.intake` 一个字节没动，
> `maos/contracts/**` 与 `maos/core/**` 零改动，Control Plane / 状态机 / Gate / Worker
> 从头到尾不认识「租户」「渠道」「版本」这三个词。

差异全部落在**数据**上，代码里没有一处 `if tenant == …` / `if channel == …` / `if version == …`。这一条不是自述，有两条负例测试守着（`maos/tests/test_contrast_cases.py`）：

- 把 `AS-004` 从政策表里删掉 → 核销任务当场消失、审批人退回缺省；
- 把 `AS-004` 的 `channel_scope` 改成 `ch-online`、订单也改挂自营 → **核销任务原封不动地跑到自营那一侧**。

第二条是更硬的那条：它证明代码里没有任何地方认得「经销」这个词。改数据就换结论，一行代码都不用动。

## 失败案例与错误码

24 条里 8 条 `outcome='failed'`，覆盖派单点名的五类：

| 类别 | 码 | 官方 `retriable` | 官方 `outcome` | 案例 |
| :-- | :-- | :-- | :-- | :-- |
| 渠道繁忙 | `20000` | True | **unknown** | kb-rc-0017 |
| 渠道繁忙 | `40005` | True | failed | kb-rc-0018 |
| 余额不足 | `ACQ.SELLER_BALANCE_NOT_ENOUGH` | False | failed | kb-rc-0019 |
| 交易不存在 | `ACQ.TRADE_NOT_EXIST` | False | failed | kb-rc-0020 |
| 重复请求不一致 | `ACQ.DISCORDANT_REPEAT_REQUEST` | False | **unknown** | kb-rc-0021 |
| 系统错误 | `ACQ.SYSTEM_ERROR` | True | **unknown** | kb-rc-0022 |
| 客户拒收退款 | *（无码）* | — | — | kb-rc-0023 / 0024 |

两件事请连着读：

1. **`outcome` 与 `retriable` 正交**。`40005` 是 `retriable=True` + `failed`（入口即拒，可以直接重发）；`20000` 是 `retriable=True` + `unknown`（可能已经进了业务系统，**必须先 query 再决定**）。只看 `retriable` 就会在 `20000` 上重发出第二笔退款。这两个字段是原样抄的，不是按语感推断的。
2. **官方 `outcome=unknown` 的三条码，案例里另写了 `unknown_resolved_by`**，记明是怎么问清的。案件最终记 `failed` 靠的是事后 `query` 问出来的结果，不是把 `unknown` 直接当成失败 —— 后者正是铁律 8 说的那种 bug。

**客户拒收退款不产生任何网关错误码**，所以那两条的 `gateway_code` 是 `null`。`ALL_CODES` 里没有这一类，硬安一个码就是编造；退款接口的业务错误码表里也确实没有「客户拒收」这种东西 —— 它是业务侧的事，不是支付侧的。代价写在这里：这两条在 W-3 的错误码通道上召不回，只能靠全文与规则编号通道。

超出 `ALL_CODES` 这 11 条的场景一条也没造 —— `lookup()` 对未收录的码会抛 `KeyError`，那是有意设计（未知码不许兜底成「默认可重试」）。

## 自校验

```bash
# 1. 全部 JSON 可解析
python3 -c "import json,glob;[json.load(open(f)) for f in glob.glob('scenarios/refund/**/*.json',recursive=True)];print('json ok')"

# 2. 错误码 ⊆ ALL_CODES（见上文「数据来源与口径」那条命令）

# 3. case 文件里的 policy_rule 与 policy_rules.json 逐字段同一份
python3 -c "import json,glob; c=json.load(open('scenarios/refund/policy/policy_rules.json'))['policy_rule']; k=lambda r:(r['tenant_id'],r['rule_no'],r['version']); idx={k(r):r for r in c}; bad=[(f,k(r)) for f in glob.glob('scenarios/refund/cases/*.json') for r in json.load(open(f))['policy_rule'] if idx.get(k(r))!=r]; print('drift:',bad)"
```

第 3 条是防漂移的：五份 case 各自内联了自己用到的 `policy_rule` 行（`case_r1.json` 的形状要求自包含），而唯一的事实源是 `policy/policy_rules.json`。两处一旦分叉，对照实验的前提就悄悄变了，且不会有任何报错。

## 谁在消费它

W-1 造完这批数据时**零消费方**（「字段分叉不会有任何报错」那条账记在 `docs/BACKLOG.md` 的 `## task-W1`）。现在每一份都有名有姓的消费方，改一个字段就有东西会红：

| 文件 | 谁在读 | 读来干什么 | 分叉了会怎样 |
| :-- | :-- | :-- | :-- |
| `policy/policy_rules.json` | `maos/kb/experiment.py::_seed_domain_from_corpus` / `seed_kb_corpus` | R5 的靶场四张表 + 16 条政策投影进 `kb_doc` | `_checked_rows` 逐行校验列清单，多一列少一列当场抛 |
| 同上 | `maos/domain/refund/fixtures.py::seed_policy_corpus` / `seed_policy_kb` | 三组对照的知识库底料（**跨租户召不回**那条约束要靠它，库里必须躺着两个租户的知识） | 同上，共用同一份列清单 |
| `history/history_cases.json` | `maos/domain/refund/fixtures.py::seed_history_kb` | 24 条按**晋升规则分流**落进 `kb_doc`：`outcome='success'` → `history_case`（规划正例），8 条 `outcome='failed'` → `failure_hint`（**不作为规划正例**） | 列清单对齐 `kb.DOC_COLUMNS`，分叉即抛；条数与分流比例由 `test_contrast_cases.py` 守着 |
| 同上 | `maos/tests/test_kb_corpus.py` | 全量装载 + 取值域 / 错误码守卫 | 同上 |
| `kb/*.json` | `maos/kb/experiment.py::seed_process_kb` | R5 的库装上九类流程知识（**不含 `history_case`** —— 核验器第 7 项要它回查得到本库 case） | 列清单对齐 `kb.DOC_COLUMNS`，分叉即抛；条数与 kind 分布由 `test_kb_process_corpus.py` 守着 |
| 同上 | `scripts/gen_refund_kb.py --check` | 校验磁盘上那份就是当前生成器的产物 | 手改语料、或错误码表 / 真实 DAG 改了形状而语料没跟上，`--check` 当场非零退出 |
| `cases/*.json` | `maos/flows/contrast.py`（经 `fixtures.load_case`） | 三组对照的靶场与**判据**：`case` 块当 `case_seed`、五张外部快照表灌库、`_expected` 块当唯一判据 | 判据不符时 `run.py --contrast` 直接抛、`make_evidence.py --contrast` 非零退出不留产物 |

入口：

```bash
python3 run.py --contrast                      # 三组六个 case 跑一遍，屏幕上打对照结论
python3 scripts/make_evidence.py --contrast    # 另产 evidence/contrast-R3/R4/R6
python3 -m pytest maos/tests/test_contrast_cases.py -q
```

`kind` 的分流发生在**装载侧**不是检索侧：语料里 24 条的 `kind` 全是 `history_case`（数据侧只记「这是一条历史案例」），落库时按「外部结果明不明确」分流，口径与 `maos/kb/guardrails.py::classify_case` 同一份。写错 `kind` 的条目查得出来但归不了类，而错误发生在写入侧、暴露在几周后的检索侧，是最难回溯的一类脏数据。

**本目录仍然只出数据**：接线全部在 `maos/` 与 `scripts/` 下，这里一个 `.py`、一行 DDL 都没有加。


## 九类流程知识（T118）

评委第二条建议点名了九类面向 workflow 规划的企业流程知识。它们落成 `kb_doc.kind` 的**八个**取值 ——
「产品与渠道差异」走 `policy` 的渠道 / 品类变体，不另立一类（它讲的是同一条规则在不同渠道上落地的差异）：

| 评委点名的类别 | `kind` | 条数 | 出处 |
| :-- | :-- | --: | :-- |
| 售后政策及生效范围 | `policy` | 16 | `policy/policy_rules.json` 投影 |
| 产品与渠道差异 | `policy`（`kb-pv-*`） | 8 | 构造，两个 mfg 租户各 4 条 |
| 历史退款原因 | `history_case` / `failure_hint` | 16 / 8 | `history/history_cases.json` |
| 任务拆分 | `task_pattern` | 12 | 4 种真实 DAG 形状 x 3 租户 |
| 人工驳回 | `rejection` | 13 | 底账 7 条 + 构造 6 条 |
| 支付错误码 · 超时与补偿路径 | `error_code_playbook` | 33 | 11 条官方码 x 3 租户 |
| 客户沟通结果 | `comms_result` | 12 | 底账 6 条 + 构造 6 条 |
| 真实到账结果 | `arrival_result` | 15 | 底账 9 条 + 构造 6 条 |

**只有 `task_pattern` 的 body 用 `steps` 这个键。** 这不是文风：`guardrails.apply_suggestions`
会把命中文档 body 里的 `steps` 并进当前 DAG，而它**只按 kind 过滤**（`kb.POSITIVE_KINDS`）。
`policy` 与 `error_code_playbook` 都在正例里，它们的 body 一旦多出这个键，R5 的计划就凭空多出几步 ——
而两版 DAG 的 diff 照样是绿的。所以处置步骤一律写成 `remedy_steps`、差异写成 `differences`，
由 `test_kb_process_corpus.py::test_positive_kinds_carry_no_planning_steps` 钉住。

`task_pattern` 本身**今天不在 `POSITIVE_KINDS` 里** —— 它够格（body 就是步骤清单，键名与
`guardrails._steps_of` 对齐），但「让一类知识自动改写 DAG 的形状」是规划面的判断，
不是语料面能替它定的。要启用只需改 `kb.POSITIVE_KINDS` 一处。

### 错误码手册的 `outcome` 为什么一律是 NULL

官方码表里有 `outcome=unknown` 这一档（`20000`、`ACQ.SYSTEM_ERROR`、`ACQ.DISCORDANT_REPEAT_REQUEST`、
`ACQ.REFUND_CHARGE_ERROR`），而 `kb_doc.outcome` 的取值域只有 `success` / `failed`。
把 `unknown` 压成 `failed` 就是「把查不到当成没发生」—— 铁律 8 说的正是这种 bug。
官方那三个字段原样躺在 `body.gateway` 里，谁要用都取得到。

处置口径按 `(retriable, outcome)` **两个字段的组合**判，不是只看 `retriable`：
`40005` 是 `retriable=True + failed`（入口即拒，可以直接重发），`20000` 是 `retriable=True + unknown`
（可能已经进了业务系统，必须先 query）。只看 `retriable` 就会在 `20000` 上重发出第二笔退款。

### 重新生成

```bash
python3 scripts/gen_refund_kb.py            # 覆盖写，末尾打印各 kind 条数
python3 scripts/gen_refund_kb.py --check    # 只校验，不写盘（有测试跑它）
```

产物是**确定性**的：同样输入两次生成逐字节相同（没有 `random`、没有 `datetime.now()`、
没有集合迭代序）。少了这条，语料每跑一次就产生一份无意义的 diff，`git diff` 从此不能当判据用。
