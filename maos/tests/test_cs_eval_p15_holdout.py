"""p15 盲写留出集（review/p15-cs-contracts.md §2 T183）的形状、覆盖地板、门槛与近重复棘轮。

PYTEST_DONT_REWRITE —— 本模块关掉 pytest 的断言改写：断言失败时不展开局部变量的 repr，
留出句一个字都不进失败输出。每条断言的失败消息只报 case id / 计数，永不报句子。

**不跑真前台**：真前台读数由主会话在 p15 整合期接上（契约 §5）。这里只钉：

* ``load_cases`` 读得动、全部 synthetic、门槛与预登记逐字相等；
* 覆盖地板（DECISIONS task-t183）：规模、日常流量占比、§1 各误判类别、查单各结果、追问与追问用尽、
  退款桥、触发词各原因、英文、话术库每篇非转人工话术的引用；
* 期望自洽：照 p12 契约 §1.4 / p13 契约 §2 与 p15 契约附录 A 的地板，逐轮复核出题人自己写的期望
  （转人工之后只许 silent、连续两轮兜底才 repeated_fallback、含地板说法的轮按优先级转人工……）；
* 近重复棘轮：与两份开发集、话术库例句、三份旧留出集的句子归一后相等或只差一个字即红。
"""

from __future__ import annotations

import json
import pathlib
import re
import unicodedata
from collections import Counter

from maos.domain.cs import evaluate
from maos.domain.cs.ports import LOOKUP_OUTCOMES, MAX_ASKS_PER_SLOT
from maos.domain.cs.types import FALLBACK_STREAK_HANDOFF

P15_HOLDOUT_PATH_T183 = evaluate.P13_EVAL_PATH.with_name("p15_holdout_cases.json")
_EVAL_DIR_T183 = evaluate.P13_EVAL_PATH.parent
_KB_DIR_T183 = _EVAL_DIR_T183.parent / "kb"

#: 预登记门槛（契约 §2 T183，一经提交不许改；与 test_cs_contract_p15 的钉子同值）。
PREREGISTERED_T183 = {
    "intent_accuracy": 0.85, "route_accuracy": 0.85, "handoff_recall": 0.90,
    "status_fabrication_max": 0, "wording_accuracy": 1.0, "wrong_status_max": 0,
}

#: p15 契约 §1 的误判类别（C5 主会话裁定不改，不设地板）。
CATEGORIES_T183 = ("C1", "C2", "C3", "C4", "C6", "C7", "C8", "C9", "C10")

#: 话术库里不带转人工标记的篇（p12 契约 §1.5 编号目录）：每篇至少两轮带 cite 的 answer。
ANSWERABLE_SCHEMES_T183 = ("LOG-001", "LOG-002", "LOG-003", "PAY-001", "PAY-002", "PAY-005",
                           "RET-001", "RET-002", "RET-003", "RET-004",
                           "GEN-001", "GEN-002", "GEN-003")

#: 触发原因 → 意图（p12 契约 §1.4 第 3 步）。
TRIGGER_INTENT_T183 = {"privacy": "privacy", "compensation": "compensation",
                       "anger": "complaint", "complaint": "complaint",
                       "requested": "handoff_request"}
PRIORITY_T183 = ("privacy", "compensation", "anger", "complaint", "requested")

#: p15 契约附录 A 的触发词地板（子串即中）。出题人自查用：含这些说法的轮期望必须是按优先级转人工。
_FLOOR_T183 = {
    "requested": ("人工", "真人", "活人", "客服小姐姐", "客服小哥", "客服妹妹", "不想跟机器人",
                  "不要跟机器人", "叫你们经理", "找你们经理", "换你们经理", "叫你们主管",
                  "找你们主管", "换你们主管", "叫你们负责人", "找你们负责人", "换你们负责人",
                  "叫你们领导", "找你们领导", "换你们领导", "叫你们老板", "找你们老板",
                  "换你们老板"),
    "complaint": ("投诉", "12315", "消协", "消费者协会", "消保委", "工商局", "市场监管", "曝光",
                  "起诉", "律师", "法院", "告你们", "举报", "维权"),
    "anger": ("垃圾", "骗子", "骗人", "骗钱", "坑人", "坑爹", "黑店", "气死", "气炸", "恶心", "混蛋",
              "王八蛋", "无耻", "他妈", "妈的", "傻逼", "去死", "什么破", "破店"),
    "compensation": ("赔", "补偿", "损失费", "精神损失"),
    "privacy": ("手机号", "电话号码", "身份证", "身份信息", "住址", "个人信息", "个人资料", "隐私",
                "银行卡号", "泄露", "泄漏"),
}
_FLOOR_EN_T183 = {"requested": ("human", "real person", "agent"),
                  "complaint": ("lawyer", "complaint")}
_FLOOR_EXEMPT_T183 = ("人工草坪", "赔本", "滚筒", "滚动")


def _doc_t183() -> dict:
    return json.loads(P15_HOLDOUT_PATH_T183.read_text(encoding="utf-8"))


def _cases_t183():
    return evaluate.load_cases(P15_HOLDOUT_PATH_T183)


def _turns_t183(cases):
    """(case, 1 起的轮号, 期望) 的扁平列表。"""
    return [(c, i, e) for c in cases for i, e in enumerate(c.expect, start=1)]


def _cats_t183(case) -> set[str]:
    return {t.split(":", 1)[1] for t in case.tags if t.startswith("cat:")}


def _floor_reason_t183(text: str) -> str:
    """一轮按附录 A 该转人工的原因（按优先级取）；不含地板说法返回空串。"""
    s = text
    for w in _FLOOR_EXEMPT_T183:
        s = s.replace(w, "")
    low = s.lower()
    hit = set()
    for reason, words in _FLOOR_T183.items():
        if any(w in s for w in words):
            hit.add(reason)
    for reason, words in _FLOOR_EN_T183.items():
        if any(re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", low) for w in words):
            hit.add(reason)
    if "滚" in s or re.search(r"[!！]{2,}", s):
        hit.add("anger")
    return next((r for r in PRIORITY_T183 if r in hit), "")


# ---------------------------------------------------------------------------
# 形状与门槛
# ---------------------------------------------------------------------------
def test_holdout_loads_and_is_synthetic_t183():
    cases = _cases_t183()
    doc = _doc_t183()
    assert doc["_provenance"]["synthetic"] is True, "provenance 必须标 synthetic"
    assert doc["_provenance"].get("blind") is True, "provenance 必须标 blind"
    not_synth = [c.id for c in cases if not c.synthetic]
    assert not not_synth, f"这些 case 没标 synthetic:true：{not_synth}"
    raw_flags = [c.get("id") for c in doc["cases"] if c.get("synthetic") is not True]
    assert not raw_flags, f"这些 case 的 synthetic 不是字面 true：{raw_flags}"


def test_thresholds_are_the_preregistered_ones_t183():
    doc = _doc_t183()
    assert doc["_thresholds"] == PREREGISTERED_T183, "_thresholds 与预登记门槛不逐字相等"
    from maos.tests.test_cs_contract_p15 import P15_PREREGISTERED_THRESHOLDS
    assert PREREGISTERED_T183 == P15_PREREGISTERED_THRESHOLDS, "本文件的门槛字面量与契约钉子不一致"


def test_size_floor_t183():
    cases = _cases_t183()
    turns = sum(len(c.turns) for c in cases)
    assert len(cases) >= 60, f"case 数 {len(cases)} < 60"
    assert turns >= 130, f"轮数 {turns} < 130"


# ---------------------------------------------------------------------------
# 覆盖地板
# ---------------------------------------------------------------------------
def test_daily_traffic_is_at_least_half_t183():
    cases = _cases_t183()
    total = sum(len(c.turns) for c in cases)
    daily = sum(len(c.turns) for c in cases if "daily" in c.tags and not _cats_t183(c))
    both = [c.id for c in cases if "daily" in c.tags and _cats_t183(c)]
    assert not both, f"daily 与 cat: 标签不许同挂：{both}"
    untagged = [c.id for c in cases if "daily" not in c.tags and not _cats_t183(c)]
    assert not untagged, f"每个 case 必须挂 daily 或 cat:Cn：{untagged}"
    assert daily * 2 >= total, f"日常流量 {daily}/{total} 轮，不到一半"


def test_each_misjudged_category_has_four_turns_t183():
    cases = _cases_t183()
    got = Counter()
    for c in cases:
        for cat in _cats_t183(c):
            got[cat] += 1
    unknown = sorted(set(got) - set(CATEGORIES_T183))
    assert not unknown, f"未知类别标签：{unknown}"
    short = {cat: got[cat] for cat in CATEGORIES_T183 if got[cat] < 4}
    assert not short, f"这些类别的 case（每个至少一轮该类）不足 4：{short}"


def test_lookup_and_flow_coverage_t183():
    rows = _turns_t183(_cases_t183())
    lookups = Counter(e.lookup for _, _, e in rows if e.lookup)
    short = {o: lookups[o] for o in LOOKUP_OUTCOMES if lookups[o] < 2}
    assert not short, f"查单结果覆盖不足 2 轮：{short}"
    says = Counter((e.lang or "zh", e.say) for _, _, e in rows if e.say)
    missing = [(lang, st) for lang in ("zh", "en") for st in ("paid", "shipped", "cancelled")
               if says[(lang, st)] < 1]
    assert not missing, f"ok 三态中英缺：{missing}"
    reasons = Counter(e.reason for _, _, e in rows if e.route == "handoff")
    floors = {"identity_unverified": 3, "refund_request": 3, "tenant_unmapped": 1,
              "repeated_fallback": 3, "order_unmapped": 2, "lookup_failed": 4,
              "needs_order_lookup": 8}
    floors.update({r: 2 for r in TRIGGER_INTENT_T183})
    short = {r: reasons[r] for r, n in floors.items() if reasons[r] < n}
    assert not short, f"转人工原因覆盖不足：{short}"
    refused = sum(1 for _, _, e in rows if e.route == "handoff"
                  and e.reason == "needs_order_lookup" and e.lookup == "ok")
    assert refused >= 2, f"退款桥被拒（查单 ok 后 needs_order_lookup）只有 {refused} 轮"
    routes = Counter(e.route for _, _, e in rows)
    assert routes["clarify"] >= 12, f"追问轮 {routes['clarify']} < 12"
    assert routes["silent"] >= 6, f"silent 轮 {routes['silent']} < 6"
    assert routes["fallback"] >= 6, f"fallback 轮 {routes['fallback']} < 6"
    en = sum(1 for _, _, e in rows if e.lang == "en")
    assert en >= 15, f"英文轮 {en} < 15"


def test_asks_exhausted_then_handoff_coverage_t183():
    exhausted = []
    for c in _cases_t183():
        routes = [e.route for e in c.expect]
        for i in range(MAX_ASKS_PER_SLOT, len(c.expect)):
            if (routes[i - MAX_ASKS_PER_SLOT:i] == ["clarify"] * MAX_ASKS_PER_SLOT
                    and c.expect[i].route == "handoff"
                    and c.expect[i].reason == "needs_order_lookup"):
                exhausted.append(c.id)
    assert len(exhausted) >= 4, f"追问用尽后转人工的 case 只有 {len(exhausted)} 个"


def test_policy_scripts_are_each_cited_t183():
    rows = _turns_t183(_cases_t183())
    cites = Counter(e.cite for _, _, e in rows if e.cite)
    short = {s: cites[s] for s in ANSWERABLE_SCHEMES_T183 if cites[s] < 2}
    assert not short, f"非转人工话术篇被引用不足 2 轮：{short}"
    per_intent = Counter(e.intent for _, _, e in rows if e.cite)
    short = {i: per_intent[i] for i in ("logistics", "refund_payment", "return_exchange", "general")
             if per_intent[i] < 3}
    assert not short, f"政策意图带引用的说法不足 3 种：{short}"
    p12_path = sum(1 for c in _cases_t183() if c.fixtures is None)
    p13_path = sum(1 for c in _cases_t183() if c.fixtures is not None)
    assert p12_path >= 20 and p13_path >= 20, f"两条路径 case 数 p12={p12_path} p13={p13_path}"


# ---------------------------------------------------------------------------
# 期望自洽（照契约，不照实现）
# ---------------------------------------------------------------------------
def test_expectations_follow_the_contract_order_t183():
    bad: list[str] = []
    for c in _cases_t183():
        handed = False
        streak = 0
        asks = 0
        for i, (text, e) in enumerate(zip(c.turns, c.expect), start=1):
            tag = f"{c.id}#{i}"
            if handed:
                if e.route != "silent" or e.intent != "unknown":
                    bad.append(tag + ":after_handoff")
                continue
            if e.route == "silent":
                bad.append(tag + ":silent_before_handoff")
            if c.effective_open_kfid not in evaluate.DEFAULT_TENANT_MAP:
                if (e.route, e.reason, e.intent) != ("handoff", "tenant_unmapped", "unknown"):
                    bad.append(tag + ":tenant")
            else:
                floor = _floor_reason_t183(text)
                if floor and (e.route, e.reason) != ("handoff", floor):
                    bad.append(tag + ":floor")
                if e.route == "handoff" and e.reason in TRIGGER_INTENT_T183:
                    if not floor or e.intent != TRIGGER_INTENT_T183[e.reason]:
                        bad.append(tag + ":trigger")
            if e.route == "clarify" and c.fixtures is None:
                bad.append(tag + ":clarify_without_ports")
            if e.reason in ("identity_unverified", "order_unmapped", "lookup_failed",
                            "refund_request") and c.fixtures is None:
                bad.append(tag + ":lookup_without_ports")
            if e.reason == "refund_request" and e.lookup != "ok":
                bad.append(tag + ":bridge_without_lookup")
            if e.route == "fallback":
                streak += 1
                if streak >= FALLBACK_STREAK_HANDOFF:
                    bad.append(tag + ":fallback_past_streak")
                if e.intent != "unknown":
                    bad.append(tag + ":fallback_intent")
            elif e.route == "handoff" and e.reason == "repeated_fallback":
                if streak + 1 != FALLBACK_STREAK_HANDOFF or e.intent != "unknown":
                    bad.append(tag + ":repeated_fallback")
            elif e.route in ("answer", "handoff"):
                streak = 0
            if e.route == "clarify":
                asks += 1
                if asks > MAX_ASKS_PER_SLOT:
                    bad.append(tag + ":too_many_asks")
            if e.route == "handoff":
                handed = True
    assert not bad, f"期望与契约判定顺序不自洽：{bad}"


def test_fixture_orders_back_the_expected_status_t183():
    """say 的状态必须就是本 case 夹具里某张绑定单的 ok 状态（出题人自查，防手滑）。"""
    bad = []
    for c in _cases_t183():
        if c.fixtures is None:
            continue
        truth = {o.status for b in c.fixtures.bindings
                 for o in [c.fixtures.order(b.query_key)] if o is not None and o.outcome == "ok"}
        for i, e in enumerate(c.expect, start=1):
            if e.say and e.say not in truth:
                bad.append(f"{c.id}#{i}")
    assert not bad, f"say 与夹具对不上：{bad}"


# ---------------------------------------------------------------------------
# 近重复棘轮（失败只报 id）
# ---------------------------------------------------------------------------
_KEEP_T183 = re.compile(r"[0-9a-z㐀-鿿]")


def _norm_t183(text: str) -> str:
    s = unicodedata.normalize("NFKC", text).casefold()
    return "".join(ch for ch in s if _KEEP_T183.match(ch))


def _within_one_t183(a: str, b: str) -> bool:
    """编辑距离 ≤ 1（相等、替换一字、增删一字）。"""
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
    return a[i + 1:] == b[i + 1:] if la == lb else a[i:] == b[i + 1:]


def _strings_t183(node) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, list):
        return [s for x in node for s in _strings_t183(x)]
    if isinstance(node, dict):
        return [s for k, v in node.items() if not str(k).startswith("_") for s in _strings_t183(v)]
    return []


def _case_turns_t183(path: pathlib.Path) -> list[str]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    return [t for c in doc.get("cases") or () for t in c.get("turns") or ()]


def _external_sentences_t183() -> dict[str, list[str]]:
    """来源名 → 句子（只在内存里比，永不输出）。"""
    out: dict[str, list[str]] = {}
    out["dev12"] = _case_turns_t183(_EVAL_DIR_T183 / "p12_cases.json")
    out["dev13"] = _case_turns_t183(_EVAL_DIR_T183 / "p13_cases.json")
    out["holdout12"] = _case_turns_t183(_EVAL_DIR_T183 / "p12_holdout_cases.json")
    out["holdout14"] = _case_turns_t183(_EVAL_DIR_T183 / "p14_holdout_cases.json")
    out["kb_holdout"] = _strings_t183(
        json.loads((_KB_DIR_T183 / "cs_scripts_holdout.json").read_text(encoding="utf-8")))
    kb = json.loads((_KB_DIR_T183 / "cs_scripts.json").read_text(encoding="utf-8"))
    out["kb_examples"] = [ex for row in kb["kb_doc"]
                          for ex in (json.loads(row["body"]).get("examples") or ())]
    return out


def _near_dups_t183(mine: list[tuple[str, str]], others: dict[str, list[str]]) -> list[str]:
    """mine = [(标签, 句子)]；返回撞车的「标签@来源」，不含句子。"""
    pool = {src: {_norm_t183(s) for s in texts if _norm_t183(s)} for src, texts in others.items()}
    hits = []
    for tag, text in mine:
        n = _norm_t183(text)
        for src, norms in pool.items():
            if any(_within_one_t183(n, o) for o in norms):
                hits.append(f"{tag}@{src}")
    return hits


def test_sources_for_the_ratchet_are_not_empty_t183():
    others = _external_sentences_t183()
    empty = [src for src, texts in others.items() if not texts]
    assert not empty, f"棘轮的对照来源读空了（守卫会空转）：{empty}"


def test_no_near_duplicates_of_dev_kb_or_old_holdouts_t183():
    mine = [(f"{c.id}#{i}", t) for c in _cases_t183() for i, t in enumerate(c.turns, start=1)]
    hits = _near_dups_t183(mine, _external_sentences_t183())
    assert not hits, f"与开发集 / 话术库例句 / 旧留出集近重复：{hits}"


def test_no_near_duplicates_inside_the_set_t183():
    mine = [(f"{c.id}#{i}", _norm_t183(t)) for c in _cases_t183()
            for i, t in enumerate(c.turns, start=1)]
    seen: dict[str, str] = {}
    dups = []
    for tag, n in mine:
        if n in seen:
            dups.append(f"{tag}={seen[n]}")
        else:
            seen[n] = tag
    assert not dups, f"留出集内部句子重复：{dups}"


def test_near_dup_detector_bites_t183():
    """棘轮自身能判负：相等、只差一个字、只差标点 / 大小写都算撞车；差两个字不算。"""
    base = "退货要满足什么条件"
    assert _near_dups_t183([("x", base)], {"s": [base]}) == ["x@s"]
    assert _near_dups_t183([("x", "退货要满足啥条件")], {"s": ["退货要满足么条件"]}) == ["x@s"]
    assert _near_dups_t183([("x", "退货要满足什么条件吗")], {"s": [base]}) == ["x@s"]
    assert _near_dups_t183([("x", "Order #A1  ok？")], {"s": ["order a1 OK"]}) == ["x@s"]
    assert _near_dups_t183([("x", "退货要满足啥条件呀")], {"s": [base]}) == []


# ---------------------------------------------------------------------------
# 真前台（整合期 p15 主会话接上，2026-09-25 首跑 = 唯一一次验收读数）
# ---------------------------------------------------------------------------
#: 首跑实测（integrate/p15 合入四轨之后，夹具端口、零模型）：intent 148/161 = 0.9193 ✓、
#: route 140/161 = 0.8696 ✓、handoff 48/51 = 0.9412 ✓、编造 0 ✓、错状态 0 ✓、措辞 ≈0.8947 ✗（1.0；两轮没说出
#: 该说的状态，落了兜底，不是说错）、零「自信答错」（route=answer 的轮意图全对；有一轮引错篇，见 DECISIONS
#: integrate-p15）。读过之后本集不再是盲的，这里只钉安全不变量与首跑地板。
MEASURED_P15_HOLDOUT = {"turns": 161, "intent_hits": 148, "route_hits": 140,
                        "handoff_expected": 51, "handoff_caught": 48}


def _real_desk_factory_p15h(ports):
    from maos.core.store import SqliteStore
    from maos.domain.cs.corpus import seed_cs_kb
    from maos.domain.cs.desk import CsConfig, FrontDesk

    store = SqliteStore(":memory:")
    seed_cs_kb(store)
    return FrontDesk(store, CsConfig(tenants={"wk_eval": "tnt-demo"}, handoff_target=None),
                     **ports)


def _aggregate_only_p15h(r) -> str:
    ids = sorted({f"{m.case_id}#{m.turn}" for m in r.failures})
    return (f"intent {r.intent_hits}/{r.turns} route {r.route_hits}/{r.turns} "
            f"handoff {r.handoff_caught}/{r.handoff_expected} fabrication {r.status_fabrication} "
            f"wrong_status {r.wrong_status}; 没对上的轮（只列 id）：{', '.join(ids)}")


def test_real_desk_p15_holdout_safety_and_floor_p15h():
    r = evaluate.run_eval_p13(_real_desk_factory_p15h, _cases_t183())
    assert r.turns == MEASURED_P15_HOLDOUT["turns"]
    assert r.status_fabrication == 0 and r.wrong_status == 0, _aggregate_only_p15h(r)
    confident_wrong = [f"{m.case_id}#{m.turn}" for m in r.failures
                       if m.actual.get("route") == "answer" and "intent" in m.problems]
    assert not confident_wrong, "真前台自信答错（只列 id）：" + ", ".join(confident_wrong)
    assert r.handoff_expected == MEASURED_P15_HOLDOUT["handoff_expected"], _aggregate_only_p15h(r)
    assert r.intent_hits >= MEASURED_P15_HOLDOUT["intent_hits"], _aggregate_only_p15h(r)
    assert r.route_hits >= MEASURED_P15_HOLDOUT["route_hits"], _aggregate_only_p15h(r)
    assert r.handoff_caught >= MEASURED_P15_HOLDOUT["handoff_caught"], _aggregate_only_p15h(r)
