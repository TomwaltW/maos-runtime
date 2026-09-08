"""生命周期 hook：第一个能**否决**的挂点（T110）。

## 本文件钉的是什么

`maos/runtime/hooks.py` 的模块 docstring 列了改造前那四处回调 —— 它们没有一处能
否决：返回值被丢弃、异常被吞掉、或者干脆在动作**发生之后**才 fire。所以这里的
断言全都围绕同一句话展开：**这一次，回调说的话真的算数，而且它说的话留得下痕迹。**

四类断言，缺一不可：

1. **不接线时逐字节不变**（`test_absent_and_empty_registry_are_byte_identical`）。
   这是压倒一切的那条：一个能否决的挂点若默认接上，某个第三方回调抛异常就能改变
   全仓既有链路的行为。
2. **否决真的挡住了动作** —— 任务没建出来、DONE 没落下去。只断言 `fire` 的返回值
   不够：那只证明注册表算对了，不证明控制面听了它的。
3. **失败姿态方向不许写反**：抛异常 = 不否决 + 留痕（fail-open）；否决 = 留痕。
   这两条的方向是相反的，写反任何一条都是事故 —— 抛异常若否决，一个 hook 的 bug
   能把整条产线停掉；否决若不留痕，治理就成了黑箱。
4. **转人工要捞得到人**（`test_task_completed_veto_...` 末段）。
   `maos/runtime/gate.py::HumanApprovalQueue.pending` 的原话：转人工而捞不到人，
   比直接 FAILED 更糟 —— 那是静默挂起。

## 为什么大量用真 store、真 bus，不用 mock

否决的价值全在「它落在 event_log 里、被人的队列捞得到」这一段，而那正是 mock
最容易假绿的地方：mock 一个 store，`append_event_log` 断言调用了几次，改天列名
写错照样绿。这里全程 `SqliteStore` + `InMemoryEventBus`，断言直接读库。
"""

from __future__ import annotations

import pytest

from maos.contracts import events as E
from maos.contracts.events import Topic
from maos.contracts.states import PlanState, TaskState
from maos.core.control_plane import ControlPlane
from maos.core.eventbus import InMemoryEventBus
from maos.core.store import SqliteStore
from maos.runtime.gate import HumanApprovalQueue
from maos.runtime.hooks import (
    EVT_HOOK_FAILED,
    EVT_HOOK_VETOED,
    HOOK_VETO_REASON,
    TASK_COMPLETED,
    TASK_CREATED,
    WORKER_IDLE,
    HookRegistry,
    PlanVetoed,
    Veto,
)

TRACE = "trace-t110"


# ======================================================================
# 夹具
# ======================================================================
def _build(*, hooks: HookRegistry | None = None):
    store = SqliteStore()
    store.init_schema()
    bus = InMemoryEventBus()
    cp = ControlPlane(store, bus, hooks=hooks)
    return store, bus, cp


def _build_with_registry():
    """registry 与 cp 共用同一个 store —— 留痕必须落进同一本账。"""
    store = SqliteStore()
    store.init_schema()
    bus = InMemoryEventBus()
    reg = HookRegistry(store)
    cp = ControlPlane(store, bus, hooks=reg)
    return store, bus, cp, reg


def _spec(task_id: str, title: str, *, role: str = "coding",
          depends_on: list[str] | None = None, effect_risk: str = "L") -> dict:
    """task_id 写死，两次独立跑出来的 event_log 才逐字可比。"""
    return {"task_id": task_id, "role": role, "title": title,
            "inputs": {}, "acceptance": [], "effect_risk": effect_risk,
            "depends_on": depends_on or []}


def _run_task(cp, bus, plan_id, task_id, *, attempt=1, verdict="pass"):
    """一轮完整的真链路：认领 -> 交产物 -> 判定。判定直接发事件，不经 Gate。

    刻意绕开 `ReviewerGate`：本轨要验的是 `on_review_verdict` 收到 pass 之后
    做了什么，闸怎么判出 pass 是别人的面。少一层依赖，这些断言就少一个会因为
    闸的判据变化而假红的理由。
    """
    cp.claim(task_id, "w1", attempt)
    bus.publish(Topic.TASK_RESULT, E.task_result(
        plan_id=plan_id, task_id=task_id, attempt=attempt, trace_id=TRACE,
        status="ok", artifacts=[{"kind": "generic", "content": {"summary": "做完了"}}]))
    bus.drain()
    bus.publish(Topic.REVIEW_VERDICT, E.review_verdict(
        plan_id=plan_id, task_id=task_id, attempt=attempt, trace_id=TRACE,
        verdict=verdict, gate_results={"lint": "pass"}))
    bus.drain()


def _log_shape(store, plan_id) -> list[tuple]:
    """event_log 的可比指纹：类型 + 迁移三元组 + detail。时间戳与 seq 不进来。"""
    return [(e["event_type"], e["task_id"], e["from_state"], e["to_state"],
             e["reason"], e["detail"])
            for e in store.list_event_log(plan_id)]


def _hook_rows(store, plan_id, event_type) -> list[dict]:
    return [e for e in store.list_event_log(plan_id) if e["event_type"] == event_type]


# ======================================================================
# 1. Veto 本身：说不出理由的否决不许发出
# ======================================================================
@pytest.mark.parametrize("bad", ["", "   ", "\n\t"])
def test_empty_reason_is_rejected_at_construction(bad):
    """空理由的否决等于没说话 —— 构造时就拒，不等到落库再兜底。

    在构造时拒是刻意的：这一行是事后唯一能回答「为什么这个任务没被创建」的东西，
    让它空着通过，等于把一条查不出所以然的记录留给三个月后的人。
    """
    with pytest.raises(ValueError, match="不许为空"):
        Veto(bad)


def test_a_real_reason_is_kept_verbatim():
    assert Veto("effect_risk=H 的任务不许自动创建").reason == \
        "effect_risk=H 的任务不许自动创建"


def test_registering_an_unknown_event_is_refused():
    """拼错挂点名 = 这个 hook 永远不被调用且不报警 —— 本模块要治的正是这个病。"""
    reg = HookRegistry()
    with pytest.raises(ValueError, match="未知挂点"):
        reg.on("task_creted", lambda **p: None)


# ======================================================================
# 2. 注册顺序与短路
# ======================================================================
def test_hooks_run_in_registration_order_and_first_veto_short_circuits():
    """按注册顺序跑；第一个 Veto 即短路，**后面的没被调用**（用计数器证明）。

    短路不是优化：否决是终局判定，第二条 hook 再说什么都改不了结果。跑完全部再
    挑一个，会让「是谁拦的」变成一个要靠优先级规则回答的问题。
    """
    calls: list[str] = []
    reg = HookRegistry()

    def first(**p):
        calls.append("first")
        return None

    def second(**p):
        calls.append("second")
        return Veto("第二条拦下的")

    def third(**p):
        calls.append("third")
        return Veto("第三条也想拦")

    for fn in (first, second, third):
        reg.on(TASK_CREATED, fn)

    veto = reg.fire(TASK_CREATED, plan_id="p1", trace_id=TRACE, title="t", role="r")

    assert calls == ["first", "second"], "短路失败：第三条不该被调用"
    assert veto is not None and veto.reason == "第二条拦下的"
    assert [fn.__name__ for fn in reg.registered(TASK_CREATED)] == \
        ["first", "second", "third"], "注册顺序必须原样保留"


def test_no_hooks_registered_means_no_veto():
    assert HookRegistry().fire(TASK_CREATED, plan_id="p1") is None


# ======================================================================
# 3. WORKER_IDLE：只定义、不接线
# ======================================================================
def test_worker_idle_is_defined_and_fireable_but_not_wired():
    """挂点的形状用假 registry 直接验；接线点留给整合期（worker.py 是 T107 的面）。

    payload 的三个键写死在这里：接线的人照着填，填错了这条会红。
    """
    seen: list[dict] = []
    reg = HookRegistry()
    reg.on(WORKER_IDLE, lambda **p: seen.append(p))

    reg.fire(WORKER_IDLE, worker_id="w1", roles=["coding"],
             just_finished_task_id="task-1")

    assert seen == [{"worker_id": "w1", "roles": ["coding"],
                     "just_finished_task_id": "task-1"}]


def test_worker_idle_is_not_wired_into_the_worker_runtime():
    """反向守卫：worker.py 里不许出现本挂点 —— 接线是整合期的事，不是本轨的。"""
    import inspect

    from maos.runtime import worker

    assert "worker_idle" not in inspect.getsource(worker), \
        "WORKER_IDLE 已被接进 worker.py —— 那是 T107 的面，本轨只定义不接线"


# ======================================================================
# 4. 压倒一切的那条：不接线时逐字节不变
# ======================================================================
def test_absent_and_empty_registry_are_byte_identical():
    """不注入 registry、注入一个空 registry，两条链路的 event_log 逐字相同。

    两件事一起钉住：
    · `hooks=None` 时那几行 fire 代码根本不执行（执行了会 AttributeError）；
    · 注册表为空时 fire 不产生任何副作用（一条 Hook* 都不许多）。
    """
    shapes = []
    for hooks_factory in (lambda s: None, lambda s: HookRegistry(s)):
        store = SqliteStore()
        store.init_schema()
        bus = InMemoryEventBus()
        cp = ControlPlane(store, bus, hooks=hooks_factory(store))
        plan_id = cp.create_plan(
            goal="把活干完", trace_id=TRACE, plan_id="plan-fixed",
            tasks=[_spec("task-a", "第一件"), _spec("task-b", "第二件")])
        cp.start_plan(plan_id)
        _run_task(cp, bus, plan_id, "task-a")
        _run_task(cp, bus, plan_id, "task-b")

        shapes.append(_log_shape(store, plan_id))
        assert store.get_plan(plan_id)["state"] == PlanState.DONE
        assert [t["state"] for t in store.list_tasks(plan_id)] == \
            [TaskState.DONE, TaskState.DONE]
        assert not [e for e in store.list_event_log(plan_id)
                    if e["event_type"].startswith("Hook")
                    or e["event_type"] == "TaskCreationVetoed"], \
            "没注册任何回调，却落了 hook 相关的行"

    assert shapes[0] == shapes[1], "装一个空 registry 改变了 event_log —— 缺省路径被污染"


# ======================================================================
# 5. 失败姿态一：回调抛异常 = 不否决 + 留痕
# ======================================================================
def test_callback_exception_does_not_veto_and_leaves_a_trace():
    """协调信号 fail-open：hook 挂了，主链路照走 —— 但**必须**留下一行。

    这一条与 `maos/ingress/router.py` 的「吞成 warning」刻意不同：那里只打日志，
    于是钩子挂了没有任何人知道。一个静默失效的挂点比没有挂点更糟。
    """
    store, bus, cp, reg = _build_with_registry()

    def boom(**p):
        raise RuntimeError("hook 自己写崩了")

    reg.on(TASK_CREATED, boom)
    plan_id = cp.create_plan(goal="g", trace_id=TRACE, plan_id="plan-1",
                             tasks=[_spec("task-a", "照样要建出来")])

    assert [t["task_id"] for t in store.list_tasks(plan_id)] == ["task-a"], \
        "回调抛异常不该否决 —— 主链路必须照走"

    rows = _hook_rows(store, plan_id, EVT_HOOK_FAILED)
    assert len(rows) == 1, "异常被吞了但没留痕 —— 那正是静默失效"
    d = rows[0]["detail"]
    assert d["hook_event"] == TASK_CREATED
    assert d["hook"] == "test_callback_exception_does_not_veto_and_leaves_a_trace.<locals>.boom"
    assert d["error_type"] == "RuntimeError"
    assert "hook 自己写崩了" in d["error"], "异常消息必须原文进 detail，否则查不出是哪一句崩的"


def test_a_broken_hook_does_not_stop_the_next_one_from_vetoing():
    """一个坏 hook 不该顶掉后面那个好 hook 的否决权。"""
    store, bus, cp, reg = _build_with_registry()
    reg.on(TASK_CREATED, lambda **p: (_ for _ in ()).throw(ValueError("坏了")))
    reg.on(TASK_CREATED, lambda **p: Veto("后面这条仍然拦得住"))

    with pytest.raises(PlanVetoed):
        cp.create_plan(goal="g", trace_id=TRACE, plan_id="plan-1",
                       tasks=[_spec("task-a", "该被拦")])

    assert len(_hook_rows(store, "plan-1", EVT_HOOK_FAILED)) == 1
    assert len(_hook_rows(store, "plan-1", EVT_HOOK_VETOED)) == 1


def test_non_veto_return_is_treated_as_a_failure_not_a_veto():
    """回调 `return False` 不算否决 —— 只认 Veto 一种否决形态，但要留痕。

    留痕这半句不许省：否则就成了「他以为拦了、实际没拦、还没人知道」。
    """
    store, bus, cp, reg = _build_with_registry()
    reg.on(TASK_CREATED, lambda **p: False)

    plan_id = cp.create_plan(goal="g", trace_id=TRACE, plan_id="plan-1",
                             tasks=[_spec("task-a", "照建")])

    assert len(store.list_tasks(plan_id)) == 1, "非 Veto 返回值不该拦住任何东西"
    rows = _hook_rows(store, plan_id, EVT_HOOK_FAILED)
    assert len(rows) == 1 and rows[0]["detail"]["error_type"] == "BadReturn"


def test_registry_without_store_still_works():
    """store=None 时一切照跑，只是不留痕（口径同 SkillInvoker._settle）。"""
    reg = HookRegistry()
    reg.on(TASK_CREATED, lambda **p: (_ for _ in ()).throw(ValueError("坏了")))
    reg.on(TASK_CREATED, lambda **p: Veto("拦下"))
    assert reg.fire(TASK_CREATED, plan_id="p1").reason == "拦下"


# ======================================================================
# 6. 失败姿态二：否决必须进审计链
# ======================================================================
def test_veto_writes_hook_vetoed_with_the_reason_verbatim():
    """否决是治理动作 —— 谁、在哪个挂点、为什么，三样都要答得出。"""
    store, bus, cp, reg = _build_with_registry()
    reason = "effect_risk=H 的任务必须先过人工审批，不许自动建"
    reg.on(TASK_CREATED, lambda **p: Veto(reason))

    with pytest.raises(PlanVetoed):
        cp.create_plan(goal="g", trace_id=TRACE, plan_id="plan-1",
                       tasks=[_spec("task-a", "上线支付改动")])

    rows = _hook_rows(store, "plan-1", EVT_HOOK_VETOED)
    assert len(rows) == 1
    assert rows[0]["detail"]["reason"] == reason, "理由必须逐字落库"
    assert rows[0]["detail"]["hook_event"] == TASK_CREATED


def test_hooks_receive_the_documented_task_created_payload():
    """payload 的形状是契约：写 hook 的人照着这几个键判，少一个就判不了。"""
    seen: list[dict] = []
    store, bus, cp, reg = _build_with_registry()
    reg.on(TASK_CREATED, lambda **p: seen.append(p))

    cp.create_plan(goal="把退款做掉", trace_id=TRACE, plan_id="plan-1", tasks=[
        {"task_id": "task-a", "role": "payment", "title": "发起退款",
         "risk_level": "M", "effect_risk": "H", "depends_on": ["task-z"]}])

    assert len(seen) == 1
    p = seen[0]
    assert p["plan_id"] == "plan-1" and p["trace_id"] == TRACE
    assert p["goal"] == "把退款做掉"
    assert p["role"] == "payment" and p["title"] == "发起退款"
    assert p["risk_level"] == "M" and p["effect_risk"] == "H"
    assert p["depends_on"] == ["task-z"]
    assert p["spec"]["task_id"] == "task-a", "原始规格也要给出去，hook 可能要看别的字段"


def test_task_created_fires_before_the_task_exists():
    """挂点必须在**落库前**开火 —— 事后才 fire 的回调否决不了任何东西。

    这正是圆桌钩子（回帖发出之后才 fire）不算 hook 的那条判据，反过来钉一遍。
    """
    store, bus, cp, reg = _build_with_registry()
    seen: list[list] = []
    reg.on(TASK_CREATED, lambda **p: seen.append(store.list_tasks(p["plan_id"])))

    cp.create_plan(goal="g", trace_id=TRACE, plan_id="plan-1",
                   tasks=[_spec("task-a", "第一件"), _spec("task-b", "第二件")])

    assert seen == [[], []], "fire 的时候库里已经有任务了 —— 挂点接晚了"


# ======================================================================
# 7. TASK_CREATED 否决：那个任务不建，其余照建
# ======================================================================
def test_task_created_veto_drops_only_that_task():
    store, bus, cp, reg = _build_with_registry()
    reg.on(TASK_CREATED, lambda **p: Veto("这一件不许做") if p["role"] == "payment" else None)

    plan_id = cp.create_plan(goal="g", trace_id=TRACE, plan_id="plan-1", tasks=[
        _spec("task-a", "写代码"),
        _spec("task-b", "发起退款", role="payment"),
        _spec("task-c", "写测试", role="testing"),
    ])

    assert [t["task_id"] for t in store.list_tasks(plan_id)] == ["task-a", "task-c"]
    assert store.get_plan(plan_id) is not None, "只否决一件，plan 照建"

    rows = _hook_rows(store, plan_id, "TaskCreationVetoed")
    assert len(rows) == 1
    assert rows[0]["task_id"] == "task-b"
    assert rows[0]["reason"] == "这一件不许做"
    assert rows[0]["detail"]["title"] == "发起退款"


# ======================================================================
# 8. 全部否决：抛异常，不建空 plan
# ======================================================================
def test_all_tasks_vetoed_raises_and_creates_no_plan_row():
    """空 plan 会被 `_advance` 收敛成 FAILED —— 那是把「治理拦下了」伪装成
    「执行失败了」。两件事在 event_log 上必须分得开。
    """
    store, bus, cp, reg = _build_with_registry()
    reg.on(TASK_CREATED, lambda **p: Veto(f"{p['title']} 不许做"))

    with pytest.raises(PlanVetoed) as exc:
        cp.create_plan(goal="g", trace_id=TRACE, plan_id="plan-1",
                       tasks=[_spec("task-a", "甲"), _spec("task-b", "乙")])

    assert store.get_plan("plan-1") is None, "全否决还建出了 plan 行"
    assert store.list_tasks("plan-1") == []
    assert [v["title"] for v in exc.value.vetoed] == ["甲", "乙"]
    assert exc.value.plan_id == "plan-1"
    assert "甲 不许做" in str(exc.value), "异常消息里要带得出每一条理由"

    # 否决本身仍然留痕：计划没建成，但「为什么没建成」查得到。
    assert len(_hook_rows(store, "plan-1", "TaskCreationVetoed")) == 2
    assert not [e for e in store.list_event_log("plan-1")
                if e["event_type"] == "PlanTransition"], \
        "不许出现 plan 迁移 —— 它压根没被创建，更没有失败"


def test_empty_task_list_still_creates_a_plan_as_before():
    """`create_plan(tasks=[])` 是既有合法调用（重规划返回空规格时走到这里）。

    它建的空 plan 由 `_advance` 收敛，与「被否决」无关 —— 判据写成「一个都没留下」
    而不带 `tasks and`，会把这条既有路径一起判死。
    """
    store, bus, cp, reg = _build_with_registry()
    reg.on(TASK_CREATED, lambda **p: Veto("全拦"))

    plan_id = cp.create_plan(goal="g", trace_id=TRACE, plan_id="plan-1", tasks=[])

    assert store.get_plan(plan_id) is not None
    assert store.list_tasks(plan_id) == []


# ======================================================================
# 9. 被否决的 task 是别人的依赖 —— 不重连，让它停在 PENDING
# ======================================================================
def test_vetoed_task_leaves_its_dependent_stuck_in_pending():
    """治理否决了一环，剩下的活不该假装还能跑完。

    自动跳过被否决的那一环去接线，等于系统替人判定「那一环其实可有可无」——
    而那恰恰是人刚刚否掉的东西。所以依赖指向一个不存在的 task_id 时，
    `dispatch_ready` 的 `issubset(done)` 永远不满足，依赖方停在 PENDING。
    """
    store, bus, cp, reg = _build_with_registry()
    reg.on(TASK_CREATED, lambda **p: Veto("这一环不许做") if p["title"] == "发起退款" else None)

    plan_id = cp.create_plan(goal="g", trace_id=TRACE, plan_id="plan-1", tasks=[
        _spec("task-refund", "发起退款", role="payment"),
        _spec("task-notify", "通知用户", depends_on=["task-refund"]),
        _spec("task-log", "记一笔账"),
    ])
    cp.start_plan(plan_id)

    states = {t["task_id"]: t["state"] for t in store.list_tasks(plan_id)}
    assert "task-refund" not in states, "被否决的那一件不该存在"
    assert states["task-log"] == TaskState.DISPATCHED, "不相干的任务照常派发"
    assert states["task-notify"] == TaskState.PENDING, \
        "依赖被否决的任务竟然派发出去了 —— 依赖被自作主张地重连了"

    # 把能跑的跑完，依赖方仍然停在 PENDING，plan 也不许被判成 DONE。
    _run_task(cp, bus, plan_id, "task-log")

    after = {t["task_id"]: t["state"] for t in store.list_tasks(plan_id)}
    assert after["task-log"] == TaskState.DONE
    assert after["task-notify"] == TaskState.PENDING
    assert store.get_plan(plan_id)["state"] == PlanState.RUNNING, \
        "还有一件永远跑不了的任务，plan 不许收敛成 DONE"


# ======================================================================
# 10. TASK_COMPLETED 否决：不落 DONE，走既有转人工出口
# ======================================================================
def test_task_completed_veto_blocks_instead_of_done_and_the_human_queue_sees_it():
    """否决完成 = 走既有迁移 AWAITING_REVIEW->BLOCKED(gate_needs_human)。

    **不加状态、不加迁移**（铁律 1/9）。末段那条断言不许省：转人工而捞不到人，
    比直接 FAILED 更糟（`gate.py::HumanApprovalQueue.pending` 原话）。
    """
    store, bus, cp, reg = _build_with_registry()
    reg.on(TASK_COMPLETED, lambda **p: Veto("验收标准没覆盖回滚，先给人看一眼"))

    plan_id = cp.create_plan(goal="g", trace_id=TRACE, plan_id="plan-1",
                             tasks=[_spec("task-a", "改代码")])
    cp.start_plan(plan_id)
    _run_task(cp, bus, plan_id, "task-a")

    task = store.get_task("task-a")
    assert task["state"] == TaskState.BLOCKED, "hook 说不行，却仍然落了 DONE"
    assert task["effect_risk"] == "L", \
        "前提没成立：effect_risk=H 本来就会转人工，那样这条测试证明不了 hook 的作用"

    hop = [e for e in store.list_event_log(plan_id)
           if e["event_type"] == "StateTransition" and e["to_state"] == TaskState.BLOCKED]
    assert len(hop) == 1
    assert hop[0]["from_state"] == TaskState.AWAITING_REVIEW
    assert hop[0]["reason"] == "gate_needs_human", "必须走既有迁移，不许新增"
    assert hop[0]["detail"]["await"] == "human_decision"
    assert hop[0]["detail"]["reason"] == HOOK_VETO_REASON
    assert hop[0]["detail"]["hook_reason"] == "验收标准没覆盖回滚，先给人看一眼"

    assert store.get_plan(plan_id)["state"] == PlanState.RUNNING, \
        "否决不动 plan 状态 —— plan 的死活由人的决定说了算"

    pending = HumanApprovalQueue(store, cp).pending(plan_id)
    assert [t["task_id"] for t in pending] == ["task-a"], \
        "转人工却没人捞得到 —— 静默挂起，比直接 FAILED 更糟"


def test_task_completed_hook_sees_the_documented_payload_and_can_pass():
    """放行时一切照旧：落 DONE、plan 收敛。payload 形状一并钉住。"""
    seen: list[dict] = []
    store, bus, cp, reg = _build_with_registry()
    reg.on(TASK_COMPLETED, lambda **p: seen.append(p))

    plan_id = cp.create_plan(goal="g", trace_id=TRACE, plan_id="plan-1",
                             tasks=[_spec("task-a", "改代码")])
    cp.start_plan(plan_id)
    _run_task(cp, bus, plan_id, "task-a")

    assert store.get_task("task-a")["state"] == TaskState.DONE
    assert store.get_plan(plan_id)["state"] == PlanState.DONE
    assert len(seen) == 1
    p = seen[0]
    assert p["task_id"] == "task-a" and p["plan_id"] == "plan-1"
    assert p["trace_id"] == TRACE and p["attempt"] == 1
    assert p["role"] == "coding"
    assert p["gate_results"] == {"lint": "pass"}
    assert p["artifact_count"] == 1, "产物数要真的数一遍，hook 要靠它判空交付"


def test_task_completed_fires_before_the_task_is_done():
    """同 TASK_CREATED：落 DONE 之前开火，否则否决不了任何东西。"""
    states: list[str] = []
    store, bus, cp, reg = _build_with_registry()
    reg.on(TASK_COMPLETED, lambda **p: states.append(store.get_task(p["task_id"])["state"]))

    plan_id = cp.create_plan(goal="g", trace_id=TRACE, plan_id="plan-1",
                             tasks=[_spec("task-a", "改代码")])
    cp.start_plan(plan_id)
    _run_task(cp, bus, plan_id, "task-a")

    assert states == [TaskState.AWAITING_REVIEW], "fire 的时候任务已经 DONE 了 —— 接晚了"


def test_task_completed_does_not_fire_on_rework_or_block():
    """挂点只在「判定完成」那一刻开火，rework / block 不是完成。"""
    fired: list[str] = []
    store, bus, cp, reg = _build_with_registry()
    reg.on(TASK_COMPLETED, lambda **p: fired.append(p["task_id"]))

    plan_id = cp.create_plan(goal="g", trace_id=TRACE, plan_id="plan-1",
                             tasks=[_spec("task-a", "改代码")])
    cp.start_plan(plan_id)
    _run_task(cp, bus, plan_id, "task-a", verdict="rework")

    assert fired == [], "rework 判定不该触发 TASK_COMPLETED"
    assert store.get_task("task-a")["state"] == TaskState.DISPATCHED
