"""Skill 注册表 —— name -> version -> Skill 类，保留历史版本。

保留历史版本不是为了好看：skill 升级时旧 Plan 可能还在跑，
按名取默认拿最高版本，按名 + 版本取则拿到当年那一个，行为可复现。
"""

from __future__ import annotations

import logging

from maos.skills.contract import Skill

log = logging.getLogger("maos.skills")

# name -> {version -> Skill 子类}
SKILL_REGISTRY: dict[str, dict[str, type[Skill]]] = {}

# builtin 动态发现只尝试一次。要挡的不是 import 开销（import 有缓存，重复调用
# 近乎免费），是**失败**的那次：builtin 里任一模块 import 抛了，没有这个标志
# 就会在此后每一次未命中时重演一遍，并反复吞掉同一个异常。
_discovered = False


def _semver_key(version: str) -> tuple[int, ...]:
    """"1.10.0" > "1.9.0" —— 按段取数字比，非数字段记 0，不做严格 semver 校验。"""
    out = []
    for chunk in version.split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)


def register_skill(cls: type[Skill]) -> type[Skill]:
    """类装饰器：按 ``cls.contract.name`` / ``.version`` 入注册表。

    模块被 import 即注册 —— 这就是 builtin/ 动态发现能生效的原因（C-1）。
    """
    contract = cls.contract
    SKILL_REGISTRY.setdefault(contract.name, {})[contract.version] = cls
    return cls


def _discover_builtin() -> None:
    """import builtin 包触发注册（C-1）—— 无论成败都只走一次。

    刻意用 ``import`` 而不是 ``builtin.discover()``：import 有缓存，包已装载时
    是空操作；``discover()`` 每次都重扫目录，会把 test_registry_autodiscovery
    里「投放后、discover 前不应已注册」那条断言打红。别改成 discover()。
    """
    global _discovered
    _discovered = True                    # 先置位：下面抛了也不再重试
    try:
        import maos.skills.builtin        # noqa: F401 —— import 即注册
    except Exception:                     # noqa: BLE001
        # 不改抛：get() 的契约是「取不到返回 None」，invoker 靠它兜底成 failed。
        # 但也不能静默 —— 装载失败和「这个 skill 本来就没实现」是两回事。
        log.warning("builtin skill 动态发现失败，registry 只剩显式注册的条目", exc_info=True)


def _lookup(name: str, version: str | None) -> type[Skill] | None:
    versions = SKILL_REGISTRY.get(name)
    if not versions:
        return None
    if version is not None:
        return versions.get(version)
    return versions[max(versions, key=_semver_key)]


def get(name: str, version: str | None = None) -> type[Skill] | None:
    """取 skill 类。version 缺省 = 最高版本；取不到返回 None（由 invoker 兜底成 failed）。

    未命中时先触发一次 builtin 动态发现再重查。触发点落在这里、而不是某个 Agent
    的 run() 里：不经过 CodingAgent 的调用方（测试、CLI、别的轨）直接 get 也要
    拿到真类，否则拿到的 None 是「没触发发现」而不是「真没实现」。
    ``names()`` / ``versions()`` 不带这层兜底，它们只报当前已注册的。
    """
    cls = _lookup(name, version)
    if cls is not None or _discovered:
        return cls
    _discover_builtin()
    return _lookup(name, version)


def versions(name: str) -> list[str]:
    """按版本升序列出某个 skill 的全部已注册版本。"""
    return sorted(SKILL_REGISTRY.get(name, {}), key=_semver_key)


def names() -> list[str]:
    return sorted(SKILL_REGISTRY)
