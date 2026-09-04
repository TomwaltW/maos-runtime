"""C1：显式多域入口与真实外部观察导出，默认演示口径仍冻结为八束。"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from maos import main as maos_main
from scripts import make_evidence


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("scenario", [8, 9, 10])
def test_cli_dispatches_new_domains(scenario, monkeypatch):
    called = []

    def run(n, *, matrix):
        called.append((n, matrix))
        return 0

    monkeypatch.setattr(maos_main, "_run_scenario", run)
    assert maos_main.main(["--scenario", str(scenario)]) == 0
    assert called == [(scenario, False)]


@pytest.fixture(scope="module")
def domain_bundles(tmp_path_factory):
    out = tmp_path_factory.mktemp("c1-domains")
    proc = subprocess.run(
        [sys.executable, "scripts/make_evidence.py", "--domains", "--out", str(out)],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return out


def test_expanded_bundle_is_explicit_and_complete(domain_bundles):
    index = make_evidence.load_evidence_json(str(domain_bundles / "INDEX.json"))
    assert index["requested"] == [*range(1, 11), "R5"]
    assert len(index["produced"]) == 11
    for info in index["produced"]:
        bundle = ROOT / info["dir"]
        for path in bundle.rglob("*"):
            if not path.is_file() or path.suffix == ".db":
                continue
            assert path.read_text().startswith("# generated at "), path


@pytest.mark.parametrize("scenario,observation_table,success_state", [
    (8, "claim_payment_observation", "paid"),
    (10, "ap_payment_observation", "settled"),
])
def test_failure_runtime_stays_isolated(domain_bundles, scenario, observation_table, success_state):
    import sqlite3

    directory = domain_bundles / f"scenario-{scenario}" / "runtime-2"
    assert directory.is_dir()
    with sqlite3.connect(directory / "maos.db") as conn:
        assert conn.execute(
            f"SELECT COUNT(*) FROM {observation_table} WHERE observed_state=?",
            (success_state,),
        ).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM plan WHERE state='FAILED'").fetchone()[0] > 0
    index = make_evidence.load_evidence_json(str(domain_bundles / "INDEX.json"))
    info = next(i for i in index["produced"] if i["scenario"] == scenario)
    assert len(info["runtimes"]) == 1
    assert (ROOT / info["runtimes"][0]["dir"]).resolve() == directory.resolve()


@pytest.mark.parametrize("scenario,kind,id_key,receipt_key", [
    (8, "claim_payment_observation", "claim_id", "request_id"),
    (9, "resolution_observation", "case_id", "message_type"),
    (10, "ap_payment_observation", "case_id", "bank_reference"),
])
def test_domain_success_exports_real_external_observation(
        domain_bundles, scenario, kind, id_key, receipt_key):
    result = make_evidence.load_evidence_json(
        str(domain_bundles / f"scenario-{scenario}" / "result.json"))
    successes = [p for p in result["plans"] if p["state"] == "DONE"]
    assert successes
    for plan in successes:
        outcome = plan["business_outcome"]
        assert outcome["status"] == "succeeded"
        evidence = outcome["external_evidence"]
        assert evidence
        assert all(e["kind"] == kind and e[id_key] and e[receipt_key]
                   and e["actor_invocation_id"] for e in evidence)
        if scenario == 9:
            assert all(e["observed_state"] == "returned"
                       and e["message_type"].startswith("pacs.004") for e in evidence)


def test_domain_evidence_can_be_verified(domain_bundles):
    proc = subprocess.run(
        [sys.executable, "scripts/verify.py", "--domains", "--json",
         "--evidence", str(domain_bundles)],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = json.JSONDecoder().raw_decode(proc.stdout)[0]
    assert not [c for c in report["checks"] if c["status"] == "FAIL"]
    assert all(c["total"] > 0 for c in report["checks"] if c["status"] == "PASS")
