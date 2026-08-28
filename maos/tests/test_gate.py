"""ReviewerGate 行为测试 —— 自检判定的口径锁死在这里。

Gate 是独立的判定面，不能假设上游（skill / Coding Agent）已经把 self_check
收敛成合法形状：

  · "没自检"必须判成 finding，不能因为 .get 兜了个 {} 就静默放行；
  · 形状不对（None / 字符串）必须按"没自检"处理，**不能抛** ——
    flows/common.py 的驱动循环是裸调 review_pending()，异常逃出去整个 plan
    当场崩，连退化成一次 rework 都做不到。

测试走 review_pending() 而不是直接调 _gate_acceptance()，就是因为要验的正是
"异常会不会从这个入口逃出去"，那才是驱动循环真实的调用形状。
"""

from __future__ import annotations

import pytest

from maos.contracts.events import Topic
from maos.contracts.states import PlanState, TaskState
from maos.core.control_plane import ControlPlane
from maos.core.eventbus import EventBus
from maos.core.store import SqliteStore
from maos.runtime.gate import ReviewerGate

_MISSING = object()  # 与 None 区分：键根本不在 vs 键在但值是 null


class _RecordingBus(EventBus):
    """只记不发。Gate 的判定结果从 REVIEW_VERDICT 出来，这里原样收下。

    不用 InMemoryEventBus 是为了不让 ControlPlane 的订阅在 drain 时跟着跑状态迁移 ——
    这里测的是 Gate 的判定，不是状态机。
    """

    def __init__(self) -> None:
        self.published: list[tuple[str, object]] = []

    def publish(self, topic, env) -> None:
        self.published.append((topic, env))

    def subscribe(self, topic, group, handler) -> None:
        pass

    def drain(self, max_rounds: int = 1000) -> int:
        return 0


def _review_one(self_check) -> dict:
    """造一个 AWAITING_REVIEW 的任务 + 一份 patch_set，跑 Gate，返回 verdict payload。

    self_check 传 _MISSING 表示"这个键根本不写进 content"。
    """
    store = SqliteStore()
    store.init_schema()
    bus = _RecordingBus()
    gate = ReviewerGate(store, bus, ControlPlane(store, bus))

    content = {
        "files": [{"path": "src/auth.py", "diff": "@@ -12,3 +12,4 @@\n+    verify_token(t)"}],
        "summary": "修复 token 校验缺失",
    }
    if self_check is not _MISSING:
        content["self_check"] = self_check

    store.insert_plan({"plan_id": "p1", "trace_id": "tr",
                       "goal": "g", "state": PlanState.RUNNING})
    store.insert_task({"task_id": "t1", "plan_id": "p1", "trace_id": "tr", "role": "coding",
                       "title": "修复 token 校验缺失",
                       "state": TaskState.AWAITING_REVIEW, "attempt": 1})
    store.insert_artifact({"artifact_id": "a1", "task_id": "t1", "plan_id": "p1",
                           "kind": "patch_set", "version": 1, "content": content})

    assert gate.review_pending("p1") == 1, "AWAITING_REVIEW 的任务没有被 Gate 取到"
    topic, env = bus.published[-1]
    assert topic == Topic.REVIEW_VERDICT, f"Gate 发到了 {topic}，不是 REVIEW_VERDICT"
    return env.payload


def _acceptance_findings(self_check) -> list[dict]:
    """只看 acceptance 这道闸的 findings，别的闸（evidence 等）不干扰判定。"""
    return [f for f in _review_one(self_check)["findings"] if f["gate"] == "acceptance"]


def _acceptance_findings_no_raise(self_check, label: str) -> list[dict]:
    """断言"不抛"——不区分异常类型，抛任何东西都是失败。"""
    try:
        return _acceptance_findings(self_check)
    except Exception as exc:  # noqa: BLE001 —— 这里要的就是"任何异常都不许有"
        pytest.fail(f"self_check={label} 时 Gate 抛了 {exc!r}；"
                    f"review_pending() 在 flows/common.py 是裸调用，异常逃出即整个 plan 崩")


# ---------------------------------------------------------------- 缺失半
def test_self_check_missing_is_finding():
    """键缺失必须判 finding。改动前 .get(..., {}) 让循环一次都不进，静默判 pass。"""
    assert _acceptance_findings(_MISSING), "self_check 缺失竟然被当成自检通过放行"


# ---------------------------------------------------------------- 崩溃半
def test_self_check_none_does_not_raise():
    """self_check 为 null：按"没自检"判 finding，不许抛 AttributeError。"""
    assert _acceptance_findings_no_raise(None, "null"), "self_check 为 null 竟然被放行"


def test_self_check_str_does_not_raise():
    """self_check 是字符串（模型直接吐了 "pass"）：同上，按"没自检"处理。"""
    assert _acceptance_findings_no_raise("pass", '"pass"'), "self_check 是字符串竟然被放行"


# ---------------------------------------------------------------- 防回归
def test_self_check_fail_is_finding():
    """现有行为不许退：build=fail 出 finding，severity 仍是 major（不是 blocker）。"""
    fs = _acceptance_findings({"build": "fail", "lint": "pass"})
    assert fs, "build=fail 没有被判 finding"
    assert all(f["severity"] == "major" for f in fs), \
        "severity 被提成了别的等级，会改变四场景的流转"


def test_self_check_all_pass_is_clean():
    """全 pass 不许判 —— 否则 GOOD_PATCH 走不通，场景 1 直接红。"""
    assert _acceptance_findings({"build": "pass", "lint": "pass"}) == [], \
        "自检全 pass 竟然被判出 finding"
