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

## 复合词白名单（p13 T173）

子串匹配会误伤**复合词**：商品名、地名里夹着触发词的两个字（「空气炸锅」里的「气炸」、
「垃圾袋」里的「垃圾」、「去死皮」里的「去死」、「给我妈的礼物」里的「妈的」），一句普通咨询
被判成情绪激烈、转了人工。三种手段里选**已知复合词白名单**（:data:`BENIGN_COMPOUNDS`，
数据表、按原因分组只为可读）：规范化之后、匹配之前，先把白名单里的复合词整段**遮掉**
（换成同长的占位符），再在剩下的文字上照旧匹配。整词只管列出来的那几个串，所以同一张白名单
另有**上下文写法**（:data:`BENIGN_PATTERNS`，复核 L3-3）：「垃圾 + 桶 / 筒 / 袋 / 挂……」「气炸 +
锅 / 烤箱」「称谓 + 妈的」「去死 + 皮 / 角 / 海」这类「触发词 + 前后文」的正则，只遮触发词那几个字，
换个同类的商品名、地名也认得出。

* 不选「词边界」：中文没有词边界，要边界就得分词，而分词本身就会把「气炸锅」切错；
* 不选「否定语境」：误伤不是否定句（「不是垃圾」），是复合词，否定判不到它；
* 遮掉的只是那个复合词本身：同一句里别处的真触发词照认（「垃圾桶都比你们的东西好，垃圾店」
  照样 anger；「人工费多少，转人工」照样 requested）。白名单只收**不可能是诉求**的复合词，
  所以「人工智能」不收 —— 问「你是人工智能吗」的客户多半是想找人。

## 英文触发词（p13 T173）

同一张优先级表，英文说法在 :data:`EN_PATTERNS`（human / real person / live agent / complaint /
lawyer / sue / scam / compensation / personal data ……）。英文要按词认（「sue」不能认进
「issue」、「human」不能认进「inhumane」），而 :func:`normalize` 为了中文把空白全删了，所以英文
在另一份规范化文本上匹配（:func:`_spaced_words`：NFKC、小写、空白压成一个空格），词边界用
「前后不挨 ASCII 字母数字」—— 中英混排的「找human」也认得出（``\\b`` 在汉字与字母之间不成立）。
「agent」单说不认（「cleaning agent」是清洁剂），要带冠词或 live / real / support 这类修饰。
英文同样有复合词白名单（「human hair」是假发的品类、「trash can」是垃圾桶、「privacy screen」
是防窥膜、「privacy policy」是问条款）与上下文写法（trash / garbage + 物件名）；「representative」
要带冠词或修饰、「phone number」要是「my phone number」或说到泄露才认。

「refund me now」这类要钱的说法**不**进触发词：p13 起退款诉求走「身份核验 → 查单 → 预检卡」
（契约 §2 第 6 步），在第 3 步就转人工会把那条路整个绕开（见 docs/DECISIONS.md task-t173）。

## 纯函数、零模型

同一句话恒得同一个结果；不读库、不调模型、不打日志（客户原文不进任何日志）。
词表按「宁可多转、不可漏转」写：误把一句普通咨询转了人工，代价是人工多接一次；
漏掉一句投诉或隐私诉求，机器人就会照话术库答下去。白名单是这句话唯一的例外，所以它只收
「明确是商品 / 地名 / 物件」的复合词，拿不准的一律不收。
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

#: 「滚」单字：排除「滚筒 / 滚动 / 滚轮 / 滚烫 / 翻滚 / 打滚」这类与情绪无关的词；
#: p13 T173 复核 L2-6 补「摇滚」（服饰图案、乐器）与「滚梳」（卷发梳）。
_GUN_RE = r"(?<![翻打摇])滚(?![筒动轮珠烫雪梯刀梳])"

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


#: 复合词白名单（p13 T173）：夹着触发词子串、但整个词**不可能是诉求**的商品名 / 物件 / 地名。
#: 规范化之后、匹配之前整段遮掉（:func:`_mask`）。按「被误伤的原因」分组只为可读 —— 遮是对
#: 所有原因一起遮的（「曝光补偿」同时夹着 complaint 的「曝光」与 compensation 的「补偿」）。
#: 写成规范化后的形态（小写、无空白）。**只收拿得准的**：拿不准的留给「宁可多转」。
BENIGN_COMPOUNDS: dict[str, tuple[str, ...]] = {
    HANDOFF_ANGER: (
        # 垃圾：家居清洁类商品（「垃圾车 / 垃圾站 / 垃圾处理」不收：「你们就是垃圾站」「垃圾处理方式」
        # 是骂人的说法，复核 L2-5；厨下的「垃圾处理器」是商品，收整词）
        "垃圾袋", "垃圾桶", "垃圾篓", "垃圾箱", "垃圾筐", "垃圾篮",
        "垃圾分类", "垃圾处理器", "垃圾粉碎", "垃圾夹", "垃圾铲", "垃圾收纳", "垃圾清运",
        # 气炸 / 气死：厨房电器、老式马灯
        "空气炸", "气炸锅", "气死风灯",
        # 去死：美妆个护、清洁
        "去死皮", "去死角", "去死细胞",
        # 他妈 / 妈的：说的是自己或别人的母亲（「给我妈的」「他妈妈」），不是骂人
        "他妈妈", "妈妈的", "我妈的", "俺妈的", "咱妈的", "老妈的", "给妈的", "帮妈的",
        "替妈的", "姑妈的", "舅妈的", "姨妈的", "干妈的", "后妈的",
        # 什么破：问有没有破损，不是「什么破东西」
        "什么破损", "什么破洞", "什么破裂", "什么破口",
        # 恶心：药品 / 晕车贴的功效说明
        "缓解恶心", "止恶心", "防恶心",
        # 滚（单字另有 _GUN_RE）：五金、服装工艺
        "滚刷", "滚轴", "滚子", "滚边", "滚针",
    ),
    HANDOFF_COMPLAINT: (
        # 曝光：相机参数
        "曝光补偿", "曝光度", "曝光时间", "曝光模式", "曝光值", "曝光过度", "曝光不足",
        "曝光锁定", "曝光参数", "长曝光", "自动曝光", "包围曝光", "多重曝光", "双重曝光",
    ),
    HANDOFF_COMPENSATION: (
        # 补偿：电子 / 工控元件的术语
        "温度补偿", "补偿器", "补偿导线", "运动补偿",
    ),
    HANDOFF_REQUESTED: (
        # 人工：人造的商品，或装修的人工费
        "人工草坪", "人工草皮", "人工草", "人工湖", "人工费", "人工泪液", "人工耳蜗",
        "人工钻石", "人工宝石", "人工晶体", "人工水晶", "人工皮", "人工革", "人工合成",
        "人工养殖", "人工种植",
        # 真人：服装 / 假发的实拍与品类
        "真人实拍", "真人图", "真人照", "真人视频", "真人试穿", "真人秀", "真人版",
        "真人发", "真人模特", "真人手办", "真人娃娃", "真人尺寸", "真人大小", "真人cs",
    ),
    HANDOFF_PRIVACY: (
        # 隐私：手机防窥膜、浴室隐私帘
        "隐私膜", "隐私保护膜", "隐私屏", "隐私帘", "隐私玻璃", "隐私贴",
        # 泄漏：防漏的日用品（「不泄漏」不收：「请保证不泄漏我的信息」是隐私诉求，复核 L2-5）
        "防泄漏",
    ),
}

#: 复合词的**上下文写法**（p13 T173 复核 L2-6 / L3-3）：整词白名单只遮列出来的那几个串，换一个
#: 同类的商品（「气炸烤箱」「垃圾筒」「垃圾挂袋」）、地名（「死海」「黑店村」）、称谓（「爸妈的」
#: 「孩子他妈」）就又误伤。这里按「触发词两个字 + 前后是什么」写成正则片段，**只遮触发词那几个字**
#: （前后文用环视判、不遮），在规范化后的正文上与 :data:`BENIGN_COMPOUNDS` 一起遮。
#: 同样「只收拿得准的」：「他妈的」「你妈的」「去死吧」「恶心死了」一律不在内。
BENIGN_PATTERNS: dict[str, tuple[str, ...]] = {
    HANDOFF_ANGER: (
        # 垃圾 + 盛放 / 清理的物件（垃圾筒、垃圾挂袋、垃圾兜）
        r"垃圾(?=[袋桶筒篓箱筐篮夹铲兜盒挂罐])",
        # 气炸 + 厨房电器（气炸锅、气炸烤箱、气炸一体机）；空气 + 炸 / 死（空气炸锅、空气死角）
        r"气炸(?=锅|烤|机|一体)", r"空气(?=炸|死)",
        # 去死 + 皮 / 角 / 细胞 / 茧（美妆清洁）；去 + 死海（地名）
        r"去死(?=皮|角|细胞|茧|海)",
        # 妈的：前面是称谓、自称或「给 / 帮 / 替」（给爸妈的、宝妈的、奶妈的、妈妈的）；
        # 前面是「你 / 他 / 她」的（你妈的、他妈的）不在内
        r"(?<=[我俺咱爸老给帮替姑舅姨干后宝奶岳妈])妈的",
        # 他妈：「他妈妈 / 他妈咪」、其他妈咪包、孩子他妈、老公他妈；后面跟「的 / 逼」的不在内
        r"他妈(?=妈|咪)", r"(?<=[其子公婆])他妈(?![的逼])",
        # 恶心：药品 / 孕期用品的功效与副作用问法（缓解恶心、孕吐恶心、吃了会恶心吗）
        r"(?:缓解|止|防|预防|减轻|孕吐|晕车|晕船|反胃)恶心",
        r"会(?:不会)?恶心(?=[吗么嘛呢不啊呀]|$)",
        # 黑店 + 村镇街路（地名）
        r"黑店(?=村|镇|乡|街|路|庄|屯|寨|桥|湾)",
        # 滚滚：熊猫的昵称、玩偶
        r"(?<=熊猫)滚滚", r"滚滚(?=玩偶|公仔|抱枕|周边)",
    ),
}

#: 英文触发词（p13 T173）：写成**不带边界**的正则片段，匹配时统一包上「前后不挨 ASCII 字母数字」
#: （:data:`_EN_BOUNDARY`），在 :func:`_spaced_words` 的文本上认。
EN_PATTERNS: dict[str, tuple[str, ...]] = {
    HANDOFF_REQUESTED: (
        r"humans?", r"real (?:person|people|human)", r"live (?:agent|person|chat|support)",
        r"(?:an?|the|real|live|human|support|service) agent",
        r"(?:customer service|customer support|support) (?:rep|representative|agent|staff)",
        # 「representative」要带冠词或修饰才是「客服代表」（「Is this color representative of …」
        # 是「有代表性」，复核 L3-4）
        r"(?:a|an|the|your|customer|service|sales|support|real|human|live|company) representatives?",
        r"representatives? please", r"operator",
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
        r"report (?:you|this|your (?:shop|store|company))", r"expose you",
    ),
    HANDOFF_ANGER: (
        r"scam(?:s|mer|mers|med)?", r"fraud(?:s|ulent)?", r"cheat(?:s|ed|ers?|ing)?",
        r"rip(?:-| )?off", r"garbage", r"rubbish", r"trash", r"useless", r"ridiculous",
        r"wtf", r"f+u+c+k\w*", r"shit\w*", r"damn", r"idiots?", r"stupid", r"pissed",
    ),
    HANDOFF_COMPENSATION: (
        r"compensat(?:e|ed|es|ion|ing)", r"pay (?:me )?for (?:my|the) (?:loss|losses|damages?|trouble)",
    ),
    HANDOFF_PRIVACY: (
        # 「phone number」要是**客户自己的**号（my phone number）或说到泄露；「your store phone
        # number」是问店铺电话（复核 L3-4）。「privacy policy」在 EN_BENIGN_COMPOUNDS 里遮掉
        r"personal (?:data|info|information|details)", r"privacy",
        r"my (?:phone|mobile|cell)(?: phone)? numbers?",
        r"(?:phone|mobile) numbers? (?:was |were |got |has been |have been )?(?:leaked|exposed|sold|stolen|shared)",
        r"(?:id|identity) card", r"passport", r"social security", r"ssn",
        r"delete my (?:data|account|info|information|details)", r"gdpr",
        # 「leaked」单说多半是瓶子漏了，只认泄露信息的说法；「home address」多半是收货地址，不收
        r"data (?:leak|breach)", r"leak(?:ed|ing)? my (?:data|info|information|details|number)",
    ),
}

#: 英文的复合词白名单（写成空格分词的小写形态）。
EN_BENIGN_COMPOUNDS: tuple[str, ...] = (
    "human hair", "trash can", "trash cans", "trash bag", "trash bags", "trash bin", "trash bins",
    "garbage bag", "garbage bags", "garbage can", "garbage cans", "garbage bin", "garbage bins",
    "garbage disposal", "rubbish bag", "rubbish bags", "rubbish bin", "rubbish bins",
    "privacy screen", "privacy filter", "privacy film", "privacy protector", "privacy glass",
    "privacy curtain", "privacy fence", "privacy policy",
    "passport holder", "passport cover", "passport case", "passport wallet", "passport bag",
    "id card holder", "id card case",
)

#: 英文复合词的上下文写法（复核 L3-4）：trash / garbage / rubbish 后面跟着盛放、清理、运输的物件名
#: （trash compactor、garbage truck toy、rubbish chute）是商品，整段遮掉。
EN_BENIGN_PATTERNS: tuple[str, ...] = (
    r"(?:trash|garbage|rubbish) (?:cans?|bags?|bins?|compactors?|disposals?|trucks?|liners?|"
    r"containers?|pickers?|grabbers?|chutes?|lids?|baskets?|sacks?)",
)

#: 英文按词认：前后不挨 ASCII 字母数字。
_EN_BOUNDARY = "(?<![a-z0-9])(?:{})(?![a-z0-9])"

#: 遮复合词用的占位符（规范化后的文本里不会出现：它不是任何触发词的一部分）。
_MASK_CHAR = "□"


def _pattern_for(reason: str) -> re.Pattern[str]:
    parts = [_SPECIAL_WORDS.get(w) or re.escape(w) for w in CONTRACT_WORDS[reason]]
    parts.extend(_EXTRA_PATTERNS.get(reason, ()))
    return re.compile("|".join(f"(?:{p})" for p in parts))


_PATTERNS: dict[str, re.Pattern[str]] = {reason: _pattern_for(reason) for reason in PRIORITY}

_EN_RES: dict[str, re.Pattern[str]] = {
    reason: re.compile(_EN_BOUNDARY.format("|".join(f"(?:{p})" for p in EN_PATTERNS[reason])))
    for reason in PRIORITY
}

#: 中文白名单：整词（长的先遮）在前、上下文写法在后，一条正则一趟遮完。环视看的是**遮之前**的
#: 原文（``re.sub`` 的语义），所以「他妈妈的」里「他妈」遮掉之后，「妈的」前面仍是「妈」、照样遮。
_BENIGN_RE = re.compile("|".join(
    [re.escape(w) for w in sorted({w for ws in BENIGN_COMPOUNDS.values() for w in ws},
                                  key=lambda w: (-len(w), w))]
    + [f"(?:{p})" for ps in BENIGN_PATTERNS.values() for p in ps]))
_EN_BENIGN_RE = re.compile(_EN_BOUNDARY.format("|".join(
    [re.escape(w) for w in sorted(set(EN_BENIGN_COMPOUNDS), key=lambda w: (-len(w), w))]
    + [f"(?:{p})" for p in EN_BENIGN_PATTERNS])))


def _mask(pattern: re.Pattern[str], text: str) -> str:
    """把 ``pattern`` 认出的每一段换成同长的占位符。"""
    return pattern.sub(lambda m: _MASK_CHAR * len(m.group(0)), text)


def normalize(text: str) -> str:
    """逐字 NFKC、去空白与格式字符（零宽一类）、英文转小写。"""
    out: list[str] = []
    for ch in text or "":
        for c in unicodedata.normalize("NFKC", ch):
            if c.isspace() or unicodedata.category(c) == "Cf":
                continue
            out.append(c.lower())
    return "".join(out)


def _spaced_words(text: str) -> str:
    """英文匹配用的那份：同 :func:`_spaced`，再把弯引号换成直引号、连续空格压成一个。"""
    return re.sub(" +", " ", _spaced(text).replace("’", "'").replace("‘", "'"))


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
    masked = _mask(_BENIGN_RE, norm)
    words = _mask(_EN_BENIGN_RE, _spaced_words(text))
    hit = []
    for reason in PRIORITY:
        if _PATTERNS[reason].search(masked) or _EN_RES[reason].search(words):
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
