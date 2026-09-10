"""业务结果四判据（T120）—— `maos/domain/refund/outcome.py`。

本文件的第一断言就是这一整轨的论点：

    所有任务都 DONE、Plan 也 DONE，只要 `payment_observation` 里没有 settled 那一行，
    `arrival` 就只能是 `unknown`，`business_success` 就只能是 false。

标了 `# 论证：` 的断言是复赛材料里那几句话的机器化版本，评审时可按前缀捞出来对。

第二类断言守的是 **arrival 的来源**（铁律 8 的本轨落点）：把 `biz_status` 推到
`settled` 而不落观察行，四判据必须仍然说「没到账」。这条负例是本文件的核心 ——
它守的是「不许从 biz_status 反推到账」这条约束在**运行时**成立，而不只是写在注释里。
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest

from maos.core.store import SqliteStore
from maos.domain.refund import objects, outcome


@pytest.fixture()
def store():
    s = SqliteStore()
    s.init_schema()
    objects.ensure_schema(s)
    outcome.ensure_outcome_schema(s)
    return s


TENANT = "tnt-t120"
CASE = "case-t120-0001"


def _observe(store, *, state: str, request_id: str = "req-1",
             at: str = "2026-09-10T00:00:00+00:00", code: str = "10000",
             receipt: dict | None = None) -> None:
    objects.execute(
        store,
        "INSERT INTO payment_observation (tenant_id, case_id, request_id, gateway_code,"
        " raw_receipt_json, observed_state, observed_at, actor_invocation_id)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (TENANT, CASE, request_id, code,
         json.dumps(receipt or {}, ensure_ascii=False), state, at, "inv-1"))


def _notify(store, *, ack: str | None, digest: str = "d1") -> None:
    objects.execute(
        store,
        "INSERT INTO notification (tenant_id, case_id, channel, content_digest, sent_at,"
        " ack_at) VALUES (?,?,?,?,?,?)",
        (TENANT, CASE, "sms", digest, "2026-09-10T00:00:00+00:00", ack))


def _compensate(store, *, kind: str = "manual_ticket") -> None:
    objects.execute(
        store,
        "INSERT INTO compensation_record (tenant_id, case_id, kind, detail_json,"
        " executed_at, operator) VALUES (?,?,?,?,?,?)",
        (TENANT, CASE, kind, "{}", "2026-09-10T00:00:00+00:00", "ops"))


# ======================================================================
# 1. 纯函数：四判据各自的取值
# ======================================================================
def test_no_observation_means_unknown_not_unsettled():
    """# 论证：轮询到顶问不出终态是 unknown，不是 unsettled。

    「我问累了」和「网关说没退成」是两回事。混起来会让一笔可能已经出去的钱
    在账面上凭空少一笔（铁律 8）。
    """
    row = outcome.compute_case_outcome(observations=[])
    assert row["arrival"] == outcome.ARRIVAL_UNKNOWN
    assert row["arrival_basis"] == "", "没有观察行就没有可回查的依据，不许编一个出来"
    assert row["business_success"] is False


def test_failed_observation_means_unsettled_with_basis():
    row = outcome.compute_case_outcome(observations=[
        {"observed_state": "failed", "request_id": "r9", "observed_at": "T1"}])
    assert row["arrival"] == outcome.ARRIVAL_UNSETTLED
    assert row["arrival_basis"] == "payment_observation:r9@T1"
    assert row["business_success"] is False


def test_settled_basis_points_at_the_last_settled_row():
    """basis 指向最后一条 settled 观察 —— 一单可能被观察多次。"""
    row = outcome.compute_case_outcome(observations=[
        {"observed_state": "processing", "request_id": "r1", "observed_at": "T1"},
        {"observed_state": "settled", "request_id": "r1", "observed_at": "T2"},
        {"observed_state": "settled", "request_id": "r1", "observed_at": "T3"},
    ])
    assert row["arrival"] == outcome.ARRIVAL_SETTLED
    assert row["arrival_basis"] == "payment_observation:r1@T3"


def test_business_success_formula_is_the_contract_one():
    """# 论证：business_success = 到账 且 未提异议 且 投诉没开着（契约 §E 逐字）。"""
    settled = [{"observed_state": "settled", "request_id": "r", "observed_at": "T"}]

    ok = outcome.compute_case_outcome(observations=settled)
    assert ok["business_success"] is True

    disputed = outcome.compute_case_outcome(
        observations=settled, recorded_confirmation=outcome.CONFIRMATION_DISPUTED)
    assert disputed["business_success"] is False, "客户提了异议就不算业务成功"

    complained = outcome.compute_case_outcome(
        observations=settled, complaints=[{"closed_at": None}])
    assert complained["business_success"] is False, "投诉开着一票否决"

    closed = outcome.compute_case_outcome(
        observations=settled, complaints=[{"closed_at": "T2"}])
    assert closed["complaint"] == outcome.COMPLAINT_CLOSED
    assert closed["business_success"] is True, "关掉的投诉不再否决"


def test_evidence_complete_is_not_part_of_business_success():
    """证据不全不等于业务没成 —— 两件事，分两个字段。

    混起来的症状：一单真到账、客户也确认了，只因为少一条业务对象引用就被判成
    业务失败，于是「业务成功率」这个数字实际在衡量「引用挂全了没有」。
    """
    row = outcome.compute_case_outcome(
        observations=[{"observed_state": "settled", "request_id": "r", "observed_at": "T"}],
        resolved_ref_types=(), required_ref_types=("refund_case", "order_snapshot"))
    assert row["evidence_complete"] is False
    assert row["business_success"] is True


def test_evidence_complete_needs_every_required_type():
    required = ("refund_case", "order_snapshot", "policy_rule")
    partial = outcome.compute_case_outcome(
        resolved_ref_types=("refund_case", "order_snapshot"), required_ref_types=required)
    assert partial["evidence_complete"] is False
    assert partial["evidence"]["missing_ref_types"] == ["policy_rule"]

    full = outcome.compute_case_outcome(
        resolved_ref_types=required, required_ref_types=required)
    assert full["evidence_complete"] is True


def test_manual_correction_prefers_compensation_over_manual_receipt():
    """补偿工单与人工回执都在时判 compensated —— 走了工单的那一档信息更全。"""
    manual_obs = [{"observed_state": "settled", "request_id": "r", "observed_at": "T",
                   "raw_receipt_json": json.dumps({"source": "manual"})}]
    both = outcome.compute_case_outcome(
        observations=manual_obs, compensations=[{"kind": "manual_ticket"}])
    assert both["manual_correction"] == outcome.CORRECTION_COMPENSATED

    only_manual = outcome.compute_case_outcome(observations=manual_obs)
    assert only_manual["manual_correction"] == outcome.CORRECTION_OVERRIDDEN

    neither = outcome.compute_case_outcome(observations=[
        {"observed_state": "settled", "request_id": "r", "observed_at": "T"}])
    assert neither["manual_correction"] == outcome.CORRECTION_NONE


def test_dirty_receipt_json_does_not_break_the_judgement():
    """回执 JSON 坏了就当没来源。判据不该被一份脏数据掀翻。"""
    row = outcome.compute_case_outcome(observations=[
        {"observed_state": "settled", "request_id": "r", "observed_at": "T",
         "raw_receipt_json": "{不是 json"}])
    assert row["manual_correction"] == outcome.CORRECTION_NONE
    assert row["arrival"] == outcome.ARRIVAL_SETTLED


def test_recorded_confirmation_beats_ack():
    """客户先签收、后提异议 —— 结论是异议，不许被早先那次 ack 翻回来。"""
    acked = [{"ack_at": "T1"}]
    row = outcome.compute_case_outcome(
        notifications=acked, recorded_confirmation=outcome.CONFIRMATION_DISPUTED)
    assert row["customer_confirmation"] == outcome.CONFIRMATION_DISPUTED


# ======================================================================
# 2. 铁律 8：arrival 只能来自 payment_observation
# ======================================================================
def test_settled_biz_status_without_observation_is_still_unknown(store):
    """# 论证：把 biz_status 推到 settled 也换不来「到账」——
    到账只由 payment_observation 的行决定（铁律 8）。

    这是本轨的核心负例。走 guard 正规推到 settled 是不可能的（它要求同事务附回执），
    所以这里直接对着表按 T120 之前那种「从 biz_status 反推」的做法验一遍：
    库里 biz_status 说 settled、观察表空着，四判据必须仍然说 unknown。
    """
    guard_row = {"biz_status": "settled"}
    row = outcome.record_case_outcome(store, tenant_id=TENANT, case_id=CASE)
    assert guard_row["biz_status"] == "settled"          # 反推的诱惑就在这一行
    assert row["arrival"] == outcome.ARRIVAL_UNKNOWN
    assert row["arrival_basis"] == ""
    assert not row["business_success"]


def test_arrival_basis_round_trips_to_the_observation_row(store):
    """basis 必须能把那一行原样查回来 —— verify 第 10 项就是这么回查的。"""
    _observe(store, state="settled", request_id="req-7", at="2026-09-10T01:02:03+00:00")
    row = outcome.record_case_outcome(store, tenant_id=TENANT, case_id=CASE)
    assert row["arrival"] == outcome.ARRIVAL_SETTLED

    prefix, _, tail = row["arrival_basis"].partition(":")
    request_id, _, observed_at = tail.partition("@")
    assert prefix == "payment_observation"
    hit = objects.query(
        store, "SELECT observed_state FROM payment_observation WHERE tenant_id=?"
               " AND case_id=? AND request_id=? AND observed_at=?",
        (TENANT, CASE, request_id, observed_at))
    assert len(hit) == 1 and hit[0]["observed_state"] == "settled"


# ======================================================================
# 3. 落库、事件、重算
# ======================================================================
def test_record_is_idempotent_and_recomputes_in_place(store):
    _observe(store, state="settled")
    first = outcome.record_case_outcome(store, tenant_id=TENANT, case_id=CASE,
                                        plan_id="plan-x")
    second = outcome.record_case_outcome(store, tenant_id=TENANT, case_id=CASE,
                                         plan_id="plan-x")
    rows = objects.query(store, "SELECT * FROM case_outcome WHERE tenant_id=? AND case_id=?",
                         (TENANT, CASE))
    assert len(rows) == 1, "一个 case 一行，重算就地覆盖"
    assert first["arrival"] == second["arrival"] == outcome.ARRIVAL_SETTLED

    events = [e for e in store.list_event_log("plan-x")
              if e["event_type"] == outcome.EVENT_OUTCOME_COMPUTED]
    assert len(events) == 2, "每次重算各留一条事件 —— 结论变过几次要看得见"
    detail = events[0]["detail"]
    detail = json.loads(detail) if isinstance(detail, str) else detail
    assert detail["tenant_id"] == TENANT and detail["case_id"] == CASE, "契约 §F"


def test_unknown_flips_to_settled_when_a_receipt_arrives_later(store):
    """先问不出下落、后来人工补录回执 —— 结论跟着变，这是真实路径（T117 的闭环）。"""
    before = outcome.record_case_outcome(store, tenant_id=TENANT, case_id=CASE)
    assert before["arrival"] == outcome.ARRIVAL_UNKNOWN

    _observe(store, state="settled", request_id="late",
             receipt={"source": "manual"}, at="2026-09-11T00:00:00+00:00")
    after = outcome.record_case_outcome(store, tenant_id=TENANT, case_id=CASE)
    assert after["arrival"] == outcome.ARRIVAL_SETTLED
    assert after["manual_correction"] == outcome.CORRECTION_OVERRIDDEN
    assert after["arrival_basis"].endswith("@2026-09-11T00:00:00+00:00")


def test_no_event_without_plan_id(store):
    """不给 plan_id 就不落事件：`event_log.plan_id` 是必填，编一个假的更糟。"""
    _observe(store, state="settled")
    outcome.record_case_outcome(store, tenant_id=TENANT, case_id=CASE)
    assert store.list_event_log("") == []


# ======================================================================
# 4. 入站：确认 / 异议 / 投诉
# ======================================================================
def test_confirm_writes_ack_so_both_judgements_agree(store):
    """确认要同时落 `notification.ack_at` —— 晋升规则读的是那一列。

    只写 `case_outcome` 会造出第二份「客户认了没有」的事实，
    症状是四判据说确认了、`classify_case` 说没有。
    """
    _observe(store, state="settled")
    _notify(store, ack=None)
    row = outcome.record_confirmation(store, tenant_id=TENANT, case_id=CASE)
    assert row["customer_confirmation"] == outcome.CONFIRMATION_CONFIRMED
    notes = objects.query(store, "SELECT ack_at FROM notification WHERE tenant_id=?"
                                 " AND case_id=?", (TENANT, CASE))
    assert notes[0]["ack_at"], "ack 必须真的落到通知行上"


def test_confirm_without_notification_is_refused(store):
    """一条通知都没发出去，客户无从确认 —— 不静默成功。"""
    with pytest.raises(outcome.OutcomeError):
        outcome.record_confirmation(store, tenant_id=TENANT, case_id=CASE)


def test_confirm_does_not_touch_arrival(store):
    """# 论证：客户说收到了也换不来「到账」—— 钱到没到账归网关（铁律 8）。"""
    _notify(store, ack=None)
    row = outcome.record_confirmation(store, tenant_id=TENANT, case_id=CASE)
    assert row["customer_confirmation"] == outcome.CONFIRMATION_CONFIRMED
    assert row["arrival"] == outcome.ARRIVAL_UNKNOWN
    assert not row["business_success"]


def test_dispute_overrides_a_previous_confirmation(store):
    _observe(store, state="settled")
    _notify(store, ack=None)
    outcome.record_confirmation(store, tenant_id=TENANT, case_id=CASE)
    row = outcome.record_confirmation(store, tenant_id=TENANT, case_id=CASE,
                                      decision=outcome.CONFIRMATION_DISPUTED)
    assert row["customer_confirmation"] == outcome.CONFIRMATION_DISPUTED
    assert not row["business_success"]


def test_bad_confirmation_decision_is_refused(store):
    with pytest.raises(outcome.OutcomeError):
        outcome.record_confirmation(store, tenant_id=TENANT, case_id=CASE, decision="ok")


def test_complaint_opens_closed_and_flips_business_success(store):
    """# 论证：钱到了、客户也签收了，只要投诉还开着，这单业务就没算成。"""
    _observe(store, state="settled")
    _notify(store, ack="2026-09-10T02:00:00+00:00")
    assert outcome.record_case_outcome(store, tenant_id=TENANT,
                                       case_id=CASE)["business_success"]

    opened = outcome.record_complaint(store, tenant_id=TENANT, case_id=CASE,
                                      content="到账金额比核准少 50 元")
    assert opened["complaint"] == outcome.COMPLAINT_OPEN
    assert not opened["business_success"]

    digest = outcome.list_complaints(store, tenant_id=TENANT, case_id=CASE)[0]["content_digest"]
    closed = outcome.close_complaint(store, tenant_id=TENANT, case_id=CASE,
                                     content_digest=digest, resolution="已补付差额")
    assert closed["complaint"] == outcome.COMPLAINT_CLOSED
    assert closed["business_success"]


def test_complaint_stores_digest_not_the_text(store):
    """投诉正文不落库 —— 判据只需要「有没有、开没开着」，原文归工单系统。"""
    text = "我的手机号是 13800000000，退款没到"
    outcome.record_complaint(store, tenant_id=TENANT, case_id=CASE, content=text)
    rows = outcome.list_complaints(store, tenant_id=TENANT, case_id=CASE)
    assert rows[0]["content_digest"] == outcome.digest_of(text)
    assert text not in json.dumps([dict(r) for r in rows], ensure_ascii=False)


def test_same_complaint_twice_is_one_row(store):
    outcome.record_complaint(store, tenant_id=TENANT, case_id=CASE, content="同一条")
    outcome.record_complaint(store, tenant_id=TENANT, case_id=CASE, content="同一条")
    assert len(outcome.list_complaints(store, tenant_id=TENANT, case_id=CASE)) == 1


def test_empty_complaint_is_refused(store):
    with pytest.raises(outcome.OutcomeError):
        outcome.record_complaint(store, tenant_id=TENANT, case_id=CASE, content="   ")


# ======================================================================
# 5. 清单常量与 schema
# ======================================================================
def test_evidence_ref_types_cover_the_contract_ten():
    """T116 合入后：十类恒要，人工补偿只在案子真走到补偿时才要 —— 顺利路径本就没有工单。"""
    assert len(outcome.EVIDENCE_REF_TYPES) == 10
    assert outcome.EVIDENCE_REF_TYPES_ON_COMPENSATION == ("compensation_record",)
    assert "compensation_record" not in outcome.EVIDENCE_REF_TYPES
    assert set(outcome.required_ref_types()) == set(outcome.EVIDENCE_REF_TYPES)
    assert len(outcome.required_ref_types(compensated=True)) == 11
    assert "compensation_record" in outcome.required_ref_types(compensated=True)


def test_enum_domains_match_the_contract():
    """契约 §E 的取值域逐字对齐。改了这里就要同步 verify.py 第 10 项。"""
    assert set(outcome.VALID_ARRIVAL) == {"settled", "unsettled", "unknown"}
    assert set(outcome.VALID_CONFIRMATION) == {"confirmed", "disputed", "none"}
    assert set(outcome.VALID_CORRECTION) == {"none", "overridden", "compensated"}
    assert set(outcome.VALID_COMPLAINT) == {"none", "open", "closed"}


def test_schema_is_idempotent_and_adds_only_new_tables(store):
    """建表可连跑；而且只新增表，一张现有表都不碰（铁律 1）。"""
    before = {r["name"] for r in objects.query(
        store, "SELECT name FROM sqlite_master WHERE type='table'")}
    outcome.ensure_outcome_schema(store)
    outcome.ensure_outcome_schema(store)
    after = {r["name"] for r in objects.query(
        store, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert after == before
    assert {"case_outcome", "complaint", "failure_hint_index"} <= after


def test_enum_check_constraints_are_enforced_by_the_db(store):
    """取值域写进了 CHECK，绕开 Python 直接写也拦得住。"""
    with pytest.raises(Exception):
        objects.execute(
            store,
            "INSERT INTO case_outcome (tenant_id, case_id, arrival, computed_at)"
            " VALUES (?,?,?,?)", (TENANT, "case-bad", "arrived", "T"))


# ======================================================================
# 6. 导出侧：make_evidence 的两个来源
# ======================================================================
def _load_make_evidence():
    key = "_t120_make_evidence"
    root = pathlib.Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(key, root / "scripts" / "make_evidence.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


def _file_store(path: str):
    from maos.core.store import SqliteStore as _S

    s = _S(path)
    s.init_schema()
    objects.ensure_schema(s)
    return s


def _seed_for_export(s) -> None:
    """造一条已收口的 case。

    `refund_case` 走**底层连接**而不是 `objects.execute` —— 后者会拒绝任何对这张表的
    写入（那是 `guard.py` 的专属入口，铁律 8）。测试夹具要的是一行数据，不是一次
    合法的业务动作，所以从连接直落；口径同 `test_verify_warn._build_refund_db`。
    """
    s._conn.execute(
        "INSERT INTO refund_case (tenant_id, case_id, channel_id, order_id, order_version,"
        " sku, reason_code, amount_claimed, biz_status, plan_id, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (TENANT, CASE, "ch-1", "ord-1", 1, "sku-1", "quality", 10.0, "settled",
         "plan-export", "2026-09-10T00:00:00+00:00"))
    s._conn.commit()
    _observe(s, state="settled", request_id="req-e", at="2026-09-10T03:00:00+00:00")


def test_export_derives_the_four_criteria_when_the_table_is_absent(tmp_path):
    """老库（没有 `case_outcome` 表）导出时现算 —— 留空会把「没有四判据」和
    「确实没到账」混成一个样子。"""
    me = _load_make_evidence()
    path = str(tmp_path / "old.db")
    _seed_for_export(_file_store(path))

    conn = me.connect_ro(path)
    try:
        rows = me.case_outcomes(conn, "plan-export", me.table_names(conn))
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0]["source"] == "derived-at-export-time"
    assert rows[0]["arrival"] == "settled"
    assert rows[0]["arrival_basis"] == "payment_observation:req-e@2026-09-10T03:00:00+00:00"
    assert rows[0]["business_success"] is True


def test_export_prefers_the_row_the_runtime_already_wrote(tmp_path):
    """表里有行就取它 —— 那是运行时算完落下的，带着它自己的 computed_at。"""
    me = _load_make_evidence()
    path = str(tmp_path / "new.db")
    s = _file_store(path)
    _seed_for_export(s)
    outcome.record_case_outcome(s, tenant_id=TENANT, case_id=CASE)

    conn = me.connect_ro(path)
    try:
        rows = me.case_outcomes(conn, "plan-export", me.table_names(conn))
    finally:
        conn.close()
    assert rows[0]["source"] == "case_outcome-table"
    assert rows[0]["computed_at"], "落库那一行必须带算的时刻"


def test_export_is_a_noop_without_the_refund_domain(tmp_path):
    me = _load_make_evidence()
    path = str(tmp_path / "bare.db")
    from maos.core.store import SqliteStore as _S

    _S(path).init_schema()
    conn = me.connect_ro(path)
    try:
        assert me.case_outcomes(conn, "plan-x", me.table_names(conn)) == []
    finally:
        conn.close()
