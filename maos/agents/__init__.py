"""Bounded agent identities and implementations.

注册口径（冻结契约 C-2）：import 本包时自动扫描并 import 包内全部非下划线开头模块，
带 ``@register`` 的 Agent 类随之进 ``AGENT_POOL``（机制见 ``base.py`` 末尾）。

所以新增 Agent 只做一件事：往本目录投一个文件。**不要改本文件**——
显式 import 清单意味着多条并行轨都要改同一处，合并必冲突；
而在各 scenario 顶部 import 注册则会让每个场景各存一份清单，必漂移。
"""

from __future__ import annotations

import importlib
import pkgutil

from maos.agents.base import AGENT_POOL  # noqa: F401 —— 对外唯一的注册表出口

for _mod in pkgutil.iter_modules(__path__):
    if not _mod.name.startswith("_"):
        importlib.import_module(f"{__name__}.{_mod.name}")
