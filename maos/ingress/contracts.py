"""渠道接入的平台无关契约 —— 三个 IM 的差异到此为止，往里一律是同一个形状。

**这不是冻结契约。** `maos/contracts/` 那两个文件禁改（铁律 1），本文件是渠道层
自己的形状，可以随新平台演进。放在这里而不是各 adapter 里，是为了让「加一个平台」
只需实现 :class:`ChannelAdapter` 的四个方法，`router.py` 与 `server.py` 一行不动。

## 四个方法为什么是这四个

飞书、企业微信、微信客服的回调长得完全不一样，但要做的事只有四件，缺一不可：

  · ``verify``    —— 先校签名。这是**公网面**，任何人都能 POST 到这个地址。
  · ``challenge`` —— 平台配回调地址时先来一次「URL 验证」，答不上来配不通。
  · ``parse``     —— 把回调解包成若干条 :class:`InboundMessage`。
  · ``send``      —— 把结果发回那个会话。

``parse`` 返回**列表**而不是单条，是被微信客服逼出来的、且是对的：它的回调里
没有消息内容，只给一个 ``Token``，要拿着它回调 ``sync_msg`` 主动拉，一次能拉回
多条。签名成 ``-> InboundMessage | None`` 就得在 adapter 里私藏一个队列，把
「这次回调带回了 3 条」变成三次不透明的状态，测试也没法一次看全。飞书、企微
自建应用返回 0 或 1 条，是这个形状的特例。

顺序不可换：**校验没过就不许碰 body**。先解析后校验的写法在功能上看不出区别
（正常请求两种顺序都通），但它把「解析未鉴权的攻击者输入」这件事做成了默认路径。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

#: 三个渠道的稳定标识。落进 `event_log` 的 detail 与幂等键，改名等于把历史记录割裂。
CHANNEL_FEISHU = "feishu"
CHANNEL_WECOM = "wecom"
CHANNEL_WECHAT_KF = "wechat_kf"

#: 微信客服**不是**第四套凭证：它挂在企业微信下面，与自建应用共用 corp_id。
#: 分成两个渠道标识是因为「谁在说话」不同 —— 自建应用里是同事，微信客服里是客户，
#: 而审批名单只认同事。合并成一个标识，客户就能在客服窗口里打 /approve。
CHANNELS = (CHANNEL_FEISHU, CHANNEL_WECOM, CHANNEL_WECHAT_KF)

#: Matrix 房间。**刻意不进 :data:`CHANNELS`** —— 那个元组是「有 webhook 路由的渠道」，
#: `server.py` 照着它建路由、`describe_config` 照着它报凭证。Matrix 走的是长连
#: ``sync``，没有回调地址也没有签名要校，混进去只会得到一条永远 503 的路由。
#: 但它同样会有人往里发照片，所以证据与幂等键需要一个稳定的渠道标识。
CHANNEL_MATRIX = "matrix"


class VerifyError(RuntimeError):
    """签名 / 时间戳 / token 校验不通过。**一律 401，且不解析 body。**

    与下面两个异常分开，是因为处置完全不同：这一条意味着「这个请求不可信」，
    正确动作是拒绝并留痕；另两条意味着「我方没配好」，是运维问题。混在一起，
    公网上的探测流量会和自家配置错误混进同一条日志里，两边都查不动。
    """


class ChannelDepMissing(RuntimeError):
    """回调加密需要 AES，而 ``cryptography`` 没装。**环境错，不是运行时故障。**

    照 `maos/store/pg_store.py` 的 `PgBackendUnavailable` 范式显式抛，不静默回落 ——
    企业微信的回调**强制加密**，静默回落只有一种可能：把密文当明文解析失败、
    然后当成「没有消息事件」丢掉。症状是「群里发了没反应」，离原因很远。

    装：``python3 -m pip install 'maos[ingress]'``（只有企微/微信客服需要，
    飞书把 Encrypt Key 留空即明文，零依赖可通）。
    """


class AttachmentUnsupported(RuntimeError):
    """这个渠道没实现取件。**是能力缺口，不是运行时故障**，与上面三条分开的理由同前。

    走到这里说明 ``parse`` 产出了 :class:`Attachment` 而 ``fetch`` 没跟上 ——
    那是我方漏了一半实现，不是对方发来的东西有问题。
    """


class ChannelNotConfigured(RuntimeError):
    """凭证不全。**入站一律拒收，不许降级放行。**

    这一条与 `hiclaw/matrix_bus.py` 的 `from_env` 刻意相反，值得说清楚：
    那边缺凭证降级 ``log_only`` 是对的 —— 出站发不出消息，最坏是没人看见镜像。
    入站不是：缺 token 意味着**签名没法校验**，此时「降级放行」等于在公网上开一个
    无鉴权的口子，谁都能往里塞一条 ``/refund``。所以出站可降级、入站必拒收。
    """


@dataclass(frozen=True)
class WebhookRequest:
    """一次 HTTP 回调的原始形态。故意不依赖任何 web 框架。

    ``headers`` 的键**必须已经小写化**（HTTP 头大小写不敏感，而各平台的文档写法
    各不相同：飞书 ``X-Lark-Signature``、企微用 query 参数）。小写化放在构造处
    而不是每次取值时，是为了让「取不到头」只有一个原因：对方真的没发。
    """

    method: str
    path: str
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""
    query: Mapping[str, str] = field(default_factory=dict)

    def header(self, name: str, default: str = "") -> str:
        return self.headers.get(name.lower(), default)

    def json(self) -> dict:
        """解析 body。**只在 ``verify`` 通过之后调**（见模块抬头）。

        解析失败返回空 dict 而不抛：走到这里说明签名已经过了，是自家平台发来的，
        而一条解析不了的回调不该掀掉整个 webhook 进程 —— 后面 ``parse`` 会因为
        取不到消息字段而返回 None，落进「非消息事件」那条正常路径。
        """
        if not self.body:
            return {}
        try:
            data = json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}


@dataclass(frozen=True)
class Attachment:
    """一份附件的**取件凭据** —— 我知道群里发了个东西，钥匙在这，但还没去取。

    刻意不带字节，也不带 digest。三个平台的回调里都只给一把钥匙（飞书 ``image_key``、
    企微 ``media_id``、Matrix ``mxc://``），真正的字节要拿着凭证再发一次请求去拉，
    而拉取会失败、会超时、会被体积上限拒掉。把「有这么个东西」与「已经拿到手了」
    分成两个类型（后者是 `attachments.StoredAttachment`），是为了让**没下载成功却
    当成证据挂上去**这件事在类型上就不可能发生 —— 那种错的症状是案子里躺着一条
    指向空气的 uri，而 `customer_evidence` 只存引用、看不出引用是死的。

    ``msg_ref`` 是渠道私有的取件参数（飞书要 message_id + file_key + type，
    Matrix 只要 mxc）。放一个不透明 dict 而不是给每个平台加字段，理由同
    :class:`OutboundMessage` 的 ``meta``：router 不解读它，原样交回给 adapter。
    """

    channel: str
    #: 取件的钥匙本身。进日志，所以不放任何凭证 —— 凭证在 adapter 的 config 里。
    file_key: str
    #: ``image`` / ``file``。平台自报，仅用于选取件接口；真类型以落盘时嗅探为准。
    kind: str = "image"
    filename: str = ""
    #: 平台自报的 MIME。**不可信也不采信** —— 落盘的类型只由内容嗅探决定
    #: （见 `attachments.sniff_mime`），这一列仅用于把拒收原因说清楚。
    mime: str = ""
    #: 平台自报的体积。用来在**下载前**就拒掉超大附件，省掉那一次没必要的出网。
    size: int = 0
    msg_ref: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class InboundMessage:
    """一条**平台无关**的入站消息。router 只认这个形状。

    ``raw`` 带 ``repr=False``：回调原文里有 open_id、手机号这类东西，而这个对象
    会被日志打印。要看原文去读 event_log 的 detail，那里过了脱敏。

    ``attachments`` 与 ``text`` 是**并列**的，不是二选一：一条消息可以只有图
    （群里甩一张破损照片）、只有字（``/refund ORD-1``），也可以两者都有（飞书的
    图文混排 ``post``）。所以 router 的入口要先收附件、再看命令，而不是
    ``if text: ... else: ...`` —— 后者会让「带一句话的照片」丢掉图。
    """

    channel: str
    chat_id: str
    sender: str
    text: str
    msg_id: str
    ts: str = ""
    raw: dict = field(default_factory=dict, repr=False)
    attachments: tuple[Attachment, ...] = ()

    @property
    def dedup_key(self) -> str:
        """幂等键。**三个平台都会重推同一条消息，这不是异常而是约定**：

        飞书 1s / 20s / 60s 三次，企业微信 5s 内 3 次 —— 只要我方没在超时内回
        200 就重推。而本层收到一条 ``/refund`` 是要**真跑一次退款处置**的，
        跑一次要几秒，正好落在重推窗口里。不去重的症状不是报错，是**同一单退三次**。

        走 `Store.claim_idempotency` 而不是进程内 set：webhook 进程重启后
        set 就空了，而平台的重推能跨越那次重启。
        """
        return f"ingress:{self.channel}:{self.msg_id}"


@dataclass(frozen=True)
class OutboundMessage:
    """一条出站消息。``html`` 给支持富文本的渠道，收不动的 adapter 自行忽略。

    ``meta`` 是**回信所需的渠道私有参数**，由 router 从对应的 :class:`InboundMessage`
    原样带回来。目前只有一个用户：微信客服回消息必须指明 ``open_kfid``（一个企业
    可以开多个客服账号），而那个 id 只在收到的那条消息里有。

    让 adapter 自己在内存里记 ``external_userid -> open_kfid`` 也能跑，但那份映射
    活不过进程重启，症状是重启后回信报「invalid open_kfid」而收消息一切正常。
    """

    chat_id: str
    text: str
    html: str = ""
    meta: Mapping[str, str] = field(default_factory=dict)


class ChannelAdapter(Protocol):
    """一个 IM 平台的接入面。加平台 = 实现这四个方法 + 在 `server.py` 注册一条路由。

    ``name`` 是 :data:`CHANNELS` 里的值，不是显示名 —— 它进幂等键和 event_log。

    第五个方法 ``fetch``（取附件字节）是**按需**的：只在该平台的 ``parse`` 会产出
    :class:`Attachment` 时才必须实现。不实现的平台照旧只跑文本命令面，一行不动。
    """

    name: str

    @property
    def configured(self) -> bool:
        """凭证是否齐全。不齐 -> `server.py` 直接 503，不进 ``verify``。"""
        ...

    def verify(self, req: WebhookRequest) -> None:
        """校验失败抛 :class:`VerifyError`；通过则安静返回。"""
        ...

    def challenge(self, req: WebhookRequest) -> str | None:
        """是 URL 验证请求就返回该原样回给平台的响应体，否则 None。"""
        ...

    def parse(self, req: WebhookRequest) -> list[InboundMessage]:
        """解包成统一消息。不是文本消息事件（如「进群」「已读」）返回空列表。"""
        ...

    def send(self, msg: OutboundMessage) -> None:
        """把消息发回会话。发送失败抛异常，由调用方决定要不要吞。"""
        ...

    def fetch(self, att: Attachment) -> bytes:
        """按取件凭据拉回字节。**按需实现**，不实现就抛 :class:`AttachmentUnsupported`。

        抛异常而不是返回 ``b""``：空字节会被当成「拉到了一个 0 字节的文件」一路
        往下走，最后落成一条指向空内容的证据。而这一层最不该发生的就是**看起来
        成功了的失败** —— 群里发了图、系统回了「已收到」、案子里那条 uri 是死的。
        """
        raise AttachmentUnsupported(f"{getattr(self, 'name', '?')} 渠道未实现附件取件")


def coerce_headers(raw: Mapping[str, Any]) -> dict[str, str]:
    """把任意来源的头小写化成 :class:`WebhookRequest` 要的形状。

    单拎出来是因为三处都要用（server、测试、各平台的联调脚本），而漏掉小写化的
    症状是「签名头取不到 -> 每一条都判 401」—— 看起来像密钥配错了。
    """
    return {str(k).lower(): str(v) for k, v in raw.items()}
