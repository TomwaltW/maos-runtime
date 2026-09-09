"""业务域共用的**案件存储骨架** —— 建表、迁移原语、SQL 薄壳、业务引用。

## 为什么会有这一层

`maos/core/store.py` 是冻结面（铁律 1），而 `Store` 抽象基类只有
plan/task/artifact/event_log 那几个具名方法，没有通用 execute。各业务域的新增表
只能从 `SqliteStore` 的连接上走，于是每个域的 `objects.py` 都自带一层
`execute()` / `query()` 薄壳，本域所有 SQL 从那里过 —— store.py 一个字不改。

四个域各写了一份之后，实测这层薄壳**逐字节同构**：`maos/domain/ap/objects.py`
与 `maos/domain/claim/objects.py` 的 `_guarded` 逐行 diff 只有 4 处，全部是
`ap_case` → `claim_case` 这类表名字符串，逻辑一字不差。**可参数化的重复，
不是领域差异** —— 所以下沉成这一份，由 `case_table` 现造。

## 骨架管什么，不管什么

管：写入拦截、连接/锁、`execute` / `query`、迁移用的两个原语
（`_atomic` / `_has_column`）、版本记账、建表、业务引用三件套。

**不管**：各域的迁移步骤表（`_MIGRATIONS`）与 `_migrate()` —— 那是各域自己的
历史，步骤各不相同，留在各域 `objects.py` 里；骨架只提供它要用的原语，再留一个
`migrate` 钩子把它接回 `ensure_schema()`。也不管域特有的读取口径
（ap 的 `po_lines` / `money`、claim 的 `terms_at_bind`、investigation 的
`put_payment_snapshot`）—— 那些才是域。

## 一条边界：这一层不是「域基类」

各域 `objects.py` 抬头那段「同构而不共用是有意的」讲的是**域与域之间不互相
import**，那条仍然成立：本模块不属于任何一个域，是四个域共同踩的地板，
不是某个域的父类。换第三个域只要多调一次 `make_case_store()`，
不必去动别的域。

`maos/domain/refund/objects.py` **本轮不接**这份骨架（跨轨契约 §4）：
它在同期另一轮里是只读面，改它两轮合并必冲突，而冲突点在地基上。
详见 `docs/BACKLOG.md` 的 `## task-T80`。
"""

from __future__ import annotations

import contextlib
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable


class BypassedGuardError(RuntimeError):
    """有人试图绕开 `guard.py` 直接写案件表。

    这是**基类**。每个域拿到的是它的一个子类（类名同样叫 `BypassedGuardError`），
    域与域之间互不相等 —— 一个域的守卫被绕开，不该能被另一个域的 `except` 接住。
    要一网打尽时才 catch 这个基类。
    """


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _case_write_pattern(case_table: str) -> re.Pattern[str]:
    """认 insert / update / delete / replace 四种写语句打向 `case_table`。

    表名两侧允许带引号或方括号（不同后端的引用风格）。
    `ALTER TABLE` **刻意不在拦截面上** —— 「给案件表加一列」是正常迁移，不是旁路写入。
    """
    return re.compile(
        r"\b(?:insert\s+(?:or\s+\w+\s+)?into|update|delete\s+from|replace\s+into)\s+"
        rf"[\"'`\[]?{re.escape(case_table)}[\"'`\]]?\b",
        re.IGNORECASE,
    )


class CaseStore:
    """一个域的存储口径。**不要直接构造，走 `make_case_store()`。**

    方法名与下沉前四份 `objects.py` 逐字对齐，域模块把它们原样绑成模块级名字，
    对外的调用形态（`objects.execute(store, sql, params)`）一个字不变。
    """

    def __init__(self, *, case_table: str, schema_sql: str) -> None:
        #: 域前缀由案件表名反推：`ap_case` -> `ap`。四个域的记账表、保存点、
        #: 业务引用表全都按这个前缀命名，所以 `case_table` 一个参数就够，
        #: 不必再让调用方把三个表名各报一遍（多报一次就多一次报错的机会）。
        self.case_table = case_table
        self.domain = case_table[: -len("_case")] if case_table.endswith("_case") else case_table
        self.schema_sql = schema_sql
        self.version_table = f"{self.domain}_schema_version"
        self.business_ref_table = f"{self.domain}_business_ref"
        #: 迁移那组「同生共死」语句用的保存点名。**带域前缀**：几个域的迁移嵌套跑时
        #: 保存点不能重名。
        self.savepoint = f"{self.domain}_schema_migrate"
        self._case_write = _case_write_pattern(case_table)

        #: 本域专属的异常类型，见 `BypassedGuardError` 的说明。
        self.BypassedGuardError: type[BypassedGuardError] = type(
            "BypassedGuardError",
            (BypassedGuardError,),
            {"__doc__": f"有人试图绕开 `guard.py` 直接写 `{case_table}`。",
             "__module__": f"maos.domain.{self.domain}.objects"},
        )

        #: 各域的 `_migrate(store, script)`，由域模块在定义完之后挂上来。
        #: 不进构造参数是因为它得先有 `_MIGRATIONS` 才写得出来，而那张表又要引用
        #: 本对象的 `_atomic` / `_has_column` —— 顺序上只能后挂。没挂就是没有迁移步骤。
        self.migrate: Callable[[Any, str], None] | None = None

        #: object_type -> (表名, 主键列名)，以及其中哪些表按版本取。
        #: 每个域自己的取值域，域模块构造完自行填；不填就是本域不做业务引用。
        self.ref_targets: dict[str, tuple[str, str]] = {}
        self.versioned_ref_tables: set[str] = set()

    # ------------------------------------------------------------ 连接与锁
    def _conn(self, store: Any) -> sqlite3.Connection:
        """取底层连接。只认暴露了 `_conn` 的 Store 实现（当前是 `SqliteStore`）。"""
        conn = getattr(store, "_conn", None)
        if conn is None:
            raise TypeError(
                f"{type(store).__name__} 没有暴露 sqlite 连接，"
                f"{self.case_table} 所在域的新增表无处落库。"
                " 换后端时在这里加一条分支，不要去改冻结的 store.py。"
            )
        return conn

    def lock_of(self, store: Any) -> Any:
        """借 Store 自己的锁。

        `SqliteStore` 的连接是**共享**的（`check_same_thread=False` + 一把 RLock）。
        业务域绕过 store.py 直接用这条连接，就必须一并用它那把锁：否则别的线程在
        `insert_task` 里一次 `commit()`，就把 guard 这边只写了回单/回执/观察、
        还没改状态的事务提交掉了 —— 「终态与回单同事务」当场破，而且是偶发的。
        """
        lock = getattr(store, "_lock", None)
        return lock if lock is not None else contextlib.nullcontext()

    # ------------------------------------------------------------ 写入拦截
    def _guarded(self, sql: str) -> str:
        """案件表的写入一律拒绝，报错**点名到具体表**。

        文案里那个表名不许换成「本域的案件表」之类的通用话：下沉之后一个进程里
        可能同时活着三四个域的骨架，看不出是哪张表，排查要绕远路。
        """
        if self._case_write.search(sql):
            raise self.BypassedGuardError(
                f"{self.case_table} 的写入必须走 guard.create_case / guard.update_biz_status，"
                "不许经 objects.execute 旁路（铁律 8）"
            )
        return sql

    def execute(self, store: Any, sql: str, params: tuple | list = ()) -> None:
        """本域的写入口径。对案件表的写入一律拒绝。"""
        conn = self._conn(store)
        with self.lock_of(store):
            conn.execute(self._guarded(sql), tuple(params))
            conn.commit()

    def query(self, store: Any, sql: str, params: tuple | list = ()) -> list[dict]:
        """本域的读取口径。读不设限 —— 守的是写入方，不是读取方。"""
        with self.lock_of(store):
            rows = self._conn(store).execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ 迁移原语
    def _atomic(self, store: Any, statements: list[tuple[str, tuple]]) -> None:
        """一组语句同生共死，**DDL 也算在内**。

        迁移非用它不可：像「删表 → 建表 → 重灌」这种三步走，断在中间而前两步已落盘的
        话，表在、列全、**一行数据都没有** —— 下一次跑迁移的探针看到列已存在于是跳过，
        那张表从此恒空且不报错。

        **光靠 `rollback()` 撤不回 DDL**：Python 的 sqlite3 在传统模式下只为 DML 隐式开
        事务，`DROP TABLE` 是在自动提交下跑的，一发就落盘。显式发一句 `SAVEPOINT` 才能
        把 DDL 拉进事务。用 `SAVEPOINT` 而不是 `BEGIN`：外层已经在事务里时 `BEGIN` 会报
        cannot start a transaction within a transaction。

        **每条语句照样过 `_guarded()`**（铁律 8）。迁移直连底层连接是为了拿事务，
        不是为了拿豁免权。
        """
        for sql, _params in statements:
            self._guarded(sql)
        conn = self._conn(store)
        with self.lock_of(store):
            conn.execute(f"SAVEPOINT {self.savepoint}")
            try:
                for sql, params in statements:
                    conn.execute(sql, tuple(params))
            except BaseException:
                conn.execute(f"ROLLBACK TO {self.savepoint}")
                conn.execute(f"RELEASE {self.savepoint}")
                conn.rollback()
                raise
            conn.execute(f"RELEASE {self.savepoint}")
            conn.commit()

    def _has_column(self, store: Any, table: str, column: str) -> bool:
        """探「这张表有没有这一列」。用一条 SELECT，**不用 PRAGMA**。

        两条理由，第二条是坑：

        · PRAGMA 是 SQLite 方言，换后端时不保证有（PG 那边没有）。
        · `PRAGMA table_info` **不列生成列** —— 生成列要 `table_xinfo` 才看得到。
          拿 table_info 判一个生成列在不在，新库上会答「不在」，于是每次都去 ALTER
          一次，每次都撞 duplicate column name。

        表名列名都是迁移步骤里的字面量，不是外来输入，所以直接拼进 SQL。
        异常一律当「没这列」：真是连接坏了，紧随其后的 ALTER 会自己响。
        """
        try:
            self.query(store, f"SELECT {column} FROM {table} LIMIT 1")
            return True
        except Exception:                              # noqa: BLE001 —— 探针不该炸
            return False

    def applied_schema_version(self, store: Any) -> int:
        """这库已经升到第几版。没有记账行就是 0。"""
        rows = self.query(store, f"SELECT MAX(version) AS version FROM {self.version_table}")
        version = rows[0]["version"] if rows else None
        return int(version) if version is not None else 0

    # ---------------------------------------------------------------- 建表
    def ensure_schema(self, store: Any) -> None:
        """建表 + 迁移到最新版本。幂等，可连跑。

        **两段，缺一不可**：`schema.sql` 那段全是 `IF NOT EXISTS`，只管「表不在就建」，
        对已经存在的表一个字都改不动；`migrate` 那段才管「表在但形状旧」。
        只有第一段的时候，改列是静默无效的。
        """
        conn = self._conn(store)
        with self.lock_of(store):
            conn.executescript(self.schema_sql)
            conn.commit()
        if self.migrate is not None:
            self.migrate(store, self.schema_sql)

    # ------------------------------------------------------ DAG -> 业务对象
    def attach_business_ref(
        self,
        store: Any,
        *,
        plan_id: str,
        task_id: str,
        tenant_id: str,
        object_type: str,
        object_id: str,
        object_version: int = 0,
        purpose: str = "",
    ) -> dict:
        """把一个 Task 挂到一个业务对象上 —— **只存引用，不存副本**。

        存副本会立刻产生第二份事实：业务对象改了，Task 里那份不会跟着改，
        而下游分不清哪份是真的。引用只指路，读的时候一定读到当前那一份。
        """
        row = {
            "plan_id": plan_id, "task_id": task_id, "tenant_id": tenant_id,
            "object_type": object_type, "object_id": object_id,
            "object_version": int(object_version), "purpose": purpose,
            "created_at": _now(),
        }
        self.execute(
            store,
            f"INSERT OR REPLACE INTO {self.business_ref_table} (plan_id, task_id, tenant_id,"
            " object_type, object_id, object_version, purpose, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (row["plan_id"], row["task_id"], row["tenant_id"], row["object_type"],
             row["object_id"], row["object_version"], row["purpose"], row["created_at"]),
        )
        return row

    def list_business_refs(self, store: Any, *, plan_id: str,
                           task_id: str | None = None) -> list[dict]:
        if task_id is None:
            return self.query(store, f"SELECT * FROM {self.business_ref_table} WHERE plan_id=?"
                                     " ORDER BY task_id, object_type, object_id", (plan_id,))
        return self.query(store, f"SELECT * FROM {self.business_ref_table} WHERE plan_id=?"
                                 " AND task_id=? ORDER BY object_type, object_id",
                          (plan_id, task_id))

    def resolve_business_ref(self, store: Any, ref: dict) -> dict | None:
        """按引用取回被指对象；指不到（对象不存在或版本对不上）返回 None。

        业务引用表**不带外键** —— 它跨的是「编排层对象」与「业务对象」两个世界，
        完整性靠这个函数在读的时候查，不靠数据库替我们保证。
        """
        table, key = self.ref_targets.get(ref["object_type"], (None, None))
        if table is None:
            return None
        sql = f"SELECT * FROM {table} WHERE tenant_id=? AND {key}=?"
        params: list[Any] = [ref["tenant_id"], ref["object_id"]]
        if table in self.versioned_ref_tables:
            sql += " AND version=?"
            params.append(ref["object_version"])
        rows = self.query(store, sql, params)
        return rows[0] if rows else None


def make_case_store(*, case_table: str, schema_sql: str) -> CaseStore:
    """造一个域的存储口径。

    `case_table` 是本域那张受守卫保护的案件表（`ap_case` / `claim_case` / …），
    记账表、保存点、业务引用表的名字都从它反推。`schema_sql` 是本域 `schema.sql`
    的原文 —— 传原文不传路径，是因为 `ensure_schema()` 与各域 `_migrate()` 的步骤
    函数拿到的必须是**同一份**脚本文本，路径传下去就得各读各的。

    造完之后域模块还要挂三样（都可以不挂，不挂就是本域没有）：

    · `store.migrate = _migrate` —— 本域的迁移入口
    · `store.ref_targets` / `store.versioned_ref_tables` —— 本域的业务引用取值域
    """
    return CaseStore(case_table=case_table, schema_sql=schema_sql)
