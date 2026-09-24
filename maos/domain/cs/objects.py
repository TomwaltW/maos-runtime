"""客服前台（cs）会话对象的读写口径 —— 建表、SQL 薄壳、迁移记账、事务。

照 ``maos/domain/refund/objects.py`` 的写法（跨轨契约 §1.4 T167）：

* ``maos/core/store.py`` 是冻结面，``Store`` 抽象基类没有通用 execute；本域的
  三张业务表 + 一张迁移记账表是**新增表**（铁律 1 只许新增），SQL 全部从这里的
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from maos.domain._dbport import DomainConn

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


def ensure_schema(store: Any) -> None:
    """建表 + 迁移到最新版本。幂等，可连跑；会话层每个入口先调它。

    两段缺一不可：``schema.sql`` 全是 ``IF NOT EXISTS``，只管「表不在就建」；
    ``_migrate()`` 才管「表在但形状旧」。
    """
    script = _SCHEMA_PATH.read_text(encoding="utf-8")
    _domain(store).executescript(script)
    _migrate(store, script)
