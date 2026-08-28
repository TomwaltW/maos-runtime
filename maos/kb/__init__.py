"""知识层（KB/RAG）—— 建表、读写口径、开关、取值域常量。

**为什么这里自带一层 SQL 访问器**：核心 store 是冻结面（表结构禁改，只许新增表），
而 `Store` 抽象基类只有 plan/task/artifact/event_log 那几个具名方法，没有通用 execute。
`kb_doc` 是新增表，只能从 `SqliteStore` 的连接上走 —— 与退款域 `objects.py` 同一套做法，
理由也一样：冻结面一个字不动，新增表由使用方自己建。

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
def _conn(store: Any) -> sqlite3.Connection:
    """取底层连接。只认暴露了 `_conn` 的 Store 实现（当前是 `SqliteStore`）。"""
    conn = getattr(store, "_conn", None)
    if conn is None:
        raise TypeError(
            f"{type(store).__name__} 没有暴露 sqlite 连接，知识层的新增表无处落库。"
            " 换后端时在这里加一条分支，不要去改冻结的核心 store。"
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
    conn = _conn(store)
    with lock_of(store):
        conn.execute(sql, tuple(params))
        conn.commit()


def query(store: Any, sql: str, params: tuple | list = ()) -> list[dict]:
    with lock_of(store):
        rows = _conn(store).execute(sql, tuple(params)).fetchall()
    return [dict(r) for r in rows]


def ensure_schema(store: Any) -> None:
    """读 schema.sql 建表。幂等（全部 IF NOT EXISTS），可连跑。

    检索与写入两侧都先调它：知识层的表不属于 `init_schema()` 的五表，
    谁先用到谁负责建，不指望调用方记得。
    """
    conn = _conn(store)
    with lock_of(store):
        conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
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
    conn = _conn(store)
    with lock_of(store):
        conn.execute(f"INSERT OR REPLACE INTO kb_doc ({cols}) VALUES ({marks})",
                     tuple(row[c] for c in DOC_COLUMNS))
        # 影子表没有主键，REPLACE 管不到它：不先删就会攒出同一 doc_id 的多份副本，
        # BM25 于是把同一条知识数进去好几次，排序被自己刷上去。
        conn.execute("DELETE FROM kb_doc_fts WHERE doc_id=? AND tenant_id=?",
                     (row["doc_id"], row["tenant_id"]))
        conn.execute(
            "INSERT INTO kb_doc_fts (doc_id, tenant_id, title, body) VALUES (?,?,?,?)",
            (row["doc_id"], row["tenant_id"], fts_text(row["title"]), fts_text(row["body"])))
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
