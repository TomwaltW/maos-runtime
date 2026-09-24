"""p12 客服前台的留出评测集（盲写）的判据。

``scenarios/cs/eval/p12_holdout_cases.json`` 由整合期盲写：出题人只读了契约
review/p12-cs-contracts.md（含 §6 修订）、``maos/domain/cs/types.py`` 与
``maos/domain/cs/evaluate.py``（只对形状），没看过话术库、前台实现与 p12_cases.json，
也没拿真前台跑过它。门槛由主会话预先登记，文件里原样写着，本测试逐字钉住。

三条：

* 形状与覆盖下限 —— 只用 :func:`load_cases`，不碰前台；触发词、多意图两项按契约 §1.4
  「触发词最低覆盖」表的**原文**判，不信出题人自己打的标签；
* 不重合棘轮 —— 留出句与开发集（p12_cases.json）各轮、话术库 examples / synonyms
  去标点后不许共享 ≥6 字的连续片段，也不许整句一字之差。失败消息**只报留出 case id
  与来源**，不回显开发集或话术库的句子：改写者看不到对面，改写才仍然是盲的；
* 真前台跑留出集 —— 主会话合流后启用（cf0e97e 首跑）。首跑没达到预登记门槛，门槛原样保留
  作 p13 验收目标；这里钉的是安全不变量（零编造、零自信答错）与「不许比首跑更差」的地板，
  见 :data:`MEASURED_P12_HOLDOUT` 的注释。

标签约定：expect 不写 cite（evaluate 对门槛里没登记的 cite_accuracy 按 1.0 要求），
方案编号记在 case 的 tags 里，形如 ``scheme:LOG-001@2``（第 2 轮对应 LOG-001）；
被更高优先级的触发词压住的业务问题记成 ``also:LOG-004@1``（第 1 轮里还带着 LOG-004 的问题）。
"""

from __future__ import annotations

import json
import pathlib
import re
import unicodedata
from collections import Counter

import pytest

from maos.domain.cs.corpus import load_corpus
from maos.domain.cs.evaluate import (
    DEFAULT_OPEN_KFID,
    DEFAULT_TENANT_MAP,
    EVAL_PATH,
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

#: 契约 §1.4 第 3 步的优先级，从高到低。
TRIGGER_PRIORITY_HOLDOUT = (HANDOFF_PRIVACY, HANDOFF_COMPENSATION, HANDOFF_ANGER,
                            HANDOFF_COMPLAINT, HANDOFF_REQUESTED)

#: 契约 §1.4「触发词最低覆盖」表，逐字抄；anger 的「连续三个及以上感叹号」见 _BANGS_HOLDOUT。
TRIGGER_TABLE_HOLDOUT: dict[str, tuple[str, ...]] = {
    HANDOFF_REQUESTED: ("转人工", "人工客服", "找人工", "真人"),
    HANDOFF_COMPLAINT: ("投诉", "12315", "消协", "曝光", "起诉", "律师"),
    HANDOFF_ANGER: ("垃圾", "骗子", "气死", "滚"),
    HANDOFF_COMPENSATION: ("赔偿", "补偿", "赔钱", "赔我"),
    HANDOFF_PRIVACY: ("手机号", "身份证", "住址", "个人信息", "隐私"),
}
_BANGS_HOLDOUT = re.compile(r"[!！]{3,}")

#: 不重合棘轮：去标点后与参照句共享的连续片段不许达到这个长度。
MIN_SHARED_RUN_HOLDOUT = 6

_DOMAIN_INTENTS_HOLDOUT = frozenset({INTENT_LOGISTICS, INTENT_REFUND_PAYMENT,
                                     INTENT_RETURN_EXCHANGE, INTENT_GENERAL})
_SCHEME_TAG_HOLDOUT = re.compile(r"^scheme:([A-Z]{3}-\d{3})@(\d+)$")
_ALSO_TAG_HOLDOUT = re.compile(r"^also:([A-Z]{3}-\d{3})@(\d+)$")
_CJK_HOLDOUT = re.compile(r"[一-鿿]")


def _flat_holdout(cases):
    """[(case, 轮号 1 起, 原文, 期望)]。"""
    return [(c, i, t, e) for c in cases
            for i, (t, e) in enumerate(zip(c.turns, c.expect), start=1)]


def _table_hits_holdout(text: str) -> set[str]:
    """按 §1.4 表的原文，这句话命中了哪几类触发词。"""
    hits = {r for r, words in TRIGGER_TABLE_HOLDOUT.items() if any(w in text for w in words)}
    if _BANGS_HOLDOUT.search(text):
        hits.add(HANDOFF_ANGER)
    return hits


def _tagged_turns_holdout(case, pattern: re.Pattern[str]) -> list[tuple[str, int]]:
    """case 里按 ``pattern`` 写的 (编号, 轮号) 标注；写法不对直接判红。"""
    out = []
    prefix = "scheme:" if pattern is _SCHEME_TAG_HOLDOUT else "also:"
    for tag in case.tags:
        if not tag.startswith(prefix):
            continue
        m = pattern.match(tag)
        assert m, f"{case.id} 的标注写法不对：{tag!r}"
        scheme, turn = m.group(1), int(m.group(2))
        assert scheme in SCHEMES_HOLDOUT, f"{case.id}: 未知编号 {scheme}"
        assert 1 <= turn <= len(case.turns), f"{case.id}: {tag} 越界"
        out.append((scheme, turn))
    return out


def _priority_kind_holdout(case, i: int, text: str, exp) -> str:
    """这一轮是不是「多意图按优先级」的一轮，是哪一种；不是返回空串。全部按 §1.4 表的原文判。"""
    hits = _table_hits_holdout(text)
    if not hits:
        return ""
    if exp.route == ROUTE_SILENT:
        return "silent_over_trigger"            # 第 1 步压第 3 步
    if exp.reason == HANDOFF_TENANT_UNMAPPED:
        return "unmapped_over_trigger"          # 第 2 步压第 3 步
    if len(hits) >= 2:
        return "multi_trigger"                  # 第 3 步内部的优先级
    if any(turn == i for _, turn in _tagged_turns_holdout(case, _ALSO_TAG_HOLDOUT)):
        return "trigger_over_business"          # 第 3 步压第 4 步
    return ""


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
        for scheme, turn in _tagged_turns_holdout(case, _SCHEME_TAG_HOLDOUT):
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

    # 按表的原文判：句子里有表中原词、又没被第 1、2 步截走的轮，必须在第 3 步转人工，
    # 且期望原因的优先级不低于句中命中的最高一类（更高一类可以是不含原词的自然变体）。
    for c, i, t, e in flat:
        hits = _table_hits_holdout(t)
        if not hits or e.route == ROUTE_SILENT or e.reason == HANDOFF_TENANT_UNMAPPED:
            continue
        top = min(hits, key=TRIGGER_PRIORITY_HOLDOUT.index)
        assert e.route == ROUTE_HANDOFF and e.reason in TRIGGER_TABLE_HOLDOUT, (
            f"{c.id}#{i}: 句中有 {sorted(hits)} 的表中原词，应在第 3 步转人工")
        assert TRIGGER_PRIORITY_HOLDOUT.index(e.reason) <= TRIGGER_PRIORITY_HOLDOUT.index(top), (
            f"{c.id}#{i}: 期望 {e.reason}，低于句中命中的 {top}")

    # 每类至少一句自然变体：期望该原因、句子里却不含该原因任何表中原词。
    for reason in TRIGGER_TABLE_HOLDOUT:
        variants = [(c.id, i) for c, i, t, e in flat
                    if e.route == ROUTE_HANDOFF and e.reason == reason
                    and reason not in _table_hits_holdout(t)]
        assert variants, f"{reason} 没有不含表中原词的自然变体"

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
                # 兜底计数被 answer 清零过的一组：… fallback, answer, fallback, repeated_fallback。
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

    # 多意图按优先级（按表的原文判，不信 priority 标签本身）：
    # also: 标注的那一轮必须真带触发词、期望在第 3 步转人工；
    for case in cases:
        for scheme, turn in _tagged_turns_holdout(case, _ALSO_TAG_HOLDOUT):
            exp = case.expect[turn - 1]
            assert _table_hits_holdout(case.turns[turn - 1]), f"{case.id}#{turn} 不含表中触发词"
            assert exp.route == ROUTE_HANDOFF and exp.reason in TRIGGER_TABLE_HOLDOUT, (
                f"{case.id}#{turn}: 触发词应压住 {scheme}")
    # 标 priority 的 case 每个至少有一轮真是多意图；四种压法各有、第 3 步内部的够多够杂。
    kinds: dict[str, list[tuple[str, int, str]]] = {}
    for c, i, t, e in flat:
        kind = _priority_kind_holdout(c, i, t, e)
        if kind:
            kinds.setdefault(kind, []).append((c.id, i, e.reason))
    for case in cases:
        if "priority" in case.tags:
            assert any(_priority_kind_holdout(case, i, t, e)
                       for i, (t, e) in enumerate(zip(case.turns, case.expect), start=1)), case.id
    assert set(kinds) == {"silent_over_trigger", "unmapped_over_trigger",
                          "multi_trigger", "trigger_over_business"}, sorted(kinds)
    assert len(kinds["multi_trigger"]) >= 4
    assert len({r for _, _, r in kinds["multi_trigger"]}) >= 3
    assert len(kinds["trigger_over_business"]) >= 2

    # 口语化与错别字（标签即可）。
    assert any("typo" in c.tags for c in cases)
    assert sum("colloquial" in c.tags for c in cases) >= 10


# ---------------------------------------------------------------------------
# 不重合棘轮
# ---------------------------------------------------------------------------
def _norm_holdout(text: str) -> str:
    """去标点与空白（只留字母、数字、汉字），全角转半角，英文转小写。"""
    return "".join(ch for ch in unicodedata.normalize("NFKC", text).lower() if ch.isalnum())


def _one_edit_apart_holdout(a: str, b: str) -> bool:
    """相等，或恰好一字之差（替换 / 增 / 删一个字）。"""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) > len(b):
        a, b = b, a
    k = 0
    while k < len(a) and a[k] == b[k]:
        k += 1
    if len(a) == len(b):
        return a[k + 1:] == b[k + 1:]
    return a[k:] == b[k + 1:]


def _reference_texts_holdout() -> dict[str, list[str]]:
    """{来源: [去标点后的参照句]}：开发集各轮、话术库每篇的 examples 与 synonyms。只在内存里比。"""
    dev = [_norm_holdout(t) for c in load_cases(EVAL_PATH) for t in c.turns]
    kb: list[str] = []
    for row in load_corpus():
        body = row.get("body")
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except ValueError:
                continue
        if not isinstance(body, dict):
            continue
        for key in ("examples", "synonyms"):
            kb.extend(_norm_holdout(str(x)) for x in body.get(key) or ())
    return {"开发集": [s for s in dev if s], "话术库": [s for s in kb if s]}


def _overlap_flags_holdout(cases) -> list[str]:
    """撞了棘轮的留出轮，形如 ``CS12H-003#1(话术库)``。只报留出这边的 id 与来源，不带任何参照句。"""
    refs = _reference_texts_holdout()
    n = MIN_SHARED_RUN_HOLDOUT
    flags: list[str] = []
    for source, texts in refs.items():
        grams = {s[k:k + n] for s in texts for k in range(len(s) - n + 1)}
        for c, i, t, _ in _flat_holdout(cases):
            h = _norm_holdout(t)
            if (any(h[k:k + n] in grams for k in range(len(h) - n + 1))
                    or any(_one_edit_apart_holdout(h, s) for s in texts)):
                flags.append(f"{c.id}#{i}({source})")
    return flags


def test_holdout_does_not_overlap_dev_set_or_scripts_holdout():
    # 参照不许空转（只比条数，不让断言回显参照句）。
    refs = _reference_texts_holdout()
    n_dev, n_kb = len(refs["开发集"]), len(refs["话术库"])
    assert n_dev >= 40 and n_kb >= 19, (n_dev, n_kb)

    flags = _overlap_flags_holdout(load_cases(HOLDOUT_PATH_HOLDOUT))
    assert not flags, (
        f"这些留出轮与开发集或话术库共享 ≥{MIN_SHARED_RUN_HOLDOUT} 字连续片段、或整句一字之差，"
        "请没看过开发集与话术库的人重写（本消息故意不回显对面的句子）：" + "、".join(flags))


# ---------------------------------------------------------------------------
# 真前台（主会话合流后启用，2026-09-24 于 cf0e97e 首跑）
# ---------------------------------------------------------------------------
#: 首跑实测（cf0e97e，真前台、零模型）。预登记门槛**没达到**：intent 48/70 = 0.6857 < 0.85、
#: route 45/70 = 0.6429 < 0.80、handoff 28/38 = 0.7368 < 0.90；status_fabrication = 0 达到。
#: 主会话裁定（DECISIONS integrate-p12）：不拿留出集调 p12（调了它就不再是留出集），
#: 预登记门槛原样保留、转成 p13 理解层的验收目标；p12 这里钉两件事 ——
#: 安全不变量（零编造、零「自信答错」）与「不许比首跑更差」的地板。
MEASURED_P12_HOLDOUT = {"turns": 70, "intent_hits": 48, "route_hits": 45,
                        "handoff_expected": 38, "handoff_caught": 28}


def _aggregate_only_holdout(r) -> str:
    """失败消息只报聚合数与 case id —— p13 起实现轨会跑全量，留出句不许从断言消息里漏给他们。"""
    ids = sorted({f"{m.case_id}#{m.turn}" for m in r.failures})
    return (f"intent {r.intent_hits}/{r.turns} route {r.route_hits}/{r.turns} "
            f"handoff {r.handoff_caught}/{r.handoff_expected} fabrication {r.status_fabrication}; "
            f"没对上的轮（只列 id）：{', '.join(ids)}")


def _desk_factory_holdout():
    from maos.core.store import SqliteStore
    from maos.domain.cs.corpus import seed_cs_kb
    from maos.domain.cs.desk import CsConfig, FrontDesk

    store = SqliteStore(":memory:")
    seed_cs_kb(store)
    return FrontDesk(store, CsConfig(tenants={"wk_eval": "tnt-demo"}, handoff_target=None))


@pytest.fixture(scope="module")
def holdout_report():
    cases = load_cases(HOLDOUT_PATH_HOLDOUT)
    report = run_eval(_desk_factory_holdout, cases)
    assert report.turns == sum(len(c.turns) for c in cases)
    return report


def test_preregistered_thresholds_are_untouched_holdout():
    """预登记门槛一个字都不许动 —— 没达到就是没达到，改门槛等于改题。"""
    assert load_document(HOLDOUT_PATH_HOLDOUT)["_thresholds"] == PREREGISTERED_THRESHOLDS_HOLDOUT


def test_real_desk_holdout_safety_invariants_holdout(holdout_report):
    """安全不变量：宁可不答，不许错答。

    * 零编造：任何一轮的回复在空观察下都过后置校验；
    * 零「自信答错」：真前台走了 answer 的轮，意图必须对 —— 答错一篇话术比兜底更糟；
    * 该转人工的轮，没被转的只能落在兜底 / 静默（兜底两轮就转人工），不许被「答」掉。
    """
    r = holdout_report
    assert r.status_fabrication == 0, _aggregate_only_holdout(r)
    wrong_answers = [m for m in r.failures if m.actual.get("route") == ROUTE_ANSWER]
    assert not wrong_answers, "真前台自信答错：" + ", ".join(
        f"{m.case_id}#{m.turn}" for m in wrong_answers)
    swallowed = [m for m in r.failures
                 if m.expected.get("route") == ROUTE_HANDOFF
                 and m.actual.get("route") not in (ROUTE_HANDOFF, ROUTE_FALLBACK, ROUTE_SILENT)]
    assert not swallowed, [f"{m.case_id}#{m.turn}" for m in swallowed]


def test_real_desk_holdout_does_not_regress_below_first_run_holdout(holdout_report):
    """地板：不许比 cf0e97e 首跑更差。变好了照样过 —— 那时来这里把地板抬上去。"""
    r = holdout_report
    assert r.turns == MEASURED_P12_HOLDOUT["turns"]
    assert r.handoff_expected == MEASURED_P12_HOLDOUT["handoff_expected"]
    assert r.intent_hits >= MEASURED_P12_HOLDOUT["intent_hits"], _aggregate_only_holdout(r)
    assert r.route_hits >= MEASURED_P12_HOLDOUT["route_hits"], _aggregate_only_holdout(r)
    assert r.handoff_caught >= MEASURED_P12_HOLDOUT["handoff_caught"], _aggregate_only_holdout(r)
