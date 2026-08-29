"""房间接线测试 —— 状态迁移镜像 + 房间审批独立入口（task-C3）。

**一条都不碰网络**：全部注入假 channel。判据必须在没有 Synapse 的机器上成立，
否则「房间没起来」和「接线写错了」两种失败长得一模一样，而演示当天只有一次机会
分辨它们。

守的是四件出事时症状离原因最远的东西：

1. **幂等靠行序。** 轮询器重复跑不许重发同一条。破了的症状不是报错，是房间被
   同一串迁移刷屏，而人类要在那串刷屏里找那条待审批的高风险任务。
2. **旁路语义。** channel 抛异常时轮询器必须吞掉、断点照常推进、流水线照跑。
   用一个「必炸」的通道验，不靠读代码相信 try/except 写对了。
3. **越权留痕。** 名单外的人发 ``/approve``，除了回绝还必须**落一条 event_log**，
   且任务状态一个字节都不许动。「系统拒绝了一次越权审批」本身就是给评委看的证据。
4. **前置检查在装配之前。** ``--case reject`` 缺 ``MAOS_SANDBOX_WORKDIR`` 必须
   启动即报错。晚一步的代价是人在 Element 里打完 ``/reject`` 才收到「审批未生效」。
"""

from __future__ import annotations

import time

import pytest

from hiclaw import room_demo
from hiclaw.matrix_bus import (ENV_APPROVERS, EVENT_APPROVAL_DENIED, MatrixBusConfig,
                               RoomApprovalBridge)
from hiclaw.transition_mirror import MIRRORED_EVENT_TYPES, TransitionMirror
from maos.contracts.states import TaskState
from maos.core.control_plane import ENV_SANDBOX_WORKDIR
from maos.flows.common import GOOD_PATCH, build
from maos.runtime.gate import HumanApprovalQueue

APPROVER = "@boss:example.org"
OUTSIDER = "@mallory:example.org"


# --------------------------------------------------------------------------
# 假通道
# --------------------------------------------------------------------------
class RecordingChannel:
    """收下每一条镜像，不出网。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.closed = False

    def send(self, plain: str, html: str) -> None:
        self.sent.append((plain, html))

    def close(self) -> None:
        self.closed = True

    @property
    def lines(self) -> list[str]:
        """每条消息的摘要行 —— 断言用这个，不用整段 JSON。"""
        return [plain.splitlines()[0] for plain, _html in self.sent]


class ExplodingChannel:
    """每次 send 都炸。用来验旁路语义 —— 光读 try/except 不算验过。"""

    def __init__(self) -> None:
        self.attempts = 0

    def send(self, plain: str, html: str) -> None:
        self.attempts += 1
        raise RuntimeError("房间连接断了")

    def close(self) -> None:
        pass


class ScriptedRoom(RecordingChannel):
    """假房间：看到审批卡就记下 task_id，``listen`` 一挂上就把脚本命令喂回来。

    同步喂而不是起线程：测试要的是确定性，而真房间那条路径（``_NioChannel.listen``
    起 ``sync_forever``）的异步性不是这个测试要证明的东西。
    """

    def __init__(self, sender: str, action: str = "approve", reason: str = "") -> None:
        super().__init__()
        self.sender = sender
        self.action = action
        self.reason = reason
        self.task_id = ""

    def send(self, plain: str, html: str) -> None:
        super().send(plain, html)
        for line in plain.splitlines():
            stripped = line.strip()
            if stripped.startswith("/approve "):
                self.task_id = stripped.split()[1]

    def listen(self, on_message) -> None:               # noqa: ANN001
        assert self.task_id, "审批卡必须先发出来，命令里的 task_id 才有出处"
        command = f"/{self.action} {self.task_id}"
        if self.reason:
            command += f" {self.reason}"
        on_message(self.sender, command)


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------
def _live_config(**over) -> MatrixBusConfig:
    """一份「看起来能连上」的配置，配合注入的假通道用。"""
    base = dict(homeserver="https://matrix.example.org", user="@maos-bot:example.org",
                token="not-a-real-token", room_id="!room:example.org",
                approvers=frozenset({APPROVER}))
    base.update(over)
    return MatrixBusConfig(**base)


@pytest.fixture
def blocked():
    """一个 effect_risk=H、已过闸、停在 BLOCKED 的任务。

    造法直接借 ``room_demo.seed_blocked_task``，不在测试里另抄一遍 —— 抄一遍就是
    留第二条构造路径，两条一定会漂。
    """
    store, bus, cp, _model, _worker, gate = build({"任务输入": GOOD_PATCH})
    plan_id, task = room_demo.seed_blocked_task(store, cp, bus, gate)
    assert task["state"] == TaskState.BLOCKED
    return store, bus, cp, plan_id, task


# --------------------------------------------------------------------------
# 1. 迁移镜像
# --------------------------------------------------------------------------
def test_mirror_renders_transitions_and_never_resends(blocked):
    """迁移进 channel，且重复轮询不重发 —— 断点是 seq，不是时间戳。"""
    store, bus, cp, plan_id, task = blocked
    channel = RecordingChannel()
    mirror = TransitionMirror(store, plan_id, channel)

    first = mirror.poll_once()
    assert first > 0, "过闸到 BLOCKED 这一路的迁移一条都没镜像出来"
    assert any("RUNNING → AWAITING_REVIEW" in line for line in channel.lines), (
        f"BACKLOG task-E 第 3 条点名的那条迁移不在房间里：{channel.lines}")
    assert all(" → " in line for line in channel.lines)

    # 幂等：库里没有新行，重复轮询必须一条都不发。
    assert mirror.poll_once() == 0
    assert mirror.poll_once() == 0
    assert len(channel.sent) == first

    # 有新迁移才继续发，且只发新的那几条。
    HumanApprovalQueue(store, cp).decide(
        task["task_id"], approved=True, operator=APPROVER, note="已核对")
    bus.drain()
    more = mirror.poll_once()
    assert more > 0
    assert len(channel.sent) == first + more
    assert any("BLOCKED → DONE" in line for line in channel.lines)
    assert mirror.mirrored == first + more


def test_mirror_only_takes_transition_events(blocked):
    """只镜像迁移类事件；SkillInvoked / CompensationAttached 之类不许混进来。"""
    store, _bus, _cp, plan_id, _task = blocked
    channel = RecordingChannel()
    TransitionMirror(store, plan_id, channel).poll_once()

    rows = store.list_event_log(plan_id)
    noise = {r["event_type"] for r in rows} - set(MIRRORED_EVENT_TYPES)
    assert noise, "这个夹具本该产生非迁移事件，否则这条断言什么都没验到"
    assert len(channel.sent) == sum(
        1 for r in rows if r["event_type"] in MIRRORED_EVENT_TYPES)
    for kind in noise:
        assert not any(kind in line for line in channel.lines), (
            f"{kind} 不是迁移，混进房间会把轨迹淹掉")


def test_mirror_is_noop_when_degraded(blocked):
    """降级（channel is None）时是 no-op：不发、不抛、连库都不查。"""
    store, _bus, _cp, plan_id, _task = blocked

    class ExplodingStore:
        def list_event_log(self, plan_id):              # noqa: ANN001
            raise AssertionError("降级时不该去查 event_log")

    mirror = TransitionMirror(ExplodingStore(), plan_id, None)
    assert mirror.muted is True
    assert mirror.poll_once() == 0                      # 不抛 = 这一行跑得过去
    mirror.start()                                      # 降级时不该起线程
    assert mirror._thread is None
    mirror.stop()
    assert mirror.mirrored == 0


def test_mirror_swallows_channel_failure_and_pipeline_continues(blocked):
    """channel 必炸时：吞掉、断点照常推进、流水线一点不受影响。"""
    store, bus, cp, plan_id, task = blocked
    channel = ExplodingChannel()
    mirror = TransitionMirror(store, plan_id, channel)

    sent = mirror.poll_once()                           # 不抛
    assert sent == 0, "全炸了不该报告发出去过"
    assert channel.attempts > 0, "得真试过才算验了旁路"

    # 断点仍然推进：失败的那几行不许在下一轮重发（否则房间会被失败墙刷满）。
    attempts_after_first = channel.attempts
    assert mirror.poll_once() == 0
    assert channel.attempts == attempts_after_first

    # 流水线继续：审批照样能放行，任务照样到终态。
    HumanApprovalQueue(store, cp).decide(
        task["task_id"], approved=True, operator=APPROVER, note="已核对")
    bus.drain()
    assert store.get_task(task["task_id"])["state"] == TaskState.DONE


def test_mirror_stop_returns_within_one_second(blocked):
    """``stop()`` 必须一秒内退 —— 演示当天 Ctrl-C 要干净。"""
    store, _bus, _cp, plan_id, _task = blocked
    mirror = TransitionMirror(store, plan_id, RecordingChannel(), interval=0.2)
    mirror.start()
    assert mirror._thread is not None
    started = time.monotonic()
    mirror.stop()
    assert time.monotonic() - started < 1.0
    assert mirror._thread is None


def test_from_bus_accepts_both_channel_shapes():
    """``channel`` 只读属性（C-2 本轮补）到位前后，取法都得能用。"""
    room = RecordingChannel()

    class OnlyPrivate:                                  # C-2 提交之前的形状
        def __init__(self) -> None:
            self._channel = room

    class HasPublic:                                    # C-2 提交之后的形状
        channel = room

    class Degraded:
        _channel = None

    assert TransitionMirror.from_bus(OnlyPrivate(), None, "p").channel is room
    assert TransitionMirror.from_bus(HasPublic(), None, "p").channel is room
    assert TransitionMirror.from_bus(Degraded(), None, "p").muted is True


# --------------------------------------------------------------------------
# 2. 审批链路
# --------------------------------------------------------------------------
def test_room_approve_moves_task_out_of_blocked(blocked):
    """房间里一条 /approve 走通全程：hq.decide 生效，任务离开 BLOCKED。"""
    store, bus, cp, plan_id, task = blocked
    channel = RecordingChannel()
    bridge = RoomApprovalBridge(HumanApprovalQueue(store, cp), _live_config(),
                                channel=channel)

    reply = bridge.handle_message(APPROVER, f"/approve {task['task_id']}")
    bus.drain()

    assert "已批准" in reply and task["task_id"] in reply
    assert channel.sent, "回执必须也发回房间，不能只有函数返回值"
    assert store.get_task(task["task_id"])["state"] == TaskState.DONE
    assert store.get_plan(plan_id)["state"] == "DONE"


def test_outsider_is_denied_recorded_and_changes_nothing(blocked):
    """越权：回绝 + 落一条 event_log + 任务状态一个字节都不动。"""
    store, _bus, cp, plan_id, task = blocked
    before = dict(store.get_task(task["task_id"]))
    channel = RecordingChannel()
    bridge = RoomApprovalBridge(HumanApprovalQueue(store, cp), _live_config(),
                                channel=channel)

    reply = bridge.handle_message(OUTSIDER, f"/approve {task['task_id']}")

    assert "无审批权限" in reply and OUTSIDER in reply
    denied = [e for e in store.list_event_log(plan_id)
              if e["event_type"] == EVENT_APPROVAL_DENIED]
    assert len(denied) == 1, "越权尝试必须留痕，静默丢弃等于把证据扔了"
    assert denied[0]["detail"]["sender"] == OUTSIDER
    assert denied[0]["task_id"] == task["task_id"]
    assert store.get_task(task["task_id"])["state"] == TaskState.BLOCKED
    assert dict(store.get_task(task["task_id"])) == before


def test_approval_card_spells_out_the_commands(blocked):
    """审批卡必须逐字写出可用指令 —— 房间里的人不会去翻文档。"""
    _store, _bus, _cp, _plan_id, task = blocked
    plain, html = room_demo.approval_card(task)

    assert f"/approve {task['task_id']}" in plain
    assert f"/reject {task['task_id']}" in plain
    assert task["title"] in plain
    assert "<details>" in html and "Envelope JSON" in html
    assert "<code>/approve" in html


# --------------------------------------------------------------------------
# 3. 独立入口
# --------------------------------------------------------------------------
def test_reject_without_workdir_fails_before_assembling_anything(monkeypatch):
    """``--case reject`` 缺 workdir 时启动即报错，且**早于装配**，绝不进等待。

    用「装配必炸」来证明次序：光断言返回码 3 证明不了它是在装配之前拦的，
    而「晚一步拦」正是这条要防的失效形态。
    """
    def _boom(*a, **kw):
        raise AssertionError("前置检查必须早于装配")

    monkeypatch.setattr(room_demo, "build", _boom)
    monkeypatch.delenv(ENV_SANDBOX_WORKDIR, raising=False)

    assert room_demo.main(["--case", "reject"]) == room_demo.EXIT_PRECONDITION

    # 反向对照：env 齐了就该往下走（于是撞上那个必炸的装配）——
    # 少了这一半，上面那条断言在「永远返回 3」的实现下也成立。
    monkeypatch.setenv(ENV_SANDBOX_WORKDIR, "/private/tmp")
    with pytest.raises(AssertionError, match="前置检查必须早于装配"):
        room_demo.main(["--case", "reject"])


def test_reject_with_nonexistent_workdir_also_fails_fast(monkeypatch, tmp_path):
    """目录不存在同样拦下 —— 否则补偿会在 stage=prepare 上挂，看起来像补偿坏了。"""
    monkeypatch.setattr(room_demo, "build",
                        lambda *a, **kw: pytest.fail("不该走到装配"))
    monkeypatch.setenv(ENV_SANDBOX_WORKDIR, str(tmp_path / "并不存在"))
    assert room_demo.main(["--case", "reject"]) == room_demo.EXIT_PRECONDITION


def test_degraded_demo_runs_end_to_end_without_a_room(monkeypatch, capsys):
    """没有房间也能走完全程 —— C-4 写 runbook 与本轨测试都不依赖 Synapse。"""
    for name in ("MATRIX_HOMESERVER", "MATRIX_USER", "MATRIX_TOKEN", "MATRIX_ROOM_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv(ENV_APPROVERS, raising=False)

    assert room_demo.run_demo("approve", timeout=10, auto_approve=True) == room_demo.EXIT_OK

    out = capsys.readouterr().out
    assert "[降级]" in out
    assert "房间消息" in out, "本该发进房间的每一条必须按原文打到 stdout"
    assert "RUNNING → AWAITING_REVIEW" in out
    assert "可用指令" in out
    assert "终态: task=DONE" in out


def test_room_demo_drives_a_real_approval_through_the_channel(monkeypatch):
    """接通「房间」时走的是 listen -> bridge -> decide 这条真链路，不是模拟审批。"""
    room = ScriptedRoom(APPROVER)
    monkeypatch.setattr(room_demo, "bus_channel", lambda bus: room)
    monkeypatch.setenv(ENV_APPROVERS, APPROVER)

    assert room_demo.run_demo("approve", timeout=10, auto_approve=False) == room_demo.EXIT_OK
    assert room.task_id, "审批卡没发出来，命令的 task_id 就没有出处"
    assert any("已批准" in line for line in room.lines)
    assert any("BLOCKED → DONE" in line for line in room.lines), (
        "放行后的迁移必须补进房间 —— stop(flush=True) 就是为这一行存在的")


def test_auto_approve_is_refused_when_a_room_is_connected(monkeypatch):
    """有真房间时 ``--auto-approve`` 必须拒掉：那会让演示自己给自己签字放行。"""
    monkeypatch.setattr(room_demo, "bus_channel", lambda bus: RecordingChannel())
    monkeypatch.setenv(ENV_APPROVERS, APPROVER)
    assert room_demo.run_demo(
        "approve", timeout=1, auto_approve=True) == room_demo.EXIT_PRECONDITION


def test_timeout_is_not_disguised_as_success(monkeypatch):
    """没人审批就非 0 退出，任务仍停在 BLOCKED。超时不许伪装成成功。"""
    monkeypatch.setattr(room_demo, "bus_channel", lambda bus: RecordingChannel())
    monkeypatch.setenv(ENV_APPROVERS, APPROVER)
    assert room_demo.run_demo(
        "approve", timeout=0.3, auto_approve=False) == room_demo.EXIT_TIMEOUT
