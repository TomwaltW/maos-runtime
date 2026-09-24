"""T171 存储与绑定的机器验收 —— p13 五张新表、旧库探针、观察 / 绑定 / 槽位 / 轮次扩展 / 退款桥、
``record_turn`` 的 p13 增量、R5 哨兵。

跨轨契约 review/p13-cs-contracts.md §1.1 / §1.3 / §1.4 T171 / §2' R5。期望值**写死在本文件**：
表名、列形状照契约抄；枚举取 ``ports.py`` / ``types.py``（冻结面，不是被测文件）。
CHECK 取值用本文件自己的正则从库里读回，不借被测的 ``objects._check_literals``。
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading

import pytest

from maos.core.store import SqliteStore
from maos.domain._dbport import POSTGRES, DomainConn, to_pg_ddl, to_pg_sql
from maos.domain.cs import conversation as C
from maos.domain.cs import identity as I
from maos.domain.cs import objects
from maos.domain.cs import ports as P
from maos.domain.cs import records as R
from maos.domain.cs import types as T

NOW_T171 = "2026-09-24T10:00:00+00:00"
LATER_T171 = "2026-09-24T11:30:00+00:00"
TENANT_T171 = "tnt-t171"
NEW_TABLES_T171 = ("cs_observation", "cs_order_binding", "cs_slot", "cs_turn_ext",
                   "cs_refund_bridge")


def _store_t171() -> SqliteStore:
    s = SqliteStore()
    s.init_schema()
    return s


def _open_t171(store, *, tenant_id=TENANT_T171, external_userid="wm_t171_a",
               open_kfid="wk_t171", channel=T.CHANNEL_WECHAT_KF) -> C.Conversation:
    return C.open_conversation(store, tenant_id=tenant_id, channel=channel, open_kfid=open_kfid,
                               external_userid=external_userid, now=NOW_T171)


def _record_t171(store, conv, route, *, turn_id=None, seq=None, **extra):
    """取号（或用给的号）+ 落一轮，返回 (turn_id, 更新后的会话)。"""
    if turn_id is None:
        turn_id, seq = C.allocate_turn(store, conv)
    reply = "" if route == T.ROUTE_SILENT else "好的，请问订单号是多少"
    reason = T.HANDOFF_REQUESTED if route == T.ROUTE_HANDOFF else ""
    conv = C.record_turn(store, conv, turn_id=turn_id, seq=seq, msg_dedup_key="",
                         inbound_text="我的单到哪了", reply_text=reply, route=route,
                         intent=T.INTENT_LOGISTICS, handoff_reason=reason,
                         draft=T.ReplyDraft(text=reply), check=T.CheckResult(ok=True),
                         now=NOW_T171, **extra)
    return turn_id, conv


def _ok_t171(status=P.ORDER_SHIPPED, *, system_name="demo-orders",
             query_key="gid-1001") -> P.LookupResult:
    return P.LookupResult(outcome=P.LOOKUP_OK, system_name=system_name, query_key=query_key,
                          status=status, version=3, updated_at="2026-09-20T08:00:00+00:00")


def _binding_t171(**over) -> P.Binding:
    base = dict(tenant_id=TENANT_T171, channel=T.CHANNEL_WECHAT_KF, external_userid="wm_t171_a",
                display_no="A1001", system_name="demo-orders", query_key="gid-1001",
                source=P.BINDING_TEST)
    base.update(over)
    return P.Binding(**base)


def _event_rows_t171(store) -> list[dict]:
    return objects.query(store, "SELECT * FROM event_log ORDER BY seq")


def _count_t171(store, table: str) -> int:
    return int(objects.query(store, f"SELECT COUNT(*) AS n FROM {table}")[0]["n"])


def _resolve_t171(store, display_no, **over):
    key = dict(tenant_id=TENANT_T171, channel=T.CHANNEL_WECHAT_KF, external_userid="wm_t171_a")
    key.update(over)
    return I.BindingVerifier().resolve(store, display_no=display_no, **key)


# ====================================================================== 表形状
#: 契约 §1.3 逐列抄：(列名, 类型, NOT NULL, 缺省, 主键序号)。缺省按 PRAGMA table_info 原样。
EXPECTED_COLUMNS_T171: dict[str, list[tuple[str, str, int, str | None, int]]] = {
    "cs_observation": [
        ("tenant_id", "TEXT", 1, None, 1),
        ("observation_id", "TEXT", 1, None, 2),
        ("conversation_id", "TEXT", 1, None, 0),
        ("turn_id", "TEXT", 1, None, 0),
        ("kind", "TEXT", 1, None, 0),
        ("system_name", "TEXT", 1, None, 0),
        ("query_key", "TEXT", 1, None, 0),
        ("status", "TEXT", 1, None, 0),
        ("version", "INTEGER", 1, "0", 0),
        ("updated_at", "TEXT", 1, "''", 0),
        ("observed_at", "TEXT", 1, None, 0),
    ],
    "cs_order_binding": [
        ("tenant_id", "TEXT", 1, None, 1),
        ("channel", "TEXT", 1, None, 2),
        ("external_userid", "TEXT", 1, None, 3),
        ("display_no", "TEXT", 1, None, 4),
        ("system_name", "TEXT", 1, None, 0),
        ("query_key", "TEXT", 1, None, 0),
        ("source", "TEXT", 1, None, 0),
        ("bound_at", "TEXT", 1, None, 0),
    ],
    "cs_slot": [
        ("tenant_id", "TEXT", 1, None, 1),
        ("conversation_id", "TEXT", 1, None, 2),
        ("slot_key", "TEXT", 1, None, 3),
        ("value", "TEXT", 1, None, 0),
        ("turn_id", "TEXT", 1, None, 0),
        ("source", "TEXT", 1, None, 0),
        ("updated_at", "TEXT", 1, None, 0),
    ],
    "cs_turn_ext": [
        ("tenant_id", "TEXT", 1, None, 1),
        ("turn_id", "TEXT", 1, None, 2),
        ("conversation_id", "TEXT", 1, None, 0),
        ("lang", "TEXT", 1, None, 0),
        ("lookup_outcome", "TEXT", 1, "''", 0),
        ("ask_slot", "TEXT", 1, "''", 0),
        ("ask_count", "INTEGER", 1, "0", 0),
    ],
    "cs_refund_bridge": [
        ("tenant_id", "TEXT", 1, None, 1),
        ("bridge_id", "TEXT", 1, None, 2),
        ("conversation_id", "TEXT", 1, None, 0),
        ("turn_id", "TEXT", 1, None, 0),
        ("order_no", "TEXT", 1, None, 0),
        ("ok", "INTEGER", 1, None, 0),
        ("decision", "TEXT", 1, "''", 0),
        ("rule_ref", "TEXT", 1, "''", 0),
        ("reason_code", "TEXT", 1, "''", 0),
        ("command_line", "TEXT", 1, "''", 0),
        ("refused_why", "TEXT", 1, "''", 0),
        ("created_at", "TEXT", 1, None, 0),
    ],
}


def test_new_tables_columns_match_contract_t171():
    s = _store_t171()
    objects.ensure_schema(s)
    assert set(EXPECTED_COLUMNS_T171) == set(NEW_TABLES_T171)
    for table, expected in EXPECTED_COLUMNS_T171.items():
        got = [(r["name"], r["type"], r["notnull"], r["dflt_value"], r["pk"])
               for r in objects.query(s, f"PRAGMA table_info({table})")]
        assert got == expected, table


def _check_in_list_t171(store, table: str, column: str) -> set[str]:
    """从库里（sqlite_master）读回某列 ``CHECK (col IN ('…', …))`` 的取值集合。"""
    sql = objects.query(store, "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                        (table,))[0]["sql"]
    flat = " ".join(sql.split())
    m = re.search(rf"\b{column} TEXT NOT NULL(?: DEFAULT '[^']*')? CHECK \({column} IN \(([^)]*)\)\)",
                  flat)
    assert m is not None, f"{table}.{column} 没有 CHECK (… IN (…))：{flat}"
    return set(re.findall(r"'([^']*)'", m.group(1)))


@pytest.mark.parametrize("table,column,enum", [
    ("cs_observation", "kind", P.OBS_KINDS),
    ("cs_observation", "status", P.ORDER_STATUSES),
    ("cs_order_binding", "source", P.BINDING_SOURCES),
    ("cs_slot", "slot_key", P.SLOT_KEYS),
    ("cs_slot", "source", P.SLOT_SOURCES),
    ("cs_turn_ext", "lang", P.LANGS),
    ("cs_turn_ext", "lookup_outcome", ("",) + P.LOOKUP_OUTCOMES),
    ("cs_turn_ext", "ask_slot", ("",) + P.SLOT_KEYS),
])
def test_check_lists_equal_the_frozen_enums_t171(table, column, enum):
    s = _store_t171()
    objects.ensure_schema(s)
    assert _check_in_list_t171(s, table, column) == set(enum)


def test_bridge_ok_is_checked_boolean_t171():
    s = _store_t171()
    objects.ensure_schema(s)
    sql = objects.query(s, "SELECT sql FROM sqlite_master WHERE name='cs_refund_bridge'")[0]["sql"]
    assert "ok              INTEGER NOT NULL CHECK (ok IN (0, 1))" in sql


#: 每张表一行合法的原始行（主键各自独立）；CHECK 判据只改其中一列。
_RAW_ROWS_T171: dict[str, dict] = {
    "cs_observation": {"tenant_id": TENANT_T171, "observation_id": "csc-x-t0001-o01",
                       "conversation_id": "csc-x", "turn_id": "csc-x-t0001",
                       "kind": "order_lookup", "system_name": "demo-orders",
                       "query_key": "gid-1", "status": "paid", "observed_at": NOW_T171},
    "cs_order_binding": {"tenant_id": TENANT_T171, "channel": "wechat_kf",
                         "external_userid": "wm_1", "display_no": "A1", "system_name": "s",
                         "query_key": "q", "source": "seed", "bound_at": NOW_T171},
    "cs_slot": {"tenant_id": TENANT_T171, "conversation_id": "csc-x", "slot_key": "order_no",
                "value": "A1", "turn_id": "csc-x-t0001", "source": "rule",
                "updated_at": NOW_T171},
    "cs_turn_ext": {"tenant_id": TENANT_T171, "turn_id": "csc-x-t0001",
                    "conversation_id": "csc-x", "lang": "zh"},
    "cs_refund_bridge": {"tenant_id": TENANT_T171, "bridge_id": "csc-x-t0001",
                         "conversation_id": "csc-x", "turn_id": "csc-x-t0001",
                         "order_no": "A1", "ok": 1, "created_at": NOW_T171},
}

#: (表, 列, 全部合法取值, 非法取值)。
_CHECK_CASES_T171 = (
    ("cs_observation", "kind", P.OBS_KINDS, ("", "order", "ORDER_LOOKUP", "payment_observe")),
    ("cs_observation", "status", P.ORDER_STATUSES, ("", "delivered", "Shipped", "refunded")),
    ("cs_order_binding", "source", P.BINDING_SOURCES, ("", "external", "wechat_kf", "Seed")),
    ("cs_slot", "slot_key", P.SLOT_KEYS, ("", "phone", "Order_no", "amount")),
    ("cs_slot", "source", P.SLOT_SOURCES, ("", "customer", "Rule", "llm")),
    ("cs_turn_ext", "lang", P.LANGS, ("", "fr", "ZH", "zh-CN")),
    ("cs_turn_ext", "lookup_outcome", ("",) + P.LOOKUP_OUTCOMES, ("OK", "error", "timeout", " ")),
    ("cs_turn_ext", "ask_slot", ("",) + P.SLOT_KEYS, ("phone", "ORDER_NO", " ")),
    ("cs_refund_bridge", "ok", (0, 1), (2, -1, "yes")),
)


def _raw_insert_t171(store, table: str, **over) -> None:
    row = dict(_RAW_ROWS_T171[table])
    row.update(over)
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    objects.execute(store, f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(row.values()))


@pytest.mark.parametrize("table,column,value", [
    (t, c, v) for t, c, good, _bad in _CHECK_CASES_T171 for v in good])
def test_new_table_checks_accept_every_legal_value_t171(table, column, value):
    s = _store_t171()
    objects.ensure_schema(s)
    _raw_insert_t171(s, table, **{column: value})
    assert _count_t171(s, table) == 1


@pytest.mark.parametrize("table,column,value", [
    (t, c, v) for t, c, _good, bad in _CHECK_CASES_T171 for v in bad])
def test_new_table_checks_reject_illegal_values_t171(table, column, value):
    s = _store_t171()
    objects.ensure_schema(s)
    with pytest.raises(sqlite3.IntegrityError):
        _raw_insert_t171(s, table, **{column: value})
    assert _count_t171(s, table) == 0


def test_every_new_table_has_a_check_that_bites_t171():
    """五张表每张都至少有一条 CHECK 判据（防有表漏了 CHECK 却没人发现）。"""
    assert {t for t, *_ in _CHECK_CASES_T171} == set(NEW_TABLES_T171)


def test_new_tables_lead_with_tenant_in_primary_key_t171():
    s = _store_t171()
    objects.ensure_schema(s)
    for table in NEW_TABLES_T171:
        info = objects.query(s, f"PRAGMA table_info({table})")
        assert (info[0]["name"], info[0]["pk"]) == ("tenant_id", 1), table


# ====================================================================== PG 翻译
_PKS_T171 = {
    "cs_observation": "PRIMARY KEY (tenant_id, observation_id)",
    "cs_order_binding": "PRIMARY KEY (tenant_id, channel, external_userid, display_no)",
    "cs_slot": "PRIMARY KEY (tenant_id, conversation_id, slot_key)",
    "cs_turn_ext": "PRIMARY KEY (tenant_id, turn_id)",
    "cs_refund_bridge": "PRIMARY KEY (tenant_id, bridge_id)",
}
_PG_CHECK_COUNTS_T171 = {"cs_observation": 2, "cs_order_binding": 1, "cs_slot": 2,
                         "cs_turn_ext": 3, "cs_refund_bridge": 1}


def test_pg_translation_covers_the_five_new_tables_t171():
    ddl = to_pg_ddl(objects._SCHEMA_PATH.read_text(encoding="utf-8"))
    for table in NEW_TABLES_T171:
        m = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);", ddl, re.DOTALL)
        assert m is not None, table
        body = m.group(1)
        assert _PKS_T171[table] in body, table
        assert len(re.findall(r"\bCHECK \(", body)) == _PG_CHECK_COUNTS_T171[table], table
        assert "datetime(" not in body.lower()
    assert "CREATE INDEX IF NOT EXISTS idx_cs_observation_conv_turn ON cs_observation" in ddl
    assert "CREATE INDEX IF NOT EXISTS idx_cs_turn_ext_conv_slot ON cs_turn_ext" in ddl


def test_upserts_translate_to_on_conflict_on_the_contract_pk_t171():
    """records 里两处 ``INSERT OR REPLACE`` 在 PG 侧翻成 ON CONFLICT(契约主键) DO UPDATE。"""
    pks = {"cs_order_binding": ("tenant_id", "channel", "external_userid", "display_no"),
           "cs_slot": ("tenant_id", "conversation_id", "slot_key")}
    binding = to_pg_sql(R._UPSERT_BINDING, lambda t: pks[t], with_params=True)
    assert "ON CONFLICT (tenant_id, channel, external_userid, display_no) DO UPDATE SET" in binding
    assert "system_name=EXCLUDED.system_name" in binding and "?" not in binding


# ====================================================================== 旧库探针
#: p12 收尾（c7d4a1e）时 maos/domain/cs/schema.sql 的建表语句，逐字抄（去注释）。
#: 写死在这里，不从当前 schema.sql 推：推出来的「旧库」会跟着被测文件一起变。
P12_TURN_T171 = """
CREATE TABLE IF NOT EXISTS cs_turn (
    tenant_id       TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    turn_id         TEXT NOT NULL,
    seq             INTEGER NOT NULL,
    msg_dedup_key   TEXT NOT NULL DEFAULT '',
    inbound_text    TEXT NOT NULL,
    reply_text      TEXT NOT NULL DEFAULT '',
    route           TEXT NOT NULL CHECK (route IN ('answer', 'fallback', 'handoff', 'silent')),
    intent          TEXT NOT NULL DEFAULT '',
    handoff_reason  TEXT NOT NULL DEFAULT '' CHECK (handoff_reason IN ('', 'requested',
                        'complaint', 'anger', 'compensation', 'privacy', 'needs_order_lookup',
                        'unverified_claim', 'repeated_fallback', 'tenant_unmapped')),
    draft_json      TEXT NOT NULL DEFAULT '{}',
    check_json      TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    PRIMARY KEY (tenant_id, turn_id)
);
CREATE INDEX IF NOT EXISTS idx_cs_turn_conv_seq ON cs_turn (tenant_id, conversation_id, seq);
"""
P12_HANDOFF_T171 = """
CREATE TABLE IF NOT EXISTS cs_handoff (
    tenant_id       TEXT NOT NULL,
    handoff_id      TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    turn_id         TEXT NOT NULL,
    reason          TEXT NOT NULL CHECK (reason IN ('requested', 'complaint', 'anger',
                        'compensation', 'privacy', 'needs_order_lookup', 'unverified_claim',
                        'repeated_fallback', 'tenant_unmapped')),
    card_json       TEXT NOT NULL,
    delivery        TEXT NOT NULL CHECK (delivery IN ('pending', 'delivered', 'failed',
                        'unconfigured')),
    delivered_to    TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (tenant_id, handoff_id)
);
"""
P12_SCHEMA_T171 = """
CREATE TABLE IF NOT EXISTS cs_schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cs_conversation (
    tenant_id       TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    channel         TEXT NOT NULL,
    open_kfid       TEXT NOT NULL DEFAULT '',
    external_userid TEXT NOT NULL,
    stage           TEXT NOT NULL CHECK (stage IN ('active', 'handed_off', 'closed')),
    fallback_streak INTEGER NOT NULL DEFAULT 0,
    turn_count      INTEGER NOT NULL DEFAULT 0,
    opened_at       TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (tenant_id, conversation_id)
);
""" + P12_TURN_T171 + P12_HANDOFF_T171

_P13_ONLY_T171 = ["'clarify'", "'identity_unverified'", "'order_unmapped'", "'lookup_failed'",
                  "'refund_request'"]


def _cs_tables_t171(store) -> set[str]:
    return {r["name"] for r in objects.query(
        store, "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'cs_%'")}


def _p12_store_t171(script: str = P12_SCHEMA_T171) -> SqliteStore:
    s = _store_t171()
    DomainConn.open(s).executescript(script)
    return s


def test_p12_schema_fixture_is_really_the_old_shape_t171():
    """判据不空转：夹具里的旧 CHECK 确实不认 p13 的新值，而当前 schema.sql 认。"""
    for literal in _P13_ONLY_T171:
        assert literal not in P12_SCHEMA_T171
        assert literal in objects._SCHEMA_PATH.read_text(encoding="utf-8")
    s = _p12_store_t171()
    with pytest.raises(sqlite3.IntegrityError):
        objects.execute(s, "INSERT INTO cs_turn (tenant_id, conversation_id, turn_id, seq,"
                           " inbound_text, route, created_at) VALUES (?,?,?,?,?,?,?)",
                        (TENANT_T171, "csc-x", "csc-x-t0001", 1, "x", "clarify", NOW_T171))


def test_old_p12_db_is_refused_loudly_and_left_untouched_t171():
    s = _p12_store_t171()
    objects.execute(s, "INSERT INTO cs_conversation (tenant_id, conversation_id, channel,"
                       " external_userid, stage, opened_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                    (TENANT_T171, "csc-old", "wechat_kf", "wm_old", "active", NOW_T171, NOW_T171))
    before = objects.query(s, "SELECT name, sql FROM sqlite_master ORDER BY name")
    with pytest.raises(objects.CsSchemaOutdated) as exc:
        objects.ensure_schema(s)
    msg = str(exc.value)
    assert "p13 之前建的库" in msg and "要重建" in msg
    for part in ("cs_turn.route", "'clarify'", "cs_turn.handoff_reason", "cs_handoff.reason",
                 "'refund_request'", "'identity_unverified'"):
        assert part in msg, part
    # 探针在建表之前：旧库上一张新表都没建，已有的行一行没动。
    assert objects.query(s, "SELECT name, sql FROM sqlite_master ORDER BY name") == before
    assert _cs_tables_t171(s) == {"cs_schema_version", "cs_conversation", "cs_turn", "cs_handoff"}
    assert [r["conversation_id"] for r in objects.query(
        s, "SELECT conversation_id FROM cs_conversation")] == ["csc-old"]
    # 不是一次性的：每个入口都照样被拦（不静默写失败）。
    with pytest.raises(objects.CsSchemaOutdated):
        objects.ensure_schema(s)
    with pytest.raises(objects.CsSchemaOutdated):
        _open_t171(s)
    with pytest.raises(objects.CsSchemaOutdated):
        _resolve_t171(s, "A1001")


@pytest.mark.parametrize("script,named,not_named", [
    (P12_HANDOFF_T171, ["cs_handoff.reason"], ["cs_turn."]),
    (P12_TURN_T171, ["cs_turn.route", "cs_turn.handoff_reason"], ["cs_handoff."]),
])
def test_probe_names_exactly_the_outdated_table_t171(script, named, not_named):
    s = _p12_store_t171(script)
    with pytest.raises(objects.CsSchemaOutdated) as exc:
        objects.ensure_schema(s)
    for part in named:
        assert part in str(exc.value), part
    for part in not_named:
        assert part not in str(exc.value), part


def test_probe_is_silent_and_read_only_on_a_new_db_t171():
    s = _store_t171()
    objects.ensure_schema(s)                                  # 新库：不抛
    assert _cs_tables_t171(s) == {"cs_schema_version", "cs_conversation", "cs_turn",
                                  "cs_handoff", *NEW_TABLES_T171}
    before = objects.query(s, "SELECT name, sql FROM sqlite_master ORDER BY name")
    changes = s._conn.total_changes
    statements: list[str] = []
    s._conn.set_trace_callback(statements.append)
    try:
        objects.ensure_schema(s)
        objects.ensure_schema(s)
    finally:
        s._conn.set_trace_callback(None)
    assert objects.query(s, "SELECT name, sql FROM sqlite_master ORDER BY name") == before
    assert s._conn.total_changes == changes
    writes = [st for st in statements
              if re.match(r"\s*(INSERT|UPDATE|DELETE|REPLACE|DROP|ALTER)\b", st, re.IGNORECASE)]
    assert writes == []
    assert any("sqlite_master" in st for st in statements)   # 探针真的读了建表原文


def _pg_def_t171(column: str, values) -> str:
    """PG ``pg_get_constraintdef`` 对 ``CHECK (col IN (...))`` 的规范化输出形状。"""
    return ("CHECK ((" + column + " = ANY (ARRAY["
            + ", ".join(f"'{v}'::text" for v in values) + "])))")


class _FakePgConn_t171:
    """只够探针用的 PG 连接替身：按表名回 ``pg_constraint`` 的约束定义。"""
    dialect = POSTGRES

    def __init__(self, defs: dict[str, list[str]]):
        self.defs = defs
        self.calls: list[tuple[str, tuple]] = []

    def query(self, sql, params=()):
        self.calls.append((sql, tuple(params)))
        return [{"body": b} for b in self.defs.get(params[0], [])]


def test_probe_reads_pg_constraint_metadata_t171():
    p12_routes = ("answer", "fallback", "handoff", "silent")
    p12_reasons = T.HANDOFF_REASONS[:9]
    assert "refund_request" not in p12_reasons
    old = _FakePgConn_t171({
        "cs_turn": [_pg_def_t171("route", p12_routes),
                    _pg_def_t171("handoff_reason", ("",) + p12_reasons)],
        "cs_handoff": [_pg_def_t171("reason", T.HANDOFF_REASONS),
                       _pg_def_t171("delivery", T.DELIVERIES)],
    })
    with pytest.raises(objects.CsSchemaOutdated) as exc:
        objects._probe_enum_checks(old)
    assert "cs_turn.route" in str(exc.value) and "cs_turn.handoff_reason" in str(exc.value)
    assert "cs_handoff." not in str(exc.value)
    # 走的是 pg_constraint（等价元数据、只读），不是 sqlite_master；参数只有表名。
    assert {p for _sql, p in old.calls} == {("cs_turn",), ("cs_handoff",)}
    for sql, _p in old.calls:
        assert "pg_constraint" in sql and "sqlite_master" not in sql
        assert not re.match(r"\s*(INSERT|UPDATE|DELETE|SAVEPOINT)", sql, re.IGNORECASE)
        assert to_pg_sql(sql, lambda t: (), with_params=True).count("%s") == 1

    fresh = _FakePgConn_t171({
        "cs_turn": [_pg_def_t171("route", T.ROUTES),
                    _pg_def_t171("handoff_reason", ("",) + T.HANDOFF_REASONS)],
        "cs_handoff": [_pg_def_t171("reason", T.HANDOFF_REASONS)],
    })
    objects._probe_enum_checks(fresh)                         # 新形状：不抛
    objects._probe_enum_checks(_FakePgConn_t171({}))          # 表不存在（新库）：不抛


# ====================================================================== 观察
def test_record_observation_accepts_worded_statuses_and_numbers_them_t171():
    s = _store_t171()
    conv = _open_t171(s)
    tid, _seq = C.allocate_turn(s, conv)
    statuses = (P.ORDER_PAID, P.ORDER_SHIPPED, P.ORDER_CANCELLED)
    ids = [R.record_observation(s, conv, turn_id=tid, result=_ok_t171(st), now=NOW_T171)
           for st in statuses]
    assert ids == [P.observation_id_for(tid, n) for n in (1, 2, 3)]
    assert ids == [f"{tid}-o01", f"{tid}-o02", f"{tid}-o03"]
    assert R.turn_observation_ids(s, conversation_id=conv.conversation_id,
                                  turn_id=tid) == frozenset(ids)
    rows = R.observations_for_turn(s, conversation_id=conv.conversation_id, turn_id=tid)
    assert list(rows) == ids
    assert rows[ids[1]] == {
        "tenant_id": TENANT_T171, "observation_id": ids[1],
        "conversation_id": conv.conversation_id, "turn_id": tid, "kind": "order_lookup",
        "system_name": "demo-orders", "query_key": "gid-1001", "status": "shipped",
        "version": 3, "updated_at": "2026-09-20T08:00:00+00:00", "observed_at": NOW_T171}
    assert [rows[i]["status"] for i in ids] == list(statuses)
    assert _event_rows_t171(s) == []                          # 观察不落 event_log


@pytest.mark.parametrize("outcome,status", [
    *[(o, "") for o in P.LOOKUP_OUTCOMES if o != P.LOOKUP_OK],
    *[(o, P.ORDER_SHIPPED) for o in P.LOOKUP_OUTCOMES if o != P.LOOKUP_OK],
    (P.LOOKUP_OK, P.ORDER_AMENDED), (P.LOOKUP_OK, ""), (P.LOOKUP_OK, "delivered"),
    (P.LOOKUP_OK, "SHIPPED"), (P.LOOKUP_OK, "refunded"), ("OK", P.ORDER_PAID),
])
def test_record_observation_rejects_failed_or_unworded_lookups_t171(outcome, status):
    s = _store_t171()
    conv = _open_t171(s)
    tid, _seq = C.allocate_turn(s, conv)
    bad = P.LookupResult(outcome=outcome, system_name="demo-orders", query_key="gid-1",
                         status=status, error_kind="KeyError")
    with pytest.raises(ValueError):
        R.record_observation(s, conv, turn_id=tid, result=bad)
    assert _count_t171(s, "cs_observation") == 0
    assert R.turn_observation_ids(s, conversation_id=conv.conversation_id,
                                  turn_id=tid) == frozenset()


def test_record_observation_rejects_a_turn_of_another_conversation_t171():
    s = _store_t171()
    conv = _open_t171(s)
    other = _open_t171(s, external_userid="wm_t171_b")
    other_tid, _ = C.allocate_turn(s, other)
    for tid in (other_tid, "t-free-text", conv.conversation_id + "-tXX", ""):
        with pytest.raises(ValueError):
            R.record_observation(s, conv, turn_id=tid, result=_ok_t171())
    assert _count_t171(s, "cs_observation") == 0


def test_observations_read_back_only_this_turn_of_this_conversation_t171():
    s = _store_t171()
    a = _open_t171(s)
    b = _open_t171(s, external_userid="wm_t171_b")                      # 同租户另一位客户
    c = _open_t171(s, tenant_id="tnt-t171-other")                       # 另一个租户、同一位客户
    a1, _ = C.allocate_turn(s, a)
    a2, _ = C.allocate_turn(s, a)
    b1, _ = C.allocate_turn(s, b)
    c1, _ = C.allocate_turn(s, c)
    got = {
        "a1": [R.record_observation(s, a, turn_id=a1, result=_ok_t171(P.ORDER_PAID)),
               R.record_observation(s, a, turn_id=a1, result=_ok_t171(P.ORDER_SHIPPED))],
        "a2": [R.record_observation(s, a, turn_id=a2, result=_ok_t171(P.ORDER_CANCELLED))],
        "b1": [R.record_observation(s, b, turn_id=b1, result=_ok_t171(P.ORDER_PAID))],
        "c1": [R.record_observation(s, c, turn_id=c1, result=_ok_t171(P.ORDER_PAID))],
    }
    assert got["a2"] == [f"{a2}-o01"]                                   # 序号按轮次从 1 起
    assert got["b1"] == [f"{b1}-o01"] and got["c1"] == [f"{c1}-o01"]
    for conv, tid, key in ((a, a1, "a1"), (a, a2, "a2"), (b, b1, "b1"), (c, c1, "c1")):
        assert R.turn_observation_ids(s, conversation_id=conv.conversation_id,
                                      turn_id=tid) == frozenset(got[key]), key
        rows = R.observations_for_turn(s, conversation_id=conv.conversation_id, turn_id=tid)
        assert set(rows) == set(got[key]), key
        assert {r["conversation_id"] for r in rows.values()} == {conv.conversation_id}
    # 别的会话的轮次、本会话还没落观察的轮次，都读不到。
    a3, _ = C.allocate_turn(s, a)
    assert R.turn_observation_ids(s, conversation_id=a.conversation_id, turn_id=a3) == frozenset()
    assert R.turn_observation_ids(s, conversation_id=b.conversation_id, turn_id=a1) == frozenset()
    assert R.observations_for_turn(s, conversation_id=a.conversation_id, turn_id=b1) == {}


def test_record_observation_numbers_are_unique_under_threads_t171():
    s = _store_t171()
    conv = _open_t171(s)
    tid, _ = C.allocate_turn(s, conv)
    ids: list[str] = []
    errors: list[BaseException] = []
    lock = threading.Lock()
    start = threading.Barrier(8)

    def worker():
        try:
            start.wait()
            for _ in range(3):
                oid = R.record_observation(s, conv, turn_id=tid, result=_ok_t171())
                with lock:
                    ids.append(oid)
        except BaseException as exc:                                    # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert sorted(ids) == [P.observation_id_for(tid, n) for n in range(1, 25)]
    assert len(R.turn_observation_ids(s, conversation_id=conv.conversation_id, turn_id=tid)) == 24


# ====================================================================== 绑定与核验
def test_binding_verifier_satisfies_the_identity_port_t171():
    assert isinstance(I.BindingVerifier(), P.IdentityVerifier)


def test_normalize_display_no_only_trims_and_folds_fullwidth_t171():
    assert I.normalize_display_no("Ａ１００１") == "A1001"
    assert I.normalize_display_no("　 Ａ－１００１\t\n") == "A-1001"
    assert I.normalize_display_no("ａ1001") == "a1001"                   # 不做大小写折叠
    assert I.normalize_display_no("A 1001") == "A 1001"                 # 不去中间空白
    assert I.normalize_display_no("①001") == "①001"                     # 不做 NFKC
    assert I.normalize_display_no("") == "" and I.normalize_display_no("  　") == ""


@pytest.mark.parametrize("typed,hit", [
    ("A1001", True), ("  A1001\t", True), ("Ａ１００１", True), ("　Ａ１００１　", True),
    ("a1001", False), ("A 1001", False), ("A10011", False), ("A100", False), ("", False),
    ("   ", False), ("A1001.", False),
])
def test_resolve_matches_exactly_after_normalization_t171(typed, hit):
    s = _store_t171()
    R.upsert_binding(s, _binding_t171(bound_at=NOW_T171))
    got = _resolve_t171(s, typed)
    if hit:
        assert got == _binding_t171(bound_at=NOW_T171)
    else:
        assert got is None


def test_resolve_is_closed_across_tenant_channel_and_customer_t171():
    s = _store_t171()
    R.upsert_binding(s, _binding_t171())
    assert _resolve_t171(s, "A1001") is not None                         # 判据不空转
    assert _resolve_t171(s, "A1001", tenant_id="tnt-t171-other") is None
    assert _resolve_t171(s, "A1001", channel="feishu") is None
    assert _resolve_t171(s, "A1001", external_userid="wm_t171_b") is None
    assert _resolve_t171(s, "A1001", external_userid="WM_T171_A") is None
    for empty in ({"tenant_id": ""}, {"channel": ""}, {"external_userid": ""}):
        assert _resolve_t171(s, "A1001", **empty) is None, empty
    # 空租户的绑定根本写不进来（不猜默认租户）。
    with pytest.raises(ValueError):
        R.upsert_binding(s, _binding_t171(tenant_id=""))
    assert _resolve_t171(s, "A1001", tenant_id="") is None


def test_resolve_on_an_empty_db_is_none_not_an_error_t171():
    s = _store_t171()
    assert _resolve_t171(s, "A1001") is None


def test_upsert_binding_validates_normalizes_and_overwrites_t171():
    s = _store_t171()
    for bad in ({"source": "external"}, {"source": ""}, {"source": "Seed"},
                {"channel": ""}, {"external_userid": " "}, {"display_no": "　"},
                {"system_name": ""}, {"query_key": ""}):
        with pytest.raises(ValueError):
            R.upsert_binding(s, _binding_t171(**bad))
    objects.ensure_schema(s)                          # 校验在建表之前：上面一句都没碰库
    assert _count_t171(s, "cs_order_binding") == 0
    R.upsert_binding(s, _binding_t171(display_no=" Ｂ２００２ "))
    rows = objects.query(s, "SELECT display_no, bound_at, source FROM cs_order_binding")
    assert [r["display_no"] for r in rows] == ["B2002"]                 # 存规范形
    assert rows[0]["bound_at"] and rows[0]["source"] == P.BINDING_TEST
    R.upsert_binding(s, _binding_t171(display_no="B2002", system_name="shop-b",
                                      query_key="gid-b", source=P.BINDING_INTERNAL,
                                      bound_at=LATER_T171))
    assert _count_t171(s, "cs_order_binding") == 1                      # 同主键覆盖
    assert _resolve_t171(s, "B2002") == _binding_t171(
        display_no="B2002", system_name="shop-b", query_key="gid-b", source=P.BINDING_INTERNAL,
        bound_at=LATER_T171)
    assert _event_rows_t171(s) == []


def _write_seed_t171(tmp_path, doc, name="bindings.json"):
    path = tmp_path / name
    path.write_text(doc if isinstance(doc, str) else json.dumps(doc, ensure_ascii=False),
                    encoding="utf-8")
    return path


def test_load_bindings_file_roundtrip_t171(tmp_path):
    s = _store_t171()
    path = _write_seed_t171(tmp_path, {"_note": "演示种子", "bindings": [
        {"tenant_id": TENANT_T171, "channel": "wechat_kf", "external_userid": "wm_t171_a",
         "display_no": "A1001", "system_name": "demo-orders", "query_key": "gid-1001"},
        {"tenant_id": TENANT_T171, "channel": "wechat_kf", "external_userid": "wm_t171_b",
         "display_no": "Ａ２００２", "system_name": "demo-orders", "query_key": "gid-2002",
         "source": "internal", "bound_at": NOW_T171},
    ]})
    assert R.load_bindings_file(s, path) == 2
    assert R.load_bindings_file(s, str(path)) == 2                      # 幂等，路径字符串也认
    assert _count_t171(s, "cs_order_binding") == 2
    first = _resolve_t171(s, "A1001")
    assert (first.source, first.query_key) == (P.BINDING_SEED, "gid-1001")   # source 缺省 seed
    second = _resolve_t171(s, "A2002", external_userid="wm_t171_b")
    assert second == P.Binding(tenant_id=TENANT_T171, channel="wechat_kf",
                               external_userid="wm_t171_b", display_no="A2002",
                               system_name="demo-orders", query_key="gid-2002",
                               source=P.BINDING_INTERNAL, bound_at=NOW_T171)
    assert _event_rows_t171(s) == []


_GOOD_ENTRY_T171 = {"tenant_id": TENANT_T171, "channel": "wechat_kf", "external_userid": "wm_1",
                    "display_no": "A1", "system_name": "s", "query_key": "q"}


@pytest.mark.parametrize("doc", [
    "{ 不是 JSON",
    "",
    json.dumps([_GOOD_ENTRY_T171]),
    json.dumps({"bindings": {"a": 1}}),
    json.dumps({"entries": [_GOOD_ENTRY_T171]}),
    json.dumps({"bindings": [_GOOD_ENTRY_T171, "A1001"]}),
    json.dumps({"bindings": [_GOOD_ENTRY_T171, {k: v for k, v in _GOOD_ENTRY_T171.items()
                                                if k != "query_key"}]}),
    json.dumps({"bindings": [_GOOD_ENTRY_T171, {**_GOOD_ENTRY_T171, "extenal_userid": "x"}]}),
    json.dumps({"bindings": [_GOOD_ENTRY_T171, {**_GOOD_ENTRY_T171, "display_no": "  "}]}),
    json.dumps({"bindings": [_GOOD_ENTRY_T171, {**_GOOD_ENTRY_T171, "source": "wechat_kf"}]}),
    json.dumps({"bindings": [_GOOD_ENTRY_T171, {**_GOOD_ENTRY_T171, "query_key": 1001}]}),
    json.dumps({"bindings": [_GOOD_ENTRY_T171, {**_GOOD_ENTRY_T171, "tenant_id": ""}]}),
])
def test_load_bindings_file_bad_file_names_the_file_and_writes_nothing_t171(tmp_path, doc):
    s = _store_t171()
    path = _write_seed_t171(tmp_path, doc, name="坏种子_t171.json")
    with pytest.raises(ValueError) as exc:
        R.load_bindings_file(s, path)
    assert str(path) in str(exc.value)
    objects.ensure_schema(s)
    assert _count_t171(s, "cs_order_binding") == 0                      # 一条都不写


def test_load_bindings_file_missing_file_is_a_value_error_t171(tmp_path):
    path = tmp_path / "没有这个文件_t171.json"
    with pytest.raises(ValueError) as exc:
        R.load_bindings_file(_store_t171(), path)
    assert str(path) in str(exc.value)


# ====================================================================== 槽位
def test_set_slot_validates_keys_sources_and_closed_values_t171():
    s = _store_t171()
    conv = _open_t171(s)
    tid, _ = C.allocate_turn(s, conv)
    bad_calls = [
        dict(key="phone", value="138", source=P.SLOT_SOURCE_RULE),
        dict(key="Order_no", value="A1", source=P.SLOT_SOURCE_RULE),
        dict(key=P.SLOT_ORDER_NO, value="A1", source="llm"),
        dict(key=P.SLOT_REQUEST, value="refund_now", source=P.SLOT_SOURCE_RULE),
        dict(key=P.SLOT_REQUEST, value="Refund", source=P.SLOT_SOURCE_MODEL),
        dict(key=P.SLOT_EMOTION, value="furious", source=P.SLOT_SOURCE_RULE),
        dict(key=P.SLOT_PRODUCT, value="", source=P.SLOT_SOURCE_RULE),
        dict(key=P.SLOT_PROBLEM, value="  ", source=P.SLOT_SOURCE_RULE),
    ]
    for kw in bad_calls:
        with pytest.raises(ValueError):
            R.set_slot(s, conv, turn_id=tid, **kw)
    with pytest.raises(ValueError):
        R.set_slot(s, conv, key=P.SLOT_ORDER_NO, value="A1", turn_id="t-other",
                   source=P.SLOT_SOURCE_RULE)
    assert R.get_slots(s, conv.tenant_id, conv.conversation_id) == {}
    for value in P.REQUEST_VALUES:
        R.set_slot(s, conv, key=P.SLOT_REQUEST, value=value, turn_id=tid,
                   source=P.SLOT_SOURCE_RULE)
        assert R.get_slots(s, conv.tenant_id, conv.conversation_id)[P.SLOT_REQUEST] == value
    for value in P.EMOTION_VALUES:
        R.set_slot(s, conv, key=P.SLOT_EMOTION, value=value, turn_id=tid,
                   source=P.SLOT_SOURCE_MODEL)
        assert R.get_slots(s, conv.tenant_id, conv.conversation_id)[P.SLOT_EMOTION] == value
    for key in (P.SLOT_ORDER_NO, P.SLOT_PRODUCT, P.SLOT_PROBLEM):         # 自由文本槽位
        R.set_slot(s, conv, key=key, value="任意 文本 #1", turn_id=tid,
                   source=P.SLOT_SOURCE_RULE)
    assert list(R.get_slots(s, conv.tenant_id, conv.conversation_id)) == list(P.SLOT_KEYS)
    assert _event_rows_t171(s) == []


def test_slots_accumulate_across_turns_and_new_value_wins_t171():
    s = _store_t171()
    conv = _open_t171(s)
    other = _open_t171(s, external_userid="wm_t171_b")
    t1, _ = C.allocate_turn(s, conv)
    t2, _ = C.allocate_turn(s, conv)
    R.set_slot(s, conv, key=P.SLOT_ORDER_NO, value="A1001", turn_id=t1,
               source=P.SLOT_SOURCE_RULE, now=NOW_T171)
    R.set_slot(s, conv, key=P.SLOT_REQUEST, value=P.REQUEST_REFUND, turn_id=t1,
               source=P.SLOT_SOURCE_RULE, now=NOW_T171)
    R.set_slot(s, conv, key=P.SLOT_PRODUCT, value="耳机", turn_id=t2,
               source=P.SLOT_SOURCE_MODEL, now=LATER_T171)
    R.set_slot(s, conv, key=P.SLOT_ORDER_NO, value="A2002", turn_id=t2,
               source=P.SLOT_SOURCE_MODEL, now=LATER_T171)
    slots = R.get_slots(s, conv.tenant_id, conv.conversation_id)
    assert slots == {"order_no": "A2002", "product": "耳机", "request": "refund"}
    assert list(slots) == ["order_no", "product", "request"]            # SLOT_KEYS 的顺序
    rows = objects.query(s, "SELECT slot_key, turn_id, source, updated_at FROM cs_slot"
                            " WHERE conversation_id=? ORDER BY slot_key", (conv.conversation_id,))
    assert [(r["slot_key"], r["turn_id"], r["source"], r["updated_at"]) for r in rows] == [
        ("order_no", t2, "model", LATER_T171), ("product", t2, "model", LATER_T171),
        ("request", t1, "rule", NOW_T171)]
    assert R.get_slots(s, other.tenant_id, other.conversation_id) == {}
    assert R.get_slots(s, "tnt-t171-other", conv.conversation_id) == {}


# ====================================================================== 轮次扩展与追问次数
def test_ask_count_counts_turn_ext_rows_per_slot_t171():
    s = _store_t171()
    conv = _open_t171(s)
    other = _open_t171(s, external_userid="wm_t171_b")
    turns = [C.allocate_turn(s, conv)[0] for _ in range(5)]
    R.record_turn_ext(s, conv, turn_id=turns[0], lang=P.LANG_ZH, ask_slot=P.SLOT_ORDER_NO,
                      ask_count=1)
    R.record_turn_ext(s, conv, turn_id=turns[1], lang=P.LANG_ZH, ask_slot=P.SLOT_ORDER_NO,
                      ask_count=2)
    R.record_turn_ext(s, conv, turn_id=turns[2], lang=P.LANG_EN, ask_slot=P.SLOT_PRODUCT,
                      ask_count=1)
    R.record_turn_ext(s, conv, turn_id=turns[3], lang=P.LANG_EN, lookup_outcome=P.LOOKUP_OK)
    R.record_turn_ext(s, conv, turn_id=turns[4], lang=P.LANG_ZH,
                      lookup_outcome=P.LOOKUP_NOT_FOUND)
    o1, _ = C.allocate_turn(s, other)
    R.record_turn_ext(s, other, turn_id=o1, lang=P.LANG_ZH, ask_slot=P.SLOT_ORDER_NO,
                      ask_count=1)
    count = {k: R.ask_count(s, conv.tenant_id, conv.conversation_id, k) for k in P.SLOT_KEYS}
    assert count == {"order_no": 2, "product": 1, "problem": 0, "request": 0, "emotion": 0}
    assert R.ask_count(s, other.tenant_id, other.conversation_id, P.SLOT_ORDER_NO) == 1
    assert R.ask_count(s, "tnt-t171-other", conv.conversation_id, P.SLOT_ORDER_NO) == 0
    with pytest.raises(ValueError):
        R.ask_count(s, conv.tenant_id, conv.conversation_id, "phone")
    rows = objects.query(s, "SELECT turn_id, lang, lookup_outcome, ask_slot, ask_count"
                            " FROM cs_turn_ext WHERE conversation_id=? ORDER BY turn_id",
                         (conv.conversation_id,))
    assert [tuple(r.values()) for r in rows] == [
        (turns[0], "zh", "", "order_no", 1), (turns[1], "zh", "", "order_no", 2),
        (turns[2], "en", "", "product", 1), (turns[3], "en", "ok", "", 0),
        (turns[4], "zh", "not_found", "", 0)]
    assert _event_rows_t171(s) == []


def test_record_turn_ext_validates_and_is_one_row_per_turn_t171():
    s = _store_t171()
    conv = _open_t171(s)
    tid, _ = C.allocate_turn(s, conv)
    for kw in (dict(lang="fr"), dict(lang=""), dict(lang="zh", lookup_outcome="error"),
               dict(lang="zh", ask_slot="phone", ask_count=1),
               dict(lang="zh", ask_slot=P.SLOT_ORDER_NO, ask_count=-1),
               dict(lang="zh", ask_slot=P.SLOT_ORDER_NO, ask_count=True),
               dict(lang="zh", ask_slot=P.SLOT_ORDER_NO, ask_count="1"),
               dict(lang="zh", ask_count=1)):
        with pytest.raises(ValueError):
            R.record_turn_ext(s, conv, turn_id=tid, **kw)
    with pytest.raises(ValueError):
        R.record_turn_ext(s, conv, turn_id="t-other", lang="zh")
    assert _count_t171(s, "cs_turn_ext") == 0
    R.record_turn_ext(s, conv, turn_id=tid, lang="en", lookup_outcome=P.LOOKUP_AMENDED)
    with pytest.raises(sqlite3.IntegrityError):
        R.record_turn_ext(s, conv, turn_id=tid, lang="zh")
    assert _count_t171(s, "cs_turn_ext") == 1


# ====================================================================== 退款桥
def test_record_bridge_roundtrip_t171():
    s = _store_t171()
    conv = _open_t171(s)
    t1, _ = C.allocate_turn(s, conv)
    t2, _ = C.allocate_turn(s, conv)
    R.record_bridge(s, conv, turn_id=t1, order_no="A1001", now=NOW_T171,
                    result=P.PrecheckResult(ok=True, decision="approve", rule_ref="R-7",
                                            reason_code="quality",
                                            command_line="/refund A1001 quality",
                                            summary="质量问题，规则 R-7 可退"))
    R.record_bridge(s, conv, turn_id=t2, order_no="A1001", now=LATER_T171,
                    result=P.PrecheckResult(ok=False, refused_why="台账里没有这一单"))
    rows = objects.query(s, "SELECT * FROM cs_refund_bridge ORDER BY bridge_id")
    assert rows == [
        {"tenant_id": TENANT_T171, "bridge_id": t1, "conversation_id": conv.conversation_id,
         "turn_id": t1, "order_no": "A1001", "ok": 1, "decision": "approve", "rule_ref": "R-7",
         "reason_code": "quality", "command_line": "/refund A1001 quality", "refused_why": "",
         "created_at": NOW_T171},
        {"tenant_id": TENANT_T171, "bridge_id": t2, "conversation_id": conv.conversation_id,
         "turn_id": t2, "order_no": "A1001", "ok": 0, "decision": "", "rule_ref": "",
         "reason_code": "", "command_line": "", "refused_why": "台账里没有这一单",
         "created_at": LATER_T171},
    ]
    with pytest.raises(sqlite3.IntegrityError):                          # 一轮至多一行
        R.record_bridge(s, conv, turn_id=t1, order_no="A1001", result=P.PrecheckResult(ok=False))
    for kw in (dict(turn_id=t1, order_no=""), dict(turn_id=t1, order_no="  "),
               dict(turn_id="t-other", order_no="A1001")):
        with pytest.raises(ValueError):
            R.record_bridge(s, conv, result=P.PrecheckResult(ok=False), **kw)
    assert _count_t171(s, "cs_refund_bridge") == 2
    assert _event_rows_t171(s) == []


# ====================================================================== record_turn 的 p13 增量
def test_clarify_does_not_move_fallback_streak_t171():
    s = _store_t171()
    conv = _open_t171(s)
    plan = [(T.ROUTE_CLARIFY, 0), (T.ROUTE_FALLBACK, 1), (T.ROUTE_CLARIFY, 1),
            (T.ROUTE_CLARIFY, 1), (T.ROUTE_SILENT, 1), (T.ROUTE_FALLBACK, 2),
            (T.ROUTE_CLARIFY, 2), (T.ROUTE_ANSWER, 0), (T.ROUTE_CLARIFY, 0)]
    for route, expected in plan:
        _tid, conv = _record_t171(s, conv, route)
        assert conv.fallback_streak == expected, route
    details = [r["detail"] for r in s.list_event_log(T.plan_id_for(conv.conversation_id))]
    assert [d["route"] for d in details] == [r for r, _ in plan]
    assert [d["fallback_streak"] for d in details] == [e for _, e in plan]
    routes = [r["route"] for r in objects.query(s, "SELECT route FROM cs_turn ORDER BY seq")]
    assert routes == [r for r, _ in plan]                                # CHECK 认 clarify


DETAIL_KEYS_T171 = {"seq", "route", "intent", "handoff_reason", "stage", "inbound_digest",
                    "reply_digest", "citations", "claim_count", "check_ok", "violation_kinds",
                    "fallback_streak", "lang", "lookup_outcome", "observation_count",
                    "slot_count"}


def test_turn_recorded_detail_carries_p13_keys_counted_from_db_t171():
    s = _store_t171()
    conv = _open_t171(s)
    other = _open_t171(s, external_userid="wm_t171_b")
    t1, seq1 = C.allocate_turn(s, conv)
    o1, _ = C.allocate_turn(s, other)
    R.set_slot(s, conv, key=P.SLOT_ORDER_NO, value="A1001", turn_id=t1, source="rule")
    R.set_slot(s, conv, key=P.SLOT_REQUEST, value="track", turn_id=t1, source="rule")
    R.set_slot(s, other, key=P.SLOT_PRODUCT, value="耳机", turn_id=o1, source="rule")
    R.record_observation(s, conv, turn_id=t1, result=_ok_t171())
    R.record_observation(s, other, turn_id=o1, result=_ok_t171())       # 别的会话同序号的轮
    R.record_observation(s, other, turn_id=o1, result=_ok_t171())
    _t1, conv = _record_t171(s, conv, T.ROUTE_ANSWER, turn_id=t1, seq=seq1, lang="en",
                             lookup_outcome=P.LOOKUP_OK)
    t2, conv = _record_t171(s, conv, T.ROUTE_CLARIFY)                   # p12 式：不传两个新参数
    R.set_slot(s, conv, key=P.SLOT_PRODUCT, value="耳机", turn_id=t2, source="model")
    t3, conv = _record_t171(s, conv, T.ROUTE_HANDOFF, lookup_outcome=P.LOOKUP_NOT_FOUND)
    rows = s.list_event_log(T.plan_id_for(conv.conversation_id))
    assert [r["task_id"] for r in rows] == [t1, t2, t3]
    for r in rows:
        assert set(r["detail"]) == DETAIL_KEYS_T171
    got = [(r["detail"]["lang"], r["detail"]["lookup_outcome"], r["detail"]["observation_count"],
            r["detail"]["slot_count"]) for r in rows]
    assert got == [("en", "ok", 1, 2), ("zh", "", 0, 2), ("zh", "not_found", 0, 3)]


def test_record_turn_takes_no_counts_from_the_caller_and_validates_enums_t171():
    s = _store_t171()
    conv = _open_t171(s)
    tid, seq = C.allocate_turn(s, conv)
    base = dict(turn_id=tid, seq=seq, msg_dedup_key="", inbound_text="x", reply_text="y",
                route=T.ROUTE_ANSWER, intent=T.INTENT_GENERAL, handoff_reason="",
                draft=T.ReplyDraft(text="y"), check=T.CheckResult(ok=True))
    for extra in ({"observation_count": 5}, {"slot_count": 3}):
        with pytest.raises(TypeError):
            C.record_turn(s, conv, **base, **extra)
    for extra in ({"lang": "fr"}, {"lang": ""}, {"lookup_outcome": "error"},
                  {"lookup_outcome": "OK"}):
        with pytest.raises(ValueError):
            C.record_turn(s, conv, **base, **extra)
    assert _count_t171(s, "cs_turn") == 0 and _event_rows_t171(s) == []
    C.record_turn(s, conv, **base, lang="en", lookup_outcome=P.LOOKUP_PLATFORM_ERROR)
    assert _count_t171(s, "cs_turn") == 1


# ====================================================================== R5 哨兵
SNTL_ORDER_T171 = "SNTL-ORDER-A7Q2"
SNTL_DISPLAY_T171 = "SNTL-DNO-B9K4"
SNTL_SEED_DISPLAY_T171 = "SNTL-SEEDNO-C3"
SNTL_QUERY_KEY_T171 = "gid://SNTL-QKEY-Z4"
SNTL_SYSTEM_T171 = "SNTL-SYS-orders"
SNTL_PRODUCT_T171 = "哨兵商品·松鼠牌降噪耳机 SNTL-PROD-K8"
SNTL_PROBLEM_T171 = "哨兵问题·左耳没声音 SNTL-PROB-M3"
SNTL_RULE_T171 = "SNTL-RULE-R1"
SNTL_CMD_T171 = f"/refund {SNTL_ORDER_T171} quality"
SNTL_REFUSED_T171 = "哨兵拒绝原因·台账里查无此单 SNTL-REFUSED-P5"
SNTL_USER_T171 = "wm_SNTL_T171_USERID_VQXR"


def test_record_writes_never_reach_event_log_t171(tmp_path):
    s = _store_t171()
    conv = _open_t171(s, external_userid=SNTL_USER_T171)
    R.upsert_binding(s, _binding_t171(external_userid=SNTL_USER_T171,
                                      display_no=SNTL_DISPLAY_T171,
                                      system_name=SNTL_SYSTEM_T171,
                                      query_key=SNTL_QUERY_KEY_T171))
    seed = _write_seed_t171(tmp_path, {"bindings": [
        {"tenant_id": TENANT_T171, "channel": "wechat_kf", "external_userid": SNTL_USER_T171,
         "display_no": SNTL_SEED_DISPLAY_T171, "system_name": SNTL_SYSTEM_T171,
         "query_key": SNTL_QUERY_KEY_T171}]})
    assert R.load_bindings_file(s, seed) == 1
    assert _resolve_t171(s, SNTL_DISPLAY_T171, external_userid=SNTL_USER_T171) is not None
    t1, seq1 = C.allocate_turn(s, conv)
    for key, value in ((P.SLOT_ORDER_NO, SNTL_ORDER_T171), (P.SLOT_PRODUCT, SNTL_PRODUCT_T171),
                       (P.SLOT_PROBLEM, SNTL_PROBLEM_T171), (P.SLOT_REQUEST, P.REQUEST_REFUND),
                       (P.SLOT_EMOTION, P.EMOTION_UPSET)):
        R.set_slot(s, conv, key=key, value=value, turn_id=t1, source=P.SLOT_SOURCE_RULE)
    R.record_observation(s, conv, turn_id=t1, result=_ok_t171(
        system_name=SNTL_SYSTEM_T171, query_key=SNTL_QUERY_KEY_T171))
    R.record_turn_ext(s, conv, turn_id=t1, lang=P.LANG_ZH, lookup_outcome=P.LOOKUP_OK)
    R.record_bridge(s, conv, turn_id=t1, order_no=SNTL_ORDER_T171, result=P.PrecheckResult(
        ok=True, decision="approve", rule_ref=SNTL_RULE_T171, reason_code="quality",
        command_line=SNTL_CMD_T171, summary="摘要"))
    assert _event_rows_t171(s) == []                  # 这些写入本身一条审计行都不落
    conv = C.record_turn(s, conv, turn_id=t1, seq=seq1, msg_dedup_key="",
                         inbound_text=f"我要退款，单号 {SNTL_ORDER_T171}",
                         reply_text="已为您转人工处理", route=T.ROUTE_HANDOFF,
                         intent=T.INTENT_REFUND_PAYMENT,
                         handoff_reason=T.HANDOFF_REFUND_REQUEST,
                         draft=T.ReplyDraft(text="已为您转人工处理"),
                         check=T.CheckResult(ok=True), lookup_outcome=P.LOOKUP_OK)
    t2, seq2 = C.allocate_turn(s, conv)
    R.record_bridge(s, conv, turn_id=t2, order_no=SNTL_ORDER_T171,
                    result=P.PrecheckResult(ok=False, refused_why=SNTL_REFUSED_T171))
    C.record_turn(s, conv, turn_id=t2, seq=seq2, msg_dedup_key="", inbound_text="人呢",
                  reply_text="", route=T.ROUTE_SILENT, intent=T.INTENT_UNKNOWN,
                  handoff_reason="", draft=T.ReplyDraft(text=""), check=T.CheckResult(ok=True))

    raw = _event_rows_t171(s)
    assert [r["event_type"] for r in raw] == [T.EVENT_TURN_RECORDED] * 2   # 没有新事件类型
    first = json.loads(raw[0]["detail"])
    assert (first["observation_count"], first["slot_count"], first["lookup_outcome"]) == (1, 5, "ok")
    # 判据不空转：每个哨兵确实住在 cs_ 表里。
    table_dump = json.dumps({t: objects.query(s, f"SELECT * FROM {t}")
                             for t in ("cs_turn",) + NEW_TABLES_T171}, ensure_ascii=False)
    needles = (SNTL_ORDER_T171, SNTL_DISPLAY_T171, SNTL_SEED_DISPLAY_T171, SNTL_QUERY_KEY_T171,
               SNTL_SYSTEM_T171, SNTL_PRODUCT_T171, SNTL_PROBLEM_T171, SNTL_RULE_T171,
               SNTL_CMD_T171, SNTL_REFUSED_T171, SNTL_USER_T171)
    for needle in needles:
        assert needle in table_dump, needle
    dumps = (json.dumps(raw, ensure_ascii=False), json.dumps(raw, ensure_ascii=True),
             json.dumps(s.list_event_log(T.plan_id_for(conv.conversation_id)), ensure_ascii=False),
             "\n".join(str(v) for r in raw for v in r.values()))
    fragments = ("SNTL-ORDER", "SNTL-DNO", "SNTL-SEEDNO", "SNTL-QKEY", "SNTL-SYS", "SNTL-PROD",
                 "SNTL-PROB", "SNTL-RULE", "SNTL-REFUSED", "松鼠牌", "左耳", "查无此单",
                 SNTL_USER_T171[-6:])
    for dump in dumps:
        for needle in needles + fragments:
            assert needle not in dump, needle
