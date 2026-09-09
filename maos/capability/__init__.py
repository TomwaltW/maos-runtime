"""职责能力档案层 —— 「什么职责该配什么 skill、什么工具」的单一事实表。

刻意不放进 ``maos/agents/``：那个包会 ``pkgutil.iter_modules`` 自动 import 包内
每一个非下划线开头的模块（冻结口径 C-2），本层一旦 import 失败就会把整个
``AGENT_POOL`` 拖成空的。档案是**旁路的核对面**，不该有能力弄塌注册面。

本包不导出任何符号：档案与体检机都在 ``profiles`` 模块里，按需显式 import。
留空的 ``__init__`` 也让 ``import maos.capability`` 不产生任何副作用 ——
``profiles`` 会 import 全仓的 agents / skills / tools 来取事实，那不该在
``import maos.capability`` 这一步就发生。
"""
