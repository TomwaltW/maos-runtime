"""房间常驻监听器 —— 一个**不退出**的进程，房间里说什么它都在听。

    ~/.maos-matrix/venv/bin/python -m hiclaw.room_agent --plan-id <plan_id>
    python3 -m hiclaw.room_agent --allow-degraded --once      # 无房间自检，读 stdin

## 它买的是哪个缺口

``hiclaw/room_demo.py`` 是**一次性进程**：跑完那一条审批就 exit 0。2026-09-02 那次
真房间实跑正是这样 —— 审批生效、``exit=0``、进程退出，然后人在 Element 里说
「你们好 / 为什么不讲话」，一点反应都没有。**不是不理，是房间里已经没有监听者了。**

本模块只补第一层（有人一直在听）。第二层（听得懂人话）是 ``maos/nlu`` 的事，
本模块通过 :class:`IntentParser` 这个洞把它接进来，自己不含任何理解逻辑。

## 它绝不做的那件事（R1）

解析出「这人想批准」**不等于**可以去调 ``HumanApprovalQueue.decide()``。
自然语言路径的终点永远是一句确认话：

    你是想批准 task_997ca4541e66 吗？确认请发：/approve task_997ca4541e66

真正让状态迁移的唯一入口，仍然是显式的 ``/approve <task_id>`` 文本，走
``RoomApprovalBridge``，与今天的行为一字不差。理由与铁律 8 同源 ——
**权威动作不能由推断产生**。关键词匹配和大模型在这一点上没有差别：判断错一次，
等于越权批掉一笔生产变更。自然语言层买的是「少打字、看得懂」，不是「替人做决定」。

## 三条实测坑，都写进代码里了

1. **自己发的消息必须丢**，否则机器人的回执又被自己听见，自问自答死循环。
   真通道在 :func:`hiclaw.matrix_bus.should_deliver` 里已经按权威 mxid 过滤了一道，
   本模块再过滤一道 —— 假通道和 stdin 驱动没有那一道。
2. **同一条消息可能被投递多次**（sync 重连会重放）。审批是不可逆动作，
   人说一句「同意」被处理三遍等于三次误判机会。见 :meth:`RoomAgent._is_duplicate`。
3. **Synapse 限流（429）是常态，不是故障**。常驻进程一直在收发，比一次性进程更容易
   撞上。所以发送失败一律**不退出**；而 :class:`RoomSendTimeout` 更是虚警，
   **一次都不许重发** —— 它多半已经送达，重发只会再撞一次限流（见该异常的 docstring）。
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import re
import signal
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from html import escape as _esc
from typing import Any, Callable, Protocol, Sequence

from hiclaw.matrix_bus import (
    MatrixBusConfig,
    RoomApprovalBridge,
    RoomSendTimeout,
    current_approvers,
    describe_exc,
    looks_like_command,
)
# 退出码、自检行、降级通道一律从 room_demo 借，不重写第二份：两份判据一定会漂，
# 而漂了的症状是「降级跑完 exit=0，看起来像取到了证」—— 这条链路最贵的那种失效。
from hiclaw.room_demo import (
    EXIT_NO_ROOM,
    EXIT_OK,
    StdoutChannel,
    bus_channel,
    selfcheck_line,
)

log = logging.getLogger("maos.room_agent")


# --------------------------------------------------------------------------
# 数据契约 —— 逐字照 review/nl-contracts.md §1，一个字段名都不许改
# --------------------------------------------------------------------------
ACTION_APPROVE = "approve"
ACTION_REJECT = "reject"
ACTION_STATUS = "status"
ACTION_UNKNOWN = "unknown"
ACTIONS = frozenset({ACTION_APPROVE, ACTION_REJECT, ACTION_STATUS, ACTION_UNKNOWN})

CONF_HIGH = "high"
CONF_LOW = "low"


@dataclass(frozen=True)
class Intent:
    """一句人话解析出来的意图。

    **本轨自带这一份是权宜之计**：权威定义在 T66 的 ``maos/nlu/intent.py``，
    但两轨并行时那个模块可能还不存在，``import maos.nlu`` 会让本轨在 T66 落地前
    根本 import 不进来。字段与契约逐字一致，整合时删掉这份、改 import 即可。
    """

    action: str
    task_id: str = ""
    reason: str = ""
    confidence: str = CONF_LOW
    raw_text: str = ""
    detail: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.action not in ACTIONS:
            raise ValueError(f"未知 action：{self.action!r}（必须在 {sorted(ACTIONS)} 内）")


KIND_CONFIRM = "confirm"    # 需要人再发一次显式指令才生效
KIND_DONE = "done"          # 显式指令已执行，状态真的迁移了
KIND_DENIED = "denied"      # 发言人不在 MAOS_APPROVERS 名单
KIND_IGNORED = "ignored"    # unknown / 与编排无关的闲聊

KINDS = frozenset({KIND_CONFIRM, KIND_DONE, KIND_DENIED, KIND_IGNORED})


@dataclass(frozen=True)
class DispatchResult:
    """派发结果。与 :class:`Intent` 同理，权威定义在 T68，这份是并行期的替身。"""

    kind: str
    text: str
    task_id: str = ""

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"未知 kind：{self.kind!r}（必须在 {sorted(KINDS)} 内）")


class IntentParser(Protocol):
    """一句人话 -> :class:`Intent`。**认不出就返回 UNKNOWN，不抛、不猜。**"""

    def __call__(self, text: str, *, known_task_ids: Sequence[str]) -> Intent: ...


# --------------------------------------------------------------------------
# 兜底解析器（并行期替身）
# --------------------------------------------------------------------------
#: 驳回词必须**先**匹配。「不同意」里含「同意」，先判 approve 就会把驳回念成批准 ——
#: 而 R1 之下这只会多回一句确认话，但确认话里写的是 ``/approve``，等于**引导人去打错命令**。
_REJECT_WORDS = ("不同意", "不批准", "不通过", "不能过", "先别", "别过", "驳回", "拒绝",
                 "否决", "打回", "不行", "退回", "reject")
_APPROVE_WORDS = ("同意", "批准", "通过", "放行", "批了", "准了", "可以过", "没问题",
                  "approve", "lgtm", "ok")
_STATUS_WORDS = ("什么状态", "状态", "进度", "到哪了", "怎么样", "什么情况", "status")

#: 「因为 xxx」里的 xxx。人没说理由就留空 —— **理由不许编**（契约 §1 的 reason 那行）。
_REASON_RE = re.compile(r"(?:因为|原因是|理由是|原因：|理由：)\s*(.+)$")

#: 「长得像 task_id 的串」。用途只有一个：人**自己写了**一个 id 却不在 known 里时，
#: 不许再回退到「全场唯一那条」。回退在那种情况下不是兜底，是**替人把 id 换掉了** ——
#: 他说的是 A，机器人回一句「你是想批准 B 吗」，而 B 恰好是真在等人的那条。
_TASK_ID_LIKE = re.compile(r"\btask[_-][0-9A-Za-z]{4,}\b", re.IGNORECASE)


class _KeywordParser:
    """纯关键词兜底解析器。**不调模型**，因此在没有任何 key 的机器上逐字节可复现。

    它存在的理由不是「关键词够用」—— 是**并行期 T66 还没落地**，而本轨的监听骨架
    需要一个形状对得上的实现才能自证。整合时整个类都会被删掉。

    保守到什么程度：``task_id`` 必须命中 ``known_task_ids``（契约 R2）。人在房间里
    随口念一个格式正确的 id，或者模型编一个，都必须降 ``UNKNOWN`` —— 编出格式完全
    正确的 id 恰恰是模型最擅长的事。唯一的推断是「全场只有一条在等人时，
    不写 id 的『同意』指的就是它」，且这一档只给 ``CONF_LOW``。
    """

    def __call__(self, text: str, *, known_task_ids: Sequence[str]) -> Intent:
        raw = (text or "").strip()
        lowered = raw.lower()
        action = self._action(lowered)
        if action == ACTION_UNKNOWN:
            return Intent(action=ACTION_UNKNOWN, raw_text=raw)

        if action == ACTION_STATUS:
            task_id, explicit = self._task_id(raw, known_task_ids)
            return Intent(action=ACTION_STATUS, task_id=task_id, raw_text=raw,
                          confidence=CONF_HIGH if explicit else CONF_LOW,
                          detail={"parser": "keyword"})

        task_id, explicit = self._task_id(raw, known_task_ids)
        if not task_id:
            # 认出了动作却对不上任务 = 不知道要批哪一条。这不是「差一点」，是 UNKNOWN。
            return Intent(action=ACTION_UNKNOWN, raw_text=raw,
                          detail={"parser": "keyword", "why": "task_id 未命中 known_task_ids"})

        reason = ""
        if action == ACTION_REJECT:
            hit = _REASON_RE.search(raw)
            reason = hit.group(1).strip() if hit else ""
        return Intent(action=action, task_id=task_id, reason=reason, raw_text=raw,
                      confidence=CONF_HIGH if explicit else CONF_LOW,
                      detail={"parser": "keyword"})

    @staticmethod
    def _action(lowered: str) -> str:
        if any(word in lowered for word in _REJECT_WORDS):
            return ACTION_REJECT
        if any(word in lowered for word in _APPROVE_WORDS):
            return ACTION_APPROVE
        if any(word in lowered for word in _STATUS_WORDS):
            return ACTION_STATUS
        return ACTION_UNKNOWN

    @staticmethod
    def _task_id(raw: str, known_task_ids: Sequence[str]) -> tuple[str, bool]:
        """返回 ``(task_id, 是不是人自己写出来的)``。对不上一律返回 ``("", False)``。"""
        known = [tid for tid in known_task_ids if tid]
        for tid in known:
            if tid in raw:
                return tid, True
        if _TASK_ID_LIKE.search(raw):
            return "", False             # 他写了个对不上的 id：这是 UNKNOWN，不是「没写」
        if len(known) == 1:
            return known[0], False       # 全场只有一条在等人，且这一档只给 CONF_LOW
        return "", False


# --------------------------------------------------------------------------
# 兜底派发器（并行期替身）
# --------------------------------------------------------------------------
def _confirm_text(action: str, task_id: str) -> str:
    verb = "批准" if action == ACTION_APPROVE else "驳回"
    return f"你是想{verb} {task_id} 吗？确认请发：/{action} {task_id}"


class _StubDispatcher:
    """:class:`Intent` -> :class:`DispatchResult`。**R1 的落点就在这里。**

    ``approve`` / ``reject`` 永远只产出 :data:`KIND_CONFIRM` —— 这个类里**没有**
    任何一条通往 ``HumanApprovalQueue.decide()`` 的路径，这是刻意的：让「自然语言
    不能授权」成为一条读代码就能验证的结构性事实，而不是一句要靠人记得遵守的约定。

    ``status`` 这一档本轨窄化了：它是只读查询，没有状态迁移，与 :data:`KIND_DONE`
    的字面口径（「状态真的迁移了」）不完全贴合，这里借它表示「已答复，不需要人再做
    动作」。正式口径以 T68 的 ``dispatch_intent`` 为准，见 docs/DECISIONS.md ## task-T67。
    """

    def __call__(self, intent: Intent, *, store: Any = None, cp: Any = None,
                 sender: str = "", approvers: Sequence[str] = ()) -> DispatchResult:
        if intent.action == ACTION_UNKNOWN:
            # 静默。房间里的闲聊不该收到机器人的用法提示 —— 口径与
            # RoomApprovalBridge「不是审批命令就一声不吭」一致。
            return DispatchResult(kind=KIND_IGNORED, text="")

        if intent.action == ACTION_STATUS:
            return DispatchResult(kind=KIND_DONE, task_id=intent.task_id,
                                  text=self._status_text(store, intent.task_id))

        if approvers and sender not in set(approvers):
            # 提前说，省得他去 Element 里打一遍 /approve 才发现自己不在名单上。
            return DispatchResult(kind=KIND_DENIED, task_id=intent.task_id,
                                  text=f"无审批权限：{sender} 不在 MAOS_APPROVERS 名单内")

        return DispatchResult(kind=KIND_CONFIRM, task_id=intent.task_id,
                              text=_confirm_text(intent.action, intent.task_id))

    @staticmethod
    def _status_text(store: Any, task_id: str) -> str:
        if store is None or not task_id:
            return "我这边没接到任务库，查不了状态。"
        task = store.get_task(task_id)
        if task is None:
            return f"库里没有 {task_id} 这条任务。"
        return f"{task_id}：{task['title']} 当前 {task['state']}"


# --------------------------------------------------------------------------
# 常驻监听器
# --------------------------------------------------------------------------
GOODBYE = "MAOS 房间监听已下线（收到停止信号）。下次上线前，房间里的消息没有人在听。"
GREETING = "MAOS 房间监听已上线。说人话我会先跟你确认；真要生效请发 /approve <task_id>。"

#: 合成 event_id 的去重时间窗（秒）。见 :meth:`RoomAgent._is_duplicate`。
DEFAULT_DEDUP_WINDOW = 5.0
DEFAULT_DEDUP_MAX = 512


class RoomAgent:
    """房间常驻监听器。**只管收发和路由**，不含任何理解逻辑。

    一条消息进来只有三个去处，判定顺序不可换：

    1. **自己发的** -> 丢。不丢就自问自答。
    2. **长得像 /approve、/reject** -> :class:`RoomApprovalBridge`，
       这是唯一能让状态迁移的路径。
    3. **其它文本** -> :attr:`parser` 理解 -> :attr:`dispatcher` 派发 -> 回一句话。
       这条路上**没有** ``decide()``。

    ``bridge`` 构造时故意不给 ``channel``：让它只返回文本、由本类统一发送，
    发送就只有 :meth:`_say` 这一个出口，退避与去噪才有唯一的落点。
    """

    def __init__(self, *, channel: Any, bridge: RoomApprovalBridge,
                 parser: IntentParser | None = None,
                 dispatcher: Callable[..., DispatchResult] | None = None,
                 store: Any = None, cp: Any = None,
                 config: MatrixBusConfig | None = None,
                 self_id: str = "",
                 known_task_ids: Callable[[], Sequence[str]] | None = None,
                 dedup_window: float = DEFAULT_DEDUP_WINDOW,
                 dedup_max: int = DEFAULT_DEDUP_MAX,
                 send_retries: int = 2, send_backoff: float = 0.5,
                 sleeper: Callable[[float], None] = time.sleep) -> None:
        self.channel = channel
        self.bridge = bridge
        # INTEGRATION-POINT: 整合时把 _KeywordParser 换成 maos.nlu.intent.parse_intent
        # 的偏函数（functools.partial(parse_intent, model=model)）—— 签名已对齐。
        self.parser: IntentParser = parser or _KeywordParser()
        # INTEGRATION-POINT: 整合时把 _StubDispatcher 换成
        # maos.runtime.intent_dispatch.dispatch_intent —— 调用形状已按契约 §1.2 对齐。
        self.dispatcher = dispatcher or _StubDispatcher()
        self.store = store
        self.cp = cp
        self.config = config
        self.self_id = self_id
        self._known_task_ids = known_task_ids or (lambda: ())
        self._dedup_window = dedup_window
        self._dedup_max = dedup_max
        self._send_retries = send_retries
        self._send_backoff = send_backoff
        self._sleep = sleeper

        self._seen: "OrderedDict[str, float]" = OrderedDict()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self.handled = 0                 #: 真正处理过的消息条数（不含自己发的与重复的）
        self.sent: list[str] = []        #: 发出去的每一条，取证与测试用

    # -- 消息路由 ---------------------------------------------------------
    def handle_message(self, sender: str, body: str, event_id: str = "") -> str:
        """处理一条房间消息，返回发回房间的文本（``""`` = 没回）。**绝不抛。**"""
        text = (body or "").strip()
        if self.self_id and sender == self.self_id:
            log.debug("忽略自己发的消息（%s）", self.self_id)
            return ""
        if self._is_duplicate(event_id, sender, text):
            log.debug("重复投递，已丢弃：%s", event_id or "(无 event_id)")
            return ""

        self.handled += 1
        try:
            if looks_like_command(text):
                return self._handle_command(sender, text)
            return self._handle_natural(sender, text)
        except Exception as exc:                        # noqa: BLE001
            # 异常逃出去会掀掉 nio 的 sync 循环 —— 一条打错的消息不该让房间失去监听。
            log.warning("处理房间消息失败（%s），监听继续", describe_exc(exc))
            return ""

    def _handle_command(self, sender: str, text: str) -> str:
        """显式指令路径。**这是本进程唯一会调用 decide() 的地方**，口径全在 bridge 里。"""
        reply = self.bridge.handle_message(sender, text)
        return self.say(reply)

    def _handle_natural(self, sender: str, text: str) -> str:
        """自然语言路径。**R1：这条路上没有 decide()，终点最多是一句确认话。**"""
        if not text:
            return ""
        known = list(self._known_task_ids())
        intent = self.parser(text, known_task_ids=known)
        result = self.dispatcher(intent, store=self.store, cp=self.cp,
                                 sender=sender, approvers=self._approvers())
        if result.kind == KIND_IGNORED:
            log.debug("听不懂，静默：%r", text[:80])
            return ""
        return self.say(result.text)

    def _approvers(self) -> tuple[str, ...]:
        """每次判定现读一次名单，口径照 ``RoomApprovalBridge._effective_approvers``。"""
        snapshot = self.config.approvers if self.config is not None else frozenset()
        return tuple(current_approvers() or snapshot)

    # -- 去重 -------------------------------------------------------------
    def _is_duplicate(self, event_id: str, sender: str, body: str) -> bool:
        """这条是不是已经处理过了。**两档口径，因为真通道给不出 event_id。**

        ``MirrorChannel.listen`` 的回调签名是 ``(sender, body)``（``matrix_bus.py``
        是冻结参照物，本轮不许改），所以真房间里本方法拿到的 ``event_id`` 是空的。
        两档分别是：

        · **有 event_id**（假通道、以及将来放宽 listen 签名之后）——
          按 id 永久去重，命中即丢。这是唯一真正可靠的一档。
        · **没有 event_id** —— 退到 ``sender + 正文`` 的指纹，且只在
          :data:`DEFAULT_DEDUP_WINDOW` 秒内有效。窗口不能省：sync 重放是**连着**
          来的，而人隔一会儿再说一次「同意」是**合法的第二次发言**，永久去重会把
          它一起吃掉，症状是「我明明又说了一遍，机器人装死」。

        缺口已记进 docs/BACKLOG.md ## task-T67：要真正可靠，得让 listen 把
        ``event_id`` 一路带下来，那要动冻结面，不是本轮的事。
        """
        key = event_id or ("syn:" + hashlib.sha1(
            f"{sender}\x00{body}".encode("utf-8")).hexdigest())
        now = time.monotonic()
        with self._lock:
            seen_at = self._seen.get(key)
            if seen_at is not None and (event_id or now - seen_at <= self._dedup_window):
                self._seen.move_to_end(key)
                return True
            self._seen[key] = now
            self._seen.move_to_end(key)
            while len(self._seen) > self._dedup_max:
                self._seen.popitem(last=False)
        return False

    # -- 发送 -------------------------------------------------------------
    def say(self, text: str) -> str:
        """发一条进房间。**发不出去也不退出**，退避重试，且超时那一档一次都不重发。

        公开是刻意的：降级驱动（``_stdin_pump``）与 :meth:`run` 都要在收口时说一句
        「我下线了」，两条路径必须走同一个出口，否则退避与去噪只保护其中一条。
        """
        if not text:
            return ""
        for attempt in range(self._send_retries + 1):
            try:
                self.channel.send(text, f"<p>{_esc(text)}</p>")
            except RoomSendTimeout as exc:
                # 虚警：协程还在后台退避重试，这条多半已经送达。重发只会再撞一次限流。
                log.warning("房间回话超时（%s）—— 不重发，判定已生效", describe_exc(exc))
                break
            except Exception as exc:                    # noqa: BLE001
                if attempt >= self._send_retries:
                    log.warning("房间回话失败（%s），已放弃这一条，监听继续",
                                describe_exc(exc))
                    break
                delay = self._send_backoff * (2 ** attempt)
                log.warning("房间回话失败（%s），%.1fs 后重试（第 %d/%d 次）",
                            describe_exc(exc), delay, attempt + 1, self._send_retries)
                self._sleep(delay)
                continue
            break
        self.sent.append(text)
        return text

    # -- 生命周期 ---------------------------------------------------------
    def on_room_message(self, *args: Any) -> None:
        """喂给 ``channel.listen`` 的回调。**绝不抛**，且同时吃两参和三参两种形状。

        真通道按 ``MirrorChannel`` 的 Protocol 调两参 ``(sender, body)``；假通道和
        将来带 ``event_id`` 的通道调三参。用 ``*args`` 而不是默认参数，是为了让
        「通道多给了一个位置参数」不至于变成一个 TypeError 掀掉 sync 循环。
        """
        sender = args[0] if len(args) > 0 else ""
        body = args[1] if len(args) > 1 else ""
        event_id = args[2] if len(args) > 2 else ""
        try:
            self.handle_message(sender, body, event_id or "")
        except Exception as exc:                        # noqa: BLE001
            log.warning("房间回调异常（%s），监听继续", describe_exc(exc))

    def stop(self) -> None:
        """请求优雅退出。信号处理器和测试都走这一个入口。"""
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def run(self, *, once: bool = False, timeout: float | None = None,
            greet: bool = True, poll: float = 0.2) -> int:
        """挂上监听并常驻，直到收到停止信号 / 处理满一条（``once``）/ 超时。

        退出前**一定**把「我下线了」发进房间：房间里没有监听者时消息会石沉大海，
        而人是分不出「在听但不理我」和「根本没人在听」的 —— 2026-09-02 那次实跑
        踩的就是这一脚。所以下线要出声，这不是礼貌，是可观测性。
        """
        self._install_signal_handlers()
        listen = getattr(self.channel, "listen", None)
        if listen is None:
            log.warning("通道没有 listen()，收不到任何房间消息 —— 这个进程等于白跑")
        else:
            listen(self.on_room_message)
        if greet:
            self.say(GREETING)

        deadline = None if timeout is None else time.monotonic() + timeout
        while not self._stop.is_set():
            if once and self.handled >= 1:
                break
            if deadline is not None and time.monotonic() >= deadline:
                log.info("到达 --timeout，收工")
                break
            self._stop.wait(poll)

        self.say(GOODBYE)
        return EXIT_OK

    def _install_signal_handlers(self) -> None:
        """SIGINT / SIGTERM -> 优雅退出。装不上不算错。

        ``signal.signal`` 只能在主线程调；测试和将来「监听器跑在工作线程里」那种用法
        会撞 ValueError。装不上就退回「只能靠 stop() 停」，这比让进程起不来强。
        """
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, lambda _s, _f: self.stop())
            except (ValueError, OSError, AttributeError) as exc:   # noqa: PERF203
                log.debug("信号 %s 未挂上（%s）", sig, describe_exc(exc))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
_DEGRADE_SAY = (
    "没接通 Matrix 房间。常驻监听器**没有房间就没有任何意义** —— 它会一直空转，\n"
    "  而终端输出与真房间形态无法分辨，跑完 exit=0 等于让一次没进房间的运行\n"
    "  看起来像一次成功的值守。\n"
    "  · 用装了 matrix-nio 的解释器重跑：~/.maos-matrix/venv/bin/python -m hiclaw.room_agent ...\n"
    "  · 配齐 MATRIX_HOMESERVER / MATRIX_USER / MATRIX_TOKEN / MATRIX_ROOM_ID\n"
    "  · 确实只想做无房间自检：显式加 --allow-degraded（改从 stdin 读消息）"
)


def _stdin_pump(agent: RoomAgent, *, once: bool, greet: bool = True) -> int:
    """降级驱动：从 stdin 逐行读 ``<sender>|<正文>`` 喂给 agent，EOF 收工。

    没有它，``--allow-degraded`` 只是一个空转的循环，本地根本验不了路由是否成立。
    格式故意写成最土的一种：runbook 和冒烟脚本都能一行 echo 喂进来，不打网络。
    """
    print("[降级] 未接通房间，改从 stdin 读消息。格式：<sender>|<正文>，Ctrl-D 收工。",
          flush=True)
    if greet:
        agent.say(GREETING)
    for line in sys.stdin:
        line = line.rstrip("\n")
        if not line.strip():
            continue
        sender, _, body = line.partition("|")
        if not body:
            sender, body = "@stdin:local", line
        agent.handle_message(sender.strip(), body.strip())
        if once and agent.handled >= 1:
            break
        if agent.stopped:
            break
    agent.say(GOODBYE)      # 收口口径与 run() 保持同一个出口
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hiclaw.room_agent",
        description="Matrix 房间常驻监听：一直在听，人说人话也认，但只确认不执行")
    parser.add_argument("--plan-id", action="append", default=[],
                        help="要盯的 plan（可重复）。它的 BLOCKED 任务构成 known_task_ids；"
                             "不给则自然语言路径认不出任何 task_id（显式 /approve 不受影响）")
    parser.add_argument("--once", action="store_true",
                        help="处理完一条消息就退出（冒烟与手动验收用）")
    parser.add_argument("--timeout", type=float, default=None,
                        help="最多守多少秒（缺省一直守到 Ctrl-C / SIGTERM）")
    parser.add_argument("--allow-degraded", action="store_true",
                        help=f"承认这一轮不进房间，改从 stdin 读消息。"
                             f"缺省下没接通房间直接 exit {EXIT_NO_ROOM}")
    parser.add_argument("--no-greet", action="store_true",
                        help="上线时不发问候（不想在房间里刷屏时用）")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)-5s %(name)-12s %(message)s")
    logging.getLogger("maos.bus").setLevel(logging.WARNING)

    # 自检行必须**第一个**落屏：解释器用错是这条链路唯一看终端分辨不出来的失效形态。
    print(selfcheck_line(), flush=True)

    from maos.flows.common import build
    from maos.runtime.gate import HumanApprovalQueue

    store, bus, cp, _model, _worker, _gate = build({}, matrix=True)
    room = bus_channel(bus)
    degraded = room is None
    if degraded and not args.allow_degraded:
        print(f"\n[没进房间] {_DEGRADE_SAY}", file=sys.stderr)
        detail = getattr(bus, "degrade_detail", "")
        if detail:
            print(f"  原因：{detail}", file=sys.stderr)
        _close(bus)
        return EXIT_NO_ROOM

    channel = StdoutChannel() if degraded else room
    config = getattr(bus, "config", None) or MatrixBusConfig.from_env()
    hq = HumanApprovalQueue(store, cp)

    def known_task_ids() -> list[str]:
        ids: list[str] = []
        for plan_id in args.plan_id:
            try:
                ids.extend(task["task_id"] for task in hq.pending(plan_id))
            except Exception as exc:                    # noqa: BLE001
                log.warning("捞 %s 的待审任务失败（%s）", plan_id, describe_exc(exc))
        return ids

    agent = RoomAgent(
        channel=channel,
        # channel=None 是刻意的：让 bridge 只判定、只返回文本，发送统一走 RoomAgent._say。
        bridge=RoomApprovalBridge(hq, config, channel=None),
        store=store, cp=cp, config=config,
        # 权威 mxid 优先：MATRIX_USER 可能写成 localpart（maos-bot），而 event.sender
        # 是 @maos-bot:maos.local，拿原文比等于回声过滤形同虚设（should_deliver 的注释）。
        # 真通道只有私有 _user_id（whoami 回填），公开属性是留给将来的通道实现的。
        self_id=(getattr(channel, "user_id", "")
                 or getattr(channel, "_user_id", "")
                 or config.user),
        known_task_ids=known_task_ids,
    )

    try:
        if degraded:
            return _stdin_pump(agent, once=args.once, greet=not args.no_greet)
        return agent.run(once=args.once, timeout=args.timeout, greet=not args.no_greet)
    finally:
        _close(bus if not degraded else channel)


def _close(target: Any) -> None:
    """收口。关不掉也不许把异常带出去 —— 值守已经结束了。"""
    closer = getattr(target, "close", None)
    if closer is None:
        return
    try:
        closer()
    except Exception as exc:                            # noqa: BLE001
        log.debug("收口异常（已忽略）：%s", describe_exc(exc))


if __name__ == "__main__":
    sys.exit(main())
