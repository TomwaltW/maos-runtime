"""场景 7 的退款审批由 Matrix 房间命令驱动时的接线回归。"""

from __future__ import annotations

import json
import threading
import time

import pytest

from maos.contracts.states import PlanState, TaskState
from maos.domain.refund import objects
from maos.flows import scenario_7 as s7
from maos.skills.builtin.refund import _common as C
from hiclaw import room_demo
from hiclaw.matrix_bus import EVENT_APPROVAL_DENIED, MatrixBusConfig
from hiclaw.transition_mirror import TransitionMirror


APPROVER = "@boss:maos.local"
OUTSIDER = "@intern:maos.local"


class ScriptedRefundRoom:
    """只在收到真实审批卡后投递命令；不模拟网络。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.callback = None
        self._finance_done = False
        self._payment_done = False
        self.flush_calls: list[float] = []

    def listen(self, callback) -> None:
        self.callback = callback

    def send(self, plain: str, html: str) -> None:
        self.sent.append((plain, html))
        if "HumanApprovalRequired" not in plain or self.callback is None:
            return
        if s7.TASK_FINANCE_2 in plain and not self._finance_done:
            self._finance_done = True
            self.callback(OUTSIDER, f"/approve {s7.TASK_FINANCE_2}")
            self.callback(APPROVER, f"/approve {s7.TASK_FINANCE_2} 金额与政策已核对")
        elif s7.TASK_PAYMENT_2 in plain and not self._payment_done:
            self._payment_done = True
            self.callback(APPROVER, f"/reject {s7.TASK_PAYMENT_2} 交易号有误，改单重来")

    def close(self) -> None:
        pass

    def flush_pending_sends(self, *, timeout: float) -> bool:
        self.flush_calls.append(timeout)
        return True


class PassiveRoom(ScriptedRefundRoom):
    def send(self, plain: str, html: str) -> None:
        self.sent.append((plain, html))


class FinanceOnlyRoom(ScriptedRefundRoom):
    def send(self, plain: str, html: str) -> None:
        self.sent.append((plain, html))
        if ("HumanApprovalRequired" in plain and s7.TASK_FINANCE_2 in plain
                and self.callback is not None and not self._finance_done):
            self._finance_done = True
            self.callback(APPROVER, f"/approve {s7.TASK_FINANCE_2}")


def test_refund_finish_waits_until_slow_transition_mirror_is_quiescent():
    """证据入口不能沿用普通 stop 的 1s 尽力而为语义后立刻写 audit。"""
    completed = threading.Event()
    entered = threading.Event()

    class OneTransitionStore:
        def list_event_log(self, plan_id):
            return [{
                "seq": 1,
                "event_type": "StateTransition",
                "plan_id": plan_id,
                "task_id": "task-slow-tail",
                "from_state": "BLOCKED",
                "to_state": "FAILED",
                "reason": "slow tail",
                "detail": {},
                "created_at": "2026-09-01T00:00:00Z",
                "event_id": "evt-slow-tail",
                "trace_id": "trace-slow-tail",
            }]

        def get_task(self, task_id):
            return {"attempt": 1}

    class SlowChannel:
        def send(self, plain, html):
            entered.set()
            time.sleep(1.2)
            completed.set()

    class NoopBus:
        def drain(self):
            return 0

    driver = room_demo.RefundRoomDecisionDriver(
        store=OneTransitionStore(), bus=NoopBus(), channel=SlowChannel(),
        config=_live_config(), timeout=1.0)
    mirror = TransitionMirror(driver.store, "plan-slow-tail", driver.channel, interval=0.01)
    driver._mirror = mirror
    mirror.start()
    assert entered.wait(1.0), "慢发送没有进入，测试未命中竞态"

    driver.finish()
    assert completed.is_set(), "finish 在镜像线程仍发送时就返回，audit 会抢跑"


def _live_config() -> MatrixBusConfig:
    return MatrixBusConfig(
        homeserver="https://matrix.example.org",
        user="@maos-bot:example.org",
        token="sentinel-not-real",
        room_id="!refund:example.org",
        approvers=frozenset({APPROVER}),
    )


def test_s7b_decision_hook_owns_business_record_and_task_transition(capsys):
    """注入点必须拿到真实审批人，并仍走场景自己的业务收口顺序。"""
    out = s7.drive()
    seen: list[tuple[str, bool]] = []

    def room_decision(task: dict, expected_approved: bool, apply_decision) -> None:
        seen.append((task["task_id"], expected_approved))
        note = "房间批准" if expected_approved else "房间驳回"
        apply_decision(expected_approved, APPROVER, note)

        if task["task_id"] == s7.TASK_FINANCE_2:
            rows = C.approvals_of(
                out["store"], tenant_id=s7.TENANT_ID, case_id=s7.CASE_ID_2)
            assert rows and rows[-1]["approver"] == APPROVER

    try:
        result = s7.drive_human_exit(
            store=out["store"], bus=out["bus"], cp=out["cp"], gate=out["gate"],
            hq=out["hq"], decision_hook=room_decision)

        assert seen == [
            (s7.TASK_FINANCE_2, True),
            (s7.TASK_PAYMENT_2, False),
        ]
        assert out["store"].get_task(s7.TASK_FINANCE_2)["state"] == TaskState.DONE
        assert out["store"].get_task(s7.TASK_PAYMENT_2)["state"] == TaskState.FAILED
        assert out["store"].get_plan(result["plan_id"])["state"] == PlanState.FAILED

        approval_rows = objects.query(
            out["store"],
            "SELECT * FROM approval_record WHERE tenant_id=? AND case_id=?",
            (s7.TENANT_ID, s7.CASE_ID_2),
        )
        assert approval_rows and {row["approver"] for row in approval_rows} == {APPROVER}
        assert "[9] 主管处置: 房间驳回" in capsys.readouterr().out
    finally:
        C.reset_gateways()


def test_s7b_refuses_to_call_payment_hook_when_task_is_not_pending():
    """第二个审批点也必须先由人工队列证明仍在 BLOCKED。"""
    out = s7.drive()

    class MissingSecondPending:
        def __init__(self, real):
            self.real = real
            self.calls = 0

        def pending(self, plan_id):
            self.calls += 1
            if self.calls == 1:
                return self.real.pending(plan_id)
            return []

        def decide(self, *args, **kwargs):
            return self.real.decide(*args, **kwargs)

    fake_hq = MissingSecondPending(out["hq"])

    def auto_decide(task, expected_approved, apply_decision):
        apply_decision(expected_approved, APPROVER, "room decision")

    try:
        with pytest.raises(AssertionError, match="付款任务应停在 BLOCKED"):
            s7.drive_human_exit(
                store=out["store"], bus=out["bus"], cp=out["cp"], gate=out["gate"],
                hq=fake_hq, decision_hook=auto_decide)
    finally:
        C.reset_gateways()


def test_room_commands_drive_s7b_and_mirror_transitions_with_denial_audit():
    """退款链只由房间命令离开 BLOCKED，且越权与迁移都留在同一份库证据里。"""
    out = s7.drive()
    channel = ScriptedRefundRoom()
    driver = room_demo.RefundRoomDecisionDriver(
        store=out["store"], bus=out["bus"], channel=channel,
        config=_live_config(), timeout=1.0)
    channel.listen(driver.on_message)

    try:
        result = s7.drive_human_exit(
            store=out["store"], bus=out["bus"], cp=out["cp"], gate=out["gate"],
            hq=out["hq"], decision_hook=driver)
        driver.finish()

        denied = [
            row for row in out["store"].list_event_log(result["plan_id"])
            if row["event_type"] == EVENT_APPROVAL_DENIED
        ]
        assert len(denied) == 1
        assert denied[0]["task_id"] == s7.TASK_FINANCE_2
        assert denied[0]["detail"]["sender"] == OUTSIDER

        assert out["store"].get_task(s7.TASK_FINANCE_2)["state"] == TaskState.DONE
        assert out["store"].get_task(s7.TASK_PAYMENT_2)["state"] == TaskState.FAILED
        assert out["store"].get_plan(result["plan_id"])["state"] == PlanState.FAILED

        room_text = "\n".join(plain for plain, _ in channel.sent)
        assert f"[{s7.TASK_FINANCE_2}] StateTransition → BLOCKED → DONE" in room_text
        assert f"[{s7.TASK_PAYMENT_2}] StateTransition → BLOCKED → FAILED" in room_text
        assert "PlanTransition → RUNNING → FAILED" in room_text
        assert f"无审批权限：{OUTSIDER}" in room_text
        assert channel.flush_calls == [45.0]
        finance_card = next(
            plain for plain, _ in channel.sent
            if "HumanApprovalRequired" in plain and s7.TASK_FINANCE_2 in plain)
        payment_card = next(
            plain for plain, _ in channel.sent
            if "HumanApprovalRequired" in plain and s7.TASK_PAYMENT_2 in plain)
        assert f"/approve {s7.TASK_FINANCE_2}" in finance_card
        assert f"/reject {s7.TASK_FINANCE_2}" not in finance_card
        assert "名单外账号" in finance_card and "无审批权限" in finance_card
        assert f"/reject {s7.TASK_PAYMENT_2}" in payment_card
        assert f"/approve {s7.TASK_PAYMENT_2}" not in payment_card
        assert "原因，必填" in payment_card
    finally:
        driver.finish()
        C.reset_gateways()


def test_room_replies_are_not_sent_from_inside_listener_callback():
    """nio 回调所在事件循环不能同步等待同一循环的 room_send。"""

    class ThreadRecordingRoom(ScriptedRefundRoom):
        def __init__(self):
            super().__init__()
            self.callback_thread_ids: list[int] = []
            self.reply_thread_ids: list[int] = []

        def _deliver(self, sender, command):
            self.callback_thread_ids.append(threading.get_ident())
            self.callback(sender, command)

        def send(self, plain: str, html: str) -> None:
            self.sent.append((plain, html))
            if plain.startswith(("无审批权限：", "已批准 ", "已驳回 ")):
                self.reply_thread_ids.append(threading.get_ident())
                return
            if "HumanApprovalRequired" not in plain or self.callback is None:
                return
            if s7.TASK_FINANCE_2 in plain and not self._finance_done:
                self._finance_done = True
                self._deliver(OUTSIDER, f"/approve {s7.TASK_FINANCE_2}")
                self._deliver(APPROVER, f"/approve {s7.TASK_FINANCE_2} 金额与政策已核对")
            elif s7.TASK_PAYMENT_2 in plain and not self._payment_done:
                self._payment_done = True
                self._deliver(APPROVER, f"/reject {s7.TASK_PAYMENT_2} 交易号有误，改单重来")

    out = s7.drive()
    channel = ThreadRecordingRoom()
    driver = room_demo.RefundRoomDecisionDriver(
        store=out["store"], bus=out["bus"], channel=channel,
        config=_live_config(), timeout=1.0)
    channel.listen(driver.on_message)
    try:
        s7.drive_human_exit(
            store=out["store"], bus=out["bus"], cp=out["cp"], gate=out["gate"],
            hq=out["hq"], decision_hook=driver)
        driver.finish()
        assert channel.reply_thread_ids
        assert set(channel.reply_thread_ids).isdisjoint(channel.callback_thread_ids)
    finally:
        driver.finish()
        C.reset_gateways()


def test_inflight_room_decision_settles_before_timeout_is_reported():
    """已进入业务落库的回调不能在 CLI 报超时后继续改状态。"""

    class Store:
        def __init__(self):
            self.task = {
                "task_id": "task-race", "plan_id": "plan-race", "trace_id": "trace-race",
                "title": "race", "state": TaskState.BLOCKED, "risk_level": "M",
                "effect_risk": "H", "attempt": 1, "acceptance": [], "inputs": {},
            }

        def get_task(self, task_id):
            return dict(self.task) if task_id == "task-race" else None

        def list_event_log(self, plan_id):
            return []

    class Bus:
        def drain(self):
            return None

    class Room:
        def __init__(self):
            self.callback = None
            self.worker = None

        def send(self, plain, html):
            if "HumanApprovalRequired" in plain and self.worker is None:
                self.worker = threading.Thread(
                    target=lambda: self.callback(APPROVER, "/approve task-race"), daemon=True)
                self.worker.start()

    store, room = Store(), Room()
    entered = threading.Event()

    def apply_decision(approved, operator, note):
        entered.set()
        time.sleep(0.05)
        store.task["state"] = TaskState.DONE

    driver = room_demo.RefundRoomDecisionDriver(
        store=store, bus=Bus(), channel=room, config=_live_config(), timeout=0.01)
    room.callback = driver.on_message

    driver(store.task, True, apply_decision)
    driver.finish()
    assert entered.is_set()
    assert store.task["state"] == TaskState.DONE


def test_s7b_finance_stays_blocked_without_a_room_command(capsys):
    out = s7.drive()
    channel = PassiveRoom()
    driver = room_demo.RefundRoomDecisionDriver(
        store=out["store"], bus=out["bus"], channel=channel,
        config=_live_config(), timeout=0.01)
    channel.listen(driver.on_message)

    try:
        with pytest.raises(room_demo.RefundRoomDecisionTimeout):
            s7.drive_human_exit(
                store=out["store"], bus=out["bus"], cp=out["cp"], gate=out["gate"],
                hq=out["hq"], decision_hook=driver)

        assert out["store"].get_task(s7.TASK_FINANCE_2)["state"] == TaskState.BLOCKED
        assert C.approvals_of(
            out["store"], tenant_id=s7.TENANT_ID, case_id=s7.CASE_ID_2) == []
        assert out["store"].get_task(s7.TASK_PAYMENT_2)["state"] == TaskState.PENDING
        assert "先由名单外账号发送" in capsys.readouterr().out
    finally:
        driver.finish()
        C.reset_gateways()


def test_payment_is_not_auto_rejected_after_only_finance_room_command(capsys):
    out = s7.drive()
    channel = FinanceOnlyRoom()
    driver = room_demo.RefundRoomDecisionDriver(
        store=out["store"], bus=out["bus"], channel=channel,
        config=_live_config(), timeout=0.01)
    channel.listen(driver.on_message)

    try:
        with pytest.raises(room_demo.RefundRoomDecisionTimeout):
            s7.drive_human_exit(
                store=out["store"], bus=out["bus"], cp=out["cp"], gate=out["gate"],
                hq=out["hq"], decision_hook=driver)

        assert out["store"].get_task(s7.TASK_FINANCE_2)["state"] == TaskState.DONE
        assert out["store"].get_task(s7.TASK_PAYMENT_2)["state"] == TaskState.BLOCKED
        payment = out["store"].get_task(s7.TASK_PAYMENT_2)
        assert out["store"].get_plan(payment["plan_id"])["state"] != PlanState.FAILED
        assert "<原因，必填>" in capsys.readouterr().out
    finally:
        driver.finish()
        C.reset_gateways()


def test_payment_reject_without_reason_is_refused_before_state_change():
    class EmptyReasonRoom(ScriptedRefundRoom):
        def send(self, plain: str, html: str) -> None:
            self.sent.append((plain, html))
            if "HumanApprovalRequired" not in plain or self.callback is None:
                return
            if s7.TASK_FINANCE_2 in plain and not self._finance_done:
                self._finance_done = True
                self.callback(APPROVER, f"/approve {s7.TASK_FINANCE_2}")
            elif s7.TASK_PAYMENT_2 in plain and not self._payment_done:
                self._payment_done = True
                self.callback(APPROVER, f"/reject {s7.TASK_PAYMENT_2}")

    out = s7.drive()
    channel = EmptyReasonRoom()
    driver = room_demo.RefundRoomDecisionDriver(
        store=out["store"], bus=out["bus"], channel=channel,
        config=_live_config(), timeout=0.02)
    channel.listen(driver.on_message)
    try:
        with pytest.raises(room_demo.RefundRoomDecisionTimeout):
            s7.drive_human_exit(
                store=out["store"], bus=out["bus"], cp=out["cp"], gate=out["gate"],
                hq=out["hq"], decision_hook=driver)
        assert out["store"].get_task(s7.TASK_PAYMENT_2)["state"] == TaskState.BLOCKED
        assert out["store"].get_plan(
            out["store"].get_task(s7.TASK_PAYMENT_2)["plan_id"])["state"] != PlanState.FAILED
        assert any("必须携带处置原因" in plain for plain, _ in channel.sent)
    finally:
        driver.finish()
        C.reset_gateways()


def test_duplicate_authorized_command_is_consumed_only_once():
    class DuplicateFinanceRoom(ScriptedRefundRoom):
        def send(self, plain: str, html: str) -> None:
            self.sent.append((plain, html))
            if "HumanApprovalRequired" not in plain or self.callback is None:
                return
            if s7.TASK_FINANCE_2 in plain and not self._finance_done:
                self._finance_done = True
                command = f"/approve {s7.TASK_FINANCE_2} 金额已核对"
                self.callback(APPROVER, command)
                self.callback(APPROVER, command)
            elif s7.TASK_PAYMENT_2 in plain and not self._payment_done:
                self._payment_done = True
                self.callback(APPROVER, f"/reject {s7.TASK_PAYMENT_2} 交易号有误")

    out = s7.drive()
    channel = DuplicateFinanceRoom()
    driver = room_demo.RefundRoomDecisionDriver(
        store=out["store"], bus=out["bus"], channel=channel,
        config=_live_config(), timeout=1.0)
    channel.listen(driver.on_message)
    try:
        s7.drive_human_exit(
            store=out["store"], bus=out["bus"], cp=out["cp"], gate=out["gate"],
            hq=out["hq"], decision_hook=driver)
        driver.finish()
        rows = objects.query(
            out["store"],
            "SELECT decision FROM approval_record WHERE tenant_id=? AND case_id=?",
            (s7.TENANT_ID, s7.CASE_ID_2),
        )
        assert sorted(row["decision"] for row in rows) == ["approved", "rejected"]
        assert len(driver.accepted_room_decisions) == 2
    finally:
        driver.finish()
        C.reset_gateways()


def test_refund_s7b_cli_is_explicit_and_passes_audit_path(monkeypatch, tmp_path):
    called = {}
    audit = tmp_path / "refund-s7b-audit.json"

    def fake_refund_demo(*, timeout: float, audit_out: str | None) -> int:
        called.update(timeout=timeout, audit_out=audit_out)
        return room_demo.EXIT_OK

    monkeypatch.setattr(room_demo, "run_refund_demo", fake_refund_demo, raising=False)

    assert room_demo.main([
        "--case", "refund-s7b", "--timeout", "12", "--audit-out", str(audit),
    ]) == room_demo.EXIT_OK
    assert called == {"timeout": 12.0, "audit_out": str(audit)}


def test_refund_runner_uses_real_s7b_runtime_and_writes_atomic_audit(
        monkeypatch, tmp_path):
    real_drive = s7.drive
    channel = ScriptedRefundRoom()
    audit = tmp_path / "refund-s7b-audit.json"
    token_sentinel = "MATRIX_TOKEN_SENTINEL_P8"
    real_fsync = room_demo.os.fsync
    fsync_calls: list[int] = []

    def tracking_fsync(fd: int) -> None:
        fsync_calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(s7, "drive", lambda *, matrix: real_drive(matrix=False))
    monkeypatch.setattr(room_demo, "bus_channel", lambda bus: channel)
    monkeypatch.setattr(
        room_demo.MatrixBusConfig, "from_env",
        classmethod(lambda cls, env=None: _live_config()),
    )
    monkeypatch.setenv("MATRIX_TOKEN", token_sentinel)
    monkeypatch.setattr(room_demo.os, "fsync", tracking_fsync)

    try:
        assert room_demo.run_refund_demo(
            timeout=1.0, audit_out=str(audit)) == room_demo.EXIT_OK
        body = audit.read_text(encoding="utf-8")

        assert body.startswith("# generated at ")
        assert s7.TASK_FINANCE_2 in body and '"state": "DONE"' in body
        assert s7.TASK_PAYMENT_2 in body and '"state": "FAILED"' in body
        assert EVENT_APPROVAL_DENIED in body and OUTSIDER in body
        assert APPROVER in body
        assert token_sentinel not in body
        assert not list(tmp_path.glob("*.tmp")), "原子写失败后不得遗留半份文件"
        payload = json.loads(body.split("\n", 1)[1])
        assert [row["task_id"] for row in payload["accepted_room_decisions"]] == [
            s7.TASK_FINANCE_2, s7.TASK_PAYMENT_2]
        assert [row["approved"] for row in payload["accepted_room_decisions"]] == [True, False]
        assert [row["decision"] for row in payload["approval_records"]] == [
            "approved", "rejected"]
        assert len(fsync_calls) >= 2, "临时 inode 与父目录项都必须 fsync"
    finally:
        C.reset_gateways()


def test_refund_runner_rejects_non_failed_plan_and_writes_no_audit(monkeypatch, tmp_path):
    real_drive = s7.drive
    real_human_exit = s7.drive_human_exit
    channel = ScriptedRefundRoom()
    audit = tmp_path / "invalid-audit.json"

    monkeypatch.setattr(s7, "drive", lambda *, matrix: real_drive(matrix=False))

    def tampered_human_exit(**kwargs):
        result = real_human_exit(**kwargs)
        kwargs["store"].update_plan_state(result["plan_id"], PlanState.RUNNING)
        return result

    monkeypatch.setattr(s7, "drive_human_exit", tampered_human_exit)
    monkeypatch.setattr(room_demo, "bus_channel", lambda bus: channel)
    monkeypatch.setattr(
        room_demo.MatrixBusConfig, "from_env",
        classmethod(lambda cls, env=None: _live_config()),
    )

    try:
        assert room_demo.run_refund_demo(
            timeout=1.0, audit_out=str(audit)) == room_demo.EXIT_PRECONDITION
        assert not audit.exists()
    finally:
        C.reset_gateways()


def test_refund_runner_writes_no_audit_when_background_send_flush_fails(
        monkeypatch, tmp_path):
    """房间尾消息最终失败时，库虽终态也不能发布成功审计。"""
    real_drive = s7.drive

    class FailedFlushRoom(ScriptedRefundRoom):
        def flush_pending_sends(self, *, timeout: float) -> bool:
            self.flush_calls.append(timeout)
            return False

    channel = FailedFlushRoom()
    audit = tmp_path / "must-not-exist.json"
    monkeypatch.setattr(s7, "drive", lambda *, matrix: real_drive(matrix=False))
    monkeypatch.setattr(room_demo, "bus_channel", lambda bus: channel)
    monkeypatch.setattr(
        room_demo.MatrixBusConfig, "from_env",
        classmethod(lambda cls, env=None: _live_config()),
    )

    try:
        assert room_demo.run_refund_demo(
            timeout=1.0, audit_out=str(audit)) == room_demo.EXIT_PRECONDITION
        assert not audit.exists()
        assert channel.flush_calls == [45.0]
    finally:
        C.reset_gateways()


def test_refund_runner_refuses_to_overwrite_existing_audit(monkeypatch, tmp_path):
    audit = tmp_path / "old-audit.json"
    audit.write_text("old evidence\n", encoding="utf-8")
    called = False

    def should_not_start(*, matrix):
        nonlocal called
        called = True
        raise AssertionError("must reject before starting the business run")

    monkeypatch.setattr(s7, "drive", should_not_start)

    assert room_demo.run_refund_demo(
        timeout=1.0, audit_out=str(audit)) == room_demo.EXIT_PRECONDITION
    assert called is False
    assert audit.read_text(encoding="utf-8") == "old evidence\n"
