"""investigation.observe —— 向清算方**问**出决议，是全系统唯一写得进 `returned` 的地方。

这是「观察与推断分离」的落点，也是本域最该被评委追问的一处：

    问：你怎么知道这笔钱退回来了？
    答：不是因为 camt.056 发出去没报错，**也不是因为清算方回了「撤销成功」**，
        是因为问到第 N 次时收到了一份 pacs.004 退款报文，那份观察连同状态更新
        在同一个事务里落了库（`resolution_observation`），
        `actor_invocation_id` 指回是哪一次调用观察到的。

## 中间那句「撤销成功」才是本域的题眼

`camt.029` 的结论码 `CNCL`（CancelledAsPerRequest）官方定义是
「Used when a requested cancellation is successful.」—— 一句不折不扣的肯定答复。
本 skill 收到它的时候**一个状态都不推**，只落一条 `cancellation_confirmed` 观察。

因为 CNCL 肯定的是**那条撤销指令**，不是资金。钱回没回来由 `pacs.004` 说，
它带自己的退回原因码和退回金额。演示里两条路径共用同一句 CNCL：

  · 顺利路径：PDCR -> **CNCL** -> pacs.004  → 这时才写 returned
  · 失败路径：PDCR -> **CNCL** -> CNCL -> …  pacs.004 永远不来 → 一个字都不写

把 CNCL 当成业务成功的系统，会在失败路径上报「成功」，而钱一分都没回来。
这就是「Agent 都回复完成 ≠ 业务成功」在这个域里的具体形状。

## 非终态一律不推进

问到顶仍没有 pacs.004 时**如实返回**「还没问出来」，不写状态、不写 returned 观察。
「我问累了」和「清算方说不给退」是两回事，混起来会让一笔实际会退回的款被当成失败收口。
问询次数落在 `poll_count` 上 —— 它证明结论是问出来的，不是猜出来的。
"""

from __future__ import annotations

import json

from maos.domain.investigation import guard
from maos.skills.contract import Skill, SkillContext, SkillContract
from maos.skills.registry import register_skill
from maos.tools import investigation_codes as codes
from maos.tools.investigation import CLEARING_RESOLUTION_PORT
from maos.tools.port import invoke_tool

from . import _common as C

#: 问询上限。到顶仍没有资金证据就如实返回「还没问出来」，**不许**改判成失败 ——
#: 「我问累了」和「清算方说不行」是两回事，混起来会让一笔实际会退回的款被当成失败收口。
DEFAULT_MAX_POLLS = 5


def observed_state_of(receipt: dict) -> str:
    """把一份清算方回执归一成 guard 认的观察状态。**本域唯一的归一口径。**

    分档顺序不可换，每一档挡掉一种具体的误判：

    1. `funds_settled`（= pacs.004）→ `returned`。**必须排第一**：它是唯一的资金证据。
    2. `resolution == rejected` → `rejected`。请求被明确拒了，有结论。
    3. `resolution == confirmed` → `cancellation_confirmed`。**不是 returned** ——
       这一档就是 CNCL，本域全部风险集中在这里。
    4. 其余（pending / partial / other）→ `pending`。没有结论，继续问。

    `funds_settled` 取自 `ResolutionReceipt.funds_settled`，那是对 `message_type`
    的判定（只有 pacs.004 为真），不是可以手填的字段 —— 见 tools 层的 property。
    """
    if receipt.get("funds_settled"):
        return guard.OBS_RETURNED
    resolution = str(receipt.get("resolution") or "")
    if resolution == codes.RESOLUTION_REJECTED:
        return guard.OBS_REJECTED
    if resolution == codes.RESOLUTION_CONFIRMED:
        # 清算方说撤销成功了。**不是** returned —— 见模块 docstring。
        return guard.OBS_CANCELLATION_CONFIRMED
    return guard.OBS_PENDING


@register_skill
class InvestigationObserveSkill(Skill):
    contract = SkillContract(
        name="investigation.observe",
        version="1.0.0",
        purpose=("问询清算方取得决议与资金下落，写 resolution_observation 并"
                 "（仅在此处、且仅凭 pacs.004）写 returned"),
        input_schema={
            "tenant_id": "str",
            "case_id": "str",
            "clearing": "str（已 register_clearing 的名字，缺省 'demo'）",
            "request_id": "str（可选，缺省取该案子最近一笔 cancellation_request）",
            "max_polls": "int（可选，默认 5）",
        },
        output_schema={
            "receipt": "dict（终态回执，或到顶时的最后一次观察）",
            "observed_state": "returned|cancellation_confirmed|rejected|pending",
            "poll_count": "int（问了几次 —— 结论是问出来的证据）",
            "biz_status": "str（returned 只可能由本 skill 写入）",
            "funds_returned": "bool（**只有 pacs.004 才为 True**）",
            "request_resolved": "bool（撤销请求有结论了吗；CNCL 时为 True）",
            "needs_compensation": "bool（明确被拒或问不出资金下落时为 True）",
            "invocation_id": "str",
        },
        preconditions=["tenant_id", "case_id"],
        depends_tools=["clearing.resolution"],
        failure_policy="escalate",
        max_retries=0,
        security_boundary=(
            "本 skill 是 guard.AUTHORITATIVE_WRITER —— 全系统唯一可写 returned 的 actor，"
            "且写入必须同事务附一份 **pacs.004** 观察（带退回原因码与退回金额），"
            "缺任一条由 guard 抛 AuthoritativeFactViolation；"
            "camt.029 的肯定答复（CNCL）**写不进 returned** —— 它证明的是撤销指令已执行，"
            "不是资金已退回；"
            "非终态一律不推进状态；清算方调用一律经 invoke_tool 留审计行"
        ),
        reuse_note=("任何「权威在外部系统、且肯定答复与业务成功不是一回事」的域都该照此写："
                    "先归一观察、再按证据类型分档、只有拿到那一类证据才收口"),
        owner_roles=["investigation_observe"],
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
            from maos.domain.investigation import objects
            rows = objects.query(
                store,
                "SELECT request_id FROM cancellation_request WHERE tenant_id=? AND case_id=?"
                " ORDER BY sent_at DESC", (tenant_id, case_id))
            if not rows:
                raise LookupError(
                    f"case={case_id} 没有 cancellation_request，没有可观察的对象；"
                    "先跑 investigation.cancel")
            request_id = rows[0]["request_id"]

        clearing = C.get_clearing(payload.get("clearing"))
        max_polls = int(payload.get("max_polls") or DEFAULT_MAX_POLLS)
        if max_polls < 1:
            raise ValueError("max_polls 至少为 1 —— 一次都不问就没有观察可言")

        tool_extras = {
            "plan_id": extras.get("plan_id", ""),
            "task_id": extras.get("task_id"),
            "trace_id": extras.get("trace_id", ""),
        }

        # **每一次问询都是一次观察，每一次都留痕。**
        # 只留最后一次的话，「第 2 次就问到了 CNCL（撤销成功），而系统那时一个字
        # 都没写」这件事就没有证据 —— 而那正是本域要演的东西。
        seen: list[dict] = []
        for _ in range(max_polls):
            receipt = invoke_tool(CLEARING_RESOLUTION_PORT,
                                  {"clearing": clearing, "request_id": request_id},
                                  store=store, extras=tool_extras)
            seen.append(receipt)
            if receipt.get("is_terminal"):
                break

        # 中间那几次照实落库。它们一律不是 returned（returned 必然终态、必然是最后一次），
        # 所以走 insert_observation 而不经 guard 的状态迁移。
        for mid in seen[:-1]:
            guard.insert_observation(
                store, tenant_id=tenant_id, case_id=case_id,
                observation=self._observation(request_id, mid, observed_state_of(mid)),
                invocation_id=invocation_id)

        receipt = seen[-1]                             # max_polls >= 1，循环必至少跑一次
        state = observed_state_of(receipt)
        poll_count = int(receipt.get("poll_count") or 0)
        observation = self._observation(request_id, receipt, state)

        # ---- 分档。顺序照 observed_state_of 的分档，一档一个出口 ----------------
        if state == guard.OBS_RETURNED:
            # 唯一写 returned 的路径。观察与状态更新由 guard 放进同一个事务。
            case = guard.update_biz_status(
                store, tenant_id, case_id, "returned",
                self.contract.name, invocation_id,
                observation=observation,
                reason=(f"收到 {receipt.get('message_type')} 退款报文"
                        f"（问了 {poll_count} 次，退回原因码 "
                        f"{receipt.get('return_reason_code')}，"
                        f"退回金额 {receipt.get('returned_amount')}）"))
            return self._out(receipt, case["biz_status"], state,
                             needs_compensation=False, invocation_id=invocation_id,
                             poll_count=poll_count)

        # 以下三档**都不推进业务状态**，但观察一律留痕 ——
        # 否则「清算方说了什么」这件事只活在日志里。
        guard.insert_observation(store, tenant_id=tenant_id, case_id=case_id,
                                 observation=observation, invocation_id=invocation_id)

        if state == guard.OBS_REJECTED:
            # 撤销请求被明确拒了。**不推进到 rejected**：走到那一步意味着收口已经做完，
            # 而收口（补偿 / 转人工）是失败路径场景的事，在这里替它宣布就是又一次
            # 把状态写死（口径同退款域 payment.observe 对 failed 的处置）。
            return self._out(receipt, case["biz_status"], state,
                             needs_compensation=True, invocation_id=invocation_id,
                             poll_count=poll_count)

        # 剩下两档：cancellation_confirmed 与 pending。
        # **cancellation_confirmed 不是成功**：清算方确认撤销了，但资金证据还没到。
        # 到顶都没等到 pacs.004 时按「问不出来」处置，需要收口 —— 但收口是补偿的事。
        return self._out(receipt, case["biz_status"], state,
                         needs_compensation=True, invocation_id=invocation_id,
                         poll_count=poll_count)

    # ------------------------------------------------------------------
    @staticmethod
    def _observation(request_id: str, receipt: dict, state: str) -> dict:
        """把一份回执折成 `resolution_observation` 的一行。

        `returned_amount` 只在 pacs.004 上有值，别的报文一律 None ——
        给 camt.029 填一个金额会让「有金额」不再是资金证据的标志。
        """
        return {
            "request_id": request_id,
            # 第几次问询问到的。进主键，同一微秒内的两条不会互相覆盖，
            # 也让观察能按问询顺序念出来。
            "poll_seq": int(receipt.get("poll_count") or 0),
            "message_type": str(receipt.get("message_type") or ""),
            "confirmation_code": str(receipt.get("confirmation_code") or ""),
            "rejection_code": str(receipt.get("rejection_code") or ""),
            "return_reason_code": str(receipt.get("return_reason_code") or ""),
            "returned_amount": (receipt.get("returned_amount")
                                if receipt.get("funds_settled") else None),
            "raw_message_json": json.dumps(receipt, ensure_ascii=False, sort_keys=True),
            "observed_state": state,
            "observed_at": C.now_iso(),
        }

    @staticmethod
    def _out(receipt: dict, biz_status: str, state: str, *, needs_compensation: bool,
             invocation_id: str, poll_count: int) -> dict:
        return {
            "receipt": receipt,
            "observed_state": state,
            "poll_count": poll_count,
            "biz_status": biz_status,
            # 两个正交的布尔，不许压成一个。funds_returned 才是「业务成功了吗」。
            "funds_returned": bool(receipt.get("funds_settled")),
            "request_resolved": bool(receipt.get("request_resolved")),
            "needs_compensation": needs_compensation,
            "definition": receipt.get("definition", ""),
            "source": receipt.get("source", ""),
            "invocation_id": invocation_id,
        }
