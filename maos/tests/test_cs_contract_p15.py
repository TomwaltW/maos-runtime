"""p15 契约钉子（review/p15-cs-contracts.md §2 T183 / §3）：新盲写留出集的预登记门槛一字不许动。

留出集文件由 T183 写；文件还没合入时本条只钉常量与门槛键名，合入之后逐字比对文件里的 ``_thresholds``。
"""

from __future__ import annotations

import json

from maos.domain.cs import evaluate
from maos.domain.cs.triggers import PRIORITY

P15_HOLDOUT_PATH = evaluate.P13_EVAL_PATH.with_name("p15_holdout_cases.json")

P15_PREREGISTERED_THRESHOLDS = {
    "intent_accuracy": 0.85, "route_accuracy": 0.85, "handoff_recall": 0.90,
    "status_fabrication_max": 0, "wording_accuracy": 1.0, "wrong_status_max": 0,
}


def test_threshold_keys_are_the_evaluator_keys():
    assert set(P15_PREREGISTERED_THRESHOLDS) == set(evaluate.THRESHOLD_KEYS) | set(
        evaluate.P13_THRESHOLD_KEYS)


def test_holdout_thresholds_match_the_preregistration_when_present():
    if not P15_HOLDOUT_PATH.exists():
        return
    doc = json.loads(P15_HOLDOUT_PATH.read_text(encoding="utf-8"))
    assert doc["_thresholds"] == P15_PREREGISTERED_THRESHOLDS


def test_reason_priority_stays_the_p12_contract_order():
    # p15 契约 §1（C5）：优先级不改。
    assert PRIORITY == ("privacy", "compensation", "anger", "complaint", "requested")
