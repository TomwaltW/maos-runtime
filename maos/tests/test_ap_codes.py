"""应付账款域的码表与规则表 —— 出处、完整性，以及「编号不许硬编在逻辑里」。

本文件守的是一句话：**拒付理由可核对**。它由三条互相独立的判据支撑：

  1. 每条码、每条规则都带 `source`，且 `source` 指向 Peppol BIS Billing 3.0
     的真实页面（`test_every_entry_has_a_source`）；
  2. 未知码、未知编号一律**抛**，不兜底（`test_unknown_*_raises`）——
     兜底会把没核过出处的东西当成已核过的；
  3. 判定逻辑里**不许出现规则编号的裸字面量**（`test_no_hardcoded_rule_ids`），
     只能从 `ap_codes` 的常量取 —— 从常量取就必过 `require_rule()`，
     打错一个字当场死掉，而不是流到产物上让人以为它可核对。

第 3 条是三条里最容易被忽略、也最要紧的一条：前两条保证「表里的东西是真的」，
第 3 条保证「用的是表里的东西」。
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

from maos.tools import ap_codes

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
MAOS_PKG = REPO_ROOT / "maos"

#: 规则编号长什么样。两个前缀分属 EN 16931 与 Peppol 附加层。
RULE_ID_RE = re.compile(r"\b(?:BR-[A-Z0-9-]+|PEPPOL-EN16931-[A-Za-z0-9]+)\b")

#: 本域会跑判定的源码面。`ap_codes.py` 自己不在里面 —— 它就是那张表。
AP_SOURCE_FILES = (
    *(MAOS_PKG / "domain" / "ap").rglob("*.py"),
    *(MAOS_PKG / "skills" / "builtin" / "ap").rglob("*.py"),
    *(MAOS_PKG / "agents" / "ap").rglob("*.py"),
    MAOS_PKG / "tools" / "ap.py",
    MAOS_PKG / "flows" / "scenario_10.py",
)


# ---------------------------------------------------------------- 出处与完整性
def test_every_code_entry_has_a_peppol_source():
    """每条码都带出处，且出处指向本规范的站点。核不到出处的不许进表。"""
    assert ap_codes.CODE_LISTS, "码表一张都没有"
    for list_id, table in ap_codes.CODE_LISTS.items():
        assert table, f"{list_id} 是空表"
        for code, entry in table.items():
            assert entry.source.startswith("https://docs.peppol.eu/poacc/billing/3.0/"), (
                f"{list_id}/{code} 的出处 {entry.source!r} 不指向 Peppol BIS Billing 3.0")
            assert entry.name.strip(), f"{list_id}/{code} 没有官方名称"
            assert entry.list_id == list_id, f"{code} 挂错了码表"


def test_every_rule_has_a_source_and_original_text():
    """每条规则都带出处与规范原文。原文是「拿编号去查能查到」的落点。"""
    assert ap_codes.RULES, "规则表是空的"
    for rule_id, rule in ap_codes.RULES.items():
        assert rule.rule_id == rule_id
        assert rule.source in (ap_codes.SRC_RULES_EN16931, ap_codes.SRC_RULES_PEPPOL), (
            f"{rule_id} 的出处 {rule.source!r} 不是那两个规则页之一")
        assert len(rule.text.strip()) > 20, (
            f"{rule_id} 的原文太短（{rule.text!r}）—— 原文要能拿去和规范对字")
        # 编号前缀与所属层必须一致：Peppol 层的编号不该挂 en16931，反之亦然。
        expected = (ap_codes.LAYER_PEPPOL if rule_id.startswith("PEPPOL-")
                    else ap_codes.LAYER_EN16931)
        assert rule.layer == expected, f"{rule_id} 的 layer 与编号前缀对不上"


def test_spec_release_and_fetch_date_are_recorded():
    """版本与抓取日期必须写着 —— 规范会改版，结论有有效期。"""
    assert "Peppol BIS Billing 3.0" in ap_codes.SPEC_RELEASE
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", ap_codes.FETCHED_AT), (
        f"抓取日期格式不对：{ap_codes.FETCHED_AT!r}")


def test_table_sizes_are_computed_not_copied():
    """条数是 `len()` 现算的，不是抄页面上的数字。

    抓取时同一页做过两次独立提取，**枚举一致而页面摘要给出的总数两次不同**
    （见 `ap_codes` 模块 docstring）。所以条数只能自己数。这条用例顺带钉住
    「三张表都不是空的」以及「payment means 的断档不是抄漏」。
    """
    sizes = ap_codes.table_sizes()
    assert set(sizes) == set(ap_codes.CODE_LISTS)
    for list_id, n in sizes.items():
        assert n == len(ap_codes.CODE_LISTS[list_id])

    means = ap_codes.PAYMENT_MEANS_CODES
    # 71/72/73 在规范里就是不存在的。抓取时专门核过一次，这条断言把结论钉住 ——
    # 将来谁看见断档以为抄漏了、顺手补上三条，会在这里被拦下。
    for absent in ("71", "72", "73"):
        assert absent not in means, (
            f"UNCL4461 里不该有 {absent} —— 规范原表在 70 与 74 之间是断的，"
            f"补上它等于凭空造一个码")
    for present in ("70", "74", "98", "ZZZ"):
        assert present in means, f"UNCL4461 少了 {present}"


@pytest.mark.parametrize("list_id, code, name", [
    # 抽三条自己核（派单 §6 硬判据 5）。三条各出自一张表，
    # 值逐字抄自各自的码表页，改动任何一个字都要重新去页面上核。
    (ap_codes.LIST_INVOICE_TYPE, "380", "Commercial invoice"),
    (ap_codes.LIST_TAX_CATEGORY, "S", "Standard rate"),
    (ap_codes.LIST_PAYMENT_MEANS, "30", "Credit transfer"),
])
def test_spot_checked_codes(list_id, code, name):
    """三条抽检码：值与官方名称逐字一致。"""
    entry = ap_codes.require_code(list_id, code)
    assert entry.name == name, (
        f"{list_id}/{code} 的官方名称应为 {name!r}，实际 {entry.name!r} —— "
        f"名称是原文照抄的，改了就对不上文档")


def test_unknown_code_raises_instead_of_falling_back():
    """未知码抛，不返回兜底条目。兜底 = 把没核过出处的码当成已知码。"""
    with pytest.raises(KeyError, match="不在"):
        ap_codes.require_code(ap_codes.LIST_PAYMENT_MEANS, "72")
    with pytest.raises(KeyError, match="没有名为"):
        ap_codes.require_code("UNCL9999", "1")
    assert ap_codes.is_valid_code(ap_codes.LIST_PAYMENT_MEANS, "30") is True
    assert ap_codes.is_valid_code(ap_codes.LIST_PAYMENT_MEANS, "72") is False


def test_unknown_rule_id_raises():
    """自造编号在取值这一层就死掉 —— 这是「理由可核对」的最后一道机器闸。"""
    with pytest.raises(KeyError, match="不许挂自造编号"):
        ap_codes.require_rule("BR-99")
    with pytest.raises(KeyError):
        ap_codes.cite("PEPPOL-EN16931-R999")


def test_cite_carries_source_and_version():
    """引用块里带出处与版本，评委不必回源码就能核。"""
    block = ap_codes.cite(ap_codes.RULE_LINE_NET_AMOUNT)
    assert block["rule_id"] == ap_codes.RULE_LINE_NET_AMOUNT
    assert block["source"] == ap_codes.SRC_RULES_PEPPOL
    assert block["spec"] == ap_codes.SPEC_RELEASE
    assert block["fetched_at"] == ap_codes.FETCHED_AT
    assert block["text"]


def test_all_rule_constants_point_into_the_table():
    """`RULE_*` 常量全部指向表里真实存在的规则。

    常量与表分离之后，最容易出的错是「加了常量忘了加规则」—— 那时候判定代码
    照跑，直到某条判据第一次命中才抛。这条用例让它在测试期就死。
    """
    constants = {n: v for n, v in vars(ap_codes).items()
                 if n.startswith("RULE_") and isinstance(v, str)}
    assert constants, "一个 RULE_* 常量都没有"
    for name, rule_id in constants.items():
        assert rule_id in ap_codes.RULES, (
            f"常量 {name}={rule_id!r} 不在 RULES 里 —— 加常量必须同时加规则")


# ---------------------------------------------- 编号不许硬编在判定逻辑里（第 3 条）
def _executable_string_constants(path: pathlib.Path) -> list[tuple[int, str]]:
    """取一份源码里**可执行位置**的字符串字面量，跳过 docstring。

    为什么要跳过 docstring：本域的模块与函数说明里大量引用规则编号（那正是好文档
    该做的事），把它们算成违例会让这条判据变成「不许在注释里提规则」——
    那不是这条用例要买的东西。注释（`#` 开头）根本不进 AST，天然不在扫描面上。

    跳的是**所有裸字符串表达式语句**，不只是模块/类/函数的第一句。按构造，
    一个求值即丢弃的字符串语句只可能是文档 —— PEP 257 的属性 docstring
    （`amount: str` 下面那一行）就是这一类，它到不了任何判定逻辑里。
    反过来，凡是被当作**值**用的字符串（赋给常量、进 `SkillContract` 字段、
    进 f-string 拼进消息）一律留在扫描面上：它们会出现在产物、文档生成物或
    房间卡片里，编号写错在那里和写错在判定里一样坏。

    f-string 里的常量段也算：`f"按 BR-CO-17 算"` 与 `"BR-CO-17"` 是同一件事。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            docstrings.add(id(node.value))
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docstrings):
            out.append((node.lineno, node.value))
    return out


def test_no_hardcoded_rule_ids_outside_the_code_table():
    """本域判定逻辑里不许出现规则编号的裸字面量，只能从 `ap_codes` 常量取。

    理由不是洁癖：字面量散在判定逻辑里，就**没有任何机制保证它是存在的规则** ——
    打错一个字（`BR-CO-31`）照样跑，而拒付理由挂着一个查不到的编号，看起来毫无
    破绽。从常量取则必过 `require_rule()`，编号不存在当场抛。

    扫描面刻意包含 `flows/scenario_10.py`：场景里那份给模型的脚本文案同样会被
    评委读，编号写错在那里和写错在判定里一样坏。
    """
    offenders = []
    for path in sorted(set(AP_SOURCE_FILES)):
        for lineno, text in _executable_string_constants(path):
            for hit in RULE_ID_RE.findall(text):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno} -> {hit}")
    assert not offenders, (
        "判定逻辑里出现了规则编号的裸字面量，必须改成 import ap_codes 的 RULE_* 常量：\n  "
        + "\n  ".join(offenders))


def test_ap_domain_does_not_import_the_refund_domain():
    """本域不许 import 退款域 —— 两个域各是各的，共用一行就不是两个域了。

    与 `test_refund_flow.py::test_kernel_does_not_know_the_refund_domain` 同一种
    写法（认 import 语句、不认字面量）：本域的 docstring 里就写着「与退款域
    `maos/domain/refund/guard.py` 的关系」，按子串扫会把那句自我说明判成违例。
    """
    offenders = []
    for path in sorted(set(AP_SOURCE_FILES)):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                hit = any(a.name.startswith("maos.domain.refund") for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                hit = bool(node.module and node.module.startswith("maos.domain.refund"))
            else:
                continue
            if hit:
                offenders.append(str(path.relative_to(REPO_ROOT)))
                break
    assert not offenders, f"应付账款域 import 了退款域：{offenders}"


def test_kernel_does_not_know_the_ap_domain():
    """铁律 9 推论：runtime / core / contracts 不许 import **任何**业务域。

    退款域那边已经有两条同样的守卫（`test_gate.py` 与 `test_refund_flow.py`）。
    本条不是重复：它们扫的是「有没有 import `maos.domain`」，而本条的意义在于
    **本域落地之后再跑一次** —— 「换域零改动」这句话是对每一个新域各说一遍的，
    不是说一次就永远成立。
    """
    offenders = []
    for sub in ("runtime", "core", "contracts"):
        for path in sorted((MAOS_PKG / sub).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    hit = any(a.name.startswith("maos.domain") for a in node.names)
                elif isinstance(node, ast.ImportFrom):
                    hit = bool(node.module and node.module.startswith("maos.domain"))
                else:
                    continue
                if hit:
                    offenders.append(str(path.relative_to(REPO_ROOT)))
                    break
    assert not offenders, f"内核 import 了业务域：{offenders}"
