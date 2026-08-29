"""Skill 多版本发布 / 取版 / 回滚 / 质量评估的机器验收（T11）。

`test_skills.py` 已经守住了**机制**：同名三版共存时 `versions()` 按数值序返回。
但那三版是用 `type()` 当场造出来的空壳，回滚路径在演示链路上从没被真用过。
本文件守的是**真事**：`policy.match` 两个真实版本、真实的行为差异、真实的证据链。

九条断言分三组：

  · 发布 / 取版 / 回滚（1-3）—— 对应 `docs/skill-catalog.md` 的同名小节；
  · 行为差异与**存量不受影响**（4-5）—— 5 是跨轨契约 1 的回归，用的是场景 6
    那一份真实靶场数据，不是为测试造的；
  · 证据与边界（6-9）—— 审计行分得开两版、v1.1.0 不进正常 import 路径、
    时效参数的取值边界、超窗规则不进依据链。

**第 7 条是本文件最要紧的一条**：v1.1.0 一旦进 `refund/__init__.py` 的清单，
退款域两个不钉版本的调用点会静默升版，`evidence/scenario-*/trace.json` 里
那几十处 `"version": "1.0.0"` 全部跟着变（`test_refund_flow.py:127` 也会当场变红）。
那一行 import 是加不得的，所以拿源码把它钉死。

本文件不发网络请求，不读 `MAOS_LLM_API_KEY` —— 裁定是零模型的规则匹配。
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from maos.agents.base import AgentIdentity
from maos.core.store import SqliteStore
from maos.domain.refund import guard, objects
from maos.model.client import Tier
from maos.skills import version_demo as demo
from maos.skills import registry
from maos.skills.builtin.refund.policy import PolicyMatchSkill
from maos.skills.invoker import SkillInvoker
from maos.skills.registry import SKILL_REGISTRY, register_skill

SKILL = "policy.match"
V_OLD = "1.0.0"
V_NEW = "1.1.0"

REPO_ROOT = Path(__file__).resolve().parents[2]

#: 契约的九要素 = 除主键（name + version）外的全部字段，顺序取自 dataclass 声明，
#: 不在这里另抄一份（同 `scripts/gen_docs.py` 的口径）。
NINE = tuple(f.name for f in dataclasses.fields(PolicyMatchSkill.contract)
             if f.name not in ("name", "version"))

TEST_IDENTITY = AgentIdentity(
    agent_id="test-skill-version",
    role="test_refund",
    duty="测试夹具：只授权 policy.match",
    allowed_skills=frozenset({SKILL}),
    allowed_tools=frozenset(),
    write_scope=frozenset({"artifact"}),
    max_risk="L",
    model_tier=Tier.LIGHT,
)


@pytest.fixture(scope="module", autouse=True)
def published():
    """把 v1.1.0 发布进注册表，本模块跑完再摘掉；yield 出模块本身供用例取符号。

    摘掉不是洁癖：v1.1.0 留在表里，`registry.get("policy.match")` 就变成最高版本，
    而 `test_refund_flow.py:127` 断言按名取到的是 1.0.0 —— 那条会当场变红。

    **import 必须写在这里，不能写在文件顶层**：pytest 的 collection 阶段会 import
    每一个测试模块，顶层那行 import 的 `@register_skill` 副作用于是发生在**所有**
    用例开跑之前，`test_refund_flow.py` 无论排在多前面都躲不开。收进 fixture，
    污染窗口才收敛成本模块自己的生命周期。

    显式 `register_skill` 而不是只靠 import 副作用：import 有缓存，摘掉之后
    第二次 import 是空操作，同一 session 内再进本模块就注册不回来了。
    """
    from maos.skills.builtin.refund import policy_v1_1

    register_skill(policy_v1_1.PolicyMatchV11Skill)
    yield policy_v1_1
    SKILL_REGISTRY[SKILL].pop(V_NEW, None)


@pytest.fixture
def demo_store():
    """演示那一份靶场：订单支付于 92 天前，AS-01 声明 30 天申请时效。

    直接复用 `version_demo.seed()` —— 测试与演示看同一组数据，两边各造一套，
    「演示里是 reject、测试里是 approve」这种分叉迟早会发生。
    """
    st = SqliteStore()
    st.init_schema()
    demo.seed(st)
    return st


@pytest.fixture
def s6_store():
    """场景 6 那一份**真实**靶场 —— 契约 1 的回归就得用既有场景的数据来验。"""
    from maos.flows import scenario_6 as s6

    st = SqliteStore()
    st.init_schema()
    s6.seed_domain(st)
    guard.create_case(
        st, tenant_id=s6.TENANT_ID, case_id=s6.CASE_ID, channel_id=s6.CHANNEL_ID,
        order_id=s6.ORDER_ID, order_version=s6.ORDER_VERSION, sku=s6.SKU,
        reason_code="quality_defect", amount_claimed=s6.AMOUNT_CLAIMED,
        plan_id="plan-ver-s6", actor_skill="test.seed", invocation_id="test-seed-0001")
    return st, s6


def _invoke(store, case_id, version, *, plan_id="plan-ver", task_id="task-ver", **payload):
    inv = SkillInvoker(TEST_IDENTITY, store)
    body = {"tenant_id": demo.TENANT_ID, "case_id": case_id, **payload}
    return inv.invoke(SKILL, body, version=version,
                      extras={"plan_id": plan_id, "task_id": task_id})


def _business(out: dict) -> dict:
    """结论本身。invocation_id 每次调用都新生成，不属于结论。"""
    return {k: v for k, v in out.items() if k != "invocation_id"}


# ======================================================================
# 发布 / 取版 / 回滚
# ======================================================================
def test_two_versions_coexist_after_publish(published):
    """① 发布：投放一个模块就两版共存，且升序按数值排。"""
    assert registry.versions(SKILL) == [V_OLD, V_NEW]
    assert SKILL_REGISTRY[SKILL][V_OLD] is PolicyMatchSkill
    assert SKILL_REGISTRY[SKILL][V_NEW] is published.PolicyMatchV11Skill


def test_default_get_returns_highest_version(published):
    """② 取版：缺省拿最高版本，按段数值序（不是字符串序）。"""
    assert registry.get(SKILL) is published.PolicyMatchV11Skill
    assert registry.get(SKILL).contract.version == V_NEW
    assert registry._semver_key(V_NEW) > registry._semver_key(V_OLD)
    assert registry.get(SKILL, "9.9.9") is None, "取不到的版本返回 None，不许回落最高版"


def test_rollback_returns_the_very_class_of_v1_0_0(published):
    """③ 回滚：按版本取拿到的**就是**当年那一个类，九要素逐字段没被新版改写。

    只比版本号字符串是不够的 —— 那连「旧版被就地改成新逻辑、只留了个旧版本号」
    都发现不了。所以逐字段比契约，并要求两版确有差异。
    """
    old = registry.get(SKILL, V_OLD)
    assert old is PolicyMatchSkill, "旧版本被覆盖了 —— 回滚叙事的全部依据就是它还在"

    for field in NINE:
        assert getattr(old.contract, field) == getattr(PolicyMatchSkill.contract, field)

    changed = [f for f in NINE
               if getattr(old.contract, f) != getattr(published.PolicyMatchV11Skill.contract, f)]
    assert {"purpose", "input_schema", "output_schema", "reuse_note"} <= set(changed), (
        f"两版契约差异不足，只改版本号不算升版：变了的是 {changed}")
    assert "window_days" not in old.contract.purpose, "v1.0.0 的契约被新版污染了"


# ======================================================================
# 行为差异，以及存量口径不受影响
# ======================================================================
def test_two_versions_disagree_on_windowed_rule(demo_store):
    """④ 同一份输入喂两版，结论确有差异 —— 版本升级不是改个号。"""
    new = _invoke(demo_store, demo.CASE_WINDOWED, None, as_of=demo.AS_OF)
    old = _invoke(demo_store, demo.CASE_WINDOWED, V_OLD, as_of=demo.AS_OF)
    assert new.status == old.status == "ok", (new.error, old.error)

    assert old.output["decision"] == "approve"
    assert old.output["rule_refs"] == ["AS-01@v1"], "v1.0.0 不读 window_days"
    assert new.output["decision"] == "reject"
    assert new.output["rule_refs"] == [], "v1.1.0 把超窗规则从命中集合里剔除"
    assert "超出申请时效" in new.output["reason"]
    assert _business(new.output) != _business(old.output)


def test_existing_scenario_input_yields_identical_output(s6_store):
    """⑤ 跨轨契约 1：场景 6 那份**真实**输入下，两版输出逐字段相同。

    既有七个场景的政策规则都没声明 window_days，时效闸对它们是恒等变换。
    这条一旦红，说明 v1.1.0 改动了存量口径 —— 那 `evidence/` 的证据束就守不住了。
    """
    store, s6 = s6_store
    inv = SkillInvoker(TEST_IDENTITY, store)
    body = {"tenant_id": s6.TENANT_ID, "case_id": s6.CASE_ID}
    extras = {"plan_id": "plan-ver-s6", "task_id": "task-ver-s6"}

    new = inv.invoke(SKILL, body, version=V_NEW, extras=extras)
    old = inv.invoke(SKILL, body, version=V_OLD, extras=extras)
    assert new.status == old.status == "ok", (new.error, old.error)

    assert _business(new.output) == _business(old.output), (
        "两版对既有场景的输入给出了不同的输出 —— 契约 1 破了")
    assert new.output["decision"] == "approve"
    assert new.output["rule_refs"] == ["AS-01@v1"], "场景 6 的金额锁在 AS-01@v1 上"
    assert new.output["policy_version"] == 1, "版本仍锁在订单快照上，不是 max(version)"


# ======================================================================
# 证据与边界
# ======================================================================
def test_skill_invoked_event_distinguishes_versions(demo_store):
    """⑥ 质量评估：两版各落一条 SkillInvoked，detail 里的 version 分得开。"""
    _invoke(demo_store, demo.CASE_WINDOWED, None, plan_id="plan-agg", as_of=demo.AS_OF)
    _invoke(demo_store, demo.CASE_WINDOWED, V_OLD, plan_id="plan-agg", as_of=demo.AS_OF)

    rows = [r for r in demo_store.list_event_log("plan-agg")
            if r["event_type"] == "SkillInvoked"]
    assert len(rows) == 2

    by_version = {r["detail"]["version"]: r["detail"] for r in rows}
    assert set(by_version) == {V_OLD, V_NEW}, "两版混成一行就聚合不出各自的成功率"
    for version, detail in by_version.items():
        assert detail["skill"] == SKILL
        assert detail["status"] == "ok"
        assert {"duration_ms", "input_digest", "output_hash", "invocation_id"} <= set(detail)
    assert by_version[V_OLD]["input_digest"] == by_version[V_NEW]["input_digest"], (
        "同一份输入的摘要必须相同，否则两版的差异无从归因")
    assert by_version[V_OLD]["output_hash"] != by_version[V_NEW]["output_hash"]


def test_v1_1_0_stays_out_of_the_normal_import_path():
    """⑦ 契约 1 的**设计本身**：v1.1.0 不许进两个 __init__.py 的清单。

    进了的话 `registry.get("policy.match")` 缺省变 1.1.0，而退款域两个调用点
    （policy_agent.py / finance_agent.py）都不钉版本 —— 落库那行 SkillInvoked 的
    version 会从 1.0.0 变成 1.1.0，evidence/ 的证据束跟着变。
    两个调用点不在本轨手里，所以这里拿源码钉死，不靠口头约定。
    """
    for rel in ("maos/skills/builtin/__init__.py",
                "maos/skills/builtin/refund/__init__.py"):
        src = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "policy_v1_1" not in src, (
            f"{rel} 里出现了 policy_v1_1：v1.1.0 进了正常 import 路径，"
            "既有场景会静默升版（跨轨契约 1）")

    for rel in ("maos/agents/refund/policy_agent.py",
                "maos/agents/refund/finance_agent.py"):
        src = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "version=" not in src, (
            f"{rel} 开始钉版本了 —— 那说明升版路线已改走乙案，本条随之作废")


@pytest.mark.parametrize("raw, want", [
    ({"window_days": 30}, 30.0),
    ({"window_days": 7.5}, 7.5),
    ({}, None),
    ({"window_days": True}, None),      # bool 是 int 的子类，会被算成 1 天
    ({"window_days": 0}, None),
    ({"window_days": -1}, None),
    ({"window_days": "30"}, None),      # 人写的自然语言条款不猜
])
def test_window_days_of_rejects_bool_and_non_positive(published, raw, want):
    """⑧ 时效参数的取值边界。None 一律等于「不限时效」= 退回 v1.0.0 的行为。"""
    assert published.window_days_of(raw) == want


def test_expired_rule_is_not_recorded_as_business_ref(demo_store):
    """⑨ 安全边界：超窗规则不写进 business_ref —— 依据链里只许留真正采信的那几条。"""
    _invoke(demo_store, demo.CASE_WINDOWED, None,
            plan_id="plan-ref", task_id="task-ref", as_of=demo.AS_OF)
    refs = objects.list_business_refs(demo_store, plan_id="plan-ref", task_id="task-ref")
    assert [r for r in refs if r["object_type"] == "policy_rule"] == [], (
        "超窗规则被记成了裁定依据 —— 那条依据链解释不通")

    _invoke(demo_store, demo.CASE_LEGACY, None,
            plan_id="plan-ref2", task_id="task-ref2", as_of=demo.AS_OF)
    kept = [r for r in objects.list_business_refs(demo_store, plan_id="plan-ref2",
                                                  task_id="task-ref2")
            if r["object_type"] == "policy_rule"]
    assert [r["object_id"] for r in kept] == ["AS-02"], "窗内规则要照常进依据链"
