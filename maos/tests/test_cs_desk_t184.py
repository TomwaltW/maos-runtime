"""T184 · 判定顺序（review/p15-cs-contracts.md §1 C1–C4、§2 T184）的机器验收。

注入了端口的路径（p13 契约 §2 第 6 步前后）改四处判定，每类都用本文件自写的说法（每类 ≥ 8 种）
与反例（每类 ≥ 4 条：相近、但判定不该变）钉住：

* C1：泛泛的「帮我查物流 / 看下我包裹 / 物流查一下」没有单号 → ``clarify`` 追问 order_no，不再经
  话术篇（LOG-004 一类）的转人工标记直接 needs_order_lookup；会话里已有单号就查；
* C2：英文的订单状态问法（「is order A1001 on its way」「has my parcel shipped」）按词认、进查单；
* C3：上一轮刚追问过单号、本轮仍没给但仍在说这一单 → 追问没用尽再问一次，用尽就 needs_order_lookup；
* C4：假设 / 泛问句（「要是退货的话运费谁出」「what if …」）问的是规则，不进查单、不追问。

不变量（p15 契约 §0）：没注入端口时（p12 路径）逐字节不变 —— :data:`P12_GOLDEN_T184` 是在基线
7c19a1d 的 desk.py 上、同一时钟跑本文件全部说法得出的指纹；编造 0、错状态 0、零自信答错（每一轮
逐个过）；触发词先于这四处判定；连续兜底规则照旧。p12 / p13 开发集全绿由 T169 / T174 的评测测试钉住。
"""

from __future__ import annotations

import hashlib
import json

import pytest

from maos.core.store import SqliteStore
from maos.domain.cs import claims, evaluate, records
from maos.domain.cs import desk as D
from maos.domain.cs import types as T
from maos.domain.cs.corpus import seed_cs_kb
from maos.domain.cs.desk import CsConfig, FrontDesk
from maos.domain.cs.ports import (
    BINDING_TEST,
    LANG_EN,
    LANG_ZH,
    LOOKUP_NOT_FOUND,
    LOOKUP_OK,
    MAX_ASKS_PER_SLOT,
    ORDER_STATUS_WORDING,
    SLOT_ORDER_NO,
    Binding,
    LookupResult,
    PrecheckResult,
)
from maos.ingress.contracts import CHANNEL_WECHAT_KF, InboundMessage

KFID_T184 = "wk_t184"
TENANT_T184 = "tnt-demo"
USER_T184 = "wm_t184_customer_0001"
CLOCK_T184 = "2026-09-25T09:00:00+00:00"
ORDER_T184 = "A1001"


# ---------------------------------------------------------------------------
# 替身端口：只认 USER_T184 名下的 A1001，查单一律 shipped；记下每一次调用
# ---------------------------------------------------------------------------
class _Verifier_t184:
    def __init__(self):
        self.calls = []

    def resolve(self, store, *, tenant_id, channel, external_userid, display_no):
        self.calls.append(display_no)
        if display_no != ORDER_T184:
            return None
        return Binding(tenant_id=tenant_id, channel=channel, external_userid=external_userid,
                       display_no=display_no, system_name="demo-orders",
                       query_key="qk-" + display_no, source=BINDING_TEST)


class _Lookup_t184:
    def __init__(self):
        self.calls = []

    def lookup(self, store, binding, *, plan_id, task_id):
        self.calls.append(binding.query_key)
        if binding.query_key != "qk-" + ORDER_T184:
            return LookupResult(outcome=LOOKUP_NOT_FOUND, system_name="demo-orders",
                                query_key=binding.query_key, error_kind="KeyError")
        return LookupResult(outcome=LOOKUP_OK, system_name="demo-orders", query_key=binding.query_key,
                            status="shipped", version=1, updated_at="2026-09-24T00:00:00+00:00")


class _Precheck_t184:
    def __init__(self):
        self.calls = []

    def precheck(self, *, tenant_id, order_no, reason_text, now):
        self.calls.append(order_no)
        return PrecheckResult(ok=False, refused_why="order_not_in_ledger")


class _Talk_t184:
    """一段会话。``ports=False`` 就是 p12 路径（三个端口全为 None）。"""

    def __init__(self, *, ports: bool = True, user: str = USER_T184):
        self.store = SqliteStore(":memory:")
        seed_cs_kb(self.store)
        self.verifier, self.lookup, self.precheck = _Verifier_t184(), _Lookup_t184(), _Precheck_t184()
        kw = ({"verifier": self.verifier, "lookup": self.lookup, "precheck": self.precheck}
              if ports else {})
        self.desk = FrontDesk(self.store, CsConfig(tenants={KFID_T184: TENANT_T184}),
                              clock=lambda: CLOCK_T184, **kw)
        self.user, self.n, self.results = user, 0, []

    def say(self, text: str):
        self.n += 1
        res = self.desk.handle(InboundMessage(
            channel=CHANNEL_WECHAT_KF, chat_id=self.user, sender=self.user, text=text,
            msg_id=f"t184-{self.n}", raw={"open_kfid": KFID_T184}))
        self.results.append(res)
        return res

    def talk(self, turns):
        for t in turns:
            self.say(t)
        return self.results[-1]


def _one_t184(text: str, *, ports: bool = True):
    talk = _Talk_t184(ports=ports)
    return talk, talk.say(text)


def _assert_safe_t184(talk: _Talk_t184) -> None:
    """编造 0、错状态 0、零自信答错：每一轮的校验都过；说了状态的轮必须挂本轮 shipped 观察、
    措辞逐字是措辞表那句；answer 的轮意图不是 unknown。"""
    for res in talk.results:
        assert res.check.ok, res
        said = evaluate.said_statuses(res.reply_text)
        if said:
            assert said == {"shipped"} and res.lookup_outcome == LOOKUP_OK, res
            assert res.reply_text == ORDER_STATUS_WORDING[res.lang]["shipped"]
            assert [c.basis_ref.startswith(T.BASIS_OBS) for c in res.draft.claims] == [True]
            obs = records.turn_observation_ids(talk.store, conversation_id=res.conversation_id,
                                               turn_id=res.turn_id)
            assert {c.basis_ref[len(T.BASIS_OBS):] for c in res.draft.claims} <= obs
            gate = claims.check_reply(res.draft, observations=obs, kb_doc_ids=frozenset(
                claims.turn_kb_doc_ids(talk.store, conversation_id=res.conversation_id,
                                       turn_id=res.turn_id)))
            assert gate.ok
        if res.route == T.ROUTE_ANSWER:
            assert res.intent != T.INTENT_UNKNOWN


# ---------------------------------------------------------------------------
# 说法表（自写；不来自任何评测集）
# ---------------------------------------------------------------------------
#: C1：要看这一单、但没说单号、没有诉求词与进度线索（或只是泛泛的查物流）。
C1_SAYINGS_T184 = (
    "帮我查物流", "查个快递", "看下我包裹", "物流查一下", "看看我的快递", "帮忙查查包裹",
    "瞅瞅我的快递", "快递帮我看看", "我想查快递", "麻烦看一下我的订单", "追踪一下我的包裹",
    "包裹信息帮我查一下",
)
#: C1 反例：问规则 / 问怎么查 / 与订单无关 —— 不进查单分支。
C1_ANTI_T184 = ("你们发什么快递", "快递费怎么算", "快递公司是哪家", "在哪里可以看物流",
                "看看你们有什么新款")

#: C2：英文订单状态问法，带单号（→ 查单）。
C2_WITH_NO_T184 = (
    "is order A1001 on its way", "has A1001 shipped", "where is order A1001",
    "what's the status of order A1001", "has order A1001 been dispatched",
    "can you check order A1001 for me", "is A1001 coming", "any update on order A1001?",
    "Is my order A1001 still in transit?", "did A1001 ship yet",
)
#: C2：英文订单状态问法，不带单号（→ 追问，英文）。
C2_NO_NUMBER_T184 = (
    "is my order on the way", "did my package ship", "any update on my order?",
    "I want to know where my order is", "check my order", "has my parcel been sent out",
    "Has it shipped?", "where's my stuff", "track my shipment please", "is my package out for delivery",
)
#: C2 反例：问规则、别的词里夹着 check / track / ship（按词认，认不进去）。
C2_ANTI_T184 = (
    "which courier do you use", "how long does shipping take", "do you ship to Canada",
    "is the checkout page broken", "I bought a tracksuit, what sizes do you have",
    "is shipping free", "where is your store",
)
#: C2 按词认：这些句子里的 check / track / ship / where 都嵌在别的词里。
C2_EMBEDDED_T184 = (
    "I rechecked my order details", "love the tracksuit my order came with",
    "the checkout of my order page", "wherever my order goes", "hasty order comments",
)

#: C3：追问用尽（两次）之后的下一轮 —— 仍没给单号、仍在说这一单。(前两轮, 第三轮)
C3_EXHAUSTED_T184 = (
    (("帮我查物流", "查一下嘛"), "找不到单号，你直接告诉我在哪"),
    (("帮我查物流", "查一下嘛"), "我不知道单号"),
    (("帮我查物流", "查一下嘛"), "单号忘了，你们查不到吗"),
    (("看下我包裹", "看一下嘛"), "订单删了找不到了"),
    (("看下我包裹", "看一下嘛"), "没有单号，直接帮我看看"),
    (("我的快递到哪了", "快递到哪了"), "记不得了，反正就是上周买的那个"),
    (("我的快递到哪了", "快递到哪了"), "不清楚单号是多少"),
    (("where is my package", "where is my package"), "no idea what the number is, can I just return it"),
    (("where is my package", "where is my package"), "I can't find the order number"),
    (("where is my package", "where is my package"), "I forgot the number"),
)
#: C3 反例：追问用尽后的下一轮说的是别的（道谢、问规则、寒暄）或给了单号。
C3_ANTI_T184 = ("谢谢", "退货运费谁出", "你好", ORDER_T184, "今天天气怎么样")

#: C4：假设 / 泛问句（问规则）。
C4_SAYINGS_T184 = (
    "要是退货的话运费谁出", "如果退货怎么办", "如果我要退款，多久能到账", "假如收到的东西坏了怎么办",
    "如果我想换货要怎么弄", "万一快递丢了怎么办", "如果包裹一直没到怎么办", "要是签收了没收到怎么办",
    "假如退款没到账怎么办", "A1001 如果退货运费谁出", "一般情况下退款多久到账",
    "what if my order arrives damaged", "if I return it who pays shipping",
)
#: C4 反例：要办 / 在查这一单（祈使句、客气话里的「如果可以」、真实的「没到怎么办」）。
C4_ANTI_T184 = (
    "我要退货，运费谁出", "我的包裹一直没到，怎么办", "如果可以的话帮我查一下物流",
    "要是今天不发货就给我退款", "如果方便，帮我看下A1001到哪了",
    "if possible, could you check order A1001",
    # 复核 L2-1 / L2-2：间接问句里的 if / whether 不是假设；条件从句前已经有完整的「看这一单」分句
    "Can you check if my order A1001 has shipped?", "is order A1001 on its way? if not I want to cancel",
    "帮我查下A1001到哪了，如果今天不到怎么办", "我的包裹一直没到，要是今天还不到怎么办",
)

#: 复核 L2-1：英文间接问句（check if / let me know if / wondering whether）带单号 → 查单。
C2_INDIRECT_T184 = (
    "Can you check if my order A1001 has shipped?", "Please check if order A1001 is on its way",
    "Could you let me know if A1001 has shipped?", "I'm wondering whether A1001 has been dispatched",
    "can you see if A1001 has been sent out", "tell me if order A1001 is in transit",
    "is order A1001 on its way? if not I want to cancel", "check whether my order A1001 has arrived",
)
#: 复核 L3-2：英文商品 / 闲话里的 it 与带数字的词不是订单宾语。
C2_PRODUCT_ANTI_T184 = (
    "Is it shipped with a charger?", "Did it come with a warranty?", "Is it coming in blue?",
    "Can I find 100ml bottles here", "I will check it later, thanks", "Has it arrived in stores yet",
    "Where is the item size guide", "check the 250ml size",
)
#: 复核 L3-1：问商品 / 问规则 / 问别人的订单 —— 检索会落到查单篇，但本轮没有「看这一单」的信号。
C1_REROUTE_ANTI_T184 = (
    "帮我查一下这个商品的价格", "订单能合并发货吗", "别人的订单能帮忙查吗", "帮我看看这款耳机有没有货",
    "看看我的订单有没有优惠券",
)
#: 复核 L2-3 / L3-3：追问之后换了个问题 / 明说不查了 —— 不再接回查单分支。
#: 第三轮复核 L2-2：「不知道 / 不清楚 / 忘了」没挨着单号、后面另起的是政策问句，也是换了题。
C3_SWITCH_T184 = (
    "算了不查了，我想问下七天无理由怎么退", "算了，你们发什么快递", "直接问下，发什么快递", "我想看看新品",
    "订单能合并发货吗", "never mind, do you ship to Canada",
    "不知道你们运费谁出", "我不清楚七天无理由怎么算", "不清楚，退款多久到账", "忘了问，你们发什么快递",
    "我不记得七天无理由是不是从签收算", "is there a number I can call?",
)
#: 第三轮复核 L2-3 / L2-4：明说不查了、同时又提到给不出单号 —— 不查了优先，不再追问、不转人工。
C3_DROP_T184 = (
    "算了，单号找不到就不查了", "算了不查了，单号不记得了", "不用查了，单号找不到",
    "never mind, I forgot the order number", "never mind, I can't find the order number",
)
#: 第三轮复核 L2-2：收窄之后仍算「还在说这一单」的短回答（自成一句的忘了 / 不记得、挨着单号说）。
C3_STILL_T184 = (
    "不记得了", "忘了", "找不到了，你们查不到吗", "订单删了找不到了", "I forgot", "no idea",
    "I don't remember the order no.", "not sure what the tracking number is",
)
#: 第三轮复核 L3-B：明说不用查 / 不是查 —— 不是在要看这一单，不追问单号。
NEG_LOOKUP_T184 = (
    "不用帮我查快递，退货运费谁出", "我不想查物流，问下退货规则", "不是查物流，我想问退货规则",
    "don't check my order, what's the return policy",
    "I don't want to check my order, is there a warranty", "no need to check my parcel, do you ship to Canada",
)
#: 第三轮复核 L2-1：「的话」作话题标记、话题是单号 / 订单名词 —— 不是假设，照查单。
TOPIC_DEHUA_T184 = (
    "A1001的话到哪了", "A1001的话现在什么状态", "订单A1001的话，发货了吗？", "A1001的话发货了吗",
    "我的快递的话到哪了",
)
#: 第三轮复核 L3-A：条件问句在前、另起一句在要看这一单（带单号）—— 不是假设，照查单。
COND_FIRST_T184 = (
    "如果今天不到怎么办，帮我查下A1001到哪了", "what if it doesn't arrive? where is order A1001",
    "if it's late can I cancel? has A1001 shipped", "要是退货的话运费谁出？另外A1001到哪了",
)


# ---------------------------------------------------------------------------
# C1
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text", C1_SAYINGS_T184)
def test_c1_generic_lookup_without_number_asks_for_order_no_t184(text):
    talk, res = _one_t184(text)
    assert (res.route, res.ask_slot, res.lang) == (T.ROUTE_CLARIFY, SLOT_ORDER_NO, LANG_ZH), res
    assert res.intent == T.INTENT_LOGISTICS
    assert res.reply_text == D.REPLY_ASK_ORDER_NO and res.handoff is None
    assert talk.verifier.calls == [] and talk.lookup.calls == []
    _assert_safe_t184(talk)


@pytest.mark.parametrize("text", C1_SAYINGS_T184)
def test_c1_generic_lookup_with_number_in_session_looks_it_up_t184(text):
    talk = _Talk_t184()
    first = talk.say(f"我的订单号是{ORDER_T184}")
    assert first.route == T.ROUTE_FALLBACK                      # 只报单号：记槽位、不查（p13 口径）
    res = talk.say(text)
    assert (res.route, res.lookup_outcome) == (T.ROUTE_ANSWER, LOOKUP_OK), res
    assert res.reply_text == ORDER_STATUS_WORDING[LANG_ZH]["shipped"]
    assert talk.lookup.calls == ["qk-" + ORDER_T184]
    _assert_safe_t184(talk)


@pytest.mark.parametrize("text", C1_SAYINGS_T184)
def test_c1_answering_the_ask_with_only_the_number_looks_it_up_t184(text):
    talk = _Talk_t184()
    assert talk.say(text).route == T.ROUTE_CLARIFY
    res = talk.say(ORDER_T184)
    assert (res.route, res.lookup_outcome, res.intent) == (T.ROUTE_ANSWER, LOOKUP_OK,
                                                           T.INTENT_LOGISTICS), res
    _assert_safe_t184(talk)


def test_c1_sayings_are_recognised_by_the_lookup_phrase_table_t184():
    assert [t for t in C1_SAYINGS_T184 if not D.asks_order_status(t)] == []
    assert [t for t in C1_ANTI_T184 if D.asks_order_status(t)] == []


@pytest.mark.parametrize("text", C1_ANTI_T184)
def test_c1_anti_examples_do_not_ask_for_order_no_t184(text):
    talk, res = _one_t184(text)
    assert res.route != T.ROUTE_CLARIFY, res
    assert talk.verifier.calls == [] and talk.lookup.calls == []
    _assert_safe_t184(talk)


def test_c1_order_view_script_marker_no_longer_hands_off_directly_t184():
    """检索落到「看这一单」的查单篇（这里是 PAY-004）、说法表与进度线索都不中：注入了端口就走查单
    分支追问单号，而不是凭话术篇的转人工标记直接 needs_order_lookup；p12 路径照旧转人工。"""
    text = "付款一直失败"
    assert not D.asks_order_status(text)
    talk, res = _one_t184(text)
    assert (res.route, res.ask_slot, res.intent) == (T.ROUTE_CLARIFY, SLOT_ORDER_NO,
                                                     T.INTENT_REFUND_PAYMENT)
    _, p12 = _one_t184(text, ports=False)
    assert (p12.route, p12.handoff_reason) == (T.ROUTE_HANDOFF, T.HANDOFF_NEEDS_ORDER_LOOKUP)
    assert p12.draft.citations == (evaluate.cite_doc_id(TENANT_T184, "PAY-004"),)


def test_c1_action_scripts_keep_their_marker_t184():
    """改地址（LOG-005）不是「看这一单」：注入了端口照旧凭标记转人工，不追问、不查单。"""
    talk, res = _one_t184("地址填错了想改一下")
    assert (res.route, res.handoff_reason) == (T.ROUTE_HANDOFF, T.HANDOFF_NEEDS_ORDER_LOOKUP)
    assert not D.cites_order_view(res.draft.citations)
    assert talk.lookup.calls == []


# ---------------------------------------------------------------------------
# C2
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text", C2_WITH_NO_T184)
def test_c2_english_status_question_with_number_looks_up_t184(text):
    talk, res = _one_t184(text)
    assert (res.route, res.lang, res.lookup_outcome) == (T.ROUTE_ANSWER, LANG_EN, LOOKUP_OK), res
    assert res.intent == T.INTENT_LOGISTICS
    assert res.reply_text == ORDER_STATUS_WORDING[LANG_EN]["shipped"]
    assert talk.lookup.calls == ["qk-" + ORDER_T184]
    _assert_safe_t184(talk)


@pytest.mark.parametrize("text", C2_NO_NUMBER_T184)
def test_c2_english_status_question_without_number_asks_in_english_t184(text):
    talk, res = _one_t184(text)
    assert (res.route, res.ask_slot, res.lang) == (T.ROUTE_CLARIFY, SLOT_ORDER_NO, LANG_EN), res
    assert res.reply_text == D.REPLY_ASK_ORDER_NO_EN
    assert talk.lookup.calls == []
    _assert_safe_t184(talk)


def test_c2_english_status_question_then_number_looks_up_t184():
    talk = _Talk_t184()
    assert talk.say("is my order on the way").route == T.ROUTE_CLARIFY
    res = talk.say(ORDER_T184)
    assert (res.route, res.lang) == (T.ROUTE_ANSWER, LANG_EN)
    assert res.reply_text == ORDER_STATUS_WORDING[LANG_EN]["shipped"]
    _assert_safe_t184(talk)


@pytest.mark.parametrize("text", C2_ANTI_T184)
def test_c2_anti_examples_stay_out_of_lookup_t184(text):
    assert not D.asks_order_status(text)
    talk, res = _one_t184(text)
    assert res.route == T.ROUTE_FALLBACK and res.lang == LANG_EN, res
    assert talk.verifier.calls == [] and talk.lookup.calls == []


def test_c2_english_words_are_matched_as_whole_words_t184():
    assert [t for t in C2_WITH_NO_T184 + C2_NO_NUMBER_T184 if not D.asks_order_status(t)] == []
    assert [t for t in C2_EMBEDDED_T184 if D.asks_order_status(t)] == []
    # 同一句换成整词就认：边界确实是「按词」而不是整句都不认
    assert D.asks_order_status("I checked, can you check my order")
    assert D.asks_order_status("track my order")
    assert not D.asks_order_status("racetrack my order")


# ---------------------------------------------------------------------------
# C3
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("first,last", C3_EXHAUSTED_T184)
def test_c3_still_no_number_after_asks_exhausted_hands_off_t184(first, last):
    talk = _Talk_t184()
    for t in first:
        assert talk.say(t).route == T.ROUTE_CLARIFY
    assert len(first) == MAX_ASKS_PER_SLOT
    res = talk.say(last)
    assert (res.route, res.handoff_reason) == (T.ROUTE_HANDOFF, T.HANDOFF_NEEDS_ORDER_LOOKUP), res
    assert res.handoff is not None and res.handoff.suggestion == D.SUGGESTION_ASK_EXHAUSTED
    assert res.intent in (T.INTENT_LOGISTICS, T.INTENT_RETURN_EXCHANGE)
    assert talk.verifier.calls == [] and talk.lookup.calls == []
    assert D.still_on_order(last)
    _assert_safe_t184(talk)


@pytest.mark.parametrize("last", C3_ANTI_T184)
def test_c3_anti_examples_after_asks_exhausted_t184(last):
    talk = _Talk_t184()
    talk.talk(("帮我查物流", "查一下嘛"))
    res = talk.say(last)
    assert res.handoff_reason != T.HANDOFF_NEEDS_ORDER_LOOKUP, res
    if last == ORDER_T184:
        assert (res.route, res.lookup_outcome) == (T.ROUTE_ANSWER, LOOKUP_OK)
    else:
        assert talk.lookup.calls == []
    _assert_safe_t184(talk)


def test_c3_before_exhaustion_asks_once_more_t184():
    talk = _Talk_t184()
    talk.say("帮我查物流")
    res = talk.say("找不到单号")
    assert (res.route, res.ask_slot) == (T.ROUTE_CLARIFY, SLOT_ORDER_NO)
    assert records.ask_count(talk.store, res.tenant_id, res.conversation_id, SLOT_ORDER_NO) == 2


def test_c3_needs_the_previous_turn_to_be_the_ask_t184():
    """没追问过单号时，「找不到单号」不凭空进查单分支（C3 只接追问之后的那一轮）。"""
    talk, res = _one_t184("找不到单号")
    assert res.route != T.ROUTE_CLARIFY and res.handoff_reason != T.HANDOFF_NEEDS_ORDER_LOOKUP


# ---------------------------------------------------------------------------
# C4
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text", C4_SAYINGS_T184)
def test_c4_hypothetical_questions_go_to_policy_not_lookup_t184(text):
    assert D.hypothetical_question(text)
    talk, res = _one_t184(text)
    assert res.route != T.ROUTE_CLARIFY and res.ask_slot == "", res
    assert talk.verifier.calls == [] and talk.lookup.calls == [] and talk.precheck.calls == []
    if res.lang == LANG_ZH:
        # 走的是政策检索：本轮落了一条 KbRetrieved，结果照检索（答政策 / 查单篇照旧转人工 / 兜底）
        kb = [r for r in talk.store.list_event_log(T.plan_id_for(res.conversation_id))
              if r["event_type"] == "KbRetrieved"]
        assert len(kb) == 1
    _assert_safe_t184(talk)


def test_c4_policy_answers_cite_the_rule_scripts_t184():
    got = {t: _one_t184(t)[1] for t in ("要是退货的话运费谁出", "如果我要退款，多久能到账",
                                        "如果我想换货要怎么弄", "A1001 如果退货运费谁出")}
    assert {t: r.route for t, r in got.items()} == dict.fromkeys(got, T.ROUTE_ANSWER)
    assert got["要是退货的话运费谁出"].draft.citations == (evaluate.cite_doc_id(TENANT_T184, "RET-004"),)
    assert got["A1001 如果退货运费谁出"].draft.citations == (evaluate.cite_doc_id(TENANT_T184, "RET-004"),)
    assert got["如果我要退款，多久能到账"].draft.citations == (evaluate.cite_doc_id(TENANT_T184, "PAY-001"),)


@pytest.mark.parametrize("text", C4_ANTI_T184 + TOPIC_DEHUA_T184 + COND_FIRST_T184)
def test_c4_anti_examples_still_look_at_the_order_t184(text):
    assert not D.hypothetical_question(text)
    talk, res = _one_t184(text)
    if ORDER_T184 in text:
        assert (res.route, res.lookup_outcome) == (T.ROUTE_ANSWER, LOOKUP_OK), res
    else:
        assert (res.route, res.ask_slot) == (T.ROUTE_CLARIFY, SLOT_ORDER_NO), res
    _assert_safe_t184(talk)


# ---------------------------------------------------------------------------
# 复核意见（L2-1 / L2-2 / L2-3 / L3-1 / L3-2 / L3-3）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text", C2_INDIRECT_T184)
def test_indirect_if_is_not_hypothetical_and_looks_up_t184(text):
    assert not D.hypothetical_question(text)
    talk, res = _one_t184(text)
    assert (res.route, res.lang, res.lookup_outcome) == (T.ROUTE_ANSWER, LANG_EN, LOOKUP_OK), res
    assert res.reply_text == ORDER_STATUS_WORDING[LANG_EN]["shipped"]
    _assert_safe_t184(talk)


def test_what_if_is_still_hypothetical_t184():
    assert D.hypothetical_question("I want to know what if my order arrives damaged")
    assert D.hypothetical_question("can you tell me what happens if my parcel is lost?")


def test_indirect_if_without_number_asks_for_order_no_t184():
    talk, res = _one_t184("can you check if my order has shipped?")
    assert (res.route, res.ask_slot, res.lang) == (T.ROUTE_CLARIFY, SLOT_ORDER_NO, LANG_EN), res


@pytest.mark.parametrize("text", C2_PRODUCT_ANTI_T184)
def test_english_product_questions_are_not_order_status_t184(text):
    assert not D.asks_order_status(text)
    talk, res = _one_t184(text)
    assert res.route == T.ROUTE_FALLBACK and res.lang == LANG_EN, res
    # 会话里已有单号、上一轮刚答过状态：商品问题也不拿去答状态
    talk = _Talk_t184()
    assert talk.say(f"is order {ORDER_T184} on its way").route == T.ROUTE_ANSWER
    res = talk.say(text)
    assert res.route != T.ROUTE_ANSWER and res.ask_slot == "", res
    assert talk.lookup.calls == ["qk-" + ORDER_T184]
    _assert_safe_t184(talk)


@pytest.mark.parametrize("text", C1_REROUTE_ANTI_T184)
def test_product_or_policy_questions_keep_the_script_marker_t184(text):
    assert not D.order_view_signal(text) and not D.asks_order_status(text)
    talk, res = _one_t184(text)
    assert res.route != T.ROUTE_CLARIFY and res.ask_slot == "", res
    talk = _Talk_t184()
    assert talk.say(f"帮我查下{ORDER_T184}到哪了").route == T.ROUTE_ANSWER
    res = talk.say(text)
    assert res.route != T.ROUTE_ANSWER and res.lookup_outcome == "", res
    assert talk.lookup.calls == ["qk-" + ORDER_T184]
    _assert_safe_t184(talk)


@pytest.mark.parametrize("text", C3_SWITCH_T184)
def test_switching_topic_after_an_ask_is_not_pulled_back_t184(text):
    assert not D.still_on_order(text)
    talk = _Talk_t184()
    talk.say("帮我查一下物流")
    res = talk.say(text)
    assert res.route != T.ROUTE_CLARIFY and res.ask_slot == "", res
    talk = _Talk_t184()
    talk.talk(("帮我查物流", "查一下嘛"))
    res = talk.say(text)
    assert res.handoff is None or res.handoff.suggestion != D.SUGGESTION_ASK_EXHAUSTED, res
    _assert_safe_t184(talk)


def test_policy_question_after_ask_gets_the_policy_answer_t184():
    talk = _Talk_t184()
    last = talk.talk(("帮我查物流", "不记得了", "你们发什么快递"))
    assert last.route == T.ROUTE_ANSWER, last
    assert last.draft.citations == (evaluate.cite_doc_id(TENANT_T184, "LOG-002"),)
    talk = _Talk_t184()
    last = talk.talk(("帮我查一下物流", "算了不查了，我想问下七天无理由怎么退"))
    assert (last.route, last.intent) == (T.ROUTE_ANSWER, T.INTENT_RETURN_EXCHANGE), last


# ---------------------------------------------------------------------------
# 第三轮复核（L1-1 / L2-1 / L2-2 / L2-3 / L2-4 / L3-A / L3-B）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text", TOPIC_DEHUA_T184 + COND_FIRST_T184)
def test_topic_de_hua_and_conditional_first_are_not_hypothetical_t184(text):
    assert not D.hypothetical_question(text)


def test_topic_de_hua_duration_question_is_not_answered_as_refund_t184():
    """「请问A1001的话多久能到」问的是物流时长：不算假设、不拿退款到账那篇（PAY-001）去答。"""
    text = f"请问{ORDER_T184}的话多久能到"
    assert not D.hypothetical_question(text)
    talk, res = _one_t184(text)
    assert evaluate.cite_doc_id(TENANT_T184, "PAY-001") not in res.draft.citations, res
    assert not (res.route == T.ROUTE_ANSWER and res.intent == T.INTENT_REFUND_PAYMENT), res
    _assert_safe_t184(talk)


def test_conditional_first_then_order_ask_after_an_ask_t184():
    talk = _Talk_t184()
    talk.say("帮我查物流")
    res = talk.say(f"{ORDER_T184}的话发货了吗")
    assert (res.route, res.lookup_outcome) == (T.ROUTE_ANSWER, LOOKUP_OK), res
    _assert_safe_t184(talk)


def test_hypothetical_de_hua_on_a_rule_topic_still_counts_t184():
    """「退货的话运费谁出」话题是规则（不是单号 / 订单名词）：照旧算假设泛问，答政策。"""
    assert D.hypothetical_question("退货的话运费谁出")
    talk, res = _one_t184("退货的话运费谁出")
    assert res.route == T.ROUTE_ANSWER and res.ask_slot == "", res
    assert res.draft.citations == (evaluate.cite_doc_id(TENANT_T184, "RET-004"),)


@pytest.mark.parametrize("text", C3_DROP_T184)
def test_drop_marker_beats_order_number_words_after_an_ask_t184(text):
    assert not D.still_on_order(text) and not D.asks_order_status(text)
    talk = _Talk_t184()
    talk.say("帮我查物流")
    res = talk.say(text)
    assert res.route != T.ROUTE_CLARIFY and res.ask_slot == "", res
    talk = _Talk_t184()
    talk.talk(("帮我查物流", "查一下嘛"))
    res = talk.say(text)
    assert res.handoff_reason != T.HANDOFF_NEEDS_ORDER_LOOKUP and res.ask_slot == "", res
    assert talk.lookup.calls == []
    _assert_safe_t184(talk)


@pytest.mark.parametrize("text", C3_STILL_T184)
def test_short_cannot_give_number_replies_still_count_t184(text):
    assert D.still_on_order(text)
    talk = _Talk_t184()
    talk.say("帮我查物流")
    res = talk.say(text)
    assert (res.route, res.ask_slot) == (T.ROUTE_CLARIFY, SLOT_ORDER_NO), res
    _assert_safe_t184(talk)


@pytest.mark.parametrize("text", C3_SWITCH_T184)
def test_policy_switch_after_ask_matches_the_policy_path_t184(text):
    """追问之后换了个政策问题：结果与会话里从没追问过时一模一样（不被拉回查单分支）。"""
    talk = _Talk_t184()
    talk.say("帮我查一下物流")
    after_ask = talk.say(text)
    _, fresh = _one_t184(text)
    assert (after_ask.route, after_ask.intent, after_ask.draft.citations) == (
        fresh.route, fresh.intent, fresh.draft.citations), (after_ask, fresh)


def test_english_cannot_find_number_is_not_an_order_status_ask_t184():
    assert not D.asks_order_status("I can't find the order number")
    assert not D.asks_order_status("never mind, I can't find the order number")
    assert D.still_on_order("I can't find the order number")


@pytest.mark.parametrize("text", NEG_LOOKUP_T184)
def test_negated_lookup_is_not_an_order_ask_t184(text):
    assert not D.asks_order_status(text)
    talk, res = _one_t184(text)
    assert res.route != T.ROUTE_CLARIFY and res.ask_slot == "", res
    assert talk.lookup.calls == []
    _assert_safe_t184(talk)


def test_new_desk_paths_keep_customer_text_out_of_event_log_t184():
    """R5：C1 改道（检索 → 查单分支）与 C3 追问用尽这两条新路径，事件日志里不落客户原文、回复原文、
    external_userid、open_kfid、单号与查询键。"""
    user = "wm_t184_sentinel_user_7q"
    flows = (
        ("付款一直失败",),
        (f"我的订单号是{ORDER_T184}", "付款一直失败"),
        ("帮我查物流", "查一下嘛", "找不到单号，你直接告诉我在哪"),
        ("where is my package", "I forgot"),
        (f"is order {ORDER_T184} on its way",),
        ("要是退货的话运费谁出",),
    )
    for i, flow in enumerate(flows):
        talk = _Talk_t184(user=f"{user}{i}")
        talk.talk(flow)
        convs = {r.conversation_id for r in talk.results}
        blob = json.dumps([dict(r) for c in convs for r in talk.store.list_event_log(T.plan_id_for(c))],
                          ensure_ascii=False, default=str)
        assert blob != "[]"
        sentinels = {f"{user}{i}", KFID_T184, ORDER_T184, "qk-" + ORDER_T184}
        sentinels |= set(flow) | {r.reply_text for r in talk.results}
        assert [s for s in sentinels if s and s in blob] == [], flow


# ---------------------------------------------------------------------------
# 不变量
# ---------------------------------------------------------------------------
def test_triggers_still_come_first_t184():
    assert _one_t184("如果不给我退款我就投诉")[1].handoff_reason == T.HANDOFF_COMPLAINT
    assert _one_t184("what if I want to talk to a human")[1].handoff_reason == T.HANDOFF_REQUESTED
    talk = _Talk_t184()
    talk.talk(("帮我查物流", "查一下嘛"))
    assert talk.say("找不到单号，我要投诉").handoff_reason == T.HANDOFF_COMPLAINT


def test_fallback_streak_rule_unchanged_with_ports_t184():
    talk = _Talk_t184()
    first = talk.say("今天天气怎么样")
    second = talk.say("讲个笑话吧")
    assert first.route == T.ROUTE_FALLBACK
    assert (second.route, second.handoff_reason) == (T.ROUTE_HANDOFF, T.HANDOFF_REPEATED_FALLBACK)


def _digest_t184(res) -> str:
    blob = json.dumps({
        "reply_text": res.reply_text, "tenant_id": res.tenant_id,
        "conversation_id": res.conversation_id, "turn_id": res.turn_id,
        "route": res.route, "intent": res.intent, "draft": res.draft.to_json(),
        "handoff": res.handoff.to_json() if res.handoff is not None else None,
        "handoff_reason": res.handoff_reason, "check": res.check.to_json(),
        "lookup_outcome": res.lookup_outcome, "ask_slot": res.ask_slot, "lang": res.lang,
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def p12_conversations_t184() -> tuple[tuple[str, ...], ...]:
    """本文件全部说法（单句与多轮），p12 路径逐字节对照用。"""
    singles = (C1_SAYINGS_T184 + C1_ANTI_T184 + C2_WITH_NO_T184 + C2_NO_NUMBER_T184 + C2_ANTI_T184
               + C4_SAYINGS_T184 + C4_ANTI_T184 + C3_ANTI_T184 + ("付款一直失败", "地址填错了想改一下")
               + C2_INDIRECT_T184 + C2_PRODUCT_ANTI_T184 + C1_REROUTE_ANTI_T184
               + TOPIC_DEHUA_T184 + COND_FIRST_T184 + NEG_LOOKUP_T184
               + (f"请问{ORDER_T184}的话多久能到", "退货的话运费谁出"))
    multi = (tuple(first + (last,) for first, last in C3_EXHAUSTED_T184)
             + tuple(("帮我查物流", "查一下嘛", t) for t in C3_SWITCH_T184 + C3_DROP_T184)
             + tuple(("帮我查物流", t) for t in C3_STILL_T184)
             + tuple((f"帮我查下{ORDER_T184}到哪了", t) for t in C1_REROUTE_ANTI_T184)
             + (("帮我查物流", "不记得了", "你们发什么快递"),))
    return tuple((t,) for t in singles) + multi


def p12_fingerprint_t184() -> str:
    """p12 路径（无端口）跑 :func:`p12_conversations_t184` 每一轮的 DeskResult 指纹。"""
    h = hashlib.sha256()
    for i, conv in enumerate(p12_conversations_t184()):
        talk = _Talk_t184(ports=False, user=f"wm_t184_p12_{i:03d}")
        assert not talk.desk.ports_injected
        for text in conv:
            h.update(_digest_t184(talk.say(text)).encode())
    return h.hexdigest()


#: 基线 7c19a1d 的 desk.py 跑 :func:`p12_fingerprint_t184` 的结果。原值 72a7e736…（T184 分支上、
#: 其余代码同基线时实测）。整合期 p15 并入 T186（词法零命中时的近邻兜底，p12 路径同样生效，契约本意）之后
#: 整体读数变了；主会话在整合后的代码上把 desk.py 换回 7c19a1d 原样再跑一次，得到的指纹与当前 desk.py
#: **逐字相同**（cfd2e407…）—— 即 T184 的判定改动在 p12 路径上仍是零变化，变化全来自检索层。于是改钉整合后的值。
P12_GOLDEN_T184 = "cfd2e407f6762505bfff6dc5299fc9dc4107c2aff2ba6802792cf813af581ee6"


def test_without_ports_every_turn_is_byte_identical_to_baseline_t184():
    assert p12_fingerprint_t184() == P12_GOLDEN_T184
