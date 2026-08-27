"""内置 Skill 包 —— **投放即注册，不要改本文件**（冻结契约 C-1）。

新增 skill 只做一件事：往本目录放一个模块，类上打 ``@register_skill``。
本文件不维护任何显式 import 清单 —— A/B/D 三条轨都要往这里加文件，
显式清单等于三方改同一处，合并必冲突。
"""

from __future__ import annotations

import importlib
import pkgutil


def discover() -> list[str]:
    """扫描本包并 import 全部非下划线开头模块，返回模块名（升序）。

    幂等：重复调用只是重新 import，已注册的 skill 会被同版本覆盖，结果不变。
    先 invalidate_caches() 是必需的 —— 本包首次 import 之后才落盘的新模块，
    不清缓存扫不出来（测试正是这个场景）。
    """
    importlib.invalidate_caches()
    found = []
    for mod in pkgutil.iter_modules(__path__):
        if mod.name.startswith("_"):
            continue
        importlib.import_module(f"{__name__}.{mod.name}")
        found.append(mod.name)
    return sorted(found)


discover()
