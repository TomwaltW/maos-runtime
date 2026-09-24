"""T170 · 外部渠道零授权的静态守卫（契约 §2.3，红线 R1），全仓生效。

扫描范围：``maos/domain/cs/**`` 与 ``maos/skills/builtin/cs/**``（后者 T169 才建，不在时跳过
该目录；但 ``maos/domain/cs`` 必须扫到 ``types.py`` —— 防空转：路径写错时扫到零个文件、
守卫恒绿，而且没有任何症状）。

判据（契约 §2.3 逐条，外加 R1「不按名字调、不借别人的 identity」落成机器判据的部分，
超出契约原文的几处见 DECISIONS task-t170）：

* **禁 import 前缀** —— ``ast.walk`` 全树（函数体内的 import 也算），相对 import 按包路径
  解析成绝对名再判；``from A import B`` 按 ``A.B`` 判（``from maos.domain.refund import
  projection`` 是 projection、放行；``from maos.domain.refund import objects`` 拦）。
  另三种拿到同一个模块的写法按同一套规则判：
  - ``from <祖先包> import *``（``maos`` / ``maos.domain`` / ``maos.skills.builtin`` …… 的
    star import 会把禁区子模块一并绑进来）；
  - 属性链：``from maos.skills import builtin`` 之后的 ``builtin.refund.payment_execute``、
    ``import maos.kb`` 之后的 ``maos.runtime.gate``，按还原出的点分名判；
  - 动态 import：``import_module`` / ``find_spec`` / ``__import__`` / ``resolve_name`` /
    ``run_module`` 的字面量参数（位置参数与关键字参数、``package`` / ``level`` 的相对名、
    ``fromlist`` 每一项），``sys.modules[...]`` 的字面量下标；``exec`` / ``eval`` /
    内建 ``compile`` 在扫描范围里一律判。
* **允许清单** —— 契约列出来免得守卫写过头的那几项，逐条断言不误报。
* **禁字符串常量** —— 任何位置的 ``ast.Constant`` 与之**相等**即判；包着禁工具的 skill 名
  （``rtv.ship`` 等）同判，并由注册表反推钉住（新 skill 包了禁工具而这里没列，当场红）。
* **禁调用名** —— 调用（属性调用、裸名调用）与**引用**（``fn = gate.decide``、
  ``partial(gate.decide)``、``methodcaller("decide")``、``getattr(x, "decide")``）都判。
* **不借别人的 identity** —— 扫描范围里不许出现 ``AGENT_POOL``（名字、属性、import 名、
  字符串），不许 import ``maos.capability``（``identities()`` 按角色给出全部身份）；
  造 identity 时 ``allowed_skills`` 里的字面量只许是 ``cs.*``、``allowed_tools`` 不许有字面量。
* **文件层** —— 扫描目录下出现符号链接（rglob 不跟进、import 会跟进）或没有源码的可导入
  文件（包目录里、不在 ``__pycache__`` 下的 ``.pyc``、扩展模块、``.pth``）即判。

扫描逻辑是可注入根目录的纯函数 :func:`scan_cs_guard_t170`；反向验证全部在 tmp 目录里
造违规文件喂给它，不落仓库。守卫只认字面量：拼接出来的字符串、用变量传的名字判不到
（BACKLOG task-t170）。
"""

from __future__ import annotations

import ast
import importlib.machinery
import os
import pathlib
import textwrap

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
#: 前台自己的 skill 前缀：造 identity 时 allowed_skills 的字面量只许是它。
CS_SKILL_PREFIX_T170 = "cs."

#: star import 会把禁区子模块一并绑进来的祖先包（禁前缀的真前缀 + 按段放行的几个父包）。
STAR_FORBIDDEN_BASES_T170 = frozenset(
    {".".join(p.split(".")[:i]) for p in FORBIDDEN_PREFIXES_T170
     for i in range(1, p.count(".") + 1)}
    | {"maos", "maos.skills", "maos.skills.builtin", "maos.agents", "maos.domain",
       "maos.domain.refund"})

#: 动态 import 的入口 -> (模块名参数位置, 关键字)。
DYNAMIC_IMPORTERS_T170 = {"import_module": (0, "name"), "find_spec": (0, "name"),
                          "__import__": (0, "name"), "resolve_name": (0, "name"),
                          "run_module": (0, "mod_name")}
#: 扫描范围里一律不许的动态执行。``compile`` 只认内建（``re.compile`` 是属性调用，不算）。
DYNAMIC_EXEC_T170 = frozenset({"exec", "eval", "compile"})

#: 没有源码也能被 import 的文件（不在 __pycache__ 下时）。
SOURCELESS_SUFFIXES_T170 = tuple(importlib.machinery.BYTECODE_SUFFIXES
                                 + importlib.machinery.EXTENSION_SUFFIXES
                                 + [".pyd", ".pth"])


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


def star_violation_t170(base: str) -> str | None:
    """``from <base> import *`` 触犯哪条禁令：base 本身禁，或是禁区的祖先包。"""
    why = import_violation_t170(base)
    if why:
        return why
    if base in STAR_FORBIDDEN_BASES_T170:
        return f"star import 祖先包 {base}（会把禁区子模块一并绑进来）"
    return None


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


def _str_t170(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _arg_t170(call: ast.Call, pos: int, kw: str) -> ast.expr | None:
    if len(call.args) > pos:
        return call.args[pos]
    for keyword in call.keywords:
        if keyword.arg == kw:
            return keyword.value
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


def _dynamic_targets_t170(call: ast.Call, name: str, module_name: str,
                          is_pkg: bool) -> tuple[list[str], str | None]:
    """动态 import 调用的目标模块名（绝对）；第二项非空 = 相对名解析不了（按违例算）。"""
    pos, kw = DYNAMIC_IMPORTERS_T170[name]
    target = _str_t170(_arg_t170(call, pos, kw))
    if target is None:
        return [], None                                  # 非字面量：判不到（BACKLOG）
    own_pkg = module_name if is_pkg else module_name.rpartition(".")[0]
    if name == "resolve_name":
        return [target.replace(":", ".")], None
    if name in ("import_module", "find_spec") and target.startswith("."):
        level = len(target) - len(target.lstrip("."))
        pkg_node = _arg_t170(call, 1, "package")
        pkg = _str_t170(pkg_node)
        if pkg is None and isinstance(pkg_node, ast.Name) and pkg_node.id == "__package__":
            pkg = own_pkg
        if pkg is None:
            return [], f"{name}({target!r}) 的 package 不是字面量，相对名解析不了"
        base = _resolve_from_t170(pkg, True, level, target[level:] or None)
        return ([base], None) if base else ([], f"{name}({target!r}) 越过顶层")
    if name == "__import__":
        level_node = _arg_t170(call, 4, "level")
        level = 0
        if level_node is not None:
            if not (isinstance(level_node, ast.Constant) and isinstance(level_node.value, int)):
                return [], "__import__ 的 level 不是字面量"
            level = level_node.value
        base = target
        if level > 0:
            base = _resolve_from_t170(module_name, is_pkg, level, target or None)
            if base is None:
                return [], f"__import__({target!r}, level={level}) 越过顶层"
        out = [base]
        fromlist = _arg_t170(call, 3, "fromlist")
        if isinstance(fromlist, (ast.List, ast.Tuple, ast.Set)):
            for elt in fromlist.elts:
                item = _str_t170(elt)
                if item is not None:
                    out.append(base if item == "*" else f"{base}.{item}")
        return out, None
    return [target], None


def _scan_file_t170(root: pathlib.Path, path: pathlib.Path) -> list[str]:
    rel = path.relative_to(root).as_posix()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
    except (SyntaxError, UnicodeDecodeError) as exc:
        return [f"{rel}:0: 解析失败: {exc}"]
    module_name, is_pkg = _module_of_t170(root, path)
    bindings = _bindings_t170(tree, module_name, is_pkg)
    call_funcs = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    out: list[str] = []

    def bad(node: ast.AST, kind: str, detail: str) -> None:
        out.append(f"{rel}:{getattr(node, 'lineno', 0)}: {kind}: {detail}")

    def check_import(node: ast.AST, target: str, how: str) -> None:
        why = import_violation_t170(target)
        if why:
            bad(node, "import", f"{how}{target} —— {why}")

    def check_identity_literals(node: ast.AST, field: str, value: ast.AST) -> None:
        for sub in ast.walk(value):
            lit = _str_t170(sub)
            if lit is None:
                continue
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
                check_import(node, f"{base}.{alias.name}" if base else alias.name, "")
                if alias.name in FORBIDDEN_CALLS_T170:
                    bad(node, "call", f"import 名 {alias.name}")
                if alias.name in IDENTITY_REGISTRIES_T170:
                    bad(node, "identity", f"import 名 {alias.name}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value in FORBIDDEN_CONSTANTS_T170:
                bad(node, "constant", repr(node.value))
            if node.value in FORBIDDEN_CALLS_T170:
                bad(node, "call", f"字符串 {node.value!r}")
            if node.value in IDENTITY_REGISTRIES_T170:
                bad(node, "identity", f"字符串 {node.value!r}")
        elif isinstance(node, ast.Name):
            if node.id in FORBIDDEN_CALLS_T170 and id(node) not in call_funcs:
                bad(node, "call", f"引用 {node.id}")
            if node.id in IDENTITY_REGISTRIES_T170:
                bad(node, "identity", node.id)
        elif isinstance(node, ast.Attribute):
            if node.attr in FORBIDDEN_CALLS_T170 and id(node) not in call_funcs:
                bad(node, "call", f"引用 .{node.attr}")
            if node.attr in IDENTITY_REGISTRIES_T170:
                bad(node, "identity", f".{node.attr}")
            full = _dotted_t170(node, bindings)
            inner = _dotted_t170(node.value, bindings)
            if full and not (inner and import_violation_t170(inner)):
                check_import(node, full, "属性链 ")
        elif isinstance(node, ast.Subscript):
            owner = _dotted_t170(node.value, bindings)
            key = _str_t170(node.slice)
            if owner == "sys.modules" and key is not None:
                check_import(node, key, "sys.modules[…] ")
        if isinstance(node, ast.Call):
            name = _call_name_t170(node.func)
            if name in FORBIDDEN_CALLS_T170:
                bad(node, "call", name)
            if name in DYNAMIC_IMPORTERS_T170:
                targets, err = _dynamic_targets_t170(node, name, module_name, is_pkg)
                if err:
                    bad(node, "import", err)
                for target in targets:
                    check_import(node, target, f"{name}(…) ")
            if isinstance(node.func, ast.Attribute) and node.func.attr in ("get", "pop", "setdefault"):
                if _dotted_t170(node.func.value, bindings) == "sys.modules":
                    key = _str_t170(_arg_t170(node, 0, "key"))
                    if key is not None:
                        check_import(node, key, "sys.modules.get(…) ")
            if name in DYNAMIC_EXEC_T170 and (
                    isinstance(node.func, ast.Name)
                    or _dotted_t170(node.func, bindings) in {f"builtins.{name}"}):
                bad(node, "import", f"动态执行 {name}(…) —— 扫描范围里一律不许")
            if name == "AgentIdentity" and len(node.args) > 3:
                check_identity_literals(node, "allowed_skills", node.args[3])
                if len(node.args) > 4:
                    check_identity_literals(node, "allowed_tools", node.args[4])
            for keyword in node.keywords:
                if keyword.arg in ("allowed_skills", "allowed_tools"):
                    check_identity_literals(node, keyword.arg, keyword.value)
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
    hits = [x for x in v if x.startswith("maos/domain/cs/dyn.py:")]
    assert len(hits) == 2, v


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
                import importlib
                return (importlib.import_module("maos.domain.cs.claims"),
                        importlib.import_module(".claims", __package__),
                        importlib.import_module(".scripts", "maos.domain.cs"),
                        importlib.import_module(name="maos.kb.retriever"),
                        __import__("maos.domain.cs", fromlist=["claims"]), r)
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
        "maos/domain/cs/dyn2.py": "import importlib, importlib.util, sys, pkgutil, runpy, builtins\n"
                                  f"def f(pkg, lv, src, name):\n    return {src}\n",
    })
    _one_t170(v, "maos/domain/cs/dyn2.py", "import")


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
