"""MatrixEventBus 行为测试 —— C-6 的可执行版本。

这些断言守的是三件在演示当天最容易出事、且出事时症状离原因最远的东西：

1. **降级等价**。缺 env / 没装 matrix-nio / 房间连不上时，包了 Matrix 的总线必须
   和裸 InMemoryEventBus 表现得一模一样。"一样"不是形容词，是可断言的：同一串
   publish 序列（含会 nack 的 handler）必须得到同样的 drain 返回值、同样的
   handler 调用序列、同样的死信。CI 与测试永远跑在这条路径上，它一歪，
   四个场景全部跟着歪，而报错会指向状态机。
2. **旁路语义**。镜像抛异常不许影响 inner。用一个「必炸」的通道验，而不是靠读代码
   相信 try/except 写对了 —— 顺手把 `self.inner.publish` 挪到 try 里面就会破坏它，
   那种改动看起来完全无害。
3. **token 不进 repr**（铁律 6）。灌一个哨兵串进去反查 repr / str / %s 三种回显。
   这是**安全断言不是格式断言**：它变红意味着真 token 会随 evidence/ 入库。

真房间联通（Synapse / Element）属 Phase 4，这里一律不碰网络。
"""

from __future__ import annotations

import dataclasses
import inspect
import sys
import threading
import time

import pytest

import hiclaw.matrix_bus as matrix_bus
from hiclaw.matrix_bus import (ACTION_APPROVE, DEGRADE_CONNECT, DEGRADE_DEPS,
                               DEGRADE_ENV, DEGRADE_NONE, ENC_CLEAR, ENC_ENCRYPTED,
                               ENC_ERROR, ENV_APPROVERS, ENV_HOMESERVER, ENV_ROOM_ID,
                               ENV_TOKEN, ENV_USER, EVENT_APPROVAL_DENIED,
                               MAX_MIRROR_FAILURES, NO_MESSAGE, REQUIRED_ENV, USAGE,
                               VENV_PYTHON, ApprovalCommand, MatrixBusConfig,
                               MatrixDepMissing, MatrixEventBus, RoomApprovalBridge,
                               RoomSendTimeout, describe_exc, encryption_verdict,
                               looks_like_command, open_channel, parse_approval_command,
                               redact, render_mirror, should_deliver)
from maos.agents.testing import make_test_report, seed_scripted_report
from maos.contracts import events as E
from maos.contracts.events import Topic
from maos.contracts.states import PlanState, TaskState
from maos.core.control_plane import ENV_SANDBOX_WORKDIR
from maos.core.eventbus import EventBus, InMemoryEventBus
from maos.flows.common import GOOD_PATCH, build, run_until_settled
from maos.runtime.gate import HumanApprovalQueue

#: 只在本文件里出现的哨兵。别换成 "secret" 之类的常见词 —— 那种词可能因为别的原因
#: 出现在 repr 里，断言就失去了指向性。
SENTINEL_TOKEN = "sentinel-token-3f9c2a7e"

APPROVER = "@boss:example.org"
OUTSIDER = "@mallory:example.org"


def _live_config(**over) -> MatrixBusConfig:
    """一份「看起来能连上」的配置（log_only=False），配合注入的假通道用。"""
    base = dict(homeserver="https://matrix.example.org", user="@maos-bot:example.org",
                token=SENTINEL_TOKEN, room_id="!room:example.org",
                approvers=frozenset({APPROVER}))
    base.update(over)
    return MatrixBusConfig(**base)


class _RecordingChannel:
    """把镜像内容收进列表，不出网。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, plain: str, html: str) -> None:
        self.sent.append((plain, html))

    def close(self) -> None:
        pass


class _ExplodingChannel:
    """每次都炸的镜像通道 —— 用来验「镜像失败不得影响 inner」。

    记 calls 是为了防止断言变成空转：万一哪天 publish 干脆不调镜像了，
    inner 行为当然还是对的，但这条测试就再也守不住任何东西了。
    """

    def __init__(self) -> None:
        self.calls = 0

    def send(self, plain: str, html: str) -> None:
        self.calls += 1
        raise RuntimeError("房间炸了")

    def close(self) -> None:
        raise RuntimeError("关也炸")


# --------------------------------------------------------------------------
# 1. 降级等价
# --------------------------------------------------------------------------
def _drive(bus: EventBus) -> tuple[list[str], list[str], int]:
    """给一条总线喂同样的序列，返回 (正常 handler 收到的 key, 死信 key, drain 返回值)。

    刻意混进一个必抛的 handler：drain 的重投与死信是 InMemoryEventBus 语义里最容易
    被包装层改坏的部分，只 publish 一条成功事件是验不出来的。
    """
    ok_seen: list[str] = []
    bus.subscribe(Topic.TASK_RESULT, "ok", lambda env: ok_seen.append(env.idempotency_key))

    def _always_nack(env):
        raise RuntimeError("handler 故意失败")

    bus.subscribe(Topic.REWORK, "nack", _always_nack)

    for i in (1, 2, 3):
        bus.publish(Topic.TASK_RESULT, E.task_result(
            plan_id="p", task_id=f"t{i}", attempt=1, trace_id="tr", status="ok"))
    bus.publish(Topic.REWORK, E.rework(
        plan_id="p", task_id="t9", next_attempt=2, trace_id="tr",
        findings=[], reason="故意让它进死信"))

    processed = bus.drain()
    return ok_seen, [env.idempotency_key for env in bus.dead_letters], processed


def test_log_only_behaves_exactly_like_inner_bus():
    """降级模式下三方法行为与 inner bus 完全一致（C-6 的核心承诺）。"""
    bare = InMemoryEventBus()
    wrapped_inner = InMemoryEventBus()
    bus = MatrixEventBus(wrapped_inner, MatrixBusConfig.from_env({}))
    assert bus.config.log_only is True, "空环境必须降级，否则下面的比较没有意义"

    assert _drive(bus) == _drive(bare), "降级模式与裸 InMemoryEventBus 出现行为差异"


def test_log_only_forwards_inner_attributes():
    """dead_letters 之类的属性必须转发。

    少转发一个，就多一处**只在 --matrix 下才炸**的 AttributeError ——
    test_contracts.py:225 正是直接断言 bus.dead_letters 的。
    """
    inner = InMemoryEventBus()
    bus = MatrixEventBus(inner, MatrixBusConfig.from_env({}))
    assert bus.dead_letters is inner.dead_letters
    with pytest.raises(AttributeError):
        getattr(bus, "no_such_attribute_on_any_bus")


def test_three_methods_match_eventbus_signatures():
    """三方法签名与 EventBus 抽象逐字一致（C-6 明写）。

    签名漂了不会当场报错，只会在别人按 EventBus 的形状调用时才炸，
    所以钉在断言里。
    """
    for name in ("publish", "subscribe", "drain"):
        assert (inspect.signature(getattr(MatrixEventBus, name))
                == inspect.signature(getattr(EventBus, name))), f"{name} 签名与抽象不一致"


# --------------------------------------------------------------------------
# 2. from_env 降级
# --------------------------------------------------------------------------
def test_from_env_missing_all_degrades_without_raising(monkeypatch):
    for var in REQUIRED_ENV + (ENV_APPROVERS,):
        monkeypatch.delenv(var, raising=False)
    cfg = MatrixBusConfig.from_env()          # 不许抛
    assert cfg.log_only is True
    assert cfg.approvers == frozenset()


@pytest.mark.parametrize("missing", REQUIRED_ENV)
def test_from_env_missing_any_one_required_degrades(monkeypatch, missing):
    """四个必填项缺**任何一个**都降级 —— 缺一个就发不出消息，早降级比晚报错好。"""
    for var, val in ((ENV_HOMESERVER, "https://hs"), (ENV_USER, "@bot:hs"),
                     (ENV_TOKEN, SENTINEL_TOKEN), (ENV_ROOM_ID, "!r:hs")):
        monkeypatch.setenv(var, val)
    monkeypatch.delenv(missing, raising=False)
    assert MatrixBusConfig.from_env().log_only is True, f"缺 {missing} 没有降级"


def test_from_env_blank_value_counts_as_missing(monkeypatch):
    """env 设成空串 / 纯空白等同于没设 —— .env 里留一行 `MATRIX_TOKEN=` 是常态。"""
    for var, val in ((ENV_HOMESERVER, "https://hs"), (ENV_USER, "@bot:hs"),
                     (ENV_TOKEN, "   "), (ENV_ROOM_ID, "!r:hs")):
        monkeypatch.setenv(var, val)
    assert MatrixBusConfig.from_env().log_only is True


def test_from_env_complete_does_not_degrade(monkeypatch):
    for var, val in ((ENV_HOMESERVER, "https://hs"), (ENV_USER, "@bot:hs"),
                     (ENV_TOKEN, SENTINEL_TOKEN), (ENV_ROOM_ID, "!r:hs")):
        monkeypatch.setenv(var, val)
    monkeypatch.setenv(ENV_APPROVERS, f" {APPROVER} , ,{OUTSIDER} ")
    cfg = MatrixBusConfig.from_env()
    assert cfg.log_only is False
    assert cfg.approvers == frozenset({APPROVER, OUTSIDER}), "逗号分隔项没有去空白/去空项"
    assert cfg.token == SENTINEL_TOKEN


# --------------------------------------------------------------------------
# 3. token 不进 repr（安全断言）
# --------------------------------------------------------------------------
def test_token_never_appears_in_repr_or_str():
    """repr / str / %s 三种回显都不许出现 token 值。

    这条红了不是「格式不好看」：它意味着任何一句 log、任何一次异常栈、任何一份
    evidence/ 落盘都会把真 token 写进仓库，而 evidence/ 是要入库的。
    """
    cfg = _live_config()
    assert cfg.token == SENTINEL_TOKEN, "值本身必须还在，否则这条断言是空转"
    for shown in (repr(cfg), str(cfg), f"{cfg}", "bus config=%s" % (cfg,)):
        assert SENTINEL_TOKEN not in shown, f"token 泄漏进了：{shown}"


def test_replace_keeps_token_out_of_repr():
    """降级时走的是 dataclasses.replace，新对象同样不许把 token 带进 repr。"""
    bus = MatrixEventBus(InMemoryEventBus(), _live_config(), channel=_ExplodingChannel())
    for _ in range(MAX_MIRROR_FAILURES):
        bus.publish(Topic.TASK_RESULT, E.task_result(
            plan_id="p", task_id="t", attempt=1, trace_id="tr", status="ok"))
    assert bus.config.log_only is True, "连续镜像失败后应永久降级"
    assert SENTINEL_TOKEN not in repr(bus.config)
    assert bus.config.token == SENTINEL_TOKEN


# --------------------------------------------------------------------------
# 4. 镜像旁路
# --------------------------------------------------------------------------
def test_exploding_mirror_does_not_affect_inner():
    """镜像侧抛异常时 inner 的行为不受影响 —— 与裸总线逐项比对。"""
    channel = _ExplodingChannel()
    bare = InMemoryEventBus()
    bus = MatrixEventBus(InMemoryEventBus(), _live_config(), channel=channel)

    assert _drive(bus) == _drive(bare), "镜像炸了以后 inner 行为变了"
    assert channel.calls >= 1, "镜像通道压根没被调用过，这条测试在空转"


def test_mirror_content_is_summary_plus_folded_json():
    """镜像内容 = 一行人话摘要 + 折叠的 Envelope JSON。"""
    channel = _RecordingChannel()
    bus = MatrixEventBus(InMemoryEventBus(), _live_config(), channel=channel)
    env = E.review_verdict(plan_id="p", task_id="t7", attempt=2, trace_id="tr",
                           verdict="rework", findings=[])
    bus.publish(Topic.REVIEW_VERDICT, env)

    plain, html = channel.sent[-1]
    assert plain.startswith("[t7] ReviewVerdict"), plain
    assert "verdict=rework" in plain and "attempt=2" in plain
    assert "<details>" in html and "</details>" in html, "JSON 没有折叠，房间会被刷屏"
    assert env.event_id in html


def test_render_mirror_redacts_secret_looking_payload_keys():
    """镜像是**出网**动作，payload 里的疑似密钥必须在出口打码。"""
    env = E.task_result(plan_id="p", task_id="t", attempt=1, trace_id="tr", status="ok")
    env.payload["api_key"] = SENTINEL_TOKEN
    plain, html = render_mirror(Topic.TASK_RESULT, env)
    assert SENTINEL_TOKEN not in plain and SENTINEL_TOKEN not in html


def test_redact_does_not_touch_idempotency_key():
    """脱敏按键名匹配，别把 idempotency_key 这种正常字段一起打码。"""
    out = redact({"api_key": "x", "idempotency_key": "result:t1:1",
                  "nested": [{"token": "y"}, {"role": "coding"}]})
    assert out["api_key"] == "***"
    assert out["idempotency_key"] == "result:t1:1"
    assert out["nested"] == [{"token": "***"}, {"role": "coding"}]


def test_log_only_never_touches_channel():
    """降级模式下不许碰通道 —— 否则「行为与 inner 完全一致」就不成立了。"""
    channel = _RecordingChannel()
    bus = MatrixEventBus(InMemoryEventBus(), _live_config(log_only=True), channel=channel)
    bus.subscribe(Topic.TASK_RESULT, "g", lambda env: None)
    bus.publish(Topic.TASK_RESULT, E.task_result(
        plan_id="p", task_id="t", attempt=1, trace_id="tr", status="ok"))
    bus.drain()
    assert channel.sent == []


# --------------------------------------------------------------------------
# 5. 审批命令解析：合法 / 非法
# --------------------------------------------------------------------------
@pytest.mark.parametrize("text,expected", [
    ("/approve t1", ApprovalCommand("approve", "t1", "")),
    ("/reject t1", ApprovalCommand("reject", "t1", "")),
    ("/reject t1 补丁缺自检", ApprovalCommand("reject", "t1", "补丁缺自检")),
    ("/reject t1 缺 自检 报告", ApprovalCommand("reject", "t1", "缺 自检 报告")),
    ("  /APPROVE   t1  ", ApprovalCommand("approve", "t1", "")),
    ("/approve t1 已核对", ApprovalCommand("approve", "t1", "已核对")),
])
def test_parse_valid_commands(text, expected):
    assert parse_approval_command(text) == expected


@pytest.mark.parametrize("text", [
    "/aprove t1",          # 拼错命令
    "/approveall t1",      # 前缀像但不是
    "/approve",            # 缺 task_id
    "/reject",             # 缺 task_id
    "approve t1",          # 没有斜杠
    "",                    # 空
    "   ",
    "今天房间里聊点别的",
])
def test_parse_invalid_commands_return_none(text):
    """认不出一律 None，**不猜** —— 审批不可逆，猜错一个 task_id 就是放行了错的任务。"""
    assert parse_approval_command(text) is None


@pytest.mark.parametrize("text,expected", [
    ("/approve", True),            # 缺参数，但确实是冲着审批来的
    ("/reject t1 原因", True),
    ("/aprove t1", False),
    ("随便聊两句", False),
    ("", False),
])
def test_looks_like_command(text, expected):
    """认命令词与校参数是两个问题：合成一个，越权尝试会被降级成用法提示，证据就丢了。"""
    assert looks_like_command(text) is expected


# --------------------------------------------------------------------------
# 6. 房间审批：合法 / 非法 / 越权三类各有明确行为
# --------------------------------------------------------------------------
# 与 scenario_3.py 的 PASS_REPORT 同一份东西：高风险任务要走到 BLOCKED，
# 先得过得了验收闸，而 Task-C 起代码类任务的验收证据是一份 test_report ——
# 补丁自己的 self_check 全 pass 也不作数。Task-C 合并前这里不预置也能到 BLOCKED
# （旧判据回落 self_check），合并后不预置就一路 REWORK 到 FAILED，
# 十条房间审批用例会齐刷刷挂在「前置条件没成立」上，而真正的原因在验收闸。
BLOCKED_FIXTURE_REPORT = make_test_report(
    passed=1, failed=0, cases=[{"id": "tests/test_config.py::test_prod_cfg",
                                "status": "passed", "msg": ""}],
    summary="高风险变更回归：1 过 0 挂")


def _blocked_plan():
    """跑到「高风险任务停在 BLOCKED」那一刻，与场景 3 同一条路径。"""
    store, bus, cp, _model, _worker, gate = build({"任务输入": GOOD_PATCH})
    plan_id = cp.create_plan(goal="高风险变更需人工放行", trace_id=E.new_id("trace"), tasks=[{
        "role": "coding", "title": "变更生产环境配置",
        "inputs": {"repo": "demo/app"}, "acceptance": ["build 通过"],
        "risk_level": "M", "effect_risk": "H",
    }])
    # 预置验收证据，照抄 scenario_3.py 的写法（DAG 里没有 testing 节点，
    # 报告不可能由谁跑出来 —— 本用例要验的是房间审批，不是测试链路）。
    for task in cp.store.list_tasks(plan_id):
        if task["role"] == "coding":
            seed_scripted_report(store, plan_id=plan_id, task_id=task["task_id"],
                                 attempt=1, report=BLOCKED_FIXTURE_REPORT)
    cp.start_plan(plan_id)
    run_until_settled(bus, gate, cp, plan_id)
    hq = HumanApprovalQueue(store, cp)
    pending = hq.pending(plan_id)
    assert len(pending) == 1, "前置条件没成立：高风险任务应停在 BLOCKED"
    return store, bus, cp, hq, plan_id, pending[0]["task_id"]


def test_approver_can_approve_from_room():
    """合法：名单内 + 参数合法 -> 真的走到 DONE。"""
    store, bus, cp, hq, plan_id, task_id = _blocked_plan()
    bridge = RoomApprovalBridge(hq, _live_config())

    reply = bridge.handle_message(APPROVER, f"/approve {task_id} 已核对")
    bus.drain()

    assert "已批准" in reply and task_id in reply
    assert store.get_task(task_id)["state"] == TaskState.DONE
    assert cp.store.get_plan(plan_id)["state"] == PlanState.DONE


def test_approver_can_reject_from_room_with_reason(tmp_path, monkeypatch):
    """合法：/reject 带原因 -> FAILED，原因落进 event_log 的 detail。

    唯一一条会真走到补偿执行器的房间用例（驳回 -> 先回滚再落 FAILED），
    所以必须显式钉住 `MAOS_SANDBOX_WORKDIR`：不设会硬失败（缺省取仓库根已废止），
    而本条要验的是房间审批的三件事 —— 回执、终态、原因入 event_log，不是回滚本身。
    指向一个空临时目录即可：补丁打不上会如实记 ok=False，驳回照常推进；
    「打得上、还原得了」由 test_governance 的真实还原用例负责。
    """
    monkeypatch.setenv(ENV_SANDBOX_WORKDIR, str(tmp_path))
    store, bus, cp, hq, plan_id, task_id = _blocked_plan()
    bridge = RoomApprovalBridge(hq, _live_config())

    reply = bridge.handle_message(APPROVER, f"/reject {task_id} 配置未经二次核对")
    bus.drain()

    assert "已驳回" in reply and "配置未经二次核对" in reply
    assert store.get_task(task_id)["state"] == TaskState.FAILED
    assert cp.store.get_plan(plan_id)["state"] == PlanState.FAILED
    notes = [row["detail"].get("note") for row in store.list_event_log(plan_id)]
    assert "配置未经二次核对" in notes, "驳回原因没有进 event_log"


def test_malformed_command_from_approver_returns_usage_and_changes_nothing():
    """非法：名单内但参数不合法 -> 回用法，一个状态都不许动。"""
    store, _bus, _cp, hq, _plan_id, task_id = _blocked_plan()
    bridge = RoomApprovalBridge(hq, _live_config())

    assert bridge.handle_message(APPROVER, "/approve") == USAGE
    assert bridge.handle_message(APPROVER, "/reject") == USAGE
    assert store.get_task(task_id)["state"] == TaskState.BLOCKED


def test_non_command_chatter_gets_no_reply():
    """房间闲聊不该收到机器人的任何回应，包括用法提示。"""
    _store, _bus, _cp, hq, _plan_id, _task_id = _blocked_plan()
    bridge = RoomApprovalBridge(hq, _live_config())
    assert bridge.handle_message(APPROVER, "这个补丁我看过了") == ""
    assert bridge.handle_message(OUTSIDER, "/aprove 打错了") == ""


def test_outsider_is_denied_and_state_unchanged():
    """越权：名单外 -> 回「无审批权限」，任务状态一个字节都不许变。"""
    store, _bus, _cp, hq, _plan_id, task_id = _blocked_plan()
    bridge = RoomApprovalBridge(hq, _live_config())

    reply = bridge.handle_message(OUTSIDER, f"/approve {task_id}")

    assert "无审批权限" in reply and OUTSIDER in reply
    assert store.get_task(task_id)["state"] == TaskState.BLOCKED, "越权审批竟然生效了"


def test_outsider_denial_is_recorded_in_event_log():
    """越权必须**记一条 event_log** —— 「系统拒绝了一次越权审批」本身就是审计证据。"""
    store, _bus, _cp, hq, plan_id, task_id = _blocked_plan()
    bridge = RoomApprovalBridge(hq, _live_config())

    bridge.handle_message(OUTSIDER, f"/approve {task_id}")

    denied = [row for row in store.list_event_log(plan_id)
              if row["event_type"] == EVENT_APPROVAL_DENIED]
    assert len(denied) == 1, "越权尝试没有留下 event_log"
    row = denied[0]
    assert row["task_id"] == task_id
    assert row["detail"]["sender"] == OUTSIDER
    assert row["detail"]["command"] == ACTION_APPROVE


def test_outsider_denied_even_when_command_is_malformed():
    """名单外 + 参数也不合法 -> 仍按越权处理并留证。

    判定顺序是先查名单再校参数：反过来会把这次尝试降级成一句用法提示，
    event_log 里什么都不剩。task_id 未知时挂不到具体 plan 上，于是落在 plan_id=""
    这一档 —— **记下来比归类整齐重要**。
    """
    store, _bus, _cp, hq, plan_id, _task_id = _blocked_plan()
    bridge = RoomApprovalBridge(hq, _live_config())

    reply = bridge.handle_message(OUTSIDER, "/approve")

    assert "无审批权限" in reply
    assert [row for row in store.list_event_log(plan_id)
            if row["event_type"] == EVENT_APPROVAL_DENIED] == [], "task_id 未知却挂到了某个 plan 上"
    orphan = [row for row in store.list_event_log("")
              if row["event_type"] == EVENT_APPROVAL_DENIED]
    assert len(orphan) == 1, "越权尝试没有留证"
    assert orphan[0]["detail"]["sender"] == OUTSIDER


def test_unknown_task_denial_still_replies():
    """task_id 查不到时不许抛 —— 房间里一条打错的命令不该掀掉监听循环。"""
    _store, _bus, _cp, hq, _plan_id, _task_id = _blocked_plan()
    bridge = RoomApprovalBridge(hq, _live_config())
    assert "无审批权限" in bridge.handle_message(OUTSIDER, "/approve 根本不存在的任务")


def test_approver_hitting_unknown_task_gets_error_not_crash():
    """名单内但 task_id 打错 -> 回一句「未生效」，不抛。

    静默吞掉同样不行：发命令的人必须当场知道这条没生效，否则会一直等一个不会来的结果。
    """
    _store, _bus, _cp, hq, _plan_id, _task_id = _blocked_plan()
    bridge = RoomApprovalBridge(hq, _live_config())
    reply = bridge.handle_message(APPROVER, "/approve task_打错了")
    assert "审批未生效" in reply


def test_bridge_reply_survives_exploding_channel():
    """回话也是旁路：房间回不了话，判定照样生效。"""
    store, bus, _cp, hq, _plan_id, task_id = _blocked_plan()
    bridge = RoomApprovalBridge(hq, _live_config(), channel=_ExplodingChannel())

    bridge.handle_message(APPROVER, f"/approve {task_id}")
    bus.drain()

    assert store.get_task(task_id)["state"] == TaskState.DONE


# ==========================================================================
# 7. 真房间路径（task-C2）——「判错了只会静默降级」的那几处
# ==========================================================================
# 下面全部不连网络：响应对象的形态是照 matrix-nio 0.26.0 的**实测输出**抄的
# （见 docs/DECISIONS.md 的 ## task-C2），fake 客户端经 sys.modules 注入，
# 所以这些用例在没装 matrix-nio 的解释器上（本机 python3、CI）一样跑得完。


class _FakeStateError:
    """照抄 RoomGetStateEventError 的字段形态：有 status_code / message，**没有** content。

    nio 把响应体里的 ``errcode`` 原样放进 ``status_code``（是字符串，不是 HTTP 数字），
    且只有 HTTP 404 才会走到这个类 —— 这两点是本节所有断言的前提。
    """

    def __init__(self, status_code: str, message: str = "") -> None:
        self.status_code = status_code
        self.message = message
        self.room_id = "!room:example.org"


class _FakeStateResponse:
    """照抄 RoomGetStateEventResponse：有 content，**没有** status_code。"""

    def __init__(self, content) -> None:
        self.content = content
        self.event_type = "m.room.encryption"
        self.state_key = ""
        self.room_id = "!room:example.org"


def test_encryption_verdict_unencrypted_room_is_clear():
    """未加密房：Synapse 回 404 M_NOT_FOUND -> RoomGetStateEventError -> clear。"""
    verdict, detail = encryption_verdict(_FakeStateError("M_NOT_FOUND", "Event not found."))
    assert verdict == ENC_CLEAR
    assert detail == "M_NOT_FOUND"


def test_encryption_verdict_encrypted_room_is_encrypted():
    """加密房：200 + content 里有 algorithm -> encrypted。

    只验未加密那一侧等于没验：判据写反时两侧都会「通过」，
    然后演示当天 send 全静默失败。
    """
    resp = _FakeStateResponse({"algorithm": "m.megolm.v1.aes-sha2"})
    verdict, detail = encryption_verdict(resp)
    assert verdict == ENC_ENCRYPTED
    assert "megolm" in detail


@pytest.mark.parametrize("errcode,error", [
    ("M_FORBIDDEN", "User @maos-bot:maos.local not in room"),   # 机器人没进房间
    ("M_UNKNOWN_TOKEN", "Invalid access token"),                # token 失效
])
def test_non_404_error_body_is_not_mistaken_for_encryption(errcode, error):
    """**本轨的核心回归**：非 404 的错误体不许被念成「房间已加密」。

    matrix-nio 0.26.0 的 ``create_matrix_response`` 只在 HTTP 404 时把响应体转成
    ``RoomGetStateEventError``；403 / 401 落到 ``else`` 走
    ``RoomGetStateEventResponse.from_dict``，而它是一句 ``return cls(parsed_dict, ...)``
    —— 错误体被原样包成「成功」响应。旧判据「不是 Error 就是已加密」于是会把
    「机器人没进房间」「token 过期」一起报成加密房，降级日志里留一个假原因，
    而真原因（没邀请 / token 该换了）没人会去查。
    """
    verdict, detail = encryption_verdict(_FakeStateResponse({"errcode": errcode,
                                                             "error": error}))
    assert verdict == ENC_ERROR, "非 404 的错误体被当成了加密房"
    assert errcode in detail and error in detail


def test_encryption_verdict_other_state_error_is_error():
    """404 但 errcode 不是 M_NOT_FOUND -> 当错误处理，不当未加密。"""
    verdict, detail = encryption_verdict(_FakeStateError("M_LIMIT_EXCEEDED", "slow down"))
    assert verdict == ENC_ERROR
    assert "M_LIMIT_EXCEEDED" in detail


# -- 回声过滤 --------------------------------------------------------------
class _Room:
    def __init__(self, room_id: str) -> None:
        self.room_id = room_id


class _Msg:
    def __init__(self, sender: str, body: str) -> None:
        self.sender = sender
        self.body = body


BOT_MXID = "@maos-bot:example.org"


def test_should_deliver_drops_own_echo():
    """sender == 自己 的消息不进 on_message。"""
    room = _Room("!room:example.org")
    assert should_deliver("!room:example.org", BOT_MXID, room,
                          _Msg(BOT_MXID, "[t1] TaskResult")) is False
    assert should_deliver("!room:example.org", BOT_MXID, room,
                          _Msg(APPROVER, "/approve t1")) is True


def test_should_deliver_drops_other_rooms():
    """sync 是全量的，别的房间的消息不许进来。"""
    assert should_deliver("!room:example.org", BOT_MXID, _Room("!other:example.org"),
                          _Msg(APPROVER, "/approve t1")) is False


def test_echo_filter_needs_authoritative_mxid_not_env_localpart():
    """回声过滤必须比服务器给的权威 mxid，不能比 MATRIX_USER 原文。

    实测：``AsyncClient(hs, user)`` 只把 user 原样存进 ``.user``，``.user_id``
    在 whoami / login 之前恒为空串。MATRIX_USER 写成 localpart 时，拿它去比
    ``event.sender`` 永远不相等 —— bot 会听见自己发的每一条消息。
    这条钉的就是「_NioChannel 必须先 whoami 拿到 user_id」。
    """
    own = _Msg(BOT_MXID, "[t1] TaskResult")
    room = _Room("!room:example.org")
    assert should_deliver("!room:example.org", "maos-bot", room, own) is True, \
        "localpart 比不上 —— 这正是必须用 whoami 回填 user_id 的理由"
    assert should_deliver("!room:example.org", BOT_MXID, room, own) is False


# -- 整条 _NioChannel 路径（注入假 nio 模块）--------------------------------
class _FakeWhoamiError(Exception):
    """独立类型，供 _verify_identity 的 isinstance 判定用。"""

    def __init__(self, status_code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class _FakeWhoamiResponse:
    def __init__(self, user_id: str) -> None:
        self.user_id = user_id
        self.device_id = "DEVICE"


class _FakeSendError(Exception):
    def __init__(self, message: str = "send failed") -> None:
        super().__init__(message)
        self.message = message


class _FakeSyncError(Exception):
    def __init__(self, status_code: str = "M_UNKNOWN", message: str = "sync failed") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class _FakeSyncResponse:
    def __init__(self, next_batch: str) -> None:
        self.next_batch = next_batch


class _FakeRoomMessageText:
    """占位：只用来当 add_event_callback 的过滤类型。"""


class _FakeRoomMessageMedia:
    """照 nio 的 RoomMessageImage / RoomMessageFile 抄字段：sender / body / url / source。"""

    def __init__(self, sender: str, body: str, url: str = "", *, mimetype: str = "",
                 size: int = 0) -> None:
        self.sender = sender
        self.body = body
        self.url = url
        self.source = {"content": {"info": {"mimetype": mimetype, "size": size}}}


class _FakeRoomMessageImage(_FakeRoomMessageMedia):
    pass


class _FakeRoomMessageFile(_FakeRoomMessageMedia):
    pass


class _FakeDownloadResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body


_TYPED_EVENTS = (_FakeRoomMessageText, _FakeRoomMessageImage, _FakeRoomMessageFile)


class _FakeAsyncClient:
    """够 ``_NioChannel`` 跑完整条路径的假客户端，逐笔记录调用顺序。

    ``sync`` 会把 :attr:`history` 派发给**当时已注册**的回调 —— 真 nio 就是这么干的
    （实测：喂一份带历史 timeline 的 SyncResponse 进去，回调当场触发）。
    所以「先 sync 后挂回调」这个顺序一旦被改回去，历史就会漏进 on_message，
    下面那条用例立刻变红。
    """

    instances: list["_FakeAsyncClient"] = []

    # 由用例改写的服务器行为
    whoami_result: object = None
    state_result: object = None
    #: room_send 拖多久才落地。用来复现「限流退避把 send 拖过超时」那一幕。
    send_delay: float = 0.0
    #: sync_forever 是否常驻不返回（真 nio 的形态）。缺省立刻返回够别的用例用，
    #: 但验收口那条必须有个会一直挂着的版本 —— 否则 close() 里 cancel 那步等于没测。
    hang_sync: bool = False

    def __init__(self, homeserver: str, user: str) -> None:
        self.homeserver = homeserver
        self.user = user
        self.user_id = ""              # 与真 nio 一致：whoami 之前是空串
        self.access_token = ""
        self.next_batch = ""
        self.calls: list[str] = []
        self.sent: list[dict] = []
        self.callbacks: list = []
        self.history: list = []        # 首次 sync 会吐出来的「历史」
        self.live: list = []           # sync_forever 期间到达的新消息
        self.closed = False
        self.sync_stopped = False
        self.sync_done = threading.Event()
        self.sync_forever_since = None
        type(self).instances.append(self)

    # -- 服务器动作 --
    async def whoami(self):
        self.calls.append("whoami")
        return type(self).whoami_result

    async def room_get_state_event(self, room_id, event_type, state_key=""):
        self.calls.append(f"state:{event_type}")
        return type(self).state_result

    async def room_send(self, room_id, message_type, content, **kw):
        self.calls.append("room_send")
        if type(self).send_delay:
            import asyncio

            await asyncio.sleep(type(self).send_delay)
        # 落地在 sleep **之后**：这正是「调用方已经放弃、消息照样送达」那一幕。
        self.sent.append(content)
        return object()

    async def sync(self, timeout=None, sync_filter=None, since=None, *a, **kw):
        self.calls.append(f"sync(filter={sync_filter!r})")
        await self._dispatch(self.history)
        self.next_batch = "s72_1_2_3"
        return _FakeSyncResponse(self.next_batch)

    async def sync_forever(self, timeout=None, **kw):
        self.calls.append("sync_forever")
        self.sync_forever_since = kw.get("since")
        await self._dispatch(self.live)
        self.sync_done.set()           # 用例靠它等一轮派发跑完，不靠 sleep 猜
        if type(self).hang_sync:
            import asyncio

            await asyncio.Event().wait()

    async def _dispatch(self, events):
        for room, event in events:
            for cb, cls in list(self.callbacks):
                # 真 nio 按注册时的事件类型分发。老用例的 `_Msg` 不属于任何类型，
                # 仍派给所有回调（那些用例只挂了文本回调，形态不变）。
                if isinstance(event, _TYPED_EVENTS) and not isinstance(event, cls):
                    continue
                await cb(room, event)

    async def download(self, mxc=None, **kw):
        self.calls.append(f"download({mxc})")
        return _FakeDownloadResponse(b"%PDF-fake-bytes-for-" + str(mxc).encode())

    def add_event_callback(self, cb, event_class):
        self.calls.append("add_event_callback")
        self.callbacks.append((cb, event_class))

    def stop_sync_forever(self):
        self.sync_stopped = True

    async def close(self):
        self.closed = True


@pytest.fixture
def fake_nio(monkeypatch):
    """把假 nio 塞进 sys.modules。

    ``_NioChannel`` 的 import 全是**方法内惰性 import**，所以换掉 sys.modules 就够 ——
    这也是为什么这几条用例在装了真 matrix-nio 的 venv 里同样走假实现，不出网。
    """
    import sys
    import types

    module = types.ModuleType("nio")
    module.AsyncClient = _FakeAsyncClient
    module.WhoamiError = _FakeWhoamiError
    module.RoomSendError = _FakeSendError
    module.SyncError = _FakeSyncError
    module.RoomMessageText = _FakeRoomMessageText
    module.RoomMessageImage = _FakeRoomMessageImage
    module.RoomMessageFile = _FakeRoomMessageFile
    module.RoomGetStateEventError = _FakeStateError
    monkeypatch.setitem(sys.modules, "nio", module)

    _FakeAsyncClient.instances = []
    _FakeAsyncClient.whoami_result = _FakeWhoamiResponse(BOT_MXID)
    _FakeAsyncClient.state_result = _FakeStateError("M_NOT_FOUND", "Event not found.")
    _FakeAsyncClient.send_delay = 0.0
    _FakeAsyncClient.hang_sync = False
    yield _FakeAsyncClient
    _FakeAsyncClient.instances = []
    _FakeAsyncClient.send_delay = 0.0
    _FakeAsyncClient.hang_sync = False


def test_channel_asks_whoami_before_touching_the_room(fake_nio):
    """开工顺序：先 whoami 验 token 拿权威 mxid，再查加密。

    反过来的代价不是崩：token 失效时 ``room_get_state_event`` 会回一个 401 错误体，
    而那个错误体会被 nio 包成「成功」响应 —— 于是报「房间已加密」，假原因。
    """
    channel = open_channel(_live_config())
    client = fake_nio.instances[-1]

    assert client.calls[0] == "whoami"
    assert client.calls[1] == "state:m.room.encryption"
    assert client.access_token == SENTINEL_TOKEN, "token 没被装进客户端"
    channel.close()


def test_channel_uses_server_mxid_for_echo_filter_even_if_env_has_localpart(fake_nio):
    """MATRIX_USER 写成 localpart 时，回声过滤仍按 whoami 回来的权威 mxid 走。"""
    channel = open_channel(_live_config(user="maos-bot"))
    client = fake_nio.instances[-1]
    assert client.user == "maos-bot", "传给 AsyncClient 的仍是 env 原文"

    seen: list[tuple[str, str]] = []
    client.live = [(_Room("!room:example.org"), _Msg(BOT_MXID, "自己发的回执")),
                   (_Room("!room:example.org"), _Msg(APPROVER, "/approve t1"))]
    channel.listen(lambda sender, body: seen.append((sender, body)))
    assert client.sync_done.wait(5), "sync_forever 没跑完"

    assert seen == [(APPROVER, "/approve t1")], f"回声过滤没拦住自己的消息：{seen}"
    channel.close()


def test_first_sync_history_is_not_replayed_as_new_commands(fake_nio):
    """首次 sync 灌回来的历史**不许**进 on_message。

    实测（venv + 真 matrix-nio，喂一份带历史 timeline 的 SyncResponse）：不带 since
    的首次 /sync 会把房间 timeline 的最近若干条历史一并返回，nio 照样派发给
    ``add_event_callback``。房间里半小时前有人打过一句 ``/approve task-x``，
    bot 一起来就会当成新指令重放 —— 而审批不可逆，重放的后果是放行一个没人再看的任务。

    钉的是顺序：``_skip_history()`` 先把 next_batch 推到「现在」，之后才 add_event_callback。
    把这两行换个位置，本条立刻红。
    """
    channel = open_channel(_live_config())
    client = fake_nio.instances[-1]
    client.history = [(_Room("!room:example.org"),
                       _Msg(APPROVER, "/approve task-from-half-an-hour-ago"))]

    seen: list[tuple[str, str]] = []
    channel.listen(lambda sender, body: seen.append((sender, body)))
    assert client.sync_done.wait(5), "sync_forever 没跑完"

    assert seen == [], f"首次 sync 的历史被当成了新指令：{seen}"
    order = [c for c in client.calls if c.startswith("sync(") or c == "add_event_callback"]
    assert order[0].startswith("sync("), f"挂回调挂在了跳历史之前：{client.calls}"
    assert order[1] == "add_event_callback"
    assert client.next_batch, "跳历史那次 sync 没把 next_batch 推起来"
    assert client.sync_forever_since == client.next_batch, (
        "常驻同步没有显式从已证明的历史边界续跑")
    channel.close()


def test_first_sync_uses_zero_timeline_filter(fake_nio):
    """跳历史那次 sync 必须带「timeline 一条都不要」的过滤器。

    只靠「先 sync 后挂回调」也能挡住历史，但那次 sync 会把整个房间的近期消息
    拉回本地；过滤器让它连拉都不拉。两道一起上，是因为漏掉任何一道的症状
    都是「看起来正常，偶尔重放一条」。
    """
    channel = open_channel(_live_config())
    client = fake_nio.instances[-1]
    channel.listen(lambda sender, body: None)

    first_sync = next(c for c in client.calls if c.startswith("sync("))
    assert "'limit': 0" in first_sync, first_sync
    channel.close()


@pytest.mark.parametrize("failure", [
    "sync-error", "missing-next-batch", "empty-next-batch",
])
def test_listener_fails_closed_when_history_boundary_is_not_proven(fake_nio, failure):
    """首次 /sync 没拿到有效游标时，不得挂回调猜测历史边界。"""
    channel = open_channel(_live_config())
    client = fake_nio.instances[-1]
    client.next_batch = "stale-client-cursor-must-not-count"

    async def broken_sync(*args, **kwargs):
        client.calls.append("broken_sync")
        if failure == "sync-error":
            return _FakeSyncError("M_LIMIT_EXCEEDED", "slow down")
        if failure == "empty-next-batch":
            return _FakeSyncResponse("")
        return object()

    client.sync = broken_sync
    with pytest.raises(RuntimeError, match="历史|next_batch|sync"):
        channel.listen(lambda sender, body: None)

    assert client.callbacks == [], "边界未证明就挂回调，会把历史命令当真人新命令"
    assert channel._sync_task is None
    assert "sync_forever" not in client.calls
    channel.close()


def test_encrypted_room_degrades_bus_to_log_only(fake_nio):
    """撞加密房 -> 通道构造失败 -> 总线降级 log-only，且不抛。"""
    fake_nio.state_result = _FakeStateResponse({"algorithm": "m.megolm.v1.aes-sha2"})
    inner = InMemoryEventBus()
    bus = MatrixEventBus(inner, _live_config())        # 不注入 channel，走 open_channel

    assert bus.config.log_only is True
    assert bus.channel is None
    assert _drive(bus) == _drive(InMemoryEventBus()), "降级后行为与裸总线不一致"


def test_bad_token_degrades_with_the_real_reason(fake_nio, caplog):
    """token 失效要报 token 失效，不许报成「房间已加密」。"""
    fake_nio.whoami_result = _FakeWhoamiError("M_UNKNOWN_TOKEN", "Invalid access token")
    with caplog.at_level("WARNING", logger="maos.matrix"):
        bus = MatrixEventBus(InMemoryEventBus(), _live_config())

    assert bus.config.log_only is True and bus.channel is None
    text = caplog.text
    assert "M_UNKNOWN_TOKEN" in text, f"降级日志没说真原因：{text}"
    assert "加密" not in text, f"token 失效被报成了加密房：{text}"
    assert SENTINEL_TOKEN not in text, "token 值泄漏进了日志"


def test_failed_construction_does_not_leak_the_event_loop(fake_nio):
    """降级是常态路径，每失败一次不许漏一条守护线程。"""
    before = {t.ident for t in threading.enumerate()}
    fake_nio.state_result = _FakeStateResponse({"algorithm": "m.megolm.v1.aes-sha2"})
    for _ in range(3):
        MatrixEventBus(InMemoryEventBus(), _live_config())

    leaked: list = []
    deadline = time.time() + 5
    while time.time() < deadline:
        leaked = [t for t in threading.enumerate()
                  if t.name == "matrix-bus" and t.ident not in before and t.is_alive()]
        if not leaked:
            break
        time.sleep(0.05)
    assert not leaked, f"构造失败漏了 {len(leaked)} 条 matrix-bus 线程"


def test_live_channel_sends_notice_with_html(fake_nio):
    """接通时 publish 真的把摘要 + 折叠 JSON 发进房间，且用 m.notice。"""
    bus = MatrixEventBus(InMemoryEventBus(), _live_config())
    assert bus.config.log_only is False and bus.channel is not None

    bus.publish(Topic.TASK_RESULT, E.task_result(
        plan_id="p", task_id="t1", attempt=1, trace_id="tr", status="ok"))

    client = fake_nio.instances[-1]
    assert len(client.sent) == 1
    content = client.sent[0]
    assert content["msgtype"] == "m.notice", "机器人消息不该触发人类推送"
    assert content["body"].startswith("[t1] TaskResult")
    assert "<details>" in content["formatted_body"]
    bus.close()


def test_token_field_is_declared_repr_false(fake_nio):
    """结构守卫：``MatrixBusConfig.token`` 的 ``repr=False`` 被抹掉时立刻红。

    已有的 test_token_never_appears_in_repr_or_str 验的是**行为**；这条验的是
    **声明**。两条都要：行为那条在有人给 dataclass 加自定义 __repr__ 时仍会绿，
    而那种 __repr__ 一改就漏。
    """
    fields = {f.name: f for f in dataclasses.fields(MatrixBusConfig)}
    assert fields["token"].repr is False, "token 字段的 repr=False 被去掉了（安全边界）"


# ==========================================================================
# 8. 四种「安静地什么都没发生」—— 每一种都必须出声
# ==========================================================================
# 这一节守的是 docs/matrix-room-runbook.md §0 那张表里的四行。它们的共同点是：
# 屏幕上一切正常、退出码是 0、房间里一条消息都没有。真房间取证时逐条撞出来的，
# 每一条都曾让一整轮跑完之后才发现白跑（T 轮，见 docs/BACKLOG.md ## task-T4）。
#
# 判据一律**不是**「日志里有话」，而是「调用方能据此做出不同的动作」：
# 退出码不同、失败计数不同、线程/循环收干净了。日志措辞另有几条单独钉，
# 因为那几条恰恰是措辞本身出的问题（空括号）。


class _BlockNio:
    """让 ``import nio`` 抛真正的 ModuleNotFoundError —— 系统 python3 上的形态。

    不用 ``sys.modules["nio"] = None``：那条路抛的是 ``ImportError`` 且 ``.name``
    是空的，而 :func:`open_channel` 的判据正是 ``exc.name == "nio"``，
    用假形态测等于没测到那一支。
    """

    def find_spec(self, name, path=None, target=None):
        if name == "nio" or name.startswith("nio."):
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return None


@pytest.fixture
def no_nio(monkeypatch):
    """把当前解释器伪装成「没装 matrix-nio」的那一个。"""
    monkeypatch.delitem(sys.modules, "nio", raising=False)
    monkeypatch.setattr(sys, "meta_path", [_BlockNio(), *sys.meta_path])
    yield


class _TimingOutChannel:
    """每次 send 都超时的通道。形状对齐 MirrorChannel。"""

    def __init__(self) -> None:
        self.attempts = 0

    def send(self, plain: str, html: str) -> None:
        self.attempts += 1
        raise RoomSendTimeout("30s 内没等到房间回执；协程仍在后台重试")

    def listen(self, on_message) -> None:               # noqa: ANN001
        pass

    def close(self) -> None:
        pass


# -- 8.1 空括号：告警必须说得出自己是什么 ----------------------------------
def test_empty_message_exception_still_says_what_it_was():
    """``TimeoutError()`` 的 str() 是空串 —— 告警不许因此变成一对空括号。

    T 轮真房间那一轮打了 3 条 ``房间回话失败（）``，括号里什么都没有。看到它的人
    既不知道是什么错、也不知道该不该重跑，而正确答案恰恰是「别重跑」。
    """
    assert str(TimeoutError()) == "", "前提变了：这条用例守的就是空 str() 那个坑"

    line = describe_exc(TimeoutError())
    assert "TimeoutError" in line, f"没说出异常类型：{line}"
    assert NO_MESSAGE in line, f"空消息没有兜底占位：{line}"
    assert f"（{line}）" != "（）"


def test_timeout_description_tells_the_reader_not_to_rerun():
    """超时类的描述必须带处置口径。告警只说「失败了」等于没说。"""
    line = describe_exc(RoomSendTimeout("30s 内没等到房间回执"))
    assert "不要重跑" in line, f"没写处置口径：{line}"
    assert "限流" in line and "数消息" in line, line


def test_non_timeout_exception_keeps_its_own_message():
    """普通异常照原文带上，只在前面补类型。别把原始信息挤掉。"""
    line = describe_exc(RuntimeError("M_UNKNOWN_TOKEN Invalid access token"))
    assert line.startswith("RuntimeError: ")
    assert "M_UNKNOWN_TOKEN Invalid access token" in line
    assert "不要重跑" not in line, "非超时不该带超时的处置口径"


# -- 8.2 坑一：解释器用错了，和房间连不上不是一回事 -----------------------
def test_missing_matrix_nio_names_the_interpreter_you_actually_used(no_nio):
    """没装 nio 时抛 MatrixDepMissing，且消息里有**当前**解释器和该换的那个。

    这是整条链路最贵的一步：系统 python3 跑完 exit=0、终端照常刷「房间消息」，
    截那个窗口当证据与真房间**无法分辨**。能自动认出它的地方只有这里。
    """
    with pytest.raises(MatrixDepMissing) as caught:
        open_channel(_live_config())

    text = str(caught.value)
    assert sys.executable in text, f"没说清是哪个解释器跑的：{text}"
    assert VENV_PYTHON in text, f"没给出能直接粘的那一行：{text}"
    assert isinstance(caught.value.__cause__, ModuleNotFoundError)


def test_a_different_missing_module_is_not_blamed_on_nio(monkeypatch):
    """nio 装了但它自己缺依赖时，不许念成「你没装 nio」—— 那会把人指向错方向。"""
    # `**kw` 是给 open_channel 的 `ignored_senders=` 留的（T84）：假件的签名要跟着
    # 被测函数走，否则这条用例断的就不再是「怪谁」，而是「假件参数对不上」。断言不动。
    def _boom(config, **kw):
        raise ModuleNotFoundError("No module named 'h11'", name="h11")

    monkeypatch.setattr(matrix_bus, "_NioChannel", _boom)
    with pytest.raises(ModuleNotFoundError) as caught:
        open_channel(_live_config())
    assert not isinstance(caught.value, MatrixDepMissing)
    assert caught.value.name == "h11"


@pytest.mark.parametrize("scenario, expected", [
    ("deps", DEGRADE_DEPS),
    ("connect", DEGRADE_CONNECT),
    ("env", DEGRADE_ENV),
])
def test_degrade_reason_separates_never_meant_to_from_meant_to_but_failed(
        request, scenario, expected):
    """三种降级必须给出**三种**原因 —— 入口靠它决定退出码。

    ``log_only`` 这一个布尔把它们抹平成同一个 True：「四个 env 一个都没配」（自检
    常态，退 0）和「配齐了却没进成房间」（事故，必须非 0）在它上面长得一模一样。
    没有这个字段，入口就只能对两者做同一件事 —— 而那正是坑一的成因。
    """
    if scenario == "deps":
        request.getfixturevalue("no_nio")
        bus = MatrixEventBus(InMemoryEventBus(), _live_config())
    elif scenario == "connect":
        fake = request.getfixturevalue("fake_nio")
        fake.state_result = _FakeStateResponse({"algorithm": "m.megolm.v1.aes-sha2"})
        bus = MatrixEventBus(InMemoryEventBus(), _live_config())
    else:
        bus = MatrixEventBus(InMemoryEventBus(), MatrixBusConfig.from_env({}))

    assert bus.degrade_reason == expected
    assert bus.config.log_only is True and bus.channel is None
    assert _drive(bus) == _drive(InMemoryEventBus()), "降级后行为与裸总线不一致"


def test_connected_bus_reports_no_degrade_reason(fake_nio):
    """反向对照：真接通时原因必须是空的。少了这一半，上面那条在「永远返回 deps」
    的实现下也成立。"""
    bus = MatrixEventBus(InMemoryEventBus(), _live_config())
    assert bus.degrade_reason == DEGRADE_NONE and bus.degrade_detail == ""
    assert bus.channel is not None
    bus.close()


def test_missing_nio_is_loud_and_does_not_leak_the_token(no_nio, caplog):
    """这一条要在屏幕上站得住（ERROR），且不许把 token 带出去。"""
    with caplog.at_level("WARNING", logger="maos.matrix"):
        MatrixEventBus(InMemoryEventBus(), _live_config())

    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert errors, f"没装 nio 只留了 WARNING，会被日志淹掉：{caplog.text}"
    assert "房间里一条都不会有" in caplog.text, caplog.text
    assert SENTINEL_TOKEN not in caplog.text, "token 值泄漏进了日志"


# -- 8.3 坑二：超时是虚警，不许被做实成永久降级 ---------------------------
def test_send_timeout_never_triggers_the_permanent_degrade():
    """连续超时 **不计入** MAX_MIRROR_FAILURES —— 否则一次限流就把镜像关掉。

    撞限流时 nio 还在后台退避重试，消息很可能已经落地（T 轮实测：3 条超时告警，
    房间里 23 条消息一条不少）。把它计进失败次数，撞一次限流就够 3 次、直接永久
    降级 —— 那之后房间里是真的一条都没有了，一次虚警被自己亲手做实成真故障。
    """
    channel = _TimingOutChannel()
    bus = MatrixEventBus(InMemoryEventBus(), _live_config(), channel=channel)

    rounds = MAX_MIRROR_FAILURES + 2
    for i in range(rounds):
        bus.publish(Topic.TASK_RESULT, E.task_result(
            plan_id="p", task_id=f"t{i}", attempt=1, trace_id="tr", status="ok"))

    assert channel.attempts == rounds, "超时之后就不再尝试了，等于已经降级"
    assert bus.channel is channel, "超时把通道摘掉了 —— 虚警做实成了真故障"
    assert bus.config.log_only is False


def test_a_real_send_failure_still_degrades_permanently():
    """反向对照：真失败照旧走永久降级。少了这一半，上面那条在「永不降级」下也绿。"""
    bus = MatrixEventBus(InMemoryEventBus(), _live_config(),
                         channel=_ExplodingChannel())
    for i in range(MAX_MIRROR_FAILURES):
        bus.publish(Topic.TASK_RESULT, E.task_result(
            plan_id="p", task_id=f"t{i}", attempt=1, trace_id="tr", status="ok"))

    assert bus.channel is None and bus.config.log_only is True


def test_mirror_timeout_warning_is_not_an_empty_pair_of_parens(caplog):
    """镜像超时那条告警要说得出自己是什么，并且说清「未计入失败次数」。"""
    bus = MatrixEventBus(InMemoryEventBus(), _live_config(),
                         channel=_TimingOutChannel())
    with caplog.at_level("WARNING", logger="maos.matrix"):
        bus.publish(Topic.TASK_RESULT, E.task_result(
            plan_id="p", task_id="t1", attempt=1, trace_id="tr", status="ok"))

    assert "（）" not in caplog.text, f"又打成空括号了：{caplog.text}"
    assert "RoomSendTimeout" in caplog.text
    assert "未计入失败次数" in caplog.text


def test_message_still_lands_after_the_caller_gave_up_waiting(fake_nio, monkeypatch):
    """超时只代表「我不等了」，协程照旧把消息送达 —— 这就是虚警的成因。

    钉住它是为了守 ``_await`` 里那个**不 cancel** 的决定：顺手加一句
    ``future.cancel()`` 看起来是收尾更干净，实际是把一条本来会送达的消息掐掉，
    而症状是「房间里少了几条，没人知道少在哪」。
    """
    monkeypatch.setattr(matrix_bus, "DEFAULT_SEND_TIMEOUT", 0.15)
    fake_nio.send_delay = 0.6
    channel = open_channel(_live_config())
    client = fake_nio.instances[-1]

    with pytest.raises(RoomSendTimeout):
        channel.send("摘要行", "<p>摘要行</p>")
    assert client.sent == [], "还没到点就落地了，这条用例没测到超时"

    deadline = time.time() + 5
    while time.time() < deadline and not client.sent:
        time.sleep(0.05)
    assert len(client.sent) == 1, "调用方放弃后协程被掐掉了 —— 消息真丢了"
    channel.close()


def test_timed_out_send_can_be_flushed_before_channel_close(fake_nio, monkeypatch):
    """证据入口收口前必须能等后台重试完成，不能 close() 把它截断。"""
    monkeypatch.setattr(matrix_bus, "DEFAULT_SEND_TIMEOUT", 0.05)
    fake_nio.send_delay = 0.2
    channel = open_channel(_live_config())
    client = fake_nio.instances[-1]

    with pytest.raises(RoomSendTimeout):
        channel.send("最终回执", "<p>最终回执</p>")
    assert channel.flush_pending_sends(timeout=1.0) is True
    assert [row["body"] for row in client.sent] == ["最终回执"]
    channel.close()


def test_timed_out_send_that_eventually_fails_makes_flush_fail_closed(
        fake_nio, monkeypatch):
    """后台重试最终失败不能从 pending 消失后伪装成 flush 成功。"""
    monkeypatch.setattr(matrix_bus, "DEFAULT_SEND_TIMEOUT", 0.02)
    channel = open_channel(_live_config())
    client = fake_nio.instances[-1]

    async def delayed_failure(room_id, message_type, content, **kwargs):
        import asyncio

        await asyncio.sleep(0.08)
        return _FakeSendError("delayed send failed")

    client.room_send = delayed_failure
    with pytest.raises(RoomSendTimeout):
        channel.send("最终迁移", "<p>最终迁移</p>")
    assert channel.flush_pending_sends(timeout=1.0) is False
    channel.close()


def test_send_gets_a_wider_timeout_than_construction(fake_nio):
    """send 与构造期不共用一档超时。共用的话，连发几条镜像必然一串假失败。"""
    channel = open_channel(_live_config())
    assert channel._send_timeout > channel._timeout, (
        "send 的超时不比构造期宽 —— 一次 429 退避就是几秒")
    channel.close()


# -- 8.4 坑三：退出时刷一屏 asyncio 报错 ----------------------------------
def test_close_leaves_no_running_loop_or_thread(fake_nio):
    """收口后循环关了、线程退了、常驻协程也 cancel 了。

    少了这三步的代价不是崩，是退出时刷一屏 ``RuntimeError: Event loop is closed``
    / ``Task was destroyed but it is pending!``：``sync_forever`` 和 aiohttp 的连接池
    都还活着，循环却停了，GC 去跑它们的 ``__del__`` 就每个都要碰那个死循环。
    报错在终态**之后**，不影响判定 —— 但它把真正该看的那几行冲出了屏幕，
    而这套链路所有的失败都只在日志里。
    """
    fake_nio.hang_sync = True                   # 真 nio 的形态：sync_forever 不返回
    channel = open_channel(_live_config())
    client = fake_nio.instances[-1]
    channel.listen(lambda sender, body: None)
    assert client.sync_done.wait(5), "sync_forever 没起来"

    channel.close()

    assert client.sync_stopped is True and client.closed is True
    assert channel._loop.is_closed(), "事件循环没关 —— GC 时那些 __del__ 会去碰它"
    channel._thread.join(timeout=5)
    assert not channel._thread.is_alive(), "守护线程没退出"
    assert channel._sync_task is None, "sync_forever 那条协程还挂着"


def test_close_is_idempotent(fake_nio):
    """收口调两次不许炸。演示脚本的收口路径不止一条（超时分支也走它）。"""
    channel = open_channel(_live_config())
    channel.close()
    channel.close()


# -- 8.5 坑四：bot 不听自己的回声，但要说出来 -----------------------------
def test_bot_talking_to_itself_is_dropped_but_said_out_loud(fake_nio, caplog):
    """bot 账号自己打的 /approve 照旧丢弃，但必须留一句「换个账号」。

    回声过滤本身没错，不过滤就自激。错的是它的症状：房间里什么都不会发生 ——
    没有回执、没有报错、任务照旧停在 BLOCKED，发命令的人只能以为程序没在听。
    """
    channel = open_channel(_live_config())
    client = fake_nio.instances[-1]
    client.live = [(_Room("!room:example.org"), _Msg(BOT_MXID, "/approve task-1"))]

    seen: list[tuple[str, str]] = []
    with caplog.at_level("WARNING", logger="maos.matrix"):
        channel.listen(lambda sender, body: seen.append((sender, body)))
        assert client.sync_done.wait(5), "sync_forever 没跑完"

    assert seen == [], "回声过滤没拦住自己的消息"
    assert "不听自己的回声" in caplog.text, f"丢得静悄悄：{caplog.text}"
    assert "MAOS_APPROVERS" in caplog.text, "没说清下一步该用哪个账号"
    channel.close()


def test_bots_own_ordinary_messages_stay_silent(fake_nio, caplog):
    """自己发的**普通**回执本就该悄悄丢 —— 每条都喊一句就成了刷屏。"""
    channel = open_channel(_live_config())
    client = fake_nio.instances[-1]
    client.live = [(_Room("!room:example.org"), _Msg(BOT_MXID, "[t1] TaskResult → ok"))]

    with caplog.at_level("WARNING", logger="maos.matrix"):
        channel.listen(lambda sender, body: None)
        assert client.sync_done.wait(5), "sync_forever 没跑完"

    assert "不听自己的回声" not in caplog.text, f"把普通回执也喊了：{caplog.text}"
    channel.close()


# ==========================================================================
# 8. 回调离开事件循环线程（真房间实测：回调里 send / fetch 必然 30s 超时）
# ==========================================================================
# nio 在 sync_forever 里 await 回调，回调就跑在私有循环线程上；回调里再
# run_coroutine_threadsafe(...).result() 等同一条循环，是自己等自己。
# 症状：回帖固定迟到 30s、取件 100% 超时 —— 而回调里不发消息的单测全绿。
# 下面四条钉的是「listen 的回调不在循环线程上，且回调里能 send / fetch」。


def _room_and_att_events():
    room = _Room("!room:example.org")
    image = _FakeRoomMessageImage(APPROVER, "破损.jpg", "mxc://example.org/img1",
                                  mimetype="image/jpeg", size=412)
    sheet = _FakeRoomMessageFile(APPROVER, "bad-requests.csv", "mxc://example.org/csv1",
                                 mimetype="text/csv", size=463)
    return room, image, sheet


def test_listener_callbacks_run_off_the_event_loop_thread(fake_nio, monkeypatch):
    """回调线程 != 循环线程，且回调里 send 当场落地、不撞 RoomSendTimeout。

    把 send 超时压到 0.5s：死锁还在的话，这里会在 0.5s 后拿到 RoomSendTimeout，
    而不是等 30s —— 用例失败得快、且失败的理由指向根因。
    """
    monkeypatch.setattr(matrix_bus, "DEFAULT_SEND_TIMEOUT", 0.5)
    channel = open_channel(_live_config())
    client = fake_nio.instances[-1]
    client.live = [(_Room("!room:example.org"), _Msg(APPROVER, "/approve t1"))]

    seen: list[dict] = []

    def on_message(sender: str, body: str) -> None:
        entry = {"thread": threading.get_ident(), "error": None}
        try:
            channel.send(f"回执 {body}", "<p>回执</p>")
        except Exception as exc:                        # noqa: BLE001
            entry["error"] = exc
        seen.append(entry)

    channel.listen(on_message)
    assert client.sync_done.wait(5), "sync_forever 没跑完"

    assert len(seen) == 1
    assert seen[0]["error"] is None, f"回调里 send 失败了：{seen[0]['error']!r}"
    assert seen[0]["thread"] != channel._thread.ident, "回调仍在事件循环线程上跑"
    assert [row["body"] for row in client.sent] == ["回执 /approve t1"]
    channel.close()


def test_file_and_image_events_both_reach_on_attachment(fake_nio):
    """m.file 与 m.image 都进 on_attachment，各带自己的 kind；文本回调收不到它们。

    Element 把 PDF / CSV 和它认不出的一切都发成 m.file。只挂 m.image 的后果
    不是「拒收」而是一声不吭 —— 真房间实测，发的人只能得出「机器人挂了」。
    """
    channel = open_channel(_live_config())
    client = fake_nio.instances[-1]
    room, image, sheet = _room_and_att_events()
    client.live = [(room, _Msg(APPROVER, "先说一句")), (room, image), (room, sheet)]

    texts: list[str] = []
    atts: list = []
    channel.listen(lambda sender, body: texts.append(body),
                   lambda sender, att: atts.append(att))
    assert client.sync_done.wait(5), "sync_forever 没跑完"

    assert texts == ["先说一句"], f"附件事件漏进了文本回调：{texts}"
    assert [(a.kind, a.filename, a.file_key, a.mime, a.size) for a in atts] == [
        ("image", "破损.jpg", "mxc://example.org/img1", "image/jpeg", 412),
        ("file", "bad-requests.csv", "mxc://example.org/csv1", "text/csv", 463),
    ]
    assert all(a.channel == matrix_bus.CHANNEL_MATRIX for a in atts)
    channel.close()


def test_fetch_works_from_inside_the_attachment_callback(fake_nio, monkeypatch):
    """取件就发生在回调里（有人发图 -> 回调 -> 取件）—— 这条路径必须不超时。

    这是原始事故的直接复现：修复前 fetch 在循环线程上等 download，30s 超时；
    修复后回调在工作线程上，download 协程能被循环推进。
    """
    monkeypatch.setattr(matrix_bus, "DEFAULT_SEND_TIMEOUT", 0.5)
    channel = open_channel(_live_config())
    client = fake_nio.instances[-1]
    room, image, _sheet = _room_and_att_events()
    client.live = [(room, image)]

    got: list = []

    def on_attachment(sender: str, att) -> None:
        try:
            got.append(channel.fetch(att))
        except Exception as exc:                        # noqa: BLE001
            got.append(exc)

    channel.listen(lambda sender, body: None, on_attachment)
    assert client.sync_done.wait(5), "sync_forever 没跑完"

    assert got and isinstance(got[0], bytes), f"回调里取件失败：{got}"
    assert got[0].startswith(b"%PDF-fake-bytes-for-mxc://example.org/img1")
    assert "download(mxc://example.org/img1)" in client.calls
    channel.close()


def test_exploding_callback_does_not_kill_the_listener(fake_nio, caplog):
    """回调抛了异常：记日志、继续听下一条。掀掉 sync_forever 的症状是此后全静默。"""
    channel = open_channel(_live_config())
    client = fake_nio.instances[-1]
    room = _Room("!room:example.org")
    client.live = [(room, _Msg(APPROVER, "第一条会炸")), (room, _Msg(APPROVER, "第二条"))]

    seen: list[str] = []

    def on_message(sender: str, body: str) -> None:
        if "炸" in body:
            raise RuntimeError("处理第一条时炸了")
        seen.append(body)

    with caplog.at_level("WARNING", logger="maos.matrix"):
        channel.listen(on_message)
        assert client.sync_done.wait(5), "sync_forever 没跑完 —— 回调的异常把它掀了"

    assert seen == ["第二条"]
    assert "房间回调抛出异常" in caplog.text and "处理第一条时炸了" in caplog.text
    channel.close()


def test_close_waits_for_the_in_flight_callback(fake_nio, monkeypatch):
    """收口时正在跑的回调要**跑完**，且它里面的 send 要落地。

    评审实测：`shutdown(wait=False)` 后立刻关客户端、停循环，回调里的 send 撞
    `Event loop is closed`、回帖丢掉；卡在 fetch 上的回调要等满 send_timeout 才出来，
    进程退出被拖住 30s。
    """
    monkeypatch.setattr(matrix_bus, "DEFAULT_SEND_TIMEOUT", 2.0)
    channel = open_channel(_live_config())
    client = fake_nio.instances[-1]
    client.live = [(_Room("!room:example.org"), _Msg(APPROVER, "慢慢处理"))]
    started = threading.Event()
    outcome: list = []

    def on_message(sender: str, body: str) -> None:
        started.set()
        time.sleep(0.4)                                 # 模拟一次慢处理（调模型 / 跑预检）
        try:
            channel.send("处理完了", "<p>处理完了</p>")
            outcome.append("sent")
        except Exception as exc:                        # noqa: BLE001
            outcome.append(exc)

    channel.listen(on_message)
    assert started.wait(5), "回调没起来"
    t0 = time.time()
    channel.close()                                     # 回调还在 sleep 里
    elapsed = time.time() - t0

    assert outcome == ["sent"], f"收口把在跑的回调坑了：{outcome}"
    assert [row["body"] for row in client.sent] == ["处理完了"]
    assert elapsed < 1.5, f"close 等了 {elapsed:.1f}s —— 像是等到了 send_timeout 而不是等回调跑完"


def test_malformed_media_event_does_not_kill_the_listener(fake_nio, caplog):
    """一条 content.info 形状不对的附件事件：记日志、跳过；下一条照收。

    这几行跑在循环线程上、在 _offload 之前；nio 对回调抛出的异常是 `except: raise`，
    不包住的话整条 sync 就没了 —— 进程还在，房间里从此一片安静。
    """
    channel = open_channel(_live_config())
    client = fake_nio.instances[-1]
    room, image, _sheet = _room_and_att_events()
    broken = _FakeRoomMessageImage(APPROVER, "坏事件.png", "mxc://example.org/bad")
    broken.source = {"content": {"info": "not-a-dict"}}
    client.live = [(room, broken), (room, image)]

    atts: list = []
    with caplog.at_level("WARNING", logger="maos.matrix"):
        channel.listen(lambda sender, body: None, lambda sender, att: atts.append(att))
        assert client.sync_done.wait(5), "sync_forever 没跑完 —— 坏事件把它掀了"

    assert [a.filename for a in atts] == ["破损.jpg"]
    assert "房间附件事件形状不对" in caplog.text
    # 假 sync_forever 派发完就正常返回：sync_done 置位在协程收尾之前，等它一下。
    deadline = time.time() + 5
    while time.time() < deadline and channel.alive():
        time.sleep(0.02)
    assert channel.alive() is False and channel.failure() == ""
    channel.close()


def test_alive_and_failure_reflect_the_sync_task(fake_nio):
    """常驻入口靠 alive() 决定要不要退出；sync 炸了要能从 failure() 读到原因。"""
    fake_nio.hang_sync = True
    channel = open_channel(_live_config())
    assert channel.alive() is False, "还没 listen 就说活着"
    channel.listen(lambda sender, body: None)
    assert channel.alive() is True and channel.failure() == ""
    channel.close()
    assert channel.alive() is False

    async def exploding_sync(*a, **kw):
        raise RuntimeError("sync 炸了")

    channel2 = open_channel(_live_config())
    fake_nio.instances[-1].sync_forever = exploding_sync
    channel2.listen(lambda sender, body: None)
    deadline = time.time() + 5
    while time.time() < deadline and channel2.alive():
        time.sleep(0.02)
    assert channel2.alive() is False
    assert "sync 炸了" in channel2.failure()
    channel2.close()


# -- 忽略名单：同房间里别的机器人账号（T84）--------------------------------
#: 退款圆桌那五个岗位号之一。本文件用 example.org 域，与真房间的 maos.local 无关。
VOICE_MXID = "@maos-intake:example.org"


def test_should_deliver_drops_ignored_senders():
    """名单里的 sender 不进 on_message；名单外的照进（第三条否决）。

    一个房间只该有一个监听者：岗位号的发言若喂进 ``on_message``，闲聊回复器会接
    一句、岗位号下一轮再接 —— 两个机器人互相接龙刷屏，而每一条单看都合法。
    """
    room = _Room("!room:example.org")
    ignored = frozenset({VOICE_MXID})
    assert should_deliver("!room:example.org", BOT_MXID, room,
                          _Msg(VOICE_MXID, "受理完毕，转规则审核岗"),
                          ignored_senders=ignored) is False
    assert should_deliver("!room:example.org", BOT_MXID, room,
                          _Msg(APPROVER, "/approve t1"),
                          ignored_senders=ignored) is True


def test_should_deliver_positional_call_is_still_compatible():
    """四个位置实参、不传 keyword 时行为与加名单之前**逐字一致**。

    钉的是调用形态：现存三个调用方（``listen`` 两处与 ``scripts/matrix_probe.py``）
    全是四个位置实参。第五个参数若写成位置参数，它们会一起变成 TypeError ——
    而那是在**回调里**抛的，症状是房间从此一片安静，没有一行日志说为什么。
    """
    room = _Room("!room:example.org")
    assert should_deliver("!room:example.org", BOT_MXID, room,
                          _Msg(VOICE_MXID, "受理完毕")) is True
    assert should_deliver("!room:example.org", BOT_MXID, room,
                          _Msg(BOT_MXID, "[t1] TaskResult")) is False
    assert should_deliver("!room:example.org", BOT_MXID, _Room("!other:example.org"),
                          _Msg(APPROVER, "/approve t1")) is False

    kind = inspect.signature(should_deliver).parameters["ignored_senders"].kind
    assert kind is inspect.Parameter.KEYWORD_ONLY


def _listen_and_collect(channel, client, events):
    """挂上监听、喂一批 live 事件、把进到 on_message 的 sender 收回来。"""
    seen: list[str] = []
    client.live = list(events)
    channel.listen(lambda sender, body: seen.append(sender))
    assert client.sync_done.wait(5), "sync_forever 没跑完"
    return seen


def test_open_channel_reads_room_bots_from_env_when_not_given(fake_nio, monkeypatch):
    """不传 ``ignored_senders`` 时 ``open_channel`` **现读** ``MAOS_ROOM_BOTS``。

    现读而不是让调用方传，是为了让 ``hiclaw/room_ingress.py`` 那个常驻入口**一个字
    不改**就吃到岗位账号名单 —— 它拿到的正是 ``open_channel`` 的返回值。口径同
    ``current_approvers``：读进程环境，不进 config、不进 dataclass（C-6 冻结）。
    """
    room = _Room("!room:example.org")
    events = [(room, _Msg(VOICE_MXID, "受理完毕，转规则审核岗")),
              (room, _Msg(APPROVER, "/approve t1"))]

    monkeypatch.setenv(matrix_bus.ENV_ROOM_BOTS,
                       VOICE_MXID + ", @maos-policy:example.org")
    channel = open_channel(_live_config())
    assert _listen_and_collect(channel, fake_nio.instances[-1], events) == [APPROVER]
    channel.close()

    # 名单撤掉，同一批事件全进来 —— 证明上面那条挡住的确实是名单，不是别的否决。
    monkeypatch.delenv(matrix_bus.ENV_ROOM_BOTS)
    channel2 = open_channel(_live_config())
    assert _listen_and_collect(channel2, fake_nio.instances[-1], events) == \
        [VOICE_MXID, APPROVER]
    channel2.close()


def test_explicit_ignored_senders_overrides_env(fake_nio, monkeypatch):
    """显式传 ``ignored_senders=`` 时不读 env —— 「就按我给的这份读」。

    与 ``MatrixBusConfig.from_env(env=...)`` 同一条取向：显式给了还去读进程环境，
    会让测试与降级自检拿到一份自己没给过的名单。
    """
    room = _Room("!room:example.org")
    monkeypatch.setenv(matrix_bus.ENV_ROOM_BOTS, VOICE_MXID)

    channel = open_channel(_live_config(), ignored_senders=frozenset())
    seen = _listen_and_collect(channel, fake_nio.instances[-1],
                              [(room, _Msg(VOICE_MXID, "受理完毕"))])
    assert seen == [VOICE_MXID], "env 的名单在显式传参时仍然生效了"
    channel.close()

    channel2 = open_channel(_live_config(), ignored_senders=frozenset({APPROVER}))
    seen2 = _listen_and_collect(channel2, fake_nio.instances[-1],
                               [(room, _Msg(VOICE_MXID, "受理完毕")),
                                (room, _Msg(APPROVER, "/approve t1"))])
    assert seen2 == [VOICE_MXID], "显式名单没生效"
    channel2.close()
