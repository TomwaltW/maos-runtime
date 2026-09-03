"""T85 证据核验：skill 的两级判定与四态，Agent 的产物与失败姿态。

零 store 可跑 —— 本 skill 是纯函数，`SkillInvoker(identity, store=None)` 时 `_settle`
不落库。要比对逐字节一致的出参就直接调 `run()` 并给一个**固定的** `invocation_id`：
经 invoker 时 `invoker.py:91-93` 会用它自己生成的随机值覆盖 extras 里的同名键。
"""

from __future__ import annotations

import json

import pytest

from maos.agents.base import AGENT_POOL, AgentIdentity, PermissionDenied, TaskContext
from maos.agents.refund import REFUND_ROLES
from maos.agents.refund.evidence_agent import KIND_EVIDENCE_REPORT, RefundEvidenceAgent
from maos.model.client import ScriptedModelClient, Tier
from maos.skills import registry
from maos.skills.builtin.refund.evidence_check import RefundEvidenceCheckSkill
from maos.skills.contract import SkillContext
from maos.skills.invoker import SkillInvoker

SKILL = "refund.evidence_check"
REQUESTED_AT = "2026-09-01T10:00:00"

# 演示底账 `scenarios/custom/ledger.json` 三条规则的**真实形状**：三条都只写了
# applies_when，一条都没写举证判据 —— 这正是本 skill 必须有默认表的原因。
LEDGER_RULES = [
    {"rule_no": "AS-001", "version": 1, "title": "无理由退货期（30 天）",
     "ref": "AS-001@v1",
     "params": {"applies_when": {"reason_code": ["no_reason_return"]},
                "deduct_fee": "0", "no_reason_days": 30, "refund_ratio": "1",
                "rule_kind": "no_reason_return"}},
    {"rule_no": "AS-002", "version": 1, "title": "质保期内质量问题全额退",
     "ref": "AS-002@v1",
     "params": {"applies_when": {"reason_code": ["quality_defect"]},
                "deduct_fee": "0", "refund_ratio": "1",
                "rule_kind": "quality_within_warranty"}},
    {"rule_no": "AS-003", "version": 1, "title": "发错货全额退并免手续费",
     "ref": "AS-003@v1",
     "params": {"applies_when": {"reason_code": ["wrong_item"]},
                "deduct_fee": "0", "refund_ratio": "1", "rule_kind": "wrong_item"}},
]

# 测试用 identity：只授权本 skill，与 Agent 的白名单同宽。
EVIDENCE_IDENTITY = AgentIdentity(
    agent_id="test-evidence", role="test_evidence",
    duty="测试夹具：授权证据核验 skill",
    allowed_skills=frozenset({SKILL}), allowed_tools=frozenset(),
    write_scope=frozenset({"artifact"}), max_risk="L", model_tier=Tier.LIGHT,
)


def _extras(**over) -> dict:
    base = {"plan_id": "plan-test", "task_id": "task-test", "trace_id": "trace-test",
            "attempt": 1}
    base.update(over)
    return base


def _evidence(evidence_id: str, kind: str, *, digest: str = "d", source: str = "matrix") -> dict:
    return {"evidence_id": evidence_id, "kind": kind, "uri": f"mxc://{evidence_id}",
            "digest": digest, "source": source}


def _run(*, reason_code: str = "quality_defect", evidence=None, rules=None,
         order_facts=None, requested_at: str = REQUESTED_AT,
         invocation_id: str = "fixed") -> dict:
    """直调 skill，`invocation_id` 固定 —— 出参因此逐字节可复现。"""
    payload = {
        "case_seed": {"tenant_id": "T-1", "case_id": "C-1", "order_id": "ORD-1",
                      "reason_code": reason_code, "amount_claimed": "100.00"},
        "customer_evidence": evidence or [],
        "rules": rules if rules is not None else [],
        "order_facts": order_facts or {},
        "requested_at": requested_at,
    }
    return RefundEvidenceCheckSkill().run(
        payload, SkillContext(extras={"invocation_id": invocation_id}))


def _check(out: dict, name: str) -> dict:
    return next(c for c in out["consistency"] if c["check"] == name)


# ======================================================================
# 注册：投放即生效
# ======================================================================
def test_evidence_check_is_registered_with_version_1_0_0_and_nonempty_boundary():
    """投放进 builtin/refund/ 即注册；契约的三个必填面都在。"""
    cls = registry.get(SKILL)
    assert cls is not None, f"{SKILL} 没进注册表 —— 子包没被 discover() 扫到"
    assert cls.contract.version == "1.0.0"
    assert cls.contract.security_boundary, "缺 security_boundary（契约必填）"
    assert cls.contract.owner_roles == ["refund_evidence"]
    assert cls.contract.preconditions == ["case_seed"]
    assert cls.contract.failure_policy == "escalate" and cls.contract.max_retries == 0


def test_evidence_agent_is_registered_by_role_and_stays_out_of_refund_roles():
    """按 role 进池；但**不进** REFUND_ROLES —— 它是旁路观察岗，不进处置 DAG。

    `test_refund_flow.py` 对 REFUND_ROLES 是等值断言，加进去当场红（先例：
    `refund_channel` 也不在里面）。
    """
    assert AGENT_POOL["refund_evidence"] is RefundEvidenceAgent
    assert "refund_evidence" not in REFUND_ROLES


# ======================================================================
# 两级判定：规则声明优先，都没声明才退默认表
# ======================================================================
def test_silent_rules_and_no_default_yield_not_required():
    """底账三条规则 + 无理由退货：两级都没有要求 -> not_required，且不报未满足判据。"""
    out = _run(reason_code="no_reason_return", rules=LEDGER_RULES)
    assert out["verdict"] == "not_required"
    assert out["requirement_source"] == "none"
    assert out["unmet"] == []
    assert out["required_kinds"] == [] and out["min_count"] == 0


def test_default_table_applies_when_rules_are_silent():
    """底账三条规则一条都没写举证判据 -> 退到公司缺省口径，「证据缺」剧情跑得出来。"""
    out = _run(reason_code="quality_defect", rules=LEDGER_RULES, evidence=[])
    assert out["verdict"] == "missing"
    assert out["requirement_source"] == "default"
    assert out["required_kinds"] == ["image"] and out["min_count"] == 1
    assert out["unmet"][0]["rule_ref"] == "default:quality_defect"
    assert {u["direction"] for u in out["unmet"]} == {"not_applied"}, \
        "举证不足的方向恒为「不予适用」，不是拒赔"


def test_declared_kinds_all_present_yields_complete():
    """规则声明了举证判据 -> 默认表整张退场，按规则判。"""
    rules = [{"rule_no": "AS-002", "version": 1, "title": "t", "ref": "AS-002@v1",
              "params": {"requires_evidence_kinds": ["image"], "min_evidence_count": 1}}]
    out = _run(rules=rules, evidence=[_evidence("EV-1", "image")])
    assert out["verdict"] == "complete"
    assert out["requirement_source"] == "policy"
    assert out["unmet"] == [] and out["gaps"] == []


def test_missing_one_of_two_required_kinds_yields_partial():
    """要两类交了一类：既不是齐，也不是一类都没有 —— 是 partial。"""
    rules = [{"rule_no": "AS-002", "version": 1, "title": "t", "ref": "AS-002@v1",
              "params": {"requires_evidence_kinds": ["image", "video"]}}]
    out = _run(rules=rules, evidence=[_evidence("EV-1", "image")])
    assert out["verdict"] == "partial"
    assert out["required_kinds"] == ["image", "video"]
    assert len(out["unmet"]) == 1
    assert out["unmet"][0]["requirement"] == "requires_evidence_kinds"
    assert out["unmet"][0]["required"] == ["image", "video"]
    assert out["unmet"][0]["actual"] == ["image"]
    assert out["gaps"] == ["缺少 video 类证据"]


def test_rule_not_applying_to_reason_code_is_ignored():
    """声明挂在别的诉求类型上就不算数 —— 否则一条发错货的举证要求会套到质量问题上。"""
    rules = [{"rule_no": "AS-003", "version": 1, "title": "t", "ref": "AS-003@v1",
              "params": {"applies_when": {"reason_code": ["wrong_item"]},
                         "requires_evidence_kinds": ["video"], "min_evidence_count": 3}}]
    out = _run(reason_code="quality_defect", rules=rules, evidence=[])
    assert out["requirement_source"] == "default", "不适用的规则不该把判定拉到 policy 级"
    assert out["required_kinds"] == ["image"] and out["min_count"] == 1


def test_empty_declared_list_does_not_fall_back_to_default_table():
    """`requires_evidence_kinds: []` 是规则作者明确说的「不限类型」，不是没写。

    退回默认表会让「政策特意放宽」无法表达，且规则作者无从察觉。
    """
    rules = [{"rule_no": "AS-002", "version": 1, "title": "t", "ref": "AS-002@v1",
              "params": {"requires_evidence_kinds": []}}]
    out = _run(reason_code="quality_defect", rules=rules, evidence=[])
    assert out["requirement_source"] == "policy"
    assert out["required_kinds"] == [] and out["min_count"] == 0
    assert out["verdict"] == "complete", "没有要求就没有缺口"


# ======================================================================
# kind 归一化
# ======================================================================
def test_kind_aliases_normalize_mime_and_fall_back_to_attachment():
    """MIME 与自由文本都归一到五值；认不出走 attachment 兜底，**不丢证据**。"""
    out = _run(evidence=[
        _evidence("E1", "application/pdf"), _evidence("E2", "image/jpeg"),
        _evidence("E3", "PHOTO"), _evidence("E4", "喵喵喵"),
        _evidence("E5", "image/webp"),
    ])
    kinds = {it["evidence_id"]: it["kind"] for it in out["items"]}
    assert kinds == {"E1": "document", "E2": "image", "E3": "image",
                     "E4": "attachment", "E5": "image"}
    assert len(out["items"]) == 5, "认不出的证据不许被静默丢掉"


def test_kind_aliases_do_not_guess_from_uri_suffix():
    """声明优先于后缀：声明 document 而 uri 以 .jpg 结尾时以声明为准（T77 第 2 条口径）。"""
    out = _run(evidence=[{"evidence_id": "E1", "kind": "document",
                          "uri": "mxc://x/photo.jpg", "digest": "d", "source": "matrix"}])
    assert out["items"][0]["kind"] == "document"


def test_evidence_without_digest_is_not_counted():
    """digest 空 = 材料没真落下来，不计入证据集合，也不该把缺口盖过去。"""
    out = _run(rules=LEDGER_RULES,
               evidence=[_evidence("E1", "image", digest="")])
    assert out["items"][0]["ok"] is False
    assert out["verdict"] == "missing"
    assert _check(out, "evidence_digest_nonempty")["ok"] is False


# ======================================================================
# 交叉核对
# ======================================================================
def test_signed_at_after_requested_at_is_flagged_inconsistent():
    """签收晚于申请是时序矛盾；早于则成立。"""
    late = _run(order_facts={"logistics": {"signed_at": "2026-09-02T10:00:00"}})
    assert _check(late, "logistics_signed_before_request")["ok"] is False

    early = _run(order_facts={"logistics": {"signed_at": "2026-08-20T09:00:00"}})
    assert _check(early, "logistics_signed_before_request")["ok"] is True


def test_missing_logistics_is_skipped_not_flagged():
    """缺数据一律跳过并说明 —— 判 False 会让「没这份数据」和「数据对不上」混成一个信号。"""
    out = _run(order_facts={})
    row = _check(out, "logistics_signed_before_request")
    assert row["ok"] is True and "跳过" in row["note"]


def test_unparsable_timestamp_is_flagged_without_raising():
    """时间格式对不上是数据问题，报一条未能核对，**不抛**。"""
    out = _run(order_facts={"logistics": {"signed_at": "不是时间"}})
    row = _check(out, "logistics_signed_before_request")
    assert row["ok"] is False and "无法解析" in row["note"]


def test_qc_result_conflicting_with_reason_is_flagged_inconsistent():
    """质检判 pass 而客户报质量问题 —— 报不一致，但**不改 verdict**（那是给人看的线索）。"""
    out = _run(reason_code="quality_defect",
               order_facts={"qc_report": {"result": "pass"}},
               evidence=[_evidence("E1", "image")])
    assert _check(out, "qc_result_matches_reason")["ok"] is False
    assert out["verdict"] == "complete", "交叉核对不是举证是否充分的判据"

    ok = _run(reason_code="quality_defect",
              order_facts={"qc_report": {"result": "DEFECT"}})
    assert _check(ok, "qc_result_matches_reason")["ok"] is True, "大小写不敏感"


# ======================================================================
# 契约与确定性
# ======================================================================
def test_output_keys_equal_contract_1_5():
    """出参键集合 == 契约 §1.5 的九键 == output_schema —— 多一键少一键都算偏离。"""
    expected = {"items", "required_kinds", "min_count", "gaps", "verdict",
                "consistency", "invocation_id", "unmet", "requirement_source"}
    out = _run(rules=LEDGER_RULES)
    assert set(out) == expected
    assert set(RefundEvidenceCheckSkill.contract.output_schema) == expected
    assert set(RefundEvidenceCheckSkill.contract.input_schema) == {
        "case_seed", "customer_evidence", "rules", "order_facts", "requested_at"}


def test_same_input_twice_yields_identical_output():
    """纯函数：同一份入参两次逐字节一致（不用 now()、不把 set 直接落进出参）。"""
    kwargs = dict(rules=LEDGER_RULES, evidence=[_evidence("E1", "photo"),
                                                _evidence("E2", "mp4")],
                  order_facts={"logistics": {"signed_at": "2026-08-20T09:00:00"},
                               "qc_report": {"result": "defect"}})
    first, second = _run(**kwargs), _run(**kwargs)
    assert json.dumps(first, sort_keys=True, ensure_ascii=False) == \
        json.dumps(second, sort_keys=True, ensure_ascii=False)


def test_identity_without_the_skill_is_denied():
    """白名单不含它就调不动 —— 越权是安全事件，不是软失败。"""
    identity = AgentIdentity(
        agent_id="no-evidence", role="no_evidence", duty="缺 refund.evidence_check 授权",
        allowed_skills=frozenset({"policy.match"}), model_tier=Tier.LIGHT)
    with pytest.raises(PermissionDenied):
        SkillInvoker(identity, None).invoke(
            SKILL, {"case_seed": {"reason_code": "quality_defect"}}, extras=_extras())


def test_invoker_returns_ok_without_store():
    """本 skill 不落库，所以 store=None 也能经 invoker 跑通（房间旁路不需要库）。"""
    res = SkillInvoker(EVIDENCE_IDENTITY, None).invoke(
        SKILL, {"case_seed": {"reason_code": "quality_defect"},
                "rules": LEDGER_RULES, "customer_evidence": [],
                "requested_at": REQUESTED_AT}, extras=_extras())
    assert res.status == "ok", res.error
    assert res.output["verdict"] == "missing"
    assert res.output["invocation_id"] == res.invocation_id, "actor 锚点必须对得上"


# ======================================================================
# Agent 侧
# ======================================================================
def test_agent_wraps_output_into_evidence_report_artifact():
    """Agent 只搬运：产物带 kind / summary / self_check，业务判定一行都不在这里。"""
    agent = RefundEvidenceAgent(ScriptedModelClient({}), store=None)
    out = agent.run(TaskContext(
        plan_id="plan-test", task_id="task-test", trace_id="trace-test", attempt=1,
        inputs={"case_seed": {"tenant_id": "T-1", "case_id": "C-1",
                              "reason_code": "quality_defect"},
                "customer_evidence": [], "rules": LEDGER_RULES,
                "order_facts": {}, "requested_at": REQUESTED_AT},
        acceptance=[], risk_level="L"))
    assert out.status == "ok", out.error
    art = out.artifacts[0]
    assert art["kind"] == KIND_EVIDENCE_REPORT
    assert art["content"]["summary"], "summary 为空，Gate 判 blocker"
    assert art["content"]["self_check"] == {"build": "pass", "lint": "pass"}
    assert art["content"]["verdict"] == "missing"
    assert art["content"]["requirement_source"] == "default"
    assert out.metrics == {"verdict": "missing", "evidence_count": 0, "gaps": 2}


def test_agent_returns_failed_when_case_seed_is_absent():
    """上游没给案子时**不许**补个空 case_seed —— 那会报 not_required，掩盖数据没到。"""
    agent = RefundEvidenceAgent(ScriptedModelClient({}), store=None)
    out = agent.run(TaskContext(
        plan_id="plan-test", task_id="task-test", trace_id="trace-test", attempt=1,
        inputs={}, acceptance=[], risk_level="L"))
    assert out.status == "failed"
    assert "precondition_failed" in out.error and "case_seed" in out.error


def test_agent_does_not_claim_rejection_when_evidence_is_short():
    """铁律方向：举证不足只说「不予适用」，产物里不许出现拒赔口径。"""
    agent = RefundEvidenceAgent(ScriptedModelClient({}), store=None)
    out = agent.run(TaskContext(
        plan_id="plan-test", task_id="task-test", trace_id="trace-test", attempt=1,
        inputs={"case_seed": {"reason_code": "quality_defect"},
                "customer_evidence": [], "rules": LEDGER_RULES,
                "requested_at": REQUESTED_AT},
        acceptance=[], risk_level="L"))
    content = out.artifacts[0]["content"]
    assert {u["direction"] for u in content["unmet"]} == {"not_applied"}
    blob = json.dumps(content, ensure_ascii=False)
    for banned in ("拒赔", "驳回", "reject"):
        assert banned not in blob, f"证据核验岗不裁定，产物里不该出现「{banned}」"
