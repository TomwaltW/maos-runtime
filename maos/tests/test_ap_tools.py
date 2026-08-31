"""银行 ToolPort —— 「发出去 ≠ 成功了」，以及不去撞别人的判据面。

两组判据：

  1. **`pay()` 永远不返回终态**、幂等键挡得住第二笔、`query()` 要问几次才给终态。
     这三条一起支撑 `ap.observe` 的存在理由：把银行换成一步返回 settled 的桩，
     那个 skill 就没必要存在了，整条论证跟着塌。
  2. **本域的回单不叫 `receipt`**。第七道闸按 `content["receipt"]` 里带不带 `code`
     触发，命中之后拿那个 code 去查**退款域**的支付宝码表，查不到就判 blocker
     并归到最危险的一档。本域的回单要是也叫 receipt，每一份产物都会被那道闸
     对着一张它根本不认识的码表开火。
"""

from __future__ import annotations

import pytest

from maos.runtime.gate import GATEWAY_RECEIPT_FIELD, ReviewerGate
from maos.tools import ap_codes
from maos.tools.ap import (
    ADVICE_FIELD,
    AP_PORTS,
    BANK_PAY_PORT,
    BANK_QUERY_PORT,
    DEFAULT_SETTLE_AFTER,
    STATUS_ACCEPTED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_SETTLED,
    STATUS_UNKNOWN,
    TERMINAL_STATUSES,
    BankAdvice,
    DuplicateInstruction,
    MockBank,
    PaymentInstruction,
)
from maos.tools.port import invoke_tool


def _instruction(**over) -> PaymentInstruction:
    kw = dict(supplier_id="SUP-1", invoice_id="INV-1", amount="100.00",
              idempotency_key="ap:t:c")
    kw.update(over)
    return PaymentInstruction(**kw)


# ------------------------------------------------------- pay 永远不返回终态
def test_pay_never_returns_a_terminal_advice():
    """本模块的第一号规矩。**这条塌了，ap.observe 就没有存在理由。**"""
    for settle_after in (1, 2, 5, 99):
        bank = MockBank(settle_after=settle_after)
        advice = bank.pay(_instruction(idempotency_key=f"k{settle_after}"))
        assert advice.status == STATUS_ACCEPTED
        assert advice.status not in TERMINAL_STATUSES
        assert advice.is_terminal is False
        assert advice.poll_count == 0, "受理不算一次观察"
        assert advice.bank_reference == "", "没问出终态就不该有流水号"


def test_terminal_state_takes_more_than_one_query():
    """一次 query 不一定够 —— `poll_count` 是「终态是问出来的」的审计证据。"""
    bank = MockBank(settle_after=3)
    advice = bank.pay(_instruction())
    seen = []
    for _ in range(3):
        advice = bank.query(advice.instruction_id)
        seen.append(advice.status)
    assert seen == [STATUS_PENDING, STATUS_PENDING, STATUS_SETTLED]
    assert advice.poll_count == 3
    assert advice.bank_reference, "settled 必须给出可对账的流水号"
    assert advice.value_date, "终态回单必须有起息日"


def test_terminal_advice_never_changes_again():
    """终态是终态：已经 settled 的再问一次还是同一份回单。"""
    bank = MockBank(settle_after=1)
    advice = bank.pay(_instruction())
    first = bank.query(advice.instruction_id)
    assert first.status == STATUS_SETTLED
    again = bank.query(advice.instruction_id)
    assert again == first, "终态回单不该因为多问一次而变"


@pytest.mark.parametrize("scripted", [STATUS_FAILED, STATUS_UNKNOWN, STATUS_PENDING])
def test_scripted_outcomes_are_deterministic(scripted):
    """脚本注入按 invoice_id 命中，确定性回放，不依赖随机数。"""
    bank = MockBank(settle_after=1, script={"INV-9": scripted})
    advice = bank.pay(_instruction(invoice_id="INV-9"))
    got = bank.query(advice.instruction_id)
    assert got.status == scripted
    assert got.is_terminal is (scripted in TERMINAL_STATUSES)
    if scripted != STATUS_SETTLED:
        assert not got.bank_reference, "只有 settled 才有流水号"


def test_stuck_bank_never_reaches_terminal_within_the_poll_budget():
    """失败路径的靶子：银行要 99 次，预算 3 次 —— **一定**问不出终态。"""
    bank = MockBank(settle_after=99)
    advice = bank.pay(_instruction())
    for _ in range(3):
        advice = bank.query(advice.instruction_id)
    assert advice.is_terminal is False
    assert advice.status == STATUS_PENDING
    assert advice.poll_count == 3


def test_settle_after_zero_is_refused():
    """一发就终态的银行演不出观察，构造期就拦。"""
    with pytest.raises(ValueError, match="settle_after"):
        MockBank(settle_after=0)


# ------------------------------------------------------------------ 幂等
def test_same_key_same_params_returns_the_same_instruction():
    """幂等：参数一致的重发原样返回同一笔，不新建第二笔。"""
    bank = MockBank()
    first = bank.pay(_instruction())
    again = bank.pay(_instruction())
    assert again.instruction_id == first.instruction_id
    assert len(bank._ledger) == 1, "重发不该在账本上多出一笔"


def test_same_key_different_params_is_refused():
    """参数不一致 -> 抛。两种静默走法都错：收下会付出第二笔，丢弃会让调用方
    拿到一份与自己递进来的指令对不上的回单。"""
    bank = MockBank()
    bank.pay(_instruction())
    with pytest.raises(DuplicateInstruction, match="参数不同"):
        bank.pay(_instruction(amount="200.00"))


def test_empty_idempotency_key_is_refused():
    """没有幂等键就挡不住第二笔付款。"""
    bank = MockBank()
    with pytest.raises(ValueError, match="幂等键"):
        bank.pay(_instruction(idempotency_key=""))


def test_remittance_info_is_not_part_of_the_fingerprint():
    """附言改了不影响资金结果，不算参数不一致。"""
    bank = MockBank()
    first = bank.pay(_instruction(remittance_info="A"))
    again = bank.pay(_instruction(remittance_info="B"))
    assert again.instruction_id == first.instruction_id


def test_unknown_instruction_id_raises():
    bank = MockBank()
    with pytest.raises(LookupError):
        bank.query("bkins-nope")


# ------------------------------------------------------------ 码表接得住
def test_payment_means_code_is_validated_at_construction():
    """付款方式码不在 UNCL4461 内 -> 构造时就抛（BR-CL-16）。

    在构造时抛而不是等银行拒：那时候钱已经在路上了。
    """
    with pytest.raises(KeyError, match="不在 UNCL4461"):
        _instruction(payment_means_code="72")
    ok = _instruction(payment_means_code=ap_codes.CODE_CREDIT_TRANSFER)
    assert ok.payment_means_code == "30"


def test_advice_carries_the_official_code_name():
    """回单里带官方名称，读产物的人不必回码表里查 30 是什么意思。"""
    bank = MockBank()
    advice = bank.pay(_instruction()).as_dict()
    assert advice["payment_means_name"] == \
        ap_codes.require_code(ap_codes.LIST_PAYMENT_MEANS, "30").name


def test_unknown_advice_status_is_refused():
    with pytest.raises(ValueError, match="未知的回单状态"):
        BankAdvice(instruction_id="i", idempotency_key="k", status="paid-ish",
                   amount="1", currency="CNY", payment_means_code="30")


# ------------------------------------------- 不去撞第七道闸（最要紧的一条）
def test_advice_field_is_not_the_gateway_receipt_field():
    """本域的回单键名不许与第七道闸的触发键相同。

    这不是绕开闸，是**不去撞一道不该由本域触发的闸**：那道闸守的是退款域的
    支付宝码表，本来就不该认识银行回单。
    """
    assert ADVICE_FIELD != GATEWAY_RECEIPT_FIELD, (
        f"本域回单键名与第七道闸的触发键撞了（都是 {ADVICE_FIELD!r}）——"
        f"每一份应付产物都会被那道闸判成未知网关码")
    assert ADVICE_FIELD == "bank_advice"


def test_gateway_gate_stays_silent_on_ap_artifacts():
    """把一份真实形状的应付产物喂给第七道闸，它应当一条 finding 都不出。

    这条用例是上一条的运行时版本：键名改对了不等于闸真的不响 —— 闸的触发条件
    将来可能变宽，那时候要在这里当场知道。
    """
    bank = MockBank(settle_after=1)
    advice = bank.pay(_instruction())
    advice = bank.query(advice.instruction_id).as_dict()
    artifacts = [
        {"kind": "ap_bank_advice", "content": {ADVICE_FIELD: advice, "summary": "x"}},
        {"kind": "ap_payment_instruction",
         "content": {ADVICE_FIELD: advice, "summary": "y"}},
    ]
    findings = ReviewerGate._gate_gateway({"inputs": {}}, artifacts)
    assert findings == [], (
        f"第七道闸对应付产物出了 finding：{findings} —— 它查的是退款域的码表，"
        f"本域的回单不该进它的视野")


def test_gateway_gate_would_fire_if_we_used_the_wrong_field_name():
    """反面：真把回单挂到 `receipt` 上，那道闸立刻判 blocker。

    这条是上面两条的**变异检验**：它证明那两条不是恒真断言 —— 键名一旦写错，
    症状确实存在，而且很难从「应付流程走不完」这个表象反推回来。
    """
    bank = MockBank(settle_after=1)
    advice = bank.pay(_instruction())
    advice = bank.query(advice.instruction_id).as_dict()
    wrong = dict(advice)
    wrong["code"] = "BANK-OK"          # 银行侧的码，支付宝码表里当然没有
    artifacts = [{"kind": "ap_bank_advice",
                  "content": {GATEWAY_RECEIPT_FIELD: wrong, "summary": "x"}}]
    findings = ReviewerGate._gate_gateway({"inputs": {}}, artifacts)
    assert findings, "键名写成 receipt 时第七道闸必须响 —— 不响的话上面两条就是空断言"
    assert findings[0]["severity"] == "blocker"


# ------------------------------------------------------------ ToolPort 九要素
@pytest.mark.parametrize("port", AP_PORTS, ids=lambda p: p.name)
def test_ports_declare_all_nine_elements(port):
    """九要素一个都不许空着 —— failure_modes 与 security_boundary 尤其。

    它们不是文档，是评审时会被逐条对的东西（`maos/tools/port.py` 抬头）。
    """
    assert port.name and port.purpose and callable(port.entry)
    assert port.params_schema, f"{port.name} 没声明入参"
    assert port.returns_schema, f"{port.name} 没声明返回"
    assert len(port.failure_modes) >= 3, f"{port.name} 的失败模式声明太少"
    assert len(port.security_boundary) > 40, f"{port.name} 的安全边界写得太薄"
    assert port.owner, f"{port.name} 没有 owner"


def test_invoke_tool_leaves_an_audit_row():
    """调用一律经 invoke_tool —— 直接调就没有 ToolInvoked 审计行。"""
    from maos.core.store import SqliteStore

    store = SqliteStore()
    store.init_schema()
    bank = MockBank(settle_after=1)
    advice = invoke_tool(BANK_PAY_PORT, {"bank": bank, "instruction": _instruction()},
                         store=store, extras={"plan_id": "p", "task_id": "t"})
    invoke_tool(BANK_QUERY_PORT,
                {"bank": bank, "instruction_id": advice["instruction_id"]},
                store=store, extras={"plan_id": "p", "task_id": "t"})
    rows = [e for e in store.list_event_log("p") if e["event_type"] == "ToolInvoked"]
    assert [r["detail"]["tool"] for r in rows] == ["bank.pay", "bank.query"]
    assert all(r["detail"]["status"] == "ok" for r in rows)


def test_invoke_tool_records_failures_too():
    """工具抛异常时**先落审计再原样抛出** —— 失败也是需要被追溯的事实。"""
    from maos.core.store import SqliteStore

    store = SqliteStore()
    store.init_schema()
    with pytest.raises(LookupError):
        invoke_tool(BANK_QUERY_PORT,
                    {"bank": MockBank(), "instruction_id": "nope"},
                    store=store, extras={"plan_id": "p"})
    rows = [e for e in store.list_event_log("p") if e["event_type"] == "ToolInvoked"]
    assert len(rows) == 1 and rows[0]["detail"]["status"] == "failed"
    assert "LookupError" in rows[0]["detail"]["error"]


def test_default_settle_after_is_more_than_one():
    """缺省就要求问不止一次 —— 缺省值本身就是那条论证的一部分。"""
    assert DEFAULT_SETTLE_AFTER > 1
