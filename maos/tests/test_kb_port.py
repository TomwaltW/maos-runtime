"""T24 的机器验收 —— `kb.port_of()` 的判据：**`_conn` 可不可调用**，不是有没有。

## 这一束在守什么

老判据是「暴露了 `_conn` 的对象一律走老路径」，本意是认出核心 `SqliteStore`。
`PgStorePort` 恰好也有一个叫 `_conn` 的属性（存连接），于是：

* **没连库时判得对** —— `_conn` 是 None，`port_of()` 认出端口；
* **连上库那一刻判反** —— `_conn` 变成 psycopg 连接，`port_of()` 改口说「这是核心
  store」，`ensure_schema()` 拿这条连接去调 sqlite 专有的 `executescript()`，
  当场 `AttributeError: 'Connection' object has no attribute 'executescript'`。

「先对后错」是这类 bug 最难查的形态：本地起不起库都不复现，只有真连上 PolarDB
才炸，而且炸在建表这一步，上面所有「RAG 跑在 PG 上」的验收一条都到不了。

## 三条钉子，缺一条都不算修好

1. **核心 `SqliteStore` 仍走老路径**（第 1 节）—— 这条是防回归的关键。只测新形状
   等于没测：把判据写成「一律认端口」，新形状那两条照样绿，而缺省链路全断。
2. **连上库的 PG 端口形状被认作端口**（第 2 节）。
3. **`ensure_schema()` 吃到那种对象不再抛 `AttributeError`**（第 3 节）。

## 为什么不用真库

判据是纯形状判断，真库一个字节都不多告诉我们，却要 DSN、要网络、要跳过逻辑。
真库路径归 `test_pg_store_live.py`。这里用**形状照抄 PgStorePort**的替身：
`_conn` 是存连接的属性（不可调用），同时实现 F-2 的 `execute` / `query`。
替身的连接**故意不实现 `executescript`** —— 判据一旦判反，第 3 节抛的就是线上那条
一模一样的 `AttributeError`，而不是一句「断言失败」。

第 4 节把判据脚下的两条驱动事实也钉住：换 Python 版本或换驱动时，判据是否还分得开
由那两条决定，让它们红在这里，好过在真连上 PG 的那一刻才发现。
"""

from __future__ import annotations

import sqlite3

import pytest

from maos import kb
from maos.core.store import SqliteStore


class _NonCallableConn:
    """替身连接：能跑 SQL，但**不可调用**，且**没有 `executescript`**。

    psycopg 的连接就是这个形状（整条 MRO 没有 `__call__`，也没有 sqlite 专有的
    `executescript`）。底下借 sqlite 驱动真跑，是为了让第 3 节的建表**真的建成**，
    而不是被一个空实现糊过去。
    """

    def __init__(self, raw: sqlite3.Connection) -> None:
        self._raw = raw

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        return self._raw.execute(sql, params)

    def commit(self) -> None:
        self._raw.commit()


class _ConnectedPgShape:
    """`PgStorePort` **连上库之后**的形状 —— 本轨要修的就是这个形状被判反。

    照抄的是 `maos/store/pg_store.py` 的两个事实：`_conn` 是构造时置 None、
    `connect()` 后置连接对象的**实例属性**；`execute` / `query` 是 F-2 的两个方法。
    """

    def __init__(self, *, connected: bool = True) -> None:
        self._raw = sqlite3.connect(":memory:")
        self._raw.row_factory = sqlite3.Row
        #: 未连接是 None、连上是连接对象 —— 两种形态都不可调用，判据要都认得。
        self._conn = _NonCallableConn(self._raw) if connected else None

    def execute(self, sql: str, params: tuple = ()) -> None:
        self._raw.execute(sql, tuple(params))
        self._raw.commit()

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        return [dict(r) for r in self._raw.execute(sql, tuple(params)).fetchall()]


# ---------------------------------------------------------------------------
# 1. 缺省链路不许改道 —— 这条是防回归的关键，别只测新形状
# ---------------------------------------------------------------------------
def test_core_sqlite_store_still_takes_the_old_path() -> None:
    """核心 `SqliteStore` 必须仍返回 None，走 `_conn` 那条老路径。

    判据换成「可不可调用」之后，这条成立有**两个各自都够**的理由，测试把两个都断言，
    好让将来只塌掉一个时能立刻看出塌的是哪个：

    · `SqliteStore._conn` 存的是 `sqlite3.Connection`，该类自带 `__call__`；
    · 就算哪天 sqlite 把 `__call__` 摘了，`SqliteStore` 也没有 `execute` / `query`。
    """
    store = SqliteStore(":memory:")

    assert kb.port_of(store) is None, \
        "缺省链路改道了 —— 核心 SqliteStore 必须走 _conn 那条老路径"

    # 两道防线各自的前提，逐条钉住。
    assert callable(store._conn), "第一道防线塌了：SqliteStore._conn 不再可调用"
    assert not hasattr(store, "execute") and not hasattr(store, "query"), \
        "第二道防线塌了：核心 store 长出了 execute / query，判据只剩一道防线"


def test_none_is_not_a_store() -> None:
    """`None` 既不是 store 也不是端口 —— 判据换掉时这条最容易被顺手丢掉。"""
    assert kb.port_of(None) is None


# ---------------------------------------------------------------------------
# 2. 连上库的 PG 端口形状 —— 修之前这条返回 None（bug 本体）
# ---------------------------------------------------------------------------
def test_connected_pg_shaped_port_is_recognised_as_a_port() -> None:
    """`_conn` 是**不可调用的连接对象**时，仍必须认作 StorePort。

    修之前这里返回 None：老判据只问「`_conn` 是不是非 None」，而 `PgStorePort`
    连上库之后它恰好非 None。
    """
    port = _ConnectedPgShape(connected=True)

    assert port._conn is not None, "夹具没连上 —— 这条测的就不是「连上之后」那个形态"
    assert not callable(port._conn), "夹具的连接可调用了 —— 那就不是 psycopg 的形状"
    assert kb.port_of(port) is port, \
        "连上库的 PG 端口被判成了核心 store —— 判据退回「有没有 _conn」了"


def test_idle_pg_shaped_port_is_still_recognised() -> None:
    """未连接（`_conn is None`）这一侧本来就是对的，钉住它别被顺手改坏。"""
    port = _ConnectedPgShape(connected=False)

    assert port._conn is None
    assert kb.port_of(port) is port


# ---------------------------------------------------------------------------
# 3. 症状本体 —— 建表不再撞 executescript
# ---------------------------------------------------------------------------
def test_ensure_schema_no_longer_explodes_on_a_connected_port() -> None:
    """`ensure_schema()` 吃连上库的端口形状：不抛 `AttributeError`，且表真的建成。

    只断言「不抛」不够 —— 判据要是改成「一律认端口」，不抛也能满足，但缺省链路已断
    （那条由第 1 节守）。这里再多问一句「表在不在」，把「走通了」和「悄悄没做事」
    分开：端口路径是逐条 `execute` 打进去的，表没建成就说明那条路径根本没跑。
    """
    port = _ConnectedPgShape(connected=True)

    kb.ensure_schema(port)  # 修之前：AttributeError: 'NoneType'/连接对象没有 executescript

    assert kb.has_kb_table(port), "端口路径跑完了却没建出 kb_doc —— 建表语句没真打进去"


def test_ensure_schema_writes_and_reads_back_through_the_port() -> None:
    """再往前走一步：建表 → 写 → 读，整条链路都在端口上通。

    §5.1 修的是判据，但判据修对了不等于整层通 —— `port_of()` 之后每个调用点都还有
    自己的老路径分支。这条把 `kb` 层最主要的读写口在端口形状上跑一遍。
    """
    port = _ConnectedPgShape(connected=True)
    kb.ensure_schema(port)

    kb.upsert_doc(port, {"tenant_id": "tnt-t24", "doc_id": "doc-1",
                         "kind": kb.KIND_POLICY, "title": "退款政策",
                         "body": "超时未到账全额退款"})

    got = kb.get_doc(port, "tnt-t24", "doc-1")
    assert got is not None and got["title"] == "退款政策"
    assert {d["doc_id"] for d in kb.list_docs(port, tenant_id="tnt-t24")} == {"doc-1"}


# ---------------------------------------------------------------------------
# 4. 判据脚下的驱动事实 —— 换 Python / 换驱动时让它红在这里
# ---------------------------------------------------------------------------
def _defines_call(cls: type) -> bool:
    """整条 MRO 上有没有**自己定义**的 `__call__`。

    不能写 `hasattr(cls, "__call__")` —— 类对象本身永远可调用（`type.__call__`），
    那样问 `psycopg.Connection` 会得到 True，是个假信号。
    """
    return any("__call__" in klass.__dict__ for klass in cls.__mro__)


def test_sqlite_connection_is_callable_and_pg_connection_is_not() -> None:
    """判据分得开这件事，压在这两条驱动事实上，逐条钉住。

    哪天这里红了，不要改这条测试 —— 它红就说明 `port_of()` 的判据已经分不开两种
    连接了，该去换判据（比如改判 `isinstance(_conn, sqlite3.Connection)`）。
    """
    assert _defines_call(sqlite3.Connection), \
        "sqlite3.Connection 不再自带 __call__ —— port_of() 的第一道防线没了"

    psycopg = pytest.importorskip("psycopg", reason="没装驱动时这条无从验证")
    assert not _defines_call(psycopg.Connection), \
        "psycopg.Connection 长出了 __call__ —— 连上库的 PG 端口又会被判成核心 store"
