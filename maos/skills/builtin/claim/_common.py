"""理赔域六个 skill 的共用件 —— 只放**跨 skill 复用的机制**，不放业务判定。

下划线开头，`builtin/__init__.py::discover()` 扫不到它（`mod.name.startswith("_")` 跳过），
本包的 `__init__.py` 显式 import 各 skill 模块，所以它永远不会被误当成一个 skill。

四件事（artifact 形状不在其列，那是 Agent 的职责，见 `maos/agents/claim/_base.py`）：

1. **建表**：理赔域的 12 张表由 `objects.ensure_schema()` 建，幂等。每个写库的 skill
   在 run() 开头调一次 —— 不假设「场景已经建过了」，单测直接调某个 skill 也要能跑。

2. **invocation_id**：`guard.update_biz_status()` 要求非空的 actor 锚点，而
   `SkillInvoker` 生成的那个 id **进不到 skill 里**（invoker.py 生成后只放进
   SkillResult 与落库那行，没有塞进 `SkillContext.extras`）。invoker.py 不是本轨的
   文件，不能为此去改它。所以口径定成：**调用方经 extras 传入，传不到则本地生成**，
   两种情况都保证非空，且一律回填进 output，让 artifact 与库里那行对得上号。

3. **赔付方按名取**：`task.inputs` 会被 `store.insert_task` 做 `json.dumps`，
   MockPayer 实例塞不进去。所以进程内维护一张 name -> 赔付方 的表，任务只带名字。
   这不是全局单例的偷懒写法 —— 换成真实赔付方适配器时，注册一行就切完，
   上层 skill 一个字不用改。

4. **审批记录**：`claim_approval` 由**人**的决定写入（CLI 或 Matrix 房间），
   `claim.pay` 只读不写 —— 让付款方自己写下「我被批准了」，等于没有审批。

口径与 `maos/skills/builtin/refund/_common.py` 同构而**不共用**：两个域各带一份
注册表，才不会出现「测试里 reset 了退款域的网关，顺手把理赔域的赔付方也清了」
这种跨域串味。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from maos.domain.claim import objects

#: 业务类型标记。理赔任务的 `inputs["biz_type"]` 取这个值。
#:
#: 🔴 **刻意不是 "refund"**：`maos/runtime/gate.py` 的第六道财务复核闸按
#: `inputs["biz_type"] == "refund"`（`FINANCE_BIZ_TYPE`）触发，判据是同 attempt 的产物
#: 里有没有 `finance_entry` 键 —— 那是退款域的产物形状，理赔域根本不产出它。
#: 冒用 "refund" 会让本域每个带金额的任务都被要求交一份它产不出的凭据，闸恒 blocker，
#: 而报错信息会指向「退款」，离原因极远。
#:
#: 本域的高风险放行走的是**既有的 `effect_risk=H` 人工审批入口**（control_plane 在
#: ReviewVerdict=pass 时把它路由到 BLOCKED），那条路不看 biz_type，与域无关。
BIZ_TYPE = "claim"


#: 赔款超过这个数就必须停下来等人批。可由 `MAOS_CLAIM_APPROVAL_THRESHOLD` 覆盖。
#:
#: 判据放在域里、不放在场景里：场景是演示，改一次演示不该改变业务规则；而测试要能
#: 单独校验「多少钱以上要人批」这件事本身。**现读环境变量**而不是 import 时固化，
#: 口径同 `gate._finance_threshold` —— 改一次阈值不必重启进程。
DEFAULT_APPROVAL_THRESHOLD = 5000.0
ENV_APPROVAL_THRESHOLD = "MAOS_CLAIM_APPROVAL_THRESHOLD"


def approval_threshold() -> float:
    """当前的人工审批阈值。环境变量解析不出数就回落缺省，**不抛**。

    不抛的理由：这个函数挂在派单路径上，一个写错的环境变量不该让整条链路起不来；
    而回落到缺省是**更严**的一侧（缺省 5000 比任何更大的值都更容易触发人工审批），
    fail-closed。
    """
    import os
    raw = (os.environ.get(ENV_APPROVAL_THRESHOLD) or "").strip()
    if not raw:
        return DEFAULT_APPROVAL_THRESHOLD
    try:
        return float(raw)
    except (TypeError, ValueError):
        return DEFAULT_APPROVAL_THRESHOLD


def needs_human_approval(amount: Any) -> bool:
    """这笔赔款要不要停下来等人批。

    **解析不出数 = 要人批，不是放过**（fail-closed，口径同
    `gate._over_finance_threshold`）：一个 `None` 或 `"一万二"` 说明上游把金额弄丢了，
    那种案子更该让人看一眼，不该因为「读不出数」就自动放行。
    """
    try:
        return float(amount) > approval_threshold()
    except (TypeError, ValueError):
        return True


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
    """取 store 并保证理赔域的表已建好；没有 store 直接抛。

    不兜底成「没有 store 就跳过写库」：那会让 skill 报 ok 而一行数据都没落，
    是这条链路上最容易造出的假绿。
    """
    store = getattr(ctx, "store", None)
    if store is None:
        raise ValueError("理赔域 skill 必须在有 store 的上下文里跑：业务对象无处落库")
    objects.ensure_schema(store)
    return store


def invocation_id_of(ctx: Any) -> str:
    """本次调用的 actor 锚点。调用方给了就用调用方的，没给就本地生成，**恒非空**。

    见模块 docstring 第 2 条：SkillInvoker 那个 id 到不了 skill 里，而
    `guard._require_invocation_id` 空了就抛 —— 兜底成空字符串等于让整条审计链断掉。
    """
    extras = getattr(ctx, "extras", None) or {}
    return str(extras.get("invocation_id") or "").strip() or uuid.uuid4().hex


def tool_extras(ctx: Any) -> dict:
    """给 `invoke_tool` 的归属字段。三处 skill 都要，集中在这里不各抄一遍。"""
    extras = getattr(ctx, "extras", None) or {}
    return {
        "plan_id": extras.get("plan_id", ""),
        "task_id": extras.get("task_id"),
        "trace_id": extras.get("trace_id", ""),
    }


# ---------------------------------------------------------------- 赔付方按名取
_PAYERS: dict[str, Any] = {}

DEFAULT_PAYER = "demo"


def register_payer(name: str, payer: Any) -> Any:
    """把一个赔付方实现登记成一个名字。场景/测试在装配时调一次。"""
    _PAYERS[str(name)] = payer
    return payer


def get_payer(name: str | None = None) -> Any:
    """按名取赔付方。取不到就抛，**不自动造一个 MockPayer** ——

    自动兜底会让「忘了注册赔付方」变成「悄悄用了一个空账本的 mock」：幂等、轮询次数、
    码值注入全部失真，而表面上一路绿灯。这种失效只会在演示现场暴露。
    """
    key = str(name or DEFAULT_PAYER)
    payer = _PAYERS.get(key)
    if payer is None:
        raise LookupError(
            f"没有登记名为 {key!r} 的赔付方（已登记：{sorted(_PAYERS)}）；"
            "请在装配处调用 register_payer(name, MockPayer(...))")
    return payer


def reset_payers() -> None:
    """清空登记表 —— 只给测试用，保证用例之间不互相串账本。"""
    _PAYERS.clear()


# ---------------------------------------------------------------- 审批落库
def record_approval(store: Any, *, tenant_id: str, claim_id: str, approver: str,
                    decision: str, reason: str = "") -> dict:
    """把一次主管审批落进 `claim_approval`。

    刻意放在这里而不是某个 skill 里：审批是**人**做的动作，发生在 CLI 或
    Matrix 房间里，不是哪个 Agent 跑出来的。`claim.pay` 只读它、不写它 ——
    让付款方自己写下「我被批准了」，等于没有审批。
    """
    if decision not in ("approved", "rejected"):
        raise ValueError(f"审批结论只能是 approved / rejected，实际 {decision!r}")
    row = {
        "tenant_id": tenant_id, "claim_id": claim_id, "approver": approver,
        "decision": decision, "reason": reason, "decided_at": now_iso(),
    }
    objects.execute(
        store,
        "INSERT OR REPLACE INTO claim_approval (tenant_id, claim_id, approver, decision,"
        " reason, decided_at) VALUES (?,?,?,?,?,?)",
        (row["tenant_id"], row["claim_id"], row["approver"], row["decision"],
         row["reason"], row["decided_at"]),
    )
    return row


def approvals_of(store: Any, *, tenant_id: str, claim_id: str,
                 decision: str = "approved") -> list[dict]:
    return objects.query(
        store,
        "SELECT * FROM claim_approval WHERE tenant_id=? AND claim_id=? AND decision=?"
        " ORDER BY decided_at",
        (tenant_id, claim_id, decision),
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
