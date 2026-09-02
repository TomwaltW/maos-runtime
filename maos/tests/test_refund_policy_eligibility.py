"""`policy.match` 的条件判据（`eligibility`）—— 证据与条件真的成为判据（T74）。

本文件守的是一条**静默失效**：语料里 AS-003 写着 `requires_evidence_kinds:["image"]`、
`min_evidence_count:1`、`evidence_source:"customer_evidence"`，而这三个键在
`maos/**/*.py` 里曾经 grep 零命中 —— 后果是同一个案子交一张图和不交，
裁定结论逐字节相同，而且不报错。

🔴 **方向是本文件最要紧的事**（跨轨契约 §2 R1）：

    AS-003 是 `effect:"exclude"`、`refund_ratio:"0"`，商家**靠它免责**。
    `requires_evidence_kinds:["image"]` 的意思是「认定人为损坏需要图片支撑」。
    所以举证不足时的正确方向是：不能认定人为损坏 → 该免责条款**不予适用** →
    客户**照退**。反过来写（没交图就拒赔）是把举证责任倒置到客户身上。

因此每条相关用例的函数名都带方向词，且断言里都钉住
「`decision` 仍是 `approve`、`matched_rules` 仍含那条规则」——
「命中了哪几条」与「按这几条该不该退」是两个问题（`docs/BACKLOG.md:185`），
混成一个字段之后审计就说不清是规则没命中还是条件不满足。

时点一律相对 `now()` 造：`refund_case.created_at` 由 `guard.create_case` 写 now()，
而 `objects.execute` 拒绝对 `refund_case` 的旁路写入（铁律 8）——
把 `paid_at` 钉成「now 减 N 天」，「距支付 N 天」才是与跑测试的日子无关的常量。

本文件不发网络请求、不读 `MAOS_LLM_API_KEY`：条件判定是零模型的规则匹配
（跨轨契约 §2 R4）。证据行直接往 `customer_evidence` 表里造，不走附件入口（R3）。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from maos.agents.base import AgentIdentity
from maos.core.store import SqliteStore
from maos.domain.refund import guard, objects
from maos.model.client import Tier
from maos.skills.builtin.refund import policy as P
from maos.skills.builtin.refund.policy import PolicyMatchSkill
from maos.skills.invoker import SkillInvoker
from maos.skills.registry import SKILL_REGISTRY

SKILL = "policy.match"
V_OLD = "1.0.0"
V_NEW = "1.1.0"

CHANNEL_ID = "ch-web"
SKU = "SKU-BEARING-01"
ORDER_ID = "ord-t74"
ORDER_VERSION = 1
AMOUNT = 800.0
PLAN_ID = "plan-t74"
TASK_ID = "task-t74"

#: 三个租户各带一条规则，互不干扰 —— 一个租户里堆两条判据，
#: 对照组的 `unmet` 里就会混进另一条规则的未满足项，读不出是哪条判据在起作用。
T_EVIDENCE = "tnt-ev"      # 只有 AS-003（证据判据）
T_WINDOW_30 = "tnt-a"      # 只有 AS-001，no_reason_days = 30
T_WINDOW_7 = "tnt-b"       # 只有 AS-001，no_reason_days = 7
T_PROSE = "tnt-prose"      # 只有 AS-007，body 是人写的自然语言条款
T_WARRANTY = "tnt-wty"     # 只有 AS-002，warranty_basis

#: 申请时点距支付时点固定 10 天：30 天窗满足、7 天窗不满足，两侧只差这一个参数。
DAYS_SINCE_PAID = 10

AS_003_BODY = {
    "applies_when": {"reason_code": ["artificial_damage"]},
    "deduct_fee": "0",
    "effect": "exclude",
    "evidence_source": "customer_evidence",
    "min_evidence_count": 1,
    "refund_ratio": "0",
    "requires_evidence_kinds": ["image"],
    "rule_kind": "artificial_damage_exclusion",
}


TEST_IDENTITY = AgentIdentity(
    agent_id="test-t74-eligibility",
    role="test_refund",
    duty="测试夹具：只授权 policy.match",
    allowed_skills=frozenset({SKILL}),
    allowed_tools=frozenset(),
    write_scope=frozenset({"artifact"}),
    max_risk="L",
    model_tier=Tier.LIGHT,
)


# ---------------------------------------------------------------- 靶场
def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _seed_tenant(store, tenant_id: str, *, rules: list[tuple[str, int, dict | str]],
                 days_since_paid: int = DAYS_SINCE_PAID,
                 warranty_months: int = 12) -> str:
    """一个租户 = 一条订单 + 一个案子 + 若干条规则。返回 case_id。"""
    paid_at = _iso(datetime.now(timezone.utc) - timedelta(days=days_since_paid))
    objects.execute(store, "INSERT OR REPLACE INTO tenant (tenant_id, name, region)"
                           " VALUES (?,?,?)", (tenant_id, tenant_id, "CN-EAST"))
    objects.execute(store, "INSERT OR REPLACE INTO channel (tenant_id, channel_id, kind, name)"
                           " VALUES (?,?,?,?)", (tenant_id, CHANNEL_ID, "marketplace", "测试店"))
    objects.execute(
        store,
        "INSERT OR REPLACE INTO product_snapshot (tenant_id, sku, version, name, category,"
        " warranty_months, payload_json) VALUES (?,?,?,?,?,?,?)",
        (tenant_id, SKU, 1, "深沟球轴承", "bearing", warranty_months, "{}"))
    objects.execute(
        store,
        "INSERT OR REPLACE INTO order_snapshot (tenant_id, order_id, version, sku,"
        " amount_paid, paid_at, channel_id, policy_version_at_order, payload_json, read_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (tenant_id, ORDER_ID, ORDER_VERSION, SKU, AMOUNT, paid_at, CHANNEL_ID,
         1, "{}", paid_at))
    for rule_no, version, body in rules:
        text = body if isinstance(body, str) else json.dumps(
            body, ensure_ascii=False, sort_keys=True)
        objects.execute(
            store,
            "INSERT OR REPLACE INTO policy_rule (tenant_id, rule_no, version, title, body,"
            " effective_from, effective_to, channel_scope, sku_scope)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (tenant_id, rule_no, version, f"{rule_no} 测试规则", text,
             "2020-01-01T00:00:00+00:00", None, "*", "*"))
    case_id = f"case-{tenant_id}"
    guard.create_case(
        store, tenant_id=tenant_id, case_id=case_id, channel_id=CHANNEL_ID,
        order_id=ORDER_ID, order_version=ORDER_VERSION, sku=SKU,
        reason_code="artificial_damage", amount_claimed=AMOUNT, plan_id=PLAN_ID,
        actor_skill="test.seed", invocation_id="test-t74-seed-0001")
    return case_id


@pytest.fixture
def store():
    st = SqliteStore()
    st.init_schema()
    objects.ensure_schema(st)
    _seed_tenant(st, T_EVIDENCE, rules=[("AS-003", 1, AS_003_BODY)])
    _seed_tenant(st, T_WINDOW_30, rules=[("AS-001", 1, {
        "applies_when": {"reason_code": ["no_reason_return"]},
        "deduct_fee": "0", "no_reason_days": 30, "refund_ratio": "1",
        "rule_kind": "no_reason_return"})])
    _seed_tenant(st, T_WINDOW_7, rules=[("AS-001", 1, {
        "applies_when": {"reason_code": ["no_reason_return"]},
        "deduct_fee": "0", "no_reason_days": 7, "refund_ratio": "1",
        "rule_kind": "no_reason_return"})])
    _seed_tenant(st, T_PROSE, rules=[(
        "AS-007", 1, "自签收之日起七日内可申请无理由退货，退货运费由买方承担。")])
    return st


def _add_evidence(store, tenant_id: str, case_id: str, *kinds: str) -> None:
    """证据行直接落表 —— 附件入口上有别的会话在作业（跨轨契约 §2 R3）。"""
    for i, kind in enumerate(kinds, start=1):
        objects.execute(
            store,
            "INSERT OR REPLACE INTO customer_evidence (tenant_id, case_id, evidence_id,"
            " kind, uri, digest, submitted_at) VALUES (?,?,?,?,?,?,?)",
            (tenant_id, case_id, f"ev-{i}", kind, f"file://ev-{i}", f"sha-{i}",
             _iso(datetime.now(timezone.utc))))


def _invoke(store, tenant_id: str, case_id: str, version: str | None = V_OLD, **payload):
    inv = SkillInvoker(TEST_IDENTITY, store)
    return inv.invoke(SKILL, {"tenant_id": tenant_id, "case_id": case_id, **payload},
                      version=version, extras={"plan_id": PLAN_ID, "task_id": TASK_ID})


def _ok(res):
    assert res.status == "ok", res.error
    return res.output


# ======================================================================
# 证据判据 —— 方向：举证不足 = 免责条款不予适用 = 客户照退
# ======================================================================
def test_insufficient_evidence_does_not_apply_the_exclusion(store):
    """① 0 张图 → AS-003 进 `ineffective_rules`，而**裁定仍是 approve**。

    这一条是整轨的方向锚点：断言的重点不是「eligible 为 False」，
    而是「`decision` 没有因此翻成 reject、`matched_rules` 里 AS-003 还在」。
    少了后半截，本轨就变成了「没交图就拒赔」——举证责任倒置。
    """
    out = _ok(_invoke(store, T_EVIDENCE, f"case-{T_EVIDENCE}"))
    elig = out["eligibility"]

    assert elig["eligible"] is False
    assert elig["ineffective_rules"] == ["AS-003@v1"]
    assert elig["checked_rules"] == ["AS-003@v1"]
    assert elig["evidence_seen"] == {"image": 0, "video": 0, "total": 0}

    # 🔴 方向：不予适用该排除规则，不是拒赔。
    assert {u["direction"] for u in elig["unmet"]} == {"not_applied"}
    assert out["decision"] == "approve", "举证不足被写成了拒赔 —— 举证责任倒置"
    assert [r["rule_no"] for r in out["matched_rules"]] == ["AS-003"], (
        "AS-003 确实命中了，只是不予适用；从 matched_rules 里剔掉它，"
        "审计就说不清是规则没命中还是条件不满足")
    assert out["rule_refs"] == ["AS-003@v1"]


def test_sufficient_evidence_keeps_the_exclusion_effective(store):
    """② 交了 1 张 image → 判据满足，`ineffective_rules` 为空。

    与①合起来才是本轨买的东西：**交与不交，出参不再逐字节相同**。
    """
    _add_evidence(store, T_EVIDENCE, f"case-{T_EVIDENCE}", "image")
    out = _ok(_invoke(store, T_EVIDENCE, f"case-{T_EVIDENCE}"))
    elig = out["eligibility"]

    assert elig["eligible"] is True
    assert elig["unmet"] == []
    assert elig["ineffective_rules"] == []
    assert elig["checked_rules"] == ["AS-003@v1"]
    assert elig["evidence_seen"] == {"image": 1, "video": 0, "total": 1}
    assert out["decision"] == "approve"


def test_missing_required_kind_does_not_apply_the_exclusion(store):
    """③ 规则要 image+video 而库里只有 image → 未满足，且 requirement 点名是 kinds。

    数量够（1 ≥ 1）但**种类不全**：两条判据必须各自独立判，
    合成一个「证据够不够」的布尔值就说不出差在哪一维。
    """
    objects.execute(
        store,
        "UPDATE policy_rule SET body=? WHERE tenant_id=? AND rule_no=? AND version=?",
        (json.dumps({**AS_003_BODY, "requires_evidence_kinds": ["image", "video"]},
                    ensure_ascii=False, sort_keys=True), T_EVIDENCE, "AS-003", 1))
    _add_evidence(store, T_EVIDENCE, f"case-{T_EVIDENCE}", "image")

    out = _ok(_invoke(store, T_EVIDENCE, f"case-{T_EVIDENCE}"))
    elig = out["eligibility"]

    assert [u["requirement"] for u in elig["unmet"]] == ["requires_evidence_kinds"], (
        "min_evidence_count 是满足的（1 ≥ 1），不该跟着报未满足")
    miss = elig["unmet"][0]
    assert miss["required"] == ["image", "video"] and miss["actual"] == ["image"]
    assert miss["direction"] == "not_applied"
    assert elig["ineffective_rules"] == ["AS-003@v1"]
    assert out["decision"] == "approve"


# ======================================================================
# 时点判据 —— docs/BACKLOG.md:185 说的「今天跑不出驳回」的那组对照
# ======================================================================
def test_tenant_window_difference_makes_exactly_one_side_ineffective(store):
    """④ 租户 A（30 天）与租户 B（7 天）同一申请时点：一个满足、一个不满足。

    两侧都命中 `AS-001@v1`，唯一的差别是 `params.no_reason_days` 30 与 7 ——
    在 `b35c618` 上这个差别没有任何代码去看，两侧逐字节同结论。
    本条钉住它现在看得见了。

    仍然只体现在 `eligibility` 上：`decision` 两侧都还是 approve，
    「按这几条该不该退」归 `finance.settle`（T75 按 `ineffective_rules` 剔参）。
    """
    a = _ok(_invoke(store, T_WINDOW_30, f"case-{T_WINDOW_30}"))
    b = _ok(_invoke(store, T_WINDOW_7, f"case-{T_WINDOW_7}"))

    assert a["rule_refs"] == b["rule_refs"] == ["AS-001@v1"], "两侧命中的规则本来就相同"
    assert a["decision"] == b["decision"] == "approve", (
        "时效不满足的方向也是「不予适用」，不是当场翻成 reject")

    assert a["eligibility"]["eligible"] is True
    assert a["eligibility"]["ineffective_rules"] == []

    assert b["eligibility"]["eligible"] is False
    assert b["eligibility"]["ineffective_rules"] == ["AS-001@v1"]
    miss = b["eligibility"]["unmet"][0]
    assert miss["requirement"] == "no_reason_days"
    assert miss["required"] == 7 and miss["actual"] > 7
    assert miss["direction"] == "not_applied"

    assert a["eligibility"] != b["eligibility"], (
        "两个租户的判定结果仍然逐字节相同 —— 30 与 7 的区别还是没人看")


def test_expired_warranty_does_not_apply_the_warranty_rule(store):
    """⑤ 过保：AS-002 声明按商品快照的质保月数判，1 个月保修、申请在 400 天后。"""
    case_id = _seed_tenant(
        store, T_WARRANTY, days_since_paid=400, warranty_months=1,
        rules=[("AS-002", 1, {"applies_when": {"reason_code": ["quality_defect"]},
                              "deduct_fee": "0", "refund_ratio": "1",
                              "rule_kind": "quality_within_warranty",
                              "warranty_basis": "product_snapshot.warranty_months"})])
    out = _ok(_invoke(store, T_WARRANTY, case_id))
    elig = out["eligibility"]

    assert elig["ineffective_rules"] == ["AS-002@v1"]
    miss = elig["unmet"][0]
    assert miss["requirement"] == "warranty_basis"
    assert miss["required"]["warranty_months"] == 1
    assert miss["direction"] == "not_applied"
    assert out["decision"] == "approve", "过保的方向同样是不予适用该规则，不是拒赔"


def test_within_warranty_stays_effective(store):
    """⑥ 在保：同一条规则、24 个月保修、申请在 10 天后 → 判据满足。"""
    case_id = _seed_tenant(
        store, T_WARRANTY, days_since_paid=10, warranty_months=24,
        rules=[("AS-002", 1, {"applies_when": {"reason_code": ["quality_defect"]},
                              "deduct_fee": "0", "refund_ratio": "1",
                              "rule_kind": "quality_within_warranty",
                              "warranty_basis": "product_snapshot.warranty_months"})])
    elig = _ok(_invoke(store, T_WARRANTY, case_id))["eligibility"]
    assert elig["eligible"] is True
    assert elig["checked_rules"] == ["AS-002@v1"] and elig["unmet"] == []


# ======================================================================
# 认不出就报错 / 没声明就不判（跨轨契约 §2 R5）
# ======================================================================
def test_illegal_evidence_source_fails_closed(store):
    """⑦ `evidence_source` 是枚举外的值 → 抛异常，不静默当成 customer_evidence。

    `evidence_source` **不是判据，是数据源声明**：它回答「去哪张表数证据」。
    静默走缺省的症状是演示当场看不出来、对账时才发现按错了表数
    （`docs/DECISIONS.md:333` 记过同型的教训）。
    """
    case = guard.get_case(store, T_EVIDENCE, f"case-{T_EVIDENCE}")
    rule = {"rule_no": "AS-003", "version": 1,
            "body": json.dumps({**AS_003_BODY, "evidence_source": "attachment_bucket"})}

    with pytest.raises(ValueError, match="evidence_source"):
        P.evaluate_conditions(store, tenant_id=T_EVIDENCE, case=case, rules=[rule])


def test_missing_evidence_source_fails_closed(store):
    """⑧ 声明了证据判据却没声明 `evidence_source` → 同样抛异常，不猜缺省值。"""
    case = guard.get_case(store, T_EVIDENCE, f"case-{T_EVIDENCE}")
    body = {k: v for k, v in AS_003_BODY.items() if k != "evidence_source"}
    rule = {"rule_no": "AS-003", "version": 1, "body": json.dumps(body)}

    with pytest.raises(ValueError, match="evidence_source"):
        P.evaluate_conditions(store, tenant_id=T_EVIDENCE, case=case, rules=[rule])


def test_unreadable_rule_body_stays_out_of_checked_rules(store):
    """⑨ body 是人写的自然语言条款 → 不进 `checked_rules`，**更不进 `unmet`**。

    没有声明判据的规则不该被判为「不满足」：那等于把「我们读不动这条条款」
    翻译成「客户没达标」，方向与 R1 同类。
    """
    out = _ok(_invoke(store, T_PROSE, f"case-{T_PROSE}"))
    elig = out["eligibility"]

    assert out["rule_refs"] == ["AS-007@v1"], "规则确实命中了"
    assert elig["checked_rules"] == [], "读不动的条款没有判据可检查"
    assert elig["unmet"] == [] and elig["ineffective_rules"] == []
    assert elig["eligible"] is True
    assert out["decision"] == "approve"


# ======================================================================
# 两版一致 —— 本轨不许多出第二处版本差异
# ======================================================================
@pytest.fixture
def published():
    """把 v1.1.0 发布进注册表，跑完摘掉（口径同 `test_skill_versioning.py`）。

    留在表里的话 `registry.get("policy.match")` 变成最高版本，
    `test_refund_flow.py` 里那条「按名取到 1.0.0」会当场变红。
    """
    from maos.skills.builtin.refund import policy_v1_1
    from maos.skills.registry import register_skill

    register_skill(policy_v1_1.PolicyMatchV11Skill)
    yield policy_v1_1
    SKILL_REGISTRY[SKILL].pop(V_NEW, None)


@pytest.mark.parametrize("kinds", [(), ("image",)])
def test_v1_0_and_v1_1_agree_on_eligibility(store, published, kinds):
    """⑩ 同一个案子、同一份证据下，两版的 `eligibility` 逐字节相同。

    两版的差异**仍然只有时效窗那一处**：本轨把判定器抽成 `policy.py` 的模块级
    函数、两版都调它，正是为了不多出第二处差异。各写一份的话，
    症状是同一个案子在 v1.0.0 与 v1.1.0 下证据结论不同，且没有任何地方报错。
    """
    _add_evidence(store, T_EVIDENCE, f"case-{T_EVIDENCE}", *kinds)
    old = _ok(_invoke(store, T_EVIDENCE, f"case-{T_EVIDENCE}", version=V_OLD))
    new = _ok(_invoke(store, T_EVIDENCE, f"case-{T_EVIDENCE}", version=V_NEW))

    assert old["eligibility"] == new["eligibility"]
    assert json.dumps(old["eligibility"], sort_keys=True) == \
        json.dumps(new["eligibility"], sort_keys=True)
    assert old["rule_refs"] == new["rule_refs"], "AS-003 没声明 window_days，时效闸不该动它"


def test_eligibility_shape_matches_the_cross_track_contract(store):
    """⑪ 出参形状与跨轨契约 §1.1 逐字段一致 —— 多一个键少一个键 T75 都会对不上。"""
    elig = _ok(_invoke(store, T_EVIDENCE, f"case-{T_EVIDENCE}"))["eligibility"]

    assert set(elig) == {"eligible", "unmet", "evidence_seen",
                         "checked_rules", "ineffective_rules"}
    assert set(elig["unmet"][0]) == {"rule_ref", "requirement", "required",
                                     "actual", "direction"}
    assert set(elig["evidence_seen"]) >= {"image", "video", "total"}
    assert elig["unmet"][0]["requirement"] in {
        P.REQUIREMENT_MIN_EVIDENCE_COUNT, P.REQUIREMENT_REQUIRES_EVIDENCE_KINDS,
        P.REQUIREMENT_NO_REASON_DAYS, P.REQUIREMENT_WARRANTY_BASIS}


def test_contract_declares_eligibility_and_read_only_boundary():
    """⑫ 契约面：`output_schema` 有 eligibility，`security_boundary` 声明只读不写。"""
    c = PolicyMatchSkill.contract
    assert "eligibility" in c.output_schema
    assert "customer_evidence" in c.security_boundary and "不写" in c.security_boundary
    assert c.version == V_OLD, (
        "出参加键是向后兼容的；升版本会让两个不钉版本的调用点静默换类")
