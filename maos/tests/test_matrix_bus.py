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
import threading
import time

import pytest

from hiclaw.matrix_bus import (ACTION_APPROVE, ENC_CLEAR, ENC_ENCRYPTED, ENC_ERROR,
                               ENV_APPROVERS, ENV_HOMESERVER, ENV_ROOM_ID, ENV_TOKEN,
                               ENV_USER, EVENT_APPROVAL_DENIED, MAX_MIRROR_FAILURES,
                               REQUIRED_ENV, USAGE, ApprovalCommand, MatrixBusConfig,
                               MatrixEventBus, RoomApprovalBridge, encryption_verdict,
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


class _FakeRoomMessageText:
    """占位：只用来当 add_event_callback 的过滤类型。"""


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
        self.sent.append(content)
        return object()

    async def sync(self, timeout=None, sync_filter=None, since=None, *a, **kw):
        self.calls.append(f"sync(filter={sync_filter!r})")
        await self._dispatch(self.history)
        self.next_batch = "s72_1_2_3"
        return object()

    async def sync_forever(self, timeout=None, **kw):
        self.calls.append("sync_forever")
        await self._dispatch(self.live)
        self.sync_done.set()           # 用例靠它等一轮派发跑完，不靠 sleep 猜

    async def _dispatch(self, events):
        for room, event in events:
            for cb, _cls in list(self.callbacks):
                await cb(room, event)

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
    module.RoomMessageText = _FakeRoomMessageText
    module.RoomGetStateEventError = _FakeStateError
    monkeypatch.setitem(sys.modules, "nio", module)

    _FakeAsyncClient.instances = []
    _FakeAsyncClient.whoami_result = _FakeWhoamiResponse(BOT_MXID)
    _FakeAsyncClient.state_result = _FakeStateError("M_NOT_FOUND", "Event not found.")
    yield _FakeAsyncClient
    _FakeAsyncClient.instances = []


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
