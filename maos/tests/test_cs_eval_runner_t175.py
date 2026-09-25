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


def test_expectations_follow_the_p13_judgment_order_t175(cases_t175):
    """按 p13 契约 §2 的判定顺序把每一轮的期望重推一遍（数据自洽的判据）。"""
    for case in cases_t175:
        tenant = DEFAULT_TENANT_MAP.get(case.effective_open_kfid, "")
        fx = case.fixtures
        handed_off, streak, asks = False, 0, 0
        order_no: str | None = None
        request: str | None = None
        for i, (text, exp) in enumerate(zip(case.turns, case.expect), start=1):
            where = (case.id, i, text)
            assert exp.lang == _detect_lang_t175(text), where                      # 0. 语种
            if handed_off:                                                         # 1.
                assert (exp.route, exp.intent) == (T.ROUTE_SILENT, T.INTENT_UNKNOWN), where
                continue
            assert exp.route != T.ROUTE_SILENT, where
            if not tenant:                                                         # 2.
                assert (exp.route, exp.reason, exp.intent) == (
                    T.ROUTE_HANDOFF, T.HANDOFF_TENANT_UNMAPPED, T.INTENT_UNKNOWN), where
                handed_off = True
                continue
            triggers = _triggers_in_t175(text)
            if triggers:                                                           # 3.
                top = triggers[0]
                assert (exp.route, exp.reason, exp.intent) == (
                    T.ROUTE_HANDOFF, top, INTENT_OF_TRIGGER_T175[top]), where
                assert not (exp.lookup or exp.say or exp.ask), where
                handed_off = True
                continue
            found = ORDER_NO_RE_T175.findall(text)                                 # 4. 槽位累积
            if found:
                order_no = found[-1]
            if RETURN_RE_T175.search(text):
                request = "return"
            elif TRACK_RE_T175.search(text):
                request = "track"
            order_flow = bool(exp.route == T.ROUTE_CLARIFY or exp.lookup
                              or exp.reason in P13_REASONS_T175
                              or (fx is not None and exp.reason == T.HANDOFF_NEEDS_ORDER_LOOKUP))
            if fx is None:                                                         # 5. p12 路径
                assert not order_flow or exp.reason == T.HANDOFF_NEEDS_ORDER_LOOKUP, where
                assert not (exp.lookup or exp.ask or exp.say), where
            elif order_flow:                                                       # 6. 查单链
                assert request is not None, where
                want_intent = T.INTENT_LOGISTICS if request == "track" else T.INTENT_RETURN_EXCHANGE
                assert exp.intent == want_intent, where
                if order_no is None:                                               # 6a
                    assert not (exp.lookup or exp.say), where
                    if asks >= P.MAX_ASKS_PER_SLOT:
                        assert (exp.route, exp.reason) == (
                            T.ROUTE_HANDOFF, T.HANDOFF_NEEDS_ORDER_LOOKUP), where
                    else:
                        assert (exp.route, exp.ask) == (T.ROUTE_CLARIFY, P.SLOT_ORDER_NO), where
                        asks += 1
                else:
                    binding = fx.binding(order_no)
                    if binding is None:                                            # 6b
                        assert (exp.route, exp.reason) == (
                            T.ROUTE_HANDOFF, T.HANDOFF_IDENTITY_UNVERIFIED), where
                        assert not exp.lookup, where                               # 不查单
                    else:                                                          # 6c
                        order = fx.order(binding.query_key)
                        outcome = order.outcome if order else P.LOOKUP_NOT_FOUND
                        assert exp.lookup == outcome, where
                        if outcome == P.LOOKUP_OK and request == "return":         # 6d
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
            if not order_flow or fx is None:                                       # 5 / 7. 话术
                assert exp.reason not in P13_REASONS_T175, where
                assert exp.reason not in TRIGGERS_T175, where
                assert exp.reason != T.HANDOFF_TENANT_UNMAPPED, where
                if fx is not None and exp.route == T.ROUTE_ANSWER:
                    assert not ORDER_NO_RE_T175.search(text), where                # 带单号的是查单
                if exp.route == T.ROUTE_FALLBACK:
                    streak += 1
                    assert streak < T.FALLBACK_STREAK_HANDOFF, where
                    assert exp.intent == T.INTENT_UNKNOWN, where
                    if fx is not None and found:
                        assert not (RETURN_RE_T175.search(text) or TRACK_RE_T175.search(text))
                elif exp.reason == T.HANDOFF_REPEATED_FALLBACK:
                    assert streak + 1 == T.FALLBACK_STREAK_HANDOFF, where
                    assert exp.intent == T.INTENT_UNKNOWN, where
                else:
                    assert exp.route == T.ROUTE_ANSWER or (
                        fx is None and exp.reason == T.HANDOFF_NEEDS_ORDER_LOOKUP), where
                    assert exp.intent not in SPECIAL_INTENTS_T175, where
            if exp.route in (T.ROUTE_ANSWER, T.ROUTE_HANDOFF):
                streak = 0                                   # clarify 与 silent 不动兜底计数
            if exp.route == T.ROUTE_HANDOFF:
                handed_off = True


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
    ``obs_status``（观察行记成什么状态）、``write_obs``、``claim``。
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
            "claim": True,
        }
        if self.tweak is not None:
            fields.update(self.tweak(msg, exp, fields) or {})
        claims: tuple[T.Claim, ...] = ()
        if fields["say"]:
            history = " ".join(m.text for m in self.seen)
            order_no = ORDER_NO_RE_T175.findall(history)[-1]
            binding = self.ports["verifier"].resolve(
                self.store if hasattr(self, "store") else None, tenant_id=tenant,
                channel=msg.channel, external_userid=msg.chat_id, display_no=order_no)
            result = self.ports["lookup"].lookup(None, binding, plan_id=T.plan_id_for(conv),
                                                 task_id=turn)
            sentence = P.ORDER_STATUS_WORDING[fields["say_lang"]][fields["say"]]
            fields["reply"] = sentence
            if fields["write_obs"]:
                obs_id = _write_obs_t175(self._db, tenant=tenant, conv=conv, turn=turn, n=1,
                                         system_name=result.system_name,
                                         query_key=result.query_key,
                                         status=fields["obs_status"] or result.status)
            else:
                obs_id = P.observation_id_for(turn, 1)
            if fields["claim"]:
                claims = (T.Claim(sentence, f"obs:{obs_id}"),)
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
    ({"say_lang": P.LANG_EN}, {"wording"}),                                      # 语种说错
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
    assert report.wording_accuracy < 1.0 and report.wrong_status == 0
    assert [m.problems for m in report.failures] == [("wording",)]


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


# ===========================================================================
# 小件：said_statuses、turn_observations、p12 跑批不变
# ===========================================================================
@pytest.mark.parametrize("text,said", [
    *[(s, {st}) for t in P.ORDER_STATUS_WORDING.values() for st, s in t.items()],
    ("您的订单已发货", {"shipped"}), ("订单已经取消了", {"cancelled"}), ("已为您支付", {"paid"}),
    ("Your order has shipped.", {"shipped"}), ("It was CANCELED.", {"cancelled"}),
    ("It is already paid.", {"paid"}),
    ("It has not shipped yet.", set()), ("It hasn't shipped.", set()),
    ("您的包裹尚未发货", set()), ("请在订单页完成付款", set()), ("", set()),
    (P.ORDER_STATUS_WORDING["zh"]["paid"] + "，" + P.ORDER_STATUS_WORDING["en"]["shipped"],
     {"paid", "shipped"}),
])
def test_said_statuses_t175(text, said):
    assert said_statuses(text) == frozenset(said)


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
