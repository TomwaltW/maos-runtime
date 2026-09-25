"""p13 T173 · 理解层：语种、槽位、意图（契约 review/p13-cs-contracts.md §1.4 T173、§2 第 0 / 4 / 7 步）。

守七件事（主会话裁定收敛轮 R1–R5 的机器判据都在这里）：

1. **语种**（``lang.detect_lang``）：有 CJK 即 zh；否则（拿掉单号这类编码串后）字母里 ASCII 字母
   严格过半即 en；否则 zh。
2. **槽位**（``understand.extract_slots``）：订单号（R3）—— 纯数字 ≥ 8 位、字母开头的字母数字混排
   （总长 ≥ 5，不要单号字眼）、带连字符的平台单号都认；p13 开发集里带夹具单号的轮全对；手机号（含
   +86 / 86 前缀、连字符、空格）、座机、400 / 800 热线、日期、型号、卡号 / QQ / 身份证号不认；紧挨单号
   字眼的，后面跟着品类名词（「订单号 A1001 手机上……」）也是单号。
   诉求（R2，回到第二轮口径）—— 有诉求词就给；问规则而没有行动标记的不给；客户撤回给 other，
   质问 / 转述商家不退、店家一直不办（还 / 一直 / 迟迟 + 不退）、A 不 A 的催问、第三方撤的都不算撤回；
   先抱怨没到、后明说要办的是要办；复核的 22 句真诉求、自写的政策问题与撤回句按门槛钉住。
   商品与问题只从词表来；跨轮合并新值覆盖旧值。
3. **意图**：触发词 → 诉求 → 关键词（R4：意图示例表整张删掉）；「<商品>什么时候能到」不会被拉成
   refund_payment；输出恒在 ``types.INTENTS``。
4. **触发词**（R1）：p12（bf53df6）判出的，p13 一律判出、优先级不低 —— 唯一例外是触发词完全落在
   封闭的名词表里、且句中没有别的触发词；≥ 80 句真说法、名词表逐条、开发集与这几组句子上的性质
   测试；名词表的收词规矩（触发词外至少多两个字、「垃圾 / 曝光 / 补偿」打头的不收、触发词接上常见
   邻词照 p12 判）逐条钉住。p12 的判定结果是拿 bf53df6 的 triggers.py 实跑出来、以字面量写进本文件的
   （不依赖 git 历史）。
5. **match_scripts 的 intent_hint**：缺省时与 p12（bf53df6 的 scripts.py）逐字节一致 —— 在 p12
   开发集全部轮上比对返回值与 KbRetrieved；给了提示时，时长 / 进度线索把政策篇与查单篇分开
   （自写 22 句正反例），开发集接上提示后满分（含 CS12-044#1）。
6. **模型路径**：只在规则判不出、且注入真模型时调；记一行 usage（plan_id 照传、trace_id 空、
   task_id None）或一行 failure；Scripted / None 零行；输出夹到 INTENTS。
7. **不退步**（R5）：真前台（desk 本轨不改）跑开发集，route_hits / intent_hits 不低于基线；
   T168 的 holdout 在提示模式下，无关句仍全部低于门槛、问自己那一单状态的句子不被政策篇答掉。

只用开发集（scenarios/cs/eval/p12_cases.json）、T168 的 cs_scripts_holdout.json、T175 的 p13 开发集
（带夹具单号的轮，抄成字面量）、复核留下的探针句与本文件自写的句子；不读 p12 留出集。
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re

import pytest

from maos import kb
from maos.agents.base import AgentIdentity
from maos.core.store import SqliteStore
from maos.domain.cs import corpus, evaluate, lang, ports, scripts, triggers
from maos.domain.cs import types as T
from maos.domain.cs import understand as U
from maos.domain.cs.corpus import seed_cs_kb
from maos.domain.cs.desk import CsConfig, FrontDesk, retrieval_query
from maos.model.client import ModelClient, ModelResponse, ScriptedModelClient, Tier
from maos.obs.call_sites import CALL_SITE_CS_UNDERSTAND, REGISTERED_CALL_SITES
from maos.skills import registry
from maos.skills.invoker import SkillInvoker

ROOT_T173 = pathlib.Path(__file__).resolve().parents[2]
HOLDOUT_T168_PATH = ROOT_T173 / "scenarios" / "cs" / "kb" / "cs_scripts_holdout.json"
TENANT_T173 = "tnt-demo"
CONV_T173 = "csc-t173test00000000"
PLAN_T173 = T.plan_id_for(CONV_T173)
TURN_T173 = T.turn_id_for(CONV_T173, 1)


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def _seeded_t173() -> SqliteStore:
    store = SqliteStore()
    store.init_schema()
    kb.ensure_schema(store)
    seed_cs_kb(store)
    return store


def _match_t173(store, text: str, *, hint: str | None = None):
    kw = {} if hint is None else {"intent_hint": hint}
    return scripts.match_scripts(store, tenant_id=TENANT_T173, text=text, plan_id=PLAN_T173,
                                 task_id=TURN_T173, **kw)


def _top_t173(store, text: str, *, hint: str | None = None) -> tuple[str, float]:
    hits = _match_t173(store, text, hint=hint)
    return (hits[0].scheme_no, hits[0].score) if hits else ("", 0.0)


def _desk_factory_t173() -> FrontDesk:
    store = SqliteStore(":memory:")
    seed_cs_kb(store)
    return FrontDesk(store, CsConfig(tenants={"wk_eval": TENANT_T173}, handoff_target=None))


def _hinted_match_scripts_t173(monkeypatch) -> None:
    """模拟 §2 第 7 步的接线：检索时把 understand 的意图当 intent_hint 传给 match_scripts。
    desk.py 不归本轨（W-B 由 T174 接），这里只在测试里把 cs.answer 调的那个函数包一层。"""
    original = scripts.match_scripts

    def hinted(store, **kw):
        kw.setdefault("intent_hint", U.understand(kw["text"], prior_slots={}).intent)
        return original(store, **kw)

    monkeypatch.setattr(scripts, "match_scripts", hinted)


def _intent_of_scheme_t173() -> dict[str, str]:
    return {row["rule_no"]: json.loads(row["body"])["intent"] for row in corpus.load_corpus()}


def _handoff_of_scheme_t173() -> dict[str, str]:
    return {row["rule_no"]: json.loads(row["body"])["handoff"] for row in corpus.load_corpus()}


# ---------------------------------------------------------------------------
# 1. 语种
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text,want", [
    ("你好，我想问下退货", "zh"),                        # 纯中文
    ("包邮吗", "zh"),
    ("Where is my package?", "en"),                     # 纯英文
    ("how long does shipping take", "en"),
    ("ＨＩ，ｗｈｅｒｅ ｉｓ ｍｙ ｏｒｄｅｒ？", "en"),       # 全角字母与全角标点：NFKC 后是英文
    ("Hi，where is my order？", "en"),                   # 中文输入法的逗号问号不算 CJK
    ("我的 order A1001 到哪了", "zh"),                    # 混排：有一个汉字就是中文
    ("order A1001 到了吗", "zh"),
    ("A1001 where", "en"),                              # 编码串不数，剩下的 where 全是 ASCII 字母
    ("A1001", "zh"),                                    # 只回一个号：没有字母，回缺省
    ("AB12", "zh"),                                     # 夹着数字的编码串整段不数
    ("ABC12", "zh"),
    ("SO-2026-000123", "zh"),                           # 带连字符的单号同样是编码串
    ("A1001，谢谢", "zh"),
    # 复核 L1-2：长单号的数字不再压过英文单词（原先按「字母数字」数，这几句被判成中文）
    ("Where is order 2026092400123?", "en"),
    ("Order #20260924001234, where is it?", "en"),
    ("order 2026092400123456 status?", "en"),
    ("iPhone15 is broken", "en"),
    ("2026092400123", "zh"),                            # 纯数字
    ("？？？！！！", "zh"),                              # 纯符号
    ("😀😀", "zh"),                                      # 表情不计入分母
    ("OK!!!", "en"),                                    # 标点不计入分母
    ("", "zh"),                                         # 空串回缺省
    ("   ", "zh"),
    ("こんにちは", "zh"),                                # 假名算 CJK（只有 zh / en 两种）
    ("「A1001」", "zh"),                                 # CJK 标点区的引号
    ("café latte", "en"),                               # é 算字母但不是 ASCII：8/9 仍过半
    # 复核 L2-4：「严格过半」的边界 —— 恰好一半不是 en，多一个才是
    ("aé", "zh"), ("Éa", "zh"), ("ok да", "zh"),       # 1/2、1/2、2/4
    ("oké", "en"), ("ok да k", "en"),                   # 2/3、3/5
])
def test_detect_lang_boundaries_t173(text, want):
    assert lang.detect_lang(text) == want
    assert lang.detect_lang(text) in ports.LANGS


def test_detect_lang_constants_are_the_frozen_ones_t173():
    assert (lang.LANG_ZH, lang.LANG_EN) == (ports.LANG_ZH, ports.LANG_EN)


# ---------------------------------------------------------------------------
# 2. 槽位
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text,want", [
    ("订单号：2026092412345678 帮我查一下", "2026092412345678"),   # 纯数字 ≥ 8 位
    ("12345678到哪了", "12345678"),                                # 恰好 8 位
    ("单号 200924-1234567890123 到哪了", "200924-1234567890123"),  # 数字 + 连字符（拼多多式）
    ("SO-2026-000123 这单退了吧", "SO-2026-000123"),               # 字母数字混排带连字符
    ("我那单 A1001 发了没", "A1001"),                              # 短形态 + 「那单」
    ("order A1001 where is it", "A1001"),                          # 短形态 + order
    ("A1001", "A1001"),                                           # 整句就是一个号（追问后的回答）
    ("a1001。", "A1001"),                                         # 小写、带句号：转大写、去标点
    ("Ａ１００１", "A1001"),                                       # 全角：NFKC
    ("订单 #1001 能取消吗", "#1001"),                              # 「#」+ 数字 + 订单字眼
    ("SO-2026-000123 和 2026092400123 两单", "SO-2026-000123"),    # 多个取第一个
    ("A1001，谢谢", "A1001"),
    ("A1001 thanks", "A1001"),
    ("电话138-1234-5678，单号2026092400123", "2026092400123"),     # 手机号跳过，取后面的单号
    ("A1001 这单到哪了", "A1001"),
    ("A1001退款", "A1001"),
    ("订单号：A1001", "A1001"),
    # 复核 L3-4：身份证 / 银行卡形态**有**单号字眼时照认（平台单号也可能长这样）
    ("订单号 330102199001011234 到哪了", "330102199001011234"),
    ("330102199001011234 这单到哪了", "330102199001011234"),
    ("order id 20260924001234", "20260924001234"),
    # 主会话裁定 R3：短形态不要单号字眼也认（字母开头、字母数字混排、总长 ≥ 5）
    ("查一下A1001", "A1001"), ("A1001到哪了", "A1001"), ("我的是A1001", "A1001"),
    ("where is A1001", "A1001"), ("A1001 refund please", "A1001"), ("AB12345 发货了吗", "AB12345"),
    ("ORD12345 到哪了", "ORD12345"), ("SO-2026-0001 到了吗", "SO-2026-0001"),
    ("I'd like to order the MX5000, when will it ship?", "MX5000"),   # 型号与短单号字面上分不开
    # 紧挨单号字眼的优先（型号在前、单号在后）
    ("RTX4090显卡，订单号A1001", "A1001"),
    # 复核 L2-2：紧挨着单号字眼的，后面跟着品类名词（这单买的东西）也是单号，型号那条让路
    ("我那单 E5005 耳机坏了要退货", "E5005"), ("订单号 A1001 手机上显示已签收，但我没收到", "A1001"),
    ("单号 SO-2026-0001 手机上查不到", "SO-2026-0001"), ("订单号 JD20260924001 手机壳发错了", "JD20260924001"),
    ("单号 F6006 相机镜头裂了", "F6006"), ("订单 G7007 平板屏幕碎了", "G7007"),
    ("订单号A1001手机上查不到物流", "A1001"), ("我的订单 B2002 电脑上看不到", "B2002"),
    ("订单号：C3003款还没退回来", "C3003"), ("order A5001 phone case broken", "A5001"),
])
def test_order_no_shapes_are_recognised_t173(text, want):
    assert U.extract_slots(text, lang=lang.detect_lang(text)).get(ports.SLOT_ORDER_NO) == want


#: R3 的反例：手机号（含 +86 / 86 / 0086 前缀、带连字符或空格）、座机（连字符 / 括号 / 空格 / 不带分隔符）、
#: 400 / 800 热线。自写 18 句，判据是「全不抽」。
PHONE_NEGATIVES_T173 = [
    "我的手机号13812345678", "13812345678", "电话 138-1234-5678", "+86 138 1234 5678", "+8613812345678",
    "86-138-1234-5678", "(+86)13812345678", "0086 13812345678", "联系电话 138 1234 5678",
    "座机 0571-88886666", "座机 (0571)88886666", "打 021 62345678 找我", "客服热线 400-812-3123 打不通",
    "4008123123 没人接", "800-820-8820", "057188886666 这个座机", "我的电话是138-1234-5678，单号查一下",
    "+86-138-1234-5678",
]


@pytest.mark.parametrize("text", PHONE_NEGATIVES_T173)
def test_phone_numbers_are_never_order_numbers_t173(text):
    assert ports.SLOT_ORDER_NO not in U.extract_slots(text, lang=lang.detect_lang(text))


@pytest.mark.parametrize("text", [
    "2026-09-24 那天下的单",            # 日期
    "我买的iPhone15坏了",               # 型号：品牌驼峰写法
    "RTX4090显卡什么时候发",            # 型号：紧跟品类名词
    "我订单里的RTX4090显卡还没发",
    "华为Mate60什么时候发货",           # 数字不到 3 位
    "支持Wi-Fi6吗",
    "1234567到哪了",                    # 7 位纯数字
    "我打了12315",
    "GT-3 有货吗",                      # 连字符但太短
    "",
    # 复核 L2-6 / L3-4：离得最近的字眼说的是 QQ / 卡号 / 会员号 / 身份证；没字眼的身份证、过 Luhn 的银行卡
    "我的QQ号是123456789，退款到了吗",
    "退款退到银行卡6222021234567890123了吗",
    "我的会员号 20260924001 能积分吗",
    "退款退到这张卡 6222021234567890123 可以吗",
    "我卡号是6222021234567890123，钱退了没",
    "330102199001011234 这个是我的证件",
    "6222021234567890128",
    "请查一下 4111111111111111 到哪了",
    "我的微信号是abc12345",
])
def test_order_no_negatives_t173(text):
    assert ports.SLOT_ORDER_NO not in U.extract_slots(text, lang=lang.detect_lang(text))


def test_personal_number_shapes_are_real_near_misses_t173():
    """卡号 / 身份证的形态判据不空转：差一位校验、月份不合法的同长数字串照旧当单号认。"""
    assert U.extract_order_no("6222021234567890128") == ""              # 过 Luhn
    assert U.extract_order_no("6222021234567890123") == "6222021234567890123"   # 不过 Luhn
    assert U.extract_order_no("330102199001011234") == ""               # 合法年月日
    assert U.extract_order_no("330102199013011234") == "330102199013011234"     # 13 月
    # 离得最近的字眼说了算：卡号在前、单号紧挨着号 → 单号
    assert U.extract_order_no("卡号不对，订单号2026092400123") == "2026092400123"


#: R3 的正例：T175 的 p13 开发集（scenarios/cs/eval/p13_cases.json，task-t175 @ cd517ef）里正文含
#: 夹具单号（display_no）的全部 31 轮，逐字抄来。本轨分支上还没有那份文件（W-A 并行），所以写成字面量；
#: 合流之后文件在了，下一条测试还会拿真文件再比一遍。
P13_DEV_ORDER_TURNS_T173 = [
    ("帮我看下 A1001 这单发货了没有", "A1001"), ("订单 B2002 到哪一步了？怎么还看不到物流", "B2002"),
    ("帮我查查 C3003 现在是什么状态，物流一直没动静", "C3003"),
    ("Hi, could you check where my order A5001 is?", "A5001"),
    ("Where is my package? The order number is E5005.", "E5005"), ("F6006 这个单子的物流到哪了", "F6006"),
    ("Can you track order G7007 for me?", "G7007"), ("H8008 现在发到哪了", "H8008"),
    ("帮我看看 E3030 的快递现在到哪儿了", "E3030"), ("J1010 我昨天改了下颜色，现在发出来了吗", "J1010"),
    ("Has my order K1111 been sent out yet?", "K1111"), ("L1212 的快递到哪儿了", "L1212"),
    ("帮我查一下 M1313 物流进度", "M1313"), ("N1414 发货了吗？帮我查一下", "N1414"),
    ("我想知道 P1515 这单寄出来没有", "P1515"), ("单号是 R1717", "R1717"), ("订单号是 S1818", "S1818"),
    ("帮我查下 T1919 发货没有", "T1919"), ("Where is my order U2020?", "U2020"),
    ("V2121 这件外套尺码不合适，我要退货", "V2121"), ("W2222 我不想要了，帮我申请退款", "W2222"),
    ("X2323 收到的杯子有裂痕，我要退货", "X2323"), ("Y2424 我要退货", "Y2424"),
    ("那我的 Z2525 发出去了没", "Z2525"), ("好的，那 A2626 这单质量有问题，我要退货", "A2626"),
    ("我的订单号是 B2727", "B2727"), ("订单 C2828", "C2828"), ("D2929 都三天了还没发货，我要投诉你们", "D2929"),
    ("E3131 到底到哪了，直接帮我转人工", "E3131"), ("A1001 发货了吗", "A1001"), ("我想查一下 G3333 的物流", "G3333"),
]
P13_DEV_PATH_T173 = ROOT_T173 / "scenarios" / "cs" / "eval" / "p13_cases.json"


def _p13_dev_order_turns_t173() -> list[tuple[str, str]]:
    """p13 开发集（文件在的时候）里正文含夹具单号的轮：(原文, display_no)。"""
    data = json.loads(P13_DEV_PATH_T173.read_text(encoding="utf-8"))
    out = []
    for case in data["cases"]:
        fixtures = case.get("fixtures") or {}
        nos = {b["display_no"] for b in fixtures.get("bindings", [])} | set(fixtures.get("orders", {}))
        for turn in case["turns"]:
            out.extend((turn, no) for no in sorted(nos) if no and no in turn)
    return out


def test_p13_dev_set_order_numbers_are_all_extracted_t173():
    """R3 的判据：p13 开发集里带夹具单号的轮，抽出的 order_no 逐轮等于夹具的 display_no（全对）。"""
    turns = list(P13_DEV_ORDER_TURNS_T173)
    if P13_DEV_PATH_T173.exists():          # 合流之后：真文件里的轮一并比（不 skip，只是多比）
        turns += _p13_dev_order_turns_t173()
    assert len(P13_DEV_ORDER_TURNS_T173) == 31
    wrong = [(t, no, U.extract_slots(t, lang=lang.detect_lang(t)).get(ports.SLOT_ORDER_NO))
             for t, no in turns
             if U.extract_slots(t, lang=lang.detect_lang(t)).get(ports.SLOT_ORDER_NO) != no]
    assert wrong == []


# ---------------------------------------------------------------------------
# 2b. 诉求（主会话裁定 R2：回到第二轮 3831987 的口径）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text,want", [
    ("帮我把这单退了", ports.REQUEST_RETURN),
    ("我要退货", ports.REQUEST_RETURN),
    ("我要退款", ports.REQUEST_REFUND),
    ("给我退钱", ports.REQUEST_REFUND),
    ("帮我换个大一码的", ports.REQUEST_EXCHANGE),
    ("收到的是坏的，我要换货", ports.REQUEST_EXCHANGE),
    ("帮我查下物流", ports.REQUEST_TRACK),
    ("我的快递到哪了", ports.REQUEST_TRACK),
    ("退款到了没", ports.REQUEST_TRACK),                  # 有「退款」但问的是进度：track 不是 refund
    ("我的退货申请审核了没有", ports.REQUEST_TRACK),
    ("地址写错了能改吗", ports.REQUEST_OTHER),            # 改地址问一句「能改吗」就是要改
    ("I want a refund for order SO-2026-000123", ports.REQUEST_REFUND),
    ("where is my order A1001", ports.REQUEST_TRACK),
    ("my headphones arrived broken, I want an exchange", ports.REQUEST_EXCHANGE),
    # 复核 L2-1：明说要办的分句优先 —— 前一分句在催、后一分句在问规则都不改诉求
    ("东西一直没收到，我要退款", ports.REQUEST_REFUND),
    ("快递一直没到，帮我退款", ports.REQUEST_REFUND),
    ("I still haven't received it, I want a refund", ports.REQUEST_REFUND),
    ("等了一周还没发，给我退货", ports.REQUEST_RETURN),
    ("申请退款，要多久到账", ports.REQUEST_REFUND),
    ("我要退货，运费谁出", ports.REQUEST_RETURN),
    ("换货太麻烦了，我要退款", ports.REQUEST_REFUND),
    # 同一分句里在查、在问的，照旧是查进度
    ("我要查退款进度", ports.REQUEST_TRACK),
    ("帮我看看退款到账了没", ports.REQUEST_TRACK),
    ("给我退了没", ports.REQUEST_TRACK),
    ("我想知道退款到了没", ports.REQUEST_TRACK),
    ("my refund hasn't arrived yet", ports.REQUEST_TRACK),
    ("i haven't received my refund", ports.REQUEST_TRACK),
    ("is my refund processed", ports.REQUEST_TRACK),
    ("退款到现在都没到", ports.REQUEST_TRACK),
    ("Refund status?", ports.REQUEST_TRACK),
    # 有诉求词就给（第三轮的「缺省不给」撤掉）
    ("A1001退款", ports.REQUEST_REFUND),
    ("我那单退款", ports.REQUEST_REFUND),
    ("这单我要退货", ports.REQUEST_RETURN),
    ("收到的杯子有裂纹，能给我换个新的吗", ports.REQUEST_EXCHANGE),   # 问句，但有「给我」
    ("退款", ports.REQUEST_REFUND),
    ("退货吧", ports.REQUEST_RETURN),
    ("那就退货吧", ports.REQUEST_RETURN),
    ("还是退款吧", ports.REQUEST_REFUND),
    ("refund please", ports.REQUEST_REFUND),
    ("麻烦帮我把钱退了", ports.REQUEST_REFUND),            # 「把钱退」是退款，不是退货
    ("不要退款，给我换一件", ports.REQUEST_EXCHANGE),       # 撤回的是退款，换货照给
    ("不需要换货，直接退款", ports.REQUEST_REFUND),
    # 复核 L2-3：同一分句里先抱怨没到 / 没发（进度线索）、**后**明说要办 —— 是要办，不是查进度
    ("都没收到货给我退款", ports.REQUEST_REFUND),
    ("等了一周都没发货给我退款", ports.REQUEST_REFUND),
    ("到现在都没发货直接退款吧", ports.REQUEST_REFUND),
    ("都没收到货呢赶紧退钱", ports.REQUEST_REFUND),
    ("东西都没收到直接退款", ports.REQUEST_REFUND),
    ("申请退款了没", ports.REQUEST_TRACK),                # 线索在办事说法后面：照旧是查
])
def test_request_closed_set_t173(text, want):
    got = U.extract_slots(text, lang=lang.detect_lang(text)).get(ports.SLOT_REQUEST)
    assert got == want and got in ports.REQUEST_VALUES


#: 复核 final-fr4/probe_req.py 的 22 句真诉求（REAL_REQUESTS）与各自该给的诉求。
#: 「退货退款」两样都说了，给 return 或 refund 都算对（都走 RET-005 那条路）。
REVIEW_REAL_REQUESTS_T173 = [
    ("鞋子开胶了想退货", {"return"}), ("东西坏了，退款吧", {"refund"}), ("尺码小了，换大一码", {"exchange"}),
    ("衣服太大了想换小一号", {"exchange"}), ("质量太差，退款", {"refund"}), ("不喜欢，想退", {"return"}),
    ("收到就坏了，退钱", {"refund"}), ("颜色发错了，换货", {"exchange"}), ("退货，太难穿了", {"return"}),
    ("退款退款退款", {"refund"}), ("有质量问题，退货", {"return"}),
    ("The item is broken, refund please", {"refund"}), ("wrong size, exchange please", {"exchange"}),
    ("这个不合适要退货", {"return"}), ("裤子短了想换长一点的", {"exchange"}), ("杯子碎了，赶紧退款", {"refund"}),
    ("发错货了，要换货", {"exchange"}), ("东西有问题，退货退款", {"return", "refund"}),
    ("I want my money back", {"refund"}), ("Refund now", {"refund"}), ("退钱！", {"refund"}),
    ("马上退款", {"refund"}),
]


def test_review_real_requests_get_the_right_request_t173():
    """R2 的判据：复核那 22 句真诉求 ≥ 20 句给出正确的 request（实测 22 / 22）。"""
    assert len(REVIEW_REAL_REQUESTS_T173) == 22
    wrong = [(t, U.extract_request(t)) for t, want in REVIEW_REAL_REQUESTS_T173
             if U.extract_request(t) not in want]
    assert len(REVIEW_REAL_REQUESTS_T173) - len(wrong) >= 20, wrong
    assert wrong == [], wrong                     # 实测全对：退步一句就红，好让人看见


#: R2 的判据：自写的政策问题（问规则，**不含**行动标记）。≥ 17 句不许给 refund / return / exchange。
POLICY_QUESTIONS_T173 = [
    "退款一般几天能到账", "退货的运费谁来出", "七天无理由退货需要保留吊牌吗", "拆封了还能退货吗",
    "换货要重新付运费吗", "退款会退到原来的支付账户吗", "退货流程是怎样的", "退款有手续费吗",
    "换货的话多久能收到新的", "退货地址在哪里", "退款是全额退吗", "特价商品能退款吗",
    "退货需要自己叫快递吗", "换货可以换别的颜色吗", "退款审核要多长时间", "什么情况下不能退货",
    "过了七天还能退款吗", "退款会原路返回吗", "退货要自己寄吗", "退款是退到哪里",
    "How long does a refund take?", "Can I exchange an item bought on sale?", "Is return shipping free?",
    "What is your refund policy?", "Do you accept returns after 30 days?", "How do exchanges work?",
]
#: 行动标记（R2 列的那几个）—— 上面那张表里一个都不许有，否则判据就不是「不含行动标记」。
ACTION_MARKERS_T173 = ("我要", "帮我", "申请", "给我", "想", "麻烦", "马上", "赶紧", "now", "please")


def test_policy_questions_without_action_markers_carry_no_request_t173():
    assert len(POLICY_QUESTIONS_T173) >= 20
    for text in POLICY_QUESTIONS_T173:
        assert not [m for m in ACTION_MARKERS_T173 if m in text.lower()], text
    given = [(t, U.extract_request(t)) for t in POLICY_QUESTIONS_T173
             if U.extract_request(t) in (ports.REQUEST_REFUND, ports.REQUEST_RETURN, ports.REQUEST_EXCHANGE)]
    assert len(POLICY_QUESTIONS_T173) - len(given) >= 17, given
    assert given == [], given                     # 实测 26 / 26


@pytest.mark.parametrize("text", [
    # 第二轮起就钉着的问规则的句子
    "退款多久到账", "拆封了还能退吗", "坏了能换吗", "退货寄回去的运费是我出还是你们出", "Can I return this item?",
    "今天天气真好", "我的退款什么时候能到账", "退款啥时候到账", "退款何时到账", "退款几号能到",
    "我的退款什么时候退回来", "when will my refund arrive", "when will i get my refund",
    "退款会退运费吗", "退款有手续费吗", "退款要多少时间", "退款要等几日", "退款到账周期是多长", "退款快吗",
    "退款是全额退吗", "退货会影响我的信用分吗", "换货要重新付运费吗", "退款需要审核吗", "退款到账要等多长",
    "Is a refund free?", "Do refunds include shipping?", "退款要扣手续费吗", "退货需要自己出运费吗",
    "退款一般几日到账", "退款会退到原来的卡上吗", "退货包装需要完整吗", "Do I get a full refund?",
    "Is return shipping free?", "How fast is a refund?",
    # 「想问 / 想知道 / 麻烦问下 / 麻烦了」是问，不是办；「申请退款后」是个时间点；行动标记在诉求词后面的不算
    "我想问一下退款多久到账", "我想知道退货流程", "麻烦问下退货流程", "麻烦了，退款要多久",
    "申请退款后钱什么时候退回来", "退货我要付运费吗", "Please tell me how long a refund takes",
])
def test_policy_questions_carry_no_request_t173(text):
    """问规则的句子不给诉求：否则前台会为一句政策问题去要单号。"""
    assert ports.SLOT_REQUEST not in U.extract_slots(text, lang=lang.detect_lang(text))


@pytest.mark.parametrize("text,want", [
    ("我想退货怎么弄", ports.REQUEST_RETURN),            # 问办法，但「想」在诉求词前面：要退
    ("我要退款可以吗", ports.REQUEST_REFUND),
    ("能给我退款吗", ports.REQUEST_REFUND),
    ("可以帮我换个大一码吗", ports.REQUEST_EXCHANGE),
    ("赶紧给我退货，要多久", ports.REQUEST_RETURN),
    ("Can I get a refund please?", ports.REQUEST_REFUND),
    ("I need a refund, how long will it take?", ports.REQUEST_REFUND),
])
def test_policy_question_with_an_action_marker_is_a_request_t173(text, want):
    """R2 (a)：问规则的问法只在**没有**行动标记时才不给；有「我要 / 帮我 / 想 / please」就是要办。"""
    assert U.extract_request(text) == want


#: 复核 L2-2 的撤回句（前 6 句）与本轨事先写好的探针句（后 4 句）。
WITHDRAWN_T173 = ["不用退款了，东西我留着", "我不要退货了", "我不需要换货", "I don't want a refund",
                  "no need to refund", "退款不用了，谢谢",
                  "我不想退货了，东西挺好的", "算了不换了", "I don't need a refund anymore", "先不退了"]
#: R2 (b) 的判据：客户撤回（第一人称 / 无主语的「不用 / 不要 / 不需要 / 不想 / 先不 + 诉求词」、
#: 「诉求词 + 就不用了 / 算了」、「撤销 / 撤回 + 诉求词」、英文 no longer want / never mind / changed my
#: mind）。前 12 句来自复核 final-fr4/probe_req.py 的 WITHDRAW，其余自写。
WITHDRAWN_R2_T173 = [
    "我不想要退款了", "退款的事就算了", "我改主意了，不退货", "撤销退款申请", "A1001撤销退款",
    "I no longer want a refund for order A1001", "never mind the refund on my order",
    "I changed my mind, no refund for my order", "退款申请我撤回了", "这单我不退了", "我那单先不退款了",
    "不想退了", "东西挺好不想换了", "我不需要退款了谢谢", "那单退货取消吧", "算了，不退了",
    "I don't want to return my order anymore", "please cancel my refund", "我的订单不要退款",
    "A1001不退货", "这单退款的事算了", "我那单不想要退款了",
    # 复核 L3-1 之后（撤回要是客户自己开口）仍是撤回的：主语是客户、拿主意的说法、客气话收尾
    "我还是不退了", "我又不想退了", "我们不退了", "那就不退款了", "退款我已经撤回了", "换货就不用了谢谢",
    "never mind the return", "I changed my mind about the exchange",
]


@pytest.mark.parametrize("text", WITHDRAWN_T173 + WITHDRAWN_R2_T173)
def test_withdrawn_request_overwrites_the_earlier_one_t173(text):
    """撤回的诉求不算诉求；本轮没有别的诉求时给 other，盖掉上一轮的 refund（跨轮合并新值覆盖旧值）。"""
    assert U.extract_request(text) == ports.REQUEST_OTHER
    u = U.understand(text, prior_slots={ports.SLOT_ORDER_NO: "A1001",
                                        ports.SLOT_REQUEST: ports.REQUEST_REFUND})
    assert u.slots[ports.SLOT_REQUEST] == ports.REQUEST_OTHER
    assert u.slots[ports.SLOT_ORDER_NO] == "A1001"


#: 复核点名的「怪店家不退」7 句（final-fr4/refuse.txt 前 7 行）—— R2 的判据：一句都不许给 other。
BLAME_REVIEW_T173 = ["为什么不退了", "你们凭什么不退款了", "商家说不退了", "客服说不给退了", "怎么又不退了",
                     "店家不换了怎么办", "说好的退款怎么不退了"]


@pytest.mark.parametrize("text", BLAME_REVIEW_T173 + [
    "为什么不给我退款", "you don't refund anything", "they won't refund me", "我不想要了", "你们怎么还不退款",
    "you guys don't refund", "they said no refund", "卖家说退款不用了", "商家说了，不退了", "你们不退了是吧"])
def test_complaints_about_no_refund_are_not_withdrawals_t173(text):
    """质问 / 转述商家不退、「不想要了」（要退）都不是撤回。"""
    assert U.extract_request(text) != ports.REQUEST_OTHER


#: 复核 L3-1：不是客户自己撤回的说法 —— 店家一直不办（还 / 一直 / 迟迟 / 到现在 + 不退）、A 不 A 的催问
#: （退不退款）、「不要了 / 不想要了」本身是要退、诉求词不是 never mind / changed my mind 的宾语、
#: 第三方撤的（系统 / 谁 / 被）。一律不许给 other（第二轮 3831987 给 refund / return）。
NOT_WITHDRAWN_T173 = [
    "都三天了还不退款", "都三天了还不退款。", "申请一周了一直不退款，", "拖了半个月还不退款！", "到现在还不退款",
    "你们到底退不退款", "你们到底退不退货", "商品都寄回去了还不退钱", "迟迟不退款", "三天了都不退款",
    "一直不想给我退款", "这件衣服不想要了退货", "不要了退货吧", "不想要了退了吧", "我不要了退款",
    "Never mind, just refund me.", "Never mind the tracking, just give me a refund.",
    "Changed my mind, just refund it", "I changed my mind about the color, can I get a refund",
    "我的退款申请已经取消了，谁取消的", "系统自动取消退款申请了", "谁撤销我的退款申请", "退款申请被取消了",
]


@pytest.mark.parametrize("text", NOT_WITHDRAWN_T173)
def test_merchant_side_no_refund_is_not_a_withdrawal_t173(text):
    """R2 (b) 只认客户自己撤回：这些句子不给 other，也不盖掉上一轮的 refund。"""
    assert U.extract_request(text) != ports.REQUEST_OTHER
    u = U.understand(text, prior_slots={ports.SLOT_ORDER_NO: "A1001",
                                        ports.SLOT_REQUEST: ports.REQUEST_REFUND})
    assert u.slots[ports.SLOT_REQUEST] in (ports.REQUEST_REFUND, ports.REQUEST_RETURN), text


def test_blame_words_are_what_keep_those_from_being_withdrawals_t173(monkeypatch):
    """判据不空转：把「不是客户自己撤的」那几层（质问 / 转述 / 第三方、抱怨副词、客户这一方开口）
    关掉，复核那 7 句里至少 5 句、L3-1 那几句里至少一半会被当成撤回（给 other）。"""
    never = U.re.compile(r"(?!)")
    for name in ("_BLAME_ZH_RE", "_REPORTED_ZH_RE", "_BLAME_YOU_ZH_RE", "_COMPLAINT_ADVERB_RE"):
        monkeypatch.setattr(U, name, never)
    monkeypatch.setattr(U, "_OWN_SUBJECT_RE", U.re.compile(r""))
    flipped = [t for t in BLAME_REVIEW_T173 if U.extract_request(t) == ports.REQUEST_OTHER]
    assert len(flipped) >= 5, flipped
    zh = [t for t in NOT_WITHDRAWN_T173[:11]]
    flipped = [t for t in zh if U.extract_request(t) == ports.REQUEST_OTHER]
    assert len(flipped) * 2 >= len(zh), flipped


@pytest.mark.parametrize("text,want", [
    ("气死了！！！东西还没到", ports.EMOTION_ANGRY),     # 情绪触发词
    ("太过分了吧你们", ports.EMOTION_ANGRY),
    ("这也太ridiculous了", ports.EMOTION_ANGRY),
    ("等了好久了还没发，我挺着急的", ports.EMOTION_UPSET),
    ("I'm really disappointed", ports.EMOTION_UPSET),
    ("好的谢谢你", ports.EMOTION_CALM),
    ("不着急，你们慢慢查", ports.EMOTION_CALM),
])
def test_emotion_closed_set_t173(text, want):
    got = U.extract_slots(text, lang=lang.detect_lang(text)).get(ports.SLOT_EMOTION)
    assert got == want and got in ports.EMOTION_VALUES


def test_emotion_is_not_guessed_without_a_cue_t173():
    assert ports.SLOT_EMOTION not in U.extract_slots("运费怎么算", lang="zh")


def test_product_and_problem_come_only_from_the_lexicons_t173():
    slots = U.extract_slots("收到的杯子杯口有一条裂纹", lang="zh")
    assert (slots.get(ports.SLOT_PRODUCT), slots.get(ports.SLOT_PROBLEM)) == ("杯子", "裂纹")
    slots = U.extract_slots("my headphones arrived broken", lang="en")
    assert (slots.get(ports.SLOT_PRODUCT), slots.get(ports.SLOT_PROBLEM)) == ("headphones", "broken")
    slots = U.extract_slots("空气炸锅不能用了", lang="zh")               # 最长的先认
    assert slots.get(ports.SLOT_PRODUCT) == "空气炸锅"
    # 取不到就不给：不自由抽取（住址、手机号进不了槽位），「手机号」不是手机。
    for text in ("我住在杭州市西湖区文三路 100 号", "你们怎么拿到我手机号的", "运费怎么算"):
        slots = U.extract_slots(text, lang="zh")
        assert ports.SLOT_PRODUCT not in slots and ports.SLOT_PROBLEM not in slots, text


def test_all_slot_values_stay_in_their_closed_sets_over_the_dev_set_t173():
    for case in evaluate.load_cases():
        for text in case.turns:
            slots = U.extract_slots(text, lang=lang.detect_lang(text))
            assert set(slots) <= set(ports.SLOT_KEYS), text
            assert all(isinstance(v, str) and v for v in slots.values()), text
            assert slots.get(ports.SLOT_REQUEST, "refund") in ports.REQUEST_VALUES, text
            assert slots.get(ports.SLOT_EMOTION, "calm") in ports.EMOTION_VALUES, text


def test_prior_slots_merge_new_values_win_t173():
    prior = {ports.SLOT_ORDER_NO: "A1001", ports.SLOT_PRODUCT: "外套",
             ports.SLOT_EMOTION: ports.EMOTION_CALM}
    u = U.understand("气死了，外套和鞋子都没到，A2002 那单也查一下", prior_slots=prior)
    assert u.slots[ports.SLOT_ORDER_NO] == "A2002"          # 新值覆盖
    assert u.slots[ports.SLOT_EMOTION] == ports.EMOTION_ANGRY
    assert u.slots[ports.SLOT_PRODUCT] == "外套"
    # 本轮没说到的槽位原样带着。
    u2 = U.understand("好的", prior_slots={ports.SLOT_ORDER_NO: "A1001",
                                          ports.SLOT_REQUEST: ports.REQUEST_REFUND})
    assert u2.slots[ports.SLOT_ORDER_NO] == "A1001"
    assert u2.slots[ports.SLOT_REQUEST] == ports.REQUEST_REFUND
    # 上一轮里不合契约的键 / 取值丢掉，不让它进 Understanding。
    u3 = U.understand("你好", prior_slots={"address": "文三路", ports.SLOT_REQUEST: "chargeback",
                                         ports.SLOT_PRODUCT: ""})
    assert set(u3.slots) <= {ports.SLOT_EMOTION}


def test_slot_fill_turn_inherits_the_earlier_request_t173():
    """先说诉求、后补单号（追问之后的回答）：本轮只有单号，意图按之前的退款 / 退货诉求算。"""
    first = U.understand("我要退款", prior_slots={})
    assert (first.intent, first.slots.get(ports.SLOT_REQUEST)) == (
        T.INTENT_RETURN_EXCHANGE, ports.REQUEST_REFUND)
    second = U.understand("A1001", prior_slots=dict(first.slots))
    assert second.intent == T.INTENT_RETURN_EXCHANGE
    assert second.slots == {ports.SLOT_ORDER_NO: "A1001", ports.SLOT_REQUEST: ports.REQUEST_REFUND}
    # 查进度查的是什么只有那一轮原文知道：不猜。
    assert U.understand("A1001", prior_slots={ports.SLOT_REQUEST: ports.REQUEST_TRACK}).intent \
        == T.INTENT_UNKNOWN
    # 先报单号、后说诉求：单号留着，诉求补上。
    third = U.understand("帮我退货", prior_slots={ports.SLOT_ORDER_NO: "A1001"})
    assert third.slots == {ports.SLOT_ORDER_NO: "A1001", ports.SLOT_REQUEST: ports.REQUEST_RETURN}
    # 复核 L2-9：回答时顺带的客套（会投「通用」一票）不挡继承；短形态 + 客套也认得出单号。
    for text in ("订单号A1001谢谢", "A1001，谢谢", "A1001 thanks", "好的，A1001"):
        u = U.understand(text, prior_slots={ports.SLOT_REQUEST: ports.REQUEST_REFUND})
        assert u.intent == T.INTENT_RETURN_EXCHANGE, text
        assert u.slots[ports.SLOT_ORDER_NO] == "A1001", text
    # 客套本身不是补槽位：没有单号时照旧是通用。
    assert U.understand("谢谢", prior_slots={ports.SLOT_REQUEST: ports.REQUEST_REFUND}).intent \
        == T.INTENT_GENERAL


# ---------------------------------------------------------------------------
# 3. 意图
# ---------------------------------------------------------------------------
def test_understanding_shape_is_validated_and_read_only_t173():
    u = U.Understanding(lang="zh", intent=T.INTENT_LOGISTICS,
                        slots={ports.SLOT_REQUEST: ports.REQUEST_TRACK, ports.SLOT_ORDER_NO: "A1"},
                        source=ports.SLOT_SOURCE_RULE)
    assert list(u.slots) == [ports.SLOT_ORDER_NO, ports.SLOT_REQUEST]   # 按 SLOT_KEYS 排
    with pytest.raises(TypeError):
        u.slots["order_no"] = "B2"                                      # type: ignore[index]
    assert U.Understanding.from_json(json.loads(json.dumps(u.to_json()))) == u
    bad = [dict(lang="fr"), dict(intent="chitchat"), dict(source="guess"),
           dict(slots={"address": "x"}), dict(slots={ports.SLOT_REQUEST: "chargeback"}),
           dict(slots={ports.SLOT_EMOTION: "happy"}), dict(slots={ports.SLOT_ORDER_NO: ""})]
    base = dict(lang="zh", intent=T.INTENT_UNKNOWN, slots={}, source=ports.SLOT_SOURCE_RULE)
    for override in bad:
        with pytest.raises(ValueError):
            U.Understanding(**{**base, **override})


@pytest.mark.parametrize("text,want", [
    ("帮我转人工", T.INTENT_HANDOFF_REQUEST),               # 触发词最先
    ("我要投诉，还要退款", T.INTENT_COMPLAINT),
    ("钱退回来要几天呀", T.INTENT_REFUND_PAYMENT),
    ("帮我把这单退了", T.INTENT_RETURN_EXCHANGE),           # 诉求：要退 → 退换货（RET-005）
    ("我要退款", T.INTENT_RETURN_EXCHANGE),
    ("我的退款到了没", T.INTENT_REFUND_PAYMENT),             # 查进度查的是钱
    ("我的退货申请审核了没有", T.INTENT_RETURN_EXCHANGE),     # 查的是售后单
    ("我的快递到哪了", T.INTENT_LOGISTICS),                  # 查的是包裹
    ("退货运费谁出", T.INTENT_RETURN_EXCHANGE),              # 平票：退换货先于物流
    ("付款以后一般几天发货", T.INTENT_LOGISTICS),            # 「付款以后」只是时间点
    ("能开发票吗", T.INTENT_REFUND_PAYMENT),
    ("你好呀", T.INTENT_GENERAL),
    ("how long does shipping take", T.INTENT_LOGISTICS),
    ("帮我写一首关于秋天的诗", T.INTENT_UNKNOWN),
    ("东西一直没收到，我要退款", T.INTENT_RETURN_EXCHANGE),     # 明说要退款：RET-005 那篇
    # 复核 L3-5：换大小、商品本身的破洞 / 开胶 → 退换货；运输途中碎了 → 物流（包裹破损那篇）
    ("衣服小了能换大一码吗", T.INTENT_RETURN_EXCHANGE),
    ("衣服有个洞", T.INTENT_RETURN_EXCHANGE),
    ("鞋子开胶了", T.INTENT_RETURN_EXCHANGE),
    ("收到的碗碎了", T.INTENT_LOGISTICS),
])
def test_rule_intent_t173(text, want):
    assert U.understand(text, prior_slots={}).intent == want


#: 复核 L2-8：同一句里既有触发词、又有别的意图的话题词 —— 触发词说了算。
@pytest.mark.parametrize("text,want", [
    ("钱什么时候能回到卡里，你们这是骗子吧", T.INTENT_COMPLAINT),
    ("东西什么时候能到？转人工", T.INTENT_HANDOFF_REQUEST),
    ("收到的东西是坏的，气死我了", T.INTENT_COMPLAINT),
    ("钱什么时候能到，赔我", T.INTENT_COMPENSATION),
    ("我的货到哪了？找人工", T.INTENT_HANDOFF_REQUEST),
])
def test_trigger_wins_over_topic_words_t173(text, want):
    assert U.rule_intent(text) == want
    assert U.understand(text, prior_slots={}).intent == want


def test_rule_intent_order_is_trigger_request_keywords_t173(monkeypatch):
    """顺序钉死：触发词 → 本轮诉求 → 关键词。把关键词票数换成「永远投通用」的桩，
    触发词句与诉求句不受影响，没有诉求的句子才轮到关键词。"""
    monkeypatch.setattr(U, "keyword_votes", lambda text: {T.INTENT_GENERAL: 1.0})
    assert U.rule_intent("帮我转人工") == T.INTENT_HANDOFF_REQUEST
    assert U.rule_intent("我要退款", request=ports.REQUEST_REFUND) == T.INTENT_RETURN_EXCHANGE
    assert U.rule_intent("我的快递到哪了", request=ports.REQUEST_TRACK) == T.INTENT_LOGISTICS
    assert U.rule_intent("包邮吗") == T.INTENT_GENERAL


def test_intent_examples_table_is_gone_t173():
    """主会话裁定 R4：意图示例表在开发集与 T168 holdout 上测不出增益，整张删掉（连同同义归一与
    只给它用的 scripts.weighted_similarity）。ADP 4.4 的示例干预留给真模型路径的 few-shot。"""
    for name in ("INTENT_EXAMPLES", "EXAMPLE_SYNONYMS", "example_intent", "example_similarity",
                 "canonical_for_examples", "EXAMPLE_MIN_SIMILARITY"):
        assert not hasattr(U, name), name
    assert not hasattr(scripts, "weighted_similarity")


#: 复核 r2 的回归（示例表把「<X>什么时候能到」的虚词骨架拉成物流 / 退款）：话题词说了算。
@pytest.mark.parametrize("text,want", [
    ("退款什么时候能到账", T.INTENT_REFUND_PAYMENT),
    ("退款什么时候能到", T.INTENT_REFUND_PAYMENT),
    ("钱什么时候能到账", T.INTENT_REFUND_PAYMENT),
    ("什么时候能到账", T.INTENT_REFUND_PAYMENT),
    ("发票什么时候能到", T.INTENT_REFUND_PAYMENT),
    ("退的钱什么时候能到", T.INTENT_REFUND_PAYMENT),
    ("换货什么时候能到", T.INTENT_RETURN_EXCHANGE),
    ("东西什么时候能退", T.INTENT_RETURN_EXCHANGE),
    ("where is my refund", T.INTENT_REFUND_PAYMENT),
    ("when will my refund arrive", T.INTENT_REFUND_PAYMENT),
    ("when will my invoice arrive", T.INTENT_REFUND_PAYMENT),
    ("快递员什么时候上班", T.INTENT_LOGISTICS),
    ("要另外付安装费吗", T.INTENT_UNKNOWN),
])
def test_topic_words_decide_the_intent_t173(text, want):
    assert U.understand(text, prior_slots={}).intent == want


def test_goods_arrival_questions_are_never_refund_payment_t173():
    """R4：任何「<商品>什么时候能到」都不许被判成 refund_payment —— 商品词表里的每个中文品类都试一遍。"""
    wrong = []
    for product in U.PRODUCT_WORDS:
        if product.isascii():
            continue
        for text in (f"{product}什么时候能到", f"我买的{product}什么时候能到啊"):
            if U.understand(text, prior_slots={}).intent == T.INTENT_REFUND_PAYMENT:
                wrong.append(text)
    assert wrong == []


#: 复核 L3-5：诉求词表里认作换货 / 退货的说法，意图词表也要投退换货一票（两张表不许各说各的）。
REQUEST_PHRASES_T173 = {
    ports.REQUEST_EXCHANGE: ("换货", "换一件", "换个颜色", "换尺码", "换大一码", "换个小一号", "调换",
                             "换长一点的", "exchange", "replacement"),
    ports.REQUEST_RETURN: ("退货", "退回去", "寄回", "退掉", "退了吧", "不想要了", "不要了", "退换",
                           "无理由退", "帮我把这单退了", "return", "send it back"),
}


@pytest.mark.parametrize("kind", list(REQUEST_PHRASES_T173))
def test_request_words_also_vote_for_return_exchange_t173(kind):
    for phrase in REQUEST_PHRASES_T173[kind]:
        assert U.extract_request(phrase) in (kind, ""), phrase     # 确实是这一类的说法
        assert U._REQUEST_RES[kind][0].search(U._compact(phrase)) \
            or U._REQUEST_RES[kind][1].search(U._spaced(phrase)), phrase
        assert U.keyword_votes(phrase)[T.INTENT_RETURN_EXCHANGE] > 0, phrase


def test_intent_is_always_in_the_enum_t173():
    texts = [t for c in evaluate.load_cases() for t in c.turns]
    texts += ["", "   ", "!!!", "asdf", "A1001", "🙂", "谢谢", "hello there"]
    for text in texts:
        u = U.understand(text, prior_slots={})
        assert u.intent in T.INTENTS and u.lang in ports.LANGS, text
        assert u.source == ports.SLOT_SOURCE_RULE, text


# ---------------------------------------------------------------------------
# 4. 触发词（主会话裁定 R1：召回不许低于 p12）
# ---------------------------------------------------------------------------
_ANGER, _COMPLAINT, _COMP, _PRIV, _REQ = (T.HANDOFF_ANGER, T.HANDOFF_COMPLAINT, T.HANDOFF_COMPENSATION,
                                         T.HANDOFF_PRIVACY, T.HANDOFF_REQUESTED)

#: R1 判据 ①：带触发词的真实说法，五类（骂人 / 投诉 / 要赔偿 / 隐私 / 要人工）共 117 句。每句后面是
#: **p12（bf53df6 的 triggers.py）实跑**判出的原因（None = p12 没判出），由 scratchpad 里的一次性脚本
#: 算出后抄进来；p13 必须判出、且优先级不低于 p12。骂人一类里前 19 句是复核 final-fr4/probe_trig.py
#: 的「滚」，接着 9 句是复核 final-fr4/gun.txt（第三轮漏判的），其余自写。
TRUE_TRIGGERS_T173: dict[str, list[tuple[str, str | None]]] = {
    _ANGER: [
        ("快点滚", _ANGER), ("不想跟你说了滚", _ANGER), ("机器人滚", _ANGER), ("退钱然后滚", _ANGER),
        ("滚边去", _ANGER), ("滚回你老家去", _ANGER), ("滚去死", _ANGER), ("你们这群骗子都滚", _ANGER),
        ("客服滚", _ANGER), ("赶紧给我退钱滚", _ANGER), ("别废话了滚", _ANGER), ("滚远远的", _ANGER),
        ("滚一边玩去", _ANGER), ("你滚不滚", _ANGER), ("爱退不退滚", _ANGER), ("谁要你回答了滚", _ANGER),
        ("滚，别烦我", _ANGER), ("滚你丫的", _ANGER), ("都给老子滚蛋", _ANGER),
        ("快点滚！", _ANGER), ("机器人滚！", _ANGER), ("客服滚。", _ANGER), ("骗子客服快点滚！！", _ANGER),
        ("你们客服能不能滚", _ANGER), ("滚吧", _ANGER), ("不退钱就滚", _ANGER), ("少废话，赶紧滚", _ANGER),
        ("赶紧滚", _ANGER),
        ("他娘的什么时候发货", None), ("你他娘的", None), ("尼玛的快递", None), ("tmd什么时候发", None),
        ("操你妈", None), ("干你娘", None), ("特么的还没到", None), ("去你的吧", None),
        ("等了妈的一个星期", _ANGER), ("我等你妈的一周了", _ANGER), ("客服妈的装死", _ANGER),
        ("真他妈烦", _ANGER), ("你们他妈的是不是骗子", _ANGER), ("这都他妈几天了", _ANGER),
        ("我他妈要退款", _ANGER), ("你妈的退款呢", _ANGER), ("什么破玩意儿", _ANGER), ("这什么破客服", _ANGER),
        ("买了个什么破烂", _ANGER), ("你们客服真恶心人", _ANGER), ("垃圾东西", _ANGER), ("黑店！", _ANGER),
        ("你们就是骗子", _ANGER), ("气死我了", _ANGER), ("我气炸", _ANGER), ("要气炸了", _ANGER),
        ("赶紧去死", _ANGER), ("都去死吧", _ANGER), ("你老妈的，什么时候发货", _ANGER),
        ("你妈妈的，退款呢", _ANGER), ("他妈妈的就知道拖", _ANGER), ("去你老妈的", _ANGER),
        ("坑人的玩意", _ANGER), ("无耻商家", _ANGER), ("你们这群王八蛋", _ANGER), ("等了十天了！！！", _ANGER),
        ("This is a scam", None), ("What a rip-off", None),
    ],
    _COMPLAINT: [
        ("我要投诉你们", _COMPLAINT), ("再不处理我就打12315", _COMPLAINT), ("我去消协告你们", _COMPLAINT),
        ("我要找律师起诉你们", _COMPLAINT), ("我要曝光你们", _COMPLAINT), ("我要去市场监管局举报", _COMPLAINT),
        ("我去工商局投诉", _COMPLAINT), ("准备维权了", _COMPLAINT), ("法院见", _COMPLAINT),
        ("我要在黑猫投诉上发帖", _COMPLAINT), ("12315 我已经打过了", _COMPLAINT), ("告你们去", _COMPLAINT),
        ("I will file a complaint", None), ("My lawyer will contact you", None), ("I'm going to sue you", None),
    ],
    _COMP: [
        ("你们必须赔偿我的损失", _COMP), ("给我补偿", _COMP), ("这事得赔钱", _COMP), ("赔我一个新的", _COMP),
        ("不赔就投诉", _COMP), ("要求精神损失费", _COMP), ("退一赔三", _COMP), ("我要索赔", _COMP),
        ("你们要包赔", _COMP), ("这损失谁赔", _COMP), ("I demand compensation", None),
        ("You need to compensate me", None),
    ],
    _PRIV: [
        ("你们怎么泄露了我的手机号", _PRIV), ("把我的个人信息删掉", _PRIV), ("我的身份证号被你们泄露了", _PRIV),
        ("我的住址被别人知道了", _PRIV), ("你们侵犯我隐私", _PRIV), ("为什么要我的身份证", _PRIV),
        ("别把我的电话号码给别人", _PRIV), ("我的银行卡号你们存了吗", _PRIV), ("个人资料怎么删除", _PRIV),
        ("delete my personal data", None), ("this is a privacy issue", None),
        ("my mobile number got leaked", None),
    ],
    _REQ: [
        ("转人工", _REQ), ("我要找人工客服", _REQ), ("叫你们经理出来", _REQ), ("我不想跟机器人说话", _REQ),
        ("有没有真人", _REQ), ("让活人来回答", _REQ), ("人工服务在哪", _REQ), ("找个客服小姐姐", _REQ),
        ("I want to talk to a human", None), ("live agent please", None), ("get me your manager", None),
        ("Can I speak to a real person?", None),
    ],
}
_TRUE_ROWS_T173 = [(cat, text, p12) for cat, rows in TRUE_TRIGGERS_T173.items() for text, p12 in rows]

#: 只夹着表内名词的普通咨询：p12 误伤（右边是 p12 实跑的原因），p13 按 R1 的唯一例外豁免。
BENIGN_SENTENCES_T173: list[tuple[str, str]] = [
    ("空气炸锅坏了能换吗", _ANGER), ("去死皮膏怎么用", _ANGER), ("温度补偿器是原装的吗", _COMP),
    ("人工草坪运费怎么算", _REQ), ("粘毛滚的替换纸什么时候发货", _ANGER), ("卧室空气死角多，这台风扇够用吗", _ANGER),
    ("气死风灯是煤油的吗", _ANGER), ("身份证卡套有透明的吗", _PRIV), ("隐私门帘能定做尺寸吗", _PRIV),
    ("显示器有背光补偿吗", _COMP), ("气炸烤箱几天发", _ANGER), ("人工耳蜗的电池多久换一次", _REQ),
    ("真人手办什么时候到货", _REQ), ("去死皮霜过敏了能退吗", _ANGER), ("人工泪液能用多久", _REQ),
    ("隐私玻璃贴膜发什么快递", _PRIV), ("气炸一体机的内胆有涂层吗", _ANGER), ("真人模特图和实物一样吗", _REQ),
]

#: 名词表的收词规矩（复核 L2-1 / L3-2，见 triggers.BENIGN_NOUNS 的注释）撤掉的名词：前半截是跨词边界拼出来的
#: 真话（隐私贴到网上、身份证夹在、真人发消息、人工费劲、什么垃圾夹克、滚 刷单狗、不曝光值得吗、补偿器材、
#: 自动曝光、人工湖南仓……），后半截是原来靠这些名词豁免的普通咨询。一律照 p12 判（右边是 p12 实跑的原因）。
NOUN_EDGE_T173: list[tuple[str, str]] = [
    ("你们把我的隐私贴到网上了", _PRIV), ("谁把我的隐私贴出来的", _PRIV), ("麻烦把我的隐私屏蔽一下", _PRIV),
    ("我不小心把身份证夹在盒子里寄给你们了", _PRIV), ("我的身份证夹在退回的包裹里寄过去了", _PRIV),
    ("你们是不是拿我身份证套现了", _PRIV), ("是真人发的消息吗", _REQ), ("能不能让真人发个消息给我", _REQ),
    ("转真人cs", _REQ), ("转个人工费这么大劲", _REQ), ("找个人工费劲死了", _REQ),
    ("你们卖的什么垃圾夹克，洗一次就掉色", _ANGER), ("什么垃圾篮球，一拍就瘪了", _ANGER),
    ("买了个行李箱，什么垃圾箱子，轮子第一天就掉", _ANGER), ("什么垃圾处理器，开机就卡死", _ANGER),
    ("你们家垃圾桶装水一股味", _ANGER), ("什么垃圾袋子，一拎就破", _ANGER), ("你给我滚 刷单狗", _ANGER),
    ("滚石头去吧", _ANGER), ("这种事不曝光值得吗", _COMPLAINT), ("我曝光度假村那家店", _COMPLAINT),
    ("你们得补偿器材的损失", _COMP), ("你们app自动曝光了我的订单", _COMPLAINT), ("我要人工湖南仓发的货", _REQ),
    # 原来靠撤掉的名词豁免的普通咨询：照 p12 转人工（多转一次，不漏转）
    ("这款气炸锅包邮吗", _ANGER), ("垃圾袋什么时候发货", _ANGER), ("垃圾桶的盖子裂了", _ANGER),
    ("相机的曝光补偿在哪调", _COMP), ("安装要另收人工费吗", _REQ), ("真人发假发能烫吗", _REQ),
    ("手机隐私膜贴歪了", _PRIV), ("油漆滚刷发错颜色了", _ANGER), ("摇滚风的外套还有M码吗", _ANGER),
    ("冰滚美容仪发货了吗", _ANGER), ("滚石乐队的黑胶唱片", _ANGER), ("人工湖边的酒店能送到吗", _REQ),
    ("垃圾分类的垃圾桶有几种颜色", _ANGER),
]

#: 第三轮用否定语境 / 亲属称谓 / 上下文正则豁免过、p12 判出的句子：R1 把那几套豁免撤了，
#: 这些照 p12 转人工（多转一次，不漏转）。右边是 p12 实跑的原因。「去死海」里 p12 的「去死」跨过了
#: 「死海」的左边界，不算「完全落在」名词里，所以名词表不收「死海」。
FLOOR_RESTORED_T173: list[tuple[str, str]] = [
    ("给我妈的生日礼物什么时候能到", _ANGER), ("孩子他妈让我问一下运费", _ANGER), ("我想去死海玩", _ANGER),
    ("晕车贴能缓解恶心吗", _ANGER), ("你们家有什么破壁机推荐", _ANGER), ("宝妈的奶粉什么时候发货", _ANGER),
    ("孕吐恶心吃什么好，有推荐的零食吗", _ANGER), ("为什么破了一个角能换吗", _ANGER),
    ("请保证不泄漏我的信息", _PRIV), ("熊猫滚滚抱枕发货了吗", _ANGER), ("有真人实拍图吗", _REQ),
    ("防泄漏水杯漏水了", _PRIV), ("垃圾兜能挂在门后面吗", _ANGER), ("投影仪的梯形补偿怎么设置", _COMP),
]

#: p12 开发集（scenarios/cs/eval/p12_cases.json）里 p12 判出触发词的全部轮：原文 → p12 实跑的原因。
#: 不在表里的轮 p12 都判 None（下面的测试逐轮核对，表与文件对不上就红）。
P12_DEV_TRIGGERS_T173: dict[str, str] = {
    "帮我转人工": _REQ, "我不想跟机器人聊，找个真人来": _REQ, "你们再不处理，我就打12315投诉": _COMPLAINT,
    "这事我已经咨询过律师了，准备起诉你们": _COMPLAINT, "什么垃圾店，东西一用就坏": _ANGER,
    "等了一个礼拜还没消息！！！": _ANGER, "东西坏了耽误我好几天，你们得给我补偿": _COMP,
    "快递把我的东西摔坏了，你们赔钱": _COMP, "你们是怎么拿到我手机号的，天天给我发短信": _PRIV,
    "麻烦把我的个人信息和住址都删掉": _PRIV, "我要投诉你们，还要赔偿": _COMP,
    "你们这服务太垃圾了，转人工": _ANGER, "骗子！把钱赔我": _COMP,
    "我要去消协投诉，你们凭什么泄露我的隐私": _PRIV, "找人工！我要查一下你们存了我哪些个人信息": _PRIV,
    "再不给我处理我就曝光你们，快给我转人工客服": _COMPLAINT, "直接给我转人工": _REQ,
    "喂，人呢？怎么不回我！！！": _ANGER,
}

#: 每一类都至少有一种说法的真触发词（与表内名词同句时，名词里的那一处照常算）。
REAL_TRIGGER_SAMPLES_T173 = ["转人工", "我要投诉", "骗子", "你们得赔偿", "把我的个人信息删了",
                             "I want a human", "等了好久！！！", "我打了12315", "操你妈"]


def _priority_t173(reason: str) -> int:
    return triggers.PRIORITY.index(reason)


def test_true_trigger_set_covers_five_classes_t173():
    """判据 ① 的规模：≥ 80 句、五类每类 ≥ 12 句、含复核那 19 句「滚」。"""
    assert len(_TRUE_ROWS_T173) >= 80
    assert set(TRUE_TRIGGERS_T173) == set(triggers.PRIORITY)
    assert all(len(rows) >= 12 for rows in TRUE_TRIGGERS_T173.values())
    gun = [t for t, _ in TRUE_TRIGGERS_T173[_ANGER][:19]]
    assert len(gun) == 19 and all("滚" in t for t in gun)


@pytest.mark.parametrize("cat,text,p12", _TRUE_ROWS_T173)
def test_true_triggers_fire_and_never_below_p12_t173(cat, text, p12):
    """R1 判据 ①：p13 判出，原因的优先级不低于 p12；地板层（p12_reasons）与 bf53df6 实跑结果逐句一致。"""
    got = triggers.detect(text)
    assert got is not None, text
    assert got[0] == cat, (text, got)
    if p12 is not None:
        assert _priority_t173(got[0]) <= _priority_t173(p12), (text, got, p12)
    assert triggers.p12_reasons(text)[:1] == ((p12,) if p12 else ()), text


def test_benign_noun_table_is_a_closed_list_of_nouns_t173():
    """R1 判据 ②（名词性）：三类（商品 / 地点 / 日常名词）、每条都是纯字面（不是正则）、规范化后的形态、
    不含否定 / 代词 / 语气助词 / 意愿动词这些**成句**的字（名词短语里不会有）。上下文正则、句型、
    亲属称谓那几套豁免都不在了。"""
    assert set(triggers.BENIGN_NOUNS) == {"商品", "地点", "日常名词"}
    clause_chars = set("不没别无未非我你他她它咱您们了着过吗呢吧啊呀的得地要想请给让把被说")
    words = [w for ws in triggers.BENIGN_NOUNS.values() for w in ws]
    assert len(words) == len(set(words))
    for word in words:
        assert word == triggers.normalize(word) and re.escape(word) == word, word
        assert not set(word) & clause_chars, word
        assert "妈" not in word and "娘" not in word, word          # 称谓不收
    function_words = {"a", "an", "the", "my", "your", "i", "you", "to", "of", "for", "is", "are", "not",
                      "no", "don't", "please", "me"}
    for phrase in triggers.EN_BENIGN_NOUNS:
        parts = phrase.split()
        assert len(parts) >= 2 and not set(parts) & function_words, phrase
    for gone in ("BENIGN_PATTERNS", "BENIGN_COMPOUNDS", "INSULT_SHAPES", "EN_BENIGN_PATTERNS",
                 "EN_BENIGN_COMPOUNDS"):
        assert not hasattr(triggers, gone), gone


@pytest.mark.parametrize("word", sorted({w for ws in triggers.BENIGN_NOUNS.values() for w in ws}))
def test_each_benign_noun_is_silent_alone_and_fires_with_a_real_trigger_t173(word):
    """R1 判据 ②：每条名词确实夹着一处 p12 触发词（不是摆设），单独出现不触发；跟任一真触发词同句，
    照常触发，且优先级不低于 p12（名词里那一处照常算）。"""
    assert triggers.p12_reasons(word), word
    assert triggers.detect(word) is None
    assert triggers.detect(f"我想问下{word}的事") is None
    for sample in REAL_TRIGGER_SAMPLES_T173:
        for text in (f"{word}，{sample}", f"{sample}，{word}"):
            got = triggers.detect(text)
            floor = triggers.p12_reasons(text)
            assert got is not None, text
            if floor:
                assert _priority_t173(got[0]) <= _priority_t173(floor[0]), (text, got, floor)


@pytest.mark.parametrize("phrase", triggers.EN_BENIGN_NOUNS)
def test_each_english_benign_noun_is_silent_alone_t173(phrase):
    assert triggers.detect(phrase) is None and triggers.detect(f"do you sell a {phrase}") is None
    assert triggers.detect(f"{phrase}, I want a human") == (_REQ, triggers.INTENT_OF[_REQ])


@pytest.mark.parametrize("text,p12", BENIGN_SENTENCES_T173)
def test_benign_sentences_are_the_only_exception_t173(text, p12):
    assert triggers.p12_reasons(text)[:1] == (p12,)
    assert triggers.detect(text) is None
    norm = triggers.normalize(text)
    assert any(w in norm for ws in triggers.BENIGN_NOUNS.values() for w in ws), text


@pytest.mark.parametrize("text,p12", FLOOR_RESTORED_T173 + NOUN_EDGE_T173)
def test_exemptions_of_round_three_are_withdrawn_t173(text, p12):
    """否定语境、亲属称谓、上下文正则撤掉之后，这些句子照 p12 判（同一原因）；名词表按收词规矩撤掉的
    那些名词（复核 L2-1 / L3-2）拼出来的句子也一样。"""
    assert triggers.p12_reasons(text)[:1] == (p12,)
    assert triggers.detect(text) == (p12, triggers.INTENT_OF[p12])


def _trigger_cores_t173(word: str) -> list[str]:
    """名词里夹着的**最短**触发子串（自己单说就触发、再短就不触发的那几段）。"""
    subs = {word[i:j] for i in range(len(word)) for j in range(i + 1, len(word) + 1)}
    fires = {s for s in subs if triggers.p12_reasons(s) or triggers.matched_reasons(s)}
    return sorted(s for s in fires if not any(t != s and t in s for t in fires))


def test_benign_nouns_reach_two_chars_past_each_trigger_t173():
    """收词规矩一（复核 L2-1 / L3-2）：名词在它夹着的每一段触发子串外面，左右合计至少多出两个字 ——
    只多一个字的，那个字常是下一个词的词头（隐私贴到 / 身份证夹在 / 人工费劲 / 垃圾箱子）。"""
    for word in (w for ws in triggers.BENIGN_NOUNS.values() for w in ws):
        cores = _trigger_cores_t173(word)
        assert cores, word
        for core in cores:
            assert len(word) - len(core) >= 2, (word, core)


def test_nouns_never_start_with_a_trigger_that_takes_a_noun_t173():
    """收词规矩二：「垃圾 + 名词」就是骂、「曝光 / 补偿 + 名词」就是要曝光 / 要赔 —— 这三个触发词
    打头的复合词一条不收（名词在「补偿」前面的照收）。"""
    words = [w for ws in triggers.BENIGN_NOUNS.values() for w in ws]
    assert not [w for w in words if w.startswith(("垃圾", "曝光", "补偿"))]


#: 触发词在真话里常紧挨着的词（右边：下一个词；左边：上一个词）。名词表里的名词若能由「触发词 + 这些词」
#: 拼出来，就会把真话当名词豁免掉 —— 下面逐条断言它们照 p12 转人工（收词规矩的机器判据）。
TRIGGER_NEIGHBOURS_T173: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "垃圾": ((), ("袋子", "袋装零食", "桶装水", "箱子", "箱包", "篮球", "篮子", "夹克", "夹子", "处理器", "挂袋",
                "分类页面", "铲子", "筐子", "篓子", "玻璃杯")),
    "隐私": (("我的", "侵犯"), ("贴到网上", "贴出来", "屏蔽", "泄露", "照片", "被曝光")),
    "身份证": (("我的",), ("夹在包裹里", "套现", "卡在机器里", "号码", "照片")),
    "真人": (("找个", "要"), ("发消息", "发个语音", "cs", "回复", "秀一下", "客服")),
    "人工": (("转", "要个", "找"), ("费劲", "湖南仓", "湖北仓", "服务", "处理", "介入", "审核")),
    "滚": (("快", "给我"), ("刷单狗", "梳理清楚", "石头", "轴承", "远点", "犊子")),
    "曝光": (("自动", "家长", "店长", "公开", "网上"), ("度假村", "值得吗", "时间线", "模式", "你们", "补偿不到位的店")),
    "补偿": (("必须", "给我"), ("器材", "导线的钱", "电容的钱", "运费", "差价", "金")),
    "气炸": (("我", "快"), ("了", "肺")),
}


@pytest.mark.parametrize("trigger", sorted(TRIGGER_NEIGHBOURS_T173))
def test_triggers_next_to_their_common_neighbours_still_fire_t173(trigger):
    """收词规矩的机器判据：触发词前后接上常紧挨着它的词，照 p12 判（名词表不许把这些拼出来的串吞掉）。"""
    left, right = TRIGGER_NEIGHBOURS_T173[trigger]
    for text in [w + trigger for w in left] + [trigger + w for w in right]:
        floor = triggers.p12_reasons(text)
        got = triggers.detect(text)
        assert floor and got is not None, text
        assert _priority_t173(got[0]) <= _priority_t173(floor[0]), (text, got, floor)


@pytest.mark.parametrize("text", ["this is trash can you refund it", "garbage can you just help me"])
def test_english_trash_can_is_not_a_noun_when_can_is_a_modal_t173(text):
    """英文同一条规矩：单数的「trash can / garbage can」不收，「can」常是情态动词。"""
    assert triggers.detect(text) == (_ANGER, triggers.INTENT_OF[_ANGER])


def test_p12_floor_property_over_dev_set_and_all_sentence_sets_t173():
    """R1 判据 ③（性质测试）：在 p12 开发集全部轮与本节几组句子上，p12 判出而 p13 判 None 的句子
    必须全部命中名词表；p13 判出的，优先级不低于 p12。开发集的 p12 结果先与地板层逐轮对上。"""
    dev = [t for c in evaluate.load_cases() for t in c.turns]
    assert set(P12_DEV_TRIGGERS_T173) <= set(dev)
    for text in dev:
        want = P12_DEV_TRIGGERS_T173.get(text)
        assert triggers.p12_reasons(text)[:1] == ((want,) if want else ()), text
    rows = [(t, P12_DEV_TRIGGERS_T173.get(t)) for t in dev]
    rows += [(t, p12) for _c, t, p12 in _TRUE_ROWS_T173] + BENIGN_SENTENCES_T173 + FLOOR_RESTORED_T173 + NOUN_EDGE_T173
    nouns = [w for ws in triggers.BENIGN_NOUNS.values() for w in ws]
    dropped = []
    for text, p12 in rows:
        if p12 is None:
            continue
        got = triggers.detect(text)
        if got is None:
            dropped.append(text)
            assert any(w in triggers.normalize(text) for w in nouns), text
        else:
            assert _priority_t173(got[0]) <= _priority_t173(p12), (text, got, p12)
    assert sorted(dropped) == sorted(t for t, _ in BENIGN_SENTENCES_T173)   # 开发集一句都没少判


#: 同一句里既有名词表里的词、又有真触发词：真的照认，优先级不低于 p12。
@pytest.mark.parametrize("text,reason", [
    ("人工草坪都比你们的东西结实，什么垃圾店", _ANGER),
    ("人工草坪另算？转人工", _REQ),
    ("空气炸锅坏了，气死我了", _ANGER),
    ("温度补偿坏了，你们得赔偿", _COMP),
    ("温度补偿坏了，转人工", _COMP),            # 「温度补偿」里的补偿照常算：同 p12 是 compensation
    ("隐私玻璃的订单里有我的身份证号", _PRIV),
    ("空气炸锅漏了，I want a human", _ANGER),    # 英文也算「别的触发词」；名词里的「气炸」照常算
])
def test_real_triggers_next_to_benign_nouns_still_fire_t173(text, reason):
    assert triggers.detect(text) == (reason, triggers.INTENT_OF[reason])


@pytest.mark.parametrize("text,reason", [
    ("帮我找人工草坪的安装师傅", _REQ),        # p12 的「找人工」跨过了「人工草坪」的左边界
    ("帮我找人工耳蜗的售后", _REQ),
])
def test_a_trigger_straddling_a_noun_edge_is_not_inside_it_t173(text, reason):
    """「完全落在」按字符区间判：触发词只要有一个字在名词外面，就不算落在名词里（碰一下不算）。"""
    norm = triggers.normalize(text)
    assert any(w in norm for ws in triggers.BENIGN_NOUNS.values() for w in ws), text
    assert triggers.p12_reasons(text)[:1] == (reason,)
    assert triggers.detect(text) == (reason, triggers.INTENT_OF[reason])


def test_added_patterns_only_add_t173():
    """第 2 层只加不减：去掉第 2 层与英文，p13 判出的原因集合恰好是「p12 的原因集合」减去名词表豁免。"""
    rows = [t for _c, t, _p in _TRUE_ROWS_T173] + [t for t, _ in BENIGN_SENTENCES_T173 + FLOOR_RESTORED_T173 + NOUN_EDGE_T173]
    rows += [t for c in evaluate.load_cases() for t in c.turns]
    for text in rows:
        full = set(triggers.matched_reasons(text))
        assert full >= set(triggers.p12_reasons(text)) or not full, text


#: 自写的英文触发句：每类至少两种说法。
EN_TRIGGERS_T173 = {
    T.HANDOFF_REQUESTED: ["I want to talk to a human", "Can I speak to a real person?",
                          "live agent please", "get me your manager", "I don't want a bot",
                          "human please", "connect me to an operator", "human",
                          "I want a representative", "let me speak to a representative"],
    T.HANDOFF_COMPLAINT: ["I will file a complaint", "My lawyer will contact you",
                          "I'm going to sue you", "I'll report you to consumer protection",
                          "I will report your store"],
    T.HANDOFF_ANGER: ["This is a scam", "What a rip-off", "your service is garbage",
                      "WTF is going on", "you ripped me off", "what a rip off"],
    T.HANDOFF_COMPENSATION: ["I demand compensation", "You need to compensate me"],
    T.HANDOFF_PRIVACY: ["delete my personal data", "why do you have my phone number",
                        "this is a privacy issue", "my mobile number got leaked",
                        "my phone number was leaked"],
}


@pytest.mark.parametrize("reason", list(EN_TRIGGERS_T173))
def test_english_triggers_t173(reason):
    for text in EN_TRIGGERS_T173[reason]:
        assert triggers.detect(text) == (reason, triggers.INTENT_OF[reason]), text


def test_english_triggers_keep_the_priority_and_match_inside_chinese_t173():
    assert triggers.detect("scam! I want compensation") == (
        T.HANDOFF_COMPENSATION, T.INTENT_COMPENSATION)
    assert triggers.detect("我要找human") == (T.HANDOFF_REQUESTED, T.INTENT_HANDOFF_REQUEST)
    assert triggers.detect("ＨＵＭＡＮ　ｐｌｅａｓｅ") == (T.HANDOFF_REQUESTED, T.INTENT_HANDOFF_REQUEST)


#: 复核 L2-5：中英混排的一句 —— 中文低优先级 + 英文高优先级、英文低优先级 + 中文高优先级，两个方向都
#: 按同一张优先级表取（不许「中文先、英文后」）。每句都先断言两类确实都中（判据不空转）。
MIXED_PRIORITY_T173 = [
    ("垃圾店, delete my personal data", T.HANDOFF_PRIVACY, T.HANDOFF_ANGER),
    ("转人工, I will sue you", T.HANDOFF_COMPLAINT, T.HANDOFF_REQUESTED),
    ("投诉! I demand compensation", T.HANDOFF_COMPENSATION, T.HANDOFF_COMPLAINT),
    ("scam, 我的手机号被泄露了", T.HANDOFF_PRIVACY, T.HANDOFF_ANGER),
    ("I want a human, 我要投诉", T.HANDOFF_COMPLAINT, T.HANDOFF_REQUESTED),
    ("this is a scam, 你们得赔偿", T.HANDOFF_COMPENSATION, T.HANDOFF_ANGER),
]


@pytest.mark.parametrize("text,top,lower", MIXED_PRIORITY_T173)
def test_mixed_language_sentences_follow_one_priority_table_t173(text, top, lower):
    reasons = triggers.matched_reasons(text)
    assert top in reasons and lower in reasons, reasons
    assert list(reasons) == [r for r in triggers.PRIORITY if r in reasons]
    assert triggers.detect(text) == (top, triggers.INTENT_OF[top])


@pytest.mark.parametrize("text", [
    "How long does shipping take?", "Where is my parcel?", "Can I get my money back?",
    "Please refund me now", "Is there an issue with my order?", "This is inhumane packaging",
    "Hi, how long does shipping usually take?", "Does it come with a user manual?",
    "Do you sell a trash compactor?", "I bought a garbage truck toy for my son",
    "Is this color representative of the real product?", "Where is your privacy policy?",
    "What is your store phone number?", "Does this phone work with any operator?", "is this safe for humans?",
    "rip off the tags, can I still return it?", "I want to report this issue with my order",
    "Is this real human hair?", "The operator manual is missing",
    "Do you sell trash cans?", "Is this human hair?", "privacy screen protector for iphone",
    "My name is Sue", "the bottle leaked in the box", "I need a cleaning agent",
    "Do you have a passport holder?", "Please ship to my home address", "Do you sell trash grabbers?",
    "What size is the rubbish chute cover?",
])
def test_ordinary_english_questions_do_not_trigger_t173(text):
    """「refund me now」这类要钱的说法不进触发词（退款诉求走核验 → 查单 → 预检卡）；
    按词认：issue 里的 sue、inhumane 里的 human 不算；只认诉求的说法（复核 L2-7）；英文名词表遮英文这一层。"""
    assert triggers.detect(text) is None


# ---------------------------------------------------------------------------
# 5. match_scripts 的 intent_hint
# ---------------------------------------------------------------------------
#: p12（bf53df6 的 maos/domain/cs/scripts.py，同一份话术库）在开发集每一轮的检索形态
#: （desk.retrieval_query）上：返回的 ScriptHit 全部字段 + 落的 KbRetrieved（event_type / plan_id /
#: task_id / trace_id / detail，detail 去掉 duration_ms 与知识层配置的 weights）的 sha256 前 16 位。
#: 由 scratchpad 里的一次性脚本拿 bf53df6 的 scripts.py 原样算出。话术库改了（主会话批准）要拿
#: p12 的 scripts.py 重算这张表 —— 用本轨的 scripts.py 重算就等于拿被测物验被测物。
P12_DEV_MATCH_DIGESTS_T173: dict[tuple[str, int], str] = {
    ("CS12-001", 1): "50769f761340ffde",
    ("CS12-002", 1): "d7a8dae36f641f36",
    ("CS12-003", 1): "965e73bab8fe7e95",
    ("CS12-004", 1): "cb5bb2dde3012455",
    ("CS12-005", 1): "d2437b0e3bd326d8",
    ("CS12-006", 1): "ef2df19727b1d442",
    ("CS12-007", 1): "5a878c35622746c9",
    ("CS12-008", 1): "2ee245d54fa9bd2c",
    ("CS12-009", 1): "5c5bca11ceab7243",
    ("CS12-010", 1): "3c897fa707cc3c43",
    ("CS12-011", 1): "396e07bcd4125761",
    ("CS12-012", 1): "8eb762764e0c036b",
    ("CS12-013", 1): "03784ec958950af1",
    ("CS12-014", 1): "8dd4fa643d8fef7d",
    ("CS12-015", 1): "8b1dd2b3daf68eee",
    ("CS12-016", 1): "2b319143ab05ccee",
    ("CS12-017", 1): "32f10eeab7003a70",
    ("CS12-018", 1): "b64cb70015d07373",
    ("CS12-019", 1): "ea39bf4f29c73c7e",
    ("CS12-020", 1): "06e8027d3b160dab",
    ("CS12-021", 1): "9b401167d5e5e75e",
    ("CS12-022", 1): "d5b1f28d222f0a80",
    ("CS12-023", 1): "a040ef30fe50b30a",
    ("CS12-024", 1): "4d123959e7464588",
    ("CS12-025", 1): "52ca87d62c8b0013",
    ("CS12-026", 1): "065e6c4b8d4930a3",
    ("CS12-027", 1): "4aa381c17eec7141",
    ("CS12-028", 1): "625374849693057d",
    ("CS12-029", 1): "8d182daa4e7032d3",
    ("CS12-030", 1): "cd980dd8a8dfcc77",
    ("CS12-031", 1): "5f21a9b5a599a83c",
    ("CS12-032", 1): "6594e93dfd6c9978",
    ("CS12-033", 1): "f6f2faf486ab9200",
    ("CS12-034", 1): "02ec6de49fbce0b6",
    ("CS12-035", 1): "57cc2d6e0f67814a",
    ("CS12-036", 1): "b987cc7e5c2675b9",
    ("CS12-036", 2): "aedc60944d5cd6e4",
    ("CS12-036", 3): "559940dbefdce5ba",
    ("CS12-037", 1): "88899645881a75ba",
    ("CS12-037", 2): "f0c051d7fcad3ea2",
    ("CS12-038", 1): "3785b6874fb18a12",
    ("CS12-038", 2): "4f166aa4577fcfdc",
    ("CS12-038", 3): "ebaed88c56ff8f03",
    ("CS12-039", 1): "d9aa9eb155e6d5b3",
    ("CS12-039", 2): "905a94182c84ec9b",
    ("CS12-039", 3): "a2abff701cb3bfaa",
    ("CS12-040", 1): "5e558c7a058e23bc",
    ("CS12-041", 1): "a61868dc1827104e",
    ("CS12-042", 1): "4cbf330f2bac7daa",
    ("CS12-043", 1): "e29db3de48278739",
    ("CS12-044", 1): "d06c0c877d6b9ed3",
    ("CS12-045", 1): "f92971ae0ce9eacf",
    ("CS12-045", 2): "42ea54fb8ddc1dc3",
}


def _match_digest_t173(text: str, *, hint: str | None) -> str:
    store = _seeded_t173()
    kw = {} if hint is None else {"intent_hint": hint}
    hits = scripts.match_scripts(store, tenant_id=TENANT_T173, text=text,
                                 plan_id="cs:csc-t173golden", task_id="csc-t173golden-t0001", **kw)
    rows = kb.query(store, "SELECT event_type, plan_id, task_id, trace_id, detail FROM event_log")
    events = []
    for row in rows:
        detail = json.loads(row["detail"])
        detail.pop("duration_ms", None)
        detail.pop("weights", None)
        events.append([row["event_type"], row["plan_id"], row["task_id"], row["trace_id"], detail])
    blob = json.dumps({"hits": [[h.doc_id, h.scheme_no, h.intent, h.score, h.script, h.principle,
                                 h.handoff] for h in hits], "events": events},
                      ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def test_default_match_scripts_is_byte_identical_to_p12_on_the_dev_set_t173():
    """缺省（不给 intent_hint、或给空串）时，开发集每一轮的返回值与 KbRetrieved 都与 p12 一致。"""
    got = {}
    for case in evaluate.load_cases():
        for i, text in enumerate(case.turns, start=1):
            query = retrieval_query(text)
            omitted = _match_digest_t173(query, hint=None)
            assert _match_digest_t173(query, hint="") == omitted, (case.id, i)
            got[(case.id, i)] = omitted
    assert got == P12_DEV_MATCH_DIGESTS_T173


def test_intent_hint_is_validated_and_logged_without_customer_text_t173():
    store = _seeded_t173()
    with pytest.raises(ValueError):
        _match_t173(store, "包邮吗", hint="shipping")
    text = "钱退回来要几天呀"
    hits = _match_t173(store, text, hint=T.INTENT_REFUND_PAYMENT)
    assert hits and all(0.0 < h.score <= 1.0 for h in hits)
    rows = kb.query(store, "SELECT detail FROM event_log WHERE event_type = 'KbRetrieved'")
    detail = json.loads(rows[-1]["detail"])
    assert detail["query"]["intent_hint"] == T.INTENT_REFUND_PAYMENT
    assert detail["query"]["cue"] == scripts.CUE_DURATION
    assert text not in rows[-1]["detail"]
    assert [d["doc_id"] for d in detail["docs"]] == [h.doc_id for h in hits]
    assert [d["score"] for d in detail["docs"]] == [h.score for h in hits]


def test_hint_only_moves_the_hinted_intent_t173():
    store = _seeded_t173()
    text = "退货运费谁出"
    plain = {h.scheme_no: h.score for h in _match_t173(store, text, hint=None)}
    hinted = {h.scheme_no: h.score for h in _match_t173(store, text, hint=T.INTENT_LOGISTICS)}
    intent_of = _intent_of_scheme_t173()
    for scheme, score in hinted.items():
        if scheme in plain and intent_of[scheme] != T.INTENT_LOGISTICS:
            assert score == plain[scheme], scheme
        elif scheme in plain:
            assert score > plain[scheme], scheme
    # unknown 没有可优先的话术：排序与分数同缺省。
    unknown = [(h.doc_id, h.score) for h in _match_t173(store, text, hint=T.INTENT_UNKNOWN)]
    assert unknown == [(h.doc_id, h.score) for h in _match_t173(store, text, hint=None)]


@pytest.mark.parametrize("text,cue", [
    ("退款一般要多久", scripts.CUE_DURATION), ("几天能发货", scripts.CUE_DURATION),
    ("how long does a refund take", scripts.CUE_DURATION),
    ("退款到了没", scripts.CUE_PROGRESS), ("发货了吗", scripts.CUE_PROGRESS),
    ("好几天了还没到", scripts.CUE_PROGRESS),              # 「好几天了」不是问时长
    ("退款多久了还没到", scripts.CUE_PROGRESS),
    ("where is my refund", scripts.CUE_PROGRESS),
    ("包邮吗", ""), ("你好", ""), ("你们店开了多久了", ""),
    # 复核 L2-2 / L3-2：问「什么时候」是问时间；英文的「还没到 / 处理了吗」是查进度
    ("退款什么时候到账", scripts.CUE_DURATION), ("大概几号能收到", scripts.CUE_DURATION),
    ("when will my refund arrive", scripts.CUE_DURATION),
    ("my refund hasn't arrived yet", scripts.CUE_PROGRESS),
    ("i haven't received my refund", scripts.CUE_PROGRESS),
    ("is my refund processed", scripts.CUE_PROGRESS),
])
def test_detect_cue_t173(text, cue):
    assert scripts.detect_cue(text) == cue


#: 复核 L2-7：**两类线索都中**的句子（催这一单、又顺带问规则时长）—— 进度优先。
BOTH_CUES_T173 = ["都一个星期了还没到，一般要几天", "发货时效是多久，我的怎么还没发",
                  "退款多长时间能到账，我的还没退", "什么时候能到啊，都等了一周了还没收到"]


@pytest.mark.parametrize("text", BOTH_CUES_T173)
def test_progress_wins_when_both_cues_match_t173(text):
    norm = scripts._cue_text(text)
    assert scripts._CUE_RES[scripts.CUE_DURATION].search(norm), text     # 判据不空转：两类都中
    assert scripts._CUE_RES[scripts.CUE_PROGRESS].search(norm), text
    assert scripts.detect_cue(text) == scripts.CUE_PROGRESS


#: 自写的 22 句（11 对）：问规则时长 vs 查某一笔进度，共享实词（钱退回来 / 退款 / 发货 / 寄出）。
#: 期望的是提示模式下（意图取 understand 的结果）排第一且过门槛的方案编号。
CUE_PAIRS_T173 = [
    ("退款一般几天能退到卡上", "PAY-001"), ("退款退到卡上了没有", "PAY-003"),
    ("钱退回来大概要多久", "PAY-001"), ("钱退回来了没有啊", "PAY-003"),
    ("下单后多久能发货", "LOG-001"), ("我下的单发货了没", "LOG-004"),
    ("退款多长时间能到账", "PAY-001"), ("我的退款现在到账没有", "PAY-003"),
    ("付款后一般几天发出", "LOG-001"), ("付款好几天了还没发出", "LOG-004"),
    ("退的钱几个工作日能回来", "PAY-001"), ("退的钱回来了吗", "PAY-003"),
    ("钱退回卡里要几天", "PAY-001"), ("钱退回卡里了吗", "PAY-003"),
    ("东西一般几天能寄出", "LOG-001"), ("东西寄出了吗", "LOG-004"),
    ("退款退回来通常多久", "PAY-001"), ("退款退回来了没", "PAY-003"),
    ("钱退回来要几天呀", "PAY-001"), ("钱到底退回来没有", "PAY-003"),
    # 复核轮补：问「什么时候」的一对
    ("退款什么时候能退到卡上", "PAY-001"), ("退款退到卡上了没有", "PAY-003"),
]


def test_duration_and_progress_cues_separate_policy_from_lookup_t173():
    store = _seeded_t173()
    handoff_of = _handoff_of_scheme_t173()
    wrong_without_cue = []
    for text, want in CUE_PAIRS_T173:
        hint = U.understand(text, prior_slots={}).intent
        scheme, score = _top_t173(store, text, hint=hint)
        assert (scheme, score >= scripts.MIN_SCRIPT_SCORE) == (want, True), (text, scheme, score)
        cue = scripts.detect_cue(text)
        assert cue == (scripts.CUE_PROGRESS if handoff_of[want] else scripts.CUE_DURATION), text
        if _top_t173(store, text)[0] != want:
            wrong_without_cue.append(text)
    # 判据不空转：这几句 p12 的缺省检索判错，是线索把它们分开的。
    assert len(wrong_without_cue) >= 2, wrong_without_cue


def test_cue_bonus_is_what_separates_them_t173(monkeypatch):
    """反向：线索加减清零，CS12-044#1 那一类又被查单篇抢走。"""
    store = _seeded_t173()
    text = "钱退回来要几天呀"
    assert _top_t173(store, text, hint=T.INTENT_REFUND_PAYMENT)[0] == "PAY-001"
    monkeypatch.setattr(scripts, "CUE_BONUS", 0.0)
    assert _top_t173(store, text, hint=T.INTENT_REFUND_PAYMENT)[0] == "PAY-003"


# ---------------------------------------------------------------------------
# 6. 模型路径
# ---------------------------------------------------------------------------
class _StubModelT173(ModelClient):
    """桩「真模型」：不是 ScriptedModelClient，按给定文本回，或抛给定异常。"""

    model = "stub-real-model"

    def __init__(self, text: str = '{"intent":"logistics"}', exc: Exception | None = None):
        self.text, self.exc = text, exc
        self.calls: list[dict] = []

    def complete(self, *, system: str, user: str, tier: str) -> ModelResponse:
        self.calls.append({"system": system, "user": user, "tier": tier})
        if self.exc is not None:
            raise self.exc
        return ModelResponse(text=self.text, tokens_in=40, tokens_out=6, model=self.model)


_OFF_TOPIC_T173 = "帮我写一首关于秋天的诗"     # 规则判不出（unknown）


def _invoke_understand_t173(store, text: str, model, *, prior=None):
    ident = AgentIdentity(agent_id="t173-probe", role="cs_front_desk", duty="测试理解层",
                          allowed_skills=frozenset({"cs.understand"}),
                          allowed_tools=frozenset(), max_risk="L", model_tier=Tier.LIGHT)
    return SkillInvoker(ident, store).invoke("cs.understand", {
        "tenant_id": TENANT_T173, "conversation_id": CONV_T173, "turn_id": TURN_T173,
        "text": text, "prior_slots": prior or {},
    }, extras={"plan_id": PLAN_T173, "task_id": TURN_T173, "trace_id": "", "model": model})


def test_real_model_is_called_only_when_rules_give_up_and_one_usage_row_is_recorded_t173():
    store = SqliteStore()
    store.init_schema()
    model = _StubModelT173('{"intent":"logistics"}')
    res = _invoke_understand_t173(store, _OFF_TOPIC_T173, model)
    assert res.status == "ok"
    assert res.output["intent"] == T.INTENT_LOGISTICS and res.output["source"] == "model"
    assert len(model.calls) == 1 and model.calls[0]["tier"] == Tier.LIGHT
    for intent in T.INTENTS:                       # system 段逐个列出意图
        assert intent in model.calls[0]["system"]
    (row,) = store.list_model_usage()
    assert row["call_site"] == U.CALL_SITE == CALL_SITE_CS_UNDERSTAND
    assert row["call_site"] in REGISTERED_CALL_SITES
    assert (row["plan_id"], row["trace_id"], row["task_id"]) == (PLAN_T173, "", None)
    assert (row["agent_role"], row["model"], row["tier"]) == ("cs_front_desk", "stub-real-model", "light")
    assert store.list_model_call_failures() == []
    # 规则判得出的句子：真模型也一次不调、一行不记 —— 含规则判成「通用」的问候与感谢（复核 L2-3：
    # 通用是规则判出来的意图，不是「判不出」）。
    for text, want in (("包邮吗", T.INTENT_LOGISTICS), ("你好呀", T.INTENT_GENERAL),
                       ("thank you for your help", T.INTENT_GENERAL)):
        res2 = _invoke_understand_t173(store, text, model)
        assert (res2.output["intent"], res2.output["source"]) == (want, "rule"), text
        assert len(model.calls) == 1, text
        assert len(store.list_model_usage()) == 1, text


def test_skill_passes_extras_plan_id_through_as_is_t173():
    """复核 L1-1：plan_id 照契约取 extras["plan_id"]，extras 里没有就是空串 —— 与同一次调用的
    SkillInvoked 行一致（invoker 也是 extras.get('plan_id', '')），不替调用方现算。"""
    store = SqliteStore()
    store.init_schema()
    model = _StubModelT173('{"intent":"logistics"}')
    ident = AgentIdentity(agent_id="t173-probe", role="cs_front_desk", duty="测试理解层",
                          allowed_skills=frozenset({"cs.understand"}),
                          allowed_tools=frozenset(), max_risk="L", model_tier=Tier.LIGHT)
    res = SkillInvoker(ident, store).invoke("cs.understand", {
        "tenant_id": TENANT_T173, "conversation_id": CONV_T173, "turn_id": TURN_T173,
        "text": _OFF_TOPIC_T173}, extras={"task_id": TURN_T173, "trace_id": "", "model": model})
    assert res.status == "ok" and res.output["source"] == "model"
    (row,) = store.list_model_usage()
    assert (row["plan_id"], row["trace_id"], row["task_id"]) == ("", "", None)
    invoked = [e for e in store.list_event_log("") if e["event_type"] == "SkillInvoked"]
    assert len(invoked) == 1 and invoked[0]["plan_id"] == row["plan_id"]
    assert store.list_event_log(PLAN_T173) == []


def test_model_failure_records_one_failure_row_and_falls_back_to_rules_t173():
    store = SqliteStore()
    store.init_schema()
    model = _StubModelT173(exc=RuntimeError("gateway down"))
    res = _invoke_understand_t173(store, _OFF_TOPIC_T173, model)
    assert res.status == "ok"                      # 不抛：模型坏了不该让客户被转人工
    assert (res.output["intent"], res.output["source"]) == (T.INTENT_UNKNOWN, "rule")
    assert store.list_model_usage() == []
    (row,) = store.list_model_call_failures()
    assert row["call_site"] == U.CALL_SITE
    assert (row["plan_id"], row["trace_id"], row["task_id"]) == (PLAN_T173, "", None)


@pytest.mark.parametrize("model", [None, ScriptedModelClient({"": '{"intent":"logistics"}'})],
                         ids=["none", "scripted"])
def test_scripted_or_no_model_means_zero_rows_t173(model):
    store = SqliteStore()
    store.init_schema()
    res = _invoke_understand_t173(store, _OFF_TOPIC_T173, model)
    assert (res.output["intent"], res.output["source"]) == (T.INTENT_UNKNOWN, "rule")
    assert store.list_model_usage() == [] and store.list_model_call_failures() == []
    if model is not None:
        assert model.calls == []


@pytest.mark.parametrize("raw,want", [
    ('{"intent":"refund_payment"}', T.INTENT_REFUND_PAYMENT),
    ('{"intent":"refund"}', T.INTENT_UNKNOWN),           # 枚举外 → unknown
    ('{"intent":"  general "}', T.INTENT_GENERAL),
    ('["logistics"]', T.INTENT_UNKNOWN),
    ("not json", T.INTENT_UNKNOWN),
    ("", T.INTENT_UNKNOWN),
])
def test_model_output_is_clamped_to_intents_t173(raw, want):
    store = SqliteStore()
    store.init_schema()
    u = U.understand(_OFF_TOPIC_T173, prior_slots={}, model=_StubModelT173(raw), store=store,
                     plan_id=PLAN_T173)
    assert (u.intent, u.source) == (want, "model")
    assert len(store.list_model_usage()) == 1


def test_skill_contract_and_output_shape_t173():
    cls = registry.get("cs.understand")
    assert cls is not None
    contract = cls.contract
    assert contract.version == "1.0.0" and contract.owner_roles == []
    assert contract.depends_tools == [] and contract.failure_policy == "escalate"
    assert contract.max_retries == 0
    assert "只读" in contract.security_boundary and "不调任何工具" in contract.security_boundary
    assert set(contract.output_schema) == {"lang", "intent", "slots", "source"}
    store = SqliteStore()
    store.init_schema()
    text = "订单号2026092400123那件外套我不想要了，帮我申请退货"
    res = _invoke_understand_t173(store, text, None, prior={ports.SLOT_EMOTION: "calm"})
    assert res.status == "ok"
    assert U.Understanding.from_json(res.output) == U.understand(
        text, prior_slots={ports.SLOT_EMOTION: "calm"})
    assert res.output["slots"][ports.SLOT_ORDER_NO] == "2026092400123"
    # SkillInvoked 只有摘要：客户原文、订单号不进 event_log（R5）。
    blob = json.dumps(store.list_event_log(PLAN_T173), ensure_ascii=False)
    assert "SkillInvoked" in blob and "2026092400123" not in blob and "外套" not in blob


# ---------------------------------------------------------------------------
# 7. 不退步
# ---------------------------------------------------------------------------
#: 基线 bf53df6 上真前台跑开发集的实测（53 轮）：intent 53、route 52（CS12-044#1 是已知缺口）。
BASELINE_DEV_T173 = {"turns": 53, "intent_hits": 53, "route_hits": 52}


def test_dev_set_on_the_real_desk_is_not_worse_than_baseline_t173():
    """desk 本轨不改；触发词（复合词白名单、英文）动过之后，开发集的指标不许比基线差。"""
    report = evaluate.run_eval(_desk_factory_t173, evaluate.load_cases())
    assert report.turns == BASELINE_DEV_T173["turns"]
    assert report.route_hits >= BASELINE_DEV_T173["route_hits"], report.describe()
    assert report.intent_hits >= BASELINE_DEV_T173["intent_hits"], report.describe()
    assert report.status_fabrication == 0


def test_dev_set_with_the_hint_wired_in_is_perfect_t173(monkeypatch):
    """§2 第 7 步的接线（检索带 understand 的意图）：开发集全部对上，CS12-044#1 也对上。"""
    _hinted_match_scripts_t173(monkeypatch)
    report = evaluate.run_eval(_desk_factory_t173, evaluate.load_cases())
    assert report.failures == (), report.describe()
    assert report.route_hits == report.turns == BASELINE_DEV_T173["turns"]


def test_t168_holdout_under_the_hint_t173():
    """T168 的检索 holdout（不是 p12 留出集）在提示模式下：无关句与英文句仍全部低于门槛；
    问自己那一单状态的句子不被政策篇以过门槛的分数答掉；改写句过门槛且判对的不少于缺省。"""
    holdout = json.loads(HOLDOUT_T168_PATH.read_text(encoding="utf-8"))
    store = _seeded_t173()
    handoff_of = _handoff_of_scheme_t173()
    for text in holdout["unrelated"] + holdout["english"]:
        hint = U.understand(text, prior_slots={}).intent
        assert _top_t173(store, text, hint=hint)[1] < scripts.MIN_SCRIPT_SCORE, text
    for texts in holdout["status_lookup"].values():
        for text in texts:
            hint = U.understand(text, prior_slots={}).intent
            scheme, score = _top_t173(store, text, hint=hint)
            assert not (score >= scripts.MIN_SCRIPT_SCORE and not handoff_of[scheme]), text
    plain = hinted = 0
    for scheme_no, texts in holdout["rewrites"].items():
        for text in texts:
            top_plain = _top_t173(store, text)
            top_hint = _top_t173(store, text, hint=U.understand(text, prior_slots={}).intent)
            plain += top_plain[0] == scheme_no and top_plain[1] >= scripts.MIN_SCRIPT_SCORE
            hinted += top_hint[0] == scheme_no and top_hint[1] >= scripts.MIN_SCRIPT_SCORE
    # p14 T182：原句检不到时的同义归一把缺省检索也抬了（63 → 67），提示模式 68 → 69；
    # 提示自己的增益钉成 +2，另钉提示模式不比 T173 时差（68）
    assert hinted >= plain + 2 and hinted >= 68, (plain, hinted)
