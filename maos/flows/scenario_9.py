"""场景 9：银行差错处理 —— 撤销请求的答案只有清算方给得出。**两条路径**。

    顺利路径  受理差错 → 定性（**人工调账必须人批**，停 BLOCKED）
              → 发出 camt.056 → 问询决议
              → 第 1 次 PDCR（未决）
              → 第 2 次 **CNCL（清算方说「撤销成功」）—— 一个字都不写**
              → 第 3 次 pacs.004 退款报文 → **这时才**写 returned
              → 收口人放行 → Plan DONE

    失败路径  受理差错 → 定性（人批）→ 发出 camt.056 → 问询决议
              → 第 1 次 PDCR
              → 第 2 次 **CNCL —— 与顺利路径逐字相同的那一句肯定答复**
              → 第 3/4/5 次仍是 CNCL，**pacs.004 永远不来**
              → 问到上限：不写 returned、不写任何资金结论
              → 收口人驳回：撤销确认了但钱没回来，转人工
              → investigation.compensate：留档最后观察 + 开人工对账工单 → compensated
              → Plan FAILED
              → investigation_case.biz_status = 'compensated'，**从未进入 returned**

## 这个场景要证明的两句话

1. **权威事实边界成立**：全系统只有 `investigation.observe` 写得进 `returned`，
   而且只有拿到 pacs.004 才写得进去。越权写入不静默失败，落
   `AuthoritativeFactViolation` 事件并抛。

2. **「Agent 都说完成了」不等于业务成功**：失败路径上**四个 Agent 全回 ok**，
   产物齐全、`summary` 写得明明白白，而案子确实没成 —— 钱一分都没回来。

## 本场景最值钱的一处：两条路径共用同一句肯定答复

`camt.029` 的结论码 `CNCL`（CancelledAsPerRequest）官方定义是
「Used when a requested cancellation is successful.」—— 不折不扣的肯定答复。
它在两条路径的**同一个位置**出现，逐字相同：

    顺利路径  PDCR → **CNCL** → pacs.004      钱回来了
    失败路径  PDCR → **CNCL** → CNCL → …      钱没回来

一个把 CNCL 当成业务成功的系统，会在失败路径上报「成功」而资金分文未动。
所以本域的判据不是「清算方答复了吗」，是「**答复里有没有 pacs.004**」——
`guard.AUTHORITATIVE_EVIDENCE` 把它钉在守卫层，
`investigation_codes.is_funds_evidence()` 把它钉在码表层。

> **与派单原文的一处出入**（已记 docs/DECISIONS.md）：派单写「camt.029 恒为否定
> 答复，肯定答复走 pacs.004」。按 ISO 官方码表实查不成立 —— camt.029 可以是肯定
> 答复（CNCL）。判据因此比派单原文**更严**，本场景演的正是这条更严的判据。

## 三处刻意不「顺手兜底」的地方，每一处都是铁律 8 的落点

1. **CNCL 不推进状态**。清算方说撤销成功了，`investigation.observe` 只落一条
   `cancellation_confirmed` 观察，一个状态都不推。「指令撤销了」与「钱回来了」
   是两件事，压成一个布尔就等于把外部状态写死为终态。

2. **问到上限不改判成失败**。失败路径问满 5 次仍无 pacs.004 时，
   `resolution_observation` 里躺着 5 行**真实**观察（1 条 pending + 4 条
   cancellation_confirmed），**0 行 returned** —— 而不是一行伪造的 failed。

3. **补偿不宣布那笔钱没退回来**。`last_observed_state` 落的是
   `cancellation_confirmed` 而不是 `rejected`：清算方明明确认撤销了，
   写成「被拒」会让对账的人从错误的方向开始查（见 `compensate._todo_for`）。

## 业务状态不进 Task 状态机（铁律 9）

`compensated` / `returned` 都是 `investigation_case` 自己的字段，
`maos/contracts/states.py` 一个新状态、一条新迁移都没加 ——
本场景的收口断言之一就是全部 Task 迁移仍落在既有迁移表内。

## 无 key 可跑

`select_model_client(SCRIPT, force_scripted=True)`：配了 key 的机器上也一行网络都不走。
金额、原因码、报文时序、问询次数全部写死，连跑两次输出逐条一致。

## 为什么不接进 `main.py` 的缺省场景序列

本轮三轨都在新增 flow，谁改 `maos/main.py` 谁就和另两轨冲突。
接进缺省序列是整合轮的事；本场景由 `maos/tests/test_investigation_flow.py` 调用。
"""

from __future__ import annotations

import json

from maos.agents.investigation import (
    KIND_CANCELLATION_REQUEST,
    KIND_RESOLUTION,
    ROLE_CANCEL,
    ROLE_CLASSIFY,
    ROLE_INTAKE,
    ROLE_OBSERVE,
)
from maos.contracts.events import new_id
from maos.contracts.states import TASK_TRANSITIONS, PlanState, TaskState
from maos.domain.investigation import guard, objects
from maos.flows.common import build, dump, run_until_settled
from maos.model.client import Tier, select_model_client
from maos.runtime.gate import HumanApprovalQueue
from maos.skills.builtin.investigation import _common as C
from maos.skills.builtin.investigation.compensate import (
    KIND_CANCELLATION_WITHDRAWN,
    KIND_MANUAL_TICKET,
)
from maos.skills.invoker import SkillInvoker
from maos.tools.investigation import (
    SCRIPT_CONFIRMED_ONLY,
    SCRIPT_RETURNED,
    MockClearingHouse,
)
from maos.agents.base import AgentIdentity

# ---------------------------------------------------------------------- 常量
# 全部写死，不用 new_id：验收之一是「连跑两次输出逐条一致」，而 dump() 会打印
# task_id。随机 id 会让两次输出必然不同。
TENANT_ID = "tnt-bank-001"

#: 付款行 / 收款行 BIC。差错处理是**行间**对话，两边都要有名有姓。
DEBTOR_AGENT = "DEUTDEFFXXX"
CREDITOR_AGENT = "BNPAFRPPXXX"

# ---- 第一笔：顺利路径 -------------------------------------------------------
CASE_ID = "case-s9-0001"
ORIGINAL_MSG_ID = "MSG-S9-88231"
ORIGINAL_VERSION = 1
END_TO_END_ID = "E2E-S9-88231"
AMOUNT = 12500.00
CURRENCY = "EUR"
VALUE_DATE = "2026-08-20"
CLEARING_OK = "s9-clearing"

# ---- 第二笔：失败路径 -------------------------------------------------------
CASE_ID_2 = "case-s9-0002"
ORIGINAL_MSG_ID_2 = "MSG-S9-88232"
END_TO_END_ID_2 = "E2E-S9-88232"
AMOUNT_2 = 48000.00
CLEARING_STUCK = "s9-clearing-stuck"

#: 定性：重复支付。`DUPL` = DuplicatePayment，官方定义
#: 「Payment is a duplicate of another payment.」—— 判据表在
#: `maos/skills/builtin/investigation/classify.py`，import 时机器核对码还在。
CLASSIFICATION = "duplicate_payment"

#: 清算方第几次问询才给出决议。**必须 > 1** —— 一次就给结论的 mock 会让
#: 「决议是问出来的」这条论证塌掉。第 2 次给 CNCL，第 3 次（如果有）给 pacs.004。
RESOLVE_AFTER = 2

#: 顺利路径问 3 次就够（PDCR → CNCL → pacs.004）。
MAX_POLLS_OK = 3
#: 失败路径问满 5 次，每次都是 CNCL，pacs.004 永远不来。
MAX_POLLS_STUCK = 5

# ---- 任务 id。写死，理由同上 ------------------------------------------------
TASK_FILE = "task-s9-file"
TASK_CLASSIFY = "task-s9-classify"
TASK_CANCEL = "task-s9-cancel"
TASK_OBSERVE = "task-s9-observe"

TASK_FILE_2 = "task-s9b-file"
TASK_CLASSIFY_2 = "task-s9b-classify"
TASK_CANCEL_2 = "task-s9b-cancel"
TASK_OBSERVE_2 = "task-s9b-observe"

#: 审批人。真房间演示时换成 `@boss-bank:maos.local`（见 §5.4 的房间脚本）。
APPROVER = "@boss-bank:maos.local"
ADJUSTMENT_REASON = "已核对原始报文与账务，同意人工调账撤销该笔重复支付"
REJECT_REASON = "清算方确认撤销但未收到退款报文，资金下落未明，转人工对账"

GOAL = "处理一笔跨行重复支付差错：向清算方发起撤销并确认资金退回"
GOAL_2 = "处理第二笔跨行重复支付差错：清算方确认撤销但资金未退回"


# ------------------------------------------------------------------- DAG
def _tasks(*, case_id: str, msg_id: str, clearing: str, max_polls: int,
           file_id: str, classify_id: str, cancel_id: str, observe_id: str) -> list[dict]:
    """两条路径的 DAG 同构，只换案子与清算方实例。

    **effect_risk 落在哪两步，是本场景的设计核心**：

    · `classify` 带 `effect_risk="H"` —— 这一步是**人工调账授权**。
      差错处理里动别人的钱必须先有人批，这是监管硬要求不是产品选项。
      人在这里放行之后，`_common.record_adjustment_approval` 才落
      `adjustment_approval`，而 `investigation.cancel` 读不到它就拒绝发报文。
      授权在**发报文之前**，顺序不可换：发出去的 camt.056 撤不回来。

    · `observe` 带 `effect_risk="H"` —— 收口是对客/对手行的不可逆动作，
      Gate 过了也要人放行。失败路径的转折点就在这里：主管拿到的产物写着
      「清算方已确认撤销」，但 `funds_returned=False` —— 他必须驳回。
      **没有这一步，一句 CNCL 会被当成成功一路挂着。**

    · `cancel` 只有 `risk_level="M"`：它发报文，但发之前已经有人批过了，
      再停一次是给演示加噪音。真实行里这一步是自动发的。
    """
    return [
        {"task_id": file_id, "role": ROLE_INTAKE,
         "title": "受理跨行支付差错并核对原始报文快照",
         "inputs": {"biz_type": C.BIZ_TYPE, "tenant_id": TENANT_ID, "case_id": case_id,
                    "original_msg_id": msg_id, "original_version": ORIGINAL_VERSION,
                    "creator_agent": DEBTOR_AGENT, "assignee_agent": CREDITOR_AGENT},
         "acceptance": ["建出 investigation_case", "金额币种取自原始支付快照"],
         "depends_on": [], "risk_level": "L"},

        {"task_id": classify_id, "role": ROLE_CLASSIFY,
         "title": "差错定性并申请人工调账授权",
         "inputs": {"biz_type": C.BIZ_TYPE, "tenant_id": TENANT_ID, "case_id": case_id,
                    "classification": CLASSIFICATION},
         "acceptance": ["给出官方撤销原因码与其定义原文", "人工调账须经审批放行"],
         "depends_on": [file_id], "risk_level": "M", "effect_risk": "H"},

        {"task_id": cancel_id, "role": ROLE_CANCEL,
         "title": "向清算方发出 camt.056 撤销请求",
         "inputs": {"biz_type": C.BIZ_TYPE, "tenant_id": TENANT_ID, "case_id": case_id,
                    "clearing": clearing},
         "acceptance": ["发出前必须读到 approved 的调账审批", "发出后不得写 returned"],
         "depends_on": [classify_id], "risk_level": "M"},

        {"task_id": observe_id, "role": ROLE_OBSERVE,
         "title": "问询清算方决议并确认资金下落",
         "inputs": {"biz_type": C.BIZ_TYPE, "tenant_id": TENANT_ID, "case_id": case_id,
                    "clearing": clearing, "max_polls": max_polls},
         "acceptance": ["returned 只能凭 pacs.004 写入", "撤销确认不等于资金退回"],
         "depends_on": [cancel_id], "risk_level": "M", "effect_risk": "H"},
    ]


TASKS_OK = _tasks(case_id=CASE_ID, msg_id=ORIGINAL_MSG_ID, clearing=CLEARING_OK,
                  max_polls=MAX_POLLS_OK, file_id=TASK_FILE, classify_id=TASK_CLASSIFY,
                  cancel_id=TASK_CANCEL, observe_id=TASK_OBSERVE)

TASKS_STUCK = _tasks(case_id=CASE_ID_2, msg_id=ORIGINAL_MSG_ID_2,
                     clearing=CLEARING_STUCK, max_polls=MAX_POLLS_STUCK,
                     file_id=TASK_FILE_2, classify_id=TASK_CLASSIFY_2,
                     cancel_id=TASK_CANCEL_2, observe_id=TASK_OBSERVE_2)

#: 本场景**不走 ManagerAgent 出方案**：SCRIPT 是按关键字查表的，塞两份方案 JSON
#: 进去就得靠提示词里的关键字分派，那是拿确定性换省事。规格直接交给 create_plan ——
#: 控制面本来就收规格列表，Manager 只是规格的一种来源（口径同 scenario_7 第二段）。
SCRIPT = {
    "用户请求": json.dumps({"tasks": TASKS_OK}, ensure_ascii=False),
}

# ---- 编排层自己的 identity：补偿收口是**人的决定之后**的动作，不属于任何 Agent ----
# 与其给本域再加一个 Agent（那会让「补偿是谁做的」这个问题多一个含糊的答案），
# 不如让编排层带一个只有补偿权限的 identity —— 白名单机制正是用来表达这种最小授权的。
# 口径同 scenario_7 的 COMPENSATION_IDENTITY。
COMPENSATION_IDENTITY = AgentIdentity(
    agent_id="investigation-compensation-desk",
    role="investigation_compensation",
    duty="撤销走不通之后的域内补偿收口：留档最后观察、开人工对账工单、推进 compensated",
    allowed_skills=frozenset({"investigation.compensate"}),
    allowed_tools=frozenset(),
    write_scope=frozenset(),
    max_risk="M",
    model_tier=Tier.LIGHT,
)


# ---------------------------------------------------------------------- 靶场
def seed_domain(store) -> None:
    """预置两笔原始支付快照 —— 它们是**读到的那一版**，不是清算系统的当前值。"""
    objects.ensure_schema(store)
    for msg_id, e2e, amount in ((ORIGINAL_MSG_ID, END_TO_END_ID, AMOUNT),
                                (ORIGINAL_MSG_ID_2, END_TO_END_ID_2, AMOUNT_2)):
        objects.put_payment_snapshot(
            store, tenant_id=TENANT_ID, original_msg_id=msg_id,
            version=ORIGINAL_VERSION, end_to_end_id=e2e, interbank_amount=amount,
            currency=CURRENCY, value_date=VALUE_DATE, debtor_agent=DEBTOR_AGENT,
            creditor_agent=CREDITOR_AGENT, settlement_method="INDA",
            payload_json=json.dumps({"note": "原始 pacs.008 快照（演示靶场）"},
                                    ensure_ascii=False))


def resolution_artifact(store, task_id: str) -> dict:
    """取问询任务**最近一轮**的决议产物 —— 主管就是看着它做放行/驳回决定的。

    按 `version`（= 产出它的那次 attempt）取最大的一份，不取列表里的第一份：
    返工重跑之后这个任务会有多份产物，而主管处置的依据只能是**最后那一份**。
    """
    arts = [a for a in store.list_artifacts(task_id) if a["kind"] == KIND_RESOLUTION]
    if not arts:
        raise LookupError(f"{task_id} 没有决议产物 —— 问询任务应当产出 {KIND_RESOLUTION}")
    return max(arts, key=lambda a: a["version"])["content"]


def compensate(store, *, plan_id: str, task_id: str, trace_id: str, case_id: str,
               operator: str, reason: str) -> dict:
    """编排层以最小授权 identity 调 investigation.compensate。

    走 SkillInvoker 而不是直接 `InvestigationCompensateSkill().run()`：白名单校验与
    SkillInvoked 审计行都在 invoker 里，直接调就没有审计行，出事之后查不到是谁做的。
    """
    invoker = SkillInvoker(COMPENSATION_IDENTITY, store)
    res = invoker.invoke("investigation.compensate", {
        "tenant_id": TENANT_ID, "case_id": case_id,
        "operator": operator, "reason": reason, "assignee": operator,
    }, extras={"plan_id": plan_id, "task_id": task_id, "trace_id": trace_id})
    if res.status != "ok" or not isinstance(res.output, dict):
        raise RuntimeError(f"域内补偿失败，不许静默收口：{res.error}")
    return res.output


def _approve_adjustment(store, hq, *, case_id: str, task_id: str) -> None:
    """人工调账审批：**先落 `adjustment_approval`，再放行任务**。顺序不可换 ——
    `investigation.cancel` 会核对审批记录，没有它就拒绝发出 camt.056。

    审批记录由这里写而不是由某个 skill 写：审批是**人**的动作，
    让调账方自己写下「我被批准了」等于没有审批。
    """
    C.record_adjustment_approval(store, tenant_id=TENANT_ID, case_id=case_id,
                                 approver=APPROVER, decision="approved",
                                 reason=ADJUSTMENT_REASON)
    hq.decide(task_id, approved=True, operator=APPROVER, note=ADJUSTMENT_REASON)


def _print_observations(store, case_id: str, *, indent: str = "    ") -> list[dict]:
    """把这个案子的全部观察按问询顺序念出来 —— 本场景的核心证据。"""
    rows = guard.observations_of(store, TENANT_ID, case_id)
    for o in rows:
        code = o["confirmation_code"] or o["return_reason_code"] or "-"
        amt = "-" if o["returned_amount"] is None else f"{o['returned_amount']:.2f}"
        print(f"{indent}第{o['poll_seq']}次  {o['message_type']:16s} code={code:5s} "
              f"退回金额={amt:>10s}  -> {o['observed_state']}")
    return rows


# --------------------------------------------------------------- 顺利路径
def drive_success(*, matrix: bool = False) -> dict:
    """跑完顺利路径并返回收口用的句柄。**只跑不断言** —— 断言在 run() 里。

    拆出这一层是给 `maos/tests/test_investigation_flow.py` 用的：测试要对**库里的行**
    下断言，而 run() 只返回一个退出码，store 拿不出来。让测试自己再拼一遍流程
    则等于维护第二份场景，两边迟早漂。
    """
    print("场景 9：银行差错处理（顺利路径），无 key 确定性复现")

    model = select_model_client(SCRIPT, force_scripted=True)
    store, bus, cp, model, worker, gate = build(SCRIPT, matrix=matrix, model=model)
    seed_domain(store)

    # 清算方按名取：task.inputs 会被 json.dumps，实例塞不进去（见 _common.py 第 3 条）。
    # 剧本按 original_msg_id 注入，确定性回放，不依赖随机数。
    C.reset_clearing()
    C.register_clearing(CLEARING_OK, MockClearingHouse(
        resolve_after=RESOLVE_AFTER, script={ORIGINAL_MSG_ID: SCRIPT_RETURNED}))

    trace_id, plan_id = new_id("trace"), new_id("plan")
    cp.create_plan(goal=GOAL, trace_id=trace_id, plan_id=plan_id, tasks=TASKS_OK)
    cp.start_plan(plan_id)
    run_until_settled(bus, gate, cp, plan_id)

    hq = HumanApprovalQueue(store, cp)

    # —— 第一次人工介入：人工调账授权（监管硬闸）——
    pending = hq.pending(plan_id)
    assert [t["task_id"] for t in pending] == [TASK_CLASSIFY], (
        f"应停在定性的人工调账审批上，实际 {[t['task_id'] for t in pending]}")
    print(f"\n[1] 待人工调账审批: {pending[0]['title']}"
          f"（effect_risk=H —— 动别人的钱必须有人批，监管要求）")
    _approve_adjustment(store, hq, case_id=CASE_ID, task_id=TASK_CLASSIFY)
    print(f"    {APPROVER} 放行 → 已落 adjustment_approval，"
          f"investigation.cancel 这才读得到授权")
    run_until_settled(bus, gate, cp, plan_id)

    # —— 撤销请求已发出，决议已问出来 ——
    resolution = resolution_artifact(store, TASK_OBSERVE)
    print(f"\n[2] 已发出 camt.056，问询清算方 {resolution['poll_count']} 次，"
          f"逐次观察如下：")
    _print_observations(store, CASE_ID)
    print(f"\n    ↑ 第 2 次就问到了 CNCL（官方定义：清算方确认撤销成功）——"
          f"**系统那时一个状态都没推**")
    print(f"      因为 CNCL 证明的是「撤销指令照办了」，不是「钱回来了」；"
          f"资金证据只有 pacs.004 给得出")

    # —— 第二次人工介入：收口放行 ——
    pending = hq.pending(plan_id)
    assert [t["task_id"] for t in pending] == [TASK_OBSERVE], (
        f"问询任务应停在 BLOCKED 等人放行，实际 {[t['task_id'] for t in pending]}")
    print(f"\n[3] 待主管收口: {pending[0]['title']} —— "
          f"资金已退回={resolution['funds_returned']}，可放行")
    hq.decide(TASK_OBSERVE, approved=True, operator=APPROVER,
              note="已收到 pacs.004 退款报文，资金确认退回")
    run_until_settled(bus, gate, cp, plan_id)
    bus.drain()

    dump(cp, plan_id, "场景 9：银行差错处理（顺利路径）")
    return {"store": store, "cp": cp, "plan_id": plan_id, "trace_id": trace_id,
            "resolution": resolution, "hq": hq, "bus": bus, "gate": gate}


# --------------------------------------------------------------- 失败路径
def drive_failure(*, store, bus, cp, gate, hq) -> dict:
    """第二笔：清算方确认撤销，但退款报文永远不来。**本轨的核心产出。**

    与顺利路径共用同一套运行时（口径同 scenario_7 的 `drive_human_exit`）：
    另起一个 plan 而不新开一个场景号，避免动 `ALL_SCENARIOS` / `DEFAULT_SCENARIOS`
    —— 那会牵动 argparse choices、证据束数量、README 里写死的场景列表。

    只跑不断言，断言在 run() 里。
    """
    print(f"\n{'-' * 72}\n"
          f"第二笔差错：清算方确认撤销（CNCL），但退款报文（pacs.004）永远不来\n"
          f"{'-' * 72}")

    # 第三个清算方实例。同一个 MockClearingHouse 类，行为一行没改 —— 换的是剧本。
    C.register_clearing(CLEARING_STUCK, MockClearingHouse(
        resolve_after=RESOLVE_AFTER,
        script={ORIGINAL_MSG_ID_2: SCRIPT_CONFIRMED_ONLY}))

    trace_id = new_id("trace")
    plan_id = cp.create_plan(goal=GOAL_2, trace_id=trace_id, tasks=TASKS_STUCK)
    cp.start_plan(plan_id)
    run_until_settled(bus, gate, cp, plan_id)

    # —— 人工调账授权：与顺利路径同一道门，一个字没改 ——
    pending = hq.pending(plan_id)
    assert [t["task_id"] for t in pending] == [TASK_CLASSIFY_2], (
        f"第二笔也应先停在人工调账审批上，实际 {[t['task_id'] for t in pending]}")
    print(f"\n[4] 待人工调账审批: {pending[0]['title']}（既有的 effect_risk=H 入口，未改动）")
    _approve_adjustment(store, hq, case_id=CASE_ID_2, task_id=TASK_CLASSIFY_2)
    run_until_settled(bus, gate, cp, plan_id)

    # —— 问询到顶：清算方一直说「撤销成功」，钱就是不来 ——
    resolution = resolution_artifact(store, TASK_OBSERVE_2)
    print(f"\n[5] 问询清算方 {resolution['poll_count']} 次（上限 {MAX_POLLS_STUCK}），"
          f"逐次观察如下：")
    observations = _print_observations(store, CASE_ID_2)
    receipt = resolution["receipt"]
    print(f"\n    最后一次: {receipt['message_type']} "
          f"confirmation_code={receipt['confirmation_code']}")
    print(f"    官方定义: {resolution['definition']}")
    print(f"    出处    : {resolution['source']}")
    print(f"    撤销请求有结论了吗 request_resolved={resolution['request_resolved']}")
    print(f"    钱回来了吗         funds_returned  ={resolution['funds_returned']}"
          f"   ← **业务成功与否看这个**")

    # —— 四个 Agent 全回 ok，而业务确实没成 ——
    agent_states = {t["task_id"]: t["state"] for t in cp.store.list_tasks(plan_id)}
    print(f"\n[6] 四个 Agent 的任务状态: "
          f"{ {k.replace('task-s9b-', ''): v for k, v in agent_states.items()} }")
    print(f"    没有一个 Agent 报 failed —— 它们都如实完成了自己那一步。"
          f"业务没成功这件事，不在任何一个 Agent 的返回值里")

    # —— 人工介入：主管拿着一份写着「撤销成功」的产物，仍然必须驳回 ——
    pending = hq.pending(plan_id)
    assert [t["task_id"] for t in pending] == [TASK_OBSERVE_2], (
        f"问询任务应停在 BLOCKED 等人处置，实际 {[t['task_id'] for t in pending]}")
    print(f"\n[7] 待主管处置: {pending[0]['title']}")
    print(f"    主管看到的产物写着「清算方已确认撤销」，但 funds_returned=False —— "
          f"不能当成功放行")

    # 先补偿、再落 FAILED —— 与 control_plane.human_decision 同一个顺序与同一个理由：
    # 状态一旦落 FAILED，「外面还有一份下落不明的 camt.056」这件事就没人记得了。
    comp = compensate(store, plan_id=plan_id, task_id=TASK_OBSERVE_2, trace_id=trace_id,
                      case_id=CASE_ID_2, operator=APPROVER, reason=REJECT_REASON)
    print(f"\n[8] 域内补偿: 撤回 {len(comp['withdrawn'])} 份请求，"
          f"最后观察到的下落 = {comp['last_observed_state']}")
    print(f"    （**不是 rejected** —— 清算方明明确认撤销了，"
          f"写成被拒会让对账的人从错误方向查起）")
    print(f"    人工对账工单: {comp['ticket']['ticket_id']} -> {comp['ticket']['assignee']}")
    for line in comp["ticket"]["todo"][:2]:
        print(f"      · {line}")

    hq.decide(TASK_OBSERVE_2, approved=False, operator=APPROVER, note=REJECT_REASON)
    bus.drain()

    dump(cp, plan_id, "场景 9：银行差错处理（失败路径）")
    return {"plan_id": plan_id, "trace_id": trace_id, "resolution": resolution,
            "compensation": comp, "observations": observations,
            "agent_states": agent_states}


# -------------------------------------------------------------------------- run
def run(*, matrix: bool = False) -> int:
    ok = drive_success(matrix=matrix)
    store, cp = ok["store"], ok["cp"]

    # ============================================================ 顺利路径收口
    case = guard.get_case(store, TENANT_ID, CASE_ID)
    obs_ok = guard.observations_of(store, TENANT_ID, CASE_ID)
    returned_rows = guard.observations_of(store, TENANT_ID, CASE_ID,
                                          observed_state=guard.OBS_RETURNED)
    plan_ok = cp.store.get_plan(ok["plan_id"])

    print(f"\n  业务状态    : {case['biz_status']}")
    print(f"  观察共      : {len(obs_ok)} 条"
          f"（{[o['observed_state'] for o in obs_ok]}）")
    print(f"  returned 观察: {len(returned_rows)} 条，来自 "
          f"{[o['message_type'] for o in returned_rows]}")
    print(f"  Plan 终态   : {plan_ok['state']}")

    assert case["biz_status"] == "returned", (
        f"顺利路径应收口到 returned，实际 {case['biz_status']}")
    assert len(returned_rows) == 1, (
        f"应恰好一条 returned 观察，实际 {len(returned_rows)} 条")
    # 本场景第一断言：写 returned 的那条观察**必须**来自 pacs.004。
    assert returned_rows[0]["message_type"].startswith("pacs.004"), (
        f"returned 只能凭 pacs.004 写入，实际来自 "
        f"{returned_rows[0]['message_type']} —— 确认撤销不等于资金已退回")
    assert returned_rows[0]["return_reason_code"], "pacs.004 观察必须带退回原因码"
    assert returned_rows[0]["returned_amount"] is not None, "pacs.004 观察必须带退回金额"

    # —— 中间那句肯定答复真的出现过，且当时什么都没写 ——
    confirmed = [o for o in obs_ok
                 if o["observed_state"] == guard.OBS_CANCELLATION_CONFIRMED]
    assert confirmed, (
        "顺利路径也必须经过一次 CNCL —— 没有它，「肯定答复不算资金证据」这条"
        "判据在本场景里一次都没被触发过，等于没演")
    assert all(o["confirmation_code"] == "CNCL" for o in confirmed)
    assert all(o["returned_amount"] is None for o in confirmed), (
        "camt.029 观察不许带退回金额 —— 带了就让「有金额」不再是资金证据的标志")
    assert plan_ok["state"] == PlanState.DONE, (
        f"顺利路径 Plan 应收敛到 DONE，实际 {plan_ok['state']}")

    # ============================================================ 失败路径
    # **位置不能挪到上面去**：上面几条断言按 case 收窄，但下面要数的
    # 「全库 returned 观察」是全表口径，第二笔一旦先跑就不再是第一笔那条链路了。
    bad = drive_failure(store=store, bus=ok["bus"], cp=cp, gate=ok["gate"], hq=ok["hq"])
    case2 = guard.get_case(store, TENANT_ID, CASE_ID_2)
    obs2 = bad["observations"]
    returned2 = guard.observations_of(store, TENANT_ID, CASE_ID_2,
                                      observed_state=guard.OBS_RETURNED)
    comp_rows = objects.query(
        store, "SELECT * FROM investigation_compensation WHERE tenant_id=? AND case_id=?"
               " ORDER BY kind", (TENANT_ID, CASE_ID_2))
    comp_events = [e for e in cp.store.list_event_log(bad["plan_id"])
                   if e["event_type"] == "CompensationExecuted"]
    plan2 = cp.store.get_plan(bad["plan_id"])

    print(f"\n  业务状态  : {case2['biz_status']}（全程没有经过 returned）")
    print(f"  returned 观察: {len(returned2)} 条 —— 没问出资金证据就一条都不该有")
    print(f"  真实观察  : {len(obs2)} 条 "
          f"{ {s: [o['observed_state'] for o in obs2].count(s) for s in sorted({o['observed_state'] for o in obs2})} }")
    print(f"  补偿记录  : {len(comp_rows)} 行 {[r['kind'] for r in comp_rows]}")
    print(f"  补偿事件  : {len(comp_events)} 条 CompensationExecuted")
    print(f"  Plan 终态 : {plan2['state']}（主管驳回，业务确实没成功）")

    # —— 本场景存在的理由，第一断言 ——
    assert case2["biz_status"] == "compensated", (
        f"补偿之后业务状态应为 compensated，实际 {case2['biz_status']}")
    assert len(returned2) == 0, (
        f"失败路径不该有任何 returned 观察，实际 {len(returned2)} 条 —— "
        "有就说明有人在没拿到 pacs.004 的情况下把资金结论写死了")

    # —— 「Agent 都说完成了」不等于业务成功。本场景第二断言 ——
    #
    # `agent_states` 是在**人做决定之前**那一刻取的快照，这是本条判据唯一成立的时刻：
    # 那时四个 Agent 都跑完了、一个都没失败，而钱一分没回来。取在人决定之后就变成了
    # 「主管驳回所以任务 FAILED」—— 那证明的是人的判断力，不是系统的诚实。
    states = bad["agent_states"]
    assert set(states.values()) <= {TaskState.DONE, TaskState.BLOCKED}, (
        f"人做决定之前，四个 Agent 的任务只该是 DONE 或（等人的）BLOCKED，实际 {states}")
    assert TaskState.FAILED not in states.values(), (
        f"人做决定之前不该有任何 Agent 失败，实际 {states} —— "
        "本场景要演的正是「四个 Agent 全回 ok 而业务没成」")
    assert states[TASK_OBSERVE_2] == TaskState.BLOCKED, (
        f"收口任务应停在 BLOCKED 等人处置，实际 {states[TASK_OBSERVE_2]}")
    assert [s for t, s in states.items() if t != TASK_OBSERVE_2] == [TaskState.DONE] * 3, (
        f"前三个 Agent 应全部 DONE（它们如实完成了各自那一步），实际 {states}")
    assert bad["resolution"]["funds_returned"] is False
    assert bad["resolution"]["request_resolved"] is True, (
        "清算方确实给了结论（CNCL）—— 请求有结论、资金没回来，正是本域的题眼")

    # —— 真实观察一条不少，而且**不是**伪造的失败 ——
    assert len(obs2) == MAX_POLLS_STUCK, (
        f"问了 {MAX_POLLS_STUCK} 次就该有 {MAX_POLLS_STUCK} 条观察，实际 {len(obs2)}")
    assert {o["observed_state"] for o in obs2} == {
        guard.OBS_PENDING, guard.OBS_CANCELLATION_CONFIRMED}, (
        f"失败路径的观察只该是 pending 与 cancellation_confirmed，"
        f"实际 {sorted({o['observed_state'] for o in obs2})} —— "
        "出现 rejected 说明有人把「我问累了」写成了「清算方说不行」")

    # —— 补偿真发生过，且没有替清算方下结论 ——
    kinds = {r["kind"] for r in comp_rows}
    assert KIND_CANCELLATION_WITHDRAWN in kinds and KIND_MANUAL_TICKET in kinds, (
        f"补偿必须同时留下撤回记录与人工对账工单，实际 {sorted(kinds)}")
    assert comp_events, "补偿执行必须落 CompensationExecuted，否则这件事只活在日志里"
    assert bad["compensation"]["last_observed_state"] == guard.OBS_CANCELLATION_CONFIRMED, (
        f"最后观察应为 cancellation_confirmed，实际 "
        f"{bad['compensation']['last_observed_state']} —— "
        "写成 rejected 就是替清算方下了它没下的结论")

    # —— 铁律 9：业务状态不进 Task 状态机，也没有为本域新开一条迁移 ——
    # 断言两件事而不是一件：只查状态集合挡不住「用既有的两个状态连一条新边」。
    known_states = {v for k, v in vars(TaskState).items()
                    if not k.startswith("_") and isinstance(v, str)}
    for pid in (ok["plan_id"], bad["plan_id"]):
        task_states = {t["state"] for t in cp.store.list_tasks(pid)}
        assert task_states <= known_states, (
            f"出现了不在既有 Task 状态机内的状态：{sorted(task_states - known_states)}")
        assert not ({"returned", "compensated", "cancellation_sent"} & task_states), (
            "returned / compensated / cancellation_sent 都是 investigation_case "
            "自己的字段，不许变成 Task 状态（铁律 9）")
        moves = {(e["from_state"], e["to_state"])
                 for e in cp.store.list_event_log(pid)
                 if e["event_type"] == "StateTransition"}
        assert moves <= set(TASK_TRANSITIONS), (
            f"出现了不在冻结迁移表里的 Task 迁移：{sorted(moves - set(TASK_TRANSITIONS))}")

    assert plan2["state"] == PlanState.FAILED, (
        f"主管驳回后 Plan 应收敛到 FAILED，实际 {plan2['state']}")

    # —— 撤销请求发出去的份数：幂等键由 (tenant, case) 定，一个案子恒一份 ——
    for case_id in (CASE_ID, CASE_ID_2):
        n = objects.query(
            store, "SELECT COUNT(*) AS n FROM cancellation_request"
                   " WHERE tenant_id=? AND case_id=?", (TENANT_ID, case_id))[0]["n"]
        assert n == 1, (
            f"case={case_id} 应恰好发出 1 份 camt.056，实际 {n} 份 —— "
            "第二份会让清算方开出第二个 case，而资金只有一笔")

    print(f"\n{'=' * 72}")
    print("场景 9 收口：两条路径共用同一句 CNCL，一条拿到 pacs.004 收口 returned，")
    print("            另一条拿不到就一个字都不写 —— 差别不在 Agent 说了什么，")
    print("            在于外部权威到底给没给出资金证据。")
    print(f"{'=' * 72}")
    return 0


if __name__ == "__main__":                              # pragma: no cover
    raise SystemExit(run())
