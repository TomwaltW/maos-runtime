"""换渠道重试演在屏幕上 + Payment Agent 与第七道闸判据同源（Y-4）。

本文件守两件事，对应 `docs/BACKLOG.md` 的 `## task-X2` 第 2、4 条：

1. **演出来**（第 4 条 / §4.1）：手册 R2 的「网关返可重试错误码 → 换渠道 →
   仍失败 → 转人工」这条链路，在 `test_replan_gateway.py` 里跑得通，但在 X 轮结束时
   **没有任何场景把它演出来** —— 场景 7 走的是 `effect_risk=H` 那条 HITL 入口。
   现在它演在 `flows/scenario_7.py`：主渠道注 `40005`、备用渠道注 `ACQ.SYSTEM_ERROR`。

2. **判据同源**（第 4 条 / §4.2）：同一份网关回执原先被判两次 —— 第七道闸按四象限，
   `RefundPaymentAgent._open_questions` 按 `needs_compensation` 一个 bool。两处当时
   都对，但判据不同源，码表加一条码或改一个 `outcome`，只有闸会跟着变，那句话会
   悄悄漂。**这类漂没有症状**，所以只能靠测试钉住：本文件逐格断言 Agent 那句话里的
   `retriable` / `outcome` 与码表逐字一致，且分档用的是闸算出来的 `disposition`。

## 为什么 §4.1 那组跑 `drive()` 而不是 `run()`

`scenario_7.run()` 目前会撞在收口断言 `scenario_7.py:394`
（`comp["last_observed_state"] == UNOBSERVED`，实测 `failed`）上 —— 换渠道之后
`refund.compensate` 分不清「上一笔的下落」和「收口这一笔的下落」，根因与最小改法
见 `docs/BACKLOG.md` 的 `## task-Y4` 第 1 条，落在本轨白名单之外。
`drive()` 只跑不断言，本文件对它的产物自己下断言，不碰那 11 条收口断言一个字。
"""

from __future__ import annotations

import pytest

from maos.agents.refund.payment_agent import (
    _DISPOSITION_PHRASE,
    _UNKNOWN_CODE_PHRASE,
    RefundPaymentAgent,
)
from maos.core.control_plane import (
    GW_HUMAN_TERMINAL,
    GW_QUERY_FIRST,
    GW_QUERY_OR_HUMAN,
    GW_REPLAN_CHANNEL,
)
from maos.runtime.gate import ReviewerGate
from maos.tools.gateway_codes import (
    ALL_CODES,
    OUTCOME_FAILED,
    OUTCOME_SUCCESS,
    OUTCOME_UNKNOWN,
    lookup,
)

# 四象限 -> 期望的 disposition。这张表是**期望值**，不是实现的复制：
# 它照抄 gateway_codes.py 的模块 docstring（「四个象限各自对应一种处置」那一段），
# 闸和 Agent 谁改坏了都会在这里对不上。
QUADRANT_DISPOSITION = {
    (True, OUTCOME_FAILED): GW_REPLAN_CHANNEL,
    (True, OUTCOME_UNKNOWN): GW_QUERY_FIRST,
    (False, OUTCOME_FAILED): GW_HUMAN_TERMINAL,
    (False, OUTCOME_UNKNOWN): GW_QUERY_OR_HUMAN,
}


def _seen(code, *, settled=False, poll_count=3, observed_state="unknown",
          remedy="", needs_compensation=False) -> dict:
    """造一份 `payment.observe` 形状的输出。字段照 `payment_observe._out()`。"""
    return {
        "receipt": {} if code is None else {"code": code},
        "observed_state": observed_state,
        "poll_count": poll_count,
        "settled": settled,
        "needs_compensation": needs_compensation,
        "remedy": remedy,
        "source": "",
        "biz_status": "gateway_accepted",
        "invocation_id": "inv-test",
    }


def _one_code_per_quadrant() -> dict[tuple, str]:
    """每格取一个真码。从 ALL_CODES 现取而不是写死码值 —— 码表加码时自动覆盖到。"""
    picked: dict[tuple, str] = {}
    for code, entry in ALL_CODES.items():
        if entry.outcome == OUTCOME_SUCCESS:
            continue
        picked.setdefault((entry.retriable, entry.outcome), code)
    return picked


def _q(seen: dict) -> str:
    out = RefundPaymentAgent._open_questions(seen)
    assert len(out) == 1, f"非 settled 时应恰好挂一条 open_question，实际 {out}"
    return out[0]


# ===================================================================== §4.2
class TestOpenQuestionsSameSourceAsGate:
    """Agent 那句话的判据必须来自第七道闸，不是另写一套。"""

    def test_四象限每格都被真码覆盖到(self):
        picked = _one_code_per_quadrant()
        missing = set(QUADRANT_DISPOSITION) - set(picked)
        assert not missing, (
            f"码表里没有覆盖到这些象限：{sorted(missing)} —— "
            "四象限断言会静默漏测，先给码表补一条代表码")

    @pytest.mark.parametrize("quadrant,code", sorted(_one_code_per_quadrant().items()))
    def test_每格的措辞取自闸算出来的disposition(self, quadrant, code):
        """逐格：Agent 用的那句话 == `_DISPOSITION_PHRASE[闸给的 disposition]`。"""
        finding = ReviewerGate._gateway_finding(code)
        assert finding is not None
        assert finding["disposition"] == QUADRANT_DISPOSITION[quadrant], (
            f"{code} 落在象限 {quadrant}，闸判 {finding['disposition']}，"
            f"与 gateway_codes 模块 docstring 的四象限表对不上")

        text = _q(_seen(code))
        assert text.startswith(_DISPOSITION_PHRASE[finding["disposition"]]), (
            f"{code} 的措辞没走闸的 disposition：{text}")

    @pytest.mark.parametrize("code", sorted(c for c, e in ALL_CODES.items()
                                            if e.outcome != OUTCOME_SUCCESS))
    def test_措辞里的判据与码表逐字一致(self, code):
        """`retriable` / `outcome` 原样来自码表 —— 这是「不重写一套映射」的落点。"""
        entry = lookup(code)
        text = _q(_seen(code))
        assert f"code={entry.code}" in text
        assert f"retriable={entry.retriable}" in text
        assert f"outcome={entry.outcome}" in text
        if entry.remedy:
            assert entry.remedy in text, "官方 remedy 应原文带出，供人直接照做"

    def test_未知码不许被当成轮询到顶(self):
        """未知码与「retriable=False + unknown」共用 disposition，但理由完全不同。"""
        text = _q(_seen("ACQ.NO_SUCH_CODE_9999"))
        assert text.startswith(_UNKNOWN_CODE_PHRASE), text
        assert "retriable=None" in text and "outcome=None" in text
        assert "继续观察" not in text, (
            "未知码被当成了「轮询到顶，继续观察」—— 那是在替一张没查过的表下结论")

    def test_成功码与无码仍走轮询到顶那一句(self):
        """`_gateway_finding` 返回 None 的那一档：网关一个异常都没报。"""
        for seen in (_seen(None, observed_state="processing"),
                     _seen("10000", observed_state="processing")):
            text = _q(seen)
            assert "仍未取得终态" in text and "这不是失败，需继续观察" in text, text

    def test_settled之后不挂任何问题(self):
        assert RefundPaymentAgent._open_questions(
            _seen("10000", settled=True, observed_state="settled")) == []


# ===================================================================== §4.1
@pytest.fixture(scope="module")
def s7():
    """跑一遍场景 7 的失败路径。module 级：这条链路要跑满两轮付款，不便每条重跑。"""
    from maos.flows import scenario_7
    return scenario_7, scenario_7.drive()


class TestChannelSwitchIsOnScreen:
    """手册 R2 那一段真的演在场景 7 里，不只在 test_replan_gateway.py 里绿。"""

    def test_恰好换过一次渠道(self, s7):
        mod, out = s7
        assert out["replans"] == 1, (
            f"应恰好重规划 1 次（{mod.GATEWAY_RETRIABLE_CODE} 触发），实际 {out['replans']}")

    def test_两轮回执的码依次是可重试码与说不清的码(self, s7):
        """这条序列就是「撞可重试码 → 换渠道 → 再失败」的证据本身。"""
        mod, out = s7
        codes = [(a["version"], (a["content"].get("receipt") or {}).get("code"))
                 for a in out["store"].list_artifacts(mod.TASK_PAYMENT)
                 if a["kind"] == mod.KIND_PAYMENT_RECEIPT]
        assert [c for _, c in sorted(codes)] == [
            mod.GATEWAY_RETRIABLE_CODE, mod.GATEWAY_ERROR_CODE], sorted(codes)

    def test_第一个码可换渠道第二个码一票否决(self, s7):
        """两个码各自落在哪一格 —— 换个码就演不出这条链路，钉住它。"""
        mod, _ = s7
        first = ReviewerGate._gateway_finding(mod.GATEWAY_RETRIABLE_CODE)
        second = ReviewerGate._gateway_finding(mod.GATEWAY_ERROR_CODE)
        assert first["disposition"] == GW_REPLAN_CHANNEL, (
            "主渠道的码必须落在唯一允许换渠道的那一格，否则 replan 压根不会触发")
        assert second["disposition"] == GW_QUERY_FIRST, (
            "备用渠道的码必须是一票否决的那一格，否则会换第三个渠道 —— 那就是自旋")

    def test_收口那一笔落在换过去的备用渠道上(self, s7):
        mod, out = s7
        from maos.domain.refund import objects
        rows = objects.query(
            out["store"],
            "SELECT gateway FROM refund_request WHERE tenant_id=? AND case_id=?",
            (mod.TENANT_ID, mod.CASE_ID))
        assert [r["gateway"] for r in rows] == [mod.GATEWAY_BACKUP_NAME], (
            "refund_request 按 (tenant, case) 覆盖，收口时应只剩备用渠道那一笔")

    def test_换渠道没有吃掉轮询预算(self, s7):
        """派单预警的两条之一：换渠道消耗 replan 额度，不该动 poll_count。"""
        mod, out = s7
        assert out["receipt"]["poll_count"] == mod.MAX_POLLS

    def test_换渠道没有把任何一笔推到settled(self, s7):
        """派单预警的另一条：`40005` 是「确定没执行」，重发安全，但仍不许问出终态。"""
        mod, out = s7
        assert mod._count(
            out["store"],
            "SELECT COUNT(*) AS n FROM payment_observation WHERE observed_state='settled'"
        ) == 0

    def test_换渠道不换幂等键(self, s7):
        """两笔请求共用一个幂等键 —— 换渠道不会造出第二笔退款。"""
        mod, out = s7
        from maos.skills.builtin.refund.payment_execute import idempotency_key_of
        from maos.domain.refund import objects
        rows = objects.query(
            out["store"],
            "SELECT idempotency_key FROM refund_request WHERE tenant_id=? AND case_id=?",
            (mod.TENANT_ID, mod.CASE_ID))
        assert {r["idempotency_key"] for r in rows} == {
            idempotency_key_of(mod.TENANT_ID, mod.CASE_ID)}
