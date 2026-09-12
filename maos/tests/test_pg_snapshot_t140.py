"""同构比对的**分类判据**，以及 `model-usage.json` 那条 note 的现况（T140）。

这个文件守的全是「读证据的人会看到什么」，不是程序跑不跑得起来：

1. `suspicious_identical` 里曾恒报一条 `outcome.case_outcome.arrival_basis` ——
   钱没到账时它两束都是空串，而空串不是「生成出来的值恰好一样」，是这条路径上
   压根没生成过它。误报恰好落在 `drift` / `reject` 这两条**最该干净**的对照路径上。
2. 收窄之后最怕的反面：判据被废掉。所以这里既钉「两边都空不算可疑」，也钉
   「两边都有值且相同**仍然**算可疑」「`0` 不是空」。
3. 9/18 真跑日要拿 PolarDB 真实例的束与本机束比对。两边的库名 / host / 实例版本
   天然不同，时钟也对不齐 —— 这里钉住那些差异**进不了**「不符」栏，而业务字段
   （金额、状态、案号、审批人）不同**必须**仍然判红。

手搭束而不跑真束是有意的：这几条钉的是分类规则本身，跑一次真束要三十秒还要一个
PG 库，而规则是纯函数 —— 真束那一档由 `--all-paths --domain-backend postgres`
的四条 verdict 行盯着（T140 回执里有实跑）。

**两边的易变字段默认不同**（`_VOLATILE` 两套值）：真束就是两次真跑的产物，
`plan_id` 与 `computed_at` 本来就不会一样。让手搭束照着真束的样子长，才不会
把「判据在正常工作」当成测试失败。
"""

from __future__ import annotations

import json
import os
import sqlite3

import pytest

from scripts.make_case_bundle import collect_model_usage
from scripts.make_evidence import HEADER_PREFIX
from scripts.pg_case_snapshot import _is_empty, compare

CASE = {"tenant_id": "tnt-mfg-a", "case_id": "RC-2026-0904-001"}

#: 两次跑各自现生成的那几个值。`side` 只有 "left" / "right" 两个取值。
_VOLATILE = {
    "left": {"plan_id": "plan_9a7f1c2b0d3e", "computed_at": "2026-09-12T01:00:00+00:00"},
    "right": {"plan_id": "plan_4b8e2d5a6c1f", "computed_at": "2026-09-12T01:00:07+00:00"},
}


def _outcome(side: str, **over) -> dict:
    """一份 `outcome.json` 的最小真形状（字段名取自 `evidence/case-real-01/drift`）。"""
    volatile = _VOLATILE[side]
    doc = {
        **CASE,
        "plan_id": volatile["plan_id"],
        "case_outcome": {
            **CASE,
            "arrival": "not_arrived",
            # 钱没到账这条路上它恒为空串 —— 本文件的第一条就是为它写的。
            "arrival_basis": "",
            "customer_confirmation": "none",
            "manual_correction": "none",
            "complaint": "none",
            "evidence_complete": 1,
            "business_success": 0,
            "computed_at": volatile["computed_at"],
        },
        "biz_status": "submitted",
        "public_status": "processing",
        "payment_observations": [],
        "plan_state": "RUNNING",
        "note": "手搭的束，只给分类判据用。",
    }
    doc.update(over)
    return doc


def _write_json(path: str, doc: dict) -> None:
    """带出处首行地写 —— `load_evidence_json` 认这一行，缺了它当场判证据不合规。"""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"{HEADER_PREFIX} 2026-09-12T00:00:00+00:00 from t140-unit-test\n")
        fh.write(json.dumps(doc, ensure_ascii=False, indent=2) + "\n")


def _write_bundle(directory: str, outcome: dict, *, pg_tables: dict | None = None) -> str:
    os.makedirs(directory, exist_ok=True)
    _write_json(os.path.join(directory, "outcome.json"), outcome)
    if pg_tables is not None:
        _write_json(os.path.join(directory, "pg-tables.json"), pg_tables)
    return directory


@pytest.fixture()
def two_bundles(tmp_path):
    """造两束的工厂：给 (左边的 outcome, 右边的 outcome)，回比对结果。"""
    def build(left: dict, right: dict, *, left_pg=None, right_pg=None) -> dict:
        a = _write_bundle(str(tmp_path / "sqlite"), left, pg_tables=left_pg)
        b = _write_bundle(str(tmp_path / "pg"), right, pg_tables=right_pg)
        return compare(a, b)
    return build


def _field(doc: dict, column: str, name: str) -> dict | None:
    return next((x for x in doc[column] if x["field"] == name), None)


# --------------------------------------------------------- 收窄：两边都空不算可疑
def test_an_empty_volatile_field_on_both_sides_is_not_suspicious(two_bundles):
    """`arrival_basis` 两束都空 —— 不进可疑栏，但**仍然留在** differ 栏。

    留在 differ 栏是要点：收窄的是「算不算可疑」这一步，不是把字段从清单里摘掉。
    真到账的那条路上它照样每跑一次都不同、照样被盯着（见下一条）。
    """
    doc = two_bundles(_outcome("left"), _outcome("right"))

    assert doc["verdict"] == "isomorphic"
    assert doc["suspicious_identical"] == []
    basis = _field(doc, "differ_by_design", "outcome.case_outcome.arrival_basis")
    assert basis is not None, "不许把 basis 从 VOLATILE_KEYS 里摘掉来消这条误报"
    assert basis["both_empty"] is True
    assert basis["differs"] is False


def test_a_non_empty_volatile_field_identical_on_both_sides_is_still_suspicious(two_bundles):
    """真到账时两束的 `arrival_basis` 一字不差 = 有一束不是真跑出来的，必须报。"""
    same = "payment_observation:gw_deadbeef@2026-09-12T01:00:00+00:00"
    left, right = _outcome("left"), _outcome("right")
    for side in (left, right):
        side["case_outcome"]["arrival_basis"] = same

    doc = two_bundles(left, right)

    assert [s["field"] for s in doc["suspicious_identical"]] == [
        "outcome.case_outcome.arrival_basis"]


def test_zero_is_not_treated_as_empty(two_bundles):
    """`0` 是有内容的值，不是「没生成过」。两束都是 0 的易变字段照样报可疑。

    这一条钉的是 `_is_empty` 的边界：写成 `if not value` 的话 `0` / `False` 会被
    当成空，于是「两束的耗时一模一样」这种真该报的事就被吞了。
    """
    left, right = _outcome("left"), _outcome("right")
    for side in (left, right):
        side["case_outcome"]["wall_ms"] = 0

    doc = two_bundles(left, right)

    assert [s["field"] for s in doc["suspicious_identical"]] == [
        "outcome.case_outcome.wall_ms"]
    assert _is_empty(0) is False and _is_empty(False) is False
    assert _is_empty("") is True and _is_empty([]) is True and _is_empty({}) is True


# ------------------------------------------------- 跨实例：该红的仍红，该忍的不红
def test_a_business_field_difference_is_still_a_mismatch(two_bundles):
    """故意改一个业务字段 —— 比对**必须**红。

    这是 T140 收窄判据时唯一不能松的那条线：跨实例比对之所以有意义，全靠它。
    金额 / 状态 / 案号 / 审批人不同而 verdict 还绿，这份证据就一文不值了。
    """
    right = _outcome("right")
    right["biz_status"] = "settled"                 # 同一案子在两个实例上状态不同
    right["case_outcome"]["business_success"] = 1

    doc = two_bundles(_outcome("left"), right)

    assert doc["verdict"] == "divergent"
    bad = {m["field"] for m in doc["mismatches"]}
    assert "outcome.biz_status" in bad
    assert "outcome.case_outcome.business_success" in bad


def test_instance_level_facts_never_enter_the_comparison(two_bundles):
    """库名 / host / 端口 / 实例版本串不同，verdict 仍是 isomorphic。

    9/18 真跑日那一比（PolarDB 真实例的束 vs 本机 PG 的束）成立就靠这件事：
    比对只读 `outcome.json` / `skills.json` / `event-chain.json` 三份**语义**产物，
    实例级事实只落在 `pg-tables.json` 的 `source` 段里，从来不是判据。
    """
    local = {"source": {"backend": "postgres", "host": "127.0.0.1", "port": 5432,
                        "database": "maos_t140_a", "user": "maos"},
             "tables_present": 19}
    live = {"source": {"backend": "postgres", "host": "polardb.example.internal",
                       "port": 1921, "database": "maos_prod", "user": "maos_app"},
            "tables_present": 19}

    doc = two_bundles(_outcome("left"), _outcome("right"), left_pg=local, right_pg=live)

    assert doc["verdict"] == "isomorphic"
    assert doc["mismatches"] == []
    touched = doc["identical_fields"] + [x["field"] for x in doc["differ_by_design"]]
    assert not any("database" in f or "host" in f or f.startswith("pg_tables")
                   for f in touched)


def test_a_clock_skew_between_instances_lands_in_the_differ_column(two_bundles):
    """两个实例的时钟差八小时 —— 落 differ 栏，不落「不符」栏。

    真实例与本机不在同一台机器上，`*_at` 必然对不齐。它们本来就是「每跑一次都
    现生成」的那一族，所以这条不是给时钟开的后门，是那一族分类的自然结果。
    """
    right = _outcome("right")
    right["case_outcome"]["computed_at"] = "2026-09-12T09:00:00+08:00"

    doc = two_bundles(_outcome("left"), right)

    assert doc["verdict"] == "isomorphic"
    skew = _field(doc, "differ_by_design", "outcome.case_outcome.computed_at")
    assert skew is not None and skew["differs"] is True
    assert skew["both_empty"] is False


# ---------------------------------------------------- note：不许自己打自己的脸
def test_live_model_usage_note_no_longer_denies_the_rows_beneath_it():
    """`--live-model` 那束的 note 不许再说「圆桌不落 model_usage」。

    它曾经这么写，而同一份文件的 `rows` 里那五条圆桌用量逐字都在 —— 读的人若信了
    note，会以为那些 token 是别处来的。而 `--live-model` 恰恰是**唯一**一束有真
    圆桌用量的，也正是真跑日要产的那一束。
    """
    doc = collect_model_usage(None, set(), live=True)
    note = doc["note"]

    assert "不落 model_usage" not in note
    assert "T113" not in note and "137c960" not in note
    assert "rows" in note, "要正面说清：那几次调用就在这份文件的 rows 里"
    assert "roundtable_traces" in note, "还要说清归属：trace.json 的哪一段认领了它们"


def test_scripted_model_usage_note_no_longer_denies_the_rows_beneath_it():
    """Scripted 那束的 note 也不许说「这张表是空的」—— 它下面就有一行。

    整合期 p10-f 补：本轨改掉了 `--live-model` 那句自打脸的 note，却漏了紧挨着它
    上面的 Scripted 那句。四束已提交的 Scripted 证据全都是 `"count": 1` 配
    「所以这张表是空的」—— 与本轨要消灭的是同一类缺陷。

    原来那条测试用 `tables=set()` 把 rows 强制构造成空再断言它为空，是恒真的，
    反而给这句假话发了绿灯。这里改成**喂一行真形状的 model_usage**，让 note
    与 rows 当面对质。
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE model_usage (seq INTEGER PRIMARY KEY, trace_id TEXT, "
        "plan_id TEXT, agent_role TEXT, call_site TEXT, model TEXT, "
        "tokens_in INTEGER, tokens_out INTEGER, estimated INTEGER)"
    )
    conn.execute(
        "INSERT INTO model_usage VALUES (1, 'trace_x', 'plan_x', 'manager', "
        "'maos/agents/base.py::BaseAgent.ask', 'scripted-strong', 133, 909, 1)"
    )

    doc = collect_model_usage(conn, {"model_usage"}, live=False)
    note = doc["note"]

    assert doc["model_mode"] == "scripted"
    assert doc["count"] == 1 and doc["rows"], "Scripted 束里本就有行，不是空的"
    assert "这张表是空的" not in note and "空数组是事实" not in note
    assert "estimated" in note, "要说清这一行是本地估算，不是真实计费"
    assert "圆桌" in note, "要说清真正不记账的是圆桌，不是整张表"
