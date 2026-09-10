"""规则号撞号的机器判据：`AS-00x` 只在租户内有意义。

起因是一处会在答辩现场咬人的失真：**同一个 `AS-003` 在本仓库里指着两件毫不相干的事**。

  · `scenarios/custom/ledger.json` 的租户 `tnt-demo`：发错货全额退（`wrong_item`），与证据无关；
  · `scenarios/refund/**` 的租户 `tnt-mfg-a` / `tnt-mfg-b`：人为损坏免责，需图片举证
    （`artificial_damage_exclusion`）。

于是拿自定义入口实跑一单，回帖里打出 `AS-003@v1`，听众按 `scenarios/refund/` 的语料理解，
会以为「举证判据生效了」—— 其实命中的是发错货那条。凡是拿规则号当口径讲的地方
（答辩问答、PPT、`docs/EXECUTION.md` 附 B 的差异表）都可能对错人。

`policy_rule` 表的主键是 `(tenant_id, rule_no, version)`，`rule_no` **不是**全局主键。
这一条本来只写在两份 README 里，而「下一个人照样会撞」—— 所以有了本文件。

四条，各钉一件：

  1. 每份含规则号的语料都在抬头声明了自己的租户作用域，且把文件里真实出现的租户都点了名；
  2. 两个租户的 `AS-003` 的 `rule_kind` **确实不同** —— 将来谁把两边改成一样，这条会红，
     逼那个人先来看这段注释，而不是默默假设它们一致；
  3. 语料里出现的每个 `tenant_id`，两份 README 的对照表里都有一行；
  4. `tnt-demo` 的 `AS-003` **不带**任何举证键 —— 它是发错货规则，不是举证规则。

**本文件只读语料**，不 import `maos/skills/builtin/refund/` 的任何模块：那几个文件正在别的
轨上改形状，测试跟着它们走就会变成「谁先合并谁把别人搞红」。语料是本轨的面，也是唯一的面。

编号本身**没有改**：`evidence/` 下的证据束里冻着 `AS-003` 这个字面量，而证据必须是真实
命令输出、一个字节都不许手改（铁律 3）。改号会让证据束与语料当场对不上。所以这里买的是
「口径说清楚 + 机器守着」，不是重新编号。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCENARIOS = REPO / "scenarios"

#: 抬头口径句的三个不变关键词。整句措辞六处一致，但租户名一处一个样，
#: 所以判据落在不随租户变化的这三段上。
SCOPE_MARKERS = (
    "编号只在租户",
    "内有意义",
    "跨语料引用规则号前先看租户",
)

#: 本轨（T78）白名单内、已补口径的语料。
#:
#: `scenarios/bulk/ledger-bulk.json` 是**生成产物**（`scenarios/bulk/generate.py`），
#: 不是手写语料，但照样列在这里：这条判据认的是「文件里有没有非空 `policy_rule`」，
#: 生成的规则一样会被人拿着编号去对照。抬头那句口径由生成器写进 `_rule_no_scope`，
#: 改生成器时会连着改。删掉 bulk 目录的话本条会报「缺失」—— 那时把这一行一起删。
COVERED = {
    "scenarios/bulk/ledger-bulk.json",
    "scenarios/custom/ledger.json",
    "scenarios/refund/cases/case_r4a.json",
    "scenarios/refund/cases/case_r4b.json",
    # T116 的十类齐单案例包。租户只有 tnt-mfg-a，policy_rule 八行逐字段取自
    # policy/policy_rules.json（唯一事实源），抬头 `_rule_no_scope` 已写。
    "scenarios/refund/cases/case_real_01.json",
    "scenarios/refund/policy/policy_rules.json",
}

#: 含规则号、但**不在 T78 白名单内**、口径待补的语料。
#: 记在 `docs/BACKLOG.md` 的 `## task-T78`，交整合轮补齐。
#: 列在这里而不是从判据里删掉，是为了让「还差哪几份」写在代码里而不是只写在账本里 ——
#: 补完一份就从这个集合挪进 COVERED，挪漏了第 1 条会红。
PENDING = {
    "scenarios/custom/refund-case.json",
    "scenarios/refund/cases/case_r3a.json",
    "scenarios/refund/cases/case_r3b.json",
    "scenarios/refund/cases/case_r6.json",
}

READMES = (
    "scenarios/custom/README.md",
    "scenarios/refund/README.md",
)


def _corpus_files() -> dict[str, dict]:
    """扫出 `scenarios/` 下所有**定义了政策规则**的语料，键是仓库相对路径。

    判据是「有非空的 `policy_rule` 顶层键」，不是文件名 —— 按名字挑会漏掉将来新增的语料，
    而新增语料正是这条口径最容易再撞一次的地方。
    """
    found: dict[str, dict] = {}
    for path in sorted(SCENARIOS.rglob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        rules = payload.get("policy_rule")
        if isinstance(rules, list) and rules:
            found[path.relative_to(REPO).as_posix()] = payload
    return found


def _rule_kind(payload: dict, tenant: str, rule_no: str, version: int = 1) -> str:
    """取某租户某条规则某一版的 `rule_kind`。`body` 是紧凑 JSON 字符串。"""
    for row in payload["policy_rule"]:
        if (row["tenant_id"], row["rule_no"], row["version"]) == (tenant, rule_no, version):
            return json.loads(row["body"])["rule_kind"]
    raise AssertionError(f"语料里找不到 {tenant} / {rule_no} v{version}")


def _table_after_header(text: str, header_marker: str) -> list[str]:
    """取 markdown 里表头含 `header_marker` 的那张表的**数据行**（不含表头与分隔行）。

    按「行里有没有 AS-003」筛是不行的：数据行里写的是租户与规则标题，
    只有表头那一格写着「AS-003 是什么」。
    """
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        if line.startswith("|") and header_marker in line:
            rows = []
            for row in lines[idx + 1:]:
                if not row.startswith("|"):
                    break
                if set(row) <= set("| :-"):  # 分隔行
                    continue
                rows.append(row)
            return rows
    return []


@pytest.fixture(scope="module")
def corpus() -> dict[str, dict]:
    return _corpus_files()


def test_every_corpus_file_declares_its_tenant_scope(corpus):
    """每份含规则号的语料抬头都声明租户作用域，且点名了自己文件里真实出现的租户。

    新增一份带 `policy_rule` 的语料而不分类，这条会红 —— 那正是提醒的时机。
    """
    assert set(corpus) == COVERED | PENDING, (
        "scenarios/ 下含 policy_rule 的语料清单变了。新增的语料要么补上抬头的租户作用域说明"
        "并加进 COVERED，要么先记进 PENDING 与 docs/BACKLOG.md —— 不许默默留着，"
        f"实测：{sorted(set(corpus) - (COVERED | PENDING))} 多出，"
        f"{sorted((COVERED | PENDING) - set(corpus))} 缺失")

    for rel in sorted(COVERED):
        scope = corpus[rel].get("_rule_no_scope")
        assert isinstance(scope, str) and scope, (
            f"{rel} 缺 `_rule_no_scope` 抬头 —— 规则号不带租户就没有所指")
        for marker in SCOPE_MARKERS:
            assert marker in scope, f"{rel} 的 `_rule_no_scope` 缺固定措辞「{marker}」"
        tenants = {row["tenant_id"] for row in corpus[rel]["policy_rule"]}
        for tenant in sorted(tenants):
            assert tenant in scope, (
                f"{rel} 的 policy_rule 里有租户 {tenant}，但抬头口径句没点它的名 —— "
                "作用域说明漏了租户，等于没说")

    for rel in READMES:
        text = (REPO / rel).read_text(encoding="utf-8")
        for marker in SCOPE_MARKERS:
            assert marker in text, f"{rel} 缺固定措辞「{marker}」"


def test_same_rule_no_across_tenants_is_not_the_same_rule(corpus):
    """`tnt-demo` 与 `tnt-mfg-a` 的 `AS-003` 的 `rule_kind` 确实不同。

    这条测试的价值不在「现在不同」，在**将来谁把两边改成一样了它会红** ——
    逼那个人来看本文件的模块注释，而不是默默假设同一个编号就是同一条规则。
    真要让两边一致，先改这条测试并说明理由，别反过来。
    """
    ledger = corpus["scenarios/custom/ledger.json"]
    r4a = corpus["scenarios/refund/cases/case_r4a.json"]

    demo_kind = _rule_kind(ledger, "tnt-demo", "AS-003")
    mfg_kind = _rule_kind(r4a, "tnt-mfg-a", "AS-003")

    assert demo_kind == "wrong_item"
    assert mfg_kind == "artificial_damage_exclusion"
    assert demo_kind != mfg_kind, (
        "两个租户的 AS-003 的 rule_kind 变成一样了。若这是有意为之，请连带更新"
        " scenarios/custom/README.md 与 scenarios/refund/README.md 的撞号对照表，"
        "以及 docs/EXECUTION.md 附 B 那一格 —— 那几处都是按「两者毫无关系」写的")


def test_every_tenant_in_corpus_has_a_row_in_both_readme_tables(corpus):
    """语料里出现的每个 `tenant_id`，两份 README 的对照表里都有一行。

    只写「注意租户」没用，得让人查得到「这个租户的 AS-003 到底是什么」。
    """
    tenants = {row["tenant_id"] for payload in corpus.values() for row in payload["policy_rule"]}
    assert tenants, "语料里一个租户都没扫到 —— 扫描器坏了，不是语料干净了"

    for rel in READMES:
        text = (REPO / rel).read_text(encoding="utf-8")
        table_rows = _table_after_header(text, "AS-003 是什么")
        assert table_rows, f"{rel} 里找不到 AS-003 撞号对照表"
        joined = "\n".join(table_rows)
        for tenant in sorted(tenants):
            assert tenant in joined, (
                f"{rel} 的撞号对照表里没有租户 {tenant} 那一行 —— "
                "语料里有它，对照表里查不到，读者只能猜")


def test_ledger_as003_stays_wrong_item_not_an_evidence_rule(corpus):
    """`tnt-demo` 的 `AS-003` **不带**任何举证键 —— 它是发错货规则。

    方向词是 `stays`：这条钉的是「它保持不是举证规则」。
    实跑自定义入口命中 `AS-003@v1` 时，回帖里的这个编号与「证据」无关；
    要演示举证判据，得用 `scenarios/refund/` 那侧租户 `tnt-mfg-a` 的语料，别拿这条讲。
    """
    ledger = corpus["scenarios/custom/ledger.json"]
    body = next(
        json.loads(row["body"])
        for row in ledger["policy_rule"]
        if (row["tenant_id"], row["rule_no"]) == ("tnt-demo", "AS-003")
    )
    evidence_keys = {"requires_evidence_kinds", "min_evidence_count", "evidence_source"}
    assert not (evidence_keys & set(body)), (
        "tnt-demo 的 AS-003 长出了举证键。若这是有意为之，两份 README 的对照表与"
        " docs/EXECUTION.md 附 B 那一格都要跟着改 —— 否则「命中它 ≠ 举证生效」这句话就不成立了")
    assert body["rule_kind"] == "wrong_item"
    assert body["applies_when"]["reason_code"] == ["wrong_item"]
