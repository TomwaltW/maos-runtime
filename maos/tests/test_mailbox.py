"""收件箱（agent→agent 点对点通道）的行为契约。

三条贯穿全篇的取向：

1. **定向不是广播。** 第一组用例的重点不在「收得到」，在**第三方收不到** ——
   收得到那半句用现有的总线也能糊出来，收不到那半句才是这条通道存在的理由。
2. **信任边界在投递层，不在正文。** 伪造 `body["from_agent"]` 那条用例钉的是
   「发件人由投递层写死」；这条一旦松了，后面任何基于身份的判断都没有地基。
3. **证据链要认得这条通道。** span 树那两条用例把消息事件接进 `maos.obs.trace`，
   而「不带 plan_id 会掉进 stray_events」「取走即已读、重试时消息不再出现」这两个
   **已知代价**同样钉成断言 —— 已知代价写成断言，才不会变成将来某个人的意外发现。
4. **闸的判据用与被测常量无关的字面量钉。** 凭证那几条用例参数化在
   `EXPECTED_MARKERS` 上，不参数化在被测的 `SECRET_MARKERS` 上。后者有一个致命
   形态：把常量清空，parametrize 退化成零个用例，整套凭证测试跑出
   `0 failed` —— 这道安全闸的存在与否在测试面上不可分辨（实测过）。
"""

from __future__ import annotations

import ast
import inspect
import json
import textwrap
import threading

import pytest

from maos.core.mailbox import (
    AGENT_MESSAGE_EVENT,
    SECRET_MARKERS,
    SECURITY_EVENT,
    Mailbox,
    MailboxError,
    SecretLeakBlocked,
)
from maos.core.store import SqliteStore
from maos.obs.trace import KIND_EVENT, KIND_TASK, check_span_tree, export_trace, stray_events
from maos.runtime.gate import ReviewerGate
from maos.skills.invoker import _digest

PLAN_ID, TASK_ID, TRACE_ID = "plan_mbox", "task_mbox", "trace_mbox"

T0 = "2026-09-08T00:00:00+00:00"
T1 = "2026-09-08T00:00:01+00:00"
T2 = "2026-09-08T00:00:02+00:00"


def _store(path: str = ":memory:") -> SqliteStore:
    """一个带 plan + task 的库。span 树那两条用例要靠这两行才挂得上。"""
    store = SqliteStore(path)
    store.init_schema()
    store.insert_plan({"plan_id": PLAN_ID, "trace_id": TRACE_ID,
                       "goal": "收件箱夹具", "state": "PENDING"})
    store.insert_task({"task_id": TASK_ID, "plan_id": PLAN_ID, "trace_id": TRACE_ID,
                       "role": "coding", "title": "写代码", "state": "PENDING",
                       "attempt": 1, "depends_on": [], "inputs": {},
                       "acceptance": [], "findings": []})
    return store


def _rows(store: SqliteStore) -> list[dict]:
    cur = store._conn.execute("SELECT * FROM agent_message ORDER BY rowid")
    return [dict(r) for r in cur.fetchall()]


def _events(store: SqliteStore, etype: str, plan_id: str = PLAN_ID) -> list[dict]:
    return [e for e in store.list_event_log(plan_id) if e["event_type"] == etype]


# ---------------------------------------------------------------------------
# 1. 定向投递：收件方看得到，第三方看不到
# ---------------------------------------------------------------------------
def test_message_reaches_only_the_addressee():
    store = _store()
    box = Mailbox(store)
    box.send(from_agent="manager", to_agent="coder", kind="ask",
             body={"q": "这条 DAG 的第 3 步谁做"}, plan_id=PLAN_ID, task_id=TASK_ID,
             now_iso=T0)

    got = box.inbox("coder")
    assert [m["body"] for m in got] == [{"q": "这条 DAG 的第 3 步谁做"}]
    assert got[0]["from_agent"] == "manager"

    # 这一行才是这条通道存在的理由：不是订阅了就都能收到。
    assert box.inbox("reviewer") == []
    assert box.inbox("manager") == []


def test_inbox_is_ordered_by_created_at():
    store = _store()
    box = Mailbox(store)
    box.send(from_agent="manager", to_agent="coder", kind="inform", body="第二条",
             plan_id=PLAN_ID, now_iso=T1)
    box.send(from_agent="reviewer", to_agent="coder", kind="inform", body="第一条",
             plan_id=PLAN_ID, now_iso=T0)
    assert [m["body"] for m in box.inbox("coder")] == ["第一条", "第二条"]


def test_send_returns_msg_id_that_addresses_the_row():
    store = _store()
    box = Mailbox(store)
    msg_id = box.send(from_agent="manager", to_agent="coder", kind="handoff",
                      body={"n": 1}, plan_id=PLAN_ID, now_iso=T0)
    assert msg_id.startswith("msg_")
    assert [r["msg_id"] for r in _rows(store)] == [msg_id]


# ---------------------------------------------------------------------------
# 2. 已读语义：deliver_into 取走一次就不再进第二次上下文
# ---------------------------------------------------------------------------
def test_deliver_into_consumes_the_inbox():
    store = _store()
    box = Mailbox(store)
    box.send(from_agent="manager", to_agent="coder", kind="ask", body={"q": "?"},
             plan_id=PLAN_ID, now_iso=T0)

    first = box.deliver_into("coder", now_iso=T1)
    assert [m["body"] for m in first] == [{"q": "?"}]
    # 取走那一刻就已读：返回的 list 自己也得这么说，否则读它的人以为还没读过。
    assert first[0]["read_at"] == T1

    # 再取为空 —— 重复注入的症状是 Agent 反复回应同一条请求。
    assert box.deliver_into("coder", now_iso=T2) == []
    # 但历史查得到：已读不等于删除。
    assert len(box.inbox("coder", unread_only=False)) == 1


def test_mark_read_keeps_the_first_read_moment():
    store = _store()
    box = Mailbox(store)
    msg_id = box.send(from_agent="manager", to_agent="coder", kind="inform", body="x",
                      plan_id=PLAN_ID, now_iso=T0)
    assert box.mark_read([msg_id], to_agent="coder", now_iso=T1) == 1
    # 第二次标记不改 read_at：首次读取时刻才是有意义的那个。
    assert box.mark_read([msg_id], to_agent="coder", now_iso=T2) == 0
    assert _rows(store)[0]["read_at"] == T1


def test_mark_read_cannot_touch_another_agents_message():
    """A 标不掉 B 的消息。

    `msg_id` 不是秘密 —— 它就写在 `AgentMessage` 事件的 detail 里，谁读审计谁都拿得到。
    UPDATE 不带 `to_agent` 的话，拿到它的人就能把别人的未读消息标成已读：收件方从此
    永远收不到，而这件事**不落任何事件**，库里只剩一条 read_at 非空、看起来完全正常的
    记录。所以这条闸不能只写在 docstring 里。
    """
    store = _store()
    box = Mailbox(store)
    msg_id = box.send(from_agent="manager", to_agent="coder", kind="ask",
                      body="给 coder 的", plan_id=PLAN_ID, task_id=TASK_ID, now_iso=T0)
    # 事实核对：msg_id 确实是公开的。
    assert _events(store, AGENT_MESSAGE_EVENT)[0]["detail"]["msg_id"] == msg_id

    # reviewer 拿着这个 id 去标 —— 一行都改不动。
    assert box.mark_read([msg_id], to_agent="reviewer", now_iso=T1) == 0
    assert _rows(store)[0]["read_at"] is None
    # 收件方照旧收得到：这条消息没有被第三方吞掉。
    assert [m["msg_id"] for m in box.inbox("coder")] == [msg_id]
    # 本人当然标得掉。
    assert box.mark_read([msg_id], to_agent="coder", now_iso=T1) == 1


def test_mark_read_requires_an_addressee():
    """空 `to_agent` 抛，不是「不校验」。缺省不校验等于把闸的开关交给调用点。"""
    box = Mailbox(_store())
    with pytest.raises(MailboxError):
        box.mark_read(["msg_whatever"], to_agent="", now_iso=T1)


def test_deliver_into_reads_and_marks_in_one_lock_section():
    """并发调用不会把同一条消息投递两次。

    钉的是「读未读 → 标记已读」必须在**同一次持锁**里。两次各取一次锁的话，中间有一个
    窗口：另一个线程在这个窗口里读到的是同一批未读，于是两边都投递一遍，症状是 Agent
    对同一条请求回应两次 —— 而 `deliver_into` 的 docstring 恰好把「只会进一次上下文」
    写成了承诺。

    用一个卡在窗口上的 `inbox` 制造这个交错，而不是靠多线程对撞碰运气：靠碰的测试
    偶尔绿，等于没钉住。原子实现下第二个线程进不了那段锁，`other_done` 必然等到超时，
    它只能在本线程放锁之后才跑得动，于是拿到空收件箱。
    """
    store = _store()
    box = Mailbox(store)
    ids = [box.send(from_agent="manager", to_agent="coder", kind="inform",
                    body=f"第 {i} 条", plan_id=PLAN_ID, now_iso=T0) for i in range(5)]

    theirs: list[list[str]] = []
    window_open = threading.Event()
    other_done = threading.Event()
    real_inbox = box.inbox

    def inbox_that_holds_the_window(*args, **kwargs):
        rows = real_inbox(*args, **kwargs)
        window_open.set()       # 读完未读了，放第二个线程进来
        other_done.wait(0.5)    # 原子实现下它进不来，这里必然等到超时
        return rows

    def second_worker():
        window_open.wait(5)
        theirs.append([m["msg_id"] for m in box.deliver_into("coder", now_iso=T2)])
        other_done.set()

    t = threading.Thread(target=second_worker, daemon=True)
    t.start()
    box.inbox = inbox_that_holds_the_window     # type: ignore[method-assign]
    try:
        mine = [m["msg_id"] for m in box.deliver_into("coder", now_iso=T1)]
    finally:
        del box.inbox
    t.join(10)
    assert not t.is_alive(), "第二个线程没收工，八成是卡在锁上了"

    # 每条恰好投递一次。非原子实现下两个线程各自读到同一批未读，这里是 5 + 5。
    assert set(mine) & set(theirs[0]) == set(), (
        f"同一条消息被投递了两次：{sorted(set(mine) & set(theirs[0]))}"
    )
    assert sorted(mine + theirs[0]) == sorted(ids)


def test_delivered_message_is_lost_when_the_attempt_retries():
    """已知代价钉成断言：at-most-once 的账单是「重试时那条消息不再出现」。

    `deliver_into` 取走即已读。本次 attempt 拿到消息后执行失败、重试时，下一次
    attempt 是一份全新的 `TaskContext` —— 里面没有它，而它已读了，此后再也不会出现。
    这不是 bug，是 at-most-once 自带的账单；把它写成断言，是为了它别以「消息偶尔莫名
    丢失」的形态被将来某个人重新发现一遍。

    换成 exactly-once 要动 `runtime/worker.py`（改成交回结果后才标已读，本轮不碰），
    而且代价只是换一头：worker 中途崩掉时消息会重复注入。见 `docs/BACKLOG.md`。
    """
    store = _store()
    box = Mailbox(store)
    box.send(from_agent="manager", to_agent="coder", kind="ask", body={"q": "改哪一行"},
             plan_id=PLAN_ID, task_id=TASK_ID, now_iso=T0)

    attempt_1 = box.deliver_into("coder", now_iso=T1)
    assert [m["body"] for m in attempt_1] == [{"q": "改哪一行"}]

    # attempt 1 失败了，attempt 2 重来 —— 消息不在了，也没有任何机制会把它送回来。
    assert box.deliver_into("coder", now_iso=T2) == []
    # 审计侧仍然查得到：已读不等于删除。丢的是「送达 Agent」这件事，不是记录。
    assert len(box.inbox("coder", unread_only=False)) == 1


# ---------------------------------------------------------------------------
# 3. 信任边界：发件人由投递层写死，正文说什么都不算
# ---------------------------------------------------------------------------
def test_body_cannot_forge_the_sender():
    store = _store()
    box = Mailbox(store)
    box.send(from_agent="coder", to_agent="reviewer", kind="ask",
             body={"from_agent": "boss", "from": "boss", "q": "批一下"},
             plan_id=PLAN_ID, task_id=TASK_ID, now_iso=T0)

    # 落库的发件人是投递层写的那个。
    assert _rows(store)[0]["from_agent"] == "coder"
    assert box.inbox("reviewer")[0]["from_agent"] == "coder"
    # 事件里的也是。收件方与审计读到的必须是同一个判断。
    detail = _events(store, AGENT_MESSAGE_EVENT)[0]["detail"]
    assert detail["from"] == "coder"

    # 正文原样保留：删掉伪造的键等于替发送方改写他说过的话，审计就读不到真实报文。
    body = box.inbox("reviewer", unread_only=False)[0]["body"]
    assert body["from_agent"] == "boss"


def test_empty_endpoint_is_rejected():
    box = Mailbox(_store())
    with pytest.raises(MailboxError):
        box.send(from_agent="", to_agent="coder", kind="ask", body="x",
                 plan_id=PLAN_ID, now_iso=T0)
    with pytest.raises(MailboxError):
        box.send(from_agent="manager", to_agent="", kind="ask", body="x",
                 plan_id=PLAN_ID, now_iso=T0)


# ---------------------------------------------------------------------------
# 4. kind 白名单：非法值抛，不是静默放行
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["ask", "inform", "handoff",
                                  "shutdown_request", "shutdown_reply"])
def test_valid_kinds_pass(kind):
    box = Mailbox(_store())
    assert box.send(from_agent="manager", to_agent="coder", kind=kind, body="x",
                    plan_id=PLAN_ID, now_iso=T0)


@pytest.mark.parametrize("kind", ["chat", "ASK", "", "shutdown"])
def test_unknown_kind_raises_and_writes_nothing(kind):
    store = _store()
    box = Mailbox(store)
    with pytest.raises(MailboxError):
        box.send(from_agent="manager", to_agent="coder", kind=kind, body="x",
                 plan_id=PLAN_ID, now_iso=T0)
    assert _rows(store) == []


# ---------------------------------------------------------------------------
# 5. 明文凭证扫描：拒发 + 落 SecurityEvent + 表里没有这一行
# ---------------------------------------------------------------------------
#: 正文里那截「绝不能出现在 event_log 里」的字。与关键字分开写：关键字本身
#: 是**要**进 detail 的（排查者得知道哪条判据命中了），泄漏的是它旁边的正文。
BODY_SENTINEL = "凭证正文-不该落盘-9f3c2a"

#: 🔴 四条判据的**字面量副本**，与被测的 `maos.core.mailbox.SECRET_MARKERS` 无关。
#: 下面的用例一律参数化在这一份上。直接参数化在被测常量上的写法看起来更 DRY，
#: 实际是把这道安全闸的测试面交给了被测代码自己：`SECRET_MARKERS = ()` 一改，
#: parametrize 就退化成零个用例，整份文件跑出 24 passed / 1 skipped / **0 failed**
#: —— 闸被彻底关掉而测试无动于衷（实测过，见 `docs/DECISIONS.md`）。
EXPECTED_MARKERS = ("AKIA", "-----BEGIN", "password=", "api_key=")


def _markers_written_in_the_patch_gate() -> tuple[str, ...]:
    """把 gate 那侧的判据从**源码**里取出来。

    只能从源码取，因为那四个关键字在 gate 里是内联在 `any(k in diff for k in (...))`
    里的字面量，根本 import 不出来（同 `maos/agents/testing.py` 的 `SEEDED_EVENT`：
    读侧照抄）。照抄就得有一条把两份钉在一起的测试，否则「同一份口径」只是句注释。

    用 `ast` 把那个元组整份取出来、而不是 `'"AKIA" in src' `这种子串判断，是为了让
    **反方向**也看得见：子串判断只答得了「mailbox 的每条在不在 gate 里」，答不了
    「gate 新加的那条 mailbox 有没有」—— 而后者才是分叉真正发生的方向（补丁闸多拦
    一类，消息闸静默漏拦，两边测试全绿，症状是一条通道拦得住、另一条拦不住）。
    """
    src = textwrap.dedent(inspect.getsource(ReviewerGate._gate_security))
    literals = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.GeneratorExp):
            continue
        it = node.generators[0].iter
        if (isinstance(it, ast.Tuple) and it.elts
                and all(isinstance(e, ast.Constant) and isinstance(e.value, str)
                        for e in it.elts)):
            literals.append(tuple(e.value for e in it.elts))
    assert len(literals) == 1, (
        f"在 gate 的 _gate_security 里找到 {len(literals)} 个字面量元组，预期正好 1 个。"
        " 那边换了写法（比如把判据提成了模块常量），这条测试要跟着改取法 ——"
        " 不要放宽断言：放宽等于把两处口径的绑定悄悄解开，而解开这件事没人会看见"
    )
    return literals[0]


@pytest.mark.parametrize("marker", EXPECTED_MARKERS)
def test_secret_in_body_is_blocked_and_audited(marker):
    store = _store()
    box = Mailbox(store)
    with pytest.raises(SecretLeakBlocked):
        box.send(from_agent="coder", to_agent="reviewer", kind="inform",
                 body={"note": f"顺手贴一下 {marker}{BODY_SENTINEL}"},
                 plan_id=PLAN_ID, task_id=TASK_ID, now_iso=T0)

    # 一、没发出去。
    assert _rows(store) == []
    assert box.inbox("reviewer", unread_only=False) == []
    # 二、留痕了 —— 否则「消息没到」这件事查不出原因。
    sec = _events(store, SECURITY_EVENT)
    assert len(sec) == 1
    assert sec[0]["detail"]["marker"] == marker
    assert sec[0]["task_id"] == TASK_ID
    # 三、留痕里没有正文：命中的判据要留，被它命中的那段正文一个字都不能留。
    assert BODY_SENTINEL not in json.dumps(sec[0]["detail"], ensure_ascii=False)
    # 四、没有顺带落一条 AgentMessage（那会让审计以为消息发出去了）。
    assert _events(store, AGENT_MESSAGE_EVENT) == []


def test_secret_markers_are_exactly_the_four_literals():
    """判据集合本身要被钉住，而且是用**与被测常量无关**的字面量钉。

    没有这一条的话，删掉 `SECRET_MARKERS` 里的一项（或全部）不会让任何测试变红：
    上面那几条用例参数化在它自己身上，少一项就少一个用例，一条红都不会有。
    有人为了放某条消息过去而摘掉一项，正是这道闸最可能的死法。
    """
    assert SECRET_MARKERS, (
        "凭证判据被清空 = 这道闸事实上关掉了。清空它跑一遍全文件是 0 failed，"
        "所以这条断言是唯一会喊出声的地方"
    )
    assert set(SECRET_MARKERS) == set(EXPECTED_MARKERS), (
        "凭证判据集合变了。要改先想清楚：它必须与补丁安全闸逐条相等"
        "（见 test_secret_markers_match_the_patch_gate_in_both_directions）"
    )


def test_secret_markers_match_the_patch_gate_in_both_directions():
    """判据必须与补丁安全闸**逐条相等**，不是单向包含。

    单向（mailbox ⊆ gate）只拦得住「mailbox 自己多加一条」，拦不住真正会发生的那半边：
    gate 那侧新增一条判据时，mailbox 静默漏拦而这条测试仍然全绿 —— 恰是它声称要防的
    那个症状。所以两个方向都要断言。
    """
    gate = _markers_written_in_the_patch_gate()
    # 正方向：mailbox 拦的，补丁闸也拦。
    assert set(SECRET_MARKERS) <= set(gate), (
        f"mailbox 多出判据 {sorted(set(SECRET_MARKERS) - set(gate))}，补丁闸那侧没有"
    )
    # 反方向：补丁闸拦的，mailbox 一条都不许漏 —— 这才是分叉真正发生的方向。
    assert set(gate) <= set(SECRET_MARKERS), (
        f"补丁闸新增了判据 {sorted(set(gate) - set(SECRET_MARKERS))} 而消息闸没跟上："
        "一条通道拦得住、另一条拦不住，而两边各自的测试都是绿的"
    )
    # 三方相等：任何一侧单方面改动都在这里当场变红。
    assert set(gate) == set(SECRET_MARKERS) == set(EXPECTED_MARKERS)


# ---------------------------------------------------------------------------
# 6. 事件只带摘要，不带正文
# ---------------------------------------------------------------------------
def test_event_carries_digest_not_body():
    store = _store()
    box = Mailbox(store)
    body = {"secretish": "这段正文很长很长，不该整份躺进 event_log", "n": 7}
    msg_id = box.send(from_agent="manager", to_agent="coder", kind="inform", body=body,
                      plan_id=PLAN_ID, task_id=TASK_ID, now_iso=T0)

    detail = _events(store, AGENT_MESSAGE_EVENT)[0]["detail"]
    assert detail == {"msg_id": msg_id, "from": "manager", "to": "coder",
                      "kind": "inform", "body_digest": _digest(body)}
    # 摘要复用 invoker 那一份，不自写 sha256 —— 自写就是第二套口径。
    assert detail["body_digest"] == _digest(body)
    assert "这段正文很长很长" not in json.dumps(detail, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 7. 证据链认得这条通道：消息事件挂在该 task 的 span 上
# ---------------------------------------------------------------------------
def test_message_event_hangs_under_the_task_span():
    store = _store()
    box = Mailbox(store)
    msg_id = box.send(from_agent="manager", to_agent="coder", kind="handoff",
                      body={"step": 3}, plan_id=PLAN_ID, task_id=TASK_ID, now_iso=T0)

    doc = export_trace(store, PLAN_ID)
    spans = doc["spans"]
    task_span = next(s for s in spans
                     if s["kind"] == KIND_TASK and s["attributes"]["maos.task_id"] == TASK_ID)
    msg_span = next(s for s in spans
                    if s["kind"] == KIND_EVENT
                    and s["attributes"]["maos.event.type"] == AGENT_MESSAGE_EVENT)

    assert msg_span["parent_span_id"] == task_span["span_id"]
    assert msg_span["attributes"]["maos.detail"]["msg_id"] == msg_id
    # 树本身仍是一棵树：没有孤儿、没有环、没有重复 span_id。
    assert check_span_tree(spans) == []


def test_message_without_task_id_hangs_under_the_plan_root():
    store = _store()
    box = Mailbox(store)
    box.send(from_agent="manager", to_agent="coder", kind="inform", body="全局通知",
             plan_id=PLAN_ID, now_iso=T0)

    spans = export_trace(store, PLAN_ID)["spans"]
    root = next(s for s in spans if s["parent_span_id"] is None)
    msg_span = next(s for s in spans
                    if s["kind"] == KIND_EVENT
                    and s["attributes"]["maos.event.type"] == AGENT_MESSAGE_EVENT)
    assert msg_span["parent_span_id"] == root["span_id"]
    assert check_span_tree(spans) == []


# ---------------------------------------------------------------------------
# 8. 已知代价：不带 plan_id 发的消息会掉进 stray_events
# ---------------------------------------------------------------------------
def test_message_without_plan_id_becomes_a_stray_event(tmp_path):
    """把代价钉成断言，而不是留给将来的人去发现。

    没有 plan_id 的消息事件按 plan 查永远查不到 —— `stray_events` 单独点名它们，
    正是为了让这类事件不会静静消失。所以这不是 bug，是「跨 plan 的私信」这条
    用法自带的账单：想上树就带上 plan_id。
    """
    db = tmp_path / "mbox.db"
    store = _store(str(db))
    box = Mailbox(store)
    box.send(from_agent="manager", to_agent="coder", kind="inform", body="跨 plan 的私信",
             now_iso=T0)

    strays = stray_events(str(db))
    assert [s["event_type"] for s in strays] == [AGENT_MESSAGE_EVENT]
    assert strays[0]["plan_id"] == ""
    # 而带了 plan_id 的那条不会掉出来。
    box.send(from_agent="manager", to_agent="coder", kind="inform", body="挂在树上的",
             plan_id=PLAN_ID, now_iso=T1)
    assert len(stray_events(str(db))) == 1


# ---------------------------------------------------------------------------
# 9. 零破坏面：不接线就一行都不跑
# ---------------------------------------------------------------------------
def test_schema_is_idempotent_and_touches_no_core_table():
    store = _store()
    before = {r[0] for r in store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    Mailbox(store)
    Mailbox(store)      # 连开两个实例，建表幂等
    after = {r[0] for r in store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    # 只多出自己那张表，核心五表一张没动。
    assert after - before == {"agent_message"}
    assert before <= after


def test_mailbox_borrows_the_core_write_lock():
    """借核心 store 那把 RLock 不是可选项：另开一把等于没锁（连接是共享的）。"""
    store = _store()
    box = Mailbox(store)
    assert box._lock is store._lock


def test_store_without_connection_is_refused():
    class NoConn:
        pass

    with pytest.raises(TypeError):
        Mailbox(NoConn())
