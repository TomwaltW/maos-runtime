"""复检那一轮的事实卡口径 —— 五岗得说得出「这是第二遍」。

一条消息触发一轮五岗；证据补进来之后会再触发一轮。第 2 轮如果与第 1 轮逐字相同、
只有数字变了，房间里的人看不出发生过什么 —— 他会以为机器人把同一单重放了一遍。
本文件钉住受理岗与证据岗为此多说的那两行，以及**多说不许说过头**：

1. **只报轮次与本轮新增份数**，不提上一轮判成什么。上一轮的结论根本不在入参里，
   说「证据补齐了」「已改判」「比上一轮更充分」就是编（R1 / 铁律 8）。
2. **首次预检输出逐字不变**（`round_no=1, added=0`，也是今天全部调用点的形态）。
   这是本文件唯一的回归闸：冒烟脚本走的全是首次预检，它的输出不该变一个字。
3. **两个新参都带默认值**。老调用点（`maos/ingress/router.py::_fire`、
   `scripts/room_team_smoke.py`）仍按五参调用，少一个默认值就是 `TypeError`
   落进 `_fire` 的 except —— 那条 except 只记 WARNING，症状是整个圆桌静默哑掉、
   回帖照发，没有任何测试会红。所以签名本身也要钉，不能只钉「调了不抛」。
4. **合计份数只有一个来源**：`payload["customer_evidence"]` 的长度。两处各取一个
   来源的症状是「受理岗说 2 份、证据岗说 3 份」，而两边都不报错。

零网络、零模型、零 Matrix：语料是真底账 + 真 `build_case` + 真 `preflight`，模型
一律**显式注入** `ScriptedModelClient`（口径同 `test_roundtable_team.py` 抬头 ——
这台机器 source 过密钥的 shell 里，无参 `select_model_client()` 会真打网关）。
"""

from __future__ import annotations

import inspect
import json
from decimal import Decimal
from pathlib import Path

import pytest

from maos.flows import custom_case
from maos.ingress.router import _load_run_requests, preflight
from maos.model.client import ScriptedModelClient
from maos.roundtable import TEAM_ORDER, RefundRoundtable, stages
from maos.tests.test_roundtable_stages import _allowed, _hide, _numbers
from maos.tests.test_roundtable_team import _Voices

ROOT = Path(__file__).resolve().parents[2]
LEDGER = ROOT / "scenarios" / "custom" / "ledger.json"
ORDER = "ORD-2026-0001"
REQUESTED_AT = "2026-09-03T00:00:00+08:00"
BOSS = "@boss:maos.local"

#: 上一轮的结论不在入参里，这几个字眼一个都不许出现在事实卡里（R1 / 铁律 8）。
BANNED = ("证据补齐", "已改判", "改判", "更充分", "上一轮的结论")


# --------------------------------------------------------------------------
# 语料
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def ledger() -> dict:
    return custom_case.load(str(LEDGER), require_case=False)


@pytest.fixture(scope="module")
def case(ledger: dict) -> tuple[dict, dict, dict]:
    """质量问题 → `decision == "approve"`，与 `test_roundtable_team.py` 同一单。"""
    payload = _load_run_requests().build_case(ledger, {
        "order_id": ORDER, "reason": "quality_defect", "amount": None,
        "requested_at": REQUESTED_AT})
    return payload, preflight(payload), ledger


def _with_evidence(payload: dict, count: int) -> dict:
    """复制一份 payload 并挂上 `count` 份随案证据。

    **复制**是因为 `case` 是 module 级夹具，就地改会把份数漏给同文件后面的用例。
    五个键的形状照 `maos/ingress/attachments.py::as_evidence`，少键会让
    `refund.evidence_check` 走别的分支 —— 那时测的就不是本轨改的那一行了。
    """
    out = json.loads(json.dumps(payload))
    out["customer_evidence"] = [
        {"evidence_id": f"ev-{i:02d}", "kind": "image",
         "uri": f"attach://t99/{i:02d}", "digest": f"sha256:{i:064d}",
         "source": "test:t99"}
        for i in range(1, count + 1)]
    return out


def _line_starting(facts: str, prefix: str) -> str:
    hit = [line for line in facts.splitlines() if line.startswith(prefix)]
    assert len(hit) == 1, f"以 {prefix!r} 开头的行有 {len(hit)} 行，期望恰好 1 行"
    return hit[0]


def _index_of(facts: str, prefix: str) -> int:
    lines = facts.splitlines()
    return next(i for i, line in enumerate(lines) if line.startswith(prefix))


# --------------------------------------------------------------------------
# 回归闸：首次预检一个字都不许变
# --------------------------------------------------------------------------
def test_first_round_facts_are_byte_identical_to_the_old_signature(
        case: tuple[dict, dict, dict]) -> None:
    """不传新参 == 传首轮那对缺省值，两串逐字节相同。

    冒烟脚本与 router 走的全是这一条路径。这里一旦不等，`room_team_smoke.py`
    的输出就会跟着变，而那份输出是要进 evidence/ 的。
    """
    payload, checked, ledger = case
    attached = _with_evidence(payload, 2)

    assert stages.facts_intake(payload, checked, 2)[0] == \
        stages.facts_intake(payload, checked, 2, 1)[0]
    assert stages.facts_evidence(attached, checked, ledger)[0] == \
        stages.facts_evidence(attached, checked, ledger, 0)[0]


def test_first_round_facts_carry_no_recheck_wording(
        case: tuple[dict, dict, dict]) -> None:
    """首轮不许出现「本单第 N 轮」「本轮新增」，也不许提上一轮的结论。"""
    payload, checked, ledger = case
    attached = _with_evidence(payload, 2)

    intake, intake_data = stages.facts_intake(attached, checked, 2)
    evidence, _ = stages.facts_evidence(attached, checked, ledger)

    assert "本单第" not in intake and "本轮新增" not in intake
    assert "本轮新增" not in evidence and "本单第" not in evidence
    assert intake_data["round_no"] == 1
    for text in (intake, evidence):
        for banned in BANNED:
            assert banned not in text


# --------------------------------------------------------------------------
# 跨轨契约 §3：两个新参必须带默认值
# --------------------------------------------------------------------------
def test_on_preflight_new_params_have_defaults() -> None:
    """签名本身就是契约。少一个默认值 = 圆桌静默哑掉，且没有别的测试会红。"""
    params = inspect.signature(RefundRoundtable.on_preflight).parameters

    for name, expected in (("round_no", 1), ("added_evidence", 0)):
        assert name in params, f"{name} 不在 on_preflight 的签名里"
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY
        assert params[name].default == expected, f"{name} 必须带默认值 {expected}"


def test_old_five_arg_call_still_speaks_five_stages(
        case: tuple[dict, dict, dict]) -> None:
    """T97 并入之前的调用形态：不传两个新参，五岗照样各说一条。"""
    payload, checked, ledger = case
    voices = _Voices()
    reports = RefundRoundtable(ScriptedModelClient({}), voices).on_preflight(
        payload=payload, checked=checked, ledger=ledger,
        evidence=[], requested_by=BOSS)

    assert [r.agent_id for r in reports] == list(TEAM_ORDER)
    assert [a for a, _ in voices.said] == list(TEAM_ORDER)


# --------------------------------------------------------------------------
# 受理岗：本单第几轮
# --------------------------------------------------------------------------
def test_second_round_intake_says_which_round(case: tuple[dict, dict, dict]) -> None:
    payload, checked, _ledger = case
    facts, data = stages.facts_intake(payload, checked, 2, 2)

    assert "本单第 2 轮（上一轮之后有新证据进来，按当前材料重新过一遍）" in facts
    assert data["round_no"] == 2
    # 紧跟在「随案证据：N 份」之后：那一行讲的就是材料，轮次接着它说才连得上。
    assert _index_of(facts, "本单第") == _index_of(facts, "随案证据：") + 1
    for banned in BANNED:
        assert banned not in facts


def test_round_line_introduces_exactly_one_new_number(
        case: tuple[dict, dict, dict]) -> None:
    """R1：多说这一行，只许多出「第几轮」那一个数字，别的一个都不许冒出来。"""
    payload, checked, _ledger = case
    base, _ = stages.facts_intake(payload, checked, 2)
    seventh, _ = stages.facts_intake(payload, checked, 2, 7)

    assert _numbers(seventh) - _numbers(base) == {Decimal(7)}
    allowed = _allowed(payload, checked, extra=(2, 7, len(checked["matched_rules"])))
    assert _numbers(seventh) <= allowed, f"多出来的数字：{_numbers(seventh) - allowed}"


# --------------------------------------------------------------------------
# 证据岗：本轮新增几份
# --------------------------------------------------------------------------
def test_added_evidence_line_counts_from_payload(
        case: tuple[dict, dict, dict]) -> None:
    payload, checked, ledger = case
    facts, data = stages.facts_evidence(_with_evidence(payload, 2), checked, ledger, 1)

    assert "本轮新增 1 份材料，随案证据合计 2 份" in facts
    assert data["added_evidence"] == 1
    # 排在「逐份核验」之前：先说这一轮多了什么，再逐份念，顺序反了读起来是倒叙。
    assert _index_of(facts, "本轮新增") == _index_of(facts, "逐份核验：") - 1
    for banned in BANNED:
        assert banned not in facts


def test_added_evidence_line_never_computes_a_third_number(
        case: tuple[dict, dict, dict]) -> None:
    """R1：新增 1 份、随案共 3 份，事实卡里只许出现 1 和 3。

    「上一轮有几份」= 3 - 1 = 2 是**推算**出来的，不是入参里的数 —— 而且推算前提
    「老 ticket 原本没有证据」本身就不成立。所以那一行里不许出现第三个数。
    """
    payload, checked, ledger = case
    facts, data = stages.facts_evidence(_with_evidence(payload, 3), checked, ledger, 1)

    line = _line_starting(facts, "本轮新增")
    assert line == "本轮新增 1 份材料，随案证据合计 3 份"
    assert _numbers(line) == {Decimal(1), Decimal(3)}
    assert data["added_evidence"] == 1


def test_no_added_line_when_evidence_skill_is_not_loaded(
        case: tuple[dict, dict, dict], monkeypatch: pytest.MonkeyPatch) -> None:
    """skill 没装载时连份数都核不了，报一句「本轮新增 1 份」会让人以为核过了。

    这条早返回的 `data` 形状也保持原样（只有 `verdict`）：没核验就不该留下一个
    看着像核过的计数，`test_roundtable_stages.py` 那条 `data == {"verdict": ...}`
    也钉着同一件事。
    """
    _hide(monkeypatch, "refund.evidence_check")
    payload, checked, ledger = case

    facts, data = stages.facts_evidence(_with_evidence(payload, 2), checked, ledger, 1)

    assert "未装载" in facts
    assert "本轮新增" not in facts
    assert data == {"verdict": "unavailable"}


# --------------------------------------------------------------------------
# 整轮：加参之后五岗仍然是五个
# --------------------------------------------------------------------------
def test_recheck_round_still_speaks_exactly_five_stages(
        case: tuple[dict, dict, dict]) -> None:
    """两个新参靠闭包穿到受理岗与证据岗，名册与发言条数一个不变。"""
    payload, checked, ledger = case
    attached = _with_evidence(payload, 2)
    voices = _Voices()
    reports = RefundRoundtable(ScriptedModelClient({}), voices).on_preflight(
        payload=attached, checked=checked, ledger=ledger,
        evidence=attached["customer_evidence"], requested_by=BOSS,
        round_no=2, added_evidence=1)

    assert [r.agent_id for r in reports] == list(TEAM_ORDER)
    assert [a for a, _ in voices.said] == list(TEAM_ORDER)

    seats = {r.agent_id: r for r in reports}
    assert "本单第 2 轮" in seats["refund-intake"].facts
    assert seats["refund-intake"].data["round_no"] == 2
    assert "本轮新增 1 份材料，随案证据合计 2 份" in seats["refund-evidence"].facts
    assert seats["refund-evidence"].data["added_evidence"] == 1
    # 另外三岗一个字都不该跟着变：本轨只动了两张事实卡。
    for agent_id in ("refund-policy", "refund-risk", "refund-finance"):
        assert "本单第" not in seats[agent_id].facts
        assert "本轮新增" not in seats[agent_id].facts


# --------------------------------------------------------------------------
# 防御性：怪数字不许把圆桌带崩
# --------------------------------------------------------------------------
@pytest.mark.parametrize("round_no", [0, -1, 10 ** 9])
def test_odd_round_numbers_do_not_raise(
        case: tuple[dict, dict, dict], round_no: int) -> None:
    """显示口径（本轨定的）：`round_no >= 2` 才多说那一行。

    `0` 与负数按首轮处理、一个字都不多说 —— 在这里夹一层「看着不对就改成 1」，
    症状是房间里的轮次和触发侧的账对不上，且两边都不报错。超大值照数字排版：
    数是触发侧数出来的，这一层不校验也不改写。
    """
    payload, checked, _ledger = case
    facts, data = stages.facts_intake(payload, checked, 2, round_no)

    assert data["round_no"] == round_no
    if round_no >= 2:
        assert f"本单第 {round_no} 轮" in facts
    else:
        assert "本单第" not in facts


@pytest.mark.parametrize("added", [0, -1, 10 ** 9])
def test_odd_added_counts_do_not_raise(
        case: tuple[dict, dict, dict], added: int) -> None:
    """显示口径同上：`added >= 1` 才多说那一行，`0` 与负数一个字都不多说。"""
    payload, checked, ledger = case
    facts, data = stages.facts_evidence(_with_evidence(payload, 2), checked, ledger, added)

    assert data["added_evidence"] == added
    if added >= 1:
        assert f"本轮新增 {added} 份材料，随案证据合计 2 份" in facts
    else:
        assert "本轮新增" not in facts
