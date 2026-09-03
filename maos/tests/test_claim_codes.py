"""X12 码表守卫 —— 每条码都有出处，而且**判据是从码表读的，不是硬编在逻辑里**。

这组断言要买两样东西：

1. **可核对性。** 每条码都带 `source` + `fetched_at` + `start`，随便抽三条都能回到
   x12.org 上逐字比。核不到出处的码不许进表 —— `__post_init__` 当场抛。

2. **单一判据来源。** 码 -> 处置的映射只许存在一处（`claim_codes.py`）。
   skill / agent / flow 里但凡出现第二套「见到 96 就怎样」的字面量判断，这里就红。
   这一条比第一条更容易被悄悄破坏：加一个 `if carc == "96"` 不会有任何症状，
   直到码表改了一个 recourse，而那句 if 还按老口径走。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from maos.tools import claim_codes as CC

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
MAOS_PKG = REPO_ROOT / "maos"

#: 允许出现 CARC 裸字面量的地方。
#:   · 码表本身 —— 它就是那份字面量；
#:   · 场景文件 —— 演示要指名注入哪条码，那是**数据**不是判据，且它注入之后
#:     一律回头查码表拿 effect/recourse（`DENIAL_CARC` 只被喂给 MockPayer 与断言）。
#: 别的地方一律不许。
_LITERAL_ALLOWED = {
    "maos/tools/claim_codes.py",
    "maos/flows/scenario_8.py",
}


# ------------------------------------------------------------------ 出处完整
def test_every_code_carries_source_and_fetch_date():
    """论证：核不到出处的码进不了表。"""
    for code, entry in CC.ALL_CODES.items():
        assert entry.source.startswith("https://x12.org/"), (
            f"CARC {code} 的出处不是 x12.org：{entry.source}")
        assert entry.fetched_at == CC.FETCHED_AT, (
            f"CARC {code} 的抓取日期与全表不一致 —— 说不清照的是哪一版")
        assert entry.start, f"CARC {code} 缺 Start 日期"
        assert entry.description.strip(), f"CARC {code} 缺官方描述"
        assert entry.rationale.strip(), (
            f"CARC {code} 的 effect/recourse 没写判断依据；这两个字段不是官方原文，"
            "不写依据就与凭记忆编造无异")


def test_dataclass_refuses_a_code_without_provenance():
    """论证：出处校验是**代码级**的，不是靠自觉填。"""
    with pytest.raises(ValueError, match="没有出处"):
        CC.AdjustmentCode(code="X", description="d", start="01/01/1995", last_modified="",
                          effect=CC.EFFECT_DENIED, recourse=CC.RECOURSE_NONE,
                          rationale="r", source="", fetched_at=CC.FETCHED_AT)
    with pytest.raises(ValueError, match="抓取日期"):
        CC.AdjustmentCode(code="X", description="d", start="01/01/1995", last_modified="",
                          effect=CC.EFFECT_DENIED, recourse=CC.RECOURSE_NONE,
                          rationale="r", source=CC.SRC_CARC, fetched_at="")
    with pytest.raises(ValueError, match="判断依据"):
        CC.AdjustmentCode(code="X", description="d", start="01/01/1995", last_modified="",
                          effect=CC.EFFECT_DENIED, recourse=CC.RECOURSE_NONE,
                          rationale="", source=CC.SRC_CARC, fetched_at=CC.FETCHED_AT)


def test_sampled_codes_match_the_published_table_verbatim():
    """论证：抽样四条与 x12.org 页面原文逐字一致（2026-08-31 实测）。

    这四条是编排侧派单里点名的抽样，页面上长这样：

        1    Deductible Amount                                   Start: 01/01/1995
        2    Coinsurance Amount                                  Start: 01/01/1995
        45   Charge exceeds fee schedule/maximum allowable or    Start: 01/01/1995
             contracted/legislated fee arrangement               Last Modified: 07/01/2017
        96   Non-covered charge(s). At least one Remark Code     Start: 01/01/1995
             must be provided                                    Last Modified: 07/01/2017

    长描述在页面上是折行显示的，这里比对的是**完整句**（含 `Usage:` 段），
    所以用 startswith + 关键片段，而不是整串相等 —— 整串相等会把「页面换了折行方式」
    误报成「码表被篡改」。
    """
    assert CC.ALL_CODES["1"].description == "Deductible Amount"
    assert CC.ALL_CODES["1"].start == "01/01/1995"
    assert CC.ALL_CODES["1"].last_modified == ""

    assert CC.ALL_CODES["2"].description == "Coinsurance Amount"
    assert CC.ALL_CODES["2"].start == "01/01/1995"

    c45 = CC.ALL_CODES["45"]
    assert c45.description.startswith(
        "Charge exceeds fee schedule/maximum allowable or contracted/legislated "
        "fee arrangement.")
    assert c45.start == "01/01/1995" and c45.last_modified == "07/01/2017"

    c96 = CC.ALL_CODES["96"]
    assert c96.description.startswith("Non-covered charge(s). At least one Remark Code "
                                      "must be provided")
    assert c96.start == "01/01/1995" and c96.last_modified == "07/01/2017"


def test_group_codes_are_the_four_x12_defines():
    """论证：调整组码就是 X12 定义的那四条，不多不少。"""
    assert sorted(CC.ALL_GROUP_CODES) == ["CO", "OA", "PI", "PR"]
    for code, entry in CC.ALL_GROUP_CODES.items():
        assert entry.start == "05/20/2018", f"Group Code {code} 的 Start 与页面不符"
        assert entry.source == CC.SRC_GROUP


# ------------------------------------------------------------------ 判据一致
def test_unknown_code_raises_instead_of_defaulting():
    """论证：未知码抛，不兜底。

    兜底的后果不是报错，是**把没核过出处的码当成已知码处理** —— 那正是这张表要防的事。
    """
    with pytest.raises(KeyError, match="不在已核对"):
        CC.lookup("ZZZ")
    with pytest.raises(KeyError):
        CC.effect_of("999999")
    with pytest.raises(KeyError):
        CC.lookup_group("XX")


def test_four_recourses_are_all_covered():
    """论证：四种处置各有代表码。少一类就说明码表塌了一格。"""
    assert set(CC.REQUIRED_RECOURSES) == set(CC.RECOURSES)
    for recourse, codes in CC.REQUIRED_RECOURSES.items():
        assert codes, f"{recourse} 一条代表码都没有"
        for code in codes:
            assert CC.recourse_of(code) == recourse


def test_effect_and_recourse_are_orthogonal():
    """论证：两个维度真的正交 —— 同一个 effect 下有不同 recourse。

    只要它们退化成一一对应，其中一个就是冗余的，而冗余的那个迟早会被删掉，
    连带把「拒赔也分能不能补救」这条信息一起删掉。
    """
    by_effect: dict[str, set[str]] = {}
    for entry in CC.ALL_CODES.values():
        by_effect.setdefault(entry.effect, set()).add(entry.recourse)
    assert len(by_effect[CC.EFFECT_DENIED]) >= 3, (
        f"拒赔类应当有多种处置（终态 / 补件重报 / 转其他赔付方 / 只能人工），"
        f"实际 {sorted(by_effect[CC.EFFECT_DENIED])}")


def test_carc_never_means_paid():
    """论证：码表**不提供**任何「这条码等于已赔付」的入口（铁律 8）。

    `1` / `2` / `45` 的 effect 不是 denied，读起来像「那这笔是赔了的」。
    给这条路留一个 `is_paid()` 函数，迟早有人拿它去写 paid。
    """
    assert not hasattr(CC, "is_paid"), (
        "码表不许有 is_paid()：到账是外部权威事实，只能由 claim.observe 问出来")
    assert CC.effect_of("45") != CC.EFFECT_DENIED
    assert not CC.is_denial("45")
    # 而它照样不是「可以据此写 paid」的信号 —— 判据只在 guard 那一处。
    from maos.domain.claim import guard
    assert guard.AUTHORITATIVE_RECEIPT_STATE["paid"] == frozenset({"paid"})


def test_route_to_other_payer_is_not_a_machine_retry():
    """论证：「改送别的赔付方」不算机器可重报。

    混进来会让机器把同一份申报原样投给一个还没确定的对象。
    """
    assert CC.machine_can_retry("16") is True
    assert CC.machine_can_retry("252") is True
    assert CC.machine_can_retry("109") is False
    assert CC.machine_can_retry("96") is False


# ------------------------------------------------- 判据从码表读，不硬编在逻辑里
def test_no_carc_literals_outside_the_code_table():
    """论证：码 -> 处置的判断只存在于码表里，别处不许出现 CARC 裸字面量的比较。

    判据走 AST：只看**比较表达式**里的常量（`x == "96"` / `x in ("96", "16")`），
    不看 docstring、不看普通赋值 —— 按文本 grep 会把注释里提到的码一并判成违规，
    而一条会误报的守卫等于没有守卫（退款域踩过这个坑）。

    这条红了通常意味着有人在某个 skill 里加了 `if carc == "96"`。那不会有任何症状，
    直到码表改了一个 recourse，而那句 if 还按老口径走。

    **扫描面分两档，因为短码是有歧义的**：`1` / `2` / `3` 这三条 CARC 同时也是任何
    代码里最常见的字面量（`maos/tools/sandbox.py:158` 就有一句与 `"1"` 的比较，
    与理赔毫无关系）。全仓按全表扫会把它误报成违规，而误报的守卫等于没有守卫。
    所以：

      · **理赔域自己的文件** —— 按**全表**扫，包括短码。第二套映射真要出现，
        只会出现在这里。
      · **全仓其余部分** —— 只扫两位以上的判别性码（`96` / `109` / `197` / …），
        它们不会与别处的普通字面量撞车。
    """
    claim_owned = ("maos/domain/claim/", "maos/skills/builtin/claim/",
                   "maos/agents/claim/", "maos/tools/claim")
    distinctive = {c for c in CC.ALL_CODES if len(c) >= 2}
    offenders = []
    for path in sorted(MAOS_PKG.rglob("*.py")):
        rel = str(path.relative_to(REPO_ROOT))
        if rel in _LITERAL_ALLOWED or rel.startswith("maos/tests/"):
            continue
        known = (set(CC.ALL_CODES) if rel.startswith(claim_owned) else distinctive)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            operands = [node.left, *node.comparators]
            for operand in operands:
                for const in _constants_in(operand):
                    if const in known:
                        offenders.append(f"{rel}:{node.lineno} 比较了 CARC {const!r}")
    assert not offenders, (
        "码 -> 处置的判断只许存在于 maos/tools/claim_codes.py：" + "; ".join(offenders))


def _constants_in(node: ast.AST) -> list[str]:
    """取一个表达式里的字符串常量（含 tuple/list/set 字面量里的）。"""
    out = []
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        out.append(node.value)
    elif isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        for item in node.elts:
            out.extend(_constants_in(item))
    return out


def test_domain_reads_recourse_through_the_table():
    """论证：工单措辞是**查表**得来的，不是另写一套映射。

    改一条码的 recourse，工单里那句话要跟着变 —— 这就是「单一判据来源」的可观察后果。
    """
    from maos.skills.builtin.claim.compensate import ClaimCompensateSkill as S

    resubmit = S._todo("denied", "16", CC.recourse_of("16"))
    terminal = S._todo("denied", "96", CC.recourse_of("96"))
    other = S._todo("denied", "109", CC.recourse_of("109"))
    human = S._todo("denied", "197", CC.recourse_of("197"))

    assert any("重新申报" in line for line in resubmit)
    assert any("重报无意义" in line for line in terminal)
    assert any("改送" in line for line in other)
    assert any("申诉" in line for line in human)
    # 四档必须两两不同，否则「分档」只是摆设。
    bodies = [tuple(x) for x in (resubmit, terminal, other, human)]
    assert len(set(bodies)) == 4
