"""第二家协议：Anthropic Messages 客户端 + provider 注册表（T55）。

在此之前全仓只有**一条**真模型协议路径：``client.py::GatewayModelClient`` 的
OpenAI 兼容 ``POST {base_url}/chat/completions``。本模块补上并列的第二条
``POST {base_url}/v1/messages``，两家共用同一套安全设施与 usage 归一化。

**本模块只造零件，不接线**：``select_model_client()`` 仍然只认 OpenAI 兼容那条，
「按角色选 provider」「把新 provider 接进构造入口」都不在这里 —— 那会牵动
``client.py`` 与 worker/flows，属于后续轨。这里给的是可被路由表按名取用的
:data:`PROVIDERS` 与一个能独立实例化、独立测试的客户端类。

铁律 6：本模块不含任何密钥字面量。:class:`ProviderSpec` 只存**变量名**
（``api_key_env``），值一律由调用方从环境变量现取现传。
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

# 三件安全设施 + usage 归一化一律从 client.py **import 复用**，不复制粘贴。
# 复制会立刻长出第二套口径：脱敏规则、重定向判据、usage 两家方言的加总方式
# 都会各自漂移，而 T54 刚把 usage 口径对齐过一次。私有名（下划线）跨模块
# import 是刻意的 —— 它们是同一个包内的内部设施，不是对外 API；宁可 import
# 私有名，也不要为了「不碰私有名」而抄一份出来。
from maos.model.client import (
    DEFAULT_TIMEOUT,
    ENV_API_KEY,
    GatewayModelClient,
    ModelClient,
    ModelResponse,
    RedirectRefused,
    _SameOriginRedirectHandler,
    _scrub,
    _usage_tokens,
)

log = logging.getLogger("maos.model")

#: Anthropic 要求每个请求显式声明 API 版本。这不是可选头 —— 缺了直接 400。
#: 写死成常量而不是让调用方传：版本决定了请求/响应的字段形状，本文件的解析
#: 逻辑是按这个版本写的，允许外部随便改版本等于让解析静默错位。
ANTHROPIC_VERSION = "2023-06-01"

#: 官方入口。与 openai-compat 不同，这家有稳定的官方地址，可以给默认值。
DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com"

#: Anthropic 的 ``max_tokens`` 是**必填**，没有服务端默认值可依赖，所以这里必须
#: 有一个我方默认。取 4096：够一次规划/编码回复写完，又不至于让一次跑飞的生成
#: 把成本拉到失控。调用方可以按角色覆盖。
DEFAULT_MAX_TOKENS = 4096

#: 本家的 key 环境变量名。用 ``MAOS_`` 前缀与仓库既有配置面（``MAOS_LLM_*``）
#: 保持一致，而不是复用 Anthropic 官方 SDK 的 ``ANTHROPIC_API_KEY`` ——
#: 后者是别人的约定，撞上了分不清是谁在读。
ENV_ANTHROPIC_API_KEY = "MAOS_ANTHROPIC_API_KEY"


class AnthropicModelClient(ModelClient):
    """Anthropic Messages 协议的真模型客户端（``POST {base_url}/v1/messages``）。

    与 :class:`~maos.model.client.GatewayModelClient`（OpenAI 兼容）有**四处实质
    差异**，每一处都是「照抄 OpenAI 那套就会 400 或解析不出来」的地方：

    1. 鉴权头是 ``x-api-key: <key>``，不是 ``Authorization: Bearer <key>``。
       两家的鉴权头名字不同，不是风格差异 —— 发错头名等于没带凭据，401。
    2. 必带 ``anthropic-version`` 头。缺了直接 400，没有隐式默认版本。
    3. ``system`` 是请求体的**顶层参数**，不是 ``messages`` 里一条
       ``role: "system"`` 的消息。Anthropic 的 ``messages`` 只接受
       ``user`` / ``assistant`` 两种 role，塞 system 进去会被拒。
    4. ``max_tokens`` 是**必填**。OpenAI 兼容那家不传就由服务端兜底，这家不传
       直接 400 —— 所以本类必须自带默认值（:data:`DEFAULT_MAX_TOKENS`）。

    其余一切与 ``GatewayModelClient`` 同口径，且是 import 复用而非复制：
    私有 ``_api_key`` 属性、不含 key 的显式 ``__repr__``、异常文本过 ``_scrub()``、
    出网异常一律 ``from None`` 掐断链、自建带
    :class:`~maos.model.client._SameOriginRedirectHandler` 的 opener、
    usage 走 :func:`~maos.model.client._usage_tokens`。

    tier 同样不参与选模型（模型由 ``model`` 参数定死），只作为 ``X-MAOS-Tier``
    路由头透出并记进 ``meta`` —— 「tier -> 具体模型」是治理决策，属于路由层，
    不属于客户端，更不属于 Agent。
    """

    def __init__(self, base_url: str = DEFAULT_ANTHROPIC_BASE_URL, api_key: str = "",
                 model: str = "", timeout: float = DEFAULT_TIMEOUT,
                 max_tokens: int = DEFAULT_MAX_TOKENS) -> None:
        self.base_url = (base_url or DEFAULT_ANTHROPIC_BASE_URL).rstrip("/")
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens
        self._api_key = api_key
        # 自建 opener，不走 urllib.request.urlopen 的全局默认 opener：默认 opener
        # 会跟随跨源 3xx 并把请求头（这里是 x-api-key）原样搬过去 —— 那是把 key
        # **发**到别人服务器上，脱敏/from None 那几道一道都拦不住。
        self._opener = urllib.request.build_opener(_SameOriginRedirectHandler())

    def __repr__(self) -> str:
        # 显式 __repr__ 把「repr 里有什么」钉死：默认 object.__repr__ 天然不含 key，
        # 但那是缺省行为，哪天有人给这个类加 @dataclass，key 当场进 repr、进
        # pytest 的对象打印、进 traceback。base_url/model 留着是排错要用的。
        return (f"AnthropicModelClient(base_url={self.base_url!r}, "
                f"model={self.model!r})")

    def complete(self, *, system: str, user: str, tier: str) -> ModelResponse:
        body = json.dumps({
            "model": self.model,
            # 差异 4：必填，缺了 400。
            "max_tokens": self.max_tokens,
            # 差异 3：顶层参数，不是 messages 里的一条 role: system。
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "temperature": 0,       # 演示要可复现，不要采样随机性
        }, ensure_ascii=False).encode("utf-8")

        req = urllib.request.Request(
            f"{self.base_url}/v1/messages", data=body, method="POST",
            headers={
                "Content-Type": "application/json",
                # 差异 1：x-api-key，不是 Authorization: Bearer。
                "x-api-key": self._api_key,
                # 差异 2：必带，缺了 400。
                "anthropic-version": ANTHROPIC_VERSION,
                "X-MAOS-Tier": tier,
            },
        )
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except RedirectRefused as exc:
            raise RuntimeError(
                f"Anthropic 端点要求跳转到 {exc.origin_to}（HTTP {exc.code}），已拒绝："
                f"x-api-key 头不出 {exc.origin_from}。请把 base_url 直接配成最终地址"
            ) from None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            raise RuntimeError(
                f"Anthropic 返回 HTTP {exc.code}：{_scrub(detail, self._api_key)}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(
                f"Anthropic 端点不可达：{_scrub(str(exc), self._api_key)}") from None
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Anthropic 响应不是合法 JSON：{exc}") from None

        text, skipped = _join_text_blocks(data)

        usage = data.get("usage") or {}
        tokens_in, tokens_out, usage_detail = _usage_tokens(usage)
        meta = {
            "tier": tier,
            # Anthropic 叫 stop_reason，OpenAI 兼容那家叫 finish_reason。不强行
            # 改名成一样的：两家的取值集合本来就不同，混成一个字段名会让下游
            # 以为可以按同一套值判断。
            "stop_reason": data.get("stop_reason") or "",
            "usage_detail": usage_detail,
        }
        if skipped:
            # 跳过的块**不静默丢**：将来 tool_use / thinking 块出现时，「回复怎么
            # 是空的」得能在 meta 里一眼查到是被跳过了，而不是去猜。
            meta["skipped_block_types"] = skipped
        return ModelResponse(
            text=text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model=str(data.get("model") or self.model),
            meta=meta,
        )


def _join_text_blocks(data: dict) -> tuple[str, list[str]]:
    """从 Messages 响应里拼出正文，并把跳过的块类型列出来。

    差异之五（解析侧）：正文在 ``content[]`` 这个**块数组**里，不是
    ``choices[0].message.content`` 那样一个字符串。一次回复可以有多个块，
    未来还会混入 ``tool_use`` / ``thinking`` 等非文本块。

    非 text 块**跳过而不是报错**：多一种块类型就整个调用失败，等于把「协议向前
    兼容地加了东西」变成故障。但也不能静默丢 —— 类型记进返回值，由调用方写进
    ``ModelResponse.meta``。
    """
    content = data.get("content")
    if not isinstance(content, list):
        raise RuntimeError("Anthropic 响应不符合 Messages 协议：缺 content[] 块数组")

    parts: list[str] = []
    skipped: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            skipped.append(type(block).__name__)
            continue
        if block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
        else:
            skipped.append(str(block.get("type") or "unknown"))
    if skipped:
        log.info("Anthropic 响应含 %d 个非文本块（已跳过）：%s", len(skipped), skipped)
    return "".join(parts), skipped


@dataclass(frozen=True)
class ProviderSpec:
    """一家 provider 的规格。形状先定死，供后续的路由表按名取用。

    ``api_key_env`` 只存**变量名**，绝不存值（铁律 6）：这张表是模块级常量，
    会被 import、被打印、被写进文档；任何真实 key 一旦进来就等于进文件。
    构造客户端时由调用方自己 ``os.environ.get(spec.api_key_env)`` 现取现传。

    ``default_base_url`` 允许是空字符串，语义是「没有可信默认值，必须由 env
    显式给」—— openai-compat 就是这种：它是一族兼容实现（自建网关、各家云的
    兼容端点），地址各不相同，给任何默认值都是猜。
    """

    name: str
    factory: Callable[..., ModelClient]
    default_base_url: str
    api_key_env: str
    notes: str = ""


PROVIDERS: dict[str, ProviderSpec] = {
    "openai-compat": ProviderSpec(
        name="openai-compat",
        # 引用 client.py 里已有的实现，不在本模块重写一份。
        factory=GatewayModelClient,
        default_base_url="",
        api_key_env=ENV_API_KEY,
        notes="POST {base_url}/chat/completions；base_url 必须显式配置（兼容端点地址各异）",
    ),
    "anthropic": ProviderSpec(
        name="anthropic",
        factory=AnthropicModelClient,
        default_base_url=DEFAULT_ANTHROPIC_BASE_URL,
        api_key_env=ENV_ANTHROPIC_API_KEY,
        notes=f"POST {{base_url}}/v1/messages；x-api-key + anthropic-version: "
              f"{ANTHROPIC_VERSION}；max_tokens 必填",
    ),
}
