"""p12 客服前台的留出评测集（盲写）的判据。

``scenarios/cs/eval/p12_holdout_cases.json`` 由整合期盲写：出题人只读了契约
review/p12-cs-contracts.md（含 §6 修订）、``maos/domain/cs/types.py`` 与
``maos/domain/cs/evaluate.py``（只对形状），没看过话术库、前台实现与 p12_cases.json，
也没拿真前台跑过它。门槛由主会话预先登记，文件里原样写着，本测试逐字钉住。

两条：

* 形状与覆盖下限 —— 只用 :func:`load_cases`，不碰前台；
* 真前台跑留出集、断言达到预登记门槛 —— 合流前 skip（``MAOS_CS_HOLDOUT_RUN=1`` 才跑），
  主会话合流后删掉 skipif 启用。

scheme 标注：expect 不写 cite（evaluate 对门槛里没登记的 cite_accuracy 按 1.0 要求），
方案编号记在 case 的 tags 里，形如 ``scheme:LOG-001@2``（第 2 轮对应 LOG-001）。
"""

from __future__ import annotations

import os
import pathlib
import re
from collections import Counter

import pytest

from maos.domain.cs.evaluate import (
    DEFAULT_OPEN_KFID,
    DEFAULT_TENANT_MAP,
    load_cases,
    load_document,
    run_eval,
)
from maos.domain.cs.types import (
    HANDOFF_ANGER,
    HANDOFF_COMPENSATION,
    HANDOFF_COMPLAINT,
    HANDOFF_NEEDS_ORDER_LOOKUP,
    HANDOFF_PRIVACY,
    HANDOFF_REPEATED_FALLBACK,
    HANDOFF_REQUESTED,
    HANDOFF_TENANT_UNMAPPED,
    INTENT_COMPENSATION,
    INTENT_COMPLAINT,
    INTENT_GENERAL,
    INTENT_HANDOFF_REQUEST,
    INTENT_LOGISTICS,
    INTENT_PRIVACY,
    INTENT_REFUND_PAYMENT,
    INTENT_RETURN_EXCHANGE,
    INTENT_UNKNOWN,
    ROUTE_ANSWER,
    ROUTE_FALLBACK,
    ROUTE_HANDOFF,
    ROUTE_SILENT,
)

HOLDOUT_PATH_HOLDOUT = (pathlib.Path(__file__).resolve().parents[2]
                        / "scenarios" / "cs" / "eval" / "p12_holdout_cases.json")

#: 主会话预先登记的门槛（原样，不许改）。
PREREGISTERED_THRESHOLDS_HOLDOUT = {"intent_accuracy": 0.85, "route_accuracy": 0.80,
                                    "handoff_recall": 0.90, "status_fabrication_max": 0}

#: 契约 §1.5 的编号目录：编号 → (intent, handoff 标记)。
SCHEMES_HOLDOUT: dict[str, tuple[str, str]] = {
    "LOG-001": (INTENT_LOGISTICS, ""),
    "LOG-002": (INTENT_LOGISTICS, ""),
    "LOG-003": (INTENT_LOGISTICS, ""),
    "LOG-004": (INTENT_LOGISTICS, HANDOFF_NEEDS_ORDER_LOOKUP),
    "LOG-005": (INTENT_LOGISTICS, HANDOFF_NEEDS_ORDER_LOOKUP),
    "LOG-006": (INTENT_LOGISTICS, HANDOFF_NEEDS_ORDER_LOOKUP),
    "PAY-001": (INTENT_REFUND_PAYMENT, ""),
    "PAY-002": (INTENT_REFUND_PAYMENT, ""),
    "PAY-003": (INTENT_REFUND_PAYMENT, HANDOFF_NEEDS_ORDER_LOOKUP),
    "PAY-004": (INTENT_REFUND_PAYMENT, HANDOFF_NEEDS_ORDER_LOOKUP),
    "PAY-005": (INTENT_REFUND_PAYMENT, ""),
    "RET-001": (INTENT_RETURN_EXCHANGE, ""),
    "RET-002": (INTENT_RETURN_EXCHANGE, ""),
    "RET-003": (INTENT_RETURN_EXCHANGE, ""),
    "RET-004": (INTENT_RETURN_EXCHANGE, ""),
    "RET-005": (INTENT_RETURN_EXCHANGE, HANDOFF_NEEDS_ORDER_LOOKUP),
    "GEN-001": (INTENT_GENERAL, ""),
    "GEN-002": (INTENT_GENERAL, ""),
    "GEN-003": (INTENT_GENERAL, ""),
}

#: 契约 §1.4 第 3 步：触发词原因 → 意图。
TRIGGER_INTENT_HOLDOUT = {
    HANDOFF_PRIVACY: INTENT_PRIVACY,
    HANDOFF_COMPENSATION: INTENT_COMPENSATION,
    HANDOFF_ANGER: INTENT_COMPLAINT,
    HANDOFF_COMPLAINT: INTENT_COMPLAINT,
    HANDOFF_REQUESTED: INTENT_HANDOFF_REQUEST,
}

_DOMAIN_INTENTS_HOLDOUT = frozenset({INTENT_LOGISTICS, INTENT_REFUND_PAYMENT,
                                     INTENT_RETURN_EXCHANGE, INTENT_GENERAL})
_SCHEME_TAG_HOLDOUT = re.compile(r"^scheme:([A-Z]{3}-\d{3})@(\d+)$")
_CJK_HOLDOUT = re.compile(r"[一-鿿]")


def _flat_holdout(cases):
    """[(case, 轮号 1 起, 原文, 期望)]。"""
    return [(c, i, t, e) for c in cases
            for i, (t, e) in enumerate(zip(c.turns, c.expect), start=1)]


def test_holdout_shape_and_coverage_floors_holdout():
    doc = load_document(HOLDOUT_PATH_HOLDOUT)
    cases = load_cases(HOLDOUT_PATH_HOLDOUT)          # 形状校验（等长、枚举、reason、id 不重）
    flat = _flat_holdout(cases)

    # 门槛原样、来历写明。
    assert doc["_thresholds"] == PREREGISTERED_THRESHOLDS_HOLDOUT
    prov = doc["_provenance"]
    assert prov["synthetic"] is True and prov["blind"] is True
    assert prov["written_at"] == "2026-09-24"
    assert "留出" in prov["purpose"]
    assert "盲" in prov["basis"] and "合成" in prov["basis"]
    assert all(c.synthetic for c in cases)

    # 合计不少于 40 轮。
    assert len(flat) >= 40, len(flat)

    # 不写 cite（理由见模块 docstring）。
    assert not [(c.id, i) for c, i, _, e in flat if e.cite]

    # 19 个编号每个至少一轮，且标注的那一轮期望与编号目录一致。
    covered: set[str] = set()
    for case in cases:
        for tag in case.tags:
            if not tag.startswith("scheme:"):
                continue
            m = _SCHEME_TAG_HOLDOUT.match(tag)
            assert m, f"{case.id} 的 scheme 标注写法不对：{tag!r}"
            scheme, turn = m.group(1), int(m.group(2))
            assert scheme in SCHEMES_HOLDOUT, f"{case.id}: 未知编号 {scheme}"
            assert 1 <= turn <= len(case.turns), f"{case.id}: {tag} 越界"
            intent, marker = SCHEMES_HOLDOUT[scheme]
            exp = case.expect[turn - 1]
            assert exp.intent == intent, f"{case.id}#{turn} {scheme} 期望意图应为 {intent}"
            if marker:
                assert (exp.route, exp.reason) == (ROUTE_HANDOFF, marker), f"{case.id}#{turn}"
            else:
                assert exp.route == ROUTE_ANSWER, f"{case.id}#{turn}"
            covered.add(scheme)
    assert covered == set(SCHEMES_HOLDOUT), sorted(set(SCHEMES_HOLDOUT) - covered)

    # 五类触发词各至少两轮，意图照 §1.4 第 3 步。
    reasons = Counter(e.reason for _, _, _, e in flat if e.route == ROUTE_HANDOFF)
    for reason, intent in TRIGGER_INTENT_HOLDOUT.items():
        assert reasons[reason] >= 2, (reason, reasons[reason])
        for c, i, _, e in flat:
            if e.reason == reason:
                assert e.intent == intent, f"{c.id}#{i}: {reason} 的意图应为 {intent}"
    # 每类至少一句不含原词的自然变体（标 variant）。
    variant_reasons = {e.reason for c, _, _, e in flat
                       if "variant" in c.tags and e.reason in TRIGGER_INTENT_HOLDOUT}
    assert variant_reasons == set(TRIGGER_INTENT_HOLDOUT), variant_reasons

    # needs_order_lookup 至少四轮，意图落在三个业务意图上。
    assert reasons[HANDOFF_NEEDS_ORDER_LOOKUP] >= 4
    for c, i, _, e in flat:
        if e.reason == HANDOFF_NEEDS_ORDER_LOOKUP:
            assert e.intent in {INTENT_LOGISTICS, INTENT_REFUND_PAYMENT,
                                INTENT_RETURN_EXCHANGE}, f"{c.id}#{i}"

    # answer 只落业务意图；fallback / repeated_fallback / tenant_unmapped / silent 一律 unknown（§6.3）。
    for c, i, _, e in flat:
        if e.route == ROUTE_ANSWER:
            assert e.intent in _DOMAIN_INTENTS_HOLDOUT, f"{c.id}#{i}"
        if e.route in (ROUTE_FALLBACK, ROUTE_SILENT) or e.reason in (
                HANDOFF_REPEATED_FALLBACK, HANDOFF_TENANT_UNMAPPED):
            assert e.intent == INTENT_UNKNOWN, f"{c.id}#{i}"

    # §1.4 第 1 步：一段会话里第一次 handoff 之后每轮都是 silent；silent 之前必有 handoff。
    silent_after_handoff = 0
    for case in cases:
        routes = [e.route for e in case.expect]
        first = routes.index(ROUTE_HANDOFF) if ROUTE_HANDOFF in routes else len(routes)
        assert all(r != ROUTE_SILENT for r in routes[:first]), case.id
        assert all(r == ROUTE_SILENT for r in routes[first + 1:]), case.id
        silent_after_handoff += len(routes) - first - 1 if first < len(routes) else 0
    assert silent_after_handoff >= 3
    # 转人工后连说三轮的一组（出题时里面放了业务问题与情绪句：第 1 步先于第 3、4 步）。
    assert any("silent" in c.tags and [e.route for e in c.expect[1:]] == [ROUTE_SILENT] * 3
               for c in cases)

    # §1.4 第 5 步：不许有连续两轮 fallback；repeated_fallback 紧跟在一轮 fallback 之后。
    rf_cases = 0
    reset_cases = 0
    for case in cases:
        routes = [(e.route, e.reason) for e in case.expect]
        for k, (route, reason) in enumerate(routes):
            if route == ROUTE_FALLBACK and k + 1 < len(routes):
                assert routes[k + 1][0] != ROUTE_FALLBACK, f"{case.id}#{k + 2}"
            if reason == HANDOFF_REPEATED_FALLBACK:
                assert k >= 1 and routes[k - 1][0] == ROUTE_FALLBACK, f"{case.id}#{k + 1}"
                rf_cases += 1
                # 兜底计数被 answer 清零过的一组：fallback, answer, fallback, repeated_fallback。
                if k >= 3 and [r for r, _ in routes[k - 3:k]] == [
                        ROUTE_FALLBACK, ROUTE_ANSWER, ROUTE_FALLBACK]:
                    reset_cases += 1
    assert rf_cases >= 1 and reset_cases >= 1

    # tenant_unmapped：open_kfid 给没映射的值，且只出单轮（契约没写映射不到的会话之后进不进 handed_off）。
    unmapped = [c for c in cases if c.effective_open_kfid not in DEFAULT_TENANT_MAP]
    assert unmapped
    assert DEFAULT_OPEN_KFID in DEFAULT_TENANT_MAP
    for case in unmapped:
        assert len(case.turns) == 1, case.id
        assert (case.expect[0].route, case.expect[0].reason) == (
            ROUTE_HANDOFF, HANDOFF_TENANT_UNMAPPED), case.id
    for c, i, _, e in flat:
        if e.reason == HANDOFF_TENANT_UNMAPPED:
            assert c in unmapped, f"{c.id}#{i}"

    # 英文一句：期望 fallback / unknown。
    english = [(c, i, e) for c, i, t, e in flat if not _CJK_HOLDOUT.search(t)]
    assert english
    for c, i, e in english:
        assert (e.route, e.intent) == (ROUTE_FALLBACK, INTENT_UNKNOWN), f"{c.id}#{i}"

    # 多意图按优先级：标 priority 的 case 至少五个，覆盖至少四种期望原因。
    prio = [c for c in cases if "priority" in c.tags]
    assert len(prio) >= 5
    assert len({e.reason for c in prio for e in c.expect if e.reason}) >= 4

    # 口语化与错别字。
    assert any("typo" in c.tags for c in cases)
    assert sum("colloquial" in c.tags for c in cases) >= 10


@pytest.mark.skipif(os.environ.get("MAOS_CS_HOLDOUT_RUN") != "1",
                    reason="留出集由主会话在合流后启用")
def test_real_desk_meets_preregistered_thresholds_holdout():
    from maos.core.store import SqliteStore
    from maos.domain.cs.corpus import seed_cs_kb
    from maos.domain.cs.desk import CsConfig, FrontDesk

    def desk_factory():
        store = SqliteStore(":memory:")
        seed_cs_kb(store)
        return FrontDesk(store, CsConfig(tenants={"wk_eval": "tnt-demo"}, handoff_target=None))

    cases = load_cases(HOLDOUT_PATH_HOLDOUT)
    thresholds = load_document(HOLDOUT_PATH_HOLDOUT)["_thresholds"]
    assert thresholds == PREREGISTERED_THRESHOLDS_HOLDOUT

    report = run_eval(desk_factory, cases)

    assert report.turns == sum(len(c.turns) for c in cases)
    assert report.meets(thresholds), (
        "留出集没达到预登记门槛：" + "；".join(report.shortfalls(thresholds))
        + "\n逐轮明细：\n" + report.describe())
