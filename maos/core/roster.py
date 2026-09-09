"""成员名册 —— 「这里有哪些同伴、各自是谁」的唯一可问处（T109）。

**为什么要有这一层**：这个问题今天有四份各说各话的答案，谁都答不全 ——

  · ``maos.agents.base.AGENT_POOL``：键是 role 不是 agent_id，值是**类**不是实例，
    没有在线状态；
  · 圆桌名册（``maos.roundtable.team`` 的 ``TEAM_ORDER`` / ``TITLES`` / ``ROLE_OF``）：
    三张写死的常量表，只覆盖退款五岗，不从池子枚举；
  · 房间发声面（``hiclaw.room_voices``）：只管「谁在房间里说话」；
  · ``worker_id``：只有 Worker Runtime 自己知道，与前三份没有任何连接。

本模块**不是第五份事实**，是把前面几份投影到一个形状上：``duty`` 逐字取自
``AgentIdentity.duty``（不另写一份文案），``title`` 逐字取自圆桌那三张常量表，
``worker_id`` 由活着的 worker 自己 ``register`` 进来，忙闲从 ``task`` 表**推**出来。
名册自己不落库、不新开表 —— 一落库就有了第二份事实，而它必然与前面几份漂移。

**惰性 import**：本模块顶层不 import ``maos.agents`` 与 ``maos.roundtable``。
``maos.agents.base`` 反过来 import 核心 store，顶层拉进来就成环；而 ``maos.core``
这一层在没接圆桌、没装 Agent 的进程里（``maos.ingress`` 就是）也要能 import。

**领域无关**（铁律 9）：名册里没有一个退款/理赔字样，五岗只是 ``AGENT_POOL`` 里
恰好存在的五条记录。新业务域投一个 Agent 文件进 ``maos/agents/``，名册自动多几个人。
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace
from typing import Any

from maos.contracts.states import TaskState        # 只读常量：冻结面一个字节不动

#: 这条记录是哪来的。名册的每一行都必须答得上这个问题 —— 答不上就说明
#: 它是被人凭空写进来的，而那正是「第五份事实」的样子。
SOURCE_AGENT_POOL = "agent_pool"                   # 从 AGENT_POOL 枚举出来的
SOURCE_ROUNDTABLE = "roundtable"                   # 从圆桌那三张常量表折进来的
SOURCE_MANUAL = "manual"                           # 活着的 worker 报上来、池子里却没有的

#: ``status_of`` 的三个取值。``unknown`` 不是错误码，是**诚实**：
#: 没登记在哪个 worker 上、或问不到 task 表，就答不出忙闲，不许猜一个。
STATUS_IDLE = "idle"
STATUS_BUSY = "busy"
STATUS_UNKNOWN = "unknown"


@dataclass(frozen=True)
class Member:
    """名册上的一行。

    ``frozen`` 是刻意的：改一个成员只能整条替换掉，于是任何一次改动都必然经过
    ``Roster``，而不是被某个持有引用的调用方就地改掉、别处却还看着旧值。
    """

    agent_id: str            # 稳定名字，点对点消息的收件人就用它
    role: str                # 派单路由用的键
    duty: str                # 逐字取自 AgentIdentity.duty，不另写一份
    title: str = ""          # 人话岗位名（圆桌五岗有，其余留空）
    worker_id: str = ""      # 哪个 worker 装着它；未登记则空
    source: str = ""         # 这条记录哪来的（见上面三个常量）


def roundtable_members() -> list[Member]:
    """把圆桌那三张常量表折成 ``Member``。**只读，不改 team.py。**

    ``duty`` 走 ``team.identity_of(role)`` 而不是在这里另抄一句：那个函数的口径是
    「池子里有真 Agent 就用真的，没有才退到过渡件」，绕过它就等于把过渡件的说法
    钉死在名册上 —— 那两个 Agent 合进来之后两边都不报错，只是对不上。
    """
    from maos.roundtable.team import ROLE_OF, TEAM_ORDER, TITLES, identity_of

    out: list[Member] = []
    for agent_id in TEAM_ORDER:
        role = ROLE_OF[agent_id]
        out.append(Member(
            agent_id=agent_id, role=role, duty=identity_of(role).duty,
            title=TITLES.get(agent_id, ""), source=SOURCE_ROUNDTABLE))
    return out


def _running_worker_ids(store: Any) -> set[str] | None:
    """哪些 worker 名下**此刻**有 RUNNING 任务。``None`` = 问不出来。

    判据取自 ``task`` 表，**不为它新开字段**（口径同 ``runtime/gate.py`` 的
    ``pending()``：在任务行上另开一个字段，就有了第二份事实）。走 store 自己那条
    连接与那把锁 —— 另开连接会绕过全仓唯一的写互斥（先例：``SqliteStorePort``）。

    ``callable(conn)`` 而不是 ``conn is not None``：口径与 ``maos/kb`` 的
    ``port_of()`` 一致。别的后端（PG 端口）也有 ``_conn`` 属性，拿它去跑 sqlite
    专有的调用会当场炸；问不出来就老实回 ``None``，让上层报 ``unknown``。
    """
    conn = getattr(store, "_conn", None)
    if conn is None or not callable(conn):
        return None
    lock = getattr(store, "_lock", None)
    guard: Any = lock if lock is not None else contextlib.nullcontext()
    try:
        with guard:
            rows = conn.execute(
                "SELECT DISTINCT worker_id FROM task"
                " WHERE state=? AND worker_id IS NOT NULL",
                (TaskState.RUNNING,)).fetchall()
    except Exception:                               # noqa: BLE001 —— 见 docstring
        return None
    return {str(row[0]) for row in rows if row[0]}


class Roster:
    """一份成员名册。**内存投影**，不落库、不新开表。

    键是 ``agent_id`` 而不是 ``role``：一个 role 可以有多个成员（两个 worker 各装
    一份），而点对点消息的收件人必须唯一。``AGENT_POOL`` 按 role 索引答不了这个。
    """

    def __init__(self, members: Iterable[Member] = ()) -> None:
        self._members: dict[str, Member] = {}
        for member in members:
            self.add(member)

    # -- 构造 ---------------------------------------------------------------
    @classmethod
    def from_agent_pool(cls) -> Roster:
        """枚举 ``AGENT_POOL`` 生成名册。**不硬编码任何名字。**

        ``import maos.agents`` 而不是 ``maos.agents.base``：前者的 ``__init__``
        会扫描包内全部模块触发 ``@register``，后者只给一个可能还是空的字典。
        """
        from maos.agents import AGENT_POOL

        out = cls()
        for agent_cls in AGENT_POOL.values():
            identity = agent_cls.identity
            out.add(Member(agent_id=identity.agent_id, role=identity.role,
                           duty=identity.duty, source=SOURCE_AGENT_POOL))
        return out

    def merge_roundtable(self) -> Roster:
        """把圆桌名册折进来：认得的**只补 title**，不认得的整条收进来。返回 ``self``。

        补 title 而不整条覆盖，是因为这一行的出处仍然是池子（``source`` 不变）——
        圆桌给的只是一个人话名字，不是另一个身份。
        """
        for seat in roundtable_members():
            have = self._members.get(seat.agent_id)
            if have is None:
                self.add(seat)
            elif not have.title:
                self._members[seat.agent_id] = replace(have, title=seat.title)
        return self

    def add(self, member: Member) -> Member:
        """按 ``agent_id`` 放进名册（同名整条替换）。"""
        self._members[member.agent_id] = member
        return member

    # -- 查询 ---------------------------------------------------------------
    def members(self) -> list[Member]:
        """全部成员，按 ``agent_id`` 排序。

        排序是**确定性**要求，不是审美要求：这份名单会被渲染进群里的回帖、喂给模型
        当【事实】、进证据文件。字典序之外的任何顺序（比如注册顺序）都会随
        「谁先被 import」漂移，而两次跑出来不一样的证据不是证据。
        """
        return [self._members[key] for key in sorted(self._members)]

    def find(self, name: str) -> Member | None:
        """按 ``agent_id`` **或** ``role`` 查。都查不到返回 ``None``，不抛。

        不抛是刻意的：``find`` 的调用方多半是「有人 @ 了一个名字」这种场景，
        名字打错在群里是常态，不该把它变成一次异常。

        按 ``agent_id`` 先查：它是唯一的，而 role 不是（同一个 role 可能挂在两个
        worker 上，见 ``register`` 规则 3）。role 命中多个时取 ``agent_id`` 最小的
        那一个 —— 取「随便一个」会让同一句 ``find`` 两次跑出不同的人。要全部就问
        ``by_role``；``find`` 的契约是「给我一个」，不是「给我那个对的」。
        """
        if not name:
            return None
        hit = self._members.get(name)
        if hit is not None:
            return hit
        same_role = self.by_role(name)
        return same_role[0] if same_role else None

    def by_role(self, role: str) -> list[Member]:
        """该 role 的全部成员，按 ``agent_id`` 排序。一个 role 可能有多个成员。"""
        if not role:
            return []
        return [m for m in self.members() if m.role == role]

    def status_of(self, agent_id: str, store: Any,
                  *, plan_id: str | None = None) -> str:
        """``idle`` / ``busy`` / ``unknown``。**从 task 表推，不另存一份状态字段。**

        判据：该成员所属 ``worker_id`` 名下有 RUNNING 任务即 ``busy``。
        给了 ``plan_id`` 就只看那一个 Plan（走 ``Store`` 协议的 ``list_tasks``，
        任何后端都支持）；不给就跨 Plan 问一次 ``task`` 表。

        三条 ``unknown``：查无此人、这人没登记在任何 worker 上、问不到 task 表。
        它们都不是「闲着」—— 把不知道渲染成 idle，等于给了一个会被当真的假答案。
        """
        member = self.find(agent_id)
        if member is None or not member.worker_id or store is None:
            return STATUS_UNKNOWN
        if plan_id:
            try:
                rows = store.list_tasks(plan_id)
            except Exception:                       # noqa: BLE001 —— 见 docstring
                return STATUS_UNKNOWN
            busy = any(row.get("state") == TaskState.RUNNING
                       and (row.get("worker_id") or "") == member.worker_id
                       for row in rows)
            return STATUS_BUSY if busy else STATUS_IDLE
        running = _running_worker_ids(store)
        if running is None:
            return STATUS_UNKNOWN
        return STATUS_BUSY if member.worker_id in running else STATUS_IDLE

    # -- 登记 ---------------------------------------------------------------
    def _origin_id(self, member: Member) -> str:
        """这一行的「本尊」``agent_id``：分叉行剥掉 ``@<worker>`` 后缀，其余原样。

        分叉行是 ``register`` 规则 3 造出来的副本，**不是另一个人** —— 它和本尊
        指的是同一个 Agent，只是装在别的 worker 上。分不清这件事，``register``
        就会拿副本再分叉一次：第三个 worker 起成员数按 2^(n-1) 膨胀，并造出
        ``refund-intake@w2@w3`` 这种没有对应真实收件人的 ``agent_id`` ——
        而 ``agent_id`` 正是本模块声明的「点对点消息的收件人」，按它发信发不到人。

        判据三个条件同时成立才认，缺一个都会误伤：``source == manual``（池子里
        扫出来的行不可能是副本）、``agent_id`` 带 ``@``、剥掉后缀之后**在册且
        role 相同**。第三条挡的是 ``register(agent_ids=["boss@matrix"])`` 这种
        本来就带 ``@`` 的真名字 —— 那不是副本，不许被切成 ``boss``。
        """
        if member.source != SOURCE_MANUAL or "@" not in member.agent_id:
            return member.agent_id
        base = member.agent_id.split("@", 1)[0]
        origin = self._members.get(base)
        if origin is None or origin.role != member.role:
            return member.agent_id
        return base

    def register(self, worker_id: str, *, roles: Iterable[str] = (),
                 agent_ids: Iterable[str] | None = None) -> list[Member]:
        """登记一个**活着的** worker 装了哪几个 role。返回被登记到的成员。

        三条规则，第三条最容易被误读：

        1. 这一行还没挂 worker（``worker_id == ""``）→ **就地填上**。单 worker 部署
           （全仓今天只有 ``w1``）因此拿到的仍是干净的 ``refund-intake``。
        2. 已经挂在同一个 worker 上 → 不动。``register`` 幂等，重复调不长记录——
           第二个 worker 出现之后也一样：候选只取**本尊**那一行，规则 3 造出来的
           副本不再参与下一轮分叉（见 ``_origin_id``）。
        3. 已经挂在**别的** worker 上 → 从**本尊**那一行**另起一行**，
           ``agent_id`` 是 ``<本尊 agent_id>@<worker_id>``，``source="manual"``。
           n 个 worker 装同一个 role，名册上就恰好 n 行 —— 副本不会再生副本。

        为什么第三条不覆盖：``agent_id`` 是点对点消息的收件人，覆盖等于把发给
        w1 那位的信悄悄改投给 w2。也不能让两行共用一个 ``agent_id`` —— 那样收件人
        就不唯一了。加后缀是唯一能同时守住「收件人唯一」与「两个都在册」的形状；
        代价是 w2 上那位的名字与 w1 上那位不同，而这是真的不同，不是命名瑕疵。

        池子里没有的 role / agent_id 不丢弃，按 ``source="manual"`` 收进来：
        worker 是活着的事实，名册答不出它是谁，比多一行没有 duty 的记录更糟。
        """
        worker_id = (worker_id or "").strip()
        if not worker_id:
            raise ValueError(
                "register 需要一个非空 worker_id —— 登记的是「哪个 worker 装着它」，"
                "而空字符串在 Member 里的含义是「没登记」，两者不是一回事")

        picked: dict[str, Member] = {}
        for role in roles or ():
            same_role = self.by_role(role)
            if not same_role:
                same_role = [self.add(Member(agent_id=role, role=role, duty="",
                                             source=SOURCE_MANUAL))]
            origins = [m for m in same_role if self._origin_id(m) == m.agent_id]
            for member in origins or same_role:
                picked.setdefault(member.agent_id, member)
        for agent_id in agent_ids or ():
            member = self.find(agent_id)
            if member is None:
                member = self.add(Member(agent_id=agent_id, role="", duty="",
                                         source=SOURCE_MANUAL))
            picked.setdefault(member.agent_id, member)

        out: list[Member] = []
        for member in picked.values():
            if member.worker_id == worker_id:       # 规则 2：幂等
                out.append(member)
            elif not member.worker_id:              # 规则 1：就地填
                out.append(self.add(replace(member, worker_id=worker_id)))
            else:                                   # 规则 3：从本尊另起一行
                forked = f"{self._origin_id(member)}@{worker_id}"
                have = self._members.get(forked)
                out.append(have if have is not None else self.add(replace(
                    member, agent_id=forked, worker_id=worker_id,
                    source=SOURCE_MANUAL)))
        return sorted(out, key=lambda m: m.agent_id)

    # -- 容器协议 -----------------------------------------------------------
    def __len__(self) -> int:
        return len(self._members)

    def __iter__(self) -> Iterator[Member]:
        return iter(self.members())

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and self.find(name) is not None


def render_members(members: Iterable[Member]) -> str:
    """把名册排成群里能一眼读完的样子。

    与 ``ingress/router.py`` 的 ``render_roster`` 并存而不是替掉它：那一份排的是
    圆桌五岗**在房间里的样子**（mxid、代言与否、skill 三元组），本份排的是
    **全局有哪些成员**。合成一个的代价是让没接圆桌的进程也去问发声面。
    """
    lines: list[str] = []
    for member in members:
        head = f"{member.title}（{member.agent_id}）" if member.title else member.agent_id
        tail = f" · {member.worker_id}" if member.worker_id else ""
        lines.append(f"{head} · role={member.role}{tail}")
        if member.duty:
            lines.append(f"  职责：{member.duty}")
    return "\n".join(lines)
