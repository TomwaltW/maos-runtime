"""跨平台一致性守卫 —— `maos/tools/commerce/` 下**全部**适配器的共同纪律。

## 这份测试是什么

p11 六轨并行，给七个平台各写一个 `CommerceAdapter` 子类。最容易出的问题不是某个平台
写错，而是七个平台**各自偏一点**，最后没人说得清「电商接入到底保证了什么」。
本文件是整批的收口：**动态发现**本包里的全部适配器，逐条验共同纪律。

它**不硬编码平台清单**（契约 §F.1）：发现逻辑扫的是包目录，不是一张名单。于是单独
跑时就绿，合并后自动覆盖全部平台，与谁先合谁后合无关。

## 八条判据 + 两条发现本身的判据

1. 每个子类声明了非空的 `platform` 与非空的 `status_rules`
2. 每条规则都是 `StatusRule`，且 `target` 在 `ALL_ORDER_STATUSES` 四态里（铁律 9）
3. 每条 `source` 是实质出处，不是「见官方文档」这类空话
4. 没有子类（含中间类）覆盖 `query()` / `__repr__` —— `__init_subclass__`
   只拦得住类定义时的覆盖，拦不住事后 `setattr`
5. `platform` 名两两不重复 —— 重名会让注册表悄悄互相顶掉
6. 平台模块不 import HTTP 客户端库 —— 一律走 `self.transport.request(...)`
7. 平台模块不 import `maos.domain*` / `maos.contracts*`（铁律 8）
8. 平台模块在**导入期**不读凭据 / 环境变量 —— 本文件会动态 import 全部模块，
   导入期读凭据会让发现当场炸

9. 真包里每个模块都能干净导入（逐模块报，不整体崩）
10. 发现结果不含包外定义的子类（测试文件里的参照类、用例里现造的类）

判据 1–8 **每条一个独立的测试函数**：挤在一起时第一条挂了后面几条就不跑了，
而读报错的人需要一眼看出是哪条纪律被破了。判据 1–8 只看发现到的类与源文件，
**不看导入错误** —— 那由判据 9 独占，否则一个模块导入失败会连带红一片。

## 判据 1–8 在任务 worktree 里对真包是**空跑**的

本文件写成时（task-t166），`maos/tools/commerce/` 里只有基座三层，一个平台都没有，
发现结果为空，判据 1–8 在空集合上断言成立。**这是有意的**：不许加
「至少发现一个」的下限，也不许 skip —— 包里本来就可能一个平台都没有。

所以它们的牙不由真对象证明，而由两处证明：

- 本文件下半部的合成用例（`test_check_flags_*` 与
  `test_discovery_reports_import_errors_per_module`）：每条检查函数都喂过一个
  **必须判出**的构造与一个**必须放过**的构造；
- 任务派单要求的反向验证：往包里放一次性探针（撞名 / 导入期读凭据），
  确认恰好对应的那一条变红。

真对象上的证明归整合期：六轨合流后，整合轨另加「发现数 == 7」的精确断言。
"""

from __future__ import annotations

import ast
import collections
import importlib
import pathlib
import pkgutil
import re
import sys
import textwrap
import uuid

import pytest

import maos.tools.commerce as commerce
from maos.tools.commerce import CommerceAdapter, FakeTransport, StatusRule, read_credential
from maos.tools.order import ALL_ORDER_STATUSES

#: 基座三层由 T160 自己的测试管，不归本测试的 AST 扫描（这是基座清单，不是平台清单）。
INFRA = frozenset({"base", "mapping", "transport"})

#: AST 判据扫描时，相对 import（`from . import x`）按这个包名解析。
_PACKAGE = "maos.tools.commerce"


# ---------------------------------------------------------------------------
# 发现
# ---------------------------------------------------------------------------

def discover_adapters(path=None, prefix="maos.tools.commerce."):
    """返回 (adapters, errors)。

    adapters：本包里定义的全部 CommerceAdapter 子类，按 (__module__, __qualname__) 排序。
    errors：导入失败的模块，每项 (模块全名, f"{type(exc).__name__}: {exc}")。
    **逐模块报，不整体崩**：一个平台模块导入期炸了，其余模块照常发现、其余判据照常跑。
    """
    path = list(path) if path is not None else list(commerce.__path__)
    errors = []
    for info in sorted(pkgutil.iter_modules(path), key=lambda i: i.name):
        name = prefix + info.name
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001
            errors.append((name, f"{type(exc).__name__}: {exc}"))
    found, stack = set(), list(CommerceAdapter.__subclasses__())
    while stack:
        cls = stack.pop()
        stack.extend(cls.__subclasses__())
        if cls.__module__.startswith(prefix) and cls.__module__ != prefix + "base":  # ← 过滤行
            found.add(cls)
    return sorted(found, key=lambda c: (c.__module__, c.__qualname__)), errors


def platform_module_files(path=None, prefix="maos.tools.commerce."):
    """平台模块的源文件：`(模块全名, 路径)` 的列表，基座三层（`INFRA`）除外。

    **只列文件，不 import** —— 判据 6–8 按源码文本扫，导入失败的模块更要扫。
    平台若写成子包，子包下的 `.py` 全部算进来。
    """
    path = list(path) if path is not None else list(commerce.__path__)
    files = []
    for info in sorted(pkgutil.iter_modules(path), key=lambda i: i.name):
        if info.name in INFRA:
            continue
        base = pathlib.Path(info.module_finder.path) / info.name
        if info.ispkg:
            files += [(prefix + info.name, p) for p in sorted(base.rglob("*.py"))]
        else:
            files.append((prefix + info.name, base.with_name(info.name + ".py")))
    return files


def _label(cls) -> str:
    return f"{cls.__module__}.{cls.__qualname__}"


# ---------------------------------------------------------------------------
# 判据 1–5：吃类的列表，返回违规清单（纯函数）
# ---------------------------------------------------------------------------

def check_platform_and_rules(adapters) -> list[str]:
    """判据 1：`platform` 非空、`status_rules` 非空。空表等于这个适配器不能用。"""
    out = []
    for cls in adapters:
        platform = getattr(cls, "platform", "")
        if not isinstance(platform, str) or not platform.strip():
            out.append(f"{_label(cls)} 没声明 platform（读到 {platform!r}）")
        if not getattr(cls, "status_rules", ()):
            out.append(f"{_label(cls)} 的 status_rules 为空")
    return out


def check_rule_targets(adapters) -> list[str]:
    """判据 2：每条规则都是 `StatusRule`，`target` 在四态内。

    `StatusRule.__post_init__` 已经拦了一道，这里再拦一道，是因为 frozen dataclass
    挡不住 `object.__new__` + `object.__setattr__` 这种绕过，也挡不住规则表里混进
    一个根本不是 `StatusRule` 的东西。
    """
    out = []
    for cls in adapters:
        for i, rule in enumerate(getattr(cls, "status_rules", ()) or ()):
            if not isinstance(rule, StatusRule):
                out.append(f"{_label(cls)} 规则表第 {i} 条不是 StatusRule：{rule!r}")
            elif rule.target not in ALL_ORDER_STATUSES:
                out.append(f"{_label(cls)} 规则表第 {i} 条 target={rule.target!r} "
                           f"不在四态 {ALL_ORDER_STATUSES} 里（铁律 9）")
    return out


#: 空话黑名单：命中即判出，**优先于**下面三条放行条件。
#: 按短语判而不是按单词判 —— 裸的 `docs` / `文档` 出现在真出处里很正常。
_VAGUE_SOURCE = re.compile(
    r"(?:参|详|请)?见(?:官方)?文档|参考(?:官方)?文档|待(?:补|核|查)"
    r"|\b(?:todo|tbd|fixme)\b"
    r"|\bsee\s+(?:the\s+)?(?:official\s+)?(?:docs?|documentation)\b",
    re.IGNORECASE,
)


def source_problem(platform, source) -> str | None:
    """判据 3 的核心：一条 `source` 算不算**实质出处**。算返回 None，不算返回原因。

    判出：空串；命中空话黑名单（「见官方文档」「TODO」「see docs」这类）。
    放过（黑名单没命中时，三条**任一**成立即可）：

    - 含 platform 名按 `_` / `-` 切开后的首段（大小写不敏感；首段至少 2 个字符）；
    - 含 `https://`；
    - 含 ≥ 2 个 ` / ` —— 「文档名 / 接口或资源 / 字段或枚举」这种三段式路径。

    **不拿「逐字含 platform 名」当唯一判据**：带下划线的平台名（形如 `acme_sp`）
    不会逐字出现在文档标题或 URL 里，那样判会让别轨合规的规则在整合期报假红。
    """
    text = source.strip() if isinstance(source, str) else ""
    if not text:
        return "source 为空"
    if _VAGUE_SOURCE.search(text):
        return f"source 是空话：{text!r}"
    head = re.split(r"[_\-]", str(platform or "").strip().lower())[0]
    if len(head) >= 2 and head in text.lower():
        return None
    if "https://" in text.lower():
        return None
    if text.count(" / ") >= 2:
        return None
    return (f"source 看不出出处：{text!r}（要含平台名、或 https:// 链接、"
            "或「文档 / 接口 / 字段」三段式路径）")


def check_rule_sources(adapters) -> list[str]:
    """判据 3：每条规则的 `source` 都是实质出处。核不到出处的规则不该存在。"""
    out = []
    for cls in adapters:
        platform = getattr(cls, "platform", "")
        for i, rule in enumerate(getattr(cls, "status_rules", ()) or ()):
            problem = source_problem(platform, getattr(rule, "source", ""))
            if problem:
                out.append(f"{_label(cls)} 规则表第 {i} 条：{problem}")
    return out


def check_no_query_or_repr_override(adapters) -> list[str]:
    """判据 4：MRO 上位于 `CommerceAdapter` 之前的类（含中间类、mixin）都不许定义
    `query` / `__repr__`。

    按 `__dict__` 判而不是按「定义时」判：`__init_subclass__` 只在 class 语句那一刻
    检查一次，事后 `setattr(cls, "query", ...)` 它看不见，这里看得见。
    """
    out = []
    for cls in adapters:
        for klass in cls.__mro__:
            if klass is CommerceAdapter:
                break
            for name in ("query", "__repr__"):
                if name in vars(klass):
                    via = "" if klass is cls else f"（经由 {_label(klass)}）"
                    out.append(f"{_label(cls)} 覆盖了 {name}{via}")
    return out


def check_unique_platforms(adapters) -> list[str]:
    """判据 5：`platform` 名两两不重复。空名由判据 1 管，这里不重复报。"""
    owners = collections.defaultdict(list)
    for cls in adapters:
        platform = getattr(cls, "platform", "")
        if isinstance(platform, str) and platform.strip():
            owners[platform.strip().lower()].append(_label(cls))
    return [f"platform {name!r} 被 {len(who)} 个类同时声明：{who}"
            for name, who in sorted(owners.items()) if len(who) > 1]


# ---------------------------------------------------------------------------
# 判据 6–8：吃源码文本，返回违规清单（纯函数）
# ---------------------------------------------------------------------------

#: 判据 6 的禁单。契约 §D.1 点名的是 requests / httpx；后四个同为绕开
#: `self.transport` 的出网口，一并禁掉。
FORBIDDEN_HTTP = ("requests", "httpx", "aiohttp", "urllib3", "urllib.request", "http.client")

#: 判据 7 的禁单：适配器不碰业务状态，也不碰冻结契约。
FORBIDDEN_LAYERS = ("maos.domain", "maos.contracts")


def _resolve_relative(module: str | None, level: int, package: str) -> str:
    """把 `from ..x import y` 的 `..x` 按所在包解析成绝对模块名。"""
    if not level:
        return module or ""
    parts = package.split(".")
    base = parts[: len(parts) - (level - 1)] if level - 1 < len(parts) else []
    return ".".join(base + ([module] if module else []))


def imported_modules(source: str, *, package: str = _PACKAGE) -> list[tuple[int, str]]:
    """源码里 import 到的全部模块名：`(行号, 点号全名)`。函数体内的 import 也算。

    `from a import b` 同时记 `a` 与 `a.b` 两条 —— 只记 `a` 会漏掉
    `from urllib import request`（那一行 import 到的正是 `urllib.request`）。
    """
    names = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names += [(node.lineno, alias.name) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            module = _resolve_relative(node.module, node.level, package)
            if module:
                names.append((node.lineno, module))
            names += [(node.lineno, f"{module}.{alias.name}" if module else alias.name)
                      for alias in node.names]
    return names


def _hits(source: str, banned: tuple[str, ...]) -> list[str]:
    return sorted({f"第 {line} 行 import 了 {name}"
                   for line, name in imported_modules(source)
                   if any(name == b or name.startswith(b + ".") for b in banned)})


def http_client_imports(source: str) -> list[str]:
    """判据 6：HTTP 客户端库的 import。`urllib.parse` / `json` 不算。"""
    return _hits(source, FORBIDDEN_HTTP)


def domain_or_contract_imports(source: str) -> list[str]:
    """判据 7：`maos.domain*` / `maos.contracts*` 的 import。"""
    return _hits(source, FORBIDDEN_LAYERS)


_CREDENTIAL_CALLS = frozenset({"read_credential", "getenv", "getenvb"})


def import_time_credential_reads(source: str) -> list[str]:
    """判据 8：**导入期**执行的代码里读凭据 / 环境变量的地方。

    「导入期」= 不在任何函数体（`def` / `async def` / `lambda`）里的代码。
    类体、装饰器、函数默认参数值都在导入期执行，要算进去；函数体不算。
    判出三种：调 `read_credential(...)`、调 `getenv(...)`（含 `os.getenv`）、
    碰 `os.environ` / `environ`（`.get`、下标、`in` 都是它）。
    """
    hits = []

    def flag(node) -> None:
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else (
                func.attr if isinstance(func, ast.Attribute) else "")
            if name in _CREDENTIAL_CALLS:
                hits.append(f"第 {node.lineno} 行导入期调用 {ast.unparse(func)}()")
        elif isinstance(node, ast.Attribute) and node.attr == "environ":
            hits.append(f"第 {node.lineno} 行导入期读 {ast.unparse(node)}")
        elif isinstance(node, ast.Name) and node.id == "environ":
            hits.append(f"第 {node.lineno} 行导入期读 environ")

    def visit(node) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            # 函数体不在导入期执行；装饰器与默认参数值在。
            for sub in getattr(node, "decorator_list", []):
                visit(sub)
            for sub in node.args.defaults + [d for d in node.args.kw_defaults if d]:
                visit(sub)
            return
        flag(node)
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(ast.parse(source))
    return hits


def scan_platform_modules(check, path=None, prefix="maos.tools.commerce.") -> list[str]:
    """拿一条源码判据扫全部平台模块。读不了 / 解析不了的文件**点名报出**，不静默跳过。"""
    out = []
    for name, file in platform_module_files(path, prefix):
        try:
            found = check(file.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError, ValueError) as exc:
            out.append(f"{name}（{file.name}）无法扫描：{type(exc).__name__}: {exc}")
            continue
        out += [f"{name}（{file.name}）{v}" for v in found]
    return out


def _fail_message(title: str, violations: list[str]) -> str:
    return title + "\n" + "\n".join(f"  - {v}" for v in violations)


# ---------------------------------------------------------------------------
# 真包：八条判据，每条一个测试
# ---------------------------------------------------------------------------

def _real_adapters():
    adapters, _errors = discover_adapters()   # 导入错误只归判据 9 管
    return adapters


def test_every_adapter_declares_platform_and_rules():
    violations = check_platform_and_rules(_real_adapters())
    assert not violations, _fail_message("适配器缺 platform / status_rules：", violations)


def test_every_rule_target_is_one_of_four_states():
    violations = check_rule_targets(_real_adapters())
    assert not violations, _fail_message("规则 target 越出四态：", violations)


def test_every_rule_source_is_substantive():
    violations = check_rule_sources(_real_adapters())
    assert not violations, _fail_message("规则 source 不是实质出处：", violations)


def test_no_adapter_overrides_query_or_repr():
    violations = check_no_query_or_repr_override(_real_adapters())
    assert not violations, _fail_message(
        "query() / __repr__ 是纪律的载体，不许覆盖：", violations)


def test_platform_names_are_unique():
    violations = check_unique_platforms(_real_adapters())
    assert not violations, _fail_message("platform 名撞了：", violations)


def test_platform_modules_do_not_import_http_clients():
    violations = scan_platform_modules(http_client_imports)
    assert not violations, _fail_message(
        "平台模块 import 了 HTTP 客户端库（HTTP 一律走 self.transport.request）：", violations)


def test_platform_modules_do_not_import_domain_or_contracts():
    violations = scan_platform_modules(domain_or_contract_imports)
    assert not violations, _fail_message(
        "平台模块 import 了 domain / contracts（适配器不碰业务状态，铁律 8）：", violations)


def test_platform_modules_read_no_credentials_at_import():
    violations = scan_platform_modules(import_time_credential_reads)
    assert not violations, _fail_message(
        "平台模块在导入期读凭据 / 环境变量（挪进 build_query_request / verifier）：",
        violations)


def test_every_commerce_module_imports_cleanly():
    _adapters, errors = discover_adapters()
    assert not errors, _fail_message(
        "这些 commerce 模块导入失败：", [f"{name}：{err}" for name, err in errors])


def test_discovery_ignores_adapters_defined_outside_the_package():
    class _Outsider(CommerceAdapter):
        platform = "zz-outsider-t166"
        status_rules = (StatusRule("status", ("paid",), "paid",
                                   source="ZZ-Outsider-T166 Open API / GetOrder / statuses"),)

    adapters, _errors = discover_adapters()
    assert _Outsider not in adapters
    outside = [_label(c) for c in adapters if not c.__module__.startswith(_PACKAGE + ".")]
    assert not outside, f"发现结果混进了包外定义的类：{outside}"


# ---------------------------------------------------------------------------
# 合成用例：证明每条检查函数真的有牙
# ---------------------------------------------------------------------------

_OK_RULES = (StatusRule("status", ("paid",), "paid",
                        source="ZZ-Synth-T166 Open API / GetOrder / statuses"),)


def _forge_rule(field, values, target, source) -> StatusRule:
    """绕过 `__post_init__` 造一条非法规则 —— 证明检查函数不依赖构造期校验。"""
    rule = object.__new__(StatusRule)
    for key, value in (("field", field), ("values", values),
                       ("target", target), ("source", source)):
        object.__setattr__(rule, key, value)
    return rule


def _synth(platform: str, rules=_OK_RULES, name: str = "_Synth"):
    """现造一个合成子类。它定义在本测试模块里，真包的发现会把它过滤掉。"""
    return type(name, (CommerceAdapter,), {"platform": platform, "status_rules": rules})


def test_check_flags_duplicate_platform():
    one = _synth("zz-dup-t166", name="_DupOne")
    two = _synth("zz-dup-t166", name="_DupTwo")
    other = _synth("zz-other-t166", name="_Other")

    violations = check_unique_platforms([one, two, other])
    assert len(violations) == 1
    assert "zz-dup-t166" in violations[0]
    assert "_DupOne" in violations[0] and "_DupTwo" in violations[0]
    assert check_unique_platforms([one, other]) == []


def test_check_flags_repr_or_query_override(monkeypatch):
    canary = f"canary-t166-{uuid.uuid4().hex}"
    monkeypatch.setenv("ZZ_T166_CANARY_SECRET", canary)

    class _Leaky(CommerceAdapter):
        platform = "zz-leaky-t166"
        status_rules = _OK_RULES

        def __repr__(self):
            secret = read_credential("ZZ_T166_CANARY_SECRET", platform=self.platform,
                                     purpose="合成泄密用例")
            return f"_Leaky(secret={secret})"

    # 先证明合成的泄密是真泄密，否则「判出」证明不了什么。
    assert canary in repr(_Leaky(transport=FakeTransport()))
    leaky = check_no_query_or_repr_override([_Leaky])
    assert len(leaky) == 1 and "__repr__" in leaky[0] and "_Leaky" in leaky[0]

    class _Late(CommerceAdapter):
        platform = "zz-late-t166"
        status_rules = _OK_RULES

    assert check_no_query_or_repr_override([_Late]) == []
    # 定义之后再塞进去：__init_subclass__ 看不见这一步。
    setattr(_Late, "query", lambda self, order_id: None)
    late = check_no_query_or_repr_override([_Late])
    assert len(late) == 1 and "query" in late[0] and "_Late" in late[0]

    # 中间类覆盖、叶子类没覆盖：照样判出，并点名经由哪个类。
    class _Middle(CommerceAdapter):
        def __repr__(self):
            return "middle"

    leaf = type("_Leaf", (_Middle,), {"platform": "zz-leaf-t166", "status_rules": _OK_RULES})
    via = check_no_query_or_repr_override([leaf])
    assert len(via) == 1 and "_Middle" in via[0]

    assert check_no_query_or_repr_override([_synth("zz-clean-t166")]) == []


def test_check_flags_empty_or_vague_source():
    blank = _forge_rule("status", ("paid",), "paid", "")
    assert blank.source == ""

    must_flag = [
        ("zz-probe-t166", ""),
        ("zz-probe-t166", "   "),
        ("zz-probe-t166", "见官方文档"),
        ("zz-probe-t166", "TODO"),
        ("zz-probe-t166", "see docs"),
        # 黑名单优先于放行条件：三段式路径里夹着 TODO 也判出。
        ("zz-probe-t166", "ZZ-Probe-T166 Open API / GetOrder / TODO"),
        ("zz-probe-t166", "orders"),
    ]
    for platform, source in must_flag:
        assert source_problem(platform, source), f"应判出：{platform!r} / {source!r}"

    must_pass = [
        ("shopify", "Shopify Admin API / Order resource / cancelled_at"),
        ("zz-probe-t166", "ZZ-Probe-T166 Open API / GetOrder / statuses"),
        ("acme_sp", "Acme SP-API / Orders API v0 / OrderStatus"),
        ("foo_shop", "Foo Shop Partner Center / Get Order Detail / order_status "
                     "https://partner.example.com/docv2/page/get-order-detail"),
    ]
    for platform, source in must_pass:
        assert source_problem(platform, source) is None, f"应放过：{platform!r} / {source!r}"

    # 同一组口径经 check_rule_sources 走一遍类的层面：空串规则（绕过构造期校验造的）判出。
    bad = _synth("zz-probe-t166", rules=(blank,), name="_BlankSource")
    vague = _synth("zz-probe-t166", name="_VagueSource",
                   rules=(StatusRule("status", ("paid",), "paid", source="见官方文档"),))
    good = _synth("acme_sp", name="_GoodSource",
                  rules=(StatusRule("OrderStatus", ("shipped",), "shipped",
                                    source="Acme SP-API / Orders API v0 / OrderStatus"),))
    flagged = check_rule_sources([bad, vague, good])
    assert len(flagged) == 2
    assert any("_BlankSource" in v for v in flagged)
    assert any("_VagueSource" in v for v in flagged)


def test_check_flags_forbidden_imports_in_source_text():
    http_bad = [
        "import requests",
        "import httpx",
        "from urllib import request",
        "import http.client",
        "import aiohttp",
        "import urllib3",
        "from http import client",
        "import urllib.request as ur",
        "from requests.adapters import HTTPAdapter",
        "def f():\n    import httpx\n    return httpx\n",
    ]
    layer_bad = [
        "from maos.domain.refund import x",
        "import maos.contracts.states",
        "from maos import contracts",
        "from ...contracts import states",
    ]
    allowed = [
        "import json",
        "import urllib.parse",
        "from urllib.parse import urlencode",
        "from maos.tools.commerce import CommerceAdapter",
        "from . import base",
    ]
    for src in http_bad:
        assert http_client_imports(src), f"应判出（判据 6）：{src!r}"
        assert domain_or_contract_imports(src) == [], f"不该算判据 7：{src!r}"
    for src in layer_bad:
        assert domain_or_contract_imports(src), f"应判出（判据 7）：{src!r}"
        assert http_client_imports(src) == [], f"不该算判据 6：{src!r}"
    for src in allowed:
        assert http_client_imports(src) == [], f"应放过（判据 6）：{src!r}"
        assert domain_or_contract_imports(src) == [], f"应放过（判据 7）：{src!r}"


def test_check_flags_module_level_credential_read():
    must_flag = {
        "顶层 read_credential": (
            "from maos.tools.commerce import read_credential\n"
            "TOKEN = read_credential('ZZ_T166_TOKEN', platform='zz', purpose='p')\n"),
        "顶层 os.environ.get": "import os\nREGION = os.environ.get('X', 'sg')\n",
        "类体 os.getenv": "import os\nclass A:\n    k = os.getenv('X')\n",
        "顶层 environ 下标": "from os import environ\nKEY = environ['X']\n",
        "默认参数值": "import os\ndef f(region=os.getenv('X')):\n    return region\n",
        "装饰器实参": ("import os\ndef deco(x):\n    return lambda fn: fn\n"
                   "@deco(os.environ.get('X'))\ndef f():\n    return 1\n"),
    }
    for label, src in must_flag.items():
        assert import_time_credential_reads(src), f"应判出：{label}"

    must_pass = {
        "函数体": textwrap.dedent("""\
            import os
            from maos.tools.commerce import read_credential

            def f():
                t = read_credential('ZZ_T166_TOKEN', platform='zz', purpose='p')
                r = os.environ.get('X', 'sg')
                k = os.getenv('X')
                return t, r, k
            """),
        "方法体": textwrap.dedent("""\
            import os

            class A:
                def m(self):
                    return os.getenv('X')
            """),
        "lambda 体": "import os\nf = lambda: os.getenv('X')\n",
        "只 import 不读": "from maos.tools.commerce import read_credential\nimport os\n",
    }
    for label, src in must_pass.items():
        assert import_time_credential_reads(src) == [], f"应放过：{label}"


def test_discovery_reports_import_errors_per_module(tmp_path, monkeypatch):
    pkg = f"zz_t166_pkg_{uuid.uuid4().hex[:12]}"
    pkg_dir = tmp_path / pkg
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("", encoding="utf-8")
    (pkg_dir / "broken.py").write_text(
        "raise RuntimeError('zz-t166 导入期故意炸')\n", encoding="utf-8")
    (pkg_dir / "good.py").write_text(textwrap.dedent("""\
        from maos.tools.commerce import CommerceAdapter, StatusRule


        class GoodAdapter(CommerceAdapter):
            platform = "zz-good-t166"
            status_rules = (StatusRule("status", ("paid",), "paid",
                                       source="ZZ-Good-T166 Open API / GetOrder / statuses"),)
        """), encoding="utf-8")
    # 先写文件再挂路径：syspath_prepend 会顺带 invalidate_caches()，顺序反了可能 import 不到。
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        adapters, errors = discover_adapters(path=[str(pkg_dir)], prefix=f"{pkg}.")

        assert [name for name, _ in errors] == [f"{pkg}.broken"]
        assert errors[0][1].startswith("RuntimeError: ")
        assert "zz-t166 导入期故意炸" in errors[0][1]

        assert [(c.__module__, c.__qualname__) for c in adapters] == [(f"{pkg}.good", "GoodAdapter")]
        assert adapters[0].platform == "zz-good-t166"
    finally:
        for key in [k for k in sys.modules if k == pkg or k.startswith(pkg + ".")]:
            del sys.modules[key]


# ---------------------------------------------------------------------------
# 合成用例（补充）：判据 1 / 2 的牙
# ---------------------------------------------------------------------------

def test_check_flags_missing_platform_or_rules():
    no_platform = _synth("", name="_NoPlatform")
    no_rules = _synth("zz-norules-t166", rules=(), name="_NoRules")
    flagged = check_platform_and_rules([no_platform, no_rules, _synth("zz-ok-t166")])
    assert len(flagged) == 2
    assert any("_NoPlatform" in v and "platform" in v for v in flagged)
    assert any("_NoRules" in v and "status_rules" in v for v in flagged)


def test_check_flags_rule_target_outside_four_states():
    fifth = _forge_rule("status", ("refunded",), "refunded",
                        "ZZ-Synth-T166 Open API / GetOrder / statuses")
    with pytest.raises(ValueError):
        StatusRule("status", ("refunded",), "refunded",
                   source="ZZ-Synth-T166 Open API / GetOrder / statuses")
    fifth_state = _synth("zz-fifth-t166", rules=(fifth,), name="_FifthState")
    not_a_rule = _synth("zz-tuple-t166", rules=(("status", ("paid",), "paid"),), name="_TupleRule")

    flagged = check_rule_targets([fifth_state, not_a_rule, _synth("zz-ok-t166")])
    assert len(flagged) == 2
    assert any("_FifthState" in v and "refunded" in v for v in flagged)
    assert any("_TupleRule" in v and "不是 StatusRule" in v for v in flagged)
