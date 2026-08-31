"""应付账款域的四个 Agent。

`maos/agents/__init__.py` 用 `pkgutil.iter_modules` 扫上一层，会把本包当成一个模块
import 进去（`iter_modules` 同时枚举模块与子包）—— 于是本文件被执行，下面四行 import
触发 `@register`，四个角色就进 `AGENT_POOL` 了。
**投放即注册这条口径没有被破坏**：`maos/agents/__init__.py` 一个字都不用改（C-2）。

`_base.py` 是共用件不是 Agent，不在清单里，由各 Agent 模块自己 import；
它的 artifact kind 常量在这里再导出一次，测试与场景按名取，不抄字面量。
"""

from __future__ import annotations

from ._base import (  # noqa: F401 —— 对外导出 artifact kind 口径
    ALL_AP_KINDS,
    KIND_BANK_ADVICE,
    KIND_INVOICE_INTAKE,
    KIND_MATCH_RESULT,
    KIND_PAYMENT_INSTRUCTION,
    KIND_PAYMENT_PLAN,
)
from .control_agent import ApControlAgent  # noqa: F401 —— 出付款计划，交人批
from .intake_agent import ApIntakeAgent  # noqa: F401 —— import 即注册
from .match_agent import ApMatchAgent  # noqa: F401 —— 三单匹配
from .treasury_agent import ApTreasuryAgent  # noqa: F401 —— 发指令 + 观察回单

#: 本域四个角色的 role 名。场景的 DAG 与测试按它派单，不在各处抄字面量。
ROLE_INTAKE = ApIntakeAgent.identity.role
ROLE_MATCH = ApMatchAgent.identity.role
ROLE_CONTROL = ApControlAgent.identity.role
ROLE_TREASURY = ApTreasuryAgent.identity.role

AP_ROLES = (ROLE_INTAKE, ROLE_MATCH, ROLE_CONTROL, ROLE_TREASURY)
