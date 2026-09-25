"""理解层（p13 契约 §1.4 T173、§2 第 0 / 4 步）：一句客户原文 → 语种、槽位、意图。

ADP 课程 3.3 单工作流的「参数提取 + 意图识别」两个节点的 MAOS 版本。前台（T174）在触发词之后
调 ``cs.understand``（本模块的 skill 外壳），拿到槽位并进 ``cs_slot``、拿意图决定走查单还是走话术，
检索时把意图当 ``intent_hint`` 传给 ``scripts.match_scripts``。

## 确定性优先，模型兜底

意图按这个顺序判，前一步判出来后一步就不看（契约 §1.4 与派单）：

1. **触发词**（:func:`maos.domain.cs.triggers.detect`）：隐私 / 赔偿 / 情绪 / 投诉 / 要人工；
2. **本轮诉求**：要退款 / 退货 / 换货 → 退换货；查进度按查的是钱、是售后单还是包裹分到三个意图；
3. **意图示例**（:data:`INTENT_EXAMPLES`，ADP 课程 4.4「相近说法靠示例强制干预」）：一张
   「说法 → 意图」的数据表。原文与某条示例**在实词上**足够像（:func:`example_similarity`）就按它、
   压过关键词词表；只在**句式上**像（「……什么时候能到」）的，只在关键词词表一票都没有时补位
   （复核 L2-3 / L3-1：只看整句二元组，虚词骨架会把「退款什么时候能到」拉成物流）；
4. **关键词词表**（:data:`INTENT_KEYWORDS`）数票；
5. 以上都判不出 → ``unknown``。话术检索的意图**不在这里做**（检索落 KbRetrieved，是前台的事）；
6. 判不出、且注入的是**真模型**（非 None、非 ``ScriptedModelClient``）才调一次模型，输出夹到
   ``types.INTENTS``（不在里面就是 unknown）。调了就记账（``record_model_usage`` /
   ``record_model_failure``，trace_id 空串、task_id None、plan_id 照传）；模型出错不抛，
   记一行失败、回规则的结果 —— 模型坏了不该让客户被转人工（前台「永不抛」）。

Scripted / None 下一次模型都不调，零 ``model_usage`` 行：测试、证据、演示恒为确定性。

## 槽位（:func:`extract_slots`）

五个槽位（``ports.SLOT_KEYS``），全部确定性、**只从词表与正则来**，取不到就不给：

* ``order_no``：常见平台单号形态 —— 纯数字 ≥ 8 位；字母数字混排带连字符（拼多多式
  「200924-1234567890」、自编号「SO-2026-000123」）；短一些的「字母 + 数字」（「A1001」）与
  「# + 数字」只在句子里有「订单 / 单号 / 那单 / order」这类字眼、或整句就是这个号（可带
  「谢谢」这类客套）时才认（「iPhone15」「RTX4090」是型号不是单号）。**不认**：手机号（去掉
  连字符与 86 / +86 国家码后是 11 位手机号形态的，「138-1234-5678」「+8613812345678」一样不认；
  隐私，不能当单号存进槽位表）、日期（2026-09-24）、400 / 800 热线（带不带连字符）与座机号。
  取原文里第一个，NFKC 后字母转大写。
* ``request``：诉求闭集 refund / return / exchange / track / other。**只在客户要办 / 要查自己那一单
  时给**：问规则的（怎么退、多久到、什么时候到账、能不能退）不给 —— 否则前台会为一句政策问题去要单号。
  问进度（到没到 / 发了没）是 track，哪怕句子里有「退款」二字；但**明说要办**的分句（「我要退款」
  「帮我把这单退了」「I want a refund」）优先 —— 「东西一直没收到，我要退款」是要退款（复核 L2-1）。
* ``emotion``：calm / upset / angry，按词表（情绪触发词、「着急 / 失望 / 等了好久」、「谢谢 /
  不着急」），没有线索不给（不猜「平静」）。
* ``product`` / ``problem``：商品名、问题描述**只从词表取**（:data:`PRODUCT_WORDS` /
  :data:`PROBLEM_WORDS`，最左最长）。词表取不到就不给 —— 自由抽取会把客户的住址、手机号当成
  「商品」存进槽位表与转人工卡片（R5）。

跨轮合并：``understand(prior_slots=…)`` 把上一轮的槽位并进来，本轮抽到的覆盖旧值
（契约 §2 第 4 步「新值覆盖旧值」）。本轮只补了槽位（例如追问后只回一个单号）、意图判不出
而之前说过要退款 / 退货 / 换货时，意图按之前的诉求算（退换货）。

## 不写库、不落事件

本模块只返回结构；唯一的写是模型账（真模型路径）。槽位值、订单号不进任何日志（R5）。
"""

from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from maos import kb
from maos.core.store import record_model_failure, record_model_usage
from maos.domain.cs import scripts, triggers
from maos.domain.cs.lang import detect_lang
from maos.domain.cs.ports import (
    EMOTION_ANGRY,
    EMOTION_CALM,
    EMOTION_UPSET,
    EMOTION_VALUES,
    LANG_EN,
    LANGS,
    REQUEST_EXCHANGE,
    REQUEST_OTHER,
    REQUEST_REFUND,
    REQUEST_RETURN,
    REQUEST_TRACK,
    REQUEST_VALUES,
    SLOT_EMOTION,
    SLOT_KEYS,
    SLOT_ORDER_NO,
    SLOT_PROBLEM,
    SLOT_PRODUCT,
    SLOT_REQUEST,
    SLOT_SOURCE_MODEL,
    SLOT_SOURCE_RULE,
    SLOT_SOURCES,
)
from maos.domain.cs.types import (
    HANDOFF_ANGER,
    INTENT_COMPENSATION,
    INTENT_COMPLAINT,
    INTENT_GENERAL,
    INTENT_HANDOFF_REQUEST,
    INTENT_LOGISTICS,
    INTENT_PRIVACY,
    INTENT_REFUND_PAYMENT,
    INTENT_RETURN_EXCHANGE,
    INTENT_UNKNOWN,
    INTENTS,
)
from maos.model.client import ScriptedModelClient, Tier

log = logging.getLogger("maos.cs")

#: 落 ``model_usage.call_site`` 的值（登记在 ``maos/obs/call_sites.py``，逐字节对齐由
#: ``maos/tests/test_cost_metrics.py`` 钉）。模型调用发生在本模块的 :func:`understand` 里
#: （契约把 model / store / plan_id 给了它），所以常量放在这里 —— 成本守卫按
#: ``record_model_usage`` 所在的模块取 ``CALL_SITE``。
CALL_SITE = "maos/domain/cs/understand.py::understand"

#: 记账行的 agent_role：客服前台的角色名（``desk.CS_FRONT_DESK_IDENTITY.role``；本模块不 import
#: desk —— 前台要 import 本模块，反过来就成环）。
AGENT_ROLE = "cs_front_desk"

#: 模型调用档位：意图分类是轻活。
MODEL_TIER = Tier.LIGHT


# ---------------------------------------------------------------------------
# 结果类型（契约 §1.4 冻结的形状）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Understanding:
    """理解一句话的结果。``slots`` 是合并过上一轮之后的全量槽位（只读映射）。

    ``source`` 说的是**意图**从哪来：``rule``（触发词 / 示例 / 词表，含判不出的 unknown）或
    ``model``（调了模型且用了它的输出，含它输出不合法被夹成的 unknown）。槽位恒来自规则。
    """
    lang: str
    intent: str
    slots: Mapping[str, str]
    source: str

    def __post_init__(self) -> None:
        if self.lang not in LANGS:
            raise ValueError(f"lang 必须取自 {LANGS}，收到 {self.lang!r}")
        if self.intent not in INTENTS:
            raise ValueError(f"intent 必须取自 types.INTENTS，收到 {self.intent!r}")
        if self.source not in SLOT_SOURCES:
            raise ValueError(f"source 必须取自 {SLOT_SOURCES}，收到 {self.source!r}")
        slots = dict(self.slots or {})
        for key, value in slots.items():
            if key not in SLOT_KEYS or not isinstance(value, str) or not value:
                raise ValueError(f"槽位 {key!r}={value!r} 不合契约（键取自 SLOT_KEYS、值为非空串）")
        if slots.get(SLOT_REQUEST, REQUEST_OTHER) not in REQUEST_VALUES:
            raise ValueError(f"request 槽位取值 {slots[SLOT_REQUEST]!r} 不在 REQUEST_VALUES")
        if slots.get(SLOT_EMOTION, EMOTION_CALM) not in EMOTION_VALUES:
            raise ValueError(f"emotion 槽位取值 {slots[SLOT_EMOTION]!r} 不在 EMOTION_VALUES")
        ordered = {k: slots[k] for k in SLOT_KEYS if k in slots}
        object.__setattr__(self, "slots", MappingProxyType(ordered))

    def to_json(self) -> dict[str, Any]:
        return {"lang": self.lang, "intent": self.intent, "slots": dict(self.slots),
                "source": self.source}

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> "Understanding":
        return cls(lang=str(d["lang"]), intent=str(d["intent"]),
                   slots={str(k): str(v) for k, v in dict(d.get("slots") or {}).items()},
                   source=str(d["source"]))


# ---------------------------------------------------------------------------
# 规范化
# ---------------------------------------------------------------------------
def _nfkc(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").replace("’", "'").replace("‘", "'")


def _compact(text: str) -> str:
    """中文词表用：NFKC、小写、去掉空白与格式字符。"""
    return "".join(c for c in _nfkc(text).lower()
                   if not c.isspace() and unicodedata.category(c) != "Cf")


def _spaced(text: str) -> str:
    """英文词表用：NFKC、小写、空白压成一个空格。"""
    return re.sub(r"\s+", " ", _nfkc(text).lower()).strip()


#: 英文按词认：前后不挨 ASCII 字母数字。
_EN_WRAP = "(?<![a-z0-9])(?:{})(?![a-z0-9])"


def _alt(parts) -> str:
    return "|".join(f"(?:{p})" for p in parts)


def _zh_re(parts) -> re.Pattern[str]:
    return re.compile(_alt(parts))


def _en_re(parts) -> re.Pattern[str]:
    return re.compile(_EN_WRAP.format(_alt(parts)))


# ---------------------------------------------------------------------------
# 订单号
# ---------------------------------------------------------------------------
#: 纯数字 ≥ 8 位（淘宝 / 京东 / 抖店一类）。
_DIGITS_RE = re.compile(r"(?<![0-9A-Za-z])\d{8,}(?![0-9A-Za-z])")
#: 字母数字混排、带连字符（拼多多「200924-1234567890」、自编号「SO-2026-000123」）。
_HYPHEN_RE = re.compile(r"(?<![0-9A-Za-z-])[0-9A-Za-z]+(?:-[0-9A-Za-z]+)+(?![0-9A-Za-z-])")
#: 短形态：字母 1–4 个 + 数字 ≥ 4 位（「A1001」「JD20260001」），或「#」+ 数字 ≥ 3 位。
_SHORT_RE = re.compile(r"(?<![0-9A-Za-z#])(?:[A-Za-z]{1,4}\d{4,}|#\d{3,})(?![0-9A-Za-z])")
#: 短形态要有的上下文字眼（中文按字、英文按词）。
_ORDER_CONTEXT_ZH = re.compile(r"订单|单号|那单|这单|那一单|这一单|单子|我的单|下的单|拍的单")
_ORDER_CONTEXT_EN = re.compile(r"(?<![a-z])order(?![a-z])")
#: 不是单号的形态：手机号、日期、400 / 800 热线与座机。手机号与热线按**去掉分隔符与国家码之后**
#: 的数字判（复核 L1-1 / L2-4）：「138-1234-5678」「+8613812345678」「86-138-1234-5678」
#: 「4008123123」都不是单号 —— 手机号当单号存进 cs_slot 就是把个人信息写进槽位表（R5）。
_MOBILE_RE = re.compile(r"^1[3-9]\d{9}$")
_HOTLINE_RE = re.compile(r"^[48]00\d{7}$")
_COUNTRY_CODE_RE = re.compile(r"^(?:00)?86(?=1[3-9]\d{9}$)")
_DATE_RE = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}$")
_PHONE_RE = re.compile(r"^(?:[48]00-\d{3,4}-\d{3,4}|0\d{2,3}-\d{7,8}(?:-\d{1,6})?)$")
#: 连字符形态至少要有这么多位数字（再短就像型号「GT-3」「X-100」）。
_HYPHEN_MIN_DIGITS = 6


def _is_phone_shaped(token: str) -> bool:
    """去掉分隔符与国家码（86 / 0086）后是手机号或 400 / 800 热线。"""
    digits = token.replace("-", "")
    if not digits.isdigit():
        return False
    digits = _COUNTRY_CODE_RE.sub("", digits)
    return bool(_MOBILE_RE.match(digits) or _HOTLINE_RE.match(digits))


def _order_candidates(text: str) -> list[tuple[int, str]]:
    """原文里所有像单号的串：(起点, 原样)。

    被排除的连字符串（日期、电话）占的那一段，里面的数字段也不再当纯数字单号认 ——
    否则「0571-88886666」排除了整串，又把后半截「88886666」认成八位单号。
    """
    found: list[tuple[int, str]] = []
    blocked: list[tuple[int, int]] = []
    for m in _HYPHEN_RE.finditer(text):
        tok = m.group(0)
        if _DATE_RE.match(tok) or _PHONE_RE.match(tok) or _is_phone_shaped(tok):
            blocked.append((m.start(), m.end()))
            continue
        if sum(ch.isdigit() for ch in tok) >= _HYPHEN_MIN_DIGITS:
            found.append((m.start(), tok))
    for m in _DIGITS_RE.finditer(text):
        if _is_phone_shaped(m.group(0)):
            continue
        if any(s <= m.start() < e for s, e in blocked):
            continue
        if not any(s <= m.start() < s + len(t) for s, t in found):
            found.append((m.start(), m.group(0)))
    return sorted(found)


#: 只报一个号时常带的客套（「A1001，谢谢」「A1001 thanks」）：判「整句就是这个号」前先抹掉。
_POLITE_RE = re.compile(r"谢谢(?:你|您|啦|了)?|谢啦|多谢|感谢|麻烦(?:你|您)?了|辛苦(?:你|您)?了|好的|您好|你好"
                        r"|(?<![a-z])(?:thanks|thank you|thx|please|pls|hi|hello)(?![a-z])")
_BARE_STRIP = "。.，,！!？?：:；;、~～ 啊呀哦呢哈亲"


def extract_order_no(text: str) -> str:
    """订单号：见模块头「槽位」。取不到返回空串。"""
    s = _nfkc(text)
    strong = _order_candidates(s)
    if strong:
        return strong[0][1].upper()
    lower = s.lower()
    bare = _POLITE_RE.sub(" ", lower).strip(_BARE_STRIP).strip()
    has_context = bool(_ORDER_CONTEXT_ZH.search(_compact(s)) or _ORDER_CONTEXT_EN.search(lower))
    for m in _SHORT_RE.finditer(s):
        if has_context or m.group(0).lower() == bare:
            return m.group(0).upper()
    return ""


# ---------------------------------------------------------------------------
# 诉求
# ---------------------------------------------------------------------------
#: 诉求词表：诉求 → (中文片段, 英文片段)。先后即优先级（同一句中了几种取靠前的）。
REQUEST_WORDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    REQUEST_EXCHANGE: (
        (r"换货", r"换一(?:个|件|双|台|条|套|只|支|瓶|盒)", r"换个(?:新的|颜色|尺码|码|型号|大的|小的)",
         r"换(?:颜色|尺码|大一码|小一码|大一号|小一号|码)", r"换个?(?:大|小)一?[码号]", r"调换",
         r"更换(?:商品|新的|一个)"),
        (r"exchange", r"swap", r"replace(?:ment)?"),
    ),
    REQUEST_RETURN: (
        (r"退货", r"退回去", r"寄回(?!来)", r"退掉", r"退了吧", r"不想要了", r"不要了", r"退换",
         r"无理由退",
         # 「帮我把这单退了」「给我退了」：单个「退」带着办事的人称（退款、退钱、退到卡是钱）
         r"(?:帮|给|替)我.{0,6}退(?![款钱费宽到回休出])"),
        (r"return(?:ing|ed)?(?! policy)", r"send (?:it |this |them )?back"),
    ),
    REQUEST_REFUND: (
        (r"退款", r"退钱", r"钱退", r"退费", r"返款", r"退我钱", r"把钱还", r"钱还给我"),
        (r"refund(?:ed|s)?", r"money back"),
    ),
    REQUEST_OTHER: (
        (r"改地址", r"修改地址", r"改收货", r"换地址", r"换个地址", r"换收货", r"地址填错", r"地址写错",
         r"改(?:配送|送货)时间", r"取消订单", r"(?:能|要|想|帮我|给我)取消", r"不要发了"),
        (r"change (?:my |the )?(?:shipping |delivery )?address", r"cancel (?:my |the |this )?order"),
    ),
    REQUEST_TRACK: (
        (r"查(?:一下|下|查)?(?:我的)?(?:物流|快递|订单|单子|包裹)", r"催(?:单|发货|一下|一催|催)",
         r"快递到哪", r"物流到哪", r"包裹到哪", r"物流信息", r"物流没更新", r"物流不动"),
        (r"track(?:ing)?", r"where(?:'s| is) my", r"shipped yet", r"has (?:it|my \w+) shipped"),
    ),
}

_REQUEST_RES: dict[str, tuple[re.Pattern[str], re.Pattern[str]]] = {
    k: (_zh_re(zh), _en_re(en)) for k, (zh, en) in REQUEST_WORDS.items()
}

#: 问规则的说法（怎么办理、要多久、能不能）：有它、又没有「帮我 / 给我」这类办事的字眼，
#: 就是在问政策，不给 request。
_HOWTO_ZH = _zh_re((r"怎么", r"怎样", r"如何", r"咋", r"流程", r"步骤", r"条件", r"规则", r"要求",
                    r"政策", r"谁出", r"我出", r"你们出", r"谁承担", r"谁付", r"在哪", r"哪里", r"入口",
                    r"需要什么", r"要什么"))
_HOWTO_EN = _en_re((r"how (?:do|can|to|does|should)", r"what(?:'s| is) the (?:policy|process)",
                    r"policy", r"process", r"who pays"))
#: 是非问（能退吗 / 可以换吗）：没有办事的字眼时也是在问政策（「其他」类诉求除外：改地址、
#: 取消订单问一句「能改吗」就是要改）。
_YESNO_ZH = _zh_re((r"能不能", r"可不可以", r"可以吗", r"能吗", r"行吗", r"支持吗", r"可以不",
                    r"能.{0,6}[吗嘛么不][?!.~。]*$", r"可以.{0,6}[吗嘛么不][?!.~。]*$"))
_YESNO_EN = _en_re((r"can i", r"could i", r"is it possible", r"am i able",
                    r"do you (?:accept|allow|support)"))
_ACTION_ZH = _zh_re((r"帮我", r"给我", r"替我", r"帮忙", r"麻烦你?帮", r"请帮", r"我要", r"我想要",
                     r"我需要", r"申请", r"办理", r"办一下", r"处理一下"))
_ACTION_EN = _en_re((r"i want", r"i'd like", r"i would like", r"i need", r"please", r"help me"))

#: 进度线索要落在「订单的事」上才算查进度（「你们店开了多久」「怎么样了」闲聊不算）。
_TRACK_TOPIC_ZH = _zh_re((r"单", r"货", r"快递", r"物流", r"包裹", r"东西", r"钱", r"款", r"售后",
                          r"申请", r"发票", r"寄"))
_TRACK_TOPIC_EN = _en_re((r"order", r"package", r"parcel", r"refund", r"money", r"item",
                          r"shipment", r"delivery", r"return", r"exchange"))

# ---- 明说要办（复核 L2-1）：「我要退款」「帮我把这单退了」「I want a refund」 ----------------
#: 办事的字眼**紧挨着**（中间至多几个不是「查 / 看 / 问」的字）退款 / 退货 / 换货的说法，且同一分句里
#: 没有进度或时长线索：这是客户明说要办，诉求就是它 —— 前一分句在催（「东西一直没收到，我要退款」）、
#: 后一分句在问规则（「我要退货，运费谁出」）都不改。「申请退款后……」「退款以后……」里的办事是个
#: 时间点，不算。
_EXPLICIT_ACTION_ZH = r"(?:帮我|给我|替我|帮忙|麻烦你?帮?我?|请帮?我?|我要|我想要?|我需要|要求|申请|办理)"
#: 办事字眼与诉求说法之间允许的字（不许夹「查 / 看 / 问 / 知道」：「帮我查下退款」是在查）。
_EXPLICIT_GAP_ZH = r"[^查看问催知解确认，,。.!！?？;；]{0,5}?"
_EXPLICIT_CORE_ZH: dict[str, tuple[str, ...]] = {
    REQUEST_EXCHANGE: (r"换货", r"换一(?:个|件|双|台|条|套|只|支|瓶|盒)", r"换个?(?:新的|颜色|尺码|码|型号|大|小)",
                       r"换(?:颜色|尺码|码|尺寸)", r"调换", r"更换"),
    REQUEST_RETURN: (r"退货", r"退回去", r"退掉", r"退换", r"寄回(?!来)", r"退(?![款钱费宽到回休出])"),
    REQUEST_REFUND: (r"退款", r"退钱", r"退费", r"返款", r"退我钱", r"把钱退", r"把钱还"),
}
_EXPLICIT_TAIL_ZH = r"(?!了?(?:以|之)?后)"
_EXPLICIT_ZH_RES: dict[str, re.Pattern[str]] = {
    k: re.compile(_EXPLICIT_ACTION_ZH + _EXPLICIT_GAP_ZH + f"(?:{_alt(core)})" + _EXPLICIT_TAIL_ZH)
    for k, core in _EXPLICIT_CORE_ZH.items()
}
_EXPLICIT_EN_RES: dict[str, re.Pattern[str]] = {
    k: _en_re((rf"(?:i want|i'd like|i would like|i need|please|pls|help me)(?: to)?"
               rf"(?: (?:get|have|request|make|process|issue|apply for|do))?"
               rf"(?: (?:a|an|my|the|full|this|it))* (?:{word})",))
    for k, word in ((REQUEST_EXCHANGE, r"exchange|replacement|swap"),
                    (REQUEST_RETURN, r"return(?! policy)|send (?:it |this |them )?back"),
                    (REQUEST_REFUND, r"refund|money back"))
}
#: 分句：按标点切，「明说要办」只在同一分句里看线索。
_CLAUSE_SPLIT_RE = re.compile(r"[，,。.!！?？;；~～\n]+")


def explicit_request(text: str) -> str:
    """明说要办的诉求（refund / return / exchange），没有返回空串。先后即优先级（同 REQUEST_WORDS）。

    只看**同一分句**里既没有进度 / 时长线索、也不是在问规则（怎么 / 流程 / 能不能）的那些分句：
    「我想退货怎么弄」「给我退了没」「申请退款后钱什么时候退回来」都不算明说要办。
    """
    for clause in _CLAUSE_SPLIT_RE.split(_nfkc(text)):
        if not clause.strip() or scripts.detect_cue(clause):
            continue
        zh, en = _compact(clause), _spaced(clause)
        if (_HOWTO_ZH.search(zh) or _HOWTO_EN.search(en)
                or _YESNO_ZH.search(zh) or _YESNO_EN.search(en)):
            continue
        for key in (REQUEST_EXCHANGE, REQUEST_RETURN, REQUEST_REFUND):
            if _EXPLICIT_ZH_RES[key].search(zh) or _EXPLICIT_EN_RES[key].search(en):
                return key
    return ""


def extract_request(text: str) -> str:
    """诉求：见模块头「槽位」。取不到返回空串。

    明说要办（:func:`explicit_request`：「我要退款」「帮我把这单退了」）→ 就是它，别的分句在催在问
    都不改；否则查进度（进度线索 + 说的是订单的事）→ track，哪怕句子里有「退款」；问规则（怎么 /
    流程 / 多久 / 什么时候 / 谁出）→ 不给；是非问又没有「帮我 / 我要」→ 不给；其余按诉求词表（先后即优先级）。
    """
    explicit = explicit_request(text)
    if explicit:
        return explicit
    zh, en = _compact(text), _spaced(text)
    kind = ""
    for key, (zre, ere) in _REQUEST_RES.items():
        if zre.search(zh) or ere.search(en):
            kind = key
            break
    cue = scripts.detect_cue(text)
    on_topic = bool(kind or _TRACK_TOPIC_ZH.search(zh) or _TRACK_TOPIC_EN.search(en))
    if cue == scripts.CUE_PROGRESS and on_topic:
        return REQUEST_TRACK
    if not kind or kind == REQUEST_TRACK:
        return kind
    action = bool(_ACTION_ZH.search(zh) or _ACTION_EN.search(en))
    howto = bool(_HOWTO_ZH.search(zh) or _HOWTO_EN.search(en)) or cue == scripts.CUE_DURATION
    if howto:
        return ""
    if kind != REQUEST_OTHER and not action and (_YESNO_ZH.search(zh) or _YESNO_EN.search(en)):
        return ""
    return kind


# ---------------------------------------------------------------------------
# 情绪
# ---------------------------------------------------------------------------
EMOTION_WORDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    EMOTION_ANGRY: (
        (r"太过分", r"过分了", r"忍无可忍", r"火大", r"恼火", r"愤怒", r"气人", r"欺负人", r"耍我",
         r"忽悠", r"坑人", r"骗人", r"无语死"),
        (r"angry", r"furious", r"outraged", r"unacceptable", r"ridiculous"),
    ),
    EMOTION_UPSET: (
        (r"着急", r"急死", r"急用", r"很急", r"挺急", r"失望", r"郁闷", r"烦死", r"好烦", r"无语",
         r"难受", r"伤心", r"不开心", r"担心", r"怎么还", r"到底", r"等了好久", r"等了很久", r"好几天",
         r"一直没", r"太慢", r"慢死", r"心累", r"崩溃", r"不满意", r"不满"),
        (r"upset", r"disappointed", r"frustrat(?:ed|ing)", r"annoyed", r"worried", r"unhappy",
         r"so slow", r"urgent", r"asap", r"still (?:not|waiting|haven't|hasn't)"),
    ),
    EMOTION_CALM: (
        (r"谢谢", r"感谢", r"多谢", r"不着急", r"不急", r"没事", r"辛苦", r"麻烦了", r"请问", r"您好",
         r"你好", r"好的"),
        (r"thanks", r"thank you", r"no rush", r"no hurry", r"please", r"hello", r"hi"),
    ),
}
_EMOTION_RES = {k: (_zh_re(zh), _en_re(en)) for k, (zh, en) in EMOTION_WORDS.items()}
#: 「不着急 / 不急 / 不用急」是平静，不是着急。
_CALM_NEGATION_RE = re.compile(r"不(?:用|要|太|是很)?(?:着急|急)")


def extract_emotion(text: str) -> str:
    """情绪：情绪类触发词或 angry 词表 → angry；upset 词表 → upset；calm 词表 → calm；否则不给。"""
    if HANDOFF_ANGER in triggers.matched_reasons(text):
        return EMOTION_ANGRY
    zh, en = _compact(text), _spaced(text)
    # 「不着急 / 不急」是平静：先抹掉再找 upset（否则「着急」二字会把它判成着急）。
    calm_zh = _CALM_NEGATION_RE.sub(" ", zh)
    for emotion in (EMOTION_ANGRY, EMOTION_UPSET):
        zre, ere = _EMOTION_RES[emotion]
        if zre.search(calm_zh) or ere.search(en):
            return emotion
    zre, ere = _EMOTION_RES[EMOTION_CALM]
    if zre.search(zh) or ere.search(en):
        return EMOTION_CALM
    return ""


# ---------------------------------------------------------------------------
# 商品与问题（只从词表取）
# ---------------------------------------------------------------------------
#: 商品名词表（常见电商品类的叫法）。最左最长；词表外的一律不给。
PRODUCT_WORDS: tuple[str, ...] = (
    # 服饰鞋包
    "衣服", "外套", "羽绒服", "大衣", "风衣", "夹克", "卫衣", "毛衣", "针织衫", "衬衫", "t恤", "短袖",
    "裤子", "牛仔裤", "短裤", "裙子", "连衣裙", "半身裙", "内衣", "袜子", "睡衣", "帽子", "围巾",
    "手套", "鞋子", "运动鞋", "皮鞋", "靴子", "拖鞋", "凉鞋", "包包", "背包", "双肩包", "钱包",
    "行李箱", "腰带", "皮带", "假发",
    # 数码电器
    "手机", "耳机", "蓝牙耳机", "充电器", "充电宝", "数据线", "手机壳", "钢化膜", "平板", "电脑",
    "笔记本", "键盘", "鼠标", "显示器", "音箱", "手表", "智能手表", "手环", "相机", "镜头", "路由器",
    "电视", "冰箱", "洗衣机", "空调", "风扇", "电风扇", "吹风机", "电吹风", "剃须刀", "电动牙刷",
    "吸尘器", "扫地机", "扫地机器人", "空气炸锅", "电饭煲", "微波炉", "烤箱", "榨汁机", "豆浆机",
    "热水壶", "电水壶", "加湿器", "台灯", "灯泡", "插座", "电池",
    # 家居日用
    "杯子", "水杯", "保温杯", "盘子", "炒锅", "菜刀", "筷子", "床单", "被子",
    "枕头", "四件套", "毛巾", "浴巾", "窗帘", "地毯", "拖把", "垃圾袋", "垃圾桶", "收纳盒", "衣架",
    "椅子", "桌子", "沙发", "床垫", "玩具", "积木", "文具", "图书",
    # 美妆食品
    "口红", "面膜", "面霜", "乳液", "精华", "爽肤水", "洗面奶", "防晒霜", "粉底", "香水", "洗发水",
    "沐浴露", "牙膏", "零食", "茶叶", "咖啡豆", "咖啡机", "奶粉", "水果", "月饼", "巧克力", "饼干",
    "保健品",
    # 英文
    "shirt", "t-shirt", "jacket", "coat", "dress", "skirt", "jeans", "pants", "shoes", "sneakers",
    "boots", "bag", "backpack", "wallet", "hat", "phone", "headphones", "earbuds", "charger",
    "cable", "laptop", "tablet", "keyboard", "mouse", "watch", "camera", "speaker", "tv",
    "lamp", "cup", "mug", "bottle", "pan", "pot", "blanket", "pillow", "towel", "toy", "book",
    "lipstick", "perfume", "shampoo", "snacks", "coffee", "tea",
)

#: 问题描述词表（「坏了 / 发错了 / 没收到」这一类的说法）。最左最长；词表外的一律不给。
PROBLEM_WORDS: tuple[str, ...] = (
    "坏了", "坏的", "摔坏了", "压坏了", "破了", "破损", "裂了", "裂纹", "裂开了", "划痕", "刮花",
    "有瑕疵", "瑕疵", "质量问题", "质量太差", "不能用", "用不了", "开不了机", "不开机", "充不进电",
    "充不上电", "没反应", "不亮", "漏水", "漏液", "漏气", "掉色", "褪色", "起球", "开线", "变形",
    "发错了", "发错货", "发错颜色", "发错尺码", "少发", "漏发", "缺件", "少件", "空包",
    "尺码不对", "不合适", "太大了", "太小了", "偏大", "偏小", "颜色不对", "色差", "与描述不符",
    "和描述不符", "货不对板", "过期", "变质", "有异味", "味道不对", "是假的", "假货",
    "没收到", "没收到货", "丢件", "丢了", "寄丢了", "显示签收", "被签收", "没发货", "还没发货",
    "物流不动", "物流没更新", "地址填错了", "地址写错了", "地址错了",
    "重复扣款", "扣了两次", "多扣", "支付失败", "付款失败", "付不了款", "退款没到", "没到账",
    "broken", "damaged", "cracked", "scratched", "defective", "doesn't work", "does not work",
    "not working", "wrong item", "wrong size", "wrong color", "missing", "not received",
    "never arrived", "lost", "charged twice", "payment failed", "expired", "leaking", "too small",
    "too big",
)


def _lexicon_re(words) -> re.Pattern[str]:
    """最左最长：长的写在前面（同一位置先试长的），英文词加边界。"""
    parts = []
    for w in sorted(set(words), key=lambda w: (-len(w), w)):
        esc = re.escape(w)
        parts.append(_EN_WRAP.format(esc) if w.isascii() else esc)
    return re.compile("|".join(parts))


_PRODUCT_RE = _lexicon_re(PRODUCT_WORDS)
#: 含商品名、却不是在说商品的词（「手机号」不是手机）：找商品前先抹掉。
_NOT_PRODUCT_RE = re.compile(r"手机号码?|手机支付|手机银行|电话号码?")
_PROBLEM_RE = _lexicon_re(PROBLEM_WORDS)


def _first(pattern: re.Pattern[str], text: str) -> str:
    """中文在去空白的文本上找、英文在保留空格的文本上找，取靠前出现的那个。"""
    zh = pattern.search(_compact(text))
    en = pattern.search(_spaced(text))
    got = [m for m in (zh, en) if m is not None]
    if not got:
        return ""
    cjk_first = [m for m in got if not m.group(0).isascii()]
    return (cjk_first or got)[0].group(0)


def extract_slots(text: str, *, lang: str) -> dict[str, str]:
    """确定性槽位抽取：订单号正则、诉求 / 情绪 / 商品 / 问题词表。取不到的键不出现。

    ``lang`` 只用来决定先认哪种写法（英文句里的英文商品名优先）；两种语言的词表都会看 ——
    中英混排（「我的 order A1001 到哪了」）是常态。
    """
    out: dict[str, str] = {}
    order_no = extract_order_no(text)
    if order_no:
        out[SLOT_ORDER_NO] = order_no
    product = _first_lang(_PRODUCT_RE, _NOT_PRODUCT_RE.sub(" ", _nfkc(text)), lang)
    if product:
        out[SLOT_PRODUCT] = product
    problem = _first_lang(_PROBLEM_RE, text, lang)
    if problem:
        out[SLOT_PROBLEM] = problem
    request = extract_request(text)
    if request:
        out[SLOT_REQUEST] = request
    emotion = extract_emotion(text)
    if emotion:
        out[SLOT_EMOTION] = emotion
    return out


def _first_lang(pattern: re.Pattern[str], text: str, lang: str) -> str:
    if lang == LANG_EN:
        m = pattern.search(_spaced(text))
        if m is not None:
            return m.group(0)
    return _first(pattern, text)


# ---------------------------------------------------------------------------
# 意图
# ---------------------------------------------------------------------------
#: 关键词词表：意图 → (中文片段, 英文片段)。每中一处记一票（最长的先吃，吃过的字不再算）。
INTENT_KEYWORDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    INTENT_RETURN_EXCHANGE: (
        (r"退货", r"换货", r"退换", r"无理由", r"售后", r"寄回", r"退回去", r"质量问题", r"有瑕疵",
         r"瑕疵", r"坏了", r"尺码不对", r"不合适", r"不合身", r"不喜欢", r"运费险", r"拍错", r"买错",
         r"不想要", r"不要了", r"换一(?:个|件|双)", r"换个新", r"换尺码", r"退掉", r"退了吧",
         # 换大小 / 换尺寸（与 REQUEST_WORDS 的换货说法对齐，复核 L3-5）
         r"换个?(?:大|小)一?[码号]", r"换个?(?:颜色|码|尺寸|型号)", r"调换",
         # 商品本身的毛病（质量问题换货那一篇）；包裹破损、摔坏是物流那一篇，不在这里
         r"裂纹", r"裂了", r"划痕", r"不能用", r"用不了", r"开不了机", r"漏水", r"掉色", r"起球",
         r"色差", r"发错", r"少发", r"漏发", r"缺件",
         r"有个洞", r"有(?:一个|个)?破洞", r"破了个洞", r"开胶", r"脱胶", r"开线", r"脱线", r"断了"),
        (r"return(?:s|ed|ing)?", r"exchange", r"replace(?:ment)?", r"defective", r"wrong size",
         r"doesn'?t fit", r"send (?:it |this |them )?back"),
    ),
    INTENT_REFUND_PAYMENT: (
        (r"退款", r"退钱", r"钱退", r"退费", r"返款", r"退宽", r"到账", r"到帐", r"原路",
         r"钱.{0,4}(?:到|回)", r"退回来(?!的货)", r"(?:退|回)到卡", r"到卡上", r"卡里", r"支付",
         r"退(?:的|回的|回来的)钱",
         r"付款", r"付钱", r"付不了",
         r"扣款", r"扣钱", r"扣费", r"扣了", r"花呗", r"白条", r"信用卡", r"分期", r"微信付",
         r"(?:用|收|支持)微信", r"支付宝", r"云闪付", r"货到付款", r"发票", r"开票", r"电子票",
         r"开(?:个|张)票", r"专票", r"普票", r"抬头"),
        (r"refund(?:s|ed)?", r"money back", r"payments?", r"pay(?:ing)?", r"paid", r"charged?",
         r"invoice", r"receipt", r"credit card", r"paypal", r"installments?"),
    ),
    INTENT_LOGISTICS: (
        (r"发货", r"发出", r"发走", r"寄出", r"出库", r"出货", r"快递", r"物流", r"包裹", r"运费",
         r"邮费", r"包邮", r"配送", r"送货", r"送到", r"送过来", r"送达", r"寄过来", r"寄到", r"派送",
         r"揽收", r"签收", r"驿站", r"运单", r"顺丰", r"中通", r"圆通", r"申通", r"韵达", r"邮政",
         r"ems", r"收货地址", r"改地址", r"地址", r"收件", r"丢件", r"寄丢", r"催单", r"催发", r"到货",
         r"收到货", r"没收到", r"发了没", r"发了吗",
         # 运输途中的破损（包裹丢失或破损那一篇，复核 L3-5）
         r"碎了", r"摔碎", r"压碎", r"破损", r"压坏",
         # 「什么时候发？」只认句末的「发」（「什么时候发新品」是售前）
         r"(?:什么|啥|何|几)时(?:候)?(?:能|可以|才)?发(?=[吗呢呀啊哦]|$|[?!.,。，])",
         # 「付款后 / 下单后 …… 发」：付款、下单只是时间点，问的是发货（最长的先吃，吃掉付款二字）
         r"(?:付完?款|付钱|支付|下单|拍下?)(?:了|完)?(?:以后|之后|后)?.{0,6}?[发寄]"),
        (r"ship(?:s|ped|ping)?", r"deliver(?:y|ed|s)?", r"package", r"parcel", r"courier",
         r"carrier", r"dispatch(?:ed)?", r"arrive[sd]?", r"address", r"postage"),
    ),
    INTENT_GENERAL: (
        (r"你好", r"您好", r"在吗", r"在么", r"在的吗", r"在不在", r"有人吗", r"有人在", r"哈喽", r"嗨",
         r"早上好", r"晚上好", r"下午好", r"谢[谢啦了咯]", r"感谢", r"多谢", r"再见", r"拜拜", r"辛苦了",
         r"客服.{0,4}(?:上班|下班|在线|上线|下线|值班|几点)", r"几点上班", r"几点下班", r"上班时间",
         r"服务时间", r"在线时间", r"有客服"),
        (r"hello", r"hi", r"hey", r"thanks", r"thank you", r"bye", r"goodbye", r"business hours",
         r"working hours", r"opening hours"),
    ),
}

#: 弱关键词：单个字（「退」「发」）也是线索，但只记半票 ——
#: 同一句里有正经的词（「退款」）时让它说了算。
INTENT_WEAK_KEYWORDS: dict[str, tuple[str, ...]] = {
    # 单个「退」（能退吗 / 要退的话 / 帮我退了）：不是退款、退钱、退到卡、退回来、退的钱（那是钱）
    INTENT_RETURN_EXCHANGE: (r"(?<![钱])退(?![款钱费宽到回休出役伍步缩烧化]|的钱)",),
    # 单个「发」只在「发 + 吗 / 不 / 没 / 了 / 过来」和「才 / 能 / 再 / 先 发」里算（「发票」「发错」不算）
    INTENT_LOGISTICS: (r"(?<!开)发(?=[吗不没了过])", r"[才能再先就]发(?![票现生错])"),
}
WEAK_VOTE = 0.5

#: 票数相同时的先后：退换货 > 支付退款 > 物流 > 通用（「退货运费谁出」是退换货篇，
#: 「你好，退货流程」问的是退货不是问候）。
INTENT_TIE_ORDER: tuple[str, ...] = (INTENT_RETURN_EXCHANGE, INTENT_REFUND_PAYMENT,
                                     INTENT_LOGISTICS, INTENT_GENERAL)

_INTENT_ZH_RES = {
    k: [(re.compile(p), 1.0) for p in zh]
    + [(re.compile(p), WEAK_VOTE) for p in INTENT_WEAK_KEYWORDS.get(k, ())]
    for k, (zh, _en) in INTENT_KEYWORDS.items()
}
_INTENT_EN_RES = {k: [re.compile(_EN_WRAP.format(p)) for p in en]
                  for k, (_zh, en) in INTENT_KEYWORDS.items()}


def keyword_votes(text: str) -> dict[str, float]:
    """每个意图在原文里的票数：中文最长的先吃、吃过的字不再算，一处一票（弱关键词半票）；英文按词。"""
    zh = _compact(text)
    spans: list[tuple[int, int, str, float]] = []
    for intent, pats in _INTENT_ZH_RES.items():
        for p, weight in pats:
            spans.extend((m.start(), m.end(), intent, weight)
                         for m in p.finditer(zh) if m.end() > m.start())
    spans.sort(key=lambda s: (-(s[1] - s[0]), -s[3], s[0], INTENT_TIE_ORDER.index(s[2])))
    taken = [False] * len(zh)
    votes = {k: 0.0 for k in INTENT_TIE_ORDER}
    for start, end, intent, weight in spans:
        if any(taken[start:end]):
            continue
        for i in range(start, end):
            taken[i] = True
        votes[intent] += weight
    en = _spaced(text)
    for intent, pats in _INTENT_EN_RES.items():
        votes[intent] += sum(1 for p in pats for _ in p.finditer(en))
    return votes


def _vote_intent(votes: Mapping[str, float]) -> str:
    best = max(votes.values(), default=0)
    if best <= 0:
        return INTENT_UNKNOWN
    for intent in INTENT_TIE_ORDER:
        if votes.get(intent, 0) == best:
            return intent
    return INTENT_UNKNOWN


#: 查进度时查的是什么：钱 → 支付退款；售后单 / 退换货申请 → 退换货；其余（包裹）→ 物流。
_TRACK_REFUND_ZH = _zh_re((r"退款", r"退钱", r"钱", r"到账", r"到帐", r"退宽", r"扣款", r"发票"))
_TRACK_REFUND_EN = _en_re((r"refund", r"money", r"payment", r"charge", r"invoice"))
_TRACK_AFTERSALE_ZH = _zh_re((r"售后", r"退货", r"换货", r"退换"))
_TRACK_AFTERSALE_EN = _en_re((r"return", r"exchange"))
_TRACK_MONEY_ZH = _zh_re((r"钱", r"到账", r"到帐"))
_TRACK_APPLY_ZH = _zh_re((r"申请", r"审核"))


def _request_intent(request: str, text: str) -> str:
    """本轮诉求对应的意图（契约 §1.5 目录：要退款 / 退货 / 换货都是 RET-005 那篇 = 退换货）。

    查进度（track）按查的是什么分：只提到钱 → 支付退款（PAY-003）；提到退货 / 换货 / 售后单
    → 退换货（RET-005）；两样都提到时看有没有「到账 / 钱」（问的是钱）；都没提到而说了「申请 /
    审核」→ 退换货；其余是包裹 → 物流（LOG-004）。
    """
    if request in (REQUEST_REFUND, REQUEST_RETURN, REQUEST_EXCHANGE):
        return INTENT_RETURN_EXCHANGE
    if request != REQUEST_TRACK:
        return INTENT_UNKNOWN
    zh, en = _compact(text), _spaced(text)
    money = bool(_TRACK_REFUND_ZH.search(zh) or _TRACK_REFUND_EN.search(en))
    goods = bool(_TRACK_AFTERSALE_ZH.search(zh) or _TRACK_AFTERSALE_EN.search(en))
    if money and goods:
        return INTENT_REFUND_PAYMENT if _TRACK_MONEY_ZH.search(zh) else INTENT_RETURN_EXCHANGE
    if money:
        return INTENT_REFUND_PAYMENT
    if goods or _TRACK_APPLY_ZH.search(zh):
        return INTENT_RETURN_EXCHANGE
    return INTENT_LOGISTICS


# ---- 意图示例（ADP 课程 4.4：相近说法靠示例强制干预） ------------------------
#: 「说法 → 意图」。词表容易判错或判不出的说法写在这里；原文与某条在实词上的相似度
#: （:func:`example_similarity`）≥ :data:`EXAMPLE_MIN_SIMILARITY` 就按那条的意图、压过关键词词表；
#: 只有句式像的，词表一票都没有时才补位（见 :data:`EXAMPLE_MIN_SHAPE_SIMILARITY`）。
#: **自写**：不抄 p12 开发集的句子，也没看过任何留出集。
INTENT_EXAMPLES: tuple[tuple[str, str], ...] = (
    # 物流：没有「发货 / 快递」字眼的问法
    ("东西什么时候能到", INTENT_LOGISTICS),
    ("大概几号能收到", INTENT_LOGISTICS),
    ("我买的东西寄了没有", INTENT_LOGISTICS),
    ("我的货到哪了", INTENT_LOGISTICS),
    ("能送到我们村里吗", INTENT_LOGISTICS),
    ("要另外付邮费吗", INTENT_LOGISTICS),
    ("东西一直没收到", INTENT_LOGISTICS),
    # 支付退款：没有「退款」二字的问法
    ("钱什么时候能回到卡里", INTENT_REFUND_PAYMENT),
    # 与物流那条「东西什么时候能到」只差主语：说的是钱就是支付退款（复核 L2-3 / L3-1）
    ("钱什么时候能到", INTENT_REFUND_PAYMENT),
    ("钱会原路返回吗", INTENT_REFUND_PAYMENT),
    ("钱什么时候到我账上", INTENT_REFUND_PAYMENT),
    ("怎么扣了我两次", INTENT_REFUND_PAYMENT),
    ("付不了钱怎么办", INTENT_REFUND_PAYMENT),
    ("能开个票吗", INTENT_REFUND_PAYMENT),
    # 退换货：没有「退货 / 换货」字眼的问法
    ("收到的东西是坏的", INTENT_RETURN_EXCHANGE),
    ("尺码拍错了怎么办", INTENT_RETURN_EXCHANGE),
    ("不喜欢可以退吗", INTENT_RETURN_EXCHANGE),
    ("东西有问题想换一个", INTENT_RETURN_EXCHANGE),
    ("穿着不合身想退", INTENT_RETURN_EXCHANGE),
    # 通用
    ("客服什么时候上班", INTENT_GENERAL),
    ("好的知道了谢谢", INTENT_GENERAL),
    # 英文
    ("where is my order", INTENT_LOGISTICS),
    ("when will my package arrive", INTENT_LOGISTICS),
    ("how long does delivery take", INTENT_LOGISTICS),
    ("when will i get my money back", INTENT_REFUND_PAYMENT),
    ("i was charged twice", INTENT_REFUND_PAYMENT),
    ("can i return this item", INTENT_RETURN_EXCHANGE),
    ("the item arrived broken", INTENT_RETURN_EXCHANGE),
    ("thank you for your help", INTENT_GENERAL),
)

#: 示例生效的两档门槛（复核 L2-3 / L3-1：只看整句二元组时，「退款什么时候能到」因为和
#: 「东西什么时候能到」共享「什么时候能到」的虚词骨架而被判成物流，压过了「退款」这个实词）：
#:
#: 1. **实词上像**（:func:`example_similarity`：中文按 ``scripts.weighted_similarity`` 的加权二元组，
#:    虚词二元组几乎不计；英文按去掉虚词后的词集合）≥ :data:`EXAMPLE_MIN_SIMILARITY`：压过关键词词表
#:    （ADP 4.4「示例强制干预」）；
#: 2. **只有句式像**（:func:`example_shape_similarity`：不分虚实的二元组 Dice）≥
#:    :data:`EXAMPLE_MIN_SHAPE_SIMILARITY`，且关键词词表一票都没有：词表判不出时才补位
#:    （「东西大概什么时候能到呀」没有一个物流的词，只能靠句式像「东西什么时候能到」）。
EXAMPLE_MIN_SIMILARITY = 0.75
EXAMPLE_MIN_SHAPE_SIMILARITY = 0.65

#: 英文示例比对时丢掉的虚词（where / is / my 这类句子骨架）。英文按词比：字符二元组在英文上
#: 会被骨架带跑（「where is my refund」与「where is my order」共享 wh / he / er / is / my ……）。
EN_EXAMPLE_STOPWORDS: frozenset[str] = frozenset((
    "a an the i me my mine you your yours we our us it its this that these those he she they them "
    "is are was were be been being am do does did done will would can could should shall may might must "
    "have has had to of for on in at by with from and or but so if when where what which who whom how why "
    "there here please pls hi hello hey just still yet not no yes ok okay t s d m ll re ve don doesn didn "
    "isn aren wasn haven hasn won can cant get got very really any some all"
).split())


def _bigrams(text: str) -> frozenset[str]:
    s = "".join(kb.tokenize(text))
    if len(s) < 2:
        return frozenset({s}) if s else frozenset()
    return frozenset(s[i:i + 2] for i in range(len(s) - 1))


def _en_words(text: str) -> frozenset[str] | None:
    """全英文（没有一个汉字）时的实词集合；有汉字返回 None。"""
    tokens = kb.tokenize(text)
    if not tokens or not all(t.isascii() for t in tokens):
        return None
    return frozenset(t for t in tokens if t not in EN_EXAMPLE_STOPWORDS)


def example_similarity(text: str, example: str) -> float:
    """原文与一条示例**在实词上**的相似度，[0, 1]，六位小数。

    去句尾语气词后逐字相等（``scripts.tail_forms`` 有交集）为 1；两句都是英文时按实词集合的
    Dice（:data:`EN_EXAMPLE_STOPWORDS` 之外的词）；否则是加权二元组 Dice
    （``scripts.weighted_similarity``：实词二元组 1、虚词二元组 0.1）。
    """
    if scripts.tail_forms(text) & scripts.tail_forms(example):
        return 1.0
    a_words, b_words = _en_words(text), _en_words(example)
    if a_words is not None and b_words is not None:
        if not a_words or not b_words:
            return 0.0
        return round(2.0 * len(a_words & b_words) / (len(a_words) + len(b_words)), 6)
    return scripts.weighted_similarity(text, example)


def example_shape_similarity(text: str, example: str) -> float:
    """原文与一条示例**在句式上**的相似度：不分虚实的字符二元组 Dice，[0, 1]，六位小数。"""
    if scripts.tail_forms(text) & scripts.tail_forms(example):
        return 1.0
    a, b = _bigrams(text), _bigrams(example)
    if not a or not b:
        return 0.0
    return round(2.0 * len(a & b) / (len(a) + len(b)), 6)


def _best_example(text: str, score) -> tuple[float, str]:
    best, got = 0.0, INTENT_UNKNOWN
    for example, intent in INTENT_EXAMPLES:
        sim = score(text, example)
        if sim > best:
            best, got = sim, intent
    return best, got


def example_intent(text: str, *, votes: Mapping[str, float] | None = None) -> str:
    """按示例表判的意图（见 :data:`EXAMPLE_MIN_SIMILARITY` 的两档）；没有返回 unknown。同分取表里靠前的。

    ``votes`` 是这句话的关键词票数（缺省现算）：只有一票都没有时才看第二档（句式像）。
    """
    best, got = _best_example(text, example_similarity)
    if best >= EXAMPLE_MIN_SIMILARITY:
        return got
    if votes is None:
        votes = keyword_votes(text)
    if max(votes.values(), default=0) <= 0:
        best, got = _best_example(text, example_shape_similarity)
        if best >= EXAMPLE_MIN_SHAPE_SIMILARITY:
            return got
    return INTENT_UNKNOWN


def rule_intent(text: str, *, request: str = "") -> str:
    """确定性的意图：触发词 → 本轮诉求 → 示例 → 关键词。判不出是 unknown。

    诉求在示例之前（复核 L2-1）：客户明说要办 / 在查自己那一单（「东西一直没收到，我要退款」）时，
    诉求是结构化的信号，示例只是整句像不像。
    """
    hit = triggers.detect(text)
    if hit is not None:
        return hit[1]
    by_request = _request_intent(request, text)
    if by_request != INTENT_UNKNOWN:
        return by_request
    votes = keyword_votes(text)
    by_example = example_intent(text, votes=votes)
    if by_example != INTENT_UNKNOWN:
        return by_example
    return _vote_intent(votes)


# ---------------------------------------------------------------------------
# 模型兜底
# ---------------------------------------------------------------------------
#: 给模型看的意图说明（逐个列出 ``types.INTENTS``）。模型只看得见这一段，写含糊了它就猜。
INTENT_HINTS: dict[str, str] = {
    INTENT_LOGISTICS: "物流：发货时效、快递公司与配送范围、运费包邮、查包裹到哪了、催发货、改地址、丢件破损",
    INTENT_REFUND_PAYMENT: "支付与退款：退款多久到账、支付方式、查某笔退款到没到、支付失败或重复扣款、开发票",
    INTENT_RETURN_EXCHANGE: "退换货：七天无理由、退货流程、质量问题换货、退货运费、要给某个订单退货或退款",
    INTENT_GENERAL: "通用：问候、感谢道别、人工客服的服务时间",
    INTENT_HANDOFF_REQUEST: "客户点名要人工客服、要真人",
    INTENT_COMPLAINT: "投诉、维权、情绪激烈",
    INTENT_COMPENSATION: "要求赔偿或补偿",
    INTENT_PRIVACY: "涉及个人信息、隐私",
    INTENT_UNKNOWN: "与售后无关，或信息不足以判断",
}

_SYSTEM_HEAD = ("你是电商售后客服的意图识别助手。读客户的一句话，判断它属于下面哪一类意图。"
                "\n\n可选的意图只有这些：")
_SYSTEM_TAIL = (f"\n\n判不出来、或与售后无关时输出 \"{INTENT_UNKNOWN}\"，不要猜。"
                "\n只输出 JSON，不要任何解释文字，格式：{\"intent\":\"...\"}"
                "\nintent 只能是上面列出的取值之一。")


def model_system_prompt() -> str:
    """模型的 system 段：逐个列出意图与中文含义。"""
    lines = [f"- {i}：{INTENT_HINTS[i]}" for i in INTENTS]
    return _SYSTEM_HEAD + "\n" + "\n".join(lines) + _SYSTEM_TAIL


def is_real_model(model: Any) -> bool:
    """注入的是真模型（非 None、非 ScriptedModelClient）才调。"""
    return model is not None and not isinstance(model, ScriptedModelClient)


def clamp_intent(raw: Any) -> str:
    """模型输出夹到 ``types.INTENTS``：不在里面（或形状不对）就是 unknown。"""
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except ValueError:
            return INTENT_UNKNOWN
    else:
        data = raw
    intent = data.get("intent") if isinstance(data, dict) else None
    intent = str(intent or "").strip()
    return intent if intent in INTENTS else INTENT_UNKNOWN


def _ask_model(model: Any, store: Any, text: str, *, plan_id: str) -> str | None:
    """调一次模型并记账。成功返回夹过的意图；模型抛错记一行失败、返回 None（不抛）。

    记账口径照 ``refund.reason_classify``：trace_id 空串（前台不属于任何 Run）、task_id None
    （model_usage 不挂轮次，契约 p12 §1.2）、plan_id 照传（``cs:csc-…``）。
    """
    started = time.perf_counter()
    try:
        resp = model.complete(system=model_system_prompt(), user=f"客户原文：{text}",
                              tier=MODEL_TIER)
    except Exception as exc:                          # noqa: BLE001 —— 记一行失败，回规则
        record_model_failure(
            store, exc, agent_role=AGENT_ROLE, call_site=CALL_SITE, tier=MODEL_TIER,
            latency_ms=int((time.perf_counter() - started) * 1000),
            model=getattr(model, "model", "") or "",
            trace_id="", plan_id=plan_id or "", task_id=None,
        )
        log.warning("理解层调模型失败（%s），按规则结果处理", type(exc).__name__)
        return None
    record_model_usage(
        store, resp, client=model, agent_role=AGENT_ROLE, call_site=CALL_SITE, tier=MODEL_TIER,
        latency_ms=int((time.perf_counter() - started) * 1000),
        trace_id="", plan_id=plan_id or "", task_id=None,
    )
    return clamp_intent(getattr(resp, "text", ""))


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def _clean_prior(prior_slots: Mapping[str, str] | None) -> dict[str, str]:
    """上一轮的槽位：只留契约里的键、非空串、闭集槽位的取值合法的。"""
    out: dict[str, str] = {}
    for key, value in dict(prior_slots or {}).items():
        if key not in SLOT_KEYS or not isinstance(value, str) or not value:
            continue
        if key == SLOT_REQUEST and value not in REQUEST_VALUES:
            continue
        if key == SLOT_EMOTION and value not in EMOTION_VALUES:
            continue
        out[key] = value
    return out


def understand(text: str, *, prior_slots: Mapping[str, str], model=None, store=None,
               plan_id: str = "") -> Understanding:
    """一句话 → :class:`Understanding`。见模块头：确定性优先，判不出且是真模型才调模型。"""
    lang = detect_lang(text)
    fresh = extract_slots(text, lang=lang)
    slots = {**_clean_prior(prior_slots), **fresh}
    intent = rule_intent(text, request=fresh.get(SLOT_REQUEST, ""))
    if (intent in (INTENT_UNKNOWN, INTENT_GENERAL) and SLOT_ORDER_NO in fresh
            and not (set(fresh) - {SLOT_ORDER_NO, SLOT_EMOTION})
            and slots.get(SLOT_REQUEST) in (REQUEST_REFUND, REQUEST_RETURN, REQUEST_EXCHANGE)):
        # 本轮只补了单号（追问之后的回答）：意图按之前说过的退款 / 退货 / 换货诉求算。
        # 回答时顺带的客套（「A1001，谢谢」会投「通用」一票）不是这一轮的意图（复核 L2-9）。
        # 查进度（track）查的是什么只有那一轮的原文知道，这里不猜，留给前台。
        intent = INTENT_RETURN_EXCHANGE
    source = SLOT_SOURCE_RULE
    if intent == INTENT_UNKNOWN and is_real_model(model):
        asked = _ask_model(model, store, text, plan_id=plan_id)
        if asked is not None:
            intent, source = asked, SLOT_SOURCE_MODEL
    return Understanding(lang=lang, intent=intent, slots=slots, source=source)
