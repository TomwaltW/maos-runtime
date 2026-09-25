"""T186 · 检索召回的确定性近邻兜底（review/p15-cs-contracts.md §1 C9 / C10、§2「T186」）。

``maos/domain/cs/similar.py``：字符 1–3 元组 TF-IDF 余弦、按例句取最近邻；词法（二元组重排 + 同义归一）
零命中时由 ``scripts.match_scripts(..., nearest=True)`` 启用（``cs.answer`` 这么调），分不够 / 与第二名
差距不够 / 否定句 / 第三人称 / 没有汉字 / 太短 → 弃权（照旧兜底）。

本文件的例句全部是本轨按类别自写的（没有打开任何留出集与它的测试，也没有据全量里留出集测试的
失败 id 反推）：C9 政策问答的口语说法、C10 具体订单异常的口语说法各 ≥ 20 句，不该命中的说法
（商品咨询、闲聊、无关问题、否定句、第三人称）≥ 30 句。实测数字写在各常量旁，测试按「只许抬」钉地板。

不变量：p12 / p13 开发集上近邻一次都不启用（开发集照旧全绿，由 T169 / T174 的测试钉逐轮）；
近邻命中的每一轮都对（零「自信答错」）；缺省 ``nearest=False`` 时 match_scripts 逐字节同 p14；
近邻那篇照常组稿、带 ``kb:<doc_id>`` 引用、过后置校验，``KbRetrieved.detail.query.channel == "similar"``、
不含客户原文；转人工标记的篇照旧转人工。
"""

from __future__ import annotations

import ast
import json
import pathlib
import re
import sys

import pytest

from maos.core.store import SqliteStore
from maos.domain.cs import claims, corpus, evaluate, scripts, similar
from maos.domain.cs import types as T
from maos.domain.cs.corpus import seed_cs_kb
from maos.domain.cs.desk import CsConfig, FrontDesk, retrieval_query
from maos.domain.cs.types import CHANNEL_WECHAT_KF
from maos.ingress.contracts import InboundMessage

TENANT_T186 = "tnt-demo"
KFID_T186 = "wk_eval"
_ANS, _HO, _FB = T.ROUTE_ANSWER, T.ROUTE_HANDOFF, T.ROUTE_FALLBACK

# ---------------------------------------------------------------------------
# 自写例句（按类别；不带具体地名 / 商品名 / 日期 / 数字）
# ---------------------------------------------------------------------------
#: C9：政策问答的口语说法 → (句子, 该答的方案编号)。
C9_T186: tuple[tuple[str, str], ...] = (
    ("衣服试穿了一下不太合适，没洗过能退吗", "RET-001"),
    ("商品没开封的话可以无条件退回吗", "RET-001"),
    ("买回来不喜欢了，还在规定时间内，能退不", "RET-001"),
    ("东西完好无损就是不想要了，可以退掉吗", "RET-001"),
    ("要退的话我这边需要做些什么", "RET-002"),
    ("退东西是先寄回去还是先在网上申请", "RET-002"),
    ("售后退货在页面上哪里点", "RET-002"),
    ("退回去的邮费是买家承担吗", "RET-004"),
    ("寄回的快递钱最后谁来出", "RET-004"),
    ("退的钱会回到当初付款的那个账户吗", "PAY-001"),
    ("退款是退到余额还是退回银行卡", "PAY-001"),
    ("用信用卡付的，退款一般多长时间能回来", "PAY-001"),
    ("付完款大概什么时候能寄出来", "LOG-001"),
    ("拍下以后要等多久才会出库", "LOG-001"),
    ("你们默认走哪家物流", "LOG-002"),
    ("偏僻一点的地方也能配送过去吗", "LOG-002"),
    ("邮费是怎么算的，有没有免邮的门槛", "LOG-003"),
    ("哈喽哈喽，有人在线吗", "GEN-001"),
    ("亲，在不在呀", "GEN-001"),
    ("好嘞，多谢啦，没别的事了", "GEN-002"),
    ("行，知道了，辛苦你啦", "GEN-002"),
    ("衣服洗了一次就褪色了，可以换一件吗", "RET-003"),
    ("付款能不能用云闪付之类的", "PAY-002"),
    ("退货的时候快递费要自己垫吗", "RET-004"),
    ("没拆吊牌的衣服还能退吗", "RET-001"),
    ("钱退回原来的支付方式大概要几天", "PAY-001"),
    ("一般下单后隔多久发出", "LOG-001"),
    ("怎么申请退货退款，步骤是什么", "RET-002"),
    ("东西原封不动的，我能不能退回去", "RET-001"),
    ("外包装还是好的，不想要了能退吗", "RET-001"),
    ("退掉的话要走哪些手续", "RET-002"),
    ("我想把东西寄回去，具体该怎么弄", "RET-002"),
    ("寄回去要花的钱是不是得我自己出", "RET-004"),
    ("退回来的钱最后进到哪个账户", "PAY-001"),
    ("退款会原路返回到我付款的地方吗", "PAY-001"),
    ("一般拍完隔几天安排出货", "LOG-001"),
    ("你们合作的是哪家快递公司呀", "LOG-002"),
    ("特别远的地方送不送", "LOG-002"),
    ("要凑多少钱才能免运费", "LOG-003"),
    ("晚上几点以后就没人回复了", "GEN-003"),
    ("好的好的，麻烦你了", "GEN-002"),
    ("收到的东西有毛病，可以换个好的吗", "RET-003"),
    ("能不能货到了再给钱", "PAY-002"),
)

#: C10：具体订单异常的口语说法 → (句子, p12 路径该转的查单篇)。
C10_T186: tuple[tuple[str, str], ...] = (
    ("我的包裹好几天都停在同一个地方没动", "LOG-004"),
    ("物流信息一直没有变化，是不是出问题了", "LOG-004"),
    ("快递在路上走了好久还没送到", "LOG-004"),
    ("包裹在中转站待了很久都不走", "LOG-004"),
    ("下单的时候地址选错了，还来得及改吗", "LOG-005"),
    ("收件地址想换成另一个，可以帮忙改一下吗", "LOG-005"),
    ("收货人名字写错了，能修改吗", "LOG-005"),
    ("显示已经送达了，可是我家里根本没有", "LOG-006"),
    ("快递说放门口了，我回家没找到", "LOG-006"),
    ("收到的时候外箱破破烂烂的，里面东西也碎了", "LOG-006"),
    ("包裹寄丢了找不回来怎么办", "LOG-006"),
    ("别人帮我签收了但东西不在我这", "LOG-006"),
    ("商家已经同意退款了，钱却一直没回到账户", "PAY-003"),
    ("说好给我退钱的，到现在都没收到", "PAY-003"),
    ("退款显示成功了但是账户里没看到", "PAY-003"),
    ("同一笔订单扣了我两遍款", "PAY-004"),
    ("付了钱订单还是显示待付款", "PAY-004"),
    ("支付的时候一直提示失败，钱却扣掉了", "PAY-004"),
    ("付一次钱怎么被划走了两笔", "PAY-004"),
    ("这一单我不想要了，帮我办一下退款", "RET-005"),
    ("麻烦帮我把刚买的那个退了", "RET-005"),
    ("我要申请售后退货，东西不想要了", "RET-005"),
    ("快递签收的不是我本人，我也没拿到", "LOG-006"),
    ("物流好久都不更新了，帮我看看", "LOG-004"),
    ("东西一直在路上，好几天没有新的消息", "LOG-004"),
    ("物流显示的位置好久没变过了", "LOG-004"),
    ("我填的收件地址不对，想改成新家的", "LOG-005"),
    ("能不能换个地方收货", "LOG-005"),
    ("东西明明没到我手里，却显示有人收了", "LOG-006"),
    ("打开快递一看东西都碎成渣了", "LOG-006"),
    ("包裹不知道被谁拿走了", "LOG-006"),
    ("你们说退钱了，可我一分都没见到", "PAY-003"),
    ("退款通过好多天了，钱还是没回来", "PAY-003"),
    ("我的钱被扣了两回", "PAY-004"),
    ("付款失败了但银行卡的钱少了", "PAY-004"),
    ("这个订单我不要了，帮我退掉", "RET-005"),
)

#: 不该命中的说法：商品咨询、闲聊、无关问题、否定句、第三人称。
NEGATIVES_T186: tuple[str, ...] = (
    # 商品咨询
    "这件外套有没有大一码的", "这个颜色会不会显黑", "面料是纯棉的吗", "有没有优惠券可以领",
    "现在买有赠品吗", "这个锅能用在电磁炉上吗", "尺码偏大还是偏小", "这款和那款有什么区别",
    # 闲聊
    "今天天气真好", "你是机器人吗", "你叫什么名字", "中午吃什么好呢", "最近好累啊",
    "讲个笑话听听", "你会唱歌吗",
    # 无关问题
    "怎么学好英语", "推荐一部好看的电影", "附近有什么好吃的", "明天要不要带伞", "帮我写一篇作文",
    # 否定句
    "我不是要退货，只是想问问怎么保养", "不用改地址了，地址是对的", "我没有要退款，东西挺好的",
    "不用查物流，已经收到了，挺满意", "我不打算换货，就是说一声",
    # 第三人称
    "我朋友说他在你们家买的东西质量不错", "我同事也想买一个，能推荐吗", "听说别人家的快递经常丢",
    "我妈妈问这个适合老人用吗", "邻居说你们店的东西挺实惠",
    "他们公司的物流系统是怎么设计的", "我姐姐说她上次买的衣服很好看",
)

#: 实测（p12 路径 = 不注入端口，整条前台：触发词 → 词法 → 同义归一 → 近邻）：C9 31 / 43、C10 24 / 36
#: 答对（route + intent + 引用的那篇都对）。地板只许抬。
C9_RECALL_FLOOR_T186 = 31
C10_RECALL_FLOOR_T186 = 24
#: 其中经近邻通道答对的（其余是词法 / 同义归一答对的）。近邻命中的每一句都必须对。
SIMILAR_HITS_FLOOR_T186 = 1

#: 词法检索（不是近邻）在自写句上已有的「自信答错」：这一句的 RET-001 与 PAY-001 共享「退回」，
#: 二元组重排取了 PAY-001。与近邻通道无关（近邻在这一句上没启用），记 BACKLOG task-t186，本轨不改词法。
KNOWN_LEXICAL_CONFIDENT_WRONG_T186 = frozenset({"东西原封不动的，我能不能退回去"})


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def _bodies_t186() -> dict[str, dict]:
    return {row["rule_no"]: json.loads(row["body"]) for row in corpus.load_corpus()}


def _doc_id_t186(scheme_no: str) -> str:
    return evaluate.cite_doc_id(TENANT_T186, scheme_no)


def _seeded_store_t186():
    store = SqliteStore()
    store.init_schema()
    seed_cs_kb(store)
    return store


def _desk_t186(store=None) -> FrontDesk:
    store = store if store is not None else SqliteStore(":memory:")
    seed_cs_kb(store)
    return FrontDesk(store, CsConfig(tenants={KFID_T186: TENANT_T186}, handoff_target=None))


def _msg_t186(text: str, *, user: str = "wm_t186") -> InboundMessage:
    return InboundMessage(channel=CHANNEL_WECHAT_KF, chat_id=user, sender=user, text=text,
                          msg_id=f"m-{user}", raw={"open_kfid": KFID_T186})


def _kb_rows_t186(store, conversation_id: str) -> list[dict]:
    return [r for r in store.list_event_log(T.plan_id_for(conversation_id))
            if r["event_type"] == "KbRetrieved"]


def _similar_fired_t186(rows) -> bool:
    return any((r["detail"].get("query") or {}).get("channel") == similar.CHANNEL for r in rows)


def _one_t186(text: str):
    """p12 路径（不注入端口）跑一句：(DeskResult, 本轮是否经近邻通道, store)。"""
    store = SqliteStore(":memory:")
    res = _desk_t186(store).handle(_msg_t186(text))
    return res, _similar_fired_t186(_kb_rows_t186(store, res.conversation_id)), store


def _expected_t186(scheme_no: str) -> tuple[str, str, str]:
    body = _bodies_t186()[scheme_no]
    return (_HO if body["handoff"] else _ANS), body["intent"], body["handoff"] or ""


def _toy_index_t186(**kw) -> similar.SimilarIndex:
    return similar.SimilarIndex([
        ("A", ["快递到哪里了呀", "我的包裹怎么还没到"]),
        ("B", ["能开发票吗", "发票怎么申请"]),
        ("C", ["发票抬头写错了", "发票能重开吗"]),
    ], **kw)


def _measure_t186(sentences):
    """[(句子, 期望编号, 实得 route, intent, reason, 引用编号, 是否经近邻)]。"""
    out = []
    for text, want in sentences:
        res, fired, _ = _one_t186(text)
        cite = next((s for s in _bodies_t186() if res.draft.citations == (_doc_id_t186(s),)), "")
        out.append((text, want, res.route, res.intent, res.handoff_reason, cite, fired))
    return out


# ---------------------------------------------------------------------------
# 1. 纯函数：n 元组、索引、弃权规则、门槛
# ---------------------------------------------------------------------------
def test_ngrams_are_characters_one_to_three_t186():
    assert similar.ngrams("退货！Ab") == frozenset(
        {"退", "货", "a", "b", "退货", "货a", "ab", "退货a", "货ab"})
    assert similar.normalize("  退 货～！ＡＢ ") == "退货ab"
    assert similar.ngrams("！？") == frozenset()


def test_index_is_deterministic_and_order_independent_t186():
    entries = [("A", ["快递到哪里了呀", "我的包裹怎么还没到"]), ("B", ["能开发票吗", "发票怎么申请"])]
    one = similar.SimilarIndex(entries).rank("包裹到哪里了")
    two = similar.SimilarIndex(list(reversed([(k, list(reversed(v))) for k, v in entries]))).rank(
        "包裹到哪里了")
    assert one == two and one[0][0] == "A"
    assert all(0.0 < s <= 1.0 for _, s in one)


def test_exact_example_scores_one_and_unseen_text_scores_nothing_t186():
    index = _toy_index_t186()
    assert index.rank("能开发票吗")[0] == ("B", 1.0)
    assert index.rank("今天星期几") == []


def test_unseen_grams_lower_the_score_t186():
    """没见过的 n 元组照样进原文的向量长度：同一段对得上的话，后面多挂一截谁也不认识的，分更低。"""
    index = _toy_index_t186()
    short = dict(index.rank("发票怎么申请"))["B"]
    padded = dict(index.rank("发票怎么申请顺便问问明天吃什么"))["B"]
    assert padded < short


def test_light_chars_are_down_weighted_t186():
    """虚字 n 元组减重：「要不要」这种全由虚字组成的片段不再撑起一整句。"""
    entries = [("FEE", ["运费要不要另付", "要不要运费"]), ("TAX", ["发票怎么开"])]
    heavy = similar.SimilarIndex(entries).rank("明天要不要带伞")
    light = similar.SimilarIndex(entries, light_chars=frozenset("要不"), light_weight=0.1).rank(
        "明天要不要带伞")
    assert dict(light)["FEE"] < dict(heavy)["FEE"]


@pytest.mark.parametrize("text", [
    "where is my parcel",          # 没有汉字
    "发票",                         # 太短
    "不用开发票了",                  # 否定所需
    "我不是要开发票",
    "听说发票能重开",                # 第三人称 / 转述
    "我朋友问能开发票吗",
])
def test_hard_abstain_rules_t186(text):
    index = _toy_index_t186()
    assert similar.nearest(index, text, min_similarity=0.0, min_margin=0.0) is None


def test_threshold_and_margin_decide_t186():
    index = _toy_index_t186()
    found = similar.nearest(index, "能不能开发票呀", min_similarity=0.0, min_margin=0.0)
    assert found is not None and found.key == "B" and found.runner_up == "C"
    assert similar.nearest(index, "能不能开发票呀", min_similarity=found.score + 0.01,
                           min_margin=0.0) is None
    assert similar.nearest(index, "能不能开发票呀", min_similarity=0.0,
                           min_margin=found.margin + 0.01) is None
    # B 与 C 都说「发票」：几乎打平的一句在缺省差距下弃权，不在两篇之间猜。
    tie = index.rank("发票的事")
    assert tie[0][1] - tie[1][1] < similar.MIN_MARGIN
    assert similar.nearest(index, "发票的事", min_similarity=0.0) is None


def test_constants_are_pinned_t186():
    """阈值与差距是在开发集 + 自写例句上扫出来的（DECISIONS task-t186）；近邻那篇以余弦进 ScriptHit，
    组稿按 MIN_SCRIPT_SCORE 收，所以近邻门槛不许低于它。"""
    assert (similar.MIN_SIMILARITY, similar.MIN_MARGIN) == (0.34, 0.10)
    assert (similar.NGRAM_MIN, similar.NGRAM_MAX) == (1, 3)
    assert similar.MIN_SIMILARITY >= scripts.MIN_SCRIPT_SCORE
    assert similar.CHANNEL == "similar"


def test_similar_module_imports_only_the_standard_library_t186():
    src = pathlib.Path(similar.__file__).read_text(encoding="utf-8")
    names = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or "").split(".")[0])
    assert names and names <= set(sys.stdlib_module_names) | {"__future__"}, names


# ---------------------------------------------------------------------------
# 2. 接线：match_scripts 的开关、KbRetrieved、组稿与后置校验
# ---------------------------------------------------------------------------
def _match_t186(store, text, **kw):
    return scripts.match_scripts(store, tenant_id=TENANT_T186, text=text, plan_id="cs:csc-t186",
                                 task_id="csc-t186-t0001", **kw)


def _kb_details_t186(store):
    rows = [r for r in store.list_event_log("cs:csc-t186") if r["event_type"] == "KbRetrieved"]
    return [{k: v for k, v in r["detail"].items() if k != "duration_ms"} for r in rows]


def test_default_is_unchanged_and_abstaining_is_byte_identical_t186():
    """缺省 nearest=False 逐字节同 p14；nearest=True 而近邻弃权（或词法本来就命中）时，
    返回值与 KbRetrieved（除时长）也与缺省逐字节相同。覆盖开发集全部轮 + 本文件全部例句。"""
    texts = [t for c in evaluate.load_cases() for t in c.turns]
    texts += [t for c in evaluate.load_cases(evaluate.P13_EVAL_PATH) for t in c.turns]
    texts += [t for t, _ in C9_T186 + C10_T186] + list(NEGATIVES_T186)
    fired = []
    for text in texts:
        query = retrieval_query(text)
        off_store, on_store = _seeded_store_t186(), _seeded_store_t186()
        off = _match_t186(off_store, query)
        on = _match_t186(on_store, query, nearest=True)
        on_detail = _kb_details_t186(on_store)
        assert not _kb_details_t186(off_store)[0]["query"].get("channel"), text
        if on_detail and on_detail[0]["query"].get("channel") == similar.CHANNEL:
            fired.append(text)
            continue
        assert (off, _kb_details_t186(off_store)) == (on, on_detail), text
    assert fired == ["行，知道了，辛苦你啦"], fired


def test_similar_hit_goes_through_compose_and_post_check_t186():
    """近邻命中那篇照常组稿：answer、引用 kb:<doc_id>、过后置校验；KbRetrieved 标 channel=similar，
    docs 就是那一篇，客户原文不进 event_log。"""
    text = "行，知道了，辛苦你啦"
    res, fired, store = _one_t186(text)
    assert fired
    assert (res.route, res.intent) == (_ANS, T.INTENT_GENERAL)
    assert res.draft.citations == (_doc_id_t186("GEN-002"),)
    assert res.reply_text == _bodies_t186()["GEN-002"]["script"]
    assert res.check is not None and res.check.ok
    (row,) = _kb_rows_t186(store, res.conversation_id)
    detail = row["detail"]
    assert detail["query"]["channel"] == similar.CHANNEL
    assert detail["query"]["keyword"] == T.text_digest(retrieval_query(text))
    assert detail["query"]["similar_margin"] >= similar.MIN_MARGIN
    assert [d["doc_id"] for d in detail["docs"]] == [_doc_id_t186("GEN-002")]
    assert detail["docs"][0]["score"] >= similar.MIN_SIMILARITY
    assert detail["docs"][0]["channels"] == {similar.CHANNEL: detail["docs"][0]["score"]}
    assert claims.turn_kb_doc_ids(store, conversation_id=res.conversation_id,
                                  turn_id=res.turn_id) == frozenset({_doc_id_t186("GEN-002")})
    dump = json.dumps(store.list_event_log(T.plan_id_for(res.conversation_id)), ensure_ascii=False,
                      default=str)
    assert "辛苦你啦" not in dump and "知道了" not in dump


def _force_neighbor_t186(monkeypatch, scheme_no: str):
    doc_id = _doc_id_t186(scheme_no)
    monkeypatch.setattr(similar, "nearest",
                        lambda index, text, **kw: similar.Neighbor(key=doc_id, score=0.5, margin=0.3))
    return doc_id


def test_handoff_marked_doc_via_similar_still_hands_off_t186(monkeypatch):
    """C10：近邻落到带 needs_order_lookup 标记的查单篇 → 照旧转人工（p12 路径），引用那一篇。"""
    doc_id = _force_neighbor_t186(monkeypatch, "LOG-006")
    res, fired, store = _one_t186("今天天气真好")
    assert fired
    assert (res.route, res.intent, res.handoff_reason) == (_HO, T.INTENT_LOGISTICS,
                                                          T.HANDOFF_NEEDS_ORDER_LOOKUP)
    assert res.draft.citations == (doc_id,) and res.handoff is not None
    assert res.reply_text == _bodies_t186()["LOG-006"]["script"]


def test_similar_is_only_tried_when_lexical_misses_t186(monkeypatch):
    """词法已命中（分 ≥ MIN_SCRIPT_SCORE）的句子，近邻一次都不问。"""
    calls = []
    real = similar.nearest

    def spy(index, text, **kw):
        calls.append(text)
        return real(index, text, **kw)

    monkeypatch.setattr(similar, "nearest", spy)
    store = _seeded_store_t186()
    hits = _match_t186(store, "包邮吗亲", nearest=True)
    assert hits and hits[0].score >= scripts.MIN_SCRIPT_SCORE and calls == []
    _match_t186(store, "今天天气真好", nearest=True)
    assert calls == ["今天天气真好"]


def test_intent_hint_disagreement_abstains_t186(monkeypatch):
    """p13 路径：理解层给了业务意图、近邻那篇是别的意图 → 两路打架，弃权（照旧兜底）。"""
    doc_id = _force_neighbor_t186(monkeypatch, "LOG-006")
    store = _seeded_store_t186()
    assert [h.doc_id for h in _match_t186(store, "今天天气真好", nearest=True,
                                          intent_hint=T.INTENT_LOGISTICS)] == [doc_id]
    store = _seeded_store_t186()
    got = _match_t186(store, "今天天气真好", nearest=True, intent_hint=T.INTENT_REFUND_PAYMENT)
    assert all(h.score < scripts.MIN_SCRIPT_SCORE for h in got)
    assert _kb_details_t186(store)[0]["query"].get("channel") is None
    store = _seeded_store_t186()
    assert [h.doc_id for h in _match_t186(store, "今天天气真好", nearest=True,
                                          intent_hint=T.INTENT_UNKNOWN)] == [doc_id]


def test_kb_disabled_means_no_similar_and_no_event_t186(monkeypatch):
    monkeypatch.setenv("MAOS_KB_ENABLED", "0")
    store = _seeded_store_t186()
    assert _match_t186(store, "行，知道了，辛苦你啦", nearest=True) == []
    assert _kb_details_t186(store) == []


# ---------------------------------------------------------------------------
# 3. 召回、弃权率、零「自信答错」
# ---------------------------------------------------------------------------
def _report_t186(label, rows):
    ok = [r for r in rows if (r[2], r[3], r[4]) == _expected_t186(r[1]) and r[5] == r[1]]
    via_similar = [r for r in rows if r[6]]
    return (f"{label}: 答对 {len(ok)}/{len(rows)}，经近邻 {len(via_similar)} 句"
            f"（其中对 {sum(1 for r in via_similar if r in ok)}）"), ok, via_similar


@pytest.mark.parametrize("label,sentences,floor", [
    ("C9", C9_T186, C9_RECALL_FLOOR_T186),
    ("C10", C10_T186, C10_RECALL_FLOOR_T186),
])
def test_category_recall_and_no_confident_wrong_t186(label, sentences, floor):
    assert len(sentences) >= 20
    rows = _measure_t186(sentences)
    summary, ok, via_similar = _report_t186(label, rows)
    print(summary)
    assert len(ok) >= floor, summary
    # 近邻命中的每一句都对。
    assert all(r in ok for r in via_similar), [r[0] for r in via_similar if r not in ok]
    # 零「自信答错」：route=answer 的轮意图必对（词法已有的那一句单列，与近邻无关）。
    wrong = {r[0] for r in rows if r[2] == _ANS and r[3] != _expected_t186(r[1])[1]}
    assert wrong <= KNOWN_LEXICAL_CONFIDENT_WRONG_T186, sorted(wrong)
    assert not (wrong & {r[0] for r in via_similar})


def test_similar_channel_contributes_t186():
    rows = _measure_t186(C9_T186 + C10_T186)
    via = [r for r in rows if r[6]]
    assert len(via) >= SIMILAR_HITS_FLOOR_T186
    assert all((r[2], r[3], r[4]) == _expected_t186(r[1]) and r[5] == r[1] for r in via)


def test_negatives_abstain_t186():
    """不该命中的 32 句：近邻通道一次都不启用（弃权率 32/32 = 100%）。整条前台上它们要么兜底，要么是
    词法 / 同义归一已有的判定（否定句里有三句词法照旧检到查单篇 —— 与近邻无关，见 BACKLOG task-t186）。"""
    assert len(NEGATIVES_T186) >= 30
    fired = []
    for text in NEGATIVES_T186:
        res, got, _ = _one_t186(text)
        if got:
            fired.append(text)
        assert res.route != _ANS or not got, text
    rate = 1 - len(fired) / len(NEGATIVES_T186)
    print(f"近邻弃权率 {len(NEGATIVES_T186) - len(fired)}/{len(NEGATIVES_T186)} = {rate:.0%}")
    assert fired == []


def test_similar_never_fires_on_the_dev_sets_t186():
    """p12 / p13 开发集上近邻一次都不启用 —— 开发集逐轮照旧（逐轮期望由 T169 / T174 的测试钉）。"""
    stores = []

    def p12_factory():
        store = SqliteStore(":memory:")
        stores.append(store)
        return _desk_t186(store)

    def p13_factory(ports):
        store = SqliteStore(":memory:")
        stores.append(store)
        seed_cs_kb(store)
        return FrontDesk(store, CsConfig(tenants={KFID_T186: TENANT_T186}, handoff_target=None),
                         clock=lambda: "2026-09-25T08:00:00+00:00", **ports)

    evaluate.run_eval(p12_factory, evaluate.load_cases())
    evaluate.run_eval_p13(p13_factory, evaluate.load_cases(evaluate.P13_EVAL_PATH))
    assert stores
    for store in stores:
        rows = [dict(r) for r in store._conn.execute(
            "SELECT detail FROM event_log WHERE event_type='KbRetrieved'").fetchall()]
        assert not any('"channel": "similar"' in (r["detail"] or "") for r in rows)


# ---------------------------------------------------------------------------
# 4. 例句是自写的
# ---------------------------------------------------------------------------
def test_self_written_sentences_are_not_copies_t186():
    """不是开发集句子、不是话术库登记过的说法；不带数字（不凑具体日期 / 金额 / 单号）。"""
    dev = {t for c in evaluate.load_cases() for t in c.turns}
    dev |= {t for c in evaluate.load_cases(evaluate.P13_EVAL_PATH) for t in c.turns}
    registered = set()
    for body in _bodies_t186().values():
        registered |= {similar.normalize(v) for v in [*body["synonyms"], *body["examples"]]}
    texts = [t for t, _ in C9_T186 + C10_T186] + list(NEGATIVES_T186)
    assert len(set(texts)) == len(texts)
    for text in texts:
        assert text not in dev and similar.normalize(text) not in registered, text
        assert not re.search(r"[0-9０-９]", text), text
    schemes = set(_bodies_t186())
    assert {s for _, s in C9_T186 + C10_T186} <= schemes
