"""把一条群消息变成一次动作 —— 去重、认命令、分发、回帖。

## 处理顺序：去重 -> 认渠道 -> 认命令 -> 执行

**去重必须在最前面。** 三个平台都会重推（飞书 1s/20s/60s，企微 5s 内 3 次），
而这里的 ``/refund`` 是要真跑一次退款处置的。放到后面去重，前两条已经跑起来了。

## 渠道的信任级别不是同一级

`ALLOW_APPROVAL` 里只有内部渠道。微信客服里坐着的是**外部客户**，让他打一句
``/approve`` 就能放行自己的退款，是这一层最容易犯、也最贵的错。审批名单
（``MAOS_APPROVERS``）挡不住它：名单比的是 sender 字符串，而外部用户的
``external_userid`` 完全可能被配进名单（比如运维图省事把客服账号也加了）。
所以这里按**渠道**先关一道，与名单是两道独立的闸。

## 回帖措辞受铁律 8 约束

群里那句话是很多人唯一会看的东西。所以：裁定说「已裁定」，网关说「已受理」，
只有 ``payment.observe`` 真观察到 settled 才敢说「已到账」。把「已提交网关」写成
「已退款」不是措辞问题 —— 客服会照着它去答复客户，而钱可能根本没动。

## ``/refund`` 为什么分两步

一条群消息直接触发一次真付款，是不可接受的：发命令的人可能打错订单号、可能不是
审批人、可能只是想问问「这单能退多少」。所以 ``/refund`` **只做只读预检**（读政策、
算窗口、出裁定与金额），把结果连同一个待办挂在群里；真正的处置要等审批人回一句
``/approve <case_id>``。

预检复用 `contrast` 里那批**同样的**函数（``policy_view`` / ``evaluate_eligibility``），
不另算一套：否则会出现「预检说批 6800、真跑退了 5390」，而两条路都不报错。

## 圆桌（``team=``）是旁路，不是处置的一环

装上一个 `TeamObserver`（`maos/roundtable/`）之后，预检 / 申请表 / 放行这三件事
各会额外触发一次「让五个岗位在群里各说一句」。它对本层是**只读观察者**：

  · 钩子一律在 `_reply` **之后**触发 —— 回帖是处置的结论，不能等五次模型调用；
  · 每个钩子各自 ``try/except``，抛了只记 WARNING，**回帖一个字不变**；
  · 触发在 `self._lock` **之外** —— `handle_execute` 的 runner 在锁内跑，
    钩子里再碰一次 router 就是自锁死。

所以 ``team=None``（缺省）时这一层的行为与从前逐字一致，一次调用都不会发生。

## 非命令文本：先看是不是在 @ 某一岗

2026-09-03 真房间实测：boss 在 Element 里 @ 了「财务执行岗」问「你是干什么的」，
回话的是 ``maos-bot``，说的是退款助手的通用话术 —— 因为这一层对非命令文本一律走
`_chat`，而回话器只有一张嘴。所以 `_text_reply` 先过一道 `parse_mention`：认出
点名就交给那一岗自己答（`answer()`，跨轨契约 §4），认不出才走闲聊。

这条回帖**仍由主通道发出**（本层只有 `_reply` 这一条出口），靠 `【岗位 · agent_id】`
名牌区分是谁在说 —— 岗位账号自己发声是发声面的事，不在这一层。

## 拖一张图为什么能激活五岗

一张照片自己不带订单号，所以从前它只能落进暂存、等一句 ``/refund`` 来认领 ——
房间里的观感是「甩了张图，五岗一言不发」。`locate_order` 补的就是这一段：
按**文件名 -> 正文 -> 本会话最近的待办**三级去认这批证据配给哪一单，认出来就把它
叠加到那一单的随案证据上、再跑一次只读预检（`_recheck`），五岗照常发言。

三条边界，每条都是铁律 8 的直接后果：

  · **认不出就不触发。** 随便挑一单挂上去 = 凭空造一条「这张图属于这一单」的事实，
    而挂错之后没有任何一条记录能解释清楚。认不出时这一层的行为与从前逐字一致。
  · **诉求类型只从正文认，不从底账猜。** 「为什么要退」是人的诉求、不是订单属性，
    底账里根本查不出来。认不出诉求类型的新单宁可不触发。
  · **一条消息只触发一轮。** 三张图是一次复检、五岗各说一次，不是三轮 15 条；
    带命令的消息更不走这条 —— ``/refund`` 自己会认领暂存并触发一次预检。
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import logging
import math
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from maos.contracts.states import TaskState
from maos.core.control_plane import AWAIT_HUMAN_DECISION
from maos.domain.refund import (
    annotation, case_pack as _case_pack, objects as _refund_objects,
    outcome as _refund_outcome, projection as _projection,
)
from maos.ingress import classify as _classify
from maos.ingress import outcome_commands as _outcome_cmds
from maos.ingress import sheet as _sheet
from maos.ingress.attachments import (
    AttachmentBuffer, AttachmentStore, AttachmentTooLarge, AttachmentTypeRejected,
    StoredAttachment,
)
from maos.ingress.contracts import (
    CHANNEL_FEISHU, CHANNEL_MATRIX, CHANNEL_WECOM, Attachment, AttachmentUnsupported,
    ChannelAdapter, InboundMessage, OutboundMessage,
)

log = logging.getLogger("maos.ingress.router")

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LEDGER = ROOT / "scenarios" / "custom" / "ledger.json"

#: 允许发审批命令的渠道。**外部渠道不在里面**，理由见模块抬头。
#: Matrix 房间是内部审批房（`MAOS_APPROVERS` 里的人就坐在里面），与企微自建应用同级。
ALLOW_APPROVAL = frozenset({CHANNEL_FEISHU, CHANNEL_WECOM, CHANNEL_MATRIX})

CMD_REFUND = "refund"
CMD_HELP = "help"
CMD_PENDING = "pending"
#: 报一遍圆桌岗位。**不在 `ALLOW_APPROVAL` 那道闸后面**：它是只读的自我介绍，
#: 不碰钱、不碰待办，外部客户问「你们这边谁在看我这单」也该答得上来。
CMD_TEAM = "team"
#: 审批命令词与 `hiclaw/matrix_bus.py` 的 `_COMMANDS` 同一份口径 —— 那边已经跑绿，
#: 这里只负责把消息**转过去**，不重新实现判定。
CMD_APPROVAL = ("approve", "reject")

#: 结果面的四条命令（`/assign` `/resolve` `/confirm` `/complain`）。**一个字都不在
#: 这里重新定义**：词表与处理函数都在 `maos/ingress/outcome_commands.py`，本文件只
#: 负责把消息转过去（口径同 `CMD_APPROVAL` 之于 `RoomApprovalBridge`）。
CMD_OUTCOME = _outcome_cmds.COMMANDS

#: 本 router 认领的全部命令词。与 `_dispatch` 那串 if **必须同源**：少列一个词，
#: 那条命令带着图进来时会多触发一轮证据复检（`_is_command`，跨轨契约 §4）。
#: `test_ingress_evidence_recheck.py::test_known_verbs_covers_dispatch` 钉着两处一致。
KNOWN_VERBS = frozenset({CMD_REFUND, CMD_HELP, CMD_PENDING, CMD_TEAM, *CMD_APPROVAL,
                         *CMD_OUTCOME})

#: 房间**不代签**的那一类闸（T135）。群里那次 `/approve` 签的是「这一单可以去退」，
#: 而 `AWAIT_HUMAN_DECISION` 的闸是控制面判定「机器已经没有别的招了」之后才落的
#: —— 网关回执回来之前它根本不存在，按键的那一刻签不到它。把那一次签字用在它上面，
#: 等于替按键的人做了一个他没做过的决定，而 Plan 会因此收在 DONE：「所有 Agent 都
#: 回复完成」而钱没退出去，正是评委那句话指的病。
#: 元组而不是单个值：控制面的第三出口与 replan 上限共用这个标记
#: （`core/control_plane.py::_escalate_to_human`），日后再多一类也只加这里一处。
HOLD_AWAITS = (AWAIT_HUMAN_DECISION,)

#: `human_exits` 的决定在卡片上怎么念。**英文原值直接打给人看是不行的**：
#: `held_for_human` 在群里读起来像个报错，而它恰恰是这张卡最要紧的一行 ——
#: 人要照着它再按一次键。认不出的值原样打（`.get` 的第二参），不猜。
EXIT_DECISION_CN = {
    "approved": "已放行",
    "rejected": "已驳回",
    "held_for_drift": "扣住等人 · 订单被改过",
    "held_for_human": "**停在这里等你**",
}

#: 待办的有效期（秒）。过期的待办**不许放行**：预检结论是按当时的政策与日期算的，
#: 隔一天再批，窗口天数已经变了，而放行时不会重算 —— 那就是拿旧结论退新钱。
TICKET_TTL = 24 * 3600

#: 点名时称呼与问题之间允许出现的分隔符。Element 的 @提及 pill 落到纯文本里带的是
#: 「显示名: 」，手打的更随意（全角冒号、逗号、直接空格），所以这几个字符一并认。
MENTION_SEPS = " \t\r\n：:，,、"

#: 只 @ 了一下、一个字没说时替上的问题。**不许沉默**：房间里 @ 了一个岗位却什么都
#: 没发生，与「那个岗位根本不在」分不出来，而后者是要去查部署的。
SELF_INTRO_Q = "你是干什么的？请用一两句话做个自我介绍。"

USAGE = """MAOS 退款助手 · 可用命令
  /refund <订单号> <诉求类型> [金额] [申请日期]
      预检一单（只读，不动钱），给出裁定与核准金额，挂一条待办
      例：/refund ORD-2026-0001 质量问题 6800
      诉求类型写中文即可（质量问题 / 七天无理由 / 发错货）
      金额留空 = 按订单实付；日期留空 = 按今天算
  /approve <case_id>         放行待办，真正执行处置
  /reject  <case_id> [原因]  撤掉待办
  /pending                   列出待办与等人审批的任务
  /team                      圆桌有哪几岗、各岗挂着什么 skill（只读，不调模型）
  /help                      本说明

钱没退出去之后（放行回帖里会给出工单号）：
  /assign  <工单号> <岗位>            把补偿工单派给一个岗，例：/assign MT-RC-… payment_ops
  /resolve <工单号> [--not-settled] <流水号> <摘要>
                                      提交线下凭证关单（第一个词当凭证引用）
                                      关单人必须是这张单的承接人
                                      加 --not-settled = 线下核对过，这笔确实没退成
                                      （关单结论 not_settled，回填的观察是 failed）
  /confirm  <案号>                    记下客户确认收到退款
  /complain <案号> <内容>             记一条客户投诉（投诉一开，这单业务就没算成）
  /compensate <案号>                  补开一张补偿工单（自动开单没成时用，已有单不重开）"""


class CommandError(ValueError):
    """命令写得不对。**回给用户看**，所以措辞要是人话，且要说清怎么改。"""


def _load_run_requests():
    """加载 `scripts/run_requests.py`，复用它的 `build_case` 与中文映射。

    用 importlib 从 scripts/ 加载而不是把那些函数搬进包里，是刻意的：搬动它等于
    改一个**已经跑绿、且被 `test_request_sheet.py` 钉着**的既有文件，属于手册范围外
    的改动（铁律 4）。而另抄一份中文诉求映射，就会出现「CSV 里写『坏了』能跑、
    群里写『坏了』不认」这种两套口径 —— 且两边都不报错。

    加载范式与 `maos/tests/test_request_sheet.py::_load_script` 逐字一致。
    """
    key = "_ingress_run_requests"
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, ROOT / "scripts" / "run_requests.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_room_smoke():
    """加载 `scripts/room_team_smoke.py`，借它的 `order_of`（文件名 -> 订单号）。

    与 `_load_run_requests` 同一个范式、同一个理由：那份口径（按长度从长到短匹配、
    前缀之后必须紧跟 ``-`` 或 ``.``）已经跑绿、已经写进
    `scenarios/custom/evidence/README.md`、也已经被冒烟脚本用着。这里再抄一份的话
    两处会慢慢长歪 —— 而症状是「冒烟里配得上、真房间里配不上」，两边都不报错。

    那个文件顶层只有 stdlib import 与常量（活儿全在 ``if __name__ == "__main__"``
    后面），所以 import 它不会起任何东西，也不读一个环境变量。
    """
    key = "_ingress_room_team_smoke"
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(
        key, ROOT / "scripts" / "room_team_smoke.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


#: 「此刻没有可对外说的三态」时房间里念的那一句。**它不是第六个对外口径**：
#: 五个字面值的唯一来源仍是 `domain/refund/projection.py` 的 `PUBLIC_*`，这一句
#: 说的是「投影投不出来」这件事本身，与那五句不在一个层面上。
PUBLIC_STATUS_PENDING_LINE = "对客户口径：尚未到可对外说的三态"

#: 对外口径那一行的前缀。拼在这里一处，两张嘴都从 `public_status_line()` 取。
PUBLIC_STATUS_PREFIX = "对客户口径："


def public_status_line(public: str | None, *, case_id: str = "") -> str:
    """把 `public_status` 渲染成房间里那一行；不该打这一行时返回 `""`（T129）。

    ## 为什么是一个函数，而不是两处各写一遍

    房间里有**两张嘴**会念这句话：圆桌财务岗的事实卡
    （`maos/roundtable/stages.py::facts_finance_result`）与 `/approve` 的回帖卡
    （`IngressRouter._render`）。T129 之前两处各有一份判定，空串时一处打
    「尚未到可对外说的三态」、另一处**整行不打** —— 同一个案子在同一个房间里
    有了两种说法。这正是 `domain/refund/projection.py` 抬头警告的事：措辞漂在
    这件事上不是排版问题，而是「谁对客户说了什么」有了两个答案。

    ## 三档，各自的理由不一样

    1. **五句之一** -> 原样念。字面值只来自 `projection.PUBLIC_*`，本函数一个
       新字面值都不拼（跨轨契约 §C 禁止自造措辞）。
    2. **空串** -> 念 `PUBLIC_STATUS_PENDING_LINE`。空串是 `public_status()` 的
       **正常产出**（`submitted`、以及 `approved` 但还没落 `refund_request` 行
       那两档），照实说「还没到」；不回落到内部七态去凑一个对外说法，那等于
       把内部进度当成了对客户的交代。
    3. **不在五句里** -> **整行不打**，并留一条 WARNING。与第 2 档分开是刻意的：
       空串时我们知道「这一单还没到那三态」，是一句真话；而拿到一个来路不明的
       字符串时，我们**不知道**这一单到哪了（它完全可能已经到账，只是标签被
       写坏了），此时念「尚未到可对外说的三态」就是替这个案子宣布一件没人核实过
       的事（铁律 8）。宁可不说，也不说一句可能是假的。

    `case_id` 只进日志，不进那一行 —— 卡片上案号已经有了，重复一遍是噪声。
    """
    text = str(public or "").strip()
    if not text:
        return PUBLIC_STATUS_PENDING_LINE
    if text not in _projection.PUBLIC_STATUSES:
        log.warning("案子 %s 的对外口径 %r 不在契约 §C 的五个字面值里，本行不打",
                    case_id or "(未指名)", text)
        return ""
    return f"{PUBLIC_STATUS_PREFIX}{text}"


def ensure_room_schema(store) -> None:
    """把房间那个库建到四条结果面命令能用的形状。**两个房间入口共用这一份**（T129）。

    从前 `hiclaw/room_ingress.py::wire()` 里写着三句 ensure，而
    `scripts/run_ingress.py::_store()` 只有 `init_schema()` —— 两个入口的建表口径
    不一样。今天靠 Skill 层的懒建表撑住不崩，但读代码的人会被绊：同样是「起房间」，
    一个入口的库里有退款域的表、另一个没有。

    **三句的顺序不许换**：`ensure_ticket_schema` 只 `ALTER TABLE ... ADD COLUMN`，
    表还不在时它自己会抛 `no such table: compensation_record`。三句都幂等，连跑无副作用。
    """
    store.init_schema()
    _refund_objects.ensure_schema(store)
    _outcome_cmds.CP.ensure_ticket_schema(store)
    _refund_outcome.ensure_outcome_schema(store)


@dataclass
class Ticket:
    """一条预检出来的待办。**只活在内存里**，进程重启即失效。

    不落库是刻意的：待办承载的是「某人说他想退这一单」这个观察，重启后重发一条
    ``/refund`` 即可重建，成本是一句话。而落库就得回答「重启后那些待办算谁批的、
    政策变了要不要重算」这一串问题 —— 在预检本身只花几十毫秒的前提下，
    让它短命是更小的面。
    """

    case_id: str
    payload: dict
    summary: str
    channel: str
    chat_id: str
    requested_by: str
    created_at: float
    #: 建这个待办时认领的证据。留在这里只为了让 `/pending` 说得出「这一单挂了几张图」——
    #: 真正进案子的那份在 ``payload["customer_evidence"]`` 里，两处同源、不各算一遍。
    evidence: tuple[StoredAttachment, ...] = ()
    #: 建这个待办时那次 `preflight` 的返回。留下来只为让圆桌钩子在 `_reply` 之后
    #: 还拿得到它 —— 预检结论是**当时**算的，放行时不重算，钩子更不该自己再算一遍。
    checked: dict = field(default_factory=dict)
    #: 五岗合议出来的收口卡（`maos/roundtable/verdict.py` 的 `Verdict`，跨轨契约 §2）。
    #: 缺省 `None`，由 `_fire` 在 `on_preflight` 返回之后填一次。没接圆桌、或圆桌
    #: 还没装合议引擎时它一直是 None，`/pending` 那一行随之整条不出现 ——
    #: 不打「建议：None」，也不在这一层自己编一句（那就是第二套裁定口径）。
    verdict: Any = None
    #: 这一单被预检到第几轮。第 1 轮是 ``/refund`` 起单那次，之后每补一批证据触发
    #: 一次复检就 +1（`_recheck`）。只用来告诉圆桌「这不是第一次看这一单」
    #: （跨轨契约 §3），**不参与任何裁定** —— 轮次多不代表更该批。
    round_no: int = 1

    def expired(self, ttl: int = TICKET_TTL, now: float | None = None) -> bool:
        return (time.time() if now is None else now) - self.created_at > ttl


@dataclass(frozen=True)
class Command:
    verb: str
    args: list[str]

    @classmethod
    def parse(cls, text: str) -> "Command | None":
        """认不出返回 None（群里的闲聊不该收到用法提示）。"""
        stripped = (text or "").strip()
        if not stripped.startswith("/"):
            return None
        parts = stripped.split()
        return cls(verb=parts[0][1:].lower(), args=parts[1:])


class IngressRouter:
    """一条入站消息的全部处置。**同步**执行，耗时由调用方决定要不要丢后台。

    ``store`` 是**这个房间的库**，处置就跑在它上面（T122）：`handle_execute` 把
    ``store=self.store`` 透给 `custom_case.run_payload()`，于是 plan / task /
    退款域那十一张表全部落在这里，而不是从前那套「`run_payload` 自建一个
    `:memory:`、跑完即灭」。

    这不是收纳整齐的问题，是四条结果面命令成立的前提：``/assign`` 要查
    `compensation_record` 里的工单，``/resolve`` 要往 `payment_observation` 回填
    一条观察，``/confirm`` ``/complain`` 要改 `case_outcome` —— 处置在别的库里跑完
    就消失的话，这四条命令一条都查不到东西，而症状是「房间里打了命令没反应」。
    `MAOS_INGRESS_DB` 指到文件时（`hiclaw/room_ingress.py::wire`），连跨进程回查
    都成立：演示完第二天还能把那一单的证据链翻出来。

    幂等（`claim_idempotency`）仍走同一个库，与从前一样。
    """

    def __init__(self, adapters: dict[str, ChannelAdapter], *, store: Any,
                 ledger_path: str | Path = DEFAULT_LEDGER,
                 approval_bridge: Any = None,
                 approval_queue: Any = None,
                 runner: Callable[..., dict] | None = None,
                 approvers: Callable[[], frozenset[str]] | None = None,
                 ticket_ttl: int = TICKET_TTL,
                 attachment_store: AttachmentStore | None = None,
                 attachment_buffer: AttachmentBuffer | None = None,
                 chat: Any = None,
                 team: Any = None) -> None:
        self.adapters = adapters
        self.store = store
        self.ledger_path = Path(ledger_path)
        self.approval_bridge = approval_bridge
        self.approval_queue = approval_queue
        self.ticket_ttl = ticket_ttl
        self._runner = runner
        self._approvers = approvers or _current_approvers
        self._ledger: dict | None = None
        self._tickets: dict[str, Ticket] = {}
        self.attachments = attachment_store or AttachmentStore()
        self.pending_evidence = attachment_buffer or AttachmentBuffer()
        #: 非命令文本的回话器（`maos.ingress.chat.ChatResponder`）。**缺省不装**：
        #: 飞书 / 企微群里人来人往，对每句闲聊都回话是骚扰；专门的审批房间才装。
        self.chat = chat
        #: 圆桌观察者（`maos.roundtable.team.RefundRoundtable`）。**缺省不装**，
        #: 装上之后预检 / 申请表 / 放行各多一次旁路发言，见模块抬头。
        self.team = team
        # 圆桌与 router 共用同一个 store：圆桌是先建出来才传得进来的（房间入口那侧
        # 在 `wire()` 之前就要 `_build_team`），所以接线只能在这里补。**探到才调** ——
        # 还没有这个方法的圆桌版本照旧跑，只是它的事件链不落库。
        attach_store = getattr(team, "attach_store", None)
        if attach_store is not None:
            attach_store(store)
        #: 每个会话最近一张申请表的一行摘要，喂给回话器当【事实】。
        self._last_sheet: dict[tuple[str, str], str] = {}
        self._lock = threading.Lock()
        #: 本次 `handle` 调用登记下来、等回帖发完再触发的圆桌事件。**按线程存**：
        #: `IngressServer` 的工作线程与 Matrix 的回调线程都会调 `handle`，
        #: 共用一个列表的话，A 的回帖会把 B 登记的事件一起 fire 掉。
        self._events = threading.local()

    # -- 审批人 -------------------------------------------------------------
    def is_approver(self, sender: str) -> bool:
        """**现读**一次名单 —— 改审批人不必重启进程。

        口径与 `hiclaw/matrix_bus.py::RoomApprovalBridge._effective_approvers` 同源
        （同一个 ``MAOS_APPROVERS``），所以 Matrix 房间和 IM 群共用一份名单。
        代价是名单里会同时躺着 Matrix 的 ``@u:server`` 和飞书的 ``ou_xxx``，
        这是对的：它们是同一批**人**在不同平台上的身份。
        """
        return sender in self._approvers()

    # -- 底账 ---------------------------------------------------------------
    def ledger(self) -> dict:
        """读一次底账并缓存。改了底账要重启进程 —— 这是刻意的。

        底账是「公司的政策与订单快照」，中途换掉它意味着同一天的两单按不同的
        政策裁定，而群里看不出发生过这件事。要换就重启，让它有一个明确的时刻。
        """
        if self._ledger is None:
            from maos.flows.custom_case import load
            self._ledger = load(self.ledger_path, require_case=False)
        return self._ledger

    # -- 入口 ---------------------------------------------------------------
    def handle(self, msg: InboundMessage) -> str:
        """处理一条消息，返回**已发出**的回帖文本（``""`` = 没回）。

        任何异常都不许抛给 webhook 循环：一条打错的命令不该让整个进程掉线，
        更不该让平台因为收不到 200 而无限重推。
        """
        # 上一次调用要是没走到 fire（直接调 `handle_execute` 之类），残留的事件不许
        # 跟着这一条消息发出去 —— 那会让房间里凭空多出一轮对着旧案子的发言。
        self._pending().clear()
        if not self._claim(msg):
            log.info("重复投递，已忽略：%s", msg.dedup_key)
            return ""

        # 先收附件、再认命令。两者并列而不是二选一：一条消息可以图文都有
        # （飞书 post、Matrix 带 body 的图），先判命令会把图丢掉。
        evidence_note = self._ingest_attachments(msg) if msg.attachments else ""

        cmd = Command.parse(msg.text)
        if cmd is None:
            # 纯图片没有命令。**必须回一句** —— 群里发了张照片却什么都不发生，
            # 发的人只能得出「机器人没在听」这个结论，然后要么重发要么放弃，
            # 而证据其实已经存下来了。回执同时告诉他下一步该打什么。
            # 带字的非命令消息交给 `_text_reply`：@ 了某一岗就由那一岗答，否则走闲聊
            # （缺省没装回话器，闲聊照旧一声不吭）。
            chat_note = self._text_reply(msg) if (msg.text or "").strip() else ""
            out = self._reply(msg, "\n\n".join(p for p in (evidence_note, chat_note) if p))
            self._fire()
            return out
        try:
            reply = self._dispatch(msg, cmd)
        except CommandError as exc:
            reply = f"{exc}\n\n{USAGE}"
        except Exception as exc:                        # noqa: BLE001
            log.exception("处理 %s 失败", msg.dedup_key)
            reply = f"处理失败：{type(exc).__name__}: {exc}"
        if evidence_note:
            reply = f"{evidence_note}\n\n{reply}" if reply else evidence_note
        out = self._reply(msg, reply)
        self._fire()
        return out

    # -- 圆桌 ---------------------------------------------------------------
    def _pending(self) -> list:
        """本线程这次调用登记下来的事件。没装圆桌时它永远是空的。"""
        box = getattr(self._events, "box", None)
        if box is None:
            box = self._events.box = []
        return box

    def _record(self, event: tuple) -> None:
        """登记一件「刚才发生了什么」，等 `handle` 把回帖发完再触发。

        **不当场调**：`handle_refund` / `handle_execute` 里拿得到 payload 的那一刻，
        回帖还没发出去；在那里调钩子等于让群里先看五个岗位发言、再看预检结论。
        """
        if self.team is not None:
            self._pending().append(event)

    def _fire(self) -> None:
        """把这次调用登记的事件依次交给圆桌。**每个各自兜异常，回帖已经发过了。**

        钩子抛出来的任何东西都只记 WARNING：圆桌是旁路观察者，它炸了不该让
        「这一单批了没有」这个结论跟着不见（红线 R4）。
        """
        events = self._pending()
        if not events:
            return
        fired, events[:] = list(events), []
        for event in fired:
            kind = event[0]
            try:
                if kind == "preflight":
                    ticket = event[1]
                    # 复检那一路多登记一个 dict（轮次与本轮新增证据数，契约 §3）。
                    # 起单那一路仍是两元组 —— 那两个参都有默认值，第 1 轮传不传
                    # 一个样，而不传就不会碰 `on_preflight` 的老签名。
                    extra = event[2] if len(event) > 2 else {}
                    reports = self.team.on_preflight(
                        payload=ticket.payload, checked=ticket.checked,
                        ledger=self.ledger(), evidence=list(ticket.evidence),
                        requested_by=ticket.requested_by,
                        **_accepted_extra(self.team, extra))
                    self._attach_verdict(ticket, reports)
                elif kind == "sheet":
                    self.team.on_sheet(rows=event[1], ledger=self.ledger(),
                                       requested_by=event[2])
                elif kind == "execute":
                    self.team.on_execute(payload=event[1], result=event[2],
                                         operator=event[3])
            except Exception as exc:                    # noqa: BLE001
                log.warning("圆桌 %s 钩子失败（%s: %s），回帖不受影响",
                            kind, type(exc).__name__, exc)

    def _attach_verdict(self, ticket: Ticket, reports: Any) -> None:
        """把五岗合议出来的收口卡挂到待办上，供 `/pending` 报一句「建议：…」。

        **取不到入口就保持 None**：合议引擎（`decide()`，跨轨契约 §2）是另一轨的件，
        这一层只消费。取不到时 `/pending` 少一行，而不是少一条待办。

        算收口卡单独兜一层异常、日志与钩子失败那句分开：`on_preflight` 走完了、
        只是收口卡没算出来，与「五岗根本没发言」是两件事，混成一句会指错方向。
        """
        decide = getattr(self.team, "decide", None)
        if decide is None:
            decide = _decide_fn()
        if decide is None or not reports:
            return
        try:
            ticket.verdict = decide(
                reports, case_id=str(ticket.checked.get("case_id") or ""))
        except Exception as exc:                        # noqa: BLE001
            log.warning("合议收口卡没算出来（%s: %s），待办不带建议",
                        type(exc).__name__, exc)

    def handle_team(self, msg: InboundMessage) -> str:
        """``/team`` —— 报一遍这里有哪些成员、各自什么职责、挂着哪些 skill。

        **不判渠道、不判名单、不调模型**。三条都是刻意的：这是只读的自我介绍，
        它不碰钱也不碰待办，与 `/approve` 那道渠道闸不是一件事；而名单本来就是
        代码里的常量与 skill 注册表，让模型复述一遍只会多一次编造的机会（铁律 8）。

        **接了圆桌那条路径逐字节不变**（T109）：`render_roster` 排的是五岗
        「在房间里的样子」—— mxid、代言与否、skill 三元组，这是 `/team` 今天的
        答案，没有理由因为多了一条兜底路径就动它。

        **没接圆桌时不再只回那一句。** `/team` 问的是「这里有哪些成员」，而这个
        问题在没接圆桌时**也有答案**：`AGENT_POOL` 里躺着二十几个 Agent。只回
        「没接圆桌」，是把「圆桌没装」说成了「没有成员」—— 那是答错了，不是没答。
        「没接圆桌」那半句仍然留着并且排在最前：它是真的，而少了它，人会把下面
        这份名册当成房间里那五个名牌。

        兜底走 `Roster.from_agent_pool()`，**不叫 `merge_roundtable()`** 补岗位名。
        这条路径的前提就是「圆桌没装」，为了几个人话名字反过来 import 圆桌那一包，
        与前提自相矛盾；title 是圆桌给的东西，没装圆桌就没有它，留空是诚实的。

        名册渲染失败退回原来那一句，不让 `/team` 整条打不出来：这条路径要 import
        `maos.agents` 全包（扫目录触发 `@register`），而只装了命令面的进程未必
        带得动它。那时该说的是「圆桌没接」，不是把一个 ImportError 甩进群里。
        """
        del msg                                         # 谁问都一样，与会话无关
        if self.team is not None:
            return render_roster(self.team.roster())

        lone = "本进程没接圆桌（单机器人模式），命令面与申请表照常可用"
        try:
            # 惰性 import，同 `_titles()`：`maos/ingress/` 不在模块级拉 Agent 池。
            from maos.core.roster import Roster, render_members

            roster = Roster.from_agent_pool()
            listing = render_members(roster.members())
        except Exception as exc:                        # noqa: BLE001 —— 见 docstring
            log.warning("全局名册没排出来（%s: %s），/team 只报没接圆桌",
                        type(exc).__name__, exc)
            return lone
        if not listing:
            return lone
        return f"{lone}\n\n本进程装着 {len(roster)} 位成员：\n{listing}"

    # -- 附件 ---------------------------------------------------------------
    def _ingest_attachments(self, msg: InboundMessage) -> str:
        """取件 -> 落盘 -> 暂存，返回给群里的一句回执（``""`` = 一件都没成）。

        **逐个附件独立处理**：三张照片里有一张超限，不该让另外两张一起失败。
        群里的回执把成功与被拒的分开列，因为发的人需要知道要不要重发那一张。

        取件失败不抛给上层：`handle` 那层的 except 会把它变成「处理失败：ApiError」，
        而附件取件失败与命令执行失败是两回事 —— 前者重发一张图就好，后者可能是
        底账没有那个订单。混成同一句话，人不知道该改什么。
        """
        adapter = self.adapters.get(msg.channel)
        stored: list[StoredAttachment] = []
        rejected: list[str] = []
        sheets: list[str] = []

        limit = self.attachments.max_bytes
        for att in msg.attachments:
            # 取件按**附件自己的渠道**找件，找不到才退回消息的渠道：补件页传上来的
            # 字节在 `web-upload` 那个件手里，而消息本身属于房间（回帖要发回房间）。
            # 三个 webhook 渠道与 Matrix 的附件 channel 与消息 channel 恒相同，
            # 这一步对它们是恒等。
            taker = self.adapters.get(att.channel) or adapter
            try:
                if taker is None:
                    raise AttachmentUnsupported(f"渠道 {msg.channel} 未注册 adapter")
                # 体积闸在**取件前后各一道**：平台自报的 size 能省掉一次没必要的出网；
                # 自报值不可信，所以拿到字节再校一次。放在这里而不是只靠 `put()`，
                # 是因为申请表那条分支不经过 `put()` —— 一张 200 MB 的「表」会被整个
                # 下载、解码、逐行走完，而同样体积的照片早在 `put()` 就被拒了。
                if att.size and att.size > limit:
                    raise AttachmentTooLarge(f"附件自报 {att.size} 字节，超过上限 {limit} 字节")
                data = taker.fetch(att)
                if len(data) > limit:
                    raise AttachmentTooLarge(f"附件 {len(data)} 字节，超过上限 {limit} 字节")
                if _sheet.looks_like_sheet(data):
                    # 申请表不是证据：不落盘、不暂存，逐行预检后直接回帖。
                    # 认表按内容不按扩展名（与白名单同一取向），判不出的照旧走白名单。
                    try:
                        sheets.append(self.handle_sheet(msg, att, data))
                    except Exception as exc:            # noqa: BLE001
                        # 表处理失败与取件失败**不是一回事**：前者要人去看表，后者要人
                        # 查 token / 网络。混成「取件失败」会把人指向完全错误的方向。
                        log.exception("申请表处理失败 channel=%s key=%s", msg.channel, att.file_key)
                        rejected.append(f"{att.filename or att.kind}：申请表处理失败"
                                        f"（{type(exc).__name__}: {exc}）")
                    continue
                item = self.attachments.put(
                    data, att, chat_id=msg.chat_id, sender=msg.sender)
            except (AttachmentTooLarge, AttachmentTypeRejected) as exc:
                # 拒收是**对方的输入问题**，说清楚哪一张、为什么，人能自己改。
                rejected.append(f"{att.filename or att.kind}：{exc}")
                continue
            except Exception as exc:                    # noqa: BLE001
                # 含 AttachmentUnsupported（渠道没实现取件）与一切网络/凭证故障。
                # 取件失败是**我方的问题**（token 过期、网络、没实现），不让人去猜。
                log.exception("取附件失败 channel=%s key=%s", msg.channel, att.file_key)
                rejected.append(f"{att.filename or att.kind}：取件失败（{type(exc).__name__}）")
                continue
            self.pending_evidence.add(item)
            stored.append(item)

        # 这批新证据配得上某一单就当场复检（判单四级见 `locate_order`）。
        # **带命令的消息不走这条**：``/refund`` 自己会认领暂存并触发一次预检，
        # 在这里再触发一次，就是同一条消息两轮五岗发言（契约 §4）。
        attached, card = ("", "")
        if stored and not _is_command(msg.text):
            attached, card = self._recheck(msg, stored)
        notes = [*sheets,
                 self._render_evidence(msg, stored, rejected, attached=attached),
                 card]
        return "\n\n".join(n for n in notes if n)

    # -- 判单与复检 ---------------------------------------------------------
    def locate_order(self, msg: InboundMessage,
                     stored: list[StoredAttachment]) -> tuple[str, str]:
        """这批新证据配给哪一单。返回 ``(order_id, 依据说明)``；定位不到返回 ``("", "")``。

        四级优先，先命中先返回，**顺序不可调换**（跨轨契约 §1）：

          1. 附件文件名前缀是底账里的订单号   -> ``按文件名 <filename>``
          2. 消息正文里出现的订单号（底账里查得到才算） -> ``按消息里的订单号``
          3. 本会话最近一条未过期待办         -> ``按本会话最近的待办 <case_id>``
          4. 都不命中                         -> ``("", "")``

        第 4 级返回空**不是失败**。五岗里有三岗（规则审核 / 风险反欺诈 / 财务执行）
        的事实全部来自 `checked`，而 `checked` 来自一个具体订单 —— 没有订单，
        那三岗只能各说一句「本岗这一轮没有结论」，房间里就是五个人排队说废话。
        更不能随便挑一单挂上去：那是凭空造了一条「这张图属于这一单」的事实，
        而 MAOS 不持有权威事实（铁律 8），挂错之后没有任何一条记录能解释清楚。
        """
        known = {str(o["order_id"]) for o in self.ledger().get("order_snapshot", [])
                 if o.get("order_id")}
        if not known:
            return ("", "")

        # 1) 文件名。口径**借** `scripts/room_team_smoke.py::order_of`，不另抄一份，
        # 理由见 `_load_room_smoke`。
        order_of = _load_room_smoke().order_of
        for item in stored:
            hit = order_of(item.filename, known)
            if hit:
                return (hit, f"按文件名 {item.filename}")

        # 2) 正文。**必须回底账校验**：正则从正文里刮出一个 ORD-xxxx-xxxx 只说明
        # 「长得像订单号」，底账里没有这一单的话 `build_case` 会当场抛，而群里看到的
        # 会是「处理失败：RequestSheetError」。所以反过来做 —— 拿底账里的订单号去
        # 正文里找，查得到才算，校验与匹配是同一步，不可能漏。
        # 从长到短与 `order_of` 同一个理由：`ORD-1` 会先命中 `ORD-10` 的正文。
        text = msg.text or ""
        for order_id in sorted(known, key=len, reverse=True):
            if order_id in text:
                return (order_id, "按消息里的订单号")

        # 3) 本会话最近一条未过期待办。**这一级是这条链路的价值所在**：人的真实动作
        # 是「/refund 起单 -> 看到证据不齐 -> 拖一张图」，中间不会再打一遍订单号。
        latest = self._latest_ticket(msg)
        if latest is not None:
            order_id = str(latest.checked.get("order_id") or "")
            if order_id in known:
                return (order_id, f"按本会话最近的待办 {latest.case_id}")
        return ("", "")

    def _latest_ticket(self, msg: InboundMessage, order_id: str = "") -> "Ticket | None":
        """本会话最近一条未过期待办；给了 ``order_id`` 就只找那一单。

        先取快照再挑，不在锁里做判断：`expired()` 与比较都只读，而 `self._tickets`
        会被别的线程改 —— 边迭代边被改的症状是一次随机的 RuntimeError，
        且只在真房间的并发下才出现。
        """
        with self._lock:
            tickets = list(self._tickets.values())
        alive = [t for t in tickets
                 if t.channel == msg.channel and t.chat_id == msg.chat_id
                 and not t.expired(self.ticket_ttl)
                 and (not order_id or str(t.checked.get("order_id") or "") == order_id)]
        return max(alive, key=lambda t: t.created_at, default=None)

    def _recheck(self, msg: InboundMessage,
                 stored: list[StoredAttachment]) -> tuple[str, str]:
        """把这批新证据挂到定位出来的那一单上，再跑一次只读预检。

        返回 ``(挂载行, 复检卡)``；定位不到、或案子建不出来时两者都是 ``""`` ——
        此时这一层的行为与从前**逐字一致**（暂存 + 「等一条 /refund 认领」），
        一个岗都不叫。

        **逐条复用既有路径**：`build_case` / `preflight` / `Ticket` / `_record`
        与 `handle_refund` 是同一批，不另写一套。另写一套的症状是
        「/refund 说批 6800、拖张图复检说批 5390」，而两条路各自都不报错。
        """
        order_id, why = self.locate_order(msg, stored)
        if not order_id:
            return ("", "")
        try:
            ticket, added, before = self._build_recheck(msg, order_id, stored)
        except CommandError as exc:
            # 认不出诉求类型、或底账里没这一单：**退回不触发**，不是回「处理失败」。
            # 人刚甩了一张图，得到一句异常名对他没有任何用处，而暂存那条老路
            # （「等一条 /refund 认领」）恰好就是他该走的下一步。
            log.info("证据复检没起来（%s），按未定位处理：%s", exc, order_id)
            return ("", "")
        except Exception as exc:                        # noqa: BLE001
            # 预检本身炸了也一样降级 —— 复检是给房间加戏的，不该把「已收下 N 份
            # 证据」这句唯一的回执一起带走（红线 R4 的同一条取向）。
            log.exception("证据复检失败 order=%s channel=%s（%s）",
                          order_id, msg.channel, type(exc).__name__)
            return ("", "")

        with self._lock:
            # 同一 case_id 覆盖而不是新增，与 `handle_refund` 的既有语义一致 ——
            # 待办是「当前想退这一单」的意思，留着两条会让 /approve 不知道批哪一条。
            self._tickets[ticket.case_id] = ticket
        self._claim_stored(msg, stored)
        # 锁外登记，`handle` 末尾才 fire —— 与 `handle_refund` 同一条规矩。
        self._record(("preflight", ticket,
                      {"round_no": ticket.round_no, "added_evidence": added}))
        line = (f"已挂到 {order_id}（{why}），随案证据 "
                f"{before} → {len(ticket.evidence)} 份")
        return (line, self._render_preflight(ticket.checked, ticket, recheck=True))

    def _build_recheck(self, msg: InboundMessage, order_id: str,
                       stored: list[StoredAttachment]) -> tuple["Ticket", int, int]:
        """建复检那条待办。返回 ``(ticket, 本轮新增几份, 原先几份)``。

        建不出来一律抛 `CommandError` —— 由 `_recheck` 翻成「按未定位处理」。
        """
        rr = _load_run_requests()
        old = self._latest_ticket(msg, order_id)
        if old is not None:
            # 浅拷贝：复检中途抛了，老待办的 payload 不该已经被改过一半。
            payload = dict(old.payload)
            base: tuple[StoredAttachment, ...] = old.evidence
            summary, round_no = old.summary, old.round_no + 1
            # 待办的归属不因为别人补了张图就转移：`requested_by` 是「谁想退这一单」，
            # 收口卡按它 @ 人，而该看结论的是起单的那个人。
            requested_by = old.requested_by
        else:
            # 没有待办时 `reason` 从哪来？**从底账查不出来** —— 它是人的诉求，
            # 不是订单属性。所以只认消息正文里写着的诉求类型，认不出就整条不触发：
            # 编一个「质量问题」出来 = 编一条权威事实（铁律 8）。
            reason_raw = _reason_in(msg.text or "")
            if not reason_raw:
                raise CommandError(f"{order_id} 没有在办的待办，正文里也认不出诉求类型")
            try:
                payload = rr.build_case(self.ledger(), {
                    "order_id": order_id, "reason": rr._reason_code(reason_raw),
                    "amount": None, "requested_at": rr._iso(""),
                })
            except rr.RequestSheetError as exc:
                raise CommandError(str(exc)) from exc
            base, summary, round_no = (), f"{order_id}（{reason_raw}）", 1
            requested_by = msg.sender

        merged = _merge_evidence(base, stored)
        payload["customer_evidence"] = [
            item.as_evidence(f"ev-{i:02d}") for i, item in enumerate(merged, 1)
        ]
        checked = preflight(payload)
        ticket = Ticket(
            case_id=checked["case_id"], payload=payload, summary=summary,
            channel=msg.channel, chat_id=msg.chat_id, requested_by=requested_by,
            created_at=time.time(), evidence=tuple(merged), checked=checked,
            round_no=round_no,
        )
        return ticket, len(merged) - len(base), len(base)

    def _claim_stored(self, msg: InboundMessage,
                      stored: list[StoredAttachment]) -> None:
        """把已经挂上案子的那几份从暂存里移走。

        **不移走的症状**：下一句 ``/refund ORD-B`` 会把它们再挂一遍，而两个案子
        引用同一张图这件事，事后没有任何一条记录能解释清楚（与
        `AttachmentBuffer.claim` 的清空是同一条理由）。

        `take` 是跨轨契约 §2 的件。取不到就退回 `claim()` 全取 —— 代价是这个会话里
        **别的**还没认领的图也一起被清掉。那是已知降级，但方向是对的：宁可多清，
        也不能留下来让它挂到下一单上。
        """
        take = getattr(self.pending_evidence, "take", None)
        if take is None:
            self.pending_evidence.claim(msg.channel, msg.chat_id)
            return
        take(msg.channel, msg.chat_id, digests={item.digest for item in stored})

    # -- 申请表 -------------------------------------------------------------
    def _record_reason_annotations(self, parsed: Any) -> None:
        """把这张表里**模型判出来的**每一格诉求类型落进 `intake_annotation`。

        **这条链路上落库第一次真正有用**：`self.store` 是持久库，标注写进去查得回来。
        命令行那条每单一个 `:memory:` 运行时，落了跑完就没，接线时刻意没落
        （见 `docs/DECISIONS.md`）。有了这张表，「模型当时把『漏发了两个』认成了什么、
        多有把握、凭哪一次调用」才回答得出来 —— 模型给的是观察与推断，不是权威事实。

        **只落 source 是 `model` 的那几格。** `annotation.record` 的 `invocation_id`
        是 actor 锚点、也是主键的一部分，而词表命中与 fallback 压根没有那次调用。
        给它们编一个合成 id 能过校验，却让审计链指向一条查不到的记录 —— 那正好是
        这张表存在理由的反面（`annotation.py::_require_invocation_id`）。词表命中是
        确定性匹配、原样可复现，不留痕也答得出「当时怎么认的」。

        落库失败**不掀掉回帖**：标注是观察的留痕，不是处置的一环。写不进去要出声
        （WARNING），但群里那份预检结果一个字都不该因此少。
        """
        from maos.model.client import ENV_MODEL

        judged = [r for r in parsed.rows
                  if (r.verdict or {}).get("source") == "model"
                  and (r.verdict or {}).get("invocation_id")]
        if not judged:
            # 一张全靠词表认下来的表不建表、不写库 —— 那是常态（没配 key 时是全部）。
            return
        try:
            # 与写库的 skill 同一个范式（`skills/builtin/ap/_common.py::ensure_schema`）：
            # 谁写谁先建，幂等。router 的 store 由调用方给，建表不是它的构造职责。
            _refund_objects.ensure_schema(self.store)
        except Exception as exc:                        # noqa: BLE001
            log.warning("建 intake_annotation 表失败，本张表的诉求类型标注没留痕（%s: %s）",
                        type(exc).__name__, exc)
            return

        model = os.environ.get(ENV_MODEL, "") or ""
        for row in judged:
            verdict = row.verdict or {}
            order = _sheet._find_order(self.ledger(), row.order_id)
            if order is None:
                # 底账里没这一单 -> 推不出 tenant_id。那一行本来也进不了处置
                # （`_parse_row` 已经把它记成 problem），标注无处可挂。
                continue
            try:
                annotation.record(
                    self.store,
                    tenant_id=str(order["tenant_id"]),
                    # 与 `run_requests.build_case` 同一条口径：case_id = RC-<订单号>。
                    # 两处不一致的话，受理岗写的标注与房间读的案子对不上号。
                    case_id=f"RC-{row.order_id}",
                    field="reason_code",
                    raw_text=row.reason_raw,
                    value=str(verdict.get("reason") or ""),
                    confidence=float(verdict.get("confidence") or 0.0),
                    why=str(verdict.get("why") or ""),
                    source="model",
                    model=model,
                    invocation_id=str(verdict["invocation_id"]),
                )
            except Exception as exc:                    # noqa: BLE001
                log.warning("第 %d 行（%s）的诉求类型标注没落进库（%s: %s）",
                            row.line, row.order_id, type(exc).__name__, exc)

    def handle_sheet(self, msg: InboundMessage, att: Attachment, data: bytes) -> str:
        """一张申请表：逐行校验 -> 合法的行逐单**只读预检** -> 挂待办 -> 一份回帖。

        与 :meth:`handle_refund` 走同一条 `build_case` + `preflight`，只是把
        「一行命令」换成「一张表」。不动钱：放行仍要审批人逐单 ``/approve <case_id>``。
        表里的行**不认领**会话暂存的照片 —— 十行申请配三张图，没法知道图是谁的；
        要挂证据就单发 ``/refund`` 那一单。

        任何一行预检抛错都不许掀掉整张表：那一行记进 ``errors``，其余照跑。

        词表认不出的诉求类型交给 `refund.reason_classify` 兜底（`make_classifier()`，
        **没配 key 时返回 None，那不是故障**）。判不准的行既不预检也不催人改表，
        挑出来等人工 —— 房间才是演示主场，一句「看不懂的诉求类型」在群里是死路。
        """
        rr = _load_run_requests()
        name = att.filename or "附件"
        classifier = _classify.make_classifier()
        try:
            parsed = _sheet.parse(data, att.filename, self.ledger(), classifier=classifier)
        except _sheet.NotASheet as exc:
            return f"{name}：看着像表，但{exc}"
        except UnicodeDecodeError:
            return (f"{name}：解不出文字（试过 {'、'.join(_sheet.ENCODINGS)}），"
                    "请另存为 UTF-8 CSV 再发")
        except _sheet.SheetParseError as exc:
            return f"{name}：表解析失败 —— {exc}。请另存为标准 CSV（UTF-8）再发"

        self._record_reason_annotations(parsed)

        verdicts: dict[int, dict] = {}
        errors: dict[int, str] = {}
        #: 按行号另存一份 payload，只给圆桌钩子用 —— 回帖不需要它，而钩子在
        #: `_reply` 之后才跑，那时这个循环的局部变量已经没了。
        payloads: dict[int, dict] = {}
        for row in parsed.valid:
            try:
                payload = rr.build_case(self.ledger(), row.req)
                checked = preflight(payload)
            except Exception as exc:                    # noqa: BLE001
                log.exception("申请表第 %d 行预检失败（%s）", row.line, row.order_id)
                errors[row.line] = f"{type(exc).__name__}: {exc}"
                continue
            ticket = Ticket(
                case_id=checked["case_id"], payload=payload,
                summary=f"{row.order_id}（{row.reason_raw or row.req['reason']}）",
                channel=msg.channel, chat_id=msg.chat_id, requested_by=msg.sender,
                created_at=time.time(), checked=checked,
            )
            payloads[row.line] = payload
            with self._lock:
                old = self._tickets.get(ticket.case_id)
                self._tickets[ticket.case_id] = ticket
            if old is not None and (old.requested_by != msg.sender or old.evidence):
                # 同一 case_id 的待办被表里这一行**换掉了**，而且换的是别人的、或者
                # 原来挂着证据。/approve 只认 case_id，审批人拿着上一条回帖去放行，
                # 执行的会是这一行 —— 这件事必须在同一份回帖里说出来。
                note = f"替换了 {old.requested_by} 之前提交的待办（{old.summary}）"
                if old.evidence:
                    note += f"，原挂的 {len(old.evidence)} 份证据不再随案"
                row.warnings.append(note)
            verdicts[row.line] = checked

        if self.team is not None:
            # 整张表**一次**登记：50 行 × 5 岗 = 250 条会把房间刷爆，所以行全给出去，
            # 由圆桌自己汇总说一次（契约 §1.4）。
            self._record(("sheet", _sheet_rows(parsed, payloads, verdicts, errors),
                          msg.sender))
        self._last_sheet[(msg.channel, msg.chat_id)] = _sheet.summary(parsed, verdicts, errors)
        return _sheet.render(parsed, verdicts, errors, decision_cn=rr.DECISION_CN)

    # -- 闲聊 ---------------------------------------------------------------
    def _chat(self, msg: InboundMessage) -> str:
        """非命令文本交给回话器。没装回话器 -> ``""``（与从前逐字一致）。"""
        if self.chat is None:
            return ""
        try:
            return self.chat.reply(msg.text, facts=self._chat_facts(msg)) or ""
        except Exception as exc:                        # noqa: BLE001
            log.warning("闲聊回话失败（%s: %s），本条不回", type(exc).__name__, exc)
            return ""

    def _text_reply(self, msg: InboundMessage) -> str:
        """一条非命令文本的回话：先认点名，认不出才走闲聊。

        **这一步必须在 `Command.parse` 之后**：``/refund ORD-2026-0001 质量问题``
        的参数里完全可能出现「财务执行岗」四个字，先判点名就会把一条命令变成一次
        岗位问答，而群里看到的是「命令没生效」，没有任何报错。

        解析本身也兜一层：点名认错了最多是少答一句，把 `handle` 带崩就是整条消息
        变成「处理失败：…」。
        """
        try:
            hit = parse_mention(msg.text)
        except Exception as exc:                        # noqa: BLE001
            log.warning("点名解析失败（%s: %s），本条按闲聊处理", type(exc).__name__, exc)
            hit = None
        if hit is None:
            return self._chat(msg)
        return self._mention(msg, *hit)

    def _mention(self, msg: InboundMessage, agent_id: str, question: str) -> str:
        """@ 了某一岗 -> 让那一岗自己答一句，带名牌回帖。

        名牌形态（``【岗位 · agent_id】 话``）与房间发声面代言时的 plain 一致：
        这条回帖仍由主通道发出，不带名牌的话，房间里看到的还是 ``maos-bot`` 用
        退款助手的口气答话 —— 而那正是这一层要消灭的观感。

        三条退化路径 —— 没接圆桌、圆桌没有 `answer`、`answer` 抛了或回了空话 ——
        **一律退回闲聊并说明白是谁在代答**。房间里的沉默与「机器人挂了」分不出来。
        """
        title = _titles().get(agent_id, agent_id)
        answer = getattr(self.team, "answer", None) if self.team is not None else None
        if answer is None:
            why = "本进程没接圆桌" if self.team is None else "本进程的圆桌没装载岗位问答"
            return self._mention_fallback(
                msg, f"（{why}，{title}暂时开不了口，下面由退款助手代答）")
        try:
            said = str(answer(agent_id, question or SELF_INTRO_Q,
                              facts=self._chat_facts(msg)) or "").strip()
        except Exception as exc:                        # noqa: BLE001
            log.warning("岗位 %s 问答失败（%s: %s），退回闲聊",
                        agent_id, type(exc).__name__, exc)
            return self._mention_fallback(
                msg, f"（{title}这次没答上来：{type(exc).__name__}，下面由退款助手代答）")
        if not said:
            # 空回答单独判：它不抛异常，照发就是在房间里发一条只有名牌的空消息。
            log.warning("岗位 %s 的问答回了空话，退回闲聊", agent_id)
            return self._mention_fallback(
                msg, f"（{title}这次没答上来，下面由退款助手代答）")
        return f"【{title} · {agent_id}】 {said}"

    def _mention_fallback(self, msg: InboundMessage, head: str) -> str:
        """点名答不上来时的回音。闲聊回话器也没装就给命令面 —— **空回帖不是选项**。"""
        tail = self._chat(msg) or "发 /team 看圆桌五岗与各自职责，发 /help 看全部命令"
        return f"{head}\n{tail}"

    def _chat_facts(self, msg: InboundMessage) -> str:
        """拼给回话器的【事实】：命令面、底账订单、本会话的待办 / 证据 / 上一张表。

        全部来自本进程已经算好或读到的东西，模型只能在这个范围里说话（铁律 8）。
        底账订单只报订单号 / SKU / 实付 / 付款日 —— 政策裁定不在这里预告，那要走预检。
        """
        rr = _load_run_requests()
        lines = ["可用命令：", USAGE, "", "底账里的订单（只有这些能起单）："]
        for o in self.ledger().get("order_snapshot", []):
            lines.append(f"  · {o.get('order_id')}  {o.get('sku')}  "
                         f"实付 {o.get('amount_paid')}  付款日 {str(o.get('paid_at') or '')[:10]}")
        lines.append("售后政策（标题）：")
        for r in self.ledger().get("policy_rule", []):
            lines.append(f"  · {r.get('rule_no', '')} {r.get('title', '')}")
        lines.append("诉求类型可写：" + "、".join(sorted(set(rr.REASONS))))

        live = [t for t in self._tickets.values()
                if t.channel == msg.channel and t.chat_id == msg.chat_id
                and not t.expired(self.ticket_ttl)]
        lines.append("")
        if live:
            # 按**裁定结论**分组，而不是把预检过的单子一律叫「待放行」。
            # 原来 50 单混在一句底下、每行也不带结论，于是「哪几单没批准」这个问题
            # 模型手里没有答案 —— 它只能从计数推出「4 单没批准」，报不出是哪四单，
            # 只好让人回去翻申请表回帖。数据一直都在（`Ticket.checked` 是建这个待办
            # 时那次 preflight 的返回），缺的只是把它说出来。
            # 结论中文一律走 `rr.DECISION_CN`，不在这一层另写一份映射 ——
            # 两套口径的症状是同一单在回帖里叫「驳回」、模型嘴里叫「拒绝」。
            groups: dict[str, list[Ticket]] = {}
            for t in live:
                groups.setdefault(str((t.checked or {}).get("decision") or ""), []).append(t)
            # 固定顺序：批准在前、驳回在后，其余按出现顺序跟在后面。
            # 靠 dict 的插入序会让同一批单子在两次问话里排出不同的样子。
            ordered = ["approve", "reject"] + [d for d in groups
                                               if d not in ("approve", "reject")]
            for decision in ordered:
                items = groups.get(decision)
                if not items:
                    continue
                cn = rr.DECISION_CN.get(decision, decision or "未裁定")
                if decision == "approve":
                    head = (f"本会话预检裁定{cn}、待放行的 {len(items)} 单"
                            "（审批人发 /approve <case_id> 才会执行）：")
                elif decision == "reject":
                    # 驳回的单子也留着待办（`handle_refund` 那句「如仍要走一次」），
                    # 但它不是「待放行」—— 说成待放行会让人以为这几单也在等他点头。
                    head = f"本会话预检裁定{cn}、不予退款的 {len(items)} 单（无需放行）："
                else:
                    head = f"本会话预检裁定「{cn}」的 {len(items)} 单："
                lines.append(head)
                lines += [f"  · {t.case_id}  {t.summary}  由 {t.requested_by} 提交"
                          for t in items]
        else:
            lines.append("本会话当前没有待放行的预检")
        waiting = len(self.pending_evidence.peek(msg.channel, msg.chat_id))
        lines.append(f"本会话待认领的证据：{waiting} 份")
        last = self._last_sheet.get((msg.channel, msg.chat_id))
        lines.append(f"本会话上一张申请表：{last}" if last else "本会话还没收到过申请表")
        lines.append(f"说话的人：{msg.sender}"
                     f"（{'在' if self.is_approver(msg.sender) else '不在'}审批人名单内）")

        # 与 `/team` 用**同一份** `render_roster`：两处各排一遍的话，模型嘴里的岗位
        # 和 `/team` 打出来的岗位会慢慢长歪，而两边都不报错。
        if self.team is not None:
            try:
                roster = render_roster(self.team.roster())
            except Exception as exc:                    # noqa: BLE001
                log.warning("取圆桌名单失败（%s: %s），本条事实不含岗位表",
                            type(exc).__name__, exc)
            else:
                if roster:
                    lines += ["", "圆桌岗位与技能：", roster]
        return "\n".join(lines)

    def _render_evidence(self, msg: InboundMessage, stored: list[StoredAttachment],
                         rejected: list[str], *, attached: str = "") -> str:
        """``attached`` 非空 = 这批证据当场就挂上某一单了（`_recheck`）。

        那时暂存里已经没有它们，再说「暂存 30 分钟，等一条 /refund 认领」
        与「这个会话现有 N 份待认领证据」就都是假话 —— 人照着它再打一句
        ``/refund``，得到的会是一单没有证据的预检。
        """
        if not stored and not rejected:
            return ""
        lines: list[str] = []
        if stored:
            # 挂上案子的那一批不能再说「暂存、等一条 /refund 认领」—— 暂存里已经
            # 没有它们了，人照着这句再打一句 /refund，得到的是一单没有证据的预检。
            lines.append(f"已收下 {len(stored)} 份证据：" if attached else
                         f"已收下 {len(stored)} 份证据（暂存 "
                         f"{self.pending_evidence.ttl // 60} 分钟，等一条 /refund 认领）：")
            for item in stored:
                # 只报 digest 前 12 位：全长 64 位在手机上要折三行，而 12 位
                # 足够人工比对，也足够 `AttachmentStore.read` 之外的任何用途。
                lines.append(f"  · {item.filename}（{item.mime}，"
                             f"{item.size // 1024} KB，sha256:{item.digest[:12]}）")
        if rejected:
            lines.append(f"未收下 {len(rejected)} 份：")
            lines.extend(f"  · {r}" for r in rejected)
        if stored and attached:
            lines.append(attached)
        elif stored:
            waiting = len(self.pending_evidence.peek(msg.channel, msg.chat_id))
            lines.append(f"这个会话现有 {waiting} 份待认领证据。"
                         f"接着发：/refund <订单号> <诉求类型>")
        return "\n".join(lines)

    def _dispatch(self, msg: InboundMessage, cmd: Command) -> str:
        if cmd.verb in CMD_APPROVAL:
            return self.handle_approval(msg, cmd)
        if cmd.verb == CMD_REFUND:
            return self.handle_refund(msg, cmd.args)
        if cmd.verb == CMD_PENDING:
            return self.handle_pending(msg)
        if cmd.verb == CMD_TEAM:
            return self.handle_team(msg)
        if cmd.verb == CMD_HELP:
            return USAGE
        if cmd.verb in CMD_OUTCOME:
            return self.handle_outcome(msg, cmd)
        return ""                                       # 不是我们的命令词，不接管

    # -- 幂等 ---------------------------------------------------------------
    def _claim(self, msg: InboundMessage) -> bool:
        """首次见到这条消息返回 True。**没有 msg_id 一律放行**。

        放行是刻意的：拿不到 msg_id 说明平台的形状变了或解析漏了，此时把消息
        丢掉会让「群里发命令没反应」成为静默行为。宁可重复处理一次（幂等的
        真正保障在下游 —— `claim_idempotency` 也保护着每一次状态迁移），
        也不要静默吞掉。这条走 WARNING，让它在日志里显形。
        """
        if not msg.msg_id:
            log.warning("入站消息没有 msg_id（channel=%s），本条不去重", msg.channel)
            return True
        seen = self.store.claim_idempotency(msg.dedup_key, "ingress.message", "")
        return seen is None

    # -- 起单 ---------------------------------------------------------------
    def handle_refund(self, msg: InboundMessage, args: list[str]) -> str:
        """``/refund <订单号> <诉求类型> [金额] [日期]`` -> **只读预检** + 挂一条待办。

        这一步一分钱都不动：不建案、不跑 plan、不碰网关。谁都可以发（问「这单
        能退多少」本来就该人人能问），真正的执行要审批人回 ``/approve <case_id>``。
        """
        rr = _load_run_requests()
        if len(args) < 2:
            raise CommandError("至少要给订单号和诉求类型，例：/refund ORD-2026-0001 质量问题")

        order_id, reason_raw = args[0], args[1]
        amount_raw = args[2] if len(args) > 2 else ""
        date_raw = args[3] if len(args) > 3 else ""
        try:
            req = {
                "order_id": order_id,
                "reason": rr._reason_code(reason_raw),
                "amount": float(amount_raw.replace(",", "")) if amount_raw else None,
                "requested_at": rr._iso(date_raw),
            }
            payload = rr.build_case(self.ledger(), req)
        except rr.RequestSheetError as exc:
            raise CommandError(str(exc)) from exc
        except ValueError as exc:
            raise CommandError(f"金额 {amount_raw!r} 不是数字：{exc}") from exc
        amount = req["amount"]
        if amount is not None and not math.isfinite(amount):
            # float() 认 'nan' / 'inf'；nan 跟什么比都是 False，`<= 0` 拦不住它，
            # 最后在核算里以 Decimal InvalidOperation 炸成一个 FAILED 的 plan。
            raise CommandError(f"金额 {amount_raw!r} 不是数字")
        if amount is not None and amount <= 0:
            # 与申请表那条口径一致（`sheet._parse_row`）。负数能一路穿到核算：
            # `min(-500, 实付)` 取到负数再被 `max(…, 0)` 抹成 0，最后记成一笔
            # 「已到账」的 0 元退款 —— 入口这里就得拦。
            raise CommandError(f"金额 {amount_raw} 不能是负数或 0；想按订单实付退就把金额留空")

        # 认领这个会话暂存的照片，挂成 case 自带的 `customer_evidence`。
        # 塞进 payload 而不是直接写库：`refund.intake` 见到带 uri 的信号才落库，
        # 那是 customer_evidence 唯一那条写入路径（`domain/refund/fixtures.py` 的
        # `evidence_signals_of`）。绕开它自己 INSERT 会得到两条落库路径，
        # 而证据重复的症状是同一张图在案子里出现两次、evidence_id 还不一样。
        claimed = self.pending_evidence.claim(msg.channel, msg.chat_id)
        if claimed:
            payload["customer_evidence"] = [
                item.as_evidence(f"ev-{i:02d}") for i, item in enumerate(claimed, 1)
            ]

        checked = preflight(payload)
        ticket = Ticket(
            case_id=checked["case_id"], payload=payload,
            summary=f"{order_id}（{reason_raw}）", channel=msg.channel,
            chat_id=msg.chat_id, requested_by=msg.sender, created_at=time.time(),
            evidence=tuple(claimed), checked=checked,
        )
        with self._lock:
            # 同一单重发 /refund 直接覆盖：待办是「当前想退这一单」的意思，
            # 留着两条只会让 /approve 不知道该批哪一条。
            self._tickets[ticket.case_id] = ticket
        self._record(("preflight", ticket))             # 锁外登记，`handle` 末尾才 fire
        return self._render_preflight(checked, ticket)

    def _render_preflight(self, c: dict, ticket: Ticket, *,
                          recheck: bool = False) -> str:
        """``recheck=True`` 只换抬头那两个字与末尾那句预告。

        中间四行（裁定 / 依据 / 金额 / 下一步）**一个字都不许因为轮次而变** ——
        补一张图不会让政策变松，复检卡与预检卡说的必须是同一套话。
        """
        rr = _load_run_requests()
        decision = rr.DECISION_CN.get(c["decision"], c["decision"])
        lines = [
            f"{'复检' if recheck else '预检'} · {ticket.summary} · 案子 {c['case_id']}",
            f"裁定：{decision} —— {c['why']}",
            # `deciding_rule` 为 None 表示「没有适用的时限规则，按基线裁定」——
            # 直接打 None 会让群里以为程序出错了，而它其实是个正常结论。
            f"依据：{c['deciding_rule'] or '基线裁定（无适用的时限规则）'}"
            f"（订单锁定政策 v{c['pinned_policy_version']}），"
            f"付款至申请 {c['elapsed_days']} 天",
            f"申报金额：{c['amount_claimed']}",
        ]
        if ticket.evidence:
            # 认领了几张要说出来。人的疑问是「我刚发的那三张挂上了吗」，
            # 而这一句是唯一能回答它的地方 —— 暂存已经被 claim 清空了。
            lines.append(f"随案证据：{len(ticket.evidence)} 份（本会话上传，"
                         f"{'、'.join(e.digest[:8] for e in ticket.evidence)}）")
        if c["decision"] == "approve":
            # 核准金额由核算那一步（第六道闸）算，预检不预告它 —— 预告一个
            # 未经核算的数字，群里会把它当成承诺。
            lines.append(f"需 {c['approver_role']} 放行后执行核算与付款：")
            lines.append(f"  /approve {c['case_id']}")
        else:
            lines.append(f"不予退款，无需执行。如仍要走一次：/approve {c['case_id']}")
        lines.append("（本条为只读预检，尚未动任何资金）")
        if self.team is not None:
            # 只预告、不写结论：此刻一岗都还没发言（钩子在 `_reply` 之后才 fire），
            # 在这里写一句「建议批复」就是凭空捏一个裁定（红线 R1/R2）。
            # 「复检」与「合议」的区别只是告诉房间这不是第一次看这一单 ——
            # 措辞里同样一个结论都不许有，齐不齐是证据核验岗的事。
            lines.append("五岗正在复检，稍后给出批复建议" if recheck
                         else "五岗正在合议，稍后给出批复建议")
        return "\n".join(lines)

    # -- 执行 ---------------------------------------------------------------
    def handle_execute(self, msg: InboundMessage, ticket: Ticket,
                       approved: bool, reason: str = "") -> str:
        """审批人放行/撤销一条待办。放行才真跑 `run_payload`。"""
        with self._lock:
            self._tickets.pop(ticket.case_id, None)
        if not approved:
            log.info("待办 %s 被 %s 撤销：%s", ticket.case_id, msg.sender, reason)
            return f"已撤掉待办 {ticket.case_id}（操作人 {msg.sender}）" + (
                f"，原因：{reason}" if reason else "")
        if ticket.expired(self.ticket_ttl):
            return (f"待办 {ticket.case_id} 已过期（超过 {self.ticket_ttl // 3600} 小时）。"
                    "预检结论是按当时的政策与日期算的，请重新 /refund")

        # 同一个案子在同一个库里**只跑一次**（T122）。从前 `run_payload` 每次自建一个
        # `:memory:`，重跑是无害的；现在处置跑在这个长命的库上，重跑有两层后果：
        #
        #   · 技术上必炸：`task_id` 由 case_id 推出而不是 `new_id`
        #     （`flows/contrast.py::plan_tasks`，为的是「连跑两次输出逐条一致」），
        #     第二遍在 `insert_task` 撞 UNIQUE。
        #   · 业务上更糟：就算不炸，重跑会给同一个案子产出**第二套**付款请求与观察，
        #     于是「这一单的钱到底怎么样了」有了两份答案，而铁律 8 说这件事的权威
        #     只能有一处。
        #
        # 所以这里拦在跑之前，并把已经跑出来的结论报回去 —— 人打第二次 `/approve`
        # 多半是没看到第一次的回帖，他要的就是这句话。
        ran = self._case_row(ticket.case_id)
        if ran is not None:
            # 这道闸拦对了，但从前它是条死路：自动开单也失败的案子，`/assign`
            # `/resolve` 都走 `require_ticket`（明写不补开），于是长命库里那个案子
            # 永久锁死。T129 之后指一条出路 —— `/compensate` 补开一张（已有单不重开）。
            return (f"{ticket.case_id} 已经在本房间跑过一次（plan {ran['plan_id']}，"
                    f"当前业务状态 {ran['biz_status']}），不重跑。\n"
                    f"要看这一单现在怎么样了：/pending；"
                    f"钱没退出去的话按上一条回帖里的 /assign、/resolve 往下走；"
                    f"连工单都没开出来就先 /compensate {ticket.case_id}")

        # `is not None` 而不是 `or`：注入的处置器完全可能是个 falsy 的可调用对象
        # （测试里那个继承 list 的记录器就是），`or` 会静默把它换成真跑的那个。
        run = self._runner if self._runner is not None else _default_runner
        # 串行化：`run_payload` 会调 `C.reset_gateways()` 重置一个**进程级**的网关
        # 注册表。两单并发跑，后一条的 reset 会把前一条的网关摘掉，症状是前一条
        # 在发起付款时报「网关未注册」—— 而它自己的输入毫无问题。
        # 跑起来之后才出现的闸**不代签**（T135）—— 理由见 `HOLD_AWAITS`。
        # 照 `_accepted_extra` 的取向探参数：不认这个关键字的处置器（测试里的替身）
        # 一个字都不多收，行为退化成 T135 之前的代签，那几条测试因此一条不受影响。
        hold = {"hold_awaits": HOLD_AWAITS} if _takes_kw(run, "hold_awaits") else {}
        # 闸循环里代跑的那一跳署**房间里按键的那个人**（T137），不是 CLI 那个写死的
        # `custom_case.APPROVER`。T135 之后付款闸这一跳已经署真人名（那次决定就发生
        # 在房间里），核算那一跳却还署一个常量 —— 同一条 HITL trace 上两跳署了两个
        # 不同来源的名字，而事后问「谁批的这一单」，库里给的是后者。
        # 代跑的语义没变：卡片上「由处置流程代跑」那句照旧，变的只是署名的来源。
        # 探参数的理由同 `hold`：不认这个关键字的替身一个字都不多收。
        who = {"gate_operator": msg.sender} if _takes_kw(run, "gate_operator") else {}
        with self._lock:
            # `store=self.store`（T122）：处置跑在 **router 自己的库**上，不再是
            # `run_payload` 每次自建又随手丢掉的那个 `:memory:`。这一句是四条结果面
            # 命令（`/assign` `/resolve` `/confirm` `/complain`）成立的前提 ——
            # 工单、退款申请、到账观察从此查得到；`MAOS_INGRESS_DB` 指到文件时，
            # 连跨进程回查都成立。
            result = run(ticket.payload, approve=True, verbose=False, store=self.store,
                         **hold, **who)
        # 锁**释放之后**才登记。钩子里的圆桌会再碰一次 router（取底账、报待办），
        # 在锁内触发就是自己等自己 —— 而症状是房间彻底不动，没有任何报错。
        self._record(("execute", ticket.payload, result, msg.sender))
        head = f"已放行 {ticket.case_id}（操作人 {msg.sender}）\n"
        # 停在那一类闸上时**先不开补偿工单**：补偿是「看过事实之后的决定」
        # （`_compensate_if_stuck` 的 docstring），而这一单的决定还没做出来 ——
        # 单先开出来，人再打 `/reject` 就成了给一个已经开好的单补一张签字，
        # 那第二次决定就只是走过场了。开单挪到第二次决定之后
        # （`handle_gate_decision`），两条路开的是同一张单、走同一个函数。
        held = _held_gates(result)
        if held:
            return head + self._render(result, title=ticket.summary) + \
                _gate_prompt(held, ticket.case_id, result)
        # **先补偿、再渲染**。开单那一步会把 `biz_status` 推到 `compensated`，
        # 卡片必须照补偿**之后**的库讲话。反过来（从前那样把 `_render` 写在
        # 表达式左边、靠求值序先跑）的症状是同一条回帖自相矛盾：上半截说
        # 「业务状态：已提交网关·未确认 / 对客户口径：已提出退款」，下半截说
        # 「钱没退出去，已开人工补偿工单」—— 而那一刻库里已经是 compensated。
        # 拼接位置不变：这段仍然接在卡片末尾。
        tail = self._compensate_if_stuck(result, msg)
        return head + self._render(result, title=ticket.summary) + tail

    def _compensate_if_stuck(self, r: dict, msg: InboundMessage) -> str:
        """网关明确失败、钱没退出去 -> 开一张人工补偿工单，并说出下一步（T122）。

        ## 为什么这一步在编排层，而不在 `run_payload` 里

        `run_payload` 只做「按计划跑一遍并如实记下观察到的事实」，补偿是**看过事实
        之后的决定**，一直由调用方下：`scripts/make_case_bundle.py` 的 gateway_fail
        路径就是跑完之后显式调一次 `refund.compensate`。房间是这条链路的第二个调用方，
        这一句是它那一份。挪进 `run_payload` 会让每条 CLI 路径都跟着自动补偿，
        而那几条正在演的是「人决定要不要补」。

        ## 判据只认观察，不认任务状态（铁律 8）

        「钱到底退没退出去」的权威是 `payment_observation`，不是付款任务收在哪个态 ——
        任务 DONE 而钱没到账，正是评委那句「所有 Agent 都回复完成不代表业务成功」
        指的病。所以这里只看最后一条观察是不是 `failed`，且一条 settled 都没有。
        `unknown` / `unobserved` **一律不补偿**：下落不明不等于失败，对一笔可能已经
        退出去的钱再退一次，比不补偿贵得多。

        开单失败不许把一次**已经生效**的放行升级成崩溃：记日志，并在回帖里说明
        「工单没开出来」——房间里的人据此去查，总好过以为已经有单在等人接。
        """
        obs = r.get("payment_observations") or []
        if r.get("settled_observations") or not obs:
            return ""
        last = obs[-1] if isinstance(obs[-1], dict) else {}
        if str(last.get("observed_state") or "") != "failed":
            return ""

        case_id, tenant_id = str(r.get("case_id") or ""), str(r.get("tenant_id") or "")
        plan_id = str(r.get("plan_id") or "")
        code = str(last.get("gateway_code") or "未知码")
        # 付款那一步的 task_id：审计行要挂在**它**上面，挂到别的任务上，
        # 「补偿是因为哪一步走不通」在 trace 里就断了。
        payment = next((str(t.get("task_id")) for t in (r.get("tasks") or [])
                        if str(t.get("task_id", "")).endswith("-payment")), "")
        try:
            trace_id = str((self.store.get_plan(plan_id) or {}).get("trace_id") or "")
        except Exception:                               # noqa: BLE001
            trace_id = ""

        from maos.skills.invoker import SkillInvoker
        res = SkillInvoker(_outcome_cmds.COMPENSATION_DESK_IDENTITY, self.store).invoke(
            _outcome_cmds.SKILL_COMPENSATE,
            {"tenant_id": tenant_id, "case_id": case_id, "operator": msg.sender,
             # 措辞与 `make_case_bundle.py` 那一句**逐字相同**：同一件事在证据束里
             # 和在房间里该说同一句话，两处各写一句的症状是对不上账。
             "reason": f"网关明确失败（{code}），机器返工修不好，转人工线下退款"},
            extras={"plan_id": plan_id, "task_id": payment, "trace_id": trace_id})
        if res.status != "ok" or not isinstance(res.output, dict):
            log.warning("案子 %s 网关失败但补偿工单没开出来：%s", case_id, res.error)
            # 回帖去掉异常类名（T129，口径同 `outcome_commands.humanize`），并把**补开
            # 那条命令**说出来：T129 之前这里是条死路 —— `/approve` 被「不重跑」拦死、
            # `/assign` `/resolve` 都走 `require_ticket`（明写不补开），长命库里这种案子
            # 只能换库。一句「请人工介入」不告诉人介入的入口，等于没说。
            return (f"\n⚠️ 这一单钱没退出去，但补偿工单没开出来"
                    f"（{_outcome_cmds.humanize(res.error)}）\n"
                    f"  补开：/compensate {case_id}")

        out = res.output
        # 把补偿**之后**的事实写回观测，好让紧接着跑的 `_render` 照新库讲话
        # （调用序见 `handle_execute`）。对外口径仍然**只经**
        # `domain/refund/projection.py` 取值 —— 这里拼一句字面值就是契约 §D
        # 说的「第二处措辞」，而第二处迟早会跟第一处漂。
        r["biz_status"] = out.get("biz_status", r.get("biz_status"))
        r["public_status"] = _projection.public_status(
            str(r.get("biz_status") or ""),
            self._has_refund_request(tenant_id, case_id),
            _projection.observed_state_of(obs))
        opened = out.get("ticket") if isinstance(out.get("ticket"), dict) else {}
        ticket_id = _outcome_cmds.CP.ticket_id_of(case_id)
        role = str(opened.get("assignee_role") or "")
        # 承接岗按开单口径落在**放行人所在的岗**上（`compensate._opening_role`：
        # 他此刻最清楚上下文），所以下面那句 `/assign` 不是走过场 —— 它是把这张单
        # 从「谁批的谁先背着」交到真正干活的支付运维手上。
        seat = (f" · 当前承接岗 {_outcome_cmds.roles.title_of(role)}（{role}）"
                f"· 接单人 {opened.get('assignee') or '未指名'}" if role else "")
        return (f"\n钱没退出去（{code}），已开人工补偿工单：{ticket_id}{seat}\n"
                f"  改派：/assign {ticket_id} payment_ops\n"
                f"  关单：/resolve {ticket_id} <渠道流水号> <线下凭证摘要>")

    def _has_refund_request(self, tenant_id: str, case_id: str) -> bool:
        """库里有没有本案的 `refund_request` 行 —— 口径逐字同 `custom_case._observe()`。

        `projection.public_status` 的第二个入参就是它：`approved` 加上这一行才是
        客户口径的「已提出退款」。查不动（表还没建、库读不了）一律当作**没有**——
        拿 False 最多少说一句话，拿 True 则可能替这个案子宣布一件没发生的事（铁律 8）。
        """
        try:
            return bool(_refund_objects.query(
                self.store,
                "SELECT request_id FROM refund_request"
                " WHERE tenant_id=? AND case_id=? LIMIT 1",
                (tenant_id, case_id)))
        except Exception as exc:                        # noqa: BLE001
            log.warning("查案子 %s 的退款申请行失败（%s）—— 当作没有", case_id, exc)
            return False

    def _render(self, r: dict, *, title: str) -> str:
        """把 `_observe()` 的观测结果排成一张群里能一眼读完的卡。"""
        rr = _load_run_requests()
        decision = rr.DECISION_CN.get(r.get("decision"), r.get("decision"))
        lines = [
            f"{title} · 案子 {r.get('case_id')}",
            f"裁定：{decision} —— {r.get('why')}",
        ]
        if r.get("decision") == "approve":
            lines.append(
                f"核准金额：{r.get('amount_approved')}"
                f"（政策 v{r.get('policy_version_used')}，依据 {r.get('rule_refs')}）")
        lines.append(f"业务状态：{rr.STATUS_CN.get(r.get('biz_status'), r.get('biz_status'))}")

        # 对客户口径（跨轨契约 §C）—— 上面那行 `STATUS_CN` 是**内部七态**，给房间里
        # 干活的人看；这一行是同一个案子对外能说的话，五个字面值唯一来源
        # `domain/refund/projection.py` 的 `PUBLIC_*`，`custom_case._observe()` 已经
        # 算好放在 `public_status` 里。**渲染走 `public_status_line()`**（T129）：
        # 圆桌财务岗的事实卡念的是同一个函数的返回，两张嘴从此不可能说得不一样。
        # 三档的判据与理由全在那个函数的 docstring 里，这里不复述、更不重写一份。
        public_line = public_status_line(r.get("public_status"),
                                         case_id=str(r.get("case_id") or ""))
        if public_line:
            lines.append(public_line)

        # 铁律 8：钱到没到账只认观察。没有 settled 观察就明说没有，不含糊。
        settled = r.get("settled_observations") or 0
        obs = r.get("payment_observations") or []
        if settled:
            lines.append(f"到账观察：{settled} 条（已确认到账）")
        elif obs:
            lines.append(
                f"到账观察：0 条 —— 已提交网关但**未确认到账**，"
                f"最后一次观察是 {obs[-1].get('observed_state')}")
        else:
            lines.append("到账观察：0 条（本单未走到付款）")

        exits = r.get("human_exits") or []
        if exits:
            # 说清楚这是**第二层**：群里那次 /approve 决定的是「这一单要不要办」，
            # 而这些是 Plan 跑起来之后闸门拦下的任务级审批点。不点破，群里会以为
            # 自己刚才那次放行是多余的。
            #
            # **代跑的只是其中一部分**（T135）：`held_*` 那几条一个字都没代签，
            # 正等着人。把它们一并说成「由处置流程代跑」，是在卡片上宣布一件没发生
            # 的事 —— 而人会照着这句话以为自己不用再管了，那一单就停在那里没人动。
            held_n = sum(1 for e in exits if _is_held(e))
            lines.append(
                f"Plan 内任务级审批点 {len(exits)} 个（与群里这次放行不是同一层）"
                + (f"，其中 {held_n} 个**没有代跑**、正等你决定：" if held_n
                   else "，由处置流程代跑："))
            lines += [f"  · {e.get('title')}"
                      f"（{EXIT_DECISION_CN.get(e.get('decision'), e.get('decision'))}）"
                      f"—— {e.get('why')}" for e in exits]
        lines.append(f"Plan {r.get('plan_id')} 收在 {r.get('plan_state')}")
        return "\n".join(lines)

    # -- 审批 ---------------------------------------------------------------
    def handle_approval(self, msg: InboundMessage, cmd: Command) -> str:
        """审批命令有两个落点，按参数分流。**渠道闸永远在最前面。**

        · 参数是本进程的待办 ``case_id`` -> 放行/撤销那条待办（本层自己判名单）。
        · 其余（``task_id``）-> 原样转给 `RoomApprovalBridge`，那是任务级审批，
          判定序已经在 Matrix 上跑绿，这里一行都不重复实现。

        分流靠**查表**而不是看 id 长什么样：按前缀猜的那天，一个恰好以 ``RC-``
        开头的 task_id 就会被当成待办处理，而两边都不会报错。
        """
        if msg.channel not in ALLOW_APPROVAL:
            log.warning("外部渠道 %s 的 %s 试图发审批命令，已拒",
                        msg.channel, msg.sender)
            return "该渠道不受理审批命令（审批只在企业内部渠道进行）"

        target = cmd.args[0] if cmd.args else ""
        ticket = self._tickets.get(target)
        if ticket is not None:
            approved = cmd.verb == "approve"
            if not self.is_approver(msg.sender):
                # 先判名单再看别的：越权尝试要留痕，且**不许**降级成一句用法提示
                # （口径同 `RoomApprovalBridge`：先认命令词、再查名单、最后校参数）。
                log.warning("越权：%s 不在 MAOS_APPROVERS 名单内，试图 %s %s",
                            msg.sender, cmd.verb, target)
                return f"无审批权限：{msg.sender} 不在 MAOS_APPROVERS 名单内"
            return self.handle_execute(msg, ticket, approved,
                                       reason=" ".join(cmd.args[1:]))

        # 待办没了，但这个案子可能停在一道**跑起来之后才出现的**闸上（T135）：
        # `handle_execute` 跑完就把待办摘了，而付款那一步还在 BLOCKED 上等第二次
        # 决定。查表而不是看 id 长什么样，口径同上面那一档。
        gate = self._held_gate_of(target)
        if gate is not None:
            if not self.is_approver(msg.sender):
                log.warning("越权：%s 不在 MAOS_APPROVERS 名单内，试图 %s %s 的人工闸",
                            msg.sender, cmd.verb, target)
                return f"无审批权限：{msg.sender} 不在 MAOS_APPROVERS 名单内"
            return self.handle_gate_decision(msg, gate, cmd.verb == "approve",
                                             reason=" ".join(cmd.args[1:]))

        if self.approval_bridge is None:
            return (f"没有待办 {target}。本进程也没接长驻运行时，"
                    "任务级 /approve 无处可落")
        return self.approval_bridge.handle_message(msg.sender, msg.text)

    def handle_gate_decision(self, msg: InboundMessage, gate: dict, approved: bool,
                             reason: str = "") -> str:
        """房间里那**第二次**人工决定：跑起来之后才出现的闸，由人当场签或不签（T135）。

        ## 为什么非有这一条不可

        群里那次 `/approve` 签的是「这一单要不要办」。付款跑起来、网关回了一个终态
        失败码之后，控制面落下的那道闸问的是另一件事：「钱没退出去，这一步还算不算
        完成」。两件事发生在不同的时刻、答案也可能相反，用一次签字把两个都签了，
        第二个答案就是编的 —— 而 Plan 会因此收在 DONE，「所有 Agent 都回复完成」
        而钱还在账上。这条命令把第二次决定交还给人。

        ## 为什么要把运行时重新装配一遍

        `handle_execute` 那次调用返回之后，跑它的那套 cp/bus/gate 就没了（房间这边
        没有长驻运行时）。而人的决定要真的把 Plan 推下去：驳回要让付款那一步落
        FAILED、Plan 跟着收在 FAILED；放行要让下游的通知任务接着跑完。`build()` 是
        幂等的（`init_schema()` 全是 IF NOT EXISTS），**且库是同一个** —— 重新装配
        出来的这套读到的是同一份事实，不是第二份。

        `build({})` 的模型缺省就是 `ScriptedModelClient`（`flows/common.py`），
        这一句不会去打任何真模型 —— 房间里按一次 `/reject` 不该花钱，更不该在
        没有 key 的机器上因此失败。

        ## 操作者写的是**房间里按键的那个人**

        不是 `custom_case.APPROVER` 那个 CLI 代跑用的写死名字。这一跳是 HITL trace
        上唯一一条「人在现场做的决定」，署名署错了，这条链就只剩形式。

        ## 这次决定同时落进退款域那张审批表（T137）

        `cp.human_decision()` 只落 `event_log` 的迁移事件，而 CLI 那条路另落一条
        `approval_record` 并挂成 business_ref。两条路的留痕位置不一样，缺的那半边
        正是「谁在什么时候驳回了这笔」—— 见 `_record_gate_approval()`，
        那里也写着「驳回这一跳」与「驳回整个案子」在库里怎么区分。
        """
        from maos.flows.common import build, run_until_settled

        case_id, plan_id, task_id = gate["case_id"], gate["plan_id"], gate["task_id"]
        note = reason or ("仍然放行：已确认钱通过别的渠道到账" if approved
                          else "钱没退出去，这一步不算完成")
        # 串行化同 `handle_execute`：重新装配的这套会再碰一次进程级的网关注册表。
        with self._lock:
            _s, bus, cp, _m, _w, reviewer = build({}, store=self.store)
            try:
                cp.human_decision(task_id, approved, operator=msg.sender, note=note)
            except Exception as exc:                    # noqa: BLE001
                # 多半是 `assert_transition` —— 这个任务已经被人决定过了。房间里
                # 两个人同时看见那条提示、同时按键是常事，第二个人该收到一句人话，
                # 不是一个栈。**不许**因此把第一次那个已经生效的决定说成失败。
                log.warning("案子 %s 的人工闸决定没落下：%s", case_id, exc)
                return (f"{case_id} 的「{gate['title']}」这一步没能落下这次决定"
                        f"（{_outcome_cmds.humanize(exc)}）—— 多半是已经有人决定过了。"
                        f"看一眼现在停在哪：/pending")
            # 决定落下之后再补审批表那一行（T137）：顺序不可换 —— `human_decision`
            # 抛了就说明这次决定根本没生效，那时落一行审批记录等于记了一件没发生的事。
            self._record_gate_approval(case_id, plan_id, task_id,
                                       approved=approved, operator=msg.sender, note=note)
            run_until_settled(bus, reviewer, cp, plan_id)
        self._record(("gate_decision", case_id, {"task_id": task_id,
                                                 "approved": approved}, msg.sender))

        # 补偿**在这里**开，不在 `handle_execute` 里：补偿是「看过事实之后的决定」，
        # 而那个决定刚刚才做出来。判据一个字节没动，仍只认 `payment_observation`
        # 的最后一行（铁律 8）—— 人驳回的是「这一步算不算完成」，不是「钱退没退」。
        r = self._observed_for_compensation(case_id, plan_id)
        tail = self._compensate_if_stuck(r, msg)
        rr = _load_run_requests()
        plan = self.store.get_plan(plan_id) or {}
        lines = [f"{'已放行' if approved else '已驳回'} {case_id} 的"
                 f"「{gate['title']}」这一步（操作人 {msg.sender}）",
                 f"  {note}",
                 f"业务状态：{rr.STATUS_CN.get(r.get('biz_status'), r.get('biz_status'))}"]
        # 对外口径仍然**只经** `public_status_line()`（T129 收的口，跨轨契约 §C）——
        # 这里一个新字面值都不拼。`_compensate_if_stuck` 已经把补偿之后的值写回
        # `r` 了，所以这一行念的是补偿**之后**的事实，与卡片那条口径逐字同源。
        public_line = public_status_line(r.get("public_status"), case_id=case_id)
        if public_line:
            lines.append(public_line)
        lines.append(f"Plan {plan_id} 收在 {plan.get('state')}")
        return "\n".join(lines) + tail

    def _record_gate_approval(self, case_id: str, plan_id: str, task_id: str, *,
                              approved: bool, operator: str, note: str) -> dict | None:
        """把房间里这次闸决定补进退款域那张审批表（T137）。落不下只记日志，返回 None。

        ## `event_log` 那一跳为什么不够

        `cp.human_decision()` 落的是一条 `StateTransition`，操作者完整在册 ——
        HITL 证据束读的就是它（`scripts/make_case_bundle.py::collect_hitl`），
        **本方法一个字节都不改变它**。缺的是退款域那张 `approval_record`：
        CLI 那条路（`flows/custom_case.py` 的闸循环）两个分支各落一行并把它挂成
        business_ref，房间这条路一行都不落。于是「谁在什么时候驳回了这笔」在库里
        查不到 —— 而客户投诉时要对的第一件事就是它（那句话是 `custom_case` 那条路
        自己的注释）。房间里这次驳回是**真人**当场做的，比 CLI 代跑那次更该落。

        ## 「驳回这一跳」与「驳回整个案子」在库里怎么区分

        用的全是既有字段，一个新状态、一条新迁移、一个新列都没加（铁律 9）：

        · **这一行落在哪一跳** —— `stamp_approval_revision` 挂的那条 business_ref
          带着 `task_id`，指到付款那一步。审批表第 N 行属于哪道闸，查这条引用。
        · **整个案子有没有被驳回** —— 看 `refund_case.biz_status` 是不是 `rejected`，
          那是业务对象自己的字段，与审批表各记各的。

        所以本方法**不碰 `biz_status`**，也就不调 `custom_case._reject_case()`：
        房间这一跳驳的是「钱没退出去，这一步算不算完成」，不是「这一单不予退款」。
        案子此刻停在 `gateway_accepted`（付款已经发起过了），而 `_reject_case` 那条
        submitted/approved 守卫正是为这个分界设的 —— 它本来也不会放行。两件事各落
        各处，合进一个字段就再也分不开了。

        ## 放行那一支照样落

        CLI 那条路两个分支都落（`custom_case.py` 的 `record_approval` 各一句），
        房间只落驳回那一支的话，同一张表上「放行」与「驳回」两种决定的可查性就
        不一样了 —— 而下一个人读这张表时无从知道「查不到放行记录」是没发生过
        还是没记。`reason` 原样取房间那条 `note`，不另编一句。
        """
        # 惰性 import，口径同 `_compensate_if_stuck` 里那句 `SkillInvoker`：
        # `maos.ingress` 不为拿两个函数把整个 skill 包挂到自己的 import 图上。
        from maos.skills.builtin.refund import _common as _refund_common

        row = self._case_row(case_id) or {}
        tenant_id = str(row.get("tenant_id") or "")
        if not tenant_id:
            log.warning("案子 %s 查不到租户 —— 房间这次闸决定没能补进审批表", case_id)
            return None
        try:
            approval = _refund_common.record_approval(
                self.store, tenant_id=tenant_id, case_id=case_id, approver=operator,
                decision="approved" if approved else "rejected", reason=note)
            _case_pack.stamp_approval_revision(
                self.store, approval, plan_id=plan_id, task_id=task_id)
        except Exception as exc:                        # noqa: BLE001
            # 留痕失败不许把一次**已经生效**的闸决定说成失败：那个决定上一句就落库了。
            # 口径同 `_compensate_if_stuck` 的开单失败那一支。
            log.warning("案子 %s 的闸决定没能补进审批表（%s: %s）",
                        case_id, type(exc).__name__, exc)
            return None
        return approval

    def _held_gate_of(self, case_id: str) -> dict | None:
        """这个案子有没有停在一道**跑起来之后才出现的**闸上；没有返回 None。

        判据只此一份，取自 `custom_case.await_kind()` —— 在这里另写一遍，两条路
        迟早对「这个闸该不该由我代签」给出不同答案，且两边都不报错。

        只按 BLOCKED 捞是不够的：`effect_risk=H` 那一类闸规划期就知道会来，群里那次
        `/approve` 签得到，处置流程已经代跑过了。把它也捞出来问人，就是同一件事
        问两遍 —— 而第二遍问的时候任务早就是 DONE 了，人按下去只会收到一句报错。
        """
        row = self._case_row(case_id)
        if row is None or not row.get("plan_id"):
            return None
        plan_id = str(row["plan_id"])
        try:
            tasks = self.store.list_tasks(plan_id)
        except Exception as exc:                        # noqa: BLE001
            log.warning("查 plan %s 的任务失败（%s）—— 当作没有等人的闸", plan_id, exc)
            return None
        from maos.flows.custom_case import await_kind

        for task in tasks:
            if task["state"] != TaskState.BLOCKED:
                continue
            kind = await_kind(self.store, plan_id, task["task_id"])
            if kind in HOLD_AWAITS:
                return {"case_id": case_id, "plan_id": plan_id,
                        "task_id": task["task_id"], "title": task["title"],
                        "await": kind, "why": _why_blocked(self.store, plan_id,
                                                           task["task_id"])}
        return None

    def _held_gates_all(self) -> list[dict]:
        """库里所有停在那一类闸上的案子。**扫库不扫内存**。

        `handle_execute` 一跑完就把待办摘了，进程重启之后内存里更是什么都没有，
        而闸还在库里停着。只认内存的话 `/pending` 会说「没有等人的闸」，那一单
        就永远等不到第二次决定 —— 与改造前 Plan 静默收在 DONE 一样难查，只是换了
        个地方静默。
        """
        try:
            rows = _refund_objects.query(
                self.store, "SELECT case_id FROM refund_case ORDER BY case_id", ())
        except Exception as exc:                        # noqa: BLE001
            log.warning("扫退款案子失败（%s）—— 当作没有等人的闸", exc)
            return []
        out: list[dict] = []
        for row in rows:
            gate = self._held_gate_of(str(row["case_id"]))
            if gate is not None:
                out.append(gate)
        return out

    def _observed_for_compensation(self, case_id: str, plan_id: str) -> dict:
        """补偿判据要的那几件事实，形状同 `custom_case._observe()` 的对应字段。

        **重读库，不缓存第一次那份**：两次决定之间隔着人的时间，中间完全可能有人
        打过 `/compensate`、或网关那边又回了一条观察。拿旧快照下判断，判的是一个
        可能已经不成立的事实 —— 权威在 `payment_observation` 那几行上，不在进程内
        存着的那个 dict 里（铁律 8）。
        """
        row = self._case_row(case_id) or {}
        tenant_id = str(row.get("tenant_id") or "")
        try:
            obs = _refund_objects.query(
                self.store,
                "SELECT * FROM payment_observation WHERE tenant_id=? AND case_id=?"
                " ORDER BY observed_at", (tenant_id, case_id))
        except Exception as exc:                        # noqa: BLE001
            log.warning("查案子 %s 的到账观察失败（%s）—— 当作没有观察", case_id, exc)
            obs = []
        try:
            tasks = [{"task_id": t["task_id"]} for t in self.store.list_tasks(plan_id)]
        except Exception:                               # noqa: BLE001
            tasks = []
        return {
            "case_id": case_id, "tenant_id": tenant_id, "plan_id": plan_id,
            "biz_status": row.get("biz_status"),
            "payment_observations": [dict(o) for o in obs],
            # 口径逐字同 `custom_case._observe()`：数的是**观察行**，不是任务状态。
            "settled_observations": sum(
                1 for o in obs
                if str(o.get("observed_state") or "") == _projection.OBSERVED_SETTLED),
            "tasks": tasks,
        }

    def handle_pending(self, msg: InboundMessage) -> str:
        if msg.channel not in ALLOW_APPROVAL:
            return "该渠道不受理审批命令"
        blocks: list[str] = []

        live = [t for t in self._tickets.values() if not t.expired(self.ticket_ttl)]
        if live:
            # 与下面那个 `rows` 分开命名：这里是排好的**文本行**，那里是待审批
            # 任务的**记录**，重名的代价是读的人以为两处是同一份东西。
            waiting: list[str] = []
            for t in live:
                waiting.append(f"  · {t.case_id}  {t.summary}  由 {t.requested_by} 提交")
                head = _headline_of(t.verdict)
                if head:
                    # 合议出来了才多这一行。它是**建议**，不是处置 —— 放行仍要
                    # 审批人自己发 /approve，收口卡改不了这一点（红线 R4）。
                    waiting.append(f"    建议：{head}")
            blocks.append("待放行（/approve <case_id>）：\n" + "\n".join(waiting))

        # 停在「跑起来之后才出现的闸」上的案子（T135）。**扫库不扫内存**：
        # `handle_execute` 跑完待办就摘了，重启之后内存里更是什么都没有，而闸还在
        # 库里停着 —— 只认内存的话 `/pending` 会说「没有等人的闸」，那一单就永远
        # 等不到第二次决定，与改造前 Plan 静默收在 DONE 一样难查。
        gates = self._held_gates_all()
        if gates:
            blocks.append("等你再决定一次的闸（/approve 或 /reject <case_id>）：\n" +
                          "\n".join(f"  · {g['case_id']}  {g['title']}  —— {g['why']}"
                                     for g in gates))

        if self.approval_queue is not None:
            rows: list[dict] = []
            for plan in self._open_plans():
                rows += self.approval_queue.pending(plan)
            if rows:
                blocks.append("等人审批的任务：\n" + "\n".join(
                    f"  · {t['task_id']}  {t['title']}（风险 {t['effect_risk']}）"
                    for t in rows))

        return "\n\n".join(blocks) if blocks else "当前没有待办，也没有等人审批的任务"

    # -- 结果面四条命令 -----------------------------------------------------
    def _case_row(self, case_id: str) -> dict | None:
        """按案号在 **router 自己的库**里找那一行 `refund_case`；没有返回 None。

        「没有」有两种：这个案子从没在本房间跑过，或者这个库还没建过退款域的表
        （一次 `/approve` 都没放行过）。两种在这里合成同一个 None，因为它们对
        发命令的人是同一句话 —— 「先把这一单跑起来」。分开报的唯一后果是房间里
        多一句「no such table: refund_case」，而他对此无能为力。
        """
        if not case_id:
            return None
        try:
            rows = _refund_objects.query(
                self.store,
                "SELECT tenant_id, plan_id, biz_status FROM refund_case WHERE case_id=?",
                (case_id,))
        except Exception as exc:                        # noqa: BLE001
            log.warning("查案子 %s 失败（%s）—— 当作没在本房间跑过", case_id, exc)
            return None
        return rows[0] if rows else None

    def _command_extras(self, plan_id: str) -> dict:
        """房间命令落事件时挂的 trace 三件套（T129）。

        T129 之前这里只给 `plan_id`，于是 `/assign` `/resolve` `/confirm` `/complain`
        落下的 `CompensationAssigned` / `CompensationResolved` / `CaseOutcomeComputed`
        三个事件 `trace_id` 与 `task_id` 都是空的 —— 而同一个文件里的
        `_compensate_if_stuck` 三者齐全。后果是按 trace 串「这一单发生过什么」时，
        DAG 那一半串得起来，房间里这一串命令**接不上去**：它们在事件表里像是
        另一件事的记录。

        `task_id` 取**付款那一步**的，口径逐字同 `_compensate_if_stuck`：房间里这几条
        命令都是在收拾「钱没退出去」的尾巴，挂到别的任务上，「补偿是因为哪一步走不通」
        在 trace 里就断了。

        查不到一律留空串，不猜也不抛：这几条命令的价值在于**动作本身已经生效**，
        为了一个审计字段把它翻成失败是本末倒置（口径同 `_record_denied`）。
        """
        if not plan_id:
            return {"plan_id": "", "trace_id": "", "task_id": ""}
        try:
            trace_id = str((self.store.get_plan(plan_id) or {}).get("trace_id") or "")
        except Exception as exc:                        # noqa: BLE001
            log.warning("查 plan %s 的 trace_id 失败（%s）—— 事件里留空", plan_id, exc)
            trace_id = ""
        try:
            task_id = next(
                (str(t.get("task_id")) for t in (self.store.list_tasks(plan_id) or [])
                 if str(t.get("task_id", "")).endswith("-payment")), "")
        except Exception as exc:                        # noqa: BLE001
            log.warning("查 plan %s 的付款任务失败（%s）—— 事件里留空", plan_id, exc)
            task_id = ""
        return {"plan_id": plan_id, "trace_id": trace_id, "task_id": task_id}

    def handle_outcome(self, msg: InboundMessage, cmd: Command) -> str:
        """`/assign` `/resolve` `/confirm` `/complain` -> `maos/ingress/outcome_commands.py`。

        本方法只做三件事，判定一件都不重新实现（口径同 `handle_approval` 之于
        `RoomApprovalBridge`）：

        1. **渠道闸永远在最前面**，与 `/approve` 同一道 —— 派单、关单、替客户录
           确认与投诉，都是在改这一单的结论，与放行同级的权限面。
        2. **先查你是谁，再谈你想干什么**（`outcome_commands` 模块抬头的第 2 条）：
           名单检查排在查案子**之前**。反过来的话，名单外的人打错一个单号会先收到
           「这个案子没在本房间跑过」—— 那是一句免费的探测应答，等于替他确认了
           「换个案号再试就行」，而越权这件事连痕都没留下。
        3. 查 `tenant_id`：命令里只有案号/工单号，而退款域每一张表的主键都带租户。
           在 `self.store` 上查，查得到就说明这一单真的在本房间跑过 —— 这同时也是
           「命令与处置共用一个库」那件事的验证点（`handle_execute` 传 `store=`）。

        `plan_id` 顺手一起取：`record_case_outcome` 拿到它才落得下
        `CaseOutcomeComputed` 事件，缺了它四判据算得出来但在 trace 里查不到是谁算的。
        """
        if msg.channel not in ALLOW_APPROVAL:
            log.warning("外部渠道 %s 的 %s 试图发结果面命令 /%s，已拒",
                        msg.channel, msg.sender, cmd.verb)
            return "该渠道不受理结果面命令（补偿工单与客户回执只在企业内部渠道处理）"

        approvers = self._approvers()
        case_id = _outcome_cmds.case_id_of(cmd.verb, cmd.args)
        row: dict | None = None
        if case_id and _outcome_cmds.in_approver_list(msg.sender, approvers):
            row = self._case_row(case_id)
            if row is None:
                return (f"这个案子没在本房间跑过：{case_id}。"
                        f"先 /refund 起单、再 /approve 放行 —— 工单与到账观察"
                        f"都是那一跑落下来的，没跑过就无处可派、无处可关")

        res = _outcome_cmds.dispatch(
            msg.text, store=self.store,
            tenant_id=str((row or {}).get("tenant_id") or ""),
            sender=msg.sender, approvers=approvers,
            identity=_outcome_cmds.TICKET_DESK_IDENTITY,
            extras=self._command_extras(str((row or {}).get("plan_id") or "")))
        # `ignored` 不发帖：那是「这压根不是本模块的命令」，而 `_dispatch` 已经按
        # 命令词把它转进来了，走到这里说明两张词表分叉了 —— 静默是对的，但要留痕。
        if res.kind == _outcome_cmds.KIND_IGNORED:
            log.warning("/%s 进了 handle_outcome 却被 outcome_commands 判为 ignored "
                        "—— KNOWN_VERBS 与 COMMANDS 分叉了", cmd.verb)
            return ""
        return res.text + self._tell_customer_after_resolve(res, row)

    def _tell_customer_after_resolve(self, res, row: dict | None) -> str:  # noqa: ANN001
        """关单**真的生效之后**补一次给客户的告知；不该发时返回空串（T137）。

        ## 为什么客户到这一步为止一个字都没收到

        `notify.customer` 在 DAG 上依赖付款那一步。付款闸被真人驳回、任务落 FAILED
        之后，它停在 PENDING 再也不跑。于是：钱没退出去（对）、补偿工单开了（对）、
        `/assign` `/resolve` 都走完了（对）、**客户从头到尾没被告知过任何事**（错）。
        `/confirm` 因此回一句「一条通知都没发出去，客户无从确认」，那条命令在补偿链上
        无路可走。这不是 T135 引入的回归 —— 改造前那条通知是在一个*本不该完成*的任务
        （被代签的付款闸）跑完之后才发出去的，T135 只是把它暴露出来。

        ## 为什么补在这里，而不是补在 `refund.compensation_close` 里面

        关单那个 skill 经 `SkillInvoker` 调什么，受**调用方 identity** 的白名单管：
        `TICKET_DESK_IDENTITY` 只授权了 `refund.compensation_close` 与 `payment.observe`
        两个（`maos/flows/scenario_7.py` 那份逐字段相同，两处都在本轨白名单外）。
        让那个 skill 自己造一个 identity 去发通知，绕开的正是它自己 `security_boundary`
        写明不许绕的那道闸 —— 「缺授权时抛 PermissionDenied，**不降级成本地直写**」。

        而告知客户本来就是**编排层的决定**，不是关单动作的一部分：口径逐字同
        `_compensate_if_stuck` 那段 docstring ——「补偿是看过事实之后的决定，一直由
        调用方下」。这一句是房间这个调用方的那一份。

        ## 只在 `KIND_DONE` 且确实是 `/resolve` 时发

        `KIND_USAGE` 是「这一步没生效」（参数不合法、工单已关、关单 skill 报错），
        那时候发通知等于替一件没发生的事告知客户。`KIND_DENIED` 更不必说。
        """
        if (res.kind != _outcome_cmds.KIND_DONE
                or res.command != _outcome_cmds.CMD_RESOLVE):
            return ""
        case_id = str(res.case_id or "")
        tenant_id = str((row or {}).get("tenant_id") or "")
        if not case_id or not tenant_id:
            return ""
        from maos.flows.custom_case import notify_customer

        plan_id = str((row or {}).get("plan_id") or "")
        # trace 三件套走 `_command_extras`，与紧邻的 `CompensationResolved` 完全同源：
        # `task_id` 因此是**付款那一步**的 —— 这条通知与那几条命令一样，是在收拾
        # 「钱没退出去」的尾巴，挂到别的任务上，「这一切是因为哪一步走不通」就断了。
        extras = self._command_extras(plan_id)
        # 措辞一个字都不在这里拼：`notify.customer` 按库里的事实产出（契约 §D），
        # 补偿收口那一档会带上工单号与凭证引用。判据同源，两条路说同一句话。
        err = notify_customer(self.store, tenant_id, case_id, plan_id=plan_id,
                              trace_id=extras.get("trace_id", ""),
                              task_id=extras.get("task_id", ""))
        if err:
            # 告知没发出去不许把一次**已经生效**的关单说成失败 —— 那件事上一句就落库了。
            # 但也不许静默：房间里的人据此知道还要另行通知客户。
            return f"\n⚠️ 这一单的客户告知没发出去（{_outcome_cmds.humanize(err)}）"
        return "\n已按补偿收口的事实告知客户（工单号与凭证引用都在正文里）"

    def _open_plans(self) -> list[str]:
        """长驻运行时里还没收口的 plan。取不到就返回空 —— 不猜。"""
        lister = getattr(self.store, "list_open_plans", None)
        return list(lister()) if callable(lister) else []

    # -- 回帖 ---------------------------------------------------------------
    def _reply(self, msg: InboundMessage, text: str) -> str:
        if not text:
            return ""
        adapter = self.adapters.get(msg.channel)
        if adapter is None:
            log.warning("渠道 %s 没有 adapter，回帖丢弃", msg.channel)
            return ""
        out = OutboundMessage(
            chat_id=msg.chat_id, text=text,
            # 微信客服回信必须带 open_kfid，它只在入站那条消息里有。
            meta={"open_kfid": str((msg.raw or {}).get("open_kfid") or "")},
        )
        try:
            adapter.send(out)
        except Exception as exc:                        # noqa: BLE001
            # 回帖失败不能把处置结果也一起丢掉 —— 那件事**已经发生了**。
            # 记全，让人能从日志里把结论捞回来。
            log.error("回帖失败（%s -> %s）：%s\n原文：%s",
                      msg.channel, msg.chat_id, exc, text)
        return text


def _is_command(text: str) -> bool:
    """这条消息是不是本 router 认领的命令。

    带命令的消息**不走证据复检**：``/refund`` 自己会认领暂存并触发一次预检，
    在附件那一步再触发一次，就是同一条消息两轮五岗发言（契约 §4）。其余命令
    （``/approve`` 之类）也不该被一张图带出一轮复检 —— 那时人在放行，不在补证据。
    """
    cmd = Command.parse(text)
    return cmd is not None and cmd.verb in KNOWN_VERBS


def _merge_evidence(old, new) -> list[StoredAttachment]:
    """老证据 + 新证据，按 digest 去重、顺序稳定（老的在前）。

    **叠加不是替换**：人的动作是「起单 -> 发现证据不齐 -> 再补一张」，补的那张
    替掉前面的，等于每补一次就丢一次，而案子里少了哪张没有任何一处会报出来。
    去重按 digest 不按文件名：内容寻址下 digest 相同就是同一份证据，重拖一遍
    不该在案子里变成 ev-01 / ev-02 两条一模一样的 uri。
    """
    merged: list[StoredAttachment] = []
    seen: set[str] = set()
    for item in (*old, *new):
        if item.digest in seen:
            continue
        seen.add(item.digest)
        merged.append(item)
    return merged


def _reason_in(text: str) -> str:
    """正文里写着的诉求类型（原话）；认不出返回 ``""``。

    词表**借** `run_requests.REASONS`，不另列一份中文说法：另列一份的症状是
    「CSV 里写『坏了』能跑、群里写『坏了』不认」，而两边都不报错。

    按长度从长到短扫：「七天无理由」含「无理由」—— 这两个恰好同一个 code，
    但「买错型号」与「买错了」不是，短的先命中就认错了诉求，而这种错不报错。
    """
    rr = _load_run_requests()
    for word in sorted(set(rr.REASONS) | set(rr.REASONS.values()),
                       key=len, reverse=True):
        if word in text:
            return word
    return ""


def _accepted_extra(team: Any, extra: dict) -> dict:
    """``extra`` 里圆桌**显式收得下**的那几个参；问不出来就一个都不传。

    不传是安全的降级：`on_preflight` 的两个新参（``round_no`` / ``added_evidence``，
    跨轨契约 §3）都带默认值，不传只是这一轮不带轮次信息，复检照样发生。
    传错了才是灾难 —— 不认的关键字会当场 TypeError，落进 `_fire` 的 except，
    症状是**房间里一片安静**、五岗一句话没有，而日志里只有一行「钩子失败」。

    要穿一层壳：真房间里坐在 router 对面的是 `hiclaw/room_ingress.py::RoomTeam`，
    它的 ``on_preflight(**kw)`` 原样转发给真圆桌。只问它，问到的永远是「什么都收」，
    而收不收得下由它包着的那一层说了算 —— 那正是 TypeError 会从里面抛出来的地方。
    """
    if not extra or team is None:
        return {}
    node: Any = team
    for _ in range(3):                                  # 壳最多穿三层，防成环
        names = _keyword_params(getattr(node, "on_preflight", None))
        if names is not None:
            return {k: v for k, v in extra.items() if k in names}
        node = getattr(node, "_team", None)
        if node is None:
            break
    return {}


def _takes_kw(fn: Any, name: str) -> bool:
    """``fn`` 收不收得下关键字 ``name``；只有 ``**kw`` 的转发壳算收得下。

    与 `_accepted_extra` 同一条理由（不认的关键字会当场 TypeError，落进调用方的
    except，症状是房间里一片安静），但**不能**复用 `_keyword_params`：
    `_default_runner(payload, **kw)` 在它那里返回的是 ``{"payload"}``——非空，
    于是不算「问不出来」，而它恰恰是什么都收得下的那种壳。这里按 ``**kw`` 判。

    探不到就不传是安全的降级：`hold_awaits` 带默认值，不传只是这一跑照旧代签，
    与 T135 之前逐字节相同 —— 注入替身的那几条测试因此一条都不受影响。
    """
    if fn is None:
        return False
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    if any(prm.kind is prm.VAR_KEYWORD for prm in params.values()):
        return True
    return name in params


def _is_held(exit_row: dict) -> bool:
    """这一条 `human_exits` 是「扣住了、没人做决定」，还是「有人做过决定」。

    按前缀判而不是逐个枚举：`held_for_drift`（T116）与 `held_for_human`（T135）
    是同一类事 —— 机器**没有**代做那个决定。日后再多一类扣法，漏加枚举的症状是
    卡片上把它说成「已放行」，那是替人宣布了一件他没做过的事。
    """
    return str(exit_row.get("decision") or "").startswith("held_")


def _why_blocked(store, plan_id: str, task_id: str) -> str:      # noqa: ANN001
    """这个任务**这一次**因为什么停下来 —— 直接借 `custom_case._blocked_reason()`。

    它要的是一个有 `.store` 的东西，而这里只有 store 本身；包一个最小的壳而不是
    另写一份取 detail 的逻辑 —— 那就是第二份判据了（同 `await_kind` 那段的理由）。
    """
    from maos.flows.custom_case import _blocked_reason

    return _blocked_reason(SimpleNamespace(store=store), plan_id, task_id)


def _held_gates(result: dict) -> list[dict]:
    """这一跑里**没有代签**的那几道闸。没有就是空表。"""
    return [e for e in (result.get("human_exits") or [])
            if str(e.get("decision") or "") == "held_for_human"]


def _gate_prompt(held: list[dict], case_id: str, result: dict) -> str:
    """停在人工闸上时接在卡片末尾的那一段：要决定什么、怎么决定。

    ## 措辞分两档，不能只写「网关回执 X」

    `AWAIT_HUMAN_DECISION` 的闸不止网关失败一种 —— 控制面第三出口还有
    `plan_defect`，另有 replan 上限那条分支。撞上别的原因时「网关回执 X」那句话
    就是编的，而人会照着这句编出来的理由做决定。所以：有 `failed` 观察才说网关
    那半句，没有就只念控制面自己给的那一行理由。

    钱到底退没退出去仍然只认观察（铁律 8）：这里念的 `gateway_code` 取自
    `payment_observation` 的最后一行，一个字都不是这里判出来的。
    """
    obs = result.get("payment_observations") or []
    last = obs[-1] if obs and isinstance(obs[-1], dict) else {}
    failed = (not result.get("settled_observations")
              and str(last.get("observed_state") or "") == "failed")
    lines = []
    for gate in held:
        why = (f"网关回执 {last.get('gateway_code') or '未知码'}，钱没退出去"
               if failed else str(gate.get("why") or "机器已经没有别的招了"))
        lines.append(f"\n⚠️ 「{gate.get('title')}」这一步停在人工闸上：{why}")
    lines.append(
        "   这一步要你**再决定一次** —— 群里刚才那次放行签的是「这一单要不要办」，"
        "签不到这里：\n"
        f"     /reject  {case_id} <理由>   不签这一步（随后会开人工补偿工单）\n"
        f"     /approve {case_id} <理由>   仍然放行（少见；只有你确认钱已通过"
        "别的渠道到账才用）")
    return "\n".join(lines)


def _keyword_params(fn: Any) -> set[str] | None:
    """``fn`` 显式列出的关键字参名；只有 ``**kw``（或问不出签名）时返回 ``None``。

    ``None`` 与空集合是两件事：空集合 = 问清楚了，它一个新参都不收；
    ``None`` = 没问出来，得再往里问一层。混成一个值就会把转发壳当成
    「什么都不收」，T99 并进来之后真房间永远拿不到轮次信息。
    """
    if fn is None:
        return None
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return None
    names = {n for n, prm in params.items()
             if prm.kind in (prm.POSITIONAL_OR_KEYWORD, prm.KEYWORD_ONLY)}
    if not names and any(prm.kind is prm.VAR_KEYWORD for prm in params.values()):
        return None
    return names


def render_roster(roster: list[dict]) -> str:
    """把 `TeamObserver.roster()` 排成群里能一眼读完的岗位表。

    模块级函数而不是方法：`/team` 与闲聊喂给模型的【事实】必须是**同一份**名单。
    没接通独立账号的岗位说明白是「代言」—— 房间里五个名牌全挂在 ``maos-bot`` 头上
    时，人有权知道那不是五个账号在说话（红线 R5：只报 mxid，绝不报 token）。
    """
    lines: list[str] = []
    for seat in roster:
        who = seat.get("user_id") if seat.get("own_identity") else ""
        lines.append(f"{seat.get('title')}（{seat.get('agent_id')}）· "
                     f"{who or '由 maos-bot 代言'}")
        duty = seat.get("duty")
        if duty:
            lines.append(f"  职责：{duty}")
        for skill in seat.get("skills") or []:
            lines.append(f"  · {skill.get('name')}@{skill.get('version')} "
                         f"— {skill.get('purpose')}")
    return "\n".join(lines)


def _titles() -> dict[str, str]:
    """五岗显示名。**惰性 import 且只此一份**（`maos/roundtable/team.py::TITLES`）。

    在这里另抄一份的症状是：圆桌改了岗位名，房间里 @ 新名字点不动，而 `/team`
    打出来的又是新名字 —— 两边都不报错。惰性是因为 `maos/ingress/` 在没接圆桌的
    进程里也要能起，不该在模块级把圆桌那一串依赖拉起来。
    """
    from maos.roundtable.team import TITLES

    return TITLES


def _mxid_localpart(agent_id: str) -> str:
    """岗位账号的 localpart：``refund-intake`` -> ``maos-intake``。

    与 `deploy/synapse/add_agents.sh` 建号时写死的那五个账号名同一份口径。那边是
    shell、没法 import 这里，所以两边一起改（同 `room_voices.env_keys_of` 的取舍）。
    推导而不是再抄一份清单：`TITLES` 增减一岗，这里自动跟着走。
    """
    return "maos-" + agent_id.split("-", 1)[-1]


def parse_mention(text: str) -> tuple[str, str] | None:
    """一条非命令消息是不是在 @ 某个岗位。命中给 ``(agent_id, 去掉称呼的问题)``。

    认三种形态，**都只在句首**：

      · ``财务执行岗: 你是干什么的``       Element 的 @提及 pill 落到纯文本里的样子
      · ``@财务执行岗 你是干什么的``        手打的 @
      · ``@maos-finance:maos.local 在吗``  手打的 mxid（能认就认，认不了走闲聊）

    ⚠️ 第一种是**主路**。房间回调 ``on_message(sender, body)`` 只给纯文本 body、
    拿不到 ``formatted_body``（`hiclaw/matrix_bus.py`），Element 里那个 pill 到这一层
    就只剩一串显示名 —— 按 mxid 匹配的那条路，真房间里根本走不到。

    只在句首匹配也是刻意的：「我问一下财务执行岗的意见」是在跟**人**说话，把它判成
    点名，房间里就会冒出一个没人叫过的岗位来答话，而发话的人不知道自己招了谁。
    """
    raw = (text or "").strip()
    if not raw:
        return None
    # 手打的 @ 允许跟一个空格（``@ 财务执行岗``）；pill 那种没有 @，原样进。
    body = raw[1:].lstrip() if raw.startswith("@") else raw
    for agent_id, title in _titles().items():
        if body.startswith(title):
            rest = body[len(title):]
            if rest and rest[0] not in MENTION_SEPS:
                # 「财务执行岗位调整」不是点名 —— 显示名只是它的前缀。
                continue
            return agent_id, rest.lstrip(MENTION_SEPS).strip()
        local = _mxid_localpart(agent_id)
        if body.startswith(local) and body[len(local):len(local) + 1] == ":":
            # 只比 localpart，**不比 homeserver**：这台机器上是 maos.local，换一套
            # 部署就是别的域名。在这里猜一个域名去比，猜错的症状是点名静默失效。
            parts = body[len(local) + 1:].split(None, 1)
            return agent_id, (parts[1].strip() if len(parts) > 1 else "")
    return None


def _decide_fn():
    """取合议引擎的 `decide()`（跨轨契约 §2）。**取不到返回 None**，不是错误。

    合议引擎是另一轨的件，本层只消费；它没并进来时，待办就是不带建议的待办。
    在这里抛，会让一条本来跑得好好的 `/refund` 在回帖之后炸出一行 WARNING。
    """
    try:
        from maos import roundtable
    except Exception as exc:                            # noqa: BLE001
        log.debug("取不到圆桌包（%s: %s），待办不带建议", type(exc).__name__, exc)
        return None
    fn = getattr(roundtable, "decide", None)
    return fn if callable(fn) else None


def _headline_of(verdict: Any) -> str:
    """收口卡的那一句话。**没有就返回空串**。

    打一行「建议：None」比不打更糟（群里会以为程序出错了），而在这里自己编一句
    就成了第二套裁定口径 —— 收口卡里每一个字都该来自 `decide()`（红线 R1）。
    """
    if verdict is None:
        return ""
    return str(getattr(verdict, "headline", "") or "").strip()


def _sheet_rows(parsed, payloads: dict[int, dict], verdicts: dict[int, dict],
                errors: dict[int, str]) -> list[dict]:
    """一张申请表给圆桌的那份行清单（契约 §1.4 的八个键）。

    **合法行与坏行一起给**：圆桌要说的是「这张表整体什么情况」，而「有两行订单
    根本不存在」正是最该被说出来的那半边。坏行的 ``payload`` / ``checked`` / ``error``
    三者都是 None —— 它压根没走到预检。
    """
    return [{
        "line": row.line,
        "order_id": row.order_id,
        "reason_raw": row.reason_raw,
        "payload": payloads.get(row.line),
        "checked": verdicts.get(row.line),
        "error": errors.get(row.line),
        "problems": list(row.problems),
        "warnings": list(row.warnings),
    } for row in parsed.rows]


def _default_runner(payload: dict, **kw) -> dict:
    """缺省处置器。单拎出来是为了让测试能塞一个不真跑的替身。"""
    from maos.flows.custom_case import run_payload
    return run_payload(payload, **kw)


def _current_approvers() -> frozenset[str]:
    """审批人名单。走 `hiclaw.matrix_bus.current_approvers`，与房间共用一份配置。

    惰性 import：`hiclaw/` 是可选依赖层，而它的模块级不 import matrix-nio，
    所以这一句在没装 nio 的机器上也安全（`test_ingress_router.py` 钉着这条）。
    """
    from hiclaw.matrix_bus import current_approvers
    return current_approvers()


def preflight(payload: dict) -> dict:
    """**只读**裁定预检：读政策、算窗口、出结论与金额。不建案、不跑 plan、不碰网关。

    每一步都调 `maos/flows/contrast.py` 里那批已经跑绿的函数 —— 与
    `custom_case.run_payload()` 规划期读政策用的是**同一批**。另算一套的症状是
    「预检说批 6800，真跑退了 5390」，而两条路各自都自洽、都不报错。

    自建一个 `:memory:` store 只为把外部快照灌进去查：政策视图要按
    ``(tenant, channel, 下单时锁定的版本)`` 过滤，这些判据全在表里，
    绕开表去读 payload 就是第二套口径。
    """
    from maos.core.store import SqliteStore
    from maos.domain.refund import fixtures, objects
    from maos.flows import contrast

    store = SqliteStore(":memory:")
    store.init_schema()
    fixtures.seed_case(store, payload)

    seed = fixtures.case_seed_of(payload)
    view = contrast.policy_view(store, tenant_id=str(seed["tenant_id"]),
                                order_id=str(seed["order_id"]),
                                order_version=int(seed["order_version"]))
    paid_at = objects.query(
        store,
        "SELECT paid_at FROM order_snapshot WHERE tenant_id=? AND order_id=? AND version=?",
        (str(seed["tenant_id"]), str(seed["order_id"]), int(seed["order_version"])),
    )[0]["paid_at"]
    requested_at = str(payload.get("requested_at") or "")
    days = contrast.elapsed_days(paid_at, requested_at)
    verdict = contrast.evaluate_eligibility(
        view["rules"], reason_code=str(seed["reason_code"]), elapsed_days=days)
    directives = contrast.policy_directives(view["rules"])

    return {
        "case_id": str(seed["case_id"]),
        "order_id": str(seed["order_id"]),
        "amount_claimed": seed.get("amount_claimed"),
        "pinned_policy_version": view["pinned"],
        "matched_rules": [r["ref"] for r in view["rules"]],
        "decision": verdict["decision"],
        "deciding_rule": verdict["rule_ref"],
        "why": verdict["why"],
        "elapsed_days": days,
        "paid_at": paid_at,
        "requested_at": requested_at,
        "approver_role": directives["approver_role"],
    }


def describe_config(adapters: dict[str, ChannelAdapter]) -> str:
    """启动时打一行「哪些渠道配好了」。**只报配没配，绝不打值**（铁律 6）。"""
    return json.dumps(
        {name: ("configured" if a.configured else "not-configured")
         for name, a in adapters.items()}, ensure_ascii=False)
