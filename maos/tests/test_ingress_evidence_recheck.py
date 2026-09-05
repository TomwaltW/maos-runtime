"""拖一张图就能激活五岗 —— 判单四级与证据复检。

这一组钉的是一件事：**一张照片自己不带订单号，凭什么知道它是哪一单的**。

判单的四级优先（文件名 -> 正文 -> 本会话最近的待办 -> 都不算）顺序不可调换，
而第 4 级「都不算」返回空**不是失败**：随便挑一单挂上去，等于凭空造了一条
「这张图属于这一单」的事实（铁律 8），而挂错之后没有任何一条记录能解释清楚。
所以这里有两条同样重要的测试 —— 一条钉「认出来了就演一场好戏」，
一条钉「认不出来就一个岗都不叫、回帖与从前逐字一致」。

零网络、零模型、零 Matrix：附件字节是本文件里生成的真 PNG，圆桌全是假件。
"""

from __future__ import annotations

import pytest

from maos.core.store import SqliteStore
from maos.ingress.attachments import AttachmentBuffer, AttachmentStore, digest_of
from maos.ingress.contracts import CHANNEL_FEISHU, Attachment, InboundMessage
from maos.ingress.router import IngressRouter, KNOWN_VERBS
from maos.tests.test_ingress_attachments import PNG_1PX

ORDER = "ORD-2026-0004"
CASE = f"RC-{ORDER}"
OTHER = "ORD-2026-0005"
GHOST = "ORD-9999-9999"                             # 底账里没有这一单
ALICE = "ou_alice"
BOB = "ou_bob"
CHAT = "oc_1"


def png(tag: int) -> bytes:
    """一张 digest 各不相同的真 PNG。

    改的是**最后一个字节**（IEND 的 CRC 尾巴）而不是头 8 字节：类型嗅探只看头，
    所以这几张仍然是合法的 image/png，但内容寻址下它们是三份不同的证据。
    范式与 `test_ingress_attachments.py` 里造多份暂存那段一致。
    """
    return PNG_1PX[:-1] + bytes([tag])


class FakeAdapter:
    """按 file_key 发字节的取件件；顺带记下发出去的回帖。"""

    configured = True
    name = CHANNEL_FEISHU

    def __init__(self, blobs: dict[str, bytes]) -> None:
        self.blobs = blobs
        self.sent: list = []

    def fetch(self, att: Attachment) -> bytes:
        return self.blobs[att.file_key]

    def send(self, msg) -> None:
        self.sent.append(msg)


class FakeTeam:
    """圆桌假件（**老签名**：不接 `round_no` / `added_evidence`）。

    刻意逐个列出参数而不是收 ``**kw``：`_accepted_extra` 就是靠签名判断
    「这一层收不收得下新参」的，写成 ``**kw`` 会让这条测试测不到它想测的东西。
    """

    def __init__(self, *, boom: str = "") -> None:
        self.calls: list[dict] = []
        self.boom = boom

    def on_preflight(self, *, payload, checked, ledger, evidence, requested_by):
        self.calls.append({"kind": "preflight", "payload": payload, "checked": checked,
                           "evidence": evidence, "requested_by": requested_by})
        if self.boom == "preflight":
            raise RuntimeError("preflight 钩子炸了")
        return []

    def on_sheet(self, *, rows, ledger, requested_by):
        self.calls.append({"kind": "sheet", "rows": rows})
        return []

    def on_execute(self, **kw):
        self.calls.append({"kind": "execute"})
        return []

    def roster(self) -> list[dict]:
        return []

    def kinds(self) -> list[str]:
        return [c["kind"] for c in self.calls]

    def preflights(self) -> list[dict]:
        return [c for c in self.calls if c["kind"] == "preflight"]


class NewTeam(FakeTeam):
    """**新签名**圆桌（跨轨契约 §3，T99 的件）：两个新参都带默认值。"""

    def on_preflight(self, *, payload, checked, ledger, evidence, requested_by,
                     round_no: int = 1, added_evidence: int = 0):
        self.calls.append({"kind": "preflight", "checked": checked,
                           "evidence": evidence, "round_no": round_no,
                           "added_evidence": added_evidence})
        return []


class Shell:
    """`hiclaw/room_ingress.py::RoomTeam` 那个转发壳的形状：``on_preflight(**kw)``。

    真房间里坐在 router 对面的就是它，所以「圆桌收不收得下新参」这件事，
    问壳永远问到「什么都收」—— 收不收得下由它包着的那一层说了算。
    """

    def __init__(self, inner) -> None:
        self._team = inner

    def on_preflight(self, **kw):
        return self._team.on_preflight(**kw)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_team"), name)


class TakeBuffer(AttachmentBuffer):
    """记录每次 `take` 拿走了哪几个 digest 的**探针**（跨轨契约 §2）。

    T98 并进来之前这里自带一份挑着取的实现 —— 那时 `claim` 还是独立的一段代码。
    T98 到位之后 `claim` 成了 `take` 的薄壳，假件再回调 `claim` 就是
    `claim -> take -> claim` 无限递归（整合轮实测 RecursionError，而症状是
    `/refund` 那条路整条被 `handle` 的 except 吞成「处理失败」，测试里看到的
    却是「复检没发生」——离真正的原因隔着两层）。

    所以现在只记调用、实现一律交给 `super()`：契约件到位之后，假件的职责从
    「替身」退成「探针」。两份挑着取的实现并存，本来就是长歪的开始。
    """

    def __init__(self) -> None:
        super().__init__()
        self.taken: list[set[str]] = []

    def take(self, channel: str, chat_id: str, *, digests=None):
        self.taken.append(set(digests) if digests is not None else set())
        return super().take(channel, chat_id, digests=digests)


# --------------------------------------------------------------------------
# 脚手架
# --------------------------------------------------------------------------
def _store() -> SqliteStore:
    s = SqliteStore(":memory:")
    s.init_schema()
    return s


def _setup(tmp_path, blobs: dict[str, bytes], *, team=None, buffer=None):
    adapter = FakeAdapter(blobs)
    router = IngressRouter(
        {CHANNEL_FEISHU: adapter}, store=_store(),
        attachment_store=AttachmentStore(tmp_path),
        attachment_buffer=buffer if buffer is not None else AttachmentBuffer(),
        approvers=lambda: frozenset({ALICE}),
        # runner 不该被碰到：这一组一次都不放行。真跑的那个在这里等于建 plan。
        runner=lambda payload, **kw: pytest.fail("复检不许触发任何处置"),
        team=team)
    return router, adapter


def _att(file_key: str, filename: str = "") -> Attachment:
    return Attachment(channel=CHANNEL_FEISHU, file_key=file_key, filename=filename)


def _msg(*, text: str = "", atts=(), msg_id: str = "m1",
         sender: str = ALICE, chat_id: str = CHAT) -> InboundMessage:
    return InboundMessage(channel=CHANNEL_FEISHU, chat_id=chat_id, sender=sender,
                          text=text, msg_id=msg_id, attachments=tuple(atts))


def _stored(router, data: bytes, filename: str):
    """直接落一份到库里，用来单测 `locate_order`（它只吃 `StoredAttachment`）。"""
    return router.attachments.put(data, _att("k", filename),
                                  chat_id=CHAT, sender=ALICE)


def _refund(router, order: str = ORDER, *, msg_id: str = "cmd", reason: str = "质量问题"):
    return router.handle(_msg(text=f"/refund {order} {reason}", msg_id=msg_id))


# --------------------------------------------------------------------------
# §1 判单四级 —— 顺序不可调换，先命中先返回
# --------------------------------------------------------------------------
def test_level_1_filename_prefix(tmp_path):
    """第 1 级：文件名前缀是底账里的订单号。"""
    r, _ = _setup(tmp_path, {})
    item = _stored(r, png(1), f"{ORDER}-rust.png")

    assert r.locate_order(_msg(), [item]) == (ORDER, f"按文件名 {ORDER}-rust.png")


def test_level_1_needs_a_separator_after_the_prefix(tmp_path):
    """文件名口径与 `room_team_smoke.order_of` **同一份**：前缀后必须紧跟 `-` 或 `.`。

    这条红了说明两处口径长歪了 —— 而症状是「冒烟里配得上、真房间里配不上」，
    两边都不报错。
    """
    r, _ = _setup(tmp_path, {})
    item = _stored(r, png(1), f"{ORDER}1-rust.png")   # ORD-2026-00041-...

    assert r.locate_order(_msg(), [item]) == ("", "")


def test_level_2_order_id_in_the_text(tmp_path):
    """第 2 级：正文里的订单号。"""
    r, _ = _setup(tmp_path, {})
    item = _stored(r, png(1), "破损.png")

    assert r.locate_order(_msg(text=f"这是 {ORDER} 的照片"), [item]) == (
        ORDER, "按消息里的订单号")


def test_level_2_only_counts_when_the_ledger_has_it(tmp_path):
    """正文里那串**像**订单号不算数，底账里查得到才算。

    不回底账校验的话，`build_case` 会当场抛，而群里看到的是
    「处理失败：RequestSheetError」—— 人刚甩了一张图，这句话对他没有任何用处。
    """
    r, _ = _setup(tmp_path, {})
    item = _stored(r, png(1), "破损.png")

    assert r.locate_order(_msg(text=f"{GHOST} 这单坏了"), [item]) == ("", "")


def test_level_3_latest_open_ticket(tmp_path):
    """第 3 级：本会话最近一条未过期待办。**这一级是这条链路的价值所在** ——
    人的动作是「/refund 起单 -> 看到证据不齐 -> 拖一张图」，不会再打一遍订单号。"""
    r, _ = _setup(tmp_path, {})
    _refund(r)
    item = _stored(r, png(1), "破损.png")

    assert r.locate_order(_msg(), [item]) == (ORDER, f"按本会话最近的待办 {CASE}")


def test_level_3_ignores_other_sessions(tmp_path):
    """别的会话的待办不算：A 群的图挂到 B 群的单子上，这种错不会报错。"""
    r, _ = _setup(tmp_path, {})
    r.handle(_msg(text=f"/refund {ORDER} 质量问题", msg_id="c1", chat_id="oc_A"))
    item = _stored(r, png(1), "破损.png")

    assert r.locate_order(_msg(chat_id="oc_B"), [item]) == ("", "")


def test_level_4_gives_up(tmp_path):
    """第 4 级：都不命中就返回空 —— 不挑一单凑上去（铁律 8）。"""
    r, _ = _setup(tmp_path, {})
    item = _stored(r, png(1), "破损.png")

    assert r.locate_order(_msg(text="这个怎么退"), [item]) == ("", "")


def test_filename_wins_over_text_and_ticket(tmp_path):
    """三级同时能命中不同的单时，**文件名赢**。顺序调换了这条会红。"""
    r, _ = _setup(tmp_path, {})
    _refund(r, OTHER, msg_id="c1")                    # 第 3 级会指向 0005
    item = _stored(r, png(1), f"{ORDER}-rust.png")

    assert r.locate_order(_msg(text=f"顺便看下 {OTHER}"), [item])[0] == ORDER


def test_text_wins_over_ticket(tmp_path):
    """第 2 级压第 3 级：人在正文里点了名，就不该按「最近那一单」猜。"""
    r, _ = _setup(tmp_path, {})
    _refund(r, OTHER, msg_id="c1")
    item = _stored(r, png(1), "破损.png")

    assert r.locate_order(_msg(text=f"这是 {ORDER} 的"), [item]) == (
        ORDER, "按消息里的订单号")


def test_ghost_order_in_text_falls_back_to_the_ticket(tmp_path):
    """正文里那串底账没有 -> 退回第 3 级，不抛、不回「处理失败」（第 5 步第 9 条）。"""
    r, ad = _setup(tmp_path, {"k1": png(1)})
    _refund(r, msg_id="c1")
    reply = r.handle(_msg(text=f"{GHOST} 补张图", atts=[_att("k1", "破损.png")],
                          msg_id="m2"))

    assert "处理失败" not in reply
    assert f"已挂到 {ORDER}（按本会话最近的待办 {CASE}）" in reply


# --------------------------------------------------------------------------
# 认不出来的时候：行为与从前逐字一致，一个岗都不叫
# --------------------------------------------------------------------------
def test_unlocated_evidence_reply_is_unchanged_and_wakes_nobody(tmp_path):
    """第 4 级的全部行为：暂存 + 原话提示 + **五岗零发言**。

    这条是整组里最该盯住的一条 —— 它红了说明「认不出就不触发」这条边界破了，
    而破了之后的症状是房间里五个岗位排队对着一张没主的图说三句废话，
    或者更糟：这张图被挂到了某一单上，而没有任何一条记录能解释为什么是那一单。
    """
    blob = png(1)
    team = FakeTeam()
    r, _ = _setup(tmp_path, {"k1": blob}, team=team)

    reply = r.handle(_msg(atts=[_att("k1", "破损.png")]))

    assert reply == (
        "已收下 1 份证据（暂存 30 分钟，等一条 /refund 认领）：\n"
        f"  · 破损.png（image/png，{len(blob) // 1024} KB，"
        f"sha256:{digest_of(blob)[:12]}）\n"
        "这个会话现有 1 份待认领证据。接着发：/refund <订单号> <诉求类型>"
    )
    assert team.calls == []
    assert len(r.pending_evidence.peek(CHANNEL_FEISHU, CHAT)) == 1
    assert r._tickets == {}


def test_no_ticket_and_no_reason_does_not_fire(tmp_path):
    """文件名认得出单、但没有待办、正文里也认不出诉求类型 -> **不触发**。

    诉求类型是人的诉求，不是订单属性，底账里查不出来。默认一个「质量问题」
    = 编一条权威事实（铁律 8）。宁可不触发。
    """
    team = FakeTeam()
    r, _ = _setup(tmp_path, {"k1": png(1)}, team=team)

    reply = r.handle(_msg(atts=[_att("k1", f"{ORDER}-rust.png")]))

    assert "已挂到" not in reply and "复检 ·" not in reply
    assert "等一条 /refund 认领" in reply
    assert team.calls == [] and r._tickets == {}


def test_reason_in_the_text_opens_a_case_without_a_ticket(tmp_path):
    """正文里写了诉求类型就够了：文件名判单 + 正文认诉求 -> 现建一单复检。

    回执头三行**逐字**钉住：命中之后那句「暂存 30 分钟，等一条 /refund 认领」
    要换掉，但每一份的文件名 / 类型 / 体积 / digest 明细一行都不许少 ——
    人的疑问是「我刚发的那张挂上了吗」，明细行是唯一能回答它的地方。
    """
    blob = png(1)
    team = FakeTeam()
    r, _ = _setup(tmp_path, {"k1": blob}, team=team)

    reply = r.handle(_msg(text="这单坏了，给退了吧",
                          atts=[_att("k1", f"{ORDER}-rust.png")]))

    assert reply.split("\n\n")[0] == (
        "已收下 1 份证据：\n"
        f"  · {ORDER}-rust.png（image/png，{len(blob) // 1024} KB，"
        f"sha256:{digest_of(blob)[:12]}）\n"
        f"已挂到 {ORDER}（按文件名 {ORDER}-rust.png），随案证据 0 → 1 份"
    )
    assert f"复检 · {ORDER}（坏了） · 案子 {CASE}" in reply
    assert len(team.preflights()) == 1
    assert r._tickets[CASE].round_no == 1             # 现建的是第 1 轮，不是第 2 轮


def test_longest_reason_word_wins():
    """诉求词按长度从长到短扫：一个词是另一个的前缀时，**长的那个赢**。

    今天词表里的包含关系（「七天无理由」⊃「无理由」）恰好同一个 code，所以扫错了
    也看不出来 —— 这条守的是往里加词那一天：加一个 code 不同的短词进去，
    短的先命中就把政策套错了一条，而认错诉求类型不报错。

    词表本身**借** `run_requests.REASONS`，不另列一份中文说法。
    """
    from maos.ingress.router import _load_run_requests, _reason_in

    rr = _load_run_requests()
    assert _reason_in("七天无理由") == "七天无理由"          # 不是「无理由」
    assert rr.REASONS[_reason_in("这单坏了，给退了吧")] == "quality_defect"
    assert _reason_in("wrong_item") == "wrong_item"           # 直接写 code 也认
    assert _reason_in("今天午饭吃什么") == ""


# --------------------------------------------------------------------------
# 复检本身：证据叠加、待办覆盖、一条消息一轮
# --------------------------------------------------------------------------
def _stack(tmp_path, team=None, buffer=None):
    """起一单 + 挂一张图，返回 router。用来做「再补一张」的起点。"""
    r, _ = _setup(tmp_path, {"a": png(1), "b": png(2), "c": png(3), "d": png(4)},
                  team=team, buffer=buffer)
    r.handle(_msg(atts=[_att("a", "第一张.png")], msg_id="m1"))   # 不命中 -> 暂存
    _refund(r)                                                    # 认领那一张
    return r


def test_evidence_stacks_instead_of_replacing(tmp_path):
    """再补一张 -> 案子里是 2 条，编号 ev-01/ev-02，digest 不重复。

    **叠加不是替换**：补的那张替掉前面的，等于每补一次就丢一次，
    而案子里少了哪张没有任何一处会报出来。
    """
    r = _stack(tmp_path)
    reply = r.handle(_msg(atts=[_att("b", "第二张.png")], msg_id="m2"))

    ticket = r._tickets[CASE]
    rows = ticket.payload["customer_evidence"]
    assert [row["evidence_id"] for row in rows] == ["ev-01", "ev-02"]
    assert [row["digest"] for row in rows] == [digest_of(png(1)), digest_of(png(2))]
    assert len(ticket.evidence) == 2
    assert "随案证据 1 → 2 份" in reply


def test_same_photo_again_does_not_stack(tmp_path):
    """同一张图重拖：digest 相同 -> 仍是 2 条，不是 3 条。"""
    r = _stack(tmp_path)
    r.handle(_msg(atts=[_att("b", "第二张.png")], msg_id="m2"))
    reply = r.handle(_msg(atts=[_att("b", "又发了一遍.png")], msg_id="m3"))

    assert len(r._tickets[CASE].payload["customer_evidence"]) == 2
    assert "随案证据 2 → 2 份" in reply


def test_three_photos_are_one_round(tmp_path):
    """一条消息带三张图 = **一次**复检、五岗各说一次，不是三轮 15 条（契约 §4）。"""
    team = FakeTeam()
    r = _stack(tmp_path, team=team)
    r.handle(_msg(atts=[_att("b", "b.png"), _att("c", "c.png"), _att("d", "d.png")],
                  msg_id="m2"))

    assert team.kinds() == ["preflight", "preflight"]   # 起单一轮 + 复检一轮
    assert len(team.preflights()[-1]["evidence"]) == 4  # 起单那张 + 这条消息的三张
    assert len(r._tickets[CASE].payload["customer_evidence"]) == 4


def test_recheck_replaces_the_ticket_not_adds_one(tmp_path):
    """复检后同一 case_id 只有一条待办，且 `checked` 是新算的。"""
    r = _stack(tmp_path)
    before = r._tickets[CASE].checked
    r.handle(_msg(atts=[_att("b", "b.png")], msg_id="m2"))

    assert list(r._tickets) == [CASE]
    ticket = r._tickets[CASE]
    assert ticket.checked is not before and ticket.checked["case_id"] == CASE
    assert ticket.round_no == 2


def test_recheck_keeps_the_original_requester(tmp_path):
    """别人补一张图不会把待办的归属抢走 —— 该看结论的仍是起单的那个人。"""
    r = _stack(tmp_path)
    r.handle(_msg(atts=[_att("b", "b.png")], msg_id="m2", sender=BOB))

    assert r._tickets[CASE].requested_by == ALICE


def test_recheck_does_not_move_money(tmp_path):
    """复检是**只读**的：runner 一次都不许被碰（碰了 `_setup` 里那个会当场 fail）。"""
    r = _stack(tmp_path)
    reply = r.handle(_msg(atts=[_att("b", "b.png")], msg_id="m2"))

    assert "（本条为只读预检，尚未动任何资金）" in reply
    assert f"/approve {CASE}" in reply                 # 放行仍要人来一句


def test_recheck_card_never_claims_the_evidence_is_complete(tmp_path):
    """措辞受铁律 8 约束：齐不齐是证据核验岗的结论，这一层一岗都还没发言。"""
    r = _stack(tmp_path, team=FakeTeam())
    reply = r.handle(_msg(atts=[_att("b", "b.png")], msg_id="m2"))

    for forbidden in ("证据已齐", "证据齐", "可以退款", "已退款", "已到账"):
        assert forbidden not in reply
    assert "五岗正在复检，稍后给出批复建议" in reply


def test_recheck_card_reuses_the_preflight_wording(tmp_path):
    """复检卡与预检卡**只差抬头那两个字与末尾那句** —— 中间四行逐字一致。

    补一张图不会让政策变松，两张卡说的必须是同一套话。
    """
    r = _stack(tmp_path)
    first = r._tickets[CASE]
    fresh = r._render_preflight(first.checked, first)
    again = r._render_preflight(first.checked, first, recheck=True)

    assert fresh.startswith("预检 · ") and again.startswith("复检 · ")
    assert fresh.split("\n")[1:] == again.split("\n")[1:]


# --------------------------------------------------------------------------
# 跨轨兼容：§2 的 take、§3 的两个新参
# --------------------------------------------------------------------------
def test_claimed_evidence_leaves_the_buffer(tmp_path):
    """挂上案子的那几份要从暂存里走掉 —— 留着会被下一句 `/refund ORD-B` 再挂一遍。"""
    r = _stack(tmp_path)
    r.handle(_msg(atts=[_att("b", "b.png")], msg_id="m2"))

    assert r.pending_evidence.peek(CHANNEL_FEISHU, CHAT) == []


def test_take_only_removes_what_was_used(tmp_path):
    """§2 到位之后：只取走用掉的那几个 digest，别人的暂存不动。"""
    buf = TakeBuffer()
    r = _stack(tmp_path, buffer=buf)
    r.handle(_msg(atts=[_att("c", "c.png")], msg_id="m0", chat_id="oc_other"))
    r.handle(_msg(atts=[_att("b", "b.png")], msg_id="m2"))

    assert buf.taken[-1] == {digest_of(png(2))}
    assert len(buf.peek(CHANNEL_FEISHU, "oc_other")) == 1   # 别的会话没被清
    assert buf.peek(CHANNEL_FEISHU, CHAT) == []


def test_old_signature_team_still_gets_a_recheck(tmp_path):
    """§3 的兼容钉子：圆桌只认老签名时，复检**照样发生**，只是不带轮次信息。

    这条红了说明 `_accepted_extra` 把新参硬塞给了老签名 —— 那会当场 TypeError、
    落进 `_fire` 的 except，症状是房间里一片安静，五岗一句话都没有。
    """
    team = FakeTeam()
    r = _stack(tmp_path, team=team)
    reply = r.handle(_msg(atts=[_att("b", "b.png")], msg_id="m2"))

    assert len(team.preflights()) == 2
    assert set(team.calls[-1]) == {"kind", "payload", "checked", "evidence",
                                   "requested_by"}
    assert "复检 · " in reply


def test_new_signature_team_gets_round_and_added(tmp_path):
    """圆桌认新参时，复检那一轮带上 `round_no` 与 `added_evidence`。"""
    team = NewTeam()
    r = _stack(tmp_path, team=team)
    r.handle(_msg(atts=[_att("b", "b.png")], msg_id="m2"))

    last = team.calls[-1]
    assert last["round_no"] == 2 and last["added_evidence"] == 1
    assert team.calls[0]["round_no"] == 1             # 起单那轮走默认值


def test_new_params_reach_through_the_forwarding_shell(tmp_path):
    """壳（``on_preflight(**kw)``）挡不住探测 —— 真房间里坐着的就是这么个壳。

    只问壳的话，问到的永远是「什么都收」，于是老签名圆桌会被硬塞新参；
    而认得下的新签名圆桌又永远收不到 —— 两个方向都错。
    """
    new = NewTeam()
    r = _stack(tmp_path, team=Shell(new))
    r.handle(_msg(atts=[_att("b", "b.png")], msg_id="m2"))
    assert new.calls[-1]["round_no"] == 2

    old = FakeTeam()
    r2 = _stack(tmp_path, team=Shell(old))
    reply = r2.handle(_msg(atts=[_att("b", "b.png")], msg_id="m2"))
    assert len(old.preflights()) == 2 and "复检 · " in reply


def test_team_exception_does_not_change_the_reply(tmp_path, caplog):
    """红线 R4：圆桌炸了只记 WARNING，回帖一个字不变。"""
    quiet = _stack(tmp_path)
    expected = quiet.handle(_msg(atts=[_att("b", "b.png")], msg_id="m2"))

    boom = _stack(tmp_path, team=FakeTeam(boom="preflight"))
    with caplog.at_level("WARNING", logger="maos.ingress.router"):
        angry = boom.handle(_msg(atts=[_att("b", "b.png")], msg_id="m2"))

    assert angry == expected + "\n五岗正在复检，稍后给出批复建议"
    assert "圆桌 preflight 钩子失败" in caplog.text


# --------------------------------------------------------------------------
# 不该被这条链路碰到的两条老路
# --------------------------------------------------------------------------
SHEET_CSV = ("订单号,诉求类型,申报金额,申请日期\n"
             f"{ORDER},质量问题,6800,2026-07-10\n").encode("utf-8")


def test_a_sheet_still_goes_down_the_old_path(tmp_path):
    """拖一张申请表触发的是 ``sheet``，不是 ``preflight``。

    申请表不是证据：不落盘、不暂存，所以这条链路根本看不到它。
    """
    team = FakeTeam()
    r, _ = _setup(tmp_path, {"csv": SHEET_CSV}, team=team)

    reply = r.handle(_msg(atts=[_att("csv", f"{ORDER}-申请表.csv")]))

    assert team.kinds() == ["sheet"]
    assert "已挂到" not in reply and "复检 · " not in reply


def test_refund_with_a_photo_fires_exactly_one_round(tmp_path):
    """图文一条发：``/refund`` 那条老路认领暂存并触发一轮，附件这条不许再来一轮。"""
    team = FakeTeam()
    r, _ = _setup(tmp_path, {"a": png(1)}, team=team)

    reply = r.handle(_msg(text=f"/refund {ORDER} 质量问题",
                          atts=[_att("a", f"{ORDER}-rust.png")]))

    assert team.kinds() == ["preflight"]
    assert "复检 · " not in reply and "预检 · " in reply
    assert len(r._tickets[CASE].evidence) == 1


def test_known_verbs_covers_dispatch():
    """`KNOWN_VERBS` 与 `_dispatch` 认得的命令词同源。

    少列一个词，那条命令带着图进来时会多触发一轮复检（契约 §4），
    而症状是房间里同一条消息两轮五岗发言 —— 不报错。
    """
    from maos.ingress import router as mod

    declared: set[str] = set()
    for name in dir(mod):
        if not name.startswith("CMD_"):
            continue
        value = getattr(mod, name)
        declared |= {value} if isinstance(value, str) else set(value)
    assert declared == set(KNOWN_VERBS)
