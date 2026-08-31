"""场景 10：应付账款三单匹配与付款 —— 顺利路径 + 失败路径，一个文件两条。

    顺利路径 drive_happy()
      收票（三单齐备）→ 三单匹配（数量/单价/税额各自容差，差异在容差内）
        → 出付款计划 → effect_risk=H 停 BLOCKED 等人批 → 主管放行
        → ap.execute 发指令（受理回单，**非终态**）
        → ap.observe 问到第 2 次拿到带流水号的回单 → **这时才**写 settled
        → Plan DONE

    失败路径 drive_failure()
      同样收票、匹配通过、主管放行、指令发出去
        → 银行永远问不出终态（MockBank(settle_after=99)，轮询上限 3）
        → **一个字都不写**：不推状态、不落观察行
        → 付款任务 effect_risk=H，Gate 过了也停在 BLOCKED 等人处置
        → 主管驳回：回单问不出来，转人工对账
        → ap.compensate：作废付款指令 + 开对账工单 + biz_status -> compensated
        → Plan FAILED
        → ap_case.biz_status = 'compensated'，**从未进入 settled**

## 失败路径要证明的那句话

    「所有 Agent 都回复完成」没有发生，因为业务确实没成功，系统如实记录了这一点。

失败路径上**四个 Agent 全部 `status=ok`** —— 收票 ok、匹配 ok、出计划 ok、
付款+观察 ok。四个都跑完了、都产出了产物、都没抛异常。而这笔货款到底付没付
出去，系统的回答是「不知道」，`biz_status` 停在 `compensated`，
`ap_payment_observation` 里 settled 观察 **0 条**。

`AgentOutput.status` 说的是「这一步跑完了没有」，不是「业务成功了没有」。
本场景的收口断言就钉在这个差上（见 `run()` 里 `assert all_ok and not settled`）。

## 三处刻意不「顺手兜底」的地方，每一处都是铁律 8 的落点

1. **轮询到顶不改判成失败**。`MockBank(settle_after=99)` 保证 3 次 query 一定问不
   出终态。此时 `ap.observe` 一行观察都不写、一个状态都不推 —— 「我问累了」和
   「银行说没付成」是两回事。于是 `ap_payment_observation` 表是**空的**，
   而不是躺着一行伪造的 failed。

2. **补偿不宣布那笔钱没付出去**。回单问不出来意味着那一笔**可能已经划走了**。
   所以 `ap.compensate` 落的是「MAOS 侧不再推进 + 最后观察到的下落 + 对账工单」，
   `last_observed_state` 是 `unobserved` 而不是 `failed`。真付过的话，写成 failed
   会让账面上凭空少一笔，而供应商那边收到了钱。

3. **业务状态不进 Task 状态机**（铁律 9）。`compensated` 是 `ap_case` 自己的字段，
   `contracts/states.py` 一个新状态、一条新迁移都没加 —— 本场景的收口断言之一
   就是全部 Task 迁移仍落在既有迁移表内。

## 无 key 可跑

`select_model_client(SCRIPT, force_scripted=True)`：配了 key 的机器上也一行网络都
不走。金额、单价、数量、容差、轮询次数全部写死，连跑两次输出逐条一致。

## 本场景**不在** `maos/main.py` 的 `DEFAULT_SCENARIOS` 里

那是有意的：同期三条轨都新增 flow，谁改 `main.py` 谁就和另两轨冲突。
接进缺省序列是整合轮的事。本场景由 `maos/tests/test_ap_flow.py` 调用。
"""

from __future__ import annotations

import json

from maos.agents.ap import ROLE_CONTROL, ROLE_INTAKE, ROLE_MATCH, ROLE_TREASURY
from maos.agents.base import AgentIdentity
from maos.contracts.events import new_id
from maos.contracts.states import TASK_TRANSITIONS, PlanState, TaskState
from maos.domain.ap import fixtures, guard, objects
from maos.flows.common import build, dump, run_until_settled
from maos.model.client import Tier, select_model_client
from maos.runtime.gate import HumanApprovalQueue
from maos.skills.builtin.ap import _common as C
from maos.skills.builtin.ap.compensate import (
    KIND_INSTRUCTION_REVOKED,
    KIND_RECONCILIATION_TICKET,
    UNOBSERVED,
)
from maos.skills.invoker import SkillInvoker
from maos.tools import ap_codes
from maos.tools.ap import ADVICE_FIELD, MockBank

# ---------------------------------------------------------------------- 常量
# 全部写死，不用 new_id：验收之一是「连跑两次输出逐条一致」，而 dump() 会打印
# task_id。随机 id 会让两次输出必然不同。
TENANT_ID = "tnt-mfg-ap"
SUPPLIER_ID = "SUP-4471"
SUPPLIER_NAME = "华东紧固件"

#: 顺利路径的三单。
CASE_OK = "case-ap-0001"
PO_OK = "PO-2026-0731"
GR_OK = "GR-2026-0812"
INVOICE_OK = "INV-2026-0819"

#: 失败路径的三单。**另起一套单号**：顺利路径那张发票已经付掉了（settled），
#: 在它上面再发一次付款不是演示，是在演一个不该发生的动作。
CASE_BAD = "case-ap-0002"
PO_BAD = "PO-2026-0805"
GR_BAD = "GR-2026-0818"
INVOICE_BAD = "INV-2026-0826"

#: 银行名。两条路径各一个实例，只有 `settle_after` 不同 ——
#: 同一个 MockBank 类，行为一行没改。
BANK_OK = "s10-clearing"
BANK_SLOW = "s10-stuck"

#: 顺利路径：问 2 次拿到终态；上限 5，留出余量。
SETTLE_AFTER_OK = 2
MAX_POLLS_OK = 5

#: 失败路径：银行要 99 次才给终态，而上限只有 3 —— 保证**一定**问不出来。
#: 两个数写死是本场景确定性的来源：poll_count 恒为 3。
SETTLE_AFTER_STUCK = 99
MAX_POLLS_STUCK = 3

#: 发票类型码与税种码。从码表常量取，不写字面量 ——
#: 这两个值会被 ap.match 拿去验 BR-CL-01 / BR-CL-17。
INVOICE_TYPE = ap_codes.CODE_COMMERCIAL_INVOICE
TAX_CATEGORY = ap_codes.CODE_TAX_STANDARD
TAX_RATE = 13.0
PAYMENT_MEANS = ap_codes.CODE_CREDIT_TRANSFER

#: 三单的行。**订单单价与发票单价刻意不完全相同**，差 0.01 元 ——
#: 演的正是「差异在容差内」这一档：单价容差 0.01，差 0.01 不超容差，匹配通过。
#: 数量同理：收货合格数比订单少 0.4 件（散装计量误差），数量容差 0.5，也在容差内。
#: 全都写成整齐相等的话，「容差」这三个字在场景里就没有演出来。
#
#  line_no, sku,             订单数, 订单单价, 收货到货, 收货不合格, 发票数, 发票单价
LINES_OK = [
    (1, "SKU-BOLT-M8",   200.0, "12.50", 200.0, 0.0, 200.0, "12.51"),
    (2, "SKU-NUT-M8",    200.0, "3.20",  200.4, 0.0, 200.0, "3.20"),
    (3, "SKU-WASHER-M8", 400.0, "0.85",  400.0, 0.0, 400.0, "0.85"),
]

#: 失败路径的三单**完全对得上** —— 本场景的失败不在匹配，在银行回单问不出来。
#: 刻意让匹配通过：要证明的是「四个 Agent 全回 ok 而业务没成」，
#: 匹配就挂掉的话只演出了「有一步失败了」，那是另一件事。
LINES_BAD = [
    (1, "SKU-SEAL-32", 60.0, "48.00", 60.0, 0.0, 60.0, "48.00"),
    (2, "SKU-RING-32", 60.0, "9.50",  60.0, 0.0, 60.0, "9.50"),
]

TASK_INTAKE = "task-s10-intake"
TASK_MATCH = "task-s10-match"
TASK_PLAN = "task-s10-plan"
TASK_PAY = "task-s10-pay"

TASK_INTAKE_B = "task-s10b-intake"
TASK_MATCH_B = "task-s10b-match"
TASK_PLAN_B = "task-s10b-plan"
TASK_PAY_B = "task-s10b-pay"

APPROVER = "@boss-ap:maos.local"
APPROVE_REASON = f"金额与三单一致，依据 {ap_codes.RULE_AMOUNT_DUE}"
REJECT_REASON = "银行回单问不出来，转人工对账"

GOAL_OK = (f"支付华东紧固件 2026-08 月结货款：三单匹配后按 "
           f"{ap_codes.RULE_AMOUNT_DUE} 出账")
GOAL_BAD = "支付华东紧固件密封件货款：三单无异议，但银行回单迟迟问不出来"

#: 本域不出方案，DAG 直接交给 `create_plan` —— 控制面本来就收规格列表，
#: Manager 只是规格的一种来源。ScriptedModelClient 仍要给一份脚本：
#: Reviewer 的语义审查会问模型。
REVIEW_JSON = json.dumps({
    "defects": [],
    "conclusion": f"金额按 {ap_codes.RULE_AMOUNT_DUE} 从三单算出，付款方式取 "
                  f"{ap_codes.LIST_PAYMENT_MEANS} 的 {PAYMENT_MEANS}；"
                  f"款项尚未划出，可放行",
}, ensure_ascii=False)

SCRIPT = {"语义审查产物清单": REVIEW_JSON}

# ---- 编排层自己的 identity：补偿收口是**人的决定之后**的动作 ------------------
# 与其给本域再加第五个 Agent（那会让「补偿是谁做的」这个问题多一个含糊的答案），
# 不如让编排层带一个只有补偿权限的 identity —— 白名单机制正是用来表达这种最小
# 授权的。口径同 scenario_7 的 COMPENSATION_IDENTITY。
COMPENSATION_IDENTITY = AgentIdentity(
    agent_id="ap-compensation-desk",
    role="ap_compensation",
    duty="付款走不通之后的域内补偿收口：作废付款指令、开对账工单、推进 compensated",
    allowed_skills=frozenset({"ap.compensate"}),
    allowed_tools=frozenset(),
    write_scope=frozenset(),
    max_risk="M",
    model_tier=Tier.LIGHT,
)


# ---------------------------------------------------------------------- 靶场数据
def seed_supplier(store) -> None:
    """供应商主数据。付款方式码取 UNCL4461，会被 ap.plan-payment 验 BR-CL-16。"""
    fixtures.seed_supplier(
        store, tenant_id=TENANT_ID, supplier_id=SUPPLIER_ID, name=SUPPLIER_NAME,
        payment_means_code=PAYMENT_MEANS, payment_terms="月结 30 天",
        bank_account="62220000****4471")


def seed_three_way(store, *, po_id: str, gr_id: str, invoice_id: str,
                   lines: list, issued_at: str, due_at: str) -> dict:
    """落一套三单 —— 它们是**外部系统里读到的那一版**，不是外部系统的当前值。

    构造走 `maos/domain/ap/fixtures.py`，本场景不另写一份：测试也从那里落数据，
    留第二条构造路径两条一定会漂（口径同 `flows/common.py` 抬头）。
    """
    return fixtures.seed_three_way(
        store, tenant_id=TENANT_ID, supplier_id=SUPPLIER_ID, po_id=po_id, gr_id=gr_id,
        invoice_id=invoice_id, lines=lines, tax_category=TAX_CATEGORY,
        tax_rate=TAX_RATE, invoice_type=INVOICE_TYPE, issued_at=issued_at,
        due_at=due_at)


def _tasks(*, case_id: str, po_id: str, gr_id: str, invoice_id: str, bank: str,
           max_polls: int, ids: tuple[str, str, str, str]) -> list[dict]:
    """一条路径的四任务 DAG。两条路径同构，只换案子与银行。

    `biz_type` 用本域自己的标记（`C.BIZ_TYPE == "ap"`），**不是 "refund"** ——
    第六道财务复核闸按 `biz_type == "refund"` 触发，冒用会让闸恒 blocker，
    而报错信息指向退款域的财务复核（见 `_common.py` 的 BIZ_TYPE 注释）。
    """
    t_intake, t_match, t_plan, t_pay = ids
    base = {"biz_type": C.BIZ_TYPE, "tenant_id": TENANT_ID, "case_id": case_id}
    return [
        {"task_id": t_intake, "role": ROLE_INTAKE, "title": "收供应商发票并确认三单齐备",
         "inputs": {**base, "invoice_id": invoice_id, "po_id": po_id, "po_version": 1,
                    "gr_id": gr_id},
         "acceptance": ["三单齐备", "建出 ap_case 且 biz_status=received"],
         "depends_on": [], "risk_level": "L"},

        {"task_id": t_match, "role": ROLE_MATCH, "title": "三单匹配：数量、单价、税额各自容差",
         "inputs": {**base},
         "acceptance": ["逐行比数量与单价", "拒付理由必须挂真实规则编号"],
         "depends_on": [t_intake], "risk_level": "M"},

        # effect_risk=H 挂在**付款计划**上：产物一旦被批准，下一步就是把钱打出去。
        # Gate 过了也停在 BLOCKED 等人放行 —— 这就是 §5.4 房间审批停的那一步。
        {"task_id": t_plan, "role": ROLE_CONTROL, "title": "出付款计划，交主管审批",
         "inputs": {**base},
         "acceptance": ["金额取匹配算出的那个，不取发票自称的", "依据挂规范编号"],
         "depends_on": [t_match], "risk_level": "L", "effect_risk": "H"},

        # 付款任务同样 effect_risk=H：把钱打出去是不可逆动作，Gate 过了也要人放行。
        # 失败路径的转折点就在这里 —— 主管拿到的不是「已付」，是一份问不出终态的
        # 回单，于是他驳回。**没有这一步，pending 会被当成「还在清算」一路挂着**。
        {"task_id": t_pay, "role": ROLE_TREASURY, "title": "发出付款指令并观察银行回单",
         "inputs": {**base, "bank": bank, "max_polls": max_polls},
         "acceptance": ["发出后不得写 settled", "终态必须由 bank.query 观察得到"],
         "depends_on": [t_plan], "risk_level": "M", "effect_risk": "H"},
    ]


def _count(store, sql: str, params: tuple = ()) -> int:
    return objects.query(store, sql, params)[0]["n"]


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
               operator: str, reason: str) -> dict:
    """编排层以最小授权 identity 调 ap.compensate。

    走 SkillInvoker 而不是直接 `ApCompensateSkill().run()`：白名单校验与
    SkillInvoked 审计行都在 invoker 里，直接调就没有审计行，出事之后查不到是谁做的。
    """
    invoker = SkillInvoker(COMPENSATION_IDENTITY, store)
    res = invoker.invoke("ap.compensate", {
        "tenant_id": TENANT_ID, "case_id": case_id,
        "operator": operator, "reason": reason, "assignee": operator,
    }, extras={"plan_id": plan_id, "task_id": task_id, "trace_id": trace_id})
    if res.status != "ok" or not isinstance(res.output, dict):
        raise RuntimeError(f"域内补偿失败，不许静默收口：{res.error}")
    return res.output


# ------------------------------------------------------------------ 顺利路径
def drive_happy(*, matrix: bool = False) -> dict:
    """跑完顺利路径并返回收口用的句柄。**只跑不断言** —— 断言在 run() 里。

    拆出这一层是给 `maos/tests/test_ap_flow.py` 用的：测试要对**库里的行**下断言，
    而 run() 只返回一个退出码，store 拿不出来。让测试自己再拼一遍流程则等于维护
    第二份场景，两边迟早漂。
    """
    print("场景 10（顺利路径）：应付账款三单匹配与付款 —— 银行给了流水号才算付掉")

    model = select_model_client(SCRIPT, force_scripted=True)
    store, bus, cp, model, worker, gate = build(SCRIPT, matrix=matrix, model=model)
    seed_supplier(store)
    totals = seed_three_way(store, po_id=PO_OK, gr_id=GR_OK, invoice_id=INVOICE_OK,
                            lines=LINES_OK, issued_at="2026-08-19T00:00:00+00:00",
                            due_at="2026-09-18")

    # 银行按名取：task.inputs 会被 json.dumps，实例塞不进去（见 _common.py 第 3 条）。
    C.reset_banks()
    C.register_bank(BANK_OK, MockBank(settle_after=SETTLE_AFTER_OK))

    trace_id, plan_id = new_id("trace"), new_id("plan")
    cp.create_plan(goal=GOAL_OK, trace_id=trace_id, plan_id=plan_id,
                   tasks=_tasks(case_id=CASE_OK, po_id=PO_OK, gr_id=GR_OK,
                                invoice_id=INVOICE_OK, bank=BANK_OK,
                                max_polls=MAX_POLLS_OK,
                                ids=(TASK_INTAKE, TASK_MATCH, TASK_PLAN, TASK_PAY)))
    cp.start_plan(plan_id)
    run_until_settled(bus, gate, cp, plan_id)

    match = artifact_of(store, TASK_MATCH, "ap_match_result")
    print(f"\n[1] 三单匹配: {'通过' if match['matched'] else '未通过'}"
          f"（跑了 {len(match['checked'])} 条判据）")
    print(f"    容差    : 数量 {match['tolerance']['quantity']} 件 / "
          f"单价 {match['tolerance']['unit_price']} 元 / "
          f"税额 {match['tolerance']['tax']} 元 —— 三个量纲不同，不合并成一个")
    print(f"    差异    : 第 1 行单价差 0.01（容差内）、第 2 行收货多 0.4 件（容差内）")
    print(f"    应付    : {match['payable_amount']}（按 "
          f"{ap_codes.RULE_AMOUNT_DUE} 算出，不取发票自称的 {totals['amount_due']}）")

    hq = HumanApprovalQueue(store, cp)

    # —— 人工介入：付款计划要人批 ——
    pending = hq.pending(plan_id)
    assert [t["task_id"] for t in pending] == [TASK_PLAN], (
        f"应停在付款计划的人工审批上，实际 {[t['task_id'] for t in pending]}")
    plan_art = artifact_of(store, TASK_PLAN, "ap_payment_plan")
    print(f"\n[2] 待主管审批: {pending[0]['title']}（effect_risk="
          f"{pending[0]['effect_risk']}，出账不可逆）")
    print(f"    付款计划: {plan_art['plan']['amount']} {plan_art['plan']['currency']} "
          f"-> {plan_art['plan']['supplier_name']}，方式 "
          f"{plan_art['plan']['payment_means_code']} "
          f"{plan_art['plan']['payment_means_name']}")
    print(f"    依据    : {[c['rule_id'] for c in plan_art['citations']]}")

    # 审批是**人**的动作：先落 payment_approval，再放行任务。顺序不可换 ——
    # ap.execute 会核对审批记录，没有它就拒绝发起付款。
    C.record_approval(store, tenant_id=TENANT_ID, case_id=CASE_OK, approver=APPROVER,
                      decision="approved", reason=APPROVE_REASON)
    hq.decide(TASK_PLAN, approved=True, operator=APPROVER, note=APPROVE_REASON)
    run_until_settled(bus, gate, cp, plan_id)

    advice = artifact_of(store, TASK_PAY, "ap_bank_advice")
    instruction = artifact_of(store, TASK_PAY, "ap_payment_instruction")
    print(f"\n[3] 付款指令: {instruction['amount']} {instruction['currency']}，"
          f"受理回单 {instruction[ADVICE_FIELD]['status']}（**非终态**）")
    print(f"    幂等键  : {instruction['idempotency_key']} —— 由 (租户, 案子) 唯一确定，"
          f"一张发票只允许有一笔付款")
    print(f"\n[4] 银行回单: {advice['observed_state']}（问了 {advice['poll_count']} 次）")
    print(f"    流水号  : {advice['bank_reference']}  起息 {advice['value_date']}")
    print(f"    —— settled 是**问出来的**：把银行换成一步返回 settled 的桩，"
          f"ap.observe 就没有存在理由了")

    # 付款任务同样 effect_risk=H，钱已经确认到账了，主管确认收口。
    pending = hq.pending(plan_id)
    if pending:
        assert [t["task_id"] for t in pending] == [TASK_PAY], (
            f"此时只该剩付款任务等确认，实际 {[t['task_id'] for t in pending]}")
        hq.decide(TASK_PAY, approved=True, operator=APPROVER,
                  note=f"银行流水 {advice['bank_reference']} 已确认")
        run_until_settled(bus, gate, cp, plan_id)

    dump(cp, plan_id, "场景 10 顺利路径：应付账款三单匹配与付款")
    return {"store": store, "cp": cp, "bus": bus, "gate": gate, "hq": hq,
            "plan_id": plan_id, "trace_id": trace_id, "match": match,
            "plan": plan_art, "advice": advice, "instruction": instruction,
            "totals": totals}


# ------------------------------------------------------------------ 失败路径
def drive_failure(*, matrix: bool = False) -> dict:
    """跑完失败路径并返回收口用的句柄。**只跑不断言**。

    另起一套运行时（不复用顺利路径那个）：收口断言里有全库口径的
    「settled 观察 0 条」，两条路径共库的话它校验的就不再是本条链路了。
    """
    print("\n场景 10（失败路径）：三单无异议、四个 Agent 全回 ok —— 而这笔钱付没付出去，"
          "系统说不知道")

    model = select_model_client(SCRIPT, force_scripted=True)
    store, bus, cp, model, worker, gate = build(SCRIPT, matrix=matrix, model=model)
    seed_supplier(store)
    seed_three_way(store, po_id=PO_BAD, gr_id=GR_BAD, invoice_id=INVOICE_BAD,
                   lines=LINES_BAD, issued_at="2026-08-26T00:00:00+00:00",
                   due_at="2026-09-25")

    # 银行要 99 次才给终态，而轮询上限只有 3 —— 保证**一定**问不出终态。
    C.reset_banks()
    C.register_bank(BANK_SLOW, MockBank(settle_after=SETTLE_AFTER_STUCK))

    trace_id, plan_id = new_id("trace"), new_id("plan")
    cp.create_plan(goal=GOAL_BAD, trace_id=trace_id, plan_id=plan_id,
                   tasks=_tasks(case_id=CASE_BAD, po_id=PO_BAD, gr_id=GR_BAD,
                                invoice_id=INVOICE_BAD, bank=BANK_SLOW,
                                max_polls=MAX_POLLS_STUCK,
                                ids=(TASK_INTAKE_B, TASK_MATCH_B, TASK_PLAN_B,
                                     TASK_PAY_B)))
    cp.start_plan(plan_id)
    run_until_settled(bus, gate, cp, plan_id)

    match = artifact_of(store, TASK_MATCH_B, "ap_match_result")
    print(f"\n[1] 三单匹配: {'通过' if match['matched'] else '未通过'} —— "
          f"应付 {match['payable_amount']}。**本场景的失败不在匹配**")

    hq = HumanApprovalQueue(store, cp)
    pending = hq.pending(plan_id)
    assert [t["task_id"] for t in pending] == [TASK_PLAN_B], (
        f"应先停在付款计划的人工审批上，实际 {[t['task_id'] for t in pending]}")
    print(f"\n[2] 待主管审批: {pending[0]['title']}")
    C.record_approval(store, tenant_id=TENANT_ID, case_id=CASE_BAD, approver=APPROVER,
                      decision="approved", reason=APPROVE_REASON)
    hq.decide(TASK_PLAN_B, approved=True, operator=APPROVER, note=APPROVE_REASON)
    run_until_settled(bus, gate, cp, plan_id)

    advice = artifact_of(store, TASK_PAY_B, "ap_bank_advice")
    obs_rows = _count(store, "SELECT COUNT(*) AS n FROM ap_payment_observation")
    print(f"\n[3] 银行回单: {advice['observed_state']}"
          f"（问了 {advice['poll_count']} 次仍非终态）")
    print(f"    {advice[ADVICE_FIELD]['message']}")
    print(f"    观察行  : {obs_rows} 条 —— 「我问累了」和「银行说没付成」是两回事，"
          f"问不出来就一个字都不写")

    # —— 四个 Agent 全回 ok，这是本场景的题眼 ——
    agent_status = _agent_status(store, plan_id)
    print(f"\n[4] 四个 Agent 的自述: {agent_status}")
    print(f"    全部 ok。而 ap_case.biz_status = "
          f"{guard.get_case(store, TENANT_ID, CASE_BAD)['biz_status']} —— "
          f"「Agent 说完成了」不等于业务成功了")

    # —— 人工介入：主管看着这份回单驳回 ——
    pending = hq.pending(plan_id)
    assert [t["task_id"] for t in pending] == [TASK_PAY_B], (
        f"付款任务应停在 BLOCKED 等人处置，实际 {[t['task_id'] for t in pending]}")
    print(f"\n[5] 待主管处置: {pending[0]['title']} —— 回单非终态，不能当成功放行")

    C.record_approval(store, tenant_id=TENANT_ID, case_id=CASE_BAD, approver=APPROVER,
                      decision="rejected", reason=REJECT_REASON)

    # 先补偿、再落 FAILED —— 与 control_plane.human_decision 同一个顺序与同一个理由：
    # 状态一旦落 FAILED，「外面还有一笔下落不明的指令」这件事就没人记得了。
    comp = compensate(store, plan_id=plan_id, task_id=TASK_PAY_B, trace_id=trace_id,
                      case_id=CASE_BAD, operator=APPROVER, reason=REJECT_REASON)
    print(f"\n[6] 域内补偿: 作废 {len(comp['revoked'])} 笔付款指令，"
          f"最后观察到的下落 = {comp['last_observed_state']}"
          f"（**不是 failed** —— 银行自己都没给出结论）")
    print(f"    对账工单: {comp['ticket']['ticket_id']} -> {comp['ticket']['assignee']}")
    print(f"    工单待办: {comp['ticket']['todo']}")

    hq.decide(TASK_PAY_B, approved=False, operator=APPROVER, note=REJECT_REASON)
    bus.drain()

    dump(cp, plan_id, "场景 10 失败路径：应付账款付款回单问不出来")
    return {"store": store, "cp": cp, "plan_id": plan_id, "trace_id": trace_id,
            "match": match, "advice": advice, "compensation": comp,
            "agent_status": agent_status}


def _agent_status(store, plan_id: str) -> dict:
    """四个 Agent 各自的**自述结论** —— 取自控制面的状态迁移，不从任务终态推。

    判据是 `RUNNING -> AWAITING_REVIEW` 这一跳：`ControlPlane.on_task_result` 里
    **只有 `status == "ok"` 那一条分支**走这一跳（`control_plane.py:356`），
    `blocked` 走 BLOCKED、`failed` 走 PENDING/FAILED。而全仓 `_transit(...,
    AWAITING_REVIEW)` 只此一处（Gate 只读这个状态、不写它）。所以「这个任务出现过
    这一跳」与「它的 Agent 回了 ok」是同一件事。

    **不取任务终态**：那正是本场景要对比的另一个东西。付款任务最终是 FAILED，
    但那是**人**驳回的结果，不是 Agent 的自述 —— Agent 那一步跑完了、产物交了、
    一个异常都没抛。两者的差就是本场景要讲的话。

    也**不新造一个事件类型**来记这件事：event_log 是审计的唯一来源，同一个事实
    落两处，两处迟早不一致，而不一致的那一天没有任何症状。
    """
    out: dict[str, str] = {}
    for e in store.list_event_log(plan_id):
        if (e["event_type"] == "StateTransition"
                and e["from_state"] == TaskState.RUNNING
                and e["to_state"] == TaskState.AWAITING_REVIEW):
            out[e["task_id"]] = "ok"
    return out


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
    case = guard.get_case(store, TENANT_ID, CASE_OK)
    obs = guard.observations_of(store, TENANT_ID, CASE_OK)
    plan = cp.store.get_plan(plan_id)

    print(f"\n  业务状态  : {case['biz_status']}")
    print(f"  settled 观察: {len(obs)} 条，流水号 "
          f"{[o['bank_reference'] for o in obs]}")
    print(f"  Plan 终态 : {plan['state']}")

    assert out["match"]["matched"] is True, "顺利路径的三单必须匹配得上"
    assert case["biz_status"] == "settled", (
        f"银行给了流水号之后业务状态应为 settled，实际 {case['biz_status']}")
    # settled 必须有一条**带流水号**的观察兜底 —— 这是本域比退款域多要的那一条。
    assert len(obs) == 1 and obs[0]["observed_state"] == "settled", (
        f"settled 必须恰好有一条终态观察兜底，实际 {[o['observed_state'] for o in obs]}")
    assert obs[0]["bank_reference"], (
        "settled 的观察必须带银行流水号 —— 没有流水号的「已付」在财务上对不了账")
    assert obs[0]["actor_invocation_id"], "观察必须带 actor 锚点，否则审计链断了"
    # 终态是**问出来的**：一次 query 不够，poll_count 恒等于 settle_after。
    assert out["advice"]["poll_count"] == SETTLE_AFTER_OK, (
        f"应恰好轮询 {SETTLE_AFTER_OK} 次拿到终态，实际 {out['advice']['poll_count']}")
    # 受理回单**永远不是终态**：这一条塌了，ap.observe 就没有存在理由。
    assert out["instruction"][ADVICE_FIELD]["status"] == "accepted", (
        f"银行受理回单不该是终态，实际 {out['instruction'][ADVICE_FIELD]['status']}")
    # 付出去的钱是**我们按规则算的**，不是抄发票上的数字。
    assert out["plan"]["payable_amount"] == out["match"]["payable_amount"], (
        "付款计划的金额必须取匹配算出来的那个")
    assert plan["state"] == PlanState.DONE, (
        f"顺利路径应收敛到 DONE，实际 {plan['state']}")
    _assert_frozen_states(cp, plan_id)


def _assert_failure(out: dict) -> None:
    """失败路径的收口断言 —— 与场景 7 同构的那份收口。"""
    store, cp, plan_id = out["store"], out["cp"], out["plan_id"]
    case = guard.get_case(store, TENANT_ID, CASE_BAD)
    settled_rows = _count(
        store, "SELECT COUNT(*) AS n FROM ap_payment_observation"
               " WHERE observed_state='settled'")
    comp_rows = objects.query(
        store, "SELECT * FROM ap_compensation_record WHERE tenant_id=? AND case_id=?"
               " ORDER BY kind", (TENANT_ID, CASE_BAD))
    comp_events = [e for e in cp.store.list_event_log(plan_id)
                   if e["event_type"] == "CompensationExecuted"]
    plan = cp.store.get_plan(plan_id)
    comp = out["compensation"]

    print(f"\n  业务状态  : {case['biz_status']}（全程没有经过 settled）")
    print(f"  settled 观察: {settled_rows} 条 —— 没问出终态就一条都不该有")
    print(f"  补偿记录  : {len(comp_rows)} 行 {[r['kind'] for r in comp_rows]}")
    print(f"  补偿事件  : {len(comp_events)} 条 CompensationExecuted")
    print(f"  Plan 终态 : {plan['state']}（主管驳回，业务确实没成功）")
    print(f"  Agent 自述 : {out['agent_status']} —— 四个全 ok，而案子没成")

    # —— 本轨要买的第二件东西：Agent 全回 ok ≠ 业务成功 ——
    statuses = out["agent_status"]
    assert len(statuses) == 4 and set(statuses.values()) == {"ok"}, (
        f"失败路径上四个 Agent 都应回 ok（这正是要演的），实际 {statuses}")

    # —— 本场景存在的理由，第一断言 ——
    assert case["biz_status"] == "compensated", (
        f"补偿之后业务状态应为 compensated，实际 {case['biz_status']}")
    assert settled_rows == 0, (
        f"全库不该有任何 settled 观察，实际 {settled_rows} 条 —— "
        "有就说明有人在没问出终态的情况下把外部状态写死为终态了")
    assert _count(store, "SELECT COUNT(*) AS n FROM ap_payment_observation") == 0, (
        "轮询到顶没问出终态时一条观察都不该写 —— 「我问累了」不是可以落库的结论")

    # —— 补偿真发生过：记录、事件、语义三样都在 ——
    kinds = {r["kind"] for r in comp_rows}
    assert KIND_INSTRUCTION_REVOKED in kinds and KIND_RECONCILIATION_TICKET in kinds, (
        f"补偿必须同时留下作废记录与对账工单，实际 {sorted(kinds)}")
    assert comp_events, "补偿执行必须落 CompensationExecuted，否则这件事只活在日志里"
    assert comp["last_observed_state"] == UNOBSERVED, (
        f"轮询没问出终态时最后观察应为 {UNOBSERVED}，实际 {comp['last_observed_state']} "
        "—— 写成 failed 就是替银行下了它自己都没下的结论")
    assert len(comp["revoked"]) == 1, (
        f"应恰好作废一笔付款指令，实际 {len(comp['revoked'])} 笔 —— "
        "幂等键由 (租户, 案子) 唯一确定，一张发票只允许有一笔")

    # —— poll_count 是「终态是问出来的」的唯一审计证据 ——
    assert out["advice"]["poll_count"] == MAX_POLLS_STUCK, (
        f"应恰好轮询 {MAX_POLLS_STUCK} 次，实际 {out['advice']['poll_count']}")
    assert out["advice"]["settled"] is False
    assert out["advice"]["observed_state"] != "settled"

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
    for biz in guard.BIZ_STATUS_FLOW:
        assert biz not in task_states, (
            f"{biz} 是 ap_case 自己的字段，不许变成 Task 状态（铁律 9）")
    moves = {(e["from_state"], e["to_state"]) for e in cp.store.list_event_log(plan_id)
             if e["event_type"] == "StateTransition"}
    assert moves <= set(TASK_TRANSITIONS), (
        f"出现了不在冻结迁移表里的 Task 迁移：{sorted(moves - set(TASK_TRANSITIONS))}")
