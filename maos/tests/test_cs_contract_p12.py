"""p12 跨轨契约（review/p12-cs-contracts.md §1）的机器钉。

``maos/domain/cs/types.py`` 是四条轨共同照着写的那一份。这里逐字段、逐枚举值钉住：
任何一轨「顺手」改了一个字段名，别的轨各自绿、合起来红 —— 这条测试让它在改的那一轨
当场红。要改形状，回主会话改契约，同一个提交改 types.py 与本文件。
"""

from __future__ import annotations

import ast
import dataclasses
import pathlib
import sys

from maos.domain.cs import types as T

ROOT = pathlib.Path(__file__).resolve().parents[2]
TYPES_PY = ROOT / "maos" / "domain" / "cs" / "types.py"


def _fields(cls) -> tuple[str, ...]:
    return tuple(f.name for f in dataclasses.fields(cls))


def test_dataclass_shapes_are_frozen():
    assert _fields(T.Claim) == ("literal", "basis_ref")
    assert _fields(T.ReplyDraft) == ("text", "claims", "citations")
    assert _fields(T.Violation) == ("kind", "detail")
    assert _fields(T.CheckResult) == ("ok", "violations")
    assert _fields(T.ScriptHit) == ("doc_id", "scheme_no", "intent", "score", "script",
                                    "principle", "handoff")
    assert _fields(T.HandoffCard) == (
        "handoff_id", "tenant_id", "conversation_id", "turn_id", "channel", "reason",
        "intent", "customer_ref", "customer_text", "recent_turns", "suggestion",
        "citations", "slots", "created_at")
    assert _fields(T.DeskResult) == (
        "reply_text", "tenant_id", "conversation_id", "turn_id", "route", "intent",
        "draft", "handoff", "handoff_reason", "check",
        # p13 骨架追加的三个带缺省字段（review/p13-cs-contracts.md §1.1）
        "lang", "lookup_outcome", "ask_slot")
    for cls in (T.Claim, T.ReplyDraft, T.Violation, T.CheckResult, T.ScriptHit,
                T.HandoffCard, T.DeskResult):
        assert cls.__dataclass_params__.frozen, f"{cls.__name__} 必须 frozen"


def test_enumerations_are_frozen():
    assert T.STAGES == ("active", "handed_off", "closed")
    # p13 骨架追加 clarify（review/p13-cs-contracts.md §1.1）
    assert T.ROUTES == ("answer", "fallback", "handoff", "silent", "clarify")
    assert T.INTENTS == ("logistics", "refund_payment", "return_exchange", "general",
                         "handoff_request", "complaint", "compensation", "privacy", "unknown")
    assert T.HANDOFF_REASONS == ("requested", "complaint", "anger", "compensation", "privacy",
                                 "needs_order_lookup", "unverified_claim",
                                 "repeated_fallback", "tenant_unmapped",
                                 # p13 骨架追加的四个（review/p13-cs-contracts.md §1.1）
                                 "identity_unverified", "order_unmapped", "lookup_failed",
                                 "refund_request")
    assert T.DELIVERIES == ("pending", "delivered", "failed", "unconfigured")
    assert T.VIOLATION_KINDS == ("unbacked_status", "dangling_basis", "literal_not_in_text",
                                 "uncited_rule", "foreign_literal")
    assert T.CS_EVENT_TYPES == ("CsTurnRecorded", "CsConversationStageChanged",
                                "CsHandoffRaised", "CsReplyRejected")
    assert T.STATUS_WORDS == ("已到账", "已退款", "已发货", "已签收", "已取消", "赔偿", "补偿")
    assert (T.CS_PLAN_PREFIX, T.BIZ_TYPE_CS, T.CS_KB_KIND) == ("cs:", "cs", "cs_script")
    assert (T.BASIS_OBS, T.BASIS_KB) == ("obs:", "kb:")
    assert (T.FALLBACK_STREAK_HANDOFF, T.CARD_RECENT_TURNS) == (2, 5)


def test_stage_flow_is_closed_over_stages_and_closed_absorbs():
    assert set(T.STAGE_FLOW) == set(T.STAGES)
    for src, dsts in T.STAGE_FLOW.items():
        assert dsts <= set(T.STAGES), src
        assert src not in dsts, f"{src} 不许自迁移"
    assert T.STAGE_FLOW[T.STAGE_CLOSED] == frozenset()


def test_channel_literal_matches_ingress():
    from maos.ingress.contracts import CHANNEL_WECHAT_KF
    assert T.CHANNEL_WECHAT_KF == CHANNEL_WECHAT_KF


def test_ids_are_stable_and_carry_no_raw_identity():
    cid = T.conversation_id_for("tnt-demo", "wechat_kf", "wk_1", "wm_customer_42")
    assert cid == T.conversation_id_for("tnt-demo", "wechat_kf", "wk_1", "wm_customer_42")
    assert cid.startswith("csc-") and len(cid) == 4 + 16
    assert "wm_customer_42" not in cid and "wk_1" not in cid
    # 客服账号不同就是另一段会话
    assert cid != T.conversation_id_for("tnt-demo", "wechat_kf", "wk_2", "wm_customer_42")
    # 映射不到租户（空串）也有稳定 id，且不与有租户的撞
    assert T.conversation_id_for("", "wechat_kf", "wk_1", "wm_customer_42") != cid
    assert T.turn_id_for(cid, 1) == cid + "-t0001"
    assert T.turn_id_for(cid, 12) == cid + "-t0012"
    assert T.plan_id_for(cid) == "cs:" + cid
    try:
        T.turn_id_for(cid, 0)
    except ValueError:
        pass
    else:
        raise AssertionError("序号 0 必须拒")


def test_text_digest_and_mask():
    d = T.text_digest("我的耳机三天没发货")
    assert d.startswith("sha256:") and len(d) == 7 + 16
    assert "耳机" not in d
    assert T.mask_customer("wm_customer_42") == "…mer_42"
    assert T.mask_customer("abc") == "***"
    assert T.mask_customer("") == ""


def test_status_patterns_hit_every_word_and_the_eta_forms():
    def hit(s: str) -> bool:
        return any(p.search(s) for p in T.STATUS_PATTERNS)
    for w in T.STATUS_WORDS:
        assert hit(f"您的订单{w}了"), w
    for s in ("预计3天", "预计 3 个工作日", "预计三天", "预计两日", "预计12小时", "预计３天"):
        assert hit(s), s
    # 反向：否定式与流程说明不算断言
    for s in ("尚未发货", "还没有到账", "退款会按原支付方式退回", "审核通过后处理", "预计"):
        assert not hit(s), s


def test_json_roundtrips():
    draft = T.ReplyDraft(text="退款已到账", claims=(T.Claim("退款已到账", "obs:o1"),),
                         citations=("kb-cs-tnt-demo-PAY-001",))
    assert T.ReplyDraft.from_json(draft.to_json()) == draft
    card = T.HandoffCard(
        handoff_id="h1", tenant_id="tnt-demo", conversation_id="csc-x", turn_id="csc-x-t0001",
        channel="wechat_kf", reason="requested", intent="handoff_request",
        customer_ref="…mer_42", customer_text="转人工", recent_turns=(("转人工", "好的"),),
        suggestion="客户要求人工", citations=(), slots=(("order_no", "A1"),),
        created_at="2026-09-24T00:00:00+00:00")
    assert T.HandoffCard.from_json(card.to_json()) == card


def test_types_module_is_stdlib_only():
    """四条轨都 import 它；它 import 任何一条轨的模块就有了环。"""
    tree = ast.parse(TYPES_PY.read_text(encoding="utf-8"))
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "types.py 不许相对 import"
            mods.add((node.module or "").split(".")[0])
    stdlib = set(getattr(sys, "stdlib_module_names", ())) | {"__future__"}
    assert mods <= stdlib, f"types.py 只许 import 标准库，多了 {sorted(mods - stdlib)}"
