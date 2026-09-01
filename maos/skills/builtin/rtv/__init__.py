"""采购退货退款域（RTV，Return to Vendor）的内置 Skill（六个）。

`builtin/__init__.py::discover()` 用 `pkgutil.iter_modules` 扫上一层，会把本包当成
一个模块 import 进去（`iter_modules` 同时枚举模块与子包）—— 于是本文件被执行，
下面几行 import 触发 `@register_skill`，它们就进注册表了。
**投放即注册这条口径没有被破坏**：往 `builtin/` 放的是这个包，不是一堆散文件，
而 `builtin/__init__.py` 一个字都不用改（冻结契约 C-1）。口径同 `builtin/ap/__init__.py`。

`_common.py` 不在清单里 —— 它是共用件不是 skill，由各 skill 模块自己 import。

## 本域相对 `ap` 域的增量：两个权威终态

`ap` 域只有一个外部权威（银行回单 -> `settled`）。RTV 有**两个**：

    供应商开出贷项通知单  -> credited     （供应商认了这笔退货）
    AP 调整凭单核销/到账  -> settled      （钱真的回来了）

两件事由两个不同的外部系统说了算，MAOS 一个都写不了，只能观察 —— 所以这六个
skill 里只有 `rtv.observe` 碰得到这两个状态（契约 C-R3）。
"""

from __future__ import annotations

from . import compensate  # noqa: F401 —— rtv.compensate（失败路径的域内补偿）
from . import dispose  # noqa: F401 —— rtv.dispose（裁定 credit/exchange/replacement）
from . import intake  # noqa: F401 —— rtv.intake（定位源 PO/GR 并建案）
from . import observe  # noqa: F401 —— rtv.observe（唯一写得进 credited/settled 的 actor）
from . import reconcile  # noqa: F401 —— rtv.reconcile（三方对账，**不推进状态**）
from . import ship  # noqa: F401 —— rtv.ship（发运并取承运商回执）

#: 本域 skill 的名字清单，测试与场景按它做存在性断言，不在各处抄字面量。
#: 顺序即流程顺序（SOP 五步），读的人不必去翻 DAG 才知道谁在谁前面。
#: 名字与版本是注册表主键，逐字对齐契约 C-R4。
RTV_SKILLS = (
    "rtv.intake",
    "rtv.dispose",
    "rtv.ship",
    "rtv.reconcile",
    "rtv.observe",
    "rtv.compensate",
)
