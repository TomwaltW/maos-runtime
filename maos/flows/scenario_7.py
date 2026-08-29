"""场景 7：制造企业售后退款 —— **失败路径**（手册的场景 R2，按 D-05 编号为 7）。

    多源诉求 → 政策裁定通过 → 财务核算（第六道闸）→ 主管审批通过
      → payment.execute → 主渠道返 40005（retriable=True / outcome=**failed**）
      → 第七道闸判 replan_channel → **重规划换渠道**（s7-primary -> s7-backup），幂等键不变
      → 备用渠道返 ACQ.SYSTEM_ERROR（retriable=True / outcome=**unknown**）→ 一票否决再 replan
      → payment.observe 轮询 3 次仍问不出终态 → 不写状态、不写观察行
      → 付款任务 effect_risk=H，Gate 过了也停在 BLOCKED，等人处置
      → 主管驳回：渠道异常，转人工
      → refund.compensate：留档最后观察 + 开人工工单 + biz_status -> compensated
      → Plan FAILED
      → refund_case.biz_status = 'compensated'，**从未进入 settled**

## 这个场景要证明的那句话

    「所有 Agent 都回复完成」没有发生，因为业务确实没成功，系统如实记录了这一点。

顺利路径（场景 6）证明的是链路跑得通；本场景证明的是**跑不通的时候不会假装跑通**。
失败路径的价值高于顺利路径，就在这里 —— HITL 与补偿不是 PPT 上的框。

## 三处刻意不「顺手兜底」的地方，每一处都是铁律 8 的落点

1. **轮询到顶不改判成失败**。`MockGateway(settle_after=99)` 保证 3 次 query 一定
   问不出终态。此时 `payment.observe` 一行观察都不写、一个状态都不推 ——
   「我问累了」和「网关说失败了」是两回事。于是 `payment_observation` 表是**空的**，
   而不是躺着一行伪造的 failed。

2. **补偿不宣布那笔钱没退出去**。`ACQ.SYSTEM_ERROR` 的官方 remedy 原文是
   「保持参数不变重试**或查询执行结果**」—— 网关自己都不知道那一笔执行了没有。
   所以 `refund.compensate` 落的是「MAOS 侧不再推进 + 最后观察到的下落 + 人工工单」，
   `last_observed_state` 是 `unobserved` 而不是 `failed`。真退过的话，写成 failed
   会让账面上凭空少一笔。

3. **业务状态不进 Task 状态机**（铁律 9）。`compensated` 是 `refund_case` 自己的字段，
   `contracts/states.py` 一个新状态、一条新迁移都没加 —— 本场景的收口断言之一就是
   全部 Task 迁移仍落在既有迁移表内。

## 无 key 可跑

`select_model_client(SCRIPT, force_scripted=True)`（A-12）：配了 key 的机器上也一行
网络都不走。金额、政策版本、错误码、轮询次数全部写死，连跑两次输出逐条一致。

## 手册 R2 的「换渠道重试」演在哪一段

手册 R2 原文里「网关可重试错误码 → replan 换渠道重试 → 仍失败 → 转人工」这一段，
**机制**由第七道闸 `ReviewerGate._gate_gateway`（产 finding）与
`ControlPlane._should_replan`（消费 disposition）落地，19 条测试守在
`maos/tests/test_replan_gateway.py`。但机制跑得通不等于**看得见** —— 这一段
现在演在本文件的下面两处，评委在屏幕上就能读到：

  · **换渠道**：`drive()` 注册两个网关。主渠道 `s7-primary` 注入 `40005`
    （retriable=True + outcome=failed）—— 四象限里**唯一**允许换渠道的那一格：
    网关在入口就拒了，业务确定没执行，所以重发是安全的。第七道闸判
    `replan_channel`，`_switch_channel` 把付款任务的 `gateway` 改派到 `s7-backup`。
    幂等键由 (tenant, case) 唯一确定，换渠道**不换键**，不会造出第二笔。
  · **换完仍失败，但不再换第三次**：备用渠道注入 `ACQ.SYSTEM_ERROR`
    （retriable=True + outcome=unknown），这一格对 replan 是**一票否决**——
    网关自己都说不清那一笔执行了没有，再重发就可能真退第二笔（铁律 8）。
    于是不自旋，落回 `effect_risk=H` 的人工审批入口，也就是本文件原先唯一走的
    那条收口路径：主管驳回 → 补偿 → Plan FAILED。

两个码一前一后，演的正是要讲给评委的那句话：**可重试的那一格才重试，
说不清的那一格一次都不许重发**。收口断言（第 383 行起）一条都没有因此改动 ——
换渠道消耗的是一次 replan 额度，不是轮询预算，`poll_count` 仍恰好打满 3 次。
"""

from __future__ import annotations

import json

from maos.agents.base import AgentIdentity
from maos.agents.manager import ManagerAgent
from maos.agents.refund import (
    KIND_PAYMENT_RECEIPT,
    ROLE_FINANCE,
    ROLE_INTAKE,
    ROLE_PAYMENT,
    ROLE_POLICY,
)
from maos.agents.reviewer import ReviewerAgent, review_after_gate
from maos.contracts.events import new_id
from maos.contracts.states import TASK_TRANSITIONS, PlanState, TaskState
from maos.core.control_plane import GATEWAY_GATE
from maos.domain.refund import guard, objects
from maos.flows.common import build, dump, run_until_settled
from maos.model.client import Tier, select_model_client
from maos.runtime.gate import HumanApprovalQueue
from maos.skills.builtin.refund import _common as C
from maos.skills.builtin.refund.compensate import (
    KIND_MANUAL_TICKET,
    KIND_REQUEST_REVOKED,
    UNOBSERVED,
)
from maos.skills.invoker import SkillInvoker
from maos.tools.gateway import MockGateway

# ---------------------------------------------------------------------- 常量
# 全部写死，不用 new_id：验收之一是「连跑两次输出逐条一致」，而 dump() 会打印
# task_id（flows/common.py）。随机 id 会让两次输出必然不同。
TENANT_ID = "tnt-mfg-001"
CHANNEL_ID = "ch-dealer"
CASE_ID = "case-s7-0001"
ORDER_ID = "ord-s7-88231"
ORDER_VERSION = 1
SKU = "SKU-BRG-6204"
PAID_AT = "2026-07-05T10:00:00+00:00"
AMOUNT_PAID = 6800.00
#: 超过第六道闸的阈值（MAOS_FINANCE_THRESHOLD，默认 5000）——
#: 财务核算任务必须交出 finance_entry，否则闸判 blocker。
AMOUNT_CLAIMED = 6800.00

#: 主渠道与被换过去的备用渠道。两个都是 MockGateway，只有注入的码不同 ——
#: 「换渠道」在本场景里是一次真实的重规划（换掉付款任务的 `gateway` 入参），
#: 不是在同一个网关实例上改脚本。后者演不出 replan，评委也看不出换了什么。
GATEWAY_NAME = "s7-primary"
GATEWAY_BACKUP_NAME = "s7-backup"

#: 主渠道注入的码 —— 四象限里**唯一**允许换渠道重试的那一格：
#:   · retriable=True  —— 官方 remedy 就是重试；
#:   · outcome=failed  —— 网关在入口就拒了，这一笔**确定没执行**，重发不会造出第二笔；
#: 第七道闸判它 `replan_channel`，`_should_replan` 据此触发重规划。
#: 换成 outcome=unknown 的码就演不出换渠道了 —— 那一格是一票否决。
GATEWAY_RETRIABLE_CODE = "40005"

#: 备用渠道注入的码。选它有三个理由，缺一不可：
#:   · retriable=True —— 是「可重试」那一类，不是终态失败；
#:   · outcome=unknown —— 官方 remedy 原文带「或查询执行结果」，网关自己说不清；
#:   · layer=business —— 业务层的码，与网关层的 20000 各占一层，演示时能讲清两层结构。
#: 它对 replan 是**一票否决**：换过一次渠道之后不再换第三次，落人工审批收口。
#: 判据一律查 `maos/tools/gateway_codes.py`，不在这里凭语感另定。
GATEWAY_ERROR_CODE = "ACQ.SYSTEM_ERROR"

#: 轮询上限 3，而网关要 99 次才「结算」—— 保证**一定**问不出终态。
#: 两个数写死是本场景确定性的来源：poll_count 恒为 3。
MAX_POLLS = 3
SETTLE_AFTER = 99

TASK_INTAKE = "task-s7-intake"
TASK_POLICY = "task-s7-policy"
TASK_FINANCE = "task-s7-finance"
TASK_PAYMENT = "task-s7-payment"

#: 只用于打印，判据仍在 control_plane —— 这里不重算一遍上限逻辑。
MAX_REPLAN_ENV = "MAOS_MAX_REPLAN"

APPROVER = "沈思锴"
REJECT_REASON = "渠道异常，转人工"

GOAL = "处理客户对轴承订单的退款诉求：政策与金额均无异议，但支付渠道回执异常"

# ---------------------------------------------------------------- 多源退款诉求
# 与场景 6 同构：三条说同一件事（标题归一化后相同），第四条是另一件事不该被并掉。
SIGNALS = [
    {"source": "工单系统", "kind": "ticket", "severity": "major",
     "title": "轴承内圈有裂纹", "detail": "工单 T-20913：客户反馈内圈可见裂纹，要求全额退款"},
    {"source": "客服记录", "kind": "csr_note", "severity": "major",
     "title": "轴承内圈有裂纹 ", "detail": "客服 0726 通话记录：客户口述同一问题"},
    {"source": "客户上传", "kind": "image", "severity": "major",
     "title": "轴承内圈有裂纹", "detail": "客户上传的实物照片",
     "uri": "oss://after-sales/case-s7-0001/crack-01.jpg",
     "digest": "sha256:demo-crack-01", "evidence_id": "ev-11"},
    {"source": "客户上传", "kind": "image", "severity": "minor",
     "title": "随货保修卡缺失", "detail": "包装内未见保修卡",
     "uri": "oss://after-sales/case-s7-0001/card-01.jpg",
     "digest": "sha256:demo-card-01", "evidence_id": "ev-12"},
]

# 政策与场景 6 同一套：两版生效区间完全相同，只有版本号不同，
# 把 v2 排除在外的只可能是「订单锁定了 v1」。
POLICY_RULES = [
    ("AS-01", 1, "整机质量问题全额退款",
     {"refund_ratio": 1.0, "deduct_fee": 0},
     "2026-01-01T00:00:00+00:00", None, "*", "*"),
    ("AS-01", 2, "整机质量问题退款扣除渠道手续费（新版）",
     {"refund_ratio": 0.8, "deduct_fee": 50},
     "2026-01-01T00:00:00+00:00", None, "*", "*"),
    ("PS-07", 1, "预售定金不退",
     {"refund_ratio": 0.0, "deduct_fee": 0},
     "2026-01-01T00:00:00+00:00", None, "*", "*"),
]

# ------------------------------------------------------------------- DAG 与脚本
_TASKS = [
    {"task_id": TASK_INTAKE, "role": ROLE_INTAKE, "title": "受理多源退款诉求并聚合证据",
     "inputs": {"step": "intake", "biz_type": C.BIZ_TYPE, "signals": SIGNALS,
                "case_seed": {
                    "tenant_id": TENANT_ID, "case_id": CASE_ID, "channel_id": CHANNEL_ID,
                    "order_id": ORDER_ID, "order_version": ORDER_VERSION, "sku": SKU,
                    "reason_code": "quality_defect", "amount_claimed": AMOUNT_CLAIMED}},
     "acceptance": ["多源诉求去重后建出 refund_case", "证据引用落库"],
     "depends_on": [], "risk_level": "L"},

    {"task_id": TASK_POLICY, "role": ROLE_POLICY, "title": "按下单锁定的政策版本裁定退款资格",
     "inputs": {"biz_type": C.BIZ_TYPE, "tenant_id": TENANT_ID, "case_id": CASE_ID},
     "acceptance": ["按订单快照锁定的政策版本判定", "给出命中的规则编号与版本"],
     "depends_on": [TASK_INTAKE], "risk_level": "L"},

    # 只有这个任务带 amount_claimed —— 第六道财务复核闸按
    # `biz_type == "refund" and amount_claimed > 阈值` 触发（F-1），判据是**同 attempt**
    # 的产物里有没有 finance_entry，而那份产物只有本任务产得出来。
    # 别的退款任务带上金额就会被要求交一份它根本不产出的凭据，闸恒 blocker。
    {"task_id": TASK_FINANCE, "role": ROLE_FINANCE, "title": "核算退款金额并写财务分录",
     "inputs": {"biz_type": C.BIZ_TYPE, "amount_claimed": AMOUNT_CLAIMED,
                "tenant_id": TENANT_ID, "case_id": CASE_ID},
     "acceptance": ["产出 finance_entry 且与库表一致", "金额按锁定政策版本核算"],
     "depends_on": [TASK_POLICY], "risk_level": "M", "effect_risk": "H"},

    # 付款任务同样 effect_risk=H：把钱打出去是不可逆的落地动作，Gate 过了也要人放行。
    # 本场景的转折点就在这里 —— 主管拿到的不是「成功」，是一份 observed_state=unknown
    # 的回执，于是他驳回。**没有这一步，unknown 会被当成「还在处理中」一路挂着**。
    {"task_id": TASK_PAYMENT, "role": ROLE_PAYMENT, "title": "发起退款并观察网关终态",
     "inputs": {"biz_type": C.BIZ_TYPE, "tenant_id": TENANT_ID, "case_id": CASE_ID,
                "gateway": GATEWAY_NAME, "max_polls": MAX_POLLS},
     "acceptance": ["发起后不得写 settled", "终态必须由 query 观察得到"],
     "depends_on": [TASK_FINANCE], "risk_level": "M", "effect_risk": "H"},
]

PLAN_JSON = json.dumps({"tasks": _TASKS}, ensure_ascii=False)

REVIEW_JSON = json.dumps({
    "defects": [],
    "conclusion": "金额按订单锁定的政策 v1 核算，依据 AS-01@v1；退款尚未发起，可放行",
}, ensure_ascii=False)

# 查表顺序即分派规则：ScriptedModelClient 返回**第一个**命中的关键字，专用的排前面。
SCRIPT = {
    "语义审查产物清单": REVIEW_JSON,
    "用户请求": PLAN_JSON,
}

# ---- 编排层自己的 identity：补偿收口是**人的决定之后**的动作，不属于任何 Agent ----
# 与其给退款域再加一个 Agent（`maos/agents/refund/**` 不在本轨白名单，且那会让
# 「补偿是谁做的」这个问题多一个含糊的答案），不如让编排层带一个只有补偿权限的
# identity —— 白名单机制正是用来表达这种最小授权的。口径同 scenario_5 的
# INTAKE_IDENTITY。
COMPENSATION_IDENTITY = AgentIdentity(
    agent_id="refund-compensation-desk",
    role="refund_compensation",
    duty="退款走不通之后的域内补偿收口：留档最后观察、开人工工单、推进 compensated",
    allowed_skills=frozenset({"refund.compensate"}),
    allowed_tools=frozenset(),
    write_scope=frozenset(),
    max_risk="M",
    model_tier=Tier.LIGHT,
)


# ---------------------------------------------------------------------- 靶场数据
def seed_domain(store) -> None:
    """预置外部系统快照与政策 —— 它们是**读到的那一版**，不是外部系统的当前值。"""
    objects.ensure_schema(store)
    objects.execute(store, "INSERT OR REPLACE INTO tenant (tenant_id, name, region)"
                           " VALUES (?,?,?)", (TENANT_ID, "示例精密制造", "CN-EAST"))
    objects.execute(store, "INSERT OR REPLACE INTO channel (tenant_id, channel_id, kind, name)"
                           " VALUES (?,?,?,?)", (TENANT_ID, CHANNEL_ID, "dealer", "华东经销商"))
    objects.execute(
        store,
        "INSERT OR REPLACE INTO product_snapshot (tenant_id, sku, version, name, category,"
        " warranty_months, payload_json) VALUES (?,?,?,?,?,?,?)",
        (TENANT_ID, SKU, 1, "深沟球轴承 6204", "bearing", 12, "{}"))
    objects.execute(
        store,
        "INSERT OR REPLACE INTO order_snapshot (tenant_id, order_id, version, sku, amount_paid,"
        " paid_at, channel_id, policy_version_at_order, payload_json, read_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (TENANT_ID, ORDER_ID, ORDER_VERSION, SKU, AMOUNT_PAID, PAID_AT, CHANNEL_ID,
         1, "{}", C.now_iso()))
    for rule_no, version, title, params, eff_from, eff_to, ch, sku in POLICY_RULES:
        objects.execute(
            store,
            "INSERT OR REPLACE INTO policy_rule (tenant_id, rule_no, version, title, body,"
            " effective_from, effective_to, channel_scope, sku_scope) VALUES (?,?,?,?,?,?,?,?,?)",
            (TENANT_ID, rule_no, version, title,
             json.dumps(params, ensure_ascii=False, sort_keys=True),
             eff_from, eff_to, ch, sku))


def receipt_artifact(store, task_id: str) -> dict:
    """取付款任务**最近一轮**的回执产物 —— 主管就是看着它做驳回决定的。

    按 `version`（= 产出它的那次 attempt）取最大的一份，不取列表里的第一份：
    换渠道重试之后这个任务有两份回执（attempt=1 的 40005、attempt=2 的
    ACQ.SYSTEM_ERROR），而主管处置的依据只能是**最后那一份**。取第一份会让
    收口断言去校验一份已经被重规划推翻了的回执。
    """
    arts = [a for a in store.list_artifacts(task_id) if a["kind"] == KIND_PAYMENT_RECEIPT]
    if not arts:
        raise LookupError(f"{task_id} 没有回执产物 —— 付款任务应当产出 {KIND_PAYMENT_RECEIPT}")
    return max(arts, key=lambda a: a["version"])["content"]


def _replan_count(cp, plan_id: str) -> int:
    """重规划次数 = event_log 里的 Replanned 条数（口径同 scenario_5）。"""
    return sum(1 for e in cp.store.list_event_log(plan_id)
               if e["event_type"] == "Replanned")


def compensate(store, *, plan_id: str, task_id: str, trace_id: str, operator: str,
               reason: str) -> dict:
    """编排层以最小授权 identity 调 refund.compensate。

    走 SkillInvoker 而不是直接 `RefundCompensateSkill().run()`：白名单校验与
    SkillInvoked 审计行都在 invoker 里，直接调就没有审计行，出事之后查不到是谁做的。
    """
    invoker = SkillInvoker(COMPENSATION_IDENTITY, store)
    res = invoker.invoke("refund.compensate", {
        "tenant_id": TENANT_ID, "case_id": CASE_ID,
        "operator": operator, "reason": reason, "assignee": operator,
    }, extras={"plan_id": plan_id, "task_id": task_id, "trace_id": trace_id})
    if res.status != "ok" or not isinstance(res.output, dict):
        raise RuntimeError(f"域内补偿失败，不许静默收口：{res.error}")
    return res.output


def _count(store, sql: str, params: tuple = ()) -> int:
    return objects.query(store, sql, params)[0]["n"]


def _switch_channel(*, goal: str, findings: list[dict], open_tasks: list[dict]) -> list[dict]:
    """重规划回调：把付款任务改派到备用渠道 —— 换渠道就是这一次重规划的全部内容。

    **零模型**。同 scenario_5 的口径：replan、补偿、审批是控制面行为，其正确性不得
    依赖模型的智力表现。这里更强一层 —— 让 Manager 重新出一份方案会返回**整份**
    四任务 DAG，而 `_apply_replan` 是逐位 zip 覆写 open_tasks（此刻只剩付款一条），
    多出来的三条会被当成新任务插进来，把已经 DONE 的受理/裁定/核算凭空重做一遍。

    返回的 specs 与 open_tasks **等长**，就是为了让覆写落在原 task_id 上：
    task_id、attempt 与 event_log 的因果链连续，Trace 上看得出「换渠道前后是同一件事」。
    """
    gw = [f for f in findings if isinstance(f, dict) and f.get("gate") == GATEWAY_GATE]
    for f in gw:
        print(f"\n[2] 第七道闸认出网关回执: code={f.get('code')} "
              f"retriable={f.get('retriable')} outcome={f.get('outcome')} "
              f"-> disposition={f.get('disposition')}")
        print(f"    {f.get('message')}")

    specs = []
    for task in open_tasks:
        inputs = dict(task["inputs"])
        title = task["title"]
        if inputs.get("gateway") == GATEWAY_NAME:
            inputs["gateway"] = GATEWAY_BACKUP_NAME
            title = f"{title}（改派备用渠道）"
            print(f"    → 触发 replan 重规划：付款任务换渠道 "
                  f"{GATEWAY_NAME} -> {GATEWAY_BACKUP_NAME}；"
                  f"幂等键由 (tenant, case) 定，换渠道不换键，不会造出第二笔")
        specs.append({
            "task_id": task["task_id"], "role": task["role"], "title": title,
            "inputs": inputs, "acceptance": task["acceptance"],
            "depends_on": task["depends_on"], "risk_level": task["risk_level"],
            # effect_risk 显式带上：付款仍是不可逆落地动作，换个渠道不降它的风险。
            "effect_risk": task["effect_risk"],
        })
    return specs


# ------------------------------------------------------------------------ drive
def drive(*, matrix: bool = False) -> dict:
    """跑完整条失败路径并返回收口用的句柄。**只跑不断言** —— 断言在 run() 里。

    拆出这一层是给 `maos/tests/test_refund_failure.py` 用的：测试要对**库里的行**
    下断言（settled 观察 0 条、补偿记录两种 kind），而 run() 只返回一个退出码，
    store 拿不出来。让测试自己再拼一遍流程则等于维护第二份场景，两边迟早漂。
    """
    print("场景 7：制造企业售后退款（失败路径），无 key 确定性复现")

    model = select_model_client(SCRIPT, force_scripted=True)
    store, bus, cp, model, worker, gate = build(SCRIPT, matrix=matrix, model=model)
    seed_domain(store)

    # 网关按名取：task.inputs 会被 json.dumps，实例塞不进去（见 _common.py 第 3 条）。
    # script 按 out_trade_no 注入错误码，确定性回放，不依赖随机数。
    # 两个网关各注一个码：主渠道 40005（确定没执行，可换渠道），
    # 备用渠道 ACQ.SYSTEM_ERROR（说不清，一票否决再 replan）。同一个 MockGateway 类，
    # 行为一行没改 —— 换的是任务派到哪个实例上。
    C.reset_gateways()
    C.register_gateway(GATEWAY_NAME, MockGateway(
        settle_after=SETTLE_AFTER, script={ORDER_ID: GATEWAY_RETRIABLE_CODE}))
    C.register_gateway(GATEWAY_BACKUP_NAME, MockGateway(
        settle_after=SETTLE_AFTER, script={ORDER_ID: GATEWAY_ERROR_CODE}))

    # 控制面只认这个回调，不认识 ManagerAgent（同 scenario_5）。
    cp.set_replanner(_switch_channel)

    mgr = ManagerAgent(model)
    trace_id = new_id("trace")
    plan_id = cp.create_plan(goal=GOAL, trace_id=trace_id, tasks=mgr.plan(GOAL))
    cp.start_plan(plan_id)
    run_until_settled(bus, gate, cp, plan_id)

    hq = HumanApprovalQueue(store, cp)

    # —— 第一次人工介入：财务核算通过，放行 ——
    pending = hq.pending(plan_id)
    assert len(pending) == 1 and pending[0]["task_id"] == TASK_FINANCE, (
        f"应停在财务核算的人工审批上，实际 {[t['task_id'] for t in pending]}")
    finance_task = pending[0]
    print(f"\n[1] 待主管审批: {finance_task['title']}")

    reviewer = ReviewerAgent(model)
    note = review_after_gate(reviewer, cp, plan_id, host_task=finance_task)
    print(f"    语义审查: {note.artifacts[0]['content']['conclusion']}")

    # 审批是**人**的动作：先落 approval_record，再放行任务。顺序不可换 ——
    # payment.execute 会核对审批记录，没有它就拒绝发起付款。
    C.record_approval(store, tenant_id=TENANT_ID, case_id=CASE_ID, approver=APPROVER,
                      decision="approved", reason="金额与订单锁定的政策 v1 一致")
    hq.decide(finance_task["task_id"], approved=True, operator=APPROVER,
              note="已核对金额与政策版本")
    run_until_settled(bus, gate, cp, plan_id)

    # —— 换过渠道之后仍然没跑成 ——
    receipt = receipt_artifact(store, TASK_PAYMENT)
    replans = _replan_count(cp, plan_id)
    print(f"\n[3] 备用渠道付款回执: observed_state={receipt['observed_state']}"
          f"（问了 {receipt['poll_count']} 次仍非终态）")
    print(f"    网关判据: code={receipt['receipt']['code']} "
          f"retriable={receipt['receipt']['retriable']} "
          f"outcome={receipt['receipt']['outcome']}")
    print(f"    官方处置: {receipt['remedy']}")
    print(f"    出处    : {receipt['source']}")
    print(f"    重规划   : 已换渠道 {replans} 次（replan 上限 {MAX_REPLAN_ENV} 默认 2）；"
          f"这一格 outcome=unknown 对 replan 一票否决，**不再换第三个渠道** —— "
          f"重发可能真退出第二笔，正确出口是人工")

    # —— 第二次人工介入：主管看着这份回执驳回 ——
    pending = hq.pending(plan_id)
    assert len(pending) == 1 and pending[0]["task_id"] == TASK_PAYMENT, (
        f"付款任务应停在 BLOCKED 等人处置，实际 {[t['task_id'] for t in pending]}")
    payment_task = pending[0]
    print(f"\n[4] 待主管处置: {payment_task['title']} —— 回执非终态，不能当成功放行")

    C.record_approval(store, tenant_id=TENANT_ID, case_id=CASE_ID, approver=APPROVER,
                      decision="rejected", reason=REJECT_REASON)

    # 先补偿、再落 FAILED —— 与 control_plane.human_decision 同一个顺序与同一个理由：
    # 状态一旦落 FAILED，「外面还有一笔下落不明的请求」这件事就没人记得了。
    comp = compensate(store, plan_id=plan_id, task_id=TASK_PAYMENT, trace_id=trace_id,
                      operator=APPROVER, reason=REJECT_REASON)
    print(f"\n[5] 域内补偿: 作废 {len(comp['revoked'])} 笔请求，"
          f"最后观察到的下落 = {comp['last_observed_state']}"
          f"（**不是 failed** —— 网关自己都没给出结论）")
    print(f"    人工工单: {comp['ticket']['ticket_id']} -> {comp['ticket']['assignee']}")

    hq.decide(payment_task["task_id"], approved=False, operator=APPROVER, note=REJECT_REASON)
    bus.drain()

    dump(cp, plan_id, "场景 7：制造企业售后退款（失败路径）")
    return {"store": store, "cp": cp, "plan_id": plan_id, "trace_id": trace_id,
            "receipt": receipt, "compensation": comp, "replans": replans}


# -------------------------------------------------------------------------- run
def run(*, matrix: bool = False) -> int:
    out = drive(matrix=matrix)
    store, cp, plan_id = out["store"], out["cp"], out["plan_id"]
    receipt, comp = out["receipt"], out["compensation"]
    replans = out["replans"]

    # ---------------------------------------------------------------- 收口与断言
    case = guard.get_case(store, TENANT_ID, CASE_ID)
    settled_rows = _count(
        store, "SELECT COUNT(*) AS n FROM payment_observation WHERE observed_state='settled'")
    comp_rows = objects.query(
        store, "SELECT * FROM compensation_record WHERE tenant_id=? AND case_id=? ORDER BY kind",
        (TENANT_ID, CASE_ID))
    comp_events = [e for e in cp.store.list_event_log(plan_id)
                   if e["event_type"] == "CompensationExecuted"]
    plan = cp.store.get_plan(plan_id)

    print(f"\n  业务状态  : {case['biz_status']}（全程没有经过 settled）")
    print(f"  settled 观察: {settled_rows} 条 —— 没问出终态就一条都不该有")
    print(f"  补偿记录  : {len(comp_rows)} 行 {[r['kind'] for r in comp_rows]}")
    print(f"  补偿事件  : {len(comp_events)} 条 CompensationExecuted")
    print(f"  Plan 终态 : {plan['state']}（主管驳回，业务确实没成功）")
    print(f"  换渠道重试: {replans} 次 replan（{GATEWAY_RETRIABLE_CODE} 触发，"
          f"{GATEWAY_ERROR_CODE} 一票否决，没有自旋）")

    # —— 手册 R2 那一段真的演过：换渠道不是测试里才跑得通的机制 ——
    # 放在收口断言之前：这一条红了说明叙事没接上，收口那几条会跟着连锁红，
    # 先在这里断掉，报错信息才指得准。
    assert replans >= 1, (
        f"付款应先撞 {GATEWAY_RETRIABLE_CODE} 换一次渠道，实际重规划 {replans} 次 —— "
        "手册 R2 的 replan 段没演出来")
    # 两轮回执的码按 attempt 排出来，就是那条链路本身的证据。
    # 不拿 refund_request 表数渠道：它按 (tenant, case) 做 INSERT OR REPLACE，
    # 一个案子恒只留一行（那是「一个案子只允许有一笔退款」的落点），
    # 换渠道之后第一笔的 gateway 已被覆盖，数出来只有一个渠道。
    codes = [(a["version"], (a["content"].get("receipt") or {}).get("code"))
             for a in store.list_artifacts(TASK_PAYMENT)
             if a["kind"] == KIND_PAYMENT_RECEIPT]
    assert [c for _, c in sorted(codes)] == [GATEWAY_RETRIABLE_CODE, GATEWAY_ERROR_CODE], (
        f"付款两轮的回执码应依次是 {GATEWAY_RETRIABLE_CODE} -> {GATEWAY_ERROR_CODE}，"
        f"实际 {sorted(codes)}")
    assert objects.query(
        store, "SELECT gateway FROM refund_request WHERE tenant_id=? AND case_id=?",
        (TENANT_ID, CASE_ID))[0]["gateway"] == GATEWAY_BACKUP_NAME, (
        "收口那一笔应落在换过去的备用渠道上")

    # —— 本场景存在的理由，第一断言 ——
    assert case["biz_status"] == "compensated", (
        f"补偿之后业务状态应为 compensated，实际 {case['biz_status']}")
    assert settled_rows == 0, (
        f"全库不该有任何 settled 观察，实际 {settled_rows} 条 —— "
        "有就说明有人在没问出终态的情况下把外部状态写死为终态了")

    # —— 补偿真发生过：记录与事件都在 ——
    kinds = {r["kind"] for r in comp_rows}
    assert KIND_REQUEST_REVOKED in kinds and KIND_MANUAL_TICKET in kinds, (
        f"补偿必须同时留下作废记录与人工工单，实际 {sorted(kinds)}")
    assert comp_events, "补偿执行必须落 CompensationExecuted，否则这件事只活在日志里"
    assert comp["last_observed_state"] == UNOBSERVED, (
        f"轮询没问出终态时最后观察应为 {UNOBSERVED}，实际 {comp['last_observed_state']} —— "
        "写成 failed 就是替网关下了它自己都没下的结论")

    # —— poll_count 是「终态是问出来的」的唯一审计证据 ——
    assert receipt["poll_count"] == MAX_POLLS, (
        f"应恰好轮询 {MAX_POLLS} 次，实际 {receipt['poll_count']}")
    assert receipt["settled"] is False and receipt["observed_state"] != "settled"

    # —— 铁律 9：业务状态不进 Task 状态机，也没有为退款域新开一条迁移 ——
    # 断言两件事而不是一件：只查状态集合挡不住「用既有的两个状态连一条新边」。
    known_states = {v for k, v in vars(TaskState).items()
                    if not k.startswith("_") and isinstance(v, str)}
    task_states = {t["state"] for t in cp.store.list_tasks(plan_id)}
    assert task_states <= known_states, (
        f"出现了不在既有 Task 状态机内的状态：{sorted(task_states - known_states)}")
    assert "compensated" not in task_states, (
        "compensated 是 refund_case 自己的字段，不许变成 Task 状态（铁律 9）")
    moves = {(e["from_state"], e["to_state"]) for e in cp.store.list_event_log(plan_id)
             if e["event_type"] == "StateTransition"}
    assert moves <= set(TASK_TRANSITIONS), (
        f"出现了不在冻结迁移表里的 Task 迁移：{sorted(moves - set(TASK_TRANSITIONS))}")

    assert plan["state"] == PlanState.FAILED, (
        f"主管驳回后 Plan 应收敛到 FAILED，实际 {plan['state']}")
    return 0
