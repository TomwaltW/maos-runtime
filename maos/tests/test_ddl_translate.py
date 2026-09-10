"""T115 的机器验收（一）—— **DDL 翻译器**。不需要 PG，进缺省全量。

## 这一束在守什么

本轨的题眼是「**不许手写第二份 DDL**」。同波次还有三条轨在往退款域加表加列
（跨轨契约 §B），PG 侧要是靠人手抄一份 DDL，那三轨的片段一落地就漂 —— 而漂了
**只在配了 PG 的机器上、只在跑到那张表时**才炸（`relation does not exist`）。
所以 PG 侧的建表由 `_dbport.to_pg_ddl()` 从 SQLite 方言现翻，片段作者只写一份。

翻译器一旦漏翻一条，症状就是上面那种。所以这里逐条钉：

1. **16 张退款表 + 知识层两张表真的翻得出来**，且翻出来的东西形状对
   （类型映射、生成列、自增主键、约束原样）。
2. **不认识的构造当场抛 `UnsupportedDdlError`，且报错里带原文** —— 不猜、不静默
   跳过。静默跳过等于 PG 上少一张表/少一个约束，而少了只在跑的时候才知道。
3. **唯一允许的跳过是 FTS5 虚表**，别的虚表照样抛。
4. **占位符翻译认引号**：`WHERE note LIKE '%?%'` 里那个 `?` 一个字都不许动。
   这条是 `docs/DECISIONS.md` 2026-08-29「不翻译」那条决策的全部顾虑所在，
   本轨把「翻」这件事做成开关之后，它变成必须由测试钉住的东西。

不跑真库是**有意的**：翻译器是纯文本函数，它的正确性不该依赖谁的机器上起没起
Postgres。真库上的验收在 `test_refund_domain_pg.py` / `test_kb_pg_prefilter.py`。
"""

from __future__ import annotations

import pathlib
import re

import pytest

from maos.domain._dbport import (
    UnsupportedDdlError,
    rewrite_upsert,
    split_statements,
    to_pg_ddl,
    to_pg_sql,
    translate_placeholders,
)

_ROOT = pathlib.Path(__file__).resolve().parents[2]
REFUND_SCHEMA = _ROOT / "maos" / "domain" / "refund" / "schema.sql"
KB_SCHEMA = _ROOT / "maos" / "kb" / "schema.sql"

#: 退款域那 16 张表。**写死在这里而不是从文件里数**：这一条要守的正是
#: 「翻译器一张都没漏」，而从被翻译的那份文件里数表名等于拿嫌疑人当证人。
REFUND_TABLES = (
    "refund_schema_version", "tenant", "channel", "order_snapshot",
    "product_snapshot", "policy_rule", "refund_case", "customer_evidence",
    "approval_record", "finance_entry", "refund_request", "payment_observation",
    "notification", "compensation_record", "business_ref", "intake_annotation",
)


def _created_tables(ddl: str) -> set[str]:
    return set(re.findall(r"CREATE TABLE(?: IF NOT EXISTS)? (\w+)", ddl))


# ------------------------------------------------------------ 1. 真 schema
def test_refund_schema_translates_all_sixteen_tables() -> None:
    """16 张表一张不少，且带索引。少一张 = PG 上跑到那张表才炸。"""
    ddl = to_pg_ddl(REFUND_SCHEMA.read_text(encoding="utf-8"))

    assert _created_tables(ddl) == set(REFUND_TABLES), (
        "翻出来的表与 16 张对不上 —— 少的那张在 PG 上要等到某条 INSERT 才报"
        " relation does not exist")
    assert ddl.count("CREATE INDEX IF NOT EXISTS") == 4, "四条索引也要翻过去"


def test_real_type_becomes_double_precision() -> None:
    """PG 不认 `REAL` 这个 SQLite 拼法下的金额列。翻错的症状是建表就失败。"""
    ddl = to_pg_ddl(REFUND_SCHEMA.read_text(encoding="utf-8"))

    assert "amount_claimed double precision NOT NULL" in ddl
    assert "amount_paid double precision NOT NULL" in ddl
    assert not re.search(r"\bREAL\b", ddl), "一个 REAL 都不该剩下"


def test_constraints_and_defaults_survive_verbatim() -> None:
    """CHECK / PRIMARY KEY / UNIQUE / DEFAULT 字面量原样过去。

    这些是**约束**，掉一个不报错，只是库上少了一道拦截 —— 而 `refund_case` 那条
    CHECK 挡的正是「biz_status 写进一个不存在的状态」。
    """
    ddl = to_pg_ddl(REFUND_SCHEMA.read_text(encoding="utf-8"))

    assert "CHECK (biz_status IN ('submitted', 'approved', 'gateway_accepted'," in ddl
    assert "PRIMARY KEY (tenant_id, case_id)" in ddl
    assert "UNIQUE (tenant_id, idempotency_key)" in ddl
    assert "payload_json TEXT NOT NULL DEFAULT '{}'" in ddl
    assert "channel_scope TEXT NOT NULL DEFAULT '*'" in ddl
    assert "effective_to TEXT," in ddl, "可空列不许被塞上 NOT NULL"


def test_kb_schema_keeps_seven_prefilter_columns_and_id() -> None:
    """知识层：七维预过滤列 + rule_no / gateway_code / kind / outcome 一个不少。

    少一列的症状不是报错，是**阶段一在 PG 上按不同的维度过滤** —— 召回悄悄变了，
    而两边都不响。`id` 那条生成列同理：没有它 F-2 的 `SELECT id` 恒抛，
    检索器那条端口分支一次都走不到。
    """
    ddl = to_pg_ddl(KB_SCHEMA.read_text(encoding="utf-8"))

    for column in ("tenant_id", "biz_type", "channel_id", "region", "sku",
                   "policy_version", "workflow_version", "rule_no",
                   "gateway_code", "kind", "outcome"):
        assert re.search(rf"^\s+{column} ", ddl, re.MULTILINE), f"kb_doc 少了 {column}"
    assert "PRIMARY KEY (tenant_id, doc_id)" in ddl


def test_virtual_generated_column_becomes_stored() -> None:
    """PG 16 只有 STORED 生成列（VIRTUAL 要 PG 18）。翻不过去 = `id` 列建不出来。"""
    ddl = to_pg_ddl(KB_SCHEMA.read_text(encoding="utf-8"))

    assert "id TEXT GENERATED ALWAYS AS (tenant_id || ':' || doc_id) STORED" in ddl
    assert "VIRTUAL" not in ddl


# --------------------------------------------------- 2. 允许子集的逐条断言
@pytest.mark.parametrize(("sqlite_ddl", "expect"), [
    ("CREATE TABLE t (a TEXT)", "a TEXT"),
    ("CREATE TABLE t (a INTEGER)", "a INTEGER"),
    ("CREATE TABLE t (a REAL)", "a double precision"),
    ("CREATE TABLE t (a TEXT NOT NULL)", "a TEXT NOT NULL"),
    ("CREATE TABLE t (a TEXT DEFAULT 'x')", "a TEXT DEFAULT 'x'"),
    ("CREATE TABLE t (a INTEGER DEFAULT 0)", "a INTEGER DEFAULT 0"),
    ("CREATE TABLE t (a REAL DEFAULT -1.5)", "a double precision DEFAULT -1.5"),
    ("CREATE TABLE t (a TEXT UNIQUE)", "a TEXT UNIQUE"),
    ("CREATE TABLE t (a TEXT PRIMARY KEY)", "a TEXT PRIMARY KEY"),
    ("CREATE TABLE t (a TEXT CHECK (a IN ('x','y')))", "CHECK (a IN ('x','y'))"),
    ("CREATE TABLE t (a INTEGER PRIMARY KEY AUTOINCREMENT)", "a bigserial PRIMARY KEY"),
    ("CREATE TABLE t (a TEXT, b TEXT, PRIMARY KEY (a, b))", "PRIMARY KEY (a, b)"),
    ("CREATE TABLE t (a TEXT, FOREIGN KEY (a) REFERENCES u(a))",
     "FOREIGN KEY (a) REFERENCES u(a)"),
    ("CREATE TABLE t (a TEXT REFERENCES u(a))", "a TEXT REFERENCES u(a)"),
    ("CREATE TABLE IF NOT EXISTS t (a TEXT)", "CREATE TABLE IF NOT EXISTS t"),
    ("CREATE INDEX IF NOT EXISTS ix ON t(a, b)", "CREATE INDEX IF NOT EXISTS ix ON t(a, b)"),
    ("ALTER TABLE t ADD COLUMN c REAL DEFAULT 0", "ALTER TABLE t ADD COLUMN c double precision"),
    ("DROP TABLE t", "DROP TABLE t"),
])
def test_allowed_subset(sqlite_ddl: str, expect: str) -> None:
    """跨轨契约 §B.3 允许片段用的那个子集，逐条翻得出来。"""
    assert expect in to_pg_ddl(sqlite_ddl)


def test_comments_do_not_confuse_the_splitter() -> None:
    """注释里的分号不许把语句切断。schema.sql 的注释里就有分号。"""
    script = """
    -- 一句注释，里面有个分号；还有个 'quote
    CREATE TABLE a (x TEXT);  -- 尾注释
    /* 块注释；也有分号 */
    CREATE TABLE b (y TEXT);
    """
    assert _created_tables(to_pg_ddl(script)) == {"a", "b"}
    assert len(split_statements(script)) == 2


# -------------------------------------------- 3. 不认识就抛（不猜、不跳过）
@pytest.mark.parametrize("bad", [
    "CREATE TABLE t (a BLOB)",                       # 没在 _TYPE_MAP 里的类型
    "CREATE TABLE t (a NUMERIC(10,2))",
    "CREATE TABLE t (a TEXT DEFAULT (datetime('now')))",   # 函数默认值
    "CREATE TABLE t (a TEXT DEFAULT CURRENT_TIMESTAMP)",
    "CREATE TABLE t (a TEXT COLLATE NOCASE)",        # SQLite 专属排序规则
    "CREATE TABLE t (a INTEGER AUTOINCREMENT)",      # 不带 PRIMARY KEY 的自增
    "CREATE TABLE t (a TEXT) WITHOUT ROWID",
    "REINDEX t",                                     # 压根不是建表 DDL
    "PRAGMA table_info(t)",
])
def test_unknown_constructs_raise_with_the_original_line(bad: str) -> None:
    """认不出来就抛，并且**报错里带原文** —— 那是唯一能指出该往哪儿加的信息。

    静默跳过是这里最不能接受的处置：它让 PG 上少一张表 / 少一个约束，
    而少了只在跑到那一步时才炸，离原因十万八千里。
    """
    with pytest.raises(UnsupportedDdlError) as err:
        to_pg_ddl(bad)

    fingerprint = bad.split("(")[0].split()[-1]
    assert fingerprint in str(err.value), "报错里要认得出是哪一句翻不动"


def test_fts5_virtual_table_is_the_only_allowed_skip() -> None:
    """FTS5 整段跳过（PG 上本来就不该有它）；别的虚表照样抛。"""
    fts = ("CREATE VIRTUAL TABLE IF NOT EXISTS kb_doc_fts USING fts5("
           "id UNINDEXED, doc_id UNINDEXED, tenant_id UNINDEXED, title, body)")
    assert to_pg_ddl(fts).strip() == ""

    both = f"{fts};\nCREATE TABLE keep (a TEXT);"
    assert _created_tables(to_pg_ddl(both)) == {"keep"}

    with pytest.raises(UnsupportedDdlError):
        to_pg_ddl("CREATE VIRTUAL TABLE t USING rtree(id, minX, maxX)")


# ------------------------------------------------------ 4. 占位符与 upsert
@pytest.mark.parametrize(("sql", "expect"), [
    ("SELECT * FROM t WHERE a=?", "SELECT * FROM t WHERE a=%s"),
    ("INSERT INTO t (a,b) VALUES (?,?)", "INSERT INTO t (a,b) VALUES (%s,%s)"),
    # 字符串字面量里的 `?` 一个字都不许动 —— 这正是「不自动翻译」那条决策的顾虑。
    ("SELECT * FROM t WHERE n LIKE '%?%' AND a=?",
     "SELECT * FROM t WHERE n LIKE '%%?%%' AND a=%s"),
    ("SELECT '?' AS q WHERE a=?", "SELECT '?' AS q WHERE a=%s"),
    # 裸 `%` 要转义，否则 psycopg 把它当格式符。
    ("SELECT '100%' WHERE a=?", "SELECT '100%%' WHERE a=%s"),
])
def test_placeholders_respect_string_literals(sql: str, expect: str) -> None:
    assert translate_placeholders(sql) == expect


def test_insert_or_replace_becomes_on_conflict() -> None:
    """PG 没有 `INSERT OR REPLACE`。冲突目标取那张表的主键，从系统目录现查。"""
    out = rewrite_upsert(
        "INSERT OR REPLACE INTO kb_doc (tenant_id, doc_id, body) VALUES (?,?,?)",
        lambda _t: ("tenant_id", "doc_id"))

    assert out == ("INSERT INTO kb_doc (tenant_id, doc_id, body) VALUES (?,?,?)"
                   " ON CONFLICT (tenant_id, doc_id) DO UPDATE SET body=EXCLUDED.body")


def test_insert_or_replace_with_only_key_columns_does_nothing() -> None:
    """插入列全是主键列时没有非主键列可更新，`DO UPDATE SET` 会是语法错。"""
    out = rewrite_upsert("INSERT OR REPLACE INTO t (a, b) VALUES (?,?)",
                         lambda _t: ("a", "b"))

    assert out.endswith("ON CONFLICT (a, b) DO NOTHING")


def test_insert_or_ignore_becomes_do_nothing() -> None:
    out = rewrite_upsert("INSERT OR IGNORE INTO t (a, b) VALUES (?,?)",
                         lambda _t: ("a",))

    assert out.endswith("ON CONFLICT (a) DO NOTHING")


def test_upsert_without_primary_key_raises() -> None:
    """没有主键就推不出冲突目标。这里抛而不是默默退成裸 INSERT —— 后者会攒重复行。"""
    with pytest.raises(ValueError, match="没有主键"):
        rewrite_upsert("INSERT OR REPLACE INTO t (a) VALUES (?)", lambda _t: ())


def test_to_pg_sql_routes_ddl_and_dml_apart() -> None:
    """一个入口两条路：DDL 走翻译器，DML 走占位符 + upsert。"""
    assert to_pg_sql("CREATE TABLE t (a REAL)", lambda _t: (),
                     with_params=False).startswith("CREATE TABLE t")
    assert to_pg_sql("SELECT * FROM t WHERE a=?", lambda _t: (),
                     with_params=True) == "SELECT * FROM t WHERE a=%s"
    # 不传参数就不碰 `%`：psycopg 那次调用根本不解析它，转义反而会写进库里。
    assert to_pg_sql("SELECT '100%'", lambda _t: (), with_params=False) == "SELECT '100%'"
