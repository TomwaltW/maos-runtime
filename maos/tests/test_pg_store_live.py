"""PG 后端连真库的测试 —— P5 把 P1 留的空壳填实之后，唯一能证明「真跑通」的东西。

**没库就整个 skip，绝不红。** 判据是「`MAOS_PG_DSN` 未设 或 连不上」：CI、别人的
机器、以及本仓库缺省的 SQLite 路径上都没有 PG，那是常态不是回归。起库：

    docker compose -f deploy/docker-compose.yml --profile pg up -d pgvector
    docker compose -f deploy/docker-compose.yml exec -T pgvector \
        psql -U <user> -d <db> -c 'CREATE EXTENSION IF NOT EXISTS vector;'
    export MAOS_PG_DSN=postgresql://<user>:<pass>@<host>:<port>/<db>

这里守四件事，前三件在 SQLite 侧已经有对应的测试，第四件是本轨新增的：

1. **排序方向**。pgvector 的 `<=>` 是余弦**距离**（越小越近），F-2 要的是「越大越
   相关」，适配器取了 `1 - 距离`。这一步搞反的症状是排序整个倒过来而**仍然有结果**，
   肉眼看不出来 —— 所以这里构造一组已知相似度的向量，正查反查各钉一次 top-1。
2. **换后端不换语义**。同一份数据、同一条逻辑查询，SQLite 后端与 PG 后端的
   `query` 结果必须逐字节相同。
3. **契约甲还活着**。驱动缺失时必须抛、必须不回落 sqlite。这条与
   `test_store_port.py` 里那条重合，那个文件是冻结的 28 条不许动，所以在这里再钉一遍
   —— 填实之后它的失败形态变了（不再是无条件抛，而是「后端不可用才抛」），值得单独守。
4. **中文全文的局限是事实，不是缺陷伪装**。PG 内置配置一个都没有中文分词器，本层
   因此对 CJK 查询抛 `LookupError` 让检索器退化，而不是安静地返回空集。这里把
   「整串汉字被当成一个 token」这条底层事实也钉住 —— 它不报错，只能靠测试记着。
   T142 在旁边补了**对照**（`test_shadow_table_text_is_matchable_char_by_char`）：
   同一串中文进了影子表就匹配得上了，那正是 A 口径成立的底层依据。
   **中文第二档（真分词器）的判据搬去了 `test_pg_vector_channel_t139.py` 的第 4 节** ——
   它必须建在装配路径上，建在本文件这张手建的原文靶表上是恒 0 命中的假红，见那一节。
"""

from __future__ import annotations

import functools
import json
import os
import sys

import pytest

import maos.kb as kb
from maos.store import create_store
from maos.store.pg_store import (
    DSN_ENV,
    FTS_CONFIG_ENV,
    PgBackendUnavailable,
    PgStorePort,
    _PG_BUILTIN_FTS_CONFIGS,
)

#: 靶表。名字带轨号，免得跟 pg_schema.sql 建的 kb_doc_pg 或别人的表撞上。
TABLE = "t10_live_doc"
CMP_TABLE = "t10_live_cmp"

#: 与 test_store_port.py 的 kb fixture 同一份语料，方便两个后端横向对齐。
ROWS = [
    ("d1", "refund timeout window is seven days", [1.0, 0.0, 0.0]),
    ("d2", "timeout", [0.9, 0.1, 0.0]),
    ("d3", "shipping policy has nothing to do with it here", [0.0, 1.0, 0.0]),
    ("d5", "退款政策超时未到账", None),
]


@functools.lru_cache(maxsize=1)
def _live_dsn() -> str | None:
    """探一次：DSN 配了吗、连得上吗。连不上就是没库，不是失败。"""
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
    reason=f"没有可连的 PG：{DSN_ENV} 未设或连不上。起库见本模块 docstring。",
)


@pytest.fixture(scope="module")
def pg() -> PgStorePort:
    """一张建好的 PG 靶表。用完 DROP，不给下一次留脏数据。"""
    port = PgStorePort(_live_dsn())
    port.execute("CREATE EXTENSION IF NOT EXISTS vector", ())
    port.execute(f"DROP TABLE IF EXISTS {TABLE}", ())
    port.execute(
        f"CREATE TABLE {TABLE} (id TEXT PRIMARY KEY, body TEXT, emb vector(3))", ()
    )
    for doc_id, body, emb in ROWS:
        port.execute(
            f"INSERT INTO {TABLE} (id, body, emb) VALUES (%s, %s, %s)",
            (doc_id, body, None if emb is None else json.dumps(emb)),
        )
    yield port
    port.execute(f"DROP TABLE IF EXISTS {TABLE}", ())
    port.execute(f"DROP TABLE IF EXISTS {CMP_TABLE}", ())
    port.close()


# ---------------------------------------------------------------- execute / query
def test_execute_and_query_roundtrip(pg: PgStorePort) -> None:
    rows = pg.query(f"SELECT id, body FROM {TABLE} WHERE id = %s", ("d2",))

    assert rows == [{"id": "d2", "body": "timeout"}], "query 应返回按列名索引的 dict 列表"


def test_params_are_bound_not_concatenated(pg: PgStorePort) -> None:
    """参数里带 SQL 片段也只能是数据。拼字符串的实现会在这条上把表删掉。"""
    payload = "'); DROP TABLE " + TABLE + "; --"
    pg.execute(f"INSERT INTO {TABLE} (id, body) VALUES (%s, %s)", ("evil", payload))
    try:
        assert pg.query(f"SELECT body FROM {TABLE} WHERE body = %s", (payload,)) == [
            {"body": payload}
        ], "值应原样存回原样取出"
        assert pg.query(f"SELECT count(*) AS n FROM {TABLE}", ()) == [
            {"n": len(ROWS) + 1}
        ], "表还得在"
    finally:
        pg.execute(f"DELETE FROM {TABLE} WHERE id = %s", ("evil",))


def test_sqlite_placeholder_is_refused_with_a_readable_message(pg: PgStorePort) -> None:
    """SQLite 的 `?` 在 PG 上不成立。本层不自动翻译，但要说人话。"""
    with pytest.raises(ValueError) as err:
        pg.query(f"SELECT id FROM {TABLE} WHERE id = ?", ("d1",))

    assert "%s" in str(err.value) and "?" in str(err.value)


# ------------------------------------------------------------------- 全文通道
def test_fts_search_hits_on_postgres(pg: PgStorePort) -> None:
    """tsvector / ts_rank 真返结果：'timeout' 在 d1 与 d2 里。"""
    hits = pg.fts_search(TABLE, "body", "timeout", 5)

    assert {doc_id for doc_id, _ in hits} == {"d1", "d2"}
    scores = [score for _, score in hits]
    assert scores == sorted(scores, reverse=True), "分数必须越大越相关、降序返回"
    assert all(score > 0.0 for score in scores)


def test_fts_search_terms_are_anded_and_limit_honoured(pg: PgStorePort) -> None:
    """plainto_tsquery 的词间是 AND：两个词都在的只有 d1。"""
    assert [d for d, _ in pg.fts_search(TABLE, "body", "refund timeout", 5)] == ["d1"]
    assert pg.fts_search(TABLE, "body", "refund shipping", 5) == []
    assert len(pg.fts_search(TABLE, "body", "timeout", 1)) == 1
    assert pg.fts_search(TABLE, "body", "timeout", 0) == []


def test_fts_search_survives_hostile_query_text(pg: PgStorePort) -> None:
    """用户输入里的算子字符不许把查询打成语法错。plainto_tsquery 负责转义。"""
    for hostile in ["a' OR 1=1 --", "NEAR(", "*", "col:", "-x", "   "]:
        assert pg.fts_search(TABLE, "body", hostile, 5) == []


def test_chinese_query_raises_on_builtin_config(pg: PgStorePort) -> None:
    """**中文第一档**（`MAOS_PG_FTS_CONFIG` 指向 PG 内置配置，本机今天的 `simple`）：
    中文查询必须抛 `LookupError`，不许静默漏。

    F-2 原话「『后端没准备好』不许伪装成『没命中』」。PG 内置配置没有中文分词器，
    这就是「没准备好」。报错里必须写清修法，否则下一个人只会以为库里没数据。

    ⚠️ T139 之后这条抛错**不再等于「查了也命不中」**。全文改查影子表之后，里面的
    中文已被 `kb.fts_text()` 按字切开，`simple` 其实匹配得上。仍然抛，是因为「按字
    AND」不是中文检索：「退款政策」与「政策退款」在它眼里一样，召回偏宽而排序无
    意义。把这种劣质召回当成「中文通了」会直接诱出那句不许说的话
    （`docs/submission-checklist.md:224`「缺省支持中文分词检索」）。显式退化比悄悄
    用劣质召回诚实 —— 这是 T139 明确保留的口径，别顺手把它放开。

    装了真分词器的部署走的是下一条，两条一起看才是完整的分档。
    """
    if pg.fts_config().lower() not in _PG_BUILTIN_FTS_CONFIGS:
        pytest.skip(f"这个库配的是 {pg.fts_config()}，不是 PG 内置配置 —— 第二档的事")

    with pytest.raises(LookupError) as err:
        pg.fts_search(TABLE, "body", "退款政策", 5)

    msg = str(err.value)
    assert FTS_CONFIG_ENV in msg, "报错要点名换哪个环境变量"
    assert "zhparser" in msg or "pg_jieba" in msg, "报错要点名装什么扩展"


# 🔴 **中文第二档的判据不在本文件**（T142 搬走）。
#
# 它原来叫 `test_chinese_query_recalls_on_real_tokenizer`，断言建在本文件手建的靶表
# `t10_live_doc` 上 —— 那张表的 body 存的是**原文**。而 `fts_search()` 内部一律把查询
# 串再过一遍 `kb.tokenize()`（`pg_store._fts_terms`），按 `pg_schema.sql` 记的 zhcfg
# 建法（索引建在影子表、`ADD MAPPING FOR n,v,a,i,e,l`）是**索引侧出词、查询侧出字**，
# 恒 0 命中。本机双重门控所以 skip、不显形，真跑日装好 zhparser 一切过去就会**假红**。
#
# T142 把它搬到了生产装配路径上（`kb.ensure_schema()` 建表 + `kb.upsert_doc()` 灌语料）：
# `maos/tests/test_pg_vector_channel_t139.py` 的「4. 中文第二档」一节，那里还多了一条
# **本机就能真跑**的链路档。取舍与理由见 `docs/DECISIONS.md` 的 `## task-t142`。


def test_simple_config_makes_a_whole_chinese_string_one_token(pg: PgStorePort) -> None:
    """上一条抛错的底层事实：`simple` 把整串汉字切成**一个** token。

    这条不报错，所以只能由测试记着 —— 它正是「中文召回恒空且无症状」的成因。

    ⚠️ 这条量的是 **`kb_doc` 原文列**那一路。检索实际走的是影子表，那一路上的同一
    件事量出来是相反的结论 —— 对照见下一条。
    """
    rows = pg.query("SELECT to_tsvector('simple', %s) AS tv", ("退款政策超时未到账",))

    assert rows[0]["tv"] == "'退款政策超时未到账':1", "整条是一个 token，子串查不中"


def test_shadow_table_text_is_matchable_char_by_char(pg: PgStorePort) -> None:
    """🔴 **A 影子表口径成立的底层事实**（T142 钉住）：同一串中文，存进影子表之后
    `simple` 就**匹配得上**了 —— 因为 `kb.fts_text()` 已经按字切开。

    与上一条并排读才完整：
    - 原文列 `退款政策超时未到账` → 一个 token `'退款政策超时未到账':1`，子串恒不命中
    - 影子表 `退 款 政 策 超 时 未 到 账` → 九个 token，`退 & 款` **真命中**

    这就是 T142 选 A（保留影子表口径）而不是 B（把 zhcfg 索引建回原文列）的底层依据：
    中文在影子表上**召得回来**（代价是「按字 AND」，排序无意义），而 B 口径要把错误码
    那条通道重新打瞎（`acq.trade_not_exist` 在原文列上又被黏成一个 token，
    `test_error_code_is_searchable_after_the_shadow_table_switch` 当场红）。两者不可
    兼得，取舍记在 `docs/DECISIONS.md` 的 `## task-t142`。

    ⚠️ 「匹配得上」**不等于**「中文通了」。第一档（内置配置 + CJK）照旧抛 `LookupError`，
    口径一个字没松（`DECISIONS.md` 的 `## task-t139`）：按字 AND 会让「退款政策」与
    「政策退款」一样，召回偏宽而排序无意义。那句「缺省支持中文分词检索」在**任何一档
    上都仍然不许说**。
    """
    shadow_text = kb.fts_text("退款政策超时未到账")
    assert shadow_text == "退 款 政 策 超 时 未 到 账", "影子表存的就是这个形状"

    rows = pg.query(
        "SELECT to_tsvector('simple', %s) AS tv,"
        " to_tsvector('simple', %s) @@ to_tsquery('simple', %s) AS hit",
        (shadow_text, shadow_text, " & ".join(kb.tokenize(kb.fts_text("退款")))),
    )

    assert rows[0]["tv"].count(":") == 9, \
        f"按字切开之后该是九个 token，实际 {rows[0]['tv']!r}"
    assert rows[0]["hit"] is True, \
        "影子表口径下中文必须匹配得上 —— 匹配不上的话 A 口径整个不成立"


def test_missing_table_raises_lookup_error(pg: PgStorePort) -> None:
    """表不在就抛 LookupError —— 检索器据此把通道判定为不可用并退化。"""
    with pytest.raises(LookupError):
        pg.fts_search("t10_no_such_table", "body", "timeout", 5)
    with pytest.raises(LookupError):
        pg.vector_search("t10_no_such_table", "emb", [1.0, 0.0, 0.0], 5)


# ------------------------------------------------------------------- 向量通道
def test_vector_search_ranks_by_cosine_similarity_not_distance(pg: PgStorePort) -> None:
    """🔴 排序方向。`<=>` 是距离（越小越近），F-2 要「越大越相关」。

    已知相似度：d1=[1,0,0] 与查询完全同向 → 1.0；d2=[.9,.1,0] → 0.99388；
    d3=[0,1,0] 正交 → 0.0。top-1 必须是 d1。取成距离就会返回 d3 打头。
    """
    hits = pg.vector_search(TABLE, "emb", [1.0, 0.0, 0.0], 3)

    assert [doc_id for doc_id, _ in hits] == ["d1", "d2", "d3"]
    assert hits[0][0] == "d1", "top-1 必须是与查询同向的那条"
    assert hits[0][1] == pytest.approx(1.0, abs=1e-6)
    assert hits[1][1] == pytest.approx(0.99388, abs=1e-4)
    assert hits[2][1] == pytest.approx(0.0, abs=1e-6)


def test_vector_search_direction_holds_for_a_second_query(pg: PgStorePort) -> None:
    """反向再钉一次：换个查询向量，top-1 必须跟着换。

    只查一次的话，「分数取反」这种错在某些数据上仍能碰巧给出对的 top-1。
    """
    hits = pg.vector_search(TABLE, "emb", [0.0, 1.0, 0.0], 3)

    assert hits[0][0] == "d3", "查询转向 d3 之后 top-1 必须是 d3"
    assert hits[0][1] == pytest.approx(1.0, abs=1e-6)
    assert [doc_id for doc_id, _ in hits][-1] == "d1"


def test_vector_search_matches_the_sqlite_backend_scores(pg: PgStorePort) -> None:
    """同一份向量、同一个查询，两个后端的分数必须对得上（换后端不换语义）。"""
    sqlite_port = create_store()
    sqlite_port.execute("CREATE TABLE t (id TEXT PRIMARY KEY, emb TEXT)", ())
    for doc_id, _, emb in ROWS:
        sqlite_port.execute(
            "INSERT INTO t (id, emb) VALUES (?,?)",
            (doc_id, None if emb is None else json.dumps(emb)),
        )

    on_sqlite = sqlite_port.vector_search("t", "emb", [1.0, 0.0, 0.0], 3)
    on_pg = pg.vector_search(TABLE, "emb", [1.0, 0.0, 0.0], 3)

    assert [d for d, _ in on_pg] == [d for d, _ in on_sqlite]
    for (_, pg_score), (_, sqlite_score) in zip(on_pg, on_sqlite):
        assert pg_score == pytest.approx(sqlite_score, abs=1e-6)


def test_vector_search_rejects_zero_query_vector(pg: PgStorePort) -> None:
    """零向量的余弦无定义。pgvector 会安静地返回 NaN，排序变随机 —— 必须响。"""
    with pytest.raises(ValueError):
        pg.vector_search(TABLE, "emb", [0.0, 0.0, 0.0], 3)


def test_vector_search_dimension_mismatch_raises_value_error(pg: PgStorePort) -> None:
    """维度对不上要抛。PG 报不出是哪一行 —— 已知差异，见 deploy/polardb.md。"""
    with pytest.raises(ValueError) as err:
        pg.vector_search(TABLE, "emb", [1.0, 0.0], 3)

    assert "维度" in str(err.value)


@pytest.mark.parametrize("bad", ["t; DROP TABLE t", "1bad", "", "kb doc", "kb-doc"])
def test_identifiers_are_validated_before_interpolation(pg: PgStorePort, bad: str) -> None:
    """表名/列名绑不了只能拼，所以拼之前必须卡形状。"""
    with pytest.raises(ValueError):
        pg.fts_search(bad, "body", "timeout", 5)
    with pytest.raises(ValueError):
        pg.vector_search(TABLE, bad, [1.0, 0.0, 0.0], 5)


# ---------------------------------------------------- 换后端不换语义 / 契约甲
def test_same_data_same_query_on_both_backends(pg: PgStorePort) -> None:
    """§5.4 第 4 条：同一份数据，两个后端的 `query` 结果逐字节相同。

    唯一允许不同的是**占位符方言**（SQLite `?` / PG `%s`），那是本层不做自动翻译
    的已知差异；行内容、列名、顺序都必须一致。
    """
    pg.execute(f"DROP TABLE IF EXISTS {CMP_TABLE}", ())
    pg.execute(f"CREATE TABLE {CMP_TABLE} (k TEXT PRIMARY KEY, n INTEGER)", ())
    sqlite_port = create_store()
    sqlite_port.execute("CREATE TABLE cmp (k TEXT PRIMARY KEY, n INTEGER)", ())
    for key, num in [("a", 1), ("b", 2), ("c", 3)]:
        pg.execute(f"INSERT INTO {CMP_TABLE} (k, n) VALUES (%s, %s)", (key, num))
        sqlite_port.execute("INSERT INTO cmp (k, n) VALUES (?,?)", (key, num))

    on_pg = pg.query(f"SELECT k, n FROM {CMP_TABLE} WHERE n >= %s ORDER BY k", (2,))
    on_sqlite = sqlite_port.query("SELECT k, n FROM cmp WHERE n >= ? ORDER BY k", (2,))

    assert on_pg == on_sqlite == [{"k": "b", "n": 2}, {"k": "c", "n": 3}]
    assert pg.dialect() == "postgres" and sqlite_port.dialect() == "sqlite"


def test_missing_driver_raises_and_never_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """契约甲：驱动缺失必须当场抛，绝不悄悄换回 SQLite。

    `sys.modules[name] = None` 是让 `import name` 抛 ImportError 的标准做法，
    比卸载包干净。抛出来的必须仍是 `NotImplementedError` 的子类 —— 冻结的 28 条
    里 `test_postgres_shell_raises_on_every_operation` 正按这个类型断言。
    """
    monkeypatch.setitem(sys.modules, "psycopg", None)
    port = PgStorePort("postgresql://<user>:<pass>@<host>:<port>/<db>")

    with pytest.raises(PgBackendUnavailable) as err:
        port.execute("SELECT 1", ())

    assert isinstance(err.value, NotImplementedError), "契约甲靠这个类型钉着，不许改基类"
    assert "psycopg" in str(err.value), "报错要告诉人装什么"
    assert port.dialect() == "postgres", "方言是静态事实，连不上也不许变成 sqlite"
    with pytest.raises(NotImplementedError):
        create_store(backend="postgres")


def test_unavailable_backend_never_leaks_the_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    """铁律 6：连不上时的报错与 repr 都不许回显连接串。"""
    monkeypatch.setenv(DSN_ENV, "postgresql://u:s3cr3t@127.0.0.1:1/nope")
    port = PgStorePort()

    with pytest.raises(PgBackendUnavailable) as err:
        port.query("SELECT 1", ())

    assert "s3cr3t" not in str(err.value)
    assert "s3cr3t" not in repr(port) and "已配置" in repr(port)
