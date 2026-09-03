"""闲聊回话：缺省不装（闲聊照旧一声不吭）、装了走真模型且只拿事实、没模型回固定话术。

顺带钉 Matrix 房间的审批面：它是内部审批房，`/approve` 在这里必须能落。
"""

from __future__ import annotations

from maos.core.store import SqliteStore
from maos.ingress.chat import FALLBACK, ChatResponder
from maos.ingress.contracts import CHANNEL_FEISHU, CHANNEL_MATRIX, InboundMessage
from maos.ingress.router import ALLOW_APPROVAL, IngressRouter
from maos.model.client import ModelClient, ModelResponse, ScriptedModelClient
from maos.tests.test_ingress_router import FakeAdapter, Runs

BOSS = "@boss:maos.local"


class _EchoModel(ModelClient):
    """把收到的 system / user 记下来，回一句固定的话。"""

    def __init__(self, answer: str = "你好，我是退款助手") -> None:
        self.answer = answer
        self.calls: list[dict] = []
        self.model = "echo-1"

    def complete(self, *, system: str, user: str, tier: str) -> ModelResponse:
        self.calls.append({"system": system, "user": user, "tier": tier})
        return ModelResponse(text=self.answer, model=self.model)


class _BrokenModel(ModelClient):
    model = "broken"

    def complete(self, *, system: str, user: str, tier: str) -> ModelResponse:
        raise RuntimeError("模型网关返回 HTTP 502：bad gateway")


def _store() -> SqliteStore:
    s = SqliteStore(":memory:")
    s.init_schema()
    return s


def _msg(text: str, *, channel: str = CHANNEL_FEISHU, sender: str = "ou_alice",
         msg_id: str = "m1") -> InboundMessage:
    return InboundMessage(channel=channel, chat_id="c1", sender=sender, text=text,
                          msg_id=msg_id)


# --------------------------------------------------------------------------
# ChatResponder 本身
# --------------------------------------------------------------------------
def test_scripted_model_counts_as_no_model():
    """假模型未命中脚本返回 '{}'，房间里刷一句 '{}' 比不回还糟 —— 不用它。"""
    r = ChatResponder(ScriptedModelClient())
    assert r.live is False
    assert r.reply("maos", facts="x") == FALLBACK
    assert "固定话术" in r.describe()


def test_live_model_gets_facts_and_the_message():
    model = _EchoModel()
    r = ChatResponder(model)
    assert r.live and "echo-1" in r.describe()

    out = r.reply("  maos  ", facts="底账里有 ORD-2026-0001")
    assert out == "你好，我是退款助手"
    call, = model.calls
    assert "【事实】\n底账里有 ORD-2026-0001" in call["user"]
    assert call["user"].endswith("【群里的人说】\nmaos")
    assert "只依据【事实】说话" in call["system"]
    assert "不能承诺退款结果" in call["system"]


def test_gateway_failure_falls_back_instead_of_silence(caplog):
    r = ChatResponder(_BrokenModel())
    with caplog.at_level("WARNING", logger="maos.ingress.chat"):
        out = r.reply("在吗", facts="")
    assert out == FALLBACK
    assert "退回固定话术" in caplog.text and "502" in caplog.text


def test_empty_model_answer_falls_back():
    assert ChatResponder(_EchoModel("   ")).reply("在吗", facts="") == FALLBACK


# --------------------------------------------------------------------------
# router 接线
# --------------------------------------------------------------------------
def test_router_without_chat_stays_silent_on_chatter():
    ad = FakeAdapter()
    r = IngressRouter({ad.name: ad}, store=_store(), runner=Runs())
    assert r.handle(_msg("maos")) == ""
    assert ad.sent == []


def test_router_with_chat_answers_chatter_with_facts():
    ad = FakeAdapter()
    model = _EchoModel("底账里有三张订单，发 /refund 起单")
    r = IngressRouter({ad.name: ad}, store=_store(), runner=Runs(),
                      approvers=lambda: frozenset({BOSS}), chat=ChatResponder(model))
    r.handle(_msg("/refund ORD-2026-0001 质量问题", msg_id="m0"))

    out = r.handle(_msg("maos", msg_id="m1", sender=BOSS))

    assert out == "底账里有三张订单，发 /refund 起单"
    assert ad.sent[-1].text == out
    facts = model.calls[-1]["user"]
    assert "ORD-2026-0001" in facts and "ORD-2026-0003" in facts     # 底账订单
    assert "/refund <订单号>" in facts                                # 命令面
    assert "RC-ORD-2026-0001" in facts and "待放行" in facts            # 本会话待办
    assert f"说话的人：{BOSS}（在审批人名单内）" in facts
    assert "还没收到过申请表" in facts


def test_chat_never_takes_over_commands():
    """命令永远走命令面，回话器一个字都不插。"""
    ad = FakeAdapter()
    model = _EchoModel()
    r = IngressRouter({ad.name: ad}, store=_store(), runner=Runs(), chat=ChatResponder(model))
    out = r.handle(_msg("/help"))
    assert "refund" in out and model.calls == []
    assert r.handle(_msg("/deploy prod", msg_id="m2")) == "" and model.calls == []


def test_chat_exception_does_not_break_the_message():
    class Exploding:
        def reply(self, text, *, facts):
            raise RuntimeError("回话器炸了")

    ad = FakeAdapter()
    r = IngressRouter({ad.name: ad}, store=_store(), runner=Runs(), chat=Exploding())
    assert r.handle(_msg("在吗")) == ""


# --------------------------------------------------------------------------
# Matrix 房间是内部审批房
# --------------------------------------------------------------------------
def test_matrix_room_is_an_internal_channel_for_approval():
    assert CHANNEL_MATRIX in ALLOW_APPROVAL


def test_approver_in_matrix_room_can_release_a_ticket():
    runs = Runs()
    ad = FakeAdapter(CHANNEL_MATRIX)
    r = IngressRouter({ad.name: ad}, store=_store(), runner=runs,
                      approvers=lambda: frozenset({BOSS}))
    r.handle(_msg("/refund ORD-2026-0001 质量问题", channel=CHANNEL_MATRIX, msg_id="a"))
    out = r.handle(_msg("/approve RC-ORD-2026-0001", channel=CHANNEL_MATRIX,
                        sender=BOSS, msg_id="b"))
    assert "已放行 RC-ORD-2026-0001" in out and len(runs) == 1


def test_outsider_in_matrix_room_is_still_denied():
    runs = Runs()
    ad = FakeAdapter(CHANNEL_MATRIX)
    r = IngressRouter({ad.name: ad}, store=_store(), runner=runs,
                      approvers=lambda: frozenset({BOSS}))
    r.handle(_msg("/refund ORD-2026-0001 质量问题", channel=CHANNEL_MATRIX, msg_id="a"))
    out = r.handle(_msg("/approve RC-ORD-2026-0001", channel=CHANNEL_MATRIX,
                        sender="@intern:maos.local", msg_id="b"))
    assert "无审批权限" in out and runs == []


# --------------------------------------------------------------------------
# 圆桌名单进【事实】（T88）
# --------------------------------------------------------------------------
def test_chat_facts_include_the_roster_when_a_team_is_wired():
    """接了圆桌，模型才有资格答「你们那边都有谁」—— 答案只能来自这一段。"""
    from maos.tests.test_ingress_router import FakeTeam

    ad = FakeAdapter()
    model = _EchoModel()
    r = IngressRouter({ad.name: ad}, store=_store(), runner=Runs(),
                      chat=ChatResponder(model), team=FakeTeam())
    r.handle(_msg("你们那边都有谁"))

    facts = model.calls[-1]["user"]
    assert "圆桌岗位与技能：" in facts
    assert "申请受理岗（refund-intake）" in facts and "refund.intake@1.0.0" in facts
    assert "第 6 条" not in facts                       # 规矩在 system 里，不混进事实
    assert "有哪些岗位" in model.calls[-1]["system"]     # 第 6 条确实在系统提示里


def test_chat_facts_omit_the_roster_without_a_team():
    """没接圆桌就一个字都不给：模型看不见的东西才编不出来（铁律 8）。"""
    ad = FakeAdapter()
    model = _EchoModel()
    r = IngressRouter({ad.name: ad}, store=_store(), runner=Runs(),
                      chat=ChatResponder(model))
    r.handle(_msg("你们那边都有谁"))

    facts = model.calls[-1]["user"]
    assert "圆桌岗位与技能" not in facts and "refund-intake" not in facts
