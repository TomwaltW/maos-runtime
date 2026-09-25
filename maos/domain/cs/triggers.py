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
就可能被当成承诺，情绪与投诉要先安抚，点名要人工的最宽松。英文说法在同一张优先级表里，
中英混排的一句照样按这张表取（「垃圾店, delete my personal data」是 privacy）。

## 先规范化再匹配

逐字 NFKC（全角 → 半角：「！」→「!」、全角数字 → 半角，于是「１２３１５」就是「12315」），
去掉空白与格式字符（零宽空格一类），英文转小写。感叹号认「!」与「❗❕」（「‼」经 NFKC
本来就是两个「!」），连续三个及以上判 anger —— 中英文混着打的「！!！」同样算。
「12315」按数字边界认：嵌在更长的数字串里（订单号、手机号）不算；空格算边界
（「12315 12345都打过了」照认，见 ``_HOTLINE_RE`` 的注释）。

## 复合词误伤（p13 T173）

子串匹配会误伤**复合词**：商品名、称谓、地名里夹着触发词的两个字（「空气炸锅」里的「气炸」、
「垃圾袋」里的「垃圾」、「孕妈的防辐射服」里的「妈的」、「圆滚滚」里的「滚」、「背光补偿」里的
「补偿」），一句普通咨询被判成情绪激烈、转了人工。派单给的三种手段（词边界 / 复合词白名单 /
否定语境）里：

* 不选「词边界」：中文没有词边界，要边界就得分词，而分词本身就会把「气炸锅」切错；
* 不选「否定语境」：误伤不是否定句（「不是垃圾」），是复合词，否定判不到它；
* 选**已知复合词白名单**，但按触发词的**两种形状**分开写（复核 L3-1：逐个列无害上下文，
  换一批没见过的商品名就又误伤）：

  1. **骂人的说法是闭集、无害的说法是开集**的几个短词（「滚」「妈的 / 他妈」「气炸」「去死」
     「什么破」）：无害的商品名、称谓、地名列不完，改成**只认骂人的句型**（:data:`INSULT_SHAPES`
     与 ``_GUN_SHAPES``）：「滚蛋 / 滚开 / 给我滚 / 整句只有滚」、「你妈的 / 他妈的 / 你老妈的 /
     去你妈 / 分句开头的妈的 / 妈的 + 太 / 真 / 就……」、「气炸了 / 快气炸」、「去死吧 / 你去死」、
     「什么破 + 名词」（「为什么破了」「破损 / 破壁机」不是）。句型之外的一律不算。
  2. **骂人 / 诉求的说法是开集、无害的说法是闭集**的词（「垃圾 + 任何名词」都能骂人；「补偿」
     「人工」「隐私」「身份证」的诉求说法列不完）：照旧按子串认，只把**成类**的无害复合词遮掉 ——
     整词（:data:`BENIGN_COMPOUNDS`）与上下文写法（:data:`BENIGN_PATTERNS`：垃圾 + 盛放 / 清理的
     物件、补偿前面是技术参数（背光 / 温度 / 梯形 / 功率……）或后面是元件与设置、恶心是身体不适
     （吃了 / 喝了……恶心、恶心想吐、孕吐恶心）、人工 + 材料 / 景观 / 医疗器件、隐私 / 身份证 +
     收纳与遮挡物件）。遮掉的只是那几个字：同一句里别处的真触发词照认（「垃圾桶都比你们的东西好，
     垃圾店」照样 anger；「人工费多少，转人工」照样 requested）。白名单只收**不可能是诉求**的
     复合词，所以「人工智能」不收 —— 问「你是人工智能吗」的客户多半是想找人。

句型与白名单都只管「拿得准的」：拿不准的留给「宁可多转」。

## 英文触发词（p13 T173）

同一张优先级表，英文说法在 :data:`EN_PATTERNS`（human / real person / live agent / complaint /
lawyer / sue / scam / compensation / personal data ……）。英文要按词认（「sue」不能认进
「issue」、「human」不能认进「inhumane」），而 :func:`normalize` 为了中文把空白全删了，所以英文
在另一份规范化文本上匹配（:func:`_spaced_words`：NFKC、小写、空白压成一个空格），词边界用
「前后不挨 ASCII 字母数字」—— 中英混排的「找human」也认得出（``\\b`` 在汉字与字母之间不成立）。
同样只认**诉求的说法**：「agent」「human」「operator」「representative」单说不认，要带冠词、
修饰或「talk to」一类的说法（「cleaning agent」是清洁剂、「safe for humans」是问安全、「work with
any operator」是问运营商）；「rip off」要是名词（a rip-off）或「ripped me off」（「rip off the tags」
是撕标签）；「report」要是「report you / your store」（「report this issue」是报告问题）。
英文同样有复合词白名单（「human hair」是假发的品类、「trash can」是垃圾桶、「privacy screen」
是防窥膜、「privacy policy」是问条款）与上下文写法（trash / garbage + 物件名）。

「refund me now」这类要钱的说法**不**进触发词：p13 起退款诉求走「身份核验 → 查单 → 预检卡」
（契约 §2 第 6 步），在第 3 步就转人工会把那条路整个绕开（见 docs/DECISIONS.md task-t173）。

## 纯函数、零模型

同一句话恒得同一个结果；不读库、不调模型、不打日志（客户原文不进任何日志）。
词表按「宁可多转、不可漏转」写：误把一句普通咨询转了人工，代价是人工多接一次；
漏掉一句投诉或隐私诉求，机器人就会照话术库答下去。句型与白名单是这条的例外，所以它们只收
「明确是骂人的句型」与「明确是商品 / 物件 / 地名」的复合词，拿不准的一律不收。
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
    # 「他妈 / 妈的 / 气炸 / 去死 / 什么破」不在这里：它们只按骂人的句型认（INSULT_SHAPES）。
    HANDOFF_ANGER: (
        "骗人", "骗钱", "坑人", "坑爹", "黑店", "恶心", "混蛋", "王八蛋", "无耻",
        "傻逼", "破店",
    ),
    # 「赔」几乎只在赔偿语境里出现：赔点钱 / 赔付 / 索赔 / 包赔 / 退一赔三 都认。
    # 「赔本」是店家的话，不算。
    HANDOFF_COMPENSATION: (r"赔(?!本)", "损失费", "精神损失"),
    HANDOFF_PRIVACY: (
        "个人资料", "身份信息", "身份证号", "手机号码", "电话号码", "银行卡号",
        "泄露", "泄漏",
    ),
}

#: 规范化后仍在的标点：分句的边界（句型里「整个分句」「分句开头 / 末尾」按它判）。
_PUNCT = ",.!?;:~。、…❗❕"
_START = f"(?:^|(?<=[{_PUNCT}]))"
_END = f"(?=$|[{_PUNCT}])"

#: 「滚」只认骂人的句型（p13 T173 复核 L3-1：「圆滚滚」「冰滚」「粘毛滚」「滚石」「逗猫滚球」
#: 这类无害的词列不完，p12 的「排除滚筒 / 滚动……」同理）：
#:
#: * 滚 + 骂人的后缀：滚蛋 / 滚开 / 滚犊子 / 滚粗 / 滚远点 / 滚一边 / 滚出去 / 滚回去 / 滚吧 / 滚你的；
#: * 骂人的前缀 + 滚、且「滚」在分句末：给我滚 / 你们都滚 / 快滚 / 让他滚；
#: * 整个分句只有「滚」（「滚」「滚滚滚」「滚！」「爱卖不卖，滚」）；连着三个及以上的「滚」。
_GUN_SHAPES: tuple[str, ...] = (
    r"滚(?:蛋|开|犊子|粗|远|一边|出去|出(?![来货])|回去|回家|回老家|吧|啊|呀|啦|你)",
    f"(?:你|你们|他|他们|都|快|赶紧|赶快|马上|立刻|给我|给老子|叫他|让他|让你|请你)滚+{_END}",
    f"{_START}滚+{_END}",
    r"滚{3,}",
)
_GUN_RE = "|".join(f"(?:{p})" for p in _GUN_SHAPES)

#: 其余几个「骂人的说法是闭集、无害的说法是开集」的短词，只认这些句型（p13 T173 复核 L2-1 /
#: L3-1）。句型之外（「孕妈的防辐射服」「孩子他妈」「气炸锅」「去死皮」「为什么破了」）一律不算。
INSULT_SHAPES: dict[str, tuple[str, ...]] = {
    HANDOFF_ANGER: (
        # 你 / 他 / 她 / 尼 +（个）（老）妈 / 妈妈 / 娘 + 的：你妈的、他妈的、你老妈的、你妈妈的、
        # 他妈妈的、你个老妈的。前面是「给 / 帮 / 替 / 送 / 陪 / 为」的是所有格（买给她妈的围巾）。
        r"(?<![给帮替送陪为和跟])[你他她尼](?:个)?老?(?:妈妈?|娘)的",
        # 去 / 操 / 草 / 日 + 你（老）妈：去你妈、操你老妈、去你妈妈的
        r"(?:去|操|草|日|艹)[你他她尼](?:个)?老?(?:妈|娘)",
        # 他妈当副词（他妈就知道拖、真他妈、他妈逼）；「孩子他妈 / 老公他妈 / 其他妈咪 / 他妈妈说」不是
        r"(?<![其子公婆])他妈(?![妈咪])",
        # 分句开头的「妈的」（妈的，快递又没动）；前面不是「我 / 给……」、后面紧跟着骂人时常跟的
        # 副词或分句结束的「妈的」（这快递妈的太慢了）。「宝妈的奶粉」「我妈的外套」不是。
        f"{_START}妈的",
        f"(?<![我俺咱给帮替送为])妈的(?:{_END}|(?=[太真就又怎什简气烦]))",
        # 气炸了 / 气炸我了 / 快气炸 / 被气炸；「气炸锅 / 气炸烤箱 / 气炸电烤箱」不是
        f"气炸(?:{_END}|(?=[了我啦肺死]))", r"(?<=[快要都真被给把])气炸",
        # 去死吧 / 去死 / 你去死 / 都去死；「去死皮 / 去死垢 / 去死海 / 去死亡谷」不是
        f"去死(?:{_END}|(?=[吧啊呀了]))", r"(?<=[你他她们都快滚])去死",
        # 什么破 + 名词（什么破东西 / 破快递 / 破手机）；「为什么破了」、破损 / 破洞 / 破壁机 /
        # 破窗器 / 破冰这些以「破」起头的词不是
        r"(?<!为)什么破(?![损洞裂口壁皮碎掉了开窗冰绽解成旧产])",
    ),
}

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


#: 复合词白名单（p13 T173）：夹着「按子串认」的触发词、但整个词**不可能是诉求**的商品名 / 物件。
#: 规范化之后、匹配之前整段遮掉（:func:`_mask`）。按「被误伤的原因」分组只为可读 —— 遮是对
#: 所有原因一起遮的（「曝光补偿」同时夹着 complaint 的「曝光」与 compensation 的「补偿」）。
#: 写成规范化后的形态（小写、无空白）。**只收拿得准的**：拿不准的留给「宁可多转」。
#: 只按句型认的几个词（滚、妈的、他妈、气炸、去死、什么破）不在这里 —— 句型之外本来就不算。
BENIGN_COMPOUNDS: dict[str, tuple[str, ...]] = {
    HANDOFF_ANGER: (
        # 垃圾：家居清洁类商品（「垃圾车 / 垃圾站 / 垃圾处理」不收：「你们就是垃圾站」「垃圾处理方式」
        # 是骂人的说法，复核 L2-5；厨下的「垃圾处理器」是商品，收整词）
        "垃圾袋", "垃圾桶", "垃圾篓", "垃圾箱", "垃圾筐", "垃圾篮",
        "垃圾分类", "垃圾处理器", "垃圾粉碎", "垃圾夹", "垃圾铲", "垃圾收纳", "垃圾清运",
        # 气死：老式马灯
        "气死风灯",
        # 恶心：药品 / 晕车贴的功效说明
        "缓解恶心", "止恶心", "防恶心",
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

#: 复合词的**上下文写法**（p13 T173 复核 L2-6 / L3-1 / L3-3）：按「触发词 + 前后是哪一类词」写成
#: 正则，在规范化后的正文上与 :data:`BENIGN_COMPOUNDS` 一起遮。只遮触发词那几个字：前后文用环视判；
#: 前后文不定长时把触发词写成命名组 ``w``，只遮这一组。同样「只收拿得准的」：「垃圾站 / 垃圾车」
#: 「恶心死了」「恶心人」「精神补偿」「经济补偿」「差价补偿」一律不在内。
BENIGN_PATTERNS: dict[str, tuple[str, ...]] = {
    HANDOFF_ANGER: (
        # 垃圾 + 盛放 / 清理的物件（垃圾筒、垃圾挂袋、垃圾兜、垃圾架、垃圾拾取器）
        r"垃圾(?=[袋桶筒篓箱筐篮夹铲兜盒挂罐架钳斗]|(?:拾|捡)取?[器夹钳])",
        # 空气 + 死（空气死角）
        r"空气(?=死)",
        # 恶心是身体不适：前面是止 / 缓解 / 孕吐 / 晕车……，后面是想吐 / 呕吐 / 反胃 / 头晕……，
        # 或者同一分句里前面有「吃 / 喝 / 服 / 闻 + 了 / 完 / 过 / 着」（吃了这个有点恶心）；
        # 「恶心死 / 恶心人」照认
        r"(?:缓解|止|防|预防|减轻|孕吐|晕车|晕船|晕机|反胃|容易)恶心",
        r"会(?:不会)?恶心(?=[吗么嘛呢不啊呀]|$)",
        r"恶心(?=想吐|呕吐|干呕|反胃|头晕|头疼|头痛|胸闷|腹泻|拉肚子|犯困|乏力)",
        rf"(?:吃|喝|服|闻)(?:了|完|过|着)[^{_PUNCT}]{{0,8}}?(?P<w>恶心)(?![死人])",
        # 黑店 + 村镇街路（地名）
        r"黑店(?=村|镇|乡|街|路|庄|屯|寨|桥|湾)",
    ),
    HANDOFF_COMPENSATION: (
        # 补偿前面是技术参数（背光补偿、梯形补偿、功率补偿），或后面是元件 / 设置
        r"(?:背光|逆光|曝光|补光|侧光|低音|高音|温度|运动|梯形|功率|相位|色温|无功|线损|动态|"
        r"增益|电压|电流|频率|角度|延迟|重力|零点|温漂|畸变|视差|亮度|抖动|光学|白平衡|色彩)(?P<w>补偿)",
        r"补偿(?=器|导线|电容|电路|功能|模式|值|参数|系数|算法|装置|网络|开关|精度|档位|"
        r"怎么(?:设置|打开|开启|关闭|关掉|调))",
    ),
    HANDOFF_REQUESTED: (
        # 人工 + 材料 / 景观 / 医疗器件 / 渔具（人工鱼饵、人工降雨、人工关节）；「人工智能」不在内
        r"人工(?=草|湖|饵|鱼饵|晶|钻|宝石|水晶|皮|革|合成|养殖|种植|培育|降雨|心脏|关节|骨|牙|"
        r"湿地|瀑布|景观|假山|泪液|耳蜗|费)",
    ),
    HANDOFF_PRIVACY: (
        # 隐私 / 身份证 + 遮挡与收纳的物件（隐私门帘、身份证卡套）
        r"隐私(?=门?帘|膜|屏|玻璃|贴|窗|挡板|围挡|防护)",
        r"身份证(?=卡套|套|夹|包|壳|保护|收纳)",
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
    parts.extend(INSULT_SHAPES.get(reason, ()))
    return re.compile("|".join(f"(?:{p})" for p in parts))


_PATTERNS: dict[str, re.Pattern[str]] = {reason: _pattern_for(reason) for reason in PRIORITY}

_EN_RES: dict[str, re.Pattern[str]] = {
    reason: re.compile(_EN_BOUNDARY.format("|".join(f"(?:{p})" for p in EN_PATTERNS[reason])))
    for reason in PRIORITY
}

#: 中文白名单：整词（长的先遮）一条正则、上下文写法各一条。环视看的都是**遮之前**的原文
#: （每条都在同一份规范化文本上找，找完一起遮）。
_BENIGN_RES: tuple[re.Pattern[str], ...] = (
    re.compile("|".join(re.escape(w) for w in sorted(
        {w for ws in BENIGN_COMPOUNDS.values() for w in ws}, key=lambda w: (-len(w), w)))),
    *(re.compile(p) for ps in BENIGN_PATTERNS.values() for p in ps),
)
_EN_BENIGN_RE = re.compile(_EN_BOUNDARY.format("|".join(
    [re.escape(w) for w in sorted(set(EN_BENIGN_COMPOUNDS), key=lambda w: (-len(w), w))]
    + [f"(?:{p})" for p in EN_BENIGN_PATTERNS])))


def _benign_spans(text: str) -> list[tuple[int, int]]:
    """白名单在 ``text`` 上认出的、要遮掉的各段：写了命名组 ``w`` 的只遮那一组。"""
    spans: list[tuple[int, int]] = []
    for pattern in _BENIGN_RES:
        for m in pattern.finditer(text):
            if "w" in pattern.groupindex and m.group("w") is not None:
                spans.append(m.span("w"))
            elif m.end() > m.start():
                spans.append(m.span())
    return spans


def _mask_spans(text: str, spans) -> str:
    chars = list(text)
    for start, end in spans:
        for i in range(start, end):
            chars[i] = _MASK_CHAR
    return "".join(chars)


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


def _clauses(text: str) -> str:
    """同 :func:`normalize`，但每一处空白换成一个逗号：句型里的「分句」认空格隔开的（「爱卖不卖 滚」）。"""
    return re.sub(r"\s+", ",", _spaced(text).strip())


def _spaced_words(text: str) -> str:
    """英文匹配用的那份：同 :func:`_spaced`，再把弯引号换成直引号、连续空格压成一个。"""
    return re.sub(" +", " ", _spaced(text).replace("’", "'").replace("‘", "'")).strip()


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
    """这句话命中的全部原因，按优先级从高到低排。一个都不中返回空元组。

    中文在两份文本上认：去掉空白的那份（其余一切），与空白换成逗号的那份（只给句型用：
    「爱卖不卖 滚」的「滚」在分句末）。两份都先遮白名单。
    """
    norm = normalize(text)
    if not norm:
        return ()
    masked = _mask_spans(norm, _benign_spans(norm))
    clauses = _clauses(text)
    clauses = _mask_spans(clauses, _benign_spans(clauses))
    words = _mask(_EN_BENIGN_RE, _spaced_words(text))
    hit = []
    for reason in PRIORITY:
        if (_PATTERNS[reason].search(masked) or _EN_RES[reason].search(words)
                or (reason in _SHAPE_RES and _SHAPE_RES[reason].search(clauses))):
            hit.append(reason)
        elif reason == HANDOFF_ANGER and _EXCLAIM_RE.search(norm):
            hit.append(reason)
        elif reason == HANDOFF_COMPLAINT and _HOTLINE_SPACED_RE.search(_spaced(text)):
            hit.append(reason)
    return tuple(hit)


#: 只按句型认的那几条（含「滚」），在空白换成逗号的那份文本上再认一次。
_SHAPE_RES: dict[str, re.Pattern[str]] = {
    HANDOFF_ANGER: re.compile("|".join(
        f"(?:{p})" for p in (_GUN_RE, *INSULT_SHAPES[HANDOFF_ANGER]))),
}


def detect(text: str) -> tuple[str, str] | None:
    """``(转人工原因, 本轮意图)``；不触发返回 None。多类同中取优先级最高的一类。"""
    reasons = matched_reasons(text)
    if not reasons:
        return None
    return reasons[0], INTENT_OF[reasons[0]]
