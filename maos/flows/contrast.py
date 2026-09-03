"""三组对照 case —— 租户 / 渠道 / 政策版本三个维度，同一个内核跑六个 case。

    R3  租户    唯一变量 tenant_id      租户 A 命中 30 天窗口 -> approve
                                        租户 B 同一条规则 7 天 -> reject
    R4  渠道    唯一变量 channel_id     经销渠道多命中 AS-004 -> 多一个核销任务、
                                        审批人换成区域经理；**金额两侧相同**
    R6  版本    下单时 v1、执行时已升 v2 按订单快照锁定的 v1 判 -> approve
                                        按库里最新的 v2 判 -> reject（差额 1280.00）

## 这三组要证明的那句话

场景 6 证明的是「换**域**只换 Skill / ToolPort / 业务对象」。这三组比它更进一步：
**同一个域内换三个维度，连 Skill 都没换** —— `maos/contracts/**` 与 `maos/core/**`
零改动，`policy.match` / `finance.settle` / `refund.intake` 一个字节没动，
Control Plane / 状态机 / Gate / Worker 更不认识「租户」「渠道」「版本」这三个词。

三组的差异全部来自**数据**：

  · R3 来自两个租户各自 `AS-001` 的 `no_reason_days` 参数；
  · R4 来自 `AS-004` 的 `channel_scope='ch-dealer'` —— `policy_rules_at_order`
    按渠道过滤，自营渠道压根取不到这条规则，于是它的 `extra_tasks` 也就无从展开；
  · R6 来自 `order_snapshot.policy_version_at_order`，`version <= pinned` 把 v2 挡在外面。

所以本模块里**没有一处 `if tenant == ...` / `if channel == ...` / `if version == ...`**。
规划期读到什么规则，就排出什么任务；读不到，那一步就不存在。**差异是规划出来的，
不是 if 出来的** —— 这是这三组唯一的含金量，写成 if 就一文不值。

## 判据只有一个来源

每份 case json 自带 `_expected` 块，把结论写死了。本模块**不另写一份期望值**：
两份期望值一定会漂，漂了之后测试绿而结论错，那比红更坏。

## 一处诚实的边界：窗口判定不在 `policy.match` 里

`policy.match` 的 approve/reject 只判「有没有命中 `AS-` 规则」，**没有**评估
`no_reason_days` 窗口（`scenarios/refund/README.md` 与 `docs/BACKLOG.md` 的
`## task-W1` 记着这条账）。它是 `maos/skills/**` 的面，本轨禁改，所以窗口判定
补在**流程层**（`evaluate_eligibility`）。

补的是判定，不是数据：窗口参数逐字取自 `policy.match` 出参的
`matched_rules[].params`，本模块一个数都不自造。两个裁定都会如实出现在结论里 ——
`policy_baseline` 是 skill 的原话，`decision` 是补上窗口之后的结论，
哪一条规则、哪一版、窗口几天、第几天申请，全部指名道姓。

## 无 key 可跑

`select_model_client(SCRIPT, force_scripted=True)`：配了 key 的机器上也一行网络
不走。task_id 全部由 case_id 推出（不用 `new_id`），连跑两次输出逐条一致。
"""

from __future__ import annotations

import json
import re
from datetime import datetime

from maos.agents.manager import ManagerAgent
from maos.agents.refund import ROLE_FINANCE, ROLE_INTAKE, ROLE_POLICY
from maos.contracts.events import new_id
from maos.contracts.states import PlanState
from maos.domain.refund import fixtures, guard, objects
from maos.flows.common import build, dump, run_until_settled
from maos.model.client import select_model_client
from maos.runtime.gate import HumanApprovalQueue
from maos.skills.builtin.refund import _common as C
from maos.skills.builtin.refund.policy import (
    AFTER_SALES_PREFIX,
    DECISION_APPROVE,
    DECISION_REJECT,
    rule_params,
    rule_ref,
)

#: 三组的展示口径。`dimension` 与 case json 的 `_expected.dimension` 同一份取值域。
GROUPS: tuple[tuple[str, str, str], ...] = (
    ("R3", "tenant", "租户维度（唯一变量 tenant_id）"),
    ("R4", "channel", "渠道维度（唯一变量 channel_id）"),
    ("R6", "policy_version", "政策版本维度（下单锁 v1，库里已升 v2）"),
)

#: 没有任何政策规则指定审批人时的兜底角色。R4A（自营）落在这里，
#: R4B（经销）由 `AS-004` 的 `approver_role` 覆盖成 `region_manager`。
DEFAULT_APPROVER_ROLE = "supervisor"

#: 审批是**人**的动作。名字写死，两次跑输出一致。
APPROVER = "沈思锴"

#: `_expected` 里机器可读的角色名长这样。写成中文说明（如 R4A 的
#: 「常规主管（无 AS-004）」）时不做等值比对，改判「与对照组不同且不是 region_manager」
#: —— 拿一句中文说明去和一个 role 名做 `==`，只能靠改判据才过，那是自欺。
_ROLE_TOKEN = re.compile(r"[a-z][a-z0-9_]*")

GOAL_TEMPLATE = ("处理 {tenant_id} 在 {channel_id} 渠道的退款诉求（{reason_code}）："
                 "需按下单当时锁定的政策版本裁定资格并核定金额")


# ---------------------------------------------------------------------------
# 政策视图 —— 全部走 R-1 的冻结口径，本模块不自写一行 SQL
# ---------------------------------------------------------------------------
def policy_view(store, *, tenant_id: str, order_id: str, order_version: int,
                prefix: str = AFTER_SALES_PREFIX) -> dict:
    """按订单快照锁定的政策版本取适用规则，并把 body 里的参数解出来。

    版本锁定与规则检索**一律调 `objects.policy_rules_at_order()`** —— 与
    `policy.match` 同一个函数，不另写一套。两份实现一定会在「≤ 锁定版本取每条规则
    的最大版本」这个细节上分叉，而分叉的症状是「金额算错了一点点」。

    `rule_ref` / `rule_params` 也直接借 `policy.match` 的那两个 —— `AS-001@v1`
    这个书写口径全仓只有一份，本模块跟着它走。
    """
    pinned = objects.pinned_policy_version(
        store, tenant_id=tenant_id, order_id=order_id, order_version=order_version)
    applicable = objects.policy_rules_at_order(
        store, tenant_id=tenant_id, order_id=order_id, order_version=order_version)
    matched = [r for r in applicable if str(r["rule_no"]).startswith(prefix)]
    return {
        "pinned": int(pinned),
        "rules": [{"rule_no": r["rule_no"], "version": int(r["version"]),
                   "title": r.get("title", ""), "ref": rule_ref(r),
                   "params": rule_params(r)} for r in matched],
    }


def policy_view_latest(store, *, tenant_id: str, order_id: str, order_version: int,
                       prefix: str = AFTER_SALES_PREFIX) -> dict:
    """**故意用错的那一版**：取 `max(policy_rule.version)`，无视订单锁定的版本。

    只给 R6 用，且只用来把「错误套用政策」这条路径真的走一遍。不这样跑一次，
    「按快照锁定的版本判」就只是一句自述 —— 陷阱得真踩得进去才叫证据。

    时间过滤照留（`effective_from <= paid_at`）：R6 的语料把 v2 的生效时刻设在
    订单支付**之前**，正是为了让 v2 能通过这道过滤 —— 唯一挡住它的必须是版本锁定
    本身，不是日期。
    """
    snap = objects.query(
        store,
        "SELECT sku, channel_id, paid_at FROM order_snapshot"
        " WHERE tenant_id=? AND order_id=? AND version=?",
        (tenant_id, order_id, int(order_version)))[0]
    rows = objects.query(
        store,
        "SELECT r.* FROM policy_rule r"
        " JOIN (SELECT rule_no, MAX(version) AS v FROM policy_rule"
        "        WHERE tenant_id=? GROUP BY rule_no) m"
        "   ON r.rule_no=m.rule_no AND r.version=m.v"
        " WHERE r.tenant_id=?"
        "   AND (r.channel_scope='*' OR r.channel_scope=?)"
        "   AND (r.sku_scope='*'     OR r.sku_scope=?)"
        "   AND r.effective_from<=?"
        "   AND (r.effective_to IS NULL OR r.effective_to>?)"
        " ORDER BY r.rule_no",
        (tenant_id, tenant_id, snap["channel_id"], snap["sku"], snap["paid_at"],
         snap["paid_at"]))
    matched = [r for r in rows if str(r["rule_no"]).startswith(prefix)]
    return {
        "pinned": max([int(r["version"]) for r in matched], default=0),
        "rules": [{"rule_no": r["rule_no"], "version": int(r["version"]),
                   "title": r.get("title", ""), "ref": rule_ref(r),
                   "params": rule_params(r)} for r in matched],
    }


# ---------------------------------------------------------------------------
# 规则参数 -> 规划期指令
# ---------------------------------------------------------------------------
def _applies_to(params: dict, reason_code: str) -> bool:
    """这条规则适不适用于本次诉求类型。

    `applies_when.reason_code` 没写就是不限（AS-004 的渠道差异对任何诉求都成立）。
    """
    cond = params.get("applies_when")
    if not isinstance(cond, dict):
        return True
    codes = cond.get("reason_code")
    if not codes:
        return True
    return reason_code in [str(c) for c in codes]


def policy_directives(rules: list[dict]) -> dict:
    """从命中规则的参数里读出「规划期该照做的事」。

    **逐条扫参数，不认渠道也不认租户**：读到 `extra_tasks` 就展开成任务，
    读到 `approver_role` 就换审批人。自营渠道之所以没有核销任务，是因为
    `AS-004` 压根没进 `rules`（`channel_scope` 在 `policy_rules_at_order`
    那一层就把它滤掉了），不是因为这里判了渠道。

    同一个 `task_key` 只展开一次：多条规则要求同一步时，那是同一步。
    `approver_role` 取**第一条**声明它的规则（规则按 `rule_no` 排序，口径确定）。
    """
    extra: list[dict] = []
    seen: set[str] = set()
    approver: str | None = None
    for rule in rules:
        params = rule.get("params") or {}
        for step in params.get("extra_tasks") or []:
            if not isinstance(step, dict):
                continue
            key = str(step.get("task_key") or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            extra.append({
                "task_key": key,
                "owner_role": str(step.get("owner_role") or ""),
                "title": str(step.get("title") or key),
                # 出处跟着任务走：核销任务落地时要说得出「是哪条规则要求的」，
                # 说不出的核销任务不该被规划出来（见 channel_agent.py）。
                "rule_ref": rule["ref"],
            })
        if approver is None and params.get("approver_role"):
            approver = str(params["approver_role"])
    return {"extra_tasks": extra, "approver_role": approver or DEFAULT_APPROVER_ROLE}


def evaluate_eligibility(rules: list[dict], *, reason_code: str,
                         elapsed_days: int) -> dict:
    """在 `policy.match` 的基线裁定之上补一层**窗口判定**。

    基线（`policy.match` 的口径，本模块不改它）：命中任一 `AS-` 规则即 approve。
    本层只做一件事：命中的规则里，凡是**适用于本次诉求类型**且声明了
    `no_reason_days` 的，逐条比对经过天数，超窗即 reject 并指名是哪一条。

    窗口天数取自规则参数，不是常量 —— R3 两个租户的差别正是这个数（30 vs 7），
    写死任何一个都会让这一组当场失去意义。
    """
    baseline = DECISION_APPROVE if rules else DECISION_REJECT
    for rule in rules:
        params = rule.get("params") or {}
        days = params.get("no_reason_days")
        if days is None or not _applies_to(params, reason_code):
            continue
        window = int(days)
        if elapsed_days > window:
            return {
                "decision": DECISION_REJECT, "policy_baseline": baseline,
                "rule_ref": rule["ref"], "no_reason_days": window,
                "why": f"{rule['ref']} 窗口 {window} 天，第 {elapsed_days} 天申请，"
                       f"{elapsed_days} > {window}",
            }
        return {
            "decision": DECISION_APPROVE, "policy_baseline": baseline,
            "rule_ref": rule["ref"], "no_reason_days": window,
            "why": f"{rule['ref']} 窗口 {window} 天，第 {elapsed_days} 天申请，"
                   f"{elapsed_days} ≤ {window}",
        }
    return {
        "decision": baseline, "policy_baseline": baseline,
        "rule_ref": None, "no_reason_days": None,
        "why": (f"命中 {len(rules)} 条 {AFTER_SALES_PREFIX} 规则，"
                f"其中没有适用于 {reason_code} 的时限规则，按基线裁定"
                if rules else f"没有适用的 {AFTER_SALES_PREFIX} 规则"),
    }


def elapsed_days(paid_at: str, requested_at: str) -> int:
    """支付到申请之间的整天数。两端都是 ISO8601 带时区。"""
    return (datetime.fromisoformat(requested_at) - datetime.fromisoformat(paid_at)).days


# ---------------------------------------------------------------------------
# 规划：骨架 + 政策展开
# ---------------------------------------------------------------------------
def _signals_of(payload: dict) -> list[dict]:
    """本次诉求的多源信号。工单一条 + case 自带的每份证据一条。

    工单那条的内容**全部由 case 数据推出**（诉求类型 / SKU / 申报金额），
    不现编情节；证据那几条由 `fixtures.evidence_signals_of` 逐字取自语料。
    """
    seed = fixtures.case_seed_of(payload)
    ticket = {
        "source": "工单系统", "kind": "ticket", "severity": "major",
        "title": f"{seed['sku']} 退款诉求（{seed['reason_code']}）",
        "detail": (f"订单 {seed['order_id']} 申请退款 {seed['amount_claimed']}，"
                   f"诉求类型 {seed['reason_code']}"),
    }
    return [ticket, *fixtures.evidence_signals_of(payload)]


def plan_tasks(*, seed: dict, signals: list[dict], directives: dict,
               decision: str) -> list[dict]:
    """本次 case 的 DAG。**任务集是政策的函数**，不是渠道/租户/版本的函数。

    骨架四步对六个 case 逐字节相同：受理 -> 裁定 -> （核算）-> 通知。
    两处、且只有两处随数据变：

      · `directives["extra_tasks"]` 展开出来的任务 —— 命中 `AS-004` 才有，
        它的 role 逐字取自规则里的 `owner_role`；
      · 裁定为 reject 时不排核算 —— 「不予退款」就没有金额要核，
        `finance.settle` 见到 reject 也会拒绝核算（给一个 0 元分录会让下游
        误以为「核算过了，只是金额为零」）。DAG 的形状本身就是裁定结论。

    `task_id` 由 case_id 推出而不是 `new_id`：本流程的验收之一是连跑两次输出
    逐条一致，而 `dump()` 会打印 task_id。
    """
    case_id = str(seed["case_id"])
    stem = f"task-{case_id.lower()}"
    shared = {"biz_type": C.BIZ_TYPE, "tenant_id": seed["tenant_id"], "case_id": case_id}

    intake, policy = f"{stem}-intake", f"{stem}-policy"
    tasks = [
        {"task_id": intake, "role": ROLE_INTAKE, "title": "受理多源退款诉求并聚合证据",
         "inputs": {**shared, "step": "intake", "signals": signals,
                    "case_seed": dict(seed)},
         "acceptance": ["多源诉求去重后建出 refund_case", "证据引用落库"],
         "depends_on": [], "risk_level": "L"},
        {"task_id": policy, "role": ROLE_POLICY,
         "title": "按下单锁定的政策版本裁定退款资格",
         "inputs": dict(shared),
         "acceptance": ["按订单快照锁定的政策版本判定", "给出命中的规则编号与版本"],
         "depends_on": [intake], "risk_level": "L"},
    ]

    extra_ids: list[str] = []
    for step in directives["extra_tasks"]:
        tid = f"{stem}-{step['task_key'].replace('_', '-')}"
        extra_ids.append(tid)
        tasks.append({
            "task_id": tid, "role": step["owner_role"], "title": step["title"],
            "inputs": {**shared, "channel_id": seed["channel_id"],
                       "task_key": step["task_key"], "rule_ref": step["rule_ref"]},
            "acceptance": [f"核销事项按 {step['rule_ref']} 登记", "保留规则出处"],
            "depends_on": [policy], "risk_level": "M"})

    tail = policy
    if decision == DECISION_APPROVE:
        finance = f"{stem}-finance"
        tasks.append({
            "task_id": finance, "role": ROLE_FINANCE, "title": "核算退款金额并写财务分录",
            # 申报金额**只挂在这一步**：第六道闸的任务级判据按
            # `biz_type + amount_claimed` 触发，而判据是同 attempt 的产物里有没有
            # finance_entry —— 那份产物只有本任务产得出来。别的任务带上金额，
            # 就会被要求交一份它根本不产出的凭据，闸恒 blocker。
            "inputs": {**shared, "amount_claimed": seed["amount_claimed"]},
            "acceptance": ["产出 finance_entry 且与库表一致", "金额按锁定政策版本核算"],
            # 产物落地（真的把钱退出去）是不可逆动作：Gate 过了也不自动放行，转人工。
            "depends_on": [policy, *extra_ids], "risk_level": "M", "effect_risk": "H"})
        tail = finance

    tasks.append({
        "task_id": f"{stem}-notify", "role": ROLE_INTAKE, "title": "通知客户裁定结果",
        "inputs": {**shared, "step": "notify", "channel": "sms"},
        "acceptance": ["通知记录落库", "ack 缺失不阻塞"],
        "depends_on": [tail], "risk_level": "L"})
    return tasks


# ---------------------------------------------------------------------------
# 单个 case
# ---------------------------------------------------------------------------
def run_case(filename: str, *, matrix: bool = False, verbose: bool = True) -> dict:
    """跑一个对照 case，返回**观测到的事实**（不含任何期望值）。

    判据比对由 `check_case()` 单独做 —— 观测与判定分开，观测这一步不许知道
    期望是什么，否则「跑出来的」和「想要的」就分不清了。
    """
    payload = fixtures.load_case(filename)
    seed = fixtures.case_seed_of(payload)
    expected = fixtures.expected_of(payload)
    tenant_id, case_id = str(seed["tenant_id"]), str(seed["case_id"])

    # 脚本是一个**可变 dict**：规划应答要等靶场装好、政策读出来之后才拼得出来，
    # 而 ScriptedModelClient 每次 complete() 才查表，所以先给占位、后填。
    #
    # 占位不能是空 dict：`ScriptedModelClient.__init__` 写的是 `script or {}`，
    # 空 dict 是 falsy，它会**另造一个**，于是后填的内容根本到不了客户端手里 ——
    # 症状是规划应答恒为 `{}`、Plan 里一个任务都没有，而且不报错。
    script: dict[str, str] = {"用户请求": "{}"}
    model = select_model_client(script, force_scripted=True)
    store, bus, cp, model, worker, gate = build(script, matrix=matrix, model=model)

    # ---- 靶场：本 case 的外部快照 + 知识库 ----
    # **不灌 `policy/policy_rules.json` 的 16 条政策**：每份 case json 自包含它用到的
    # `policy_rule` 行（`case_r1.json` 的形状要求），全量灌进去会让 R3 的两个 case
    # 额外命中 AS-002 / AS-003，「唯一变量是 tenant_id」当场不成立。
    # 知识库那一侧照灌全量 —— 检索本来就该在一个有别人家知识的库里做，
    # 跨租户召不回才有说服力。
    fixtures.seed_case(store, payload)
    fixtures.seed_policy_kb(store)
    fixtures.seed_history_kb(store)

    # ---- 规划期读政策：版本锁定 + 渠道过滤，全走冻结口径 ----
    view = policy_view(store, tenant_id=tenant_id, order_id=str(seed["order_id"]),
                       order_version=int(seed["order_version"]))
    directives = policy_directives(view["rules"])
    paid_at = objects.query(
        store, "SELECT paid_at FROM order_snapshot WHERE tenant_id=? AND order_id=?"
               " AND version=?",
        (tenant_id, seed["order_id"], int(seed["order_version"])))[0]["paid_at"]
    # `requested_at` 是本次诉求的时刻，语料把它放在 `_expected` 里（case 块没有这个字段，
    # 已记 BACKLOG）。**只取它当输入**，天数由它与库里的 paid_at 现算，不抄 elapsed_days。
    days = elapsed_days(paid_at, str(expected["requested_at"]))
    verdict = evaluate_eligibility(view["rules"], reason_code=str(seed["reason_code"]),
                                   elapsed_days=days)

    tasks = plan_tasks(seed=seed, signals=_signals_of(payload), directives=directives,
                       decision=verdict["decision"])
    # 「用户请求」是 ScriptedModelClient 的分派关键字，也是 manager prompt 的前缀。
    script["用户请求"] = json.dumps({"tasks": tasks}, ensure_ascii=False)

    # ---- Manager 零改动复用：为代码域写的规划器，在退款域照样规划 DAG ----
    goal = GOAL_TEMPLATE.format(**{k: seed[k] for k in
                                   ("tenant_id", "channel_id", "reason_code")})
    trace_id, plan_id = new_id("trace"), new_id("plan")
    mgr = ManagerAgent(model, store=store)
    # 检索上下文只给规划期**此刻真知道**的四个维度。`tenant_id` 是阶段一最硬的一维：
    # 租户 A 的 case 检索不到租户 B 的任何一条知识，这正是 R3 那一组要证明的东西。
    planned = mgr.plan(goal, context={
        "tenant_id": tenant_id, "biz_type": C.BIZ_TYPE,
        "channel_id": seed["channel_id"], "sku": seed["sku"],
        "plan_id": plan_id, "trace_id": trace_id})
    cp.create_plan(goal=goal, trace_id=trace_id, plan_id=plan_id, tasks=planned)
    cp.start_plan(plan_id)
    run_until_settled(bus, gate, cp, plan_id)

    # ---- 人工审批：审批人由政策规则指定（R4 的第二处差异） ----
    hq = HumanApprovalQueue(store, cp)
    approvals: list[dict] = []
    for blocked in hq.pending(plan_id):
        # 顺序不可换：先落 approval_record（人的决定），再放行任务。
        approvals.append(C.record_approval(
            store, tenant_id=tenant_id, case_id=case_id,
            approver=f"{APPROVER}（{directives['approver_role']}）",
            decision="approved",
            reason=f"金额与订单锁定的政策 v{view['pinned']} 一致"))
        hq.decide(blocked["task_id"], approved=True,
                  operator=f"{APPROVER}（{directives['approver_role']}）",
                  note=f"按 {directives['approver_role']} 权限放行")
    run_until_settled(bus, gate, cp, plan_id)

    if verbose:
        dump(cp, plan_id, f"对照 case {case_id}（{expected['dimension']} 维度）")

    return _observe(store, cp, plan_id, payload=payload, view=view,
                    directives=directives, verdict=verdict, days=days,
                    paid_at=paid_at, approvals=approvals)


def _observe(store, cp, plan_id: str, *, payload: dict, view: dict, directives: dict,
             verdict: dict, days: int, paid_at: str, approvals: list[dict]) -> dict:
    """把这一跑的事实收成一份可比对的字典。只读库，不做任何判定。"""
    seed = fixtures.case_seed_of(payload)
    tenant_id, case_id = str(seed["tenant_id"]), str(seed["case_id"])
    tasks = cp.store.list_tasks(plan_id)
    entries = objects.query(
        store, "SELECT * FROM finance_entry WHERE tenant_id=? AND case_id=?",
        (tenant_id, case_id))
    breakdown = json.loads(entries[0]["breakdown_json"]) if entries else {}
    return {
        "case_id": case_id,
        "case_file": None,                       # 由调用方填，观测层不认文件名
        "dimension": fixtures.expected_of(payload)["dimension"],
        "tenant_id": tenant_id,
        "channel_id": str(seed["channel_id"]),
        "reason_code": str(seed["reason_code"]),
        "paid_at": paid_at,
        "elapsed_days": days,
        "pinned_policy_version": view["pinned"],
        "matched_rules": [r["ref"] for r in view["rules"]],
        "decision": verdict["decision"],
        "policy_baseline": verdict["policy_baseline"],
        "deciding_rule": verdict["rule_ref"],
        "no_reason_days": verdict["no_reason_days"],
        "why": verdict["why"],
        "extra_tasks": [s["task_key"] for s in directives["extra_tasks"]],
        "extra_task_rules": {s["task_key"]: s["rule_ref"]
                             for s in directives["extra_tasks"]},
        "approver_role": directives["approver_role"],
        "approvals": [a["approver"] for a in approvals],
        "amount_approved": breakdown.get("amount_approved", "0.00"),
        "plan_id": plan_id,
        "plan_state": cp.store.get_plan(plan_id)["state"],
        "task_count": len(tasks),
        "tasks": [{"task_id": t["task_id"], "role": t["role"], "title": t["title"],
                   "state": t["state"]} for t in tasks],
        "biz_status": (guard.get_case(store, tenant_id, case_id) or {}).get("biz_status"),
        "state_transitions": sorted({
            e["to_state"] for e in cp.store.list_event_log(plan_id)
            if e["event_type"] == "StateTransition" and e.get("to_state")}),
    }


# ---------------------------------------------------------------------------
# 判据比对 —— 唯一来源是 case json 的 `_expected`
# ---------------------------------------------------------------------------
def check_case(expected: dict, observed: dict) -> list[str]:
    """逐条比对 `_expected`，返回**不符项**清单（空 = 全对）。

    共通三条对三个维度都成立；R6 另有一条「按最新版判会得出相反结论」，
    由 `check_r6_wrong_path()` 单独比。
    """
    bad: list[str] = []

    def eq(name: str, want, got) -> None:
        if want != got:
            bad.append(f"{name}: 期望 {want!r}，实际 {got!r}")

    eq("pinned_policy_version", expected["pinned_policy_version"],
       observed["pinned_policy_version"])
    eq("paid_at", expected["paid_at"], observed["paid_at"])
    if "elapsed_days" in expected:
        eq("elapsed_days", expected["elapsed_days"], observed["elapsed_days"])

    correct = expected.get("correct") or expected
    if "matched_rules" in correct:
        eq("matched_rules", list(correct["matched_rules"]), observed["matched_rules"])
    if "matched_rule" in correct:
        eq("deciding_rule", correct["matched_rule"], observed["deciding_rule"])
    if "no_reason_days" in correct:
        eq("no_reason_days", correct["no_reason_days"], observed["no_reason_days"])
    eq("decision", correct["decision"], observed["decision"])
    if "amount_approved" in correct:
        eq("amount_approved", correct["amount_approved"], observed["amount_approved"])
    if "extra_tasks" in expected:
        eq("extra_tasks", list(expected["extra_tasks"]), observed["extra_tasks"])

    want_role = str(expected.get("approver_role") or "")
    if want_role and _ROLE_TOKEN.fullmatch(want_role):
        eq("approver_role", want_role, observed["approver_role"])
    elif want_role:
        # 期望写成中文说明（R4A 的「常规主管（无 AS-004）」）：不做等值比对，
        # 只断言它没被某条规则改写过 —— 硬比中文串只能靠改判据才过。
        if observed["approver_role"] != DEFAULT_APPROVER_ROLE:
            bad.append(f"approver_role: 期望仍是缺省的 {DEFAULT_APPROVER_ROLE!r}"
                       f"（_expected 写作 {want_role!r}），实际 {observed['approver_role']!r}")
    return bad


def check_r6_wrong_path(store, seed: dict, expected: dict) -> tuple[dict, list[str]]:
    """R6 专用：把「按库里最新版判」这条错误路径真的走一遍，比对 `wrong_if_latest`。

    这一条不是锦上添花：库里没有 v2、或者 v2 通不过时间过滤，「锁定 v1」就无所谓
    锁不锁 —— 那时 R6 这一组证明不了任何东西，而它照样会绿。
    """
    wrong = expected["wrong_if_latest"]
    view = policy_view_latest(store, tenant_id=str(seed["tenant_id"]),
                              order_id=str(seed["order_id"]),
                              order_version=int(seed["order_version"]))
    days = elapsed_days(
        objects.query(store, "SELECT paid_at FROM order_snapshot WHERE tenant_id=?"
                             " AND order_id=? AND version=?",
                      (seed["tenant_id"], seed["order_id"],
                       int(seed["order_version"])))[0]["paid_at"],
        str(expected["requested_at"]))
    verdict = evaluate_eligibility(view["rules"], reason_code=str(seed["reason_code"]),
                                   elapsed_days=days)
    observed = {
        "latest_policy_version": view["pinned"],
        "matched_rule": verdict["rule_ref"],
        "no_reason_days": verdict["no_reason_days"],
        "decision": verdict["decision"],
        # 裁定为 reject 就没有金额要核（`finance.settle` 见到 reject 直接拒绝核算）。
        "amount_approved": "0.00" if verdict["decision"] == DECISION_REJECT else None,
        "why": verdict["why"],
    }
    bad = [f"wrong_if_latest.{k}: 期望 {wrong[k]!r}，实际 {observed[k]!r}"
           for k in ("matched_rule", "no_reason_days", "decision", "amount_approved")
           if k in wrong and wrong[k] != observed[k]]
    return observed, bad


# ---------------------------------------------------------------------------
# 组与入口
# ---------------------------------------------------------------------------
def run_group(group: str, *, matrix: bool = False, verbose: bool = True) -> list[dict]:
    """跑一组对照的全部 case，返回各自的观测 + 判据比对结果。"""
    out = []
    for filename in fixtures.CASE_FILES[group]:
        payload = fixtures.load_case(filename)
        expected = fixtures.expected_of(payload)
        observed = run_case(filename, matrix=matrix, verbose=verbose)
        observed["case_file"] = filename
        out.append({"file": filename, "expected": expected, "observed": observed,
                    "mismatch": check_case(expected, observed)})
    return out


def _fmt_case(row: dict) -> str:
    o = row["observed"]
    window = f"窗口 {o['no_reason_days']:>2} 天" if o["no_reason_days"] is not None else "无时限规则"
    return (f"  {o['case_id']:<8} tenant={o['tenant_id']:<10} channel={o['channel_id']:<10} "
            f"政策 v{o['pinned_policy_version']}  命中 {'、'.join(o['matched_rules']):<40} "
            f"{window}  第 {o['elapsed_days']:>2} 天申请  任务 {o['task_count']} 个  "
            f"审批人 {o['approver_role']:<14} -> {o['decision'].upper()}")


def run(*, matrix: bool = False) -> int:
    """三组对照跑一遍，屏幕上打对照结论。任一组与 `_expected` 不符即抛。"""
    print("三组对照 case：租户 / 渠道 / 政策版本 —— 同一个内核，零改动，"
          "差异全部来自政策数据")
    results: dict[str, list[dict]] = {}
    for group, _dim, title in GROUPS:
        results[group] = run_group(group, matrix=matrix)

    print(f"\n{'=' * 78}\n三组对照结论\n{'=' * 78}")
    mismatches: list[str] = []
    for group, _dim, title in GROUPS:
        rows = results[group]
        print(f"\n组 {group} · {title}")
        for row in rows:
            print(_fmt_case(row))
            mismatches += [f"{row['file']} {m}" for m in row["mismatch"]]
        print(f"  差异变量：{_variable_of(rows)}")
        print(f"  结论：{_note_of(rows)}")
        for row in rows:
            plan = row["observed"]
            assert plan["plan_state"] == PlanState.DONE, (
                f"{plan['case_id']} 的 Plan 应收敛到 DONE，实际 {plan['plan_state']}")

    # R6 的错误路径：把「按最新版判」真的走一遍，证明陷阱踩得进去。
    wrong, wrong_bad = _r6_wrong_path()
    mismatches += wrong_bad
    print(f"\n组 R6 · 错误路径实跑（按 max(policy_rule.version) 判，无视订单锁定）")
    print(f"  命中 {wrong['matched_rule']}  窗口 {wrong['no_reason_days']} 天  "
          f"-> {wrong['decision'].upper()}  核准 {wrong['amount_approved']}")
    print(f"  两条路径结论相反 —— v2 已生效且通得过时间过滤，"
          f"唯一挡住它的是版本锁定本身")

    if mismatches:
        raise AssertionError("对照结果与 case json 的 _expected 不符：\n  "
                             + "\n  ".join(mismatches))
    print("\n三组对照全部与 case json 的 _expected 一致；"
          "maos/contracts/** 与 maos/core/** 零改动。")
    return 0


def _r6_wrong_path() -> tuple[dict, list[str]]:
    """给 R6 单独装一次靶场跑错误路径 —— 只读政策，不建 case、不跑 DAG。"""
    from maos.core.store import SqliteStore

    store = SqliteStore()
    store.init_schema()
    payload = fixtures.load_case(fixtures.CASE_FILES["R6"][0])
    fixtures.seed_case(store, payload)
    return check_r6_wrong_path(store, fixtures.case_seed_of(payload),
                               fixtures.expected_of(payload))


def _variable_of(rows: list[dict]) -> str:
    """这一组唯一变的那一项。两个 case 的观测逐字段比出来，不是写死的。"""
    if len(rows) < 2:
        return "单 case 组（同一笔订单的两种判法，见错误路径实跑）"
    a, b = rows[0]["observed"], rows[1]["observed"]
    fields = ("tenant_id", "channel_id", "reason_code", "elapsed_days",
              "pinned_policy_version")
    diff = [f for f in fields if a[f] != b[f]]
    return "、".join(diff) if diff else "（无）"


def _note_of(rows: list[dict]) -> str:
    return str(rows[0]["expected"].get("note") or rows[0]["expected"].get("why") or "")
