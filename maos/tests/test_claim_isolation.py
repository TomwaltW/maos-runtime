"""「换域只新增文件」的机器判据 —— 内核不认识理赔域，两个业务域也互不认识。

`docs/domain-portability.md` §3 给退款域立了两条 AST 守卫（内核三个子包不许 import
`maos.domain.refund`）。本文件把同样的守卫立给理赔域，并多立一条：

    **两个业务域之间也不许互相 import。**

这一条退款域那时没有，因为当时只有一个域。现在有两个了，它才成为一个能塌的判据 ——
理赔域一旦 import 了退款域的 `objects` 或 `guard`，「换域只新增文件」就成了
「换域要先长在上一个域身上」，第三个域会更难上。

判据一律走 **AST 扫 import 语句**，不做文本子串匹配：本域的 docstring 里到处写着
「不 import 退款域」这类自我说明，按子串扫会把这句话本身判成违例
（`maos/tests/test_refund_flow.py:461` 的注释记着这个坑真踩过）。
"""

from __future__ import annotations

import ast
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
MAOS_PKG = REPO_ROOT / "maos"

#: 内核三个子包。它们对任何业务域都必须是零依赖。
KERNEL_PKGS = ("contracts", "core", "runtime")

#: 理赔域自己的四个面。
CLAIM_PKGS = (
    MAOS_PKG / "domain" / "claim",
    MAOS_PKG / "skills" / "builtin" / "claim",
    MAOS_PKG / "agents" / "claim",
)
CLAIM_FILES = (MAOS_PKG / "tools" / "claim.py", MAOS_PKG / "tools" / "claim_codes.py")


def _imported_modules(path: pathlib.Path) -> set[str]:
    """一个文件 import 了哪些模块（全限定名）。只认 import 语句，不认字面量。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:                      # 相对 import：留在包内，与本判据无关
                continue
            module = node.module or ""
            names.add(module)
            names.update(f"{module}.{alias.name}" for alias in node.names)
    return names


def _files_under(*roots: pathlib.Path) -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for root in roots:
        if root.is_dir():
            out.extend(sorted(root.rglob("*.py")))
        elif root.is_file():
            out.append(root)
    return out


def _offenders(files, forbidden_prefixes: tuple[str, ...]) -> list[str]:
    bad = []
    for path in files:
        rel = str(path.relative_to(REPO_ROOT))
        for module in _imported_modules(path):
            if module.startswith(forbidden_prefixes):
                bad.append(f"{rel} -> {module}")
    return bad


def test_kernel_does_not_know_the_claim_domain():
    """论证：`contracts/` / `core/` / `runtime/` 一行理赔域知识都没有。

    这是「本轮内核零改动」那句话的机器判据。它红了，说明有人为了让理赔域好写
    去动了内核 —— 那时该停下来问人，不是把这条守卫改宽（铁律 9）。
    """
    files = _files_under(*(MAOS_PKG / pkg for pkg in KERNEL_PKGS))
    assert files, "内核三个子包一个文件都没扫到，判据形同虚设"
    bad = _offenders(files, ("maos.domain.claim", "maos.skills.builtin.claim",
                             "maos.agents.claim", "maos.tools.claim"))
    assert not bad, f"内核 import 了理赔域：{bad}"


def test_kernel_still_does_not_know_the_refund_domain():
    """论证：给理赔域立守卫的同时，退款域那条**没有被顺手放宽**。

    两条一起跑，是因为「新域上线时把老域的守卫改松了」是最容易发生、也最难发现的
    一种回归 —— 老域的测试仍然绿，因为它测的是老域自己。
    """
    files = _files_under(*(MAOS_PKG / pkg for pkg in KERNEL_PKGS))
    bad = _offenders(files, ("maos.domain.refund",))
    assert not bad, f"内核 import 了退款域：{bad}"


def test_the_two_business_domains_do_not_import_each_other():
    """论证：理赔域与退款域互不 import。

    理赔域一旦长在退款域身上，「换域只新增文件」就成了「换域要先长在上一个域上」，
    第三个域会更难上。同构而不共用，是有意的重复（见各文件抬头）。
    """
    claim_bad = _offenders(
        _files_under(*CLAIM_PKGS, *CLAIM_FILES),
        ("maos.domain.refund", "maos.skills.builtin.refund", "maos.agents.refund",
         "maos.tools.gateway"))
    assert not claim_bad, f"理赔域 import 了退款域：{claim_bad}"

    refund_bad = _offenders(
        _files_under(MAOS_PKG / "domain" / "refund",
                     MAOS_PKG / "skills" / "builtin" / "refund",
                     MAOS_PKG / "agents" / "refund",
                     MAOS_PKG / "tools" / "gateway.py",
                     MAOS_PKG / "tools" / "gateway_codes.py"),
        ("maos.domain.claim", "maos.skills.builtin.claim", "maos.agents.claim",
         "maos.tools.claim"))
    assert not refund_bad, f"退款域 import 了理赔域：{refund_bad}"


def test_claim_domain_does_not_touch_the_frozen_store_tables():
    """论证：理赔域只**新增**表，一张既有表都不写（铁律 1）。

    判据扫 `schema.sql`：本域建的每一张表都必须带 `claim` / `policy` / `payer` /
    `adjudication` 这几个本域前缀，且不许出现 Phase 0 那五张既有表与退款域那 15 张表
    的表名。
    """
    schema = (MAOS_PKG / "domain" / "claim" / "schema.sql").read_text(encoding="utf-8")
    created = [line.split("EXISTS")[1].split("(")[0].strip()
               for line in schema.splitlines()
               if line.strip().upper().startswith("CREATE TABLE")]
    assert created, "schema.sql 一张表都没建？判据形同虚设"

    # Phase 0 的五张既有表 + 退款域那 15 张。一张都不许出现。
    foreign = {
        "plan", "task", "artifact", "event_log", "idempotency", "model_usage",
        "tenant", "channel", "order_snapshot", "product_snapshot", "policy_rule",
        "refund_case", "customer_evidence", "approval_record", "finance_entry",
        "refund_request", "payment_observation", "notification",
        "compensation_record", "business_ref", "refund_schema_version",
    }
    clashes = sorted(set(created) & foreign)
    assert not clashes, (
        f"理赔域的 schema.sql 建了不属于本域的表：{clashes} —— "
        "只许新增，不许碰既有表（铁律 1）")

    allowed_prefix = ("claim", "policy_", "payer", "adjudication")
    odd = [t for t in created if not t.startswith(allowed_prefix)]
    assert not odd, f"理赔域建了没有本域前缀的表：{odd}"


def test_claim_schema_creates_the_declared_number_of_tables():
    """论证：表数与 `__init__.py` 里写的那句话对得上。

    文档里写「12 张业务表 + 1 张迁移记账表」而实际建了别的数，是最典型的文档漂移。
    """
    schema = (MAOS_PKG / "domain" / "claim" / "schema.sql").read_text(encoding="utf-8")
    count = sum(1 for line in schema.splitlines()
                if line.strip().upper().startswith("CREATE TABLE"))
    assert count == 13, (
        f"schema.sql 建了 {count} 张表，与 domain/claim/__init__.py 里写的"
        " 12 + 1 对不上；改表就把那句话一起改")


def test_claim_domain_never_imports_the_kernel_write_path():
    """论证：理赔域不去改内核的写入口 —— 它只用 Store 暴露的连接建自己的表。

    `maos.core.store` 可以读（`SqliteStore` 类型标注、`_conn`），但本域一行都不该
    去 import `maos.core.control_plane` 或 `maos.runtime.*`：状态迁移是控制面的事，
    业务域插手就等于开了第二条迁移路径。
    """
    bad = _offenders(_files_under(*CLAIM_PKGS, *CLAIM_FILES),
                     ("maos.core.control_plane", "maos.runtime"))
    assert not bad, (
        f"理赔域 import 了控制面/运行时的写入路径：{bad} —— "
        "状态迁移只有控制面做得了，业务域插手就是第二条迁移路径")
