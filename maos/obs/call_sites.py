"""``model_usage.call_site`` 的登记表 —— **穷举**，新增调用点必须先登记（T48）。

``core/store.py`` 里 ``call_site`` 是一列自由字符串（``call_site TEXT NOT NULL``），
表结构冻结、不许加约束。于是「谁在烧 token」这件事全靠调用点自觉：新增一个模型
调用点忘了 ``record_model_usage``，或者记了但写了个新字符串，``cost_view`` 就
**静默偏低** —— 屏幕上一个数都不会变红，只是那笔钱不见了。T32 那次「六处补
``store=``」就是这类漏接的实证（``flows/scenario_7.py`` 的注释里写着该场景原先
``cost.calls`` 恒为 0）。

本模块不改表、不改任何调用点，只把这三个值**登记成穷举集合**，配一条测试
（``tests/test_cost_metrics.py`` 的「call_site 登记表」一节）扫出未登记的值就报红。
判据是登记，不是命名规范 —— 登记表拦的是「悄悄多了一个调用点」，不是「名字起得
不好看」。

**为什么是字面量，不是 import 过来的常量**（这一条是本模块最容易被"顺手优化"掉的
地方）：三个值的源头分别在 ``maos/agents/base.py``、``maos/skills/builtin/
req_normalize.py``、``maos/skills/builtin/code_repo_patch.py``。而 ``maos/obs``
只许 import ``maos.core.store``，不 import 任何业务域与上层模块（规矩立在
``obs/trace.py`` 的模块 docstring 末行）。从 agents / skills 里 import 常量会当场
破掉那条边界，把可观测层变成上层模块的下游。

抄字面量的代价是**可能漂**：源头改了字符串而这里没跟着改。这个代价由
``test_registered_call_sites_are_byte_for_byte_the_ones_in_the_source`` 兜住 ——
它在测试里（测试可以 import 任何东西）把三处源头的常量与这里逐字节对齐，
漂一个字符就红。**这条测试不许删**：删了它，登记表就从"穷举"退化成"一份注释"。
"""

from __future__ import annotations

from collections.abc import Iterable

#: ``BaseAgent.ask()``（``maos/agents/base.py`` 的 ``CALL_SITE_ASK``）。
CALL_SITE_AGENT_ASK = "maos/agents/base.py::BaseAgent.ask"

#: 需求归一化 skill（``maos/skills/builtin/req_normalize.py`` 的 ``CALL_SITE``）。
CALL_SITE_REQ_NORMALIZE = "maos/skills/builtin/req_normalize.py::ReqNormalizeSkill.run"

#: 代码补丁 skill（``maos/skills/builtin/code_repo_patch.py`` 的 ``CALL_SITE``）。
CALL_SITE_CODE_REPO_PATCH = (
    "maos/skills/builtin/code_repo_patch.py::CodeRepoPatchSkill.run"
)

#: 已登记的全部 ``call_site``。**穷举**：库里出现集合外的值即视为漏登记。
REGISTERED_CALL_SITES: frozenset[str] = frozenset({
    CALL_SITE_AGENT_ASK,
    CALL_SITE_REQ_NORMALIZE,
    CALL_SITE_CODE_REPO_PATCH,
})

#: 报错正文里统一带上这一句 —— 红灯要给出下一步动作，不然它只是一次打扰。
REGISTER_HINT = (
    "新增调用点请登记到 maos/obs/call_sites.py 的 REGISTERED_CALL_SITES"
    "（并在那里说明它是谁、在哪一步烧的 token）"
)


def unregistered(values: Iterable[str]) -> list[str]:
    """挑出未登记的 ``call_site``，排序返回（空列表 = 全部已登记）。

    排序是为了让报错正文可复现：``set`` 的迭代顺序随进程变，同一个漏接在两次跑里
    会给出两句不同的话，读的人以为是两个问题。
    """
    return sorted({str(v) for v in values} - REGISTERED_CALL_SITES)


def unregistered_in_store(store) -> list[str]:
    """扫一个库里 ``model_usage`` 的全部 ``call_site``，返回其中未登记的。

    只走 ``Store`` 的公开读接口（``list_model_usage``），不碰 SQL、不碰表结构 ——
    这一层是守卫，不是第二个存储层。
    """
    return unregistered(row.get("call_site") or "" for row in store.list_model_usage())
