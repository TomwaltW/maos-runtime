"""场景 10 端到端 —— 顺利路径与失败路径，以及本轨要买的那两件东西。

    1. **权威事实边界成立**：全系统只有 `ap.observe` 写得进 settled，越权不静默失败。
    2. **「Agent 都说完成了」不等于业务成功**：失败路径上四个 Agent 全回 ok，
       而案子确实没成。

用例对**库里的行**下断言（settled 观察几条、补偿记录几行、Task 迁移落在哪张表里），
不只看退出码 —— 退出码是场景自己的断言给的，拿它当判据等于让被测者给自己判分。

场景的 `drive_happy()` / `drive_failure()` 只跑不断言，正是为此拆出来的：
测试不再拼第二份流程，两边不会漂。
"""

from __future__ import annotations

import pytest

from maos.contracts.states import TASK_TRANSITIONS, PlanState, TaskState
from maos.domain.ap import guard, objects
from maos.flows import scenario_10 as s10
from maos.skills.builtin.ap import AP_SKILLS
from maos.skills.builtin.ap.compensate import (
    KIND_INSTRUCTION_REVOKED,
    KIND_RECONCILIATION_TICKET,
    UNOBSERVED,
)
from maos.skills.registry import get as skill_get
from maos.tools.ap import ADVICE_FIELD, TERMINAL_STATUSES

# 两条路径各跑一次就够 —— 它们都建自己的 SqliteStore，互不影响，
# 但一条路径跑两遍是纯浪费（场景里有真实的驱动循环）。
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


@pytest.fixture(scope="module")
def happy():
    return s10.drive_happy()


@pytest.fixture(scope="module")
def failure():
    return s10.drive_failure()


# ------------------------------------------------------------------ 顺利路径
def test_happy_path_settles_only_after_the_bank_says_so(happy):
    """顺利路径：银行给了流水号，`settled` 才落库。"""
    store = happy["store"]
    case = guard.get_case(store, s10.TENANT_ID, s10.CASE_OK)
    assert case["biz_status"] == "settled"

    obs = guard.observations_of(store, s10.TENANT_ID, s10.CASE_OK)
    assert len(obs) == 1, "settled 必须恰好有一条终态观察兜底"
    assert obs[0]["observed_state"] == "settled"
    assert obs[0]["bank_reference"], (
        "settled 的观察必须带银行流水号 —— 没有流水号的「已付」在财务上对不了账")
    assert obs[0]["value_date"], "终态观察必须带起息日"
    assert obs[0]["actor_invocation_id"], "观察必须带 actor 锚点，否则审计链断了"


def test_happy_path_terminal_state_was_asked_for_not_assumed(happy):
    """终态是**问出来的**：受理回单非终态，且轮询了不止一次。

    这两条一起支撑 `ap.observe` 的存在理由。任一条塌了，整条论证跟着塌。
    """
    assert happy["instruction"][ADVICE_FIELD]["status"] == "accepted"
    assert happy["instruction"][ADVICE_FIELD]["is_terminal"] is False
    assert happy["advice"]["poll_count"] == s10.SETTLE_AFTER_OK > 1


def test_happy_path_pays_the_amount_we_computed(happy):
    """付出去的钱是**按规则算出来的**，不是抄发票上的数字。

    两者数额相同（BR-CO-16 就在判它），但依据不同 —— 而依据才是可核对的那部分。
    """
    assert happy["match"]["matched"] is True
    assert happy["plan"]["payable_amount"] == happy["match"]["payable_amount"]
    assert happy["instruction"]["amount"] == happy["match"]["payable_amount"]
    rows = objects.query(
        happy["store"], "SELECT amount, revoked FROM payment_instruction WHERE"
        " tenant_id=? AND case_id=?", (s10.TENANT_ID, s10.CASE_OK))
    assert len(rows) == 1 and rows[0]["revoked"] == 0
    assert rows[0]["amount"] == happy["match"]["payable_amount"]


def test_happy_path_stopped_for_a_human_before_paying(happy):
    """付款计划那一步 effect_risk=H，Gate 过了也停过 BLOCKED 等人放行。"""
    log = happy["cp"].store.list_event_log(happy["plan_id"])
    blocked = [e for e in log
               if e["event_type"] == "StateTransition"
               and e["task_id"] == s10.TASK_PLAN
               and e["to_state"] == TaskState.BLOCKED]
    assert blocked, "付款计划必须停在 BLOCKED 等人 —— 出账不可逆"
    approvals = objects.query(
        happy["store"], "SELECT * FROM payment_approval WHERE tenant_id=? AND case_id=?"
        " AND decision='approved'", (s10.TENANT_ID, s10.CASE_OK))
    assert approvals and approvals[0]["approver"] == s10.APPROVER
    assert happy["cp"].store.get_plan(happy["plan_id"])["state"] == PlanState.DONE


def test_happy_path_tolerances_were_actually_exercised(happy):
    """靶场里的差异**真的落在容差里**，不是三单完全相等蒙混过关。

    全都写成整齐相等的话，「容差」这三个字在场景里就没有演出来 ——
    而演它正是派单点名要的东西。
    """
    lines = {ln[0]: ln for ln in s10.LINES_OK}
    # 第 1 行：发票单价比订单高 0.01，正好等于单价容差。
    assert lines[1][3] != lines[1][7], "第 1 行应当有单价差"
    assert abs(float(lines[1][7]) - float(lines[1][3])) == pytest.approx(0.01)
    # 第 2 行：收货比发票多 0.4 件，在数量容差 0.5 之内。
    assert lines[2][4] != lines[2][6], "第 2 行应当有数量差"
    assert abs(lines[2][4] - lines[2][6]) == pytest.approx(0.4)
    assert happy["match"]["matched"] is True, "这两处差异都在容差内，应当放过"


# ------------------------------------------------------------------ 失败路径
def test_failure_path_writes_nothing_when_the_bank_stays_silent(failure):
    """**一个字都不写**：轮询到顶没问出终态时，观察表是空的。

    「我问累了」和「银行说没付成」是两回事。躺一行伪造的 failed 比什么都不写坏得多：
    它会让一笔实际已经付出去的钱在账上变成「未付」，然后被再付一次。
    """
    store = failure["store"]
    assert failure["advice"]["poll_count"] == s10.MAX_POLLS_STUCK
    assert failure["advice"]["settled"] is False
    assert failure["advice"]["observed_state"] != "settled"
    assert objects.query(store, "SELECT COUNT(*) AS n FROM ap_payment_observation"
                         )[0]["n"] == 0, "问不出终态就一条观察都不该有"


def test_failure_path_never_enters_settled(failure):
    """全库 settled 观察 0 条，业务状态收在 compensated。"""
    store = failure["store"]
    case = guard.get_case(store, s10.TENANT_ID, s10.CASE_BAD)
    assert case["biz_status"] == "compensated"
    assert objects.query(
        store, "SELECT COUNT(*) AS n FROM ap_payment_observation"
               " WHERE observed_state='settled'")[0]["n"] == 0
    # 业务状态机上「从没经过 settled」是可查的：状态变更事件里没有它。
    to_settled = [e for e in store.list_event_log(failure["plan_id"])
                  if e["event_type"] == "ApBizStatusChanged"
                  and e["to_state"] == "settled"]
    assert not to_settled, "全程不该有任何一次进入 settled 的迁移"


def test_failure_path_all_four_agents_reported_ok(failure):
    """本轨要买的第二件东西：**四个 Agent 全回 ok，而案子确实没成**。

    `AgentOutput.status` 说的是「这一步跑完了没有」，不是「业务成功了没有」。
    业务成没成看的是 `ap_case.biz_status` 与银行回单。
    """
    statuses = failure["agent_status"]
    assert len(statuses) == 4, f"四个任务都该有 Agent 自述，实际 {statuses}"
    assert set(statuses.values()) == {"ok"}, f"四个都该回 ok，实际 {statuses}"
    assert set(statuses) == {s10.TASK_INTAKE_B, s10.TASK_MATCH_B,
                             s10.TASK_PLAN_B, s10.TASK_PAY_B}

    # 而业务确实没成 —— 这两句必须同时成立，本用例才有意义。
    case = guard.get_case(failure["store"], s10.TENANT_ID, s10.CASE_BAD)
    assert case["biz_status"] == "compensated"
    assert failure["cp"].store.get_plan(failure["plan_id"])["state"] == PlanState.FAILED


def test_failure_path_match_passed_so_the_failure_is_not_a_match_failure(failure):
    """失败路径的三单是**对得上**的 —— 失败在银行回单问不出来。

    匹配就挂掉的话只演出了「有一步失败了」，那是另一件事，买不到上一条用例
    要买的那句话。
    """
    assert failure["match"]["matched"] is True
    assert failure["match"]["findings"] == []


def test_failure_path_compensation_does_not_declare_the_money_unpaid(failure):
    """补偿的语义是「不再推进」，**不是**「这笔钱确认没付」。

    走到补偿的场景里那一笔**可能已经划出去了**。写成 failed 会让账面上凭空少一笔，
    而供应商那边收到了钱 —— 下个月对账时这笔差额没有人查得清是哪来的。
    """
    comp = failure["compensation"]
    # 判据写成**字面量**，不写成 `== UNOBSERVED`：后者两边同源，把常量改成
    # "failed" 时断言仍然成立 —— 变异检验 M8 就是这么漏过去的。
    assert comp["last_observed_state"] == "unobserved", (
        f"一次都没观察到时应为 unobserved，实际 {comp['last_observed_state']!r}")
    assert comp["last_observed_state"] not in TERMINAL_STATUSES, (
        "补偿的最后观察不许是银行的终态说法 —— 银行什么都没说，"
        "写成 failed 就是替它下了它自己都没下的结论")
    assert UNOBSERVED not in TERMINAL_STATUSES, (
        "UNOBSERVED 这个占位本身就不许取银行的终态值")
    assert len(comp["revoked"]) == 1, "一张发票只允许有一笔付款指令"
    assert comp["ticket"]["assignee"] == s10.APPROVER
    assert comp["ticket"]["last_observed_state"] == UNOBSERVED
    assert "不要" in comp["ticket"]["todo"], "工单必须明说不许凭它断定钱没付出去"

    rows = objects.query(
        failure["store"], "SELECT kind, detail_json FROM ap_compensation_record"
        " WHERE tenant_id=? AND case_id=? ORDER BY kind",
        (s10.TENANT_ID, s10.CASE_BAD))
    kinds = {r["kind"] for r in rows}
    assert kinds == {KIND_INSTRUCTION_REVOKED, KIND_RECONCILIATION_TICKET}, (
        f"补偿必须同时留下作废记录与对账工单，实际 {sorted(kinds)}")
    revoked_rows = objects.query(
        failure["store"], "SELECT revoked FROM payment_instruction WHERE tenant_id=?"
        " AND case_id=?", (s10.TENANT_ID, s10.CASE_BAD))
    assert [r["revoked"] for r in revoked_rows] == [1]


def test_failure_path_compensation_left_an_event(failure):
    """补偿执行过了这件事不能只活在日志里。"""
    events = [e for e in failure["cp"].store.list_event_log(failure["plan_id"])
              if e["event_type"] == "CompensationExecuted"]
    assert len(events) == 1
    assert events[0]["detail"]["domain"] == guard.DOMAIN, (
        "与退款域共用事件类型时必须按 detail.domain 区分，否则查不出是哪个域")
    assert events[0]["detail"]["last_observed_state"] == UNOBSERVED


def test_settled_cases_refuse_compensation(happy):
    """已经 settled 的案子拒绝补偿 —— 那是数据被改坏的信号，不是一次空操作。"""
    from maos.skills.invoker import SkillInvoker

    res = SkillInvoker(s10.COMPENSATION_IDENTITY, happy["store"]).invoke(
        "ap.compensate", {"tenant_id": s10.TENANT_ID, "case_id": s10.CASE_OK,
                          "operator": "someone", "reason": "试图对已付案子补偿"})
    assert res.status == "failed" and "不许补偿" in res.error


# --------------------------------------------- ap.execute 的三道前置
# 这一组是**变异检验 M13 补上的**：把 `ap.execute` 里那句「没有 approved 就拒」
# 删掉之后，原先全部用例仍然全绿 —— 顺利路径那条只断言了「停过 BLOCKED」与
# 「库里有一条审批记录」，两者在没有前置校验时照样成立。
# 「人批过」这件事必须由**付款那一步自己**拒绝无审批的调用来保证，
# 不能靠流程恰好按顺序走过一遍。
@pytest.fixture()
def fresh():
    """一套干净的、三单已对得上、案子已 matched 的库 —— 只差审批。"""
    from maos.agents.base import AgentIdentity
    from maos.core.store import SqliteStore
    from maos.domain.ap import fixtures
    from maos.model.client import Tier
    from maos.skills.invoker import SkillInvoker
    from maos.tools.ap import MockBank

    store = SqliteStore()
    store.init_schema()
    objects.ensure_schema(store)
    s10.seed_supplier(store)
    fixtures.seed_three_way(
        store, tenant_id=s10.TENANT_ID, supplier_id=s10.SUPPLIER_ID, po_id="PO-X",
        gr_id="GR-X", invoice_id="INV-X", lines=s10.LINES_BAD,
        tax_category=s10.TAX_CATEGORY, tax_rate=s10.TAX_RATE,
        invoice_type=s10.INVOICE_TYPE, issued_at="2026-08-01T00:00:00+00:00")
    s10.C.reset_banks()
    s10.C.register_bank("t-bank", MockBank(settle_after=2))

    identity = AgentIdentity(
        agent_id="precondition-test", role="precondition-test", duty="用例专用",
        allowed_skills=frozenset(AP_SKILLS), write_scope=frozenset({"artifact"}),
        max_risk="H", model_tier=Tier.LIGHT)
    inv = SkillInvoker(identity, store)
    assert inv.invoke("ap.intake", {
        "tenant_id": s10.TENANT_ID, "case_id": "case-x", "invoice_id": "INV-X",
        "po_id": "PO-X", "po_version": 1, "gr_id": "GR-X"}).status == "ok"
    return store, inv


def test_execute_refuses_without_a_human_approval(fresh):
    """**没有人批过就不许把钱打出去** —— 前置 2。

    `ap.execute` 只读审批记录、不写：让付款方自己写下「我被批准了」等于没有审批。
    """
    store, inv = fresh
    assert inv.invoke("ap.match", {"tenant_id": s10.TENANT_ID,
                                   "case_id": "case-x"}).status == "ok"
    res = inv.invoke("ap.execute", {"tenant_id": s10.TENANT_ID, "case_id": "case-x",
                                    "bank": "t-bank"})
    assert res.status == "failed", "没有审批记录时必须拒绝发指令"
    assert "审批" in res.error
    assert objects.query(store, "SELECT COUNT(*) AS n FROM payment_instruction"
                         )[0]["n"] == 0, "被拒之后不该留下任何付款指令"
    assert guard.get_case(store, s10.TENANT_ID, "case-x")["biz_status"] == "matched"


def test_execute_refuses_before_the_three_way_match_passes(fresh):
    """**三单没对上就不许付钱** —— 前置 1，本域要拦的头一件事。

    连审批都有了也不行：审批批的是一份基于匹配结论的计划，匹配没跑过时那份
    计划根本不存在。
    """
    store, inv = fresh
    s10.C.record_approval(store, tenant_id=s10.TENANT_ID, case_id="case-x",
                          approver=s10.APPROVER, decision="approved")
    res = inv.invoke("ap.execute", {"tenant_id": s10.TENANT_ID, "case_id": "case-x",
                                    "bank": "t-bank"})
    assert res.status == "failed" and "matched" in res.error
    assert objects.query(store, "SELECT COUNT(*) AS n FROM payment_instruction"
                         )[0]["n"] == 0


def test_execute_is_idempotent_on_the_same_case(fresh):
    """**一张发票只允许有一笔付款指令** —— 前置 3。

    幂等键由 (租户, 案子) 唯一确定，返工重跑落在同一个键上。重跑之后库里仍然
    只有一行，银行账本上也只有一笔。
    """
    store, inv = fresh
    inv.invoke("ap.match", {"tenant_id": s10.TENANT_ID, "case_id": "case-x"})
    s10.C.record_approval(store, tenant_id=s10.TENANT_ID, case_id="case-x",
                          approver=s10.APPROVER, decision="approved")
    first = inv.invoke("ap.execute", {"tenant_id": s10.TENANT_ID, "case_id": "case-x",
                                      "bank": "t-bank"})
    assert first.status == "ok"
    rows = objects.query(store, "SELECT instruction_id, idempotency_key FROM"
                                " payment_instruction WHERE tenant_id=?",
                         (s10.TENANT_ID,))
    assert len(rows) == 1
    assert rows[0]["idempotency_key"] == s10.C.idempotency_key(s10.TENANT_ID, "case-x")
    # 银行账本上也只有一笔 —— 幂等键挡住了第二笔。
    assert len(s10.C.get_bank("t-bank")._ledger) == 1


def test_execute_never_accepts_a_terminal_advice_from_the_bank(fresh, monkeypatch):
    """银行受理回单若是终态，`ap.execute` 当场断。

    这不是防御性编程：换成真银行适配器时，一个把「受理」当「已付」返回的实现会让
    `ap.observe` 失去存在理由，而症状是「一切正常，钱好像也付了」。
    """
    from maos.tools import ap as ap_tools

    store, inv = fresh
    inv.invoke("ap.match", {"tenant_id": s10.TENANT_ID, "case_id": "case-x"})
    s10.C.record_approval(store, tenant_id=s10.TENANT_ID, case_id="case-x",
                          approver=s10.APPROVER, decision="approved")

    class LyingBank(ap_tools.MockBank):
        def pay(self, instruction):
            advice = super().pay(instruction)
            return ap_tools.replace(advice, status=ap_tools.STATUS_SETTLED,
                                    bank_reference="fake")

    s10.C.register_bank("liar", LyingBank(settle_after=2))
    res = inv.invoke("ap.execute", {"tenant_id": s10.TENANT_ID, "case_id": "case-x",
                                    "bank": "liar"})
    assert res.status == "failed" and "不该是终态" in res.error


# ------------------------------------------------ 铁律 9：业务状态不进状态机
@pytest.mark.parametrize("which", ["happy", "failure"])
def test_business_status_never_becomes_a_task_state(request, which):
    """两条路径都不许出现表外 Task 状态或表外迁移。

    断言两件事而不是一件：只查状态集合挡不住「用既有的两个状态连一条新边」。
    """
    out = request.getfixturevalue(which)
    cp, plan_id = out["cp"], out["plan_id"]
    known = {v for k, v in vars(TaskState).items()
             if not k.startswith("_") and isinstance(v, str)}
    states = {t["state"] for t in cp.store.list_tasks(plan_id)}
    assert states <= known, f"出现了表外 Task 状态：{sorted(states - known)}"
    for biz in guard.BIZ_STATUS_FLOW:
        assert biz not in states, f"{biz} 是 ap_case 的字段，不许变成 Task 状态"
    moves = {(e["from_state"], e["to_state"])
             for e in cp.store.list_event_log(plan_id)
             if e["event_type"] == "StateTransition"}
    assert moves <= set(TASK_TRANSITIONS), (
        f"出现了表外 Task 迁移：{sorted(moves - set(TASK_TRANSITIONS))}")


# ------------------------------------------------------------ 投放即注册
def test_all_ap_skills_are_registered_without_touching_builtin_init():
    """六个 skill 靠 `@register_skill` 自动进注册表（冻结契约 C-1）。"""
    for name in AP_SKILLS:
        assert skill_get(name) is not None, f"{name} 没进注册表"


def test_all_ap_agents_are_registered_without_touching_agents_init():
    """四个 Agent 靠 `@register` 自动进 AGENT_POOL（冻结契约 C-2）。"""
    from maos.agents import AGENT_POOL
    from maos.agents.ap import AP_ROLES

    for role in AP_ROLES:
        assert role in AGENT_POOL, f"角色 {role} 没进 AGENT_POOL"
    assert len(set(AP_ROLES)) == 4


def test_ap_agents_have_least_privilege():
    """最小授权：只有资金岗碰得到银行，别的角色 `allowed_tools` 是空的。"""
    from maos.agents.ap import (
        ApControlAgent,
        ApIntakeAgent,
        ApMatchAgent,
        ApTreasuryAgent,
    )

    for cls in (ApIntakeAgent, ApMatchAgent, ApControlAgent):
        assert cls.identity.allowed_tools == frozenset(), (
            f"{cls.__name__} 不该有任何工具权限 —— 只有资金岗碰银行")
    assert ApTreasuryAgent.identity.allowed_tools == frozenset({"bank.pay", "bank.query"})
    # 补偿是**人的决定之后**的动作，不属于任何 Agent；它的权限挂在编排层 identity 上。
    assert s10.COMPENSATION_IDENTITY.allowed_skills == frozenset({"ap.compensate"})
    for cls in (ApIntakeAgent, ApMatchAgent, ApControlAgent, ApTreasuryAgent):
        assert "ap.compensate" not in cls.identity.allowed_skills, (
            f"{cls.__name__} 不该能自己做补偿收口")


def test_scenario_10_is_not_wired_into_default_scenarios():
    """本场景刻意不进 `DEFAULT_SCENARIOS` —— 同期三轨都新增 flow，谁改谁冲突。

    这条断言是给整合轮看的：接进缺省序列时它会红，那正是提醒改这里的时机。
    """
    from maos.main import DEFAULT_SCENARIOS

    assert 10 not in DEFAULT_SCENARIOS, (
        "场景 10 接进缺省序列了 —— 若是整合轮有意为之，把本条断言一并改掉")
