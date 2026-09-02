"""意图派发与权限闸的测试。

三条断言是安全断言，删掉任何一条这个模块就白写了：

* **R1**：名单内的人说「同意」，`decide()` 零调用（`test_r1_*`）。
* **权限闸在意图闸之前**：名单外的人说「同意」拿到 `KIND_DENIED`，
  而不是一张告诉他「照这个格式发就能批」的确认卡片。
* **空名单 = 谁都不许**：配置缺失时放行是最经典的权限漏洞形态。

全部离线：不连 Matrix、不走网络、不调模型。`Intent` 用本地替身（生产代码按字段名
读，不 import `maos.nlu`），store 用内存 SQLite。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from hiclaw.matrix_bus import current_approvers as _hiclaw_current_approvers
from hiclaw.matrix_bus import parse_approvers as _hiclaw_parse_approvers
from hiclaw.matrix_bus import ENV_APPROVERS as _HICLAW_ENV_APPROVERS
from maos.contracts.states import PlanState, TaskState
from maos.core.store import SqliteStore
from maos.runtime.gate import HumanApprovalQueue
from maos.runtime.intent_dispatch import (ACTION_APPROVE, ACTION_REJECT,
                                          ACTION_STATUS, ACTION_UNKNOWN,
                                          ENV_APPROVERS, KIND_CONFIRM,
                                          KIND_DENIED, KIND_DONE, KIND_IGNORED,
                                          DispatchResult, dispatch_intent,
                                          resolve_approvers)

APPROVER = "@boss:maos.local"
#: 故意留的反例账号：它发同样的指令必须被顶回「无审批权限」（派单 §0.1 的现场读数）。
OUTSIDER = "@intern:maos.local"

PLAN_ID = "plan_t68"
TASK_ID = "task_997ca4541e66"


# -- 替身 ---------------------------------------------------------------------
@dataclass(frozen=True)
class _Intent:
    """`maos/nlu/intent.py` 的 `Intent` 的本地替身，字段名逐字对齐跨轨契约 §1。

    不 import T66 的真 `Intent`：本轨与它并行开发，import 会让测试依赖另一轨的
    进度。生产代码按字段名读，所以任何同形状的对象都是合法输入。
    """

    action: str
    task_id: str = ""
    reason: str = ""
    confidence: str = "low"
    raw_text: str = ""
    detail: dict = field(default_factory=dict)


class _SpyCP:
    """控制面替身。R1 下自然语言路径**一次都不该碰它**，所以碰了就当场炸。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def human_decision(self, task_id, approved, operator, note="") -> None:
        self.calls.append((task_id, approved, operator, note))
        raise AssertionError("R1 违例：自然语言路径把状态迁移做掉了")


class _SpyQueue(HumanApprovalQueue):
    """真 `HumanApprovalQueue` 的子类，只把 `decide` 换成计数器。

    用子类而不是鸭子替身：万一哪天 `dispatch_intent` 真去 `isinstance` 或者摸了
    队列的别的方法，替身得长得跟真的一样，否则这条钉子测试会因为「替身不像」
    而假绿。
    """

    def __init__(self, store, cp) -> None:
        super().__init__(store, cp)
        self.decided: list[tuple] = []

    def decide(self, task_id: str, approved: bool, operator: str, note: str = "") -> None:
        self.decided.append((task_id, approved, operator, note))
        raise AssertionError("R1 违例：自然语言路径调了 decide()")


def _store(state=TaskState.BLOCKED) -> SqliteStore:
    store = SqliteStore()
    store.init_schema()
    store.insert_plan({"plan_id": PLAN_ID, "trace_id": "tr_t68", "goal": "自然语言接入",
                       "state": PlanState.RUNNING})
    store.insert_task({"task_id": TASK_ID, "plan_id": PLAN_ID, "trace_id": "tr_t68",
                       "role": "coding", "title": "改点东西", "state": state,
                       "attempt": 1, "effect_risk": "H"})
    return store


@pytest.fixture()
def env():
    """一套完整的替身：store / cp / queue 三件套，外加零调用自查。"""
    store = _store()
    cp = _SpyCP()
    queue = _SpyQueue(store, cp)
    return store, cp, queue


def _assert_nothing_executed(cp: _SpyCP, queue: _SpyQueue) -> None:
    assert queue.decided == [], "R1：自然语言路径不许调 decide()"
    assert cp.calls == [], "R1：自然语言路径不许迁移状态"


# -- 四种结局与判定顺序（§5.1）-------------------------------------------------
def test_unknown_intent_is_ignored(env):
    """认不出来的一句话产不出动作，也不该被判「无审批权限」——闲聊不是越权。"""
    store, cp, queue = env
    r = dispatch_intent(_Intent(action=ACTION_UNKNOWN, raw_text="今天天气不错"),
                        store=store, cp=cp, sender=OUTSIDER, approvers=[APPROVER])

    assert r.kind == KIND_IGNORED
    _assert_nothing_executed(cp, queue)


def test_outsider_approve_is_denied(env):
    """名单外账号说「同意」→ 顶回无审批权限，且什么都没执行。"""
    store, cp, queue = env
    r = dispatch_intent(_Intent(action=ACTION_APPROVE, task_id=TASK_ID, raw_text="同意"),
                        store=store, cp=cp, sender=OUTSIDER, approvers=[APPROVER])

    assert r.kind == KIND_DENIED
    assert "无审批权限" in r.text and OUTSIDER in r.text
    _assert_nothing_executed(cp, queue)


def test_permission_gate_runs_before_intent_kind(env):
    """🔴 判定顺序钉子：先查「你是谁」，再谈「你想干什么」。

    顺序反过来的话，名单外的人也会收到一张格式完整的确认卡片，等于当面告诉他
    「照这个格式发就能批」—— 把权限边界当成了 UI 提示。所以这里要的不只是
    「不放行」，而是**不许出现 CONFIRM 的那句话**。
    """
    store, cp, queue = env
    denied = dispatch_intent(_Intent(action=ACTION_APPROVE, task_id=TASK_ID),
                             store=store, cp=cp, sender=OUTSIDER, approvers=[APPROVER])
    allowed = dispatch_intent(_Intent(action=ACTION_APPROVE, task_id=TASK_ID),
                              store=store, cp=cp, sender=APPROVER, approvers=[APPROVER])

    assert denied.kind == KIND_DENIED, "同一句话，名单外必须是 DENIED"
    assert allowed.kind == KIND_CONFIRM, "同一句话，名单内才走到意图判定"
    assert "/approve" not in denied.text, "确认指令的格式不许漏给名单外的人"
    _assert_nothing_executed(cp, queue)


# -- 🔴 R1：自然语言只确认，不执行（§5.2）--------------------------------------
def test_r1_approve_only_confirms_never_executes(env):
    """🔴 R1 钉子：名单内的人说「同意」，`decide()` 零调用。

    模型判断错一次 = 越权批掉一笔生产变更（铁律 8：权威动作不能由推断产生）。
    这条测试红了就说明自然语言层开始替人做决定了 —— 那不是回归，是安全事故。
    """
    store, cp, queue = env
    r = dispatch_intent(_Intent(action=ACTION_APPROVE, task_id=TASK_ID, raw_text="同意，批了吧"),
                        store=store, cp=cp, sender=APPROVER, approvers=[APPROVER])

    assert r.kind == KIND_CONFIRM
    assert r.task_id == TASK_ID
    assert f"/approve {TASK_ID}" in r.text, "确认卡片必须给出显式指令的原文"
    _assert_nothing_executed(cp, queue)


def test_r1_reject_only_confirms_never_executes(env):
    """驳回同理：一句「这个先别过」也只换来一张确认卡片。"""
    store, cp, queue = env
    r = dispatch_intent(_Intent(action=ACTION_REJECT, task_id=TASK_ID,
                                reason="金额对不上", raw_text="这个先别过"),
                        store=store, cp=cp, sender=APPROVER, approvers=[APPROVER])

    assert r.kind == KIND_CONFIRM
    assert f"/reject {TASK_ID}" in r.text
    _assert_nothing_executed(cp, queue)


def test_r1_holds_for_every_action(env):
    """把四种 action 全跑一遍，`decide()` 仍是零调用 —— 没有哪条支路能执行。"""
    store, cp, queue = env
    for action in (ACTION_APPROVE, ACTION_REJECT, ACTION_STATUS, ACTION_UNKNOWN):
        for sender in (APPROVER, OUTSIDER):
            dispatch_intent(_Intent(action=action, task_id=TASK_ID),
                            store=store, cp=cp, sender=sender, approvers=[APPROVER])
    _assert_nothing_executed(cp, queue)


def test_approve_without_task_id_is_not_confirmed(env):
    """没有 task_id 的 approve 不许被猜成某个任务（R2 的下游防线）。"""
    store, cp, queue = env
    r = dispatch_intent(_Intent(action=ACTION_APPROVE, task_id="", raw_text="批了"),
                        store=store, cp=cp, sender=APPROVER, approvers=[APPROVER])

    assert r.kind == KIND_IGNORED
    assert r.task_id == ""
    _assert_nothing_executed(cp, queue)


# -- 查询类 -------------------------------------------------------------------
def test_status_reads_the_real_state(env):
    """状态查询回的是库里那一条，不是编的。"""
    store, cp, queue = env
    r = dispatch_intent(_Intent(action=ACTION_STATUS, task_id=TASK_ID, raw_text="现在什么情况"),
                        store=store, cp=cp, sender=APPROVER, approvers=[APPROVER])

    assert r.kind == KIND_DONE
    assert TASK_ID in r.text
    assert TaskState.BLOCKED in r.text
    _assert_nothing_executed(cp, queue)


def test_status_of_unknown_task_is_not_done(env):
    """查不到就说查不到：把「没查到」也算成 DONE，上游就分不出答了和没答。"""
    store, cp, queue = env
    r = dispatch_intent(_Intent(action=ACTION_STATUS, task_id="task_不存在"),
                        store=store, cp=cp, sender=APPROVER, approvers=[APPROVER])

    assert r.kind == KIND_IGNORED
    assert "没查到" in r.text


def test_status_needs_permission_too(env):
    """状态也是内部信息：名单外的人问「现在什么情况」同样顶回去。"""
    store, cp, queue = env
    r = dispatch_intent(_Intent(action=ACTION_STATUS, task_id=TASK_ID),
                        store=store, cp=cp, sender=OUTSIDER, approvers=[APPROVER])

    assert r.kind == KIND_DENIED
    assert TASK_ID not in r.text, "拒绝的回话不该顺带确认这个任务存在"


# -- 🔴 空名单 = 谁都不许（§5.3）----------------------------------------------
@pytest.mark.parametrize("action", [ACTION_APPROVE, ACTION_REJECT, ACTION_STATUS])
@pytest.mark.parametrize("sender", [APPROVER, OUTSIDER])
def test_empty_approver_list_denies_everyone(env, action, sender):
    """🔴 `MAOS_APPROVERS` 未设置 → 谁都不许，包括平时那个审批人。

    配置缺失时放行是最经典的权限漏洞形态，而它在测试里长得像「默认行为」。
    这条测试就是不让它长成默认行为。
    """
    store, cp, queue = env
    approvers = resolve_approvers({})                    # 未设置
    assert approvers == frozenset()

    r = dispatch_intent(_Intent(action=action, task_id=TASK_ID),
                        store=store, cp=cp, sender=sender, approvers=approvers)

    assert r.kind == KIND_DENIED
    _assert_nothing_executed(cp, queue)


def test_empty_approver_list_still_ignores_chitchat(env):
    """空名单下闲聊仍是 IGNORED —— `UNKNOWN` 在权限闸之前，且它产不出任何动作。

    这不是上一条的例外：`UNKNOWN` 从来就不会变成动作，放行它不扩大任何权限面，
    而把闲聊判成「无审批权限」只会在房间里刷噪音。
    """
    store, cp, queue = env
    r = dispatch_intent(_Intent(action=ACTION_UNKNOWN, raw_text="哈哈"),
                        store=store, cp=cp, sender=OUTSIDER, approvers=resolve_approvers({}))

    assert r.kind == KIND_IGNORED
    _assert_nothing_executed(cp, queue)


# -- 名单口径不许分叉（§5.3）--------------------------------------------------
#: 覆盖真实会出现的形态：未设置 / 空 / 纯空白 / 单个 / 逗号多个 / 带空格 /
#: 空项与尾随逗号 / 大小写差异（Matrix localpart 大小写敏感，不许 lower）。
APPROVER_ENVS = [
    {},
    {ENV_APPROVERS: ""},
    {ENV_APPROVERS: "   "},
    {ENV_APPROVERS: ","},
    {ENV_APPROVERS: APPROVER},
    {ENV_APPROVERS: f"{APPROVER},{OUTSIDER}"},
    {ENV_APPROVERS: f" {APPROVER} , {OUTSIDER} "},
    {ENV_APPROVERS: f"{APPROVER},,{OUTSIDER},"},
    {ENV_APPROVERS: "@Boss:maos.local,@boss:maos.local"},
    {ENV_APPROVERS: f"{APPROVER},{APPROVER}"},
]


@pytest.mark.parametrize("env_map", APPROVER_ENVS)
def test_resolve_approvers_matches_the_existing_room_bridge(env_map):
    """同一份 env，本模块与房间侧给出同一个名单 —— 口径分叉是安全事故。

    一边放行一边拒绝，症状是「同一个人在两条路径上待遇不同」，而这种 bug 不会
    自己冒烟。对的是 `hiclaw/matrix_bus.py` 的**公开** API
    （`parse_approvers` / `current_approvers`），不是私有方法：私有面不稳定，
    而要钉住的本来就是行为，不是符号。
    """
    assert resolve_approvers(env_map) == _hiclaw_current_approvers(env_map)
    assert resolve_approvers(env_map) == _hiclaw_parse_approvers(env_map.get(ENV_APPROVERS))


def test_env_key_name_matches_the_room_bridge():
    """连键名都不许分叉：读错键 = 永远拿到空名单 = 谁都批不动。"""
    assert ENV_APPROVERS == _HICLAW_ENV_APPROVERS


def test_approver_ids_are_case_sensitive():
    """`@Boss` 与 `@boss` 是两个人。自作主张 lower() 会把权限面悄悄放宽。"""
    got = resolve_approvers({ENV_APPROVERS: "@Boss:maos.local"})
    assert got == frozenset({"@Boss:maos.local"})
    assert APPROVER not in got


def test_dispatch_result_rejects_unknown_kind():
    """结局只有四种。多出一种就意味着上游要开始 `if kind == ...` 猜了。"""
    with pytest.raises(ValueError):
        DispatchResult(kind="whatever", text="")
