"""清算方 ToolPort —— 撤销请求的发出与决议的问询，以及「观察与推断分离」的落点。

## 这一层存在的理由（铁律 8）

一笔跨行支付撤没撤、钱回没回，**权威状态永远在清算方那边**，不在 MAOS 里。
MAOS 能做的只有两件事：发一份 camt.056（`clearing.cancel`）、问一次决议
（`clearing.resolution`）。**任何把外部状态直接写死为终态的代码都是 bug** ——
包括「报文发出去没报错所以撤销成功了」这种看起来无害的推断。

所以本模块有一条硬规矩：``send_cancellation()`` **永远不返回终态**。

    真实时序：camt.056 发出 -> 受理（还没有决议）
              camt.029 问询 -> pending … -> 有结论
              pacs.004     -> **这才是钱回来了**

## 第三种、也是最要命的一种回执：肯定但不算数

比「还没结果」更要紧的是「**清算方说撤销成功了，但钱还没回来**」。

`camt.029` 的结论码 `CNCL`（CancelledAsPerRequest）官方定义是
「Used when a requested cancellation is successful.」—— 一句不折不扣的肯定答复。
但它肯定的是**那条撤销指令**，不是资金。资金实际退回走的是 `pacs.004`
（PaymentReturn），带自己的退回原因码和退回金额。

于是本模块的回执有两个**正交**的布尔，不许压成一个：

    request_resolved  撤销请求有结论了吗（CNCL / RJCR 之后为 True）
    funds_settled     钱回来了吗（**只有 pacs.004 之后**为 True）

把这两个压成一个 `success`，`CNCL` 就会被当成业务成功 —— 而那正是本域
「Agent 都回复完成 ≠ 业务成功」的具体形状。判据不要自己写，
用 ``investigation_codes.is_funds_evidence()``。

## 幂等

`idempotency_key` 对应 camt.056 报文的 `Assgnmt/Id`（指派号）——
这不是我们发明的机制，是报文自带的。同一个 key 重复调 ``send_cancellation()``：

  · 参数一致 -> 原样返回**同一份**请求的当前观察，不发第二份 camt.056
  · 参数不一致 -> 抛 `DiscordantCancellationRequest`

重发第二份 camt.056 的后果不是「多问一次」：清算方会把它当成第二个 case，
两条对话各自往下走，而资金只有一笔。

## 调用一律走 invoke_tool()

直接调 ``MockClearingHouse.send_cancellation()`` 就没有 ToolInvoked 审计行，
出事之后查不到是谁、什么参数、跑了多久。上层请走
``invoke_tool(CLEARING_CANCEL_PORT, {...}, store=...)``。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from maos.tools.investigation_codes import (
    RESOLUTION_CONFIRMED,
    RESOLUTION_PENDING,
    RESOLUTION_REJECTED,
    SET_CANCELLATION_REASON,
    SET_CANCELLATION_REJECTION,
    SET_RESOLUTION,
    SET_RETURN_REASON,
    is_funds_evidence,
    lookup,
    message_family,
    provenance,
    resolution_of,
)
from maos.tools.port import ToolPort

log = logging.getLogger("maos.tools.investigation")

# 报文族常量。与 guard 同口径，但本模块**不 import 域** —— tools 层对 domain 零依赖，
# 换一个业务域时这两层各自独立（docs/domain-portability.md §4）。
MSG_CANCELLATION_REQUEST = "camt.056.001.08"
MSG_RESOLUTION = "camt.029.001.08"
MSG_PAYMENT_RETURN = "pacs.004.001.09"

#: mock 默认问几次才给出最终答复。>1 才能证明「一次 query 不一定够」。
DEFAULT_RESOLVE_AFTER = 3


class DiscordantCancellationRequest(ValueError):
    """同一个指派号来了一份参数不一样的撤销请求。

    不静默新建第二份：清算方会把它当成第二个 case，两条对话各自往下走，
    而资金只有一笔 —— 没有正确解，只能当场响。
    """


@dataclass(frozen=True)
class CancellationRequest:
    """一份 camt.056。字段名对齐 FIToFIPaymentCancellationRequest 的报文语义。"""

    original_msg_id: str
    """被撤销的那笔原始支付的报文号（Undrlyg/OrgnlGrpInf/OrgnlMsgId）。"""

    end_to_end_id: str
    """端到端参考号，跨行追踪同一笔的锚点。"""

    amount: str
    """金额。用字符串不用 float —— 金额永远不进浮点。"""

    currency: str

    reason_code: str
    """撤销原因码，取自 ExternalCancellationReason1Code（由定性那一步选定）。"""

    idempotency_key: str
    """指派号（Assgnmt/Id）。同一个 key 不发第二份报文。"""

    case_id: str = ""

    def fingerprint(self) -> tuple[str, str, str, str]:
        """幂等比对面：同一个指派号下，这四项变了就算「参数不一致」。

        原因码**在**比对面里（与退款域的 `RefundRequest.fingerprint` 不同）：
        camt.056 的原因码决定清算方按哪条规则处置，改了它就是另一个请求，
        不是同一件事的重放。
        """
        return (self.original_msg_id, self.end_to_end_id, self.amount, self.reason_code)


@dataclass(frozen=True)
class ResolutionReceipt:
    """清算方的一次答复 —— **一次观察的记录，不是事实本身**。

    frozen=True 是有意的：回执代表「某一时刻清算方说了什么」，改它等于篡改观察记录。
    """

    request_id: str
    """清算方给的受理号，``poll_resolution()`` 用它。"""

    idempotency_key: str

    message_type: str
    """这一次观察到的是哪种报文：camt.056（刚受理，还没答复）/ camt.029 / pacs.004。"""

    confirmation_code: str = ""
    """camt.029 的结论码（ExternalInvestigationExecutionConfirmation1Code）。"""

    rejection_code: str = ""
    """camt.029 否定决议时随附的拒绝原因码（ExternalPaymentCancellationRejection1Code）。"""

    return_reason_code: str = ""
    """pacs.004 的退回原因码（ExternalReturnReason1Code）。"""

    returned_amount: str = ""
    """pacs.004 的退回金额。字符串，金额不进浮点。"""

    resolution: str = RESOLUTION_PENDING
    """撤销**请求**的下落：confirmed / rejected / pending / partial / other。"""

    definition: str = ""
    """本次结论码的官方定义原文。直接进 findings 给人看，评委当场能对。"""

    poll_count: int = 0
    """已经问过几次。审计用：证明结论是**问出来的**，不是猜出来的。"""

    detail: dict = field(default_factory=dict)

    @property
    def funds_settled(self) -> bool:
        """钱回来了吗。**只有 pacs.004 说了才算** —— 本域全部判据的地基。

        刻意做成 property 而不是构造入参：让它恒等于对 ``message_type`` 的判定，
        杜绝「构造时手填一个 True」这条路。
        """
        return is_funds_evidence(self.message_type)

    @property
    def request_resolved(self) -> bool:
        """撤销**请求**有结论了吗。

        注意 `CNCL` 在这里是 True 而 ``funds_settled`` 仍是 False ——
        这两个属性正交，是本模块存在的全部理由。
        """
        return self.resolution in (RESOLUTION_CONFIRMED, RESOLUTION_REJECTED)

    @property
    def is_terminal(self) -> bool:
        """这条链路还要不要继续问。

        钱回来了（pacs.004）或者请求被明确拒了（RJCR）才算问到头。
        **`CNCL` 不算**：清算方确认撤销之后，资金退回的 pacs.004 还在路上，
        这时候停止轮询就等于永远拿不到资金证据。
        """
        return self.funds_settled or self.resolution == RESOLUTION_REJECTED

    @property
    def source(self) -> str:
        """码表出处。回执里带着它，评委问「这个码哪来的」当场能答。"""
        return provenance()["source_url"]

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "idempotency_key": self.idempotency_key,
            "message_type": self.message_type,
            "message_family": message_family(self.message_type),
            "confirmation_code": self.confirmation_code,
            "rejection_code": self.rejection_code,
            "return_reason_code": self.return_reason_code,
            "returned_amount": self.returned_amount,
            "resolution": self.resolution,
            "definition": self.definition,
            "poll_count": self.poll_count,
            "funds_settled": self.funds_settled,
            "request_resolved": self.request_resolved,
            "is_terminal": self.is_terminal,
            "source": self.source,
            "detail": dict(self.detail),
        }


class ClearingHousePort(Protocol):
    """清算方的两个动作。签名冻结，上层只按这两个签名写代码。"""

    def send_cancellation(self, request: CancellationRequest) -> ResolutionReceipt:
        """发一份 camt.056。**返回值永远不是终态** —— 见模块 docstring。"""
        ...

    def poll_resolution(self, request_id: str) -> ResolutionReceipt:
        """问一次决议。终态只能从这里来。"""
        ...


# ---------------------------------------------------------------------------
# 剧本 —— mock 的确定性回放
# ---------------------------------------------------------------------------
#: 三个剧本，覆盖本域必须演的三种下落。每一步是一份要观察到的报文。
#:
#: 剧本用**官方码**写，不用自造的字符串：`MockClearingHouse` 构造时会逐个
#: `lookup()`，写错一个码在装配阶段就抛，不留到演示当天。
SCRIPT_RETURNED = "returned"
"""顺利路径：未决 -> 撤销确认（CNCL）-> **退款报文**。

中间那一步 `CNCL` 是本剧本的题眼：它是一句肯定答复，而系统在那一刻**一个字都不写**。
"""

SCRIPT_CONFIRMED_ONLY = "confirmed_only"
"""失败路径：未决 -> 撤销确认（CNCL）-> 之后一直是 CNCL，**pacs.004 永远不来**。

与顺利路径**共用同一句肯定答复**，这是本域最值钱的对照：
把 CNCL 当成业务成功的系统，会在这条路径上报「成功」，而钱一分都没回来。
"""

SCRIPT_REJECTED = "rejected"
"""否定路径：未决 -> 明确拒绝（RJCR + 拒绝原因码）。请求有结论了，但结论是「不给撤」。"""

SCRIPT_SILENT = "silent"
"""问不出来：一直 pending。用来演「我问累了 ≠ 清算方说不行」。"""

ALL_SCRIPTS = (SCRIPT_RETURNED, SCRIPT_CONFIRMED_ONLY, SCRIPT_REJECTED, SCRIPT_SILENT)

#: 剧本用到的官方码。集中在这里，`_validate_script_codes()` 逐个核。
CODE_PENDING = "PDCR"          # PendingCancellationRequest
CODE_CONFIRMED = "CNCL"        # CancelledAsPerRequest
CODE_REJECTED = "RJCR"         # RejectedCancellationRequest
DEFAULT_REJECTION_CODE = "LEGL"   # 「因监管规则不能接受撤销」—— 银行域最典型的一种拒绝
DEFAULT_RETURN_REASON = "CUST"    # pacs.004 的退回原因：RequestedByCustomer


@dataclass
class _Entry:
    """账本里的一份撤销请求。mock 的「已经发生过的事」都记在这里。"""

    request_id: str
    request: CancellationRequest
    script: str
    polls: int = 0


class MockClearingHouse:
    """演示用清算方 —— 报文时序与码值都对齐 ISO 20022 官方码表。

    它是 mock，但**时序不是假的**：``send_cancellation()`` 一定给非终态，
    结论一定要 ``poll_resolution()`` 问出来，而资金证据一定单独走 pacs.004。
    这条时序是「观察与推断分离」能成立的前提，换成一步到位的 mock，整个论证就没了。

    剧本按 ``original_msg_id`` 走，让场景可以用特定报文号稳定触发特定下落，
    不依赖随机数（确定性回放是本仓已定的口径）。
    """

    def __init__(self, *, resolve_after: int = DEFAULT_RESOLVE_AFTER,
                 script: dict[str, str] | None = None,
                 rejection_code: str = DEFAULT_REJECTION_CODE,
                 return_reason_code: str = DEFAULT_RETURN_REASON) -> None:
        if resolve_after < 1:
            raise ValueError("resolve_after 至少为 1 —— 撤销不允许一步到终态")
        self.resolve_after = resolve_after
        self.script = dict(script or {})
        for msg_id, name in self.script.items():
            if name not in ALL_SCRIPTS:
                raise ValueError(
                    f"未知剧本 {name!r}（{msg_id}）；可选 {list(ALL_SCRIPTS)}")
        self.rejection_code = rejection_code
        self.return_reason_code = return_reason_code
        self._validate_script_codes()
        self._ledger: dict[str, _Entry] = {}       # idempotency_key -> 账本
        self._by_request: dict[str, str] = {}      # request_id -> idempotency_key

    def _validate_script_codes(self) -> None:
        """构造时就把要用的码全 lookup 一遍，写错的码在装配阶段抛。

        留到调用时才炸的话，症状会出现在演示中途，而且长得像「清算方返回了未知码」——
        那是排查方向完全相反的两件事。
        """
        for code in (CODE_PENDING, CODE_CONFIRMED, CODE_REJECTED):
            lookup(SET_RESOLUTION, code)
        lookup(SET_CANCELLATION_REJECTION, self.rejection_code)
        lookup(SET_RETURN_REASON, self.return_reason_code)

    def __repr__(self) -> str:
        # 不带内存地址：这个对象会进 invoke_tool 的 params_digest，
        # 带地址会让同样参数每次算出不同的 digest，审计就对不上了。
        return (f"MockClearingHouse(resolve_after={self.resolve_after}, "
                f"scripted={len(self.script)})")

    @property
    def request_count(self) -> int:
        """一共发出去几份 camt.056。幂等测试断言的就是它。"""
        return len(self._ledger)

    # ------------------------------------------------------------------ 发送
    def send_cancellation(self, request: CancellationRequest) -> ResolutionReceipt:
        key = request.idempotency_key
        if not key:
            raise ValueError("撤销请求必须带 idempotency_key（对应 camt.056 的 Assgnmt/Id）")
        lookup(SET_CANCELLATION_REASON, request.reason_code)   # 原因码必须是官方码

        existing = self._ledger.get(key)
        if existing is not None:
            if existing.request.fingerprint() != request.fingerprint():
                raise DiscordantCancellationRequest(
                    f"指派号 {key} 已用于另一份撤销请求"
                    f"（库里 {existing.request.fingerprint()}，这次 {request.fingerprint()}）；"
                    "不许发第二份 camt.056 —— 清算方会把它当成第二个 case，"
                    "两条对话各自往下走而资金只有一笔")
            # 参数一致：原样返回当前观察，不推进、不新发。
            return self._observe(existing, advance=False)

        entry = _Entry(
            request_id=f"clr_{uuid.uuid4().hex[:16]}",
            request=request,
            script=self.script.get(request.original_msg_id, SCRIPT_RETURNED),
        )
        self._ledger[key] = entry
        self._by_request[entry.request_id] = key

        # 受理。**这里绝不能返回决议** —— camt.056 发出去之后清算方还没答复，
        # 一步返回 CNCL 的 mock 会让 investigation.observe 失去存在理由。
        return ResolutionReceipt(
            request_id=entry.request_id,
            idempotency_key=key,
            message_type=MSG_CANCELLATION_REQUEST,
            resolution=RESOLUTION_PENDING,
            definition="撤销请求已发出，清算方尚未给出决议",
            poll_count=0,
            detail={"case_id": request.case_id, "script": entry.script},
        )

    # ------------------------------------------------------------------ 问询
    def poll_resolution(self, request_id: str) -> ResolutionReceipt:
        key = self._by_request.get(request_id)
        if key is None:
            raise KeyError(f"未知 request_id：{request_id!r}")
        return self._observe(self._ledger[key], advance=True)

    def _observe(self, entry: _Entry, *, advance: bool) -> ResolutionReceipt:
        """产出一次观察。``advance=True`` 才推进问询计数（只有 poll 会推进）。"""
        if advance:
            entry.polls += 1
        polls, key = entry.polls, entry.request.idempotency_key

        # 还没到该给结论的那一次 —— 一律 pending。
        if polls < self.resolve_after:
            return self._camt029(entry, CODE_PENDING, polls)

        if entry.script == SCRIPT_SILENT:
            # 永远 pending。这一档演的是「我问累了」，**不是**「清算方说不行」。
            return self._camt029(entry, CODE_PENDING, polls)

        if entry.script == SCRIPT_REJECTED:
            return self._camt029(entry, CODE_REJECTED, polls,
                                 rejection_code=self.rejection_code)

        # 到这里剧本是 RETURNED 或 CONFIRMED_ONLY —— 两者在**这一步完全一样**：
        # 清算方确认撤销（CNCL）。差别只在下一次问询有没有 pacs.004。
        if polls == self.resolve_after or entry.script == SCRIPT_CONFIRMED_ONLY:
            return self._camt029(entry, CODE_CONFIRMED, polls)

        # 只有 RETURNED 剧本才走到这里：资金实际退回，pacs.004。
        return self._pacs004(entry, polls)

    # ------------------------------------------------------------------ 造回执
    def _camt029(self, entry: _Entry, code: str, polls: int,
                 *, rejection_code: str = "") -> ResolutionReceipt:
        """造一份 camt.029。`definition` 取自官方码表，不在这里手写。"""
        entry_code = lookup(SET_RESOLUTION, code)
        detail: dict = {"case_id": entry.request.case_id, "script": entry.script}
        if rejection_code:
            rej = lookup(SET_CANCELLATION_REJECTION, rejection_code)
            detail["rejection_reason"] = rej.definition
            detail["rejection_name"] = rej.name
        return ResolutionReceipt(
            request_id=entry.request_id,
            idempotency_key=entry.request.idempotency_key,
            message_type=MSG_RESOLUTION,
            confirmation_code=code,
            rejection_code=rejection_code,
            resolution=resolution_of(code),
            definition=entry_code.definition,
            poll_count=polls,
            detail=detail,
        )

    def _pacs004(self, entry: _Entry, polls: int) -> ResolutionReceipt:
        """造一份 pacs.004 —— 本模块唯一会让 ``funds_settled`` 为 True 的地方。"""
        reason = lookup(SET_RETURN_REASON, self.return_reason_code)
        return ResolutionReceipt(
            request_id=entry.request_id,
            idempotency_key=entry.request.idempotency_key,
            message_type=MSG_PAYMENT_RETURN,
            return_reason_code=self.return_reason_code,
            returned_amount=entry.request.amount,
            resolution=RESOLUTION_CONFIRMED,
            definition=reason.definition,
            poll_count=polls,
            detail={"case_id": entry.request.case_id, "script": entry.script,
                    "return_reason_name": reason.name},
        )


class SwiftNetworkAdapter:
    """真 SWIFT / 清算网络适配层 —— **本轮只留壳**。

    签名与 ``MockClearingHouse`` 完全一致，切换时上层一行不用改。

    每个方法都显式 ``raise NotImplementedError``，**不静默返回假数据** ——
    一个「看起来返回了点什么」的桩会让上层以为接通了，那比没实现危险得多
    （口径同 `maos/tools/gateway.py::AlipaySandboxAdapter`）。
    """

    def __init__(self, *, bic: str = "", endpoint: str = "",
                 credential: str = "") -> None:
        self.bic = bic
        self.endpoint = endpoint
        # 凭据走私有属性 + 自定义 __repr__，两道防线 —— 避免密钥进 repr /
        # pytest 对象打印 / traceback（铁律 6）。
        self._credential = credential

    def __repr__(self) -> str:
        return f"SwiftNetworkAdapter(bic={self.bic!r}, endpoint={self.endpoint!r})"

    def send_cancellation(self, request: CancellationRequest) -> ResolutionReceipt:
        raise NotImplementedError(
            "SwiftNetworkAdapter.send_cancellation 尚未接通真实清算网络："
            "演示使用 MockClearingHouse（码值与报文时序对齐 ISO 20022 官方码表）")

    def poll_resolution(self, request_id: str) -> ResolutionReceipt:
        raise NotImplementedError(
            "SwiftNetworkAdapter.poll_resolution 尚未接通真实清算网络："
            "演示使用 MockClearingHouse（码值与报文时序对齐 ISO 20022 官方码表）")


# ---------------------------------------------------------------------------
# 两个 ToolPort 声明（九要素）—— 调用一律走 invoke_tool()，直接调没有审计行
# ---------------------------------------------------------------------------
def clearing_cancel(*, clearing: Any, original_msg_id: str, end_to_end_id: str,
                    amount: str, currency: str, reason_code: str,
                    idempotency_key: str, case_id: str = "") -> dict:
    """ToolPort 入口：发一份 camt.056，返回受理回执 dict。

    入参摊平成基本类型而不是收一个 CancellationRequest 对象：``invoke_tool`` 会把
    params 做 sha256 进审计行，摊平之后 digest 才对得上「同样的参数」这个直觉。
    """
    req = CancellationRequest(
        original_msg_id=original_msg_id, end_to_end_id=end_to_end_id,
        amount=amount, currency=currency, reason_code=reason_code,
        idempotency_key=idempotency_key, case_id=case_id)
    return clearing.send_cancellation(req).to_dict()


def clearing_resolution(*, clearing: Any, request_id: str) -> dict:
    """ToolPort 入口：问一次决议。终态只能从这里来。"""
    return clearing.poll_resolution(request_id).to_dict()


CLEARING_CANCEL_PORT = ToolPort(
    name="clearing.cancel",
    purpose=("向清算方发出 camt.056 撤销请求；返回受理回执，**不返回决议**"
             "（决议须经 clearing.resolution 问询，资金证据更须等 pacs.004）"),
    entry=clearing_cancel,
    params_schema={"clearing": "ClearingHousePort", "original_msg_id": "str",
                   "end_to_end_id": "str", "amount": "str（金额不进浮点）",
                   "currency": "str", "reason_code": "str（ExternalCancellationReason1Code）",
                   "idempotency_key": "str（camt.056 的 Assgnmt/Id）",
                   "case_id": "str（可选）"},
    returns_schema={"request_id": "str", "message_type": "camt.056.001.08（受理，非决议）",
                    "resolution": "pending（发出去的那一刻不可能有结论）",
                    "funds_settled": "bool（恒 False）",
                    "request_resolved": "bool（恒 False）", "is_terminal": "bool（恒 False）",
                    "source": "str（码表出处）"},
    failure_modes=[
        "ValueError: 缺 idempotency_key（对应 camt.056 的 Assgnmt/Id）",
        "UnknownCodeError: reason_code 不在 ExternalCancellationReason1Code 里 ——"
        "**不许兜底**，编造的原因码发出去就是一份不合规报文",
        "DiscordantCancellationRequest: 同指派号参数不一致；不许发第二份 camt.056，"
        "清算方会把它当成第二个 case，而资金只有一笔",
        "NotImplementedError: 用了 SwiftNetworkAdapter 而清算网络未接通",
    ],
    security_boundary=(
        "MAOS 不持有撤销与资金的权威事实（铁律 8），本工具只产生**观察记录**："
        "send 永不返回决议，决议一律经 clearing.resolution 取得；"
        "同一 idempotency_key 不发第二份 camt.056；"
        "原因码取自 iso20022_codes.json 的已核对官方表，未知码抛 UnknownCodeError 不兜底"
    ),
    rate_limit="",
    owner="task-t38",
)

CLEARING_RESOLUTION_PORT = ToolPort(
    name="clearing.resolution",
    purpose=("问询清算方对撤销请求的决议（camt.029）与资金退回（pacs.004）——"
             "本域终态的唯一合法来源"),
    entry=clearing_resolution,
    params_schema={"clearing": "ClearingHousePort", "request_id": "str"},
    returns_schema={
        "message_type": "camt.029.001.08 | pacs.004.001.09",
        "confirmation_code": "str（camt.029 的 ExternalInvestigationExecutionConfirmation1Code）",
        "rejection_code": "str（否定决议时的 ExternalPaymentCancellationRejection1Code）",
        "return_reason_code": "str（pacs.004 的 ExternalReturnReason1Code）",
        "returned_amount": "str（只有 pacs.004 才有）",
        "resolution": "confirmed|rejected|pending|partial|other（**撤销请求**的下落）",
        "request_resolved": "bool（请求有结论了吗）",
        "funds_settled": "bool（钱回来了吗 —— **只有 pacs.004 为 True**）",
        "poll_count": "int（问过几次，证明结论是问出来的）",
        "is_terminal": "bool（funds_settled 或明确被拒；**CNCL 不算**）",
    },
    failure_modes=[
        "KeyError: 未知 request_id",
        "resolution=pending: 清算方还没给结论，继续问，**不许当成失败**",
        "confirmation_code=CNCL 而 funds_settled=False: 清算方说撤销成功了，"
        "但资金退回报文（pacs.004）还没到 —— **这一档最危险**，"
        "把它当成业务成功就是把外部状态写死为终态（铁律 8）",
        "confirmation_code=RJCR: 撤销请求被拒，随附 rejection_code，终态，转人工或补偿",
        "NotImplementedError: 用了 SwiftNetworkAdapter 而清算网络未接通",
    ],
    security_boundary=(
        "只读观察，不改变清算方任何状态；"
        "问询次数落在回执的 poll_count 上，审计可证明结论来自观察而非本地推断；"
        "funds_settled 是对 message_type 的判定（只有 pacs.004 为真），"
        "**不是构造入参** —— 杜绝在调用处手填一个 True"
    ),
    rate_limit="",
    owner="task-t38",
)
