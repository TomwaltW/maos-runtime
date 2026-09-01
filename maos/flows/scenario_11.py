"""场景 11：采购退货退款（RTV）五步 SOP —— 顺利路径 + 失败路径，一个文件两条。

    顺利路径 drive_happy()
      ① 受理：定位源 PO/收货单，建案（return_action 留空，受理不替裁定拍板）
        → ② 裁定：按退货理由裁 credit，rationale 带规则编号
        → ③ 发运：carrier.ship 下运单 + carrier.track 取回执 → shipped
        → ④ 对账：supplier.credit_query 拿到贷项通知单，退货行 × 贷项通知单勾稽
             （差 0.50 元，容差 1.00 内），可退金额取**供应商认的那个**
        → ⑤ 观察：effect_risk=H 停 BLOCKED 等人放行 → 主管放行
             → rtv.observe 问供应商侧拿到 issued → **这时才**写 credited
             → 再问 AP 侧，第 2 次拿到调整凭单 → **这时才**写 settled
        → Plan DONE

    失败路径 drive_failure()
      同样受理、裁定、发运，对账时供应商门户回的是 **disputed（供应商不认这笔退货）**
        → 对账 reconciled=0，findings 挂 RTV-REC-03
        → 观察岗轮询到顶，供应商侧恒 disputed、AP 侧压根没有调整凭单
        → **一个字都不写**：不推 credited、不落 credit_note、不落终态观察行
        → 观察任务 effect_risk=H，Gate 过了也停在 BLOCKED 等人处置
        → 主管驳回：供应商不认，转人工与供应商交涉
        → rtv.compensate：提 RMA 申请留痕 + 开交涉工单 + biz_status -> compensated
        → Plan FAILED
        → rtv_case.biz_status = 'compensated'，**从未进入 credited / settled**

## 本域比 ap / refund 域多出来的那一条：**两个**权威源

契约 C-R3 的 `AUTHORITATIVE_STATES` 有两个元素。「供应商认不认这笔退货」和
「钱到没到账」是两件独立的事，由两个不同的外部系统说了算，MAOS 一个都写不了。
所以顺利路径上这两步是**分开**问的、分开写的，中间隔着一次 `acknowledged` ——
那是「供应商收到退货了」，不是「供应商认了这笔钱」，两者在回执里字段齐全、
形状一样，但差着一次会计确认。守卫 `AUTHORITATIVE_RECEIPT_STATE` 只认 `issued`。

## 失败路径要证明的那句话

    「所有 Agent 都回复完成」没有发生，因为业务确实没成功，系统如实记录了这一点。

失败路径上**五个 Agent 全部 `status=ok`**，五个都跑完了、都产出了产物、都没抛异常。
🔴 **失败演的是「供应商不认」这件业务事实，不是「代码抛了异常」** —— 后者是 bug，
证明不了任何关于编排的事。

## 同一份回执，谁能拿它写权威终态

第 ④ 步的对账与第 ⑤ 步的观察问的是**同一个** `supplier.credit_query`，
拿到的是**同一份** issued 回执。可只有 `rtv.observe` 写得进 `credited`
（C-R3 的 `AUTHORITATIVE_WRITER`）—— 对账那一步只把它拿去算数。
换个写入方去写，`_guard_update_biz_status` 抛 `AuthoritativeFactViolation` 并落一条
事件（`maos/tests/test_rtv_flow.py` 里有一条用例直接这么试）。

## 🔴 本场景**不在** `maos/main.py` 的 `ALL_SCENARIOS` / `DEFAULT_SCENARIOS` 里

缺省证据束恒为 8 束（`scenario-1..7` + `scenario-R5`）是跨轨冻结口径，
`scripts/demo_preflight.sh` 与复赛材料都写死了 8。所以本场景与
`scenario_8` / `scenario_9` / `scenario_10` 一样只有独立 `run()`，
由 `maos/tests/test_rtv_flow.py` 调用，人工也可以直接 `python3 -c` 调。
`maos/tests/test_rtv_flow.py::test_scenario_11_is_not_wired_into_default_scenarios`
钉着这件事。

===============================================================================
## 🔴 STUB 清单 —— 整合期照这张表逐条替换，别让下一个人去猜

T61 / T62 / T63 的产出**不在本轨的基线里**（契约 C-R8：五轨互不 import）。
所以本文件自带一层契约级 stub：**名字与形状照契约，行为是最小可跑实现。**
编排逻辑（DAG、闸、状态推进、权威事实守卫）用的是真的，
整合期把下面这些换掉，`drive_happy()` / `drive_failure()` 一行都不用改。

### 一、业务对象层（C-R1 / C-R2 / C-R3）→ 归 **T61**

| 本文件里的东西 | 整合期换成 |
| :-- | :-- |
| `STUB_SCHEMA`（照 C-R1 抄的建表语句） | `maos/domain/rtv/schema.sql` |
| `_stub_execute` / `_stub_query` | `maos/domain/rtv/objects.py` 的同名薄壳 |
| `BIZ_STATUS_FLOW` / `INITIAL_STATUS` | `maos/domain/rtv/guard.py::BIZ_STATUS_FLOW` |
| `AUTHORITATIVE_*` / `VIOLATION_EVENT` / `DOMAIN` | `maos/domain/rtv/guard.py` 同名常量 |
| `_guard_create_case` / `_guard_update_biz_status` | `guard.create_case` / `guard.update_biz_status` |
| `seed_case_inputs()` 里那份源单数据 | `maos/domain/rtv/fixtures.py`，源单从**复用**的 ap 域五张表读 |

### 二、ToolPort（C-R5 五个，一个不落）→ 归 **T62**

| stub | 整合期换成 `maos/tools/rtv.py` 里的 |
| :-- | :-- |
| `StubSupplierPortal.rma_submit` | `supplier.rma_submit`（写） |
| `StubSupplierPortal.credit_query` | `supplier.credit_query`（只读） |
| `StubCarrier.ship` | `carrier.ship`（写） |
| `StubCarrier.track` | `carrier.track`（只读） |
| `StubApSystem.adjust_query` | `ap.adjust_query`（**只读** —— RTV 域不许写 AP） |

`RETURN_REASONS` / `RULES` / `DOC_TYPE_CREDIT_NOTE` 三份码表同归 T62 的
`maos/tools/rtv_codes.py`。UNCL1001 的 `381 = Credit note` 出处沿用仓库已有的
`maos/tools/ap_codes.py`（Peppol BIS Billing 3.0），不另找一份。

### 三、Skill（C-R4 六个，一个不落）→ 归 **T63**

| stub 类 | 契约名（注册表主键） | 整合期换成 |
| :-- | :-- | :-- |
| `StubRtvIntakeSkill` | `rtv.intake` v1.0.0 | `maos/skills/builtin/rtv/intake.py` |
| `StubRtvDisposeSkill` | `rtv.dispose` v1.0.0 | `maos/skills/builtin/rtv/dispose.py` |
| `StubRtvShipSkill` | `rtv.ship` v1.0.0 | `maos/skills/builtin/rtv/ship.py` |
| `StubRtvReconcileSkill` | `rtv.reconcile` v1.0.0 | `maos/skills/builtin/rtv/reconcile.py` |
| `StubRtvObserveSkill` | `rtv.observe` v1.0.0 | `maos/skills/builtin/rtv/observe.py` |
| `StubRtvCompensateSkill` | `rtv.compensate` v1.0.0 | `maos/skills/builtin/rtv/compensate.py` |

**`name` / `version` / `owner_roles` / `depends_tools` 已照 C-R4 逐字填好**，
注册表主键（name + version）与真实现完全一致 —— 所以整合期投放真 skill 之后，
把本文件里那六个 `@register_skill` 的 stub 类整段删掉即可，调用点零改动。
🔴 六个 stub 是在 **import 本模块时**注册进全局 `SKILL_REGISTRY` 的，
删的时候六个一起删，留半份会让注册表里同名同版本的条目被 stub 覆盖掉真实现。

### 四、不是 stub 的部分（这些是真的，别当 stub 一起换掉）

`maos/agents/rtv/**` 五个薄壳 Agent、`build()` 六元组、`run_until_settled` 驱动
循环、`ReviewerGate` 七道闸、`HumanApprovalQueue`、`ControlPlane` 的状态机与
补偿顺序 —— 全是生产件。**本场景要证明的正是它们一行都不用改**。
===============================================================================
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import nullcontext
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from maos.agents.rtv import (
    KIND_RTV_DISPOSITION,
    KIND_RTV_INTAKE,
    KIND_RTV_RECONCILIATION,
    KIND_RTV_SETTLEMENT_ADVICE,
    KIND_RTV_SHIPMENT,
    ROLE_DISPOSITION,
    ROLE_INTAKE,
    ROLE_LOGISTICS,
    ROLE_RECONCILE,
    ROLE_SETTLEMENT,
    RtvSettlementAgent,
)
from maos.agents.rtv.settlement_agent import SKILL_COMPENSATE
from maos.contracts.events import new_id
from maos.contracts.states import TASK_TRANSITIONS, PlanState, TaskState
from maos.flows.common import build, dump, run_until_settled
from maos.model.client import select_model_client
from maos.runtime.gate import HumanApprovalQueue
from maos.skills.contract import Skill, SkillContract, SkillContext
from maos.skills.invoker import SkillInvoker
from maos.skills.registry import SKILL_REGISTRY, register_skill

# =========================================================================
# STUB 一：业务对象层（归 T61）—— 表结构照 C-R1，守卫照 C-R2 / C-R3
# =========================================================================
#: 照契约 C-R1 抄的建表语句（子集：本场景用得到的八张表 + 业务引用表）。
#: **五张源单表刻意不建** —— supplier / purchase_order / purchase_order_line /
#: goods_receipt / goods_receipt_line 在 `maos/domain/ap/schema.sql` 里已经存在，
#: RTV 只引用不重建。`CREATE TABLE IF NOT EXISTS` 撞名的后果不是报错而是**静默跳过**，
#: 重建一份「看起来一样」的定义，两处一旦漂开，症状会离原因非常远。
#: 本 stub 因此让源单信息从 task.inputs 进来（见 `seed_case_inputs`）：
#: 整合期换成 T61 的 fixtures 从那五张复用表读，本文件这一段整段删掉。
STUB_SCHEMA = """
CREATE TABLE IF NOT EXISTS rtv_schema_version (
    version    INTEGER NOT NULL,
    applied_at TEXT NOT NULL,
    PRIMARY KEY (version)
);

CREATE TABLE IF NOT EXISTS rtv_case (
    tenant_id   TEXT NOT NULL,
    case_id     TEXT NOT NULL,
    supplier_id TEXT NOT NULL,
    po_id       TEXT NOT NULL,
    po_version  INTEGER NOT NULL,
    gr_id       TEXT NOT NULL,
    return_action TEXT NOT NULL DEFAULT '',
    amount_claimed TEXT NOT NULL,
    currency    TEXT NOT NULL DEFAULT 'CNY',
    biz_status  TEXT NOT NULL,
    plan_id     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (tenant_id, case_id),
    CHECK (return_action IN ('', 'credit', 'exchange', 'replacement')),
    CHECK (biz_status IN ('received', 'disposed', 'shipped',
                          'credited', 'settled', 'rejected', 'compensated'))
);

CREATE TABLE IF NOT EXISTS rtv_line (
    tenant_id         TEXT NOT NULL,
    case_id           TEXT NOT NULL,
    line_no           INTEGER NOT NULL,
    gr_line_no        INTEGER NOT NULL,
    sku               TEXT NOT NULL,
    quantity_returned REAL NOT NULL,
    unit_price        TEXT NOT NULL,
    reason_code       TEXT NOT NULL,
    PRIMARY KEY (tenant_id, case_id, line_no)
);

CREATE TABLE IF NOT EXISTS rtv_disposition (
    tenant_id      TEXT NOT NULL,
    case_id        TEXT NOT NULL,
    attempt        INTEGER NOT NULL,
    return_action  TEXT NOT NULL,
    rationale_json TEXT NOT NULL DEFAULT '[]',
    decided_by     TEXT NOT NULL,
    decided_at     TEXT NOT NULL,
    PRIMARY KEY (tenant_id, case_id, attempt),
    CHECK (return_action IN ('credit', 'exchange', 'replacement'))
);

CREATE TABLE IF NOT EXISTS rtv_shipment (
    tenant_id      TEXT NOT NULL,
    case_id        TEXT NOT NULL,
    shipment_id    TEXT NOT NULL,
    carrier        TEXT NOT NULL,
    tracking_no    TEXT NOT NULL,
    carrier_status TEXT NOT NULL,
    shipped_at     TEXT NOT NULL,
    PRIMARY KEY (tenant_id, case_id, shipment_id)
);

CREATE TABLE IF NOT EXISTS credit_note (
    tenant_id       TEXT NOT NULL,
    case_id         TEXT NOT NULL,
    credit_note_id  TEXT NOT NULL,
    document_type   TEXT NOT NULL DEFAULT '381',
    amount_credited TEXT NOT NULL,
    currency        TEXT NOT NULL DEFAULT 'CNY',
    issued_at       TEXT NOT NULL,
    observed_at     TEXT NOT NULL,
    observed_by     TEXT NOT NULL,
    invocation_id   TEXT NOT NULL,
    PRIMARY KEY (tenant_id, case_id, credit_note_id)
);

CREATE TABLE IF NOT EXISTS rtv_reconciliation (
    tenant_id       TEXT NOT NULL,
    case_id         TEXT NOT NULL,
    attempt         INTEGER NOT NULL,
    reconciled      INTEGER NOT NULL,
    creditable_amount TEXT NOT NULL DEFAULT '',
    findings_json   TEXT NOT NULL DEFAULT '[]',
    tolerance_json  TEXT NOT NULL DEFAULT '{}',
    reconciled_by   TEXT NOT NULL,
    reconciled_at   TEXT NOT NULL,
    PRIMARY KEY (tenant_id, case_id, attempt)
);

CREATE TABLE IF NOT EXISTS rtv_settlement_observation (
    tenant_id      TEXT NOT NULL,
    case_id        TEXT NOT NULL,
    seq            INTEGER NOT NULL,
    adjustment_id  TEXT NOT NULL,
    observed_state TEXT NOT NULL,
    ap_reference   TEXT NOT NULL,
    observed_at    TEXT NOT NULL,
    observed_by    TEXT NOT NULL,
    invocation_id  TEXT NOT NULL,
    PRIMARY KEY (tenant_id, case_id, seq)
);

CREATE TABLE IF NOT EXISTS rtv_compensation_record (
    tenant_id     TEXT NOT NULL,
    case_id       TEXT NOT NULL,
    seq           INTEGER NOT NULL,
    reason        TEXT NOT NULL,
    detail_json   TEXT NOT NULL DEFAULT '{}',
    compensated_by TEXT NOT NULL,
    compensated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, case_id, seq)
);

CREATE TABLE IF NOT EXISTS rtv_business_ref (
    plan_id        TEXT NOT NULL,
    task_id        TEXT,
    object_table   TEXT NOT NULL,
    object_id      TEXT NOT NULL,
    object_version INTEGER,
    purpose        TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL
);
"""

# ---- 业务状态机（契约 C-R2，一字照抄）---------------------------------------
BIZ_STATUS_FLOW: dict[str, tuple[str, ...]] = {
    "received":    ("disposed", "rejected"),
    "disposed":    ("shipped", "rejected", "compensated"),
    "shipped":     ("credited", "compensated"),
    "credited":    ("settled", "compensated"),
    "settled":     (),
    "rejected":    (),
    "compensated": (),
}

INITIAL_STATUS = "received"

# ---- 权威事实归属（契约 C-R3，一字照抄）-------------------------------------
AUTHORITATIVE_WRITER = "rtv.observe"

#: 🔴 **两个**权威终态，这是本域相对 ap / refund 域的增量。
AUTHORITATIVE_STATES = frozenset({"credited", "settled"})

#: 权威终态 -> 该终态要求回执里的 observed_state 取值。与 AUTHORITATIVE_STATES
#: **同增同减**：加一个权威终态就必须在这里给出它的判据，漏配不放行。
#: 🔴 `acknowledged` 绝不许进 `credited` 那一格：那是「供应商收到退货了」，
#: 不是「供应商认了这笔钱」，两者差着一次会计确认。
AUTHORITATIVE_RECEIPT_STATE: dict[str, frozenset[str]] = {
    "credited": frozenset({"issued"}),
    "settled":  frozenset({"settled"}),
}

VIOLATION_EVENT = "AuthoritativeFactViolation"
DOMAIN = "rtv"

#: 业务状态变更事件。与 ap 域的 `ApBizStatusChanged` 同构、分域命名 ——
#: **不往 `maos/contracts/states.py` 加任何状态或迁移**（铁律 9）：
#: 业务状态是 `rtv_case` 自己的字段，不是 Task 状态。
BIZ_STATUS_EVENT = "RtvBizStatusChanged"


class AuthoritativeFactViolation(RuntimeError):
    """有人试图不经 `rtv.observe`、或不带外部回执，把权威终态写死。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn(store: Any) -> sqlite3.Connection:
    conn = getattr(store, "_conn", None)
    if conn is None:
        raise TypeError(f"{type(store).__name__} 没有暴露 sqlite 连接，RTV 域的新增表无处落库")
    return conn


def _lock_of(store: Any) -> Any:
    """借 Store 自己的锁 —— 口径照抄 `maos/domain/ap/objects.py::lock_of`。

    `SqliteStore` 的连接是共享的（`check_same_thread=False` + 一把 RLock），
    绕过 store.py 直接用这条连接就必须一并用它那把锁，否则别的线程一次 commit
    会把这边只写了回执、还没改状态的事务提交掉 —— 「权威终态与回执同事务」当场破。
    """
    lock = getattr(store, "_lock", None)
    return lock if lock is not None else nullcontext()


def _stub_execute(store: Any, sql: str, params: tuple | list = ()) -> None:
    conn = _conn(store)
    with _lock_of(store):
        conn.execute(sql, tuple(params))
        conn.commit()


def _stub_query(store: Any, sql: str, params: tuple | list = ()) -> list[dict]:
    with _lock_of(store):
        rows = _conn(store).execute(sql, tuple(params)).fetchall()
    return [dict(r) for r in rows]


def ensure_schema(store: Any) -> None:
    """建表。整合期换成 `maos.domain.rtv.objects.ensure_schema`。"""
    with _lock_of(store):
        _conn(store).executescript(STUB_SCHEMA)
        _conn(store).commit()


def get_case(store: Any, tenant_id: str, case_id: str) -> dict:
    rows = _stub_query(store, "SELECT * FROM rtv_case WHERE tenant_id=? AND case_id=?",
                       (tenant_id, case_id))
    if not rows:
        raise LookupError(f"退货案 {case_id} 不存在")
    return rows[0]


def _guard_create_case(store: Any, *, tenant_id: str, case_id: str, supplier_id: str,
                       po_id: str, po_version: int, gr_id: str, amount_claimed: str,
                       currency: str, plan_id: str) -> dict:
    """建案。**`return_action` 恒为空串** —— 受理的人不该替裁定的人拍板（C-R1）。"""
    _stub_execute(store, "INSERT INTO rtv_case (tenant_id, case_id, supplier_id, po_id,"
                         " po_version, gr_id, return_action, amount_claimed, currency,"
                         " biz_status, plan_id, created_at)"
                         " VALUES (?,?,?,?,?,?,'',?,?,?,?,?)",
                  (tenant_id, case_id, supplier_id, po_id, po_version, gr_id,
                   amount_claimed, currency, INITIAL_STATUS, plan_id, _now()))
    return get_case(store, tenant_id, case_id)


def _record_violation(store: Any, ctx_extras: dict, *, to_status: str, writer: str,
                      receipt_state: str, why: str) -> None:
    """把一次越权写入落成事件行 —— 不能只抛异常。

    异常只活在调用栈里，进程一退就没了；而「有人试图把外部状态写死为终态」
    是需要事后查得到的安全事实（铁律 8）。事件类型与 ap / refund 域共用同一个名字，
    按 `detail.domain` 区分，否则查不出是哪个域。
    """
    if store is None:
        return
    store.append_event_log({
        "event_id": "", "trace_id": ctx_extras.get("trace_id", ""),
        "plan_id": ctx_extras.get("plan_id", ""), "task_id": ctx_extras.get("task_id"),
        "event_type": VIOLATION_EVENT, "from_state": "", "to_state": to_status,
        "reason": why,
        "detail": {"domain": DOMAIN, "writer": writer, "expected_writer":
                   AUTHORITATIVE_WRITER, "receipt_state": receipt_state,
                   "authoritative_states": sorted(AUTHORITATIVE_STATES)},
    })


def _guard_update_biz_status(store: Any, *, tenant_id: str, case_id: str, to_status: str,
                             writer: str, receipt_state: str = "",
                             invocation_id: str = "", extras: dict | None = None) -> dict:
    """本域**唯一**的业务状态写入口。整合期换成 `maos.domain.rtv.guard.update_biz_status`。

    三条判据，权威终态一条都不许少（契约 C-R3）：

      1. 迁移必须在 `BIZ_STATUS_FLOW` 里 —— 状态机是业务对象自己的，不是 Task 的；
      2. 权威终态只有 `rtv.observe` 写得动（`AUTHORITATIVE_WRITER`）；
      3. 权威终态必须带一份**外部回执**，且回执状态在 `AUTHORITATIVE_RECEIPT_STATE`
         里给出的取值集合内 —— 拿 `acknowledged` 换 `credited` 会在这里被拒。

    三条都在**落库之前**判，且违规时先落一条 `AuthoritativeFactViolation` 再抛：
    只抛不落的话，这件事就只活在那一次调用栈里。
    """
    extras = extras or {}
    case = get_case(store, tenant_id, case_id)
    frm = case["biz_status"]
    if to_status not in BIZ_STATUS_FLOW.get(frm, ()):
        raise ValueError(f"业务状态迁移 {frm} -> {to_status} 不在 BIZ_STATUS_FLOW 里")

    if to_status in AUTHORITATIVE_STATES:
        if writer != AUTHORITATIVE_WRITER:
            why = (f"{to_status} 是权威终态，只有 {AUTHORITATIVE_WRITER} 写得进，"
                   f"实际写入方 {writer!r} —— 权威状态归外部系统（铁律 8）")
            _record_violation(store, extras, to_status=to_status, writer=writer,
                              receipt_state=receipt_state, why=why)
            raise AuthoritativeFactViolation(why)
        allowed = AUTHORITATIVE_RECEIPT_STATE.get(to_status, frozenset())
        if receipt_state not in allowed:
            why = (f"{to_status} 要求外部回执状态属于 {sorted(allowed)}，"
                   f"实际拿到 {receipt_state!r} —— 没有回执就不许写终态")
            _record_violation(store, extras, to_status=to_status, writer=writer,
                              receipt_state=receipt_state, why=why)
            raise AuthoritativeFactViolation(why)
        if not invocation_id:
            why = f"{to_status} 的写入必须带 actor 锚点（invocation_id），否则审计链断了"
            _record_violation(store, extras, to_status=to_status, writer=writer,
                              receipt_state=receipt_state, why=why)
            raise AuthoritativeFactViolation(why)

    _stub_execute(store, "UPDATE rtv_case SET biz_status=? WHERE tenant_id=? AND case_id=?",
                  (to_status, tenant_id, case_id))
    if store is not None:
        store.append_event_log({
            "event_id": "", "trace_id": extras.get("trace_id", ""),
            "plan_id": extras.get("plan_id", ""), "task_id": extras.get("task_id"),
            "event_type": BIZ_STATUS_EVENT, "from_state": frm, "to_state": to_status,
            "reason": f"{writer} 推进业务状态",
            "detail": {"domain": DOMAIN, "case_id": case_id, "writer": writer,
                       "receipt_state": receipt_state,
                       "actor_invocation_id": invocation_id},
        })
    return get_case(store, tenant_id, case_id)


def _attach_business_ref(store: Any, *, plan_id: str, task_id: str | None,
                         object_table: str, object_id: str, purpose: str) -> dict:
    """挂一条业务对象引用。口径照抄 ap 域 `objects.attach_business_ref`。"""
    _stub_execute(store, "INSERT INTO rtv_business_ref (plan_id, task_id, object_table,"
                         " object_id, object_version, purpose, created_at)"
                         " VALUES (?,?,?,?,?,?,?)",
                  (plan_id, task_id, object_table, object_id, None, purpose, _now()))
    return {"object_table": object_table, "object_id": object_id, "purpose": purpose}


# =========================================================================
# STUB 二：码表与 ToolPort（归 T62）—— 名字照 C-R5，一个不落
# =========================================================================
TOOL_SUPPLIER_RMA_SUBMIT = "supplier.rma_submit"
TOOL_SUPPLIER_CREDIT_QUERY = "supplier.credit_query"
TOOL_CARRIER_SHIP = "carrier.ship"
TOOL_CARRIER_TRACK = "carrier.track"
TOOL_AP_ADJUST_QUERY = "ap.adjust_query"

#: C-R5 的五个 ToolPort 名。测试按它对照契约，场景里不抄字面量。
STUB_TOOL_NAMES = (
    TOOL_SUPPLIER_RMA_SUBMIT, TOOL_SUPPLIER_CREDIT_QUERY,
    TOOL_CARRIER_SHIP, TOOL_CARRIER_TRACK, TOOL_AP_ADJUST_QUERY,
)

#: 退货理由码（整合期归 `maos/tools/rtv_codes.py::RETURN_REASONS`）。
RETURN_REASONS = {
    "RC-DEFECT": "到货不合格",
    "RC-WRONG-ITEM": "错发型号",
    "RC-OVERSHIP": "超量到货",
}

#: 规则编号（整合期归 `maos/tools/rtv_codes.py::RULES`）。裁定与对账的每一条
#: 结论都必须挂一个这里的编号 —— 「凭什么这么裁」在事后要查得到，
#: 自然语言理由查不了。
RULES = {
    "RTV-DISP-01": "到货不合格 -> credit（供应商开贷项通知单冲减应付）",
    "RTV-DISP-02": "错发型号 -> replacement（换货，不走贷记）",
    "RTV-REC-01": "退货行金额合计与贷项通知单金额差额在容差内，对上",
    "RTV-REC-02": "退货行金额合计与贷项通知单金额差额超容差，对不上",
    "RTV-REC-03": "供应商未开出贷项通知单，没有可勾稽的对方凭证",
}

#: UNCL1001 单据类型码：贷项通知单恒为 381。出处沿用 `maos/tools/ap_codes.py`
#: （Peppol BIS Billing 3.0），不另找一份。
DOC_TYPE_CREDIT_NOTE = "381"

#: 处置裁定表：退货理由 -> (return_action, rule_id)。
DISPOSITION_BY_REASON = {
    "RC-DEFECT": ("credit", "RTV-DISP-01"),
    "RC-OVERSHIP": ("credit", "RTV-DISP-01"),
    "RC-WRONG-ITEM": ("replacement", "RTV-DISP-02"),
}

#: 承运商回执里算「货已经在退回路上/已送达」的取值。`shipped` 不是权威终态
#: （C-R3 只有两个），但它同样由回执得到，不由「我调用成功了」推断。
SHIPPED_CARRIER_STATES = frozenset({"in_transit", "delivered"})

#: 供应商侧回执的三种取值。**`acknowledged` 与 `issued` 差着一次会计确认** ——
#: 前者是「收到退货了」，后者是「开出贷项通知单了」，只有后者能换 `credited`。
SUPPLIER_ACKNOWLEDGED = "acknowledged"
SUPPLIER_ISSUED = "issued"
SUPPLIER_DISPUTED = "disputed"

#: AP 侧回执：还没建调整凭单 / 建了还没核销 / 已核销。
AP_NONE = "none"
AP_PENDING = "pending"
AP_SETTLED = "settled"

#: 一次观察都没做成时的占位。**不许取外部系统的终态值** —— 口径同 ap 域的
#: `UNOBSERVED`：「我问累了」和「供应商说不认」是两回事。
UNOBSERVED = "unobserved"


class StubSupplierPortal:
    """供应商门户 stub：`supplier.rma_submit`（写）+ `supplier.credit_query`（只读）。

    `issue_after` 是「问到第几次才开出贷项通知单」，之前一律回 `acknowledged`
    （收到退货了，但还没做会计确认）。`disputed=True` 则**恒**回 disputed ——
    失败路径演的就是这一档：供应商不认这笔退货，这是业务事实，不是异常。
    """

    def __init__(self, *, issue_after: int = 1, amount: str = "", disputed: bool = False,
                 issued_at: str = "2026-08-30T00:00:00+00:00") -> None:
        self.issue_after = issue_after
        self.amount = amount
        self.disputed = disputed
        self.issued_at = issued_at
        self.queries = 0
        self.rma_submissions: list[dict] = []

    def credit_query(self, case_id: str) -> dict:
        self.queries += 1
        if self.disputed:
            return {"state": SUPPLIER_DISPUTED, "document_id": "", "amount": "",
                    "currency": "", "issued_at": "",
                    "message": "供应商不认这笔退货：主张到货时已验收合格，拒开贷项通知单"}
        if self.queries < self.issue_after:
            return {"state": SUPPLIER_ACKNOWLEDGED, "document_id": "", "amount": "",
                    "currency": "", "issued_at": "",
                    "message": "供应商已收到退货，尚未做会计确认（**不是**认了这笔钱）"}
        return {"state": SUPPLIER_ISSUED,
                "document_id": f"CN-{case_id[-4:]}-2026",
                "document_type": DOC_TYPE_CREDIT_NOTE,
                "amount": self.amount, "currency": "CNY",
                "issued_at": self.issued_at,
                "message": "供应商已开出贷项通知单"}

    def rma_submit(self, case_id: str, reason: str) -> dict:
        rma = {"rma_id": f"RMA-{case_id[-4:]}-{len(self.rma_submissions) + 1}",
               "case_id": case_id, "reason": reason, "accepted_at": _now()}
        self.rma_submissions.append(rma)
        return rma


class StubCarrier:
    """承运商 stub：`carrier.ship`（写）+ `carrier.track`（只读）。"""

    def __init__(self, *, status: str = "in_transit") -> None:
        self.status = status
        self.shipments: list[dict] = []

    def ship(self, case_id: str) -> dict:
        seq = len(self.shipments) + 1
        out = {"shipment_id": f"SHP-{case_id[-4:]}-{seq}",
               "tracking_no": f"SF{case_id[-4:]}{seq:04d}",
               "shipped_at": "2026-08-28T02:00:00+00:00"}
        self.shipments.append(out)
        return out

    def track(self, tracking_no: str) -> dict:
        return {"tracking_no": tracking_no, "carrier_status": self.status}


class StubApSystem:
    """AP 系统 stub：`ap.adjust_query`（**只读**）。

    🔴 RTV 域不许写 AP 的任何东西 —— 调整凭单由 AP 侧按自己的规则建，我方只观察。
    两处都能写会让「这笔调整是谁建的」失去唯一答案（C-R5 红字）。所以这个 stub
    只有 query，没有任何写方法。

    `settle_after` 是「问到第几次才核销」；`idle=True` 则恒回 `none`
    —— 供应商都不认，AP 那边压根不会有调整凭单。
    """

    def __init__(self, *, settle_after: int = 1, idle: bool = False) -> None:
        self.settle_after = settle_after
        self.idle = idle
        self.queries = 0

    def adjust_query(self, case_id: str) -> dict:
        self.queries += 1
        if self.idle:
            return {"state": AP_NONE, "document_id": "", "ap_reference": "",
                    "message": "AP 尚未就这笔退货建调整凭单"}
        if self.queries < self.settle_after:
            return {"state": AP_PENDING, "document_id": f"ADJ-{case_id[-4:]}",
                    "ap_reference": "",
                    "message": "AP 已建调整凭单，尚未核销"}
        return {"state": AP_SETTLED, "document_id": f"ADJ-{case_id[-4:]}",
                "ap_reference": f"AR-{case_id[-4:]}-0001",
                "message": "AP 调整凭单已核销"}


# ---- 按名取实例：task.inputs 会被 json.dumps，实例塞不进去 -------------------
_PORTALS: dict[str, StubSupplierPortal] = {}
_CARRIERS: dict[str, StubCarrier] = {}
_AP_SYSTEMS: dict[str, StubApSystem] = {}


def reset_stub_tools() -> None:
    _PORTALS.clear()
    _CARRIERS.clear()
    _AP_SYSTEMS.clear()


def register_portal(name: str, portal: StubSupplierPortal) -> None:
    _PORTALS[name] = portal


def register_carrier(name: str, carrier: StubCarrier) -> None:
    _CARRIERS[name] = carrier


def register_ap_system(name: str, system: StubApSystem) -> None:
    _AP_SYSTEMS[name] = system


def get_portal(name: str) -> StubSupplierPortal:
    if name not in _PORTALS:
        raise LookupError(f"没有注册叫 {name!r} 的供应商门户")
    return _PORTALS[name]


def get_carrier(name: str) -> StubCarrier:
    if name not in _CARRIERS:
        raise LookupError(f"没有注册叫 {name!r} 的承运商")
    return _CARRIERS[name]


def get_ap_system(name: str) -> StubApSystem:
    if name not in _AP_SYSTEMS:
        raise LookupError(f"没有注册叫 {name!r} 的 AP 系统")
    return _AP_SYSTEMS[name]


# =========================================================================
# STUB 三：六个 skill（归 T63）—— name / version / owner_roles / depends_tools
#          逐字照 C-R4，注册表主键与真实现完全一致
# =========================================================================
def _money(raw: Any) -> Decimal:
    return Decimal(str(raw or "0"))


def _line_total(lines: list[dict]) -> Decimal:
    return sum((_money(ln["unit_price"]) * _money(ln["quantity_returned"])
                for ln in lines), Decimal("0"))


def _lines_of(store: Any, tenant_id: str, case_id: str) -> list[dict]:
    return _stub_query(store, "SELECT * FROM rtv_line WHERE tenant_id=? AND case_id=?"
                              " ORDER BY line_no", (tenant_id, case_id))


def _next_attempt(store: Any, table: str, tenant_id: str, case_id: str) -> int:
    rows = _stub_query(store, f"SELECT COALESCE(MAX(attempt), 0) AS n FROM {table}"
                              " WHERE tenant_id=? AND case_id=?", (tenant_id, case_id))
    return int(rows[0]["n"]) + 1


@register_skill
class StubRtvIntakeSkill(Skill):
    """STUB（归 T63）—— 定位源 PO/GR，建案，挂业务对象引用。"""

    contract = SkillContract(
        name="rtv.intake", version="1.0.0",
        purpose="定位源 PO/收货单，建出退货案并挂上业务对象引用",
        preconditions=["tenant_id", "case_id"],
        depends_tools=[],
        owner_roles=["rtv_intake"],
        failure_policy="escalate",
        security_boundary="只写 rtv_case / rtv_line / rtv_business_ref，不碰任何外部系统",
        reuse_note="STUB：整合期换成 maos/skills/builtin/rtv/intake.py",
    )

    def run(self, payload: dict, ctx: SkillContext) -> Any:
        store, extras = ctx.store, dict(ctx.extras)
        tenant_id, case_id = payload["tenant_id"], payload["case_id"]
        lines = list(payload.get("lines") or [])

        case = _guard_create_case(
            store, tenant_id=tenant_id, case_id=case_id,
            supplier_id=payload["supplier_id"], po_id=payload["po_id"],
            po_version=int(payload["po_version"]), gr_id=payload["gr_id"],
            amount_claimed=str(payload.get("amount_claimed") or "0"),
            currency=str(payload.get("currency") or "CNY"),
            plan_id=extras.get("plan_id", ""))
        for ln in lines:
            _stub_execute(store, "INSERT INTO rtv_line (tenant_id, case_id, line_no,"
                                 " gr_line_no, sku, quantity_returned, unit_price,"
                                 " reason_code) VALUES (?,?,?,?,?,?,?,?)",
                          (tenant_id, case_id, ln["line_no"], ln["gr_line_no"],
                           ln["sku"], ln["quantity_returned"], ln["unit_price"],
                           ln["reason_code"]))
            if ln["reason_code"] not in RETURN_REASONS:
                raise ValueError(f"退货理由码 {ln['reason_code']} 不在 RETURN_REASONS 里")

        refs = [
            _attach_business_ref(store, plan_id=extras.get("plan_id", ""),
                                 task_id=extras.get("task_id"), object_table="rtv_case",
                                 object_id=case_id, purpose="本次退货的案子"),
            _attach_business_ref(store, plan_id=extras.get("plan_id", ""),
                                 task_id=extras.get("task_id"),
                                 object_table="purchase_order", object_id=payload["po_id"],
                                 purpose="退的是哪一张订单"),
            _attach_business_ref(store, plan_id=extras.get("plan_id", ""),
                                 task_id=extras.get("task_id"),
                                 object_table="goods_receipt", object_id=payload["gr_id"],
                                 purpose="退的是哪一次收货"),
        ]
        return {"case": case, "lines": _lines_of(store, tenant_id, case_id),
                "refs": refs, "invocation_id": extras.get("invocation_id", "")}


@register_skill
class StubRtvDisposeSkill(Skill):
    """STUB（归 T63）—— 裁定 credit/exchange/replacement，出规则出处。"""

    contract = SkillContract(
        name="rtv.dispose", version="1.0.0",
        purpose="按退货理由与合同条款裁定处置类型，并保留规则出处",
        preconditions=["tenant_id", "case_id"],
        depends_tools=[],
        owner_roles=["rtv_disposition"],
        failure_policy="escalate",
        security_boundary="只读 rtv_line、只写 rtv_disposition 与 rtv_case.return_action",
        reuse_note="STUB：整合期换成 maos/skills/builtin/rtv/dispose.py",
    )

    def run(self, payload: dict, ctx: SkillContext) -> Any:
        store, extras = ctx.store, dict(ctx.extras)
        tenant_id, case_id = payload["tenant_id"], payload["case_id"]
        lines = _lines_of(store, tenant_id, case_id)
        if not lines:
            raise ValueError(f"退货案 {case_id} 没有退货行，裁不了")

        # 裁定依据取**首行**的理由码：本 stub 只演「裁定要挂规则编号」这件事，
        # 多行理由不一致该怎么裁是真实现的事（整合期由 T63 定），不在这里猜。
        reason = lines[0]["reason_code"]
        action, rule_id = DISPOSITION_BY_REASON[reason]
        rationale = [{"rule_id": rule_id, "reason_code": reason,
                      "reason_name": RETURN_REASONS[reason], "rule": RULES[rule_id]}]

        attempt = _next_attempt(store, "rtv_disposition", tenant_id, case_id)
        decided_by = str(payload.get("decided_by") or "")
        decided_at = _now()
        _stub_execute(store, "INSERT INTO rtv_disposition (tenant_id, case_id, attempt,"
                             " return_action, rationale_json, decided_by, decided_at)"
                             " VALUES (?,?,?,?,?,?,?)",
                      (tenant_id, case_id, attempt, action,
                       json.dumps(rationale, ensure_ascii=False), decided_by, decided_at))
        _stub_execute(store, "UPDATE rtv_case SET return_action=? WHERE tenant_id=?"
                             " AND case_id=?", (action, tenant_id, case_id))
        case = _guard_update_biz_status(store, tenant_id=tenant_id, case_id=case_id,
                                        to_status="disposed", writer=self.contract.name,
                                        extras=extras)
        return {"disposition": {"attempt": attempt, "return_action": action,
                                "rationale": rationale, "decided_by": decided_by,
                                "decided_at": decided_at},
                "biz_status": case["biz_status"],
                "invocation_id": extras.get("invocation_id", "")}


@register_skill
class StubRtvShipSkill(Skill):
    """STUB（归 T63）—— 登记发运并取承运商回执。"""

    contract = SkillContract(
        name="rtv.ship", version="1.0.0",
        purpose="登记退货发运并取得承运商回执",
        preconditions=["tenant_id", "case_id"],
        depends_tools=["carrier.ship", "carrier.track"],
        owner_roles=["rtv_logistics"],
        failure_policy="escalate",
        security_boundary="只写 rtv_shipment；shipped 由承运商回执得到，不由调用成功推断",
        reuse_note="STUB：整合期换成 maos/skills/builtin/rtv/ship.py",
    )

    def run(self, payload: dict, ctx: SkillContext) -> Any:
        store, extras = ctx.store, dict(ctx.extras)
        tenant_id, case_id = payload["tenant_id"], payload["case_id"]
        carrier = get_carrier(str(payload["carrier"]))

        sent = carrier.ship(case_id)                       # carrier.ship（写）
        seen = carrier.track(sent["tracking_no"])          # carrier.track（只读）
        status = seen["carrier_status"]
        _stub_execute(store, "INSERT INTO rtv_shipment (tenant_id, case_id, shipment_id,"
                             " carrier, tracking_no, carrier_status, shipped_at)"
                             " VALUES (?,?,?,?,?,?,?)",
                      (tenant_id, case_id, sent["shipment_id"], str(payload["carrier"]),
                       sent["tracking_no"], status, sent["shipped_at"]))
        if status not in SHIPPED_CARRIER_STATES:
            raise ValueError(f"承运商回执 {status!r} 不足以说明货已退回，不推进 shipped")
        case = _guard_update_biz_status(store, tenant_id=tenant_id, case_id=case_id,
                                        to_status="shipped", writer=self.contract.name,
                                        extras=extras)
        return {"shipment": {**sent, "carrier": str(payload["carrier"]),
                             "carrier_status": status},
                "biz_status": case["biz_status"],
                "invocation_id": extras.get("invocation_id", "")}


@register_skill
class StubRtvReconcileSkill(Skill):
    """STUB（归 T63）—— 退货行 × 贷项通知单 × 到账三方对账。

    🔴 **对账不产出终态**：它问的是同一个 `supplier.credit_query`，拿到的是同一份
    回执，但写不进 `credited` —— 那归 `rtv.observe`（C-R3）。这一条正是本场景要演的
    「权威事实边界」最细的那一处：同一份事实，读它的人很多，写它的人只有一个。
    """

    TOLERANCE = {"amount": "1.00"}

    contract = SkillContract(
        name="rtv.reconcile", version="1.0.0",
        purpose="退货行 × 贷项通知单 × 到账三方对账，产出可核对的结论",
        preconditions=["tenant_id", "case_id"],
        depends_tools=["supplier.credit_query"],
        owner_roles=["rtv_reconcile"],
        failure_policy="escalate",
        security_boundary="只读供应商门户、只写 rtv_reconciliation；不推进任何权威终态",
        reuse_note="STUB：整合期换成 maos/skills/builtin/rtv/reconcile.py",
    )

    def run(self, payload: dict, ctx: SkillContext) -> Any:
        store, extras = ctx.store, dict(ctx.extras)
        tenant_id, case_id = payload["tenant_id"], payload["case_id"]
        portal = get_portal(str(payload["supplier_portal"]))

        receipt = portal.credit_query(case_id)             # supplier.credit_query（只读）
        claimed = _line_total(_lines_of(store, tenant_id, case_id))
        tolerance = Decimal(self.TOLERANCE["amount"])

        findings: list[dict] = []
        creditable = ""
        if receipt["state"] != SUPPLIER_ISSUED:
            findings.append({"rule_id": "RTV-REC-03", "rule": RULES["RTV-REC-03"],
                             "supplier_state": receipt["state"],
                             "message": receipt["message"]})
        else:
            credited = _money(receipt["amount"])
            gap = abs(claimed - credited)
            if gap > tolerance:
                findings.append({"rule_id": "RTV-REC-02", "rule": RULES["RTV-REC-02"],
                                 "claimed": str(claimed), "credited": str(credited),
                                 "gap": str(gap), "tolerance": str(tolerance)})
            else:
                findings.append({"rule_id": "RTV-REC-01", "rule": RULES["RTV-REC-01"],
                                 "claimed": str(claimed), "credited": str(credited),
                                 "gap": str(gap), "tolerance": str(tolerance)})
                # 可退金额取**供应商认的那个**，不取我方自称的：贷项通知单是外部权威。
                creditable = str(credited)

        reconciled = 1 if creditable else 0
        attempt = _next_attempt(store, "rtv_reconciliation", tenant_id, case_id)
        reconciled_by = str(payload.get("reconciled_by") or "")
        reconciled_at = _now()
        _stub_execute(store, "INSERT INTO rtv_reconciliation (tenant_id, case_id, attempt,"
                             " reconciled, creditable_amount, findings_json,"
                             " tolerance_json, reconciled_by, reconciled_at)"
                             " VALUES (?,?,?,?,?,?,?,?,?)",
                      (tenant_id, case_id, attempt, reconciled, creditable,
                       json.dumps(findings, ensure_ascii=False),
                       json.dumps(self.TOLERANCE, ensure_ascii=False),
                       reconciled_by, reconciled_at))

        questions = [] if reconciled else [
            f"三方对账没对上（{findings[0]['rule_id']}）：{findings[0].get('message') or ''}"
            f" —— 需人工与供应商交涉，**不要**据此断定这笔退货已经作废"]
        case = get_case(store, tenant_id, case_id)
        return {"reconciliation": {"attempt": attempt, "reconciled": reconciled,
                                   "creditable_amount": creditable, "findings": findings,
                                   "tolerance": self.TOLERANCE,
                                   "reconciled_by": reconciled_by,
                                   "reconciled_at": reconciled_at,
                                   "credit_receipt": receipt},
                "biz_status": case["biz_status"], "open_questions": questions,
                "invocation_id": extras.get("invocation_id", "")}


@register_skill
class StubRtvObserveSkill(Skill):
    """STUB（归 T63）—— 本域**唯一**的权威写入方（C-R3）。

    两个权威终态分开问、分开写，中间隔着各自的回执：

      · 供应商侧 `issued` -> `credited`（`acknowledged` 不算，差着一次会计确认）；
      · AP 侧 `settled` -> `settled`（`none` / `pending` 都不算）。

    **问不出来就一个字都不写**：不推状态、不落 credit_note、不落终态观察行。
    「我问累了」和「供应商说不认」是两回事，躺一行伪造的记录比什么都不写坏得多。
    """

    contract = SkillContract(
        name="rtv.observe", version="1.0.0",
        purpose="轮询供应商与 AP 两个外部权威，取得终态回执并落库",
        preconditions=["tenant_id", "case_id"],
        depends_tools=["supplier.credit_query", "ap.adjust_query"],
        owner_roles=["rtv_settlement"],
        failure_policy="escalate",
        security_boundary=("本域唯一写得进 credited / settled 的地方；"
                           "两者都必须带一份外部回执，且只读外部系统"),
        reuse_note="STUB：整合期换成 maos/skills/builtin/rtv/observe.py",
    )

    def run(self, payload: dict, ctx: SkillContext) -> Any:
        store, extras = ctx.store, dict(ctx.extras)
        tenant_id, case_id = payload["tenant_id"], payload["case_id"]
        portal = get_portal(str(payload["supplier_portal"]))
        ap_system = get_ap_system(str(payload["ap_system"]))
        observed_by = str(payload.get("observed_by") or "")
        invocation_id = extras.get("invocation_id", "")
        max_polls = int(payload.get("max_polls") or 1)

        polls = 0
        credit = {"state": UNOBSERVED, "document_id": "", "amount": "", "currency": "",
                  "issued_at": "", "message": "一次也没问到贷项通知单"}
        adjust = {"state": UNOBSERVED, "document_id": "", "ap_reference": "",
                  "message": "一次也没问到 AP 调整凭单"}

        # 第一段：供应商认不认这笔退货 —— 认了才写 credited。
        while polls < max_polls:
            polls += 1
            credit = portal.credit_query(case_id)
            if credit["state"] in AUTHORITATIVE_RECEIPT_STATE["credited"]:
                break

        if credit["state"] in AUTHORITATIVE_RECEIPT_STATE["credited"]:
            _stub_execute(store, "INSERT INTO credit_note (tenant_id, case_id,"
                                 " credit_note_id, document_type, amount_credited,"
                                 " currency, issued_at, observed_at, observed_by,"
                                 " invocation_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                          (tenant_id, case_id, credit["document_id"],
                           credit.get("document_type", DOC_TYPE_CREDIT_NOTE),
                           credit["amount"], credit["currency"], credit["issued_at"],
                           _now(), observed_by, invocation_id))
            _guard_update_biz_status(store, tenant_id=tenant_id, case_id=case_id,
                                     to_status="credited", writer=self.contract.name,
                                     receipt_state=credit["state"],
                                     invocation_id=invocation_id, extras=extras)

            # 第二段：钱到没到账 —— 这是**另一个**外部系统说了算，所以另起一轮问。
            while polls < max_polls:
                polls += 1
                adjust = ap_system.adjust_query(case_id)
                if adjust["state"] in AUTHORITATIVE_RECEIPT_STATE["settled"]:
                    break

            if adjust["state"] in AUTHORITATIVE_RECEIPT_STATE["settled"]:
                seq = len(_stub_query(
                    store, "SELECT seq FROM rtv_settlement_observation WHERE tenant_id=?"
                           " AND case_id=?", (tenant_id, case_id))) + 1
                _stub_execute(store, "INSERT INTO rtv_settlement_observation (tenant_id,"
                                     " case_id, seq, adjustment_id, observed_state,"
                                     " ap_reference, observed_at, observed_by,"
                                     " invocation_id) VALUES (?,?,?,?,?,?,?,?,?)",
                              (tenant_id, case_id, seq, adjust["document_id"],
                               adjust["state"], adjust["ap_reference"], _now(),
                               observed_by, invocation_id))
                _guard_update_biz_status(store, tenant_id=tenant_id, case_id=case_id,
                                         to_status="settled", writer=self.contract.name,
                                         receipt_state=adjust["state"],
                                         invocation_id=invocation_id, extras=extras)

        case = get_case(store, tenant_id, case_id)
        return {"credit_receipt": credit, "adjustment_receipt": adjust,
                "poll_count": polls, "biz_status": case["biz_status"],
                "open_questions": self._open_questions(credit, adjust, polls),
                "invocation_id": invocation_id}

    @staticmethod
    def _open_questions(credit: dict, adjust: dict, polls: int) -> list[str]:
        """没拿到权威回执就把处置显式挂出来给人看 —— 但一律不改业务状态。

        判据留在 skill 里而不是 Agent 里：Agent 是薄壳，把这份列表原样搬出去
        （见 `maos/agents/rtv/settlement_agent.py` 的模块 docstring）。
        """
        if adjust["state"] in AUTHORITATIVE_RECEIPT_STATE["settled"]:
            return []
        if credit["state"] == SUPPLIER_DISPUTED:
            return [f"供应商明确不认这笔退货（问了 {polls} 次，回执恒 "
                    f"{SUPPLIER_DISPUTED}）：{credit['message']}；"
                    f"需转人工与供应商交涉，**不得**据此自行冲减应付"]
        if credit["state"] not in AUTHORITATIVE_RECEIPT_STATE["credited"]:
            return [f"问了 {polls} 次仍未拿到贷项通知单（当前 {credit['state']}）："
                    f"{credit['message']}；这不是失败，需继续观察"]
        return [f"贷项通知单已开出，但 AP 侧尚未核销（问了 {polls} 次，当前 "
                f"{adjust['state']}）：{adjust['message']}；这不是失败，需继续观察"]


@register_skill
class StubRtvCompensateSkill(Skill):
    """STUB（归 T63）—— 失败路径收口。

    补偿的语义是「MAOS 侧不再推进 + 留下最后观察到的下落 + 开交涉工单」，
    **不是**「这笔退货确认作废」。口径同 ap 域 `ap.compensate`：走到补偿的案子里
    那批货**已经发出去了**，写成「作废」会让账面上凭空少一批货，
    而供应商那边收到了它。
    """

    contract = SkillContract(
        name="rtv.compensate", version="1.0.0",
        purpose="退货走不通之后的域内补偿收口：提 RMA 留痕、开交涉工单、推进 compensated",
        preconditions=["tenant_id", "case_id"],
        depends_tools=["supplier.rma_submit"],
        owner_roles=["rtv_settlement"],
        failure_policy="escalate",
        security_boundary="只写 rtv_compensation_record 与 rtv_case.biz_status",
        reuse_note="STUB：整合期换成 maos/skills/builtin/rtv/compensate.py",
    )

    def run(self, payload: dict, ctx: SkillContext) -> Any:
        store, extras = ctx.store, dict(ctx.extras)
        tenant_id, case_id = payload["tenant_id"], payload["case_id"]
        case = get_case(store, tenant_id, case_id)
        if case["biz_status"] in AUTHORITATIVE_STATES:
            raise ValueError(f"退货案 {case_id} 已处于权威终态 {case['biz_status']}，"
                             f"不许补偿 —— 那是数据被改坏的信号，不是一次空操作")

        portal = get_portal(str(payload["supplier_portal"]))
        operator = str(payload.get("operator") or "")
        reason = str(payload.get("reason") or "")
        rma = portal.rma_submit(case_id, reason)           # supplier.rma_submit（写）

        # 最后观察到的下落：库里有没有贷项通知单说了算，**不猜**。
        notes = _stub_query(store, "SELECT * FROM credit_note WHERE tenant_id=? AND"
                                   " case_id=?", (tenant_id, case_id))
        recons = _stub_query(store, "SELECT * FROM rtv_reconciliation WHERE tenant_id=?"
                                    " AND case_id=? ORDER BY attempt DESC",
                             (tenant_id, case_id))
        last_state = UNOBSERVED
        if notes:
            last_state = SUPPLIER_ISSUED
        elif recons:
            findings = json.loads(recons[0]["findings_json"])
            last_state = str(findings[0].get("supplier_state") or UNOBSERVED) if findings \
                else UNOBSERVED

        ticket = {"ticket_id": f"TK-{case_id[-4:]}-01", "assignee": operator,
                  "last_supplier_state": last_state, "rma_id": rma["rma_id"],
                  "todo": ("与供应商核对这批已发出的退货：确认它是否已签收、"
                           "是否会补开贷项通知单。**不要**据本工单断定这批货或这笔钱作废")}
        records = [
            {"kind": "rma_submitted", "detail": rma},
            {"kind": "negotiation_ticket", "detail": ticket},
        ]
        for seq, rec in enumerate(records, start=1):
            _stub_execute(store, "INSERT INTO rtv_compensation_record (tenant_id, case_id,"
                                 " seq, reason, detail_json, compensated_by,"
                                 " compensated_at) VALUES (?,?,?,?,?,?,?)",
                          (tenant_id, case_id, seq, f"{rec['kind']}: {reason}",
                           json.dumps(rec["detail"], ensure_ascii=False), operator, _now()))

        case = _guard_update_biz_status(store, tenant_id=tenant_id, case_id=case_id,
                                        to_status="compensated", writer=self.contract.name,
                                        extras=extras)
        if store is not None:
            store.append_event_log({
                "event_id": "", "trace_id": extras.get("trace_id", ""),
                "plan_id": extras.get("plan_id", ""), "task_id": extras.get("task_id"),
                "event_type": "CompensationExecuted", "from_state": "", "to_state": "",
                "reason": reason,
                "detail": {"domain": DOMAIN, "case_id": case_id,
                           "last_supplier_state": last_state,
                           "records": [r["kind"] for r in records]},
            })
        return {"records": records, "rma": rma, "ticket": ticket,
                "last_supplier_state": last_state, "biz_status": case["biz_status"],
                "invocation_id": extras.get("invocation_id", "")}


#: C-R4 的六个 skill 名。测试按它对照契约，场景里不抄字面量。
STUB_SKILL_NAMES = ("rtv.intake", "rtv.dispose", "rtv.ship", "rtv.reconcile",
                    "rtv.observe", "rtv.compensate")


def stub_skill_classes() -> dict[str, type[Skill]]:
    """本文件注册的六个 stub skill 类，name -> 类。整合期这个函数一起删掉。"""
    return {name: SKILL_REGISTRY[name]["1.0.0"] for name in STUB_SKILL_NAMES}


# =========================================================================
# 场景常量 —— 全部写死，两次跑出来的结论逐条一致
# =========================================================================
TENANT_ID = "tnt-mfg-rtv"
SUPPLIER_ID = "SUP-8802"

#: 顺利路径的源单与案子。
CASE_OK = "case-rtv-0001"
PO_OK = "PO-2026-0715"
GR_OK = "GR-2026-0808"

#: 失败路径**另起一套单号**：顺利路径那批货已经贷记结清了，
#: 在它上面再退一次不是演示，是在演一个不该发生的动作。
CASE_BAD = "case-rtv-0002"
PO_BAD = "PO-2026-0722"
GR_BAD = "GR-2026-0815"

#: 三个外部系统按名取（task.inputs 会被 json.dumps，实例塞不进去）。
PORTAL_OK = "s11-portal"
PORTAL_BAD = "s11-portal-disputed"
CARRIER_OK = "s11-express"
CARRIER_BAD = "s11-express-b"
AP_OK = "s11-ap"
AP_BAD = "s11-ap-idle"

#: 顺利路径：供应商第 1 次问就已开票；AP 要问到第 2 次才核销 ——
#: 「终态是问出来的」由 AP 侧演，poll_count 因此恒为 3（供应商 1 次 + AP 2 次）。
ISSUE_AFTER_OK = 1
SETTLE_AFTER_OK = 2
MAX_POLLS_OK = 5
EXPECTED_POLLS_OK = 3

#: 失败路径：供应商恒 disputed，轮询上限 3 —— 保证**一定**拿不到贷项通知单。
MAX_POLLS_BAD = 3

#: 退货行。**退货量单独记一处，不改 goods_receipt_line**：PeopleSoft 口径里
#: `Quantity Received` 是审计量、永不变（C-R1 的 rtv_line 注释）。
#  line_no, gr_line_no, sku, 退货数, 单价, 理由码
LINES_OK = [
    (1, 1, "SKU-CAST-A1", 12.0, "260.00", "RC-DEFECT"),
    (2, 2, "SKU-CAST-B7", 4.0, "185.00", "RC-DEFECT"),
]
#: 我方算出来的合计：12×260 + 4×185 = 3860.00。
AMOUNT_CLAIMED_OK = "3860.00"
#: 🔴 供应商**认的**金额刻意与我方自称的差 0.50 元（容差 1.00 之内）。
#: 两处金额都留着、不合并成一处，正是 C-R1 那条注释要的东西：
#: 「退货方自称的应退金额」与「供应商贷项通知单认的金额」是两个事实，
#: 对不上正是本域要拦的事。可退金额取**后者**。
AMOUNT_CREDITED_OK = "3859.50"

LINES_BAD = [
    (1, 1, "SKU-CAST-C3", 6.0, "410.00", "RC-DEFECT"),
]
AMOUNT_CLAIMED_BAD = "2460.00"

TASK_INTAKE = "task-s11-intake"
TASK_DISPOSE = "task-s11-dispose"
TASK_SHIP = "task-s11-ship"
TASK_RECONCILE = "task-s11-reconcile"
TASK_OBSERVE = "task-s11-observe"

TASK_INTAKE_B = "task-s11b-intake"
TASK_DISPOSE_B = "task-s11b-dispose"
TASK_SHIP_B = "task-s11b-ship"
TASK_RECONCILE_B = "task-s11b-reconcile"
TASK_OBSERVE_B = "task-s11b-observe"

APPROVER = "@boss-rtv:maos.local"
SHIP_APPROVE_REASON = f"处置已裁定且依据 {list(RULES)[0]}，同意发运退货"
OBSERVE_APPROVE_REASON = "贷项通知单与 AP 调整凭单都已取得，确认收口"
REJECT_REASON = "供应商不认这笔退货，转人工交涉"

GOAL_OK = "退回华南精密铸造 2026-08 到货不合格件：裁定 credit 后发运、对账、取两方终态回执"
GOAL_BAD = "退回华南精密铸造错发件：货已发出，但供应商不认这笔退货"

#: 本域不出方案，DAG 直接交给 `create_plan`。ScriptedModelClient 仍要给一份脚本：
#: Reviewer 的语义审查会问模型。
REVIEW_JSON = json.dumps({
    "defects": [],
    "conclusion": ("退货处置依据 RTV-DISP-01，可退金额取供应商贷项通知单认的那个；"
                   "两个权威终态各有回执兜底，可放行"),
}, ensure_ascii=False)

SCRIPT = {"语义审查产物清单": REVIEW_JSON}

#: 补偿用的 identity。**复用观察岗自己的 identity**，不另造一个 ——
#: C-R7 已经把 `rtv.compensate` 放进 `rtv_settlement` 的白名单，
#: 另起一个 `rtv_compensation` 角色会让「补偿是谁做的」多一个含糊的答案
#: （ap 域那条 `owner-role-unknown: ap.compensate` 就是这么来的）。
#: 补偿仍然是**人做出决定之后**的动作，所以由编排层发起，不在 Agent 的 `run()` 里。
COMPENSATION_IDENTITY = RtvSettlementAgent.identity


# ---------------------------------------------------------------------- 靶场
def seed_case_inputs(*, po_id: str, gr_id: str, lines: list,
                     amount_claimed: str) -> dict:
    """一个案子的受理入参。

    STUB：源单（PO / 收货单）信息从这里进来。整合期换成 `maos/domain/rtv/fixtures.py`
    从**复用**的 ap 域五张表（supplier / purchase_order / purchase_order_line /
    goods_receipt / goods_receipt_line）里读 —— 那五张表 RTV 只引用不重建，
    这正是 `docs/domain-portability.md` 那句主张的落点。
    """
    return {
        "supplier_id": SUPPLIER_ID, "po_id": po_id, "po_version": 1, "gr_id": gr_id,
        "amount_claimed": amount_claimed, "currency": "CNY",
        "lines": [{"line_no": ln, "gr_line_no": grl, "sku": sku,
                   "quantity_returned": qty, "unit_price": price, "reason_code": rc}
                  for ln, grl, sku, qty, price, rc in lines],
    }


def _tasks(*, case_id: str, seed: dict, carrier: str, portal: str, ap_system: str,
           max_polls: int, ids: tuple[str, str, str, str, str]) -> list[dict]:
    """一条路径的五任务 DAG。两条路径同构，只换案子与三个外部系统。

    依赖链照 SOP 五步（契约 C-R2 的映射表）：
    `rtv_intake -> rtv_disposition -> rtv_logistics -> rtv_reconcile -> rtv_settlement`

    `biz_type` 用本域自己的标记（`"rtv"`），**不是 `"refund"`** —— 第六道财务复核闸
    按 `biz_type == "refund"` 触发，冒用会让闸恒 blocker，而报错信息指向退款域的
    财务复核，离原因极远（口径同 `flows/scenario_10.py::_tasks`）。
    """
    t_intake, t_dispose, t_ship, t_recon, t_observe = ids
    base = {"biz_type": DOMAIN, "tenant_id": TENANT_ID, "case_id": case_id}
    return [
        {"task_id": t_intake, "role": ROLE_INTAKE,
         "title": "受理退货诉求并定位源 PO 与收货单",
         "inputs": {**base, **seed},
         "acceptance": ["建出 rtv_case 且 biz_status=received",
                        "return_action 留空 —— 受理不替裁定拍板"],
         "depends_on": [], "risk_level": "L"},

        {"task_id": t_dispose, "role": ROLE_DISPOSITION,
         "title": "裁定处置类型 credit/exchange/replacement",
         "inputs": {**base},
         "acceptance": ["三选一", "裁定依据必须挂真实规则编号"],
         "depends_on": [t_intake], "risk_level": "L"},

        # effect_risk=H 挂在**发运**上：货一旦发出去就退不回来，Gate 过了也停在
        # BLOCKED 等人放行。这是本域的第一个不可逆动作。
        {"task_id": t_ship, "role": ROLE_LOGISTICS,
         "title": "登记退货发运并取得承运商回执",
         "inputs": {**base, "carrier": carrier},
         "acceptance": ["shipped 只能由承运商回执得到", "运单号入库"],
         "depends_on": [t_dispose], "risk_level": "M", "effect_risk": "H"},

        {"task_id": t_recon, "role": ROLE_RECONCILE,
         "title": "退货行 × 贷项通知单 × 到账三方对账",
         "inputs": {**base, "supplier_portal": portal},
         "acceptance": ["可退金额取供应商认的那个", "对不上的理由必须挂规则编号"],
         "depends_on": [t_ship], "risk_level": "M"},

        # 观察任务同样 effect_risk=H：确认收口是不可逆动作，Gate 过了也要人放行。
        # 失败路径的转折点就在这里 —— 主管拿到的不是「已贷记」，是一份供应商
        # 明确不认的回执，于是他驳回。
        {"task_id": t_observe, "role": ROLE_SETTLEMENT,
         "title": "轮询供应商与 AP 两个外部权威的终态回执",
         "inputs": {**base, "supplier_portal": portal, "ap_system": ap_system,
                    "max_polls": max_polls},
         "acceptance": ["credited 只能由 issued 回执得到",
                        "settled 只能由 AP 调整凭单核销得到"],
         "depends_on": [t_recon], "risk_level": "M", "effect_risk": "H"},
    ]


def _count(store, sql: str, params: tuple = ()) -> int:
    return _stub_query(store, sql, params)[0]["n"]


def artifact_of(store, task_id: str, kind: str) -> dict:
    """取某任务**最近一轮**的某类产物。

    按 `version`（= 产出它的那次 attempt）取最大的一份，不取列表里的第一份：
    返工之后一个任务会有多份同类产物，而收口的依据只能是最后那一份。
    """
    arts = [a for a in store.list_artifacts(task_id) if a["kind"] == kind]
    if not arts:
        raise LookupError(f"{task_id} 没有 {kind} 产物")
    return max(arts, key=lambda a: a["version"])["content"]


def compensate(store, *, plan_id: str, task_id: str, trace_id: str, case_id: str,
               portal: str, operator: str, reason: str) -> dict:
    """编排层以观察岗的 identity 调 `rtv.compensate`。

    走 SkillInvoker 而不是直接 `StubRtvCompensateSkill().run()`：白名单校验与
    SkillInvoked 审计行都在 invoker 里，直接调就没有审计行，出事之后查不到是谁做的。
    """
    invoker = SkillInvoker(COMPENSATION_IDENTITY, store)
    res = invoker.invoke(SKILL_COMPENSATE, {
        "tenant_id": TENANT_ID, "case_id": case_id, "supplier_portal": portal,
        "operator": operator, "reason": reason,
    }, extras={"plan_id": plan_id, "task_id": task_id, "trace_id": trace_id})
    if res.status != "ok" or not isinstance(res.output, dict):
        raise RuntimeError(f"域内补偿失败，不许静默收口：{res.error}")
    return res.output


def _agent_status(store, plan_id: str) -> dict:
    """五个 Agent 各自的**自述结论** —— 取自控制面的状态迁移，不从任务终态推。

    判据是 `RUNNING -> AWAITING_REVIEW` 这一跳：`ControlPlane.on_task_result` 里
    只有 `status == "ok"` 那一条分支走这一跳，`blocked` 走 BLOCKED、
    `failed` 走 PENDING/FAILED。所以「这个任务出现过这一跳」与「它的 Agent 回了 ok」
    是同一件事。

    **不取任务终态**：那正是本场景要对比的另一个东西。观察任务最终是 FAILED，
    但那是**人**驳回的结果，不是 Agent 的自述 —— Agent 那一步跑完了、产物交了、
    一个异常都没抛。两者的差就是本场景要讲的话。
    """
    out: dict[str, str] = {}
    for e in store.list_event_log(plan_id):
        if (e["event_type"] == "StateTransition"
                and e["from_state"] == TaskState.RUNNING
                and e["to_state"] == TaskState.AWAITING_REVIEW):
            out[e["task_id"]] = "ok"
    return out


# ------------------------------------------------------------------ 顺利路径
def drive_happy(*, matrix: bool = False) -> dict:
    """跑完顺利路径并返回收口用的句柄。**只跑不断言** —— 断言在 run() 里。

    拆出这一层是给 `maos/tests/test_rtv_flow.py` 用的：测试要对**库里的行**下断言，
    而 run() 只返回一个退出码，store 拿不出来。让测试自己再拼一遍流程则等于维护
    第二份场景，两边迟早漂。
    """
    print("场景 11（顺利路径）：采购退货退款 —— 供应商开了票、AP 核销了，才算退成")

    model = select_model_client(SCRIPT, force_scripted=True)
    store, bus, cp, model, worker, gate = build(SCRIPT, matrix=matrix, model=model)
    ensure_schema(store)

    reset_stub_tools()
    register_portal(PORTAL_OK, StubSupplierPortal(issue_after=ISSUE_AFTER_OK,
                                                  amount=AMOUNT_CREDITED_OK))
    register_carrier(CARRIER_OK, StubCarrier(status="delivered"))
    register_ap_system(AP_OK, StubApSystem(settle_after=SETTLE_AFTER_OK))

    trace_id, plan_id = new_id("trace"), new_id("plan")
    seed = seed_case_inputs(po_id=PO_OK, gr_id=GR_OK, lines=LINES_OK,
                            amount_claimed=AMOUNT_CLAIMED_OK)
    cp.create_plan(goal=GOAL_OK, trace_id=trace_id, plan_id=plan_id,
                   tasks=_tasks(case_id=CASE_OK, seed=seed, carrier=CARRIER_OK,
                                portal=PORTAL_OK, ap_system=AP_OK,
                                max_polls=MAX_POLLS_OK,
                                ids=(TASK_INTAKE, TASK_DISPOSE, TASK_SHIP,
                                     TASK_RECONCILE, TASK_OBSERVE)))
    cp.start_plan(plan_id)
    run_until_settled(bus, gate, cp, plan_id)

    intake = artifact_of(store, TASK_INTAKE, KIND_RTV_INTAKE)
    dispo = artifact_of(store, TASK_DISPOSE, KIND_RTV_DISPOSITION)
    print(f"\n[1] 受理    : {intake['case']['case_id']}（源单 "
          f"{intake['case']['po_id']} v{intake['case']['po_version']} / "
          f"{intake['case']['gr_id']}），退货 {len(intake['lines'])} 行，"
          f"自称应退 {intake['case']['amount_claimed']}")
    print(f"    业务引用: {[r['object_table'] for r in intake['refs']]} —— "
          f"「退的是哪一张 PO 的哪一次收货」指得回去")
    print(f"\n[2] 处置裁定: {dispo['disposition']['return_action']}"
          f"（依据 {[r['rule_id'] for r in dispo['disposition']['rationale']]}）")

    hq = HumanApprovalQueue(store, cp)

    # —— 人工介入其一：发运不可逆，要人批 ——
    pending = hq.pending(plan_id)
    assert [t["task_id"] for t in pending] == [TASK_SHIP], (
        f"应停在发运的人工审批上，实际 {[t['task_id'] for t in pending]}")
    print(f"\n[3] 待主管审批: {pending[0]['title']}（effect_risk="
          f"{pending[0]['effect_risk']}，货发出去就退不回来）")
    hq.decide(TASK_SHIP, approved=True, operator=APPROVER, note=SHIP_APPROVE_REASON)
    run_until_settled(bus, gate, cp, plan_id)

    ship = artifact_of(store, TASK_SHIP, KIND_RTV_SHIPMENT)
    recon = artifact_of(store, TASK_RECONCILE, KIND_RTV_RECONCILIATION)
    print(f"\n[4] 退货发运: {ship['shipment']['shipment_id']}，运单 "
          f"{ship['shipment']['tracking_no']}，承运商回执 "
          f"{ship['shipment']['carrier_status']} —— 状态来自回执，不是本地推断")
    r = recon["reconciliation"]
    print(f"\n[5] 三方对账: reconciled={r['reconciled']}，可退 {r['creditable_amount']}"
          f"（我方自称 {AMOUNT_CLAIMED_OK}，供应商认 {AMOUNT_CREDITED_OK}，"
          f"差 {r['findings'][0].get('gap')} 在容差 {r['tolerance']['amount']} 内）")
    print(f"    依据    : {[f['rule_id'] for f in r['findings']]}")
    print(f"    —— 对账问的是同一个 supplier.credit_query，拿的是同一份回执，"
          f"可它写不进 credited：那归 {AUTHORITATIVE_WRITER}")

    # —— 人工介入其二：确认收口 ——
    pending = hq.pending(plan_id)
    assert [t["task_id"] for t in pending] == [TASK_OBSERVE], (
        f"应停在终态确认的人工审批上，实际 {[t['task_id'] for t in pending]}")
    advice = artifact_of(store, TASK_OBSERVE, KIND_RTV_SETTLEMENT_ADVICE)
    print(f"\n[6] 终态回执: 供应商侧 {advice['credit_receipt']['state']}"
          f"（贷项通知单 {advice['credit_receipt']['document_id']}，类型 "
          f"{advice['credit_receipt'].get('document_type')}）、AP 侧 "
          f"{advice['adjustment_receipt']['state']}"
          f"（调整凭单 {advice['adjustment_receipt']['document_id']}，"
          f"{advice['adjustment_receipt']['ap_reference']}）")
    print(f"    轮询次数: {advice['poll_count']} —— 两个权威源分开问、分开写；"
          f"acknowledged 换不来 credited")
    hq.decide(TASK_OBSERVE, approved=True, operator=APPROVER,
              note=OBSERVE_APPROVE_REASON)
    run_until_settled(bus, gate, cp, plan_id)

    dump(cp, plan_id, "场景 11 顺利路径：采购退货退款五步 SOP")
    return {"store": store, "cp": cp, "bus": bus, "gate": gate, "hq": hq,
            "plan_id": plan_id, "trace_id": trace_id, "intake": intake,
            "disposition": dispo, "shipment": ship, "reconciliation": recon,
            "advice": advice, "agent_status": _agent_status(store, plan_id)}


# ------------------------------------------------------------------ 失败路径
def drive_failure(*, matrix: bool = False) -> dict:
    """跑完失败路径并返回收口用的句柄。**只跑不断言**。

    另起一套运行时（不复用顺利路径那个）：收口断言里有全库口径的
    「终态观察 0 条」，两条路径共库的话它校验的就不再是本条链路了。
    """
    print("\n场景 11（失败路径）：货已发出、五个 Agent 全回 ok —— "
          "而供应商不认这笔退货，系统如实记下来")

    model = select_model_client(SCRIPT, force_scripted=True)
    store, bus, cp, model, worker, gate = build(SCRIPT, matrix=matrix, model=model)
    ensure_schema(store)

    reset_stub_tools()
    # 🔴 供应商**恒**回 disputed：失败演的是「供应商不认」这件业务事实，
    # 不是「代码抛了异常」—— 后者是 bug，证明不了任何关于编排的事。
    register_portal(PORTAL_BAD, StubSupplierPortal(disputed=True))
    register_carrier(CARRIER_BAD, StubCarrier(status="delivered"))
    register_ap_system(AP_BAD, StubApSystem(idle=True))

    trace_id, plan_id = new_id("trace"), new_id("plan")
    seed = seed_case_inputs(po_id=PO_BAD, gr_id=GR_BAD, lines=LINES_BAD,
                            amount_claimed=AMOUNT_CLAIMED_BAD)
    cp.create_plan(goal=GOAL_BAD, trace_id=trace_id, plan_id=plan_id,
                   tasks=_tasks(case_id=CASE_BAD, seed=seed, carrier=CARRIER_BAD,
                                portal=PORTAL_BAD, ap_system=AP_BAD,
                                max_polls=MAX_POLLS_BAD,
                                ids=(TASK_INTAKE_B, TASK_DISPOSE_B, TASK_SHIP_B,
                                     TASK_RECONCILE_B, TASK_OBSERVE_B)))
    cp.start_plan(plan_id)
    run_until_settled(bus, gate, cp, plan_id)

    hq = HumanApprovalQueue(store, cp)
    pending = hq.pending(plan_id)
    assert [t["task_id"] for t in pending] == [TASK_SHIP_B], (
        f"应先停在发运的人工审批上，实际 {[t['task_id'] for t in pending]}")
    print(f"\n[1] 待主管审批: {pending[0]['title']} —— 处置已裁定，同意发运")
    hq.decide(TASK_SHIP_B, approved=True, operator=APPROVER, note=SHIP_APPROVE_REASON)
    run_until_settled(bus, gate, cp, plan_id)

    recon = artifact_of(store, TASK_RECONCILE_B, KIND_RTV_RECONCILIATION)
    advice = artifact_of(store, TASK_OBSERVE_B, KIND_RTV_SETTLEMENT_ADVICE)
    r = recon["reconciliation"]
    print(f"\n[2] 三方对账: reconciled={r['reconciled']}，findings "
          f"{[f['rule_id'] for f in r['findings']]} —— "
          f"{r['findings'][0]['message']}")
    print(f"\n[3] 终态回执: 供应商侧 {advice['credit_receipt']['state']}"
          f"（问了 {advice['poll_count']} 次），AP 侧 "
          f"{advice['adjustment_receipt']['state']}")
    print(f"    贷项通知单: {_count(store, 'SELECT COUNT(*) AS n FROM credit_note')} 条")
    print(f"    终态观察  : "
          f"{_count(store, 'SELECT COUNT(*) AS n FROM rtv_settlement_observation')} 条"
          f" —— 问不出来就一个字都不写")

    agent_status = _agent_status(store, plan_id)
    print(f"\n[4] 五个 Agent 的自述: {agent_status}")
    print(f"    全部 ok。而 rtv_case.biz_status = "
          f"{get_case(store, TENANT_ID, CASE_BAD)['biz_status']} —— "
          f"「Agent 说完成了」不等于业务成功了")

    pending = hq.pending(plan_id)
    assert [t["task_id"] for t in pending] == [TASK_OBSERVE_B], (
        f"观察任务应停在 BLOCKED 等人处置，实际 {[t['task_id'] for t in pending]}")
    print(f"\n[5] 待主管处置: {pending[0]['title']} —— 供应商明确不认，不能当成功放行")

    # 先补偿、再落 FAILED —— 与 control_plane.human_decision 同一个顺序与同一个理由：
    # 状态一旦落 FAILED，「那批货还在供应商那里」这件事就没人记得了。
    comp = compensate(store, plan_id=plan_id, task_id=TASK_OBSERVE_B, trace_id=trace_id,
                      case_id=CASE_BAD, portal=PORTAL_BAD, operator=APPROVER,
                      reason=REJECT_REASON)
    print(f"\n[6] 域内补偿: 提 RMA {comp['rma']['rma_id']}，开交涉工单 "
          f"{comp['ticket']['ticket_id']} -> {comp['ticket']['assignee']}")
    print(f"    最后观察到的供应商说法 = {comp['last_supplier_state']}"
          f"（**是供应商自己说的**，不是我方替它下的结论）")
    print(f"    工单待办: {comp['ticket']['todo']}")

    hq.decide(TASK_OBSERVE_B, approved=False, operator=APPROVER, note=REJECT_REASON)
    bus.drain()

    dump(cp, plan_id, "场景 11 失败路径：供应商不认这笔退货")
    return {"store": store, "cp": cp, "plan_id": plan_id, "trace_id": trace_id,
            "reconciliation": recon, "advice": advice, "compensation": comp,
            "agent_status": agent_status}


# -------------------------------------------------------------------------- run
def run(*, matrix: bool = False) -> int:
    ok = drive_happy(matrix=matrix)
    _assert_happy(ok)
    bad = drive_failure(matrix=matrix)
    _assert_failure(bad)
    return 0


def _assert_happy(out: dict) -> None:
    """顺利路径的收口断言。"""
    store, cp, plan_id = out["store"], out["cp"], out["plan_id"]
    case = get_case(store, TENANT_ID, CASE_OK)
    notes = _stub_query(store, "SELECT * FROM credit_note WHERE tenant_id=? AND case_id=?",
                        (TENANT_ID, CASE_OK))
    obs = _stub_query(store, "SELECT * FROM rtv_settlement_observation WHERE tenant_id=?"
                             " AND case_id=?", (TENANT_ID, CASE_OK))
    plan = cp.store.get_plan(plan_id)

    print(f"\n  业务状态  : {case['biz_status']}")
    print(f"  贷项通知单: {len(notes)} 条 {[n['credit_note_id'] for n in notes]}")
    print(f"  终态观察  : {len(obs)} 条 {[o['ap_reference'] for o in obs]}")
    print(f"  Plan 终态 : {plan['state']}")

    assert case["biz_status"] == "settled", (
        f"AP 核销之后业务状态应为 settled，实际 {case['biz_status']}")
    assert case["return_action"] == "credit", (
        f"到货不合格应裁 credit，实际 {case['return_action']}")
    # 两个权威终态各要一份外部回执兜底 —— 这是本域比 ap 域多要的那一条。
    assert len(notes) == 1 and notes[0]["document_type"] == DOC_TYPE_CREDIT_NOTE, (
        f"credited 必须恰好有一张贷项通知单兜底，实际 {len(notes)} 张")
    assert notes[0]["invocation_id"], "贷项通知单必须带 actor 锚点，否则审计链断了"
    assert len(obs) == 1 and obs[0]["observed_state"] == AP_SETTLED, (
        f"settled 必须恰好有一条终态观察兜底，实际 {[o['observed_state'] for o in obs]}")
    assert obs[0]["ap_reference"], "终态观察必须带 AP 单号 —— 没有单号的「已退」对不了账"
    assert obs[0]["invocation_id"], "观察必须带 actor 锚点，否则审计链断了"
    # 终态是**问出来的**：一次 query 不够。
    assert out["advice"]["poll_count"] == EXPECTED_POLLS_OK, (
        f"应恰好轮询 {EXPECTED_POLLS_OK} 次，实际 {out['advice']['poll_count']}")
    # 可退的钱是**供应商认的**，不是我方自称的。
    r = out["reconciliation"]["reconciliation"]
    assert r["creditable_amount"] == AMOUNT_CREDITED_OK != AMOUNT_CLAIMED_OK, (
        f"可退金额必须取供应商贷项通知单认的那个，实际 {r['creditable_amount']}")
    assert plan["state"] == PlanState.DONE, (
        f"顺利路径应收敛到 DONE，实际 {plan['state']}")
    _assert_frozen_states(cp, plan_id)


def _assert_failure(out: dict) -> None:
    """失败路径的收口断言 —— 与场景 7 / 场景 10 同构的那份收口。"""
    store, cp, plan_id = out["store"], out["cp"], out["plan_id"]
    case = get_case(store, TENANT_ID, CASE_BAD)
    comp_rows = _stub_query(store, "SELECT * FROM rtv_compensation_record WHERE"
                                   " tenant_id=? AND case_id=? ORDER BY seq",
                            (TENANT_ID, CASE_BAD))
    comp_events = [e for e in cp.store.list_event_log(plan_id)
                   if e["event_type"] == "CompensationExecuted"]
    plan = cp.store.get_plan(plan_id)
    comp = out["compensation"]

    print(f"\n  业务状态  : {case['biz_status']}（全程没有经过 credited / settled）")
    print(f"  贷项通知单: {_count(store, 'SELECT COUNT(*) AS n FROM credit_note')} 条")
    print(f"  终态观察  : "
          f"{_count(store, 'SELECT COUNT(*) AS n FROM rtv_settlement_observation')} 条")
    print(f"  补偿记录  : {len(comp_rows)} 行 {[r['reason'].split(':')[0] for r in comp_rows]}")
    print(f"  补偿事件  : {len(comp_events)} 条 CompensationExecuted")
    print(f"  Plan 终态 : {plan['state']}（主管驳回，业务确实没成功）")
    print(f"  Agent 自述 : {out['agent_status']} —— 五个全 ok，而案子没成")

    # —— 本轨要买的第二件东西：Agent 全回 ok ≠ 业务成功 ——
    statuses = out["agent_status"]
    assert len(statuses) == 5 and set(statuses.values()) == {"ok"}, (
        f"失败路径上五个 Agent 都应回 ok（这正是要演的），实际 {statuses}")

    # —— 失败的原因是**供应商不认**，不是异常 ——
    assert out["advice"]["credit_receipt"]["state"] == SUPPLIER_DISPUTED, (
        f"失败路径演的必须是「供应商不认」，实际回执 "
        f"{out['advice']['credit_receipt']['state']}")
    assert out["reconciliation"]["reconciliation"]["findings"][0]["rule_id"] == "RTV-REC-03"

    # —— 本场景存在的理由，第一断言 ——
    assert case["biz_status"] == "compensated", (
        f"补偿之后业务状态应为 compensated，实际 {case['biz_status']}")
    assert _count(store, "SELECT COUNT(*) AS n FROM credit_note") == 0, (
        "供应商没开票就一张贷项通知单都不该有 —— 有就说明有人自己给自己开了发票")
    assert _count(store, "SELECT COUNT(*) AS n FROM rtv_settlement_observation") == 0, (
        "轮询到顶没拿到终态时一条观察都不该写 —— 「我问累了」不是可以落库的结论")

    # —— 补偿真发生过：记录、事件、语义三样都在 ——
    assert len(comp_rows) == 2, f"补偿必须同时留下 RMA 留痕与交涉工单，实际 {len(comp_rows)} 行"
    assert comp_events, "补偿执行必须落 CompensationExecuted，否则这件事只活在日志里"
    assert comp["last_supplier_state"] == SUPPLIER_DISPUTED, (
        f"供应商明确说了不认，最后观察就该如实记 {SUPPLIER_DISPUTED}，"
        f"实际 {comp['last_supplier_state']}")

    assert plan["state"] == PlanState.FAILED, (
        f"主管驳回后 Plan 应收敛到 FAILED，实际 {plan['state']}")
    _assert_frozen_states(cp, plan_id)


def _assert_frozen_states(cp, plan_id: str) -> None:
    """铁律 9：业务状态不进 Task 状态机，也没有为本域新开一条迁移。

    断言两件事而不是一件：只查状态集合挡不住「用既有的两个状态连一条新边」。
    """
    known_states = {v for k, v in vars(TaskState).items()
                    if not k.startswith("_") and isinstance(v, str)}
    task_states = {t["state"] for t in cp.store.list_tasks(plan_id)}
    assert task_states <= known_states, (
        f"出现了不在既有 Task 状态机内的状态：{sorted(task_states - known_states)}")
    for biz in BIZ_STATUS_FLOW:
        assert biz not in task_states, (
            f"{biz} 是 rtv_case 自己的字段，不许变成 Task 状态（铁律 9）")
    moves = {(e["from_state"], e["to_state"]) for e in cp.store.list_event_log(plan_id)
             if e["event_type"] == "StateTransition"}
    assert moves <= set(TASK_TRANSITIONS), (
        f"出现了不在冻结迁移表里的 Task 迁移：{sorted(moves - set(TASK_TRANSITIONS))}")
