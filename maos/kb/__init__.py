"""知识层（KB/RAG）—— 建表、读写口径、开关、取值域常量。

**为什么这里自带一层 SQL 访问器**：核心 store 是冻结面（表结构禁改，只许新增表），
而 `Store` 抽象基类只有 plan/task/artifact/event_log 那几个具名方法，没有通用 execute。
`kb_doc` 是新增表，只能从 `SqliteStore` 的连接上走 —— 与退款域 `objects.py` 同一套做法，
理由也一样：冻结面一个字不动，新增表由使用方自己建。

**两条底层路径**：知识层既要能吃核心 `Store`（有 `_conn`），也要能吃 StorePort
（F-2 的 `execute` / `query`）—— 后者是「RAG 真跑在 PolarDB 上」的前提，
不接上的话检索器里那条端口分支在真链路上一次都走不到。判据集中在 `port_of()`
一个函数里，**不散在各调用点**：散开的后果是某几处走了新路、某几处还走老路，
而两边都不报错。口径是「`_conn` 优先」—— 现在能用的对象一律走原路径，
缺省行为一个字节不变；新分支只接此前必然抛 TypeError 的那类对象。

**依赖方向**：本模块**不** import `maos.kb.retriever`，也不 import `maos.skills` /
`maos.agents` 任何一层。retriever 与 guardrails 反过来 import 本模块 ——
`__init__` 一旦回 import 子模块，`skills/builtin/kb_retrieve.py` 那条 import 链就成环。
使用方写 `from maos.kb.retriever import retrieve`，不要指望从包顶层拿到它。

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

#: `kb_doc.id` 的拼接分隔符。与 schema.sql 里那条生成列表达式是同一份口径。
DOC_ROW_ID_SEP = ":"

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
    """
    raw = (env if env is not None else os.environ).get(KB_ENABLED_ENV)
    if raw is None:
        return True
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


# ---------------------------------------------------------------- 底层连接
def port_of(store: Any) -> Any | None:
    """「这个 store 该走 StorePort 还是走 `_conn`」的**唯一**判据。返回端口或 None。

    口径是 **`_conn` 优先**，不是「谁的方法多听谁的」：

    · 暴露了 `_conn` 的对象一律返回 None，走下面的老路径 —— 核心 `SqliteStore`
      以及测试里那些继承它的 store 全在此列，缺省行为因此一个字节都不变。
    · 只有「没有 `_conn`、却有 `execute` + `query`」的对象才认作 StorePort。
      这类对象在本函数出现之前一律撞 `_conn()` 的 TypeError，所以新分支是
      **严格增量**：以前跑得通的没有一条改道，以前跑不通的现在跑得通。

    只探 `execute` / `query` 两个方法，不探满 F-2 五个：知识层的 SQL 访问器只用
    这两个，`fts_search` / `vector_search` 由检索器自己按能力探测（那两条通道
    走不通要退化，而 execute/query 走不通没有退化路径可言）。
    """
    if store is None or getattr(store, "_conn", None) is not None:
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


def ensure_schema(store: Any) -> None:
    """读 schema.sql 建表。幂等（全部 IF NOT EXISTS），可连跑。

    检索与写入两侧都先调它：知识层的表不属于 `init_schema()` 的五表，
    谁先用到谁负责建，不指望调用方记得。
    """
    script = _SCHEMA_PATH.read_text(encoding="utf-8")
    port = port_of(store)
    if port is not None:
        for statement in _schema_statements(script):
            port.execute(statement, ())
        return
    conn = _conn(store)
    with lock_of(store):
        conn.executescript(script)
        conn.commit()


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
