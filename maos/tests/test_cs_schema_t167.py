"""T167 会话表的机器验收（一）—— 建表、迁移记账、列形状、CHECK、PG 翻译。

跨轨契约 review/p12-cs-contracts.md §1.3。期望值**一律写死在本文件里**：表名集合、
列形状照契约 DDL 抄；枚举取值从 ``maos/domain/cs/types.py`` 取（那是另一份冻结面，
不是被测的 schema.sql）。从被测文件里数出来当期望，等于拿嫌疑人当证人。
"""

from __future__ import annotations

import pathlib
import re
import sqlite3

import pytest

from maos.core.store import SqliteStore
from maos.domain._dbport import split_statements, strip_sql_comments, to_pg_ddl
from maos.domain.cs import objects
from maos.domain.cs import types as T

_ROOT = pathlib.Path(__file__).resolve().parents[2]
CS_SCHEMA_T167 = _ROOT / "maos" / "domain" / "cs" / "schema.sql"

#: p12 契约 §1.3 的四张表 + p13 契约 §1.3 的五张（T171 接手时追加）。写死，不从 schema.sql 里数。
CS_TABLES_T167 = frozenset({"cs_schema_version", "cs_conversation", "cs_turn", "cs_handoff",
                            "cs_observation", "cs_order_binding", "cs_slot", "cs_turn_ext",
                            "cs_refund_bridge"})

#: 契约 §1.3 逐列抄：(列名, 类型, NOT NULL, 缺省, 主键序号)。
#: 缺省按 SQLite PRAGMA table_info 的原样（字符串字面量带引号）。
EXPECTED_COLUMNS_T167: dict[str, list[tuple[str, str, int, str | None, int]]] = {
    "cs_schema_version": [
        ("version", "INTEGER", 0, None, 1),
        ("applied_at", "TEXT", 1, None, 0),
    ],
    "cs_conversation": [
        ("tenant_id", "TEXT", 1, None, 1),
        ("conversation_id", "TEXT", 1, None, 2),
        ("channel", "TEXT", 1, None, 0),
        ("open_kfid", "TEXT", 1, "''", 0),
        ("external_userid", "TEXT", 1, None, 0),
        ("stage", "TEXT", 1, None, 0),
        ("fallback_streak", "INTEGER", 1, "0", 0),
        ("turn_count", "INTEGER", 1, "0", 0),
        ("opened_at", "TEXT", 1, None, 0),
        ("updated_at", "TEXT", 1, None, 0),
    ],
    "cs_turn": [
        ("tenant_id", "TEXT", 1, None, 1),
        ("conversation_id", "TEXT", 1, None, 0),
        ("turn_id", "TEXT", 1, None, 2),
        ("seq", "INTEGER", 1, None, 0),
        ("msg_dedup_key", "TEXT", 1, "''", 0),
        ("inbound_text", "TEXT", 1, None, 0),
        ("reply_text", "TEXT", 1, "''", 0),
        ("route", "TEXT", 1, None, 0),
        ("intent", "TEXT", 1, "''", 0),
        ("handoff_reason", "TEXT", 1, "''", 0),
        ("draft_json", "TEXT", 1, "'{}'", 0),
        ("check_json", "TEXT", 1, "'{}'", 0),
        ("created_at", "TEXT", 1, None, 0),
    ],
    "cs_handoff": [
        ("tenant_id", "TEXT", 1, None, 1),
        ("handoff_id", "TEXT", 1, None, 2),
        ("conversation_id", "TEXT", 1, None, 0),
        ("turn_id", "TEXT", 1, None, 0),
        ("reason", "TEXT", 1, None, 0),
        ("card_json", "TEXT", 1, None, 0),
        ("delivery", "TEXT", 1, None, 0),
        ("delivered_to", "TEXT", 1, "''", 0),
        ("created_at", "TEXT", 1, None, 0),
        ("updated_at", "TEXT", 1, None, 0),
    ],
}

NOW_T167 = "2026-09-24T08:00:00+00:00"


def _store_t167() -> SqliteStore:
    s = SqliteStore()
    s.init_schema()
    objects.ensure_schema(s)
    return s


def _tables_t167(store) -> set[str]:
    rows = objects.query(store, "SELECT name FROM sqlite_master WHERE type='table'")
    return {r["name"] for r in rows if r["name"].startswith("cs_")}


def _insert_conv_t167(store, cid: str, *, stage: str = T.STAGE_ACTIVE) -> None:
    objects.execute(
        store,
        "INSERT INTO cs_conversation (tenant_id, conversation_id, channel, open_kfid,"
        " external_userid, stage, opened_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        ("tnt-t167", cid, T.CHANNEL_WECHAT_KF, "wk_1", "wm_1", stage, NOW_T167, NOW_T167))


def _insert_turn_t167(store, tid: str, *, route: str = T.ROUTE_ANSWER,
                      handoff_reason: str = "") -> None:
    objects.execute(
        store,
        "INSERT INTO cs_turn (tenant_id, conversation_id, turn_id, seq, inbound_text, route,"
        " handoff_reason, created_at) VALUES (?,?,?,?,?,?,?,?)",
        ("tnt-t167", "csc-x", tid, 1, "你好", route, handoff_reason, NOW_T167))


def _insert_handoff_t167(store, hid: str, *, reason: str = T.HANDOFF_REQUESTED,
                         delivery: str = T.DELIVERY_PENDING) -> None:
    objects.execute(
        store,
        "INSERT INTO cs_handoff (tenant_id, handoff_id, conversation_id, turn_id, reason,"
        " card_json, delivery, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ("tnt-t167", hid, "csc-x", hid, reason, "{}", delivery, NOW_T167, NOW_T167))


def _check_in_list_t167(store, table: str, column: str) -> set[str]:
    """从库里（sqlite_master）读回某列 CHECK (col IN (...)) 的取值集合。"""
    sql = objects.query(store, "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                        (table,))[0]["sql"]
    flat = " ".join(sql.split())
    m = re.search(rf"\b{column} TEXT NOT NULL(?: DEFAULT '[^']*')? CHECK \({column} IN \(([^)]*)\)\)",
                  flat)
    assert m is not None, f"{table}.{column} 没有 CHECK (… IN (…))：{flat}"
    return set(re.findall(r"'([^']*)'", m.group(1)))


# ------------------------------------------------------------------ 建表与迁移
def test_ensure_schema_is_idempotent_and_keeps_rows_t167():
    s = _store_t167()
    assert _tables_t167(s) == CS_TABLES_T167
    _insert_conv_t167(s, "csc-keep")
    for _ in range(3):
        objects.ensure_schema(s)
    assert _tables_t167(s) == CS_TABLES_T167
    rows = objects.query(s, "SELECT conversation_id FROM cs_conversation")
    assert [r["conversation_id"] for r in rows] == ["csc-keep"]
    idx = objects.query(s, "SELECT name, tbl_name FROM sqlite_master WHERE type='index'"
                           " AND name='idx_cs_turn_conv_seq'")
    assert idx == [{"name": "idx_cs_turn_conv_seq", "tbl_name": "cs_turn"}]
    cols = [r["name"] for r in objects.query(s, "PRAGMA index_info(idx_cs_turn_conv_seq)")]
    assert cols == ["tenant_id", "conversation_id", "seq"]


def test_schema_version_starts_empty_with_no_migrations_t167():
    s = _store_t167()
    assert objects._MIGRATIONS == ()
    assert objects.CS_SCHEMA_VERSION == 0
    assert objects.applied_schema_version(s) == 0
    assert objects.query(s, "SELECT COUNT(*) AS n FROM cs_schema_version") == [{"n": 0}]


def test_migrate_records_each_step_exactly_once_t167(monkeypatch):
    """迁移机制真的记账：追加一步，ensure_schema 连跑两次，步骤只跑一次、记一行。"""
    s = _store_t167()
    calls: list[int] = []

    def step(store, script):
        assert "cs_conversation" in script
        calls.append(1)

    monkeypatch.setattr(objects, "_MIGRATIONS", ((1, "t167 测试步骤", step),))
    objects.ensure_schema(s)
    objects.ensure_schema(s)
    assert calls == [1]
    assert objects.applied_schema_version(s) == 1
    rows = objects.query(s, "SELECT version, applied_at FROM cs_schema_version")
    assert [r["version"] for r in rows] == [1] and rows[0]["applied_at"]


def test_columns_match_contract_t167():
    s = _store_t167()
    for table, expected in EXPECTED_COLUMNS_T167.items():
        got = [(r["name"], r["type"], r["notnull"], r["dflt_value"], r["pk"])
               for r in objects.query(s, f"PRAGMA table_info({table})")]
        assert got == expected, table


def test_business_tables_lead_with_tenant_and_prefix_cs_t167():
    s = _store_t167()
    for table in CS_TABLES_T167 - {"cs_schema_version"}:
        info = objects.query(s, f"PRAGMA table_info({table})")
        assert info[0]["name"] == "tenant_id", table
        assert info[0]["pk"] == 1, f"{table}：tenant_id 必须是主键第一列"


# ------------------------------------------------------------------ PG 翻译
def test_pg_translation_yields_exactly_the_nine_tables_t167():
    ddl = to_pg_ddl(CS_SCHEMA_T167.read_text(encoding="utf-8"))
    created = set(re.findall(r"CREATE TABLE(?: IF NOT EXISTS)? (\w+)", ddl))
    assert created == {"cs_schema_version", "cs_conversation", "cs_turn", "cs_handoff",
                       "cs_observation", "cs_order_binding", "cs_slot", "cs_turn_ext",
                       "cs_refund_bridge"}
    assert re.search(r"CREATE INDEX IF NOT EXISTS idx_cs_turn_conv_seq ON cs_turn"
                     r" \(tenant_id, conversation_id, seq\)", ddl)
    # p12 五个 + p13 九个 CHECK 翻完一个不少（翻译器对认不出的约束会抛，这里再钉一次「没被吞」）。
    assert len(re.findall(r"\bCHECK \(", ddl)) == 14
    assert "datetime(" not in ddl.lower()


def test_schema_uses_only_the_translatable_subset_t167():
    text = strip_sql_comments(CS_SCHEMA_T167.read_text(encoding="utf-8"))
    assert "datetime(" not in text.lower() and "current_timestamp" not in text.lower()
    statements = split_statements(text)
    assert len(statements) == 12  # 9 CREATE TABLE + 3 CREATE INDEX（p13 加 5 表 2 索引）
    for st in statements:
        assert re.match(r"CREATE (TABLE|INDEX) IF NOT EXISTS (cs_|idx_cs_)", st), st
        # 列定义行 = 缩进 + 小写列名 + 类型（PRIMARY KEY 行、CHECK 续行都不是这个形状）。
        types_used = set(re.findall(r"^\s+[a-z_]+\s+([A-Z]+)\b", st, re.MULTILINE))
        assert types_used <= {"TEXT", "INTEGER"}, (types_used, st)


# ------------------------------------------------------------------ CHECK
@pytest.mark.parametrize("table,column,enum", [
    ("cs_conversation", "stage", T.STAGES),
    ("cs_turn", "route", T.ROUTES),
    ("cs_turn", "handoff_reason", ("",) + T.HANDOFF_REASONS),
    ("cs_handoff", "reason", T.HANDOFF_REASONS),
    ("cs_handoff", "delivery", T.DELIVERIES),
])
def test_check_lists_equal_the_frozen_enums_t167(table, column, enum):
    s = _store_t167()
    assert _check_in_list_t167(s, table, column) == set(enum)


@pytest.mark.parametrize("value", T.STAGES)
def test_stage_check_accepts_every_stage_t167(value):
    s = _store_t167()
    _insert_conv_t167(s, f"csc-{value}", stage=value)


@pytest.mark.parametrize("value", ["bogus", "ACTIVE", "", "handedoff"])
def test_stage_check_rejects_illegal_t167(value):
    s = _store_t167()
    with pytest.raises(sqlite3.IntegrityError):
        _insert_conv_t167(s, "csc-bad", stage=value)


@pytest.mark.parametrize("value", T.ROUTES)
def test_route_check_accepts_every_route_t167(value):
    s = _store_t167()
    _insert_turn_t167(s, f"t-{value}", route=value)


@pytest.mark.parametrize("value", ["bogus", "Answer", "", "reject"])
def test_route_check_rejects_illegal_t167(value):
    s = _store_t167()
    with pytest.raises(sqlite3.IntegrityError):
        _insert_turn_t167(s, "t-bad", route=value)


@pytest.mark.parametrize("value", ("",) + T.HANDOFF_REASONS)
def test_turn_handoff_reason_check_accepts_empty_or_reason_t167(value):
    s = _store_t167()
    _insert_turn_t167(s, f"t-{value or 'none'}", handoff_reason=value)


@pytest.mark.parametrize("value", ["bogus", "Requested", " ", "timeout"])
def test_turn_handoff_reason_check_rejects_illegal_t167(value):
    s = _store_t167()
    with pytest.raises(sqlite3.IntegrityError):
        _insert_turn_t167(s, "t-bad", handoff_reason=value)


@pytest.mark.parametrize("value", T.HANDOFF_REASONS)
def test_handoff_reason_check_accepts_every_reason_t167(value):
    s = _store_t167()
    _insert_handoff_t167(s, f"h-{value}", reason=value)


@pytest.mark.parametrize("value", ["", "bogus", "Anger"])
def test_handoff_reason_check_rejects_illegal_t167(value):
    s = _store_t167()
    with pytest.raises(sqlite3.IntegrityError):
        _insert_handoff_t167(s, "h-bad", reason=value)


@pytest.mark.parametrize("value", T.DELIVERIES)
def test_delivery_check_accepts_every_delivery_t167(value):
    s = _store_t167()
    _insert_handoff_t167(s, f"h-{value}", delivery=value)


@pytest.mark.parametrize("value", ["", "bogus", "sent", "Delivered"])
def test_delivery_check_rejects_illegal_t167(value):
    s = _store_t167()
    with pytest.raises(sqlite3.IntegrityError):
        _insert_handoff_t167(s, "h-bad", delivery=value)
