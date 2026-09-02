"""退款域六个 skill 的共用件 —— 只放**跨 skill 复用的机制**，不放业务判定。

下划线开头，`builtin/__init__.py::discover()` 扫不到它（`mod.name.startswith("_")` 跳过），
本包的 `__init__.py` 显式 import 各 skill 模块，所以它永远不会被误当成一个 skill。

四件事（artifact 形状不在其列，那是 Agent 的职责，见 `maos/agents/refund/_base.py`）：

1. **建表**：退款域的 14 张表由 `objects.ensure_schema()` 建，幂等。每个写库的 skill
   在 run() 开头调一次 —— 不假设「场景已经建过了」，单测直接调某个 skill 也要能跑。

2. **invocation_id**：`guard.update_biz_status()` 要求非空的 actor 锚点，而
   `SkillInvoker` 生成的那个 id **进不到 skill 里**（invoker.py:69 生成后只放进
   SkillResult 与落库那行，没有塞进 `SkillContext.extras`）。invoker.py 不是本轨的文件，
   不能为此去改它。所以口径定成：**调用方经 extras 传入，传不到则本地生成**，
   两种情况都保证非空，且一律回填进 output，让 artifact 与库里那行对得上号。
   （已记 docs/DECISIONS.md）

3. **网关按名取**：`task.inputs` 会被 `store.insert_task` 做 `json.dumps`（store.py:198），
   MockGateway 实例塞不进去。所以进程内维护一张 name -> 网关 的表，任务只带名字。
   这不是全局单例的偷懒写法 —— 换成真支付宝适配器时，注册一行就切完，
   上层 skill 一个字不用改。

4. **审批记录**：`approval_record` 由**人**的决定写入（本轮走 CLI），
   `payment.execute` 只读不写 —— 让付款方自己写下「我被批准了」，等于没有审批。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from maos.domain.refund import objects

#: 业务类型标记。R-0 的第六道闸按 `task["inputs"]["biz_type"] == "refund"` 触发（F-1），
#: 场景与测试都从这里取，不在各处写字面量。
#:
#: artifact 的 kind 常量与信封在 `maos/agents/refund/_base.py` —— 产物形状是 Agent 的
#: 职责（skill 只返回原始 output，包成 artifact 是调用方的事，见 contract.py 的 Skill
#: 基类注释），放在这里会让两层的边界糊掉。
BIZ_TYPE = "refund"


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
    """取 store 并保证退款域的表已建好；没有 store 直接抛。

    不兜底成「没有 store 就跳过写库」：那会让 skill 报 ok 而一行数据都没落，
    是这条链路上最容易造出的假绿。
    """
    store = getattr(ctx, "store", None)
    if store is None:
        raise ValueError("退款域 skill 必须在有 store 的上下文里跑：业务对象无处落库")
    objects.ensure_schema(store)
    return store


def invocation_id_of(ctx: Any) -> str:
    """本次调用的 actor 锚点。调用方给了就用调用方的，没给就本地生成，**恒非空**。

    见模块 docstring 第 2 条：SkillInvoker 那个 id 到不了 skill 里，而
    `guard._require_invocation_id` 空了就抛 —— 兜底成空字符串等于让整条审计链断掉。
    """
    extras = getattr(ctx, "extras", None) or {}
    return str(extras.get("invocation_id") or "").strip() or uuid.uuid4().hex


# ---------------------------------------------------------------- 网关按名取
_GATEWAYS: dict[str, Any] = {}

DEFAULT_GATEWAY = "demo"


def register_gateway(name: str, gateway: Any) -> Any:
    """把一个网关实现登记成一个名字。场景/测试在装配时调一次。"""
    _GATEWAYS[str(name)] = gateway
    return gateway


def get_gateway(name: str | None = None) -> Any:
    """按名取网关。取不到就抛，**不自动造一个 MockGateway** ——

    自动兜底会让「忘了注册网关」变成「悄悄用了一个空账本的 mock」：幂等、轮询次数、
    错误注入全部失真，而表面上一路绿灯。这种失效只会在演示现场暴露。
    """
    key = str(name or DEFAULT_GATEWAY)
    gateway = _GATEWAYS.get(key)
    if gateway is None:
        raise LookupError(
            f"没有登记名为 {key!r} 的支付网关（已登记：{sorted(_GATEWAYS)}）；"
            "请在装配处调用 register_gateway(name, MockGateway(...))"
        )
    return gateway


def reset_gateways() -> None:
    """清空登记表 —— 只给测试用，保证用例之间不互相串账本。"""
    _GATEWAYS.clear()


# ---------------------------------------------------------------- 审批落库
def record_approval(store: Any, *, tenant_id: str, case_id: str, approver: str,
                    decision: str, reason: str = "") -> dict:
    """把一次主管审批落进 `approval_record`。

    刻意放在这里而不是某个 skill 里：审批是**人**做的动作，发生在 CLI（本轮）或
    Matrix 房间（P4）里，不是哪个 Agent 跑出来的。`payment.execute` 只读它、不写它 ——
    让付款方自己写下「我被批准了」，等于没有审批。
    """
    if decision not in ("approved", "rejected"):
        raise ValueError(f"审批结论只能是 approved / rejected，实际 {decision!r}")
    row = {
        "tenant_id": tenant_id, "case_id": case_id, "approver": approver,
        "decision": decision, "reason": reason, "decided_at": now_iso(),
    }
    objects.execute(
        store,
        "INSERT OR REPLACE INTO approval_record (tenant_id, case_id, approver, decision,"
        " reason, decided_at) VALUES (?,?,?,?,?,?)",
        (row["tenant_id"], row["case_id"], row["approver"], row["decision"],
         row["reason"], row["decided_at"]),
    )
    return row


def approvals_of(store: Any, *, tenant_id: str, case_id: str,
                 decision: str = "approved") -> list[dict]:
    return objects.query(
        store,
        "SELECT * FROM approval_record WHERE tenant_id=? AND case_id=? AND decision=?"
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


# ---------------------------------------------------------------- 证据 kind 归一化
#: `customer_evidence.kind` 的规范值域。**只有这五个**，由收案面（refund.intake）定义，
#: 政策面按它对证据计数（政策规则里的 `requires_evidence_kinds` 写的就是这套词）。
#:
#: 为什么需要它：渠道送进来的 kind 是自由文本 —— 同一张照片可能写成 `photo` /
#: `img` / `screenshot`。政策里写 ["image"]，库里落 photo，一条都对不上，
#: 举证闸于是永远判「证据不足」，**而且不报错**。这是一条静默失效。
EVIDENCE_KINDS = ("image", "video", "audio", "document", "attachment")

#: 常见写法 -> 规范值。**只认字面同义词，不做语义猜测**。
#: 表只覆盖常见写法，覆盖不全是预期内的 —— 认不出的走 attachment 兜底，不是异常。
_KIND_ALIASES = {
    "img": "image", "photo": "image", "picture": "image", "screenshot": "image",
    "jpg": "image", "jpeg": "image", "png": "image",
    "mp4": "video", "mov": "video", "recording": "video",
    "voice": "audio", "mp3": "audio", "录音": "audio",
    "pdf": "document", "doc": "document", "docx": "document",
    "scan": "document", "扫描件": "document",
}


def normalize_evidence_kind(raw: Any) -> str:
    """把渠道送来的 kind 归一化到 `EVIDENCE_KINDS` 之一。三条口径：

    1. **这不是白名单过滤**：认不出的归到 attachment，证据本身**一条都不丢**。
       证据集合的成员判据仍然只是「有没有 uri」（见 `intake.py::_evidence_of`）——
       按 kind 白名单挑会把没见过的证据类型静默丢掉，那比对不上更糟。
    2. 大小写不敏感、去首尾空白，但**不按 uri 后缀反推**：`.jpg` 结尾而 kind 声明
       document 时以声明为准。后缀是传输细节，kind 是提交方的声明 —— 声明优先，
       且可审计（谁声明的、声明了什么，都留得下痕）。
    3. 认不出**不抛异常**：渠道送来没见过的词是常态。收案面只负责归一化并留痕
       （原始声明由调用方保留成出参里的 `kind_raw`），举证够不够由政策面判。

    库里只存规范值 —— `customer_evidence` 本轮不加列，`kind_raw` 只在出参与
    event_log 里（取舍已记 docs/BACKLOG.md）。
    """
    key = str(raw or "").strip().lower()
    if not key:
        return "attachment"
    if key in EVIDENCE_KINDS:
        return key
    return _KIND_ALIASES.get(key, "attachment")
