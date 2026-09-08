"""信任边界（T109）—— 通道建起来之后，哪些保证仍然成立、哪一条已经不成立。

**这一份交付的是测试，不是新机制。** 三查今天就是运行时强制的
（`maos/agents/base.py` 的 `check_tool` / `check_risk` / `check_write`，
`maos/skills/invoker.py` 的 skill 白名单），这一层是对的。

要买的是这个：今天「Agent 之间没有通道」，所以「A 指使 B 干 A 才有权干的事」
这件事**靠没有通道免疫**，不靠机制。而通道正在被别的轨建出来（点对点邮箱）。
通道一通，「不继承发送方白名单」就从一个无人验证的假设变成一条必须成立的保证 ——
下面每一条都在钉它，防的是将来有人为了「让消息能驱动动作」把它放宽掉。

**最后一条是反的**：它断言一个口子**今天是通的**。见 `test_agent_reaches_a_
writable_store_today`，那条 docstring 写清了它为什么长这样、被收紧时该怎么改。
"""

from __future__ import annotations

import pytest

from maos.agents.base import AgentIdentity, BaseAgent, PermissionDenied
from maos.core.store import SqliteStore
from maos.model.client import Tier

#: 一个**真实注册**的 skill。用真的而不是编一个名字，是为了让「发送方确实有权调
#: 它」这半边也是真的 —— 两边都用假名字的话，这条测试只证明了「未注册的 skill
#: 调不动」，那是另一件事。
SHARED_SKILL = "refund.intake"


def _identity(agent_id: str, **over) -> AgentIdentity:
    base = dict(
        agent_id=agent_id, role=agent_id.replace("-", "_"),
        duty="只在这一份测试里存在",
        allowed_skills=frozenset(), allowed_tools=frozenset(),
        write_scope=frozenset(), max_risk="L", model_tier=Tier.LIGHT,
    )
    base.update(over)
    return AgentIdentity(**base)


class _Agent(BaseAgent):
    """一个不进 `AGENT_POOL` 的 Agent。

    **刻意不加 `@register`**：注册进去会让全仓那些「池子里有 24 个」的断言随
    import 顺序变红变绿，而这一份测的是三查的机制，与池子里有谁无关。
    """

    identity = _identity("probe")

    def run(self, ctx):                                          # noqa: ANN001
        raise AssertionError("这一份测试不该跑到 run()")


def _agent(identity: AgentIdentity, store=None) -> _Agent:       # noqa: ANN001
    agent = _Agent(model=object(), store=store)                  # 模型一次都不调
    agent.identity = identity
    agent.skills.identity = identity                             # invoker 认的是这个
    return agent


#: 发送方：有权调 SHARED_SKILL、能碰沙箱、能写 repo_branch、扛得住 H 级。
SENDER = _identity("sender", allowed_skills=frozenset({SHARED_SKILL}),
                   allowed_tools=frozenset({"sandbox"}),
                   write_scope=frozenset({"artifact", "repo_branch"}), max_risk="H")

#: 收件方：四样一样都没有。它收到的请求来自一个**比它权限大**的同伴。
RECEIVER = _identity("receiver")


# --------------------------------------------------------------------------
# 收件方按自己的 Identity 三查，不继承发送方的白名单
# --------------------------------------------------------------------------
def test_receiver_checks_its_own_skill_whitelist_not_the_senders():
    """A 有权调、B 没有 —— B 拿着 A 的请求去调，必须抛，且抛的是 **B** 的名字。

    「A 让 B 去调」在今天只能这么模拟：把同一个 skill 名交给 B 的 invoker。通道
    建起来之后，这就是一条点对点消息落地时会发生的事。断言异常里是 B 的
    `agent_id`，是为了钉住「拦它的是 B 自己的白名单」—— 换成继承发送方白名单的
    实现，这条断言会红在名字上，而不是等到某天有人越权调用了才发现。
    """
    sender, receiver = _agent(SENDER), _agent(RECEIVER)

    # A 自己调不抛 PermissionDenied（skill 缺前置条件会回 failed，那是另一层的事）
    assert sender.skills.invoke(SHARED_SKILL, {}).status in {"ok", "failed"}

    with pytest.raises(PermissionDenied) as caught:
        receiver.skills.invoke(SHARED_SKILL, {})

    assert "receiver" in str(caught.value)
    assert "sender" not in str(caught.value)
    assert SHARED_SKILL in str(caught.value)


def test_receiver_checks_its_own_tool_whitelist():
    """工具白名单同理：发送方能碰沙箱，不代表收件方能。"""
    assert _agent(SENDER).check_tool("sandbox") is None

    with pytest.raises(PermissionDenied) as caught:
        _agent(RECEIVER).check_tool("sandbox")
    assert "receiver" in str(caught.value)


def test_receiver_checks_its_own_risk_ceiling():
    """风险档位同理：发送方扛得住 H，不代表收件方可以被指使去干 H。

    这条最值得盯：一条「帮我把这个 H 级的活干了」的消息，如果按发送方的档位放行，
    等于让任何一个 L 级 Agent 都能被提权到 H —— 而 `max_risk` 是这一层唯一一道
    与「这次动作有多贵」直接挂钩的闸。
    """
    assert _agent(SENDER).check_risk("H") is None

    with pytest.raises(PermissionDenied) as caught:
        _agent(RECEIVER).check_risk("H")
    assert "receiver" in str(caught.value)


def test_receiver_checks_its_own_write_scope():
    """可写资源同理：发送方能写 repo_branch，收件方连 artifact 都不能写。"""
    assert _agent(SENDER).check_write("repo_branch") is None

    with pytest.raises(PermissionDenied) as caught:
        _agent(RECEIVER).check_write("repo_branch")
    assert "receiver" in str(caught.value)


def test_a_permissive_sender_cannot_lend_its_identity_to_the_receiver():
    """把发送方的 Identity 整个递过去也不行 —— 三查读的是 `self.identity`。

    这条钉的是**实现形状**：只要三查从 `self.identity` 取，谁递什么过来都无所谓；
    哪天有人给 `check_*` 加一个 `on_behalf_of=` 参数，这条测试是最先红的地方。
    """
    receiver = _agent(RECEIVER)

    for check, arg in ((receiver.check_tool, "sandbox"),
                       (receiver.check_risk, "H"),
                       (receiver.check_write, "repo_branch")):
        with pytest.raises(PermissionDenied):
            check(arg)

    with pytest.raises(TypeError):                   # 没有「代某人执行」这条路
        receiver.check_tool("sandbox", SENDER)       # type: ignore[call-arg]


# --------------------------------------------------------------------------
# 反向的一条：一个今天还敞着的口子
# --------------------------------------------------------------------------
def test_agent_reaches_a_writable_store_today__anchor_for_tightening():
    """【待收紧的现状，不是保证】Agent 经 `self.skills.store` 拿得到**可写**的核心 store。

    `maos/core/store.py` 的原话是「Agent 永远不直接碰这一层」，而今天那是**纪律
    不是机制**：`WorkerRuntime` 把裸 store 交给每个 Agent（`cls(model, store=...)`
    → `SkillInvoker.__init__` 的 `self.store = store`），于是 `agent.skills.store`
    上挂着 `update_task` / `insert_task` —— Agent 可以绕过 Control Plane 直接改
    任务状态，一次非法迁移都不会被 `can_transition()` 看见。

    今天没人这么用（`base.py` 里只用它记账），所以这个口子是静默的。写成一条测试
    是为了让它**不再静默**：口子的形状被钉在这里，谁都能读到它有多大。

    **收紧的方向**：给 Agent 传一个只读视图，或一个只开放记账方法的 skill 专用句柄，
    而不是裸 store。那要改 `maos/runtime/worker.py` —— 那是 T107 的面，且属整合期
    的事，本轨只立锚点，一行实现都不动。

    **这条测试将来会红，那正是它的用途**：红了说明口子堵上了。那时不要「修」它 ——
    把断言改成新形状（比如 `not hasattr(store, "update_task")`），并把这段
    docstring 改成「已收紧于 <轨号>」。
    """
    store = SqliteStore(":memory:")
    store.init_schema()
    agent = _agent(RECEIVER, store=store)

    assert agent.skills.store is store               # 拿到的就是那个裸 store
    assert callable(getattr(agent.skills.store, "update_task", None))
    assert callable(getattr(agent.skills.store, "insert_task", None))

    # 而且真的写得动：这里不是「理论上可以」，是实测越过 Control Plane 写成了。
    store.insert_plan({"plan_id": "p1", "trace_id": "tr", "goal": "g",
                       "state": "RUNNING"})
    store.insert_task({"task_id": "t1", "plan_id": "p1", "trace_id": "tr",
                       "role": "probe", "title": "x", "state": "PENDING"})
    agent.skills.store.update_task("t1", state="DONE")   # PENDING -> DONE，非法迁移
    assert store.get_task("t1")["state"] == "DONE"

    # 三查一个都没被惊动 —— 因为这条路根本不经过它们。这才是口子的要害。
    assert agent.identity.write_scope == frozenset()


def test_the_gap_is_in_the_handout_not_in_the_skill_layer():
    """口子在**交出去的东西**上，不在 skill 层：不给 store，Agent 就够不着。

    这条是上一条的另一半，也是收紧方案的可行性证明 —— `SkillInvoker` 本来就接受
    `store=None`（只是跳过落库），所以换成只读句柄不需要动 skill 层的任何签名。
    """
    assert _agent(RECEIVER, store=None).skills.store is None
