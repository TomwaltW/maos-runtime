"""场景 12：采购退货退款（RTV）五步 SOP —— 顺利路径 + 失败路径，一个文件两条。

    顺利路径 drive_happy()
      ① 受理：从**复用的 ap 域五张源单表**定位 PO/收货单，建案
        （return_action 留空，受理不替裁定拍板）
        → ② 裁定：按退货理由裁 credit，rationale 带规则编号
        → ③ 发运：carrier.ship 下运单 + carrier.track 轮询到 delivered → shipped
        → ④ 观察第 1 跳：问供应商侧，acknowledged 两次之后第 3 次拿到 issued
             → **这时才**写 credited，贷项通知单与状态同事务落库
        → ⑤ 观察第 2 跳：问 AP 侧，staged → built → 第 3 次 settled
             → **这时才**写 settled
        → ⑥ 对账：三条腿（退货行合计 × 贷项通知单 × 到账观察）都齐了才对得上，
             可退 = 我方按退货行算的那个，而供应商**认的**是另一个数，两处都留着
        → Plan DONE

    失败路径 drive_failure()
      同样受理、裁定、发运，观察第 1 跳问到的是 **disputed（供应商不认这笔退货）**
        → 观察岗**一个字都不写**：不推 credited、不落 credit_note、不落终态观察行，
             只把「供应商说不认」落成一条 adverse 事件
        → 第 2 跳：案子还在 shipped，于是仍问供应商侧（问哪个外部权威由案子当前
             状态决定，见 `observe._STAGES`），仍是 disputed
        → 对账：三条腿一条都不齐，findings 挂 RTV-R-13 / RTV-R-11 / RTV-R-12
        → 两个观察任务 effect_risk=H，Gate 过了也停在 BLOCKED 等人处置
        → 主管驳回：供应商不认，转人工与供应商交涉
        → rtv.compensate：补提 RMA 留痕 + biz_status -> compensated
        → Plan FAILED
        → rtv_case.biz_status = 'compensated'，**从未进入 credited / settled**

## 本域比 ap / refund 域多出来的那一条：**两个**权威源

契约 C-R3 的 `AUTHORITATIVE_STATES` 有两个元素。「供应商认不认这笔退货」和
「钱到没到账」是两件独立的事，由两个不同的外部系统说了算，MAOS 一个都写不了。
所以顺利路径上这两步是**分开**问的、分开写的，中间隔着一次 `acknowledged` ——
那是「供应商收到退货了」，不是「供应商认了这笔钱」，两者在回执里字段齐全、
形状一样，但差着一次会计确认。守卫 `AUTHORITATIVE_RECEIPT_STATE` 只认 `issued`。

## 🔴 为什么 DAG 是**六个**节点，而 SOP 只有五步

`rtv.observe` **一次只问一个外部系统**：问哪个由案子当前的业务状态决定
（`observe._STAGES`：`shipped` 问供应商、`credited` 问 AP），一次调用最多推进一跳。
这不是实现上的偷懒，是它 docstring 里那句「有几个外部权威就分几跳，
**一个都不许由另一个推定**」的直接后果 —— 把两跳合成一次调用，
就等于让「供应商开票了」顺带宣布「钱到账了」，而那是本域最难在事后发现的错。

所以 SOP 第 ⑤ 步「观察」在 DAG 上是**两个任务**，都由 `rtv_settlement` 角色执行、
都 `effect_risk=H`。角色仍是五个（C-R7），节点是六个。

## 🔴 为什么对账排在观察**之后**

`rtv.reconcile` 的三条腿里，第 ② 条（贷项通知单）与第 ③ 条（到账）**只认库里由
`rtv.observe` 落下的那份**，不认它自己问到的回执 —— 它照样会问一次
`supplier.credit_query`，但问到的东西「只用来解释缺口，不当作凭据」
（见 `reconcile.py` 的模块 docstring）。想在对账这一步顺手落一行 `credit_note`
是走不通的：`_common.execute()` 直接拒（`BypassedGuardError`）。

于是对账排在两跳观察之前的话，它**必然**对不上，findings 恒为
`RTV-R-11`（供应商尚未开票）+ `RTV-R-12`（AP 侧尚无核销观察）。那是如实报告、
不是失败，但它把「三方对账」演成了「一方独白」。本场景把对账放在收尾：
三方都说完话之后再勾稽，`reconciled=1`，findings 为空。

## 同一份回执，谁能拿它写权威终态

第 ⑥ 步的对账与第 ④ 步的观察问的是**同一个** `supplier.credit_query`，
拿到的是**同一份** issued 回执。可只有 `rtv.observe` 写得进 `credited`
（C-R3 的 `AUTHORITATIVE_WRITER`）—— 对账那一步只把它拿去算数。
换个写入方去写，`guard.update_biz_status` 抛 `AuthoritativeFactViolation` 并落一条
事件（`maos/tests/test_rtv_flow.py` 里有一条用例直接这么试）。

## 🔴 本场景**不在** `maos/main.py` 的 `ALL_SCENARIOS` / `DEFAULT_SCENARIOS` 里

缺省证据束恒为 8 束（`scenario-1..7` + `scenario-R5`）是跨轨冻结口径，
`scripts/demo_preflight.sh` 与复赛材料都写死了 8。所以本场景与
`scenario_8` / `scenario_9` / `scenario_10` 一样只有独立 `run()`，
由 `maos/tests/test_rtv_flow.py` 调用，人工也可以直接 `python3 -c` 调。
`maos/tests/test_rtv_flow.py::test_scenario_12_is_not_wired_into_default_scenarios`
钉着这件事。

===============================================================================
## 整合来源表（T112）—— 本文件每一段来自哪条轨，别让下一个人去猜

T64 交这个文件时，T61 / T62 / T63 的产出**不在它的基线里**（契约 C-R8：五轨互不
import），所以它自带了一层契约级 stub。T112 把三段 stub 全部换成真件，
下表是换完之后的出处。编排逻辑（DAG、闸、状态推进、权威事实守卫）从头到尾没换过。

### 一、业务对象层（C-R1 / C-R2 / C-R3）→ 来自 **T61**

| 本文件里的名字 | 真出处 |
| :-- | :-- |
| `ensure_schema` | `maos/domain/rtv/objects.py::ensure_schema` + ap 域那五张源单表 |
| `_stub_query` / `_stub_execute` | `objects.query` / `objects.execute`（保留旧名当别名） |
| `BIZ_STATUS_FLOW` / `INITIAL_STATUS` | `maos/domain/rtv/guard.py` 同名常量 |
| `AUTHORITATIVE_*` / `VIOLATION_EVENT` / `DOMAIN` | `guard.py` 同名常量 |
| `_guard_create_case` / `_guard_update_biz_status` | `guard.create_case` / `guard.update_biz_status` |
| `seed_case_inputs()` 那份源单数据 | `maos/domain/rtv/fixtures.py::seed_source_documents` |

### 二、ToolPort（C-R5 五个，一个不落）→ 来自 **T62**

| 工具名（注册表主键） | 真出处 |
| :-- | :-- |
| `supplier.rma_submit` | `maos/tools/rtv.py::SUPPLIER_RMA_SUBMIT_PORT`（写） |
| `supplier.credit_query` | `SUPPLIER_CREDIT_QUERY_PORT`（只读） |
| `carrier.ship` | `CARRIER_SHIP_PORT`（写） |
| `carrier.track` | `CARRIER_TRACK_PORT`（只读） |
| `ap.adjust_query` | `AP_ADJUST_QUERY_PORT`（**只读** —— RTV 域不许写 AP） |

三个外部系统的演示实现是 `MockSupplier` / `MockCarrier` / `MockApSystem`；
码表 `RETURN_REASONS` / `RULES` / 单据类型码在 `maos/tools/rtv_codes.py`。
实例经 `RtvToolBinding` 绑进五个 port，再按 C-R5 的名字进 `extras["tools"]`
（登记处见 `maos/agents/rtv/_base.py::register_tools`）。

### 三、Skill（C-R4 六个，一个不落）→ 来自 **T63**

| 契约名（注册表主键） | 真出处 |
| :-- | :-- |
| `rtv.intake` v1.0.0 | `maos/skills/builtin/rtv/intake.py` |
| `rtv.dispose` v1.0.0 | `maos/skills/builtin/rtv/dispose.py` |
| `rtv.ship` v1.0.0 | `maos/skills/builtin/rtv/ship.py` |
| `rtv.reconcile` v1.0.0 | `maos/skills/builtin/rtv/reconcile.py` |
| `rtv.observe` v1.0.0 | `maos/skills/builtin/rtv/observe.py` |
| `rtv.compensate` v1.0.0 | `maos/skills/builtin/rtv/compensate.py` |

六个都是 `builtin/rtv/__init__.py` 投放即注册进 `SKILL_REGISTRY` 的，
本文件一个 `@register_skill` 都没有 —— T64 那六个 stub 类整段删掉了。

### 四、本来就是真的那部分（一行都没换）

`maos/agents/rtv/**` 五个薄壳 Agent、`build()` 六元组、`run_until_settled` 驱动
循环、`ReviewerGate` 七道闸、`HumanApprovalQueue`、`ControlPlane` 的状态机与
补偿顺序 —— 全是生产件。**本场景要证明的正是它们一行都不用改。**
===============================================================================
"""

from __future__ import annotations

import json
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
from maos.agents.rtv._base import register_tools, reset_tools
from maos.agents.rtv.settlement_agent import SKILL_COMPENSATE
from maos.contracts.events import new_id
from maos.contracts.states import TASK_TRANSITIONS, PlanState, TaskState
from maos.domain.ap import objects as ap_objects
from maos.domain.rtv import fixtures as rtv_fixtures
from maos.domain.rtv import guard as rtv_guard
from maos.domain.rtv import objects as rtv_objects
from maos.flows.common import build, dump, run_until_settled
from maos.model.client import select_model_client
from maos.runtime.gate import HumanApprovalQueue
from maos.skills.builtin.rtv import RTV_SKILLS
from maos.skills.builtin.rtv import _common as rtv_common
from maos.skills.contract import Skill
from maos.skills.invoker import SkillInvoker
from maos.skills.registry import SKILL_REGISTRY
from maos.tools import rtv as rtv_tools
from maos.tools import rtv_codes

# =========================================================================
# 一、业务对象层 —— 全部转发到 T61 的 domain/rtv，旧名保留为别名
# =========================================================================
# 为什么保留旧名而不是让调用方改过去：`maos/tests/test_rtv_flow.py` 有约五十处
# 按旧名下的断言，而它们钉的是**行为**（越权写入抛什么、库里落了几行），
# 不是名字。改名字等于把五十条用例一起动一遍，而那正是最容易把断言改松的时机。

AUTHORITATIVE_WRITER = rtv_guard.AUTHORITATIVE_WRITER
AUTHORITATIVE_STATES = rtv_guard.AUTHORITATIVE_STATES
AUTHORITATIVE_RECEIPT_STATE = rtv_guard.AUTHORITATIVE_RECEIPT_STATE
BIZ_STATUS_FLOW = rtv_guard.BIZ_STATUS_FLOW
INITIAL_STATUS = rtv_guard.INITIAL_STATUS
VIOLATION_EVENT = rtv_guard.VIOLATION_EVENT
BIZ_STATUS_EVENT = rtv_guard.BIZ_STATUS_EVENT
ADVERSE_EVENT = rtv_common.ADVERSE_EVENT
DOMAIN = rtv_guard.DOMAIN

AuthoritativeFactViolation = rtv_guard.AuthoritativeFactViolation

#: 码表来自 T63 的 skill 侧（六个 skill 的 findings / rationale 都挂这里的编号）。
#: T62 的 `rtv_codes` 另有一份**出处更硬**的表（`RTV-RSN-0x` / `RTV-DISP-0x`），
#: 两份并存这件事记在 docs/BACKLOG.md —— 本轨不合并，合并要动两侧的校验。
RULES = rtv_common.RULES
RETURN_REASONS = rtv_common.RETURN_REASONS

#: 三个外部系统的状态取值域，唯一出处是 T62 的 `rtv_codes`。
SUPPLIER_ACKNOWLEDGED = rtv_codes.SUPPLIER_ACKNOWLEDGED
SUPPLIER_ISSUED = rtv_codes.SUPPLIER_ISSUED
SUPPLIER_DISPUTED = rtv_codes.SUPPLIER_DISPUTED
SUPPLIER_UNKNOWN = rtv_codes.SUPPLIER_UNKNOWN
AP_NONE = rtv_codes.AP_NONE
AP_SETTLED = rtv_codes.AP_SETTLED
CARRIER_DELIVERED = rtv_codes.CARRIER_DELIVERED
DOC_TYPE_CREDIT_NOTE = rtv_codes.CODE_CREDIT_NOTE

#: 一次观察都没做成时的占位。**不取外部系统的任何终态值** —— 「我问累了」和
#: 「供应商说不认」是两回事，混起来会让一笔实际已开票的退货在账上变成没退成。
UNOBSERVED = "unobserved"

#: C-R5 的五个 ToolPort 名，从 T62 的 `RTV_PORTS` 现取，不抄字面量。
STUB_TOOL_NAMES = tuple(port.name for port in rtv_tools.RTV_PORTS)

#: C-R4 的六个 skill 名，从 T63 的包清单现取。
STUB_SKILL_NAMES = tuple(RTV_SKILLS)

#: 三个外部系统的演示实现，按 T62 的名字再导出一次（测试按名取，不抄字面量）。
MockSupplier = rtv_tools.MockSupplier
MockCarrier = rtv_tools.MockCarrier
MockApSystem = rtv_tools.MockApSystem
#: 🔴 旧名 `StubApSystem` 保留为别名：测试拿它钉「RTV 域不许写 AP」——
#: 这个类上只有 `query`，没有任何写方法（契约 C-R5 那条红字）。
StubApSystem = rtv_tools.MockApSystem


def _stub_query(store: Any, sql: str, params: tuple | list = ()) -> list[dict]:
    """只读查询。别名到 `objects.query`。"""
    return rtv_objects.query(store, sql, params)


def _stub_execute(store: Any, sql: str, params: tuple | list = ()) -> None:
    """写入。别名到 `objects.execute` —— 三张受保护表在那一层就被拒了。"""
    rtv_objects.execute(store, sql, params)


def ensure_schema(store: Any) -> None:
    """建表：ap 域五张源单表 + rtv 域自己那 10 张。

    🔴 **两段都要**。`rtv_objects.ensure_schema` 刻意只建本域 10 张
    （见 `objects.require_upstream_tables` 的 docstring）：supplier /
    purchase_order / purchase_order_line / goods_receipt / goods_receipt_line
    归应付账款域持有，本域只引用不重建 —— `CREATE TABLE IF NOT EXISTS` 撞名的后果
    不是报错而是**静默跳过**，重建一份「看起来一样」的定义，两处一旦漂开，
    症状会离原因非常远。所以这里调的是持有方的 `ensure_schema`。
    """
    ap_objects.ensure_schema(store)
    rtv_objects.ensure_schema(store)


def get_case(store: Any, tenant_id: str, case_id: str) -> dict:
    """取一个案子。**不存在就抛** —— 口径同 T64 那版。

    `objects.get_case` 返回 `None`，而调用点（场景的打印与测试的断言）几乎全是
    `get_case(...)["biz_status"]` 这种形状：让它返回 None，症状是一句
    `TypeError: 'NoneType' object is not subscriptable`，指不出是哪个案子没建成。
    """
    case = rtv_objects.get_case(store, tenant_id, case_id)
    if case is None:
        raise LookupError(f"退货案 {case_id} 不存在（tenant={tenant_id}）")
    return case


def _guard_create_case(store: Any, *, tenant_id: str, case_id: str, supplier_id: str,
                       po_id: str, po_version: int, gr_id: str, amount_claimed: str,
                       currency: str, plan_id: str,
                       actor_skill: str = "rtv.intake",
                       invocation_id: str = "seed") -> dict:
    """建案。别名到 `guard.create_case`。

    多出来的两个参数是守卫要的 actor 锚点，给了缺省值是为了让**造探针数据**的
    调用点不必每次现编一个 —— 真正的受理走 `rtv.intake`，那条路上锚点由
    `SkillInvoker` 生成，压根不经过这里。
    """
    return rtv_guard.create_case(
        store, tenant_id=tenant_id, case_id=case_id, supplier_id=supplier_id,
        po_id=po_id, po_version=po_version, gr_id=gr_id,
        amount_claimed=amount_claimed, currency=currency, plan_id=plan_id,
        actor_skill=actor_skill, invocation_id=invocation_id)


#: 两个权威终态各自的最小凭据模板。`_guard_update_biz_status` 只拿到一个
#: `receipt_state`（旧签名），而 `guard.update_biz_status` 要一份**字段齐全**的凭据
#: （第 ③ 道：凭据号与金额是可对账的那部分）。所以这里按终态补齐其余字段。
#: 真正的观察走 `rtv.observe`，那条路上的凭据逐字来自外部回执，不经过这张模板。
_PROBE_OBSERVATION: dict[str, dict] = {
    "credited": {"credit_note_id": "cn-probe", "amount_credited": "100.00",
                 "document_type": DOC_TYPE_CREDIT_NOTE, "currency": "CNY"},
    "settled": {"adjustment_id": "apadj-probe", "ap_reference": "apref-probe"},
}


def _guard_update_biz_status(store: Any, *, tenant_id: str, case_id: str, to_status: str,
                             writer: str, receipt_state: str = "",
                             invocation_id: str = "", extras: dict | None = None,
                             return_action: str = "credit") -> dict:
    """本域**唯一**的业务状态写入口。别名到 `guard.update_biz_status`。

    两处签名差异，都在这一层抹平，守卫那边一个字没动：

      · 旧签名给的是 `receipt_state` 一个字符串，新守卫要一份**字段齐全**的凭据
        （`observation`）—— 光有「回执说了 issued」不够，还得有拿得去对账的单号与
        金额。这里按 `_PROBE_OBSERVATION` 补齐，`observed_state` 仍是调用方给的那个，
        所以第 ④ 道判据（`acknowledged` 换不来 `credited`）照样拦得住。
      · 旧签名不传 `return_action`，新守卫要求进 `disposed` 时必须同事务给出裁定结果
        （`DispositionRequired`）—— 给个缺省值，探针数据推 `disposed` 才走得动。

    `extras` 只剩兼容作用：事件的 plan_id 现在由守卫自己从案子上读，
    不再靠调用方递一份可能对不上的。
    """
    observation = None
    if to_status in AUTHORITATIVE_STATES and receipt_state:
        observation = dict(_PROBE_OBSERVATION.get(to_status, {}))
        observation["observed_state"] = receipt_state
    return rtv_guard.update_biz_status(
        store, tenant_id, case_id, to_status, writer, invocation_id,
        observation=observation,
        return_action=(return_action if to_status == "disposed" else ""),
        reason=f"{writer} 推进业务状态")


def stub_skill_classes() -> dict[str, type[Skill]]:
    """本域六个 skill 的类，name -> 类。**取的是注册表里的真实现**。

    T64 那版是本文件自己注册的六个 stub；整合后本文件一个 skill 都不注册，
    这个函数因此改成从 `SKILL_REGISTRY` 现取 —— 名字与版本是注册表主键，
    真实现与当初的 stub 逐字对齐契约 C-R4，所以调用点零改动。
    """
    return {name: SKILL_REGISTRY[name]["1.0.0"] for name in STUB_SKILL_NAMES}


# =========================================================================
# 二、场景常量 —— 全部写死，两次跑出来的结论逐条一致
# =========================================================================
TENANT_ID = "tnt-mfg-rtv"
SUPPLIER_ID = "SUP-8802"
SUPPLIER_NAME = "华南精密铸造"

#: 顺利路径的源单与案子。
CASE_OK = "case-rtv-0001"
PO_OK = "PO-2026-0715"
GR_OK = "GR-2026-0808"

#: 失败路径**另起一套单号**：顺利路径那批货已经贷记结清了，
#: 在它上面再退一次不是演示，是在演一个不该发生的动作。
CASE_BAD = "case-rtv-0002"
PO_BAD = "PO-2026-0722"
GR_BAD = "GR-2026-0815"

#: 两套外部系统按名登记（task.inputs 会被 json.dumps，实例塞不进去）。
#: 两条路径各一套，不共用 —— 共用会让失败路径那份 disputed 脚本把顺利路径的
#: 轮询计数一并推着走。
TOOLS_OK = "s12-tools"
TOOLS_BAD = "s12-tools-disputed"

CARRIER_OK = "s12-express"
CARRIER_BAD = "s12-express-b"

# ---- 顺利路径的轮询语义（按 T62 的 mock 重对过，见 docs/DECISIONS.md）--------
#: 供应商：问到第 3 次才开票。前两次回 `acknowledged`（收到货了、没做会计确认），
#: 那一段正是权威闸要拦的地方 —— `MockSupplier` 的构造器强制
#: `issue_after > ack_after`，两者相等就等于把两个状态合并成了一个值。
ACK_AFTER_OK = 1
ISSUE_AFTER_OK = 3
#: AP：问到第 3 次才核销，走完 staged -> built -> settled 三段
#: （`MockApSystem` 强制 `settle_after >= 2`）。
SETTLE_AFTER_OK = 3
#: 承运商：问到第 2 次才签收。
DELIVER_AFTER_OK = 2

#: 各步的期望轮询次数 —— 「终态是问出来的」，一次 query 不够。
EXPECTED_POLLS_SHIP = 2      # carrier.track：in_transit -> delivered
EXPECTED_POLLS_CREDIT = 3    # supplier.credit_query：ack, ack -> issued
EXPECTED_POLLS_SETTLE = 3    # ap.adjust_query：staged, built -> settled
MAX_POLLS_OK = 5

# ---- 失败路径 --------------------------------------------------------------
#: 供应商第 2 次就给出终态，而那个终态是 `disputed` —— 供应商不认这笔退货。
ISSUE_AFTER_BAD = 2
#: 轮询上限 3，保证**一定**拿不到贷项通知单（第 2 次就是终态，到不了第 3 次）。
MAX_POLLS_BAD = 3
EXPECTED_POLLS_BAD = 2

#: 退货理由码。取值域是 T63 skill 侧那份（`_common.RETURN_REASONS`）；
#: 递到供应商门户时由 `RtvToolBinding` 翻成 T62 `rtv_codes` 的已核对编号。
REASON_OK = "defective"
REASON_BAD = "defective"

#: 源单：(收货行号, sku, 订购数, 单价, 到货数, 验收不合格数)。
#: **退货量单独记一处，不改 goods_receipt_line**：PeopleSoft 口径里
#: `Quantity Received` 是审计量、永不变（C-R1 的 rtv_line 注释）。
#: 不合格数就是可退来源，而 `rtv.intake` 另有一条判据：退货量不得超过**合格数**
#: （received - rejected），所以到货数要给够。
SOURCE_LINES_OK = [
    (1, "SKU-CAST-A1", 24, "260.00", 24, 12),
    (2, "SKU-CAST-B7", 8, "185.00", 8, 4),
]
#: 我方算出来的合计：12×260 + 4×185 = 3860.00。由 fixtures 现算，不手写进断言。
AMOUNT_CLAIMED_OK = "3860.00"
#: 🔴 供应商**认的**金额刻意与我方自称的差 0.50 元。
#: 两处金额都留着、不合并成一处，正是 C-R1 那条注释要的东西：
#: 「退货方自称的应退金额」与「供应商贷项通知单认的金额」是两个事实，
#: 对不上正是本域要拦的事。
AMOUNT_CREDITED_OK = "3859.50"
#: 对账容差：合同约定 1.00 元以内算对上。**必须显式给** —— `rtv.reconcile` 的缺省
#: 容差是最严的一档（0.01），放宽是要有出处的决定，不是默认值该替人做的事。
TOLERANCE_OK = {"absolute": "1.00"}

SOURCE_LINES_BAD = [
    (1, "SKU-CAST-C3", 12, "410.00", 12, 6),
]
AMOUNT_CLAIMED_BAD = "2460.00"

TASK_INTAKE = "task-s12-intake"
TASK_DISPOSE = "task-s12-dispose"
TASK_SHIP = "task-s12-ship"
TASK_OBSERVE = "task-s12-observe-credit"
TASK_OBSERVE_2 = "task-s12-observe-settle"
TASK_RECONCILE = "task-s12-reconcile"

TASK_INTAKE_B = "task-s12b-intake"
TASK_DISPOSE_B = "task-s12b-dispose"
TASK_SHIP_B = "task-s12b-ship"
TASK_OBSERVE_B = "task-s12b-observe-credit"
TASK_OBSERVE_2_B = "task-s12b-observe-settle"
TASK_RECONCILE_B = "task-s12b-reconcile"

APPROVER = "@boss-rtv:maos.local"
SHIP_APPROVE_REASON = f"处置已裁定且依据 {sorted(RULES)[0]}，同意发运退货"
OBSERVE_APPROVE_REASON = "贷项通知单与 AP 调整凭单都已取得，确认收口"
REJECT_REASON = ("供应商不认这笔退货，转人工与供应商交涉：这批货**已经发出去了**，"
                 "**不要**据此断定这批货或这笔钱作废")

GOAL_OK = "退回华南精密铸造 2026-08 到货不合格件：裁定 credit 后发运、取两方终态回执、对账"
GOAL_BAD = "退回华南精密铸造错发件：货已发出，但供应商不认这笔退货"

#: 本域不出方案，DAG 直接交给 `create_plan`。ScriptedModelClient 仍要给一份脚本：
#: Reviewer 的语义审查会问模型。
REVIEW_JSON = json.dumps({
    "defects": [],
    "conclusion": ("退货处置依据本域规则表，可退金额与供应商贷项通知单认的金额两处都留着；"
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
def seed_case_inputs(store: Any, *, po_id: str, gr_id: str, source_lines: list,
                     reason_code: str) -> dict:
    """把一个案子的源单落进**复用的 ap 域五张表**，返回受理入参。

    T64 那版是把源单信息直接塞进 task.inputs 的（本域当时建不了那五张表）。
    整合后走 `fixtures.seed_source_documents`：PO / 收货单真的在库里，
    `rtv.intake` 因此要**自己去定位**它们 —— 「退的货指不指得回去」这条判据
    从此是真判的，而不是靠调用方递一份自称的源单号。

    退货行由收货行的 `quantity_rejected` 算出来（验收不合格的那部分才该退），
    `amount_claimed` 也由它算 —— 不从这里手写一个数递进去。
    """
    rtv_fixtures.seed_supplier(
        store, tenant_id=TENANT_ID, supplier_id=SUPPLIER_ID, name=SUPPLIER_NAME,
        payment_means_code="30", payment_terms="NET30", bank_account="****8802")
    seeded = rtv_fixtures.seed_source_documents(
        store, tenant_id=TENANT_ID, supplier_id=SUPPLIER_ID, po_id=po_id, gr_id=gr_id,
        lines=source_lines, ordered_at="2026-07-15")
    return {
        "po_id": po_id, "po_version": 1, "gr_id": gr_id, "currency": "CNY",
        "amount_claimed": seeded["amount_claimed"],
        "lines": [dict(line, reason_code=reason_code) for line in seeded["lines"]],
    }


def _bind_tools(store: Any, name: str, *, supplier: Any, carrier: Any,
                ap_system: Any) -> None:
    """把三个外部系统实例绑进 C-R5 的五个 port，按名登记给 Agent 取。

    登记而不是塞进 payload：`task.inputs` 会被 `json.dumps`（`dump()` 与证据束都要
    序列化它），塞一个绑好实例的可调用进去当场就炸。Agent 侧按
    `ctx.inputs["tools_binding"]` 取，所以 Agent 仍然**不 import 本模块**。
    """
    binding = rtv_tools.RtvToolBinding(
        store, supplier=supplier, carrier=carrier, ap_system=ap_system,
        destination=f"{SUPPLIER_NAME}退货收货仓")
    register_tools(name, binding.as_tools())


def _tasks(*, case_id: str, seed: dict, carrier: str, tools: str, max_polls: int,
           ids: tuple[str, str, str, str, str, str],
           tolerance: dict | None = None) -> list[dict]:
    """一条路径的六任务 DAG。两条路径同构，只换案子与那套外部系统。

    依赖链照 SOP 五步（契约 C-R2 的映射表），第 ⑤ 步观察拆成两跳：
    `rtv_intake -> rtv_disposition -> rtv_logistics -> rtv_settlement ×2 -> rtv_reconcile`
    —— 为什么是六个节点、为什么对账排最后，见模块 docstring 那两节。

    `biz_type` 用本域自己的标记（`"rtv"`），**不是 `"refund"`** —— 第六道财务复核闸
    按 `biz_type == "refund"` 触发，冒用会让闸恒 blocker，而报错信息指向退款域的
    财务复核，离原因极远（口径同 `flows/scenario_10.py::_tasks`）。
    """
    t_intake, t_dispose, t_ship, t_observe, t_observe2, t_recon = ids
    base = {"biz_type": DOMAIN, "tenant_id": TENANT_ID, "case_id": case_id,
            "tools_binding": tools}
    return [
        {"task_id": t_intake, "role": ROLE_INTAKE,
         "title": "受理退货诉求并定位源 PO 与收货单",
         "inputs": {**base, **seed},
         "acceptance": ["建出 rtv_case 且 biz_status=received",
                        "return_action 留空 —— 受理不替裁定拍板",
                        "每条退货行都指得到一条真实收货行，且不超过合格数"],
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
         "inputs": {**base, "carrier": carrier, "max_polls": max_polls},
         "acceptance": ["shipped 只能由承运商回执 delivered 得到", "运单号入库"],
         "depends_on": [t_dispose], "risk_level": "M", "effect_risk": "H"},

        # 观察第 1 跳。effect_risk=H：确认收口是不可逆动作，Gate 过了也要人放行。
        # 失败路径的转折点就在这里 —— 主管拿到的不是「已贷记」，是一份供应商
        # 明确不认的回执，于是他驳回。
        {"task_id": t_observe, "role": ROLE_SETTLEMENT,
         "title": "向外部权威问一次终态回执（第 1 跳）",
         "inputs": {**base, "max_polls": max_polls},
         "acceptance": ["问哪个外部系统由案子当前业务状态决定，不由任务自己挑",
                        "credited 只能由供应商回执 issued 得到；acknowledged 不算"],
         "depends_on": [t_ship], "risk_level": "M", "effect_risk": "H"},

        # 观察第 2 跳。**同一个 skill、同一个角色**，问的是另一个外部权威 ——
        # 前提是第 1 跳真的把案子推到了 credited；没推到就仍问供应商侧。
        {"task_id": t_observe2, "role": ROLE_SETTLEMENT,
         "title": "向外部权威问一次终态回执（第 2 跳）",
         "inputs": {**base, "max_polls": max_polls},
         "acceptance": ["settled 只能由 AP 调整凭单核销得到",
                        "第 1 跳没推进时本跳仍问供应商侧 —— 一个权威不许推定另一个"],
         "depends_on": [t_observe], "risk_level": "M", "effect_risk": "H"},

        # 对账收尾：三条腿都齐了才勾稽得上。见模块 docstring「为什么对账排在观察之后」。
        {"task_id": t_recon, "role": ROLE_RECONCILE,
         "title": "退货行 × 贷项通知单 × 到账三方对账",
         "inputs": {**base, "tolerance": dict(tolerance or TOLERANCE_OK)},
         "acceptance": ["三条腿齐备才算对上", "对不上的理由必须挂规则编号",
                        "我方算的金额与供应商认的金额两处都要留着"],
         "depends_on": [t_observe2], "risk_level": "M"},
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
               operator: str, reason: str, observed_state: str) -> dict:
    """编排层以观察岗的 identity 调 `rtv.compensate`。

    走 SkillInvoker 而不是直接 `RtvCompensateSkill().run()`：白名单校验与
    SkillInvoked 审计行都在 invoker 里，直接调就没有审计行，出事之后查不到是谁做的。

    `resubmit=True`：这一档要在供应商门户上**留一条痕**，让对方那边也看得到我方
    还在追这笔退货。`rtv.compensate` 对 `observed_state="unknown"` 一律拒绝补提
    （那笔申请可能已经受理，重发会造出第二笔）—— 本场景递的是 `disputed`，
    供应商把话说清楚了，补提是去交涉，不是去碰运气。
    """
    invoker = SkillInvoker(COMPENSATION_IDENTITY, store)
    res = invoker.invoke(SKILL_COMPENSATE, {
        "tenant_id": TENANT_ID, "case_id": case_id, "reason": reason,
        "observed_state": observed_state, "resubmit": True,
        "detail": {"operator": operator, "channel": "人工交涉工单"},
    }, extras={"plan_id": plan_id, "task_id": task_id, "trace_id": trace_id,
               "tools": _tools_for_compensation(case_id)})
    if res.status != "ok" or not isinstance(res.output, dict):
        raise RuntimeError(f"域内补偿失败，不许静默收口：{res.error}")
    return res.output


#: 补偿是编排层发起的，不经 Agent，所以 `extras["tools"]` 得由这里递。
#: 用哪套由案子决定 —— 两条路径各一套外部系统，递错那套等于问了另一个供应商。
_COMPENSATION_TOOLS: dict[str, str] = {}


def _tools_for_compensation(case_id: str) -> dict:
    from maos.agents.rtv._base import tools_of

    return tools_of(_COMPENSATION_TOOLS[case_id])


def _agent_status(store, plan_id: str) -> dict:
    """六个 Agent 各自的**自述结论** —— 取自控制面的状态迁移，不从任务终态推。

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
    print("场景 12（顺利路径）：采购退货退款 —— 供应商开了票、AP 核销了，才算退成")

    model = select_model_client(SCRIPT, force_scripted=True)
    store, bus, cp, model, worker, gate = build(SCRIPT, matrix=matrix, model=model)
    ensure_schema(store)

    reset_tools()
    _bind_tools(
        store, TOOLS_OK,
        supplier=rtv_tools.MockSupplier(
            ack_after=ACK_AFTER_OK, issue_after=ISSUE_AFTER_OK,
            credit_amounts={CASE_OK: AMOUNT_CREDITED_OK}),
        carrier=rtv_tools.MockCarrier(deliver_after=DELIVER_AFTER_OK),
        ap_system=rtv_tools.MockApSystem(
            settle_after=SETTLE_AFTER_OK,
            ledger={CASE_OK: {"amount": AMOUNT_CREDITED_OK, "currency": "CNY",
                              "adjustment_id": f"apadj-{CASE_OK}"}}))
    _COMPENSATION_TOOLS[CASE_OK] = TOOLS_OK

    trace_id, plan_id = new_id("trace"), new_id("plan")
    seed = seed_case_inputs(store, po_id=PO_OK, gr_id=GR_OK,
                            source_lines=SOURCE_LINES_OK, reason_code=REASON_OK)
    cp.create_plan(goal=GOAL_OK, trace_id=trace_id, plan_id=plan_id,
                   tasks=_tasks(case_id=CASE_OK, seed=seed, carrier=CARRIER_OK,
                                tools=TOOLS_OK, max_polls=MAX_POLLS_OK,
                                ids=(TASK_INTAKE, TASK_DISPOSE, TASK_SHIP,
                                     TASK_OBSERVE, TASK_OBSERVE_2, TASK_RECONCILE)))
    cp.start_plan(plan_id)
    run_until_settled(bus, gate, cp, plan_id)

    intake = artifact_of(store, TASK_INTAKE, KIND_RTV_INTAKE)
    dispo = artifact_of(store, TASK_DISPOSE, KIND_RTV_DISPOSITION)
    src = intake["source"]
    print(f"\n[1] 受理    : {intake['case']['case_id']}（源单 "
          f"{src['po_id']} v{src['po_version']} / {src['gr_id']}，"
          f"收货 {src['gr_lines']} 行里退 {src['rtv_lines']} 行），"
          f"自称应退 {intake['amount_claimed']}")
    print(f"    业务引用: {[r['object_table'] for r in intake['refs']]} —— "
          f"「退的是哪一张 PO 的哪一次收货」指得回去")
    print(f"\n[2] 处置裁定: {dispo['return_action']}"
          f"（依据 {sorted({r['rule_id'] for r in dispo['rationale']})}）")

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
    print(f"\n[4] 退货发运: {ship['shipment']['shipment_id']}，运单 "
          f"{ship['shipment']['tracking_no']}，承运商回执 {ship['carrier_status']}"
          f"（问了 {ship['poll_count']} 次）—— 状态来自回执，不是本地推断")

    # —— 人工介入其二：观察第 1 跳，供应商认不认这笔退货 ——
    pending = hq.pending(plan_id)
    assert [t["task_id"] for t in pending] == [TASK_OBSERVE], (
        f"应停在第 1 跳观察的人工审批上，实际 {[t['task_id'] for t in pending]}")
    credit = artifact_of(store, TASK_OBSERVE, KIND_RTV_SETTLEMENT_ADVICE)
    print(f"\n[5] 终态回执①: 问 {credit['system']} 侧 {credit['poll_count']} 次，"
          f"对方回 {credit['observed_state']}（贷项通知单 {credit['reference']}，"
          f"类型 {credit['advice'].get('document_type')}，认的金额 "
          f"{credit['advice'].get('amount_credited')}）；biz_status="
          f"{credit['biz_status']}")
    print(f"    —— 前 {ISSUE_AFTER_OK - 1} 次回的都是 {SUPPLIER_ACKNOWLEDGED}："
          f"收到退货了，还没做会计确认。acknowledged 换不来 credited")
    hq.decide(TASK_OBSERVE, approved=True, operator=APPROVER, note=OBSERVE_APPROVE_REASON)
    run_until_settled(bus, gate, cp, plan_id)

    # —— 人工介入其三：观察第 2 跳，钱到没到账 ——
    pending = hq.pending(plan_id)
    assert [t["task_id"] for t in pending] == [TASK_OBSERVE_2], (
        f"应停在第 2 跳观察的人工审批上，实际 {[t['task_id'] for t in pending]}")
    advice = artifact_of(store, TASK_OBSERVE_2, KIND_RTV_SETTLEMENT_ADVICE)
    print(f"\n[6] 终态回执②: 问 {advice['system']} 侧 {advice['poll_count']} 次，"
          f"对方回 {advice['observed_state']}（调整凭单 "
          f"{advice['advice'].get('adjustment_id')}，核销流水 {advice['reference']}）；"
          f"biz_status={advice['biz_status']}")
    print(f"    —— 两个权威源分开问、分开写：开票是供应商的会计动作，"
          f"到账是 AP 的动作，一个都不许由另一个推定")
    hq.decide(TASK_OBSERVE_2, approved=True, operator=APPROVER,
              note=OBSERVE_APPROVE_REASON)
    run_until_settled(bus, gate, cp, plan_id)

    recon = artifact_of(store, TASK_RECONCILE, KIND_RTV_RECONCILIATION)
    print(f"\n[7] 三方对账: reconciled={recon['reconciled']}，findings "
          f"{[f['rule_id'] for f in recon['findings']]}")
    print(f"    我方按退货行算 {recon['amount_claimed']}，"
          f"供应商贷项通知单**认的** {recon['amount_credited']}，"
          f"差 0.50 在容差 {TOLERANCE_OK['absolute']} 内 —— 两个数都留着，不合并")
    print(f"    —— 对账问的是同一个 supplier.credit_query，拿的是同一份回执，"
          f"可它写不进 credited：那归 {AUTHORITATIVE_WRITER}")

    dump(cp, plan_id, "场景 12 顺利路径：采购退货退款五步 SOP")
    return {"store": store, "cp": cp, "bus": bus, "gate": gate, "hq": hq,
            "plan_id": plan_id, "trace_id": trace_id, "intake": intake,
            "disposition": dispo, "shipment": ship, "credit": credit,
            "advice": advice, "reconciliation": recon,
            "agent_status": _agent_status(store, plan_id)}


# ------------------------------------------------------------------ 失败路径
def drive_failure(*, matrix: bool = False) -> dict:
    """跑完失败路径并返回收口用的句柄。**只跑不断言**。

    另起一套运行时（不复用顺利路径那个）：收口断言里有全库口径的
    「终态观察 0 条」，两条路径共库的话它校验的就不再是本条链路了。
    """
    print("\n场景 12（失败路径）：货已发出、跑到的 Agent 全回 ok —— "
          "而供应商不认这笔退货，系统如实记下来")

    model = select_model_client(SCRIPT, force_scripted=True)
    store, bus, cp, model, worker, gate = build(SCRIPT, matrix=matrix, model=model)
    ensure_schema(store)

    reset_tools()
    # 🔴 供应商到点之后给出的终态是 disputed：失败演的是「供应商不认」这件业务事实，
    # 不是「代码抛了异常」—— 后者是 bug，证明不了任何关于编排的事。
    # AP 侧 ledger 为空 -> 恒回 none：供应商都不认，AP 那边压根不会有调整凭单。
    _bind_tools(
        store, TOOLS_BAD,
        supplier=rtv_tools.MockSupplier(
            ack_after=ACK_AFTER_OK, issue_after=ISSUE_AFTER_BAD,
            script={CASE_BAD: SUPPLIER_DISPUTED}),
        carrier=rtv_tools.MockCarrier(deliver_after=1),
        ap_system=rtv_tools.MockApSystem(settle_after=SETTLE_AFTER_OK, ledger={}))
    _COMPENSATION_TOOLS[CASE_BAD] = TOOLS_BAD

    trace_id, plan_id = new_id("trace"), new_id("plan")
    seed = seed_case_inputs(store, po_id=PO_BAD, gr_id=GR_BAD,
                            source_lines=SOURCE_LINES_BAD, reason_code=REASON_BAD)
    cp.create_plan(goal=GOAL_BAD, trace_id=trace_id, plan_id=plan_id,
                   tasks=_tasks(case_id=CASE_BAD, seed=seed, carrier=CARRIER_BAD,
                                tools=TOOLS_BAD, max_polls=MAX_POLLS_BAD,
                                ids=(TASK_INTAKE_B, TASK_DISPOSE_B, TASK_SHIP_B,
                                     TASK_OBSERVE_B, TASK_OBSERVE_2_B,
                                     TASK_RECONCILE_B)))
    cp.start_plan(plan_id)
    run_until_settled(bus, gate, cp, plan_id)

    hq = HumanApprovalQueue(store, cp)
    pending = hq.pending(plan_id)
    assert [t["task_id"] for t in pending] == [TASK_SHIP_B], (
        f"应先停在发运的人工审批上，实际 {[t['task_id'] for t in pending]}")
    print(f"\n[1] 待主管审批: {pending[0]['title']} —— 处置已裁定，同意发运")
    hq.decide(TASK_SHIP_B, approved=True, operator=APPROVER, note=SHIP_APPROVE_REASON)
    run_until_settled(bus, gate, cp, plan_id)

    credit = artifact_of(store, TASK_OBSERVE_B, KIND_RTV_SETTLEMENT_ADVICE)
    print(f"\n[2] 终态回执①: 问 {credit['system']} 侧 {credit['poll_count']} 次，"
          f"对方回 {credit['observed_state']} —— {credit['message']}")
    print(f"    推进了吗: advanced={credit['advanced']}，"
          f"biz_status={credit['biz_status']} —— **一个字都没写**")
    print(f"    贷项通知单: {_count(store, 'SELECT COUNT(*) AS n FROM credit_note')} 条")
    print(f"    终态观察  : "
          f"{_count(store, 'SELECT COUNT(*) AS n FROM rtv_settlement_observation')} 条"
          f" —— 问不出来就一个字都不写")

    # 第 1 跳没推进，案子还在 shipped。主管**先放行**让第 2 跳再问一次 ——
    # 「我问到的是 disputed」与「这笔退货到此为止」之间隔着一次人的决定，
    # 而给对方第二次机会正是这次决定该有的样子。
    hq.decide(TASK_OBSERVE_B, approved=True, operator=APPROVER,
              note="供应商侧回了 disputed，先放行让第 2 跳再问一次，别急着收口")
    run_until_settled(bus, gate, cp, plan_id)
    advice = artifact_of(store, TASK_OBSERVE_2_B, KIND_RTV_SETTLEMENT_ADVICE)
    print(f"\n[3] 终态回执②: 仍问 {advice['system']} 侧（案子还在 "
          f"{advice['biz_status']}，轮不到 AP）—— 对方仍回 {advice['observed_state']}")
    print(f"    —— 「供应商没认」不许被推定成「那就去问 AP 吧」："
          f"问哪个外部权威由案子当前状态决定，一个不许推定另一个")

    agent_status = _agent_status(store, plan_id)
    print(f"\n[4] 跑过的 Agent 自述: {agent_status}")
    print(f"    全部 ok。而 rtv_case.biz_status = "
          f"{get_case(store, TENANT_ID, CASE_BAD)['biz_status']} —— "
          f"「Agent 说完成了」不等于业务成功了")

    # —— 收口点：第 2 跳仍是 disputed，主管这次驳回 ——
    # 🔴 收口只可能停在 `effect_risk=H` 的任务上（`HumanApprovalQueue.pending`）。
    # 对账那一步是**只读推断**，不是不可逆动作，给它挂 H 只为了让流程停下来，
    # 就是把闸的语义借去当流程控制用。所以链路在这里被人截停，
    # 对账任务**不会执行** —— 那也正是要演的：人在不可逆动作前截停之后，
    # 后面的步骤不该照跑。
    pending = hq.pending(plan_id)
    assert [t["task_id"] for t in pending] == [TASK_OBSERVE_2_B], (
        f"第 2 跳观察应停在 BLOCKED 等人处置，实际 {[t['task_id'] for t in pending]}")
    print(f"\n[5] 待主管处置: {pending[0]['title']} —— 问了两轮，供应商明确不认，"
          f"不能当成功放行")

    # 先补偿、再落 FAILED —— 与 control_plane.human_decision 同一个顺序与同一个理由：
    # 状态一旦落 FAILED，「那批货还在供应商那里」这件事就没人记得了。
    comp = compensate(store, plan_id=plan_id, task_id=TASK_OBSERVE_2_B,
                      trace_id=trace_id, case_id=CASE_BAD, operator=APPROVER,
                      reason=REJECT_REASON, observed_state=advice["observed_state"])
    print(f"\n[6] 域内补偿: 补提 RMA {comp['rma'].get('rma_id')}（第 {comp['seq']} 次补偿，"
          f"resubmitted={comp['resubmitted']}）")
    print(f"    最后观察到的供应商说法 = {advice['observed_state']}"
          f"（**是供应商自己说的**，不是我方替它下的结论）")
    print(f"    收口理由: {comp['record']['reason']}")

    hq.decide(TASK_OBSERVE_2_B, approved=False, operator=APPROVER, note=REJECT_REASON)
    bus.drain()
    recon_tasks = [t for t in cp.store.list_tasks(plan_id)
                   if t["task_id"] == TASK_RECONCILE_B]
    print(f"\n[7] 对账任务  : {recon_tasks[0]['state']} —— 人在这条链路上截停了，"
          f"后面的步骤不该照跑")

    dump(cp, plan_id, "场景 12 失败路径：供应商不认这笔退货")
    return {"store": store, "cp": cp, "plan_id": plan_id, "trace_id": trace_id,
            "credit": credit, "advice": advice, "compensation": comp,
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

    # 终态是**问出来的**：三段各要一次以上的轮询，且两个权威源分开问。
    assert out["shipment"]["poll_count"] == EXPECTED_POLLS_SHIP, (
        f"承运商应问 {EXPECTED_POLLS_SHIP} 次，实际 {out['shipment']['poll_count']}")
    assert out["credit"]["poll_count"] == EXPECTED_POLLS_CREDIT, (
        f"供应商侧应问 {EXPECTED_POLLS_CREDIT} 次，实际 {out['credit']['poll_count']}")
    assert out["advice"]["poll_count"] == EXPECTED_POLLS_SETTLE, (
        f"AP 侧应问 {EXPECTED_POLLS_SETTLE} 次，实际 {out['advice']['poll_count']}")
    assert out["credit"]["system"] == "supplier" and out["advice"]["system"] == "ap", (
        "两个权威终态必须分别问两个外部系统，不许由一个推定另一个")

    # 可退的钱有两个数：我方算的与供应商**认的**，两处都留着，不合并成一处。
    r = out["reconciliation"]
    assert r["reconciled"] is True, (
        f"三条腿都齐了就该对得上，实际 findings {[f['rule_id'] for f in r['findings']]}")
    assert r["findings"] == [], f"对上了就不该有 findings，实际 {r['findings']}"
    assert r["amount_claimed"] == AMOUNT_CLAIMED_OK, (
        f"我方按退货行算的应退金额应为 {AMOUNT_CLAIMED_OK}，实际 {r['amount_claimed']}")
    assert r["amount_credited"] == AMOUNT_CREDITED_OK, (
        f"供应商贷项通知单认的金额应为 {AMOUNT_CREDITED_OK}，实际 {r['amount_credited']}")
    assert r["amount_credited"] != r["amount_claimed"], (
        "两个数写成一样的话，「以外部权威为准」这句话在场景里就没有演出来")
    # 案子上记的仍是我方自称的那个数 —— 两处都留着。
    assert case["amount_claimed"] == AMOUNT_CLAIMED_OK

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
    adverse = [e for e in cp.store.list_event_log(plan_id)
               if e["event_type"] == ADVERSE_EVENT]
    plan = cp.store.get_plan(plan_id)
    comp = out["compensation"]

    print(f"\n  业务状态  : {case['biz_status']}（全程没有经过 credited / settled）")
    print(f"  贷项通知单: {_count(store, 'SELECT COUNT(*) AS n FROM credit_note')} 条")
    print(f"  终态观察  : "
          f"{_count(store, 'SELECT COUNT(*) AS n FROM rtv_settlement_observation')} 条")
    print(f"  补偿记录  : {len(comp_rows)} 行")
    print(f"  坏消息留痕: {len(adverse)} 条 {ADVERSE_EVENT}")
    print(f"  Plan 终态 : {plan['state']}（主管驳回，业务确实没成功）")
    print(f"  Agent 自述 : {out['agent_status']} —— 跑到的五个全 ok，而案子没成")

    # —— 本轨要买的第二件东西：Agent 全回 ok ≠ 业务成功 ——
    # 五个：链路在第 2 跳观察那里被人截停，对账任务压根没执行。
    statuses = out["agent_status"]
    assert len(statuses) == 5 and set(statuses.values()) == {"ok"}, (
        f"失败路径上跑到的五个 Agent 都应回 ok（这正是要演的），实际 {statuses}")
    assert TASK_RECONCILE_B not in statuses, (
        "人截停之后对账不该照跑 —— 它跑了说明截停没截住")

    # —— 失败的原因是**供应商不认**，不是异常 ——
    assert out["credit"]["observed_state"] == SUPPLIER_DISPUTED, (
        f"失败路径演的必须是「供应商不认」，实际回执 {out['credit']['observed_state']}")
    assert out["credit"]["advanced"] is False, "供应商不认时业务状态一个字都不该动"
    assert out["credit"]["poll_count"] == EXPECTED_POLLS_BAD, (
        f"供应商第 {ISSUE_AFTER_BAD} 次就给出终态，应恰好问 {EXPECTED_POLLS_BAD} 次，"
        f"实际 {out['credit']['poll_count']}")
    # 第 2 跳仍问供应商侧：一个权威不许推定另一个。
    assert out["advice"]["system"] == "supplier", (
        f"案子还没到 credited，第 2 跳不该去问 AP，实际问的是 {out['advice']['system']}")
    assert out["advice"]["observed_state"] == SUPPLIER_DISPUTED
    assert out["advice"]["advanced"] is False

    # —— 本场景存在的理由，第一断言 ——
    assert case["biz_status"] == "compensated", (
        f"补偿之后业务状态应为 compensated，实际 {case['biz_status']}")
    assert _count(store, "SELECT COUNT(*) AS n FROM credit_note") == 0, (
        "供应商没开票就一张贷项通知单都不该有 —— 有就说明有人自己给自己开了发票")
    assert _count(store, "SELECT COUNT(*) AS n FROM rtv_settlement_observation") == 0, (
        "轮询到顶没拿到终态时一条观察都不该写 —— 「我问累了」不是可以落库的结论")
    # 但**不许不留痕**：「供应商说不认」这件事必须查得到。
    assert adverse, f"供应商明确不认必须落 {ADVERSE_EVENT}，否则这件事只活在日志里"
    assert all(e["detail"]["domain"] == DOMAIN for e in adverse)

    # —— 补偿真发生过：记录、留痕、语义三样都在 ——
    assert len(comp_rows) == 1, f"一次补偿收口留一行记录，实际 {len(comp_rows)} 行"
    assert comp["resubmitted"] is True and comp["rma"].get("rma_id"), (
        "补偿必须在供应商门户上留下补提 RMA 的痕迹")
    detail = json.loads(comp_rows[0]["detail_json"])
    assert detail["observed_state"] == SUPPLIER_DISPUTED, (
        f"供应商明确说了不认，补偿记录就该如实记 {SUPPLIER_DISPUTED}，"
        f"实际 {detail['observed_state']}")
    assert detail["observed_state"] != UNOBSERVED, (
        "供应商说了话，就不该记成「一次都没观察到」")
    assert "不要" in comp_rows[0]["reason"], (
        "补偿理由必须明说不许凭它断定货或钱作废 —— 走到补偿的案子里那批货已经发出去了")

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
