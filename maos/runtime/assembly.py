"""能力装配 —— 把 Identity 里的**名字**解析成真能调的 ToolPort，并且只收窄、不放宽。

``AgentIdentity`` 早就声明了 ``allowed_skills`` / ``allowed_tools``（**授权**），
``BaseAgent.check_tool`` 也真的在拦（**闸**）。缺的是中间那一层：名字到实现的解析。
缺了它，每个 Agent 只能各自 import 各自的 ToolPort —— 于是 ``git-mcp`` 这个名字能在
``coding.py`` 的白名单里躺很久，而全仓没有任何 ToolPort 叫这个名字（见
``maos/tools/mcp/git_tool.py`` 文件头）。同样的洞现在还剩两处：``coding`` / ``testing``
都授权了一个叫 ``sandbox`` 的东西，而实际存在的是 ``sandbox.git_apply`` 与
``sandbox.pytest_run``。本层不修那两处漂移，但**必须让它显形**：落进
``AssemblyReport.authorized_without_impl``，不 KeyError、也不静默跳过。

## 唯一一条不许自选的口径：装配集必须是授权集的子集

装配只回答「用什么调」，**不回答「准不准」** —— 后者永远只由
``identity.allowed_tools`` 说了算。所以注入进来的工具名一旦越出白名单，装配
**当场失败**，而不是把两个集合合并起来。

理由是安全性而不是洁癖：如果装配能往白名单里**加**名字，``check_tool`` 就从一道闸
退化成一句注释 —— 任何人往 profile 里写一行就能给自己提权，而 ``PermissionDenied``
在 ``agents/base.py`` 里的原话是「不要 catch，这是安全事件」。**闸只能收窄，不能放宽。**

## 三个参数为什么都是注入的

``profile``（职责能力档案）与 ``mcp_ports``（按角色分配的 MCP 工具）分别是别的轨的
产出，本模块**不 import 它们**，只按鸭子类型收：缺省 ``None`` 时退回本模块内
``collect_local_ports()`` 的最小实现。import 一个还不存在的模块会让本层从第一条测试
起就是红的，而装配层本身与「档案由谁产」无关。

## 装配不接管调用

``assemble()`` 只解析对象。取到 ToolPort 之后**必须仍经
``maos/tools/port.py::invoke_tool``** 调用 —— 直接调 ``port.entry`` 就没有
``ToolInvoked`` 审计行，出事之后查不到是谁、什么参数、跑了多久（原话在 port.py 文件头）。
"""

from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from maos.tools.port import ToolPort


class AssemblyError(RuntimeError):
    """装配失败。注入的能力越出了 Identity 的授权集 —— 提权企图，不是配置问题。"""


class ToolNotAssembled(LookupError):
    """名字有授权，但没有可用的实现（或这个 Agent 还没装配过）。

    刻意不是 ``PermissionDenied``：那是安全事件，而这里是**实现缺位**。两者混成一个
    异常会让「有人在越权」和「有个 ToolPort 还没写」在日志里长得一模一样。
    """


# ---------------------------------------------------------------- 本地工具目录
def collect_local_ports(package: str = "maos.tools") -> dict[str, ToolPort]:
    """扫出 ``maos/tools/**`` 里全部模块级 ToolPort 实例，返回 name -> port。

    动态扫描而不是维护一张显式清单：显式清单意味着每加一个 ToolPort 都要改同一个
    文件，多轨并行时合并必冲突 —— 与 ``maos/agents/__init__.py`` 里那段「不要改本
    文件」的理由是同一条。

    重名直接抛：ToolPort 的 ``name`` 就是白名单里的那个名字，两个实现抢同一个名字时
    「这次调的是哪一个」没有唯一答案，静默取其一等于把这个问题藏起来。
    """
    pkg = importlib.import_module(package)
    found: dict[str, ToolPort] = {}
    origin: dict[str, str] = {}
    for info in pkgutil.walk_packages(pkg.__path__, pkg.__name__ + "."):
        mod = importlib.import_module(info.name)
        for value in vars(mod).values():
            if not isinstance(value, ToolPort):
                continue
            prev = origin.get(value.name)
            if prev is not None and found[value.name] is not value:
                raise AssemblyError(
                    f"ToolPort 重名: {value.name} 同时来自 {prev} 与 {info.name}")
            found[value.name] = value
            origin[value.name] = info.name
    return found


def port_modules(package: str = "maos.tools") -> dict[str, str]:
    """name -> 定义它的模块名（点号形式，天然不含任何绝对路径）。

    给证据脚本判「本地背书还是 MCP 背书」用：模块名里带 ``.mcp.`` 的就是 MCP 背书。
    """
    pkg = importlib.import_module(package)
    out: dict[str, str] = {}
    for info in pkgutil.walk_packages(pkg.__path__, pkg.__name__ + "."):
        mod = importlib.import_module(info.name)
        for value in vars(mod).values():
            if isinstance(value, ToolPort):
                out.setdefault(value.name, info.name)
    return out


def _as_port_map(ports: Mapping[str, ToolPort] | Iterable[ToolPort] | None,
                 what: str) -> dict[str, ToolPort]:
    """收 dict 也收 ToolPort 序列 —— 别轨的 ``ports_for(role)`` 返回哪一种都接得住。"""
    if ports is None:
        return {}
    if isinstance(ports, Mapping):
        out: dict[str, ToolPort] = {}
        for name, port in ports.items():
            if not isinstance(port, ToolPort):
                raise AssemblyError(
                    f"{what}[{name}] 不是 ToolPort: {type(port).__name__}")
            out[str(name)] = port
        return out
    out = {}
    for port in ports:
        if not isinstance(port, ToolPort):
            raise AssemblyError(f"{what} 里混入了非 ToolPort: {type(port).__name__}")
        out[port.name] = port
    return out


def _profile_names(profile: Any, *candidates: str) -> frozenset[str] | None:
    """按鸭子类型从 profile 上取一个名字集合；一个候选字段都没有就返回 None（= 不表态）。

    不 import 档案模块、也不假定它的类名：本层只关心「它说了哪些名字」。
    """
    if profile is None:
        return None
    for attr in candidates:
        value = getattr(profile, attr, None)
        if value is None:
            continue
        if isinstance(value, Mapping):
            return frozenset(str(k) for k in value)
        if isinstance(value, (str, bytes)):
            raise AssemblyError(f"profile.{attr} 应当是名字集合，不是字符串")
        return frozenset(str(v) for v in value)
    return None


# -------------------------------------------------------------------- 装配报告
@dataclass(frozen=True)
class AssemblyReport:
    """一次装配的结果。三件事必须答得出：解析成功的、有授权无实现的、有实现无授权的。"""

    agent_id: str
    role: str
    ports: dict[str, ToolPort]                          # 解析成功：name -> port
    sources: dict[str, str]                             # name -> "local" | "mcp"
    authorized_without_impl: tuple[str, ...]            # 白名单里有，全仓没实现
    implemented_without_authorization: tuple[str, ...]  # 有实现，本角色没授权
    withheld_by_profile: tuple[str, ...]                # 有授权有实现，但 profile 没要
    skills: dict[str, str | None]                       # 授权 skill -> 最高版本（None = 无实现）
    catalog: frozenset[str]                             # 见过的全部工具名（目录 + 注入）

    @property
    def resolved_tools(self) -> tuple[str, ...]:
        return tuple(sorted(self.ports))

    @property
    def skills_without_impl(self) -> tuple[str, ...]:
        return tuple(sorted(n for n, v in self.skills.items() if v is None))

    def get(self, tool: str) -> ToolPort | None:
        return self.ports.get(tool)


def build_report(identity: Any, *, profile: Any = None,
                 mcp_ports: Mapping[str, ToolPort] | Iterable[ToolPort] | None = None,
                 ports: Mapping[str, ToolPort] | Iterable[ToolPort] | None = None,
                 ) -> AssemblyReport:
    """按一份 Identity 算装配结果，不改动任何对象。``assemble()`` 与证据脚本共用这份口径。

    ``ports`` 是**解析来源（目录）**，不是装配请求：它天然装着全仓每一个 ToolPort，
    而每个角色只授权其中一两个，拿目录去撞白名单会让每一次装配都失败。目录里多出来
    的名字落进 ``implemented_without_authorization``。

    ``mcp_ports`` 与 ``profile`` 才是**装配请求**：它们说「给这个角色配上这些」，
    所以越出 ``identity.allowed_tools`` 一律 fail fast。
    """
    allowed_tools = frozenset(getattr(identity, "allowed_tools", frozenset()))
    allowed_skills = frozenset(getattr(identity, "allowed_skills", frozenset()))
    agent_id = str(getattr(identity, "agent_id", "?"))
    role = str(getattr(identity, "role", "?"))

    catalog = _as_port_map(collect_local_ports() if ports is None else ports, "ports")
    injected = _as_port_map(mcp_ports, "mcp_ports")

    # ---- fail fast ①：注入的 MCP 工具越出白名单 = 提权
    escalated = sorted(set(injected) - allowed_tools)
    if escalated:
        raise AssemblyError(
            f"{agent_id} 装配失败：注入的工具 {escalated} 不在 identity.allowed_tools"
            f"（白名单: {sorted(allowed_tools)}）—— 装配只解析、只收窄，不许放宽白名单")

    # ---- fail fast ②：profile 点名的能力越出白名单 = 同一件事
    wanted_tools = _profile_names(profile, "allowed_tools", "tools", "tool_names")
    if wanted_tools is not None:
        over = sorted(wanted_tools - allowed_tools)
        if over:
            raise AssemblyError(
                f"{agent_id} 装配失败：profile 点名的工具 {over} 不在 "
                f"identity.allowed_tools（白名单: {sorted(allowed_tools)}）"
                f" —— 档案只能收窄授权，不能扩张")
    wanted_skills = _profile_names(profile, "allowed_skills", "skills", "skill_names")
    if wanted_skills is not None:
        over = sorted(wanted_skills - allowed_skills)
        if over:
            raise AssemblyError(
                f"{agent_id} 装配失败：profile 点名的 skill {over} 不在 "
                f"identity.allowed_skills（白名单: {sorted(allowed_skills)}）")

    requested = allowed_tools if wanted_tools is None else (allowed_tools & wanted_tools)

    resolved: dict[str, ToolPort] = {}
    sources: dict[str, str] = {}
    missing: list[str] = []
    for name in sorted(requested):
        port = injected.get(name)
        if port is not None:                   # 显式注入优先于目录扫描
            resolved[name], sources[name] = port, "mcp"
            continue
        port = catalog.get(name)
        if port is not None:
            resolved[name], sources[name] = port, "local"
            continue
        missing.append(name)                   # 有授权、没实现 —— 记下来，不抛也不吞

    seen = frozenset(catalog) | frozenset(injected)
    skills = {name: _skill_version(name) for name in sorted(allowed_skills)}
    return AssemblyReport(
        agent_id=agent_id, role=role, ports=resolved, sources=sources,
        authorized_without_impl=tuple(missing),
        implemented_without_authorization=tuple(sorted(seen - allowed_tools)),
        withheld_by_profile=tuple(sorted(allowed_tools - requested)),
        skills=skills, catalog=seen,
    )


def _skill_version(name: str) -> str | None:
    """skill 名 -> 注册表里的最高版本；没实现返回 None。

    取不到实现不抛 —— 那是一条要被记下来的事实，不是错误（口径同 invoker 的
    ``skill_not_found``：并行开发期被调方还没合并进来是常态）。
    """
    from maos.skills import registry           # 延迟 import：不绑死 skill 层的装载时机
    cls = registry.get(name)
    return None if cls is None else str(cls.contract.version)


def assemble(agent: Any, *, profile: Any = None,
             mcp_ports: Mapping[str, ToolPort] | Iterable[ToolPort] | None = None,
             ports: Mapping[str, ToolPort] | Iterable[ToolPort] | None = None,
             ) -> AssemblyReport:
    """给一个 Agent **实例**装配能力，把报告挂到 ``agent.assembly`` 上并返回它。

    挂实例属性而不是往 Identity 里加字段：``AgentIdentity`` 是 ``frozen=True``，而且
    加字段要动 23 个 identity 文件 —— 装配是运行时的事，不是声明的事。

    不调本函数的 Agent，``assembly`` 一直是类级缺省的 ``None``，行为与装配前逐字节一致。
    """
    identity = getattr(agent, "identity", None)
    if identity is None:
        raise AssemblyError(f"{agent!r} 没有 identity，不是可装配的对象")
    if isinstance(agent, type):
        raise AssemblyError(
            f"assemble() 收 Agent 实例，不收类 {agent.__name__} —— "
            "装到类上会让同一个角色的所有实例共用一份装配结果")
    report = build_report(identity, profile=profile, mcp_ports=mcp_ports, ports=ports)
    agent.assembly = report
    return report
