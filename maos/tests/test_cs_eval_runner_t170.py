"""T170 · 评测集（scenarios/cs/eval/p12_cases.json）的形状、覆盖下限与跑批器。

两半：

* **数据**：形状照契约 §1.6 冻结的那份；覆盖下限逐项断言；另外按契约 §1.4 的一轮判定
  顺序把整份期望**重推一遍**（已转人工 → silent、租户映射不到、触发词优先级、连续兜底）——
  期望值是按顺序推出来的，不是拍脑袋写的，这条让写错的期望当场红。
* **跑批器**：用按期望作答的 FakeDesk 跑出满分；故意答错一类，对应指标掉、meets 为 False；
  在内存里把一条期望改错，满分 FakeDesk 不再满分（判据的反向验证）；回一句编造状态，
  status_fabrication 计 1。

本文件不 import 前台 / 会话表 / 检索的实现（T167–T169）：前台是替身。
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import re

import pytest

from maos.domain.cs import types as T
from maos.domain.cs.evaluate import (
    DEFAULT_OPEN_KFID,
    DEFAULT_TENANT_MAP,
    EVAL_PATH,
    EvalCase,
    EvalExpect,
    EvalReport,
    cite_doc_id,
    load_cases,
    load_document,
    load_thresholds,
    run_eval,
)
from maos.ingress.contracts import CHANNEL_WECHAT_KF, InboundMessage

ROOT_T170 = pathlib.Path(__file__).resolve().parents[2]

#: 契约 §1.5 的编号目录（冻结）：编号 → (intent, handoff 标记)。
CATALOG_T170: dict[str, tuple[str, str]] = {
    "LOG-001": ("logistics", ""),
    "LOG-002": ("logistics", ""),
    "LOG-003": ("logistics", ""),
    "LOG-004": ("logistics", "needs_order_lookup"),
    "LOG-005": ("logistics", "needs_order_lookup"),
    "LOG-006": ("logistics", "needs_order_lookup"),
    "PAY-001": ("refund_payment", ""),
    "PAY-002": ("refund_payment", ""),
    "PAY-003": ("refund_payment", "needs_order_lookup"),
    "PAY-004": ("refund_payment", "needs_order_lookup"),
    "PAY-005": ("refund_payment", ""),
    "RET-001": ("return_exchange", ""),
    "RET-002": ("return_exchange", ""),
    "RET-003": ("return_exchange", ""),
    "RET-004": ("return_exchange", ""),
    "RET-005": ("return_exchange", "needs_order_lookup"),
    "GEN-001": ("general", ""),
    "GEN-002": ("general", ""),
    "GEN-003": ("general", ""),
}

#: 契约 §1.4 触发词最低覆盖表（T169 的词表必须认这些）。
TRIGGERS_T170: dict[str, tuple[str, ...]] = {
    "requested": ("转人工", "人工客服", "找人工", "真人"),
    "complaint": ("投诉", "12315", "消协", "曝光", "起诉", "律师"),
    "anger": ("垃圾", "骗子", "气死", "滚"),
    "compensation": ("赔偿", "补偿", "赔钱", "赔我"),
    "privacy": ("手机号", "身份证", "住址", "个人信息", "隐私"),
}
BANGS_T170 = re.compile(r"[!！]{3,}")

#: 触发词优先级（高 → 低）与各自的意图。
PRIORITY_T170 = ("privacy", "compensation", "anger", "complaint", "requested")
INTENT_OF_TRIGGER_T170 = {"privacy": "privacy", "compensation": "compensation",
                          "anger": "complaint", "complaint": "complaint",
                          "requested": "handoff_request"}

FABRICATED_T170 = "您的退款已到账，预计3天送达"


def _triggers_in_t170(text: str) -> list[str]:
    found = [cat for cat, words in TRIGGERS_T170.items() if any(w in text for w in words)]
    if BANGS_T170.search(text) and "anger" not in found:
        found.append("anger")
    return [cat for cat in PRIORITY_T170 if cat in found]


def _all_turns_t170(cases):
    for case in cases:
        for i, (text, exp) in enumerate(zip(case.turns, case.expect), start=1):
            yield case, i, text, exp


@pytest.fixture(scope="module")
def doc_t170():
    return load_document(EVAL_PATH)


@pytest.fixture(scope="module")
def cases_t170():
    return load_cases(EVAL_PATH)


# ===========================================================================
# 数据：形状
# ===========================================================================
def test_eval_path_points_into_scenarios_t170():
    assert EVAL_PATH == ROOT_T170 / "scenarios" / "cs" / "eval" / "p12_cases.json"
    assert EVAL_PATH.is_file()


def test_top_level_shape_and_provenance_t170(doc_t170):
    assert set(doc_t170) == {"_note", "_provenance", "_thresholds", "cases"}
    prov = doc_t170["_provenance"]
    assert prov["synthetic"] is True and prov["written_by"] == "task-t170"
    basis = prov["basis"]
    assert "合成" in basis and "§7" in basis and "不是 ADP 3.3" in basis
    assert doc_t170["_thresholds"] == {"intent_accuracy": 1.0, "route_accuracy": 1.0,
                                       "handoff_recall": 1.0, "status_fabrication_max": 0}
    assert load_thresholds(EVAL_PATH) == doc_t170["_thresholds"]


def test_case_shape_t170(doc_t170, cases_t170):
    raw_cases = doc_t170["cases"]
    assert len(raw_cases) == len(cases_t170)
    ids = [c["id"] for c in raw_cases]
    assert len(ids) == len(set(ids))
    for raw in raw_cases:
        assert set(raw) <= {"id", "synthetic", "tags", "turns", "expect", "open_kfid"}, raw["id"]
        assert {"id", "synthetic", "tags", "turns", "expect"} <= set(raw), raw["id"]
        assert re.fullmatch(r"CS12-\d{3}", raw["id"]), raw["id"]
        assert raw["synthetic"] is True, raw["id"]
        assert len(raw["turns"]) == len(raw["expect"]) >= 1, raw["id"]
        for exp in raw["expect"]:
            assert {"route", "intent"} <= set(exp) <= {"route", "intent", "reason", "cite"}
            assert exp["route"] in T.ROUTES and exp["intent"] in T.INTENTS
            if exp["route"] == T.ROUTE_HANDOFF:
                assert exp["reason"] in T.HANDOFF_REASONS, raw["id"]
            else:
                assert "reason" not in exp, raw["id"]
            if "cite" in exp:
                assert exp["cite"] in CATALOG_T170, raw["id"]


def test_load_cases_rejects_malformed_files_t170(tmp_path):
    def write(cases):
        p = tmp_path / "c.json"
        p.write_text(json.dumps({"cases": cases}, ensure_ascii=False), encoding="utf-8")
        return p

    good = {"id": "X-1", "synthetic": True, "tags": [], "turns": ["你好"],
            "expect": [{"route": "answer", "intent": "general"}]}
    assert len(load_cases(write([good]))) == 1
    for bad in (
        {**good, "turns": ["你好", "在吗"]},                                   # 不等长
        {**good, "expect": [{"route": "handoff", "intent": "general"}]},      # handoff 缺 reason
        {**good, "expect": [{"route": "answer", "intent": "general", "reason": "anger"}]},
        {**good, "expect": [{"route": "reply", "intent": "general"}]},        # 未知 route
        {**good, "expect": [{"route": "answer", "intent": "chitchat"}]},      # 未知 intent
        {**good, "expect": [{"route": "answer", "intent": "general", "cite": "kb-cs-x"}]},
    ):
        with pytest.raises(ValueError):
            load_cases(write([bad]))
    with pytest.raises(ValueError):
        load_cases(write([good, good]))                                       # id 重复
    with pytest.raises(ValueError):
        load_cases(write([]))


# ===========================================================================
# 数据：覆盖下限（契约 §1.6 逐项）
# ===========================================================================
def test_at_least_forty_turns_t170(cases_t170):
    assert sum(len(c.turns) for c in cases_t170) >= 40


def test_every_scheme_number_is_covered_t170(cases_t170):
    cites = {exp.cite for _, _, _, exp in _all_turns_t170(cases_t170) if exp.cite}
    tagged = {t for c in cases_t170 for t in c.tags if t in CATALOG_T170}
    assert len(CATALOG_T170) == 19
    assert set(CATALOG_T170) <= cites | tagged, sorted(set(CATALOG_T170) - cites - tagged)
    # 不带 handoff 标记的编号：必须至少被 cite 钉过一次（answer 轮的引用要对上那一篇）
    plain = {no for no, (_, h) in CATALOG_T170.items() if not h}
    assert plain <= cites, sorted(plain - cites)


def test_cites_are_consistent_with_catalog_t170(cases_t170):
    for case, i, _, exp in _all_turns_t170(cases_t170):
        if exp.cite:
            intent, handoff = CATALOG_T170[exp.cite]
            assert exp.route == T.ROUTE_ANSWER and not handoff, (case.id, i)
            assert exp.intent == intent, (case.id, i)
            assert exp.cite in case.tags, (case.id, i)
    # 带 handoff 标记的编号只在 tags 里：那个 case 必有一轮 needs_order_lookup、意图对得上
    for case in cases_t170:
        for no in (t for t in case.tags if t in CATALOG_T170 and CATALOG_T170[t][1]):
            intent, reason = CATALOG_T170[no]
            assert any(e.route == T.ROUTE_HANDOFF and e.reason == reason and e.intent == intent
                       for e in case.expect), (case.id, no)


def test_each_trigger_category_at_least_twice_t170(cases_t170):
    counts = {cat: 0 for cat in PRIORITY_T170}
    for _, _, text, exp in _all_turns_t170(cases_t170):
        if exp.route == T.ROUTE_HANDOFF and exp.reason in counts:
            assert exp.reason in _triggers_in_t170(text), text
            counts[exp.reason] += 1
    assert all(n >= 2 for n in counts.values()), counts


def test_needs_order_lookup_at_least_four_t170(cases_t170):
    n = sum(1 for _, _, _, e in _all_turns_t170(cases_t170)
            if e.route == T.ROUTE_HANDOFF and e.reason == T.HANDOFF_NEEDS_ORDER_LOOKUP)
    assert n >= 4, n


def test_repeated_fallback_two_turn_case_exists_t170(cases_t170):
    def hit(case: EvalCase) -> bool:
        return any(a.route == T.ROUTE_FALLBACK and b.route == T.ROUTE_HANDOFF
                   and b.reason == T.HANDOFF_REPEATED_FALLBACK
                   for a, b in zip(case.expect, case.expect[1:]))
    assert any(hit(c) for c in cases_t170)


def test_silent_after_handoff_case_exists_t170(cases_t170):
    def hit(case: EvalCase) -> bool:
        return any(a.route == T.ROUTE_HANDOFF and b.route == T.ROUTE_SILENT
                   for a, b in zip(case.expect, case.expect[1:]))
    assert any(hit(c) for c in cases_t170)


def test_tenant_unmapped_case_exists_t170(cases_t170):
    unmapped = [c for c in cases_t170 if c.open_kfid and c.open_kfid not in DEFAULT_TENANT_MAP]
    assert unmapped
    for case in unmapped:
        assert case.expect[0].route == T.ROUTE_HANDOFF
        assert case.expect[0].reason == T.HANDOFF_TENANT_UNMAPPED


def test_english_sentence_expects_fallback_unknown_t170(cases_t170):
    cjk = re.compile(r"[一-鿿]")
    english = [(t, e) for _, _, t, e in _all_turns_t170(cases_t170)
               if not cjk.search(t) and re.search(r"[A-Za-z]{3,}", t)]
    assert english
    for _, exp in english:
        assert (exp.route, exp.intent) == (T.ROUTE_FALLBACK, T.INTENT_UNKNOWN)


def test_multi_intent_turns_follow_priority_t170(cases_t170):
    multi = [(t, e) for _, _, t, e in _all_turns_t170(cases_t170)
             if e.route != T.ROUTE_SILENT and len(_triggers_in_t170(t)) >= 2]
    assert len(multi) >= 5, multi
    pairs = {tuple(_triggers_in_t170(t)[:2]) for t, _ in multi}
    assert len(pairs) >= 4, pairs                     # 覆盖多种组合，不是同一对重复
    for text, exp in multi:
        top = _triggers_in_t170(text)[0]
        assert exp.reason == top and exp.intent == INTENT_OF_TRIGGER_T170[top], text


def test_colloquial_and_typo_cases_exist_t170(cases_t170):
    tags = {t for c in cases_t170 for t in c.tags}
    assert {"colloquial", "typo"} <= tags


def test_expectations_follow_the_judgment_order_t170(cases_t170):
    """按契约 §1.4 的判定顺序把每一轮的期望重推一遍（数据自洽的判据）。"""
    for case in cases_t170:
        stage_handed_off = False
        streak = 0
        tenant = DEFAULT_TENANT_MAP.get(case.effective_open_kfid, "")
        for i, (text, exp) in enumerate(zip(case.turns, case.expect), start=1):
            where = (case.id, i, text)
            triggers = _triggers_in_t170(text)
            if stage_handed_off:
                assert (exp.route, exp.intent) == (T.ROUTE_SILENT, T.INTENT_UNKNOWN), where
                continue
            assert exp.route != T.ROUTE_SILENT, where
            if not tenant:
                assert (exp.route, exp.reason, exp.intent) == (
                    T.ROUTE_HANDOFF, T.HANDOFF_TENANT_UNMAPPED, T.INTENT_UNKNOWN), where
            elif triggers:
                top = triggers[0]
                assert (exp.route, exp.reason, exp.intent) == (
                    T.ROUTE_HANDOFF, top, INTENT_OF_TRIGGER_T170[top]), where
            else:
                assert exp.reason not in PRIORITY_T170, where
                assert exp.reason != T.HANDOFF_TENANT_UNMAPPED, where
                if exp.route == T.ROUTE_FALLBACK:
                    streak += 1
                    assert streak < T.FALLBACK_STREAK_HANDOFF, where
                    assert exp.intent == T.INTENT_UNKNOWN, where
                elif exp.reason == T.HANDOFF_REPEATED_FALLBACK:
                    assert streak + 1 == T.FALLBACK_STREAK_HANDOFF, where
                    assert exp.intent == T.INTENT_UNKNOWN, where
                else:
                    assert exp.route == T.ROUTE_ANSWER or (
                        exp.reason == T.HANDOFF_NEEDS_ORDER_LOOKUP), where
                    assert exp.intent not in (T.INTENT_UNKNOWN, T.INTENT_HANDOFF_REQUEST,
                                              T.INTENT_COMPLAINT, T.INTENT_COMPENSATION,
                                              T.INTENT_PRIVACY), where
            if exp.route in (T.ROUTE_ANSWER, T.ROUTE_HANDOFF):
                streak = 0
            if exp.route == T.ROUTE_HANDOFF:
                stage_handed_off = True


CORPUS_PATH_T170 = ROOT_T170 / "scenarios" / "cs" / "kb" / "cs_scripts.json"


def corpus_examples_t170(path: pathlib.Path) -> set[str]:
    """话术库文件里所有 ``examples`` 数组的条目。body 是 JSON 串时剥一层再找。"""
    examples: set[str] = set()

    def walk(node):
        if isinstance(node, str):
            s = node.strip()
            if s.startswith("{") or s.startswith("["):
                try:
                    walk(json.loads(s))
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

    walk(json.loads(pathlib.Path(path).read_text(encoding="utf-8")))
    return examples


def copied_sentences_t170(cases, examples: set[str]) -> list[str]:
    return sorted({t.strip() for _, _, t, _ in _all_turns_t170(cases)} & examples)


def test_eval_sentences_do_not_copy_corpus_examples_t170(cases_t170):
    """话术库的 examples 不许与评测句重合（契约 §1.6；W-A 合流后话术库才在）。"""
    if not CORPUS_PATH_T170.is_file():
        pytest.skip("话术库还没合入（T168）；合流后这条生效")
    examples = corpus_examples_t170(CORPUS_PATH_T170)
    assert examples, "话术库里一条 examples 都没读到 —— 形状变了，这条判据空转"
    copied = copied_sentences_t170(cases_t170, examples)
    assert copied == [], copied


def test_copy_detector_catches_a_copied_example_t170(tmp_path, cases_t170):
    """上一条的反向验证：造一份 kb_doc 行形状的假话术库，body 里抄一句评测句。"""
    stolen = cases_t170[0].turns[0]
    rows = [{"doc_id": "kb-cs-tnt-demo-LOG-001", "kind": "cs_script",
             "body": json.dumps({"scheme_no": "LOG-001", "examples": ["别的说法", stolen]},
                                ensure_ascii=False)}]
    fake = tmp_path / "cs_scripts.json"
    fake.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    examples = corpus_examples_t170(fake)
    assert examples == {"别的说法", stolen}
    assert copied_sentences_t170(cases_t170, examples) == [stolen]
    fake.write_text(json.dumps({"docs": rows[:0]}), encoding="utf-8")
    assert corpus_examples_t170(fake) == set()


# ===========================================================================
# 跑批器
# ===========================================================================
class FakeDeskT170:
    """按期望作答的前台替身。``tweak(msg, exp) -> dict | None`` 可以改写某一轮的作答。"""

    def __init__(self, answers: dict[str, EvalExpect], tenant_map, tweak=None):
        self.answers = answers
        self.tenant_map = dict(tenant_map)
        self.tweak = tweak
        self.seen: list[InboundMessage] = []

    def handle(self, msg: InboundMessage) -> T.DeskResult:
        self.seen.append(msg)
        exp = self.answers[msg.msg_id]
        open_kfid = msg.raw.get("open_kfid", "")
        tenant = self.tenant_map.get(open_kfid, "")
        conv = T.conversation_id_for(tenant, msg.channel, open_kfid, msg.chat_id)
        turn = T.turn_id_for(conv, len(self.seen))
        fields = {
            "route": exp.route, "intent": exp.intent, "reason": exp.reason,
            "reply": "" if exp.route == T.ROUTE_SILENT else "好的，请参考我们的服务说明。",
            "citations": (cite_doc_id(tenant, exp.cite),) if exp.cite else (),
        }
        if self.tweak is not None:
            fields.update(self.tweak(msg, exp) or {})
        if fields.get("raise"):
            raise RuntimeError("desk exploded")
        return T.DeskResult(
            reply_text=fields["reply"], tenant_id=tenant, conversation_id=conv, turn_id=turn,
            route=fields["route"], intent=fields["intent"],
            draft=T.ReplyDraft(text=fields["reply"], citations=tuple(fields["citations"])),
            handoff_reason=fields["reason"])


def _answers_t170(cases) -> dict[str, EvalExpect]:
    return {f"{c.id}-{i}": e for c in cases for i, e in enumerate(c.expect, start=1)}


def _factory_t170(cases, *, tweak=None, tenant_map=DEFAULT_TENANT_MAP):
    desks: list[FakeDeskT170] = []
    answers = _answers_t170(cases)

    def factory():
        desk = FakeDeskT170(answers, tenant_map, tweak)
        desks.append(desk)
        return desk
    return factory, desks


def _perfect_t170(report: EvalReport) -> bool:
    return (report.intent_accuracy == report.route_accuracy == report.handoff_recall
            == report.cite_accuracy == 1.0 and report.status_fabrication == 0
            and report.failures == ())


def test_perfect_desk_scores_full_marks_t170(cases_t170):
    factory, desks = _factory_t170(cases_t170)
    report = run_eval(factory, cases_t170)
    assert _perfect_t170(report), report.describe()
    assert report.meets(load_thresholds())
    assert report.cases == len(cases_t170)
    assert report.turns == sum(len(c.turns) for c in cases_t170)
    assert report.handoff_expected >= 1 and report.cite_expected >= 1
    # 每个 case 一个新前台；消息形状照契约
    assert len(desks) == len(cases_t170)
    for case, desk in zip(cases_t170, desks):
        assert len(desk.seen) == len(case.turns)
        for i, msg in enumerate(desk.seen, start=1):
            assert msg.channel == CHANNEL_WECHAT_KF == T.CHANNEL_WECHAT_KF
            assert msg.chat_id == msg.sender == f"eval-{case.id}"
            assert msg.msg_id == f"{case.id}-{i}"
            assert msg.text == case.turns[i - 1]
            assert msg.raw == {"open_kfid": case.open_kfid or DEFAULT_OPEN_KFID}


def test_desk_that_never_hands_off_fails_recall_t170(cases_t170):
    def tweak(msg, exp):
        if exp.route == T.ROUTE_HANDOFF:
            return {"route": T.ROUTE_FALLBACK, "reason": ""}
        return None
    report = run_eval(_factory_t170(cases_t170, tweak=tweak)[0], cases_t170)
    assert report.handoff_recall == 0.0
    assert report.route_accuracy < 1.0
    assert report.intent_accuracy == 1.0
    assert report.meets(load_thresholds()) is False
    assert any("handoff_recall" in s for s in report.shortfalls(load_thresholds()))
    assert all("route" in m.problems for m in report.failures)
    assert len(report.failures) == report.handoff_expected


def test_handoff_with_wrong_reason_counts_against_route_not_recall_t170(cases_t170):
    def tweak(msg, exp):
        if exp.route == T.ROUTE_HANDOFF and exp.reason != T.HANDOFF_REQUESTED:
            return {"reason": T.HANDOFF_REQUESTED}
        return None
    report = run_eval(_factory_t170(cases_t170, tweak=tweak)[0], cases_t170)
    assert report.handoff_recall == 1.0
    assert report.route_accuracy < 1.0
    assert report.meets(load_thresholds()) is False
    miss = report.failures[0]
    assert miss.problems == ("route",)
    assert miss.actual["reason"] == T.HANDOFF_REQUESTED != miss.expected["reason"]


def test_desk_with_wrong_intents_fails_intent_only_t170(cases_t170):
    def tweak(msg, exp):
        if exp.intent == T.INTENT_LOGISTICS:
            return {"intent": T.INTENT_GENERAL}
        return None
    report = run_eval(_factory_t170(cases_t170, tweak=tweak)[0], cases_t170)
    assert report.intent_accuracy < 1.0
    assert report.route_accuracy == report.handoff_recall == report.cite_accuracy == 1.0
    assert report.meets(load_thresholds()) is False
    assert {m.problems for m in report.failures} == {("intent",)}


def test_desk_without_citations_fails_cite_t170(cases_t170):
    report = run_eval(_factory_t170(cases_t170, tweak=lambda m, e: {"citations": ()})[0],
                      cases_t170)
    assert report.cite_accuracy == 0.0
    assert report.intent_accuracy == report.route_accuracy == report.handoff_recall == 1.0
    assert report.meets(load_thresholds()) is False
    assert {m.problems for m in report.failures} == {("cite",)}


def test_cite_doc_id_follows_tenant_map_t170(cases_t170):
    other = {DEFAULT_OPEN_KFID: "tnt-other"}
    # 前台按缺省租户引用，跑批按另一个租户解析 → 引用对不上
    report = run_eval(_factory_t170(cases_t170)[0], cases_t170, tenant_map=other)
    assert report.cite_accuracy == 0.0
    # 两边同一张映射 → 满分
    report = run_eval(_factory_t170(cases_t170, tenant_map=other)[0], cases_t170,
                      tenant_map=other)
    assert _perfect_t170(report), report.describe()
    assert cite_doc_id("tnt-demo", "LOG-001") == "kb-cs-tnt-demo-LOG-001"


@pytest.mark.parametrize("field,value", [
    ("route", T.ROUTE_FALLBACK),
    ("intent", T.INTENT_UNKNOWN),
    ("cite", "LOG-002"),
])
def test_mutated_expectation_breaks_full_marks_t170(cases_t170, field, value):
    """判据 1 的反向验证：期望改错一条（内存里改），满分 FakeDesk 的报告不再满分。"""
    factory, _ = _factory_t170(cases_t170)          # 替身按原始期望作答
    target = next(c for c in cases_t170 if c.expect[0].cite == "LOG-001")
    changed = dataclasses.replace(target.expect[0], **{field: value})
    mutated = tuple(
        dataclasses.replace(c, expect=(changed,) + c.expect[1:]) if c is target else c
        for c in cases_t170)
    report = run_eval(factory, mutated)
    assert not _perfect_t170(report)
    assert report.meets(load_thresholds()) is False
    assert len(report.failures) == 1
    miss = report.failures[0]
    assert (miss.case_id, miss.turn, miss.text) == (target.id, 1, target.turns[0])
    assert miss.expected[field] == value
    if field == "route":
        assert miss.actual["route"] == T.ROUTE_ANSWER and "route" in miss.problems
    elif field == "intent":
        assert miss.actual["intent"] == T.INTENT_LOGISTICS and miss.problems == ("intent",)
    else:
        assert miss.problems == ("cite",)
        assert miss.actual["citations"] == ["kb-cs-tnt-demo-LOG-001"]


def test_mutated_handoff_reason_breaks_full_marks_t170(cases_t170):
    factory, _ = _factory_t170(cases_t170)
    target = next(c for c in cases_t170
                  if c.expect[0].reason == T.HANDOFF_NEEDS_ORDER_LOOKUP)
    changed = dataclasses.replace(target.expect[0], reason=T.HANDOFF_COMPLAINT)
    mutated = tuple(dataclasses.replace(c, expect=(changed,) + c.expect[1:])
                    if c is target else c for c in cases_t170)
    report = run_eval(factory, mutated)
    assert report.route_accuracy < 1.0 and report.handoff_recall == 1.0
    assert report.meets(load_thresholds()) is False


def test_fabricated_status_reply_counts_once_t170(cases_t170):
    victim = f"{cases_t170[0].id}-1"

    def tweak(msg, exp):
        return {"reply": FABRICATED_T170} if msg.msg_id == victim else None
    report = run_eval(_factory_t170(cases_t170, tweak=tweak)[0], cases_t170)
    assert report.status_fabrication == 1
    assert report.intent_accuracy == report.route_accuracy == report.handoff_recall == 1.0
    assert report.meets(load_thresholds()) is False
    assert any("status_fabrication" in s for s in report.shortfalls(load_thresholds()))
    assert [m.problems for m in report.failures] == [("status_fabrication",)]
    # 策略话术（没有状态字眼）不算编造
    clean = run_eval(_factory_t170(
        cases_t170, tweak=lambda m, e: {"reply": "退款会按原支付方式退回，以支付渠道为准"}
        if e.route != T.ROUTE_SILENT else None)[0], cases_t170)
    assert clean.status_fabrication == 0


def test_desk_exception_is_recorded_not_raised_t170(cases_t170):
    victim = f"{cases_t170[1].id}-1"
    report = run_eval(_factory_t170(
        cases_t170, tweak=lambda m, e: {"raise": True} if m.msg_id == victim else None)[0],
        cases_t170)
    assert report.turns == sum(len(c.turns) for c in cases_t170)
    assert [m.problems for m in report.failures] == [("error",)]
    assert "RuntimeError" in report.failures[0].actual["error"]
    assert report.meets(load_thresholds()) is False


def test_meets_honours_given_thresholds_t170(cases_t170):
    def tweak(msg, exp):
        return {"intent": T.INTENT_UNKNOWN} if msg.msg_id == f"{cases_t170[0].id}-1" else None
    report = run_eval(_factory_t170(cases_t170, tweak=tweak)[0], cases_t170)
    assert report.meets(load_thresholds()) is False
    assert report.meets({**load_thresholds(), "intent_accuracy": 0.9}) is True
    assert report.meets({**load_thresholds(), "intent_accuracy": 0.9,
                         "cite_accuracy": 1.0}) is True
    assert "intent_accuracy" in report.describe().splitlines()[0]
    assert f"{cases_t170[0].id}#1" in report.describe()
