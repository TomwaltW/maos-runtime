"""T182 · 理解层泛化（p14 契约 §2「T182」，p13 欠账）。

主会话从 p12 留出集归纳了五个误判**类别**（不给句子）；本文件的例句全部是本轨按类别自写的
（没有打开任何留出集与它的测试），每类 ≥ 8 种说法，另配「相近但不该命中」的反例。

1. 寒暄开场的口语变体（叠字、波浪号、语气词、「打扰一下」、称呼）→ 问候篇 GEN-001；
2. 具体订单的进度 / 异常、不带「发货 / 物流 / 退款」一类诉求词 → p12 路径 needs_order_lookup 转人工，
   p13 路径进查单（缺单号就追问）；
3. 政策问答的口语说法 → 对应的政策篇；
4. 辱骂客服质量 → 情绪激烈（anger）；
5. 连续兜底连带打成 silent —— 修好 1–4 自然消解，**不改**连续兜底规则（本文件钉住规则没动）。

另有契约点名的两件顺手活：条件威胁（「不 X，我就 <真后果动作>」）判 complaint；``lang`` 公开
``drop_codes`` / ``has_lang_signal``，``desk.has_lang_signal`` 不再调私有名。

不变量（每条都有测试）：p12 / p13 开发集照旧；编造 0；零「自信答错」（route=answer 的轮意图必对）；
触发词只加不减（R1）；连续兜底规则不动；同义归一只在原句检不到时启用（开发集检索逐字节同 p12，
由 test_cs_understand_t173 的摘要表钉）；归一表不抄开发集。
"""

from __future__ import annotations

import inspect
import json
import re

import pytest

from maos import kb
from maos.core.store import SqliteStore
from maos.domain.cs import desk as D
from maos.domain.cs import evaluate, lang, scripts, triggers
from maos.domain.cs import types as T
from maos.domain.cs import understand as U
from maos.domain.cs.corpus import seed_cs_kb
from maos.domain.cs.desk import CsConfig, FrontDesk

_ANS, _HO, _FB, _CLAR = T.ROUTE_ANSWER, T.ROUTE_HANDOFF, T.ROUTE_FALLBACK, T.ROUTE_CLARIFY
_LOG, _PAY, _RET, _GEN = (T.INTENT_LOGISTICS, T.INTENT_REFUND_PAYMENT, T.INTENT_RETURN_EXCHANGE,
                          T.INTENT_GENERAL)
_LOOKUP = T.HANDOFF_NEEDS_ORDER_LOOKUP
TENANT_T182 = "tnt-demo"

# ---------------------------------------------------------------------------
# 自写例句（按类别）
# ---------------------------------------------------------------------------
#: 类别 1：整句寒暄。期望：两条路径都答问候篇。
GREETINGS_T182 = [
    "在嘛在嘛", "亲在不在呀", "嗨～", "哈喽哈喽亲～", "喂喂", "亲，在吗？", "有人没", "老板在吗",
    "早呀", "亲亲在嘛~~", "打扰一下哈", "小姐姐在不在", "掌柜的在吗", "上午好", "哎 有人吗～",
    "在不在啊老板",
]
#: 类别 1 的反例：带真问题、或只有称呼 / 笑声 —— 不许被问候篇答掉。
GREETING_NEGATIVES_T182 = [
    ("在吗，包邮吗", _ANS, _LOG), ("你好，我要退货", _HO, _RET), ("哈哈哈", _FB, T.INTENT_UNKNOWN),
    ("老板", _FB, T.INTENT_UNKNOWN), ("你好，这件衣服有没有大码", _FB, T.INTENT_UNKNOWN),
    ("打扰一下，这款有现货吗", _FB, T.INTENT_UNKNOWN), ("亲在吗，能便宜点不", _FB, T.INTENT_UNKNOWN),
    ("老板在吗 我想买两件", _FB, T.INTENT_UNKNOWN), ("嘿嘿嘿", _FB, T.INTENT_UNKNOWN),
    ("嘻嘻", _FB, T.INTENT_UNKNOWN),
]

#: 类别 2：具体订单的进度 / 异常（不带发货 / 物流 / 退款这类诉求词为主）。
#: (句子, 意图, p12 该转的查单篇)。p13 路径（空夹具端口、句中无单号）期望追问单号。
ORDER_ANOMALIES_T182 = [
    ("快递卡在转运中心三天了不更新", _LOG, "LOG-004"),
    ("我的件在分拣中心放了五天", _LOG, "LOG-004"),
    ("快递一直显示运输中不动", _LOG, "LOG-004"),
    ("我买的衣服在转运中心躺了四天", _LOG, "LOG-004"),
    ("上面写已签收，可家里没人收到", _LOG, "LOG-006"),
    ("快递说放驿站了可我去找没有", _LOG, "LOG-006"),
    ("快递员说放门口了但我回家没看到", _LOG, "LOG-006"),
    ("拆开一看东西碎了", _LOG, "LOG-006"),
    ("杯子寄过来就是碎的", _LOG, "LOG-006"),
    ("外包装全湿了", _LOG, "LOG-006"),
    ("商家同意退了，钱呢", _PAY, "PAY-003"),
    ("卖家答应退了可一直没见到钱", _PAY, "PAY-003"),
    ("支付的时候扣了两遍钱", _PAY, "PAY-004"),
    ("同一笔订单付了两次", _PAY, "PAY-004"),
]
#: 类别 2 里的改地址：两条路径都是 needs_order_lookup 转人工（p13 的诉求是 other，不进查单，
#: 见 docs/DECISIONS.md task-t182）。
ADDRESS_CHANGES_T182 = ["下完单发现地址填成老家的了", "地址写错了想换一个", "下单的时候地址选错了",
                        "收件人电话写错了能改吗", "我要把地址改成学校", "能不能帮我把收货地址换成公司的",
                        "我刚下的单想改一下收货地址", "收货人信息填错了要改一下"]
#: 类别 2 的反例：说到物流 / 签收 / 扣款的字眼，但不是在说自己那一单出了事。
ANOMALY_NEGATIVES_T182 = ["签收需要本人吗", "中转站在哪", "驿站几点关门", "快递员态度很好",
                          "重复购买有优惠吗", "物流信息一般多久更新", "包装盒好看吗", "这个杯子碎了能赔吗",
                          "物流公司是哪家的",
                          # 复核 L2-4 / L3-2：「件 / 信息 / 多收 / 一直在 / 几天不动 / 扁凹瘪」的误伤
                          "我想多收藏几个商品", "文件丢了", "我多收了一件", "满两件是不是多收一次运费",
                          "盒子扁扁的是设计吗", "收纳箱子凹进去的那种有吗", "这个包装盒是瘪的还是鼓的",
                          "我一直在路上晚点再收快递", "我一直在仓库上班能兼职吗", "我的会员信息没更新帮我改一下资料",
                          "这件衣服穿了几天不走样吗", "手表放几天不动会停吗", "这个配件卡住了怎么拆", "软件卡住了怎么办",
                          "已经签收了没看到说明书", "你们店铺的上新信息好几天没更新了",
                          # 本轨自补的同类近邻
                          "零件丢了能单独买吗", "拉链卡住了拉不动", "鞋盒子压了会变形吗", "证件丢了能补办吗",
                          # 二轮复核 L3-4：问规则 / 假设（是不是要扣两次、万一卡在海关）不是自己那一单出了事
                          "预售定金和尾款要付两次吗", "分期是不是要扣两次款", "会员是每个月扣一次还是扣两次",
                          "万一包裹卡在海关不动怎么办", "如果快递丢了怎么赔", "要是包裹破了的话怎么办"]

#: 类别 3：政策问答的口语说法。(句子, 意图, 该答的政策篇)。
POLICY_T182 = [
    ("衣服没穿过标签也没剪可以退吗", _RET, "RET-001"),
    ("吊牌没摘可以退货不", _RET, "RET-001"),
    ("试穿了一下不喜欢能退吗", _RET, "RET-001"),
    ("退东西要先寄回去还是先申请", _RET, "RET-002"),
    ("退货第一步干嘛", _RET, "RET-002"),
    ("退货应该先干嘛", _RET, "RET-002"),
    ("寄回来的运费要我承担吗", _RET, "RET-004"),
    ("退回去的钱谁出", _RET, "RET-004"),
    ("洗完褪色严重可以换一件吗", _RET, "RET-003"),
    ("衣服洗过之后缩水了能换吗", _RET, "RET-003"),
    ("耳机一边没声音可以换吗", _RET, "RET-003"),
    ("你们发货到西藏吗", _LOG, "LOG-002"),
    ("乡下能送到吗", _LOG, "LOG-002"),
    ("大件家具走什么物流", _LOG, "LOG-002"),
    ("冰箱这种大件发什么物流", _LOG, "LOG-002"),
    ("今天买今天能发吗", _LOG, "LOG-001"),
    ("退款退回哪个账户", _PAY, "PAY-001"),
    ("用支付宝付的退款退到哪", _PAY, "PAY-001"),
    ("零钱付的退款要几天", _PAY, "PAY-001"),
]
#: 类别 3 的反例：碰到吊牌 / 膜 / 掉色 / 大件 / 钱包这些字眼，但不是在问售后政策 —— 落兜底。
POLICY_NEGATIVES_T182 = ["吊牌上的价格是多少", "标签上写的什么面料", "这个膜是送的吗", "这个颜色会不会掉色",
                         "洗了会不会缩水", "大件的有优惠吗", "钱包有现货吗", "没穿过这种款式好看吗",
                         "花呗能分几期",
                         # 复核 L2-2 / L3-1：配送范围 / 快递公司 / 发货时间 / 退货流程 / 质量换货的误伤
                         "送去的是正品吗", "送到手的东西有保修吗", "寄去的礼物能写贺卡吗", "一般什么时候发工资",
                         "香港能买到这款吗", "我在国外可以用这个充电器吗", "国外品牌能用国内电压吗",
                         "农村老人用的手机可以推荐一款吗", "山区能用吗", "进口的国外奶粉能喝吗", "用京东支付可以吗",
                         "可以用京东白条吗", "用邮政储蓄卡付款可以吗", "走京东买的更便宜吗", "退订短信怎么弄",
                         "会员怎么退订咋整", "我家洗衣机坏了你们有什么推荐换一台", "旧手机开不了机了想换个新手机推荐一款",
                         # 本轨自补的同类近邻
                         "寄去的包裹要自己贴单吗", "一般什么时候发红包", "最快什么时候发布新款", "走顺丰会员有折扣吗",
                         "新疆产的红枣甜吗",
                         # 二轮复核 L2-1 / L3-1：换的不是商品本身（换零件 / 换设置 / 换季 / 换话题）
                         "开不了机怎么换电池", "手表坏了怎么换表带", "灯不亮了怎么换灯泡", "换季了衣服起球怎么办",
                         "我换个问题，衣服起球怎么办", "手机充不进电，要换根线吗", "灯不亮了要换灯泡吗",
                         "耳机没声音了是不是要换个模式", "电脑开不了机要换系统吗", "屏幕不亮是不是要换个设置",
                         # 二轮复核 L2-2 / L3-1：「退」不是退货（退烧 / 退热 / 退火 / 退订 / 退出）
                         "没用过退烧贴好用吗", "没试过这个退烧贴效果怎么样", "这个退热贴怎么弄",
                         "退订花呗后多久生效", "退出后怎么回到原来的界面",
                         # 二轮复核 L3-1：地名是产地不是送达地；「发」的宾语不是货
                         "西藏发货的牦牛肉干正宗吗", "新疆发的哈密瓜甜吗", "海南寄过来的芒果新鲜吗",
                         "新疆寄来的红枣好吃吗", "国外发的奶粉安全吗", "香港发货的化妆品是真的吗",
                         "寄到国外的明信片好看吗", "最快什么时候发新品", "大概什么时候发通知"]
#: 类别 3 的反例里基线 45f724d 就已经答了的（检索直接召回 RET-002，同义归一不参与）：不进上面那张
#: 「p12 路径落兜底」的表，在零自信答错那条里按 :data:`PREEXISTING_WRONG_ANSWERS_T182` 逐字钉住。
BASELINE_ANSWERED_NEGATIVES_T182 = ["退火炉怎么操作"]

#: 类别 4：辱骂客服质量 → anger。
ANGER_T182 = ["破客服有什么用", "你们这破客服", "被你们气疯了", "真是气疯我了", "什么鬼客服啊", "废物客服",
              "烂客服", "你们客服是不是有病", "你们客服是死人吗", "客服就是个摆设", "你是白痴吗",
              "这是什么烂系统", "气得我要死"]
#: 类别 4 的反例：商品名、日常说法里夹着这些字。
ANGER_NEGATIVES_T182 = ["废物利用的收纳盒有吗", "破洞牛仔裤有吗", "这件衣服是破洞款吗", "狗粮什么时候发货",
                        "气垫梳有吗", "摆设用的花瓶有吗", "包装破了", "鬼节有活动吗",
                        # 复核 L3-4：商品咨询里夹着「狗服务 / 有病 / 摆设 / 死人」
                        "有没有遛狗服务", "你们有病号服吗", "你有病历本卖吗", "你们摆设的那个花瓶还有吗",
                        "你们是死人头牌的代理吗", "有宠狗服务吗", "你们有病人用的护理垫吗",
                        # 二轮复核 L2-4 / L3-3：「狗」是宠物、「白痴 / 弱智 / 神经病」不是冲着客服说的
                        "有没有狗狗服务", "有宠物狗服务吗", "你们有训狗服务吗", "狗狗app怎么下载",
                        "宠物狗软件推荐一下", "训狗服务怎么收费", "有没有上门洗狗服务", "神经病学的书有吗",
                        "弱智吧的段子你看过吗", "这个说明书写得白痴都能看懂", "你们客服真是神经病医院推荐的吗"]

#: 顺手：条件威胁（真后果动作）→ complaint。
THREATS_T182 = ["不退我就给差评", "不处理的话我就去黑猫", "不退款我就发微博", "今天不解决我就找媒体",
                "要是还不发货我就去网上说", "再拖着不处理我就报警", "不处理我就挂网上", "再不发货我就给差评",
                "今天不给我退款我就报警", "要是再拖我就上网说", "否则我就找媒体"]
#: 条件威胁的反例：表态、拿主意、纯发泄、拒收（拒收不是向外升级，照旧走退款诉求）。
THREAT_NEGATIVES_T182 = ["我不会给差评的", "不退款我就自己留着用吧", "不退就不退吧，我认了",
                         "不退款的话我会很失望", "不退的话我就去朋友家拿", "不退款，我就拒收",
                         "为什么这么多差评", "好评返现吗",
                         # 复核 L2-1：否定的后果（承诺不升级）
                         "如果满意我就不会给差评", "要是质量好就不打差评", "要是能退我就不去黑猫了",
                         "如果今天发货就不用找平台了", "要是按时到我就不会找媒体", "不退也没事我就不发微博了",
                         # 复核 L3-3：「不 / 没 / 如果」不是条件、夸奖、问句
                         "不好意思，我就是想问问差评能删吗", "东西不错就是物流慢，不会给差评",
                         "用了不到一周就发微博夸你们了", "没想到这么快就发朋友圈了", "不到十分钟就在网上评论了",
                         "如果不满意就可以给差评吗", "不一会儿就发朋友圈晒了好评",
                         # 二轮复核 L3-2：条件是好事或求助，不是对方不办事
                         "如果质量好我就发小红书推荐一下", "要是东西好用，我就发抖音安利给朋友",
                         "如果收到了我就上网说说使用感受", "如果满意我就在网上评个五星", "万一我忘了我就找平台客服问",
                         "如果需要发票我就找平台开", "要是今天能发，我就发朋友圈帮你们宣传", "假如好用我就发帖子推荐",
                         "如果有优惠我就发朋友圈分享", "要是能便宜点我就找平台领券",
                         "要不然我找平台问问", "要不然我发朋友圈问问朋友哪个好"]
#: 条件与动作都成立、**只**被动作之后的「夸 / 好评 / 推荐 / 吗 …」排除的句子（二轮复核 L2-3：删掉那段
#: 后顾断言，这几句要变红）。
THREAT_TAIL_NEGATIVES_T182 = ["不退的话就给差评吗", "不回复也没事，我就发朋友圈夸你们",
                              "不发货也行，我就发小红书推荐别家", "不退的话我就去黑猫么"]


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def _desk_t182(ports: dict | None = None) -> FrontDesk:
    store = SqliteStore(":memory:")
    seed_cs_kb(store)
    return FrontDesk(store, CsConfig(tenants={"wk_eval": "tnt-demo"}, handoff_target=None),
                     **(ports or {}))


def _empty_ports_t182(customer: str) -> dict:
    empty = evaluate.EvalFixtures()
    return {"verifier": evaluate.FixtureVerifier(empty, tenant_id=TENANT_T182, external_userid=customer),
            "lookup": evaluate.FixtureLookup(empty), "precheck": evaluate.FixturePrecheck(empty)}


def _run_t182(texts, *, p13: bool, cid: str = "T182"):
    """一段会话里依次说 ``texts``，返回每轮的 DeskResult。p13=True 注入空夹具端口。"""
    case = evaluate.EvalCase(id=cid, turns=tuple(texts),
                             expect=tuple(evaluate.EvalExpect(_FB, T.INTENT_UNKNOWN) for _ in texts))
    desk = _desk_t182(_empty_ports_t182(case.customer) if p13 else None)
    return [desk.handle(evaluate.inbound_for(case, i, t)) for i, t in enumerate(texts, start=1)]


def _one_t182(text: str, *, p13: bool):
    return _run_t182([text], p13=p13)[0]


def _cite_t182(scheme_no: str) -> str:
    return evaluate.cite_doc_id(TENANT_T182, scheme_no)


def _all_sentences_t182() -> list[str]:
    return (GREETINGS_T182 + [t for t, *_ in GREETING_NEGATIVES_T182]
            + [t for t, *_ in ORDER_ANOMALIES_T182] + ADDRESS_CHANGES_T182 + ANOMALY_NEGATIVES_T182
            + [t for t, *_ in POLICY_T182] + POLICY_NEGATIVES_T182 + ANGER_T182 + ANGER_NEGATIVES_T182
            + THREATS_T182 + THREAT_NEGATIVES_T182 + THREAT_TAIL_NEGATIVES_T182
            + BASELINE_ANSWERED_NEGATIVES_T182)


def _without_synonym_norm_t182(monkeypatch) -> None:
    monkeypatch.setattr(scripts, "normalized_query", lambda text: ("", ()))


# ---------------------------------------------------------------------------
# 0. 例句表本身
# ---------------------------------------------------------------------------
def test_each_category_has_eight_distinct_phrasings_and_negatives_t182():
    for table in (GREETINGS_T182, ORDER_ANOMALIES_T182, ADDRESS_CHANGES_T182, POLICY_T182, ANGER_T182,
                  THREATS_T182):
        assert len(table) >= 8 and len(set(map(str, table))) == len(table)
    for table in (GREETING_NEGATIVES_T182, ANOMALY_NEGATIVES_T182, POLICY_NEGATIVES_T182,
                  ANGER_NEGATIVES_T182, THREAT_NEGATIVES_T182):
        assert len(table) >= 5
    everything = _all_sentences_t182()
    assert len(set(everything)) == len(everything)


def test_sentences_are_not_dev_set_sentences_t182():
    """自写句不取自开发集（p12 / p13），也不是话术库登记过的说法（否则重排直接给 1 分，测不出泛化）。"""
    dev = {t for path in (evaluate.EVAL_PATH, evaluate.P13_EVAL_PATH)
           for c in evaluate.load_cases(path) for t in c.turns}
    registered = set()
    for row in kb_rows_t182():
        body = json.loads(row["body"])
        for v in [body["scene"], *body["synonyms"], *body["examples"]]:
            registered |= scripts.tail_forms(v)
    for text in _all_sentences_t182():
        assert text not in dev, text
        assert not (scripts.tail_forms(text) & registered), text


def kb_rows_t182() -> list[dict]:
    from maos.domain.cs import corpus
    return corpus.load_corpus()


# ---------------------------------------------------------------------------
# 1. 寒暄
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("p13", [False, True], ids=["p12", "p13"])
@pytest.mark.parametrize("text", GREETINGS_T182)
def test_greeting_variants_get_the_greeting_script_t182(text, p13):
    assert scripts.greeting_only(text), text
    res = _one_t182(text, p13=p13)
    assert (res.route, res.intent) == (_ANS, _GEN), (text, res.route, res.intent)
    assert res.draft.citations == (_cite_t182("GEN-001"),)


@pytest.mark.parametrize("text,route,intent", GREETING_NEGATIVES_T182)
def test_greeting_plus_real_question_is_not_a_greeting_t182(text, route, intent):
    assert not scripts.greeting_only(text), text
    res = _one_t182(text, p13=False)
    assert (res.route, res.intent) == (route, intent), (text, res.route, res.intent)
    assert _cite_t182("GEN-001") not in res.draft.citations


def test_greetings_fall_back_without_the_synonym_norm_t182(monkeypatch):
    """反向：关掉同义归一，这一类大半落兜底 —— 是归一在起作用，不是话术库本来就认。"""
    _without_synonym_norm_t182(monkeypatch)
    fell = [t for t in GREETINGS_T182 if _one_t182(t, p13=False).route == _FB]
    assert len(fell) >= len(GREETINGS_T182) // 2, fell


# ---------------------------------------------------------------------------
# 2. 具体订单的进度 / 异常
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text,intent,scheme", ORDER_ANOMALIES_T182)
def test_order_anomalies_hand_off_for_lookup_on_the_p12_path_t182(text, intent, scheme):
    res = _one_t182(text, p13=False)
    assert (res.route, res.handoff_reason, res.intent) == (_HO, _LOOKUP, intent), (
        text, res.route, res.handoff_reason, res.intent)
    assert res.draft.citations == (_cite_t182(scheme),)


@pytest.mark.parametrize("text,intent,scheme", ORDER_ANOMALIES_T182)
def test_order_anomalies_enter_the_lookup_branch_on_the_p13_path_t182(text, intent, scheme):
    """p13 路径：理解层把这一单的异常认成查进度（track），前台进查单分支；句中没有单号就追问。"""
    assert scripts.order_anomaly(text), text
    assert U.extract_request(text) == "track", text
    res = _one_t182(text, p13=True)
    assert (res.route, res.ask_slot, res.intent) == (_CLAR, "order_no", intent), (
        text, res.route, res.ask_slot, res.intent)


@pytest.mark.parametrize("p13", [False, True], ids=["p12", "p13"])
@pytest.mark.parametrize("text", ADDRESS_CHANGES_T182)
def test_address_changes_hand_off_for_lookup_t182(text, p13):
    res = _one_t182(text, p13=p13)
    assert (res.route, res.handoff_reason, res.intent) == (_HO, _LOOKUP, _LOG), (
        text, res.route, res.handoff_reason, res.intent)
    assert U.extract_request(text) in ("other", ""), text


@pytest.mark.parametrize("text", ANOMALY_NEGATIVES_T182)
def test_anomaly_negatives_are_not_order_anomalies_t182(text):
    assert not scripts.order_anomaly(text), text


def test_money_anomalies_are_refund_payment_even_without_money_words_t182():
    """「付了两次」里没有「钱」字：查的仍是钱（支付退款），不是包裹。"""
    for text in ("同一笔订单付了两次", "我付款成功了订单却显示未付款", "商家同意退了，钱呢"):
        assert U.rule_intent(text, request=U.extract_request(text)) == _PAY, text


# ---------------------------------------------------------------------------
# 3. 政策问答的口语说法
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("p13", [False, True], ids=["p12", "p13"])
@pytest.mark.parametrize("text,intent,scheme", POLICY_T182)
def test_policy_colloquialisms_get_their_policy_script_t182(text, intent, scheme, p13):
    res = _one_t182(text, p13=p13)
    assert (res.route, res.intent) == (_ANS, intent), (text, res.route, res.intent)
    assert res.draft.citations == (_cite_t182(scheme),), (text, res.draft.citations)


#: 钱没到（没 / 未）是查这一单，不是问退到哪：不许被「退款退到哪」救成原路退回的政策篇（三轮自查）。
REFUND_NOT_ARRIVED_T182 = ["卖家同意退款了可钱一直没回到支付宝", "退的钱过了一周都没退回银行卡",
                           "退款显示成功了却未回到京东白条", "退款审核过了钱未退到信用卡",
                           "退款过了三天还没退回储蓄卡",
                           "店家说退钱了，可退款没退回花呗", "退的钱怎么还没到银行卡"]


@pytest.mark.parametrize("p13", [False, True], ids=["p12", "p13"])
@pytest.mark.parametrize("text", REFUND_NOT_ARRIVED_T182)
def test_refund_not_arrived_is_not_a_route_question_t182(text, p13):
    assert "refund_route" not in scripts.synonym_hits(text), text
    res = _one_t182(text, p13=p13)
    assert res.route != _ANS, (text, p13, res.route, res.draft.citations)


@pytest.mark.parametrize("text", POLICY_NEGATIVES_T182)
def test_policy_negatives_are_not_answered_t182(text):
    res = _one_t182(text, p13=False)
    assert res.route == _FB, (text, res.route, res.intent, res.draft.citations)


def test_policy_colloquialisms_fall_back_without_the_synonym_norm_t182(monkeypatch):
    """反向：关掉同义归一，这一类里有一批回到兜底。"""
    _without_synonym_norm_t182(monkeypatch)
    fell = [t for t, *_ in POLICY_T182 if _one_t182(t, p13=False).route == _FB]
    assert len(fell) >= 5, fell


def test_order_steps_question_is_not_a_return_request_t182():
    """「要先申请还是直接寄回来」是问流程：p13 路径不许为它去要单号。"""
    for text in ("退东西要先寄回去还是先申请", "退货要先申请还是直接寄回来", "退款退回哪个账户"):
        assert U.extract_request(text) == "", text


# ---------------------------------------------------------------------------
# 4. 辱骂客服质量；顺手：条件威胁
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text", ANGER_T182)
def test_insulting_the_service_is_anger_t182(text):
    assert triggers.detect(text) == (T.HANDOFF_ANGER, T.INTENT_COMPLAINT), text
    res = _one_t182(text, p13=False)
    assert (res.route, res.handoff_reason, res.intent) == (_HO, T.HANDOFF_ANGER, T.INTENT_COMPLAINT)


@pytest.mark.parametrize("text", ANGER_NEGATIVES_T182)
def test_anger_negatives_do_not_trigger_t182(text):
    assert triggers.detect(text) is None, text


@pytest.mark.parametrize("text", THREATS_T182)
def test_conditional_threats_are_complaints_t182(text):
    assert triggers.detect(text) == (T.HANDOFF_COMPLAINT, T.INTENT_COMPLAINT), text
    res = _one_t182(text, p13=True)
    assert (res.route, res.handoff_reason) == (_HO, T.HANDOFF_COMPLAINT)


@pytest.mark.parametrize("text", THREAT_NEGATIVES_T182)
def test_venting_and_decisions_are_not_threats_t182(text):
    assert triggers.detect(text) is None, text


@pytest.mark.parametrize("text", THREAT_TAIL_NEGATIVES_T182)
def test_praise_or_question_after_the_act_is_not_a_threat_t182(text):
    """条件（不退 / 不回复）与动作（差评 / 发朋友圈）都在，只是动作后面是夸奖或问句：不算威胁。"""
    assert triggers.detect(text) is None, text
    # 判负的前提：去掉动作之后那段后顾断言，同一条正则就认得出它（排除它的只是那段断言）
    pat = triggers.ADDED_PATTERNS[T.HANDOFF_COMPLAINT][0]
    head = pat[:pat.rindex("(?![")]
    assert re.search(head, triggers.normalize(text)), text


def test_threat_with_an_online_act_keeps_the_request_t182():
    """「不退款，我就上网曝光」：诉求照旧是退款（不是撤回），转人工原因是投诉。"""
    text = "不退款，我就上网曝光"
    assert U.extract_request(text) == "refund"
    assert triggers.detect(text)[0] == T.HANDOFF_COMPLAINT


# ---------------------------------------------------------------------------
# 5. 不变量
# ---------------------------------------------------------------------------
#: 反例表里**基线 45f724d 就已经**答错的（p13 路径的意图提示、或检索本身直接召回；同义归一不启用、
#: 与本轨无关，二轮复核时逐句拿基线实跑核过）：(句子, 是否 p13) → (基线答的意图, 篇)。
#: 只许这一张封闭表，别的一律要对。
PREEXISTING_WRONG_ANSWERS_T182 = {
    ("旧手机开不了机了想换个新手机推荐一款", True): (_RET, "RET-003"),
    ("我换个问题，衣服起球怎么办", True): (_RET, "RET-003"),
    ("退订花呗后多久生效", True): (_PAY, "PAY-001"),
    ("退火炉怎么操作", False): (_RET, "RET-002"),
    ("退火炉怎么操作", True): (_RET, "RET-002"),
}


def test_no_fabrication_and_no_confident_wrong_answer_t182():
    """编造 0；零「自信答错」：自写句里凡是 route=answer 的，意图都对（两条路径）。"""
    expected = {t: _GEN for t in GREETINGS_T182}
    expected.update({t: i for t, i, _s in ORDER_ANOMALIES_T182})
    expected.update({t: i for t, i, _s in POLICY_T182})
    expected.update({t: i for t, _r, i in GREETING_NEGATIVES_T182})
    # 反例表里答了的两句：问快递公司、问发货时间，都答对了
    expected.update({"物流公司是哪家的": _LOG, "狗粮什么时候发货": _LOG})
    # 条件威胁的反例里问发货的那句：基线 45f724d 就答 LOG-001（意图 logistics），不转投诉
    expected.update({"如果今天发货就不用找平台了": _LOG})
    # 二轮复核 L3-2 的反例里基线就答对的两句：问发票（PAY-005）、问今天能不能发（LOG-001），都不转投诉
    expected.update({"如果需要发票我就找平台开": _PAY, "要是今天能发，我就发朋友圈帮你们宣传": _LOG})
    for p13 in (False, True):
        for text in _all_sentences_t182():
            res = _one_t182(text, p13=p13)
            assert not evaluate._fabricates_status(res.reply_text), (text, p13)
            if (text, p13) in PREEXISTING_WRONG_ANSWERS_T182:
                # 基线就这么答（不是本轨引入的），逐字钉住它没有变成别的样子；记在 BACKLOG task-t182
                intent, scheme = PREEXISTING_WRONG_ANSWERS_T182[(text, p13)]
                assert (res.route, res.intent, res.draft.citations) == (
                    _ANS, intent, (_cite_t182(scheme),)), (text, p13)
                continue
            if res.route == _ANS:
                assert text in expected and res.intent == expected[text], (text, p13, res.intent)


def test_trigger_floor_is_never_narrowed_t182():
    """R1：p12 地板判出的原因，现在一个不少（开发集 + 本文件全部句子）；契约表的词照认。"""
    dev = [t for path in (evaluate.EVAL_PATH, evaluate.P13_EVAL_PATH)
           for c in evaluate.load_cases(path) for t in c.turns]
    for text in dev + _all_sentences_t182():
        assert set(triggers.matched_reasons(text)) >= set(triggers.p12_reasons(text)), text
    for reason, words in triggers.CONTRACT_WORDS.items():
        for word in words:
            got = triggers.detect(word)
            assert got is not None and triggers.PRIORITY.index(got[0]) <= triggers.PRIORITY.index(reason)


#: R1 地板的冻结快照（逐字抄自基线 45f724d 的 triggers.py，那一层逐字同 bf53df6）。上一条拿
#: ``matched_reasons`` 比 ``p12_reasons``，两边都从同一张树内表算：把表收窄，两边一起变小、照样绿
#: （复核 L2-3）。这里拿树外的字面量钉住表本身，再逐条实跑。
FLOOR_CONTRACT_WORDS_T182 = {
    T.HANDOFF_REQUESTED: ("转人工", "人工客服", "找人工", "真人"),
    T.HANDOFF_COMPLAINT: ("投诉", "12315", "消协", "曝光", "起诉", "律师"),
    T.HANDOFF_ANGER: ("垃圾", "骗子", "气死", "滚"),
    T.HANDOFF_COMPENSATION: ("赔偿", "补偿", "赔钱", "赔我"),
    T.HANDOFF_PRIVACY: ("手机号", "身份证", "住址", "个人信息", "隐私"),
}
FLOOR_EXTRA_PATTERNS_T182 = {
    T.HANDOFF_REQUESTED: (
        "人工", "活人", "客服小姐姐", "客服小哥", "客服妹妹",
        r"(?:不要|不想|不跟|不和|别让|别用)(?:跟|和|同)?(?:你们?的?)?机器人",
        r"(?:叫|找|换)(?:你们的?|个|一个)?(?:经理|主管|负责人|领导|老板)",
    ),
    T.HANDOFF_COMPLAINT: ("消费者协会", "消保委", "工商局", "市场监管", "法院", "告你们", "举报", "维权",
                          "黑猫投诉"),
    T.HANDOFF_ANGER: ("骗人", "骗钱", "坑人", "坑爹", "黑店", "气炸", "恶心", "混蛋", "王八蛋", "无耻",
                      "他妈", "妈的", "傻逼", "去死", "什么破", "破店"),
    T.HANDOFF_COMPENSATION: (r"赔(?!本)", "损失费", "精神损失"),
    T.HANDOFF_PRIVACY: ("个人资料", "身份信息", "身份证号", "手机号码", "电话号码", "银行卡号", "泄露", "泄漏"),
}
FLOOR_GUN_RE_T182 = r"(?<![翻打])滚(?![筒动轮珠烫雪梯刀])"
FLOOR_HOTLINE_RE_T182 = r"(?<!\d)12315(?!\d)"
#: 快照里三条正则各配的实跑句。
FLOOR_REGEX_SAMPLES_T182 = {
    r"(?:不要|不想|不跟|不和|别让|别用)(?:跟|和|同)?(?:你们?的?)?机器人": ("我不想跟你们的机器人说",
                                                                        T.HANDOFF_REQUESTED),
    r"(?:叫|找|换)(?:你们的?|个|一个)?(?:经理|主管|负责人|领导|老板)": ("叫你们的主管来", T.HANDOFF_REQUESTED),
    r"赔(?!本)": ("这得赔点钱吧", T.HANDOFF_COMPENSATION),
}


def _at_least_as_urgent_t182(text: str, reason: str) -> bool:
    got = triggers.detect(text)
    return got is not None and triggers.PRIORITY.index(got[0]) <= triggers.PRIORITY.index(reason)


def test_trigger_floor_matches_the_frozen_snapshot_t182():
    """R1 地板对着树外快照：表一个字不许动；每条字面词、每条正则的实跑句仍判出同级或更高优先级。"""
    assert dict(triggers.CONTRACT_WORDS) == FLOOR_CONTRACT_WORDS_T182
    assert dict(triggers._EXTRA_PATTERNS) == FLOOR_EXTRA_PATTERNS_T182
    assert (triggers._GUN_RE, triggers._HOTLINE_RE) == (FLOOR_GUN_RE_T182, FLOOR_HOTLINE_RE_T182)
    assert (triggers.EXCLAMATIONS, triggers.EXCLAMATION_RUN) == ("!❗❕", 3)
    special = {"滚": FLOOR_GUN_RE_T182, "12315": FLOOR_HOTLINE_RE_T182}
    for reason in triggers.PRIORITY:
        parts = [special.get(w) or re.escape(w) for w in FLOOR_CONTRACT_WORDS_T182[reason]]
        parts += list(FLOOR_EXTRA_PATTERNS_T182[reason])
        assert triggers._P12_PATTERNS[reason].pattern == "|".join(f"(?:{p})" for p in parts), reason
        for entry in FLOOR_CONTRACT_WORDS_T182[reason] + FLOOR_EXTRA_PATTERNS_T182[reason]:
            text, want = FLOOR_REGEX_SAMPLES_T182.get(entry, (entry, reason))
            assert _at_least_as_urgent_t182(text, want), (reason, entry, triggers.detect(text))
            assert _at_least_as_urgent_t182(f"你们{text}啊", want), (reason, entry)
    assert _at_least_as_urgent_t182("!!!", T.HANDOFF_ANGER)


def test_dev_sets_are_unchanged_t182():
    """p12 开发集：只差 T169 登记的那一轮（CS12-044#1）；p13 开发集：零失误。编造 0。"""
    r12 = evaluate.run_eval(lambda: _desk_t182(), evaluate.load_cases())
    assert {(m.case_id, m.turn) for m in r12.failures} == {("CS12-044", 1)}, r12.describe()
    assert r12.status_fabrication == 0
    r13 = evaluate.run_eval_p13(lambda ports: _desk_t182(ports), evaluate.load_cases(evaluate.P13_EVAL_PATH))
    assert r13.failures == (), r13.describe()
    assert r13.status_fabrication == 0 and r13.wrong_status == 0


def test_fallback_streak_rule_is_untouched_t182():
    """不改连续兜底规则：门槛仍是 2，两轮判不准就 repeated_fallback；修好的类别不再连带。"""
    assert T.FALLBACK_STREAK_HANDOFF == 2
    got = _run_t182(["帮我写一首关于秋天的诗", "给我讲个笑话"], p13=False)
    assert [(r.route, r.handoff_reason) for r in got] == [(_FB, ""), (_HO, T.HANDOFF_REPEATED_FALLBACK)]
    got = _run_t182(["亲在不在呀", "吊牌没摘可以退货不", "打扰一下哈", "退货第一步干嘛"], p13=False)
    assert [r.route for r in got] == [_ANS, _ANS, _ANS, _ANS], [(r.route, r.handoff_reason) for r in got]


def test_synonym_norm_only_rescues_turns_below_the_threshold_t182():
    """归一只在原句检不到时启用：原句本来就检得到的（开发集每一轮 answer / handoff 的检索），
    KbRetrieved 里没有 synonym_norm；启用时只记规则名，不记客户原文与归一后的句子。"""
    store = SqliteStore(":memory:")
    store.init_schema()
    seed_cs_kb(store)
    hits = scripts.match_scripts(store, tenant_id=TENANT_T182, text="亲在不在呀", plan_id="cs:csc-t182",
                                 task_id="csc-t182-t0001")
    assert hits and hits[0].scheme_no == "GEN-001" and hits[0].score >= scripts.MIN_SCRIPT_SCORE
    scripts.match_scripts(store, tenant_id=TENANT_T182, text="包邮吗亲", plan_id="cs:csc-t182",
                          task_id="csc-t182-t0002")
    rows = kb.query(store, "SELECT task_id, detail FROM event_log WHERE event_type='KbRetrieved' "
                           "ORDER BY rowid")
    first, second = (json.loads(r["detail"]) for r in rows)
    assert first["query"]["synonym_norm"] == ["greeting"]
    assert "亲在不在" not in rows[0]["detail"] and "你好" not in json.dumps(first["query"], ensure_ascii=False)
    assert "synonym_norm" not in second["query"]


def test_synonym_rule_names_and_canonicals_are_registered_phrases_t182():
    """每条规则的规范说法都是话术库登记过的同义词（归一到的是库里的话，不是另编一套）。"""
    registered = set()
    for row in kb_rows_t182():
        body = json.loads(row["body"])
        registered.update(body["synonyms"])
    names = [r[0] for r in scripts.SYNONYM_RULES]
    assert len(set(names)) == len(names) and "greeting" not in names
    assert scripts.ORDER_ANOMALY_RULES <= set(names)
    for _name, _pat, canonical, _need, _avoid in scripts.SYNONYM_RULES:
        assert canonical in registered, canonical
    assert scripts.GREETING_CANONICAL in registered


#: 归一表里与开发集句子重合的 ≥ 4 字说法：只许是契约 §2 T182 类别 1 点名的那一个。
CONTRACT_NAMED_OVERLAPS_T182 = frozenset({"打扰一下"})


def test_normalization_lexicon_does_not_copy_dev_sentences_t182():
    """近重复棘轮（同 T169 片段棘轮的口径，≥ 4 字）：归一表里自写的问候词与称呼，规整后 ≥ 4 字的
    不许是开发集任何一句的一段（契约点名的除外）。规范说法另由上一条钉成话术库已登记的同义词。"""
    dev = ["".join(kb.tokenize(t)) for path in (evaluate.EVAL_PATH, evaluate.P13_EVAL_PATH)
           for c in evaluate.load_cases(path) for t in c.turns]
    overlaps = set()
    for word in list(scripts.GREETING_WORDS) + list(scripts.GREETING_ADDRESS):
        norm = "".join(kb.tokenize(word))
        if len(norm) >= 4 and any(norm in t for t in dev):
            overlaps.add(word)
    assert overlaps <= CONTRACT_NAMED_OVERLAPS_T182, sorted(overlaps - CONTRACT_NAMED_OVERLAPS_T182)


# ---------------------------------------------------------------------------
# 6. lang 的公开函数（BACKLOG integrate-p13）
# ---------------------------------------------------------------------------
def test_desk_uses_the_public_lang_function_t182():
    src = inspect.getsource(D.has_lang_signal)
    assert "_drop_codes" not in src and "drop_codes" in src
    assert lang.drop_codes is lang._drop_codes
    for text in ("A1001", "SO-2026-000123", "2026092400123", "A1001 where", "到哪", "", "!!", "Hi",
                 "iPhone15", "订单 A1001"):
        assert D.has_lang_signal(text) == lang.has_lang_signal(text), text
    assert lang.drop_codes("order A1001 now") == "order   now"


# ---------------------------------------------------------------------------
# 整合期 p14（主会话）：二轮复核余项里两处「自信答错」的收口
# ---------------------------------------------------------------------------
import pytest as _pytest_integ  # noqa: E402

from maos.domain.cs import scripts as _scripts_integ  # noqa: E402


def _rules_hit_integ(text):
    return set(_scripts_integ.synonym_hits(text))


@_pytest_integ.mark.parametrize("text", [
    "没用过的退烧贴能退烧吗", "没拆过的退热贴好用吗", "没用过的会员能退订吗", "吊牌还在的衣服穿着会退色吗能退烧吗",
])
def test_seven_day_does_not_fire_on_fever_or_unsubscribe_integ(text):
    assert "seven_day" not in _rules_hit_integ(text)


@_pytest_integ.mark.parametrize("text", ["给我退热贴怎么弄", "退火炉怎么操作"])
def test_return_steps_does_not_fire_on_fever_or_annealing_integ(text):
    assert "return_steps" not in _rules_hit_integ(text)


@_pytest_integ.mark.parametrize("text", ["遥控器坏了换了电池还是不亮", "灯不亮了换了灯泡也没用"])
def test_quality_exchange_needs_the_item_itself_not_a_replaced_part_integ(text):
    assert "quality_exchange" not in _rules_hit_integ(text)
