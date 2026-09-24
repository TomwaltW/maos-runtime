"""T169 · 真前台端到端跑 p12 评测集（契约 §1.6 / §5 W-B）。

每个 case 一个新前台：新的 ``SqliteStore(':memory:')`` + 灌话术库 + ``FrontDesk``（客服账号
``wk_eval`` 映射到 ``tnt-demo``，不配转人工投递目标）。用 T170 的 ``evaluate.run_eval`` 跑
``scenarios/cs/eval/p12_cases.json``，断言报告达到文件里写的 ``_thresholds``；没达标时
把逐轮明细打进断言消息。

## 复核轮（L1-1）之后的现状：满分那条标 xfail(strict)，等主会话裁

首版为了跑满分，往话术库里补的同义词有几条是从评测句里逐字 / 近乎逐字截下来的
（「催一下发货」「帮我申请退货」「打扰一下」「一般几天能退回」……），违反「不许抄评测句」。
复核轮全部撤掉，只按话术自己的适用场景补了 GEN-001 的两条问候说法，另在前台把长数字串
（订单号）挡在检索之外（见 ``desk.retrieval_query``）。干净的语料下还差两轮：

* CS12-014#1「…还没动静，帮我催一下发货」：LOG-004 只有 0.18，低于门槛，落兜底；
* CS12-044#1「钱退回来一般要几天呐」：PAY-003 的同义词「钱退回来没」（T168 写的）压过 PAY-001，
  转了人工而不是答政策。

这两轮要么抄评测句才能过、要么要删 T168 的说法（本轨只许补不许删），所以不在本轨拍板：
满分那条标 ``xfail(strict=True)`` —— 哪天真跑满了会 XPASS 变红，逼着把标记摘掉；
缺口本身由 :func:`test_eval_gaps_are_exactly_the_known_ones_t169` 逐轮钉住，别处退一步就红。
"""

from __future__ import annotations

import json

import pytest

from maos import kb
from maos.core.store import SqliteStore
from maos.domain.cs import corpus, evaluate
from maos.domain.cs.corpus import seed_cs_kb
from maos.domain.cs.desk import CsConfig, FrontDesk

#: 干净语料下跑不到的轮：(case_id, 第几轮) → 没对上的指标。改动它要回主会话。
KNOWN_GAPS_T169 = {
    ("CS12-014", 1): ("intent", "route"),
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


def _desk_factory_t169() -> FrontDesk:
    store = SqliteStore(":memory:")
    seed_cs_kb(store)
    return FrontDesk(store, CsConfig(tenants={"wk_eval": "tnt-demo"}, handoff_target=None))


def _norm_t169(text: str) -> str:
    return "".join(kb.tokenize(text))


@pytest.mark.xfail(strict=True, reason=(
    "复核 L1-1：撤掉抄自评测句的同义词后差 CS12-014#1 / CS12-044#1 两轮"
    "（见模块头与 KNOWN_GAPS_T169），待主会话裁定"))
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
    doc = evaluate.load_document()
    turns = [_norm_t169(t) for c in doc["cases"] for t in c["turns"]]
    overlaps = set()
    for row in corpus.load_corpus():
        body = json.loads(row["body"])
        for variant in [*body["synonyms"], *body["examples"]]:
            norm = _norm_t169(variant)
            if len(norm) >= OVERLAP_MIN_CHARS_T169 and any(norm in t for t in turns):
                overlaps.add((row["rule_no"], variant))
    assert overlaps <= BASELINE_OVERLAPS_T169, sorted(overlaps - BASELINE_OVERLAPS_T169)


def test_copy_check_can_fail_t169():
    """反向：首版抄进去的那几条放回来，上面的判据确实判得出。"""
    doc = evaluate.load_document()
    turns = [_norm_t169(t) for c in doc["cases"] for t in c["turns"]]
    for copied in ("催一下发货", "帮我申请退货", "打扰一下", "一般几天能退回"):
        assert any(_norm_t169(copied) in t for t in turns), copied


def test_each_case_gets_a_fresh_desk_and_store_t169():
    """desk_factory 每次给新的库：上一段会话转了人工，不影响下一段。"""
    a, b = _desk_factory_t169(), _desk_factory_t169()
    assert a is not b and a.store is not b.store
