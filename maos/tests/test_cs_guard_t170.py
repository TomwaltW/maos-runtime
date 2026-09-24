"""T170 · 外部渠道零授权的静态守卫（契约 §2.3，红线 R1），全仓生效。

扫描范围：``maos/domain/cs/**`` 与 ``maos/skills/builtin/cs/**``（后者 T169 才建，不在时跳过
该目录；但 ``maos/domain/cs`` 必须扫到 ``types.py`` —— 防空转：路径写错时扫到零个文件、
守卫恒绿，而且没有任何症状）。

判据（契约 §2.3 逐条，外加 R1「不按名字调、不借别人的 identity」落成机器判据的部分，
超出契约原文的几处见 DECISIONS task-t170）：

* **禁 import 前缀** —— ``ast.walk`` 全树（函数体内的 import 也算），相对 import 按包路径
  解析成绝对名再判；``from A import B`` 按 ``A.B`` 判（``from maos.domain.refund import
  projection`` 是 projection、放行；``from maos.domain.refund import objects`` 拦）。
  另几种拿到同一个模块的写法按同一套规则判：
  - ``from <祖先包> import *``（``maos`` / ``maos.domain`` / ``maos.skills.builtin`` …… 的
    star import 会把禁区子模块一并绑进来）；
  - 属性链：``from maos.skills import builtin`` 之后的 ``builtin.refund.payment_execute``、
    ``import maos.kb`` 之后的 ``maos.runtime.gate``，按还原出的点分名判；
  - **按真实出处再判一次**（复核 L3-B）：名字放行、拿到的对象却来自禁区（允许的模块把禁区
    模块或其函数 / 类再导出）也判。``from A import b``、属性链、``from A import *`` 绑出的
    每个公开名，都在测试进程里实际解析：是模块按 ``__name__``，是函数 / 类按
    ``__module__.__qualname__``，途经的模块换成它的真名（``logging.sys.modules`` 就是
    ``sys.modules``）。解析不了的不判 —— 运行时同样拿不到。
* **动态加载失败即关**（复核 L3-A）：扫描范围里没有正当的动态加载需求。能按名字 / 路径 /
  源码串加载或执行代码的标准库入口整模块禁 import（importlib、runpy、pkgutil、builtins、
  pickle、subprocess ……），``sys.modules`` 等几个口子和起进程的 ``os`` 函数单独禁；加载入口
  的名字（``import_module`` / ``__import__`` / ``find_spec`` / ``SourceFileLoader`` ……）不论
  从哪拿到（别名、参数、getattr）出现即判；``exec`` / ``eval`` / ``compile`` 这三个名字出现
  即判；通向内建、别的模块命名空间、调用栈的内省名（``__builtins__`` / ``__globals__`` /
  ``__subclasses__`` / ``f_globals`` ……）出现即判；对导入的模块 / 对象做反射
  （``getattr`` / ``vars`` / ``setattr`` / ``delattr`` / ``.__dict__``）即判；字符串常量是禁区
  模块名（点分、``模块:属性``、``maos/…/x.py`` 路径三种写法）即判。
* **允许清单** —— 契约列出来免得守卫写过头的那几项，逐条断言不误报。
* **禁字符串常量** —— 任何位置的 ``ast.Constant``（str，以及 bytes 按 UTF-8 解开后）与之
  **相等**即判；包着禁工具的 skill 名（``rtv.ship`` 等）同判，并由注册表反推钉住（新 skill
  包了禁工具而这里没列，当场红）。
* **禁调用名** —— 调用（属性调用、裸名调用）与**引用**（``fn = gate.decide``、
  ``partial(gate.decide)``、``methodcaller("decide")``、``getattr(x, "decide")``）都判。
* **不借别人的 identity** —— 扫描范围里不许出现 ``AGENT_POOL``（名字、属性、import 名、
  字符串），不许 import ``maos.capability``（``identities()`` 按角色给出全部身份）；
  ``allowed_skills`` / ``allowed_tools`` 不论出现在哪（``AgentIdentity`` 的位置参数、任何调用的
  关键字参数、类属性 / 变量 / 属性赋值）都要能**静态求值**成字面量集合，求不出来即判
  （失败即关），求出来的 skill 只许 ``cs.*``、工具只许为空；就地改写（``|=``）和等值字符串
  （``setattr(x, "allowed_skills", …)``、``replace(x, **{"allowed_skills": …})``）即判。
* **文件层** —— 扫描目录下出现符号链接（rglob 不跟进、import 会跟进）或没有源码的可导入
  文件（包目录里、不在 ``__pycache__`` 下的 ``.pyc``、扩展模块、``.pth``）即判。

扫描逻辑是可注入根目录的函数 :func:`scan_cs_guard_t170`；反向验证全部在 tmp 目录里
造违规文件喂给它，不落仓库。守卫仍只认字面量：拼接 / f-string / 运行时算出来的字符串、
运行时对象图上的可达性（实例属性、闭包里的对象）判不到（BACKLOG task-t170）。
"""

from __future__ import annotations

import ast
import functools
import importlib
import importlib.machinery
import inspect
import os
import pathlib
import re
import textwrap
import types
from typing import Any

import pytest

ROOT_T170 = pathlib.Path(__file__).resolve().parents[2]

#: 扫描范围（相对仓库根）。
SCAN_DIRS_T170: tuple[tuple[str, ...], ...] = (
    ("maos", "domain", "cs"),
    ("maos", "skills", "builtin", "cs"),
)

#: 防空转的锚：这个文件必须在扫描集里。
ANCHOR_T170 = ("maos", "domain", "cs", "types.py")

#: 契约 §2.3 的禁 import 前缀（按点分段匹配，不按字符串前缀：``maos.toolsx`` 不是 ``maos.tools``）。
CONTRACT_FORBIDDEN_PREFIXES_T170: tuple[str, ...] = (
    "maos.runtime", "maos.core.control_plane", "maos.flows", "hiclaw",
    "maos.ingress.router", "maos.ingress.outcome_commands", "maos.ingress.chat",
    "maos.nlu", "maos.roundtable", "maos.tools",
)
#: 本轨补的（R1「不借别人的 identity」）：``maos.capability.profiles.identities()`` 按角色给出
#: 全部身份，拿到就能以别的角色调 skill。
EXTRA_FORBIDDEN_PREFIXES_T170: tuple[str, ...] = ("maos.capability",)
FORBIDDEN_PREFIXES_T170 = CONTRACT_FORBIDDEN_PREFIXES_T170 + EXTRA_FORBIDDEN_PREFIXES_T170

#: 失败即关（复核 L3-A）：能按名字 / 路径 / 源码串加载或执行代码的标准库入口，整模块禁。
LOADER_MODULES_T170: tuple[str, ...] = (
    "importlib", "runpy", "pkgutil", "imp", "zipimport", "builtins", "code", "codeop", "pydoc",
    "pickle", "_pickle", "shelve", "marshal", "gc", "ctypes", "subprocess", "multiprocessing",
)
#: sys / os 常用、不整模块禁，只禁这几个口子：模块表与导入钩子、调用栈、起进程。
SYS_OS_HOLES_T170: tuple[str, ...] = (
    "sys.modules", "sys.meta_path", "sys.path_hooks", "sys.path_importer_cache", "sys._getframe",
) + tuple(f"os.{n}" for n in (
    "system", "popen", "fork", "forkpty", "posix_spawn", "posix_spawnp",
    "execl", "execle", "execlp", "execlpe", "execv", "execve", "execvp", "execvpe",
    "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe"))
DYNAMIC_PREFIXES_T170 = LOADER_MODULES_T170 + SYS_OS_HOLES_T170

#: maos.domain 下允许的一级名（共享底座与本域）；另有 refund.projection 一处。
DOMAIN_ALLOWED_T170 = frozenset({"cs", "_dbport", "_schema_util"})

#: 契约 §2.3 的禁字符串常量。
CONTRACT_FORBIDDEN_CONSTANTS_T170 = frozenset({
    "payment.execute", "payment.observe", "refund.compensate", "refund.compensation_close",
    "gateway.refund", "gateway.query", "order.query", "claim.pay", "claim.compensate",
    "ap.execute", "ap.compensate", "rtv.compensate", "investigation.compensate",
    "bank.pay", "payer.submit", "supplier.rma_submit", "carrier.ship", "clearing.cancel",
})
#: 包着上面那些工具、名字却不在表里的 skill（注册表反推，见
#: test_skill_wrappers_of_forbidden_tools_are_forbidden_t170）。
SKILL_WRAPPERS_T170 = frozenset({"rtv.ship", "investigation.cancel", "refund.snapshot_check"})
FORBIDDEN_CONSTANTS_T170 = CONTRACT_FORBIDDEN_CONSTANTS_T170 | SKILL_WRAPPERS_T170

FORBIDDEN_CALLS_T170 = frozenset({
    "decide", "human_decision", "record_approval", "run_payload", "handle_execute",
    "handle_gate_decision", "_record_gate_approval", "invoke_tool",
})

#: 别的角色的身份从这里取（role -> Agent 类，类上挂着 identity）。
IDENTITY_REGISTRIES_T170 = frozenset({"AGENT_POOL"})
#: identity 上决定授权的两个字段。
IDENTITY_FIELDS_T170 = frozenset({"allowed_skills", "allowed_tools"})
#: 前台自己的 skill 前缀：allowed_skills 求出来的值只许是它。
CS_SKILL_PREFIX_T170 = "cs."

#: 加载入口的名字（复核 L3-A）：不论从哪拿到 —— 别名、参数、getattr —— 出现即判。
LOADER_NAMES_T170 = frozenset({
    "__import__", "import_module", "find_spec", "find_loader", "resolve_name", "run_module",
    "run_path", "spec_from_file_location", "spec_from_loader", "module_from_spec",
    "load_module", "exec_module", "get_loader", "SourceFileLoader", "SourcelessFileLoader",
    "ExtensionFileLoader",
})
#: 通向内建、别的模块命名空间、加载器、调用栈的内省名（复核 L3-A / L3-B）。
INTROSPECTION_NAMES_T170 = frozenset({
    "__builtins__", "__loader__", "__spec__", "__globals__", "__subclasses__", "__closure__",
    "f_globals", "f_locals", "f_builtins",
})
#: 对导入的模块 / 对象做这几种反射即判（``getattr(sys, "modules")``、``vars(sys)``）。
REFLECTIVE_CALLS_T170 = frozenset({"getattr", "vars", "setattr", "delattr"})
#: 扫描范围里一律不许的动态执行：名字出现即判（调用、引用都算；``re.compile`` 是属性，不算）。
DYNAMIC_EXEC_T170 = frozenset({"exec", "eval", "compile"})
#: 属性写法只认 exec / eval（``builtins.exec``、参数传进来的 ``b.eval``）；``.compile`` 是 re 的日常用法。
DYNAMIC_EXEC_ATTRS_T170 = frozenset({"exec", "eval"})

#: star import 会把禁区子模块一并绑进来的祖先包（禁前缀的真前缀 + 按段放行的几个父包）。
STAR_FORBIDDEN_BASES_T170 = frozenset(
    {".".join(p.split(".")[:i]) for p in FORBIDDEN_PREFIXES_T170
     for i in range(1, p.count(".") + 1)}
    | {"maos", "maos.skills", "maos.skills.builtin", "maos.agents", "maos.domain",
       "maos.domain.refund"})

#: 没有源码也能被 import 的文件（不在 __pycache__ 下时）。
SOURCELESS_SUFFIXES_T170 = tuple(importlib.machinery.BYTECODE_SUFFIXES
                                 + importlib.machinery.EXTENSION_SUFFIXES
                                 + [".pyd", ".pth"])

#: 字符串常量里的模块名：点分、``模块:属性``、``maos/…/x`` 路径（``.py`` 先剥掉）。
_MODULE_STRING_RE_T170 = re.compile(r"^(?:maos|hiclaw)(?:[./:][A-Za-z_]\w*)+$")

#: identity 字段静态求值时认的构造器（文件里被重新绑定过就不认）。
_SET_BUILDERS_T170 = frozenset({"frozenset", "set", "tuple", "list", "sorted"})

_MISSING_T170 = object()


# ---------------------------------------------------------------------------
# 规则
# ---------------------------------------------------------------------------
def _has_prefix_t170(name: str, prefix: str) -> bool:
    return name == prefix or name.startswith(prefix + ".")


def import_violation_t170(name: str) -> str | None:
    """绝对模块名（或 ``模块.属性``）触犯哪条禁令；放行返回 None。"""
    parts = name.split(".")
    for prefix in FORBIDDEN_PREFIXES_T170:
        if _has_prefix_t170(name, prefix):
            return f"禁 import 前缀 {prefix}"
    for prefix in DYNAMIC_PREFIXES_T170:
        if _has_prefix_t170(name, prefix):
            return f"失败即关：禁动态加载 / 起进程的入口 {prefix}"
    if parts[:3] == ["maos", "skills", "builtin"] and len(parts) >= 4 and parts[3] != "cs":
        return "禁 import maos.skills.builtin.<非 cs>"
    if parts[:2] == ["maos", "agents"] and len(parts) >= 3 and parts[2] != "base":
        return "禁 import maos.agents.<除 base 外>"
    if parts[:2] == ["maos", "domain"] and len(parts) >= 3:
        head = parts[2]
        if head in DOMAIN_ALLOWED_T170:
            return None
        if head == "refund" and len(parts) >= 4 and parts[3] == "projection":
            return None
        return "禁跨域 import maos.domain.<除 cs/_dbport/_schema_util/refund.projection 外>"
    if parts[:3] == ["maos", "skills", "registry"]:
        if len(parts) == 4 and parts[3] == "register_skill":
            return None
        return "maos.skills.registry 只许 import register_skill"
    return None


@functools.lru_cache(maxsize=None)
def _resolve_t170(dotted: str) -> Any:
    """在测试进程里按点分名拿到对象：最长可 import 的模块前缀 + 逐段 getattr。拿不到返回哨兵。"""
    parts = dotted.split(".")
    for i in range(len(parts), 0, -1):
        try:
            obj = importlib.import_module(".".join(parts[:i]))
        except Exception:                                  # noqa: BLE001 —— 拿不到就不判
            continue
        for attr in parts[i:]:
            try:
                obj = getattr(obj, attr)
            except Exception:                              # noqa: BLE001
                return _MISSING_T170
        return obj
    return _MISSING_T170


def _canonical_t170(dotted: str) -> str | None:
    """途经的最长一段模块换成它的真名：``maos.skills.invoker.registry`` → ``maos.skills.registry``。"""
    parts = dotted.split(".")
    for i in range(len(parts), 0, -1):
        obj = _resolve_t170(".".join(parts[:i]))
        if isinstance(obj, types.ModuleType):
            return ".".join([obj.__name__] + parts[i:])
    return None


def _home_t170(obj: Any) -> str | None:
    """函数 / 类的出处（``__module__.__qualname__``）；别的对象没有可信的出处，返回 None。"""
    if isinstance(obj, type) or inspect.isroutine(obj):
        mod = getattr(obj, "__module__", None)
        qual = getattr(obj, "__qualname__", None) or getattr(obj, "__name__", None)
        if isinstance(mod, str) and isinstance(qual, str):
            return f"{mod}.{qual}"
    return None


def value_violation_t170(dotted: str) -> str | None:
    """名字放行、拿到的东西却来自禁区（再导出）：按真实出处再判一次（复核 L3-B）。"""
    if import_violation_t170(dotted):
        return None                                        # 名字本身已判
    for real in (_canonical_t170(dotted), _home_t170(_resolve_t170(dotted))):
        if real and real != dotted:
            why = _real_violation_t170(real)
            if why:
                return f"实为 {real} —— {why}"
    return None


def _real_violation_t170(real: str) -> str | None:
    """真实出处触犯哪条禁令。内建的函数 / 类（``__module__ == 'builtins'``，如 typing.Text 就是
    str）本来就人人可用，只有 exec / eval / compile / __import__ 这几个算动态加载。"""
    head, _, rest = real.partition(".")
    if head == "builtins" and rest:
        if rest in DYNAMIC_EXEC_T170 or rest == "__import__":
            return f"失败即关：动态执行 / 加载入口 builtins.{rest}"
        return None
    return import_violation_t170(real)


def target_violation_t170(dotted: str) -> str | None:
    return import_violation_t170(dotted) or value_violation_t170(dotted)


def _name_violation_t170(name: str) -> tuple[str, str] | None:
    """一个裸名字（标识符 / 属性名 / import 名 / 等值字符串）触犯哪类：返回（类别, 说明）。"""
    if name in FORBIDDEN_CALLS_T170:
        return "call", f"禁调用名 {name}"
    if name in IDENTITY_REGISTRIES_T170:
        return "identity", f"借身份的入口 {name}"
    if name in LOADER_NAMES_T170:
        return "import", f"失败即关：动态加载入口 {name}"
    if name in INTROSPECTION_NAMES_T170:
        return "escape", f"内省口子 {name}"
    return None


def module_string_violation_t170(text: str) -> str | None:
    """字符串常量是禁区模块（或其成员）的名字：点分、``模块:属性``、``maos/…/x.py`` 都认。"""
    name = text[:-3] if text.endswith(".py") else text
    if not _MODULE_STRING_RE_T170.match(name):
        return None
    return import_violation_t170(name.replace("/", ".").replace(":", "."))


def star_violation_t170(base: str) -> str | None:
    """``from <base> import *`` 触犯哪条禁令：base 本身禁，或是禁区的祖先包。"""
    why = import_violation_t170(base)
    if why:
        return why
    if base in STAR_FORBIDDEN_BASES_T170:
        return f"star import 祖先包 {base}（会把禁区子模块一并绑进来）"
    return None


def star_reexports_t170(base: str) -> list[str]:
    """``from <base> import *`` 实际绑出来的公开名里触犯禁令的（名字、真实出处都判）。"""
    mod = _resolve_t170(base)
    if not isinstance(mod, types.ModuleType):
        return []
    names = getattr(mod, "__all__", None)
    if not isinstance(names, (list, tuple)):
        names = [n for n in vars(mod) if not n.startswith("_")]
    out: list[str] = []
    for name in names:
        if not isinstance(name, str):
            continue
        why = target_violation_t170(f"{base}.{name}")
        hit = _name_violation_t170(name)
        if why or hit:
            out.append(f"{name}（{why or hit[1]}）")
    return out


def _module_of_t170(root: pathlib.Path, path: pathlib.Path) -> tuple[str, bool]:
    parts = list(path.relative_to(root).with_suffix("").parts)
    is_pkg = parts[-1] == "__init__"
    if is_pkg:
        parts = parts[:-1]
    return ".".join(parts), is_pkg


def _resolve_from_t170(module_name: str, is_pkg: bool, level: int,
                       module: str | None) -> str | None:
    """``from <level 个点><module> import …`` 的绝对基名；越过顶层返回 None。"""
    if level == 0:
        return module or ""
    pkg = module_name.split(".") if is_pkg else module_name.split(".")[:-1]
    if level - 1 > len(pkg) - 1:
        return None
    base = pkg[:len(pkg) - (level - 1)]
    if module:
        base = base + module.split(".")
    return ".".join(base)


def _call_name_t170(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _bindings_t170(tree: ast.AST, module_name: str, is_pkg: bool) -> dict[str, str]:
    """文件里 import 绑定的名字 -> 绝对点分名（``import a.b`` 绑 ``a``；``as`` 绑全名）。"""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    out[alias.asname] = alias.name
                else:
                    head = alias.name.split(".")[0]
                    out[head] = head
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_from_t170(module_name, is_pkg, node.level, node.module)
            if base is None:
                continue
            for alias in node.names:
                if alias.name != "*":
                    out[alias.asname or alias.name] = f"{base}.{alias.name}" if base else alias.name
    return out


def _dotted_t170(node: ast.AST, bindings: dict[str, str]) -> str | None:
    """根名是 import 绑定的属性链还原成点分名；根不是 import 来的返回 None。"""
    if isinstance(node, ast.Name):
        return bindings.get(node.id)
    if isinstance(node, ast.Attribute):
        base = _dotted_t170(node.value, bindings)
        return f"{base}.{node.attr}" if base else None
    return None


def _bound_names_t170(tree: ast.AST) -> dict[str, int]:
    """文件里每个名字被绑定的次数（赋值、def / class、参数、import）。"""
    count: dict[str, int] = {}

    def bump(name: str) -> None:
        count[name] = count.get(name, 0) + 1

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bump(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bump(node.name)
        elif isinstance(node, ast.arg):
            bump(node.arg)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bump((alias.asname or alias.name).split(".")[0])
    return count


def _module_consts_t170(tree: ast.Module, bound: dict[str, int]) -> dict[str, ast.expr]:
    """模块顶层只绑定过一次的 ``NAME = <表达式>``，供 identity 字段静态求值。"""
    out: dict[str, ast.expr] = {}
    for stmt in tree.body:
        if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)):
            name, value = stmt.targets[0].id, stmt.value
        elif (isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
              and stmt.value is not None):
            name, value = stmt.target.id, stmt.value
        else:
            continue
        if bound.get(name) == 1:
            out[name] = value
    return out


def _as_strs_t170(value: Any) -> frozenset[str] | None:
    if isinstance(value, str):
        return frozenset({value})
    if isinstance(value, (frozenset, set, tuple, list)) and all(isinstance(v, str) for v in value):
        return frozenset(value)
    return None


def _static_strs_t170(node: ast.AST, consts: dict[str, ast.expr], bindings: dict[str, str],
                      bound: dict[str, int], depth: int = 0) -> frozenset[str] | None:
    """identity 字段的值静态求成字符串集合；求不出来（变量、调用、解包、自造集合类）返回 None。"""
    if depth > 8:
        return None

    def rec(n: ast.AST) -> frozenset[str] | None:
        return _static_strs_t170(n, consts, bindings, bound, depth + 1)

    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            return frozenset({node.value})
        if isinstance(node.value, bytes):
            return frozenset({node.value.decode("utf-8", "replace")})
        return None
    if isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        out: set[str] = set()
        for elt in node.elts:
            got = rec(elt)
            if got is None:
                return None
            out |= got
        return frozenset(out)
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in _SET_BUILDERS_T170 and node.func.id not in bound
            and not node.keywords and len(node.args) <= 1):
        return frozenset() if not node.args else rec(node.args[0])
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.BitOr, ast.Add)):
        left, right = rec(node.left), rec(node.right)
        return None if left is None or right is None else left | right
    if isinstance(node, ast.Name) and node.id in consts:
        return rec(consts[node.id])
    dotted = _dotted_t170(node, bindings)
    if dotted is not None:
        return _as_strs_t170(_resolve_t170(dotted))
    return None


def _scan_file_t170(root: pathlib.Path, path: pathlib.Path) -> list[str]:
    rel = path.relative_to(root).as_posix()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
    except (SyntaxError, UnicodeDecodeError) as exc:
        return [f"{rel}:0: 解析失败: {exc}"]
    module_name, is_pkg = _module_of_t170(root, path)
    bindings = _bindings_t170(tree, module_name, is_pkg)
    bound = _bound_names_t170(tree)
    consts = _module_consts_t170(tree, bound)
    call_funcs = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    out: list[str] = []

    def bad(node: ast.AST, kind: str, detail: str) -> None:
        out.append(f"{rel}:{getattr(node, 'lineno', 0)}: {kind}: {detail}")

    def check_import(node: ast.AST, target: str, how: str) -> None:
        why = target_violation_t170(target)
        if why:
            bad(node, "import", f"{how}{target} —— {why}")

    def check_name(node: ast.AST, name: str, how: str) -> None:
        hit = _name_violation_t170(name)
        if hit:
            bad(node, hit[0], f"{how}{hit[1]}")

    def check_identity_value(node: ast.AST, field: str, value: ast.AST) -> None:
        got = _static_strs_t170(value, consts, bindings, bound)
        if got is None:
            bad(node, "identity", f"{field} 求不出字面量集合（失败即关：判不了就不许）")
            return
        for lit in sorted(got):
            if field == "allowed_tools" or not lit.startswith(CS_SKILL_PREFIX_T170):
                bad(node, "identity", f"{field} 含 {lit!r}（前台只许持 cs.* skill、零工具）")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                check_import(node, alias.name, "")
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_from_t170(module_name, is_pkg, node.level, node.module)
            if base is None:
                bad(node, "import", f"相对 import 越过顶层（level={node.level}）")
                continue
            for alias in node.names:
                if alias.name == "*":
                    why = star_violation_t170(base)
                    if why:
                        bad(node, "import", f"from {base} import * —— {why}")
                        continue
                    for hit in star_reexports_t170(base):
                        bad(node, "import", f"from {base} import * 绑出 {hit}")
                    continue
                check_import(node, f"{base}.{alias.name}" if base else alias.name, "")
                check_name(node, alias.name, "import 名 ")
        elif isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
            text = (node.value if isinstance(node.value, str)
                    else node.value.decode("utf-8", "replace"))
            if text in FORBIDDEN_CONSTANTS_T170:
                bad(node, "constant", repr(text))
            check_name(node, text, f"字符串 {text!r}：")
            if text in IDENTITY_FIELDS_T170:
                bad(node, "identity", f"字符串 {text!r}（按名字改写 identity 的授权字段）")
            why = module_string_violation_t170(text)
            if why:
                bad(node, "import", f"字符串 {text!r} 是禁区模块名 —— {why}")
        elif isinstance(node, ast.Name):
            if node.id in FORBIDDEN_CALLS_T170:
                if id(node) not in call_funcs:
                    bad(node, "call", f"引用 {node.id}")
            else:
                check_name(node, node.id, "")
            if node.id in DYNAMIC_EXEC_T170:
                bad(node, "import", f"动态执行 {node.id} —— 扫描范围里一律不许")
        elif isinstance(node, ast.Attribute):
            if node.attr in FORBIDDEN_CALLS_T170:
                if id(node) not in call_funcs:
                    bad(node, "call", f"引用 .{node.attr}")
            else:
                check_name(node, node.attr, ".")
            if node.attr in DYNAMIC_EXEC_ATTRS_T170:
                bad(node, "import", f"动态执行 .{node.attr} —— 扫描范围里一律不许")
            if node.attr == "__dict__" and _dotted_t170(node.value, bindings):
                bad(node, "escape", f"反射 {_dotted_t170(node.value, bindings)}.__dict__")
            full = _dotted_t170(node, bindings)
            inner = _dotted_t170(node.value, bindings)
            if full and not (inner and target_violation_t170(inner)):
                check_import(node, full, "属性链 ")

        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for tgt in targets:
                for sub in ast.walk(tgt):
                    field = (sub.id if isinstance(sub, ast.Name)
                             else sub.attr if isinstance(sub, ast.Attribute) else None)
                    if field not in IDENTITY_FIELDS_T170:
                        continue
                    if isinstance(node, ast.AugAssign) or sub is not tgt:
                        bad(node, "identity", f"{field} 被就地改写 / 解包赋值（判不了）")
                    elif node.value is not None:
                        check_identity_value(node, field, node.value)

        if isinstance(node, ast.Call):
            name = _call_name_t170(node.func)
            if name in FORBIDDEN_CALLS_T170:
                bad(node, "call", name)
            if (isinstance(node.func, ast.Name) and node.func.id in REFLECTIVE_CALLS_T170
                    and node.args and _dotted_t170(node.args[0], bindings)):
                bad(node, "escape",
                    f"{node.func.id}(…) 反射访问导入的 {_dotted_t170(node.args[0], bindings)}")
            if name == "AgentIdentity":
                if (any(isinstance(a, ast.Starred) for a in node.args)
                        or any(k.arg is None for k in node.keywords)):
                    bad(node, "identity", "AgentIdentity 用 * / ** 解包传参（判不了）")
                if len(node.args) > 3:
                    check_identity_value(node, "allowed_skills", node.args[3])
                if len(node.args) > 4:
                    check_identity_value(node, "allowed_tools", node.args[4])
            for keyword in node.keywords:
                if keyword.arg in IDENTITY_FIELDS_T170:
                    check_identity_value(node, keyword.arg, keyword.value)
    return out


def _walk_scan_dir_t170(root: pathlib.Path, d: pathlib.Path) -> tuple[list[pathlib.Path], list[str]]:
    """一片扫描目录：返回（.py 文件，文件层违例）。不跟进符号链接，跳过 ``__pycache__``。"""
    files: list[pathlib.Path] = []
    anomalies: list[str] = []

    def rel(p: pathlib.Path) -> str:
        return p.relative_to(root).as_posix()

    if d.is_symlink():
        anomalies.append(f"{rel(d)}:0: file: 扫描目录本身是符号链接")
    for dirpath, dirnames, filenames in os.walk(d):          # followlinks=False
        here = pathlib.Path(dirpath)
        for name in sorted(dirnames):
            if (here / name).is_symlink():
                anomalies.append(f"{rel(here / name)}:0: file: 符号链接目录（扫描不跟进、import 会跟进）")
        dirnames[:] = sorted(n for n in dirnames if n != "__pycache__")
        for name in sorted(filenames):
            p = here / name
            if p.is_symlink():
                anomalies.append(f"{rel(p)}:0: file: 符号链接文件")
            elif name.endswith(".py"):
                files.append(p)
            elif name.endswith(SOURCELESS_SUFFIXES_T170):
                anomalies.append(f"{rel(p)}:0: file: 没有源码的可导入文件（扫描看不到内容）")
    return files, anomalies


def scan_cs_guard_t170(root: pathlib.Path) -> tuple[list[pathlib.Path], list[str]]:
    """扫 ``root`` 下的两片目录，返回（扫到的文件，违例）。目录不在就跳过。"""
    root = pathlib.Path(root)
    files: list[pathlib.Path] = []
    violations: list[str] = []
    for rel in SCAN_DIRS_T170:
        d = root.joinpath(*rel)
        if d.is_dir():
            found, anomalies = _walk_scan_dir_t170(root, d)
            files.extend(sorted(found))
            violations.extend(anomalies)
    for path in files:
        violations.extend(_scan_file_t170(root, path))
    return files, violations


def assert_not_idle_t170(root: pathlib.Path, files: list[pathlib.Path]) -> None:
    """防空转：扫描集非空，且含 ``maos/domain/cs/types.py``。"""
    rels = {p.relative_to(root).as_posix() for p in files}
    assert rels, f"{root} 下一个文件都没扫到 —— 守卫空转"
    assert "/".join(ANCHOR_T170) in rels, f"扫描集里没有 {'/'.join(ANCHOR_T170)}：{sorted(rels)}"


# ---------------------------------------------------------------------------
# 真仓库：全仓生效
# ---------------------------------------------------------------------------
def test_repo_cs_code_passes_static_guard_t170():
    files, violations = scan_cs_guard_t170(ROOT_T170)
    assert_not_idle_t170(ROOT_T170, files)
    assert violations == [], "\n".join(violations)


def test_repo_scan_covers_both_dirs_when_present_t170():
    files, _ = scan_cs_guard_t170(ROOT_T170)
    rels = {p.relative_to(ROOT_T170).as_posix() for p in files}
    assert {"maos/domain/cs/types.py", "maos/domain/cs/claims.py",
            "maos/domain/cs/evaluate.py"} <= rels
    skills_cs = ROOT_T170 / "maos" / "skills" / "builtin" / "cs"
    if skills_cs.is_dir():
        on_disk = {p.relative_to(ROOT_T170).as_posix() for p in skills_cs.rglob("*.py")
                   if "__pycache__" not in p.parts}
        assert on_disk <= rels


# ---------------------------------------------------------------------------
# 反向验证：tmp 目录造违规文件，守卫必须红
# ---------------------------------------------------------------------------
def _tree_t170(tmp_path: pathlib.Path, files: dict[str, str]) -> pathlib.Path:
    """在 tmp 下铺一棵小仓：锚文件 types.py 恒在，再加 ``files``。"""
    base = {"maos/domain/cs/types.py": "X = 1\n", "maos/domain/cs/__init__.py": ""}
    for rel, src in {**base, **files}.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(src), encoding="utf-8")
    return tmp_path


def _violations_t170(tmp_path: pathlib.Path, files: dict[str, str]) -> list[str]:
    root = _tree_t170(tmp_path, files)
    scanned, violations = scan_cs_guard_t170(root)
    assert_not_idle_t170(root, scanned)
    return violations


def _one_t170(violations: list[str], rel: str, kind: str) -> None:
    hits = [v for v in violations if v.startswith(rel + ":") and f": {kind}: " in v]
    assert hits, f"期望 {rel} 报 {kind}，实际：{violations}"


def test_clean_tree_is_green_t170(tmp_path):
    assert _violations_t170(tmp_path, {"maos/domain/cs/ok.py": "import json\n"}) == []


def test_idle_scan_is_caught_t170(tmp_path):
    """防空转判据本身能判负：没有锚文件 / 一个文件都没有时断言失败。"""
    files, violations = scan_cs_guard_t170(tmp_path)
    assert files == [] and violations == []
    with pytest.raises(AssertionError):
        assert_not_idle_t170(tmp_path, files)
    (tmp_path / "maos" / "domain" / "cs").mkdir(parents=True)
    (tmp_path / "maos" / "domain" / "cs" / "other.py").write_text("", encoding="utf-8")
    files, _ = scan_cs_guard_t170(tmp_path)
    with pytest.raises(AssertionError):
        assert_not_idle_t170(tmp_path, files)


def test_relative_imports_resolve_by_package_t170(tmp_path):
    v = _violations_t170(tmp_path, {
        "maos/domain/cs/rel_domain.py": "from ..refund import objects\n",
        "maos/domain/cs/rel_pkg.py": "from .. import ap\n",
        "maos/domain/cs/sub/__init__.py": "from ...refund.guard import check\n",
        "maos/skills/builtin/cs/__init__.py": "from .. import refund\n",
        "maos/skills/builtin/cs/answer.py": "from ..refund.intake import run\n",
        "maos/domain/cs/too_far.py": "from ....... import x\n",
    })
    _one_t170(v, "maos/domain/cs/rel_domain.py", "import")
    assert any("maos.domain.refund.objects" in x for x in v)
    _one_t170(v, "maos/domain/cs/rel_pkg.py", "import")
    assert any("maos.domain.ap" in x for x in v)
    _one_t170(v, "maos/domain/cs/sub/__init__.py", "import")
    assert any("maos.domain.refund.guard.check" in x for x in v)
    _one_t170(v, "maos/skills/builtin/cs/__init__.py", "import")
    assert any("maos.skills.builtin.refund" in x for x in v)
    _one_t170(v, "maos/skills/builtin/cs/answer.py", "import")
    _one_t170(v, "maos/domain/cs/too_far.py", "import")


def test_aliased_imports_are_caught_t170(tmp_path):
    v = _violations_t170(tmp_path, {
        "maos/domain/cs/alias1.py": "import maos.runtime.gate as harmless\n",
        "maos/domain/cs/alias2.py": "from maos.core.control_plane import ControlPlane as CP\n",
        "maos/domain/cs/alias3.py": "from maos.agents import manager as m\n",
    })
    for rel in ("alias1", "alias2", "alias3"):
        _one_t170(v, f"maos/domain/cs/{rel}.py", "import")


def test_imports_inside_function_bodies_are_caught_t170(tmp_path):
    v = _violations_t170(tmp_path, {
        "maos/domain/cs/lazy.py": """
            def f():
                import hiclaw.client
                return hiclaw

            class K:
                def m(self):
                    def inner():
                        from maos.flows import refund_flow
                    return inner
        """,
    })
    hits = [x for x in v if x.startswith("maos/domain/cs/lazy.py:")]
    assert len(hits) == 2, v
    assert any("hiclaw.client" in x for x in hits) and any("maos.flows" in x for x in hits)


@pytest.mark.parametrize("stmt", [
    "import maos.flows",
    "import hiclaw",
    "from maos.ingress import router",
    "from maos.ingress.outcome_commands import handle",
    "import maos.ingress.chat",
    "from maos.nlu import intent",
    "from maos.roundtable.verdict import _clean",
    "from maos.tools import gateway",
    "from maos.skills.builtin.refund import payment_execute",
    "import maos.skills.builtin.kb_retrieve",
    "from maos.skills.builtin import ap",
    "from maos.agents.manager import ManagerAgent",
    "from maos.skills.registry import SkillRegistry",
    "import maos.skills.registry",
    "from maos.runtime.worker import Worker",
])
def test_each_forbidden_prefix_is_caught_t170(tmp_path, stmt):
    v = _violations_t170(tmp_path, {"maos/domain/cs/bad.py": stmt + "\n"})
    _one_t170(v, "maos/domain/cs/bad.py", "import")


@pytest.mark.parametrize("stmt", [
    "from maos.domain.refund.objects import load_case",
    "from maos.domain.refund import guard",
    "import maos.domain.refund",
    "from maos.domain import refund",
    "import maos.domain.ap.guard",
    "from maos.domain.claim import objects",
    "from maos.domain import DOMAIN_REGISTRY",
    "from maos.domain._case_store import CaseStore",
])
def test_cross_domain_imports_are_caught_t170(tmp_path, stmt):
    v = _violations_t170(tmp_path, {"maos/domain/cs/bad.py": stmt + "\n"})
    _one_t170(v, "maos/domain/cs/bad.py", "import")
    assert any("跨域" in x for x in v), v


def test_dynamic_imports_are_caught_t170(tmp_path):
    v = _violations_t170(tmp_path, {
        "maos/domain/cs/dyn.py": """
            import importlib
            m = importlib.import_module("maos.runtime.gate")
            n = __import__("maos.domain.refund.objects")
            ok = importlib.import_module("maos.kb.retriever")
        """,
    })
    lines = {int(x.split(":")[1]) for x in v if x.startswith("maos/domain/cs/dyn.py:")}
    # 失败即关：import 本身、禁区目标、允许的目标（maos.kb.retriever）一样都判
    assert lines == {2, 3, 4, 5}, v


@pytest.mark.parametrize("const", sorted(FORBIDDEN_CONSTANTS_T170))
def test_each_forbidden_constant_is_caught_t170(tmp_path, const):
    v = _violations_t170(tmp_path, {"maos/domain/cs/const.py": f"SKILL = {const!r}\n"})
    _one_t170(v, "maos/domain/cs/const.py", "constant")


def test_forbidden_constants_anywhere_but_only_on_equality_t170(tmp_path):
    v = _violations_t170(tmp_path, {
        "maos/skills/builtin/cs/k.py": """
            def f(name="order.query"):
                return {"skill": ("carrier.ship",)}
            OK = ("payment.execute_preview", "order", "refund.intake", "payment")
        """,
    })
    hits = [x for x in v if ": constant: " in x]
    assert len(hits) == 2, v
    assert any("'order.query'" in x for x in hits) and any("'carrier.ship'" in x for x in hits)


@pytest.mark.parametrize("src", [
    "gate.decide(task)",
    "decide(task)",
    "self.store.human_decision('x')",
    "record_approval()",
    "cp.run_payload(p)",
    "handle_execute(x)",
    "r.handle_gate_decision(x)",
    "_record_gate_approval(x)",
    "invoker.invoke_tool('t', {})",
    "getattr(gate, 'decide')(task)",
])
def test_forbidden_calls_are_caught_t170(tmp_path, src):
    v = _violations_t170(tmp_path, {"maos/domain/cs/call.py": f"def f():\n    {src}\n"})
    _one_t170(v, "maos/domain/cs/call.py", "call")


def test_similar_call_names_are_not_flagged_t170(tmp_path):
    v = _violations_t170(tmp_path, {
        "maos/domain/cs/near.py": """
            def f(x):
                x.decided()
                x.decide_later
                getattr(x, "record")
                return decider(x)
        """,
    })
    assert v == []


def test_allowed_list_is_not_flagged_t170(tmp_path):
    """契约 §2.3「明确允许」逐条：写过头的守卫会在这里红。"""
    v = _violations_t170(tmp_path, {
        "maos/domain/cs/allowed.py": """
            from __future__ import annotations
            import json, re, pathlib, hashlib, os, logging
            from dataclasses import dataclass
            from typing import Any
            from maos.domain.cs import types
            from maos.domain.cs.types import Claim, ReplyDraft
            from . import types as T2
            from .types import CheckResult
            from .sub import helper
            from maos.domain import cs
            from maos.domain.refund.projection import PUBLIC_SETTLED
            from maos.domain.refund import projection
            from ..refund import projection as proj
            from ..refund.projection import PUBLIC_STATUSES
            from maos.domain import _dbport, _schema_util
            from maos.domain._dbport import DomainConn
            from maos.domain._schema_util import split_sql
            import maos.kb
            import maos.kb.retriever
            from maos import kb
            from maos.kb import retriever
            from maos.kb.retriever import emit_kb_retrieved, retrieve
            from maos.core.store import SqliteStore
            from maos.skills.contract import SkillSpec
            from maos.skills.registry import register_skill
            from maos.skills.invoker import SkillInvoker
            from maos.agents.base import AgentIdentity
            from maos.agents import base
            from maos.ingress.contracts import InboundMessage, CHANNEL_WECHAT_KF
            from maos.model.client import ModelClient

            import maos.domain.cs.types
            from maos.domain.cs.types import *
            from . import *
            from maos.kb.retriever import *
            from maos.domain.refund.projection import *
            import re as _re

            CS_FRONT_DESK = AgentIdentity(agent_id="cs-front-desk", role="cs_front_desk",
                                          duty="客服前台", allowed_skills=frozenset(
                                              {"cs.answer", "cs.handoff"}),
                                          allowed_tools=frozenset(), max_risk="L")
            SAME = AgentIdentity("cs-2", "cs_front_desk", "d", frozenset({"cs.answer"}), frozenset())
            PAT = _re.compile("x")
            V = (projection.PUBLIC_SETTLED, maos.kb.retriever.retrieve, maos.domain.cs.types.Claim,
                 retriever.emit_kb_retrieved, base.AgentIdentity, types.plan_id_for, kb.retriever)

            def lazy():
                from maos.kb import retriever as r
                return r

            # 失败即关只关动态加载的口子，sys / os / logging 的日常用法不误报
            import sys
            import logging
            from os import environ
            from typing import *
            LOG = logging.getLogger("maos.domain.cs.desk")
            TENANTS = os.environ.get("MAOS_CS_TENANTS", "") or environ.get("X", "")
            HERE = os.path.join(os.path.dirname(__file__), "x")

            def warn(msg, obj):
                sys.stderr.write(msg)
                return getattr(obj, "route", None), vars(obj), obj.__dict__

            SKILLS = frozenset({"cs.answer", "cs.handoff"})
            BY_CONST = AgentIdentity("cs-3", "cs_front_desk", "d", SKILLS, frozenset())
            BY_KW = AgentIdentity(agent_id="cs-4", role="r", duty="d",
                                  allowed_skills=SKILLS | {"cs.answer"}, allowed_tools=())
        """,
        "maos/domain/cs/sub/__init__.py": "from .. import types\nfrom ..types import Claim\n",
        "maos/skills/builtin/cs/__init__.py": "from . import answer, handoff\n"
                                              "from maos.skills.builtin.cs import answer as a2\n",
        "maos/skills/builtin/cs/answer.py": "from ....domain.cs.claims import check_reply\n"
                                            "from maos.skills.registry import register_skill\n",
    })
    assert v == [], "\n".join(v)


def test_import_rule_matches_segments_not_string_prefixes_t170():
    assert import_violation_t170("maos.tools") is not None
    assert import_violation_t170("maos.tools.gateway") is not None
    assert import_violation_t170("maos.toolsmith") is None
    assert import_violation_t170("maos.runtimes") is None
    assert import_violation_t170("hiclawx") is None
    assert import_violation_t170("maos.domain.refund.projection") is None
    assert import_violation_t170("maos.domain.refund.projectionx") is not None


def test_unparseable_file_is_a_violation_t170(tmp_path):
    v = _violations_t170(tmp_path, {"maos/domain/cs/broken.py": "def (:\n"})
    _one_t170(v, "maos/domain/cs/broken.py", "解析失败")


# ---------------------------------------------------------------------------
# 复核补的判据（DECISIONS task-t170 第二批）：借身份、包着禁工具的 skill 名、
# star import / 属性链、动态 import 的其余写法、引用禁调用名、文件层盲区
# ---------------------------------------------------------------------------
def test_skill_wrappers_of_forbidden_tools_are_forbidden_t170():
    """注册表反推：凡 depends_tools 碰到禁工具的 skill，名字必须在禁常量表里。

    新 skill 包了禁工具而这里没列，当场红；反推出的集合必须含已知那几个（防空转）。
    """
    import maos.skills.builtin  # noqa: F401 —— import 即注册（冻结契约 C-1）
    from maos.skills.registry import SKILL_REGISTRY

    wrappers = {name for name, versions in SKILL_REGISTRY.items()
                for cls in versions.values()
                if set(cls.contract.depends_tools) & CONTRACT_FORBIDDEN_CONSTANTS_T170}
    assert SKILL_WRAPPERS_T170 <= wrappers, sorted(SKILL_WRAPPERS_T170 - wrappers)
    assert wrappers <= FORBIDDEN_CONSTANTS_T170, sorted(wrappers - FORBIDDEN_CONSTANTS_T170)


@pytest.mark.parametrize("skill", sorted(SKILL_WRAPPERS_T170))
def test_skill_wrapper_names_are_caught_t170(tmp_path, skill):
    v = _violations_t170(tmp_path, {"maos/domain/cs/wrap.py": f"SKILL = {skill!r}\n"})
    _one_t170(v, "maos/domain/cs/wrap.py", "constant")


def test_borrowed_identity_via_agent_pool_is_caught_t170(tmp_path):
    """复核给的原样攻击：只用允许清单里的 import，借 rtv_logistics 的身份调 rtv.ship。"""
    v = _violations_t170(tmp_path, {
        "maos/domain/cs/borrow.py": """
            from maos.agents.base import AGENT_POOL
            from maos.skills.invoker import SkillInvoker

            def ship(store, t, c):
                ident = AGENT_POOL["rtv_logistics"].identity
                return SkillInvoker(ident, store).invoke("rtv.ship", {"tenant_id": t, "case_id": c})
        """,
        "maos/domain/cs/borrow2.py": """
            from maos.agents import base
            from maos.skills.invoker import SkillInvoker

            def cancel(store, payload):
                ident = base.AGENT_POOL["investigation_cancel"].identity
                return SkillInvoker(ident, store).invoke("investigation.cancel", payload)
        """,
        "maos/domain/cs/borrow3.py": """
            from maos.agents import base
            pool = getattr(base, "AGENT_POOL")
        """,
    })
    for rel in ("borrow", "borrow2", "borrow3"):
        _one_t170(v, f"maos/domain/cs/{rel}.py", "identity")
    _one_t170(v, "maos/domain/cs/borrow.py", "constant")
    _one_t170(v, "maos/domain/cs/borrow2.py", "constant")


@pytest.mark.parametrize("src", [
    'AgentIdentity(agent_id="x", role="y", duty="z", allowed_skills=frozenset({"refund.intake"}))',
    'AgentIdentity("x", "y", "z", frozenset({"cs.answer", "notify.customer"}))',
    'AgentIdentity("x", "y", "z", frozenset({"cs.answer"}), frozenset({"carrier.track"}))',
    'dataclasses.replace(CS_ID, allowed_skills=frozenset({"cs.answer", "kb.sink"}))',
    'AgentIdentity(agent_id="x", role="y", duty="z", allowed_tools={"bank.query"})',
])
def test_forged_identity_is_caught_t170(tmp_path, src):
    v = _violations_t170(tmp_path, {"maos/domain/cs/forge.py": f"X = {src}\n"})
    _one_t170(v, "maos/domain/cs/forge.py", "identity")


def test_capability_identities_are_forbidden_t170(tmp_path):
    v = _violations_t170(tmp_path, {
        "maos/domain/cs/cap.py": "from maos.capability.profiles import identities\n",
        "maos/domain/cs/cap2.py": "import maos.capability\n",
    })
    _one_t170(v, "maos/domain/cs/cap.py", "import")
    _one_t170(v, "maos/domain/cs/cap2.py", "import")


@pytest.mark.parametrize("src", [
    "from maos.skills.builtin import *",
    "from maos.domain import *",
    "from maos.domain.refund import *",
    "from maos.agents import *",
    "from maos import *",
    "from maos.core import *",
    "from maos.ingress import *",
    "from maos.runtime.gate import *",
    "from .. import *",
])
def test_star_import_from_ancestor_package_is_caught_t170(tmp_path, src):
    v = _violations_t170(tmp_path, {"maos/domain/cs/star.py": src + "\n"})
    _one_t170(v, "maos/domain/cs/star.py", "import")


@pytest.mark.parametrize("src", [
    "from maos.skills import builtin\nX = builtin.refund.payment_execute\n",
    "import maos.skills.builtin.cs\nX = maos.skills.builtin.refund.payment_execute\n",
    "import maos.kb\nX = maos.runtime.gate\n",
    "from maos import agents\nX = agents.manager\n",
    "from maos import domain\nX = domain.refund.objects\n",
    "import maos.domain.cs as cs\nX = cs.types\nY = maos\n",
])
def test_attribute_chain_to_forbidden_module_is_caught_t170(tmp_path, src):
    v = _violations_t170(tmp_path, {"maos/domain/cs/chain.py": src})
    if "cs.types" in src:
        assert v == [], v                        # 对照：链落在允许区里不报
        return
    hits = [x for x in v if x.startswith("maos/domain/cs/chain.py:") and "属性链" in x]
    assert len(hits) == 1, v                     # 最短的那一截报一次，不按链长重复报


@pytest.mark.parametrize("src", [
    'importlib.import_module(name="maos.runtime.gate")',
    'importlib.import_module(".objects", "maos.domain.refund")',
    'importlib.import_module("..refund.objects", __package__)',
    'importlib.import_module(".objects", pkg)',
    '__import__("maos", fromlist=["runtime"])',
    '__import__("maos.skills.builtin", globals(), locals(), ["refund"], 0)',
    '__import__("refund.objects", globals(), locals(), ["x"], 2)',
    '__import__("x", level=lv)',
    'sys.modules["maos.runtime.gate"]',
    'sys.modules.get("maos.domain.refund.objects")',
    'importlib.util.find_spec("maos.runtime.gate")',
    'pkgutil.resolve_name("maos.runtime.gate:Gate")',
    'runpy.run_module("maos.flows.scenario_7")',
    'exec("import maos.runtime.gate")',
    'eval("__import__(name)")',
    'compile(src, "f", "exec")',
    'builtins.exec(src)',
])
def test_other_dynamic_import_forms_are_caught_t170(tmp_path, src):
    v = _violations_t170(tmp_path, {
        "maos/domain/cs/dyn2.py": "import sys\n"
                                  "def f(pkg, lv, src, name, importlib, pkgutil, runpy, builtins):\n"
                                  f"    return {src}\n",
    })
    # 加载器是参数传进来的（import 那一行不在这里）：判的必须是第 3 行这句本身
    assert any(x.startswith("maos/domain/cs/dyn2.py:3: import: ") for x in v), v


@pytest.mark.parametrize("src", [
    "fn = gate.decide",
    "functools.partial(gate.decide, t)()",
    "list(map(gate.decide, ts))",
    'gate.__getattribute__("decide")(t)',
    'operator.methodcaller("decide", t)(gate)',
    'vars(gate)["decide"](t)',
    "cb = invoker.invoke_tool",
    "from somewhere import record_approval",
])
def test_forbidden_call_names_referenced_as_values_are_caught_t170(tmp_path, src):
    v = _violations_t170(tmp_path, {"maos/domain/cs/ref.py": f"def f(gate, t, ts, invoker):\n    {src}\n"})
    _one_t170(v, "maos/domain/cs/ref.py", "call")


def test_symlinks_and_sourceless_modules_are_caught_t170(tmp_path):
    """文件层盲区：符号链接目录 / 文件、包目录里的 .pyc / .so / .pth。__pycache__ 不算。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "evil.py").write_text("import maos.runtime.gate\n", encoding="utf-8")
    root = _tree_t170(tmp_path, {"maos/skills/builtin/cs/__init__.py": ""})
    cs = root / "maos" / "domain" / "cs"
    (cs / "linked").symlink_to(outside, target_is_directory=True)
    (cs / "alias.py").symlink_to(outside / "evil.py")
    (cs / "pyconly.pyc").write_bytes(b"\x00" * 16)
    (root / "maos" / "skills" / "builtin" / "cs" / "native.cpython-311-x86_64-linux-gnu.so"
     ).write_bytes(b"\x7fELF")
    (cs / "hook.pth").write_text("import maos.runtime.gate\n", encoding="utf-8")
    (cs / "__pycache__").mkdir()
    (cs / "__pycache__" / "types.cpython-311.pyc").write_bytes(b"\x00" * 16)
    files, v = scan_cs_guard_t170(root)
    assert_not_idle_t170(root, files)
    for rel in ("maos/domain/cs/linked", "maos/domain/cs/alias.py", "maos/domain/cs/pyconly.pyc",
                "maos/skills/builtin/cs/native.cpython-311-x86_64-linux-gnu.so",
                "maos/domain/cs/hook.pth"):
        _one_t170(v, rel, "file")
    assert not any("__pycache__" in x for x in v), v
    assert all(": file: " in x for x in v), v


def test_scan_dir_itself_symlinked_is_caught_t170(tmp_path):
    real = tmp_path / "real_cs"
    real.mkdir()
    (real / "types.py").write_text("X = 1\n", encoding="utf-8")
    (tmp_path / "maos" / "domain").mkdir(parents=True)
    (tmp_path / "maos" / "domain" / "cs").symlink_to(real, target_is_directory=True)
    files, v = scan_cs_guard_t170(tmp_path)
    assert_not_idle_t170(tmp_path, files)
    _one_t170(v, "maos/domain/cs", "file")


# ---------------------------------------------------------------------------
# 复核第三批（DECISIONS task-t170 修复轮二）：动态加载失败即关（L3-A）、按真实出处判
# 再导出（L3-B）、identity 字段静态求值（L3-C）、bytes 常量（L3-D）
# ---------------------------------------------------------------------------
def _lines_t170(violations: list[str], rel: str) -> set[int]:
    return {int(x.split(":")[1]) for x in violations if x.startswith(rel + ":")}


@pytest.mark.parametrize("src,line", [
    # 复核给的原样写法（L3-A）：导入函数起别名 / 间接拿到，模块名是完整字面量
    ("import importlib\nm = getattr(importlib, 'import_module')('maos.runtime.gate')\n", 1),
    ("from importlib import import_module as load\nm = load('maos.runtime.gate')\n", 1),
    ("m = __builtins__['__import__']('maos.runtime.gate')\n", 1),
    ("import importlib.machinery\n"
     "m = importlib.machinery.SourceFileLoader('g', 'maos/runtime/gate.py').load_module()\n", 1),
    ("import importlib.util\ns = importlib.util.spec_from_file_location('g', 'maos/runtime/gate.py')\n", 1),
    ("import pkgutil\nl = pkgutil.get_loader('maos.runtime.gate')\n", 1),
    ("import sys\nm = vars(sys)['modules']['maos.runtime.gate']\n", 2),
    ("import sys\nmods = sys.modules\nm = mods['maos.runtime.gate']\n", 2),
    ("import sys\nm = dict(sys.modules).get('maos.runtime.gate')\n", 2),
    ("import runpy\nrunpy.run_path('maos/flows/refund_case.py')\n", 1),
    ("import sys\nm = sys.modules['maos'].runtime.gate\n", 2),
    ("import importlib\ng = importlib.import_module('maos').runtime.gate\n", 1),
    ("g = __import__('maos').runtime.gate\n", 1),
    # 同一类的其余写法：起进程、模块表的其余拿法、调用栈与函数的全局表、按名字解析的标准库
    ("import subprocess, sys\nsubprocess.run([sys.executable, '-m', 'maos.flows.refund_case'])\n", 1),
    ("import os\nos.system('python -m maos.flows.refund_case')\n", 2),
    ("from os import *\n", 1),
    ("from sys import *\n", 1),
    ("import sys\nm = getattr(sys, 'modules')\n", 2),
    ("import sys\nm = sys.__dict__['modules']\n", 2),
    ("from logging import sys\nm = sys.modules\n", 2),
    ("import sys\ng = sys._getframe(1)\n", 2),
    ("def f(frame):\n    return frame.f_back.f_globals['gate']\n", 2),
    ("from maos.skills.invoker import SkillInvoker\nR = SkillInvoker.invoke.__globals__\n", 2),
    ("from maos.agents import base\nALL = base.Agent.__subclasses__()\n", 2),
    ("import gc\nmods = gc.get_objects()\n", 1),
    ("import pydoc\ng = pydoc.locate('maos.runtime.gate')\n", 1),
    ("from unittest import mock\np = mock.patch('maos.runtime.gate.decide_all')\n", 2),
    ("import logging.config\nlogging.config.dictConfig({'x': {'()': 'maos.runtime.gate.Gate'}})\n", 2),
    ("run = exec\nrun('import maos.runtime.gate')\n", 1),
    ("def f(b):\n    return b.eval('1')\n", 2),
    ("def f(loader):\n    return loader.exec_module\n", 2),
])
def test_dynamic_loading_fails_closed_t170(tmp_path, src, line):
    v = _violations_t170(tmp_path, {"maos/domain/cs/load.py": src})
    assert line in _lines_t170(v, "maos/domain/cs/load.py"), v


def test_repo_attack_shortcut_file_is_caught_t170(tmp_path):
    """复核 L3-A 的整文件原样：只给 import_module 起别名，靠允许清单里的 import 去付款。"""
    v = _violations_t170(tmp_path, {
        "maos/domain/cs/zz_shortcut.py": """
            from importlib import import_module as load
            from maos.core.store import SqliteStore

            def approve_and_pay(store: SqliteStore, tenant, case, gw):
                gate = load("maos.runtime.gate")
                objects = load("maos.domain.refund.objects")
                pay = load("maos.skills.builtin.refund.payment_execute")
                return gate, objects, pay
        """,
    })
    assert {2, 6, 7, 8} <= _lines_t170(v, "maos/domain/cs/zz_shortcut.py"), v


@pytest.mark.parametrize("text", [
    "maos.runtime.gate", "maos/flows/refund_case.py", "maos.runtime.gate:Gate",
    "hiclaw.client", "maos.skills.registry", "maos.domain.refund.objects",
    "maos.skills.builtin.refund.payment_execute",
])
def test_forbidden_module_name_strings_are_caught_t170(tmp_path, text):
    assert module_string_violation_t170(text) is not None
    v = _violations_t170(tmp_path, {"maos/domain/cs/names.py": f"X = {text!r}\n"})
    _one_t170(v, "maos/domain/cs/names.py", "import")


@pytest.mark.parametrize("text", [
    "maos.domain.cs.desk", "maos.kb.retriever", "maos", "hiclaw", "maos.domain.refund.projection",
    "maos.skills.registry.register_skill", "payment", "maos.tools是什么", "请联系 maos 客服",
])
def test_harmless_strings_are_not_module_names_t170(text):
    assert module_string_violation_t170(text) is None


@pytest.mark.parametrize("src,line", [
    ("from maos.skills.invoker import registry\n", 1),                   # 复核 b09
    ("from maos.skills.invoker import *\n", 1),                          # 复核 b10
    ("from maos.skills.version_demo import guard, objects\n", 1),        # 复核 b11
    ("from maos.ingress.classify import needs_human\n", 1),              # 函数出处在 refund 域
    ("from maos.skills import invoker\nR = invoker.registry.SKILL_REGISTRY\n", 2),
    ("import maos.skills.invoker\nR = maos.skills.invoker.registry\n", 2),
    ("from maos.skills.version_demo import *\n", 1),
])
def test_reexported_forbidden_objects_are_caught_t170(tmp_path, src, line):
    v = _violations_t170(tmp_path, {"maos/domain/cs/reexp.py": src})
    hits = [x for x in v if x.startswith(f"maos/domain/cs/reexp.py:{line}: import: ")]
    assert hits and all("实为" in x for x in hits), v
    assert len([x for x in v if "属性链" in x]) <= 1, v          # 属性链只报最短那一截


def test_synthetic_reexport_is_caught_regardless_of_repo_internals_t170(tmp_path, monkeypatch):
    """不依赖仓内哪个模块恰好再导出：在 sys.path 上造一个包，把禁区模块与函数转手出去。"""
    pkg = tmp_path / "site" / "relay_pkg_t170"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "relay.py").write_text(
        "from maos.domain.refund import objects\n"
        "from maos.skills.registry import get as Handy\n"
        "from maos.skills.registry import register_skill\n"
        "OK = 1\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path / "site"))
    _resolve_t170.cache_clear()
    try:
        assert value_violation_t170("relay_pkg_t170.relay.objects") is not None
        assert value_violation_t170("relay_pkg_t170.relay.Handy") is not None
        assert value_violation_t170("relay_pkg_t170.relay.register_skill") is None
        assert value_violation_t170("relay_pkg_t170.relay.OK") is None
        v = _violations_t170(tmp_path / "repo", {
            "maos/domain/cs/r1.py": "from relay_pkg_t170.relay import objects\n",
            "maos/domain/cs/r2.py": "from relay_pkg_t170 import relay\nX = relay.Handy\n",
            "maos/domain/cs/r3.py": "from relay_pkg_t170.relay import *\n",
            "maos/domain/cs/r4.py": "from relay_pkg_t170.relay import OK, register_skill\n",
        })
    finally:
        _resolve_t170.cache_clear()
    for rel in ("r1", "r2", "r3"):
        _one_t170(v, f"maos/domain/cs/{rel}.py", "import")
    assert not any(x.startswith("maos/domain/cs/r4.py:") for x in v), v


def test_value_rule_leaves_allowed_objects_alone_t170():
    for dotted in ("maos.skills.invoker.SkillInvoker", "maos.kb.retriever.emit_kb_retrieved",
                   "maos.core.store.SqliteStore", "maos.agents.base.AgentIdentity",
                   "maos.domain.refund.projection.PUBLIC_SETTLED", "os.path.join",
                   "os.environ", "typing.Text", "logging.getLogger", "maos.no_such_module.x"):
        assert value_violation_t170(dotted) is None, dotted
    assert value_violation_t170("maos.skills.invoker.registry") is not None


@pytest.mark.parametrize("src", [
    # 复核 b14：鸭子类型的类属性
    "class _Id:\n    agent_id = 'cs-front-desk'\n"
    "    allowed_skills = frozenset({'refund.intake', 'finance.settle'})\n",
    # 复核 b15 / b16：按名字改字段
    "import dataclasses\ndef f(ID):\n"
    "    return dataclasses.replace(ID, **{'allowed_skills': frozenset({'refund.intake'})})\n",
    "def f(ID):\n    object.__setattr__(ID, 'allowed_skills', frozenset({'refund.intake'}))\n",
    # 复核 b18：先赋给变量再传
    "from maos.agents.base import AgentIdentity\nS = frozenset({'refund.intake'})\n"
    "I = AgentIdentity('x', 'y', 'z', S)\n",
    # 复核 b19：__contains__ 恒真的集合
    "class _All(frozenset):\n    def __contains__(self, x):\n        return True\n"
    "class _Id:\n    agent_id = 'x'\n    allowed_skills = _All()\n",
    # 其余：属性赋值、就地并集、解包传参、参数透传、改掉内建构造器、变量里带工具
    "def f(ident):\n    ident.allowed_skills = {'refund.intake'}\n",
    "def f(ident):\n    ident.allowed_skills |= {'refund.intake'}\n",
    "from maos.agents.base import AgentIdentity\ndef f(kw):\n    return AgentIdentity(**kw)\n",
    "from maos.agents.base import AgentIdentity\ndef f(a):\n    return AgentIdentity(*a)\n",
    "def f(extra):\n    return dict(allowed_skills=frozenset({'cs.answer'}) | extra)\n",
    "frozenset = type('F', (set,), {'__contains__': lambda s, x: True})\n"
    "X = dict(allowed_skills=frozenset({'cs.answer'}))\n",
    "T = ('carrier.track',)\nX = dict(allowed_skills={'cs.answer'}, allowed_tools=T)\n",
    "def f(ident):\n    return getattr(ident, 'allowed_skills')\n",
])
def test_identity_forged_in_other_forms_is_caught_t170(tmp_path, src):
    v = _violations_t170(tmp_path, {"maos/domain/cs/forge2.py": src})
    _one_t170(v, "maos/domain/cs/forge2.py", "identity")


@pytest.mark.parametrize("src", [
    "SK = ('cs.answer', 'cs.handoff')\nX = dict(allowed_skills=frozenset(SK), allowed_tools=frozenset())\n",
    "SK = frozenset({'cs.answer'})\nX = dict(allowed_skills=SK | {'cs.handoff'}, allowed_tools=set())\n",
    "class Card:\n    allowed_skills = ('cs.handoff',)\n    allowed_tools = ()\n",
    "X = dict(allowed_skills=[\"cs.answer\"], allowed_tools=[])\n",
])
def test_identity_static_values_that_are_cs_only_pass_t170(tmp_path, src):
    assert _violations_t170(tmp_path, {"maos/domain/cs/ok_id.py": src}) == []


@pytest.mark.parametrize("src,kind", [
    ("X = b'payment.execute'.decode()\n", "constant"),               # 复核 b04
    ("X = b'carrier.ship'\n", "constant"),
    ("def f(g):\n    return getattr(g, b'decide'.decode())\n", "call"),
    ("X = b'AGENT_POOL'\n", "identity"),
    ("X = b'maos.runtime.gate'\n", "import"),
])
def test_bytes_constants_are_caught_t170(tmp_path, src, kind):
    v = _violations_t170(tmp_path, {"maos/domain/cs/b.py": src})
    _one_t170(v, "maos/domain/cs/b.py", kind)
