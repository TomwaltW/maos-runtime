"""T116：十类业务对象 + 对象版本 + 执行前读外部 + 三态投影。

四件事各自的题眼，按本文件的分节顺序：

1. **加列走迁移，不走 `schema.sql`**。那份文件整份都是 `IF NOT EXISTS`，表已存在就
   整段跳过 —— 改列**静默无效**，跑起来一切正常，直到某条 INSERT 报 no such column。
   §1 钉住「片段是目标形状的唯一来源」「迁移幂等」「记账落到位」。
2. **引用覆盖十类，一类都不能少**。少一条的症状不是报错，是 `resolve_business_ref`
   静默返回 None —— 「这个 Task 引用了哪个业务对象」在那一类上永远答不出来，
   而 T120 的 `evidence_complete`（跨轨契约 §E）会永远算不出 complete。
3. **执行前真的读了一次**。`refund.snapshot_check` 的价值全在「读不到 / 对不上时
   不放行」这一侧：§4 每一条负例都在钉「不许兜底成一致」。
4. **对外只说得出那五句**。`public_status` 的字面值锁死在跨轨契约 §D；
   §5 尤其钉「没有观察行就不许说已到账」（铁律 8）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from maos.agents.base import AgentIdentity
from maos.core.store import SqliteStore
from maos.domain.refund import case_pack, guard, objects, projection
from maos.flows import scenario_6 as s6
from maos.model.client import Tier
from maos.skills.builtin.refund import REFUND_SKILLS, snapshot_check
from maos.skills.builtin.refund import _common as C
from maos.skills.invoker import SkillInvoker
from maos.tools import order as order_tools
from maos.tools.port import invoke_tool

REPO = Path(__file__).resolve().parents[2]

PLAN_ID = "plan-t116"
TASK_ID = "task-t116"
TENANT = "tnt-t116"
CASE = "case-t116"
ORDER = "ord-t116"
SYSTEM = "t116-orders"

IDENTITY = AgentIdentity(
    agent_id="test-t116", role="test_refund_t116",
    duty="测试夹具：授权退款域全部 skill",
    allowed_skills=frozenset(set(REFUND_SKILLS) | {"issue.aggregate"}),
    allowed_tools=frozenset({"order.query"}),
    model_tier=Tier.LIGHT,
)


@pytest.fixture
def store():
    st = SqliteStore()
    st.init_schema()
    objects.ensure_schema(st)
    return st


@pytest.fixture
def seeded(store):
    """一个建好案的库：租户 / 渠道 / 订单快照 / 案子都在，订单系统也登记了。"""
    objects.execute(store, "INSERT OR REPLACE INTO order_snapshot (tenant_id, order_id,"
                           " version, sku, amount_paid, paid_at, channel_id,"
                           " policy_version_at_order, payload_json, read_at)"
                           " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (TENANT, ORDER, 1, "SKU-T116", 1280.0,
                     "2026-08-20T02:00:00+00:00", "ch-dealer", 1, "{}",
                     "2026-09-04T00:00:00+00:00"))
    guard.create_case(store, tenant_id=TENANT, case_id=CASE, channel_id="ch-dealer",
                      order_id=ORDER, order_version=1, sku="SKU-T116",
                      reason_code="quality_defect", amount_claimed=1280.0,
                      plan_id=PLAN_ID, actor_skill="test", invocation_id="iv-t116")
    order_tools.reset_order_systems()
    system = order_tools.MockOrderSystem()
    system.ext_order(order_id=ORDER, version=1, status=order_tools.ORDER_PAID,
                     amount="1280.00", updated_at="2026-08-20T02:00:00+00:00")
    order_tools.register_order_system(SYSTEM, system)
    return store, system


def _check(store, **payload):
    invoker = SkillInvoker(IDENTITY, store)
    body = {"tenant_id": TENANT, "case_id": CASE, "order_system": SYSTEM}
    body.update(payload)
    return invoker.invoke("refund.snapshot_check", body,
                          extras={"plan_id": PLAN_ID, "task_id": TASK_ID,
                                  "trace_id": "trace-t116"})


def _drift_events(store):
    return [e for e in store.list_event_log(PLAN_ID)
            if e.get("event_type") == snapshot_check.EVENT_SNAPSHOT_DRIFT]


# ======================================================================
# 1. schema 片段与迁移
# ======================================================================
def test_fragment_declares_exactly_the_six_columns():
    """片段声明的六列，逐条对上派单那张表。

    判据读的是 `.sql` 文件本身而不是代码里的常量：目标形状只该有一份，
    抄第二份的后果是哪天有人改了 .sql、代码没跟着改，而两边都不报错。
    """
    got = {(t, c) for t, c, _d in case_pack.fragment_columns()}
    assert got == {
        ("customer_evidence", "version"),
        ("approval_record", "revision"),
        ("finance_entry", "revision"),
        ("refund_request", "version"),
        ("notification", "revision"),
        ("compensation_record", "revision"),
    }


def test_fragment_columns_all_declare_not_null_default_one():
    """六列一律 `INTEGER NOT NULL DEFAULT 1`。

    缺省 0 会让「没升级过的库」与「升级了但还没改过的对象」在数据上分不开，
    而带版本引用的判据正是 `object_version == 对象当前版本` —— 差一就悬空。
    """
    for table, col, decl in case_pack.fragment_columns():
        assert decl == "INTEGER NOT NULL DEFAULT 1", f"{table}.{col} 的声明漂了：{decl}"


def test_fragment_rejects_any_statement_that_is_not_add_column():
    """片段里混进别的语句 -> 当场抛，**不静默跳过**。

    静默跳过的症状是「那一列永远没加上」，而且不报错 —— 正是本轨在买掉的那类失效。
    """
    with pytest.raises(case_pack.CasePackError, match="ALTER TABLE ADD COLUMN"):
        case_pack.fragment_columns("CREATE TABLE t116_bogus (a TEXT);")


def test_ensure_schema_adds_all_six_columns_on_a_fresh_db(store):
    """新库跑完 `objects.ensure_schema()` 六列就位 —— 不必另外调本模块。

    新库同样走迁移这条路：`refund_schema_version` 在新库上也是空的，
    每一步自带探针、在已是目标形状的库上是 no-op。
    """
    for table, col, _decl in case_pack.fragment_columns():
        assert objects._has_column(store, table, col), f"{table}.{col} 没加上"


def test_ensure_t116_schema_is_idempotent(store):
    """第二次调什么都不加 —— 幂等，可连跑。"""
    assert case_pack.ensure_t116_schema(store) == ()
    assert case_pack.ensure_t116_schema(store) == ()


def test_migration_is_registered_and_recorded(store):
    """迁移表里有 T116 那一步，且版本记账落到了 `REFUND_SCHEMA_VERSION`。

    记账对不上的症状是「迁移悄悄不跑了」，没有任何报错 —— 所以这条要钉死。
    """
    labels = [label for _v, label, _s in objects._MIGRATIONS]
    assert any("t116" in label for label in labels), f"迁移表里没有 T116 那一步：{labels}"
    assert objects.REFUND_SCHEMA_VERSION >= 1
    assert objects.applied_schema_version(store) == objects.REFUND_SCHEMA_VERSION
    rows = objects.query(store, "SELECT version FROM refund_schema_version ORDER BY version")
    assert [r["version"] for r in rows] == [v for v, _l, _s in objects._MIGRATIONS]


def test_schema_sql_main_file_was_not_touched():
    """主文件 `schema.sql` **一列都没加** —— 跨轨契约 §B.1。

    往那份文件里加列是静默无效的（整份 `IF NOT EXISTS`），而看起来像加了 ——
    这条钉住「没人图省事把它写进去」。
    """
    text = (REPO / "maos" / "domain" / "refund" / "schema.sql").read_text(encoding="utf-8")
    body = "\n".join(line for line in text.splitlines()
                     if not line.strip().startswith("--"))
    assert "revision" not in body, "schema.sql 主文件里出现了 revision —— 改列在那儿是静默无效的"
    assert not re.search(r"\bALTER\s+TABLE\b", body, re.IGNORECASE)


# ======================================================================
# 2. business_ref 十类全覆盖
# ======================================================================
def test_ten_objects_cover_eleven_object_types():
    """评委原话的十项 = 库里十一张表（「订单与商品快照」是一项两张表）。"""
    assert len(case_pack.TEN_OBJECTS) == 10
    assert len(case_pack.ALL_OBJECT_TYPES) == 11
    assert len(set(case_pack.ALL_OBJECT_TYPES)) == 11, "object_type 有重复"


def test_every_one_of_the_ten_object_types_is_resolvable():
    """十一个 object_type 逐个在 `_REF_TARGETS` 里，且指向真实存在的表与列。

    少一条不会报错，只会让 `resolve_business_ref` 静默返回 None。
    """
    missing = [t for t in case_pack.ALL_OBJECT_TYPES if t not in objects._REF_TARGETS]
    assert missing == [], f"这几类还没进 _REF_TARGETS：{missing}"


def test_ref_target_tables_and_keys_exist_in_the_schema(store):
    """每个 `(表, 主键列)` 都查得动 —— 写错表名/列名时这条会红，而不是运行时才炸。"""
    for object_type, (table, key) in objects._REF_TARGETS.items():
        objects.query(store, f"SELECT {key} FROM {table} LIMIT 1"), object_type


def test_versioned_ref_tables_all_have_a_column_literally_named_version(store):
    """进 `_VERSIONED_REF_TABLES` 的表，版本列必须**就叫** `version`。

    `resolve_business_ref` 拼的是写死的 `AND version=?`。把 `revision` 那几张塞进来
    会撞 no such column，症状是那几类引用**全部** resolve 失败。
    """
    for table in objects._VERSIONED_REF_TABLES:
        assert objects._has_column(store, table, "version"), \
            f"{table} 进了 _VERSIONED_REF_TABLES 却没有 version 列"


def test_revision_tables_are_kept_out_of_versioned_ref_tables():
    """`revision` 那四张**不在**版本收窄集合里 —— 见上一条的理由。"""
    revision_tables = {t for t, c, _d in case_pack.fragment_columns() if c == "revision"}
    assert revision_tables & objects._VERSIONED_REF_TABLES == set()


def test_applicant_ref_no_longer_dangles(store):
    """`applicant_ref` 能 resolve 了 —— 它指向 `business_ref` 自己。

    MAOS 不持有那份外部审批单（铁律 8），它持有的就是这条引用本身。
    T116 之前它不在 `_REF_TARGETS` 里，resolve 恒 None，而那个 None 与
    「对象丢了」长得一模一样 —— 两件事必须分得开。
    """
    ref = objects.attach_business_ref(
        store, plan_id=PLAN_ID, task_id=TASK_ID, tenant_id=TENANT,
        object_type="applicant_ref", object_id="SCR-2026-0912",
        purpose="供应链退款审批单引用")
    got = objects.resolve_business_ref(store, ref)
    assert got is not None, "applicant_ref 又悬空了"
    assert got["object_id"] == "SCR-2026-0912"


def test_unknown_object_type_still_resolves_to_none(store):
    """没登记过的 object_type 仍然返回 None、不抛 —— 既有行为不许被本轮改掉。"""
    assert objects.resolve_business_ref(
        store, {"object_type": "t116-unknown", "tenant_id": TENANT,
                "object_id": "x", "object_version": 0}) is None


# ======================================================================
# 3. next_version：修订号怎么算
# ======================================================================
def test_next_version_starts_at_one_on_an_empty_table(store):
    assert case_pack.next_version(store, "approval_record",
                                  tenant_id=TENANT, case_id=CASE) == 1


def test_next_version_takes_max_plus_one_not_row_count(store):
    """`finance_entry` 主键是 `(tenant, case)`，二次核算把前一版**挤掉**，行数恒为 1。

    按行数算，第三次核算会算出 2、与第二次撞号 —— 修订链就此断在那里且不报错。
    """
    for revision in (1, 2, 3):
        objects.execute(
            store,
            "INSERT OR REPLACE INTO finance_entry (tenant_id, case_id, amount_approved,"
            " breakdown_json, rule_refs, checked_by, checked_at, revision)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (TENANT, CASE, 1280.0, "{}", "[]", "t116", "now", revision))
        rows = objects.query(store, "SELECT COUNT(*) AS n FROM finance_entry"
                                    " WHERE tenant_id=? AND case_id=?", (TENANT, CASE))
        assert rows[0]["n"] == 1, "这张表本来就只留最新一行"
    assert case_pack.next_version(store, "finance_entry",
                                  tenant_id=TENANT, case_id=CASE) == 4


def test_next_version_narrows_by_extra_where(store):
    """`**where` 把修订链收窄到某一个对象（同案不同 kind 各数各的）。"""
    for kind, revision in (("manual_ticket", 1), ("manual_ticket", 2),
                           ("refund_request_revoked", 1)):
        objects.execute(
            store,
            "INSERT OR REPLACE INTO compensation_record (tenant_id, case_id, kind,"
            " detail_json, executed_at, operator, revision) VALUES (?,?,?,?,?,?,?)",
            (TENANT, CASE, kind, "{}", f"t{revision}", "op", revision))
    assert case_pack.next_version(store, "compensation_record", tenant_id=TENANT,
                                  case_id=CASE, kind="manual_ticket") == 3
    assert case_pack.next_version(store, "compensation_record", tenant_id=TENANT,
                                  case_id=CASE, kind="refund_request_revoked") == 2


def test_next_version_falls_back_to_one_when_the_table_cannot_be_read(store):
    """探针读不动就返回 1，不把整条链路炸掉 —— 真错了由紧随其后的 INSERT 报出来。"""
    assert case_pack.next_version(store, "t116_no_such_table",
                                  tenant_id=TENANT, case_id=CASE) == 1


# ======================================================================
# 4. 案例包：十块齐
# ======================================================================
def test_case_real_01_loads_with_all_ten_blocks():
    payload = case_pack.load_case_pack()
    assert case_pack.missing_blocks(payload) == []
    for block in case_pack.REQUIRED_BLOCKS:
        assert payload.get(block), f"{block} 是空的"


def test_missing_block_is_reported_by_name(tmp_path):
    """缺一块当场抛并说出缺哪块 —— 不做「缺了就补个空块」。

    补空块会让它照常跑绿，而跑绿的错结论比报错难查得多。
    """
    payload = json.loads(case_pack.CASE_REAL_01.read_text(encoding="utf-8"))
    payload.pop("compensation_record")
    bad = tmp_path / "broken.json"
    bad.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(case_pack.CasePackError, match="compensation_record"):
        case_pack.load_case_pack(bad)


def test_expected_blocks_are_never_seeded():
    """六块预期值一块都不许进 `fixtures` 的装载清单。

    进了的话，案例包里那份"预期审批"会在流程跑之前先落库，跑完再比对永远一致 ——
    一条不会失败的验收比没有验收更坏。
    """
    case_pack.assert_expected_blocks_are_not_seeded()


def test_expected_block_refuses_a_seeded_block():
    """取用口也把住：想灌库请走 `fixtures.seed_case()`，别从预期块拿。"""
    payload = case_pack.load_case_pack()
    with pytest.raises(case_pack.CasePackError, match="不是预期块"):
        case_pack.expected_block(payload, "order_snapshot")


def test_case_real_01_policy_rules_match_the_single_source_of_truth():
    """案例里的 `policy_rule` 与 `policy/policy_rules.json` **逐字段**相同。

    两处一旦分叉，这份案例的裁定前提就悄悄变了，且不会有任何报错
    （`scenarios/refund/README.md` 的自校验第 3 条守的是同一件事）。
    """
    corpus = json.loads((REPO / "scenarios" / "refund" / "policy"
                         / "policy_rules.json").read_text(encoding="utf-8"))
    key = lambda r: (r["tenant_id"], r["rule_no"], r["version"])   # noqa: E731
    index = {key(r): r for r in corpus["policy_rule"]}
    payload = case_pack.load_case_pack()
    drift = [key(r) for r in payload["policy_rule"] if index.get(key(r)) != r]
    assert drift == [], f"这几条与唯一事实源对不上：{drift}"


def test_case_real_01_declares_every_placeholder_field():
    """构造字段清单在文件里写着 —— 人类换真数据时照它逐条改，不用猜。"""
    payload = case_pack.load_case_pack()
    note = payload.get("_placeholder_fields") or ""
    for marker in ("customer_name", "phone", "address", "receipt_no", "digest"):
        assert marker in note, f"构造字段清单没提到 {marker}"


def test_case_real_01_customer_identity_is_masked():
    """客户身份四项已按脱敏规范打码 —— 语料里不许留可识别的真实信息。"""
    payload = case_pack.load_case_pack()
    body = json.loads(payload["order_snapshot"][0]["payload_json"])
    assert "*" in body["customer_name"]
    assert re.fullmatch(r"\d{3}\*{4}\d{4}", body["phone"]), body["phone"]
    assert body["address"].endswith("****")
    assert "****" in body["receipt_no"]


# ======================================================================
# 5. 三态投影（跨轨契约 §D）
# ======================================================================
@pytest.mark.parametrize("biz,has_request,observed,want", [
    ("approved", True, None, "已提出退款"),
    ("gateway_accepted", True, None, "已提出退款"),
    ("processing", True, "processing", "支付处理中"),
    ("settled", True, "settled", "退款已到账"),
    ("rejected", False, None, "已驳回"),
    ("compensated", True, "failed", "已补偿（未到账）"),
])
def test_public_status_literals(biz, has_request, observed, want):
    """五个字面值一个字都不许改（契约 §D）。"""
    assert projection.public_status(biz, has_request, observed) == want


def test_settled_without_an_observation_row_refuses_to_say_arrived():
    """`biz_status=settled` 而**没有观察行** -> 拒绝投影，返回空串。

    这是铁律 8 在投影层的落点：没人观察到就一个字都不许说「已到账」。
    库内真出现这种自相矛盾时（`guard.py` 本该拦住），宁可不说 ——
    说「支付处理中」同样是编一个状态。
    """
    assert projection.public_status("settled", True, None) == ""
    assert projection.public_status("settled", True, "processing") == ""


def test_approved_without_a_request_has_nothing_to_say():
    """审批通过但退款请求还没发出去 —— 对客户而言那不是「已提出退款」。"""
    assert projection.public_status("approved", False, None) == ""


def test_submitted_falls_back_to_no_public_status():
    """刚受理这一档契约里没有对应字面值 —— **不自造第六句**。"""
    assert projection.public_status("submitted", False, None) == ""


def test_an_observed_arrival_overrides_a_lagging_biz_status():
    """观察到到账、业务状态还没跟上（或已补偿收口）——以**观察**为准。

    权威在网关那边，不在本地状态机上。
    """
    assert projection.public_status("compensated", True, "settled") == "退款已到账"
    assert projection.public_status("processing", True, "settled") == "退款已到账"


def test_public_status_never_invents_a_sixth_wording():
    """穷举七态 × 有无请求 × 四种观察，产出必落在五句或空串里。"""
    states = ("submitted", "approved", "gateway_accepted", "processing",
              "settled", "rejected", "compensated")
    for biz in states:
        for has_request in (True, False):
            for observed in (None, "processing", "failed", "settled"):
                got = projection.public_status(biz, has_request, observed)
                assert got in projection.PUBLIC_STATUSES or got == "", (biz, got)


def test_observed_state_of_takes_the_last_observation():
    """当前下落永远是**最后**那次观察说的，不是「有没有任何一条是 settled」。"""
    rows = [{"observed_state": "processing"}, {"observed_state": "failed"}]
    assert projection.observed_state_of(rows) == "failed"
    assert projection.observed_state_of([]) is None


# ======================================================================
# 6. order ToolPort：执行前读外部
# ======================================================================
def test_mock_order_system_rejects_an_unknown_status():
    """未收录的状态当场抛 —— 不兜底成「大概是 paid」。"""
    system = order_tools.MockOrderSystem()
    with pytest.raises(ValueError, match="未收录的订单状态"):
        system.ext_order(order_id="o", version=1, status="whatever",
                         amount="1.00", updated_at="t")


def test_amend_pushes_the_version_up_by_exactly_one():
    """外部改一次单，版本真的推高一格 —— 恒 +1，不由调用方挑数字。"""
    system = order_tools.MockOrderSystem()
    system.ext_order(order_id="o", version=1, status=order_tools.ORDER_PAID,
                     amount="1.00", updated_at="t0")
    after = system.amend("o", status=order_tools.ORDER_AMENDED, updated_at="t1")
    assert after.version == 2
    assert system.query("o").version == 2
    assert system.query("o").amount == "1.00", "没改金额就不该动金额"


def test_query_of_a_missing_order_raises_instead_of_returning_an_empty_order():
    """查不到就抛。兜底成「一个 v1 的空订单」会把「订单不见了」伪装成「版本一致，放行」。"""
    with pytest.raises(KeyError):
        order_tools.MockOrderSystem().query("no-such")


def test_the_toolport_exposes_no_way_to_amend_an_order():
    """ToolPort 上**没有任何改单入口**（铁律 8）。

    `amend()` 模拟的是外部世界的动作，挂上 ToolPort 就等于给了 MAOS 一条
    改外部权威事实的路径。
    """
    ports = [v for k, v in vars(order_tools).items() if k.endswith("_PORT")]
    assert [p.name for p in ports] == ["order.query"]
    assert "amend" not in order_tools.ORDER_QUERY_PORT.entry.__name__


def test_get_order_system_does_not_fall_back_to_a_default():
    """取不到就抛，不悄悄用一个空账本。"""
    order_tools.reset_order_systems()
    with pytest.raises(LookupError, match="没有登记名为"):
        order_tools.get_order_system("t116-nope")


def test_invoke_tool_leaves_an_audit_row(seeded):
    """经 `invoke_tool` 读一次，留得下 ToolInvoked —— 「执行前真的读了一次」的证据。"""
    store, _system = seeded
    invoke_tool(order_tools.ORDER_QUERY_PORT,
                {"system_name": SYSTEM, "order_id": ORDER},
                store=store, extras={"plan_id": PLAN_ID, "task_id": TASK_ID})
    rows = [e for e in store.list_event_log(PLAN_ID)
            if e.get("event_type") == "ToolInvoked"]
    assert any(json.loads(e["detail"] if isinstance(e["detail"], str)
                          else json.dumps(e["detail"]))["tool"] == "order.query"
               for e in rows), "读订单没留下审计行"


# ======================================================================
# 7. refund.snapshot_check
# ======================================================================
def test_matching_versions_pass_without_an_event(seeded):
    store, _system = seeded
    res = _check(store)
    assert res.status == "ok", res.error
    assert res.output["drift"] is False
    assert res.output["reason"] == ""
    assert _drift_events(store) == [], "没漂移却落了 SnapshotDrift"


def test_a_drifted_version_is_reported_and_logged(seeded):
    """外部改过单 -> `drift=true` + 一条 `SnapshotDrift`，detail 带契约 §F 要的字段。"""
    store, system = seeded
    system.amend(ORDER, status=order_tools.ORDER_AMENDED, updated_at="t1")

    res = _check(store)
    assert res.status == "ok", res.error
    assert res.output["drift"] is True
    assert res.output["snapshot_version"] == 1
    assert res.output["current_version"] == 2

    events = _drift_events(store)
    assert len(events) == 1
    detail = events[0]["detail"]
    detail = json.loads(detail) if isinstance(detail, str) else detail
    assert detail["tenant_id"] == TENANT
    assert detail["case_id"] == CASE
    assert detail["snapshot_version"] == 1
    assert detail["current_version"] == 2


def test_drift_does_not_touch_the_business_status(seeded):
    """漂移检查**一个业务状态都不改**（铁律 8/9）——它只出参 + 落事件。"""
    store, system = seeded
    system.amend(ORDER, status=order_tools.ORDER_CANCELLED, updated_at="t1")
    before = guard.get_case(store, TENANT, CASE)["biz_status"]

    _check(store)

    assert guard.get_case(store, TENANT, CASE)["biz_status"] == before == "submitted"


def test_an_unreadable_order_is_drift_not_a_pass(seeded):
    """订单系统里没有这笔单 -> 报漂移。

    「订单不见了」比「订单改了」更严重，把它兜底成放行是这条链路上最贵的一个 bug。
    """
    store, _system = seeded
    # 本地有快照、外部账本上没有这笔单 —— 这才是「订单不见了」那一档。
    # （本地也没有快照是另一件事，由 `test_a_missing_snapshot_row_is_an_error_not_a_pass`
    #  钉：那时连比对的左边都没有，报错比报漂移准确。）
    objects.execute(store, "INSERT OR REPLACE INTO order_snapshot (tenant_id, order_id,"
                           " version, sku, amount_paid, paid_at, channel_id,"
                           " policy_version_at_order, payload_json, read_at)"
                           " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (TENANT, "ord-t116-gone", 1, "SKU-T116", 1280.0, "p", "ch",
                     1, "{}", "r"))
    res = _check(store, order_id="ord-t116-gone")
    assert res.status == "ok", res.error
    assert res.output["drift"] is True
    assert "KeyError" in res.output["read_error"]
    assert res.output["current_version"] is None


def test_same_version_but_a_different_amount_is_still_drift(seeded):
    """版本一致而金额对不上 -> 照样漂移。

    外部系统理论上改金额必推版本；真出现"同版本不同金额"，说明我们对那个系统的
    版本语义理解错了 —— 那时更该停下来问人，而不是因为版本号一样就放行。
    """
    store, system = seeded
    system.ext_order(order_id=ORDER, version=1, status=order_tools.ORDER_PAID,
                     amount="999.00", updated_at="t1")
    res = _check(store)
    assert res.output["drift"] is True
    assert "金额对不上" in res.output["reason"]


def test_it_runs_before_the_case_exists(store):
    """建案之前就能跑 —— 这一步的定位是「执行前」，而最该做它的时机是规划期。

    要求先建案会把它推到「已经按这份快照排好了 DAG」之后，那正是它要防的事。
    """
    objects.execute(store, "INSERT OR REPLACE INTO order_snapshot (tenant_id, order_id,"
                           " version, sku, amount_paid, paid_at, channel_id,"
                           " policy_version_at_order, payload_json, read_at)"
                           " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (TENANT, ORDER, 1, "SKU-T116", 1280.0, "p", "ch", 1, "{}", "r"))
    order_tools.reset_order_systems()
    system = order_tools.MockOrderSystem()
    system.ext_order(order_id=ORDER, version=1, status=order_tools.ORDER_PAID,
                     amount="1280.00", updated_at="t0")
    order_tools.register_order_system(SYSTEM, system)

    assert guard.get_case(store, TENANT, CASE) is None
    res = _check(store, order_id=ORDER, order_version=1)
    assert res.status == "ok", res.error
    assert res.output["drift"] is False


def test_without_a_case_and_without_an_order_id_it_refuses_to_guess(store):
    """既读不到 case、入参也没给订单号 -> 抛。**不猜**。"""
    order_tools.reset_order_systems()
    order_tools.register_order_system(SYSTEM, order_tools.MockOrderSystem())
    res = _check(store)
    assert res.status != "ok"
    assert "无从与外部当前版本比对" in str(res.error)


def test_a_missing_snapshot_row_is_an_error_not_a_pass(seeded):
    """手上压根没有那一版快照 -> 抛，不当作一致。"""
    store, _system = seeded
    res = _check(store, order_version=99)
    assert res.status != "ok"
    assert "没有订单快照" in str(res.error)


def test_the_skill_declares_the_tool_it_depends_on():
    """`depends_tools` 写着 `order.query` —— 声明与实际调用不许分叉。"""
    from maos.skills import registry
    contract = registry.get("refund.snapshot_check").contract
    assert contract.depends_tools == ["order.query"]
    assert "不写任何业务表" in contract.security_boundary


# ======================================================================
# 8. 端到端：一条案例，十类引用全指得到
# ======================================================================
def _run_pack(**kw):
    """跑一遍 `case_real_01`，返回 `(结果行, store, plan_id)`。

    直接调 `run_payload` 而不是 `run_file`：文件读取由 §4 那几条钉，
    这里要的是跑完之后的**库**。
    """
    from maos.flows import custom_case
    payload = case_pack.load_case_pack()
    row = custom_case.run_payload(payload, verbose=False, **kw)
    return row


@pytest.fixture(scope="module")
def happy_run():
    """顺利路径跑一次，本节几条共用 —— 一次端到端约两秒，不必每条重跑。"""
    return _run_pack()


def test_the_happy_path_settles_by_observation(happy_run):
    """基线：顺利路径收敛到 settled，且 settled 有观察行撑着（铁律 8）。"""
    assert happy_run["plan_state"] == "DONE"
    assert happy_run["biz_status"] == "settled"
    assert happy_run["settled_observations"] == 1
    assert happy_run["amount_approved"] == "1280.00"
    assert happy_run["public_status"] == "退款已到账"


def test_every_object_the_happy_path_produces_resolves(happy_run):
    """跑完之后，顺利路径**产得出的那九类**逐个 resolve 得到 —— 本轨的主判据。

    判据不是「引用条数 > 0」而是「该有的一类不少、且每一条都指得到」：
    条数是会自己涨的，覆盖面不会。

    **第十类（人工补偿）在顺利路径上本就不该有** —— 钱退成了，没有什么要补偿。
    造一条出来会让「十类齐」这句话变成假的：那条引用会指向一条本不该存在的记录。
    它由下一条用例单独证明挂得上、指得到。
    （`custom_case` 的失败路径今天也不做域内补偿，见它的模块 docstring；
     已记 `docs/BACKLOG.md` 的 `## task-t116`。）
    """
    cov = happy_run["business_ref_coverage"]
    assert cov["missing_types"] == ["compensation_record"], (
        f"顺利路径该有九类：{cov['missing_types']}")
    assert cov["dangling"] == [], f"这几条引用指不到对象：{cov['dangling']}"
    assert cov["covered"] == 9 and cov["total_types"] == 10
    assert cov["resolved"] == cov["total"]


def test_the_tenth_object_attaches_and_resolves_too(seeded):
    """人工补偿这一类照样挂得上、指得到 —— 凑齐十类的最后一块。

    走 `refund.compensate` 真跑一遍（口径同 `scenario_7`：域内补偿由 flows 经一个
    专用 identity 调），不是手工插一条记录：手工插的那条证明不了 skill 会挂引用。
    """
    store, _system = seeded
    # 补偿的前置：有一笔发起过的退款请求，且案子已到 approved。
    C.record_approval(store, tenant_id=TENANT, case_id=CASE, approver="人",
                      decision="approved", reason="单测放行")
    guard.update_biz_status(store, TENANT, CASE, "approved", "test", "iv-approve",
                            reason="单测")
    objects.execute(store, "INSERT OR REPLACE INTO refund_request (tenant_id, case_id,"
                           " request_id, amount, gateway, idempotency_key,"
                           " submitted_at, version) VALUES (?,?,?,?,?,?,?,?)",
                    (TENANT, CASE, "gw_t116", 1280.0, "demo", "rfd-t116", "t0", 1))

    res = SkillInvoker(IDENTITY, store).invoke(
        "refund.compensate",
        {"tenant_id": TENANT, "case_id": CASE, "operator": "人", "reason": "网关明确失败"},
        extras={"plan_id": PLAN_ID, "task_id": TASK_ID, "trace_id": "trace-t116"})
    assert res.status == "ok", res.error

    refs = [r for r in objects.list_business_refs(store, plan_id=PLAN_ID)
            if r["object_type"] == "compensation_record"]
    assert len(refs) == 1, "补偿没挂上引用"
    assert int(refs[0]["object_version"]) >= 1, "修订号没写上"
    assert objects.resolve_business_ref(store, refs[0]) is not None


def test_versioned_objects_carry_a_non_empty_version(happy_run):
    """带版本的那几类，`object_version` 非空。

    版本对不上时 `resolve_business_ref` 会静默返回 None，上一条会先红；
    这一条说得出**哪一类的版本是空的**，排查少一步。
    """
    cov = happy_run["business_ref_coverage"]
    for object_type in ("order_snapshot", "product_snapshot", "policy_rule",
                        "customer_evidence", "refund_request"):
        versions = cov["by_type"][object_type]["versions"]
        assert versions and all(v >= 1 for v in versions), (object_type, versions)


def test_revision_objects_carry_their_revision_on_the_ref(happy_run):
    """`revision` 那几类不走版本收窄，但修订号照样写进了 `object_version`。

    收窄与留痕是两件事：收窄要求列名恰好是 `version`（`resolve_business_ref` 写死的），
    留痕只要求这条引用说得出「这是第几次」。
    """
    cov = happy_run["business_ref_coverage"]
    for object_type in ("approval_record", "finance_entry", "notification"):
        versions = cov["by_type"][object_type]["versions"]
        assert versions and all(v >= 1 for v in versions), (object_type, versions)


def test_drift_stops_the_money_and_leaves_the_status_alone():
    """`--drift`：钱不退、`biz_status` 不动、`SnapshotDrift` 在。

    三条一起看才有意义。只看事件不看钱，等于只证明了「检查跑过」；
    只看钱不看状态，漏掉了「漂移检查有没有顺手改状态」这条铁律 8 的判据。
    """
    row = _run_pack(drift=True)
    assert row["snapshot_check"]["drift"] is True
    assert row["snapshot_check"]["events"] == 1
    assert row["payment_observations"] == [], "漂移未澄清，钱不该退出去"
    assert row["biz_status"] == "submitted", "漂移检查不许改业务状态"
    assert row["public_status"] == "", "还没到能对客户说话的那三态"
    assert [e["decision"] for e in row["human_exits"]] == ["held_for_drift"]


def test_reject_records_a_revision_and_projects_as_rejected():
    """`--reject`：审批留痕（第 1 版 rejected）+ 对外投影是「已驳回」。"""
    row = _run_pack(approve=False)
    assert row["approval_revisions"] == [1]
    assert row["approval_decisions"] == ["rejected"]
    assert row["biz_status"] == "rejected"
    assert row["public_status"] == "已驳回"
    assert row["payment_observations"] == []
