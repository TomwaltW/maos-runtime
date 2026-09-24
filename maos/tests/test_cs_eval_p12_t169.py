"""T169 · 真前台端到端跑 p12 评测集（契约 §1.6 / §5 W-B）。

每个 case 一个新前台：新的 ``SqliteStore(':memory:')`` + 灌话术库 + ``FrontDesk``（客服账号
``wk_eval`` 映射到 ``tnt-demo``，不配转人工投递目标）。用 T170 的 ``evaluate.run_eval`` 跑
``scenarios/cs/eval/p12_cases.json``，断言报告达到文件里写的 ``_thresholds``；没达标时
把逐轮明细打进断言消息。
"""

from __future__ import annotations

from maos.core.store import SqliteStore
from maos.domain.cs import evaluate
from maos.domain.cs.corpus import seed_cs_kb
from maos.domain.cs.desk import CsConfig, FrontDesk


def _desk_factory_t169() -> FrontDesk:
    store = SqliteStore(":memory:")
    seed_cs_kb(store)
    return FrontDesk(store, CsConfig(tenants={"wk_eval": "tnt-demo"}, handoff_target=None))


def test_real_front_desk_meets_the_p12_eval_thresholds_t169():
    cases = evaluate.load_cases()
    thresholds = evaluate.load_thresholds()
    assert set(evaluate.THRESHOLD_KEYS) <= set(thresholds)
    report = evaluate.run_eval(_desk_factory_t169, cases)
    assert report.turns == sum(len(c.turns) for c in cases) >= 40
    assert report.meets(thresholds), report.describe()
    assert report.failures == (), report.describe()


def test_each_case_gets_a_fresh_desk_and_store_t169():
    """desk_factory 每次给新的库：上一段会话转了人工，不影响下一段。"""
    a, b = _desk_factory_t169(), _desk_factory_t169()
    assert a is not b and a.store is not b.store
