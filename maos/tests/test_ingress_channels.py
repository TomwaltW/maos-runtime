"""渠道 adapter 层：签名、解密、解包。

这一层的测试有个特点 —— **大部分断言买的是「不通过」**。功能上「能收到消息」
很容易验，但这一层真正的职责是把不该收的挡在外面，而挡漏了不会有任何症状。
所以每条正向用例后面都跟着若干条反向用例。
"""

from __future__ import annotations

import json
import time

import pytest

from maos.ingress import crypto
from maos.ingress.contracts import (
    ChannelDepMissing, ChannelNotConfigured, VerifyError, WebhookRequest,
)
from maos.ingress.feishu import FeishuAdapter, FeishuConfig
from maos.ingress.wecom import WeChatKfAdapter, WeComAdapter, WeComCredentials

FEISHU_ENV = {
    "MAOS_FEISHU_APP_ID": "cli_demo",
    "MAOS_FEISHU_APP_SECRET": "secret-demo",
    "MAOS_FEISHU_VERIFICATION_TOKEN": "vtoken-demo",
}
ENCRYPT_KEY = "encrypt-key-demo"

#: 43 位 base64 —— EncodingAESKey 的规定长度。
AES_KEY_43 = "a" * 43

#: 有没有可用的 AES 后端。企微的回调强制加密，没有后端时那几条用例整组跳过；
#: 但「缺后端必须显式抛」那一条**不跳**，它正是要在这台机器上验的东西。
HAS_AES = True
try:
    crypto._aes_backend()
except ChannelDepMissing:
    HAS_AES = False

needs_aes = pytest.mark.skipif(not HAS_AES, reason="没装 cryptography / pycryptodome")


def _feishu(env: dict | None = None, **over) -> FeishuAdapter:
    return FeishuAdapter(FeishuConfig.from_env({**FEISHU_ENV, **(env or {})}), **over)


def _message_event(text: str = "/help", *, chat_id: str = "oc_1",
                   event_id: str = "evt-1") -> dict:
    return {
        "schema": "2.0",
        "header": {"event_id": event_id, "event_type": "im.message.receive_v1",
                   "create_time": "1756800000000", "token": "vtoken-demo"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_alice"}, "sender_type": "user"},
            "message": {"message_id": "om_1", "chat_id": chat_id, "chat_type": "group",
                        "message_type": "text",
                        "content": json.dumps({"text": text}, ensure_ascii=False)},
        },
    }


def _req(body: dict, **kw) -> WebhookRequest:
    return WebhookRequest(
        method="POST", path="/ingress/feishu",
        body=json.dumps(body, ensure_ascii=False).encode("utf-8"), **kw)


# --------------------------------------------------------------------------
# 飞书 · 明文模式
# --------------------------------------------------------------------------
def test_plain_mode_accepts_matching_token():
    _feishu().verify(_req({"token": "vtoken-demo", "type": "url_verification"}))


def test_plain_mode_rejects_wrong_token():
    with pytest.raises(VerifyError):
        _feishu().verify(_req({"token": "not-it"}))


def test_plain_mode_rejects_missing_token():
    """没有 token 字段**不等于**放行 —— 这是最容易写漏的一条。"""
    with pytest.raises(VerifyError):
        _feishu().verify(_req({"hello": "world"}))


def test_plain_mode_reads_token_from_v2_header():
    """v2 事件推送的 token 在 ``header.token``，只有 URL 验证在顶层。

    只看顶层的症状是「回调地址配得通、真消息全被 401」，而平台后台只显示一个
    失败计数 —— 分不出是哪一类请求。首版就写错在这里。
    """
    body = _message_event()
    assert "token" not in body and body["header"]["token"] == "vtoken-demo"
    _feishu().verify(_req(body))


def test_url_verification_echoes_challenge():
    answer = _feishu().challenge(
        _req({"token": "vtoken-demo", "type": "url_verification", "challenge": "abc123"}))
    assert json.loads(answer) == {"challenge": "abc123"}


def test_non_challenge_returns_none():
    assert _feishu().challenge(_req(_message_event())) is None


def test_parse_extracts_text_and_ids():
    (msg,) = _feishu().parse(_req(_message_event("/refund ORD-1 质量问题")))
    assert msg.text == "/refund ORD-1 质量问题"
    assert (msg.chat_id, msg.sender, msg.msg_id) == ("oc_1", "ou_alice", "evt-1")
    assert msg.dedup_key == "ingress:feishu:evt-1"


def test_parse_strips_at_mention():
    """群里 @机器人 时正文带占位符。不清掉，命令就永远不在行首。"""
    (msg,) = _feishu().parse(_req(_message_event("@_user_1 /help")))
    assert msg.text == "/help"


def test_parse_ignores_non_message_and_unhandled_types():
    """图片**不再**被忽略（它是证据入口），但语音、非消息事件照旧安静丢掉。

    这条原先断言 ``message_type == "image"`` 返回 ``[]``。那个断言随
    `maos/ingress/attachments.py` 一起作废：图片进来是要落成 `customer_evidence`
    的。剩下那半 —— 「不认识的类型不进命令面」—— 仍然要守，所以换成 ``audio``。
    """
    body = _message_event()
    body["event"]["message"]["message_type"] = "audio"
    body["event"]["message"]["content"] = json.dumps({"file_key": "file_v3_x"})
    assert _feishu().parse(_req(body)) == []

    body2 = _message_event()
    body2["header"]["event_type"] = "im.chat.member.bot.added_v1"
    assert _feishu().parse(_req(body2)) == []


def test_dedup_key_prefers_event_id_over_message_id():
    """飞书重推的是**事件**：同一条消息触发两类事件时 message_id 相同、event_id 不同。"""
    a, = _feishu().parse(_req(_message_event(event_id="evt-a")))
    b, = _feishu().parse(_req(_message_event(event_id="evt-b")))
    assert a.dedup_key != b.dedup_key


# --------------------------------------------------------------------------
# 飞书 · 加密模式（签名）
# --------------------------------------------------------------------------
def _signed(body: bytes, key: str = ENCRYPT_KEY, *, ts: str | None = None):
    ts = ts or str(int(time.time()))
    nonce = "n1"
    return {"x-lark-request-timestamp": ts, "x-lark-request-nonce": nonce,
            "x-lark-signature": crypto.feishu_signature(ts, nonce, key, body)}


def test_signature_mode_accepts_valid_signature():
    a = _feishu({"MAOS_FEISHU_ENCRYPT_KEY": ENCRYPT_KEY})
    body = json.dumps(_message_event()).encode()
    a.verify(WebhookRequest(method="POST", path="/x", body=body,
                            headers=_signed(body)))


def test_signature_mode_rejects_tampered_body():
    """签名对的是**原始字节**。改一个字节就该拒 —— 这条买的是「不能只校头」。"""
    a = _feishu({"MAOS_FEISHU_ENCRYPT_KEY": ENCRYPT_KEY})
    body = json.dumps(_message_event()).encode()
    headers = _signed(body)
    with pytest.raises(VerifyError):
        a.verify(WebhookRequest(method="POST", path="/x",
                                body=body + b" ", headers=headers))


def test_signature_mode_rejects_stale_timestamp():
    """重放：签名完全合法，只是旧。没有时间戳窗口这条就过了。"""
    a = _feishu({"MAOS_FEISHU_ENCRYPT_KEY": ENCRYPT_KEY})
    body = json.dumps(_message_event()).encode()
    old = str(int(time.time()) - crypto.MAX_CLOCK_SKEW - 60)
    with pytest.raises(VerifyError, match="时间戳偏差"):
        a.verify(WebhookRequest(method="POST", path="/x", body=body,
                                headers=_signed(body, ts=old)))


def test_signature_mode_rejects_missing_headers():
    """不发签名头 != 跳过校验。这是「自己给自己开后门」那条。"""
    a = _feishu({"MAOS_FEISHU_ENCRYPT_KEY": ENCRYPT_KEY})
    with pytest.raises(VerifyError, match="缺签名头"):
        a.verify(_req(_message_event()))


def test_missing_credentials_refuses_instead_of_degrading():
    """入站缺凭证**拒收**，不像出站那样降级 log-only。"""
    a = FeishuAdapter(FeishuConfig.from_env({}))
    assert not a.configured
    with pytest.raises(ChannelNotConfigured):
        a.verify(_req({"token": "whatever"}))


def test_half_configured_is_not_configured():
    """能收不能发也算没配好：处置会真跑，而群里什么都看不到。"""
    a = _feishu({"MAOS_FEISHU_APP_SECRET": ""})
    assert not a.configured


# --------------------------------------------------------------------------
# 企业微信 / 微信客服
# --------------------------------------------------------------------------
def _wecom(**over) -> WeComAdapter:
    creds = WeComCredentials(corp_id="wwcorp", secret="s", token="tok",
                             aes_key=AES_KEY_43, agent_id="1000002")
    return WeComAdapter(creds, **over)


def test_wecom_signature_is_order_independent():
    """规范要求四项**排序后**拼接，所以参数换序不改变签名。"""
    a = crypto.wecom_signature("tok", "100", "n", "E")
    b = crypto.wecom_signature("tok", "n", "100", "E")
    assert a == b


def test_wecom_rejects_missing_query_params():
    with pytest.raises(VerifyError, match="msg_signature"):
        _wecom().verify(WebhookRequest(method="POST", path="/x", body=b"<xml/>"))


def test_aes_key_length_is_validated():
    with pytest.raises(VerifyError, match="43 位"):
        crypto.wecom_aes_key("tooshort")


def test_missing_aes_backend_raises_explicitly():
    """**不跳过**：装了后端时这条验往返，没装时验它显式抛而不是静默回落。

    静默回落的症状是「企微群里发了没反应」—— 密文被当明文解析失败，然后归入
    「不是消息事件」丢掉，日志里一片安静。
    """
    if HAS_AES:
        blob = crypto.wecom_encrypt(AES_KEY_43, b"hi", "wwcorp")
        plain, receiveid = crypto.wecom_decrypt(AES_KEY_43, blob)
        assert (plain, receiveid) == (b"hi", "wwcorp")
    else:
        with pytest.raises(ChannelDepMissing, match="cryptography"):
            crypto.wecom_encrypt(AES_KEY_43, b"hi", "wwcorp")


@needs_aes
def test_wecom_roundtrip_and_receiveid_guard():
    """别家企业的合法回调打到我们地址上，只有 receiveid 能认出来。"""
    blob = crypto.wecom_encrypt(AES_KEY_43, b"<xml/>", "wwOTHER")
    with pytest.raises(VerifyError, match="不是本企业"):
        WeComAdapter(WeComCredentials(
            corp_id="wwcorp", secret="s", token="tok", aes_key=AES_KEY_43,
        ))._decrypt_body(WebhookRequest(
            method="POST", path="/x",
            body=f"<xml><Encrypt>{blob}</Encrypt></xml>".encode()))


@needs_aes
def test_wecom_parse_reads_text_message():
    inner = ("<xml><MsgType>text</MsgType><Content>/help</Content>"
             "<FromUserName>zhangsan</FromUserName><MsgId>123</MsgId>"
             "<CreateTime>1756800000</CreateTime></xml>")
    blob = crypto.wecom_encrypt(AES_KEY_43, inner.encode(), "wwcorp")
    (msg,) = _wecom().parse(WebhookRequest(
        method="POST", path="/x",
        body=f"<xml><Encrypt>{blob}</Encrypt></xml>".encode()))
    assert (msg.text, msg.sender, msg.msg_id) == ("/help", "zhangsan", "123")
    assert msg.dedup_key == "ingress:wecom:123"


def test_xml_parse_refuses_garbage():
    with pytest.raises(VerifyError, match="不是合法 XML"):
        crypto.parse_xml_fields(b"not xml at all", "Encrypt")


def test_pkcs7_unpad_validates_padding():
    """不校验 padding 等于接受任意构造的尾部。"""
    with pytest.raises(VerifyError, match="padding 非法"):
        crypto.pkcs7_unpad(b"abc" + bytes([99]), 32)


def test_kf_and_wecom_are_separate_channels():
    """微信客服与自建应用**不共用渠道标识** —— 审批面按渠道关闸靠的就是这个区分。"""
    kf = WeChatKfAdapter(WeComCredentials(corp_id="wwcorp", secret="s",
                                          token="t", aes_key=AES_KEY_43))
    assert kf.name != _wecom().name


def test_kf_stale_messages_are_dropped(monkeypatch):
    """游标丢了也不许翻旧账：超龄消息一条都不进命令面。"""
    kf = WeChatKfAdapter(WeComCredentials(corp_id="wwcorp", secret="s",
                                          token="t", aes_key=AES_KEY_43))
    now = time.time()
    monkeypatch.setattr(kf, "_post", lambda path, payload: {
        "next_cursor": "c2",
        "msg_list": [
            {"msgtype": "text", "msgid": "old", "send_time": now - 86400,
             "external_userid": "wmA", "text": {"content": "/refund ORD-1 质量问题"}},
            {"msgtype": "text", "msgid": "new", "send_time": now,
             "external_userid": "wmA", "text": {"content": "/help"}},
        ],
    })
    msgs = kf._sync("wk1", "token")
    assert [m.msg_id for m in msgs] == ["new"]
    assert kf.cursor("wk1") == "c2"


def test_kf_send_refuses_without_open_kfid():
    """发错客服账号 = 把 A 的退款结论发给 B。宁可这条回信失败。"""
    from maos.ingress.contracts import OutboundMessage
    kf = WeChatKfAdapter(WeComCredentials(corp_id="wwcorp", secret="s",
                                          token="t", aes_key=AES_KEY_43))
    with pytest.raises(RuntimeError, match="open_kfid"):
        kf.send(OutboundMessage(chat_id="wmA", text="hi"))
