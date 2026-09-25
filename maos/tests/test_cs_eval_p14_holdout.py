"""p14 盲写留出集（review/p14-cs-contracts.md §2 T181）的形状、覆盖地板、门槛与近重复棘轮。

**不跑真前台**：真前台的读数由主会话整合期接上（契约 §5）。本文件只管：

* ``evaluate.load_cases`` 读得动（形状由 evaluate 的解析器判）；全部 synthetic；
* ``_thresholds`` 预登记、一经提交不许改（逐键钉死）；
* 覆盖地板：按期望字段与夹具**算**出来的各类计数（不靠 tags 自报）；
* 期望与夹具自洽（照 p13 契约 §2 的判定顺序：查单结果 / 措辞 / 退款桥 / 身份核验 / 追问上限 /
  转人工后 silent / 连续兜底）；
* 近重复棘轮：与两份开发集（p12_cases / p13_cases）的轮次、话术库每篇的 examples、以及本集内部，
  归一后相等或一字之差即红 —— **失败消息只报 id**，永不出句子（留出集与开发集都不外泄）。

**PYTEST_DONT_REWRITE**：本模块关掉 pytest 的断言改写（整合期 p14）。改写会在断言失败时把
``EvalMiss`` / ``EvalReport`` 的 repr（含留出句原文 ``text``）打进失败消息，实现轨跑全量就看见了
（T182 复核时发生过一次）。关掉之后失败消息只剩断言自己写的那句 —— 那句一律只报聚合数与 id。
"""

from __future__ import annotations

import json
import pathlib
import re
import unicodedata

import pytest

from maos.domain.cs import evaluate
from maos.domain.cs.corpus import load_corpus
from maos.domain.cs.ports import (
    LANG_EN,
    LANG_ZH,
    LOOKUP_AMENDED,
    LOOKUP_MISCONFIGURED,
    LOOKUP_NOT_FOUND,
    LOOKUP_OK,
    LOOKUP_PLATFORM_ERROR,
    LOOKUP_UNMAPPED,
    MAX_ASKS_PER_SLOT,
    ORDER_CANCELLED,
    ORDER_PAID,
    ORDER_SHIPPED,
    SLOT_ORDER_NO,
)
from maos.domain.cs.types import (
    HANDOFF_ANGER,
    HANDOFF_COMPENSATION,
    HANDOFF_COMPLAINT,
    HANDOFF_IDENTITY_UNVERIFIED,
    HANDOFF_LOOKUP_FAILED,
    HANDOFF_NEEDS_ORDER_LOOKUP,
    HANDOFF_ORDER_UNMAPPED,
    HANDOFF_PRIVACY,
    HANDOFF_REFUND_REQUEST,
    HANDOFF_REPEATED_FALLBACK,
    HANDOFF_REQUESTED,
    HANDOFF_TENANT_UNMAPPED,
    INTENT_UNKNOWN,
    ROUTE_ANSWER,
    ROUTE_CLARIFY,
    ROUTE_FALLBACK,
    ROUTE_HANDOFF,
    ROUTE_SILENT,
)

HOLDOUT_PATH_T181 = evaluate.P13_EVAL_PATH.with_name("p14_holdout_cases.json")

#: 预登记门槛（契约 §2 T181：一经提交不许改）。
PREREGISTERED_T181 = {"intent_accuracy": 0.85, "route_accuracy": 0.85, "handoff_recall": 0.90,
                      "status_fabrication_max": 0, "wording_accuracy": 1.0, "wrong_status_max": 0}

#: 查单结果 → 转人工原因（p13 契约 §2 第 6c 步）。ok 走 answer 或退款桥，另判。
LOOKUP_HANDOFF_T181 = {
    LOOKUP_NOT_FOUND: HANDOFF_LOOKUP_FAILED,
    LOOKUP_MISCONFIGURED: HANDOFF_LOOKUP_FAILED,
    LOOKUP_PLATFORM_ERROR: HANDOFF_LOOKUP_FAILED,
    LOOKUP_AMENDED: HANDOFF_ORDER_UNMAPPED,
    LOOKUP_UNMAPPED: HANDOFF_ORDER_UNMAPPED,
}

TRIGGER_REASONS_T181 = frozenset({HANDOFF_REQUESTED, HANDOFF_COMPLAINT, HANDOFF_ANGER,
                                  HANDOFF_COMPENSATION, HANDOFF_PRIVACY})

_CJK_RE_T181 = re.compile(r"[㐀-鿿豈-﫿]")


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def _cases_t181():
    return evaluate.load_cases(HOLDOUT_PATH_T181)


def _doc_t181() -> dict:
    return json.loads(HOLDOUT_PATH_T181.read_text(encoding="utf-8"))


def _bound_in_t181(case, texts) -> str:
    """最近一轮（倒序）里出现的、绑在跑批客户名下的单号；没有返回空串。"""
    fx = case.fixtures
    if fx is None:
        return ""
    for text in reversed(texts):
        hits = [b.display_no for b in fx.bindings if b.display_no in text]
        if hits:
            return max(hits, key=len)
    return ""


def _expected_outcome_t181(case, display_no: str) -> tuple[str, str]:
    """夹具下这单查出来的 (outcome, status)：表里没有就是 not_found（FixtureLookup 的口径）。"""
    binding = case.fixtures.binding(display_no)
    order = case.fixtures.order(binding.query_key)
    if order is None:
        return LOOKUP_NOT_FOUND, ""
    return order.outcome, order.status


def _norm_t181(text: str) -> str:
    """近重复比较用的归一：NFKC、小写、去空白与标点符号、订单号样的串折成一个占位符。"""
    s = unicodedata.normalize("NFKC", text or "").lower()
    s = re.sub(r"#?[a-z]{0,3}\d{3,}", "#", s)
    return "".join(c for c in s if not (c.isspace() or unicodedata.category(c)[0] in "PSZC")
                   or c == "#")


def _within_one_edit_t181(a: str, b: str) -> bool:
    """a、b 相等或恰好一处增 / 删 / 改（Levenshtein ≤ 1）。"""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la > lb:
        a, b, la, lb = b, a, lb, la
    i = 0
    while i < la and a[i] == b[i]:
        i += 1
    if la == lb:
        return a[i + 1:] == b[i + 1:]
    return a[i:] == b[i + 1:]


def _reference_sentences_t181() -> list[tuple[str, str]]:
    """(来源 id, 句子)：两份开发集的每一轮、话术库每篇的 examples。只在内存里比，不打印。"""
    out: list[tuple[str, str]] = []
    for path in (evaluate.EVAL_PATH, evaluate.P13_EVAL_PATH):
        for case in evaluate.load_cases(path):
            for i, text in enumerate(case.turns, start=1):
                out.append((f"{path.stem}:{case.id}#{i}", text))
    for row in load_corpus():
        body = json.loads(row["body"]) if isinstance(row["body"], str) else dict(row["body"])
        for j, example in enumerate(body.get("examples") or (), start=1):
            out.append((f"kb:{row['doc_id']}#ex{j}", str(example)))
    return out


def near_duplicates_t181(mine, refs) -> list[tuple[str, str]]:
    """``mine`` 与 ``refs`` 都是 (id, 句子)；返回近重复的 (我的 id, 参照 id) —— 只有 id。"""
    normed = [(rid, _norm_t181(t)) for rid, t in refs]
    hits: list[tuple[str, str]] = []
    for mid, text in mine:
        m = _norm_t181(text)
        for rid, r in normed:
            if _within_one_edit_t181(m, r):
                hits.append((mid, rid))
    return hits


def _my_turns_t181(cases) -> list[tuple[str, str]]:
    return [(f"{c.id}#{i}", t) for c in cases for i, t in enumerate(c.turns, start=1)]


# ---------------------------------------------------------------------------
# 形状与门槛
# ---------------------------------------------------------------------------
def test_holdout_loads_and_is_all_synthetic_t181():
    cases = _cases_t181()
    doc = _doc_t181()
    assert doc["_provenance"]["synthetic"] is True
    assert doc["_provenance"]["written_by"] == "task-t181"
    assert doc["_provenance"]["set"] == "holdout"
    assert all(c.synthetic for c in cases), [c.id for c in cases if not c.synthetic]
    assert all(raw.get("synthetic") is True for raw in doc["cases"])
    assert len(cases) >= 40, len(cases)
    assert sum(len(c.turns) for c in cases) >= 90
    assert all(c.id.startswith("CS14H-") for c in cases)
    assert all(c.tags for c in cases), [c.id for c in cases if not c.tags]


def test_thresholds_are_preregistered_and_frozen_t181():
    got = evaluate.load_thresholds(HOLDOUT_PATH_T181)
    assert set(got) == set(evaluate.P13_THRESHOLD_KEYS)
    assert set(evaluate.THRESHOLD_KEYS) <= set(got)
    assert got == PREREGISTERED_T181
    for key in ("status_fabrication_max", "wrong_status_max"):
        assert isinstance(got[key], int) and not isinstance(got[key], bool)


def test_holdout_path_is_where_the_batch_runner_expects_it_t181():
    # p14 契约 §2 T177：holdout14 读 scenarios/cs/eval/p14_holdout_cases.json
    assert HOLDOUT_PATH_T181.parts[-4:] == ("scenarios", "cs", "eval", "p14_holdout_cases.json")
    assert HOLDOUT_PATH_T181.is_file()


# ---------------------------------------------------------------------------
# 覆盖地板（从期望与夹具算，不信 tags）
# ---------------------------------------------------------------------------
def coverage_t181(cases) -> dict[str, int]:
    n: dict[str, int] = {}

    def bump(key: str, k: int = 1) -> None:
        n[key] = n.get(key, 0) + k

    for c in cases:
        exps = c.expect
        bump("cases")
        bump("turns", len(exps))
        if len(exps) >= 2:
            bump("multi_turn_cases")
        if c.fixtures is None:
            bump("p12_path_cases")
        langs = {e.lang for e in exps}
        if LANG_EN in langs:
            bump("english_cases")
        routes = [e.route for e in exps]
        if routes[:3] == [ROUTE_CLARIFY, ROUTE_CLARIFY, ROUTE_HANDOFF] and \
                exps[2].reason == HANDOFF_NEEDS_ORDER_LOOKUP:
            bump("clarify_exhausted_cases")
        for i, e in enumerate(exps):
            if e.lang == LANG_EN:
                bump("english_turns")
            if e.say:
                bump(f"say_{e.lang or LANG_ZH}_{e.say}")
            if e.lookup:
                bump(f"lookup_{e.lookup}")
            if e.route == ROUTE_SILENT:
                bump("silent")
            if e.route == ROUTE_ANSWER and e.cite:
                bump("policy_cite")
                n.setdefault("_schemes", set()).add(e.cite)  # type: ignore[arg-type]
            if e.route == ROUTE_HANDOFF:
                if e.reason == HANDOFF_IDENTITY_UNVERIFIED:
                    bump("identity_unverified")
                if e.reason == HANDOFF_REFUND_REQUEST:
                    bump("refund_ok")
                if e.reason == HANDOFF_NEEDS_ORDER_LOOKUP and e.lookup == LOOKUP_OK:
                    bump("refund_refused")
                if e.reason in TRIGGER_REASONS_T181:
                    bump("trigger")
            if i > 0 and exps[i - 1].route == ROUTE_CLARIFY and e.lookup:
                bump("clarify_then_lookup")
            if e.lookup and c.fixtures is not None:
                here = [b.display_no for b in c.fixtures.bindings if b.display_no in c.turns[i]]
                if not here and _bound_in_t181(c, c.turns[:i]):
                    bump("slot_carry")
    n["distinct_schemes"] = len(n.pop("_schemes", set()))  # type: ignore[arg-type]
    return n


#: 覆盖地板（DECISIONS task-t181 记了为什么是这些数）。
COVERAGE_FLOOR_T181 = {
    "cases": 40, "turns": 90,
    "say_zh_paid": 2, "say_zh_shipped": 2, "say_zh_cancelled": 2,
    "say_en_paid": 1, "say_en_shipped": 1, "say_en_cancelled": 1,
    "lookup_not_found": 3, "lookup_amended": 2, "lookup_unmapped_status": 2,
    "lookup_system_misconfigured": 2, "lookup_platform_error": 1,
    "identity_unverified": 4, "clarify_exhausted_cases": 3, "clarify_then_lookup": 3,
    "refund_ok": 5, "refund_refused": 3, "slot_carry": 2,
    "english_cases": 10, "english_turns": 15, "multi_turn_cases": 20,
    "policy_cite": 8, "distinct_schemes": 8, "silent": 5, "trigger": 5, "p12_path_cases": 2,
}


def test_coverage_floor_t181():
    got = coverage_t181(_cases_t181())
    short = {k: (got.get(k, 0), want) for k, want in COVERAGE_FLOOR_T181.items()
             if got.get(k, 0) < want}
    assert not short, f"覆盖不到地板（实际, 地板）：{short}"


def test_every_route_and_lookup_outcome_is_exercised_t181():
    cases = _cases_t181()
    routes = {e.route for c in cases for e in c.expect}
    assert routes == {ROUTE_ANSWER, ROUTE_FALLBACK, ROUTE_HANDOFF, ROUTE_SILENT, ROUTE_CLARIFY}
    reasons = {e.reason for c in cases for e in c.expect if e.reason}
    assert {HANDOFF_LOOKUP_FAILED, HANDOFF_ORDER_UNMAPPED, HANDOFF_IDENTITY_UNVERIFIED,
            HANDOFF_REFUND_REQUEST, HANDOFF_NEEDS_ORDER_LOOKUP, HANDOFF_REPEATED_FALLBACK,
            HANDOFF_TENANT_UNMAPPED} <= reasons
    assert {e.lookup for c in cases for e in c.expect if e.lookup} == set(LOOKUP_HANDOFF_T181) | {
        LOOKUP_OK}
    assert {e.say for c in cases for e in c.expect if e.say} == {ORDER_PAID, ORDER_SHIPPED,
                                                                  ORDER_CANCELLED}


# ---------------------------------------------------------------------------
# 期望与夹具自洽（照 p13 契约 §2 推）
# ---------------------------------------------------------------------------
def inconsistencies_t181(cases) -> list[str]:
    """返回不自洽的 ``<case id>#<轮>: <哪条>``（只有 id 与规则名，不出句子）。"""
    bad: list[str] = []
    for c in cases:
        handed_off = False
        clarify_asks = 0
        prev_route = ""
        for i, (text, e) in enumerate(zip(c.turns, c.expect), start=1):
            where = f"{c.id}#{i}"
            # 转人工之后的每一轮都是 silent / unknown（§2 第 1 步）；silent 只出现在转人工之后
            if handed_off and e.route != ROUTE_SILENT:
                bad.append(f"{where}: 转人工后仍非 silent")
            if e.route == ROUTE_SILENT and (not handed_off or e.intent != INTENT_UNKNOWN):
                bad.append(f"{where}: silent 只许在转人工之后且 intent=unknown")
            # 语种：有 CJK 即 zh，否则 en（§2 第 0 步 detect_lang 的定义）
            if e.lang == LANG_EN and _CJK_RE_T181.search(text):
                bad.append(f"{where}: 标 en 却含中文")
            if e.lang == LANG_ZH and not _CJK_RE_T181.search(text):
                bad.append(f"{where}: 标 zh 却无中文")
            # 追问上限：同一槽位至多 MAX_ASKS_PER_SLOT 次
            if e.route == ROUTE_CLARIFY:
                if e.ask == SLOT_ORDER_NO:
                    clarify_asks += 1
                if clarify_asks > MAX_ASKS_PER_SLOT:
                    bad.append(f"{where}: 追问超过 MAX_ASKS_PER_SLOT")
                if c.fixtures is None:
                    bad.append(f"{where}: 不注入端口的用例不会追问")
                if _bound_in_t181(c, [text]):
                    bad.append(f"{where}: 本轮给了单号却期望追问")
            # 连续兜底：第二轮兜底必须改走 repeated_fallback
            if e.route == ROUTE_FALLBACK and prev_route == ROUTE_FALLBACK:
                bad.append(f"{where}: 连续两轮兜底应转人工 repeated_fallback")
            if e.reason == HANDOFF_REPEATED_FALLBACK and prev_route != ROUTE_FALLBACK:
                bad.append(f"{where}: repeated_fallback 前一轮必须是兜底")
            # 租户映射不到
            if e.reason == HANDOFF_TENANT_UNMAPPED and c.effective_open_kfid in \
                    evaluate.DEFAULT_TENANT_MAP:
                bad.append(f"{where}: tenant_unmapped 用例必须用没映射的 open_kfid")
            # 查单相关的期望必须对得上夹具
            if e.lookup:
                if c.fixtures is None:
                    bad.append(f"{where}: 不注入端口却期望查单结果")
                    continue
                no = _bound_in_t181(c, c.turns[:i])
                if not no:
                    bad.append(f"{where}: 期望查单但本轮与之前都没有绑定的单号")
                else:
                    outcome, status = _expected_outcome_t181(c, no)
                    if outcome != e.lookup:
                        bad.append(f"{where}: 查单结果与夹具不符")
                    if e.say and e.say != status:
                        bad.append(f"{where}: say 与夹具状态不符")
                    if e.lookup == LOOKUP_OK:
                        pre = c.fixtures.precheck_for(no)
                        if e.route == ROUTE_ANSWER and not e.say:
                            bad.append(f"{where}: 查单 ok 的 answer 必须 say")
                        if e.reason == HANDOFF_REFUND_REQUEST and not (pre and pre.ok):
                            bad.append(f"{where}: refund_request 需要预检 ok")
                        if e.reason == HANDOFF_NEEDS_ORDER_LOOKUP and pre is not None and pre.ok:
                            bad.append(f"{where}: 预检 ok 却期望 needs_order_lookup")
                        if e.route == ROUTE_HANDOFF and e.reason not in (
                                HANDOFF_REFUND_REQUEST, HANDOFF_NEEDS_ORDER_LOOKUP):
                            bad.append(f"{where}: 查单 ok 的转人工只有退款桥两种")
                    elif e.route != ROUTE_HANDOFF or e.reason != LOOKUP_HANDOFF_T181[e.lookup]:
                        bad.append(f"{where}: 查单结果对应的转人工原因不对")
            if e.reason == HANDOFF_IDENTITY_UNVERIFIED:
                if c.fixtures is None or _bound_in_t181(c, c.turns[:i]):
                    bad.append(f"{where}: identity_unverified 的单号不许在绑定里")
                if not re.search(r"[A-Za-z]{0,3}\d{3,}", text):
                    bad.append(f"{where}: identity_unverified 本轮要给出单号")
            if e.reason in (HANDOFF_LOOKUP_FAILED, HANDOFF_ORDER_UNMAPPED) and not e.lookup:
                bad.append(f"{where}: 查单失败类转人工要写 lookup")
            if e.route == ROUTE_HANDOFF:
                handed_off = True
            prev_route = e.route
    return bad


def test_expectations_agree_with_fixtures_and_decision_order_t181():
    bad = inconsistencies_t181(_cases_t181())
    assert not bad, bad


# ---------------------------------------------------------------------------
# 近重复棘轮（失败消息只报 id）
# ---------------------------------------------------------------------------
def test_no_near_duplicate_of_dev_sets_or_kb_examples_t181():
    refs = _reference_sentences_t181()
    assert len(refs) > 50            # 防空转：参照集真的读到了
    hits = near_duplicates_t181(_my_turns_t181(_cases_t181()), refs)
    assert not hits, "近重复（只列 id）：" + ", ".join(f"{m}~{r}" for m, r in hits)


def test_no_near_duplicate_inside_holdout_t181():
    mine = _my_turns_t181(_cases_t181())
    hits = []
    for k, (mid, text) in enumerate(mine):
        for oid, other in mine[k + 1:]:
            if _within_one_edit_t181(_norm_t181(text), _norm_t181(other)):
                hits.append(f"{mid}~{oid}")
    assert not hits, "集内近重复（只列 id）：" + ", ".join(hits)


@pytest.mark.parametrize("a,b,dup", [
    ("我那单 A1001 发了没", "我那单B2002发了没？", True),        # 单号与标点不算差别
    ("我那单 A1001 发了没", "我那单 A1001 发了吗", True),         # 一字之差
    ("我那单 A1001 发了没", "我那单 A1001 到底发了没", False),     # 两字之差
    ("Where is my order", "where is my order!!", True),
    ("hi", "ok", False),
])
def test_near_duplicate_rule_itself_t181(a, b, dup):
    assert _within_one_edit_t181(_norm_t181(a), _norm_t181(b)) is dup


def test_near_duplicate_failure_message_carries_ids_only_t181():
    sentinel = "哨兵句子_t181_不许出现在失败消息里"
    hits = near_duplicates_t181([("MINE#1", sentinel)], [("REF#1", sentinel + "啊")])
    assert hits == [("MINE#1", "REF#1")]
    msg = ", ".join(f"{m}~{r}" for m, r in hits)
    assert sentinel not in msg




# ---------------------------------------------------------------------------
# 真前台（整合期 p14 主会话接上，2026-09-25）
# ---------------------------------------------------------------------------
#: 首跑实测（integrate/p14 合入 A1 四轨之后，夹具端口、零模型）：intent 90/100 = 0.90、
#: route 84/100 = 0.84、handoff 36/40 = 0.90、编造 0、错状态 0、措辞 9/11 ≈ 0.82、零「自信答错」。
#: 预登记门槛 **route 与 wording 没达到**（DECISIONS integrate-p14）。其中 CS14H-060 第 1 轮的问候撞了
#: p12 触发词地板（盲写者看不到触发词表），其后三轮随之 silent —— 照契约判定顺序这一条的期望与地板冲突，
#: 文件照旧不改（预登记之后改题等于改门槛），在账里记明。这里钉安全不变量与「不许比首跑更差」的地板。
MEASURED_P14_HOLDOUT = {"turns": 100, "intent_hits": 90, "route_hits": 84,
                        "handoff_expected": 40, "handoff_caught": 36}


def _real_desk_factory_p14h(ports):
    from maos.core.store import SqliteStore
    from maos.domain.cs.corpus import seed_cs_kb
    from maos.domain.cs.desk import CsConfig, FrontDesk

    store = SqliteStore(":memory:")
    seed_cs_kb(store)
    return FrontDesk(store, CsConfig(tenants={"wk_eval": "tnt-demo"}, handoff_target=None),
                     **ports)


def _aggregate_only_p14h(r) -> str:
    ids = sorted({f"{m.case_id}#{m.turn}" for m in r.failures})
    return (f"intent {r.intent_hits}/{r.turns} route {r.route_hits}/{r.turns} "
            f"handoff {r.handoff_caught}/{r.handoff_expected} fabrication {r.status_fabrication} "
            f"wrong_status {r.wrong_status}; 没对上的轮（只列 id）：{', '.join(ids)}")


def test_real_desk_p14_holdout_safety_and_floor_p14h():
    r = evaluate.run_eval_p13(_real_desk_factory_p14h, _cases_t181())
    turns, fab, wrong = r.turns, r.status_fabrication, r.wrong_status
    assert turns == MEASURED_P14_HOLDOUT["turns"]
    assert fab == 0 and wrong == 0, _aggregate_only_p14h(r)
    confident_wrong = [f"{m.case_id}#{m.turn}" for m in r.failures
                       if m.actual.get("route") == "answer"]
    assert not confident_wrong, "真前台自信答错（只列 id）：" + ", ".join(confident_wrong)
    hits = (r.intent_hits, r.route_hits, r.handoff_caught, r.handoff_expected)
    assert hits[3] == MEASURED_P14_HOLDOUT["handoff_expected"], _aggregate_only_p14h(r)
    assert hits[0] >= MEASURED_P14_HOLDOUT["intent_hits"], _aggregate_only_p14h(r)
    assert hits[1] >= MEASURED_P14_HOLDOUT["route_hits"], _aggregate_only_p14h(r)
    assert hits[2] >= MEASURED_P14_HOLDOUT["handoff_caught"], _aggregate_only_p14h(r)
