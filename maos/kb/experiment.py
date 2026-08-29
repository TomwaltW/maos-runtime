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

## 拦点从权威边界前移到闸 —— 两层防线，两段历史

**Phase 7 之前**：手册预期 without_kb 会被第六道闸判 blocker，实测不是。
第六道闸按 `task.inputs` 的 `biz_type + amount_claimed` **逐任务**触发（F-1 冻结口径），
而「漏排财务核算」意味着**没有任何任务带着申报金额**——闸根本没有可判的对象，
`finance_gate` 如实记 `not_triggered`。漏排的真正症状出在下一步：
`payment.execute` 查不到 `finance_entry`，抛「金额未经核算，不许发起付款」，
整个 Plan 收在 FAILED。拦住它的不是审查员的意见，是权威事实边界本身。

**Phase 7 起**（BACKLOG `## task-W3` 第 3 条）：第六道闸补上 **plan 级判据** ——
「这个 Plan 报了超阈金额，却没有任何任务把它带进闸的视野」。它判计划的**静态结构**，
不判凭据跑出来没有，所以与评审顺序无关。于是 without_kb 段在**受理那一步过闸时**
就被判出计划缺陷，比付款早两步，`finance_gate` 记 `blocker`。

**权威边界没被拆，只是这一跑走不到那儿了。** 两层防线各管各的：闸判「计划里排没排
这一步」，付款方判「这一笔到底核算过没有」；上面那层永远可能被绕过（改阈值、改
`biz_type`），下面这层不能。R5 让出的那条运行时演示，由 `maos/tests/test_plan_gate.py`
的 `test_authority_boundary_still_refuses_payment_without_a_finance_entry` 接住 ——
唯一的演示没了却没人补断言，是这次改动最容易留下的暗坑。

⚠️ **当前 without_kb 段的拦点是个过渡态。** plan 级 finding 带 `scope="plan"`，按跨轨
冻结契约该由控制面直接转人工（`AWAITING_REVIEW -> BLOCKED`，**不返工**）；那条路由在
D-1 轨，本分支里还没有。于是 blocker 走了普通返工路径，受理那一步被重跑，撞上
`refund_case` 的唯一键 —— 日志里那句 IntegrityError 就是这么来的（受理 skill 在返工下
不幂等，是先于本次改动就在的问题，记在 BACKLOG `## task-D2`）。D-1 合并后这一段会变成
「受理 BLOCKED，等人决策」，**证据束届时必须重跑**。

两版的 `finance_gate` 字段一律**如实记闸真的说了什么**，不硬凑 ——
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

#: 靶场身份**逐字对齐 W-1 的语料**（`scenarios/refund/`）。
#: 对齐不是为了好看：阶段一按 tenant/channel/region/sku/policy_version 硬过滤，
#: 身份对不上时那 40 条语料一条都进不了候选集 —— 库里有几十条、候选集仍然是 1 条，
#: 而且不报错。R5 的含金量全在候选集上，所以这几个常量必须跟着语料走。
TENANT_ID = "tnt-mfg-a"
CHANNEL_ID = "ch-online"
REGION = "cn-hangzhou"
SKU = "SKU-BRG-6205"
BIZ_TYPE = "refund"
POLICY_VERSION = 1
#: 本案诉求是 `quality_defect`，对应语料里的 AS-002「质保期内质量问题全额退」。
#: 精确通道按它命中，所以它必须是语料里真实存在的编号，不能是自造的 AS-01。
RULE_NO = "AS-002"
APPROVER = "沈思锴"
GATEWAY_NAME = "r5-demo"
SETTLE_AFTER = 2

#: W-1 语料根目录。本文件是证据生成器，读靶场数据是它的本职（见模块头「领域相关性」）。
CORPUS_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "scenarios", "refund")

#: 语料里被本文件消费的四张退款域表，以及各自的**列清单**。
#: 列清单写在这里而不是靠 `INSERT ... VALUES` 的位置对齐：语料多一列少一列都当场抛，
#: 而不是等到某条 INSERT 报 no such column，或者更糟 —— 值悄悄错位一列。
#: W-1 的账（`## task-W1` 第 3 条）记着这 7 份数据文件「零消费方，字段分叉不会有任何报错」，
#: 这一份列清单就是那条账的守卫。
CORPUS_TABLES = (
    ("tenant", ("tenant_id", "name", "region")),
    ("channel", ("tenant_id", "channel_id", "kind", "name")),
    ("product_snapshot", ("tenant_id", "sku", "version", "name", "category",
                          "warranty_months", "payload_json")),
    ("policy_rule", ("tenant_id", "rule_no", "version", "title", "body",
                     "effective_from", "effective_to", "channel_scope", "sku_scope")),
)

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
            # 申报金额只挂在这一步：第六道闸的**任务级**判据按 biz_type + amount_claimed
            # 触发（F-1），而判据是同 attempt 的产物里有没有 finance_entry —— 那份产物只有
            # 本任务产得出来。所以 with_finance=False 那一版，顶层申报金额跟着一起消失。
            # 那一版由**plan 级**判据接住（P7 起）：它扫的是 inputs 树里任意深度的同一个
            # 字段名，于是受理那一步 case_seed 里那份金额仍然在场 —— 这个形状**不许为了
            # 迁就判据去改**（把金额塞进 shared 就是自证，与本文件抬头那条红线同一件事）。
            "inputs": {**shared, "amount_claimed": AMOUNT},
            "acceptance": ["产出 finance_entry 且与库表一致", "金额按锁定政策版本核算"],
            "depends_on": [policy], "risk_level": "M", "effect_risk": "H"})
        tasks[3]["depends_on"] = [finance]
    return tasks


SEGMENTS_BY_CASE = {v["case_id"]: v for v in SEGMENTS.values()}


# ---------------------------------------------------------------- 语料装载
def load_corpus(name: str) -> dict:
    """读一份 W-1 语料。文件缺失就抛 —— 靶场数据不在了，这一跑的结论不成立。

    不做「读不到就回落到自造的最小集」：那样 R5 会照常跑绿，而候选集悄悄退回 1 条，
    整个对照实验的含金量凭空蒸发且没有任何症状。
    """
    path = os.path.join(CORPUS_ROOT, name)
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _checked_rows(payload: dict, key: str, columns: tuple) -> list[dict]:
    """取语料里的一段行，并**逐行校验列清单逐字对齐**，分叉就抛。

    这是 W-1 那条「零消费方」账的守卫（`## task-W1` 第 3 条）：语料多一列、少一列、
    改个列名，从前不会有任何东西变红；现在会当场抛，而且报的是差在哪一列。
    """
    rows = payload.get(key)
    if not isinstance(rows, list) or not rows:
        raise KeyError(f"语料里没有 {key!r} 这一段，或者它是空的")
    want = set(columns)
    for idx, row in enumerate(rows):
        got = set(row)
        if got != want:
            raise ValueError(
                f"{key}[{idx}] 的列清单与本文件登记的不一致："
                f"多出 {sorted(got - want)}，缺少 {sorted(want - got)}。"
                " 语料与消费方的列清单分叉，改一处就要改另一处，不许只改一边。")
    return rows


def _seed_domain_from_corpus(store) -> dict:
    """把 W-1 的政策语料装进退款域四张表。返回各表落了几行。"""
    from maos.domain.refund import objects

    payload = load_corpus(os.path.join("policy", "policy_rules.json"))
    counted: dict[str, int] = {}
    for table, columns in CORPUS_TABLES:
        rows = _checked_rows(payload, table, columns)
        marks = ", ".join("?" for _ in columns)
        sql = (f"INSERT OR REPLACE INTO {table} ({', '.join(columns)})"
               f" VALUES ({marks})")
        for row in rows:
            objects.execute(store, sql, tuple(row[c] for c in columns))
        counted[table] = len(rows)
    return counted


def promote_policy_rule(store, row: dict) -> dict:
    """把**一行** `policy_rule` 投影成 `kind='policy'` 的知识文档。返回落库整行。

    投影是**逐字搬运**，不是改写：`title` / `body` / `rule_no` / `version` 原样取自
    `policy_rule` 行，`created_at` 取该版本的 `effective_from`。
    `channel_id` / `region` / `sku` 一律留 NULL —— 语料里这些规则的
    `channel_scope` / `sku_scope` 都是通配，而阶段一的口径正是「文档侧 NULL = 通配」，
    照抄成具体值反而会把一条不限渠道的政策锁死在一个渠道上。

    拆成「一行一次」是为了让场景 6 播它自己那 3 条政策时走同一份口径
    （`flows/scenario_6.py` 的 `_seed_kb`）。两处各写一套投影，迟早在字段口径上
    分叉，而症状只是「候选集少了些」—— 不报错，也没人看得出。
    """
    from maos.kb.retriever import embed

    title = str(row["title"])
    body = str(row["body"])
    return kb.upsert_doc(store, {
        "tenant_id": row["tenant_id"],
        # doc_id 里必须带租户：`kb_doc` 的主键是 `(tenant_id, doc_id)`，
        # 两个租户各有一条 AS-001，不带租户就是两行同名的 doc_id ——
        # 表里不冲突，但事件里的一个 doc_id 从此指不到唯一一行，
        # 证据（KbRetrieved.docs）就再也读不出「命中的到底是谁家那条」。
        "doc_id": f"kb-policy-{row['tenant_id']}-{row['rule_no']}-v{row['version']}",
        "biz_type": BIZ_TYPE,
        "channel_id": None, "region": None, "sku": None,
        "policy_version": int(row["version"]),
        "workflow_version": None,
        "rule_no": row["rule_no"], "gateway_code": None,
        "kind": kb.KIND_POLICY, "outcome": None, "source_case_id": None,
        "title": title, "body": body,
        "embedding": embed(f"{title} {body}"),
        "created_at": row["effective_from"],
    })


def seed_kb_corpus(store) -> int:
    """把 W-1 语料里的 16 条政策投影进 `kb_doc`。返回落库条数。

    租户**原样保留**（tnt-mfg-a / tnt-mfg-b），不改写成调用方自己的租户。改写是
    最省事的做法也是最错的：`kb_doc` 的主键是 `(tenant_id, doc_id)`，两个租户各有
    一条 AS-001，改写后就是同一行，16 条静默塌成 8 条；更要紧的是租户是阶段一
    最硬的一维（`retriever.prefilter`），给别人家的政策贴上本租户的标签，等于亲手
    废掉这套系统唯一不能出错的那条约束。

    **只投影政策，不投影 `history/history_cases.json` 的 24 条历史案例** ——
    核验器第 7 项要求库里每一条 `history_case` 的 `source_case_id` 都能回查到一条
    `biz_status='settled'` 的真实 `refund_case`，而外部导入的历史知识按定义没有这样
    一条本库记录。给它们凭空造 refund_case 行就是伪造证据（铁律 3），所以那 24 条由
    `maos/tests/test_kb_corpus.py` 全量装载并守着，账记在 BACKLOG `## task-X3`。
    """
    payload = load_corpus(os.path.join("policy", "policy_rules.json"))
    rows = _checked_rows(payload, "policy_rule",
                         dict(CORPUS_TABLES)["policy_rule"])
    for row in rows:
        promote_policy_rule(store, row)
    return len(rows)


#: 旧名。`maos/tests/test_kb_corpus.py`（X-3 的面，不是本轨的文件）按它消费，留个别名。
_seed_kb_from_corpus = seed_kb_corpus


# ---------------------------------------------------------------- 靶场
def _seed(store, case_id: str) -> None:
    """装靶场：W-1 的政策语料 + 本段自己的订单快照。

    订单快照是 R5 自己的（三段各一笔），语料里没有 —— 语料给的是政策与商品，
    交易是本次实验现造的。两者的租户 / 渠道 / SKU / 政策版本必须一致，
    否则政策裁定与检索预过滤会各自落到不同的口径上。
    """
    from maos.domain.refund import objects
    from maos.skills.builtin.refund import _common as C

    order_id = SEGMENTS_BY_CASE[case_id]["order_id"]
    objects.ensure_schema(store)
    _seed_domain_from_corpus(store)
    objects.execute(
        store,
        "INSERT OR REPLACE INTO order_snapshot (tenant_id, order_id, version, sku, amount_paid,"
        " paid_at, channel_id, policy_version_at_order, payload_json, read_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (TENANT_ID, order_id, 1, SKU, AMOUNT, PAID_AT, CHANNEL_ID, POLICY_VERSION,
         "{}", C.now_iso()))
    kb.ensure_schema(store)
    seed_kb_corpus(store)


# ---------------------------------------------------------------- 一段的执行
def _await_kind(store, plan_id: str, task_id: str) -> str:
    """这个任务最近一次进 BLOCKED 时，控制面声明它在等哪一类人的动作。

    判据与 `HumanApprovalQueue.pending()` 同源：都读 BLOCKED 迁移那条事件的
    `detail["await"]`，不在任务行上另开字段（event_log 是唯一事实源）。

    取**最近一次**而不是第一次：BLOCKED 可以进出多次（`human_resume` 回去、再因
    别的原因停下），要处置的是最后那一次。缺省回 `human_approval` —— effect_risk=H
    那条既有路径不写 `await`，缺省成放行才是保持既有语义。
    """
    from maos.contracts.states import TaskState

    kinds = [e["detail"].get("await") for e in store.list_event_log(plan_id)
             if e.get("task_id") == task_id
             and e.get("to_state") == TaskState.BLOCKED
             and isinstance(e.get("detail"), dict)]
    return kinds[-1] if kinds and kinds[-1] else "human_approval"


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
    # plan_id 也先生成、规划期带着它跑：这次检索发生在 `create_plan` **之前**，
    # 不先拿到 id，KbRetrieved / SkillInvoked 就只能落空串，成为 trace 里认领不了的
    # 游离事件（docs/BACKLOG.md `## task-X4` 第 2 条）。归属不是硬凑的 ——
    # 这次检索检的正是这个 Plan 该怎么排。
    plan_id = new_id("plan")
    # Manager **带 store**：规划前检索要有库可查。不带的话 SkillInvoker.store is None，
    # 检索恒返回空且不报错 —— 两种构造方式都必须能跑。
    mgr = ManagerAgent(model, store=store)
    context = {"tenant_id": TENANT_ID, "biz_type": BIZ_TYPE, "channel_id": CHANNEL_ID,
               "region": REGION, "sku": SKU, "policy_version": POLICY_VERSION,
               "rule_no": RULE_NO, "plan_id": plan_id, "trace_id": trace_id,
               "keyword": "轴承 锈蚀 退款 财务核算"}
    with _kb_switch(use_kb):
        tasks = mgr.plan(GOAL, context=context)

    cp.create_plan(goal=GOAL, trace_id=trace_id, tasks=tasks, plan_id=plan_id)

    # 主管审批**先落库**，两段一视同仁。放在 start_plan 之前是刻意的：
    # 漏排财务核算的那一段没有高风险任务，不会停在 BLOCKED，付款会在第一轮就执行 ——
    # 审批要是等到那之后再补，两段的失败原因就变成「没审批」而不是「没核算」，
    # 对照实验凭空多出第二个变量。唯一的变量必须是 MAOS_KB_ENABLED。
    C.record_approval(store, tenant_id=TENANT_ID, case_id=case_id, approver=APPROVER,
                      decision="approved", reason="金额与订单锁定的政策 v1 一致")

    cp.start_plan(plan_id)
    run_until_settled(bus, gate, cp, plan_id)

    # 停在 BLOCKED 等人的任务有两类，处置方式不同，判据与 `HumanApprovalQueue.pending()`
    # 同源（都读 BLOCKED 迁移那条事件的 `detail["await"]`，见 `_await_kind`）：
    #
    # · `human_approval` —— effect_risk=H 的高风险放行。补上财务核算的那一段会停在
    #   这里等人核对金额与政策版本，主管放行（既有语义，一个字节没动）。
    # · `human_decision` —— 控制面的第三出口：机器返工修不好，转人工裁决。漏排财务
    #   核算是**计划的静态结构缺陷**，放行一次不会让它消失（重评照旧 blocker，闸判
    #   的是「排没排这一步」），主管在这里能做的只有驳回，让这一版计划收敛到 FAILED。
    #
    # 改造前漏排那一段压根停不下来、要等付款技能拒绝才失败；现在闸在裁定那一步就
    # 拦下转人工，早两步。这里补上「人来处置」是为了不在演示里留一个悬空的 BLOCKED，
    # **不动闸判什么** —— `finance_gate` 记的仍是闸真的说的那个 blocker。
    hq = HumanApprovalQueue(store, cp)
    dispositions: list[dict] = []
    for blocked in hq.pending(plan_id):
        await_kind = _await_kind(store, plan_id, blocked["task_id"])
        approved = await_kind != "human_decision"
        hq.decide(blocked["task_id"], approved=approved, operator=APPROVER,
                  note=("已核对金额与政策版本" if approved else
                        "计划漏排财务核算，退回重排 —— 不许在没有财务凭据时发起付款"))
        dispositions.append({"title": blocked["title"], "await": await_kind,
                             "approved": approved})
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
        # 按 trace_id 数，不按 plan_id：trace 是这一段从头到尾唯一不变的那根线。
        # 本段现在把 plan_id 预生成好再带着规划期跑，按 plan 查也数得到；但哪天
        # 有人把那个前置去掉，检索又落回空串，按 plan 查会静悄悄退回 0 —— 把
        # 「检索确实跑了」误报成「一次都没检索」。按 trace 数不吃这一跤。
        "kb_retrieved_events": len(kb.query(
            store, "SELECT seq FROM event_log WHERE event_type='KbRetrieved'"
                   " AND trace_id=?", (trace_id,))),
        # 候选集大小 = 阶段一过完七维硬约束之后还剩几条。它是「融合排序有没有话可说」
        # 的直接量度：只有 1 条时四通道排给谁看都一样，对照实验说明不了检索质量。
        "kb_candidate_count": _candidate_count(store, trace_id),
        # 放行的那些（既有字段，口径不变：主管核对后放行的高风险任务）
        "human_approval_stops": [d["title"] for d in dispositions if d["approved"]],
        # 全部处置明细，含被驳回的 —— 「谁停下了、等的是哪一类人、人怎么判的」
        # 三件事得在证据里分得开，只留一个 title 列表分不开。
        "human_dispositions": dispositions,
        "biz_status": (case or {}).get("biz_status"),
        "failed_tasks": [{"title": t["title"], "error": t["last_error"]}
                         for t in rows if t["last_error"]],
        "finance_entries": len(objects.query(
            store, "SELECT 1 FROM finance_entry WHERE tenant_id=? AND case_id=?",
            (TENANT_ID, case_id))),
        "settled": (case or {}).get("biz_status") == "settled",
        "plan_done": plan["state"] == PlanState.DONE,
    }


def _candidate_count(store, trace_id: str) -> int:
    """本段规划期那次检索的候选集大小。从 event_log 读，不从内存拼（铁律 3）。"""
    rows = kb.query(
        store, "SELECT detail FROM event_log WHERE event_type='KbRetrieved'"
               " AND trace_id=? ORDER BY seq", (trace_id,))
    for row in rows:
        try:
            return int(json.loads(row["detail"]).get("candidate_count") or 0)
        except (TypeError, ValueError):
            continue
    return 0


def _kb_funnel(store) -> dict:
    """检索漏斗的三级数字：库存 -> 同租户 -> 七维预过滤后。

    三个数一起看才说明问题：只报候选集大小，看不出预过滤到底砍掉了什么；
    只报库存，看不出跨租户的那一半从来没进过候选集。
    """
    from maos.kb.retriever import prefilter

    total = kb.query(store, "SELECT COUNT(1) AS n FROM kb_doc")[0]["n"]
    same_tenant = kb.query(
        store, "SELECT COUNT(1) AS n FROM kb_doc WHERE tenant_id=?", (TENANT_ID,))[0]["n"]
    candidates = prefilter(store, {
        "tenant_id": TENANT_ID, "biz_type": BIZ_TYPE, "channel_id": CHANNEL_ID,
        "region": REGION, "sku": SKU, "policy_version": POLICY_VERSION})
    by_kind = kb.query(
        store, "SELECT kind, COUNT(1) AS n FROM kb_doc GROUP BY kind ORDER BY kind")
    return {
        "kb_doc_total": int(total),
        "same_tenant": int(same_tenant),
        "after_prefilter": len(candidates),
        "by_kind": {r["kind"]: int(r["n"]) for r in by_kind},
        "corpus_root": os.path.join("scenarios", "refund"),
    }


def _gate_result(verdicts: list[dict]) -> str:
    """整趟 Gate 的判定：任何一条 rework 即 blocker，否则 pass。"""
    if any(v.get("verdict") == "rework" for v in verdicts):
        return "blocker"
    return "pass" if verdicts else "no_verdict"


def _finance_gate(task_rows: list[dict], verdicts: list[dict]) -> str:
    """第六道闸的真实判定：`not_triggered` / `pass` / `blocker`。

    **先读闸真的说了什么，再谈触没触发**。闸有两条判据（P7 起）：任务级按
    `task.inputs` 的 `biz_type + amount_claimed` 触发，plan 级按「这个 Plan 报了超阈
    金额却没有任何任务带着它」触发。后者命中时**没有任何一个任务的顶层 inputs 带
    金额** —— 照着任务级的触发面去反推，会把一条真的 blocker 记成 `not_triggered`。
    所以这里的顺序是：`gate_results["finance"] == "fail"` 即 blocker，不问是哪条判据。

    `not_triggered` 仍是**有意义的一档**，不是「没数据」：两条判据都没开口，才说明
    这个 Plan 里根本没有超阈的钱。它必须能与 `pass` 区分，不能都记成「闸过了」。
    """
    from maos.runtime.gate import DEFAULT_FINANCE_THRESHOLD, FINANCE_BIZ_TYPE

    judged_all = [v for v in verdicts if isinstance(v.get("gate_results"), dict)]
    if any(v["gate_results"].get("finance") == "fail" for v in judged_all):
        return "blocker"

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

    judged = [v for v in judged_all if v.get("task_id") in triggering]
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
        "rule_no": RULE_NO, "gateway_code": None,
        "kind": doc_kind, "outcome": outcome, "source_case_id": case_id,
        "title": "轴承锈蚀全额退款：财务核算不可省",
        "body": body,
        "embedding": embed(f"轴承 锈蚀 退款 财务核算 政策 {RULE_NO}"),
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
        # 三行叙事：闸判出计划缺陷 -> 控制面第三出口转人工 -> 主管驳回。
        # 每一行都从这一段的真实观测里取，不写死任何结论。
        for rejected in [d for d in without_kb["human_dispositions"]
                         if not d["approved"]]:
            print(f"  第六道闸：计划缺陷 {without_kb['finance_gate']}"
                  f"（漏排财务核算，付款前拿不到金额凭据）")
            print(f"  第三出口：{rejected['title']} -> BLOCKED"
                  f"（await={rejected['await']}，机器返工修不好，转人工）")
            print(f"  主管裁决：驳回 -> 计划收敛到 {without_kb['plan_state']}，"
                  f"付款一次都没派发")
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
    funnel = _kb_funnel(store)
    print(f"\n差异：delta_tasks={delta}，触发文档="
          f"{[(h['doc_id'], h['score']) for h in hits]}")
    print(f"检索漏斗：库存 {funnel['kb_doc_total']} 条 -> 同租户 {funnel['same_tenant']} 条"
          f" -> 七维预过滤后 {funnel['after_prefilter']} 条"
          f"（with_kb 段实测候选集 {with_kb['kb_candidate_count']} 条）")
    return {
        "experiment": "R5 · RAG 有无对照",
        "variable": f"{kb.KB_ENABLED_ENV}=0 / 1，两段的计划脚本逐字节相同",
        "kb_funnel": funnel,
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
