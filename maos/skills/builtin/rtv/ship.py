"""rtv.ship —— 把货发回供应商，并**取承运商的回执**。

SOP 第 ③ 步。这一步有一个容易被做错的地方：`shipped` 不是「我们把货交出去了」，
是「承运商说送到了」。

    问：你怎么知道货退到供应商那儿了？
    答：不是因为 carrier.ship 没抛异常，是因为 carrier.track 问到第 N 次时承运商
        回了 delivered，那条状态连同运单号落在 `rtv_shipment` 里。

所以本 skill 只在承运商回 `delivered` 时才推进 `disposed -> shipped`：

    created     运单建好了，货还没走      -> 不推进
    in_transit  在途                      -> 不推进
    exception   承运商侧异常（丢件/退回） -> 不推进，转补偿或人工
    delivered   送达                      -> 推进到 shipped

承运商是外部系统，`rtv_shipment.carrier_status` 是**观察结果**不是我方决定
（契约 C-R1 那一列的注释）。但注意它与 `credited` / `settled` 不同：承运商说的是
物流事实，不是钱的事实，所以这一跳不归 `rtv.observe` 管，也不进
`AUTHORITATIVE_STATES` —— 两个权威终态说的都是钱（契约 C-R3）。

## 不重发运单

已经有一张非 `exception` 的运单时，重跑**只查不发**。`carrier.ship` 是写操作，
重发的后果是同一批货有两张运单，而供应商侧会收到两次到货 —— 到那一步再纠正，
成本远高于在这里挡一道。理由同 `ap/_common.py::idempotency_key` 那条注释。
"""

from __future__ import annotations

from maos.skills.contract import Skill, SkillContext, SkillContract
from maos.skills.registry import register_skill

from . import _common as C

#: 轮询上限。到顶仍非终态就如实返回「还在路上」，**不许**改判成送达或异常。
DEFAULT_MAX_POLLS = 5


@register_skill
class RtvShipSkill(Skill):
    contract = SkillContract(
        name="rtv.ship",
        version="1.0.0",
        purpose="下发退货运单并轮询承运商回执，写 rtv_shipment；仅在 delivered 时推进到 shipped",
        input_schema={
            "tenant_id": "str",
            "case_id": "str",
            "carrier": "str（承运商名，缺省 'demo-carrier'）",
            "address": "str（可选，退货收货地址，原样递给承运商）",
            "max_polls": "int（可选，默认 5）",
        },
        output_schema={
            "shipment": "dict（rtv_shipment 当前那一行）",
            "carrier_status": "created|in_transit|delivered|exception",
            "poll_count": "int（问了几次 —— 送达是问出来的证据）",
            "delivered": "bool",
            "reshipped": "bool（False 表示复用了已有运单，没有重发）",
            "needs_compensation": "bool（承运商回 exception 时为 True）",
            "biz_status": "str（shipped 只可能由本 skill 在 delivered 时写入）",
            "invocation_id": "str",
        },
        preconditions=["tenant_id", "case_id"],
        depends_tools=["carrier.ship", "carrier.track"],
        failure_policy="retry",
        max_retries=1,
        security_boundary=(
            "承运商调用一律经 invoke_tool 留 ToolInvoked 审计行；已有非 exception 运单时"
            "只查不发，不重复下发写操作。只写 rtv_shipment 与 biz_status: disposed->shipped，"
            "credited / settled 在本 skill 里没有任何写入路径"
        ),
        reuse_note="任何「交出去之后要等对方确认」的一步都该照此写：非终态一律不推进，"
                   "写操作不重发，终态由查询得到而不是由发起动作推定",
        owner_roles=["rtv_logistics"],
    )

    def run(self, payload: dict, ctx: SkillContext) -> dict:
        store = C.ensure_schema(ctx)
        invocation_id = C.invocation_id_of(ctx)
        tenant_id, case_id = C.required(payload, "tenant_id", "case_id")

        case = C.get_case(store, tenant_id, case_id)
        if case is None:
            raise LookupError(f"没有这个 case：tenant={tenant_id} case={case_id}")

        max_polls = int(payload.get("max_polls") or DEFAULT_MAX_POLLS)
        if max_polls < 1:
            raise ValueError("max_polls 至少为 1 —— 一次都不问就没有回执可言")

        # ---- 1. 运单：已有非 exception 的就复用，不重发 ----------------------
        existing = [r for r in C.query(
            store, "SELECT * FROM rtv_shipment WHERE tenant_id=? AND case_id=?"
                   " ORDER BY shipped_at", (tenant_id, case_id))
            if str(r["carrier_status"]) != "exception"]
        if existing:
            shipment = existing[-1]
            carrier = str(shipment["carrier"])
            tracking_no = str(shipment["tracking_no"])
            shipment_id = str(shipment["shipment_id"])
            reshipped = False
        else:
            carrier = str(payload.get("carrier") or "demo-carrier")
            receipt = C.call_tool(ctx, store, C.TOOL_CARRIER_SHIP, {
                "tenant_id": tenant_id, "case_id": case_id, "carrier": carrier,
                "address": str(payload.get("address") or ""),
                "lines": C.lines_of(store, tenant_id, case_id),
            })
            shipment_id = str(receipt["shipment_id"])
            tracking_no = str(receipt["tracking_no"])
            carrier = str(receipt.get("carrier") or carrier)
            status = C.require_state("carrier", receipt.get("status") or "created")
            C.execute(
                store,
                "INSERT OR REPLACE INTO rtv_shipment (tenant_id, case_id, shipment_id,"
                " carrier, tracking_no, carrier_status, shipped_at) VALUES (?,?,?,?,?,?,?)",
                (tenant_id, case_id, shipment_id, carrier, tracking_no, status,
                 C.now_iso()))
            reshipped = True

        # ---- 2. 轮询轨迹。终态由我方按取值域判，不看回执自述的 is_terminal ----
        status, poll_count = "", 0
        for _ in range(max_polls):
            poll_count += 1
            track = C.call_tool(ctx, store, C.TOOL_CARRIER_TRACK, {
                "tenant_id": tenant_id, "case_id": case_id,
                "shipment_id": shipment_id, "tracking_no": tracking_no,
            })
            status = C.require_state("carrier", track.get("status"))
            if C.terminal("carrier", status):
                break

        C.execute(store, "UPDATE rtv_shipment SET carrier_status=? WHERE tenant_id=?"
                         " AND case_id=? AND shipment_id=?",
                  (status, tenant_id, case_id, shipment_id))

        # ---- 3. 只有 delivered 才推进 ---------------------------------------
        delivered = status == "delivered"
        if delivered and str(case["biz_status"]) == "disposed":
            case = C.update_biz_status(
                store, tenant_id, case_id, "shipped", self.contract.name, invocation_id,
                poll_count=poll_count,
                reason=f"承运商回执 delivered（问了 {poll_count} 次，运单 {tracking_no}）")

        rows = C.query(store, "SELECT * FROM rtv_shipment WHERE tenant_id=? AND case_id=?"
                              " AND shipment_id=?", (tenant_id, case_id, shipment_id))
        return {
            "shipment": rows[0] if rows else {},
            "carrier_status": status,
            "poll_count": poll_count,
            "delivered": delivered,
            "reshipped": reshipped,
            "needs_compensation": status == "exception",
            "biz_status": str(case["biz_status"]),
            "invocation_id": invocation_id,
        }
