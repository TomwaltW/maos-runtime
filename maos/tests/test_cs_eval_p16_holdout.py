"""p16 盲写留出集（T187，review/p16-cs-contracts.md §2 T187）的形状、覆盖地板、门槛与近重复棘轮。

PYTEST_DONT_REWRITE —— 关掉断言改写：失败时只出消息里的 case id 与聚合数，不回显留出句。

本文件**不跑真前台**：真前台读数由主会话整合期接上（契约 §5）。这里只钉：

* ``evaluate.load_cases`` 读得动，≥ 70 case、≥ 150 轮，全部 synthetic，至少一半轮是日常客服流量；
* 错别字地板：≥ 16 轮有意错别字，同音 / 形近 / 拼音首字母 / 漏字多字各 ≥ 3 轮（按 case 的 ``typos`` 逐轮
  标注计数，tags 带 ``typo`` 与 ``typo-<种类>`` 且与逐轮标注一致）；
* GAP_CLASSES_P16 九类各 ≥ 5 轮（按 case 的 ``gaps`` 逐轮标注计数；给了 cite 的轮，标注与 cite 必须对得上）；
* 查单各结果、追问与追问用尽、退款桥、英文、多轮、兜底 / repeated_fallback、触发词五类都有；
* 期望自洽：附录 A 触发词地板与优先级、转人工之后只剩 silent、连续兜底第二轮必是 repeated_fallback、
  fixtures.precheck 的键写 query_key（契约 §2 T187 夹具口径补丁；唯一例外是专门钉这条口径的 case）；
* ``_thresholds`` 与 ``test_cs_contract_p16.P16_PREREGISTERED_THRESHOLDS`` 逐字相等；
* 与两份开发集、话术库例句、四份旧留出集的近重复棘轮（归一后相等或一字之差即红；失败消息只报 id）。
"""

from __future__ import annotations

import json
import pathlib
import unicodedata

from maos.domain.cs import evaluate
from maos.domain.cs.ports import LOOKUP_OUTCOMES, ORDER_STATUS_WORDING
from maos.tests.test_cs_contract_p16 import P16_HOLDOUT_PATH, P16_PREREGISTERED_THRESHOLDS
from maos.tests.test_cs_eval_p12_t169 import GAP_CLASSES_P16

EVAL_DIR_T187 = evaluate.P13_EVAL_PATH.parent
KB_DIR_T187 = EVAL_DIR_T187.parent / "kb"
HOLDOUT_PATH_T187 = EVAL_DIR_T187 / "p16_holdout_cases.json"

TYPO_KINDS_T187 = ("homophone", "shape", "pinyin", "missing_extra")
MIN_CASES_T187 = 70
MIN_TURNS_T187 = 150
MIN_TYPO_TURNS_T187 = 16
MIN_TYPO_PER_KIND_T187 = 3
MIN_PER_GAP_CLASS_T187 = 5

#: 附录 A（p15 契约）的触发词地板：原因 → 子串；排除 → 不算触发的复合词。
TRIGGER_FLOOR_T187 = {
    "privacy": ("手机号", "电话号码", "身份证", "身份信息", "住址", "个人信息", "个人资料", "隐私",
                "银行卡号", "泄露", "泄漏"),
    "compensation": ("赔", "补偿", "损失费"),
    "anger": ("垃圾", "骗子", "骗人", "骗钱", "坑人", "坑爹", "黑店", "气死", "气炸", "恶心", "混蛋",
              "王八蛋", "无耻", "他妈", "妈的", "傻逼", "去死", "什么破", "破店", "滚", "!!", "！！"),
    "complaint": ("投诉", "12315", "消协", "消费者协会", "消保委", "工商局", "市场监管", "曝光", "起诉",
                  "律师", "法院", "告你们", "举报", "维权"),
    "requested": ("人工", "真人", "活人", "客服小姐姐", "客服小哥", "客服妹妹", "不想跟机器人",
                  "不要跟机器人", "经理", "主管", "负责人", "领导", "老板", "talk to a human",
                  "a real person", "an agent"),
}
TRIGGER_EXCLUDE_T187 = ("人工草坪", "赔本", "滚筒", "滚动")
PRIORITY_T187 = ("privacy", "compensation", "anger", "complaint", "requested")
TRIGGER_INTENT_T187 = {"privacy": "privacy", "compensation": "compensation", "anger": "complaint",
                       "complaint": "complaint", "requested": "handoff_request"}


def _doc_t187() -> dict:
    return json.loads(HOLDOUT_PATH_T187.read_text(encoding="utf-8"))


def _raw_cases_t187() -> list[dict]:
    return list(_doc_t187()["cases"])


def _cases_t187():
    return evaluate.load_cases(HOLDOUT_PATH_T187)


def _turn_labels_t187(key: str) -> list[tuple[str, int, str]]:
    """(case id, 轮序 0 起, 标注) —— 标注非空的轮。"""
    out = []
    for raw in _raw_cases_t187():
        for i, v in enumerate(raw.get(key) or ()):
            if v:
                out.append((raw["id"], i, v))
    return out


# ---------------------------------------------------------------------------
# 形状与规模
# ---------------------------------------------------------------------------
def test_holdout_loads_and_meets_size_floor_t187():
    assert HOLDOUT_PATH_T187 == P16_HOLDOUT_PATH, "路径与契约钉子不一致"
    cases = _cases_t187()
    turns = sum(len(c.turns) for c in cases)
    assert len(cases) >= MIN_CASES_T187, f"case 数 {len(cases)} < {MIN_CASES_T187}"
    assert turns >= MIN_TURNS_T187, f"轮数 {turns} < {MIN_TURNS_T187}"
    bad = [c.id for c in cases if not c.synthetic]
    assert not bad, f"非 synthetic 的 case：{bad}"
    assert all(c.id.startswith("CS16H-") for c in cases), "case id 前缀须为 CS16H-"
    prov = _doc_t187()["_provenance"]
    assert prov.get("synthetic") is True and prov.get("written_by") == "task-t187"


def test_per_turn_label_arrays_are_aligned_t187():
    bad = []
    for raw in _raw_cases_t187():
        n = len(raw["turns"])
        gaps, typos = raw.get("gaps"), raw.get("typos")
        if not isinstance(gaps, list) or len(gaps) != n:
            bad.append((raw["id"], "gaps"))
        elif any(g and g not in GAP_CLASSES_P16 for g in gaps):
            bad.append((raw["id"], "gaps-value"))
        if not isinstance(typos, list) or len(typos) != n:
            bad.append((raw["id"], "typos"))
        elif any(t and t not in TYPO_KINDS_T187 for t in typos):
            bad.append((raw["id"], "typos-value"))
    assert not bad, f"逐轮标注不齐：{bad}"


def test_thresholds_are_the_preregistered_ones_t187():
    assert _doc_t187()["_thresholds"] == P16_PREREGISTERED_THRESHOLDS, "_thresholds 与预登记不一致"
    assert evaluate.load_thresholds(HOLDOUT_PATH_T187) == {
        "intent_accuracy": 0.85, "route_accuracy": 0.85, "handoff_recall": 0.90,
        "status_fabrication_max": 0, "wording_accuracy": 1.0, "wrong_status_max": 0,
    }, "_thresholds 字面量被改"


def test_at_least_half_the_turns_are_everyday_traffic_t187():
    cases = _cases_t187()
    total = sum(len(c.turns) for c in cases)
    daily = sum(len(c.turns) for c in cases if "daily" in c.tags)
    assert daily * 2 >= total, f"日常流量 {daily}/{total} 不到一半"


# ---------------------------------------------------------------------------
# 错别字地板
# ---------------------------------------------------------------------------
def test_typo_floor_by_kind_t187():
    labels = _turn_labels_t187("typos")
    per_kind = {k: sum(1 for _, _, v in labels if v == k) for k in TYPO_KINDS_T187}
    assert len(labels) >= MIN_TYPO_TURNS_T187, f"错别字轮 {len(labels)} < {MIN_TYPO_TURNS_T187}"
    short = {k: n for k, n in per_kind.items() if n < MIN_TYPO_PER_KIND_T187}
    assert not short, f"错别字种类不足 {MIN_TYPO_PER_KIND_T187} 轮：{short}"


def test_typo_tags_match_per_turn_labels_t187():
    bad = []
    for raw in _raw_cases_t187():
        kinds = {t for t in raw["typos"] if t}
        tags = set(raw.get("tags") or ())
        want = ({"typo"} | {f"typo-{k}" for k in kinds}) if kinds else set()
        have = {t for t in tags if t == "typo" or t.startswith("typo-")}
        if have != want:
            bad.append(raw["id"])
    assert not bad, f"typo tag 与逐轮标注不一致：{bad}"


# ---------------------------------------------------------------------------
# 九类覆盖
# ---------------------------------------------------------------------------
def test_every_gap_class_has_five_turns_t187():
    labels = _turn_labels_t187("gaps")
    per = {g: sum(1 for _, _, v in labels if v == g) for g in GAP_CLASSES_P16}
    short = {g: n for g, n in per.items() if n < MIN_PER_GAP_CLASS_T187}
    assert not short, f"类别不足 {MIN_PER_GAP_CLASS_T187} 轮：{short}"


def test_gap_labels_agree_with_cites_t187():
    doc_class = {doc: g for g, docs in GAP_CLASSES_P16.items() for doc in docs}
    bad = []
    for raw, case in zip(_raw_cases_t187(), _cases_t187()):
        for i, exp in enumerate(case.expect):
            if exp.cite and raw["gaps"][i] != doc_class.get(exp.cite, ""):
                bad.append((case.id, i + 1))
    assert not bad, f"gaps 标注与 cite 对不上：{bad}"


# ---------------------------------------------------------------------------
# 流程覆盖
# ---------------------------------------------------------------------------
def _all_expects_t187():
    return [(c, i, e) for c in _cases_t187() for i, e in enumerate(c.expect)]


def test_lookup_outcomes_and_sayings_are_all_covered_t187():
    rows = _all_expects_t187()
    outcomes = {e.lookup for _, _, e in rows if e.lookup}
    missing = set(LOOKUP_OUTCOMES) - outcomes
    assert not missing, f"查单结果缺：{sorted(missing)}"
    for lang, table in ORDER_STATUS_WORDING.items():
        said = {e.say for _, _, e in rows if e.say and (e.lang or "zh") == lang}
        assert said == set(table), f"{lang} 的 say 三态不全：{sorted(said)}"


def test_handoff_reasons_and_routes_are_covered_t187():
    rows = _all_expects_t187()
    reasons = {e.reason for _, _, e in rows if e.reason}
    need = {"requested", "complaint", "anger", "compensation", "privacy", "needs_order_lookup",
            "repeated_fallback", "tenant_unmapped", "identity_unverified", "order_unmapped",
            "lookup_failed", "refund_request"}
    assert need <= reasons, f"转人工原因缺：{sorted(need - reasons)}"
    routes = {e.route for _, _, e in rows}
    assert routes == {"answer", "fallback", "handoff", "silent", "clarify"}, f"出口不全：{sorted(routes)}"
    en = sum(1 for _, _, e in rows if e.lang == "en")
    assert en >= 10, f"英文轮 {en} < 10"
    multi = sum(1 for c in _cases_t187() if len(c.turns) >= 2)
    assert multi >= 15, f"多轮 case {multi} < 15"
    fb = sum(1 for _, _, e in rows if e.route == "fallback")
    assert fb >= 5, f"兜底轮 {fb} < 5"


def test_clarify_exhaustion_and_refund_bridge_shapes_exist_t187():
    exhausted = refund_ok = refund_no = 0
    for case in _cases_t187():
        routes = [e.route for e in case.expect]
        for i in range(len(routes) - 2):
            e3 = case.expect[i + 2]
            if routes[i:i + 2] == ["clarify", "clarify"] and e3.reason == "needs_order_lookup":
                exhausted += 1
        for e in case.expect:
            if e.reason == "refund_request" and e.lookup == "ok":
                refund_ok += 1
            if e.reason == "needs_order_lookup" and e.lookup == "ok":
                refund_no += 1
    assert exhausted >= 2, f"追问用尽 case {exhausted} < 2"
    assert refund_ok >= 3 and refund_no >= 2, f"退款桥 ok={refund_ok} 拒={refund_no}"


# ---------------------------------------------------------------------------
# 期望自洽（照契约推，不照实现）
# ---------------------------------------------------------------------------
def _floor_reasons_t187(text: str) -> list[str]:
    t = text
    for ex in TRIGGER_EXCLUDE_T187:
        t = t.replace(ex, "")
    low = t.lower()
    return [r for r in PRIORITY_T187
            if any((w.lower() in low) if w.isascii() else (w in t) for w in TRIGGER_FLOOR_T187[r])]


def test_trigger_floor_and_priority_are_respected_t187():
    bad = []
    for case in _cases_t187():
        for i, (text, e) in enumerate(zip(case.turns, case.expect)):
            if e.route == "silent" or e.reason == "tenant_unmapped":
                continue
            hit = _floor_reasons_t187(text)
            if hit:
                if e.route != "handoff" or e.reason != hit[0] or e.intent != TRIGGER_INTENT_T187[hit[0]]:
                    bad.append((case.id, i + 1, "floor"))
            elif e.reason in TRIGGER_FLOOR_T187:
                bad.append((case.id, i + 1, "no-trigger"))
    assert not bad, f"触发词期望与附录 A 不一致：{bad}"


def test_after_handoff_only_silent_and_streak_rule_t187():
    bad = []
    for case in _cases_t187():
        handed = False
        streak = 0
        for i, e in enumerate(case.expect):
            if handed and e.route != "silent":
                bad.append((case.id, i + 1, "after-handoff"))
            if not handed and e.route == "silent":
                bad.append((case.id, i + 1, "silent-early"))
            # p12 §1.4 第 5 步：本轮兜底使 streak 达到 FALLBACK_STREAK_HANDOFF（2）就改走 repeated_fallback；
            # clarify / silent 不动 streak（p13 §1.1）。
            if e.reason == "repeated_fallback" and streak != 1:
                bad.append((case.id, i + 1, "repeated-without-streak"))
            if e.route == "fallback":
                streak += 1
                if streak >= 2:
                    bad.append((case.id, i + 1, "streak"))
            elif e.route in ("answer", "handoff"):
                streak = 0
            if e.route == "handoff":
                handed = True
    assert not bad, f"会话推进期望不自洽：{bad}"


def test_precheck_fixture_keys_are_query_keys_t187():
    bad = []
    for case in _cases_t187():
        fx = case.fixtures
        if fx is None:
            continue
        qks = {b.query_key for b in fx.bindings}
        for key, _ in fx.precheck:
            if key in qks:
                continue
            patch = "precheck_key_patch" in case.tags and all(
                e.reason in ("", "needs_order_lookup") for e in case.expect)
            if not patch:
                bad.append(case.id)
    assert not bad, f"fixtures.precheck 的键须写 query_key：{bad}"
    assert any("precheck_key_patch" in c.tags for c in _cases_t187()), "缺钉夹具口径补丁的 case"


# ---------------------------------------------------------------------------
# 近重复棘轮（只报 id，不回显句子）
# ---------------------------------------------------------------------------
def _norm_t187(text: str) -> str:
    t = unicodedata.normalize("NFKC", str(text)).lower()
    return "".join(ch for ch in t if ch.isalnum())


def _within_one_edit_t187(a: str, b: str) -> bool:
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


def _eval_turns_t187(path: pathlib.Path, label: str) -> list[tuple[str, str]]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    return [(f"{label}:{c.get('id', '?')}", t) for c in doc.get("cases") or ()
            for t in c.get("turns") or () if isinstance(t, str)]


def _strings_t187(node, path: str) -> list[tuple[str, str]]:
    if isinstance(node, str):
        return [(path, node)]
    if isinstance(node, dict):
        return [x for k, v in node.items() if not str(k).startswith("_")
                for x in _strings_t187(v, f"{path}/{k}")]
    if isinstance(node, list):
        return [x for i, v in enumerate(node) for x in _strings_t187(v, f"{path}[{i}]")]
    return []


def _script_examples_t187() -> list[tuple[str, str]]:
    doc = json.loads((KB_DIR_T187 / "cs_scripts.json").read_text(encoding="utf-8"))
    out = []
    for row in doc["kb_doc"]:
        body = json.loads(row["body"])
        for j, ex in enumerate(body.get("examples") or ()):
            out.append((f"kb:{body.get('scheme_no', row['doc_id'])}#{j}", ex))
    return out


def _reference_sentences_t187() -> list[tuple[str, str]]:
    refs: list[tuple[str, str]] = []
    for name in ("p12_cases.json", "p13_cases.json", "p12_holdout_cases.json",
                 "p14_holdout_cases.json", "p15_holdout_cases.json"):
        refs += _eval_turns_t187(EVAL_DIR_T187 / name, name.split(".")[0])
    kb_holdout = json.loads((KB_DIR_T187 / "cs_scripts_holdout.json").read_text(encoding="utf-8"))
    refs += _strings_t187(kb_holdout, "cs_scripts_holdout")
    refs += _script_examples_t187()
    return refs


def _near_dups_t187(mine, refs) -> list[tuple[str, int, str]]:
    normed = [(rid, _norm_t187(t)) for rid, t in refs]
    normed = [(rid, n) for rid, n in normed if n]
    hits = []
    for cid, i, text in mine:
        m = _norm_t187(text)
        for rid, n in normed:
            if _within_one_edit_t187(m, n):
                hits.append((cid, i + 1, rid))
    return hits


def test_reference_sets_are_really_loaded_t187():
    refs = _reference_sentences_t187()
    labels = {rid.split(":")[0].split("/")[0] for rid, _ in refs}
    for need in ("p12_cases", "p13_cases", "p12_holdout_cases", "p14_holdout_cases",
                 "p15_holdout_cases", "cs_scripts_holdout", "kb"):
        assert need in labels, f"参照集没读到：{need}"
    assert len(refs) >= 300, f"参照句只有 {len(refs)} 条，疑似空转"


def test_no_near_duplicates_of_dev_sets_examples_or_old_holdouts_t187():
    mine = [(c.id, i, t) for c in _cases_t187() for i, t in enumerate(c.turns)]
    hits = _near_dups_t187(mine, _reference_sentences_t187())
    assert not hits, f"近重复（归一后相等或一字之差）{len(hits)} 处：{hits}"


def test_no_near_duplicates_inside_the_set_t187():
    mine = [(c.id, i, t) for c in _cases_t187() for i, t in enumerate(c.turns)]
    hits = []
    for a in range(len(mine)):
        for b in range(a + 1, len(mine)):
            if _within_one_edit_t187(_norm_t187(mine[a][2]), _norm_t187(mine[b][2])):
                hits.append((mine[a][0], mine[a][1] + 1, mine[b][0], mine[b][1] + 1))
    assert not hits, f"集内近重复：{hits}"


def test_near_dup_detector_judges_negative_t187():
    # 反向：检测器对自造的一字之差 / 归一后相等必须判中，对两字之差不判。
    assert _within_one_edit_t187(_norm_t187("你好，请问 ABC"), _norm_t187("你好请问abc"))
    assert _within_one_edit_t187(_norm_t187("多久能发货"), _norm_t187("多久能发火"))
    assert _within_one_edit_t187(_norm_t187("多久能发货"), _norm_t187("多久发货"))
    assert not _within_one_edit_t187(_norm_t187("多久能发货"), _norm_t187("几天能发出"))
    fake = [("X", 0, "今天能发货吗")]
    assert _near_dups_t187(fake, [("ref:1", "今天能发货吗？")]) == [("X", 1, "ref:1")]
    assert _near_dups_t187(fake, [("ref:1", "明天可以发出吗")]) == []
