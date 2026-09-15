"""跨境电商适配器的 HTTP 出口 —— **一个可替换的传输层，不是一个 HTTP 客户端库**。

## 为什么不用 requests

`pyproject.toml` 的 `dependencies = []` 不是懒，是这个仓库的一条硬线：核心零依赖，
可选能力一律**惰性 import + 缺了显式抛**（见 `store/pg_store.py` 的 `PgBackendUnavailable`
与 `ingress/crypto.py` 的 `ChannelDepMissing`）。七个电商平台的适配器全都只需要
「发一个带签名头的 HTTPS 请求、把 body 读回来」，标准库 `urllib.request` 够用；
为这点事把 requests 提成核心依赖，等于让所有不碰电商的部署也背上它。

## 为什么要抽成 Protocol，而不是直接在适配器里调 urllib

两个理由，第二个是决定性的：

1. 七个平台的重试、超时、限流退避是同一件事，写七遍会漂。
2. **测试必须打不到真网。** 适配器的正确性 = 请求构造对不对 + 响应解析对不对，
   这两件事都不需要真实网络，而真实网络会让测试变成「今天 Shopify 沙箱在不在」的
   函数。`FakeTransport` 把「平台会回什么」变成用例里的**数据**，于是
   「Shopify 返回 429 时我们退避了几次」这种事才测得出来 —— 打真网是测不出来的，
   你没法让平台按需给你一个 429。

## 密钥不经过这一层

`headers` 里会有 `X-Shopify-Access-Token` 之类的东西，所以本层**不打印 headers**，
异常信息里只带 method / url / status。口径同 `gateway.AlipaySandboxAdapter.__repr__`
与 `model/client.py::GatewayModelClient`：密钥不进 repr、不进 traceback、不进日志。
URL 本身也可能带 query 签名（Lazada / Shopee 都是签在 query 上的），所以
`redact_url()` 在进异常前把 query 抹掉。
"""

from __future__ import annotations

import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Protocol


DEFAULT_TIMEOUT = 20.0
"""单次请求超时（秒）。比 `ingress` 那边短 —— 电商查单是**执行前**的阻塞动作，
挂在这里等于把整条退款链路卡住；宁可失败让上层重试。"""

DEFAULT_MAX_RETRIES = 3
"""含首次在内的总尝试次数上限。只对**可重试**的状态码生效，见 `RETRIABLE_STATUS`。"""

RETRIABLE_STATUS = frozenset({429, 500, 502, 503, 504})
"""可重试的 HTTP 状态。

**401/403 不在里面**：那是签名或授权错了，重试只会把同一个错误再犯两遍，
还会撞平台的风控阈值。**404 也不在里面**：订单不存在是一个结论，不是一次失败。
"""

MAX_BACKOFF = 30.0
"""退避上限（秒）。平台给的 `Retry-After` 比这个还长时**不睡够**，直接判失败返回 ——
睡 5 分钟等于把调用方挂死，而调用方（`order.query`）是退款执行前的同步动作。"""


class TransportError(RuntimeError):
    """传输层失败 —— 网络断了、超时、重试到顶仍是 5xx。

    **与「平台返回了一个错误响应」是两回事**：后者是 `HttpResponse(status=4xx)`
    正常返回给适配器，由适配器按各家错误码归一（`codes.py`）。只有「根本没拿到
    一个完整的 HTTP 响应」才抛这个。
    """


@dataclass(frozen=True)
class HttpResponse:
    """一次 HTTP 往返的结果。`frozen` 同 `GatewayReceipt` / `ExternalOrder` 的理由：
    它是一条**观察记录**，改它等于篡改平台说过的话。"""

    status: int
    headers: dict[str, str]
    body: bytes

    def text(self, encoding: str = "utf-8") -> str:
        return self.body.decode(encoding, errors="replace")

    def header(self, name: str, default: str = "") -> str:
        """按名取头，**大小写不敏感** —— HTTP 头名不区分大小写，而各平台的大小写
        写法不一致（Shopify 给 `X-Shopify-Shop-Api-Call-Limit`，
        Amazon 给 `x-amzn-RateLimit-Limit`）。按字面 key 取会漏。"""
        lowered = name.lower()
        for key, value in self.headers.items():
            if key.lower() == lowered:
                return value
        return default


def redact_url(url: str) -> str:
    """把 URL 的 query 抹掉再进异常 / 日志。

    Lazada 与 Shopee 的签名是拼在 query 上的（`sign=...`），TikTok Shop 的
    `access_token` 也可以走 query。这些串进 traceback 就等于泄密，而 traceback
    是会被 pytest 打出来、被 evidence 收走的 —— 铁律 6 那条「禁止让密钥出现在
    evidence 的任何输出里」拦的正是这条路径。
    """
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return "<unparsable-url>"
    if not parts.query:
        return url
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "<redacted>", ""))


class HttpTransport(Protocol):
    """传输层的**唯一**动作。签名冻结，七个适配器都只按它写。"""

    def request(self, method: str, url: str, *, headers: dict[str, str] | None = None,
                body: bytes | None = None, timeout: float = DEFAULT_TIMEOUT) -> HttpResponse:
        """发一次请求。拿不到完整响应抛 `TransportError`，**不返回一个空响应**。

        4xx / 5xx 是**正常返回**（`HttpResponse.status` 带着码回来），不抛 ——
        平台的错误响应体里有错误码，那是适配器要解析的东西，在这里抛掉就没了。
        """
        ...


class UrllibTransport:
    """默认实现：标准库 `urllib.request`，零依赖。

    重试只对 `RETRIABLE_STATUS` 生效，退避是指数 + 平台的 `Retry-After` 取大者。
    **不做 jitter**：这里的并发度是 1（退款链路串行查单），jitter 解决的是
    惊群，而惊群在这个调用点不存在；加了反而让测试不可复现。
    """

    def __init__(self, *, max_retries: int = DEFAULT_MAX_RETRIES,
                 timeout: float = DEFAULT_TIMEOUT,
                 sleep: object = None) -> None:
        self.max_retries = max(1, int(max_retries))
        self.timeout = float(timeout)
        # sleep 可注入 —— 测试要验「退避了几次、每次多久」，而真 sleep 会让
        # 那条用例跑 30 秒。注入之后退避时长成为可断言的**数据**。
        self._sleep = sleep if callable(sleep) else time.sleep

    def request(self, method: str, url: str, *, headers: dict[str, str] | None = None,
                body: bytes | None = None, timeout: float | None = None) -> HttpResponse:
        wait = timeout if timeout is not None else self.timeout
        last_status = 0
        for attempt in range(1, self.max_retries + 1):
            req = urllib.request.Request(url, data=body, method=method.upper())
            for key, value in (headers or {}).items():
                req.add_header(key, value)
            try:
                with urllib.request.urlopen(req, timeout=wait) as resp:      # noqa: S310
                    return HttpResponse(status=int(resp.status),
                                        headers=dict(resp.headers.items()),
                                        body=resp.read())
            except urllib.error.HTTPError as exc:
                # HTTPError 也是一个**完整响应** —— body 里有平台的错误码。
                # urllib 把 4xx/5xx 抛成异常是它的历史包袱，不是语义上的失败。
                resp = HttpResponse(status=int(exc.code),
                                    headers=dict(exc.headers.items()) if exc.headers else {},
                                    body=exc.read() if hasattr(exc, "read") else b"")
                if resp.status not in RETRIABLE_STATUS or attempt >= self.max_retries:
                    return resp
                last_status = resp.status
                self._sleep(_backoff(attempt, resp.header("Retry-After")))
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt >= self.max_retries:
                    raise TransportError(
                        f"{method.upper()} {redact_url(url)} 传输失败"
                        f"（第 {attempt}/{self.max_retries} 次）：{type(exc).__name__}"
                    ) from exc
                self._sleep(_backoff(attempt, ""))
        raise TransportError(
            f"{method.upper()} {redact_url(url)} 重试 {self.max_retries} 次仍未成功"
            f"（末次 status={last_status}）"
        )


def _backoff(attempt: int, retry_after: str) -> float:
    """指数退避与平台 `Retry-After` 取大者，封顶 `MAX_BACKOFF`。

    取大者而不是取平台值：平台说 1 秒不代表我们就该 1 秒后再撞上去，
    但平台说 10 秒时**必须**听 —— 那是它的限流窗口，早去一次就是白挨一次 429。
    """
    base = min(2.0 ** (attempt - 1), MAX_BACKOFF)
    try:
        hinted = float(retry_after) if retry_after else 0.0
    except ValueError:
        hinted = 0.0            # Retry-After 也可以是 HTTP-date；不解析，按退避走
    return min(max(base, hinted), MAX_BACKOFF)


@dataclass
class FakeTransport:
    """测试与靶场用的传输层。**账本是预置的，不是编的。**

    与 `MockOrderSystem` / `MockGateway` 同一条纪律：没预置的请求**抛**，
    不返回一个 200 空 body。一个「什么都回一点」的桩会让「适配器把 URL 拼错了」
    这类 bug 全部测不出来 —— 那正是这一层最该守住的东西。
    """

    #: key 是 ``(METHOD, url)``；value 是要回的响应。url 按**完整串**匹配，
    #: 包括 query —— 签名拼错一个字符就该 KeyError，那是本类存在的意义。
    responses: dict[tuple[str, str], HttpResponse] = field(default_factory=dict)
    #: 按到达顺序记下每一次请求，供用例断言「请求构造对不对」。
    calls: list[dict] = field(default_factory=list)

    def expect(self, method: str, url: str, *, status: int = 200,
               body: bytes | str = b"", headers: dict[str, str] | None = None) -> "FakeTransport":
        """预置一条响应。返回 self，可链式。"""
        raw = body.encode("utf-8") if isinstance(body, str) else body
        self.responses[(method.upper(), url)] = HttpResponse(
            status=status, headers=dict(headers or {}), body=raw)
        return self

    def request(self, method: str, url: str, *, headers: dict[str, str] | None = None,
                body: bytes | None = None, timeout: float = DEFAULT_TIMEOUT) -> HttpResponse:
        key = (method.upper(), url)
        # headers 原样记下来给用例断言签名头 —— 这是进程内的测试替身，
        # 不落盘、不进日志，与 `redact_url` 守的不是同一条路径。
        self.calls.append({"method": key[0], "url": url,
                           "headers": dict(headers or {}), "body": body})
        if key not in self.responses:
            known = "\n  ".join(f"{m} {u}" for m, u in sorted(self.responses)) or "（空）"
            raise KeyError(
                f"FakeTransport 没有预置 {key[0]} {url}\n已预置：\n  {known}\n"
                "—— 不兜底成 200 空响应：那会把「URL 拼错了」伪装成「平台没返回数据」"
            )
        return self.responses[key]
