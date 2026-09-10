"""客户侧入站 CLI 与 `run_case.py --stall`（T120）。

两条命令各卖一句话，本文件钉住它们：

  · `scripts/case_inbound.py` —— **客户确认与投诉不再是靶场事件**，有一条客户走得进来
    的路；而且这条路**碰不到 arrival**（客户说收到了也换不来「到账」，铁律 8）。
  · `scripts/run_case.py --stall` —— **所有 Agent 都回复完成 ≠ 业务成功**：
    任务全 DONE，`arrival=unknown`、`business_success=false`。

`--stall` 那条要真跑一遍完整 DAG（约数秒）。它是本轨的招牌证据，值这个时间 ——
用桩替掉的话，钉住的就只是「我们打印了这句话」。
"""

from __future__ import annotations

import copy
import importlib.util
import json
import pathlib
import sys

import pytest

from maos.core.store import SqliteStore
from maos.domain.refund import guard, objects, outcome

ROOT = pathlib.Path(__file__).resolve().parents[2]
SAMPLE = ROOT / "scenarios" / "custom" / "refund-case.json"

TENANT = "tnt-inbound"
CASE = "case-inbound-1"
PLAN = "plan-inbound-1"


def _load_script(name: str):
    key = f"_t120_{name}"
    spec = importlib.util.spec_from_file_location(key, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


case_inbound = _load_script("case_inbound")
run_case = _load_script("run_case")


@pytest.fixture()
def db(tmp_path) -> str:
    """一个有案子、有通知、还没有到账观察的库。"""
    path = str(tmp_path / "maos.db")
    store = SqliteStore(path)
    store.init_schema()
    objects.ensure_schema(store)
    objects.execute(
        store,
        "INSERT INTO order_snapshot (tenant_id, order_id, version, sku, amount_paid,"
        " paid_at, channel_id, policy_version_at_order, payload_json, read_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (TENANT, "ord-1", 1, "sku-1", 100.0, "2026-07-01T00:00:00+00:00", "ch-1", 1, "{}",
         "2026-07-02T00:00:00+00:00"))
    guard.create_case(store, tenant_id=TENANT, case_id=CASE, channel_id="ch-1",
                      order_id="ord-1", order_version=1, sku="sku-1",
                      reason_code="quality_defect", amount_claimed=100.0, plan_id=PLAN,
                      actor_skill="refund.intake", invocation_id="inv-1")
    objects.execute(
        store,
        "INSERT INTO notification (tenant_id, case_id, channel, content_digest, sent_at,"
        " ack_at) VALUES (?,?,?,?,?,?)",
        (TENANT, CASE, "sms", "d1", "2026-07-10T00:00:00+00:00", None))
    return path


def _run(argv: list[str]) -> int:
    return case_inbound.main(argv)


# ======================================================================
# case_inbound.py
# ======================================================================
def test_confirm_records_ack_and_recomputes(db, capsys):
    assert _run(["--db", db, "--confirm", CASE]) == 0
    out = capsys.readouterr().out
    assert "客户已确认" in out

    store = SqliteStore(db)
    row = outcome.read_case_outcome(store, tenant_id=TENANT, case_id=CASE)
    assert row["customer_confirmation"] == outcome.CONFIRMATION_CONFIRMED
    notes = objects.query(store, "SELECT ack_at FROM notification WHERE tenant_id=?"
                                 " AND case_id=?", (TENANT, CASE))
    assert notes[0]["ack_at"], "确认要落到 notification.ack_at —— 晋升规则读的是那一列"


def test_confirm_does_not_grant_arrival(db):
    """# 论证：客户说收到了也换不来「到账」—— 钱到没到账归网关（铁律 8）。"""
    assert _run(["--db", db, "--confirm", CASE]) == 0
    row = outcome.read_case_outcome(SqliteStore(db), tenant_id=TENANT, case_id=CASE)
    assert row["arrival"] == outcome.ARRIVAL_UNKNOWN
    assert not row["business_success"]


def test_complain_then_close(db, capsys):
    assert _run(["--db", db, "--complain", CASE, "退款一直没到"]) == 0
    assert "投诉已受理" in capsys.readouterr().out

    store = SqliteStore(db)
    rows = outcome.list_complaints(store, tenant_id=TENANT, case_id=CASE)
    assert len(rows) == 1 and rows[0]["closed_at"] is None
    assert outcome.read_case_outcome(
        store, tenant_id=TENANT, case_id=CASE)["complaint"] == outcome.COMPLAINT_OPEN

    digest = rows[0]["content_digest"]
    assert _run(["--db", db, "--close-complaint", CASE, digest, "已补付"]) == 0
    assert outcome.read_case_outcome(
        store, tenant_id=TENANT, case_id=CASE)["complaint"] == outcome.COMPLAINT_CLOSED


def test_dispute_flips_business_success(db):
    objects.execute(
        SqliteStore(db),
        "INSERT INTO payment_observation (tenant_id, case_id, request_id, gateway_code,"
        " raw_receipt_json, observed_state, observed_at, actor_invocation_id)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (TENANT, CASE, "req-1", "10000", "{}", "settled",
         "2026-07-11T00:00:00+00:00", "inv-obs"))
    assert _run(["--db", db, "--confirm", CASE]) == 0
    assert outcome.read_case_outcome(
        SqliteStore(db), tenant_id=TENANT, case_id=CASE)["business_success"]

    assert _run(["--db", db, "--dispute", CASE]) == 0
    row = outcome.read_case_outcome(SqliteStore(db), tenant_id=TENANT, case_id=CASE)
    assert row["customer_confirmation"] == outcome.CONFIRMATION_DISPUTED
    assert not row["business_success"]


def test_json_output_is_machine_readable(db, capsys):
    assert _run(["--db", db, "--complain", CASE, "少退了钱", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["case_id"] == CASE and doc["tenant_id"] == TENANT
    assert doc["case_outcome"]["complaint"] == "open"
    assert len(doc["complaints"]) == 1


def test_missing_db_and_missing_case_have_distinct_exit_codes(db, tmp_path, capsys):
    """库不在与案子不在是两回事，退出码分开 —— 打对了命令指错了案子看得出来。"""
    assert _run(["--db", str(tmp_path / "nope.db"), "--confirm", CASE]) == \
        case_inbound.EXIT_NO_DB
    assert _run(["--db", db, "--confirm", "case-not-here"]) == case_inbound.EXIT_NO_CASE


def test_show_does_not_lie_about_writing(db, capsys):
    """`--show` 首次会现算并落库 —— 标题里如实说，不写「只读」。"""
    assert _run(["--db", db, "--show", CASE]) == 0
    first = capsys.readouterr().out
    assert "现算并落库" in first

    assert _run(["--db", db, "--show", CASE]) == 0
    assert "读已落库的那一行" in capsys.readouterr().out


def test_confirm_without_notification_is_refused(tmp_path, capsys):
    path = str(tmp_path / "bare.db")
    store = SqliteStore(path)
    store.init_schema()
    objects.ensure_schema(store)
    objects.execute(
        store,
        "INSERT INTO order_snapshot (tenant_id, order_id, version, sku, amount_paid,"
        " paid_at, channel_id, policy_version_at_order, payload_json, read_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (TENANT, "ord-1", 1, "sku-1", 100.0, "2026-07-01T00:00:00+00:00", "ch-1", 1, "{}",
         "2026-07-02T00:00:00+00:00"))
    guard.create_case(store, tenant_id=TENANT, case_id=CASE, channel_id="ch-1",
                      order_id="ord-1", order_version=1, sku="sku-1",
                      reason_code="quality_defect", amount_claimed=100.0, plan_id=PLAN,
                      actor_skill="refund.intake", invocation_id="inv-1")
    assert _run(["--db", path, "--confirm", CASE]) == 2
    assert "无从确认" in capsys.readouterr().err


# ======================================================================
# run_case.py --stall
# ======================================================================
def test_stall_all_done_but_business_not_successful(capsys):
    """# 论证：「所有 Agent 都回复完成」只表示协作结束，不代表业务成功。

    这是本轨的招牌证据。三条一起才成立，少一条都不算：
    任务全 DONE、观察行 0 条、`arrival=unknown` 且 `business_success=false`。
    """
    from maos.flows.custom_case import load

    payload = copy.deepcopy(load(SAMPLE))
    row, outcome_row = run_case._run_stalled_payload(payload, verbose=False)

    assert row["plan_state"] == "DONE"
    assert all(t["state"] == "DONE" for t in row["tasks"]), (
        "每个 Agent 都回复完成了 —— 这正是要演的那一半")
    assert row["payment_observations"] == [], (
        "轮询到 max_polls 上限仍非终态，payment.observe 一行都不写（铁律 8）")
    assert outcome_row["arrival"] == "unknown"
    assert outcome_row["arrival_basis"] == ""
    assert not outcome_row["business_success"]


def test_stall_settle_after_is_above_the_poll_ceiling():
    """`--stall` 的 settle_after 必须大于 `payment.observe` 的轮询上限。

    小于等于它的话，轮询在到顶之前就问出了终态，这条路径演的东西当场没了 ——
    而输出看上去只是「跑成功了」，不会报错。
    """
    from maos.skills.builtin.refund.payment_observe import DEFAULT_MAX_POLLS

    assert run_case.STALL_SETTLE_AFTER > DEFAULT_MAX_POLLS
