"""RTV（Return to Vendor，采购退货退款）域的五个 Agent。

`maos/agents/__init__.py` 用 `pkgutil.iter_modules` 扫上一层，会把本包当成一个模块
import 进去（`iter_modules` 同时枚举模块与子包）—— 于是本文件被执行，下面五行 import
触发 `@register`，五个角色就进 `AGENT_POOL` 了。
**投放即注册这条口径没有被破坏**：`maos/agents/__init__.py` 一个字都不用改（C-2）。

`_base.py` 是共用件不是 Agent，不在清单里，由各 Agent 模块自己 import；
它的 artifact kind 常量在这里再导出一次，测试与场景按名取，不抄字面量。
"""

from __future__ import annotations

from ._base import (  # noqa: F401 —— 对外导出 artifact kind 口径（契约 C-R6）
    ALL_RTV_KINDS,
    KIND_RTV_DISPOSITION,
    KIND_RTV_INTAKE,
    KIND_RTV_RECONCILIATION,
    KIND_RTV_SETTLEMENT_ADVICE,
    KIND_RTV_SHIPMENT,
)
from .disposition_agent import RtvDispositionAgent  # noqa: F401 —— 三选一裁定
from .intake_agent import RtvIntakeAgent  # noqa: F401 —— import 即注册
from .logistics_agent import RtvLogisticsAgent  # noqa: F401 —— 发运 + 承运商回执
from .reconcile_agent import RtvReconcileAgent  # noqa: F401 —— 三方对账
from .settlement_agent import RtvSettlementAgent  # noqa: F401 —— 两个权威终态的观察岗

#: 本域五个角色的 role 名。场景的 DAG 与测试按它派单，不在各处抄字面量。
ROLE_INTAKE = RtvIntakeAgent.identity.role
ROLE_DISPOSITION = RtvDispositionAgent.identity.role
ROLE_LOGISTICS = RtvLogisticsAgent.identity.role
ROLE_RECONCILE = RtvReconcileAgent.identity.role
ROLE_SETTLEMENT = RtvSettlementAgent.identity.role

#: 顺序即 SOP 五步（契约 C-R2 的映射表），场景的依赖链照它连。
RTV_ROLES = (ROLE_INTAKE, ROLE_DISPOSITION, ROLE_LOGISTICS, ROLE_RECONCILE,
             ROLE_SETTLEMENT)
