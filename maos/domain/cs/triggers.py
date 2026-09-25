"""转人工触发词（p12 跨轨契约 §1.4 T169，一轮判定顺序的第 3 步；p13 由 T173 接手）。

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
就可能被当成承诺，情绪与投诉要先安抚，点名要人工的最宽松。英文说法在同一张优先级表里，
中英混排的一句照样按这张表取（「垃圾店, delete my personal data」是 privacy）。

## 先规范化再匹配

逐字 NFKC（全角 → 半角：「！」→「!」、全角数字 → 半角，于是「１２３１５」就是「12315」），
去掉空白与格式字符（零宽空格一类），英文转小写。感叹号认「!」与「❗❕」（「‼」经 NFKC
本来就是两个「!」），连续三个及以上判 anger —— 中英文混着打的「！!！」同样算。
「12315」按数字边界认：嵌在更长的数字串里（订单号、手机号）不算；空格算边界
（「12315 12345都打过了」照认，见 ``_HOTLINE_RE`` 的注释）。

## 三层：p12 地板、只加不减、名词性良性复合词（p13 T173，主会话裁定 R1）

安全方向是「多转人工可以，漏转不行」，所以词表分三层写：

1. **p12 地板**：契约词表（:data:`CONTRACT_WORDS`）、p12 的自然变体（:data:`_EXTRA_PATTERNS`）、
   「滚」与「12315」的专门写法、连续感叹号 —— **逐字照搬 bf53df6**，一个字都不收窄。
   :func:`p12_reasons` 就是 p12 的 ``matched_reasons``（测试拿 bf53df6 实跑出来的结果钉住它）。
2. **只加不减**：p12 漏掉的中文骂法（:data:`ADDED_PATTERNS`：操你妈 / 他娘的 / 尼玛 / tmd……）
   与英文触发词（:data:`EN_PATTERNS`）。它们只会让一句话多判出原因、判出更高优先级的原因，
   不会让任何一句少判。
3. **唯一的例外：名词性良性复合词**（:data:`BENIGN_NOUNS`，一张封闭的名词表：商品、地点、
   日常名词 —— 「空气炸锅」里的「气炸」、「垃圾袋」里的「垃圾」、「人工草坪」里的「人工」）。
   一句话里触发词的**每一处**匹配都完全落在某个表内名词里、且句中**没有任何别的触发词**
   （含英文、感叹号、12315）时，这句不触发；只要还有一处别的触发词，表内名词里的那几处
   照常算，优先级不因复合词降低（「曝光补偿坏了，转人工」仍是 compensation，同 p12）。

   「完全落在」按字符区间判：p12 的「去死」在「去死海」里跨过了「死海」的左边界（「去」在
   名词外），所以「死海」救不了「我想去死海玩」—— 这句照旧转人工（多转一次，不漏转）。

   否定语境、亲属称谓、上下文正则（前后是什么字就遮掉）一律不用：复核证明它们会吞掉真骂人
   的话（「你老妈的」「他妈妈的就知道拖」），而名词表是封闭的、每一条都能逐条审。

## 英文触发词（p13 T173）

英文要按词认（「sue」不能认进「issue」、「human」不能认进「inhumane」），而 :func:`normalize`
为了中文把空白全删了，所以英文在另一份规范化文本上匹配（:func:`_spaced_words`：NFKC、小写、
空白压成一个空格），词边界用「前后不挨 ASCII 字母数字」—— 中英混排的「找human」也认得出。
英文只认**诉求的说法**（「agent」「operator」单说不认，要带冠词或 talk to 一类的说法；
「cleaning agent」是清洁剂）。英文是纯增量（p12 一个英文词都不认），英文名词表
（:data:`EN_BENIGN_NOUNS`：trash can、human hair、privacy screen……）只遮英文这一层。

「refund me now」这类要钱的说法**不**进触发词：p13 起退款诉求走「身份核验 → 查单 → 预检卡」
（契约 §2 第 6 步），在第 3 步就转人工会把那条路整个绕开（见 docs/DECISIONS.md task-t173）。

## 纯函数、零模型

同一句话恒得同一个结果；不读库、不调模型、不打日志（客户原文不进任何日志）。
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

# ---------------------------------------------------------------------------
# 第 1 层：p12 地板（逐字同 bf53df6，不许收窄）
# ---------------------------------------------------------------------------
#: 契约表里「至少认」的词，逐字（测试逐条钉住它们确实被认出）。
CONTRACT_WORDS: dict[str, tuple[str, ...]] = {
    HANDOFF_REQUESTED: ("转人工", "人工客服", "找人工", "真人"),
    HANDOFF_COMPLAINT: ("投诉", "12315", "消协", "曝光", "起诉", "律师"),
    HANDOFF_ANGER: ("垃圾", "骗子", "气死", "滚"),
    HANDOFF_COMPENSATION: ("赔偿", "补偿", "赔钱", "赔我"),
    HANDOFF_PRIVACY: ("手机号", "身份证", "住址", "个人信息", "隐私"),
}

#: 在契约词表之上补的自然变体（规范化后的正文上匹配的正则片段）。p12 原样。
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

#: 「滚」单字：排除「滚筒 / 滚动 / 滚轮 / 滚烫 / 翻滚 / 打滚」这类与情绪无关的词。p12 原样。
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


def _p12_parts(reason: str) -> tuple[str, ...]:
    """p12 的 ``_pattern_for`` 拼的那几段（契约词 + 专门写法 + 自然变体），顺序同 p12。"""
    parts = [_SPECIAL_WORDS.get(w) or re.escape(w) for w in CONTRACT_WORDS[reason]]
    parts.extend(_EXTRA_PATTERNS.get(reason, ()))
    return tuple(parts)


#: p12 的整条正则（逐字同 bf53df6 的 ``_PATTERNS``）。
_P12_PATTERNS: dict[str, re.Pattern[str]] = {
    reason: re.compile("|".join(f"(?:{p})" for p in _p12_parts(reason))) for reason in PRIORITY
}

# ---------------------------------------------------------------------------
# 第 2 层：只加不减（p13 T173）
# ---------------------------------------------------------------------------
#: p12 漏掉的中文说法（规范化后的正文上匹配的正则片段，不许带捕获组）。只会多判，不会少判。
ADDED_PATTERNS: dict[str, tuple[str, ...]] = {
    HANDOFF_ANGER: (
        # 操你妈 / 干你娘 / 日你大爷 / 去你妈（p12 只认带「的」的「妈的」与「他妈」）；
        # 「去她妈妈家」「去你大爷家」是走亲戚，不算
        r"(?:操|草|艹|日|干)(?:你|尼|他|她)(?:个)?老?(?:妈|娘|大爷)",
        r"去你(?:个)?老?(?:妈|娘|大爷)(?![妈家])",
        # 他娘的 / 你娘的 / 你老娘的
        r"(?:你|他|她)老?娘的",
        "尼玛", "特么", "妈蛋", "卧槽", "狗日", "脑残", "智障",
        r"(?<![a-z])tmd(?![a-z])",
        # 「去你的吧」「去你的！」：只认分句末的（「去你的店里买」不是）
        r"去你的(?=$|[吧啊呀,.!?~，。！？])",
    ),
}

#: 英文触发词（p13 T173）：写成**不带边界**的正则片段，匹配时统一包上「前后不挨 ASCII 字母数字」
#: （:data:`_EN_BOUNDARY`），在 :func:`_spaced_words` 的文本上认。
EN_PATTERNS: dict[str, tuple[str, ...]] = {
    HANDOFF_REQUESTED: (
        # 「human」要是在找人（a human / to a human / human please / 找human / 整句就是 human）；
        # 「safe for humans」不是（复核 L2-7）
        r"(?:an?|to a|with a|real|actual|live|need|want|get) humans?(?: beings?)?",
        r"humans? (?:please|pls|agent|support|service|help|being|representative|operator|staff)",
        r"(?<=[㐀-鿿])humans?", r"^humans?(?=[ .!?]*$)",
        r"real (?:person|people)", r"live (?:agent|person|chat|support)",
        r"(?:an?|the|real|live|human|support|service) agent",
        r"(?:customer service|customer support|support) (?:rep|representative|agent|staff)",
        # 「representative」要带冠词或修饰才是「客服代表」（「Is this color representative of …」
        # 是「有代表性」，复核 L3-4）
        r"(?:a|an|the|your|customer|service|sales|support|real|human|live|company) representatives?",
        r"representatives? please",
        # 「operator」要是真人接线员（human / live operator、talk to an operator）；「work with any
        # operator」是问运营商（复核 L2-7）
        r"(?:human|live|real) operator",
        r"(?:talk|speak|chat|connect me|transfer me|put me through) (?:to|with) "
        r"(?:an? |the |a live |a human |a real )?operator",
        r"(?:talk|speak|chat) (?:to|with) (?:a |an |the |your )?"
        r"(?:person|someone|somebody|manager|supervisor|staff|representative)",
        r"(?:your|a|the) (?:manager|supervisor)",
        r"(?:not|no) (?:a )?(?:bot|robot)s?", r"don'?t want (?:to talk to )?(?:a |the )?(?:bot|robot|machine)",
    ),
    HANDOFF_COMPLAINT: (
        r"complain(?:t|ts|ed|ing|s)?", r"lawyers?", r"attorneys?", r"suing", r"lawsuit",
        # 「sue」单说会认进人名，只认「sue you / will sue」这类说法
        r"sue (?:you|your|the|this)", r"(?:will|gonna|going to|i'll|i will) sue",
        r"legal action", r"consumer (?:protection|council|association|rights)",
        # 「report」只认举报店家（report you / your store）；「report this issue」是报告问题（复核 L2-7）
        r"report (?:you|your (?:shop|store|company|business)|this (?:shop|store|seller|company|scam|fraud))",
        r"expose you",
    ),
    HANDOFF_ANGER: (
        r"scam(?:s|mer|mers|med)?", r"fraud(?:s|ulent)?", r"cheat(?:s|ed|ers?|ing)?",
        # 「rip off」要是名词（a rip-off / what a rip off）或「ripped me off」；「rip off the tags」
        # 是撕标签（复核 L2-7）
        r"rip-?offs?", r"(?:a|what a|such a|total|complete|is a|it's a) rip off",
        r"ripp(?:ed|ing) (?:me|us|people|customers|everyone) off",
        r"garbage", r"rubbish", r"trash", r"useless", r"ridiculous",
        r"wtf", r"f+u+c+k\w*", r"shit\w*", r"damn", r"idiots?", r"stupid", r"pissed",
    ),
    HANDOFF_COMPENSATION: (
        r"compensat(?:e|ed|es|ion|ing)", r"pay (?:me )?for (?:my|the) (?:loss|losses|damages?|trouble)",
    ),
    HANDOFF_PRIVACY: (
        # 「phone number」要是**客户自己的**号（my phone number）或说到泄露；「your store phone
        # number」是问店铺电话（复核 L3-4）。「privacy policy」在 EN_BENIGN_NOUNS 里遮掉
        r"personal (?:data|info|information|details)", r"privacy",
        r"my (?:phone|mobile|cell)(?: phone)? numbers?",
        r"(?:phone|mobile) numbers? (?:was |were |got |has been |have been )?(?:leaked|exposed|sold|stolen|shared)",
        r"(?:id|identity) card", r"passport", r"social security", r"ssn",
        r"delete my (?:data|account|info|information|details)", r"gdpr",
        # 「leaked」单说多半是瓶子漏了，只认泄露信息的说法；「home address」多半是收货地址，不收
        r"data (?:leak|breach)", r"leak(?:ed|ing)? my (?:data|info|information|details|number)",
    ),
}

# ---------------------------------------------------------------------------
# 第 3 层：名词性良性复合词（封闭表，唯一的例外）
# ---------------------------------------------------------------------------
#: 夹着触发词、但整个词是**名词**的复合词：按「商品 / 地点 / 日常名词」三类列，逐条都是名词
#: （测试逐条断言：单说不触发、跟任一真触发词同句照常触发、确实夹着一处 p12 触发词）。
#: 写成规范化后的形态（小写、无空白）。**只收拿得准的名词**：骂人时也拿来比喻的名词
#: （「垃圾站 / 垃圾车 / 垃圾场」「滚边」「滚滚」）、本身就是诉求的名词（「律师函」「起诉书」
#: 「身份证复印件」「精神补偿」）、「人工智能」（问「你是人工智能吗」的客户多半想找人）一律不收。
BENIGN_NOUNS: dict[str, tuple[str, ...]] = {
    "商品": (
        # 垃圾：盛放 / 清理垃圾的家居用品
        "垃圾袋", "垃圾桶", "垃圾篓", "垃圾箱", "垃圾筐", "垃圾篮", "垃圾夹", "垃圾铲", "垃圾挂袋",
        "垃圾处理器",
        # 气炸：厨房电器
        "空气炸锅", "气炸锅", "气炸烤箱", "空气炸烤箱", "气炸一体机",
        # 去死：去角质的护理品
        "去死皮膏", "去死皮霜", "去死皮凝胶", "去死皮刀",
        # 气死：老式马灯
        "气死风灯",
        # 滚：美容、清洁、五金（滚筒 / 滚轮 / 滚珠 p12 本来就不认）
        "冰滚", "粘毛滚", "滚刷", "滚梳", "滚轴",
        # 人工 / 真人：人造材料、医用品、假发与手办
        "人工草坪", "人工草皮", "人工钻石", "人工宝石", "人工水晶", "人工晶体", "人工泪液", "人工耳蜗",
        "人工鱼饵", "人工皮革", "真人发", "真人手办", "真人娃娃", "真人模特",
        # 隐私 / 身份证：防窥膜、遮挡帘、证件套
        "隐私膜", "隐私屏", "隐私帘", "隐私贴", "隐私玻璃", "隐私门帘", "身份证卡套", "身份证套",
        "身份证夹",
        # 补偿：电工元件
        "补偿器", "补偿导线", "补偿电容",
    ),
    "地点": (
        "人工湖", "人工岛", "空气死角",
    ),
    "日常名词": (
        "垃圾分类", "摇滚", "滚石", "真人秀", "真人cs", "人工费",
        # 相机 / 显示器的参数
        "曝光补偿", "曝光度", "曝光时间", "曝光模式", "曝光值", "长曝光", "自动曝光",
        "背光补偿", "逆光补偿", "温度补偿", "运动补偿",
    ),
}

#: 英文的名词性复合词（写成空格分词的小写形态），只遮英文触发词这一层。
EN_BENIGN_NOUNS: tuple[str, ...] = (
    "human hair", "trash can", "trash cans", "trash bag", "trash bags", "trash bin", "trash bins",
    "trash compactor", "trash compactors", "trash grabber", "trash grabbers", "trash picker",
    "trash pickers", "trash liner", "trash liners",
    "garbage bag", "garbage bags", "garbage can", "garbage cans", "garbage bin", "garbage bins",
    "garbage disposal", "garbage disposals", "garbage truck", "garbage trucks",
    "rubbish bag", "rubbish bags", "rubbish bin", "rubbish bins", "rubbish chute", "rubbish chutes",
    "privacy screen", "privacy filter", "privacy film", "privacy protector", "privacy glass",
    "privacy curtain", "privacy fence", "privacy policy",
    "passport holder", "passport cover", "passport case", "passport wallet", "passport bag",
    "id card holder", "id card case",
)

#: 英文按词认：前后不挨 ASCII 字母数字。
_EN_BOUNDARY = "(?<![a-z0-9])(?:{})(?![a-z0-9])"

#: 遮英文名词用的占位符（不是 ASCII 字母数字，不是任何触发词的一部分）。
_MASK_CHAR = "□"


def _scanners(reason: str) -> tuple[re.Pattern[str], ...]:
    """中文这一原因的每一段写法各一条「逐位置」扫描正则：零宽前瞻 + 捕获组，``finditer`` 每个
    起点都试一次（重叠的匹配也拿得到），第 1 组就是这一处匹配的区间。"""
    parts = (*_p12_parts(reason), *ADDED_PATTERNS.get(reason, ()))
    return tuple(re.compile(f"(?=({p}))") for p in parts)


_ZH_SCANNERS: dict[str, tuple[re.Pattern[str], ...]] = {reason: _scanners(reason) for reason in PRIORITY}

_EN_RES: dict[str, re.Pattern[str]] = {
    reason: re.compile(_EN_BOUNDARY.format("|".join(f"(?:{p})" for p in EN_PATTERNS[reason])))
    for reason in PRIORITY
}

_BENIGN_WORDS: tuple[str, ...] = tuple(sorted({w for ws in BENIGN_NOUNS.values() for w in ws},
                                              key=lambda w: (-len(w), w)))
_EN_BENIGN_RE = re.compile(_EN_BOUNDARY.format("|".join(
    re.escape(w) for w in sorted(set(EN_BENIGN_NOUNS), key=lambda w: (-len(w), w)))))


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


def _spaced_words(text: str) -> str:
    """英文匹配用的那份：同 :func:`_spaced`，再把弯引号换成直引号、连续空格压成一个。"""
    return re.sub(" +", " ", _spaced(text).replace("’", "'").replace("‘", "'")).strip()


def benign_spans(norm: str) -> list[tuple[int, int]]:
    """规范化文本里每一处表内名词的区间（同一个词出现几次记几次，互相重叠的也都记）。"""
    spans: list[tuple[int, int]] = []
    for word in _BENIGN_WORDS:
        start = norm.find(word)
        while start >= 0:
            spans.append((start, start + len(word)))
            start = norm.find(word, start + 1)
    return spans


def _zh_hits(norm: str) -> dict[str, bool]:
    """中文各原因的命中：原因 → 这一原因有没有**落在表内名词之外**的匹配（只收有匹配的原因）。"""
    benign = benign_spans(norm)
    out: dict[str, bool] = {}
    for reason in PRIORITY:
        found = outside = False
        for scanner in _ZH_SCANNERS[reason]:
            for m in scanner.finditer(norm):
                start, end = m.span(1)
                if end <= start:
                    continue
                found = True
                if not any(b0 <= start and end <= b1 for b0, b1 in benign):
                    outside = True
                    break
            if outside:
                break
        if found:
            out[reason] = outside
    return out


def matched_reasons(text: str) -> tuple[str, ...]:
    """这句话命中的全部原因，按优先级从高到低排。一个都不中返回空元组。

    中文的每一处匹配都完全落在表内名词里、又没有任何别的触发词（英文、感叹号、12315、
    表外的中文匹配）时返回空元组；否则表内名词里的那几处也照常算（见模块头「三层」）。
    """
    norm = normalize(text)
    if not norm:
        return ()
    zh = _zh_hits(norm)
    reasons = set(zh)
    real = any(zh.values())
    words = _EN_BENIGN_RE.sub(lambda m: _MASK_CHAR * len(m.group(0)), _spaced_words(text))
    for reason in PRIORITY:
        if _EN_RES[reason].search(words):
            reasons.add(reason)
            real = True
    if _EXCLAIM_RE.search(norm):
        reasons.add(HANDOFF_ANGER)
        real = True
    if _HOTLINE_SPACED_RE.search(_spaced(text)):
        reasons.add(HANDOFF_COMPLAINT)
        real = True
    if not real:
        return ()
    return tuple(r for r in PRIORITY if r in reasons)


def p12_reasons(text: str) -> tuple[str, ...]:
    """p12（bf53df6）的 ``matched_reasons``，逐字同：地板层，不带名词表、不带第 2 层。

    前台不用它（前台用 :func:`detect`）；它是「p13 不许比 p12 少判」这条判据的对照物，
    测试拿 bf53df6 实跑出来的结果钉住它没有漂。
    """
    norm = normalize(text)
    if not norm:
        return ()
    hit = []
    for reason in PRIORITY:
        if _P12_PATTERNS[reason].search(norm):
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
