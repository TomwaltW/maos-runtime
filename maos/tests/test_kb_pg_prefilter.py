"""T115 的机器验收（三）—— **知识层的阶段一预过滤在 PostgreSQL 上跑得对**。

没库就 skip，绝不红。起库见 `test_refund_domain_pg.py` 的 docstring。

## 这一束在守什么

在本轨之前，PG 上根本没有 `kb_doc` 这张表 —— 只有 `pg_schema.sql` 里那张没有租户列
的参考表 `kb_doc_pg`（T115 已退役）。也就是说「七维预过滤」这件事**在 PG 上一次都
没跑过**，评委看到的是「PolarDB 连通过」，不是「知识层跑在 PolarDB 上」。

补这一刀之后最要紧的不是「能不能查出东西」，而是**查出来的东西跟 SQLite 上一不一样**：

* **`tenant_id` 是硬约束不是打分项**。缺它一律空候选集；回落成「全租户检索」
  在单租户演示里看不出任何异常，多租户上线当天泄漏。
* **文档侧 NULL = 通配**（不限渠道的政策对任何渠道都算候选）。这条语义在 PG 上
  靠的是 `(col IS NULL OR col = %s)`，三值逻辑写错的话结果会**少召回**，而不报错。
* **顺序即语义**。`PREFILTER_FIELDS` 的字段顺序与四通道权重是写进 README 和 PPT
  的口径，本轨一个字都不许改 —— 所以这里也钉一条。

第 4 节是本文件的重点：同一份语料、同一组查询，**两个后端逐条同结果**。
只测 PG 自己「有结果」是不够的 —— 换后端把召回悄悄改小是这一层最典型的失效，
而它不报错。
"""

from __future__ import annotations

import functools
import os

import pytest

from maos import kb
from maos.core.store import SqliteStore
from maos.kb import retriever
from maos.store.pg_store import DSN_ENV, SQLITE_DIALECT_ENV, PgStorePort

TENANT_A = "tnt-t115-a"
TENANT_B = "tnt-t115-b"

#: 三条语料，刚好把七维、NULL 通配与跨租户三件事都盖上。
#: `d2` 的五个窄维度全是 NULL —— 它是「不限渠道的政策」，任何查询都该召回。
DOCS = (
    dict(tenant_id=TENANT_A, doc_id="t115-d1", biz_type="refund",
         channel_id="ch-tmall", region="CN", sku="sku-1", policy_version=1,
         workflow_version=1, rule_no="AS-101", gateway_code="0000", kind="policy",
         title="七天无理由", body="gateway timeout retry twice", outcome=None,
         source_case_id=None, created_at="2026-01-01T00:00:00+00:00"),
    dict(tenant_id=TENANT_A, doc_id="t115-d2", biz_type="refund", channel_id=None,
         region=None, sku=None, policy_version=None, workflow_version=None,
         rule_no="AS-102", gateway_code=None, kind="history_case",
         title="回执延迟", body="gateway timeout then settled", outcome="success",
         source_case_id="case-9", created_at="2026-01-02T00:00:00+00:00"),
    dict(tenant_id=TENANT_B, doc_id="t115-d3", biz_type="refund",
         channel_id="ch-tmall", region="CN", sku="sku-1", policy_version=1,
         workflow_version=1, rule_no="AS-101", gateway_code="0000", kind="policy",
         title="别家租户的同名政策", body="other tenant policy", outcome=None,
         source_case_id=None, created_at="2026-01-03T00:00:00+00:00"),
)

#: 两个后端要逐条比对的查询。最后两条刻意各打一个「窄维度」，验 NULL 通配。
QUERIES = (
    {"tenant_id": TENANT_A},
    {"tenant_id": TENANT_B},
    {"tenant_id": "tnt-does-not-exist"},
    {"tenant_id": TENANT_A, "biz_type": "refund"},
    {"tenant_id": TENANT_A, "biz_type": "refund", "channel_id": "ch-tmall",
     "region": "CN", "sku": "sku-1", "policy_version": 1, "workflow_version": 1},
    {"tenant_id": TENANT_A, "channel_id": "ch-jd"},
    {"tenant_id": TENANT_A, "sku": "sku-9"},
    {"tenant_id": TENANT_A, "policy_version": 2},
)


@functools.lru_cache(maxsize=1)
def _live_dsn() -> str | None:
    """探一次：DSN 配了吗、连得上吗。连不上就是没库，不是失败。

    **必须在 collection 期求值**（模块级 `pytestmark` 里），
    理由见 `conftest._no_ambient_store_env` 的 docstring。
    """
    dsn = os.environ.get(DSN_ENV, "")
    if not dsn:
        return None
    port = PgStorePort(dsn)
    try:
        port.connect()
    except Exception:                                  # noqa: BLE001 —— 探测不该炸收集
        return None
    finally:
        port.close()
    return dsn


pytestmark = pytest.mark.skipif(
    _live_dsn() is None,
    reason=f"没有可连的 PG：{DSN_ENV} 未设或连不上。起库见 test_refund_domain_pg.py。",
)


def _seed(store) -> None:
    for doc in DOCS:
        kb.upsert_doc(store, {
            **doc,
            "embedding": retriever.embed(f"{doc['title']} {doc['body']}"),
        })


@pytest.fixture()
def pg(monkeypatch: pytest.MonkeyPatch) -> PgStorePort:
    """一张灌好语料的 PG 上的 `kb_doc`。用完把三张表删掉，不给下一次留脏数据。

    DSN 要自己 setenv 回来，理由同 `test_refund_domain_pg.py::pg_store`。
    方言开关也显式打开，不吃 ambient —— 它决定这个端口收不收 SQLite 方言的 SQL。
    """
    monkeypatch.setenv(DSN_ENV, _live_dsn() or "")
    monkeypatch.setenv(SQLITE_DIALECT_ENV, "1")
    port = PgStorePort()
    for table in ("kb_doc_fts", "kb_doc", "kb_schema_version"):
        port.execute(f"DROP TABLE IF EXISTS {table}", ())
    kb.ensure_schema(port)
    _seed(port)
    yield port
    for table in ("kb_doc_fts", "kb_doc", "kb_schema_version"):
        port.execute(f"DROP TABLE IF EXISTS {table}", ())
    port.close()


@pytest.fixture()
def local() -> SqliteStore:
    """同一份语料在 SQLite 上的对照组。"""
    store = SqliteStore(":memory:")
    store.init_schema()
    kb.ensure_schema(store)
    _seed(store)
    return store


# --------------------------------------------------------------- 1. 建表
def test_kb_doc_exists_on_postgres_with_all_prefilter_columns(pg: PgStorePort) -> None:
    """`kb_doc` 在 PG 上与 `maos/kb/schema.sql` 同构：七维 + 四个附加列 + `id`。

    少一列不报错，只是阶段一在 PG 上按不同的维度过滤 —— 召回悄悄变了。
    """
    assert kb.has_kb_table(pg), "has_kb_table 在 PG 上答错了 —— 它问的是 sqlite_master？"

    rows = pg.query(
        "SELECT column_name AS name FROM information_schema.columns"
        " WHERE table_schema = current_schema() AND table_name = 'kb_doc'", ())
    have = {r["name"] for r in rows}

    assert set(retriever.PREFILTER_FIELDS) <= have, (
        f"七维预过滤列少了：{sorted(set(retriever.PREFILTER_FIELDS) - have)}")
    assert {"rule_no", "gateway_code", "kind", "outcome"} <= have
    assert "id" in have, "F-2 口径的 id 列没建出来 —— 端口那两条检索通道会恒抛"


def test_generated_id_column_matches_doc_row_id(pg: PgStorePort) -> None:
    """`kb_doc.id` 与 `kb.doc_row_id()` 是同一份口径，两边漂了 id 就对不上号。"""
    doc = kb.get_doc(pg, TENANT_A, "t115-d1")

    assert doc is not None
    assert doc["id"] == kb.doc_row_id(TENANT_A, "t115-d1")


def test_ensure_schema_is_idempotent_on_postgres(pg: PgStorePort) -> None:
    kb.ensure_schema(pg)
    kb.ensure_schema(pg)

    assert kb.applied_schema_version(pg) == kb.KB_SCHEMA_VERSION
    assert len(kb.list_docs(pg)) == len(DOCS)


def test_upsert_replay_does_not_duplicate(pg: PgStorePort) -> None:
    """重放同一条知识仍是一行 —— `INSERT OR REPLACE` 翻成 ON CONFLICT 之后
    还得是**覆盖**；退成裸 INSERT 会攒重复行，BM25 把同一条知识数好几次。"""
    _seed(pg)

    assert len(kb.list_docs(pg)) == len(DOCS)
    assert pg.query("SELECT count(*) AS n FROM kb_doc_fts", ())[0]["n"] == len(DOCS)


# ---------------------------------------------------- 2. 租户是硬约束
def test_no_tenant_id_returns_empty(pg: PgStorePort) -> None:
    """不给租户就没有候选集。回落成「全租户检索」是最危险的一种默认值。"""
    assert retriever.prefilter(pg, {}) == []
    assert retriever.prefilter(pg, {"tenant_id": ""}) == []
    assert retriever.prefilter(pg, {"biz_type": "refund"}) == []


def test_cross_tenant_is_never_recalled(pg: PgStorePort) -> None:
    """B 租户那条与 A 的语料同 biz_type / 同渠道 / 同 sku / 同 rule_no，
    只有 tenant_id 不同 —— 它一次都不许出现在 A 的候选集里。"""
    for query in QUERIES:
        if query["tenant_id"] != TENANT_A:
            continue
        got = {r["doc_id"] for r in retriever.prefilter(pg, query)}
        assert "t115-d3" not in got, f"跨租户召回了：{query}"


# ------------------------------------------------------ 3. 七维漏斗
def test_seven_dimension_funnel_narrows_as_expected(pg: PgStorePort) -> None:
    """逐维收窄，且**文档侧 NULL = 通配**：`d2` 五个窄维度全 NULL，一直在候选里。"""
    def ids(query: dict) -> list[str]:
        return [r["doc_id"] for r in retriever.prefilter(pg, query)]

    assert ids({"tenant_id": TENANT_A}) == ["t115-d1", "t115-d2"]
    assert ids({"tenant_id": TENANT_A, "biz_type": "refund"}) == ["t115-d1", "t115-d2"]
    # 渠道对不上：d1 被滤掉，d2（channel_id IS NULL）留下。
    assert ids({"tenant_id": TENANT_A, "channel_id": "ch-jd"}) == ["t115-d2"]
    assert ids({"tenant_id": TENANT_A, "sku": "sku-9"}) == ["t115-d2"]
    assert ids({"tenant_id": TENANT_A, "policy_version": 2}) == ["t115-d2"]


def test_prefilter_field_order_is_untouched() -> None:
    """预过滤字段顺序与四通道权重是写进 README 与 PPT 的口径，本轨一个字不许改。"""
    assert retriever.PREFILTER_FIELDS == (
        "tenant_id", "biz_type", "channel_id", "region", "sku",
        "policy_version", "workflow_version")
    assert retriever.DEFAULT_WEIGHTS == {
        "rule_no": 0.35, "gateway_code": 0.25, "fts": 0.20, "vector": 0.20}


# ------------------------------------------- 4. 两个后端逐条同结果（重点）
def test_prefilter_matches_sqlite_row_for_row(pg: PgStorePort, local: SqliteStore) -> None:
    """同一份语料、同一组查询，两个后端的候选集**逐条相同**（含顺序）。

    只验 PG 侧「有结果」是不够的：换后端把召回悄悄改小是这一层最典型的失效，
    而它既不报错也不会有人去比。顺序也要比 —— 阶段二按候选集打分，候选集的
    顺序会影响同分时的次序。
    """
    for query in QUERIES:
        on_pg = [r["doc_id"] for r in retriever.prefilter(pg, query)]
        on_sqlite = [r["doc_id"] for r in retriever.prefilter(local, query)]

        assert on_pg == on_sqlite, f"两个后端候选集不同：{query} PG={on_pg} SQLite={on_sqlite}"


def test_prefilter_limit_is_honoured_on_both(pg: PgStorePort, local: SqliteStore) -> None:
    """候选集上限两边同样生效 —— PG 的 `LIMIT %s` 走的是参数绑定。"""
    assert len(retriever.prefilter(pg, {"tenant_id": TENANT_A}, limit=1)) == 1
    assert (retriever.prefilter(pg, {"tenant_id": TENANT_A}, limit=1)
            == retriever.prefilter(local, {"tenant_id": TENANT_A}, limit=1))


# --------------------------------------------------------- 5. 全文通道
def test_port_fts_channel_answers_on_postgres(pg: PgStorePort) -> None:
    """英文查询走 PG 的 tsvector —— 这条通道**真的被问到了**，不是悄悄退化。

    `fts_search` 返回的是 F-2 口径的源表主键（`tenant_id:doc_id`），不是 doc_id。
    """
    hits = pg.fts_search("kb_doc", "body", kb.fts_text("timeout"), 5)

    assert {doc_id for doc_id, _ in hits} == {
        kb.doc_row_id(TENANT_A, "t115-d1"), kb.doc_row_id(TENANT_A, "t115-d2")}
    scores = [score for _, score in hits]
    assert scores == sorted(scores, reverse=True), "分数必须越大越相关、降序返回"


def test_chinese_query_degrades_instead_of_pretending(pg: PgStorePort) -> None:
    """PG 内置配置一个都没有中文分词器 —— 本机没装 zhparser，中文查询**抛**而不是
    安静地返回空集。「后端没准备好」不许伪装成「没命中」（F-2 原话）。

    这是既有行为，不是本轨要「修」的东西：检索器接住它之后退化走本地通道，
    中文召回照常。真实例上装了 zhparser 的那条路是 9/18 真跑日的事。
    """
    if pg.fts_config().lower() not in ("simple",):
        pytest.skip(f"这个库配的是 {pg.fts_config()}，不是内置的 simple")

    with pytest.raises(LookupError, match="中日韩"):
        pg.fts_search("kb_doc", "body", "退款政策", 5)


# --------------------------------------------------------------- 6. 靶场
def test_fixtures_seed_history_kb_lands_on_postgres(pg: PgStorePort) -> None:
    """`fixtures.seed_history_kb()` 把 24 条历史案例灌进 PG 的 `kb_doc`（派单 §3.5）。

    T115 时本条是 strict xfail：语料的 `workflow_version` 是 '1.0.0' 这类版本串，
    PG 的 INTEGER 列当场拒。T118 把语料整数化（1.0.0 -> 1、1.1.0 -> 2）之后它转绿，
    整合期（2026-09-10）按 strict 的约定把壳摘掉 —— 24 条历史在 PG 上真灌得进去了。

    分流口径不因换后端而变：`outcome='success'` -> `history_case`（规划正例），
    `failed` -> `failure_hint`（**不是**正例）。
    """
    from maos.domain.refund import fixtures

    counted = fixtures.seed_history_kb(pg)

    assert counted, "一条历史案例都没灌"
    assert set(counted) <= {kb.KIND_HISTORY_CASE, kb.KIND_FAILURE_HINT}
    landed = {d["doc_id"] for d in kb.list_docs(pg, kind=kb.KIND_HISTORY_CASE)}
    assert landed, "PG 上查不到 history_case"


def test_sqlite_silently_accepts_a_semver_in_an_integer_column() -> None:
    """把上面那条分歧的**成因**也钉住：SQLite 收下了一个不是整数的整数列。

    这不是「PG 太严」，是 SQLite 的类型亲和性把一处数据缺陷藏了几个月 ——
    `workflow_version` 在库里是 INTEGER，语料给的是 `'1.0.0'`。藏着的后果不止是
    换后端时炸：`retriever.prefilter` 拿 `workflow_version = ?` 去比的时候，
    比的是字符串还是整数取决于**存进去的是什么**，而两边都不报错。
    """
    store = SqliteStore(":memory:")
    store.init_schema()
    kb.ensure_schema(store)

    kb.upsert_doc(store, {**DOCS[0], "doc_id": "t115-semver",
                          "workflow_version": "1.0.0"})

    assert kb.get_doc(store, TENANT_A, "t115-semver")["workflow_version"] == "1.0.0"


def test_fixtures_seed_policy_kb_lands_on_postgres(pg: PgStorePort) -> None:
    """16 条政策也灌得进 PG，且租户**原样保留**（跨租户永不召回靠的正是它）。"""
    from maos.domain.refund import fixtures

    count = fixtures.seed_policy_kb(pg)

    assert count == 16
    tenants = {d["tenant_id"] for d in kb.list_docs(pg, kind=kb.KIND_POLICY)}
    assert {"tnt-mfg-a", "tnt-mfg-b"} <= tenants, "政策的租户被改写了"


def test_retrieve_end_to_end_on_postgres(pg: PgStorePort) -> None:
    """整条检索在 PG 上跑得通，且跨租户仍然一条都不召回。

    本地 FTS5 那条退化路径在 PG 上是死的（`bm25()` 与影子表 MATCH 都是 FTS5
    专有语法），检索器自己会退化并只告警一次 —— 整条检索不该因此挂掉。
    """
    hits = retriever.retrieve(pg, {"tenant_id": TENANT_A, "biz_type": "refund",
                                   "keyword": "timeout", "rule_no": "AS-101"})

    assert hits, "PG 上一条都没召回"
    assert all(h["doc"]["tenant_id"] == TENANT_A for h in hits), "跨租户漏了"
    assert "t115-d1" in {h["doc_id"] for h in hits}
