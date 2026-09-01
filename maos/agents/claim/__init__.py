"""保险理赔域的四个 Agent。

`maos/agents/__init__.py` 用 `pkgutil.iter_modules` 扫上一层，会把本包当成一个模块
import 进去（`iter_modules` 同时枚举模块与子包）—— 于是本文件被执行，下面四行 import
触发 `@register`，四个角色就进 `AGENT_POOL` 了。
**投放即注册这条口径没有被破坏**：`maos/agents/__init__.py` 一个字都不用改（C-2）。

`_base.py` 是共用件不是 Agent，不在清单里，由各 Agent 模块自己 import；
它的 artifact kind 常量与 `RECEIPT_FIELD` 在这里再导出一次，测试与场景按名取，
不抄字面量。
"""

from __future__ import annotations

from ._base import (  # noqa: F401 —— 对外导出 artifact kind 与回执键名口径
    ALL_CLAIM_KINDS,
    KIND_ADJUDICATION,
    KIND_CLAIM_DRAFT,
    KIND_PAYER_RECEIPT,
    KIND_PAYMENT_INSTRUCTION,
    KIND_SETTLEMENT,
    RECEIPT_FIELD,
)
from .adjudicator_agent import ClaimAdjudicatorAgent  # noqa: F401 —— import 即注册
from .intake_agent import ClaimIntakeAgent  # noqa: F401
from .payment_agent import ClaimPaymentAgent  # noqa: F401
from .settlement_agent import ClaimSettlementAgent  # noqa: F401

#: 本域四个角色的 role 名。场景的 DAG 与测试按它派单，不在各处抄字面量。
ROLE_INTAKE = ClaimIntakeAgent.identity.role
ROLE_ADJUDICATOR = ClaimAdjudicatorAgent.identity.role
ROLE_SETTLEMENT = ClaimSettlementAgent.identity.role
ROLE_PAYMENT = ClaimPaymentAgent.identity.role

CLAIM_ROLES = (ROLE_INTAKE, ROLE_ADJUDICATOR, ROLE_SETTLEMENT, ROLE_PAYMENT)
