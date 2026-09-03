"""跨后端全文名次一致性 —— T18 给 `ts_rank` 补长度归一之后，唯一能钉住它的东西。

**没库就整个 skip，绝不红。** 判据与 `test_pg_store_live.py` 同一套：`MAOS_PG_DSN`
未设或连不上就是没库，那是 CI、别人的机器、以及本仓库缺省 SQLite 路径上的常态。
起库见那个模块的 docstring。

## 为什么要有这个文件

`ts_rank` 缺省**不做文档长度归一**，`bm25` 做。T10 轨实测、T18 轨在自己的库上复跑
确认：同一条查询 `timeout`、同一份语料，PG 侧 `d1`(6 词) 与 `d2`(1 词) 同分
`0.06079271`，并列后按 id 升序排成 `['d1', 'd2']`；SQLite 侧 `-bm25` 排成
`['d2', 'd1']`。

两边都满足 F-2 的「越大越相关、降序、同分按 id 升序」，所以**没有任何东西会报错**，
但**名次不同**。而 `maos/kb/retriever.py` 的 `_rank_normalize` 正是按**名次**归一的
—— 于是「换个后端，混合召回的最终排序悄悄变了」。这是铁律 8 点名要防的那类假象：
看上去在工作，结论却是错的，而且没有症状。

口径**以本地 `-bm25` 为准**（本地是缺省路径、是全部现有测试与演示的基准，让 PG 向
它对齐影响面最小），理由记在 docs/DECISIONS.md。实现是给 `ts_rank` 传
`maos/store/pg_store.py` 的 `FTS_RANK_NORMALIZATION`。

## 这里守四件事

1. **名次一致**。两份语料、同一条查询，两个后端返回的 id 序列必须逐个相同。
   分数不比 —— 两边的分数量纲差着四个数量级，本来就不可跨后端比较。
2. **语料真的能区分**。光断言「两边一样」是不够的：如果语料本身在不归一时也碰巧
   一样，这个文件就是一张永远绿的空票。所以有一条**反面对照**，拿裸 SQL 复现
   「不传 normalization」那条查询，断言它与本地**确实不一致** —— 归一化被谁删掉的
   那天，第 1 条会红，而这一条负责说清红的原因。
3. **生效的确实是那个常量**。适配器返回的分数必须与 `ts_rank(..., 那个常量)` 逐位
   相同。只改常量不改 SQL、或只改 SQL 不改常量，都会在这条上当场红。
4. **F-2 的同分附则还在**。归一化改的是分数，不许把「同分按 id 升序」改掉。

## 为什么第 2 组语料的 id 是反着排的

`skewed` 组里**最长的文档 id 最小**（`g1` 61 词、`g2` 9 词、`g3` 1 词）。这是刻意的：
不归一时三篇同分，F-2 的附则让它们按 id 升序排成 `['g1', 'g2', 'g3']`，恰好与本地
按长度排出的 `['g3', 'g2', 'g1']` **完全相反**。要是让最短的 id 最小，同分并列的
名次会碰巧和本地一样，这组就白设了 —— 第一版就是这么写的，实跑发现前后读数一模一样
才改过来。
"""

from __future__ import annotations

import functools
import os

import pytest

from maos.store import SqliteStorePort, create_store
from maos.store.pg_store import DSN_ENV, FTS_RANK_NORMALIZATION, PgStorePort

#: 靶表前缀。带轨号，免得跟 t10_live_doc 或别人的表撞上。
TABLE_PREFIX = "t18_parity_"

#: 本地侧的源表与 FTS5 影子表。trigram 是本仓库的既定口径（中文缺省 unicode61
#: 会把整串汉字切成一个 token），跟 test_store_port.py 的 kb fixture 保持一致。
LOCAL_TABLE = "parity_doc"

#: 两组语料共用的查询词。选 `timeout` 是为了跟 T10 的原始读数对得上。
QUERY = "timeout"

#: 每篇都恰好含 `timeout` 一次，tf 相同 —— 名次差异因此**只来自文档长度**。
CORPORA: dict[str, list[tuple[str, str]]] = {
    # T10 那份语料，与 test_pg_store_live.ROWS / test_store_port.kb 同源。
    # d3 不含 timeout，是陪衬，用来确认命中集合没被归一化改掉。
    "shared": [
        ("d1", "refund timeout window is seven days"),
        ("d2", "timeout"),
        ("d3", "shipping policy has nothing to do with it here"),
    ],
    # 长度差异拉到 61 : 9 : 1，且 id 顺序与长度顺序同向（见模块 docstring 末节）。
    "skewed": [
        ("g1", " ".join(["filler"] * 30) + " timeout " + " ".join(["padding"] * 30)),
        ("g2", "the timeout value is configured in the settings file"),
        ("g3", "timeout"),
    ],
    # 同长同 tf：两边都该同分，然后按 id 升序 —— F-2 的附则。
    "tie": [
        ("h1", "timeout window"),
        ("h2", "timeout window"),
    ],
}


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
    reason=f"没有可连的 PG：{DSN_ENV} 未设或连不上。起库见 test_pg_store_live.py。",
)


def _local_port(rows: list[tuple[str, str]]) -> SqliteStorePort:
    """一张建好 FTS5 影子表的本地靶表。`backend=` 写死 sqlite：本机此刻 DSN 是配着的，
    不写死的话工厂会去读 `MAOS_STORE_BACKEND`，被别处的环境变量牵着走。"""
    port = create_store(backend="sqlite")
    port.execute(f"CREATE TABLE {LOCAL_TABLE} (id TEXT PRIMARY KEY, body TEXT)", ())
    port.execute(
        f"CREATE VIRTUAL TABLE {LOCAL_TABLE}_fts"
        f" USING fts5(id UNINDEXED, body, tokenize='trigram')",
        (),
    )
    for doc_id, body in rows:
        port.execute(f"INSERT INTO {LOCAL_TABLE} (id, body) VALUES (?,?)", (doc_id, body))
        port.execute(f"INSERT INTO {LOCAL_TABLE}_fts (id, body) VALUES (?,?)", (doc_id, body))
    return port


class _Bench:
    """同一份语料的两条腿：一条落在真 PG 上，一条落在本地 SQLite 上。"""

    def __init__(self, pg: PgStorePort) -> None:
        self.pg = pg
        self._local = {name: _local_port(rows) for name, rows in CORPORA.items()}

    def table(self, name: str) -> str:
        return TABLE_PREFIX + name

    def pg_hits(self, name: str, q: str = QUERY) -> list[tuple[str, float]]:
        """走真适配器 —— 测的必须是产线那条路径，不是测试自己拼的 SQL。"""
        return self.pg.fts_search(self.table(name), "body", q, 10)

    def local_hits(self, name: str, q: str = QUERY) -> list[tuple[str, float]]:
        return self._local[name].fts_search(LOCAL_TABLE, "body", q, 10)

    def pg_hits_raw(self, name: str, normalization: int | None) -> list[tuple[str, float]]:
        """裸 SQL 版 `ts_rank`，`normalization=None` 就是改造前那条查询。"""
        rank = "ts_rank(to_tsvector('simple', body), plainto_tsquery('simple', %s)"
        rank += ")" if normalization is None else f", {int(normalization)})"
        sql = (
            f"SELECT id, {rank} AS score FROM {self.table(name)}"
            " WHERE to_tsvector('simple', body) @@ plainto_tsquery('simple', %s)"
            " ORDER BY score DESC, id ASC LIMIT 10"
        )
        rows = self.pg.query(sql, (QUERY, QUERY))
        return [(str(r["id"]), float(r["score"])) for r in rows]


def _ids(hits: list[tuple[str, float]]) -> list[str]:
    return [doc_id for doc_id, _ in hits]


@pytest.fixture(scope="module")
def bench() -> _Bench:
    """三张 PG 靶表 + 三个本地端口。用完 DROP，不给下一次留脏数据。"""
    pg = PgStorePort(_live_dsn())
    for name, rows in CORPORA.items():
        table = TABLE_PREFIX + name
        pg.execute(f"DROP TABLE IF EXISTS {table}", ())
        pg.execute(f"CREATE TABLE {table} (id TEXT PRIMARY KEY, body TEXT)", ())
        for doc_id, body in rows:
            pg.execute(f"INSERT INTO {table} (id, body) VALUES (%s, %s)", (doc_id, body))
    yield _Bench(pg)
    for name in CORPORA:
        pg.execute(f"DROP TABLE IF EXISTS {TABLE_PREFIX + name}", ())
    pg.close()


# --------------------------------------------------------------- 1. 名次一致
@pytest.mark.parametrize(
    ("corpus", "expected"),
    [
        # T10 的原始读数：本地把短的 d2 排在长的 d1 前面。归一化之前 PG 排反。
        ("shared", ["d2", "d1"]),
        # 长度 61 : 9 : 1，本地按「越短越相关」排。归一化之前 PG 排成 g1 g2 g3。
        ("skewed", ["g3", "g2", "g1"]),
    ],
)
def test_rank_order_is_identical_on_both_backends(
    bench: _Bench, corpus: str, expected: list[str]
) -> None:
    """名次必须逐个相同 —— 这正是 `_rank_normalize` 依赖的那个东西。

    分数**不比**：PG 侧是 1e-2 量级、本地侧是 1e-6 量级，跨后端比绝对值没有意义，
    F-2 也只要求「越大越相关、降序」。
    """
    pg_hits = bench.pg_hits(corpus)
    local_hits = bench.local_hits(corpus)

    assert _ids(pg_hits) == _ids(local_hits) == expected, (
        f"{corpus} 组名次没对齐：PG {_ids(pg_hits)} / 本地 {_ids(local_hits)}。"
        " 混合召回按名次归一，名次不一致等于换后端悄悄改排序，且不报错。"
    )
    for hits in (pg_hits, local_hits):
        scores = [score for _, score in hits]
        assert scores == sorted(scores, reverse=True), "F-2：越大越相关、降序返回"
        assert all(score > 0.0 for score in scores)


def test_hit_sets_are_identical_too(bench: _Bench) -> None:
    """归一化只许动名次，不许动命中集合 —— 陪衬文档 d3 不含 timeout，两边都不该命中。"""
    assert {i for i, _ in bench.pg_hits("shared")} == {"d1", "d2"}
    assert {i for i, _ in bench.local_hits("shared")} == {"d1", "d2"}


# ------------------------------------------------------- 2. 语料真的能区分
@pytest.mark.parametrize("corpus", ["shared", "skewed"])
def test_without_normalization_the_backends_would_disagree(
    bench: _Bench, corpus: str
) -> None:
    """反面对照：不传 normalization 时两边**确实**不一致。

    没有这一条，上面那条断言可能只是「语料碰巧对齐」的空票。这里同时钉住不一致的
    **成因**：不归一时每篇的 tf 都是 1，`ts_rank` 给出完全相同的分，于是名次退化成
    F-2 附则的「同分按 id 升序」—— 跟文档长度一点关系都没有。
    """
    raw = bench.pg_hits_raw(corpus, normalization=None)
    scores = [score for _, score in raw]

    assert len(set(scores)) == 1, (
        f"{corpus} 组在不归一时本该同分，实得 {scores}。同分是这组语料的构造前提"
        "（每篇 tf 都是 1），不同分说明语料被改过，下面那条断言的意义也就变了。"
    )
    assert _ids(raw) == sorted(_ids(raw)), "同分时应退化成按 id 升序"
    assert _ids(raw) != _ids(bench.local_hits(corpus)), (
        f"{corpus} 组在不归一时竟然与本地一致 —— 那这组语料区分不出归一化有没有生效，"
        " 上面那条名次断言会变成一张永远绿的空票。换一组语料，别删这条断言。"
    )


# --------------------------------------------------- 3. 生效的确实是那个常量
def test_adapter_uses_the_declared_normalization_constant(bench: _Bench) -> None:
    """适配器的分数必须与 `ts_rank(..., FTS_RANK_NORMALIZATION)` 逐位相同。

    只改常量不改 SQL、或只改 SQL 不改常量，都会在这条上当场红 —— 那两种改法各自
    都不会让「名次一致」立刻失效（别的 normalization 位在本语料上也可能碰巧对齐），
    所以值得单独钉一次。
    """
    declared = bench.pg_hits_raw("skewed", normalization=FTS_RANK_NORMALIZATION)

    assert bench.pg_hits("skewed") == declared, (
        "适配器返回的分数与常量声明的 normalization 对不上 ——"
        f" 常量是 {FTS_RANK_NORMALIZATION}，但 fts_search 拼的显然不是它。"
    )
    assert declared != bench.pg_hits_raw("skewed", normalization=None), (
        "传了 normalization 却与不传时逐位相同 —— 参数根本没进 SQL。"
    )


# ------------------------------------------------------- 4. F-2 的同分附则
def test_ties_still_break_by_id_ascending_after_normalization(bench: _Bench) -> None:
    """同长同 tf 的两篇：两边都该同分，然后按 id 升序。归一化不许动这条附则。"""
    for hits in (bench.pg_hits("tie"), bench.local_hits("tie")):
        assert _ids(hits) == ["h1", "h2"], "同分必须按 id 升序，否则结果不可复现"
        assert hits[0][1] == hits[1][1], "同长同 tf 的两篇不该被归一化拉开"
