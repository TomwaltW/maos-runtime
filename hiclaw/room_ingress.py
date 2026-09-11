"""MAOS 退款助手进 Matrix 房间 —— 房间里的一句话 / 一张申请表 / 一张照片，走 ingress 那条链路。

    set -a; . ~/.maos.env; . ~/.maos-matrix/room.env; set +a
    ~/.maos-matrix/venv/bin/python -m hiclaw.room_ingress

补的是 `maos/ingress/` 与 `hiclaw/matrix_bus.py` 之间一直缺的那半截：bus 认得出房间里的
文本与附件，router 会处置命令与申请表，但库里没有任何一个进程把两者接在一起。
真房间实测的三层症状 —— 发 CSV 一声不吭、取件 30s 超时、回帖迟到半分钟 ——
每一层都在这条缺口里。

## 房间里能做什么

  · 打一句话（不是命令）—— 有真模型就由它接一句，只依据本进程算好的事实说话
    （`maos/ingress/chat.py`）；没真模型回固定话术。**不会沉默**。
  · 拖一张退款申请表（CSV）—— 逐行预检，报出每行哪里填错、每单裁定如何、
    怎么放行（`maos/ingress/sheet.py`）。只读，不动钱。
  · 拖一张照片 / PDF —— 收下当证据，等一句 ``/refund`` 认领。
  · ``/refund`` / ``/approve`` / ``/reject`` / ``/pending`` / ``/help`` —— 与飞书群同一套。
  · 钱没退出去那一单的后半程（T122）：``/assign`` 派单、``/resolve`` 交线下凭证关单、
    ``/confirm`` 记客户确认、``/complain`` 记投诉。**处置与这四条命令跑在同一个库上**
    （`IngressRouter.store`），所以工单与到账观察查得到 —— 在这之前处置跑在一个
    用完即弃的 `:memory:` 里，``/assign`` 一定报「没有人工工单」。
  · 装上圆桌之后（缺省就装，``--no-team`` 关）：预检 / 申请表 / 放行三处各让五个岗位
    依次说一句，``/team`` 报一遍谁是谁。圆桌是**旁路** —— 它没装、没账号、没模型
    都只是少几句发言，命令面与申请表一个字不受影响。

## 两个口径，刻意不同（同 `hiclaw/ap_room.py`）

处置（预检、核算、付款）全是规则代码，一个 token 都不花，连跑两次逐字一致。
模型只在「接一句闲聊」这一处出场，且只能复述事实（铁律 8）。所以没配
`MAOS_LLM_*` 也能跑 —— 申请表反馈与命令面不受影响，只是闲聊变成固定话术。
这与 `ap_room` 的 `EXIT_NO_MODEL` 不同：那边对话就是演示本身，这边对话是配菜。

## 不做什么

  · 不接长驻运行时：任务级 ``/approve <task_id>`` 在本进程无处可落（router 会说明）。
    那是 `room_demo` / `ap_room` 的地盘，各起各的进程，别在一个房间里两个 bot 抢答。
  · 待办与暂存证据不落库：它们在进程内存里，随进程消失（口径同 `run_ingress`）。
    **库本身落不落，看 `MAOS_INGRESS_DB`**：不设就是 `:memory:`（演完即散），
    指到一个文件时，这一轮跑过的案子、工单、观察、四判据跑完还在，第二天回查得到。
"""

from __future__ import annotations

import argparse
import inspect
import logging
import os
import sys
import threading
import time
from html import escape as _esc
from urllib.parse import urlsplit

from maos.core.store import SqliteStore
from maos.ingress.chat import ChatResponder
from maos.ingress.contracts import CHANNEL_MATRIX, Attachment, InboundMessage, OutboundMessage
from maos.ingress.router import (DEFAULT_LEDGER, IngressRouter, ensure_room_schema,
                                 render_roster)
from hiclaw.matrix_bus import (MatrixBusConfig, describe_exc, html_block,
                               open_channel, with_actions)

log = logging.getLogger("maos.room_ingress")

EXIT_OK = 0
EXIT_NO_ENV = 2
#: 与 `room_demo` / `ap_room` 同一个数：要了房间却没进去，不许 exit 0。
EXIT_NO_ROOM = 4

BAR = "=" * 68


#: 一条房间消息最多带多少字符的正文。Matrix 一条事件上限 64 KB，而正文要再抄一遍
#: 进 formatted_body（HTML 那份），加上转义与 JSON 开销，纯文本留 20 K 字符是安全线。
#: 超过就拆成几条 —— 发不出去的症状（Synapse 回 413）与「机器人挂了」无法分辨。
CHUNK_CHARS = 20_000

#: 五岗依次发言的节奏（毫秒）。缺省 0 = 一次都不等。
#: 🔴 缺省必须是 0：`maos/tests` 与 `scripts/room_team_smoke.py` 一秒都不许变慢。
ENV_TEAM_PACE_MS = "MAOS_TEAM_PACE_MS"

#: 补件页（`hiclaw/upload_server.py`）。`MAOS_UPLOAD_URL` 是房间里按钮指向的地址
#: （人点开的那个，例如 http://127.0.0.1:8787），`MAOS_UPLOAD_BIND` 是本进程监听的
#: host:port，缺省 `127.0.0.1:<URL 里的端口>`。URL 不配 = 不起补件页、不挂按钮，
#: 房间行为与从前逐字一致 —— 补件页是旁路，缺它只是少一个按钮。
ENV_UPLOAD_URL = "MAOS_UPLOAD_URL"
ENV_UPLOAD_BIND = "MAOS_UPLOAD_BIND"
#: Element 的地址（`deploy/synapse/up.sh` 写进 room.env 的那个），补件页结果页上
#: 「回到房间」那条链接指向它。没配就不给这条链接。
ENV_ELEMENT_URL = "MAOS_ELEMENT_URL"
#: 补件页传上来的字节走这个渠道名取件（`WebUploadAdapter.name`）。
CHANNEL_WEB_UPLOAD = "web-upload"


#: 正文转 formatted_body。实现在 `hiclaw/matrix_bus.py` —— 发声面
#: （`room_voices.RoomVoice._render`）要用同一份，而它 import 不了本模块
#: （本模块 import 它）。两份实现的症状：一边修好了换行、另一边没有，
#: 而房间里两条消息看着一样是从同一个程序发出来的。
_html_block = html_block


def split_message(text: str, limit: int = CHUNK_CHARS) -> list[str]:
    """按行把长回帖拆成若干段，每段不超过 ``limit`` 字符；单行超长就硬切。"""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for line in text.split("\n"):
        while len(line) > limit:                      # 单行就超了：硬切
            if buf:
                chunks.append("\n".join(buf))
                buf, size = [], 0
            chunks.append(line[:limit])
            line = line[limit:]
        if buf and size + len(line) + 1 > limit:
            chunks.append("\n".join(buf))
            buf, size = [], 0
        buf.append(line)
        size += len(line) + 1
    if buf:
        chunks.append("\n".join(buf))
    return chunks


class MatrixRoomAdapter:
    """把 `_NioChannel` 包成 router 认的 `ChannelAdapter` 入站半边。

    只有 ``send`` / ``fetch`` 两个方法有实现 —— router 对非 webhook 渠道只调这两个。
    ``verify`` / ``challenge`` / ``parse`` 是 webhook 的事，Matrix 走长连 sync 没有它们。
    """

    name = CHANNEL_MATRIX
    configured = True

    def __init__(self, channel) -> None:              # noqa: ANN001 —— MirrorChannel + fetch
        self._channel = channel

    def send(self, msg: OutboundMessage) -> None:
        # 回帖是对齐好的多行文本，缩进靠 _html_block 保住；没给 html 的一律走这条。
        # 长回帖拆成几条发：一条 Matrix 事件 64 KB 上限，超了 Synapse 回 413，
        # 而 router 只会把发送失败记进日志 —— 房间里就是一片安静。
        parts = split_message(msg.text)
        for i, part in enumerate(parts, 1):
            head = f"（{i}/{len(parts)}）\n" if len(parts) > 1 else ""
            self._channel.send(head + part, _html_block(head + part))

    def fetch(self, att: Attachment) -> bytes:
        return self._channel.fetch(att)


# --------------------------------------------------------------------------
# 圆桌装配 —— 三个件都可以不在，不在就退回单机器人
# --------------------------------------------------------------------------
# 圆桌引擎（`maos/roundtable/`）与发声面（`hiclaw/room_voices.py`）都是**可选件**。
# 三个 import 一律惰性、一律 ImportError 退化：房间入口是这条链路唯一的常驻进程，
# 让它因为一个旁路组件没装就起不来，等于用「圆桌不在」换来「命令面也没了」。
# 退化的每一档都打一行说明 —— 静默退化与「机器人挂了」无法分辨，那正是本模块要消灭的东西。

def _team_constants():
    """五个岗位的 id 与岗位名（`TEAM_ORDER` / `TITLES`）。没装载返回 ``None``。

    常量只在 `maos/roundtable/team.py` 定义一次，这里**不抄第二份**：抄了之后
    房间里的名牌与圆桌自己认的岗位会各说各话，且两边都不报错（契约 §1.1）。
    """
    try:
        from maos.roundtable.team import TEAM_ORDER, TITLES
    except ImportError as exc:
        log.warning("圆桌引擎未装载，单机器人模式（%s）", exc)
        return None
    return TEAM_ORDER, TITLES


def _open_voices(channel, *, agent_ids, titles):       # noqa: ANN001
    """五个岗位账号的嘴（`hiclaw.room_voices.open_voices`）。没装载返回 ``None``。"""
    try:
        from hiclaw.room_voices import open_voices
    except ImportError as exc:
        log.warning("发声面未装载，圆桌发言全部由 maos-bot 代言（%s）", exc)
        return None
    return open_voices(channel, agent_ids=agent_ids, titles=titles)


def _build_team(model, voices, *, pace=None, upload_link=None):   # noqa: ANN001
    """圆桌本体（`RefundRoundtable`）。没装载返回 ``None``。

    ``pace`` 是发言节奏回调（契约 §3），``upload_link`` 是「上传材料」按钮的地址
    生成器；两个都**按签名探再传**：老版本的圆桌不认这些关键字，直接传会
    `TypeError`，而那一刻的症状是「房间起不来」—— 拿一个观感参数换掉整条命令面，
    比不等更糟。探不到就打一行说清为什么没生效。
    """
    try:
        from maos.roundtable.team import RefundRoundtable
    except ImportError as exc:
        log.warning("圆桌引擎未装载，单机器人模式（%s）", exc)
        return None
    params = inspect.signature(RefundRoundtable).parameters
    kwargs = {}
    if pace is not None:
        if "pace" in params:
            kwargs["pace"] = pace
        else:
            log.warning("圆桌引擎不认 pace= 参数（合议轮尚未并入），%s 本次不生效",
                        ENV_TEAM_PACE_MS)
    if upload_link is not None:
        if "upload_link" in params:
            kwargs["upload_link"] = upload_link
        else:
            log.warning("圆桌引擎不认 upload_link= 参数，房间里不挂「上传材料」按钮，"
                        "%s 本次不生效", ENV_UPLOAD_URL)
    return RefundRoundtable(model, voices, **kwargs)


def upload_config(env: dict | None = None) -> tuple[str, str]:
    """补件页的 ``(按钮地址, 监听地址)``。没配 URL 返回 ``("", "")``。

    监听地址缺省 ``127.0.0.1:<URL 里的端口>``，**不拿 URL 的 host 去 bind**：URL 是
    给人的浏览器用的（可能是 `my-mac.local`、可能是一层反代），本进程该听在哪与它
    无关。要对局域网开，`MAOS_UPLOAD_BIND=0.0.0.0:8787` 自己打开。
    """
    src = os.environ if env is None else env
    url = str(src.get(ENV_UPLOAD_URL) or "").strip().rstrip("/")
    if not url:
        return "", ""
    bind = str(src.get(ENV_UPLOAD_BIND) or "").strip()
    if not bind:
        parts = urlsplit(url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        bind = f"127.0.0.1:{port}"
    return url, bind


def _upload_link_of(base_url: str):                     # noqa: ANN001, ANN202
    """按钮地址生成器。补件页模块没装载就不挂按钮（同其它可选件的退化口径）。"""
    if not base_url:
        return None
    try:
        from hiclaw.upload_server import make_upload_link
    except ImportError as exc:
        log.warning("补件页未装载，房间里不挂「上传材料」按钮（%s）", exc)
        return None
    return make_upload_link(base_url)


class WebUploadAdapter:
    """补件页传上来的字节的**取件件**。router 按附件的 ``channel`` 找到它。

    形状照 `MatrixRoomAdapter`：只有 ``fetch`` 有实质实现。``send`` 是空的 —— 回帖
    走消息本身的渠道（房间），补件页拿到的回执是 `router.handle` 的返回值。
    字节按 key 暂存在内存里，取一次就删：补件页是同步等结果的，取不走的字节
    只会是失败路径留下的，攒着没有意义。
    """

    name = CHANNEL_WEB_UPLOAD
    configured = True

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}
        self._lock = threading.Lock()
        self._n = 0

    def stash(self, data: bytes) -> str:
        with self._lock:
            self._n += 1
            key = f"web-{self._n}-{len(data)}"
            self._blobs[key] = data
        return key

    def fetch(self, att: Attachment) -> bytes:
        with self._lock:
            data = self._blobs.pop(att.file_key, None)
        if data is None:
            raise KeyError(f"补件页没有这份文件：{att.file_key}")
        return data

    def send(self, msg: OutboundMessage) -> None:
        return None


def start_uploads(router: IngressRouter, *, room_id: str, bind: str,
                  back_url: str = ""):                  # noqa: ANN201
    """起补件页，把上传接进 router 收附件那条链路。起不来记 WARNING、返回 ``None``。

    每一次上传都变成一条**房间里的**入站消息（channel=matrix、chat_id=房间），
    只是附件的取件件是 `WebUploadAdapter`。这样 router 那边的一切 —— 落盘、按文件名
    定位订单、找这一单在办的待办、复检、五岗重说一轮、回帖发回房间 —— 与在房间里
    拖一张图逐字同一条路。文件名前缀加订单号，正是 `locate_order` 的第 1 级判据。

    正文留空：写一句「补材料 ORD-xxx」进去，router 会把它当闲聊交给模型接一句，
    多一次模型调用、房间里多一条废话。
    """
    try:
        from hiclaw.upload_server import UploadServer
    except ImportError as exc:
        log.warning("补件页未装载（%s），不起", exc)
        return None

    adapter = WebUploadAdapter()
    # 记住原来占着这个名字的件：起不来要原样放回去，不能把别人的摘掉。
    previous = router.adapters.get(adapter.name)
    router.adapters[adapter.name] = adapter
    seq = {"n": 0}

    def on_upload(req) -> str:                           # noqa: ANN001
        atts = []
        for f in req.files:
            name = f.filename if f.filename.startswith(req.order_id) else f"{req.order_id}-{f.filename}"
            atts.append(Attachment(
                channel=adapter.name, file_key=adapter.stash(f.data),
                kind="image" if f.mime.startswith("image/") else "file",
                filename=name, mime=f.mime, size=len(f.data),
                msg_ref={"via": adapter.name}))
        seq["n"] += 1
        who = req.uploader or "补件页"
        print(f"\n[{who} 从补件页传了 {len(atts)} 份给 {req.order_id}]", flush=True)
        reply = router.handle(InboundMessage(
            channel=CHANNEL_MATRIX, chat_id=room_id, sender=who, text="",
            msg_id=f"web-upload-{seq['n']}", attachments=tuple(atts)))
        if reply:
            print(f"[回帖]\n{reply}\n", flush=True)
        return reply

    def known_orders() -> set[str]:
        return {str(o.get("order_id")) for o in router.ledger().get("order_snapshot", [])
                if o.get("order_id")}

    server = UploadServer(on_upload=on_upload, known_orders=known_orders,
                          max_bytes=router.attachments.max_bytes, bind=bind,
                          back_url=back_url)
    try:
        server.start()
    except OSError as exc:
        log.warning("补件页起不来（%s: %s），房间里的按钮会打不开", type(exc).__name__, exc)
        if previous is None:
            router.adapters.pop(adapter.name, None)
        else:
            router.adapters[adapter.name] = previous
        return None
    return server


def _load_decide():
    """合议引擎 `decide()`（契约 §2）。没装载返回 ``None``。

    这是「三个件都可以不在」的**第四个件**：`decide is None` 时房间照常五岗
    发言，只是最后没有那张收口卡 —— 与「圆桌未装载」同一档退化。
    """
    try:
        from maos.roundtable.verdict import decide
    except ImportError as exc:
        log.warning("合议引擎未装载，房间里只有五岗发言、没有收口卡（%s）", exc)
        return None
    return decide


def pace_ms(env: dict | None = None) -> int:
    """从 env 读发言节奏毫秒数。**解析失败按 0 并记一行 WARNING，绝不抛。**

    这个值只影响观感。为了一个观感参数让常驻入口起不来（房间里命令面、申请表
    一起没了），是拿主路赔旁路 —— 红线 R4 的同一条道理。
    """
    src_env: dict = os.environ if env is None else env       # noqa: ANN001
    raw = (src_env.get(ENV_TEAM_PACE_MS) or "").strip()
    if not raw:
        return 0
    try:
        ms = int(raw)
    except ValueError:
        log.warning("%s=%r 不是整数，按 0（不等）处理", ENV_TEAM_PACE_MS, raw)
        return 0
    if ms < 0:
        log.warning("%s=%d 是负数，按 0（不等）处理", ENV_TEAM_PACE_MS, ms)
        return 0
    return ms


def make_pace(ms: int, sleep=time.sleep):              # noqa: ANN001
    """毫秒数 -> 契约 §3 的 ``pace(i, total)`` 回调。``ms <= 0`` 返回 ``None``。

    返回 ``None`` 而不是一个「睡 0 秒」的函数：缺省路径上连一次函数调用都不该
    发生（契约 §3 说的是「一次都不调」）。

    🔴 `time.sleep` 写在 **hiclaw 这一侧**：`maos/roundtable/` 里不许 import
    `time`（契约 §3）。平台无关层只负责给一个可以插进去的点，等多久是房间的事。
    """
    if ms <= 0:
        return None
    seconds = ms / 1000.0

    def _pace(i: int, total: int) -> None:
        del i, total                                   # 每一岗之后都等一样久
        sleep(seconds)

    return _pace


class _ProxyVoice:
    """一个岗位借 ``maos-bot`` 的主通道说话，靠名牌区分是谁在说。

    形态与 `ap_room.render_speech` 同构，**但两份都过 `html.escape`** —— 那边没转义，
    模型吐一个 ``<`` 或 ``&`` 就能把 ``formatted_body`` 破掉，而 Synapse 不会报错，
    房间里看到的是半句话。契约 §1.3 点名这是要避开的坑，不是要抄的形态。
    """

    #: 借的是别人的号，不是自己的（契约 §1.3）。房间里那个名牌就是靠它决定加不加。
    own_identity = False

    def __init__(self, channel, agent_id: str, title: str,   # noqa: ANN001
                 user_id: str) -> None:
        self._channel = channel
        self.agent_id = agent_id
        self.title = title or agent_id
        self.user_id = user_id

    def _render(self, text: str) -> tuple[str, str]:
        plain = f"【{self.title} · {self.agent_id}】 {text}"
        # 正文走 `html_block`（转义 + 换行 + 缩进），与 `room_voices.RoomVoice._render`
        # 同一份 —— 这是兜底形态，不该比正主少一样。
        html = (f"<p><strong>{_esc(self.title)}</strong> "
                f"<code>{self.agent_id}</code><br/>{html_block(text)}</p>")
        return plain, html

    def say(self, text: str) -> None:
        plain, html = self._render(text)
        self._channel.send(plain, html)

    def say_with_actions(self, text: str, actions) -> None:   # noqa: ANN001
        """发言 + 按钮。渲染在 `matrix_bus.with_actions` 一处，与独立账号那张嘴
        （`room_voices.RoomVoice.say_with_actions`）共用 —— 兜底形态不该少一样。"""
        plain, html = with_actions(*self._render(text), actions)
        self._channel.send(plain, html)


class _ProxyVoiceSet:
    """全员代言的 `VoiceSet`：发声面不在、或一个岗位账号都没配好时用它。

    这是**兜底形态**，不是 `hiclaw/room_voices.py` 的替身：它没有独立账号、
    不开第二条通道、也不需要 Synapse。房间里照样看得见五个名牌依次发言，
    只是全挂在 ``maos-bot`` 头上 —— 启动那一行会把这件事说清楚。
    """

    def __init__(self, channel, *, agent_ids, titles=None,   # noqa: ANN001
                 user_id: str = "") -> None:
        self._channel = channel
        self._agent_ids = tuple(agent_ids)
        self._titles = dict(titles or {})
        self._user_id = user_id
        self._voices: dict[str, _ProxyVoice] = {}

    def voice(self, agent_id: str) -> _ProxyVoice:
        """**任何** agent_id 都给一张嘴，不抛（契约 §1.3）。

        名单外的 id 也照发：圆桌那边要是多了一岗，房间里看得见比一句都不发好，
        而「少了一岗」这种事没人会去翻日志。
        """
        if agent_id not in self._voices:
            self._voices[agent_id] = _ProxyVoice(
                self._channel, agent_id, self._titles.get(agent_id, agent_id),
                self._user_id)
        return self._voices[agent_id]

    def bot_users(self) -> frozenset[str]:
        """恒空集 —— 一个独立账号都没接通，监听面就没有要忽略的 sender（红线 R3）。"""
        return frozenset()

    def describe(self) -> str:
        """启动那一行。只报岗位名与代言用的 mxid，**不报 token**（铁律 6 / 红线 R5）。"""
        seats = "、".join(f"{self._titles.get(a, a)}({a})" for a in self._agent_ids)
        return (f"圆桌发声：{len(self._agent_ids)} 岗全部由 "
                f"{self._user_id or 'maos-bot'} 代言带名牌 —— {seats}")

    def close(self) -> None:
        """没开过自己的通道，主通道由 `main` 的 finally 关。"""


class _ChairTeam:
    """给圆桌加一个主席：五岗开口**之前**一句预告，说完**之后**一张收口卡。

    **装饰器，不是替身。** 除了这两件事，任何调用都原样转交给里面那个圆桌
    （`__getattr__` 兜底）—— 圆桌以后长出新方法（@岗位点名问答那种）不用改这里
    一行，而抄一份方法清单的症状是「新方法在房间里静默失踪」。

    三层 try/except，一层都不能省（红线 R4）：预告发不出去不影响五岗发言，
    收口卡发不出去不影响 `/refund` 的回帖与 `/approve` 的处置。房间是旁路。
    """

    def __init__(self, team, channel, *, decide=None, seats: str = "") -> None:  # noqa: ANN001
        self._team = team
        self._channel = channel
        self._decide = decide
        #: 预告里那串岗位名，由 `main` 从 `roster()` 拿来传进来。**这里不抄第二份**：
        #: 抄了之后房间里的名牌与圆桌自己认的岗位会慢慢长歪，且两边都不报错。
        self._seats = (seats or "").strip()

    # -- 主席自己的两件事 ---------------------------------------------------
    def describe(self) -> str:
        """启动那一行：收口卡在不在。静默退化与「机器人挂了」无法分辨。"""
        return ("圆桌收口：合议引擎已装载，五岗说完由 maos-bot 发一张收口卡"
                if self._decide is not None else
                "圆桌收口：合议引擎未装载，五岗照常发言、最后没有收口卡")

    def _notice(self, text: str) -> None:
        """五岗开口前那一句预告（契约 §8 第 4 条的前半）。

        五岗各一次真模型调用要十几秒，期间房间对任何消息都没反应 —— 这一句是
        那十几秒里**唯一**能证明「机器人没挂」的东西。所以它必须在五岗之前发。
        """
        try:
            self._channel.send(text, f"<p><em>{_esc(text)}</em></p>")
        except Exception as exc:                        # noqa: BLE001 —— 观感，不是主路
            log.warning("发言预告没进房间（%s），五岗照常发言", describe_exc(exc))

    def _chair(self, reports, checked: dict, requested_by: str) -> None:
        """五岗说完之后那张收口卡。**由主通道发，不归五岗任何一岗**（契约 §5.2）——
        让财务执行岗来念，房间里会以为这是财务的判断，而它是合议结果。
        """
        if self._decide is None or not reports:
            return
        try:
            from hiclaw.room_voices import render_verdict_card

            verdict = self._decide(reports, case_id=str(checked.get("case_id") or ""))
            plain, html_body = render_verdict_card(
                verdict, mention_user_id=str(requested_by or ""))
            self._channel.send(plain, html_body)
        except Exception as exc:                        # noqa: BLE001 —— 见类抬头
            log.warning("收口卡没进房间（%s），五岗发言与回帖不受影响",
                        describe_exc(exc))

    def _preflight_notice(self, round_no) -> str:
        """预告的措辞：首检与复检读起来必须不一样。

        房间里那句预告是十几秒模型调用期间**唯一**能证明「机器人没挂」的东西。
        复检那一轮如果还是同一句话，boss 分不出「它在重新过这一单」和
        「刚才那条又刷了一遍」—— 而这两件事该做的反应完全不同。

        `round_no` 从 router 那侧来，所以这里按**外部输入**待它：`None`、`"2"`、
        `"第二轮"` 都不许把预告炸掉。炸了不是少一句话 —— 异常会一路抛出这个
        钩子、落进 router 的 except，五岗**整轮**哑掉，房间里只剩一条
        指不到原因的 WARNING。
        """
        try:
            n = int(round_no or 1)
        except (TypeError, ValueError):                 # 不是数字：按首检待它
            n = 1
        if n >= 2:
            return (f"收到新证据，五岗正在复检这一单（第 {n} 轮）"
                    f"{self._seats}，请稍候")
        return f"五岗正在过这一单{self._seats}，请稍候"

    # -- TeamObserver 的两个钩子（其余走 __getattr__ 原样转交）-------------
    def on_preflight(self, **kw):
        """一单预检：预告 -> 五岗 -> 收口卡。

        `**kw` 转发而不是逐个列参数：签名由 router 那侧定，列一遍就是把两处
        绑死，而对不上时是 `TypeError` 落进 router 的 except，房间里一片安静。
        `round_no` 同样**只从 `kw` 里读、不进签名**，理由一模一样：它的形状
        归 router 那侧定，这里只拿它换一句预告的措辞。
        """
        self._notice(self._preflight_notice(kw.get("round_no")))
        reports = self._team.on_preflight(**kw)
        self._chair(reports, kw.get("checked") or {}, kw.get("requested_by") or "")
        return reports

    def on_sheet(self, **kw):
        """一张表：只发预告，**不发收口卡**。

        收口卡的真值表读的是单案五岗的 `data`（契约 §2.1），而读表那一轮每岗
        汇总的是「这批有多少行、多少能过」，键完全不同 —— 拿它去合议，出来的
        是一张看着像结论、实则没有依据的卡。
        """
        rows = kw.get("rows") or []
        self._notice(f"五岗正在过这一批（{len(rows)} 行）{self._seats}，请稍候")
        return self._team.on_sheet(**kw)

    def __getattr__(self, name: str):
        """其余一切原样转交（`on_execute` / `roster` / 以后的 @点名问答）。

        走 `object.__getattribute__` 取 `_team`：直接写 `self._team` 会在
        `_team` 还没赋值时（反序列化、构造期抛异常）无限递归成 RecursionError，
        而那个栈里看不出真正的原因。
        """
        return getattr(object.__getattribute__(self, "_team"), name)


def wire(channel, *, room_id: str, ledger_path=DEFAULT_LEDGER,   # noqa: ANN001
         chat: ChatResponder | None = None, team=None) -> IngressRouter:
    """把一条房间通道接到一个新 router 上，并把两个回调挂到 ``listen``。

    单拎出来是为了能用假通道测：真的 `_NioChannel` 要 Synapse。
    ``team`` 原样透传给 router，缺省不接 —— 接不接圆桌是 `main` 的决定。
    """
    adapter = MatrixRoomAdapter(channel)
    # `MAOS_INGRESS_DB` 指一个文件时，这一轮房间的事件链**跑完还在**（跨轨契约 §F）：
    # `:memory:` 的库随进程消失，于是「五岗到底调了什么、说了什么」演完就没了，
    # 评委只看得到截图。`init_schema()` 全是 `CREATE TABLE IF NOT EXISTS`，
    # 对已经存在的文件库是幂等的 —— 重启一次房间不会清掉上一轮的账。
    store = SqliteStore(os.environ.get("MAOS_INGRESS_DB") or ":memory:")
    # 内核四张表 + 退款域的表与两处加列（T122），四句收在 `ensure_room_schema()` 里
    # （T129 抽的，T136 把本入口也接过去）。**两个房间入口共用这一份**：
    # `scripts/run_ingress.py::_store()` 走的是同一个函数，从前这里自己写四句，
    # 于是同样是「起房间」，两个入口的库形状可以不一样 —— 今天靠 Skill 层懒建表
    # 撑住不崩，但读代码的人会在「`/assign` 在这个入口能用吗」这一问上卡住。
    # 四句的顺序、幂等性、以及「为什么房间这个库必须先有退款域的表」都写在那边。
    ensure_room_schema(store)
    router = IngressRouter({adapter.name: adapter}, store=store,
                           ledger_path=ledger_path, chat=chat, team=team)
    # router 的构造里已经对 `team` 调过一次 `attach_store`，这里是第二道：
    # `wire()` 也接受**不经 router 构造**传进来的圆桌（测试里就这么用），
    # 而漏接的症状是房间照跑、事件表一行没有，没有任何测试会红。
    # `_ChairTeam` 靠 `__getattr__` 把这个方法转交给里面那个真圆桌。
    attach_store = getattr(team, "attach_store", None)
    if attach_store is not None:
        attach_store(store)
    seq = {"n": 0}

    def _next(tag: str) -> str:
        seq["n"] += 1
        return f"matrix-{tag}-{seq['n']}"

    def on_message(sender: str, body: str) -> None:
        print(f"\n[{sender} 说] {body}", flush=True)
        reply = router.handle(InboundMessage(
            channel=adapter.name, chat_id=room_id, sender=sender,
            text=body, msg_id=_next("txt")))
        if reply:
            print(f"[回帖]\n{reply}\n", flush=True)

    def on_attachment(sender: str, att: Attachment) -> None:
        print(f"\n[{sender} 发了附件] {att.filename or att.file_key}"
              f"（平台自报 {att.mime or '未声明'}，{att.size} 字节）", flush=True)
        reply = router.handle(InboundMessage(
            channel=adapter.name, chat_id=room_id, sender=sender,
            text="", msg_id=_next("att"), attachments=(att,)))
        if reply:
            print(f"[回帖]\n{reply}\n", flush=True)

    channel.listen(on_message, on_attachment)
    return router


def announce(channel, plain: str) -> None:            # noqa: ANN001
    """上线那一句。发不出去只记日志 —— 房间是旁路，监听照常起。"""
    try:
        channel.send(plain, f"<p>{_esc(plain)}</p>")
    except Exception as exc:                          # noqa: BLE001
        log.warning("上线说明发送失败（%s），监听照常", describe_exc(exc))


def serve(channel, *, poll: float = 1.0) -> int:      # noqa: ANN001
    """常驻：每隔 ``poll`` 秒看一眼监听还活着没。**监听死了进程就得死。**

    ``sync_forever`` 因异常结束时进程自己不会退出，房间里发什么都没反应 ——
    与「机器人挂了」无法分辨，而模块抬头承诺的是「不会沉默」。所以这里
    以非 0 退出并把原因打到 stderr，让起进程的人（或 supervisor）看得见。
    通道没有 ``alive`` 的（测试里的假通道）就一直等。
    """
    alive = getattr(channel, "alive", None)
    while True:
        if alive is not None and not alive():
            why = getattr(channel, "failure", lambda: "")() or "同步循环结束了"
            print(f"[监听已停] {why}。房间里再发什么都不会有回应，请重新起进程",
                  file=sys.stderr, flush=True)
            return EXIT_NO_ROOM
        time.sleep(poll)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hiclaw.room_ingress",
        description="MAOS 退款助手进 Matrix 房间：闲聊、申请表、照片、命令都在房间里处置")
    parser.add_argument("--ledger", default=None,
                        help="底账路径，缺省 scenarios/custom/ledger.json")
    parser.add_argument("--quiet-start", action="store_true",
                        help="上线时不往房间里发那句说明")
    parser.add_argument("--no-team", action="store_true",
                        help="不接圆桌，只跑单机器人（命令面与申请表照常）")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)-5s %(name)-22s %(message)s")
    # nio 的 INFO 会把每一轮 sync 都打出来，淹掉本进程自己那几行。
    logging.getLogger("nio").setLevel(logging.WARNING)
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    config = MatrixBusConfig.from_env()
    if config.log_only:
        print("MATRIX_HOMESERVER / MATRIX_USER / MATRIX_TOKEN / MATRIX_ROOM_ID 没配齐，"
              "不起监听。\n        先 source 房间配置：  set -a; . ~/.maos-matrix/room.env; set +a",
              file=sys.stderr)
        return EXIT_NO_ENV
    try:
        channel = open_channel(config)
    except Exception as exc:                          # noqa: BLE001
        print(f"[没进房间] {describe_exc(exc)}", file=sys.stderr)
        return EXIT_NO_ROOM

    chat = ChatResponder()
    upload_url, upload_bind = upload_config()

    # 圆桌装配。三个件（岗位常量 / 发声面 / 圆桌本体）缺任意一个都退回单机器人，
    # 房间照常起 —— 命令面与申请表是规则代码，不依赖其中任何一个。
    team = None
    voices = None
    roster: list[dict] = []
    constants = None if args.no_team else _team_constants()
    if constants is not None:
        agent_ids, titles = constants
        # 发声面不在就全员代言：房间里照样五个名牌依次发言，只是都由 maos-bot 说。
        voices = (_open_voices(channel, agent_ids=agent_ids, titles=titles)
                  or _ProxyVoiceSet(channel, agent_ids=agent_ids, titles=titles,
                                    user_id=config.user))
        # 没配 MAOS_LLM_* 就传 None：圆桌对「没模型」的姿态是发事实卡，不是沉默、
        # 更不是刷一句 `{}`（契约 §1.4，与 `ap_room` 的 EXIT_NO_MODEL 刻意不同）。
        team = _build_team(chat.model if chat.live else None, voices,
                           pace=make_pace(pace_ms()),
                           upload_link=_upload_link_of(upload_url))
        if team is not None:
            roster = team.roster()
            # 主席包在圆桌**外面**：名册要先取（`roster()` 走的是内层），
            # 之后交给 router 的才是包好的那一个。
            seats = " → ".join(str(s.get("title") or s.get("agent_id"))
                               for s in roster)
            team = _ChairTeam(team, channel, decide=_load_decide(),
                              seats=f"（{seats}）" if seats else "")

    print(f"{BAR}\n已进房间 {config.room_id}，身份 {config.user}")
    print(chat.describe())
    if args.no_team:
        print("圆桌：按 --no-team 关闭（单机器人模式）")
    elif team is None:
        print("圆桌：未装载（单机器人模式，命令面与申请表照常可用）")
    else:
        print(voices.describe())
        print(team.describe())
        ms = pace_ms()
        print(f"发言节奏：{ENV_TEAM_PACE_MS}="
              + (f"{ms}ms（每岗说完停一下）" if ms else "0（不等，缺省）"))
        print("圆桌岗位与技能：")
        print(render_roster(roster))
        print("拖图片 / PDF 进来补证据：认得出是哪一单就自动复检、五岗重说一轮"
              "（只读复检，放行仍要 /approve）；认不出订单号就先存着，"
              "等下一句 /refund 认领")
    print(f"附件落盘：{os.environ.get('MAOS_ATTACHMENT_DIR') or 'var/attachments'}（不进 git）")
    if upload_url:
        print(f"补件页：{upload_url}/upload（监听 {upload_bind}）—— 缺材料的岗位发言后面"
              "带「上传材料」按钮，点开传文件即自动复检")
    else:
        print(f"补件页：未配 {ENV_UPLOAD_URL}，房间里不挂「上传材料」按钮（拖图进房间照样能补）")
    print("在 Element 里说话、拖申请表 / 照片、或打 /help。Ctrl-C 退出。")
    print(BAR, flush=True)

    ledger = args.ledger or DEFAULT_LEDGER
    uploads = None
    try:
        router = wire(channel, room_id=config.room_id, ledger_path=ledger, chat=chat, team=team)
        if upload_url:
            uploads = start_uploads(router, room_id=config.room_id, bind=upload_bind,
                                    back_url=os.environ.get(ENV_ELEMENT_URL, ""))
        if not args.quiet_start:
            if team is not None:
                seats = " → ".join(str(s.get("title") or s.get("agent_id"))
                                   for s in roster)
                announce(channel,
                         f"MAOS 退款圆桌已上线（5 岗：{seats}）。"
                         "/refund 或拖申请表起单，五岗依次发言；"
                         "拖图片 / PDF 进来补证据，认得出是哪一单就自动复检、"
                         "五岗重说一轮（只读复检，放行仍要 /approve），"
                         "认不出订单号就先存着、等下一句 /refund 认领；"
                         "/team 看岗位与 skill")
            else:
                model = getattr(chat.model, "model", "") if chat.live else ""
                announce(channel,
                         "MAOS 退款助手已上线"
                         + (f"（闲聊由真模型 {model} 接话）" if model else "（未接真模型，闲聊回固定话术）")
                         + "。直接说话、拖退款申请表（CSV）进来逐行预检、"
                           "或发 /help 看命令。")
        return serve(channel)
    except KeyboardInterrupt:
        print("\n停止监听")
        return EXIT_OK
    finally:
        if uploads is not None:
            try:
                uploads.stop()
            except Exception as exc:                  # noqa: BLE001
                log.warning("关补件页失败（%s），继续关通道", describe_exc(exc))
        # 先关五条 send-only 通道再关主通道。关不掉只记 WARNING —— 退出路径上
        # 抛异常会把 `channel.close()` 一起吃掉，那才是真的留下一条活着的 sync。
        if voices is not None:
            try:
                voices.close()
            except Exception as exc:                  # noqa: BLE001
                log.warning("关发声面失败（%s），继续关主通道", describe_exc(exc))
        channel.close()


if __name__ == "__main__":
    raise SystemExit(main())
