"""合议收口引擎（`maos/roundtable/verdict.py`）。

本文件钉住四件事：

1. **真值表六行的顺序**。顺序本身就是判断 —— 规则说不退，证据齐、风险低都不能把它
   翻成批准；缺件是可补的，不是拒绝。调换任何两行，这里就有一条红的。
2. **`blockers` 与 `recommend` 无关**。「建议批复」四个字不许把风险岗那几句话盖掉。
3. **禁止词（铁律 8）**。收口卡里不许出现那五个词，语料带进来也不行 ——
   判据引用 `FORBIDDEN_WORDS` 本身，将来加词自动跟上。
4. **算不清楚就不放行**。空 reports、某岗汇总失败、不足五岗，一律 `need_more`，
   而且一次都不许抛：`decide()` 抛异常等于五岗说完之后房间里一片安静。

用手搓的 `StageReport` 而不是跑真 skill：真值表要覆盖的六行里有四行在现有演示语料上
根本走不到（那五单全是缺件），拿真语料测就只测到了其中一行。真语料另有一条
`test_decide_on_a_real_preflight_round` 兜着，防的是 `stages.py` 改键名。

🔴 模型一律显式注入（契约 §6 R8）：`conftest.py` 不清 `MAOS_LLM_*`，无参
`select_model_client()` 在 source 过密钥的 shell 里会真打网关。
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from maos.flows import custom_case
from maos.ingress.router import _load_run_requests, preflight
from maos.model.client import ScriptedModelClient
from maos.roundtable import (
    ESCALATION,
    FORBIDDEN_WORDS,
    RECOMMENDS,
    TEAM_ORDER,
    TITLES,
    RefundRoundtable,
    StageReport,
    Verdict,
    decide,
)

ROOT = Path(__file__).resolve().parents[2]
LEDGER = ROOT / "scenarios" / "custom" / "ledger.json"

#: agent_id 的短名，只为让下面的用例读起来是一句话而不是一串横杠。
SHORT = {"intake": "refund-intake", "policy": "refund-policy",
         "evidence": "refund-evidence", "risk": "refund-risk",
         "finance": "refund-finance"}

#: 五岗全绿的一单：规则批准、证据齐、风险 low、核算跑通。
#: 每条用例只打自己关心的那一处补丁，别处保持绿 —— 否则「为什么是 need_more」
#: 会有好几个候选原因，而用例名只说得出一个。
GREEN: dict[str, dict] = {
    "refund-intake": {
        "order_id": "ORD-2026-0001", "sku": "SKU-A", "amount_paid": "6800.00",
        "amount_claimed": "6800.00", "over_paid": False, "evidence_count": 2},
    "refund-policy": {
        "decision": "approve", "deciding_rule": "AS-002@v1",
        "matched_rules": ["AS-001@v1", "AS-002@v1"], "elapsed_days": 3,
        "pinned_policy_version": 1, "approver_role": "supervisor",
        "why": "命中 AS-002@v1，7 天内质量问题可退"},
    "refund-evidence": {
        "verdict": "complete", "gaps": [], "required_kinds": ["image"], "min_count": 1,
        "items": [{"ok": True}, {"ok": True}], "consistency": [], "unmet": [],
        "requirement_source": "AS-002@v1", "invocation_id": "inv-evidence"},
    "refund-risk": {
        "level": "low", "score": 10, "reasons": [], "signals": {}, "invocation_id": "inv-risk"},
    "refund-finance": {
        "preview_ran": True, "amount_approved": "6800.00", "breakdown": {},
        "rule_refs": ["AS-002@v1"], "policy_version": 1, "error": None},
}


def _report(agent_id: str, data: dict) -> StageReport:
    return StageReport(agent_id=agent_id, title=TITLES[agent_id], facts="事实卡",
                       speech="事实卡", data=data, spoken_by_model=False)


def _five(**patch: dict | None) -> list[StageReport]:
    """五岗 reports。`_five(policy={"decision": "reject"})` 只改那一处；
    传 `None`（`_five(finance=None)`）表示**那一岗缺席**，用来测「不足五条」。"""
    reports = []
    for short, agent_id in SHORT.items():
        if short in patch and patch[short] is None:
            continue
        data = copy.deepcopy(GREEN[agent_id])
        data.update(patch.get(short) or {})
        reports.append(_report(agent_id, data))
    return reports


def _blob(verdict: Verdict) -> str:
    """收口卡上所有会进房间的文字。禁止词与措辞的判据都从这里取。"""
    return "\n".join([verdict.headline, *verdict.reasons, *verdict.blockers])


# --------------------------------------------------------------------------
# 真值表六行，自上而下第一条命中为准
# --------------------------------------------------------------------------
def test_row1_policy_reject_is_not_overturned_by_complete_evidence_and_low_risk() -> None:
    """第 1 行：规则说不退就是不退。

    证据齐、风险 low —— 这两条都指向「可以放心批」，但它们是**观察**，
    而 reject 是**裁定**。拿观察去改裁定，就是这个项目最不该有的那种 bug。
    """
    verdict = decide(_five(policy={"decision": "reject", "why": "AS-001@v1 超窗 58 天"}))

    assert verdict.recommend == "reject"
    assert verdict.headline == "不建议批复 · AS-001@v1 超窗 58 天"
    assert verdict.next_command.startswith("/reject ")


def test_row1_beats_row2_when_finance_also_cannot_compute() -> None:
    """真值表顺序：驳回单本来就不做核算预演，`preview_ran=False` 是它的正常形态。

    第 2 行排在第 1 行前面的话，每一张驳回单都会被说成「需补件后再议」——
    而它需要的不是补件，是被拒绝。
    """
    verdict = decide(_five(policy={"decision": "reject", "why": "超窗"},
                           finance={"preview_ran": False, "amount_approved": None}))

    assert verdict.recommend == "reject"


def test_row2_finance_preview_not_ran_is_need_more() -> None:
    """第 2 行：算不出金额就不能让人拍板 —— 拍板意味着按某个数字付钱。"""
    verdict = decide(_five(finance={"preview_ran": False, "amount_approved": None}))

    assert verdict.recommend == "need_more"
    assert verdict.amount_preview == ""


def test_row2_finance_error_is_need_more_and_shows_up_as_a_blocker() -> None:
    verdict = decide(_five(finance={"error": "finance.settle: 库里没有这一单"}))

    assert verdict.recommend == "need_more"
    assert "核算：finance.settle: 库里没有这一单" in verdict.blockers


def test_row3_evidence_missing_with_gaps_is_need_more_not_reject() -> None:
    """第 3 行：缺件是可补的，不是拒绝。说成 reject，客户就白白丢了一次退款。"""
    verdict = decide(_five(evidence={"verdict": "missing",
                                     "gaps": ["缺少 image 类证据"]}))

    assert verdict.recommend == "need_more"
    assert verdict.headline == "需补件后再议 · 证据：缺少 image 类证据"
    assert verdict.next_command == "补齐材料后重发 /refund ORD-2026-0001 <诉求类型>"


def test_row3_evidence_unavailable_without_gaps_does_not_hit() -> None:
    """skill 未装载时 data 只有 `{"verdict": "unavailable"}`，**没有 gaps**。

    第 3 行要 gaps 非空才命中 —— 「核验不了」和「缺哪几份」是两件事，
    没有缺口清单就说「补件后再议」，等于让人去补一份没人说得出名字的材料。
    """
    verdict = decide(_five(evidence={"verdict": "unavailable", "gaps": []}))

    assert verdict.recommend == "approve", "证据核验不了不该单独把一单卡住，由别的行决定"


def test_row3_evidence_unavailable_with_gaps_is_need_more() -> None:
    verdict = decide(_five(evidence={"verdict": "unavailable", "gaps": ["核验器没跑通"]}))

    assert verdict.recommend == "need_more"


def test_row4_high_risk_escalates_instead_of_rejecting() -> None:
    """第 4 行：风险岗只提示不裁定，所以它把结论推高一档、不推翻。"""
    verdict = decide(_five(risk={"level": "high", "score": 100,
                                 "reasons": ["同一单已有一笔退款记录"]}))

    assert verdict.recommend == "escalate"
    assert verdict.headline == "建议升级审批 · 风险 high · 请 finance_manager 复核"
    assert verdict.next_command.startswith("/approve ")


def test_row5_all_green_is_approve() -> None:
    verdict = decide(_five(), case_id="RC-ORD-2026-0001")

    assert verdict.recommend == "approve"
    assert verdict.headline == "建议批复 · 核准预演 6800.00 · 请 supervisor 拍板"
    assert verdict.next_command == "/approve RC-ORD-2026-0001"
    assert verdict.blockers == []


def test_row6_missing_decision_falls_back_to_need_more() -> None:
    """第 6 行：默认不放行。规则审核岗没给裁定，就没有人说过这一单该退 ——
    这时候沉默地放行比说一句「我拿不准」危险得多（铁律 8）。"""
    verdict = decide(_five(policy={"decision": None, "why": None}))

    assert verdict.recommend == "need_more"


def test_row6_incomplete_reports_never_reach_approve() -> None:
    """五岗到齐才算「五岗全绿」。少一岗就没有人替那一岗说过话。

    这条防的是一种很静的错：`on_preflight` 整轮失败会返回 `[]`，调用方若只传了
    前几岗，第 5 行按 `policy.decision == "approve"` 就能一路走到「建议批复」。
    """
    for absent in SHORT:
        verdict = decide(_five(**{absent: None}))
        assert verdict.recommend == "need_more", f"缺了 {absent} 岗竟然还能放行"


# --------------------------------------------------------------------------
# blockers：与 recommend 无关，命中即列
# --------------------------------------------------------------------------
def test_blockers_are_listed_even_when_recommend_is_approve() -> None:
    """风险 medium 不改建议，但必须写在卡上 —— 批复建议里也要能看见风险。"""
    verdict = decide(_five(risk={"level": "medium", "score": 30,
                                 "reasons": ["近 30 天已有 2 笔退款记录，频率偏高"]}))

    assert verdict.recommend == "approve"
    assert verdict.blockers == ["风险：近 30 天已有 2 笔退款记录，频率偏高"]
    assert verdict.approver_role == "supervisor", "medium 不升档，升档只在 escalate"


def test_low_risk_reasons_are_not_blockers() -> None:
    """low 档的命中信号是背景说明，不是拦路条 —— 每单都列会把真的拦路条淹掉。"""
    verdict = decide(_five(risk={"level": "low", "score": 10,
                                 "reasons": ["同一客户名下有 3 笔订单快照"]}))

    assert verdict.blockers == []


def test_stage_summary_failure_becomes_a_blocker_and_a_reason() -> None:
    """`_round` 兜底写下的 `{"error": ...}`：卡上要既有拦路条又有那一岗的依据行。"""
    verdict = decide(_five(risk={"error": "KeyError: 'order_snapshot'"}))

    assert verdict.recommend == "need_more"
    assert "风险反欺诈岗事实汇总失败：KeyError: 'order_snapshot'" in verdict.blockers
    assert any("风险反欺诈岗：事实汇总失败" in r for r in verdict.reasons)


def test_finance_error_is_not_listed_twice() -> None:
    """财务岗的 error 归「核算：」前缀，不再按「某某岗事实汇总失败」列第二遍 ——
    同一个错误在卡上出现两次，人会以为是两件事。"""
    verdict = decide(_five(finance={"error": "finance.settle: 出参不是 dict"}))

    hits = [b for b in verdict.blockers if "finance.settle: 出参不是 dict" in b]
    assert len(hits) == 1
    assert hits[0].startswith("核算：")


# --------------------------------------------------------------------------
# 升档
# --------------------------------------------------------------------------
def test_escalation_upgrades_supervisor_to_finance_manager() -> None:
    verdict = decide(_five(risk={"level": "high", "score": 100, "reasons": ["重复退款"]}))

    assert verdict.approver_role == ESCALATION["supervisor"] == "finance_manager"


def test_escalation_at_top_tier_keeps_the_role_and_says_so() -> None:
    """已经是最高档就保持 —— 升到一个不存在的角色，房间里那句「请 X 拍板」点不到人。"""
    verdict = decide(_five(policy={"approver_role": "finance_manager"},
                           risk={"level": "high", "score": 100, "reasons": ["重复退款"]}))

    assert verdict.approver_role == "finance_manager"
    assert "风险：已是最高审批档，建议二人复核" in verdict.blockers


def test_escalation_does_not_guess_an_unknown_approver_role() -> None:
    verdict = decide(_five(policy={"approver_role": "cfo"},
                           risk={"level": "high", "score": 100, "reasons": ["重复退款"]}))

    assert verdict.approver_role == "cfo"
    assert "风险：已是最高审批档，建议二人复核" not in verdict.blockers


def test_no_escalation_when_recommend_is_not_escalate() -> None:
    """风险 high 但证据缺件 -> 第 3 行先命中 need_more，这时不该升档：
    升档是「就差一个更高的人点头」，而这一单差的是材料。"""
    verdict = decide(_five(evidence={"verdict": "missing", "gaps": ["缺少 image 类证据"]},
                           risk={"level": "high", "score": 100, "reasons": ["重复退款"]}))

    assert verdict.recommend == "need_more"
    assert verdict.approver_role == "supervisor"


# --------------------------------------------------------------------------
# headline 四个模板逐字
# --------------------------------------------------------------------------
def test_headline_templates_are_verbatim() -> None:
    """四轨都按这四行断言，改一个字就要四轨一起改 —— 所以它在这里被钉死。"""
    cards = {
        "approve": decide(_five()),
        "reject": decide(_five(policy={"decision": "reject", "why": "超窗 58 天"})),
        "need_more": decide(_five(evidence={"verdict": "missing", "gaps": ["缺少 image 类证据"]})),
        "escalate": decide(_five(risk={"level": "high", "score": 100, "reasons": ["重复退款"]})),
    }

    assert sorted(cards) == sorted(RECOMMENDS)
    assert cards["approve"].headline == "建议批复 · 核准预演 6800.00 · 请 supervisor 拍板"
    assert cards["reject"].headline == "不建议批复 · 超窗 58 天"
    assert cards["need_more"].headline == "需补件后再议 · 证据：缺少 image 类证据"
    assert cards["escalate"].headline == "建议升级审批 · 风险 high · 请 finance_manager 复核"
    for recommend, verdict in cards.items():
        assert verdict.recommend == recommend


def test_need_more_headline_without_any_blocker() -> None:
    """policy 缺 decision 时没有任何拦路条，headline 也不许留半句空话。"""
    verdict = decide(_five(policy={"decision": ""}))

    assert verdict.headline == "需补件后再议 · 缺少可核算的事实"


def test_reject_headline_without_a_reason_from_policy() -> None:
    verdict = decide(_five(policy={"decision": "reject", "why": None}))

    assert verdict.headline == "不建议批复 · 规则审核岗未给判定理由"


# --------------------------------------------------------------------------
# 铁律 8：禁止词
# --------------------------------------------------------------------------
def test_forbidden_words_never_appear_on_a_normal_card() -> None:
    for verdict in (decide(_five()),
                    decide(_five(policy={"decision": "reject", "why": "超窗"})),
                    decide(_five(risk={"level": "high", "score": 100, "reasons": ["重复退款"]})),
                    decide(_five(evidence={"verdict": "missing", "gaps": ["缺少 image"]}))):
        for word in FORBIDDEN_WORDS:
            assert word not in _blob(verdict), f"收口卡上出现了 {word}"


def test_forbidden_words_from_the_stage_data_are_rewritten_not_echoed() -> None:
    """禁止词最可能的来源不是我们自己写的模板，而是 skill 的出参。

    风险岗观察到「外部系统已退款」是**事实汇总**，不算 MAOS 在宣布终态；但把它
    原样抄进给 boss 的批复建议卡，卡上就出现了一句替外部系统宣布的话。所以
    收口卡改写、`seats` 里的原文一个字不动 —— 观察留着，宣布去掉。
    """
    verdict = decide(_five(
        policy={"decision": "reject", "why": "本单已批准过一次，不再受理"},
        evidence={"verdict": "missing", "gaps": ["附件里的回单显示已到账，与申请不符"]},
        risk={"level": "high", "score": 100, "reasons": ["同一单已退款且已放款"]},
        finance={"error": "finance.settle: 上一笔已完成，拒绝重复核算"},
    ))

    blob = _blob(verdict)
    for word in FORBIDDEN_WORDS:
        assert word not in blob, f"收口卡上出现了 {word}"
    # 原文没丢：五岗的 data 原样在 seats 里，回头核对得到。
    assert "已批准" in verdict.seats["refund-policy"]["why"]
    assert "已到账" in verdict.seats["refund-evidence"]["gaps"][0]
    assert "已退款" in verdict.seats["refund-risk"]["reasons"][0]
    assert "已完成" in verdict.seats["refund-finance"]["error"]


# --------------------------------------------------------------------------
# 可复现 / 吃得下残缺输入
# --------------------------------------------------------------------------
def test_decide_is_reproducible() -> None:
    """同一份 reports 连跑两次逐字一致 —— 它是 boss 唯一能信的那一行，
    必须能被复现、能被审计。读时钟、读 env、调模型，任意一条都会让这里红。"""
    reports = _five(risk={"level": "high", "score": 100, "reasons": ["重复退款"]})

    first = decide(reports, case_id="RC-1")
    second = decide(reports, case_id="RC-1")

    assert first == second
    assert first.blockers == second.blockers, "blockers 是列表，升档时会往里追加"


def test_decide_never_raises_on_broken_input() -> None:
    """空 reports、某岗汇总失败、data 是空 dict —— 三种都不许抛。

    抛了就是房间里五岗说完之后一片安静，而 boss 正在等一句话。
    """
    broken = [
        [],
        _five(evidence={"error": "RuntimeError: 核验器挂了"}),
        [_report(a, {}) for a in TEAM_ORDER],
        [_report("refund-policy", {"decision": "approve"})],
    ]
    for reports in broken:
        verdict = decide(reports, case_id="RC-X")
        assert verdict.recommend in RECOMMENDS
        assert verdict.headline.strip()
        assert verdict.next_command.strip()


def test_decide_tolerates_things_that_are_not_stage_reports() -> None:
    """`data` 不是 dict、`agent_id` 缺失的假件都当缺席算，不许在 seats 里炸。"""
    class _Odd:
        agent_id = "refund-policy"
        data = "这不是 dict"

    class _Nameless:
        agent_id = ""
        data = {"decision": "approve"}

    verdict = decide([_Odd(), None, _Nameless()])

    assert verdict.recommend == "need_more"
    assert verdict.seats == {"refund-policy": {}}, "没名字的那条不占座，None 也不占"


def test_empty_reports_still_produce_a_usable_card() -> None:
    verdict = decide([])

    assert verdict.recommend == "need_more"
    assert verdict.headline == "需补件后再议 · 缺少可核算的事实"
    assert verdict.reasons == [] and verdict.blockers == []
    assert verdict.case_id == "" and verdict.amount_preview == ""


# --------------------------------------------------------------------------
# 字段形状（T91/T92/T93 三轨按它写桩）
# --------------------------------------------------------------------------
def test_reasons_follow_team_order_and_are_prefixed_by_title() -> None:
    verdict = decide(_five())

    assert len(verdict.reasons) == len(TEAM_ORDER)
    for reason, agent_id in zip(verdict.reasons, TEAM_ORDER, strict=True):
        assert reason.startswith(f"{TITLES[agent_id]}：")


def test_seats_keep_every_stage_data_verbatim() -> None:
    """`seats` 是可追溯面：卡上每一个数字都该在这里找得到出处（R1）。"""
    reports = _five()
    verdict = decide(reports)

    assert sorted(verdict.seats) == sorted(TEAM_ORDER)
    for report in reports:
        assert verdict.seats[report.agent_id] is report.data


def test_next_command_matches_the_recommend() -> None:
    assert decide(_five(), case_id="RC-9").next_command == "/approve RC-9"
    assert decide(_five(risk={"level": "high", "score": 100, "reasons": ["x"]}),
                  case_id="RC-9").next_command == "/approve RC-9"
    assert decide(_five(policy={"decision": "reject", "why": "超窗"}),
                  case_id="RC-9").next_command == "/reject RC-9 <理由>"
    assert decide(_five(evidence={"verdict": "missing", "gaps": ["缺件"]}),
                  case_id="RC-9").next_command == "补齐材料后重发 /refund ORD-2026-0001 <诉求类型>"


def test_amount_preview_is_the_finance_string_verbatim() -> None:
    assert decide(_five()).amount_preview == "6800.00"
    assert decide(_five(finance={"amount_approved": None})).amount_preview == ""
    assert decide(_five(finance={"amount_approved": 6800})).amount_preview == "6800"


def test_verdict_is_frozen() -> None:
    """收口卡定下来就不许再改 —— 谁改了它，boss 看到的就不是 `decide()` 的结论。"""
    verdict = decide(_five())

    with pytest.raises(Exception):
        verdict.recommend = "approve"        # type: ignore[misc]


# --------------------------------------------------------------------------
# 真语料：防 stages.py 改键名
# --------------------------------------------------------------------------
def test_decide_on_a_real_preflight_round() -> None:
    """走一遍真的 `on_preflight`，防的是「`stages.py` 改了键名而 `decide()` 不知道」。

    现有演示语料喂不进随案证据，五单的证据核验全是 `missing` —— 所以这里断言的是
    `need_more`，而不是「演示不好看」。让演示能演出 approve 是另一轨的活。
    """
    ledger = custom_case.load(str(LEDGER), require_case=False)
    payload = _load_run_requests().build_case(ledger, {
        "order_id": "ORD-2026-0001", "reason": "quality_defect", "amount": None,
        "requested_at": "2026-09-03T00:00:00+08:00"})
    checked = preflight(payload)

    class _Voices:
        def voice(self, agent_id: str):
            return type("_V", (), {"say": staticmethod(lambda text: None)})()

    roundtable = RefundRoundtable(ScriptedModelClient({}), _Voices())
    reports = roundtable.on_preflight(payload=payload, checked=checked, ledger=ledger,
                                      evidence=[], requested_by="@boss:maos.local")
    verdict = roundtable.verdict_of(reports, checked)

    assert verdict.case_id == checked["case_id"]
    assert verdict.recommend == "need_more"
    assert sorted(verdict.seats) == sorted(TEAM_ORDER)
    assert len(verdict.reasons) == len(TEAM_ORDER)
    assert any(b.startswith("证据：") for b in verdict.blockers)
    for word in FORBIDDEN_WORDS:
        assert word not in _blob(verdict)
