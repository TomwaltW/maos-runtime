"""T175 · p13 评测集（scenarios/cs/eval/p13_cases.json）的形状、覆盖下限、夹具端口与跑批器。

* **数据**：形状照 p13 契约 §3；覆盖下限逐项断言；再按 §2 的一轮判定顺序把整份期望**重推一遍**
  （语种 → silent → 租户 → 触发词 → 查单链：缺单号追问 / 核验 / 查单结果 / 退款桥 → 其余 p12 路径），
  期望是按顺序推出来的，写错一条当场红。
* **夹具端口**：三个 Protocol 的实现恰好按夹具作答（核验只认跑批客户、查单按 query_key、
  预检缺项拒绝）。
* **跑批器**：按期望作答的 FakeDesk（查单轮真的经夹具端口查、真的把观察写进自己的库）跑出满分；
  故意答错一类，对应指标掉；在内存里把一条期望改错，不再满分；wrong_status 与 wording_accuracy
  各有只有它能判负的用例。

本文件不 import T171–T174 的新模块：前台是替身，观察行按契约 §1.3 的列直接写 ``cs_observation``。
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import re

import pytest

from maos.core.store import SqliteStore
from maos.domain.cs import objects
from maos.domain.cs import ports as P
from maos.domain.cs import types as T
from maos.domain.cs.evaluate import (
    DEFAULT_OPEN_KFID,
    DEFAULT_TENANT_MAP,
    EVAL_PATH,
    P13_EVAL_PATH,
    P13_THRESHOLD_KEYS,
    REFUSED_NOT_IN_LEDGER,
    STATUS_OFF_TABLE,
    EvalCase,
    EvalExpect,
    EvalFixtures,
    EvalReport,
    EvalReportP13,
    FixtureLookup,
    FixturePrecheck,
    FixtureVerifier,
    cite_doc_id,
    fixture_ports,
    load_cases,
    load_document,
    load_thresholds,
    run_eval,
    run_eval_p13,
    said_statuses,
    turn_observations,
)
from maos.ingress.contracts import InboundMessage

ROOT_T175 = pathlib.Path(__file__).resolve().parents[2]

CONTRACT_THRESHOLDS_T175 = {"intent_accuracy": 0.9, "route_accuracy": 0.9, "handoff_recall": 0.95,
                            "status_fabrication_max": 0, "wording_accuracy": 1.0,
                            "wrong_status_max": 0}

#: p12 契约 §1.4 触发词最低覆盖表（优先级高 → 低）与各自的意图。
TRIGGERS_T175: dict[str, tuple[str, ...]] = {
    "privacy": ("手机号", "身份证", "住址", "个人信息", "隐私"),
    "compensation": ("赔偿", "补偿", "赔钱", "赔我"),
    "anger": ("垃圾", "骗子", "气死", "滚"),
    "complaint": ("投诉", "12315", "消协", "曝光", "起诉", "律师"),
    "requested": ("转人工", "人工客服", "找人工", "真人"),
}
INTENT_OF_TRIGGER_T175 = {"privacy": "privacy", "compensation": "compensation",
                          "anger": "complaint", "complaint": "complaint",
                          "requested": "handoff_request"}
#: 评测集里的单号写法（字母 + 四位数字）；诉求两类的说法。
ORDER_NO_RE_T175 = re.compile(r"(?<![A-Za-z0-9])[A-Z]\d{4}(?!\d)")
RETURN_RE_T175 = re.compile(r"退货|退款|(?i:\b(?:return|refund)\b)")
TRACK_RE_T175 = re.compile(r"到哪|物流|发货|发出|寄出|快递|状态|(?i:\b(?:track|where|sent)\b)")
#: 查单链才有的四个出口原因（p13 契约 §1.1）。
P13_REASONS_T175 = frozenset({T.HANDOFF_IDENTITY_UNVERIFIED, T.HANDOFF_ORDER_UNMAPPED,
                              T.HANDOFF_LOOKUP_FAILED, T.HANDOFF_REFUND_REQUEST})
SPECIAL_INTENTS_T175 = frozenset({T.INTENT_UNKNOWN, T.INTENT_HANDOFF_REQUEST, T.INTENT_COMPLAINT,
                                  T.INTENT_COMPENSATION, T.INTENT_PRIVACY})


def _triggers_in_t175(text: str) -> list[str]:
    found = [cat for cat, words in TRIGGERS_T175.items() if any(w in text for w in words)]
    if re.search(r"[!！]{3,}", text) and "anger" not in found:
        found.append("anger")
    return [cat for cat in TRIGGERS_T175 if cat in found]


def _detect_lang_t175(text: str) -> str:
    """契约 §1.4 T173 的口径：有 CJK 即 zh；否则 ASCII 字母占多数即 en；否则 zh。"""
    if re.search(r"[\u3400-\u9fff\uf900-\ufaff]", text):
        return P.LANG_ZH
    visible = [c for c in text if not c.isspace()]
    letters = [c for c in visible if c.isascii() and c.isalpha()]
    return P.LANG_EN if visible and len(letters) * 2 > len(visible) else P.LANG_ZH


def _all_turns_t175(cases):
    for case in cases:
        for i, (text, exp) in enumerate(zip(case.turns, case.expect), start=1):
            yield case, i, text, exp


@pytest.fixture(scope="module")
def doc_t175():
    return load_document(P13_EVAL_PATH)


@pytest.fixture(scope="module")
def cases_t175():
    return load_cases(P13_EVAL_PATH)


# ===========================================================================
# 数据：形状
# ===========================================================================
def test_p13_path_points_into_scenarios_t175():
    assert P13_EVAL_PATH == ROOT_T175 / "scenarios" / "cs" / "eval" / "p13_cases.json"
    assert P13_EVAL_PATH.is_file()
    assert EVAL_PATH.name == "p12_cases.json"               # p12 的入口没被挪走


def test_top_level_shape_provenance_and_thresholds_t175(doc_t175):
    assert set(doc_t175) == {"_note", "_provenance", "_thresholds", "cases"}
    assert doc_t175["_provenance"] == {"synthetic": True, "written_by": "task-t175", "set": "dev"}
    assert doc_t175["_thresholds"] == CONTRACT_THRESHOLDS_T175
    assert tuple(doc_t175["_thresholds"]) == P13_THRESHOLD_KEYS
    assert load_thresholds(P13_EVAL_PATH) == CONTRACT_THRESHOLDS_T175


def test_case_shape_t175(doc_t175, cases_t175):
    raw_cases = doc_t175["cases"]
    assert len(raw_cases) == len(cases_t175)
    assert len({c["id"] for c in raw_cases}) == len(raw_cases)
    for raw in raw_cases:
        cid = raw["id"]
        assert re.fullmatch(r"CS13-\d{3}", cid), cid
        assert {"id", "synthetic", "tags", "turns", "expect"} <= set(raw) <= {
            "id", "synthetic", "tags", "open_kfid", "fixtures", "turns", "expect"}, cid
        assert raw["synthetic"] is True and raw["tags"], cid
        assert len(raw["turns"]) == len(raw["expect"]) >= 1, cid
        if "fixtures" in raw:
            assert set(raw["fixtures"]) <= {"bindings", "orders", "precheck"}, cid
            assert "bindings" in raw["fixtures"] and "orders" in raw["fixtures"], cid
        for exp in raw["expect"]:
            assert {"route", "intent", "lang"} <= set(exp) <= {
                "route", "intent", "reason", "cite", "lang", "lookup", "ask", "say"}, cid
            if "fixtures" not in raw:
                assert not {"lookup", "ask", "say"} & set(exp), cid          # p12 式：不查单不追问
                assert exp.get("reason") not in P13_REASONS_T175, cid
    # 绑定一律挂在跑批客户名下：数据里不写客户，由夹具核验端口补
    for case in cases_t175:
        assert case.customer == f"eval-{case.id}"


def _write_t175(tmp_path, cases) -> pathlib.Path:
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"cases": cases}, ensure_ascii=False), encoding="utf-8")
    return p


GOOD_T175 = {"id": "X-1", "synthetic": True, "tags": [], "turns": ["A1001 到哪了"],
             "fixtures": {"bindings": [{"display_no": "A1001", "system_name": "s", "query_key": "q1"}],
                          "orders": {"q1": {"outcome": "ok", "status": "shipped"}},
                          "precheck": {"A1001": {"ok": True, "reason_code": "quality_defect"}}},
             "expect": [{"route": "answer", "intent": "logistics", "lang": "zh", "lookup": "ok",
                         "say": "shipped"}]}


@pytest.mark.parametrize("patch", [
    {"expect": [{"route": "answer", "intent": "logistics", "lang": "fr"}]},
    {"expect": [{"route": "answer", "intent": "logistics", "lookup": "maybe"}]},
    {"expect": [{"route": "clarify", "intent": "logistics"}]},                          # clarify 缺 ask
    {"expect": [{"route": "answer", "intent": "logistics", "ask": "order_no"}]},        # 非 clarify 带 ask
    {"expect": [{"route": "clarify", "intent": "logistics", "ask": "address"}]},        # 槽位不在表里
    {"expect": [{"route": "answer", "intent": "logistics", "lookup": "ok", "say": "amended"}]},
    {"expect": [{"route": "answer", "intent": "logistics", "say": "shipped"}]},         # say 缺 lookup=ok
    {"expect": [{"route": "handoff", "intent": "logistics", "reason": "refund_request",
                 "lookup": "ok", "say": "shipped"}]},                                    # say 只在 answer
    {"fixtures": {"bindings": [{"display_no": "A1001", "system_name": "s"}], "orders": {}}},
    {"fixtures": {"bindings": [], "orders": {"q1": {"outcome": "ok"}}}},                 # ok 缺状态
    {"fixtures": {"bindings": [], "orders": {"q1": {"outcome": "ok", "status": "amended"}}}},
    {"fixtures": {"bindings": [], "orders": {"q1": {"outcome": "not_found", "status": "paid"}}}},
    {"fixtures": {"bindings": [], "orders": {"q1": {"outcome": "lost"}}}},
    {"fixtures": {"bindings": [], "orders": {}, "precheck": {"A1001": {"ok": True}}}},   # 缺原因码
    {"fixtures": {"bindings": [], "orders": {}, "precheck": {"A1001": {"ok": False}}}},  # 缺拒绝理由
    {"fixtures": {"bindings": [], "orders": {}, "precheck": {"A1001": {"ok": "yes"}}}},
    {"fixtures": {"bindings": [], "orders": {}, "extra": {}}},
    {"fixtures": {"bindings": [{"display_no": "A1", "system_name": "s", "query_key": "q"},
                               {"display_no": "A1", "system_name": "s", "query_key": "r"}],
                  "orders": {}}},                                                       # 单号重复
])
def test_load_cases_rejects_malformed_p13_cases_t175(tmp_path, patch):
    assert len(load_cases(_write_t175(tmp_path, [GOOD_T175]))) == 1
    with pytest.raises(ValueError):
        load_cases(_write_t175(tmp_path, [{**GOOD_T175, **patch}]))


def test_parsed_fixtures_and_expect_t175(tmp_path):
    (case,) = load_cases(_write_t175(tmp_path, [GOOD_T175]))
    fx = case.fixtures
    assert isinstance(fx, EvalFixtures)
    assert fx.binding("A1001").query_key == "q1" and fx.binding("A1002") is None
    assert fx.order("q1").status == "shipped" and fx.order("A1001") is None
    pre = fx.precheck_for("A1001")
    assert pre.ok and pre.command_line == "/refund A1001 quality_defect"
    assert case.expect[0] == EvalExpect(route="answer", intent="logistics", lang="zh",
                                        lookup="ok", say="shipped")
    assert case.expect[0].to_json() == GOOD_T175["expect"][0]
    # p12 式（不写 fixtures）：None，不是空夹具
    (bare,) = load_cases(_write_t175(tmp_path, [{k: v for k, v in GOOD_T175.items()
                                                 if k != "fixtures"} | {
        "expect": [{"route": "answer", "intent": "logistics"}]}]))
    assert bare.fixtures is None


# ===========================================================================
# 数据：覆盖下限（契约 §3 逐项）
# ===========================================================================
def test_at_least_fifty_turns_t175(cases_t175):
    assert sum(len(c.turns) for c in cases_t175) >= 50


def test_each_lookup_outcome_at_least_twice_t175(cases_t175):
    counts = {o: 0 for o in P.LOOKUP_OUTCOMES}
    for _, _, _, exp in _all_turns_t175(cases_t175):
        if exp.lookup:
            counts[exp.lookup] += 1
    assert all(n >= 2 for n in counts.values()), counts


def test_every_status_is_said_and_both_languages_look_up_t175(cases_t175):
    said = {(e.lang, e.say) for _, _, _, e in _all_turns_t175(cases_t175) if e.say}
    assert {s for _, s in said} == {"paid", "shipped", "cancelled"}
    assert {lang for lang, _ in said} == {P.LANG_ZH, P.LANG_EN}          # 英文查单


def test_clarify_and_clarify_twice_then_handoff_t175(cases_t175):
    assert any(e.route == T.ROUTE_CLARIFY for _, _, _, e in _all_turns_t175(cases_t175))

    def twice_then_handoff(case: EvalCase) -> bool:
        return any(a.route == b.route == T.ROUTE_CLARIFY and c.route == T.ROUTE_HANDOFF
                   and c.reason == T.HANDOFF_NEEDS_ORDER_LOOKUP
                   for a, b, c in zip(case.expect, case.expect[1:], case.expect[2:]))
    assert any(twice_then_handoff(c) for c in cases_t175)
    # 追问之后补了单号、接着查单
    assert any(a.route == T.ROUTE_CLARIFY and b.lookup
               for c in cases_t175 for a, b in zip(c.expect, c.expect[1:]))


def test_identity_failure_and_refund_bridge_both_ways_t175(cases_t175):
    turns = list(_all_turns_t175(cases_t175))
    assert any(e.reason == T.HANDOFF_IDENTITY_UNVERIFIED for *_, e in turns)
    assert any(e.reason == T.HANDOFF_REFUND_REQUEST for *_, e in turns)            # 预检 ok
    refused = [(c, e) for c, _, _, e in turns
               if e.reason == T.HANDOFF_NEEDS_ORDER_LOOKUP and e.lookup == P.LOOKUP_OK]
    assert refused                                                                # 预检 refused
    # 拒绝里既有「台账里没有」（夹具缺项），也有夹具写明的拒绝
    kinds = set()
    for case, _ in refused:
        for no, _ in case.fixtures.orders:
            pre = case.fixtures.precheck_for(no)
            kinds.add("missing" if pre is None else pre.refused_why)
    assert "missing" in kinds and len(kinds) >= 2, kinds


def test_english_lookup_and_english_fallback_t175(cases_t175):
    turns = list(_all_turns_t175(cases_t175))
    assert any(e.lang == P.LANG_EN and e.lookup for *_, e in turns)
    en_fallback = [(c, e) for c, _, _, e in turns
                   if e.lang == P.LANG_EN and e.route == T.ROUTE_FALLBACK]
    assert {c.fixtures is None for c, _ in en_fallback} == {True, False}   # 注入端口与否各一


def test_policy_then_lookup_and_slot_accumulation_t175(cases_t175):
    def policy_then_lookup(case: EvalCase) -> bool:
        return any(a.route == T.ROUTE_ANSWER and a.cite and b.lookup
                   for a, b in zip(case.expect, case.expect[1:]))

    def order_no_first(case: EvalCase) -> bool:
        """先说单号（那一轮不说诉求）、后说诉求（那一轮不说单号），后一轮照样查单。"""
        for (ta, a), (tb, b) in zip(zip(case.turns, case.expect),
                                    zip(case.turns[1:], case.expect[1:])):
            if (ORDER_NO_RE_T175.search(ta) and not (RETURN_RE_T175.search(ta)
                                                      or TRACK_RE_T175.search(ta))
                    and not ORDER_NO_RE_T175.search(tb) and b.lookup):
                return True
        return False
    assert sum(policy_then_lookup(c) for c in cases_t175) >= 2
    assert sum(order_no_first(c) for c in cases_t175) >= 2


def test_p12_style_cases_at_least_five_t175(cases_t175):
    p12_style = [c for c in cases_t175 if c.fixtures is None]
    assert len(p12_style) >= 5
    routes = {e.route for c in p12_style for e in c.expect}
    assert {T.ROUTE_ANSWER, T.ROUTE_HANDOFF, T.ROUTE_FALLBACK, T.ROUTE_SILENT} <= routes


def test_trigger_beats_lookup_and_tenant_unmapped_with_fixtures_t175(cases_t175):
    with_ports = [(c, t, e) for c, _, t, e in _all_turns_t175(cases_t175) if c.fixtures]
    assert any(_triggers_in_t175(t) and ORDER_NO_RE_T175.search(t) for _, t, _ in with_ports)
    assert any(c.open_kfid and c.open_kfid not in DEFAULT_TENANT_MAP for c, _, _ in with_ports)


#: 不带单号的轮里「点名要看具体订单」的说法（到哪了 / 帮我查查 / 我要退货 / where / track）——
#: 与「下单后多久能发货」「退货运费谁出」这类政策问法区分（复核 L2-2：查单链由正文推，不由标签推）。
SPECIFIC_RE_T175 = re.compile(r"到哪|查查|查一下|查下|看看|看下|发了没|发货了没|发货没有|发出去了没"
                              r"|寄出来没|我要退|想退|申请退|(?i:\b(?:where|track|check)\b)")
#: 要看具体订单的话术编号（p12 契约 §1.5 带 needs_order_lookup 的六篇）：注入端口时不该被当政策答。
ORDER_SCHEMES_T175 = frozenset({"LOG-004", "LOG-005", "LOG-006", "PAY-003", "PAY-004", "RET-005"})


def _judge_case_t175(case: EvalCase) -> None:
    """按 p13 契约 §2 的判定顺序把一个 case 的每轮期望重推一遍；对不上就 AssertionError。

    「这一轮进不进查单链」由**正文与累积的槽位**推（有诉求、且本轮带单号或点名要看具体订单），
    再断言期望的标签与它一致 —— 标签写错（该追问的写成兜底、该查单的写成政策答）当场红。
    """
    tenant = DEFAULT_TENANT_MAP.get(case.effective_open_kfid, "")
    fx = case.fixtures
    handed_off, streak, asks = False, 0, 0
    order_no: str | None = None
    request: str | None = None
    for i, (text, exp) in enumerate(zip(case.turns, case.expect), start=1):
        where = (case.id, i, text)
        assert exp.lang == _detect_lang_t175(text), where                          # 0. 语种
        if handed_off:                                                             # 1.
            assert (exp.route, exp.intent) == (T.ROUTE_SILENT, T.INTENT_UNKNOWN), where
            continue
        assert exp.route != T.ROUTE_SILENT, where
        if not tenant:                                                             # 2.
            assert (exp.route, exp.reason, exp.intent) == (
                T.ROUTE_HANDOFF, T.HANDOFF_TENANT_UNMAPPED, T.INTENT_UNKNOWN), where
            handed_off = True
            continue
        triggers = _triggers_in_t175(text)
        if triggers:                                                               # 3.
            top = triggers[0]
            assert (exp.route, exp.reason, exp.intent) == (
                T.ROUTE_HANDOFF, top, INTENT_OF_TRIGGER_T175[top]), where
            assert not (exp.lookup or exp.say or exp.ask), where
            handed_off = True
            continue
        found = ORDER_NO_RE_T175.findall(text)                                     # 4. 槽位累积
        if found:
            order_no = found[-1]
        if RETURN_RE_T175.search(text):
            request = "return"
        elif TRACK_RE_T175.search(text):
            request = "track"
        # 由正文推：有诉求（本轮或之前），且本轮带单号或点名要看具体订单
        wants_order = request is not None and bool(found or SPECIFIC_RE_T175.search(text))
        labelled_flow = bool(exp.route == T.ROUTE_CLARIFY or exp.lookup
                             or exp.reason in P13_REASONS_T175
                             or (fx is not None and exp.reason == T.HANDOFF_NEEDS_ORDER_LOOKUP))
        order_flow = fx is not None and wants_order
        if fx is None:                                                             # 5. p12 路径
            assert not (exp.lookup or exp.ask or exp.say), where
            assert exp.route != T.ROUTE_CLARIFY and exp.reason not in P13_REASONS_T175, where
            # p12 口径：要看具体订单的一律 needs_order_lookup（话术带标记），别的轮不是
            assert (exp.reason == T.HANDOFF_NEEDS_ORDER_LOOKUP) == wants_order, where
        else:
            assert labelled_flow == order_flow, (where, "期望的标签与正文推出的查单链不一致")
        if order_flow:                                                             # 6. 查单链
            assert fx is not None and request is not None, where
            want_intent = T.INTENT_LOGISTICS if request == "track" else T.INTENT_RETURN_EXCHANGE
            assert exp.intent == want_intent, where
            assert not exp.cite, where
            if order_no is None:                                                   # 6a
                assert not (exp.lookup or exp.say), where
                if asks >= P.MAX_ASKS_PER_SLOT:
                    assert (exp.route, exp.reason) == (
                        T.ROUTE_HANDOFF, T.HANDOFF_NEEDS_ORDER_LOOKUP), where
                else:
                    assert (exp.route, exp.ask) == (T.ROUTE_CLARIFY, P.SLOT_ORDER_NO), where
                    asks += 1
            else:
                binding = fx.binding(order_no)
                if binding is None:                                                # 6b
                    assert (exp.route, exp.reason) == (
                        T.ROUTE_HANDOFF, T.HANDOFF_IDENTITY_UNVERIFIED), where
                    assert not exp.lookup, where                                   # 不查单
                else:                                                              # 6c
                    order = fx.order(binding.query_key)
                    outcome = order.outcome if order else P.LOOKUP_NOT_FOUND
                    assert exp.lookup == outcome, where
                    if outcome == P.LOOKUP_OK and request == "return":             # 6d
                        pre = fx.precheck_for(order_no)
                        want = (T.HANDOFF_REFUND_REQUEST if pre is not None and pre.ok
                                else T.HANDOFF_NEEDS_ORDER_LOOKUP)
                        assert (exp.route, exp.reason, exp.say) == (
                            T.ROUTE_HANDOFF, want, ""), where
                    elif outcome == P.LOOKUP_OK:
                        assert (exp.route, exp.say) == (T.ROUTE_ANSWER, order.status), where
                    elif outcome in (P.LOOKUP_AMENDED, P.LOOKUP_UNMAPPED):
                        assert (exp.route, exp.reason) == (
                            T.ROUTE_HANDOFF, T.HANDOFF_ORDER_UNMAPPED), where
                    else:
                        assert (exp.route, exp.reason) == (
                            T.ROUTE_HANDOFF, T.HANDOFF_LOOKUP_FAILED), where
        else:                                                                      # 5 / 7. 话术
            assert exp.route != T.ROUTE_CLARIFY and not (exp.lookup or exp.ask or exp.say), where
            assert exp.reason not in P13_REASONS_T175, where
            assert exp.reason not in TRIGGERS_T175, where
            assert exp.reason != T.HANDOFF_TENANT_UNMAPPED, where
            if fx is not None and exp.route == T.ROUTE_ANSWER:
                assert not ORDER_NO_RE_T175.search(text), where                    # 带单号的是查单
                assert exp.cite and exp.cite not in ORDER_SCHEMES_T175, where      # 注入端口的答是政策答
            if exp.route == T.ROUTE_FALLBACK:
                streak += 1
                assert streak < T.FALLBACK_STREAK_HANDOFF, where
                assert exp.intent == T.INTENT_UNKNOWN, where
            elif exp.reason == T.HANDOFF_REPEATED_FALLBACK:
                assert streak + 1 == T.FALLBACK_STREAK_HANDOFF, where
                assert exp.intent == T.INTENT_UNKNOWN, where
            else:
                assert exp.route == T.ROUTE_ANSWER or (
                    fx is None and exp.reason == T.HANDOFF_NEEDS_ORDER_LOOKUP), where
                assert exp.intent not in SPECIAL_INTENTS_T175, where
        if exp.route in (T.ROUTE_ANSWER, T.ROUTE_HANDOFF):
            streak = 0                                       # clarify 与 silent 不动兜底计数
        if exp.route == T.ROUTE_HANDOFF:
            handed_off = True


def _judge_t175(cases) -> list[str]:
    """逐 case 跑 :func:`_judge_case_t175`，收集对不上的（case id + 断言说明）。"""
    out: list[str] = []
    for case in cases:
        try:
            _judge_case_t175(case)
        except AssertionError as exc:
            out.append(f"{case.id}: {exc}")
    return out


def test_expectations_follow_the_p13_judgment_order_t175(cases_t175):
    """按 p13 契约 §2 的判定顺序把每一轮的期望重推一遍（数据自洽的判据）。"""
    assert _judge_t175(cases_t175) == []


def _mutate_t175(cases, case_id: str, turn: int, **changes):
    """内存里把某个 case 第 turn 轮（1 起）的期望改掉，返回新的 case 元组。"""
    out = []
    for case in cases:
        if case.id == case_id:
            exp = dataclasses.replace(case.expect[turn - 1], **changes)
            case = dataclasses.replace(case, expect=case.expect[:turn - 1] + (exp,)
                                       + case.expect[turn:])
        out.append(case)
    return tuple(out)


@pytest.mark.parametrize("case_id,turn,changes", [
    # 复核 L2-2 的原样变异体：该追问（缺单号、问物流）的轮写成兜底
    ("CS13-017", 1, {"route": T.ROUTE_FALLBACK, "intent": T.INTENT_UNKNOWN, "ask": ""}),
    # 政策问法（下单后多久发货）写成追问
    ("CS13-025", 1, {"route": T.ROUTE_CLARIFY, "cite": "", "ask": P.SLOT_ORDER_NO}),
    # 只报单号、没有诉求的轮写成追问
    ("CS13-027", 1, {"route": T.ROUTE_CLARIFY, "intent": T.INTENT_LOGISTICS, "ask": P.SLOT_ORDER_NO}),
    # 缺单号的退货诉求直接转人工（该先追问）
    ("CS13-018", 1, {"route": T.ROUTE_HANDOFF, "reason": T.HANDOFF_NEEDS_ORDER_LOOKUP, "ask": ""}),
    # 追问两次以后还在追问（该转人工）
    ("CS13-016", 3, {"route": T.ROUTE_CLARIFY, "reason": "", "ask": P.SLOT_ORDER_NO}),
    # 查单结果对应的出口写错：unmapped_status 该 order_unmapped
    ("CS13-008", 1, {"reason": T.HANDOFF_LOOKUP_FAILED}),
    # 预检缺项（台账里没有）写成 refund_request
    ("CS13-022", 1, {"reason": T.HANDOFF_REFUND_REQUEST}),
    # 说的状态与夹具不符
    ("CS13-001", 1, {"say": "paid"}),
    # p12 式要看具体订单的轮写成兜底
    ("CS13-036", 1, {"route": T.ROUTE_FALLBACK, "intent": T.INTENT_UNKNOWN, "reason": ""}),
    # 连续第二次兜底写成兜底（该 repeated_fallback）
    ("CS13-031", 3, {"route": T.ROUTE_FALLBACK, "reason": ""}),
    # 政策答引用了要看具体订单的话术编号
    ("CS13-025", 1, {"cite": "LOG-004"}),
])
def test_judge_rejects_mislabelled_expectations_t175(cases_t175, case_id, turn, changes):
    """判据能判负：期望在内存里改错一条，重推必须红、且红在那个 case 上。"""
    assert _judge_t175(cases_t175) == []
    bad = _judge_t175(_mutate_t175(cases_t175, case_id, turn, **changes))
    assert bad and all(x.startswith(case_id + ":") for x in bad), bad


def test_cites_only_on_answer_turns_and_in_tags_t175(cases_t175):
    for case, i, _, exp in _all_turns_t175(cases_t175):
        if exp.cite:
            assert exp.route == T.ROUTE_ANSWER and not exp.lookup, (case.id, i)
            assert exp.cite in case.tags, (case.id, i)


CORPUS_PATH_T175 = ROOT_T175 / "scenarios" / "cs" / "kb" / "cs_scripts.json"


def _corpus_examples_t175(path: pathlib.Path) -> set[str]:
    examples: set[str] = set()

    def walk(node):
        if isinstance(node, str) and node.strip()[:1] in ("{", "["):
            try:
                walk(json.loads(node))
            except ValueError:
                pass
        elif isinstance(node, dict):
            for key, value in node.items():
                if key == "examples" and isinstance(value, list):
                    examples.update(str(x).strip() for x in value if isinstance(x, str))
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(json.loads(path.read_text(encoding="utf-8")))
    return examples


def test_sentences_copy_neither_corpus_examples_nor_p12_dev_t175(cases_t175):
    ours = {t.strip() for _, _, t, _ in _all_turns_t175(cases_t175)}
    p12 = {t.strip() for c in load_cases(EVAL_PATH) for t in c.turns}
    assert not ours & p12, sorted(ours & p12)
    if CORPUS_PATH_T175.is_file():
        examples = _corpus_examples_t175(CORPUS_PATH_T175)
        assert examples, "话术库里一条 examples 都没读到 —— 形状变了，这条判据空转"
        assert not ours & examples, sorted(ours & examples)


# ===========================================================================
# 夹具端口
# ===========================================================================
def _fx_t175(**kw) -> EvalFixtures:
    base = {"bindings": (), "orders": (), "precheck": ()}
    return EvalFixtures(**{**base, **kw})


def test_fixture_ports_implement_the_protocols_t175(cases_t175):
    case = next(c for c in cases_t175 if c.fixtures and c.fixtures.precheck)
    ports = fixture_ports(case, tenant_id="tnt-demo")
    assert set(ports) == {"verifier", "lookup", "precheck"}
    assert isinstance(ports["verifier"], P.IdentityVerifier)
    assert isinstance(ports["lookup"], P.OrderLookup)
    assert isinstance(ports["precheck"], P.RefundPrecheck)
    p12_style = next(c for c in cases_t175 if c.fixtures is None)
    assert fixture_ports(p12_style, tenant_id="tnt-demo") == {
        "verifier": None, "lookup": None, "precheck": None}


def test_fixture_verifier_only_resolves_for_the_eval_customer_t175(cases_t175):
    case = next(c for c in cases_t175 if c.id == "CS13-005")
    v = FixtureVerifier(case.fixtures, tenant_id="tnt-demo", external_userid=case.customer)
    kw = {"tenant_id": "tnt-demo", "channel": T.CHANNEL_WECHAT_KF,
          "external_userid": "eval-CS13-005", "display_no": "E5005"}
    got = v.resolve(None, **kw)
    assert got == P.Binding(tenant_id="tnt-demo", channel=T.CHANNEL_WECHAT_KF,
                            external_userid="eval-CS13-005", display_no="E5005",
                            system_name="demo-orders", query_key="gid-88005", source=P.BINDING_TEST)
    for bad in ({"external_userid": "eval-CS13-004"}, {"tenant_id": "tnt-other"},
                {"channel": "feishu"}, {"display_no": "e5005"}, {"display_no": "E5006"},
                {"display_no": "gid-88005"}):
        assert v.resolve(None, **{**kw, **bad}) is None, bad
    assert len(v.calls) == 7 and v.calls[0] == kw
    # 租户空：失败即关
    empty = FixtureVerifier(case.fixtures, tenant_id="", external_userid=case.customer)
    assert empty.resolve(None, **{**kw, "tenant_id": ""}) is None


def test_fixture_lookup_answers_exactly_from_orders_t175():
    fx = _fx_t175(orders=(("q-ok", _order_t175("ok", "paid", version=4, updated_at="u")),
                          ("q-bad", _order_t175("platform_error", error_kind="ValueError"))))
    lk = FixtureLookup(fx)

    def binding(q):
        return P.Binding("tnt-demo", T.CHANNEL_WECHAT_KF, "eval-X", "A1", "demo-orders", q,
                         P.BINDING_TEST)
    assert lk.lookup(None, binding("q-ok"), plan_id="cs:c", task_id="t1") == P.LookupResult(
        outcome="ok", system_name="demo-orders", query_key="q-ok", status="paid", version=4,
        updated_at="u")
    assert lk.lookup(None, binding("q-bad"), plan_id="cs:c", task_id="t2") == P.LookupResult(
        outcome="platform_error", system_name="demo-orders", query_key="q-bad",
        error_kind="ValueError")
    missing = lk.lookup(None, binding("q-none"), plan_id="cs:c", task_id="t3")
    assert (missing.outcome, missing.status, missing.error_kind) == ("not_found", "", "KeyError")
    assert [c["task_id"] for c in lk.calls] == ["t1", "t2", "t3"]
    assert lk.calls[0] == {"display_no": "A1", "query_key": "q-ok", "plan_id": "cs:c",
                           "task_id": "t1"}


def _order_t175(outcome, status="", **kw):
    from maos.domain.cs.evaluate import FixtureOrder
    return FixtureOrder(outcome=outcome, status=status, **kw)


def test_fixture_precheck_answers_from_precheck_and_refuses_missing_t175():
    ok = P.PrecheckResult(ok=True, decision="approve", reason_code="quality_defect",
                          command_line="/refund A1 quality_defect")
    no = P.PrecheckResult(ok=False, refused_why="reason_not_matched")
    pc = FixturePrecheck(_fx_t175(precheck=(("A1", ok), ("A2", no))))
    kw = {"tenant_id": "tnt-demo", "reason_text": "质量问题", "now": "2026-09-24T00:00:00+00:00"}
    assert pc.precheck(order_no="A1", **kw) is ok
    assert pc.precheck(order_no="A2", **kw) is no
    assert pc.precheck(order_no="A3", **kw) == P.PrecheckResult(
        ok=False, refused_why=REFUSED_NOT_IN_LEDGER)
    assert REFUSED_NOT_IN_LEDGER == "order_not_in_ledger"
    assert [c["order_no"] for c in pc.calls] == ["A1", "A2", "A3"]


# ===========================================================================
# 跑批器：FakeDesk
# ===========================================================================
#: 契约 §1.3 的 cs_observation（W-A 期骨架里还没有这张表；T171 合入后 ensure_schema 先建、这句空转）。
_OBS_DDL_T175 = (
    "CREATE TABLE IF NOT EXISTS cs_observation (tenant_id TEXT NOT NULL, observation_id TEXT NOT NULL,"
    " conversation_id TEXT NOT NULL, turn_id TEXT NOT NULL, kind TEXT NOT NULL,"
    " system_name TEXT NOT NULL, query_key TEXT NOT NULL, status TEXT NOT NULL,"
    " version INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL DEFAULT '',"
    " observed_at TEXT NOT NULL, PRIMARY KEY (tenant_id, observation_id))")


def _store_t175() -> SqliteStore:
    store = SqliteStore()
    store.init_schema()
    objects.ensure_schema(store)
    objects.execute(store, _OBS_DDL_T175)
    return store


def _write_obs_t175(store, *, tenant, conv, turn, n, system_name, query_key, status) -> str:
    obs_id = P.observation_id_for(turn, n)
    objects.execute(store, "INSERT INTO cs_observation (tenant_id, observation_id, conversation_id,"
                           " turn_id, kind, system_name, query_key, status, version, updated_at,"
                           " observed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (tenant, obs_id, conv, turn, P.OBS_ORDER_LOOKUP, system_name, query_key,
                     status, 1, "", "2026-09-24T00:00:00+00:00"))
    return obs_id


REPLY_ZH_T175 = "好的，请参考我们的服务说明。"
REPLY_EN_T175 = "Thanks, a colleague will follow up with you."


class FakeDeskT175:
    """按期望作答的前台替身。查单轮**真的**经夹具端口核验、查单，并把观察写进自己的库。

    ``tweak(msg, exp, fields) -> dict | None`` 改写某一轮：可改 route / intent / reason / lang /
    lookup / ask / reply / citations，以及查单轮的 ``say``（说哪个状态）、``say_lang``、
    ``obs_status``（观察行记成什么状态）、``write_obs``、``claim``；``lookup_now``（不说状态的轮也真的
    核验、查单、落观察，例如退款桥轮）、``claim_literal``（obs claim 挂在哪句上，缺省是措辞表那句）、
    ``suffix``（接在回复后面的话）。
    """

    def __init__(self, answers, tenant_map, ports, tweak=None, *, with_store=True):
        self.answers = answers
        self.tenant_map = dict(tenant_map)
        self.ports = ports
        self.tweak = tweak
        if with_store:
            self.store = _store_t175()
        self._db = self.store if with_store else _store_t175()
        self.seen: list[InboundMessage] = []

    def handle(self, msg: InboundMessage) -> T.DeskResult:
        self.seen.append(msg)
        exp = self.answers[msg.msg_id]
        open_kfid = msg.raw.get("open_kfid", "")
        tenant = self.tenant_map.get(open_kfid, "")
        conv = T.conversation_id_for(tenant, msg.channel, open_kfid, msg.chat_id)
        turn = T.turn_id_for(conv, len(self.seen))
        lang = exp.lang or P.LANG_ZH
        fields = {
            "route": exp.route, "intent": exp.intent, "reason": exp.reason, "lang": lang,
            "lookup": exp.lookup, "ask": exp.ask,
            "reply": "" if exp.route == T.ROUTE_SILENT else (
                REPLY_EN_T175 if lang == P.LANG_EN else REPLY_ZH_T175),
            "citations": (cite_doc_id(tenant, exp.cite),) if exp.cite else (),
            "say": exp.say, "say_lang": lang, "obs_status": exp.say, "write_obs": True,
            "claim": True, "lookup_now": None, "claim_literal": "", "suffix": "",
        }
        if self.tweak is not None:
            fields.update(self.tweak(msg, exp, fields) or {})
        if fields["lookup_now"] is None:                     # 缺省：要说状态的轮才查单
            fields["lookup_now"] = bool(fields["say"])
        claims: tuple[T.Claim, ...] = ()
        if fields["lookup_now"]:
            history = " ".join(m.text for m in self.seen)
            order_no = ORDER_NO_RE_T175.findall(history)[-1]
            binding = self.ports["verifier"].resolve(
                self.store if hasattr(self, "store") else None, tenant_id=tenant,
                channel=msg.channel, external_userid=msg.chat_id, display_no=order_no)
            if binding is not None:                          # 核验不过：不查单、不说状态
                result = self.ports["lookup"].lookup(None, binding, plan_id=T.plan_id_for(conv),
                                                     task_id=turn)
                sentence = ""
                if fields["say"]:
                    sentence = P.ORDER_STATUS_WORDING[fields["say_lang"]][fields["say"]]
                    fields["reply"] = sentence
                if fields["write_obs"]:
                    obs_id = _write_obs_t175(self._db, tenant=tenant, conv=conv, turn=turn, n=1,
                                             system_name=result.system_name,
                                             query_key=result.query_key,
                                             status=fields["obs_status"] or result.status)
                else:
                    obs_id = P.observation_id_for(turn, 1)
                literal = fields["claim_literal"] or sentence
                if fields["claim"] and literal:
                    claims = (T.Claim(literal, f"obs:{obs_id}"),)
        fields["reply"] += fields["suffix"]
        return T.DeskResult(
            reply_text=fields["reply"], tenant_id=tenant, conversation_id=conv, turn_id=turn,
            route=fields["route"], intent=fields["intent"],
            draft=T.ReplyDraft(text=fields["reply"], claims=claims,
                               citations=tuple(fields["citations"])),
            handoff_reason=fields["reason"], lang=fields["lang"],
            lookup_outcome=fields["lookup"], ask_slot=fields["ask"])


def _answers_t175(cases) -> dict[str, EvalExpect]:
    return {f"{c.id}-{i}": e for c in cases for i, e in enumerate(c.expect, start=1)}


def _factory_t175(cases, *, tweak=None, tenant_map=DEFAULT_TENANT_MAP, with_store=True):
    desks: list[FakeDeskT175] = []
    got_ports: list[dict] = []
    answers = _answers_t175(cases)

    def factory(ports):
        got_ports.append(ports)
        desk = FakeDeskT175(answers, tenant_map, ports, tweak, with_store=with_store)
        desks.append(desk)
        return desk
    return factory, desks, got_ports


def _perfect_t175(report: EvalReportP13) -> bool:
    m = report.metrics()
    return (all(m[k] == 1.0 for k in ("intent_accuracy", "route_accuracy", "handoff_recall",
                                      "cite_accuracy", "wording_accuracy", "lang_accuracy",
                                      "lookup_accuracy", "ask_accuracy"))
            and m["status_fabrication"] == 0 and m["wrong_status"] == 0
            and report.failures == ())


def _say_turn_t175(cases, lang=P.LANG_ZH) -> tuple[EvalCase, int]:
    return next((c, i) for c, i, _, e in _all_turns_t175(cases) if e.say and e.lang == lang)


def test_perfect_desk_scores_full_marks_t175(cases_t175):
    factory, desks, got_ports = _factory_t175(cases_t175)
    report = run_eval_p13(factory, cases_t175)
    assert isinstance(report, EvalReportP13) and isinstance(report, EvalReport)
    assert _perfect_t175(report), report.describe()
    assert report.meets(load_thresholds(P13_EVAL_PATH))
    assert report.cases == len(cases_t175) and report.turns == sum(len(c.turns) for c in cases_t175)
    says = sum(1 for *_, e in _all_turns_t175(cases_t175) if e.say)
    assert report.wording_expected == report.wording_hits == says >= 6
    assert report.lookup_expected >= 12 and report.ask_expected >= 4 and report.lang_expected == report.turns
    # 每个 case 一个新前台、一组新端口；p12 式三个 None；消息形状同 p12
    assert len(desks) == len(got_ports) == len(cases_t175)
    for case, desk, ports in zip(cases_t175, desks, got_ports):
        assert set(ports) == {"verifier", "lookup", "precheck"}
        if case.fixtures is None:
            assert set(ports.values()) == {None}
        else:
            assert ports["verifier"].external_userid == f"eval-{case.id}"
            assert ports["verifier"].fixtures is case.fixtures
            # 查单轮真的经夹具端口查了（每个 say 轮一次）
            assert len(ports["lookup"].calls) == sum(1 for e in case.expect if e.say)
        for i, msg in enumerate(desk.seen, start=1):
            assert msg.chat_id == msg.sender == f"eval-{case.id}"
            assert msg.msg_id == f"{case.id}-{i}"
            assert msg.raw == {"open_kfid": case.open_kfid or DEFAULT_OPEN_KFID}


def test_wrong_intent_route_recall_each_drop_t175(cases_t175):
    th = load_thresholds(P13_EVAL_PATH)

    def run(tweak):
        return run_eval_p13(_factory_t175(cases_t175, tweak=tweak)[0], cases_t175)

    wrong_intent = run(lambda m, e, f: {"intent": T.INTENT_GENERAL}
                       if e.intent == T.INTENT_LOGISTICS else None)
    assert wrong_intent.intent_accuracy < 0.9 and wrong_intent.route_accuracy == 1.0
    assert not wrong_intent.meets(th)
    never_handoff = run(lambda m, e, f: {"route": T.ROUTE_FALLBACK, "reason": ""}
                        if e.route == T.ROUTE_HANDOFF else None)
    assert never_handoff.handoff_recall == 0.0 and never_handoff.route_accuracy < 0.9
    assert not never_handoff.meets(th)
    no_clarify = run(lambda m, e, f: {"route": T.ROUTE_FALLBACK, "ask": ""}
                     if e.route == T.ROUTE_CLARIFY else None)
    assert no_clarify.route_accuracy < 1.0 and no_clarify.ask_accuracy == 0.0
    assert {("route", "ask")} == {m.problems for m in no_clarify.failures}


def test_wrong_status_counts_when_desk_says_another_status_t175(cases_t175):
    """前台把观察记错、说了另一个状态：观察撑得住、措辞对得上观察 —— 只有 wrong_status 能判负。"""
    case, turn = _say_turn_t175(cases_t175)
    victim = f"{case.id}-{turn}"
    truth = case.expect[turn - 1].say
    other = next(s for s in ("paid", "shipped", "cancelled") if s != truth)
    report = run_eval_p13(_factory_t175(cases_t175, tweak=lambda m, e, f: {
        "say": other, "obs_status": other} if m.msg_id == victim else None)[0], cases_t175)
    assert report.wrong_status == 1
    assert report.status_fabrication == 0                  # 有观察撑，不算编造
    assert report.wording_accuracy < 1.0                   # 说的不是期望那句
    assert not report.meets(load_thresholds(P13_EVAL_PATH))
    assert any("wrong_status" in s for s in report.shortfalls(load_thresholds(P13_EVAL_PATH)))
    (miss,) = report.failures
    assert miss.case_id == case.id and set(miss.problems) == {"wrong_status", "wording"}


def test_status_word_in_a_non_lookup_turn_is_fabrication_and_wrong_t175(cases_t175):
    """p12 式轮里说「您的订单已发货」：没有观察、没有夹具 —— 编造 + 说错状态，各计一次。"""
    victim = next(f"{c.id}-1" for c in cases_t175 if c.fixtures is None
                  and c.expect[0].route == T.ROUTE_ANSWER)
    report = run_eval_p13(_factory_t175(cases_t175, tweak=lambda m, e, f: {
        "reply": "您的订单已发货"} if m.msg_id == victim else None)[0], cases_t175)
    assert report.status_fabrication == 1 and report.wrong_status == 1
    assert [set(m.problems) for m in report.failures] == [{"status_fabrication", "wrong_status"}]


@pytest.mark.parametrize("tweak_fields,problems", [
    ({"write_obs": False}, {"status_fabrication", "wrong_status", "wording"}),   # 观察没落
    ({"claim": False}, {"status_fabrication", "wording"}),                       # 没挂 claim
    # 语种说错：第二道出门校验按本轮语种不过，wrong_status 也算（复核 L2-1 / L3-1：每轮都跑）
    ({"say_lang": P.LANG_EN}, {"wording", "wrong_status"}),
])
def test_wording_accuracy_can_fail_t175(cases_t175, tweak_fields, problems):
    case, turn = _say_turn_t175(cases_t175)
    victim = f"{case.id}-{turn}"
    report = run_eval_p13(_factory_t175(cases_t175, tweak=lambda m, e, f: tweak_fields
                                        if m.msg_id == victim else None)[0], cases_t175)
    assert report.wording_accuracy < 1.0
    assert not report.meets(load_thresholds(P13_EVAL_PATH))
    (miss,) = report.failures
    assert set(miss.problems) == problems, miss


def test_wording_needs_exactly_one_sentence_and_nothing_else_t175(cases_t175):
    """同一句说两遍、只挂一条观察：wording 判负；复核 L3R2-1 起「一条观察只撑一处」，第二处说的
    不是任何一条本轮观察 —— wrong_status 也判负（上一版这里期望 wrong_status == 0）。"""
    case, turn = _say_turn_t175(cases_t175)
    victim = f"{case.id}-{turn}"
    sentence = P.ORDER_STATUS_WORDING[P.LANG_ZH][case.expect[turn - 1].say]

    class Twice(FakeDeskT175):
        def handle(self, msg):
            res = super().handle(msg)
            if msg.msg_id != victim:
                return res
            text = res.reply_text + "；" + sentence
            return dataclasses.replace(res, reply_text=text,
                                       draft=dataclasses.replace(res.draft, text=text))
    answers = _answers_t175(cases_t175)
    report = run_eval_p13(lambda ports: Twice(answers, DEFAULT_TENANT_MAP, ports), cases_t175)
    assert report.wording_accuracy < 1.0 and report.wrong_status == 1
    assert report.status_fabrication == 0                  # check_reply 的 p12 口径：一条 claim 撑每一处
    assert [m.problems for m in report.failures] == [("wrong_status", "wording")]


def test_one_observation_backing_a_second_order_is_wrong_t175(cases_t175):
    """复核 L3R2-1 原样：退款桥轮真的查了这一单，却把措辞表那句说了两遍（第二遍替没查过的
    Z9999 说）—— 路由、编造、措辞都不掉，只有 wrong_status 判负，门槛不过。"""
    case, turn = _refund_bridge_turn_t175(cases_t175)
    victim = f"{case.id}-{turn}"
    order_no = ORDER_NO_RE_T175.findall(" ".join(case.turns[:turn]))[-1]
    truth = case.fixtures.order(case.fixtures.binding(order_no).query_key).status
    s = P.ORDER_STATUS_WORDING[P.LANG_ZH][truth]

    def run(suffix):
        return run_eval_p13(_factory_t175(cases_t175, tweak=lambda m, e, f: {
            "lookup_now": True, "say": truth, "obs_status": truth,
            "suffix": suffix} if m.msg_id == victim else None)[0], cases_t175)
    # 对照：只说一遍、与夹具一致 —— 三项都不判（BACKLOG task-t175 已记的口径缺口，p14 按期望判）
    assert run("").failures == ()
    report = run("；另一单 Z9999：" + s)
    assert report.wrong_status == 1 and report.status_fabrication == 0
    assert report.route_accuracy == report.handoff_recall == 1.0
    assert not report.meets(load_thresholds(P13_EVAL_PATH))
    (miss,) = report.failures
    assert (miss.case_id, miss.turn, miss.problems) == (case.id, turn, ("wrong_status",))


class LeakyDeskT175(FakeDeskT175):
    """复核 L2-2 原样：CS13-019 第 1 轮不经 verifier.resolve，自己造一条 T1919 的绑定去查、落观察、
    说出状态（T1919 在夹具 orders 里是 ok / shipped，但没绑在跑批客户名下）。"""

    victim = "CS13-019-1"

    def handle(self, msg):
        if msg.msg_id != self.victim:
            return super().handle(msg)
        self.seen.append(msg)
        tenant = self.tenant_map.get(msg.raw.get("open_kfid", ""), "")
        conv = T.conversation_id_for(tenant, msg.channel, msg.raw["open_kfid"], msg.chat_id)
        turn = T.turn_id_for(conv, len(self.seen))
        binding = P.Binding(tenant, msg.channel, msg.chat_id, "T1919", "demo-orders", "T1919",
                            P.BINDING_TEST)
        result = self.ports["lookup"].lookup(None, binding, plan_id=T.plan_id_for(conv),
                                             task_id=turn)
        sentence = P.ORDER_STATUS_WORDING[P.LANG_ZH][result.status]
        obs_id = _write_obs_t175(self._db, tenant=tenant, conv=conv, turn=turn, n=1,
                                 system_name=result.system_name, query_key=result.query_key,
                                 status=result.status)
        return T.DeskResult(
            reply_text=sentence, tenant_id=tenant, conversation_id=conv, turn_id=turn,
            route=T.ROUTE_ANSWER, intent=T.INTENT_LOGISTICS,
            draft=T.ReplyDraft(text=sentence, claims=(T.Claim(sentence, f"obs:{obs_id}"),)),
            lang=P.LANG_ZH, lookup_outcome=P.LOOKUP_OK)


def test_status_of_an_unbound_order_is_wrong_t175(cases_t175):
    """复核 L2-2：跳过身份核验、把别人那一单的状态说给客户 —— 观察撑得住、措辞对得上，
    wrong_status 的真值只认绑在跑批客户名下的单，判负；只丢一轮出口的 0.9 / 0.95 门槛挡不住它。"""
    case = next(c for c in cases_t175 if c.id == "CS13-019")
    assert case.fixtures.binding("T1919") is None                  # 夹具的前提：T1919 没绑
    assert case.fixtures.order("T1919").outcome == P.LOOKUP_OK     # 但查得到、状态是 ok
    answers = _answers_t175(cases_t175)
    report = run_eval_p13(lambda ports: LeakyDeskT175(answers, DEFAULT_TENANT_MAP, ports),
                          cases_t175)
    th = load_thresholds(P13_EVAL_PATH)
    assert report.wrong_status == 1 and report.status_fabrication == 0
    assert report.route_accuracy >= th["route_accuracy"]
    assert report.handoff_recall >= th["handoff_recall"]            # 只靠出口门槛拦不住
    assert not report.meets(th) and report.shortfalls(th) == ["wrong_status=1 > 0"]
    (miss,) = report.failures
    assert (miss.case_id, miss.turn, set(miss.problems)) == ("CS13-019", 1, {"route", "wrong_status"})


@pytest.mark.parametrize("backed", [False, True])
def test_wording_requires_no_other_status_t175(cases_t175, backed):
    """复核 L2-4：对的那句恰一次、挂着真观察，后面又说了另一个状态 —— wording 自己就得判负
    （不挂观察的那一版只有「没说别的状态」这一条拦得住）。"""
    case, turn = _say_turn_t175(cases_t175)
    victim = f"{case.id}-{turn}"
    truth = case.expect[turn - 1].say
    other = P.ORDER_STATUS_WORDING[P.LANG_ZH][next(
        s for s in ("paid", "shipped", "cancelled") if s != truth)]

    class Extra(FakeDeskT175):
        def handle(self, msg):
            res = super().handle(msg)
            if msg.msg_id != victim:
                return res
            text = res.reply_text + "；" + other
            claims = res.draft.claims + (
                (T.Claim(other, res.draft.claims[0].basis_ref),) if backed else ())
            return dataclasses.replace(res, reply_text=text, draft=dataclasses.replace(
                res.draft, text=text, claims=claims))
    answers = _answers_t175(cases_t175)
    report = run_eval_p13(lambda ports: Extra(answers, DEFAULT_TENANT_MAP, ports), cases_t175)
    assert report.wording_accuracy < 1.0 and report.wrong_status == 1
    assert not report.meets(load_thresholds(P13_EVAL_PATH))
    (miss,) = report.failures
    assert {"wording", "wrong_status"} <= set(miss.problems), miss


def _refund_bridge_turn_t175(cases) -> tuple[EvalCase, int]:
    return next((c, i) for c, i, _, e in _all_turns_t175(cases)
                if e.reason == T.HANDOFF_REFUND_REQUEST and e.lookup == P.LOOKUP_OK)


@pytest.mark.parametrize("reply,claimed", [
    ("您的订单已签收", True),                     # 复核 L2-1 原样：挂着本轮真观察
    ("您的订单已送达", True),
    ("Your order was delivered.", True),
    ("您的退款已到账", True),                     # 退款说法：对外字面值以外（foreign_literal）
    ("退款已到账", True),                         # 对外字面值本身：订单观察撑不起退款状态
    ("Your order ships tomorrow.", True),
    ("您的包裹在路上了", False),                  # 出门校验还不认的口语说法（评测侧认）
    ("您的订单明天就能到", False),
])
def test_off_table_status_on_a_refund_bridge_turn_is_wrong_t175(cases_t175, reply, claimed):
    """复核 L2-1 / L3-1（G2）：退款桥轮真的核验、查单、落了观察，回复说的却是措辞表以外的状态
    —— 路由全对、编造为 0，只有 wrong_status 判负，且门槛不过。"""
    case, turn = _refund_bridge_turn_t175(cases_t175)
    victim = f"{case.id}-{turn}"
    factory, _, got_ports = _factory_t175(cases_t175, tweak=lambda m, e, f: {
        "lookup_now": True, "reply": reply, "claim": claimed,
        "claim_literal": reply} if m.msg_id == victim else None)
    report = run_eval_p13(factory, cases_t175)
    ports = got_ports[[c.id for c in cases_t175].index(case.id)]
    assert len(ports["lookup"].calls) == 1                       # 真的经夹具端口查了这一单
    assert report.wrong_status == 1 and report.status_fabrication == 0
    assert report.route_accuracy == report.intent_accuracy == report.handoff_recall == 1.0
    assert not report.meets(load_thresholds(P13_EVAL_PATH))
    assert any("wrong_status" in s for s in report.shortfalls(load_thresholds(P13_EVAL_PATH)))
    (miss,) = report.failures
    assert (miss.case_id, miss.turn, miss.problems) == (case.id, turn, ("wrong_status",))


@pytest.mark.parametrize("lang,suffix", [
    (P.LANG_ZH, "，已经发了"),                    # 复核 L3-1（G3）原样
    (P.LANG_ZH, "，在路上了"),
    (P.LANG_ZH, "；您的订单已签收"),
    (P.LANG_EN, " It ships today."),
    (P.LANG_EN, " It will arrive in 2 days."),
])
def test_status_tacked_onto_the_wording_sentence_is_wrong_t175(cases_t175, lang, suffix):
    """复核 L3-1（G3）：paid 轮说对了措辞表那句（挂着真观察），后面又接一句别的状态。"""
    case, turn = next((c, i) for c, i, _, e in _all_turns_t175(cases_t175)
                      if e.say == P.ORDER_PAID and e.lang == lang)
    victim = f"{case.id}-{turn}"
    report = run_eval_p13(_factory_t175(cases_t175, tweak=lambda m, e, f: {
        "suffix": suffix} if m.msg_id == victim else None)[0], cases_t175)
    assert report.wrong_status == 1 and report.wording_accuracy < 1.0
    assert not report.meets(load_thresholds(P13_EVAL_PATH))
    (miss,) = report.failures
    assert {"wrong_status", "wording"} <= set(miss.problems), miss


@pytest.mark.parametrize("lang,suffix", [
    (P.LANG_ZH, "（您的包裹已妥投）"),                                    # 复核 L3R2-4 探针原样
    (P.LANG_ZH, "，钱已经退给您了"),
    (P.LANG_EN, " The courier picked it up and it has left our warehouse."),
    (P.LANG_EN, " You'll receive it tomorrow."),
])
def test_status_phrases_tacked_onto_every_policy_turn_fail_the_run_t175(cases_t175, lang, suffix):
    """复核 L3R2-4：每个不说状态、有回复的轮都接一句没有观察撑的状态说法 —— 出门校验与评测同口径认，
    编造与 wrong_status 各计每一轮，门槛不过（上一版 meets=True）。"""
    def tweak(m, e, f):
        if e.lang == lang and e.route != T.ROUTE_SILENT and not e.say:
            return {"suffix": suffix}
        return None
    victims = sum(1 for *_, e in _all_turns_t175(cases_t175)
                  if e.lang == lang and e.route != T.ROUTE_SILENT and not e.say)
    assert victims >= 3
    report = run_eval_p13(_factory_t175(cases_t175, tweak=tweak)[0], cases_t175)
    assert report.status_fabrication == report.wrong_status == victims
    assert not report.meets(load_thresholds(P13_EVAL_PATH))


def test_tenant_map_is_honoured_t175(cases_t175):
    """复核 L2-3：run_eval_p13 的 tenant_map 决定夹具核验的租户与引用 doc_id 的租户。"""
    th = load_thresholds(P13_EVAL_PATH)
    tmap = {DEFAULT_OPEN_KFID: "tnt-x"}
    factory, _, got_ports = _factory_t175(cases_t175, tenant_map=tmap)
    report = run_eval_p13(factory, cases_t175, tenant_map=tmap)
    assert _perfect_t175(report), report.describe()
    for case, ports in zip(cases_t175, got_ports):
        if case.fixtures is not None:
            assert ports["verifier"].tenant_id == tmap.get(case.effective_open_kfid, "")
    assert any(p["verifier"] is not None and p["verifier"].tenant_id == "tnt-x" for p in got_ports)
    # 前台按缺省映射（tnt-demo）答、评测按 tnt-x 判：核验对不上、引用对不上
    mismatched = run_eval_p13(_factory_t175(cases_t175)[0], cases_t175, tenant_map=tmap)
    assert mismatched.cite_accuracy < 1.0 and mismatched.wording_accuracy < 1.0
    assert not mismatched.meets(th)
    problems = {p for m in mismatched.failures for p in m.problems}
    assert {"cite", "wording"} <= problems, problems


def test_desk_without_store_cannot_back_any_status_t175(cases_t175):
    """前台没有 ``.store``：读不回观察 = 本轮没有观察（失败即关）。"""
    report = run_eval_p13(_factory_t175(cases_t175, with_store=False)[0], cases_t175)
    says = sum(1 for *_, e in _all_turns_t175(cases_t175) if e.say)
    assert report.status_fabrication == report.wrong_status == says
    assert report.wording_accuracy == 0.0
    assert not report.meets(load_thresholds(P13_EVAL_PATH))


def test_info_metrics_are_reported_but_gate_only_when_named_t175(cases_t175):
    th = load_thresholds(P13_EVAL_PATH)
    wrong_lang = run_eval_p13(_factory_t175(cases_t175, tweak=lambda m, e, f: {
        "lang": P.LANG_EN} if e.lang == P.LANG_ZH and e.route == T.ROUTE_FALLBACK else None)[0],
        cases_t175)
    assert wrong_lang.lang_accuracy < 1.0
    assert wrong_lang.meets(th)                                      # 门槛文件没点名：只报不拦
    assert not wrong_lang.meets({**th, "lang_accuracy": 1.0})
    wrong_lookup = run_eval_p13(_factory_t175(cases_t175, tweak=lambda m, e, f: {
        "lookup": P.LOOKUP_PLATFORM_ERROR} if e.lookup == P.LOOKUP_NOT_FOUND else None)[0],
        cases_t175)
    assert wrong_lookup.lookup_accuracy < 1.0 and wrong_lookup.meets(th)
    assert not wrong_lookup.meets({**th, "lookup_accuracy": 1.0})
    no_cite = run_eval_p13(_factory_t175(cases_t175, tweak=lambda m, e, f: {"citations": ()})[0],
                           cases_t175)
    assert no_cite.cite_accuracy == 0.0 and no_cite.meets(th)
    assert {m.problems for m in no_cite.failures} == {("cite",)}
    assert "wording_accuracy" in wrong_lang.describe().splitlines()[0]


@pytest.mark.parametrize("field,value", [
    ("say", "cancelled"),
    ("lookup", "platform_error"),
    ("route", "handoff"),
    ("lang", "en"),
])
def test_mutated_expectation_breaks_full_marks_t175(cases_t175, field, value):
    """判据的反向验证：期望在内存里改错一条，满分 FakeDesk（按原始期望作答）不再满分。"""
    factory, _, _ = _factory_t175(cases_t175)
    case, turn = next((c, i) for c, i, _, e in _all_turns_t175(cases_t175)
                      if e.say == "shipped" and e.lang == P.LANG_ZH)
    changes = {field: value}
    if field == "route":
        changes = {"route": T.ROUTE_HANDOFF, "reason": T.HANDOFF_LOOKUP_FAILED, "say": ""}
    changed = dataclasses.replace(case.expect[turn - 1], **changes)
    mutated = tuple(dataclasses.replace(
        c, expect=c.expect[:turn - 1] + (changed,) + c.expect[turn:]) if c is case else c
        for c in cases_t175)
    report = run_eval_p13(factory, mutated)
    assert not _perfect_t175(report)
    (miss,) = report.failures
    assert (miss.case_id, miss.turn) == (case.id, turn)
    metric = {"say": "wording_accuracy", "lookup": "lookup_accuracy", "route": "route_accuracy",
              "lang": "lang_accuracy"}[field]
    assert getattr(report, metric) < 1.0
    if field == "say":                               # wording 门槛是 1.0：错一轮就不达标
        assert not report.meets(load_thresholds(P13_EVAL_PATH))
    if field == "route":                             # 期望 handoff 的轮没转：召回也掉
        assert report.handoff_recall < 1.0


def test_run_eval_p13_rejects_malformed_cases_and_empty_runs_t175(cases_t175):
    built: list[object] = []

    def factory(ports):
        built.append(ports)
        raise AssertionError("形状不对时一个前台都不该造")
    exp = EvalExpect(route=T.ROUTE_ANSWER, intent=T.INTENT_GENERAL, lang="zh")
    for bad in ((EvalCase(id="X-1", turns=("你好", "在吗"), expect=(exp,)),),
                (EvalCase(id="D", turns=("你好",), expect=(exp,)),
                 EvalCase(id="D", turns=("你好",), expect=(exp,)))):
        with pytest.raises(ValueError):
            run_eval_p13(factory, bad)
    assert built == []
    empty = run_eval_p13(factory, [])
    assert empty.turns == 0 and not empty.meets(load_thresholds(P13_EVAL_PATH))


def test_desk_errors_are_recorded_not_raised_t175(cases_t175):
    victim = f"{cases_t175[0].id}-1"

    def tweak(m, e, f):
        if m.msg_id == victim:
            raise RuntimeError("desk exploded")
    report = run_eval_p13(_factory_t175(cases_t175, tweak=tweak)[0], cases_t175)
    assert [m.problems for m in report.failures] == [("error",)]
    assert report.turns == sum(len(c.turns) for c in cases_t175)


@pytest.mark.parametrize("bad_claim", [
    (P.ORDER_STATUS_WORDING[P.LANG_ZH]["shipped"], None),    # 复核 L2-4 的三种形状
    (123, "obs:x"),
    (P.ORDER_STATUS_WORDING[P.LANG_ZH]["shipped"], 5),
])
def test_malformed_claims_are_recorded_as_errors_t175(cases_t175, bad_claim):
    """claim 的 literal / basis_ref 不是 str：记该轮 error，不中断整批（以前在 try 外面抛、整批崩掉）。"""
    case, turn = _say_turn_t175(cases_t175)
    victim = f"{case.id}-{turn}"

    class BadClaims(FakeDeskT175):
        def handle(self, msg):
            res = super().handle(msg)
            if msg.msg_id != victim:
                return res
            return dataclasses.replace(res, draft=dataclasses.replace(
                res.draft, claims=(T.Claim(*bad_claim),)))
    answers = _answers_t175(cases_t175)
    report = run_eval_p13(lambda ports: BadClaims(answers, DEFAULT_TENANT_MAP, ports), cases_t175)
    assert report.turns == sum(len(c.turns) for c in cases_t175)
    (miss,) = report.failures
    assert (miss.case_id, miss.turn, miss.problems) == (case.id, turn, ("error",))
    assert "TypeError" in miss.actual["error"]


@pytest.mark.parametrize("literal,basis", [
    ("退款已到账啦", "kb:kb-cs-tnt-demo-PAY-003"),       # 以前的第 3 条（check_reply 报 foreign_literal）
    (P.ORDER_STATUS_WORDING[P.LANG_ZH]["cancelled"], "obs:csc-x-t0001-o99"),   # 第 2 条（悬空的 obs）
])
@pytest.mark.parametrize("in_text", [False, True])
def test_claims_the_customer_never_sees_are_not_wrong_status_t175(cases_t175, in_text, literal,
                                                                   basis):
    """复核 L2-3：wrong_status 只看客户收到的正文。状态说法挂在一条 literal 不在正文里的 claim 上 ——
    客户什么状态都没听到，不算；同一句真的出现在正文里，就是说了没有观察撑 / 措辞表以外的状态。"""
    victim = next(f"{c.id}-1" for c in cases_t175 if c.fixtures is None
                  and c.expect[0].route == T.ROUTE_ANSWER)

    class Stray(FakeDeskT175):
        def handle(self, msg):
            res = super().handle(msg)
            if msg.msg_id != victim:
                return res
            text = res.reply_text + (literal if in_text else "")
            return dataclasses.replace(res, reply_text=text, draft=dataclasses.replace(
                res.draft, text=text, claims=(T.Claim(literal, basis),)))
    answers = _answers_t175(cases_t175)
    report = run_eval_p13(lambda ports: Stray(answers, DEFAULT_TENANT_MAP, ports), cases_t175)
    if not in_text:
        assert report.wrong_status == 0 and report.status_fabrication == 0
        assert report.failures == ()
    else:
        assert report.wrong_status == 1 and report.status_fabrication == 1
        assert [set(m.problems) for m in report.failures] == [{"status_fabrication", "wrong_status"}]


def test_said_statuses_strips_the_normalised_table_sentences_t175(monkeypatch):
    """复核 L2-5：整句按正文同一种规范化（NFKC）去挖 —— 中文 paid / shipped 两句里的全角，（）
    会被折成半角，拿原句去找永远找不到。装一条命中句中片段（暂未发货 / 分批发出）的评测模式，
    说对的那句仍然只算它自己的状态。"""
    import maos.domain.cs.evaluate as E

    monkeypatch.setattr(E, "_OFF_TABLE_PATTERNS", E._OFF_TABLE_PATTERNS + (
        re.compile(r"暂未发货|分批发出|multi-item"),))
    for lang, table in P.ORDER_STATUS_WORDING.items():
        for status, sentence in table.items():
            assert said_statuses(sentence) == {status}, (lang, status)
            assert said_statuses(f"您好，{sentence}") == {status}, (lang, status)
    # 对照：同样的片段不在整句里，就是 off_table
    assert said_statuses("您的包裹暂未发货") == {STATUS_OFF_TABLE}


# ===========================================================================
# 小件：said_statuses、turn_observations、p12 跑批不变
# ===========================================================================
@pytest.mark.parametrize("text,said", [
    *[(s, {st}) for t in P.ORDER_STATUS_WORDING.values() for st, s in t.items()],
    ("您的订单已发货", {"shipped"}), ("订单已经取消了", {"cancelled"}), ("已为您支付", {"paid"}),
    ("Your order has shipped.", {"shipped"}), ("It was CANCELED.", {"cancelled"}),
    ("It is already paid.", {"paid"}),
    # 措辞表以外的说法：出门校验认的状态字眼没对上三种状态的（含英文否定句）记 off_table
    ("It has not shipped yet.", {STATUS_OFF_TABLE}), ("It hasn't shipped.", {STATUS_OFF_TABLE}),
    ("您的订单已签收", {STATUS_OFF_TABLE}), ("Your order was delivered.", {STATUS_OFF_TABLE}),
    ("您的退款已到账", {STATUS_OFF_TABLE}), ("退款已到账", {STATUS_OFF_TABLE}),
    ("Your order ships today.", {STATUS_OFF_TABLE}), ("预计3-5个工作日到账", {STATUS_OFF_TABLE}),
    ("已发货已签收", {"shipped", STATUS_OFF_TABLE}),
    ("Your order has been shipped.", {"shipped", STATUS_OFF_TABLE}),
    # 评测侧补认的中文口语说法（出门校验还不认）
    ("您的订单已经发了", {STATUS_OFF_TABLE}), ("包裹在路上了", {STATUS_OFF_TABLE}),
    ("快递正在派送中", {STATUS_OFF_TABLE}), ("明天就能到", {STATUS_OFF_TABLE}),
    ("退款成功", {STATUS_OFF_TABLE}), ("订单已被取消", {STATUS_OFF_TABLE}),
    # 复核 L3R2-4：出门校验新认的（评测经 reply_status_places 同口径认，记 off_table）
    ("您的包裹已妥投", {STATUS_OFF_TABLE}), ("快递已派件", {STATUS_OFF_TABLE}),
    ("您的订单已完成", {STATUS_OFF_TABLE}), ("您的货已经到了", {STATUS_OFF_TABLE}),
    ("钱已经退给您了", {STATUS_OFF_TABLE}), ("快递小哥已经在派送了", {STATUS_OFF_TABLE}),
    ("Your order has left our warehouse.", {STATUS_OFF_TABLE}),
    ("The courier picked it up this morning.", {STATUS_OFF_TABLE}),
    ("You'll receive it tomorrow.", {STATUS_OFF_TABLE}), ("It's with the courier now.", {STATUS_OFF_TABLE}),
    ("Your parcel was signed for at the door.", {STATUS_OFF_TABLE}),
    ("We received your payment.", {STATUS_OFF_TABLE}), ("Your order went out yesterday.", {STATUS_OFF_TABLE}),
    ("您的包裹尚未发货", set()), ("请在订单页完成付款", set()), ("", set()),
    ("物流由配送中心统一安排", set()), ("您的包裹发出去了吗？请提供单号", set()),
    (P.ORDER_STATUS_WORDING["zh"]["paid"] + "，" + P.ORDER_STATUS_WORDING["en"]["shipped"],
     {"paid", "shipped"}),
    (P.ORDER_STATUS_WORDING["zh"]["paid"] + "，已经发了", {"paid", STATUS_OFF_TABLE}),
])
def test_said_statuses_t175(text, said):
    assert said_statuses(text) == frozenset(said)


def test_policy_scripts_say_no_status_under_the_judge_t175():
    """话术库每篇 script 在评测的状态扫描下都什么状态都没说 —— 评测不冤枉正当的政策回答。"""
    from maos.domain.cs.corpus import load_corpus
    scripts = [json.loads(row["body"])["script"] for row in load_corpus()]
    assert len(scripts) >= 19
    for script in scripts:
        assert said_statuses(script) == frozenset(), script
    for reply in (REPLY_ZH_T175, REPLY_EN_T175):
        assert said_statuses(reply) == frozenset(), reply


def test_turn_observations_reads_only_this_turn_t175():
    store = _store_t175()
    conv = T.conversation_id_for("tnt-demo", T.CHANNEL_WECHAT_KF, "wk_eval", "eval-X")
    t1, t2 = T.turn_id_for(conv, 1), T.turn_id_for(conv, 2)
    kw = {"system_name": "demo-orders", "query_key": "A1", "status": "paid"}
    o1 = _write_obs_t175(store, tenant="tnt-demo", conv=conv, turn=t1, n=1, **kw)
    o2 = _write_obs_t175(store, tenant="tnt-demo", conv=conv, turn=t2, n=1, **kw)
    _write_obs_t175(store, tenant="tnt-other", conv=conv, turn=t2, n=2, **kw)
    got = turn_observations(store, tenant_id="tnt-demo", conversation_id=conv, turn_id=t2)
    assert set(got) == {o2} and got[o2]["status"] == "paid" and o1 not in got
    assert turn_observations(None, tenant_id="tnt-demo", conversation_id=conv, turn_id=t2) == {}
    bare = SqliteStore()
    bare.init_schema()                                    # 没有 cs_observation 这张表
    assert turn_observations(bare, tenant_id="tnt-demo", conversation_id=conv, turn_id=t2) == {}


def test_p12_run_eval_still_returns_the_p12_report_t175():
    """p12 的入口不变：run_eval 回的是 EvalReport（不是 p13 的），指标键集照旧。"""
    cases = load_cases(EVAL_PATH)
    assert all(c.fixtures is None for c in cases)
    assert all(not (e.lang or e.lookup or e.ask or e.say) for c in cases for e in c.expect)

    class Echo:
        def __init__(self):
            self.n = 0

        def handle(self, msg):
            self.n += 1
            return T.DeskResult(reply_text="好的", tenant_id="t", conversation_id="c",
                                turn_id=f"c-t{self.n:04d}", route=T.ROUTE_FALLBACK,
                                intent=T.INTENT_UNKNOWN, draft=T.ReplyDraft(text="好的"))
    report = run_eval(Echo, cases)
    assert type(report) is EvalReport
    assert set(report.metrics()) == {"intent_accuracy", "route_accuracy", "handoff_recall",
                                     "status_fabrication", "cite_accuracy"}
