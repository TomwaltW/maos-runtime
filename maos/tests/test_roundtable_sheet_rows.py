"""读表那一轮，下游三岗**逐单真跑 skill**，以及缺材料时挂在发言后面的按钮。

2026-09-10 的房间：一张 12 行的表拖进去，受理岗与规则岗说得出逐行结论，证据 /
风险 / 财务三岗的事实卡却各只有一行计数（「进入证据核验范围的有 8 单」）——
于是模型要么编出 8 单的逐单结论（真模型 8 轮里 18 轮在编），要么止血之后念一句
空话，风险岗说「证据岗还没出逐单结论，我这边也不替它说」，财务岗跟着说「两岗都
还没出结论」。三岗排队说废话，而三个 skill 都是确定性代码、整表跑完 35 ms。

本文件钉四件事：

1. **三岗逐单**：证据岗逐单说缺什么，风险岗点名中高风险，财务岗逐单预演并给整表合计。
2. **口径同源**：整表卡的缺口措辞与单案卡逐字同一份；`ledger` 带默认值，单参照样能调。
3. **按钮长在数据上**：`material_gaps` 是结构化的，圆桌按它生成 `Action`；没配上传
   地址一个按钮都没有；老嘴（只有 `say`）收到的是文字行；按钮不进 speech、不进 history。
4. **合计有条件**：一单预演没跑通，整表合计就不出（字段归属表：所有单预演完才允许发）。

零模型（`ScriptedModelClient` 视作没模型，五岗发的就是事实卡）、零 Matrix。
"""

from __future__ import annotations

import pytest

from maos.model.client import ScriptedModelClient
from maos.roundtable import stages
from maos.roundtable.team import MAX_ACTIONS, Action, RefundRoundtable, actions_text
from maos.tests.test_roundtable_stages import CASE_ID, ORDER, REQUESTED_AT, _case, ledger  # noqa: F401

SETTLED_HISTORY = {
    "tenant_id": "tnt-demo", "case_id": f"RC-{ORDER}-H9", "order_id": ORDER,
    "customer_id": "CUS-2026-0042", "amount": 6800.0, "status": "settled",
    "decided_at": "2026-08-20T10:00:00+00:00",
}


# --------------------------------------------------------------------------
# 语料：真底账 + 真 build_case + 真 preflight
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def quality(ledger: dict) -> tuple[dict, dict]:
    """质量问题 → 批准；表里没带证据 → 证据岗判 missing。"""
    return _case(ledger, "quality_defect")


@pytest.fixture(scope="module")
def no_reason_ok(ledger: dict) -> tuple[dict, dict]:
    """七天无理由、第 9 天申请 → 批准；无理由退货不要求举证 → not_required。"""
    from maos.ingress.router import _load_run_requests, preflight

    payload = _load_run_requests().build_case(ledger, {
        "order_id": ORDER, "reason": "no_reason_return", "amount": None,
        "requested_at": "2026-07-10T00:00:00+08:00"})
    return payload, preflight(payload)


@pytest.fixture(scope="module")
def no_reason_late(ledger: dict) -> tuple[dict, dict]:
    """七天无理由、第 63 天才申请 → 驳回。"""
    return _case(ledger, "no_reason_return")


def _row(line: int, case: tuple[dict, dict], reason_raw: str) -> dict:
    payload, checked = case
    return {"line": line, "order_id": ORDER, "reason_raw": reason_raw, "payload": payload,
            "checked": checked, "error": None, "problems": [], "warnings": []}


@pytest.fixture
def rows(quality, no_reason_ok, no_reason_late) -> list[dict]:
    """第 2 行批准（要举证）、第 3 行批准（不要举证）、第 4 行驳回。"""
    return [_row(2, quality, "质量问题"), _row(3, no_reason_ok, "七天无理由"),
            _row(4, no_reason_late, "无理由")]


def _with_settled_history(ledger: dict) -> dict:
    """同一单已有一笔退成的记录 → `refund.risk_screen` 判重复退款、高风险。"""
    book = dict(ledger)
    book["refund_history"] = list(ledger.get("refund_history") or []) + [SETTLED_HISTORY]
    return book


# --------------------------------------------------------------------------
# 1. 三岗逐单
# --------------------------------------------------------------------------
def test_sheet_evidence_lists_each_row_that_lacks_material(rows, ledger) -> None:
    facts, data = stages.facts_sheet_evidence(rows, ledger)

    assert f"· 第 2 行 {ORDER}（质量问题）：缺少 image 类证据" in facts
    # 不要举证的单只计数、不点名；驳回的单连范围都不进。
    assert "第 3 行" not in facts and "第 4 行" not in facts
    assert "逐单核验：证据齐 0 单、缺材料 1 单、无需举证 1 单" in facts
    assert (data["evidence_gaps"], data["evidence_not_required"], data["evidence_complete"]) == (1, 1, 0)

    gaps = data["material_gaps"]
    assert len(gaps) == 1
    gap = gaps[0]
    assert (gap["order_id"], gap["case_id"], gap["line"]) == (ORDER, CASE_ID, 2)
    assert gap["kinds"] == ["image"] and gap["verdict"] == "missing"
    assert gap["reason_raw"] == "质量问题"
    # 行数还是行数：三岗共用的 `sheet_stats` 键一个都不许被本岗自己的键盖掉。
    assert data["total"] == 3


def test_sheet_gap_wording_is_the_same_as_the_single_case_card(quality, ledger) -> None:
    """整表卡的缺口那一句与单案卡「缺口：…」逐字同源 —— 两处各写一份的症状是
    「/refund 说缺照片、读表说证据不齐」。"""
    payload, checked = quality
    single, single_data = stages.facts_evidence(payload, checked, ledger)
    gap_line = next(ln for ln in single.splitlines() if ln.startswith("缺口："))
    wording = gap_line[len("缺口："):]
    assert wording, "单案语料本身要有缺口，否则这条测试什么都没钉住"

    sheet, _ = stages.facts_sheet_evidence([_row(2, quality, "质量问题")], ledger)
    assert f"· 第 2 行 {ORDER}（质量问题）：{wording}" in sheet
    # 单案卡的 data 也带同一个结构（行号为 None），文本不变。
    assert single_data["material_gaps"][0]["kinds"] == ["image"]
    assert single_data["material_gaps"][0]["line"] is None
    assert "material_gaps" not in single and "http" not in single


def test_single_case_card_has_no_gap_entry_once_the_evidence_is_complete(quality, ledger) -> None:
    payload, checked = quality
    fed = dict(payload)
    fed["customer_evidence"] = [{"evidence_id": "ev-01", "kind": "image",
                                 "uri": "maos-attachment://" + "a" * 64,
                                 "digest": "a" * 64, "source": "test"}]
    _facts, data = stages.facts_evidence(fed, checked, ledger)
    assert data["verdict"] == "complete"
    assert data["material_gaps"] == []


def test_sheet_risk_names_only_the_medium_and_high_rows(rows, ledger) -> None:
    calm, calm_data = stages.facts_sheet_risk(rows, ledger)
    assert "逐单筛查：低风险 3 单、中风险 0 单、高风险 0 单" in calm
    assert "· 第" not in calm, "低风险的单不点名 —— 12 行「低风险」在房间里是一堵墙"
    assert calm_data["flagged"] == []

    hot, hot_data = stages.facts_sheet_risk(rows, _with_settled_history(ledger))
    assert "高风险" in hot
    flagged = hot_data["flagged"]
    assert flagged and all(f["order_id"] == ORDER for f in flagged)
    assert all(f["level"] in ("medium", "high") for f in flagged)
    listed = [ln for ln in hot.splitlines() if ln.startswith("  · 第 ")]
    assert len(listed) == len(flagged)
    assert "重复退款" in hot
    assert hot_data["risk_low"] + hot_data["risk_medium"] + hot_data["risk_high"] == 3


def test_sheet_finance_previews_each_approved_row_and_gives_the_total(rows) -> None:
    facts, data = stages.facts_sheet_finance(rows)

    assert f"· 第 2 行 {ORDER}（质量问题）：6800.00" in facts
    assert f"· 第 3 行 {ORDER}（七天无理由）：6800.00" in facts
    assert "第 4 行" not in facts, "驳回的单不预演、不点名"
    assert "整表合计（2 单预演完）：13600.00" in facts
    assert stages.PREVIEW_WORDING in facts
    assert data["amount_total"] == "13600.00"
    assert [p["line"] for p in data["previews"]] == [2, 3]
    assert data["total"] == 3, "`total` 仍是行数，合计另有 `amount_total`"


def test_sheet_finance_withholds_the_total_when_a_preview_fails(rows, monkeypatch) -> None:
    """一单没跑通，整表合计就不出：合计一个没算完的数，群里会当成已经算完的账。"""
    real = stages.facts_finance_preview

    def flaky(payload, checked):
        _text, d = real(payload, checked)
        if str(payload.get("case", {}).get("reason_code")) == "quality_defect":
            return "", {**d, "preview_ran": False, "amount_approved": None,
                        "error": "finance.settle: 网关抽风"}
        return _text, d

    monkeypatch.setattr(stages, "facts_finance_preview", flaky)
    facts, data = stages.facts_sheet_finance(rows)

    assert "· 第 2 行" in facts and "预演没跑通（finance.settle: 网关抽风）" in facts
    assert f"· 第 3 行 {ORDER}（七天无理由）：6800.00" in facts
    assert "整表合计：有单预演没跑通，合计不出" in facts
    assert "13600" not in facts and "6800.00\n整表合计（" not in facts
    assert data["amount_total"] is None and data["preview_failed"] == 1


# --------------------------------------------------------------------------
# 2. 口径与签名
# --------------------------------------------------------------------------
def test_sheet_cards_still_accept_the_single_argument_signature(rows, ledger) -> None:
    """`ledger` 带默认值：老调用点单参照样能调，缺默认值就是 TypeError 落进 router
    只记 WARNING 的 except —— 整个圆桌静默哑掉、回帖照发，没有任何测试会红。"""
    with_book, _ = stages.facts_sheet_evidence(rows, ledger)
    without, _ = stages.facts_sheet_evidence(rows)
    assert with_book == without, "订单快照在 payload 里也有一份，不带底账结论不该变"

    risk, _ = stages.facts_sheet_risk(rows)
    assert "逐单筛查：" in risk


def test_downstream_lists_are_capped_with_their_own_tail(quality, ledger) -> None:
    """下游三岗的截断尾句**不指回申请表回帖**：那份回帖只有预检裁定，没有逐单的
    证据 / 风险 / 核算结论，指过去是把人指向一个不存在的东西。"""
    many = [_row(i, quality, "质量问题") for i in range(2, 2 + stages.ROW_CAP + 3)]
    facts, data = stages.facts_sheet_evidence(many, ledger)

    listed = [ln for ln in facts.splitlines() if ln.startswith("  · 第 ")]
    assert len(listed) == stages.ROW_CAP
    assert stages.DOWNSTREAM_TAIL in facts
    assert stages.REPLY_TAIL not in facts
    # 数据不截：按钮上限另由圆桌管（`MAX_ACTIONS`）。
    assert len(data["material_gaps"]) == stages.ROW_CAP + 3


def test_unloaded_skill_still_says_so_and_carries_no_gaps(rows, ledger, monkeypatch) -> None:
    from maos.skills import registry

    real = registry.get
    monkeypatch.setattr(registry, "get",
                        lambda name, version=None: None if name == stages.EVIDENCE_SKILL
                        else real(name, version))
    facts, data = stages.facts_sheet_evidence(rows, ledger)
    assert "证据核验 skill 未装载" in facts
    assert "· 第" not in facts and data["material_gaps"] == []


# --------------------------------------------------------------------------
# 3. 圆桌：底账下传、按钮
# --------------------------------------------------------------------------
class _Voice:
    """会挂按钮的嘴：记下 `(agent_id, text, actions)`。"""

    def __init__(self, agent_id: str, said: list) -> None:
        self.agent_id = agent_id
        self._said = said

    def say(self, text: str) -> None:
        self._said.append((self.agent_id, text, None))

    def say_with_actions(self, text: str, actions) -> None:
        self._said.append((self.agent_id, text, tuple(actions)))


class _OldVoice:
    """只有 `say` 的老嘴（冒烟脚本、测试假件的形状）：没有 `say_with_actions`。"""

    def __init__(self, agent_id: str, said: list) -> None:
        self.agent_id = agent_id
        self._said = said

    def say(self, text: str) -> None:
        self._said.append((self.agent_id, text, None))


class _Voices:
    def __init__(self, cls=_Voice) -> None:
        self.said: list = []
        self._cls = cls

    def voice(self, agent_id: str):
        return self._cls(agent_id, self.said)


def _link(gap: dict) -> str:
    return f"http://127.0.0.1:8787/upload?order={gap['order_id']}&kinds={','.join(gap['kinds'])}"


def _by_agent(said: list, agent_id: str):
    return next(entry for entry in said if entry[0] == agent_id)


def test_on_sheet_hands_the_ledger_to_the_downstream_cards(rows, ledger) -> None:
    """底账要真的传到风险岗手上：退款历史只在底账里，不传就永远是低风险。"""
    voices = _Voices()
    reports = RefundRoundtable(ScriptedModelClient({}), voices).on_sheet(
        rows=rows, ledger=_with_settled_history(ledger), requested_by="@boss:maos.local")
    risk = next(r for r in reports if r.agent_id == "refund-risk")
    assert "高风险" in risk.facts and risk.data["risk_high"] >= 1


def test_evidence_stage_gets_one_upload_button_per_order_that_lacks_material(rows, ledger) -> None:
    voices = _Voices()
    rt = RefundRoundtable(ScriptedModelClient({}), voices, upload_link=_link)
    reports = rt.on_sheet(rows=rows, ledger=ledger, requested_by="@boss:maos.local")

    evidence = next(r for r in reports if r.agent_id == "refund-evidence")
    assert evidence.actions == (Action(label=f"上传照片 · {ORDER}",
                                       url=f"http://127.0.0.1:8787/upload?order={ORDER}&kinds=image"),)
    _agent, text, actions = _by_agent(voices.said, "refund-evidence")
    assert actions == evidence.actions and text == evidence.speech
    # 按钮不进 speech、不进下一岗的上下文：URL 一个字都不该出现在任何一岗的话里。
    assert all("http" not in r.speech for r in reports)
    # 其余四岗没有按钮，走的是普通的 `say`。
    for agent_id in ("refund-intake", "refund-policy", "refund-risk", "refund-finance"):
        assert _by_agent(voices.said, agent_id)[2] is None
        assert next(r for r in reports if r.agent_id == agent_id).actions == ()


def test_an_old_voice_without_say_with_actions_gets_the_button_as_text(rows, ledger) -> None:
    voices = _Voices(_OldVoice)
    rt = RefundRoundtable(ScriptedModelClient({}), voices, upload_link=_link)
    reports = rt.on_sheet(rows=rows, ledger=ledger, requested_by="@boss:maos.local")

    evidence = next(r for r in reports if r.agent_id == "refund-evidence")
    _agent, text, actions = _by_agent(voices.said, "refund-evidence")
    assert actions is None, "老嘴只有 say"
    assert text == f"{evidence.speech}\n{actions_text(evidence.actions)}"
    assert text.endswith(f"📎 上传照片 · {ORDER}：http://127.0.0.1:8787/upload?order={ORDER}&kinds=image")
    assert "http" not in evidence.speech


def test_no_upload_link_means_no_buttons_at_all(rows, ledger) -> None:
    voices = _Voices()
    reports = RefundRoundtable(ScriptedModelClient({}), voices).on_sheet(
        rows=rows, ledger=ledger, requested_by="@boss:maos.local")
    assert all(r.actions == () for r in reports)
    assert all(actions is None for _a, _t, actions in voices.said)
    assert next(r for r in reports if r.agent_id == "refund-evidence").data["material_gaps"]


def test_a_failing_link_builder_only_drops_that_button(rows, ledger) -> None:
    def boom(gap: dict) -> str:
        raise RuntimeError("补件页没起")

    voices = _Voices()
    reports = RefundRoundtable(ScriptedModelClient({}), voices, upload_link=boom).on_sheet(
        rows=rows, ledger=ledger, requested_by="@boss:maos.local")
    evidence = next(r for r in reports if r.agent_id == "refund-evidence")
    assert evidence.actions == () and evidence.speech == evidence.facts
    assert len(voices.said) == 5


def test_buttons_are_capped_even_when_the_gaps_are_not(quality, ledger) -> None:
    many = [_row(i, quality, "质量问题") for i in range(2, 2 + MAX_ACTIONS + 4)]
    voices = _Voices()
    reports = RefundRoundtable(ScriptedModelClient({}), voices, upload_link=_link).on_sheet(
        rows=many, ledger=ledger, requested_by="@boss:maos.local")
    evidence = next(r for r in reports if r.agent_id == "refund-evidence")
    assert len(evidence.data["material_gaps"]) == MAX_ACTIONS + 4
    assert len(evidence.actions) == MAX_ACTIONS


def test_preflight_round_carries_a_button_when_the_single_case_lacks_material(quality, ledger) -> None:
    """单案预检那一轮同样挂按钮：`/refund` 起单、证据岗说缺照片，按钮就在它后面。"""
    payload, checked = quality
    voices = _Voices()
    reports = RefundRoundtable(ScriptedModelClient({}), voices, upload_link=_link).on_preflight(
        payload=payload, checked=checked, ledger=ledger, evidence=[],
        requested_by="@boss:maos.local")
    evidence = next(r for r in reports if r.agent_id == "refund-evidence")
    assert len(evidence.actions) == 1 and evidence.actions[0].label == f"上传照片 · {ORDER}"
    assert "缺口：缺少 image 类证据" in evidence.facts
