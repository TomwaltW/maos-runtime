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

import inspect

import pytest

from hiclaw.matrix_bus import (ACTION_APPROVE, ENV_APPROVERS, ENV_HOMESERVER, ENV_ROOM_ID,
                               ENV_TOKEN, ENV_USER, EVENT_APPROVAL_DENIED,
                               MAX_MIRROR_FAILURES, REQUIRED_ENV, USAGE, ApprovalCommand,
                               MatrixBusConfig, MatrixEventBus, RoomApprovalBridge,
                               looks_like_command, parse_approval_command, redact,
                               render_mirror)
from maos.contracts import events as E
from maos.contracts.events import Topic
from maos.contracts.states import PlanState, TaskState
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
def _blocked_plan():
    """跑到「高风险任务停在 BLOCKED」那一刻，与场景 3 同一条路径。"""
    store, bus, cp, _model, _worker, gate = build({"任务输入": GOOD_PATCH})
    plan_id = cp.create_plan(goal="高风险变更需人工放行", trace_id=E.new_id("trace"), tasks=[{
        "role": "coding", "title": "变更生产环境配置",
        "inputs": {"repo": "demo/app"}, "acceptance": ["build 通过"],
        "risk_level": "M", "effect_risk": "H",
    }])
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


def test_approver_can_reject_from_room_with_reason():
    """合法：/reject 带原因 -> FAILED，原因落进 event_log 的 detail。"""
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
