"""房间里的「上传材料」按钮与补件页（`hiclaw/matrix_bus.py::html_actions` /
`hiclaw/room_voices.py` / `hiclaw/room_ingress.py` / `hiclaw/upload_server.py`）。

按钮是一条链接：Element 能渲染的富文本只有链接、颜色、加粗，没有 `<button>`，
所以「醒目」靠 `data-mx-bg-color`（Element 会转成行内 style，class 与 style 本身都被
过滤）。点开是本机的补件页，传上来的文件走 router 收附件那条**同一条**路 ——
落盘、按文件名定位订单、复检、五岗重说一轮 —— 与在房间里拖一张图逐字一致。

本文件钉四件事：

1. **渲染**：两种发声形态（独立账号 / 代言）挂的是同一个 chip；label 与 url 都转义。
2. **补件页**：GET 出表单、POST 收文件、底账里没有的单 404、没文件 400、超大 413；
   multipart 解析保住中文文件名、剥掉路径。
3. **接线**：`start_uploads` 把上传变成一条房间里的入站消息，router 按附件的渠道
   取件、按文件名前缀定位订单、对在办的待办发起复检（`round_no=2`）。
4. **配置**：不配 `MAOS_UPLOAD_URL` 就什么都不起、什么都不挂；监听缺省回环。

零 Synapse、零模型；补件页真起在 127.0.0.1 的随机端口上。
"""

from __future__ import annotations

import http.client
import logging
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from hiclaw import room_ingress
from hiclaw.matrix_bus import ACTION_BG, html_actions, plain_actions, with_actions
from hiclaw.room_voices import RoomVoice
from hiclaw.upload_server import (UploadFile, UploadRequest, UploadServer, parse_multipart,
                                  safe_filename, upload_url)
from maos.ingress.contracts import Attachment
from maos.roundtable.team import Action
from maos.tests.test_ingress_attachments import PNG_1PX

ROOT = Path(__file__).resolve().parents[2]
LEDGER = ROOT / "scenarios" / "custom" / "ledger.json"
ROOM = "!room:example.org"
BOSS = "@boss:example.org"
ORDER = "ORD-2026-0001"
ACTION = Action(label="上传照片 · ORD-1 <x> & y",
                url="http://127.0.0.1:8787/upload?order=ORD-1&kinds=image")


# --------------------------------------------------------------------------
# 假件
# --------------------------------------------------------------------------
class _Channel:
    """形状对齐 `_NioChannel`：send / listen / fetch / close。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.on_message = None
        self.on_attachment = None

    def send(self, plain: str, html: str) -> None:
        self.sent.append((plain, html))

    def listen(self, on_message, on_attachment=None) -> None:
        self.on_message, self.on_attachment = on_message, on_attachment

    def fetch(self, att: Attachment) -> bytes:
        raise AssertionError(f"补件页的字节不该从房间取：{att.file_key}")

    def close(self) -> None:
        pass


class _Team:
    """圆桌假件（新签名：认 round_no / added_evidence），只记调用。"""

    def __init__(self) -> None:
        self.preflights: list[dict] = []

    def on_preflight(self, *, payload, checked, ledger, evidence, requested_by,
                     round_no: int = 1, added_evidence: int = 0):
        self.preflights.append({"order": checked.get("order_id"), "evidence": list(evidence),
                                "round_no": round_no, "added_evidence": added_evidence,
                                "requested_by": requested_by})
        return []

    def on_sheet(self, **kw):
        return []

    def on_execute(self, **kw):
        return []

    def roster(self) -> list[dict]:
        return []


def _multipart(fields: dict[str, str], files: list[tuple[str, str, bytes]],
               boundary: str = "----maosBoundary7") -> tuple[str, bytes]:
    """手工拼一份浏览器会发的 multipart/form-data。"""
    out = bytearray()
    for name, value in fields.items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                f"{value}\r\n").encode("utf-8")
    for name, filename, data in files:
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; "
                f"filename=\"{filename}\"\r\nContent-Type: image/png\r\n\r\n").encode("utf-8")
        out += data + b"\r\n"
    out += f"--{boundary}--\r\n".encode("utf-8")
    return f"multipart/form-data; boundary={boundary}", bytes(out)


def _request(server: UploadServer, method: str, path: str, body: bytes = b"",
             ctype: str = "") -> tuple[int, str]:
    host, port = server.address
    conn = http.client.HTTPConnection(host, port, timeout=10)
    headers = {"Content-Type": ctype, "Content-Length": str(len(body))} if body else {}
    conn.request(method, path, body=body or None, headers=headers)
    resp = conn.getresponse()
    text = resp.read().decode("utf-8", "replace")
    conn.close()
    return resp.status, text


@pytest.fixture
def server():
    calls: list[UploadRequest] = []

    def on_upload(req: UploadRequest) -> str:
        calls.append(req)
        return f"已收下 {len(req.files)} 份证据：{req.files[0].filename}"

    srv = UploadServer(on_upload=on_upload, known_orders=lambda: {ORDER},
                       max_bytes=1 << 20, bind="127.0.0.1:0", back_url="http://localhost:8080")
    srv.calls = calls
    srv.start()
    try:
        yield srv
    finally:
        srv.stop()


# --------------------------------------------------------------------------
# 1. 渲染
# --------------------------------------------------------------------------
def test_html_actions_render_a_colored_chip_and_escape_label_and_url() -> None:
    html = html_actions([ACTION])
    assert f'data-mx-bg-color="{ACTION_BG}"' in html, "醒目全靠这个属性：Element 只认它"
    assert 'href="http://127.0.0.1:8787/upload?order=ORD-1&amp;kinds=image"' in html
    assert "上传照片 · ORD-1 &lt;x&gt; &amp; y" in html and "<x>" not in html
    assert "📎" in html

    plain = plain_actions([ACTION])
    assert plain == "📎 上传照片 · ORD-1 <x> & y：http://127.0.0.1:8787/upload?order=ORD-1&kinds=image"

    assert with_actions("p", "<p>p</p>", ()) == ("p", "<p>p</p>")
    assert with_actions("p", "<p>p</p>", [Action(label="", url="x")]) == ("p", "<p>p</p>")
    plain2, html2 = with_actions("说了一句", "<p>说了一句</p>", [ACTION])
    assert plain2 == f"说了一句\n{plain}" and html2 == f"<p>说了一句</p><p>{html}</p>"


def test_both_voice_shapes_hang_the_same_chip_after_the_speech() -> None:
    own = _Channel()
    RoomVoice(agent_id="refund-evidence", title="证据核验岗",
              user_id="@maos-evidence:example.org", own_identity=True,
              channel=own).say_with_actions("第 5 行缺照片", [ACTION])
    proxy = _Channel()
    room_ingress._ProxyVoiceSet(proxy, agent_ids=("refund-evidence",),
                                titles={"refund-evidence": "证据核验岗"},
                                user_id="@maos-bot:example.org"
                                ).voice("refund-evidence").say_with_actions("第 5 行缺照片", [ACTION])

    chip = html_actions([ACTION])
    (own_plain, own_html), (proxy_plain, proxy_html) = own.sent[-1], proxy.sent[-1]
    assert own_html.endswith(f"<p>{chip}</p>") and proxy_html.endswith(f"<p>{chip}</p>")
    assert own_plain == f"第 5 行缺照片\n{plain_actions([ACTION])}"
    assert proxy_plain.startswith("【证据核验岗 · refund-evidence】 第 5 行缺照片\n📎 ")
    assert "<code>refund-evidence</code>" in proxy_html and "<code>" not in own_html


# --------------------------------------------------------------------------
# 2. 补件页
# --------------------------------------------------------------------------
def test_parse_multipart_keeps_utf8_filenames_and_strips_paths() -> None:
    ctype, body = _multipart({"order": ORDER, "who": "张三"},
                             [("files", "破损照片.png", PNG_1PX),
                              ("files", "../../etc/passwd.png", b"\x89PNG\r\n\x1a\nxx")])
    fields, files = parse_multipart(ctype, body)
    assert fields == {"order": ORDER, "who": "张三"}
    assert [f.filename for f in files] == ["破损照片.png", "passwd.png"]
    assert files[0].mime == "image/png" and files[0].data == PNG_1PX

    with pytest.raises(ValueError):
        parse_multipart("application/x-www-form-urlencoded", b"order=1")
    assert safe_filename("") == "upload" and safe_filename("a\x00b.png") == "ab.png"


def test_upload_url_carries_order_kinds_reason_and_line() -> None:
    url = upload_url("http://127.0.0.1:8787/", {"order_id": ORDER, "kinds": ["image"],
                                                  "reason_raw": "质量问题", "line": 5})
    parts = urlsplit(url)
    assert (parts.scheme, parts.netloc, parts.path) == ("http", "127.0.0.1:8787", "/upload")
    assert parse_qs(parts.query) == {"order": [ORDER], "kinds": ["image"],
                                     "reason": ["质量问题"], "line": ["5"]}
    assert upload_url("http://h", {"order_id": ORDER}) == f"http://h/upload?order={ORDER}"


def test_upload_page_serves_a_form_for_a_known_order_only(server: UploadServer) -> None:
    status, page = _request(server, "GET", f"/upload?order={ORDER}&kinds=image&line=5&reason=%E8%B4%A8%E9%87%8F")
    assert status == 200
    assert ORDER in page and "照片" in page and "第 5 行" in page
    assert 'enctype="multipart/form-data"' in page and 'name="files"' in page
    assert f'name="order" value="{ORDER}"' in page

    status, page = _request(server, "GET", "/upload?order=ORD-9999-9999")
    assert status == 404 and "底账里没有订单 ORD-9999-9999" in page
    status, _ = _request(server, "GET", "/upload")
    assert status == 404
    status, _ = _request(server, "GET", "/nothing")
    assert status == 404
    assert _request(server, "GET", "/healthz") == (200, "ok\n")


def test_upload_page_accepts_files_and_shows_the_reply(server: UploadServer) -> None:
    ctype, body = _multipart({"order": ORDER, "kinds": "image", "who": "张三", "line": "5"},
                             [("files", "破损照片.png", PNG_1PX)])
    status, page = _request(server, "POST", "/upload", body, ctype)
    assert status == 200
    assert "已收下 1 份证据：破损照片.png" in page
    assert 'href="http://localhost:8080"' in page, "结果页要能回到房间"
    assert f"order={ORDER}" in page, "结果页要能再传一份"

    req = server.calls[-1]
    assert req.order_id == ORDER and req.kinds == ("image",) and req.uploader == "张三"
    assert req.line == "5" and req.files[0] == UploadFile("破损照片.png", "image/png", PNG_1PX)


def test_upload_page_rejects_missing_files_unknown_orders_and_oversized_bodies(
        server: UploadServer) -> None:
    ctype, body = _multipart({"order": ORDER}, [])
    assert _request(server, "POST", "/upload", body, ctype)[0] == 400

    ctype, body = _multipart({"order": "ORD-9999-9999"}, [("files", "a.png", PNG_1PX)])
    status, page = _request(server, "POST", "/upload", body, ctype)
    assert status == 404 and "底账里没有订单" in page

    huge = b"x" * ((1 << 20) * 6 + (1 << 20) + 1)
    host, port = server.address
    conn = http.client.HTTPConnection(host, port, timeout=10)
    conn.request("POST", "/upload", body=None,
                 headers={"Content-Type": "multipart/form-data; boundary=x",
                          "Content-Length": str(len(huge))})
    assert conn.getresponse().status == 413
    conn.close()
    assert server.calls == [], "被拒的请求一个都不该走到回调"


def test_upload_page_turns_a_callback_failure_into_a_500_page(server: UploadServer) -> None:
    def boom(req: UploadRequest) -> str:
        raise RuntimeError("router 炸了")

    server.on_upload = boom
    ctype, body = _multipart({"order": ORDER}, [("files", "a.png", PNG_1PX)])
    status, page = _request(server, "POST", "/upload", body, ctype)
    assert status == 500 and "RuntimeError: router 炸了" in page


# --------------------------------------------------------------------------
# 3. 接线：补件 -> router -> 复检
# --------------------------------------------------------------------------
def test_start_uploads_feeds_the_router_and_triggers_a_recheck(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MAOS_ATTACHMENT_DIR", str(tmp_path))
    channel = _Channel()
    team = _Team()
    router = room_ingress.wire(channel, room_id=ROOM, ledger_path=LEDGER, team=team)
    channel.on_message(BOSS, f"/refund {ORDER} 质量问题")
    assert [p["round_no"] for p in team.preflights] == [1]

    server = room_ingress.start_uploads(router, room_id=ROOM, bind="127.0.0.1:0")
    assert server is not None
    try:
        assert room_ingress.CHANNEL_WEB_UPLOAD in router.adapters
        req = UploadRequest(order_id=ORDER, kinds=("image",), reason="质量问题", line="2",
                            uploader="张三", files=(UploadFile("rust.png", "image/png", PNG_1PX),))
        reply = server.on_upload(req)
    finally:
        server.stop()

    # 回帖：收下、挂上（按文件名前缀定位）、复检卡 —— 与拖图进房间同一条路的措辞。
    assert "已收下 1 份证据" in reply
    assert f"已挂到 {ORDER}（按文件名 {ORDER}-rust.png）" in reply
    assert "随案证据 0 → 1 份" in reply
    assert channel.sent and reply.splitlines()[0] in channel.sent[-1][0], "同一份回帖也发进了房间"
    # 圆桌收到的是第 2 轮、本轮新增 1 份，且待办的归属仍是起单的人。
    second = team.preflights[-1]
    assert (second["round_no"], second["added_evidence"], second["order"]) == (2, 1, ORDER)
    assert second["requested_by"] == BOSS
    assert len(second["evidence"]) == 1
    # 字节取走即删：暂存里不留东西。
    assert router.adapters[room_ingress.CHANNEL_WEB_UPLOAD]._blobs == {}


def test_upload_without_a_ticket_is_kept_for_a_later_refund(tmp_path, monkeypatch) -> None:
    """没有在办的待办、正文又是空的：走老路暂存，等一条 /refund 认领 —— 不编诉求。"""
    monkeypatch.setenv("MAOS_ATTACHMENT_DIR", str(tmp_path))
    channel = _Channel()
    team = _Team()
    router = room_ingress.wire(channel, room_id=ROOM, ledger_path=LEDGER, team=team)
    server = room_ingress.start_uploads(router, room_id=ROOM, bind="127.0.0.1:0")
    try:
        reply = server.on_upload(UploadRequest(
            order_id=ORDER, kinds=("image",), reason="", line="", uploader="",
            files=(UploadFile("rust.png", "image/png", PNG_1PX),)))
    finally:
        server.stop()
    assert "已收下 1 份证据" in reply and "等一条 /refund 认领" in reply
    assert team.preflights == []


def test_start_uploads_degrades_when_the_port_is_taken(tmp_path, monkeypatch, caplog) -> None:
    monkeypatch.setenv("MAOS_ATTACHMENT_DIR", str(tmp_path))
    channel = _Channel()
    router = room_ingress.wire(channel, room_id=ROOM, ledger_path=LEDGER)
    first = room_ingress.start_uploads(router, room_id=ROOM, bind="127.0.0.1:0")
    assert first is not None
    try:
        host, port = first.address
        with caplog.at_level(logging.WARNING, logger="maos.room_ingress"):
            second = room_ingress.start_uploads(router, room_id=ROOM, bind=f"{host}:{port}")
    finally:
        first.stop()
    assert second is None
    assert any("补件页起不来" in r.getMessage() for r in caplog.records)
    # 取件件还在 —— 第一台补件页仍用它；第二台没起就不该把它摘掉。
    assert room_ingress.CHANNEL_WEB_UPLOAD in router.adapters


# --------------------------------------------------------------------------
# 4. 配置
# --------------------------------------------------------------------------
def test_upload_config_defaults_the_bind_to_loopback_on_the_url_port() -> None:
    assert room_ingress.upload_config({}) == ("", "")
    assert room_ingress.upload_config({"MAOS_UPLOAD_URL": "http://my-mac.local:8787/"}) == (
        "http://my-mac.local:8787", "127.0.0.1:8787")
    assert room_ingress.upload_config({"MAOS_UPLOAD_URL": "https://up.example.org"}) == (
        "https://up.example.org", "127.0.0.1:443")
    assert room_ingress.upload_config({"MAOS_UPLOAD_URL": "http://127.0.0.1:8787",
                                       "MAOS_UPLOAD_BIND": "0.0.0.0:9000"}) == (
        "http://127.0.0.1:8787", "0.0.0.0:9000")


def test_build_team_passes_the_link_only_when_the_engine_takes_it(monkeypatch, caplog) -> None:
    import maos.roundtable.team as team_mod

    link = lambda gap: "http://x"  # noqa: E731
    built = room_ingress._build_team(None, None, upload_link=link)
    assert built is not None and built._upload_link is link

    class Older:
        def __init__(self, model, voices) -> None:
            self.model, self.voices = model, voices

    monkeypatch.setattr(team_mod, "RefundRoundtable", Older)
    with caplog.at_level(logging.WARNING, logger="maos.room_ingress"):
        older = room_ingress._build_team(None, None, upload_link=link)
    assert isinstance(older, Older)
    assert any("不认 upload_link=" in r.getMessage() for r in caplog.records)
