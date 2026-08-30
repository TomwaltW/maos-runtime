"""退款域 schema 迁移机制（T26）。

**买的是什么**：`schema.sql` 整份都是 `CREATE TABLE IF NOT EXISTS`，它只描述目标
形状，搬不动已经存在的库。加表没事（新表直接生效），**改列不行** —— 往已建好的表
加一列，`IF NOT EXISTS` 直接跳过，跑起来一切正常，直到某条 INSERT 报
`no such column`。出处 `docs/BACKLOG.md` 的 `## task-T17` 第 2 条，原话
「建议在往退款域加第一列之前做，不然那次加列会先静默失效一轮」。

所以 T26 **只装机制，一列都不改**：`_MIGRATIONS` 现在是空元组，
`REFUND_SCHEMA_VERSION` 因此是 0。空的迁移表不代表机制没用 —— 版本表、记账口径、
探针契约、`_atomic` 的 SAVEPOINT 语义都得在**第一次加列之前**就位并且被钉住，
否则那次加列会带着一个没人验证过的搬运路径上线。

下面前三条是派单点名的三条（新库 / 老库 / 幂等）。后四条钉的是**机制语义**：
`_MIGRATIONS` 空着的时候，前三条其实只证明了「版本表建出来了」，证明不了
「往里放一条步骤它会正确地跑」。第 4、5 条因此注入一条**只活在测试里**的假步骤，
把「探针优先于版本号」「跑过就不再跑」钉死；第 6 条钉 DDL 也能整块回滚；
第 7 条钉铁律 8 不因为迁移而破例。
"""

from __future__ import annotations

import re
import sqlite3

import pytest

from maos.core.store import SqliteStore
from maos.domain.refund import objects

#: R-1 定下的业务表张数。**硬编码是刻意的**：从 `schema.sql` 现取一份期望集合的话，
#: 有人删掉一张表时期望值会跟着缩水，这条断言就永远绿。数字写死才抓得住。
_BUSINESS_TABLE_COUNT = 14

#: 剥掉记账表建表语句用的模式 —— 用来造「T26 之前那种库」。
_VERSION_TABLE_DDL = re.compile(
    r"CREATE TABLE IF NOT EXISTS refund_schema_version\s*\(.*?\);",
    re.DOTALL | re.IGNORECASE,
)


def _schema_text() -> str:
    return objects._SCHEMA_PATH.read_text(encoding="utf-8")


def _declared_tables(script: str) -> set[str]:
    """从 schema.sql 现取表名，不在这里另抄一份清单。"""
    return set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", script, re.IGNORECASE))


def _tables_in(store: SqliteStore) -> set[str]:
    rows = objects.query(
        store, "SELECT name FROM sqlite_master WHERE type='table'")
    return {r["name"] for r in rows}


def _fresh_store() -> SqliteStore:
    store = SqliteStore()
    store.init_schema()
    return store


def _legacy_store() -> SqliteStore:
    """造一个 **T26 之前形状**的库：14 张业务表都在，就是没有记账表。

    老 schema 从当前 `schema.sql` **剥**出来，而不是另存一份副本：副本会僵化在
    今天的形状上，哪天有人给业务表加了列，这条用例仍在拿一份古董 schema 建库，
    「老库升级」于是验的是一个早已不存在的老库。
    """
    script = _schema_text()
    legacy, hits = _VERSION_TABLE_DDL.subn("", script)
    assert hits == 1, (
        f"schema.sql 里有 {hits} 条 refund_schema_version 建表语句，期望恰好 1 条 ——"
        " 剥不干净的话这条用例建出来的就不是老库，验的东西整个是假的")
    assert "refund_schema_version" not in _declared_tables(legacy)

    store = SqliteStore()
    store.init_schema()
    conn = objects._conn(store)
    with objects.lock_of(store):
        conn.executescript(legacy)
        conn.commit()
    return store


# ----------------------------------------------------------- 1. 全新库
def test_fresh_db_gets_every_table_and_lands_on_the_current_version() -> None:
    """新库：14 张业务表 + 记账表都建出来，版本号落在当前值。"""
    store = _fresh_store()
    objects.ensure_schema(store)

    declared = _declared_tables(_schema_text())
    business = declared - {"refund_schema_version"}
    assert len(business) == _BUSINESS_TABLE_COUNT, (
        f"schema.sql 声明了 {len(business)} 张业务表，R-1 定的是 {_BUSINESS_TABLE_COUNT} 张")

    present = _tables_in(store)
    assert declared <= present, f"没建出来的表：{sorted(declared - present)}"
    assert "refund_schema_version" in present, "记账表本身也得建出来"
    assert objects.applied_schema_version(store) == objects.REFUND_SCHEMA_VERSION


def test_current_version_is_derived_from_the_migration_table_not_handwritten() -> None:
    """版本号必须跟着 `_MIGRATIONS` 算。手写的那份迟早对不上，症状是「迁移悄悄不跑了」。"""
    expected = max((v for v, _label, _step in objects._MIGRATIONS), default=0)
    assert objects.REFUND_SCHEMA_VERSION == expected


# ----------------------------------------------------------- 2. 老库升级
def test_legacy_db_without_the_version_table_upgrades_without_blowing_up() -> None:
    """🔴 派单点名的第 2 条：先用旧 schema 建一次，再跑 `ensure_schema()`。

    把迁移机制摘掉（schema.sql 里删掉记账表建表语句）这条就红：
    `applied_schema_version()` 会撞 `no such table: refund_schema_version`。
    """
    store = _legacy_store()
    assert "refund_schema_version" not in _tables_in(store), "前提：老库没有记账表"

    objects.ensure_schema(store)                       # 不许炸

    assert "refund_schema_version" in _tables_in(store), "升级后记账表必须补上"
    assert objects.applied_schema_version(store) == objects.REFUND_SCHEMA_VERSION
    declared = _declared_tables(_schema_text())
    assert declared <= _tables_in(store), "业务表一张都不许丢"


def test_legacy_db_keeps_the_rows_it_already_had() -> None:
    """升级不许动数据。老库里已有的行，升完还得在。"""
    store = _legacy_store()
    objects.execute(
        store,
        "INSERT INTO tenant (tenant_id, name, region) VALUES (?,?,?)",
        ("t-legacy", "老租户", "cn-hangzhou"),
    )

    objects.ensure_schema(store)

    rows = objects.query(store, "SELECT name FROM tenant WHERE tenant_id = ?", ("t-legacy",))
    assert rows == [{"name": "老租户"}]


# ----------------------------------------------------------- 3. 幂等
def test_ensure_schema_is_idempotent() -> None:
    """连跑两次：不报错，记账行不重复灌。"""
    store = _fresh_store()
    objects.ensure_schema(store)
    first = objects.query(store, "SELECT version FROM refund_schema_version ORDER BY version")

    objects.ensure_schema(store)                       # 第二次

    second = objects.query(store, "SELECT version FROM refund_schema_version ORDER BY version")
    assert first == second, "第二次跑不许再灌一遍记账行"
    assert objects.applied_schema_version(store) == objects.REFUND_SCHEMA_VERSION


# ------------------------------------------- 4/5. 机制语义（注入一条假步骤）
def _install_fake_step(monkeypatch: pytest.MonkeyPatch, calls: list, probe_says_done=lambda store: False):
    """把一条假迁移步骤装进 `_MIGRATIONS`，只活在这条用例里。

    生产的 `_MIGRATIONS` 保持空 —— 派单原话「不要凭空造一次迁移」。这里造的是
    **测试替身**，为的是证明「真放一条进去时框架会正确地跑」，而不是给退款域加迁移。
    """
    def step(store, script):
        calls.append(script)
        if probe_says_done(store):                     # 探针：已是目标形状就 no-op
            return
        objects.execute(store, "CREATE TABLE IF NOT EXISTS t26_probe_marker (k TEXT)")

    monkeypatch.setattr(objects, "_MIGRATIONS", ((1, "假步骤", step),))
    monkeypatch.setattr(objects, "REFUND_SCHEMA_VERSION", 1)
    return step


def test_a_registered_step_runs_once_then_the_version_short_circuits_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """装一条步骤：第一次跑，记账；第二次被版本号快路径挡掉，不再跑。"""
    calls: list = []
    _install_fake_step(monkeypatch, calls)
    store = _fresh_store()

    objects.ensure_schema(store)
    assert len(calls) == 1, "第一次必须跑这一步"
    assert objects.applied_schema_version(store) == 1, "跑完要记账"
    assert "t26_probe_marker" in _tables_in(store)
    assert calls[0].lstrip().startswith("--"), "步骤拿到的第二个参数是 schema.sql 原文"

    objects.ensure_schema(store)
    assert len(calls) == 1, "记到最新之后不许再跑一遍 —— 版本号是快路径"


def test_the_step_receives_a_store_whose_bookkeeping_row_is_not_yet_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """记账写在步骤**之后**：步骤跑到一半炸了就不记，下次重跑同一步。

    反过来先记账后干活的话，第一次失败会把这一步永久标记成「做过了」，
    库停在半成品上而版本号说一切正常 —— 又一个无症状失效。
    """
    seen: list[int] = []

    def failing_step(store, script):
        seen.append(objects.applied_schema_version(store))
        raise RuntimeError("这一步炸了")

    monkeypatch.setattr(objects, "_MIGRATIONS", ((1, "会炸的步骤", failing_step),))
    monkeypatch.setattr(objects, "REFUND_SCHEMA_VERSION", 1)
    store = _fresh_store()

    with pytest.raises(RuntimeError):
        objects.ensure_schema(store)

    assert seen == [0], "步骤跑的时候版本还是 0"
    assert objects.applied_schema_version(store) == 0, "炸了就不许记账"


# ----------------------------------------------------------- 6. _atomic 的 DDL 回滚
def test_atomic_rolls_back_ddl_not_just_dml() -> None:
    """🔴 SAVEPOINT 的理由：光靠 `rollback()` 撤不回 `CREATE TABLE`。

    sqlite3 传统模式只为 DML 隐式开事务，DDL 是在自动提交下跑的，一发就落盘。
    这条断言在没有显式 SAVEPOINT 的实现上会红 —— 半成品表会留在库里。
    """
    store = _fresh_store()
    objects.ensure_schema(store)

    with pytest.raises(sqlite3.Error):
        objects._atomic(store, [
            ("CREATE TABLE t26_half_baked (k TEXT)", ()),
            ("INSERT INTO t26_no_such_table (k) VALUES ('x')", ()),   # 这句炸
        ])

    assert "t26_half_baked" not in _tables_in(store), (
        "前一句的 CREATE TABLE 必须跟着回滚 —— 留下来就是「表在、数据空」那种无症状失效")


def test_atomic_commits_the_whole_group_when_nothing_fails() -> None:
    """正路：整组都成功就整组落盘。"""
    store = _fresh_store()
    objects.ensure_schema(store)

    objects._atomic(store, [
        ("CREATE TABLE t26_ok (k TEXT)", ()),
        ("INSERT INTO t26_ok (k) VALUES (?)", ("v",)),
    ])

    assert objects.query(store, "SELECT k FROM t26_ok") == [{"k": "v"}]


# ----------------------------------------------------------- 7. 铁律 8 不破例
def test_atomic_still_refuses_refund_case_writes() -> None:
    """迁移绕开 `execute()` 是为了拿事务，不是为了拿豁免权（铁律 8）。"""
    store = _fresh_store()
    objects.ensure_schema(store)

    with pytest.raises(objects.BypassedGuardError):
        objects._atomic(store, [
            ("UPDATE refund_case SET biz_status = 'settled'", ()),
        ])


def test_atomic_allows_alter_table_on_refund_case() -> None:
    """但 ALTER 要放行：「给 refund_case 加一列」正是这套机制存在的理由。"""
    store = _fresh_store()
    objects.ensure_schema(store)

    objects._atomic(store, [("ALTER TABLE refund_case ADD COLUMN t26_probe TEXT", ())])

    assert objects._has_column(store, "refund_case", "t26_probe")


# ----------------------------------------------------------- 探针本身
def test_has_column_answers_both_ways() -> None:
    store = _fresh_store()
    objects.ensure_schema(store)

    assert objects._has_column(store, "refund_case", "biz_status") is True
    assert objects._has_column(store, "refund_case", "no_such_column_here") is False
    assert objects._has_column(store, "t26_no_such_table", "whatever") is False
