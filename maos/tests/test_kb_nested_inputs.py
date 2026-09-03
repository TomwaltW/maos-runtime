"""`_shared_inputs` 要取到嵌在载荷里的共享参数 —— 尤其是 `amount_claimed`。

## 症状不是「闸不触发」，是「闸按别人的钱数触发」

`guardrails._shared_inputs` 原先只扫 `task["inputs"]` 顶层。而漏排财务核算的计划里，
当前 case 的申报金额只剩受理那一步的 `case_seed` 里那一份（`kb.experiment._tasks`
的注释明确要求这个形状**不许为了迁就判据去改**）—— 于是 shared 里没有
`amount_claimed`。

关键在于「取不到」之后发生了什么：`suggested_tasks_from_docs` 是先抄历史 step 的
inputs、再用 shared 覆盖。`ORDER_FACT_FIELDS` 不含 `amount_claimed`（申报金额是客户
诉求，不是订单事实），所以历史那份金额没被丢掉，也没被覆盖 —— **它原样留在了建议
任务上**。第六道闸于是按历史 case 的钱数判当前这一单，正是 `_shared_inputs` 自己的
docstring 警告的那件事：「抄错一位数就是把闸绕过去」。

R5 里两段用同一个 `AMOUNT` 常量，历史与当前碰巧相等，症状被完全掩盖。本文件把两个
金额**拉开**：历史 3200（阈下）、当前 9000（阈上）。修前建议任务拿到 3200，第六道闸
一次都不触发，一笔 9000 的退款不经财务核算就放行。

## 口径：按字段名下潜，不按路径

与 `runtime.gate._claimed_amounts` 同一把尺（那条判据也扫同一片 inputs 树）。写死
`case_seed.amount_claimed` 这种路径，换个域、换个种子键名当场变死代码且没有症状。
"""

from __future__ import annotations

import pytest

from maos import kb
from maos.kb import guardrails
from maos.runtime.gate import DEFAULT_FINANCE_THRESHOLD, ReviewerGate

TENANT = "tnt-nested"
CASE = "case-nested-0001"

#: 历史案例的申报金额，**阈下**。抄到当前 case 上，第六道闸就闭嘴。
HISTORY_AMOUNT = 3200.0
#: 当前 case 的申报金额，**阈上**，且只挂在 `case_seed` 里 —— 漏排财务核算的计划
#: 就长这个样（见 `kb.experiment._tasks`）。
CURRENT_AMOUNT = 9000.0

assert HISTORY_AMOUNT < DEFAULT_FINANCE_THRESHOLD < CURRENT_AMOUNT, \
    "两个金额必须跨在阈值两侧，否则这组回归证明不了任何东西"


def _baseline_missing_finance() -> list[dict]:
    """漏排财务核算的四步计划：顶层一份 `amount_claimed` 都没有。"""
    shared = {"tenant_id": TENANT, "case_id": CASE, "biz_type": "refund"}
    return [
        {"task_id": "t-intake", "role": "refund_intake", "title": "受理",
         "inputs": {**shared, "step": "intake",
                    "case_seed": {"tenant_id": TENANT, "case_id": CASE,
                                  "channel_id": "ch-dealer-7", "order_id": "ord-n1",
                                  "order_version": 1, "sku": "SKU-N",
                                  "reason_code": "quality_defect",
                                  "amount_claimed": CURRENT_AMOUNT}},
         "risk_level": "L"},
        {"task_id": "t-policy", "role": "refund_policy", "title": "裁定",
         "inputs": dict(shared), "risk_level": "L"},
        {"task_id": "t-payment", "role": "refund_payment", "title": "发起退款",
         "inputs": {**shared, "gateway": "gw"}, "risk_level": "M"},
        {"task_id": "t-notify", "role": "refund_intake", "title": "通知",
         "inputs": {**shared, "step": "notify", "channel": "sms"}, "risk_level": "L"},
    ]


def _history_docs() -> list[dict]:
    """历史知识：财务核算那一步带着**历史那一单**的金额。"""
    body = guardrails.case_to_doc_body([
        {"role": "refund_finance", "title": "核算退款金额并写财务分录",
         "inputs": {"biz_type": "refund", "amount_claimed": HISTORY_AMOUNT},
         "risk_level": "M", "effect_risk": "H"},
    ])
    return [{"doc_id": "doc-hist", "score": 0.9, "kind": kb.KIND_HISTORY_CASE,
             "body": body}]


# ---------------------------------------------------------------------------
# 1. 本轨要买的东西：第六道闸对补出来的财务任务真的触发
# ---------------------------------------------------------------------------
def test_suggested_finance_task_carries_the_current_case_amount():
    """建议任务的申报金额必须是**当前 case** 的，不是历史文档里那一单的。

    修前：shared 取不到嵌套的金额 → 历史那份 3200 原样留在建议任务上。
    """
    merged, added = guardrails.apply_suggestions(
        _baseline_missing_finance(), _history_docs())

    assert len(added) == 1 and added[0]["role"] == "refund_finance"
    assert added[0]["inputs"]["amount_claimed"] == CURRENT_AMOUNT, (
        f"建议任务带的是 {added[0]['inputs'].get('amount_claimed')!r}，"
        f"当前 case 报的是 {CURRENT_AMOUNT} —— 历史案例的钱数被抄到了这一单上")
    assert len(merged) == 5


def test_finance_gate_triggers_on_the_suggested_task():
    """接上第六道闸的**任务级**判据实判一次：补出来的财务任务必须进闸的视野。

    这是本轨真正的验收面 —— 「建议任务带了金额」只是中间量，「闸对它开口」才是
    要买的东西。没有 finance_entry 的那一版必须判 blocker。
    """
    _, added = guardrails.apply_suggestions(
        _baseline_missing_finance(), _history_docs())
    task = added[0]

    fired = ReviewerGate._gate_finance_task(task, artifacts=[])
    assert len(fired) == 1 and fired[0]["severity"] == "blocker", (
        f"第六道闸对补出来的财务任务没开口（findings={fired}）—— "
        f"一笔 {CURRENT_AMOUNT} 的退款不经财务核算就放行了")
    assert str(CURRENT_AMOUNT) in fired[0]["message"]

    # 交得出凭据就该放行 —— 证明上面那条 blocker 判的是「缺凭据」，不是恒 blocker。
    ok = ReviewerGate._gate_finance_task(
        task, artifacts=[{"content": {"finance_entry": {"amount": CURRENT_AMOUNT}}}])
    assert ok == []


# ---------------------------------------------------------------------------
# 2. `_shared_inputs` 的口径
# ---------------------------------------------------------------------------
def test_shared_inputs_reaches_into_case_seed():
    """嵌在 `case_seed` 里的键要取得到，顶层已有的键不受影响。"""
    shared = guardrails._shared_inputs(_baseline_missing_finance())
    assert shared["amount_claimed"] == CURRENT_AMOUNT
    assert shared["channel_id"] == "ch-dealer-7", "channel_id 同样只在种子里"
    assert shared["tenant_id"] == TENANT and shared["case_id"] == CASE
    assert shared["biz_type"] == "refund"


def test_top_level_wins_over_nested():
    """顶层是任务自己声明的参数，比任何载荷内部的同名键更权威。

    顺序也不许影响结论：带嵌套的任务排在前面，取到的仍然是顶层那份。
    """
    baseline = [
        {"role": "refund_intake",
         "inputs": {"step": "intake", "biz_type": "refund",
                    "case_seed": {"amount_claimed": HISTORY_AMOUNT}}},
        {"role": "refund_finance",
         "inputs": {"biz_type": "refund", "amount_claimed": CURRENT_AMOUNT}},
    ]
    assert guardrails._shared_inputs(baseline)["amount_claimed"] == CURRENT_AMOUNT


def test_matched_key_is_not_descended_into():
    """命中的键不再往下潜：值本身是 dict，那是「金额解析不出」这一档。

    潜下去的话 `{"amount_claimed": {"amount_claimed": 3200}}` 会取出 3200，
    把一份自证不了的脏数据洗成一个阈下的干净数字 —— 与闸的口径相反
    （`_over_finance_threshold`：解析不出 = 触发）。
    """
    baseline = [{"role": "refund_intake",
                 "inputs": {"seed": {"amount_claimed": {"amount_claimed": HISTORY_AMOUNT}}}}]
    assert guardrails._shared_inputs(baseline)["amount_claimed"] == {
        "amount_claimed": HISTORY_AMOUNT}


def test_scan_depth_is_bounded():
    """inputs 是外部喂进来的 JSON，不许无限下潜。深到上限之外就当没有。"""
    deep: dict = {"amount_claimed": CURRENT_AMOUNT}
    for _ in range(guardrails.SHARED_SCAN_MAX_DEPTH + 2):
        deep = {"wrap": deep}
    assert "amount_claimed" not in guardrails._shared_inputs(
        [{"role": "refund_intake", "inputs": deep}])

    shallow: dict = {"amount_claimed": CURRENT_AMOUNT}
    for _ in range(guardrails.SHARED_SCAN_MAX_DEPTH - 1):
        shallow = {"wrap": shallow}
    assert guardrails._shared_inputs(
        [{"role": "refund_intake", "inputs": shallow}])["amount_claimed"] == CURRENT_AMOUNT


def test_scan_walks_lists():
    """多源信号是 list of dict，共享参数可能挂在其中一条上。"""
    baseline = [{"role": "refund_intake",
                 "inputs": {"signals": [{"source": "email"},
                                        {"source": "im", "channel_id": "ch-im-9"}]}}]
    assert guardrails._shared_inputs(baseline)["channel_id"] == "ch-im-9"


@pytest.mark.parametrize("inputs", [
    None, "不是 dict", 42, [],
    {"case_seed": None}, {"case_seed": "字符串"}, {"case_seed": []},
])
def test_malformed_inputs_never_raise(inputs):
    """形状怎么脏都不许抛 —— 规划期抛异常等于整条 plan 起不来。"""
    assert isinstance(guardrails._shared_inputs([{"role": "r", "inputs": inputs}]), dict)


def test_nested_scan_does_not_change_the_healthy_shape():
    """顶层齐全的计划，取到的东西与只扫顶层时逐字节相同（不引入新键）。"""
    baseline = [
        {"role": "refund_intake",
         "inputs": {"tenant_id": TENANT, "case_id": CASE, "biz_type": "refund",
                    "step": "intake"}},
        {"role": "refund_finance",
         "inputs": {"biz_type": "refund", "amount_claimed": CURRENT_AMOUNT}},
    ]
    assert guardrails._shared_inputs(baseline) == {
        "tenant_id": TENANT, "case_id": CASE, "biz_type": "refund",
        "amount_claimed": CURRENT_AMOUNT}
