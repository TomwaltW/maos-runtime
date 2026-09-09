"""T55 · 第二家协议（Anthropic Messages）客户端与 provider 注册表。

**一行真网络都不打**：所有请求都在客户端的 ``_opener`` 上截住（真客户端走的是
自建 opener，不是 ``urllib.request.urlopen`` 的全局默认 opener，所以换掉它就
足够拦下全部出网），响应由本文件伪造。因此这套测试在无 key、断网的机器上
必须全绿 —— 那正是仓库的基线环境。

判据分三组：
  · 请求形状 —— 四处与 OpenAI 兼容协议的实质差异，写错任何一处线上就是 400/401；
  · 响应解析 —— content[] 块数组的拼接与非文本块的处置；
  · 安全 —— key 不进 repr / 不进异常文本、跨源重定向被拒、注册表只存变量名。
"""

from __future__ import annotations

import email.message
import io
import json
import os
import urllib.error
import urllib.request

import pytest

from maos.model.client import (
    ENV_API_KEY,
    GatewayModelClient,
    ModelClient,
    RedirectRefused,
    _SameOriginRedirectHandler,
    _usage_tokens,
)
from maos.model.providers import (
    ANTHROPIC_VERSION,
    DEFAULT_ANTHROPIC_BASE_URL,
    DEFAULT_MAX_TOKENS,
    ENV_ANTHROPIC_API_KEY,
    PROVIDERS,
    AnthropicModelClient,
    ProviderSpec,
)

# 一眼能认出来的哨兵，与 test_model_client_hardening.py 同口径：真漏出去 grep 得到。
CANARY = "sk-ant-LEAK-CANARY-0123456789"
BASE_URL = "https://api.example/anthropic"

_TEXT_ONLY = {
    "model": "claude-x",
    "content": [{"type": "text", "text": "hello"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 12, "output_tokens": 3},
}


class _FakeResponse:
    """够 ``with opener.open(...) as resp: resp.read()`` 用的最小响应。"""

    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


class _RecordingOpener:
    """记下请求、回一份固定响应；也可以让它抛异常，模拟出网失败。"""

    def __init__(self, payload: dict | None = None,
                 raises: BaseException | None = None) -> None:
        self.payload = payload if payload is not None else dict(_TEXT_ONLY)
        self.raises = raises
        self.request: urllib.request.Request | None = None
        self.timeout: float | None = None

    def open(self, req, timeout=None):     # noqa: A003 —— 对齐 opener 的接口名
        self.request = req
        self.timeout = timeout
        if self.raises is not None:
            raise self.raises
        return _FakeResponse(self.payload)


def _client(**kwargs) -> AnthropicModelClient:
    kwargs.setdefault("base_url", BASE_URL)
    kwargs.setdefault("api_key", CANARY)
    kwargs.setdefault("model", "claude-x")
    return AnthropicModelClient(**kwargs)


def _call(client: AnthropicModelClient, opener: _RecordingOpener, **kwargs):
    client._opener = opener
    kwargs.setdefault("system", "你是 Manager")
    kwargs.setdefault("user", "拆解这个需求")
    kwargs.setdefault("tier", "strong")
    return client.complete(**kwargs)


# ---------------------------------------------------------------------------
# 一、请求形状：四处与 OpenAI 兼容协议的实质差异
# ---------------------------------------------------------------------------

def test_posts_to_v1_messages_not_chat_completions():
    opener = _RecordingOpener()
    _call(_client(), opener)
    assert opener.request.full_url == f"{BASE_URL}/v1/messages"
    assert opener.request.get_method() == "POST"


def test_auth_header_is_x_api_key_not_bearer():
    """差异 1：头名不同不是风格问题 —— 发成 Authorization 就是 401。"""
    opener = _RecordingOpener()
    _call(_client(), opener)
    headers = {k.lower(): v for k, v in opener.request.header_items()}
    assert headers["x-api-key"] == CANARY
    assert "authorization" not in headers


def test_anthropic_version_header_is_present():
    """差异 2：缺 anthropic-version 直接 400，没有隐式默认版本。"""
    opener = _RecordingOpener()
    _call(_client(), opener)
    headers = {k.lower(): v for k, v in opener.request.header_items()}
    assert headers["anthropic-version"] == ANTHROPIC_VERSION
    assert ANTHROPIC_VERSION, "版本值不能是空串"


def test_system_is_top_level_not_a_message():
    """差异 3：messages 只收 user/assistant，system 塞进去会被拒。"""
    opener = _RecordingOpener()
    _call(_client(), opener, system="你是 Manager")
    body = json.loads(opener.request.data.decode("utf-8"))
    assert body["system"] == "你是 Manager"
    assert [m["role"] for m in body["messages"]] == ["user"]
    assert all(m["role"] != "system" for m in body["messages"])


def test_max_tokens_is_always_sent_and_positive():
    """差异 4：必填。不传就是 400，所以本类必须自带默认值。"""
    opener = _RecordingOpener()
    _call(_client(), opener)
    body = json.loads(opener.request.data.decode("utf-8"))
    assert body["max_tokens"] == DEFAULT_MAX_TOKENS
    assert isinstance(body["max_tokens"], int) and body["max_tokens"] > 0


def test_max_tokens_is_overridable_per_client():
    opener = _RecordingOpener()
    _call(_client(max_tokens=77), opener)
    body = json.loads(opener.request.data.decode("utf-8"))
    assert body["max_tokens"] == 77


def test_tier_rides_as_a_routing_header_and_does_not_pick_the_model():
    """tier 不选模型：模型由构造参数定死，tier 只是路由头 + meta 留痕。"""
    opener = _RecordingOpener()
    resp = _call(_client(model="claude-x"), opener, tier="light")
    headers = {k.lower(): v for k, v in opener.request.header_items()}
    assert headers["x-maos-tier"] == "light"
    assert json.loads(opener.request.data.decode("utf-8"))["model"] == "claude-x"
    assert resp.meta["tier"] == "light"


def test_base_url_trailing_slash_does_not_double_up():
    opener = _RecordingOpener()
    _call(_client(base_url=BASE_URL + "/"), opener)
    assert opener.request.full_url == f"{BASE_URL}/v1/messages"


def test_timeout_is_passed_through_to_the_opener():
    opener = _RecordingOpener()
    _call(_client(timeout=7.5), opener)
    assert opener.timeout == 7.5


# ---------------------------------------------------------------------------
# 二、响应解析：content[] 块数组
# ---------------------------------------------------------------------------

def test_joins_multiple_text_blocks_in_order():
    opener = _RecordingOpener({"content": [{"type": "text", "text": "前"},
                                           {"type": "text", "text": "后"}]})
    assert _call(_client(), opener).text == "前后"


def test_non_text_blocks_are_skipped_but_recorded_in_meta():
    """跳过而不是报错（协议向前兼容加块不该变成故障），但也不许静默丢。"""
    opener = _RecordingOpener({"content": [
        {"type": "text", "text": "答案"},
        {"type": "tool_use", "name": "search", "input": {}},
        {"type": "thinking", "thinking": "…"},
    ]})
    resp = _call(_client(), opener)
    assert resp.text == "答案"
    assert resp.meta["skipped_block_types"] == ["tool_use", "thinking"]


def test_meta_has_no_skipped_key_when_everything_is_text():
    resp = _call(_client(), _RecordingOpener())
    assert "skipped_block_types" not in resp.meta


def test_stop_reason_is_kept_under_its_own_name():
    resp = _call(_client(), _RecordingOpener())
    assert resp.meta["stop_reason"] == "end_turn"
    assert "finish_reason" not in resp.meta, "两家取值集合不同，不许混成一个字段名"


def test_model_echoed_by_the_server_wins():
    opener = _RecordingOpener({"model": "claude-x-20260101",
                               "content": [{"type": "text", "text": "x"}]})
    assert _call(_client(model="claude-x"), opener).model == "claude-x-20260101"


def test_missing_content_array_is_a_protocol_error():
    opener = _RecordingOpener({"model": "claude-x", "usage": {}})
    with pytest.raises(RuntimeError, match="content"):
        _call(_client(), opener)


def test_empty_content_array_yields_empty_text_not_a_crash():
    resp = _call(_client(), _RecordingOpener({"content": []}))
    assert resp.text == ""


# ---------------------------------------------------------------------------
# 三、usage 归一化：复用 client.py 的 _usage_tokens，不许有第二套口径
# ---------------------------------------------------------------------------

_CACHE_USAGE = {
    "input_tokens": 500,
    "cache_read_input_tokens": 8000,
    "cache_creation_input_tokens": 1200,
    "output_tokens": 420,
}


def test_usage_matches_shared_normalizer_exactly():
    """断言的是「与 ``_usage_tokens()`` 口径一致」，不是「等于我在这里算的数」。

    写成对拍而不是硬编码 9700：硬编码的话，将来 ``_usage_tokens`` 改了口径，
    这条会红成「providers 错了」，而实际上两边只是不同步 —— 对拍才能钉住
    「providers 没有自己的第二套解析」这件事本身。
    """
    opener = _RecordingOpener({"content": [{"type": "text", "text": "x"}],
                               "usage": dict(_CACHE_USAGE)})
    resp = _call(_client(), opener)
    tokens_in, tokens_out, detail = _usage_tokens(dict(_CACHE_USAGE))
    assert (resp.tokens_in, resp.tokens_out) == (tokens_in, tokens_out)
    assert resp.meta["usage_detail"] == detail
    assert detail["dialect"] == "anthropic"


def test_cache_fields_are_not_dropped():
    """T54 折账第 18 条的回归闸：缓存两笔被丢掉 = 真调用被记成零成本。"""
    opener = _RecordingOpener({"content": [{"type": "text", "text": "x"}],
                               "usage": dict(_CACHE_USAGE)})
    resp = _call(_client(), opener)
    assert resp.tokens_in == 9700       # 500 + 8000 + 1200，三笔互不重叠
    assert resp.meta["usage_detail"]["cache_read_input_tokens"] == 8000


def test_missing_usage_is_zero_not_an_error():
    resp = _call(_client(), _RecordingOpener({"content": [{"type": "text", "text": "x"}]}))
    assert (resp.tokens_in, resp.tokens_out) == (0, 0)


# ---------------------------------------------------------------------------
# 四、铁律 6：key 一个字节都不许漏出去
# ---------------------------------------------------------------------------

def test_api_key_is_private_not_a_public_attribute():
    client = _client()
    assert not hasattr(client, "api_key"), "key 应挂在私有属性 _api_key 上"
    assert client._api_key == CANARY
    public = [name for name in vars(client) if not name.startswith("_")]
    assert CANARY not in str([getattr(client, n) for n in public]), \
        f"key 从公开属性 {public} 里漏出来了"


def test_repr_is_explicit_and_has_no_key():
    text = repr(_client())
    assert CANARY not in text
    assert "sk-" not in text
    assert text.startswith("AnthropicModelClient("), f"repr 退化成默认 object repr：{text}"
    assert "object at 0x" not in text
    assert BASE_URL in text, "base_url 该留在 repr 里 —— 它是排错要的"


def test_str_and_format_are_also_clean():
    client = _client()
    assert CANARY not in str(client)
    assert CANARY not in f"{client}"
    assert CANARY not in "{}".format(client)      # noqa: UP032 —— 显式覆盖第三条路径


def test_http_error_text_is_scrubbed_of_the_key():
    """网关把请求原样回显进错误体是常见行为，key 会随之进异常文本、进日志。"""
    body = json.dumps({"error": {"message": f"bad x-api-key: {CANARY}"}}).encode()
    err = urllib.error.HTTPError(
        f"{BASE_URL}/v1/messages", 401, "Unauthorized", {}, io.BytesIO(body))
    with pytest.raises(RuntimeError) as exc:
        _call(_client(), _RecordingOpener(raises=err))
    assert CANARY not in str(exc.value)
    assert "***" in str(exc.value)
    assert "401" in str(exc.value)


def test_network_error_text_is_scrubbed_of_the_key():
    err = urllib.error.URLError(f"connect failed with key={CANARY}")
    with pytest.raises(RuntimeError) as exc:
        _call(_client(), _RecordingOpener(raises=err))
    assert CANARY not in str(exc.value)


def test_outgoing_exceptions_break_the_cause_chain():
    """``from None``：不让底层 traceback 把带 key 的请求头顺出去。"""
    err = urllib.error.URLError("boom")
    with pytest.raises(RuntimeError) as exc:
        _call(_client(), _RecordingOpener(raises=err))
    assert exc.value.__cause__ is None
    assert exc.value.__suppress_context__ is True


def test_bad_json_response_is_a_clean_runtime_error():
    class _BadJSONOpener(_RecordingOpener):
        def open(self, req, timeout=None):
            self.request = req

            class _R:
                def read(self_inner):
                    return b"<html>502</html>"

                def __enter__(self_inner):
                    return self_inner

                def __exit__(self_inner, *a):
                    return False

            return _R()

    with pytest.raises(RuntimeError, match="JSON"):
        _call(_client(), _BadJSONOpener())


# ---------------------------------------------------------------------------
# 五、跨源重定向被拒 —— 防的是「key 出现在别人的服务器上」，脱敏那几道拦不住
# ---------------------------------------------------------------------------

def test_opener_is_built_with_the_same_origin_redirect_handler():
    """真客户端不走全局默认 opener：默认那个会把 x-api-key 原样搬到新主机。"""
    client = _client()
    assert any(isinstance(h, _SameOriginRedirectHandler)
               for h in client._opener.handlers)


def test_cross_origin_redirect_is_refused_and_reported_without_the_key():
    refused = RedirectRefused("https://api.example:443", "https://evil.example:443", 302)
    with pytest.raises(RuntimeError) as exc:
        _call(_client(), _RecordingOpener(raises=refused))
    text = str(exc.value)
    assert "evil.example" in text and "302" in text
    assert CANARY not in text
    assert exc.value.__cause__ is None


class _ClosableFp:
    """3xx 分支会 ``fp.close()``（抛异常后就轮不到 http_error_302 去关）。"""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_handler_refuses_when_origin_changes():
    handler = _SameOriginRedirectHandler()
    req = urllib.request.Request(f"{BASE_URL}/v1/messages")
    fp = _ClosableFp()
    with pytest.raises(RedirectRefused):
        handler.redirect_request(req, fp, 302, "Found", email.message.Message(),
                                 "https://evil.example/v1/messages")
    assert fp.closed, "拒绝时要把连接关掉，别泄一个 socket"


def test_handler_still_allows_same_origin_path_redirect():
    """同 origin 的纯路径跳转（补斜杠、路径规范化）是端点的正常行为，不该被误伤。"""
    handler = _SameOriginRedirectHandler()
    req = urllib.request.Request(f"{BASE_URL}/v1/messages")
    new = handler.redirect_request(req, _ClosableFp(), 302, "Found",
                                   email.message.Message(),
                                   f"{BASE_URL}/v1/messages/")
    assert new is not None and new.full_url == f"{BASE_URL}/v1/messages/"


# ---------------------------------------------------------------------------
# 六、provider 注册表
# ---------------------------------------------------------------------------

def test_registry_has_both_protocol_families():
    assert set(PROVIDERS) >= {"openai-compat", "anthropic"}
    for key, spec in PROVIDERS.items():
        assert isinstance(spec, ProviderSpec)
        assert spec.name == key, "键与 spec.name 必须一致，否则按名取用会取错"


def test_registry_points_at_the_existing_gateway_client_not_a_rewrite():
    """openai-compat 只引用 client.py 里已有的实现，本轨不重写第二份。"""
    assert PROVIDERS["openai-compat"].factory is GatewayModelClient
    assert PROVIDERS["anthropic"].factory is AnthropicModelClient


def test_factories_produce_model_clients():
    for spec in PROVIDERS.values():
        client = spec.factory(base_url="https://gw.example", api_key=CANARY,
                              model="m")
        assert isinstance(client, ModelClient)
        assert CANARY not in repr(client)


def test_api_key_env_holds_a_variable_name_never_a_value(monkeypatch):
    """铁律 6 的机器判据：这张表是模块级常量，进来的值就等于写进文件。"""
    for spec in PROVIDERS.values():
        name = spec.api_key_env
        assert name and name.isupper(), f"{spec.name} 的 api_key_env 不像变量名：{name!r}"
        assert name.replace("_", "").isalnum()
        assert name.startswith("MAOS_"), "配置面统一用 MAOS_ 前缀"
        assert "sk-" not in name and CANARY not in name
        # 真变量名的判据：它是能被 os.environ 查的键，而不是一段 key 明文。
        monkeypatch.setenv(name, "probe")
        assert os.environ[name] == "probe"


def test_registry_reuses_the_env_names_already_in_use():
    assert PROVIDERS["openai-compat"].api_key_env == ENV_API_KEY
    assert PROVIDERS["anthropic"].api_key_env == ENV_ANTHROPIC_API_KEY


def test_default_base_url_is_empty_only_where_there_is_no_trustworthy_default():
    """openai-compat 是一族兼容端点，地址各异，给默认值就是猜。"""
    assert PROVIDERS["openai-compat"].default_base_url == ""
    assert PROVIDERS["anthropic"].default_base_url == DEFAULT_ANTHROPIC_BASE_URL
    assert DEFAULT_ANTHROPIC_BASE_URL.startswith("https://")


def test_spec_is_frozen_so_the_table_cannot_be_mutated_in_place():
    with pytest.raises(Exception):
        PROVIDERS["anthropic"].api_key_env = "MAOS_OTHER"


def test_client_falls_back_to_the_default_base_url_when_given_empty():
    client = AnthropicModelClient(base_url="", api_key=CANARY, model="m")
    assert client.base_url == DEFAULT_ANTHROPIC_BASE_URL
