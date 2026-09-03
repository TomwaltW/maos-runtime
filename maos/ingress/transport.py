"""出网调用 —— 三个平台的 OpenAPI 都从这里发。只用标准库，核心依赖恒为空。

**为什么不 `requests`**：`pyproject.toml` 的 ``dependencies = []`` 是这个仓库的一条
硬口径（PG、matrix-nio、OTel 全是可选组）。渠道层是「最前面」，如果它把一个必装
依赖带进来，`python3 run.py` 在干净机器上就不再是零依赖跑得起来。urllib 够用。

## 证书这一条会咬人

本机 python3 是 Python.framework，**没装根证书**，任何 https 都 ``CERTIFICATE_VERIFY_FAILED``。
它的正确解法是给进程一份 CA bundle（``SSL_CERT_FILE``），**不是关掉校验**：
关校验会让出网调用在中间人面前完全不设防，而这一层发出去的东西带着
tenant_access_token。所以这里只做一件事 —— 把那个原本很晦涩的报错翻成能直接照做
的一行。见 :class:`TlsTrustMissing`。
"""

from __future__ import annotations

import json
import logging
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping

log = logging.getLogger("maos.ingress.http")

#: 单次调用超时。IM 平台的回调超时普遍在 3-5 秒，我方发消息又是在回调里同步做的，
#: 拖过对方的超时就会触发重推（然后被幂等键挡掉，看起来像「消息只发了一条但日志有三条」）。
DEFAULT_TIMEOUT = 5.0

#: 取附件的超时。比上面那条长，且刻意分开：附件取件不在「必须赶在平台重推前
#: 回 200」那条同步路径上，用 5 秒去卡一张手机照片只会把正常的慢网判成故障。
ATTACHMENT_TIMEOUT = 30.0


class TlsTrustMissing(RuntimeError):
    """本进程没有可用的根证书。**环境错，不是平台故障。**

    单独一个类型是因为它的处置和别的出网失败完全不同：网络抖动重试就好，这一条
    重试一万次都一样。而两者原本共用同一条 ``URLError`` 分支，报出来的是
    ``<urlopen error [SSL: CERTIFICATE_VERIFY_FAILED]>`` —— 第一反应会去查平台
    的证书、查代理，而问题在自己这台机器上。
    """


class ApiError(RuntimeError):
    """平台返回了业务错误码。消息里带 code 与 msg，**不带请求体**（里面有 token）。"""


def _describe_ssl(exc: BaseException) -> TlsTrustMissing:
    return TlsTrustMissing(
        f"TLS 根证书不可用（{exc}）。这台机器的 python3 没带 CA bundle，"
        "给进程指一份即可，不要关掉证书校验：\n"
        "  export SSL_CERT_FILE=$(python3 -m certifi 2>/dev/null || "
        "echo /etc/ssl/cert.pem)\n"
        "确认：python3 -c \"import ssl;ssl.create_default_context().load_default_certs()\""
    )


def request_json(url: str, *, method: str = "GET",
                 payload: Any = None,
                 headers: Mapping[str, str] | None = None,
                 params: Mapping[str, str] | None = None,
                 timeout: float = DEFAULT_TIMEOUT) -> dict:
    """发一次请求并解析 JSON 响应。失败一律抛，不返回半个结果。

    抛而不返回 ``{}`` 是刻意的：调用方拿到空 dict 会当成「平台说没有消息」继续走，
    于是一次网络故障被记成一条正常的空回调 —— 那正是最难查的一类。
    """
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    data = None
    hdrs = {"Content-Type": "application/json; charset=utf-8", **(headers or {})}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    body, _ = _fetch(url, method=method, data=data, headers=hdrs, timeout=timeout)

    try:
        out = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApiError(f"{_safe_url(url)} 返回的不是 JSON：{body[:200]!r}") from exc
    if not isinstance(out, dict):
        raise ApiError(f"{_safe_url(url)} 返回的 JSON 顶层不是对象")
    return out


def _fetch(url: str, *, method: str, data: bytes | None,
           headers: Mapping[str, str], timeout: float) -> tuple[bytes, str]:
    """发一次请求，返回 ``(body, content_type)``。错误翻译集中在这一处。

    从 :func:`request_json` 里抽出来是因为**取附件拿回的是二进制**，走不了那条
    「读完就 json.loads」的路。抽的是网络与错误翻译，不是又写一遍 —— 两份 urlopen
    一定会漂，漂了的症状是其中一条路径上的证书报错重新变回那句晦涩的
    ``CERTIFICATE_VERIFY_FAILED``（见模块抬头）。
    """
    req = urllib.request.Request(url, data=data, headers=dict(headers), method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(), str(resp.headers.get("Content-Type") or "")
    except urllib.error.HTTPError as exc:
        # 平台的 4xx/5xx 里通常有可读的错误说明，读出来比只报状态码有用得多。
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise ApiError(f"{method} {_safe_url(url)} -> HTTP {exc.code}：{detail}") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ssl.SSLCertVerificationError):
            raise _describe_ssl(exc.reason) from exc
        raise ApiError(f"{method} {_safe_url(url)} 连不上：{exc.reason}") from exc
    except ssl.SSLCertVerificationError as exc:      # 少数路径直接抛这个
        raise _describe_ssl(exc) from exc


def request_bytes(url: str, *, headers: Mapping[str, str] | None = None,
                  params: Mapping[str, str] | None = None,
                  timeout: float = ATTACHMENT_TIMEOUT) -> tuple[bytes, str]:
    """取一份二进制，返回 ``(body, content_type)``。

    超时**比 JSON 调用长**（见 :data:`ATTACHMENT_TIMEOUT`）：一张 5MB 的照片
    在 5 秒里拉不完是常态，而这一次拉取不发生在回调的同步路径上 —— 平台的
    重推窗口约束的是「多久回 200」，附件取件已经在那之后了。

    有些平台在**出错时改回 JSON**（飞书取件失败会返回 ``{"code":234005,...}``
    而不是 4xx）。这里不替调用方判：JSON 与图片在字节层面分不出对错，
    只有知道自家协议的 adapter 才判得了。所以把 content_type 一并交回去。
    """
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    return _fetch(url, method="GET", data=None,
                  headers=dict(headers or {}), timeout=timeout)


def _safe_url(url: str) -> str:
    """URL 进日志前脱敏。企微把 ``access_token`` 放在 **query 参数**里（铁律 6）。

    只去掉值、保留键名：``?access_token=***`` 仍然告诉你这个调用带了 token，
    而整条 query 一起抹掉会让「参数拼错了」变得无法排查。
    """
    parsed = urllib.parse.urlsplit(url)
    if not parsed.query:
        return url
    pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    masked = [(k, "***" if _is_secret(k) else v) for k, v in pairs]
    return urllib.parse.urlunsplit(
        parsed._replace(query=urllib.parse.urlencode(masked)))


def _is_secret(key: str) -> bool:
    lowered = key.lower()
    return any(w in lowered for w in ("token", "secret", "key", "signature", "sign"))
