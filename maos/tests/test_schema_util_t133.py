"""T133 —— 两份加列助手收成一处，且那一处在 PG 上真的加得上列。

## 这一束在守什么

p10 跨轨契约 §B.2 让 T116 / T117 各自复制一份加列助手到自己的模块里，
「整合期由主会话去重」。落下来的两份**行为不一样**，而且分叉点恰好是方言：
两份都拿 `PRAGMA table_info` 当探针，`case_pack.py` 那份包了 `try/except` 回落、
`compensate.py` 那份没包。`PRAGMA` 是 SQLite 方言，PG 上没有 —— 于是
`make_case_bundle.py --all-paths --domain-backend postgres` 在 PG 上只有 happy
一条路径跑得出来，凡走到 `ensure_ticket_schema()` 的路径当场
`SyntaxError: syntax error at or near "PRAGMA"`。

去重本身不难，**难的是不要去重成另一个静默失效**：把 `case_pack.py` 那份带回落的
助手原样装到 `compensate.py` 上，它在 PG 上就不抛了，六列却一列都没加 ——
因为回落的前提是「交给调用方的 SELECT 探针」，而 `ensure_ticket_schema()` 这条
路径上根本没有那个探针（T116 那条有）。症状要等到下游写 `compensation_record`
时才以 `UndefinedColumn` 冒出来，而那句报错完全不提加列这件事。T133 实测过这条。

所以本束的四条分三层：

1. **源码级**（`test_pragma_is_gone_from_both_loaders`）—— 两处都不许再有 PRAGMA。
   这条最直接：哪天有人图省事把 PRAGMA 写回去，它当场红，不必等 PG 束跑。
2. **语义级**（`test_apply_columns_raises_when_the_adder_is_a_no_op`）—— 加列助手
   哑火时 `apply_columns()` 必须抛，不许静默算过。这是把上面那个静默失效
   变成一条会响的断言。
3. **真路径**（两条 PG 门控）—— `ensure_ticket_schema()` / `ensure_t116_schema()`
   在真 PG 上加得上、连跑是 no-op。没库就 skip，绝不红。
"""
from __future__ import annotations

import functools
import os
from pathlib import Path

import pytest

from maos.core.store import SqliteStore
from maos.domain import _dbport, _schema_util
from maos.domain.refund import case_pack, objects
from maos.skills.builtin.refund import compensate
from maos.store.pg_store import DSN_ENV, PgStorePort

#: 两个片段加载器 —— 去重之后它们共用 `_schema_util.apply_columns()`。
_LOADERS = (
    Path(case_pack.__file__),
    Path(compensate.__file__),
)


@pytest.fixture()
def store() -> SqliteStore:
    st = SqliteStore(":memory:")
    st.init_schema()
    objects.ensure_schema(st)
    return st


# --------------------------------------------------------------- 1. 源码级守卫
def test_pragma_is_gone_from_both_loaders() -> None:
    """两个片段加载器里不许再出现 `PRAGMA`。

    去重之前两处各有一句 `PRAGMA table_info(...)`，那是本轨要修的 bug 的**根因**：
    SQLite 方言写进了一条两个后端都要走的路。探针该按后端分支的地方只有一处
    （`_dbport.add_column_if_missing()`），加载器这一层必须是后端无关的。

    钉源码而不是钉行为，是因为行为那条要有 PG 才跑得到（没库就 skip），
    而这条在任何机器上都红得出来。
    """
    for path in _LOADERS:
        text = path.read_text(encoding="utf-8")
        code = "\n".join(
            line for line in text.splitlines()
            if not line.lstrip().startswith("#")
        )
        # docstring 里允许提它（讲的正是「为什么不能再用它」），代码里不许。
        offenders = [
            line.strip() for line in code.splitlines()
            if "PRAGMA" in line and "table_info" in line
            and not line.lstrip().startswith(("*", "·", '"', "'"))
            and "`PRAGMA" not in line
        ]
        assert not offenders, (
            f"{path.name} 里还留着 PRAGMA 探针：{offenders}。"
            " 方言探针只许在 `_dbport.add_column_if_missing()` 里按后端分支。")


def test_both_loaders_share_one_helper() -> None:
    """两处的私有助手都没了，加列走同一个 `_schema_util.apply_columns()`。"""
    assert not hasattr(case_pack, "_add_column_if_missing"), \
        "case_pack 还留着私有加列助手 —— 去重没做干净"
    assert not hasattr(compensate, "_add_column_if_missing"), \
        "compensate 还留着私有加列助手 —— 去重没做干净"
    for path in _LOADERS:
        assert "_schema_util" in path.read_text(encoding="utf-8"), \
            f"{path.name} 没有 import 共享助手"


# --------------------------------------------------------------- 2. 语义级守卫
def test_apply_columns_reports_only_newly_added(store: SqliteStore) -> None:
    """返回值是**本次真加的**那几列，不是片段里声明的全部。连跑第二次是空。"""
    conn = _schema_util.open_conn(store)
    conn.execute("CREATE TABLE t133_cols (a TEXT)")
    wanted = (("t133_cols", "b", "TEXT NOT NULL DEFAULT ''"),
              ("t133_cols", "c", "INTEGER NOT NULL DEFAULT 1"))

    first = _schema_util.apply_columns(conn, wanted)
    second = _schema_util.apply_columns(conn, wanted)

    assert first == ("t133_cols.b", "t133_cols.c")
    assert second == (), "第二次还报加了列 —— 幂等破了"


def test_apply_columns_raises_when_the_adder_is_a_no_op(
        store: SqliteStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """加列助手哑火时必须抛，**不许静默算过**。

    这条模拟的正是 T133 之前 `compensate.ensure_ticket_schema()` 在 PG 上的处境：
    把带回落的助手装上去，它探不动就 `return`，于是「加列」整体成了 no-op 而
    没有任何人报错。静默算过的代价是症状离原因很远 —— 下游 INSERT 报
    no such column / UndefinedColumn，那句话完全不提加列这件事。
    """
    conn = _schema_util.open_conn(store)
    conn.execute("CREATE TABLE t133_silent (a TEXT)")
    monkeypatch.setattr(_schema_util, "add_column_if_missing",
                        lambda *_a, **_k: None)

    with pytest.raises(_schema_util.SchemaFragmentError, match="片段加列没加上"):
        _schema_util.apply_columns(
            conn, (("t133_silent", "b", "TEXT NOT NULL DEFAULT ''"),))


def test_ensure_ticket_schema_is_idempotent_on_sqlite(store: SqliteStore) -> None:
    """SQLite 侧行为不变：连跑两次，六列都在，一次都没撞 duplicate column name。"""
    compensate.ensure_ticket_schema(store)
    compensate.ensure_ticket_schema(store)

    for table, col, _decl in compensate.ticket_columns():
        assert objects._has_column(store, table, col), f"{table}.{col} 没加上"


# --------------------------------------------------------------- 3. PG 门控守卫
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


requires_pg = pytest.mark.skipif(
    _live_dsn() is None,
    reason=f"没有可连的 PG：{DSN_ENV} 未设或连不上。"
           " 起库见 test_refund_domain_pg.py 的 docstring。")


@pytest.fixture()
def pg_store(monkeypatch: pytest.MonkeyPatch):
    """业务表落在 PG 的 store。口径同 `test_refund_domain_pg.py::pg_store`。

    **不清表**：本束只关心 schema 形状，一行数据都不写，清表反而会把别的束
    刚灌的东西抹掉。
    """
    monkeypatch.setenv(DSN_ENV, _live_dsn() or "")
    monkeypatch.setenv(_dbport.BACKEND_ENV, _dbport.POSTGRES)
    _dbport.close_pg()

    st = SqliteStore(":memory:")
    st.init_schema()
    objects.ensure_schema(st)
    yield st
    _dbport.close_pg()


@requires_pg
def test_ensure_ticket_schema_runs_on_postgres(pg_store) -> None:
    """**本轨的回归守卫**：`ensure_ticket_schema()` 在真 PG 上跑得过，六列齐。

    守的是「PRAGMA 被写回去」：`PRAGMA table_info(...)` 发到 PG 上就是一条语法错，
    **与那六列在不在无关**，所以这条不必先把列 DROP 掉就守得住 —— 去重之前它抛
    `SyntaxError: syntax error at or near "PRAGMA"`。

    （`ALTER TABLE ... DROP COLUMN` 不在 `_dbport` 翻译器认的 DDL 里，扩它属于
    「把翻译器改成通用 SQL 翻译器」，本轨不做，已记 BACKLOG。「列不在时真加得上」
    那一半由下面那条用自建表守。）
    """
    compensate.ensure_ticket_schema(pg_store)

    conn = _dbport.DomainConn.open(pg_store)
    for table, col, _decl in compensate.ticket_columns():
        assert _schema_util.has_column(conn, table, col), \
            f"{table}.{col} 在 PG 上没加上"


@requires_pg
def test_apply_columns_really_adds_on_postgres(pg_store) -> None:
    """**「静默不加列」的守卫**：列确实不在时，PG 上真的加得上。

    上面那条在「列早就在了」的持久库上也绿，所以守不住「探不动就 return」那个
    变体（它同样不抛、六列同样齐）。这条自己建一张表，起点保证是空的：
    去重成带回落的助手，`added` 会是空元组而两列一列都没有 —— 当场红。
    """
    conn = _dbport.DomainConn.open(pg_store)
    conn.execute("DROP TABLE IF EXISTS t133_pg_addcol")
    conn.execute("CREATE TABLE t133_pg_addcol (a TEXT)")
    wanted = (("t133_pg_addcol", "b", "TEXT NOT NULL DEFAULT ''"),
              ("t133_pg_addcol", "c", "INTEGER NOT NULL DEFAULT 1"))
    try:
        added = _schema_util.apply_columns(conn, wanted)

        assert added == ("t133_pg_addcol.b", "t133_pg_addcol.c")
        for table, col, _decl in wanted:
            assert _schema_util.has_column(conn, table, col), \
                f"{table}.{col} 在 PG 上没加上 —— 助手的探针在这个后端上哑了"
        assert _schema_util.apply_columns(conn, wanted) == (), "PG 上幂等破了"
    finally:
        conn.execute("DROP TABLE IF EXISTS t133_pg_addcol")


@requires_pg
def test_ensure_ticket_schema_is_idempotent_on_postgres(pg_store) -> None:
    """连跑两次不抛、不重复加列。"""
    compensate.ensure_ticket_schema(pg_store)
    compensate.ensure_ticket_schema(pg_store)

    conn = _dbport.DomainConn.open(pg_store)
    for table, col, _decl in compensate.ticket_columns():
        rows = conn.query(
            "SELECT count(*) AS n FROM information_schema.columns"
            " WHERE table_schema = current_schema() AND table_name = ?"
            " AND column_name = ?", (table, col))
        assert rows[0]["n"] == 1, f"{table}.{col} 在 PG 上有 {rows[0]['n']} 份"


@requires_pg
def test_ensure_t116_schema_is_idempotent_on_postgres(pg_store) -> None:
    """T116 那条路径在 PG 上也是幂等的（它原先靠调用方的 SELECT 探针补位，
    去重之后靠 `_dbport` 那套后端感知探针，两次都不该再报「加了列」）。"""
    case_pack.ensure_t116_schema(pg_store)
    assert case_pack.ensure_t116_schema(pg_store) == ()
