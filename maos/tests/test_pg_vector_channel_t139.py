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

**T142 起多守第 4 件：中文第二档**（见本文件最后一节）。第一档「内置配置 + CJK 必须抛
`LookupError`」仍在 `test_pg_store_live.py::test_chinese_query_raises_on_builtin_config`
—— 那一档不依赖装配路径，留在原处。第二档搬来了这里，因为它必须建在
`kb.ensure_schema()` + `kb.upsert_doc()` 这条真装配路径上：建在手建的原文靶表上是恒 0
命中的假红。形状取舍（A 影子表口径）见 `docs/DECISIONS.md` 的 `## task-t142`。
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
    FTS_CONFIG_ENV,
    VECTOR_ACCEL_SUFFIX,
    PgStorePort,
    _PG_BUILTIN_FTS_CONFIGS,
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


def _capture_sql(port: PgStorePort, call) -> list[tuple[str, tuple]]:
    """截下 `call()` 这一跑里经 `_search_query` 发出去的 (sql, params)。

    判据要绑到**生产路径**上：手抄一条 SQL 去 EXPLAIN，证明的是「这张表上存在一条
    走得了索引的写法」，不是「生产代码发的就是那条」。两者的差别在回退时才显形 ——
    把 `vector_search` 改回单层退化形状，手抄那条照样绿。
    """
    sent: list[tuple[str, tuple]] = []
    original = type(port)._search_query

    def spy(self, sql, params, *, table, field):        # noqa: ANN001
        sent.append((sql, params))
        return original(self, sql, params, table=table, field=field)

    type(port)._search_query = spy
    try:
        call()
    finally:
        type(port)._search_query = original
    return sent


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

    # 🔴 走索引的必须是 `vector_search` **真发出去**的那条，不是测试另写的一条。
    #    上面 `fast` 那条是手抄的，它只能证明「这张表上存在一条走得了 HNSW 的写法」，
    #    证明不了生产代码发的就是它 —— 把 `vector_search` 改回退化形状，上面四条断言
    #    一条都不会红（整合期 p10-f 实测过）。所以这里截下真 SQL 再 EXPLAIN 它。
    sent = _capture_sql(pg, lambda: pg.vector_search("kb_doc", "embedding", vec, 5))
    assert sent, "一条 SQL 都没发出去 —— vector_search 没走到检索那一步"
    real_plan = _plan(pg, sent[0][0], sent[0][1])
    assert "idx_kb_doc_embedding_hnsw" in real_plan, (
        "vector_search 真发的那条没走 HNSW。它发的是：\n" + sent[0][0]
        + "\n计划：\n" + real_plan)
    assert "Index Scan" in real_plan, "真 SQL 的计划里没有索引扫描：\n" + real_plan

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
    # 回落到原表之后，英文那一路照样该查得到（`lesson` 在 d-l1 的 body 里）——
    # 原来这一行写成 `== [] or True`，右侧的 `or True` 让它对任何返回值都成立，
    # 一条约束都不产生（整合期 p10-f 清掉）。
    assert pg.fts_search("kb_doc", "body", kb.fts_text("lesson"), 5), \
        "回落查原表之后英文也召不回了 —— 回落的那条路本身坏了"
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


# ====================================================== 4. 中文第二档（T142 搬来）
#
# 🔴 这一节整个是 T142 从 `test_pg_store_live.py` 搬过来的，因为**判据必须建在装配路径
#    上**（`kb.ensure_schema()` 建表 + `kb.upsert_doc()` 灌语料，也就是本文件的 `pg`
#    fixture）。原来那条建在手建的原文靶表 `t10_live_doc` 上，而 `fts_search()` 内部一律
#    把查询串再过一遍 `kb.tokenize()`（`pg_store._fts_terms`）：按 `pg_schema.sql` 记的
#    zhcfg 建法，索引侧出词、查询侧出字，**恒 0 命中**。它今天双重门控（有 PG + 装了
#    zhparser）所以本机 skip、不显形，真跑日一切过去就是**假红** —— 而它原来的 docstring
#    写着「红了就知道不该切」，会把人指向一个错误的结论。
#
# 形状取舍（T142 定死，`docs/DECISIONS.md` 的 `## task-t142`）：**选 A，保留影子表口径**。
# 底层事实钉在 `test_pg_store_live.py::test_shadow_table_text_is_matchable_char_by_char`。

#: T142 在本机造出来的「非内置配置」。`COPY = simple` 意味着 parser 与 `simple` 一模
#: 一样（中文按字、英数按 `fts_text()` 已切好的形状），但**名字不在**
#: `_PG_BUILTIN_FTS_CONFIGS` 里 —— 于是 `fts_search()` 不再走第一档那条 `LookupError`，
#: 整条第二档链路（不抛 → `kb.tokenize()` → `to_tsquery` → 影子表 → 召回）在本机就能真跑。
#:
#: 这**不是**「为了本机也绿把断言弱化」（T139 原话，仍然有效）：断言一个字没松，仍然要求
#: 真召回 `d-zh`、分数 > 0。它买的是把「真跑日第一次执行」缩成「每次跑测试都执行」——
#: 第二档链路此前在本机永远 skip，而一条永不执行的断言守不住任何东西。
#: 它也**没有**替代下一条：唯一还没验的是「zhparser 对单字序列出不出词」，那要真分词器。
SECOND_TIER_PROBE_CONFIG = "t142_second_tier_probe"


@pytest.fixture
def second_tier(pg: PgStorePort, monkeypatch: pytest.MonkeyPatch):
    """本机造一个非内置的文本检索配置，把第二档链路开出来。用完 DROP，不留残留。

    建在 `public` 下，名字带轨号 —— 别的轨也在同一台 PG 上跑。

    ## 建不出来就 skip，**不许 ERROR**（整合期 p10-g 补）

    本机 `pgvector/pgvector:pg16` 是超级用户，建得出来；真跑日那台 PolarDB 的普通账号
    **不一定有 schema 上的 CREATE 权限**（`deploy/polardb-live.md` §1.3 记着它连
    `CREATE EXTENSION vector` 都建不出）。fixture setup 里抛异常在 pytest 里是 **ERROR
    不是 skip** —— 那会把真跑日那一束直接打红，而本轨的立意正是拆真跑日的雷。

    这是**门控，不是弱化断言**：跑起来的那一档断言一个字没松，仍然要求真召回 `d-zh`。
    建不出配置时这条链路在那台实例上本就无从验起，与「没有可连的 PG 就整组 skip」
    是同一件事的下一级。
    """
    drop = f"DROP TEXT SEARCH CONFIGURATION IF EXISTS {SECOND_TIER_PROBE_CONFIG}"
    try:
        pg._raw_query(drop, ())
        pg._raw_query(
            f"CREATE TEXT SEARCH CONFIGURATION {SECOND_TIER_PROBE_CONFIG} (COPY = simple)", ())
    except Exception as exc:                            # noqa: BLE001
        pytest.skip(f"这台实例上建不出文本检索配置（{type(exc).__name__}: {exc}）—— "
                    f"多半是账号没有 schema 上的 CREATE 权限。第二档链路在这里无从验起")
    monkeypatch.setenv(FTS_CONFIG_ENV, SECOND_TIER_PROBE_CONFIG)
    try:
        yield SECOND_TIER_PROBE_CONFIG
    finally:
        # 清理失败不许把一条已经跑完的测试翻红：留下的是一个带轨号的探针配置，
        # 下一次 setup 的 `DROP ... IF EXISTS` 会把它收掉。
        try:
            pg._raw_query(drop, ())
        except Exception:                               # noqa: BLE001
            pass


@live_only
def test_second_tier_chinese_recalls_through_the_assembly_path(
    pg: PgStorePort, second_tier: str
) -> None:
    """🔴 **第二档链路在本机就成立**：配置一旦不是 PG 内置的，装配路径上的中文查询
    必须**真召回** —— 而且**错误码那条通道不许因此变瞎**。

    这条是 A 口径（保留影子表）的完整判据。它同时钉住两件事：

    1. 中文召得回来。影子表里存的是 `kb.fts_text()` 的产物（中文已按字切开），
       查询侧过同一个函数，两边口径一致，所以 `退 & 款 & 政 & 策` 真命中。
    2. **错误码通道还活着**。这正是 A 与 B 的分水岭：B 口径（把 zhcfg 索引建回
       `kb_doc` 原文列、查询侧不过 `fts_text()`）能让 zhparser 真按词切中文，代价是
       `acq.trade_not_exist` 在原文列上又被黏成一个 token，本条最后一行当场破。
       两者不可兼得，T142 选 A —— 理由见 `docs/DECISIONS.md` 的 `## task-t142`。

    ⚠️ 「召得回来」**不等于**「中文分词检索通了」。这一档实际是**按字 AND**：
    「退款政策」与「政策退款」在它眼里一样，召回偏宽而排序无意义。所以那句
    「缺省支持中文分词检索」在**任何一档上都仍然不许说**
    （`docs/submission-checklist.md:224`，T139 已定，本轨没翻案）。
    """
    config = pg.fts_config()
    assert config == second_tier, "fixture 没把配置指过去，这条就没在验第二档"
    assert config.lower() not in _PG_BUILTIN_FTS_CONFIGS, \
        "探针配置必须是非内置的，否则走的还是第一档那条 LookupError"

    hits = pg.fts_search("kb_doc", "body", kb.fts_text("退款政策"), 5)

    assert [doc_id for doc_id, _ in hits] == [kb.doc_row_id(TENANT, "d-zh")], \
        f"第二档上中文没召回 d-zh：{hits} —— A 影子表口径的链路不成立"
    assert all(score > 0.0 for _, score in hits), "F-2：分数越大越相关，不许是 0"

    # 🔴 A 口径买的就是这一行：切到第二档之后错误码照样检索得到。
    assert [d for d, _ in pg.fts_search("kb_doc", "body", kb.fts_text("acq"), 5)] == \
        [kb.doc_row_id(TENANT, "d-err")], \
        "第二档下错误码通道瞎了 —— 那正是 B 口径的病，A 口径不该有"


@live_only
def test_chinese_query_recalls_on_real_tokenizer(pg: PgStorePort) -> None:
    """**中文第二档在真分词器上**（`MAOS_PG_FTS_CONFIG=zhcfg`，9/18 真跑日的 PolarDB）：
    装配路径上的中文查询必须**真召回**。

    为什么要有这一档：8/30 那次在 PolarDB 真实例上 `zhparser 2.2` 已经装成、`zhcfg`
    检索配置已建、中文召回已实测（`deploy/polardb-live.md` §1.4）。真跑日那天
    `MAOS_PG_FTS_CONFIG=zhcfg` 是能真跑的，这条描述那一档该是什么样。

    🔴 **这条红了不等于「不该切」**（T142 重写；原措辞给的是错误结论）。上一条
    `test_second_tier_chinese_recalls_through_the_assembly_path` 已经在本机证明
    **链路本身通**了 —— 不抛错、切词一致、影子表命中、错误码不瞎。所以本条一旦红，
    病根只剩唯一一个：**zhparser 在「单字序列」上不出词**（影子表里中文已被
    `kb.fts_text()` 按字切开，它拿到的是 `退 款 政 策` 而不是 `退款政策`，而
    `ADD MAPPING FOR n,v,a,i,e,l` 只收这几种词性，单字未必落在里面）。

    **当场处置**（真跑日照这个走，不要临场发挥）：把 `MAOS_PG_FTS_CONFIG` 退回
    `simple`，口径退回第一档 —— 中文照旧由 `fts_search()` 抛 `LookupError`、检索器
    退化走本地实现，召回照常，只是不走 PG。**不要在现场改判据，也不要改索引形状**
    （把 zhcfg 索引建回原文列是 B 口径，会把错误码通道打瞎，是一次形状改造不是现场
    微调，见 `docs/DECISIONS.md` 的 `## task-t142`）。

    本机跑不到这一档（`pgvector/pgvector:pg16` 没装 zhparser，且本波不许装），所以 skip。
    **skip 不是绿** —— 但它不再是「唯一一条描述第二档的判据」了，链路那半已经由上一条
    每次真跑。不许为了「本机也绿」把断言弱化成 `if hits:` 那种形状（T139 原话）。
    """
    config = pg.fts_config()
    if config.lower() in _PG_BUILTIN_FTS_CONFIGS:
        pytest.skip(
            f"这个库配的是 PG 内置的 {config}，没有中文分词器 —— 本机"
            " pgvector/pgvector:pg16 没装 zhparser，跑不到这一档。装了之后"
            " export MAOS_PG_FTS_CONFIG=zhcfg 再跑本条（建法见 pg_schema.sql 中文那一节）。"
        )

    hits = pg.fts_search("kb_doc", "body", kb.fts_text("退款政策"), 5)

    if not hits:
        # 红了就地把病根打出来，省掉真跑日现场那一轮「是表没建、是没灌数据、还是分词
        # 没出词」的排查 —— 现场最贵的就是这一轮往返。
        probe = pg._raw_query(
            "SELECT to_tsvector(%s, %s)::text AS tv, to_tsquery(%s, %s)::text AS tq",
            (config, kb.fts_text("退款政策超时未到账"),
             config, " & ".join(kb.tokenize(kb.fts_text("退款政策")))))[0]
        pytest.fail(
            f"{config} 上中文 0 命中。索引侧 to_tsvector = {probe['tv']!r}；"
            f"查询侧 to_tsquery = {probe['tq']!r}。两边（或任一边）是空的 ="
            " zhparser 在单字序列上不出词。当场处置：把 MAOS_PG_FTS_CONFIG 退回"
            " simple，口径退回第一档，中文照旧走本地退化。不要在现场改判据或索引形状"
            "（见本函数 docstring 与 docs/DECISIONS.md 的 ## task-t142）。"
        )

    assert [doc_id for doc_id, _ in hits] == [kb.doc_row_id(TENANT, "d-zh")], \
        f"中文查询在 {config} 上召回的不是 d-zh：{hits}"
    assert all(score > 0.0 for _, score in hits), "F-2：分数越大越相关，不许是 0"


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
