"""转人工触发词（p12 跨轨契约 §1.4 T169，一轮判定顺序的第 3 步）。

客户一句话里出现这五类说法之一，前台不检索、不作答，直接转人工：

=============  ==========================================  =================
原因（reason）  至少认（契约 §1.4 表）                       意图（intent）
=============  ==========================================  =================
privacy        手机号、身份证、住址、个人信息、隐私          privacy
compensation   赔偿、补偿、赔钱、赔我                        compensation
anger          垃圾、骗子、气死、滚、连续三个及以上感叹号    complaint
complaint      投诉、12315、消协、曝光、起诉、律师           complaint
requested      转人工、人工客服、找人工、真人                handoff_request
=============  ==========================================  =================

同时命中几类时按**优先级**取一类：privacy > compensation > anger > complaint > requested
（契约原文）。理由是「谁最不能让机器人多说一句」：隐私一答就可能回显敏感信息，赔偿一答
就可能被当成承诺，情绪与投诉要先安抚，点名要人工的最宽松。

## 先规范化再匹配

逐字 NFKC（全角 → 半角：「！」→「!」、全角数字 → 半角，于是「１２３１５」就是「12315」），
去掉空白与格式字符（零宽空格一类），英文转小写。感叹号认「!」与「❗❕」（「‼」经 NFKC
本来就是两个「!」），连续三个及以上判 anger —— 中英文混着打的「！!！」同样算。
「12315」按数字边界认：嵌在更长的数字串里（订单号、手机号）不算；空格算边界
（「12315 12345都打过了」照认，见 ``_HOTLINE_RE`` 的注释）。

## 纯函数、零模型

同一句话恒得同一个结果；不读库、不调模型、不打日志（客户原文不进任何日志）。
词表按「宁可多转、不可漏转」写：误把一句普通咨询转了人工，代价是人工多接一次；
漏掉一句投诉或隐私诉求，机器人就会照话术库答下去。
"""

from __future__ import annotations

import re
import unicodedata

from maos.domain.cs.types import (
    HANDOFF_ANGER,
    HANDOFF_COMPENSATION,
    HANDOFF_COMPLAINT,
    HANDOFF_PRIVACY,
    HANDOFF_REQUESTED,
    INTENT_COMPENSATION,
    INTENT_COMPLAINT,
    INTENT_HANDOFF_REQUEST,
    INTENT_PRIVACY,
)

#: 优先级从高到低（契约 §1.4）。
PRIORITY: tuple[str, ...] = (HANDOFF_PRIVACY, HANDOFF_COMPENSATION, HANDOFF_ANGER,
                             HANDOFF_COMPLAINT, HANDOFF_REQUESTED)

#: 原因 → 本轮意图（契约 §1.4：anger 与 complaint 都记 complaint）。
INTENT_OF: dict[str, str] = {
    HANDOFF_PRIVACY: INTENT_PRIVACY,
    HANDOFF_COMPENSATION: INTENT_COMPENSATION,
    HANDOFF_ANGER: INTENT_COMPLAINT,
    HANDOFF_COMPLAINT: INTENT_COMPLAINT,
    HANDOFF_REQUESTED: INTENT_HANDOFF_REQUEST,
}

#: 契约表里「至少认」的词，逐字（测试逐条钉住它们确实被认出）。
CONTRACT_WORDS: dict[str, tuple[str, ...]] = {
    HANDOFF_REQUESTED: ("转人工", "人工客服", "找人工", "真人"),
    HANDOFF_COMPLAINT: ("投诉", "12315", "消协", "曝光", "起诉", "律师"),
    HANDOFF_ANGER: ("垃圾", "骗子", "气死", "滚"),
    HANDOFF_COMPENSATION: ("赔偿", "补偿", "赔钱", "赔我"),
    HANDOFF_PRIVACY: ("手机号", "身份证", "住址", "个人信息", "隐私"),
}

#: 在契约词表之上补的自然变体（规范化后的正文上匹配的正则片段）。
_EXTRA_PATTERNS: dict[str, tuple[str, ...]] = {
    # 「人工」一词就够：转人工 / 人工客服 / 找人工 / 要人工 都含它。
    HANDOFF_REQUESTED: (
        "人工", "活人", "客服小姐姐", "客服小哥", "客服妹妹",
        r"(?:不要|不想|不跟|不和|别让|别用)(?:跟|和|同)?(?:你们?的?)?机器人",
        r"(?:叫|找|换)(?:你们的?|个|一个)?(?:经理|主管|负责人|领导|老板)",
    ),
    HANDOFF_COMPLAINT: (
        "消费者协会", "消保委", "工商局", "市场监管", "法院", "告你们", "举报", "维权",
        "黑猫投诉",
    ),
    HANDOFF_ANGER: (
        "骗人", "骗钱", "坑人", "坑爹", "黑店", "气炸", "恶心", "混蛋", "王八蛋", "无耻",
        "他妈", "妈的", "傻逼", "去死", "什么破", "破店",
    ),
    # 「赔」几乎只在赔偿语境里出现：赔点钱 / 赔付 / 索赔 / 包赔 / 退一赔三 都认。
    # 「赔本」是店家的话，不算。
    HANDOFF_COMPENSATION: (r"赔(?!本)", "损失费", "精神损失"),
    HANDOFF_PRIVACY: (
        "个人资料", "身份信息", "身份证号", "手机号码", "电话号码", "银行卡号",
        "泄露", "泄漏",
    ),
}

#: 「滚」单字：排除「滚筒 / 滚动 / 滚轮 / 滚烫 / 翻滚 / 打滚」这类与情绪无关的词。
_GUN_RE = r"(?<![翻打])滚(?![筒动轮珠烫雪梯刀])"

#: 「12315」要前后都不挨着数字（复核 L2-3）：订单号、流水号、手机号里碰巧含这五位的
#: （「20261231500」）不是投诉。规范化后全角数字已是半角。
#:
#: 数字边界在**两份**规范化文本上各判一次，任一份认出就算（复核二轮 L2-3，宁可多转）：
#:
#: * 去掉空白的那份（其余词表也用它）：「1 2 3 1 5」这种拆开打的号照认；
#: * 空白压成一个空格、不删的那份（:func:`_spaced`）：空格本身就是数字边界 ——
#:   「12315 12345都打过了」「我打了12315 3次了」照认，不会跟后面的数字粘成一长串漏掉。
#:
#: 代价：「单号 2026 12315 0」这种用空格隔开的数字串也会判投诉、转人工（多转一次，不漏转）。
_HOTLINE_RE = r"(?<!\d)12315(?!\d)"
_HOTLINE_SPACED_RE = re.compile(_HOTLINE_RE)

#: 契约词里不按字面匹配、改用上面专门写法的几个。
_SPECIAL_WORDS: dict[str, str] = {"滚": _GUN_RE, "12315": _HOTLINE_RE}

#: 感叹号：规范化后的「!」与 NFKC 不改写的两个 emoji 感叹号。
EXCLAMATIONS = "!❗❕"
#: 连续几个感叹号判 anger（契约：三个及以上）。
EXCLAMATION_RUN = 3
_EXCLAIM_RE = re.compile(f"[{re.escape(EXCLAMATIONS)}]{{{EXCLAMATION_RUN},}}")


def _pattern_for(reason: str) -> re.Pattern[str]:
    parts = [_SPECIAL_WORDS.get(w) or re.escape(w) for w in CONTRACT_WORDS[reason]]
    parts.extend(_EXTRA_PATTERNS.get(reason, ()))
    return re.compile("|".join(f"(?:{p})" for p in parts))


_PATTERNS: dict[str, re.Pattern[str]] = {reason: _pattern_for(reason) for reason in PRIORITY}


def normalize(text: str) -> str:
    """逐字 NFKC、去空白与格式字符（零宽一类）、英文转小写。"""
    out: list[str] = []
    for ch in text or "":
        for c in unicodedata.normalize("NFKC", ch):
            if c.isspace() or unicodedata.category(c) == "Cf":
                continue
            out.append(c.lower())
    return "".join(out)


def _spaced(text: str) -> str:
    """同 :func:`normalize`，但空白不删、每一处换成一个空格：给「12315」判数字边界用。"""
    out: list[str] = []
    for ch in text or "":
        for c in unicodedata.normalize("NFKC", ch):
            if c.isspace():
                out.append(" ")
            elif unicodedata.category(c) != "Cf":
                out.append(c.lower())
    return "".join(out)


def matched_reasons(text: str) -> tuple[str, ...]:
    """这句话命中的全部原因，按优先级从高到低排。一个都不中返回空元组。"""
    norm = normalize(text)
    if not norm:
        return ()
    hit = []
    for reason in PRIORITY:
        if _PATTERNS[reason].search(norm):
            hit.append(reason)
        elif reason == HANDOFF_ANGER and _EXCLAIM_RE.search(norm):
            hit.append(reason)
        elif reason == HANDOFF_COMPLAINT and _HOTLINE_SPACED_RE.search(_spaced(text)):
            hit.append(reason)
    return tuple(hit)


def detect(text: str) -> tuple[str, str] | None:
    """``(转人工原因, 本轮意图)``；不触发返回 None。多类同中取优先级最高的一类。"""
    reasons = matched_reasons(text)
    if not reasons:
        return None
    return reasons[0], INTENT_OF[reasons[0]]
