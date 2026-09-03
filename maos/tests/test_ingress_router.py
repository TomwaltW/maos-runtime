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

from types import SimpleNamespace

import pytest

from maos.core.store import SqliteStore
from maos.ingress.contracts import (
    CHANNEL_FEISHU, CHANNEL_WECHAT_KF, CHANNEL_WECOM, InboundMessage, OutboundMessage,
)
from maos.ingress.router import (
    SELF_INTRO_Q, Command, IngressRouter, TICKET_TTL, USAGE, parse_mention,
)
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


#: 接了圆桌之后 ``/refund`` 回帖末尾多的那一句（T92）。它只是**预告** ——
#: 此刻一岗都还没发言，所以措辞里不许有任何结论。
TEAM_NOTE = "\n五岗正在合议，稍后给出批复建议"


def test_router_without_team_answers_refund_exactly_as_before(store):
    """缺省不接圆桌：回帖与从前逐字一致，且一次钩子都不发生。

    接了圆桌只在**末尾**多一句预告 —— 裁定 / 依据 / 金额 / 下一步那四行一个字
    都不许变（`docs/matrix-room-runbook.md` 与这里的措辞测试都在依赖它们）。
    """
    lone, ad_lone = _router(store)
    team = FakeTeam()
    teamed, _ = _router(_second_store(), team=team)

    alone = _refund(lone)
    assert _refund(teamed) == alone + TEAM_NOTE
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

    # 预告那一句只看「接没接圆桌」，与圆桌跑没跑成无关 —— 它在钩子触发之前就
    # 写进回帖了。炸掉的是钩子，不是这一层的结论。
    assert angry == _refund(lone) + TEAM_NOTE
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


# --------------------------------------------------------------------------
# @岗位点名（T92）—— 2026-09-03 真房间实测：boss @ 了「财务执行岗」，
# 回话的却是 maos-bot 的通用话术。这一节钉的就是那个现象的第二层成因。
# --------------------------------------------------------------------------
FINANCE = "refund-finance"
FINANCE_TITLE = "财务执行岗"


class AnsweringTeam(FakeTeam):
    """装了岗位问答的圆桌假件（跨轨契约 §4 的 `answer`）。

    ``boom`` 与 ``said`` 各自钉一条退化路径：抛异常、回空话。两者都必须落回闲聊
    而不是沉默 —— 房间里的空回帖与「机器人挂了」分不出来。
    """

    def __init__(self, said: str = "我负责核算核准金额并执行付款，只做预演不动钱",
                 *, blow: str = "", **kw) -> None:
        super().__init__(**kw)
        self.said = said
        self.blow = blow
        self.asked: list[dict] = []

    def answer(self, agent_id: str, question: str, *, facts: str = "") -> str:
        self.asked.append({"agent_id": agent_id, "question": question, "facts": facts})
        if self.blow:
            raise RuntimeError(self.blow)
        return self.said


class DecidingTeam(FakeTeam):
    """带合议引擎的圆桌假件：`on_preflight` 交回报告，`decide` 出一张收口卡。

    收口卡用 `SimpleNamespace` 顶替 `Verdict` —— 这一层只读 ``headline``，
    把真件搬进测试只会让本轨依赖另一轨的落地时刻。
    """

    HEADLINE = "建议批复 · 核准预演 6800.00 · 请 supervisor 拍板"

    def __init__(self, headline: str = HEADLINE, *, blow: str = "", **kw) -> None:
        super().__init__(**kw)
        self.headline = headline
        self.blow = blow
        self.decided: list[dict] = []

    def on_preflight(self, **kw) -> list:
        super().on_preflight(**kw)
        return [{"agent_id": "refund-policy"}, {"agent_id": "refund-finance"}]

    def decide(self, reports, *, case_id: str = ""):
        self.decided.append({"reports": reports, "case_id": case_id})
        if self.blow:
            raise RuntimeError(self.blow)
        return SimpleNamespace(headline=self.headline, recommend="approve")


def _chatty(answer: str = "我是 MAOS 退款助手"):
    """一个真回话的 `ChatResponder`。**显式注入模型**：无参 `ChatResponder()` 会按
    环境变量选客户端，而 `conftest` 不清 ``MAOS_LLM_*`` —— 这台机器上会真打 DeepSeek
    （跨轨契约 §6 R8）。"""
    from maos.ingress.chat import ChatResponder
    from maos.tests.test_ingress_chat import _EchoModel

    return ChatResponder(_EchoModel(answer))


# -- 解析：三种形态，都只在句首 --------------------------------------------
def test_mention_parses_the_element_pill_form():
    """**主路**。`on_message` 只给纯文本 body，Element 的 @提及 pill 到这一层
    就是一串显示名 —— 真房间里 boss 那条消息长的就是这个样子。"""
    assert parse_mention("财务执行岗: 你是干什么的") == (FINANCE, "你是干什么的")
    assert parse_mention("规则审核岗，这单为什么批") == ("refund-policy", "这单为什么批")


def test_mention_parses_the_typed_at_form():
    assert parse_mention("@财务执行岗 你是干什么的") == (FINANCE, "你是干什么的")
    assert parse_mention("@ 申请受理岗  这单收到了吗") == ("refund-intake", "这单收到了吗")


def test_mention_parses_the_typed_mxid_form():
    """mxid 只比 localpart，**不比 homeserver**：换一套部署域名就换了，
    在这里猜一个去比，症状是点名静默失效。"""
    assert parse_mention("@maos-finance:maos.local 在吗") == (FINANCE, "在吗")
    assert parse_mention("@maos-risk:example.org 风险几档") == ("refund-risk", "风险几档")
    assert parse_mention("@boss:maos.local 你看看这单") is None      # 不是岗位账号


def test_a_title_in_mid_sentence_is_not_a_mention():
    """「我问一下财务执行岗的意见」是在跟**人**说话。判成点名，房间里就会冒出
    一个没人叫过的岗位来答话，而发话的人不知道自己招了谁。"""
    assert parse_mention("我问一下财务执行岗的意见") is None
    assert parse_mention("财务执行岗位调整了吗") is None              # 显示名只是前缀
    assert parse_mention("") is None and parse_mention("   ") is None


# -- 分流：命中就让那一岗回话 ------------------------------------------------
def test_mentioned_seat_answers_with_its_own_nameplate(store):
    """回帖仍由主通道发出，靠名牌区分是谁在说 —— 没有名牌，房间里看到的还是
    bot 在自问自答，那正是这一层要消灭的观感。"""
    team = AnsweringTeam()
    r, ad = _router(store, team=team, chat=_chatty())
    out = r.handle(_msg("财务执行岗: 你是干什么的"))

    assert out.startswith(f"【{FINANCE_TITLE} · {FINANCE}】 ")
    assert team.said in out and ad.sent[-1].text == out


def test_bare_mention_gets_a_self_introduction(store):
    """只 @ 了一下、一个字没说，也**不许沉默** —— 那与「这个岗位不在」分不出来。"""
    team = AnsweringTeam()
    r, ad = _router(store, team=team)
    out = r.handle(_msg("@财务执行岗"))

    assert out and len(ad.sent) == 1
    asked, = team.asked
    assert asked["agent_id"] == FINANCE and asked["question"] == SELF_INTRO_Q


def test_mention_hands_the_question_and_the_facts_to_the_seat(store):
    """问题去掉称呼、事实与闲聊同源 —— 岗位答话也只许依据本进程算好的事实。"""
    team = AnsweringTeam()
    r, _ = _router(store, team=team)
    _refund(r, msg_id="m0")
    r.handle(_msg("财务执行岗：这单能退多少", msg_id="m1"))

    asked = team.asked[-1]
    assert asked["question"] == "这单能退多少"
    assert "可用命令：" in asked["facts"] and CASE in asked["facts"]


def test_a_command_never_enters_the_mention_branch(store):
    """``/refund`` 的参数里出现「财务执行岗」四个字是完全可能的。先判点名，
    群里看到的就是「命令没生效」，而没有任何报错。"""
    team = AnsweringTeam()
    r, _ = _router(store, team=team, chat=_chatty())
    out = r.handle(_msg(f"/refund {ORDER} 质量问题 6800 2026-07-10 财务执行岗"))

    assert out.startswith("预检 · ") and team.asked == []


# -- 三条退化路径：一律有回音 -------------------------------------------------
def test_mention_without_a_team_falls_back_to_chat_with_a_voice(store):
    """没接圆桌：退回闲聊，但要说明白是谁在代答 —— **不许**挂着岗位名牌，
    那等于让 bot 冒充那一岗说话。"""
    r, ad = _router(store, chat=_chatty("底账里有三张订单，发 /refund 起单"))
    out = r.handle(_msg("财务执行岗: 你是干什么的"))

    assert "没接圆桌" in out and "退款助手代答" in out
    assert "底账里有三张订单" in out
    assert not out.startswith("【") and len(ad.sent) == 1


def test_mention_without_an_answer_entry_falls_back_to_chat(store):
    """圆桌接了、但没装岗位问答（本轨基线就是这样）：照样落回闲聊并点破。"""
    r, ad = _router(store, team=FakeTeam(), chat=_chatty())
    out = r.handle(_msg("财务执行岗: 你是干什么的"))

    assert "没装载岗位问答" in out and "我是 MAOS 退款助手" in out
    assert not out.startswith("【") and len(ad.sent) == 1


def test_mention_answer_exception_falls_back_to_chat(store, caplog):
    """`answer()` 抛了只记 WARNING：一次模型抖动不该让房间里彻底没回音。"""
    r, ad = _router(store, team=AnsweringTeam(blow="模型网关 502"), chat=_chatty())
    with caplog.at_level("WARNING", logger="maos.ingress.router"):
        out = r.handle(_msg("财务执行岗: 你是干什么的"))

    assert "没答上来" in out and "我是 MAOS 退款助手" in out
    assert "RuntimeError" in out and len(ad.sent) == 1
    assert "问答失败" in caplog.text and "退回闲聊" in caplog.text


def test_mention_empty_answer_falls_back_instead_of_an_empty_nameplate(store):
    """空回答单独判：它不抛异常，照发就是一条只有名牌、没有话的消息。"""
    r, _ = _router(store, team=AnsweringTeam("   "), chat=_chatty())
    out = r.handle(_msg("财务执行岗: 你是干什么的"))
    assert "没答上来" in out and not out.startswith("【")


def test_mention_fallback_speaks_even_without_a_chat_responder(store):
    """连回话器都没装也得有回音 —— **空回帖不是选项**（模块承诺不会沉默）。"""
    r, ad = _router(store, team=FakeTeam())
    out = r.handle(_msg("财务执行岗: 你是干什么的"))

    assert out and len(ad.sent) == 1
    assert "/team" in out and "/help" in out


# -- 待办里的合议建议 ---------------------------------------------------------
def test_pending_carries_the_verdict_headline(store):
    """预检之后，`/pending` 每条待办多一行「建议：…」，逐字来自收口卡。"""
    team = DecidingTeam()
    r, _ = _router(store, team=team)
    _refund(r, msg_id="m0")
    out = r.handle(_msg("/pending", msg_id="m1"))

    assert f"    建议：{DecidingTeam.HEADLINE}" in out
    assert CASE in out
    decided, = team.decided
    assert decided["case_id"] == CASE and len(decided["reports"]) == 2


def test_pending_omits_the_advice_line_without_a_verdict(store):
    """没有收口卡就整行不出现 —— 不打「建议：None」，也不自己编一句。"""
    r, _ = _router(store, team=FakeTeam())
    _refund(r, msg_id="m0")
    out = r.handle(_msg("/pending", msg_id="m1"))

    assert CASE in out and "建议：" not in out and "None" not in out


def test_a_broken_decide_leaves_the_ticket_without_advice(store, caplog):
    """合议引擎炸了只少一行建议，待办本身一条都不许少（红线 R4）。"""
    r, _ = _router(store, team=DecidingTeam(blow="真值表炸了"))
    _refund(r, msg_id="m0")
    with caplog.at_level("WARNING", logger="maos.ingress.router"):
        out = r.handle(_msg("/pending", msg_id="m1"))

    assert CASE in out and "建议：" not in out
    assert "收口卡没算出来" in caplog.text


def test_refund_reply_keeps_its_first_four_lines(store):
    """裁定 / 依据 / 金额 那几行有测试与 runbook 在依赖，一个字都不许动。"""
    lone, _ = _router(store)
    teamed, _ = _router(_second_store(), team=FakeTeam())

    alone, with_team = _refund(lone), _refund(teamed)
    assert with_team.splitlines()[:4] == alone.splitlines()[:4]
    assert with_team.splitlines()[-1] == "五岗正在合议，稍后给出批复建议"


# -- 措辞守卫（铁律 8 / 跨轨契约 R2）------------------------------------------
FORBIDDEN = ("已批准", "已退款", "已到账", "已放款", "已完成")


def test_mention_and_advice_wording_never_claims_finality(store):
    """MAOS 不持有权威事实：房间里只能说「建议」与「预演」，钱有没有到账
    归外部系统。这三处新增措辞里出现任何一个终态词都是回归。"""
    team = DecidingTeam(headline="需补件后再议 · 证据：缺 image 一份")
    r, _ = _router(store, team=team, chat=_chatty())
    said = [_refund(r, msg_id="m0"),
            r.handle(_msg("/pending", msg_id="m1")),
            r.handle(_msg("财务执行岗: 你是干什么的", msg_id="m2")),
            r.handle(_msg("@maos-finance:maos.local", msg_id="m3"))]

    for text in said:
        assert text
        for word in FORBIDDEN:
            assert word not in text, f"{word} 出现在回帖里：{text}"
