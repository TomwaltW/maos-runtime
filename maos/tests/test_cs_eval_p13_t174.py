"""T174 · 真前台跑 p13 开发集（review/p13-cs-contracts.md §3 / §5 W-B）。

1. **夹具端口**：真前台（真存储、真理解层、真校验）+ evaluate 的 FixtureVerifier / FixtureLookup /
   FixturePrecheck 跑 ``scenarios/cs/eval/p13_cases.json``。
2. **真端口**：同一批用例里带查单、且查单结果 MockOrderSystem 造得出来的子集（ok / amended /
   not_found / system_misconfigured），换成 T172 的 ``CommerceOrderLookup`` + ``MockOrderSystem``
   （订单从夹具灌）+ T171 的 ``BindingVerifier``（绑定从夹具灌进库，source=test），结论与夹具端口一致。

## CS13-035（整合期 p13 已裁定）

原第 1 轮「嗨，客服小姐姐在吗」撞 p12 触发词地板（「客服小姐姐」→ requested，不许收窄），
整合期裁定改评测句（开发集，改句不改地板）：现为「嗨，你好呀」。于是整份开发集应当全对：

* :func:`test_p13_dev_set_has_no_misses_t174` 钉住零失误、措辞准确率 1.0；
* :func:`test_p13_dev_set_meets_file_thresholds_t174` 是整份评测集的 meets；
* :func:`test_cs13_035_old_greeting_still_hands_off_t174` 反向钉住：原句照旧转人工（地板没被收窄）。
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


def test_p13_dev_set_has_no_misses_t174():
    report = evaluate.run_eval_p13(_fixture_factory_t174, _cases_t174())
    assert report.failures == (), _describe_t174(report)
    assert report.wrong_status == 0 and report.status_fabrication == 0
    assert report.metrics()["wording_accuracy"] == 1.0
    assert report.confident_wrong == 0 and report.confident_wrong_ids() == ()  # p16 T189：篇级


def test_p13_dev_set_meets_file_thresholds_t174():
    report = evaluate.run_eval_p13(_fixture_factory_t174, _cases_t174())
    assert report.meets(_thresholds_t174()), _describe_t174(report)


def test_cs13_035_old_greeting_still_hands_off_t174():
    (case,) = [c for c in _cases_t174() if c.id == KNOWN_CONFLICT_T174]
    variant = dataclasses.replace(case, turns=("嗨，客服小姐姐在吗",) + case.turns[1:])
    report = evaluate.run_eval_p13(_fixture_factory_t174, (variant,))
    assert (KNOWN_CONFLICT_T174, 1) in {(m.case_id, m.turn) for m in report.failures}


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


# ---------------------------------------------------------------------------
# 复核 L3-1：真查单端口路径上的 event_log 哨兵（R5）
# ---------------------------------------------------------------------------
SENTINEL_USER_T174 = "wm_t174_l3_sentinel"
SENTINEL_NO_T174 = "Z7731"
SENTINEL_KEY_T174 = "QK-Z7731-SECRETQK"
OTHER_KEY_T174 = "QK-B2002-OTHERCUSTOMER"


def _real_sentinel_run_t174(*, registered: bool) -> str:
    """真 BindingVerifier + CommerceOrderLookup(MockOrderSystem) 查一单，返回该会话 event_log 的 JSON。"""
    from maos.domain.cs.types import plan_id_for
    from maos.ingress.contracts import InboundMessage

    store = SqliteStore(":memory:")
    seed_cs_kb(store)
    records.upsert_binding(store, Binding(
        tenant_id="tnt-demo", channel=CHANNEL_WECHAT_KF, external_userid=SENTINEL_USER_T174,
        display_no=SENTINEL_NO_T174, system_name="demo-orders", query_key=SENTINEL_KEY_T174,
        source=BINDING_TEST))
    mock = MockOrderSystem()
    mock.ext_order(order_id=OTHER_KEY_T174, version=1, status="shipped", amount="10.00",
                   updated_at=CLOCK_T174)
    if registered:
        mock.ext_order(order_id=SENTINEL_KEY_T174, version=1, status="shipped",
                       amount="10.00", updated_at=CLOCK_T174)
    desk = FrontDesk(store, CsConfig(tenants={"wk_eval": "tnt-demo"}, handoff_target=None),
                     clock=lambda: CLOCK_T174, verifier=BindingVerifier(),
                     lookup=CommerceOrderLookup({"demo-orders": mock}))
    res = desk.handle(InboundMessage(
        channel=CHANNEL_WECHAT_KF, chat_id=SENTINEL_USER_T174, sender=SENTINEL_USER_T174,
        text=f"{SENTINEL_NO_T174} 到哪了", msg_id="l3-1", raw={"open_kfid": "wk_eval"}))
    expected = LOOKUP_OK if registered else LOOKUP_NOT_FOUND
    assert res.lookup_outcome == expected
    import json
    return json.dumps(store.list_event_log(plan_id_for(res.conversation_id)), ensure_ascii=False)


def test_real_lookup_ok_path_keeps_order_numbers_out_of_event_log_t174():
    blob = _real_sentinel_run_t174(registered=True)
    assert "ToolInvoked" in blob                     # 真端口确实落了审计行
    for sentinel in (SENTINEL_NO_T174, SENTINEL_KEY_T174, OTHER_KEY_T174, SENTINEL_USER_T174):
        assert sentinel not in blob, sentinel


# 复核 L3-1：整合期 p13 在 cs_ports 修好（审计行只剩 OrderQueryFailed: <类名>），摘了 xfail。
def test_real_lookup_not_found_path_keeps_order_numbers_out_of_event_log_t174():
    blob = _real_sentinel_run_t174(registered=False)
    assert "ToolInvoked" in blob
    for sentinel in (SENTINEL_NO_T174, SENTINEL_KEY_T174, OTHER_KEY_T174, SENTINEL_USER_T174):
        assert sentinel not in blob, sentinel
