"""成员名册（`maos/core/roster.py`，T109）。

这一份钉的不是「名册长什么样」，是**它的每一行都得有出处**。名册是投影不是事实：
`duty` 逐字来自 `AgentIdentity`，`title` 逐字来自圆桌那三张常量表，忙闲从 `task`
表推。任何一条改成「在名册里另写一份」，下面都有一条测试会红 —— 那正是它们存在
的理由。第五份各说各话的身份来源，是这个模块唯一可能造成的伤害。
"""

from __future__ import annotations

import pytest

from maos.agents import AGENT_POOL
from maos.agents.base import AgentIdentity
from maos.contracts.states import TaskState
from maos.core.roster import (
    SOURCE_AGENT_POOL,
    SOURCE_MANUAL,
    SOURCE_ROUNDTABLE,
    STATUS_BUSY,
    STATUS_IDLE,
    STATUS_UNKNOWN,
    Member,
    Roster,
    render_members,
    roundtable_members,
)
from maos.core.store import SqliteStore

#: 今天池子里有 24 个 Agent（`docs/agent-identity.md`）。断言写成 `>=` 而不是 `==`：
#: 别的轨往 `maos/agents/` 投一个文件就多一个，那是**预期中的增长**，不该让这里变红；
#: 而少一个是有人误删了 Agent，那必须红。上限由「名册条数 == 池子条数」那条管。
POOL_FLOOR = 24


@pytest.fixture()
def store() -> SqliteStore:
    st = SqliteStore(":memory:")
    st.init_schema()
    return st


def _plan_with_task(st: SqliteStore, *, plan_id: str = "p1", task_id: str = "t1",
                    role: str = "refund_intake") -> None:
    st.insert_plan({"plan_id": plan_id, "trace_id": "tr-1", "goal": "g",
                    "state": "RUNNING"})
    st.insert_task({"task_id": task_id, "plan_id": plan_id, "trace_id": "tr-1",
                    "role": role, "title": "一件事", "state": TaskState.PENDING})


# --------------------------------------------------------------------------
# 常量本身：下面所有断言都拿它们当尺子，尺子自己得先钉住
# --------------------------------------------------------------------------
def test_the_six_constants_are_distinct_literals_not_just_names():
    """六个常量的**字面值**逐个钉死，且两组各自互不相同。

    没有这一条，下面每一条断言都可能假绿：它们比的是 `x == STATUS_UNKNOWN` 而不是
    `x == "unknown"`，所以把 `STATUS_UNKNOWN` 改成 `"idle"` 之后，「不知道」与
    「闲着」会塌成同一个值，而全套测试仍然全绿 —— 实测过，16 条一条不红。那恰好
    抹掉的是 `status_of` 存在的全部理由：把「不知道」答成「闲着」，调度方会照着
    它派活，而那台机器可能压根没起。

    `source` 那三个同理：三者塌成一个值之后，「这条记录哪来的」这个问题的答案
    永远是对的，也永远没有信息。
    """
    assert (STATUS_IDLE, STATUS_BUSY, STATUS_UNKNOWN) == ("idle", "busy", "unknown")
    assert (SOURCE_AGENT_POOL, SOURCE_ROUNDTABLE, SOURCE_MANUAL) == (
        "agent_pool", "roundtable", "manual")
    assert len({STATUS_IDLE, STATUS_BUSY, STATUS_UNKNOWN}) == 3
    assert len({SOURCE_AGENT_POOL, SOURCE_ROUNDTABLE, SOURCE_MANUAL}) == 3


# --------------------------------------------------------------------------
# 出处：每一行都来自别处，不来自这里
# --------------------------------------------------------------------------
def test_from_agent_pool_covers_every_role_with_the_identity_duty_verbatim():
    """名册覆盖池子全部 role，且 `duty` **逐字**等于 `identity.duty`。

    逐字比较是关键：名册里另写一句「更好读」的职责文案，就等于凭空造了第二份
    身份说明 —— 两边都不报错，只是慢慢对不上，而房间里的人读到的是名册这份。
    """
    roster = Roster.from_agent_pool()

    assert len(AGENT_POOL) >= POOL_FLOOR
    assert len(roster) == len(AGENT_POOL)
    assert {m.role for m in roster.members()} == set(AGENT_POOL)

    for member in roster.members():
        identity = AGENT_POOL[member.role].identity
        assert member.agent_id == identity.agent_id
        assert member.duty == identity.duty          # 逐字，不是「差不多」
        assert member.source == SOURCE_AGENT_POOL
        assert member.worker_id == ""                # 没登记就是没登记，不许猜


def test_from_agent_pool_hardcodes_no_names(monkeypatch):
    """往池子里投一个 Agent，名册自动多一个人 —— 名册里没有一份写死的名单。

    这条是「领域无关」（铁律 9）在本模块的落点：新业务域投一个 Agent 文件进
    `maos/agents/`，名册就该认得它，不需要有人回来改 `roster.py`。
    """
    fake = type("FakeAgentCls", (), {"identity": AgentIdentity(
        agent_id="zzz-probe", role="zzz_probe", duty="只在这条测试里存在")})
    monkeypatch.setitem(AGENT_POOL, "zzz_probe", fake)

    roster = Roster.from_agent_pool()
    hit = roster.find("zzz-probe")

    assert hit is not None
    assert hit.role == "zzz_probe" and hit.duty == "只在这条测试里存在"
    assert len(roster) == len(AGENT_POOL)


def test_roundtable_seats_and_agent_pool_describe_the_same_people():
    """两份名册说的是**同一批人**：圆桌五岗的 role 都在池子里找得到 Agent。

    圆桌那三张表是写死的常量（`maos/roundtable/team.py`），池子是扫出来的。它们
    对不上时，症状是房间里那五个名牌背后没有真 Agent —— 而两边都不报错。
    """
    from maos.roundtable.team import ROLE_OF, TEAM_ORDER, TITLES

    seats = roundtable_members()
    assert [m.agent_id for m in seats] == list(TEAM_ORDER)   # 顺序即发言顺序

    for seat in seats:
        assert seat.source == SOURCE_ROUNDTABLE
        assert seat.title == TITLES[seat.agent_id]           # 逐字，不是自己排的
        assert seat.role == ROLE_OF[seat.agent_id]
        assert seat.role in AGENT_POOL, f"圆桌的 {seat.agent_id} 在池子里没有 Agent"
        # duty 走 identity_of()：池子里有真 Agent 就该是真 Agent 那句，
        # 不是 team.py 里那两条过渡件的说法。
        assert seat.duty == AGENT_POOL[seat.role].identity.duty


def test_merge_roundtable_only_lends_titles_and_keeps_the_source():
    """折进圆桌只补 `title`，不改 `source` —— 那一行的出处仍然是池子。

    整条覆盖的话，五岗的 `source` 会变成 roundtable，于是「这条记录哪来的」这个
    问题答错了：岗位名是圆桌给的，身份不是。
    """
    plain = Roster.from_agent_pool()
    merged = Roster.from_agent_pool().merge_roundtable()

    assert len(merged) == len(plain)                 # 五岗都在池子里，一个都不新增
    intake = merged.find("refund-intake")
    assert intake.title == "申请受理岗"
    assert intake.source == SOURCE_AGENT_POOL        # 出处没变
    assert intake.duty == AGENT_POOL["refund_intake"].identity.duty
    assert plain.find("refund-intake").title == ""   # 不 merge 就没有 title


# --------------------------------------------------------------------------
# 查询
# --------------------------------------------------------------------------
def test_find_hits_by_agent_id_and_by_role_and_misses_return_none():
    """按 agent_id 或 role 都查得到；查不到返回 None，**不抛**。

    不抛是刻意的：调用方多半是「有人在群里 @ 了一个名字」，名字打错是常态。
    """
    roster = Roster.from_agent_pool()

    assert roster.find("refund-intake").role == "refund_intake"      # agent_id
    assert roster.find("refund_intake").agent_id == "refund-intake"  # role
    assert roster.find("查无此人") is None
    assert roster.find("") is None
    assert "refund-intake" in roster and "refund_intake" in roster
    assert "查无此人" not in roster


def test_by_role_returns_every_member_sharing_that_role():
    """一个 role 挂在两个 worker 上时，`by_role` 两个都给，`find` 只给一个。

    这是两个方法存在的全部理由：点对点消息要唯一收件人（find），而「这个 role
    现在有几个人在干」要全量（by_role）。混成一个方法就必然有一边是错的。
    """
    roster = Roster.from_agent_pool()
    roster.register("w1", roles=["refund_intake"])
    roster.register("w2", roles=["refund_intake"])

    same = roster.by_role("refund_intake")
    assert [m.agent_id for m in same] == ["refund-intake", "refund-intake@w2"]
    assert {m.worker_id for m in same} == {"w1", "w2"}
    assert roster.find("refund_intake").agent_id == "refund-intake"   # 确定的那一个
    assert roster.by_role("") == [] and roster.by_role("没这个 role") == []


def test_members_order_is_deterministic_across_runs():
    """两次建名册，`members()` 逐字节相同。

    这不是审美：这份名单会进群里的回帖、进喂给模型的【事实】、进证据文件。
    按注册顺序排会随「谁先被 import」漂移，而两次跑出来不一样的证据不是证据。
    """
    first = render_members(Roster.from_agent_pool().members())
    second = render_members(Roster.from_agent_pool().members())

    assert first == second
    ids = [m.agent_id for m in Roster.from_agent_pool().members()]
    assert ids == sorted(ids)


# --------------------------------------------------------------------------
# 登记
# --------------------------------------------------------------------------
def test_register_fills_in_place_then_forks_for_a_second_worker():
    """三条规则各一句：就地填 / 幂等 / 第二个 worker 另起一行。

    第三条最容易被写成「覆盖」，而覆盖的后果是把发给 w1 那位的信悄悄改投给 w2 ——
    `agent_id` 是点对点消息的收件人，它必须唯一且稳定。
    """
    roster = Roster.from_agent_pool()
    size = len(roster)

    got = roster.register("w1", roles=["refund_intake"])
    assert [(m.agent_id, m.worker_id) for m in got] == [("refund-intake", "w1")]
    assert len(roster) == size                       # 就地填，不长记录

    again = roster.register("w1", roles=["refund_intake"])
    assert [(m.agent_id, m.worker_id) for m in again] == [("refund-intake", "w1")]
    assert len(roster) == size                       # 幂等

    forked = roster.register("w2", roles=["refund_intake"])
    assert [m.agent_id for m in forked] == ["refund-intake@w2"]
    assert len(roster) == size + 1
    assert roster.find("refund-intake").worker_id == "w1"    # w1 那位一个字没动
    assert roster.find("refund-intake@w2").source == SOURCE_MANUAL


def test_register_keeps_a_worker_the_pool_never_heard_of():
    """池子里没有的 role / agent_id 不丢弃，按 `manual` 收进来。

    worker 是**活着的事实**。名册答不出「它是谁」，比多一行没有 duty 的记录更糟 ——
    前者让人以为那个 worker 不存在。
    """
    roster = Roster.from_agent_pool()
    got = roster.register("w9", roles=["role_nobody_declared"], agent_ids=["ghost"])

    assert {m.agent_id for m in got} == {"role_nobody_declared", "ghost"}
    assert all(m.source == SOURCE_MANUAL and m.worker_id == "w9" for m in got)
    assert roster.find("ghost").duty == ""           # 不知道就是不知道，不许编

    with pytest.raises(ValueError):                  # 空 worker_id 的含义是「没登记」
        roster.register("", roles=["refund_intake"])


# --------------------------------------------------------------------------
# 忙闲：从 task 表推，不另存一份
# --------------------------------------------------------------------------
def test_status_of_reads_busy_from_the_task_table(store):
    """有 RUNNING 任务就 busy，没有就 idle —— 判据在 `task` 表，不在名册里。

    名册上另存一个 `status` 字段就有了第二份事实，而它必然与 task 表漂移：
    进程崩在 RUNNING 上时，那个字段会永远停在 busy。
    """
    roster = Roster.from_agent_pool()
    roster.register("w1", roles=["refund_intake"])
    _plan_with_task(store)

    assert roster.status_of("refund-intake", store) == STATUS_IDLE

    store.update_task("t1", state=TaskState.RUNNING, worker_id="w1")
    assert roster.status_of("refund-intake", store) == STATUS_BUSY
    assert roster.status_of("refund-intake", store, plan_id="p1") == STATUS_BUSY

    store.update_task("t1", state=TaskState.AWAITING_REVIEW)
    assert roster.status_of("refund-intake", store) == STATUS_IDLE


def test_status_of_does_not_borrow_another_workers_running_task(store):
    """w2 名下的 RUNNING 任务不许把 w1 上那位说成 busy。"""
    roster = Roster.from_agent_pool()
    roster.register("w1", roles=["refund_intake"])
    roster.register("w2", roles=["refund_intake"])
    _plan_with_task(store)
    store.update_task("t1", state=TaskState.RUNNING, worker_id="w2")

    assert roster.status_of("refund-intake@w2", store) == STATUS_BUSY
    assert roster.status_of("refund-intake", store) == STATUS_IDLE


def test_status_of_says_unknown_instead_of_guessing_idle(store):
    """三条 `unknown`：查无此人 / 没登记在任何 worker 上 / 问不到 task 表。

    它们都**不是**「闲着」。把不知道渲染成 idle，等于给了一个会被当真的假答案 ——
    调度方会照着它派活，而那台机器可能压根没起。
    """
    roster = Roster.from_agent_pool()
    _plan_with_task(store)

    assert roster.status_of("查无此人", store) == STATUS_UNKNOWN
    assert roster.status_of("refund-intake", store) == STATUS_UNKNOWN   # 没登记 worker

    roster.register("w1", roles=["refund_intake"])
    assert roster.status_of("refund-intake", None) == STATUS_UNKNOWN    # 没有 store

    class _Mute:                                     # 有 _conn 但问不出东西的后端
        _conn = None

    assert roster.status_of("refund-intake", _Mute()) == STATUS_UNKNOWN


def test_status_of_survives_a_store_without_the_task_table():
    """没建表的 store 报 `unknown`，不抛 —— `/team` 不该被一个空库打掉。"""
    roster = Roster.from_agent_pool()
    roster.register("w1", roles=["refund_intake"])

    assert roster.status_of("refund-intake", SqliteStore(":memory:")) == STATUS_UNKNOWN


# --------------------------------------------------------------------------
# 渲染 与 /team 的兜底路径
# --------------------------------------------------------------------------
def test_render_members_shows_duty_and_worker_but_invents_nothing():
    """渲染只排已有字段：没 title 就不写岗位名，没 worker 就不写 worker。"""
    out = render_members([
        Member(agent_id="a-1", role="r1", duty="干一件事", title="某岗",
               worker_id="w1", source=SOURCE_AGENT_POOL),
        Member(agent_id="a-2", role="r2", duty="", source=SOURCE_MANUAL),
    ])

    assert "某岗（a-1） · role=r1 · w1" in out
    assert "  职责：干一件事" in out
    assert "a-2 · role=r2" in out
    assert "（a-2）" not in out                       # 没岗位名就不套一对括号
    assert out.count("职责") == 1                    # duty 为空就不排那一行


def test_team_command_without_roundtable_now_lists_the_global_roster(store):
    """`/team` 没接圆桌时报全局名册，而不只是一句「没接圆桌」（T109）。

    「没接圆桌」那半句仍然在，且排在最前：它是真的，而少了它，人会把这份名册
    当成房间里那五个名牌。改这条之前先想清楚 —— `/team` 问的是「这里有哪些
    成员」，这个问题在没接圆桌时也有答案。
    """
    from maos.ingress.contracts import InboundMessage
    from maos.ingress.router import IngressRouter

    router = IngressRouter({}, store=store)
    out = router.handle_team(InboundMessage(
        channel="feishu", chat_id="oc_1", sender="u1", text="/team", msg_id="m1"))

    assert "没接圆桌" in out                          # 现有测试钉着这半句，别弄丢
    assert f"{len(AGENT_POOL)} 位成员" in out
    for role, cls in AGENT_POOL.items():
        assert cls.identity.agent_id in out, f"{role} 没出现在 /team 里"
    assert AGENT_POOL["refund_finance"].identity.duty in out


def test_team_command_falls_back_to_one_line_if_the_roster_blows_up(store, monkeypatch):
    """名册排不出来就退回原来那一句，不把异常甩进群里。

    这条路径要 import 整个 Agent 池；只装了命令面的进程未必带得动它，而那时该说的
    是「圆桌没接」，不是一个 ImportError。
    """
    from maos.ingress.contracts import InboundMessage
    from maos.ingress.router import IngressRouter

    def _boom() -> Roster:
        raise RuntimeError("池子扫不动")

    monkeypatch.setattr(Roster, "from_agent_pool", staticmethod(_boom))
    router = IngressRouter({}, store=store)
    out = router.handle_team(InboundMessage(
        channel="feishu", chat_id="oc_1", sender="u1", text="/team", msg_id="m1"))

    assert out == "本进程没接圆桌（单机器人模式），命令面与申请表照常可用"
