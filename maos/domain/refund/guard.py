"""settled guard —— 退款案例的权威事实边界（铁律 8）。

题眼：**MAOS 不持有权威事实**。一笔退款到没到账，权威在支付网关，不在我们库里。
所以 `settled` 这一个终态，全系统只有 `payment.observe` 这一个 skill 写得进去，
而且必须同事务附上它读到的那份回执（`payment_observation`）——
没有回执的 settled 就是「把外部状态直接写死为终态」，那是 bug 不是功能。

越权写入**不静默失败**：抛 `AuthoritativeFactViolation` + 落一条事件。
理由与 `scripts/guard_bash.py` 同：「系统拒绝了一次越权写入」本身就是要拿给评委看的
证据，吞掉就没了。

`refund_case` 的一切写入只有两个入口 —— `create_case()` 建、`update_biz_status()` 改，
不留第三条路径。旁路自查见派单步骤 5：
    grep -rn "biz_status.*=.*'settled'" maos/ | grep -v guard.py | grep -v observe
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------- 冻结常量
# R-2 的六个 skill 按名 import 这三个，改动等于破坏跨轨契约。
AUTHORITATIVE_WRITER = "payment.observe"

#: 只有 AUTHORITATIVE_WRITER 写得进来的状态集合。
#: 现在只有 settled；将来若有第二个「外部说了才算」的终态，加进这里而不是散在判断里。
AUTHORITATIVE_STATES = frozenset({"settled"})

#: 业务状态机（**不是** Task 状态机）：主干三段 + 两个分支。
BIZ_STATUS_FLOW: dict[str, tuple[str, ...]] = {
    "submitted":        ("approved", "rejected"),
    "approved":         ("gateway_accepted", "rejected", "compensated"),
    "gateway_accepted": ("processing", "compensated"),
    "processing":       ("settled", "compensated"),
    "settled":          (),
    "rejected":         (),
    "compensated":      (),
}

INITIAL_STATUS = "submitted"


class AuthoritativeFactViolation(RuntimeError):
    """非权威写入方试图写入权威终态。

    定义在本模块而不是 `contracts/` —— contracts 是冻结面，且这是退款域自己的
    业务规则，不是内核契约。
    """


class BizStatusTransitionError(ValueError):
    """业务状态迁移不在 `BIZ_STATUS_FLOW` 里。"""


# ---------------------------------------------------------------- 冻结签名
# 以下签名在 T+0 commit 冻结，R-2 从这个 sha 起可以按名调用。
# 位置参数顺序照派单原文，扩展一律走 keyword-only。

def create_case(
    store: Any,
    *,
    tenant_id: str,
    case_id: str,
    channel_id: str,
    order_id: str,
    order_version: int,
    sku: str,
    reason_code: str,
    amount_claimed: float,
    plan_id: str,
    actor_skill: str,
    invocation_id: str,
) -> dict:
    """建一个 refund_case，落 `submitted`。这是本表唯一的插入口径。"""
    raise NotImplementedError


def update_biz_status(
    store: Any,
    tenant_id: str,
    case_id: str,
    new_status: str,
    actor_skill: str,
    invocation_id: str,
    *,
    observation: dict | None = None,
    reason: str = "",
) -> dict:
    """`refund_case.biz_status` 的唯一写入路径。

    - `new_status` 落在 `AUTHORITATIVE_STATES` 且 `actor_skill != AUTHORITATIVE_WRITER`
      → 落 `AuthoritativeFactViolation` 事件并抛 `AuthoritativeFactViolation`。
    - 写 `settled` 必须带 `observation`，与状态更新**同事务**插入 `payment_observation`。
    - 迁移不在 `BIZ_STATUS_FLOW` 里 → 抛 `BizStatusTransitionError`。
    """
    raise NotImplementedError


def get_case(store: Any, tenant_id: str, case_id: str) -> dict | None:
    """按 (tenant_id, case_id) 读一个 case；不存在返回 None。"""
    raise NotImplementedError
