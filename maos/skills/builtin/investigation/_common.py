"""差错处理域五个 skill 的共用件 —— 只放**跨 skill 复用的机制**，不放业务判定。

下划线开头，`builtin/__init__.py::discover()` 扫不到它（`mod.name.startswith("_")` 跳过），
本包的 `__init__.py` 显式 import 各 skill 模块，所以它永远不会被误当成一个 skill。

四件事（artifact 形状不在其列，那是 Agent 的职责，见 `maos/agents/investigation/_base.py`）：

1. **建表**：本域的 6 张表由 `objects.ensure_schema()` 建，幂等。每个写库的 skill
   在 run() 开头调一次 —— 不假设「场景已经建过了」，单测直接调某个 skill 也要能跑。

2. **invocation_id**：`guard.update_biz_status()` 要求非空的 actor 锚点，而
   `SkillInvoker` 生成的那个 id **进不到 skill 里**（它生成后只放进 SkillResult 与
   落库那行，没有塞进 `SkillContext.extras`）。invoker.py 不是本轨的文件，不能为此改它。
   所以口径定成：**调用方经 extras 传入，传不到则本地生成**，两种情况都保证非空，
   且一律回填进 output。口径与退款域 `builtin/refund/_common.py` 第 2 条逐字相同。

3. **清算方按名取**：`task.inputs` 会被 `store.insert_task` 做 `json.dumps`，
   MockClearingHouse 实例塞不进去。所以进程内维护一张 name -> 清算方 的表，
   任务只带名字。换成真 SWIFT 适配器时注册一行就切完，上层 skill 一个字不用改。

4. **调账审批**：`adjustment_approval` 由**人**的决定写入（本轮走 Matrix 房间），
   任何 skill 只读不写 —— 让调账方自己写下「我被批准了」，等于没有审批。
   差错处理域里这一条是**硬监管要求**，不是产品选项。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from maos.domain.investigation import objects

#: 业务类型标记。场景与测试从这里取，不在各处写字面量。
#: 与退款域的 `BIZ_TYPE = "refund"` 并列，用来在共用事件里分流是哪个域。
BIZ_TYPE = "investigation"


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
    是这条链路上最容易造出的假绿。
    """
    store = getattr(ctx, "store", None)
    if store is None:
        raise ValueError("差错处理域 skill 必须在有 store 的上下文里跑：业务对象无处落库")
    objects.ensure_schema(store)
    return store


def invocation_id_of(ctx: Any) -> str:
    """本次调用的 actor 锚点。调用方给了就用调用方的，没给就本地生成，**恒非空**。

    见模块 docstring 第 2 条：invoker 那个 id 到不了 skill 里，而
    `guard._require_invocation_id` 空了就抛 —— 兜底成空字符串等于让整条审计链断掉。
    """
    extras = getattr(ctx, "extras", None) or {}
    return str(extras.get("invocation_id") or "").strip() or uuid.uuid4().hex


# ------------------------------------------------------------ 清算方按名取
_CLEARING: dict[str, Any] = {}

DEFAULT_CLEARING = "demo"


def register_clearing(name: str, clearing: Any) -> Any:
    """把一个清算方实现登记成一个名字。场景/测试在装配时调一次。"""
    _CLEARING[str(name)] = clearing
    return clearing


def get_clearing(name: str | None = None) -> Any:
    """按名取清算方。取不到就抛，**不自动造一个 MockClearingHouse** ——

    自动兜底会让「忘了注册清算方」变成「悄悄用了一个空账本的 mock」：幂等、问询次数、
    剧本注入全部失真，而表面上一路绿灯。这种失效只会在演示现场暴露。
    """
    key = str(name or DEFAULT_CLEARING)
    clearing = _CLEARING.get(key)
    if clearing is None:
        raise LookupError(
            f"没有登记名为 {key!r} 的清算方（已登记：{sorted(_CLEARING)}）；"
            "请在装配处调用 register_clearing(name, MockClearingHouse(...))")
    return clearing


def reset_clearing() -> None:
    """清空登记表 —— 只给测试用，保证用例之间不互相串账本。"""
    _CLEARING.clear()


# ---------------------------------------------------------------- 调账审批
def record_adjustment_approval(store: Any, *, tenant_id: str, case_id: str,
                               approver: str, decision: str, reason: str = "") -> dict:
    """把一次人工调账审批落进 `adjustment_approval`。

    刻意放在这里而不是某个 skill 里：审批是**人**做的动作，发生在 Matrix 房间里，
    不是哪个 Agent 跑出来的。差错处理域的人工调账必须有人批是**监管要求**，
    HITL 闸不是可选项 —— 所以这条记录的写入方必须与读取方分开。
    """
    if decision not in ("approved", "rejected"):
        raise ValueError(f"审批结论只能是 approved / rejected，实际 {decision!r}")
    row = {
        "tenant_id": tenant_id, "case_id": case_id, "approver": approver,
        "decision": decision, "reason": reason, "decided_at": now_iso(),
    }
    objects.execute(
        store,
        "INSERT OR REPLACE INTO adjustment_approval (tenant_id, case_id, approver,"
        " decision, reason, decided_at) VALUES (?,?,?,?,?,?)",
        (row["tenant_id"], row["case_id"], row["approver"], row["decision"],
         row["reason"], row["decided_at"]),
    )
    return row


def adjustment_approvals(store: Any, *, tenant_id: str, case_id: str,
                         decision: str = "approved") -> list[dict]:
    return objects.query(
        store,
        "SELECT * FROM adjustment_approval WHERE tenant_id=? AND case_id=? AND decision=?"
        " ORDER BY decided_at",
        (tenant_id, case_id, decision),
    )


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
