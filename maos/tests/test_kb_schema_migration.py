"""T17 的机器验收 —— **T13 之前建的老库，也能升到 `id` 列那一版**。

## 这一束在守什么

T13 把 `kb_doc.id` 补进了 `schema.sql`，于是**新建**的库对得上 F-2 的主键口径。
但 `schema.sql` 整份都是 `CREATE ... IF NOT EXISTS`：表已经在了就整段跳过，
**改列静默无效**。所以 T13 那笔改动对**已经存在**的库一个字都没改到 ——

* `SELECT id FROM kb_doc` 恒抛 `no such column: id`；
* 检索器那层「通道抛异常就退化成本地实现」的探测于是次次命中，
  端口通道**恒退化**，而它只告警一次（`_port_search` 的设计如此，
  那层是对的 —— 它兜的是「PG 装不上」，不是「列名对不上」）；
* 写入侧更直接：`upsert_doc` 往影子表插 `id` 会当场报 no such column named id。

演示期不咬人（库都是 `:memory:` 或每次新建），PolarDB 是持久库，上线第一天就咬，
而且**咬得没有声音**。本文件把「老库真的升上去了」钉成机器判据。

## 为什么判据不能只看版本号

`kb_schema_version` 刚建出来时是空的 —— **新库和老库在这一刻长得一模一样**。
只按版本号决定跑不跑迁移，新库也会被当成老库去 ALTER，然后撞
`duplicate column name: id`。所以每条迁移步骤自带探针，版本号只是快路径。
第 11 条（新库上一条 DDL 都不发）钉的就是这件事。

## 两张表补法为什么不同

`kb_doc` 走 `ALTER TABLE ADD COLUMN`：SQLite **允许**加 VIRTUAL 生成列
（只有 STORED 不行，报 `cannot add a STORED column`），所以补完的形状与
`schema.sql` 逐字等价，不必「建新表→拷数据→换名」，也就不会在拷贝途中丢
CHECK 与主键。`kb_doc_fts` 是 FTS5 虚表，`ALTER` 一律报
`virtual tables may not be altered`，只能删表→按 schema.sql 重建→从 `kb_doc` 重灌。

## PRAGMA 的坑（读本文件的人最容易在这里判错）

`PRAGMA table_info` **不列生成列**，`PRAGMA table_xinfo` 才列（末位 hidden=2）。
拿 table_info 看 `kb_doc`，新库老库都是 17 列、都「没有 id」—— 照它判会得出
「迁移没生效」的错误结论。本文件一律用 `table_xinfo` 比形状。
"""

from __future__ import annotations

import inspect
import re
import sqlite3
from pathlib import Path

import pytest

from maos import kb
from maos.core.store import SqliteStore
from maos.kb import retriever
from maos.store.sqlite_store import SqliteStorePort

#: T13 **之前**的知识层建表语句，逐字取自 commit d79d815 的 `maos/kb/schema.sql`。
#:
#: 抄一份进测试而不是从 git 里取：测试不该依赖仓库历史还在（浅克隆、导出的
#: tarball 里都不在），而这段的作用就是「把老库的形状固定下来」——
#: 它是历史事实，本来就不该再变。两张表都没有 `id` 列，这是本轨的起点。
PRE_T13_KB_SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_doc (
    tenant_id        TEXT NOT NULL,
    doc_id           TEXT NOT NULL,
    biz_type         TEXT,
    channel_id       TEXT,
    region           TEXT,
    sku              TEXT,
    policy_version   INTEGER,
    workflow_version INTEGER,
    rule_no          TEXT,
    gateway_code     TEXT,
    kind             TEXT NOT NULL,
    title            TEXT NOT NULL DEFAULT '',
    body             TEXT NOT NULL DEFAULT '',
    embedding        TEXT,
    outcome          TEXT,
    source_case_id   TEXT,
    created_at       TEXT NOT NULL,
    PRIMARY KEY (tenant_id, doc_id),
    CHECK (kind IN ('policy', 'history_case', 'failure_hint', 'error_code_playbook')),
    CHECK (outcome IS NULL OR outcome IN ('success', 'failed'))
);
CREATE INDEX IF NOT EXISTS idx_kb_doc_prefilter
    ON kb_doc(tenant_id, biz_type, channel_id, region, sku);
CREATE INDEX IF NOT EXISTS idx_kb_doc_kind ON kb_doc(tenant_id, kind);
CREATE VIRTUAL TABLE IF NOT EXISTS kb_doc_fts USING fts5(
    doc_id UNINDEXED,
    tenant_id UNINDEXED,
    title,
    body
);
"""

TENANT = "tnt-old"

#: 老库里的存量语料。`timeout` 只出现在 body 且是单个英数 token ——
#: 与 `test_kb_pg_channel.py` 同一套取舍，让端口/本地两条通道的比较不被
#: 分词与跨列 OR 的差异搅浑。带 embedding 是为了让向量通道也有东西可召回。
OLD_CORPUS = (
    ("doc-old-a", "支付回执缺失", "gateway timeout retry twice", "AS-101"),
    ("doc-old-b", "回执延迟", "gateway timeout then settled", "AS-102"),
    ("doc-old-c", "包装破损", "carton crushed on arrival", "AS-103"),
)

QUERY = {"tenant_id": TENANT, "biz_type": "refund", "keyword": "timeout"}

SCHEMA_SQL = Path(kb.__file__).with_name("schema.sql").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- 夹具
def _core() -> SqliteStore:
    store = SqliteStore()
    store.init_schema()
    return store


def _old_db() -> SqliteStore:
    """造一个 T13 之前形状、且**已经有存量数据**的库。

    灌数据走裸 SQL 而不是 `kb.upsert_doc()` —— 后者第一件事就是
    `ensure_schema()`，一调就把库升上去了，那这个夹具就没有老库可言。
    """
    store = _core()
    conn = store._conn
    conn.executescript(PRE_T13_KB_SCHEMA)
    for doc_id, title, body, rule_no in OLD_CORPUS:
        conn.execute(
            "INSERT INTO kb_doc (tenant_id, doc_id, kind, biz_type, rule_no,"
            " title, body, embedding, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (TENANT, doc_id, kb.KIND_POLICY, "refund", rule_no, title, body,
             kb.json.dumps(retriever.embed(f"{title} {body}")),
             "2026-01-01T00:00:00+00:00"))
        conn.execute(
            "INSERT INTO kb_doc_fts (doc_id, tenant_id, title, body)"
            " VALUES (?,?,?,?)",
            (doc_id, TENANT, kb.fts_text(title), kb.fts_text(body)))
    conn.commit()
    return store


def _fresh_db() -> SqliteStore:
    store = _core()
    kb.ensure_schema(store)
    return store


def _xinfo(conn: sqlite3.Connection, table: str) -> list[tuple]:
    """列名 + 类型 + 非空 + 默认 + 主键位 + **hidden 标志**。

    hidden=2 就是「VIRTUAL 生成列」，这一位是本轨要买的东西里最关键的一格：
    掉成普通列的话 `SELECT id` 照样能跑，但值要靠写入侧记得填 —— 那正是
    schema.sql 当初选生成列要避开的「只加列不填值」型无症状故障。
    """
    return [tuple(r)[1:] for r in conn.execute(f"PRAGMA table_xinfo({table})")]


def _table_info(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _ddl(conn: sqlite3.Connection, name: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
    return row[0] if row else ""


# ---------------------------------------------------------------------------
# 1. 前提复现 —— 老库确实升不上去，且症状正是「端口通道恒退化」
# ---------------------------------------------------------------------------
def test_pre_t13_db_really_lacks_the_row_id_column():
    """起点必须是真的坏的，否则后面全是空跑。

    两张表都要断言：`kb_doc` 缺 `id` 让检索侧抛，影子表缺 `id` 让**写入侧**抛，
    是两条独立的失效，修一条不算修好。
    """
    store = _old_db()
    for table in ("kb_doc", "kb_doc_fts"):
        with pytest.raises(sqlite3.OperationalError, match="no such column: id"):
            store._conn.execute(f"SELECT id FROM {table}").fetchall()
    assert "id" not in [c for c, *_ in
                        [(r[0], ) for r in _xinfo(store._conn, "kb_doc")]], \
        "老库的 kb_doc 不该有 id 列 —— 夹具没造对，后面的断言全是空跑"


def test_old_db_degrades_both_port_channels_before_migration():
    """未迁移的老库上，两条 StorePort 通道**恒退化** —— 本轨要买掉的正是这个。

    只断言「检索还有结果」是看不出问题的：退化之后召回照常、分数照常、
    日志只有一行。所以这里断言的是 `port_channel_state` 的**探测结论**。

    夹具刻意绕开 `ensure_schema`：`upsert_doc` / `retrieve` 都不会自己去建表，
    是 `kb.ensure_schema` 才会 —— 这条要的就是「没升级过的库」。
    """
    port = SqliteStorePort(_old_db())
    retriever.retrieve(port, QUERY, limit=10)
    assert retriever.port_channel_state(port) == {"fts_search": False,
                                                  "vector_search": False}, \
        "老库上两条通道居然通了 —— 那说明夹具没造出 T13 之前的形状"


def test_old_db_blocks_writes_before_migration():
    """老库上 `upsert_doc` 会当场炸在影子表那句 INSERT。

    这条是「无症状」的例外：写入侧是**响的**。真正没有声音的是检索侧，
    两者都得修，所以两条都钉住。
    """
    store = _old_db()
    with pytest.raises(sqlite3.OperationalError, match="no column named id"):
        # 直接走裸 SQL：`upsert_doc` 会先 ensure_schema 把库升上去。
        store._conn.execute(
            "INSERT INTO kb_doc_fts (id, doc_id, tenant_id, title, body)"
            " VALUES ('x','d','t','a','b')")


# ---------------------------------------------------------------------------
# 2. 迁移把老库升上去
# ---------------------------------------------------------------------------
def test_ensure_schema_adds_row_id_to_an_existing_kb_doc():
    """核心判据：迁移后 `SELECT id FROM kb_doc` 可用，且值就是 `doc_row_id()`。

    值也要对 —— 只断言「列在」会放过一种更坏的情况：列补成了普通列且全是 NULL，
    `SELECT id` 不报错，候选集回查却一条都对不上。
    """
    store = _old_db()
    kb.ensure_schema(store)

    rows = kb.query(store, "SELECT tenant_id, doc_id, id FROM kb_doc"
                           " ORDER BY doc_id")
    assert len(rows) == len(OLD_CORPUS)
    for row in rows:
        assert row["id"] == kb.doc_row_id(row["tenant_id"], row["doc_id"])


def test_migration_rebuilds_the_fts_shadow_table_with_content():
    """影子表补上 `id` 列，**而且老内容一条不少地回来了**。

    虚表加不了列，只能删表重建 —— 重建完忘了重灌的话，BM25 通道从此恒空、
    不报错、日志干净。所以这条同时断言列、行数、`id` 取值，以及 MATCH 真能命中。
    重灌从 `kb_doc` 取（它才是权威），所以断言的是与 `kb_doc` 对齐，不是与旧影子表。
    """
    store = _old_db()
    kb.ensure_schema(store)

    assert _table_info(store._conn, "kb_doc_fts") == [
        "id", "doc_id", "tenant_id", "title", "body"]

    rows = kb.query(store, "SELECT id, doc_id, tenant_id, title, body"
                           " FROM kb_doc_fts ORDER BY doc_id")
    assert len(rows) == len(OLD_CORPUS), "影子表重建了却没重灌 —— BM25 从此恒空"
    for row in rows:
        assert row["id"] == kb.doc_row_id(row["tenant_id"], row["doc_id"])

    hits = kb.query(store, "SELECT doc_id FROM kb_doc_fts"
                           " WHERE kb_doc_fts MATCH ? ORDER BY doc_id", ("timeout",))
    assert [h["doc_id"] for h in hits] == ["doc-old-a", "doc-old-b"], \
        "重灌进去的文本没过 fts_text()，或者压根没灌"


def test_migration_keeps_every_old_row_untouched():
    """存量数据一行不丢、一个字段不改。

    补列走的是 ALTER 而不是「建新表→拷数据→换名」，图的就是这个：
    没有拷贝就没有拷漏。这条把它钉住 —— 哪天有人换成重建表的路子，
    漏拷一列会在这里红，而不是在某次上线之后。
    """
    before = _old_db()
    snapshot = [tuple(r) for r in before._conn.execute(
        "SELECT tenant_id, doc_id, kind, biz_type, rule_no, title, body,"
        " embedding, created_at FROM kb_doc ORDER BY doc_id")]

    kb.ensure_schema(before)

    after = [tuple(r) for r in before._conn.execute(
        "SELECT tenant_id, doc_id, kind, biz_type, rule_no, title, body,"
        " embedding, created_at FROM kb_doc ORDER BY doc_id")]
    assert after == snapshot


def test_migration_unblocks_writes_on_an_old_db():
    """迁移后 `upsert_doc` 能写了，且新写的行两张表都带对 `id`。"""
    store = _old_db()
    kb.ensure_schema(store)

    kb.upsert_doc(store, {"tenant_id": TENANT, "doc_id": "doc-new",
                          "kind": kb.KIND_POLICY, "biz_type": "refund",
                          "title": "迁移后新写入", "body": "gateway timeout again"})

    expected = kb.doc_row_id(TENANT, "doc-new")
    assert kb.query(store, "SELECT id FROM kb_doc WHERE doc_id=?",
                    ("doc-new",))[0]["id"] == expected
    assert kb.query(store, "SELECT id FROM kb_doc_fts WHERE doc_id=?",
                    ("doc-new",))[0]["id"] == expected


def test_migration_restores_both_port_channels():
    """**本轨真正要买的东西**：迁移后端口通道不再恒退化。

    与第 2 条是同一个库、同一套语料，只差一次 `ensure_schema` ——
    对照成立才说明修的是这件事，不是别的什么让它碰巧绿了。

    端口对象必须**重新造一个**：`retriever._PORT_STATE` 是按 store 对象记的
    粘性判定（「只告警一次」的实现方式），同一个对象上探过一次 False 就一直
    是 False。这不是 bug —— 但它意味着**长跑进程里就地升级库并不会自愈**，
    得重开进程或换端口对象。这条已记进 BACKLOG `## task-T17`。
    """
    core = _old_db()
    kb.ensure_schema(core)
    port = SqliteStorePort(core)

    hits = retriever.retrieve(port, QUERY, limit=10)

    assert hits, "迁移后端口通道下一条都没召回"
    assert retriever.port_channel_state(port) == {"fts_search": True,
                                                  "vector_search": True}, \
        "两条通道仍在退化 —— id 列补上了但端口还是走不通"


# ---------------------------------------------------------------------------
# 3. 形状对齐 —— 迁移后的老库 == 新库
# ---------------------------------------------------------------------------
def test_migrated_shape_equals_a_fresh_database():
    """迁移后的老库与全新库，**表结构逐项相同**（含 `id` 是 VIRTUAL 生成列）。

    比的是 `table_xinfo` 而不是 `sqlite_master.sql` 那段文本：ALTER 会把新列
    追加在原 DDL 文本的 `created_at` 那一行末尾，**文本注定不逐字相同**，
    而结构是一样的。要比的是结构，不是排版 —— 拿文本比会红在一个与正确性
    无关的地方，然后诱人去改成重建表，白白引入拷数据的风险。
    """
    migrated = _old_db()
    kb.ensure_schema(migrated)
    fresh = _fresh_db()

    for table in ("kb_doc", "kb_doc_fts", "kb_schema_version"):
        assert _xinfo(migrated._conn, table) == _xinfo(fresh._conn, table), \
            f"{table} 的结构与新库不一致"

    id_col = [c for c in _xinfo(migrated._conn, "kb_doc") if c[0] == "id"]
    assert id_col and id_col[0][-1] == 2, \
        "迁移后的 id 不是 VIRTUAL 生成列（hidden 标志不是 2）—— 退化成普通列了"


def test_migrated_row_id_stays_generated():
    """生成列的语义在迁移后仍然成立：写不得、也无从忘填。

    这正是 schema.sql 当初选生成列而不是普通列的理由。老库上要是补成了普通列，
    `SELECT id` 一样能跑，只是值要靠每个写入口记得填 —— 少填一处就是
    「这条知识检索不到，别的都正常」。
    """
    store = _old_db()
    kb.ensure_schema(store)

    with pytest.raises(sqlite3.OperationalError, match="generated column"):
        store._conn.execute(
            "INSERT INTO kb_doc (tenant_id, doc_id, kind, created_at, id)"
            " VALUES ('t','d','policy','2026-01-01T00:00:00+00:00','xxx')")

    store._conn.execute(
        "INSERT INTO kb_doc (tenant_id, doc_id, kind, created_at)"
        " VALUES ('t','d','policy','2026-01-01T00:00:00+00:00')")
    assert store._conn.execute(
        "SELECT id FROM kb_doc WHERE doc_id='d'").fetchone()[0] == \
        kb.doc_row_id("t", "d")


def test_fresh_database_ddl_is_byte_identical_to_schema_sql():
    """**新库无副作用**：跑完 `ensure_schema` 的新库，DDL 与直接跑 schema.sql 逐字相同。

    迁移最容易犯的错是「顺手把新库也改一遍」—— 新库上多发一条 ALTER，
    要么撞 duplicate column、要么把形状改成了另一个样子。这条按逐字比，
    因为新库上本来就一条 DDL 都不该多发。
    """
    raw = sqlite3.connect(":memory:")
    raw.executescript(SCHEMA_SQL)
    fresh = _fresh_db()

    for table in ("kb_doc", "kb_doc_fts", "kb_schema_version",
                  "idx_kb_doc_prefilter", "idx_kb_doc_kind"):
        assert _ddl(fresh._conn, table) == _ddl(raw, table), f"{table} 的 DDL 被迁移改动了"


def test_migration_issues_no_ddl_on_a_fresh_database(monkeypatch):
    """新库上迁移**一条 DDL 都不发** —— 判据在探针，不在版本号。

    `kb_schema_version` 刚建出来是空的，新库老库在这一刻长得一模一样。
    只按版本号决定跑不跑，新库也会挨一次 ALTER 然后撞 duplicate column name: id。
    这条把「探针先探再做」钉住：探到列已经在，就一句都不发。
    """
    seen: list[str] = []
    real_execute, real_atomic = kb.execute, kb._atomic

    def spy_execute(store, sql, params=()):
        seen.append(sql)
        return real_execute(store, sql, params)

    def spy_atomic(store, statements):
        seen.extend(sql for sql, _ in statements)
        return real_atomic(store, statements)

    monkeypatch.setattr(kb, "execute", spy_execute)
    monkeypatch.setattr(kb, "_atomic", spy_atomic)

    kb.ensure_schema(_core())

    ddl = [s for s in seen if s.split()[0].upper() in ("ALTER", "DROP")]
    assert ddl == [], f"新库上多发了 DDL：{ddl}"
    assert any(s.startswith("INSERT INTO kb_schema_version") for s in seen), \
        "新库连版本都没记 —— 下次启动会把它当老库再探一遍"


# ---------------------------------------------------------------------------
# 4. 幂等与原子性
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("factory", [_old_db, _core], ids=["old", "fresh"])
def test_ensure_schema_is_idempotent(factory):
    """连跑三次不报错，且形状与数据都不再变 —— 老库新库两条路都要。

    第三次是刻意的：第二次走的是「版本号已最新」的快路径，第三次确认快路径
    自己也幂等（它要是把版本记重了，主键会响）。
    """
    store = factory()
    kb.ensure_schema(store)
    shape = _xinfo(store._conn, "kb_doc")
    fts_rows = store._conn.execute("SELECT COUNT(*) FROM kb_doc_fts").fetchone()[0]

    kb.ensure_schema(store)
    kb.ensure_schema(store)

    assert _xinfo(store._conn, "kb_doc") == shape
    assert store._conn.execute(
        "SELECT COUNT(*) FROM kb_doc_fts").fetchone()[0] == fts_rows, \
        "影子表被重复重灌了 —— 同一条知识数进 BM25 好几次，排序会被自己刷上去"
    assert kb.applied_schema_version(store) == kb.KB_SCHEMA_VERSION


def test_version_table_records_one_row_per_applied_migration():
    """版本表一条迁移一行，`applied_schema_version()` 取的是 MAX。

    做成一行一条而不是「一行存当前版本」，是为了让 INSERT 天然幂等
    （版本号是主键，重复插会响），而不是读-改-写 —— 后者两个进程同时升级会互相盖掉。
    """
    store = _old_db()
    kb.ensure_schema(store)

    rows = kb.query(store, "SELECT version, applied_at FROM kb_schema_version"
                           " ORDER BY version")
    assert [r["version"] for r in rows] == [v for v, _l, _s in kb._MIGRATIONS]
    assert all(r["applied_at"] for r in rows), "记账没写时间戳"
    assert kb.applied_schema_version(store) == kb.KB_SCHEMA_VERSION

    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute(
            "INSERT INTO kb_schema_version (version, applied_at)"
            " VALUES (?, ?)", (kb.KB_SCHEMA_VERSION, kb.now_iso()))


def test_failed_fts_rebuild_rolls_back_the_whole_step(monkeypatch):
    """影子表重建**整块回滚** —— 删了表却没建回来是最坏的一种半成品。

    「删表 → 建表 → 重灌」三步各自提交的话，中途断在第三步就留下一张
    列全、行空的影子表。下一次跑迁移，探针看到 `id` 列在于是跳过，
    BM25 从此恒空且不报错 —— 本轨要买掉的正是这类无症状失效，不能自己再造一个。

    这里让重建用的 DDL 变成一句错的 SQL 来制造失败，然后断言：表还在、
    老内容还在、版本**没有**被记上（下次重跑还会再来一遍）。
    """
    store = _old_db()
    monkeypatch.setattr(kb, "_fts_create_statement", lambda script: "CREATE NONSENSE")

    with pytest.raises(sqlite3.OperationalError):
        kb.ensure_schema(store)

    rows = store._conn.execute(
        "SELECT doc_id FROM kb_doc_fts ORDER BY doc_id").fetchall()
    assert [r[0] for r in rows] == [d for d, *_ in OLD_CORPUS], \
        "影子表没回滚回来 —— 老库被迁移弄丢了全文索引"
    assert kb.applied_schema_version(store) == 0, \
        "步骤失败了却记了账，下次不会重试 —— 库永远停在半成品上"

    # 解除注入后重跑，必须自愈到目标形状。
    monkeypatch.undo()
    kb.ensure_schema(store)
    assert kb.applied_schema_version(store) == kb.KB_SCHEMA_VERSION
    assert _table_info(store._conn, "kb_doc_fts")[0] == "id"


def test_failed_fts_rebuild_rolls_back_through_the_store_port(monkeypatch):
    """回滚在**端口路径**上同样成立 —— 真上线走的是这条，不是 `_conn` 那条。

    两条路径的事务机制不一样（端口靠自己的 `transaction()` 计深度），所以
    「`_conn` 那条绿了」推不出这条也绿。实际上两条**都**需要显式 SAVEPOINT：
    Python 的 sqlite3 不为 DDL 开事务，端口的 `transaction()` 也只是延后 commit。
    """
    core = _old_db()
    port = SqliteStorePort(core)
    monkeypatch.setattr(kb, "_fts_create_statement", lambda script: "CREATE NONSENSE")

    with pytest.raises(sqlite3.OperationalError):
        kb.ensure_schema(port)

    rows = kb.query(port, "SELECT doc_id FROM kb_doc_fts ORDER BY doc_id")
    assert [r["doc_id"] for r in rows] == [d for d, *_ in OLD_CORPUS]
    assert kb.applied_schema_version(port) == 0


class _NoTransactionPort:
    """只有 F-2 必需的 `execute` / `query`、**没有** `transaction()` 的最小端口。

    内部连接刻意不叫 `_conn` —— 叫 `_conn` 会被 `port_of()` 判成核心 store，
    整条端口分支就绕过去了，这个夹具也就白造了。
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._sql = conn

    def execute(self, sql: str, params: tuple = ()) -> None:
        self._sql.execute(sql, tuple(params))
        self._sql.commit()

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        cur = self._sql.execute(sql, tuple(params))
        cols = [d[0] for d in cur.description] if cur.description else []
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def test_migration_refuses_a_port_that_cannot_hold_a_transaction():
    """端口给不了事务就**当场抛**，不退化成逐条提交。

    `upsert_doc` 在同样的情形下是退化的，因为那里的后果是「全文同步慢一拍」。
    迁移不一样：逐条提交意味着持久库可能停在「表在、列全、数据空」的半成品上，
    而那个状态下一次迁移的探针会认为已经做完 —— 没有症状的失效。宁可现在响。

    注意抛的位置：`kb_doc` 那条 ALTER 走的是普通 `execute`，先成功；抛在
    影子表重建那一步。所以断言不能只看「抛没抛」，还要确认抛的是 KbError。
    """
    store = _old_db()
    port = _NoTransactionPort(store._conn)
    assert kb.port_of(port) is port, "夹具没被认成端口 —— 这条测的就不是端口路径"

    with pytest.raises(kb.KbError, match="没法整块回滚"):
        kb.ensure_schema(port)

    assert kb.applied_schema_version(port) == 0


# ---------------------------------------------------------------------------
# 5. 端口路径
# ---------------------------------------------------------------------------
def test_migration_runs_through_the_store_port_too():
    """迁移在 StorePort 路径上同样生效 —— 那条路才是 PolarDB 走的。

    端口路径上没有 `executescript`（F-2 五个签名里没有它），建表是按 `;` 硬切
    逐条发的；迁移也一样，全部经 `execute` / `query`，不碰任何 sqlite 专属 API。
    这条要是只在 `_conn` 路径上绿，真上线时等于没做。
    """
    port = SqliteStorePort(_old_db())

    kb.ensure_schema(port)

    assert kb.applied_schema_version(port) == kb.KB_SCHEMA_VERSION
    rows = kb.query(port, "SELECT tenant_id, doc_id, id FROM kb_doc ORDER BY doc_id")
    assert len(rows) == len(OLD_CORPUS)
    for row in rows:
        assert row["id"] == kb.doc_row_id(row["tenant_id"], row["doc_id"])
    assert port.fts_search("kb_doc", "body", kb.fts_text("timeout"), 5), \
        "端口的全文通道仍然走不通"


# ---------------------------------------------------------------------------
# 6. 契约守护 —— 防漂、分号、签名
# ---------------------------------------------------------------------------
def test_row_id_expr_is_the_same_in_python_and_schema_sql():
    """`DOC_ROW_ID_EXPR` 与 schema.sql 里那条生成列表达式**逐字一致**。

    同一个口径现在有三处：schema.sql 的列定义、`doc_row_id()`、以及补列用的
    `DOC_ROW_ID_EXPR`。三处漂开的后果不是报错，是新库与迁移后的老库拼法不同 ——
    `id` 对不上号，而两边都不抛。这条让「改一处不改另一处」当场红。
    """
    assert kb.DOC_ROW_ID_EXPR in SCHEMA_SQL
    assert kb.doc_row_id("a", "b") == "a" + kb.DOC_ROW_ID_SEP + "b"
    assert f"GENERATED ALWAYS AS ({kb.DOC_ROW_ID_EXPR}) VIRTUAL" in SCHEMA_SQL, \
        "schema.sql 里那列不再是按 DOC_ROW_ID_EXPR 定义的 VIRTUAL 生成列"


def test_schema_sql_has_no_semicolon_inside_string_literals():
    """`_schema_statements()` 是按 `;` 硬切的，schema.sql 里就不能有含分号的字面量。

    端口路径上没有 `executescript`，只能自己切。哪天有人往 CHECK 或 DEFAULT 里
    写一个带分号的字符串，切法会把一条语句劈成两半 —— 报的错会指向一句
    根本不存在的 SQL，查起来极费劲。T17 新增的迁移语句同样守这条（它们连
    分号都没有），但守卫得钉在文件上，不然下一个人加的时候没人拦。
    """
    literals = re.findall(r"'[^']*'", SCHEMA_SQL)
    assert [lit for lit in literals if ";" in lit] == []
    assert len(kb._schema_statements(SCHEMA_SQL)) == 5, \
        "schema.sql 的语句条数变了 —— 加表要一并确认切分还是对的"


def test_fts_create_statement_is_taken_from_schema_sql():
    """重建影子表用的 DDL 现取自 schema.sql，Python 里没有第二份。"""
    statement = kb._fts_create_statement(SCHEMA_SQL)
    assert "CREATE VIRTUAL TABLE IF NOT EXISTS kb_doc_fts" in statement
    assert statement in SCHEMA_SQL

    with pytest.raises(kb.KbError, match="期望恰好 1 条"):
        kb._fts_create_statement("CREATE TABLE t (a TEXT)")


def test_ensure_schema_signature_is_unchanged():
    """契约 1：`ensure_schema()` 的签名一个字节都不许动。

    T16 轨正在按现签名调用它。本轨只许换里子 —— 这条把面子钉住。

    注解带引号是 `from __future__ import annotations` 的效果（注解全是字符串），
    不是签名里真写了引号。同时按「零件」再断言一遍：调用方在乎的是
    「一个位置参数、没有默认值」，那才是改了会当场炸别人的部分。
    """
    sig = inspect.signature(kb.ensure_schema)
    assert list(sig.parameters) == ["store"]
    assert sig.parameters["store"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert sig.parameters["store"].default is inspect.Parameter.empty
    assert str(sig) == "(store: 'Any') -> 'None'"
