"""银行差错处理域的四个 Agent。

`maos/agents/__init__.py` 用 `pkgutil.iter_modules` 扫上一层，会把本包当成一个模块
import 进去（`iter_modules` 同时枚举模块与子包）—— 于是本文件被执行，下面四行 import
触发 `@register`，四个角色就进 `AGENT_POOL` 了。
**投放即注册这条口径没有被破坏**：`maos/agents/__init__.py` 一个字都不用改（冻结契约 C-2）。

`_base.py` 是共用件不是 Agent，不在清单里，由各 Agent 模块自己 import；
它的 artifact kind 常量在这里再导出一次，测试与场景按名取，不抄字面量。
口径同 `maos/agents/refund/__init__.py`。
"""

from __future__ import annotations

from ._base import (  # noqa: F401 —— 对外导出 artifact kind 口径
    ALL_INVESTIGATION_KINDS,
    KIND_CANCELLATION_REQUEST,
    KIND_CASE_FILE,
    KIND_CLASSIFICATION,
    KIND_RESOLUTION,
)
from .cancel_agent import InvestigationCancelAgent  # noqa: F401 —— 发 camt.056（高风险落地）
from .classify_agent import InvestigationClassifyAgent  # noqa: F401
from .intake_agent import InvestigationIntakeAgent  # noqa: F401 —— import 即注册
from .observe_agent import InvestigationObserveAgent  # noqa: F401 —— 唯一能促成 returned

#: 本域四个角色的 role 名。场景的 DAG 与测试按它派单，不在各处抄字面量。
ROLE_INTAKE = InvestigationIntakeAgent.identity.role
ROLE_CLASSIFY = InvestigationClassifyAgent.identity.role
ROLE_CANCEL = InvestigationCancelAgent.identity.role
ROLE_OBSERVE = InvestigationObserveAgent.identity.role

INVESTIGATION_ROLES = (ROLE_INTAKE, ROLE_CLASSIFY, ROLE_CANCEL, ROLE_OBSERVE)
