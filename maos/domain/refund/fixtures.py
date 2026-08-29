"""对照 case 的靶场装载 —— 把 `scenarios/refund/**` 的 JSON 灌进库。

**只 INSERT，不建表**（铁律 2）。对照组要的五张表 —— `tenant` / `channel` /
`order_snapshot` / `product_snapshot` / `policy_rule` —— 在 `schema.sql` 里都有，
`kb_doc` 在 `maos/kb/schema.sql` 里。本模块一条 DDL 都不写，建表一律走两个域各自的
`ensure_schema()`。

`refund_case` 也不在这里建：那张表全系统只有 `guard.create_case()` 一个入口，
对照流程一律经 `refund.intake` 建案（`objects.execute()` 见到它的写语句会直接抛
`BypassedGuardError`，绕不过去）。所以 case json 里的 `case` 块由调用方拿去当
`case_seed`，本模块不碰。

## 列清单守卫复用 R5 那一份

`_checked_rows()` 逐行校验语料的列清单，多一列少一列当场抛。它已经在
`maos/kb/experiment.py` 里，本模块**复用**而不是另写一份 —— 两份列清单迟早分叉，
而分叉的症状是「值悄悄错位一列」，不报错。本模块只把 R5 没消费的两张表
（`order_snapshot` / `customer_evidence`）补进登记表。

## 历史案例按晋升规则分流

语料里 24 条历史案例的 `kind` 全是 `history_case`（数据侧只记「这是一条历史案例」）。
落库时按 `outcome` 分流：`success` 进 `history_case`，`failed` 进 `failure_hint`
—— 后者**不作为规划正例**（`kb.POSITIVE_KINDS` 不含它，`guardrails.apply_suggestions`
在合并建议前就把它滤掉了）。分流口径与 `guardrails.classify_case` 同一份：
那边管「本库跑出来的案子该进哪一类」，这边管「外部导入的历史该进哪一类」，
判据都是「外部结果明不明确」。

**为什么在装载侧分流而不是在检索侧过滤**：写错 kind 的条目查得出来但归不了类，
而错误发生在写入侧、暴露在几周后的检索侧，是最难回溯的一类脏数据（`kb/schema.sql`
把取值域写进 CHECK 也是这个理由）。
"""

from __future__ import annotations

import os
from typing import Any

#: 三组对照的五份 case，按组归拢。值是 `scenarios/refund/cases/` 下的文件名。
#: 组号与 `_expected.pair_id` 一致，一处改名两处都对不上时当场看得出来。
CASE_FILES: dict[str, tuple[str, ...]] = {
    "R3": ("case_r3a.json", "case_r3b.json"),
    "R4": ("case_r4a.json", "case_r4b.json"),
    "R6": ("case_r6.json",),
}

#: case json 里**由本模块灌库**的表及其列清单。
#:
#: `tenant` / `channel` / `product_snapshot` / `policy_rule` 四张与 R5 的
#: `experiment.CORPUS_TABLES` 同一份（在 `case_tables()` 里取，不抄），
#: 这里只补 R5 没消费的两张。`case` 块不在其列 —— 见模块 docstring。
EXTRA_CASE_TABLES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("order_snapshot", ("tenant_id", "order_id", "version", "sku", "amount_paid",
                        "paid_at", "channel_id", "policy_version_at_order",
                        "payload_json", "read_at")),
    ("customer_evidence", ("tenant_id", "case_id", "evidence_id", "kind", "uri",
                           "digest", "submitted_at")),
)


def _experiment():
    """R5 的语料装载器。**局部 import**：它是证据生成器，模块级 import 会把整个
    对照实验挂到退款域的 import 图上（`flows/scenario_6.py::_seed_kb` 同一口径）。"""
    from maos.kb import experiment
    return experiment


def case_tables() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """case json 里全部可灌库的表 + 列清单。R5 那四张 + 本模块补的两张。"""
    return (*_experiment().CORPUS_TABLES, *EXTRA_CASE_TABLES)


# ------------------------------------------------------------------ 读语料
def load_case(name: str) -> dict:
    """读一份对照 case。文件缺失就抛 —— 靶场数据不在了，这一组的结论不成立。

    不做「读不到就回落到自造的最小集」：那样对照会照常跑绿，而变量控制悄悄失效。
    """
    return _experiment().load_corpus(os.path.join("cases", name))


def expected_of(payload: dict) -> dict:
    """取 case 的 `_expected` 块 —— **判据的唯一来源**。

    对照实验的判据一律读它，代码里不另写一份期望值：两份期望值一定会漂，
    漂了之后测试绿而结论错，比红更坏。
    """
    exp = payload.get("_expected")
    if not isinstance(exp, dict) or not exp:
        raise KeyError("case json 缺 `_expected` 块 —— 对照实验没有判据可读")
    return exp


def case_seed_of(payload: dict) -> dict:
    """取 case json 的 `case` 块，即 `refund.intake` 的 `case_seed` 入参。"""
    seed = payload.get("case")
    if not isinstance(seed, dict) or not seed:
        raise KeyError("case json 缺 `case` 块 —— 建不出 refund_case")
    return dict(seed)


def evidence_signals_of(payload: dict) -> list[dict]:
    """把 case 自带的 `customer_evidence` 行翻成 `refund.intake` 的信号。

    证据不由本模块直接灌库：`refund.intake` 见到带 `uri` 的信号就会写
    `customer_evidence`，两边都写等于同一份证据有两条落库路径。翻成信号让它
    走**唯一那条**，而 `evidence_id` / `uri` / `digest` 逐字取自语料，不是现编的。
    """
    rows = payload.get("customer_evidence")
    if not isinstance(rows, list):
        return []
    return [{
        "source": "客户上传", "kind": str(r.get("kind") or "attachment"),
        "severity": "major",
        "title": f"客户提交的证据 {r.get('evidence_id')}",
        "detail": f"{r.get('kind')}：{r.get('uri')}",
        "uri": r.get("uri"), "digest": r.get("digest"),
        "evidence_id": r.get("evidence_id"),
    } for r in rows if isinstance(r, dict)]


# ------------------------------------------------------------------ 灌库
def seed_case(store: Any, payload: dict) -> dict[str, int]:
    """把一份 case json 里的外部快照与政策灌进退款域的表。返回各表落了几行。

    灌的全是**外部系统快照**：MAOS 执行前读到的那一版，不是外部系统的当前值
    （铁律 8）。`customer_evidence` 在登记表里但由 `refund.intake` 落库，
    这里跳过它 —— 见 `evidence_signals_of`。
    """
    from maos.domain.refund import objects

    objects.ensure_schema(store)
    experiment = _experiment()
    counted: dict[str, int] = {}
    for table, columns in case_tables():
        if table == "customer_evidence" or table not in payload:
            continue
        rows = experiment._checked_rows(payload, table, columns)
        marks = ", ".join("?" for _ in columns)
        sql = (f"INSERT OR REPLACE INTO {table} ({', '.join(columns)})"
               f" VALUES ({marks})")
        for row in rows:
            objects.execute(store, sql, tuple(row[c] for c in columns))
        counted[table] = len(rows)
    return counted


def seed_policy_corpus(store: Any) -> dict[str, int]:
    """把 `policy/policy_rules.json` 的 16 条政策 + 租户/渠道/商品灌进退款域四张表。

    直接复用 R5 那条装载路径：五份 case 各自内联了自己用到的 `policy_rule` 行，
    而唯一的事实源是 `policy/policy_rules.json`（`scenarios/refund/README.md`
    的自校验第 3 条守着两处不分叉）。两处各写一套装载，迟早在列清单上分叉。
    """
    from maos.domain.refund import objects

    objects.ensure_schema(store)
    return _experiment()._seed_domain_from_corpus(store)


def seed_history_kb(store: Any) -> dict[str, int]:
    """把 24 条历史案例投影进 `kb_doc`，按晋升规则分流。返回各 kind 落了几条。

    `outcome='success'` -> `history_case`（规划正例）；
    `outcome='failed'`  -> `failure_hint`（只提示「哪类组合需要额外步骤」，
    **不作为规划正例**，`kb.POSITIVE_KINDS` 不含它）。

    租户**原样保留**（tnt-mfg-a / tnt-mfg-b），不改写成调用方的租户：
    `kb_doc` 的主键是 `(tenant_id, doc_id)`，改写会让两个租户的同名文档静默塌成一条，
    更要紧的是租户是阶段一最硬的一维，给别人家的知识贴上本租户的标签，
    等于亲手废掉「跨租户永不召回」这条约束（R3 那一组要证明的正是它）。

    `embedding` 语料里恒为 null，落库时按当前嵌入实现现算 —— 语料里预置一串数
    等于把「用哪个嵌入模型」这个决定提前做掉。
    """
    from maos import kb
    from maos.kb import retriever

    experiment = _experiment()
    payload = experiment.load_corpus(os.path.join("history", "history_cases.json"))
    rows = experiment._checked_rows(payload, "kb_doc", kb.DOC_COLUMNS)

    kb.ensure_schema(store)
    counted: dict[str, int] = {}
    for row in rows:
        kind = (kb.KIND_HISTORY_CASE if row.get("outcome") == kb.OUTCOME_SUCCESS
                else kb.KIND_FAILURE_HINT)
        kb.upsert_doc(store, {
            **row,
            "kind": kind,
            "embedding": retriever.embed(f"{row['title']} {row['body']}"),
        })
        counted[kind] = counted.get(kind, 0) + 1
    return counted


def seed_policy_kb(store: Any) -> int:
    """把 16 条政策投影进 `kb_doc`（`kind='policy'`）。返回落库条数。

    复用 R5 的 `seed_kb_corpus`，不另写一套投影：两套迟早在字段口径上分叉，
    而症状只是「候选集少了些」，不报错也没人看得出。
    """
    from maos import kb

    kb.ensure_schema(store)
    return _experiment().seed_kb_corpus(store)
