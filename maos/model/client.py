"""模型调用抽象 —— 后续接 Higress AI Gateway 时只改这一个文件。

刻意做成注入式：Agent 不知道背后是通义、DeepSeek 还是 OpenAI，只知道 tier。
tier 到具体模型的映射是治理决策，属于网关的职责，不属于 Agent。

真模型分支只读环境变量（铁律 6：密钥不进任何文件、不进 evidence）：
``MAOS_LLM_BASE_URL`` / ``MAOS_LLM_API_KEY`` / ``MAOS_LLM_MODEL`` /
``MAOS_LLM_TIMEOUT``（默认 120s）。三个必填项缺任何一个都降级回 ScriptedModelClient，
**不发起任何网络请求** —— 无 key 的机器上跑测试与场景必须是确定性的。
"""

from __future__ import annotations

import json
import logging
import math
import os
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

log = logging.getLogger("maos.model")

ENV_BASE_URL = "MAOS_LLM_BASE_URL"
ENV_API_KEY = "MAOS_LLM_API_KEY"
ENV_MODEL = "MAOS_LLM_MODEL"
ENV_TIMEOUT = "MAOS_LLM_TIMEOUT"

DEFAULT_TIMEOUT = 120.0


class Tier:
    STRONG = "strong"   # 强推理：Manager / Requirement / Architecture
    MEDIUM = "medium"   # 中等：Coding / Testing
    LIGHT = "light"     # 轻量：格式化、分类


@dataclass
class ModelResponse:
    text: str
    tokens_in: int = 0
    tokens_out: int = 0
    model: str = ""
    meta: dict = field(default_factory=dict)


class ModelClient(ABC):
    @abstractmethod
    def complete(self, *, system: str, user: str, tier: str) -> ModelResponse: ...


class ScriptedModelClient(ModelClient):
    """MVP 阶段用的假模型：按 (tier, 关键字) 返回预设答案。

    这样第一步验证的是「事件契约和状态机对不对」，不会被模型输出的随机性干扰。
    换真模型只需要把 main.py 里注入的实例换掉。
    """

    def __init__(self, script: dict[str, str] | None = None) -> None:
        self.script = script or {}
        self.calls: list[dict] = []

    def complete(self, *, system: str, user: str, tier: str) -> ModelResponse:
        self.calls.append({"tier": tier, "system": system[:60], "user": user[:120]})
        for kw, answer in self.script.items():
            if kw in user:
                return ModelResponse(text=answer, tokens_in=len(user) // 4,
                                     tokens_out=len(answer) // 4, model=f"scripted-{tier}")
        return ModelResponse(text="{}", model=f"scripted-{tier}")


class HigressModelClient(ModelClient):
    """占位。Track B 接网关时实现：走 Higress 统一入口，tier 作为路由 header。

    key 的两道防线与 :class:`GatewayModelClient` 对齐：私有属性 + 不含 key 的
    ``__repr__``。**改前并没有现行泄漏** —— 默认的 ``object.__repr__`` 只打类名和
    内存地址，属性一个都不打；泄漏面是公开属性 ``api_key`` 本身（``vars()`` /
    ``__dict__`` / 任何遍历属性的序列化都会把值带出来），以及「谁给这个类加一个
    ``@dataclass`` 或 ``__repr__``，key 当场进 repr」。

    所以这两行买的是**不变量**，不是止血：下划线声明它不是公开 API，显式
    ``__repr__`` 把「repr 里有什么」钉死，不随将来的改动漂移。同文件那三道
    （``_scrub()`` / ``from None`` / :class:`_SameOriginRedirectHandler`）等
    ``complete()`` 真正出网时才轮得到，这个类现在一进来就抛。
    """

    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = base_url
        self._api_key = api_key

    def __repr__(self) -> str:
        return f"HigressModelClient(base_url={self.base_url!r})"

    def complete(self, *, system: str, user: str, tier: str) -> ModelResponse:
        raise NotImplementedError("Track B：接入 Higress 时实现")


def _scrub(text: str, secret: str) -> str:
    """抹掉可能混进异常文本的 api key（铁律 6：密钥不许出现在任何输出里）。"""
    return text.replace(secret, "***") if secret else text


def _safe_int(value: object, default: int = 0) -> int:
    """usage 计数容错：网关给了非整数就回退 default 并告警。

    这一行原本在 complete() 的 try 之外，``int("n/a")`` 会抛 ValueError **逃出**
    统一的 RuntimeError 兜底与脱敏，用户看到的是裸 traceback。回退而不是抛：
    token 计数不该让一次已经成功的模型调用失败；但必须 warning —— 计数错了
    会一路传导到成本统计，静默吞掉等于埋雷。
    """
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        log.warning("模型网关 usage 字段不是整数：%r，按 %d 计（成本统计会偏低）", value, default)
        return default


#: Anthropic 口径里三个都属于**输入侧**、且互不重叠的字段。
#: 缓存读比普通输入便宜，但它们是三笔独立的量，不是同一笔的三种说法 ——
#: 所以 tokens_in 取三者之和，而不是只取 ``input_tokens``。
_ANTHROPIC_INPUT_FIELDS = (
    "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
)


def _usage_tokens(usage: dict) -> tuple[int, int, dict]:
    """从一份 ``usage`` 里读出 (tokens_in, tokens_out, 明细)，认两家口径。

    原来只读 ``prompt_tokens`` / ``completion_tokens`` 这一家。网关后面挂
    Anthropic 系模型时，回的是 ``input_tokens`` / ``output_tokens``，另有
    ``cache_read_input_tokens`` / ``cache_creation_input_tokens`` 两笔单列的
    输入侧用量 —— 这三个字段在旧读法下**被整段丢掉**，落库就是 0，
    而 ``estimated=0`` 还说着「这是网关回的真实计费」。**一次真调用被记成
    零成本**，比没有成本视图更糟：它看起来是有数的。

    两家不混算：
    - OpenAI 口径的 ``prompt_tokens`` **已经包含**命中缓存的部分
      （``prompt_tokens_details.cached_tokens`` 是它的子集），再加一次就是重复计数；
    - Anthropic 口径的三个输入字段互不重叠，所以求和。

    第三个返回值是明细，进 ``ModelResponse.meta``：库里那两列存不下分项
    （表结构是冻结面，不能加列），但排查「这次为什么这么贵」时要看得到。
    """
    if not isinstance(usage, dict) or not usage:
        return 0, 0, {"dialect": "none"}

    if "prompt_tokens" in usage or "completion_tokens" in usage:
        detail = {"dialect": "openai"}
        cached = ((usage.get("prompt_tokens_details") or {}).get("cached_tokens")
                  if isinstance(usage.get("prompt_tokens_details"), dict) else None)
        if cached is not None:
            detail["cached_tokens"] = _safe_int(cached)
        return (_safe_int(usage.get("prompt_tokens")),
                _safe_int(usage.get("completion_tokens")), detail)

    if any(f in usage for f in _ANTHROPIC_INPUT_FIELDS) or "output_tokens" in usage:
        detail = {"dialect": "anthropic"}
        tokens_in = 0
        for field_name in _ANTHROPIC_INPUT_FIELDS:
            value = _safe_int(usage.get(field_name))
            detail[field_name] = value
            tokens_in += value
        return tokens_in, _safe_int(usage.get("output_tokens")), detail

    # 认不出的口径：不猜、不编，如实记成 0 并留声。落库那行的 estimated 仍由
    # 客户端类型决定（core/store.py::usage_is_estimated），本函数不碰它 ——
    # 但 cost_view 会因为「真客户端却 0 token」而显出异常，那正是该被看见的。
    log.warning("模型网关 usage 字段名不认识：%s；本次 token 记为 0（成本统计会偏低）",
                sorted(usage)[:8])
    return 0, 0, {"dialect": "unknown", "keys": sorted(usage)[:8]}


def _origin(url: str) -> str:
    """取 ``scheme://host:port`` 作为 origin —— 三者全等才算「没换主机」。

    刻意不含 userinfo 和 path：这个字符串会进异常文本，不能夹带凭据。
    端口显式补默认值，免得 ``http://h`` 和 ``http://h:80`` 被判成两个 origin。
    """
    parts = urllib.parse.urlsplit(url)
    try:
        port = parts.port
    except ValueError:      # 恶意 Location 里的非法端口，别让 ValueError 逃出兜底网
        port = None
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    return f"{parts.scheme}://{(parts.hostname or '').lower()}:{port}"


class RedirectRefused(Exception):
    """跨 origin 重定向被拒。独立类型，好和网关真正返回的 HTTP 错误分开给口径。"""

    def __init__(self, origin_from: str, origin_to: str, code: int) -> None:
        super().__init__(f"HTTP {code} -> {origin_to}")
        self.origin_from = origin_from
        self.origin_to = origin_to
        self.code = code


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """只放行同 origin 的 3xx，换了 scheme / 主机 / 端口一律拒绝。

    urllib 默认跟随重定向，且 ``HTTPRedirectHandler`` 把原请求头（含
    ``Authorization``）**原样**搬到新请求上。key 不是被打印，是被**发**出去 ——
    ``_api_key`` 私有化、``__repr__``、``_scrub()``、``from None`` 这四道防的都是
    「key 出现在日志/traceback 里」，一道都拦不住「key 出现在别人的服务器上」。
    而 ``MAOS_LLM_BASE_URL`` 是环境变量可配的，配错一个地址就够。

    同 origin 的纯路径跳转（补斜杠、路径规范化）是网关的正常行为，保留。
    同主机的 https -> http 降级也算换 origin：那同样是把 Authorization 明文发上线。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        src, dst = _origin(req.full_url), _origin(newurl)
        if src != dst:
            fp.close()      # 本该由 http_error_302 在本函数返回后关，抛了就轮不到它
            raise RedirectRefused(src, dst, code)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class GatewayModelClient(ModelClient):
    """OpenAI 兼容协议的真模型客户端（POST ``{base_url}/chat/completions``）。

    只用标准库 urllib —— 这一层不值得为它引第三方依赖（改依赖要先问人）。
    tier 不参与选模型：模型由 ``MAOS_LLM_MODEL`` 指定，tier 只作为路由 header 交给
    网关，「tier -> 具体模型」是治理决策，属于网关，不属于 Agent（见模块 docstring）。

    api key 存在单下划线属性里，且 ``__repr__`` 不含它：异常、日志、pytest 的
    对象打印都不该把它带出去。所有出网异常都 ``from None`` 掐断链，
    避免底层 traceback 把 Authorization 头顺出来。以上三条防的都是「key 进日志」；
    「key 进别人的服务器」由 :class:`_SameOriginRedirectHandler` 单独封 ——
    走自建 opener，不用 ``urllib.request.urlopen`` 的全局默认 opener。
    """

    def __init__(self, base_url: str, api_key: str, model: str,
                 timeout: float = DEFAULT_TIMEOUT) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self._api_key = api_key
        # 自建 opener：build_opener 见到 HTTPRedirectHandler 的子类实例就不再装默认那个
        self._opener = urllib.request.build_opener(_SameOriginRedirectHandler())

    def __repr__(self) -> str:
        return f"GatewayModelClient(base_url={self.base_url!r}, model={self.model!r})"

    def complete(self, *, system: str, user: str, tier: str) -> ModelResponse:
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": 0,       # 演示要可复现，不要采样随机性
        }, ensure_ascii=False).encode("utf-8")

        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=body, method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
                "X-MAOS-Tier": tier,
            },
        )
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except RedirectRefused as exc:
            raise RuntimeError(
                f"模型网关要求跳转到 {exc.origin_to}（HTTP {exc.code}），已拒绝："
                f"Authorization 头不出 {exc.origin_from}。"
                f"请把 {ENV_BASE_URL} 直接配成最终地址") from None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            raise RuntimeError(
                f"模型网关返回 HTTP {exc.code}：{_scrub(detail, self._api_key)}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(
                f"模型网关不可达：{_scrub(str(exc), self._api_key)}") from None
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"模型网关响应不是合法 JSON：{exc}") from None

        try:
            choice = data["choices"][0]
            text = choice["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise RuntimeError("模型网关响应不符合 OpenAI 兼容协议：缺 choices[0].message.content") from None

        usage = data.get("usage") or {}
        tokens_in, tokens_out, usage_detail = _usage_tokens(usage)
        return ModelResponse(
            text=text or "",
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model=str(data.get("model") or self.model),
            meta={"tier": tier, "finish_reason": choice.get("finish_reason", ""),
                  "usage_detail": usage_detail},
        )


def _timeout_from_env() -> float:
    """读 MAOS_LLM_TIMEOUT；非数字 / 非有限值 / 非正数一律回退默认，不让配置笔误变成挂死。

    ``inf`` 和 ``nan`` 两条都要单独拦：``float()`` 收它们**不抛 ValueError**，
    而 ``inf <= 0`` 是 False、``nan`` 参与的一切比较都是 False —— 原来那两道闸
    都放行。放行的后果不是超时变长，是 ``socket.settimeout()`` 抛
    OverflowError / ValueError，**逃出** complete() 那张统一 RuntimeError 兜底网，
    错误口径和脱敏一起失效。
    """
    raw = (os.environ.get(ENV_TIMEOUT) or "").strip()
    if not raw:
        return DEFAULT_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        log.warning("%s=%r 不是数字，回退默认 %.0fs", ENV_TIMEOUT, raw, DEFAULT_TIMEOUT)
        return DEFAULT_TIMEOUT
    if not math.isfinite(value):
        log.warning("%s=%r 不是有限数值（inf/nan），回退默认 %.0fs",
                    ENV_TIMEOUT, raw, DEFAULT_TIMEOUT)
        return DEFAULT_TIMEOUT
    if value <= 0:
        log.warning("%s=%r 非正数，回退默认 %.0fs", ENV_TIMEOUT, raw, DEFAULT_TIMEOUT)
        return DEFAULT_TIMEOUT
    return value


def select_model_client(script: dict[str, str] | None = None, *,
                        force_scripted: bool = False) -> ModelClient:
    """选择模型客户端 —— 上层唯一的构造入口，签名与语义冻结（A-12）。

    force_scripted=True 恒返 ScriptedModelClient(script)，一行网络都不走 ——
    场景 5 与全部测试必须显式传它，否则在配了 key 的机器上会开始打真网络。

    未强制时按环境变量决定：``MAOS_LLM_BASE_URL`` / ``MAOS_LLM_API_KEY`` /
    ``MAOS_LLM_MODEL`` 三个都非空才构造 GatewayModelClient；缺任何一个都降级回
    ScriptedModelClient 并只记录**缺失的变量名**（铁律 6：值绝不进日志）。
    ``MAOS_LLM_TIMEOUT`` 可选，默认 120s。
    """
    if force_scripted:
        return ScriptedModelClient(script)

    env = {
        ENV_BASE_URL: (os.environ.get(ENV_BASE_URL) or "").strip(),
        ENV_API_KEY: (os.environ.get(ENV_API_KEY) or "").strip(),
        ENV_MODEL: (os.environ.get(ENV_MODEL) or "").strip(),
    }
    missing = [name for name, value in env.items() if not value]
    if missing:
        # WARNING 而不是 INFO，且正文必须写明**后果** —— 这条降级本身是对的，
        # 坏的是它安静。「以为在跑真模型、其实在跑假模型」不会有任何显眼提示，
        # 而它恰好让这一跑的一整类结论失真：现场 key 配错，「真模型跑通了」
        # 就成了假结论。只说「降级了」，读日志的人还要自己推后果；写明后果，
        # 他一眼知道接下来哪些结论不能信。
        # 铁律 6：这里只打**变量名**（missing 是名字列表），env 一个值都不许带进来。
        log.warning("未配置 %s，降级为确定性 ScriptedModelClient —— 本次运行不发起任何"
                    "网络请求，所有「模型」输出都是脚本回放：成本读数（token/费用）、"
                    "延迟、以及任何与模型行为相关的结论，这一跑一律不成立",
                    "/".join(missing))
        return ScriptedModelClient(script)

    log.info("启用真模型：base_url=%s model=%s", env[ENV_BASE_URL], env[ENV_MODEL])
    return GatewayModelClient(
        base_url=env[ENV_BASE_URL], api_key=env[ENV_API_KEY],
        model=env[ENV_MODEL], timeout=_timeout_from_env(),
    )
