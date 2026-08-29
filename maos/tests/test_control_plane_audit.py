"""控制面五处缺陷的回归守卫（T2 轨，对应审查报告 P1-2 / P1-3 / P1-4 / P1-5 / P2-9）。

五条各自独立，但错法是同一种：**守卫站在副作用后面**。

  · P1-2  幂等键在状态校验之前就被消费 —— 认领失败，键却烧掉了
  · P1-4  补偿在状态机守卫之前执行 —— 守卫拦得住状态，拦不住 ``git apply -R``
  · P1-3  同一次覆写里混了两套缺省语义 —— 没提到的字段被当成「要清空」
  · P1-5  收敛判定只考虑了「部分冻结」—— 全冻结时 Plan 永远出不来
  · P2-9  「取最后一条」没有排序依据支撑 —— 换个后端就回滚错 attempt 的补丁

存量测试为什么一条都没抓到，逐条写在各用例的 docstring 里；那不是疏忽，
而是它们恰好都走在「顺序正确」的那条路上（比如 test_contracts 只测了
先派发再认领，test_replan_gateway 的 replanner 都显式写死了 inputs/acceptance）。
所以这里每一条都刻意**走偏一步**：抢跑一次、少写一个字段、多投递一次。

全文件不发网络请求、不读 ``MAOS_LLM_API_KEY``；补偿那几条把
``MAOS_SANDBOX_WORKDIR`` 指到一个非 git 目录，让沙箱如实报 ok=False ——
要验的是「补偿被调用了几次」，不是「补丁打得上打不上」。
"""

from __future__ import annotations

import pytest

from maos.artifacts import KIND_COMPENSATION, KIND_PATCH_SET
from maos.contracts import events as E
from maos.contracts.events import Topic
from maos.contracts.states import (
    PLAN_TRANSITIONS,
    TASK_TRANSITIONS,
    IllegalTransition,
    PlanState,
    TaskState,
)
from maos.core.control_plane import (
    ENV_SANDBOX_WORKDIR,
    FROZEN_BY_REPLAN,
    ControlPlane,
)
from maos.core.eventbus import InMemoryEventBus
from maos.core.store import SqliteStore

TRACE = "trace-t2"

PATCH_CONTENT = {
    "files": [{"path": "src/pay.py", "diff": "@@ -1,2 +1,3 @@\n+    verify(sig)"}],
    "summary": "补丁集样本",
    "self_check": {"build": "pass", "lint": "pass"},
}

#: 原始规格。P1-3 要验「新规格没提到的字段保持原值」，所以这几个字段必须**非空**
#: —— 都填成空的话，「保留原值」和「缺省清空」两种实现给出的结果一模一样，验不出来。
BASE_SPEC = {
    "role": "coding", "title": "原标题", "depends_on": [],
    "inputs": {"case_id": "C-1", "amount": 100}, "acceptance": ["必须过测试"],
    "risk_level": "L", "effect_risk": "L",
}

TASK_STATES = frozenset(v for k, v in vars(TaskState).items()
                        if not k.startswith("_") and isinstance(v, str))
PLAN_STATES = frozenset(v for k, v in vars(PlanState).items()
                        if not k.startswith("_") and isinstance(v, str))


# ======================================================================
# 夹具
# ======================================================================
def _build():
    store = SqliteStore()
    store.init_schema()
    bus = InMemoryEventBus()
    return store, bus, ControlPlane(store, bus)


def _make_task(cp, **overrides) -> tuple[str, str]:
    """建一个单任务计划，**不 start** —— 抢跑那条用例要的就是「还没派发」这一刻。"""
    plan_id = cp.create_plan(goal="目标", trace_id=TRACE, tasks=[{**BASE_SPEC, **overrides}])
    return plan_id, cp.store.list_tasks(plan_id)[0]["task_id"]


def _blockers(n: int) -> list[dict]:
    return [{"gate": "security", "severity": "blocker", "path": f"f{i}.py",
             "message": "明文凭证"} for i in range(n)]


def _submit_patch(bus, cp, plan_id, task_id, attempt):
    cp.claim(task_id, "w1", attempt)
    bus.publish(Topic.TASK_RESULT, E.task_result(
        plan_id=plan_id, task_id=task_id, attempt=attempt, trace_id=TRACE, status="ok",
        artifacts=[{"kind": KIND_PATCH_SET, "content": PATCH_CONTENT}]))
    bus.drain()


def _to_blocked_awaiting_approval(bus, cp, plan_id, task_id):
    """把一个 effect_risk=H 的任务推到「等人工审批」那一刻。"""
    _submit_patch(bus, cp, plan_id, task_id, 1)
    bus.publish(Topic.REVIEW_VERDICT, E.review_verdict(
        plan_id=plan_id, task_id=task_id, attempt=1, trace_id=TRACE,
        verdict="pass", findings=[], gate_results={}))
    bus.drain()
    assert cp.store.get_task(task_id)["state"] == TaskState.BLOCKED, \
        "前提没成立：effect_risk=H 且闸过了，任务应停在 BLOCKED 等人工"


def _compensations_executed(store, plan_id) -> list[dict]:
    return [e for e in store.list_event_log(plan_id)
            if e["event_type"] == "CompensationExecuted"]


def _assert_no_new_states(store, plan_id) -> None:
    """铁律 1 的机器判据：这条链路一个新状态、一个新迁移都没用上。

    contracts/states.py 是冻结契约，而 P1-3 / P1-5 都是「加个状态就好办了」的形状 ——
    加没加不靠人看 diff，靠这里逐条比对 event_log 里真实发生过的迁移。
    """
    for task in store.list_tasks(plan_id):
        assert task["state"] in TASK_STATES, f"出现了 states.py 之外的任务状态：{task['state']}"
    assert store.get_plan(plan_id)["state"] in PLAN_STATES

    for e in store.list_event_log(plan_id):
        if e["event_type"] == "StateTransition":
            assert (e["from_state"], e["to_state"]) in TASK_TRANSITIONS, \
                f"用了迁移表里没有的任务迁移：{e['from_state']} -> {e['to_state']}"
        elif e["event_type"] == "PlanTransition":
            assert (e["from_state"], e["to_state"]) in PLAN_TRANSITIONS, \
                f"用了迁移表里没有的计划迁移：{e['from_state']} -> {e['to_state']}"


class _ReversedArtifactStore:
    """一个**合规**的 Store 变体：等值 version 的相对顺序与 SQLite 现在给的相反。

    ``list_artifacts`` 的 SQL 只写了 ``ORDER BY version``，对同值行不作任何承诺 ——
    换后端、加索引、甚至换个查询计划都可能翻转。这个桩不是在造假，是在**行使**那份
    没被约束的自由度：任何依赖「现在恰好是插入顺序」的代码，在它面前必须照样正确。
    """

    def __init__(self, inner) -> None:
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def list_artifacts(self, task_id: str) -> list[dict]:
        arts = self._inner.list_artifacts(task_id)
        # 仍严格满足 ORDER BY version（sorted 是稳定排序），只是等值行反了过来。
        return sorted(reversed(arts), key=lambda a: a["version"])


# ======================================================================
# P1-2 · claim() 的幂等键不许在状态校验之前烧掉
# ======================================================================
def test_premature_claim_does_not_lock_out_the_real_one():
    """Worker 抢在派发之前认领一次，之后**合法**的那次认领仍必须成功。

    存量的 test_duplicate_claim_ignored 只走了「先派发再认领」这一条正路，
    所以「认领失败时键会不会被烧掉」从来没人问过。抢跑一次就问出来了：
    幂等键在状态校验之前被消费，且失败路径不回滚（store 也没有撤销口），
    于是同一个 attempt 的合法认领被当成重复投递拒掉 —— 任务停在 DISPATCHED，
    此后**没有任何人能再认领它**，重试额度也用不上，计划就此静默卡死。
    """
    store, bus, cp = _build()
    plan_id, task_id = _make_task(cp)

    assert cp.claim(task_id, "w-抢跑", 1) is None, "任务还没派发，这次认领本就该失败"
    assert store.get_task(task_id)["state"] == TaskState.PENDING, "失败的认领不许改状态"

    cp.start_plan(plan_id)
    assert store.get_task(task_id)["state"] == TaskState.DISPATCHED
    assert store.get_task(task_id)["attempt"] == 1, "前提没成立：抢跑用的正是这个 attempt"

    claimed = cp.claim(task_id, "w1", 1)
    assert claimed is not None, "修复前这里是 None —— 键已被抢跑那次烧掉，任务永久卡死"
    assert claimed["worker_id"] == "w1"
    assert store.get_task(task_id)["state"] == TaskState.RUNNING
    _assert_no_new_states(store, plan_id)


def test_duplicate_claim_of_the_same_attempt_is_still_rejected():
    """把校验挪到幂等闸前面，不许把幂等本身弄松：同 attempt 第二个 Worker 仍拿不到。"""
    store, bus, cp = _build()
    plan_id, task_id = _make_task(cp)
    cp.start_plan(plan_id)

    assert cp.claim(task_id, "w1", 1) is not None
    assert cp.claim(task_id, "w2", 1) is None, "同一 attempt 只许一个 Worker 领走"
    assert store.get_task(task_id)["worker_id"] == "w1", "第二次认领不许改写归属"


# ======================================================================
# P1-3 · _apply_replan 的覆写缺省语义
# ======================================================================
def test_replan_overwrite_keeps_fields_the_new_spec_did_not_mention():
    """新规格只说「换个角色、换个标题」，其余字段必须原样留着。

    修复前 title/risk_level 缺省保留原值，而 inputs/acceptance/depends_on 缺省清空，
    role 压根不在覆写里 —— 一次调用里两套语义。后果有三层：任务带着**空输入**被重新
    派发；depends_on 被清空后它会抢在依赖项前面跑；role 不更新意味着「重规划换角色」
    根本做不到，reviewer 的规格被安在 coding 任务上照旧交给 coding 去做。

    存量 test_replan_gateway 的 replanner 全都显式写了 ``inputs`` / ``acceptance``，
    且 role 前后同为 payment，恰好把这三层一起绕开了。
    """
    store, bus, cp = _build()
    plan_id, task_id = _make_task(cp, depends_on=[])
    store.update_task(task_id, findings=[{"gate": "security", "id": "F-1"}])
    before = store.get_task(task_id)

    cp._apply_replan(plan_id, [before], [{"role": "reviewer", "title": "新标题"}])

    task = store.get_task(task_id)
    assert task["role"] == "reviewer", "修复前 role 根本不在覆写分支里，换角色做不到"
    assert task["title"] == "新标题"
    assert task["inputs"] == before["inputs"], "修复前这里被清成 {} —— 任务带着空输入重新派发"
    assert task["acceptance"] == before["acceptance"], "修复前这里被清成 []"
    assert task["depends_on"] == before["depends_on"], "修复前这里被清成 []，会让它抢跑依赖项"
    assert task["risk_level"] == before["risk_level"]
    assert task["effect_risk"] == before["effect_risk"]
    assert task["findings"] == [{"gate": "security", "id": "F-1"}], \
        "findings 是**故意**保留不清的（下一轮要拿它当 rework_findings），别顺手统一掉"
    assert task["last_error"] is None, "覆写等于重新接手，冻结标记要清掉"


def test_replan_overwrite_still_honours_fields_the_spec_did_mention():
    """反面：规格显式写了的就以它为准，**包括显式写成空的**。

    「缺省保留原值」不许滑成「一律保留原值」—— 那样重规划就没法把一个任务的输入
    真正清空了。区分的是「没提到」和「提到了，值是空」。
    """
    store, bus, cp = _build()
    plan_id, task_id = _make_task(cp)
    before = store.get_task(task_id)

    cp._apply_replan(plan_id, [before], [{
        "role": "payment", "title": "改走备用渠道",
        "inputs": {}, "acceptance": [], "depends_on": [],
        "risk_level": "M", "effect_risk": "H",
    }])

    task = store.get_task(task_id)
    assert task["role"] == "payment"
    assert task["inputs"] == {}, "显式写了空输入就该是空输入"
    assert task["acceptance"] == []
    assert task["risk_level"] == "M" and task["effect_risk"] == "H"


# ======================================================================
# P1-4 · human_decision 的守卫必须挡在补偿前面
# ======================================================================
def test_repeated_reject_runs_compensation_only_once(tmp_path, monkeypatch):
    """同一条驳回投递两次，``git apply -R`` 只许真跑一遍。

    修复前 ``_execute_compensation()`` 排在 ``_transit()`` 之前，而状态机守卫在
    ``_transit()`` 里面：重复投递时补偿**先完整执行完**，IllegalTransition 才抛出来 ——
    守卫拦得住状态，拦不住副作用。对同一份补丁反向应用两遍是实打实的重复外部动作
    （铁律 8）。on_task_result / on_review_verdict 都过了幂等闸，唯独人工决策没过。

    第二次仍抛 IllegalTransition 是**保留的行为**：任务已是终态，再来一次人工决策
    本就是异常，该出声。变的只是它出声的时机 —— 现在一行副作用都还没发生。
    """
    monkeypatch.setenv(ENV_SANDBOX_WORKDIR, str(tmp_path / "empty-not-a-repo"))
    store, bus, cp = _build()
    plan_id, task_id = _make_task(cp, effect_risk="H")
    cp.start_plan(plan_id)
    _to_blocked_awaiting_approval(bus, cp, plan_id, task_id)

    cp.human_decision(task_id, approved=False, operator="沈思锴", note="不合规")
    assert len(_compensations_executed(store, plan_id)) == 1
    assert store.get_task(task_id)["state"] == TaskState.FAILED

    with pytest.raises(IllegalTransition):
        cp.human_decision(task_id, approved=False, operator="沈思锴", note="不合规")

    executed = _compensations_executed(store, plan_id)
    assert len(executed) == 1, f"补偿被打了 {len(executed)} 遍 —— 修复前这里是 2"
    assert executed[0]["detail"]["patch_ref"]["attempt"] == 1
    assert store.get_task(task_id)["state"] == TaskState.FAILED
    _assert_no_new_states(store, plan_id)


def test_approve_then_reject_never_reaches_compensation(tmp_path, monkeypatch):
    """先批准后驳回：一条补偿都不许执行。

    这条解释了幂等键为什么取 ``human:<task_id>`` 而不带决策 —— 带上决策的话，
    「驳回」会拿到一个从没被消费过的新键，补偿照跑一遍，非法迁移才在后面抛出来：
    同一个 bug 换扇门进来。一个任务只可能被人工决策一次，DONE 与 FAILED 都是终态。
    """
    monkeypatch.setenv(ENV_SANDBOX_WORKDIR, str(tmp_path / "empty-not-a-repo"))
    store, bus, cp = _build()
    plan_id, task_id = _make_task(cp, effect_risk="H")
    cp.start_plan(plan_id)
    _to_blocked_awaiting_approval(bus, cp, plan_id, task_id)

    cp.human_decision(task_id, approved=True, operator="沈思锴", note="放行")
    assert store.get_task(task_id)["state"] == TaskState.DONE

    with pytest.raises(IllegalTransition):
        cp.human_decision(task_id, approved=False, operator="沈思锴", note="反悔了")

    assert _compensations_executed(store, plan_id) == [], \
        "任务已经 DONE 了，驳回连一次回滚都不该发生"
    assert store.get_task(task_id)["state"] == TaskState.DONE


# ======================================================================
# P1-5 · 重规划交白卷时 Plan 必须收敛
# ======================================================================
def test_all_tasks_frozen_converges_the_plan_to_failed():
    """一条活任务都不剩时，Plan 落 FAILED，不再停在 RUNNING。

    ``_advance`` 的收敛判定原先写作 ``if tasks and all(... DONE)``：那句 ``tasks and``
    只考虑了「部分冻结」，全冻结时 ``tasks == []``，两个分支都不命中，Plan 永远
    出不来。终态选 FAILED 不选 DONE —— 一个什么都没做成的计划收成 DONE，正好撞上
    README §1 那句「四个 Agent 全部回复完成，而这一单没有成功，系统如实这么记了」。
    走的是既有迁移 RUNNING->FAILED("task_failed")，没有新状态、没有新迁移（铁律 1）。
    """
    store, bus, cp = _build()
    plan_id, task_id = _make_task(cp)
    cp.start_plan(plan_id)

    cp._apply_replan(plan_id, [store.get_task(task_id)], [])
    assert store.get_task(task_id)["last_error"] == FROZEN_BY_REPLAN, "前提：任务确已被冻结"

    cp._advance(plan_id)

    state = store.get_plan(plan_id)["state"]
    assert state != PlanState.RUNNING, "修复前这里恒为 RUNNING —— 计划永远出不来"
    assert state == PlanState.FAILED, "什么都没做成的计划不许记成 DONE"
    _assert_no_new_states(store, plan_id)


def test_replanner_returning_nothing_fails_the_plan_through_the_real_path():
    """走真链路：闸判 rework -> 触发重规划 -> replanner 交白卷 -> 计划落 FAILED。

    上一条直接调 ``_advance`` 验收敛判定；这条验**没人会去调它**的那条路 ——
    ``_replan`` 末尾调的是 ``start_plan``，全冻结时它派发 0 个任务就返回了，
    此后再没有任何事件会把这个计划推向终态。所以收敛必须由 ``_replan`` 自己兜住。

    replanner 返回空列表不是假想：``specs = self._replanner(...) or []`` 这一句
    把「模型输出空」和「调用异常被上游吞成 None」两种情况都归到了这里。
    """
    store, bus, cp = _build()
    plan_id, task_id = _make_task(cp, max_attempts=9)
    cp.start_plan(plan_id)
    calls: list[dict] = []

    def empty_replanner(*, goal, findings, open_tasks):
        calls.append({"goal": goal, "open": len(open_tasks)})
        return []

    cp.set_replanner(empty_replanner)

    _submit_patch(bus, cp, plan_id, task_id, 1)
    bus.publish(Topic.REVIEW_VERDICT, E.review_verdict(
        plan_id=plan_id, task_id=task_id, attempt=1, trace_id=TRACE,
        verdict="rework", findings=_blockers(2), gate_results={"security": "fail"}))
    bus.drain()

    assert calls, "前提没成立：单轮 2 个 blocker 本该触发重规划"
    assert store.get_task(task_id)["last_error"] == FROZEN_BY_REPLAN
    plan_state = store.get_plan(plan_id)["state"]
    assert plan_state != PlanState.RUNNING, "修复前这里停在 RUNNING，且再没人会来推它"
    assert plan_state == PlanState.FAILED
    _assert_no_new_states(store, plan_id)


# ======================================================================
# P2-9 · 多条 compensation 时选哪条必须确定
# ======================================================================
@pytest.mark.parametrize("reverse_rows", [False, True],
                         ids=["插入顺序", "等值行逆序"])
def test_compensation_targets_the_latest_attempt_whatever_the_row_order(
        tmp_path, monkeypatch, reverse_rows):
    """三轮 attempt 各附一条补偿，回滚的必须是 attempt=3 那份补丁。

    ``COMPENSATION_VERSION`` 恒为 0（它是引用不是产物，version 跟 attempt 走会被
    第四道产物闸当成本轮产物来评判），而 ``list_artifacts`` 只 ``ORDER BY version``
    —— 三条 version 全是 0，SQL 对同值行不作任何顺序承诺。原先的 ``comps[-1]``
    因此没有排序依据支撑：它现在指对了 attempt=3，靠的是 SQLite 隐式 rowid 顺序，
    换后端或加索引就可能翻转，而**翻转的后果是回滚了错误 attempt 的补丁**。

    两个参数跑的是同一份数据、两种都合规的行顺序。修复前逆序那一档选中 attempt=1，
    也就是把三轮之前那份早已被覆盖的补丁反着打了回去。
    """
    monkeypatch.setenv(ENV_SANDBOX_WORKDIR, str(tmp_path / "empty-not-a-repo"))
    store, bus, cp = _build()
    plan_id, task_id = _make_task(cp, effect_risk="H")

    for attempt in (1, 2, 3):
        store.insert_artifact({"artifact_id": E.new_id("art"), "task_id": task_id,
                               "plan_id": plan_id, "kind": KIND_PATCH_SET,
                               "version": attempt,
                               "content": {**PATCH_CONTENT, "summary": f"第 {attempt} 轮"}})
        cp._attach_compensation(store.get_task(task_id), attempt)

    comps = [a for a in store.list_artifacts(task_id) if a["kind"] == KIND_COMPENSATION]
    assert [a["version"] for a in comps] == [0, 0, 0], \
        "前提没成立：三条 version 不全是 0 的话，ORDER BY version 就已经定了序"

    if reverse_rows:
        cp.store = _ReversedArtifactStore(store)

    cp._execute_compensation(store.get_task(task_id), operator="沈思锴", note="驳回")

    executed = _compensations_executed(store, plan_id)
    assert len(executed) == 1
    assert executed[0]["detail"]["patch_ref"]["attempt"] == 3, \
        "回滚了错误 attempt 的补丁 —— 选中哪条不许依赖 list_artifacts 的返回顺序"
    assert executed[0]["detail"]["files"] == len(PATCH_CONTENT["files"])
