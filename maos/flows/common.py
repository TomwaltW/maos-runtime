"""场景共用装配层 —— 六件套构造、驱动循环、快照打印、演示靶场接线。

build() 的入参与返回值都是冻结契约（C-3 / C-4）：
返回六元组的**位序与类型**不许调换，解包写法固定为
``store, bus, cp, model, worker, gate = build(...)``。

任何场景都不许绕过 build() 自己拼装 store/bus/cp/model/worker/gate ——
留第二条构造路径，两条一定会漂。

## 软件域两个场景（1 / 2）的证据是真跑出来的

本文件下半段是场景 1/2 的靶场接线，它替掉的是原先的**预置报告**。三件一起：

  * ``GOOD_PATCH`` / ``BAD_PATCH`` 由 ``_build_demo_patches()`` 在导入时**现造** ——
    改好靶场副本、``git diff``、还原。手写 diff 要连 @@ 行号和上下文一起写死，
    靶场注释改一个字就打不上，而症状是「补丁应用失败」，很容易被当成沙箱的锅。
  * ``sandbox_workdir()`` 按 run 现造一份靶场工作目录，跑完删掉。
  * ``patch_verifier()`` 是给 ``run_until_settled(before_review=...)`` 的钩子：
    本轮交出了补丁、还没有测试报告的任务，**当场真跑一次沙箱**（还原靶场 ->
    ``git apply`` -> ``pytest``），把真报告落成 test_report artifact。

### 为什么这一步落在演示装配层，而不是 Testing Agent

DAG 是 ``requirement -> architecture -> coding -> testing``，而
``dispatch_ready()`` 要求依赖项 **DONE** 才派发。于是 coding 过闸的那一刻
testing 还没派出去，报告不可能存在 —— 这个环正是当初 ``seed_scripted_report()``
存在的全部理由（见 docs/DECISIONS.md 「per-task 严格闸与四节点 DAG 不自洽」一条）。

判据一字不让（代码类任务无报告即 blocker、不回落 self_check），所以妥协仍然
放在场景侧：**同一个插入点，内容从脚本化常量换成真跑出来的产物**。Gate 一行
判据不改，Testing Agent 那一节点照旧真跑一遍它自己的回归。
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Callable

from maos.agents.testing import make_test_report, record_seeded_artifact
from maos.artifacts import KIND_PATCH_SET, KIND_TEST_REPORT
from maos.contracts.events import new_id
from maos.contracts.states import PlanState, TaskState
from maos.core.control_plane import ControlPlane
from maos.core.eventbus import EventBus, create_event_bus
from maos.core.store import SqliteStore
from maos.model.client import ModelClient, ScriptedModelClient
from maos.runtime.gate import ReviewerGate
from maos.runtime.worker import WorkerRuntime
from maos.tools.port import invoke_tool
from maos.tools.sandbox import (
    GIT_APPLY_PORT,
    MODE_NOT_RUN,
    PYTEST_RUN_PORT,
    prepare_sandbox_workdir,
    sandbox_git_apply,
    sandbox_pytest_run,
)

log = logging.getLogger("maos.flows")


def _wrap_matrix(inner: EventBus) -> EventBus:
    """把 inner bus 包进 MatrixEventBus；任何失败都告警回退，不让演示中断。

    HiClaw 对接层由 Task-E 落地，在那之前这里恒走 ImportError 分支。
    """
    try:
        from hiclaw.matrix_bus import MatrixBusConfig, MatrixEventBus
    except ImportError as exc:
        log.warning("Matrix 总线不可用（%s），回退进程内 EventBus", exc)
        return inner
    try:
        return MatrixEventBus(inner, MatrixBusConfig.from_env())
    except Exception as exc:  # noqa: BLE001 —— 连接/配置失败一律降级
        log.warning("Matrix 总线构造失败（%s），回退进程内 EventBus", exc)
        return inner


#: **装配级**业务域后端开关（T126）。设成 `postgres` 时，这一次 `build()` 装出来的
#: store 上的业务域连接走 `MAOS_PG_DSN` 那个库；不设时**一个字节都不变**。
#:
#: 为什么另起一个变量，而不是直接用既有的 `MAOS_DOMAIN_BACKEND`：那个是**进程级**
#: 的，一设就把本进程每一条业务域连接都拨到 PG，包括按设计就该是一次性副本的库。
#: 完整的坑与实测症状记在 `maos/domain/_dbport.py::STORE_BACKEND_ATTR` 上 ——
#: 一句话版：圆桌的核算预演用 `_memory_store()` 现造一个一次性内存库，进程级开关
#: 会让它写的 `refund_case` 落进真 PG 库，紧接着真跑的 `refund.intake` 撞上受理
#: 幂等闸，整条 DAG 停在第一步。装配级开关把「一次性副本」的语义原样保住。
#:
#: **控制面不跟着走**：`plan` / `task` / `artifact` / `event_log` 仍在本地 SQLite，
#: 与 `docs/architecture.md` §5 那张表逐字一致，也是 `docs/phases/phase-10.md` §8
#: 「控制面表上 PG」明确不做的那条。这里换的只有业务对象那 16 张表。
FLOW_DOMAIN_BACKEND_ENV = "MAOS_FLOW_DOMAIN_BACKEND"


def _mark_flow_domain_backend(store: object) -> None:
    """把 `MAOS_FLOW_DOMAIN_BACKEND` 的值盖到这条 store 上。没设就什么都不做。

    值不认（拼错一个字母）时**当场抛**，不回落 sqlite —— 口径同
    `_dbport.backend_name()`：回落的话你会以为验过了 PG，其实一行都没跑。
    """
    import os                                                  # noqa: PLC0415

    name = os.environ.get(FLOW_DOMAIN_BACKEND_ENV, "").strip()
    if not name:
        return
    from maos.domain import _dbport                            # noqa: PLC0415

    _dbport.mark_store_backend(store, name)


#: **装配级**知识层后端开关（T132）。设成 `postgres` 时，这一次 `build()` 装出来的
#: store 在**知识层那条路**（`kb_doc` / `kb_doc_fts` / `kb_schema_version` 三张表，
#: 外加检索器的 fts / vector 两条通道）上走 `MAOS_PG_DSN` 那个库；不设时**一个
#: 字节都不变**。
#:
#: **与 `MAOS_FLOW_DOMAIN_BACKEND` 并列，不合成一个开关**：两面各判各的 ——
#: 业务域读 `_dbport.backend_of()` 挂在 store 上的标记，知识层读 `kb.port_of()` 认
#: 的端口。合成一个就表达不了「业务对象上了 PG、知识层还在本地」这个真实的中间
#: 态，而那恰是本仓库从 T115 一路走到 T132 时实际所处的状态。
#:
#: 为什么不直接用进程级的 `MAOS_STORE_BACKEND`：理由与业务域那半逐字相同（一次性
#: 内存副本会被一起拨到 PG），完整原文记在 `maos/kb/__init__.py::KB_PORT_ATTR`。
#:
#: **控制面不跟着走**：`plan` / `task` / `artifact` / `event_log` 仍在本地 SQLite，
#: 与 `docs/architecture.md` §5 那张表逐字一致。这里换的只有知识层那三张表。
FLOW_KB_BACKEND_ENV = "MAOS_FLOW_KB_BACKEND"


def _mark_flow_kb_backend(store: object) -> None:
    """把 `MAOS_FLOW_KB_BACKEND` 的值接到这条 store 的知识层那一面。没设就什么都不做。

    值不认（拼错一个字母）时**当场抛**，不回落 sqlite —— 口径逐字同
    `_mark_flow_domain_backend()`：回落的话你会以为验过了 PG，其实一行都没跑。
    """
    import os                                                  # noqa: PLC0415

    name = os.environ.get(FLOW_KB_BACKEND_ENV, "").strip()
    if not name:
        return
    from maos import kb                                        # noqa: PLC0415

    kb.attach_backend(store, name)


def build(script: dict[str, str], *, matrix: bool = False, model: ModelClient | None = None,
          model_factory: Callable[[str, str], ModelClient] | None = None,
          store: object | None = None):
    """装配一套完整运行时，返回冻结的六元组（C-4）。

    script：喂给缺省 ScriptedModelClient 的「关键字 -> 应答」表。
    model ：传实例则原样注入（场景 2 的 FlakyModel 由此进入），不再按 script 构造。
    matrix：True 时事件总线经 HiClaw(Matrix) 转发，不可用则自动降级。
    model_factory：``(role, tier) -> ModelClient``，让 Worker 里每个角色各连各的模型
        （``runtime/worker.py``）。**缺省 None 时行为逐字节不变**：22 个 Agent 仍共用
        第 4 位返回的那一个缺省 ``model``。加参数照 C-3 走 keyword-only + 带默认值，
        返回值仍是六元组、位序与类型不动（C-4）—— 多一个入参不许换成七元组或 dataclass。
    store：**给了就用这个库，没给照旧自建一个 `:memory:`**（T122）。加它是为了让房间
        入口跑出来的案子落在 router 那个库里 —— 在这之前，`/refund` 跑完的
        `compensation_record` / `refund_request` / `payment_observation` 随着进程内
        那个 `:memory:` 一起消失，于是房间里紧接着打一句 `/assign` 必定查不到工单
        （`compensate.require_ticket` 抛 LookupError）。**不建第二条构造路径**：
        传进来的库照样走下面同一套 `init_schema()` 与装配，唯一的差别是它活得比这次
        调用长。`init_schema()` 全是 `CREATE TABLE IF NOT EXISTS`，对已经建过表的库
        是幂等的。缺省 None 时行为逐字节不变。

    总线由 `core/eventbus.py::create_event_bus` 按 `MAOS_EVENTBUS_BACKEND` 造。
    **不设这个环境变量时它返回的就是 `InMemoryEventBus`**，所以「裸 clone 不用任何
    key 跑到 7/7」这条卖点一个字节都没动；设了才走 RocketMQ，并接受它的 drain 地板。

    接这一行之前，`create_event_bus` 全仓只有测试调用过 —— 于是「总线层可替换」
    是被证过的，而**生产路径从没走过那个开关**：可替换性停在测试里，演示链路仍然
    写死内存版。差别不在默认行为（逐字节相同），在于这句话到底能不能说。
    """
    store = SqliteStore() if store is None else store
    # 业务域后端：`MAOS_FLOW_DOMAIN_BACKEND` 设了才盖标记，没设是 no-op（缺省
    # 逐字节不变）。控制面照旧 SQLite —— 换的只有那 16 张业务表落在哪个库。
    _mark_flow_domain_backend(store)
    # 知识层后端：同样是装配级、同样设了才动（T132）。两条**各拨各的** ——
    # 业务对象与知识层落在不同库是允许的中间态，不是配错。
    _mark_flow_kb_backend(store)
    store.init_schema()
    bus = create_event_bus()
    if matrix:
        bus = _wrap_matrix(bus)
    cp = ControlPlane(store, bus)
    model = ScriptedModelClient(script) if model is None else model
    worker = WorkerRuntime(worker_id="w1", bus=bus, control_plane=cp, model=model,
                           model_factory=model_factory)
    gate = ReviewerGate(store, bus, cp)
    return store, bus, cp, model, worker, gate


def run_until_settled(bus, gate, cp, plan_id: str, max_cycles: int = 20, *,
                      before_review: Callable[[object, str], None] | None = None) -> None:
    """驱动循环：drain 队列 -> 跑 Gate -> 再 drain，直到没有新进展。

    换 RocketMQ 后这个循环消失（消费者常驻），但语义完全一样。

    ``before_review`` 在「队列已排空、Gate 还没判」这一刻被调用一次，签名
    ``(cp, plan_id)``。软件域场景用它现跑沙箱回归（见 ``patch_verifier()``）——
    位置不能挪：早于 drain 结束则本轮产物还没入库，晚于 review_pending 则报告
    赶不上这一次判定。缺省 None，别的场景一行不改。
    """
    for _ in range(max_cycles):
        bus.drain()
        if before_review is not None:
            before_review(cp, plan_id)
        reviewed = gate.review_pending(plan_id)
        bus.drain()
        plan = cp.store.get_plan(plan_id)
        if plan["state"] in (PlanState.DONE, PlanState.FAILED):
            return
        if reviewed == 0:
            return
    raise RuntimeError("驱动循环未收敛")


def dump(cp, plan_id: str, title: str) -> None:
    snap = cp.snapshot(plan_id)
    print(f"\n{'=' * 68}\n{title}\n{'=' * 68}")
    print(f"Plan: {snap['plan']['state']}  |  {snap['plan']['goal']}")
    for t in snap["tasks"]:
        print(f"  · {t['title'][:34]:36s} {t['state']:16s} attempt={t['attempt']} "
              f"risk={t['risk_level']}")
    print("  状态迁移轨迹:")
    for e in snap["log"]:
        if e["event_type"] == "StateTransition":
            print(f"    {e['task_id']}  {e['from_state']:16s} -> {e['to_state']:16s} "
                  f"[{e['reason']}]")


# ---------------------------------------------------------------------------
# 演示靶场：补丁现造，工作目录按 run 现造
# ---------------------------------------------------------------------------
#: 靶场里那个留了时区 bug 的文件（``scenarios/fixture-repo/``，契约附录 C 冻结）。
SESSION_FILE = "auth/session.py"

#: 改写锚点：函数体从这一行的 docstring 起被整段替换。
_DOC_ANCHOR = '    """会话在 last_seen 之后'

#: 修好的样子 —— 两个入参都是 UTC 感知时间，直接做差，靶场两条用例全过。
_FIXED_TAIL = '''    """会话在 last_seen 之后 SESSION_TTL 之内算有效。两个入参都是 UTC 感知时间。"""
    # 两个入参都是 UTC 感知时间，直接做差就是真实年龄；本地时区只用于展示。
    return now - last_seen < SESSION_TTL
'''

#: 修不好的样子 —— 照第一轮那份**缺了「按 UTC 判定」的架构契约**写：
#: 判定按业务方本地时区做，于是会话年龄凭空多出一个时区偏移。
#: ``test_valid_session`` 照过、``test_expired_session`` 真挂，正好 1 过 1 挂。
_LOCAL_TZ_TAIL = '''    """会话在 last_seen 之后 SESSION_TTL 之内算有效。两个入参都是 UTC 感知时间。"""
    # 按业务方本地时区判定过期 —— 架构契约（第一轮）没写「按 UTC 判定」这一条。
    now_wall = now.astimezone(LOCAL_TZ).replace(tzinfo=timezone.utc)
    age = now_wall - last_seen
    return age < SESSION_TTL
'''


def _diff_of(workdir: Path, tail: str, *, summary: str) -> str:
    """把靶场副本的 ``is_session_valid`` 换成 tail，``git diff`` 出来，再还原。

    造法照抄 ``maos/tests/test_sandbox_isolation.py::golden_patch``：补丁一律现造，
    不手写。手写要连 @@ 行号和上下文一起写死，靶场注释改一个字就打不上。
    """
    target = workdir / SESSION_FILE
    head, anchor, _ = target.read_text(encoding="utf-8").partition(_DOC_ANCHOR)
    if not anchor:
        raise RuntimeError(f"{SESSION_FILE} 里找不到锚点 {_DOC_ANCHOR!r}：靶场被改过就得同步改这里")
    target.write_text(head + tail, encoding="utf-8")
    diff = subprocess.run(["git", "-C", str(workdir), "diff"],
                          capture_output=True, text=True, timeout=60).stdout
    subprocess.run(["git", "-C", str(workdir), "checkout", "--", "."],
                   capture_output=True, timeout=60)
    if not diff.strip():
        raise RuntimeError("git diff 没产出补丁 —— 靶场副本可能没建成 git 仓库")
    return json.dumps({
        "files": [{"path": SESSION_FILE, "diff": diff}],
        "summary": summary,
        # 两份补丁的 self_check 都写全 pass：场景 2 演的正是「自称完工、自检全绿、
        # 变更说明齐全，四道旧闸一条都拦不住」，拦下它的必须是跑出来的测试报告。
        "self_check": {"build": "pass", "lint": "pass"},
    }, ensure_ascii=False)


def _build_demo_patches() -> tuple[str, str]:
    """导入时现造两份补丁，共用一个一次性靶场副本，用完即删。"""
    workdir = Path(prepare_sandbox_workdir())
    try:
        return (
            _diff_of(workdir, _FIXED_TAIL,
                     summary="会话有效期改回 UTC 直减，不再绕本地墙上时间"),
            _diff_of(workdir, _LOCAL_TZ_TAIL,
                     summary="按架构契约补上会话过期判定（第一轮：按业务方本地时区）"),
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


#: 真能打在 ``auth/session.py`` 上、并真修掉时区 bug 的补丁 —— 打完靶场全过。
#: 真能打上、但**修不好**的补丁 —— 打完 ``test_expired_session`` 仍然真挂。
GOOD_PATCH, BAD_PATCH = _build_demo_patches()


@contextmanager
def sandbox_workdir():
    """按 run 现造一份靶场工作目录，跑完删掉。

    补丁只打在这个副本上，宿主的 ``scenarios/fixture-repo/`` 永远不被触碰。
    """
    workdir = prepare_sandbox_workdir()
    try:
        yield workdir
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 两个 thunk：把 ToolPort 的 entry 接到**模块全局里此刻那个名字**上（T147）
#
# 这一层有意保留着「沙箱入口可被 monkeypatch 替换」这个接缝，而
# ``ToolPort.entry`` 在 import 那一刻就绑死了原函数 —— 把 GIT_APPLY_PORT /
# PYTEST_RUN_PORT 原样传进 ``invoke_tool``，
# ``monkeypatch.setattr(flows_common, "sandbox_pytest_run", ...)`` 会**悄悄失效**。
# ``test_exec_path_in_evidence.py`` 靠它验「容器路径的 summary 口径」：那条断言若改去
# 问真 Docker，在装了 / 没装 Docker 的机器上结论相反，等于没验。
#
# 两头的调用姿势不一样，thunk 各对一边：
#   · ``invoke_tool`` 用 ``entry(**params)`` 调进来 —— 所以形参名跟着 params_schema 走；
#   · 转发时**按位置**调模块全局那个名字 —— 既有的桩是 ``lambda _wd: {...}``，
#     形参叫什么由写桩的人定，按关键字转发会 TypeError。
# 审计行认的是 ``port.name``，换 entry 不影响它记下的是哪个工具。
# ---------------------------------------------------------------------------
def _apply_entry(patch_set, workdir):
    return sandbox_git_apply(patch_set, workdir)


def _pytest_entry(workdir):
    return sandbox_pytest_run(workdir)


def verify_patch_in_sandbox(patch_set: dict, workdir: str, *,
                            store=None, extras: dict | None = None) -> dict:
    """真跑一次回归：靶场还原 -> ``git apply`` -> ``pytest``，返回 C-7 形状的报告。

    **每次都先把 workdir 整个还原成靶场基线**：上一次 attempt 的补丁留在那里，
    下一轮要么打不上、要么把「第二轮真修好了」变成「两份补丁叠出来的巧合」。

    补丁打不上按 ``tool_error`` 上报，不按「0 条失败」——本轮压根没有测试证据，
    而 Gate 对这两者的判定完全不同（铁律：tool_error 与 failed 分开报）。

    两条出口都带上执行路径（``sandbox_mode`` / ``degraded_reason``）。这是本函数
    存在感最低、也最容易漏的一段：``sandbox_pytest_run`` 早就把它们返回了，可这里
    从前只逐字段搬那六个，于是**演示当天那份报告到底是不是在容器里跑的，证据里查不到**
    —— 「容器隔离」只在日志里成立。补丁没落进沙箱那一条同样要报：pytest 压根没被
    调用过，``not-run`` 说的就是这件事，它与「容器里跑挂了」不是一回事。

    两次沙箱调用都走 ``invoke_tool``，各落一条 ``ToolInvoked``（T147）。``store`` 与
    ``extras`` 是 **keyword-only 且带缺省**：``verify_patch_in_sandbox(patch, workdir)``
    这种两个位置参数的老调用一个都不用改（``maos/tests/test_exec_path_in_evidence.py``
    就是这么调的），不传 store 时 ``invoke_tool`` 不落行，行为与从前逐字节一致。
    真正的调用方 ``patch_verifier`` 会把 store 和任务的三个 id 传进来 —— 审计行的
    ``plan_id`` **必须落在真 plan 上**，否则它在 trace 里会变成一条游离事件。
    """
    shutil.rmtree(workdir, ignore_errors=True)
    prepare_sandbox_workdir(workdir)

    applied = invoke_tool(replace(GIT_APPLY_PORT, entry=_apply_entry),
                          {"patch_set": patch_set, "workdir": workdir},
                          store=store, extras=extras)
    if not applied.get("ok"):
        err = applied.get("error") or {}
        return make_test_report(
            tool_error=(
                f"补丁没能落进沙箱（stage={err.get('stage')} path={err.get('path')}）: "
                f"{err.get('message')}"),
            sandbox_mode=MODE_NOT_RUN,
            degraded_reason="补丁没落进沙箱，pytest 未被调用",
        )

    raw = invoke_tool(replace(PYTEST_RUN_PORT, entry=_pytest_entry), {"workdir": workdir},
                      store=store, extras=extras)
    return make_test_report(
        passed=raw.get("passed") or 0,
        failed=raw.get("failed") or 0,
        errors=raw.get("errors") or 0,
        cases=raw.get("cases") or (),
        duration=raw.get("duration") or 0.0,
        tool_error=raw.get("tool_error"),
        summary=str(raw.get("summary") or ""),
        sandbox_mode=raw.get("sandbox_mode"),
        degraded_reason=raw.get("degraded_reason"),
    )


def patch_verifier(store, workdir: str) -> Callable[[object, str], None]:
    """造一个 ``before_review`` 钩子：给本轮交出补丁、还没有报告的任务补上真报告。

    只认「同 attempt 有 patch_set、且同 attempt 还没有 test_report」这一种形状 ——
    Testing 节点自己产的报告走的是 ``test.verify``，不从这里过。

    报告落库之后补一条 ``ArtifactSeeded``（``agents/testing.py::
    record_seeded_artifact``）：这条路绕开 ``on_task_result``，从前因此在
    ``trace.json`` 里留一份 ``provenance="unknown"`` 的产物 —— 一份**真跑出来的**
    报告被记成「指不到是谁产的」，评委只能靠 ``sandbox.mode`` 反推。现在事件里
    点名了本函数，``provenance`` 变成 ``artifact_seeded``，而「走的是旁路」这件事
    照旧写在脸上（不冒充 ``task_result``）。
    """
    def _verify(_cp, plan_id: str) -> None:
        for task in store.list_tasks(plan_id):
            if task["state"] != TaskState.AWAITING_REVIEW:
                continue
            arts = [a for a in store.list_artifacts(task["task_id"])
                    if a["version"] == task["attempt"]]
            if any(a["kind"] == KIND_TEST_REPORT for a in arts):
                continue
            patch = next((a for a in arts if a["kind"] == KIND_PATCH_SET), None)
            if patch is None:
                continue

            report = verify_patch_in_sandbox(
                patch["content"], workdir, store=store,
                extras={"trace_id": task.get("trace_id") or "", "plan_id": plan_id,
                        "task_id": task["task_id"]})
            artifact_id = new_id("art")
            store.insert_artifact({
                "artifact_id": artifact_id, "task_id": task["task_id"],
                "plan_id": plan_id, "kind": KIND_TEST_REPORT,
                "version": task["attempt"], "content": report,
            })
            record_seeded_artifact(
                store, plan_id=plan_id, task_id=task["task_id"],
                artifact_id=artifact_id, kind=KIND_TEST_REPORT,
                version=task["attempt"], trace_id=task.get("trace_id") or "",
                source="maos.flows.common.patch_verifier",
                reason=("演示装配层的 before_review 钩子当场跑的沙箱回归："
                        "DAG 是 requirement->architecture->coding->testing，coding 过闸"
                        "那一刻 testing 还没派出去，报告不可能由 on_task_result 带回来"),
                # 这份是**真跑**出来的，mode 由沙箱回报（container / subprocess /
                # not-run），不是常量；scripted=False 与预置件那条路划清界限。
                extra={"sandbox_mode": report.get("sandbox_mode"), "scripted": False},
            )
            log.info("[%s] attempt=%d 沙箱回归：%s", task["task_id"], task["attempt"],
                     report["tool_error"] or report["summary"])
    return _verify


PLAN_JSON = json.dumps({"tasks": [{
    "role": "coding", "title": "修复 token 校验缺失",
    "inputs": {"repo": "demo/app", "issue": "#42"},
    "acceptance": ["build 通过", "lint 通过", "有变更说明"],
    "depends_on": [], "risk_level": "L",
}]}, ensure_ascii=False)
