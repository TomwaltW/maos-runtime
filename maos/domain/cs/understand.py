"""理解层（p13 契约 §1.4 T173、§2 第 0 / 4 步）：一句客户原文 → 语种、槽位、意图。

ADP 课程 3.3 单工作流的「参数提取 + 意图识别」两个节点的 MAOS 版本。前台（T174）在触发词之后
调 ``cs.understand``（本模块的 skill 外壳），拿到槽位并进 ``cs_slot``、拿意图决定走查单还是走话术，
检索时把意图当 ``intent_hint`` 传给 ``scripts.match_scripts``。

## 确定性优先，模型兜底

意图按这个顺序判，前一步判出来后一步就不看（契约 §1.4 与派单）：

1. **触发词**（:func:`maos.domain.cs.triggers.detect`）：隐私 / 赔偿 / 情绪 / 投诉 / 要人工；
2. **本轮诉求**：要退款 / 退货 / 换货 → 退换货；查进度按查的是钱、是售后单还是包裹分到三个意图；
3. **关键词词表**（:data:`INTENT_KEYWORDS`）数票；
4. 以上都判不出 → ``unknown``。话术检索的意图**不在这里做**（检索落 KbRetrieved，是前台的事）；
5. 判不出、且注入的是**真模型**（非 None、非 ``ScriptedModelClient``）才调一次模型，输出夹到
   ``types.INTENTS``（不在里面就是 unknown）。调了就记账（``record_model_usage`` /
   ``record_model_failure``，trace_id 空串、task_id None、plan_id 照传）；模型出错不抛，
   记一行失败、回规则的结果 —— 模型坏了不该让客户被转人工（前台「永不抛」）。

p13 第三轮曾在第 2、3 步之间加过一张「意图示例」表（ADP 课程 4.4「相近说法靠示例强制干预」）。
主会话裁定 R4：示例表在开发集与 T168 holdout 上测不出增益，整张删掉 —— ADP 4.4 的示例干预留给
真模型路径的 few-shot（docs/DECISIONS.md task-t173）。

Scripted / None 下一次模型都不调，零 ``model_usage`` 行：测试、证据、演示恒为确定性。

## 槽位（:func:`extract_slots`）

五个槽位（``ports.SLOT_KEYS``），全部确定性、**只从词表与正则来**，取不到就不给：

* ``order_no``（主会话裁定 R3）：纯数字 ≥ 8 位；字母开头、字母数字混排、总长 ≥ 5 且至少 3 位数字
  （「A1001」「AB12345」「SO-2026-0001」，**不要求**旁边有「订单号」字眼 —— 追问之后客户常常只回
  「H8008 发到哪了」）；数字开头、带连字符、至少 6 位数字的平台单号（「200924-1234567890」）；
  「# + 数字」只在紧挨单号字眼时认。**不认**：手机号（去掉连字符、空格与 86 / +86 / 0086 国家码后
  是 11 位手机号形态的；隐私，不能当单号存进槽位表）、座机（带连字符、括号、空格或不带分隔符的
  「0 + 区号 + 号码」）、400 / 800 热线、日期；离得最近的上下文字眼说的是别的号（银行卡 / 卡号 /
  QQ / 微信 / 身份证 / 电话 / 会员号）的；前后没有单号字眼、又是身份证号或过 Luhn 校验的银行卡号
  形态的（复核 L3-4）；型号的两类写法（品牌驼峰「iPhone15」、紧跟品类名词「RTX4090显卡」）——
  紧挨着单号字眼的不算型号（「订单号 A1001 手机上显示已签收」，复核 L2-2）。
  紧挨单号字眼的优先，其余取原文里第一个；NFKC 后字母转大写。
  p15 T185：边界只按字母数字串判（与中文 / 商品名紧贴照样切开，C6）；「#」「No.」「单号：」一类前缀
  过 ``identity.normalize_display_no`` 去掉（与绑定写入、核验同一个函数，C7）；紧挨强单号字眼的
  4–7 位纯数字也认；尾号（:func:`extract_order_tail`，只作提示）、金额、数量、日期、门牌楼层不认（C8）。
* ``request``（主会话裁定 R2，回到 3831987 的口径）：诉求闭集 refund / return / exchange / track /
  other。句子里有退款 / 退货 / 换货 / refund / return / exchange 这类诉求词就给对应的诉求，**除非**
  (a) 是问规则的问法（怎么 / 流程 / 能不能 / ……吗 / 多久 / 几天 / 什么时候 / how / when / 问号）
  且诉求词前面没有行动标记（我要 / 帮我 / 申请 / 给我 / 想 / 麻烦 / 马上 / 赶紧 / now / please），
  这时不给 —— 否则前台会为一句政策问题去要单号；或 (b) 是**客户自己撤回**（「不用 / 不要 /
  不需要 / 不想 / 先不 + 诉求词」「诉求词 + 就不用了 / 算了」「撤销 / 撤回 + 诉求词」、英文 no longer
  want / never mind / changed my mind），且前面不是在质问或转述商家不退（为什么 / 凭什么 / 怎么又 /
  商家 / 店家 / 你们 + 不退）、不是店家一直不办（还 / 一直 / 迟迟 + 不退）、不是 A 不 A 的催问
  （退不退款）、不是第三方撤的（系统 / 谁 / 被）；裸的「不 + 诉求词」只在客户这一方开口时算
  （复核 L3-1）—— 撤回的那一段不算诉求，本轮没有别的诉求就给 ``other``（槽位跨轮合并、
  新值覆盖旧值，给空就盖不掉上一轮的 refund）。明说要办的分句优先（「东西一直没收到，我要退款」
  是要退款，复核 L2-1；同一分句里先抱怨没到、后明说要办的「都没收到货给我退款」也是，复核 L2-3）；
  问进度（到没到 / 发了没）是 track，哪怕句子里有「退款」二字。
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

from maos.core.store import record_model_failure, record_model_usage
from maos.domain.cs import scripts, triggers
from maos.domain.cs.identity import normalize_display_no
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

    ``source`` 说的是**意图**从哪来：``rule``（触发词 / 诉求 / 词表，含判不出的 unknown）或
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
# 订单号（主会话裁定 R3：短形态无上下文也认）
# ---------------------------------------------------------------------------
#: 纯数字 ≥ 8 位（淘宝 / 京东 / 抖店一类）。
_DIGITS_RE = re.compile(r"(?<![0-9A-Za-z])\d{8,}(?![0-9A-Za-z])")
#: 带连字符的串（拼多多「200924-1234567890」、自编号「SO-2026-000123」、日期、电话都长这样，下面分辨）。
_HYPHEN_RE = re.compile(r"(?<![0-9A-Za-z-])[0-9A-Za-z]+(?:-[0-9A-Za-z]+)+(?![0-9A-Za-z-])")
#: 字母开头、字母数字混排的串（「A1001」「AB12345」「JD20260001」）：总长够 :data:`_ALNUM_MIN_LEN`、
#: 数字够 :data:`_ALNUM_MIN_DIGITS` 位才认（型号、制式常常只带一两位数：Mate60、wifi6、Note12）。
#: p15 T185（C7）：前面紧挨「#」也认（「order #A1001」「订单号#A1001」）—— 「#」是前缀，不是单号的一部分，
#: 抽出来的值过 ``identity.normalize_display_no`` 去掉。边界只看字母数字串（C6：与中文紧贴也照样切得开）。
_ALNUM_RE = re.compile(r"(?<![0-9A-Za-z-])[A-Za-z][A-Za-z0-9]*\d[A-Za-z0-9]*(?![0-9A-Za-z-])")
_ALNUM_MIN_LEN = 5
_ALNUM_MIN_DIGITS = 3
#: 「#」+ 数字（「订单 #1001」）：太短，只在紧挨着单号字眼时认。
_HASH_RE = re.compile(r"(?<![0-9A-Za-z#])#\d{3,}(?![0-9A-Za-z])")
#: p15 T185（C6）：4–7 位的短纯数字，只在**紧挨着强单号字眼**（订单号 / 单号 / 订单 / order no. /
#: No. / #，见 :data:`_STRONG_ORDER_WORD_RE`）时认（「单号8812093前天拍的」）。「这单 / 那单」不算强字眼。
_SHORT_DIGITS_RE = re.compile(r"(?<![0-9A-Za-z])\d{4,7}(?![0-9A-Za-z])")
_STRONG_ORDER_WORD_RE = re.compile(
    r"订单(?:编?号|号码)?|单号|(?<![a-z])order(?:\s*(?:no\.?|number|num|id))?(?![a-z])"
    r"|(?<![a-z])no\s*[.:#]")
#: p15 T185（C8）：尾号 —— 「尾数 / 尾号 / 后四位 / last four」后面的数字只是单号（或卡、手机）的
#: 末几位，不是单号，不拿去查单（可以当提示，见 :func:`extract_order_tail`）。
_TAIL_WORD_RE = re.compile(
    r"(?:尾数|尾号|末尾|末[三四五六几3-6]位|后[三四五六几3-6]位|最后[三四五六几3-6]位"
    r"|(?<![a-z])last\s*(?:[3-6]|three|four|five|six)(?:\s*digits?)?|(?<![a-z])ending\s*(?:in|with))"
    r"[\s:是为]*$")
_TAIL_DIGITS_RE = re.compile(r"(?<![0-9A-Za-z])\d{3,6}(?![0-9A-Za-z])")
#: p15 T185（C8）：8 位纯数字、恰好是合法的「年月日」（19xx / 20xx 年、01–12 月、01–31 日）→ 日期，
#: 不当单号（紧挨单号字眼的除外）。
_YMD_RE = re.compile(r"^(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])$")
#: p15 T185（C8）：纯数字后面紧跟单位 → 金额 / 数量 / 日期 / 门牌楼层，不是单号。
#: 复核 L2-1：单位字常常也是普通词的第一个字（包裹、平台、天猫、米家、点了、条码、月底、周末、台灯、
#: 「XXX号订单」），这些词排除在外 —— 单位只认单位本身。
_DIGIT_UNIT_AFTER_RE = re.compile(
    r"\s*(?:元|块|毛|角|rmb|yuan|dollars?|usd|件|个|台(?![灯式面历风湾州])|只|双|套|箱|包(?![裹邮装])"
    r"|瓶|盒|条(?!码|形码)|张|份|斤|克|千克|公斤|kg"
    r"|毫升|ml|米(?![家色白])|公里|km|平(?![台板时安])|室|房(?![东子])|楼(?![下上道梯])|层(?!层)"
    r"|栋|幢|座|单元|号(?!码|订?单)|年(?![货])|月(?![底初中末])|日(?![期志])|天(?![猫])"
    r"|点(?![了击开进错])|分钟|小时|周(?![末边一二三四五六日天])"
    r"|pcs|pieces|units|items)(?![a-z])")
#: 字母开头的串后面紧跟门牌 / 楼层单位（「B1203室」）→ 房号，不是单号（同上，楼下 / 房东一类普通词除外）。
_ALNUM_UNIT_AFTER_RE = re.compile(r"\s*(?:室|房(?![东子])|楼(?![下上道梯])|层(?!层)|栋|幢|单元)")
#: 纯数字前面紧挨货币记号 / 金额字眼（「¥12345678」「金额 199」）→ 金额。
_MONEY_BEFORE_RE = re.compile(r"(?:[¥$]|rmb|人民币|金额|价格|价钱|花了|付了|退了|扣了)\s*$")
#: 候选串前面的上下文字眼（复核 L2-6 / L3-4）：**离候选串最近的那一个**说了算 ——
#: 说单号的（订单号 / 单号 / 那单 / order #）与说别的号的（卡号 / QQ / 微信 / 身份证 / 电话 / 会员号）。
#: 在 NFKC、小写、保留空白的文本上找，只看候选串前面 :data:`_CONTEXT_WINDOW` 个字符。
_ORDER_WORD_RE = re.compile(
    r"订单(?:编?号|号码)?|单号|那一?单|这一?单|单子|我的单|下的单|拍的单"
    r"|(?<![a-z])order(?:\s*(?:no\.?|number|num|id|#))?(?![a-z])|(?<![a-z])no\s*[.:#]")
_OTHER_NUMBER_RE = re.compile(
    r"银行卡|卡号|信用卡|储蓄卡|借记卡|这张卡|qq|微信|身份证|证件|护照|工号|会员|账号|账户|支付宝"
    r"|手机|电话|座机|热线|门牌|房号|房间|室号"
    r"|(?<![a-z])(?:card|account|wechat|passport|phone|mobile|tel|id)(?![a-z])")
_CONTEXT_WINDOW = 12
#: 紧挨着单号字眼：中间只许空白、冒号、「#」、「是 / 为」。
_ADJACENT_GAP_RE = re.compile(r"[\s:#是为]*")
#: 候选串后面紧跟着的单号字眼（「A1001 这单到哪了」「A1001 的订单」）。
_FOLLOWING_ORDER_RE = re.compile(r"\s*(?:(?:这|那)一?单|的?订单|order(?![a-z]))")
#: 不是单号的形态：手机号、日期、400 / 800 热线与座机。手机号与热线按**去掉分隔符与国家码之后**
#: 的数字判（复核 L1-1 / L2-4）：「138-1234-5678」「+8613812345678」「86-138-1234-5678」
#: 「4008123123」都不是单号 —— 手机号当单号存进 cs_slot 就是把个人信息写进槽位表（R5）。
_MOBILE_RE = re.compile(r"^1[3-9]\d{9}$")
_HOTLINE_RE = re.compile(r"^[48]00\d{7}$")
_COUNTRY_CODE_RE = re.compile(r"^(?:00)?86(?=1[3-9]\d{9}$)")
#: 座机不带分隔符的写法：0 + 区号 + 七八位号（「057188886666」）。平台单号不以 0 开头。
_LANDLINE_DIGITS_RE = re.compile(r"^0[1-9]\d{8,10}$")
_DATE_RE = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}$")
_PHONE_RE = re.compile(r"^(?:[48]00-\d{3,4}-\d{3,4}|0\d{2,3}-\d{7,8}(?:-\d{1,6})?)$")
#: 座机的另两种写法：区号带括号（「(0571)88886666」）、区号与号码之间是空格（「021 62345678」）。
#: 这一段里的数字都不当单号。
_LANDLINE_SPAN_RE = re.compile(r"[(（]\s*0\d{2,3}\s*[)）]\s*\d{7,8}(?!\d)|(?<!\d)0\d{2,3}\s+\d{7,8}(?!\d)")
#: 连字符串（数字开头的）至少要有这么多位数字（再短就像日期、型号）。
_HYPHEN_MIN_DIGITS = 6
#: 型号不是单号（复核 L2-6）。按两类一般写法认，不逐个列型号：
#: * 品牌的驼峰写法：同一段字母里小写紧跟大写（「iPhone15」「AirPods3」「MacBook2024」）——
#:   平台单号是单一大小写的；
#: * 紧跟一个品类名词（「RTX4090显卡」「MX5000 鼠标」「X1 Carbon 笔记本」不在此列）：
#:   见 :data:`MODEL_FOLLOWERS`。
_CAMEL_RE = re.compile(r"[a-z][A-Z]")
MODEL_FOLLOWERS: tuple[str, ...] = (
    "显卡", "手机", "耳机", "主板", "处理器", "芯片", "相机", "镜头", "手表", "电脑", "笔记本", "平板",
    "鼠标", "键盘", "显示器", "路由器", "音箱", "充电器", "电视", "投影仪", "硬盘", "内存", "型号",
    "机型", "款", "系列",
)
_MODEL_FOLLOWER_RE = re.compile(r"\s*(?:" + "|".join(map(re.escape, MODEL_FOLLOWERS)) + ")")
#: 分句开头：前面什么都没有，或只有标点 / 空白。
_CLAUSE_START_RE = re.compile(r"(?:^|[，,。.!！?？;；~～:：])\s*$")
#: 品类名词后面紧跟的问题说法（坏了 / 碎了 / 发错了……）。
_DEFECT_AFTER_RE = re.compile(
    r"\s*(?:的)?(?:屏幕|外壳|壳子?|包装|盒子)?(?:坏|碎|裂|破|烂|断|不亮|不响|没声|没反应|开不了机|开不机|充不进|充不上"
    r"|用不了|不能用|有问题|出问题|有瑕疵|划痕|发错|错发|少发|漏发|质量)")
#: 品类名词后面紧跟物流状态（复核 L3-1：只给「隔着空白」那一路用）。
_LOGISTICS_AFTER_RE = re.compile(
    r"\s*(?:的)?(?:怎么|咋)?(?:还|一直|到现在)?(?:没|未|不)?(?:有)?(?:发货|发出|到哪|到了没|物流|快递|签收|派送|揽收)")
#: 分句里说商品的引语：有它就是在说买的东西是什么型号，不是报单号。
_BUYER_PREAMBLE_RE = re.compile(r"买的|买了|我的|请问|想问|问下|问一下|咨询|这款|那款|同款")


def _is_phone_shaped(token: str) -> bool:
    """去掉分隔符与国家码（86 / 0086）后是手机号、400 / 800 热线，或不带分隔符的座机。"""
    digits = token.replace("-", "")
    if not digits.isdigit():
        return False
    digits = _COUNTRY_CODE_RE.sub("", digits)
    return bool(_MOBILE_RE.match(digits) or _HOTLINE_RE.match(digits)
                or _LANDLINE_DIGITS_RE.match(digits))


#: 18 位身份证形态：6 位地区 + 19xx / 20xx 年 + 合法月日 + 3 位 + 校验位（数字；带 X 的本来就不是纯数字）。
_ID_CARD_RE = re.compile(r"^\d{6}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{4}$")
#: 银行卡形态：62（银联）/ 4 / 5 开头、16–19 位，且过 Luhn 校验。
_BANK_CARD_RE = re.compile(r"^(?:62|4|5)\d{14,17}$")


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _is_personal_number(token: str) -> bool:
    """身份证号或银行卡号的形态（复核 L3-4）：没有单号上下文时不当单号，理由同手机号（R5）。"""
    return bool(_ID_CARD_RE.match(token)
                or (_BANK_CARD_RE.match(token) and _luhn_ok(token)))


def _context_before(text: str, start: int) -> str:
    """候选串前面最近的上下文字眼是哪一类：``order`` / ``other`` / ``""``（窗口里都没有）。"""
    window = text[max(0, start - _CONTEXT_WINDOW):start].lower()
    best, kind = -1, ""
    for pattern, label in ((_ORDER_WORD_RE, "order"), (_OTHER_NUMBER_RE, "other")):
        for m in pattern.finditer(window):
            if m.end() > best or (m.end() == best and label == "order"):
                best, kind = m.end(), label
    return kind


def _adjacent_order_word(text: str, start: int, end: int) -> bool:
    """候选串紧挨着单号字眼：前面是「订单号：」「那单 」「order 」，或后面是「这单」「的订单」。"""
    before = text[max(0, start - _CONTEXT_WINDOW):start].lower()
    for m in _ORDER_WORD_RE.finditer(before):
        if _ADJACENT_GAP_RE.fullmatch(before[m.end():]):
            return True
    return bool(_FOLLOWING_ORDER_RE.match(text[end:].lower()))


def _alnum_shaped(alnum: str) -> bool:
    """字母开头的串（已去掉连字符）够长、数字够多。"""
    return len(alnum) >= _ALNUM_MIN_LEN and sum(c.isdigit() for c in alnum) >= _ALNUM_MIN_DIGITS


def _is_model_number(text: str, token: str, start: int, end: int) -> bool:
    """型号的两类写法：驼峰（iPhone15）、紧跟品类名词（RTX4090显卡）。

    紧挨着单号字眼的不算型号（复核 L2-2）：「订单号 A1001 手机上显示已签收」「我那单 E5005 耳机坏了」
    里后面的「手机 / 耳机」是在说这单买的东西，单号字眼说了算。只是**窗口里有**单号字眼不够
    （「我订单里的RTX4090显卡还没发」照旧是型号）。
    """
    if _adjacent_order_word(text, start, end):
        return False
    if _CAMEL_RE.search(token):
        return True
    follower = _MODEL_FOLLOWER_RE.match(text[end:])
    if follower is None:
        return False
    # p15 T185（C6）：品类名词跟单号之间隔着空白（「XX1234 耳机坏了」）是在报单号、再说这单买的东西；
    # 型号是跟品类名词写成一个词的（「RTX4090显卡」）。写成一个词、但串在分句开头、品类名词后面
    # 紧跟着坏了 / 碎了一类问题（「XX1234耳机坏了」）也是在报单号 —— 说型号的人会先说「我买的」。
    # 复核 L3-1：「隔着空白」本身不够（「我买的 GTX1660 显卡能七天无理由退吗」是在问政策、说型号）。
    # 隔着空白时，还要品类名词后面紧跟问题（坏了 / 碎了）或物流状态（还没发货 / 到哪了），且这一分句里
    # 前面没有「我买的 / 请问 / 想问」一类说商品的引语。
    after = end + len(follower.group(0))
    if follower.group(0)[:1].isspace():
        return not ((_DEFECT_AFTER_RE.match(text, after) or _LOGISTICS_AFTER_RE.match(text, after))
                    and not _BUYER_PREAMBLE_RE.search(_clause_before(text, start)))
    return not (_CLAUSE_START_RE.search(text[:start]) and _DEFECT_AFTER_RE.match(text, after))


def _clause_before(text: str, start: int) -> str:
    """候选串所在分句里、它前面的那一段。"""
    head = text[:start]
    cut = max(head.rfind(p) for p in "，,。.!！?？;；~～")
    return head[cut + 1:]


def _order_candidates(text: str) -> list[tuple[int, int, str]]:
    """原文里所有像单号的串：(起点, 终点, 原样)，按起点排。

    认三种形态（主会话裁定 R3）：纯数字 ≥ 8 位；字母开头、字母数字混排、总长 ≥ 5（带不带连字符都行：
    「A1001」「AB12345」「SO-2026-0001」）；数字开头带连字符、至少 6 位数字的平台单号。另有「# + 数字」
    只在紧挨单号字眼时认。

    不认：手机号（含 +86 / 86 前缀、带连字符或空格）、座机（带连字符、括号、空格或不带分隔符）、
    400 / 800 热线、日期 —— 被排除的那一段里的数字段也不再另当单号（「0571-88886666」排除了整串，
    后半截「88886666」不另算）；离候选串最近的上下文说的是别的号（卡号 / QQ / 身份证 / 电话……）的；
    前后没有单号字眼、又是身份证号或银行卡号形态的（复核 L2-6 / L3-4）；型号（:func:`_is_model_number`）。
    """
    found: list[tuple[int, int, str]] = []
    blocked: list[tuple[int, int]] = [m.span() for m in _LANDLINE_SPAN_RE.finditer(text)]

    def free(start: int) -> bool:
        return not any(s <= start < e for s, e in blocked) \
            and not any(s <= start < e for s, e, _t in found)

    for m in _HYPHEN_RE.finditer(text):
        tok = m.group(0)
        if not free(m.start()):
            continue
        if (_DATE_RE.match(tok) or _PHONE_RE.match(tok) or _is_phone_shaped(tok)
                or _context_before(text, m.start()) == "other"):
            blocked.append(m.span())
            continue
        if tok[0].isalpha():
            ok = _alnum_shaped(tok.replace("-", "")) and not _is_model_number(text, tok, m.start(), m.end())
        else:
            ok = sum(ch.isdigit() for ch in tok) >= _HYPHEN_MIN_DIGITS
        if ok:
            found.append((m.start(), m.end(), tok))
    for m in _DIGITS_RE.finditer(text):
        tok = m.group(0)
        # 复核 L2-1：紧挨单号字眼的长串不做「后面跟单位」判定（「订单号 88120937 包裹没到」），尾号 / 金额照判。
        if (_is_phone_shaped(tok) or not free(m.start())
                or _not_an_order_number(text, m.start(), m.end(),
                                        units=not _adjacent_order_word(text, m.start(), m.end()))):
            continue
        context = _context_before(text, m.start())
        if context == "other":
            continue
        if (context != "order" and _is_personal_number(tok)
                and not _FOLLOWING_ORDER_RE.match(text[m.end():].lower())):
            continue
        if (_YMD_RE.match(tok) and not _adjacent_order_word(text, m.start(), m.end())):
            continue
        found.append((m.start(), m.end(), tok))
    for m in _ALNUM_RE.finditer(text):
        tok = m.group(0)
        if not _alnum_shaped(tok) or not free(m.start()):
            continue
        if _context_before(text, m.start()) == "other" or _is_model_number(text, tok, m.start(), m.end()):
            continue
        adjacent = _adjacent_order_word(text, m.start(), m.end())
        # 复核 L2-4：前面紧挨「#」的串（色号 #FF5733、话题标签）只在紧挨单号字眼时认。
        if m.start() > 0 and text[m.start() - 1] == "#" and not adjacent:
            continue
        # 复核 L2-3：紧挨单号字眼的不做房号判定（「订单号A1001楼下签收的」）。
        if not adjacent and _ALNUM_UNIT_AFTER_RE.match(text, m.end()):
            continue
        found.append((m.start(), m.end(), tok))
    for m in _HASH_RE.finditer(text):
        if free(m.start()) and free(m.start() + 1) and _adjacent_order_word(text, m.start(), m.end()):
            found.append((m.start(), m.end(), m.group(0)))
    for m in _SHORT_DIGITS_RE.finditer(text):
        if (free(m.start()) and _strong_order_word_before(text, m.start())
                and not _not_an_order_number(text, m.start(), m.end())):
            found.append((m.start(), m.end(), m.group(0)))
    return sorted(found)


def _strong_order_word_before(text: str, start: int) -> bool:
    """候选串前面紧挨着强单号字眼（订单号 / 单号 / 订单 / order no. / No.），中间只许 :data:`_ADJACENT_GAP_RE`。"""
    before = text[max(0, start - _CONTEXT_WINDOW):start].lower()
    return any(_ADJACENT_GAP_RE.fullmatch(before[m.end():])
               for m in _STRONG_ORDER_WORD_RE.finditer(before))


def _tail_word_before(text: str, start: int) -> bool:
    """候选串前面紧挨着尾号字眼（尾数 / 尾号 / 后四位 / last four……）。"""
    return bool(_TAIL_WORD_RE.search(text[max(0, start - 2 * _CONTEXT_WINDOW):start].lower()))


def _not_an_order_number(text: str, start: int, end: int, *, units: bool = True) -> bool:
    """纯数字串的 C8 反例（p15 T185）：尾号、金额（前面是货币记号 / 金额字眼）、后面紧跟单位
    （金额 / 数量 / 日期 / 门牌楼层；``units=False`` 时不看这一项）。"""
    before = text[max(0, start - _CONTEXT_WINDOW):start].lower()
    return bool(_tail_word_before(text, start) or _MONEY_BEFORE_RE.search(before)
                or (units and _DIGIT_UNIT_AFTER_RE.match(text[end:].lower())))


def extract_order_no(text: str) -> str:
    """订单号：见模块头「槽位」。紧挨单号字眼的优先，其余取原文里第一个；取不到返回空串。

    抽出来的串过 :func:`maos.domain.cs.identity.normalize_display_no`（与绑定写入、核验同一个函数，
    p15 契约 §1 C7），「#」一类前缀不进槽位；再转大写。「№」在 NFKC 下会变成贴着数字的「No」，
    所以先写成「No.」（前缀字眼）再规范化。
    """
    s = _nfkc((text or "").replace("№", "No."))
    candidates = _order_candidates(s)
    if not candidates:
        return ""
    adjacent = [c for c in candidates if _adjacent_order_word(s, c[0], c[1])]
    return normalize_display_no((adjacent or candidates)[0][2]).upper()


def extract_order_tail(text: str) -> str:
    """尾号提示（p15 契约 §1 C8）：「尾数 / 尾号 / 后四位 / last four」后面的 3–6 位数字；没有返回空串。

    **只是提示**：不进 ``order_no`` 槽位、不拿去核验与查单（尾号对不上唯一一单）。槽位键是冻结的
    ``ports.SLOT_KEYS``，本期不给它开新槽位；前台要用（例如写进转人工卡片）由调用方决定。
    """
    s = _nfkc(text)
    for m in _TAIL_DIGITS_RE.finditer(s):
        if _tail_word_before(s, m.start()):
            return m.group(0)
    return ""


# ---------------------------------------------------------------------------
# 诉求（主会话裁定 R2：回到 3831987 的口径 —— 有诉求词就给，问规则的与客户撤回的除外）
# ---------------------------------------------------------------------------
#: 诉求词表：诉求 → (中文片段, 英文片段)。先后即优先级（同一句中了几种取靠前的）。
REQUEST_WORDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    REQUEST_EXCHANGE: (
        (r"换货", r"换一(?:个|件|双|台|条|套|只|支|瓶|盒)", r"换个(?:新的|颜色|尺码|码|型号|大的|小的)",
         r"换(?:颜色|尺码|大一码|小一码|大一号|小一号|码)", r"换个?(?:大|小)一?[码号]", r"调换",
         r"更换(?:商品|新的|一个)",
         # 换 + 尺寸的形容词（换长一点的、换宽松些、换个短一点）
         r"换(?:个|件|条|双)?更?[大小长短宽窄厚薄松紧肥瘦深浅]{1,2}一?(?:点|些|码|号)"),
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

# ---- 问规则的标记（R2 (a)）：问办法 / 条件 / 谁出、是非问、问时长 ------------------------------
#: 问办法、条件、规则。
_HOWTO_ZH = _zh_re((r"怎么", r"怎样", r"如何", r"咋", r"流程", r"步骤", r"条件", r"规则", r"要求",
                    r"政策", r"规定", r"谁出", r"谁来出", r"我出", r"你们出", r"谁承担", r"谁付", r"在哪",
                    r"哪里", r"哪儿", r"入口", r"需要什么", r"要什么", r"什么情况", r"哪些",
                    # p14 T182（误判类别 3）：问先后顺序的选择问（「要先申请还是直接寄回来」「第一步干嘛」）
                    r"先.{0,10}还是", r"还是先", r"先后", r"顺序", r"第一步",
                    # 问退到哪个账户（「退款退回哪个账户」「退到哪」；「到哪了」是查进度，线索先判）
                    r"哪个", r"哪种", r"退(?:到|回)哪(?![了一])"))
_HOWTO_EN = _en_re((r"how", r"what(?:'s| is| are)? (?:the|your) (?:policy|process|rules?)", r"policy",
                    r"process", r"who pays", r"which"))
#: 是非问（能退吗 / 可以换吗 / 会退运费吗 / 有手续费吗）。
_YESNO_ZH = _zh_re((r"能不能", r"可不可以", r"能否", r"是否", r"有没有", r"会不会", r"要不要", r"需不需要",
                    r"是不是", r"可以吗", r"能吗", r"行吗", r"支持吗", r"可以不",
                    r"能.{0,6}[吗嘛么不][?!.~。]*$", r"可以.{0,6}[吗嘛么不][?!.~。]*$",
                    # 分句末的疑问语气词
                    r"[吗嘛么](?=$|[?？!！。.,，~～…;；])"))
#: 英文的问句：分句以助动词 / 疑问词开头，或带问号。
_YESNO_EN = re.compile(r"(?:^|[,.;!?] ?)(?:is|are|do|does|did|can|could|will|would|should|may|am|when|what|"
                       r"which|why|who)(?![a-z0-9])|\?")
#: 行动标记（R2 (a)）：有它，问句也是在要办。中文的要出现在诉求词**前面**（「我想退货怎么弄」是要退；
#: 「退货我要付运费吗」是在问）；「想问 / 想知道 / 麻烦问下 / 麻烦了」是问，不是办。
_ACTION_ZH = _zh_re((r"帮我", r"给我", r"替我", r"帮忙", r"请帮", r"我要(?!问|咨询)", r"我想要", r"我需要",
                     r"要求", r"申请", r"办理", r"办一下", r"处理一下", r"直接", r"马上", r"赶紧", r"立刻",
                     r"立即", r"尽快", r"快点", r"麻烦(?!问|咨询|请教|了|你了|您了)",
                     r"(?<!不)想(?!问|知道|了解|咨询|确认|请教|看|查|打听)"))
_ACTION_EN = _en_re((r"i want", r"i'd like", r"i would like", r"(?<!do )i need(?! to know)", r"help me",
                     r"please(?! (?:tell|let me know|advise|explain|confirm))", r"pls", r"now", r"asap",
                     r"right away", r"immediately"))
#: 诉求词后面跟着「后 / 以后 / 之后」是个时间点（「申请退款后钱什么时候到」），前面的办事字眼不算。
_TIME_POINT_RE = re.compile(r"了?(?:以|之)?后")

#: 进度线索要落在「订单的事」上才算查进度（「你们店开了多久」「怎么样了」闲聊不算）。
_TRACK_TOPIC_ZH = _zh_re((r"单", r"货", r"快递", r"物流", r"包裹", r"东西", r"钱", r"款", r"售后",
                          r"申请", r"发票", r"寄"))
_TRACK_TOPIC_EN = _en_re((r"order", r"package", r"parcel", r"refund", r"money", r"item",
                          r"shipment", r"delivery", r"return", r"exchange"))


def _is_policy_question(zh: str, en: str, cue: str) -> bool:
    """问规则的问法：问办法 / 条件、是非问、问时长（多久 / 几天 / 什么时候）。"""
    return bool(_HOWTO_ZH.search(zh) or _HOWTO_EN.search(en) or _YESNO_ZH.search(zh)
                or _YESNO_EN.search(en) or cue == scripts.CUE_DURATION)


def _has_action(zh: str, en: str, request_at: tuple[int, int] | None) -> bool:
    """句中有明确的行动标记。中文的要在诉求词前面，且诉求词后面不是「后 / 以后」这个时间点。"""
    if _ACTION_EN.search(en):
        return True
    if request_at is None:
        return bool(_ACTION_ZH.search(zh))
    start, end = request_at
    if _TIME_POINT_RE.match(zh, end):
        return False
    return any(m.start() < start for m in _ACTION_ZH.finditer(zh))


# ---- 明说要办（复核 L2-1）：「我要退款」「帮我把这单退了」「I want a refund」 ----------------
#: 办事的字眼**紧挨着**（中间至多几个不是「查 / 看 / 问」的字）退款 / 退货 / 换货的说法，且同一分句里
#: 没有进度或时长线索、不是在问规则：这是客户明说要办，诉求就是它 —— 前一分句在催（「东西一直没收到，
#: 我要退款」）、后一分句在问规则（「我要退货，运费谁出」）都不改。「申请退款后……」「退款以后……」
#: 里的办事是个时间点，不算。
_EXPLICIT_ACTION_ZH = (r"(?:帮我|给我|替我|帮忙|麻烦你?帮?我?|请帮?我?|我要|(?<!不)我?想要?|(?<![不想])要|我需要|要求"
                       r"|申请|办理|直接|马上|赶紧|立刻|立即|尽快|快点)")
#: 办事字眼与诉求说法之间允许的字（不许夹「查 / 看 / 问 / 知道」：「帮我查下退款」是在查）。
_EXPLICIT_GAP_ZH = r"[^查看问催知解确认，,。.!！?？;；]{0,5}?"
_EXPLICIT_CORE_ZH: dict[str, tuple[str, ...]] = {
    REQUEST_EXCHANGE: (r"换货", r"换一(?:个|件|双|台|条|套|只|支|瓶|盒)", r"换个?(?:新的|颜色|尺码|码|型号|大|小)",
                       r"换(?:颜色|尺码|码|尺寸)", r"调换", r"更换",
                       r"换(?:个|件|条|双)?更?[大小长短宽窄厚薄松紧肥瘦深浅]{1,2}一?(?:点|些|码|号)"),
    REQUEST_RETURN: (r"退货", r"退回去", r"退掉", r"退换", r"寄回(?!来)", r"(?<!钱)退(?![款钱费宽到回休出])"),
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

    只看**同一分句**里既没有进度 / 时长线索、也不是在问规则（怎么 / 流程 / 能不能 / ……吗）的那些分句：
    「我想退货怎么弄」「给我退了没」「申请退款后钱什么时候退回来」都不算**明说**要办（前一句另由
    :func:`extract_request` 的行动标记认出来）。
    """
    for clause in _CLAUSE_SPLIT_RE.split(_nfkc(text)):
        if not clause.strip():
            continue
        cue = scripts.detect_cue(clause)
        if cue == scripts.CUE_DURATION:
            continue
        zh, en = _compact(clause), _spaced(clause)
        if (_HOWTO_ZH.search(zh) or _HOWTO_EN.search(en)
                or _YESNO_ZH.search(zh) or _YESNO_EN.search(en)):
            continue
        # 进度线索只挡住它**后面**没有办事说法的分句（复核 L2-3）：「都没收到货给我退款」是先抱怨、
        # 再明说要办；「给我退了没」的线索在办事说法后面，是在查
        zh_after = _after_progress(zh) if cue else zh
        en_after = _after_progress(en) if cue else en
        for key in (REQUEST_EXCHANGE, REQUEST_RETURN, REQUEST_REFUND):
            if _EXPLICIT_ZH_RES[key].search(zh_after) or _EXPLICIT_EN_RES[key].search(en_after):
                return key
    return ""


def _after_progress(text: str) -> str:
    """最后一处进度线索后面的那截（:data:`scripts.INTENT_CUES` 的 progress 那一类）；没有线索原样返回。"""
    last = max((m.end() for m in scripts.PROGRESS_CUE_RE.finditer(text)), default=0)
    return text[last:]


# ---- 客户撤回（R2 (b)）：「不用退款了」「退款的事就算了」「撤销退款申请」「I no longer want a refund」 ----
#: 客户以第一人称或无主语说的撤回。撤回的那一段先换成分句边界再判诉求（「不要退款，给我换一件」照样
#: 是换货）；撤回之后本轮没有别的诉求时，诉求给 ``other``（不是空）—— 槽位跨轮合并、新值覆盖旧值，
#: 给空就盖不掉上一轮的 refund，前台照旧会去出退款预检卡。
_WITHDRAW_CORE = r"(?:退款|退货|换货|退钱|退换货?)"
WITHDRAWN_REQUEST_ZH: tuple[str, ...] = (
    # 不用 / 不要 / 不需要 / 不想 / 先不 / 别 +（再）+（给我 / 帮我 / 申请）+ 诉求词。中间不许夹「了」：
    # 「不要了 / 不想要了」自己就是退货的说法（「这件衣服不想要了退货」是要退，复核 L3-1）
    r"(?:不用|不要|不需要|不想要?|不打算|无需|没必要|不必|先不|暂时不|暂不|别)(?:再)?(?:给我|帮我|申请|办)?"
    r"(?:退款|退货|换货|退钱|退换货?|退|换)",
    # 裸的「不 + 诉求词」（不带 用 / 要 / 需要 / 想）：+ 了（这单我不退了、算了不换了），或在分句末
    # （我改主意了，不退货）。只在客户这一方开口时算，见 :data:`_OWN_SUBJECT_RE`
    r"不(?:退款|退货|换货|退钱|退|换)(?:了|啦|咯)",
    r"不(?:退款|退货|换货|退钱)(?=$|[，,。.!！~～…;；])",
    # 诉求词（的事 / 申请）+ 就不用了 / 算了 / 取消吧 / 撤回了（「退货不要运费吗」不是：后面还有字）。
    # 「已经」只许跟在「我」后面：「退款申请已经取消了」是在说状态，谁取消的不知道（复核 L3-1）
    _WITHDRAW_CORE + r"(?:的事|这事|申请)?(?:我(?:已经)?)?(?:就|也|先|还是)?"
    r"(?:不用|不要|不需要|不办|算了|不必|取消|撤销|撤回)(?:了|吧|掉|啦)*"
    r"(?=$|[，,。.!！~～…;；?？ ]|谢|多谢|感谢|[哈啊哦呢嗯])",
    # 撤销 / 撤回 / 取消 + 诉求词（申请）
    r"(?:撤销|撤回|取消)(?:掉)?(?:这个|这笔|我的|那个)?" + _WITHDRAW_CORE + r"(?:申请)?",
)
WITHDRAWN_REQUEST_EN: tuple[str, ...] = (
    # 主语是客户自己（I / we don't want / no longer need …、I'm not going to …、no need to / for …）：
    # 「you don't refund」「they won't refund me」是在怪店家不退，不是撤回
    r"(?:(?:i|we)(?: really| just)? (?:don't|do not|dont|no longer) (?:want|need|wanna)"
    r"|(?:i'm|i am|we're|we are) not going|no need)(?: to| for)?"
    r"(?: (?:get|have|request|make|do|process))?(?: (?:a|an|the|my|this|it|any))*"
    r" (?:refunds?|returns?|exchanges?|send (?:it |this |them )?back)",
    r"no (?:refunds?|returns?|exchanges?) (?:needed|necessary|required|please)",
    r"(?:refunds?|returns?|exchanges?) (?:is |are )?(?:not|no longer) (?:needed|necessary|required)",
    r"cancel (?:my |the |this )?(?:refund|return|exchange)(?: request)?",
    # never mind / changed my mind：诉求词要是它的**宾语**（never mind the refund、changed my mind about
    # the return），或紧跟着「no + 诉求词」（changed my mind, no refund）。「Never mind, just refund me」
    # 「changed my mind about the color, can I get a refund」里的诉求词不是它撤的（复核 L3-1）
    r"(?:never ?mind|changed my mind)(?: (?:about|on))?(?: (?:the|my|this|that|a|an))* (?:refunds?|returns?|exchanges?)",
    r"(?:never ?mind|changed my mind)[,;.]? no (?:refunds?|returns?|exchanges?)",
)
_WITHDRAWN_ZH_RE = _zh_re(WITHDRAWN_REQUEST_ZH)
_WITHDRAWN_EN_RE = _en_re(WITHDRAWN_REQUEST_EN)
#: 质问 / 转述商家不退、第三方撤的（R2 (b)）：撤回的说法前面（同一分句）有这些，就不是客户自己撤回
#: （「为什么不退了」「凭什么不退款了」「商家说不退了」「卖家说退款不用了」「店家不换了怎么办」
#: 「系统自动取消退款申请了」「谁撤销我的退款申请」「被取消退款了」）。
_BLAME_ZH_RE = re.compile(r"为什么|为啥|凭什么|怎么|咋|商家|店家|卖家|客服|他们|她们|平台|厂家|店铺|老板|说好"
                          r"|系统|自动|谁|被")
#: 「你们 + 不退」是在怪店家；「你们不用退了」是客户在放弃 —— 「你们」只在紧挨着直接拒绝时算质问。
_BLAME_YOU_ZH_RE = re.compile(r"你们(?:就|都|也|还|又|竟然|居然)?\s*$")
#: 前一分句是第三方在说（「商家说了，不退了」）。
_REPORTED_ZH_RE = re.compile(r"(?:商家|店家|卖家|客服|他们|平台|厂家|老板)[^，,。.!！?？]{0,4}说[^，,。.!！?？]*[，,]\s*$")
#: 「不」前面紧挨着抱怨的副词：说的是店家一直不办（「都三天了还不退款」「迟迟不退款」「到现在都不退」），
#: 不是客户撤回（复核 L3-1）。「还是不退了」是客户拿主意，「我一直不想退」主语是客户，都不算。
_COMPLAINT_ADVERB_RE = re.compile(
    r"(?<!我)(?<!我们)(?:还(?!是)|一直|迟迟|始终|老是|总是|就是|硬是|死活|偏偏|根本|从来|到现在|至今|一再|又"
    r"|仍然?|依然|依旧)(?:都|也|还)?\s*$")
#: 裸的「不 + 诉求词」要是客户这一方开的口：在分句开头，或紧跟着客户这一方的主语 / 拿主意的说法
#: （我 / 我们 / 这单 / 那单 / 这件 / 单号 / 算了 / 改主意了 / 决定 / 那），中间可夹「就 / 也 / 还是 / 先 /
#: 暂时 / 干脆 / 都 / 已经」。「你们不退了是吧」「都三天了不退款」「你们到底退不退款」都不是。
_OWN_SUBJECT_RE = re.compile(
    r"(?:^|我们?|咱们?|这单|那单|这件|那件|这个|那个|[a-z]*\d[a-z0-9-]*|算了|改主意了|想了想|决定|那)\s*"
    r"(?:就|也|还是|先|暂时|干脆|都|已经)?\s*$")
#: 裸的「不 + 诉求词」。
_BARE_NOT_RE = re.compile(r"不(?:退|换)")
#: 条件威胁（T173 终审复核 major-1，task-t174 修）：裸的「不 + 诉求词」后面（隔着「的话」、标点、空白）
#: 紧跟一个后果从句 —— 「不退款，我就去差评」「不退货，我就不收了」「不退钱，这事没完」「不换货，否则
#: 我去消协」。这是「不给我办我就……」的省略说法，诉求还在，不是客户撤回。
#: 后果从句只认**真的后果动作**（差评 / 投诉 / 举报 / 曝光 / 起诉 / 报警 / 找消协 / 拒收 / 不收 ……），
#: 不认裸的「就 / 会 / 去」：「不退款，我就继续用吧」「我就收下了」「我会自己处理」「我去送人」是撤回
#: （T174 复核 L2-1）。
_THREAT_ACT = (r"(?:中?差评|投诉|举报|曝光|起诉|告你们|报警|打\s*(?:12315|110)|12315|消协|工商|维权|仲裁"
               r"|找(?:你们|平台|消协|律师|媒体|工商|记者|领导|老板)|拒收|拒签|不收|发(?:微博|抖音|小红书|网上)"
               # p14 T182：「不退款，我就上网曝光」「……我就去网上说」「……挂网上」「……去黑猫」
               r"|上网|网上|挂网|黑猫)")
_THREAT_TAIL_RE = re.compile(
    r"(?:的话)?[\s，,、:：]*(?:那|那么)?\s*(?:我们?|咱们?)?\s*"
    r"(?:(?:就|会|要|肯定|一定|直接|立刻|马上|去|到|上|给你们|给|打)\s*)*" + _THREAT_ACT +
    r"|(?:的话)?[\s，,、:：]*(?:这事儿?|这件事|这个事)?(?:没完|不算完|跟你们没完)"
    r"|(?:的话)?[\s，,、:：]*(?:否则|不然|要不然|要么|别怪)")
#: 前文里客户自己拿了主意（「我改主意了，不退货，我就自己留着」）：那是撤回，不是威胁。
_OWN_DECISION_RE = re.compile(r"改主意|算了|决定|想了想|想想|还是")


def _conditional_threat(text: str, start: int, matched: str) -> bool:
    """这一处「不 + 诉求词」是条件威胁（见 :data:`_THREAT_TAIL_RE`），不是撤回。

    只认**裸的**「不 + 诉求词」且不带「了 / 啦 / 咯」：「不退款了，我就留着用吧」是客户拿主意（撤回），
    「不用退款，我就自己修」也是（「不用」不是裸的「不」）。前文里客户已经说了拿主意的话也不算。
    """
    if not matched.startswith("不") or len(matched) < 2 or matched[1] not in "退换":
        return False
    if matched[-1] in "了啦咯":
        return False
    if _OWN_DECISION_RE.search(text[:start]):
        return False
    return bool(_THREAT_TAIL_RE.match(text, start + len(matched)))


def _blamed(text: str, start: int, matched: str) -> bool:
    """这一处撤回的说法不是客户自己撤的：前面是在质问 / 转述商家、第三方撤的（:data:`_BLAME_ZH_RE`）、
    「不」前面是抱怨副词（:data:`_COMPLAINT_ADVERB_RE`）、A 不 A 的问法（退不退 / 需不需要）、
    裸的「不 + 诉求词」前面不是客户这一方（:data:`_OWN_SUBJECT_RE`），或者后面紧跟后果从句、
    是条件威胁（:func:`_conditional_threat`）。"""
    if _conditional_threat(text, start, matched):
        return True
    prefix = text[:start]
    clause = _CLAUSE_SPLIT_RE.split(prefix)[-1]
    if _BLAME_ZH_RE.search(clause) or _REPORTED_ZH_RE.search(prefix):
        return True
    if matched.startswith("不"):
        if _COMPLAINT_ADVERB_RE.search(clause):
            return True
        if start > 0 and len(matched) > 1 and text[start - 1] == matched[1]:     # A 不 A
            return True
    if _BARE_NOT_RE.match(matched):
        return (not _OWN_SUBJECT_RE.search(clause)) or bool(_BLAME_YOU_ZH_RE.search(clause))
    return False


def _mask_withdrawn(text: str) -> tuple[str, bool]:
    """把客户撤回诉求的说法换成分句边界（「，」）：(处理后的文本, 有没有撤回)。

    在 NFKC、小写、空白压成一个空格的文本上找（中文片段按字、英文按词），返回的也是这份文本。
    """
    s = _spaced(text)
    spans = [m.span() for m in _WITHDRAWN_ZH_RE.finditer(s) if not _blamed(s, m.start(), m.group(0))]
    spans += [m.span() for m in _WITHDRAWN_EN_RE.finditer(s)]
    if not spans:
        return s, False
    out, last = [], 0
    for start, end in sorted(spans):
        if start < last:
            start = last
        out.append(s[last:start])
        out.append("，")
        last = max(last, end)
    out.append(s[last:])
    return "".join(out), True


def extract_request(text: str) -> str:
    """诉求：见模块头「槽位」。取不到返回空串。

    先把客户撤回诉求的说法（「不用退款了」）换成分句边界；然后：明说要办（:func:`explicit_request`：
    「我要退款」「帮我把这单退了」）→ 就是它，别的分句在催在问都不改；否则查进度（进度线索 + 说的是
    订单的事）→ track，哪怕句子里有「退款」；有诉求词就给对应的诉求，**除非**是问规则的问法（怎么 /
    流程 / 能不能 / ……吗 / 多久 / 什么时候）而句中没有行动标记（我要 / 帮我 / 申请 / 想 / 马上 /
    please ……）。改地址、取消订单这类 ``other`` 问一句「能改吗」就是要改，只有问办法（怎么改）不给。
    撤回过、本轮又没有别的诉求 → ``other``。
    """
    masked, withdrawn = _mask_withdrawn(text)
    got = _request_of(masked)
    if not got and withdrawn:
        return REQUEST_OTHER
    return got


def _request_of(text: str) -> str:
    explicit = explicit_request(text)
    if explicit and not (explicit == REQUEST_EXCHANGE and "address" in scripts.synonym_hits(text)):
        return explicit
    zh, en = _compact(text), _spaced(text)
    kind, at = "", None
    for key, (zre, ere) in _REQUEST_RES.items():
        m = zre.search(zh)
        if m or ere.search(en):
            kind, at = key, (m.span() if m else None)
            break
    if kind == REQUEST_EXCHANGE and "address" in scripts.synonym_hits(text):
        # p14 T182：「地址写错了想换一个」换的是地址，不是换货（改地址那一类，诉求 other）。
        kind = REQUEST_OTHER
        m = _REQUEST_RES[REQUEST_OTHER][0].search(zh)
        at = m.span() if m else None
    cue = scripts.detect_cue(text)
    on_topic = bool(kind or _TRACK_TOPIC_ZH.search(zh) or _TRACK_TOPIC_EN.search(en))
    if cue == scripts.CUE_PROGRESS and on_topic:
        return REQUEST_TRACK
    if not kind and scripts.order_anomaly(text):
        # p14 T182（误判类别 2）：某一单的进度 / 异常说法里没有「发货 / 物流 / 退款」这类诉求词
        # （包裹卡在中转站不动、签收了没收到、拆开碎了、重复扣款）—— 同样是在办这一单，按查进度算。
        return REQUEST_TRACK
    if not kind or kind == REQUEST_TRACK:
        return kind
    if kind == REQUEST_OTHER:
        howto = bool(_HOWTO_ZH.search(zh) or _HOWTO_EN.search(en))
        return "" if howto and not _has_action(zh, en, at) else kind
    if _is_policy_question(zh, en, cue) and not _has_action(zh, en, at):
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
    "坏了", "坏的", "摔坏了", "压坏了", "破了", "破损", "裂了", "裂纹", "裂缝", "裂口", "裂开了", "划痕", "刮花",
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
         r"裂(?:了|纹|缝|开|口|痕)", r"划痕", r"不能用", r"用不了", r"开不了机", r"漏水", r"掉色", r"起球",
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
         r"(?:什么|啥|何|几)时(?:候)?(?:能|可以|才|会|给我|帮我)?[发寄](?=[吗呢呀啊哦]|$|[?!.,。，])",
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

#: 诉求词表里认作换货 / 退货的说法，同样给退换货投一票（复核 L3-3：两张表各说各的，「想换个大的」
#: 诉求词表认得、意图词表不认，就落兜底）。从 REQUEST_WORDS 取，不另抄一份。
_REQUEST_VOTES_ZH: tuple[str, ...] = tuple(
    p for key in (REQUEST_EXCHANGE, REQUEST_RETURN) for p in REQUEST_WORDS[key][0]
    if p not in INTENT_KEYWORDS[INTENT_RETURN_EXCHANGE][0])

_INTENT_ZH_RES = {
    k: [(re.compile(p), 1.0) for p in zh]
    + [(re.compile(p), 1.0) for p in (_REQUEST_VOTES_ZH if k == INTENT_RETURN_EXCHANGE else ())]
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
#: 查的是钱的那几类订单异常（``scripts.SYNONYM_RULES`` 的规则名）。
_MONEY_ANOMALY_RULES = frozenset({"refund_missing", "double_charge", "paid_unpaid"})


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
    if _MONEY_ANOMALY_RULES & set(scripts.synonym_hits(text)):
        # p14 T182：重复扣款、扣了钱订单没付上、退款没到 —— 查的是钱（「付了两次」里没有「钱」字）。
        return INTENT_REFUND_PAYMENT
    money = bool(_TRACK_REFUND_ZH.search(zh) or _TRACK_REFUND_EN.search(en))
    goods = bool(_TRACK_AFTERSALE_ZH.search(zh) or _TRACK_AFTERSALE_EN.search(en))
    if money and goods:
        return INTENT_REFUND_PAYMENT if _TRACK_MONEY_ZH.search(zh) else INTENT_RETURN_EXCHANGE
    if money:
        return INTENT_REFUND_PAYMENT
    if goods or _TRACK_APPLY_ZH.search(zh):
        return INTENT_RETURN_EXCHANGE
    return INTENT_LOGISTICS


def rule_intent(text: str, *, request: str = "") -> str:
    """确定性的意图：触发词 → 本轮诉求 → 关键词。判不出是 unknown。

    诉求在关键词之前（复核 L2-1）：客户明说要办 / 在查自己那一单（「东西一直没收到，我要退款」）时，
    诉求是结构化的信号。p13 第三轮的「意图示例」一步已整张删掉（主会话裁定 R4：在开发集与 T168 holdout
    上测不出增益，见 docs/DECISIONS.md task-t173）。
    """
    hit = triggers.detect(text)
    if hit is not None:
        return hit[1]
    by_request = _request_intent(request, text)
    if by_request != INTENT_UNKNOWN:
        return by_request
    return _vote_intent(keyword_votes(text))


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
