"""保险理赔域的内置 Skill（六个）。

`builtin/__init__.py::discover()` 用 `pkgutil.iter_modules` 扫上一层，会把本包当成
一个模块 import 进去（`iter_modules` 同时枚举模块与子包）—— 于是本文件被执行，
下面几行 import 触发 `@register_skill`，它们就进注册表了。
**投放即注册这条口径没有被破坏**：往 `builtin/` 放的是这个包，不是一堆散文件，
而 `builtin/__init__.py` 一个字都不用改（冻结契约 C-1）。

这里必须写显式清单（与 `builtin/__init__.py` 的「不维护清单」相反）：本包是单轨
独占的，不存在多轨同改一处的合并冲突；而再套一层 pkgutil 只会让「哪些是 skill、
哪些是共用件」变成靠下划线约定，反而更容易漏。

`_common.py` 不在清单里 —— 它是共用件不是 skill，由各 skill 模块自己 import。
"""

from __future__ import annotations

from . import adjudicate  # noqa: F401 —— import 即注册（claim.adjudicate）
from . import compensate  # noqa: F401 —— claim.compensate（失败路径的域内补偿）
from . import intake  # noqa: F401 —— claim.intake
from . import observe  # noqa: F401 —— claim.observe（**全系统唯一写得进 paid**）
from . import pay  # noqa: F401 —— claim.pay
from . import settle  # noqa: F401 —— claim.settle

#: 本域 skill 的名字清单，测试与场景按它做存在性断言，不在各处抄字面量。
#: 顺序即流程顺序，别按字母排 —— 读这份清单的人第一眼要看到的是链路。
CLAIM_SKILLS = (
    "claim.intake",
    "claim.adjudicate",
    "claim.settle",
    "claim.pay",
    "claim.observe",
    "claim.compensate",
)
