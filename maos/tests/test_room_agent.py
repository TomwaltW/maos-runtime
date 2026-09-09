"""房间常驻监听器行为测试 —— T67 的可执行版本。**一行网络都不走。**

这些断言守的是四件「出事时症状离原因最远」的东西：

1. **R1：自然语言不能授权**。喂一句「同意」，``HumanApprovalQueue.decide()``
   必须**零调用**。这是本文件里唯一一条安全断言 —— 它变红意味着一次关键词误判
   就能批掉一笔生产变更（与铁律 8 同源：权威动作不能由推断产生）。
   钉子是 :func:`test_natural_approve_never_decides`。
2. **回声不许自激**。机器人自己发的回执又被自己听见 = 自问自答死循环，
   而房间里看到的是刷屏，看不出原因在回声过滤。
3. **重复投递只算一次**。Matrix 的 sync 重连会重放事件；审批是不可逆动作，
   一条「同意」被处理三遍就是三次误判机会。
4. **发送失败不许掀掉监听**。Synapse 的 429 是常态不是故障，常驻进程尤其容易撞；
   一条发不出去就退出的监听器，等于房间里又没人在听了。

假通道全程代替房间：``FakeChannel`` 记下每一条 ``send``，并把 ``listen`` 注册的回调
留在手边，测试自己扮演 Matrix 投递消息。
"""

from __future__ import annotations

import logging
import signal
import threading
import time

import pytest

import hiclaw.room_agent as room_agent
from hiclaw.matrix_bus import MatrixBusConfig, RoomApprovalBridge, RoomSendTimeout
from hiclaw.room_agent import (ACTION_APPROVE, ACTION_REJECT, ACTION_STATUS,
                               ACTION_UNKNOWN, CONF_HIGH, CONF_LOW, GOODBYE, GREETING,
                               KIND_CONFIRM, KIND_DENIED, KIND_DONE, KIND_IGNORED,
                               DispatchResult, Intent, RoomAgent, _KeywordParser,
                               _StubDispatcher)

APPROVER = "@boss:maos.local"
OUTSIDER = "@intern:maos.local"
BOT = "@maos-bot:maos.local"
TASK_ID = "task_997ca4541e66"


# --------------------------------------------------------------------------
# 替身
# --------------------------------------------------------------------------
class FakeChannel:
    """假房间通道。形状照 ``MirrorChannel``：send / listen / close。"""

    def __init__(self, *, fail_times: int = 0, exc: Exception | None = None) -> None:
        self.sent: list[str] = []
        self.listener = None
        self.closed = False
        self.attempts = 0
        self._fail_times = fail_times
        self._exc = exc or RuntimeError("房间发送失败：模拟故障")

    def send(self, plain: str, html: str) -> None:
        self.attempts += 1
        if self.attempts <= self._fail_times:
            raise self._exc
        self.sent.append(plain)

    def listen(self, on_message) -> None:               # noqa: ANN001 —— 形状对齐即可
        self.listener = on_message

    def close(self) -> None:
        self.closed = True


class SpyQueue:
    """审批队列替身。**它存在的全部意义是数 decide() 被调了几次。**

    用替身而不是真 ``HumanApprovalQueue``：R1 要断言的是「一次都没调」，
    而真队列调不动时会因为库里没这条任务而抛异常 —— 那样测试即使在
    「自然语言真的去调了 decide」的实现下也照样能过，钉子就成了摆设。
    """

    def __init__(self) -> None:
        self.store = None
        self.calls: list[tuple] = []

    def decide(self, task_id: str, approved: bool, operator: str, note: str = "") -> None:
        self.calls.append((task_id, approved, operator, note))


class FakeStore:
    def __init__(self, tasks: dict | None = None) -> None:
        self._tasks = tasks or {}

    def get_task(self, task_id: str):
        return self._tasks.get(task_id)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """名单只认测试自己给的那一份。

    ``RoomApprovalBridge._effective_approvers`` 与 ``RoomAgent._approvers`` 都会
    **现读一次** ``MAOS_APPROVERS``（T28 的动态治理），不清掉的话，
    本机 shell 里配了名单就会让 `越权` 那几条用例在别人机器上莫名其妙地过/挂。
    """
    monkeypatch.delenv("MAOS_APPROVERS", raising=False)


def make_agent(*, channel=None, queue=None, known=(TASK_ID,), store=None,
               approvers=(APPROVER,), self_id=BOT, **kwargs) -> tuple[RoomAgent, FakeChannel, SpyQueue]:
    channel = channel or FakeChannel()
    queue = queue or SpyQueue()
    config = MatrixBusConfig(user=self_id, approvers=frozenset(approvers))
    agent = RoomAgent(
        channel=channel,
        bridge=RoomApprovalBridge(queue, config, channel=None),
        store=store,
        config=config,
        self_id=self_id,
        known_task_ids=lambda: list(known),
        sleeper=lambda _delay: None,        # 测试不真睡
        **kwargs,
    )
    return agent, channel, queue


# --------------------------------------------------------------------------
# 1. 数据契约（与 review/nl-contracts.md §1 逐字对齐）
# --------------------------------------------------------------------------
def test_intent_fields_match_contract():
    intent = Intent(action=ACTION_APPROVE, task_id=TASK_ID, reason="r",
                    confidence=CONF_HIGH, raw_text="批准", detail={"k": "v"})
    assert (intent.action, intent.task_id, intent.reason) == (ACTION_APPROVE, TASK_ID, "r")
    assert (intent.confidence, intent.raw_text, intent.detail) == (CONF_HIGH, "批准", {"k": "v"})
    assert Intent(action=ACTION_UNKNOWN).confidence == CONF_LOW      # 缺省保守取低


def test_intent_rejects_unknown_action():
    with pytest.raises(ValueError):
        Intent(action="approve_all")


def test_dispatch_result_rejects_unknown_kind():
    with pytest.raises(ValueError):
        DispatchResult(kind="maybe", text="")


def test_no_import_of_other_tracks():
    """本轨不许 import T66 / T68 的面 —— 并行期它们可能还不存在，import 即红。

    按 ``ast`` 里的 import 节点判，不按字符串扫源码：``INTEGRATION-POINT`` 那两行
    注释**必须**写出目标模块名（整合时靠它 grep），字符串扫描会把指引本身念成违规。
    """
    import ast

    source = open(room_agent.__file__, encoding="utf-8").read()
    modules = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.add(node.module or "")
    assert not [m for m in modules if m.startswith("maos.nlu")], modules
    assert not [m for m in modules if "intent_dispatch" in m], modules


def test_integration_points_present():
    """两处替换点必须留注释，整合时编排侧 grep 它（契约 §5）。"""
    source = open(room_agent.__file__, encoding="utf-8").read()
    marks = [line for line in source.splitlines() if "INTEGRATION-POINT" in line]
    assert len(marks) >= 2, marks
    assert any("_KeywordParser" in line for line in marks)
    assert any("_StubDispatcher" in line for line in marks)


# --------------------------------------------------------------------------
# 2. 兜底解析器
# --------------------------------------------------------------------------
@pytest.mark.parametrize("text", ["同意", "批准了", "可以过", "放行吧", "LGTM", "approve"])
def test_keyword_parser_approve(text):
    intent = _KeywordParser()(text, known_task_ids=[TASK_ID])
    assert intent.action == ACTION_APPROVE
    assert intent.task_id == TASK_ID


@pytest.mark.parametrize("text", ["不同意", "驳回吧", "这个先别过", "不行", "打回去"])
def test_keyword_parser_reject(text):
    """「不同意」里含「同意」—— 驳回词必须先匹配，否则确认话会引导人去打反了的命令。"""
    assert _KeywordParser()(text, known_task_ids=[TASK_ID]).action == ACTION_REJECT


def test_keyword_parser_confidence_high_only_when_id_written_out():
    high = _KeywordParser()(f"同意 {TASK_ID}", known_task_ids=[TASK_ID])
    low = _KeywordParser()("同意", known_task_ids=[TASK_ID])
    assert high.confidence == CONF_HIGH
    assert low.confidence == CONF_LOW           # 靠「全场只有一条」推断出来的，不许算高


def test_keyword_parser_unknown_when_task_id_not_in_known():
    """R2：编出来的 id 一律降 UNKNOWN。模型最擅长编格式完全正确的 id。"""
    intent = _KeywordParser()("同意 task_deadbeef", known_task_ids=[TASK_ID])
    assert intent.action == ACTION_UNKNOWN


def test_keyword_parser_ambiguous_when_multiple_pending():
    """两条在等人、又没写 id = 不知道要批哪一条。这不是「差一点」，是 UNKNOWN。"""
    intent = _KeywordParser()("同意", known_task_ids=[TASK_ID, "task_other"])
    assert intent.action == ACTION_UNKNOWN


def test_keyword_parser_reason_only_when_stated():
    """理由不许编：人说了才有。"""
    said = _KeywordParser()(f"驳回 {TASK_ID} 因为回滚脚本没写", known_task_ids=[TASK_ID])
    silent = _KeywordParser()(f"驳回 {TASK_ID}", known_task_ids=[TASK_ID])
    assert said.reason == "回滚脚本没写"
    assert silent.reason == ""


def test_keyword_parser_chitchat_is_unknown():
    assert _KeywordParser()("今天天气不错", known_task_ids=[TASK_ID]).action == ACTION_UNKNOWN
    assert _KeywordParser()("你们好", known_task_ids=[TASK_ID]).action == ACTION_UNKNOWN


def test_keyword_parser_status():
    assert _KeywordParser()("现在什么状态", known_task_ids=[TASK_ID]).action == ACTION_STATUS


# --------------------------------------------------------------------------
# 3. 🔴 R1 —— 自然语言只确认，不执行
# --------------------------------------------------------------------------
def test_natural_approve_never_decides():
    """🔴 **本文件最重要的一条**：喂人话「同意」，decide() 必须一次都没被调用。"""
    agent, channel, queue = make_agent()

    reply = agent.handle_message(APPROVER, "同意", event_id="$e1")

    assert queue.calls == []                     # ← 钉子：零调用
    assert reply == f"你是想批准 {TASK_ID} 吗？确认请发：/approve {TASK_ID}"
    assert channel.sent == [reply]


def test_natural_reject_never_decides():
    agent, channel, queue = make_agent()

    reply = agent.handle_message(APPROVER, "这个先别过", event_id="$e1")

    assert queue.calls == []
    assert reply == f"你是想驳回 {TASK_ID} 吗？确认请发：/reject {TASK_ID}"


def test_stub_dispatcher_has_no_path_to_decide():
    """结构性自证：派发器的**代码**里不存在通往 decide 的调用，读代码就能验。

    docstring 要剥掉：那里正解释着「这个类里没有通往 decide() 的路径」，
    连它一起扫就成了自己打自己。
    """
    import ast

    tree = ast.parse(__import__("inspect").getsource(_StubDispatcher).lstrip())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if (node.body and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)):
                node.body.pop(0)                        # 剥掉 docstring
    assert "decide" not in ast.unparse(tree)


def test_natural_approve_from_outsider_is_denied_not_confirmed():
    agent, channel, queue = make_agent()

    reply = agent.handle_message(OUTSIDER, "同意", event_id="$e1")

    assert queue.calls == []
    assert "无审批权限" in reply


# --------------------------------------------------------------------------
# 4. 显式指令路径 —— 唯一能让状态迁移的入口
# --------------------------------------------------------------------------
def test_explicit_command_goes_through_bridge():
    agent, channel, queue = make_agent()

    reply = agent.handle_message(APPROVER, f"/approve {TASK_ID}", event_id="$e1")

    assert queue.calls == [(TASK_ID, True, APPROVER, "")]
    assert reply == f"已批准 {TASK_ID}（操作人 {APPROVER}）"
    assert channel.sent == [reply]


def test_explicit_reject_carries_reason():
    agent, _channel, queue = make_agent()

    agent.handle_message(APPROVER, f"/reject {TASK_ID} 回滚脚本没写", event_id="$e1")

    assert queue.calls == [(TASK_ID, False, APPROVER, "回滚脚本没写")]


def test_explicit_command_from_outsider_never_decides():
    agent, _channel, queue = make_agent()

    reply = agent.handle_message(OUTSIDER, f"/approve {TASK_ID}", event_id="$e1")

    assert queue.calls == []
    assert "无审批权限" in reply


def test_explicit_command_ignores_known_task_ids():
    """显式指令不经 known_task_ids —— 它是人自己打出来的，不是推断出来的。"""
    agent, _channel, queue = make_agent(known=())

    agent.handle_message(APPROVER, f"/approve {TASK_ID}", event_id="$e1")

    assert queue.calls == [(TASK_ID, True, APPROVER, "")]


# --------------------------------------------------------------------------
# 5. 回声过滤
# --------------------------------------------------------------------------
def test_own_message_is_ignored():
    """自己发的回执又被自己听见 = 自问自答死循环。"""
    agent, channel, queue = make_agent()

    reply = agent.handle_message(BOT, f"/approve {TASK_ID}", event_id="$e1")

    assert reply == ""
    assert channel.sent == []
    assert queue.calls == []
    assert agent.handled == 0           # 连「处理过」都不算


def test_own_natural_message_is_ignored():
    agent, channel, _queue = make_agent()
    agent.handle_message(BOT, "同意", event_id="$e1")
    assert channel.sent == []


# --------------------------------------------------------------------------
# 6. 去重
# --------------------------------------------------------------------------
def test_same_event_id_processed_once():
    """sync 重放同一条 —— 审批是不可逆动作，处理两遍就是两次误判机会。"""
    agent, channel, queue = make_agent()

    first = agent.handle_message(APPROVER, f"/approve {TASK_ID}", event_id="$dup")
    second = agent.handle_message(APPROVER, f"/approve {TASK_ID}", event_id="$dup")

    assert first != ""
    assert second == ""
    assert queue.calls == [(TASK_ID, True, APPROVER, "")]
    assert channel.sent == [first]
    assert agent.handled == 1


def test_different_event_ids_both_processed():
    agent, _channel, queue = make_agent()

    agent.handle_message(APPROVER, f"/approve {TASK_ID}", event_id="$a")
    agent.handle_message(APPROVER, f"/approve {TASK_ID}", event_id="$b")

    assert len(queue.calls) == 2


def test_dedup_without_event_id_uses_fingerprint_window():
    """真通道给不出 event_id（listen 的回调只有 sender/body），退到指纹 + 时间窗。"""
    agent, _channel, queue = make_agent(dedup_window=60.0)

    agent.handle_message(APPROVER, f"/approve {TASK_ID}")
    agent.handle_message(APPROVER, f"/approve {TASK_ID}")

    assert len(queue.calls) == 1


def test_fingerprint_dedup_expires_so_a_second_real_utterance_lands():
    """窗口不能省：隔一会儿再说一遍是**合法的第二次发言**，永久去重会把它吃掉。"""
    agent, _channel, queue = make_agent(dedup_window=0.0)

    agent.handle_message(APPROVER, f"/approve {TASK_ID}")
    time.sleep(0.01)
    agent.handle_message(APPROVER, f"/approve {TASK_ID}")

    assert len(queue.calls) == 2


def test_dedup_cache_is_bounded():
    agent, _channel, _queue = make_agent(dedup_max=8)
    for i in range(50):
        agent.handle_message(APPROVER, "今天天气不错", event_id=f"$e{i}")
    assert len(agent._seen) <= 8


# --------------------------------------------------------------------------
# 7. 听不懂 —— 静默
# --------------------------------------------------------------------------
def test_chitchat_is_silent():
    """房间里的闲聊不该收到机器人的用法提示（口径同 RoomApprovalBridge）。

    选静默而不是回一句「听不懂」：常驻进程一直在听，逢话必回就是刷屏，
    而刷屏会让真正的审批回执淹掉。见 docs/DECISIONS.md ## task-T67。
    """
    agent, channel, queue = make_agent()

    reply = agent.handle_message(APPROVER, "今天天气不错", event_id="$e1")

    assert reply == ""
    assert channel.sent == []
    assert queue.calls == []
    assert agent.handled == 1           # 处理过了，只是没话说


def test_unparsable_command_gets_usage_not_silence():
    """认得出是冲审批来的、但参数不合法 -> 回用法。这条不该被静默吃掉。"""
    agent, channel, queue = make_agent()

    reply = agent.handle_message(APPROVER, "/approve", event_id="$e1")

    assert "用法" in reply
    assert queue.calls == []


# --------------------------------------------------------------------------
# 8. 发送失败不许掀掉监听
# --------------------------------------------------------------------------
def test_send_failure_does_not_crash_and_warns(caplog):
    agent, channel, queue = make_agent(channel=FakeChannel(fail_times=99))

    with caplog.at_level(logging.WARNING, logger="maos.room_agent"):
        reply = agent.handle_message(APPROVER, f"/approve {TASK_ID}", event_id="$e1")

    assert reply != ""                      # 判定照样生效
    assert queue.calls == [(TASK_ID, True, APPROVER, "")]
    assert not agent.stopped                # 没退出
    assert any("房间回话失败" in r.message for r in caplog.records)


def test_send_retries_with_backoff_then_succeeds():
    agent, channel, _queue = make_agent(channel=FakeChannel(fail_times=1))
    delays: list[float] = []
    agent._sleep = delays.append

    agent.handle_message(APPROVER, "同意", event_id="$e1")

    assert channel.attempts == 2
    assert len(channel.sent) == 1
    assert delays == [0.5]


def test_room_send_timeout_is_never_retried():
    """429 退避导致的超时是**虚警**：消息多半已送达，重发只会再撞一次限流。"""
    channel = FakeChannel(fail_times=99, exc=RoomSendTimeout("30s 内没等到房间回执"))
    agent, channel, _queue = make_agent(channel=channel)

    agent.handle_message(APPROVER, "同意", event_id="$e1")

    assert channel.attempts == 1            # ← 一次，不是三次


def test_handler_exception_does_not_escape():
    """异常逃出回调会掀掉 nio 的 sync 循环 —— 一条打错的消息不该让房间失去监听。"""
    def boom(text, *, known_task_ids):
        raise RuntimeError("解析器炸了")

    agent, channel, _queue = make_agent(parser=boom)

    assert agent.handle_message(APPROVER, "同意", event_id="$e1") == ""
    agent.on_room_message(APPROVER, "同意", "$e2")      # 回调层也不许抛
    assert not agent.stopped


# --------------------------------------------------------------------------
# 9. 回调形状 —— 真通道两参、假通道三参
# --------------------------------------------------------------------------
def test_on_room_message_accepts_two_and_three_args():
    agent, _channel, queue = make_agent()

    agent.on_room_message(APPROVER, f"/approve {TASK_ID}")              # 真通道形状
    agent.on_room_message(APPROVER, f"/approve {TASK_ID}", "$e2")       # 带 event_id

    assert len(queue.calls) == 2


# --------------------------------------------------------------------------
# 10. 生命周期
# --------------------------------------------------------------------------
def test_run_once_exits_after_one_message():
    agent, channel, queue = make_agent()
    done = threading.Event()
    result: list[int] = []

    def _run():
        result.append(agent.run(once=True, timeout=5.0, poll=0.01))
        done.set()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    for _ in range(200):                    # 等 listen 挂上
        if channel.listener is not None:
            break
        time.sleep(0.01)
    channel.listener(APPROVER, f"/approve {TASK_ID}", "$e1")

    assert done.wait(5.0), "--once 没有在处理完一条后退出"
    thread.join(5.0)
    assert result == [0]
    assert queue.calls == [(TASK_ID, True, APPROVER, "")]
    assert channel.sent[0] == GREETING
    assert channel.sent[-1] == GOODBYE      # 下线必须出声


def test_run_says_goodbye_on_stop():
    """下线要出声：人分不出「在听但不理我」和「根本没人在听」。"""
    agent, channel, _queue = make_agent()
    threading.Timer(0.05, agent.stop).start()

    assert agent.run(timeout=5.0, poll=0.01) == 0
    assert channel.sent[-1] == GOODBYE


def test_run_stops_on_timeout():
    agent, channel, _queue = make_agent()
    assert agent.run(timeout=0.05, poll=0.01, greet=False) == 0
    assert channel.sent == [GOODBYE]


def test_run_survives_channel_without_listen(caplog):
    class MuteChannel(FakeChannel):
        listen = None

    agent, _channel, _queue = make_agent(channel=MuteChannel())
    with caplog.at_level(logging.WARNING, logger="maos.room_agent"):
        assert agent.run(timeout=0.05, poll=0.01, greet=False) == 0
    assert any("listen" in r.message for r in caplog.records)


def test_signal_handler_requests_graceful_stop():
    """SIGINT / SIGTERM -> stop()，进程自己收口，exit=0。"""
    agent, channel, _queue = make_agent()
    original = signal.getsignal(signal.SIGTERM)
    try:
        agent._install_signal_handlers()
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)
        assert agent.stopped
    finally:
        signal.signal(signal.SIGTERM, original)
        signal.signal(signal.SIGINT, signal.default_int_handler)


def test_install_signal_handlers_off_main_thread_is_not_fatal():
    """监听器跑在工作线程里时 signal.signal 会抛 ValueError —— 不许因此起不来。"""
    agent, _channel, _queue = make_agent()
    errors: list[BaseException] = []

    def _try():
        try:
            agent._install_signal_handlers()
        except BaseException as exc:        # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=_try)
    thread.start()
    thread.join(5.0)
    assert errors == []


# --------------------------------------------------------------------------
# 11. 兜底派发器的 status 档
# --------------------------------------------------------------------------
def test_status_reports_state_from_store():
    store = FakeStore({TASK_ID: {"task_id": TASK_ID, "title": "变更生产环境配置",
                                 "state": "BLOCKED"}})
    agent, channel, queue = make_agent(store=store)

    reply = agent.handle_message(APPROVER, "现在什么状态", event_id="$e1")

    assert TASK_ID in reply and "BLOCKED" in reply
    assert queue.calls == []                # 查询不是动作


def test_status_without_store_says_so_instead_of_guessing():
    agent, _channel, _queue = make_agent(store=None)
    reply = agent.handle_message(APPROVER, "现在什么状态", event_id="$e1")
    assert "查不了" in reply


def test_dispatcher_kinds_are_within_contract():
    dispatcher = _StubDispatcher()
    kinds = {
        dispatcher(Intent(action=ACTION_UNKNOWN), sender=APPROVER,
                   approvers=[APPROVER]).kind,
        dispatcher(Intent(action=ACTION_APPROVE, task_id=TASK_ID), sender=APPROVER,
                   approvers=[APPROVER]).kind,
        dispatcher(Intent(action=ACTION_REJECT, task_id=TASK_ID), sender=OUTSIDER,
                   approvers=[APPROVER]).kind,
        dispatcher(Intent(action=ACTION_STATUS, task_id=TASK_ID),
                   store=FakeStore(), sender=APPROVER, approvers=[APPROVER]).kind,
    }
    assert kinds == {KIND_IGNORED, KIND_CONFIRM, KIND_DENIED, KIND_DONE}


# --------------------------------------------------------------------------
# 12. 降级 —— 没房间不许装成接通了
# --------------------------------------------------------------------------
def test_main_exits_non_zero_without_matrix_env(monkeypatch, capsys):
    """硬判据 4：MATRIX_* 没配时明确非 0 退出（``EXIT_NO_ROOM``），不许 exit=0。"""
    for name in ("MATRIX_HOMESERVER", "MATRIX_USER", "MATRIX_TOKEN", "MATRIX_ROOM_ID"):
        monkeypatch.delenv(name, raising=False)

    code = room_agent.main([])

    assert code == room_agent.EXIT_NO_ROOM
    assert "没进房间" in capsys.readouterr().err


def test_main_degraded_reads_stdin_when_allowed(monkeypatch, capsys):
    """``--allow-degraded`` 下改从 stdin 读，本地能验路由，且一行网络都不走。"""
    import io

    for name in ("MATRIX_HOMESERVER", "MATRIX_USER", "MATRIX_TOKEN", "MATRIX_ROOM_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(f"{APPROVER}|今天天气不错\n"))

    assert room_agent.main(["--allow-degraded", "--once"]) == room_agent.EXIT_OK
    out = capsys.readouterr().out
    assert GREETING in out and GOODBYE in out       # 两条路径的上下线口径必须一致
