"""T174 · 真前台跑 p13 开发集（review/p13-cs-contracts.md §3 / §5 W-B）。

1. **夹具端口**：真前台（真存储、真理解层、真校验）+ evaluate 的 FixtureVerifier / FixtureLookup /
   FixturePrecheck 跑 ``scenarios/cs/eval/p13_cases.json``。
2. **真端口**：同一批用例里带查单、且查单结果 MockOrderSystem 造得出来的子集（ok / amended /
   not_found / system_misconfigured），换成 T172 的 ``CommerceOrderLookup`` + ``MockOrderSystem``
   （订单从夹具灌）+ T171 的 ``BindingVerifier``（绑定从夹具灌进库，source=test），结论与夹具端口一致。

## 已知的一处出入：CS13-035（写进回执 open_issues，不在本轨改）

CS13-035 第 1 轮「嗨，客服小姐姐在吗」期望 answer / GEN-001，但「客服小姐姐」是 p12 触发词地板
（``maos/domain/cs/triggers.py`` 的 ``_EXTRA_PATTERNS[requested]``，「逐字同 bf53df6，不许收窄」），
契约 §2 第 3 步触发词先于一切 —— 前台照契约转人工，后两轮（含一轮 say=cancelled）随之 silent。
评测集归 T175、触发词归 T173，都不在本轨白名单里。于是：

* :func:`test_p13_dev_set_misses_are_exactly_the_known_trigger_conflict_t174` 钉住「除 CS13-035
  三轮外全对」，且去掉 CS13-035 后达到文件里的全部 ``_thresholds``；
* :func:`test_p13_dev_set_meets_file_thresholds_t174` 是整份评测集的 meets，标 ``xfail(strict=True)``
  —— 评测集或触发词哪天对齐了它就 XPASS 变红，提醒摘掉标记；
* :func:`test_cs13_035_passes_when_the_greeting_is_not_a_trigger_t174` 证明这个 case 的其余两轮
  前台都答得对（只把第 1 轮换成一句不含触发词的问候）。
"""

from __future__ import annotations

import dataclasses

import pytest

from maos.core.store import SqliteStore
from maos.domain.cs import evaluate, records
from maos.domain.cs.corpus import seed_cs_kb
from maos.domain.cs.desk import CsConfig, FrontDesk
from maos.domain.cs.identity import BindingVerifier
from maos.domain.cs.ports import (
    BINDING_TEST, LOOKUP_AMENDED, LOOKUP_MISCONFIGURED, LOOKUP_NOT_FOUND, LOOKUP_OK, Binding,
)
from maos.domain.cs.types import CHANNEL_WECHAT_KF
from maos.ingress.cs_ports import CommerceOrderLookup
from maos.tools.order import MockOrderSystem

CLOCK_T174 = "2026-09-25T08:00:00+00:00"
KNOWN_CONFLICT_T174 = "CS13-035"


def _cases_t174():
    return evaluate.load_cases(evaluate.P13_EVAL_PATH)


def _thresholds_t174() -> dict:
    return evaluate.load_thresholds(evaluate.P13_EVAL_PATH)


def _fixture_factory_t174(ports) -> FrontDesk:
    store = SqliteStore(":memory:")
    seed_cs_kb(store)
    return FrontDesk(store, CsConfig(tenants={"wk_eval": "tnt-demo"}, handoff_target=None),
                     clock=lambda: CLOCK_T174, **ports)


def _describe_t174(report) -> str:
    return report.describe() + "\n" + "\n".join(
        f"{m.case_id}#{m.turn} {m.problems} 期望 {m.expected} 实得 {m.actual}"
        for m in report.failures)


def test_p13_dev_set_misses_are_exactly_the_known_trigger_conflict_t174():
    cases = _cases_t174()
    assert len(cases) >= 40 and sum(len(c.turns) for c in cases) >= 50
    report = evaluate.run_eval_p13(_fixture_factory_t174, cases)
    missed = sorted({(m.case_id, m.turn) for m in report.failures})
    assert missed == [(KNOWN_CONFLICT_T174, 1), (KNOWN_CONFLICT_T174, 2),
                      (KNOWN_CONFLICT_T174, 3)], _describe_t174(report)
    assert report.wrong_status == 0 and report.status_fabrication == 0
    rest = tuple(c for c in cases if c.id != KNOWN_CONFLICT_T174)
    clean = evaluate.run_eval_p13(_fixture_factory_t174, rest)
    assert clean.meets(_thresholds_t174()), _describe_t174(clean)
    assert clean.failures == ()
    assert clean.metrics()["wording_accuracy"] == 1.0


@pytest.mark.xfail(strict=True, reason="CS13-035#1「客服小姐姐」是 p12 触发词地板，与评测期望冲突"
                                       "（评测集归 T175、触发词归 T173；见模块头与回执 open_issues）")
def test_p13_dev_set_meets_file_thresholds_t174():
    report = evaluate.run_eval_p13(_fixture_factory_t174, _cases_t174())
    assert report.meets(_thresholds_t174()), _describe_t174(report)


def test_cs13_035_passes_when_the_greeting_is_not_a_trigger_t174():
    (case,) = [c for c in _cases_t174() if c.id == KNOWN_CONFLICT_T174]
    variant = dataclasses.replace(case, turns=("你好呀",) + case.turns[1:])
    report = evaluate.run_eval_p13(_fixture_factory_t174, (variant,))
    assert report.failures == (), _describe_t174(report)
    assert report.metrics()["wording_accuracy"] == 1.0


# ---------------------------------------------------------------------------
# 真端口子集
# ---------------------------------------------------------------------------
#: MockOrderSystem + CommerceOrderLookup 造得出来的查单结果。
REAL_OUTCOMES_T174 = frozenset({LOOKUP_OK, LOOKUP_AMENDED, LOOKUP_NOT_FOUND, LOOKUP_MISCONFIGURED})


def _real_subset_t174():
    out = []
    for case in _cases_t174():
        fx = case.fixtures
        if fx is None or not fx.orders or case.id == KNOWN_CONFLICT_T174:
            continue
        if all(o.outcome in REAL_OUTCOMES_T174 for _, o in fx.orders):
            out.append(case)
    return tuple(out)


def _real_factory_t174(ports) -> FrontDesk:
    """夹具端口只拿来取 case 的 fixtures 与跑批客户；真正注入的是 T171 / T172 的实现。"""
    store = SqliteStore(":memory:")
    seed_cs_kb(store)
    fx_verifier = ports["verifier"]
    fx = fx_verifier.fixtures
    for b in fx.bindings if fx_verifier.tenant_id else ():   # 客服账号没映射租户：无从绑定
        records.upsert_binding(store, Binding(
            tenant_id=fx_verifier.tenant_id, channel=CHANNEL_WECHAT_KF,
            external_userid=fx_verifier.external_userid, display_no=b.display_no,
            system_name=b.system_name, query_key=b.query_key, source=BINDING_TEST))
    mock = MockOrderSystem()
    for key, order in fx.orders:
        if order.outcome == LOOKUP_OK:
            mock.ext_order(order_id=key, version=order.version or 1, status=order.status,
                           amount="10.00", updated_at=order.updated_at or CLOCK_T174)
        elif order.outcome == LOOKUP_AMENDED:
            mock.ext_order(order_id=key, version=order.version or 1, status="amended",
                           amount="10.00", updated_at=order.updated_at or CLOCK_T174)
        # not_found：不灌；system_misconfigured：绑定里的系统名不在 systems 里（legacy-erp）
    lookup = CommerceOrderLookup({"demo-orders": mock})
    return FrontDesk(store, CsConfig(tenants={"wk_eval": "tnt-demo"}, handoff_target=None),
                     clock=lambda: CLOCK_T174, verifier=BindingVerifier(), lookup=lookup,
                     precheck=ports["precheck"])


def test_real_ports_subset_reaches_the_same_conclusions_t174():
    subset = _real_subset_t174()
    outcomes = {o.outcome for c in subset for _, o in c.fixtures.orders}
    assert outcomes == REAL_OUTCOMES_T174, "子集要把四种真端口造得出的结果都盖到"
    assert len(subset) >= 15
    real = evaluate.run_eval_p13(_real_factory_t174, subset)
    fixture = evaluate.run_eval_p13(_fixture_factory_t174, subset)
    assert real.failures == () and fixture.failures == (), _describe_t174(real)
    assert real.metrics() == fixture.metrics()
    assert real.meets(_thresholds_t174())
