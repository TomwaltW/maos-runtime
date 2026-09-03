"""房间入口的接线：假通道进来，文本 / 附件两个回调都挂上，回帖以 <pre> 进房间。"""

from __future__ import annotations

import os

import pytest

from maos.ingress.chat import ChatResponder
from maos.ingress.contracts import CHANNEL_MATRIX, Attachment, OutboundMessage
from maos.model.client import ModelClient, ModelResponse
from hiclaw import room_ingress

ROOM = "!room:example.org"
BOSS = "@boss:example.org"
CSV = ("订单号,诉求类型,申报金额,申请日期\n"
       "ORD-2026-0001,质量问题,6800,2026-07-10\n").encode("utf-8")


class _Channel:
    """形状对齐 `_NioChannel`：send / listen / fetch / close。"""

    def __init__(self, blobs: dict[str, bytes] | None = None) -> None:
        self.sent: list[tuple[str, str]] = []
        self.blobs = blobs or {}
        self.on_message = None
        self.on_attachment = None

    def send(self, plain: str, html: str) -> None:
        self.sent.append((plain, html))

    def listen(self, on_message, on_attachment=None) -> None:
        self.on_message, self.on_attachment = on_message, on_attachment

    def fetch(self, att: Attachment) -> bytes:
        return self.blobs[att.file_key]

    def close(self) -> None:
        pass


class _Model(ModelClient):
    model = "fake"

    def complete(self, *, system: str, user: str, tier: str) -> ModelResponse:
        return ModelResponse(text="我是退款助手，把申请表拖进来就行")


def test_adapter_sends_plain_and_escaped_pre_html():
    ch = _Channel()
    room_ingress.MatrixRoomAdapter(ch).send(OutboundMessage(chat_id=ROOM, text="a <b> & c"))
    assert ch.sent == [("a <b> & c", "<pre>a &lt;b&gt; &amp; c</pre>")]


def test_wire_hooks_both_callbacks_and_answers_text_and_sheet(capsys):
    ch = _Channel({"mxc://example.org/csv": CSV})
    router = room_ingress.wire(ch, room_id=ROOM, chat=ChatResponder(_Model()))
    assert ch.on_message is not None and ch.on_attachment is not None
    assert router.chat is not None

    ch.on_message(BOSS, "maos")
    assert ch.sent[-1][0] == "我是退款助手，把申请表拖进来就行"

    ch.on_attachment(BOSS, Attachment(channel=CHANNEL_MATRIX, kind="file",
                                      file_key="mxc://example.org/csv",
                                      filename="requests.csv", mime="text/csv"))
    plain, html = ch.sent[-1]
    assert "申请表 requests.csv：共 1 行，可预检 1 行" in plain
    assert html.startswith("<pre>") and "/approve RC-ORD-2026-0001" in plain
    assert "RC-ORD-2026-0001" in router._tickets

    out = capsys.readouterr().out
    assert f"[{BOSS} 说] maos" in out and "[回帖]" in out


def test_wire_distinct_message_ids_so_dedup_does_not_swallow_the_second_message():
    ch = _Channel()
    room_ingress.wire(ch, room_id=ROOM)
    ch.on_message(BOSS, "/help")
    ch.on_message(BOSS, "/help")
    assert len(ch.sent) == 2


def test_long_replies_are_split_into_numbered_chunks():
    """一条 Matrix 事件 64 KB 上限；超了 Synapse 回 413，房间里就是一片安静。"""
    ch = _Channel()
    text = "\n".join(f"第 {i} 行 " + "x" * 100 for i in range(400))      # ≈ 44 K 字符
    room_ingress.MatrixRoomAdapter(ch).send(OutboundMessage(chat_id=ROOM, text=text))
    assert len(ch.sent) == 3
    assert ch.sent[0][0].startswith("（1/3）\n第 0 行")
    assert all(len(plain) <= room_ingress.CHUNK_CHARS + 12 for plain, _ in ch.sent)
    joined = "\n".join(plain.split("\n", 1)[1] for plain, _ in ch.sent)
    assert joined == text                                              # 一行不丢、不切半行


def test_single_overlong_line_is_hard_split():
    parts = room_ingress.split_message("a" * 25 + "\nb", limit=10)
    assert parts == ["a" * 10, "a" * 10, "aaaaa\nb"]       # 尾巴装得下就跟下一行合在一段


def test_serve_exits_nonzero_when_the_listener_dies(capsys):
    class Dying(_Channel):
        def __init__(self):
            super().__init__()
            self.ticks = 0

        def alive(self):
            self.ticks += 1
            return self.ticks < 3

        def failure(self):
            return "RuntimeError: sync 炸了"

    assert room_ingress.serve(Dying(), poll=0.01) == room_ingress.EXIT_NO_ROOM
    err = capsys.readouterr().err
    assert "监听已停" in err and "sync 炸了" in err and "重新起进程" in err


def test_main_refuses_to_start_without_room_env(monkeypatch, capsys):
    for name in ("MATRIX_HOMESERVER", "MATRIX_USER", "MATRIX_TOKEN", "MATRIX_ROOM_ID"):
        monkeypatch.delenv(name, raising=False)
    assert room_ingress.main([]) == room_ingress.EXIT_NO_ENV
    assert "没配齐" in capsys.readouterr().err


# --------------------------------------------------------------------------
# 圆桌装配（T88）—— 两个可选件都不在，房间照常起
# --------------------------------------------------------------------------
def test_main_still_wires_when_roundtable_and_voices_are_missing(monkeypatch, capsys):
    """圆桌引擎与发声面都没装载：退回单机器人，命令面照样接上，退出码仍是 0。

    房间入口是这条链路唯一的常驻进程 —— 让它因为一个旁路组件没装就起不来，
    等于用「圆桌不在」换来「命令面也没了」。而退化必须说出来：静默退化与
    「机器人挂了」无法分辨，那正是本模块要消灭的东西。
    """
    import sys

    from maos.model.client import ScriptedModelClient

    monkeypatch.setitem(sys.modules, "maos.roundtable.team", None)
    monkeypatch.setitem(sys.modules, "hiclaw.room_voices", None)

    ch = _Channel()
    wired: dict = {}
    original_wire = room_ingress.wire

    def _wire(channel, **kw):
        wired.update(kw)
        return original_wire(channel, **kw)

    monkeypatch.setattr(room_ingress, "open_channel", lambda cfg: ch)
    monkeypatch.setattr(room_ingress, "serve", lambda channel, **kw: 0)
    monkeypatch.setattr(room_ingress, "wire", _wire)
    monkeypatch.setattr(room_ingress, "ChatResponder",
                        lambda: ChatResponder(ScriptedModelClient({})))
    monkeypatch.setenv("MATRIX_HOMESERVER", "https://matrix.example.org")
    monkeypatch.setenv("MATRIX_USER", "@maos-bot:example.org")
    monkeypatch.setenv("MATRIX_TOKEN", "not-a-real-token")
    monkeypatch.setenv("MATRIX_ROOM_ID", ROOM)

    assert room_ingress.main([]) == room_ingress.EXIT_OK
    assert "未装载" in capsys.readouterr().out
    assert wired["team"] is None
    assert ch.on_message is not None and ch.on_attachment is not None

    ch.on_message(BOSS, "/help")
    assert "/refund" in ch.sent[-1][0]                 # 命令面一个字不受影响


def test_proxy_voice_escapes_html_and_keeps_the_name_tag():
    """代言形态与 `ap_room.render_speech` 同构，**但两份都转义** —— 那边没转义，
    模型吐一个 `<` 就能把 formatted_body 破掉，而 Synapse 不报错。
    """
    ch = _Channel()
    voices = room_ingress._ProxyVoiceSet(
        ch, agent_ids=("refund-intake", "refund-policy"),
        titles={"refund-intake": "申请受理岗"}, user_id="@maos-bot:example.org")

    voices.voice("refund-intake").say("a <b> & c")
    plain, html = ch.sent[-1]
    assert plain.startswith("【")
    assert plain == "【申请受理岗 · refund-intake】 a <b> & c"
    assert "&lt;b&gt; &amp;" in html and "<code>refund-intake</code>" in html

    # 岗位名没给就退回工号，照样发得出去（契约 §1.3：任何 agent_id 都不抛）
    seat = voices.voice("refund-policy")
    assert seat.title == "refund-policy" and seat.own_identity is False
    assert voices.voice("谁都不认识的岗").agent_id == "谁都不认识的岗"

    assert voices.bot_users() == frozenset()           # 一个独立账号都没接通
    assert "代言" in voices.describe() and "token" not in voices.describe().lower()
    voices.close()


# --------------------------------------------------------------------------
# 主席收口面与发言节奏（T91，跨轨契约 §3 / §5 / §8 第 4 条）
# --------------------------------------------------------------------------
# `decide()`（`maos/roundtable/verdict.py`）是 T90 的产物，本轨基线里**还不存在** ——
# 所以这里用一个同形假件（契约 §2 的九个字段）自测。生产代码对它的依赖止于
# 「有这几个名字」，真件并进来之后这些断言仍然成立。
#
# 三件事在这里定住：
#
# 1. **预告必须在五岗之前。** 五岗各一次真模型调用要十几秒，期间房间对任何消息
#    都没反应 —— 那一句是这十几秒里唯一能证明「机器人没挂」的东西。
# 2. **收口卡是旁路（红线 R4）。** 它发不出去，`/refund` 的回帖与 `/approve` 的处置
#    一个字都不许受影响；`decide` 不在时五岗照常发言，只是最后没有那张卡。
# 3. **节奏缺省 0。** `maos/tests` 与 `scripts/room_team_smoke.py` 一秒都不许变慢，
#    env 里写了个 `abc` 也只能按 0 处理 —— 抛出去会让房间起不来。


class _FakeVerdict:
    """`Verdict` 的同形假件。字段够渲染用就行，`render_verdict_card` 只按名字读。"""

    def __init__(self, **kw) -> None:
        self.case_id = kw.get("case_id", "")
        self.recommend = kw.get("recommend", "approve")
        self.headline = kw.get("headline", "建议批复 · 请 supervisor 拍板")
        self.reasons = kw.get("reasons", ["规则审核岗：按基线裁定批准"])
        self.blockers = kw.get("blockers", [])
        self.approver_role = kw.get("approver_role", "supervisor")
        self.amount_preview = kw.get("amount_preview", "9600.00")
        self.next_command = kw.get("next_command", "/approve RC-1")
        self.seats = kw.get("seats", {})


class _Team:
    """圆桌假件：三个钩子 + `roster`。发言直接进同一条通道，好断言先后顺序。"""

    def __init__(self, channel, *, reports=None) -> None:   # noqa: ANN001
        self._channel = channel
        self._reports = [1, 2, 3, 4, 5] if reports is None else reports
        self.calls: list[tuple] = []

    def on_preflight(self, **kw):
        self.calls.append(("preflight", kw))
        for title in ("申请受理岗", "规则审核岗"):
            self._channel.send(f"【{title}】 说了一句", "<p>说了一句</p>")
        return self._reports

    def on_sheet(self, **kw):
        self.calls.append(("sheet", kw))
        self._channel.send("【申请受理岗】 这一批 3 行", "<p>3 行</p>")
        return self._reports

    def on_execute(self, **kw):
        self.calls.append(("execute", kw))
        return self._reports

    def roster(self):
        return [{"agent_id": "refund-intake", "title": "申请受理岗"}]


def _chair(channel, *, team=None, decide=None, seats="（申请受理岗 → 规则审核岗）"):
    team = _Team(channel) if team is None else team
    return room_ingress._ChairTeam(team, channel, decide=decide, seats=seats), team


def test_chair_announces_the_seats_before_any_of_them_speaks():
    """预告在五岗**之前**，且它自己发不出去也不影响五岗（契约 §8 第 4 条）。"""
    ch = _Channel()
    chair, team = _chair(ch, decide=lambda reports, case_id="": _FakeVerdict())

    chair.on_preflight(payload={}, checked={"case_id": "RC-1"}, ledger={},
                       evidence=[], requested_by=BOSS)

    assert ch.sent[0][0].startswith("五岗正在过这一单")
    assert "申请受理岗 → 规则审核岗" in ch.sent[0][0]
    assert "<em>" in ch.sent[0][1]                       # 预告是旁白，排版上与发言分开
    assert ch.sent[1][0].startswith("【申请受理岗】")     # 五岗在预告之后
    assert team.calls[0][0] == "preflight"


def test_chair_sends_the_verdict_card_last_and_mentions_the_requester():
    """收口卡在五岗**之后**、由**主通道**发（契约 §5.2），@ 的是起单人。"""
    ch = _Channel()
    seen: dict = {}

    def _decide(reports, case_id=""):
        seen["reports"], seen["case_id"] = reports, case_id
        return _FakeVerdict(case_id=case_id, next_command=f"/approve {case_id}")

    chair, _ = _chair(ch, decide=_decide)
    reports = chair.on_preflight(payload={}, checked={"case_id": "RC-ORD-2026-0004"},
                                 ledger={}, evidence=[], requested_by=BOSS)

    assert reports == [1, 2, 3, 4, 5]                    # 钩子返回值原样透传给 router
    assert seen["case_id"] == "RC-ORD-2026-0004" and seen["reports"] == reports
    plain, markup = ch.sent[-1]                          # 最后一条就是那张卡
    assert plain.startswith("建议批复 · 请 supervisor 拍板")
    assert "  下一步：/approve RC-ORD-2026-0004" in plain
    assert markup.startswith("<blockquote>")
    assert f'href="https://matrix.to/#/{BOSS}">{BOSS}</a>' in markup
    assert "【" not in plain                             # 不归五岗任何一岗，没有名牌


def test_chair_keeps_the_seats_speaking_when_the_verdict_engine_is_missing(caplog):
    """`decide is None`：五岗照常发言，只是最后没有那张卡 —— 与「圆桌未装载」同一档。

    这是本模块「三个件都可以不在」的**第四个件**。整合轮把合议引擎并进来之后这条
    退路自动失效，但不许删：删了之后哪天 `verdict.py` 没跟着并，房间就起不来。
    """
    ch = _Channel()
    chair, team = _chair(ch, decide=None)

    reports = chair.on_preflight(payload={}, checked={"case_id": "RC-1"}, ledger={},
                                 evidence=[], requested_by=BOSS)

    assert reports == [1, 2, 3, 4, 5] and team.calls[0][0] == "preflight"
    assert ch.sent[0][0].startswith("五岗正在过这一单")   # 预告照发
    assert ch.sent[-1][0] == "【规则审核岗】 说了一句"    # 最后一条仍是五岗，没有卡
    assert not any("<blockquote>" in markup for _, markup in ch.sent)
    assert "未装载" in room_ingress._ChairTeam(team, ch).describe()


def test_chair_card_failure_only_warns_and_never_reaches_the_router(caplog):
    """卡片发不出去只记 WARNING（红线 R4）：`/refund` 的回帖一个字都不许受影响。"""
    ch = _Channel()

    class _Broken(_Channel):
        def send(self, plain, html):
            if plain.startswith("建议批复"):
                raise RuntimeError("Synapse 回了 413")
            super().send(plain, html)

    broken = _Broken()
    chair, _ = _chair(broken, decide=lambda reports, case_id="": _FakeVerdict())
    with caplog.at_level("WARNING"):
        reports = chair.on_preflight(payload={}, checked={}, ledger={}, evidence=[],
                                     requested_by=BOSS)
    assert reports == [1, 2, 3, 4, 5]                    # 钩子返回值照旧
    assert "收口卡没进房间" in caplog.text and "413" in caplog.text

    # `decide()` 自己炸也一样，只记 WARNING
    caplog.clear()

    def _boom(reports, case_id=""):
        raise ValueError("真值表读到一个没有的键")

    chair2, _ = _chair(ch, decide=_boom)
    with caplog.at_level("WARNING"):
        assert chair2.on_preflight(payload={}, checked={}, ledger={}, evidence=[],
                                   requested_by=BOSS) == [1, 2, 3, 4, 5]
    assert "收口卡没进房间" in caplog.text


def test_chair_heads_up_failure_does_not_stop_the_seats(caplog):
    """预告是观感，不是主路：它发不出去，五岗照常说完、卡照常发。"""
    class _FirstSendDies(_Channel):
        def send(self, plain, html):
            if plain.startswith("五岗正在过"):
                raise RuntimeError("发不出去")
            super().send(plain, html)

    ch = _FirstSendDies()
    chair, _ = _chair(ch, decide=lambda reports, case_id="": _FakeVerdict())
    with caplog.at_level("WARNING"):
        chair.on_preflight(payload={}, checked={}, ledger={}, evidence=[],
                           requested_by=BOSS)
    assert "发言预告没进房间" in caplog.text
    assert ch.sent[0][0].startswith("【申请受理岗】")
    assert ch.sent[-1][1].startswith("<blockquote>")


def test_chair_sheet_round_gets_a_heads_up_but_no_verdict_card():
    """读表那一轮只发预告，**不发收口卡**。

    收口卡的真值表读的是单案五岗的 `data`（契约 §2.1），而读表每岗汇总的是
    「这批多少行、多少能过」，键完全不同 —— 拿它去合议，出来的是一张看着像
    结论、实则没有依据的卡。
    """
    ch = _Channel()
    chair, team = _chair(ch, decide=lambda reports, case_id="": _FakeVerdict())

    chair.on_sheet(rows=[{}, {}, {}], ledger={}, requested_by=BOSS)

    assert ch.sent[0][0].startswith("五岗正在过这一批（3 行）")
    assert not any("<blockquote>" in markup for _, markup in ch.sent)
    assert team.calls[0][0] == "sheet"


def test_chair_passes_every_other_call_straight_through():
    """`on_execute` / `roster` / 以后长出来的方法都原样转交（`__getattr__`）。

    抄一份方法清单的症状是「圆桌的新方法在房间里静默失踪」—— 比如 @岗位点名
    问答并进来之后，房间里点名还是 `maos-bot` 用退款助手的口气答。
    """
    ch = _Channel()
    chair, team = _chair(ch, decide=lambda reports, case_id="": _FakeVerdict())

    assert chair.on_execute(payload={}, result={"case_id": "RC-1"}, operator=BOSS) \
        == [1, 2, 3, 4, 5]
    assert team.calls[-1][0] == "execute"
    assert chair.roster() == [{"agent_id": "refund-intake", "title": "申请受理岗"}]
    assert not ch.sent                                   # 放行回执那一轮不发卡、不预告

    # 内层没有的属性照样报 AttributeError，不吞
    with pytest.raises(AttributeError):
        chair.answer_something_that_does_not_exist


# -- 发言节奏（契约 §3）----------------------------------------------------
def test_pace_defaults_to_zero_and_never_sleeps():
    """🔴 缺省必须是 0：`maos/tests` 与冒烟脚本一秒都不许变慢。

    `make_pace` 返回 `None` 而不是一个「睡 0 秒」的函数 —— 契约 §3 说的是
    「一次都不调」，多一次函数调用就多一个能出错的地方。
    """
    assert room_ingress.pace_ms({}) == 0
    assert room_ingress.pace_ms({room_ingress.ENV_TEAM_PACE_MS: "   "}) == 0
    assert room_ingress.make_pace(0) is None
    assert room_ingress.make_pace(room_ingress.pace_ms({})) is None


def test_pace_reads_the_env_and_sleeps_once_per_seat():
    """设了值：每一岗说完停一次。用假 sleep 记账，**不真等**。"""
    env = {room_ingress.ENV_TEAM_PACE_MS: "1500"}
    assert room_ingress.pace_ms(env) == 1500

    naps: list[float] = []
    pace = room_ingress.make_pace(1500, sleep=naps.append)
    assert pace is not None
    for i in range(1, 6):
        pace(i, 5)
    assert naps == [1.5] * 5


@pytest.mark.parametrize("raw,note", [("abc", "不是整数"), ("1.5", "不是整数"),
                                      ("-200", "是负数")])
def test_pace_falls_back_to_zero_for_a_bad_value_and_never_raises(raw, note, caplog):
    """env 里写坏了按 0 处理并记一行 WARNING —— **抛出去会让房间起不来**。

    这个值只影响观感。为了一个观感参数让常驻入口起不来（命令面、申请表一起没了），
    是拿主路赔旁路，与红线 R4 同一条道理。
    """
    with caplog.at_level("WARNING"):
        got = room_ingress.pace_ms({room_ingress.ENV_TEAM_PACE_MS: raw})
    assert got == 0 and room_ingress.make_pace(got) is None
    assert note in caplog.text and room_ingress.ENV_TEAM_PACE_MS in caplog.text


def test_build_team_skips_pace_when_the_roundtable_does_not_take_it(caplog):
    """圆桌不认 `pace=` 就不传，并说清为什么不等 —— 直接传是 `TypeError`，
    而那一刻的症状是「房间起不来」：拿一个观感参数换掉整条命令面。
    """
    class _NoPace:
        def __init__(self, model, voices):
            self.model, self.voices = model, voices

    import maos.roundtable.team as team_mod

    saved = team_mod.RefundRoundtable
    try:
        team_mod.RefundRoundtable = _NoPace
        with caplog.at_level("WARNING"):
            built = room_ingress._build_team(None, "voices",
                                             pace=lambda i, total: None)
    finally:
        team_mod.RefundRoundtable = saved

    assert isinstance(built, _NoPace) and built.voices == "voices"
    assert room_ingress.ENV_TEAM_PACE_MS in caplog.text and "本次不生效" in caplog.text


def test_load_decide_degrades_to_none_when_the_engine_is_missing(monkeypatch, caplog):
    """合议引擎没并进来：返回 `None` 并打一行，房间照常起（第四个可选件）。"""
    import sys

    monkeypatch.setitem(sys.modules, "maos.roundtable.verdict", None)
    with caplog.at_level("WARNING"):
        assert room_ingress._load_decide() is None
    assert "合议引擎未装载" in caplog.text and "没有收口卡" in caplog.text


def test_main_wraps_the_loaded_roundtable_in_a_chair(monkeypatch, capsys):
    """圆桌**装载成功**那条路：交给 router 的是包好的主席，横幅报清收口卡与节奏。

    这条补的是 T89 那个教训的同一种坑（BACKLOG 已记）：装载成功的分支只有单元级
    假件在测、启动路径一次都没实跑过，于是 `main` 里新加的一行是死代码还是活代码，
    要等到真房间才知道 —— 而那时的症状是常驻进程起不来。

    🔴 `MAOS_AGENT_*` 必须自己剥：`conftest.py` 的 `MATRIX_ENV_VARS` 只清 `MATRIX_*`
    （那是共享文件，不在本轨白名单）。在 source 过 `~/.maos-matrix/agents.env` 的
    shell 里，`open_voices` 会拿真 token 去开五条**真**通道 —— 这条测试就要 Synapse 了。
    """
    from maos.model.client import ScriptedModelClient

    for name in list(os.environ):
        if name.startswith("MAOS_AGENT_") or name == "MAOS_ROOM_BOTS":
            monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv(room_ingress.ENV_TEAM_PACE_MS, raising=False)

    ch = _Channel()
    wired: dict = {}
    original_wire = room_ingress.wire

    def _wire(channel, **kw):
        wired.update(kw)
        return original_wire(channel, **kw)

    monkeypatch.setattr(room_ingress, "open_channel", lambda cfg: ch)
    monkeypatch.setattr(room_ingress, "serve", lambda channel, **kw: 0)
    monkeypatch.setattr(room_ingress, "wire", _wire)
    monkeypatch.setattr(room_ingress, "ChatResponder",
                        lambda: ChatResponder(ScriptedModelClient({})))
    monkeypatch.setenv("MATRIX_HOMESERVER", "https://matrix.example.org")
    monkeypatch.setenv("MATRIX_USER", "@maos-bot:example.org")
    monkeypatch.setenv("MATRIX_TOKEN", "not-a-real-token")
    monkeypatch.setenv("MATRIX_ROOM_ID", ROOM)

    assert room_ingress.main([]) == room_ingress.EXIT_OK

    out = capsys.readouterr().out
    assert f"发言节奏：{room_ingress.ENV_TEAM_PACE_MS}=0（不等，缺省）" in out
    assert isinstance(wired["team"], room_ingress._ChairTeam)
    # 名册取的是**内层**圆桌的，五岗一个不少（包装不许挡住 roster）
    assert len(wired["team"].roster()) == 5

    # 横幅报的收口卡状态必须与真实装载情况一致 —— 合议引擎并进来的前后**都**成立。
    # 钉死其中一种，整合轮那天这条会以「测试红了」的形态报一件其实正常的事。
    chair = [ln for ln in out.split("\n") if ln.startswith("圆桌收口：")]
    assert len(chair) == 1
    assert ("已装载" in chair[0]) is (room_ingress._load_decide() is not None)
