"""退款域端到端与逐 skill 的契约测试。

本文件里有两类断言，混在一起是有意的：

  · **功能性**：六个 skill 的 IO 形状、状态推进、幂等、金额核算；
  · **论证性**：Manager / Reviewer 零改动即可参与退款域编排、内核不认识退款域、
    settled 只有一个写入方。这几条是复赛材料里那句「换域只换 Skill / ToolPort /
    业务对象」的机器化版本 —— 写成注释谁都会写，写成断言才拦得住下一次改动。

论证性断言标了 `# 论证：` 前缀，评审时可直接按这个前缀捞出来对。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from maos.agents.base import AgentIdentity
from maos.agents.manager import ManagerAgent
from maos.agents.refund import (
    KIND_FINANCE_SETTLEMENT,
    REFUND_ROLES,
    RefundFinanceAgent,
    RefundIntakeAgent,
    RefundPaymentAgent,
    RefundPolicyAgent,
)
from maos.agents.reviewer import ReviewerAgent
from maos.contracts.states import PlanState
from maos.core.store import SqliteStore
from maos.domain.refund import guard, objects
from maos.flows import common as flows_common
from maos.flows import scenario_6 as s6
from maos.model.client import Tier
from maos.skills import registry
from maos.skills.builtin.refund import REFUND_SKILLS
from maos.skills.builtin.refund import _common as C
from maos.skills.invoker import SkillInvoker
from maos.tools.gateway import MockGateway

REPO_ROOT = Path(__file__).resolve().parents[2]
MAOS_PKG = REPO_ROOT / "maos"

TEST_GATEWAY = "test-gw"

# 测试用 identity：把本域全部 skill 都授权给它。
# 单测要验的是 skill 自己的行为，不是白名单 —— 越权那条路由 invoker 的既有测试守。
ALL_SKILLS_IDENTITY = AgentIdentity(
    agent_id="test-refund",
    role="test_refund",
    duty="测试夹具：授权退款域全部 skill",
    allowed_skills=frozenset(set(REFUND_SKILLS) | {"issue.aggregate"}),
    allowed_tools=frozenset({"gateway.refund", "gateway.query"}),
    write_scope=frozenset({"artifact"}),
    max_risk="M",
    model_tier=Tier.LIGHT,
)


@pytest.fixture
def store():
    st = SqliteStore()
    st.init_schema()
    # 靶场数据直接复用场景 6 的那一份：测试与演示看的是同一组政策与订单，
    # 两边各造一套数据，金额断言迟早对不上。
    s6.seed_domain(st)
    return st


@pytest.fixture
def gateway():
    C.reset_gateways()
    gw = C.register_gateway(TEST_GATEWAY, MockGateway(settle_after=s6.SETTLE_AFTER))
    yield gw
    C.reset_gateways()


@pytest.fixture
def invoker(store):
    return SkillInvoker(ALL_SKILLS_IDENTITY, store)


def _extras(**over) -> dict:
    base = {"plan_id": "plan-test", "task_id": "task-test", "trace_id": "trace-test",
            "attempt": 1}
    base.update(over)
    return base


def _intake(invoker, *, case_id=s6.CASE_ID, signals=None):
    seed = {
        "tenant_id": s6.TENANT_ID, "case_id": case_id, "channel_id": s6.CHANNEL_ID,
        "order_id": s6.ORDER_ID, "order_version": s6.ORDER_VERSION, "sku": s6.SKU,
        "reason_code": "quality_defect", "amount_claimed": s6.AMOUNT_CLAIMED,
    }
    return invoker.invoke("refund.intake", {
        "signals": s6.SIGNALS if signals is None else signals, "case_seed": seed,
    }, extras=_extras())


def _settle(invoker, *, case_id=s6.CASE_ID):
    pol = invoker.invoke("policy.match",
                         {"tenant_id": s6.TENANT_ID, "case_id": case_id}, extras=_extras())
    assert pol.status == "ok", pol.error
    fin = invoker.invoke("finance.settle", {
        "tenant_id": s6.TENANT_ID, "case_id": case_id, "policy": pol.output,
    }, extras=_extras())
    return pol, fin


def _approve(store, *, case_id=s6.CASE_ID):
    return C.record_approval(store, tenant_id=s6.TENANT_ID, case_id=case_id,
                             approver="测试主管", decision="approved", reason="单测放行")


# ======================================================================
# 注册：投放即生效
# ======================================================================
def test_six_refund_skills_registered_by_file_drop():
    """六个 skill 放进 builtin/refund/ 即注册，`builtin/__init__.py` 一个字没改。"""
    for name in REFUND_SKILLS:
        cls = registry.get(name)
        assert cls is not None, f"{name} 没进注册表 —— 子包没被 discover() 扫到"
        assert cls.contract.version == "1.0.0"
        assert cls.contract.security_boundary, f"{name} 缺 security_boundary（契约必填）"


def test_builtin_init_not_touched():
    """discover() 靠 pkgutil 扫子包，`builtin/__init__.py` 里不许出现退款域的显式清单。"""
    src = (MAOS_PKG / "skills" / "builtin" / "__init__.py").read_text(encoding="utf-8")
    assert "refund" not in src, "builtin/__init__.py 被改成了显式清单，投放即注册的口径破了"


def test_four_refund_agents_registered():
    from maos.agents import AGENT_POOL
    expected = {
        "refund_intake": RefundIntakeAgent,
        "refund_policy": RefundPolicyAgent,
        "refund_finance": RefundFinanceAgent,
        "refund_payment": RefundPaymentAgent,
    }
    for role, cls in expected.items():
        assert AGENT_POOL.get(role) is cls, f"{role} 没按 role 注册进 AGENT_POOL"
    assert set(REFUND_ROLES) == set(expected)


# ======================================================================
# 六个 skill 的 IO 契约
# ======================================================================
def test_intake_io_contract_and_dedup(invoker, store):
    """入 {signals, case_seed} -> 出 {case_draft, evidence_refs, issues, dedup}。"""
    res = _intake(invoker)
    assert res.status == "ok", res.error
    out = res.output
    assert set(out) >= {"case_draft", "evidence_refs", "issues", "dedup", "invocation_id"}

    case = out["case_draft"]
    assert case["biz_status"] == "submitted", "建案必须落 submitted，不接受调用方指定"
    assert case["case_id"] == s6.CASE_ID

    # 三条说同一件事的信号被并成一个 issue，另一条独立 —— 4 进 2 出。
    assert out["dedup"] == {"signals": 4, "issues": 2, "merged": 2}
    assert len(out["evidence_refs"]) == 2, "带 uri 的两条是证据"
    rows = objects.query(store, "SELECT * FROM customer_evidence WHERE case_id=?", (s6.CASE_ID,))
    assert len(rows) == 2 and all(r["digest"] for r in rows), "证据 digest 不许为空"


def test_intake_refuses_to_degrade_when_dedup_unavailable(store, monkeypatch):
    """去重不可用是硬失败，不许静默跳过 —— 跳过会让多源诉求被当成多个不同的问题。"""
    identity = AgentIdentity(
        agent_id="no-agg", role="no_agg", duty="缺 issue.aggregate 授权",
        allowed_skills=frozenset(set(REFUND_SKILLS)),   # 刻意不含 issue.aggregate
        model_tier=Tier.LIGHT)
    from maos.agents.base import PermissionDenied
    with pytest.raises(PermissionDenied):
        _intake(SkillInvoker(identity, store))


def test_policy_match_uses_pinned_version_not_latest(invoker, store):
    """论证：政策按**订单快照锁定的版本**判，不是 policy_rule 的最新版本。

    靶场里 AS-01 有 v1 / v2 两版，**生效区间完全相同**，唯一差别是版本号。
    所以把 v2 排除在外的只可能是版本锁定这一条 —— 若有人把实现改成取
    max(version)，本条会红，且金额会从 6800.00 变成 5390.00。
    """
    _intake(invoker)
    res = invoker.invoke("policy.match",
                         {"tenant_id": s6.TENANT_ID, "case_id": s6.CASE_ID}, extras=_extras())
    assert res.status == "ok", res.error
    out = res.output
    assert out["policy_version"] == 1, "订单锁定的是 v1"
    assert out["rule_refs"] == ["AS-01@v1"], f"应只命中 AS-01 的 v1，实际 {out['rule_refs']}"
    assert out["decision"] == "approve"

    latest = objects.query(
        store, "SELECT MAX(version) AS v FROM policy_rule WHERE tenant_id=? AND rule_no='AS-01'",
        (s6.TENANT_ID,))[0]["v"]
    assert latest == 2, "靶场必须存在更新的 v2，否则这条测试什么都没验到"
    assert out["policy_version"] != latest, "用了最新版本就是拿今天的规则追溯昨天的交易"

    # 前缀过滤：售前规则 PS-07 生效区间也覆盖本单，但不该参与售后裁定。
    assert all(r["rule_no"].startswith("AS-") for r in out["matched_rules"])


def test_finance_settle_writes_both_sides(invoker, store):
    """F-1 两侧都验：产物 content 带 finance_entry 键，且 finance_entry 表有对应行。"""
    _intake(invoker)
    _pol, fin = _settle(invoker)
    assert fin.status == "ok", fin.error

    entry = fin.output["finance_entry"]
    assert isinstance(entry, dict) and entry, "finance_entry 必须是非空 dict（F-1 判据）"
    assert entry["amount_approved"] == 6800.00, "按锁定的 v1 政策全额退"

    rows = objects.query(store, "SELECT * FROM finance_entry WHERE tenant_id=? AND case_id=?",
                         (s6.TENANT_ID, s6.CASE_ID))
    assert len(rows) == 1, "库表必须同时写"
    # 两侧同一份数据：各造一份的症状是闸恒 blocker 或恒 pass，且要跑到场景 6 才发作。
    assert rows[0]["amount_approved"] == entry["amount_approved"]
    assert rows[0]["breakdown_json"] == entry["breakdown_json"]
    assert json.loads(rows[0]["rule_refs"]) == ["AS-01@v1"]


def test_finance_agent_artifact_carries_finance_entry(store, gateway):
    """F-1 的 R-2 侧义务落在 Agent 产物上 —— 闸读的就是这个键。"""
    from maos.agents.base import TaskContext
    from maos.model.client import ScriptedModelClient

    _intake(SkillInvoker(ALL_SKILLS_IDENTITY, store))
    agent = RefundFinanceAgent(ScriptedModelClient({}), store=store)
    out = agent.run(TaskContext(
        plan_id="plan-test", task_id="task-test", trace_id="trace-test", attempt=1,
        inputs={"tenant_id": s6.TENANT_ID, "case_id": s6.CASE_ID},
        acceptance=[], risk_level="M"))
    assert out.status == "ok", out.error
    art = out.artifacts[0]
    assert art["kind"] == KIND_FINANCE_SETTLEMENT
    assert isinstance(art["content"].get("finance_entry"), dict)
    assert art["content"]["finance_entry"], "content.finance_entry 为空，第六道闸会判 blocker"
    # Gate 对非代码类产物的两条硬判据，缺了就是 rework 且症状指不到原因。
    assert art["content"]["summary"]
    assert art["content"]["self_check"] == {"build": "pass", "lint": "pass"}


def test_payment_execute_stops_at_processing(invoker, store, gateway):
    """payment.execute 后是 processing，**断言不等于 settled**。"""
    _intake(invoker)
    _settle(invoker)
    _approve(store)

    res = invoker.invoke("payment.execute", {
        "tenant_id": s6.TENANT_ID, "case_id": s6.CASE_ID, "gateway": TEST_GATEWAY,
    }, extras=_extras())
    assert res.status == "ok", res.error
    out = res.output

    case = guard.get_case(store, s6.TENANT_ID, s6.CASE_ID)
    assert case["biz_status"] == "processing"
    assert case["biz_status"] != "settled", (
        "发起方不许宣布成功：调用没抛异常 ≠ 钱退到客户账上")
    assert out["needs_query"] is True
    assert not out["receipt"]["is_terminal"], "refund() 永远不返回终态"
    assert objects.query(store, "SELECT * FROM refund_request WHERE case_id=?",
                         (s6.CASE_ID,)), "退款请求必须落库"
    # 此刻还不该有任何观察 —— 观察是 payment.observe 的产物。
    assert not objects.query(store, "SELECT * FROM payment_observation WHERE case_id=?",
                             (s6.CASE_ID,))


def test_payment_execute_refuses_without_approval(invoker, store, gateway):
    """没有审批记录就不许付款 —— 审批是人的动作，付款方不得自行补记。"""
    _intake(invoker)
    _settle(invoker)
    res = invoker.invoke("payment.execute", {
        "tenant_id": s6.TENANT_ID, "case_id": s6.CASE_ID, "gateway": TEST_GATEWAY,
    }, extras=_extras())
    assert res.status == "failed"
    assert "审批" in (res.error or "")
    assert guard.get_case(store, s6.TENANT_ID, s6.CASE_ID)["biz_status"] == "submitted"


def test_payment_execute_is_idempotent(invoker, store, gateway):
    """同一个案子重复发起不产生第二笔退款（幂等键 = out_request_no 的语义）。"""
    _intake(invoker)
    _settle(invoker)
    _approve(store)
    first = invoker.invoke("payment.execute", {
        "tenant_id": s6.TENANT_ID, "case_id": s6.CASE_ID, "gateway": TEST_GATEWAY,
    }, extras=_extras())
    second = invoker.invoke("payment.execute", {
        "tenant_id": s6.TENANT_ID, "case_id": s6.CASE_ID, "gateway": TEST_GATEWAY,
    }, extras=_extras())
    assert first.status == "ok" and second.status == "ok", second.error
    assert first.output["request_id"] == second.output["request_id"]
    assert gateway.refund_count == 1, "同幂等键必须只产生一笔退款"


def test_payment_observe_is_the_only_settled_writer(invoker, store, gateway):
    """payment.observe 写入后才 settled，且 payment_observation 同事务存在。"""
    _intake(invoker)
    _settle(invoker)
    _approve(store)
    sent = invoker.invoke("payment.execute", {
        "tenant_id": s6.TENANT_ID, "case_id": s6.CASE_ID, "gateway": TEST_GATEWAY,
    }, extras=_extras())
    assert guard.get_case(store, s6.TENANT_ID, s6.CASE_ID)["biz_status"] == "processing"

    res = invoker.invoke("payment.observe", {
        "tenant_id": s6.TENANT_ID, "case_id": s6.CASE_ID, "gateway": TEST_GATEWAY,
        "request_id": sent.output["request_id"],
    }, extras=_extras())
    assert res.status == "ok", res.error
    out = res.output
    assert out["settled"] is True
    assert out["poll_count"] == s6.SETTLE_AFTER, (
        f"终态必须是问出来的：settle_after={s6.SETTLE_AFTER} 时应问 {s6.SETTLE_AFTER} 次，"
        f"实际 {out['poll_count']} 次；一次就到终态说明网关被换成了同步桩")

    case = guard.get_case(store, s6.TENANT_ID, s6.CASE_ID)
    assert case["biz_status"] == "settled"
    obs = objects.query(store, "SELECT * FROM payment_observation WHERE case_id=?", (s6.CASE_ID,))
    assert len(obs) == 1, "settled 必须与回执同事务落库"
    assert obs[0]["observed_state"] == "settled"
    assert obs[0]["actor_invocation_id"], "回执必须能溯源到是哪一次调用观察到的"
    assert json.loads(obs[0]["raw_receipt_json"])["poll_count"] == s6.SETTLE_AFTER


def test_finance_actor_cannot_write_settled(invoker, store):
    """论证：非权威写入方写 settled -> 抛 AuthoritativeFactViolation **且事件落库**。

    这是「系统拒绝了一次越权写入」的证据本身 —— 吞掉异常就没有这份证据了。
    """
    _intake(invoker)
    with pytest.raises(guard.AuthoritativeFactViolation):
        guard.update_biz_status(store, s6.TENANT_ID, s6.CASE_ID, "settled",
                                "finance.settle", "iv-finance-illegal")

    events = [e for e in store.list_event_log("plan-test")
              if e["event_type"] == guard.VIOLATION_EVENT]
    assert events, "越权尝试必须留痕"
    detail = events[-1]["detail"]
    assert detail["actor"] == "finance.settle"
    assert detail["attempted"] == "settled"
    assert detail["authoritative_writer"] == guard.AUTHORITATIVE_WRITER
    assert guard.get_case(store, s6.TENANT_ID, s6.CASE_ID)["biz_status"] == "submitted"


def test_notify_missing_ack_is_followup_not_blocking(invoker, store):
    """ack 缺失 -> needs_followup，但 skill 仍然 ok（不阻塞）。"""
    _intake(invoker)
    res = invoker.invoke("notify.customer", {
        "tenant_id": s6.TENANT_ID, "case_id": s6.CASE_ID, "channel": "sms",
    }, extras=_extras())
    assert res.status == "ok", res.error
    assert res.output["needs_followup"] is True
    assert res.output["acked"] is False

    rows = objects.query(store, "SELECT * FROM notification WHERE case_id=?", (s6.CASE_ID,))
    assert len(rows) == 1
    assert rows[0]["ack_at"] is None, "不许拿 sent_at 顶替 ack_at"

    acked = invoker.invoke("notify.customer", {
        "tenant_id": s6.TENANT_ID, "case_id": s6.CASE_ID, "channel": "email", "ack": True,
    }, extras=_extras())
    assert acked.output["acked"] is True and acked.output["needs_followup"] is False


# ======================================================================
# 场景 6 端到端
# ======================================================================
def test_scenario_6_end_to_end(monkeypatch):
    """跑完整场景，再对着它自己那份库断言 —— 断言的是演示现场看到的同一份数据。"""
    captured = {}
    real_store_cls = flows_common.SqliteStore

    def _capture(*args, **kwargs):
        st = real_store_cls(*args, **kwargs)
        captured["store"] = st
        return st

    monkeypatch.setattr(flows_common, "SqliteStore", _capture)
    assert s6.run() == 0
    store = captured["store"]

    plans = objects.query(store, "SELECT * FROM plan")
    assert len(plans) == 1 and plans[0]["state"] == PlanState.DONE

    case = guard.get_case(store, s6.TENANT_ID, s6.CASE_ID)
    assert case["biz_status"] == "settled"
    assert objects.query(store, "SELECT * FROM payment_observation WHERE case_id=?",
                         (s6.CASE_ID,)), "payment_observation 必须存在"
    assert objects.query(store, "SELECT * FROM business_ref"), "business_ref 不能为空"
    assert objects.query(store, "SELECT * FROM finance_entry WHERE case_id=?", (s6.CASE_ID,))

    # ack 缺失没有把 Plan 卡住 —— 这条和上面的 DONE 合起来才是「不阻塞」的完整证据。
    notes = objects.query(store, "SELECT * FROM notification WHERE case_id=?", (s6.CASE_ID,))
    assert notes and notes[0]["ack_at"] is None

    # 审批发生过，且发生在付款之前（approval_record 是 payment.execute 的前置条件）。
    assert C.approvals_of(store, tenant_id=s6.TENANT_ID, case_id=s6.CASE_ID)


def test_scenario_6_is_deterministic(monkeypatch):
    """连跑两次，状态迁移轨迹必须逐条一致 —— 控制面行为不依赖模型的智力表现。"""
    def _trace():
        captured = {}
        real_cls = flows_common.SqliteStore

        def _capture(*a, **k):
            st = real_cls(*a, **k)
            captured["store"] = st
            return st

        monkeypatch.setattr(flows_common, "SqliteStore", _capture)
        assert s6.run() == 0
        store = captured["store"]
        plan_id = objects.query(store, "SELECT plan_id FROM plan")[0]["plan_id"]
        return [(e["task_id"], e["from_state"], e["to_state"], e["reason"])
                for e in store.list_event_log(plan_id)
                if e["event_type"] == "StateTransition"]

    assert _trace() == _trace()


# ======================================================================
# 论证性断言 —— 复赛材料里那句话的机器化版本
# ======================================================================
def test_manager_and_reviewer_participate_unchanged():
    """论证：Manager / Reviewer 一行新代码都没写，就参与了退款域编排。

    判据分两层：
      · 结构层 —— 两个角色的源码里不含任何退款域字样，identity 也没被加进本域 skill；
      · 行为层 —— 场景 6 确实用它们规划了 DAG、出了语义审查意见书（见端到端用例）。

    只验结构会漏掉「其实没被调用」，只验行为会漏掉「偷偷改了它们」，两层都要。
    """
    for path in (MAOS_PKG / "agents" / "manager.py", MAOS_PKG / "agents" / "reviewer.py"):
        src = path.read_text(encoding="utf-8")
        assert "refund" not in src.lower(), (
            f"{path.name} 出现了退款域字样 —— 角色抽象一旦按业务域特判，"
            "「换域只换 Skill」就不成立了")

    for cls in (ManagerAgent, ReviewerAgent):
        assert not any(s in cls.identity.allowed_skills for s in REFUND_SKILLS), \
            f"{cls.__name__} 的白名单被塞进了退款域 skill"

    # 场景 6 的脚本里确实给这两个角色备了应答 —— 它们真的被调用了。
    assert "语义审查产物清单" in s6.SCRIPT and "用户请求" in s6.SCRIPT


def test_kernel_does_not_know_the_refund_domain():
    """论证（铁律 9 推论）：runtime / core / contracts 不许 import 退款域。

    一旦内核 import 了具体业务域，「换域只换 Skill / ToolPort / 业务对象」当场作废 ——
    而那是复赛材料里最核心的一句。这条断言就是那句话的守门人。
    """
    offenders = []
    for sub in ("runtime", "core", "contracts"):
        for path in sorted((MAOS_PKG / sub).rglob("*.py")):
            src = path.read_text(encoding="utf-8")
            if "domain.refund" in src or "domain import refund" in src:
                offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, f"内核 import 了退款域：{offenders}"


def test_no_bypass_writes_settled():
    """论证：全仓库只有 payment.observe 调得出 `update_biz_status(..., "settled", ...)`。

    等价于派单验收里那条 grep：
        grep -rn "biz_status.*=.*'settled'" maos/ | grep -v guard.py | grep -v observe
    做成测试是因为 grep 只在有人想起来跑的时候才拦得住。

    判据走 AST 而不是文本匹配：按行 grep 会把 docstring 里「本 skill 写不出 settled」
    这类散文当成违规，于是这条断言迟早被人改宽或删掉 —— 一条会误报的守卫等于没有守卫。
    """
    allowed = {"maos/skills/builtin/refund/payment_observe.py"}
    offenders = []
    for path in sorted(MAOS_PKG.rglob("*.py")):
        rel = str(path.relative_to(REPO_ROOT))
        if rel in allowed or rel.startswith("maos/tests/"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name != "update_biz_status":
                continue
            args = list(node.args) + [kw.value for kw in node.keywords]
            if any(isinstance(a, ast.Constant) and a.value == "settled" for a in args):
                offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, f"settled 的写入出现在预期之外的位置：{offenders}"


def test_refund_case_writes_have_no_bypass():
    """论证：没有第二条写 refund_case 的路径 —— 运行时拦截也在（不只是 grep）。"""
    st = SqliteStore()
    st.init_schema()
    objects.ensure_schema(st)
    with pytest.raises(objects.BypassedGuardError):
        objects.execute(st, "UPDATE refund_case SET biz_status='settled' WHERE case_id=?",
                        ("whatever",))


def test_scenario_6_assembles_through_build_and_runs_without_key():
    """论证：场景 6 走冻结的 build() 六元组装配，且强制 Scripted —— 没有 key 也看得到退款域。

    `force_scripted=True` 不是省钱：评审现场那台机器如果配了 key，场景就会开始打真网络，
    而这条链路要证明的是控制面行为的确定性，不是模型的发挥（A-12）。
    """
    src = (MAOS_PKG / "flows" / "scenario_6.py").read_text(encoding="utf-8")
    tree = ast.parse(src, filename="scenario_6.py")
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]

    def _called(name: str) -> list[ast.Call]:
        out = []
        for n in calls:
            f = n.func
            got = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if got == name:
                out.append(n)
        return out

    assert _called("build"), "装配必须走 build() 的冻结六元组，不许内联拼装"
    assert not _called("SqliteStore"), "场景不自己造 store"
    forced = [n for n in _called("select_model_client")
              if any(kw.arg == "force_scripted" and kw.value.value is True
                     for kw in n.keywords)]
    assert forced, "场景 6 必须 force_scripted=True，否则配了 key 的机器上会走真网络"
