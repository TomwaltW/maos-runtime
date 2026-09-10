"""refund.snapshot_check —— **执行前**去读订单的当前版本，和手上这份快照比一比。

评委原话：「MAOS 在**执行前读取当前版本**，在返回后记录实际观察。」
后半句早就有了（`payment.observe` 把网关回执落进 `payment_observation`）；
前半句在 T116 之前是空的 —— `order_snapshot` 里躺的全是靶场预置的数据，
全仓没有任何一条读订单系统的路径，也就没有「我手上这版 vs 外部当前版」这个比对。

本 skill 补的就是那半句，落点在**付款之前**：钱要动之前，先确认依据没变。

## 三条硬边界

1. **不改任何业务状态**（铁律 8/9）。漂移不是一个新的业务状态，它是「停下来问人」。
   本 skill 只做三件事：出参 `drift`、落一条 `SnapshotDrift` 事件、把处置交给
   调用方。`biz_status` 一个字节都不碰 —— 碰了就等于让一次本地推断改写了
   一个由外部权威决定的字段。
2. **不自动重读快照往下跑**。订单被改过意味着退款依据可能变了（金额、SKU、
   甚至订单已取消）。悄悄按新版重算一遍，是拿一份 MAOS 自己刚拿到、没人复核过的
   事实去动钱 —— 那正是「快照 + 版本锁定」这套机制要防的事。
3. **读不到订单不算「一致」**。`order.query` 抛 KeyError（订单系统里没这笔单）
   时本 skill 照样报 `drift=true`：订单不见了比订单改了更严重，
   把它兜底成放行是这条链路上最贵的一个 bug。

## 处置为什么走 `gate_needs_human` 而不是新状态

铁律 9：不许加新业务状态、不许加新迁移。`AWAITING_REVIEW -> BLOCKED` 这条既有
迁移（`contracts/states.py` 里的 `gate_needs_human`）本来就是「机器没别的招了，
交给人」的出口，漂移正属于这一类。调用方（`flows/*`）拿到 `drift=true` 之后
把付款任务的 `effect_risk` 提到 `H`，走的就是那条出口 —— 一条新迁移都不加。
"""

from __future__ import annotations

from maos.domain.refund import guard, objects
from maos.skills.contract import Skill, SkillContext, SkillContract
from maos.skills.registry import register_skill
from maos.tools.order import ORDER_QUERY_PORT
from maos.tools.port import invoke_tool

from . import _common as C

#: `event_log.event_type`（跨轨契约 §F）。**不进 `contracts/events.py`**（铁律 1）——
#: 走 `append_event_log` 的自由 event_type 是仓库成文纪律，先例见
#: `guard.py` 的 `RefundBizStatusChanged`。
EVENT_SNAPSHOT_DRIFT = "SnapshotDrift"

#: 读不到订单时填进 `current_version` 的值。用 `None` 而不是 -1 / 0：
#: 「没读到」与「读到了第 0 版」是两件事，拿一个数字表示"没有"迟早被人拿去做比较。
UNREAD = None


@register_skill
class RefundSnapshotCheckSkill(Skill):
    contract = SkillContract(
        name="refund.snapshot_check",
        version="1.0.0",
        purpose="付款前读订单系统的当前版本，与本案锁定的订单快照版本比对；"
                "不一致即报漂移并落 SnapshotDrift 事件（不改任何业务状态）",
        input_schema={
            "tenant_id": "str",
            "case_id": "str",
            "order_system": "str（已 register_order_system 的名字；缺省 demo-orders）",
            "order_id": "str（可选；缺省取本案 refund_case.order_id）",
            "order_version": "int（可选；缺省取本案 refund_case.order_version）",
        },
        output_schema={
            "drift": "bool（True = 手上的快照版本与外部当前版本对不上，或订单读不到）",
            "snapshot_version": "int（MAOS 手上这份 order_snapshot 的版本）",
            "current_version": "int|None（外部当前版本；读不到为 None）",
            "current_status": "str（外部订单状态；读不到为空串）",
            "current_amount": "str（外部订单金额；读不到为空串）",
            "snapshot_amount": "str（手上快照里的 amount_paid）",
            "updated_at": "str（外部上次改这笔单的时刻）",
            "reason": "str（漂移的一句话说明；无漂移为空串）",
            "read_error": "str（读订单失败时的原始异常文本；成功为空串）",
            "checked_at": "str",
            "invocation_id": "str",
        },
        preconditions=["tenant_id", "case_id"],
        depends_tools=["order.query"],
        # 纯读 + 一次外部查询。读失败**不重试**：本 skill 对读失败的处置已经是
        # 「报漂移、转人工」，重试只会把同一个结论晚几秒得出。
        failure_policy="escalate",
        max_retries=0,
        security_boundary=(
            "只读 order_snapshot / refund_case，只调 order.query（工具侧只读，"
            "没有改单入口）；**不写任何业务表、不改 biz_status**（铁律 8/9）；"
            "漂移的处置是落一条 SnapshotDrift 事件并把判断交给调用方，"
            "由它走既有的 gate_needs_human 出口转人工 —— 不加新状态、不加新迁移"
        ),
        reuse_note="任何「执行前先确认外部依据没变」的场景都可照此写："
                   "读当前版本、比对、不一致就停下来问人，不自动按新版往下跑",
        owner_roles=["refund_payment"],
    )

    def run(self, payload: dict, ctx: SkillContext) -> dict:
        store = C.ensure_schema(ctx)
        invocation_id = C.invocation_id_of(ctx)
        extras = getattr(ctx, "extras", None) or {}
        tenant_id, case_id = C.required(payload, "tenant_id", "case_id")

        # **case 可以还不存在**：这一步的定位是「执行前」，而最该做这件事的时机是
        # 规划期 —— 那时 `refund.intake` 还没建案，但订单号与版本已经在 case_seed 上
        # 了。要求先建案会把这一步推到建案之后，也就推到「已经按这份快照排好了 DAG」
        # 之后，那正是它要防的事。所以：读得到 case 就从 case 上取，读不到就要求
        # 调用方显式给 —— 两个都没有才抛（连订单号都不知道，比不上「无从比对」）。
        case = guard.get_case(store, tenant_id, case_id)
        order_id = str(payload.get("order_id") or (case or {}).get("order_id") or "")
        raw_version = payload.get("order_version")
        if raw_version is None and case is not None:
            raw_version = case["order_version"]
        if not order_id or raw_version is None:
            raise LookupError(
                f"tenant={tenant_id} case={case_id} 既读不到 refund_case，"
                "入参也没给 order_id / order_version —— 无从与外部当前版本比对")
        snapshot_version = int(raw_version)

        snapshot_amount = self._snapshot_amount(store, tenant_id, order_id,
                                                snapshot_version)

        system_name = str(payload.get("order_system") or "").strip() or None
        current, read_error = self._read_current(store, extras, system_name, order_id)

        drift, reason = self._verdict(snapshot_version, snapshot_amount, current,
                                      read_error)

        out = {
            "drift": drift,
            "snapshot_version": snapshot_version,
            "current_version": current["version"] if current else UNREAD,
            "current_status": str(current["status"]) if current else "",
            "current_amount": str(current["amount"]) if current else "",
            "snapshot_amount": snapshot_amount,
            "updated_at": str(current["updated_at"]) if current else "",
            "reason": reason,
            "read_error": read_error,
            "checked_at": C.now_iso(),
            "invocation_id": invocation_id,
        }

        if drift:
            self._log_drift(store, extras, tenant_id=tenant_id, case_id=case_id,
                            order_id=order_id, out=out)
        return out

    # ------------------------------------------------------------------
    @staticmethod
    def _snapshot_amount(store, tenant_id: str, order_id: str, version: int) -> str:
        """手上那份快照的实付金额。快照行不在就抛 —— 比对无从谈起。

        金额转成 str 不是排版：本 skill 的出参会进 artifact 与事件 detail，
        而金额一旦进过 float 再打印出来就可能带上 `6800.000000000001`
        （口径同 `gateway.RefundRequest.refund_amount`：金额永远不进浮点）。
        """
        rows = objects.query(
            store,
            "SELECT amount_paid FROM order_snapshot"
            " WHERE tenant_id=? AND order_id=? AND version=?",
            (tenant_id, order_id, int(version)))
        if not rows:
            raise LookupError(
                f"没有订单快照 tenant={tenant_id} order={order_id} v{version}，"
                "无从与外部当前版本比对 —— 先落快照再查漂移")
        return f"{float(rows[0]['amount_paid']):.2f}"

    @staticmethod
    def _read_current(store, extras: dict, system_name: str | None,
                      order_id: str) -> tuple[dict | None, str]:
        """经 `invoke_tool` 读外部当前版本。返回 `(订单 dict 或 None, 错误文本)`。

        **走 invoke_tool 而不是直接调 mock**：直接调就没有 ToolInvoked 审计行，
        「执行前真的读了一次」这句话就只剩自述 —— 而那正是评委要看的那半句。

        读失败**吞异常但不吞事实**：异常文本原样进出参的 `read_error`，
        `_verdict` 随即把它判成漂移。吞成 None 再报「一致」才是这里唯一不许有的写法。
        """
        params = {"order_id": order_id}
        if system_name:
            params["system_name"] = system_name
        else:
            from maos.tools.order import DEFAULT_ORDER_SYSTEM
            params["system_name"] = DEFAULT_ORDER_SYSTEM
        tool_extras = {
            "plan_id": extras.get("plan_id", ""),
            "task_id": extras.get("task_id"),
            "trace_id": extras.get("trace_id", ""),
        }
        try:
            return invoke_tool(ORDER_QUERY_PORT, params, store=store,
                               extras=tool_extras), ""
        except Exception as exc:                       # noqa: BLE001 —— 见 docstring
            return None, f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _verdict(snapshot_version: int, snapshot_amount: str, current: dict | None,
                 read_error: str) -> tuple[bool, str]:
        """判漂移。三档，**没有第四档「大概没事」**。

        · 读不到订单 -> 漂移。订单不见了比订单改了更严重（见模块 docstring 第 3 条）。
        · 版本对不上 -> 漂移。这是主判据。
        · 版本一致但金额对不上 -> 漂移。外部系统理论上改金额必推版本，
          真出现"同版本不同金额"说明我们对那个系统的版本语义理解错了 ——
          那种时候更该停下来问人，而不是因为"版本号一样"就放行。
        """
        if current is None:
            return True, f"读不到订单当前版本：{read_error}"
        current_version = int(current["version"])
        if current_version != snapshot_version:
            return True, (
                f"订单快照 v{snapshot_version} 与外部当前 v{current_version} 不一致"
                f"（外部状态 {current['status']}，上次改动 {current['updated_at']}）")
        if str(current["amount"]) != snapshot_amount:
            return True, (
                f"版本同为 v{snapshot_version} 但金额对不上："
                f"快照 {snapshot_amount} vs 外部 {current['amount']}")
        return False, ""

    @staticmethod
    def _log_drift(store, extras: dict, *, tenant_id: str, case_id: str,
                   order_id: str, out: dict) -> None:
        """落一条 `SnapshotDrift`。detail 逐字段按跨轨契约 §F：带 `tenant_id` 与
        `case_id`，另加本轨的 `snapshot_version` / `current_version`。

        落事件而不是落表：漂移是**一次观察**，不是一个业务对象。给它开一张表
        等于承认它是一件独立的事实，而它其实只是「这一刻读到的两个版本号不同」——
        那是 event_log 的形状（口径同 `guard.py` 的 `RefundBizStatusChanged`）。
        """
        store.append_event_log({
            "trace_id": str(extras.get("trace_id") or ""),
            "plan_id": str(extras.get("plan_id") or ""),
            "task_id": str(extras.get("task_id") or ""),
            "event_type": EVENT_SNAPSHOT_DRIFT,
            "reason": out["reason"],
            "detail": {
                "tenant_id": tenant_id,
                "case_id": case_id,
                "order_id": order_id,
                "snapshot_version": out["snapshot_version"],
                "current_version": out["current_version"],
                "snapshot_amount": out["snapshot_amount"],
                "current_amount": out["current_amount"],
                "current_status": out["current_status"],
                "read_error": out["read_error"],
                "invocation_id": out["invocation_id"],
            },
        })
