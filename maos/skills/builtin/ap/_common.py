"""应付账款域六个 skill 的共用件 —— 只放**跨 skill 复用的机制**，不放业务判定。

下划线开头，`builtin/__init__.py::discover()` 扫不到它（`mod.name.startswith("_")`
跳过），本包的 `__init__.py` 显式 import 各 skill 模块，所以它永远不会被误当成
一个 skill。

四件事（artifact 形状不在其列，那是 Agent 的职责，见 `maos/agents/ap/_base.py`）：

1. **建表**：本域的 14 张表由 `objects.ensure_schema()` 建，幂等。每个写库的 skill
   在 run() 开头调一次 —— 不假设「场景已经建过了」，单测直接调某个 skill 也要能跑。

2. **invocation_id**：`guard.update_biz_status()` 要求非空的 actor 锚点。
   `SkillInvoker` 生成的那个**会**进 `SkillContext.extras`（invoker.py 里那段
   「invocation_id 必须进 extras」），所以正常路径下直接取得到；取不到才本地生成，
   两种情况都保证非空，且一律回填进 output，让 artifact 与库里那行对得上号。

3. **银行按名取**：`task.inputs` 会被 `store.insert_task` 做 `json.dumps`，
   `MockBank` 实例塞不进去。所以进程内维护一张 name -> 银行 的表，任务只带名字。
   这不是全局单例的偷懒写法 —— 换成真网银适配器时注册一行就切完，上层 skill 一个
   字不用改。**取不到就抛，不自动造一个 MockBank**：自动兜底会让「忘了注册银行」
   变成「悄悄用了一个空账本的 mock」，幂等、轮询次数全部失真，而表面上一路绿灯。

4. **审批记录**：`payment_approval` 由**人**的决定写入（本轮走 Matrix 房间），
   `ap.execute` 只读不写 —— 让付款方自己写下「我被批准了」，等于没有审批。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from maos.domain.ap import objects

#: artifact 里挂银行回单的键名，从 tools 层取，不在这里另抄一份字面量。
#: 为什么不能叫 `receipt`：见 `maos/tools/ap.py` 模块 docstring。
from maos.tools.ap import ADVICE_FIELD

#: 业务类型标记。本域的任务在 `task.inputs["biz_type"]` 上带它。
#:
#: **刻意不是 "refund"**：`maos/runtime/gate.py` 的第六道财务复核闸按
#: `inputs["biz_type"] == FINANCE_BIZ_TYPE`（= "refund"）触发，判据是同 attempt 的
#: 产物里有没有 `finance_entry`。本域产不出那种凭据，冒用那个标记会让闸恒 blocker，
#: 而报错信息会指向退款域的财务复核，离原因极远。
#: 本域自己的「金额要人批」由 `effect_risk=H` 的人工审批入口把守。
BIZ_TYPE = "ap"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(obj: Any) -> str:
    try:
        raw = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        raw = repr(obj)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 建表与锚点
def ensure_schema(ctx: Any) -> Any:
    """取 store 并保证本域的表已建好；没有 store 直接抛。

    不兜底成「没有 store 就跳过写库」：那会让 skill 报 ok 而一行数据都没落，
    是这条链路上最容易造出的假绿 —— 而本轨要买的正是「Agent 说完成了 ≠ 业务成功」。
    """
    store = getattr(ctx, "store", None)
    if store is None:
        raise ValueError("应付账款域 skill 必须在有 store 的上下文里跑：业务对象无处落库")
    objects.ensure_schema(store)
    return store


def invocation_id_of(ctx: Any) -> str:
    """本次调用的 actor 锚点。invoker 给了就用它的，没给就本地生成，**恒非空**。

    兜底成空字符串等于让整条审计链断掉 —— `guard._require_invocation_id` 空了就抛。
    """
    extras = getattr(ctx, "extras", None) or {}
    return str(extras.get("invocation_id") or "").strip() or uuid.uuid4().hex


def tool_extras(ctx: Any) -> dict:
    """给 `invoke_tool` 的审计上下文。缺哪个就是空，不编。"""
    extras = getattr(ctx, "extras", None) or {}
    return {
        "plan_id": extras.get("plan_id", ""),
        "task_id": extras.get("task_id"),
        "trace_id": extras.get("trace_id", ""),
    }


# ---------------------------------------------------------------- 银行按名取
_BANKS: dict[str, Any] = {}

DEFAULT_BANK = "demo"


def register_bank(name: str, bank: Any) -> Any:
    """把一个银行实现登记成一个名字。场景/测试在装配时调一次。"""
    _BANKS[str(name)] = bank
    return bank


def get_bank(name: str | None = None) -> Any:
    """按名取银行。取不到就抛 —— 见模块 docstring 第 3 条。"""
    key = str(name or DEFAULT_BANK)
    bank = _BANKS.get(key)
    if bank is None:
        raise LookupError(
            f"没有登记名为 {key!r} 的银行（已登记：{sorted(_BANKS)}）；"
            "请在装配处调用 register_bank(name, MockBank(...))"
        )
    return bank


def reset_banks() -> None:
    """清空登记表 —— 只给测试与场景用，保证用例之间不互相串账本。"""
    _BANKS.clear()


# ---------------------------------------------------------------- 审批落库
def record_approval(store: Any, *, tenant_id: str, case_id: str, approver: str,
                    decision: str, reason: str = "") -> dict:
    """把一次主管审批落进 `payment_approval`。

    刻意放在这里而不是某个 skill 里：审批是**人**做的动作，发生在 Matrix 房间或
    CLI 里，不是哪个 Agent 跑出来的。`ap.execute` 只读它、不写它。
    """
    if decision not in ("approved", "rejected"):
        raise ValueError(f"审批结论只能是 approved / rejected，实际 {decision!r}")
    row = {
        "tenant_id": tenant_id, "case_id": case_id, "approver": approver,
        "decision": decision, "reason": reason, "decided_at": now_iso(),
    }
    objects.execute(
        store,
        "INSERT OR REPLACE INTO payment_approval (tenant_id, case_id, approver, decision,"
        " reason, decided_at) VALUES (?,?,?,?,?,?)",
        (row["tenant_id"], row["case_id"], row["approver"], row["decision"],
         row["reason"], row["decided_at"]),
    )
    return row


def approvals_of(store: Any, *, tenant_id: str, case_id: str,
                 decision: str = "approved") -> list[dict]:
    return objects.query(
        store,
        "SELECT * FROM payment_approval WHERE tenant_id=? AND case_id=? AND decision=?"
        " ORDER BY decided_at",
        (tenant_id, case_id, decision),
    )


# ---------------------------------------------------------------- 幂等键
def idempotency_key(tenant_id: str, case_id: str) -> str:
    """付款指令的幂等键 —— 由 (租户, 案子) 唯一确定。

    **一张发票只允许有一笔付款**，所以键不带时间戳、不带 attempt：返工重跑必须
    落在同一个键上，否则第二次执行就会付出第二笔。`payment_instruction` 表上的
    `UNIQUE (tenant_id, idempotency_key)` 是同一件事的第二道防线。
    """
    return f"ap:{tenant_id}:{case_id}"


# ---------------------------------------------------------------- 入参小工具
def required(payload: dict, *keys: str) -> tuple:
    """取必填入参，缺一个就抛。

    `SkillContract.preconditions` 只查「键存在且非 None」，空字符串照样过；
    而 tenant_id 空字符串会让写库落到一个谁也读不到的租户下，是静默的错。
    """
    out = []
    missing = []
    for key in keys:
        value = payload.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(key)
        out.append(value)
    if missing:
        raise ValueError(f"缺必填入参：{missing}")
    return tuple(out)
