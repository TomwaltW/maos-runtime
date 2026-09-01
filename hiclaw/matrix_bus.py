"""HiClaw(Matrix) 镜像总线 —— 装饰器包住进程内 EventBus（冻结契约 C-6）。

读代码前先记住三条不变量，本文件所有取舍都从这三条推出来：

1. **镜像是旁路，不是主路。** `publish` / `subscribe` / `drain` 一律先委托
   inner_bus，再做镜像；镜像抛任何异常都吞掉记日志。房间连不上不该让流水线
   停摆 —— 演示当天 Matrix 挂了，场景照跑，只是房间里没消息。
2. **降级永远可用。** 缺必填 env、matrix-nio 没装、连不上、撞加密房，四种情况
   一律自动置 ``log_only=True``，**不抛异常**。降级模式下三方法行为与 inner_bus
   完全一致 —— 「一致」是可断言的：同样的 publish 序列必须得到同样的 drain 结果。
3. **token 不进 repr。** ``MatrixBusConfig.token`` 用 ``field(repr=False)``。这是
   安全边界不是风格选择：dataclass 默认 repr 会把真 token 带进任何一句
   ``log.info("bus config=%s", cfg)``、任何一次异常栈回显、任何一份 ``evidence/``
   落盘 —— 而 ``evidence/`` 是要入库的（铁律 6），且出口脱敏管不到 ``__repr__``
   这个入口。演示当天不炸、但密钥已经进了 git 历史，比当天炸更难收拾。

``_NioChannel`` 那条活路径在系统 python3 上走不到（matrix-nio 未安装，构造即
ImportError，上游降级 log-only），所以它的三条关键判据全部另行验过 —— 用真
matrix-nio 0.26.0 客户端栈打真 HTTP 到一个本地 stub homeserver，逐条撞出来的
结论写在 ``docs/DECISIONS.md`` 的 ``## task-C2``，回归钉在
``maos/tests/test_matrix_bus.py`` 第 7 节。**尚未在真 Synapse / Element 上跑过**
（等 C-1 的房间），仍未验的部分记在 ``docs/BACKLOG.md`` 的 ``## task-C2``。

这三处判错了的症状都是「降级」而不是「崩」，所以它们不会自己暴露 —— 这也是为什么
判据要抽成模块级纯函数（:func:`encryption_verdict` / :func:`should_deliver`）
而不是埋在 ``_NioChannel`` 的方法里：埋着就只能靠读代码相信。
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
from concurrent.futures import TimeoutError as _FuturesTimeout
from dataclasses import asdict, dataclass, field, replace
from html import escape as _esc
from typing import TYPE_CHECKING, Any, Callable, Protocol

from maos.config import get_config_source
from maos.contracts.events import Envelope, EventType
from maos.core.eventbus import EventBus, Handler

if TYPE_CHECKING:                                     # pragma: no cover
    from maos.runtime.gate import HumanApprovalQueue

log = logging.getLogger("maos.matrix")


# --------------------------------------------------------------------------
# 配置（C-6 冻结：字段名、类型、env 来源逐字对应）
# --------------------------------------------------------------------------
ENV_HOMESERVER = "MATRIX_HOMESERVER"
ENV_USER = "MATRIX_USER"
ENV_TOKEN = "MATRIX_TOKEN"
ENV_ROOM_ID = "MATRIX_ROOM_ID"
ENV_APPROVERS = "MAOS_APPROVERS"

#: 四个必填项。缺任何一个都发不出消息，早降级比晚报错好（同 A-12 口径）。
#: MAOS_APPROVERS 不在此列：没有审批人只是审批命令全被拒，镜像本身照常。
REQUIRED_ENV = (ENV_HOMESERVER, ENV_USER, ENV_TOKEN, ENV_ROOM_ID)


def parse_approvers(raw: str | None) -> frozenset[str]:
    """``MAOS_APPROVERS`` 逗号分隔 -> frozenset。空白项丢弃，不做格式校验。

    不校验「必须长得像 @user:server」：Matrix user id 的形态由 homeserver 定，
    在这里画一条自造的正则，只会在换 homeserver 那天把合法审批人挡在门外，
    而挡错的症状是「命令发了没反应」，离原因很远。
    """
    if not raw:
        return frozenset()
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def current_approvers(env: dict[str, str] | None = None) -> frozenset[str]:
    """**现读**一次审批人名单（T28）。`env` 显式传就读它，否则走 `maos.config`。

    缺省配置源就是 `os.environ.get`，所以取值与 `parse_approvers(os.environ.get(...))`
    逐字节一致；`MAOS_CONFIG_SOURCE=nacos` 时同一句改从 Nacos 取。

    显式 `env` 那一支不走配置面是刻意的：`from_env({...})` 的语义是「就按我给的这份
    读」，把它改成读进程环境会让测试与降级自检拿到一份自己没给过的名单。
    """
    if env is not None:
        return parse_approvers(env.get(ENV_APPROVERS))
    return parse_approvers(get_config_source().get(ENV_APPROVERS, ""))


@dataclass
class MatrixBusConfig:
    """Matrix 接入配置。只读环境变量，**禁止写进任何文件**（铁律 6）。

    ``token`` 的 ``repr=False`` 见模块抬头第 3 条 —— 删掉它测试会当场变红，
    那条断言是安全断言，不是格式断言。
    """

    homeserver: str = ""
    user: str = ""
    token: str = field(default="", repr=False)
    room_id: str = ""
    approvers: frozenset[str] = frozenset()
    log_only: bool = False

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "MatrixBusConfig":
        """从环境变量构造。缺必填项 -> ``log_only=True`` 自动降级，**不抛异常**。

        日志只打**缺失的变量名**，不打值：这个函数拿到的正是最敏感的那几个串。
        """
        src = os.environ if env is None else env
        vals = {name: (src.get(name) or "").strip() for name in REQUIRED_ENV}
        missing = [name for name, value in vals.items() if not value]
        if missing:
            log.warning("Matrix 配置缺 %s，降级 log-only（不进房间，行为等同进程内总线）",
                        ", ".join(missing))
        return cls(
            homeserver=vals[ENV_HOMESERVER],
            user=vals[ENV_USER],
            token=vals[ENV_TOKEN],
            room_id=vals[ENV_ROOM_ID],
            approvers=current_approvers(env),
            log_only=bool(missing),
        )


# --------------------------------------------------------------------------
# 镜像内容渲染
# --------------------------------------------------------------------------
#: 出口脱敏用。只按**键名**匹配，不扫值 —— 扫值会把 GOOD_PATCH 里
#: "修复 token 校验缺失" 这种正常摘要一起打码，房间里就没法看了。
_SECRET_KEY = re.compile(
    r"(token|secret|passwd|password|api[_-]?key|authorization|credential)", re.I)
REDACTED = "***"


def redact(value: Any) -> Any:
    """递归把疑似密钥的字段值换成 ``***``。

    镜像是**出网**动作：Envelope 一旦进了房间就收不回来，而 ``payload`` 是自由
    dict，谁往里塞一个 ``api_key`` 契约都不会拦。所以脱敏放在出口这一侧，
    不指望上游自觉。注意 ``idempotency_key`` 不会误伤 —— 正则要的是 ``api_key``
    那个前缀，光一个 ``key`` 不算。
    """
    if isinstance(value, dict):
        return {k: (REDACTED if isinstance(k, str) and _SECRET_KEY.search(k) else redact(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def summarize(topic: str, env: Envelope) -> str:
    """一行人话摘要。房间刷屏时人眼要能一行扫过去，所以只挑该事件最关键的那个字段。"""
    payload = env.payload or {}
    extra = ""
    if env.event_type == EventType.TASK_RESULT:
        extra = f" status={payload.get('status')}"
    elif env.event_type == EventType.REVIEW_VERDICT:
        extra = f" verdict={payload.get('verdict')}"
    elif env.event_type == EventType.TASK_ASSIGNMENT:
        extra = f" role={payload.get('role')}"
    elif env.event_type == EventType.REWORK:
        extra = f" reason={payload.get('reason')}"
    return f"[{env.task_id}] {env.event_type} → {topic} attempt={env.attempt}{extra}"


def render_mirror(topic: str, env: Envelope) -> tuple[str, str]:
    """返回 ``(plain, html)``：一行摘要 + 折叠的 Envelope JSON。

    折叠是必需的而非好看：一个 plan 跑完几十条事件，不折叠的话房间里全是 JSON，
    人类翻不到那条要审批的高风险任务。
    """
    line = summarize(topic, env)
    body = json.dumps(redact(asdict(env)), ensure_ascii=False, indent=2)
    plain = f"{line}\n```json\n{body}\n```"
    html = (f"<p>{_esc(line)}</p>"
            f"<details><summary>Envelope JSON</summary>"
            f'<pre><code class="language-json">{_esc(body)}</code></pre>'
            f"</details>")
    return plain, html


# --------------------------------------------------------------------------
# 房间通道
# --------------------------------------------------------------------------
class MirrorChannel(Protocol):
    """镜像通道。抽出来是为了能在测试里塞一个「必炸」的实现验证旁路语义。

    ``listen`` 声明在这里而不是只留在 ``_NioChannel`` 上：下游（C-3 的 room_demo）
    拿到的是 :attr:`MatrixEventBus.channel`，形状写进 Protocol 才有一处可读的出处。
    否则换一个通道实现时漏掉 listen，症状是「房间里发命令没反应」—— 离原因很远。
    """

    def send(self, plain: str, html: str) -> None: ...

    def listen(self, on_message: Callable[[str, str], None]) -> None: ...

    def close(self) -> None: ...


class RoomEncrypted(RuntimeError):
    """房间开了端到端加密。不装 ``matrix-nio[e2e]``，遇加密房直接降级（phase-3.md:14/20）。"""


#: runbook 抬头那一条：跑真房间必须用这个解释器，系统 python3 没装 matrix-nio。
#: 路径写死是刻意的 —— 报错要给的是**能直接粘的那一行**，不是「请自行找个装了 nio 的解释器」。
VENV_PYTHON = os.path.expanduser("~/.maos-matrix/venv/bin/python")


class MatrixDepMissing(RuntimeError):
    """``matrix-nio`` 没装。这是**环境错**，不是运行时故障 —— 两者必须分开。

    别的降级（连不上、token 失效、撞加密房）都是「房间那边出了事」，重试或换个房间
    就能好；这一条是「你用错了解释器」，重试一万次都一样。而它们原来共用同一条
    ``except Exception`` 分支：同一行 WARNING、同样降级 log-only、退出码同样是 0，
    于是终端照常刷「房间消息」、场景照常跑完 —— 截那个窗口当证据，形态与真房间
    **无法分辨**。这是整条链路最贵的一步（``docs/matrix-room-runbook.md`` 抬头）。

    单独一个类型，就是为了让上游能把它判出来、并给它一个**非 0 的退出码**。
    """


class RoomSendTimeout(TimeoutError):
    """单次 send 超过通道超时。**这不等于消息没发出去。**

    Synapse 默认开 ``rc_message`` 限流，演示开头那一串镜像是连发的，直接打穿；
    matrix-nio 收到 429 会自己退避重试（实测一轮 approve 打出 4 条
    ``Got 429 response (ratelimited), sleeping for ~4.5s``）。退避把单次 send 拖过
    超时，等待方就放弃了 —— 可**协程还在私有循环上跑**，退避结束消息照样送达。

    所以它是虚警。两条推论都写进了处理它的代码里：
    ① 不许计进 ``MAX_MIRROR_FAILURES``（否则一次限流就把镜像永久降级掉，
       虚警亲手做实成真故障）；② 措辞必须说清「别重跑」—— 重跑只会再撞一次限流。
    """


#: 异常消息为空时的占位。见 :func:`describe_exc`。
NO_MESSAGE = "<该异常没有消息>"

_TIMEOUT_HINT = ("这多半是 Synapse 的 rc_message 限流（429），matrix-nio 正在后台退避重试，"
                 "消息很可能仍会送达 —— **不要重跑**，重跑只会再撞一次限流。"
                 "唯一算数的判据是去房间里数消息")


def describe_exc(exc: BaseException) -> str:
    """把异常压成一行**永不为空**的人话。所有 ``except`` 分支的日志都过这一道。

    ``log.warning("失败（%s）", exc)`` 有个哑坑：``concurrent.futures.TimeoutError``
    的 ``str()`` 恰好是**空字符串**，于是终端上打出来的是 ``房间回话失败（）`` ——
    括号里什么都没有。读的人既不知道是什么错，也无从判断严重程度，而这条恰恰是
    最需要读懂的一条：它是虚警（见 :class:`RoomSendTimeout`）。T 轮实测一轮
    approve 打了 3 条这个空括号，房间里 23 条消息一条不少。

    所以类名一律带上、消息为空时补占位；超时类再追一句「别重跑」的处置口径 ——
    告警的价值在于**读的人下一步该干什么**，只说「失败了」等于没说。
    """
    text = str(exc).strip()
    line = f"{type(exc).__name__}: {text or NO_MESSAGE}"
    if isinstance(exc, (TimeoutError, _FuturesTimeout)):
        line += f"（{_TIMEOUT_HINT}）"
    return line


# --------------------------------------------------------------------------
# 两个纯判据
# --------------------------------------------------------------------------
# 抽成模块级纯函数，是为了能在**没装 matrix-nio、也没有 Synapse** 的解释器里直接
# 断言 —— 本机 python3 与 CI 正是这种环境，而这两处恰好是整个模块里判错了也不会
# 崩、只会静默降级的地方。不抽出来，它们就只能靠读代码相信。
ENC_CLEAR = "clear"
ENC_ENCRYPTED = "encrypted"
ENC_ERROR = "error"

#: Synapse 对「状态事件不存在」回的 errcode。matrix-nio 把响应体里的 ``errcode``
#: 原样放进 ``status_code``（实测：是这个字符串，不是 HTTP 404 那个数字）。
ERRCODE_NOT_FOUND = "M_NOT_FOUND"

#: 首次 sync 用的过滤器：一条 timeline 消息都不要，只为把 ``next_batch`` 推到「现在」。
NO_HISTORY_FILTER = {"room": {"timeline": {"limit": 0}}}


def encryption_verdict(resp: Any) -> tuple[str, str]:
    """把 ``room_get_state_event("m.room.encryption")`` 的返回判成 ``(档位, 说明)``。

    **不按返回类型判，按内容判** —— 这是 matrix-nio 0.26.0 实测逼出来的
    （见 ``docs/DECISIONS.md`` 的 ``## task-C2``）：

    ``AsyncClient.create_matrix_response`` 里只有 **HTTP 404** 那一条分支会把响应体
    转成 ``RoomGetStateEventError``；其余非 200（403 机器人不在房间、401 token 失效）
    统统落进 ``else`` 走 ``RoomGetStateEventResponse.from_dict``，而它的实现就是一句
    ``return cls(parsed_dict, ...)`` —— **把错误体原样包成一个「成功」响应**。
    于是「返回的不是 Error 就是已加密」这个判据，会把「机器人没进房间」和
    「token 过期」一起念成「房间开了加密」，然后降级，日志里留一个假原因。

    用 ``getattr`` 而不是 ``isinstance``，同样是为了能在没装 matrix-nio 的解释器里
    被直接调用。两个类的字段本就互斥：Error 有 ``status_code`` 没 ``content``，
    Response 有 ``content`` 没 ``status_code``。
    """
    status_code = getattr(resp, "status_code", None)
    if status_code is not None:
        if status_code == ERRCODE_NOT_FOUND:
            return ENC_CLEAR, ERRCODE_NOT_FOUND      # 状态事件查不到 = 房间未加密
        return ENC_ERROR, f"{status_code} {getattr(resp, 'message', '')}".strip()

    content = getattr(resp, "content", None)
    if not isinstance(content, dict):
        return ENC_ERROR, f"无法识别的状态查询响应：{type(resp).__name__}"
    if content.get("errcode"):
        # 非 404 的错误体，被 from_dict 包成了「成功」响应。别念成加密房。
        return ENC_ERROR, f"{content['errcode']} {content.get('error', '')}".strip()
    algorithm = content.get("algorithm")
    return ENC_ENCRYPTED, str(algorithm) if algorithm else "m.room.encryption 已设置"


def should_deliver(room_id: str, self_user_id: str, room: Any, event: Any) -> bool:
    """这条房间事件该不该喂给 ``on_message``。两条否决都不能省。

    · **不是本房间的** —— ``sync`` 是全量的，一个 client 可能同时在多个房间里。
    · **sender 是自己** —— 比的是服务器给的权威 mxid（``whoami`` 回填的
      ``user_id``），不是 ``MATRIX_USER`` 原文。实测：``AsyncClient(hs, user)``
      只把 user 原样存进 ``.user``，``.user_id`` 在 login/whoami 之前恒为空串；
      ``MATRIX_USER`` 若写成 localpart（``maos-bot``），它和 ``event.sender``
      （``@maos-bot:maos.local``）永远不相等 —— 回声过滤就成了摆设。
    """
    if getattr(room, "room_id", None) != room_id:
        return False
    sender = getattr(event, "sender", None)
    return not (sender and sender == self_user_id)


#: send 的缺省超时（秒）。构造期仍用 10s —— 连不上要早知道，发消息要经得起退避。
DEFAULT_SEND_TIMEOUT = 30.0


class _NioChannel:
    """matrix-nio 真房间通道。

    matrix-nio 只提供 async 客户端，而 EventBus 三方法是同步签名（C-6 逐字冻结），
    所以这里自起一条守护线程跑私有事件循环，``send`` 用
    ``run_coroutine_threadsafe`` 同步等结果。不用 ``asyncio.run``：``AsyncClient``
    要跨调用保持会话状态，每次新建循环等于每次重登。

    构造顺序是 **whoami -> 查加密**，``listen`` 里再接 **先同步 -> 后挂回调**。
    三步都不能换序，理由分别写在 :meth:`_verify_identity`、:func:`encryption_verdict`
    与 :meth:`listen` 上。系统 python3 未装 matrix-nio，构造即 ImportError 由上游
    降级 log-only；测试用 ``sys.modules["nio"]`` 注入假模块走完整条路径。
    """

    def __init__(self, config: MatrixBusConfig, *, timeout: float = 10.0,
                 send_timeout: float | None = None) -> None:
        import asyncio

        from nio import AsyncClient           # 未装 -> open_channel 换成 MatrixDepMissing

        self._timeout = timeout
        #: send 单独一档，比构造期宽。一次 429 退避就是几秒，连发几条镜像很容易累计
        #: 过 10 秒 —— 用同一个 10s 去卡 send，得到的是一串「失败」告警，而消息其实
        #: 都送到了。**不是调大到把限流盖住**：限流照旧由 nio 自己那条
        #: ``Got 429 response`` 打出来，真超过这一档也照旧告警，只是措辞换成
        #: 「超时不等于没送达」（见 :class:`RoomSendTimeout`）。
        self._send_timeout = DEFAULT_SEND_TIMEOUT if send_timeout is None else send_timeout
        self._room_id = config.room_id
        self._sync_task: Any = None
        self._pending_sends: set[Any] = set()
        self._pending_send_failed = False
        self._pending_sends_lock = threading.Lock()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, name="matrix-bus", daemon=True)
        self._thread.start()

        self._client = AsyncClient(config.homeserver, config.user)
        # 用 access token 直接鉴权，不走 password login：演示机上不该出现口令。
        self._client.access_token = config.token
        #: 权威 mxid。先拿 config.user 兜底，_verify_identity 用服务器的回答覆盖它。
        self._user_id = config.user
        try:
            self._await(self._verify_identity())
            self._await(self._verify_room())
        except BaseException:
            # 构造失败要把私有循环收干净。降级是**常态路径**（连不上 / 加密房 /
            # token 失效都走这里），每失败一次漏一条守护线程加一个事件循环，
            # 进程里就多一份永不退出的后台 —— 而它不报错，只是慢慢堆。
            self._shutdown_loop()
            raise

    # -- 事件循环管线 -----------------------------------------------------
    def _run_loop(self) -> None:
        import asyncio

        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _await(self, coro: Any, timeout: float | None = None) -> Any:
        """把协程扔进私有循环并**同步等**结果。超时后协程仍在循环上继续跑。

        「继续跑」是刻意的，不是没写完：撞限流时 nio 正在退避重试，取消它等于亲手
        把一条本来会送达的消息掐掉。调用方放弃等待，消息照样落地 —— 所以超时在这里
        只是「我不等了」，不是「它失败了」（见 :class:`RoomSendTimeout`）。
        """
        import asyncio

        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(
            self._timeout if timeout is None else timeout)

    # -- 房间动作 ---------------------------------------------------------
    async def _verify_identity(self) -> None:
        """先问服务器「我是谁」。一次调用办两件事，缺哪件都会在别处变成哑故障。

        ① **验 token**。赋 ``client.access_token`` 只让 nio 本地的 ``logged_in``
           变 True（它的实现就是 ``bool(self.access_token)``），服务器认不认要等
           第一次真请求。放在开工这一刻，token 失效的症状是一句
           ``M_UNKNOWN_TOKEN``；不放，症状是演示到一半才降级、且原因指向房间。
        ② **拿权威 mxid**。``AsyncClient(hs, user)`` 只把 user 原样存进 ``.user``，
           ``.user_id`` 在此之前恒为空串 —— 而回声过滤要比的正是这个。
        """
        from nio import WhoamiError

        resp = await self._client.whoami()
        if isinstance(resp, WhoamiError):
            raise RuntimeError(
                f"access_token 未通过服务器校验：{resp.status_code} {resp.message}")
        self._user_id = getattr(resp, "user_id", "") or self._user_id

    async def _verify_room(self) -> None:
        """开工前先确认房间没开 E2EE。加密房必须**当场**降级，不能等 send 失败。

        判据本身在 :func:`encryption_verdict`，那里解释了为什么不能按返回类型判。
        """
        resp = await self._client.room_get_state_event(self._room_id, "m.room.encryption")
        verdict, detail = encryption_verdict(resp)
        if verdict == ENC_CLEAR:
            return
        if verdict == ENC_ENCRYPTED:
            raise RoomEncrypted(
                f"房间开启了端到端加密（{detail}）；本轨不装 matrix-nio[e2e]，降级 log-only")
        raise RuntimeError(f"房间状态查询失败：{detail}")

    def send(self, plain: str, html: str) -> None:
        """发一条进房间。超时换成 :class:`RoomSendTimeout` 抛出，**不是**普通失败。

        换类型是为了让上游能把「我不等了」和「它失败了」分开处理 —— 裸的
        ``concurrent.futures.TimeoutError`` 混在 ``except Exception`` 里，
        既说不出原因（``str()`` 是空串），也会被计进永久降级的失败次数。
        """
        import asyncio

        future = asyncio.run_coroutine_threadsafe(self._send(plain, html), self._loop)
        try:
            future.result(self._send_timeout)
        except _FuturesTimeout as exc:
            with self._pending_sends_lock:
                self._pending_sends.add(future)
            future.add_done_callback(self._pending_send_done)
            raise RoomSendTimeout(
                f"{self._send_timeout:.0f}s 内没等到房间回执；协程仍在后台重试，"
                f"这条消息很可能已经送达") from exc

    def _pending_send_done(self, future: Any) -> None:
        failure: Exception | None = None
        try:
            future.result()
        except Exception as exc:                        # noqa: BLE001
            failure = exc
            log.warning("超时后的房间发送最终失败（%s）", describe_exc(exc))
        # failure 与 pending→空必须在同一把锁里同时可见；反过来先 discard，
        # flush 可能正好在两句之间看见空集合并误报成功。
        with self._pending_sends_lock:
            if failure is not None:
                self._pending_send_failed = True
            self._pending_sends.discard(future)

    def flush_pending_sends(self, *, timeout: float = 45.0) -> bool:
        """等待已经超时但仍在后台重试的发送；只用于证据入口有序收口。"""
        import concurrent.futures

        deadline = time.monotonic() + timeout
        while True:
            with self._pending_sends_lock:
                pending = set(self._pending_sends)
                failed = self._pending_send_failed
            if not pending:
                return not failed
            remaining = max(deadline - time.monotonic(), 0.0)
            if remaining == 0:
                return False
            _, not_done = concurrent.futures.wait(pending, timeout=remaining)
            if not_done:
                return False

    async def _send(self, plain: str, html: str) -> None:
        from nio import RoomSendError

        resp = await self._client.room_send(
            room_id=self._room_id,
            message_type="m.room.message",
            content={
                # m.notice 而不是 m.text：机器人消息不该触发人类的推送提醒，
                # 也避免和别的 bot 互相接龙。
                "msgtype": "m.notice",
                "body": plain,
                "format": "org.matrix.custom.html",
                "formatted_body": html,
            },
        )
        if isinstance(resp, RoomSendError):
            raise RuntimeError(f"房间发送失败：{getattr(resp, 'message', resp)}")

    def listen(self, on_message: Callable[[str, str], None]) -> None:
        """在私有事件循环里常驻 ``sync_forever``，房间消息喂给 ``on_message(sender, body)``。

        **先同步、后挂回调** —— 这个顺序是本方法的全部要点，反过来写会出真事故。
        Matrix 的首次 ``/sync``（不带 ``since``）会把每个房间 timeline 的最近若干条
        **历史**一并返回，nio 照样把它们派发给 ``add_event_callback``（已实测：喂一份
        带历史 timeline 的 SyncResponse 进去，回调当场被触发）。于是半小时前有人在
        房间里打过的一句 ``/approve task-x``，bot 一起来就当成新指令重放 ——
        而审批是不可逆动作。所以先空跑一次 ``sync`` 把 ``next_batch`` 推到「现在」，
        再挂回调；此后 ``sync_forever`` 从 ``next_batch`` 续，只看得见新消息。

        守护线程里的私有循环在主线程阻塞时照常转（已实测），C-3 的 ``room_demo``
        正是「主线程等人、后台收消息」这个用法。
        """
        from nio import RoomMessageText

        async def _cb(room: Any, event: Any) -> None:
            if should_deliver(self._room_id, self._user_id, room, event):
                on_message(event.sender, event.body)
            else:
                self._warn_self_command(room, event)

        cursor = self._await(self._skip_history())
        self._client.add_event_callback(_cb, RoomMessageText)

        import asyncio

        self._sync_task = asyncio.run_coroutine_threadsafe(
            self._client.sync_forever(timeout=30_000, since=cursor), self._loop)

    def _warn_self_command(self, room: Any, event: Any) -> None:
        """被回声过滤丢掉的那条如果**是审批命令**，出声说一句。

        回声过滤本身没错，不过滤就自激。错的是它的**症状**：bot 账号自己在 Element
        里打的 ``/approve`` 也一起被丢，而丢掉之后房间里什么都不会发生 —— 没有回执、
        没有报错、任务照旧停在 BLOCKED。发命令的人只能得出「程序没在听」这个结论，
        而真正该做的是换个账号再发一遍。

        所以丢之前先看一眼：是自己发的、且长得像审批命令，就在日志里点破。判据用
        :func:`looks_like_command` 而不是自己再写一遍 —— 两份判据一定会漂，
        漂了的症状又是这条提示不出现，等于白加。
        """
        if getattr(room, "room_id", None) != self._room_id:
            return                                       # 别的房间，与回声无关
        if getattr(event, "sender", None) != self._user_id:
            return
        if not looks_like_command(getattr(event, "body", "") or ""):
            return                                       # 自己发的普通回执，本就该丢
        log.warning("忽略了一条 bot 自己发的审批命令（%s）—— 机器人不听自己的回声。"
                    "请换一个**人类**账号（MAOS_APPROVERS 里的那个）在 Element 里发",
                    self._user_id)

    async def _skip_history(self) -> str:
        """空跑一次 sync，只为把 ``next_batch`` 推到「现在」；过滤器把 timeline 压到 0 条。"""
        response = await self._client.sync(timeout=0, sync_filter=NO_HISTORY_FILTER)
        cursor = getattr(response, "next_batch", None)
        if not isinstance(cursor, str) or not cursor:
            # 不读 client.next_batch：那里可能残留上一轮的旧游标；也不回显 response，
            # 错误对象并不承诺不带敏感字段。边界没拿到就绝不能监听审批命令。
            raise RuntimeError("首次 /sync 未返回有效 next_batch，无法证明历史边界；拒绝监听")
        return cursor

    def close(self) -> None:
        """有序收口：停 sync -> 等 sync 落地 -> 关客户端 -> 清生成器 -> 停循环 -> 等线程 -> 关循环。

        **顺序就是这个方法的全部内容。** 原来的实现只做了第 1、3 步就 stop 掉循环，
        代价是退出时刷一屏 ``RuntimeError: Event loop is closed``、
        ``Task was destroyed but it is pending!``、
        ``Exception ignored in: <coroutine object AsyncClient.sync_forever ...>``：
        ``sync_forever`` 那条常驻协程和 aiohttp 的连接池都还活着，循环却已经停了，
        于是解释器退出时 GC 去跑它们的 ``__del__``，每一个都要碰那个再也不会转的循环。

        这些报错发生在终态**之后**，不影响判定也不影响退出码 —— 但它们会把真正该看的
        那几行冲出屏幕，而这套链路所有的失败都只在日志里（见模块抬头不变量 1）。
        """
        if self._loop.is_closed():
            return                                      # 已经收过口，重复调用是空操作
        if not self.flush_pending_sends(timeout=self._send_timeout):
            # 最终失败已经由 callback 记录；这里只补报仍在途，避免把两种状态混成
            # “未完成”并重复告警。
            with self._pending_sends_lock:
                still_pending = bool(self._pending_sends)
            if still_pending:
                log.warning("关闭 Matrix 通道前仍有后台发送未完成；将继续执行有序收口")
        try:
            self._client.stop_sync_forever()
        except Exception as exc:                        # noqa: BLE001 —— 没在 sync 也无所谓
            log.debug("停止 sync_forever 异常（已忽略）：%s", describe_exc(exc))
        self._stop_sync_task()
        try:
            self._await(self._client.close())
        except Exception as exc:                        # noqa: BLE001 —— 关闭失败无所谓
            log.debug("Matrix 客户端关闭异常（已忽略）：%s", describe_exc(exc))
        self._shutdown_loop()

    def _stop_sync_task(self) -> None:
        """cancel 掉 ``sync_forever`` 并**等它真的结束**。

        ``stop_sync_forever()`` 只置一个标志位，nio 要等当前这轮长轮询（timeout=30s）
        回来才看得见它 —— 光调它就往下走，收口跑完协程还挂着，正是上面那一屏报错的
        第一个来源。``CancelledError`` 继承的是 ``BaseException`` 不是 ``Exception``，
        所以必须显式列进 except，否则它会穿过去掀掉收口。
        """
        import asyncio
        import concurrent.futures

        task = self._sync_task
        if task is None:
            return
        self._sync_task = None
        task.cancel()
        try:
            task.result(self._timeout)
        except (_FuturesTimeout, concurrent.futures.CancelledError,
                asyncio.CancelledError):
            pass                                        # 取消掉/等超时都算收到了
        except Exception as exc:                        # noqa: BLE001
            log.debug("sync 任务收尾异常（已忽略）：%s", describe_exc(exc))

    def _shutdown_loop(self) -> None:
        """把私有循环连同它的守护线程收干净。构造失败与正常收口共用这一条路径。

        必须**等线程退出**再 ``loop.close()``：关一个还在转的循环会当场抛
        ``RuntimeError: Cannot close a running event loop``。等不到就宁可漏一条
        守护线程 —— 收口这一步炸掉，比多一条永不退出的后台更难查。
        """
        if self._loop.is_closed():
            return
        try:
            self._await(self._loop.shutdown_asyncgens())
        except Exception as exc:                        # noqa: BLE001
            log.debug("async 生成器收尾异常（已忽略）：%s", describe_exc(exc))
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=self._timeout)
        if self._thread.is_alive():
            log.debug("事件循环线程 %.0fs 内未退出，跳过 loop.close()", self._timeout)
            return
        self._loop.close()


def open_channel(config: MatrixBusConfig) -> MirrorChannel:
    """按 config 打开真房间通道。任何失败都原样抛出，由调用方统一降级。

    只有一处例外：``matrix-nio`` 没装时把 ``ModuleNotFoundError`` 换成
    :class:`MatrixDepMissing`，并把**当前解释器**写进消息里。理由见那个类的注释 ——
    这是「拿系统 python3 跑」这个坑唯一能被自动认出来的地方，认不出它就只能靠人
    记得去查，而这套链路里「记得去查」从来没成立过。

    ``exc.name != "nio"`` 那一支不能省：``matrix-nio`` 装了但它自己缺依赖时，
    抛的也是 ``ModuleNotFoundError``，念成「你没装 nio」会把人指向错的方向。
    """
    try:
        return _NioChannel(config)
    except ModuleNotFoundError as exc:
        if exc.name != "nio":
            raise
        raise MatrixDepMissing(
            f"当前解释器没装 matrix-nio：{sys.executable}。"
            f"改用装了它的那个重跑：{VENV_PYTHON}") from exc


# --------------------------------------------------------------------------
# 总线本体
# --------------------------------------------------------------------------
#: 连续镜像失败这么多次后永久降级。房间挂一整场时，不该把控制台刷成告警墙 ——
#: 那会把真正的业务日志淹掉，而镜像失败本身已经在第一次就报过了。
MAX_MIRROR_FAILURES = 3

#: 为什么没进房间。``log_only`` 这一个布尔把两件事抹平成了同一个 True：
#: 「本来就没打算进房间」（env 没配）和「打算进但没进成」（解释器错 / 连不上）。
#: 前者是自检的常态，后者是事故 —— 它们的正确退出码不一样，所以原因要单记一份。
DEGRADE_NONE = ""              #: 接通了，房间是活的
DEGRADE_ENV = "env"            #: 四个必填 env 没配齐 = 明确的降级意图
DEGRADE_DEPS = "deps"          #: matrix-nio 没装 = 解释器用错了（最贵的那一步）
DEGRADE_CONNECT = "connect"    #: 配齐也装了，但连不上 / 撞加密房 / token 失效


class MatrixEventBus(EventBus):
    """把 inner bus 包一层，事件顺带镜像进 Matrix 房间（C-6）。

    **装饰器，不是替代品**：三方法都先委托 inner 再镜像，语义由 inner 定义，
    这里一个字节都不改。``channel`` 是 keyword-only 的测试注入口，C-6 冻结的
    ``MatrixEventBus(inner_bus, config)`` 两参调用形态原样成立。
    """

    def __init__(self, inner: EventBus, config: MatrixBusConfig, *,
                 channel: MirrorChannel | None = None) -> None:
        self.inner = inner
        self.config = config
        self._channel: MirrorChannel | None = None
        self._failures = 0
        #: 降级原因，取值见 DEGRADE_* 那组常量。入口靠它决定退出码。
        self.degrade_reason = DEGRADE_NONE
        #: 降级原因的一行详情（已过 describe_exc）。接通时是空串。
        self.degrade_detail = ""
        if config.log_only:
            self.degrade_reason = DEGRADE_ENV
            log.warning("Matrix 总线降级 log-only：不进房间，三方法行为等同 %s",
                        type(inner).__name__)
            return
        try:
            self._channel = channel if channel is not None else open_channel(config)
        except MatrixDepMissing as exc:
            # 单独一支、单独一个级别：这不是「房间连不上」，是**解释器用错了**。
            # 四个 env 都配齐说明操作者确实想进房间，而这条原来与其他降级共用
            # 一行 WARNING、退出码同样是 0 —— 于是终端照常刷「房间消息」、场景照常
            # 跑完，截那个窗口当证据与真房间无法分辨。ERROR 只是让它在屏幕上站住，
            # **真正的闸在入口**（room_demo 按 degrade_reason 非 0 退出）。
            self.degrade_reason = DEGRADE_DEPS
            self.degrade_detail = describe_exc(exc)
            log.error("Matrix 房间没接通：%s", self.degrade_detail)
            log.error("降级 log-only —— 终端仍会照常刷「房间消息」，但房间里一条都不会有")
            self.config = replace(config, log_only=True)
        except Exception as exc:                        # noqa: BLE001 —— 见不变量 2
            self.degrade_reason = DEGRADE_CONNECT
            self.degrade_detail = describe_exc(exc)
            log.warning("Matrix 房间连接失败（%s），降级 log-only", self.degrade_detail)
            self.config = replace(config, log_only=True)

    @property
    def channel(self) -> "MirrorChannel | None":
        """当前镜像通道；降级或未接通时为 None。C-3 的 room_demo 靠它起监听。

        只读是刻意的：``_channel`` 的唯一写入方是本类的降级逻辑（连续镜像失败
        ``MAX_MIRROR_FAILURES`` 次后置 None）。开一个 setter 就多一条绕过降级的路 ——
        外部把通道塞回来，永久降级就失效了，而症状是告警墙，不是崩。
        """
        return self._channel

    # -- EventBus 三方法（签名逐字对齐 maos/core/eventbus.py:26-34）---------
    def publish(self, topic: str, env: Envelope) -> None:
        self.inner.publish(topic, env)
        if self._muted():
            # 提前返回不只是省一次 send：render_mirror 要序列化整个 Envelope，
            # 降级模式下跑它纯属白烧，而降级模式是测试与 CI 的常态路径。
            return
        self._mirror(*render_mirror(topic, env))

    def subscribe(self, topic: str, group: str, handler: Handler) -> None:
        self.inner.subscribe(topic, group, handler)
        if not self._muted():
            self._mirror_line(f"订阅 {topic}（group={group}）")

    def drain(self, max_rounds: int = 1000) -> int:
        processed = self.inner.drain(max_rounds)
        # 只在真有进展时说话：驱动循环每轮都 drain，多数返回 0，全镜像等于刷屏。
        if processed and not self._muted():
            self._mirror_line(f"drain 处理 {processed} 条事件")
        return processed

    # -- 旁路实现 ---------------------------------------------------------
    def _muted(self) -> bool:
        return self.config.log_only or self._channel is None

    def _mirror_line(self, line: str) -> None:
        self._mirror(line, f"<p>{_esc(line)}</p>")

    def _mirror(self, plain: str, html: str) -> None:
        """镜像的唯一出口。任何异常都吞掉 —— inner 已经落地的行为不许受影响。"""
        if self._muted():
            return
        try:
            self._channel.send(plain, html)             # type: ignore[union-attr]
        except RoomSendTimeout as exc:
            # **超时不计入失败次数。** nio 还在后台退避重试，这条消息很可能已经落地
            # （T 轮实测：3 条超时告警，房间里 23 条消息一条不少）。计进去的话，
            # 撞一次限流就够 3 次、直接触发永久降级 —— 那之后房间里是真的一条都没有
            # 了，一次虚警被自己亲手做实成了真故障。
            log.warning("Matrix 镜像超时（%s）；未计入失败次数", describe_exc(exc))
        except Exception as exc:                        # noqa: BLE001 —— 见不变量 1
            self._failures += 1
            log.warning("Matrix 镜像失败第 %d 次（%s），inner 总线不受影响",
                        self._failures, describe_exc(exc))
            if self._failures >= MAX_MIRROR_FAILURES:
                log.warning("镜像连续失败 %d 次，永久降级 log-only", self._failures)
                self.config = replace(self.config, log_only=True)
                self._channel = None
        else:
            self._failures = 0

    def close(self) -> None:
        if self._channel is not None:
            try:
                self._channel.close()
            except Exception as exc:                    # noqa: BLE001
                log.debug("关闭镜像通道异常（已忽略）：%s", describe_exc(exc))
            self._channel = None

    def __getattr__(self, name: str) -> Any:
        """本类没有的属性一律转给 inner（``dead_letters`` 之类）。

        装饰器要能当 inner 用：少转发一个属性，就多一处**只在 --matrix 下才炸**的
        AttributeError，而那种错最难查。``__getattr__`` 只在常规查找失败后触发，
        遮不住本类自己的三方法。
        """
        if name in ("inner", "config"):                 # __init__ 跑完前的兜底，防递归
            raise AttributeError(name)
        return getattr(self.inner, name)


# --------------------------------------------------------------------------
# 房间审批
# --------------------------------------------------------------------------
ACTION_APPROVE = "approve"
ACTION_REJECT = "reject"
_COMMANDS = frozenset({ACTION_APPROVE, ACTION_REJECT})

#: 越权尝试落 event_log 用的事件类型。「系统拒绝了一次越权审批」本身就是给评委看的
#: 证据，所以是**记事件**，不是静默丢弃。
EVENT_APPROVAL_DENIED = "ApprovalDenied"

USAGE = "用法：/approve <task_id>  或  /reject <task_id> [原因]"


@dataclass(frozen=True)
class ApprovalCommand:
    action: str                    # approve | reject
    task_id: str
    reason: str = ""               # /reject 的原因；/approve 时作为放行备注


def looks_like_command(text: str) -> bool:
    """这条消息是不是**冲着审批来的**（哪怕参数写错了）。

    与 :func:`parse_approval_command` 分开，是因为两者回答的问题不同：
    这个管「要不要接管这条消息」，那个管「参数合不合法」。合成一个函数，
    「名单外的人打了个缺 task_id 的 /approve」就会被降级成一句用法提示，
    越权证据跟着丢。
    """
    stripped = (text or "").strip()
    if not stripped.startswith("/"):
        return False
    return stripped.split(maxsplit=1)[0][1:].lower() in _COMMANDS


def parse_approval_command(text: str) -> ApprovalCommand | None:
    """解析 ``/approve <task_id>`` 与 ``/reject <task_id> [原因]``，认不出返回 None。

    拼错命令、缺 task_id 都算认不出，**一律不猜**：审批是不可逆动作，猜错一个
    task_id 就是把错的任务放行了，而房间里没人会去核对机器人猜了什么。
    """
    parts = (text or "").strip().split()
    if not parts or not parts[0].startswith("/"):
        return None
    action = parts[0][1:].lower()
    if action not in _COMMANDS or len(parts) < 2:
        return None
    return ApprovalCommand(action=action, task_id=parts[1], reason=" ".join(parts[2:]))


class RoomApprovalBridge:
    """房间审批桥 —— 把房间里的一行命令变成 ``HumanApprovalQueue.decide()``。

    判定顺序是**先认命令词、再查名单、最后校参数**，三步不可换序：

    · 不是审批命令 -> 一声不吭。房间里的闲聊不该收到机器人的用法提示。
    · 名单外 -> 回「无审批权限」并记一条 ``event_log``，哪怕参数是错的。
      先校参数会把越权尝试降级成一句用法提示，那条证据就没了。
    · 名单内但参数不合法 -> 回用法，不落任何决策。
    """

    def __init__(self, queue: "HumanApprovalQueue", config: MatrixBusConfig, *,
                 channel: MirrorChannel | None = None) -> None:
        self.queue = queue
        self.config = config
        self.channel = channel

    def handle_message(self, sender: str, body: str) -> str:
        """处理一条房间消息，返回回给房间的文本（``""`` = 不回）。"""
        text = (body or "").strip()
        if not looks_like_command(text):
            return ""

        cmd = parse_approval_command(text)
        if sender not in self._effective_approvers():
            action = text.split(maxsplit=1)[0][1:].lower()
            self._record_denied(sender, action, cmd.task_id if cmd else "")
            return self._say(f"无审批权限：{sender} 不在 MAOS_APPROVERS 名单内")

        if cmd is None:
            return self._say(USAGE)

        approved = cmd.action == ACTION_APPROVE
        try:
            self.queue.decide(cmd.task_id, approved, sender, cmd.reason)
        except Exception as exc:                        # noqa: BLE001
            # 房间里一条打错的 task_id 不该掀掉整个进程；但也不能静默 ——
            # 发命令的人必须当场知道「这条没生效」，否则会一直等一个不会来的结果。
            log.warning("审批未生效 %s：%s", cmd.task_id, describe_exc(exc))
            return self._say(f"审批未生效：{cmd.task_id} —— {exc}")

        verb = "已批准" if approved else "已驳回"
        reply = f"{verb} {cmd.task_id}（操作人 {sender}）"
        if cmd.reason:
            reply += f"，原因：{cmd.reason}"
        return self._say(reply)

    # -- 内部 -------------------------------------------------------------
    def _effective_approvers(self) -> frozenset[str]:
        """每次判定现读一次名单（T28）—— **改审批人不必重启进程**。

        口径照抄 `gate._finance_threshold` 那条已定的决策（`docs/DECISIONS.md`
        2026-08-28 P3）：在 import 时固化，改一次配置就得重启；现读则让
        `MAOS_CONFIG_SOURCE=nacos` 下 Nacos 推来的新名单在**下一条审批命令**上
        就生效。这是 §5.4 那个动态治理演示成立的地方。

        读到空就回落构造时那份快照。这一条留着三处活路，都不是假设：
        `MatrixBusConfig.from_env({...})` 显式给字典造的 config、
        `room_demo.py` 降级自检里 `replace(config, approvers=...)` 换过的 config、
        以及测试里直接 `MatrixBusConfig(approvers=...)` 构造的那些。
        进程环境没配名单时，它们仍按自己那份判 —— 与 T28 之前逐字节一致。
        """
        return current_approvers() or self.config.approvers

    def _record_denied(self, sender: str, action: str, task_id: str) -> None:
        """把越权尝试写进 event_log。写不进去也不许把异常抛给房间监听循环。"""
        store = getattr(self.queue, "store", None)
        if store is None:
            return
        plan_id, trace_id = "", ""
        if task_id:
            task = store.get_task(task_id)
            if task:
                plan_id, trace_id = task["plan_id"], task["trace_id"]
        try:
            store.append_event_log({
                "trace_id": trace_id,
                "plan_id": plan_id,
                "task_id": task_id or None,
                "event_type": EVENT_APPROVAL_DENIED,
                "reason": "sender 不在 MAOS_APPROVERS 名单内",
                "detail": {"sender": sender, "command": action, "task_id": task_id},
            })
        except Exception as exc:                        # noqa: BLE001
            log.warning("越权审批记录写入失败（%s）", describe_exc(exc))

    def _say(self, text: str) -> str:
        if self.channel is not None:
            try:
                self.channel.send(text, f"<p>{_esc(text)}</p>")
            except Exception as exc:                    # noqa: BLE001 —— 回话也是旁路
                log.warning("房间回话失败（%s），判定已生效", describe_exc(exc))
        return text
