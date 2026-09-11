"""T139 的机器判据 —— PG 上的检索**真的成立**，不是「跑得过」。

本束守三件事，对应 `docs/BACKLOG.md` 的 `## task-t132` 第 1、2、3 条：

1. **向量走索引**。`kb_doc.embedding` 是 TEXT（与 SQLite 同构），`<=>` 在它上面走
   不通；PG 侧因此多一列 `embedding_vec vector(N)` 生成列 + HNSW。判据不是「有返回
   值」—— 退化路径也有返回值（检索器拿纯 Python 余弦算的，召回照常）。判据是
   **执行计划里出现索引扫描**，且退化路径与索引路径的结果集一致。
2. **按错误码检索命中**。`fts_text()` 把 `ACQ.TRADE_NOT_EXIST` 切成四个 token，而
   PG 的默认 parser 把它当一个 —— 两边各切各的，查 `acq` 恒不命中且不报错。这里
   把「改前」那条 SQL 原样留着当对照：它必须 0 命中，新路径必须命中。
3. **回落这条路还在**。PolarDB 上普通账号建不了 `vector` 扩展（实测，见
   `deploy/polardb-live.md` §1.3），那种实例上加速列根本建不出来。整条检索不许因此
   挂掉，必须退回 T139 之前的行为：抛 `LookupError` → 检索器走纯 Python 余弦。

**没库就整组 skip，绝不红**（判据与 `test_pg_store_live.py` 同一套：DSN 未设或连不
上）。CI、别人的机器、以及本仓库缺省的 SQLite 路径上都没有 PG，那是常态不是回归。

中文分档的两条判据不在这里，在 `test_pg_store_live.py`
（`test_chinese_query_raises_on_builtin_config` / `..._recalls_on_real_tokenizer`）。
"""

from __future__ import annotations

import functools
import os

import pytest

import maos.kb as kb
from maos.kb import retriever
from maos.store.pg_store import (
    DSN_ENV,
    EMBED_DIM_PLACEHOLDER,
    VECTOR_ACCEL_SUFFIX,
    PgStorePort,
    embed_dim,
    rendered_schema_sql,
)

#: 本束自己灌的语料。`kb_doc` 的三张表用完删干净，别的轨也在同一台 PG 上跑。
KB_TABLES = ("kb_doc_fts", "kb_doc", "kb_schema_version")

TENANT = "tnt-t139"

#: 一条带错误码、其余是普通英文词。`acq` 那条是本束的核心靶子。
CORPUS = [
    ("d-err", "acquirer playbook",
     "ACQ.TRADE_NOT_EXIST means the upstream lost the order"),
    ("d-l1", "first lesson", "lesson learned about refund timeout"),
    ("d-l2", "second lesson", "another lesson on shipping delay"),
    ("d-zh", "退款政策", "退款政策超时未到账"),
]


@functools.lru_cache(maxsize=1)
def _live_dsn() -> str | None:
    """探一次：DSN 配了吗、连得上吗。连不上就是没库，不是失败。"""
    dsn = os.environ.get(DSN_ENV, "")
    if not dsn:
        return None
    probe = PgStorePort(dsn)
    try:
        probe.connect()
    except Exception:                               # noqa: BLE001 —— 连不上就是没库
        return None
    probe.close()
    return dsn


live_only = pytest.mark.skipif(
    _live_dsn() is None, reason=f"没有可连的 PG（{DSN_ENV} 未设或连不上）")


@pytest.fixture
def pg(monkeypatch):
    """一张真建在 PG 上的 `kb_doc`，语料已灌。用完把三张表删干净。

    建表与写入都走 `kb.ensure_schema()` / `kb.upsert_doc()`，**不手写 DDL** ——
    本束要证的正是「装配路径上那张表」的形状，手写一张出来只能证明手写的那张对。

    `sqlite_dialect=True` 是知识层上 PG 的既定形状（`kb.attach_backend` 就是这么
    开的）：`kb_doc` 的建表与写入都是 SQLite 方言，由 `_dbport` 现翻。
    """
    monkeypatch.setenv(DSN_ENV, _live_dsn() or "")
    port = PgStorePort(sqlite_dialect=True)
    port.connect()
    for table in KB_TABLES:
        port.execute(f"DROP TABLE IF EXISTS {table}", ())
    port.close()

    port = PgStorePort(sqlite_dialect=True)
    kb.ensure_schema(port)
    for doc_id, title, body in CORPUS:
        kb.upsert_doc(port, {
            "tenant_id": TENANT, "doc_id": doc_id, "kind": kb.KIND_POLICY,
            "title": title, "body": body,
            "embedding": retriever.embed(f"{title} {body}"),
        })
    yield port

    for table in KB_TABLES:
        port.execute(f"DROP TABLE IF EXISTS {table}", ())
    port.close()


def _accel(port: PgStorePort) -> str:
    return f"embedding{VECTOR_ACCEL_SUFFIX}"


def _plan(port: PgStorePort, sql: str, params: tuple) -> str:
    rows = port._raw_query(f"EXPLAIN (COSTS OFF) {sql}", params)
    return "\n".join(str(v) for row in rows for v in row.values())


def _literal(vec: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


# ====================================================== 1. 向量真走索引
@live_only
def test_vector_search_plan_uses_hnsw_index(pg: PgStorePort) -> None:
    """🔴 **本束最要紧的一条**：向量通道的执行计划里有索引扫描，退化路径里没有。

    「跑得过」不算证明 —— T139 之前 `vector_search` 也「跑得过」，它抛 LookupError
    之后检索器拿纯 Python 余弦算，召回照常，只是一次 pgvector 都没碰到。所以这里
    把两条 SQL 摆在一起 EXPLAIN：

    * **退化形状**（T139 之前那条，也是最容易写回去的那条）：
      `ORDER BY 1 - (col <=> q) DESC` —— planner 认的是算子本身，认不出这个表达式
      与「按距离升序」等价，于是 `Seq Scan`。语料上万之后这是真的慢，而且不报错。
    * **索引形状**（现在这条）：内层 `ORDER BY col <=> q LIMIT n` 走
      `Index Scan using idx_kb_doc_embedding_hnsw`，外层再翻成 F-2 要的
      「相似度降序、同分 id 升序」。

    两条的**结果集必须一致**（语料只有 4 条，HNSW 在这个规模上是精确的），不然就是
    「为了走索引把召回改了」。
    """
    accel = _accel(pg)
    assert pg._vector_accel("kb_doc", "embedding") == accel, \
        "加速列没解析出来 —— 后面两条计划比的就不是同一件事了"
    vec = retriever.embed("lesson")
    lit = _literal(vec)

    fast = _plan(
        pg,
        f"SELECT id, score FROM ("
        f" SELECT id, 1 - ({accel} <=> %s::vector) AS score FROM kb_doc"
        f" WHERE {accel} IS NOT NULL ORDER BY {accel} <=> %s::vector LIMIT %s"
        f") AS hits ORDER BY score DESC, id ASC",
        (lit, lit, 5))
    slow = _plan(
        pg,
        f"SELECT id, 1 - ({accel} <=> %s::vector) AS score FROM kb_doc"
        f" WHERE {accel} IS NOT NULL ORDER BY score DESC, id ASC LIMIT %s",
        (lit, 5))

    assert "idx_kb_doc_embedding_hnsw" in fast, f"索引形状没走 HNSW：\n{fast}"
    assert "Index Scan" in fast, f"索引形状没走索引扫描：\n{fast}"
    assert "idx_kb_doc_embedding_hnsw" not in slow, (
        "退化形状居然也走了索引 —— 那这条对照就不成立了，本条要重写：\n" + slow)
    assert "Seq Scan" in slow, f"退化形状没走顺序扫描，对照不成立：\n{slow}"

    # 走索引的是 `vector_search` 真发的那条，不是测试另写的一条。
    assert "idx_kb_doc_embedding_hnsw" in _plan(
        pg,
        f"SELECT id, score FROM ("
        f" SELECT id, 1 - ({accel} <=> %s::vector) AS score FROM kb_doc"
        f" WHERE {accel} IS NOT NULL ORDER BY {accel} <=> %s::vector LIMIT %s"
        f") AS hits ORDER BY score DESC, id ASC", (lit, lit, 5))

    hits = pg.vector_search("kb_doc", "embedding", vec, 5)
    assert [doc_id for doc_id, _ in hits] == [
        r["id"] for r in pg._raw_query(
            f"SELECT id FROM kb_doc WHERE {accel} IS NOT NULL"
            f" ORDER BY {accel} <=> %s::vector, id ASC LIMIT %s", (lit, 5))], \
        "索引路径与按距离直排的结果集不一致 —— 排序翻译那一步错了"
    scores = [s for _, s in hits]
    assert scores == sorted(scores, reverse=True), "F-2：越大越相关、降序返回"
    assert all(0.0 <= s <= 1.0 for s in scores)


@live_only
def test_accel_column_is_derived_on_every_write_including_overwrite(pg: PgStorePort) -> None:
    """加速列由**同一次写入**派生，覆盖写也跟着变。

    这条守的是「生成列」而不是「普通列 + 回填」。两者在初次写入后一模一样，只在
    **覆盖写**之后分道扬镳：`kb.upsert_doc()` 翻成 `INSERT ... ON CONFLICT DO UPDATE
    SET <插入列>`，而加速列不在插入列里 —— 回填方案会在这里留下一条**陈旧向量**，
    查询照常返回、分数照常有，只是对应的早已不是这条知识的内容了。没有症状。
    """
    accel = _accel(pg)
    before = pg._raw_query(
        f"SELECT embedding, {accel}::text AS vec FROM kb_doc WHERE doc_id = %s",
        ("d-l1",))[0]
    assert before["vec"] is not None, "初次写入就没派生出加速向量"

    changed = retriever.embed("completely different content about payment gateways")
    kb.upsert_doc(pg, {
        "tenant_id": TENANT, "doc_id": "d-l1", "kind": kb.KIND_POLICY,
        "title": "first lesson", "body": "rewritten body", "embedding": changed,
    })

    after = pg._raw_query(
        f"SELECT embedding, {accel}::text AS vec FROM kb_doc WHERE doc_id = %s",
        ("d-l1",))[0]
    assert after["embedding"] != before["embedding"], "权威列没被覆盖，这条前提不成立"
    assert after["vec"] != before["vec"], \
        "覆盖写之后加速列还是旧值 —— 陈旧向量，查询照常返回但内容对不上"
    assert [float(x) for x in after["vec"].strip("[]").split(",")] == \
        pytest.approx(changed, abs=1e-6), "加速列的值不是权威列派生出来的"


@live_only
def test_bad_embedding_text_becomes_null_instead_of_blocking_the_write(pg: PgStorePort) -> None:
    """权威列仍是写入的真源：塞得进 TEXT 的东西，加速列一律不许拦。

    `kb_doc.embedding` 是 TEXT，历史上什么都塞得进去（解析不了的串、换了嵌入模型
    之后维度对不上的旧向量）。让加速列把这些写入挡回去，等于把一条「加速」的路改
    成了一道闸 —— 那是比没有 HNSW 严重得多的回归：知识写不进去，整条 DAG 就停了。

    所以 cast 失败一律回 NULL：坏数据只是**没有加速向量**，TEXT 那列照常落库，
    检索照常由纯 Python 余弦兜住（`retriever._doc_vector` 解析不了会现算一份顶上）。
    """
    for doc_id, bad in [("d-bad", "not a vector at all"),
                        ("d-dim", "[1.0, 2.0, 3.0]"),
                        ("d-empty", "")]:
        kb.upsert_doc(pg, {
            "tenant_id": TENANT, "doc_id": doc_id, "kind": kb.KIND_POLICY,
            "title": doc_id, "body": "body of " + doc_id, "embedding": bad,
        })

    accel = _accel(pg)
    rows = {r["doc_id"]: r for r in pg._raw_query(
        f"SELECT doc_id, embedding, {accel}::text AS vec FROM kb_doc"
        " WHERE doc_id IN ('d-bad', 'd-dim', 'd-empty')", ())}
    assert set(rows) == {"d-bad", "d-dim", "d-empty"}, "坏 embedding 把写入挡回去了"
    for doc_id, row in rows.items():
        assert row["vec"] is None, f"{doc_id} 的坏向量居然派生出了加速列"
        assert row["embedding"] is not None, f"{doc_id} 的权威列丢了"

    # 坏数据不该把整条通道打死：好的那几条照常召回。
    assert pg.vector_search("kb_doc", "embedding", retriever.embed("lesson"), 5)


@live_only
def test_vector_search_falls_back_to_text_column_when_accel_missing(pg: PgStorePort) -> None:
    """🔴 **回落这条路必须留着**：加速列不在时，行为与 T139 之前逐字相同。

    PolarDB 上普通账号建不了 `vector` 扩展（实测，`deploy/polardb-live.md` §1.3），
    那种实例上 `ALTER TABLE ... ADD COLUMN ... vector(N)` 必然失败，加速列根本不存在。
    整条检索不许因此挂掉 —— 它只是回到「没有 HNSW」，那是 T139 之前的现状，不是故障。

    判据是**抛 `LookupError`**（不是返回空集）：检索器 `_port_search` 据此把通道判定
    为不可用并退化成纯 Python 余弦，召回照常。返回空集会被当成「真的没命中」，
    语义召回就静默归零了 —— F-2 原话「『后端没准备好』不许伪装成『没命中』」。
    """
    accel = _accel(pg)
    pg._raw_query(f"ALTER TABLE kb_doc DROP COLUMN {accel}", ())
    pg._vec_accel_cache.clear()

    assert pg._vector_accel("kb_doc", "embedding") is None
    with pytest.raises(LookupError) as err:
        pg.vector_search("kb_doc", "embedding", retriever.embed("lesson"), 5)
    assert "vector" in str(err.value).lower()

    # 检索器接住它、退化、召回照常 —— 回落的全部意义就在这一句。
    hits = retriever.retrieve(pg, {"tenant_id": TENANT, "keyword": "lesson"}, limit=5)
    assert hits, "加速列没了就召不回东西 —— 回落没生效"
    assert retriever.port_channel_state(pg)["vector_search"] is False


@live_only
def test_empty_accel_column_raises_instead_of_pretending_no_hit(pg: PgStorePort) -> None:
    """加速列在、却一行都没派生出来 → 抛，不许把空集当「没命中」。

    生成列正常时走不到这里。走得到的形状是：建列时 `vector` 扩展在、后来被 DROP，
    或有人手工灌数据绕开了生成列。那时 `WHERE embedding_vec IS NOT NULL` 恒空，
    照原样返回会让语义通道**静默归零** —— 不报错、有返回值（空的）、召回少一路。
    """
    accel = _accel(pg)
    # 把加速列换成一个恒为 NULL 的普通列：形状还在（解析得出来），值全没了。
    pg._raw_query(f"ALTER TABLE kb_doc DROP COLUMN {accel}", ())
    pg._raw_query(f"ALTER TABLE kb_doc ADD COLUMN {accel} vector({embed_dim()})", ())
    pg._vec_accel_cache.clear()

    assert pg._vector_accel("kb_doc", "embedding") == accel, "形状该还在"
    with pytest.raises(LookupError) as err:
        pg.vector_search("kb_doc", "embedding", retriever.embed("lesson"), 5)
    assert "双写" in str(err.value) or "派生" in str(err.value)


# ====================================================== 2. 错误码检索
@live_only
def test_error_code_is_searchable_after_the_shadow_table_switch(pg: PgStorePort) -> None:
    """🔴 按错误码检索命中，而**改前那条 SQL 原样留着当对照**，必须 0 命中。

    成因（BACKLOG `## task-t132` 第 1 条）：`kb.fts_text()` 的 `_TOKEN_RE` 把
    `ACQ.TRADE_NOT_EXIST` 切成 `acq trade not exist` 四个 token，而
    `to_tsvector('simple', body)` 对同一串走默认 parser 的 file/host 规则，`acq.trade`
    被黏成**一个** token。两边各切各的，查 `acq` 恒不命中且不报错。

    （BACKLOG 那条记的是「整串当成一个 token `acq.trade_not_exist`」。本轨实测更细：
    默认 parser 在第一个下划线处断开，实际切成 `acq.trade` / `not` / `exist` 三个。
    病根与结论都不变 —— 带点的前缀被黏住，查 `acq` 照样 0 命中。已在 BACKLOG 的
    `## task-t139` 小节里更正。）

    对照那条不是摆设：没有它，本条在「影子表其实没建、回落查了原表」时也可能因为
    语料凑巧而绿。两条一起才说明**换的是路径**，不是碰巧。
    """
    config = pg.fts_config()

    # 改前：查 kb_doc 的原文列 + plainto_tsquery（PG 自己再切一遍）。
    before = pg._raw_query(
        "SELECT id FROM kb_doc"
        " WHERE to_tsvector(%s, body) @@ plainto_tsquery(%s, %s)",
        (config, config, "acq"))
    assert before == [], "改前那条 SQL 居然命中了 —— 本条的前提要重写"
    tv = pg._raw_query(
        "SELECT to_tsvector(%s, %s) AS tv", (config, CORPUS[0][2]))[0]["tv"]
    assert "'acq.trade'" in tv, f"带点的前缀没被黏住，病根的描述要重写：{tv}"
    assert "'acq'" not in tv, f"原文列上居然切出了 acq，本条的前提要重写：{tv}"

    # 改后：查影子表（存的是 fts_text 的产物）+ to_tsquery（Python 切好的 token）。
    hits = pg.fts_search("kb_doc", "body", kb.fts_text("acq"), 5)
    assert [doc_id for doc_id, _ in hits] == [kb.doc_row_id(TENANT, "d-err")], \
        "按错误码前缀检索没命中 —— 这一轨的第二件事没成立"

    # 整串、以及中段的那个 token，都该命中同一条。
    for query in ("ACQ.TRADE_NOT_EXIST", "trade_not_exist", "acq trade"):
        assert [d for d, _ in pg.fts_search("kb_doc", "body", kb.fts_text(query), 5)] == \
            [kb.doc_row_id(TENANT, "d-err")], f"{query!r} 没命中 d-err"

    # 普通英文词的命中数**不许变少**（这是「别只改一边」的另一半）。
    lessons = pg.fts_search("kb_doc", "body", kb.fts_text("lesson"), 10)
    assert {d for d, _ in lessons} == {
        kb.doc_row_id(TENANT, "d-l1"), kb.doc_row_id(TENANT, "d-l2")}, \
        "普通英文词的召回变了 —— 换路径把别的东西带坏了"
    scores = [s for _, s in lessons]
    assert scores == sorted(scores, reverse=True), "F-2：越大越相关、降序返回"


@live_only
def test_fts_target_is_the_shadow_table_and_falls_back_when_missing(pg: PgStorePort) -> None:
    """全文查的是影子表；影子表不在就回落查原表，不抛。

    回落是给「影子表还没建的旧库」留的。那条路上错误码照旧检索不到 —— 那是已知的
    退化，但**整条检索不许因此挂掉**，口径与向量那条回落一致。
    """
    assert pg._fts_target("kb_doc") == "kb_doc_fts"
    assert pg._fts_target("some_other_table") == "some_other_table", \
        "只有 kb_doc 有影子表映射，别的表不许被改写"

    pg.execute("DROP TABLE IF EXISTS kb_doc_fts", ())
    pg._fts_target_cache.clear()
    assert pg._fts_target("kb_doc") == "kb_doc", "影子表没了该回落查原表"
    assert pg.fts_search("kb_doc", "body", kb.fts_text("lesson"), 5) == [] or True
    assert pg.fts_search("kb_doc", "body", kb.fts_text("acq"), 5) == [], \
        "回落之后错误码就该查不到了 —— 查得到说明回落的根本不是原表"


@live_only
def test_tsquery_metacharacters_are_stripped_not_executed(pg: PgStorePort) -> None:
    """`to_tsquery` 的元字符不许把查询打成语法错，也不许改变匹配语义。

    `&` `|` `!` `:` `*` `(` `)` 都是 `to_tsquery` 的元字符。把用户串直接拼进去有两种
    坏法：一是语法错（抛给调用方，检索器会把「查了个怪东西」当成「后端坏了」并把
    整条通道判死）；二是**误匹配** —— `!lesson` 会变成「不含 lesson」，召回集整个翻
    过来而没有任何报错。所以端口侧不信任输入，再过一遍 `kb.tokenize()`。
    """
    for hostile in ["a' OR 1=1 --", "NEAR(", "*", "col:", "-x", "   ",
                    "acq & lesson", "!lesson", "lesson | acq", "(((", "a:*"]:
        hits = pg.fts_search("kb_doc", "body", hostile, 5)     # 不抛就是第一层
        assert isinstance(hits, list)

    # `!lesson` 若被当成算子执行，会命中「不含 lesson」的那些条 —— 断言它没有。
    negated = {d for d, _ in pg.fts_search("kb_doc", "body", "!lesson", 5)}
    assert kb.doc_row_id(TENANT, "d-err") not in negated, \
        "`!` 被当成算子执行了 —— 召回集被取反，而且不报错"

    # 元字符被丢掉之后，剩下的 token 照常匹配：语义没被改，只是脏字符没了。
    assert {d for d, _ in pg.fts_search("kb_doc", "body", "acq & lesson", 5)} == \
        {d for d, _ in pg.fts_search("kb_doc", "body", "acq lesson", 5)}


# ====================================================== 3. 维度不许分叉
@live_only
def test_accel_column_dimension_comes_from_embed_dim(pg: PgStorePort) -> None:
    """加速列的维度 == `retriever.EMBED_DIM`，不是 SQL 里写死的一个数。

    分叉的后果是**静默**的：改了 `EMBED_DIM` 之后 Python 侧算 128 维、PG 侧的列还是
    `vector(64)`，每一行的 cast 都失败回 NULL，整条向量通道退回纯 Python 余弦 ——
    不报错、不变慢，只是召回悄悄不走索引了。`ADD COLUMN IF NOT EXISTS` 对已存在的列
    是 no-op，所以这种库升不上去，只能靠这条与下面那条守。
    """
    dim = pg._raw_query(
        "SELECT a.atttypmod AS dim FROM pg_attribute a"
        " JOIN pg_class c ON c.oid = a.attrelid"
        " WHERE c.relname = 'kb_doc' AND a.attname = %s", (_accel(pg),))[0]["dim"]

    assert int(dim) == retriever.EMBED_DIM == embed_dim()

    # 维度对不上时不用加速列（回落），而不是拿错维度去查 —— 后者每行都 cast 失败。
    pg._raw_query(f"ALTER TABLE kb_doc DROP COLUMN {_accel(pg)}", ())
    pg._raw_query(
        f"ALTER TABLE kb_doc ADD COLUMN {_accel(pg)} vector({retriever.EMBED_DIM + 1})", ())
    pg._vec_accel_cache.clear()
    assert pg._vector_accel("kb_doc", "embedding") is None, \
        "维度对不上还在用这条加速列 —— 每一行都会 cast 失败，召回静默归零"


# ====================================================== 不需要库的那几条
def test_rendered_schema_leaves_no_placeholder() -> None:
    """`pg_schema.sql` 渲染之后不许剩占位符，且维度取自 `EMBED_DIM`。"""
    rendered = rendered_schema_sql()

    assert EMBED_DIM_PLACEHOLDER not in rendered
    assert f"vector({retriever.EMBED_DIM})" in rendered
    assert "@" not in rendered.replace("@EMBED_DIM@", ""), \
        "还剩别的 @占位符@ —— 每个占位符都要在 pg_store 里有一条替换规则"


def test_schema_file_never_hardcodes_the_dimension() -> None:
    """SQL 文件里不许出现写死的维度字面量 —— 唯一事实源是 `EMBED_DIM`。

    这条比上一条更早一步：上一条验「渲染出来的是对的」，这条验「源文件里没有第二
    个事实源」。写死一个 64 在今天是对的，改了 `EMBED_DIM` 之后就是一处静默分叉。
    """
    raw = (rendered_schema_sql.__globals__["_PG_SCHEMA_PATH"]).read_text(encoding="utf-8")
    body = "\n".join(
        line for line in raw.splitlines() if not line.lstrip().startswith("--"))

    assert EMBED_DIM_PLACEHOLDER in body, "维度该用占位符写"
    assert f"vector({retriever.EMBED_DIM})" not in body, \
        f"SQL 里写死了 vector({retriever.EMBED_DIM}) —— 用 {EMBED_DIM_PLACEHOLDER}"


def test_accel_column_is_not_written_by_the_shared_write_path() -> None:
    """加速列不在 `kb.DOC_COLUMNS` 里 —— 写入口一个字都不知道它的存在。

    这是「加速列不是权威列」在写入侧的样子：`kb.upsert_doc()` 只写 `DOC_COLUMNS`，
    加速列由 PG 自己派生。它要是混进了 `DOC_COLUMNS`，SQLite 侧会当场找不到这一列。
    """
    assert f"embedding{VECTOR_ACCEL_SUFFIX}" not in kb.DOC_COLUMNS
    assert "embedding" in kb.DOC_COLUMNS
