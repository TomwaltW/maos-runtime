"""飞书（Lark）接入 —— 事件订阅 v2 收，OpenAPI 发。

## 明文还是加密，是一个**安全决策**，不是配置口味

飞书应用后台的 Encrypt Key 留空 = 明文回调，此时平台**不发签名头**，能校的只有
body 里那个 ``token`` 字段。它是一个长期不变的共享秘密，且随每一个请求原样送达 ——
一旦回调地址走过任何一段不受信的链路（日志、网关、抓包），它就泄了，而泄了之后
任何人都能构造出「合法」的回调。

配上 Encrypt Key 才有 ``sha256(timestamp + nonce + key + body)`` 签名，加上时间戳
窗口，重放和伪造才有代价。**生产必须配**；留明文这条路是因为它零依赖，本机
（没装 cryptography 的 python3）能当场把整条链路跑通，联调时少一个变量。

两种模式在 :meth:`FeishuAdapter.verify` 里分叉，而不是「有签名就校、没有就算了」——
后者的意思是攻击者只要不发签名头就能跳过校验，这类降级是自己给自己开的后门。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field

from maos.ingress import crypto
from maos.ingress.contracts import (
    CHANNEL_FEISHU, Attachment, ChannelNotConfigured, InboundMessage,
    OutboundMessage, VerifyError, WebhookRequest,
)
from maos.ingress.transport import ApiError, request_bytes, request_json

log = logging.getLogger("maos.ingress.feishu")

BASE_URL = "https://open.feishu.cn/open-apis"
EVENT_MESSAGE = "im.message.receive_v1"

#: 进命令面的消息类型。``post`` 是图文混排 —— 群里「一段说明 + 三张照片」发出来
#: 就是它，漏掉它等于漏掉最自然的那种发法。语音、视频、名片仍然安静忽略：
#: 收下它们只会得到一份存不进白名单、也没人看的字节。
_HANDLED_TYPES = ("text", "image", "file", "post")

ENV_APP_ID = "MAOS_FEISHU_APP_ID"
ENV_APP_SECRET = "MAOS_FEISHU_APP_SECRET"
ENV_VERIFY_TOKEN = "MAOS_FEISHU_VERIFICATION_TOKEN"
ENV_ENCRYPT_KEY = "MAOS_FEISHU_ENCRYPT_KEY"

#: 群里 @机器人，正文会带一个占位符而不是昵称。不清掉，`/refund` 就永远不在行首，
#: 命令解析全部落空 —— 症状是「群里 @ 了机器人发命令没反应，私聊却好使」。
_MENTION = re.compile(r"@_(?:user_\d+|all)\s*")


@dataclass(frozen=True)
class FeishuConfig:
    """只读环境变量（铁律 6）。两个 secret 都 ``repr=False`` —— 它们会随日志走。"""

    app_id: str = ""
    app_secret: str = field(default="", repr=False)
    verification_token: str = field(default="", repr=False)
    encrypt_key: str = field(default="", repr=False)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "FeishuConfig":
        src = os.environ if env is None else env
        get = lambda name: (src.get(name) or "").strip()      # noqa: E731
        return cls(
            app_id=get(ENV_APP_ID),
            app_secret=get(ENV_APP_SECRET),
            verification_token=get(ENV_VERIFY_TOKEN),
            encrypt_key=get(ENV_ENCRYPT_KEY),
        )

    @property
    def encrypted(self) -> bool:
        return bool(self.encrypt_key)


class FeishuAdapter:
    """实现 `ChannelAdapter`。一个实例对应一个飞书应用。"""

    name = CHANNEL_FEISHU

    def __init__(self, config: FeishuConfig | None = None, *,
                 base_url: str = BASE_URL) -> None:
        self.config = config or FeishuConfig.from_env()
        self.base_url = base_url.rstrip("/")
        self._token = ""
        self._token_expire = 0.0

    # -- 凭证 ---------------------------------------------------------------
    @property
    def configured(self) -> bool:
        """收得下**且**发得出才算配好。

        只配了校验密钥没配 app_secret 的半吊子状态尤其要挡在门外：它能收下命令、
        跑完一次真实处置，然后回帖那一步失败 —— 处置已经发生了，而群里什么都
        没看到。宁可在启动时 503。
        """
        c = self.config
        return bool(c.app_id and c.app_secret
                    and (c.verification_token or c.encrypt_key))

    def _require(self) -> None:
        if not self.configured:
            missing = [n for n, v in (
                (ENV_APP_ID, self.config.app_id),
                (ENV_APP_SECRET, self.config.app_secret),
            ) if not v]
            if not (self.config.verification_token or self.config.encrypt_key):
                missing.append(f"{ENV_VERIFY_TOKEN} 或 {ENV_ENCRYPT_KEY}")
            raise ChannelNotConfigured(f"飞书渠道缺环境变量：{', '.join(missing)}")

    # -- 入站 ---------------------------------------------------------------
    def verify(self, req: WebhookRequest) -> None:
        self._require()
        if self.config.encrypted:
            self._verify_signature(req)
        else:
            self._verify_token(req)

    def _verify_signature(self, req: WebhookRequest) -> None:
        timestamp = req.header("x-lark-request-timestamp")
        nonce = req.header("x-lark-request-nonce")
        given = req.header("x-lark-signature")
        if not (timestamp and nonce and given):
            raise VerifyError(
                "缺签名头（X-Lark-Request-Timestamp / -Nonce / X-Lark-Signature）。"
                "配了 Encrypt Key 平台才发这三个头 —— 如果后台是空的，请把 "
                f"{ENV_ENCRYPT_KEY} 也清掉，两边口径要一致")
        crypto.check_timestamp(timestamp)
        want = crypto.feishu_signature(timestamp, nonce, self.config.encrypt_key, req.body)
        if not crypto.equal(given, want):
            raise VerifyError("飞书签名不匹配")

    def _verify_token(self, req: WebhookRequest) -> None:
        """明文模式：比对 body 里的 ``token``。

        **两个位置都要看**：v2 事件推送把它放在 ``header.token``，而 URL 验证
        （``type=url_verification``）放在顶层。只看顶层的后果是「回调地址配得通、
        真消息全被 401」—— 而平台后台只显示一个失败计数，看不出是哪一类请求。

        注意这里**必须**自己解析 body —— 而模块抬头说「校验没过不许碰 body」。
        两者不冲突：明文模式下 token 就在 body 里，除了解析没有别的取法。
        代价是这条路径确实在鉴权前 ``json.loads`` 了一次不可信输入，这也正是
        生产该配 Encrypt Key 的又一条理由。
        """
        body = req.json()
        token = str(body.get("token") or (body.get("header") or {}).get("token") or "")
        if not token or not crypto.equal(token, self.config.verification_token):
            raise VerifyError("飞书 verification token 不匹配")

    def _decode(self, req: WebhookRequest) -> dict:
        """拿到已解密的事件体。加密模式下 body 是 ``{"encrypt": "..."}``。"""
        raw = req.json()
        blob = raw.get("encrypt")
        if not blob:
            return raw
        plain = crypto.feishu_decrypt(self.config.encrypt_key, str(blob))
        try:
            out = json.loads(plain.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VerifyError(f"飞书密文解出来不是 JSON：{exc}") from exc
        return out if isinstance(out, dict) else {}

    def challenge(self, req: WebhookRequest) -> str | None:
        """URL 验证。加密模式下 challenge 也在密文里，所以先解密再判。"""
        body = self._decode(req)
        if body.get("type") != "url_verification":
            return None
        return json.dumps({"challenge": body.get("challenge", "")}, ensure_ascii=False)

    def parse(self, req: WebhookRequest) -> list[InboundMessage]:
        body = self._decode(req)
        header = body.get("header") or {}
        if header.get("event_type") != EVENT_MESSAGE:
            return []                         # 进群、撤回、已读回执……不是我们的活
        event = body.get("event") or {}
        message = event.get("message") or {}
        msg_type = str(message.get("message_type") or "")
        if msg_type not in _HANDLED_TYPES:
            return []                         # 语音、视频、表情、卡片回调……不是我们的活

        content: dict = {}
        try:
            content = json.loads(message.get("content") or "{}")
        except json.JSONDecodeError:
            log.warning("飞书 message.content 不是 JSON，按空内容处理")
        if not isinstance(content, dict):
            content = {}

        text = str(content.get("text") or "")
        attachments = self._attachments_of(msg_type, content, message)
        sender = (event.get("sender") or {}).get("sender_id") or {}

        return [InboundMessage(
            channel=self.name,
            chat_id=str(message.get("chat_id") or ""),
            # open_id 是「本应用内」的用户标识，审批名单按它配。union_id 跨应用稳定，
            # 但要额外权限才拿得到；两个都没有时留空 —— 名单必然不匹配，于是被拒，
            # 这正是想要的失败方向（宁可拒错，不可放错）。
            sender=str(sender.get("open_id") or sender.get("union_id") or ""),
            text=_MENTION.sub("", text).strip(),
            # 去重优先用 event_id：飞书重推的是**事件**，同一条消息若触发两类事件，
            # message_id 会相同而 event_id 不同 —— 用 message_id 会把第二类事件误吞。
            msg_id=str(header.get("event_id") or message.get("message_id") or ""),
            ts=str(header.get("create_time") or ""),
            raw=body,
            attachments=attachments,
        )]

    def _attachments_of(self, msg_type: str, content: dict,
                        message: dict) -> tuple[Attachment, ...]:
        """从一条消息里挑出取件凭据。

        ``message_id`` 是取件的**必需**参数，而它与 :attr:`InboundMessage.msg_id`
        不是一个东西 —— 后者优先用 ``event_id``（去重要按事件去重，见 ``parse``）。
        用 msg_id 去取件会得到 232001「消息不存在」，而那个错离原因很远。所以这里
        单独把 message_id 放进 ``msg_ref``。

        ``post``（图文混排）里的图片藏在 ``content.content`` 的嵌套段落里，
        与 ``image`` 的顶层 ``image_key`` 不同层。不铺开它，群里「一段说明 + 三张
        照片」这个最自然的发法就只剩文字进来。
        """
        message_id = str(message.get("message_id") or "")
        if not message_id:
            # 取不到就不产出凭据：产出一个取不了件的 Attachment，下游会先回一句
            # 「已收到证据」再在取件时失败 —— 那是「看起来成功了的失败」。
            log.warning("飞书 %s 消息缺 message_id，附件无法取件，本条按纯文本处理", msg_type)
            return ()

        keys: list[tuple[str, str, str]] = []          # (kind, file_key, filename)
        if msg_type == "image":
            keys.append(("image", str(content.get("image_key") or ""), ""))
        elif msg_type == "file":
            keys.append(("file", str(content.get("file_key") or ""),
                         str(content.get("file_name") or "")))
        elif msg_type == "post":
            for para in content.get("content") or []:
                for node in para if isinstance(para, list) else []:
                    if not isinstance(node, dict):
                        continue
                    if node.get("tag") == "img":
                        keys.append(("image", str(node.get("image_key") or ""), ""))
                    elif node.get("tag") == "media":
                        keys.append(("file", str(node.get("file_key") or ""),
                                     str(node.get("file_name") or "")))

        return tuple(
            Attachment(channel=self.name, file_key=key, kind=kind, filename=name,
                       msg_ref={"message_id": message_id})
            for kind, key, name in keys if key
        )

    def fetch(self, att: Attachment) -> bytes:
        """按 ``im/v1/messages/{message_id}/resources/{file_key}`` 取回字节。

        ``type`` 必须与消息类型一致（image 的 key 用 ``type=file`` 去取会 234005），
        所以它取自 :attr:`Attachment.kind` 而不是猜。

        飞书取件**失败时返回 JSON 而不是 4xx**，HTTP 状态照样 200。不判这一条的
        症状最难查：那份 JSON 会被当成图片字节落盘，digest 算得出来、文件也在，
        只有真去打开它的人才会发现是段错误信息。
        """
        message_id = str((att.msg_ref or {}).get("message_id") or "")
        if not message_id or not att.file_key:
            raise ApiError(f"飞书取件凭据不全：message_id={message_id!r} key={att.file_key!r}")

        data, ctype = request_bytes(
            f"{self.base_url}/im/v1/messages/{message_id}/resources/{att.file_key}",
            params={"type": "image" if att.kind == "image" else "file"},
            headers={"Authorization": f"Bearer {self._tenant_token()}"},
        )
        if "application/json" in ctype.lower():
            detail = data.decode("utf-8", "replace")[:200]
            raise ApiError(f"飞书取件失败（HTTP 200 但返回 JSON）：{detail}")
        return data

    # -- 出站 ---------------------------------------------------------------
    def _tenant_token(self) -> str:
        """取 tenant_access_token，带一点提前量的缓存。

        提前 120 秒过期是刻意的：token 恰好在「发消息」这一刻失效会得到一个
        99991663，而那时处置已经跑完了 —— 重试的成本远高于早换一次 token。
        """
        if self._token and time.time() < self._token_expire:
            return self._token
        self._require()
        out = request_json(
            f"{self.base_url}/auth/v3/tenant_access_token/internal",
            method="POST",
            payload={"app_id": self.config.app_id, "app_secret": self.config.app_secret},
        )
        if out.get("code") != 0:
            raise ChannelNotConfigured(
                f"取飞书 tenant_access_token 失败：code={out.get('code')} "
                f"msg={out.get('msg')}（先查 {ENV_APP_ID} / {ENV_APP_SECRET}）")
        self._token = str(out.get("tenant_access_token") or "")
        self._token_expire = time.time() + max(int(out.get("expire") or 0) - 120, 0)
        return self._token

    def send(self, msg: OutboundMessage) -> None:
        out = request_json(
            f"{self.base_url}/im/v1/messages",
            method="POST",
            params={"receive_id_type": "chat_id"},
            headers={"Authorization": f"Bearer {self._tenant_token()}"},
            payload={
                "receive_id": msg.chat_id,
                "msg_type": "text",
                # content 必须是**字符串化**的 JSON，不是嵌套对象 —— 传对象会得到
                # 一个 234001「参数错误」，而报错里不会指出是这个字段。
                "content": json.dumps({"text": msg.text}, ensure_ascii=False),
            },
        )
        if out.get("code") != 0:
            raise RuntimeError(
                f"飞书发送失败：code={out.get('code')} msg={out.get('msg')}")
