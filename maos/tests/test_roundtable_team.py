"""圆桌名册与三个钩子（`maos/roundtable/team.py`）。

本文件钉住四件事：

1. **没模型不沉默**（R2）：`ScriptedModelClient` 视作没模型，五岗照样各发一条，
   说的就是事实卡本身，且一次 `complete()` 都不调。
2. **模型不许编数字**（R1）：假模型原样回显 → speech 里的数字 ⊆ facts 里的数字。
3. **房间是旁路**：某一岗发不出去，后面几岗照发，钩子自己不抛。
4. **名册零模型**：「你有什么 skill」的答案来自注册表里的 `SkillContract`。

🔴 模型一律**显式注入**。`maos/tests/conftest.py` 只清 `MATRIX_*`，不清 `MAOS_LLM_*` ——
这台机器 source 过密钥的 shell 里，任何无参 `select_model_client()` 都会真打网关。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from maos.flows import custom_case
from maos.ingress.router import _load_run_requests, preflight
from maos.model.client import ModelClient, ModelResponse, ScriptedModelClient
from maos.roundtable import TEAM_ORDER, TITLES, RefundRoundtable, StageReport
from maos.tests.test_roundtable_stages import _numbers

ROOT = Path(__file__).resolve().parents[2]
LEDGER = ROOT / "scenarios" / "custom" / "ledger.json"
ROUNDTABLE_DIR = ROOT / "maos" / "roundtable"
FACTS_HEAD = "【你手上的事实】\n"
SAID_HEAD = "\n\n【群里已有的发言】"


# --------------------------------------------------------------------------
# 假件
# --------------------------------------------------------------------------
class _EchoModel(ModelClient):
    """把 user 里的【你手上的事实】原样回显。

    非 `ScriptedModelClient`，所以 `Speaker` 认它是真模型 —— 这是「有模型」那条
    分支唯一的入口。回显而不是回一句固定话，是为了让 R1 的判据有东西可比。
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.model = "echo-1"

    def complete(self, *, system: str, user: str, tier: str) -> ModelResponse:
        self.calls.append({"system": system, "user": user, "tier": tier})
        body = user.split(FACTS_HEAD, 1)[-1].split(SAID_HEAD, 1)[0]
        return ModelResponse(text=body, model=self.model)


class _BrokenModel(ModelClient):
    model = "broken"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def complete(self, *, system: str, user: str, tier: str) -> ModelResponse:
        self.calls.append({"tier": tier})
        raise RuntimeError("模型网关返回 HTTP 502：bad gateway")


class _MuteModel(ModelClient):
    """回一句空话。不抛异常，所以 try/except 抓不到它 —— 空回答要单独判。"""

    model = "mute"

    def complete(self, *, system: str, user: str, tier: str) -> ModelResponse:
        return ModelResponse(text="   \n ", model=self.model)


class _Voice:
    def __init__(self, agent_id: str, said: list[tuple[str, str]], broken: bool) -> None:
        self.agent_id = agent_id
        self.title = TITLES.get(agent_id, agent_id)
        self.user_id = f"@maos-{agent_id.removeprefix('refund-')}:maos.local"
        self.own_identity = True
        self._said = said
        self._broken = broken

    def say(self, text: str) -> None:
        if self._broken:
            raise ConnectionError(f"{self.agent_id} 的号没进房间")
        self._said.append((self.agent_id, text))


class _Voices:
    """`VoiceSet` 假件：记下 `(agent_id, text)`，可指定某一岗 `say` 抛异常。"""

    def __init__(self, broken: str | None = None) -> None:
        self.said: list[tuple[str, str]] = []
        self.broken = broken

    def voice(self, agent_id: str) -> _Voice:
        return _Voice(agent_id, self.said, broken=agent_id == self.broken)


# --------------------------------------------------------------------------
# 语料
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def case() -> tuple[dict, dict, dict]:
    ledger = custom_case.load(str(LEDGER), require_case=False)
    payload = _load_run_requests().build_case(ledger, {
        "order_id": "ORD-2026-0001", "reason": "quality_defect", "amount": None,
        "requested_at": "2026-09-03T00:00:00+08:00"})
    return payload, preflight(payload), ledger


def _preflight(rt: RefundRoundtable, case: tuple[dict, dict, dict]) -> list[StageReport]:
    payload, checked, ledger = case
    return rt.on_preflight(payload=payload, checked=checked, ledger=ledger,
                           evidence=[], requested_by="@boss:maos.local")


# --------------------------------------------------------------------------
# R2：没模型不沉默、不发 {}
# --------------------------------------------------------------------------
def test_without_model_speech_equals_facts_and_is_not_spoken_by_model(
        case: tuple[dict, dict, dict]) -> None:
    model = ScriptedModelClient({})
    voices = _Voices()
    reports = _preflight(RefundRoundtable(model, voices), case)

    assert len(reports) == len(TEAM_ORDER)
    for report in reports:
        assert report.speech == report.facts
        assert report.spoken_by_model is False
        assert report.speech.strip() and report.speech.strip() != "{}"
    # 假模型连问都不该问一次：问了就会拿到字面量 "{}"，而那一行会被当成发言发进房间。
    assert model.calls == []
    assert [a for a, _ in voices.said] == list(TEAM_ORDER)


def test_broken_model_falls_back_to_facts_card_without_raising(
        case: tuple[dict, dict, dict]) -> None:
    model = _BrokenModel()
    reports = _preflight(RefundRoundtable(model, _Voices()), case)

    assert len(reports) == len(TEAM_ORDER)
    assert len(model.calls) == len(TEAM_ORDER), "五岗都该试过一次，失败的是网关不是调用"
    for report in reports:
        assert report.speech == report.facts
        assert report.spoken_by_model is False


def test_empty_model_answer_falls_back_to_facts_card(
        case: tuple[dict, dict, dict]) -> None:
    """空回答不抛异常，照发就是在房间里发一条空消息 —— 比不发更难查。"""
    reports = _preflight(RefundRoundtable(_MuteModel(), _Voices()), case)

    for report in reports:
        assert report.speech == report.facts
        assert report.spoken_by_model is False


# --------------------------------------------------------------------------
# R1：模型只复述，不编数字
# --------------------------------------------------------------------------
def test_echo_model_numbers_are_subset_of_facts(case: tuple[dict, dict, dict]) -> None:
    model = _EchoModel()
    reports = _preflight(RefundRoundtable(model, _Voices()), case)

    assert [c["tier"] for c in model.calls] == ["light"] * len(TEAM_ORDER)
    for report in reports:
        assert report.spoken_by_model is True
        extra = _numbers(report.speech) - _numbers(report.facts)
        assert not extra, f"{report.agent_id} 说出了事实卡里没有的数字：{extra}"


def test_speaker_prompt_carries_duty_and_no_fabrication_rule(
        case: tuple[dict, dict, dict]) -> None:
    """system 提示词里必须有这一岗自己的 duty，和那条「预演/观察/受理 原样保留」。"""
    model = _EchoModel()
    rt = RefundRoundtable(model, _Voices())
    reports = _preflight(rt, case)

    systems = {c["system"] for c in model.calls}
    assert len(systems) == len(TEAM_ORDER), "五岗的 system 各不相同：借的是各自的 identity"
    for call in model.calls:
        assert "『预演 / 观察 / 受理』" in call["system"]
        assert "一个数字都不许改" in call["system"]
    duties = {row["duty"] for row in rt.roster()}
    for report in reports:
        matched = [c for c in model.calls if report.title in c["system"]]
        assert matched, f"{report.agent_id} 的 system 里没有岗位名"
        assert any(d in matched[0]["system"] for d in duties)


# --------------------------------------------------------------------------
# 三个钩子
# --------------------------------------------------------------------------
def test_on_preflight_speaks_five_stages_in_team_order(
        case: tuple[dict, dict, dict]) -> None:
    voices = _Voices()
    reports = _preflight(RefundRoundtable(ScriptedModelClient({}), voices), case)

    assert [r.agent_id for r in reports] == list(TEAM_ORDER)
    assert [r.title for r in reports] == [TITLES[a] for a in TEAM_ORDER]
    assert [a for a, _ in voices.said] == list(TEAM_ORDER)


def test_voice_failure_does_not_stop_later_stages(case: tuple[dict, dict, dict]) -> None:
    """第 2 岗的号没进房间，后 3 岗照发 —— 房间是旁路，不是主路。"""
    voices = _Voices(broken=TEAM_ORDER[1])
    reports = _preflight(RefundRoundtable(ScriptedModelClient({}), voices), case)

    assert [r.agent_id for r in reports] == list(TEAM_ORDER)
    assert [a for a, _ in voices.said] == [a for a in TEAM_ORDER if a != TEAM_ORDER[1]]
    # 发不出去不等于没说过：报告里那一条照样在，/pending 与测试都还读得到。
    assert reports[1].speech.strip()


def test_on_execute_only_finance_speaks(case: tuple[dict, dict, dict]) -> None:
    from maos.tests.test_ingress_router import RESULT_SETTLED

    payload, _checked, _ledger = case
    voices = _Voices()
    reports = RefundRoundtable(ScriptedModelClient({}), voices).on_execute(
        payload=payload, result=RESULT_SETTLED, operator="@boss:maos.local")

    assert [r.agent_id for r in reports] == ["refund-finance"]
    assert [a for a, _ in voices.said] == ["refund-finance"]
    assert "6800.00" in reports[0].facts


def test_on_sheet_each_stage_speaks_exactly_once(case: tuple[dict, dict, dict]) -> None:
    payload, checked, ledger = case
    rows = [
        {"line": 2, "order_id": "ORD-2026-0001", "reason_raw": "质量问题",
         "payload": payload, "checked": checked, "error": None,
         "problems": [], "warnings": []},
        {"line": 3, "order_id": "ORD-9999", "reason_raw": "坏了", "payload": None,
         "checked": None, "error": "底账里没有订单 ORD-9999",
         "problems": ["订单号不存在"], "warnings": []},
    ]
    voices = _Voices()
    reports = RefundRoundtable(ScriptedModelClient({}), voices).on_sheet(
        rows=rows, ledger=ledger, requested_by="@boss:maos.local")

    assert [r.agent_id for r in reports] == list(TEAM_ORDER)
    assert [a for a, _ in voices.said] == list(TEAM_ORDER)
    assert len(voices.said) == len(TEAM_ORDER), "50 行 × 5 岗会把房间刷爆，汇总只说一次"


def test_hooks_do_not_raise_when_stage_facts_blow_up(
        case: tuple[dict, dict, dict]) -> None:
    """事实汇总炸了也不许把 router 的回帖带崩：照样五条，那一岗说自己出错了。"""
    _payload, checked, ledger = case
    voices = _Voices()
    reports = RefundRoundtable(ScriptedModelClient({}), voices).on_preflight(
        payload={}, checked=checked, ledger=ledger, evidence=[], requested_by="boss")

    assert [r.agent_id for r in reports] == list(TEAM_ORDER)
    assert any("失败" in r.speech for r in reports)


# --------------------------------------------------------------------------
# 名册
# --------------------------------------------------------------------------
def test_roster_lists_five_stages_and_marks_unloaded_skills(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from maos.tests.test_roundtable_stages import _hide

    _hide(monkeypatch, "refund.evidence_check", "refund.risk_screen")
    voices = _Voices()
    rows = RefundRoundtable(ScriptedModelClient({}), voices).roster()

    assert [r["agent_id"] for r in rows] == list(TEAM_ORDER)
    for row in rows:
        assert set(row) == {"agent_id", "title", "role", "duty", "user_id",
                            "own_identity", "skills"}
        assert row["duty"] and row["own_identity"] is True
        assert row["user_id"].startswith("@maos-")
        assert row["skills"] == sorted(row["skills"], key=lambda s: s["name"])

    by_id = {r["agent_id"]: r for r in rows}
    evidence = by_id["refund-evidence"]["skills"]
    assert evidence == [{"name": "refund.evidence_check", "version": "", "purpose": "未装载"}]

    policy = by_id["refund-policy"]["skills"]
    assert [s["name"] for s in policy] == ["policy.match"]
    assert policy[0]["version"] == "1.0.0"
    assert policy[0]["purpose"] and policy[0]["purpose"] != "未装载"


def test_roster_survives_a_voice_that_cannot_be_opened() -> None:
    """取不到发声面就按未接通列，不许把 `/team` 整条打掉。"""
    class _Dead:
        def voice(self, agent_id: str):
            raise ConnectionError("房间没接通")

    rows = RefundRoundtable(ScriptedModelClient({}), _Dead()).roster()
    assert [r["agent_id"] for r in rows] == list(TEAM_ORDER)
    assert all(r["user_id"] == "" and r["own_identity"] is False for r in rows)


# --------------------------------------------------------------------------
# 平台无关
# --------------------------------------------------------------------------
def test_roundtable_package_does_not_import_hiclaw() -> None:
    """「平台无关」就是这一条：本包一个 Matrix 依赖都不许有。

    连注释里提一句都算越界 —— 判据故意放宽不了，因为一旦有人图省事 import 了
    房间层，`maos/` 就把 AP 场景与 nio 一起拖了进来，而这个包的全部价值就是
    它能在没有任何服务在跑的情况下被测。
    """
    sources = sorted(ROUNDTABLE_DIR.glob("*.py"))
    assert len(sources) == 4, f"意料之外的文件：{[p.name for p in sources]}"
    for path in sources:
        text = path.read_text(encoding="utf-8")
        for banned in ("hiclaw", "ap_room", "matrix_bus", "import nio"):
            assert banned not in text, f"{path.name} 里出现了 {banned}"
