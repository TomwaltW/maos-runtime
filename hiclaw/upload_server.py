"""补件页 —— 房间里岗位发言后面那个「上传材料」按钮点开的地方。

## 为什么要有一个网页

Matrix 的消息里放不进一个真正的按钮：Element 能渲染的富文本只有链接、颜色、加粗
这几样（`hiclaw/matrix_bus.py::html_actions`），而「点一下就能选文件传上去」这件事
Element 只在它自己的回形针上有。所以按钮是一条**指向本页**的链接：点开是一个只认
这一单的上传表单，传完直接走 `IngressRouter` 收附件那条链路 —— 与在房间里拖一张图
**同一条路**（落盘、按文件名定位订单、复检、五岗重说一轮），不另写一套。另写一套的
症状是「拖图复检说批 6800、网页补件复检说批 5390」，而两边各自都不报错。

## 边界

  · 只用标准库 `http.server`，与 `maos/ingress/server.py` 同一取向；缺省只听 127.0.0.1。
    演示机上 Element 与本进程在同一台 Mac，回环地址够用；要给局域网里的手机用，
    `MAOS_UPLOAD_BIND=0.0.0.0:8787` 自己打开 —— 并且知道自己在做什么：本页没有登录。
  · 体积闸与类型闸**不在这里做**：这里只把字节交给回调，router 的 `AttachmentStore`
    按同一套上限与白名单拒收，拒收原因原样显示在结果页上。这里只有一道 Content-Length
    的粗闸，防的是一个声明 2 GB 的请求把进程内存吃光（同 `ingress/server.py::MAX_BODY`）。
  · 订单号先对底账：底账里没有的单直接 404，不让人对着一个不存在的单传文件。
  · 不落任何状态：每次请求都是一次独立的「收字节 -> 交回调 -> 显示回执」。
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from html import escape as _esc
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import PurePosixPath
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlsplit

log = logging.getLogger("maos.upload_server")

#: 缺省监听地址。8787 没被本机别的演示件占用（Synapse 8008、Element 8080/8081）。
DEFAULT_BIND = "127.0.0.1:8787"
#: 单个文件的缺省上限，与 `maos/ingress/attachments.py` 的缺省一致；真正生效的是
#: 调用方传进来的 `max_bytes`（取自 router 的 `AttachmentStore`），这里只是兜底。
DEFAULT_MAX_BYTES = 8 << 20
#: 一次最多传几份。一单缺的通常是一两张图；这个数只是 Content-Length 粗闸的乘数。
MAX_FILES = 6
#: 表单本身（订单号、诉求、几段边界）的开销上限。
FORM_OVERHEAD = 1 << 20

UPLOAD_PATH = "/upload"
HEALTH_PATH = "/healthz"

#: 证据类型的中文显示，只给页面文案用。与 `maos/roundtable/stages.py::KIND_CN`
#: 同一份取值；这里不 import 它 —— 本页是 hiclaw 的件，不该反向依赖圆桌。
KIND_CN = {"image": "照片", "video": "视频", "audio": "录音",
           "document": "文件", "attachment": "附件"}


@dataclass(frozen=True)
class UploadFile:
    """表单里的一份文件：文件名（已去路径、去控制字符）、浏览器自报的 MIME、字节。"""

    filename: str
    mime: str
    data: bytes


@dataclass(frozen=True)
class UploadRequest:
    """一次补件：给哪一单、传了什么。``kinds`` 与 ``reason`` 只是按钮带过来的提示，
    router 不读它们 —— 定位订单靠文件名前缀，诉求类型靠这一单在办的待办。"""

    order_id: str
    kinds: tuple[str, ...]
    reason: str
    line: str
    uploader: str
    files: tuple[UploadFile, ...]


def upload_url(base: str, gap: dict) -> str:
    """按钮地址：``<base>/upload?order=…&kinds=…&reason=…&line=…``。

    ``gap`` 是 `maos.roundtable.stages._material_gaps` 的一条。只带能让页面把话
    说清楚的四样；不带 case_id —— 那是本进程的内部编号，人看不懂也不该改。
    """
    query = {"order": str(gap.get("order_id") or "")}
    kinds = [str(k) for k in (gap.get("kinds") or []) if str(k)]
    if kinds:
        query["kinds"] = ",".join(kinds)
    if gap.get("reason_raw"):
        query["reason"] = str(gap["reason_raw"])
    if gap.get("line") is not None:
        query["line"] = str(gap["line"])
    return f"{base.rstrip('/')}{UPLOAD_PATH}?{urlencode(query)}"


def make_upload_link(base: str) -> Callable[[dict], str]:
    """圆桌 `RefundRoundtable(upload_link=…)` 要的那个生成器。"""
    return lambda gap: upload_url(base, gap)


# --------------------------------------------------------------------------
# multipart/form-data
# --------------------------------------------------------------------------
_BOUNDARY_RE = re.compile(r'boundary="?([^";]+)"?', re.IGNORECASE)
_PARAM_RE = re.compile(r'(?:^|;)\s*{key}\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^;]*))')
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _param(header: str, key: str) -> str | None:
    m = re.search(_PARAM_RE.pattern.format(key=re.escape(key)), header, re.IGNORECASE)
    if m is None:
        return None
    return (m.group(1) if m.group(1) is not None else m.group(2) or "").strip()


def safe_filename(raw: str) -> str:
    """去掉路径、控制字符，限长；空的给一个占位名。文件名会进房间的回帖，也会成为
    落盘时的显示名 —— 一个带 ``../`` 的名字不该有机会走到任何一层。"""
    name = PurePosixPath(str(raw or "").replace("\\", "/")).name
    name = _CONTROL_RE.sub("", name).strip().strip(".")
    return name[:120] or "upload"


def parse_multipart(content_type: str, body: bytes) -> tuple[dict[str, str], list[UploadFile]]:
    """解一份 ``multipart/form-data``：返回 ``(文本字段, 文件)``。

    自己写而不用 `email` 包：那个包按邮件头的口径处理文件名（非 ASCII 走
    RFC 2047 / surrogateescape），浏览器发的是裸 UTF-8，两边对不上时中文文件名会
    变成一串问号 —— 而文件名正是定位订单的依据之一。四十行的解析器换一个可控的口径。
    """
    m = _BOUNDARY_RE.search(content_type or "")
    if m is None:
        raise ValueError("不是 multipart/form-data（缺 boundary）")
    boundary = b"--" + m.group(1).encode("latin-1", "ignore")
    fields: dict[str, str] = {}
    files: list[UploadFile] = []
    for chunk in body.split(boundary)[1:]:
        if chunk.startswith(b"--"):
            break                                   # 收尾边界
        if chunk.startswith(b"\r\n"):
            chunk = chunk[2:]
        head, sep, data = chunk.partition(b"\r\n\r\n")
        if not sep:
            continue
        if data.endswith(b"\r\n"):
            data = data[:-2]
        headers: dict[str, str] = {}
        for line in head.decode("utf-8", "replace").split("\r\n"):
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
        disposition = headers.get("content-disposition", "")
        name = _param(disposition, "name")
        filename = _param(disposition, "filename")
        if filename is not None:
            if data:
                files.append(UploadFile(
                    filename=safe_filename(filename),
                    mime=(headers.get("content-type") or "application/octet-stream").split(";")[0].strip(),
                    data=data))
        elif name:
            fields[name] = data.decode("utf-8", "replace").strip()
    return fields, files


# --------------------------------------------------------------------------
# 页面
# --------------------------------------------------------------------------
_CSS = """
:root{color-scheme:dark light}
body{margin:0;background:#15191e;color:#e6e9ee;font:15px/1.6 -apple-system,"PingFang SC","Helvetica Neue",sans-serif}
main{max-width:640px;margin:48px auto;padding:0 20px}
h1{font-size:22px;margin:0 0 6px}
.sub{color:#9aa4b2;margin:0 0 24px}
.card{background:#1f252d;border:1px solid #2c3540;border-radius:12px;padding:20px 22px;margin-bottom:16px}
.need{font-size:17px;font-weight:600;color:#ffb27a}
.gap{color:#c9d1db;margin:4px 0 0}
label{display:block;margin:14px 0 6px;color:#9aa4b2;font-size:13px}
input[type=text]{width:100%;box-sizing:border-box;background:#141920;border:1px solid #2c3540;border-radius:8px;color:#e6e9ee;padding:9px 11px;font-size:15px}
.drop{border:2px dashed #3a4654;border-radius:12px;padding:26px;text-align:center;color:#9aa4b2;background:#171c23}
.drop input{display:block;margin:12px auto 0;color:#e6e9ee}
button{margin-top:18px;width:100%;border:0;border-radius:10px;padding:14px;font-size:17px;font-weight:700;color:#fff;background:#d9480f;cursor:pointer}
button:hover{background:#e8590c}
pre{white-space:pre-wrap;word-break:break-all;background:#141920;border-radius:8px;padding:14px;font-size:13.5px;line-height:1.55}
a{color:#7cc4ff}
.tip{color:#9aa4b2;font-size:13px;margin-top:10px}
.bad{color:#ff8787}
"""


def _page(title: str, body: str) -> bytes:
    html = (f"<!doctype html><html lang=\"zh\"><head><meta charset=\"utf-8\">"
            f"<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>{_esc(title)}</title><style>{_CSS}</style></head>"
            f"<body><main>{body}</main></body></html>")
    return html.encode("utf-8")


def _kinds_cn(kinds: tuple[str, ...]) -> str:
    return "、".join(KIND_CN.get(k, k) for k in kinds)


def render_form(order_id: str, *, kinds: tuple[str, ...], reason: str, line: str,
                max_bytes: int, accept_hint: str) -> bytes:
    what = _kinds_cn(kinds) or "材料"
    where = f"第 {_esc(line)} 行 · " if line else ""
    why = f"（{_esc(reason)}）" if reason else ""
    hidden = "".join(
        f'<input type="hidden" name="{k}" value="{_esc(v)}">'
        for k, v in (("order", order_id), ("kinds", ",".join(kinds)),
                     ("reason", reason), ("line", line)))
    body = (
        f"<h1>补交材料</h1>"
        f"<p class=\"sub\">{where}<strong>{_esc(order_id)}</strong>{why}</p>"
        f"<div class=\"card\"><div class=\"need\">这一单缺：{_esc(what)}</div>"
        f"<p class=\"gap\">传上来会自动挂到这一单并复检，退款审批群里五岗会重说一轮；"
        f"放行仍要 /approve。</p></div>"
        f"<form class=\"card\" method=\"post\" action=\"{UPLOAD_PATH}\" enctype=\"multipart/form-data\">"
        f"{hidden}"
        f"<label>谁在补（可不填）</label><input type=\"text\" name=\"who\" placeholder=\"例：张三 / 客服 A\">"
        f"<label>文件（可多选）</label>"
        f"<div class=\"drop\">把{_esc(what)}拖到这里，或点下面选择"
        f"<input type=\"file\" name=\"files\" multiple required accept=\"{_esc(accept_hint)}\"></div>"
        f"<p class=\"tip\">收 jpg / png / gif / webp / heic / pdf，单个不超过 {max_bytes >> 20} MB，"
        f"一次最多 {MAX_FILES} 份。</p>"
        f"<button type=\"submit\">📎 上传并复检</button></form>"
    )
    return _page(f"补交材料 · {order_id}", body)


def render_result(order_id: str, reply: str, *, back_url: str, again_url: str) -> bytes:
    links = [f'<a href="{_esc(again_url)}">再传一份</a>']
    if back_url:
        links.insert(0, f'<a href="{_esc(back_url)}">回到房间看复检</a>')
    body = (
        f"<h1>已收到</h1>"
        f"<p class=\"sub\"><strong>{_esc(order_id)}</strong> 的材料已交给退款助手，"
        f"下面是它的回执（同一份也发在群里）：</p>"
        f"<div class=\"card\"><pre>{_esc(reply or '（助手没有回执）')}</pre></div>"
        f"<p>{' &nbsp;·&nbsp; '.join(links)}</p>"
    )
    return _page(f"已收到 · {order_id}", body)


def render_error(title: str, detail: str, *, status_note: str = "") -> bytes:
    body = (f"<h1 class=\"bad\">{_esc(title)}</h1>"
            f"<div class=\"card\"><pre>{_esc(detail)}</pre></div>"
            + (f"<p class=\"tip\">{_esc(status_note)}</p>" if status_note else ""))
    return _page(title, body)


# --------------------------------------------------------------------------
# 服务
# --------------------------------------------------------------------------
class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr: tuple[str, int], app: "UploadServer") -> None:
        self.app = app
        super().__init__(addr, _Handler)


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    # 标准库缺省把每个请求打到 stderr；本进程的 stdout 已经是房间日志，不再刷一遍。
    def log_message(self, fmt: str, *args: Any) -> None:
        log.info("upload %s " + fmt, self.client_address[0], *args)

    def _send(self, status: int, body: bytes, ctype: str = "text/html; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self) -> None:                       # noqa: N802
        parts = urlsplit(self.path)
        if parts.path == HEALTH_PATH:
            self._send(200, b"ok\n", "text/plain; charset=utf-8")
            return
        if parts.path != UPLOAD_PATH:
            self._send(404, render_error("没有这个页面", f"{parts.path} 不是补件页；按钮地址是 {UPLOAD_PATH}"))
            return
        q = {k: v[0] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
        order_id = (q.get("order") or "").strip()
        problem = self.server.app.check_order(order_id)
        if problem:
            self._send(404, render_error("这一单补不了", problem))
            return
        kinds = tuple(k for k in (q.get("kinds") or "").split(",") if k)
        self._send(200, render_form(
            order_id, kinds=kinds, reason=(q.get("reason") or "").strip(),
            line=(q.get("line") or "").strip(), max_bytes=self.server.app.max_bytes,
            accept_hint=self.server.app.accept_hint))

    def do_POST(self) -> None:                      # noqa: N802
        app = self.server.app
        if urlsplit(self.path).path != UPLOAD_PATH:
            self._send(404, render_error("没有这个页面", "只收 POST /upload"))
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        cap = app.max_bytes * MAX_FILES + FORM_OVERHEAD
        if length <= 0:
            self._send(400, render_error("没有内容", "请求里没有文件"))
            return
        if length > cap:
            self._send(413, render_error("太大了", f"这次上传 {length} 字节，超过一次最多 {cap} 字节"))
            return
        ctype = self.headers.get("Content-Type") or ""
        body = self.rfile.read(length)
        try:
            fields, files = parse_multipart(ctype, body)
        except ValueError as exc:
            self._send(400, render_error("表单格式不对", str(exc)))
            return
        order_id = (fields.get("order") or "").strip()
        problem = app.check_order(order_id)
        if problem:
            self._send(404, render_error("这一单补不了", problem))
            return
        if not files:
            self._send(400, render_error("没选文件", "一份文件都没有收到；回上一页选一份再传"))
            return
        if len(files) > MAX_FILES:
            self._send(413, render_error("太多了", f"一次最多 {MAX_FILES} 份，这次是 {len(files)} 份"))
            return
        req = UploadRequest(
            order_id=order_id,
            kinds=tuple(k for k in (fields.get("kinds") or "").split(",") if k),
            reason=(fields.get("reason") or "").strip(),
            line=(fields.get("line") or "").strip(),
            uploader=(fields.get("who") or "").strip()[:40],
            files=tuple(files))
        try:
            reply = app.on_upload(req)
        except Exception as exc:                        # noqa: BLE001
            log.exception("补件回调失败 order=%s", order_id)
            self._send(500, render_error("助手这边出错了", f"{type(exc).__name__}: {exc}",
                                         status_note="文件没有挂上；房间里拖一张图也能补"))
            return
        again = upload_url("", {"order_id": order_id, "kinds": list(req.kinds),
                                "reason_raw": req.reason, "line": req.line or None})
        self._send(200, render_result(order_id, str(reply or ""),
                                      back_url=app.back_url, again_url=again))


class UploadServer:
    """补件页服务。``on_upload(UploadRequest) -> str`` 是唯一的出口，返回给人看的回执。

    ``known_orders`` 是「底账里有哪些订单」的取数器（可选）：给了就在 GET / POST 两处
    都先对一遍，没给就不校验。取数器抛了当「查不到」处理 —— 补件页不该因为底账
    读不出来而把整个进程带崩。
    """

    def __init__(self, *, on_upload: Callable[[UploadRequest], str],
                 known_orders: Callable[[], set[str]] | None = None,
                 max_bytes: int = DEFAULT_MAX_BYTES, bind: str = DEFAULT_BIND,
                 back_url: str = "", accept_hint: str = "image/*,.pdf") -> None:
        self.on_upload = on_upload
        self._known_orders = known_orders
        self.max_bytes = int(max_bytes)
        self.bind = bind or DEFAULT_BIND
        self.back_url = back_url
        self.accept_hint = accept_hint
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    # -- 校验 -----------------------------------------------------------------
    def check_order(self, order_id: str) -> str:
        """订单号合不合法。返回问题描述，空串 = 没问题。"""
        if not order_id:
            return "地址里没有订单号"
        if self._known_orders is None:
            return ""
        try:
            known = self._known_orders()
        except Exception as exc:                        # noqa: BLE001
            log.warning("补件页读底账失败（%s: %s）", type(exc).__name__, exc)
            return "底账现在读不出来，稍后再试"
        return "" if order_id in known else f"底账里没有订单 {order_id}"

    # -- 生命周期 --------------------------------------------------------------
    @property
    def address(self) -> tuple[str, int]:
        """实际监听的 ``(host, port)``。``bind`` 写 0 端口时这里才知道真端口。"""
        if self._server is None:
            host, _, port = self.bind.rpartition(":")
            return host or "127.0.0.1", int(port or 0)
        return self._server.server_address[0], self._server.server_address[1]

    @property
    def url(self) -> str:
        host, port = self.address
        return f"http://{host}:{port}{UPLOAD_PATH}"

    def start(self) -> None:
        """起在守护线程上。端口被占直接抛 `OSError` —— 由调用方决定是降级还是退出。"""
        host, _, port = self.bind.rpartition(":")
        self._server = _Server((host or "127.0.0.1", int(port or 0)), self)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="maos-upload", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
