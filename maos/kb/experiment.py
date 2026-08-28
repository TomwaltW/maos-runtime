"""场景 R5 —— RAG 有无对照实验（永不砍项）。

    「这一个对照实验，抵得上把七个过滤维度全部实现。」

## 三段，一个库，唯一的变量是 `MAOS_KB_ENABLED`

1. **准备段**：跑一条**完整成功**的退款 case（含财务核算），收口到 settled、拿到
   客户 ack，然后按晋升规则（`guardrails.classify_case`）把它的真实 DAG 沉淀成
   `kind='history_case'` 的知识。知识不是手写的靶场数据，是**本库里真跑出来的**
   那一单 —— `source_case_id` 指着它，核验器第 7 项按这条线回查。
2. **without_kb 段**：`MAOS_KB_ENABLED=0`。计划**漏排财务核算**（四步 DAG）。
3. **with_kb 段**：`MAOS_KB_ENABLED=1`。**同一份计划脚本**，Manager 规划前检索命中
   准备段那条知识，补上财务核算并把付款接到它后面。

两段喂给模型的脚本逐字节相同，差的只有那个环境变量。两版 DAG 的差异是跑出来的，
不是写出来的（铁律 3）。

## 拦点是权威边界，不是质量门禁 —— 这一点比手册的设想更硬

手册预期 without_kb 会被第六道闸判 blocker。**实测不是**，而且原因是对的：
第六道闸按 `task.inputs` 的 `biz_type + amount_claimed` 触发（F-1 冻结口径），
而「漏排财务核算」意味着**没有任何任务带着申报金额**——闸根本没有可判的对象。
漏排的真正症状出在下一步：`payment.execute` 查不到 `finance_entry`，
抛「金额未经核算，不许发起付款」，整个 Plan 收在 FAILED。

这比 blocker 更有说服力：拦住它的不是审查员的意见，是权威事实边界本身。
两版的 `finance_gate` 字段如实记 `not_triggered` / `pass`，不硬凑成 blocker ——
对照实验的价值在于差异是真的，不在于差异长成预期的样子。

## 领域相关性

`retriever.py` / `guardrails.py` 是领域无关的检索内核；**本文件不是** ——
它是证据生成器，复用退款域的靶场与 Skill 来跑出一份可核验的对照。
知识层的内核一行都不 import 退款域，这条边界在本文件上刻意破例，且仅此一处。
"""

from __future__ import annotations

import json
import os
from typing import Any

from maos import kb
from maos.kb import guardrails

TENANT_ID = "tnt-mfg-001"
CHANNEL_ID = "ch-tmall"
REGION = "CN-EAST"
SKU = "SKU-BRG-6204"
BIZ_TYPE = "refund"
POLICY_VERSION = 1
APPROVER = "沈思锴"
GATEWAY_NAME = "r5-demo"
SETTLE_AFTER = 2

#: 三段各自的 case 与订单。金额全部写死 —— 对照实验的两跑必须逐条可复现。
SEGMENTS = {
    "history": {"case_id": "case-r5-hist", "order_id": "ord-r5-hist"},
    "without_kb": {"case_id": "case-r5-nokb", "order_id": "ord-r5-nokb"},
    "with_kb": {"case_id": "case-r5-kb", "order_id": "ord-r5-kb"},
}
AMOUNT = 6800.00
PAID_AT = "2026-07-01T10:00:00+00:00"

GOAL = ("处理客户对轴承订单的退款诉求：多源诉求已到，"
        "需按下单当时的政策核定并退款")

DOC_ID = "kb-r5-history-0001"

SIGNALS = [
    {"source": "工单系统", "kind": "ticket", "severity": "major",
     "title": "收到的轴承有锈蚀", "detail": "工单 T-20887：客户反馈外圈有明显锈迹，要求全额退款"},
    {"source": "客服记录", "kind": "csr_note", "severity": "major",
     "title": "收到的轴承有锈蚀 ", "detail": "客服 0721 通话记录：客户口述同一问题"},
]

POLICY_RULES = [
    ("AS-01", 1, "整机质量问题全额退款", {"refund_ratio": 1.0, "deduct_fee": 0}),
]


# ---------------------------------------------------------------- DAG 脚本
def _tasks(case_id: str, *, with_finance: bool) -> list[dict]:
    """本段的计划脚本。`with_finance=False` 就是「漏排财务核算」的那一版。

    两版的差别**只有 finance 这一步**：其余四步的 id、标题、依赖、验收逐字节相同，
    所以两版 DAG 的 diff 里出现别的东西，就说明有人动了不该动的地方。
    """
    # role 名按常量取，不在各处抄字面量（与场景的 DAG 同一份口径）。
    from maos.agents.refund import ROLE_FINANCE, ROLE_INTAKE, ROLE_PAYMENT, ROLE_POLICY

    suffix = case_id.rsplit("-", 1)[-1]
    shared = {"tenant_id": TENANT_ID, "case_id": case_id, "biz_type": BIZ_TYPE}
    intake = f"task-{suffix}-intake"
    policy = f"task-{suffix}-policy"
    finance = f"task-{suffix}-finance"
    payment = f"task-{suffix}-payment"
    notify = f"task-{suffix}-notify"

    tasks = [
        {"task_id": intake, "role": ROLE_INTAKE, "title": "受理多源退款诉求并聚合证据",
         "inputs": {**shared, "step": "intake", "signals": SIGNALS,
                    "case_seed": {"tenant_id": TENANT_ID, "case_id": case_id,
                                  "channel_id": CHANNEL_ID,
                                  "order_id": SEGMENTS_BY_CASE[case_id]["order_id"],
                                  "order_version": 1, "sku": SKU,
                                  "reason_code": "quality_defect",
                                  "amount_claimed": AMOUNT}},
         "acceptance": ["多源诉求去重后建出 refund_case", "证据引用落库"],
         "depends_on": [], "risk_level": "L"},
        {"task_id": policy, "role": ROLE_POLICY, "title": "按下单锁定的政策版本裁定退款资格",
         "inputs": dict(shared),
         "acceptance": ["按订单快照锁定的政策版本判定", "给出命中的规则编号与版本"],
         "depends_on": [intake], "risk_level": "L"},
        {"task_id": payment, "role": ROLE_PAYMENT, "title": "发起退款并观察网关终态",
         "inputs": {**shared, "gateway": GATEWAY_NAME},
         "acceptance": ["发起后不得写 settled", "终态必须由 query 观察得到"],
         "depends_on": [policy], "risk_level": "M"},
        {"task_id": notify, "role": ROLE_INTAKE, "title": "通知客户退款结果",
         "inputs": {**shared, "step": "notify", "channel": "sms"},
         "acceptance": ["通知记录落库", "ack 缺失不阻塞"],
         "depends_on": [payment], "risk_level": "L"},
    ]
    if with_finance:
        tasks.insert(2, {
            "task_id": finance, "role": ROLE_FINANCE, "title": "核算退款金额并写财务分录",
            # 申报金额只挂在这一步：第六道闸按 biz_type + amount_claimed 触发（F-1），
            # 而判据是同 attempt 的产物里有没有 finance_entry —— 那份产物只有本任务产得出来。
            "inputs": {**shared, "amount_claimed": AMOUNT},
            "acceptance": ["产出 finance_entry 且与库表一致", "金额按锁定政策版本核算"],
            "depends_on": [policy], "risk_level": "M", "effect_risk": "H"})
        tasks[3]["depends_on"] = [finance]
    return tasks


SEGMENTS_BY_CASE = {v["case_id"]: v for v in SEGMENTS.values()}


# ---------------------------------------------------------------- 靶场
def _seed(store, case_id: str) -> None:
    from maos.domain.refund import objects
    from maos.skills.builtin.refund import _common as C

    order_id = SEGMENTS_BY_CASE[case_id]["order_id"]
    objects.ensure_schema(store)
    objects.execute(store, "INSERT OR REPLACE INTO tenant (tenant_id, name, region)"
                           " VALUES (?,?,?)", (TENANT_ID, "示例精密制造", REGION))
    objects.execute(store, "INSERT OR REPLACE INTO channel (tenant_id, channel_id, kind, name)"
                           " VALUES (?,?,?,?)",
                    (TENANT_ID, CHANNEL_ID, "marketplace", "天猫旗舰店"))
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
        (TENANT_ID, order_id, 1, SKU, AMOUNT, PAID_AT, CHANNEL_ID, POLICY_VERSION,
         "{}", C.now_iso()))
    for rule_no, version, title, params in POLICY_RULES:
        objects.execute(
            store,
            "INSERT OR REPLACE INTO policy_rule (tenant_id, rule_no, version, title, body,"
            " effective_from, effective_to, channel_scope, sku_scope) VALUES (?,?,?,?,?,?,?,?,?)",
            (TENANT_ID, rule_no, version, title,
             json.dumps(params, ensure_ascii=False, sort_keys=True),
             "2026-01-01T00:00:00+00:00", None, "*", "*"))


# ---------------------------------------------------------------- 一段的执行
def _run_segment(*, case_id: str, with_finance: bool, use_kb: bool) -> dict:
    """跑一段，返回这一段的真实观测。

    刻意每段都走 `flows.common.build()`：不留第二条装配路径（C-3/C-4）。
    三段共用同一个 store 实例（由 `run_r5` 注入的工厂保证），所以准备段沉淀的知识
    对后两段可见，而这正是「同一个知识库、有无检索」这个对照成立的前提。
    """
    from maos.agents.manager import ManagerAgent
    from maos.contracts.events import Topic, new_id
    from maos.contracts.states import PlanState
    from maos.domain.refund import guard, objects
    from maos.flows.common import build, run_until_settled
    from maos.model.client import ScriptedModelClient
    from maos.runtime.gate import HumanApprovalQueue
    from maos.skills.builtin.refund import _common as C
    from maos.tools.gateway import MockGateway

    plan_json = json.dumps({"tasks": _tasks(case_id, with_finance=with_finance)},
                           ensure_ascii=False)
    model = ScriptedModelClient({"用户请求": plan_json})
    store, bus, cp, model, worker, gate = build({}, model=model)
    _seed(store, case_id)
    C.reset_gateways()
    C.register_gateway(GATEWAY_NAME, MockGateway(settle_after=SETTLE_AFTER))

    # 只读观察者：Gate 的判定要进证据，而它走总线不落库。
    # 多挂一个订阅者不改变控制面行为（ControlPlane 那个照常收），
    # 但让「第六道闸这一趟到底判了什么」有据可查，而不是靠事后猜。
    verdicts: list[dict] = []
    bus.subscribe(Topic.REVIEW_VERDICT, "r5-observer",
                  lambda env: verdicts.append({"task_id": env.task_id, **env.payload}))

    trace_id = new_id("trace")
    # Manager 这次**带 store**：规划前检索要有库可查。场景 6 用的是 `cls(model)`
    # 老写法（无 store），那条链路上检索恒返回空 —— 两种构造方式都必须能跑。
    mgr = ManagerAgent(model, store=store)
    context = {"tenant_id": TENANT_ID, "biz_type": BIZ_TYPE, "channel_id": CHANNEL_ID,
               "region": REGION, "sku": SKU, "policy_version": POLICY_VERSION,
               "rule_no": "AS-01", "trace_id": trace_id,
               "keyword": "轴承 锈蚀 退款 财务核算"}
    with _kb_switch(use_kb):
        tasks = mgr.plan(GOAL, context=context)

    plan_id = cp.create_plan(goal=GOAL, trace_id=trace_id, tasks=tasks)

    # 主管审批**先落库**，两段一视同仁。放在 start_plan 之前是刻意的：
    # 漏排财务核算的那一段没有高风险任务，不会停在 BLOCKED，付款会在第一轮就执行 ——
    # 审批要是等到那之后再补，两段的失败原因就变成「没审批」而不是「没核算」，
    # 对照实验凭空多出第二个变量。唯一的变量必须是 MAOS_KB_ENABLED。
    C.record_approval(store, tenant_id=TENANT_ID, case_id=case_id, approver=APPROVER,
                      decision="approved", reason="金额与订单锁定的政策 v1 一致")

    cp.start_plan(plan_id)
    run_until_settled(bus, gate, cp, plan_id)

    # Task 级的人工审批闸是另一回事（effect_risk=H 才停）：补上财务核算的那一段
    # 会停在这里等人放行，漏排的那一段压根停不下来 —— 这本身也是差异的一部分。
    hq = HumanApprovalQueue(store, cp)
    blocked_titles = [b["title"] for b in hq.pending(plan_id)]
    for blocked in hq.pending(plan_id):
        hq.decide(blocked["task_id"], approved=True, operator=APPROVER,
                  note="已核对金额与政策版本")
    run_until_settled(bus, gate, cp, plan_id)

    plan = cp.store.get_plan(plan_id)
    rows = cp.store.list_tasks(plan_id)
    case = guard.get_case(store, TENANT_ID, case_id)
    events = cp.store.list_event_log(plan_id)
    return {
        "plan_id": plan_id,
        "plan_state": plan["state"],
        "tasks": [t["title"] for t in rows],
        "task_keys": [".".join(guardrails.task_key(t)) for t in rows],
        "gate_result": _gate_result(verdicts),
        "finance_gate": _finance_gate(rows, verdicts),
        "rework_count": sum(1 for e in events
                            if e["event_type"] == "StateTransition"
                            and e["to_state"] == "REWORK"),
        # 按 trace_id 数，不按 plan_id：规划期的检索发生在 create_plan 之前，
        # 那条 KbRetrieved 的 plan_id 是空串，按 plan 查恒为 0 —— 会把
        # 「检索确实跑了」误报成「一次都没检索」，对照实验的关键量就此消失。
        "kb_retrieved_events": len(kb.query(
            store, "SELECT seq FROM event_log WHERE event_type='KbRetrieved'"
                   " AND trace_id=?", (trace_id,))),
        "human_approval_stops": blocked_titles,
        "biz_status": (case or {}).get("biz_status"),
        "failed_tasks": [{"title": t["title"], "error": t["last_error"]}
                         for t in rows if t["last_error"]],
        "finance_entries": len(objects.query(
            store, "SELECT 1 FROM finance_entry WHERE tenant_id=? AND case_id=?",
            (TENANT_ID, case_id))),
        "settled": (case or {}).get("biz_status") == "settled",
        "plan_done": plan["state"] == PlanState.DONE,
    }


def _gate_result(verdicts: list[dict]) -> str:
    """整趟 Gate 的判定：任何一条 rework 即 blocker，否则 pass。"""
    if any(v.get("verdict") == "rework" for v in verdicts):
        return "blocker"
    return "pass" if verdicts else "no_verdict"


def _finance_gate(task_rows: list[dict], verdicts: list[dict]) -> str:
    """第六道闸的真实判定：`not_triggered` / `pass` / `blocker`。

    `not_triggered` 是**有意义的一档**，不是「没数据」。闸按 `task.inputs` 的
    `biz_type + amount_claimed` 触发（F-1 冻结口径）—— 漏排财务核算时没有任何任务
    带着申报金额，闸连可判的对象都没有。这正是漏排最危险的地方：它不是被判不合格，
    是压根没进入判定视野。所以这一档必须能与 `pass` 区分，不能都记成「闸过了」。
    """
    from maos.runtime.gate import DEFAULT_FINANCE_THRESHOLD, FINANCE_BIZ_TYPE

    triggering = set()
    for task in task_rows:
        inputs = task.get("inputs") or {}
        if not isinstance(inputs, dict) or inputs.get("biz_type") != FINANCE_BIZ_TYPE:
            continue
        try:
            amount = float(inputs.get("amount_claimed") or 0)
        except (TypeError, ValueError):
            amount = float("inf")          # 解析不出 = 触发，与闸本身的口径一致
        if amount > DEFAULT_FINANCE_THRESHOLD:
            triggering.add(task["task_id"])
    if not triggering:
        return "not_triggered"

    judged = [v for v in verdicts if v.get("task_id") in triggering
              and isinstance(v.get("gate_results"), dict)]
    if any(v["gate_results"].get("finance") == "fail" for v in judged):
        return "blocker"
    return "pass" if judged else "not_reviewed"


class _kb_switch:
    """临时切 `MAOS_KB_ENABLED`。退出时**恢复原值**（包括原本没设这一情形）。

    不恢复的后果不是报错：下一段跑在上一段留下的开关上，两段的差异于是不再只有
    这一个变量，而对照实验的全部价值就在「只有这一个变量」。
    """

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self.previous: str | None = None

    def __enter__(self) -> "_kb_switch":
        self.previous = os.environ.get(kb.KB_ENABLED_ENV)
        os.environ[kb.KB_ENABLED_ENV] = "1" if self.enabled else "0"
        return self

    def __exit__(self, *exc: Any) -> None:
        if self.previous is None:
            os.environ.pop(kb.KB_ENABLED_ENV, None)
        else:
            os.environ[kb.KB_ENABLED_ENV] = self.previous


# ---------------------------------------------------------------- 知识晋升
def promote_history_case(store, *, case_id: str, plan_id: str) -> dict | None:
    """按晋升规则把一条已收口的 case 沉淀成知识。不够格返回 None。

    这是**手动晋升**（派单第 7 步）：自动晋升调度器不在本轮范围内，已记 BACKLOG。
    """
    from maos.domain.refund import guard, objects

    case = guard.get_case(store, TENANT_ID, case_id)
    observations = objects.query(
        store, "SELECT * FROM payment_observation WHERE tenant_id=? AND case_id=?",
        (TENANT_ID, case_id))
    notifications = objects.query(
        store, "SELECT * FROM notification WHERE tenant_id=? AND case_id=?",
        (TENANT_ID, case_id))

    verdict = guardrails.classify_case(
        observations=observations, notifications=notifications, case_row=case)
    if verdict is None:
        return None
    doc_kind, outcome = verdict
    if doc_kind != kb.KIND_HISTORY_CASE:
        return None

    tasks = store.list_tasks(plan_id)
    body = guardrails.case_to_doc_body(
        tasks, note="退款顺利路径：受理 -> 政策裁定 -> 财务核算 -> 付款 -> 通知。"
                    "财务核算是付款的前置，缺了它付款发不出去。")
    from maos.kb.retriever import embed
    return kb.upsert_doc(store, {
        "tenant_id": TENANT_ID, "doc_id": DOC_ID, "biz_type": BIZ_TYPE,
        "channel_id": CHANNEL_ID, "region": REGION, "sku": SKU,
        "policy_version": POLICY_VERSION, "workflow_version": 1,
        "rule_no": "AS-01", "gateway_code": None,
        "kind": doc_kind, "outcome": outcome, "source_case_id": case_id,
        "title": "轴承锈蚀全额退款：财务核算不可省",
        "body": body,
        "embedding": embed("轴承 锈蚀 退款 财务核算 政策 AS-01"),
    })


def _ack_notifications(store, case_id: str) -> int:
    """客户确认收到退款通知 —— 靶场事件，与 MockGateway 同性质。

    晋升规则要求「证据完整且外部结果明确」，ack 是其中一条。没有它这条 case
    进不了正例知识层（`classify_case` 会返回 None），对照实验也就没有知识可用。
    """
    from maos.domain.refund import objects
    rows = objects.query(
        store, "SELECT * FROM notification WHERE tenant_id=? AND case_id=?",
        (TENANT_ID, case_id))
    for row in rows:
        objects.execute(
            store,
            "UPDATE notification SET ack_at=? WHERE tenant_id=? AND case_id=?"
            " AND channel=? AND content_digest=?",
            (kb.now_iso(), TENANT_ID, case_id, row["channel"], row["content_digest"]))
    return len(rows)


# ---------------------------------------------------------------- 对外入口
def run_r5(db_path: str | None = None) -> dict:
    """跑完三段，返回 dag-diff 文档。`db_path` 非空则把库落到那个文件。

    落文件库的方式与证据生成器同款：在**进程内**把 `flows.common.SqliteStore`
    换成绑定了路径的工厂。仓库里一个字节不改，落库位置由调用方提供 ——
    `flows/**` 与 `core/**` 都是禁改面。
    """
    from maos.core.store import SqliteStore
    from maos.flows import common as flows_common

    singleton: dict[str, Any] = {}

    def factory(*_args: Any, **_kwargs: Any):
        """三段共用一个库 —— 准备段沉淀的知识要对后两段可见。"""
        if "store" not in singleton:
            singleton["store"] = SqliteStore(db_path or ":memory:")
        return singleton["store"]

    original = flows_common.SqliteStore
    flows_common.SqliteStore = factory       # type: ignore[assignment]
    try:
        print("场景 R5：RAG 有无对照实验，无 key 确定性复现")
        print("\n[1/3] 准备段：跑一条完整成功的退款 case，收口后按晋升规则沉淀知识")
        history = _run_segment(case_id=SEGMENTS["history"]["case_id"],
                               with_finance=True, use_kb=False)
        store = singleton["store"]
        acked = _ack_notifications(store, SEGMENTS["history"]["case_id"])
        print(f"  Plan {history['plan_state']}，业务状态 {history['biz_status']}，"
              f"客户 ack {acked} 条")
        doc = promote_history_case(store, case_id=SEGMENTS["history"]["case_id"],
                                   plan_id=history["plan_id"])
        if doc is None:
            raise RuntimeError(
                "准备段的 case 没能通过晋升规则 —— 没有可用知识，对照实验不成立。"
                f"（settled={history['settled']} ack={acked}）")
        print(f"  晋升：{doc['doc_id']} kind={doc['kind']} outcome={doc['outcome']} "
              f"source_case_id={doc['source_case_id']}")

        print(f"\n[2/3] without_kb：{kb.KB_ENABLED_ENV}=0，计划漏排财务核算")
        without_kb = _run_segment(case_id=SEGMENTS["without_kb"]["case_id"],
                                  with_finance=False, use_kb=False)
        print(f"  {len(without_kb['tasks'])} 个任务，KbRetrieved "
              f"{without_kb['kb_retrieved_events']} 条，第六道闸 "
              f"{without_kb['finance_gate']}，Plan {without_kb['plan_state']}，"
              f"业务状态 {without_kb['biz_status']}")
        for failed in without_kb["failed_tasks"]:
            print(f"  拦点：{failed['title']} -> {failed['error']}")

        print(f"\n[3/3] with_kb：{kb.KB_ENABLED_ENV}=1，同一份计划脚本")
        with_kb = _run_segment(case_id=SEGMENTS["with_kb"]["case_id"],
                               with_finance=False, use_kb=True)
        print(f"  {len(with_kb['tasks'])} 个任务，KbRetrieved "
              f"{with_kb['kb_retrieved_events']} 条，第六道闸 "
              f"{with_kb['finance_gate']}，Plan {with_kb['plan_state']}，"
              f"业务状态 {with_kb['biz_status']}")
        print(f"  人工审批停点：{with_kb['human_approval_stops'] or '无'}")
    finally:
        flows_common.SqliteStore = original  # type: ignore[assignment]

    delta = [k for k in with_kb["task_keys"] if k not in without_kb["task_keys"]]
    hits = _triggering_docs(store, with_kb["plan_id"])
    print(f"\n差异：delta_tasks={delta}，触发文档="
          f"{[(h['doc_id'], h['score']) for h in hits]}")
    return {
        "experiment": "R5 · RAG 有无对照",
        "variable": f"{kb.KB_ENABLED_ENV}=0 / 1，两段的计划脚本逐字节相同",
        "history_case": {
            "case_id": SEGMENTS["history"]["case_id"],
            "plan_id": history["plan_id"],
            "biz_status": history["biz_status"],
            "acked_notifications": acked,
            "promoted_doc": {"doc_id": doc["doc_id"], "kind": doc["kind"],
                             "outcome": doc["outcome"],
                             "source_case_id": doc["source_case_id"]},
        },
        "without_kb": without_kb,
        "with_kb": with_kb,
        "delta_tasks": delta,
        "triggering_docs": hits,
        "conclusion": _conclusion(without_kb, with_kb, delta),
    }


def _triggering_docs(store, plan_id: str) -> list[dict]:
    """with_kb 那一跑里真正促成补步骤的命中 —— 从 event_log 读，不从内存拼。"""
    rows = kb.query(
        store,
        "SELECT detail FROM event_log WHERE event_type='KbRetrieved' ORDER BY seq")
    out: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        try:
            detail = json.loads(row["detail"])
        except (TypeError, ValueError):
            continue
        for d in detail.get("docs") or []:
            if d["doc_id"] in seen:
                continue
            seen.add(d["doc_id"])
            out.append({"doc_id": d["doc_id"], "score": d["score"],
                        "title": d.get("title"), "kind": d.get("kind")})
    return out


def write_evidence(out_root: str | None = None) -> str:
    """跑一次 R5 并落成一套完整证据束 `evidence/scenario-R5/`。返回目录路径。

    **必须是完整束，不能只放 dag-diff.json**：核验器把 `evidence/scenario-*` 每个目录
    都当一个 case 读，缺 trace.json / result.json / maos.db 会让它连核验都开始不了
    （抛 VerifyError 而不是判某一项不过）。所以这里复用证据生成器的 `write_bundle`，
    与场景 1-6 走同一套落盘与脱敏口径 —— 不另立第二份。

    先在临时目录攒齐、脱敏反查过关，才 `os.replace` 挪到位。中途任何异常都连临时
    目录一起删：半份目录比没有更坏，它看起来跟跑通了一模一样。
    """
    import contextlib
    import io
    import os.path
    import shutil
    import sys
    import time

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))))
    from scripts.make_evidence import (
        git_sha, scan_for_secrets, secret_values, write_bundle, write_json)

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    out_root = out_root or os.path.join(root, "evidence")
    final = os.path.join(out_root, "scenario-R5")
    tmp = os.path.join(out_root, f".tmp-scenario-R5.{os.getpid()}")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)

    sha, secrets = git_sha(), secret_values()
    try:
        db_path = os.path.join(tmp, "maos.db")
        buf = io.StringIO()
        started = time.perf_counter()
        with contextlib.redirect_stdout(buf):
            diff = run_r5(db_path)
        wall_ms = int((time.perf_counter() - started) * 1000)

        write_bundle(db_path, tmp, scenario="R5", exit_code=0, wall_ms=wall_ms,
                     log=buf.getvalue(), sha=sha, secrets=secrets)
        write_json(os.path.join(tmp, "dag-diff.json"), diff, sha=sha, secrets=secrets)

        leaks = scan_for_secrets(tmp, secrets)
        if leaks:
            raise RuntimeError("R5 的产物里查到敏感值明文，目录已销毁：\n  "
                               + "\n  ".join(leaks))
        shutil.rmtree(final, ignore_errors=True)
        os.replace(tmp, final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return final


def _conclusion(without_kb: dict, with_kb: dict, delta: list[str]) -> str:
    if not delta:
        return "两版 DAG 无差异 —— 对照实验不成立，检查知识是否命中"
    return (
        f"关掉检索：计划漏排 {delta}，"
        f"Plan 收在 {without_kb['plan_state']}（"
        f"第六道闸 {without_kb['finance_gate']}，"
        f"业务状态 {without_kb['biz_status']}）；"
        f"打开检索：命中历史案例补上 {delta} 并接成付款的前置，"
        f"Plan 收在 {with_kb['plan_state']}（第六道闸 {with_kb['finance_gate']}，"
        f"业务状态 {with_kb['biz_status']}）。"
    )


if __name__ == "__main__":                   # python3 -m maos.kb.experiment
    import sys as _sys

    _path = write_evidence()
    print(f"\n证据束已落盘：{_path}")
    _sys.exit(0)
