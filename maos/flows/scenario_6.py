"""场景 6：制造企业售后退款 —— 顺利路径（手册的场景 R1，按 D-05 编号为 6）。

    多源诉求（工单 + 客服记录 + 图片证据）
      → refund.intake（复用 issue.aggregate 去重聚合，建案 submitted）
      → Manager 规划 DAG（**零改动复用**）
      → policy.match（按订单锁定的政策 v1 判，命中 AS- 规则）
      → finance.settle（核算 + 写 finance_entry 表 + 产物带 finance_entry 键）
      → Gate 六道闸
      → BLOCKED，等主管审批（本轮 CLI；Matrix 房间审批是 P4 的事）
      → payment.execute（gateway_accepted → processing，**写不出 settled**）
      → payment.observe（query 问到终态 → settled，回执同事务落库）
      → notify.customer（ack 缺失记 needs_followup，不阻塞）
      → Plan DONE

## 这个场景要证明的那句话

    同一个编排内核，换个领域只换 Skill / ToolPort / 业务对象，
    `contracts/` 与 `runtime/` 零改动。

所以这里刻意做了三件「不方便」的事：

  · **Manager 与 Reviewer 直接复用，一行新代码都不写** —— 它们是为代码域写的，
    在退款域里照样能规划 DAG、照样能出语义审查意见书。这是「角色抽象是配置不是
    代码」的实证，不是偷懒。
  · **业务状态不进 `contracts/states.py`** —— 退款的 submitted/approved/settled 全是
    `refund_case.biz_status`，Task 状态机一个新状态、一条新迁移都没加（铁律 9）。
  · **`maos/runtime/**` 不认识退款域** —— 运行时是领域无关内核，一旦 import 具体业务域，
    上面那句话当场作废。

## 无 key 可跑

`select_model_client(SCRIPT, force_scripted=True)`（A-12）：配了 key 的机器上也一行
网络都不走。评审现场没有 key 也必须看得到退款域，这是硬要求，不是省钱。

金额与政策版本都写死，`MockGateway(settle_after=2)` 让轮询次数固定 ——
连跑两次的输出必须逐条一致。
"""

from __future__ import annotations

import json

from maos.agents.manager import ManagerAgent
from maos.agents.refund import ROLE_FINANCE, ROLE_INTAKE, ROLE_PAYMENT, ROLE_POLICY
from maos.agents.reviewer import ReviewerAgent, review_after_gate
from maos.contracts.events import new_id
from maos.contracts.states import PlanState
from maos.domain.refund import guard, objects
from maos.flows.common import build, dump, run_until_settled
from maos.model.client import select_model_client
from maos.runtime.gate import HumanApprovalQueue
from maos.skills.builtin.refund import _common as C
from maos.tools.gateway import MockGateway

# ---------------------------------------------------------------------- 常量
# 全部写死，不用 new_id：本场景的验收之一是「连跑两次输出逐条一致」，
# 而 dump() 会打印 task_id（flows/common.py）。随机 id 会让两次输出必然不同。
TENANT_ID = "tnt-mfg-001"
CHANNEL_ID = "ch-tmall"
CASE_ID = "case-s6-0001"
ORDER_ID = "ord-s6-88231"
ORDER_VERSION = 1
SKU = "SKU-BRG-6204"
PAID_AT = "2026-07-01T10:00:00+00:00"
AMOUNT_PAID = 6800.00
AMOUNT_CLAIMED = 6800.00

GATEWAY_NAME = "s6-demo"
#: >1 才能证明「一次 query 不一定够」—— 终态是问出来的，不是一步返回的。
SETTLE_AFTER = 2

TASK_INTAKE = "task-s6-intake"
TASK_POLICY = "task-s6-policy"
TASK_FINANCE = "task-s6-finance"
TASK_PAYMENT = "task-s6-payment"
TASK_NOTIFY = "task-s6-notify"

APPROVER = "沈思锴"

GOAL = "处理客户对轴承订单的退款诉求：多源诉求已到，需按下单当时的政策核定并退款"

# ---------------------------------------------------------------- 多源退款诉求
# 三个口子说的是**同一件事**（"收到的轴承有锈蚀"），标题归一化后相同，
# issue.aggregate 会把它们并成一个 issue —— 「合并了几条」这个可观测量正是
# 多源聚合有没有真发生的证据。第四条是另一件事，不该被并掉。
SIGNALS = [
    {"source": "工单系统", "kind": "ticket", "severity": "major",
     "title": "收到的轴承有锈蚀", "detail": "工单 T-20887：客户反馈外圈有明显锈迹，要求全额退款"},
    {"source": "客服记录", "kind": "csr_note", "severity": "major",
     "title": "收到的轴承有锈蚀 ", "detail": "客服 0721 通话记录：客户口述同一问题，情绪平稳"},
    {"source": "客户上传", "kind": "image", "severity": "major",
     "title": "收到的轴承有锈蚀", "detail": "客户上传的实物照片",
     "uri": "oss://after-sales/case-s6-0001/rust-01.jpg",
     "digest": "sha256:demo-rust-01", "evidence_id": "ev-01"},
    {"source": "客户上传", "kind": "image", "severity": "minor",
     "title": "外包装箱破损", "detail": "运输箱一角压瘪",
     "uri": "oss://after-sales/case-s6-0001/box-01.jpg",
     "digest": "sha256:demo-box-01", "evidence_id": "ev-02"},
]

# ------------------------------------------------------------------ 政策两版
# 这是本场景最要紧的一处设计：**两版政策的生效区间完全相同**，唯一的区别是版本号。
# 于是把 v2 排除在外的只可能是「订单锁定了 v1」这一条，而不是日期过滤 ——
# 如果哪天有人把 policy.match 改成取 max(version)，这个场景的金额会立刻从
# 6800.00 变成 5390.00，一眼可见。
POLICY_RULES = [
    # rule_no, version, title, body(机器可读参数), effective_from, effective_to, channel, sku
    ("AS-01", 1, "整机质量问题全额退款",
     {"refund_ratio": 1.0, "deduct_fee": 0},
     "2026-01-01T00:00:00+00:00", None, "*", "*"),
    ("AS-01", 2, "整机质量问题退款扣除渠道手续费（新版）",
     {"refund_ratio": 0.8, "deduct_fee": 50},
     "2026-01-01T00:00:00+00:00", None, "*", "*"),
    # 售前规则：生效区间也覆盖本单，但前缀不是 AS-，不该参与售后裁定。
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

    # 只有这个任务带 amount_claimed：R-0 的第六道财务复核闸按
    # `biz_type == "refund" and amount_claimed > 阈值` 触发（F-1），而判据是
    # **同 attempt** 的产物里有没有 finance_entry —— 那份产物只有本任务产得出来。
    # 别的退款任务带上金额，就会被要求交一份它根本不产出的凭据，闸恒 blocker。
    {"task_id": TASK_FINANCE, "role": ROLE_FINANCE, "title": "核算退款金额并写财务分录",
     "inputs": {"biz_type": C.BIZ_TYPE, "amount_claimed": AMOUNT_CLAIMED,
                "tenant_id": TENANT_ID, "case_id": CASE_ID},
     "acceptance": ["产出 finance_entry 且与库表一致", "金额按锁定政策版本核算"],
     "depends_on": [TASK_POLICY], "risk_level": "M",
     # 产物落地（真的把钱退出去）是高风险动作：Gate 过了也不自动放行，转人工审批。
     "effect_risk": "H"},

    {"task_id": TASK_PAYMENT, "role": ROLE_PAYMENT, "title": "发起退款并观察网关终态",
     "inputs": {"biz_type": C.BIZ_TYPE, "tenant_id": TENANT_ID, "case_id": CASE_ID,
                "gateway": GATEWAY_NAME},
     "acceptance": ["发起后不得写 settled", "终态必须由 query 观察得到"],
     "depends_on": [TASK_FINANCE], "risk_level": "M"},

    {"task_id": TASK_NOTIFY, "role": ROLE_INTAKE, "title": "通知客户退款结果",
     "inputs": {"step": "notify", "biz_type": C.BIZ_TYPE,
                "tenant_id": TENANT_ID, "case_id": CASE_ID, "channel": "sms"},
     "acceptance": ["通知记录落库", "ack 缺失不阻塞"],
     "depends_on": [TASK_PAYMENT], "risk_level": "L"},
]

PLAN_JSON = json.dumps({"tasks": _TASKS}, ensure_ascii=False)

REVIEW_JSON = json.dumps({
    "defects": [],
    "conclusion": "金额按订单锁定的政策 v1 核算，依据 AS-01@v1；退款尚未发起，可放行",
}, ensure_ascii=False)

# 查表顺序即分派规则：ScriptedModelClient 返回**第一个**命中的关键字，
# 所以专用的排在前面。两个关键字互不为子串，且各自只出现在对应角色的 prompt 里
# （"语义审查产物清单" 是 reviewer.PROMPT_MARKER，"用户请求" 是 manager 的 prompt 前缀）。
SCRIPT = {
    "语义审查产物清单": REVIEW_JSON,
    "用户请求": PLAN_JSON,
}


# ---------------------------------------------------------------------- 靶场数据
def seed_domain(store) -> None:
    """预置外部系统快照与政策 —— 它们是**读到的那一版**，不是外部系统的当前值。

    `read_at` 记下读的时刻正是为此：MAOS 不持有权威事实（铁律 8），
    这些表里躺的永远只是「执行前读到的那一份」。
    """
    objects.ensure_schema(store)
    objects.execute(store, "INSERT OR REPLACE INTO tenant (tenant_id, name, region)"
                           " VALUES (?,?,?)", (TENANT_ID, "示例精密制造", "CN-EAST"))
    objects.execute(store, "INSERT OR REPLACE INTO channel (tenant_id, channel_id, kind, name)"
                           " VALUES (?,?,?,?)", (TENANT_ID, CHANNEL_ID, "marketplace", "天猫旗舰店"))
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
    _seed_kb(store)


def _seed_kb(store) -> dict:
    """给规划期检索一份**有内容**的知识库。返回 `{库存, 本租户}` 两个数。

    不播这一段，`KbRetrieved` 的 detail 里永远是 `docs: []`：接上检索只证明得了
    **链路通**，证明不了**召回准**（docs/BACKLOG.md `## task-X1` 第 1 条）。而评委
    跑 `run.py` 看到的是本场景，不是对照实验 R5。

    两批，缺一不可：

      · **W-1 语料的 16 条政策**（`scenarios/refund/`）**原样带着各自的租户**落库
        （tnt-mfg-a / tnt-mfg-b），不改写成本场景的租户 —— 理由见
        `kb/experiment.py:seed_kb_corpus`。它们是本场景**召不回**的那一批。
      · **本场景自己的 3 条政策**（`POLICY_RULES`），投影到 `TENANT_ID` 名下 ——
        这才是规划期检索真正召得回的那一批。

    于是候选集的形状本身就成了证据：库里 19 条，本租户 3 条进得了候选集，另外
    16 条一条都进不来。`docs: []` 变成有内容的同时，阶段一「跨租户永不召回」这条
    最硬的约束也在真实数据上被证明了一次，而不是只写在注释里。

    投影**复用 R5 那一份**（`kb/experiment.promote_policy_rule`），不在这里另写一套：
    两套投影迟早在字段口径上分叉，而症状只是「候选集少了些」，不报错。
    """
    # 局部 import：`kb.experiment` 是 R5 的证据生成器，把它挂到本模块的 import 图上，
    # `import maos.flows.scenario_6` 就会顺带拖进整个对照实验模块。
    from maos import kb
    from maos.kb import experiment

    kb.ensure_schema(store)
    corpus = experiment.seed_kb_corpus(store)
    for rule_no, version, title, params, eff_from, _eff_to, _ch, _sku in POLICY_RULES:
        experiment.promote_policy_rule(store, {
            "tenant_id": TENANT_ID, "rule_no": rule_no, "version": version,
            "title": title,
            # body 与 policy_rule 表里那一列**同一个串**：知识层与业务表读到的是
            # 同一份政策参数，两处各 dumps 一次迟早在键序上分叉。
            "body": json.dumps(params, ensure_ascii=False, sort_keys=True),
            "effective_from": eff_from,
        })
    return {"corpus": corpus, "own": len(POLICY_RULES)}


def _count(store, table: str) -> int:
    return objects.query(store, f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]


# -------------------------------------------------------------------------- run
def run(*, matrix: bool = False) -> int:
    print("场景 6：制造企业售后退款（顺利路径），无 key 确定性复现")

    model = select_model_client(SCRIPT, force_scripted=True)
    store, bus, cp, model, worker, gate = build(SCRIPT, matrix=matrix, model=model)
    seed_domain(store)

    # 网关按名取：task.inputs 会被 json.dumps，实例塞不进去（见 _common.py 第 3 条）。
    C.reset_gateways()
    C.register_gateway(GATEWAY_NAME, MockGateway(settle_after=SETTLE_AFTER))

    # —— Manager 零改动复用：为代码域写的规划器，在退款域照样规划 DAG ——
    # **带 store 构造**：不带的话 SkillInvoker.store is None，规划前检索恒返回空，
    # `MAOS_KB_ENABLED` 对这条链路没有任何影响。接上之后检索真的发生，
    # event_log 里落得下 KbRetrieved —— 关掉开关则一条都不落，对照才干净。
    #
    # context 只给规划期**此刻真知道**的四个维度：租户 / 业务类型 / 渠道 / SKU。
    # 政策版本与命中规则（AS-01）是 policy.match 后面才裁出来的，规划期传进来
    # 等于让它知道了它还不该知道的事 —— 检索上下文不是许愿池。
    # （`plan_id` / `trace_id` 不是检索维度：`manager._KB_QUERY_FIELDS` 不收它们，
    #  只用来给事件定归属，进不了检索查询。）
    trace_id = new_id("trace")
    # plan_id 先生成、规划期带着它跑：这次检索发生在 `create_plan` **之前**，
    # 不先拿到 id，KbRetrieved / SkillInvoked 就只能落空串，成为 trace 里认领不了的
    # 游离事件（docs/BACKLOG.md `## task-X4` 第 2 条）。归属不是硬凑的 ——
    # 这次检索检的正是这个 Plan 该怎么排。
    plan_id = new_id("plan")
    mgr = ManagerAgent(model, store=store)
    kb_context = {"tenant_id": TENANT_ID, "biz_type": C.BIZ_TYPE,
                  "channel_id": CHANNEL_ID, "sku": SKU,
                  "plan_id": plan_id, "trace_id": trace_id}
    cp.create_plan(goal=GOAL, trace_id=trace_id, plan_id=plan_id,
                   tasks=mgr.plan(GOAL, context=kb_context))
    cp.start_plan(plan_id)
    run_until_settled(bus, gate, cp, plan_id)

    # —— 停在人工审批 ——
    hq = HumanApprovalQueue(store, cp)
    pending = hq.pending(plan_id)
    assert len(pending) == 1, f"高风险的财务核算任务应停在 BLOCKED，实际 {len(pending)} 个"
    blocked = pending[0]
    print(f"\n待主管审批: {blocked['title']}")

    # —— Reviewer 零改动复用：挂在 Gate 之后、审批之前 ——
    reviewer = ReviewerAgent(model)
    note = review_after_gate(reviewer, cp, plan_id, host_task=blocked)
    print(f"语义审查: {note.artifacts[0]['content']['conclusion']}")

    # —— 审批是**人**的动作：先落 approval_record，再放行任务 ——
    # 顺序不可换：payment.execute 会核对审批记录，没有它就拒绝发起付款。
    C.record_approval(store, tenant_id=TENANT_ID, case_id=CASE_ID, approver=APPROVER,
                      decision="approved", reason="金额与订单锁定的政策 v1 一致")
    hq.decide(blocked["task_id"], approved=True, operator=APPROVER, note="已核对金额与政策版本")
    run_until_settled(bus, gate, cp, plan_id)

    # ---------------------------------------------------------------- 收口与断言
    dump(cp, plan_id, "场景 6：制造企业售后退款（顺利路径）")

    case = guard.get_case(store, TENANT_ID, CASE_ID)
    obs = objects.query(
        store, "SELECT * FROM payment_observation WHERE tenant_id=? AND case_id=?",
        (TENANT_ID, CASE_ID))
    entry = objects.query(
        store, "SELECT * FROM finance_entry WHERE tenant_id=? AND case_id=?",
        (TENANT_ID, CASE_ID))[0]
    refs = objects.list_business_refs(store, plan_id=plan_id)
    notifications = objects.query(
        store, "SELECT * FROM notification WHERE tenant_id=? AND case_id=?",
        (TENANT_ID, CASE_ID))

    breakdown = json.loads(entry["breakdown_json"])
    print(f"\n  业务状态: {case['biz_status']}（settled 只可能由 payment.observe 写入）")
    print(f"  核准金额: {breakdown['amount_approved']}"
          f"（政策 v{breakdown['policy_version']}，依据 {entry['rule_refs']}）")
    print(f"  支付观察: {len(obs)} 条，终态 {obs[-1]['observed_state']}，"
          f"actor={obs[-1]['actor_invocation_id'][:8]}…")
    print(f"  业务引用: {len(refs)} 条（DAG -> 业务对象，只存引用不存副本）")
    print(f"  客户通知: {len(notifications)} 条，"
          f"ack={'已确认' if notifications[0]['ack_at'] else '未确认（needs_followup，不阻塞）'}")

    plan = cp.store.get_plan(plan_id)
    assert plan["state"] == PlanState.DONE, f"退款顺利路径应收敛到 DONE，实际 {plan['state']}"
    assert case["biz_status"] == "settled", f"终值应为 settled，实际 {case['biz_status']}"
    assert obs, "settled 必须有同事务落库的支付观察，否则就是把外部状态写死为终态"
    assert refs, "business_ref 不能为空 —— DAG 与业务对象之间必须有引用"
    # 金额锁在 v1：若 policy.match 退化成取最新版本，这里会变成 5390.00。
    assert breakdown["amount_approved"] == "6800.00", (
        f"金额应按订单锁定的政策 v1 核算为 6800.00，实际 {breakdown['amount_approved']}；"
        "变成 5390.00 说明用了当前最新的 v2 政策")
    assert breakdown["policy_version"] == 1, "政策版本必须是订单锁定的 v1"
    assert notifications[0]["ack_at"] is None, "本场景刻意不给 ack，用来演示它不阻塞"
    return 0
