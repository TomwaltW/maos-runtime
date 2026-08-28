"""Task-D 的机器验收 —— 聚合 / 知识 / 补偿 / Replan / 场景 5。

这一轨做的是**控制面行为**，铁律只有一条（phase-4.md 原文）：
**它们的正确性不得依赖模型的智力表现。** 所以下面每条断言都是确定性的 ——
没有一条依赖模型输出对不对，场景 5 那条更是直接锁「连跑两次输出逐字一致」。

几条钉死的反例，每条都对应一种「看起来绿、演示当天炸」的失败形态：

  · 补偿缺 patch_ref 必须**抛**，不许返回 None、更不许 ``.get("patch_ref", {})``
    兜底（C-5 反例原文）。兜底的后果不是报错，是补偿**静默不执行** ——
    reject 之后文件没还原，而日志一片正常。
  · compensation 的形状只从 ``fixtures/compensation_golden.json`` 来，不手搓 dict：
    C 轨的干跑闸测试加载同一份，两边形状才不会分叉。
  · replan 必须有上限，超限转人工而**不自旋** —— 无限重试是评委点名的反模式。
"""

from __future__ import annotations

import json
import pathlib

import pytest

from maos.artifacts import KIND_COMPENSATION, KIND_PATCH_SET, resolve_patch_ref
from maos.contracts import events as E
from maos.contracts.events import Topic
from maos.contracts.states import PlanState, TaskState
from maos.core.control_plane import (
    COMPENSATION_VERSION,
    FROZEN_BY_REPLAN,
    ControlPlane,
)
from maos.core.eventbus import InMemoryEventBus
from maos.core.store import SqliteStore
from maos.skills.builtin.issue_aggregate import IssueAggregateSkill, load_signal_findings
from maos.skills.contract import SkillContext
from maos.skills.invoker import SkillInvoker
from maos.agents.base import AgentIdentity
from maos.runtime.plan_finalizer import PlanFinalizer

GOLDEN = pathlib.Path(__file__).parent / "fixtures" / "compensation_golden.json"

PATCH_CONTENT = {
    "files": [{"path": "src/pay.py", "diff": "@@ -1,2 +1,3 @@\n+    verify(sig)"}],
    "summary": "补丁集样本",
    "self_check": {"build": "pass", "lint": "pass"},
}

# 只有 kb.sink / kb.retrieve 的 identity —— 闭环测试要两头都能调，
# 而 manager/coding 的白名单里都没有 kb.sink（附录 B 白名单语义）。
KB_IDENTITY = AgentIdentity(
    agent_id="test-kb", role="test",
    duty="测试用：知识读写闭环",
    allowed_skills=frozenset({"kb.sink", "kb.retrieve"}),
)


def _load_golden() -> dict:
    """C-5 冻结的 compensation 形状。D 与 C 两轨都加载这一份，不手搓。"""
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def _build():
    store = SqliteStore()
    store.init_schema()
    bus = InMemoryEventBus()
    cp = ControlPlane(store, bus)
    return store, bus, cp


def _make_task(cp, *, effect_risk="L", max_attempts=3, title="任务") -> tuple[str, str]:
    plan_id = cp.create_plan(goal="治理测试", trace_id="trace-t", tasks=[{
        "role": "coding", "title": title, "inputs": {}, "acceptance": [],
        "effect_risk": effect_risk, "max_attempts": max_attempts,
    }])
    cp.start_plan(plan_id)
    task_id = cp.store.list_tasks(plan_id)[0]["task_id"]
    return plan_id, task_id


def _submit_patch(bus, cp, plan_id, task_id, attempt, content=None):
    """走真实事件路径交回一份 patch_set：claim -> TaskResult -> AWAITING_REVIEW。"""
    cp.claim(task_id, "w1", attempt)
    bus.publish(Topic.TASK_RESULT, E.task_result(
        plan_id=plan_id, task_id=task_id, attempt=attempt, trace_id="trace-t",
        status="ok", artifacts=[{"kind": KIND_PATCH_SET,
                                 "content": content or PATCH_CONTENT}]))
    bus.drain()


def _verdict(bus, cp, plan_id, task_id, attempt, findings):
    bus.publish(Topic.REVIEW_VERDICT, E.review_verdict(
        plan_id=plan_id, task_id=task_id, attempt=attempt, trace_id="trace-t",
        verdict="rework", findings=findings, gate_results={"security": "fail"}))
    bus.drain()


def _blockers(n: int) -> list[dict]:
    return [{"gate": "security", "severity": "blocker", "path": f"f{i}.py",
             "message": "明文凭证"} for i in range(n)]


# ======================================================================
# 1. issue.aggregate —— 去重
# ======================================================================
def test_aggregate_merges_duplicate_findings_from_same_source():
    """同源重复的 finding 只出一条 issue，且来源不因去重而丢失。"""
    findings = [
        {"source": "issue.json", "severity": "major", "title": "回调未校验签名"},
        {"source": "issue.json", "severity": "blocker", "title": "回调未校验签名"},
        {"source": "issue.json", "severity": "minor", "title": "退款时间不准"},
    ]
    out = IssueAggregateSkill().run({"findings": findings}, SkillContext())

    assert len(out["issues"]) == 2, "同源重复应合成一条"
    top = out["issues"][0]
    assert top["title"] == "回调未校验签名"
    # 合并组内取**最高**严重度：取第一条会让 blocker 被 major 盖掉，
    # 那正是聚合最不该犯的错 —— 把最严重的问题降级成普通问题。
    assert top["severity"] == "blocker"
    assert top["id"] == "issue-01" and out["issues"][1]["id"] == "issue-02"


def test_aggregate_merges_across_sources_and_keeps_every_source():
    """跨源重复同样合并，但 source 保留全部来源 —— 「几个渠道都在报」是信息不是噪声。"""
    findings = [
        {"source": "error.log", "severity": "blocker", "title": "回调未校验签名"},
        {"source": "feedback-2.txt", "severity": "major", "title": "回调未校验签名"},
        {"source": "issue.json", "severity": "major", "title": "回调未校验签名"},
    ]
    out = IssueAggregateSkill().run({"findings": findings}, SkillContext())

    assert len(out["issues"]) == 1
    assert out["issues"][0]["source"] == "error.log,feedback-2.txt,issue.json"
    assert "合并重复 2 条" in out["summary"]


def test_aggregate_is_deterministic_over_real_multi_source_inputs():
    """scenarios/inputs/ 的多源信号：同一份输入连跑两次必须完全一致（零模型）。"""
    findings = load_signal_findings()
    assert findings, "scenarios/inputs/ 应能读出信号"

    first = IssueAggregateSkill().run({"findings": findings}, SkillContext())
    second = IssueAggregateSkill().run({"findings": findings}, SkillContext())
    assert first == second

    # 日志里同一个错误出现两次，时间戳不同 —— 剥不掉时间戳就会报成两个 issue
    titles = [i["title"] for i in first["issues"]]
    assert len(titles) == len(set(titles))
    assert first["issues"][0]["severity"] == "blocker"


def test_aggregate_rejects_non_list_findings():
    with pytest.raises(ValueError):
        IssueAggregateSkill().run({"findings": "not-a-list"}, SkillContext())


# ======================================================================
# 2. kb.sink / kb.retrieve 闭环
# ======================================================================
def test_kb_sink_then_retrieve_by_tags_and_by_keyword():
    """写进去的条目，按 tags 和按 keyword 都要能取回来。"""
    store = SqliteStore()
    store.init_schema()
    skills = SkillInvoker(KB_IDENTITY, store)

    sunk = skills.invoke("kb.sink", {
        "plan_id": "plan-1", "kind": "rule",
        "title": "回调必须校验签名",
        "body": "支付回调直接信任请求体等于把订单状态交给外部",
        "tags": ["security", "payment"],
    })
    assert sunk.status == "ok", sunk.error
    assert sunk.output["knowledge_id"]

    by_tag = skills.invoke("kb.retrieve", {"tags": ["payment"]})
    assert by_tag.output["count"] == 1
    assert by_tag.output["items"][0]["title"] == "回调必须校验签名"
    assert by_tag.output["items"][0]["tags"] == ["security", "payment"]

    by_kw = skills.invoke("kb.retrieve", {"keyword": "校验签名"})
    assert by_kw.output["count"] == 1

    # 命中不了要返回空清单而不是抛 —— 冷启动时检索不到是常态，不是故障
    miss = skills.invoke("kb.retrieve", {"tags": ["不存在的标签"]})
    assert miss.status == "ok" and miss.output == {"items": [], "count": 0}


def test_kb_retrieve_without_store_returns_empty_not_raises():
    """store 没接线时返回空 —— CodingAgent 用 cls(model) 老写法构造时就是这种情形，
    检索是锦上添花，不该因为调用方没接线就中断主链路。"""
    skills = SkillInvoker(KB_IDENTITY, None)
    res = skills.invoke("kb.retrieve", {"keyword": "任意"})
    assert res.status == "ok" and res.output == {"items": [], "count": 0}


def test_kb_sink_rejects_invalid_kind():
    """kind 越界必须抛：写错的条目查得出来但归不了类，而错误暴露在几周后的检索侧。"""
    store = SqliteStore()
    store.init_schema()
    res = SkillInvoker(KB_IDENTITY, store).invoke("kb.sink", {
        "plan_id": "p", "kind": "note", "title": "t", "body": "b", "tags": []})
    assert res.status == "failed" and "kind" in res.error


def test_kb_retrieve_limit_applies():
    store = SqliteStore()
    store.init_schema()
    skills = SkillInvoker(KB_IDENTITY, store)
    for i in range(4):
        skills.invoke("kb.sink", {"plan_id": "p", "kind": "case",
                                  "title": f"条目{i}", "body": "x", "tags": ["t"]})
    assert skills.invoke("kb.retrieve", {"tags": ["t"], "limit": 2}).output["count"] == 2
    assert skills.invoke("kb.retrieve", {"tags": ["t"], "limit": 0}).output["count"] == 4


# ======================================================================
# 3. 补偿引用自动附着（A-13）
# ======================================================================
def test_compensation_is_attached_for_high_effect_risk_patch_set():
    """effect_risk=H 收到 patch_set：compensation 的 patch_ref 三键齐全且指向本轮 attempt。"""
    store, bus, cp = _build()
    plan_id, task_id = _make_task(cp, effect_risk="H")
    _submit_patch(bus, cp, plan_id, task_id, 1)

    comps = [a for a in store.list_artifacts(task_id) if a["kind"] == KIND_COMPENSATION]
    assert len(comps) == 1, "高风险产出应恰好附着一条补偿引用"

    content = comps[0]["content"]
    assert content["mode"] == "reverse"
    ref = content["patch_ref"]
    assert set(ref) == {"task_id", "kind", "attempt"}, "patch_ref 是三键复合引用"
    assert ref["task_id"] == task_id and ref["kind"] == KIND_PATCH_SET and ref["attempt"] == 1
    # 指针不带货：正向补丁内容只存一份在被引用的 patch_set 里
    assert "files" not in content and "diff" not in json.dumps(content)

    # 它是引用不是产物，不占产物版本空间；Gate 按 version==attempt 取本轮产物，
    # 这一条正是让 compensation 不被四道产物闸误伤的原因
    assert comps[0]["version"] == COMPENSATION_VERSION == 0

    assert any(e["event_type"] == "CompensationAttached"
               for e in store.list_event_log(plan_id))


def test_compensation_not_attached_for_low_effect_risk():
    """低风险产物没有要还原的东西，不该凭空多出一条补偿引用。"""
    store, bus, cp = _build()
    plan_id, task_id = _make_task(cp, effect_risk="L")
    _submit_patch(bus, cp, plan_id, task_id, 1)
    assert not [a for a in store.list_artifacts(task_id) if a["kind"] == KIND_COMPENSATION]


def test_compensation_ref_follows_the_reworked_attempt():
    """返工后重新附着的引用要指向新的 attempt，不能还钉在第一版补丁上。"""
    store, bus, cp = _build()
    plan_id, task_id = _make_task(cp, effect_risk="H", max_attempts=5)
    _submit_patch(bus, cp, plan_id, task_id, 1)
    _verdict(bus, cp, plan_id, task_id, 1, [{"gate": "evidence", "severity": "minor",
                                             "message": "缺说明"}])
    _submit_patch(bus, cp, plan_id, task_id, 2)

    comps = [a for a in store.list_artifacts(task_id) if a["kind"] == KIND_COMPENSATION]
    assert [c["content"]["patch_ref"]["attempt"] for c in comps] == [1, 2]


# ======================================================================
# 4. 补偿执行器 —— 缺 patch_ref 必须硬失败
# ======================================================================
def _plant_compensation(store, task, content) -> None:
    store.insert_artifact({
        "artifact_id": E.new_id("art"), "task_id": task["task_id"],
        "plan_id": task["plan_id"], "kind": KIND_COMPENSATION,
        "version": COMPENSATION_VERSION, "content": content,
    })


def test_missing_patch_ref_raises_never_silently_skips():
    """缺 patch_ref **抛异常**，不是返回 None。

    这条是本文件最要紧的一条。兜底成 ``.get("patch_ref", {})`` 的后果不是报错，
    是补偿静默不执行 —— reject 之后文件没还原，日志一片正常，
    直到演示现场才发现（C-5 反例原文）。所以断言的是 raises，不是 is None。
    """
    store, bus, cp = _build()
    plan_id, task_id = _make_task(cp, effect_risk="H")
    _submit_patch(bus, cp, plan_id, task_id, 1)
    task = store.get_task(task_id)

    # 洗掉自动附着的那条，换成一条缺 patch_ref 的
    store._conn.execute("DELETE FROM artifact WHERE kind=?", (KIND_COMPENSATION,))
    _plant_compensation(store, task, {"mode": "reverse"})

    with pytest.raises(ValueError, match="patch_ref"):
        cp._execute_compensation(store.get_task(task_id), operator="人类")


def test_dangling_patch_ref_raises():
    """引用在、被引用物不在 —— 数据已不一致，同样不许静默跳过。"""
    store, bus, cp = _build()
    plan_id, task_id = _make_task(cp, effect_risk="H")
    _submit_patch(bus, cp, plan_id, task_id, 1)
    task = store.get_task(task_id)

    store._conn.execute("DELETE FROM artifact")
    _plant_compensation(store, task, _load_golden()["content"])   # golden 指向别的 task

    with pytest.raises(ValueError, match="取不回正向补丁集"):
        cp._execute_compensation(task, operator="人类")


def test_no_compensation_artifact_returns_none_and_does_not_raise():
    """没有补偿引用 ≠ 补偿坏了：低风险任务被驳回时本就无物可还原，正常返回。"""
    store, bus, cp = _build()
    plan_id, task_id = _make_task(cp, effect_risk="L")
    _submit_patch(bus, cp, plan_id, task_id, 1)
    assert cp._execute_compensation(store.get_task(task_id), operator="人类") is None


def test_reject_runs_compensation_then_fails_task():
    """驳回 -> 先补偿再落 FAILED。并行开发期沙箱未就位，事件必须如实记 ok=False。

    C-7 把 Task-D 的验收拆两段写死：并行期只验「补偿事件与 patch_ref 正确生成」
    （golden fixture + 本桩的 NotImplementedError）；「文件真实还原」归合并期。
    这里刻意**不**去给 sandbox.py 填临时实现、也不另起同名本地桩 —— 那会让
    干跑闸形同虚设，补偿失败的用例反而通过。
    """
    store, bus, cp = _build()
    plan_id, task_id = _make_task(cp, effect_risk="H")
    _submit_patch(bus, cp, plan_id, task_id, 1)
    # gate pass + effect_risk=H -> BLOCKED 等人工
    bus.publish(Topic.REVIEW_VERDICT, E.review_verdict(
        plan_id=plan_id, task_id=task_id, attempt=1, trace_id="trace-t",
        verdict="pass", findings=[], gate_results={}))
    bus.drain()
    assert store.get_task(task_id)["state"] == TaskState.BLOCKED

    cp.human_decision(task_id, approved=False, operator="沈思锴", note="不合规")

    executed = [e for e in store.list_event_log(plan_id)
                if e["event_type"] == "CompensationExecuted"]
    assert len(executed) == 1
    detail = executed[0]["detail"]
    assert detail["mode"] == "reverse"
    assert detail["patch_ref"]["task_id"] == task_id and detail["patch_ref"]["attempt"] == 1
    assert detail["ok"] is False
    assert detail["error"]["stage"] == "sandbox_unavailable"
    assert detail["files"] == 1

    assert store.get_task(task_id)["state"] == TaskState.FAILED
    assert store.get_plan(plan_id)["state"] == PlanState.FAILED
    # 顺序不能倒：补偿要在状态落 FAILED 之前跑（phase-4.md:20）
    types = [e["event_type"] for e in store.list_event_log(plan_id)]
    assert types.index("CompensationExecuted") < len(types) - 1


# ======================================================================
# 5. resolve_patch_ref 对 golden 的正负例（C-5 验证项）
# ======================================================================
def test_resolve_patch_ref_on_golden_positive_and_negative():
    """同一份 golden ref：配好 patch_set 返回非 None，缺失返回 None。"""
    golden = _load_golden()
    assert golden["kind"] == KIND_COMPENSATION
    ref = golden["content"]["patch_ref"]

    store = SqliteStore()
    store.init_schema()
    assert resolve_patch_ref(store, ref) is None, "还没配 patch_set 时必须取不到"

    store.insert_artifact({
        "artifact_id": "art-1", "task_id": ref["task_id"], "plan_id": "plan-x",
        "kind": KIND_PATCH_SET, "version": ref["attempt"], "content": PATCH_CONTENT,
    })
    hit = resolve_patch_ref(store, ref)
    assert hit is not None and hit["content"] == PATCH_CONTENT


def test_resolve_patch_ref_is_version_sensitive():
    """version 就是产出它的那次 attempt —— 对不上号不许返回「差不多的那个」。"""
    golden_ref = _load_golden()["content"]["patch_ref"]
    store = SqliteStore()
    store.init_schema()
    store.insert_artifact({
        "artifact_id": "art-9", "task_id": golden_ref["task_id"], "plan_id": "plan-x",
        "kind": KIND_PATCH_SET, "version": golden_ref["attempt"] + 1,
        "content": PATCH_CONTENT,
    })
    assert resolve_patch_ref(store, golden_ref) is None


# ======================================================================
# 6. Replan 触发三边界
# ======================================================================
def test_replan_triggers_on_two_blockers_in_one_round():
    store, bus, cp = _build()
    _, task_id = _make_task(cp)
    assert cp._should_replan(cp.store.get_task(task_id), _blockers(2)) is True


def test_replan_triggers_on_second_rework():
    """第一次返工不触发，第二次触发 —— 判定读的是 event_log，不是内存计数器。"""
    store, bus, cp = _build()
    plan_id, task_id = _make_task(cp, max_attempts=5)
    minor = [{"gate": "evidence", "severity": "minor", "message": "缺说明"}]

    task = store.get_task(task_id)
    assert cp._should_replan(task, minor) is False, "第一次返工不该触发"

    _submit_patch(bus, cp, plan_id, task_id, 1)
    _verdict(bus, cp, plan_id, task_id, 1, minor)      # 落下第 1 条 REWORK

    assert cp._should_replan(store.get_task(task_id), minor) is True


def test_replan_does_not_trigger_below_both_thresholds():
    """单个 blocker + 首次返工：两条线都没到，走普通返工，不重规划。"""
    store, bus, cp = _build()
    _, task_id = _make_task(cp)
    assert cp._should_replan(cp.store.get_task(task_id), _blockers(1)) is False


def test_replan_not_executed_without_replanner():
    """没注入重规划回调时退化成普通返工 —— 场景 1-4 与既有测试的行为不受影响。"""
    store, bus, cp = _build()
    plan_id, task_id = _make_task(cp, max_attempts=5)
    _submit_patch(bus, cp, plan_id, task_id, 1)
    _verdict(bus, cp, plan_id, task_id, 1, _blockers(2))

    assert cp._replan_used(plan_id) == 0
    assert store.get_plan(plan_id)["state"] == PlanState.RUNNING
    assert any(e["to_state"] == TaskState.REWORK for e in store.list_event_log(plan_id))


# ======================================================================
# 7. Replan 上限 —— 超限转人工，不自旋
# ======================================================================
def _replan_round(bus, cp, plan_id, task_id, attempt):
    _submit_patch(bus, cp, plan_id, task_id, attempt)
    _verdict(bus, cp, plan_id, task_id, attempt, _blockers(2))


def test_replan_respects_limit_and_escalates_to_human_without_spinning(monkeypatch):
    """MAOS_MAX_REPLAN=2：第 3 次触发时转人工（BLOCKED），且计数不再增长。"""
    monkeypatch.setenv("MAOS_MAX_REPLAN", "2")
    store, bus, cp = _build()
    plan_id, task_id = _make_task(cp, max_attempts=9)

    calls: list[int] = []

    def replanner(*, goal, findings, open_tasks):
        calls.append(len(findings))
        return [{"role": "coding", "title": "换个方案", "inputs": {}, "acceptance": []}]

    cp.set_replanner(replanner)

    _replan_round(bus, cp, plan_id, task_id, 1)
    assert cp._replan_used(plan_id) == 1
    _replan_round(bus, cp, plan_id, task_id, 2)
    assert cp._replan_used(plan_id) == 2

    _replan_round(bus, cp, plan_id, task_id, 3)          # 第 3 次：上限已到

    assert cp._replan_used(plan_id) == 2, "超限后不许再重规划 —— 自旋就是从这里开始的"
    assert len(calls) == 2, "重规划回调不该被第 3 次触发调用"
    task = store.get_task(task_id)
    assert task["state"] == TaskState.BLOCKED, "超限应转人工，不是继续转圈"

    blocked = [e for e in store.list_event_log(plan_id)
               if e["to_state"] == TaskState.BLOCKED]
    assert blocked[-1]["detail"]["reason"] == "replan_limit_exceeded"
    assert blocked[-1]["detail"]["await"] == "human_decision"


def test_replan_rewrites_open_task_and_restarts_plan():
    """重规划覆写未完成任务的规格，Plan 退回 PENDING 后重启，task_id 保持不变。"""
    store, bus, cp = _build()
    plan_id, task_id = _make_task(cp, max_attempts=9, title="方案甲")
    cp.set_replanner(lambda *, goal, findings, open_tasks: [
        {"role": "coding", "title": "方案乙", "inputs": {"title": "方案乙"},
         "acceptance": ["新验收"]}])

    _replan_round(bus, cp, plan_id, task_id, 1)

    task = store.get_task(task_id)
    assert task["task_id"] == task_id, "覆写而不是新建：一条任务的经历要串在同一个 id 上"
    assert task["title"] == "方案乙" and task["acceptance"] == ["新验收"]
    assert task["findings"], "上一版为什么不行要留着喂给下一轮"
    assert task["state"] == TaskState.DISPATCHED, "重启后应已重新派发"
    assert store.get_plan(plan_id)["state"] == PlanState.RUNNING

    log_rows = store.list_event_log(plan_id)
    assert any(e["event_type"] == "PlanTransition" and e["from_state"] == PlanState.RUNNING
               and e["to_state"] == PlanState.PENDING for e in log_rows), "缺 replan 迁移"
    assert any(e["event_type"] == "Replanned" for e in log_rows)


def test_replan_freezes_tasks_the_new_plan_dropped():
    """新方案没给某个未派发任务安排活 -> 冻结：既不派发，也不挡 Plan 收敛到 DONE。"""
    store, bus, cp = _build()
    # 乙依赖甲，所以首轮只有甲被派发 —— 乙才是「未派发任务」这个场景要说的那种
    plan_id = cp.create_plan(goal="两个任务", trace_id="trace-t", tasks=[
        {"task_id": "t-jia", "role": "coding", "title": "甲",
         "inputs": {}, "acceptance": [], "max_attempts": 9},
        {"task_id": "t-yi", "role": "coding", "title": "乙", "depends_on": ["t-jia"],
         "inputs": {}, "acceptance": [], "max_attempts": 9},
    ])
    cp.start_plan(plan_id)
    assert store.get_task("t-yi")["state"] == TaskState.PENDING
    cp.set_replanner(lambda *, goal, findings, open_tasks: [
        {"role": "coding", "title": "只留一个", "inputs": {}, "acceptance": []}])

    _replan_round(bus, cp, plan_id, "t-jia", 1)

    frozen = store.get_task("t-yi")
    assert frozen["last_error"] == FROZEN_BY_REPLAN
    assert frozen["state"] == TaskState.PENDING, "冻结不动状态机 —— states.py 是冻结契约"
    # 冻结任务不再被派发
    assert cp.dispatch_ready(plan_id) == 0


# ======================================================================
# 8. 场景 5 —— 确定性
# ======================================================================
def test_scenario_5_is_deterministic_across_runs(capsys, monkeypatch):
    """连跑两次，输出逐字一致 —— 含状态迁移轨迹。

    刻意设一个假 key：``--scenario 5`` 必须**忽略** MAOS_LLM_API_KEY
    （走 force_scripted），否则配了 key 的机器上这条路径会开始打真网络，
    「任何机器任何时刻结果一致」当场失效。
    """
    from maos.flows import scenario_5

    monkeypatch.setenv("MAOS_LLM_API_KEY", "fake-key-must-be-ignored")
    monkeypatch.setenv("MAOS_LLM_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("MAOS_LLM_MODEL", "should-not-be-used")

    assert scenario_5.run() == 0
    first = capsys.readouterr().out
    assert scenario_5.run() == 0
    second = capsys.readouterr().out

    assert first == second, "场景 5 两次输出必须逐字一致"
    assert "治理路径演示，无模型确定性复现" in first
    assert "AWAITING_REVIEW  -> REWORK" in first and "-> DONE" in first


def test_scenario_5_aggregates_replans_once_and_sinks_knowledge(capsys):
    """治理闭环的三个可观测量：多源聚合、恰好一次重规划、复盘条目落库。"""
    from maos.flows import scenario_5

    assert scenario_5.run() == 0
    out = capsys.readouterr().out

    assert "聚合 8 条 findings -> 3 个 issue（合并重复 5 条）" in out
    assert "重规划次数: 1" in out
    assert "知识沉淀: 3 条" in out
    assert "[case]" in out and "[rule]" in out


def test_plan_finalizer_is_idempotent_and_needs_terminal_state():
    """Plan 没到终态不复盘；到了终态只复盘一次（幂等走 store，不是内存标志）。"""
    store, bus, cp = _build()
    plan_id, task_id = _make_task(cp)

    finalizer = PlanFinalizer(store)
    assert finalizer.poll(plan_id) == [], "RUNNING 的 Plan 不该被复盘"

    _submit_patch(bus, cp, plan_id, task_id, 1)
    bus.publish(Topic.REVIEW_VERDICT, E.review_verdict(
        plan_id=plan_id, task_id=task_id, attempt=1, trace_id="trace-t",
        verdict="pass", findings=[], gate_results={}))
    bus.drain()
    assert store.get_plan(plan_id)["state"] == PlanState.DONE

    first = finalizer.poll(plan_id)
    assert first, "终态 Plan 应沉淀至少一条"
    assert store.list_knowledge(tags=["case"])
    # 换一个实例再 poll，仍然不该重复写 —— 幂等必须跨实例成立
    assert PlanFinalizer(store).poll(plan_id) == []
    assert len(store.list_knowledge()) == len(first)
