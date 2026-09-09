"""RTV（采购退货退款）域碰得到外部世界的五个口子 —— 供应商门户、承运商、AP 系统。

## 这一层存在的理由（铁律 8）

一笔退货到底成没成，MAOS 说了不算。这件事在本域比在应付账款域更硬一层：
**权威源有两个，而且是两件独立的事**。

    「供应商认不认这笔退货」   —— 供应商门户说了算 -> 权威终态 ``credited``
    「那笔钱到没到账」         —— AP / 银行说了算   -> 权威终态 ``settled``

两个都不是 MAOS 能写的。任何把 ``credited`` 或 ``settled`` 直接写死的代码都是 bug。
本模块的五个 port 是全系统取得这两个判据的**唯一途径**。

所以本模块有一条硬规矩，与应付账款域 ``maos/tools/ap.py`` 逐字同构：

    写操作（``rma_submit`` / ``ship``）**永远不返回终态**。

    真实时序：rma_submit()    -> submitted（申请递出去了，供应商还没受理）
              credit_query()  -> acknowledged …（收到货了，还没开票）
                              -> issued（这才是 credited 的判据）
              ship()          -> created（运单建了，货还没走）
              track()         -> in_transit … -> delivered

一步返回 ``issued`` 的 mock 会把整条论证抽空 —— 那样 ``rtv.observe`` 就没有存在
理由，评委问「你怎么知道供应商认了这笔退货」只能答「因为我的 mock 这么写的」。

## 🔴 ``acknowledged`` 与 ``issued`` 是两个值，绝不许合并

这是本模块**最要紧的一条**，也是本域相对应付账款域真正的增量：

    acknowledged   供应商**收到退货了**            （非终态）
    issued         供应商**开出了贷项通知单**      （终态，credited 的唯一判据）

两者的回执字段齐全、形状一样，差的是**一次会计确认**。合并成一个值，业务域的权威
闸就没东西可拦了：货刚签收就被算成「供应商认了这笔钱」，而供应商完全可能验货之后
拒赔。口径同应付账款域拒收 ``accepted`` 那条注释。

``acknowledged`` 的回执**不带 ``credit_note_id``** —— 判据不只在状态字符串上，
也在「有没有那张单」上。两处同时假才假得出一份能骗过闸的回执。

## 第三档回执：unknown

比「还在处理」更要紧的是「**供应商门户自己也说不清**」。门户超时、单据在对方系统里
串了状态、对账文件未回，都会落到这一档：**这笔退货可能已经被受理了，只是问不出来**。

``unknown`` 时重提一次 RMA 就可能开出**第二张退货授权**，这是本模块防的第一号事故。
所以 ``rma_submit`` 带幂等键，``unknown`` 只能继续问或转人工。

## 🔴 ``ap.adjust_query`` 只读，本模块**不提供**任何写 AP 的方法

调整凭单由 AP 侧按自己的规则建，RTV 域只观察。两处都能写会让「这笔调整是谁建的」
失去唯一答案。``MockApSystem`` 因此只有 ``query()``：它的账本在构造时给定，
代表「AP 那边本来就有/没有这么一笔」，不是我方能推动的东西。

## 全部进程内，一行真网络都不打

三个模拟器都是确定性的：同样的入参连跑两次输出逐条一致，不用随机数、不读时钟做
判定、不发一个包。演示与测试在断网、不配任何 env 的机器上必须全绿。

## 调用一律走 invoke_tool()

直接调 ``MockSupplier.credit_query()`` 就没有 ToolInvoked 审计行，出事之后查不到是
谁、什么参数、跑了多久。上层请走 ``invoke_tool(SUPPLIER_CREDIT_QUERY_PORT, {...},
store=...)``。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Protocol

from maos.tools.port import ToolPort
from maos.tools.rtv_codes import (
    ALL_AP_ADJUSTMENT_STATUSES,
    ALL_CARRIER_STATUSES,
    ALL_SUPPLIER_STATUSES,
    AP_ADJUSTMENT_TERMINAL_STATUSES,
    AP_BUILT,
    AP_NONE,
    AP_SETTLED,
    AP_STAGED,
    AP_VOIDED,
    CARRIER_CREATED,
    CARRIER_DELIVERED,
    CARRIER_EXCEPTION,
    CARRIER_IN_TRANSIT,
    CARRIER_TERMINAL_STATUSES,
    CODE_CREDIT_NOTE,
    SUPPLIER_ACKNOWLEDGED,
    SUPPLIER_DISPUTED,
    SUPPLIER_ISSUED,
    SUPPLIER_SUBMITTED,
    SUPPLIER_TERMINAL_STATUSES,
    SUPPLIER_UNKNOWN,
    require_credit_note_type,
    require_return_reason,
)

log = logging.getLogger("maos.tools.rtv")

#: artifact 里挂供应商回执的键名。**不许改成 "receipt"** —— 理由同 ap.py 的
#: ``ADVICE_FIELD``：``ReviewerGate._gate_gateway``（第七道闸）按 ``content["receipt"]``
#: 的数据形状触发，然后拿里面的 ``code`` 去查支付宝码表。本域的回执要是也叫
#: ``receipt``，那道闸会对着一张它根本不认识的码表开火，退货域的每一份产物都会被判
#: 成「未知网关码」。换个域就该有自己的判据面，共用字段名等于共用判据。
SUPPLIER_ADVICE_FIELD = "supplier_advice"

#: 同上，AP 侧回执的键名。
AP_ADVICE_FIELD = "ap_adjustment_advice"

#: 供应商门户默认问几次才受理 / 才开票。``issue_after > 1`` 才能证明「一次 query
#: 不一定够」；``ack_after < issue_after`` 才能证明 acknowledged 与 issued 之间**确实
#: 隔着一段**，而那一段正是权威闸要拦的地方。
DEFAULT_ACK_AFTER = 1
DEFAULT_ISSUE_AFTER = 3

#: 承运商默认问几次才送达。
DEFAULT_DELIVER_AFTER = 2

#: AP 系统默认问几次才核销。>= 3 才走得完 staged -> built -> settled 三段。
DEFAULT_AP_SETTLE_AFTER = 3


class DuplicateRequest(RuntimeError):
    """同一个幂等键上来了一条**参数不同**的请求。

    不静默、也不当成新请求收下：前者会让调用方拿到一份与自己递进来的申请对不上的
    回执，后者会开出第二张退货授权 / 第二张运单。与应付账款域
    ``ap.DuplicateInstruction`` 同一个 fail-closed 口径。
    """


def _money(value: str, what: str) -> str:
    """金额一律字符串，**永远不进浮点**（同 ``ap.PaymentInstruction``）。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{what} 必须是非空字符串金额 —— 金额不进浮点")
    return value


# ===========================================================================
# 供应商门户：退货授权（RMA）与贷项通知单
# ===========================================================================
@dataclass(frozen=True)
class RmaRequest:
    """一条退货授权申请。

    ``reason_code`` 在构造时就核 —— 不等到供应商那边才发现。表外的码在
    ``require_return_reason`` 里抛，上层据此出「理由不可核对」的拒收结论。
    """

    supplier_id: str
    case_id: str
    """RTV 案子号（契约 C-R1 的 ``rtv_case.case_id``）。"""

    po_id: str
    gr_id: str
    """源采购订单与源收货单。指不回去的退货在对账时无法与贷项通知单勾稽。"""

    amount_claimed: str
    """退货方自称的应退金额。真正认的金额以供应商贷项通知单为准（外部权威）。"""

    reason_code: str
    """退货理由，必在 ``rtv_codes.RETURN_REASONS`` 里。"""

    currency: str = "CNY"

    line_count: int = 1
    """退货行数。进指纹 —— 行数变了就是另一笔退货。"""

    idempotency_key: str = ""
    """幂等键。由 (tenant, case) 唯一确定 —— 一个退货案子只允许有一张退货授权。"""

    note: str = ""
    """给供应商的附言。**不进指纹**：附言改了不影响退货结果。"""

    def __post_init__(self) -> None:
        require_return_reason(self.reason_code)
        _money(self.amount_claimed, "amount_claimed")
        if self.line_count < 1:
            raise ValueError("退货授权至少要有一行 —— 零行的退货申请没有对象")

    def fingerprint(self) -> tuple[str, str, str, str, int]:
        """幂等比对面：同一个键下这五项变了就算「重复申请不一致」。

        不比 ``note``，理由同 ``ap.PaymentInstruction.fingerprint`` 不比
        ``remittance_info``。
        """
        return (self.case_id, self.amount_claimed, self.currency,
                self.reason_code, self.line_count)


@dataclass(frozen=True)
class SupplierAdvice:
    """供应商门户回执 —— **一次观察的记录，不是事实本身**。

    ``frozen=True`` 是有意的：回执代表「某一时刻供应商门户说了什么」，改它等于篡改
    观察记录。状态推进用 ``replace()`` 产生新回执，旧的留在审计里。
    """

    rma_id: str
    """供应商侧的退货授权号，``credit_query()`` 用它。"""

    idempotency_key: str
    case_id: str

    status: str
    """submitted / acknowledged / issued / disputed / unknown。见模块 docstring。"""

    amount_claimed: str
    currency: str

    message: str = ""
    """供应商给的人话说明。"""

    poll_count: int = 0
    """已经 query 过几次。审计用：证明终态是**问出来的**，不是猜出来的。"""

    credit_note_id: str = ""
    """贷项通知单号，**只有 issued 才有** —— 供应商侧生成，我方不许自己造一个。
    ``acknowledged`` 时这里恒为空串，那是「收到货」与「认了钱」之间的第二道分界。"""

    document_type: str = ""
    """UNCL1001 单据类型码，只有 issued 才有，恒为 ``381``（见 ``rtv_codes``）。"""

    amount_credited: str = ""
    """供应商认的金额，只有 issued 才有。与 ``amount_claimed`` 刻意分成两处 ——
    两者不一致正是三方对账要拦的事，合并成一处就拦不到了。"""

    issued_at: str = ""
    """供应商开出贷项通知单的时间，只有 issued 才有。**不是**我方观察到的时间。"""

    detail: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in ALL_SUPPLIER_STATUSES:
            raise ValueError(f"未知的供应商回执状态：{self.status}")
        # 判据的第二条腿：非 issued 的回执**不许**带贷项通知单号。
        # 只判状态字符串的话，一份「acknowledged 但带着单号」的回执照样能进对账。
        if self.status != SUPPLIER_ISSUED and self.credit_note_id:
            raise ValueError(
                f"状态 {self.status} 的回执不许带 credit_note_id —— "
                f"贷项通知单是 issued 才有的东西，带着它的非终态回执是伪造的判据")
        if self.status == SUPPLIER_ISSUED:
            if not self.credit_note_id:
                raise ValueError("issued 回执必须带 credit_note_id")
            require_credit_note_type(self.document_type)

    @property
    def is_terminal(self) -> bool:
        return self.status in SUPPLIER_TERMINAL_STATUSES

    @property
    def is_credit_evidence(self) -> bool:
        """这份回执够不够格当 ``credited`` 的判据。

        **只有 issued 算数**。写成一个 property 而不是让调用方自己比字符串：
        比字符串的地方多一处，就多一处可以悄悄把 ``acknowledged`` 也算进去的机会。
        """
        return self.status == SUPPLIER_ISSUED and bool(self.credit_note_id)

    def as_dict(self) -> dict:
        """折成 dict 供 artifact / 落库使用。

        额外带上 ``is_terminal`` 与 ``is_credit_evidence``：读产物的人（和评委）不必
        自己判这个状态算不算终态、够不够当判据。
        """
        return {
            "rma_id": self.rma_id,
            "idempotency_key": self.idempotency_key,
            "case_id": self.case_id,
            "status": self.status,
            "is_terminal": self.is_terminal,
            "is_credit_evidence": self.is_credit_evidence,
            "amount_claimed": self.amount_claimed,
            "currency": self.currency,
            "message": self.message,
            "poll_count": self.poll_count,
            "credit_note_id": self.credit_note_id,
            "document_type": self.document_type,
            "amount_credited": self.amount_credited,
            "issued_at": self.issued_at,
            "detail": dict(self.detail),
        }


class SupplierPort(Protocol):
    """供应商门户适配器要实现的两个动作。换真门户时实现这个协议即可，上层一字不改。"""

    def rma_submit(self, request: RmaRequest) -> SupplierAdvice: ...

    def credit_query(self, rma_id: str) -> SupplierAdvice: ...


#: 各供应商状态回执上给人看的那句话。集中在这里而不是散在分支里 —— 散着写，改一句
#: 话要翻三个分支，而漏改的那句不会有任何症状（口径同 ``ap._MESSAGE_OF``）。
_SUPPLIER_MESSAGE_OF: dict[str, str] = {
    SUPPLIER_SUBMITTED: "退货授权申请已递出，供应商尚未受理",
    SUPPLIER_ACKNOWLEDGED: "供应商已确认收到退货；**尚未开出贷项通知单**，"
                           "这一档不构成 credited 的判据",
    SUPPLIER_ISSUED: "供应商已开出贷项通知单",
    SUPPLIER_DISPUTED: "供应商明确不认这笔退货",
    SUPPLIER_UNKNOWN: "供应商门户未能给出该笔退货授权的下落（门户超时 / 对账文件未回）；"
                      "该笔**可能已被受理**，不许据此重提 RMA",
}


class MockSupplier:
    """演示用供应商门户。**确定性**：同样的入参连跑两次输出逐条一致，不用随机数。

    ``ack_after`` / ``issue_after`` 是本类存在的全部意义：它保证 **acknowledged 与
    issued 之间隔着若干次观察**。设 ``issue_after`` 为一个大于轮询上限的数（失败路径
    就是这么用的），就能造出「货签收了、供应商却迟迟不开票」—— 那是采购退货里最常见、
    也最容易被系统假装成已退款的一档。

    ``script`` 按 ``case_id`` 注入非正常终态：值取 ``rtv_codes`` 的供应商状态之一，
    ``credit_query()`` 到点之后回它而不是 ``issued``。
    ``credit_amounts`` / ``credit_currencies`` 同样按 ``case_id`` 注入供应商**认的**
    金额与币种，缺省与申请一致 —— 注入不一致的值就能演出三方对账对不上的那两条规则。

    本类**没有**任何写贷项通知单的方法：贷项通知单是供应商开的，我方开一张等于自己
    给自己开发票。
    """

    def __init__(self, *, ack_after: int = DEFAULT_ACK_AFTER,
                 issue_after: int = DEFAULT_ISSUE_AFTER,
                 script: dict[str, str] | None = None,
                 credit_amounts: dict[str, str] | None = None,
                 credit_currencies: dict[str, str] | None = None) -> None:
        if ack_after < 1:
            raise ValueError("ack_after 至少为 1 —— 一提就受理的门户演不出观察")
        if issue_after <= ack_after:
            raise ValueError(
                "issue_after 必须大于 ack_after —— 两者相等就等于把 acknowledged "
                "和 issued 合并成了一个值，权威闸从此没东西可拦")
        self.ack_after = int(ack_after)
        self.issue_after = int(issue_after)
        self.script = dict(script or {})
        self.credit_amounts = dict(credit_amounts or {})
        self.credit_currencies = dict(credit_currencies or {})
        #: rma_id -> 当前回执
        self._ledger: dict[str, SupplierAdvice] = {}
        #: idempotency_key -> (rma_id, fingerprint)
        self._by_key: dict[str, tuple[str, tuple]] = {}
        #: rma_id -> 那条申请，query 时要用它挑脚本
        self._requests: dict[str, RmaRequest] = {}

    # ----------------------------------------------------------- rma_submit
    def rma_submit(self, request: RmaRequest) -> SupplierAdvice:
        """递一条退货授权申请。**返回值永远不是终态**。

        幂等：同一个 ``idempotency_key`` 再来一次 —— 参数一致就原样返回**同一张单**
        的当前回执（不新建第二张）；参数不一致抛 ``DuplicateRequest``。
        """
        key = request.idempotency_key
        if not key:
            raise ValueError("退货授权申请必须带幂等键 —— 没有它就挡不住第二张 RMA")

        known = self._by_key.get(key)
        if known is not None:
            rma_id, fingerprint = known
            if fingerprint != request.fingerprint():
                raise DuplicateRequest(
                    f"幂等键 {key!r} 上已经有一条参数不同的退货授权申请："
                    f"库里 {fingerprint}、这次 {request.fingerprint()}。"
                    f"这不是重提，是两笔不同的退货撞了同一个键")
            return self._ledger[rma_id]

        rma_id = f"rma-{uuid.uuid4().hex[:12]}"
        advice = SupplierAdvice(
            rma_id=rma_id,
            idempotency_key=key,
            case_id=request.case_id,
            status=SUPPLIER_SUBMITTED,        # ← 递出去了，**不是**终态
            amount_claimed=request.amount_claimed,
            currency=request.currency,
            message=_SUPPLIER_MESSAGE_OF[SUPPLIER_SUBMITTED],
            detail={"supplier_id": request.supplier_id,
                    "po_id": request.po_id,
                    "gr_id": request.gr_id,
                    "reason_code": request.reason_code,
                    "line_count": request.line_count},
        )
        self._ledger[rma_id] = advice
        self._by_key[key] = (rma_id, request.fingerprint())
        self._requests[rma_id] = request
        return advice

    # --------------------------------------------------------- credit_query
    def credit_query(self, rma_id: str) -> SupplierAdvice:
        """问一次供应商门户。每问一次 ``poll_count`` 加一。

        分段推进（这三段刻意分开，合并任意两段都会毁掉一条判据）：

            polls <  ack_after     -> submitted     （门户还没受理）
            polls <  issue_after   -> acknowledged  （**收到货了，没开票**）
            polls >= issue_after   -> 脚本指定的终态（缺省 issued）

        **已经是终态的不再变**：终态是终态。
        """
        advice = self._ledger.get(rma_id)
        if advice is None:
            raise LookupError(f"没有这条退货授权：{rma_id}")
        if advice.is_terminal:
            return advice

        polls = advice.poll_count + 1
        if polls < self.ack_after:
            advice = replace(advice, status=SUPPLIER_SUBMITTED, poll_count=polls,
                             message=f"供应商尚未受理（第 {polls} 次查询）")
        elif polls < self.issue_after:
            # 🔴 这一档**不带** credit_note_id：收到货 ≠ 认了这笔钱。
            advice = replace(advice, status=SUPPLIER_ACKNOWLEDGED, poll_count=polls,
                             message=_SUPPLIER_MESSAGE_OF[SUPPLIER_ACKNOWLEDGED])
        else:
            advice = self._settle(advice, polls)
        self._ledger[rma_id] = advice
        return advice

    def _settle(self, advice: SupplierAdvice, polls: int) -> SupplierAdvice:
        """到点之后按脚本给出终态回执。issued 才挂贷项通知单。"""
        case_id = advice.case_id
        target = self.script.get(case_id, SUPPLIER_ISSUED)
        if target not in ALL_SUPPLIER_STATUSES:
            raise ValueError(f"脚本给了未知的供应商回执状态：{target!r}")
        if target != SUPPLIER_ISSUED:
            # disputed / unknown / 甚至被脚本按住在 acknowledged：一律不发单号。
            return replace(advice, status=target, poll_count=polls,
                           message=_SUPPLIER_MESSAGE_OF[target])
        return replace(
            advice,
            status=SUPPLIER_ISSUED,
            poll_count=polls,
            message=_SUPPLIER_MESSAGE_OF[SUPPLIER_ISSUED],
            credit_note_id=f"cn-{advice.rma_id[4:]}",
            document_type=CODE_CREDIT_NOTE,
            amount_credited=self.credit_amounts.get(case_id, advice.amount_claimed),
            currency=self.credit_currencies.get(case_id, advice.currency),
            issued_at="2026-09-02",
        )


# ===========================================================================
# 承运商：退货发运
# ===========================================================================
@dataclass(frozen=True)
class ShipmentOrder:
    """一条退货发运指令。"""

    case_id: str
    rma_id: str
    """退货授权号。**没有 RMA 不许发货** —— 发出去的货供应商可以直接拒收。"""

    carrier: str
    origin: str
    destination: str
    parcel_count: int = 1
    idempotency_key: str = ""
    """幂等键。由 (tenant, case) 唯一确定 —— 一个退货案子只允许有一张运单。"""

    def __post_init__(self) -> None:
        if not self.rma_id:
            raise ValueError("发运指令必须带 rma_id —— 没有退货授权的货会被拒收")
        if self.parcel_count < 1:
            raise ValueError("包裹件数至少为 1")

    def fingerprint(self) -> tuple[str, str, str, int]:
        return (self.case_id, self.rma_id, self.destination, self.parcel_count)


@dataclass(frozen=True)
class CarrierAdvice:
    """承运商回执 —— 一次观察的记录。``frozen=True`` 理由同 ``SupplierAdvice``。"""

    shipment_id: str
    tracking_no: str
    idempotency_key: str
    case_id: str
    carrier: str

    status: str
    """created / in_transit / delivered / exception。"""

    message: str = ""
    poll_count: int = 0
    delivered_at: str = ""
    """签收时间，只有 delivered 才有。"""

    detail: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in ALL_CARRIER_STATUSES:
            raise ValueError(f"未知的承运商回执状态：{self.status}")

    @property
    def is_terminal(self) -> bool:
        return self.status in CARRIER_TERMINAL_STATUSES

    def as_dict(self) -> dict:
        return {
            "shipment_id": self.shipment_id,
            "tracking_no": self.tracking_no,
            "idempotency_key": self.idempotency_key,
            "case_id": self.case_id,
            "carrier": self.carrier,
            "status": self.status,
            "is_terminal": self.is_terminal,
            "message": self.message,
            "poll_count": self.poll_count,
            "delivered_at": self.delivered_at,
            "detail": dict(self.detail),
        }


class CarrierPort(Protocol):
    """承运商适配器要实现的两个动作。"""

    def ship(self, order: ShipmentOrder) -> CarrierAdvice: ...

    def track(self, tracking_no: str) -> CarrierAdvice: ...


_CARRIER_MESSAGE_OF: dict[str, str] = {
    CARRIER_CREATED: "运单已建，货尚未离仓",
    CARRIER_IN_TRANSIT: "运输途中",
    CARRIER_DELIVERED: "已签收",
    CARRIER_EXCEPTION: "运输异常（丢件 / 拒收 / 途中破损退回）；"
                       "货未送达供应商，**不许据此推进对账**",
}


class MockCarrier:
    """演示用承运商。确定性，无随机数。

    ``deliver_after`` 保证「一次 track 不一定够」。``script`` 按 ``case_id`` 注入
    ``exception``，演失败路径。
    """

    def __init__(self, *, deliver_after: int = DEFAULT_DELIVER_AFTER,
                 script: dict[str, str] | None = None) -> None:
        if deliver_after < 1:
            raise ValueError("deliver_after 至少为 1 —— 一发就签收的承运商演不出观察")
        self.deliver_after = int(deliver_after)
        self.script = dict(script or {})
        self._ledger: dict[str, CarrierAdvice] = {}
        self._by_key: dict[str, tuple[str, tuple]] = {}

    def ship(self, order: ShipmentOrder) -> CarrierAdvice:
        """下一张退货运单。**返回值永远不是终态**：运单建了不等于货送到了。

        幂等口径同 ``MockSupplier.rma_submit``。
        """
        key = order.idempotency_key
        if not key:
            raise ValueError("发运指令必须带幂等键 —— 没有它就挡不住第二张运单")

        known = self._by_key.get(key)
        if known is not None:
            tracking_no, fingerprint = known
            if fingerprint != order.fingerprint():
                raise DuplicateRequest(
                    f"幂等键 {key!r} 上已经有一条参数不同的发运指令："
                    f"库里 {fingerprint}、这次 {order.fingerprint()}。"
                    f"这不是重发，是两次不同的发运撞了同一个键")
            return self._ledger[tracking_no]

        suffix = uuid.uuid4().hex[:12]
        advice = CarrierAdvice(
            shipment_id=f"shp-{suffix}",
            tracking_no=f"trk-{suffix}",
            idempotency_key=key,
            case_id=order.case_id,
            carrier=order.carrier,
            status=CARRIER_CREATED,           # ← 建单，**不是**终态
            message=_CARRIER_MESSAGE_OF[CARRIER_CREATED],
            detail={"rma_id": order.rma_id,
                    "origin": order.origin,
                    "destination": order.destination,
                    "parcel_count": order.parcel_count},
        )
        self._ledger[advice.tracking_no] = advice
        self._by_key[key] = (advice.tracking_no, order.fingerprint())
        return advice

    def track(self, tracking_no: str) -> CarrierAdvice:
        """问一次轨迹。已经是终态的不再变。"""
        advice = self._ledger.get(tracking_no)
        if advice is None:
            raise LookupError(f"没有这条运单：{tracking_no}")
        if advice.is_terminal:
            return advice

        polls = advice.poll_count + 1
        if polls < self.deliver_after:
            return self._store(replace(
                advice, status=CARRIER_IN_TRANSIT, poll_count=polls,
                message=f"运输途中（第 {polls} 次查询）"))

        target = self.script.get(advice.case_id, CARRIER_DELIVERED)
        if target not in ALL_CARRIER_STATUSES:
            raise ValueError(f"脚本给了未知的承运商回执状态：{target!r}")
        return self._store(replace(
            advice, status=target, poll_count=polls,
            message=_CARRIER_MESSAGE_OF[target],
            delivered_at=("2026-09-02" if target == CARRIER_DELIVERED else "")))

    def _store(self, advice: CarrierAdvice) -> CarrierAdvice:
        self._ledger[advice.tracking_no] = advice
        return advice


# ===========================================================================
# AP 系统：调整凭单 / 借项通知单（🔴 只读，没有任何写方法）
# ===========================================================================
@dataclass(frozen=True)
class ApAdjustmentAdvice:
    """AP 系统回执 —— 一次观察的记录。"""

    case_id: str

    status: str
    """none / staged / built / settled / voided。"""

    adjustment_id: str = ""
    """AP 侧的调整凭单号，**AP 生成，我方不许造**。``none`` 时为空串。"""

    amount: str = ""
    currency: str = ""

    ap_reference: str = ""
    """核销流水号。**只有 settled 才有** —— 它是「钱确实到账」的外部凭据。"""

    message: str = ""
    poll_count: int = 0
    settled_at: str = ""
    detail: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in ALL_AP_ADJUSTMENT_STATUSES:
            raise ValueError(f"未知的 AP 调整凭单状态：{self.status}")
        if self.status != AP_SETTLED and self.ap_reference:
            raise ValueError(
                f"状态 {self.status} 的回执不许带 ap_reference —— "
                f"核销流水号是 settled 才有的东西")

    @property
    def is_terminal(self) -> bool:
        return self.status in AP_ADJUSTMENT_TERMINAL_STATUSES

    @property
    def is_settlement_evidence(self) -> bool:
        """这份回执够不够格当 ``settled`` 的判据。**只有 settled 算数**，
        ``voided``（凭单作废）是终态但不是「钱到账」。"""
        return self.status == AP_SETTLED and bool(self.ap_reference)

    def as_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "status": self.status,
            "is_terminal": self.is_terminal,
            "is_settlement_evidence": self.is_settlement_evidence,
            "adjustment_id": self.adjustment_id,
            "amount": self.amount,
            "currency": self.currency,
            "ap_reference": self.ap_reference,
            "message": self.message,
            "poll_count": self.poll_count,
            "settled_at": self.settled_at,
            "detail": dict(self.detail),
        }


class ApSystemPort(Protocol):
    """AP 系统适配器要实现的动作 —— **只有一个，而且是只读的**。

    协议里刻意不留写方法：换真 AP 系统时，实现者照着这个协议做，也就没有地方能把
    「建调整凭单」接进来。RTV 域不许写 AP 的任何东西（契约 C-R5 红字）。
    """

    def query(self, case_id: str) -> ApAdjustmentAdvice: ...


_AP_MESSAGE_OF: dict[str, str] = {
    AP_NONE: "AP 侧尚未就本案建调整凭单；**这不是失败**，只是还没到",
    AP_STAGED: "已进入 AP 待处理队列",
    AP_BUILT: "调整凭单已建，尚未核销",
    AP_SETTLED: "调整凭单已核销，款项到账",
    AP_VOIDED: "调整凭单已作废；本笔退款不会到账，须转补偿路径",
}


class MockApSystem:
    """演示用 AP 系统。确定性，无随机数。

    🔴 **只有 ``query()``。没有任何写方法，也不许加。** 调整凭单由 AP 侧按自己的
    规则建，我方只观察 —— 两处都能写会让「这笔调整是谁建的」失去唯一答案。

    ``ledger`` 是构造时给定的「AP 那边本来就有的那些案子」：``{case_id: {"amount":
    ..., "currency": ...}}``。**不在 ledger 里的案子恒为 ``none``** —— 那正是
    「货退了、供应商也开票了，AP 却迟迟没建凭单」这一档，而它非常容易被系统假装成
    失败。``none`` 不是终态，只能继续问或转人工。

    ``script`` 按 ``case_id`` 注入 ``voided``，演凭单作废的失败路径。
    """

    def __init__(self, *, ledger: dict[str, dict] | None = None,
                 settle_after: int = DEFAULT_AP_SETTLE_AFTER,
                 script: dict[str, str] | None = None) -> None:
        if settle_after < 2:
            raise ValueError("settle_after 至少为 2 —— 一问就终态的 AP 演不出观察")
        self.settle_after = int(settle_after)
        self.ledger = {k: dict(v) for k, v in (ledger or {}).items()}
        self.script = dict(script or {})
        #: case_id -> 当前回执
        self._observed: dict[str, ApAdjustmentAdvice] = {}

    def known_cases(self) -> Iterable[str]:
        """AP 那边有凭单的案子。只读辅助，**不是**写入口。"""
        return tuple(self.ledger)

    def query(self, case_id: str) -> ApAdjustmentAdvice:
        """问一次 AP 调整凭单。每问一次 ``poll_count`` 加一。

            不在 ledger 里            -> none     （AP 还没建，非终态）
            polls == 1                -> staged
            1 < polls < settle_after  -> built
            polls >= settle_after     -> 脚本指定的终态（缺省 settled）

        **已经是终态的不再变**。
        """
        prev = self._observed.get(case_id)
        if prev is not None and prev.is_terminal:
            return prev
        polls = (prev.poll_count if prev else 0) + 1

        row = self.ledger.get(case_id)
        if row is None:
            advice = ApAdjustmentAdvice(
                case_id=case_id, status=AP_NONE, poll_count=polls,
                message=_AP_MESSAGE_OF[AP_NONE])
            self._observed[case_id] = advice
            return advice

        amount = _money(str(row.get("amount", "")), f"ledger[{case_id}].amount")
        currency = str(row.get("currency", "CNY"))
        adjustment_id = str(row.get("adjustment_id", "")) or f"apadj-{case_id}"

        if polls == 1:
            status, ap_reference, settled_at = AP_STAGED, "", ""
        elif polls < self.settle_after:
            status, ap_reference, settled_at = AP_BUILT, "", ""
        else:
            status = self.script.get(case_id, AP_SETTLED)
            if status not in ALL_AP_ADJUSTMENT_STATUSES:
                raise ValueError(f"脚本给了未知的 AP 调整凭单状态：{status!r}")
            ap_reference = f"apref-{adjustment_id}" if status == AP_SETTLED else ""
            settled_at = "2026-09-02" if status == AP_SETTLED else ""

        advice = ApAdjustmentAdvice(
            case_id=case_id,
            status=status,
            adjustment_id=adjustment_id,
            amount=amount,
            currency=currency,
            ap_reference=ap_reference,
            message=_AP_MESSAGE_OF[status],
            poll_count=polls,
            settled_at=settled_at,
            detail={"source_system": "ap"},
        )
        self._observed[case_id] = advice
        return advice


# ===========================================================================
# ToolPort 九要素声明（契约 C-R5 的五个口子）
# ===========================================================================
def _rma_submit(*, supplier: Any, request: RmaRequest) -> dict:
    """``SUPPLIER_RMA_SUBMIT_PORT`` 的 entry。返回 dict 而不是 SupplierAdvice ——
    产物要能 json 化。"""
    return supplier.rma_submit(request).as_dict()


def _credit_query(*, supplier: Any, rma_id: str) -> dict:
    return supplier.credit_query(rma_id).as_dict()


def _ship(*, carrier: Any, order: ShipmentOrder) -> dict:
    return carrier.ship(order).as_dict()


def _track(*, carrier: Any, tracking_no: str) -> dict:
    return carrier.track(tracking_no).as_dict()


def _adjust_query(*, ap_system: Any, case_id: str) -> dict:
    return ap_system.query(case_id).as_dict()


SUPPLIER_RMA_SUBMIT_PORT = ToolPort(
    name="supplier.rma_submit",
    purpose="向供应商门户递一条退货授权（RMA）申请；返回受理回执，**永远不是终态**",
    entry=_rma_submit,
    params_schema={
        "supplier": "SupplierPort（进程内按名取到的供应商门户实例）",
        "request": "RmaRequest（金额为字符串，reason_code 取 rtv_codes.RETURN_REASONS）",
    },
    returns_schema={
        "rma_id": "str（供应商侧退货授权号，supplier.credit_query 用它）",
        "idempotency_key": "str",
        "case_id": "str（RTV 案子号）",
        "status": "submitted —— 递出态，永不为 issued/disputed",
        "is_terminal": "bool（恒 False）",
        "is_credit_evidence": "bool（恒 False —— 递申请不构成任何权威判据）",
        "amount_claimed": "str（退货方自称的应退金额，非供应商认的金额）",
        "currency": "str",
        "credit_note_id": "str（恒空串 —— 贷项通知单只有 issued 才有）",
        "poll_count": "int（恒 0，递申请不算一次观察）",
    },
    failure_modes=[
        "幂等键为空 -> ValueError：没有幂等键就挡不住第二张退货授权",
        "同一幂等键上参数不一致 -> DuplicateRequest，**不静默收下也不静默丢弃**："
        "收下会开出第二张 RMA，丢弃会让调用方拿到一份与自己递进来的申请对不上的回执",
        "reason_code 不在 RETURN_REASONS 内 -> KeyError（RmaRequest 构造时即抛），"
        "上层据此出「退货理由不可核对」的拒收结论",
        "供应商门户不可达 / 超时 -> 由适配器抛，经 invoke_tool 落审计后原样上抛，"
        "上层按「未知外部状态」处置，**不许推断成 disputed**："
        "「我没问到」和「供应商说不认」是两回事",
    ],
    security_boundary=(
        "只递申请，不判成败：本 port 的返回值永远不是终态，"
        "任何据此写 credited 的代码都是 bug（铁律 8）—— credited 的唯一判据是 "
        "supplier.credit_query 观察到 issued，而写入方只有 rtv.observe。"
        "金额一律字符串，不进浮点。幂等键由 (tenant, case) 唯一确定，"
        "一个退货案子只允许有一张退货授权"
    ),
    rate_limit="",
    owner="rtv_settlement",
)

SUPPLIER_CREDIT_QUERY_PORT = ToolPort(
    name="supplier.credit_query",
    purpose="问一次供应商门户 —— 全系统**唯一**能取得 credited 判据（贷项通知单已开出）的途径",
    entry=_credit_query,
    params_schema={
        "supplier": "SupplierPort（进程内按名取到的供应商门户实例）",
        "rma_id": "str（supplier.rma_submit 返回的供应商侧退货授权号）",
    },
    returns_schema={
        "status": "submitted|acknowledged|issued|disputed|unknown"
                  "（取值域唯一出处：rtv_codes.SUPPLIER_STATUSES）",
        "is_terminal": "bool（只有 issued / disputed 为 True）",
        "is_credit_evidence": "bool（**只有 issued 且带单号才为 True** —— "
                              "acknowledged 恒 False）",
        "poll_count": "int（问了几次 —— 终态是问出来的证据）",
        "credit_note_id": "str（**仅 issued 才有**：供应商侧单号，"
                          "acknowledged 时恒为空串）",
        "document_type": "str（仅 issued 才有：UNCL1001 的 381，见 rtv_codes）",
        "amount_credited": "str（仅 issued 才有：供应商**认的**金额，"
                           "与 amount_claimed 刻意分开）",
        "issued_at": "str（仅 issued 才有：供应商开票时间，非我方观察时间）",
    },
    failure_modes=[
        "退货授权号不存在 -> LookupError",
        "轮询到顶仍非终态 -> **如实返回非终态回执**，不许改判成 disputed："
        "「我问累了」和「供应商说不认这笔退货」是两回事",
        "status=acknowledged -> **这不是 credited 的判据**：供应商收到退货了，"
        "还没开贷项通知单，差着一次会计确认。据此推进 credited 是本域第一号 bug",
        "status=unknown -> 该笔**可能已被受理**，不许重提 RMA（会开出第二张授权），"
        "只能继续问或转人工",
    ],
    security_boundary=(
        "只读，不写供应商门户的任何东西。本 port 是 rtv.observe 取得 credited "
        "权威事实的唯一入口，而 rtv.observe 是全系统唯一写得进 credited 的 actor"
        "（契约 C-R3：AUTHORITATIVE_WRITER = rtv.observe）。"
        "判据只收 issued，**acknowledged 绝不许进 credited 的判据集**；"
        "非终态回执一律不推进业务状态"
    ),
    rate_limit="",
    owner="rtv_reconcile",
)

CARRIER_SHIP_PORT = ToolPort(
    name="carrier.ship",
    purpose="向承运商下一张退货运单；返回建单回执，**永远不是终态**（建单不等于送达）",
    entry=_ship,
    params_schema={
        "carrier": "CarrierPort（进程内按名取到的承运商实例）",
        "order": "ShipmentOrder（必须带 rma_id —— 没有退货授权的货会被供应商拒收）",
    },
    returns_schema={
        "shipment_id": "str（承运商侧运单 id）",
        "tracking_no": "str（carrier.track 用它）",
        "case_id": "str",
        "status": "created —— 建单态，永不为 delivered/exception",
        "is_terminal": "bool（恒 False）",
        "poll_count": "int（恒 0，建单不算一次观察）",
    },
    failure_modes=[
        "order 缺 rma_id -> ValueError：没有退货授权就发货，供应商可以直接拒收，"
        "而货已经在路上",
        "幂等键为空 -> ValueError；同一幂等键上参数不一致 -> DuplicateRequest："
        "两张运单会让「货到底在哪一箱里」失去唯一答案",
        "承运商系统不可达 -> 原样上抛，上层按「未知外部状态」处置，"
        "**不许推断成 exception**：没建成单和货丢了是两回事",
    ],
    security_boundary=(
        "只下运单，不判送达：本 port 的返回值永远不是终态。业务状态 shipped 只能由"
        "carrier.track 观察到的回执得到，我方不许自称已发运（契约 C-R2 第 ③ 步"
        "「谁说了算」写的是承运商）。本 port 不碰供应商门户，也不碰 AP —— "
        "裁定的人不碰承运商，发运的人不碰供应商开票（契约 C-R7）"
    ),
    rate_limit="",
    owner="rtv_logistics",
)

CARRIER_TRACK_PORT = ToolPort(
    name="carrier.track",
    purpose="问一次承运商轨迹 —— 退货业务状态 shipped/送达只能由这里的回执得到",
    entry=_track,
    params_schema={
        "carrier": "CarrierPort（进程内按名取到的承运商实例）",
        "tracking_no": "str（carrier.ship 返回的运单号）",
    },
    returns_schema={
        "status": "created|in_transit|delivered|exception"
                  "（取值域唯一出处：rtv_codes.CARRIER_STATUSES）",
        "is_terminal": "bool（只有 delivered / exception 为 True）",
        "poll_count": "int（问了几次）",
        "delivered_at": "str（仅 delivered 才有：签收时间）",
        "message": "str（异常时说明是丢件 / 拒收 / 途中破损退回）",
    },
    failure_modes=[
        "运单号不存在 -> LookupError",
        "轮询到顶仍在途 -> **如实返回 in_transit**，不许改判成 exception："
        "「我问累了」和「承运商说货丢了」是两回事",
        "status=exception -> 货**未**送达供应商，不许据此推进对账；"
        "该案子走补偿路径（契约 C-R2 的 shipped -> compensated）",
    ],
    security_boundary=(
        "只读，不改承运商的任何东西。承运商是外部系统，回执是观察结果不是我方决定"
        "（契约 C-R1 里 rtv_shipment.carrier_status 那条注释）。"
        "本 port 不构成 credited / settled 的判据 —— 货到了不等于供应商认了钱，"
        "更不等于钱到账；那两个判据分别归 supplier.credit_query 与 ap.adjust_query"
    ),
    rate_limit="",
    owner="rtv_logistics",
)

AP_ADJUST_QUERY_PORT = ToolPort(
    name="ap.adjust_query",
    purpose="问一次 AP 系统的调整凭单 —— 全系统**唯一**能取得 settled 判据（款项到账）的途径",
    entry=_adjust_query,
    params_schema={
        "ap_system": "ApSystemPort（进程内按名取到的 AP 系统实例；"
                     "该协议**只有 query 一个方法，没有写方法**）",
        "case_id": "str（RTV 案子号）",
    },
    returns_schema={
        "status": "none|staged|built|settled|voided"
                  "（取值域唯一出处：rtv_codes.AP_ADJUSTMENT_STATUSES）",
        "is_terminal": "bool（只有 settled / voided 为 True）",
        "is_settlement_evidence": "bool（**只有 settled 且带核销流水号才为 True** —— "
                                  "voided 是终态但不是钱到账）",
        "adjustment_id": "str（AP 侧凭单号，**AP 生成，我方不许造**；none 时为空串）",
        "ap_reference": "str（仅 settled 才有：核销流水号，钱确实到账的外部凭据）",
        "poll_count": "int（问了几次 —— 终态是问出来的证据）",
        "settled_at": "str（仅 settled 才有）",
    },
    failure_modes=[
        "AP 侧还没就本案建凭单 -> status=none，**这不是失败也不是错误**，"
        "是「还没到」；不许改判成 voided，也不许因此重发任何东西",
        "轮询到顶仍非终态 -> **如实返回非终态回执**，不许改判成失败："
        "「我问累了」和「AP 说这笔作废了」是两回事",
        "status=voided -> 凭单作废，本笔退款不会到账，该案子走补偿路径"
        "（契约 C-R2 的 credited -> compensated），**不是** settled",
        "AP 系统不可达 -> 原样上抛，上层按「未知外部状态」处置，不许推断成任何终态",
    ],
    security_boundary=(
        "🔴 **只读，而且本模块不提供任何写 AP 的方法**（契约 C-R5 红字）：调整凭单由 "
        "AP 侧按自己的规则建，RTV 域只观察 —— 两处都能写会让「这笔调整是谁建的」"
        "失去唯一答案。ApSystemPort 协议里因此只有 query 一个方法。"
        "本 port 是 rtv.observe 取得 settled 权威事实的唯一入口，"
        "而 rtv.observe 是全系统唯一写得进 settled 的 actor（契约 C-R3）；"
        "非终态回执一律不推进业务状态"
    ),
    rate_limit="",
    owner="rtv_settlement",
)

#: 本域五个 port。测试与文档按它取，不在各处抄字面量（口径同 ``ap.AP_PORTS``）。
RTV_PORTS: tuple[ToolPort, ...] = (
    SUPPLIER_RMA_SUBMIT_PORT,
    SUPPLIER_CREDIT_QUERY_PORT,
    CARRIER_SHIP_PORT,
    CARRIER_TRACK_PORT,
    AP_ADJUST_QUERY_PORT,
)


# ===========================================================================
# 绑定层（T112 新增）—— 把三个外部系统的**实例**绑进上面五个 port 的 entry
# ===========================================================================
# 为什么需要这一层，而不是一句 ``functools.partial(entry, supplier=inst)``：
# 五个 port 的 entry 收的是**领域对象**（``RmaRequest`` / ``ShipmentOrder``）与
# ``rma_id`` / ``tracking_no``，而六个 skill 经 ``_common.call_tool`` 递进来的
# 一律是一份扁平 params（``{"tenant_id": ..., "case_id": ..., ...}``）——
# 两侧按契约 C-R8「五轨互不 import」各自开发，从没对过形状。
#
# 三件桥接都发生在**这一层**，T62 的五个 ``*_PORT`` 与 T63 的六个 skill 一行不改：
#
#   ① 形状：params dict -> 领域对象。构造 ``RmaRequest`` 要的字段
#      （supplier_id / po_id / gr_id / amount_claimed / line_count）全在
#      ``rtv_case`` 与 ``rtv_line`` 里，所以本类持有 store，按案子现读现造 ——
#      不缓存，案子的金额会随返工重裁而变。
#   ② 退货授权号：``supplier.credit_query`` 要 ``rma_id``，可顺利路径上**没有任何一步
#      调过 ``supplier.rma_submit``**（``rtv.observe`` 的 depends_tools 里刻意没有写
#      工具）。所以第一次问贷项通知单时在这里**懒建**一张 RMA：现实里退货件寄出之前
#      本来就要先拿授权号，缺的是场景没把这一步显式画进 DAG，不是判据少了一条。
#      幂等键恒为 (tenant, case)，重复调只会拿回同一张单。
#   ③ 理由码：T63 的 skill 用 ``defective`` / ``damaged`` 那一套
#      （``skills/builtin/rtv/_common.RETURN_REASONS``），T62 的 ``RmaRequest``
#      只收 ``RTV-RSN-0x``（``rtv_codes.RETURN_REASONS``）—— 两份码表**不是同一份**，
#      谁也没引用谁。在这里做一次显式映射，而不是在任一侧偷偷放宽校验：
#      两侧的校验都还在，只是中间多了一张对照表。对照表本身是一笔账，
#      记在 docs/BACKLOG.md。

#: 本段自己要读整张理由码表（判「已经是编号了吗」）。单独 import 一次而不是往文件
#: 顶上那个 import 块里加名字：那个块是 T62 交付面的一部分，本轨只许新增。
from maos.tools.rtv_codes import RETURN_REASONS as _RETURN_REASON_TABLE  # noqa: E402

#: T63 skill 侧理由码 -> T62 ``rtv_codes`` 的已核对编号。
#: 值必须落在 ``rtv_codes.RETURN_REASONS`` 里 —— 构造 ``RmaRequest`` 时会当场核。
REASON_CODE_ALIASES: dict[str, str] = {
    "damaged": "RTV-RSN-01",         # 运输途中损坏
    "defective": "RTV-RSN-04",       # 到货即为次品 / 功能不良
    "wrong_item": "RTV-RSN-03",      # 发错货
    "not_ordered": "RTV-RSN-03",     # 未订购的货物（同归错发 / 超发那一档）
    "over_shipment": "RTV-RSN-03",   # 超发
    "spec_change": "RTV-RSN-02",     # 规格变更：按验收未通过那一档退
}


def alias_return_reason(code: str) -> str:
    """把 skill 侧的理由码翻成 ``rtv_codes`` 的编号。已经是编号的原样返回。

    翻不出来就抛，**不兜底成一个通用编号**：兜底会让一笔理由说不清的退货带着一个
    看起来完全正常的编号递到供应商门户，而那正是 ``require_return_reason`` 存在的理由。
    """
    if code in _RETURN_REASON_TABLE:
        return code
    try:
        return REASON_CODE_ALIASES[code]
    except KeyError:
        raise KeyError(
            f"退货理由码 {code!r} 既不在 rtv_codes.RETURN_REASONS 里，"
            f"也没有对照条目（已知对照：{sorted(REASON_CODE_ALIASES)}）"
        ) from None


class RtvToolBinding:
    """一套外部系统实例 + 一个 store，折成六个 skill 认得的 ``extras["tools"]``。

    一条 RTV 案子的三个外部系统各一个实例。**按路径建一套**而不是全局单例：
    两条路径（顺利 / 失败）共用一个 ``MockSupplier`` 的话，失败路径那份 disputed
    脚本会把顺利路径的轮询计数一并推着走，两条链路的 ``poll_count`` 从此互相污染。

    ``origin`` / ``destination`` 只进运单，不进任何判据 —— 发货地址改了不影响这笔
    退货的任何结论，所以它们也不在 ``ShipmentOrder.fingerprint()`` 里。
    """

    def __init__(self, store: Any, *, supplier: Any, carrier: Any, ap_system: Any,
                 origin: str = "WH-1", destination: str = "") -> None:
        self.store = store
        self.supplier = supplier
        self.carrier = carrier
        self.ap_system = ap_system
        self.origin = origin
        self.destination = destination
        #: case_id -> rma_id。懒建之后记住，第二次问贷项通知单不再建第二张。
        self._rma_by_case: dict[str, str] = {}

    # ------------------------------------------------------------- 领域对象
    def _case_and_lines(self, tenant_id: str, case_id: str) -> tuple[dict, list[dict]]:
        from maos.domain.rtv import objects as _objects

        case = _objects.get_case(self.store, tenant_id, case_id)
        if case is None:
            raise LookupError(
                f"没有这个 case：tenant={tenant_id} case={case_id} —— "
                f"外部系统调用要按案子上的源单信息组装，案子还没建就无从组装")
        lines = _objects.rtv_lines(self.store, tenant_id, case_id)
        if not lines:
            raise LookupError(f"case={case_id} 没有任何退货行，组装不出退货授权申请")
        return case, lines

    def rma_request(self, tenant_id: str, case_id: str, *, note: str = "") -> RmaRequest:
        """按库里那一行现造一条退货授权申请。"""
        case, lines = self._case_and_lines(tenant_id, case_id)
        return RmaRequest(
            supplier_id=str(case["supplier_id"]),
            case_id=case_id,
            po_id=str(case["po_id"]),
            gr_id=str(case["gr_id"]),
            amount_claimed=str(case["amount_claimed"]),
            reason_code=alias_return_reason(str(lines[0]["reason_code"])),
            currency=str(case["currency"] or "CNY"),
            line_count=len(lines),
            idempotency_key=f"{tenant_id}:{case_id}",
            note=note,
        )

    def ensure_rma(self, tenant_id: str, case_id: str, *, note: str = "") -> str:
        """拿到这个案子的退货授权号，没有就递一张（幂等）。"""
        known = self._rma_by_case.get(case_id)
        if known:
            return known
        advice = SUPPLIER_RMA_SUBMIT_PORT.entry(
            supplier=self.supplier,
            request=self.rma_request(tenant_id, case_id, note=note))
        self._rma_by_case[case_id] = str(advice["rma_id"])
        return self._rma_by_case[case_id]

    # --------------------------------------------------------- 五个 entry 适配
    def _rma_submit(self, **params: Any) -> dict:
        """``rtv.compensate`` 的补提申请。

        **幂等键不变** —— 一个案子只有一张退货授权，补提拿回的是那张单的当前回执，
        不是第二张授权（``MockSupplier`` 那条 ``DuplicateRequest`` 守着这件事）。
        """
        tenant_id, case_id = params["tenant_id"], params["case_id"]
        request = self.rma_request(tenant_id, case_id,
                                   note=str(params.get("reason") or ""))
        advice = SUPPLIER_RMA_SUBMIT_PORT.entry(supplier=self.supplier, request=request)
        self._rma_by_case[case_id] = str(advice["rma_id"])
        return advice

    def _credit_query(self, **params: Any) -> dict:
        rma_id = self.ensure_rma(params["tenant_id"], params["case_id"])
        return SUPPLIER_CREDIT_QUERY_PORT.entry(supplier=self.supplier, rma_id=rma_id)

    def _ship(self, **params: Any) -> dict:
        tenant_id, case_id = params["tenant_id"], params["case_id"]
        case, _lines = self._case_and_lines(tenant_id, case_id)
        order = ShipmentOrder(
            case_id=case_id,
            rma_id=self.ensure_rma(tenant_id, case_id),
            carrier=str(params.get("carrier") or "demo-carrier"),
            origin=self.origin,
            destination=(self.destination
                         or str(params.get("address") or "")
                         or f"supplier:{case['supplier_id']}"),
            parcel_count=1,
            idempotency_key=f"{tenant_id}:{case_id}",
        )
        return CARRIER_SHIP_PORT.entry(carrier=self.carrier, order=order)

    def _track(self, **params: Any) -> dict:
        tracking_no = str(params.get("tracking_no") or "")
        if not tracking_no:
            raise LookupError(
                "查轨迹必须给运单号 —— 没有运单号的这货到哪了问的不是这一票货")
        return CARRIER_TRACK_PORT.entry(carrier=self.carrier, tracking_no=tracking_no)

    def _adjust_query(self, **params: Any) -> dict:
        return AP_ADJUST_QUERY_PORT.entry(
            ap_system=self.ap_system, case_id=params["case_id"])

    # ------------------------------------------------------------------ 装配
    def as_tools(self) -> dict:
        """折成 ``ctx.extras["tools"]``：契约 C-R5 的五个名字 -> 绑好实例的可调用。

        键必须逐字是 C-R5 那五个名字 —— ``_common.tool_port()`` 按名取，取不到就抛，
        **不自动兜底成一个空实现**（那会让忘了注册工具变成一路绿灯的失真）。
        """
        return {
            SUPPLIER_RMA_SUBMIT_PORT.name: self._rma_submit,
            SUPPLIER_CREDIT_QUERY_PORT.name: self._credit_query,
            CARRIER_SHIP_PORT.name: self._ship,
            CARRIER_TRACK_PORT.name: self._track,
            AP_ADJUST_QUERY_PORT.name: self._adjust_query,
        }
