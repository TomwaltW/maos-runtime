"""Skill 注册表 —— name -> version -> Skill 类，保留历史版本。

保留历史版本不是为了好看：skill 升级时旧 Plan 可能还在跑，
按名取默认拿最高版本，按名 + 版本取则拿到当年那一个，行为可复现。
"""

from __future__ import annotations

from maos.skills.contract import Skill

# name -> {version -> Skill 子类}
SKILL_REGISTRY: dict[str, dict[str, type[Skill]]] = {}


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


def get(name: str, version: str | None = None) -> type[Skill] | None:
    """取 skill 类。version 缺省 = 最高版本；取不到返回 None（由 invoker 兜底成 failed）。"""
    versions = SKILL_REGISTRY.get(name)
    if not versions:
        return None
    if version is not None:
        return versions.get(version)
    return versions[max(versions, key=_semver_key)]


def versions(name: str) -> list[str]:
    """按版本升序列出某个 skill 的全部已注册版本。"""
    return sorted(SKILL_REGISTRY.get(name, {}), key=_semver_key)


def names() -> list[str]:
    return sorted(SKILL_REGISTRY)
