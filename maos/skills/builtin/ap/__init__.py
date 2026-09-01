"""应付账款域的内置 Skill（六个）。

`builtin/__init__.py::discover()` 用 `pkgutil.iter_modules` 扫上一层，会把本包当成
一个模块 import 进去（`iter_modules` 同时枚举模块与子包）—— 于是本文件被执行，
下面几行 import 触发 `@register_skill`，它们就进注册表了。
**投放即注册这条口径没有被破坏**：往 `builtin/` 放的是这个包，不是一堆散文件，
而 `builtin/__init__.py` 一个字都不用改（冻结契约 C-1）。

这里写显式清单（与 `builtin/__init__.py` 的「不维护清单」相反）：本包是单轨独占的，
不存在多轨同改一处的合并冲突；而再套一层 pkgutil 只会让「哪些是 skill、哪些是
共用件」变成靠下划线约定，反而更容易漏。

`_common.py` 不在清单里 —— 它是共用件不是 skill，由各 skill 模块自己 import。
"""

from __future__ import annotations

from . import compensate  # noqa: F401 —— ap.compensate（失败路径的域内补偿）
from . import execute  # noqa: F401 —— ap.execute（发付款指令，写不出 settled）
from . import intake  # noqa: F401 —— ap.intake（收票建案）
from . import match  # noqa: F401 —— ap.match（三单匹配）
from . import observe  # noqa: F401 —— ap.observe（唯一写得进 settled 的 actor）
from . import plan_payment  # noqa: F401 —— ap.plan-payment（付款计划，供人审批）

#: 本域 skill 的名字清单，测试与场景按它做存在性断言，不在各处抄字面量。
#: 顺序即流程顺序，读的人不必去翻 DAG 才知道谁在谁前面。
AP_SKILLS = (
    "ap.intake",
    "ap.match",
    "ap.plan-payment",
    "ap.execute",
    "ap.observe",
    "ap.compensate",
)
