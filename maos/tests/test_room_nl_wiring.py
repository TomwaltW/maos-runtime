"""整合守卫：房间自然语言值守的接线必须落到**正式实现**上。

本文件是整合轨（T66+T67+T68 并轨）加的，不属于任何单轨的白名单 —— 它守的正是
「四轨各自全绿、合起来却有洞」的那一类问题。

钉三件事：

1. `hiclaw/room_nl.py` 真的把 T68 的 `dispatch_intent` 传下去了，不是 stub
2. 正式实现与并行期替身在**空名单**上的行为差异确实存在（差异本身是判据：
   谁哪天把 `dispatch_intent` 也写成 `if approvers and ...`，这条会红）
3. `build_parser()` 补出来的东西符合契约 §1.3 的 `IntentParser` 形状
"""

from __future__ import annotations

import pytest

from hiclaw import room_agent, room_nl
from maos.model.client import ScriptedModelClient
from maos.runtime.intent_dispatch import (
    KIND_CONFIRM, KIND_DENIED, dispatch_intent,
)

APPROVER = "@boss:maos.local"
OUTSIDER = "@intern:maos.local"
TASK_ID = "task_997ca4541e66"


class _Intent:
    """按契约 §1 的字段形状造一个，两轨的实现都按 getattr 读它。"""

    def __init__(self, action="approve", task_id=TASK_ID):
        self.action = action
        self.task_id = task_id
        self.reason = ""
        self.confidence = "low"
        self.raw_text = ""
        self.detail = {}


# --------------------------------------------------------------------------
# 1. 接线：main 必须把正式实现塞进去
# --------------------------------------------------------------------------
def test_room_nl_injects_real_dispatcher(monkeypatch):
    """`room_nl.main` 传给 `room_agent.main` 的必须是 T68 的 `dispatch_intent`。

    真跑一次房间是不可能的（要 Synapse + 凭证），所以拦在注入这一层：
    参数对了，下游就是 T67 已经测过的路径。
    """
    seen = {}

    def _fake_agent_main(argv=None, *, intent_parser=None, dispatcher=None):
        seen["parser"] = intent_parser
        seen["dispatcher"] = dispatcher
        return 0

    monkeypatch.setattr(room_nl, "_agent_main", _fake_agent_main)
    assert room_nl.main([]) == 0

    assert seen["dispatcher"] is dispatch_intent, "跑的还是 stub，权限闸口径就不是正式的"
    assert seen["parser"] is not None
    assert callable(seen["parser"])


# --------------------------------------------------------------------------
# 2. 差异守卫：空名单上两者必须不同
# --------------------------------------------------------------------------
@pytest.mark.parametrize("sender", [APPROVER, OUTSIDER])
def test_empty_approvers_official_denies(sender):
    """正式实现：名单为空 → 谁都不许，包括平时那个审批人。

    配置缺失时放行是最经典的权限漏洞形态。这条与 T68 自己的用例重复是**故意的**：
    它在整合轨再钉一次，防止并轨时被谁「统一」成 stub 的写法。
    """
    r = dispatch_intent(_Intent(), store=None, cp=None,
                        sender=sender, approvers=())

    assert r.kind == KIND_DENIED


@pytest.mark.parametrize("sender", [APPROVER, OUTSIDER])
def test_empty_approvers_stub_would_let_through(sender):
    """并行期替身：名单为空 → 直接放到确认卡片。**这就是差异本身。**

    这条不是在给 stub 背书，是把「两者不同」变成机器判据：哪天有人把 stub 的
    宽松写法搬进正式实现，上一条会红；哪天有人以为「两边一样、删一个」，
    这条会红。真房间入口走 `hiclaw/room_nl.py`，跑不到这个分支。
    """
    r = room_agent._StubDispatcher()(_Intent(), store=None, cp=None,
                                     sender=sender, approvers=())

    assert r.kind == KIND_CONFIRM


def test_official_and_stub_agree_when_list_is_configured():
    """名单配好时两者一致 —— 差异**只**在「配置缺失」这一格，不是到处不同。"""
    for sender, expected in ((APPROVER, KIND_CONFIRM), (OUTSIDER, KIND_DENIED)):
        official = dispatch_intent(_Intent(), store=None, cp=None,
                                   sender=sender, approvers=[APPROVER])
        stub = room_agent._StubDispatcher()(_Intent(), store=None, cp=None,
                                            sender=sender, approvers=[APPROVER])
        assert official.kind == expected
        assert stub.kind == expected


# --------------------------------------------------------------------------
# 3. parser 形状
# --------------------------------------------------------------------------
def test_build_parser_matches_intent_parser_shape():
    """`build_parser()` 补出来的东西要能按 `(text, *, known_task_ids)` 调。

    显式喂 `ScriptedModelClient`：这台机器**配着可用的 key**，用默认的
    `select_model_client()` 会在测试里打真网络。
    """
    parser = room_nl.build_parser(ScriptedModelClient({}))
    intent = parser("同意", known_task_ids=[TASK_ID])

    assert intent.action in {"approve", "unknown"}
    assert getattr(intent, "raw_text", None) is not None
