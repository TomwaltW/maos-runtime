"""话术检索（p12 契约 §1.4 T168）：客户说的一句话 → 按分数排好的几篇标准话术。

两段，前一段不是本模块自己的：

1. **召回走知识层的两阶段检索**（``maos.kb.retriever.retrieve``）：租户硬约束、
   ``biz_type='cs'``、``kind='cs_script'``，关键词就是客户原文。不另写一条直查
   ``kb_doc`` 的 SQL —— 绕开 ``retrieve`` 就绕开了租户那条硬约束，而那不报错。
   四通道权重点名给（``_RECALL_WEIGHTS``），不读退款侧调的 ``MAOS_KB_WEIGHTS``。
2. **重排是本模块的**：对召回的每篇话术，拿客户原文与该篇的「适用场景 + 同义词 + 例句」
   逐条算字符二元组重合度，见 :func:`script_score`。知识层的四通道分数是为退款规划调的
   （规则编号、错误码两条精确通道在这里恒为 0），直接拿来当命中门槛会把门槛定在噪声上。

**短句与虚词**（复核 L2-1）：「没有了」「可以」「这个多少钱」这类短句只有一两个二元组，
其中一个常见二元组（什么 / 怎么 / 可以 / 没有 / 多少）碰巧落在某篇例句里，未加权的覆盖率
就是 0.5–1.0，一句闲聊就被答成一篇话术。所以重排分对二元组分了轻重（虚词二元组几乎不计），
短句的覆盖率有分母下限，实词二元组只对上一个时整体打对折 —— 一个二元组碰上算巧合，两个才算证据。

**零模型**（契约 §0）：重排是纯函数，同一句话、同一份话术库，分数逐位相同。

**客户原文不进审计行**（契约 R5）：落 ``KbRetrieved`` 时 ``detail.query.keyword``
换成 ``types.text_digest(text)``；日志里也不打原文。

## 意图提示（p13 契约 §1.4 T173：``intent_hint``）

``match_scripts(..., intent_hint="")`` 缺省时**逐字节同 p12**（返回值、``KbRetrieved`` 都一样，
测试在 p12 开发集全部轮上比对）。给了意图（理解层 ``cs.understand`` 判出的，取自 ``types.INTENTS``）
就进「提示模式」，只改排序与分数，不改召回：

1. **该意图的话术优先**：每篇该意图的话术在 p12 重排分上加 :data:`HINT_BONUS`。相近说法落在
   同一意图的几篇之间时不受影响（同加），跨意图抢答时该意图的先上；p12 分数略低于门槛、但
   理解层已认出意图的改写，由它推过门槛 —— 这是「换个说法就落兜底」那一类的解法之一。
2. **时长线索 vs 进度线索**（:data:`INTENT_CUES`，数据表）：问规则时长（「多久 / 几天能 /
   多长时间 / 什么时候」）与查某一笔进度（「到没到 / 退了吗 / 发了没 / 到哪了」）共享实词（「钱退回来」），
   二元组重排分不开它们。提示模式下，原文带时长线索时，该意图里**不转人工**的政策篇加
   :data:`CUE_BONUS`、带 ``needs_order_lookup`` 的查单篇减同样的量；带进度线索时反过来；两种都带
   时按进度算（「好几天了还没到」是在催这一单）。开发集 CS12-044#1 就是这一类。
3. 只加减**该意图**的话术；分数夹在 [0, 1]、六位小数。同分时先比 p12 原分（原样命中的那篇
   仍然第一），再比知识层分、doc_id。``ScriptHit.score`` 与 ``KbRetrieved`` 里记的都是调整后的分，
   ``detail.query`` 另记 ``intent_hint`` 与 ``cue``（都是枚举，不含客户原文）。

``intent_hint`` 给 ``unknown`` 时没有可优先的话术，排序同缺省；给了 ``INTENTS`` 之外的值抛
``ValueError``（上游该先夹到枚举里，悄悄当成缺省会把接线错误藏起来）。

## 同义归一（p14 T182：``SYNONYM_RULES`` / :func:`greeting_only`）

**只在原句检不到时启用**（重排后的最高分低于 :data:`MIN_SCRIPT_SCORE`，本该落兜底）：原句本来就
检得到的，返回值与 ``KbRetrieved`` 逐字节同 p12 / p13。启用时把口语说法换成话术库里**登记过的
同义词**（整句寒暄 → 「你好」；「包裹在中转站躺了四天」→「物流没更新」；「吊牌没摘能退吗」→
「七天无理由能退吗」……），拿归一后的句子再召回一次、再打一次分，每篇取两次里高的那个。
规则按类别写（寒暄、具体订单的进度 / 异常、政策问答的口语说法），每条带「整句必须有 / 不许有」
的线索，拿不准的一律不归一（原句照旧落兜底）。``KbRetrieved.detail.query`` 多记一个
``synonym_norm``（命中的规则名，枚举），不记归一后的句子。话术库本身一条没加（增补棘轮归 T169
的测试管，见 docs/DECISIONS.md task-t182）。
"""

from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from typing import Any

from maos import kb
from maos.domain.cs import types as T
from maos.kb import retriever

log = logging.getLogger("maos.cs")

#: 命中门槛，[0, 1]。``hits[0].score >= MIN_SCRIPT_SCORE`` 才算「找得到话术」，
#: 否则前台走兜底（契约 §1.4 判定顺序第 4、5 步）。
#:
#: 取值依据（task-t168 复核轮实测，``scenarios/cs/kb/cs_scripts_holdout.json``；
#: 复核轮补进去的短对话轮与近域句先于本轮调参写成）：无关句 79 条（远域 18 + 短对话轮 /
#: 售前 / 账号 / 夸奖等近域 61）+ 英文售后 4 条，最高分 0.208（「好的」→ GEN-002），
#: 其次 0.200（「这个多少钱」→ LOG-003）；76 条改写（每篇 3 条首版 + 1 条短口语）
#: top-1 正确 74 条（0.974），其中分数 ≥ 0.25 的 63 条（0.829）；首版那 57 条是 56 / 49。
#: 取 0.25：比无关句最高分高约 0.04 的余量 —— 门槛贴着无关句定，一句没见过的闲聊就可能
#: 被答成某篇话术；说法没覆盖到的改写落到兜底，代价只是多一轮追问（连续兜底由前台转人工）。
#: 首版的重排口径（不分虚实、无分母下限、无对折）在同一份话术库与无关句上有 24 条 ≥ 0.25
#: （最高 0.75），见 docs/DECISIONS.md。
MIN_SCRIPT_SCORE = 0.25

#: 重排分数里两项的权重：最像的那一条说法（加权 Dice）与整篇说法对客户原文的加权覆盖率。
#: 只用前者，短问句会被一条同样短的例句带跑；只用后者，长句里几个常见二元组就能凑出分。
_BEST_WEIGHT = 0.5
_COVER_WEIGHT = 0.5

#: 虚词字表。**两个字都在表里**的二元组是虚词二元组（什么 / 怎么 / 可以 / 没有 / 多少 /
#: 这个 / 知道 ……），权重 ``_FUNCTION_WEIGHT``；其余二元组（只要有一个字不在表里，如
#: 「包邮」「退款」「多久」「你好」）是实词二元组，权重 1。表按字、不按词：闭集，
#: 不随话术库增长。「好」不在表里 —— 它是「你好 / 您好」的实词，放进去问候就检不到了。
_FUNCTION_CHARS = frozenset(
    "我你您他她它们咱这那哪谁啥此"                          # 代词
    "什么怎咋为几多少"                                      # 疑问
    "吗呢吧啊呀嘛哦哈啦拉呗哇喔嗯噢哎诶欸呐哟亲"            # 语气、称呼
    "的地得了着过是有没不也都还就才又再很太挺真最更在给把被让和跟与或及"  # 助词、副词、介词
    "要想会能可以该请一个下些点儿里上边样"                  # 助动词、量词、方位
    "做弄搞办说问看知道行对来去东西时候般然后现"            # 泛义动词、泛指
)
_FUNCTION_WEIGHT = 0.1

#: 覆盖率的分母下限（按二元组权重计）。客户原文的权重和不足它时按它算 ——
#: 一两个二元组的短句不能靠「我仅有的那个二元组对上了」拿满覆盖率。
_MIN_QUERY_MASS = 3.0

#: 实词二元组要对上几个才给满分；只对上一个时整体按比例打折（1 / 2），一个都没对上为 0。
_MIN_CONTENT_HITS = 2

#: 句尾语气词。客户原文与某条说法**去掉句尾语气词后逐字相等**（且至少剩两个字）也算
#: 原样命中：「你好啊」就是登记过的「你好」，「在吗亲」就是「在吗」。只用于这一条判定，
#: 不参与二元组打分；去到只剩一个字就停（「在呢」与「在吗」都去成「在」纯属巧合），见 :func:`tail_forms`。
_TAIL_PARTICLES = frozenset("啊呀吗嘛呢吧哦哈啦拉呗哇亲么噢喔嗯哟呐")

#: 召回用的四通道权重，**点名给、不读配置**（复核 L2-A）。``retrieve`` 缺省读
#: ``MAOS_KB_WEIGHTS``（受治理的配置项，env 或 Nacos），那一套是为退款规划调的；
#: 而阶段二丢掉加权和 <= 0 的文档 —— 退款侧把 fts 调成 0，话术就整篇召不回来，
#: 重排连看都看不到，本该满分命中的一句变成兜底。所以这里的召回与那个旋钮脱钩：
#: 两条精确通道对话术恒无信号，权重 0；全文通道按字召回（``kb.tokenize`` 中文按字切），
#: 客户原文与一篇话术只要共享一个汉字就进候选，而重排分 > 0 至少要共享一个二元组 ——
#: 话术库的说法里没有英数字（task-t168 复核轮实查 296 条），共享的二元组必含汉字，
#: 于是重排能打出分的每一篇都召得回来。向量通道也给 1：它的分只作重排同分时的次序。
#: ``KbRetrieved.detail.weights`` 仍是 ``retriever.weights_snapshot()`` 读到的配置值
#: （``emit_kb_retrieved`` 不收权重参数，知识层不归本轨改），见 docs/DECISIONS.md。
_RECALL_WEIGHTS = {"rule_no": 0.0, "gateway_code": 0.0, "fts": 1.0, "vector": 1.0}

#: 提示模式下给 ``intent_hint`` 那一意图的每篇话术加的分（见模块头「意图提示」）。
#: 取值依据见 docs/DECISIONS.md task-t173：T168 的 holdout 无关句 + 英文句在「理解层给什么提示
#: 就按什么提示」下全部仍低于门槛，改写句过门槛且判对的比例上升。
HINT_BONUS = 0.10

#: 时长 / 进度线索在该意图内部把政策篇与查单篇拉开的量（一加一减，两篇之间差 2 倍）。
CUE_BONUS = 0.08

CUE_DURATION = "duration"   # 问规则时长：政策篇优先
CUE_PROGRESS = "progress"   # 查某一笔进度：查单篇优先
CUES = (CUE_DURATION, CUE_PROGRESS)

#: 意图线索（数据表）：线索 → 规范化文本（NFKC、小写、空白压成一个空格）上的正则片段。
#: 这里是中文片段（按字认），英文在 :data:`EN_INTENT_CUES`。**进度优先**：两类都中按进度算。
INTENT_CUES: dict[str, tuple[str, ...]] = {
    CUE_DURATION: (
        r"多久(?!了)", r"多长时间(?!了)", r"多少天(?!了)", r"(?<!好)几天(?!了)", r"几个工作日", r"几个小时",
        r"多快", r"(?<![尽赶])快(?:吗|嘛|么|不快)", r"时效", r"一般要几", r"大概要几",
        # 同一类问法的别的说法（复核 L3-2）：几日 / 多少时间 / 周期是多长 / 多少个工作日
        r"(?<!好)几日(?!了)", r"多少时间", r"多长(?![了时])", r"多少个?工作日",
        # 问「什么时候」（复核 L2-2 / L3-2）：查单只说得出状态、说不出到账 / 送达时间（p13 契约 §0
        # 不买），问时间的句子该由政策篇答，也不该被当成「要退款」
        r"什么时候", r"啥时候", r"何时", r"几时", r"几号", r"哪天", r"多会儿?",
        r"\bhow long\b", r"\bhow many days\b", r"\bhow soon\b", r"\bhow quickly\b",
    ),
    CUE_PROGRESS: (
        r"到没到", r"到了没", r"到了吗", r"到账了?没", r"到账了吗", r"到帐了?没", r"到帐了吗",
        r"退了没", r"退了吗", r"退回来没", r"退回来了吗", r"回来了没", r"回来了吗",
        r"收到没", r"收到了没", r"收到了吗", r"发了没", r"发了吗", r"发货了?没", r"发货了吗",
        r"发出了?吗", r"发出了?没", r"发走了?没", r"寄出了?吗", r"寄出了?没", r"寄了吗", r"出库了?吗",
        r"到哪了", r"到哪儿了", r"到哪里了", r"到哪一步", r"走到哪", r"进度", r"怎么样了",
        r"有结果了?吗", r"有结果了?没", r"处理了?没", r"处理好了?吗", r"处理好了?没",
        # 「审核 / 通过 / 成功 + 吗」要带「了 / 过」才是问这一笔（复核 L3-2：「退款需要审核吗」是问规则）
        r"审核(?:了|过了?|完了?|好了?)吗", r"审核了?没", r"审核过了?没", r"通过了?没", r"通过了吗",
        r"批下来",
        r"了没有",
        r"还没到", r"还没收到", r"还没发", r"还没退", r"一直没", r"没动静", r"怎么还不", r"怎么还没",
        # 「到现在都没到 / 至今没收到 / 都一周了还没退」（复核 L3-2）
        r"(?:还|都|一直|仍然?|至今|到现在|现在)(?:都|还)?没(?:有)?(?:到|收到|退|发|动|更新|消息|回|处理|结果)",
        r"成功了吗", r"成功了?没", r"好了(?:吗|没)", r"是不是已经",
        # 「退 / 到 / 发 / 寄 / 收 / 回 …… 了吗 / 了没」：退到卡里了吗、寄出去了没、东西到了吗
        r"[退到发寄收回][^,，。?？!！]{0,4}了(?:吗|没|么)",
    ),
}

#: 英文的意图线索：写成不带边界的片段，统一包上「前后不挨 ASCII 字母数字」（中英混排也认得出）。
EN_INTENT_CUES: dict[str, tuple[str, ...]] = {
    CUE_DURATION: (r"how long", r"how many (?:business |working )?days", r"how soon",
                   r"how quickly", r"how many hours",
                   r"when (?:will|does|do|can|would|could|should|is|are)",
                   r"when (?:\w+ ){1,3}(?:will|arrive|arrives|come|comes)"),
    CUE_PROGRESS: (r"where(?:'s| is) my", r"has my", r"status of my", r"track my",
                   r"(?:refund|order|return|exchange|delivery|shipping|shipment|package|parcel) status",
                   r"tracking (?:number|info|information)", r"not (?:arrived|received)",
                   r"still (?:not|hasn't|haven't|no)", r"did (?:you|it) (?:ship|arrive)",
                   r"is my \w+ (?:shipped|delivered|refunded|processed|approved|credited)",
                   r"(?:hasn't|has not|haven't|have not|didn't|did not) (?:arrived?|received?|come|got|"
                   r"gotten|shown up|been (?:received|refunded|delivered|shipped|processed|credited))"),
}

_CUE_RES: dict[str, re.Pattern[str]] = {
    cue: re.compile("|".join(
        [f"(?:{p})" for p in INTENT_CUES[cue]]
        + [f"(?<![a-z0-9])(?:{p})(?![a-z0-9])" for p in EN_INTENT_CUES[cue]]))
    for cue in CUES
}


#: 进度线索那一条正则（理解层要知道线索落在句子哪儿：「都没收到货给我退款」里线索在办事说法前面）。
PROGRESS_CUE_RE: re.Pattern[str] = _CUE_RES[CUE_PROGRESS]


def _cue_text(text: str) -> str:
    """线索匹配用的规范化：逐字 NFKC、小写、空白压成一个空格（英文要靠空格认词）。"""
    s = unicodedata.normalize("NFKC", text or "").lower().replace("’", "'")
    return re.sub(r"\s+", " ", s).strip()


def detect_cue(text: str) -> str:
    """原文带的意图线索：``progress`` / ``duration`` / ``""``。两类都中按进度算。"""
    norm = _cue_text(text)
    if _CUE_RES[CUE_PROGRESS].search(norm):
        return CUE_PROGRESS
    if _CUE_RES[CUE_DURATION].search(norm):
        return CUE_DURATION
    return ""


# ---------------------------------------------------------------------------
# 同义归一（p14 T182）：原句检不到话术时，把口语说法换成话术库里登记过的规范说法再检一次
# ---------------------------------------------------------------------------
#: 寒暄的说法（整句只由它们、称呼与语气词组成时，这句就是一句问候）。写成规范化后的形态
#: （NFKC、小写、去掉空白与标点）。叠字（「在吗在吗」「哈喽哈喽」「喂喂」）、波浪号、语气词都靠
#: :func:`greeting_only` 逐段吃掉，不在这里逐个列。
GREETING_WORDS: tuple[str, ...] = (
    "你好", "您好", "你们好", "大家好", "在吗", "在么", "在嘛", "在嘞", "在不在", "在没在", "在不", "在没",
    "在没有", "在的吗", "在线吗", "还在吗", "还在么", "有人吗", "有人么", "有人嘛", "有人没", "有人不",
    "有人在吗", "有人在不", "有人在么", "有人在没", "有人在", "有没有人", "哈喽", "哈罗", "哈啰", "嗨", "嘿",
    "早", "早安", "早上好", "上午好", "中午好", "下午好", "晚上好", "打扰一下",
    "打扰下", "打扰了", "打扰啦", "打扰您了", "打扰你了", "打扰", "喂", "请问",
)
#: 寒暄里常带的称呼（单独出现不算问候，要跟一个 :data:`GREETING_WORDS` 同句）。
GREETING_ADDRESS: tuple[str, ...] = (
    "亲", "亲亲", "亲爱的", "老板", "老板娘", "掌柜", "掌柜的", "店家", "店主", "卖家", "商家", "客服",
    "小二", "美女", "帅哥", "小姐姐", "小哥哥", "小哥", "宝", "宝宝", "家人们", "朋友",
)
#: 寒暄里夹的语气词（单字）。
GREETING_PARTICLES = frozenset("啊呀吗嘛呢吧哦哈啦呗哇喔嗯噢哎诶欸呐哟么咯嘞")
_GREETING_TOKENS: tuple[tuple[str, bool], ...] = tuple(sorted(
    {(w, True) for w in GREETING_WORDS} | {(w, False) for w in GREETING_ADDRESS},
    key=lambda t: (-len(t[0]), t[0])))
#: 整句寒暄最多这么多个字（规范化后）：再长就不是一句单纯的招呼了。
GREETING_MAX_CHARS = 16
#: 寒暄归一成的规范说法（问候篇登记过的同义词）。
GREETING_CANONICAL = "你好"


def _greeting_text(text: str) -> str:
    """寒暄判定用的规范化：NFKC、小写，只留汉字与 ASCII 字母（标点、空白、波浪号、表情、数字都去掉）。"""
    s = unicodedata.normalize("NFKC", text or "").lower()
    return "".join(c for c in s if ("a" <= c <= "z") or "一" <= c <= "鿿")


def greeting_only(text: str) -> bool:
    """整句只是一句寒暄：从头到尾都吃得成问候词、称呼、语气词，且至少有一个问候词。

    「在吗在吗～」「亲在不在呀」「哈喽哈喽」「打扰一下哈」「老板在吗」是；「在吗，包邮吗」
    「你好，我要退货」「哈哈」「老板」不是（客套 + 真问题照旧按真问题检，见 T169 的客套开场守卫）。
    """
    s = _greeting_text(text)
    if not s or len(s) > GREETING_MAX_CHARS:
        return False
    i, greeted = 0, False
    while i < len(s):
        for word, is_greeting in _GREETING_TOKENS:
            if s.startswith(word, i):
                i += len(word)
                greeted = greeted or is_greeting
                break
        else:
            if s[i] in GREETING_PARTICLES:
                i += 1
                continue
            return False
    return greeted


#: 分句内的「若干个字」（不跨句号、问号、感叹号）。
_W = r"[^。.!！?？]"
#: 分句内的「若干个字」（不跨任何标点）。
_C = r"[^,，。.!！?？;；]"

#: 同义归一规则：(名字, 口语说法的正则, 规范说法, 整句必须有的线索, 整句不许有的线索)。
#: 在规范化文本（NFKC、小写、去空白）上匹配；命中的那一段换成规范说法 —— 规范说法都是话术库里
#: 登记过的同义词。名字是枚举（进 KbRetrieved.detail.query.synonym_norm，不含客户原文）。
#: 类别对应 p14 契约 §2 T182 的误判类别 2（具体订单的进度 / 异常）与 3（政策问答的口语说法）。
SYNONYM_RULES: tuple[tuple[str, str, str, str, str], ...] = (
    # ---- 类别 2：具体订单的进度 / 异常（话术都是 needs_order_lookup 的查单篇）----
    ("stalled", rf"(?:卡在|停在|滞留在?|压在|躺在|困在|一直在|堵在){_C}{{0,8}}?"
                r"(?:中转|转运|分拣|集散|网点|仓库?|站点|海关|路上|物流中心|快递点|营业部)"
                rf"|(?:中转|转运|分拣|集散|网点|站点|海关|物流中心|快递点|营业部){_C}{{0,6}}?"
                r"(?:放|停|卡|搁|压|躺|待|呆|滞留)了?(?:[0-9一二三四五六七八九十两好几多]+天|一周|一个星期|好久|很久)"
                rf"|(?:物流|快递|包裹|单号|轨迹|件|信息){_C}{{0,8}}?(?:不动|不走|不更新|没更新|没有更新|没变化"
                r"|没变|没动静|没动|停了|停住|卡住|卡了|没反应|没进展|一动不动)"
                rf"|(?:好几天|几天|多天|一周|一个星期|一个礼拜|[0-9一二三四五六七八九十两]+天){_C}{{0,6}}?"
                r"(?:不动|不走|不更新|没更新|没有更新|没变化|没动静|没动|停着|卡着|一动不动)",
     "物流没更新", "", r"多久|多长时间|一般|通常"),
    ("address", r"(?:改|修改|更改|变更|换)(?:一下|下|个)?(?:收货|收件|寄送|配送|送货)?地址"
                r"|(?:改|修改|更改|变更|换)(?:一下|下|个)?(?:收货|收件)(?:人|电话|信息)"
                rf"|地址{_C}{{0,3}}(?:填|写|选|弄|输|留)(?:错|成|反|到|的是)"
                r"|(?:寄|送|发)(?:到|去)?(?:另外|别的|其他|新)(?:一个|的)?(?:地方|地址)",
     "改地址", "", ""),
    ("signed_missing", r"(?:签收|已签|签了|送达|妥投|派送成功|投递成功|放(?:在|到)?了?(?:驿站|快递柜|丰巢|菜鸟"
                       rf"|门口|门卫|前台|快递点|代收点)){_W}{{0,14}}?(?:没(?:有)?(?:收到|拿到|见到|看到|找到"
                       r"|取到|到手)|没人(?:收到|拿到|签收|收过?)|找不到|没找到|不见了?|没看见|没有(?=$|[,，。!！?？呀啊呢]))"
                       rf"|(?:没|未)(?:收到|拿到){_W}{{0,10}}?(?:显示|却|但|可)(?:是|说)?(?:已经?)?(?:签收|已签|送达)",
     "显示签收没收到", "", ""),
    ("lost", rf"(?:快递|包裹|件|东西|货){_C}{{0,4}}?(?:丢了|弄丢|寄丢|丢失|不见了|找不到了)",
     "包裹丢了", "", ""),
    ("damaged", rf"(?:箱子|盒子|纸箱|外箱|外包装|包装盒?|快递盒|快递袋|袋子|包裹){_C}{{0,4}}?"
                r"(?:破了?|烂了?|扁了?|压扁|压坏|压烂|湿了|裂开|变形|凹|瘪|碎)"
                r"|(?:拆开|打开|开箱|收到|到手|拿到|取出来|寄过来|寄来|送过来|送来|送到|寄到)"
                rf"{_W}{{0,10}}?(?:碎了|碎成|摔碎|摔坏|摔裂|压碎|压坏"
                r"|压扁|压变形|破了|烂了|裂了|破损|漏了|洒了|撒了|碎的|破的|烂的|裂的)",
     "包裹破损", "", r"换|质量|用了|用就|一用|用过|穿"),
    ("refund_missing", r"(?:同意|答应|说好|说了|已经|显示|审核通过|通过了?)"
                       rf"{_W}{{0,6}}?退(?:款|钱|我|给我)?{_W}{{0,12}}?(?:没(?:有)?(?:到|收到|回来|见着|见到"
                       r"|到账|到帐|动静|看到)|不见|没退)"
                       rf"|(?:退款|退的钱|退回的钱|钱){_C}{{0,8}}?(?:一直|还|迟迟|始终|都)(?:没|没有|不见)"
                       r"(?:到|收到|回来|见着|见到|到账|到帐|动静|退)"
                       rf"|退{_W}{{0,10}}?钱(?:呢|在哪|去哪|哪去|跑哪)",
     "退款还没到账", "", ""),
    ("double_charge", r"扣(?:了)?(?:我)?(?:两|2|二|俩|多|好几|三)(?:次|笔|回|遍|下|份|倍)"
                      r"|(?:多|重复|重)(?:扣|收)(?:了)?(?:我)?(?:钱|款|费|一次|一笔)?"
                      r"|(?:付|支付|交|收)(?:了)?(?:两|2|二|俩)(?:次|笔|回|遍)",
     "重复扣款", "", ""),
    ("paid_unpaid", rf"(?:扣了?|付了|支付了|扣钱了|付过|钱已经?(?:扣|付|出)了?)(?:钱|款)?{_W}{{0,12}}?"
                    rf"(?:订单|单子|页面|显示|状态){_W}{{0,6}}?(?:没|未|待|还是)(?:付|支付|成功|生成|下单|到账)",
     "扣款了订单没成功", "", ""),
    # ---- 类别 3：政策问答的口语说法（话术都是不转人工的政策篇）----
    ("seven_day", rf"(?:吊牌|标签|标牌|水洗标|商标){_C}{{0,3}}?(?:还在|没拆|没剪|没摘|没撕|没动|完好|都在|剪了"
                  r"|摘了|拆了|撕了)"
                  rf"|(?:拆了|拆开了?|撕了|打开了?|拆封了?){_C}{{0,3}}?(?:外膜|膜|塑封|包装|外包装|封条|封口|盒子"
                  rf"|袋子)|(?:外膜|塑封|包装|外包装|封条|封口|盒子){_C}{{0,3}}?(?:拆了|拆开|撕了|打开|拆封|没拆"
                  r"|没打开|没撕)"
                  r"|(?:没|未|还没)(?:穿|用|戴|洗|拆|使用|试)过?",
     "七天无理由", r"退", r"坏|质量|破|碎|裂|掉色|褪色|问题|瑕疵|发错|少发|漏发|多久|几天|到账"),
    ("return_steps", rf"(?:先|第一步|首先){_C}{{0,10}}?(?:还是|再|然后|之后)"
                     r"|(?:第一步|先|首先|一开始)(?:要|该|得)?(?:干嘛|干啥|做什么|做啥|怎么做|咋做|弄什么)"
                     r"|先后|顺序|步骤|怎么个流程|啥流程|什么流程|要干嘛|要干啥|该干嘛|该干啥|咋弄|咋整|怎么弄"
                     r"|怎么搞|怎么办理|怎么操作",
     "退货流程", r"退|寄回|售后", r"坏|碎|破|质量|运费|邮费|快递费|多久|几天|到账"),
    ("return_fee", r"(?:运费|邮费|快递费|运输费|寄费|邮资|来回的?(?:钱|费用)|寄回(?:去|来)?的?(?:钱|费用))"
                   rf"{_C}{{0,8}}?(?:谁|我|自己|你们|商家|卖家|店家|买家|报销|承担|负责|出|付|掏|返)"
                   rf"|(?:要我|我要|要自己|得自己|需要自己|我自己)(?:掏钱|出钱|花钱|付钱|承担){_C}{{0,6}}?(?:寄|退)",
     "退货运费谁出", r"退|寄回", ""),
    ("quality_exchange", r"掉色|褪色|染色|串色|起球|开线|脱线|变形|缩水|开胶|脱胶|起皱|发黄|生锈|漏水|漏电|坏了"
                         r"|坏掉|断了|裂了|裂开|有问题|质量问题|质量不好|质量差|有瑕疵|瑕疵|破洞|有洞|不亮|不响"
                         r"|没声音|充不进电|开不了机|失灵|异响|有异味|掉漆|脱落",
     "质量问题换货", r"换", r"地址|进度|到哪|审核|多久|几天|快递|物流"),
    ("delivery_scope", r"(?:发|送|寄|配送|派送|发货)(?:得|的)?(?:到|去|往)(?![哪了货付])"
                       rf"{_C}{{1,8}}?(?:吗|么|嘛|不|没)(?=$|[,，。!！?？呀啊呢])"
                       r"|(?:偏远|乡下|农村|乡镇|村里|山区|国外|海外|境外|港澳台?|香港|澳门|台湾|西藏|新疆|内蒙古?"
                       rf"|青海|宁夏|甘肃|海南|岛上){_C}{{0,6}}?(?:能|可以|发|送|配送|到|寄)",
     "能不能送到", "", r"了吗|了没|到哪|什么时候|多久|几天|啥时候|几号|哪天|明天|今天|后天|地址|包邮|运费|邮费"),
    ("courier", r"(?:什么|哪家|哪个|啥|哪种|哪一家|哪些)(?:快递|物流|快运|配送|货运)"
                r"|(?:能|可以|可不可以|能不能|支持)(?:指定|选|选择|换|改)(?:个|一下)?(?:快递|物流)"
                r"|(?:发|走|用)(?:顺丰|京东|ems|邮政|中通|圆通|申通|韵达|德邦|极兔|百世)"
                rf"|大件(?:商品|家具|家电|物品|东西)?{_C}{{0,4}}?(?:走|用|发|送)",
     "用哪家快递", "", r"到哪|了吗|了没|单号|查|进度|多久|几天|什么时候|运费|邮费|包邮"),
    ("ship_time", rf"(?:最快|最早|最迟|最晚|一般|大概|大约)(?:哪天|哪一天|几号|什么时候|啥时候|多久|几天|何时){_C}{{0,4}}?"
                  rf"(?:发|寄|出库|出货)|(?:今天|今晚|明天|当天)(?:下单|拍|买|付款)?{_C}{{0,6}}?(?:能|可以|会)?"
                  r"(?:发|寄|出库)(?:货|出|走)?(?:吗|么|嘛|不)",
     "什么时候发货", "", r"我的|我那|我这|那单|这单|订单|单号|了吗|了没|还没|怎么还"),
    ("refund_route", rf"退(?:款|的钱|回的钱|回来的钱|钱)?{_C}{{0,6}}?(?:退到|退回|回到|返回|原路|到)"
                     r"(?:哪|哪里|哪儿|什么地方|原来|原|余额|零钱|银行卡|卡上|卡里|花呗|信用卡|钱包|账户|支付宝|微信)",
     "退款退到哪", "", r"了吗|了没|到账了|没到|还没|怎么还|一直没"),
    ("wallet_duration", r"(?:钱包|零钱|余额|花呗|信用卡|银行卡|储蓄卡|微信|支付宝|云闪付|白条)(?:支付|付款|付的|付)?"
                        rf"{_C}{{0,8}}?(?:多久|几天|多长时间|什么时候|啥时候|几个工作日|多少天)",
     "退款多久到", r"退", r"了吗|了没|还没|怎么还|一直没"),
)
_SYNONYM_RES: tuple[tuple[str, re.Pattern[str], str, re.Pattern[str] | None, re.Pattern[str] | None], ...] = tuple(
    (name, re.compile(p), canon, re.compile(need) if need else None, re.compile(avoid) if avoid else None)
    for name, p, canon, need, avoid in SYNONYM_RULES)

#: 属于「具体订单的进度 / 异常」的规则（理解层据此把诉求判成查进度，见 :func:`order_anomaly`）。
ORDER_ANOMALY_RULES = frozenset({"stalled", "signed_missing", "lost", "damaged", "refund_missing",
                                 "double_charge", "paid_unpaid"})


def _norm_text(text: str) -> str:
    """同义归一用的规范化：NFKC、小写、去掉空白与格式字符（保留标点：规则按分句看）。"""
    s = unicodedata.normalize("NFKC", text or "").lower()
    return "".join(c for c in s if not c.isspace() and unicodedata.category(c) != "Cf")


def synonym_hits(text: str) -> tuple[str, ...]:
    """原文命中的同义归一规则名（按 :data:`SYNONYM_RULES` 的先后）。整句寒暄记 ``greeting``。"""
    if greeting_only(text):
        return ("greeting",)
    s = _norm_text(text)
    return tuple(name for name, pat, _c, need, avoid in _SYNONYM_RES
                 if pat.search(s) and (need is None or need.search(s))
                 and (avoid is None or not avoid.search(s)))


def normalized_query(text: str) -> tuple[str, tuple[str, ...]]:
    """(归一后的句子, 命中的规则名)。没有命中返回 ``("", ())``。

    整句寒暄 → :data:`GREETING_CANONICAL`；否则每条命中的规则把它匹配到的那几段换成规范说法。
    """
    names = synonym_hits(text)
    if not names:
        return "", ()
    if names == ("greeting",):
        return GREETING_CANONICAL, names
    s = _norm_text(text)
    for name, pat, canon, _need, _avoid in _SYNONYM_RES:
        if name in names:
            s = pat.sub(canon, s)
    return s, names


def order_anomaly(text: str) -> bool:
    """原文说的是某一单的进度 / 异常（卡在中转站不动、签收没收到、丢件破损、退款没到、重复扣款）。"""
    return any(name in ORDER_ANOMALY_RULES for name in synonym_hits(text))


def _cue_sign(cue: str, body: dict) -> int:
    """线索与这篇话术对不对得上：+1 对上、-1 相反、0 不相干。"""
    if not cue:
        return 0
    lookup = (body.get("handoff") or "") == T.HANDOFF_NEEDS_ORDER_LOOKUP
    policy = not (body.get("handoff") or "")
    if cue == CUE_DURATION:
        return 1 if policy else (-1 if lookup else 0)
    return 1 if lookup else (-1 if policy else 0)


def hinted_score(score: float, body: dict, *, intent_hint: str, cue: str) -> float:
    """提示模式下的分：该意图加 :data:`HINT_BONUS`，再按线索加减 :data:`CUE_BONUS`；夹到 [0, 1]。

    不是该意图的话术原分返回（``intent_hint`` 为 unknown 时一篇都不动）。
    """
    if body.get("intent") != intent_hint:
        return score
    adj = score + HINT_BONUS + CUE_BONUS * _cue_sign(cue, body)
    return round(min(1.0, max(0.0, adj)), 6)


def _grams(text: str) -> frozenset[str]:
    """字符二元组。先过 ``kb.tokenize``（英数按词、中文按字，丢标点与空白、转小写）。

    规整后只剩一个字时给那一个字：二元组集合为空会让它与任何话术都算不出分，
    而一个字（「嗨」）与另一个字的整句相同也确实该算重合。
    """
    s = "".join(kb.tokenize(text))
    if len(s) < 2:
        return frozenset({s}) if s else frozenset()
    return frozenset(s[i:i + 2] for i in range(len(s) - 1))


def _norm(text: str) -> str:
    return "".join(kb.tokenize(text))


def tail_forms(text: str) -> frozenset[str]:
    """规整后的原句，加上逐个去掉句尾语气词得到的各个形式，去到只剩两个字为止。

    「包邮吗亲」→ {包邮吗亲, 包邮吗, 包邮}；「在吗亲」→ {在吗亲, 在吗}（「在」只剩一个字，不要）；
    「在吗」→ {在吗}。两句的形式有交集，就算同一种说法。生成器拿它查跨篇撞车。
    """
    s = _norm(text)
    forms = {s} if s else set()
    while len(s) > 2 and s[-1] in _TAIL_PARTICLES:
        s = s[:-1]
        forms.add(s)
    return frozenset(forms)


def _is_content(gram: str) -> bool:
    """实词二元组：至少一个字不在虚词字表里。单字（整句只剩一个字时）按那个字判。"""
    return any(ch not in _FUNCTION_CHARS for ch in gram)


def _mass(grams: frozenset[str]) -> float:
    return sum(1.0 if _is_content(g) else _FUNCTION_WEIGHT for g in grams)


def _dice(a: frozenset[str], b: frozenset[str]) -> float:
    """加权 Dice：2 × 交集权重 / (两边权重和)。"""
    inter = a & b
    if not inter:
        return 0.0
    return 2.0 * _mass(inter) / (_mass(a) + _mass(b))


def _variants(body: dict) -> list[str]:
    """一篇话术用来比对的全部说法：适用场景 + 同义词 + 例句。标准话术本身不算 ——
    它是我们要说的话，不是客户会怎么问。"""
    out = [str(body.get("scene") or "")]
    out.extend(str(s) for s in body.get("synonyms") or ())
    out.extend(str(s) for s in body.get("examples") or ())
    return [v for v in out if v]


def script_score(text: str, body: dict) -> float:
    """客户原文对一篇话术的重排分，[0, 1]，六位小数。

    * 原文就是该篇登记过的一种说法（:func:`tail_forms` 有交集：规整后逐字相等，
      或两边去掉句尾语气词后逐字相等且至少两个字）：1。
    * 否则 ``(0.5 × best + 0.5 × cover) × evidence``，二元组按 :func:`_mass` 加权
      （实词 1、虚词 ``_FUNCTION_WEIGHT``）：

      - ``best``：与最像的那一条说法的加权 Dice；
      - ``cover``：原文二元组落在该篇全部说法里的权重 ÷ max(原文权重和, ``_MIN_QUERY_MASS``)；
      - ``evidence``：落在该篇里的实词二元组个数 ÷ ``_MIN_CONTENT_HITS``，封顶 1。
    """
    query = _grams(text)
    if not query:
        return 0.0
    variants = _variants(body)
    if not variants:
        return 0.0
    forms = tail_forms(text)
    if any(forms & tail_forms(v) for v in variants):
        return 1.0
    variant_grams = [_grams(v) for v in variants]
    best = max(_dice(query, g) for g in variant_grams)
    matched = query & frozenset().union(*variant_grams)
    cover = _mass(matched) / max(_mass(query), _MIN_QUERY_MASS)
    evidence = min(1.0, sum(1 for g in matched if _is_content(g)) / _MIN_CONTENT_HITS)
    return round((_BEST_WEIGHT * best + _COVER_WEIGHT * cover) * evidence, 6)


def _usable_body(doc: dict) -> dict | None:
    """取一篇话术的 body；形状不对的返回 None（跳过并告警，不让一篇坏话术拖垮整轮）。"""
    try:
        body = json.loads(doc.get("body") or "")
    except (TypeError, ValueError):
        body = None
    if not isinstance(body, dict):
        log.warning("话术 %s 的 body 不是 JSON 对象，跳过", doc.get("doc_id"))
        return None
    intent = body.get("intent")
    handoff = body.get("handoff") or ""
    if intent not in T.INTENTS or (handoff and handoff not in T.HANDOFF_REASONS) \
            or not str(body.get("script") or "").strip():
        log.warning("话术 %s 的 intent / handoff / script 不合契约，跳过", doc.get("doc_id"))
        return None
    return body


def _score_all(recalled: list[dict], text: str, normalized: str, *, intent_hint: str,
               cue: str) -> list[tuple[float, float, float, str, dict, dict]]:
    """逐篇打分并排好：(出门的分, 原分, 知识层分, doc_id, hit, body)。

    ``normalized`` 非空（同义归一启用）时原分取原句与归一句两次里高的那个。缺省模式下出门的分
    就是原分，排序键退化成 p12 的 (-原分, -知识层分, doc_id)：逐字节同 p12。
    """
    scored: list[tuple[float, float, float, str, dict, dict]] = []
    for hit in recalled:
        doc = hit.get("doc") or {}
        if doc.get("biz_type") != T.BIZ_TYPE_CS:
            continue
        body = _usable_body(doc)
        if body is None:
            continue
        score = script_score(text, body)
        if normalized:
            score = max(score, script_score(normalized, body))
        if score <= 0:
            continue
        final = hinted_score(score, body, intent_hint=intent_hint, cue=cue) \
            if intent_hint else score
        scored.append((final, score, float(hit.get("score") or 0.0), hit["doc_id"], hit, body))
    # 同分先看 p12 原分（原样命中的那篇仍然第一），再看知识层的分，再看 doc_id：
    # 次序必须确定（「连跑两次输出一致」）。
    scored.sort(key=lambda s: (-s[0], -s[1], -s[2], s[3]))
    return scored


def match_scripts(store: Any, *, tenant_id: str, text: str, plan_id: str, task_id: str,
                  limit: int = 3, intent_hint: str = "") -> list[T.ScriptHit]:
    """检索话术，按 score 降序返回至多 ``limit`` 篇（重排分为 0 的不返回）。

    ``intent_hint``（p13）：缺省空串时逐字节同 p12；给了就进提示模式（见模块头「意图提示」），
    不在 ``types.INTENTS`` 里抛 ``ValueError``。

    * 只检 ``kind='cs_script'`` 且 ``biz_type='cs'`` 的文档：查询带 ``biz_type='cs'``
      过阶段一，召回后再逐篇核一次 ``biz_type`` —— 阶段一把文档侧 NULL 当通配，
      一篇漏写 biz_type 的话术会混进来，而话术库的约定是 biz_type 恒非空。
    * KB 关着（``MAOS_KB_ENABLED=0``）或没有 store：返回 ``[]``，**一条事件都不落**
      （口径同 ``retriever.retrieve_and_log``：关掉就是没检过）。
    * 否则**恰好落一条** ``KbRetrieved``：plan_id / task_id 照传、trace_id 恒空串
      （契约 §1.2）；``detail.docs`` 就是返回的这几篇，分数是重排分（与返回值逐篇相等）；
      ``detail.query.keyword`` 是 ``text_digest(text)``，客户原文不进 event_log。
      检不到也落（``docs: []``）—— 检索发生过本身就是事实。
    * ``detail.candidate_count`` 记的是知识层召回、进入重排的篇数。
    """
    if intent_hint and intent_hint not in T.INTENTS:
        raise ValueError(f"intent_hint 必须取自 types.INTENTS，收到 {intent_hint!r}")
    if store is None or not kb.kb_enabled():
        return []
    started = time.perf_counter()
    query = {"tenant_id": tenant_id, "biz_type": T.BIZ_TYPE_CS, "keyword": text}
    # limit 放到候选集上限：重排要看到全部召回，知识层的截断只按它自己的分数排。
    # 权重点名给：召回不随退款侧的 MAOS_KB_WEIGHTS 漂（见 _RECALL_WEIGHTS）。
    recalled = retriever.retrieve(store, query, limit=retriever.MAX_CANDIDATES,
                                  weights=dict(_RECALL_WEIGHTS), kinds=(T.CS_KB_KIND,))
    cue = detect_cue(text) if intent_hint else ""

    scored = _score_all(recalled, text, "", intent_hint=intent_hint, cue=cue)
    # 同义归一（p14 T182）：原句的最高分够不着门槛（本该落兜底）时才启用 —— 已经检得到的句子
    # 逐字节同 p12 / p13。把口语说法换成规范说法再召回一次、再打一次分，每篇取两次里高的那个。
    synonym_norm: tuple[str, ...] = ()
    if not scored or scored[0][0] < MIN_SCRIPT_SCORE:
        normalized, names = normalized_query(text)
        if normalized:
            synonym_norm = names
            extra = retriever.retrieve(store, {**query, "keyword": normalized},
                                       limit=retriever.MAX_CANDIDATES,
                                       weights=dict(_RECALL_WEIGHTS), kinds=(T.CS_KB_KIND,))
            seen = {h.get("doc_id") for h in recalled}
            recalled = list(recalled) + [h for h in extra if h.get("doc_id") not in seen]
            scored = _score_all(recalled, text, normalized, intent_hint=intent_hint, cue=cue)
    top = scored[:max(0, int(limit))]

    hits = [
        T.ScriptHit(
            doc_id=doc_id,
            scheme_no=str(body.get("scheme_no") or hit["doc"].get("rule_no") or ""),
            intent=str(body["intent"]),
            score=final,
            script=str(body["script"]),
            principle=str(body.get("principle") or ""),
            handoff=str(body.get("handoff") or ""),
        )
        for final, _score, _kb_score, doc_id, hit, body in top
    ]
    logged_query = {**query, "keyword": T.text_digest(text)}
    if intent_hint:
        logged_query.update({"intent_hint": intent_hint, "cue": cue})
    if synonym_norm:
        # 只记规则名（枚举），不记归一后的句子（那里夹着客户原文的其余部分）。
        logged_query["synonym_norm"] = list(synonym_norm)
    retriever.emit_kb_retrieved(
        store,
        [{"doc_id": h.doc_id, "score": h.score, "title": hit.get("title", ""),
          "kind": hit.get("kind"), "channels": hit.get("channels", {})}
         for h, (_f, _s, _k, _d, hit, _b) in zip(hits, top)],
        query=logged_query,
        plan_id=plan_id, task_id=task_id, trace_id="",
        duration_ms=(time.perf_counter() - started) * 1000,
        candidate_count=len(recalled),
    )
    return hits
