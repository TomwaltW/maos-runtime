"""知识层（KB/RAG）—— 建表、读写口径、开关、取值域常量。

**为什么这里自带一层 SQL 访问器**：核心 store 是冻结面（表结构禁改，只许新增表），
而 `Store` 抽象基类只有 plan/task/artifact/event_log 那几个具名方法，没有通用 execute。
`kb_doc` 是新增表，只能从 `SqliteStore` 的连接上走 —— 与退款域 `objects.py` 同一套做法，
理由也一样：冻结面一个字不动，新增表由使用方自己建。

**两条底层路径**：知识层既要能吃核心 `Store`（`_conn` 上挂着一条 sqlite 连接），
也要能吃 StorePort（F-2 的 `execute` / `query`）—— 后者是「RAG 真跑在 PolarDB 上」
的前提，不接上的话检索器里那条端口分支在真链路上一次都走不到。判据集中在
`port_of()` 一个函数里，**不散在各调用点**：散开的后果是某几处走了新路、某几处
还走老路，而两边都不报错。口径是「**`_conn` 可调用**才走老路径」，**不是**「有
`_conn` 就走老路径」—— 后一种写法会把连上库的 `PgStorePort` 判成核心 store
（它的 `_conn` 是存连接的属性，连上后是 psycopg 连接），于是 PG 后端一连上，
`ensure_schema()` 就拿这条连接去调 sqlite 专有的 `executescript()` 当场炸。
换判据之后缺省行为仍是一个字节不变，为什么分得开见 `port_of()` 的 docstring。

**依赖方向**：本模块**不** import `maos.kb.retriever`，也不 import `maos.skills` /
`maos.agents` 任何一层。retriever 与 guardrails 反过来 import 本模块 ——
`__init__` 一旦回 import 子模块，`skills/builtin/kb_retrieve.py` 那条 import 链就成环。
使用方写 `from maos.kb.retriever import retrieve`，不要指望从包顶层拿到它。

**建表与迁移是两件事**：`schema.sql` 只描述**目标形状**，整份都是 `IF NOT EXISTS`，
所以它对**已经存在**的表一个字都改不动 —— 改列静默无效，直到某条 SELECT 报
no such column。把老库搬到目标形状的是本模块的 `_MIGRATIONS`，记账落在
`kb_schema_version` 表。演示期的库都是 `:memory:` 或每次新建，两者的区别看不出来；
PolarDB 是持久库，区别就是「上线第一天端口通道恒退化，且只告警一次」。

**开关**：`MAOS_KB_ENABLED` 是 RAG 有无对照实验（R5）的唯一变量。
缺省**启用** —— 关掉检索是实验条件，不是缺省形态。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from maos.config import get_config_source

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")

#: 英数按词、中文按字。FTS5 的 unicode61 分词器不切中文，写入与查询两侧都先过它。
_TOKEN_RE = re.compile(r"[0-9a-zA-Z]+|[一-鿿]")

# -- kind / outcome 取值域（与 schema.sql 的 CHECK 同一份口径）-----------------
KIND_POLICY = "policy"
KIND_HISTORY_CASE = "history_case"
KIND_FAILURE_HINT = "failure_hint"
KIND_ERROR_CODE_PLAYBOOK = "error_code_playbook"
VALID_KINDS = (KIND_POLICY, KIND_HISTORY_CASE, KIND_FAILURE_HINT, KIND_ERROR_CODE_PLAYBOOK)

OUTCOME_SUCCESS = "success"
OUTCOME_FAILED = "failed"
VALID_OUTCOMES = (OUTCOME_SUCCESS, OUTCOME_FAILED)

#: 只有这几类进「规划正例」。failure_hint 只用来提示哪类组合需要额外步骤，
#: 它**不是**正例（晋升规则见 guardrails.classify_case）。
POSITIVE_KINDS = (KIND_POLICY, KIND_HISTORY_CASE, KIND_ERROR_CODE_PLAYBOOK)

KB_ENABLED_ENV = "MAOS_KB_ENABLED"
KB_WEIGHTS_ENV = "MAOS_KB_WEIGHTS"

#: `kb_enabled()` 认的那几个关值。抽出来只为让两条分支（显式 env / 配置面）
#: 共用同一份口径 —— 两处各写一份的后果是「显式传字典时开关失灵」，而那不报错。
_KB_OFF_VALUES = ("0", "false", "no", "off")

#: `kb_doc.id` 的拼接分隔符。与 schema.sql 里那条生成列表达式是同一份口径。
DOC_ROW_ID_SEP = ":"

#: `kb_doc.id` 那条生成列的 SQL 表达式，与 schema.sql 里那句**逐字一致**。
#:
#: 为什么要在 Python 里也留一份：给老库补这列走的是 `ALTER TABLE ADD COLUMN`，
#: 而 `schema.sql` 里那句被裹在 `CREATE TABLE` 的列定义中间，取不出来单用。
#: 两处漂开的后果是新库与迁移后的老库拼法不同 —— 两边都不报错，只是 `id`
#: 对不上号。`test_kb_schema_migration.py` 钉了一条断言：这串必须出现在
#: schema.sql 里，改一处不改另一处会当场红。
DOC_ROW_ID_EXPR = f"tenant_id || '{DOC_ROW_ID_SEP}' || doc_id"

#: kb_doc 的全部列，落库与读取共用一份，免得两处漂。
DOC_COLUMNS = (
    "tenant_id", "doc_id", "biz_type", "channel_id", "region", "sku",
    "policy_version", "workflow_version", "rule_no", "gateway_code",
    "kind", "title", "body", "embedding", "outcome", "source_case_id", "created_at",
)


class KbError(RuntimeError):
    """知识层自己的硬失败。写入侧的取值域越界走这个，不降级。"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def tokenize(text: str) -> list[str]:
    """英数按词切、中文按字切。中英混排的语料上两种都要有。"""
    return _TOKEN_RE.findall(str(text or "").lower())


def fts_text(text: str) -> str:
    """进 FTS5 影子表前的规整：切好的 token 用空格连起来。

    写入侧与查询侧共用这一个函数。两边各写一份的后果不是报错，是**召回恒为空** ——
    索引里存的是「锈 蚀」，查询发的是「锈蚀」，一条都对不上，而日志一片正常。
    """
    return " ".join(tokenize(text))


def doc_row_id(tenant_id: Any, doc_id: Any) -> str:
    """F-2 口径下 `kb_doc` 那一行的主键值（就是 `kb_doc.id` 列的内容）。

    F-2 附则要求源表主键**列名固定为 `id`**、取出来一律转 `str`，而本表的主键是
    `(tenant_id, doc_id)` 两列 —— 压进单列只能拼。拼法与 `schema.sql` 里那条
    生成列表达式必须逐字一致，所以写在这里一份、那边一份，改要一起改。

    **不提供反解函数**：`doc_id` 里带冒号时劈字符串会劈错，而调用方手上永远有
    候选集，正向构造一张 `id -> doc_id` 的回查表即可（见 retriever）。
    少一个能用错的 API，就少一类没有症状的错。
    """
    return f"{tenant_id}{DOC_ROW_ID_SEP}{doc_id}"


def kb_enabled(env: dict | None = None) -> bool:
    """检索开关。缺省启用；只有显式关掉才关。

    识别的关值：0 / false / no / off（大小写无关）。其余一律当启用 ——
    读不懂的配置值回落到「启用」而不是「关闭」：静默关掉 RAG 的症状是
    「效果不好」而不是报错，那是最难被发现的一种失效。

    T35 起「现读」这个动作走 `maos.config` 的配置面。缺省源就是 `os.environ.get`，
    取值逐字节不变 —— 未设与设成空串在改之前就都落在「其余一律当启用」那一支上，
    所以把 `None` 换成 `""` 这个 sentinel 不改变任何一格取值表。
    `MAOS_CONFIG_SOURCE=nacos` 时同一句改从 Nacos 取，于是这个开关**不重启就能改**。

    显式 `env` 那一支不走配置面是刻意的，口径照抄 `current_approvers`：
    `kb_enabled({...})` 的语义是「就按我给的这份读」，改成读配置面会让
    `experiment.py` 的对照实验拿到一份自己没给过的开关值。
    """
    if env is not None:
        return str(env.get(KB_ENABLED_ENV) or "").strip().lower() not in _KB_OFF_VALUES
    return get_config_source().get(KB_ENABLED_ENV, "").strip().lower() not in _KB_OFF_VALUES


# ---------------------------------------------------------------- 底层连接
def port_of(store: Any) -> Any | None:
    """「这个 store 该走 StorePort 还是走 `_conn`」的**唯一**判据。返回端口或 None。

    口径是 **可调用的 `_conn` 优先**，不是「有 `_conn` 就优先」，也不是「谁的方法多
    听谁的」：

    · `_conn` **可调用**的对象一律返回 None，走下面的老路径 —— 核心 `SqliteStore`
      以及测试里那些继承它的 store 全在此列，缺省行为因此一个字节都不变。
    · `_conn` 不可调用（**含压根没有 `_conn`**）、却有 `execute` + `query` 的对象
      才认作 StorePort。`PgStorePort` 正在此列：它的 `_conn` 是存连接的实例属性，
      未连接时是 None、连上后是 psycopg 连接，两种形态都不可调用。

    **为什么判「可不可调用」而不是判「有没有」**：老判据是「有 `_conn` 就走老路径」，
    它在 `PgStorePort` 连上库的那一刻判反 —— `_conn` 从 None 变成 psycopg 连接，
    `port_of()` 于是改口说「这是核心 store」，`ensure_schema()` 拿这条连接去调
    sqlite 专有的 `executescript()` 当场 `AttributeError`。这种失效**没连库时判得对、
    一连上就错**，是最难查的一类，别再把它改回「有没有」。

    **「可不可调用」这条为什么分得开**（换 Python 版本 / 换驱动时先复核这几行）：
    `SqliteStore._conn` 存的是 `sqlite3.Connection`，该类自带 `__call__`（建预编译
    语句的老接口），所以可调用；`psycopg.Connection` 整条 MRO 都没有 `__call__`，
    所以不可调用。退一步说，就算哪天 sqlite 把 `__call__` 摘了，核心 `SqliteStore`
    也没有 `execute` / `query` 两个方法，仍旧落回 None —— 两道防线各自都够，
    这条判据不是只押在那个 `__call__` 上。

    只探 `execute` / `query` 两个方法，不探满 F-2 五个：知识层的 SQL 访问器只用
    这两个，`fts_search` / `vector_search` 由检索器自己按能力探测（那两条通道
    走不通要退化，而 execute/query 走不通没有退化路径可言）。
    """
    if store is None or callable(getattr(store, "_conn", None)):
        return None
    if callable(getattr(store, "execute", None)) and callable(getattr(store, "query", None)):
        return store
    return None


def _conn(store: Any) -> sqlite3.Connection:
    """取底层连接。只认暴露了 `_conn` 的 Store 实现（当前是 `SqliteStore`）。

    走到这里说明 `port_of()` 也没认出 StorePort，两条路都不通才抛。
    """
    conn = getattr(store, "_conn", None)
    if conn is None:
        raise TypeError(
            f"{type(store).__name__} 既没有暴露 sqlite 连接、也不是 StorePort"
            "（要有 execute + query 两个方法），知识层的新增表无处落库。"
            " 换后端时实现 StorePort，不要去改冻结的核心 store。"
        )
    return conn


def lock_of(store: Any) -> Any:
    """借 Store 自己的锁 —— 连接是共享的（check_same_thread=False + 一把 RLock）。

    绕过 store 直接用这条连接，就必须一并用它那把锁，否则别的线程一次 commit()
    就把这边只写了一半的事务提交掉了。
    """
    lock = getattr(store, "_lock", None)
    return lock if lock is not None else contextlib.nullcontext()


def execute(store: Any, sql: str, params: tuple | list = ()) -> None:
    port = port_of(store)
    if port is not None:
        # 端口自己管锁与提交（F-2 的 execute 就是「一条语句一次提交」）。
        port.execute(sql, tuple(params))
        return
    conn = _conn(store)
    with lock_of(store):
        conn.execute(sql, tuple(params))
        conn.commit()


def query(store: Any, sql: str, params: tuple | list = ()) -> list[dict]:
    port = port_of(store)
    if port is not None:
        # F-2 的 query 已经承诺返回 list[dict]，这里再 dict() 一遍是为了让两条
        # 路径的返回值**同一个类型**：sqlite3.Row 支持下标取列，dict 不支持，
        # 上游哪天顺手写了 row[0] 就会只在一条路径上炸。
        return [dict(r) for r in port.query(sql, tuple(params))]
    with lock_of(store):
        rows = _conn(store).execute(sql, tuple(params)).fetchall()
    return [dict(r) for r in rows]


def _schema_statements(script: str) -> list[str]:
    """把 schema.sql 拆成一条条语句 —— StorePort 只有 `execute`，没有 executescript。

    按 `;` 切，只丢掉纯注释 / 纯空白的段。**这条切法依赖 schema.sql 里没有任何
    含分号的字符串字面量**；现在没有，加的时候要么别加，要么把这里换成真解析器。
    """
    statements = []
    for chunk in script.split(";"):
        meat = "\n".join(line for line in chunk.splitlines()
                         if not line.strip().startswith("--"))
        if meat.strip():
            statements.append(chunk.strip())
    return statements


def _fts_create_statement(script: str) -> str:
    """schema.sql 里 `kb_doc_fts` 那条建表语句 —— 迁移要重建它，DDL 只留一份。

    FTS5 是虚表，`ALTER TABLE` 加不了列（SQLite 硬限制，报
    `virtual tables may not be altered`），补 `id` 只能删表重建。重建用的 DDL
    从 schema.sql **现取**，不在这里另抄一份：抄一份的后果是哪天有人改了影子表的列，
    老库重建出来的表和新库不是同一张表，而两边都不报错。
    """
    hits = [s for s in _schema_statements(script)
            if "kb_doc_fts" in s and "CREATE VIRTUAL TABLE" in s.upper()]
    if len(hits) != 1:
        raise KbError(
            f"schema.sql 里有 {len(hits)} 条 kb_doc_fts 的建表语句，期望恰好 1 条。"
            " 迁移不敢猜该按哪一条重建影子表 —— 先把 schema.sql 改回一条。")
    return hits[0]


#: 迁移那组「同生共死」语句用的保存点名。
_MIGRATE_SAVEPOINT = "kb_schema_migrate"


def _atomic(store: Any, statements: list[tuple[str, tuple]]) -> None:
    """一组语句同生共死，**DDL 也算在内**。

    迁移非用它不可：影子表的重建是「删表 → 建表 → 重灌」三步，断在中间而前
    两步已落盘的话，表在、列全、**一行数据都没有** —— 下一次跑迁移的探针看到
    `id` 列已存在于是跳过，BM25 从此恒空且不报错。本轨要买掉的正是这类无症状
    失效，不能自己再造一个。

    **光靠 `rollback()` 撤不回 DDL**（这一条是被 `test_kb_schema_migration.py`
    里那条回滚用例逼出来的，不是想当然）：Python 的 sqlite3 在传统模式下只为
    DML 隐式开事务，`DROP TABLE` 是在自动提交下跑的，一发就落盘；端口的
    `transaction()` 同理 —— 它只是延后 commit，没有开事务。显式发一句
    `SAVEPOINT` 才能把 DDL 拉进事务，两条路径都认。

    用 `SAVEPOINT` 而不是 `BEGIN`：外层已经在事务里时 `BEGIN` 会报
    cannot start a transaction within a transaction，`SAVEPOINT` 开不开事务都能用。

    端口没有 `transaction()` 时**直接抛**，不像 `upsert_doc` 那样退化成逐条提交：
    那里退化的后果是「同步慢一拍」，这里是「持久库停在半成品上」，不是一回事。
    """
    port = port_of(store)
    if port is not None:
        transaction = getattr(port, "transaction", None)
        if not callable(transaction):
            raise KbError(
                f"{type(port).__name__} 没有 transaction()，schema 迁移没法整块回滚。"
                " 迁移中途失败会让库停在半成品上（表在、列全、数据空），"
                " 那种失效没有症状 —— 宁可现在响。")
        with transaction():
            port.execute(f"SAVEPOINT {_MIGRATE_SAVEPOINT}", ())
            try:
                for sql, params in statements:
                    port.execute(sql, params)
            except BaseException:
                port.execute(f"ROLLBACK TO {_MIGRATE_SAVEPOINT}", ())
                port.execute(f"RELEASE {_MIGRATE_SAVEPOINT}", ())
                raise
            port.execute(f"RELEASE {_MIGRATE_SAVEPOINT}", ())
        return
    conn = _conn(store)
    with lock_of(store):
        conn.execute(f"SAVEPOINT {_MIGRATE_SAVEPOINT}")
        try:
            for sql, params in statements:
                conn.execute(sql, params)
        except BaseException:
            conn.execute(f"ROLLBACK TO {_MIGRATE_SAVEPOINT}")
            conn.execute(f"RELEASE {_MIGRATE_SAVEPOINT}")
            conn.rollback()
            raise
        conn.execute(f"RELEASE {_MIGRATE_SAVEPOINT}")
        conn.commit()


def _has_column(store: Any, table: str, column: str) -> bool:
    """探「这张表有没有这一列」。用一条 SELECT，**不用 PRAGMA**。

    两条理由，第二条是坑：

    · PRAGMA 是 SQLite 方言，端口路径上不保证有（PG 那边没有）。
    · `PRAGMA table_info` **不列生成列** —— 生成列要 `table_xinfo` 才看得到
      （hidden=2）。拿 table_info 判 `kb_doc.id` 在不在，新库上会答「不在」，
      于是每次建表都去 ALTER 一次，每次都撞 duplicate column name: id。

    表名列名都是本模块的字面量，不是外来输入，所以直接拼进 SQL。
    异常一律当「没这列」：各后端抛的类型不一样，这里只关心选不选得出来；
    真是连接坏了，紧随其后的 ALTER 会自己响，不会被这层吞掉。
    """
    try:
        query(store, f"SELECT {column} FROM {table} LIMIT 1")
        return True
    except Exception:
        return False


def _migrate_v1_row_id(store: Any, script: str) -> None:
    """v1：给 `kb_doc` / `kb_doc_fts` 补 F-2 口径的 `id` 列（T13 定的形状）。

    没有这一步，T13 之前建的库上 `SELECT id FROM kb_doc` 恒抛 no such column，
    检索器那条端口分支**每次都退化**成本地实现、且只告警一次；写入侧更直接 ——
    `upsert_doc` 往影子表插 `id` 会当场报 no such column named id。

    两张表补法不同，因为 SQLite 的限制不同：

    · `kb_doc` 走 `ALTER TABLE ADD COLUMN`。VIRTUAL 生成列**可以**这么加
      （只有 STORED 不行），所以老库补完的形状与 schema.sql 逐字相同 ——
      不必「建新表→拷数据→换名」，也就不会在拷贝途中丢掉 CHECK 与主键。
    · `kb_doc_fts` 是虚表，加不了列，只能删表→按 schema.sql 重建→从 kb_doc 重灌。
      重灌从 `kb_doc` 取而不是从旧影子表取：`kb_doc` 才是权威，影子表是它的投影；
      而且 title/body 进影子表前要过 `fts_text()`，旧表里存的已经是切过的文本，
      再切一次不等幂。

    每一步先探再做，所以新库上整条是 no-op：新库的两列本来就在，探针答「在」，
    一条 ALTER 都不发。**判据在探针不在版本号** —— 新库刚建完时版本表同样是空的，
    只看版本号会把新库也当老库去 ALTER。
    """
    if not _has_column(store, "kb_doc", "id"):
        execute(store, "ALTER TABLE kb_doc ADD COLUMN id TEXT"
                       f" GENERATED ALWAYS AS ({DOC_ROW_ID_EXPR}) VIRTUAL")
    if not _has_column(store, "kb_doc_fts", "id"):
        rows = query(store, "SELECT tenant_id, doc_id, title, body FROM kb_doc"
                            " ORDER BY created_at, doc_id")
        statements = [("DROP TABLE kb_doc_fts", ()),
                      (_fts_create_statement(script), ())]
        for row in rows:
            statements.append((
                "INSERT INTO kb_doc_fts (id, doc_id, tenant_id, title, body)"
                " VALUES (?,?,?,?,?)",
                (doc_row_id(row["tenant_id"], row["doc_id"]), row["doc_id"],
                 row["tenant_id"], fts_text(row["title"]), fts_text(row["body"]))))
        _atomic(store, statements)


#: 迁移步骤表，**按版本号升序**。加一步就在末尾追加一条，不要改已有的那几条 ——
#: 已经跑过的库不会再跑一遍它们，改了等于新老库形状分叉。
#: 每一步都必须自带探针、在**已是目标形状**的库上是 no-op（新库要靠这条）。
_MIGRATIONS: tuple[tuple[int, str, Any], ...] = (
    (1, "kb_doc / kb_doc_fts 补 F-2 口径的 id 列", _migrate_v1_row_id),
)

#: 知识层 schema 的当前版本。跟着 `_MIGRATIONS` 算，不手写 —— 手写的那份
#: 迟早和实际步骤对不上，而对不上的症状是「迁移悄悄不跑了」。
KB_SCHEMA_VERSION = max((v for v, _label, _step in _MIGRATIONS), default=0)


def applied_schema_version(store: Any) -> int:
    """这库已经升到第几版。没有记账行就是 0（T13 之前建的老库都在这一档）。"""
    rows = query(store, "SELECT MAX(version) AS version FROM kb_schema_version")
    version = rows[0]["version"] if rows else None
    return int(version) if version is not None else 0


def _migrate(store: Any, script: str) -> None:
    """把库升到 `KB_SCHEMA_VERSION`，并逐条记账。

    版本号是**快路径**不是判据：已经记到最新就直接返回，省掉每次 `upsert_doc`
    都发几条探针（`ensure_schema` 挂在写入口上，调用频次很高）。
    真正决定做不做的是每一步自己的探针，见 `_migrate_v1_row_id`。

    记账写在步骤之后：中途失败就不记，下次重跑同一步 —— 步骤是幂等的，重跑安全。
    """
    applied = applied_schema_version(store)
    if applied >= KB_SCHEMA_VERSION:
        return
    for version, _label, step in _MIGRATIONS:
        if version <= applied:
            continue
        step(store, script)
        execute(store, "INSERT INTO kb_schema_version (version, applied_at)"
                       " VALUES (?, ?)", (version, now_iso()))


def ensure_schema(store: Any) -> None:
    """建表 + 迁移到最新版本。幂等，可连跑。

    检索与写入两侧都先调它：知识层的表不属于 `init_schema()` 的五表，
    谁先用到谁负责建，不指望调用方记得。

    **两段，缺一不可**：schema.sql 那段全是 `IF NOT EXISTS`，只管「表不在就建」，
    对已经存在的表一个字都改不动；`_migrate()` 那段才管「表在但形状旧」。
    只有第一段的时候，改列是静默无效的 —— 那正是 T13 补 `id` 列之后
    老库仍旧 `no such column: id` 的原因。
    """
    script = _SCHEMA_PATH.read_text(encoding="utf-8")
    port = port_of(store)
    if port is not None:
        for statement in _schema_statements(script):
            port.execute(statement, ())
    else:
        conn = _conn(store)
        with lock_of(store):
            conn.executescript(script)
            conn.commit()
    _migrate(store, script)


def has_kb_table(store: Any) -> bool:
    rows = query(
        store, "SELECT name FROM sqlite_master WHERE type='table' AND name='kb_doc'")
    return bool(rows)


# ---------------------------------------------------------------- 写入口
def upsert_doc(store: Any, doc: dict) -> dict:
    """写一条知识文档，并同步全文影子表。返回落库后的整行。

    取值域越界**抛**不降级：kind/outcome 写错的条目查得出来但归不了类。
    `tenant_id` 与 `doc_id` 是主键，缺一即抛 —— 没有租户的知识无处安放，
    而阶段一预过滤把 tenant_id 当硬约束（跨租户永不召回），缺了它这条知识
    要么谁都检不到、要么谁都能检到，两种都是事故。
    """
    row = {k: doc.get(k) for k in DOC_COLUMNS}
    for key in ("tenant_id", "doc_id"):
        if not row.get(key):
            raise KbError(f"kb_doc.{key} 不能为空")
    if row["kind"] not in VALID_KINDS:
        raise KbError(f"kb_doc.kind 必须是 {VALID_KINDS} 之一，实际 {row['kind']!r}")
    if row["outcome"] is not None and row["outcome"] not in VALID_OUTCOMES:
        raise KbError(f"kb_doc.outcome 必须是 {VALID_OUTCOMES} 之一或 None，"
                      f"实际 {row['outcome']!r}")
    row["title"] = " ".join(str(row.get("title") or "").split())
    row["body"] = str(row.get("body") or "")
    if isinstance(row.get("embedding"), (list, tuple)):
        row["embedding"] = json.dumps([float(x) for x in row["embedding"]])
    row["created_at"] = row.get("created_at") or now_iso()

    ensure_schema(store)
    cols = ", ".join(DOC_COLUMNS)
    marks = ", ".join("?" for _ in DOC_COLUMNS)
    # `kb_doc.id` 是生成列，写不得也无从忘填；影子表那列是普通列，只能在这里填。
    # 两处必须是同一个值，所以都走 `doc_row_id()`，不各拼各的。
    row_id = doc_row_id(row["tenant_id"], row["doc_id"])
    statements = [
        (f"INSERT OR REPLACE INTO kb_doc ({cols}) VALUES ({marks})",
         tuple(row[c] for c in DOC_COLUMNS)),
        # 影子表没有主键，REPLACE 管不到它：不先删就会攒出同一 doc_id 的多份副本，
        # BM25 于是把同一条知识数进去好几次，排序被自己刷上去。
        ("DELETE FROM kb_doc_fts WHERE doc_id=? AND tenant_id=?",
         (row["doc_id"], row["tenant_id"])),
        ("INSERT INTO kb_doc_fts (id, doc_id, tenant_id, title, body) VALUES (?,?,?,?,?)",
         (row_id, row["doc_id"], row["tenant_id"],
          fts_text(row["title"]), fts_text(row["body"]))),
    ]

    port = port_of(store)
    if port is not None:
        # 三条语句必须同生共死：只提交前两条就会留下一条查不到全文的知识。
        # `transaction()` 不在 F-2 五方法里，所以是能力探测而不是硬依赖 ——
        # 端口没有它就退化成逐条提交，那是「同步慢一拍」，不是「同步不了」。
        transaction = getattr(port, "transaction", None)
        scope = transaction() if callable(transaction) else contextlib.nullcontext()
        with scope:
            for sql, params in statements:
                port.execute(sql, params)
        return row

    conn = _conn(store)
    with lock_of(store):
        for sql, params in statements:
            conn.execute(sql, params)
        conn.commit()
    return row


def get_doc(store: Any, tenant_id: str, doc_id: str) -> dict | None:
    rows = query(store, "SELECT * FROM kb_doc WHERE tenant_id=? AND doc_id=?",
                 (tenant_id, doc_id))
    return rows[0] if rows else None


def list_docs(store: Any, tenant_id: str | None = None,
              kind: str | None = None) -> list[dict]:
    sql = "SELECT * FROM kb_doc"
    where, params = [], []
    if tenant_id:
        where.append("tenant_id=?")
        params.append(tenant_id)
    if kind:
        where.append("kind=?")
        params.append(kind)
    if where:
        sql += " WHERE " + " AND ".join(where)
    return query(store, sql + " ORDER BY created_at, doc_id", params)
