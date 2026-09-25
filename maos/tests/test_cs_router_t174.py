"""T174 · router 挂钩前移（review/p13-cs-contracts.md §2' R1 / §1.4 T174）与卡片投递。

* 装了前台时，外部渠道（``EXTERNAL_CHANNELS``）的**所有**消息 —— 含 ``/xxx``、含附件 —— 在
  ``_claim`` 去重之后、附件入库与 ``Command.parse`` 之前就交给前台：runner、审批桥、附件入库
  一个都走不到；只有空白又不带附件的一条什么都不做；
* ``cs=None`` 时外部渠道照旧：``/approve`` 被拒、``/help`` 回用法、附件照收、闲聊不回；
* 装了前台时内部渠道逐字节不变、前台不被调用；
* 退款桥卡片与转人工卡片经 ``_cs_deliver`` 投到内部房间，卡片文字带槽位、预检摘要与采纳命令，
  客户侧只收到过渡话术。
"""

from __future__ import annotations

import pytest

from maos.core.store import SqliteStore
from maos.domain.cs import conversation, corpus
from maos.domain.cs import desk as D
from maos.domain.cs import types as T
from maos.domain.cs.desk import CsConfig, FrontDesk, render_card_text
from maos.domain.cs.ports import (
    BINDING_TEST, LOOKUP_OK, ORDER_STATUS_WORDING, Binding, LookupResult, PrecheckResult,
)
from maos.ingress import router as R
from maos.ingress.contracts import (
    CHANNEL_FEISHU, CHANNEL_MATRIX, CHANNEL_WECHAT_KF, CHANNEL_WECOM, Attachment, InboundMessage,
)

KFID_T174 = "wk_router_t174"
USER_T174 = "wm_router_t174_customer"
ROOM_T174 = "oc_room_t174"


class _Adapter_t174:
    configured = True

    def __init__(self, name: str):
        self.name, self.sent, self.fetched = name, [], []

    def send(self, msg) -> None:
        self.sent.append(msg)

    def fetch(self, att) -> bytes:
        self.fetched.append(att)
        return b"\x89PNG\r\n\x1a\n" + b"0" * 64


class _SpyDesk_t174:
    def __init__(self, reply: str = "前台回话 t174"):
        self.calls, self.reply = [], reply
        self.config = CsConfig()

    def handle(self, msg):
        self.calls.append(msg)
        return T.DeskResult(reply_text=self.reply, tenant_id="", conversation_id="c",
                            turn_id="c-t0001", route=T.ROUTE_FALLBACK, intent=T.INTENT_UNKNOWN,
                            draft=T.ReplyDraft(text=self.reply))


class _SpyBridge_t174:
    def __init__(self):
        object.__setattr__(self, "touched", [])

    def __getattr__(self, name):
        self.touched.append(name)
        return lambda *a, **kw: None


class _Runner_t174:
    def __init__(self):
        self.calls = []

    def __call__(self, *a, **kw):
        self.calls.append((a, kw))
        return {}


def _store_t174() -> SqliteStore:
    s = SqliteStore()
    s.init_schema()
    return s


def _adapters_t174() -> dict:
    return {n: _Adapter_t174(n) for n in (CHANNEL_FEISHU, CHANNEL_WECOM, CHANNEL_WECHAT_KF,
                                          CHANNEL_MATRIX)}


def _router_t174(*, cs=None, store=None, runner=None, bridge=None) -> R.IngressRouter:
    return R.IngressRouter(_adapters_t174(), store=store if store is not None else _store_t174(),
                           runner=runner, cs=cs, approval_bridge=bridge)


def _att_t174(channel=CHANNEL_WECHAT_KF) -> Attachment:
    return Attachment(channel=channel, file_key="fk-t174", kind="image", filename="broken.png",
                      msg_ref={"media_id": "m-t174"})


def _msg_t174(channel, text, *, msg_id="m-1", attachments=(), chat_id=USER_T174):
    raw = {"open_kfid": KFID_T174} if channel == CHANNEL_WECHAT_KF else {}
    return InboundMessage(channel=channel, chat_id=chat_id, sender=chat_id, text=text,
                          msg_id=msg_id, raw=raw, attachments=tuple(attachments))


def _sent_t174(router) -> list[tuple[str, str, str]]:
    return [(name, m.chat_id, m.text) for name, a in router.adapters.items() for m in a.sent]


COMMANDS_T174 = ["/approve RC-123", "/refund ORD-2026-0001 质量问题", "/help", "/pending",
                 "/team", "/reject RC-1 不同意"]


# ---------------------------------------------------------------------------
# 挂钩前移
# ---------------------------------------------------------------------------
def test_external_commands_go_to_the_desk_and_never_reach_runner_or_bridge_t174():
    spy, runner, bridge = _SpyDesk_t174(), _Runner_t174(), _SpyBridge_t174()
    router = _router_t174(cs=spy, runner=runner, bridge=bridge)
    for i, text in enumerate(COMMANDS_T174):
        assert router.handle(_msg_t174(CHANNEL_WECHAT_KF, text, msg_id=f"c-{i}")) == spy.reply
    assert [m.text for m in spy.calls] == COMMANDS_T174
    assert _sent_t174(router) == [(CHANNEL_WECHAT_KF, USER_T174, spy.reply)] * len(COMMANDS_T174)
    assert runner.calls == [] and bridge.touched == []
    assert router._tickets == {}


def test_external_approve_with_the_real_desk_is_just_a_customer_turn_t174():
    store = _store_t174()
    corpus.seed_cs_kb(store)
    runner, bridge = _Runner_t174(), _SpyBridge_t174()
    desk = FrontDesk(store, CsConfig(tenants={KFID_T174: "tnt-demo"}))
    router = _router_t174(cs=desk, store=store, runner=runner, bridge=bridge)
    out = router.handle(_msg_t174(CHANNEL_WECHAT_KF, "/approve RC-123"))
    assert out and "不受理审批命令" not in out and "RC-123" not in out
    (row,) = conversation.objects.query(store, "SELECT inbound_text, route FROM cs_turn")
    assert row["inbound_text"] == "/approve RC-123"
    assert runner.calls == [] and bridge.touched == []


def test_external_attachments_go_to_the_desk_and_are_not_ingested_t174(monkeypatch):
    spy = _SpyDesk_t174()
    router = _router_t174(cs=spy)

    def must_not_ingest(msg):
        raise AssertionError("外部渠道的附件不许进附件入库（p13 R1）")

    monkeypatch.setattr(router, "_ingest_attachments", must_not_ingest)
    with_text = _msg_t174(CHANNEL_WECHAT_KF, "东西坏了你看", msg_id="a-1",
                          attachments=[_att_t174()])
    only_pic = _msg_t174(CHANNEL_WECHAT_KF, "", msg_id="a-2", attachments=[_att_t174()])
    assert router.handle(with_text) == spy.reply
    assert router.handle(only_pic) == spy.reply
    assert [m.msg_id for m in spy.calls] == ["a-1", "a-2"]
    assert router.adapters[CHANNEL_WECHAT_KF].fetched == []


@pytest.mark.parametrize("text", ["", "   ", "\n\t"])
def test_external_blank_without_attachments_does_nothing_t174(text):
    spy = _SpyDesk_t174()
    router = _router_t174(cs=spy)
    assert router.handle(_msg_t174(CHANNEL_WECHAT_KF, text)) == ""
    assert spy.calls == [] and _sent_t174(router) == []


def test_duplicate_external_delivery_reaches_the_desk_once_t174():
    spy = _SpyDesk_t174()
    router = _router_t174(cs=spy)
    msg = _msg_t174(CHANNEL_WECHAT_KF, "/refund ORD-2026-0001 质量问题")
    router.handle(msg)
    assert router.handle(msg) == ""
    assert len(spy.calls) == 1


# ---------------------------------------------------------------------------
# cs=None 与内部渠道不受影响
# ---------------------------------------------------------------------------
def test_without_cs_external_behaviour_is_unchanged_t174(monkeypatch):
    runner = _Runner_t174()
    router = _router_t174(runner=runner)
    assert router.cs is None
    out = router.handle(_msg_t174(CHANNEL_WECHAT_KF, "/approve RC-123", msg_id="n-1"))
    assert "不受理审批命令" in out
    assert router.handle(_msg_t174(CHANNEL_WECHAT_KF, "/help", msg_id="n-2")) == R.USAGE
    assert router.handle(_msg_t174(CHANNEL_WECHAT_KF, "退款一般多久能到账啊", msg_id="n-3")) == ""
    seen = []
    monkeypatch.setattr(router, "_ingest_attachments", lambda msg: seen.append(msg.msg_id) or "")
    router.handle(_msg_t174(CHANNEL_WECHAT_KF, "", msg_id="n-4", attachments=[_att_t174()]))
    assert seen == ["n-4"]                                   # 没装前台：附件照旧进入库那条路
    assert runner.calls == []


INTERNAL_T174 = ["/help", "/approve RC-123", "/pending", "退款一般多久能到账啊",
                 "@财务执行岗 你是干什么的"]


@pytest.mark.parametrize("channel", [CHANNEL_FEISHU, CHANNEL_WECOM, CHANNEL_MATRIX])
def test_internal_channels_are_byte_identical_with_cs_installed_t174(channel):
    spy = _SpyDesk_t174()
    bare, wired = _router_t174(), _router_t174(cs=spy)
    for i, text in enumerate(INTERNAL_T174):
        msg = _msg_t174(channel, text, msg_id=f"i-{i}")
        assert wired.handle(msg) == bare.handle(msg), text
    assert _sent_t174(wired) == _sent_t174(bare)
    assert any(t for _, _, t in _sent_t174(bare))
    assert spy.calls == []


# ---------------------------------------------------------------------------
# 卡片投递（退款桥卡片 + 转人工卡片）
# ---------------------------------------------------------------------------
class _Verifier_t174:
    def resolve(self, store, *, tenant_id, channel, external_userid, display_no):
        if external_userid != USER_T174 or display_no != "A1001":
            return None
        return Binding(tenant_id=tenant_id, channel=channel, external_userid=external_userid,
                       display_no=display_no, system_name="demo-orders", query_key="qk-A1001",
                       source=BINDING_TEST)


class _Lookup_t174:
    def lookup(self, store, binding, *, plan_id, task_id):
        return LookupResult(outcome=LOOKUP_OK, system_name="demo-orders",
                            query_key=binding.query_key, status="shipped", version=1)


class _Precheck_t174:
    def precheck(self, *, tenant_id, order_no, reason_text, now):
        return PrecheckResult(ok=True, decision="approve", rule_ref="R-7D",
                              reason_code="quality_defect",
                              command_line=f"/refund {order_no} quality_defect",
                              summary=f"只读预检：订单 {order_no}，裁定 通过")


def _wired_desk_router_t174(*, target=(CHANNEL_FEISHU, ROOM_T174)):
    store = _store_t174()
    corpus.seed_cs_kb(store)
    desk = FrontDesk(store, CsConfig(tenants={KFID_T174: "tnt-demo"}, handoff_target=target),
                     clock=lambda: "2026-09-25T08:00:00+00:00", verifier=_Verifier_t174(),
                     lookup=_Lookup_t174(), precheck=_Precheck_t174())
    return store, _router_t174(cs=desk, store=store)


def test_refund_bridge_card_is_delivered_with_slots_summary_and_command_t174():
    store, router = _wired_desk_router_t174()
    reply = router.handle(_msg_t174(CHANNEL_WECHAT_KF, "A1001 质量有问题，我要退货"))
    assert reply == D.REPLY_BY_REASON[T.HANDOFF_REFUND_REQUEST]
    ((card, delivery),) = conversation.list_handoffs(store, "tnt-demo")
    assert (card.reason, delivery) == (T.HANDOFF_REFUND_REQUEST, T.DELIVERY_DELIVERED)
    sent = _sent_t174(router)
    # p14 · T178：refund_request ∈ CONFERENCE_REASONS，转人工卡片之后同一目标再收一张会诊卡（整合期改钉子）。
    assert len(sent) == 3
    assert sent[0] == (CHANNEL_FEISHU, ROOM_T174, render_card_text(card))
    assert sent[1][:2] == (CHANNEL_FEISHU, ROOM_T174) and sent[1][2].startswith("【圆桌会诊】")
    assert sent[2] == (CHANNEL_WECHAT_KF, USER_T174, reply)
    lines = sent[0][2].splitlines()
    # 复核 L2-1：预检与命令用绑定解析出的 query_key；客户报的单号留在槽位里。
    assert "采纳命令：/refund qk-A1001 quality_defect" in lines
    assert "内部单号：qk-A1001（客户报的是 A1001）" in lines
    assert "预检摘要：只读预检：订单 qk-A1001，裁定 通过" in lines
    assert any(ln.startswith("槽位：") and "order_no=A1001" in ln for ln in lines)
    # 客户侧一个状态字都不说
    assert ORDER_STATUS_WORDING["zh"]["shipped"] not in reply and "/refund" not in reply


def test_status_answer_goes_to_the_customer_only_and_no_card_t174():
    store, router = _wired_desk_router_t174()
    reply = router.handle(_msg_t174(CHANNEL_WECHAT_KF, "A1001 到哪了"))
    assert reply == ORDER_STATUS_WORDING["zh"]["shipped"]
    assert _sent_t174(router) == [(CHANNEL_WECHAT_KF, USER_T174, reply)]
    assert conversation.list_handoffs(store, "tnt-demo") == []


def test_identity_failure_card_is_delivered_t174():
    store, router = _wired_desk_router_t174()
    reply = router.handle(_msg_t174(CHANNEL_WECHAT_KF, "Z9999 到哪了"))
    assert reply == D.REPLY_BY_REASON[T.HANDOFF_IDENTITY_UNVERIFIED]
    ((card, delivery),) = conversation.list_handoffs(store, "tnt-demo")
    assert (card.reason, delivery) == (T.HANDOFF_IDENTITY_UNVERIFIED, T.DELIVERY_DELIVERED)
    assert (CHANNEL_FEISHU, ROOM_T174, render_card_text(card)) in _sent_t174(router)


# ---------------------------------------------------------------------------
# scripts/run_ingress.py 装配（MAOS_INGRESS_DB、MAOS_CS_BINDINGS、端口与模型）
# ---------------------------------------------------------------------------
_ENV_KEYS_T174 = ("MAOS_CS_TENANTS", "MAOS_CS_HANDOFF_TARGET", "MAOS_INGRESS_DB",
                  "MAOS_CS_BINDINGS", "MAOS_CS_ORDER_SYSTEMS", "MAOS_CS_LEDGER_TENANT")


def _load_run_ingress_t174():
    import importlib.util
    import os
    import sys
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "scripts", "run_ingress.py")
    spec = importlib.util.spec_from_file_location("_t174_run_ingress", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_t174_run_ingress"] = mod
    spec.loader.exec_module(mod)
    return mod


class _FakeServer_t174:
    built: list = []

    def __init__(self, adapters, router, *, host, port):
        self.router = router
        _FakeServer_t174.built.append(self)

    def serve_forever(self):
        return None


def _serve_t174(monkeypatch, env: dict):
    import argparse
    mod = _load_run_ingress_t174()
    for key in _ENV_KEYS_T174:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(mod, "build_adapters", _adapters_t174)
    monkeypatch.setattr(mod, "IngressServer", _FakeServer_t174)
    _FakeServer_t174.built = []
    code = mod.cmd_serve(argparse.Namespace(host="127.0.0.1", port=0, ledger=R.DEFAULT_LEDGER))
    return code, (_FakeServer_t174.built[0] if _FakeServer_t174.built else None)


def _bindings_file_t174(tmp_path, user: str, display_no: str = "ORD-2026-0001") -> str:
    import json
    path = tmp_path / "bindings_t174.json"
    path.write_text(json.dumps({"bindings": [{
        "tenant_id": "tnt-demo", "channel": CHANNEL_WECHAT_KF, "external_userid": user,
        "display_no": display_no, "system_name": "demo-orders",
        "query_key": "ORD-2026-0001"}]}), encoding="utf-8")
    return str(path)


def test_run_ingress_refund_bridge_uses_the_bound_query_key_not_the_display_no_t174(
        monkeypatch, tmp_path):
    """复核 L2-1：客户看到的单号 SO88001 经绑定映射到台账单号 ORD-2026-0001 ——
    真装配（BindingVerifier + CommerceOrderLookup + LedgerRefundPrecheck）下退款桥照样出可采纳的卡。"""
    code, server = _serve_t174(monkeypatch, {
        "MAOS_CS_TENANTS": f"{KFID_T174}=tnt-demo",
        "MAOS_CS_BINDINGS": _bindings_file_t174(tmp_path, USER_T174, display_no="SO88001"),
        "MAOS_CS_ORDER_SYSTEMS": "demo", "MAOS_CS_LEDGER_TENANT": "tnt-demo"})
    assert code == 0
    router, desk = server.router, server.router.cs
    first = router.handle(_msg_t174(CHANNEL_WECHAT_KF, "SO88001 到哪了", msg_id="d-1"))
    assert first == ORDER_STATUS_WORDING["zh"]["paid"]
    second = router.handle(_msg_t174(CHANNEL_WECHAT_KF, "SO88001 质量问题，我要退货",
                                     msg_id="d-2"))
    assert second == D.REPLY_BY_REASON[T.HANDOFF_REFUND_REQUEST]
    ((card, _),) = conversation.list_handoffs(desk.store, "tnt-demo")
    lines = render_card_text(card).splitlines()
    assert any(ln.startswith("采纳命令：/refund ORD-2026-0001 ") for ln in lines)
    assert "内部单号：ORD-2026-0001（客户报的是 SO88001）" in lines
    rows = conversation.objects.query(
        desk.store, "SELECT order_no, ok, refused_why FROM cs_refund_bridge")
    assert [(r["order_no"], r["ok"], r["refused_why"]) for r in rows] == [
        ("ORD-2026-0001", 1, "")]
    assert "ORD-2026-0001" not in first + second


def test_run_ingress_wires_db_bindings_ports_and_model_t174(monkeypatch, tmp_path):
    from maos.domain.cs.identity import BindingVerifier
    from maos.ingress.cs_ports import CommerceOrderLookup, LedgerRefundPrecheck
    from maos.model.client import ScriptedModelClient

    db = tmp_path / "ingress_t174.db"
    code, server = _serve_t174(monkeypatch, {
        "MAOS_CS_TENANTS": f"{KFID_T174}=tnt-demo", "MAOS_INGRESS_DB": str(db),
        "MAOS_CS_BINDINGS": _bindings_file_t174(tmp_path, USER_T174),
        "MAOS_CS_ORDER_SYSTEMS": "demo", "MAOS_CS_LEDGER_TENANT": "tnt-demo"})
    assert code == 0
    desk = server.router.cs
    assert isinstance(desk.verifier, BindingVerifier)
    assert isinstance(desk.lookup, CommerceOrderLookup)
    assert isinstance(desk.precheck, LedgerRefundPrecheck)
    assert isinstance(desk.model, ScriptedModelClient)          # conftest 强制 Scripted
    assert desk.store is server.router.store and db.exists()
    router = server.router
    reply = router.handle(_msg_t174(CHANNEL_WECHAT_KF, "ORD-2026-0001 到哪了", msg_id="r-1"))
    assert reply == ORDER_STATUS_WORDING["zh"]["paid"]          # 台账没有 status 列 → paid
    bridge_reply = router.handle(_msg_t174(CHANNEL_WECHAT_KF, "ORD-2026-0001 质量问题，我要退货",
                                           msg_id="r-2"))
    assert bridge_reply == D.REPLY_BY_REASON[T.HANDOFF_REFUND_REQUEST]
    ((card, _),) = conversation.list_handoffs(desk.store, "tnt-demo")
    assert "采纳命令：/refund ORD-2026-0001 " in render_card_text(card)
    # 落了盘：另开一个连接读得到这两轮
    again = SqliteStore(str(db))
    rows = conversation.objects.query(again, "SELECT route FROM cs_turn ORDER BY seq")
    assert [r["route"] for r in rows] == [T.ROUTE_ANSWER, T.ROUTE_HANDOFF]


def test_run_ingress_without_order_systems_keeps_the_p12_desk_t174(monkeypatch):
    code, server = _serve_t174(monkeypatch, {"MAOS_CS_TENANTS": f"{KFID_T174}=tnt-demo"})
    desk = server.router.cs
    assert code == 0 and not desk.ports_injected
    assert (desk.verifier, desk.lookup, desk.precheck) == (None, None, None)


def test_run_ingress_without_ledger_tenant_installs_no_precheck_t174(monkeypatch):
    code, server = _serve_t174(monkeypatch, {"MAOS_CS_TENANTS": f"{KFID_T174}=tnt-demo",
                                             "MAOS_CS_ORDER_SYSTEMS": "demo"})
    desk = server.router.cs
    assert code == 0 and desk.lookup is not None and desk.precheck is None


def test_run_ingress_without_cs_tenants_ignores_the_p13_env_t174(monkeypatch, tmp_path):
    code, server = _serve_t174(monkeypatch, {
        "MAOS_CS_ORDER_SYSTEMS": "demo", "MAOS_CS_LEDGER_TENANT": "tnt-demo",
        "MAOS_CS_BINDINGS": _bindings_file_t174(tmp_path, USER_T174)})
    assert code == 0 and server.router.cs is None


def test_run_ingress_refuses_a_bad_bindings_file_t174(monkeypatch, tmp_path, capsys):
    bad = tmp_path / "bad_t174.json"
    bad.write_text('{"bindings": [{"tenant_id": "tnt-demo"}]}', encoding="utf-8")
    code, server = _serve_t174(monkeypatch, {"MAOS_CS_TENANTS": f"{KFID_T174}=tnt-demo",
                                             "MAOS_CS_BINDINGS": str(bad)})
    assert code == 2 and server is None
    assert "绑定种子文件" in capsys.readouterr().err
