"""客服前台的内置 Skill（p12 T169）：``cs.answer`` 与 ``cs.handoff``。

照退款包的先例**逐个显式 import**：``builtin/__init__.py::discover()`` 只扫上一层，
会把本包当成一个模块 import 进来 —— 下面两行 import 触发 ``@register_skill``。
放进本目录不等于注册，漏加一行就是死代码。

两个 skill 都**没有在池角色持有**（``owner_roles=[]``）：调用方是客服前台的专用身份
``maos.domain.cs.desk.CS_FRONT_DESK_IDENTITY``，它不进 AGENT_POOL（口径同
``outcome_commands.TICKET_DESK_IDENTITY``）。``test_capability_profiles`` 的基线因此各记一条
skill-unowned。
"""

from __future__ import annotations

from . import answer  # noqa: F401 —— cs.answer（检索 + 组稿 + 后置校验）
from . import handoff  # noqa: F401 —— cs.handoff（转人工卡片落库）

#: 本包 skill 的名字清单，测试按它做存在性断言。
CS_SKILLS = ("cs.answer", "cs.handoff")
