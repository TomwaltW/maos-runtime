"""p13 T173 · 理解层：语种、槽位、意图（契约 review/p13-cs-contracts.md §1.4 T173、§2 第 0 / 4 / 7 步）。

守七件事：

1. **语种**（``lang.detect_lang``）：有 CJK 即 zh；否则（拿掉单号这类编码串后）字母里 ASCII 字母
   严格过半即 en；否则 zh。
2. **槽位**（``understand.extract_slots``）：订单号的几种平台形态认、手机号（含连字符 / +86）/ 日期 /
   热线 / 型号 / 卡号 / QQ 号 / 身份证号不认；诉求与情绪只出闭集取值；明说要办的分句优先，问规则
   （含问「什么时候」、只带「退款」字眼的裸说法）的句子不给诉求，撤回的诉求（「不用退款了」）给 other；
   商品与问题只从词表来；跨轮合并新值覆盖旧值。
3. **意图**：触发词 → 诉求 → 意图示例（数据表，两边先按同义表归一；实词上像才压过词表，句式像只在
   词表判不出时补位）→ 关键词；顺序、示例的负例、同义归一确实在起作用都钉住；输出恒在 ``types.INTENTS``。
4. **触发词**：几个短词（滚、妈的、他妈、气炸、去死、什么破）只按骂人的句型认，其余词的成类复合词
   （垃圾 + 物件、技术参数 + 补偿、身体不适的恶心……）遮掉；复核点名的句子与本轨事先写好的探针句都钉住
   （这些句子作者看过，是回归判据，不是留出集）；同一句里的真触发词、骂人的说法照认；英文触发词按词认、
   只认诉求的说法；中英混排按同一张优先级表取。
5. **match_scripts 的 intent_hint**：缺省时与 p12（bf53df6 的 scripts.py）逐字节一致 —— 在 p12
   开发集全部轮上比对返回值与 KbRetrieved；给了提示时，时长 / 进度线索把政策篇与查单篇分开
   （自写 22 句正反例），开发集接上提示后满分（含 CS12-044#1）。
6. **模型路径**：只在规则判不出、且注入真模型时调；记一行 usage（plan_id 照传、trace_id 空、
   task_id None）或一行 failure；Scripted / None 零行；输出夹到 INTENTS。
7. **不退步**：真前台（desk 本轨不改）跑开发集，route_hits / intent_hits 不低于基线；
   T168 的 holdout 在提示模式下，无关句仍全部低于门槛、问自己那一单状态的句子不被政策篇答掉。

只用开发集（scenarios/cs/eval/p12_cases.json）、T168 的 cs_scripts_holdout.json 与本文件自写的句子；
不读 p12 留出集。
"""

from __future__ import annotations

import hashlib
import json
import pathlib

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
    ("A1001，谢谢", "A1001"),                                     # 只报一个号 + 客套（复核 L2-9）
    ("A1001 thanks", "A1001"),
    ("电话138-1234-5678，单号2026092400123", "2026092400123"),     # 手机号跳过，取后面的单号
    # 复核 L2-6：短形态后面紧跟单号字眼、或整句是「号 + 诉求词」
    ("A1001 这单到哪了", "A1001"),
    ("A1001退款", "A1001"),
    ("订单号：A1001", "A1001"),
    # 复核 L3-4：身份证 / 银行卡形态**有**单号字眼时照认（平台单号也可能长这样）
    ("订单号 330102199001011234 到哪了", "330102199001011234"),
    ("330102199001011234 这单到哪了", "330102199001011234"),
    ("order id 20260924001234", "20260924001234"),
])
def test_order_no_shapes_are_recognised_t173(text, want):
    assert U.extract_slots(text, lang=lang.detect_lang(text)).get(ports.SLOT_ORDER_NO) == want


@pytest.mark.parametrize("text", [
    "我的手机号13812345678",            # 11 位手机号：隐私，不当单号存
    "13812345678",
    # 复核 L1-1 / L2-4：带连字符、带 86 / +86 国家码的手机号，不带连字符的 400 热线
    "我的单到哪了，电话 138-1234-5678",
    "我的电话是138-1234-5678",
    "+8613812345678",
    "手机 86-138-1234-5678",
    "(+86)13812345678",
    "0086 13812345678",
    "打了 4008123123 没人接",
    "热线 400-8123-123 打不通",
    "座机 0571-88886666",               # 排除了座机，后半截八位数字也不另当单号
    "2026-09-24 那天下的单",            # 日期
    "客服电话400-123-4567打不通",       # 热线
    "我买的iPhone15坏了",               # 型号
    "RTX4090显卡什么时候发",            # 型号（没有订单字眼）
    "1234567到哪了",                    # 7 位纯数字、没有订单字眼
    "我打了12315",
    "GT-3 有货吗",                      # 连字符但数字太少
    "",
    # 复核 L2-6：型号前面的「订单」不紧挨着、英文 order 是动词；离得最近的字眼说的是 QQ / 卡号
    "我订单里的RTX4090显卡还没发",
    "I'd like to order the MX5000, when will it ship?",
    "查一下A1001",                      # 没有单号字眼、整句也不只是这个号
    "我的QQ号是123456789，退款到了吗",
    "退款退到银行卡6222021234567890123了吗",
    "我的会员号 20260924001 能积分吗",
    # 复核 L3-4：卡号 / 身份证号（有字眼的按字眼；没字眼的按形态：身份证、过 Luhn 的银行卡）
    "退款退到这张卡 6222021234567890123 可以吗",
    "我卡号是6222021234567890123，钱退了没",
    "330102199001011234 这个是我的证件",
    "6222021234567890128",
    "请查一下 4111111111111111 到哪了",
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
    ("地址写错了能改吗", ports.REQUEST_OTHER),
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
    # 复核 L3-2：英文的查进度说法
    ("my refund hasn't arrived yet", ports.REQUEST_TRACK),
    ("i haven't received my refund", ports.REQUEST_TRACK),
    ("is my refund processed", ports.REQUEST_TRACK),
    # 复核 r3 L3-2：没有「我要」时，指向自己那一单（本轮有单号 / 这单 / 我那单）或整句就是诉求才给
    ("A1001退款", ports.REQUEST_REFUND),
    ("我那单退款", ports.REQUEST_REFUND),
    ("这单我要退货", ports.REQUEST_RETURN),
    ("收到的杯子有裂纹，能给我换个新的吗", ports.REQUEST_EXCHANGE),
    ("退款", ports.REQUEST_REFUND),
    ("退货吧", ports.REQUEST_RETURN),
    ("那就退货吧", ports.REQUEST_RETURN),
    ("还是退款吧", ports.REQUEST_REFUND),
    ("refund please", ports.REQUEST_REFUND),
    ("麻烦帮我把钱退了", ports.REQUEST_REFUND),            # 「把钱退」是退款，不是退货
    ("不要退款，给我换一件", ports.REQUEST_EXCHANGE),       # 撤回的是退款，换货照给
    ("不需要换货，直接退款", ports.REQUEST_REFUND),
    ("退款到现在都没到", ports.REQUEST_TRACK),
    ("Refund status?", ports.REQUEST_TRACK),
])
def test_request_closed_set_t173(text, want):
    got = U.extract_slots(text, lang=lang.detect_lang(text)).get(ports.SLOT_REQUEST)
    assert got == want and got in ports.REQUEST_VALUES


@pytest.mark.parametrize("text", [
    "退款多久到账",                 # 问规则时长
    "我想退货怎么弄",               # 问流程
    "拆封了还能退吗",               # 问条件
    "坏了能换吗",
    "退货寄回去的运费是我出还是你们出",
    "Can I return this item?",
    "今天天气真好",
    # 复核 L2-2 / L3-2：问「什么时候 / 啥时候 / 何时 / 几号」到账是问时间，不是要退款
    "我的退款什么时候能到账",
    "退款啥时候到账",
    "退款何时到账",
    "退款几号能到",
    "我的退款什么时候退回来",
    "申请退款后钱什么时候退回来",   # 「申请退款后」是个时间点，不是明说要办
    "when will my refund arrive",
    "when will i get my refund",
    # 复核 r3 L3-2：只带「退款 / 退货 / 换货」字眼的裸说法是在问政策（复核意见点名的句子）
    "退款会退运费吗", "退款有手续费吗", "退款要多少时间", "退款要等几日", "退款到账周期是多长", "退款快吗",
    "退款是全额退吗", "退货会影响我的信用分吗", "换货要重新付运费吗", "退款需要审核吗", "退款到账要等多长",
    "Is a refund free?", "Do refunds include shipping?",
    # 本轨事先写好的探针句（r3 改实现之前写成）
    "退款要扣手续费吗", "退货需要自己出运费吗", "退款一般几日到账", "退款会退到原来的卡上吗",
    "退货包装需要完整吗", "Do I get a full refund?", "Is return shipping free?", "How fast is a refund?",
])
def test_policy_questions_carry_no_request_t173(text):
    """问规则的句子不给诉求：否则前台会为一句政策问题去要单号。"""
    assert ports.SLOT_REQUEST not in U.extract_slots(text, lang=lang.detect_lang(text))


#: 复核 L2-2：撤回诉求的说法。前 6 句是复核意见点名的，后 4 句是本轨事先写好的探针句。
WITHDRAWN_T173 = ["不用退款了，东西我留着", "我不要退货了", "我不需要换货", "I don't want a refund",
                  "no need to refund", "退款不用了，谢谢",
                  "我不想退货了，东西挺好的", "算了不换了", "I don't need a refund anymore", "先不退了"]


@pytest.mark.parametrize("text", WITHDRAWN_T173)
def test_withdrawn_request_overwrites_the_earlier_one_t173(text):
    """撤回的诉求不算诉求；本轮没有别的诉求时给 other，盖掉上一轮的 refund（跨轮合并新值覆盖旧值）。"""
    assert U.extract_request(text) == ports.REQUEST_OTHER
    u = U.understand(text, prior_slots={ports.SLOT_ORDER_NO: "A1001",
                                        ports.SLOT_REQUEST: ports.REQUEST_REFUND})
    assert u.slots[ports.SLOT_REQUEST] == ports.REQUEST_OTHER
    assert u.slots[ports.SLOT_ORDER_NO] == "A1001"


@pytest.mark.parametrize("text", ["为什么不给我退款", "you don't refund anything", "they won't refund me",
                                  "我不想要了", "你们怎么还不退款"])
def test_complaints_about_no_refund_are_not_withdrawals_t173(text):
    """怪店家不退、「不想要了」（要退）都不是撤回。"""
    assert U.extract_request(text) != ports.REQUEST_OTHER


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


#: 复核 L2-8：同一句里既有触发词、又和某条意图示例像到足以生效 —— 触发词说了算。
#: 每句都先断言示例确实会判出一个**别的**意图（否则这条判据空转）。
TRIGGER_OVER_EXAMPLE_T173 = [
    ("钱什么时候能回到卡里，你们这是骗子吧", T.INTENT_COMPLAINT),
    ("东西什么时候能到？转人工", T.INTENT_HANDOFF_REQUEST),
    ("收到的东西是坏的，气死我了", T.INTENT_COMPLAINT),
    ("钱什么时候能到，赔我", T.INTENT_COMPENSATION),
    ("我的货到哪了？找人工", T.INTENT_HANDOFF_REQUEST),
]


@pytest.mark.parametrize("text,want", TRIGGER_OVER_EXAMPLE_T173)
def test_trigger_wins_over_intent_examples_t173(text, want):
    assert U.example_intent(text) not in (T.INTENT_UNKNOWN, want), text
    assert U.rule_intent(text) == want
    assert U.understand(text, prior_slots={}).intent == want


def test_rule_intent_order_is_trigger_request_example_keywords_t173(monkeypatch):
    """顺序钉死：触发词 → 本轮诉求 → 示例 → 关键词。把示例换成「永远判通用」的桩，
    触发词句与诉求句不受影响，没有诉求的句子由示例压过关键词。"""
    monkeypatch.setattr(U, "example_intent", lambda text, votes=None: T.INTENT_GENERAL)
    assert U.rule_intent("帮我转人工") == T.INTENT_HANDOFF_REQUEST
    assert U.rule_intent("我要退款", request=ports.REQUEST_REFUND) == T.INTENT_RETURN_EXCHANGE
    assert U.rule_intent("我的快递到哪了", request=ports.REQUEST_TRACK) == T.INTENT_LOGISTICS
    assert U.rule_intent("包邮吗") == T.INTENT_GENERAL                     # 示例压过词表
    monkeypatch.setattr(U, "example_intent", lambda text, votes=None: T.INTENT_UNKNOWN)
    assert U.rule_intent("包邮吗") == T.INTENT_LOGISTICS                   # 示例判不出才数票


#: 复核 L2-3 / L3-1：只有虚词骨架像某条示例（「……什么时候能到」「where is my …」「when will my … arrive」
#: 「……什么时候上班」「要另外付……费吗」）的句子，不许被示例拉走，话题词说了算。
EXAMPLE_MUST_NOT_OVERRIDE_T173 = [
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
]


@pytest.mark.parametrize("text,want", EXAMPLE_MUST_NOT_OVERRIDE_T173)
def test_examples_do_not_override_topic_words_t173(text, want):
    assert U.understand(text, prior_slots={}).intent == want


def test_examples_negatives_are_real_near_misses_t173():
    """上面那张负例表不空转：每句都与某条**别的意图**的示例在句式上足够像（按修正前的口径 ——
    不分虚实的 Dice ≥ 0.6 即生效 —— 会被它拉走），而在实词上够不到门槛，是示例的两档门槛挡下来的。"""
    for text, want in EXAMPLE_MUST_NOT_OVERRIDE_T173:
        wrong = [e for e, intent in U.INTENT_EXAMPLES if intent != want]
        shape = max(U.example_shape_similarity(text, e) for e in wrong)
        content = max(U.example_similarity(text, e) for e in wrong)
        assert shape >= 0.6, (text, shape)
        assert content < U.EXAMPLE_MIN_SIMILARITY, (text, content)


#: 复核 L3-5：诉求词表里认作换货 / 退货的说法，意图词表也要投退换货一票（两张表不许各说各的）。
REQUEST_PHRASES_T173 = {
    ports.REQUEST_EXCHANGE: ("换货", "换一件", "换个颜色", "换尺码", "换大一码", "换个小一号", "调换",
                             "exchange", "replacement"),
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


def test_intent_examples_override_the_keyword_table_t173(monkeypatch):
    """意图示例是数据表、确实生效：这几句关键词词表判不出（或判错），靠示例钉住；
    把示例表清空，同一句就回到词表的结果。"""
    cases = [("东西大概什么时候能到呀", T.INTENT_LOGISTICS),
             ("收到的东西怎么是坏的", T.INTENT_RETURN_EXCHANGE),
             ("The item arrived broken.", T.INTENT_RETURN_EXCHANGE)]
    for text, want in cases:
        assert U.example_intent(text) == want, text
        assert U.rule_intent(text) == want, text
        assert U._vote_intent(U.keyword_votes(text)) != want, text      # 词表单独判不对
    monkeypatch.setattr(U, "INTENT_EXAMPLES", ())
    for text, want in cases:
        assert U.rule_intent(text) != want, text


#: 复核 L3-3：示例表的同义改写（只差「啥 / 什么」「哪天 / 几号」「货 / 东西」「破的 / 坏的」
#: 「came damaged / arrived broken」）。前 7 句是复核意见点名的，其余是本轨 r3 改实现之前写好的探针句
#: 里由示例表判出的那几句（作者看过，是回归判据，不是留出集）。
EXAMPLE_PARAPHRASES_T173 = [
    ("东西啥时候能到呀", T.INTENT_LOGISTICS), ("我买的东西啥时候到", T.INTENT_LOGISTICS),
    ("东西大概哪天能到啊", T.INTENT_LOGISTICS), ("货什么时候能送来", T.INTENT_LOGISTICS),
    ("收到的衣服是破的", T.INTENT_RETURN_EXCHANGE), ("东西有毛病想换个", T.INTENT_RETURN_EXCHANGE),
    ("my item came damaged", T.INTENT_RETURN_EXCHANGE),
    ("买的东西什么时间能收到", T.INTENT_LOGISTICS), ("货大概几号能到", T.INTENT_LOGISTICS),
    ("钱哪天能到账", T.INTENT_REFUND_PAYMENT), ("I got charged two times", T.INTENT_REFUND_PAYMENT),
    ("when will my order get here", T.INTENT_LOGISTICS),
]


@pytest.mark.parametrize("text,want", EXAMPLE_PARAPHRASES_T173)
def test_intent_examples_generalise_through_the_synonym_table_t173(text, want):
    assert U.example_intent(text) == want
    assert U.understand(text, prior_slots={}).intent == want


def test_synonym_table_is_what_makes_the_examples_generalise_t173(monkeypatch):
    """反向：把同义归一关掉（中英两张表都清空），上面那批改写至少一半认不出来；把示例表清空也一样
    —— 这两样确实在起作用，不是摆设（r2 的示例表整张删掉指标不变，复核 L3-3）。"""
    texts = [t for t, _ in EXAMPLE_PARAPHRASES_T173]
    wants = [w for _, w in EXAMPLE_PARAPHRASES_T173]

    def wrong() -> int:
        return sum(U.understand(t, prior_slots={}).intent != w for t, w in zip(texts, wants))

    assert wrong() == 0
    with monkeypatch.context() as m:
        m.setattr(U, "_EXAMPLE_SYNONYM_RES", ())
        m.setattr(U, "_EXAMPLE_PRODUCT_RE", U.re.compile(r"(?!)"))
        m.setattr(U, "EN_EXAMPLE_PHRASES", ())
        m.setattr(U, "EN_EXAMPLE_SYNONYMS", {})
        assert wrong() >= len(texts) // 2
    with monkeypatch.context() as m:
        m.setattr(U, "INTENT_EXAMPLES", ())
        assert wrong() >= len(texts) // 2


def test_intent_examples_table_is_well_formed_and_self_written_t173():
    dev = {"".join(kb.tokenize(t)) for c in evaluate.load_cases() for t in c.turns}
    seen = set()
    for example, intent in U.INTENT_EXAMPLES:
        assert intent in T.INTENTS and intent != T.INTENT_UNKNOWN, example
        assert U.example_intent(example) == intent, example      # 自己认得自己
        norm = "".join(kb.tokenize(example))
        assert norm not in dev and norm not in seen, example     # 不抄开发集、不重复
        seen.add(norm)


def test_intent_is_always_in_the_enum_t173():
    texts = [t for c in evaluate.load_cases() for t in c.turns]
    texts += ["", "   ", "!!!", "asdf", "A1001", "🙂", "谢谢", "hello there"]
    for text in texts:
        u = U.understand(text, prior_slots={})
        assert u.intent in T.INTENTS and u.lang in ports.LANGS, text
        assert u.source == ports.SLOT_SOURCE_RULE, text


# ---------------------------------------------------------------------------
# 4. 触发词：骂人的句型、复合词白名单、英文
# ---------------------------------------------------------------------------
#: 自写：商品名 / 物件 / 功效说明里夹着触发词的两个字，整句是普通咨询。
COMPOUND_NOT_TRIGGERS_T173 = [
    "空气炸锅坏了能换吗", "这款气炸锅包邮吗", "垃圾袋什么时候发货", "垃圾桶的盖子裂了",
    "去死皮膏怎么用", "给我妈的生日礼物什么时候能到", "他妈妈说东西少发了一件",
    "麻烦看下有什么破损", "晕车贴能缓解恶心吗", "油漆滚刷发错颜色了",
    "相机的曝光补偿在哪调", "温度补偿器是原装的吗", "人工草坪运费怎么算", "安装要另收人工费吗",
    "有真人实拍图吗", "真人发假发能烫吗", "手机隐私膜贴歪了", "防泄漏水杯漏水了",
    "Do you sell trash cans?", "Is this human hair?", "privacy screen protector for iphone",
    "My name is Sue", "the bottle leaked in the box", "I need a cleaning agent",
    "Do you have a passport holder?", "Please ship to my home address",
]

#: 同一句里既有白名单复合词、又有真触发词：真的照认。
COMPOUND_WITH_REAL_TRIGGER_T173 = [
    ("垃圾桶都比你们的东西结实，什么垃圾店", T.HANDOFF_ANGER),
    ("人工费另算？转人工", T.HANDOFF_REQUESTED),
    ("空气炸锅坏了，气死我了", T.HANDOFF_ANGER),
    ("给我妈的礼物被你们搞砸了，他妈的", T.HANDOFF_ANGER),
    ("曝光补偿坏了，你们得赔偿", T.HANDOFF_COMPENSATION),
    ("隐私膜的订单里有我的身份证号", T.HANDOFF_PRIVACY),
]


@pytest.mark.parametrize("text", COMPOUND_NOT_TRIGGERS_T173)
def test_compound_words_no_longer_trigger_t173(text):
    assert triggers.detect(text) is None


@pytest.mark.parametrize("text,reason", COMPOUND_WITH_REAL_TRIGGER_T173)
def test_real_triggers_next_to_compounds_still_fire_t173(text, reason):
    assert triggers.detect(text) == (reason, triggers.INTENT_OF[reason])


#: 复核 L3-1：只按骂人句型认的几个短词（滚 / 妈的 / 他妈 / 气炸 / 去死 / 什么破）夹在商品名、称谓、
#: 地名、问句里。每句都含那个词的原串（p12 按子串匹配，全都误伤 —— 下面的测试逐句证明），现在不触发。
#: 前一组是 r2 写的句子，中间是复核 L3-1 点名的句子，后一组是本轨 r3 改实现之前写好的探针句。
#: 这些句子作者都看过，是回归判据、不是留出集；泛化由主会话在整合期用 p12 留出集量。
SHAPE_WORDS_IN_BENIGN_T173 = [
    "气炸一体机的说明书丢了", "去死茧的锉刀发了吗", "五一想去死海玩，泳衣能加急吗",
    "给我爸妈的按摩仪发错颜色了", "宝妈的奶粉什么时候发货", "奶妈的工作服能开发票吗",
    "孩子他妈让我问一下运费", "老公他妈买的鞋子码数不对", "其他妈咪包还有货吗",
    "摇滚风的外套还有M码吗", "熊猫滚滚抱枕发货了吗", "滚梳和直板夹哪个好用",
    # 复核 L3-1
    "孕妈的防辐射服能开发票吗", "辣妈的连衣裙有S码吗", "广场舞大妈的音响什么时候能到",
    "衣服为什么破了个洞，能换吗", "这个杯子为什么破了", "你们家有什么破壁机推荐",
    "这款抱枕圆滚滚的好可爱", "粘毛滚的替换纸什么时候发货", "滚石乐队的黑胶唱片", "冰滚美容仪发货了吗",
    "西瓜滚圆滚圆的", "气炸烤箱几天发", "我想去死海玩",
    # 本轨 r3 事先写好的探针句
    "胖滚滚的熊玩偶还有吗", "逗猫滚球能单买吗", "瑜伽滚压按摩棒发货了吗", "汤圆在锅里滚几分钟能熟",
    "婴儿推车的前轮滚不动了", "准妈的防辐射服有XL吗", "舞蹈队大妈的扇子发错颜色了", "买给她妈的围巾颜色不对",
    "二胎妈的待产包能开发票吗", "猫妈的罐头临期了能退吗", "为什么破了一个角能换吗", "有什么破窗器推荐吗",
    "包裹里的纸箱为什么破成这样", "你们有什么破冰游戏道具", "这台多功能气炸电烤箱几天发货",
    "这个清洁剂能去死垢吗", "我想去死亡谷旅游，冲锋衣能加急吗",
]

#: 复核 L3-1：照旧按子串认的词（垃圾 / 恶心 / 补偿 / 人工 / 隐私 / 身份证）的**成类**无害复合词，靠
#: 上下文写法（BENIGN_PATTERNS）遮掉。来源同上（r2 句、复核点名句、r3 事先写好的探针句）。
CONTEXT_PATTERN_CASES_T173 = [
    "垃圾兜能挂在门后面吗", "请问垃圾罐的尺寸多大", "卧室空气死角多，这台风扇够用吗",
    "孕吐恶心吃什么好，有推荐的零食吗", "晕船恶心的贴片有吗", "这药吃了会不会恶心", "黑店镇能发顺丰吗",
    "孕妇吃了恶心想吐", "吃完恶心反胃是正常的吗", "显示器有背光补偿吗", "逆光补偿怎么打开", "垃圾挂袋能挂在哪",
    "吃了这个益生菌有点恶心正常吗", "孕早期恶心呕吐能用这个贴吗", "闻到这个香薰有点恶心头晕",
    "投影仪的梯形补偿怎么设置", "这个稳压器带功率补偿吗", "耳机的低音补偿能关吗", "厨房垃圾架的螺丝少发了",
    "宠物用的垃圾拾取器多少钱", "人工鱼饵套装发货了吗", "隐私门帘能定做尺寸吗", "身份证卡套有透明的吗",
    "稳压器的补偿电容坏了",
]
EN_CONTEXT_PATTERN_CASES_T173 = [
    "Do you sell trash grabbers?", "What size is the rubbish chute cover?",
    "Is this picture representative of the color?", "What is the shop phone number?",
]

#: 复核 L2-1 / L2-5：句型与白名单**不许**盖住的说法 —— 这些都要照旧触发（宁可多转、不可漏转）。
#: 「你老妈的 / 你妈妈的 / 他妈妈的 / 去你妈」是复核 L2-1 点名的（r2 的整词白名单把它们遮掉了）。
MUST_STILL_TRIGGER_T173 = [
    ("请保证不泄漏我的信息", T.HANDOFF_PRIVACY), ("你们能保证不泄漏我的数据吗", T.HANDOFF_PRIVACY),
    ("你们就是垃圾站", T.HANDOFF_ANGER), ("你们这垃圾车一样的服务", T.HANDOFF_ANGER),
    ("你们这垃圾处理方式太差了", T.HANDOFF_ANGER), ("他妈的什么破店", T.HANDOFF_ANGER),
    ("你妈的", T.HANDOFF_ANGER), ("婆婆他妈的", T.HANDOFF_ANGER), ("去死吧", T.HANDOFF_ANGER),
    ("恶心死了", T.HANDOFF_ANGER), ("你们真恶心", T.HANDOFF_ANGER), ("气炸了", T.HANDOFF_ANGER),
    ("滚滚滚", T.HANDOFF_ANGER), ("垃圾货", T.HANDOFF_ANGER),
    ("my phone number was leaked", T.HANDOFF_PRIVACY), ("I want a representative", T.HANDOFF_REQUESTED),
    ("let me speak to a representative", T.HANDOFF_REQUESTED),
    # 复核 L2-1
    ("去你老妈的，东西到底什么时候发", T.HANDOFF_ANGER), ("操你老妈的", T.HANDOFF_ANGER),
    ("你妈妈的", T.HANDOFF_ANGER), ("去你妈妈的，退款呢", T.HANDOFF_ANGER),
    ("你个老妈的，退款呢", T.HANDOFF_ANGER), ("你们客服他妈妈的就知道拖", T.HANDOFF_ANGER),
    ("他妈的", T.HANDOFF_ANGER),
    # 本轨 r3 事先写好的探针句（真骂人的各种句型）
    ("你老妈的，什么时候发货", T.HANDOFF_ANGER), ("操你妈的什么时候退款", T.HANDOFF_ANGER),
    ("你妈妈的，退款呢", T.HANDOFF_ANGER), ("他妈的等了一周了", T.HANDOFF_ANGER),
    ("妈的，快递又没动", T.HANDOFF_ANGER), ("这快递妈的太慢了", T.HANDOFF_ANGER),
    ("真他妈的服了", T.HANDOFF_ANGER), ("滚蛋吧你们", T.HANDOFF_ANGER), ("给我滚", T.HANDOFF_ANGER),
    ("你们都滚", T.HANDOFF_ANGER), ("滚！", T.HANDOFF_ANGER), ("爱卖不卖，滚", T.HANDOFF_ANGER),
    ("爱卖不卖 滚", T.HANDOFF_ANGER), ("什么破东西", T.HANDOFF_ANGER),
    ("什么破快递三天不动", T.HANDOFF_ANGER), ("这是什么破手机", T.HANDOFF_ANGER),
    ("你们真让人恶心", T.HANDOFF_ANGER), ("看到你们的回复就恶心", T.HANDOFF_ANGER),
    ("吃了你们的东西恶心死了", T.HANDOFF_ANGER), ("必须给我补偿", T.HANDOFF_COMPENSATION),
    ("这事你们得补偿我", T.HANDOFF_COMPENSATION), ("去你妈的", T.HANDOFF_ANGER),
    ("滚你的吧", T.HANDOFF_ANGER), ("你他妈妈的就知道拖", T.HANDOFF_ANGER),
    ("你们客服他妈就知道拖", T.HANDOFF_ANGER), ("我快气炸了", T.HANDOFF_ANGER),
    ("你们怎么不去死", T.HANDOFF_ANGER),
]

#: 只按句型认的几个词（p12 里都按子串认）。
SHAPE_WORDS_T173 = ("滚", "妈的", "他妈", "气炸", "去死", "什么破")


@pytest.mark.parametrize("text", SHAPE_WORDS_IN_BENIGN_T173)
def test_shape_words_in_benign_compounds_do_not_trigger_t173(text):
    """每句都含一个句型词的原串（p12 按子串匹配必然误伤），现在不触发：句型之外不算。"""
    norm = triggers.normalize(text)
    assert [w for w in SHAPE_WORDS_T173 if w in norm], text
    assert triggers.detect(text) is None


def test_shape_words_are_matched_only_as_insult_shapes_t173():
    """设计钉子：句型词不在按子串认的词表里（不然句型白写），而在句型表里。"""
    literal = set(triggers._EXTRA_PATTERNS[T.HANDOFF_ANGER])
    assert not literal & {"他妈", "妈的", "气炸", "去死", "什么破"}
    shapes = "".join(triggers.INSULT_SHAPES[T.HANDOFF_ANGER]) + triggers._GUN_RE
    for word in ("滚", "妈的", "他妈", "气炸", "去死", "什么破"):
        assert word in shapes, word


def test_context_patterns_fix_their_classes_t173():
    """照旧按子串认的词：每句都夹着一个触发词的原串，现在不触发，靠的是上下文写法。"""
    for text in CONTEXT_PATTERN_CASES_T173:
        norm = triggers.normalize(text)
        assert any(triggers._PATTERNS[r].search(norm) for r in triggers.PRIORITY), text
        assert triggers.detect(text) is None, text
    for text in EN_CONTEXT_PATTERN_CASES_T173:
        words = triggers._spaced_words(text)
        assert not [w for w in triggers.EN_BENIGN_COMPOUNDS if w in words], text
        assert triggers.detect(text) is None, text


def test_every_context_pattern_is_exercised_t173():
    """每条上下文写法都至少被一句用到（没用到的就是摆设），且它遮的那几个字（写了命名组 w 的只算那一组）
    确实压在一处触发词上（与未遮的原文里某个触发词的位置有重叠）。"""
    import re

    for group, pats in triggers.BENIGN_PATTERNS.items():
        assert group in triggers.PRIORITY
        for p in pats:
            compiled = re.compile(p)
            used = 0
            for text in CONTEXT_PATTERN_CASES_T173:
                norm = triggers.normalize(text)
                spans = [m.span() for m in triggers._PATTERNS[group].finditer(norm)]
                for m in compiled.finditer(norm):
                    s0, e0 = m.span("w") if "w" in compiled.groupindex else m.span()
                    used += 1
                    assert any(s < e0 and s0 < e for s, e in spans), (p, text)
            assert used, p
    for p in triggers.EN_BENIGN_PATTERNS:
        assert any(re.search(p, triggers._spaced_words(t)) for t in EN_CONTEXT_PATTERN_CASES_T173), p


@pytest.mark.parametrize("text,reason", MUST_STILL_TRIGGER_T173)
def test_real_triggers_are_not_masked_t173(text, reason):
    assert triggers.detect(text) == (reason, triggers.INTENT_OF[reason])


def test_every_whitelisted_compound_really_contains_a_trigger_t173():
    """白名单每一条都真的夹着触发词（否则这一条是摆设），而整条单说不触发。英文的整词在「a + 整词」
    里含触发词即可（human hair 单说不是在找人，real / a human hair 就是）。"""
    for group, words in triggers.BENIGN_COMPOUNDS.items():
        assert group in triggers.PRIORITY
        for word in words:
            norm = triggers.normalize(word)
            assert norm == word, word                      # 写成规范化后的形态
            assert any(triggers._PATTERNS[r].search(norm) for r in triggers.PRIORITY), word
            assert triggers.detect(word) is None, word
    for word in triggers.EN_BENIGN_COMPOUNDS:
        assert any(triggers._EN_RES[r].search(word) or triggers._EN_RES[r].search(f"a {word}")
                   for r in triggers.PRIORITY), word
        assert triggers.detect(word) is None and triggers.detect(f"a {word}") is None, word
    # 复核 L2-1 / L2-5：否定短语、骂人时也用的词、称谓 + 妈的 都不在白名单里。
    all_words = {w for ws in triggers.BENIGN_COMPOUNDS.values() for w in ws}
    assert not all_words & {"不泄漏", "垃圾站", "垃圾车", "垃圾处理", "他妈妈", "妈妈的", "老妈的"}
    assert not [w for w in all_words if "妈" in w]


#: 自写的英文触发句：每类至少两种说法。
EN_TRIGGERS_T173 = {
    T.HANDOFF_REQUESTED: ["I want to talk to a human", "Can I speak to a real person?",
                          "live agent please", "get me your manager", "I don't want a bot",
                          "human please", "connect me to an operator", "human"],
    T.HANDOFF_COMPLAINT: ["I will file a complaint", "My lawyer will contact you",
                          "I'm going to sue you", "I'll report you to consumer protection",
                          "I will report your store"],
    T.HANDOFF_ANGER: ["This is a scam", "What a rip-off", "your service is garbage",
                      "WTF is going on", "you ripped me off", "what a rip off"],
    T.HANDOFF_COMPENSATION: ["I demand compensation", "You need to compensate me"],
    T.HANDOFF_PRIVACY: ["delete my personal data", "why do you have my phone number",
                        "this is a privacy issue", "my mobile number got leaked"],
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
    # 复核 L3-4：p12 不转人工的普通英文，本轨的英文表也不许转
    "Do you sell a trash compactor?", "I bought a garbage truck toy for my son",
    "Is this color representative of the real product?", "Where is your privacy policy?",
    "What is your store phone number?",
    # 复核 L2-7：operator / human / rip off / report 的普通用法
    "Does this phone work with any operator?", "is this safe for humans?",
    "rip off the tags, can I still return it?", "I want to report this issue with my order",
    "Is this real human hair?", "The operator manual is missing",
])
def test_ordinary_english_questions_do_not_trigger_t173(text):
    """「refund me now」这类要钱的说法不进触发词（退款诉求走核验 → 查单 → 预检卡）；
    按词认：issue 里的 sue、inhumane 里的 human 不算；只认诉求的说法（复核 L2-7）。"""
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
    assert hinted >= plain + 3, (plain, hinted)
