"""入站附件 —— 把「群里发的那张照片」变成一份可调阅、可校验的证据。

业务侧早就为这件事留好了口：`refund.intake` 的证据判据是「信号带 `uri` 就是证据」
（不按 kind 白名单挑），`customer_evidence` 表存的也是 `uri` + `digest` 两列引用。
但那个 uri 一直指向**不存在的东西** —— 语料里的 `oss://...` 是写死的字符串，
系统里没有任何一处能把字节真的存下来。本模块补的就是这一段。

## 内容寻址，不是按消息 id 存

落盘路径由内容的 sha256 决定（`<root>/<ab>/<abcdef...>`），不是由 message_id 决定。
三个理由，都是这条链路上真会发生的：

  · **同一张图会重复进来。** 平台重推、客户在两个群里各发一次、先发图后补一句
    `/refund` 又重发一遍 —— 按消息 id 存会得到 N 份同样的字节，而按内容存天然是一份。
  · **`digest` 列不再是「算给它看的」。** `refund.intake` 在信号没带 digest 时会
    `C.digest(uri)` 兜一个占位，那东西证明不了「调阅到的还是当初那份」。内容寻址下
    digest 就是文件名，两者必然一致，校验退化成一次 `exists()`。
  · 证据的本质是「有个外部对象可以调阅」。调阅的钥匙应该来自内容本身，
    而不是来自某个 IM 平台的消息 id —— 那个 id 在换平台时就没了。

## 为什么不塞 sqlite BLOB

退款域的 `schema.sql` 全是引用列（`uri`/`digest`），不是字节列。把二进制灌进去要么
改表（碰铁律 1 的表结构面），要么另立一张 BLOB 表，而后者会让每一次 `SELECT *`
的调试输出里躺着几 MB 的乱码。文件系统本来就是内容寻址存储最趁手的形态。

## 图片不进 git

`docs/BACKLOG.md` 2026-08-29 那条记着：`scripts/make_evidence.py::scan_for_secrets`
只扫文本，扫不到 PNG，而且是**静默**跳过 —— 读起来像「已检查通过」。本模块是全仓
第一个会持续往盘上写二进制的地方，所以默认根目录 `var/attachments/` 走 .gitignore：
客户发来的照片里可能有身份证、面单、聊天截图，它们不该有任何一条路径通向 git 历史。

## 暂存为什么在内存里

照片自己不带订单号。人的实际动作是「甩一张图 → 再打一句 `/refund ORD-xxx`」，
中间隔着几秒到几分钟。所以按 `chat_id` 暂存最近收到的附件，等命令来了再挂上去。
这份暂存刻意与 `router.Ticket` 同构 —— 只活在内存、有 TTL、重启即失效：
它承载的是「刚才有人在这个群里发了张图」这个观察，重发一次的成本是一句话，
而落库就得回答「重启后这些没认领的图算谁的、多久清」一整串问题。
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .contracts import Attachment

log = logging.getLogger("maos.ingress.attachments")

#: uri 方案。故意不用 `file://` —— 那会诱导下游直接按本机路径去开，
#: 而存储根目录是可搬的（换机器、换挂载点），能保证稳定的只有 digest 本身。
URI_SCHEME = "maos-attachment"

#: 默认存储根。相对仓库根，走 .gitignore。
ENV_ROOT = "MAOS_ATTACHMENT_DIR"
DEFAULT_ROOT = "var/attachments"

#: 单个附件体积上限。群里一条 200MB 的视频不该把盘写满，而 20MB 足够放下
#: 任何一张手机拍的破损货物照片或一份扫描件。
ENV_MAX_BYTES = "MAOS_ATTACHMENT_MAX_BYTES"
DEFAULT_MAX_BYTES = 20 * 1024 * 1024

#: 允许落盘的类型。**白名单不是黑名单** —— 这一层的输入来自公网回调，
#: 收下一个 .exe 再存起来，等于替攻击者做了一次托管。
ALLOWED_MIME = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "application/pdf": ".pdf",
}

#: 按内容嗅探出的类型，优先于平台自报的 Content-Type。平台报的那个可以被伪造，
#: 也可以只是平台自己填错（飞书对 heic 常报 application/octet-stream）。
_MAGIC = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"%PDF-", "application/pdf"),
)

#: 暂存有效期。比 `router.TICKET_TTL` 短得多：图片是「说话的一部分」，
#: 发完图隔一天再打 /refund，那多半是另一件事了。
BUFFER_TTL = 30 * 60

#: 一个会话最多暂存几张。防的是有人往群里连甩一百张图，然后一句 /refund
#: 把它们全挂到同一个案子上。
BUFFER_MAX = 10

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class AttachmentTooLarge(ValueError):
    """超出体积上限。**不是故障，是拒收** —— 群里要给一句人话回执，不该报异常堆栈。"""


class AttachmentTypeRejected(ValueError):
    """类型不在白名单。同上，拒收要说清是哪一类被拒了。"""


def sniff_mime(data: bytes) -> str:
    """**只按内容**判类型；判不出返回 ``""``。

    刻意不接受平台自报的 MIME 做兜底 —— 那个参数曾经在这里，是个洞：判不出时
    退回 declared，白名单校的就变成了「对方声称自己是什么」。一个 ELF 只要在回调里
    把 mimetype 写成 ``image/png`` 就能落盘，而这一层的输入来自公网回调。
    判不出就拒，是白名单唯一说得通的语义。

    webp 与 heic 的魔数在偏移 4 之后（RIFF/ftyp 容器），单独判：只看头 8 字节
    会把这两类归进「判不出」，而它们恰恰是手机直出照片最常见的两种格式。
    """
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"mif1"):
        return "image/heic"
    return ""


def digest_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def uri_of(digest: str) -> str:
    return f"{URI_SCHEME}://{digest}"


def digest_from_uri(uri: str) -> str:
    """从 uri 取回 digest；不是本方案的 uri 返回 ``""``（外部语料里的 oss:// 就走这条）。"""
    prefix = f"{URI_SCHEME}://"
    if not uri.startswith(prefix):
        return ""
    tail = uri[len(prefix):]
    return tail if _DIGEST_RE.match(tail) else ""


@dataclass(frozen=True)
class StoredAttachment:
    """一份**已经落盘**的附件。与 :class:`Attachment` 的区别就是这个「已经」。

    `Attachment` 是解析回调时得到的**取件凭据**（我知道有这么个东西，钥匙在这），
    落盘之后才有 digest 和 uri。分成两个类型而不是给 `Attachment` 加可空字段，
    是为了让「还没下载」与「下载完了但 digest 是空的」在类型上就不可能混淆。
    """

    digest: str
    uri: str
    mime: str
    size: int
    kind: str
    filename: str
    channel: str
    chat_id: str
    sender: str
    received_at: float = field(default_factory=time.time)

    def as_evidence(self, evidence_id: str) -> dict:
        """翻成 `customer_evidence` 的一行。

        `kind` 用 `image` / `document` 而不是 mime：语料与 `AS-003` 判据用的是
        前者，直接写 `image/jpeg` 会让政策规则匹配不上。
        """
        return {
            "evidence_id": evidence_id,
            "kind": self.kind,
            "uri": self.uri,
            "digest": self.digest,
            "source": f"{self.channel}:{self.sender}",
        }


class AttachmentStore:
    """内容寻址的字节存储。**只写不删** —— 证据的生命周期不由这一层决定。

    删除留给运维（一条按 mtime 的清理），不做成 API：能删证据的代码路径越少越好，
    而「案子结了就删图」这个判断需要知道案子的状态，本层不知道也不该知道。
    """

    def __init__(self, root: str | Path | None = None, *,
                 max_bytes: int | None = None) -> None:
        self.root = Path(root or os.environ.get(ENV_ROOT) or DEFAULT_ROOT)
        self.max_bytes = int(max_bytes if max_bytes is not None
                             else os.environ.get(ENV_MAX_BYTES) or DEFAULT_MAX_BYTES)

    def path_of(self, digest: str) -> Path:
        """两级分片：一个目录下堆十万个文件，`ls` 会卡住，某些文件系统还会退化。"""
        return self.root / digest[:2] / digest

    def exists(self, digest: str) -> bool:
        return bool(_DIGEST_RE.match(digest)) and self.path_of(digest).is_file()

    def read(self, digest: str) -> bytes:
        if not _DIGEST_RE.match(digest):
            raise ValueError(f"不是合法的 sha256 digest：{digest!r}")
        return self.path_of(digest).read_bytes()

    def put(self, data: bytes, att: Attachment, *, chat_id: str = "",
            sender: str = "") -> StoredAttachment:
        """校验 + 落盘，返回已落盘的形态。同内容重复写只落一份。

        校验顺序是**先体积后类型**：类型嗅探要读内容，而超大的那份根本不该被
        当成内容对待。
        """
        if len(data) > self.max_bytes:
            raise AttachmentTooLarge(
                f"附件 {len(data)} 字节，超过上限 {self.max_bytes} 字节")
        if not data:
            raise AttachmentTypeRejected("附件是空的")

        mime = sniff_mime(data)
        if mime not in ALLOWED_MIME:
            # 报里带上对方自报的那个：「你说是 image/png，我看着不像」比
            # 单说「未知类型」更容易让人判断是发错了文件还是我方漏了格式。
            declared = (att.mime or "").split(";")[0].strip() or "未声明"
            raise AttachmentTypeRejected(
                f"不收这个类型：内容判定 {mime or '认不出'}（对方自报 {declared}）；"
                f"只收 {'、'.join(sorted(ALLOWED_MIME))}")

        digest = digest_of(data)
        target = self.path_of(digest)
        if target.is_file():
            log.info("附件已在库，不重复落盘：%s", digest[:12])
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            # 先写临时名再 rename：中途崩掉不会在库里留下一个「文件名声称是这个
            # digest、内容却是半截」的东西 —— 那种损坏靠 exists() 查不出来。
            tmp = target.with_name(f".{digest}.part")
            tmp.write_bytes(data)
            tmp.replace(target)

        return StoredAttachment(
            digest=digest, uri=uri_of(digest), mime=mime, size=len(data),
            kind="document" if mime == "application/pdf" else "image",
            filename=att.filename or f"{digest[:12]}{ALLOWED_MIME[mime]}",
            channel=att.channel, chat_id=chat_id, sender=sender,
        )


class AttachmentBuffer:
    """按 ``(channel, chat_id)`` 暂存最近收到的附件，等一句命令来认领。

    键带上 channel 而不只是 chat_id：三个平台的 chat_id 各有各的取值域，
    飞书的 ``oc_xxx`` 与 Matrix 的 ``!room:server`` 不会撞，但微信客服的
    ``external_userid`` 与企微的 ``chat_id`` 都是不透明字符串，撞了就是
    「A 群的图挂到了 B 群的单子上」，而这种错不会报错。
    """

    def __init__(self, *, ttl: int = BUFFER_TTL, cap: int = BUFFER_MAX) -> None:
        self.ttl = ttl
        self.cap = cap
        self._lock = threading.Lock()
        self._by_chat: dict[tuple[str, str], list[StoredAttachment]] = {}

    @staticmethod
    def _key(channel: str, chat_id: str) -> tuple[str, str]:
        return (channel, chat_id)

    def add(self, item: StoredAttachment) -> None:
        key = self._key(item.channel, item.chat_id)
        with self._lock:
            bucket = [a for a in self._by_chat.get(key, []) if not self._expired(a)]
            # 同一张图重发不叠加：内容寻址下 digest 相同就是同一份证据，
            # 挂两条只会让案子里出现 ev-01 / ev-02 两个一模一样的 uri。
            bucket = [a for a in bucket if a.digest != item.digest]
            bucket.append(item)
            self._by_chat[key] = bucket[-self.cap:]

    def _expired(self, item: StoredAttachment, now: float | None = None) -> bool:
        return (time.time() if now is None else now) - item.received_at > self.ttl

    def peek(self, channel: str, chat_id: str) -> list[StoredAttachment]:
        """看一眼，不认领。给 ``/pending`` 这类只读命令用。"""
        key = self._key(channel, chat_id)
        with self._lock:
            alive = [a for a in self._by_chat.get(key, []) if not self._expired(a)]
            self._by_chat[key] = alive
            return list(alive)

    def claim(self, channel: str, chat_id: str) -> list[StoredAttachment]:
        """取走这个会话暂存的全部附件。**取走即清空**。

        清空是刻意的：不清的话，下一句 `/refund ORD-B` 会把上一单的照片再挂一遍，
        而两个案子引用同一张图这件事，事后没有任何一条记录能解释清楚。
        """
        key = self._key(channel, chat_id)
        with self._lock:
            alive = [a for a in self._by_chat.pop(key, []) if not self._expired(a)]
            return alive
