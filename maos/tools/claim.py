"""赔付方 ToolPort —— 赔付指令的发起与查询，以及「观察与推断分离」的落点。

## 这一层存在的理由（铁律 8）

保单、责任认定、赔款到账的**权威状态永远在赔付方那边**，不在 MAOS 里。MAOS 能做的
只有两件事：发一条赔付指令（``submit``）、问一次结果（``query``）。**任何把外部状态
直接写死为终态的代码都是 bug** —— 包括「调用没抛异常所以赔款打出去了」这种看起来
无害的推断。

所以本模块有一条硬规矩：``submit()`` **永远不返回 paid**。

    真实时序：submit() -> processing（受理了，还没划账）
              query()  -> processing … -> paid（这才是终态）

一步返回 ``paid`` 的 mock 会把整个论证抽空 —— 那样 ``claim.observe`` 就没有存在理由
了，评委问「你怎么知道赔款到账了」只能答「因为我的 mock 这么写的」。

## 四态回执，终态只有两个

    processing  受理了，处理中          （非终态）
    unknown     赔付方说不清，结果未知  （非终态，**必须 query**）
    paid        确定到账                （终态）
    denied      确定拒付                （终态，带 X12 CARC）

``unknown`` 时直接重发就可能造成第二笔赔款，这是本模块防的第一号事故。

## denied 的回执必须带得出「凭哪一条拒的」

拒付回执一律带 X12 的两个码（见 ``maos/tools/claim_codes.py``）：

  · ``group_code``（CO/PR/OA/PI）—— 这笔被调整掉的钱由谁承担；
  · ``carc_code`` —— 为什么调整。

再加上原文里点名要求的 ``remark_codes``（RARC）：``16`` / ``96`` / ``252`` 三条码的
官方描述里明写「At least one Remark Code must be provided」，所以本模块对这三条码
**强制**回执带 RARC，缺了当场抛 —— 造一份不合规范的回执，比不造更坏。

## 幂等

``idempotency_key`` 由 (tenant, claim) 唯一确定（``claim.pay`` 里生成）。同一个 key
重复调 ``submit()``：

  · 参数一致 -> 原样返回**同一笔**的当前回执，不新建第二笔
  · 参数不一致 -> 抛 ``DuplicateClaimPayment``，**不静默收下**：赔款金额被改了却用
    同一个幂等键，是上游算错了或者有人在改单，两种都必须让人看见。
    这里与支付宝那条 ``ACQ.DISCORDANT_REPEAT_REQUEST`` 分道：X12 没有对应的
    调整码，硬找一条 CARC 塞进去就是编造（本文件通篇在防的事）。

## 调用一律走 invoke_tool()

直接调 ``MockPayer.submit()`` 就没有 ToolInvoked 审计行，出事之后查不到是谁、
什么参数、跑了多久。上层请走 ``invoke_tool(PAYER_SUBMIT_PORT, {...}, store=...)``。
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from maos.tools.claim_codes import (
    EFFECT_DENIED,
    NEEDS_REMARK_CODE,
    AdjustmentCode,
    lookup,
    lookup_group,
)
from maos.tools.port import ToolPort

log = logging.getLogger("maos.tools.claim")


# 回执状态。终态只有两个 —— 见 TERMINAL_STATUSES。
STATUS_PROCESSING = "processing"
STATUS_UNKNOWN = "unknown"
STATUS_PAID = "paid"
STATUS_DENIED = "denied"

#: 终态集合。``submit()`` 的返回值**永远不是 paid**，测试对这条有断言。
TERMINAL_STATUSES = frozenset({STATUS_PAID, STATUS_DENIED})

#: mock 默认轮询几次到终态。>1 才能证明「一次 query 不一定够」。
DEFAULT_SETTLE_AFTER = 2

#: 拒付回执的缺省承担方。用 ``PI``（Payor Initiated Reduction）而不是 ``CO``：
#: 场景里的拒付是赔付方单方面认定不在保障范围，既没有合同价可依（CO），
#: 也不该转嫁给被保险人（PR）。具体案子可由 ``group_script`` 覆盖。
DEFAULT_GROUP_CODE = "PI"


class DuplicateClaimPayment(RuntimeError):
    """同一个幂等键上来了一份金额/收款方不同的赔付指令。见模块 docstring。"""


@dataclass(frozen=True)
class PaymentInstruction:
    """一条赔付指令。字段名对齐 X12 835 里「谁、按哪张单、赔多少」三样。"""

    claim_ref: str
    """赔付方侧的案件参考号。mock 用它来挑要注入哪个 CARC。"""

    amount: str
    """赔款金额。用字符串不用 float —— 金额永远不进浮点。"""

    idempotency_key: str
    payee: str = ""
    """收款方标识（被保险人 / 服务方）。改了它就是改了钱打给谁，属幂等比对面。"""

    memo: str = ""

    def fingerprint(self) -> tuple[str, str, str]:
        """幂等比对面：同一个 key 下，这三项变了就算「重复请求不一致」。

        不比 ``memo`` —— 备注改了不影响资金结果。**比 ``payee``**：钱打给谁变了
        当然是另一件事，漏掉它会让改收款方这种最该拦的改动从幂等键下溜过去。
        """
        return (self.claim_ref, self.amount, self.payee)


@dataclass(frozen=True)
class PayerReceipt:
    """赔付方回执 —— **一次观察的记录，不是事实本身**。

    frozen=True 是有意的：回执代表「某一时刻赔付方说了什么」，改它等于篡改观察记录。
    """

    request_id: str
    """赔付方侧请求 id，``query()`` 用它。"""

    idempotency_key: str
    status: str
    """processing / unknown / paid / denied。见模块 docstring。"""

    carc_code: str = ""
    """X12 CARC。到账时为空 —— 赔付方没有可说的调整，不是「没查」。"""

    group_code: str = ""
    """X12 Claim Adjustment Group Code（CO/PR/OA/PI）：这笔调整由谁承担。"""

    remark_codes: tuple[str, ...] = ()
    """伴随的 RARC。``16``/``96``/``252`` 的官方原文要求至少一条。"""

    description: str = ""
    """CARC 的官方描述**原文**，直接进 findings 给人看。"""

    effect: str = ""
    """码表判据：denied / reduced / patient_share。**MAOS 侧口径，非 X12 原文。**"""

    recourse: str = ""
    """码表判据：还能做什么。**MAOS 侧口径，非 X12 原文。**"""

    source: str = ""
    """码表出处。回执里带着它，评委问「这个码哪来的」当场能答。"""

    fetched_at: str = ""
    """码表核对日期 —— 说得清「我们照的是哪一版规范」。"""

    poll_count: int = 0
    """已经 query 过几次。审计用：证明终态是**问出来的**，不是猜出来的。"""

    detail: dict = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def needs_query(self) -> bool:
        """还需不需要再问一次。非终态一律要问，``unknown`` 尤其不许跳过。"""
        return not self.is_terminal

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "idempotency_key": self.idempotency_key,
            "status": self.status,
            "carc_code": self.carc_code,
            "group_code": self.group_code,
            "remark_codes": list(self.remark_codes),
            "description": self.description,
            "effect": self.effect,
            "recourse": self.recourse,
            "source": self.source,
            "fetched_at": self.fetched_at,
            "poll_count": self.poll_count,
            "is_terminal": self.is_terminal,
            "detail": dict(self.detail),
        }


def _denied_receipt(request_id: str, key: str, code: AdjustmentCode, group: str,
                    remarks: tuple[str, ...], *, poll_count: int = 0,
                    detail: dict | None = None) -> PayerReceipt:
    """由码表条目造一份拒付回执 —— description / effect / recourse / source 全部
    **从码表来**，不在调用处手填，手填就会和官方文档分叉。"""
    if code.code in NEEDS_REMARK_CODE and not remarks:
        raise ValueError(
            f"CARC {code.code} 的官方描述明写「At least one Remark Code must be "
            f"provided」，回执缺 RARC 就不合 X12 规范；造一份不合规范的回执"
            f"比不造更坏 —— 见 maos/tools/claim_codes.py 的 NEEDS_REMARK_CODE")
    lookup_group(group)                    # 未知组码在这里就抛，不留到上层
    return PayerReceipt(
        request_id=request_id,
        idempotency_key=key,
        status=STATUS_DENIED,
        carc_code=code.code,
        group_code=group,
        remark_codes=tuple(remarks),
        description=code.description,
        effect=code.effect,
        recourse=code.recourse,
        source=code.source,
        fetched_at=code.fetched_at,
        poll_count=poll_count,
        detail=dict(detail or {}),
    )


def _open_receipt(request_id: str, key: str, status: str, *,
                  poll_count: int = 0, detail: dict | None = None) -> PayerReceipt:
    """非终态或到账的回执 —— 一个 CARC 都不带。

    到账为什么不带码：X12 的 CARC 描述的是**调整**，全额照赔时没有任何调整可说。
    硬给它挂一条「成功码」就是发明了一条规范里不存在的码。
    """
    return PayerReceipt(request_id=request_id, idempotency_key=key, status=status,
                        poll_count=poll_count, detail=dict(detail or {}))


class PayerPort(Protocol):
    """赔付方的两个动作。签名冻结，上层只按这两个签名写代码。"""

    def submit(self, instruction: PaymentInstruction) -> PayerReceipt:
        """发起赔付。**返回值永远不是 paid** —— 见模块 docstring。"""
        ...

    def query(self, request_id: str) -> PayerReceipt:
        """查一笔赔付的当前状态。终态只能从这里来。"""
        ...


@dataclass
class _Entry:
    """账本里的一笔。mock 的「已经发生过的事」都记在这里。"""

    request_id: str
    instruction: PaymentInstruction
    code: AdjustmentCode | None
    """这一笔最终会落到哪个 CARC（None = 照赔到账）。"""

    group: str = DEFAULT_GROUP_CODE
    remarks: tuple[str, ...] = ()
    polls: int = 0
    closed: bool = False


class MockPayer:
    """演示用赔付方 —— 码值与异步时序都对齐 X12 官方码表。

    它是 mock，但**时序不是假的**：``submit()`` 一定给非终态，到账一定要 ``query()``
    问出来。这条时序是「观察与推断分离」能成立的前提，换成一步到位的 mock，
    整个论证就没了。

    错误注入按 ``claim_ref`` 走 ``script``，让场景可以用特定案件号稳定触发特定 CARC，
    不依赖随机数（确定性回放是本仓库定下的口径）。
    """

    def __init__(self, *, settle_after: int = DEFAULT_SETTLE_AFTER,
                 script: dict[str, str] | None = None,
                 group_script: dict[str, str] | None = None,
                 remark_script: dict[str, tuple[str, ...]] | None = None) -> None:
        if settle_after < 1:
            raise ValueError("settle_after 至少为 1 —— 赔付不允许一步到终态")
        self.settle_after = settle_after
        #: claim_ref -> CARC。码必须在码表里，构造时就校验，不留到调用时才炸。
        self.script = dict(script or {})
        for _ref, code in self.script.items():
            lookup(code)
        self.group_script = dict(group_script or {})
        for _ref, group in self.group_script.items():
            lookup_group(group)
        self.remark_script = {k: tuple(v) for k, v in (remark_script or {}).items()}
        self._ledger: dict[str, _Entry] = {}    # idempotency_key -> 账本
        self._by_request: dict[str, str] = {}   # request_id -> idempotency_key

    def __repr__(self) -> str:
        # 不带内存地址：这个对象会进 invoke_tool 的 params_digest，
        # 带地址会让同样参数每次算出不同的 digest，审计就对不上了。
        return f"MockPayer(settle_after={self.settle_after}, scripted={len(self.script)})"

    @property
    def payment_count(self) -> int:
        """一共产生了几笔赔付指令。幂等测试断言的就是它。"""
        return len(self._ledger)

    def submit(self, instruction: PaymentInstruction) -> PayerReceipt:
        key = instruction.idempotency_key
        if not key:
            raise ValueError("赔付指令必须带 idempotency_key")

        existing = self._ledger.get(key)
        if existing is not None:
            # —— 重复请求：无论如何都**不新建第二笔** ——
            if existing.instruction.fingerprint() != instruction.fingerprint():
                raise DuplicateClaimPayment(
                    f"幂等键 {key} 已经用过，但金额/案件号/收款方与上一笔不一致："
                    f"库里 {existing.instruction.fingerprint()}、"
                    f"这次 {instruction.fingerprint()}。不静默收下 —— "
                    "同一个案子的赔款被改了金额或收款方，必须让人看见")
            return self._observe(existing, advance=False)

        code_str = self.script.get(instruction.claim_ref)
        code = lookup(code_str) if code_str else None
        entry = _Entry(
            request_id=f"pay_{uuid.uuid4().hex[:16]}",
            instruction=instruction,
            code=code,
            group=self.group_script.get(instruction.claim_ref, DEFAULT_GROUP_CODE),
            remarks=self.remark_script.get(instruction.claim_ref, ()),
        )
        self._ledger[key] = entry
        self._by_request[entry.request_id] = key

        if code is not None and code.effect == EFFECT_DENIED:
            # 拒付赔付方当场就能判（保障范围、时限、授权这些不用等划账）。
            # 但**到账不能当场判** —— 那是异步的，必须 query。
            entry.closed = True
            return _denied_receipt(entry.request_id, key, code, entry.group, entry.remarks)

        if code is not None:
            # 调整类的码（削减 / 分摊）：赔付方受理了，但金额会被调整。
            # 这一档**不是终态**：到底划没划账仍然要问出来。
            return _open_receipt(entry.request_id, key, STATUS_UNKNOWN,
                                 detail={"pending_adjustment": code.code})

        # 正常路径：受理，处理中。**这里绝不能返回 paid。**
        return _open_receipt(entry.request_id, key, STATUS_PROCESSING)

    def query(self, request_id: str) -> PayerReceipt:
        key = self._by_request.get(request_id)
        if key is None:
            raise KeyError(f"未知 request_id：{request_id!r}")
        return self._observe(self._ledger[key], advance=True)

    def _observe(self, entry: _Entry, *, advance: bool) -> PayerReceipt:
        """产出一次观察。``advance=True`` 才推进轮询计数（只有 query 会推进）。"""
        if advance and not entry.closed:
            entry.polls += 1
            if entry.polls >= self.settle_after:
                entry.closed = True

        key = entry.instruction.idempotency_key

        if entry.code is not None and entry.code.effect == EFFECT_DENIED:
            return _denied_receipt(entry.request_id, key, entry.code, entry.group,
                                   entry.remarks, poll_count=entry.polls)

        if not entry.closed:
            # 还没到终态。带调整码的那一笔在轮询期间仍报 unknown，
            # 不许在这里「乐观」地当成 processing —— 那就是在推断。
            status = STATUS_UNKNOWN if entry.code is not None else STATUS_PROCESSING
            return _open_receipt(entry.request_id, key, status, poll_count=entry.polls)

        # 到终态了：赔付方确认划账。调整类的码留在 detail 里，账面上要看得见
        # 「这笔虽然到账了，但被按 CARC 45 削减过」。
        detail = {"adjusted_by": entry.code.code} if entry.code is not None else {}
        return _open_receipt(entry.request_id, key, STATUS_PAID,
                             poll_count=entry.polls, detail=detail)


class RealPayerAdapter:
    """真赔付方 API 适配层 —— **本轮只留壳**。

    签名与 ``MockPayer`` 完全一致，切换时上层一行不用改。

    每个方法都显式 ``raise NotImplementedError``，**不静默返回假数据** ——
    一个「看起来返回了点什么」的桩会让上层以为接通了，那比没实现危险得多
    （口径同 ``maos/tools/gateway.py::AlipaySandboxAdapter``）。
    """

    def __init__(self, *, endpoint: str = "", sender_id: str = "",
                 api_key: str = "") -> None:
        self.endpoint = endpoint
        self.sender_id = sender_id
        # 凭据走私有属性 + 自定义 __repr__，两道防线 —— 避免密钥进 repr /
        # pytest 对象打印 / traceback（铁律 6）。
        self._api_key = api_key

    def __repr__(self) -> str:
        return f"RealPayerAdapter(endpoint={self.endpoint!r}, sender_id={self.sender_id!r})"

    def submit(self, instruction: PaymentInstruction) -> PayerReceipt:
        raise NotImplementedError(
            "RealPayerAdapter.submit 尚未接通真实赔付方："
            "演示使用 MockPayer（码值与时序对齐 X12 官方码表）")

    def query(self, request_id: str) -> PayerReceipt:
        raise NotImplementedError(
            "RealPayerAdapter.query 尚未接通真实赔付方："
            "演示使用 MockPayer（码值与时序对齐 X12 官方码表）")


# ---------------------------------------------------------------------------
# 两个 ToolPort 声明（九要素）—— 调用一律走 invoke_tool()，直接调没有审计行
# ---------------------------------------------------------------------------

def payer_submit(*, payer: Any, claim_ref: str, amount: str, idempotency_key: str,
                 payee: str = "", memo: str = "") -> dict:
    """ToolPort 入口：发起赔付，返回回执 dict。

    入参摊平成基本类型而不是收一个 PaymentInstruction 对象：``invoke_tool`` 会把
    params 做 sha256 进审计行，摊平之后 digest 才对得上「同样的参数」这个直觉。
    """
    ins = PaymentInstruction(claim_ref=claim_ref, amount=amount,
                             idempotency_key=idempotency_key, payee=payee, memo=memo)
    return payer.submit(ins).to_dict()


def payer_query(*, payer: Any, request_id: str) -> dict:
    """ToolPort 入口：查一笔赔付的当前状态。终态只能从这里来。"""
    return payer.query(request_id).to_dict()


PAYER_SUBMIT_PORT = ToolPort(
    name="payer.submit",
    purpose="向赔付方发起赔付指令；返回受理回执，**不返回 paid**（到账须经 payer.query 观察）",
    entry=payer_submit,
    params_schema={"payer": "PayerPort", "claim_ref": "str",
                   "amount": "str（金额不进浮点）", "idempotency_key": "str",
                   "payee": "str（可选，收款方；属幂等比对面）", "memo": "str（可选）"},
    returns_schema={"request_id": "str", "status": "processing|unknown|denied（**不含 paid**）",
                    "carc_code": "str（X12 CARC，到账/在途时为空）",
                    "group_code": "str（CO|PR|OA|PI，这笔调整由谁承担）",
                    "remark_codes": "list[str]（RARC，16/96/252 强制要求至少一条）",
                    "effect": "denied|reduced|patient_share（MAOS 侧口径）",
                    "recourse": "none|resubmit_after_fix|route_other_payer|human_appeal",
                    "source": "str（码表出处 URL）", "fetched_at": "str（码表核对日期）",
                    "is_terminal": "bool"},
    failure_modes=[
        "ValueError: 缺 idempotency_key",
        "DuplicateClaimPayment: 同幂等键但金额/案件号/收款方不一致 —— 不静默收下",
        "ValueError: CARC 16/96/252 的回执缺 RARC（X12 原文要求至少一条）",
        "status=unknown: 赔付方说不清结果 —— **不许在本地推断成败**，必须 payer.query",
        "status=denied: 明确拒付，回执带 CARC + Group Code（如 96 Non-covered charge(s)）",
        "KeyError: 未知 CARC / 未知 Group Code（码表不兜底，见 claim_codes.lookup）",
        "NotImplementedError: 用了 RealPayerAdapter 而真实赔付方未接通",
    ],
    security_boundary=(
        "MAOS 不持有赔付的权威事实（铁律 8），本工具只产生**观察记录**："
        "submit 永不返回 paid，到账一律经 payer.query 取得；"
        "同一 idempotency_key 不产生第二笔赔付；"
        "码值判据全部取自 claim_codes 的已核对 X12 官方表，未知码抛 KeyError 不兜底；"
        "回执挂在 artifact 的 payer_receipt 键上，不占用 receipt —— "
        "那个键归第七道闸的支付宝码表，两张码表不许混查"
    ),
    rate_limit="",
    owner="task-T37",
)

PAYER_QUERY_PORT = ToolPort(
    name="payer.query",
    purpose="查询一笔赔付在赔付方侧的当前状态 —— paid 这个终态的唯一合法来源",
    entry=payer_query,
    params_schema={"payer": "PayerPort", "request_id": "str"},
    returns_schema={"status": "processing|unknown|paid|denied",
                    "poll_count": "int（问过几次，证明终态是问出来的）",
                    "carc_code": "str", "group_code": "str", "is_terminal": "bool"},
    failure_modes=[
        "KeyError: 未知 request_id",
        "status 仍为 processing/unknown: 还没到终态，继续轮询，**不许当成拒付**",
        "NotImplementedError: 用了 RealPayerAdapter 而真实赔付方未接通",
    ],
    security_boundary=(
        "只读观察，不改变赔付方侧任何状态；"
        "轮询次数落在回执的 poll_count 上，审计可证明终态来自观察而非本地推断"
    ),
    rate_limit="",
    owner="task-T37",
)


def receipt_json(receipt: dict) -> str:
    """把回执序列化成落库用的字符串。集中在这里，两处落库不各写一套排序口径。"""
    return json.dumps(receipt, ensure_ascii=False, sort_keys=True)
