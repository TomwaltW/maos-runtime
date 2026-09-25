"""客服前台（cs）会话对象的读写口径 —— 建表、SQL 薄壳、迁移记账、事务。

照 ``maos/domain/refund/objects.py`` 的写法（跨轨契约 §1.4 T167）：

* ``maos/core/store.py`` 是冻结面，``Store`` 抽象基类没有通用 execute；本域的
  业务表（p12 三张、p13 五张）+ 一张迁移记账表是**新增表**（铁律 1 只许新增），SQL 全部从这里的
  ``execute()`` / ``query()`` 两个薄壳过，store.py 一个字不改。
* 连接走 ``_dbport.DomainConn``：缺省 sqlite（同一条 ``store._conn``、同一把
  ``store._lock``），``MAOS_DOMAIN_BACKEND=postgres`` 时走 PG 且**不回落**。
  ``schema.sql`` 只有一份 SQLite 方言，PG 侧由 ``to_pg_ddl`` 现翻。
* **不进** ``maos.domain.DOMAIN_REGISTRY``（理由见本包 ``__init__`` 的 docstring）。

与退款域的口径**相同但互不 import**：跨轨契约 §2.3 只许 cs 包 import
``_dbport`` / ``_schema_util`` / ``refund.projection`` 三处，``refund.objects`` 不在其列。
本域没有「只许守卫写」的权威表（会话进度是会话对象自己的字段，不是外部权威事实），
所以 ``execute()`` 不设写入拦截 —— 这一点与退款域不同，是有意的。
"""

from __future__ import annotations

import contextlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from maos.domain._dbport import POSTGRES, DomainConn, strip_sql_comments
from maos.domain.cs.types import HANDOFF_REASONS, ROUTES

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")

#: 迁移那组「同生共死」语句用的保存点名。带域前缀：别的域的迁移嵌套跑时不能重名。
_MIGRATE_SAVEPOINT = "cs_schema_migrate"

#: 会话写入（取号、落轮次、改阶段、落卡片）用的保存点名。
_TXN_SAVEPOINT = "cs_txn"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _domain(store: Any) -> DomainConn:
    """本域这一次调用用哪条连接 —— 由 store 上的装配标记或 ``MAOS_DOMAIN_BACKEND`` 决定。"""
    return DomainConn.open(store)


def execute(store: Any, sql: str, params: tuple | list = ()) -> None:
    """本域的写入口径：一条语句一次提交。"""
    _domain(store).execute(sql, tuple(params))


def query(store: Any, sql: str, params: tuple | list = ()) -> list[dict]:
    """本域的读取口径。"""
    return _domain(store).query(sql, tuple(params))


@contextlib.contextmanager
def transaction(store: Any) -> Iterator[DomainConn]:
    """一组语句同生共死：块内全部成功才提交，任何一句抛就整组回滚。

    **块内只许用 yield 出来的那个 ``DomainConn``**，不许再调本模块的
    ``execute()`` / ``query()``：那两个每次 ``DomainConn.open`` 一个新实例，
    sqlite 分支的事务深度记在实例上，新实例不知道自己在事务里，``execute`` 完
    就 ``commit()`` —— 保存点被提前提交，后面的 ``RELEASE`` 报 no such savepoint。

    整个块持有连接那把锁（sqlite 是 ``store._lock``，PG 是 ``_dbport`` 的进程锁），
    所以「读 → 算 → 写」在块内对同进程的其他线程是原子的（``allocate_turn`` 靠这条）。
    """
    conn = _domain(store)
    with conn.atomic(_TXN_SAVEPOINT):
        yield conn


# ------------------------------------------------------------------ schema 迁移
#: 迁移步骤表，**按版本号升序**，每项是 ``(版本号, 说明, 步骤函数)``，
#: 步骤函数签名 ``step(store, script)``（``script`` 是 schema.sql 原文）。
#:
#: **当前是空的，这是对的**：本域刚落地，一列都没改过（口径同 rtv / ap 域）。
#:
#: 加一步就在末尾追加一条，**不要改已有的那几条** —— 已经跑过的库不会再跑一遍，
#: 改了等于新老库形状分叉。每一步都必须**自带探针**、在已是目标形状的库上是 no-op：
#: 新库刚建完记账表同样是空的，只看版本号会把新库也当老库去 ALTER。
_MIGRATIONS: tuple[tuple[int, str, Any], ...] = ()


def _target_version() -> int:
    """跟着 ``_MIGRATIONS`` 算，**不手写**（手写的迟早和实际步骤对不上）。"""
    return max((v for v, _label, _step in _MIGRATIONS), default=0)


#: 本域 schema 的当前版本。
CS_SCHEMA_VERSION = _target_version()


def applied_schema_version(store: Any) -> int:
    """这库已经升到第几版。没有记账行就是 0。"""
    rows = query(store, "SELECT MAX(version) AS version FROM cs_schema_version")
    version = rows[0]["version"] if rows else None
    return int(version) if version is not None else 0


def _migrate(store: Any, script: str) -> None:
    """把库升到目标版本，并逐条记账到 ``cs_schema_version``。

    版本号是**快路径**不是判据：已经记到最新就直接返回。真正决定做不做的是每一步
    自己的探针。记账写在步骤之后：中途失败就不记，下次重跑同一步（步骤幂等）。
    """
    applied = applied_schema_version(store)
    if applied >= _target_version():
        return
    for version, _label, step in _MIGRATIONS:
        if version <= applied:
            continue
        step(store, script)
        execute(store, "INSERT INTO cs_schema_version (version, applied_at) VALUES (?, ?)",
                (version, _now()))


# ------------------------------------------------------------------ 旧库探针（p13）
class CsSchemaOutdated(RuntimeError):
    """库里**已存在**的 cs_ 表的 CHECK 不认当前枚举的取值 —— 这是 p13 之前建的库，要重建。

    p13 长了枚举（ROUTES 加 clarify、HANDOFF_REASONS 加四个），而 ``schema.sql`` 整份
    ``IF NOT EXISTS``：已存在的表改不动一列，CHECK 还是旧的。不拦的话，第一次落 clarify
    轮或新原因的卡片时才撞 ``CHECK constraint failed``，而且是在客户说话的那一刻。
    CHECK 的增长**没有迁移**（p13 契约 §1.1：cs_ 表之前没有落盘的库），所以只报、不修。
    """


#: 探针要求的「列 -> 必须认的取值」。取 types.py 的全集（含 p13 增量），不手抄新增的那五个：
#: 以后枚举再长，没重建的库照样在这里被认出来。
_ENUM_PROBES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("cs_turn", "route", ROUTES),
    ("cs_turn", "handoff_reason", ("",) + HANDOFF_REASONS),
    ("cs_handoff", "reason", HANDOFF_REASONS),
)

#: PG 侧读一张表的全部 CHECK 约束定义（``pg_get_constraintdef`` 给的是规范化后的文本，
#: 形如 ``CHECK ((route = ANY (ARRAY['answer'::text, …])))``）。只读目录，不试插。
#: 占位符写 SQLite 方言的 ``?``：``_PgConnection.execute`` 会翻成 ``%s``。
_PG_CHECKS_SQL = (
    "SELECT pg_get_constraintdef(c.oid) AS body"
    " FROM pg_constraint c"
    " JOIN pg_class t ON t.oid = c.conrelid"
    " JOIN pg_namespace n ON n.oid = t.relnamespace"
    " WHERE n.nspname = current_schema() AND t.relname = ? AND c.contype = 'c'"
)

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_LITERAL = re.compile(r"'((?:[^']|'')*)'")


def _table_check_text(conn: Any, table: str) -> str | None:
    """一张已存在的表的 CHECK 所在文本；表不存在返回 None。

    SQLite 读 ``sqlite_master.sql``（建表原文）；PG 读 ``pg_constraint``（等价元数据）。
    两边都是只读 —— 新库上探针不产生任何写。
    """
    if conn.dialect == POSTGRES:
        rows = conn.query(_PG_CHECKS_SQL, (table,))
        if not rows:
            return None
        return "\n".join(str(r["body"]) for r in rows)
    rows = conn.query("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,))
    if not rows or rows[0]["sql"] is None:
        return None
    return str(rows[0]["sql"])


def _balanced_group(text: str, start: int) -> tuple[str, int] | None:
    """从 ``start`` 起找第一个 ``(``，返回 ``(括号内的原文, 右括号之后的下标)``；引号内的括号不算。"""
    i, n = start, len(text)
    while i < n and text[i] != "(":
        if not text[i].isspace():
            return None
        i += 1
    if i >= n:
        return None
    depth, quoted, begin = 0, False, i + 1
    while i < n:
        ch = text[i]
        if quoted:
            if ch == "'":
                if i + 1 < n and text[i + 1] == "'":
                    i += 1
                else:
                    quoted = False
        elif ch == "'":
            quoted = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[begin:i], i + 1
        i += 1
    return None


def _check_literals(text: str) -> dict[str, frozenset[str]]:
    """把建表文本 / 约束定义里的每个 ``CHECK (...)`` 拆成「列名 -> 认的字符串取值」。

    两种方言同一套拆法：括号体里第一个标识符是列名（SQLite ``route IN ('a', …)``、
    PG ``((route = ANY (ARRAY['a'::text, …])))``），体内的单引号串就是认的取值。
    同一列出现多次就取并集。注释先剥掉：注释里的 CHECK 字样不算。
    """
    body_text = strip_sql_comments(text)
    out: dict[str, set[str]] = {}
    for m in re.finditer(r"\bCHECK\b", body_text, re.IGNORECASE):
        group = _balanced_group(body_text, m.end())
        if group is None:
            continue
        body = group[0]
        ident = _IDENT.search(body)
        if ident is None:
            continue
        values = {v.replace("''", "'") for v in _LITERAL.findall(body)}
        out.setdefault(ident.group(0), set()).update(values)
    return {col: frozenset(vals) for col, vals in out.items()}


def _probe_enum_checks(conn: Any) -> None:
    """已存在的 cs_turn / cs_handoff 的 CHECK 必须认当前枚举全集，否则抛 ``CsSchemaOutdated``。

    表不存在（新库）直接过；列上没有 CHECK 也过（不限取值就不会撞）。
    """
    texts: dict[str, dict[str, frozenset[str]] | None] = {}
    problems: list[str] = []
    for table, column, enum in _ENUM_PROBES:
        if table not in texts:
            raw = _table_check_text(conn, table)
            texts[table] = None if raw is None else _check_literals(raw)
        checks = texts[table]
        if checks is None or column not in checks:
            continue
        missing = [v for v in enum if v not in checks[column]]
        if missing:
            problems.append(f"{table}.{column} 的 CHECK 不认 {missing}")
    if problems:
        raise CsSchemaOutdated(
            "；".join(problems)
            + "。这是 p13 之前建的库（cs_ 表的 CHECK 是旧枚举），CHECK 的增长没有迁移，"
              "要重建：备份后删掉库里 cs_ 前缀的表（或换一个新库文件）再启动。"
              "不重建的话，clarify 轮与新转人工原因一写就撞 CHECK。")


def ensure_schema(store: Any) -> None:
    """建表 + 迁移到最新版本。幂等，可连跑；会话层每个入口先调它。

    两段缺一不可：``schema.sql`` 全是 ``IF NOT EXISTS``，只管「表不在就建」；
    ``_migrate()`` 才管「表在但形状旧」。

    p13 起在**建表之前**先过一道旧库探针（``_probe_enum_checks``）：已存在的 cs_turn /
    cs_handoff 的 CHECK 不认当前枚举就抛 ``CsSchemaOutdated``。放在建表之前，是为了
    旧库上一张新表都不建、一个字节都不写就停下；新库上探针只读，不多做任何动作。
    """
    script = _SCHEMA_PATH.read_text(encoding="utf-8")
    conn = _domain(store)
    _probe_enum_checks(conn)
    conn.executescript(script)
    _migrate(store, script)
