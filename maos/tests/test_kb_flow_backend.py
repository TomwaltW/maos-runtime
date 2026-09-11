"""T132 的机器验收 —— **装配级**知识层后端开关，以及它接上的那两条真通道。

分两组，与 T126 的 `test_flow_domain_backend.py` 同构：

* 前半组不需要库：`build()` 的缺省行为、旗标取值域、`port_of()` 那条「先看装配级
  端口再按形状判」的优先级、两份通道口径的一致性。
* 后半组要一个能连的 PG（`MAOS_PG_DSN`），没库整组 skip、绝不红 —— fixture 的形状
  照 `test_kb_pg_prefilter.py` 抄（`lru_cache` 的探活、DSN 自己 setenv 回来这两处
  时序坑一并照抄，理由见那个文件）。

## 这一束在守什么

`docs/architecture.md` §5 那一行原本写着「知识层可切 PolarDB PG，`MAOS_DOMAIN_BACKEND`
+ `MAOS_PG_DSN`，经 `maos/domain/_dbport.py`」——**三处都与实况不符**：知识层不走
`_dbport`（走 `kb.port_of()` + `PgStorePort`），不由 `MAOS_DOMAIN_BACKEND` 控制，而它
自己那个进程级开关 `MAOS_STORE_BACKEND` **没有任何装配读**。于是 T132 之前 PG 那条路
整条填实了却一次也没经装配跑过：`test_kb_pg_prefilter.py` 那 16 条是**直接拿
`PgStorePort` 当 store 测的**，整条 DAG 的知识层始终落在本地 SQLite 上。

所以本束的核心判据是 `test_flag_puts_kb_doc_on_postgres`：**经 `build()` 装配**之后
`kb_doc` 落在 PG、而本地 SQLite 上连这张表都不存在。它红了就说明「知识层上了 PG」
又退回成一句只在测试里成立的话。
"""

from __future__ import annotations

import functools
import os

import pytest

import maos.kb as kb
from maos.core.store import SqliteStore
from maos.domain import _dbport
from maos.flows import common
from maos.kb import retriever
from maos.store import POSTGRES, SQLITE
from maos.store.pg_store import DSN_ENV, PgBackendUnavailable, PgStorePort

ENV = common.FLOW_KB_BACKEND_ENV


@pytest.fixture(autouse=True)
def _no_ambient_kb_backend(monkeypatch):
    """本组每条用例自己决定旗标的值，不吃机器上 ambient 的那份。

    与 `conftest._no_ambient_store_env` 同一条道理：漏网时症状是「某条用例在我机器上
    绿、在别人机器上红」，而红的地方指向被测代码，不指向环境。
    """
    monkeypatch.delenv(ENV, raising=False)


# ============================================================ 不需要库的那一半
def test_build_without_flag_attaches_nothing():
    """判据①：不给旗标时 `build()` 的行为与改前逐字节相同。

    「相同」落到可断言的形状上是这四条：六元组的位序与类型不动（C-4）、store 上
    **不留**任何端口、`port_of()` 仍返回 None、两条检索通道在 store 上探不到 ——
    最后一条尤其要紧：贴上去的方法会改变 `retriever._port_search` 的能力探测结果。
    """
    six = common.build({"用户请求": "{}"})
    assert len(six) == 6
    store, bus, cp, model, worker, gate = six
    assert isinstance(store, SqliteStore)
    assert kb.attached_port(store) is None
    assert not hasattr(store, kb.KB_PORT_ATTR)
    assert kb.port_of(store) is None
    assert kb.dialect_of(store) == "sqlite"
    for name in kb.PORT_CAPABILITIES:
        assert not hasattr(store, name), f"缺省装配不该在 store 上贴 {name}"
    assert (bus, cp, model, worker, gate) == six[1:]


def test_build_flag_rejects_typo(monkeypatch):
    """旗标拼错一个字母**当场抛**，不回落 sqlite。

    口径同 `_mark_flow_domain_backend()` 与 `create_store()`：回落的话你会以为在验
    PG，其实一行 PG 代码都没执行 —— 而这件事要到真接 PolarDB 那天才暴露。
    """
    monkeypatch.setenv(ENV, "postgre")
    with pytest.raises(ValueError, match="postgre"):
        common.build({"用户请求": "{}"})


def test_attach_backend_rejects_unknown():
    store = SqliteStore(":memory:")
    with pytest.raises(ValueError, match="mysql"):
        kb.attach_backend(store, "mysql")


def test_attach_backend_sqlite_is_a_noop():
    """`sqlite` 与空串都是「不接」，且不该在 store 上留下任何痕迹。"""
    store = SqliteStore(":memory:")
    for value in (SQLITE, "", None, "  SQLite  "):
        assert kb.attach_backend(store, value) is None
        assert kb.attached_port(store) is None
        assert kb.port_of(store) is None


def test_attached_port_beats_shape_probe():
    """判据②：`port_of()` 先看装配级端口，没接过才按形状判。

    核心 `SqliteStore` 的 `_conn` 可调用，按形状必判 None —— 装配级那条判据要是排在
    形状之后，就永远轮不到。这条钉的正是那个顺序。
    """
    store = SqliteStore(":memory:")
    store.init_schema()
    assert kb.port_of(store) is None                     # 按形状：核心 store

    fake = PgStorePort("postgresql://nobody@127.0.0.1:1/none")   # 不连库的壳
    kb.attach_port(store, fake)
    assert kb.port_of(store) is fake                     # 接过端口：端口说了算
    assert kb.dialect_of(store) == "postgres"
    for name in kb.PORT_CAPABILITIES:
        assert getattr(store, name).__self__ is fake, f"{name} 该是端口的 bound method"

    kb.attach_port(store, None)                          # 摘掉，store 回原样
    assert kb.attached_port(store) is None
    assert kb.port_of(store) is None
    assert kb.dialect_of(store) == "sqlite"
    for name in kb.PORT_CAPABILITIES:
        assert not hasattr(store, name)


def test_detach_does_not_break_a_real_port():
    """摘标记这件事对**本身就是端口**的 store 必须是无害的。

    `PgStorePort` 类上就有 `fts_search` / `vector_search`，`attach_port(port, None)`
    删的是实例属性 —— 删过头会把端口自己的方法摘掉，而那要到检索时才炸。
    """
    port = PgStorePort("postgresql://nobody@127.0.0.1:1/none")
    kb.attach_port(port, None)
    for name in kb.PORT_CAPABILITIES:
        assert callable(getattr(port, name))
    assert kb.port_of(port) is port                       # 形状判据仍然认得它


def test_port_capabilities_match_retriever_channels():
    """两份口径必须逐字一致 —— 这是「两边各写一份」唯一的守卫。

    `kb.PORT_CAPABILITIES` 决定接端口时往 store 上贴哪几个方法，
    `retriever.PORT_CHANNELS` 决定检索器去 store 上探哪几个。两边漂开的后果不是
    报错，是**某条通道静默退化成本地实现**：召回悄悄变少，日志一片正常。
    """
    assert tuple(kb.PORT_CAPABILITIES) == tuple(retriever.PORT_CHANNELS)


def test_process_level_store_backend_semantics_unchanged(monkeypatch):
    """判据③：`MAOS_STORE_BACKEND` 的**进程级**语义一个字没动。

    它从来只作用于显式调 `create_store()` 的调用方；装配不读它，设了也拨不动
    `build()` 装出来的那条 store。这条红了说明 T132 把一个进程级开关偷偷接进了
    装配 —— 那正是 T126 在业务域那半证过会出事的做法（一次性内存副本被一起拨走）。
    """
    from maos.store import BACKEND_ENV

    monkeypatch.setenv(BACKEND_ENV, POSTGRES)
    monkeypatch.setenv(DSN_ENV, "postgresql://nobody:nobody@127.0.0.1:1/none")
    store = common.build({"用户请求": "{}"})[0]
    assert kb.attached_port(store) is None, "装配不许读进程级的 MAOS_STORE_BACKEND"
    assert kb.port_of(store) is None
    assert kb.dialect_of(store) == "sqlite"


def test_preview_store_stays_on_sqlite(monkeypatch):
    """圆桌核算预演那个一次性库不许被旗标拨走 —— 与 T126 那条同形。

    `facts_finance_preview` 用 `_memory_store()` 现造一个内存库，跑完即弃；它不经
    `build()`，所以拿不到装配级端口，知识层必须仍落在 sqlite。
    """
    from maos.roundtable import stages

    monkeypatch.setenv(ENV, POSTGRES)
    monkeypatch.setenv(DSN_ENV, "postgresql://nobody@127.0.0.1:1/none")

    preview = stages._memory_store()
    assert kb.attached_port(preview) is None
    assert kb.port_of(preview) is None
    assert kb.dialect_of(preview) == "sqlite"


def test_postgres_flag_without_dsn_raises(monkeypatch):
    """判据④：要 PG 却没有可用的 DSN 时**明确报错**，绝不静默回落 sqlite。

    回落一次的后果不是一个错误的结果，是一份**看起来验过 PG 的证据**。
    """
    monkeypatch.setenv(ENV, POSTGRES)
    monkeypatch.delenv(DSN_ENV, raising=False)
    with pytest.raises(PgBackendUnavailable):
        common.build({"用户请求": "{}"})


def test_two_backends_are_dialled_independently(monkeypatch):
    """业务域与知识层各拨各的 —— 合成一个开关就表达不了的那个中间态。

    只给知识层旗标时，业务域那半必须仍是 sqlite（反过来同理，由 T126 那束守）。
    """
    monkeypatch.setenv(ENV, POSTGRES)
    monkeypatch.setenv(DSN_ENV, "postgresql://nobody@127.0.0.1:1/none")
    store = SqliteStore(":memory:")
    store.init_schema()
    kb.attach_port(store, PgStorePort("postgresql://nobody@127.0.0.1:1/none"))
    assert kb.dialect_of(store) == "postgres"            # 知识层在 PG
    assert _dbport.backend_of(store) == _dbport.SQLITE   # 业务域没跟着走


# ================================================================ 要库的那一半
@functools.lru_cache(maxsize=1)
def _live_dsn() -> str | None:
    """探一次：DSN 配了吗、连得上吗。连不上就是没库，不是失败。

    **必须在 collection 期求值**（模块级的 `live_only` 就是），否则
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

KB_TABLES = ("kb_doc_fts", "kb_doc", "kb_schema_version")
TENANT = "tnt-t132"


@pytest.fixture()
def flow_on_pg(monkeypatch):
    """经 `build()` 装出来的、知识层拨到 PG 的那条 store。用完把三张表删干净。

    DSN 与旗标都自己 setenv 回来（autouse 的 delenv 先跑过），理由同
    `test_kb_pg_prefilter.py::pg`。
    """
    monkeypatch.setenv(DSN_ENV, _live_dsn() or "")
    monkeypatch.setenv(ENV, POSTGRES)

    scratch = PgStorePort(sqlite_dialect=True)
    scratch.connect()
    for table in KB_TABLES:
        scratch.execute(f"DROP TABLE IF EXISTS {table}", ())

    store = common.build({"用户请求": "{}"})[0]
    yield store

    for table in KB_TABLES:
        scratch.execute(f"DROP TABLE IF EXISTS {table}", ())
    scratch.close()
    port = kb.attached_port(store)
    if port is not None:
        port.close()


def _seed(store, tenant: str = TENANT) -> None:
    """三条够用的语料：两条同租户、一条别家的（跨租户硬过滤要有对照）。"""
    kb.ensure_schema(store)
    rows = [
        {"tenant_id": tenant, "doc_id": "d-online", "biz_type": "refund",
         "channel_id": "ch-online", "kind": kb.KIND_POLICY,
         "title": "lesson online", "body": "lesson resolution online channel"},
        {"tenant_id": tenant, "doc_id": "d-offline", "biz_type": "refund",
         "channel_id": "ch-offline", "kind": kb.KIND_POLICY,
         "title": "lesson offline", "body": "lesson resolution offline channel"},
        {"tenant_id": "tnt-other", "doc_id": "d-online", "biz_type": "refund",
         "channel_id": "ch-online", "kind": kb.KIND_POLICY,
         "title": "somebody else", "body": "lesson resolution of another tenant"},
    ]
    for row in rows:
        kb.upsert_doc(store, {**row, "embedding": retriever.embed(row["body"])})


@live_only
def test_flag_puts_kb_doc_on_postgres(flow_on_pg):
    """**本束最要紧的一条**：经装配跑出来的知识层，落在 PG、不在本地 SQLite。

    两个方向都要断言。只断言「PG 上有」不够 —— 两边都写了一份同样看起来是对的，
    而那正是「同构」最容易滑进去的失效。
    """
    store = flow_on_pg
    port = kb.attached_port(store)
    assert isinstance(port, PgStorePort)
    assert port.sqlite_dialect is True, "知识层的 SQL 全是 SQLite 方言，不翻译一条都发不出去"
    assert kb.dialect_of(store) == "postgres"

    _seed(store)
    assert port.query("SELECT count(*) AS n FROM kb_doc", ())[0]["n"] == 3
    assert kb.has_kb_table(store) is True

    local = store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='kb_doc'").fetchall()
    assert local == [], "kb_doc 不该同时在本地 SQLite 上存在"


@live_only
def test_control_plane_stays_on_sqlite(flow_on_pg):
    """控制面四表仍在本地 SQLite —— 换的只有知识层那三张。"""
    store = flow_on_pg
    port = kb.attached_port(store)
    _seed(store)

    names = {r[0] for r in store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"plan", "task", "artifact", "event_log"} <= names

    on_pg = port.query(
        "SELECT count(*) AS n FROM information_schema.tables"
        " WHERE table_schema = current_schema()"
        " AND table_name IN ('plan','task','artifact','event_log')", ())[0]["n"]
    assert on_pg == 0, "控制面不该跟着知识层上 PG"


@live_only
def test_prefilter_narrows_on_postgres(flow_on_pg):
    """七维硬过滤在 PG 上真的收窄，且跨租户一条都召不回。"""
    store = flow_on_pg
    _seed(store)
    assert len(retriever.prefilter(store, {"tenant_id": TENANT})) == 2
    assert len(retriever.prefilter(
        store, {"tenant_id": TENANT, "channel_id": "ch-online"})) == 1
    assert len(retriever.prefilter(store, {"tenant_id": "tnt-other"})) == 1
    hits = retriever.prefilter(store, {"tenant_id": TENANT})
    assert {h["tenant_id"] for h in hits} == {TENANT}, "跨租户的那条不许进候选集"


@live_only
def test_fts_channel_runs_on_tsvector(flow_on_pg):
    """fts 通道在 PG 上走的是 tsvector，不是退化成本地实现。

    判据取**英数**查询：本机 PG 16 没装中文分词器，中文查询由 `fts_search` 抛
    `LookupError`（见下一条），所以「tsvector 这条路活着」只能用英数证。
    """
    store = flow_on_pg
    port = kb.attached_port(store)
    _seed(store)

    rows = port.fts_search("kb_doc", "body", kb.fts_text("lesson"), 5)
    assert rows, "英数查询该命中 —— 空集说明 tsvector 这条路没通"
    assert all(isinstance(doc_id, str) and isinstance(score, float) for doc_id, score in rows)

    plan = port._raw_query(
        "EXPLAIN SELECT id FROM kb_doc"
        " WHERE to_tsvector(%s, body) @@ plainto_tsquery(%s, %s)",
        (port.fts_config(), port.fts_config(), "lesson"))
    text = " ".join(str(v) for row in plan for v in row.values())
    assert "tsvector" in text or "idx_kb_doc_fts" in text, f"执行计划里看不到全文这条路：{text}"


@live_only
def test_chinese_query_degrades_instead_of_pretending(flow_on_pg):
    """中文查询在本机 PG 上**抛**，不伪装成「没命中」。

    F-2 原话：「『后端没准备好』不许伪装成『没命中』」。本机 pgvector/pgvector:pg16
    没装 zhparser，这是如实的结果 —— 不许在材料里说成「缺省支持中文分词检索」。
    """
    store = flow_on_pg
    port = kb.attached_port(store)
    _seed(store)
    with pytest.raises(LookupError, match="中日韩"):
        port.fts_search("kb_doc", "body", kb.fts_text("退款"), 5)


@live_only
def test_vector_channel_degrades_on_text_column(flow_on_pg):
    """向量通道在 PG 上退化 —— `kb_doc.embedding` 是 TEXT，`<=>` 用不了。

    这是「一份 DDL 两个后端、形状必须一样」的直接代价，记在 BACKLOG `## task-t115`。
    退化本身不是缺陷，**装作没退化**才是：所以这里断言它抛，且抛的是那条说得清
    原因的 `LookupError`。要在 PG 上真用 HNSW 得给 PG 侧单开一列 `vector(64)`
    并在写入侧双写，那是两个后端形状分叉，不在本轨。
    """
    store = flow_on_pg
    port = kb.attached_port(store)
    _seed(store)
    dtype = port.query(
        "SELECT data_type FROM information_schema.columns"
        " WHERE table_name='kb_doc' AND column_name='embedding'", ())[0]["data_type"]
    assert dtype == "text"
    with pytest.raises(LookupError):
        port.vector_search("kb_doc", "embedding", retriever.embed("lesson"), 5)


@live_only
def test_retrieve_end_to_end_through_assembly(flow_on_pg):
    """整条检索经装配跑一次：候选来自 PG，命中回 PG 查得到。

    这是「知识层这一跑在 PG 上发生」的完整判据 —— 不是「我设了环境变量」。
    """
    store = flow_on_pg
    port = kb.attached_port(store)
    _seed(store)

    hits = retriever.retrieve(store, {"tenant_id": TENANT, "keyword": "lesson"}, limit=5)
    assert hits, "检索该有命中"
    for hit in hits:
        back = port.query(
            "SELECT tenant_id FROM kb_doc WHERE tenant_id=? AND doc_id=?",
            (TENANT, hit["doc_id"]))
        assert back and back[0]["tenant_id"] == TENANT, "命中必须回得到 PG 的 kb_doc"
