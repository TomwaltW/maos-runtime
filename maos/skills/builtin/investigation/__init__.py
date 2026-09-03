"""银行差错处理域的内置 Skill（五个）。

`builtin/__init__.py::discover()` 用 `pkgutil.iter_modules` 扫上一层，会把本包当成
一个模块 import 进去（`iter_modules` 同时枚举模块与子包）—— 于是本文件被执行，
下面五行 import 触发 `@register_skill`，它们就进注册表了。
**投放即注册这条口径没有被破坏**：往 `builtin/` 放的是这个包，不是一堆散文件，
而 `builtin/__init__.py` 一个字都不用改（冻结契约 C-1）。

这里必须写显式清单（与 `builtin/__init__.py` 的「不维护清单」相反）：本包是单轨
独占的，不存在多轨同改一处的合并冲突；而再套一层 pkgutil 只会让「哪些是 skill、
哪些是共用件」变成靠下划线约定，反而更容易漏。口径同 `builtin/refund/__init__.py`。

`_common.py` 不在清单里 —— 它是共用件不是 skill，由各 skill 模块自己 import。
"""

from __future__ import annotations

from . import cancel  # noqa: F401 —— investigation.cancel（发 camt.056）
from . import classify  # noqa: F401 —— investigation.classify（定性选官方原因码）
from . import compensate  # noqa: F401 —— investigation.compensate（失败路径收口）
from . import intake  # noqa: F401 —— investigation.file（受理）
from . import observe  # noqa: F401 —— investigation.observe（**唯一可写 returned**）

#: 本域 skill 的名字清单，测试与场景按它做存在性断言，不在各处抄字面量。
INVESTIGATION_SKILLS = (
    "investigation.file",
    "investigation.classify",
    "investigation.cancel",
    "investigation.observe",
    "investigation.compensate",
)
