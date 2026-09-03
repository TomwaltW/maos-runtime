"""Router 与 webhook 服务面：去重、渠道信任级别、预检、放行、回帖措辞。

两件事在这里是**被测行为**，不是文案：

  · ``/refund`` 只做只读预检 —— 一条群消息不许直接触发付款。这条用「runner
    有没有被调用」来钉，而不是看回帖里写了什么。
  · 回帖措辞受铁律 8 约束 —— 群里那句话是很多人唯一会看的东西，把「已提交
    网关」写成「已退款」会让客服照着它去答复客户。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from maos.core.store import SqliteStore
from maos.ingress.contracts import (
    CHANNEL_FEISHU, CHANNEL_WECHAT_KF, CHANNEL_WECOM, InboundMessage, OutboundMessage,
)
from maos.ingress.router import Command, IngressRouter, TICKET_TTL, USAGE
from maos.ingress.server import IngressServer, MAX_BODY, ROUTES
from maos.tests.test_ingress_channels import FEISHU_ENV, _feishu, _message_event

ORDER = "ORD-2026-0001"
CASE = f"RC-{ORDER}"
ALICE = "ou_alice"


class FakeAdapter:
    """记下发出去的消息。`configured` 恒真 —— 这一层测的不是凭证。"""

    configured = True

    def __init__(self, name: str = CHANNEL_FEISHU) -> None:
        self.name = name
        self.sent: list[OutboundMessage] = []

    def send(self, msg: OutboundMessage) -> None:
        self.sent.append(msg)


#: `run_payload` 返回形状（`custom_case._observe`）的最小切片：只截取 router 会读的键。
RESULT_SETTLED = {
    "case_id": CASE, "decision": "approve", "why": "质保期内质量问题",
    "amount_approved": "6800.00", "policy_version_used": 1, "rule_refs": "AS-002@v1",
    "biz_status": "settled", "settled_observations": 1,
    "payment_observations": [{"observed_state": "settled"}],
    "human_exits": [], "plan_id": "plan-1", "plan_state": "DONE",
}
RESULT_UNCONFIRMED = {
    **RESULT_SETTLED,
    "biz_status": "gateway_accepted", "settled_observations": 0,
    "payment_observations": [{"observed_state": "gateway_accepted"}],
    "human_exits": [{"task_id": "t-9", "title": "主管审批", "decision": "approved",
                     "why": "effect_risk=H"}],
    "plan_state": "RUNNING",
}


@pytest.fixture()
def store() -> SqliteStore:
    s = SqliteStore(":memory:")
    s.init_schema()
    return s


class Runs(list):
    """记下 runner 被调用了几次、拿到了什么。"""

    def __call__(self, payload: dict, **kw) -> dict:
        self.append(payload)
        return dict(self.result)

    result = RESULT_SETTLED


def _router(store, *, runner=None, adapter=None, approvers=(ALICE,), **kw):
    ad = adapter or FakeAdapter()
    # `Runs` 继承 list，空的时候是 falsy —— 这里必须 `is None`，否则一个还没被
    # 调用过的记录器会被换成真跑的那个，测试照样绿（只是慢，且真的建了 plan）。
    return IngressRouter({ad.name: ad}, store=store,
                         runner=Runs() if runner is None else runner,
                         approvers=lambda: frozenset(approvers), **kw), ad


def _msg(text: str, *, channel: str = CHANNEL_FEISHU, msg_id: str = "m1",
         sender: str = ALICE) -> InboundMessage:
    return InboundMessage(channel=channel, chat_id="oc_1", sender=sender,
                          text=text, msg_id=msg_id)


def _refund(r, *, msg_id="m1", sender=ALICE, extra="") -> str:
    return r.handle(_msg(f"/refund {ORDER} 质量问题{extra}", msg_id=msg_id, sender=sender))


# --------------------------------------------------------------------------
# 命令解析
# --------------------------------------------------------------------------
def test_non_command_is_ignored_silently(store):
    r, ad = _router(store)
    assert r.handle(_msg("今天午饭吃什么")) == ""
    assert ad.sent == []                      # 闲聊不该收到用法提示


def test_unknown_command_is_not_taken_over(store):
    r, ad = _router(store)
    assert r.handle(_msg("/deploy prod")) == ""
    assert ad.sent == []


def test_help_returns_usage(store):
    r, _ = _router(store)
    assert "refund" in r.handle(_msg("/help"))


def test_command_parse_rejects_plain_text():
    assert Command.parse("hello") is None
    assert Command.parse("/refund A B").verb == "refund"


# --------------------------------------------------------------------------
# 幂等：三个平台都会重推
# --------------------------------------------------------------------------
def test_duplicate_delivery_is_handled_once(store):
    r, ad = _router(store)
    first = _refund(r, msg_id="same")
    second = _refund(r, msg_id="same")
    assert first and second == ""             # 第二次一声不吭
    assert len(ad.sent) == 1


def test_duplicate_approve_executes_once(store):
    """重推挡在**放行**这一步尤其要紧 —— 那一步是真付款。"""
    runs = Runs()
    r, _ = _router(store, runner=runs)
    _refund(r)
    r.handle(_msg(f"/approve {CASE}", msg_id="a1"))
    r.handle(_msg(f"/approve {CASE}", msg_id="a1"))
    assert len(runs) == 1


def test_distinct_messages_both_handled(store):
    r, ad = _router(store)
    _refund(r, msg_id="a")
    _refund(r, msg_id="b")
    assert len(ad.sent) == 2


def test_message_without_id_is_not_dropped(store, caplog):
    """拿不到 msg_id 时宁可重复处理，也不要静默吞掉。"""
    r, _ = _router(store)
    with caplog.at_level("WARNING"):
        out = r.handle(_msg("/help", msg_id=""))
    assert out and "没有 msg_id" in caplog.text


# --------------------------------------------------------------------------
# /refund 是只读预检
# --------------------------------------------------------------------------
def test_refund_does_not_move_money(store):
    """**这条是整层最重要的断言**：一条群消息不许直接触发付款。"""
    runs = Runs()
    r, _ = _router(store, runner=runs)
    out = _refund(r)
    assert runs == []                         # runner 一次都没被调用
    assert "尚未动任何资金" in out and f"/approve {CASE}" in out


def test_refund_reports_decision_and_basis(store):
    r, _ = _router(store)
    out = _refund(r)
    assert "裁定：" in out and "订单锁定政策 v" in out and "付款至申请" in out


def test_refund_needs_order_and_reason(store):
    r, _ = _router(store)
    out = r.handle(_msg("/refund"))
    assert "至少要给订单号和诉求类型" in out and USAGE.splitlines()[0] in out


def test_unknown_reason_is_refused_not_guessed(store):
    """猜错一个诉求词，套用的就是另一条政策。与 CSV 入口同一份口径。"""
    r, _ = _router(store)
    assert "心情不好" in r.handle(_msg(f"/refund {ORDER} 心情不好"))


def test_unknown_order_is_refused(store):
    r, _ = _router(store)
    assert "底账里没有订单" in r.handle(_msg("/refund ORD-NOT-EXIST 质量问题"))


def test_refund_passes_amount_and_date_through(store):
    runs = Runs()
    r, _ = _router(store, runner=runs)
    _refund(r, extra=" 1234.5 2026-07-10")
    r.handle(_msg(f"/approve {CASE}", msg_id="a1"))

    case = runs[0]["case"]
    assert case["amount_claimed"] == 1234.5
    assert runs[0]["requested_at"].startswith("2026-07-10")
    # 订单号是唯一要人填的钥匙：租户/渠道/SKU 全从底账查出来
    assert case["tenant_id"] and case["sku"]


def test_blank_amount_falls_back_to_order_amount(store):
    runs = Runs()
    r, _ = _router(store, runner=runs)
    _refund(r)
    r.handle(_msg(f"/approve {CASE}", msg_id="a1"))
    assert runs[0]["case"]["amount_claimed"] > 0


def test_repeat_refund_replaces_ticket(store):
    """同一单重发只留一条待办 —— 留两条会让 /approve 不知道批哪一条。"""
    r, _ = _router(store)
    _refund(r, msg_id="a")
    _refund(r, msg_id="b", extra=" 999")
    out = r.handle(_msg("/pending", msg_id="p"))
    assert out.count(CASE) == 1


# --------------------------------------------------------------------------
# 放行才执行
# --------------------------------------------------------------------------
def test_approve_runs_the_case(store):
    runs = Runs()
    r, _ = _router(store, runner=runs)
    _refund(r)
    out = r.handle(_msg(f"/approve {CASE}", msg_id="a1"))
    assert len(runs) == 1
    assert f"已放行 {CASE}" in out and ALICE in out


def test_non_approver_cannot_release(store, caplog):
    """越权尝试要留痕，且**不许**降级成一句用法提示。"""
    runs = Runs()
    r, _ = _router(store, runner=runs, approvers=("ou_boss",))
    _refund(r, sender="ou_intern")
    with caplog.at_level("WARNING"):
        out = r.handle(_msg(f"/approve {CASE}", msg_id="a1", sender="ou_intern"))
    assert "无审批权限" in out
    assert runs == []                         # 一分钱没动
    assert "越权" in caplog.text


def test_requester_cannot_self_approve_unless_listed(store):
    """提交人不会因为「是他自己提的」就获得放行权 —— 只认名单。"""
    runs = Runs()
    r, _ = _router(store, runner=runs, approvers=("ou_boss",))
    _refund(r, sender="ou_sales")
    r.handle(_msg(f"/approve {CASE}", msg_id="a1", sender="ou_sales"))
    assert runs == []


def test_reject_drops_the_ticket(store):
    runs = Runs()
    r, _ = _router(store, runner=runs)
    _refund(r)
    out = r.handle(_msg(f"/reject {CASE} 金额对不上", msg_id="a1"))
    assert "已撤掉待办" in out and "金额对不上" in out
    assert runs == []
    assert "没有待办" in r.handle(_msg(f"/approve {CASE}", msg_id="a2"))


def test_expired_ticket_is_not_released(store):
    """预检结论是按当时的政策与日期算的，隔天再批就是拿旧结论退新钱。"""
    runs = Runs()
    r, _ = _router(store, runner=runs, ticket_ttl=0)
    _refund(r)
    time.sleep(0.01)
    out = r.handle(_msg(f"/approve {CASE}", msg_id="a1"))
    assert "已过期" in out
    assert runs == []


def test_ticket_ttl_default_is_a_day():
    assert TICKET_TTL == 24 * 3600


def test_pending_lists_tickets(store):
    r, _ = _router(store)
    assert "没有待办" in r.handle(_msg("/pending", msg_id="p0"))
    _refund(r)
    out = r.handle(_msg("/pending", msg_id="p1"))
    assert CASE in out and ALICE in out


# --------------------------------------------------------------------------
# 渠道信任级别
# --------------------------------------------------------------------------
def test_external_channel_cannot_approve(store):
    """客户在微信客服窗口里打 /approve —— 这是这一层最贵的错。"""
    bridge_calls = []

    class Bridge:
        def handle_message(self, sender, body):
            bridge_calls.append((sender, body))
            return "已批准"

    ad = FakeAdapter(CHANNEL_WECHAT_KF)
    r = IngressRouter({ad.name: ad}, store=store, approval_bridge=Bridge(),
                      approvers=lambda: frozenset({"wmCustomer"}))
    out = r.handle(_msg("/approve task-1", channel=CHANNEL_WECHAT_KF,
                        sender="wmCustomer"))

    assert "不受理审批命令" in out
    assert bridge_calls == []                 # 连桥都没碰到 —— 名单之外的第二道闸


def test_external_channel_cannot_release_ticket(store):
    """连自己在客服窗口里提交的待办也不许自己放行。"""
    runs = Runs()
    ad = FakeAdapter(CHANNEL_WECHAT_KF)
    r = IngressRouter({ad.name: ad}, store=store, runner=runs,
                      approvers=lambda: frozenset({"wmCustomer"}))
    r.handle(_msg(f"/refund {ORDER} 质量问题", channel=CHANNEL_WECHAT_KF,
                  sender="wmCustomer", msg_id="k1"))
    out = r.handle(_msg(f"/approve {CASE}", channel=CHANNEL_WECHAT_KF,
                        sender="wmCustomer", msg_id="k2"))
    assert "不受理审批命令" in out
    assert runs == []


def test_task_level_approval_goes_to_bridge(store):
    """参数不是待办 -> 原样转给已跑绿的 RoomApprovalBridge，本层不重复判定。"""
    seen = []

    class Bridge:
        def handle_message(self, sender, body):
            seen.append((sender, body))
            return "已批准 task-1（操作人 ou_alice）"

    ad = FakeAdapter()
    r = IngressRouter({ad.name: ad}, store=store, approval_bridge=Bridge(),
                      approvers=lambda: frozenset({ALICE}))
    assert "已批准" in r.handle(_msg("/approve task-1"))
    assert seen == [(ALICE, "/approve task-1")]


def test_ticket_lookup_is_by_table_not_prefix(store):
    """分流靠查表：一个恰好以 RC- 开头的 task_id 不该被当成待办。"""
    seen = []

    class Bridge:
        def handle_message(self, sender, body):
            seen.append(body)
            return "转给桥了"

    ad = FakeAdapter()
    r = IngressRouter({ad.name: ad}, store=store, approval_bridge=Bridge(),
                      approvers=lambda: frozenset({ALICE}))
    r.handle(_msg("/approve RC-NOT-A-TICKET"))
    assert seen == ["/approve RC-NOT-A-TICKET"]


def test_approval_without_bridge_says_so(store):
    r, _ = _router(store)
    assert "没有待办" in r.handle(_msg("/reject task-1 金额不符"))


def test_approvers_are_read_live(store):
    """改审批人不必重启进程（口径同 RoomApprovalBridge）。"""
    names = {"ou_boss"}
    ad = FakeAdapter()
    r = IngressRouter({ad.name: ad}, store=store, runner=Runs(),
                      approvers=lambda: frozenset(names))
    assert not r.is_approver(ALICE)
    names.add(ALICE)
    assert r.is_approver(ALICE)


def test_default_approvers_reads_matrix_env(monkeypatch):
    """名单与 Matrix 房间共用一份 MAOS_APPROVERS，且不吃 matrix-nio 依赖。"""
    from maos.ingress.router import _current_approvers

    monkeypatch.setenv("MAOS_APPROVERS", "ou_alice, @bob:example.org")
    assert _current_approvers() == frozenset({ALICE, "@bob:example.org"})


# --------------------------------------------------------------------------
# 回帖措辞（铁律 8）
# --------------------------------------------------------------------------
def _released(store, result: dict):
    runs = Runs()
    runs.result = result
    r, ad = _router(store, runner=runs)
    _refund(r)
    return r.handle(_msg(f"/approve {CASE}", msg_id="a1")), ad


def test_reply_says_settled_only_when_observed(store):
    out, ad = _released(store, RESULT_SETTLED)
    assert "已到账" in out
    assert ad.sent[-1].text == out


def test_reply_never_claims_paid_without_observation(store):
    """已提交网关但没有 settled 观察 —— **不许**说已退款/已到账。"""
    out, _ = _released(store, RESULT_UNCONFIRMED)
    assert "未确认到账" in out
    assert "已到账" not in out and "已退款" not in out


def test_reply_lists_human_approval_points(store):
    """代跑掉的任务级审批点必须逐条列出，且说清它与群里那次放行**不是同一层**。"""
    out, _ = _released(store, RESULT_UNCONFIRMED)
    assert "任务级审批点 1 个" in out and "主管审批" in out
    assert "不是同一层" in out


def test_runner_failure_is_reported_not_swallowed(store):
    def boom(payload, **kw):
        raise RuntimeError("网关炸了")

    r, _ = _router(store, runner=boom)
    _refund(r)
    out = r.handle(_msg(f"/approve {CASE}", msg_id="a1"))
    assert "处理失败" in out and "网关炸了" in out


def test_send_failure_does_not_lose_the_result(store, caplog):
    """回帖失败不能把已经发生的处置一起丢掉。"""

    class Broken(FakeAdapter):
        def send(self, msg):
            raise RuntimeError("飞书 429")

    r, _ = _router(store, adapter=Broken())
    with caplog.at_level("ERROR"):
        out = _refund(r)
    assert out and "回帖失败" in caplog.text and "预检" in caplog.text


def test_kf_reply_carries_open_kfid(store):
    """微信客服回信必须带 open_kfid，它只在入站那条消息里有。"""
    ad = FakeAdapter(CHANNEL_WECHAT_KF)
    r = IngressRouter({ad.name: ad}, store=store)
    r.handle(InboundMessage(channel=CHANNEL_WECHAT_KF, chat_id="wmA", sender="wmA",
                            text="/help", msg_id="k1", raw={"open_kfid": "wk_1"}))
    assert ad.sent[0].meta["open_kfid"] == "wk_1"


def test_describe_config_never_prints_values():
    from maos.ingress.router import describe_config
    from maos.ingress.server import build_adapters

    text = describe_config(build_adapters(dict(FEISHU_ENV)))
    assert "secret-demo" not in text and "vtoken-demo" not in text
    assert json.loads(text)[CHANNEL_FEISHU] == "configured"


# --------------------------------------------------------------------------
# Webhook 服务面
# --------------------------------------------------------------------------
class _Collector:
    def __init__(self) -> None:
        self.seen: list[InboundMessage] = []
        self.done = threading.Event()

    def handle(self, msg: InboundMessage) -> str:
        self.seen.append(msg)
        self.done.set()
        return ""


@pytest.fixture()
def live_server():
    collector = _Collector()
    srv = IngressServer({CHANNEL_FEISHU: _feishu()}, collector,
                        host="127.0.0.1", port=0)
    srv.start()
    srv.port = srv._httpd.server_address[1]
    threading.Thread(target=srv._httpd.serve_forever, daemon=True).start()
    yield srv, collector
    srv.shutdown()


def _post(srv, path: str, body: bytes, headers: dict | None = None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{srv.port}{path}", data=body, method="POST",
        headers={"Content-Type": "application/json", **(headers or {})})
    return urllib.request.urlopen(req, timeout=3)


def test_healthz(live_server):
    srv, _ = live_server
    with urllib.request.urlopen(f"http://127.0.0.1:{srv.port}/healthz", timeout=3) as r:
        assert json.loads(r.read())["ok"] is True


def test_valid_event_is_queued_and_answered_immediately(live_server):
    """**先回 200 再干活**：响应不等 router 跑完。"""
    srv, collector = live_server
    started = time.time()
    resp = _post(srv, "/ingress/feishu", json.dumps(_message_event("/help")).encode())
    assert resp.status == 200
    assert time.time() - started < 2
    assert collector.done.wait(3)
    assert collector.seen[0].text == "/help"


def test_bad_token_is_401_with_empty_body(live_server):
    """拒收的原因只进日志 —— 告诉对面是签名错还是时间戳错，等于给爆破一个进度条。"""
    srv, collector = live_server
    body = json.dumps({**_message_event(), "token": "wrong"}).encode()
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(srv, "/ingress/feishu", body)
    assert exc.value.code == 401
    assert exc.value.read() == b""
    assert collector.seen == []


def test_url_verification_answers_challenge(live_server):
    srv, _ = live_server
    body = json.dumps({"token": "vtoken-demo", "type": "url_verification",
                       "challenge": "cha-1"}).encode()
    with _post(srv, "/ingress/feishu", body) as resp:
        assert json.loads(resp.read())["challenge"] == "cha-1"


def test_unconfigured_channel_is_503_not_404(live_server):
    """地址对、我方没配好 —— 503 才指向正确的排查方向。"""
    srv, _ = live_server
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(srv, "/ingress/wecom", b"{}")
    assert exc.value.code == 503


def test_unknown_path_is_404(live_server):
    srv, _ = live_server
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(srv, "/nope", b"{}")
    assert exc.value.code == 404


def test_oversized_body_is_rejected(live_server):
    """公网面必须有体积上限，且它在签名校验**之前**生效。

    断言的是**结果**（没进队列），不是状态码：服务端拒绝时不把剩下的 body 读完，
    客户端因此可能收到 413，也可能在写入途中撞上 RST —— 两种都算挡住了。
    """
    srv, collector = live_server
    with pytest.raises((urllib.error.HTTPError, urllib.error.URLError)) as exc:
        _post(srv, "/ingress/feishu", b"x" * (MAX_BODY + 1))
    if isinstance(exc.value, urllib.error.HTTPError):
        assert exc.value.code == 413
    assert collector.seen == []


def test_routes_cover_all_three_channels():
    assert set(ROUTES.values()) == {CHANNEL_FEISHU, CHANNEL_WECOM, CHANNEL_WECHAT_KF}


# --------------------------------------------------------------------------
# 圆桌钩子（T88）—— 旁路观察者：回帖之后触发、锁之外触发、抛了也不改回帖
# --------------------------------------------------------------------------
class FakeTeam:
    """`TeamObserver` 的假件：记下四个方法各拿到什么 kwargs。

    ``order`` 传进来就与 adapter 共用一份调用顺序 —— 「回帖先出房间、圆桌后说话」
    这件事只能靠一份共享的顺序表钉住，各记各的看不出谁先谁后。
    """

    #: 契约 §1.4 的 roster 形状。两岗足够分辨「有独立账号」与「代言」两条渲染路径。
    ROSTER = [
        {"agent_id": "refund-intake", "title": "申请受理岗", "role": "refund_intake",
         "duty": "受理退款申请，核对订单要素与随案证据",
         "user_id": "@maos-bot:maos.local", "own_identity": False,
         "skills": [{"name": "refund.intake", "version": "1.0.0",
                     "purpose": "把客户诉求变成一条可裁定的申请"}]},
        {"agent_id": "refund-finance", "title": "财务执行岗", "role": "refund_finance",
         "duty": "核算核准金额并执行付款",
         "user_id": "@maos-finance:maos.local", "own_identity": True,
         "skills": [{"name": "finance.settle", "version": "1.0.0",
                     "purpose": "按政策算出核准金额"}]},
    ]

    def __init__(self, order: list | None = None, *, boom: str = "") -> None:
        self.calls: list[dict] = []
        self.order = [] if order is None else order
        self.boom = boom

    def _log(self, kind: str, kw: dict) -> list:
        self.order.append(kind)
        self.calls.append({"kind": kind, **kw})
        if self.boom == kind:
            raise RuntimeError(f"{kind} 钩子炸了")
        return []

    def on_preflight(self, **kw) -> list:
        return self._log("preflight", kw)

    def on_sheet(self, **kw) -> list:
        return self._log("sheet", kw)

    def on_execute(self, **kw) -> list:
        return self._log("execute", kw)

    def roster(self) -> list[dict]:
        return [dict(seat) for seat in self.ROSTER]

    def last(self, kind: str) -> dict:
        return [c for c in self.calls if c["kind"] == kind][-1]


def _second_store() -> SqliteStore:
    """第二个 router 要第二个库 —— 共用一个的话去重会把第二条 /refund 吞掉。"""
    s = SqliteStore(":memory:")
    s.init_schema()
    return s


SHEET_CSV = ("订单号,诉求类型,申报金额,申请日期\n"
             f"{ORDER},质量问题,6800,2026-07-10\n"
             "ORD-9999-9999,质量问题,500,2026-07-10\n").encode("utf-8")


class SheetAdapter(FakeAdapter):
    """带 ``fetch`` 的 adapter —— 申请表要先取件、按内容认出来才走 handle_sheet。"""

    def __init__(self, blob: bytes) -> None:
        super().__init__()
        self.blob = blob

    def fetch(self, att) -> bytes:
        return self.blob


def test_router_without_team_answers_refund_exactly_as_before(store):
    """缺省不接圆桌：回帖与接了圆桌时逐字相同，且一次钩子都不发生。"""
    lone, ad_lone = _router(store)
    team = FakeTeam()
    teamed, _ = _router(_second_store(), team=team)

    assert _refund(lone) == _refund(teamed)
    assert len(ad_lone.sent) == 1                     # 回帖一条，没有第二条来自圆桌
    assert lone._pending() == []                      # 没装圆桌就一件事都不登记
    assert [c["kind"] for c in team.calls] == ["preflight"]


def test_preflight_hook_fires_after_the_reply_with_the_same_case_id(store):
    """回帖先出房间、圆桌后说话 —— 处置结论不该等五次模型调用。"""
    order: list[str] = []

    class Recording(FakeAdapter):
        def send(self, msg: OutboundMessage) -> None:
            order.append("send")
            super().send(msg)

    team = FakeTeam(order)
    r, _ = _router(store, adapter=Recording(), team=team)
    reply = _refund(r)

    assert order == ["send", "preflight"]
    call = team.last("preflight")
    assert set(call) == {"kind", "payload", "checked", "ledger",
                         "evidence", "requested_by"}
    assert call["checked"]["case_id"] == CASE and CASE in reply
    assert call["requested_by"] == ALICE and call["evidence"] == []
    assert call["payload"]["case"] and call["ledger"]["order_snapshot"]


def test_team_exception_does_not_change_the_reply(store, caplog):
    """圆桌炸了只记 WARNING：它是旁路，不该带走「这一单批了没有」这个结论。"""
    lone, _ = _router(store)
    boom, ad = _router(_second_store(), team=FakeTeam(boom="preflight"))
    with caplog.at_level("WARNING", logger="maos.ingress.router"):
        angry = _refund(boom)

    assert angry == _refund(lone)
    assert len(ad.sent) == 1
    assert "圆桌 preflight 钩子失败" in caplog.text and "钩子炸了" in caplog.text


def test_execute_hook_gets_result_and_operator_outside_the_lock(store):
    """放行的钩子必须在锁外跑：runner 在锁内，钩子里再取一次锁就是自锁死。"""
    seen: dict = {}

    class LockPeeking(FakeTeam):
        router = None

        def on_execute(self, **kw) -> list:
            seen["free"] = self.router._lock.acquire(blocking=False)
            if seen["free"]:
                self.router._lock.release()
            return super().on_execute(**kw)

    team = LockPeeking()
    r, _ = _router(store, team=team)
    team.router = r
    _refund(r)
    out = r.handle(_msg(f"/approve {CASE}", msg_id="m2"))

    assert f"已放行 {CASE}" in out
    call = team.last("execute")
    assert set(call) == {"kind", "payload", "result", "operator"}
    assert call["result"] == RESULT_SETTLED and call["operator"] == ALICE
    assert seen["free"] is True


def test_sheet_hook_fires_once_with_one_row_per_line(store, tmp_path):
    """一张表说一次、行全给出去 —— 50 行 x 5 岗 = 250 条会把房间刷爆。"""
    from maos.ingress.attachments import AttachmentBuffer, AttachmentStore
    from maos.ingress.contracts import Attachment

    team = FakeTeam()
    ad = SheetAdapter(SHEET_CSV)
    r = IngressRouter({ad.name: ad}, store=store, runner=Runs(),
                      approvers=lambda: frozenset({ALICE}), team=team,
                      attachment_store=AttachmentStore(tmp_path),
                      attachment_buffer=AttachmentBuffer())
    r.handle(InboundMessage(
        channel=CHANNEL_FEISHU, chat_id="oc_1", sender=ALICE, text="", msg_id="s1",
        attachments=(Attachment(channel=CHANNEL_FEISHU, file_key="k1", kind="file",
                                filename="requests.csv", mime="text/csv"),)))

    assert [c["kind"] for c in team.calls] == ["sheet"]
    call = team.last("sheet")
    assert set(call) == {"kind", "rows", "ledger", "requested_by"}
    assert call["requested_by"] == ALICE
    rows = call["rows"]
    assert len(rows) == 2
    good, bad = rows
    assert set(good) == {"line", "order_id", "reason_raw", "payload", "checked",
                         "error", "problems", "warnings"}
    assert good["order_id"] == ORDER and good["checked"]["case_id"] == CASE
    assert good["payload"]["case"] and good["problems"] == []
    # 坏行压根没走到预检，三者一个都不许瞎填
    assert bad["problems"] and bad["checked"] is None and bad["payload"] is None
    assert bad["error"] is None


def test_team_command_renders_roster_without_calling_the_model(store):
    """/team 是只读的自我介绍：名单来自常量与注册表，一次模型都不调（铁律 8）。"""
    from maos.ingress.chat import ChatResponder
    from maos.tests.test_ingress_chat import _EchoModel

    model = _EchoModel()
    r, _ = _router(store, team=FakeTeam(), chat=ChatResponder(model))
    out = r.handle(_msg("/team"))

    assert "申请受理岗（refund-intake）" in out
    assert "财务执行岗（refund-finance）" in out
    assert "refund.intake@1.0.0" in out and "finance.settle@1.0.0" in out
    assert "受理退款申请" in out                        # duty 那一行
    assert "由 maos-bot 代言" in out                    # 没独立账号的那一岗要说明白
    assert "@maos-finance:maos.local" in out           # 有独立账号的报 mxid
    assert model.calls == []


def test_team_command_without_team_says_so(store):
    """没接圆桌就明说，不装作有；外部渠道也答得上来 —— 它不碰钱也不碰待办。"""
    lone, _ = _router(store)
    assert "没接圆桌" in lone.handle(_msg("/team"))

    kf, ad = _router(_second_store(), adapter=FakeAdapter(CHANNEL_WECHAT_KF))
    out = kf.handle(_msg("/team", channel=CHANNEL_WECHAT_KF))
    assert "没接圆桌" in out and len(ad.sent) == 1
    assert "不受理审批命令" not in out                  # 不吃 ALLOW_APPROVAL 那道闸
