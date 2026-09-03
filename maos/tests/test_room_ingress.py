"""房间入口的接线：假通道进来，文本 / 附件两个回调都挂上，回帖以 <pre> 进房间。"""

from __future__ import annotations

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
