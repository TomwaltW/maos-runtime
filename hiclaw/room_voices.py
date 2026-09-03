"""退款圆桌的发声面 —— 五个岗位各自的一张嘴（T84）。

房间里原本只有 `maos-bot` 一个身份在说话，四岗全靠 `【岗位 · agent_id】` 前缀区分
（`hiclaw/ap_room.py::render_speech`）。本模块让每个岗位用**自己的 Matrix 账号**发言：
Element 里五个头像、五个显示名，谁说的一眼可见。缺号时自动退回名牌形态。

三条不变量，本文件所有取舍都从它们推出来：

1. **只发不听（红线 R3）。** 岗位账号的通道是 `matrix_bus.open_channel(config)` **本尊**，
   构造后**永不调 `listen`** —— `_NioChannel.__init__` 只做 whoami + 查加密、不 sync，
   所以它天然就是 send-only。这里**不写第二套 nio 代码**。
   一个房间只该有一个监听者（`hiclaw.room_ingress`，`maos-bot`）：岗位号的发言若被
   监听方喂进 `on_message`，闲聊回复器会去接一句、岗位号下一轮再接 —— 两个机器人
   互相接龙刷屏。监听侧的防线是 `MAOS_ROOM_BOTS`，由 `open_channel` **现读**、进
   `should_deliver` 的忽略名单；本模块**不负责**那一侧，只负责把号建起来说话。
   🔴 send-only 通道的 `alive()` 恒 False，**绝不能**交给 `room_ingress.serve()` ——
   那个函数见 False 即判 `EXIT_NO_ROOM`。

2. **缺号不沉默。** 缺 USER/TOKEN、token 失效、号没进房间（构造期查加密会当场炸），
   一律退化为「经主通道发言 + 名牌」，**不抛、不 EXIT**。房间是旁路：少一个头像
   是体验问题，五岗集体哑掉才是事故。启动时打一行谁独立、谁代言、为什么。

3. **token 只进 env，不进任何输出（铁律 6 / 红线 R5）。** `describe()` 与本模块每一条
   日志都过 :func:`_redact`：把本次真读到的 token 值反向抹掉。这是**出口兜底**不是
   风格 —— 判断「这条异常消息里会不会带上 token」是在赌上游措辞，而赌错的代价是
   密钥进回执、进日志、进 evidence，且当场不报错。

发言的 `html` 一律先 `html.escape`：`_NioChannel.send` 把 `formatted_body` 原样塞进去
不转义，而 `ap_room.render_speech` 没做这一步 —— 模型吐出一个 `<` 或 `&` 就能破掉
整条消息的 HTML。那是本模块要避开的坑，不是要抄的形态。

本模块**不 import `maos.roundtable`**：那是调用方（T87），方向反了就成了循环依赖。
"""

from __future__ import annotations

import html
import logging
import os
from typing import Any, Protocol

from hiclaw.matrix_bus import (ENV_HOMESERVER, ENV_ROOM_ID, ENV_TOKEN, ENV_USER,
                               MatrixBusConfig, describe_exc, open_channel)

log = logging.getLogger("maos.room_voices")

#: 岗位账号 env 键名的前缀与两个后缀。逐字对齐跨轨契约 §1.2 与
#: `deploy/synapse/add_agents.sh` 写出来的那份 `~/.maos-matrix/agents.env`。
ENV_AGENT_PREFIX = "MAOS_AGENT_"
ENV_AGENT_USER_SUFFIX = "_USER"
ENV_AGENT_TOKEN_SUFFIX = "_TOKEN"

#: 脱敏占位。同 `scripts/matrix_probe.py::REDACTED`。
REDACTED = "***"


def env_keys_of(agent_id: str) -> tuple[str, str]:
    """``agent_id`` -> ``(USER 键名, TOKEN 键名)``。**全仓只此一份推导。**

    ``refund-intake`` -> ``("MAOS_AGENT_REFUND_INTAKE_USER",
    "MAOS_AGENT_REFUND_INTAKE_TOKEN")``。

    别处再抄一份的代价不是重复，是**漂**：建号脚本按一份规则写文件、发声面按另一份
    读，改一处漏一处的症状是「五个号都建好了、房间里却全在代言」，而它不报错。
    建号脚本是 shell、没法 import 这个函数，所以那边的字面量与本函数一起改
    （已记 `docs/DECISIONS.md` 的 `## task-T84`）。
    """
    key = agent_id.upper().replace("-", "_")
    return (f"{ENV_AGENT_PREFIX}{key}{ENV_AGENT_USER_SUFFIX}",
            f"{ENV_AGENT_PREFIX}{key}{ENV_AGENT_TOKEN_SUFFIX}")


def _redact(text: str, secrets: frozenset[str]) -> str:
    """把本次读到的 token 值从一段文本里抹成 ``***``。出口兜底，见抬头第 3 条。

    只抹**值**、不按键名扫（`matrix_bus.redact` 是按键名的那一种，管的是 JSON 载荷）。
    短串不抹：空串会把每个字符位都替换掉，把一行日志变成一片星号。
    """
    for secret in secrets:
        if len(secret) >= 8:
            text = text.replace(secret, REDACTED)
    return text


class Voice(Protocol):
    """一个岗位的嘴（跨轨契约 §1.3）。

    调用方只交**文本**；转义、名牌、plain/html 两份形态都由 Voice 自己做 ——
    让每个调用点自己拼 HTML，就是让每个调用点自己有机会忘记 escape。
    """

    agent_id: str
    title: str
    #: 用哪个 mxid 说话。代言时是主通道那个（`maos-bot`）。
    user_id: str
    #: True = 用自己的 Matrix 账号发；False = 借主通道 + 名牌。
    own_identity: bool

    def say(self, text: str) -> None: ...


class VoiceSet(Protocol):
    """五岗的嘴的集合（跨轨契约 §1.3）。"""

    def voice(self, agent_id: str) -> Voice: ...

    def bot_users(self) -> frozenset[str]: ...

    def describe(self) -> str: ...

    def close(self) -> None: ...


class RoomVoice:
    """:class:`Voice` 的实现。一条通道 + 一份身份，`say` 渲染两份形态再发。

    `say` **可以抛**（同 `MirrorChannel.send`）：房间是旁路这件事由调用方兑现
    —— T87 的圆桌用 `try/except Exception` 包住每一次 `say`，一岗发不出去不影响
    下一岗。在这里吞掉异常会让「五岗全哑」表现成「五岗都发成功了」。
    """

    def __init__(self, *, agent_id: str, title: str, user_id: str,
                 own_identity: bool, channel: Any) -> None:
        self.agent_id = agent_id
        self.title = title
        self.user_id = user_id
        self.own_identity = own_identity
        self._channel = channel

    def _render(self, text: str) -> tuple[str, str]:
        """一条发言的房间形态 ``(plain, html)``。逐字按契约 §1.3。

        独立账号**不加名牌**：账号的显示名就是岗位名（建号时 `PUT displayname` 设过），
        再加一遍 `【岗位 · 工号】` 是把同一件事说两次。代言时才需要名牌 —— 那一刻
        房间里看到的头像是 `maos-bot`，不写清是谁说的就分不出来。
        """
        esc = html.escape
        if self.own_identity:
            return text, f"<p>{esc(text)}</p>"
        return (f"【{self.title} · {self.agent_id}】 {text}",
                f"<p><strong>{esc(self.title)}</strong> "
                f"<code>{self.agent_id}</code><br/>{esc(text)}</p>")

    def say(self, text: str) -> None:
        plain, html_body = self._render(text)
        self._channel.send(plain, html_body)


class RoomVoices:
    """:class:`VoiceSet` 的实现。由 :func:`open_voices` 装配，别直接构造。"""

    def __init__(self, *, voices: dict[str, RoomVoice], main_channel: Any,
                 main_user_id: str, titles: dict[str, str],
                 reasons: dict[str, str], own_channels: list[Any],
                 secrets: frozenset[str]) -> None:
        self._voices = voices
        self._main_channel = main_channel
        self._main_user_id = main_user_id
        self._titles = titles
        self._reasons = reasons
        self._own_channels = own_channels
        self._secrets = secrets

    def voice(self, agent_id: str) -> Voice:
        """任何 ``agent_id`` 都返回一个 Voice —— 没登记过的**现造一个代言的**，不抛。

        不抛是刻意的：这个方法在圆桌的发言循环里被调，抛一次就把整轮圆桌掀了，
        而代价只是房间里少一句话。造出来的存回去，同一个 id 两次拿到同一个对象
        （身份对象在一轮发言里漂移，读日志的人无从分辨）。
        """
        got = self._voices.get(agent_id)
        if got is None:
            got = RoomVoice(agent_id=agent_id,
                            title=self._titles.get(agent_id, agent_id),
                            user_id=self._main_user_id, own_identity=False,
                            channel=self._main_channel)
            self._voices[agent_id] = got
            self._reasons[agent_id] = "未登记的 agent_id"
        return got

    def bot_users(self) -> frozenset[str]:
        """已接通的**独立账号** mxid 集合。代言岗不在内（它们用的是主通道那个 mxid）。

        这份集合正是监听侧要忽略的那批 sender —— 建号脚本把它写成
        ``MAOS_ROOM_BOTS``，`open_channel` 现读进 `should_deliver`。
        """
        return frozenset(v.user_id for v in self._voices.values()
                         if v.own_identity and v.user_id)

    def describe(self) -> str:
        """启动时那一行：谁独立、谁代言、为什么。**只报键名与 mxid，不报 token。**"""
        own = [v for v in self._voices.values() if v.own_identity]
        proxy = [v for v in self._voices.values() if not v.own_identity]
        parts = [f"房间发声面：{len(self._voices)} 岗，"
                 f"{len(own)} 岗独立账号、{len(proxy)} 岗经主通道 "
                 f"{self._main_user_id or '（未知 mxid）'} 代言"]
        if own:
            parts.append("独立：" + "、".join(
                f"{v.agent_id}={v.user_id}" for v in own))
        if proxy:
            parts.append("代言：" + "、".join(
                f"{v.agent_id}（{self._reasons.get(v.agent_id, '原因未记')}）"
                for v in proxy))
        return _redact("；".join(parts), self._secrets)

    def close(self) -> None:
        """关掉每一条**独立**通道。**不关主通道** —— 那是调用方开的，归调用方关。

        每条都单独包住：一条关不掉不该让后面几条漏着（每条通道背后是一个私有
        事件循环加一条守护线程，漏一条就多一份永不退出的后台，而它不报错）。
        """
        for channel in self._own_channels:
            try:
                channel.close()
            except Exception as exc:                    # noqa: BLE001 —— 收口失败无所谓
                log.warning("岗位通道关闭异常（已忽略）：%s",
                            _redact(describe_exc(exc), self._secrets))
        self._own_channels = []


def open_voices(main_channel: Any, *, agent_ids: tuple[str, ...],
                titles: dict[str, str] | None = None,
                env: dict | None = None) -> RoomVoices:
    """按 env 里的岗位账号逐个开通道，开不成的退化为代言。**不抛、不 EXIT。**

    ``env`` 缺省 ``os.environ``；homeserver / room_id 取 ``MATRIX_HOMESERVER`` /
    ``MATRIX_ROOM_ID``（与主通道同一个房间）。`title` 缺省 = `agent_id`。

    每岗一条 `_NioChannel` = 一个私有事件循环 + 一条守护线程 + 两个启动 GET
    （whoami、查加密）。五岗就是 5 线程 / 10 个 GET，都在**启动**那一刻，不吃
    `rc_message` 限流（那是发消息侧的）。演示规模可接受；合并成一个多账号 client
    归以后（已记 `docs/BACKLOG.md` 的 `## task-T84`）。
    """
    src: Any = os.environ if env is None else env
    title_map = dict(titles or {})
    main_user_id = (src.get(ENV_USER) or "").strip()
    homeserver = (src.get(ENV_HOMESERVER) or "").strip()
    room_id = (src.get(ENV_ROOM_ID) or "").strip()

    voices: dict[str, RoomVoice] = {}
    reasons: dict[str, str] = {}
    own_channels: list[Any] = []
    secrets: set[str] = set()

    for agent_id in agent_ids:
        user_key, token_key = env_keys_of(agent_id)
        user = (src.get(user_key) or "").strip()
        token = (src.get(token_key) or "").strip()
        if token:
            secrets.add(token)
        title = title_map.get(agent_id, agent_id)

        # 逐岗构造，**不给 MatrixBusConfig 加任何字段**（C-6 冻结：
        # `docs/parallel/contracts.md` 的「MatrixBusConfig」一节）。
        # from_env 缺必填项自己会降级 log_only=True 且不抛 —— 判据借它的，
        # 但它那条 WARNING 报的是 MATRIX_* 键名，对岗位账号是**错的指向**，
        # 所以下面自己再打一条带真键名的。
        config = MatrixBusConfig.from_env(env={ENV_HOMESERVER: homeserver,
                                               ENV_ROOM_ID: room_id,
                                               ENV_USER: user,
                                               ENV_TOKEN: token})
        if config.log_only:
            missing = [name for name, value in ((user_key, user), (token_key, token),
                                                (ENV_HOMESERVER, homeserver),
                                                (ENV_ROOM_ID, room_id)) if not value]
            reasons[agent_id] = "缺 " + "、".join(missing)
            log.info("岗位 %s 经主通道代言：%s", agent_id, reasons[agent_id])
            voices[agent_id] = RoomVoice(agent_id=agent_id, title=title,
                                         user_id=main_user_id, own_identity=False,
                                         channel=main_channel)
            continue

        try:
            channel = open_channel(config)
        except Exception as exc:                        # noqa: BLE001 —— 见抬头第 2 条
            # 常态路径，不是异常路径：token 过期、号还没进房间（构造期查加密会拿到
            # 403 并被判成「房间状态查询失败」）都落这里。措辞要说清是**哪个岗**、
            # 用的是**哪个键**，不然读日志的人只知道「有个通道没开」。
            reasons[agent_id] = f"{token_key} 未接通：{describe_exc(exc)}"
            log.warning("岗位 %s（%s）改由主通道代言：%s", agent_id, user or user_key,
                        _redact(reasons[agent_id], frozenset(secrets)))
            voices[agent_id] = RoomVoice(agent_id=agent_id, title=title,
                                         user_id=main_user_id, own_identity=False,
                                         channel=main_channel)
            continue

        own_channels.append(channel)
        voices[agent_id] = RoomVoice(agent_id=agent_id, title=title,
                                     user_id=user, own_identity=True, channel=channel)

    voice_set = RoomVoices(voices=voices, main_channel=main_channel,
                           main_user_id=main_user_id, titles=title_map,
                           reasons=reasons, own_channels=own_channels,
                           secrets=frozenset(secrets))
    log.info("%s", voice_set.describe())
    return voice_set
