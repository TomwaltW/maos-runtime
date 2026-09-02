"""T71 —— 发票图片抽取工具 :mod:`maos.tools.invoice_extract` 的回归。

**全部离线**。这台机器配着可用的 DeepSeek key，一条写漏的用例就会当场打真网络
并烧 token，所以本文件有两道闸：

1. :func:`_no_real_network` 是 autouse 的，把 ``socket.socket`` 整个封掉 ——
   哪条用例忘了喂假响应，红的是「测试自己想上网」，而不是账单；
2. 每条要出网的用例都显式装 :class:`_FakeOpener`，并断言请求体的形状。

用例的分档照跨轨契约 R5 与派单 §5.3 的那张表来：**输入不合规就抛，没看清就留空**，
两者不许互相冒充。全文件反复钉同一件事 —— 这个工具的职责是「如实报告图片上像是
什么」，不是「给出一个能用的答案」。
"""

from __future__ import annotations

import base64
import json
import logging
import socket
import urllib.error
import urllib.request

import pytest

from maos.tools.invoice_extract import (
    CONF_HIGH,
    CONF_LOW,
    ENV_API_KEY,
    ENV_BASE_URL,
    ENV_VISION_MODEL,
    FIELD_KEYS,
    LINE_KEYS,
    MAX_IMAGE_BYTES,
    ExtractedInvoice,
    InvoiceExtractError,
    RedirectRefused,
    VisionGateway,
    _SameOriginRedirectHandler,
    extract_invoice,
)

# 一眼能认出来的哨兵。真出现在任何输出里，grep 得到。
CANARY = "sk-VISION-LEAK-CANARY-0123456789"
BASE_URL = "https://gw.example/v1"
MODEL = "deepseek-v4-flash-vision-exp"

# 1x1 的 PNG，够真到能被当成图片读，又不用往仓库里塞二进制文件。
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """任何一条用例只要真的去连网络，当场红。

    比「记得喂假响应」可靠：漏喂的用例不会安静地走出去打网关，而是在这里炸。
    """
    def _boom(*args, **kwargs):
        raise AssertionError("测试试图打开真网络连接 —— 用例必须喂假响应")

    monkeypatch.setattr(socket, "socket", _boom)


@pytest.fixture
def image(tmp_path):
    """一张能通过输入校验的最小 PNG。"""
    path = tmp_path / "invoice.png"
    path.write_bytes(PNG_1PX)
    return path


@pytest.fixture
def vision_env(monkeypatch):
    """把三个环境变量配齐 —— 只有这样才会走到出网分支。"""
    monkeypatch.setenv(ENV_BASE_URL, BASE_URL)
    monkeypatch.setenv(ENV_API_KEY, CANARY)
    monkeypatch.setenv(ENV_VISION_MODEL, MODEL)


class _FakeResponse:
    def __init__(self, payload) -> None:
        self._body = payload if isinstance(payload, bytes) else json.dumps(
            payload, ensure_ascii=False).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


class _FakeOpener:
    """替掉 ``build_opener()`` 的产物：记下请求，回一个预设响应或抛一个预设异常。"""

    def __init__(self, payload=None, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error
        self.requests: list[urllib.request.Request] = []
        self.timeouts: list[float] = []

    def open(self, req, timeout=None):
        self.requests.append(req)
        self.timeouts.append(timeout)
        if self.error is not None:
            raise self.error
        return _FakeResponse(self.payload)


def _install(monkeypatch, opener: _FakeOpener) -> _FakeOpener:
    """让模块里的 ``build_opener()`` 交出我们的假 opener。"""
    monkeypatch.setattr(urllib.request, "build_opener", lambda *a, **kw: opener)
    return opener


def _completion(content: str) -> dict:
    """一份 OpenAI 兼容的成功响应。"""
    return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "model": MODEL, "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


def _model_json(fields: dict | None = None, lines: list | None = None,
                confidence: dict | None = None) -> str:
    return json.dumps({
        "fields": fields if fields is not None else {},
        "lines": lines if lines is not None else [],
        "confidence": confidence if confidence is not None else {},
    }, ensure_ascii=False)


# --------------------------------------------------------------------------
# 一、没配模型：不打网络，不抛，不假装抽到了
# --------------------------------------------------------------------------

def test_missing_vision_model_makes_no_network_call(monkeypatch, image, caplog):
    """``MAOS_VISION_MODEL`` 没配就是**不启用抽取** —— 一行网络都不许走。

    这条用两道断言钉住「没打网络」：``build_opener`` 一次都不许被调（连 opener
    都没造出来），加上 autouse 的 socket 闸。只断言返回值是空表不够 ——
    打了网络再把结果丢掉，返回值一样是空表。
    """
    monkeypatch.setenv(ENV_BASE_URL, BASE_URL)
    monkeypatch.setenv(ENV_API_KEY, CANARY)
    monkeypatch.delenv(ENV_VISION_MODEL, raising=False)

    def _never(*args, **kwargs):
        raise AssertionError("没配 MAOS_VISION_MODEL 却造了 opener —— 说明要出网了")

    monkeypatch.setattr(urllib.request, "build_opener", _never)

    with caplog.at_level(logging.WARNING, logger="maos.tools.invoice_extract"):
        result = extract_invoice(str(image))

    assert result.fields == {key: "" for key in FIELD_KEYS}
    assert result.lines == []
    assert set(result.confidence.values()) == {CONF_LOW}
    assert result.source == str(image)


@pytest.mark.parametrize("missing", [ENV_BASE_URL, ENV_API_KEY, ENV_VISION_MODEL])
def test_any_missing_env_degrades_without_network(monkeypatch, image, missing, caplog):
    """三个变量缺任何一个都降级 —— 缺 key 时尤其不许「反正有地址就试一下」。"""
    monkeypatch.setenv(ENV_BASE_URL, BASE_URL)
    monkeypatch.setenv(ENV_API_KEY, CANARY)
    monkeypatch.setenv(ENV_VISION_MODEL, MODEL)
    monkeypatch.delenv(missing, raising=False)
    monkeypatch.setattr(urllib.request, "build_opener",
                        lambda *a, **kw: pytest.fail(f"缺 {missing} 却要出网"))

    with caplog.at_level(logging.WARNING, logger="maos.tools.invoice_extract"):
        result = extract_invoice(str(image))

    assert result.low_confidence_keys == sorted(FIELD_KEYS)
    assert missing in caplog.text, "日志要说清缺的是哪个变量，不然没法自助修"


def test_degrade_log_states_consequence_and_never_leaks_value(monkeypatch, image, caplog):
    """降级日志必须写明**后果**，且只记变量名 —— 值一个字都不许进日志（铁律 6）。

    「降级了」这四个字读日志的人还要自己推后果。一张全空的表和「这张发票上什么都
    没有」长得一模一样，不写清楚就会被当成抽取结论 —— 那正是最贵的误读。
    """
    monkeypatch.setenv(ENV_BASE_URL, BASE_URL)
    monkeypatch.setenv(ENV_API_KEY, CANARY)
    monkeypatch.delenv(ENV_VISION_MODEL, raising=False)

    with caplog.at_level(logging.WARNING, logger="maos.tools.invoice_extract"):
        extract_invoice(str(image))

    text = caplog.text
    assert ENV_VISION_MODEL in text
    assert CANARY not in text, "环境变量的**值**漏进日志了"
    assert BASE_URL not in text, "只记变量名，地址也不必进日志"
    assert "不表示" in text and "为空" in text, f"日志没写明后果：{text}"


# --------------------------------------------------------------------------
# 二、正常路径：字段、行项目、逐字段置信度
# --------------------------------------------------------------------------

def test_normal_json_maps_fields_lines_and_confidence(monkeypatch, image, vision_env):
    opener = _install(monkeypatch, _FakeOpener(_completion(_model_json(
        fields={"发票号": "INV-2026-0001", "采购单号": "PO-2026-0001",
                "开票日期": "2026-07-25", "到期日": "2026-08-24", "税率": "0.13"},
        lines=[{"行号": "1", "SKU": "SKU-A", "数量": "100", "单价": "12.50"}],
        confidence={key: "high" for key in FIELD_KEYS},
    ))))

    result = extract_invoice(str(image))

    assert isinstance(result, ExtractedInvoice)
    assert result.fields["发票号"] == "INV-2026-0001"
    assert result.fields["税率"] == "0.13"
    assert result.lines == [{"行号": "1", "SKU": "SKU-A", "数量": "100", "单价": "12.50"}]
    assert all(result.confidence[key] == CONF_HIGH for key in FIELD_KEYS)
    assert result.confidence["lines[0].单价"] == CONF_HIGH
    assert result.low_confidence_keys == []
    assert result.source == str(image)
    assert opener.requests, "正常路径就是要出网的"


def test_request_shape_is_openai_multimodal(monkeypatch, image, vision_env):
    """请求体是 OpenAI 兼容的多模态形状，图片按 data URI 塞在 ``image_url`` 里。"""
    opener = _install(monkeypatch, _FakeOpener(_completion(_model_json())))

    extract_invoice(str(image), timeout=12.5)

    req = opener.requests[0]
    assert req.full_url == f"{BASE_URL}/chat/completions"
    assert req.get_method() == "POST"
    assert opener.timeouts == [12.5], "timeout 要透传到 opener，不然挂死没人管"

    body = json.loads(req.data.decode("utf-8"))
    assert body["model"] == MODEL
    assert body["temperature"] == 0, "复核要可复现，不要采样随机性"
    content = body["messages"][-1]["content"]
    assert content[0]["type"] == "text"
    image_part = content[1]
    assert image_part["type"] == "image_url"
    uri = image_part["image_url"]["url"]
    assert uri.startswith("data:image/png;base64,")
    assert base64.b64decode(uri.split(",", 1)[1]) == PNG_1PX, "发出去的得是原图字节"


def test_jpeg_uses_jpeg_mime(monkeypatch, tmp_path, vision_env):
    """``.jpg`` / ``.jpeg`` 都走 ``image/jpeg`` —— mime 写错网关那边就认不出图。"""
    path = tmp_path / "invoice.JPEG"          # 顺带钉住后缀大小写不敏感
    path.write_bytes(PNG_1PX)
    opener = _install(monkeypatch, _FakeOpener(_completion(_model_json())))

    extract_invoice(str(path))

    body = json.loads(opener.requests[0].data.decode("utf-8"))
    uri = body["messages"][-1]["content"][1]["image_url"]["url"]
    assert uri.startswith("data:image/jpeg;base64,")


def test_fenced_json_is_unwrapped(monkeypatch, image, vision_env):
    """模型爱把 JSON 包进 ```json 围栏。剥得掉就不该白白丢掉一整次抽取。"""
    payload = _model_json(fields={"发票号": "INV-9"}, confidence={"发票号": "high"})
    _install(monkeypatch, _FakeOpener(_completion(f"```json\n{payload}\n```")))

    result = extract_invoice(str(image))

    assert result.fields["发票号"] == "INV-9"
    assert result.confidence["发票号"] == CONF_HIGH


# --------------------------------------------------------------------------
# 三、看不清的各档：一律留空标 low，不抛、不猜
# --------------------------------------------------------------------------

def test_non_json_response_degrades_without_raising(monkeypatch, image, vision_env, caplog):
    """模型回了一段散文 —— 字段全空 + 全 low，**不抛**，原文留在 raw 里。"""
    prose = "这张图片看起来像一张增值税专用发票，但是我看不清具体数字。"
    _install(monkeypatch, _FakeOpener(_completion(prose)))

    with caplog.at_level(logging.WARNING, logger="maos.tools.invoice_extract"):
        result = extract_invoice(str(image))

    assert result.fields == {key: "" for key in FIELD_KEYS}
    assert result.lines == []
    assert result.low_confidence_keys == sorted(FIELD_KEYS)
    assert result.raw == prose, "原始返回要留着 —— 排查时唯一能回溯的东西"
    assert "不表示发票上没有" in caplog.text


def test_json_but_not_an_object_degrades(monkeypatch, image, vision_env):
    """合法 JSON 但顶层是数组 —— 同样按没抽到处理，不去猜它想表达什么。"""
    _install(monkeypatch, _FakeOpener(_completion('["INV-1", "INV-2"]')))

    result = extract_invoice(str(image))

    assert result.fields == {key: "" for key in FIELD_KEYS}
    assert result.low_confidence_keys == sorted(FIELD_KEYS)


def test_missing_fields_keep_keys_and_go_low(monkeypatch, image, vision_env):
    """模型只给了两个字段 —— 缺的键**留着**但为空并标 low。

    缺键会让下游把「没抽出来」读成「这个字段不存在」，那是两件事。
    """
    _install(monkeypatch, _FakeOpener(_completion(_model_json(
        fields={"发票号": "INV-1", "开票日期": "2026-07-25"},
        confidence={"发票号": "high", "开票日期": "high"}))))

    result = extract_invoice(str(image))

    assert set(result.fields) == set(FIELD_KEYS)
    assert result.fields["采购单号"] == "" and result.fields["税率"] == ""
    assert result.confidence["采购单号"] == CONF_LOW
    assert result.confidence["发票号"] == CONF_HIGH


def test_claimed_high_on_empty_value_is_forced_low(monkeypatch, image, vision_env):
    """模型自报 high 但字段是空的 —— 压回 low。宁可标低不许标高。

    模型的自评是**必要条件**不是充分条件：标高的代价是人略过它（漏掉一笔错款），
    标低的代价只是多看一眼。
    """
    _install(monkeypatch, _FakeOpener(_completion(_model_json(
        fields={"发票号": "   ", "采购单号": "PO-1"},
        confidence={key: "high" for key in FIELD_KEYS}))))

    result = extract_invoice(str(image))

    assert result.fields["发票号"] == ""
    assert result.confidence["发票号"] == CONF_LOW
    assert result.confidence["到期日"] == CONF_LOW, "模型没给的字段不许因为自报 high 就高"
    assert result.confidence["采购单号"] == CONF_HIGH


def test_lines_not_a_list_and_row_not_a_dict(monkeypatch, image, vision_env, caplog):
    """``lines`` 畸形的两档：整段不是数组 / 某一行不是对象。都不抛。"""
    _install(monkeypatch, _FakeOpener(_completion(_model_json(lines="一行 SKU-A 100 个"))))
    with caplog.at_level(logging.WARNING, logger="maos.tools.invoice_extract"):
        assert extract_invoice(str(image)).lines == []

    _install(monkeypatch, _FakeOpener(_completion(_model_json(
        lines=[{"行号": "1", "SKU": "SKU-A", "数量": "10", "单价": "1.00"}, "SKU-B 50 个"]))))
    result = extract_invoice(str(image))

    assert len(result.lines) == 2, "坏的那行也要占位 —— 少一行比留个空行更难发现"
    assert result.lines[1] == {key: "" for key in LINE_KEYS}
    assert result.confidence["lines[1].SKU"] == CONF_LOW
    assert result.confidence["lines[0].SKU"] == CONF_HIGH


# --------------------------------------------------------------------------
# 四、不猜的姿态：大写金额、货币符号、不合理的数
# --------------------------------------------------------------------------

def test_chinese_uppercase_amount_kept_verbatim(monkeypatch, image, vision_env):
    """「壹万贰仟」原样留着 + low，**不换算**。

    换算是会计的判断，不是转录工具的判断（R5）。工具自作主张换算一次，人就少看
    一眼；换错一次，多付的是真钱。
    """
    _install(monkeypatch, _FakeOpener(_completion(_model_json(
        lines=[{"行号": "壹", "SKU": "SKU-A", "数量": "壹佰", "单价": "壹万贰仟"}]))))

    result = extract_invoice(str(image))

    row = result.lines[0]
    assert row["单价"] == "壹万贰仟" and row["数量"] == "壹佰" and row["行号"] == "壹"
    assert result.confidence["lines[0].单价"] == CONF_LOW
    assert result.confidence["lines[0].数量"] == CONF_LOW


def test_currency_symbol_and_thousands_separator_normalized(monkeypatch, image, vision_env):
    """``¥1,234.50`` 规范化成 ``1234.50``，但原文仍在 :attr:`raw` 里。

    这一档和上一档的区别是「去掉符号之后还是个数」—— 那是格式，不是判断。
    """
    _install(monkeypatch, _FakeOpener(_completion(_model_json(
        lines=[{"行号": "1", "SKU": "SKU-A", "数量": "1,200", "单价": "¥1,234.50"}]))))

    result = extract_invoice(str(image))

    assert result.lines[0]["单价"] == "1234.50"
    assert result.lines[0]["数量"] == "1200"
    assert result.confidence["lines[0].单价"] == CONF_HIGH
    assert "¥1,234.50" in result.raw, "规范化了也要留得下原文，不然没法回查"


def test_implausible_numbers_are_kept_not_fixed(monkeypatch, image, vision_env):
    """负数量、单价 0 —— **照抽**，标 low，不自作主张修正。

    图上确实这么印着。是不是该改是人的事；工具替它改一笔，错误就再也看不见了。
    """
    _install(monkeypatch, _FakeOpener(_completion(_model_json(
        lines=[{"行号": "1", "SKU": "SKU-A", "数量": "-5", "单价": "0"}]))))

    result = extract_invoice(str(image))

    assert result.lines[0]["数量"] == "-5" and result.lines[0]["单价"] == "0"
    assert result.confidence["lines[0].数量"] == CONF_LOW
    assert result.confidence["lines[0].单价"] == CONF_LOW


def test_tax_rate_with_percent_sign_stays_verbatim(monkeypatch, image, vision_env):
    """``13%`` 不是 ``0.13`` —— 两种口径差 100 倍，工具不做这个换算。"""
    _install(monkeypatch, _FakeOpener(_completion(_model_json(
        fields={"税率": "13%"}, confidence={"税率": "high"}))))

    result = extract_invoice(str(image))

    assert result.fields["税率"] == "13%"
    assert result.confidence["税率"] == CONF_LOW


# --------------------------------------------------------------------------
# 五、输入不合规：抛，不静默降级
# --------------------------------------------------------------------------

def test_pdf_is_refused_with_actionable_message(tmp_path, vision_env, monkeypatch):
    """PDF 本轮不做（要装依赖）。明确报错并给出下一步，不要降级成空表。"""
    path = tmp_path / "invoice.pdf"
    path.write_bytes(b"%PDF-1.7\n")
    monkeypatch.setattr(urllib.request, "build_opener",
                        lambda *a, **kw: pytest.fail("输入不合规却出网了"))

    with pytest.raises(InvoiceExtractError) as exc:
        extract_invoice(str(path))

    assert "PDF" in str(exc.value) and "导出为图片" in str(exc.value)


@pytest.mark.parametrize("name", ["invoice.tiff", "invoice.bmp", "invoice"])
def test_unsupported_suffix_refused(tmp_path, name, vision_env):
    path = tmp_path / name
    path.write_bytes(PNG_1PX)
    with pytest.raises(InvoiceExtractError):
        extract_invoice(str(path))


def test_missing_file_refused(tmp_path, vision_env):
    with pytest.raises(InvoiceExtractError) as exc:
        extract_invoice(str(tmp_path / "nope.png"))
    assert "不存在" in str(exc.value)


def test_empty_file_refused(tmp_path, vision_env):
    """0 字节的图 —— 发过去也是白发一次，在本地就说清楚。"""
    path = tmp_path / "empty.png"
    path.write_bytes(b"")
    with pytest.raises(InvoiceExtractError) as exc:
        extract_invoice(str(path))
    assert "空文件" in str(exc.value)


def test_oversized_image_refused_before_any_network(tmp_path, vision_env, monkeypatch):
    """超过 10 MB 直接拒 —— base64 之后还要涨三分之一，别把它塞进请求。"""
    path = tmp_path / "huge.png"
    with open(path, "wb") as handle:            # 稀疏文件：占大小不占磁盘
        handle.truncate(MAX_IMAGE_BYTES + 1)
    monkeypatch.setattr(urllib.request, "build_opener",
                        lambda *a, **kw: pytest.fail("超大图片却出网了"))

    with pytest.raises(InvoiceExtractError) as exc:
        extract_invoice(str(path))

    assert "MB" in str(exc.value)


# --------------------------------------------------------------------------
# 六、第四道防线：跨 origin 重定向
# --------------------------------------------------------------------------

class _ClosableFp:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize("newurl", [
    "https://evil.example/v1/chat/completions",      # 换主机
    "https://gw.example:8443/v1/chat/completions",   # 换端口
    "http://gw.example/v1/chat/completions",         # https -> http 降级
])
def test_cross_origin_redirect_is_refused(newurl):
    """换了 scheme / 主机 / 端口一律拒 —— 拦的是 key 被**发**到别人服务器上。

    urllib 默认跟随重定向，且把 ``Authorization`` 头原样搬到新主机。私有属性、
    ``__repr__``、``_scrub``、``from None`` 四条防的都是「key 进日志」，
    一条都拦不住这个。
    """
    handler = _SameOriginRedirectHandler()
    req = urllib.request.Request(f"{BASE_URL}/chat/completions")
    fp = _ClosableFp()

    with pytest.raises(RedirectRefused) as exc:
        handler.redirect_request(req, fp, 302, "Found", {}, newurl)

    assert exc.value.origin_from.startswith("https://gw.example")
    assert fp.closed, "抛之前要把响应体关掉，不然连接泄漏"


def test_same_origin_path_redirect_allowed():
    """同 origin 的纯路径跳转是网关的正常行为（补斜杠、路径规范化），保留。"""
    import email.message

    handler = _SameOriginRedirectHandler()
    handler.parent = urllib.request.OpenerDirector()
    req = urllib.request.Request(f"{BASE_URL}/chat/completions")
    headers = email.message.Message()

    new = handler.redirect_request(req, _ClosableFp(), 302, "Found", headers,
                                   f"{BASE_URL}/chat/completions/")

    assert new is not None and new.full_url == f"{BASE_URL}/chat/completions/"


def test_gateway_actually_installs_the_handler():
    """上面那几条钉的是 handler 本身，这条钉的是**它被装上了**。

    少了这条，把 ``build_opener(_SameOriginRedirectHandler())`` 改回
    ``build_opener()`` —— 防线整个没了 —— 全套测试照样绿：handler 的单测直接
    构造类，``extract_invoice`` 那条用的是假 opener，两边都碰不到真实装配。
    顺带断言默认的 ``HTTPRedirectHandler`` 没同时在链上：它在，就还是会跟过去。
    """
    client = VisionGateway(base_url=BASE_URL, api_key=CANARY, model=MODEL)
    handlers = client._opener.handlers

    assert any(isinstance(h, _SameOriginRedirectHandler) for h in handlers), \
        f"opener 上没装同 origin 重定向 handler：{[type(h).__name__ for h in handlers]}"
    assert not any(type(h) is urllib.request.HTTPRedirectHandler for h in handlers), \
        "默认的 HTTPRedirectHandler 还在链上，跨 origin 跳转仍会被跟随"


def test_network_sentinel_actually_bites():
    """自证 :func:`_no_real_network` 有牙齿 —— 哨兵本身坏了会静默放行真网络。

    这台机器配着可用的 key，「测试没打网络」不能靠自觉，得有一条会红的断言。
    """
    with pytest.raises(AssertionError, match="真网络"):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)


def test_gateway_turns_redirect_into_scrubbed_error(monkeypatch, image, vision_env):
    """真跑到 ``extract_invoice`` 这一层，跨 origin 跳转变成**看得懂的报错**。

    而且是抛不是降级：跳转被拒说明这次根本没问成模型，静默返回空表会被读成
    「发票上没有这些字段」。
    """
    _install(monkeypatch, _FakeOpener(
        error=RedirectRefused("https://gw.example:443", "https://evil.example:443", 302)))

    with pytest.raises(RuntimeError) as exc:
        extract_invoice(str(image))

    text = str(exc.value)
    assert "evil.example" in text and ENV_BASE_URL in text
    assert CANARY not in text


# --------------------------------------------------------------------------
# 七、前三道防线：key 不进 repr、不进异常、不进公开属性
# --------------------------------------------------------------------------

def test_key_is_private_and_absent_from_repr():
    client = VisionGateway(base_url=BASE_URL, api_key=CANARY, model=MODEL)

    assert not hasattr(client, "api_key"), "key 应挂在私有属性 _api_key 上"
    assert client._api_key == CANARY
    public = [name for name in vars(client) if not name.startswith("_")]
    assert CANARY not in str([getattr(client, n) for n in public]), \
        f"key 从公开属性 {public} 里漏出来了"
    assert CANARY not in repr(client) and CANARY not in str(client)
    assert repr(client).startswith("VisionGateway("), "repr 退化成默认 object repr"
    assert "object at 0x" not in repr(client)


@pytest.mark.parametrize("error", [
    pytest.param(urllib.error.HTTPError(
        BASE_URL, 401, f"Unauthorized: bad key {CANARY}", {},
        __import__("io").BytesIO(f'{{"error":"invalid api key {CANARY}"}}'.encode())),
        id="http-error"),
    pytest.param(urllib.error.URLError(f"connection refused while sending {CANARY}"),
                 id="url-error"),
    pytest.param(TimeoutError(f"timed out with Authorization: Bearer {CANARY}"),
                 id="timeout"),
])
def test_key_never_appears_in_error_text(monkeypatch, image, vision_env, error):
    """出网失败的三档异常文本里都不许出现 key —— 它会一路进日志和 traceback。

    还要断言 ``__cause__`` 是 None：``from None`` 掐断的链，正是底层 traceback
    把 ``Authorization`` 头顺出来的那条路。
    """
    _install(monkeypatch, _FakeOpener(error=error))

    with pytest.raises(RuntimeError) as exc:
        extract_invoice(str(image))

    assert CANARY not in str(exc.value)
    assert CANARY not in repr(exc.value)
    assert exc.value.__cause__ is None, "异常链没掐断，traceback 会带出 Authorization"


def test_malformed_gateway_envelope_raises_without_key(monkeypatch, image, vision_env):
    """响应不符合 OpenAI 协议 —— 抛，且文本里没有 key。

    这一档和「模型回了散文」不同：散文是模型没看清（降级），缺 ``choices`` 是
    网关侧不对（抛）。
    """
    _install(monkeypatch, _FakeOpener({"unexpected": "shape"}))

    with pytest.raises(RuntimeError) as exc:
        extract_invoice(str(image))

    assert "OpenAI" in str(exc.value)
    assert CANARY not in str(exc.value)


def test_authorization_header_is_sent_but_not_stored_anywhere_public(monkeypatch,
                                                                     image, vision_env):
    """key 该出现的地方只有一处：发出去的 ``Authorization`` 头。

    结果对象是要交给 T72 落成待复核表的，它身上沾一点 key 就会写进文件。
    """
    opener = _install(monkeypatch, _FakeOpener(_completion(_model_json())))

    result = extract_invoice(str(image))

    assert opener.requests[0].get_header("Authorization") == f"Bearer {CANARY}"
    assert CANARY not in repr(result) and CANARY not in str(result.raw)


# --------------------------------------------------------------------------
# 八、契约形状：键名与结构钉死（跨轨契约 §1.3，T72 按这个读）
# --------------------------------------------------------------------------

def test_contract_keys_match_cross_track_contract():
    """键名一改，T72 的待复核表当场对不上 —— 所以逐字钉住。"""
    assert FIELD_KEYS == ("发票号", "采购单号", "开票日期", "到期日", "税率")
    assert LINE_KEYS == ("行号", "SKU", "数量", "单价")


def test_result_is_frozen_dataclass():
    """结果是**观察记录**，不是可编辑的草稿：要改就重新抽，别就地改一笔金额。"""
    result = ExtractedInvoice(source="x.png")
    with pytest.raises(Exception):
        result.source = "y.png"     # type: ignore[misc]
