"""T170 · 外部渠道零授权的静态守卫（契约 §2.3，红线 R1），全仓生效。

扫描范围：``maos/domain/cs/**`` 与 ``maos/skills/builtin/cs/**``（后者 T169 才建，不在时跳过
该目录；但 ``maos/domain/cs`` 必须扫到 ``types.py`` —— 防空转：路径写错时扫到零个文件、
守卫恒绿，而且没有任何症状）。

四类判据：

* **禁 import 前缀** —— ``ast.walk`` 全树（函数体内的 import 也算），相对 import 按包路径
  解析成绝对名再判；``from A import B`` 按 ``A.B`` 判（``from maos.domain.refund import
  projection`` 是 projection、放行；``from maos.domain.refund import objects`` 拦）。
  ``importlib.import_module("…")`` / ``__import__("…")`` 带字面量参数的，按同一套规则判。
* **允许清单** —— 契约列出来免得守卫写过头的那几项，逐条断言不误报。
* **禁字符串常量** —— 任何位置的 ``ast.Constant`` 与之**相等**即判。
* **禁调用名** —— 属性调用（``x.decide()``）与裸名调用（``decide()``）；
  ``getattr(x, "decide")`` 按名字取也算。

扫描逻辑是可注入根目录的纯函数 :func:`scan_cs_guard_t170`；反向验证全部在 tmp 目录里
造违规文件喂给它，不落仓库。
"""

from __future__ import annotations

import ast
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

#: 禁 import 前缀（按点分段匹配，不按字符串前缀：``maos.toolsx`` 不是 ``maos.tools``）。
FORBIDDEN_PREFIXES_T170: tuple[str, ...] = (
    "maos.runtime", "maos.core.control_plane", "maos.flows", "hiclaw",
    "maos.ingress.router", "maos.ingress.outcome_commands", "maos.ingress.chat",
    "maos.nlu", "maos.roundtable", "maos.tools",
)

#: maos.domain 下允许的一级名（共享底座与本域）；另有 refund.projection 一处。
DOMAIN_ALLOWED_T170 = frozenset({"cs", "_dbport", "_schema_util"})

FORBIDDEN_CONSTANTS_T170 = frozenset({
    "payment.execute", "payment.observe", "refund.compensate", "refund.compensation_close",
    "gateway.refund", "gateway.query", "order.query", "claim.pay", "claim.compensate",
    "ap.execute", "ap.compensate", "rtv.compensate", "investigation.compensate",
    "bank.pay", "payer.submit", "supplier.rma_submit", "carrier.ship", "clearing.cancel",
})

FORBIDDEN_CALLS_T170 = frozenset({
    "decide", "human_decision", "record_approval", "run_payload", "handle_execute",
    "handle_gate_decision", "_record_gate_approval", "invoke_tool",
})

#: 动态 import 的入口：带字面量模块名时按 import 规则判。
DYNAMIC_IMPORTERS_T170 = frozenset({"import_module", "__import__"})


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


def _scan_file_t170(root: pathlib.Path, path: pathlib.Path) -> list[str]:
    rel = path.relative_to(root).as_posix()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
    except (SyntaxError, UnicodeDecodeError) as exc:
        return [f"{rel}:0: 解析失败: {exc}"]
    module_name, is_pkg = _module_of_t170(root, path)
    out: list[str] = []

    def bad(node: ast.AST, kind: str, detail: str) -> None:
        out.append(f"{rel}:{getattr(node, 'lineno', 0)}: {kind}: {detail}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                why = import_violation_t170(alias.name)
                if why:
                    bad(node, "import", f"{alias.name} —— {why}")
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_from_t170(module_name, is_pkg, node.level, node.module)
            if base is None:
                bad(node, "import", f"相对 import 越过顶层（level={node.level}）")
                continue
            for alias in node.names:
                target = base if alias.name == "*" else f"{base}.{alias.name}"
                why = import_violation_t170(target)
                if why:
                    bad(node, "import", f"{target} —— {why}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value in FORBIDDEN_CONSTANTS_T170:
                bad(node, "constant", repr(node.value))
        elif isinstance(node, ast.Call):
            name = _call_name_t170(node.func)
            if name in FORBIDDEN_CALLS_T170:
                bad(node, "call", name)
            first = node.args[0] if node.args else None
            if (name in DYNAMIC_IMPORTERS_T170 and isinstance(first, ast.Constant)
                    and isinstance(first.value, str)):
                why = import_violation_t170(first.value)
                if why:
                    bad(node, "import", f"{name}({first.value!r}) —— {why}")
            if name == "getattr" and len(node.args) >= 2:
                attr = node.args[1]
                if (isinstance(attr, ast.Constant) and isinstance(attr.value, str)
                        and attr.value in FORBIDDEN_CALLS_T170):
                    bad(node, "call", f"getattr(…, {attr.value!r})")
    return out


def scan_cs_guard_t170(root: pathlib.Path) -> tuple[list[pathlib.Path], list[str]]:
    """扫 ``root`` 下的两片目录，返回（扫到的文件，违例）。目录不在就跳过。"""
    root = pathlib.Path(root)
    files: list[pathlib.Path] = []
    for rel in SCAN_DIRS_T170:
        d = root.joinpath(*rel)
        if d.is_dir():
            files.extend(sorted(p for p in d.rglob("*.py") if "__pycache__" not in p.parts))
    violations: list[str] = []
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

            def lazy():
                from maos.kb import retriever as r
                import importlib
                return importlib.import_module("maos.domain.cs.claims"), r
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
