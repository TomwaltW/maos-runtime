"""订单系统 ToolPort —— **执行前**去读订单的当前版本。

## 这一层存在的理由

评委原话：「MAOS 在**执行前读取当前版本**，在返回后记录实际观察。」
在 T116 之前，退款域的订单快照全是靶场预置的（`fixtures.seed_case` 灌进
`order_snapshot`），全仓没有任何一条读订单系统的路径 —— 也就是说
「我手上这版快照 vs 外部现在是哪一版」这个问题，代码里压根问不出来。

`order_snapshot` 那张表的抬头写得很清楚（`schema.sql` 第 8 行）：它存的是
**MAOS 执行前读到的那一版**，不是外部系统的当前值。本模块补上的正是「去读」
这个动作本身，于是那句注释第一次有了对应的代码。

## 权威在订单系统那边（铁律 8）

本模块**只读**。它不提供任何改单接口给 MAOS 调 —— 订单的版本、状态、金额都是
外部系统的事实，MAOS 只能观察。`MockOrderSystem.amend()` 是**测试与演示注入
外部改单**用的（模拟"客服在退款跑到一半时改了订单"），它模拟的是**外部世界**
的动作，不是 MAOS 的动作，所以它不在 ToolPort 上 —— 挂上去就等于给了 MAOS
一条改外部事实的路径，那正是铁律 8 禁止的东西。

## 漂移之后不许自己往下走

读到「外部已经是 v2，而我手上是 v1」时，本模块什么都不判 —— 它只如实返回
外部当前那一版。要不要停下来是 `refund.snapshot_check` 的事，而那个 skill 的
处置是**转人工**，不是自动重读快照往下跑：订单被改过意味着退款依据可能变了
（金额、SKU、甚至订单本身被取消），机器接着跑等于拿一份过期的事实去动钱。

## 调用一律走 invoke_tool()

直接调 `MockOrderSystem.query()` 就没有 ToolInvoked 审计行，「执行前真的读了一次」
这句话就只剩自述。上层请走 `invoke_tool(ORDER_QUERY_PORT, {...}, store=...)`。

## params 里只放名字，不放实例

与 `maos/tools/gateway.py` 同一条口径（那份文件的最后一节写了完整理由）：
ToolPort 收 `system_name`，实例由本模块的注册表持有，于是 `params_digest`
天然可复现，也跨得了进程（迁 MCP 的前置条件）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any, Protocol

from maos.tools.port import ToolPort

log = logging.getLogger("maos.tools.order")

#: 订单在外部系统里的状态。演示只用得到前两个，后两个留着让漂移场景有话可说。
ORDER_PAID = "paid"
ORDER_SHIPPED = "shipped"
ORDER_CANCELLED = "cancelled"
ORDER_AMENDED = "amended"

ALL_ORDER_STATUSES = (ORDER_PAID, ORDER_SHIPPED, ORDER_CANCELLED, ORDER_AMENDED)


@dataclass(frozen=True)
class ExternalOrder:
    """外部订单系统里的一版订单 —— **一次读取的记录，不是事实本身**。

    `frozen=True` 与 `GatewayReceipt` 同一个理由：它代表「某一时刻订单系统说了
    什么」，改它等于篡改观察记录。推进版本请用 `replace()` 产生新的一版，
    旧的留在账本里。
    """

    order_id: str
    version: int
    """外部系统的当前版本号。与 `order_snapshot.version` 是同一个维度 ——
    快照记的是"读到的那一版"，这里是"现在是哪一版"。"""

    status: str
    amount: str
    """订单金额。用字符串不用 float —— 金额永远不进浮点（口径同
    `gateway.RefundRequest.refund_amount`）。"""

    updated_at: str
    """外部系统上一次改这笔单的时刻。漂移排查时人要看的第一个字段。"""

    def to_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "version": self.version,
            "status": self.status,
            "amount": self.amount,
            "updated_at": self.updated_at,
        }


class OrderSystemPort(Protocol):
    """订单系统的**一个**动作。签名冻结，上层只按它写代码。

    只有 `query` —— 见模块 docstring：改单是外部世界的动作，不是 MAOS 的。
    """

    def query(self, order_id: str) -> ExternalOrder:
        """读一笔订单的当前版本。查不到抛 `KeyError`，**不返回一个空订单**。"""
        ...


class MockOrderSystem:
    """演示用订单系统。账本在内存里，读出来的永远是"当前那一版"。

    与 `MockGateway` 的关系是对称的：那个 mock 的时序不是假的（终态必须问出来），
    这个 mock 的**版本**不是假的（外部改一次单，版本就真的推高一格，
    MAOS 手上那份快照因此真的过期）。一个恒返回 v1 的 mock 会让漂移检查
    永远检不出东西 —— 那样"执行前读当前版本"就又变回一句自述。
    """

    def __init__(self, orders: dict[str, ExternalOrder] | None = None) -> None:
        self._orders: dict[str, ExternalOrder] = dict(orders or {})

    # ------------------------------------------------------------------ 建账本
    def ext_order(self, order_id: str, version: int, status: str, amount: str,
                  updated_at: str) -> ExternalOrder:
        """登记（或整体覆盖）一笔外部订单，返回它。

        `status` 必须在 :data:`ALL_ORDER_STATUSES` 里 —— 未收录的状态不许兜底成
        「大概是 paid」，口径同 `gateway_codes.lookup()` 对未知码的处置：
        兜底会让一个打错的字符串静默变成一个看起来正常的状态。
        """
        if status not in ALL_ORDER_STATUSES:
            raise ValueError(
                f"未收录的订单状态 {status!r}（已收录：{list(ALL_ORDER_STATUSES)}）")
        if int(version) < 1:
            raise ValueError(f"订单版本号从 1 起，实际 {version!r}")
        order = ExternalOrder(order_id=str(order_id), version=int(version),
                              status=status, amount=str(amount),
                              updated_at=str(updated_at))
        self._orders[order.order_id] = order
        return order

    def amend(self, order_id: str, *, status: str | None = None,
              amount: str | None = None, updated_at: str | None = None) -> ExternalOrder:
        """**注入一次外部改单** —— 版本推高一格，返回新的那一版。

        这是漂移场景的唯一入口（`scripts/run_case.py --drift` 与测试都靠它）。
        它模拟的是外部世界的动作，**不在 ToolPort 上**：挂上去就等于给了 MAOS
        一条改外部事实的路径（铁律 8）。

        版本恒 +1 而不是由调用方指定：外部系统的版本号怎么排是它自己的事，
        让测试挑一个数字会让"漂移"变成一个可以调参调没的东西。
        """
        current = self._orders.get(str(order_id))
        if current is None:
            raise KeyError(f"订单系统里没有这笔单：{order_id!r}")
        if status is not None and status not in ALL_ORDER_STATUSES:
            raise ValueError(
                f"未收录的订单状态 {status!r}（已收录：{list(ALL_ORDER_STATUSES)}）")
        nxt = replace(
            current,
            version=current.version + 1,
            status=status if status is not None else current.status,
            amount=str(amount) if amount is not None else current.amount,
            updated_at=str(updated_at) if updated_at is not None else current.updated_at,
        )
        self._orders[nxt.order_id] = nxt
        log.info("外部改单 %s：v%d -> v%d", nxt.order_id, current.version, nxt.version)
        return nxt

    # ------------------------------------------------------------------ 读
    def query(self, order_id: str) -> ExternalOrder:
        """读当前那一版。查不到就抛。

        **不兜底成「返回一个 v1 的空订单」**：那会让「订单系统里没有这笔单」
        （一个真实且严重的情况 —— 订单可能被删了、或者 order_id 打错了）
        伪装成「版本一致，放行」，而漂移检查随即报 `drift=false` 放行付款。
        """
        order = self._orders.get(str(order_id))
        if order is None:
            raise KeyError(
                f"订单系统里没有这笔单：{order_id!r}（已登记：{sorted(self._orders)}）")
        return order


# ---------------------------------------------------------------------------
# 工具侧注册表 —— params 只带名字，实例由**工具这一侧**持有
# ---------------------------------------------------------------------------
# 完整理由见 `maos/tools/gateway.py` 同名那一节：活对象进 `params_digest` 就要靠
# 每个实现自己写一个不带内存地址的 `__repr__` 来维持稳定性，那是打补丁不是机制。
# 按名取之后 params 全是标量，digest 天然可复现，也跨得了进程。

_SYSTEMS: dict[str, Any] = {}

DEFAULT_ORDER_SYSTEM = "demo-orders"


def register_order_system(name: str, system: Any) -> Any:
    """把一个订单系统实现登记成一个名字，供 ToolPort 按名取用。"""
    _SYSTEMS[str(name)] = system
    return system


def get_order_system(name: str | None = None) -> Any:
    """按名取订单系统。**取不到就抛，不兜底成一个空的 mock。**

    口径与 `gateway.get_gateway` 一致：自动兜底会把「忘了注册」变成「悄悄用了
    一个空账本」—— 而空账本上 `query()` 抛 KeyError，漂移检查会把它当成
    「订单不存在」报出来，排查方向当场偏到订单数据上，而真正的问题在装配处。
    """
    key = str(name or DEFAULT_ORDER_SYSTEM)
    system = _SYSTEMS.get(key)
    if system is None:
        raise LookupError(
            f"工具侧没有登记名为 {key!r} 的订单系统（已登记：{sorted(_SYSTEMS)}）；"
            "请在装配处调用 maos.tools.order.register_order_system(name, MockOrderSystem())"
        )
    return system


def reset_order_systems() -> None:
    """清空登记表 —— 只给测试用，保证用例之间不互相串账本。"""
    _SYSTEMS.clear()


# ---------------------------------------------------------------------------
# ToolPort 声明（A-6 九要素）—— 调用一律走 invoke_tool()，直接调没有审计行
# ---------------------------------------------------------------------------

def order_query(*, system_name: str, order_id: str) -> dict:
    """ToolPort 入口：读一笔订单在外部系统里的当前版本。

    入参**全是标量**（订单系统只给名字，不给实例）：`invoke_tool` 会把 params
    做 sha256 进审计行，标量化之后 digest 才对得上「同样的参数」这个直觉。
    """
    return get_order_system(system_name).query(order_id).to_dict()


ORDER_QUERY_PORT = ToolPort(
    name="order.query",
    purpose="执行前读订单系统里的**当前版本** —— 用来比对 MAOS 手上那份快照有没有过期",
    entry=order_query,
    params_schema={"system_name": "str（已 register_order_system 的名字；实例由工具侧持有）",
                   "order_id": "str"},
    returns_schema={"order_id": "str", "version": "int（外部系统的当前版本）",
                    "status": "paid|shipped|cancelled|amended",
                    "amount": "str（金额不进浮点）",
                    "updated_at": "str（外部上次改这笔单的时刻）"},
    failure_modes=[
        "LookupError: system_name 没有登记过 —— **不兜底成默认订单系统**",
        "KeyError: 订单系统里没有这笔单 —— **不兜底成一个 v1 的空订单**，"
        "那会把「订单不见了」伪装成「版本一致，放行」",
        "version 高于手上的快照：**不是本工具的错**，是漂移。判据在 "
        "refund.snapshot_check，处置是转人工，不是自动重读快照往下跑",
    ],
    security_boundary=(
        "只读。MAOS 不持有订单的权威事实（铁律 8），本工具只产生**观察记录**："
        "没有任何改单入口挂在 ToolPort 上（MockOrderSystem.amend 模拟的是外部世界"
        "的动作，只给测试与演示注入用）；读到的版本一律如实返回，"
        "本工具不判漂移、不改任何业务状态"
    ),
    rate_limit="",
    owner="task-t116",
)
