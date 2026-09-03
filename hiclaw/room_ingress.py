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
  · 不落库：幂等表在 `:memory:`，待办与暂存证据随进程消失（口径同 `run_ingress`）。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from html import escape as _esc

from maos.core.store import SqliteStore
from maos.ingress.chat import ChatResponder
from maos.ingress.contracts import CHANNEL_MATRIX, Attachment, InboundMessage, OutboundMessage
from maos.ingress.router import DEFAULT_LEDGER, IngressRouter, render_roster
from hiclaw.matrix_bus import MatrixBusConfig, describe_exc, open_channel

log = logging.getLogger("maos.room_ingress")

EXIT_OK = 0
EXIT_NO_ENV = 2
#: 与 `room_demo` / `ap_room` 同一个数：要了房间却没进去，不许 exit 0。
EXIT_NO_ROOM = 4

BAR = "=" * 68


#: 一条房间消息最多带多少字符的正文。Matrix 一条事件上限 64 KB，而正文要以 ``<pre>``
#: 再抄一遍进 formatted_body，加上转义与 JSON 开销，纯文本留 20 K 字符是安全线。
#: 超过就拆成几条 —— 发不出去的症状（Synapse 回 413）与「机器人挂了」无法分辨。
CHUNK_CHARS = 20_000


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
        # 回帖是对齐好的多行文本，<pre> 保住缩进；没给 html 的一律走这条。
        # 长回帖拆成几条发：一条 Matrix 事件 64 KB 上限，超了 Synapse 回 413，
        # 而 router 只会把发送失败记进日志 —— 房间里就是一片安静。
        parts = split_message(msg.text)
        for i, part in enumerate(parts, 1):
            head = f"（{i}/{len(parts)}）\n" if len(parts) > 1 else ""
            self._channel.send(head + part, f"<pre>{_esc(head + part)}</pre>")

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


def _build_team(model, voices):                        # noqa: ANN001
    """圆桌本体（`RefundRoundtable`）。没装载返回 ``None``。"""
    try:
        from maos.roundtable.team import RefundRoundtable
    except ImportError as exc:
        log.warning("圆桌引擎未装载，单机器人模式（%s）", exc)
        return None
    return RefundRoundtable(model, voices)


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

    def say(self, text: str) -> None:
        plain = f"【{self.title} · {self.agent_id}】 {text}"
        html = (f"<p><strong>{_esc(self.title)}</strong> "
                f"<code>{self.agent_id}</code><br/>{_esc(text)}</p>")
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


def wire(channel, *, room_id: str, ledger_path=DEFAULT_LEDGER,   # noqa: ANN001
         chat: ChatResponder | None = None, team=None) -> IngressRouter:
    """把一条房间通道接到一个新 router 上，并把两个回调挂到 ``listen``。

    单拎出来是为了能用假通道测：真的 `_NioChannel` 要 Synapse。
    ``team`` 原样透传给 router，缺省不接 —— 接不接圆桌是 `main` 的决定。
    """
    adapter = MatrixRoomAdapter(channel)
    store = SqliteStore(":memory:")
    store.init_schema()
    router = IngressRouter({adapter.name: adapter}, store=store,
                           ledger_path=ledger_path, chat=chat, team=team)
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
        team = _build_team(chat.model if chat.live else None, voices)
        if team is not None:
            roster = team.roster()

    print(f"{BAR}\n已进房间 {config.room_id}，身份 {config.user}")
    print(chat.describe())
    if args.no_team:
        print("圆桌：按 --no-team 关闭（单机器人模式）")
    elif team is None:
        print("圆桌：未装载（单机器人模式，命令面与申请表照常可用）")
    else:
        print(voices.describe())
        print("圆桌岗位与技能：")
        print(render_roster(roster))
    print(f"附件落盘：{os.environ.get('MAOS_ATTACHMENT_DIR') or 'var/attachments'}（不进 git）")
    print("在 Element 里说话、拖申请表 / 照片、或打 /help。Ctrl-C 退出。")
    print(BAR, flush=True)

    ledger = args.ledger or DEFAULT_LEDGER
    try:
        wire(channel, room_id=config.room_id, ledger_path=ledger, chat=chat, team=team)
        if not args.quiet_start:
            if team is not None:
                seats = " → ".join(str(s.get("title") or s.get("agent_id"))
                                   for s in roster)
                announce(channel,
                         f"MAOS 退款圆桌已上线（5 岗：{seats}）。"
                         "/refund 或拖申请表起单，五岗依次发言；/team 看岗位与 skill")
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
