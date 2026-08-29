"""W-1 语料（`scenarios/refund/`）的机器守卫 —— 它从此有消费方，分叉就红。

W-1 自己的账（BACKLOG `## task-W1` 第 3 条）写着那几份数据文件**零消费方**：
「造出来了但没有任何东西会因为它变红或变绿，一旦字段与 `schema.sql` / `kb_doc`
列清单分叉，不会有任何报错」。本文件就是那条账的守卫，守三层：

1. **列清单**。历史案例逐条对齐 `kb.DOC_COLUMNS`，政策语料逐条对齐
   `experiment.CORPUS_TABLES` 登记的四张表 —— 多一列、少一列、改个列名都当场红。
   不靠 `INSERT ... VALUES` 的位置对齐：位置错一列不报错，值会悄悄挪一格。
2. **取值域**。kind / outcome 必须落在 `maos.kb` 的常量里。写错的条目查得出来
   但归不了类，而错误发生在写入侧、暴露在几周后的检索侧。
3. **检索漏斗**。全量装库之后钉死「库存 -> 同租户 -> 七维预过滤后」三级数字。
   只钉候选集大小看不出预过滤砍掉了什么；只钉库存看不出跨租户那一半从来没进过
   候选集。三个数一起，融合排序才有话可说 —— 这正是 R5 要的那个「有话可说」。

**为什么历史案例不进 R5 的证据库**：核验器第 7 项要求库里每条 `history_case` 的
`source_case_id` 都能回查到一条 `biz_status='settled'` 的**本库** `refund_case`，
而外部导入的历史知识按定义没有这样一条记录；给它们凭空造 `refund_case` 行就是
伪造证据（铁律 3）。所以 R5 的库里只装政策那一半，24 条历史案例的检索质量在这里
验，账记在 BACKLOG `## task-X3`。
"""

from __future__ import annotations

import json
import os

import pytest

from maos import kb
from maos.core.store import SqliteStore
from maos.kb import experiment, retriever

TENANT_A = "tnt-mfg-a"
TENANT_B = "tnt-mfg-b"

#: 语料现状的三个规模数。它们不是「跑出来多少就写多少」——
#: 24/8 是 W-1 的 `_note` 自己声明的（历史案例 24 条，其中 8 条 failed，正好 1/3），
#: 16 是「4 条规则 x 2 租户 x 2 版本」。数字对不上说明语料被动过，该有人知道。
HISTORY_DOCS = 24
HISTORY_FAILED = 8
POLICY_RULES = 16

#: 装完整份语料之后的检索漏斗。三级都钉死：
#: 40 = 24 条历史案例 + 16 条政策；21 = 租户 A 的 13 + 8；
#: 7 = 再按渠道 / 区域 / SKU / 政策版本收窄之后剩下的（3 条历史 + 4 条 v1 政策）。
FUNNEL_TOTAL = HISTORY_DOCS + POLICY_RULES
FUNNEL_SAME_TENANT = 21
FUNNEL_AFTER_PREFILTER = 7

#: R5 用的那份检索上下文，逐字取自 `experiment` 的常量 —— 两处写死会各自漂。
R5_CONTEXT = {
    "tenant_id": experiment.TENANT_ID,
    "biz_type": experiment.BIZ_TYPE,
    "channel_id": experiment.CHANNEL_ID,
    "region": experiment.REGION,
    "sku": experiment.SKU,
    "policy_version": experiment.POLICY_VERSION,
}


@pytest.fixture
def store():
    s = SqliteStore()
    s.init_schema()
    kb.ensure_schema(s)
    return s


def _history_rows() -> list[dict]:
    """取历史案例，顺带过一遍列清单守卫 —— 读语料这一步就是校验这一步。"""
    payload = experiment.load_corpus(os.path.join("history", "history_cases.json"))
    return experiment._checked_rows(payload, "kb_doc", kb.DOC_COLUMNS)


def _load_history(target) -> int:
    """把 24 条历史案例装进 kb_doc。向量语料里恒为 null，落库时现算一份。"""
    rows = _history_rows()
    for row in rows:
        kb.upsert_doc(target, {
            **row,
            "embedding": retriever.embed(f"{row['title']} {row['body']}"),
        })
    return len(rows)


def _load_all(target) -> None:
    """整份语料：历史案例 + 政策投影。政策那一半复用 R5 自己那条装载路径。"""
    _load_history(target)
    experiment._seed_kb_from_corpus(target)


# ---------------------------------------------------------------------------
# 1. 列清单 —— 语料与消费方的列清单分叉，从前不会有任何报错
# ---------------------------------------------------------------------------
def test_history_corpus_columns_match_kb_doc_exactly():
    """历史案例逐条对齐 `kb.DOC_COLUMNS`。多一列少一列都红。"""
    rows = _history_rows()
    assert len(rows) == HISTORY_DOCS
    for idx, row in enumerate(rows):
        assert set(row) == set(kb.DOC_COLUMNS), f"kb_doc[{idx}] 的列清单漂了"


def test_history_corpus_column_guard_actually_fires():
    """守卫本身得会响 —— 只写断言不验断言，等于把守卫写在注释里。"""
    payload = {"kb_doc": [{c: None for c in kb.DOC_COLUMNS if c != "outcome"}]}
    with pytest.raises(ValueError, match="outcome"):
        experiment._checked_rows(payload, "kb_doc", kb.DOC_COLUMNS)

    payload = {"kb_doc": [{c: None for c in kb.DOC_COLUMNS} | {"多出来的列": 1}]}
    with pytest.raises(ValueError, match="多出来的列"):
        experiment._checked_rows(payload, "kb_doc", kb.DOC_COLUMNS)


def test_policy_corpus_columns_match_the_consumer_list():
    """政策语料四张表逐条对齐 `experiment.CORPUS_TABLES` 登记的列清单。"""
    payload = experiment.load_corpus(os.path.join("policy", "policy_rules.json"))
    counted = {table: len(experiment._checked_rows(payload, table, columns))
               for table, columns in experiment.CORPUS_TABLES}
    assert counted["policy_rule"] == POLICY_RULES
    assert counted["tenant"] == 2 and counted["channel"] == 4
    assert counted["product_snapshot"] == 4


# ---------------------------------------------------------------------------
# 2. 取值域 —— 写错的条目查得出来但归不了类
# ---------------------------------------------------------------------------
def test_history_corpus_values_stay_in_range():
    rows = _history_rows()
    assert {r["kind"] for r in rows} == {kb.KIND_HISTORY_CASE}
    assert {r["outcome"] for r in rows} <= set(kb.VALID_OUTCOMES)
    failed = [r for r in rows if r["outcome"] == kb.OUTCOME_FAILED]
    assert len(failed) == HISTORY_FAILED, "失败案例的比例被动过了"
    assert {r["tenant_id"] for r in rows} == {TENANT_A, TENANT_B}
    for row in rows:
        assert row["source_case_id"], "历史案例缺 source_case_id，核验器第 7 项要它"
        assert isinstance(json.loads(row["body"]), dict), "body 不是 JSON 对象"


def test_history_corpus_bodies_carry_no_planning_steps():
    """历史案例的 body 是叙述（situation/action/…），**不是**可照做的步骤清单。

    这条不是文风检查：`guardrails.apply_suggestions` 只认 body 里的 `steps`，
    语料哪天多出这个键，R5 的 DAG 就会凭空多出几步，而两版 diff 照样是绿的。
    """
    for row in _history_rows():
        assert "steps" not in json.loads(row["body"])


# ---------------------------------------------------------------------------
# 3. 检索漏斗 —— 三级数字一起看才说明问题
# ---------------------------------------------------------------------------
def test_full_corpus_loads_and_the_retrieval_funnel_holds(store):
    _load_all(store)

    total = kb.query(store, "SELECT COUNT(1) AS n FROM kb_doc")[0]["n"]
    same_tenant = kb.query(
        store, "SELECT COUNT(1) AS n FROM kb_doc WHERE tenant_id=?", (TENANT_A,))[0]["n"]
    candidates = retriever.prefilter(store, R5_CONTEXT)

    assert total == FUNNEL_TOTAL
    assert same_tenant == FUNNEL_SAME_TENANT, "同租户条数变了 —— 语料的租户分布被动过"
    assert len(candidates) == FUNNEL_AFTER_PREFILTER, (
        "七维预过滤后的候选集大小变了。变大可能是某一维失效了（跨维召回），"
        "变小可能是语料的维度值改了 —— 两种都要有人看一眼")
    kinds = {c["kind"] for c in candidates}
    assert kinds == {kb.KIND_HISTORY_CASE, kb.KIND_POLICY}, \
        "候选集里只剩一类知识 —— 融合排序又退回到没什么可排的状态"


def test_prefilter_wildcards_let_unscoped_policy_through(store):
    """政策投影把 channel/region/sku 留成 NULL，靠的是「文档侧 NULL = 通配」。

    照抄成具体值会把一条不限渠道的政策锁死在一个渠道上，症状是「换个渠道查就查不到
    政策了」，而且不报错。
    """
    experiment._seed_kb_from_corpus(store)
    other_channel = dict(R5_CONTEXT, channel_id="ch-dealer", sku="SKU-SRV-A2")
    got = {c["doc_id"] for c in retriever.prefilter(store, other_channel)}
    assert got == {c["doc_id"] for c in retriever.prefilter(store, R5_CONTEXT)}, \
        "换个渠道 / SKU 就查不到政策了 —— 通配没生效"


def test_doc_ids_are_globally_unique_across_tenants(store):
    """两个租户的 doc_id 不许重名。

    `kb_doc` 的主键是 `(tenant_id, doc_id)`，重名在表里不冲突 —— 正因如此它不报错。
    但 `KbRetrieved.docs[*].doc_id` 落进事件之后就指不到唯一一行了，
    「命中的到底是谁家那条」在证据里再也读不出来。
    """
    _load_all(store)
    rows = kb.query(store, "SELECT tenant_id, doc_id FROM kb_doc")
    per_tenant = {}
    for row in rows:
        per_tenant.setdefault(row["tenant_id"], set()).add(row["doc_id"])
    assert not (per_tenant[TENANT_A] & per_tenant[TENANT_B]), "两个租户有同名 doc_id"
    assert len({r["doc_id"] for r in rows}) == FUNNEL_TOTAL


def test_cross_tenant_never_retrieved_on_the_real_corpus(store):
    """租户 B 的 11 条历史案例 + 8 条政策，对租户 A 的查询必须完全不可见。"""
    _load_all(store)
    b_docs = {r["doc_id"] for r in kb.query(
        store, "SELECT doc_id FROM kb_doc WHERE tenant_id=?", (TENANT_B,))}
    assert len(b_docs) == FUNNEL_TOTAL - FUNNEL_SAME_TENANT

    hits = retriever.retrieve(store, {**R5_CONTEXT, "rule_no": experiment.RULE_NO,
                                      "keyword": "轴承 锈蚀 退款 财务核算"}, limit=50)
    assert hits, "本租户自己一条都没召回，这条测试就没在验跨租户"
    assert not (b_docs & {h["doc_id"] for h in hits}), "跨租户召回 —— 这是事故"


# ---------------------------------------------------------------------------
# 4. 四通道在真语料上各自有信号 —— 单测里验过的融合，在真语料上再验一次
# ---------------------------------------------------------------------------
def test_all_four_channels_fire_on_the_real_corpus(store):
    """规则编号 / 网关错误码 / 全文 / 向量，四个通道在真语料上都要点得着。

    自造的最小集里只有 1 条候选，四通道排给谁看都一样；这里的候选集有几条同租户
    知识，精确通道命中的那条必须排在只有文本相关的前面。
    """
    _load_all(store)
    gateway_rows = kb.query(
        store, "SELECT doc_id, rule_no, gateway_code FROM kb_doc"
               " WHERE tenant_id=? AND gateway_code IS NOT NULL ORDER BY doc_id",
        (TENANT_A,))
    assert gateway_rows, "语料里租户 A 没有带网关错误码的案例，本条无从验起"
    probe = gateway_rows[0]

    hits = retriever.retrieve(store, {
        "tenant_id": TENANT_A, "biz_type": "refund",
        "rule_no": probe["rule_no"], "gateway_code": probe["gateway_code"],
        "keyword": "退款 网关 失败"}, limit=20)

    by_id = {h["doc_id"]: h for h in hits}
    assert probe["doc_id"] in by_id, "精确通道双命中的那条没被召回"
    top = by_id[probe["doc_id"]]
    assert top["channels"]["rule_no"] == 1.0 and top["channels"]["gateway_code"] == 1.0
    assert hits[0]["doc_id"] == probe["doc_id"], \
        "两个精确通道都命中的那条没排第一 —— 融合把精确通道退化成模糊通道了"

    fired = {ch for h in hits for ch, score in h["channels"].items() if score > 0}
    assert fired == set(retriever.CHANNELS), f"这几个通道在真语料上一次都没点着：" \
                                             f"{set(retriever.CHANNELS) - fired}"


def test_retrieval_on_the_real_corpus_is_reproducible(store):
    """同一份语料连查两次，命中与分数逐条一致 —— 演示要能复现。"""
    _load_all(store)
    query = {**R5_CONTEXT, "rule_no": experiment.RULE_NO, "keyword": "轴承 锈蚀 退款"}
    first = [(h["doc_id"], h["score"]) for h in retriever.retrieve(store, query, limit=20)]
    second = [(h["doc_id"], h["score"]) for h in retriever.retrieve(store, query, limit=20)]
    assert first and first == second


# ---------------------------------------------------------------------------
# 5. R5 的装载路径本身
# ---------------------------------------------------------------------------
def test_r5_seeds_policy_corpus_and_never_seeds_history_cases(store):
    """R5 的库里只该有政策投影 —— 历史案例进去会让核验器第 7 项当场翻脸。"""
    assert experiment._seed_kb_from_corpus(store) == POLICY_RULES
    rows = kb.query(store, "SELECT kind, COUNT(1) AS n FROM kb_doc GROUP BY kind")
    assert {r["kind"]: r["n"] for r in rows} == {kb.KIND_POLICY: POLICY_RULES}

    projected = kb.get_doc(
        store, TENANT_A, f"kb-policy-{TENANT_A}-{experiment.RULE_NO}-v1")
    assert projected is not None, "R5 检索上下文用的那条规则没被投影进来"
    assert projected["rule_no"] == experiment.RULE_NO
    assert projected["policy_version"] == 1
    assert (projected["channel_id"], projected["region"], projected["sku"]) == (None,) * 3
    assert projected["source_case_id"] is None, "政策不是从某一单里沉淀出来的"


def test_missing_corpus_file_raises_instead_of_falling_back(monkeypatch):
    """语料不在就抛。回落到自造的最小集会让 R5 照常跑绿而候选集悄悄退回 1 条。"""
    monkeypatch.setattr(experiment, "CORPUS_ROOT", "/nonexistent/corpus/root")
    with pytest.raises(FileNotFoundError):
        experiment.load_corpus(os.path.join("policy", "policy_rules.json"))
