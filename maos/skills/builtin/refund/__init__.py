"""退款域的内置 Skill（R-2 落六个，W-6 为失败路径补第七个 refund.compensate）。

`builtin/__init__.py::discover()` 用 `pkgutil.iter_modules` 扫上一层，会把本包当成
一个模块 import 进去（`iter_modules` 同时枚举模块与子包）—— 于是本文件被执行，
下面几行 import 触发 `@register_skill`，它们就进注册表了。
**投放即注册这条口径没有被破坏**：往 `builtin/` 放的是这个包，不是一堆散文件，
而 `builtin/__init__.py` 一个字都不用改 —— W-6 往包里加第七个 skill 时同样没动它。

这里必须写显式清单（与 `builtin/__init__.py` 的「不维护清单」相反）：本包是单轨
独占的，不存在多轨同改一处的合并冲突；而再套一层 pkgutil 只会让「哪些是 skill、
哪些是共用件」变成靠下划线约定，反而更容易漏。

`_common.py` 不在清单里 —— 它是共用件不是 skill，由各 skill 模块自己 import。
"""

from __future__ import annotations

from . import compensate  # noqa: F401 —— refund.compensate（W-6：失败路径的域内补偿）
from . import evidence_check  # noqa: F401 —— refund.evidence_check
from . import finance  # noqa: F401 —— import 即注册（finance.settle）
from . import intake  # noqa: F401 —— refund.intake
from . import notify  # noqa: F401 —— notify.customer
from . import payment_execute  # noqa: F401 —— payment.execute
from . import payment_observe  # noqa: F401 —— payment.observe
from . import policy  # noqa: F401 —— policy.match
from . import risk_screen  # noqa: F401 —— refund.risk_screen

#: 本域 skill 的名字清单，测试与场景按它做存在性断言，不在各处抄字面量。
REFUND_SKILLS = (
    "refund.intake",
    "policy.match",
    "finance.settle",
    "payment.execute",
    "payment.observe",
    "notify.customer",
    "refund.compensate",
    "refund.evidence_check",
    "refund.risk_screen",
)
