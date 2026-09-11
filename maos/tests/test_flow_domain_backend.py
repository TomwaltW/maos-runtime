"""T126 的机器验收 —— **装配级**业务域后端开关，以及它守住的那条隔离。

分两组：

* 前半组不需要库：`build()` 的缺省行为、旗标的取值域、`_dbport` 那条
  「先看 store 标记再读环境变量」的优先级、`sqlite_master` 的方言翻译。
* 后半组要一个能连的 PG（`MAOS_PG_DSN`），没库整组 skip、绝不红 ——
  fixture 照 `maos/tests/test_refund_domain_pg.py` 的形状写（连 `_live_dsn()` 的
  `lru_cache` 与「DSN 自己 setenv 回来」那两处时序坑都一并照抄，理由见那个文件）。

## 这一束在守什么

评委第 2.4 条要的是「业务纵切**跑在** PolarDB 上」。让它跑起来只差一个开关，
难的是**开关的粒度**：

`MAOS_DOMAIN_BACKEND` 是进程级的，一设就把本进程每一条业务域连接都拨到 PG ——
包括 `maos/roundtable/stages.py::facts_finance_preview` 那个按设计就该用完即弃的
`_memory_store()`。实测后果是预演写的 `refund_case`（`plan_id='preview'`）落进真库，
紧接着真跑的 `refund.intake` 撞上 `guard.create_case` 的受理幂等闸，三次重试全败，
整条 DAG 停在第一步 —— 而报错指向受理幂等，**不指向后端开关**。

所以 T126 加的是**装配级**开关：经 `build()` 装出来的 store 带标记，不经装配的
一次性库没有。`test_preview_store_stays_on_sqlite` 是这条隔离的机器判据 ——
它红了就说明「一次性副本」的语义又被拨没了，而那件事在业务证据上是无声的。
"""

from __future__ import annotations

import functools
import os

import pytest

from maos.core.store import SqliteStore
from maos.domain import _dbport
from maos.flows import common
from maos.store.pg_store import DSN_ENV, PgBackendUnavailable, PgStorePort

ENV = common.FLOW_DOMAIN_BACKEND_ENV


@pytest.fixture(autouse=True)
def _no_ambient_flow_backend(monkeypatch):
    """本组每条用例自己决定旗标的值，不吃机器上 ambient 的那份。

    与 `conftest._no_ambient_store_env` 同一条道理：漏网时症状是「某条用例在我机器上
    绿、在别人机器上红」，而红的地方指向被测代码，不指向环境。
    """
    monkeypatch.delenv(ENV, raising=False)
    monkeypatch.delenv(_dbport.BACKEND_ENV, raising=False)


# ============================================================ 不需要库的那一半
def test_build_without_flag_marks_nothing():
    """判据①：不给旗标时 `build()` 的行为与改前逐字节相同。

    「相同」落到可断言的形状上就是这三条：六元组的**位序与类型**不动（C-4）、
    store 上**不留**任何后端标记、业务域仍解析成 sqlite。
    """
    six = common.build({"用户请求": "{}"})
    assert len(six) == 6
    store, bus, cp, model, worker, gate = six
    assert isinstance(store, SqliteStore)
    assert not hasattr(store, _dbport.STORE_BACKEND_ATTR)
    assert _dbport.backend_of(store) == _dbport.SQLITE
    assert _dbport.DomainConn.open(store).backend == _dbport.SQLITE
    # 其余五件仍在原位、类型不变（这一条钉的是 C-4，不是本轨的新行为）。
    assert (bus, cp, model, worker, gate) == six[1:]


def test_build_with_flag_marks_store(monkeypatch):
    """给了旗标，`build()` 装出来的 store 带标记，且六元组形状**一个字节不变**。"""
    monkeypatch.setenv(ENV, "postgres")
    six = common.build({"用户请求": "{}"})
    assert len(six) == 6
    store = six[0]
    assert getattr(store, _dbport.STORE_BACKEND_ATTR) == _dbport.POSTGRES
    assert _dbport.backend_of(store) == _dbport.POSTGRES


def test_build_flag_rejects_typo(monkeypatch):
    """旗标拼错一个字母**当场抛**，不回落 sqlite。

    口径同 `_dbport.backend_name()` 与 `maos/store/__init__.py::create_store`：
    回落的话你会以为在验 PG，其实一行 PG 代码都没执行 —— 而这件事要到真接
    PolarDB 那天才暴露。
    """
    monkeypatch.setenv(ENV, "postgre")
    with pytest.raises(ValueError, match="postgre"):
        common.build({"用户请求": "{}"})


def test_store_mark_beats_env(monkeypatch):
    """判据③的前半：`backend_of` 先看 store 标记，没标记才读环境变量。"""
    store = SqliteStore(":memory:")
    store.init_schema()

    monkeypatch.setenv(_dbport.BACKEND_ENV, "postgres")
    assert _dbport.backend_of(store) == _dbport.POSTGRES        # 没标记 -> 读环境变量

    _dbport.mark_store_backend(store, _dbport.SQLITE)
    assert _dbport.backend_of(store) == _dbport.SQLITE          # 有标记 -> 标记说了算

    _dbport.mark_store_backend(store, None)                     # 摘掉标记
    assert _dbport.backend_of(store) == _dbport.POSTGRES


def test_mark_store_backend_rejects_unknown():
    store = SqliteStore(":memory:")
    with pytest.raises(ValueError, match="mysql"):
        _dbport.mark_store_backend(store, "mysql")


def test_preview_store_stays_on_sqlite(monkeypatch):
    """**本轨最要紧的一条**：圆桌核算预演那个一次性库不许被旗标拨走。

    `facts_finance_preview` 用 `_memory_store()` 现造一个内存库，跑完即弃 ——
    它不经 `build()`，所以拿不到装配级标记，业务域必须仍是 sqlite。

    这条红了 = 预演又会往真 PG 库里写 `refund_case`，而症状会出现在**受理幂等闸**
    上（「库里 'preview'、这次 'plan_xxx'」），没有任何东西会提示你去看后端开关。
    """
    from maos.roundtable import stages

    monkeypatch.setenv(ENV, "postgres")
    monkeypatch.setenv(DSN_ENV, "postgresql://nobody@127.0.0.1:1/none")

    preview = stages._memory_store()
    assert not hasattr(preview, _dbport.STORE_BACKEND_ATTR)
    assert _dbport.backend_of(preview) == _dbport.SQLITE
    # 真去开一条连接：拨错了的话这一句会去连 DSN（然后抛），而不是拿到内存库。
    assert _dbport.DomainConn.open(preview).backend == _dbport.SQLITE


def test_postgres_flag_never_falls_back_to_sqlite(monkeypatch):
    """判据③：`MAOS_PG_DSN` 连不上时**明确报错**，绝不静默回落 sqlite。

    `docs/architecture.md` 把「绝不静默回落」写成了结论，这条是它的机器判据。
    回落一次的后果不是一个错误的结果，是一份**看起来验过 PG 的证据**。
    """
    store = SqliteStore(":memory:")
    store.init_schema()
    _dbport.mark_store_backend(store, _dbport.POSTGRES)
    monkeypatch.setenv(DSN_ENV, "postgresql://nobody:nobody@127.0.0.1:1/none")
    _dbport.close_pg()
    with pytest.raises(PgBackendUnavailable):
        _dbport.DomainConn.open(store)


def test_postgres_flag_without_dsn_raises(monkeypatch):
    """DSN 没配同样抛，且报错里点名它只从环境变量读（铁律 6）。"""
    store = SqliteStore(":memory:")
    store.init_schema()
    _dbport.mark_store_backend(store, _dbport.POSTGRES)
    monkeypatch.delenv(DSN_ENV, raising=False)
    _dbport.close_pg()
    with pytest.raises(PgBackendUnavailable, match=DSN_ENV):
        _dbport.DomainConn.open(store)


# ------------------------------------------------------- sqlite_master 的翻译
def test_sqlite_master_rewritten_for_pg():
    """`outcome.has_outcome_table()` 那句探针在 PG 上要问得动目录。

    它是**建表路径的探针**：问不动的后果不是报错（那还好查），是 PG 上恒答
    「没有 case_outcome」，于是四判据整段落空而流程照常绿。
    """
    sql = "SELECT name FROM sqlite_master WHERE type='table' AND name='case_outcome'"
    out = _dbport.rewrite_sqlite_master(sql)
    assert "sqlite_master" in out                      # 仍以它为别名，WHERE 才接得上
    assert "information_schema.tables" in out
    assert "pg_indexes" in out                         # type='index' 的调用方也要能问
    assert "WHERE type='table' AND name='case_outcome'" in out


def test_sqlite_master_inside_string_literal_untouched():
    """字符串字面量里的同形字**逐字保住** —— 与本模块别处同一条「按引号状态扫」的口径。"""
    sql = "SELECT 1 FROM t WHERE note LIKE '%from sqlite_master%'"
    assert _dbport.rewrite_sqlite_master(sql) == sql


def test_sqlite_master_untouched_without_reference():
    sql = "SELECT * FROM refund_case WHERE tenant_id=?"
    assert _dbport.rewrite_sqlite_master(sql) == sql


# ================================================================ 要库的那一半
@functools.lru_cache(maxsize=1)
def _live_dsn() -> str | None:
    """探一次：DSN 配了吗、连得上吗。连不上就是没库，不是失败。

    **必须在 collection 期求值**（模块级 `pytestmark` 那一句就是），否则
    `conftest._no_ambient_store_env` 的 autouse delenv 会先把 DSN 删掉，
    整组变成「没库」而不是红灯 —— 症状是条数悄悄变少，没有人会去查。
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


live_only = pytest.mark.skipif(
    _live_dsn() is None,
    reason=f"没有可连的 PG：{DSN_ENV} 未设或连不上。起库见 test_refund_domain_pg.py")

TENANT = "tnt-t126"
CASE = "case-t126-0001"


@pytest.fixture()
def pg_flow_store(monkeypatch):
    """经 `build()` 装出来、业务表落 PG、控制面仍在 SQLite 的一套。

    DSN 要自己 setenv 回来：`conftest._no_ambient_store_env` 每条用例开跑前会删掉
    `MAOS_PG_DSN`（不删的话一次裸 pytest 就往真库写表）。autouse fixture 先于同
    scope 的普通 fixture 实例化，这里的 setenv 一定盖得住它的 delenv —— 与
    `test_refund_domain_pg.py::pg_store` 同一处时序，理由在那里有完整一段。
    """
    monkeypatch.setenv(DSN_ENV, _live_dsn() or "")
    monkeypatch.setenv(ENV, "postgres")
    _dbport.close_pg()                                 # 别沿用别的用例留下的连接

    store, _bus, _cp, _model, _worker, _gate = common.build({"用户请求": "{}"})
    from maos.domain.refund import objects

    objects.ensure_schema(store)
    _wipe(store)
    yield store
    _wipe(store)
    _dbport.close_pg()


def _wipe(store) -> None:
    """清掉本组自己那个租户的残留行。

    走底层连接而不是 `objects.execute`：后者对 `refund_case` 的写入一律拒绝
    （`_guarded` / 铁律 8：不许旁路 guard）—— 那道闸是对的，清场是 fixture 的事，
    形状同 `test_refund_domain_pg.py::pg_store`。
    """
    conn = _dbport.DomainConn.open(store)
    with conn.lock:
        for table in ("refund_case", "order_snapshot"):
            conn.raw.execute(f"DELETE FROM {table} WHERE tenant_id=?", (TENANT,))
        conn.raw.commit()


@live_only
def test_build_store_writes_business_tables_to_pg(pg_flow_store):
    """判据②：给了旗标且 DSN 可连时，业务表的写入**真的落在 PG**。

    验的时候另开一条连接去 PG 里查（而不是回头问同一个 store），并**同时**确认
    这一行没有出现在控制面那份 SQLite 里 —— 只查到「PG 里有」不足以排除「两边都写了」。
    """
    from maos.domain.refund import guard, objects

    assert _dbport.DomainConn.open(pg_flow_store).backend == _dbport.POSTGRES

    objects.execute(
        pg_flow_store,
        "INSERT OR REPLACE INTO order_snapshot (tenant_id, order_id, version, sku,"
        " amount_paid, paid_at, channel_id, policy_version_at_order, payload_json, read_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (TENANT, "ord-t126", 1, "sku-t126", 100.0, "2026-01-01T00:00:00+00:00",
         "ch-tmall", 1, "{}", "2026-01-02T00:00:00+00:00"))
    guard.create_case(
        pg_flow_store, tenant_id=TENANT, case_id=CASE, channel_id="ch-tmall",
        order_id="ord-t126", order_version=1, sku="sku-t126", reason_code="damaged",
        amount_claimed=100.0, plan_id="plan-t126", actor_skill="refund.intake",
        invocation_id="inv-t126")

    # PG 侧：另起一条连接去问，问到的必须是刚写的那一行。
    probe = SqliteStore(":memory:")
    probe.init_schema()
    _dbport.mark_store_backend(probe, _dbport.POSTGRES)
    rows = objects.query(
        probe, "SELECT case_id, biz_status FROM refund_case WHERE tenant_id=? AND case_id=?",
        (TENANT, CASE))
    assert [r["case_id"] for r in rows] == [CASE]

    # SQLite 侧：控制面那份库里**不该**有这张表的这一行。
    local = pg_flow_store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='refund_case'").fetchall()
    assert not local, "业务表落进了控制面的 SQLite —— 双写，口径与 architecture.md §5 不符"


@live_only
def test_control_plane_stays_on_sqlite(pg_flow_store):
    """控制面那四张表仍在本地 SQLite（`phase-10.md` §8 明确不做控制面上 PG）。"""
    names = {r[0] for r in pg_flow_store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"plan", "task", "artifact", "event_log"} <= names


@live_only
def test_has_outcome_table_probe_works_on_pg(pg_flow_store):
    """`outcome.has_outcome_table()` 在 PG 上问得动 —— `sqlite_master` 翻译的端到端判据。

    建表之前答 False、建表之后答 True。翻译没接上的话它会抛 `UndefinedTable`；
    翻译接上但问错目录的话它会**恒答 False**，那种才是无声的。
    """
    from maos.domain.refund import outcome

    assert outcome.has_outcome_table(pg_flow_store) in (True, False)   # 至少不抛
    outcome.ensure_outcome_schema(pg_flow_store)
    assert outcome.has_outcome_table(pg_flow_store) is True
