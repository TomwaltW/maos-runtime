# 客服前台 p15 · 跨轨契约（W-A：T183 / T184 / T185 / T186）

写于 2026-09-25，基线 = integrate/p14 收尾 6e65ebd + 本期骨架提交。本文件是 p12 / p13 / p14 契约的**增量**，
冲突以本文件为准。

p15 只做一件事：**把留出集上剩下的误判收掉，并用一份新的盲写留出集来裁判**。

* p12 / p14 两份留出集主会话都看过误判明细，**不再是盲的**：它们降级为回归地板（只许抬不许降），不再作验收。
* 验收只认 p15 新盲写的留出集（T183），门槛预登记（§3），主会话整合期量一次。
* 手段从「逐句补正则」转到**结构**：判定顺序（T184）、单号抽取与规范化（T185）、检索召回的确定性近邻兜底（T186）。

-----

## 0. 硬约束（每轨照抄）

* 铁律 1 / 8 / 9 照旧；**不新增表、不动 types.py / ports.py / 任何契约钉子**；p15 骨架只加本文件与 §3 的门槛钉子。
* `maos/domain/cs/**`、`maos/skills/builtin/cs/**` 仍在静态守卫范围（失败即关白名单）：T184 / T185 / T186 的新代码只许
  标准库 + 白名单内的 maos 模块。
* **盲**：除主会话与 T183 外，任何实现者 / 复核者不许读 `scenarios/cs/eval/p1[245]_holdout_cases.json` 与
  `maos/tests/test_cs_eval_p1[245]_holdout.py`。T183 不许读实现与开发集（§2 T183）。
* **不变量**（每轨都要守，全量里有测试钉）：p12 / p13 开发集全绿；编造 0；错状态 0；**零「自信答错」**
  （route=answer 的轮意图必须对）；触发词召回不低于 p12 地板（R1，不许收窄）；p12 / p14 留出集地板不降。
* 核心零依赖；测试不打真网、Scripted 模型；不调模型（T186 的近邻是纯字符统计，不是嵌入）。

## 1. 误判类别（主会话从 p12 / p14 留出集归纳，只给类别、不给句子）

| # | 类别 | 归属 |
| :-- | :-- | :-- |
| C1 | 注入端口时，泛泛的「帮我查物流 / 查个快递 / 看下我包裹」没有单号 → 应先追问单号（clarify / ask=order_no），现在经 LOG-004 那篇的转人工标记直接 needs_order_lookup | T184 |
| C2 | 英文的订单状态问法（「is order X on its way」「has X shipped」「where's my package」一类）没进查单 | T184 |
| C3 | 同一槽位追问用尽（MAX_ASKS_PER_SLOT）之后，客户下一轮仍没给单号、但仍在要看订单（「找不到单号，你直接告诉我在哪」「no idea what the number is, can I just return it」）→ 应 needs_order_lookup 转人工，现在落兜底 | T184 |
| C4 | 假设 / 泛问句（「要是退货的话运费谁出」「如果…怎么办」）问的是规则，不该去追问单号；应走政策话术 | T184 |
| （C5） | 主会话裁定**不改**：「明确要真人 + 带情绪」按 p12 契约 §1.4 的冻结优先级取 anger（privacy > compensation > anger > complaint > requested）。p14 留出集那一轮是期望与契约冲突，不是实现缺口 | — |
| C6 | 单号紧贴商品名或中文（「XX1234 耳机坏了」「单号AB3321前天拍的」）抽不出 | T185 |
| C7 | 带「#」「No.」「单号：」等前缀的单号（「order #88120937」）规范化后对不上绑定 | T185 |
| C8 | 数字不是单号却被当成单号（「尾数 4417」只是尾号、「1203 写成 1302」是门牌号、「28 号申请的」是日期）→ 不能拿去查单，也不能挡住政策 / 转人工判定 | T185 |
| C9 | 政策问答的口语说法检索召回不到（七天无理由的各种说法、退货步骤、运费谁出、退款原路与到账时长、发什么快递 / 最快哪天发、寒暄的口语变体）→ 落兜底 | T186 |
| C10 | 具体订单异常的口语说法（卡在中转站、改地址、显示签收没收到、破损、同意退款钱没到、重复扣款）检索召回不到 → 应走对应的查单篇（p12 路径 needs_order_lookup，p13 路径进查单 / 追问） | T186 |

## 2. 各轨

### T183 · p15 盲写留出集（scenarios/cs/eval/p15_holdout_cases.json）

* 形状同 p13_cases.json（`run_eval_p13` 读得动），≥ 60 case、≥ 130 轮，全部 `"synthetic": true`。
* 覆盖：售后政策问答（话术库每个意图至少 3 种说法）、订单进度 / 异常、查单各结果（ok 三态中英 / not_found / amended /
  平台不映射 / 系统没配 / 身份不过）、追问与追问用尽、退款桥（ok 与拒）、英文、寒暄与致谢、多轮混合、
  **以及不该答的**（商品咨询、闲聊、无关问题 → 兜底，连续两轮兜底 → repeated_fallback）。§1 的十个类别各 ≥ 4 轮，
  但**不许**只写这些类别 —— 至少一半的轮是日常客服流量。
* 期望照契约判定顺序推（p13 契约 §2 + 本文件 §1 的裁定：C1 追问、C3 转人工、C4 走政策、C8 不查单；原因优先级照附录 A）。
  **注意 p12 触发词地板**：本文件附录 A 列出地板上的触发说法与原因优先级 —— 期望里凡是含这些说法的一轮都应是转人工，
  原因按优先级取。
* 预登记门槛（**一经提交不许改**）：intent 0.85 / route 0.85 / handoff 0.90 / fabrication 0 / wording 1.0 / wrong_status 0。
* **盲写**：可以读四份契约、ports.py、types.py、evaluate.py（只看 case 形状与夹具说明）、corpus.py 里话术的 doc_id 与意图
  （写 cite 期望用，不读话术正文与例句）。**不许读** understand.py / triggers.py / lang.py / desk.py / scripts.py /
  identity.py、scenarios/cs/kb/cs_scripts.json、两份开发集、三份旧留出集及其测试；**不许用真前台跑**自己写的集合。
* 测试 maos/tests/test_cs_eval_p15_holdout.py：模块 docstring 必须含 `PYTEST_DONT_REWRITE`（关掉断言改写，防 repr 外泄
  留出句）；load_cases 读得动；覆盖地板；门槛钉死；与开发集 / 话术库例句 / 三份旧留出集的近重复棘轮（失败消息只报 id）。
  **不跑真前台**：真前台读数由主会话整合期接上。

### T184 · 判定顺序（maos/domain/cs/desk.py、triggers.py）

* C1：注入端口、本轮在要看订单（进度 / 异常线索或查物流一类说法）但会话里没有单号 → clarify 追问 order_no，
  **不**经话术篇的转人工标记直接 needs_order_lookup。没注入端口时（p12 路径）逐字节不变。
* C2：`order_need` 认英文的订单状态问法（本轮带单号或会话里已有单号时进查单，都没有就 C1 追问）。
* C3：追问已用尽的槽位，下一轮仍没给、且仍在要看订单 → needs_order_lookup（原因与卡片同 p13 口径）。
* C4：假设 / 泛问句不触发 `order_need`（问规则），走政策检索。
* triggers.py 本期**不改**（优先级是 p12 契约冻结的；召回地板不收窄）。T184 的白名单里保留它只为改测试钉子时对照，改动须为零。
* 测试 maos/tests/test_cs_desk_t184.py（新）：每个类别 ≥ 8 种自写说法 + ≥ 4 条反例（相近但不该改变判定的）。

### T185 · 单号抽取与规范化（maos/domain/cs/understand.py 的 extract_order_no 一族、identity.py 的 normalize_display_no）

* C6：单号与中文 / 商品名紧贴时照样抽出（边界按「字母数字串 ↔ 非字母数字」判，不靠空格）。
* C7：`normalize_display_no` 去掉 `#` / `No.` / `NO:` / `单号：` / `订单号` 一类前缀（大小写、全半角）；绑定写入与核验
  两侧同一个函数，所以旧绑定行不受影响（**写一条测试证明**：p13 夹具与开发集里的绑定照旧命中）。
* C8：尾号（「尾数 / 尾号 / 后四位」后的 ≤ 4 位数字）、门牌 / 楼层 / 房号、日期（「28 号」「9 月 3 日」）、金额、
  数量、电话不当单号。尾号可以进槽位作为**提示**（不查单），实现自定，写 DECISIONS。
* 测试 maos/tests/test_cs_understand_t185.py（新）。

### T186 · 检索召回的确定性近邻兜底（maos/domain/cs/similar.py（新）、scripts.py、话术库例句）

* 词法检索（现有 match_scripts）**零命中**时，才用近邻兜底：对话术库每篇的**例句**（cs_scripts.json 里已有的例句字段，
  可扩充）做字符 n-gram（1–3）加权相似度，取最高一篇；**分数低于阈值或第一第二名差距不足 → 弃权**（照旧兜底）。
  阈值与差距只许在**开发集 + 你自写的类别例句**上定，写进 DECISIONS。
* 近邻命中的那篇照常走 cs.answer 的组稿与后置校验（带 `kb:<doc_id>` 引用；KbRetrieved 行的 detail 标明
  `channel: "similar"`，不含客户原文）。转人工标记的篇照旧转人工（C10）。
* 例句扩充走 scripts/gen_cs_kb.py 重产；近重复棘轮照旧；每篇新增例句 ≤ 8 条，按类别写多样说法，不许带具体地名 /
  商品 / 日期 / 数字去凑某一句。
* 零自信答错优先于召回：宁可弃权。测试 maos/tests/test_cs_similar_t186.py（新）：C9 / C10 各 ≥ 20 句自写说法的
  召回、≥ 30 句不该命中的说法（商品咨询、闲聊、无关问题、否定句、第三人称）的弃权率。

## 3. 门槛钉子（骨架已提交）

maos/tests/test_cs_contract_p15.py 钉住本文件 §2 T183 的预登记门槛字面量（T183 写进留出集的 `_thresholds` 必须与之逐字相等）。

## 4. 轨与文件（零交集）

| 轨 | 独占 |
| :-- | :-- |
| T183 | scenarios/cs/eval/p15_holdout_cases.json（新）；maos/tests/test_cs_eval_p15_holdout.py（新） |
| T184 | maos/domain/cs/desk.py、triggers.py；maos/tests/test_cs_desk_t184.py（新）；T169 / T173 / T174 / T182 测试里被本轨改到的钉子（只许改钉子） |
| T185 | maos/domain/cs/understand.py、identity.py；maos/tests/test_cs_understand_t185.py（新）；T171 / T173 / T174 / T182 测试里被本轨改到的钉子（只许改钉子） |
| T186 | maos/domain/cs/similar.py（新）、scripts.py；scripts/gen_cs_kb.py、scenarios/cs/kb/cs_scripts.json；maos/skills/builtin/cs/answer.py（只许把近邻通道接进来）；maos/tests/test_cs_similar_t186.py（新）；T168 / T173 / T182 测试里被本轨改到的钉子（只许改钉子）；gen_docs 生成文档 |

四轨同一波并行（文件零交集）。两本账同前：尾部另起 `## task-t18N（标题，2026-09-25）`，Phase 写 p15。

## 5. 整合

主会话：四轨合流后跑全量（只许容器里那 8 条环境性红）；量 p15 留出集（首次、唯一一次验收读数）并接上真前台地板；
p12 / p14 留出集地板只许抬；`cs_eval.py --set dev13 --db X` → `verify.py --cs --db X` 的 cs/claim-basis PASS；刷真源；
写 integrate-p15 两本账；快进会话分支推送。

## 附录 A · p12 触发词地板（T183 写期望用；子串即中，出自契约词表与 p12 变体）

地板是契约裁定（p13 复核 R1：召回不低于 p12、一个字不许收窄），不是实现细节。一轮里含下列任一说法，期望就是转人工；
同时含几类时原因按冻结优先级取：**privacy > compensation > anger > complaint > requested**（p12 契约 §1.4）。

* requested：转人工、人工客服、找人工、人工（任何含「人工」的词，「人工草坪」一类商品名除外）、真人、活人、
  客服小姐姐 / 小哥 / 妹妹、「不想 / 不要跟机器人」、「叫 / 找 / 换 你们经理 / 主管 / 负责人 / 领导 / 老板」；
  英文 talk to a human / a real person / an agent 一类。
* complaint：投诉、12315、消协、消费者协会、消保委、工商局、市场监管、曝光、起诉、律师、法院、告你们、举报、维权、黑猫投诉。
* anger：垃圾、骗子、骗人、骗钱、坑人、坑爹、黑店、气死、气炸、恶心、混蛋、王八蛋、无耻、他妈、妈的、傻逼、去死、
  什么破、破店、单字「滚」（滚筒 / 滚动一类除外）、连续感叹号。
* compensation：赔（任何「赔」，「赔本」除外）、赔偿、补偿、赔钱、损失费、精神损失。
* privacy：手机号、手机号码、电话号码、身份证、身份证号、身份信息、住址、个人信息、个人资料、隐私、银行卡号、泄露、泄漏。
