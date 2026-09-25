"""p14 契约钉子（review/p14-cs-contracts.md §1）：types.py 的三处增量一字不许动。"""

from __future__ import annotations

from maos.domain.cs import types as T


def test_conference_event_is_frozen_and_outside_the_conversation_events():
    assert T.EVENT_CONFERENCE_HELD == "CsConferenceHeld"
    # 会话对象自己落的四个不变；会诊卡由 roundtable/cs_conference.py 落，不进这张表。
    assert T.EVENT_CONFERENCE_HELD not in T.CS_EVENT_TYPES
    assert len(T.CS_EVENT_TYPES) == 4


def test_conference_reasons_are_exactly_four_handoff_reasons():
    assert T.CONFERENCE_REASONS == frozenset({
        T.HANDOFF_COMPLAINT, T.HANDOFF_ANGER, T.HANDOFF_COMPENSATION, T.HANDOFF_REFUND_REQUEST})
    assert T.CONFERENCE_REASONS <= set(T.HANDOFF_REASONS)


def test_mcp_plan_id_is_in_the_cs_family_and_cannot_collide_with_a_conversation():
    assert T.CS_MCP_PLAN_ID == "cs:mcp"
    assert T.CS_MCP_PLAN_ID.startswith(T.CS_PLAN_PREFIX)
    conv = T.conversation_id_for("tnt-demo", "wechat_kf", "wk_eval", "u-1")
    assert conv.startswith("csc-")
    assert T.plan_id_for(conv) != T.CS_MCP_PLAN_ID
