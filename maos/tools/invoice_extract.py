"""发票图片抽取 —— 一张图片 → 结构化字段 + 逐字段置信度。

## 这一层是工具，不是 Agent 的语义产出（跨轨契约 R3）

抽取走的是 ToolPort 这一层：给一张图片，问网关上的视觉模型「这张纸上印着什么」。
它**不经过** :mod:`maos.model.client` 的 ``ModelClient`` 抽象，也不改它 ——
``select_model_client`` 的签名与语义冻结于 A-12，``complete(system, user, tier)``
只收字符串，发不了图片。给它加一个多模态方法就是把「工具调用」塞进「Agent 语义
产出」的抽象里，两件事的失败口径、审计口径、复核要求都不一样。

所以本模块自己走网关：同一个 base_url、同一把 key（复用
``MAOS_LLM_BASE_URL`` / ``MAOS_LLM_API_KEY``），模型名单列在 ``MAOS_VISION_MODEL``。

## 抽取结果永远是「观察」，不是「事实」（R1 / 铁律 8）

这台机器上可用的视觉模型叫 ``deepseek-v4-flash-vision-exp`` —— 名字里就写着
**实验版**。看错一个小数点等于多付一笔钱，所以本模块的输出**必须**先落成待复核表
（T72），人确认之后才转正式申请表，绝不直接进付款决策。

这条不是「等模型准了就能去掉」的临时限制。发票上印着什么，权威在那张纸和开票方，
不在 MAOS 里；本模块能给的最诚实的东西是「图片上**像是**什么」，判断对不对是人的事。

由此推出本模块的三条姿态，全部体现在 :func:`extract_invoice` 里：

1. **宁可标低不许标高** —— 模型没说 ``high``、字段抽空、值不像个数，一律 ``low``；
2. **认不出就留空，不猜** —— 「壹万贰仟」原样留着不换算，负数量、单价 0 照抽不修正；
3. **降级要留痕且写明后果** —— 没配模型就不打网络，日志说清「字段全空不等于发票
   上没有」（照 ``select_model_client`` 的姿态；铁律 6：只记变量名，值不进日志）。

## key 的四道防线（与 ``maos/model/client.py`` 同口径，逐条对齐）

| 防线 | 防的是什么 |
| :-- | :-- |
| ``_api_key`` 私有属性 + 不含 key 的 ``__repr__`` | key 进 repr / ``vars()`` / 序列化 |
| :func:`_scrub` | key 混进异常文本 |
| 出网异常一律 ``from None`` | traceback 顺出 ``Authorization`` 头 |
| :class:`_SameOriginRedirectHandler` | key 被**发**到别人的服务器上 |

前三条防「key 进日志」，第四条防「key 进别人的机器」，一道都替代不了另一道。
第四条最容易漏：urllib 默认跟随重定向，且把原请求头（含 ``Authorization``）
原样搬到新主机上 —— 只要 ``MAOS_LLM_BASE_URL`` 配错一个地址就够。本模块自建
opener 而不用全局默认 opener，就是为了把这个 handler 装上。

刻意**不 import** ``client.py`` 里那个同名私有类：跨模块借私有名等于把 R3 的
「只读参考」悄悄变成耦合，那边一改这边就断。同姿态、各自持有。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("maos.tools.invoice_extract")

#: 复用文本侧的网关地址与 key —— 同一个网关、同一把 key，只是换个模型。
ENV_BASE_URL = "MAOS_LLM_BASE_URL"
ENV_API_KEY = "MAOS_LLM_API_KEY"
#: 单列的视觉模型名。**不填就是不启用抽取**，不是「用文本模型凑合」。
ENV_VISION_MODEL = "MAOS_VISION_MODEL"

DEFAULT_TIMEOUT = 60.0

#: 超过这个尺寸直接拒绝：几十兆的图 base64 之后还要涨三分之一，
#: 打过去多半是网关 413 或超时，不如在本地就说清楚。
MAX_IMAGE_BYTES = 10 * 1024 * 1024

#: 本轮只收位图。PDF 要装解析依赖（改依赖属于要先问人类的类别），明确不做。
SUFFIX_TO_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}

CONF_HIGH = "high"
CONF_LOW = "low"

#: 抬头字段，列名与跨轨契约 §1.3 逐字对齐（T72 按这些键读）。
FIELD_KEYS = ("发票号", "采购单号", "开票日期", "到期日", "税率")
#: 行项目字段，同上。
LINE_KEYS = ("行号", "SKU", "数量", "单价")
#: 需要按数值规范化的行字段（去 ¥ 与千分位）；``SKU`` 是编码，原样留着。
_NUMERIC_LINE_KEYS = ("行号", "数量", "单价")

#: 规范化只认这一种形状：可选负号 + 数字 + 可选小数。认不出就原样留着并标 low。
_NUMBER_RE = re.compile(r"^-?\d+(?:\.\d+)?$")
#: 货币符号与千分位 —— 去掉之后还是个数才算规范化成功。
_CURRENCY_CHARS = "¥￥$€£, \t "
#: 模型爱把 JSON 包在 ```json 围栏里，剥掉再解析。
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)

SYSTEM_PROMPT = (
    "你是发票识别工具。只读图片上真实印着的内容，逐字转录，"
    "看不清或图片上没有的字段一律留空字符串，绝对不要推测、不要补全、不要换算。"
    "只输出一个 JSON 对象，不要任何解释文字，不要 markdown 围栏。"
)

USER_PROMPT = (
    "把这张发票图片转成 JSON，形状如下（键名逐字照抄，不要改）：\n"
    '{"fields": {"发票号": "", "采购单号": "", "开票日期": "", "到期日": "", "税率": ""},\n'
    ' "lines": [{"行号": "", "SKU": "", "数量": "", "单价": ""}],\n'
    ' "confidence": {"发票号": "high|low", "采购单号": "high|low", "开票日期": "high|low",'
    ' "到期日": "high|low", "税率": "high|low"}}\n'
    "规则：\n"
    "1. 日期原样转录，不要改格式；金额与数量原样转录，不要换算、不要补小数位。\n"
    "2. 金额若是中文大写（如「壹万贰仟」），原样写中文，不要换算成阿拉伯数字。\n"
    "3. 任何一个字段只要不是清清楚楚印在图上，就留空并把该字段的 confidence 标 low。\n"
    "4. 没有行项目就给空数组，不要编一行出来。"
)


@dataclass(frozen=True)
class ExtractedInvoice:
    """一次抽取的全部产出（跨轨契约 §1.3）。内存结构，**不落库**。

    :param source: 图片路径，取证用 —— 复核的人要能回到原图上对。
    :param fields: 抬头字段，键为 :data:`FIELD_KEYS`；抽不到的键存在但为空字符串。
    :param lines: 行项目，每项键为 :data:`LINE_KEYS`。
    :param confidence: 逐字段 ``"high"`` / ``"low"``。抬头字段用字段名作键，
        行字段用 ``lines[i].字段名``（``i`` 从 0 起，与 :attr:`lines` 下标一致）。
    :param raw: 模型原始返回的整段文本，排查用；规范化之前的原文都在这里面。

    ``confidence`` 全 ``high`` **也不代表可以直接用**（R1）：置信度是模型对自己的
    评价，不是对账结论。它的用途是给复核的人排优先级，不是替他签字。
    """

    source: str
    fields: dict = field(default_factory=dict)
    lines: list[dict] = field(default_factory=list)
    confidence: dict = field(default_factory=dict)
    raw: str = ""

    @property
    def low_confidence_keys(self) -> list[str]:
        """所有标了 ``low`` 的键，排好序。复核表按它排优先级，也方便测试断言。"""
        return sorted(k for k, v in self.confidence.items() if v != CONF_HIGH)


class InvoiceExtractError(Exception):
    """输入本身就不能抽 —— 文件不存在、格式不收、图片过大。

    与「抽了但没抽出来」严格分开：后者返回空字段 + 全 ``low``，不抛。
    抛出来的都是**调用方写错了**，静默降级只会让人以为发票上真没有这些字段。
    """


def _scrub(text: str, secret: str) -> str:
    """抹掉可能混进异常文本的 api key（铁律 6：密钥不许出现在任何输出里）。"""
    return text.replace(secret, "***") if secret else text


def _origin(url: str) -> str:
    """取 ``scheme://host:port`` 作为 origin —— 三者全等才算「没换主机」。

    刻意不含 userinfo 和 path：这个字符串会进异常文本，不能夹带凭据。
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

    urllib 默认跟随重定向，且把原请求头（含 ``Authorization``）**原样**搬到新请求
    上：key 不是被打印，是被**发**出去。私有属性、``__repr__``、:func:`_scrub`、
    ``from None`` 四条防的都是「key 出现在日志里」，一条都拦不住这个。

    同 origin 的纯路径跳转（补斜杠、路径规范化）是网关的正常行为，保留。
    同主机的 https -> http 降级也算换 origin：那同样是把 Authorization 明文发上线。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        src, dst = _origin(req.full_url), _origin(newurl)
        if src != dst:
            fp.close()      # 本该由 http_error_302 在本函数返回后关，抛了就轮不到它
            raise RedirectRefused(src, dst, code)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class VisionGateway:
    """OpenAI 兼容协议的多模态客户端（POST ``{base_url}/chat/completions``）。

    只用标准库 urllib —— 这一层不值得为它引第三方依赖（改依赖要先问人）。
    与 ``GatewayModelClient`` 的关系是「同姿态、各自持有」：形状一样，一行不共用，
    因为它发的是图片、失败口径是「留空标 low」而不是「抛给 Agent」。
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
        return f"VisionGateway(base_url={self.base_url!r}, model={self.model!r})"

    def describe(self, data_uri: str) -> str:
        """把一张图发给模型，返回它吐的整段文本。解析是调用方的事。

        网络层的失败一律**抛**（脱敏后的 RuntimeError），不降级成空结果：
        「网关挂了」和「发票上没印这个字段」是两件事，混成一个空表最坑人。
        """
        body = json.dumps({
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": [
                    {"type": "text", "text": USER_PROMPT},
                    {"type": "image_url", "image_url": {"url": data_uri}},
                ]},
            ],
            "temperature": 0,       # 复核要可复现，不要采样随机性
        }, ensure_ascii=False).encode("utf-8")

        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=body, method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
                "X-MAOS-Tool": "invoice_extract",
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
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise RuntimeError(
                "模型网关响应不符合 OpenAI 兼容协议：缺 choices[0].message.content") from None


def _read_image(image_path: str) -> tuple[str, bytes]:
    """校验并读图，返回 (mime, 原始字节)。不合规的输入一律抛，不静默降级。"""
    path = Path(image_path)
    suffix = path.suffix.lower()

    if suffix == ".pdf":
        raise InvoiceExtractError(
            f"本轮不支持 PDF（{path.name}），请先导出为图片（.png / .jpg）再抽取")
    if suffix not in SUFFIX_TO_MIME:
        raise InvoiceExtractError(
            f"不支持的图片格式 {suffix or '（无扩展名）'}："
            f"只收 {'、'.join(sorted(SUFFIX_TO_MIME))}")
    if not path.is_file():
        raise InvoiceExtractError(f"图片不存在或不是普通文件：{image_path}")

    size = path.stat().st_size
    if size > MAX_IMAGE_BYTES:
        raise InvoiceExtractError(
            f"图片 {size / 1024 / 1024:.1f} MB，超过上限 "
            f"{MAX_IMAGE_BYTES / 1024 / 1024:.0f} MB：请先压缩或降分辨率")
    if size == 0:
        raise InvoiceExtractError(f"图片是空文件：{image_path}")

    return SUFFIX_TO_MIME[suffix], path.read_bytes()


def _data_uri(mime: str, blob: bytes) -> str:
    """按 OpenAI 兼容的 ``image_url`` 形状打包图片。"""
    return f"data:{mime};base64,{base64.b64encode(blob).decode('ascii')}"


def _strip_fence(text: str) -> str:
    """剥掉模型爱加的 ```json 围栏。剥不掉就原样返回，交给 json.loads 去红。"""
    match = _FENCE_RE.match(text or "")
    return match.group(1) if match else (text or "")


def _normalize_number(value: object) -> tuple[str, bool]:
    """数值字段规范化，返回 (写进结果的字符串, 这个值是否像个正常的数)。

    第二个返回值是**置信度的输入之一，不是修正的许可**：
    ``¥1,234.50`` 去掉符号与千分位后还是个数，规范化；「壹万贰仟」不是，
    **原样留着**并标 low —— 换算是会计的判断，不是转录工具的判断（R5）。
    负数量、单价 0 这种「抽得清楚但不合理」的值同样照抽标 low，不自作主张修正：
    图上确实这么印着，是不是该改是人的事。原文永远还在 :attr:`ExtractedInvoice.raw`。
    """
    text = "" if value is None else str(value).strip()
    if not text:
        return "", False

    cleaned = text.strip(_CURRENCY_CHARS).replace(",", "").replace(" ", "")
    if not _NUMBER_RE.match(cleaned):
        return text, False              # 中文大写、带单位、认不出的写法：原样留着
    return cleaned, float(cleaned) > 0  # <= 0 抽得出来但不合理，标 low 让人看一眼


def _empty_result(source: str, raw: str = "") -> ExtractedInvoice:
    """一份「没抽到」的结果：字段键齐全但值全空，置信度全 low。

    键留着而不是给个空 dict —— 下游按键取值，缺键会变成 KeyError 或
    「这个字段不存在」，而真相是「这个字段没抽出来」。
    """
    return ExtractedInvoice(
        source=source,
        fields={key: "" for key in FIELD_KEYS},
        lines=[],
        confidence={key: CONF_LOW for key in FIELD_KEYS},
        raw=raw,
    )


def _parse(source: str, raw: str) -> ExtractedInvoice:
    """把模型返回的文本解析成 :class:`ExtractedInvoice`。**任何畸形都不抛**。

    模型回的不是 JSON、少了键、``lines`` 不是数组 —— 一律退化成「这部分没抽到」，
    因为这条路上的失败全都是「没看清」的同义词，而没看清的正确表达是空字段 + low，
    不是异常。真正该抛的（配置错、网络断、图片不合规）在别的地方抛。
    """
    try:
        payload = json.loads(_strip_fence(raw))
    except (json.JSONDecodeError, TypeError):
        log.warning("视觉模型返回的不是 JSON（%d 字符），本次字段全部留空并标 low —— "
                    "这不表示发票上没有这些字段，原文见 raw", len(raw or ""))
        return _empty_result(source, raw)
    if not isinstance(payload, dict):
        log.warning("视觉模型返回的 JSON 顶层不是对象（%s），本次字段全部留空并标 low",
                    type(payload).__name__)
        return _empty_result(source, raw)

    raw_fields = payload.get("fields")
    raw_fields = raw_fields if isinstance(raw_fields, dict) else {}
    raw_conf = payload.get("confidence")
    raw_conf = raw_conf if isinstance(raw_conf, dict) else {}

    fields: dict = {}
    confidence: dict = {}
    for key in FIELD_KEYS:
        value = raw_fields.get(key)
        text = "" if value is None else str(value).strip()
        if key == "税率":
            text, plausible = _normalize_number(text)
        else:
            plausible = bool(text)
        fields[key] = text
        # 模型自报的 high 只是**必要条件**：抽空了、值不像个数，一律压回 low。
        # 宁可标低不许标高 —— 标高的代价是人略过它，标低的代价只是多看一眼。
        claimed = str(raw_conf.get(key, "")).strip().lower()
        confidence[key] = CONF_HIGH if (claimed == CONF_HIGH and text and plausible) else CONF_LOW

    raw_lines = payload.get("lines")
    lines: list[dict] = []
    if isinstance(raw_lines, list):
        for index, item in enumerate(raw_lines):
            if not isinstance(item, dict):
                log.warning("视觉模型返回的 lines[%d] 不是对象，整行按未抽到处理", index)
                item = {}
            row: dict = {}
            for key in LINE_KEYS:
                value = item.get(key)
                text = "" if value is None else str(value).strip()
                if key in _NUMERIC_LINE_KEYS:
                    text, plausible = _normalize_number(text)
                else:
                    plausible = bool(text)
                row[key] = text
                confidence[f"lines[{index}].{key}"] = (
                    CONF_HIGH if (text and plausible) else CONF_LOW)
            lines.append(row)
    elif raw_lines is not None:
        log.warning("视觉模型返回的 lines 不是数组（%s），按没有行项目处理",
                    type(raw_lines).__name__)

    return ExtractedInvoice(source=source, fields=fields, lines=lines,
                            confidence=confidence, raw=raw)


def extract_invoice(image_path: str, *, timeout: float = DEFAULT_TIMEOUT) -> ExtractedInvoice:
    """一张发票图片 → 结构化字段 + 逐字段置信度。认不出的字段留空并标 low。

    **返回值必须先进待复核表给人看过再用**（R1）—— 本函数不知道也不判断这些字段
    对不对，它只报告「图片上像是什么」。

    三条出路，泾渭分明：

    · 图片本身不能抽（PDF、格式不收、过大、不存在）→ 抛 :class:`InvoiceExtractError`；
    · 没配 ``MAOS_VISION_MODEL`` / 没配网关 → **不打网络**，返回空结果 + 全 low，
      日志写明后果；
    · 抽了但没看清（非 JSON、缺字段、值认不出）→ 空字段 + low，**不抛**。

    网络层的故障（不可达、HTTP 4xx/5xx、跨 origin 跳转）照抛，不吞成空结果：
    那不是「没看清」，是这次根本没问成，静默降级会让人把它当成发票上没有。
    """
    mime, blob = _read_image(image_path)        # 先验输入 —— 配置对不对都轮不到它

    env = {
        ENV_BASE_URL: (os.environ.get(ENV_BASE_URL) or "").strip(),
        ENV_API_KEY: (os.environ.get(ENV_API_KEY) or "").strip(),
        ENV_VISION_MODEL: (os.environ.get(ENV_VISION_MODEL) or "").strip(),
    }
    missing = [name for name, value in env.items() if not value]
    if missing:
        # 照 select_model_client 的姿态：不抛、不猜、不打网络，只记**变量名**
        # （铁律 6：值绝不进日志），且正文必须写明后果 —— 一张全空的表看起来
        # 和「这张发票上什么都没有」一模一样，不写清楚就会被当成抽取结论。
        log.warning("未配置 %s，本次**没有做抽取**：不发起任何网络请求，返回的字段"
                    "全部为空、置信度全部为 low。这**不表示**发票上没有这些字段，"
                    "只表示没人去看过这张图；请配好环境变量后重跑，或全部人工录入",
                    "/".join(missing))
        return _empty_result(image_path)

    gateway = VisionGateway(base_url=env[ENV_BASE_URL], api_key=env[ENV_API_KEY],
                            model=env[ENV_VISION_MODEL], timeout=timeout)
    log.info("发票抽取：source=%s model=%s（结果必须人工复核，不得直接进付款决策）",
             image_path, env[ENV_VISION_MODEL])
    raw = gateway.describe(_data_uri(mime, blob))
    result = _parse(image_path, raw)
    if result.low_confidence_keys:
        log.info("发票抽取完成，%d 个字段置信度 low，需人工逐个核对：%s",
                 len(result.low_confidence_keys), result.low_confidence_keys[:8])
    return result
