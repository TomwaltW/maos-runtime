"""payment.observe —— 向网关**问**出终态，是全系统唯一写得进 settled 的地方。

这是「观察与推断分离」的落点，也是本域最该被评委追问的一处：

    问：你怎么知道这笔退款成功了？
    答：不是因为 refund() 没抛异常，是因为 query() 问到第 N 次时网关回了 settled，
        那份回执连同状态更新在**同一个事务**里落了库（`payment_observation`），
        `actor_invocation_id` 指回是哪一次调用观察到的。

轮询次数不是可有可无的细节：`MockGateway(settle_after=2)` 保证一次 query 不够，
`poll_count` 落在回执里 —— 它证明终态是问出来的，不是猜出来的。
把网关换成一步返回 settled 的桩，这个 skill 就没有存在理由了，整条论证跟着塌。

**非终态一律不推进**。`unknown` 尤其不许乐观地当成成功：官方 remedy 原文是
「保持参数不变重试或查询执行结果」，那说明那一笔可能已经发生了，也可能没有 ——
在这里替它下结论就是把外部状态写死为终态（铁律 8）。
"""

from __future__ import annotations

import json

from maos.domain.refund import guard, objects
from maos.skills.contract import Skill, SkillContext, SkillContract
from maos.skills.registry import register_skill
from maos.tools.gateway import GATEWAY_QUERY_PORT
from maos.tools.port import invoke_tool

from . import _common as C

#: 轮询上限。到顶仍非终态就如实返回「还没问出来」，**不许**改判成失败 ——
#: 「我问累了」和「网关说失败了」是两回事，混起来会让一笔实际成功的退款被当成失败收口。
DEFAULT_MAX_POLLS = 5


@register_skill
class PaymentObserveSkill(Skill):
    contract = SkillContract(
        name="payment.observe",
        version="1.0.0",
        purpose="轮询支付网关取得终态回执，写 payment_observation 并（仅在此处）写 settled",
        input_schema={
            "tenant_id": "str",
            "case_id": "str",
            "gateway": "str（已 register_gateway 的名字，默认 'demo'）",
            "request_id": "str（可选，缺省取该案子最近一笔 refund_request）",
            "max_polls": "int（可选，默认 5）",
        },
        output_schema={
            "receipt": "dict（终态回执，或到顶时的最后一次观察）",
            "observed_state": "settled|failed|processing|unknown",
            "poll_count": "int（问了几次 —— 终态是问出来的证据）",
            "biz_status": "str（settled 只可能由本 skill 写入）",
            "settled": "bool",
            "needs_compensation": "bool（网关明确失败时为 True，收口归失败路径场景）",
            "invocation_id": "str",
        },
        preconditions=["tenant_id", "case_id"],
        depends_tools=["gateway.query"],
        failure_policy="escalate",
        max_retries=0,
        security_boundary=(
            "本 skill 是 guard.AUTHORITATIVE_WRITER —— 全系统唯一可写 settled 的 actor，"
            "且写入必须同事务附回执，缺字段由 guard 抛 AuthoritativeFactViolation；"
            "非终态一律不推进状态；网关调用一律经 invoke_tool 留审计行"
        ),
        reuse_note="任何「权威在外部系统」的终态都该照此写：先观察、再落库，两件事同一个事务",
        owner_roles=["refund_payment"],
    )

    def run(self, payload: dict, ctx: SkillContext) -> dict:
        store = C.ensure_schema(ctx)
        invocation_id = C.invocation_id_of(ctx)
        extras = getattr(ctx, "extras", None) or {}
        tenant_id, case_id = C.required(payload, "tenant_id", "case_id")

        case = guard.get_case(store, tenant_id, case_id)
        if case is None:
            raise LookupError(f"没有这个 case：tenant={tenant_id} case={case_id}")

        request_id = str(payload.get("request_id") or "").strip()
        if not request_id:
            rows = objects.query(
                store,
                "SELECT request_id FROM refund_request WHERE tenant_id=? AND case_id=?"
                " ORDER BY submitted_at DESC", (tenant_id, case_id))
            if not rows:
                raise LookupError(
                    f"case={case_id} 没有 refund_request，没有可观察的对象；"
                    "先跑 payment.execute")
            request_id = rows[0]["request_id"]

        gateway = C.get_gateway(payload.get("gateway"))
        max_polls = int(payload.get("max_polls") or DEFAULT_MAX_POLLS)
        if max_polls < 1:
            raise ValueError("max_polls 至少为 1 —— 一次都不问就没有观察可言")

        tool_extras = {
            "plan_id": extras.get("plan_id", ""),
            "task_id": extras.get("task_id"),
            "trace_id": extras.get("trace_id", ""),
        }

        receipt = None
        for _ in range(max_polls):
            receipt = invoke_tool(GATEWAY_QUERY_PORT,
                                  {"gateway": gateway, "request_id": request_id},
                                  store=store, extras=tool_extras)
            if receipt.get("is_terminal"):
                break

        assert receipt is not None                     # max_polls >= 1，循环必至少跑一次
        status = str(receipt.get("status"))
        poll_count = int(receipt.get("poll_count") or 0)

        if not receipt.get("is_terminal"):
            # 到顶仍非终态：如实返回。不写状态、不写观察行 ——
            # 「还没问出来」不是一个可以落库的结论。
            return self._out(receipt, case["biz_status"], settled=False,
                             needs_compensation=False, invocation_id=invocation_id,
                             poll_count=poll_count)

        if status == "failed":
            # 网关明确失败。**不推进业务状态** —— 走到 compensated 意味着补偿已经做完，
            # 而补偿是失败路径场景的事，在这里替它宣布收口就是又一次把状态写死。
            # 观察本身仍要留痕，否则「网关说失败了」这件事只活在日志里。
            self._record_failure(store, tenant_id, case_id, request_id, receipt,
                                 invocation_id)
            return self._out(receipt, case["biz_status"], settled=False,
                             needs_compensation=True, invocation_id=invocation_id,
                             poll_count=poll_count)

        # ---- 终态成功：唯一写 settled 的路径 ---------------------------------
        # 网关受理后停在 gateway_accepted（回执曾是 unknown）时先补一跳到 processing：
        # 业务状态机不允许 gateway_accepted 直接到 settled，而中间这一跳描述的正是
        # 「已确认在处理中」这个刚刚被观察到的事实。
        if case["biz_status"] == "gateway_accepted":
            case = guard.update_biz_status(
                store, tenant_id, case_id, "processing",
                self.contract.name, invocation_id,
                reason=f"观察到网关处理中（poll={poll_count}）")

        observation = {
            "request_id": request_id,
            "gateway_code": str(receipt.get("code") or ""),
            "raw_receipt_json": json.dumps(receipt, ensure_ascii=False, sort_keys=True),
            "observed_state": status,
            "observed_at": C.now_iso(),
        }
        case = guard.update_biz_status(
            store, tenant_id, case_id, "settled",
            self.contract.name, invocation_id,
            observation=observation,
            reason=f"网关回执 settled（问了 {poll_count} 次，code={receipt.get('code')}）")

        return self._out(receipt, case["biz_status"], settled=True,
                         needs_compensation=False, invocation_id=invocation_id,
                         poll_count=poll_count)

    # ------------------------------------------------------------------
    @staticmethod
    def _record_failure(store, tenant_id: str, case_id: str, request_id: str,
                        receipt: dict, invocation_id: str) -> None:
        """明确失败的观察直接落 payment_observation。

        走 `objects.execute` 而不是 guard：guard 的「同事务附回执」是 **settled 的**
        前置条件（观察 ⇐ 终态），反过来并不要求每条观察都伴随一次状态迁移。
        这里没有合法的目标状态可迁，但这条观察必须留下来。
        """
        objects.execute(
            store,
            "INSERT OR REPLACE INTO payment_observation (tenant_id, case_id, request_id,"
            " gateway_code, raw_receipt_json, observed_state, observed_at,"
            " actor_invocation_id) VALUES (?,?,?,?,?,?,?,?)",
            (tenant_id, case_id, request_id, str(receipt.get("code") or ""),
             json.dumps(receipt, ensure_ascii=False, sort_keys=True),
             "failed", C.now_iso(), invocation_id),
        )

    @staticmethod
    def _out(receipt: dict, biz_status: str, *, settled: bool, needs_compensation: bool,
             invocation_id: str, poll_count: int) -> dict:
        return {
            "receipt": receipt,
            "observed_state": receipt.get("status"),
            "poll_count": poll_count,
            "biz_status": biz_status,
            "settled": settled,
            "needs_compensation": needs_compensation,
            "remedy": receipt.get("remedy", ""),
            "source": receipt.get("source", ""),
            "invocation_id": invocation_id,
        }
