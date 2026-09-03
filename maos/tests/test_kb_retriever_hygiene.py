"""T25 的机器验收 —— 检索层三条卫生：判定自愈 / 影子表口径 / 退化路径记账。

## 三条各自在守什么

**一、通道判定不许粘过一次 schema 迁移。** `_PORT_STATE` 是按 store 对象记的
粘性判定（`_port_search` 的「只告警一次」就靠它），老库上探出 `False` 就一直
`False`。T17 之后老库**能**就地升到目标形状 —— 升完端口通道其实通了，而这张表
还按「不通」走，于是「库已经好了、检索却还按坏的走」，且**没有任何红灯**：召回
照常、分数照常、日志一条不多。长跑进程里唯一的解药是重开进程或换端口对象，而
没人知道自己需要这剂药。本束把「同一个对象上自愈」钉成机器判据。

自愈的实现是「判定跟着 schema 版本走，版本一动整张作废」，所以反面同样要钉住：
**版本没动的时候判定必须照旧粘住**。作废做过头就成了每次重探，「只告警一次」当场
失效，日志被刷满 —— 那是把本条要买的东西又卖了回去。

**二、`PORT_FTS_FIELDS` 与影子表的索引列是两处口径，必须机器对齐。** 端口全文
通道逐列各问一次，问哪几列写死在 `PORT_FTS_FIELDS`；影子表里哪几列真的进索引写在
`schema.sql`。两处一致纯属人工维持 —— 谁往影子表加一列忘了改这边，症状是**那一列
命中的知识在端口后端上召不回来**，本地后端却召得回来，两边都不报错。

**三、本地 FTS5 退化路径的失败要按 store 只记一次账。** `bm25()` 与影子表都是
SQLite FTS5 专有的，PG 后端上这条路走不通。正路是端口的 `fts_search`（PG 侧
T18 已填实），可一旦端口那条也走不通，检索就每次落到这里失败一次、刷一条告警 ——
把 `_port_search` 那条真告警淹掉。口径与端口通道同一套记账，不另起一套。

## 与既有几束的分工

`test_kb_schema_migration.py` 守「老库升得上去」，它那条
`test_migration_restores_both_port_channels` 刻意**重新造一个端口对象**来绕开
粘性判定；本束守的正是「不绕也行」。`test_kb_pg_channel.py` 守两条端口通道的口径
一致，本束不重复它，只补它没盖到的第三条通道（本地退化）与影子表那处口径。
"""

from __future__ import annotations

import contextlib
import gc
import logging
import re
import sqlite3
from collections.abc import Iterator

import pytest

from maos import kb
from maos.core.store import SqliteStore
from maos.kb import retriever
from maos.store.sqlite_store import SqliteStorePort

TENANT = "tnt-t25"

QUERY = {"tenant_id": TENANT, "biz_type": "refund", "keyword": "timeout"}

#: `timeout` 只出现在 body 且是单个英数 token —— 与 `test_kb_pg_channel.py`
#: 同一套取舍，让端口/本地两条通道的比较不被分词与跨列 OR 的差异搅浑。
CORPUS = (
    ("doc-a", "支付回执缺失", "gateway timeout retry twice", "AS-101"),
    ("doc-b", "回执延迟", "gateway timeout then settled", "AS-102"),
    ("doc-c", "包装破损", "carton crushed on arrival", "AS-103"),
)

#: T13 之前的形状，**只取本束用得着的那一点**：两张表都没有 `id` 列。
#: 完整的历史形状在 `test_kb_schema_migration.py` 的 `PRE_T13_KB_SCHEMA` ——
#: 那一束守的是「迁移后与新库逐项相同」，需要全量；本束只要「端口通道走不通、
#: 而 `ensure_schema()` 能把它修好」，抄全量进来反而多一份要同步的副本。
#: `channel_id` / `region` / `sku` 三列本身用不上，但 `schema.sql` 的
#: `idx_kb_doc_prefilter` 建在它们上面 —— 少一列 `ensure_schema()` 当场炸。
PRE_T13_MINIMAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_doc (
    tenant_id  TEXT NOT NULL,
    doc_id     TEXT NOT NULL,
    biz_type   TEXT,
    channel_id TEXT,
    region     TEXT,
    sku        TEXT,
    kind       TEXT NOT NULL,
    rule_no    TEXT,
    title      TEXT NOT NULL DEFAULT '',
    body       TEXT NOT NULL DEFAULT '',
    embedding  TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, doc_id)
);
CREATE VIRTUAL TABLE IF NOT EXISTS kb_doc_fts USING fts5(
    doc_id UNINDEXED,
    tenant_id UNINDEXED,
    title,
    body
);
"""


# --------------------------------------------------------------------------- 夹具
def _core() -> SqliteStore:
    store = SqliteStore()
    store.init_schema()
    return store


def _old_db() -> SqliteStore:
    """T13 之前形状、且已经有存量数据的库。

    灌数据走裸 SQL 而不是 `kb.upsert_doc()` —— 后者第一件事就是 `ensure_schema()`，
    一调就把库升上去了，那这个夹具就没有老库可言。
    """
    store = _core()
    conn = store._conn
    conn.executescript(PRE_T13_MINIMAL_SCHEMA)
    for doc_id, title, body, rule_no in CORPUS:
        conn.execute(
            "INSERT INTO kb_doc (tenant_id, doc_id, kind, biz_type, rule_no,"
            " title, body, embedding, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (TENANT, doc_id, kb.KIND_POLICY, "refund", rule_no, title, body,
             kb.json.dumps(retriever.embed(f"{title} {body}")),
             "2026-01-01T00:00:00+00:00"))
        conn.execute(
            "INSERT INTO kb_doc_fts (doc_id, tenant_id, title, body) VALUES (?,?,?,?)",
            (doc_id, TENANT, kb.fts_text(title), kb.fts_text(body)))
    conn.commit()
    return store


def _fresh_db() -> SqliteStore:
    store = _core()
    kb.ensure_schema(store)
    for doc_id, title, body, rule_no in CORPUS:
        kb.upsert_doc(store, {
            "tenant_id": TENANT, "doc_id": doc_id, "kind": kb.KIND_POLICY,
            "biz_type": "refund", "rule_no": rule_no, "title": title, "body": body,
            "embedding": retriever.embed(f"{title} {body}")})
    return store


class _BrokenPort(SqliteStorePort):
    """两条端口通道都走不通，其余照常 —— 「后端在、但索引/扩展没准备好」。"""

    def fts_search(self, table, field, q, limit):
        raise RuntimeError("tsvector 索引没建")

    def vector_search(self, table, field, vec, limit):
        raise RuntimeError("pgvector 扩展没装")


class _FakePgPort:
    """假 PG 端口：`kb_doc` 那半边照常，FTS5 专有的那半边一律不认。

    **刻意不叫 `_conn`** —— `kb.port_of()` 的口径是 `_conn` 优先，叫那个名字就会
    被认成 `SqliteStore` 走老路径，端口那条分支一次都进不去。

    用假端口而不是起一个真 PG：本束要复现的是「`bm25()` 与影子表在这个后端上不
    存在」，那是一条**语法层面**的事实，真库只是把它复述一遍，却要一个跑不动
    就整束 skip 的外部依赖。
    """

    def __init__(self, store: SqliteStore) -> None:
        # 攥住 store 本身而不只是它的连接：只留连接的话 store 随时可能被回收，
        # 底下这条 sqlite 连接跟着一起没，症状是偶发的 ProgrammingError。
        self._store = store
        self._sqlite: sqlite3.Connection = store._conn

    @staticmethod
    def _reject(sql: str) -> None:
        if "kb_doc_fts" in sql or "bm25(" in sql:
            raise RuntimeError('relation "kb_doc_fts" does not exist')

    def execute(self, sql: str, params: tuple = ()) -> None:
        self._reject(sql)
        self._sqlite.execute(sql, tuple(params))
        self._sqlite.commit()

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        self._reject(sql)
        return [dict(r) for r in self._sqlite.execute(sql, tuple(params)).fetchall()]

    def fts_search(self, table, field, q, limit):
        raise RuntimeError("tsvector 索引没建")

    def vector_search(self, table, field, vec, limit):
        raise RuntimeError("pgvector 扩展没装")


@contextlib.contextmanager
def _warnings_of(logger_name: str = "maos.kb") -> Iterator[list[str]]:
    """收这一支 logger 上的 WARNING 原文。

    自带一个而不是用 pytest 的 `caplog`，两个理由：

    1. `caplog` 收的是 root 上的所有记录，别的模块顺手写一行就把「刷了几条」带偏；
    2. 它把 `LogRecord` **整条攥到测试结束**，而本模块的退化告警把异常对象当参数传
       （`log.warning("…（%s）…", exc)`）—— 异常连着 traceback、traceback 连着抛出
       它的那个栈帧、栈帧连着 `self`，于是**端口对象在测试期间死不掉**。判「弱引用
       有没有丢」的那条会因此恒红，红的却是 pytest 的日志捕获，不是本模块。

    所以这里当场把记录**转成字符串**再存，并在作用域内掐掉 propagate ——
    异常对象出不了这个 with，root 上的捕获器也见不到它。
    """
    lines: list[str] = []

    class _Sink(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(record.getMessage())          # 只留正文，不留 record

    logger = logging.getLogger(logger_name)
    handler = _Sink(level=logging.WARNING)
    previous_level, previous_propagate = logger.level, logger.propagate
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    logger.propagate = False
    try:
        yield lines
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate


def _local_fts_lines(lines: list[str]) -> list[str]:
    return [m for m in lines if "本地 FTS5 检索失败" in m]


# ---------------------------------------------------------------------------
# 5.1 粘性判定跟着 schema 版本走
# ---------------------------------------------------------------------------
def test_old_db_still_degrades_both_port_channels():
    """前提复现：没升级的老库上两条端口通道确实探不通。

    这条绿了，下一条的「自愈」才有对照；它红了说明夹具没造出老库形状，
    后面几条全是空跑。
    """
    port = SqliteStorePort(_old_db())
    retriever.retrieve(port, QUERY, limit=10)
    assert retriever.port_channel_state(port) == {"fts_search": False,
                                                  "vector_search": False}


def test_in_place_migration_heals_the_sticky_verdict_on_the_same_port():
    """**本条是本轨要买的东西**：同一个端口对象上，迁移之后判定自己作废重探。

    对照的是 `test_kb_schema_migration.py` 那条 —— 它得重新造一个端口对象才看得
    见通道恢复。这里从头到尾**只有一个 `port`**，中间只插一次 `ensure_schema()`。
    """
    port = SqliteStorePort(_old_db())

    retriever.retrieve(port, QUERY, limit=10)
    before = retriever.port_channel_state(port)

    kb.ensure_schema(port)                             # 就地升级，同一个对象
    after_migration = retriever.port_channel_state(port)

    hits = retriever.retrieve(port, QUERY, limit=10)
    after = retriever.port_channel_state(port)

    assert before == {"fts_search": False, "vector_search": False}
    assert after_migration == {}, \
        "迁移之后旧判定还挂在表上 —— 自证接口读到的是过期结论"
    assert after == {"fts_search": True, "vector_search": True}, \
        "库升上去了、判定却没自愈 —— 长跑进程里检索会一直走本地实现"
    assert hits, "自愈之后端口通道一条都没召回"


def test_verdict_stays_sticky_while_the_schema_does_not_move():
    """反面：版本没动就**不许**重探，否则「只告警一次」当场失效。

    端口坏掉是「后端没准备好」，不是「库形状旧」—— 这种坏法重探一万次也还是坏的，
    每次检索刷一条告警只会把日志淹掉，而淹掉的正是第一条真告警。
    """
    port = _BrokenPort(_fresh_db())

    with _warnings_of() as lines:
        for _ in range(5):
            retriever.retrieve(port, QUERY, limit=10)

    assert retriever.port_channel_state(port) == {"fts_search": False,
                                                  "vector_search": False}
    fts_lines = [m for m in lines if "StorePort.fts_search" in m]
    assert len(fts_lines) == 1, \
        f"版本没动却重探了，告警刷了 {len(fts_lines)} 条：{fts_lines}"


def test_a_healthy_port_never_pays_for_the_version_lookup(monkeypatch):
    """热路径上不许多发那条 `SELECT MAX(version)`。

    版本号只在「手上有 False 判定」时才去读：`True` 判定不短路（每次仍旧真调端口，
    端口坏了自会抛出来翻成 False），作废它买不到任何东西，却要在每次检索上多一次
    往返 —— PolarDB 上那是真的网络开销，而这一层的开销约定是「每列一次的常数」。
    """
    port = SqliteStorePort(_fresh_db())         # 建库本身要查版本，先建完再挂钩子

    calls = []
    real = kb.applied_schema_version
    monkeypatch.setattr(kb, "applied_schema_version",
                        lambda store: calls.append(store) or real(store))

    for _ in range(3):
        retriever.retrieve(port, QUERY, limit=10)

    assert retriever.port_channel_state(port) == {"fts_search": True,
                                                  "vector_search": True}
    assert calls == [], f"端口全通却查了 {len(calls)} 次 schema 版本"


def test_neither_state_table_leaks_short_lived_stores():
    """两张 `WeakKeyDictionary` 的弱引用语义都不许丢。

    判定表现在是**两张**（判定 + 版本戳）。第二张要是写成普通 dict，每个短命
    store 都会在里面留一份，症状是慢性泄漏 —— 不报错、不变慢，只是内存一直涨。

    退化告警必须收进 `_warnings_of`：它掐掉 propagate，否则 pytest 的日志捕获会
    把带异常的 `LogRecord` 攥到测试结束，端口对象跟着死不掉（见那个函数的注释）。
    """
    gc.collect()                                       # 先把别束留下的待收对象扫掉
    before = (len(retriever._PORT_STATE), len(retriever._PORT_STATE_SCHEMA))

    ports = [_BrokenPort(_fresh_db()) for _ in range(20)]
    with _warnings_of():
        for port in ports:
            retriever.retrieve(port, QUERY, limit=10)
    peak = (len(retriever._PORT_STATE), len(retriever._PORT_STATE_SCHEMA))

    del ports, port                                    # 循环变量也攥着最后那一个
    gc.collect()
    after = (len(retriever._PORT_STATE), len(retriever._PORT_STATE_SCHEMA))

    assert peak[0] >= before[0] + 20 and peak[1] >= before[1] + 20, \
        f"20 个退化端口没被两张表记全：{before} -> {peak}"
    assert after == before, f"临时 store 没被回收：{before} -> {peak} -> {after}"


# ---------------------------------------------------------------------------
# 5.2 PORT_FTS_FIELDS 与影子表索引列，两处口径机器对齐
# ---------------------------------------------------------------------------
def _shadow_table_ddl(store: SqliteStore) -> str:
    rows = kb.query(store, "SELECT sql FROM sqlite_master WHERE name = ?",
                    ("kb_doc_fts",))
    assert rows, "影子表 kb_doc_fts 不在库里 —— 夹具没建起来"
    return rows[0]["sql"]


def _indexed_columns(ddl: str) -> tuple[str, ...]:
    """从影子表建表语句里解出**真正参与索引**的列，按声明顺序。

    走 `sqlite_master` 而不是 `PRAGMA table_info` —— 后者对 FTS5 虚表把五列报得
    一模一样（type 全是空串、notnull 全是 0），`UNINDEXED` 一点痕迹都不留，拿它
    做判据等于把三列 UNINDEXED 也算成索引列，断言永远绿。实测见 T25 回执。

    解析口径：`USING fts5(...)` 括号里按逗号切，跳过 fts5 的选项（`tokenize=...`
    那类带 `=` 的项），列名取每项第一个 token，标了 `UNINDEXED` 的丢掉。
    """
    body = re.search(r"USING\s+fts5\s*\((.*)\)", ddl, re.S | re.I)
    assert body, f"这不是一条 fts5 建表语句：{ddl!r}"
    columns = []
    for chunk in body.group(1).split(","):
        parts = chunk.split()
        if not parts or "=" in chunk:                  # 空项 / fts5 选项，不是列
            continue
        if any(p.upper() == "UNINDEXED" for p in parts[1:]):
            continue
        columns.append(parts[0])
    return tuple(columns)


@pytest.mark.parametrize("factory", [_fresh_db, _old_db], ids=["fresh", "old"])
def test_port_fts_fields_equal_the_indexed_columns_of_the_shadow_table(factory):
    """`PORT_FTS_FIELDS` == 影子表里真正参与索引的列。两处口径，一条机器判据。

    端口全文通道逐列各问一次，问哪几列由 `PORT_FTS_FIELDS` 写死；影子表哪几列
    进索引由 `schema.sql` 写死。谁往影子表加一列忘了改这边，症状是**那一列命中的
    知识在端口后端上召不回来**，本地后端却召得回来 —— 两边都不报错，只有召回悄悄
    变少。新库与老库两条路都要过：老库的影子表是迁移**重建**出来的，形状同样算数。
    """
    store = factory()
    kb.ensure_schema(store)
    indexed = _indexed_columns(_shadow_table_ddl(store))

    assert indexed == retriever.PORT_FTS_FIELDS, (
        f"两处口径对不上：影子表索引列 {indexed}，"
        f"PORT_FTS_FIELDS {retriever.PORT_FTS_FIELDS}；"
        f"端口没问的列 {tuple(c for c in indexed if c not in retriever.PORT_FTS_FIELDS)}，"
        f"问了但没进索引的列 "
        f"{tuple(c for c in retriever.PORT_FTS_FIELDS if c not in indexed)}")


def test_the_ddl_parse_agrees_with_what_fts5_actually_matches():
    """交叉验证：解析出来的那几列，就是 `MATCH` 真能命中的那几列。

    上一条读的是建表语句的**字面**，这条读的是 FTS5 的**行为** —— 每列塞一个独一
    无二的 token，再逐个 MATCH。两条一起才算把口径钉死：解析写歪了（比如把
    `UNINDEXED` 认成列名的一部分）会被这条抓住。
    """
    store = _fresh_db()
    all_columns = ("id", "doc_id", "tenant_id", "title", "body")
    probes = {c: f"zzprobe{c.replace('_', '')}" for c in all_columns}
    kb.execute(store,
               "INSERT INTO kb_doc_fts (id, doc_id, tenant_id, title, body)"
               " VALUES (?,?,?,?,?)",
               tuple(probes[c] for c in all_columns))

    matched = tuple(
        c for c in all_columns
        if kb.query(store, "SELECT count(*) AS n FROM kb_doc_fts"
                           " WHERE kb_doc_fts MATCH ?", (probes[c],))[0]["n"])

    assert matched == _indexed_columns(_shadow_table_ddl(store)), \
        "建表语句解出来的索引列，与 FTS5 实际能 MATCH 到的列对不上"
    assert matched == retriever.PORT_FTS_FIELDS


# ---------------------------------------------------------------------------
# 5.3 本地退化路径的失败记账
# ---------------------------------------------------------------------------
def _seeded_pg_port() -> _FakePgPort:
    return _FakePgPort(_fresh_db())


def test_pg_backend_really_falls_through_to_the_dead_local_path():
    """前提复现：PG 后端 + 端口通道走不通 = 检索落到本地那条死路上，且真的失败。

    两件事都要成立才谈得上记账 —— 真的**走到了**（端口通道退化），且真的**失败了**
    （`bm25()` 与影子表在这个后端上不存在）。
    """
    port = _seeded_pg_port()

    with _warnings_of() as lines:
        retriever.retrieve(port, QUERY, limit=10)

    assert retriever.port_channel_state(port) == {"fts_search": False,
                                                  "vector_search": False}
    assert retriever.local_fts_state(port) is False, "本地退化路径压根没走到"
    assert len(_local_fts_lines(lines)) == 1


def test_local_fts_failure_is_accounted_once_per_store():
    """同一个 store 连查 5 次，本地退化路径的失败只刷 1 条。

    口径与 `_port_search` 的「只告警一次」同一套记账（`_port_state`），不另起一套：
    两套记账迟早在「到底告警过没有」上打架，而打架的症状是日志时多时少。
    """
    port = _seeded_pg_port()

    with _warnings_of() as lines:
        for _ in range(5):
            retriever.retrieve(port, QUERY, limit=10)

    hit = _local_fts_lines(lines)
    assert len(hit) == 1, f"5 次检索刷了 {len(hit)} 条本地 FTS 告警"


def test_each_store_gets_its_own_line():
    """记账按 store 记，不是按进程记 —— 换一个后端要能重新说话。

    写成模块级的「全局只告警一次」，第二个库坏掉时就一声不吭了。
    """
    with _warnings_of() as lines:
        for _ in range(3):
            retriever.retrieve(_seeded_pg_port(), QUERY, limit=10)

    assert len(_local_fts_lines(lines)) == 3


def test_accounting_does_not_short_circuit_the_local_path(monkeypatch):
    """记账只管住日志，**不管住调用** —— 别把 SQLite 的兜底一并拆了。

    本地 FTS5 在 SQLite 上是正路：库刚建好、影子表刚迁移完这类「这一刻不行、下一刻
    行」是常态。端口通道短路是因为它坏了就是坏了，本地这条不一样，失败之后照试不误。
    """
    store = _fresh_db()
    calls: list[str] = []
    real_query = kb.query

    def flaky_query(target, sql, params=()):
        if "kb_doc_fts" in sql and "MATCH" in sql:
            calls.append(sql)
            if len(calls) <= 2:                        # 前两次假装影子表还没准备好
                raise RuntimeError("影子表还在重建")
        return real_query(target, sql, params)

    monkeypatch.setattr(kb, "query", flaky_query)
    with _warnings_of() as lines:
        for _ in range(4):
            retriever.retrieve(store, QUERY, limit=10)

    assert len(calls) == 4, f"失败之后不再试了：只发了 {len(calls)} 次本地 FTS 查询"
    assert len(_local_fts_lines(lines)) == 1
    assert retriever.local_fts_state(store) is True, "恢复之后判定没跟着翻回来"


def test_local_channel_stays_out_of_port_channel_state():
    """本地退化通道共用同一张表，但**不许**混进 `port_channel_state()`。

    那个接口是「换后端之后端口通没通」的自证判据，混进第三条通道会让它读起来含糊，
    而它正是几束验收共同引用的那一个。
    """
    store = _fresh_db()
    retriever.retrieve(store, QUERY, limit=10)

    assert retriever.local_fts_state(store) is True
    assert retriever.port_channel_state(store) == {}, \
        "核心 SqliteStore 没有端口方法，两条端口通道就该是「没探过」"
