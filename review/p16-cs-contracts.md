# 客服前台 p16 · 跨轨契约（W-A：T187 / T188 / T189）

写于 2026-09-26，基线 = integrate/p15 收尾 b33e207 + 本期骨架提交。本文件是 p12–p15 契约的**增量**，冲突以本文件为准。

p16 只做一件事：**放开话术库的增补棘轮，给近邻兜底补例句**（p15 结论：这是剩余收益最大的一处），并用一份**新的盲写留出集**
（带错别字覆盖地板）来裁判。另补一道 p15 发现的判据缺口：「零自信答错」从意图级收紧到篇级。

## 0. 硬约束（每轨照抄）

* 铁律照旧；不新增表；不动 types.py / ports.py / 契约钉子（test_cs_contract_p1[2-5].py）。
* **盲**：除主会话与 T187 外，任何实现者 / 复核者不许读 `scenarios/cs/eval/p1[2456]_holdout_cases.json` 与
  `maos/tests/test_cs_eval_p1[2456]_holdout.py`。T187 不许读实现、话术正文与例句、开发集、旧留出集（§2 T187）。
* **不变量**（全量里有测试钉）：p12 / p13 开发集全绿；编造 0；错状态 0；零自信答错（T189 之后是篇级）；
  触发词召回不降；p12 / p14 / p15 留出集地板不降；无端口（p12）路径上只许检索层变化（判定顺序不动）。

## 1. 用户授权：增补棘轮放开（骨架已提交，主会话）

`maos/tests/test_cs_eval_p12_t169.py` 的增补棘轮原先只许三类（催发货 / 客套开场 / 问退款时长）各补一篇。用户 2026-09-26
授权放开：

* 声明文件 `scenarios/cs/kb/additions_p16.json`：每条 `{scheme_no, variant, gap, kind, reason}`，kind ∈ synonym / example。
  没声明就补的说法，基线指纹照旧判红。
* 类别表 `GAP_CLASSES_P16`（九类，按「客户在问什么」分，每类只许补给表里那几篇）；每篇 ≤ 8 条；理由必填。
* **防抄对全部评测集**（两份开发集 + 全部留出集，含 T187 新写的）：≥ 5 字片段 / 一字之差 / 换词三道判据；
  失败消息只报 (编号, 增补) 与集合名 + case id，不回显评测句。**撞上就整条撤掉，不许改几个字再试**（那等于拿留出集调）。

## 2. 各轨

### T187 · p16 盲写留出集（scenarios/cs/eval/p16_holdout_cases.json）

* 形状同 p13_cases.json；≥ 70 case、≥ 150 轮；全部 synthetic；至少一半是日常客服流量。
* **错别字覆盖地板**：≥ 16 轮带有意的错别字（同音字、形近字、拼音首字母缩写、漏字 / 多字各 ≥ 3 轮），case 的 tags 里标
  `typo`，测试按 tag 计数钉住。
* 覆盖：九个 GAP_CLASSES_P16 类别各 ≥ 5 轮；查单各结果（ok 三态中英 / not_found / amended / 平台不映射 / 系统没配 / 身份不过）、
  追问与追问用尽、退款桥、英文、多轮、不该答的（→ 兜底 / repeated_fallback）。
* 期望照契约判定顺序推（p13 §2、p15 §1 的裁定、p15 附录 A 的触发词地板与优先级）。**夹具口径补丁**（p15 留出集在这里踩过）：
  退款 / 退货诉求查单成功后，前台调预检传的 `order_no` 是**绑定解析出的 query_key**（内部单号，DECISIONS task-t174 L2-1），
  不是客户报的 display_no。所以 `fixtures.precheck` 的键必须写 **query_key**；binding 的 query_key 与 display_no 不同时，
  按 display_no 写的预检夹具查不到 → ok=False → needs_order_lookup。p13 §2 第 6 步 d：refund / return 两种诉求都走预检，
  预检 ok → refund_request，不 ok → needs_order_lookup。
* 预登记门槛（一经提交不许改）：intent 0.85 / route 0.85 / handoff 0.90 / fabrication 0 / wording 1.0 / wrong_status 0。
* **盲写**：可以读五份契约、ports.py、types.py、evaluate.py（只看形状）、cs_scripts.json 里的 doc_id / title（不读正文、同义词、例句）。
  **不许读** understand.py / triggers.py / lang.py / desk.py / scripts.py / identity.py / similar.py、话术正文与例句、
  additions_p16.json、两份开发集、四份旧留出集及其测试；不许用真前台跑。
* 测试 maos/tests/test_cs_eval_p16_holdout.py：docstring 含 `PYTEST_DONT_REWRITE`；形状、覆盖与错别字地板、门槛钉死（与
  maos/tests/test_cs_contract_p16.py 逐字相等）、与开发集 / 话术库例句 / 旧留出集的近重复棘轮（只报 id）。不跑真前台。

### T188 · 例句扩充与近邻阈值重扫

* 按 GAP_CLASSES_P16 的九类，把自写的多样说法声明进 additions_p16.json（每篇 ≤ 8），`scripts/gen_cs_kb.py` 读它并进
  cs_scripts.json（生成器的自检照旧：状态字眼、错别字登记、跨篇不重复；增补里有意写的错别字要登记进该篇 typos）。
* 说法要求：像真人（口语、错别字、省略、夹英文），**不带具体地名 / 商品 / 日期 / 数字 / 单号**；每类覆盖多种句式
  （问句、陈述、祈使、反问）。**只凭类别名与话术标题自己写**，不看任何评测集。
* 重扫 `similar.py` 的阈值与差距（只在开发集 + 自写说法上扫；给出召回 vs 误命中表写 DECISIONS），零自信答错优先。
* 测试 maos/tests/test_cs_similar_t188.py（新）：每类 ≥ 10 句**另写的**（不在 additions 里的）说法的召回、≥ 40 句不该命中的弃权率、
  增补前后对照（写出实测数字）。

### T189 · 篇级「零自信答错」

* `evaluate.py`：EvalReport 增加 `confident_wrong`（route=answer 的轮中，意图错、或期望给了 cite 而实得引用不含它，计 1）
  与 `confident_wrong_ids()`；`metrics()` 多出 `confident_wrong` 一项；`describe()` 带上它。不改 THRESHOLD_KEYS。
* `scripts/cs_eval.py` 的输出与 `--json` 带上 confident_wrong；两份开发集的测试钉 `confident_wrong == 0`
  （p12 开发集 KNOWN_GAPS 那一轮若因此计入，照实钉住并写 DECISIONS）。
* 测试 maos/tests/test_cs_eval_confident_wrong_t189.py（新）：构造正反例（意图对篇错、意图错、兜底不计、转人工不计）。

## 3. 门槛钉子（骨架已提交）

maos/tests/test_cs_contract_p16.py 钉 §2 T187 的预登记门槛；文件合入后逐字比对。

## 4. 轨与文件（零交集）

| 轨 | 独占 |
| :-- | :-- |
| T187 | scenarios/cs/eval/p16_holdout_cases.json（新）；maos/tests/test_cs_eval_p16_holdout.py（新） |
| T188 | scenarios/cs/kb/additions_p16.json、cs_scripts.json；scripts/gen_cs_kb.py；maos/domain/cs/similar.py（只许动阈值 / 差距常量与其注释）；maos/tests/test_cs_similar_t188.py（新）；T168 / T173 / T182 / T186 测试里被本轨改到的钉子（只许改钉子；test_cs_eval_p12_t169.py 除外 —— 那份的 p16 判据是主会话的） |
| T189 | maos/domain/cs/evaluate.py、scripts/cs_eval.py；maos/tests/test_cs_eval_confident_wrong_t189.py（新）；test_cs_eval_runner_t170 / t175、test_cs_eval_cli_t177、test_cs_eval_p13_t174 里被本轨改到的钉子（只许改钉子） |

两本账同前：尾部另起 `## task-t18N（标题，2026-09-26）`，Phase 写 p16。

## 5. 整合

主会话：合流、全量（只许容器里那 7–8 条环境性红）；增补对 p16 新留出集的防抄（T188 看不到那份，撞上的整条撤掉）；
量 p16 留出集（唯一一次验收读数）并接上地板；旧留出集地板只许抬；刷真源；两本账；推送会话分支。
