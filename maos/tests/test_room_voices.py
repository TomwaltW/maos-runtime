"""五岗发声面的行为测试（T84）—— `hiclaw/room_voices.py` 的可执行版本。

这些断言守的是四件在演示当天最容易出事、且出事时症状离原因最远的东西：

1. **两种形态逐字对契约**（跨轨契约 §1.3）。独立账号不加名牌、代言加名牌；两份
   都必须 `html.escape`。`ap_room.render_speech` 就是没做转义的那一版 —— 模型吐出
   一个 `<` 或 `&`，`formatted_body` 当场破掉，而 Element 只是**默默少显示一段**。
2. **缺号退化，不抛。** 缺 env、token 失效、号没进房间，一律退成代言。这条一旦破，
   症状是常驻入口起不来，而原因写在五层调用之下。
3. **绝不 listen（红线 R3）。** send-only 通道一旦 sync，`maos-bot` 与岗位号就会
   互相接龙刷屏。这里直接查假 client 的调用流水里有没有 `sync` / `sync_forever`。
4. **token 不进任何输出**（铁律 6 / 红线 R5）。灌一个哨兵串进 env，反查 `describe()`
   与日志。**安全断言，不是格式断言** —— 它变红意味着真 token 会随回执与日志外流。

假 nio 是本文件自造的最小一份（四个符号够 send-only 走完），不 import
`test_matrix_bus.py` 的内部件：那边 1500 多行、假件为**监听**路径而生（历史派发、
hang_sync、附件三类事件），本文件一个都用不上，跨文件借用只会把两处形态绑死。
不需要 Synapse。
"""

from __future__ import annotations

import sys
import types

import pytest

import hiclaw.room_voices as room_voices
from hiclaw.room_voices import (REDACTED, RoomVoices, env_keys_of, open_voices)

#: 只在本文件里出现的哨兵。别换成 "secret" 之类的常见词 —— 那种词可能因为别的原因
#: 出现在输出里，断言就失去了指向性。
SENTINEL_TOKEN = "sentinel-token-t84-4b1e9d"

BOT_MXID = "@maos-bot:maos.local"
INTAKE_MXID = "@maos-intake:maos.local"
POLICY_MXID = "@maos-policy:maos.local"

TEAM = ("refund-intake", "refund-policy", "refund-evidence",
        "refund-risk", "refund-finance")
TITLES = {"refund-intake": "申请受理岗", "refund-policy": "规则审核岗",
          "refund-evidence": "证据核验岗", "refund-risk": "风险反欺诈岗",
          "refund-finance": "财务执行岗"}


@pytest.fixture(autouse=True)
def _no_ambient_room_bots(monkeypatch):
    """`MAOS_ROOM_BOTS` **不在** `conftest.py` 的 `MATRIX_ENV_VARS` 里（那是共享文件，
    不在本轨白名单）。`open_channel` 会现读它 —— 在 source 过 `~/.maos-matrix/agents.env`
    的 shell 里跑测试，真名单会被带进假通道。这里自己剥一次。
    """
    monkeypatch.delenv("MAOS_ROOM_BOTS", raising=False)


# -- 假件 -------------------------------------------------------------------
class _Channel:
    """主通道假件。形状对齐 `MirrorChannel`：send / close。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.closed = False

    def send(self, plain: str, html: str) -> None:
        self.sent.append((plain, html))

    def close(self) -> None:
        self.closed = True


class _FakeWhoamiError(Exception):
    """独立类型，供 `_verify_identity` 的 isinstance 判定用。"""

    def __init__(self, status_code: str = "M_UNKNOWN_TOKEN",
                 message: str = "Invalid access token") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class _FakeWhoamiResponse:
    def __init__(self, user_id: str) -> None:
        self.user_id = user_id
        self.device_id = "DEVICE"


class _FakeStateError(Exception):
    """`room_get_state_event` 的 404 分支。`encryption_verdict` 按 `status_code` 判。"""

    def __init__(self, status_code: str = "M_NOT_FOUND",
                 message: str = "Event not found.") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class _FakeSendError(Exception):
    def __init__(self, message: str = "send failed") -> None:
        super().__init__(message)
        self.message = message


class _FakeAsyncClient:
    """够 send-only 路径跑完的假客户端：whoami -> 查加密 -> room_send -> close。

    `sync` / `sync_forever` / `add_event_callback` 三个方法**故意留着**并记进
    `calls` —— 它们一旦被调，`test_independent_channels_never_listen` 立刻变红。
    删掉它们只会得到 AttributeError，那条断言就退化成「碰巧没这个方法」。
    """

    instances: list["_FakeAsyncClient"] = []
    whoami_result: object = None
    state_result: object = None

    def __init__(self, homeserver: str, user: str) -> None:
        self.homeserver = homeserver
        self.user = user
        self.user_id = ""              # 与真 nio 一致：whoami 之前是空串
        self.access_token = ""
        self.calls: list[str] = []
        self.sent: list[dict] = []
        self.closed = False
        type(self).instances.append(self)

    async def whoami(self):
        self.calls.append("whoami")
        return type(self).whoami_result

    async def room_get_state_event(self, room_id, event_type, state_key=""):
        self.calls.append(f"state:{event_type}")
        return type(self).state_result

    async def room_send(self, room_id, message_type, content, **kw):
        self.calls.append("room_send")
        self.sent.append(content)
        return object()

    async def sync(self, *a, **kw):                     # pragma: no cover —— 不该被调
        self.calls.append("sync")

    async def sync_forever(self, *a, **kw):             # pragma: no cover —— 不该被调
        self.calls.append("sync_forever")

    def add_event_callback(self, cb, event_class):      # pragma: no cover —— 不该被调
        self.calls.append("add_event_callback")

    async def close(self):
        self.closed = True


@pytest.fixture
def fake_nio(monkeypatch):
    """把最小假 nio 塞进 `sys.modules`。

    `_NioChannel` 的 import 全是**方法内惰性 import**，所以换掉 `sys.modules` 就够 ——
    这也是为什么这几条用例在装了真 matrix-nio 的 venv 里同样走假实现，不出网。
    """
    module = types.ModuleType("nio")
    module.AsyncClient = _FakeAsyncClient
    module.WhoamiError = _FakeWhoamiError
    module.RoomGetStateEventError = _FakeStateError
    module.RoomSendError = _FakeSendError
    monkeypatch.setitem(sys.modules, "nio", module)

    _FakeAsyncClient.instances = []
    _FakeAsyncClient.whoami_result = _FakeWhoamiResponse(INTAKE_MXID)
    _FakeAsyncClient.state_result = _FakeStateError()
    yield _FakeAsyncClient
    _FakeAsyncClient.instances = []


def _env(**agents: tuple[str, str]) -> dict[str, str]:
    """一份「房间配齐了」的 env；`agents` 逐岗给 `(mxid, token)`，不给的就是缺号。"""
    env = {"MATRIX_HOMESERVER": "http://localhost:8008",
           "MATRIX_ROOM_ID": "!room:maos.local",
           "MATRIX_USER": BOT_MXID,
           "MATRIX_TOKEN": "bot-token-not-a-sentinel"}
    for agent_id, (mxid, token) in agents.items():
        user_key, token_key = env_keys_of(agent_id.replace("_", "-"))
        env[user_key] = mxid
        env[token_key] = token
    return env


# -- 键名推导 ---------------------------------------------------------------
def test_env_keys_of_derives_upper_snake_keys():
    """`agent_id` -> 两个 env 键名。全仓只此一份推导（建号脚本那份 shell 与它同源）。"""
    assert env_keys_of("refund-intake") == ("MAOS_AGENT_REFUND_INTAKE_USER",
                                            "MAOS_AGENT_REFUND_INTAKE_TOKEN")
    assert env_keys_of("refund-finance") == ("MAOS_AGENT_REFUND_FINANCE_USER",
                                             "MAOS_AGENT_REFUND_FINANCE_TOKEN")


# -- 两种发言形态 -----------------------------------------------------------
def test_voice_with_own_account_sends_without_nameplate(fake_nio):
    """四键齐 -> 用自己的账号发、**不加名牌**（账号显示名就是岗位名）。"""
    main = _Channel()
    voices = open_voices(main, agent_ids=("refund-intake",), titles=TITLES,
                         env=_env(refund_intake=(INTAKE_MXID, SENTINEL_TOKEN)))
    try:
        voice = voices.voice("refund-intake")
        assert voice.own_identity is True
        assert voice.user_id == INTAKE_MXID and voice.title == "申请受理岗"

        voice.say("订单已受理，随案证据 2 份")
        client = fake_nio.instances[-1]
        assert client.access_token == SENTINEL_TOKEN, "token 没被装进这一岗自己的客户端"
        assert [c["body"] for c in client.sent] == ["订单已受理，随案证据 2 份"]
        assert client.sent[0]["formatted_body"] == "<p>订单已受理，随案证据 2 份</p>"
        assert client.sent[0]["msgtype"] == "m.notice"
        assert main.sent == [], "独立账号的话不该经主通道 —— 那样房间里还是一个头像"
    finally:
        voices.close()


def test_voice_without_credentials_falls_back_to_main_channel_with_nameplate():
    """缺 USER/TOKEN -> 经主通道代言，形态逐字对契约 §1.3 的名牌那一份。"""
    main = _Channel()
    voices = open_voices(main, agent_ids=("refund-policy",), titles=TITLES, env=_env())

    voice = voices.voice("refund-policy")
    assert voice.own_identity is False
    assert voice.user_id == BOT_MXID, "代言时用的是主通道那个 mxid"

    voice.say("按 AS-003@v1 全额退")
    assert main.sent == [
        ("【规则审核岗 · refund-policy】 按 AS-003@v1 全额退",
         "<p><strong>规则审核岗</strong> <code>refund-policy</code>"
         "<br/>按 AS-003@v1 全额退</p>")]
    voices.close()


def test_voice_escapes_html_in_both_modes(fake_nio):
    """两种形态的 html 都必须转义；plain 保持原样。

    `_NioChannel.send` 把 `formatted_body` 原样塞进去不转义，而模型复述事实时
    完全可能吐出 `<` 或 `&`（金额区间、"A&B 公司"）。不转义的症状不是报错，
    是 Element **默默吞掉**从那个尖括号起的一段 —— 房间里看着像话说了一半。
    """
    raw = "对比 <b>实付</b> & 申报"
    escaped = "对比 &lt;b&gt;实付&lt;/b&gt; &amp; 申报"

    main = _Channel()
    voices = open_voices(main, agent_ids=("refund-intake", "refund-policy"),
                         titles=TITLES,
                         env=_env(refund_intake=(INTAKE_MXID, SENTINEL_TOKEN)))
    try:
        voices.voice("refund-intake").say(raw)          # 独立账号
        own_sent = fake_nio.instances[-1].sent[-1]
        assert own_sent["body"] == raw, "plain 原样"
        assert own_sent["formatted_body"] == f"<p>{escaped}</p>"

        voices.voice("refund-policy").say(raw)          # 代言
        plain, html = main.sent[-1]
        assert plain.endswith(raw), "plain 原样"
        assert escaped in html and "<b>实付</b>" not in html
    finally:
        voices.close()


# -- 退化 -------------------------------------------------------------------
def test_open_channel_failure_degrades_to_proxy_instead_of_raising(monkeypatch, caplog):
    """`open_channel` 抛任何异常 -> 退成代言 + 一条 WARNING，`open_voices` **不抛**。

    真房间里这条是**常态路径**：token 过期、号还没被邀请进房（构造期查加密拿到
    403，被判成「房间状态查询失败」）。抛出去的话常驻入口起不来，而原因写在
    五层调用之下。
    """
    def _boom(config, **kw):
        raise RuntimeError("房间状态查询失败：M_FORBIDDEN not in room")

    monkeypatch.setattr(room_voices, "open_channel", _boom)
    main = _Channel()
    with caplog.at_level("WARNING"):
        voices = open_voices(main, agent_ids=("refund-risk",), titles=TITLES,
                             env=_env(refund_risk=("@maos-risk:maos.local",
                                                   SENTINEL_TOKEN)))

    voice = voices.voice("refund-risk")
    assert voice.own_identity is False and voice.user_id == BOT_MXID
    assert "M_FORBIDDEN" in caplog.text and "refund-risk" in caplog.text
    voice.say("无异常信号")
    assert main.sent and main.sent[-1][0].startswith("【风险反欺诈岗 · refund-risk】")
    voices.close()


def test_voice_for_unknown_agent_id_does_not_raise():
    """没登记过的 agent_id 也返回一个（代言的）Voice —— 抛一次就把整轮圆桌掀了。"""
    main = _Channel()
    voices = open_voices(main, agent_ids=("refund-intake",), env=_env())

    voice = voices.voice("nobody")
    assert voice.own_identity is False and voice.agent_id == "nobody"
    assert voice.title == "nobody", "titles 没给就退到 agent_id"
    voice.say("我是谁")
    assert main.sent[-1][0] == "【nobody · nobody】 我是谁"
    assert voices.voice("nobody") is voice, "同一个 id 两次要拿到同一个身份对象"
    voices.close()


# -- 铁律 6 -----------------------------------------------------------------
def test_describe_never_contains_token(fake_nio, monkeypatch, caplog):
    """哨兵 token 灌进 env：`describe()` 与日志都反查不到。**安全断言。**

    两条路径各验一次：接通的那一岗（token 进了客户端），以及**异常消息本身带着
    token** 的那一岗 —— 后者是出口脱敏真正要兜的那一幕，判断「上游措辞里会不会
    带 token」是在赌，而赌错当场不报错。
    """
    real_open = room_voices.open_channel

    def _leaky(config, **kw):
        if config.user == POLICY_MXID:
            raise RuntimeError(f"登录失败 access_token={SENTINEL_TOKEN} 已失效")
        return real_open(config, **kw)

    monkeypatch.setattr(room_voices, "open_channel", _leaky)
    main = _Channel()
    with caplog.at_level("INFO"):
        voices = open_voices(
            main, agent_ids=("refund-intake", "refund-policy", "refund-evidence"),
            titles=TITLES,
            env=_env(refund_intake=(INTAKE_MXID, SENTINEL_TOKEN),
                     refund_policy=(POLICY_MXID, SENTINEL_TOKEN)))
    try:
        described = voices.describe()
        assert SENTINEL_TOKEN not in described, "describe() 漏了 token"
        assert SENTINEL_TOKEN not in caplog.text, "日志漏了 token"
        assert REDACTED in described, "带 token 的那条原因没被抹成占位符"
        # 抹掉的是**值**，不是这条信息本身：键名与 mxid 照常报，不然读的人无从下手。
        assert INTAKE_MXID in described and "MAOS_AGENT_REFUND_EVIDENCE_USER" in described
    finally:
        voices.close()


def test_bot_users_lists_only_connected_accounts(fake_nio):
    """5 岗只配 2 岗 -> `bot_users()` 恰好那 2 个 mxid（监听侧要忽略的正是这批）。"""
    main = _Channel()
    voices = open_voices(main, agent_ids=TEAM, titles=TITLES,
                         env=_env(refund_intake=(INTAKE_MXID, SENTINEL_TOKEN),
                                  refund_policy=(POLICY_MXID, SENTINEL_TOKEN)))
    try:
        assert voices.bot_users() == frozenset({INTAKE_MXID, POLICY_MXID})
        assert len([a for a in TEAM if voices.voice(a).own_identity]) == 2
    finally:
        voices.close()


# -- 红线 R3 ----------------------------------------------------------------
def test_independent_channels_never_listen(fake_nio):
    """岗位通道只发不听：假 client 的调用流水里没有 sync / sync_forever / 挂回调。

    一旦它 sync 起来，`maos-bot` 与岗位号就会互相接龙 —— 而每一条单看都合法。
    `alive()` 恒 False 是这条的推论，也是**绝不能**把这种通道交给
    `room_ingress.serve()` 的理由（那个函数见 False 即判 EXIT_NO_ROOM）。
    """
    main = _Channel()
    voices = open_voices(main, agent_ids=("refund-intake",), titles=TITLES,
                         env=_env(refund_intake=(INTAKE_MXID, SENTINEL_TOKEN)))
    try:
        voices.voice("refund-intake").say("一句话")
        client = fake_nio.instances[-1]
        assert client.calls == ["whoami", "state:m.room.encryption", "room_send"], \
            f"send-only 通道多做了动作：{client.calls}"
        assert not any(c in ("sync", "sync_forever", "add_event_callback")
                       for c in client.calls)
        # `_own_channels` 是私有的：这条断言要的正是「构造出来的那个通道对象本身」，
        # 而公开面（Voice）刻意不暴露它 —— 暴露了调用方就能拿去 listen。
        assert all(ch.alive() is False for ch in voices._own_channels)
    finally:
        voices.close()


def test_close_closes_every_independent_channel_but_not_main(fake_nio):
    """`close()` 关掉每条独立通道，**不关主通道** —— 那是调用方开的，归调用方关。"""
    main = _Channel()
    voices = open_voices(main, agent_ids=("refund-intake", "refund-policy"),
                         titles=TITLES,
                         env=_env(refund_intake=(INTAKE_MXID, SENTINEL_TOKEN),
                                  refund_policy=(POLICY_MXID, SENTINEL_TOKEN)))
    clients = list(fake_nio.instances)
    assert len(clients) == 2, "两岗应各开一条通道"

    voices.close()
    assert all(c.closed for c in clients), "有独立通道没关 —— 每条背后是一条守护线程"
    assert main.closed is False, "主通道被顺手关了：常驻入口的回帖会一起哑掉"

    voices.close()                                      # 幂等：重复收口不炸


def test_open_voices_returns_the_contract_shape():
    """形状自证：`VoiceSet` 四个方法、`Voice` 五个成员，一个不多一个不少（契约 §1.3）。

    这条钉的是**跨轨契约**本身：T87 按这个 Protocol 写调用方、T88 按它做惰性
    import 退化。少一个成员那两轨在整合时才炸，多一个则会被下游当成可依赖的面。
    """
    main = _Channel()
    voices = open_voices(main, agent_ids=("refund-intake",), env=_env())
    assert isinstance(voices, RoomVoices)

    assert sorted(m for m in vars(room_voices.VoiceSet) if not m.startswith("_")) == [
        "bot_users", "close", "describe", "voice"]
    assert sorted(room_voices.Voice.__annotations__) == [
        "agent_id", "own_identity", "title", "user_id"]
    assert callable(room_voices.Voice.say)

    voice = voices.voice("refund-intake")
    for member in ("agent_id", "title", "user_id", "own_identity", "say"):
        assert hasattr(voice, member), f"Voice 少了 {member}"
    voices.close()
