"""T188 · 例句扩充与近邻阈值重扫（review/p16-cs-contracts.md §1、§2「T188」）。

话术库按 p16 的九个通用缺口类别（``GAP_CLASSES_P16``，maos/tests/test_cs_eval_p12_t169.py）并进了
``scenarios/cs/kb/additions_p16.json`` 里声明的增补（``scripts/gen_cs_kb.py`` 读它）。本文件量的是
**另写的**说法 —— 不在增补里、不是开发集句子、不是话术库登记过的说法，按同样九类由本轨自写（没有打开
任何评测集；只凭类别名与话术标题）—— 在增补前后的表现，以及不该命中的说法的弃权。

* 「增补前」= 话术库去掉 additions_p16.json 里声明的每一条（monkeypatch ``corpus.load_corpus``），
  其余代码与阈值一字不动；「增补后」= 仓库里的话术库。两边都跑整条 p12 路径前台（不注入端口：
  触发词 → 词法 → 同义归一 → 近邻），一句一个新库。
* 「答对」= route + intent + 转人工原因 + 引用的那一篇都对（篇级，p16 的「零自信答错」口径）。
* 近邻阈值 / 差距重扫（开发集 + 自写句，扫描表见 docs/DECISIONS.md task-t188）：0.25–0.34 × 差距 ≥ 0.10
  一段读数完全相同，差距 0.05 时自写句里多出一句经近邻引错篇（:data:`MARGIN_PROBE_T188`）。取值不变
  （0.34 / 0.10），本文件钉「重扫之后仍是这两个数」与那句探针的反向。

实测数字写在各常量旁；地板只许抬。
"""

from __future__ import annotations

import json
import re

import pytest

from maos.core.store import SqliteStore
from maos.domain.cs import corpus, evaluate, scripts, similar
from maos.domain.cs import types as T
from maos.domain.cs.corpus import seed_cs_kb
from maos.domain.cs.desk import CsConfig, FrontDesk
from maos.domain.cs.types import CHANNEL_WECHAT_KF
from maos.ingress.contracts import InboundMessage
from maos.tests.test_cs_eval_p12_t169 import GAP_CLASSES_P16

TENANT_T188 = "tnt-demo"
KFID_T188 = "wk_eval"
ADDITIONS_PATH_T188 = corpus.CORPUS_PATH.with_name("additions_p16.json")

# ---------------------------------------------------------------------------
# 自写说法（按 GAP_CLASSES_P16 的九类；不带具体地名 / 商品 / 日期 / 数字 / 单号）
# ---------------------------------------------------------------------------
#: 类别 → [(句子, 该答 / 该转的方案编号)]。
CLASS_SENTENCES_T188: dict[str, tuple[tuple[str, str], ...]] = {
    "寒暄与致谢": (
        ("哈喽，有人在吗在吗", "GEN-001"), ("hi，在不在", "GEN-001"), ("你好你好", "GEN-001"),
        ("您好，请问现在有人吗", "GEN-001"), ("嘿，在线吗", "GEN-001"), ("在嘛在嘛", "GEN-001"),
        ("好的谢谢，没啥事了", "GEN-002"), ("明白啦，感恩", "GEN-002"), ("多谢解答，拜拜", "GEN-002"),
        ("了解了，谢谢哈", "GEN-002"), ("ok谢谢你呀", "GEN-002"), ("感谢感谢，辛苦啦", "GEN-002"),
    ),
    "客服时间": (
        ("你们客服几点开工", "GEN-003"), ("客服晚上几点就不回了", "GEN-003"),
        ("周六周日客服上不上班", "GEN-003"), ("过年期间有没有客服", "GEN-003"),
        ("客服的在线时间是多久到多久", "GEN-003"), ("凌晨还能找到客服吗", "GEN-003"),
        ("你们客服每天什么时候上线", "GEN-003"), ("中午客服休息吗", "GEN-003"),
        ("客服啥时间段有人", "GEN-003"), ("放长假客服还回消息吗", "GEN-003"),
        ("客服下班了吗现在", "GEN-003"),
    ),
    "发货时效与快递": (
        ("买了以后大概几天能发出", "LOG-001"), ("你们一般多久安排发货呀", "LOG-001"),
        ("发货快吗", "LOG-001"), ("下完单多长时间能寄", "LOG-001"), ("现货一般当天能发不", "LOG-001"),
        ("付款后几天出货呀", "LOG-001"),
        ("用的是什么物流公司", "LOG-002"), ("能不能发别的快递", "LOG-002"), ("农村地区送吗", "LOG-002"),
        ("寄的哪个快递啊", "LOG-002"), ("边远的地方能到吗", "LOG-002"),
        ("可以换成我指定的快递吗", "LOG-002"),
    ),
    "运费与包邮": (
        ("邮费怎么算的", "LOG-003"), ("这个包邮不包邮", "LOG-003"), ("运费要另外付吗", "LOG-003"),
        ("买多少钱可以免邮", "LOG-003"), ("下单要付快递费吗", "LOG-003"), ("运费可以减免吗", "LOG-003"),
        ("退货的邮费谁来付", "RET-004"), ("退回来的快递费谁负责", "RET-004"),
        ("退货运费需要我承担吗", "RET-004"), ("有运费险的话退货免费吗", "RET-004"),
        ("换货寄回去的运费谁出", "RET-004"),
    ),
    "订单异常要查单": (
        ("我的包裹咋还没到", "LOG-004"), ("物流一直停着没更新", "LOG-004"),
        ("帮我查一下快递到哪儿了", "LOG-004"), ("地址填错了想改一下", "LOG-005"),
        ("能不能把收货人改一下", "LOG-005"), ("我想改一下送货时间", "LOG-005"),
        ("快递显示签收了但我没拿到", "LOG-006"), ("包裹里的东西摔坏了", "LOG-006"),
        ("快递丢了咋办", "LOG-006"), ("退款到现在还没到账", "PAY-003"), ("我申请的退款钱呢", "PAY-003"),
        ("退的钱迟迟没收到", "PAY-003"), ("被扣了双份钱", "PAY-004"), ("支付一直失败怎么回事", "PAY-004"),
    ),
    "退款规则与支付": (
        ("退款退到哪里呀", "PAY-001"), ("退款是原路退回吗", "PAY-001"), ("退款多久能到账", "PAY-001"),
        ("申请退款后钱几天能回来", "PAY-001"), ("退款会退到微信零钱吗", "PAY-001"),
        ("退款到账要多长时间", "PAY-001"),
        ("能用微信付款吗", "PAY-002"), ("支持信用卡吗", "PAY-002"), ("可以货到付款不", "PAY-002"),
        ("有哪些付款方式", "PAY-002"), ("能不能用花呗", "PAY-002"),
    ),
    "开票": (
        ("可以开发票吗", "PAY-005"), ("发票怎么开啊", "PAY-005"), ("我需要开个增值税专票", "PAY-005"),
        ("电子发票去哪找", "PAY-005"), ("发票抬头能写公司吗", "PAY-005"), ("能补开发票不", "PAY-005"),
        ("开票需要提供什么信息", "PAY-005"), ("发票开错了能重开吗", "PAY-005"),
        ("发票是纸的还是电子的", "PAY-005"), ("要报销，发票怎么弄", "PAY-005"),
    ),
    "退换货政策": (
        ("七天之内可以无理由退吗", "RET-001"), ("拆封了还能退吗", "RET-001"), ("穿过一次能退不", "RET-001"),
        ("退货的条件是啥", "RET-001"),
        ("怎么申请退货呀", "RET-002"), ("退货流程麻烦吗", "RET-002"), ("退货去哪里申请", "RET-002"),
        ("退货寄到什么地址", "RET-002"),
        ("东西坏了能换新的吗", "RET-003"), ("有质量问题可以换货吗", "RET-003"),
        ("收到的东西有瑕疵想换一个", "RET-003"), ("质量有问题换货要拍照吗", "RET-003"),
    ),
    "办具体退货退款": (
        ("这单帮我退了", "RET-005"), ("给我退款", "RET-005"), ("我想把这个订单退掉", "RET-005"),
        ("帮我办一下退款吧", "RET-005"), ("申请退货，这个不想要了", "RET-005"),
        ("我要把买的东西退回去", "RET-005"), ("帮我退了这个订单", "RET-005"), ("不想要了，帮我退", "RET-005"),
        ("请帮我发起退款", "RET-005"), ("我要退掉刚买的", "RET-005"),
    ),
}

#: 不该命中的说法：商品咨询、闲聊、无关问题、否定句、第三人称、售前 / 使用。
NEGATIVES_T188: tuple[str, ...] = (
    # 商品咨询
    "这个有黑色的吗", "这款适合送人吗", "鞋子码数标准吗", "这个质量怎么样", "有没有买一送一",
    "能便宜点吗", "这个是正品吗", "保质期多长", "这个手机壳适配吗", "有没有更大的尺寸",
    "这款什么时候上新", "店里有活动吗", "有现货吗",
    # 闲聊
    "今天好无聊", "你吃饭了吗", "你几岁了", "你喜欢猫吗", "周末去哪玩好", "心情不好", "哈哈哈哈",
    # 无关问题
    "怎么减肥", "股票能买吗", "明天会下雨吗", "给我讲个故事", "数学题怎么做", "附近哪有银行",
    "怎么做红烧肉",
    # 否定句
    "不用退了，我自己留着", "我不需要发票", "不是要换货，我只是问问", "没有要改地址",
    "我不打算退款了",
    # 第三人称
    "我朋友的快递丢了", "听说你们发货很快", "我同事买了觉得不错", "他们家包邮吗", "我妈想问能不能退",
    # 售前 / 使用
    "这个怎么用", "说明书在哪", "怎么清洗", "能刻字吗", "有没有礼盒包装", "能开个会员吗",
)

#: 每类「答对」的实测数（增补前 → 增补后）。增补后的数是地板，只许抬；增补前是去掉声明增补的
#: 固定话术库上的读数（基线说法由 T169 的指纹钉住，所以它是确定的），逐字钉住。
RECALL_BEFORE_T188 = {"寒暄与致谢": 10, "客服时间": 9, "发货时效与快递": 7, "运费与包邮": 7,
                      "订单异常要查单": 11, "退款规则与支付": 11, "开票": 9, "退换货政策": 12,
                      "办具体退货退款": 8}
RECALL_AFTER_FLOOR_T188 = {"寒暄与致谢": 10, "客服时间": 11, "发货时效与快递": 7, "运费与包邮": 8,
                           "订单异常要查单": 12, "退款规则与支付": 11, "开票": 9, "退换货政策": 12,
                           "办具体退货退款": 8}
#: 合计：增补前 84 / 103、增补后 88 / 103；落兜底的 12 → 8。

#: 词法检索（不是近邻）在自写句上**增补前就有**的答错 / 转错篇（七句，增补前后逐句相同，近邻一次都没启用）：
#: 「多长时间」把发货问法拉到退款时长篇、「指定 / 别的快递」被查单篇的「快递」拉走、「快递费」被退货运费篇
#: 拉走、「申请 + 退」被流程篇 / 运费篇拉走、「退款钱呢」被办退款篇拉走。本轨不改词法（similar.py 只许动
#: 阈值），记 BACKLOG task-t188。只许这张封闭表，别的一律要对。
KNOWN_LEXICAL_WRONG_T188 = frozenset({
    "下完单多长时间能寄", "能不能发别的快递", "可以换成我指定的快递吗", "下单要付快递费吗",
    "我申请的退款钱呢", "申请退货，这个不想要了", "我要把买的东西退回去",
})
#: 不该命中的说法里**增补前就被词法答掉**的四句（否定句两句、第三人称一句、「开个会员」被开票篇拉走），
#: 增补前后相同，近邻一次都没启用。封闭表。
KNOWN_LEXICAL_NEGATIVE_ANSWERS_T188 = frozenset({
    "不用退了，我自己留着", "我不需要发票", "他们家包邮吗", "能开个会员吗",
})

#: 差距探针：本句在缺省门槛（0.34 / 0.10）下落兜底；差距放到 0.05（门槛 0.25）时经近邻被引到
#: LOG-006（该是 LOG-004）—— 路由与意图都对、篇错了，正是 p16 要收的篇级自信答错。
MARGIN_PROBE_T188 = ("我的包裹咋还没到", "LOG-004", "LOG-006")


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def _all_positive_t188() -> list[tuple[str, str, str]]:
    return [(cls, text, want) for cls, items in CLASS_SENTENCES_T188.items() for text, want in items]


def _additions_t188() -> list[dict]:
    return list(json.loads(ADDITIONS_PATH_T188.read_text(encoding="utf-8"))["additions"])


def _strip_additions_t188(monkeypatch) -> None:
    """「增补前」：灌库时去掉 additions_p16.json 里声明的每一条，别的一字不动。"""
    declared = {(e["scheme_no"], e["variant"]) for e in _additions_t188()}
    original = corpus.load_corpus

    def patched(path=corpus.CORPUS_PATH):
        rows = original(path)
        for row in rows:
            body = json.loads(row["body"])
            no = body["scheme_no"]
            body["synonyms"] = [v for v in body["synonyms"] if (no, v) not in declared]
            body["examples"] = [v for v in body["examples"] if (no, v) not in declared]
            row["body"] = json.dumps(body, ensure_ascii=False, sort_keys=True)
        return rows

    monkeypatch.setattr(corpus, "load_corpus", patched)


def _bodies_t188() -> dict[str, dict]:
    return {row["rule_no"]: json.loads(row["body"]) for row in corpus.load_corpus()}


def _scheme_of_t188(doc_id: str) -> str:
    return doc_id.removeprefix(f"kb-cs-{TENANT_T188}-")


def _one_t188(text: str) -> tuple[str, str, str, str, bool]:
    """p12 路径（不注入端口）跑一句：(route, intent, 原因, 引用编号, 是否经近邻)。一句一个新库。"""
    store = SqliteStore(":memory:")
    seed_cs_kb(store)
    desk = FrontDesk(store, CsConfig(tenants={KFID_T188: TENANT_T188}, handoff_target=None))
    res = desk.handle(InboundMessage(channel=CHANNEL_WECHAT_KF, chat_id="wm_t188", sender="wm_t188",
                                     text=text, msg_id="m-t188", raw={"open_kfid": KFID_T188}))
    rows = [r for r in store.list_event_log(T.plan_id_for(res.conversation_id))
            if r["event_type"] == "KbRetrieved"]
    fired = any((r["detail"].get("query") or {}).get("channel") == similar.CHANNEL for r in rows)
    cite = _scheme_of_t188(res.draft.citations[0]) if res.draft.citations else ""
    return res.route, res.intent, res.handoff_reason, cite, fired


def _expected_t188(scheme_no: str, bodies: dict[str, dict]) -> tuple[str, str, str]:
    body = bodies[scheme_no]
    return ((T.ROUTE_HANDOFF if body["handoff"] else T.ROUTE_ANSWER), body["intent"],
            body["handoff"] or "")


def _measure_t188() -> dict:
    """整批跑一遍：每类答对数、答错 / 转错篇的句子、经近邻的句子（与对错）、反例的判定。"""
    bodies = _bodies_t188()
    per_class = {cls: 0 for cls in CLASS_SENTENCES_T188}
    wrong, fallback, via_ok, via_bad = set(), set(), set(), set()
    for cls, text, want in _all_positive_t188():
        route, intent, reason, cite, fired = _one_t188(text)
        ok = (route, intent, reason) == _expected_t188(want, bodies) and cite == want
        if ok:
            per_class[cls] += 1
        elif route == T.ROUTE_FALLBACK:
            fallback.add(text)
        else:
            wrong.add(text)
        if fired:
            (via_ok if ok else via_bad).add(text)
    neg_answered, neg_fired = set(), set()
    for text in NEGATIVES_T188:
        route, _intent, _reason, _cite, fired = _one_t188(text)
        if route == T.ROUTE_ANSWER:
            neg_answered.add(text)
        if fired:
            neg_fired.add(text)
    return {"per_class": per_class, "wrong": wrong, "fallback": fallback, "via_ok": via_ok,
            "via_bad": via_bad, "neg_answered": neg_answered, "neg_fired": neg_fired}


@pytest.fixture(scope="module")
def after_t188() -> dict:
    return _measure_t188()


# ---------------------------------------------------------------------------
# 1. 说法是另写的
# ---------------------------------------------------------------------------
def test_sentences_are_self_written_and_classed_t188():
    """每类 ≥ 10 句、反例 ≥ 40 句；不是开发集句子、不是话术库登记过的说法（含增补，按检索侧「原样命中」
    的口径比）、不带数字；每句期望的编号属于它的类别。"""
    assert set(CLASS_SENTENCES_T188) == set(GAP_CLASSES_P16)
    for cls, items in CLASS_SENTENCES_T188.items():
        assert len(items) >= 10, cls
        assert {want for _t, want in items} <= GAP_CLASSES_P16[cls], cls
    assert len(NEGATIVES_T188) >= 40
    texts = [t for _c, t, _w in _all_positive_t188()] + list(NEGATIVES_T188)
    assert len(set(texts)) == len(texts)
    dev = {t for c in evaluate.load_cases() for t in c.turns}
    dev |= {t for c in evaluate.load_cases(evaluate.P13_EVAL_PATH) for t in c.turns}
    registered = set()
    for body in _bodies_t188().values():
        for v in [body["scene"], *body["synonyms"], *body["examples"]]:
            registered |= scripts.tail_forms(v)
    for text in texts:
        assert text not in dev, text
        assert not (scripts.tail_forms(text) & registered), text
        assert not re.search(r"[0-9０-９]", text), text


def test_additions_are_in_the_corpus_and_typos_are_registered_t188():
    """声明的每一条都按 kind 进了该篇的 synonyms / examples；登记的错写都真出现在该篇的增补例句里。"""
    bodies = _bodies_t188()
    field = {"synonym": "synonyms", "example": "examples"}
    entries = _additions_t188()
    assert entries
    for e in entries:
        assert e["variant"] in bodies[e["scheme_no"]][field[e["kind"]]], e
    typos = json.loads(ADDITIONS_PATH_T188.read_text(encoding="utf-8"))["typos"]
    for no, pairs in typos.items():
        mine = [e["variant"] for e in entries if e["scheme_no"] == no and e["kind"] == "example"]
        for wrong, right in pairs:
            assert wrong != right and any(wrong in v for v in mine), (no, wrong)
    # 增补说法本身也不带数字（不凑具体日期 / 金额 / 单号）
    assert not [e["variant"] for e in entries if re.search(r"[0-9０-９]", e["variant"])]


# ---------------------------------------------------------------------------
# 2. 召回：每类、增补前后
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("cls", sorted(CLASS_SENTENCES_T188))
def test_per_class_recall_after_additions_t188(after_t188, cls):
    got = after_t188["per_class"][cls]
    print(f"{cls}: 答对 {got}/{len(CLASS_SENTENCES_T188[cls])}（增补前 {RECALL_BEFORE_T188[cls]}）")
    assert got >= RECALL_AFTER_FLOOR_T188[cls], (cls, got)


def test_before_and_after_additions_t188(monkeypatch, after_t188):
    """增补前后对照：每类答对数前者逐字等于 RECALL_BEFORE_T188；后者不低于前者、合计至少多 4 句；
    答错 / 转错篇的句子前后是同一张封闭表（增补没有引入任何一句新的答错）。"""
    _strip_additions_t188(monkeypatch)
    before = _measure_t188()
    assert before["per_class"] == RECALL_BEFORE_T188
    for cls in CLASS_SENTENCES_T188:
        assert after_t188["per_class"][cls] >= before["per_class"][cls], cls
    total_before = sum(before["per_class"].values())
    total_after = sum(after_t188["per_class"].values())
    print(f"合计答对 {total_before} → {total_after} / {len(_all_positive_t188())}；"
          f"兜底 {len(before['fallback'])} → {len(after_t188['fallback'])}")
    assert (total_before, len(before["fallback"])) == (84, 12)
    assert total_after >= total_before + 4
    assert before["wrong"] == KNOWN_LEXICAL_WRONG_T188
    assert before["neg_answered"] == KNOWN_LEXICAL_NEGATIVE_ANSWERS_T188


# ---------------------------------------------------------------------------
# 3. 零自信答错（篇级）与反例弃权
# ---------------------------------------------------------------------------
def test_no_new_confident_wrong_and_similar_hits_are_right_t188(after_t188):
    """答错 / 转错篇的只有增补前就有的那七句（词法，近邻没启用）；经近邻的每一句都答对篇。"""
    assert after_t188["wrong"] == KNOWN_LEXICAL_WRONG_T188, sorted(after_t188["wrong"])
    assert after_t188["via_bad"] == set(), sorted(after_t188["via_bad"])


def test_negatives_abstain_t188(after_t188):
    """不该命中的 43 句：近邻通道一次都不启用（弃权率 43/43 = 100%）；整条前台上答了的只有增补前就被
    词法答掉的那四句（39/43 不答）。"""
    assert len(NEGATIVES_T188) == 43
    print(f"近邻弃权 {len(NEGATIVES_T188) - len(after_t188['neg_fired'])}/{len(NEGATIVES_T188)}；"
          f"前台不答 {len(NEGATIVES_T188) - len(after_t188['neg_answered'])}/{len(NEGATIVES_T188)}")
    assert after_t188["neg_fired"] == set(), sorted(after_t188["neg_fired"])
    assert after_t188["neg_answered"] == KNOWN_LEXICAL_NEGATIVE_ANSWERS_T188, sorted(
        after_t188["neg_answered"])


# ---------------------------------------------------------------------------
# 4. 阈值 / 差距重扫
# ---------------------------------------------------------------------------
def test_thresholds_after_rescan_t188():
    """重扫后取值不变（DECISIONS task-t188 的扫描表）：门槛不低于词法门槛（近邻那篇以余弦进 ScriptHit）。"""
    assert (similar.MIN_SIMILARITY, similar.MIN_MARGIN) == (0.34, 0.10)
    assert similar.MIN_SIMILARITY >= scripts.MIN_SCRIPT_SCORE


def test_margin_probe_t188(monkeypatch):
    """反向：差距放到 0.05（门槛 0.25）时，探针句经近邻被引到错篇；缺省门槛下它落兜底。"""
    text, want, wrong_doc = MARGIN_PROBE_T188
    route, _intent, _reason, cite, fired = _one_t188(text)
    assert (route, cite, fired) == (T.ROUTE_FALLBACK, "", False)
    real = similar.nearest
    monkeypatch.setattr(similar, "nearest",
                        lambda index, t, **kw: real(index, t, min_similarity=0.25, min_margin=0.05))
    route, _intent, _reason, cite, fired = _one_t188(text)
    assert fired and cite == wrong_doc != want
