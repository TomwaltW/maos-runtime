"""T169 router 挂钩、微信客服 origin 过滤、run_ingress 装配的机器验收。

跨轨契约 review/p12-cs-contracts.md §1.8：

* ``cs=None``（全仓缺省）时所有渠道逐字节不变；装了 cs 时内部渠道也逐字节不变；
* 命令分支一个字不动：外部 ``/approve`` 照旧被拒、前台与 runner 都不被调用；
* ``_cs_turn`` 永不抛；回复经 ``_reply`` 出门（带 open_kfid）；卡片经 ``_cs_deliver`` 投到
  ``config.handoff_target`` 并回写 delivery；
* wecom ``_sync``：行里带 origin 且 != 3 就丢；
* run_ingress：``MAOS_CS_TENANTS`` 非空才装前台，并在启动时灌话术库。
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import time

import pytest

from maos.core.store import SqliteStore
from maos.domain.cs import conversation, corpus
from maos.domain.cs import types as T
from maos.domain.cs.desk import REPLY_DESK_UNAVAILABLE, CsConfig, FrontDesk, render_card_text
from maos.ingress import router as R
from maos.ingress.contracts import (
    CHANNEL_FEISHU, CHANNEL_MATRIX, CHANNEL_WECHAT_KF, CHANNEL_WECOM, InboundMessage,
)
from maos.ingress.wecom import WeChatKfAdapter, WeComCredentials

KFID_T169 = "wk_router_t169"
USER_T169 = "wm_router_t169_customer"


# ---------------------------------------------------------------------------
# 替身
# ---------------------------------------------------------------------------
class _Adapter_t169:
    """记下每一条出站消息的假渠道。``fail=True`` 时 send 抛。"""

    configured = True

    def __init__(self, name: str, *, fail: bool = False):
        self.name, self.fail, self.sent = name, fail, []

    def send(self, msg) -> None:
        if self.fail:
            raise RuntimeError("send failed (t169)")
        self.sent.append(msg)


class _SpyDesk_t169:
    """记下被调用的前台替身；``boom=True`` 时 handle 抛。"""

    def __init__(self, *, boom: bool = False, reply: str = "前台替身回话"):
        self.calls, self.boom, self.reply = [], boom, reply
        self.config = CsConfig()

    def handle(self, msg):
        self.calls.append(msg)
        if self.boom:
            raise RuntimeError("desk exploded (t169)")
        return T.DeskResult(reply_text=self.reply, tenant_id="", conversation_id="c",
                            turn_id="c-t0001", route=T.ROUTE_ANSWER, intent=T.INTENT_GENERAL,
                            draft=T.ReplyDraft(text=self.reply))


def _store_t169() -> SqliteStore:
    s = SqliteStore()
    s.init_schema()
    return s


def _adapters_t169(**fail) -> dict:
    return {name: _Adapter_t169(name, fail=fail.get(name, False))
            for name in (CHANNEL_FEISHU, CHANNEL_WECOM, CHANNEL_WECHAT_KF, CHANNEL_MATRIX)}


def _router_t169(*, cs=None, adapters=None, runner=None, store=None) -> R.IngressRouter:
    return R.IngressRouter(adapters if adapters is not None else _adapters_t169(),
                           store=store if store is not None else _store_t169(),
                           runner=runner, cs=cs)


def _msg_t169(channel, text, *, msg_id="m-1", chat_id=USER_T169, kfid=KFID_T169):
    raw = {"open_kfid": kfid} if channel == CHANNEL_WECHAT_KF else {}
    return InboundMessage(channel=channel, chat_id=chat_id, sender=chat_id, text=text,
                          msg_id=msg_id, raw=raw)


def _sent_t169(router) -> list[tuple[str, str, str, dict]]:
    return [(name, m.chat_id, m.text, dict(m.meta))
            for name, a in router.adapters.items() for m in a.sent]


def _real_desk_t169(store, *, target=None) -> FrontDesk:
    corpus.seed_cs_kb(store)
    return FrontDesk(store, CsConfig(tenants={KFID_T169: "tnt-demo"}, handoff_target=target))


# ---------------------------------------------------------------------------
# 缺省不装 / 内部渠道逐字节不变
# ---------------------------------------------------------------------------
def test_constants_t169():
    assert R.EXTERNAL_CHANNELS == frozenset({CHANNEL_WECHAT_KF})
    assert not R.EXTERNAL_CHANNELS & R.ALLOW_APPROVAL
    assert not R.EXTERNAL_CHANNELS & R.KNOWN_VERBS
    assert _router_t169().cs is None                          # 缺省不装


def test_without_cs_wechat_kf_small_talk_stays_silent_t169():
    router = _router_t169()
    assert router.handle(_msg_t169(CHANNEL_WECHAT_KF, "退款一般多久能到账啊")) == ""
    assert _sent_t169(router) == []


INTERNAL_TEXTS_T169 = ["退款一般多久能到账啊", "转人工", "@财务执行岗 你是干什么的",
                       "财务执行岗：你是干什么的"]


@pytest.mark.parametrize("channel", [CHANNEL_FEISHU, CHANNEL_WECOM, CHANNEL_MATRIX])
def test_internal_channels_are_byte_identical_with_cs_installed_t169(channel):
    spy = _SpyDesk_t169()
    bare, wired = _router_t169(), _router_t169(cs=spy)
    for i, text in enumerate(INTERNAL_TEXTS_T169):
        msg = _msg_t169(channel, text, msg_id=f"m-{i}")
        assert wired.handle(msg) == bare.handle(msg), text
    assert _sent_t169(wired) == _sent_t169(bare)
    assert any(text for _, _, text, _ in _sent_t169(bare)), "@ 某岗应当有回帖，否则比较空转"
    assert spy.calls == []


# ---------------------------------------------------------------------------
# 外部渠道进前台
# ---------------------------------------------------------------------------
def test_wechat_kf_text_goes_to_the_front_desk_and_reply_carries_open_kfid_t169():
    store = _store_t169()
    router = _router_t169(cs=_real_desk_t169(store), store=store)
    out = router.handle(_msg_t169(CHANNEL_WECHAT_KF, "退款一般多久能到账啊"))
    ((name, chat_id, text, meta),) = _sent_t169(router)
    assert (name, chat_id, meta) == (CHANNEL_WECHAT_KF, USER_T169, {"open_kfid": KFID_T169})
    assert text == out and "原路退回" in out
    rows = conversation.objects.query(store, "SELECT route, msg_dedup_key FROM cs_turn")
    assert rows == [{"route": T.ROUTE_ANSWER,
                     "msg_dedup_key": f"ingress:{CHANNEL_WECHAT_KF}:m-1"}]


def test_wechat_kf_mention_also_goes_to_the_front_desk_t169():
    spy = _SpyDesk_t169()
    router = _router_t169(cs=spy)
    out = router.handle(_msg_t169(CHANNEL_WECHAT_KF, "@财务执行岗 你是干什么的"))
    assert [m.text for m in spy.calls] == ["@财务执行岗 你是干什么的"]
    assert out == spy.reply


@pytest.mark.parametrize("text", ["   ", "\n\t", "　", ""])
def test_blank_wechat_kf_text_does_not_reach_the_desk_t169(text):
    """复核 L2-7：挂钩的「有字」条件 —— 只有空白的一条不进前台（否则前台拿空白去检索、
    回一句兜底，还把连续兜底计数加一），与不装前台逐字节一致：什么都不发。"""
    spy = _SpyDesk_t169()
    wired, bare = _router_t169(cs=spy), _router_t169()
    msg = _msg_t169(CHANNEL_WECHAT_KF, text)
    assert wired.handle(msg) == bare.handle(msg) == ""
    assert spy.calls == []
    assert _sent_t169(wired) == _sent_t169(bare) == []


def test_wechat_kf_duplicate_delivery_reaches_the_desk_once_t169():
    spy = _SpyDesk_t169()
    router = _router_t169(cs=spy)
    msg = _msg_t169(CHANNEL_WECHAT_KF, "包邮吗")
    router.handle(msg)
    assert router.handle(msg) == ""
    assert len(spy.calls) == 1


def test_wechat_kf_silent_turn_sends_nothing_t169():
    store = _store_t169()
    router = _router_t169(cs=_real_desk_t169(store), store=store)
    router.handle(_msg_t169(CHANNEL_WECHAT_KF, "转人工", msg_id="m-1"))
    before = _sent_t169(router)
    assert router.handle(_msg_t169(CHANNEL_WECHAT_KF, "人呢", msg_id="m-2")) == ""
    assert _sent_t169(router) == before


def test_wechat_kf_approve_is_still_refused_and_desk_untouched_t169():
    calls = []

    def runner(*a, **kw):
        calls.append((a, kw))
        return {}

    spy = _SpyDesk_t169()
    wired = _router_t169(cs=spy, runner=runner)
    bare = _router_t169(runner=runner)
    for i, text in enumerate(["/approve RC-123", "/refund ORD-2026-0001 质量问题", "/help"]):
        msg = _msg_t169(CHANNEL_WECHAT_KF, text, msg_id=f"m-{i}")
        assert wired.handle(msg) == bare.handle(msg), text
    assert "不受理审批命令" in _sent_t169(wired)[0][2]
    assert spy.calls == [] and calls == []


def test_desk_exploding_does_not_break_the_router_t169(caplog):
    router = _router_t169(cs=_SpyDesk_t169(boom=True))
    out = router.handle(_msg_t169(CHANNEL_WECHAT_KF, "包邮吗"))
    assert out == REPLY_DESK_UNAVAILABLE
    ((_, chat_id, text, meta),) = _sent_t169(router)
    assert (chat_id, text, meta["open_kfid"]) == (USER_T169, REPLY_DESK_UNAVAILABLE, KFID_T169)
    assert USER_T169 not in caplog.text and T.mask_customer(USER_T169) in caplog.text


# ---------------------------------------------------------------------------
# 卡片投递
# ---------------------------------------------------------------------------
def _handoff_state_t169(store) -> list[tuple[str, str]]:
    return [(r["delivery"], r["delivered_to"]) for r in conversation.objects.query(
        store, "SELECT delivery, delivered_to FROM cs_handoff")]


def test_card_is_delivered_to_the_internal_room_t169():
    store = _store_t169()
    router = _router_t169(cs=_real_desk_t169(store, target=(CHANNEL_FEISHU, "oc_room_t169")),
                          store=store)
    reply = router.handle(_msg_t169(CHANNEL_WECHAT_KF, "帮我转人工"))
    ((card, delivery),) = conversation.list_handoffs(store, "tnt-demo")
    assert delivery == T.DELIVERY_DELIVERED
    assert _handoff_state_t169(store) == [(T.DELIVERY_DELIVERED, f"{CHANNEL_FEISHU}:oc_room_t169")]
    sent = _sent_t169(router)
    assert (CHANNEL_FEISHU, "oc_room_t169", render_card_text(card), {}) in sent
    assert (CHANNEL_WECHAT_KF, USER_T169, reply, {"open_kfid": KFID_T169}) in sent
    assert len(sent) == 2


def test_card_without_target_is_marked_unconfigured_t169():
    store = _store_t169()
    router = _router_t169(cs=_real_desk_t169(store), store=store)
    router.handle(_msg_t169(CHANNEL_WECHAT_KF, "帮我转人工"))
    assert _handoff_state_t169(store) == [(T.DELIVERY_UNCONFIGURED, "")]
    assert [s[0] for s in _sent_t169(router)] == [CHANNEL_WECHAT_KF]


def test_card_send_failure_is_marked_failed_and_customer_still_answered_t169():
    store = _store_t169()
    router = _router_t169(cs=_real_desk_t169(store, target=(CHANNEL_FEISHU, "oc_room_t169")),
                          adapters=_adapters_t169(feishu=True), store=store)
    reply = router.handle(_msg_t169(CHANNEL_WECHAT_KF, "帮我转人工"))
    assert _handoff_state_t169(store) == [(T.DELIVERY_FAILED, "")]
    assert _sent_t169(router) == [(CHANNEL_WECHAT_KF, USER_T169, reply, {"open_kfid": KFID_T169})]


@pytest.mark.parametrize("target", [(CHANNEL_WECHAT_KF, "wm_someone_else"), ("dingtalk", "x")])
def test_card_never_goes_to_an_external_or_missing_channel_t169(target):
    store = _store_t169()
    router = _router_t169(cs=_real_desk_t169(store, target=target), store=store)
    reply = router.handle(_msg_t169(CHANNEL_WECHAT_KF, "帮我转人工"))
    assert _handoff_state_t169(store) == [(T.DELIVERY_FAILED, "")]
    assert _sent_t169(router) == [(CHANNEL_WECHAT_KF, USER_T169, reply, {"open_kfid": KFID_T169})]


# ---------------------------------------------------------------------------
# 微信客服 origin 过滤
# ---------------------------------------------------------------------------
def test_kf_sync_keeps_only_customer_messages_t169(monkeypatch):
    kf = WeChatKfAdapter(WeComCredentials(corp_id="wwcorp", secret="s", token="t",
                                          aes_key="a" * 43))
    now = time.time()

    def row(msgid, **extra):
        return {"msgtype": "text", "msgid": msgid, "send_time": now, "open_kfid": "wk1",
                "external_userid": "wmA", "text": {"content": msgid}, **extra}

    monkeypatch.setattr(kf, "_post", lambda path, payload: {"next_cursor": "c2", "msg_list": [
        row("from-customer", origin=3), row("from-servicer", origin=5),
        row("from-system", origin=4), row("no-origin"), row("str-origin", origin="3"),
        row("str-bot", origin="5"),
    ]})
    assert [m.msg_id for m in kf._sync("wk1", "token")] == ["from-customer", "no-origin",
                                                            "str-origin"]


# ---------------------------------------------------------------------------
# run_ingress 装配
# ---------------------------------------------------------------------------
def _load_run_ingress_t169():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "scripts", "run_ingress.py")
    spec = importlib.util.spec_from_file_location("_t169_run_ingress", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_t169_run_ingress"] = mod
    spec.loader.exec_module(mod)
    return mod


class _FakeServer_t169:
    built: list = []

    def __init__(self, adapters, router, *, host, port):
        self.router = router
        _FakeServer_t169.built.append(self)

    def serve_forever(self):
        return None


def _serve_t169(monkeypatch, env: dict) -> tuple[int, object]:
    mod = _load_run_ingress_t169()
    for key in ("MAOS_CS_TENANTS", "MAOS_CS_HANDOFF_TARGET"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(mod, "build_adapters", lambda: _adapters_t169())
    monkeypatch.setattr(mod, "IngressServer", _FakeServer_t169)
    _FakeServer_t169.built = []
    args = argparse.Namespace(host="127.0.0.1", port=0, ledger=R.DEFAULT_LEDGER)
    code = mod.cmd_serve(args)
    server = _FakeServer_t169.built[0] if _FakeServer_t169.built else None
    return code, server


def test_run_ingress_without_cs_env_installs_nothing_t169(monkeypatch, capsys):
    code, server = _serve_t169(monkeypatch, {})
    assert code == 0 and server.router.cs is None
    assert "客服前台" not in capsys.readouterr().out
    mod = _load_run_ingress_t169()
    assert mod._front_desk(SqliteStore(), env={}) is None
    assert mod._front_desk(SqliteStore(), env={"MAOS_CS_TENANTS": "  "}) is None


def test_run_ingress_with_cs_env_installs_the_desk_and_seeds_scripts_t169(monkeypatch):
    code, server = _serve_t169(monkeypatch, {"MAOS_CS_TENANTS": "wk_1=tnt-demo",
                                             "MAOS_CS_HANDOFF_TARGET": "feishu:oc_1"})
    desk = server.router.cs
    assert code == 0 and isinstance(desk, FrontDesk)
    assert dict(desk.config.tenants) == {"wk_1": "tnt-demo"}
    assert desk.config.handoff_target == ("feishu", "oc_1")
    assert desk.store is server.router.store
    n = conversation.objects.query(
        desk.store, "SELECT COUNT(*) AS n FROM kb_doc WHERE kind='cs_script' AND biz_type='cs'")
    assert n[0]["n"] == len(corpus.load_corpus()) == 19
    res = desk.handle(InboundMessage(channel=CHANNEL_WECHAT_KF, chat_id="wm_1", sender="wm_1",
                                     text="包邮吗亲", msg_id="x", raw={"open_kfid": "wk_1"}))
    assert res.route == T.ROUTE_ANSWER


def test_run_ingress_refuses_a_malformed_tenant_map_t169(monkeypatch, capsys):
    code, server = _serve_t169(monkeypatch, {"MAOS_CS_TENANTS": "wk_1"})
    assert code == 2 and server is None
    assert "MAOS_CS_TENANTS" in capsys.readouterr().err
