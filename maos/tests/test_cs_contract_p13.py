"""p13 跨轨契约（review/p13-cs-contracts.md §1）的机器钉。

``maos/domain/cs/ports.py`` 是五条轨共同照着写的那一份；types.py 在 p13 骨架里长了枚举、
给 DeskResult 加了三个带缺省的字段（p12 那份契约测试同步改了）。这里逐字段钉住新增部分。
"""

from __future__ import annotations

import ast
import dataclasses
import pathlib
import sys

from maos.domain.cs import ports as P
from maos.domain.cs import types as T

ROOT = pathlib.Path(__file__).resolve().parents[2]
PORTS_PY = ROOT / "maos" / "domain" / "cs" / "ports.py"


def _fields(cls) -> tuple[str, ...]:
    return tuple(f.name for f in dataclasses.fields(cls))


def test_enum_growth_in_types_is_exactly_the_p13_increment():
    assert T.ROUTES[-1] == "clarify" and T.ROUTE_CLARIFY == "clarify"
    assert T.HANDOFF_REASONS[-4:] == ("identity_unverified", "order_unmapped",
                                      "lookup_failed", "refund_request")
    assert _fields(T.DeskResult)[-3:] == ("lang", "lookup_outcome", "ask_slot")
    r = T.DeskResult(reply_text="", tenant_id="", conversation_id="", turn_id="",
                     route="silent", intent="unknown", draft=T.ReplyDraft(text=""))
    assert (r.lang, r.lookup_outcome, r.ask_slot) == ("zh", "", "")


def test_port_constants_are_frozen():
    assert P.LANGS == ("zh", "en")
    assert P.SLOT_KEYS == ("order_no", "product", "problem", "request", "emotion")
    assert P.REQUEST_VALUES == ("refund", "return", "exchange", "track", "other")
    assert P.EMOTION_VALUES == ("calm", "upset", "angry")
    assert P.SLOT_SOURCES == ("rule", "model")
    assert P.MAX_ASKS_PER_SLOT == 2
    assert P.ORDER_STATUSES == ("paid", "shipped", "cancelled", "amended")
    assert P.LOOKUP_OUTCOMES == ("ok", "not_found", "unmapped_status", "amended",
                                 "system_misconfigured", "platform_error")
    assert P.OBS_KINDS == ("order_lookup",)
    assert P.BINDING_SOURCES == ("seed", "test", "internal")


def test_order_statuses_match_the_tool_layer():
    from maos.tools.order import ALL_ORDER_STATUSES
    assert set(P.ORDER_STATUSES) == set(ALL_ORDER_STATUSES)


def test_wording_table_is_frozen_and_never_says_delivered_amount_or_time():
    assert set(P.ORDER_STATUS_WORDING) == set(P.LANGS)
    for lang, table in P.ORDER_STATUS_WORDING.items():
        assert set(table) == {"paid", "shipped", "cancelled"}, lang
        for status, text in table.items():
            for banned in ("签收", "送达", "到账", "delivered", "arrive", "元", "¥", "$"):
                assert banned not in text, (lang, status, banned)
    assert P.ORDER_STATUS_WORDING["zh"]["shipped"] == "您的订单已发货（如有多件，可能分批发出）"
    assert P.ORDER_STATUS_WORDING["zh"]["cancelled"] == "您的订单已取消"
    assert P.ORDER_STATUS_WORDING["zh"]["paid"] == "您的订单已付款，暂未发货"


def test_zh_wording_is_caught_by_the_status_scanner():
    """措辞表里的中文状态句必须**被**状态字眼扫描器认出来 —— 否则「要有观察撑」就管不到它。"""
    def hit(s: str) -> bool:
        return any(p.search(s) for p in T.STATUS_PATTERNS)
    assert hit(P.ORDER_STATUS_WORDING["zh"]["shipped"])
    assert hit(P.ORDER_STATUS_WORDING["zh"]["cancelled"])


def test_result_shapes_are_frozen():
    assert _fields(P.Binding) == ("tenant_id", "channel", "external_userid", "display_no",
                                  "system_name", "query_key", "source", "bound_at")
    assert _fields(P.LookupResult) == ("outcome", "system_name", "query_key", "status",
                                       "version", "updated_at", "error_kind")
    assert _fields(P.PrecheckResult) == ("ok", "decision", "rule_ref", "reason_code",
                                         "command_line", "summary", "refused_why")
    for cls in (P.Binding, P.LookupResult, P.PrecheckResult):
        assert cls.__dataclass_params__.frozen, cls.__name__


def test_protocol_method_names():
    assert hasattr(P.IdentityVerifier, "resolve")
    assert hasattr(P.OrderLookup, "lookup")
    assert hasattr(P.RefundPrecheck, "precheck")


def test_observation_id():
    assert P.observation_id_for("csc-x-t0003", 1) == "csc-x-t0003-o01"
    try:
        P.observation_id_for("csc-x-t0003", 0)
    except ValueError:
        pass
    else:
        raise AssertionError("序号 0 必须拒")


def test_ports_module_is_stdlib_only():
    tree = ast.parse(PORTS_PY.read_text(encoding="utf-8"))
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            mods.add((node.module or "").split(".")[0])
    stdlib = set(getattr(sys, "stdlib_module_names", ())) | {"__future__"}
    assert mods <= stdlib, sorted(mods - stdlib)
