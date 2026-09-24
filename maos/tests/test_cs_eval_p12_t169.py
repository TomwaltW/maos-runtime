"""T169 · 真前台端到端跑 p12 评测集（契约 §1.6 / §5 W-B）。

每个 case 一个新前台：新的 ``SqliteStore(':memory:')`` + 灌话术库 + ``FrontDesk``（客服账号
``wk_eval`` 映射到 ``tnt-demo``，不配转人工投递目标）。用 T170 的 ``evaluate.run_eval`` 跑
``scenarios/cs/eval/p12_cases.json``，断言报告达到文件里写的 ``_thresholds``；没达标时
把逐轮明细打进断言消息。

## p12_cases.json 现在是开发集（主会话裁定，2026-09-24）

复核两轮（L1-1）把本轨照着评测句补的说法全部撤回之后，干净的语料下差三轮
（CS12-014#1 催发货、CS12-044#1 问退款几天、CS12-045#1 带客套的问候）。这份评测集既然已经
被拿来诊断，就不再当留出集用：定性为**开发集**，另有一份盲写的留出集在别处编写。裁定允许
针对这三类**通用**缺口给话术库补说法 —— 每一条都得是「没见过评测集的话术作者也会写」的
自然说法，不许抄评测句、不许「评测句换一个字」—— 并把 T169 原先的两字词干判据（按设计就是
禁止「从评测失败里学」，与裁定冲突）换成 ≥ 5 字连续片段判据。

integrate-p12-evalfix 按三类补了 23 条（:data:`ADDITION_REASONS_T169` 逐条写了类别与理由）：

* CS12-014#1「…帮我催一下发货」：LOG-004 从 0.179 到 0.263（门槛 0.25），转人工查单 —— 对上；
* CS12-045#1「您好，打扰问一下」：GEN-001 从 0.156 到 0.454 —— 对上；
* CS12-044#1「钱退回来一般要几天呐」：PAY-001 从 0.415 到 0.621，仍低于 PAY-003 的 0.723
  （T168 写的「钱退回来没」）—— **没对上，按裁定不硬补**。两句共享的实词二元组是「钱退 / 退回 /
  回来 / 几天」，把「问时长」与「问到没到」分开的「一般 / 要几 / 来没」全是虚词二元组（权重 0.1）；
  最自然的对照说法「钱退回来要多久」也只有 0.701，要翻过来只能写「钱退回来 + 要几天」这种
  评测句换一两个字的说法。要改的是重排（``maos/domain/cs/scripts.py``，不归本轨），见
  docs/BACKLOG.md 的 integrate-p12-evalfix 小节。

所以满分那条仍标 ``xfail(strict=True)`` —— 哪天真跑满了会 XPASS 变红，逼着把标记摘掉；
剩下的一轮由 :func:`test_eval_gaps_are_exactly_the_known_ones_t169` 钉住，别处退一步就红。

## 话术库与评测集的独立：三道棘轮 + 一道补充

1. 片段棘轮（复核一轮）：任何一条同义词 / 例句规整后 ≥ 4 字的，不许是评测句的一段
   （基线就有的 12 条除外）。
2. 增补棘轮（复核二轮）：话术库的说法集合 = a4935cd 基线（按指纹钉）∪ 显式声明的增补
   :data:`ADDED_VARIANTS_T169`；没声明就补的说法（哪怕无害）指纹对不上就红。
3. 理由表（开发集裁定）：声明的每一条都在 :data:`ADDITION_REASONS_T169` 里写了针对的通用缺口
   类别（只许裁定点名的三类、且只许补给该类对应的那一篇）与理由。
4. 长片段判据（开发集裁定，替换原两字词干判据）：声明的每一条规整后不许含评测句里任何
   :data:`EVAL_FRAGMENT_CHARS_T169`（5）个字的连续片段 —— 抄句、截段、评测句前后加字都判得出。
   补充一道一字之差判据（本轨自加，见 docs/DECISIONS.md）：规整后 ≥ 5 字的补充，与任何评测句
   的某一段编辑距离不许 ≤ 1 —— 短评测句（CS12-045 规整后 7 个字）中间换一个字，每个 5 字窗口
   都盖住了那个字，长片段判据看不见。
"""

from __future__ import annotations

import hashlib
import json

import pytest

from maos import kb
from maos.core.store import SqliteStore
from maos.domain.cs import corpus, evaluate
from maos.domain.cs.corpus import seed_cs_kb
from maos.domain.cs.desk import CsConfig, FrontDesk

#: 干净语料 + 声明增补下仍跑不到的轮：(case_id, 第几轮) → 没对上的指标。改动它要回主会话。
#: CS12-044#1 是 PAY-001 / PAY-003 的歧义，要改重排（见模块头），不在话术库补。
KNOWN_GAPS_T169 = {
    ("CS12-044", 1): ("route", "cite"),
}

#: 话术库里本来就与评测句重合的说法（T168 在评测集之前写的、a4935cd 基线就有；
#: 都是常见短说法，不是从评测句里截的）。本轨之后再多出一条就红 —— 防的是照着评测句补语料。
BASELINE_OVERLAPS_T169 = frozenset({
    ("GEN-002", "明白了谢谢你"), ("LOG-001", "几天发货"), ("LOG-004", "快递到哪了"),
    ("LOG-005", "地址填错了"), ("PAY-002", "花呗分期"), ("PAY-003", "钱到没到"),
    ("PAY-004", "扣了两次钱"), ("PAY-005", "增值税发票"), ("PAY-005", "发票抬头"),
    ("RET-001", "七天无理由"), ("RET-001", "无理由退货"), ("RET-003", "换个新的"),
})
#: 多长的说法算「截下来的一段」（规整后的字数）。
OVERLAP_MIN_CHARS_T169 = 4

#: a4935cd（W-A 合流）话术库全部 (编号, 同义词或例句) 的条数与指纹（排序后 JSON 的 sha256）。
#: 这两个数只在主会话批准改语料（比如删 T168 的某条说法）时跟着改。
BASELINE_VARIANT_COUNT_T169 = 297
BASELINE_VARIANT_SHA256_T169 = "5b06851f016a3e96fb2d4cd755e757686f95e218ffbabf157ba8a3372fd4fd9b"

#: 主会话裁定（2026-09-24）点名的三类通用缺口 → 只许补给的那一篇。
GAP_CLASSES_T169 = {
    "催促发货": "LOG-004",      # 催单 / 催发货 / 帮我催催 / 怎么还不发
    "客套开场": "GEN-001",      # 打扰一下 / 麻烦问一下 / 在吗 / 请问
    "问退款时长": "PAY-001",    # 要多久 / 几天 / 多长时间才能退回（问规则；问某一笔到没到是 PAY-003）
}

#: 本轨在基线之上补的每一条说法 (编号, 说法) → (针对的通用缺口类别, 理由)。
#: 理由写的是「一个没见过评测集的话术作者为什么也会写这条」。
ADDITION_REASONS_T169: dict[tuple[str, str], tuple[str, str]] = {
    # ---- LOG-004 催促发货（同义词）
    ("LOG-004", "催单"): ("催促发货", "电商客服里最常见的两字说法，催的是自己那一单，要查单"),
    ("LOG-004", "催一催"): ("催促发货", "「催」的叠用口语，与基线「催发货」同义"),
    ("LOG-004", "帮我催催"): ("催促发货", "裁定点名；「帮我 + 动词叠用」是客户求助的常见口语"),
    ("LOG-004", "怎么还不发"): ("催促发货", "裁定点名；基线「怎么还没发货」的省字说法"),
    ("LOG-004", "能不能快点发"): ("催促发货", "催促的委婉问法，问的是自己那一单何时发"),
    # ---- LOG-004 催促发货（例句：口语、语气词）
    ("LOG-004", "急着用能帮我催催吗"): ("催促发货", "催单常带理由（急用），句尾带语气词"),
    ("LOG-004", "拍了好几天了怎么还不发啊"): ("催促发货", "「拍了」是电商口语的下单，带等待时长与语气词"),
    ("LOG-004", "麻烦给我催一催单呗"): ("催促发货", "客套词 + 催单，句尾语气词「呗」"),
    # ---- GEN-001 客套开场（同义词）
    ("GEN-001", "请问"): ("客套开场", "裁定点名；开场最常见的客套词，单独一句就是在打招呼"),
    ("GEN-001", "打扰了"): ("客套开场", "裁定点名的「打扰」一类里最常见的完整说法"),
    ("GEN-001", "麻烦问一下"): ("客套开场", "裁定点名；先客套、问题放下一句的开场"),
    ("GEN-001", "不好意思打扰了"): ("客套开场", "带致歉的客套开场，客户常用"),
    # ---- GEN-001 客套开场（例句：口语、语气词）
    ("GEN-001", "打扰啦亲"): ("客套开场", "「打扰了」的口语形，带电商称呼「亲」"),
    ("GEN-001", "请问有人在吗"): ("客套开场", "「请问」接基线已有的「有人吗 / 在吗」，确认有人值守"),
    ("GEN-001", "麻烦问下哈"): ("客套开场", "「麻烦问一下」的口语省字形，带语气词"),
    ("GEN-001", "不好意思想咨询一下呀"): ("客套开场", "致歉 + 咨询的开场，问题还没说出口"),
    # ---- PAY-001 问退款时长（同义词）
    ("PAY-001", "退款要多长时间"): ("问退款时长", "问时长最直白的说法，问的是规则、不指某一笔"),
    ("PAY-001", "钱多久能退回来"): ("问退款时长", "口语把退款说成「钱退回来」，问多久"),
    ("PAY-001", "多长时间才能退回"): ("问退款时长", "裁定点名"),
    ("PAY-001", "退回来要多久"): ("问退款时长", "与 PAY-003 问到没到的说法对照：这条问的是要多久"),
    # ---- PAY-001 问退款时长（例句：口语、语气词）
    ("PAY-001", "退款大概要多长时间啊"): ("问退款时长", "带「大概」的估时问法与语气词"),
    ("PAY-001", "申请退款以后多久能退回来呀"): ("问退款时长", "问申请之后的一般周期，不问某一笔的进度"),
    ("PAY-001", "退的钱多长时间能回到账上呢"): ("问退款时长", "问到账时长的口语说法"),
}

#: 本轨在基线之上补的说法 (编号, 说法)，= 理由表的键。
ADDED_VARIANTS_T169: frozenset[tuple[str, str]] = frozenset(ADDITION_REASONS_T169)

#: 声明的补充与评测句共享的连续片段多长算「照着评测句写」（规整后的字数）。
EVAL_FRAGMENT_CHARS_T169 = 5
#: 一字之差判据只看规整后至少这么长的补充：四个字以内差一个字就是另一个词
#: （「帮我催催」与「帮我催一」），判不出抄。
NEAR_COPY_MIN_CHARS_T169 = 5


def _desk_factory_t169() -> FrontDesk:
    store = SqliteStore(":memory:")
    seed_cs_kb(store)
    return FrontDesk(store, CsConfig(tenants={"wk_eval": "tnt-demo"}, handoff_target=None))


def _norm_t169(text: str) -> str:
    return "".join(kb.tokenize(text))


def _eval_turns_t169() -> list[str]:
    doc = evaluate.load_document()
    return [_norm_t169(t) for c in doc["cases"] for t in c["turns"]]


def _variant_pairs_t169() -> set[tuple[str, str]]:
    out = set()
    for row in corpus.load_corpus():
        body = json.loads(row["body"])
        for variant in [*body["synonyms"], *body["examples"]]:
            out.add((row["rule_no"], variant))
    return out


def _fingerprint_t169(pairs) -> str:
    blob = json.dumps(sorted(pairs), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _eval_fragments_t169(variant: str, *, turns) -> set[str]:
    """``variant`` 规整后、与某句评测句共享的 ≥ EVAL_FRAGMENT_CHARS_T169 字连续片段（按窗口列出）。"""
    norm = _norm_t169(variant)
    n = EVAL_FRAGMENT_CHARS_T169
    windows = {norm[i:i + n] for i in range(len(norm) - n + 1)}
    return {w for w in windows if any(w in t for t in turns)}


def _substring_edit_distance_t169(pattern: str, text: str) -> int:
    """``pattern`` 与 ``text`` 的某一段之间的最小编辑距离（段在哪儿起止不计代价）。"""
    prev = [0] * (len(text) + 1)
    for i, pc in enumerate(pattern, 1):
        cur = [i] + [0] * len(text)
        for j, tc in enumerate(text, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (pc != tc))
        prev = cur
    return min(prev)


def _near_copies_t169(variant: str, *, turns) -> list[str]:
    """规整后 ≥ NEAR_COPY_MIN_CHARS_T169 字的 ``variant`` 与哪些评测句的某一段只差 ≤ 1 个字。"""
    norm = _norm_t169(variant)
    if len(norm) < NEAR_COPY_MIN_CHARS_T169:
        return []
    return [t for t in turns if _substring_edit_distance_t169(norm, t) <= 1]


@pytest.mark.xfail(strict=True, reason=(
    "开发集裁定（2026-09-24）后按三类通用缺口补了说法：CS12-014#1、CS12-045#1 已对上；"
    "CS12-044#1 是 PAY-001 / PAY-003 的歧义，通用说法补不上、要改重排（见模块头与 "
    "KNOWN_GAPS_T169），按裁定不硬补"))
def test_real_front_desk_meets_the_p12_eval_thresholds_t169():
    cases = evaluate.load_cases()
    thresholds = evaluate.load_thresholds()
    assert set(evaluate.THRESHOLD_KEYS) <= set(thresholds)
    report = evaluate.run_eval(_desk_factory_t169, cases)
    assert report.turns == sum(len(c.turns) for c in cases) >= 40
    assert report.meets(thresholds), report.describe()
    assert report.failures == (), report.describe()


def test_eval_gaps_are_exactly_the_known_ones_t169():
    """满分那条之外的一切照旧要求：没对上的轮恰好是 KNOWN_GAPS_T169（不多一轮、不少一轮、
    指标一样），编造状态恒为 0，其余每一轮的 route / intent / reason / cite 全对。"""
    cases = evaluate.load_cases()
    report = evaluate.run_eval(_desk_factory_t169, cases)
    assert report.turns == sum(len(c.turns) for c in cases) >= 40
    got = {(m.case_id, m.turn): tuple(sorted(m.problems)) for m in report.failures}
    want = {k: tuple(sorted(v)) for k, v in KNOWN_GAPS_T169.items()}
    assert got == want, report.describe()
    assert report.status_fabrication == 0, report.describe()
    assert report.intent_hits == report.turns - sum("intent" in v for v in want.values())
    assert report.route_hits == report.turns - sum("route" in v for v in want.values())


def test_corpus_does_not_copy_eval_sentences_t169():
    """复核 L1-1：话术库的同义词 / 例句（规整后 ≥ OVERLAP_MIN_CHARS_T169 字）不许是任何一句
    评测句的一段，基线就有的 12 条除外。T170 的防抄测试只比整句，截一段它看不见。"""
    turns = _eval_turns_t169()
    overlaps = set()
    for rule_no, variant in _variant_pairs_t169():
        norm = _norm_t169(variant)
        if len(norm) >= OVERLAP_MIN_CHARS_T169 and any(norm in t for t in turns):
            overlaps.add((rule_no, variant))
    assert overlaps <= BASELINE_OVERLAPS_T169, sorted(overlaps - BASELINE_OVERLAPS_T169)


def test_corpus_is_the_baseline_plus_declared_additions_t169():
    """复核二轮 L1-1：话术库的说法 = a4935cd 基线 ∪ ADDED_VARIANTS_T169，一条不多一条不少。
    悄悄补一条短说法（三个字，躲得过 ≥ 4 字的片段棘轮）在这里就红。"""
    pairs = _variant_pairs_t169()
    assert ADDED_VARIANTS_T169 <= pairs, sorted(ADDED_VARIANTS_T169 - pairs)
    base = pairs - ADDED_VARIANTS_T169
    assert (len(base), _fingerprint_t169(base)) == (
        BASELINE_VARIANT_COUNT_T169, BASELINE_VARIANT_SHA256_T169), (
        "话术库的说法与 a4935cd 基线不一致：本轨补的写进 ADDITION_REASONS_T169（类别 + 理由）"
        "并过长片段与一字之差判据；改基线要主会话批准")


def test_every_declared_addition_has_a_gap_class_and_a_reason_t169():
    """开发集裁定：每条补充都写明针对哪一类通用缺口、为什么自然；类别只许是裁定点名的三类，
    且只许补给该类对应的那一篇（催促说法不许补进 PAY-003 之类）。"""
    assert ADDED_VARIANTS_T169 == frozenset(ADDITION_REASONS_T169)
    bad = {}
    for (scheme_no, variant), (gap, reason) in ADDITION_REASONS_T169.items():
        if gap not in GAP_CLASSES_T169:
            bad[(scheme_no, variant)] = f"类别 {gap!r} 不是裁定点名的三类"
        elif GAP_CLASSES_T169[gap] != scheme_no:
            bad[(scheme_no, variant)] = f"{gap} 只许补给 {GAP_CLASSES_T169[gap]}"
        elif len(reason.strip()) < 4:
            bad[(scheme_no, variant)] = "没写理由"
    assert bad == {}
    assert {gap for gap, _ in ADDITION_REASONS_T169.values()} <= set(GAP_CLASSES_T169)


def test_declared_additions_share_no_long_fragment_with_eval_t169():
    """开发集裁定（替换原两字词干判据）：声明的每一条都不许含评测句里任何 5 字连续片段。"""
    turns = _eval_turns_t169()
    bad = {(r, v): sorted(_eval_fragments_t169(v, turns=turns)) for r, v in ADDED_VARIANTS_T169}
    assert {k: v for k, v in bad.items() if v} == {}


def test_declared_additions_are_not_one_edit_from_eval_t169():
    """「评测句换一个字」这种擦边：规整后 ≥ 5 字的补充与任何评测句的某一段编辑距离不许 ≤ 1。"""
    turns = _eval_turns_t169()
    bad = {(r, v): _near_copies_t169(v, turns=turns) for r, v in ADDED_VARIANTS_T169}
    assert {k: v for k, v in bad.items() if v} == {}


def test_copy_checks_can_fail_t169():
    """反向：抄句、截段、评测句加字 / 删字 / 换字，各道判据判得出；判据也不是「凡重合都判」。"""
    turns = _eval_turns_t169()
    # 片段棘轮：首版从评测句里截下来的几条，整条就是某句评测句的一段。
    for copied in ("催一下发货", "帮我申请退货", "打扰一下", "一般几天能退回"):
        assert any(_norm_t169(copied) in t for t in turns), copied
    # 长片段判据：抄段、评测句前后加字、换掉句尾的字 —— 都留着一段 ≥ 5 字的原文。
    for fitted in ("催一下发货", "麻烦帮我催一下发货吧", "一般几天能退回", "钱退回来一般要多久",
                   "您好打扰问一下哈", "帮我申请退货呗"):
        assert _eval_fragments_t169(fitted, turns=turns), fitted
    assert "钱退回来一" in _eval_fragments_t169("钱退回来一般要多久", turns=turns)
    # 一字之差判据：短评测句中间删一个字 / 换一个字，每个 5 字窗口都盖住那个字，
    # 长片段判据判不出（先断言这一点，证明第二道不是摆设），编辑距离判得出。
    for fitted in ("帮我催下发货", "您好打搅问一下"):
        assert not _eval_fragments_t169(fitted, turns=turns), fitted
        assert _near_copies_t169(fitted, turns=turns), fitted
    # 增补棘轮本身：多一条没声明的说法，指纹就对不上。
    base = _variant_pairs_t169() - ADDED_VARIANTS_T169
    assert _fingerprint_t169(base | {("GEN-001", "打扰一下")}) != BASELINE_VARIANT_SHA256_T169
    # 不是「凡重合都判」：基线例句「催一下我的订单行不」与 CS12-014 共享「催一下」三个字，
    # 两道判据都不判；四个字以内不看一字之差。
    for fine in ("催一下我的订单行不", "帮我催催"):
        assert not _eval_fragments_t169(fine, turns=turns), fine
        assert not _near_copies_t169(fine, turns=turns), fine
    assert _substring_edit_distance_t169("帮我催催", "帮我催一下发货") == 1


def test_each_case_gets_a_fresh_desk_and_store_t169():
    """desk_factory 每次给新的库：上一段会话转了人工，不影响下一段。"""
    a, b = _desk_factory_t169(), _desk_factory_t169()
    assert a is not b and a.store is not b.store
