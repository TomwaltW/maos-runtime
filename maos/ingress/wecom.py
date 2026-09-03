"""企业微信自建应用 + 微信客服 —— 一套凭证体系，两个渠道。

## 为什么是两个 adapter 而不是一个

回调的**加密壳一模一样**（WXBizMsgCrypt），里面装的东西完全不同：

  · 自建应用：解密后就是消息本身（``MsgType=text`` / ``Content``），发言的是**同事**。
  · 微信客服：解密后只有一个 ``Token``，消息要拿着它回调 ``sync_msg`` 主动拉，
    一次可能拉回多条；发言的是**外部微信用户**（``external_userid``）。

合成一个 adapter 就得在里面按事件类型分叉，而两条路的**信任级别不同**：审批名单
只认同事。让客服窗口里的外部用户和自建应用里的同事流进同一个 ``sender`` 空间，
下一个改这里的人就得自己记着「这个 sender 是不是外部人」—— 记不住的那天，
客户在客服窗口里打一句 ``/approve`` 就把退款批了。分成两个渠道标识，
`router.py` 才能对外部渠道**整体关掉审批命令**（见 `router.py::ALLOW_APPROVAL`）。

## 微信客服的历史消息是个坑

``sync_msg`` 的 ``cursor`` 传空表示「从最早开始」，首次接入会把**积压的全部历史
消息**拉回来（上限 1000 条）。这些消息会一条条流进命令面 —— 如果里面有几条
``/refund``，那就是把三个月前的单子重跑一遍，且每一笔都是真钱。所以这里有两道闸：
游标持久化（:attr:`WeChatKfAdapter.cursor`）与消息年龄上限
（:data:`KF_MAX_AGE`）。第二道是兜底：游标丢了也不会翻旧账。
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Callable

from maos.ingress import crypto
from maos.ingress.contracts import (
    CHANNEL_WECHAT_KF, CHANNEL_WECOM, ChannelNotConfigured, InboundMessage,
    OutboundMessage, VerifyError, WebhookRequest,
)
from maos.ingress.transport import request_json

log = logging.getLogger("maos.ingress.wecom")

BASE_URL = "https://qyapi.weixin.qq.com/cgi-bin"

ENV_CORP_ID = "MAOS_WECOM_CORP_ID"
ENV_AGENT_ID = "MAOS_WECOM_AGENT_ID"
ENV_SECRET = "MAOS_WECOM_SECRET"
ENV_TOKEN = "MAOS_WECOM_TOKEN"
ENV_AES_KEY = "MAOS_WECOM_AES_KEY"

ENV_KF_SECRET = "MAOS_WECHAT_KF_SECRET"
ENV_KF_TOKEN = "MAOS_WECHAT_KF_TOKEN"
ENV_KF_AES_KEY = "MAOS_WECHAT_KF_AES_KEY"

#: 微信客服消息的年龄上限（秒）。超过就丢弃并记一条 WARNING。
#:
#: 这不是「怕慢」，是防**翻旧账**：游标丢失（换机器、库没了、第一次接入）会让
#: ``sync_msg`` 从最早的消息开始给，而那些消息里的 ``/refund`` 一条都不该再跑。
#: 十分钟足够覆盖正常的回调重试与处理耗时，又远短于任何「历史」。
KF_MAX_AGE = 600


# --------------------------------------------------------------------------
# 共用：加密回调的壳
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class WeComCredentials:
    """一个渠道的凭证。``corp_id`` 两个渠道共用，其余各自独立。"""

    corp_id: str = ""
    secret: str = field(default="", repr=False)
    token: str = field(default="", repr=False)
    aes_key: str = field(default="", repr=False)
    agent_id: str = ""


class _WeComBase:
    """WXBizMsgCrypt 的壳：签名校验、URL 验证、access_token。两个渠道共用。

    不做成 `ChannelAdapter` 的实现 —— 它缺 ``parse``/``send``，是半个 adapter。
    子类补上那两个方法才成立。
    """

    name = ""

    def __init__(self, creds: WeComCredentials, *, base_url: str = BASE_URL) -> None:
        self.creds = creds
        self.base_url = base_url.rstrip("/")
        self._token = ""
        self._token_expire = 0.0

    # -- 凭证 ---------------------------------------------------------------
    @property
    def configured(self) -> bool:
        c = self.creds
        return bool(c.corp_id and c.secret and c.token and c.aes_key)

    def _require(self) -> None:
        if not self.configured:
            missing = [name for name, val in self._env_pairs() if not val]
            raise ChannelNotConfigured(
                f"{self.name} 渠道缺环境变量：{', '.join(missing)}")

    def _env_pairs(self) -> list[tuple[str, str]]:
        raise NotImplementedError

    # -- 入站 ---------------------------------------------------------------
    def _signed_field(self, req: WebhookRequest) -> str:
        """签名的第四项：URL 验证时是 ``echostr``，消息回调时是 XML 里的 ``Encrypt``。"""
        echostr = req.query.get("echostr")
        if echostr:
            return echostr
        fields = crypto.parse_xml_fields(req.body, "Encrypt")
        if "Encrypt" not in fields:
            raise VerifyError("企微回调里没有 Encrypt 字段")
        return fields["Encrypt"]

    def verify(self, req: WebhookRequest) -> None:
        self._require()
        q = req.query
        timestamp, nonce = q.get("timestamp", ""), q.get("nonce", "")
        given = q.get("msg_signature", "")
        if not (timestamp and nonce and given):
            raise VerifyError("缺 msg_signature / timestamp / nonce（都在 query 上）")
        crypto.check_timestamp(timestamp)
        want = crypto.wecom_signature(
            self.creds.token, timestamp, nonce, self._signed_field(req))
        if not crypto.equal(given, want):
            raise VerifyError("企微 msg_signature 不匹配")

    def challenge(self, req: WebhookRequest) -> str | None:
        """URL 验证：GET + ``echostr``，解密后**原样**回明文（不加引号、不包 JSON）。"""
        echostr = req.query.get("echostr")
        if not echostr:
            return None
        plain, _ = crypto.wecom_decrypt(
            self.creds.aes_key, echostr, expect_receiveid=self.creds.corp_id)
        return plain.decode("utf-8")

    def _decrypt_body(self, req: WebhookRequest) -> bytes:
        fields = crypto.parse_xml_fields(req.body, "Encrypt")
        plain, receiveid = crypto.wecom_decrypt(self.creds.aes_key, fields["Encrypt"])
        if receiveid and not crypto.equal(receiveid, self.creds.corp_id):
            # 签名可能是**对的** —— 别家企业用它自己的 token 签的一条合法回调，
            # 被打到了我们这个地址。只有 receiveid 能把它认出来。
            raise VerifyError(
                f"回调的 receiveid 不是本企业（{receiveid[:6]}…），已拒收")
        return plain

    # -- 出站 ---------------------------------------------------------------
    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expire:
            return self._token
        self._require()
        out = request_json(f"{self.base_url}/gettoken", params={
            "corpid": self.creds.corp_id, "corpsecret": self.creds.secret})
        if out.get("errcode") not in (0, None):
            raise ChannelNotConfigured(
                f"取企微 access_token 失败：errcode={out.get('errcode')} "
                f"errmsg={out.get('errmsg')}")
        self._token = str(out.get("access_token") or "")
        # 同飞书那条：提前 120 秒换，别让 token 恰好在发消息那一刻失效。
        self._token_expire = time.time() + max(int(out.get("expires_in") or 0) - 120, 0)
        return self._token

    def _post(self, path: str, payload: dict) -> dict:
        out = request_json(f"{self.base_url}/{path.lstrip('/')}", method="POST",
                           params={"access_token": self._access_token()},
                           payload=payload)
        if out.get("errcode") not in (0, None):
            raise RuntimeError(
                f"企微 {path} 失败：errcode={out.get('errcode')} "
                f"errmsg={out.get('errmsg')}")
        return out


# --------------------------------------------------------------------------
# 企业微信自建应用（内部同事）
# --------------------------------------------------------------------------
class WeComAdapter(_WeComBase):
    """自建应用会话。``chat_id`` 存的是对方的 ``userid`` —— 应用会话就是一对一的。"""

    name = CHANNEL_WECOM

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None, **kw) -> "WeComAdapter":
        src = os.environ if env is None else env
        get = lambda n: (src.get(n) or "").strip()          # noqa: E731
        return cls(WeComCredentials(
            corp_id=get(ENV_CORP_ID), secret=get(ENV_SECRET),
            token=get(ENV_TOKEN), aes_key=get(ENV_AES_KEY),
            agent_id=get(ENV_AGENT_ID)), **kw)

    def _env_pairs(self) -> list[tuple[str, str]]:
        c = self.creds
        return [(ENV_CORP_ID, c.corp_id), (ENV_SECRET, c.secret),
                (ENV_TOKEN, c.token), (ENV_AES_KEY, c.aes_key)]

    def parse(self, req: WebhookRequest) -> list[InboundMessage]:
        plain = self._decrypt_body(req)
        f = crypto.parse_xml_fields(
            plain, "MsgType", "Content", "FromUserName", "MsgId", "CreateTime", "Event")
        if f.get("MsgType") != "text":
            return []                          # 事件、图片、语音一律不进命令面
        return [InboundMessage(
            channel=self.name,
            chat_id=f.get("FromUserName", ""),
            sender=f.get("FromUserName", ""),
            text=(f.get("Content") or "").strip(),
            msg_id=f.get("MsgId", ""),
            ts=f.get("CreateTime", ""),
        )]

    def send(self, msg: OutboundMessage) -> None:
        self._post("message/send", {
            "touser": msg.chat_id,
            "msgtype": "text",
            "agentid": self.creds.agent_id,
            "text": {"content": msg.text},
        })


# --------------------------------------------------------------------------
# 微信客服（外部微信用户）
# --------------------------------------------------------------------------
class WeChatKfAdapter(_WeComBase):
    """微信客服。外部微信用户在这里发言，**信任级别低于自建应用**。

    ``cursor`` 的持久化交给调用方：构造时传 ``load_cursor`` / ``save_cursor``
    两个回调，不传就只在进程内存着。不在这里直接写库，是因为渠道层不该自己决定
    往哪张表写 —— 那会让它和 `Store` 绑死，而 `server.py` 完全可能是个独立进程。
    """

    name = CHANNEL_WECHAT_KF

    def __init__(self, creds: WeComCredentials, *, base_url: str = BASE_URL,
                 load_cursor: Callable[[str], str] | None = None,
                 save_cursor: Callable[[str, str], None] | None = None,
                 max_age: int = KF_MAX_AGE) -> None:
        super().__init__(creds, base_url=base_url)
        self._cursors: dict[str, str] = {}
        self._load_cursor = load_cursor
        self._save_cursor = save_cursor
        self.max_age = max_age

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None, **kw) -> "WeChatKfAdapter":
        src = os.environ if env is None else env
        get = lambda n: (src.get(n) or "").strip()          # noqa: E731
        return cls(WeComCredentials(
            corp_id=get(ENV_CORP_ID),           # corp_id 与自建应用共用
            secret=get(ENV_KF_SECRET),          # 其余三项是客服自己的
            token=get(ENV_KF_TOKEN), aes_key=get(ENV_KF_AES_KEY)), **kw)

    def _env_pairs(self) -> list[tuple[str, str]]:
        c = self.creds
        return [(ENV_CORP_ID, c.corp_id), (ENV_KF_SECRET, c.secret),
                (ENV_KF_TOKEN, c.token), (ENV_KF_AES_KEY, c.aes_key)]

    # -- 游标 ---------------------------------------------------------------
    def cursor(self, open_kfid: str) -> str:
        if open_kfid not in self._cursors and self._load_cursor:
            self._cursors[open_kfid] = self._load_cursor(open_kfid) or ""
        return self._cursors.get(open_kfid, "")

    def _remember(self, open_kfid: str, cursor: str) -> None:
        if not cursor:
            return
        self._cursors[open_kfid] = cursor
        if self._save_cursor:
            self._save_cursor(open_kfid, cursor)

    # -- 入站 ---------------------------------------------------------------
    def parse(self, req: WebhookRequest) -> list[InboundMessage]:
        """回调只是一声「有新消息」，真正的内容要主动去拉。"""
        plain = self._decrypt_body(req)
        f = crypto.parse_xml_fields(plain, "MsgType", "Event", "Token", "OpenKfId")
        if f.get("MsgType") != "event" or not f.get("Token"):
            return []
        return self._sync(f.get("OpenKfId", ""), f["Token"])

    def _sync(self, open_kfid: str, token: str) -> list[InboundMessage]:
        out = self._post("kf/sync_msg", {
            "cursor": self.cursor(open_kfid),
            "token": token,
            "limit": 1000,
            "open_kfid": open_kfid,
        })
        self._remember(open_kfid, str(out.get("next_cursor") or ""))

        now, msgs = time.time(), []
        stale = 0
        for row in out.get("msg_list") or []:
            if row.get("msgtype") != "text":
                continue
            send_time = float(row.get("send_time") or 0)
            if send_time and now - send_time > self.max_age:
                stale += 1                     # 见 KF_MAX_AGE：这是防翻旧账的兜底闸
                continue
            msgs.append(InboundMessage(
                channel=self.name,
                chat_id=str(row.get("external_userid") or ""),
                sender=str(row.get("external_userid") or ""),
                text=str((row.get("text") or {}).get("content") or "").strip(),
                msg_id=str(row.get("msgid") or ""),
                ts=str(row.get("send_time") or ""),
                raw={"open_kfid": row.get("open_kfid") or open_kfid},
            ))
        if stale:
            log.warning(
                "微信客服丢弃 %d 条超过 %ds 的历史消息（游标可能丢了）—— "
                "这是刻意的：翻旧账会把老单子重跑一遍", stale, self.max_age)
        return msgs

    # -- 出站 ---------------------------------------------------------------
    def send(self, msg: OutboundMessage) -> None:
        open_kfid = str(msg.meta.get("open_kfid") or "")
        if not open_kfid:
            # 不猜、不挑一个「看起来对」的客服账号：发错账号的消息会出现在另一个
            # 客户的会话里，那是把 A 的退款结论发给了 B。宁可这条回信失败。
            raise RuntimeError(
                "微信客服回信缺 open_kfid —— 它应由 router 从入站消息的 raw 里带回来")
        self._post("kf/send_msg", {
            "touser": msg.chat_id,
            "open_kfid": open_kfid,
            "msgtype": "text",
            "text": {"content": msg.text},
        })
