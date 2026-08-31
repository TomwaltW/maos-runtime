"""场景 8：保险理赔 —— 顺利路径 + 失败路径，一个文件两条。

## 顺利路径（`drive_happy`）

    三源报案（工单 + 客服记录 + 病历影像）聚合去重
      → **按投保当时锁定的条款版本**裁定，产出带条款编号与版本的 adjudication
      → 核算赔款：起付线 -> 赔付比例 -> 保额封顶
      → 超阈值（MAOS_CLAIM_APPROVAL_THRESHOLD，默认 5000）停 BLOCKED 等人批
      → 主管批准 → claim.pay 发起赔付指令（**此时不写 paid**）
      → claim.observe 轮询到到账回执 → **这时才**写 paid
      → Plan DONE

## 失败路径（`drive_failure`，两段）

    第一段：赔付方返 CARC 96 Non-covered charge(s)（+ RARC N130）而不是到账
      → claim.observe 落一条 observed_state='denied' 的观察，**不推业务状态**
      → 付款任务 effect_risk=H，Gate 过了也停在 BLOCKED 等人处置
      → 主管驳回 → claim.compensate：作废赔付指令 + 开人工工单 + biz_status=compensated
      → Plan FAILED，**全程从未进入 paid**

    第二段：赔付方一直不给终态（settle_after=99，轮询 3 次）
      → claim.observe **一行观察都不写**（「我问累了」不是一个可以落库的结论）
      → 同样停 BLOCKED → 驳回 → 补偿（last_observed_state = unobserved）→ FAILED

## 这两条路径分别要证明的那句话

顺利路径证明**链路跑得通**；失败路径证明**跑不通的时候不会假装跑通**，
而且失败路径的价值高于顺利路径。它买的是两样东西：

1. **权威事实边界成立。** 全系统只有 `claim.observe` 写得进 `paid`，越权写入
   不静默失败（`maos/domain/claim/guard.py`）。失败路径上 `paid` 观察恰好 **0 条** ——
   没问出到账就一条都不该有。

2. **「Agent 都说完成了」不等于业务成功。** 失败路径上四个 Agent **全部返回 ok**
   （见 `maos/agents/claim/payment_agent.py` 的模块 docstring 末段），而案子确实
   没成：`biz_status=compensated`、Plan `FAILED`、账上一分钱都没记成已赔付。
   收口断言逐条校这件事。

## 条款版本锁定 —— 本域的招牌判据

保单 `POL-2023-88412` 投保于 **2023-04-18**，锁定条款 v1；条款表里 `CL-01` 同时躺着
v1 与 v2，**两版的生效区间完全相同，只有版本号不同**。所以把 v2 排除在外的只可能是
「保单锁定了 v1」这一条，不可能是时间过滤的副产品。数字上看得见：

    按锁定的 v1：(12000 - 1000) × 0.9 = 9900.00
    按最新的 v2：(12000 - 3000) × 0.7 = 6300.00     ← 差 3600，而且是少赔

人家 2023 年投的保，不能拿 2025 年的条款判。这条与退款域的 `AS-01@v1` 是同构物：
同一条道理换一个域再成立一次。

## 内核零改动

本场景一行都没有改 `maos/contracts/**`、`maos/core/**`、`maos/runtime/**`：

  · 人工审批走的是**既有的 `effect_risk=H` 入口**（control_plane 在 ReviewVerdict=pass
    时把它路由到 BLOCKED），那条路不看业务域；
  · 业务状态 `compensated` 是 `claim_case` 自己的字段，**不是 Task 状态**（铁律 9），
    `contracts/states.py` 一个新状态、一条新迁移都没加 —— 收口断言之一就是全部 Task
    迁移仍落在既有迁移表内。

## 无 key 可跑

`select_model_client(SCRIPT, force_scripted=True)`：配了 key 的机器上也一行网络都不走。
金额、条款版本、CARC、轮询次数全部写死，连跑两次输出逐条一致。

## 本场景**不进** `main.py::DEFAULT_SCENARIOS`

同期三轨都在新增 flow，谁改 `main.py` 谁就和另两轨冲突。接进缺省序列是整合轮的事；
本轮由 `maos/tests/test_claim_scenario.py` 调用。
"""

from __future__ import annotations

import json

from maos.agents.base import AgentIdentity
from maos.agents.claim import (
    KIND_PAYER_RECEIPT,
    ROLE_ADJUDICATOR,
    ROLE_INTAKE,
    ROLE_PAYMENT,
    ROLE_SETTLEMENT,
)
from maos.agents.manager import ManagerAgent
from maos.agents.reviewer import ReviewerAgent, review_after_gate
from maos.contracts.events import new_id
from maos.contracts.states import TASK_TRANSITIONS, PlanState, TaskState
from maos.domain.claim import guard, objects
from maos.flows.common import build, dump, run_until_settled
from maos.model.client import Tier, select_model_client
from maos.runtime.gate import HumanApprovalQueue
from maos.skills.builtin.claim import _common as C
from maos.skills.builtin.claim.compensate import (
    KIND_MANUAL_TICKET,
    KIND_PAYMENT_REVOKED,
)
from maos.skills.builtin.claim.observe import UNOBSERVED
from maos.skills.invoker import SkillInvoker
from maos.tools.claim import MockPayer

# ---------------------------------------------------------------------- 常量
# 全部写死，不用 new_id：验收之一是「连跑两次输出逐条一致」，而 dump() 会打印
# task_id（flows/common.py）。随机 id 会让两次输出必然不同。
TENANT_ID = "tnt-ins-001"
PAYER_ID = "payer-hz-01"
POLICY_NO = "POL-2023-88412"
POLICY_VERSION = 1

#: 🔴 投保时刻。条款版本按它锁定 —— 2023 年投的保，判的是 2023 年的条款。
BOUND_AT = "2023-04-18T00:00:00+00:00"
#: 投保当时锁定的条款版本号。落在 `policy_contract.terms_version_at_bind` 上。
TERMS_VERSION_AT_BIND = 1

SUM_INSURED = 100000.00
POLICY_DEDUCTIBLE = 1000.00
POLICY_COINSURANCE = 0.9

LOSS_TYPE = "illness"
INCIDENT_AT = "2026-06-20T09:30:00+00:00"
REPORTED_AT = "2026-06-22T08:15:00+00:00"

CLAIM_ID = "clm-s8-0001"
AMOUNT_CLAIMED = 12000.00

#: 按锁定的 CL-01@v1 算出来的赔款。写死在这里是本场景确定性的来源之一：
#: (12000 - 1000) × 0.9 = 9900.00
EXPECTED_ALLOWED = "9900.00"
#: 如果错用了当前最新的 CL-01@v2 会算成这个数 —— (12000 - 3000) × 0.7 = 6300.00。
#: 只用于打印对比，让「版本锁定」这件事在屏幕上是个可以核的数字，不是一句话。
IF_LATEST_TERMS_ALLOWED = "6300.00"

#: 赔付方按名取：task.inputs 会被 json.dumps，实例塞不进去（见 _common.py 第 3 条）。
PAYER_HAPPY = "s8-payer"
PAYER_DENY = "s8-payer-deny"
PAYER_SILENT = "s8-payer-silent"

#: 顺利路径：两次 query 才到账 —— >1 才能证明「一次 query 不一定够」。
SETTLE_AFTER = 2
#: 失败路径第二段：赔付方要 99 次才给终态，而只许问 3 次 —— 保证**一定**问不出来。
SETTLE_AFTER_SILENT = 99
MAX_POLLS = 3

# ---- 失败路径第一段：明确拒付 ----------------------------------------------
#: X12 CARC 96「Non-covered charge(s)」—— 终态拒赔那一格：不在保障范围内，
#: 补什么都改不了结论，重报必然撞同一个码。码表判据一律查
#: `maos/tools/claim_codes.py`，不在这里凭语感另定。
CLAIM_ID_2 = "clm-s8-0002"
AMOUNT_CLAIMED_2 = 8600.00
DENIAL_CARC = "96"
#: 96 的官方描述明写「At least one Remark Code must be provided」，所以回执必须带 RARC。
#: 缺了 `MockPayer` 当场抛 —— 造一份不合规范的回执比不造更坏。
DENIAL_RARC = ("N130",)
#: 承担方取 PI（Payor Initiated Reduction）：赔付方单方面认定不在保障范围，
#: 既没有合同价可依（CO），也不该转嫁给被保险人（PR）。
DENIAL_GROUP = "PI"

# ---- 失败路径第二段：问不出终态 ---------------------------------------------
CLAIM_ID_3 = "clm-s8-0003"
AMOUNT_CLAIMED_3 = 7400.00

APPROVER = "沈思锴"
REJECT_REASON_2 = "赔付方判定不在保障范围（CARC 96），转人工向被保险人解释并结案"
REJECT_REASON_3 = "轮询三次仍问不出终态，下落不明，转人工到赔付方对账"

GOAL = "处理住院医疗费用理赔：三源报案聚合，按投保当时锁定的条款版本裁定并赔付"
GOAL_2 = "处理第二笔住院医疗理赔：条款与金额均无异议，但赔付方判定该项不在保障范围"
GOAL_3 = "处理第三笔住院医疗理赔：赔付方受理后迟迟不给终态"

# ------------------------------------------------------------------ 多源报案
# 三条说同一件事（标题归一化后相同），第四条是另一件事不该被并掉。
SIGNALS = [
    {"source": "工单系统", "kind": "ticket", "severity": "major",
     "title": "住院医疗费用理赔",
     "detail": "工单 C-30871：被保险人 6/20 因急性阑尾炎住院，申请住院医疗费用理赔"},
    {"source": "客服记录", "kind": "csr_note", "severity": "major",
     "title": "住院医疗费用理赔 ",
     "detail": "客服 0622 通话记录：被保险人口述同一次住院，确认出险时间与金额"},
    {"source": "定损照片", "kind": "image", "severity": "major",
     "title": "住院医疗费用理赔",
     "detail": "被保险人上传的住院病历与费用清单影像",
     "uri": "oss://claims/clm-s8-0001/bill-01.jpg",
     "digest": "sha256:demo-bill-01", "evidence_id": "ev-01"},
    {"source": "客服记录", "kind": "csr_note", "severity": "minor",
     "title": "随行家属交通费能否报销",
     "detail": "同一通电话里被保险人另问的一件事 —— 与住院费用不是同一个诉求"},
]

SIGNALS_2 = [
    {"source": "工单系统", "kind": "ticket", "severity": "major",
     "title": "住院医疗费用理赔", "detail": "工单 C-30902：被保险人申请理赔",
     "evidence_id": "ev-11"},
    {"source": "定损照片", "kind": "image", "severity": "major",
     "title": "住院医疗费用理赔", "detail": "费用清单影像",
     "uri": "oss://claims/clm-s8-0002/bill-01.jpg",
     "digest": "sha256:demo-bill-11", "evidence_id": "ev-12"},
]

SIGNALS_3 = [
    {"source": "工单系统", "kind": "ticket", "severity": "major",
     "title": "住院医疗费用理赔", "detail": "工单 C-30955：被保险人申请理赔",
     "evidence_id": "ev-21"},
    {"source": "定损照片", "kind": "image", "severity": "major",
     "title": "住院医疗费用理赔", "detail": "费用清单影像",
     "uri": "oss://claims/clm-s8-0003/bill-01.jpg",
     "digest": "sha256:demo-bill-21", "evidence_id": "ev-22"},
]

#: 赔付明细行。三项加总恰好等于申报总额 —— 分摊那一步的余数逻辑才有得可校。
CLAIM_LINES = [
    {"line_no": 1, "item_code": "MED-HOSP", "description": "住院床位与治疗费",
     "amount_claimed": 8000.00},
    {"line_no": 2, "item_code": "MED-DRUG", "description": "药品费",
     "amount_claimed": 3000.00},
    {"line_no": 3, "item_code": "MED-EXAM", "description": "检查检验费",
     "amount_claimed": 1000.00},
]

# ------------------------------------------------------------------------ 条款
# CL-01 的 v1 与 v2 **生效区间完全相同，只有版本号不同** —— 把 v2 排除在外的
# 只可能是「保单锁定了 v1」，不可能是时间过滤的副产品。这是本场景招牌判据的
# 实验设计，改了区间就把变量弄混了。
#
# EX-09 是除外责任条款，`loss_scope='pre_existing'` —— 本案 loss_type 是 illness，
# 所以不命中。它留在这里不是摆设：`test_claim_adjudication.py` 用它校验
# 「除外条款压过承保条款」那条判定顺序。
POLICY_TERMS = [
    ("CL-01", 1, "住院医疗费用按 90% 赔付，年度起付线 1000",
     {"coinsurance_rate": 0.9, "deductible": 1000},
     "2020-01-01T00:00:00+00:00", None, "*", "*"),
    ("CL-01", 2, "住院医疗费用按 70% 赔付，年度起付线 3000（2025 改版）",
     {"coinsurance_rate": 0.7, "deductible": 3000},
     "2020-01-01T00:00:00+00:00", None, "*", "*"),
    ("EX-09", 1, "既往症除外责任",
     {}, "2020-01-01T00:00:00+00:00", None, "*", "pre_existing"),
]

# ------------------------------------------------------------------- DAG 与脚本
TASK_INTAKE = "task-s8-intake"
TASK_ADJUDICATE = "task-s8-adjudicate"
TASK_SETTLE = "task-s8-settle"
TASK_PAY = "task-s8-pay"


def _tasks(*, claim_id: str, amount: float, signals: list[dict], goal_suffix: str,
           payer_name: str, prefix: str) -> list[dict]:
    """造一份四任务 DAG。三条路径同构，只换案子、金额与赔付方。

    `effect_risk` **按金额现算**（`C.needs_human_approval`），不写死成 "H"：
    「超阈值才停下来等人批」这件事必须是一条可以被单独校验的判据，写死就只是
    演示里恰好停了一下，换个金额也照停 —— 那不叫阈值。
    """
    over = C.needs_human_approval(amount)
    return [
        {"task_id": f"{prefix}-intake", "role": ROLE_INTAKE,
         "title": f"受理三源报案并聚合证据{goal_suffix}",
         "inputs": {"biz_type": C.BIZ_TYPE, "signals": signals,
                    "claim_lines": CLAIM_LINES if claim_id == CLAIM_ID else None,
                    "reported_at": REPORTED_AT,
                    "case_seed": {
                        "tenant_id": TENANT_ID, "claim_id": claim_id,
                        "payer_id": PAYER_ID, "policy_no": POLICY_NO,
                        "policy_version": POLICY_VERSION, "loss_type": LOSS_TYPE,
                        "incident_at": INCIDENT_AT, "amount_claimed": amount}},
         "acceptance": ["三源报案去重后建出 claim_case", "证据与明细行落库"],
         "depends_on": [], "risk_level": "L"},

        {"task_id": f"{prefix}-adjudicate", "role": ROLE_ADJUDICATOR,
         "title": "按投保当时锁定的条款版本裁定赔付责任",
         "inputs": {"biz_type": C.BIZ_TYPE, "tenant_id": TENANT_ID, "claim_id": claim_id},
         "acceptance": ["按保单快照锁定的条款版本判定", "裁定产物带条款编号与版本"],
         "depends_on": [f"{prefix}-intake"], "risk_level": "L"},

        # 核算任务：金额超阈值就停下来等人批。**这一步的审批才是「批不批这笔钱」**，
        # `claim.pay` 会去核对它落下的 claim_approval，没有就拒绝发起赔付。
        {"task_id": f"{prefix}-settle", "role": ROLE_SETTLEMENT,
         "title": "核算赔款并逐行分摊",
         "inputs": {"biz_type": C.BIZ_TYPE, "tenant_id": TENANT_ID, "claim_id": claim_id,
                    "amount_claimed": amount},
         "acceptance": ["产出 settlement 且与库表一致", "赔款按锁定的条款版本核算"],
         "depends_on": [f"{prefix}-adjudicate"], "risk_level": "M",
         "effect_risk": "H" if over else "L"},

        # 付款任务同样 effect_risk=H：把钱打出去是不可逆的落地动作，Gate 过了也要人
        # 放行。失败路径的转折点就在这里 —— 主管拿到的不是「到账」，是一份带 CARC 的
        # 拒付回执（或一份根本没问出终态的观察），于是他驳回。
        # **没有这一步，denied 会被当成「还在处理中」一路挂着。**
        {"task_id": f"{prefix}-pay", "role": ROLE_PAYMENT,
         "title": "发起赔付并观察赔付方终态",
         "inputs": {"biz_type": C.BIZ_TYPE, "tenant_id": TENANT_ID, "claim_id": claim_id,
                    "payer": payer_name, "max_polls": MAX_POLLS},
         "acceptance": ["发起后不得写 paid", "到账必须由 query 观察得到"],
         "depends_on": [f"{prefix}-settle"], "risk_level": "M", "effect_risk": "H"},
    ]


PLAN_JSON = json.dumps({"tasks": _tasks(
    claim_id=CLAIM_ID, amount=AMOUNT_CLAIMED, signals=SIGNALS, goal_suffix="",
    payer_name=PAYER_HAPPY, prefix="task-s8")}, ensure_ascii=False)

REVIEW_JSON = json.dumps({
    "defects": [],
    "conclusion": f"赔款按保单锁定的条款 v{TERMS_VERSION_AT_BIND} 核算，依据 CL-01@v1；"
                  f"赔付尚未发起，可放行",
}, ensure_ascii=False)

# 查表顺序即分派规则：ScriptedModelClient 返回**第一个**命中的关键字，专用的排前面。
SCRIPT = {
    "语义审查产物清单": REVIEW_JSON,
    "用户请求": PLAN_JSON,
}

# ---- 编排层自己的 identity：补偿收口是**人的决定之后**的动作，不属于任何 Agent ----
# 与其给理赔域再加一个 Agent（那会让「补偿是谁做的」这个问题多一个含糊的答案），
# 不如让编排层带一个只有补偿权限的 identity —— 白名单机制正是用来表达这种最小授权的。
# 口径同 scenario_7 的 COMPENSATION_IDENTITY。
COMPENSATION_IDENTITY = AgentIdentity(
    agent_id="claim-compensation-desk",
    role="claim_compensation",
    duty="赔付走不通之后的域内补偿收口：留档最后观察、开人工工单、推进 compensated",
    allowed_skills=frozenset({"claim.compensate"}),
    allowed_tools=frozenset(),
    write_scope=frozenset(),
    max_risk="M",
    model_tier=Tier.LIGHT,
)


# ---------------------------------------------------------------------- 靶场数据
def require_threshold_below_amounts() -> None:
    """开跑前先确认三笔金额都超得过人工审批阈值。

    本场景的三条链路都指望核算那一步停下来等人批 —— 人批的那一刻才落
    `claim_approval`，而 `claim.pay` 没有它就拒绝发起赔付。阈值被
    `MAOS_CLAIM_APPROVAL_THRESHOLD` 调到三笔金额之上时链路确实走不完，
    这里当场说清是**阈值**的事：不拦的话，症状会显示成「付款任务失败：没有
    approved 的审批记录」，那句话指向审批记录，离原因隔着一层。

    检查放在**运行期**，不放在 `_tasks()` 里：`PLAN_JSON` 在模块级就调了 `_tasks()`，
    在那里抛会让一台设了这个环境变量的机器连 import 都过不去 —— 测试收集期集体失败，
    比运行期失败更难查。（`maos/tests/conftest.py` 目前不剥这个变量，已记
    `docs/BACKLOG.md ## task-T37`。）
    """
    amounts = (AMOUNT_CLAIMED, AMOUNT_CLAIMED_2, AMOUNT_CLAIMED_3)
    low = [a for a in amounts if not C.needs_human_approval(a)]
    if low:
        raise RuntimeError(
            f"场景 8 的金额 {low} 没有超过人工审批阈值 {C.approval_threshold()}，"
            f"链路走不完（核算那一步不会停下来等人批，claim.pay 就拿不到审批记录）"
            f"—— 检查环境变量 {C.ENV_APPROVAL_THRESHOLD}")


def seed_domain(store) -> None:
    """预置赔付方、保单快照与条款 —— 它们是**读到的那一版**，不是外部系统的当前值。"""
    objects.ensure_schema(store)
    objects.execute(store, "INSERT OR REPLACE INTO payer (tenant_id, payer_id, kind, name)"
                           " VALUES (?,?,?,?)",
                    (TENANT_ID, PAYER_ID, "insurer", "示例人寿杭州分公司"))
    objects.execute(
        store,
        "INSERT OR REPLACE INTO policy_contract (tenant_id, policy_no, version, product_code,"
        " insured_id, sum_insured, deductible, coinsurance_rate, bound_at,"
        " terms_version_at_bind, payer_id, payload_json, read_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (TENANT_ID, POLICY_NO, POLICY_VERSION, "MED-INPATIENT", "insured-8842",
         SUM_INSURED, POLICY_DEDUCTIBLE, POLICY_COINSURANCE, BOUND_AT,
         TERMS_VERSION_AT_BIND, PAYER_ID, "{}", C.now_iso()))
    for rule_no, version, title, params, eff_from, eff_to, product, loss in POLICY_TERMS:
        objects.execute(
            store,
            "INSERT OR REPLACE INTO policy_terms (tenant_id, rule_no, version, title, body,"
            " effective_from, effective_to, product_scope, loss_scope)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (TENANT_ID, rule_no, version, title,
             json.dumps(params, ensure_ascii=False, sort_keys=True),
             eff_from, eff_to, product, loss))


def receipt_artifact(store, task_id: str) -> dict:
    """取付款任务**最近一轮**的回执产物 —— 主管就是看着它做处置决定的。

    按 `version`（= 产出它的那次 attempt）取最大的一份，不取列表里的第一份：
    返工之后这个任务会有多份回执，而主管处置的依据只能是**最后那一份**。
    """
    arts = [a for a in store.list_artifacts(task_id) if a["kind"] == KIND_PAYER_RECEIPT]
    if not arts:
        raise LookupError(f"{task_id} 没有回执产物 —— 付款任务应当产出 {KIND_PAYER_RECEIPT}")
    return max(arts, key=lambda a: a["version"])["content"]


def compensate(store, *, claim_id: str, plan_id: str, task_id: str, trace_id: str,
               operator: str, reason: str) -> dict:
    """编排层以最小授权 identity 调 claim.compensate。

    走 SkillInvoker 而不是直接 `ClaimCompensateSkill().run()`：白名单校验与
    SkillInvoked 审计行都在 invoker 里，直接调就没有审计行，出事之后查不到是谁做的。
    """
    invoker = SkillInvoker(COMPENSATION_IDENTITY, store)
    res = invoker.invoke("claim.compensate", {
        "tenant_id": TENANT_ID, "claim_id": claim_id,
        "operator": operator, "reason": reason, "assignee": operator,
    }, extras={"plan_id": plan_id, "task_id": task_id, "trace_id": trace_id})
    if res.status != "ok" or not isinstance(res.output, dict):
        raise RuntimeError(f"域内补偿失败，不许静默收口：{res.error}")
    return res.output


def _count(store, sql: str, params: tuple = ()) -> int:
    return objects.query(store, sql, params)[0]["n"]


def paid_observations(store) -> int:
    """**全库**的到账观察行数。失败路径的第一断言数的就是它。

    刻意数全库而不是按 claim 收窄：按 claim 收窄的话，某个别的案子被越权写成 paid
    这件事就漏掉了，而那正是这条断言要抓的。
    """
    return _count(
        store,
        "SELECT COUNT(*) AS n FROM claim_payment_observation WHERE observed_state='paid'")


#: 「这个 Agent 自称干完了」在 event_log 里的样子。
#:
#: 控制面的 `on_task_result` 只有在 `payload["status"] == "ok"` 时才把任务推到
#: `AWAITING_REVIEW` 并把 reason 写成 `submit_result`；`blocked` 走 BLOCKED、
#: `failed` 走 REWORK/FAILED。所以这一对 (to_state, reason) 就是 Agent 自述成功的
#: 机器判据。
#:
#: **不数 `TaskResult`**：那是总线上的 Envelope，不落 event_log（实测该 Plan 的
#: event_type 分布里根本没有它），照着数会得到恒 0/0 —— 一条恒真的断言。
_AGENT_OK_TO_STATE = TaskState.AWAITING_REVIEW
_AGENT_OK_REASON = "submit_result"


def _all_agents_ok(store, plan_id: str) -> tuple[int, int]:
    """`(自称干完了的任务数, 任务总数)`。

    判据取 Agent 自己交回的结论，**不取任务最终状态**：任务状态是**控制面**的结论，
    Agent 说了什么在那之前 —— 而失败路径要证的正是「Agent 都说 ok，业务照样没成」，
    拿控制面的结论去数就把这句话数没了。

    按 task_id 去重：返工会让同一个任务交回多次，那仍然是一个任务。
    """
    said_ok = {e["task_id"] for e in store.list_event_log(plan_id)
               if e["event_type"] == "StateTransition"
               and e["to_state"] == _AGENT_OK_TO_STATE
               and e["reason"] == _AGENT_OK_REASON}
    total = len(store.list_tasks(plan_id))
    return len(said_ok), total


def _closure_block(store, *, claim_id: str, plan_id: str, cp) -> dict:
    """打印并返回失败路径的收口 —— 两段共用一份，不各写一套措辞。"""
    case = guard.get_case(store, TENANT_ID, claim_id)
    paid_rows = paid_observations(store)
    comp_rows = objects.query(
        store,
        "SELECT * FROM claim_compensation WHERE tenant_id=? AND claim_id=? ORDER BY kind",
        (TENANT_ID, claim_id))
    obs_rows = objects.query(
        store,
        "SELECT observed_state, carc_code FROM claim_payment_observation"
        " WHERE tenant_id=? AND claim_id=?", (TENANT_ID, claim_id))
    plan = cp.store.get_plan(plan_id)
    ok, total = _all_agents_ok(store, plan_id)

    print(f"\n  业务状态  : {case['biz_status']}（全程没有经过 paid）")
    print(f"  paid 观察: {paid_rows} 条 —— 没问出终态就一条都不该有")
    print(f"  补偿记录  : {len(comp_rows)} 行 {[r['kind'] for r in comp_rows]}")
    print(f"  Plan 终态 : {plan['state']}（主管驳回，业务确实没成功）")
    print(f"  本案观察  : {len(obs_rows)} 条 "
          f"{[(r['observed_state'], r['carc_code']) for r in obs_rows]}")
    print(f"  Agent 自述: {ok}/{total} 个任务回报 ok —— "
          f"「都说完成了」和「业务成功了」是两件事")
    return {"case": case, "paid_rows": paid_rows, "comp_rows": comp_rows,
            "obs_rows": obs_rows, "plan": plan, "agents_ok": (ok, total)}


# ------------------------------------------------------------------ 顺利路径
def drive_happy(*, matrix: bool = False) -> dict:
    """跑完顺利路径并返回收口用的句柄。**只跑不断言** —— 断言在 `run()` 与测试里。"""
    print("场景 8：保险理赔（顺利路径），无 key 确定性复现")
    require_threshold_below_amounts()

    model = select_model_client(SCRIPT, force_scripted=True)
    store, bus, cp, model, worker, gate = build(SCRIPT, matrix=matrix, model=model)
    seed_domain(store)

    C.reset_payers()
    C.register_payer(PAYER_HAPPY, MockPayer(settle_after=SETTLE_AFTER))

    # —— Manager 零改动复用：为代码域写的规划器，在理赔域照样规划 DAG ——
    # 先生成两个 id 再规划：`mgr.plan()` 是 create_plan 的入参，跑在建 Plan 之前，
    # 不先拿到 id，这次规划的用量就只能落空串，成为 trace 里认领不了的游离事件。
    trace_id, plan_id = new_id("trace"), new_id("plan")
    mgr = ManagerAgent(model, store=store)
    cp.create_plan(goal=GOAL, trace_id=trace_id, plan_id=plan_id,
                   tasks=mgr.plan(GOAL, context={"plan_id": plan_id,
                                                 "trace_id": trace_id}))
    cp.start_plan(plan_id)
    run_until_settled(bus, gate, cp, plan_id)

    hq = HumanApprovalQueue(store, cp)

    # —— 第一次人工介入：赔款核算超阈值，等人批 ——
    pending = hq.pending(plan_id)
    assert [t["task_id"] for t in pending] == [TASK_SETTLE], (
        f"应停在赔款核算的人工审批上，实际 {[t['task_id'] for t in pending]}")
    settle_task = pending[0]
    print(f"\n[1] 待主管审批: {settle_task['title']}"
          f"（赔款 {EXPECTED_ALLOWED} > 阈值 {C.approval_threshold():.0f}）")

    reviewer = ReviewerAgent(model, store=store)
    note = review_after_gate(reviewer, cp, plan_id, host_task=settle_task)
    print(f"    语义审查: {note.artifacts[0]['content']['conclusion']}")

    # 审批是**人**的动作：先落 claim_approval，再放行任务。顺序不可换 ——
    # claim.pay 会核对审批记录，没有它就拒绝发起赔付。
    C.record_approval(store, tenant_id=TENANT_ID, claim_id=CLAIM_ID, approver=APPROVER,
                      decision="approved",
                      reason=f"赔款 {EXPECTED_ALLOWED} 与投保锁定的条款 v"
                             f"{TERMS_VERSION_AT_BIND} 一致")
    hq.decide(TASK_SETTLE, approved=True, operator=APPROVER, note="已核对条款版本与金额")
    run_until_settled(bus, gate, cp, plan_id)

    # —— 到账是**问**出来的 ——
    receipt = receipt_artifact(store, TASK_PAY)
    print(f"\n[2] 赔付方回执: observed_state={receipt['observed_state']}"
          f"（问了 {receipt['poll_count']} 次问出来的，不是发出去就算数）")
    print(f"    biz_status={receipt['biz_status']} —— 全系统只有 claim.observe 写得进 paid")

    # —— 第二次人工介入：付款任务 effect_risk=H，收口也要人点头 ——
    pending = hq.pending(plan_id)
    assert [t["task_id"] for t in pending] == [TASK_PAY], (
        f"付款任务应停在 BLOCKED 等人收口，实际 {[t['task_id'] for t in pending]}")
    print(f"\n[3] 待主管收口: {pending[0]['title']} —— 回执已到账，可放行")
    hq.decide(TASK_PAY, approved=True, operator=APPROVER, note="已核对到账回执")
    bus.drain()

    dump(cp, plan_id, "场景 8：保险理赔（顺利路径）")
    return {"store": store, "cp": cp, "bus": bus, "gate": gate, "hq": hq,
            "plan_id": plan_id, "trace_id": trace_id, "receipt": receipt}


# ------------------------------------------------------------------ 失败路径
def drive_failure(*, matrix: bool = False) -> dict:
    """跑完失败路径两段并返回收口用的句柄。**只跑不断言。**

    自己建一套运行时，不复用顺利路径那套：本路径最硬的一条断言是
    **全库 `paid` 观察 0 条**，与顺利路径共用 store 的话它必然为 1，
    那条断言就只能退化成按 claim 收窄的弱版本。
    """
    print(f"\n{'=' * 68}\n场景 8：保险理赔（失败路径）—— 赔付方没说到账，系统就一个字都不写"
          f"\n{'=' * 68}")
    require_threshold_below_amounts()

    model = select_model_client(SCRIPT, force_scripted=True)
    store, bus, cp, model, worker, gate = build(SCRIPT, matrix=matrix, model=model)
    seed_domain(store)

    C.reset_payers()
    # 两个赔付方实例，同一个 MockPayer 类，行为一行没改 —— 换的是注进去的码与时序。
    C.register_payer(PAYER_DENY, MockPayer(
        settle_after=SETTLE_AFTER,
        script={CLAIM_ID_2: DENIAL_CARC},
        group_script={CLAIM_ID_2: DENIAL_GROUP},
        remark_script={CLAIM_ID_2: DENIAL_RARC}))
    C.register_payer(PAYER_SILENT, MockPayer(settle_after=SETTLE_AFTER_SILENT))

    first = _run_failure_leg(
        store=store, bus=bus, cp=cp, gate=gate, model=model,
        goal=GOAL_2, claim_id=CLAIM_ID_2, amount=AMOUNT_CLAIMED_2,
        signals=SIGNALS_2, payer_name=PAYER_DENY, prefix="task-s8b",
        reject_reason=REJECT_REASON_2,
        headline=f"第一段：赔付方返 CARC {DENIAL_CARC} 而不是到账")

    second = _run_failure_leg(
        store=store, bus=bus, cp=cp, gate=gate, model=model,
        goal=GOAL_3, claim_id=CLAIM_ID_3, amount=AMOUNT_CLAIMED_3,
        signals=SIGNALS_3, payer_name=PAYER_SILENT, prefix="task-s8c",
        reject_reason=REJECT_REASON_3,
        headline=f"第二段：轮询 {MAX_POLLS} 次仍问不出终态")

    return {"store": store, "cp": cp, "denied": first, "unobserved": second}


def _run_failure_leg(*, store, bus, cp, gate, model, goal: str, claim_id: str,
                     amount: float, signals: list[dict], payer_name: str, prefix: str,
                     reject_reason: str, headline: str) -> dict:
    """跑一段失败路径。两段只差赔付方与驳回理由，走同一段代码。

    不走 ManagerAgent 出方案：`SCRIPT` 是按关键字查表的，再塞两份方案 JSON 进去就得
    靠提示词里的关键字来分派，那是拿确定性换省事。这里直接把规格交给 `create_plan`
    —— 控制面本来就收规格列表，Manager 只是规格的一种来源。
    """
    print(f"\n{'-' * 68}\n{headline}\n{'-' * 68}")

    trace_id = new_id("trace")
    tasks = _tasks(claim_id=claim_id, amount=amount, signals=signals, goal_suffix="",
                   payer_name=payer_name, prefix=prefix)
    plan_id = cp.create_plan(goal=goal, trace_id=trace_id, tasks=tasks)
    cp.start_plan(plan_id)
    run_until_settled(bus, gate, cp, plan_id)

    hq = HumanApprovalQueue(store, cp)

    # —— 第一次人工介入：核算照旧走超阈值审批那道门，一道都没改 ——
    pending = hq.pending(plan_id)
    assert [t["task_id"] for t in pending] == [f"{prefix}-settle"], (
        f"应先停在赔款核算的人工审批上，实际 {[t['task_id'] for t in pending]}")
    print(f"\n[a] 待主管审批: {pending[0]['title']}（既有的 effect_risk=H 入口，未改动）")

    reviewer = ReviewerAgent(model, store=store)
    review_after_gate(reviewer, cp, plan_id, host_task=pending[0])
    C.record_approval(store, tenant_id=TENANT_ID, claim_id=claim_id, approver=APPROVER,
                      decision="approved", reason="金额与投保锁定的条款版本一致")
    hq.decide(f"{prefix}-settle", approved=True, operator=APPROVER, note="已核对条款版本与金额")
    run_until_settled(bus, gate, cp, plan_id)

    # —— 赔付走不通 ——
    receipt = receipt_artifact(store, f"{prefix}-pay")
    print(f"\n[b] 赔付方回执: observed_state={receipt['observed_state']}"
          f"（问了 {receipt['poll_count']} 次）")
    if receipt["carc_code"]:
        print(f"    X12 判据: CARC {receipt['carc_code']} / 组码 {receipt['group_code']} "
              f"/ 备注码 {receipt['remark_codes']}")
        print(f"    官方描述: {receipt['description']}")
        print(f"    处置口径: recourse={receipt['recourse']}（MAOS 侧口径，非 X12 原文）")
        print(f"    出处    : {receipt['source']}（核对于 {receipt['fetched_at']}）")
    else:
        print(f"    赔付方一个调整码都没给 —— 纯粹是还没问出终态，"
              f"**不是拒付**，所以一行观察都不写")

    # —— 第二次人工介入：主管看着这份回执驳回 ——
    pending = hq.pending(plan_id)
    assert [t["task_id"] for t in pending] == [f"{prefix}-pay"], (
        f"付款任务应停在 BLOCKED 等人处置，实际 {[t['task_id'] for t in pending]}")
    print(f"\n[c] 待主管处置: {pending[0]['title']} —— 回执不是到账，不能当成功放行")

    C.record_approval(store, tenant_id=TENANT_ID, claim_id=claim_id, approver=APPROVER,
                      decision="rejected", reason=reject_reason)

    # 先补偿、再落 FAILED —— 与 control_plane.human_decision 同一个顺序与同一个理由：
    # 状态一旦落 FAILED，「外面还有一条下落不明的赔付指令」这件事就没人记得了。
    comp = compensate(store, claim_id=claim_id, plan_id=plan_id,
                      task_id=f"{prefix}-pay", trace_id=trace_id,
                      operator=APPROVER, reason=reject_reason)
    print(f"\n[d] 域内补偿: 作废 {len(comp['revoked'])} 条赔付指令，"
          f"最后观察到的下落 = {comp['last_observed_state']}"
          f"（CARC={comp['last_carc'] or '无'}）")
    print(f"    人工工单: {comp['ticket']['ticket_id']} -> {comp['ticket']['assignee']}")
    for line in comp["ticket"]["todo"]:
        print(f"      · {line}")

    hq.decide(f"{prefix}-pay", approved=False, operator=APPROVER, note=reject_reason)
    bus.drain()

    closure = _closure_block(store, claim_id=claim_id, plan_id=plan_id, cp=cp)
    dump(cp, plan_id, f"场景 8 失败路径 · {headline}")
    return {"plan_id": plan_id, "trace_id": trace_id, "receipt": receipt,
            "compensation": comp, **closure}


# -------------------------------------------------------------------------- run
def run(*, matrix: bool = False) -> int:
    """两条路径连跑并逐条自证。测试调它，也可以单独 `python3 -m maos.flows.scenario_8`。"""
    happy = drive_happy(matrix=matrix)
    _assert_happy(happy)

    fail = drive_failure(matrix=matrix)
    _assert_failure(fail)
    return 0


def _assert_happy(out: dict) -> None:
    store, cp, plan_id = out["store"], out["cp"], out["plan_id"]
    case = guard.get_case(store, TENANT_ID, CLAIM_ID)
    receipt = out["receipt"]

    # —— 条款版本锁定：本域的招牌判据 ——
    adjs = objects.query(
        store, "SELECT * FROM adjudication WHERE tenant_id=? AND claim_id=?",
        (TENANT_ID, CLAIM_ID))
    assert len(adjs) == 1, f"应恰好一条裁定，实际 {len(adjs)}"
    adj = adjs[0]
    assert adj["rule_no"] == "CL-01" and int(adj["terms_version"]) == TERMS_VERSION_AT_BIND, (
        f"裁定必须落在投保当时锁定的 CL-01@v{TERMS_VERSION_AT_BIND} 上，"
        f"实际 {adj['rule_no']}@v{adj['terms_version']} —— "
        f"用最新条款判一份 2023 年的保单，就是拿今天的规则追溯当年的承诺")
    assert f"{float(adj['allowed_amount']):.2f}" == EXPECTED_ALLOWED, (
        f"按锁定的 v1 应核出 {EXPECTED_ALLOWED}，实际 "
        f"{float(adj['allowed_amount']):.2f}；若得到 {IF_LATEST_TERMS_ALLOWED} "
        f"则说明用的是当前最新的 CL-01@v2")

    # —— 逐行分摊之和必须等于总额（分位余数落在最后一行）——
    lines = objects.query(
        store, "SELECT amount_allowed FROM claim_line WHERE tenant_id=? AND claim_id=?",
        (TENANT_ID, CLAIM_ID))
    total = sum(round(float(r["amount_allowed"]), 2) for r in lines)
    assert f"{total:.2f}" == EXPECTED_ALLOWED, (
        f"逐行赔付额之和 {total:.2f} 与总额 {EXPECTED_ALLOWED} 对不上 —— "
        f"分摊的分位余数没有落在最后一行")

    # —— 到账是问出来的，不是发出去就算数 ——
    assert case["biz_status"] == "paid", f"顺利路径应收敛到 paid，实际 {case['biz_status']}"
    assert receipt["poll_count"] == SETTLE_AFTER, (
        f"应恰好轮询 {SETTLE_AFTER} 次，实际 {receipt['poll_count']}")
    obs = objects.query(
        store,
        "SELECT * FROM claim_payment_observation WHERE tenant_id=? AND claim_id=?",
        (TENANT_ID, CLAIM_ID))
    assert len(obs) == 1 and obs[0]["observed_state"] == "paid", (
        f"paid 必须与恰好一条到账观察同事务落库，实际 "
        f"{[(o['observed_state']) for o in obs]}")
    assert obs[0]["actor_invocation_id"], "观察行必须指得回是哪一次调用问出来的"

    plan = cp.store.get_plan(plan_id)
    assert plan["state"] == PlanState.DONE, f"顺利路径 Plan 应 DONE，实际 {plan['state']}"
    _assert_no_new_task_states(cp, plan_id)


def _assert_failure(out: dict) -> None:
    store = out["store"]

    # —— 本路径存在的理由，第一断言：全库一条到账观察都没有 ——
    assert paid_observations(store) == 0, (
        f"失败路径全库不该有任何 paid 观察，实际 {paid_observations(store)} 条 —— "
        "有就说明有人在没问出到账的情况下把外部状态写死为终态了（铁律 8）")

    for label, leg, expect_carc, expect_last in (
        ("第一段（CARC 拒付）", out["denied"], DENIAL_CARC, "denied"),
        ("第二段（问不出终态）", out["unobserved"], "", UNOBSERVED),
    ):
        case, comp = leg["case"], leg["compensation"]
        assert case["biz_status"] == "compensated", (
            f"{label}：补偿之后业务状态应为 compensated，实际 {case['biz_status']}")

        kinds = {r["kind"] for r in leg["comp_rows"]}
        assert KIND_PAYMENT_REVOKED in kinds and KIND_MANUAL_TICKET in kinds, (
            f"{label}：补偿必须同时留下作废记录与人工工单，实际 {sorted(kinds)}")
        comp_events = [e for e in store.list_event_log(leg["plan_id"])
                       if e["event_type"] == "CompensationExecuted"]
        assert comp_events, f"{label}：补偿执行必须落 CompensationExecuted"

        assert comp["last_observed_state"] == expect_last, (
            f"{label}：最后观察应为 {expect_last}，实际 {comp['last_observed_state']} —— "
            "写成别的就是替赔付方下了它自己都没下的结论")
        assert comp["last_carc"] == expect_carc, (
            f"{label}：留档的 CARC 应为 {expect_carc!r}，实际 {comp['last_carc']!r}")

        assert leg["plan"]["state"] == PlanState.FAILED, (
            f"{label}：主管驳回后 Plan 应收敛到 FAILED，实际 {leg['plan']['state']}")

        # —— 第二样要买的东西：Agent 全说 ok，业务照样没成 ——
        ok, total = leg["agents_ok"]
        assert total >= 4 and ok == total, (
            f"{label}：四个 Agent 应全部回报 ok（实际 {ok}/{total}）—— "
            "本路径要证的正是「都说完成了」不等于「业务成功了」；"
            "把 Agent 改成 failed 反而会掩盖这句话")

        _assert_no_new_task_states(out["cp"], leg["plan_id"])

    # —— 两段各自的观察行形状 ——
    denied_obs = out["denied"]["obs_rows"]
    assert len(denied_obs) == 1 and denied_obs[0]["observed_state"] == "denied" \
        and denied_obs[0]["carc_code"] == DENIAL_CARC, (
        f"第一段应恰好留下一条 denied 观察并带 CARC {DENIAL_CARC}，实际 {denied_obs}")
    assert out["unobserved"]["obs_rows"] == [], (
        f"第二段一行观察都不该有 —— 「我问累了」不是一个可以落库的结论，"
        f"实际 {out['unobserved']['obs_rows']}")
    assert out["unobserved"]["receipt"]["poll_count"] == MAX_POLLS, (
        f"第二段应恰好轮询 {MAX_POLLS} 次，实际 "
        f"{out['unobserved']['receipt']['poll_count']}")


def _assert_no_new_task_states(cp, plan_id: str) -> None:
    """铁律 9：业务状态不进 Task 状态机，也没有为理赔域新开一条迁移。

    断言两件事而不是一件：只查状态集合挡不住「用既有的两个状态连一条新边」。
    """
    known = {v for k, v in vars(TaskState).items()
             if not k.startswith("_") and isinstance(v, str)}
    states = {t["state"] for t in cp.store.list_tasks(plan_id)}
    assert states <= known, f"出现了不在既有 Task 状态机内的状态：{sorted(states - known)}"
    for biz in ("paid", "compensated", "adjudicated", "payment_requested"):
        assert biz not in states, (
            f"{biz} 是 claim_case 自己的字段，不许变成 Task 状态（铁律 9）")
    moves = {(e["from_state"], e["to_state"]) for e in cp.store.list_event_log(plan_id)
             if e["event_type"] == "StateTransition"}
    assert moves <= set(TASK_TRANSITIONS), (
        f"出现了不在冻结迁移表里的 Task 迁移：{sorted(moves - set(TASK_TRANSITIONS))}")


if __name__ == "__main__":                       # pragma: no cover —— 手跑入口
    import sys

    sys.exit(run())
