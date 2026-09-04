"""C1: registered domain receipts must withstand evidence tampering."""
from __future__ import annotations

import importlib
import json
import sqlite3

import pytest

from scripts import verify

DOMAINS = {
    "claim": ("claim_case", "claim_id", "claim_payment_observation", "paid"),
    "investigation": ("investigation_case", "case_id", "resolution_observation", "returned"),
    "ap": ("ap_case", "case_id", "ap_payment_observation", "settled"),
}


@pytest.fixture
def domain_case():
    opened = []

    def build(domain, *, status=None, observation=True, mutations=None):
        table, key, obs_table, success = DOMAINS[domain]
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        opened.append(conn)
        conn.execute("CREATE TABLE plan (plan_id TEXT, state TEXT)")
        conn.execute("INSERT INTO plan VALUES ('plan-c1', 'DONE')")
        conn.execute("CREATE TABLE event_log (detail TEXT, event_type TEXT)")
        conn.execute("INSERT INTO event_log VALUES (?, 'SkillInvoked')", (json.dumps({
            "skill": f"{domain}.observe", "invocation_id": "inv-c1"}),))
        conn.execute(f"CREATE TABLE {table} (tenant_id TEXT, {key} TEXT, biz_status TEXT, plan_id TEXT)")
        conn.execute(f"INSERT INTO {table} VALUES ('tenant-c1', 'same-id', ?, 'plan-c1')", (status or success,))
        receipt = {
            "request_id": "req-c1", "observed_state": success, "actor_invocation_id": "inv-c1",
            "carc_code": "", "group_code": "", "poll_seq": 1,
            "message_type": "pacs.004.001.12", "confirmation_code": "", "rejection_code": "",
            "return_reason_code": "AC04", "returned_amount": 100.0,
            "instruction_id": "ins-c1", "bank_reference": "BANK-REF-1", "value_date": "2026-09-05",
        }
        conn.execute(f"CREATE TABLE {obs_table} (tenant_id TEXT, {key} TEXT, " +
                     ", ".join(f"{k} {'REAL' if k == 'returned_amount' else 'INTEGER' if k == 'poll_seq' else 'TEXT'}" for k in receipt) + ")")
        receipt.update(mutations or {})
        if observation:
            columns = ["tenant_id", key, *receipt]
            conn.execute(f"INSERT INTO {obs_table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                         ("tenant-c1", "same-id", *receipt.values()))
        item = {"kind": obs_table, "provenance": obs_table, "tenant_id": "tenant-c1", key: "same-id", **receipt}
        case = verify.Case(name=f"scenario-{domain}", directory="", db_path="", conn=conn,
                           tables=verify.table_names(conn), trace={}, result={"plans": [{
                               "plan_id": "plan-c1", "state": "DONE", "business_outcome": {
                                   "status": "succeeded", "basis": "external_evidence", "plan_state": "DONE",
                                   "source": verify.OUTCOME_SOURCE, "unaudited_evidence_count": 0,
                                   "external_evidence": [item]}}]})
        return case, item

    yield build
    for conn in opened:
        conn.close()


@pytest.mark.parametrize("domain", DOMAINS)
def test_new_domain_receipt_supports_authority_and_outcome(domain_case, domain):
    case, _ = domain_case(domain)
    for check in (verify.check_authoritative_fact, verify.check_business_outcome):
        result = check([case])
        assert (result.status, result.passed, result.total) == (verify.PASS, 1, 1), result.notes


@pytest.mark.parametrize("domain", DOMAINS)
@pytest.mark.parametrize("mutations", [{"observed_state": "unknown"}, {"actor_invocation_id": "forged"}])
def test_new_domain_forged_receipt_is_rejected(domain_case, domain, mutations):
    case, _ = domain_case(domain, mutations=mutations)
    result = verify.check_authoritative_fact([case])
    assert result.status == verify.FAIL, result.notes


@pytest.mark.parametrize("domain,mutations", [
    ("investigation", {"message_type": "camt.029.001.12"}),
    ("investigation", {"returned_amount": None}),
    ("investigation", {"return_reason_code": ""}),
    ("ap", {"bank_reference": ""}),
])
def test_domain_specific_authority_fields_are_required(domain_case, domain, mutations):
    case, _ = domain_case(domain, mutations=mutations)
    assert verify.check_authoritative_fact([case]).status == verify.FAIL
    assert verify.check_business_outcome([case]).status == verify.FAIL


@pytest.mark.parametrize("domain", DOMAINS)
def test_exported_receipt_cannot_change_actor(domain_case, domain):
    case, item = domain_case(domain)
    item["actor_invocation_id"] = "forged"
    assert verify.check_business_outcome([case]).status == verify.FAIL


@pytest.mark.parametrize("domain", DOMAINS)
def test_terminal_case_with_missing_observation_table_fails(domain_case, domain):
    case, _ = domain_case(domain)
    case.conn.execute(f"DROP TABLE {DOMAINS[domain][2]}")
    case.tables = verify.table_names(case.conn)
    assert verify.check_authoritative_fact([case]).status == verify.FAIL


def test_empty_authority_and_outcome_are_skipped(domain_case):
    case, _ = domain_case("claim", observation=False)
    case.conn.execute("DELETE FROM claim_case")
    case.conn.execute("DELETE FROM plan")
    for check in (verify.check_authoritative_fact, verify.check_business_outcome):
        result = check([case])
        assert result.status == verify.SKIP and result.total == 0


def add_history(case, *, domain="claim", tenant="tenant-c1"):
    case.conn.execute("CREATE TABLE kb_doc (tenant_id TEXT, doc_id TEXT, biz_type TEXT, kind TEXT, source_case_id TEXT)")
    case.conn.execute("INSERT INTO kb_doc VALUES (?, 'history-c1', ?, 'history_case', 'same-id')", (tenant, domain))
    case.tables = verify.table_names(case.conn)


def test_history_uses_its_domain_not_a_refund_with_same_id(domain_case):
    case, _ = domain_case("claim", status="compensated")
    case.conn.execute("CREATE TABLE refund_case (tenant_id TEXT, case_id TEXT, biz_status TEXT)")
    case.conn.execute("INSERT INTO refund_case VALUES ('tenant-c1', 'same-id', 'settled')")
    add_history(case)
    assert verify.check_history_case([case]).status == verify.FAIL


def test_history_cannot_borrow_success_from_another_tenant(domain_case):
    case, _ = domain_case("claim", status="compensated")
    case.conn.execute("INSERT INTO claim_case VALUES ('other-tenant', 'same-id', 'paid', 'other-plan')")
    add_history(case)
    assert verify.check_history_case([case]).status == verify.FAIL


def test_local_new_domain_history_is_checked(domain_case):
    case, _ = domain_case("claim")
    add_history(case)
    result = verify.check_history_case([case])
    assert (result.status, result.passed, result.total) == (verify.PASS, 1, 1)


def test_domain_report_explicitly_skips_missing_domains(domain_case):
    case, _ = domain_case("claim")
    assert hasattr(verify, "domain_checks"), "verify --domains requires explicit per-domain results"
    results = verify.domain_checks([case])
    absent = [r for r in results if r.key.startswith(("ap/", "investigation/"))]
    assert len(absent) == 6
    assert all(r.status == verify.SKIP and r.total == 0 for r in absent)


def test_load_cases_preserves_isolated_runtime_databases(tmp_path):
    evidence = tmp_path / "evidence"
    databases = tmp_path / "databases"
    for relative, status in (("scenario-8", "DONE"), ("scenario-8/runtime-2", "FAILED")):
        bundle = evidence / relative
        bundle.mkdir(parents=True)
        for name in ("trace.json", "result.json"):
            (bundle / name).write_text('# generated at 2026-09-05T00:00:00+00:00 from c1\n{}')
        db_dir = databases / relative
        db_dir.mkdir(parents=True)
        conn = sqlite3.connect(db_dir / "maos.db")
        conn.execute("CREATE TABLE plan (plan_id TEXT, state TEXT)")
        conn.execute("INSERT INTO plan VALUES ('same-plan-id', ?)", (status,))
        conn.commit()
        conn.close()
    cases = verify.load_cases(str(evidence), str(databases))
    try:
        assert [c.name for c in cases] == ["scenario-8", "scenario-8/runtime-2"]
        assert [c.conn.execute("SELECT state FROM plan").fetchone()[0] for c in cases] == ["DONE", "FAILED"]
    finally:
        for case in cases:
            case.conn.close()
