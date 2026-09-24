"""客服前台（customer service front desk）—— p12 起。

MAOS 有后台、没有前台：客户在外部渠道（微信客服）里说一句「我的耳机三天没发货」，
p11 为止的 router 一声不吭。本包是接在后台之上的**对话层**：会话对象、话术检索、
回复的确定性后置校验、转人工卡片。形状照 ADP 课程 3.3「单工作流：客服助手」，
承载照 MAOS 的纪律（每一步进 event_log、状态字眼必须挂观察）。

跨轨契约：review/p12-cs-contracts.md。冻结的类型与常量在 `maos.domain.cs.types`，
由 `maos/tests/test_cs_contract_p12.py` 逐字段钉住。

三条红线（契约 §2 全文）：

* 会话进行到哪一步是**会话对象自己的字段**（``cs_conversation.stage``），不是 Task
  状态 —— 铁律 9。本包不 import、不改 ``maos/contracts/**``。
* 前台只转述**观察到的**事实。回复里的状态字眼（已到账 / 已发货 ……）挂不上本轮
  观察行就被后置校验换成兜底 + 转人工 —— 铁律 8。p12 不查单、不读支付观察，所以
  p12 的前台**说不出任何状态**，这是设计，不是缺口。
* 外部渠道零授权：本包与 ``maos/skills/builtin/cs/`` 不许 import 或调用审批、放款、
  补偿、查单任何一条路（静态守卫见契约 §2.3）。

**不进** ``maos.domain.DOMAIN_REGISTRY``：注册表里的域都持有一个权威终态与它的观察表，
前台两样都没有，登记进去会把它拖进 verify 第 3/6/7 项。

本 ``__init__`` 刻意不 import 任何子模块：谁要什么就 import 什么，免得 import 本包
就把检索、渠道、skill 注册一串拖进来。
"""
