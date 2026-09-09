# MAOS 五岗正确率测试方案

> 起草 2026-09-07。基线：`goai-restructure` @ `8a6c2f9`，`python3 -m pytest maos/tests/ -q`
> 实测 **2259 passed, 41 skipped, 66.24s**。文中所有分支、阈值、常量名均来自当日代码，
> 改代码请连同本文一起改 —— 一份对不上代码的测试方案会把「方案错了」报成「系统退化了」。

---

## 0 一句话结论

**「五岗正确率」不是一个数，测出一个数就是测错了。**

五岗的业务判定 90% 是**零模型的规则代码**（`policy.match` 注释原话：「裁定零模型：同一个案子在
任何机器任何时刻必须给同一个结论」）。规则代码没有「正确率」，只有「对不对」—— 合格线是
100%，任何一条不过都是 bug，报「95% 正确率」等于说「有 5% 的 bug 我打算留着」。

真正有百分比的只有一处：**圆桌发声层的真模型**（`maos/roundtable/speaker.py`）。

而实测下来，**当前 2259 条测试里走真模型的是 0 条** —— 41 个 skipped 全是
Nacos / RocketMQ / PostgreSQL / cryptography 的外部依赖，没有一条是模型。
判据工具 `stages.numbers_in()` 已经实现，但 `grep -rn "numbers_in"` 全仓只有定义那一行、
**零调用方**。也就是说 R1（模型不许编数字）这条铁律，在真模型路径上目前没有任何东西在查。

所以这份方案分四层，四层的指标名、判据、合格线、跑法、跑的频率全都不同。

---

## 1 口径：为什么必须分层

### 1.1 五岗的判定分别在哪

| 岗位 | agent_id | 判定所在 | 有模型吗 |
|---|---|---|---|
| 申请受理岗 | `refund-intake` | `skills/builtin/refund/intake.py` | 否，复用 `issue.aggregate` 去重 |
| 规则审核岗 | `refund-policy` | `skills/builtin/refund/policy.py` | 否，规则匹配 |
| 证据核验岗 | `refund-evidence` | `skills/builtin/refund/evidence_check.py` | 否，零 IO 确定性 |
| 风险反欺诈岗 | `refund-risk` | `skills/builtin/refund/risk_screen.py` | 否，纯函数 |
| 财务执行岗 | `refund-finance` | `skills/builtin/refund/finance.py` | 否，Decimal 核算 |

五个 Agent（`maos/agents/refund/*_agent.py`）是**薄壳**。`risk_agent.py` 的 docstring 写得最直白：
「业务判定一行都不在这里……判定一旦漏进 Agent，『换域只换 Skill』当场不成立」。

模型只在两处露面：

1. `roundtable/speaker.py` —— 把规则代码算出的事实卡说成人话，**只许复述**；
2. `BaseAgent.ask` —— 五岗当前路径没走它（圆桌发言直接 `model.complete`，绕过 `ask`）。

### 1.2 现状盘点（全部实测，非自述）

| 项 | 实测值 | 命令 |
|---|---|---|
| 全量测试 | 2259 passed / 41 skipped / 66.24s | `python3 -m pytest maos/tests/ -q` |
| 走真模型的测试 | **0 条** | `pytest -rs` 后看 skip 理由，无一条与模型有关 |
| `numbers_in()` 调用方 | **0 个**（只有定义） | `grep -rn "numbers_in" --include=*.py .` |
| 已有 `_expected` 的用例 | 5 个（R3A/R3B/R4A/R4B/R6） | `grep -l _expected scenarios/refund/cases/*.json` |
| 分岗现有测试 | intake 8 / evidence 22 / risk 16 / flow 21 / stages 14 / team 27 / verdict 34 | `grep -c "^def test"` |

### 1.3 四层指标表 —— 这张表是本方案的骨架

| 层 | 被测对象 | 模型 | 指标名 | 合格线 | 跑法 | 频率 |
|---|---|---|---|---|---|---|
| **L1** | 五个 skill 的判定 | 无 | golden set 通过率<br>判定分支覆盖率 | **100%**<br>≥ 95% | `pytest` 离线 | 每次改动 |
| **L2** | 五个 Agent 的搬运/包装 | 无 | 契约一致率 | **100%** | `pytest` 离线 | 每次改动 |
| **L3** | 圆桌 `Speaker` | **有** | 数字忠实度<br>措辞守恒率<br>越界率 | ≥ 99%（pass^5）<br>**100%**<br>**0** | 配 `MAOS_LLM_*` 才跑 | 每周 / 发版前 |
| **L4** | 五岗端到端联判 | 混合 | 联判一致率<br>重跑稳定率 | **100%**<br>**100%** | `pytest` + 真房间 | 发版前 |

**读法**：L1/L2/L4 的合格线是 100%，它们回答的是「对不对」；只有 L3 报百分比，
它回答的是「模型这次老实吗」。把 L1 的通过率和 L3 的忠实度平均成一个「五岗正确率」，
得到的数既解释不了故障也指导不了修复。

---

## 2 L1 —— 五岗 golden set（确定性层）

### 2.0 用例格式：沿用 `_expected`，不另发明一套

`scenarios/refund/cases/case_r3a.json` 已经确立了格式，新用例照抄这个形状：

```json
{
  "_note": "这条用例要证明什么，以及被摁住的变量是哪些",
  "_provenance": "合成数据出处声明（现有用例都有，别丢）",
  "_expected": {
    "case_id": "RC-XXX",
    "skill": "refund.risk_screen",
    "expect": { "level": "medium", "score": 30 },
    "why": "freq=2 记 20 + multi_order=3 记 10 = 30，正好压在 LEVEL_MEDIUM 上",
    "boundary": true
  },
  "...": "底账各表"
}
```

三条硬要求：

1. **`why` 必填，且必须是算式不是结论。**「应该是中风险」没有信息量；
   「20 + 10 = 30 = LEVEL_MEDIUM」在阈值被改动时会立刻自证过期。
2. **`boundary: true` 的用例单独成组**，L1 允许非边界用例批量生成，边界用例必须手写。
3. **`_expected` 里只写这一岗的出参**，不写下游。跨岗断言留给 L4，混在 L1 里会让
   一个 finance 的 bug 报成 policy 岗不合格。

### 2.1 岗一 · 申请受理岗（`refund.intake`）

**判定面**：多源聚合去重 → 建案 → 挂证据。零模型。

| # | 分支 | 用例要点 | 现有覆盖 |
|---|---|---|---|
| I-1 | 三源同一诉求 | 工单 + 客服记录 + 图片上传，归一化标题相同 → 合并为 1 案 | 部分（`test_refund_intake_idempotent` 8 条） |
| I-2 | 三源不同诉求 | 标题不同 → 不合并，建 3 案 | 待补 |
| I-3 | 幂等 | 同一 payload 连投两次 → 同一 `case_id`，不产生第二行 | ✅ 已有 |
| I-4 | 审计留痕 | 合并走 `SkillInvoker` → `event_log` 里有一条 `SkillInvoked(issue.aggregate)` | 待补（**这是「复用」不是自述的唯一证据**） |
| I-5 | 越过 guard 写表 | 直接 `objects.execute()` 写 `refund_case` → 抛 `BypassedGuardError` | 待补 |
| I-6 | 必填缺失 | `sku` / `reason_code` / `amount_claimed` 各缺一次 → 抛，不建半张案 | 待补 |
| I-7 | 授权不足 | identity 未授 `issue.aggregate` → 被 invoker 拒，不静默降级成「不去重」 | 待补 |

**建议规模**：7 分支 × 平均 2 条 = **14 条**。

> **I-4 与 I-7 是这一岗最值钱的两条。** 去重是「复用已在库的 skill」这个卖点的落点，
> 而绕过 invoker 直接 `.run()` 在功能上完全一样、在审计上一无所有 —— 只有查
> `event_log` 才分得出这两者。这条不测，卖点就退化成注释里的一句声称。

### 2.2 岗二 · 规则审核岗（`policy.match`）

#### ⚠️ 先说这一岗最大的坑 —— 不说清会得到一个系统性错误的正确率

`policy.match` 的裁定逻辑是**前缀命中即 approve**：

```python
matched = [r for r in applicable if str(r["rule_no"]).startswith(prefix)]   # prefix 默认 "AS-"
if matched:  decision = "approve"
else:        decision = "reject"
```

**窗口判定（下单第几天、还在不在无理由期内）根本不在这个 skill 里**，
它在 `contrast.evaluate_eligibility`。`roundtable/stages.py` 的 docstring 已经把这个坑
实测记下来了：

> 同一张单子走 `refund.intake` → `policy.match` → `finance.settle` 三步，即便裁定是驳回
> 也照样算出 6800.00 —— 因为 `policy.match` 是「前缀命中即 approve」……真跑 `run_payload` 退的是 0.00。

**后果**：如果按「这单最终该不该退」去给规则审核岗打分，凡是「规则存在但已过窗口」的用例
都会被判为岗位错误 —— 而这不是 bug，是分层设计。这类用例在真实语料里占比不低
（`case_r3b` 就是一条：AS-001@v1 窗口 7 天，第 20 天申请）。

**正确口径**：这一岗的判据是 **「有没有按下单当时锁定的版本，检出正确的规则集合」**，
不是「最终该不该退」。断言 `matched_rules` / `rule_refs` / `policy_version`，
`decision` 只作为「规则集合是否为空」的同义反复来断言。

#### 用例矩阵

| # | 分支 | 用例要点 | 现有覆盖 |
|---|---|---|---|
| P-1 | 版本锁定 | `policy_version_at_order=1`，v2 已发布 → 仍按 v1 判 | ✅ R3A/R3B |
| P-2 | 跨租户隔离 | 同 `rule_no` 不同 `tenant_id` 参数不同 → 结论相反 | ✅ R3A/R3B 对照组 |
| P-3 | 渠道范围 | `channel_scope=ch-dealer` → 自营渠道不命中 | ✅ R4A/R4B |
| P-4 | SKU 范围 | `sku_scope` 限定 → 不匹配的 SKU 不命中 | 待补 |
| P-5 | `effective_from` 过滤 | 生效时刻晚于下单时刻的版本不参与 | ✅ R3A 隐含，建议显式化 |
| P-6 | 「≤ 锁定版本取每条规则最大版本」 | 同一 `rule_no` 有 v1/v2/v3，锁定 v2 → 取 v2 不取 v1 也不取 v3 | 待补（**核心口径**） |
| P-7 | 前缀过滤 | 非 `AS-` 前缀的规则不进 `matched` | 待补 |
| P-8 | 空命中 | 无适用规则 → `reject` 且 `reason` 里报「当时可用规则 N 条」 | 待补 |
| P-9 | 业务引用 | `plan_id` + `task_id` 都非空才 `attach_business_ref` | 待补 |
| P-10 | 不自写 SQL | 版本检索一律走 `objects.policy_rules_at_order()` | 静态检查（见 §6 第 4 步） |

**建议规模**：10 分支 × 平均 2 条 = **20 条**，其中 P-6 至少 4 条（v1/v2/v3 × 锁定点）。

### 2.3 岗三 · 证据核验岗（`refund.evidence_check`）

**判定面**：`requirement_source` 三态 × `verdict` 四态，外加 `consistency` 三检查。

#### 2.3.1 主判定矩阵（有效组合 7 格，不是 12 格）

`source == none` 时 `verdict` 恒为 `not_required`，所以矩阵不是全组合：

| `requirement_source` | `complete` | `partial` | `missing` | `not_required` |
|---|---|---|---|---|
| `policy`（规则声明了） | ✅ 已有 | ✅ 已有 | 待补 | — 不可达 |
| `default`（退到缺省表） | ✅ 已有 | 待补 | 待补 | — 不可达 |
| `none`（两级都没要求） | — 不可达 | — 不可达 | — 不可达 | ✅ 已有 |

#### 2.3.2 `_verdict` 的判定顺序必须单独钉住

代码注释原话：「四态，**按此顺序判**（顺序本身是判据的一部分，换序会改语义）」。

```python
if source == SOURCE_NONE:                                     return NOT_REQUIRED   # ①
if set(required_kinds) <= set(have) and count >= min_count:    return COMPLETE       # ②
if count == 0 or (required_kinds and not set(have) & set(required_kinds)):
                                                              return MISSING        # ③
                                                              return PARTIAL        # ④
```

**必测的四条边界**（每条都恰好卡在某一步的出口上）：

| 用例 | required | have | count | min | 期望 | 卡在哪 |
|---|---|---|---|---|---|---|
| E-b1 | `[image]` | `[image]` | 1 | 1 | `complete` | ②，`count == min` 的等号 |
| E-b2 | `[image]` | `[image]` | 1 | **2** | `partial` | ② 因 count<min 落空 → ③ 交集非空 → ④ |
| E-b3 | `[image, document]` | `[image]` | 1 | 1 | `partial` | ③ 交集非空，**不是 missing** |
| E-b4 | `[image]` | `[document]` | 1 | 1 | `missing` | ③ 交集为空，虽然 count≠0 |

> E-b3 与 E-b4 的区别（「有一些但不齐」vs「一类都没有」）是这一岗最容易写反的地方，
> 且写反之后 `verdict` 仍是四态之一、不报错。

#### 2.3.3 两级取舍：默认表整张退场，**不取并集**

模块 docstring 明写：「规则声明优先；适用规则一条都没声明时，退到 `REASON_EVIDENCE_DEFAULTS`」，
且「一旦有规则声明了，默认表整张退场，不做两级取并集」。

| # | 用例 | 期望 |
|---|---|---|
| E-1 | 规则声明 `min_evidence_count: 0`，缺省表要求 1 | `source=policy`，min=0，**不被默认表拉回 1** |
| E-2 | 规则声明了但空列表 `[]` | 不回退默认表（✅ 已有 `test_empty_declared_list_...`） |
| E-3 | 规则对本 `reason_code` 不适用 | 该规则不算「声明过」，走默认表（✅ 已有） |
| E-4 | `no_reason_return` | 缺省表刻意无此项 → `not_required`（✅ 已有） |

#### 2.3.4 kind 归一化

`EVIDENCE_KINDS = (image, video, audio, document, attachment)`，归一化三级：

| # | 输入 | 期望 | 覆盖 |
|---|---|---|---|
| E-5 | `img` / `photo` / `jpg` / `录音` / `扫描件` | 字面同义词表命中 | ✅ 部分 |
| E-6 | `image/jpeg` / `application/pdf` | MIME 精确表命中 | ✅ 已有 |
| E-7 | `image/webp`（未列入精确表） | `KIND_MIME_PREFIXES` 前缀兜底 → `image` | 待补 |
| E-8 | `uri` 以 `.png` 结尾但 `kind` 写 `document` | **按声明判 document，不按后缀猜** | ✅ 已有 |
| E-9 | 完全不认识的 kind | 落 `attachment`，不抛 | ✅ 已有 |
| E-10 | `digest` 为空 | 不计入 `count`，且进 `gaps` 清单 | ✅ 已有 |

> **E-8 的方向别测反了。** 代码取的是「声明优先且可审计」，后缀是传输细节。
> 顺手「修」成按后缀猜会让一条已通过的测试变红，而那条测试是对的。

#### 2.3.5 `consistency` 三检查 —— **不改 `verdict`**

`CHECK_SIGNED_BEFORE_REQUEST` / `CHECK_QC_MATCHES_REASON` / `CHECK_DIGEST_NONEMPTY`。
代码注释：「交叉核对是给人看的线索，不是举证是否充分的判据」。

| # | 用例 | 期望 |
|---|---|---|
| E-11 | 签收时间晚于申请时间 | `consistency` 报不一致，**`verdict` 不变** |
| E-12 | 质检 `pass` 但诉求 `quality_defect` | 同上（✅ 已有） |
| E-13 | 无物流记录 | 跳过该检查，不报不一致（✅ 已有） |
| E-14 | 时间戳不可解析 | 报进 `consistency` 且不抛（✅ 已有） |
| E-15 | **回归守卫** | 任取一条不一致用例，断言 `verdict` 与去掉该不一致时**逐字相同**（待补） |

**建议规模**：主判定 7 格 + 边界 4 + 两级 4 + 归一化 6 + 一致性 5 = **26 条**（现有 22 条可复用大半）。

### 2.4 岗四 · 风险反欺诈岗（`refund.risk_screen`）

**判定面**：6 个信号 → 加权求和（封顶 100）→ 两阈值分档。全部权重与阈值是**类属性**。

#### 2.4.1 权重与阈值（写方案时的取值，改了要同步改本表）

| 常量 | 值 | 触发条件 |
|---|---|---|
| `W_DUPLICATE` | 40 | 同单历史里有 `pending` 或 `settled` |
| `W_ALREADY_REFUNDED` | 50 | 同单历史里有 `settled` |
| `W_FREQ_MEDIUM` / `W_FREQ_HIGH` | 20 / 35 | 同客户 30 天内 ≥2 笔 / ≥4 笔（**只取高档，不叠加**） |
| `W_OVER_PAID` | 15 | 申报 > 实付 |
| `W_MULTI_ORDER` | 10 | 同客户名下 ≥3 笔订单快照 |
| `LEVEL_MEDIUM` / `LEVEL_HIGH` | 30 / 60 | `score >=` 即进该档 |

#### 2.4.2 分档矩阵 —— 边界用例必须压在阈值上

| # | 信号组合 | 算式 | score | 期望 | 说明 |
|---|---|---|---|---|---|
| R-1 | 全干净 | 0 | 0 | `low` | ✅ 已有 |
| R-2 | 仅 freq=2 | 20 | 20 | **`low`** | ⚠️ 20 < 30，「频率偏高」单独不进 medium |
| R-3 | 仅 over_paid | 15 | 15 | `low` | ✅ 已有 |
| R-4 | over_paid + multi=3 | 15+10 | 25 | `low` | **差 5 分进档**，边界 |
| R-5 | **freq=2 + multi=3** | 20+10 | **30** | **`medium`** | **正好压在 `LEVEL_MEDIUM`**，边界 |
| R-6 | 仅 duplicate(pending) | 40 | 40 | `medium` | ✅ 已有 |
| R-7 | 仅 freq=4 | 35 | 35 | `medium` | 高档不叠低档 |
| R-8 | duplicate + over_paid | 40+15 | 55 | `medium` | 差 5 分进 high，边界 |
| R-9 | **duplicate + freq=2** | 40+20 | **60** | **`high`** | **正好压在 `LEVEL_HIGH`**，边界 |
| R-10 | settled 一条 | 40+50 | 90 | `high` | ⚠️ settled ∈ `ACTIVE_STATUSES`，**一条记录同时命中两个信号** |
| R-11 | 全信号拉满 | 40+50+35+15+10=150 | **100** | `high` | 封顶 `min(100, score)`，边界 |

> **R-10 值得单独盯。** `ACTIVE_STATUSES = {pending, settled}` 而 `STATUS_SETTLED = settled`，
> 所以一条 settled 记录会同时点亮 `duplicate_refund` 和 `already_refunded`，共 90 分。
> 这是注释里说的「一条就把分档顶到 high，不依赖任何别的信号凑数」，是设计不是重复计分 ——
> 但**测试必须把 90 这个数写死**，否则哪天有人「修」掉重复计分，分档从 high 掉到 medium 而没有任何测试变红。

#### 2.4.3 阈值可配性与降级

| # | 用例 | 期望 | 覆盖 |
|---|---|---|---|
| R-12 | `monkeypatch` 一个极端阈值 | 分档跟着变（证明读的是类属性不是 if 里的字面量） | ✅ 已有 |
| R-13 | 无 `customer_id` | `frequency_30d=0`、`multi=1`，且 `reasons` 含 `NOTE_NO_CUSTOMER` | ✅ 部分 |
| R-14 | `amount_paid <= 0` | 抛 `ValueError`，**不兜底成按 0 算比值** | ✅ 已有 |
| R-15 | `requested_at` 不可解析 | 抛 `ValueError` | ✅ 已有 |
| R-16 | 频率窗口边界 | `decided_at` 恰好落在第 30 天 / 第 31 天 | ✅ 部分，建议补等号侧 |
| R-17 | 确定性 | 同入参连跑两次，除 `invocation_id` 外逐字一致 | ✅ 已有 |
| R-18 | `reasons` 顺序固定 | 按 `_score` 的固定遍历顺序，不随 dict 序变 | 待补 |

**建议规模**：分档 11 + 可配/降级 7 = **18 条**（现有 16 条大部分可复用）。

### 2.5 岗五 · 财务执行岗（`finance.settle`）

**判定面**：封顶 → 比例 → 扣费 → 落库 + 出 artifact。全程 `Decimal`。

| # | 分支 | 用例要点 | 期望 |
|---|---|---|---|
| F-1 | `decision != approve` | 裁定驳回时调用 | **抛 `ValueError`**，不给 0 元 entry |
| F-2 | 正常核算 | claimed=6800, paid=8000, ratio=1, fee=0 | 6800.00 |
| F-3 | **封顶** | claimed=9999, paid=6800 | base=6800，`capped_by_paid=true` |
| F-4 | **浮点陷阱** | 6800 × 0.85 | **5780.00**，不是 5779.999999999999 |
| F-5 | 扣费到负 | gross=100, fee=200 | `max(…, 0)` → 0.00，不出负数 |
| F-6 | 半分位舍入 | 构造恰好落在半分上的数 | `ROUND_HALF_UP`，**不是**银行家舍入 |
| F-7 | 多规则命中 | 两条规则 ratio 0.8 / 1.0，fee 10 / 50 | ratio 取 **max=1.0**，fee 取 **max=50** |
| F-8 | 无订单快照 | 查不到 `order_snapshot` | 抛 `LookupError`，金额不猜 |
| F-9 | **F-1 冻结契约** | 落库那行与 artifact 的 `content.finance_entry` | **同一份 `entry` 对象**，逐字段相等 |
| F-10 | `breakdown` 保留算式 | `breakdown` 里是字符串原值 | 审计看到的是算式本身而非结果 |

> **F-7 有一处口径疑点，建议测的同时记进 `docs/open-questions.md`：**
> `_params_of` 注释说「比例取最不利于商家的一条（最大 ratio），扣费取最大」。
> `max(ratio)` 对客户有利、`max(fee)` 对客户不利，两个方向不一致。
> 若「政策对客户的承诺是并集」成立，扣费似应取 `min`。
> **这不是让你改代码** —— 先按现状写死测试（`max`/`max`），把疑点挂到 open-questions，
> 由你确认口径后再动。测试先钉住现状，改口径时才有东西变红。

> **F-9 是跨轨冻结契约，漏了会在合并后才发作。** 模块 docstring 原话：
> 「一边按 `business_ref` 查表判、一边按 artifact content 判，两轨各自都绿，
> 合到一起闸恒 blocker 或恒 pass，症状要跑到场景 6 才出现」。

**建议规模**：10 分支 × 平均 1.5 条 = **15 条**。

### 2.6 L1 汇总

| 岗位 | 建议用例数 | 现有可复用 | 净增 |
|---|---|---|---|
| 申请受理岗 | 14 | 8 | +6 |
| 规则审核岗 | 20 | ~10（含 5 个 `_expected` 用例） | +10 |
| 证据核验岗 | 26 | 22 | +4 |
| 风险反欺诈岗 | 18 | 16 | +2 |
| 财务执行岗 | 15 | ~8 | +7 |
| **合计** | **93** | **~64** | **+29** |

**L1 的两个指标**：

- **golden set 通过率 = 100%**（不是 95%，不是 99%）；
- **判定分支覆盖率 ≥ 95%**，用 `pytest --cov=maos/skills/builtin/refund --cov-branch` 量，
  未覆盖的分支要在 `docs/open-questions.md` 里逐条说明为什么不测。

---

## 3 L2 —— Agent 薄壳层

五个 Agent 不做业务判定，所以这一层测的**不是准确率，是「壳有没有漏」**。
一旦某个 Agent 里冒出一行 `if score > 60`，「换域只换 Skill」当场不成立 —— 而这种漏
不会让任何现有测试变红。

| # | 判据 | 怎么测 | 合格线 |
|---|---|---|---|
| A-1 | 出参键集 == `SkillContract` 声明 | 逐岗断言 `set(output) == set(contract.outputs)` | 5/5 |
| A-2 | 入参按名搬运 | 断言 Agent 只读 `RISK_INPUT_KEYS` 这类常量，不写字面量 | 5/5 |
| A-3 | **判定不下沉** | 静态检查：`agents/refund/*.py` 里不出现数值比较与阈值字面量 | 0 处 |
| A-4 | 授权最小化 | identity 未授对应 skill → 被 invoker 拒（✅ risk/evidence 已有） | 5/5 |
| A-5 | artifact 形状 | `kind` / `summary` / `self_check` 齐备 | 5/5 |
| A-6 | 失败姿态 | 入参缺失 → `failed(...)` 带理由，**不抛穿** | 5/5 |
| A-7 | 不越权声称 | 证据不足时 Agent 不说「已拒赔」（✅ 已有 `test_agent_does_not_claim_rejection_...`） | 5/5 |

**A-3 的写法**（这条是本层的核心，其余都是常规断言）：

```python
FORBIDDEN = re.compile(r"[<>]=?\s*\d|\d\s*[<>]=?")   # 阈值比较的字面量形态
def test_no_business_thresholds_leaked_into_agents():
    for p in Path("maos/agents/refund").glob("*_agent.py"):
        src = strip_comments_and_docstrings(p.read_text("utf-8"))
        assert not FORBIDDEN.search(src), f"{p.name} 里出现了阈值比较，判定正在下沉到 Agent"
```

> 注释与 docstring 必须先剥掉 —— `risk_agent.py` 的 docstring 里就写着「多少分算 high」，
> 不剥会拿到一条永远红的测试，然后被加白名单绕过，守卫随即失效。

**建议规模**：7 判据 × 5 岗 ≈ **20 条**（可参数化压缩），净增约 **12 条**。

---

## 4 L3 —— 真模型发声层（**唯一真有百分比的一层**）

### 4.0 现状：这一层目前是零

- 走真模型的测试：**0 条**；
- `numbers_in()`：实现了，**零调用方**；
- `test_roundtable_team.py` 里的 R1 测试用的是 `_EchoModel`（原样回显事实卡）——
  它证明的是「管道没把数字弄丢」，**不是**「模型没编数字」。回显模型永远不会编。

所以这一层是从 0 开始建的，也是这份方案里**唯一能产出一个百分比**的地方。

### 4.1 三条硬判据 + 一条软判据

#### 判据 1 · 数字忠实度（R1）—— 纯代码，零成本

```python
assert stages.numbers_in(speech) <= stages.numbers_in(facts)
```

`numbers_in()` 已就位，接上即可。**两个已知盲区，必须在报告里声明**：

1. **只抓阿拉伯数字**（正则 `\d+(?:\.\d+)?`）。模型把「20 天」说成「二十天」，
   判据看不见 —— 既不误报也不检出，是**漏检**不是误判；
2. **子集判据对「漏说」免疫**。模型只念一半数字照样通过。
   漏说不是幻觉，不该红 —— 但要在报告里单列一个「数字覆盖率」作参考值，不设合格线。

#### 判据 2 · 措辞守恒（R8）—— 纯代码，零成本

`speaker.py` 的 `SYSTEM_TMPL` 规矩 6：「事实卡里标了『预演 / 观察 / 受理』的字样必须
原样保留，不许说成『已退款』『已到账』」。`stages.PREVIEW_WORDING` 是那句定语。

```python
FORBIDDEN_BEFORE_RELEASE = ("已退款", "已到账", "已打款", "退款成功", "款项已发出")
def check_wording(speech, facts):
    if "预演" in facts or "观察" in facts:
        assert not any(w in speech for w in FORBIDDEN_BEFORE_RELEASE)
```

**合格线 100%，一条都不许破。** 放行前在群里说「已退款」是本系统最贵的一类错 ——
它不是措辞问题，是让人以为钱已经出去了。

#### 判据 3 · 越界率（规矩 5）—— 两级判，先便宜后贵

| 级 | 手段 | 成本 | 作用 |
|---|---|---|---|
| 3a | 关键词黑名单 | 0 | 风险岗说「不许退 / 拒赔 / 不予批准」、证据岗说金额，直接红 |
| 3b | LLM-as-judge | 每条一次调用 | 3a 放过的语义越界，抽样 20% 送判 |

**judge 的提示词要单岗单判**，别让 judge 同时看五段（它会开始比较五岗谁说得好，
而我们只问一件事：这一段有没有替别人下结论）。judge 的输出限定 `{"crossed": bool, "why": str}`。

**合格线：越界 = 0。** 这条比忠实度更严，因为越界的话在群里是会被人当真执行的。

#### 判据 4 · 接话率（规矩 4）—— 软指标，只观察不设线

后一岗有没有接住前一岗的话。judge 判，报百分比，**不作为合格线** ——
接不接话是文风，判错了去调提示词，不该让 CI 红。

### 4.2 抽样设计

模型非确定，**单次通过没有意义**。借 τ-bench 的 `pass^k`：同一输入独立跑 k 次，
k 次全过才算这一格通过。

| 参数 | 取值 | 理由 |
|---|---|---|
| 案子数 N | **10** | 从 L1 golden set 里挑，覆盖 approve/reject × 证据齐/缺/免除 × 风险 low/medium/high |
| 岗位数 | 5 | 全岗 |
| 重复 k | **5** | 硬判据下 k=5 足以暴露偶发幻觉；k=3 太松，k=10 成本翻倍收益递减 |
| 总调用 | **250** 次发言 + 约 50 次 judge（20% 抽样） | |

**成本估算**：单次 system+user 约 500 token in、发言上限 120 字约 200 token out。
250 次 ≈ 125K in / 50K out，按主流网关价不到一杯咖啡。**这不是一个需要省的量，
不要为了省钱把 k 降到 1** —— k=1 测出来的数字没有可比性，下次跑出别的数你无法判断
是模型变了还是抽样抖动。

**报告形式**（每岗一行）：

```
岗位            pass^1    pass^5   忠实度失败   措辞失败   越界   数字覆盖率
申请受理岗      50/50     10/10        0          0        0        94%
规则审核岗      49/50      9/10        1          0        0        91%
...
```

`pass^1` 分母是 N×k=50，`pass^5` 分母是 N=10。**两个都要报**：
`pass^1` 高而 `pass^5` 低说明失败是零散抖动，`pass^5` 也高才说明真的稳。

### 4.3 落地

**新文件**：`maos/tests/test_roundtable_fidelity_live.py`
（`_live` 后缀沿用 `test_pg_store_live.py` 的惯例：没有外部依赖就 skip）

```python
pytestmark = pytest.mark.skipif(
    not all(os.getenv(k) for k in ("MAOS_LLM_BASE_URL", "MAOS_LLM_API_KEY", "MAOS_LLM_MODEL")),
    reason="没有可用的模型网关：MAOS_LLM_* 三件套未配齐。配法见 maos/model/client.py 模块 docstring。")
```

**报告生成器**：`scripts/roundtable_fidelity_report.py`，产出
`evidence/fidelity/<日期>/report.json` + `report.md`，照仓库现有证据束的惯例。

> ### ⚠️ 这一层有一个会咬人的坑，先说
>
> `test_roundtable_team.py` 的 docstring 里有一条红色警告：
>
> > 🔴 模型一律**显式注入**。`maos/tests/conftest.py` 只清 `MATRIX_*`，不清 `MAOS_LLM_*` ——
> > 这台机器 source 过密钥的 shell 里，任何无参 `select_model_client()` 都会真打网关。
>
> 也就是说：**你在配好密钥的 shell 里跑「离线」测试，可能正在真打网关而不自知** ——
> 慢、花钱，且让本该确定性的 L1/L2 变成非确定的。
>
> **建 L3 的同时补一条守卫**：在 `conftest.py` 里加一个 autouse fixture，
> 默认清掉 `MAOS_LLM_*`，只有 `_live` 后缀的文件通过显式 opt-in 拿回来。
> 这条守卫的收益不止 L3 —— 它让现有 2259 条的「确定性」第一次真的成立。

---

## 5 L4 —— 端到端五岗联判

L1 保证每岗单独对，**不保证五岗放在一起不打架**。这一层测的是矛盾。

| # | 矛盾类型 | 用例 | 期望 |
|---|---|---|---|
| X-1 | 裁定与核算 | `policy=reject` | 圆桌**不做核算预演**（`stages.py` 已处理，钉住它） |
| X-2 | 裁定与核算（真跑） | 同一单三步直调 vs `run_payload` | 两者金额一致，或差异有据可查（见 §2.2 的坑） |
| X-3 | 证据与裁定 | `evidence=missing` | **不等于拒赔** —— 方向是「免责条款不予适用」，别测反 |
| X-4 | 风险与裁定 | `risk=high` | 风险岗**不拦付款**，`decision` 不因风险改变 |
| X-5 | 预演与真跑 | 预演金额 vs 落账金额 | 逐分相等（✅ 已有 `test_finance_preview_amount_equals_run_payload_amount`） |
| X-6 | 重跑稳定 | 同一案子跑两次 | 五岗出参除 `invocation_id` 外逐字一致 |
| X-7 | 五岗齐发 | 一轮圆桌 | 五岗各发且仅发一次，顺序 == `TEAM_ORDER`（✅ 已有） |
| X-8 | 单岗哑掉 | 某岗发声失败 | 后面几岗照发，钩子不抛（✅ 已有） |

> **X-3 的方向是这一层最容易搞反的。** `evidence_check` 模块 docstring 标题原话：
> 「举证不足 → 免责条款不予适用，**不是**拒赔」。把它测成「证据缺 → reject」会让一个
> 正确的实现变红，然后有人去「修」实现 —— 这是测试反过来把代码带坏的典型路径。

**建议规模**：**8 条**，其中 X-2 / X-6 净增。

---

## 6 执行计划

每步都给**验收命令**和**期望值**。期望值里的条数以本文档起草时实测的
`2259 passed, 41 skipped` 为基线 —— 隔几小时主干动了就会对不上，**执行前先重跑基线再改本表**
（这条坑吃过：写死的条数放久了会把「方案过期」报成「回归」）。

### 第 0 步 · 口径确认（你来，约 15 分钟）

不写代码，只定三件事：

1. **§2.5 F-7 的 `max(ratio)` / `max(fee)` 口径** —— 是有意的还是笔误？
   决定了那条测试写死哪个方向。
2. **L1 分支覆盖率的合格线**取 95% 还是更高。
3. **L3 的 k 值**：默认 5，成本敏感就 3（但别取 1，理由见 §4.2）。

> 第 1 条不定就往下走也行 —— 按现状（`max`/`max`）写死测试，把疑点记进
> `docs/open-questions.md`。**先钉住现状**，改口径时才有东西变红。

### 第 1 步 · 补 L1 golden set（净增约 29 条）

| 产出 | 文件 |
|---|---|
| 新用例 | `scenarios/refund/cases/case_*.json`（带 `_expected`） |
| 新测试 | 追加进现有 `maos/tests/test_refund_*.py`，不新建文件 |

**验收**：

```bash
python3 -m pytest maos/tests/test_refund_*.py -q -p no:randomly
python3 -m pytest maos/tests/ -q -p no:randomly --cov=maos/skills/builtin/refund --cov-branch
```

**期望**：全量条数 = 基线 + 净增数且 **0 failed**；退款域分支覆盖率 ≥ 95%。

### 第 2 步 · 补 L2 壳守卫（净增约 12 条）

**产出**：`maos/tests/test_refund_agent_shell.py`（新文件，A-1..A-7 参数化跑五岗）。

**验收**：`python3 -m pytest maos/tests/test_refund_agent_shell.py -q`
→ 全绿；**然后手工往某个 agent 里塞一行 `if score > 60:` 验证 A-3 真的会红**，
验完删掉。守卫不验证「能拦住」就只是一条永远绿的装饰。

### 第 3 步 · 补 `MAOS_LLM_*` 隔离守卫（**先于第 4 步**）

**产出**：`maos/tests/conftest.py` 加第三个 autouse fixture，清 `MAOS_LLM_BASE_URL` /
`MAOS_LLM_API_KEY` / `MAOS_LLM_MODEL` / `MAOS_LLM_TIMEOUT`，照现有 `_no_ambient_store_env`
的形状写（同一个文件里已有两个同形的 fixture：Matrix 四个、Store 两个）。

> **别给 L3 开 opt-in 后门。** 仓库对 live 测试的既有取舍写在 `_no_ambient_store_env`
> 的 docstring 里，原话是「刻意**不**给 live 测试开后门（不加 opt-in 参数、不改那两个文件）」——
> live 测试之所以没被 delenv 饿死，靠的是**时序**：模块级 `pytestmark` 在
> **collection 期**（import 那一刻）就求了值，而 autouse fixture 是 function scope，
> 最早也要等第一条用例 setup 才跑。**delenv 够不着一个已经做完的决定。**
>
> 所以 L3 只要照 §4.3 那样写模块级 `pytestmark`，就自动落在活路上，什么都不用加。
>
> **代价照实记**（这条 docstring 自己也记了）：这条活路没有任何断言钉着。
> 把 `pytestmark` 改成用例内 `pytest.skip()`，判定就挪进执行期、环境变量已被清空，
> L3 会当场全 skip —— 而**没配密钥的机器上跑不出这个差别**（本来就 skip，读数一样）。
> 唯一的哨兵是配了密钥的机器上 L3 必须真的跑起来，别只看绿。

**验收**：

```bash
export MAOS_LLM_BASE_URL=http://127.0.0.1:1 MAOS_LLM_API_KEY=x MAOS_LLM_MODEL=y
python3 -m pytest maos/tests/ -q -p no:randomly
```

**期望**：条数与不设这三个变量时**完全一致**、耗时不显著变长（约 66s 量级）。
不一致就说明现有测试里真的有在打网关的 —— 那是本次最值钱的发现，单独记一条。

### 第 4 步 · 建 L3 真模型忠实度（新面）

**产出**：`maos/tests/test_roundtable_fidelity_live.py` + `scripts/roundtable_fidelity_report.py`。

**验收**（分两次）：

```bash
python3 -m pytest maos/tests/test_roundtable_fidelity_live.py -q -rs   # 不配密钥
python3 -m pytest maos/tests/test_roundtable_fidelity_live.py -q       # 配了密钥
python3 scripts/roundtable_fidelity_report.py --cases 10 --k 5
```

**期望**：不配密钥时**全 skip 且理由可读**；配了密钥时产出
`evidence/fidelity/<日期>/report.md`，五岗各一行，**措辞失败与越界必须是 0**。

### 第 5 步 · 补 L4 联判（净增约 4 条）

**产出**：`maos/tests/test_refund_cross_stage.py`（X-2 / X-6 为主，其余复用现有）。

**验收**：`python3 -m pytest maos/tests/ -q -p no:randomly` 全绿。

### 工作量与拆轨

| 步 | 净增 | 独立开发者估时 | 能否并行 |
|---|---|---|---|
| 0 口径 | — | 15 min | 阻塞第 1 步的 F-7 一条 |
| 1 L1 | +29 | 1.5 天 | **可拆五轨**（一岗一轨，互不碰同一文件） |
| 2 L2 | +12 | 0.5 天 | 可与第 1 步并行 |
| 3 隔离守卫 | +2 | 1 小时 | **必须先于第 4 步** |
| 4 L3 | 新面 | 1 天 | 依赖第 3 步 |
| 5 L4 | +4 | 0.5 天 | 依赖第 1 步 |

**要拆多轨的话，第 1 步是天然的五轨**：五个岗位各自改各自的
`test_refund_<岗>.py` 与 `scenarios/refund/cases/`，唯一的共享面是用例目录 ——
按 `case_<岗位缩写>_*.json` 命名就不会撞。第 2、3 步各自独立成轨，共 7 轨。
按仓库惯例是 `claude-fleet t1..t7`，派单落 `review/paste-t1.md` … `paste-t7.md`。

---

## 7 读代码时发现的坑 —— 会污染正确率读数

按「会不会让你得出一个错的数」排序。

| # | 坑 | 后果 | 处置 |
|---|---|---|---|
| **1** | `policy.match` 前缀命中即 approve，**窗口判定不在其中** | 按「该不该退」给规则审核岗打分 → 系统性误判，且过窗口的用例在真实语料里占比不低 | §2.2，改判据不改代码 |
| **2** | `conftest.py` 不清 `MAOS_LLM_*`（实测：只清了 `MATRIX_*` 四个与 `MAOS_STORE_BACKEND`/`MAOS_PG_DSN`） | 配了密钥的 shell 里跑「离线」测试可能真打网关，确定性名存实亡 | 第 3 步，**优先级最高**；照既有 fixture 形状写，**不开 opt-in 后门** |
| **3** | `numbers_in()` 零调用方 | R1 铁律在真模型路径上无人查 | 第 4 步接上 |
| **4** | `numbers_in()` 只抓阿拉伯数字 | 「二十天」这类中文数字**漏检**（不误报） | 报告里声明，不修 |
| **5** | 子集判据对「漏说」免疫 | 模型只念一半数字照样过 | 单列「数字覆盖率」参考值，不设线 |
| **6** | `settled` 同时点亮两个信号（40+50=90） | 有人「修」掉重复计分 → 分档 high→medium 且无测试变红 | R-10 把 90 写死 |
| **7** | `finance._params_of` 的 `max(ratio)` + `max(fee)` 方向不一致 | 口径疑点 | 按现状写死 + 记 open-questions |
| **8** | `FALLBACK_IDENTITIES` 是过渡件 | 证据岗/风险岗的自我介绍暂来自 `team.py` 而非 Agent 声明；真 Agent 并入后此项作废 | L3 的 judge 别把它当稳定事实 |
| **9** | `risk_screen` 看不见进程内历史 | 「同一客户十分钟前刚退过」测不出来（已记 BACKLOG） | 用例只用入参里的 `refund_history` |
| **10** | `KIND_ALIASES` 只认字面同义词 | 覆盖不全是**预期内**的 | 别把「没覆盖的写法」当 bug 报 |
| **11** | 圆桌发言不经 `BaseAgent.ask` | 这几次调用**没有成本行**，L3 的花费在 `model_usage` 里查不到 | 报告生成器自己记 token 数 |

---

## 附录 A · 常用命令

```bash
# 全量基线（本文档起草时：2259 passed, 41 skipped, 66.24s）
python3 -m pytest maos/tests/ -q -p no:randomly

# 只跑退款域
python3 -m pytest maos/tests/test_refund_*.py maos/tests/test_roundtable_*.py -q

# 分支覆盖率
python3 -m pytest maos/tests/ -q --cov=maos/skills/builtin/refund --cov-branch --cov-report=term-missing

# 看 skip 理由（确认没有一条是模型相关）
python3 -m pytest maos/tests/ -q -rs 2>&1 | grep -i skip

# L3（需 MAOS_LLM_BASE_URL / MAOS_LLM_API_KEY / MAOS_LLM_MODEL）
python3 scripts/roundtable_fidelity_report.py --cases 10 --k 5
```

## 附录 B · 判定分支速查

| 岗位 | 出参关键字段 | 值域 |
|---|---|---|
| 受理 | `case_draft.biz_status` | `submitted` |
| 规则 | `decision` / `policy_version` / `rule_refs` | `approve` \| `reject` |
| 证据 | `verdict` / `requirement_source` | `complete`\|`partial`\|`missing`\|`not_required` / `policy`\|`default`\|`none` |
| 风险 | `level` / `score` | `low`\|`medium`\|`high` / 0..100 |
| 财务 | `amount_approved` / `breakdown.capped_by_paid` | `Decimal` 字符串 / bool |

---

*本方案只描述怎么测，不改任何被测代码。执行前先重跑基线（§6 抬头）。*
