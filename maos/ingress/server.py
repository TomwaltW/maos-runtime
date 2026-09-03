"""Webhook 服务面 —— 三个平台的回调都打到这里。只用标准库 ``http.server``。

## 先回 200，再干活

``/refund`` 要跑一次完整处置（建案、读政策、规划 DAG、过七道闸、发起付款、
轮询观察），秒级。而三个平台的回调超时都在 3-5 秒，超时即重推。**在回调里同步
跑完**的后果不是慢，是：平台判超时 -> 重推 -> 幂等键挡掉第 2、3 条 -> 群里看到
一条回复、日志里三条记录、平台后台一片红。所以收下、校验、去重之后**立刻 200**，
真正的处置丢给后台那一个 worker 线程。

## 为什么 worker 只有一个

``custom_case.run_payload()` 会调 `C.reset_gateways()` 重置一个**进程级**的网关
注册表。两条 ``/refund`` 并行跑，后一条的 reset 会把前一条的网关摘掉，症状是
前一条在发起付款时报「网关未注册」，而它自己的输入毫无问题 —— 极难查。
`IngressRouter` 里那把锁是同一件事的第二道保险（router 可能被别的进程复用）。

## 这个进程该监听在哪

默认 ``127.0.0.1``。三个平台都要求回调地址是**公网 HTTPS**，正确的部署是前面放
一层 nginx/frp 做 TLS 与鉴权，本进程只收内网流量。默认监听 0.0.0.0 等于把一个
自己不做 TLS 的服务直接摆到公网上，而它收的是能触发真金白银的命令。
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from maos.ingress.contracts import (
    CHANNEL_FEISHU, CHANNEL_WECHAT_KF, CHANNEL_WECOM, ChannelAdapter,
    ChannelDepMissing, ChannelNotConfigured, VerifyError, WebhookRequest,
    coerce_headers,
)

log = logging.getLogger("maos.ingress.server")

#: 路径 -> 渠道。写死而不是按渠道名拼，是为了让 URL 稳定：这几个地址要填进三个
#: 平台的后台，改一次就得三边同步改，而改错的症状是「配置能保存、消息收不到」。
ROUTES = {
    "/ingress/feishu": CHANNEL_FEISHU,
    "/ingress/wecom": CHANNEL_WECOM,
    "/ingress/wechat-kf": CHANNEL_WECHAT_KF,
}

#: 请求体上限（字节）。公网面必须有；没有它，一个 Content-Length 声明 2GB 的
#: 请求就能把进程的内存吃光，而这甚至不需要通过签名校验 —— body 是在校验**之前**读的。
MAX_BODY = 1 << 20

HEALTH_PATH = "/healthz"


class IngressServer:
    """把 adapters + router 装成一个可跑的 HTTP 服务。

    ``router`` 只要有 ``handle(InboundMessage) -> str``，所以测试可以塞个替身。
    """

    def __init__(self, adapters: dict[str, ChannelAdapter], router: Any, *,
                 host: str = "127.0.0.1", port: int = 8737) -> None:
        self.adapters = adapters
        self.router = router
        self.host, self.port = host, port
        self._queue: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._httpd: ThreadingHTTPServer | None = None
        self._stop = threading.Event()

    # -- 后台处理 -----------------------------------------------------------
    def _run_worker(self) -> None:
        while not self._stop.is_set():
            try:
                msg = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self.router.handle(msg)
            except Exception:                           # noqa: BLE001
                # worker 是唯一的处理线程，它死了整个入站面就静默停摆 ——
                # HTTP 还照常 200，群里再也没有回音。所以这里什么都不许漏出去。
                log.exception("worker 处理 %s 失败", getattr(msg, "dedup_key", "?"))
            finally:
                self._queue.task_done()

    def submit(self, msg) -> None:
        self._queue.put(msg)

    # -- 生命周期 -----------------------------------------------------------
    def start(self) -> None:
        self._stop.clear()
        self._worker = threading.Thread(target=self._run_worker, daemon=True,
                                        name="ingress-worker")
        self._worker.start()
        self._httpd = ThreadingHTTPServer((self.host, self.port), _make_handler(self))
        log.info("ingress 监听 http://%s:%d  路由：%s",
                 self.host, self.port, ", ".join(ROUTES))

    def serve_forever(self) -> None:
        if self._httpd is None:
            self.start()
        assert self._httpd is not None
        try:
            self._httpd.serve_forever()
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        self._stop.set()
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._worker is not None:
            self._worker.join(timeout=2)
            self._worker = None


def _make_handler(server: IngressServer):
    class Handler(BaseHTTPRequestHandler):
        # 默认实现往 stderr 打一行 Apache 风格日志，绕过 logging，压不住也过不了脱敏。
        def log_message(self, fmt: str, *args) -> None:      # noqa: A003
            log.debug("%s - %s", self.address_string(), fmt % args)

        def do_GET(self) -> None:                            # noqa: N802
            self._handle("GET")

        def do_POST(self) -> None:                           # noqa: N802
            self._handle("POST")

        # -- 主流程 ---------------------------------------------------------
        def _handle(self, method: str) -> None:
            parsed = urlsplit(self.path)
            if parsed.path == HEALTH_PATH:
                return self._send(200, json.dumps({"ok": True}))

            channel = ROUTES.get(parsed.path)
            if channel is None:
                return self._send(404, "")
            adapter = server.adapters.get(channel)
            if adapter is None or not adapter.configured:
                # 503 而不是 404：地址是对的，是我方没配好。两者在平台后台的
                # 「回调失败」列表里长得一样，但排查方向完全相反。
                log.warning("渠道 %s 未配置，拒收回调", channel)
                return self._send(503, "")

            body = self._read_body()
            if body is None:
                return                                       # 已在 _read_body 里应答

            req = WebhookRequest(
                method=method, path=parsed.path,
                headers=coerce_headers(self.headers),
                body=body, query=dict(parse_qsl(parsed.query, keep_blank_values=True)),
            )

            try:
                adapter.verify(req)
            except VerifyError as exc:
                # 只在日志里说原因，**响应体保持空**：把「时间戳过期」还是「签名不符」
                # 告诉对面，等于给爆破的人一个进度条。
                log.warning("拒收 %s 回调：%s", channel, exc)
                return self._send(401, "")
            except (ChannelNotConfigured, ChannelDepMissing) as exc:
                log.error("渠道 %s 不可用：%s", channel, exc)
                return self._send(503, "")

            try:
                answer = adapter.challenge(req)
            except Exception as exc:                         # noqa: BLE001
                log.error("渠道 %s URL 验证失败：%s", channel, exc)
                return self._send(400, "")
            if answer is not None:
                return self._send(200, answer)

            try:
                messages = adapter.parse(req)
            except Exception as exc:                         # noqa: BLE001
                # 解析失败仍回 200：已经过了签名校验，是自家平台发来的。回非 2xx
                # 会招来重推，而重推同样解析不了 —— 三次之后平台还会把回调置灰。
                log.exception("渠道 %s 解析失败：%s", channel, exc)
                return self._send(200, "")

            for msg in messages:
                server.submit(msg)
            self._send(200, "")

        # -- 工具 -----------------------------------------------------------
        def _read_body(self) -> bytes | None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._send(400, "")
                return None
            if length > MAX_BODY:
                # 回 413 后**不把剩下的 body 读完**。代价是客户端可能在写入途中
                # 收到 RST 而看不到这个 413（nginx 的 client_max_body_size 同款
                # 行为）；换来的是超限请求一个字节都不进内存 —— 而「先读完再拒」
                # 恰好把这道闸想挡的事情做了一遍。
                log.warning("请求体 %d 字节超过上限 %d，已拒", length, MAX_BODY)
                self.close_connection = True
                self._send(413, "")
                return None
            return self.rfile.read(length) if length > 0 else b""

        def _send(self, code: int, text: str) -> None:
            payload = text.encode("utf-8")
            self.send_response(code)
            # 企微的 URL 验证要求把明文 echostr **原样**回去，加了 JSON 包装或
            # 引号就配不通；所以统一用 text/plain，由 adapter 决定回什么。
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if payload:
                self.wfile.write(payload)

    return Handler


def build_adapters(env: dict[str, str] | None = None) -> dict[str, ChannelAdapter]:
    """按环境变量装配三个 adapter。**没配的也装进去** —— 由 `configured` 报状态。

    不装进去的话，`/ingress/wecom` 会回 404，而 404 的含义是「地址错了」；
    运维会去改回调地址，而问题是环境变量没配。503 才指向正确的方向。
    """
    from maos.ingress.feishu import FeishuAdapter, FeishuConfig
    from maos.ingress.wecom import WeChatKfAdapter, WeComAdapter

    return {
        CHANNEL_FEISHU: FeishuAdapter(FeishuConfig.from_env(env)),
        CHANNEL_WECOM: WeComAdapter.from_env(env),
        CHANNEL_WECHAT_KF: WeChatKfAdapter.from_env(env),
    }
