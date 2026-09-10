"""T115 的机器验收（二）—— **退款域真的跑在 PostgreSQL 上**。没库就 skip，绝不红。

起库（口径同 `test_pg_store_live.py`，本机 pgvector 不是 PolarDB，别混着说）：

    docker compose -f deploy/docker-compose.yml --profile pg up -d pgvector
    export MAOS_PG_DSN=postgresql://<user>:<pass>@<host>:<port>/<db>

## 这一束在守什么

评委那条建议要的是「业务纵切**跑在** PolarDB 上」，不是「PolarDB 连通过」。
这两者之间隔着的正是本文件：换了后端之后，**那四道权威闸还在不在**。

铁律 8 的全部内容是「MAOS 不持有权威事实」。它在 SQLite 上由
`maos/domain/refund/guard.py` 的四道闸落地，而那四道闸里有两道靠的是**数据库的
事务语义**（回执与状态同事务）与**主键语义**（受理幂等）—— 这两样恰恰是最容易在
换后端时悄悄变掉的东西，而且变了不报错：

* PG 里一条语句失败会把整个事务打进 aborted 态（SQLite 不会）。补偿写错的话，
  症状是一条风马牛不相及的报错，或者更糟 —— 回执落了、状态没改，**而且没人知道**。
* `INSERT OR REPLACE` 是 SQLite 专属。翻成 `ON CONFLICT DO UPDATE` 时若把
  `refund_case` 也一并翻了，一次受理重跑就会把已经推进到 processing 的案子
  静悄悄倒回 submitted（`guard.create_case` 的 docstring 点名不许）。

所以这里不测「PG 能不能存数据」（那是 `test_pg_store_live.py` 的事），
只测**换后端之后语义一个字都没松**。
"""

from __future__ import annotations

import functools
import os

import pytest

from maos.core.store import SqliteStore
from maos.domain import _dbport
from maos.domain.refund import fixtures, guard, objects
from maos.store.pg_store import DSN_ENV, PgStorePort

#: 退款域那 16 张表。建不齐就是翻译器漏了一张。
TABLES = (
    "refund_schema_version", "tenant", "channel", "order_snapshot",
    "product_snapshot", "policy_rule", "refund_case", "customer_evidence",
    "approval_record", "finance_entry", "refund_request", "payment_observation",
    "notification", "compensation_record", "business_ref", "intake_annotation",
)

TENANT = "tnt-t115"
CASE = "case-t115-0001"
ORDER = "ord-t115-1"


@functools.lru_cache(maxsize=1)
def _live_dsn() -> str | None:
    """探一次：DSN 配了吗、连得上吗。连不上就是没库，不是失败。"""
    dsn = os.environ.get(DSN_ENV, "")
    if not dsn:
        return None
    port = PgStorePort(dsn)
    try:
        port.connect()
    except Exception:                                  # noqa: BLE001 —— 探测不该炸收集
        return None
    finally:
        port.close()
    return dsn


pytestmark = pytest.mark.skipif(
    _live_dsn() is None,
    reason=f"没有可连的 PG：{DSN_ENV} 未设或连不上。起库见本模块 docstring。",
)


@pytest.fixture()
def pg_store(monkeypatch: pytest.MonkeyPatch) -> SqliteStore:
    """一个把**业务表落在 PG**、内核四张表仍在 SQLite 的 store。

    内核表本期不上 PG（派单 §4），所以 `store` 仍是核心 `SqliteStore` ——
    `guard` 往 `event_log` 落的违规事件走它。业务表由 `MAOS_DOMAIN_BACKEND`
    切到 PG，这正是本轨新增的那条分岔。

    **DSN 要自己 setenv 回来**：`conftest._no_ambient_store_env` 每条用例开跑前
    会删掉 `MAOS_PG_DSN`（不删的话一次裸 `pytest` 就往真库写表）。既有的 live
    测试靠「collection 期就把 DSN 缓存住」躲过它，而本模块要在**用例执行期**连库，
    只能按那条 fixture docstring 指的路子自己盖回去 —— autouse fixture 先于同
    scope 的普通 fixture 实例化，这里的 setenv 一定盖得住它的 delenv。
    """
    monkeypatch.setenv(DSN_ENV, _live_dsn() or "")
    monkeypatch.setenv(_dbport.BACKEND_ENV, _dbport.POSTGRES)
    _dbport.close_pg()                                  # 别沿用别的用例留下的连接

    store = SqliteStore(":memory:")
    store.init_schema()
    objects.ensure_schema(store)
    conn = _dbport.DomainConn.open(store)
    for table in TABLES:
        if table != "refund_schema_version":            # 记账表留着，迁移靠它
            conn.execute(f"DELETE FROM {table}")
    yield store
    _dbport.close_pg()


def _seed_order(store: SqliteStore, *, policy_version: int = 1) -> None:
    objects.execute(
        store,
        "INSERT OR REPLACE INTO order_snapshot (tenant_id, order_id, version, sku,"
        " amount_paid, paid_at, channel_id, policy_version_at_order, payload_json, read_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (TENANT, ORDER, 1, "sku-t115", 3200.0, "2026-01-01T00:00:00+00:00",
         "ch-tmall", policy_version, "{}", "2026-01-02T00:00:00+00:00"))


def _new_case(store: SqliteStore) -> dict:
    return guard.create_case(
        store, tenant_id=TENANT, case_id=CASE, channel_id="ch-tmall",
        order_id=ORDER, order_version=1, sku="sku-t115", reason_code="damaged",
        amount_claimed=3200.0, plan_id="plan-t115", actor_skill="refund.intake",
        invocation_id="inv-1")


def _advance_to_processing(store: SqliteStore) -> None:
    guard.update_biz_status(store, TENANT, CASE, "approved", "refund.risk_screen", "inv-a")
    guard.update_biz_status(store, TENANT, CASE, "gateway_accepted", "payment.execute", "inv-b")
    guard.update_biz_status(store, TENANT, CASE, "processing", "payment.execute", "inv-c")


RECEIPT = {"request_id": "req-t115", "gateway_code": "0000",
           "observed_state": "settled", "raw_receipt_json": "{}"}


# ------------------------------------------------------------------ 1. 建表
def test_sixteen_tables_exist_on_postgres(pg_store: SqliteStore) -> None:
    """16 张表一张不少。少一张只会在跑到它时报 relation does not exist。"""
    rows = objects.query(
        pg_store,
        "SELECT table_name AS name FROM information_schema.tables"
        " WHERE table_schema = current_schema()")
    have = {r["name"] for r in rows}

    assert set(TABLES) <= have, f"PG 上少了这几张表：{sorted(set(TABLES) - have)}"


def test_ensure_schema_is_idempotent_on_postgres(pg_store: SqliteStore) -> None:
    """连跑不炸。`ensure_schema` 挂在写入口上，调用频次很高。"""
    objects.ensure_schema(pg_store)
    objects.ensure_schema(pg_store)

    assert objects.applied_schema_version(pg_store) == objects.REFUND_SCHEMA_VERSION


# --------------------------------------------------------- 2. 四道权威闸
def test_gate_one_non_authoritative_writer_cannot_write_settled(
        pg_store: SqliteStore) -> None:
    """① 非 `payment.observe` 写 settled 一律拒，并**落一条事件**。

    落事件这一半同样要守：「系统拒绝了一次越权写入」本身就是要拿给评委看的证据，
    吞掉就没了（`guard.py` 模块 docstring 的原话）。
    """
    _seed_order(pg_store)
    _new_case(pg_store)

    with pytest.raises(guard.AuthoritativeFactViolation):
        guard.update_biz_status(pg_store, TENANT, CASE, "settled",
                                "finance.settle", "inv-x", observation=RECEIPT)

    events = [e for e in pg_store.list_event_log("plan-t115")
              if e["event_type"] == guard.VIOLATION_EVENT]
    assert events, "越权被拒了却没留证据"


def test_gate_two_receipt_only_from_the_authoritative_writer(
        pg_store: SqliteStore) -> None:
    """② 回执只有权威写入方递得进来，否则等于开了个伪造回执的口子。"""
    _seed_order(pg_store)
    _new_case(pg_store)

    with pytest.raises(guard.AuthoritativeFactViolation, match="回执"):
        guard.update_biz_status(pg_store, TENANT, CASE, "approved",
                                "refund.risk_screen", "inv-y", observation=RECEIPT)


def test_gate_three_settled_needs_a_receipt_in_the_same_transaction(
        pg_store: SqliteStore) -> None:
    """③ 没有回执的 settled 就是把外部状态写死为终态。

    被拒之后**状态必须还停在 processing** —— 这一条在 PG 上尤其要看：
    PG 的 aborted 事务处理错了的话，可能出现「回执落了、状态没改」或者反过来。
    """
    _seed_order(pg_store)
    _new_case(pg_store)
    _advance_to_processing(pg_store)

    with pytest.raises(guard.AuthoritativeFactViolation, match="缺字段"):
        guard.update_biz_status(pg_store, TENANT, CASE, "settled",
                                "payment.observe", "inv-z")

    assert guard.get_case(pg_store, TENANT, CASE)["biz_status"] == "processing"
    assert objects.query(pg_store, "SELECT * FROM payment_observation") == []


def test_gate_four_a_receipt_that_says_failed_does_not_settle(
        pg_store: SqliteStore) -> None:
    """④ 「有一张回执」不等于「网关说到账了」。"""
    _seed_order(pg_store)
    _new_case(pg_store)
    _advance_to_processing(pg_store)

    with pytest.raises(guard.AuthoritativeFactViolation, match="不是"):
        guard.update_biz_status(
            pg_store, TENANT, CASE, "settled", "payment.observe", "inv-w",
            observation={**RECEIPT, "observed_state": "failed", "gateway_code": "40005"})

    assert guard.get_case(pg_store, TENANT, CASE)["biz_status"] == "processing"


def test_settled_and_its_receipt_land_together(pg_store: SqliteStore) -> None:
    """正路：`payment.observe` 带着一张说到账了的回执，状态与回执同事务落库。"""
    _seed_order(pg_store)
    _new_case(pg_store)
    _advance_to_processing(pg_store)

    case = guard.update_biz_status(pg_store, TENANT, CASE, "settled",
                                   "payment.observe", "inv-ok", observation=RECEIPT)

    assert case["biz_status"] == "settled"
    rows = objects.query(pg_store, "SELECT * FROM payment_observation")
    assert len(rows) == 1 and rows[0]["observed_state"] == "settled"
    assert rows[0]["actor_invocation_id"] == "inv-ok"


def test_bad_transition_is_refused_on_postgres(pg_store: SqliteStore) -> None:
    """迁移不在 `BIZ_STATUS_FLOW` 里就拒。业务状态机不因换后端而松。"""
    _seed_order(pg_store)
    _new_case(pg_store)

    with pytest.raises(guard.BizStatusTransitionError):
        guard.update_biz_status(pg_store, TENANT, CASE, "processing",
                                "payment.execute", "inv-bad")


def test_objects_execute_still_refuses_refund_case_writes(pg_store: SqliteStore) -> None:
    """运行时旁路拦截在 PG 上照样在。它挡的是**运行时**的旁路，不是提交前的 grep。"""
    _seed_order(pg_store)
    _new_case(pg_store)

    with pytest.raises(objects.BypassedGuardError):
        objects.execute(pg_store, "UPDATE refund_case SET biz_status=?"
                                  " WHERE tenant_id=? AND case_id=?",
                        ("settled", TENANT, CASE))


# ------------------------------------------------- 3. 受理幂等（主键语义）
def test_create_case_replay_is_idempotent_on_postgres(pg_store: SqliteStore) -> None:
    """受理重跑：业务字段逐字段相同 → 一个字节都不写，返回**既有那一行**。

    这一条守的是 `INSERT OR REPLACE` **没有**被翻到 `refund_case` 头上：
    翻了的话一次重跑会把已经推进的案子静悄悄倒回 submitted。
    """
    _seed_order(pg_store)
    first = _new_case(pg_store)
    _advance_to_processing(pg_store)

    again = _new_case(pg_store)

    assert again["created_at"] == first["created_at"]
    assert again["biz_status"] == "processing", "重跑把状态倒回去了 —— 幂等写成了覆盖"


def test_case_identity_conflict_is_loud_on_postgres(pg_store: SqliteStore) -> None:
    """同一个案号来了一份业务字段不一样的受理 → 抛，并落一条事件。"""
    _seed_order(pg_store)
    _new_case(pg_store)

    with pytest.raises(guard.CaseIdentityConflict):
        guard.create_case(
            pg_store, tenant_id=TENANT, case_id=CASE, channel_id="ch-tmall",
            order_id=ORDER, order_version=1, sku="sku-t115", reason_code="damaged",
            amount_claimed=9999.0, plan_id="plan-t115",
            actor_skill="refund.intake", invocation_id="inv-2")

    events = [e for e in pg_store.list_event_log("plan-t115")
              if e["event_type"] == guard.CASE_CONFLICT_EVENT]
    assert events, "案号复用被拒了却没留证据"


# ------------------------------------------------------ 4. 业务引用与政策版本
def test_business_ref_resolves_on_postgres(pg_store: SqliteStore) -> None:
    """`business_ref` 只存引用，读的时候必须指得到那一行。"""
    _seed_order(pg_store)
    objects.attach_business_ref(
        pg_store, plan_id="plan-t115", task_id="t-1", tenant_id=TENANT,
        object_type="order_snapshot", object_id=ORDER, object_version=1,
        purpose="判政策")

    refs = objects.list_business_refs(pg_store, plan_id="plan-t115")
    assert len(refs) == 1
    resolved = objects.resolve_business_ref(pg_store, refs[0])
    assert resolved is not None and resolved["order_id"] == ORDER


def test_attach_business_ref_replay_does_not_duplicate(pg_store: SqliteStore) -> None:
    """同一条引用挂两次仍是一行 —— `INSERT OR REPLACE` 翻成 ON CONFLICT 之后
    还得是**去重**的，退成裸 INSERT 会攒出重复行（主键会响，但那是另一种失败）。"""
    _seed_order(pg_store)
    for _ in range(2):
        objects.attach_business_ref(
            pg_store, plan_id="plan-t115", task_id="t-1", tenant_id=TENANT,
            object_type="order_snapshot", object_id=ORDER, object_version=1,
            purpose="判政策")

    assert len(objects.list_business_refs(pg_store, plan_id="plan-t115")) == 1


def test_pinned_policy_version_is_the_order_snapshot_not_max(
        pg_store: SqliteStore) -> None:
    """政策版本锁定在**下单当时**那一版，不是 policy_rule 的 max(version)。

    用当前最新政策去判一笔历史订单，等于拿今天的规则追溯昨天的交易。
    """
    _seed_order(pg_store, policy_version=2)
    for version in (1, 2, 3):
        objects.execute(
            pg_store,
            "INSERT OR REPLACE INTO policy_rule (tenant_id, rule_no, version, title,"
            " body, effective_from, effective_to, channel_scope, sku_scope)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (TENANT, "AS-101", version, f"v{version}", "", "2025-01-01T00:00:00+00:00",
             None, "*", "*"))

    assert objects.pinned_policy_version(
        pg_store, tenant_id=TENANT, order_id=ORDER, order_version=1) == 2

    rules = objects.policy_rules_at_order(
        pg_store, tenant_id=TENANT, order_id=ORDER, order_version=1)
    assert [r["version"] for r in rules] == [2], "取到了锁定版本之后的政策"


def test_missing_order_snapshot_raises_lookup_error(pg_store: SqliteStore) -> None:
    """没有快照就没有锁定版本。回落到「用最新的」是最危险的默认值。"""
    with pytest.raises(LookupError):
        objects.pinned_policy_version(
            pg_store, tenant_id=TENANT, order_id="nope", order_version=1)


# ------------------------------------------------------------ 5. 探针与事务
def test_a_failed_probe_does_not_poison_the_transaction(pg_store: SqliteStore) -> None:
    """`_has_column()` 靠一条会失败的 SELECT 判列在不在，还把异常吞掉继续跑。

    PG 里一条语句失败会把整个事务打进 aborted 态 —— 不补这一条，探针会连坐掉
    它后面所有语句，而报出来的是一句风马牛不相及的
    `current transaction is aborted`。
    """
    _seed_order(pg_store)

    assert objects._has_column(pg_store, "refund_case", "biz_status") is True
    assert objects._has_column(pg_store, "refund_case", "no_such_column") is False
    assert len(objects.query(pg_store, "SELECT * FROM order_snapshot")) == 1


def test_atomic_rolls_back_ddl_and_dml_together(pg_store: SqliteStore) -> None:
    """`_atomic` 一组语句同生共死，**DDL 也算在内**。断在中间不许留下半成品。"""
    with pytest.raises(Exception):                     # noqa: B017 —— 什么异常不重要
        objects._atomic(pg_store, [
            ("CREATE TABLE t115_scratch (a TEXT)", ()),
            ("INSERT INTO t115_scratch (a) VALUES (?)", ("x",)),
            ("INSERT INTO no_such_table (a) VALUES (?)", ("boom",)),
        ])

    rows = objects.query(
        pg_store,
        "SELECT table_name AS name FROM information_schema.tables"
        " WHERE table_schema = current_schema() AND table_name = 't115_scratch'")
    assert rows == [], "回滚没把建表一起撤掉 —— 库停在半成品上了"


# ------------------------------------------------- 6. 加列助手（给同波次三轨用）
def test_add_column_if_missing_on_postgres(pg_store: SqliteStore) -> None:
    """`add_column_if_missing` 在 PG 上探得对、加得上、连跑是 no-op。

    这个函数**当前没有调用方** —— 同波次的 T116/T117/T120 各自复制了一份私有版本，
    整合期由主会话改成 import 这一个。没有调用方就意味着没有别的测试会踩到它，
    所以它自己必须带一条：整合期发现它是坏的，比现在发现贵得多。

    列声明只写一份 SQLite 方言的（`REAL`），PG 侧由同一个翻译器现翻成
    `double precision` —— 与建表共用一份，两处不会漂。
    """
    conn = _dbport.DomainConn.open(pg_store)
    conn.execute("DROP TABLE IF EXISTS t115_addcol")
    conn.execute("CREATE TABLE t115_addcol (a TEXT)")

    _dbport.add_column_if_missing(conn, "t115_addcol", "b", "REAL NOT NULL DEFAULT 0")
    _dbport.add_column_if_missing(conn, "t115_addcol", "b", "REAL NOT NULL DEFAULT 0")

    rows = conn.query(
        "SELECT column_name AS name, data_type AS kind"
        " FROM information_schema.columns"
        " WHERE table_schema = current_schema() AND table_name = ?"
        " ORDER BY ordinal_position", ("t115_addcol",))
    conn.execute("DROP TABLE t115_addcol")

    assert [r["name"] for r in rows] == ["a", "b"], "连跑两次加出了两列，或一列都没加"
    assert rows[1]["kind"] == "double precision", "REAL 没翻成 PG 类型"


# ------------------------------------------------------------------ 7. 靶场
def test_fixtures_seed_case_lands_on_postgres(pg_store: SqliteStore) -> None:
    """`fixtures.seed_case()` 在 PG 后端上照样灌得进去（派单 §3.5）。"""
    payload = fixtures.load_case("case_r3a.json")

    counted = fixtures.seed_case(pg_store, payload)

    assert counted, "一张表都没灌 —— 语料对不上了"
    for table, rows in counted.items():
        got = objects.query(pg_store, f"SELECT count(*) AS n FROM {table}")
        assert got[0]["n"] >= rows, f"{table} 在 PG 上没落全"
