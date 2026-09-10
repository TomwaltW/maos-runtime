"""`scripts/verify.py` 第 10 项 case-outcome 与第 6 项的四判据升级（T120）。

核验器的判据必须**独立于生成侧**：本文件不 import `domain.refund.outcome`，
四判据的取值域与公式在这里各写一份 —— 从被审那份代码 import 过来，等于让它自己
给自己定标准，枚举被改宽的那天核验器会跟着一起变宽而一声不响。

八条负例各自「失败意味着什么」写在每条 docstring 里。它们守的是同一件事：
**报告里那四个值不能是自己填的**，得对得上库里的观察行。
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sqlite3
import sys

import pytest

from maos.core.store import SqliteStore
from maos.domain.refund import objects

ROOT = pathlib.Path(__file__).resolve().parents[2]
TENANT = "tnt-t120-v"
CASE = "case-t120-v1"
PLAN = "plan-t120-v1"
AT = "2026-09-10T00:00:00+00:00"


def _load_verify():
    """按路径加载 `scripts/verify.py`（它不是包，同 test_verify_warn 的做法）。

    **先进 `sys.modules` 再 exec**：`verify.Case` 是 dataclass，而 dataclass 装饰器
    要按 `__module__` 回查自己所在模块的命名空间。没登记的话它拿到 None，
    报一句与本文件毫无关系的 `'NoneType' object has no attribute '__dict__'`。
    """
    key = "_t120_verify"
    spec = importlib.util.spec_from_file_location(key, ROOT / "scripts" / "verify.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


verify = _load_verify()


def _build_db(path: pathlib.Path, *, observations=(), biz_status="processing") -> None:
    store = SqliteStore(str(path))
    store.init_schema()
    objects.ensure_schema(store)
    # 第 6 项按库里的 `plan` 行找 Plan 终态，没有这一行整项空转判 SKIP。
    store.insert_plan({"plan_id": PLAN, "goal": "退款", "trace_id": "tr-t120",
                       "state": "DONE"})
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "INSERT INTO refund_case (tenant_id, case_id, channel_id, order_id,"
            " order_version, sku, reason_code, amount_claimed, biz_status, plan_id,"
            " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (TENANT, CASE, "ch-1", "ord-1", 1, "sku-1", "quality", 10.0,
             biz_status, PLAN, AT))
        for i, (state, request_id) in enumerate(observations):
            conn.execute(
                "INSERT INTO payment_observation (tenant_id, case_id, request_id,"
                " gateway_code, raw_receipt_json, observed_state, observed_at,"
                " actor_invocation_id) VALUES (?,?,?,?,?,?,?,?)",
                (TENANT, CASE, request_id, "10000", "{}", state,
                 f"2026-09-10T00:00:{i:02d}+00:00", "inv-1"))
        conn.commit()
    finally:
        conn.close()


def _outcome_row(**over) -> dict:
    base = {"tenant_id": TENANT, "case_id": CASE, "arrival": "settled",
            "arrival_basis": "payment_observation:req-1@2026-09-10T00:00:00+00:00",
            "customer_confirmation": "none", "manual_correction": "none",
            "complaint": "none", "evidence_complete": True, "business_success": True}
    return {**base, **over}


def _result(*, state="DONE", status="succeeded", basis="external_evidence",
            outcomes=None, evidence=None) -> dict:
    return {"plans": [{
        "plan_id": PLAN, "state": state,
        "business_outcome": {
            "status": status, "basis": basis, "plan_state": state,
            "external_evidence": evidence if evidence is not None else [
                {"kind": "payment_observation", "provenance": "payment_observation"}],
            "unaudited_evidence_count": 0,
            "case_outcomes": outcomes if outcomes is not None else [_outcome_row()],
            "source": "derived-from-db-at-export-time",
        },
    }]}


@pytest.fixture
def make_case(tmp_path):
    opened = []

    def _make(*, observations=(), result=None, name="scenario-t120",
              biz_status="processing"):
        db = tmp_path / f"{name}.db"
        _build_db(db, observations=observations, biz_status=biz_status)
        conn = verify.connect_ro(str(db))
        opened.append(conn)
        return verify.Case(name=name, directory=str(tmp_path), db_path=str(db), conn=conn,
                           tables=verify.table_names(conn), trace={},
                           result=result if result is not None else _result())

    yield _make
    for c in opened:
        c.close()


def _bad(chk) -> list[str]:
    """判负的那些 note。`Check.bad()` 不加前缀，warn / info 才加 —— 按前缀反选。"""
    return [n for n in chk.notes if not n.startswith(("warn:", "info:"))]


# ======================================================================
# 第 10 项：四判据齐全、算得对、回查得到
# ======================================================================
def test_settled_with_a_real_observation_passes(make_case):
    case = make_case(observations=[("settled", "req-1")])
    chk = verify.check_case_outcome([case])
    assert chk.status == verify.PASS, chk.notes
    assert chk.total == 1


def test_missing_case_outcome_is_a_fail(make_case):
    """**失败意味着**：库里有这个 case，四判据一条都没算过，而报告看上去是完整的。"""
    case = make_case(observations=[("settled", "req-1")], result=_result(outcomes=[]))
    chk = verify.check_case_outcome([case])
    assert chk.status == verify.FAIL
    assert any(CASE in n for n in _bad(chk))


@pytest.mark.parametrize("field, value", [
    ("arrival", "arrived"),
    ("customer_confirmation", "ok"),
    ("manual_correction", "manual"),
    ("complaint", "opened"),
])
def test_out_of_domain_enum_is_a_fail(make_case, field, value):
    """**失败意味着**：自造措辞的判据没法与别的轨对账（T117 只读不写这几个值）。"""
    case = make_case(observations=[("settled", "req-1")],
                     result=_result(outcomes=[_outcome_row(**{field: value})]))
    chk = verify.check_case_outcome([case])
    assert chk.status == verify.FAIL
    assert any(field in n for n in _bad(chk))


def test_business_success_must_follow_the_formula(make_case):
    """**失败意味着**：结论不是那三个值推出来的，是另填进去的 —— 最省事的一种造假。"""
    case = make_case(observations=[("settled", "req-1")],
                     result=_result(outcomes=[_outcome_row(complaint="open")]))
    chk = verify.check_case_outcome([case])
    assert chk.status == verify.FAIL
    assert any("business_success" in n for n in _bad(chk))


def test_arrival_must_match_the_observations_in_the_db(make_case):
    """# 论证：到账只由 payment_observation 的行决定（铁律 8）。

    **失败意味着**：报告自称到账，而库里数不出 settled 观察 ——
    与「绕过 guard 直接写 settled」是同一类事。
    """
    case = make_case(observations=[])          # 一条观察都没有
    chk = verify.check_case_outcome([case])
    assert chk.status == verify.FAIL
    assert any("arrival" in n and "铁律 8" in n for n in _bad(chk))


def test_failed_observation_reported_as_settled_is_a_fail(make_case):
    case = make_case(observations=[("failed", "req-1")])
    chk = verify.check_case_outcome([case])
    assert chk.status == verify.FAIL


def test_basis_pointing_nowhere_is_a_fail(make_case):
    """**失败意味着**：指不回观察行的到账不叫判据，叫说法。"""
    case = make_case(
        observations=[("settled", "req-1")],
        result=_result(outcomes=[_outcome_row(
            arrival_basis="payment_observation:req-9@2026-09-10T00:00:00+00:00")]))
    chk = verify.check_case_outcome([case])
    assert chk.status == verify.FAIL
    assert any("回查不到" in n for n in _bad(chk))


def test_malformed_basis_is_a_fail(make_case):
    case = make_case(observations=[("settled", "req-1")],
                     result=_result(outcomes=[_outcome_row(arrival_basis="已到账")]))
    chk = verify.check_case_outcome([case])
    assert chk.status == verify.FAIL


def test_basis_pointing_at_a_non_settled_row_is_a_fail(make_case):
    """**失败意味着**：拿一条别的观察（processing）去给「到账」背书。"""
    case = make_case(
        observations=[("processing", "req-1"), ("settled", "req-2")],
        result=_result(outcomes=[_outcome_row(
            arrival_basis="payment_observation:req-1@2026-09-10T00:00:00+00:00")]))
    chk = verify.check_case_outcome([case])
    assert chk.status == verify.FAIL
    assert any("observed_state" in n for n in _bad(chk))


def test_unknown_arrival_without_business_success_passes(make_case):
    """全 DONE 但问不出下落：如实记 unknown + false 是**正确**的，不许判负。"""
    case = make_case(observations=[], result=_result(
        status="undetermined", basis="business_outcome_not_successful",
        outcomes=[_outcome_row(arrival="unknown", arrival_basis="",
                               business_success=False)]))
    chk = verify.check_case_outcome([case])
    assert chk.status == verify.PASS, chk.notes


def test_skips_when_there_is_no_refund_case(tmp_path):
    """没有退款证据时判 SKIP 而不是 0/0 PASS —— 空转也算没跑。"""
    db = tmp_path / "bare.db"
    store = SqliteStore(str(db))
    store.init_schema()
    conn = verify.connect_ro(str(db))
    try:
        case = verify.Case(name="scenario-1", directory=str(tmp_path), db_path=str(db),
                           conn=conn, tables=verify.table_names(conn), trace={}, result={})
        chk = verify.check_case_outcome([case])
        assert chk.status == verify.SKIP and chk.total == 0
        assert "scenario-R5" not in chk.skip_reason, "补跑提示不许指向 RAG 那一束"
    finally:
        conn.close()


# ======================================================================
# 第 6 项：DONE 但 arrival != settled ⇒ 必须 business_success=false
# ======================================================================
def test_done_with_unsettled_arrival_claiming_success_is_a_fail(make_case):
    """# 论证：没问出到账就没有「业务成功」这一说。

    **失败意味着**：报告自己打自己的脸 —— arrival 不是 settled 却记 business_success=true。
    这一格从前没有任何判据管得着：Plan DONE + 有一条观察 = succeeded，
    而那条推理里没有一步问过钱到没到账。
    """
    case = make_case(observations=[("failed", "req-1")], result=_result(
        outcomes=[_outcome_row(arrival="unsettled", business_success=True)]))
    chk = verify.check_business_outcome([case])
    assert chk.status == verify.FAIL
    assert any("铁律 8" in n for n in _bad(chk))


def test_done_but_business_failed_must_be_undetermined(make_case):
    """四判据说没成，DONE 的 status 只能是 undetermined。

    这一格是**钱到了、投诉还开着**：外部判据（settled 观察）齐全、回查得到，
    案子也确实收在 settled 上 —— 以前这就是一句 succeeded，而客户正在投诉。
    """
    outcomes = [_outcome_row(complaint="open", business_success=False)]
    # 判据要逐字段与库里那一行对得上（第 6 项的回查），所以码和 actor 都得给。
    evidence = [{"kind": "payment_observation", "tenant_id": TENANT, "case_id": CASE,
                 "request_id": "req-1", "observed_state": "settled",
                 "gateway_code": "10000", "actor_invocation_id": "inv-1",
                 "provenance": "payment_observation"}]
    ok = make_case(observations=[("settled", "req-1")], biz_status="settled",
                   result=_result(status="undetermined",
                                  basis="business_outcome_not_successful",
                                  outcomes=outcomes, evidence=evidence))
    chk_ok = verify.check_business_outcome([ok])
    assert chk_ok.status == verify.PASS, chk_ok.notes

    lying = make_case(name="scenario-t120b", observations=[("settled", "req-1")],
                      biz_status="settled",
                      result=_result(status="succeeded", basis="external_evidence",
                                     outcomes=outcomes, evidence=evidence))
    chk = verify.check_business_outcome([lying])
    assert chk.status == verify.FAIL
    assert any("四判据说这单业务没成" in n for n in _bad(chk))


def test_successful_case_still_needs_the_old_teeth(make_case):
    """升级不许把老判据冲掉：DONE 而一条外部判据都没有，照旧判负。"""
    case = make_case(observations=[("settled", "req-1")], result=_result(evidence=[]))
    chk = verify.check_business_outcome([case])
    assert chk.status == verify.FAIL
    assert any("不等于业务成功" in n for n in _bad(chk))


def test_plans_without_case_outcomes_are_unaffected(make_case):
    """软件域那几个场景没有退款 case —— 第 6 项对它们的行为一个字节不变。"""
    result = _result(outcomes=[])
    result["plans"][0]["business_outcome"]["external_evidence"] = [
        {"kind": "test_report", "artifact_id": "a1", "provenance": "unknown"}]
    result["plans"][0]["business_outcome"]["unaudited_evidence_count"] = 1
    case = make_case(observations=[("settled", "req-1")], result=result)
    chk = verify.check_business_outcome([case])
    # 判负的原因只能是「判据回查不到」，不能是四判据缺失 —— 那一层对它们不适用。
    assert not any("四判据" in n for n in _bad(chk))
