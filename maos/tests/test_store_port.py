"""StorePort 与 SQLite 适配器 —— v4 手册 P1 第 7 步补的欠账。

这里守三件事，一件比一件难在事后发现：

1. **F-2 五个签名不许漂**。W-3 的检索器照着它写全文与向量两条通道，签名改一个字
   对面当场散架，而且散在别人的轨上。`test_f2_signatures_are_frozen` 是机器版的
   「别动」，比任何注释可靠。
2. **不许静默回落**。`MAOS_STORE_BACKEND=postgres` 必须当场抛错：回落 sqlite 的
   症状是「PG 后端看起来跑通了」，而实际上一行 PG 代码都没执行 —— 等真接 PG 那天，
   所有以为验过的路径都得重验，且没有任何东西提示你该重验。影子表缺失时不回落
   LIKE 是同一条道理：那会让全文检索看起来在工作而召回恒空。
3. **参数只绑不拼**。标识符（表名/列名）没法绑，所以拼之前卡形状；值一律走 `?`。
   两条合起来才算不拼 SQL，缺一条都是开着的注入面。
"""

from __future__ import annotations

import inspect
import json

import pytest

from maos.core.store import SqliteStore as CoreSqliteStore
from maos.core.store import Store as CoreStore
from maos.store import (
    BACKEND_ENV,
    PgStorePort,
    SqliteStorePort,
    StorePort,
    create_store,
)
from maos.store.pg_store import DSN_ENV

#: F-2 的原文，逐字抄自 docs/EXECUTION.md 第 197-217 行。
F2_SIGNATURES = {
    "execute": "(self, sql: str, params: tuple) -> None",
    "query": "(self, sql: str, params: tuple) -> list[dict]",
    "fts_search": "(self, table: str, field: str, q: str, limit: int) -> list[tuple[str, float]]",
    "vector_search": (
        "(self, table: str, field: str, vec: list[float], limit: int)"
        " -> list[tuple[str, float]]"
    ),
    "dialect": "(self) -> str",
}


def _sig(func: object) -> str:
    """`from __future__ import annotations` 下注解是字符串，去掉引号好跟原文比。"""
    return str(inspect.signature(func)).replace("'", "")


@pytest.fixture()
def port() -> SqliteStorePort:
    """一个建好核心表的内存后端。缺省路径，跟真链路同一个 SqliteStore。"""
    return create_store()


@pytest.fixture()
def kb(port: SqliteStorePort) -> SqliteStorePort:
    """一张带 FTS5 影子表与向量列的靶表，模拟 W-3 的检索器怎么建库。"""
    port.execute("CREATE TABLE kb_doc (id TEXT PRIMARY KEY, body TEXT, emb TEXT)", ())
    port.execute(
        "CREATE VIRTUAL TABLE kb_doc_fts USING fts5(id UNINDEXED, body, tokenize='trigram')", ()
    )
    rows = [
        ("d1", "refund timeout window is seven days", [1.0, 0.0, 0.0]),
        ("d2", "timeout", [0.9, 0.1, 0.0]),
        ("d3", "shipping policy has nothing to do with it here", [0.0, 1.0, 0.0]),
        ("d4", "unrelated note four", [0.0, 0.0, 0.0]),
        ("d5", "退款政策超时未到账", None),
    ]
    for doc_id, body, emb in rows:
        port.execute(
            "INSERT INTO kb_doc (id, body, emb) VALUES (?,?,?)",
            (doc_id, body, json.dumps(emb) if emb is not None else None),
        )
        port.execute("INSERT INTO kb_doc_fts (id, body) VALUES (?,?)", (doc_id, body))
    return port


# ---------------------------------------------------------------- execute / query
def test_execute_and_query_roundtrip(port: SqliteStorePort) -> None:
    port.execute("CREATE TABLE t (k TEXT PRIMARY KEY, v TEXT)", ())
    port.execute("INSERT INTO t (k, v) VALUES (?,?)", ("a", "1"))
    port.execute("INSERT INTO t (k, v) VALUES (?,?)", ("b", "2"))

    rows = port.query("SELECT k, v FROM t WHERE k=?", ("b",))

    assert rows == [{"k": "b", "v": "2"}], "query 应返回按列名索引的 dict 列表"


def test_params_are_bound_not_concatenated(port: SqliteStorePort) -> None:
    """参数里带 SQL 片段也只能是数据。拼字符串的实现会在这条上把表删掉。"""
    port.execute("CREATE TABLE t (k TEXT PRIMARY KEY, v TEXT)", ())
    payload = "'); DROP TABLE t; --"
    port.execute("INSERT INTO t (k, v) VALUES (?,?)", ("evil", payload))

    rows = port.query("SELECT v FROM t WHERE v=?", (payload,))

    assert rows == [{"v": payload}], "值应原样存回原样取出"
    assert port.query("SELECT count(*) AS n FROM t", ()) == [{"n": 1}], "表还得在"


def test_query_with_no_rows_is_empty_list(port: SqliteStorePort) -> None:
    port.execute("CREATE TABLE t (k TEXT)", ())
    assert port.query("SELECT k FROM t WHERE k=?", ("nope",)) == []


# ------------------------------------------------------------------- 全文通道
def test_fts_search_hits_and_orders_by_score(kb: SqliteStorePort) -> None:
    """命中与排序：短文档 d2 的 bm25 应高于长文档 d1，分数严格递减。"""
    hits = kb.fts_search("kb_doc", "body", "timeout", 5)

    assert [doc_id for doc_id, _ in hits] == ["d2", "d1"]
    scores = [score for _, score in hits]
    assert scores == sorted(scores, reverse=True), "分数必须越大越相关、降序返回"
    assert scores[0] > scores[1] > 0.0


def test_fts_search_limit_is_honoured(kb: SqliteStorePort) -> None:
    assert len(kb.fts_search("kb_doc", "body", "timeout", 1)) == 1
    assert kb.fts_search("kb_doc", "body", "timeout", 0) == []


def test_fts_search_terms_are_anded(kb: SqliteStorePort) -> None:
    """多个词是 FTS5 缺省的 AND：两个词都在的只有 d1。"""
    assert [doc_id for doc_id, _ in kb.fts_search("kb_doc", "body", "refund timeout", 5)] == ["d1"]
    assert kb.fts_search("kb_doc", "body", "refund shipping", 5) == []


def test_fts_search_chinese_needs_trigram_and_three_chars(kb: SqliteStorePort) -> None:
    """中文的两条硬事实，都不报错，所以必须由测试记着。

    影子表建成 trigram 才能子串命中；而 trigram 对 <3 字符的查询切不出任何 token，
    「退款」这种两字词恒返回空集。检索器把它误读成「库里没有」就查不出来了。
    """
    assert [doc_id for doc_id, _ in kb.fts_search("kb_doc", "body", "退款政策", 5)] == ["d5"]
    assert kb.fts_search("kb_doc", "body", "退款", 5) == [], "trigram 下两字查询恒空"


def test_fts_search_survives_hostile_query_text(kb: SqliteStorePort) -> None:
    """用户输入里的 FTS5 算子字符不许把查询打成语法错。"""
    for hostile in ['a" OR 1=1 --', "NEAR(", "*", "col:", "-x", "   "]:
        assert kb.fts_search("kb_doc", "body", hostile, 5) == []


def test_fts_search_without_shadow_table_raises_instead_of_falling_back(
    port: SqliteStorePort,
) -> None:
    """影子表不在就抛，且报错里得带上能直接粘的 DDL —— 不许回落 LIKE。"""
    with pytest.raises(LookupError) as err:
        port.fts_search("knowledge", "body", "退款政策", 5)

    msg = str(err.value)
    assert "knowledge_fts" in msg
    assert "CREATE VIRTUAL TABLE" in msg and "trigram" in msg


@pytest.mark.parametrize("bad", ["kb_doc; DROP TABLE kb_doc", "1bad", "", "kb doc", "kb-doc"])
def test_identifiers_are_validated_before_interpolation(kb: SqliteStorePort, bad: str) -> None:
    """表名/列名绑不了只能拼，所以拼之前必须卡形状。"""
    with pytest.raises(ValueError):
        kb.fts_search(bad, "body", "timeout", 5)
    with pytest.raises(ValueError):
        kb.vector_search("kb_doc", bad, [1.0, 0.0, 0.0], 5)


# ------------------------------------------------------------------- 向量通道
def test_vector_search_ranks_by_cosine(kb: SqliteStorePort) -> None:
    hits = kb.vector_search("kb_doc", "emb", [1.0, 0.0, 0.0], 3)

    assert [doc_id for doc_id, _ in hits] == ["d1", "d2", "d3"]
    assert hits[0][1] == pytest.approx(1.0)
    assert hits[1][1] == pytest.approx(0.99388, abs=1e-4)
    assert hits[2][1] == pytest.approx(0.0)


def test_vector_search_dimension_mismatch_names_the_row(kb: SqliteStorePort) -> None:
    """维度对不上是换了嵌入模型没重算，不许跳过那行 —— 跳过就没有症状了。"""
    kb.execute("UPDATE kb_doc SET emb=? WHERE id=?", (json.dumps([1.0, 0.0]), "d3"))

    with pytest.raises(ValueError) as err:
        kb.vector_search("kb_doc", "emb", [1.0, 0.0, 0.0], 3)

    assert "d3" in str(err.value)


def test_vector_search_rejects_zero_query_vector(kb: SqliteStorePort) -> None:
    with pytest.raises(ValueError):
        kb.vector_search("kb_doc", "emb", [0.0, 0.0, 0.0], 3)


# ---------------------------------------------------------------- 后端选择
def test_dialect_values(port: SqliteStorePort) -> None:
    assert port.dialect() == "sqlite"
    assert PgStorePort().dialect() == "postgres"


def test_postgres_backend_raises_and_never_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """本轨最要紧的一条：选 PG 必须当场响，不许悄悄给一个 sqlite。"""
    monkeypatch.setenv(BACKEND_ENV, "postgres")

    with pytest.raises(NotImplementedError) as err:
        create_store()

    assert "postgres" in str(err.value)
    monkeypatch.setenv(BACKEND_ENV, "sqlite")
    assert create_store().dialect() == "sqlite", "换回缺省应照常可用"


def test_postgres_shell_raises_on_every_operation() -> None:
    shell = PgStorePort("postgres://ignored")
    with pytest.raises(NotImplementedError):
        shell.execute("SELECT 1", ())
    with pytest.raises(NotImplementedError):
        shell.query("SELECT 1", ())
    with pytest.raises(NotImplementedError):
        shell.fts_search("t", "body", "q", 1)
    with pytest.raises(NotImplementedError):
        shell.vector_search("t", "emb", [1.0], 1)


def test_unknown_backend_raises_value_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(BACKEND_ENV, "mysql")
    with pytest.raises(ValueError):
        create_store()


def test_default_backend_is_sqlite_when_env_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(BACKEND_ENV, raising=False)
    assert create_store().dialect() == "sqlite"


def test_pg_dsn_comes_from_env_and_repr_hides_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """铁律 6：DSN 带口令，不许顺着 repr 漏进日志或 evidence。"""
    monkeypatch.setenv(DSN_ENV, "postgresql://u:s3cr3t@db/maos")

    shell = PgStorePort()

    assert shell.dsn == "postgresql://u:s3cr3t@db/maos"
    assert "s3cr3t" not in repr(shell) and "已配置" in repr(shell)


# ---------------------------------------------------------------- 事务与组合
def test_transaction_commits_once_and_rolls_back_on_error(port: SqliteStorePort) -> None:
    """退款域现在直接借核心 Store 的私有 `_conn` / `_lock` 干这件事，换成这个入口。"""
    port.execute("CREATE TABLE t (k TEXT)", ())

    with pytest.raises(RuntimeError):
        with port.transaction():
            port.execute("INSERT INTO t (k) VALUES (?)", ("rolled-back",))
            raise RuntimeError("boom")
    assert port.query("SELECT count(*) AS n FROM t", ()) == [{"n": 0}]

    with port.transaction():
        port.execute("INSERT INTO t (k) VALUES (?)", ("kept",))
    assert port.query("SELECT count(*) AS n FROM t", ()) == [{"n": 1}]


def test_adapter_is_composition_not_inheritance() -> None:
    """手册禁区：适配器不是重写。继承核心 Store 就等于给了改写它的口子。"""
    assert not issubclass(SqliteStorePort, CoreStore)

    core = CoreSqliteStore(":memory:")
    core.init_schema()
    wrapped = SqliteStorePort(core)

    #: 包住之后核心 Store 自己的方法照常可用，两边看到同一条连接。
    wrapped.execute(
        "INSERT INTO plan (plan_id, trace_id, goal, state, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?)",
        ("p1", "t1", "g", "PENDING", "now", "now"),
    )
    assert core.get_plan("p1")["goal"] == "g"


def test_adapter_refuses_a_store_without_a_connection() -> None:
    with pytest.raises(TypeError):
        SqliteStorePort(object())


# ---------------------------------------------------------------- F-2 冻结
def test_f2_signatures_are_frozen() -> None:
    """五个签名是跨轨契约。这条红了先停手问人，别顺手改测试。"""
    for cls in (StorePort, SqliteStorePort, PgStorePort):
        for name, expected in F2_SIGNATURES.items():
            assert _sig(getattr(cls, name)) == expected, f"{cls.__name__}.{name} 的签名漂了"


def test_sqlite_and_pg_both_satisfy_the_protocol(port: SqliteStorePort) -> None:
    assert isinstance(port, StorePort)
    assert isinstance(PgStorePort(), StorePort)
