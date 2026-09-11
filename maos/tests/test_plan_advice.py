"""规划建议（`maos/kb/plan_advice.py`）与第四条护栏。

评委第二条的后半段是这一整个文件的来源：

    Planner 应能推荐必要任务、审批人和异常处理分支……历史知识只辅助规划，
    **不替代当前事实与人工授权**。

前半句是功能，后半句是护栏，两句必须一起测：一个能推荐任务却推荐得出「跳过审批」
的 Planner，比不会推荐的那个更坏 —— 它把一条越权动作包装成了「历史上就是这么做的」。

## 三条红线各自一条负例

  · **带订单事实字段被拒** —— 建议里出现 `order_id` / `biz_status` / `order_snapshot`
    这类字段，就是拿历史那一单的事实冒充这一单的（铁律 8：MAOS 不持有权威事实）。
  · **预算放松被拒** —— 能把 `retry_budget` 调大的建议，等于让知识层决定「再自旋
    几次」，而无限重试正是评委点名的反模式。只许更紧。
  · **审批人为空被拒** —— 空审批人不是「不需要审批」，是「没人知道该谁批」，
    而这两件事在下游长得一模一样：按岗收窄的动作静默落空，且不报错。

## 为什么护栏要能吃两种形状

`assert_advice_within_bounds` 收 `PlanAdvice` 对象也收它的 `to_dict()`：建议在进
事件**之前**判一次（Manager 那条路），从 `event_log` 读回来**之后**还要能再判一次
（控制面按 `PlanAdvised.retry_budget` 收紧重试额度时）。两处判的必须是同一条判据，
否则事件里写什么就信什么 —— 而事件是可以被别处写进去的。
"""

from __future__ import annotations

import ast
import json
import pathlib

import pytest

from maos import kb
from maos.kb import guardrails, plan_advice
from maos.kb.guardrails import GuardrailViolation
from maos.kb.plan_advice import PlanAdvice

ENV_BUDGET = 2


# --------------------------------------------------------------------- 夹具
def _advice(**over) -> PlanAdvice:
    """一条**站得住**的建议。每条负例只改一个字段，差异因此是明确的。"""
    base = {
        "required_tasks": [{"role": "refund_channel", "title": "渠道商核销",
                            "reason": "政策规则 AS-004@v1 要求这一步",
                            "doc_id": "kb-policy-tnt-mfg-a-AS-004-v1"}],
        "approver_role": "region_manager",
        "exception_branches": [{"trigger": plan_advice.TRIGGER_GATEWAY_CODE,
                                "action": "入口即拒，退避后可按原请求号重发",
                                "doc_id": "kb-ecp-mfga-40005"}],
        "retry_budget": 1,
        "citations": ["kb-policy-tnt-mfg-a-AS-004-v1", "kb-ecp-mfga-40005"],
    }
    base.update(over)
    return PlanAdvice(**base)


BASELINE = {"env_max_replan": ENV_BUDGET}


def _policy_rule(**over) -> dict:
    """`policy_view()` 那个形状的一行规则，外加可选的 `doc_id`。"""
    params = {"approver_role": "region_manager", "channel_kind": "dealer",
              "extra_tasks": [{"owner_role": "refund_channel",
                               "task_key": "dealer_writeoff", "title": "渠道商核销"}]}
    rule = {"rule_no": "AS-004", "version": 1, "title": "经销渠道差异",
            "ref": "AS-004@v1", "params": params,
            "doc_id": "kb-policy-tnt-mfg-a-AS-004-v1"}
    rule.update(over)
    return rule


def _hit(doc: dict) -> dict:
    """`retriever.score_candidates` 那个形状的命中。`doc` 是整行。"""
    return {"doc_id": doc["doc_id"], "score": 0.5, "title": doc.get("title", ""),
            "kind": doc.get("kind"), "outcome": None, "source_case_id": None,
            "channels": {}, "doc": doc}


def _task_pattern_doc() -> dict:
    return {"doc_id": "kb-tp-x-dealer", "kind": kb.KIND_TASK_PATTERN,
            "title": "经销渠道：多一步渠道商核销", "tenant_id": "tnt-x",
            "rule_no": None, "gateway_code": None, "policy_version": None,
            "body": json.dumps({"steps": [
                {"role": "refund_channel", "title": "渠道商核销",
                 "inputs": {"task_key": "dealer_writeoff", "rule_ref": "AS-004@v1"}},
            ]}, ensure_ascii=False)}


def _playbook_doc(*, code="40005", max_attempts=3) -> dict:
    return {"doc_id": f"kb-ecp-x-{code}", "kind": kb.KIND_ERROR_CODE_PLAYBOOK,
            "title": f"支付错误码 {code}", "tenant_id": "tnt-x",
            "rule_no": None, "gateway_code": code, "policy_version": None,
            "body": json.dumps({
                "disposition": "入口即拒，退避后可按原请求号直接重发",
                "remedy_steps": ["按退避表等待", "原请求号重发"],
                "backoff": {"max_attempts": max_attempts},
            }, ensure_ascii=False)}


def _failure_row(**over) -> dict:
    row = {"tenant_id": "tnt-x", "channel_id": "ch-dealer", "gateway_code": "40005",
           "rule_no": "AS-004", "extra_steps": ["网关 40005：降低请求并发量"], "count": 2}
    row.update(over)
    return row


# ============================================================ 护栏 4（10 条）
def test_advice_carrying_an_order_fact_field_is_rejected():
    """负例 1：建议任务的 inputs 里带 `order_id`。

    历史案例里的订单号是**那一单**的事实。抄到当前 case 上，下游按它去查快照、
    去对账，查到的是别人的单 —— 而且一路不报错。
    """
    bad = _advice(required_tasks=[{"role": "refund_channel", "title": "渠道商核销",
                                   "inputs": {"order_id": "ord-别人的单"},
                                   "reason": "抄的", "doc_id": "d1"}])
    with pytest.raises(GuardrailViolation, match="order_snapshot"):
        guardrails.assert_advice_within_bounds(bad, BASELINE)


def test_advice_writing_biz_status_is_rejected():
    """负例 2：建议想直接写 `biz_status`。

    业务状态的权威在外部系统（铁律 8）。一条把它写死为终态的建议，等于让历史
    替当前这一单宣布「已经退成了」。
    """
    bad = _advice(required_tasks=[{"role": "refund_payment", "title": "直接收口",
                                   "biz_status": "settled",
                                   "reason": "历史上都这样", "doc_id": "d1"}])
    with pytest.raises(GuardrailViolation, match="铁律 8"):
        guardrails.assert_advice_within_bounds(bad, BASELINE)


def test_advice_writing_order_snapshot_is_rejected():
    """负例 3：建议想往 `order_snapshot` 上写。判据与上一条同源，落点不同。"""
    bad = _advice(required_tasks=[{"role": "refund_policy", "title": "改一下快照",
                                   "inputs": {"order_snapshot": {"amount_paid": 1}},
                                   "reason": "", "doc_id": "d1"}])
    with pytest.raises(GuardrailViolation, match="order_snapshot"):
        guardrails.assert_advice_within_bounds(bad, BASELINE)


def test_relaxing_the_retry_budget_is_rejected():
    """负例 4：建议把预算从 env 的 2 放宽到 5。

    这是「无限重试」那条反模式在知识层的入口：历史上某一单重发七次终于成了，
    于是建议「再多试几次」。只许更紧。
    """
    with pytest.raises(GuardrailViolation, match="只许收紧"):
        guardrails.assert_advice_within_bounds(_advice(retry_budget=5), BASELINE)


def test_tightening_the_retry_budget_is_allowed():
    """正例：收紧到 0 也放行 —— 「这个组合一次都别再发了」是合法建议。"""
    guardrails.assert_advice_within_bounds(_advice(retry_budget=0), BASELINE)


def test_an_empty_approver_role_is_rejected():
    """负例 5：审批人为空。人工授权永远在，空不是「不用批」。"""
    with pytest.raises(GuardrailViolation, match="人工授权永远在"):
        guardrails.assert_advice_within_bounds(_advice(approver_role=""), BASELINE)


def test_a_whitespace_approver_role_is_rejected():
    """负例 6：审批人是几个空格。

    单独一条而不是并进上一条：空串一眼看得出，而一个全是空格的角色名在日志里
    印出来与正常值没有区别 —— `.strip()` 那一步漏掉时，这条是唯一会红的。
    """
    with pytest.raises(GuardrailViolation, match="人工授权永远在"):
        guardrails.assert_advice_within_bounds(_advice(approver_role="   "), BASELINE)


def test_an_exception_branch_that_skips_approval_is_rejected():
    """负例 7：异常分支的处置里写了 `skip_approval`。

    异常分支是最容易藏这件事的地方：它描述的是「出事之后怎么办」，而「出事了就
    别等人批了」听上去甚至像是合理的应急口径。
    """
    bad = _advice(exception_branches=[{
        "trigger": plan_advice.TRIGGER_GATEWAY_CODE,
        "action": "超时就 skip_approval 直接重发", "doc_id": "d1"}])
    with pytest.raises(GuardrailViolation, match="免审批标记"):
        guardrails.assert_advice_within_bounds(bad, BASELINE)


def test_a_well_formed_advice_passes():
    """正例：一条完整的、站得住的建议全过。护栏不能严到把正常建议也拦下。"""
    guardrails.assert_advice_within_bounds(_advice(), BASELINE)


def test_the_guardrail_reads_the_dict_form_too():
    """护栏吃 `to_dict()` 的形状 —— 从 `event_log` 读回来的那一份长这样。

    控制面按 `PlanAdvised.retry_budget` 收紧额度时判的就是这个形状；两处判据
    不同源的后果是「事件里写什么就信什么」，而事件是可以被别处写进去的。
    """
    guardrails.assert_advice_within_bounds(_advice().to_dict(), BASELINE)
    with pytest.raises(GuardrailViolation, match="只许收紧"):
        guardrails.assert_advice_within_bounds(_advice(retry_budget=9).to_dict(), BASELINE)


def test_no_baseline_skips_only_the_budget_check():
    """不给 baseline 时只跳过预算那一条，其余三条照判。

    「没有参照物就不判」只适用于预算（分母确实不知道）；审批人与事实字段
    是建议**自身**的性质，缺参照物也判得了 —— 跟着一起跳过就是把护栏关掉。
    """
    guardrails.assert_advice_within_bounds(_advice(retry_budget=99), None)
    with pytest.raises(GuardrailViolation, match="人工授权永远在"):
        guardrails.assert_advice_within_bounds(_advice(approver_role=""), None)


def test_the_fourth_guardrail_does_not_touch_the_first_three():
    """第四条是**加**出来的，不是替代：三条旧断言的签名与行为一个字节没动。

    钉这条是因为契约明写「三条旧断言不动」，而「不动」这件事只有在它被真的
    调用过一次之后才算被验证过。
    """
    baseline = [{"role": "refund_intake", "title": "受理", "inputs": {"step": "intake"}}]
    with pytest.raises(GuardrailViolation, match="只许增加任务"):
        guardrails.assert_only_adds(baseline, [])
    with pytest.raises(GuardrailViolation, match="订单事实字段"):
        guardrails.assert_no_fact_override([{"role": "x", "inputs": {"order_id": "o1"}}])
    with pytest.raises(GuardrailViolation, match="免审批标记"):
        guardrails.assert_no_approval_skip(
            baseline, [{"role": "x", "inputs": {"skip_approval": True}}])


# ============================================================ advise 纯函数
def test_policy_rules_give_required_tasks_and_the_approver():
    """来源 1：政策规则的 `extra_tasks` / `approver_role`。

    这一条是**当前事实**不是历史知识 —— 政策要求的步骤不能没有，所以它排在
    `required_tasks` 最前面。
    """
    advice = plan_advice.advise(rules=[_policy_rule()], env_max_replan=ENV_BUDGET)
    assert advice.approver_role == "region_manager"
    assert advice.required_tasks[0]["role"] == "refund_channel"
    assert advice.required_tasks[0]["doc_id"] == "kb-policy-tnt-mfg-a-AS-004-v1"
    assert "AS-004@v1" in advice.required_tasks[0]["reason"]


def test_task_pattern_hits_become_required_tasks_with_a_doc_id():
    """来源 2：`task_pattern` 的步骤清单进 `required_tasks`，每条带出处。

    出处不是装饰：没有 `doc_id`，「凭什么多这一步」就只能答「模型觉得该有」。
    """
    advice = plan_advice.advise(hits=[_hit(_task_pattern_doc())],
                                env_max_replan=ENV_BUDGET)
    assert [t["role"] for t in advice.required_tasks] == ["refund_channel"]
    assert advice.required_tasks[0]["doc_id"] == "kb-tp-x-dealer"
    assert "kb-tp-x-dealer" in advice.citations


def test_playbook_hits_become_exception_branches_keyed_by_the_gateway_code():
    """来源 3：处置手册进 `exception_branches`，`trigger` 是码表里的那个码。

    码取自 `hit["doc"]["gateway_code"]` —— 它**不在命中对象顶层**
    （`retriever.score_candidates` 的形状），取错地方的症状是分支恒为空。
    """
    advice = plan_advice.advise(hits=[_hit(_playbook_doc())], env_max_replan=ENV_BUDGET)
    assert len(advice.exception_branches) == 1
    branch = advice.exception_branches[0]
    assert branch["trigger"] == plan_advice.TRIGGER_GATEWAY_CODE
    assert branch["doc_id"] == "kb-ecp-x-40005"
    assert "重发" in branch["action"]
    # 契约 §7 的分支逐字只有三键 —— 去重用的 code 不许跟着进事件 detail。
    assert set(branch) == {"trigger", "action", "doc_id"}


def test_a_failure_hint_tightens_the_budget_and_overrides_the_playbook():
    """来源 4：本租户在这个组合上栽过 -> 预算收到 1，并**覆盖**手册那条乐观处置。

    官方手册对 40005 写的是「退避后可按原请求号直接重发」。先到先得会让这条更
    乐观的留下，于是 Planner 照着一条被本地实测否掉的处置排异常分支 ——
    而屏幕上一切正常。
    """
    advice = plan_advice.advise(hits=[_hit(_playbook_doc())],
                                failure_rows=[_failure_row()],
                                env_max_replan=ENV_BUDGET)
    assert advice.retry_budget == plan_advice.FAILED_COMBO_RETRY_BUDGET == 1
    assert len(advice.exception_branches) == 1, "同一个码只该留一条处置"
    assert "转人工核实" in advice.exception_branches[0]["action"]
    assert advice.exception_branches[0]["doc_id"].startswith("failure_hint_index:")


def test_the_retry_budget_never_exceeds_the_env_ceiling():
    """手册说能发 3 次、env 只给 1 次 —— 取更紧的那个。

    `min` 的方向搞反了不会报错，只会让某个码悄悄拿到比配置更多的重发额度。
    """
    advice = plan_advice.advise(hits=[_hit(_playbook_doc(max_attempts=3))],
                                env_max_replan=1)
    assert advice.retry_budget == 1


def test_no_input_at_all_gives_the_env_budget_and_the_default_approver():
    """一条都没命中：预算就是 env（**不是 0**），审批人落缺省。

    「没有知识」不等于「不许重试」，也不等于「不用审批」。把空建议解读成 0
    会让一个冷启动的库把所有单子卡在第一次网关抖动上。
    """
    advice = plan_advice.advise(env_max_replan=ENV_BUDGET)
    assert advice.retry_budget == ENV_BUDGET
    assert advice.approver_role == plan_advice.DEFAULT_APPROVER_ROLE
    assert advice.is_empty()


def test_citations_are_deduped_and_cover_every_source():
    """四个来源的出处全进 `citations`，去重，且**顺序确定**。

    顺序确定不是排版问题：证据束要能连跑两次逐条一致。
    """
    advice = plan_advice.advise(
        rules=[_policy_rule(), _policy_rule()],
        hits=[_hit(_task_pattern_doc()), _hit(_playbook_doc())],
        failure_rows=[_failure_row()], env_max_replan=ENV_BUDGET)
    assert advice.citations == [
        "kb-policy-tnt-mfg-a-AS-004-v1", "kb-tp-x-dealer", "kb-ecp-x-40005",
        "failure_hint_index:ch-dealer:40005:AS-004",
    ]


def test_advise_does_not_mutate_the_hit_shape():
    """`advise` 一个字节都不改命中对象。

    `KbRetrieved` 的 detail 在 `emit_kb_retrieved` 里写死了消费侧；建议这一侧
    顺手往 hit 上挂个字段，证据里那条事件就跟着变形，而且没有任何症状。
    """
    hits = [_hit(_task_pattern_doc()), _hit(_playbook_doc())]
    before = json.dumps(hits, ensure_ascii=False, sort_keys=True)
    plan_advice.advise(hits=hits, env_max_replan=ENV_BUDGET)
    assert json.dumps(hits, ensure_ascii=False, sort_keys=True) == before


def test_advise_output_always_passes_its_own_guardrail():
    """`advise` 自己产出的建议，必定过得了第四条护栏。

    护栏与生成器同源的意义就在这里：拦得住的东西，生成器压根不该产出来。
    哪天有人往 `advise` 里加一条会放宽预算的来源，这条当场红。
    """
    advice = plan_advice.advise(
        rules=[_policy_rule()],
        hits=[_hit(_task_pattern_doc()), _hit(_playbook_doc())],
        failure_rows=[_failure_row()], env_max_replan=ENV_BUDGET)
    guardrails.assert_advice_within_bounds(advice, BASELINE)


# ============================================================ 同源与开关
def test_policy_directives_and_advise_share_one_implementation():
    """`contrast.policy_directives` 与 `advise` 算的是**同一个** `approver_role`。

    两份逻辑各算一遍是不合格的：分叉的症状是「屏幕上说区域经理批，事件里写
    主管批」—— 不报错。这条钉住它们同源，且 `policy_directives` 的返回形状
    一个字节没变（它有四个调用方）。
    """
    from maos.flows import contrast

    rules = [_policy_rule()]
    directives = contrast.policy_directives(rules)
    advice = plan_advice.advise(rules=rules, env_max_replan=ENV_BUDGET)

    assert directives["approver_role"] == advice.approver_role
    assert set(directives) == {"extra_tasks", "approver_role"}, "返回形状不许变"
    assert [s["task_key"] for s in directives["extra_tasks"]] == ["dealer_writeoff"]
    assert directives["extra_tasks"][0]["rule_ref"] == "AS-004@v1"
    assert contrast.DEFAULT_APPROVER_ROLE == plan_advice.DEFAULT_APPROVER_ROLE


def test_policy_directives_still_falls_back_to_the_default_approver():
    """没有任何规则声明审批人时落缺省 —— R4A（自营渠道）走的正是这一格。"""
    from maos.flows import contrast

    plain = _policy_rule(params={"refund_ratio": "1"})
    assert contrast.policy_directives([plain])["approver_role"] == \
        plan_advice.DEFAULT_APPROVER_ROLE
    assert contrast.policy_directives([])["extra_tasks"] == []


@pytest.mark.parametrize("value,expected", [
    ("0", False), ("false", False), ("no", False), ("OFF", False),
    ("1", True), ("", True), ("随便什么", True),
])
def test_the_advice_switch_only_turns_off_on_explicit_off_values(value, expected):
    """读不懂的值回落到「启用」。

    口径照抄 `kb.kb_enabled`：静默关掉建议的症状是「Planner 好像没那么聪明」，
    不是报错 —— 那是最难被发现的一种失效。
    """
    assert plan_advice.advice_enabled({plan_advice.KB_ADVICE_ENV: value}) is expected


def test_task_patterns_only_reshape_the_dag_when_advice_is_on():
    """`task_pattern` 改不改得动 DAG，由**规划面的开关**说了算，不由语料说了算。

    `kb.POSITIVE_KINDS` 一个字节没动 —— 那份清单答的是「哪类知识是正例」，
    与「哪类知识可以排任务」不是同一个问题（判据见
    `retriever.retrieve_task_patterns` 的 docstring）。
    """
    assert kb.KIND_TASK_PATTERN not in guardrails.planning_kinds(None)
    assert kb.KIND_TASK_PATTERN in guardrails.planning_kinds(_advice())

    baseline = [{"task_id": "t1", "role": "refund_intake", "title": "受理",
                 "inputs": {"step": "intake", "tenant_id": "tnt-x",
                            "case_id": "case-x", "biz_type": "refund"},
                 "depends_on": [], "risk_level": "L"}]
    docs = [{**_hit(_task_pattern_doc()), "body": _task_pattern_doc()["body"]}]

    _, added_off = guardrails.apply_suggestions(baseline, docs)
    assert added_off == [], "关掉建议时 task_pattern 不许改 DAG 的形状"

    _, added_on = guardrails.apply_suggestions(baseline, docs, advice=_advice())
    assert [t["role"] for t in added_on] == ["refund_channel"]
    assert added_on[0]["doc_id"] == "kb-tp-x-dealer", "补进 DAG 的任务要说得出出处"
    assert added_on[0]["reason"], "也要说得出为什么"


def test_a_suggested_task_does_not_inherit_someone_elses_amount():
    """补进来的任务**不会**凭空拿到申报金额 —— 除非历史上那一步本来就带着它。

    症状不是「闸不触发」，是「闸要一份它根本不产的凭据」：第六道闸的任务级判据
    按 `biz_type + amount_claimed` 触发，判的是同 attempt 的产物里有没有
    `finance_entry`。金额撒给经销渠道那一步核销，闸就恒 blocker，返工到额度耗尽，
    整个 Plan 跟着 FAILED —— 而两版 DAG 的任务清单看上去确实不同了。
    """
    baseline = [{"task_id": "t1", "role": "refund_intake", "title": "受理",
                 "inputs": {"step": "intake", "tenant_id": "tnt-x", "case_id": "case-x",
                            "biz_type": "refund", "amount_claimed": 9000.0},
                 "depends_on": [], "risk_level": "L"}]
    docs = [{**_hit(_task_pattern_doc()), "body": _task_pattern_doc()["body"]}]
    _, added = guardrails.apply_suggestions(baseline, docs, advice=_advice())

    assert added and "amount_claimed" not in added[0]["inputs"]
    # 身份字段照旧无条件补齐 —— 它们是当前 case 的，不是历史那一单的。
    assert added[0]["inputs"]["case_id"] == "case-x"
    assert added[0]["inputs"]["tenant_id"] == "tnt-x"


# ============================================================ 落事件与控制面
def test_plan_advised_lands_in_the_event_log_with_the_contract_fields():
    """`PlanAdvised` 的 detail 带 `tenant_id / case_id / plan_id` + 五个字段（契约 §F）。"""
    from maos.core.store import SqliteStore

    store = SqliteStore()
    store.init_schema()
    ctx = {"tenant_id": "tnt-x", "case_id": "case-x", "trace_id": "tr-x"}
    advice = plan_advice.advise_and_log(
        store, ctx, plan_id="plan-x", hits=[_hit(_playbook_doc())], env_max_replan=2)
    assert advice is not None

    rows = kb.query(store, "SELECT * FROM event_log WHERE event_type=?",
                    (plan_advice.PLAN_ADVISED_EVENT,))
    assert len(rows) == 1
    detail = json.loads(rows[0]["detail"])
    assert detail["tenant_id"] == "tnt-x"
    assert detail["case_id"] == "case-x"
    assert detail["plan_id"] == "plan-x"
    assert set(detail) == {"tenant_id", "case_id", "plan_id", "required_tasks",
                           "approver_role", "exception_branches", "retry_budget",
                           "citations"}


def test_the_switch_off_means_not_a_single_event(monkeypatch):
    """关掉建议就**一条事件都不落**。

    「关掉就一条事件都没有」是 R8 的判据之一：without_advice 那一段的 event_log
    里不该有任何 `PlanAdvised`，否则「有无建议」这条线本身就不干净。
    """
    from maos.core.store import SqliteStore

    monkeypatch.setenv(plan_advice.KB_ADVICE_ENV, "0")
    store = SqliteStore()
    store.init_schema()
    assert plan_advice.advise_and_log(store, {"tenant_id": "tnt-x"}, plan_id="p") is None
    assert kb.query(store, "SELECT * FROM event_log WHERE event_type=?",
                    (plan_advice.PLAN_ADVISED_EVENT,)) == []


def test_max_replan_takes_the_tighter_of_env_and_the_advice(monkeypatch):
    """控制面的重试额度 = `min(MAOS_MAX_REPLAN, 建议的 retry_budget)`。

    这是护栏 4 之外的**第二道**：事件是可以被别处写进去的，控制面不拿从
    `event_log` 读来的一个数当上限用。
    """
    from maos.core.control_plane import ControlPlane, ENV_MAX_REPLAN
    from maos.core.eventbus import InMemoryEventBus
    from maos.core.store import SqliteStore

    monkeypatch.setenv(ENV_MAX_REPLAN, "2")
    store = SqliteStore()
    store.init_schema()
    cp = ControlPlane(store, InMemoryEventBus())

    assert cp._max_replan() == 2, "不给 plan_id 时逐字节等于旧行为"
    assert cp._max_replan("plan-none") == 2, "没有建议就按 env"

    # 失败聚合走 T120 的真实写入方，不在测试里手捏一行 —— 这一条顺带证明
    # `_failure_rows` 真的读得到那张表（表不在时它返回空且不抛，那一支静默）。
    from maos.kb.promotion import bump_failure_hint

    row = _failure_row()
    bump_failure_hint(store, tenant_id=row["tenant_id"], channel_id=row["channel_id"],
                      gateway_code=row["gateway_code"], rule_no=row["rule_no"],
                      extra_steps=row["extra_steps"])
    plan_advice.advise_and_log(
        store, {"tenant_id": "tnt-x", "channel_id": "ch-dealer"}, plan_id="plan-tight",
        hits=[_hit(_playbook_doc())], env_max_replan=2)
    assert cp._max_replan("plan-tight") == 1, "建议收紧了就按建议"


def test_a_forged_event_cannot_widen_the_retry_budget(monkeypatch):
    """有人往 `event_log` 里塞一条 `retry_budget=99` 的建议 —— 额度仍然是 env。

    这条是上一条的负例，也是那句「不拿读来的数当上限用」唯一的证明方式。
    """
    from maos.core.control_plane import ControlPlane, ENV_MAX_REPLAN
    from maos.core.eventbus import InMemoryEventBus
    from maos.core.store import SqliteStore

    monkeypatch.setenv(ENV_MAX_REPLAN, "2")
    store = SqliteStore()
    store.init_schema()
    cp = ControlPlane(store, InMemoryEventBus())
    store.append_event_log({
        "event_id": "", "trace_id": "", "plan_id": "plan-forged", "task_id": None,
        "event_type": plan_advice.PLAN_ADVISED_EVENT, "from_state": "", "to_state": "",
        "reason": "伪造", "detail": {"retry_budget": 99, "approver_role": "x"},
    })
    assert cp._max_replan("plan-forged") == 2


# ============================================================ Manager 的 prompt
def test_the_empty_hit_prompt_is_byte_identical():
    """一条都没命中时，prompt 逐字节等于 1.0。

    「用户请求：」这个前缀是 `ScriptedModelClient` 的分派关键字 —— 动了它，
    走 ManagerAgent 规划的场景（1 / 2 / 5 / 6 / 7）全部改判。建议这一段绝不能
    从这条路径上漏出来一个字节。
    """
    from maos.agents.manager import ManagerAgent

    goal = "把登录接口的超时从 3s 调到 10s"
    assert ManagerAgent._user_message(goal, []) == f"用户请求：{goal}"
    assert ManagerAgent._user_message(goal, [], _advice()) == f"用户请求：{goal}"


def test_the_advice_block_carries_every_citation_into_the_prompt():
    """建议段进 prompt，且每条都带出处。

    出处只留在事件里是不够的：评委问的是「凭什么多这一步」，而模型看到的那份
    清单如果没有来源，它排出来的任务就答不出这一问。
    """
    from maos.agents.manager import ManagerAgent

    docs = [{"kind": kb.KIND_TASK_PATTERN, "title": "经销渠道", "score": 0.5}]
    text = ManagerAgent._user_message("处理经销退款", docs, _advice())
    assert text.startswith("用户请求：处理经销退款")
    assert "渠道商核销" in text
    assert "kb-policy-tnt-mfg-a-AS-004-v1" in text
    assert "region_manager" in text
    assert "重试预算：1 次" in text
    # 空建议不许往 prompt 里加任何东西。
    empty = ManagerAgent._user_message("处理经销退款", docs, PlanAdvice())
    assert empty == ManagerAgent._user_message("处理经销退款", docs)


# ======================================================================
# 退款域不许上本模块的 import 图（T130）
# ======================================================================
_PLAN_ADVICE_PY = pathlib.Path(plan_advice.__file__)


def _module_level_imports(path: pathlib.Path) -> set[str]:
    """一个文件在**模块级**import 了哪些模块（全限定名）。

    函数体与类体里的 import 不算：那是调用到才拖进来的，不上模块的 import 图。
    判据走 AST 不走文本子串 —— 本模块的 docstring 里到处写着
    「不 import 退款域」这类自我说明，按子串扫会把说明本身判成违例
    （口径同 `test_claim_isolation.py`，那个坑在 `test_refund_flow.py:461` 记着）。
    """
    names: set[str] = set()

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue                        # 局部 import：调用到才拖，下面不追
            if isinstance(child, ast.Import):
                names.update(alias.name for alias in child.names)
            elif isinstance(child, ast.ImportFrom):
                if child.level:                 # 相对 import：留在包内，与本判据无关
                    continue
                module = child.module or ""
                names.add(module)
                names.update(f"{module}.{alias.name}" for alias in child.names)
            else:
                walk(child)                     # 顶层的 if / try / with 块照样是模块级

    walk(ast.parse(_PLAN_ADVICE_PY.read_text(encoding="utf-8"), filename=str(path)))
    return names


def test_the_refund_domain_is_not_on_this_module_s_import_graph():
    """🔴 `import maos.kb.plan_advice` 不许顺带拖进整个退款域。

    本模块要问退款域两件事（缺省承接岗、审批人认不认得出），问法是**函数体内的
    局部 import + 目录读不到时的字面量回落**，见 `_ticket_role` / `_warn_unknown_role`。
    把那两个 import 提到模块级，规划内核就长在了退款域身上，换个业务域得先改内核
    （铁律 9、跨轨契约 §E）。

    判据只管本文件。`maos/kb/` 下 `promotion.py` 是模块级 import 退款域的，
    `experiment.py` 则是一串局部 import —— 那两个文件不在本轨白名单，
    「kb 整片是否都该守这条」记在 BACKLOG，不在这里顺手扩大判据面。
    """
    bad = sorted(n for n in _module_level_imports(_PLAN_ADVICE_PY)
                 if n.startswith("maos.domain"))
    assert not bad, f"退款域上了 plan_advice 的模块 import 图：{bad}"


def test_the_domain_import_is_local_not_absent():
    """上一条测的是 import 的**位置**，不是有无 —— 这一条钉住「确实还在问目录」。

    少了这条，把 `_ticket_role` / `_warn_unknown_role` 整个删掉也能让上一条绿：
    退款域确实不在 import 图上了，代价是缺省岗不再由目录说了算、审批人写错也不再告警。
    """
    source = _PLAN_ADVICE_PY.read_text(encoding="utf-8")
    local = [n for n in _module_level_imports(_PLAN_ADVICE_PY) if n.startswith("maos.domain")]
    assert not local
    assert "from maos.domain.refund import roles" in source, (
        "本模块一处退款域 import 都没有了？那上一条判据在测一件不存在的事")
