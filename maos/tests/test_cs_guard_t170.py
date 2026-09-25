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

## p13 增量（T175，review/p13-cs-contracts.md §1.4 T175）：``maos.*`` 改失败即关白名单

上面的禁前缀是黑名单：名单外的 ``maos.*``（例如 ``maos.obs.trace``、``maos.config``）一律放行。
p13 起扫描范围里的 ``maos.*`` 名字（import、from-import、属性链、字符串常量、按真实出处
还原出来的名字）**只许**落在 :data:`MAOS_ALLOWED_MODULES_T175` / :data:`MAOS_ALLOWED_NAMES_T175`
里（p12 契约 §2.3「明确允许」那几项 + ``maos.domain.cs.ports``；本域两棵子树
``maos.domain.cs`` / ``maos.skills.builtin.cs`` 整片放行 —— 它们自己就在扫描范围里）：

* 白名单模块的**成员**放行、白名单模块的**子模块**不放行（``maos.kb.plan_advice`` 不因
  ``maos.kb`` 在名单里就放行）；成员是别处的模块 / 函数 / 类时照旧按真实出处再判一次；
* 名单模块的祖先包（``maos`` / ``maos.domain`` / ``maos.skills.builtin`` ……）只能在属性链上
  **路过**：直接 import 它、或把它当值用（``Y = maos``，之后 ``Y.runtime.gate`` 守卫看不见）即判；
* **解析不了的判红**：from-import 的目标、以及本域子树外的 ``maos.*`` 名字，在测试进程里拿不到
  对象就判不了真实出处 —— 以前「拿不到就不判」，现在失败即关。本域子树里的名字不要求解析
  （tmp 里注入的本域文件在真仓库里不存在；本域文件本身逐个被扫）。

### p13 修复轮（复核 L2-6 / L3-2 / L3-3，DECISIONS task-t175）

* **模块对象只许紧接着取属性**：import 来的模块（白名单模块、本域模块、标准库模块、祖先包）被当值用
  （``_m = invoker``、``h(invoker)``、``[invoker][0]``、``return r``）即判 —— 换名之后的
  ``_m.registry`` 守卫看不见；``attrgetter`` / ``methodcaller`` / ``getmembers`` /
  ``getattr_static`` / ``__getattribute__`` 这几个按名字取属性的入口出现即判。
* **仓库里 maos 以外的一方代码**（run.py、scripts/、client/ …… 由仓库根目录列出）一律判红：
  它们自己定义的函数出处不在 maos，按出处判会放行，里面却装配 router / flows / 工具。
* **改模块搜索路径**：``sys.path``、``site`` 并进动态加载的禁口子，``__path__`` 出现即判。

### p13 修复轮二（复核 L2-1 / L3R2-2 / L3R2-3，DECISIONS task-t175）

* **星号绑出的名字照样判**：``from base import *`` 绑出的每个名字记成 ``base.<名字>`` 的绑定，之后的
  属性链、真实出处、「模块对象当值用」照常判（``from <本域模块> import *`` 之后的 ``kb.plan_advice`` /
  ``os.system`` / ``x = os``）；base 解析不了（绑了什么无从知道）即判。
* **起进程与内建的其余入口**：``os.*`` 那几个口子的真身 ``posix.*`` / ``nt.*`` 同名单禁，``pty`` /
  ``_posixsubprocess`` 整模块禁，``asyncio.subprocess`` / ``concurrent.futures.process`` 按真实出处禁，
  ``create_subprocess_*`` / ``subprocess_exec`` / ``ProcessPoolExecutor`` 等名字出现即判；``__self__``
  （``len.__self__`` 就是 builtins）与 ``getmodule`` 出现即判；``exec`` / ``eval`` / ``compile`` 当
  字符串键或调用参数用（``d['exec']``、``getattr(b, 'eval')``、``d.get('exec')``）即判，当路径段
  （``/ "eval" /``）不判；字节串 ``b'exec'`` 出现即判。
* **入口脚本目录里的裸名**：``python scripts/run_ingress.py`` 让 ``scripts/`` 进 ``sys.path[0]``，
  ``import run_ingress`` 就拿到 ``cmd_simulate``。仓库里放着 ``if __name__ == "__main__":`` 的每个目录
  （scripts、client、hiclaw、maos 本身与几个子目录……，动态找）里能当顶层名 import 的名字一律按一方
  代码判红（撞标准库名的除外）。
* 仍不做的（需主会话定口径，BACKLOG task-t175）：非 maos、非一方的普通 import（第三方包）按
  「标准库 + 名单」失败即关 —— p13 契约只写了 ``maos.*`` 白名单。
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
#: p13（T175 复核 L3-3）：改模块搜索路径的口子 —— 改了 sys.path 以后能用非 maos 的名字加载 maos 的
#: 源码；site 会处理 .pth。``__path__`` 是名字，见 :data:`ESCAPE_NAMES_T175`。
PATH_HOLES_T175: tuple[str, ...] = ("sys.path", "site")
#: p13（T175 复核 L3R2-2）：起进程的其余入口。``os.system`` 等的真身在 ``posix``（Windows 是 ``nt``）——
#: ``import posix; posix.system(...)`` 是同一个函数，按同一张名单禁（``posix.getpid`` 这类照旧放行）；
#: ``pty`` / ``_posixsubprocess`` 整模块禁；asyncio 的子进程与 concurrent.futures 的进程池按真实出处
#: （``asyncio.subprocess.*`` / ``concurrent.futures.process.*``）判，``asyncio.create_subprocess_exec``、
#: ``from concurrent.futures import ProcessPoolExecutor`` 都经「按真实出处再判一次」落到这里。
OS_PROCESS_NAMES_T175: tuple[str, ...] = tuple(
    h.split(".", 1)[1] for h in SYS_OS_HOLES_T170 if h.startswith("os."))
PROCESS_HOLES_T175: tuple[str, ...] = tuple(
    f"{mod}.{name}" for mod in ("posix", "nt") for name in OS_PROCESS_NAMES_T175) + (
    "pty", "_posixsubprocess", "asyncio.subprocess", "concurrent.futures.process")
DYNAMIC_PREFIXES_T170 = LOADER_MODULES_T170 + SYS_OS_HOLES_T170 + PATH_HOLES_T175 + PROCESS_HOLES_T175

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
#: p13（T175 复核 L3-2 / L3-3）：按名字取属性的其余入口、包的搜索路径 —— 名字出现即判（扫描范围里
#: 没有正当用途；``getattr`` 仍只在作用于导入名时判，因为对自己的对象取字段是日常用法）。
ESCAPE_NAMES_T175 = frozenset({
    "attrgetter", "methodcaller", "getmembers", "getmembers_static", "getattr_static",
    "__getattribute__", "__path__",
    # 复核 L3R2-2：``len.__self__`` 就是 builtins 模块（内建函数的绑定对象）；``inspect.getmodule``
    # 按对象交出模块对象（``getmodule(getmodule).__dict__['sys']`` 就是 sys）—— 两条都通向「模块对象只许
    # 紧接着取属性」管不到的地方
    "__self__", "getmodule",
})
#: p13（T175 复核 L3R2-2）：起进程的入口名，不论从哪拿到（事件循环的方法、别名、参数）出现即判。
#: ``system`` / ``popen`` 这类太常见的词不在这里（它们按 ``os.*`` / ``posix.*`` 的点分名判）。
PROCESS_NAMES_T175 = frozenset({
    "create_subprocess_exec", "create_subprocess_shell", "subprocess_exec", "subprocess_shell",
    "ProcessPoolExecutor", "posix_spawn", "posix_spawnp", "forkpty", "fork_exec",
})
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

#: p13（T175）：cs 扫描范围里 ``maos.*`` 的失败即关白名单 —— p12 契约 §2.3「明确允许」逐项，
#: 外加 p13 契约 §1.4 T175 点名的 ``maos.domain.cs.ports``（它在本域子树里，见下一张表）。
#: 名单模块的成员放行、子模块不放行；``maos.skills.registry`` 只许 ``register_skill`` 一个名字。
MAOS_ALLOWED_MODULES_T175: tuple[str, ...] = (
    "maos.domain.refund.projection",   # 唯一允许的跨域 import（五个对外字面值，零依赖）
    "maos.domain._dbport",             # 共享底座，不是域
    "maos.domain._schema_util",
    "maos.kb",
    "maos.kb.retriever",
    "maos.core.store",
    "maos.skills.contract",
    "maos.skills.invoker",
    "maos.agents.base",
    "maos.ingress.contracts",
    "maos.model.client",
)
MAOS_ALLOWED_NAMES_T175 = frozenset({"maos.skills.registry.register_skill"})
#: 本域两棵子树：本身就在扫描范围里（逐文件扫），整片放行、不要求解析。
#: ``maos.domain.cs.ports`` 在这里（p13 契约点名；ports 零依赖，也由上面的扫描钉住）。
CS_OWN_PACKAGES_T175: tuple[str, ...] = ("maos.domain.cs", "maos.skills.builtin.cs")


def _first_party_tops_t175(root: pathlib.Path) -> frozenset[str]:
    """仓库根下 maos 以外、能当顶层名 import 的一方代码：标识符命名的目录（含命名空间包）与 ``*.py``。

    撞标准库名的不算（防御：根目录下不该有，有了也不该把标准库判红）。
    """
    return _importable_names_in_t175(root, skip_private=True)


def _importable_names_in_t175(d: pathlib.Path, *, skip_private: bool = False) -> frozenset[str]:
    """``d`` 在 ``sys.path`` 上时能当顶层名 import 的名字：``*.py`` 的主名与标识符命名的子目录
    （含命名空间包）。``maos`` 与标准库名不算（有了也不该把标准库判红）。"""
    import sys

    out: set[str] = set()
    for p in d.iterdir():
        name = p.stem if (p.is_file() and p.suffix == ".py") else p.name if p.is_dir() else ""
        if (name and name.isidentifier() and not name.startswith("__") and name != "maos"
                and not (skip_private and name.startswith("_"))
                and name not in sys.stdlib_module_names):
            out.add(name)
    return frozenset(out)


#: 可执行入口脚本的标志（``if __name__ == "__main__":``）。
_MAIN_GUARD_RE_T175 = re.compile(r"^if\s+__name__\s*==\s*['\"]__main__['\"]\s*:", re.M)


def _entry_dirs_t175(root: pathlib.Path) -> list[pathlib.Path]:
    """仓库里放着可执行入口脚本的目录（``python <目录>/x.py`` 会把该目录放进 ``sys.path[0]``）。

    跳过点开头的目录（``.git``、``.worktrees`` ……）、``__pycache__`` 与 ``node_modules``。
    """
    found: set[pathlib.Path] = set()
    for dirpath, dirnames, filenames in os.walk(root):          # followlinks=False
        dirnames[:] = sorted(n for n in dirnames
                             if not n.startswith(".") and n not in ("__pycache__", "node_modules"))
        for name in filenames:
            if not name.endswith(".py"):
                continue
            try:
                text = (pathlib.Path(dirpath) / name).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if _MAIN_GUARD_RE_T175.search(text):
                found.add(pathlib.Path(dirpath))
                break
    return sorted(found)


def _entry_dir_names_t175(root: pathlib.Path) -> frozenset[str]:
    """放着入口脚本的每个目录里能当顶层名 import 的名字（复核 L3R2-3）：生产入口
    ``python scripts/run_ingress.py`` 让 ``sys.path[0] = scripts/``，``import run_ingress`` 就拿到
    ``cmd_simulate``；``python maos/main.py`` 让 ``import runtime`` 拿到 maos/runtime（绕开 maos.* 名单）。"""
    out: set[str] = set()
    for d in _entry_dirs_t175(root):
        out |= _importable_names_in_t175(d)
    return frozenset(out)


#: p13（T175 复核 L2-6 / L3-3 / L3R2-3）：仓库里 maos 以外的一方代码（run.py、scripts/、client/ ……），
#: 以及放着入口脚本的目录里的裸名（scripts/*.py 的 run_ingress / run_requests / verify ……、hiclaw/、
#: client/、maos/ 本身等）。它们自己定义的函数出处不在 maos，按真实出处判会放行，里面却装配 router /
#: flows / 工具 / 网关（``run_ingress.cmd_simulate`` 以内部人名义把 /refund 喂进 router）。一律判红。
#: 下限几个名字写死，防仓库布局变了以后这张表悄悄变空。
FIRST_PARTY_TOPS_T175 = (_first_party_tops_t175(ROOT_T170) | _entry_dir_names_t175(ROOT_T170)
                         | frozenset({"run", "scripts", "client", "run_ingress", "run_requests"}))


# ---------------------------------------------------------------------------
# 规则
# ---------------------------------------------------------------------------
def _has_prefix_t170(name: str, prefix: str) -> bool:
    return name == prefix or name.startswith(prefix + ".")


def import_violation_t170(name: str) -> str | None:
    """绝对模块名（或 ``模块.属性``）触犯哪条禁令；放行返回 None。

    先按 p12 的禁前缀判（报的理由更具体），再判仓库里 maos 以外的一方代码，最后按 p13 的
    ``maos.*`` 白名单判（失败即关）。
    """
    return (blacklist_violation_t175(name) or first_party_violation_t175(name)
            or whitelist_violation_t175(name))


def first_party_violation_t175(name: str) -> str | None:
    """顶层名是仓库里 maos 以外的一方代码（run / scripts / client ……）就判（复核 L2-6 / L3-3）。"""
    top = name.split(".")[0]
    if top in FIRST_PARTY_TOPS_T175:
        return (f"失败即关：{top} 是仓库里 maos 以外的一方代码（run.py / scripts / client ……），"
                f"会装配 router / flows / 工具，不在允许清单里")
    return None


def is_cs_own_t175(name: str) -> bool:
    """名字落在本域两棵子树里（``maos.domain.cs`` / ``maos.skills.builtin.cs``）。"""
    return any(_has_prefix_t170(name, pkg) for pkg in CS_OWN_PACKAGES_T175)


def _ancestors_t175(name: str) -> list[str]:
    parts = name.split(".")
    return [".".join(parts[:i]) for i in range(1, len(parts))]


def whitelist_violation_t175(name: str) -> str | None:
    """p13 失败即关：``maos.*`` 名字不在白名单里就判；非 ``maos`` 名字不归这条管。"""
    if name.split(".")[0] != "maos":
        return None
    if (is_cs_own_t175(name) or name in MAOS_ALLOWED_NAMES_T175
            or name in MAOS_ALLOWED_MODULES_T175):
        return None
    for mod in sorted(MAOS_ALLOWED_MODULES_T175, key=len, reverse=True):
        if not name.startswith(mod + "."):
            continue
        first = f"{mod}.{name[len(mod) + 1:].split('.')[0]}"
        obj = _resolve_t170(first)
        if isinstance(obj, types.ModuleType) and obj.__name__ == first:
            return f"失败即关：{first} 是 {mod} 的子模块，不在 maos.* 白名单里"
        return None                        # 名单模块的成员；真实出处另由 value 规则判
    return f"失败即关：{name} 不在 maos.* 白名单里"


#: 名单模块的祖先包：属性链上可以路过，直接 import 或当值用即判（见 :func:`_scan_file_t170`）。
#: 被黑名单点名的祖先（``maos.domain.refund``、``maos.skills.registry``）不算路过 —— 走到它就报。
MAOS_TRANSIT_PACKAGES_T175 = frozenset(
    anc for name in MAOS_ALLOWED_MODULES_T175 + tuple(MAOS_ALLOWED_NAMES_T175)
    + CS_OWN_PACKAGES_T175 for anc in _ancestors_t175(name)
) - frozenset(MAOS_ALLOWED_MODULES_T175) - frozenset(CS_OWN_PACKAGES_T175) - frozenset(
    {"maos.domain.refund", "maos.skills.registry"})


def unresolved_violation_t175(dotted: str, *, from_import: bool) -> str | None:
    """p13 失败即关：判不了真实出处就不放行。

    管两类：from-import 的目标（``from A import b`` 的 ``A.b``），以及本域子树外的 ``maos.*``
    名字。本域子树不要求解析（逐文件扫；tmp 里注入的本域文件在真仓库里没有）。
    """
    if is_cs_own_t175(dotted):
        return None
    if not (from_import or dotted.split(".")[0] == "maos"):
        return None
    if _resolve_t170(dotted) is _MISSING_T170:
        return f"失败即关：{dotted} 解析不了（拿不到对象就判不了真实出处）"
    return None


def blacklist_violation_t175(name: str) -> str | None:
    """p12 的禁令（契约 §2.3 禁前缀 + task-t170 复核补的），原样保留；放行返回 None。"""
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


def target_violation_t170(dotted: str, *, from_import: bool = False) -> str | None:
    """名字（黑名单 + p13 白名单）→ 真实出处 → 解析得了（p13 失败即关），依次判。"""
    return (import_violation_t170(dotted) or value_violation_t170(dotted)
            or unresolved_violation_t175(dotted, from_import=from_import))


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
    if name in ESCAPE_NAMES_T175:
        return "escape", f"按名字取属性 / 改包搜索路径 / 拿模块对象的口子 {name}"
    if name in PROCESS_NAMES_T175:
        return "import", f"失败即关：起进程的入口 {name}"
    return None


def module_string_violation_t170(text: str) -> str | None:
    """字符串常量是禁区模块（或其成员）的名字：点分、``模块:属性``、``maos/…/x.py`` 都认。"""
    name = text[:-3] if text.endswith(".py") else text
    if not _MODULE_STRING_RE_T170.match(name):
        return None
    dotted = name.replace("/", ".").replace(":", ".")
    why = blacklist_violation_t175(dotted)
    if why:
        return why
    # p13 白名单只管**真能加载到东西**的串：``logging.getLogger("maos.cs")`` 这种日志名
    # 不对应任何模块，不是加载目标（DECISIONS task-t175）。
    why = whitelist_violation_t175(dotted)
    if why and _resolve_t170(dotted) is not _MISSING_T170:
        return why
    return None


def star_violation_t170(base: str) -> str | None:
    """``from <base> import *`` 触犯哪条禁令：base 本身禁，或是禁区的祖先包。"""
    why = import_violation_t170(base)
    if why:
        return why
    if base in STAR_FORBIDDEN_BASES_T170:
        return f"star import 祖先包 {base}（会把禁区子模块一并绑进来）"
    return None


def star_names_t175(base: str) -> list[str] | None:
    """``from <base> import *`` 实际绑出来的名字（``__all__``，没有就是全部公开名）；base 解析不了
    （拿不到模块）返回 None —— 绑了哪些名字无从知道。"""
    mod = _resolve_t170(base)
    if not isinstance(mod, types.ModuleType):
        return None
    names = getattr(mod, "__all__", None)
    if not isinstance(names, (list, tuple)):
        names = [n for n in vars(mod) if not n.startswith("_")]
    return [n for n in names if isinstance(n, str)]


def star_reexports_t170(base: str) -> list[str]:
    """``from <base> import *`` 实际绑出来的公开名里触犯禁令的（名字、真实出处都判）。"""
    out: list[str] = []
    for name in star_names_t175(base) or ():
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


def _exec_key_t175(node: ast.AST) -> bool:
    """节点是不是字符串常量 exec / eval / compile（当键或参数用时判，见 :func:`_scan_file_t170`）。"""
    return (isinstance(node, ast.Constant) and isinstance(node.value, str)
            and node.value in DYNAMIC_EXEC_T170)


def _call_name_t170(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _bindings_t170(tree: ast.AST, module_name: str, is_pkg: bool) -> dict[str, str]:
    """文件里 import 绑定的名字 -> 绝对点分名（``import a.b`` 绑 ``a``；``as`` 绑全名）。

    ``from base import *`` 绑出的每个名字也记成 ``base.<名字>``（复核 L2-1）：否则
    ``from maos.domain.cs.scripts import *`` 之后的 ``kb.plan_advice``、``from maos.domain.cs.desk
    import *`` 之后的 ``os.system`` / ``x = os`` 根名没有绑定，属性链、真实出处、「模块对象当值用」
    三条规则一条都轮不到。
    """
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
                elif base:
                    for name in star_names_t175(base) or ():
                        out[name] = f"{base}.{name}"
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
    #: 作为属性链中间一截被继续取属性的节点（``maos.domain`` 在 ``maos.domain.cs`` 里）。
    deref_ids = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    out: list[str] = []

    def bad(node: ast.AST, kind: str, detail: str) -> None:
        out.append(f"{rel}:{getattr(node, 'lineno', 0)}: {kind}: {detail}")

    def check_import(node: ast.AST, target: str, how: str, *, from_import: bool = False) -> None:
        why = target_violation_t170(target, from_import=from_import)
        if why:
            bad(node, "import", f"{how}{target} —— {why}")

    def check_transit_value(node: ast.AST, dotted: str | None) -> None:
        """p13：import 来的**模块对象**只许紧接着取属性，被当值用（赋值、传参、返回、放进容器）即判。

        名单模块的祖先包（``maos`` / ``maos.domain`` ……）是一例；复核 L3-2 起扩到一切模块对象
        （白名单模块、本域模块、标准库模块）：``_m = invoker`` 之后的 ``_m.registry``、
        ``h(os).system`` 这类访问守卫看不见，只能在「当值用」这一步拦。
        """
        if dotted is None or id(node) in deref_ids:
            return
        if dotted in MAOS_TRANSIT_PACKAGES_T175:
            bad(node, "import", f"包命名空间 {dotted} 被当值用 —— 失败即关："
                                f"拿到它就能走到白名单外的任何子模块")
        elif (target_violation_t170(dotted) is None           # 名字本身已判红的，import 那处报过
              and isinstance(_resolve_t170(dotted), types.ModuleType)):
            bad(node, "import", f"模块对象 {dotted} 被当值用 —— 失败即关："
                                f"换名 / 传参之后的属性访问守卫看不见（复核 L3-2）")

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
                    if star_names_t175(base) is None:
                        # 复核 L2-1：绑出哪些名字无从知道，之后的属性链一条都判不了 —— 失败即关
                        bad(node, "import", f"from {base} import * —— 失败即关：{base} 解析不了，"
                                            f"绑出哪些名字无从判")
                        continue
                    for hit in star_reexports_t170(base):
                        bad(node, "import", f"from {base} import * 绑出 {hit}")
                    continue
                check_import(node, f"{base}.{alias.name}" if base else alias.name, "",
                             from_import=True)
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
            if isinstance(node.value, bytes) and text in DYNAMIC_EXEC_T170:
                bad(node, "import", f"字节串 {text!r} —— 动态执行入口名，扫描范围里一律不许")
        elif isinstance(node, ast.Name):
            if node.id in FORBIDDEN_CALLS_T170:
                if id(node) not in call_funcs:
                    bad(node, "call", f"引用 {node.id}")
            else:
                check_name(node, node.id, "")
            if node.id in DYNAMIC_EXEC_T170:
                bad(node, "import", f"动态执行 {node.id} —— 扫描范围里一律不许")
            if isinstance(node.ctx, ast.Load):
                check_transit_value(node, bindings.get(node.id))
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
            # 只报最短的那一截：里一截已经报过（且不是只许路过的祖先包）就不再报。
            inner_reported = bool(inner) and inner not in MAOS_TRANSIT_PACKAGES_T175 and (
                target_violation_t170(inner) is not None)
            if full and not inner_reported:
                if full in MAOS_TRANSIT_PACKAGES_T175:
                    check_transit_value(node, full)
                else:
                    check_import(node, full, "属性链 ")
                    if isinstance(node.ctx, ast.Load):
                        check_transit_value(node, full)

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

        # 复核 L3R2-2：exec / eval / compile 当字符串键用（``b.__dict__['exec']``、``getattr(b, 'eval')``、
        # ``d.get('exec')``、``itemgetter('exec')``）即判。不是「出现即判」：evaluate.py 用 "eval" 当路径段
        # （``/ "eval" /``），那是 BinOp 的操作数，不是键也不是参数。
        if isinstance(node, ast.Subscript) and _exec_key_t175(node.slice):
            bad(node, "import", f"按字符串键 {node.slice.value!r} 取东西 —— 动态执行入口名，"
                                f"扫描范围里一律不许")

        if isinstance(node, ast.Call):
            for arg in list(node.args) + [k.value for k in node.keywords]:
                if _exec_key_t175(arg):
                    bad(node, "import", f"把 {arg.value!r} 当参数传 —— 动态执行入口名"
                                        f"（getattr / .get / itemgetter ……），扫描范围里一律不许")
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
            from maos.domain._schema_util import has_column
            import maos.kb
            import maos.kb.retriever
            from maos import kb
            from maos.kb import retriever
            from maos.kb.retriever import emit_kb_retrieved, retrieve
            from maos.core.store import SqliteStore
            from maos.skills.contract import SkillContract
            from maos.domain.cs.ports import ORDER_STATUS_WORDING, OrderLookup
            from maos.skills.registry import register_skill
            from maos.skills.invoker import SkillInvoker
            from maos.agents.base import AgentIdentity
            from maos.agents import base
            from maos.ingress.contracts import InboundMessage, CHANNEL_WECHAT_KF
            from maos.model.client import ModelClient

            import maos.domain.cs.types
            from maos.domain.cs.types import *
            from . import *
            from maos.domain.refund.projection import *
            import re as _re

            CS_FRONT_DESK = AgentIdentity(agent_id="cs-front-desk", role="cs_front_desk",
                                          duty="客服前台", allowed_skills=frozenset(
                                              {"cs.answer", "cs.handoff"}),
                                          allowed_tools=frozenset(), max_risk="L")
            SAME = AgentIdentity("cs-2", "cs_front_desk", "d", frozenset({"cs.answer"}), frozenset())
            PAT = _re.compile("x")
            V = (projection.PUBLIC_SETTLED, maos.kb.retriever.retrieve, maos.domain.cs.types.Claim,
                 retriever.emit_kb_retrieved, base.AgentIdentity, types.plan_id_for,
                 kb.retriever.retrieve)

            def lazy():
                from maos.kb import retriever as r
                return r.retrieve

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
    # 黑名单按点分段匹配（p12 口径）
    assert blacklist_violation_t175("maos.tools") is not None
    assert blacklist_violation_t175("maos.tools.gateway") is not None
    assert blacklist_violation_t175("maos.toolsmith") is None
    assert blacklist_violation_t175("maos.runtimes") is None
    assert import_violation_t170("hiclawx") is None
    assert import_violation_t170("maos.domain.refund.projection") is None
    assert import_violation_t170("maos.domain.refund.projectionx") is not None
    # p13 起名单外的 maos.* 由白名单兜住（T175）：不是禁前缀，照样不放行
    for name in ("maos.toolsmith", "maos.runtimes"):
        why = import_violation_t170(name)
        assert why is not None and "白名单" in why and "maos.tools " not in why, why


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
    # 对照：链落在允许区里不报（p13 起模块对象不许当值用，所以取到成员为止 —— 复核 L3-2）
    "import maos.domain.cs as cs\nX = cs.types.Claim\nY = maos\n",
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


@pytest.mark.parametrize("src,line,marker", [
    ("from maos.skills.invoker import registry\n", 1, "实为"),                   # 复核 b09
    ("from maos.skills.invoker import *\n", 1, "实为"),                          # 复核 b10
    # 下面三条的模块名本身在 p13 白名单外（T175），名字那一关就拦住，轮不到按出处判
    ("from maos.skills.version_demo import guard, objects\n", 1, "白名单"),      # 复核 b11
    ("from maos.ingress.classify import needs_human\n", 1, "白名单"),            # 函数出处在 refund 域
    ("from maos.skills import invoker\nR = invoker.registry.SKILL_REGISTRY\n", 2, "实为"),
    ("import maos.skills.invoker\nR = maos.skills.invoker.registry\n", 2, "实为"),
    ("from maos.skills.version_demo import *\n", 1, "白名单"),
])
def test_reexported_forbidden_objects_are_caught_t170(tmp_path, src, line, marker):
    v = _violations_t170(tmp_path, {"maos/domain/cs/reexp.py": src})
    hits = [x for x in v if x.startswith(f"maos/domain/cs/reexp.py:{line}: import: ")]
    assert hits and all(marker in x for x in hits), v
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


# ---------------------------------------------------------------------------
# p13（T175）：cs 扫描范围的 maos.* 改失败即关白名单
# ---------------------------------------------------------------------------
def test_whitelist_is_the_p12_allowed_list_plus_ports_t175():
    """名单逐项等于 p12 契约 §2.3「明确允许」+ 唯一跨域 + 共享底座；ports 在本域子树里。"""
    assert set(MAOS_ALLOWED_MODULES_T175) == {
        "maos.domain.refund.projection", "maos.domain._dbport", "maos.domain._schema_util",
        "maos.kb", "maos.kb.retriever", "maos.core.store", "maos.skills.contract",
        "maos.skills.invoker", "maos.agents.base", "maos.ingress.contracts", "maos.model.client"}
    assert MAOS_ALLOWED_NAMES_T175 == {"maos.skills.registry.register_skill"}
    assert is_cs_own_t175("maos.domain.cs.ports") and is_cs_own_t175("maos.skills.builtin.cs")
    assert not is_cs_own_t175("maos.domain.csx") and not is_cs_own_t175("maos.skills.builtin")
    # 名单里每一项都是真实存在的模块（写错一个字，名单就悄悄少一项）
    for mod in MAOS_ALLOWED_MODULES_T175:
        obj = _resolve_t170(mod)
        assert isinstance(obj, types.ModuleType) and obj.__name__ == mod, mod
    assert MAOS_TRANSIT_PACKAGES_T175 == {
        "maos", "maos.domain", "maos.core", "maos.skills", "maos.skills.builtin",
        "maos.agents", "maos.ingress", "maos.model"}


@pytest.mark.parametrize("stmt", [
    "from maos.obs.trace import KIND_PLAN",          # 任务点名的例子
    "import maos.obs.trace",
    "from maos.obs import trace",
    "from maos import obs",
    "from maos.config import get_config_source",
    "import maos.kb.plan_advice",                    # 名单模块的子模块不跟着放行
    "from maos.kb import plan_advice",
    "from maos.kb.plan_advice import advice_enabled",
    "import maos.core.eventbus",
    "from maos.core import eventbus",
    "import maos",                                   # 祖先包只许路过，不许直接拿
    "from maos import domain",
    "import maos.domain",
    "from maos.skills import builtin",
    "import maos.agents",
    "from maos.kb.retriever import *",               # 星号绑出 maos.config 的函数
])
def test_maos_modules_outside_whitelist_are_caught_t175(tmp_path, stmt):
    v = _violations_t170(tmp_path, {"maos/domain/cs/wl.py": stmt + "\n"})
    hits = [x for x in v if x.startswith("maos/domain/cs/wl.py:1: import: ")]
    assert hits and any("白名单" in x for x in hits), v


def test_whitelist_rule_is_fail_closed_by_name_t175():
    for name in ("maos.obs.trace", "maos.obs.trace.KIND_PLAN", "maos.config", "maos.kb.plan_advice",
                 "maos.kb.plan_advice.advice_enabled", "maos", "maos.domain", "maos.nonexistent_t175"):
        assert whitelist_violation_t175(name) is not None, name
    for name in ("maos.kb", "maos.kb.tokenize", "maos.kb.retriever.retrieve",
                 "maos.domain.cs.ports.ORDER_STATUS_WORDING", "maos.domain.cs.not_written_yet",
                 "maos.skills.builtin.cs.answer", "maos.skills.registry.register_skill",
                 "maos.model.client.Tier", "json", "os.path.join"):
        assert whitelist_violation_t175(name) is None, name


@pytest.mark.parametrize("src,line", [
    ("from maos.kb import no_such_name_t175\n", 1),
    ("from maos.kb.retriever import no_such_name_t175\n", 1),
    ("import maos.kb.no_such_sub_t175\n", 1),
    ("from json import no_such_name_t175\n", 1),         # from-import 一律要解析得了
    ("from no_such_pkg_t175 import thing\n", 1),
    ("from maos import kb\nX = kb.no_such_attr_t175\n", 2),
])
def test_unresolvable_targets_are_caught_t175(tmp_path, src, line):
    """以前「解析不了的不判」，p13 失败即关：判不了真实出处就不放行。"""
    v = _violations_t170(tmp_path, {"maos/domain/cs/unres.py": src})
    hits = [x for x in v if x.startswith(f"maos/domain/cs/unres.py:{line}: import: ")]
    assert hits and any("解析不了" in x for x in hits), v


@pytest.mark.parametrize("src,line", [
    ("import maos.kb\nY = maos\n", 2),
    ("import maos.kb\ndef f(g):\n    return g(maos)\n", 3),
    ("import maos.domain.cs.types\nY = maos.domain\n", 2),
    ("import maos.skills.builtin.cs\nY = maos.skills.builtin\n", 2),
])
def test_transit_package_used_as_value_is_caught_t175(tmp_path, src, line):
    """祖先包只能在属性链上路过；当值传出去以后 ``Y.runtime.gate`` 守卫就看不见了。"""
    v = _violations_t170(tmp_path, {"maos/domain/cs/transit.py": src})
    hits = [x for x in v if x.startswith(f"maos/domain/cs/transit.py:{line}: import: ")]
    assert hits and all("被当值用" in x for x in hits), v


def test_cs_own_and_passing_chains_stay_green_t175(tmp_path):
    """反面：本域子树不要求解析；祖先包在属性链上路过、落到名单里不报；日志名不是加载目标。"""
    v = _violations_t170(tmp_path, {
        "maos/domain/cs/own.py": """
            import logging
            import maos.kb
            import maos.domain.cs.types
            from maos.domain.cs.not_written_yet_t175 import helper
            from .also_not_written_t175 import other
            from maos.domain.cs import ports
            from maos.skills.builtin.cs import answer

            X = (maos.kb.tokenize, maos.domain.cs.types.Claim, ports.ORDER_STATUS_WORDING)
            LOG = logging.getLogger("maos.cs")
        """,
    })
    assert v == [], "\n".join(v)


@pytest.mark.parametrize("text,red", [
    ("maos.obs.trace", True),
    ("maos.config:get_config_source", True),
    ("maos/kb/plan_advice.py", True),
    ("maos.cs", False),                  # 日志名：不对应任何模块
    ("maos.domain.cs.desk", False),
    ("maos.kb.retriever", False),
])
def test_module_strings_follow_the_whitelist_t175(text, red):
    assert (module_string_violation_t170(text) is not None) is red, text


# ---------------------------------------------------------------------------
# p13 修复轮（T175 复核 L2-6 / L3-2 / L3-3）
# ---------------------------------------------------------------------------
_OPEN_TICKET_ALIASED_T175 = (                       # 复核 L3-2 原样：换名之后绕过 allowed_skills
    "import maos.skills.invoker as inv\n"
    "_m = inv\n"
    "def open_ticket(payload, ctx):\n"
    "    return _m.registry.get('refund.intake')().run(payload, ctx)\n"
)


@pytest.mark.parametrize("src,line", [
    (_OPEN_TICKET_ALIASED_T175, 2),
    ("import maos.skills.invoker as inv\n_m = inv\nR = _m.registry\n", 2),
    ("from maos.skills import invoker\ndef h(mod):\n    return mod.registry\nR = h(invoker)\n", 4),
    ("from maos.skills import invoker\nR = [invoker][0].registry\n", 2),
    ("import maos.kb\n_m = maos.kb\nP = _m.plan_advice\n", 2),
    ("import maos.kb as kb\n_m = kb\nP = getattr(_m, 'plan_advice')\n", 2),
    ("from maos.kb import retriever\nX = retriever\n", 2),
    ("def f():\n    from maos.kb import retriever as r\n    return r\n", 3),
    ("from maos.domain.cs import types\nX = types\n", 2),              # 本域模块同理
    ("import os\n_o = os\n_o.system('echo')\n", 2),                  # 标准库的口子同理
    ("import sys\n_s = sys\nm = _s.modules\n", 2),
])
def test_module_objects_used_as_values_are_caught_t175(tmp_path, src, line):
    """复核 L3-2：import 来的模块对象只许紧接着取属性；换名 / 传参 / 放进容器即判。"""
    v = _violations_t170(tmp_path, {"maos/domain/cs/mv.py": src})
    hits = [x for x in v if x.startswith(f"maos/domain/cs/mv.py:{line}: import: ")]
    assert hits and any("被当值用" in x for x in hits), v


def test_direct_attribute_access_on_the_same_module_stays_as_before_t175(tmp_path):
    """对照：同一个攻击写成直接取属性，p12 起就按真实出处判红；取白名单模块的成员不报。"""
    direct = _violations_t170(tmp_path / "a", {"maos/domain/cs/d.py": (
        "import maos.skills.invoker as inv\n"
        "def open_ticket(payload, ctx):\n"
        "    return inv.registry.get('refund.intake')().run(payload, ctx)\n")})
    assert any("实为" in x for x in direct), direct
    ok = _violations_t170(tmp_path / "b", {"maos/domain/cs/ok.py": (
        "import os\nimport maos.kb\nfrom maos.skills import invoker\n"
        "X = (maos.kb.tokenize, invoker.SkillInvoker, os.path.join('a', 'b'), os.environ.get('X'))\n")})
    assert ok == [], ok


@pytest.mark.parametrize("src,line", [
    ("import inspect\nimport maos.kb as kb\nP = dict(inspect.getmembers(kb))['plan_advice']\n", 3),
    ("import operator\nfrom maos.skills import invoker\nR = operator.attrgetter('registry')(invoker)\n", 3),
    ("import maos.kb as kb\nP = kb.__getattribute__('plan_advice')\n", 2),
    ("from operator import methodcaller\n", 1),
    ("import inspect\ndef f(o):\n    return inspect.getattr_static(o, 'x')\n", 3),
    ("import inspect\ndef f(o):\n    return inspect.getmembers_static(o)\n", 3),
])
def test_other_reflection_entry_points_are_caught_t175(tmp_path, src, line):
    """复核 L3-2：attrgetter / methodcaller / getmembers / __getattribute__ 出现即判。"""
    v = _violations_t170(tmp_path, {"maos/domain/cs/refl.py": src})
    assert line in _lines_t170(v, "maos/domain/cs/refl.py"), v


def test_first_party_tops_cover_the_repo_layout_t175():
    """一方代码的顶层名表：含 run / scripts / client，不含 maos 与标准库名；判据按段不按前缀。"""
    import sys

    assert {"run", "scripts", "client"} <= FIRST_PARTY_TOPS_T175
    assert "maos" not in FIRST_PARTY_TOPS_T175
    assert not FIRST_PARTY_TOPS_T175 & set(sys.stdlib_module_names)
    for name in ("run", "run.main", "scripts.run_ingress.cmd_simulate", "client.preflight"):
        assert first_party_violation_t175(name) is not None, name
    for name in ("runpy_x", "json", "os.path", "maos.kb", "scriptsx", "clients"):
        assert first_party_violation_t175(name) is None, name


@pytest.mark.parametrize("src,line", [
    # 复核 L3-3 (a)：以内部人的名义把 /refund 喂进 router
    ("import types\nfrom scripts import run_ingress\n"
     "def relay(text, ledger):\n"
     "    args = types.SimpleNamespace(simulate=[text], sender='ops-lead', ledger=ledger, photo=None)\n"
     "    return run_ingress.cmd_simulate(args)\n", 2),
    # (b)：跑整条退款 case
    ("from scripts import run_case\ndef go(argv):\n    return run_case.main(argv)\n", 1),
    # 复核 L2-6：run.py / scripts / client
    ("import run\ndef go():\n    return run.main()\n", 1),
    ("from run import main\n", 1),
    ("from scripts.run_ingress import main\n", 1),
    ("import scripts\n", 1),
    ("import client.preflight\n", 1),
    ("def f():\n    from scripts import run_case\n    return run_case.run_file\n", 2),
])
def test_first_party_code_outside_maos_is_caught_t175(tmp_path, src, line):
    """复核 L2-6 / L3-3：仓库里 maos 以外的一方代码不按「真实出处」放行，名字那一关就拦。"""
    v = _violations_t170(tmp_path, {"maos/domain/cs/fp.py": src})
    hits = [x for x in v if x.startswith(f"maos/domain/cs/fp.py:{line}: import: ")]
    assert hits and any("一方代码" in x for x in hits), v


@pytest.mark.parametrize("files,rel,line,kind", [
    # 复核 L3-3 (c)：改 sys.path 之后用非 maos 的名字加载 maos 的源码
    ({"maos/domain/cs/sp.py": "import os, sys\n"
      "sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'skills', 'builtin', 'refund'))\n"
      "import payment_execute\n"}, "maos/domain/cs/sp.py", 2, "import"),
    ({"maos/domain/cs/sp.py": "from sys import path\npath.append('x')\n"}, "maos/domain/cs/sp.py", 1, "import"),
    ({"maos/domain/cs/sp.py": "import site\nsite.addsitedir('x')\n"}, "maos/domain/cs/sp.py", 1, "import"),
    # (d)：cs/__init__ 往 __path__ 里追加别的域的目录
    ({"maos/domain/cs/__init__.py": "import os\n"
      "__path__.append(os.path.join(os.path.dirname(__file__), '..', 'refund'))\n",
      "maos/domain/cs/x.py": "from maos.domain.cs.guard import update_biz_status\n"},
     "maos/domain/cs/__init__.py", 2, "escape"),
])
def test_search_path_holes_are_caught_t175(tmp_path, files, rel, line, kind):
    v = _violations_t170(tmp_path, files)
    assert any(x.startswith(f"{rel}:{line}: {kind}: ") for x in v), v


# ---------------------------------------------------------------------------
# p13 修复轮二（T175 复核 L2-1 / L3R2-2 / L3R2-3）
# ---------------------------------------------------------------------------
_RELAY_T175 = "maos.domain.cs.zz_relay_t175"


@pytest.fixture
def cs_relay_t175(monkeypatch):
    """本域里一个把 os / logging / maos.kb / 本域模块绑在自己名字空间里的模块（真实的 desk / scripts
    今天就是这样：desk 有 os、claims，scripts 有 kb、logging）。合成在 sys.modules 里，不依赖
    T174 以后怎么改 desk.py。"""
    import logging as _logging
    import os as _os
    import sys as _sys

    import maos.kb as _kb
    from maos.domain.cs import claims as _claims

    relay = types.ModuleType(_RELAY_T175)
    relay.os, relay.logging, relay.kb, relay.claims, relay.OK = _os, _logging, _kb, _claims, 1
    monkeypatch.setitem(_sys.modules, _RELAY_T175, relay)
    _resolve_t170.cache_clear()
    yield relay
    _resolve_t170.cache_clear()


@pytest.mark.parametrize("src,line", [
    # 复核 L2-1 原样：直写 ``from maos import kb; kb.plan_advice`` 是红的，星号绕一手不能变绿
    (f"from {_RELAY_T175} import *\nP = kb.plan_advice\n", 2),
    (f"from {_RELAY_T175} import *\nos.system('echo pwned')\n", 2),
    (f"from {_RELAY_T175} import *\nout = os.popen('id').read()\n", 2),
    (f"from {_RELAY_T175} import *\nm = logging.sys.modules\n", 2),
    (f"from {_RELAY_T175} import *\nx = os\n", 2),                  # 模块对象当值用
    (f"from {_RELAY_T175} import *\nc = claims\n", 2),
    ("from maos.domain.cs.zz_missing_t175 import *\n", 1),         # 绑了什么无从知道：失败即关
])
def test_names_bound_by_star_import_are_checked_t175(tmp_path, cs_relay_t175, src, line):
    v = _violations_t170(tmp_path, {"maos/domain/cs/star2.py": src})
    assert any(x.startswith(f"maos/domain/cs/star2.py:{line}: import: ") for x in v), v


def test_star_bound_names_used_normally_stay_green_t175(tmp_path, cs_relay_t175):
    v = _violations_t170(tmp_path, {"maos/domain/cs/star3.py": (
        f"from {_RELAY_T175} import *\n"
        "X = (OK, claims.check_reply, kb.tokenize, os.path.join('a', 'b'), os.environ.get('K'))\n"
        "LOG = logging.getLogger('maos.cs')\n")})
    assert v == [], "\n".join(v)


@pytest.mark.parametrize("src,line", [
    # 复核 L3R2-2 (b)：内建函数的绑定对象就是 builtins 模块，再按字符串键取 exec / eval
    ("def go(code):\n    return len.__self__.__dict__['exec'](code)\n", 2),
    ("def go(code):\n    return getattr(len.__self__, 'exec')(code)\n", 2),
    ("X = print.__self__.__dict__['eval']('1+1')\n", 1),
    ("B = len.__self__\n", 1),                                       # dunder self 本身也判（不靠键）
    ("def go(f):\n    return f.__self__\n", 2),
    ("def go(ns):\n    return ns['exec']\n", 2),                     # 键本身也判（不靠 __self__）
    ("def go(ns):\n    return ns.get('eval')\n", 2),
    ("import operator\ndef go(ns):\n    return operator.itemgetter('exec')(ns)\n", 3),
    ("X = b'exec'\n", 1),
    # (c)：inspect.getmodule 交出模块对象
    ("import inspect\ndef go():\n    s = inspect.getmodule(inspect.getmodule).__dict__['sys']\n"
     "    return s.modules\n", 3),
    # (a)：os.* 口子的真身 posix、pty、asyncio 子进程、进程池
    ("import posix\ndef go(cmd):\n    return posix.system(cmd)\n", 3),
    ("from posix import system\n", 1),
    ("import posix\ndef go(a):\n    return posix.posix_spawn(a[0], a, {})\n", 3),
    ("from posix import *\n", 1),
    ("import pty\ndef go(argv):\n    return pty.spawn(argv)\n", 1),
    ("import _posixsubprocess\n", 1),
    ("import asyncio\nasync def go(*argv):\n    return await asyncio.create_subprocess_exec(*argv)\n", 3),
    ("import asyncio.subprocess\n", 1),
    ("import asyncio\nasync def go(cmd):\n    loop = asyncio.get_running_loop()\n"
     "    return await loop.subprocess_shell(None, cmd)\n", 4),
    ("from concurrent.futures import ProcessPoolExecutor\nP = ProcessPoolExecutor\n", 1),
    ("import concurrent.futures\nP = concurrent.futures.ProcessPoolExecutor\n", 2),
])
def test_more_process_and_builtins_entry_points_are_caught_t175(tmp_path, src, line):
    v = _violations_t170(tmp_path, {"maos/domain/cs/proc.py": src})
    assert line in _lines_t170(v, "maos/domain/cs/proc.py"), v


def test_everyday_os_asyncio_inspect_and_path_segments_stay_green_t175(tmp_path):
    """对照：posix 只按起进程的那几个名字判；"eval" 当路径段（BinOp 操作数）不是键也不是参数。"""
    v = _violations_t170(tmp_path, {"maos/domain/cs/ok2.py": (
        "import asyncio\nimport inspect\nimport os\nimport pathlib\n"
        "HERE = pathlib.Path(__file__).resolve().parents[3] / 'scenarios' / 'cs' / 'eval' / 'x.json'\n"
        "PID = os.getpid()\n"
        "def sig(f):\n    return inspect.signature(f)\n"
        "async def nap():\n    await asyncio.sleep(0)\n"
        "def pick(d):\n    return d['route'], d.get('lang')\n")})
    assert v == [], "\n".join(v)


def test_entry_script_dirs_are_found_t175(tmp_path):
    """放着 ``if __name__ == "__main__":`` 的目录（含 maos 里的）都算入口目录；点开头的目录跳过。"""
    files = {
        "tools/entry.py": "def main():\n    pass\n\n\nif __name__ == '__main__':\n    main()\n",
        "tools/helper.py": "X = 1\n",
        "tools/pkgdir/__init__.py": "",
        "lib/plain.py": "X = 1\n",
        "maos/sub/runner.py": 'if __name__ == "__main__":\n    pass\n',
        "maos/sub/sibling.py": "",
        ".hidden/evil.py": "if __name__ == '__main__':\n    pass\n",
    }
    for rel, src in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src, encoding="utf-8")
    assert _entry_dirs_t175(tmp_path) == [tmp_path / "maos" / "sub", tmp_path / "tools"]
    assert _entry_dir_names_t175(tmp_path) == {"entry", "helper", "pkgdir", "runner", "sibling"}


def test_entry_dir_names_of_the_repo_are_first_party_t175():
    """复核 L3R2-3：生产入口 ``python scripts/run_ingress.py`` 让 scripts/ 进 sys.path[0]，scripts/*.py 的
    每个主名都能当顶层名 import；client/、hiclaw/ 与 maos/ 本身（maos/main.py）同理。"""
    import sys

    scripts = {p.stem for p in (ROOT_T170 / "scripts").glob("*.py")} - set(sys.stdlib_module_names)
    assert scripts and scripts <= FIRST_PARTY_TOPS_T175, sorted(scripts - FIRST_PARTY_TOPS_T175)
    assert {"run_ingress", "run_requests", "verify", "preflight", "room_agent", "runtime",
            "flows"} <= FIRST_PARTY_TOPS_T175
    assert not FIRST_PARTY_TOPS_T175 & set(sys.stdlib_module_names)
    assert "maos" not in FIRST_PARTY_TOPS_T175


@pytest.mark.parametrize("src,line", [
    # 复核 L3R2-3 原样
    ("import types\nimport run_ingress\n"
     "def relay(text, ledger):\n"
     "    args = types.SimpleNamespace(simulate=[text], sender='ops-lead', ledger=ledger, photo=None)\n"
     "    return run_ingress.cmd_simulate(args)\n", 2),
    ("import run_requests\ndef go(a):\n    return run_requests.main(a)\n", 1),
    ("from run_case import main\n", 1),
    ("import verify\n", 1),
    ("import room_agent\n", 1),                       # hiclaw/ 下的入口
    ("import preflight\n", 1),                        # client/ 下的入口
    ("import runtime.gate\n", 1),                     # python maos/main.py：maos/ 进 sys.path[0]
    ("from flows import scenario_7\n", 1),
])
def test_bare_names_from_entry_script_dirs_are_caught_t175(tmp_path, src, line):
    v = _violations_t170(tmp_path, {"maos/domain/cs/bare.py": src})
    hits = [x for x in v if x.startswith(f"maos/domain/cs/bare.py:{line}: import: ")]
    assert hits and any("一方代码" in x for x in hits), v
