"""存储层 —— 表结构对齐方案书附录 B 的 PolarDB DDL。

现在用 SQLite 是为了不引中间件就能跑通。换 PolarDB 时只改这一个文件：
Store 是抽象基类，PolarStore 照着实现同样的方法即可，上层零改动。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store(ABC):
    """Control Plane 唯一的数据出口。Agent 永远不直接碰这一层。"""

    @abstractmethod
    def init_schema(self) -> None: ...

    @abstractmethod
    def insert_plan(self, plan: dict) -> None: ...

    @abstractmethod
    def get_plan(self, plan_id: str) -> dict | None: ...

    @abstractmethod
    def update_plan_state(self, plan_id: str, state: str) -> None: ...

    @abstractmethod
    def insert_task(self, task: dict) -> None: ...

    @abstractmethod
    def get_task(self, task_id: str) -> dict | None: ...

    @abstractmethod
    def list_tasks(self, plan_id: str) -> list[dict]: ...

    @abstractmethod
    def update_task(self, task_id: str, **fields: Any) -> None: ...

    @abstractmethod
    def insert_artifact(self, artifact: dict) -> None: ...

    @abstractmethod
    def list_artifacts(self, task_id: str) -> list[dict]: ...

    @abstractmethod
    def append_event_log(self, row: dict) -> None: ...

    @abstractmethod
    def list_event_log(self, plan_id: str) -> list[dict]: ...

    @abstractmethod
    def claim_idempotency(self, key: str, op: str, task_id: str) -> dict | None:
        """幂等闸门。

        首次见到 key -> 写入并返回 None（表示"你可以继续处理"）。
        已见过 key   -> 返回上次记录（表示"这是重复投递，别再改状态"）。
        """

    @abstractmethod
    def finish_idempotency(self, key: str, outcome: dict) -> None: ...

    # -- 知识库（Phase 4 新增表；既有五表不受影响） --------------------------
    @abstractmethod
    def insert_knowledge(self, row: dict) -> None: ...

    @abstractmethod
    def list_knowledge(self, *, tags: list[str] | None = None,
                       keyword: str | None = None) -> list[dict]: ...


class SqliteStore(Store):
    def __init__(self, path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()

    # -- schema -----------------------------------------------------------
    def init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS plan (
                    plan_id     TEXT PRIMARY KEY,
                    trace_id    TEXT NOT NULL,
                    goal        TEXT NOT NULL,
                    state       TEXT NOT NULL,
                    created_at  TEXT NOT NULL,
                    updated_at  TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS task (
                    task_id      TEXT PRIMARY KEY,
                    plan_id      TEXT NOT NULL,
                    trace_id     TEXT NOT NULL,
                    role         TEXT NOT NULL,
                    title        TEXT NOT NULL,
                    state        TEXT NOT NULL,
                    attempt      INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    risk_level   TEXT NOT NULL DEFAULT 'L',
                    effect_risk  TEXT NOT NULL DEFAULT 'L',
                    depends_on   TEXT NOT NULL DEFAULT '[]',
                    inputs       TEXT NOT NULL DEFAULT '{}',
                    acceptance   TEXT NOT NULL DEFAULT '[]',
                    findings     TEXT NOT NULL DEFAULT '[]',
                    worker_id    TEXT,
                    last_error   TEXT,
                    created_at   TEXT NOT NULL,
                    updated_at   TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_task_plan_state ON task(plan_id, state);

                CREATE TABLE IF NOT EXISTS artifact (
                    artifact_id TEXT PRIMARY KEY,
                    task_id     TEXT NOT NULL,
                    plan_id     TEXT NOT NULL,
                    kind        TEXT NOT NULL,
                    version     INTEGER NOT NULL DEFAULT 1,
                    content     TEXT NOT NULL,
                    created_at  TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_artifact_task ON artifact(task_id);

                CREATE TABLE IF NOT EXISTS event_log (
                    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id    TEXT NOT NULL,
                    trace_id    TEXT NOT NULL,
                    plan_id     TEXT NOT NULL,
                    task_id     TEXT,
                    event_type  TEXT NOT NULL,
                    from_state  TEXT,
                    to_state    TEXT,
                    reason      TEXT,
                    detail      TEXT NOT NULL DEFAULT '{}',
                    created_at  TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_log_plan ON event_log(plan_id, seq);

                CREATE TABLE IF NOT EXISTS processed_key (
                    idempotency_key TEXT PRIMARY KEY,
                    op              TEXT NOT NULL,
                    task_id         TEXT NOT NULL,
                    outcome         TEXT NOT NULL DEFAULT '{}',
                    created_at      TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS knowledge (
                    id          TEXT PRIMARY KEY,
                    plan_id     TEXT NOT NULL,
                    kind        TEXT NOT NULL,
                    title       TEXT NOT NULL,
                    body        TEXT NOT NULL,
                    tags        TEXT NOT NULL DEFAULT '[]',
                    created_at  TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_knowledge_plan ON knowledge(plan_id);
                """
            )
            self._conn.commit()

    # -- plan -------------------------------------------------------------
    def insert_plan(self, plan: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO plan (plan_id, trace_id, goal, state, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?)",
                (plan["plan_id"], plan["trace_id"], plan["goal"], plan["state"], _now(), _now()),
            )
            self._conn.commit()

    def get_plan(self, plan_id: str) -> dict | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM plan WHERE plan_id=?", (plan_id,)).fetchone()
            return dict(r) if r else None

    def update_plan_state(self, plan_id: str, state: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE plan SET state=?, updated_at=? WHERE plan_id=?", (state, _now(), plan_id)
            )
            self._conn.commit()

    # -- task -------------------------------------------------------------
    _JSON_TASK_FIELDS = ("depends_on", "inputs", "acceptance", "findings")

    def insert_task(self, task: dict) -> None:
        row = dict(task)
        for f in self._JSON_TASK_FIELDS:
            row[f] = json.dumps(row.get(f, [] if f != "inputs" else {}), ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                "INSERT INTO task (task_id, plan_id, trace_id, role, title, state, attempt,"
                " max_attempts, risk_level, effect_risk, depends_on, inputs, acceptance,"
                " findings, worker_id, last_error, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row["task_id"], row["plan_id"], row["trace_id"], row["role"], row["title"],
                    row["state"], row.get("attempt", 0), row.get("max_attempts", 3),
                    row.get("risk_level", "L"), row.get("effect_risk", "L"),
                    row["depends_on"], row["inputs"],
                    row["acceptance"], row["findings"], row.get("worker_id"),
                    row.get("last_error"), _now(), _now(),
                ),
            )
            self._conn.commit()

    def _decode_task(self, r: sqlite3.Row) -> dict:
        d = dict(r)
        for f in self._JSON_TASK_FIELDS:
            d[f] = json.loads(d[f])
        return d

    def get_task(self, task_id: str) -> dict | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM task WHERE task_id=?", (task_id,)).fetchone()
            return self._decode_task(r) if r else None

    def list_tasks(self, plan_id: str) -> list[dict]:
        with self._lock:
            rs = self._conn.execute(
                "SELECT * FROM task WHERE plan_id=? ORDER BY created_at", (plan_id,)
            ).fetchall()
            return [self._decode_task(r) for r in rs]

    def update_task(self, task_id: str, **fields: Any) -> None:
        if not fields:
            return
        vals = {}
        for k, v in fields.items():
            vals[k] = json.dumps(v, ensure_ascii=False) if k in self._JSON_TASK_FIELDS else v
        cols = ", ".join(f"{k}=?" for k in vals)
        with self._lock:
            self._conn.execute(
                f"UPDATE task SET {cols}, updated_at=? WHERE task_id=?",
                (*vals.values(), _now(), task_id),
            )
            self._conn.commit()

    # -- artifact ---------------------------------------------------------
    def insert_artifact(self, artifact: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO artifact (artifact_id, task_id, plan_id, kind, version, content,"
                " created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    artifact["artifact_id"], artifact["task_id"], artifact["plan_id"],
                    artifact["kind"], artifact.get("version", 1),
                    json.dumps(artifact["content"], ensure_ascii=False), _now(),
                ),
            )
            self._conn.commit()

    def list_artifacts(self, task_id: str) -> list[dict]:
        with self._lock:
            rs = self._conn.execute(
                "SELECT * FROM artifact WHERE task_id=? ORDER BY version", (task_id,)
            ).fetchall()
            out = []
            for r in rs:
                d = dict(r)
                d["content"] = json.loads(d["content"])
                out.append(d)
            return out

    # -- event log --------------------------------------------------------
    def append_event_log(self, row: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO event_log (event_id, trace_id, plan_id, task_id, event_type,"
                " from_state, to_state, reason, detail, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    row.get("event_id", ""), row.get("trace_id", ""), row["plan_id"],
                    row.get("task_id"), row["event_type"], row.get("from_state"),
                    row.get("to_state"), row.get("reason"),
                    json.dumps(row.get("detail", {}), ensure_ascii=False), _now(),
                ),
            )
            self._conn.commit()

    def list_event_log(self, plan_id: str) -> list[dict]:
        with self._lock:
            rs = self._conn.execute(
                "SELECT * FROM event_log WHERE plan_id=? ORDER BY seq", (plan_id,)
            ).fetchall()
            out = []
            for r in rs:
                d = dict(r)
                d["detail"] = json.loads(d["detail"])
                out.append(d)
            return out

    # -- 幂等 --------------------------------------------------------------
    def claim_idempotency(self, key: str, op: str, task_id: str) -> dict | None:
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO processed_key (idempotency_key, op, task_id, outcome, created_at)"
                    " VALUES (?,?,?,?,?)",
                    (key, op, task_id, "{}", _now()),
                )
                self._conn.commit()
                return None  # 首次，放行
            except sqlite3.IntegrityError:
                r = self._conn.execute(
                    "SELECT * FROM processed_key WHERE idempotency_key=?", (key,)
                ).fetchone()
                d = dict(r)
                d["outcome"] = json.loads(d["outcome"])
                return d  # 重复投递

    def finish_idempotency(self, key: str, outcome: dict) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE processed_key SET outcome=? WHERE idempotency_key=?",
                (json.dumps(outcome, ensure_ascii=False), key),
            )
            self._conn.commit()

    # -- 知识库 -------------------------------------------------------------
    def insert_knowledge(self, row: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO knowledge (id, plan_id, kind, title, body, tags, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    row["id"], row["plan_id"], row["kind"], row["title"], row["body"],
                    json.dumps(row.get("tags", []), ensure_ascii=False), _now(),
                ),
            )
            self._conn.commit()

    def list_knowledge(self, *, tags: list[str] | None = None,
                       keyword: str | None = None) -> list[dict]:
        """keyword 走 SQL 的 LIKE（title 或 body）；tags 取交集，在 Python 侧过滤。

        tags 存的是 JSON 数组，SQLite 这层没有数组类型可查 —— 换 PolarDB 时
        这一段应改成真正的标签表 join，上层调用签名不变。
        """
        sql = "SELECT * FROM knowledge"
        params: list[Any] = []
        if keyword:
            sql += " WHERE (title LIKE ? OR body LIKE ?)"
            like = f"%{keyword}%"
            params += [like, like]
        sql += " ORDER BY created_at"
        with self._lock:
            rs = self._conn.execute(sql, params).fetchall()
        out = []
        for r in rs:
            d = dict(r)
            d["tags"] = json.loads(d["tags"])
            out.append(d)
        if tags:
            want = set(tags)
            out = [d for d in out if want & set(d["tags"])]
        return out
