"""入站附件 —— 从「群里发了张照片」到「案子里挂着一条可校验的证据」。

买的是这一整条：飞书回调解出取件凭据 -> adapter 拉字节 -> 白名单与体积闸 ->
内容寻址落盘 -> 按会话暂存 -> 一句 `/refund` 认领 -> 进 `payload["customer_evidence"]`
-> 由 `refund.intake` 落库。任何一节断掉的症状都长得一样：**群里发了图，
什么都没发生**，而证据其实（或其实没有）存下来了 —— 这类静默是本文件的主要靶子。
"""

from __future__ import annotations

import json
import time

import pytest

from maos.ingress.attachments import (
    AttachmentBuffer, AttachmentStore, AttachmentTooLarge, AttachmentTypeRejected,
    digest_of, digest_from_uri, sniff_mime, uri_of,
)
from maos.ingress.contracts import (
    CHANNEL_FEISHU, Attachment, AttachmentUnsupported, InboundMessage, WebhookRequest,
)
from maos.ingress.feishu import FeishuAdapter, FeishuConfig

# 一张 1x1 的真 PNG。用真字节而不是 b"fake"，因为落盘那一步会**嗅探内容**，
# 假字节测不到类型闸，而类型闸正是这条链路上唯一挡得住 .exe 的东西。
PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6360000002000100"
    "05fe02fea7c2b1000000000049454e44ae426082"
)
JPEG_HEAD = b"\xff\xd8\xff\xe0" + b"\x00" * 64


# --------------------------------------------------------------------------
# 存储：内容寻址
# --------------------------------------------------------------------------
def _att(kind: str = "image", **kw) -> Attachment:
    return Attachment(channel=CHANNEL_FEISHU, file_key="img_v3_x", kind=kind, **kw)


def test_same_bytes_stored_once_and_digest_is_the_filename(tmp_path):
    """同一张图重复进来只落一份，且 `digest` 与内容必然一致。

    这是 `customer_evidence.digest` 那一列能拿来**校验**的前提。按 message_id 存
    的话，平台重推 + 两个群各发一次会得到三份字节、三个 evidence_id，而它们
    指的是同一张图 —— 事后没有任何一条记录能把这件事解释清楚。
    """
    store = AttachmentStore(tmp_path)
    a = store.put(PNG_1PX, _att())
    b = store.put(PNG_1PX, _att(filename="又发了一遍.png"))

    assert a.digest == b.digest == digest_of(PNG_1PX)
    assert a.uri == uri_of(a.digest) and digest_from_uri(a.uri) == a.digest
    assert store.exists(a.digest) and store.read(a.digest) == PNG_1PX
    # 落盘的文件总数是 1，不是 2。
    assert len(list(tmp_path.rglob("*"))) == len([p for p in tmp_path.rglob("*") if p.is_dir()]) + 1


def test_oversized_and_wrong_type_are_rejected_not_stored(tmp_path):
    """超限与类型不符**一律不落盘**。收下再说是这一层最贵的错 —— 输入来自公网。"""
    store = AttachmentStore(tmp_path, max_bytes=100)
    with pytest.raises(AttachmentTooLarge):
        store.put(b"x" * 101, _att())

    big = AttachmentStore(tmp_path)
    with pytest.raises(AttachmentTypeRejected):
        # 平台**自报** image/png，内容却是个 ELF。嗅探优先于自报，正是为了这个。
        big.put(b"\x7fELF" + b"\x00" * 32, _att(mime="image/png"))
    with pytest.raises(AttachmentTypeRejected):
        big.put(b"", _att())
    assert not list(tmp_path.rglob("*.png"))


def test_sniff_only_trusts_content():
    """判类型只看内容。**判不出返回空**，不退回平台自报值 —— 那是白名单的洞。"""
    assert sniff_mime(PNG_1PX) == "image/png"
    assert sniff_mime(JPEG_HEAD) == "image/jpeg"
    assert sniff_mime(b"%PDF-1.7\n") == "application/pdf"
    assert sniff_mime(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert sniff_mime(b"\x00\x00\x00\x18ftypheic") == "image/heic"
    assert sniff_mime(b"\x00\x01\x02") == ""


def test_pdf_becomes_document_kind_not_mime(tmp_path):
    """`kind` 进的是政策判据（AS-003 认 image/document），写 mime 会匹配不上。"""
    stored = AttachmentStore(tmp_path).put(b"%PDF-1.7\n" + b"x" * 32, _att(kind="file"))
    assert stored.kind == "document"
    assert AttachmentStore(tmp_path).put(PNG_1PX, _att()).kind == "image"


# --------------------------------------------------------------------------
# 暂存：照片自己不带订单号
# --------------------------------------------------------------------------
def _stored(store: AttachmentStore, data: bytes, chat_id: str, sender: str = "ou_a"):
    return store.put(data, _att(), chat_id=chat_id, sender=sender)


def test_buffer_is_scoped_by_channel_and_chat(tmp_path):
    """A 群的图不许挂到 B 群的单子上，跨渠道同名 chat_id 也不许串。"""
    store = AttachmentStore(tmp_path)
    buf = AttachmentBuffer()
    buf.add(_stored(store, PNG_1PX, "oc_A"))
    buf.add(_stored(store, JPEG_HEAD + b"a", "oc_B"))

    assert len(buf.peek(CHANNEL_FEISHU, "oc_A")) == 1
    assert len(buf.peek(CHANNEL_FEISHU, "oc_B")) == 1
    assert buf.peek("matrix", "oc_A") == []


def test_claim_empties_the_buffer(tmp_path):
    """取走即清空 —— 不清的话下一单 `/refund` 会把上一单的照片再挂一遍。"""
    store = AttachmentStore(tmp_path)
    buf = AttachmentBuffer()
    buf.add(_stored(store, PNG_1PX, "oc_A"))

    assert len(buf.claim(CHANNEL_FEISHU, "oc_A")) == 1
    assert buf.claim(CHANNEL_FEISHU, "oc_A") == []


def test_buffer_dedups_by_digest_and_expires(tmp_path):
    store = AttachmentStore(tmp_path)
    buf = AttachmentBuffer(ttl=0)
    item = _stored(store, PNG_1PX, "oc_A")
    buf.add(item)
    buf.add(item)                                  # 同一张重发，不叠加
    time.sleep(0.01)
    assert buf.claim(CHANNEL_FEISHU, "oc_A") == []  # ttl=0，全过期

    live = AttachmentBuffer(ttl=600)
    live.add(item)
    live.add(item)
    assert len(live.claim(CHANNEL_FEISHU, "oc_A")) == 1


def test_buffer_caps_how_many_ride_on_one_case(tmp_path):
    """连甩一百张图，不该被一句 /refund 全挂到同一个案子上。"""
    store = AttachmentStore(tmp_path)
    buf = AttachmentBuffer(cap=3)
    for i in range(10):
        buf.add(_stored(store, PNG_1PX[:-1] + bytes([i]), "oc_A"))
    assert len(buf.claim(CHANNEL_FEISHU, "oc_A")) == 3


# --------------------------------------------------------------------------
# 飞书：解出取件凭据
# --------------------------------------------------------------------------
def _adapter() -> FeishuAdapter:
    return FeishuAdapter(FeishuConfig(app_id="cli_x", app_secret="s",
                                      verification_token="vtoken-demo"))


def _event(msg_type: str, content: dict, message_id: str = "om_1") -> WebhookRequest:
    body = {
        "schema": "2.0",
        "header": {"event_id": "evt-1", "event_type": "im.message.receive_v1",
                   "create_time": "1756800000000", "token": "vtoken-demo"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_alice"}},
            "message": {"message_id": message_id, "chat_id": "oc_1",
                        "chat_type": "group", "message_type": msg_type,
                        "content": json.dumps(content, ensure_ascii=False)},
        },
    }
    return WebhookRequest(method="POST", path="/ingress/feishu",
                          body=json.dumps(body, ensure_ascii=False).encode())


def test_image_message_yields_attachment_with_message_id():
    """取件要 ``message_id``，而 `msg_id` 是 ``event_id`` —— 两者不能混。

    混了的症状是 232001「消息不存在」，报错里不会指出是这个字段。
    """
    msg, = _adapter().parse(_event("image", {"image_key": "img_v3_abc"}))
    att, = msg.attachments
    assert att.file_key == "img_v3_abc" and att.kind == "image"
    assert att.msg_ref["message_id"] == "om_1"
    assert msg.msg_id == "evt-1"                   # 去重按事件，取件按消息
    assert msg.text == ""


def test_post_message_picks_up_both_text_and_images():
    """图文混排：一段说明 + 两张照片，两样都要进来。"""
    content = {"title": "破损", "content": [
        [{"tag": "text", "text": "轴承外壳裂了"}, {"tag": "img", "image_key": "img_1"}],
        [{"tag": "img", "image_key": "img_2"},
         {"tag": "media", "file_key": "file_1", "file_name": "质检报告.pdf"}],
    ]}
    msg, = _adapter().parse(_event("post", content))
    assert [a.file_key for a in msg.attachments] == ["img_1", "img_2", "file_1"]
    assert msg.attachments[2].kind == "file"


def test_missing_message_id_yields_no_attachment():
    """取不到 message_id 就不产出凭据 —— 宁可当纯文本，也不给一个取不了件的凭据。

    产出它的后果是先回一句「已收下证据」再在取件时失败：**看起来成功了的失败**。
    """
    msg, = _adapter().parse(_event("image", {"image_key": "img_x"}, message_id=""))
    assert msg.attachments == ()


def test_text_path_is_unchanged():
    """纯文本一行没变 —— 命令面不该被这次改动碰到。"""
    msg, = _adapter().parse(_event("text", {"text": "/refund ORD-1 质量问题"}))
    assert msg.text == "/refund ORD-1 质量问题" and msg.attachments == ()


# --------------------------------------------------------------------------
# router：收图即回执，`/refund` 认领
# --------------------------------------------------------------------------
class _FakeAdapter:
    """按 file_key 交字节。``fail`` 里的 key 抛 —— 取件失败是常态路径，要测。"""

    name = CHANNEL_FEISHU

    def __init__(self, blobs: dict[str, bytes], fail: set[str] | None = None) -> None:
        self.blobs = blobs
        self.fail = fail or set()
        self.sent: list = []

    def fetch(self, att: Attachment) -> bytes:
        if att.file_key in self.fail:
            raise RuntimeError("token 过期")
        return self.blobs[att.file_key]

    def send(self, msg) -> None:
        self.sent.append(msg)


class _FakeStore:
    """只提供 router 用到的那一个方法：幂等认领。"""

    def __init__(self) -> None:
        self.seen: set[str] = set()

    def claim_idempotency(self, key: str, kind: str, payload: str):
        if key in self.seen:
            return {"key": key}
        self.seen.add(key)
        return None


def _router(tmp_path, adapter: _FakeAdapter):
    from maos.ingress.router import IngressRouter

    return IngressRouter({CHANNEL_FEISHU: adapter}, store=_FakeStore(),
                         attachment_store=AttachmentStore(tmp_path),
                         attachment_buffer=AttachmentBuffer())


def _inbound(atts: tuple[Attachment, ...] = (), text: str = "", msg_id: str = "m1"):
    return InboundMessage(channel=CHANNEL_FEISHU, chat_id="oc_1", sender="ou_alice",
                          text=text, msg_id=msg_id, attachments=atts)


def test_photo_alone_gets_a_reply_and_is_buffered(tmp_path):
    """**纯图片必须有回执。** 沉默的症状是「机器人没在听」，而图其实已经存下了。"""
    adapter = _FakeAdapter({"k1": PNG_1PX})
    router = _router(tmp_path, adapter)

    reply = router.handle(_inbound((_att(filename="破损.png").__class__(
        channel=CHANNEL_FEISHU, file_key="k1", filename="破损.png"),)))

    assert "已收下 1 份证据" in reply
    assert "/refund" in reply                       # 回执要说下一步打什么
    assert len(router.pending_evidence.peek(CHANNEL_FEISHU, "oc_1")) == 1


def test_partial_failure_keeps_the_good_ones(tmp_path):
    """三张里坏一张，另外两张照收。逐个独立处理，不是一荣俱荣。"""
    adapter = _FakeAdapter({"ok1": PNG_1PX, "ok2": JPEG_HEAD + b"z"}, fail={"bad"})
    router = _router(tmp_path, adapter)
    atts = tuple(Attachment(channel=CHANNEL_FEISHU, file_key=k, filename=k)
                 for k in ("ok1", "bad", "ok2"))

    reply = router.handle(_inbound(atts))

    assert "已收下 2 份证据" in reply and "未收下 1 份" in reply
    assert "取件失败" in reply                       # 我方问题要说清，别让人去猜
    assert len(router.pending_evidence.peek(CHANNEL_FEISHU, "oc_1")) == 2


def test_rejected_type_says_which_one_and_why(tmp_path):
    adapter = _FakeAdapter({"exe": b"MZ\x90\x00" + b"\x00" * 64})
    router = _router(tmp_path, adapter)

    reply = router.handle(_inbound(
        (Attachment(channel=CHANNEL_FEISHU, file_key="exe", filename="发票.png"),)))

    assert "未收下 1 份" in reply and "发票.png" in reply
    assert router.pending_evidence.peek(CHANNEL_FEISHU, "oc_1") == []


def test_unregistered_channel_does_not_pretend_to_succeed(tmp_path):
    """adapter 没注册时，回执必须说没收下 —— 不许回「已收到」。"""
    from maos.ingress.router import IngressRouter

    router = IngressRouter({}, store=_FakeStore(),
                           attachment_store=AttachmentStore(tmp_path),
                           attachment_buffer=AttachmentBuffer())
    msg = _inbound((Attachment(channel=CHANNEL_FEISHU, file_key="k"),))
    # 断言落在**生成的回执**上而不是 `handle` 的返回值：没有 adapter 就发不出去，
    # `handle` 按既有语义返回 ""（「已发出的回帖」）。这里买的是措辞不撒谎。
    note = router._ingest_attachments(msg)

    assert "已收下" not in note and "未收下 1 份" in note
    assert router.pending_evidence.peek(CHANNEL_FEISHU, "oc_1") == []


def test_image_with_command_does_both(tmp_path):
    """图文都有时两件事都要做 —— 先判命令会把图丢掉。"""
    adapter = _FakeAdapter({"k1": PNG_1PX})
    router = _router(tmp_path, adapter)

    reply = router.handle(_inbound(
        (Attachment(channel=CHANNEL_FEISHU, file_key="k1"),), text="/help"))

    assert "已收下 1 份证据" in reply
    assert "/approve" in reply                      # USAGE 也一起回了


def test_duplicate_delivery_stores_nothing_twice(tmp_path):
    """平台重推同一条带图消息，不许在暂存里变成两份。"""
    adapter = _FakeAdapter({"k1": PNG_1PX})
    router = _router(tmp_path, adapter)
    msg = _inbound((Attachment(channel=CHANNEL_FEISHU, file_key="k1"),), msg_id="same")

    router.handle(msg)
    assert router.handle(msg) == ""                 # 第二次被幂等挡掉
    assert len(router.pending_evidence.peek(CHANNEL_FEISHU, "oc_1")) == 1


def test_refund_claims_buffered_evidence_into_payload(tmp_path, monkeypatch):
    """一句 `/refund` 把暂存的照片挂进 ``payload["customer_evidence"]``。

    断言落在 payload 而不是库表上：那一列的**唯一**写入路径是
    `refund.intake`（见 `domain/refund/fixtures.py::evidence_signals_of`），
    router 只负责把证据递到那条路的入口。绕开它自己 INSERT 会得到两条落库路径。
    """
    from maos.ingress import router as R

    adapter = _FakeAdapter({"k1": PNG_1PX, "k2": JPEG_HEAD + b"q"})
    router = _router(tmp_path, adapter)
    router.handle(_inbound(
        tuple(Attachment(channel=CHANNEL_FEISHU, file_key=k) for k in ("k1", "k2")),
        msg_id="photos"))

    # 把预检与底账换掉：本条买的是「证据有没有挂上去」，不是退款裁定本身。
    monkeypatch.setattr(R, "preflight", lambda payload: {
        "case_id": "RC-ORD-1", "decision": "approve", "why": "在窗口内",
        "deciding_rule": "AS-001", "pinned_policy_version": 1, "elapsed_days": 3,
        "amount_claimed": 100.0, "approver_role": "主管",
    })
    monkeypatch.setattr(type(router), "ledger", lambda self: {})
    monkeypatch.setattr(R, "_load_run_requests", lambda: _FakeRunRequests())

    reply = router.handle_refund(_inbound(text="/refund ORD-1 质量问题"),
                                 ["ORD-1", "质量问题"])

    ticket = router._tickets["RC-ORD-1"]
    rows = ticket.payload["customer_evidence"]
    assert [r["evidence_id"] for r in rows] == ["ev-01", "ev-02"]
    assert all(r["uri"].startswith("maos-attachment://") for r in rows)
    assert rows[0]["digest"] == digest_of(PNG_1PX)  # digest 是内容算的，不是占位
    assert rows[0]["kind"] == "image"
    assert "随案证据：2 份" in reply                 # 人要能看到挂上了几张
    # 认领之后暂存清空 —— 下一单不会把这两张再挂一遍。
    assert router.pending_evidence.peek(CHANNEL_FEISHU, "oc_1") == []


class _FakeRunRequests:
    """`scripts/run_requests.py` 的最小替身，只覆盖 `handle_refund` 会碰的四个面。"""

    RequestSheetError = ValueError
    DECISION_CN = {"approve": "批准"}

    @staticmethod
    def _reason_code(raw: str) -> str:
        return "quality"

    @staticmethod
    def _iso(raw: str) -> str:
        return "2026-09-02"

    @staticmethod
    def build_case(ledger: dict, req: dict) -> dict:
        return {"case": {"case_id": "RC-ORD-1", "order_id": req["order_id"]}}


def test_adapter_without_fetch_raises_unsupported():
    """没实现取件的渠道要**抛**，不许返回空字节。空字节会落成一条指向空气的证据。"""
    from maos.ingress.contracts import ChannelAdapter

    class Bare:
        name = "wecom"
        fetch = ChannelAdapter.fetch

    with pytest.raises(AttachmentUnsupported):
        Bare().fetch(_att())
