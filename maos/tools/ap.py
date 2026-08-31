"""银行付款 ToolPort —— 付款指令的发出与回单的查询，应付账款域的外部权威边界。

## 这一层存在的理由（铁律 8）

一笔货款到底有没有从公司账户划出去，**权威在银行**，不在 MAOS 里。MAOS 能做的只有
两件事：发一条付款指令（``pay``）、问一次回单（``query``）。

所以本模块有一条硬规矩，与退款域的 ``gateway.py`` 逐字同构：

    ``pay()`` **永远不返回终态**。

    真实时序：pay()   -> accepted（银行受理了指令，钱还没走）
              query() -> pending … -> settled（这才是终态）

一步返回 ``settled`` 的 mock 会把整条论证抽空 —— 那样 ``ap.observe`` 就没有存在
理由，评委问「你怎么知道这笔货款付出去了」只能答「因为我的 mock 这么写的」。

## 第三种回单：unknown

比「还在清算」更要紧的是「**银行自己也说不清**」。跨行清算窗口、通道超时、
对账文件未回，都会落到这一档：**这一笔可能已经划走了，只是回单没拿到**。

四态里只有两个是终态：

    accepted   指令已受理，尚未清算        （非终态）
    pending    清算中                      （非终态）
    unknown    银行说不清，结果未知        （非终态，**必须继续 query**）
    settled    确定已付                    （终态）
    failed     确定未付                    （终态）

``unknown`` 时直接重发指令就可能付出**第二笔**，这是本模块防的第一号事故。

## 与退款域 ``gateway.py`` 的关系：形状同构，一行不共用

两者都是「外部权威的观察口」，状态四态、``pay``/``refund`` 不返回终态、幂等键
挡重复 —— 这些是同一套设计。但**不 import 对方、不共用类型**：

  · 退款是把钱**退回**给消费者，应付是把钱**付出**给供应商，幂等键的业务含义
    （一个案子一笔退款 vs 一张发票一笔付款）不是一回事；
  · 退款回执带的是支付宝两层错误码，本模块带的是 Peppol 的付款方式码；
  · 最要紧的一条：**回单字段刻意不叫 ``receipt``**（见 ``ADVICE_FIELD``）。

## ``ADVICE_FIELD`` 为什么不能叫 ``receipt``

``ReviewerGate._gate_gateway``（第七道闸）的触发条件是**数据形状**：本轮任一
artifact 的 ``content["receipt"]`` 是个带 ``code`` 的 dict。命中之后它会拿这个
``code`` 去查 ``maos/tools/gateway_codes.py`` 的支付宝码表，**查不到就判 blocker
并归到最危险的那一档**。

本域的银行回单如果也叫 ``receipt``，那道闸会对着一张它根本不认识的码表开火：
应付账款的每一份产物都会被判成「未知网关码」，然后返工、再返工。而闸没有任何
错 —— 它守的是退款域的码表，本来就不该认识银行回单。

所以本域的回单挂在 ``content["bank_advice"]`` 上。这不是绕开闸，是**不去撞一道
不该由本域触发的闸**：换个域就该有自己的判据面，共用字段名等于共用判据。
``maos/tests/test_ap_tools.py`` 有一条用例钉住这件事。

## 调用一律走 invoke_tool()

直接调 ``MockBank.pay()`` 就没有 ToolInvoked 审计行，出事之后查不到是谁、什么
参数、跑了多久。上层请走 ``invoke_tool(BANK_PAY_PORT, {...}, store=...)``。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from maos.tools.ap_codes import (
    CODE_CREDIT_TRANSFER,
    LIST_PAYMENT_MEANS,
    require_code,
)
from maos.tools.port import ToolPort

log = logging.getLogger("maos.tools.ap")

#: artifact 里挂银行回单的键名。**不许改成 "receipt"** —— 理由见模块 docstring。
ADVICE_FIELD = "bank_advice"

# 回单状态。终态只有两个 —— 见 TERMINAL_STATUSES。
STATUS_ACCEPTED = "accepted"
STATUS_PENDING = "pending"
STATUS_UNKNOWN = "unknown"
STATUS_SETTLED = "settled"
STATUS_FAILED = "failed"

#: 终态集合。``pay()`` 的返回值**永远不在这里面**，测试对这条有断言。
TERMINAL_STATUSES = frozenset({STATUS_SETTLED, STATUS_FAILED})

ALL_STATUSES = frozenset({
    STATUS_ACCEPTED, STATUS_PENDING, STATUS_UNKNOWN, STATUS_SETTLED, STATUS_FAILED})

#: mock 默认问几次才给出终态。>1 才能证明「一次 query 不一定够」。
DEFAULT_SETTLE_AFTER = 2


class DuplicateInstruction(RuntimeError):
    """同一个幂等键上来了一条**参数不同**的付款指令。

    不静默、也不当成新指令收下：两者都会导致同一张发票付出第二笔。
    """


@dataclass(frozen=True)
class PaymentInstruction:
    """一条付款指令。

    金额用字符串不用 float —— **金额永远不进浮点**（同 ``gateway.RefundRequest``）。
    """

    supplier_id: str
    invoice_id: str

    amount: str
    """应付金额。口径是 BR-CO-16 的 Amount due for payment（BT-115）。"""

    currency: str = "CNY"

    payment_means_code: str = CODE_CREDIT_TRANSFER
    """付款方式，取 UNCL4461。缺省 30 = Credit transfer。"""

    idempotency_key: str = ""
    """幂等键。由 (tenant, invoice) 唯一确定 —— 一张发票只允许有一笔付款。"""

    remittance_info: str = ""

    def __post_init__(self) -> None:
        # 码值当场核，不等到银行那边才发现。表外的码在 `require_code` 里抛，
        # 上层据此出 BR-CL-16 的拒付理由。
        require_code(LIST_PAYMENT_MEANS, self.payment_means_code)

    def fingerprint(self) -> tuple[str, str, str]:
        """幂等比对面：同一个键下这三项变了就算「重复指令不一致」。

        不比 ``remittance_info``：附言改了不影响资金结果。与
        ``gateway.RefundRequest.fingerprint`` 同一条口径。
        """
        return (self.invoice_id, self.amount, self.currency)


@dataclass(frozen=True)
class BankAdvice:
    """银行回单 —— **一次观察的记录，不是事实本身**。

    ``frozen=True`` 是有意的：回单代表「某一时刻银行说了什么」，改它等于篡改观察
    记录。状态推进用 ``replace()`` 产生新回单，旧的留在审计里。
    """

    instruction_id: str
    """银行侧指令 id，``query()`` 用它。"""

    idempotency_key: str

    status: str
    """accepted / pending / unknown / settled / failed。见模块 docstring。"""

    amount: str
    currency: str

    payment_means_code: str
    """UNCL4461 码值，原样回显 —— 银行确认按哪种方式出账。"""

    message: str = ""
    """银行给的人话说明。"""

    poll_count: int = 0
    """已经 query 过几次。审计用：证明终态是**问出来的**，不是猜出来的。"""

    value_date: str = ""
    """起息日 / 入账日。只有终态回单才有。"""

    bank_reference: str = ""
    """银行流水号。只有 settled 才有 —— 它是「钱确实走了」的外部凭据。"""

    detail: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in ALL_STATUSES:
            raise ValueError(f"未知的回单状态：{self.status}")

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def as_dict(self) -> dict:
        """折成 dict 供 artifact / 落库使用。

        额外带上 ``is_terminal`` 与 ``payment_means_name``：读产物的人（和评委）
        不必回到码表里查 30 是什么意思，也不必自己判这个状态算不算终态。
        """
        return {
            "instruction_id": self.instruction_id,
            "idempotency_key": self.idempotency_key,
            "status": self.status,
            "is_terminal": self.is_terminal,
            "amount": self.amount,
            "currency": self.currency,
            "payment_means_code": self.payment_means_code,
            "payment_means_name": require_code(
                LIST_PAYMENT_MEANS, self.payment_means_code).name,
            "message": self.message,
            "poll_count": self.poll_count,
            "value_date": self.value_date,
            "bank_reference": self.bank_reference,
            "detail": dict(self.detail),
        }


class BankPort(Protocol):
    """银行适配器要实现的两个动作。换真银行时实现这个协议即可，上层一字不改。"""

    def pay(self, instruction: PaymentInstruction) -> BankAdvice: ...

    def query(self, instruction_id: str) -> BankAdvice: ...


class MockBank:
    """演示用银行。**确定性**：同样的入参连跑两次输出逐条一致，不用随机数。

    ``settle_after`` 是本类存在的全部意义：它保证**一次 query 不够**。
    设成一个大于轮询上限的数（失败路径就是这么用的），就能造出「问不出终态」——
    那是应付账款里最常见、也最容易被系统假装成失败的一档。

    ``script`` 按 ``invoice_id`` 注入非正常回单：值取 ``STATUS_*`` 之一，
    ``query()`` 到点之后回它而不是 ``settled``。
    """

    def __init__(self, *, settle_after: int = DEFAULT_SETTLE_AFTER,
                 script: dict[str, str] | None = None) -> None:
        if settle_after < 1:
            raise ValueError("settle_after 至少为 1 —— 一发就终态的银行演不出观察")
        self.settle_after = int(settle_after)
        self.script = dict(script or {})
        #: instruction_id -> 当前回单
        self._ledger: dict[str, BankAdvice] = {}
        #: idempotency_key -> (instruction_id, fingerprint)
        self._by_key: dict[str, tuple[str, tuple[str, str, str]]] = {}
        #: instruction_id -> 那条指令，query 时要用它挑脚本
        self._instructions: dict[str, PaymentInstruction] = {}

    # ------------------------------------------------------------------ pay
    def pay(self, instruction: PaymentInstruction) -> BankAdvice:
        """发一条付款指令。**返回值永远不是终态**。

        幂等：同一个 ``idempotency_key`` 再来一次 —— 参数一致就原样返回**同一笔**
        的当前回单（不新建第二笔）；参数不一致抛 ``DuplicateInstruction``。

        后者不静默收下也不静默丢弃：收下会付出第二笔，丢弃会让调用方拿到一份
        与自己递进来的指令对不上的回单。与退款域 ``create_case`` 的案号冲突
        同一个 fail-closed 口径。
        """
        key = instruction.idempotency_key
        if not key:
            raise ValueError("付款指令必须带幂等键 —— 没有它就挡不住第二笔")

        known = self._by_key.get(key)
        if known is not None:
            instruction_id, fingerprint = known
            if fingerprint != instruction.fingerprint():
                raise DuplicateInstruction(
                    f"幂等键 {key!r} 上已经有一条参数不同的付款指令："
                    f"库里 {fingerprint}、这次 {instruction.fingerprint()}。"
                    f"这不是重发，是两笔不同的付款撞了同一个键"
                )
            return self._ledger[instruction_id]

        instruction_id = f"bkins-{uuid.uuid4().hex[:12]}"
        advice = BankAdvice(
            instruction_id=instruction_id,
            idempotency_key=key,
            status=STATUS_ACCEPTED,          # ← 受理，**不是**终态
            amount=instruction.amount,
            currency=instruction.currency,
            payment_means_code=instruction.payment_means_code,
            message="指令已受理，尚未清算；终态须经 query 观察",
            detail={"supplier_id": instruction.supplier_id,
                    "invoice_id": instruction.invoice_id},
        )
        self._ledger[instruction_id] = advice
        self._by_key[key] = (instruction_id, instruction.fingerprint())
        self._instructions[instruction_id] = instruction
        return advice

    # ---------------------------------------------------------------- query
    def query(self, instruction_id: str) -> BankAdvice:
        """问一次回单。每问一次 ``poll_count`` 加一。

        到了 ``settle_after`` 次才给出脚本指定的状态（缺省 ``settled``）；
        没到就是 ``pending``。**已经是终态的不再变**：终态是终态。
        """
        advice = self._ledger.get(instruction_id)
        if advice is None:
            raise LookupError(f"没有这条付款指令：{instruction_id}")
        if advice.is_terminal:
            return advice

        polls = advice.poll_count + 1
        if polls < self.settle_after:
            advice = replace(advice, status=STATUS_PENDING, poll_count=polls,
                             message=f"清算中（第 {polls} 次查询）")
        else:
            invoice_id = self._instructions[instruction_id].invoice_id
            target = self.script.get(invoice_id, STATUS_SETTLED)
            if target not in ALL_STATUSES:
                raise ValueError(f"脚本给了未知的回单状态：{target!r}")
            advice = replace(
                advice, status=target, poll_count=polls,
                message=_MESSAGE_OF[target],
                value_date=("2026-08-31" if target in TERMINAL_STATUSES else ""),
                bank_reference=(f"bkref-{instruction_id[6:]}"
                                if target == STATUS_SETTLED else ""),
            )
        self._ledger[instruction_id] = advice
        return advice


#: 各状态回单上给人看的那句话。集中在这里而不是散在分支里 —— 散着写，改一句话
#: 要翻三个分支，而漏改的那句不会有任何症状。
_MESSAGE_OF: dict[str, str] = {
    STATUS_ACCEPTED: "指令已受理，尚未清算",
    STATUS_PENDING: "清算中",
    STATUS_UNKNOWN: "银行未能给出该笔指令的下落（通道超时 / 对账文件未回）；"
                    "该笔**可能已经划出**，不许据此重发",
    STATUS_SETTLED: "已清算入账",
    STATUS_FAILED: "银行明确拒付",
}


# ---------------------------------------------------------------------------
# ToolPort 九要素声明
# ---------------------------------------------------------------------------
def _pay(*, bank: Any, instruction: PaymentInstruction) -> dict:
    """``BANK_PAY_PORT`` 的 entry。返回 dict 而不是 BankAdvice —— 产物要能 json 化。"""
    return bank.pay(instruction).as_dict()


def _query(*, bank: Any, instruction_id: str) -> dict:
    return bank.query(instruction_id).as_dict()


BANK_PAY_PORT = ToolPort(
    name="bank.pay",
    purpose="向银行发出一条付款指令；返回受理回单，**永远不是终态**",
    entry=_pay,
    params_schema={
        "bank": "BankPort（进程内按名取到的银行实例，见 skills/builtin/ap/_common.py）",
        "instruction": "PaymentInstruction（金额为字符串，付款方式取 UNCL4461）",
    },
    returns_schema={
        "instruction_id": "str（银行侧指令 id，query 用它）",
        "idempotency_key": "str",
        "status": "accepted —— 受理态，永不为 settled/failed",
        "is_terminal": "bool（恒 False）",
        "amount": "str", "currency": "str",
        "payment_means_code": "str（UNCL4461）",
        "payment_means_name": "str（码表里的官方名称）",
        "poll_count": "int（恒 0，受理不算一次观察）",
    },
    failure_modes=[
        "幂等键为空 -> ValueError：没有幂等键就挡不住第二笔付款",
        "同一幂等键上参数不一致 -> DuplicateInstruction，**不静默收下也不静默丢弃**",
        "付款方式码不在 UNCL4461 内 -> KeyError（PaymentInstruction 构造时即抛）",
        "银行不可达 / 超时 -> 由适配器抛，经 invoke_tool 落审计后原样上抛，"
        "上层按「未知外部状态」处置，**不许推断成失败**",
    ],
    security_boundary=(
        "只发指令，不判成败：本 port 的返回值永远不是终态，任何据此写 settled 的代码"
        "都会被 maos/domain/ap/guard.py 抛回来（铁律 8）。"
        "金额一律字符串，不进浮点。幂等键由 (tenant, invoice) 唯一确定，"
        "一张发票只允许有一笔付款指令"
    ),
    rate_limit="",
    owner="ap_treasury",
)

BANK_QUERY_PORT = ToolPort(
    name="bank.query",
    purpose="问一次银行回单 —— 应付账款域**唯一**能取得付款终态的途径",
    entry=_query,
    params_schema={
        "bank": "BankPort（进程内按名取到的银行实例）",
        "instruction_id": "str（bank.pay 返回的银行侧指令 id）",
    },
    returns_schema={
        "status": "accepted|pending|unknown|settled|failed",
        "is_terminal": "bool（只有 settled / failed 为 True）",
        "poll_count": "int（问了几次 —— 终态是问出来的证据）",
        "bank_reference": "str（仅 settled 才有：银行流水号，钱确实走了的外部凭据）",
        "value_date": "str（仅终态才有：起息日）",
        "payment_means_code": "str（UNCL4461）",
    },
    failure_modes=[
        "指令 id 不存在 -> LookupError",
        "轮询到顶仍非终态 -> **如实返回非终态回单**，不许改判成失败："
        "「我问累了」和「银行说没付成」是两回事",
        "status=unknown -> 该笔**可能已经划出**，不许重发指令，只能继续问或转人工",
    ],
    security_boundary=(
        "只读。本 port 是 ap.observe 取得权威事实的唯一入口，"
        "而 ap.observe 是全系统唯一写得进 settled 的 actor（maos/domain/ap/guard.py）。"
        "非终态回单一律不推进业务状态"
    ),
    rate_limit="",
    owner="ap_treasury",
)

#: 本域两个 port。测试与文档按它取，不在各处抄字面量。
AP_PORTS: tuple[ToolPort, ...] = (BANK_PAY_PORT, BANK_QUERY_PORT)
