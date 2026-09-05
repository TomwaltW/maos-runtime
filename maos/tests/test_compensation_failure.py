"""T96 · 补偿失败要有人管 —— 回滚没做成时开人工工单的回归守卫。

改造前 `human_decision` 把 `_execute_compensation()` 的返回值**整个丢弃**：
`git apply -R` 打不上、workdir 不是仓库、沙箱不可用，这些情况事件里如实记了
`ok=false`，但没有任何人读它 —— 状态照样落 FAILED，屏幕上一片正常，而产物还在外面。
`docs/defense-brief.md` 那条「补偿失败没有人管」说的就是这件事。

本文件钉的是**处置**，不是留痕 —— 留痕早就有了（`test_governance.py` 那几条钉住
`ok=false` 不许被谎报成成功）。这里问的是下一个问题：记了之后谁去做。

  · `ok=False` -> 工单真被开出来，且能从 `HumanApprovalQueue` 捞到。
    **捞不到就等于没人知道**：任务已经落 FAILED，只按 BLOCKED 捞永远看不见它。
  · 返回 `None`（压根没有补偿引用）-> **不许**开单。每一次驳回都会走到那一步，
    低风险任务占绝大多数，开了就是给队列灌噪音，真有事的那条反而被淹掉。
  · `ok=True` -> 与改造前逐字节一致：迁移 detail 里只有 operator / note 两个键。
  · 「先回滚再改状态」的顺序没被这次改动掉个个（phase-4.md:20）。
  · 重复投递同一次驳回，工单不许被开两遍（守卫与幂等闸都在补偿前面，P1-4）。

**不设桩**：`sandbox_git_apply` 走真实现，工作目录是 `prepare_sandbox_workdir` 建的
真 git 仓库，补丁由 `git diff` 现造。补偿是有外部副作用的动作，把它换成假件就等于
把外部结果写死为终态（铁律 8）—— 那正是本轨要修的毛病的另一种形态。
"""

from __future__ import annotations

import logging
import pathlib
import subprocess

import pytest

from maos.artifacts import KIND_COMPENSATION, KIND_PATCH_SET
from maos.contracts import events as E
from maos.contracts.events import Topic
from maos.contracts.states import IllegalTransition, PlanState, TaskState
from maos.core.control_plane import (
    COMPENSATION_TICKET,
    ENV_SANDBOX_WORKDIR,
    TICKET_PREFIX,
    ControlPlane,
)
from maos.core.eventbus import InMemoryEventBus
from maos.core.store import SqliteStore
from maos.runtime.gate import HumanApprovalQueue
from maos.tools.sandbox import prepare_sandbox_workdir, sandbox_git_apply

TRACE = "trace-t96"
OPERATOR = "沈思锴"
NOTE = "改动不合规，回退"


# ======================================================================
# 夹具
# ======================================================================
def _build():
    store = SqliteStore()
    store.init_schema()
    bus = InMemoryEventBus()
    return store, bus, ControlPlane(store, bus)


def _make_task(cp, *, effect_risk="H") -> tuple[str, str]:
    plan_id = cp.create_plan(goal="补偿失败处置", trace_id=TRACE, tasks=[{
        "role": "coding", "title": "变更生产环境配置", "inputs": {}, "acceptance": [],
        "effect_risk": effect_risk, "risk_level": "M",
    }])
    cp.start_plan(plan_id)
    return plan_id, cp.store.list_tasks(plan_id)[0]["task_id"]


def _to_blocked(bus, cp, plan_id, task_id, artifacts) -> None:
    """把 effect_risk=H 的任务推到「等人工审批」那一刻。

    `artifacts` 显式传：给不给 patch_set 决定了控制面附不附补偿引用，
    而「有没有补偿引用」正是本文件里 None 分支与失败分支的分界。
    """
    cp.claim(task_id, "w1", 1)
    bus.publish(Topic.TASK_RESULT, E.task_result(
        plan_id=plan_id, task_id=task_id, attempt=1, trace_id=TRACE,
        status="ok", artifacts=artifacts))
    bus.drain()
    bus.publish(Topic.REVIEW_VERDICT, E.review_verdict(
        plan_id=plan_id, task_id=task_id, attempt=1, trace_id=TRACE,
        verdict="pass", findings=[], gate_results={}))
    bus.drain()
    assert cp.store.get_task(task_id)["state"] == TaskState.BLOCKED, \
        "前提没成立：effect_risk=H 且闸过了，任务应停在 BLOCKED 等人工"


def _real_patch(workdir: str) -> tuple[dict, str, pathlib.Path]:
    """在 workdir 里现造一份**真能打上去**的补丁，返回 (patch_set, 原文, 目标文件)。

    造法同 `test_governance.py`（改文件 -> git diff -> 还原），不写死 diff：写死要连
    @@ 行号和上下文一起写死，靶场改一个字这里就跟着挂，而症状会把排查方向引向沙箱。
    """
    target = pathlib.Path(workdir) / "auth" / "session.py"
    original = target.read_text(encoding="utf-8")
    target.write_text(original + "\n\n# T96 补偿验收用的追加行\n", encoding="utf-8")
    diff = subprocess.run(["git", "-C", workdir, "diff"],
                          capture_output=True, text=True, timeout=60).stdout
    subprocess.run(["git", "-C", workdir, "checkout", "--", "."],
                   capture_output=True, timeout=60)
    assert diff.strip(), "git diff 没产出补丁，靶场副本可能没建成 git 仓库"
    return ({"files": [{"path": "auth/session.py", "diff": diff}],
             "summary": "T96 补偿验收：给 session.py 追加一行",
             "self_check": {"build": "pass", "lint": "pass"}},
            original, target)


def _reject(cp, task_id, note=NOTE) -> None:
    cp.human_decision(task_id, approved=False, operator=OPERATOR, note=note)


def _failed_hop(store, plan_id, task_id) -> dict:
    """取落 FAILED 那一跳的 detail —— 工单就挂在它上面。"""
    hops = [e for e in store.list_event_log(plan_id)
            if e["event_type"] == "StateTransition" and e["task_id"] == task_id
            and e["to_state"] == TaskState.FAILED]
    assert len(hops) == 1, f"应当只有一跳落 FAILED，实际 {len(hops)}"
    return hops[0]["detail"]


def _executed(store, plan_id) -> list[dict]:
    return [e for e in store.list_event_log(plan_id)
            if e["event_type"] == "CompensationExecuted"]


# ======================================================================
# 1. ok=False —— 工单必须真被开出来，且捞得到
# ======================================================================
def test_failed_rollback_opens_a_manual_ticket(tmp_path, monkeypatch):
    """补丁反着打不上 -> 开一张人工工单，且从 `HumanApprovalQueue` 捞得到。

    造失败的手法是**真实**的：workdir 是一个真 git 仓库，补丁也是真的，只是那份
    正向补丁**从没打进去过** —— 于是 `git apply -R` 在 apply 阶段被 git 自己拒掉。
    这比「把 workdir 指到一个非 git 目录」更接近现场：目录、仓库、补丁三样都对，
    只有「产物到底在不在外面」这一格不对，而那正是补偿唯一要回答的问题。
    """
    workdir = prepare_sandbox_workdir(str(tmp_path / "repo"))
    monkeypatch.setenv(ENV_SANDBOX_WORKDIR, workdir)
    patch, _original, _target = _real_patch(workdir)

    store, bus, cp = _build()
    plan_id, task_id = _make_task(cp)
    _to_blocked(bus, cp, plan_id, task_id,
                [{"kind": KIND_PATCH_SET, "content": patch}])
    assert [a for a in store.list_artifacts(task_id) if a["kind"] == KIND_COMPENSATION], \
        "前提没成立：交了 patch_set 就该自动附着补偿引用"

    _reject(cp, task_id)

    detail = _executed(store, plan_id)[0]["detail"]
    assert detail["ok"] is False, "前提没成立：这份补丁本就不该反向打得上"
    assert detail["error"]["stage"] == "apply", \
        f"要造的是「补丁对不上」，实际卡在 {detail['error']['stage']}"

    tickets = HumanApprovalQueue(store, cp).compensation_tickets(plan_id)
    assert len(tickets) == 1, \
        f"回滚没做成却没开工单 —— 这就是改造前那句「不升级、不叫人」（实际 {len(tickets)} 张）"

    ticket = tickets[0]
    assert ticket["ticket_id"] == f"{TICKET_PREFIX}{task_id}"
    assert ticket["assignee"] == OPERATOR, "缺省派给驳回的人 —— 他此刻最清楚上下文"
    assert ticket["task_id"] == task_id and ticket["plan_id"] == plan_id
    assert ticket["reason"] == NOTE
    assert ticket["workdir"] == workdir, "工单不写清去哪儿看，人拿到也不知道该做什么"
    assert ticket["error"] == detail["error"], "error 要原样抄，不许改写或归纳"
    assert ticket["todo"] and all(isinstance(s, str) for s in ticket["todo"])
    assert ticket["opened_at"]

    # 工单与状态迁移是**同一条**记录，不用再去 join 第二张表
    assert _failed_hop(store, plan_id, task_id)[COMPENSATION_TICKET] == ticket
    assert store.get_task(task_id)["state"] == TaskState.FAILED
    assert store.get_plan(plan_id)["state"] == PlanState.FAILED


def test_failed_rollback_says_it_out_loud(tmp_path, monkeypatch, caplog):
    """光落进库里不算「有人管」—— 屏幕上必须有一句人看得懂的话。

    判据来自派单第 3 条：补偿失败之后，只看跑场景的屏幕输出就该看得出
    「有一次回滚没成功，已开工单 <id>」，而不是要去查库才知道。
    `maos.main` 里 `logging.basicConfig(level=INFO)` 把它送上屏幕，
    `scripts/make_evidence.py` 把同一份输出收进 `run.log`。
    """
    workdir = prepare_sandbox_workdir(str(tmp_path / "repo"))
    monkeypatch.setenv(ENV_SANDBOX_WORKDIR, workdir)
    patch, _original, _target = _real_patch(workdir)

    store, bus, cp = _build()
    plan_id, task_id = _make_task(cp)
    _to_blocked(bus, cp, plan_id, task_id,
                [{"kind": KIND_PATCH_SET, "content": patch}])

    with caplog.at_level(logging.ERROR, logger="maos.cp"):
        _reject(cp, task_id)

    spoken = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(spoken) == 1, f"补偿失败该且只该喊一声，实际 {len(spoken)} 声：{spoken}"
    assert "回滚没成功" in spoken[0] and f"{TICKET_PREFIX}{task_id}" in spoken[0], \
        f"这句话得让人看懂发生了什么、单号是多少，实际是：{spoken[0]}"


# ======================================================================
# 2. 返回 None —— 没有补偿引用就不许开单
# ======================================================================
def test_no_compensation_reference_opens_no_ticket(tmp_path, monkeypatch):
    """任务压根没有补偿引用时，驳回**不许**开工单。

    没有 patch_set 就没有可以反着打的东西，`_execute_compensation` 返回 None ——
    那是**正确行为，不是失败**（退款 / 理赔 / 应付账款那几个域的任务全走这条路，
    见 `refund/compensate.py` 开头那段）。这里每次驳回都开一张的话，队列里全是
    「无事可做」的单子，真有产物留在外面的那一张反而被淹掉。

    workdir 照样设好：要验的是「因为没有补偿引用所以不开单」，
    不是「因为配置缺失抛在半路上」。
    """
    monkeypatch.setenv(ENV_SANDBOX_WORKDIR, prepare_sandbox_workdir(str(tmp_path / "repo")))
    store, bus, cp = _build()
    plan_id, task_id = _make_task(cp)
    _to_blocked(bus, cp, plan_id, task_id, [])

    assert not [a for a in store.list_artifacts(task_id) if a["kind"] == KIND_COMPENSATION], \
        "前提没成立：没交 patch_set 就不该有补偿引用"
    _reject(cp, task_id)

    assert _executed(store, plan_id) == [], "没试过就没有可记的事实（同缺 workdir 那条口径）"
    assert HumanApprovalQueue(store, cp).compensation_tickets(plan_id) == [], \
        "无补偿引用也开单 —— 每个低风险任务被驳回都会刷一条，队列立刻失去信噪比"
    assert set(_failed_hop(store, plan_id, task_id)) == {"operator", "note"}, \
        "这条路径上的迁移记录必须与改造前一模一样"


# ======================================================================
# 3. ok=True —— 与改造前逐字节一致
# ======================================================================
def test_successful_rollback_changes_nothing(tmp_path, monkeypatch):
    """回滚真做成时，行为与改造前逐字节一致：不开单、迁移 detail 只有原来那两个键。

    顺带把「补偿真的还原了文件」再钉一遍：事件说 ok=True 而磁盘上没还原，
    是 C-5 反例里最贵的那种失败形态，只有比对文件内容才看得出来。
    """
    workdir = prepare_sandbox_workdir(str(tmp_path / "repo"))
    monkeypatch.setenv(ENV_SANDBOX_WORKDIR, workdir)
    patch, original, target = _real_patch(workdir)

    forward = sandbox_git_apply(patch, workdir)
    assert forward["ok"], f"正向应用就失败了，补丁没造对: {forward.get('error')}"
    assert target.read_text(encoding="utf-8") != original

    store, bus, cp = _build()
    plan_id, task_id = _make_task(cp)
    _to_blocked(bus, cp, plan_id, task_id,
                [{"kind": KIND_PATCH_SET, "content": patch}])
    _reject(cp, task_id)

    assert _executed(store, plan_id)[0]["detail"]["ok"] is True
    assert target.read_text(encoding="utf-8") == original, "事件说成功，文件却没还原"
    assert HumanApprovalQueue(store, cp).compensation_tickets(plan_id) == [], \
        "回滚做成了还开单，等于给人派一件已经不存在的活"
    assert set(_failed_hop(store, plan_id, task_id)) == {"operator", "note"}


# ======================================================================
# 4. 顺序：先回滚，再改状态
# ======================================================================
def test_compensation_still_runs_before_the_state_falls(tmp_path, monkeypatch):
    """开工单这件事不许把「先回滚再改状态」的顺序掉个个（phase-4.md:20）。

    「先知道成不成功再决定状态」听着更顺，但那要求状态等补偿的结果 —— 而状态一旦
    先落 FAILED，「这个任务的产物还在外面」就没人记得了。所以处置是**加在补偿之后、
    迁移之前**的一步，两条既有边界一条都不动。
    """
    workdir = prepare_sandbox_workdir(str(tmp_path / "repo"))
    monkeypatch.setenv(ENV_SANDBOX_WORKDIR, workdir)
    patch, _original, _target = _real_patch(workdir)

    store, bus, cp = _build()
    plan_id, task_id = _make_task(cp)
    _to_blocked(bus, cp, plan_id, task_id,
                [{"kind": KIND_PATCH_SET, "content": patch}])
    _reject(cp, task_id)

    rows = store.list_event_log(plan_id)
    executed_at = next(i for i, e in enumerate(rows)
                       if e["event_type"] == "CompensationExecuted")
    failed_at = next(i for i, e in enumerate(rows)
                     if e["event_type"] == "StateTransition"
                     and e["task_id"] == task_id and e["to_state"] == TaskState.FAILED)
    assert executed_at < failed_at, \
        "补偿被挪到状态迁移后面了 —— 状态先落 FAILED，产物还在外面这件事就没人记得"
    # 工单与那一跳同生共死：它就写在 FAILED 那条迁移的 detail 里
    assert COMPENSATION_TICKET in rows[failed_at]["detail"]


# ======================================================================
# 5. 重复投递 —— 工单不许开两张
# ======================================================================
def test_repeated_reject_opens_the_ticket_only_once(tmp_path, monkeypatch):
    """同一条驳回投递两次：补偿只跑一遍，工单也只开一张。

    守卫（`assert_transition`）与幂等闸都排在补偿前面（P1-4 修的就是这个），
    新加的处置也必须站在它们后面 —— 站到前面去，重复投递就会刷出第二张单，
    而人看见两张单会以为发生了两次失败。
    """
    workdir = prepare_sandbox_workdir(str(tmp_path / "repo"))
    monkeypatch.setenv(ENV_SANDBOX_WORKDIR, workdir)
    patch, _original, _target = _real_patch(workdir)

    store, bus, cp = _build()
    plan_id, task_id = _make_task(cp)
    _to_blocked(bus, cp, plan_id, task_id,
                [{"kind": KIND_PATCH_SET, "content": patch}])

    _reject(cp, task_id)
    assert len(HumanApprovalQueue(store, cp).compensation_tickets(plan_id)) == 1

    with pytest.raises(IllegalTransition):
        _reject(cp, task_id)

    assert len(_executed(store, plan_id)) == 1, "补偿被打了两遍（铁律 8）"
    tickets = HumanApprovalQueue(store, cp).compensation_tickets(plan_id)
    assert len(tickets) == 1, f"工单被开了 {len(tickets)} 张，重复驳回不该刷第二张"
