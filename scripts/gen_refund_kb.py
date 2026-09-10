#!/usr/bin/env python3
"""退款域流程知识语料生成器 —— 评委点名的九类，一次生成到 `scenarios/refund/kb/`。

    python3 scripts/gen_refund_kb.py            # 生成（覆盖写）
    python3 scripts/gen_refund_kb.py --check    # 只校验磁盘上那份是不是当前生成器的产物

## 为什么要有生成器，而不是手写这几百条 JSON

手写的语料改一次就漂一次：`kb_doc` 的列清单动了、错误码表补了一条、流程 DAG 改了形状，
手写的那份不会有任何报错，只会在几周后的检索侧表现为「某一类知识忽然召不回了」。
生成器把三件事钉在一起 —— **列清单取自 `kb.DOC_COLUMNS`、错误码逐字段取自
`maos/tools/gateway_codes.py`、任务形状逐字段取自真实 DAG**，任何一处改了，
`--check` 当场红。

## 确定性是硬要求，不是「顺便做到」

验收判据是「连跑两次产物逐字节相同」。所以本文件里**没有** `random`、没有
`datetime.now()`、没有集合迭代序：时间戳一律从固定基准 `_EPOCH` 加天数推出，
遍历一律走显式元组或 `sorted()`，落盘一律 `sort_keys=True`。
少了这条，语料每跑一次就产生一份无意义的 diff，`git diff` 从此不能当判据用。

## 数据性质（铁律 3，口径与 `scenarios/refund/README.md` 同一份）

| 内容 | 性质 |
| :-- | :-- |
| 网关错误码及其 `retriable` / `outcome` / `remedy` / `layer` / `source` | **非合成**，逐字段取自 `maos/tools/gateway_codes.py` |
| 任务拆分模式的步骤形状与依赖边 | **非合成**，逐字段取自 `flows/scenario_6`、`flows/scenario_7`、`flows/contrast` 的真实 DAG |
| 驳回 / 沟通 / 到账三类的租户 `tnt-demo` 那部分 | **半合成**：单号 / 金额 / 时间取自 `scenarios/bulk/ledger-bulk.json` 的 60 单底账，处置理由按下面写死的规则从订单事实推导 |
| 其余（两个 mfg 租户的案例、政策渠道变体） | **合成**，按行业惯例构造 |

**不许把构造数据说成真实企业数据。** 每份产物的 `_provenance` 里都带着这段分界。

## body 里为什么只有 `task_pattern` 敢用 `steps` 这个键

`guardrails.apply_suggestions` 会把命中文档里的 `body["steps"]` 当成「可照做的步骤」
并入当前 DAG，而它**只按 kind 过滤**（`kb.POSITIVE_KINDS`），不看 outcome 也不看别的。
`policy` 与 `error_code_playbook` 都在 `POSITIVE_KINDS` 里，所以这两类的 body 一旦
多出 `steps` 键，R5 的 DAG 就会凭空多出几步 —— 而两版 diff 照样是绿的，没有任何症状。
本文件因此把处置步骤一律写成 `remedy_steps` / `pattern_note`，只有 `task_pattern`
（**不在** `POSITIVE_KINDS` 里）用 `steps`：它天生就是任务拆分知识，键名与
`guardrails._steps_of` 对齐，将来要启用只需把它加进 `POSITIVE_KINDS` 一处。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from maos import kb                                    # noqa: E402
from maos.tools import gateway_codes as gcodes         # noqa: E402

OUT_DIR = os.path.join(REPO_ROOT, "scenarios", "refund", "kb")
LEDGER_PATH = os.path.join(REPO_ROOT, "scenarios", "bulk", "ledger-bulk.json")
POLICY_PATH = os.path.join(REPO_ROOT, "scenarios", "refund", "policy", "policy_rules.json")

#: 所有 `created_at` 的基准。固定字面量而不是 `now()` —— 见模块头「确定性」。
_EPOCH = datetime(2026, 1, 5, tzinfo=timezone.utc)

#: `workflow_version` 的取值。**整数**，因为 `kb/schema.sql` 里那一列是 `INTEGER`
#: （历史语料写成 `"1.0.0"` 字符串，SQLite 弱类型不报错，但七维预过滤按整数比就永远不等）。
#: 语义映射记在这里一份：1 = 基线流程（场景 6 顺利路径），2 = 带补偿分支的流程（场景 7）。
WF_BASE = 1
WF_COMPENSATE = 2

#: 三个租户面。`tnt-mfg-a` / `tnt-mfg-b` 是 W-1 语料与 R5 的租户，`tnt-demo` 是
#: 60 单批量底账的租户。知识按租户各存一份而不是共享一份：`kb_doc` 的阶段一
#: 预过滤把 `tenant_id` 当硬约束，共享行做不到「跨租户永不召回」。
TENANTS = (
    {"key": "mfga", "tenant_id": "tnt-mfg-a", "region": "cn-hangzhou",
     "channels": ("ch-online", "ch-dealer"), "skus": ("SKU-BRG-6205", "SKU-SRV-A2")},
    {"key": "mfgb", "tenant_id": "tnt-mfg-b", "region": "cn-beijing",
     "channels": ("ch-online", "ch-dealer"), "skus": ("SKU-BRG-6205", "SKU-SRV-A2")},
    {"key": "demo", "tenant_id": "tnt-demo", "region": "cn-hangzhou",
     "channels": ("ch-online", "ch-tmall", "ch-dealer"), "skus": ()},
)

BIZ_TYPE = "refund"

_PROVENANCE = (
    "本目录由 scripts/gen_refund_kb.py 生成，勿手改 —— 手改的那一行下次重跑就没了。"
    "网关错误码及其 retriable / outcome / remedy / layer / source 逐字段取自"
    " maos/tools/gateway_codes.py（该模块每条码都核过支付宝开放平台官方出处）；"
    "任务拆分的步骤形状与依赖边逐字段取自 flows/scenario_6、flows/scenario_7、"
    "flows/contrast 的真实 DAG；租户 tnt-demo 的驳回 / 沟通 / 到账三类，单号与金额"
    "取自 scenarios/bulk/ledger-bulk.json 的 60 单底账，处置理由按生成器里写死的规则"
    "从订单事实推导；其余为按行业惯例构造的合成数据。"
    "不得作为真实企业数据引用。"
)


def _at(offset: int) -> str:
    """第 `offset` 天的时间戳。确定性的唯一来源，别在别处另算一份。"""
    return (_EPOCH + timedelta(days=int(offset))).isoformat()


def _slug(text: str) -> str:
    """错误码 -> doc_id 片段。`ACQ.SYSTEM_ERROR` -> `acq-system-error`。"""
    out = []
    for ch in str(text).lower():
        out.append(ch if ch.isalnum() else "-")
    return "".join(out).strip("-")


def _body(payload: dict) -> str:
    """body 一律紧凑 JSON。

    不是文风偏好：`policy.match::rule_params()` 用 `json.loads` 读 body，读不动就
    返回空 dict，`finance.settle` 随即落到「全额退不扣费」的缺省 —— 写成自然语言
    条款时金额不会报错，只会静默算错（口径见 scenarios/refund/README.md）。
    """
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _doc(*, tenant_id: str, doc_id: str, kind: str, title: str, body: dict,
         created_at: str, channel_id=None, region=None, sku=None,
         policy_version=None, workflow_version=None, rule_no=None,
         gateway_code=None, outcome=None, source_case_id=None) -> dict:
    """按 `kb.DOC_COLUMNS` 造一行。列清单从常量取，不在这里手抄第二份。

    `embedding` 恒为 `None`：向量由入库时按当时选定的嵌入实现现算，语料里预置一串数
    等于替使用方把「用哪个嵌入模型」这个决定提前做掉（口径同 W-1 的 24 条历史案例）。
    """
    row = {
        "tenant_id": tenant_id, "doc_id": doc_id, "biz_type": BIZ_TYPE,
        "channel_id": channel_id, "region": region, "sku": sku,
        "policy_version": policy_version, "workflow_version": workflow_version,
        "rule_no": rule_no, "gateway_code": gateway_code, "kind": kind,
        "title": title, "body": _body(body), "embedding": None,
        "outcome": outcome, "source_case_id": source_case_id, "created_at": created_at,
    }
    missing = set(kb.DOC_COLUMNS) - set(row)
    extra = set(row) - set(kb.DOC_COLUMNS)
    if missing or extra:                     # 列清单漂了就当场停，不产半份语料
        raise SystemExit(f"列清单与 kb.DOC_COLUMNS 不一致：多 {sorted(extra)}，缺 {sorted(missing)}")
    return row


# ---------------------------------------------------------------- 支付错误码 + 超时补偿
#: `(retriable, outcome)` 四象限 -> 处置口径。**这张表就是评委点名的第 6、7 类知识**
#: （支付错误码、超时与补偿路径）的判据来源。
#:
#: 为什么必须按两个字段的组合判、而不是只看 `retriable`：`40005` 是
#: `retriable=True + failed`（入口即拒，可以直接重发），`20000` 是
#: `retriable=True + unknown`（可能已经进了业务系统）—— 只看 `retriable`
#: 就会在 `20000` 上重发出第二笔退款。这两个字段正交，是原样抄的官方口径，
#: 不是按语感推断的（口径见 scenarios/refund/README.md「失败案例与错误码」）。
_QUADRANT = {
    (False, "success"): {
        "quadrant": "retriable=False + outcome=success",
        "disposition": "受理成功，转 payment.observe 按 query 问终态；不得据此写 settled",
        "remedy_steps": ["记录网关回执", "payment.observe 轮询 query 直到问出终态"],
        "backoff": None,
        "timeout_path": "轮询到上限仍问不出终态 -> 不写状态、不写观察行，转人工核查",
        "compensation_path": "无需补偿：这一档尚未失败",
    },
    (True, "unknown"): {
        "quadrant": "retriable=True + outcome=unknown",
        "disposition": "**禁止直接重发** —— 可能已进业务系统，重发就是第二笔退款；先 query 查证",
        "remedy_steps": ["payment.observe 先 query 查这笔有没有落地",
                         "查到记录 -> 按查到的终态推进",
                         "连续查不到且重发窗口未过 -> 才允许按原请求号重发"],
        "backoff": {"policy": "exponential", "base_seconds": 2, "max_attempts": 3,
                    "jitter": False},
        "timeout_path": "3 次 query 仍问不出 -> 不写终态，挂起转人工（铁律 8：观察不到就不许断言）",
        "compensation_path": "确认未执行且窗口已过 -> refund.compensate 开工单转线下",
    },
    (True, "failed"): {
        "quadrant": "retriable=True + outcome=failed",
        "disposition": "入口即拒，业务系统未收单，退避后可按原请求号直接重发",
        "remedy_steps": ["按退避表等待", "原请求号重发", "仍失败则转 refund.compensate"],
        "backoff": {"policy": "exponential", "base_seconds": 2, "max_attempts": 3,
                    "jitter": False},
        "timeout_path": "重发到上限仍拒 -> 转人工，不改业务状态",
        "compensation_path": "refund.compensate 开工单，记明重发次数与最后一次错误码",
    },
    (False, "failed"): {
        "quadrant": "retriable=False + outcome=failed",
        "disposition": "确定性失败，**不许重发** —— 重发只会拿到同一个错，先按 remedy 修因",
        "remedy_steps": ["按官方 remedy 修因（改参数 / 充值 / 核对单号）",
                         "修因后作为新请求发起", "修不了则转 refund.compensate"],
        "backoff": None,
        "timeout_path": "不适用：本档不重试",
        "compensation_path": "refund.compensate 开工单转线下，工单里带上原始错误码与 remedy",
    },
    (False, "unknown"): {
        "quadrant": "retriable=False + outcome=unknown",
        "disposition": "**既不许重发、也不许当失败** —— 必须 query 问清历史执行结果",
        "remedy_steps": ["query 历史执行结果", "按查到的结果推进；查不到才按未执行处理"],
        "backoff": None,
        "timeout_path": "问不清 -> 挂起转人工，业务状态保持原样",
        "compensation_path": "确认未执行后才允许 refund.compensate；确认已执行则按已退处理",
    },
}


def gen_error_code_playbooks() -> list[dict]:
    """11 条官方码 × 3 个租户 = 33 条 playbook。

    `outcome` 列一律留 **NULL**，不映射官方的 `outcome`：本列的语义是「这条知识记的
    那一单成没成」，而 playbook 记的不是某一单，是处置口径。更要紧的是官方 `outcome`
    有 `unknown` 这一档，而本列的取值域只有 `success` / `failed` —— 把 `unknown`
    压成 `failed` 正是铁律 8 说的那种 bug（把查不到当成没发生）。官方那三个字段
    原样躺在 body 的 `gateway` 里，谁要用都取得到。
    """
    docs = []
    for tenant in TENANTS:
        for idx, code in enumerate(gcodes.ALL_CODES):
            spec = gcodes.lookup(code)
            quad = _QUADRANT[(bool(spec.retriable), str(spec.outcome))]
            docs.append(_doc(
                tenant_id=tenant["tenant_id"],
                doc_id=f"kb-ecp-{tenant['key']}-{_slug(code)}",
                kind=kb.KIND_ERROR_CODE_PLAYBOOK,
                title=f"支付错误码 {code}：{spec.message[:28]}（{quad['quadrant']}）",
                body={
                    "gateway": {
                        "code": spec.code, "message": spec.message,
                        "retriable": spec.retriable, "outcome": spec.outcome,
                        "remedy": spec.remedy, "layer": spec.layer, "source": spec.source,
                    },
                    **quad,
                    "authority_note": (
                        "网关是权威事实源，MAOS 只持有观察与推断；"
                        "没有 payment_observation 观察行就不许说「已到账」（铁律 8）"),
                },
                # 错误码处置与渠道 / 地区 / 商品 / 政策版本都无关，一律留 NULL 走通配。
                # 照抄成具体值会把一条通用处置锁死在一个渠道上，而且不报错。
                gateway_code=code,
                created_at=_at(idx),
            ))
    return docs


# ---------------------------------------------------------------- 任务拆分
def _shape_from_tasks(tasks: list[dict]) -> list[dict]:
    """把一份真实 DAG 压成步骤清单，形状与 `guardrails.case_to_doc_body` 对齐。

    **事实字段在这里就剔掉**，不是等检索回来再靠护栏 2 拦：`case_id` / `tenant_id` /
    `order_id` / `amount_claimed` 这些是那一单的数据，不是流程知识。脏数据不进库，
    比进库后拦得住更可靠（口径同 `guardrails.case_to_doc_body`）。

    依赖存成 `[role, step]` 键而不是 `task_id`：id 只在那一份计划里有意义，
    存进知识库就是一串指不到任何东西的字符串，照着它接边只会接空。
    """
    from maos.kb.guardrails import ORDER_FACT_FIELDS, task_key

    id_to_key = {t["task_id"]: list(task_key(t)) for t in tasks if t.get("task_id")}
    steps = []
    for task in tasks:
        inputs = task.get("inputs") or {}
        if not isinstance(inputs, dict):
            inputs = {}
        steps.append({
            "role": task.get("role"),
            "title": task.get("title"),
            "inputs": {k: v for k, v in inputs.items()
                       if k not in ORDER_FACT_FIELDS and k in ("step", "channel")},
            "acceptance": list(task.get("acceptance") or []),
            "risk_level": task.get("risk_level") or "L",
            "effect_risk": task.get("effect_risk") or "L",
            "depends_on_keys": [id_to_key[d] for d in (task.get("depends_on") or [])
                                if d in id_to_key],
        })
    return steps


def _real_dag_shapes() -> list[dict]:
    """四种流程形状，逐字段取自仓库里真实跑着的 DAG —— 不在这里手写第二份。

    手写的后果是流程改了形状而语料不知道，于是「任务拆分知识」教的是一条早就不存在
    的流程，且没有任何东西会红。这四份都从模块常量 / 纯函数取，代码一改，
    `--check` 当场报 diff。
    """
    from maos.flows import contrast, scenario_6, scenario_7

    # 场景 6：顺利路径（受理 -> 裁定 -> 核算 -> 支付 -> 通知）
    happy = _shape_from_tasks(scenario_6._TASKS)
    # 场景 7：支付渠道异常，带补偿分支
    compensated = _shape_from_tasks(scenario_7._TASKS)

    # contrast：同一份 plan_tasks，喂不同 directives / decision 就是不同形状。
    # 走真函数而不是抄它的输出 —— 抄一份就等着两边漂。
    seed = {"case_id": "case-pattern", "tenant_id": "tnt-pattern",
            "channel_id": "ch-dealer", "amount_claimed": 0}
    dealer = contrast.plan_tasks(
        seed=seed, signals=[],
        directives={"extra_tasks": [{"task_key": "dealer_writeoff", "owner_role": "refund_channel",
                                     "title": "渠道商核销", "rule_ref": "AS-004@v1"}],
                    "approver_role": "region_manager"},
        decision=contrast.DECISION_APPROVE)
    rejected = contrast.plan_tasks(
        seed={**seed, "channel_id": "ch-online"}, signals=[],
        directives={"extra_tasks": [], "approver_role": "supervisor"},
        decision=contrast.DECISION_REJECT)

    return [
        {"slug": "happy-path", "title": "顺利路径：受理→裁定→核算→支付→观察→通知",
         "steps": happy, "workflow_version": WF_BASE, "channel_id": None,
         "source": "maos/flows/scenario_6.py::_TASKS",
         "note": "五步骨架。settled 只可能由 payment.observe 写入，发起那一步写不出终态"},
        {"slug": "compensate-path", "title": "补偿路径：支付渠道异常，问不出终态转补偿",
         "steps": compensated, "workflow_version": WF_COMPENSATE, "channel_id": None,
         "source": "maos/flows/scenario_7.py::_TASKS",
         "note": "轮询到上限仍问不出终态时，不写状态、不写观察行，转 refund.compensate"},
        {"slug": "dealer-writeoff", "title": "经销渠道：政策命中 AS-004，多一步渠道商核销",
         "steps": dealer, "workflow_version": WF_BASE, "channel_id": "ch-dealer",
         "source": "maos/flows/contrast.py::plan_tasks（extra_tasks 展开）",
         "note": "多出来的那一步是政策的函数不是渠道的函数：规则里的 owner_role 逐字成为任务 role"},
        {"slug": "reject-no-settle", "title": "驳回：不予退款就不排核算，DAG 形状本身即结论",
         "steps": rejected, "workflow_version": WF_BASE, "channel_id": None,
         "source": "maos/flows/contrast.py::plan_tasks（decision=reject）",
         "note": "给一个 0 元分录会让下游误以为「核算过了，只是金额为零」，所以整步不排"},
    ]


def gen_task_patterns() -> list[dict]:
    """4 种真实 DAG 形状 × 3 个租户 = 12 条。

    **跨租户复制的是形状，不是事实** —— 与 `experiment.seed_kb_corpus` 痛斥的
    「把别人家的政策贴上本租户标签」不是一回事：那里搬的是政策内容（张三家的规则
    成了李四家的），这里搬的是「受理→裁定→核算→支付→通知」这个骨架，
    事实字段已经在 `_shape_from_tasks` 里剔干净了，每个租户本来就各需要一份。
    """
    docs = []
    for tenant in TENANTS:
        for idx, shape in enumerate(_real_dag_shapes()):
            docs.append(_doc(
                tenant_id=tenant["tenant_id"],
                doc_id=f"kb-tp-{tenant['key']}-{shape['slug']}",
                kind=kb.KIND_TASK_PATTERN,
                title=shape["title"],
                body={
                    # 只有本类敢用 `steps` 这个键，理由见模块头。
                    "steps": shape["steps"],
                    "step_count": len(shape["steps"]),
                    "shape_source": shape["source"],
                    "note": shape["note"],
                },
                channel_id=shape["channel_id"],
                workflow_version=shape["workflow_version"],
                created_at=_at(40 + idx),
            ))
    return docs


# ---------------------------------------------------------------- 底账投影
def _ledger() -> dict:
    with open(LEDGER_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def _orders_by_id(ledger: dict) -> dict:
    return {row["order_id"]: row for row in ledger["order_snapshot"]}


def _payload(order: dict) -> dict:
    try:
        parsed = json.loads(order.get("payload_json") or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _reject_reason(order: dict, history: dict) -> tuple[str, str]:
    """从订单事实推导驳回理由。**规则写死在这里，不随机** —— 见模块头「确定性」。

    返回 `(reason_code, 理由原文)`。四条规则按顺序判，命中即停：
    没签收 -> 核验不了；超 30 天窗口 -> 过期；无质检报告 -> 质量主张缺证；其余 -> 举证不足。
    这是**构造**的判定，不是真实企业的驳回记录 —— 底账只给了 `status='rejected'`，
    没给理由，凭空写一条「真实理由」就是编造（铁律 3）。
    """
    payload = _payload(order)
    if not (payload.get("logistics") or {}).get("signed_at"):
        return "not_signed", "无签收记录，退货状态无法核验，退回补充物流签收凭据"
    paid = datetime.fromisoformat(order["paid_at"])
    decided = datetime.fromisoformat(history["decided_at"])
    if (decided - paid).days > 30:
        return "out_of_window", (
            f"申请时点距付款 {(decided - paid).days} 天，超出无理由退货窗口（30 天）")
    if not payload.get("qc_report"):
        return "no_qc_report", "主张质量问题但无质检报告，按 AS-002 需质检前置"
    return "insufficient_evidence", "举证材料不足以支持诉求，按政策驳回并告知补件路径"


def gen_rejections() -> list[dict]:
    """人工驳回：`tnt-demo` 7 条从底账投影 + 两个 mfg 租户各 3 条构造 = 13 条。

    `outcome` 记 `failed`：这一单确实没退成。它**不会**因此被当成规划正例 ——
    `apply_suggestions` 只认 `kb.POSITIVE_KINDS`，而 `rejection` 不在里面。
    """
    ledger = _ledger()
    orders = _orders_by_id(ledger)
    docs = []

    rejected = sorted((h for h in ledger["refund_history"] if h["status"] == "rejected"),
                      key=lambda h: h["case_id"])
    for idx, history in enumerate(rejected):
        order = orders.get(history["order_id"])
        if order is None:                    # 底账自洽，走不到；走到了就该有人知道
            raise SystemExit(f"底账里 {history['case_id']} 指的订单 {history['order_id']} 不存在")
        reason_code, reason = _reject_reason(order, history)
        docs.append(_doc(
            tenant_id=history["tenant_id"],
            doc_id=f"kb-rj-demo-{idx + 1:03d}",
            kind=kb.KIND_REJECTION,
            # 标题取理由的第一个分句，不按字数硬截 —— 截在词中间的标题在检索结果里
            # 读起来像残句，而标题是命中列表上唯一露出来的那几个字。
            title=f"人工驳回：{reason.split('，')[0]}",
            body={
                "situation": f"订单 {order['order_id']}（{order['sku']}，"
                             f"{order['channel_id']}）提出退款，进入人工复核",
                "rejected_by": "supervisor",
                "reason_code": reason_code,
                "reason": reason,
                "decided_at": history["decided_at"],
                "appeal_path": "补齐材料后可在 15 日内申请复议，复议走同一条 refund.intake",
                "lesson": "驳回理由必须落到具体政策条款与缺失的那份材料上，"
                          "只写「不符合政策」的驳回会在复议时被推翻",
            },
            channel_id=order["channel_id"],
            region=ledger["tenant"][0]["region"],
            sku=order["sku"],
            policy_version=int(order["policy_version_at_order"]),
            workflow_version=WF_BASE,
            outcome=kb.OUTCOME_FAILED,
            source_case_id=history["case_id"],
            created_at=_at(60 + idx),
        ))

    # 两个 mfg 租户各 3 条：R5 与三组对照跑在这两个租户上，没有本租户的驳回知识
    # 就等于这一类对它们不存在（跨租户永不召回是硬约束，不是打分项）。
    mfg_cases = (
        ("artificial_damage", "AS-003", "人为损坏免责：图片显示外力撞击痕迹，按 AS-003 驳回",
         "举证图片里的凹陷位置与运输受力点不符，判定为使用中外力损坏"),
        ("out_of_window", "AS-001", "超出无理由退货窗口，按 AS-001 驳回",
         "签收后第 34 天提出无理由退货，窗口 30 天，超期 4 天"),
        ("no_qc_report", "AS-002", "质保期内质量主张缺质检报告，按 AS-002 驳回",
         "v2 起质检前置，客户未送检即要求全额退，退回补检"),
    )
    for tenant in TENANTS:
        if tenant["key"] == "demo":
            continue
        for idx, (reason_code, rule_no, title, reason) in enumerate(mfg_cases):
            docs.append(_doc(
                tenant_id=tenant["tenant_id"],
                doc_id=f"kb-rj-{tenant['key']}-{idx + 1:03d}",
                kind=kb.KIND_REJECTION,
                title=title,
                body={
                    "situation": "客户提出退款诉求，证据链进入人工复核",
                    "rejected_by": "supervisor",
                    "reason_code": reason_code,
                    "reason": reason,
                    "appeal_path": "补齐材料后可在 15 日内申请复议",
                    "lesson": "驳回理由必须落到具体政策条款与缺失的那份材料上",
                },
                region=tenant["region"],
                sku=tenant["skus"][idx % len(tenant["skus"])],
                policy_version=1,
                workflow_version=WF_BASE,
                rule_no=rule_no,
                outcome=kb.OUTCOME_FAILED,
                created_at=_at(70 + idx),
            ))
    return docs


def gen_comms_results() -> list[dict]:
    """客户沟通结果：通知 / ack / 二次沟通。`tnt-demo` 6 条 + 两个 mfg 各 3 条 = 12 条。

    ack 缺失**不阻塞**主链路（口径同 `notify.customer`），但它是「客户到底知不知道」
    的唯一证据 —— 没有 ack 就只是「我们这边发出去了」，不是「客户收到了」。
    """
    ledger = _ledger()
    orders = _orders_by_id(ledger)
    docs = []

    # 底账里 settled 与 rejected 各取前 3 单，两种结局的沟通姿态本来就不同。
    picks = []
    for status in ("settled", "rejected"):
        rows = sorted((h for h in ledger["refund_history"] if h["status"] == status),
                      key=lambda h: h["case_id"])[:3]
        picks.extend((status, h) for h in rows)

    for idx, (status, history) in enumerate(picks):
        order = orders[history["order_id"]]
        acked = status == "settled"          # 退成了的单，客户回执率高；驳回的常有二次沟通
        docs.append(_doc(
            tenant_id=history["tenant_id"],
            doc_id=f"kb-cm-demo-{idx + 1:03d}",
            kind=kb.KIND_COMMS_RESULT,
            title=("退款到账通知已获客户确认" if acked
                   else "驳回通知未获确认，二次电话沟通后息诉"),
            body={
                "channel": "sms" if acked else "phone",
                "notified_at": history["decided_at"],
                "ack": acked,
                "ack_lag_hours": 6 if acked else None,
                "second_contact": None if acked else "电话说明驳回依据与补件路径",
                "customer_reply": "已收到，无异议" if acked else "对结论有异议，已告知复议路径",
                "case_result": status,
                "lesson": ("ack 缺失不阻塞流程，但它是「客户知情」的唯一证据；"
                           "驳回类通知必须留二次沟通记录，否则复议时说不清告知过什么"),
            },
            channel_id=order["channel_id"],
            region=ledger["tenant"][0]["region"],
            sku=order["sku"],
            workflow_version=WF_BASE,
            outcome=kb.OUTCOME_SUCCESS if acked else kb.OUTCOME_FAILED,
            source_case_id=history["case_id"],
            created_at=_at(80 + idx),
        ))

    mfg_cases = (
        (True, "sms", "短信通知退款结果，6 小时内获 ack", "已收到，无异议"),
        (False, "email", "邮件通知退回补件，三日无回音转电话", "邮箱未读，改电话联系后补件"),
        (True, "phone", "电话说明扣费口径后客户当场确认", "认可按 v2 扣 2% 手续费"),
    )
    for tenant in TENANTS:
        if tenant["key"] == "demo":
            continue
        for idx, (acked, channel, title, reply) in enumerate(mfg_cases):
            docs.append(_doc(
                tenant_id=tenant["tenant_id"],
                doc_id=f"kb-cm-{tenant['key']}-{idx + 1:03d}",
                kind=kb.KIND_COMMS_RESULT,
                title=title,
                body={
                    "channel": channel,
                    "ack": acked,
                    "ack_lag_hours": 6 if acked else None,
                    "second_contact": None if acked else "三日无回音后改电话",
                    "customer_reply": reply,
                    "lesson": "ack 缺失不阻塞流程，但没有 ack 就只是「发出去了」，不是「客户收到了」",
                },
                channel_id=tenant["channels"][idx % len(tenant["channels"])],
                region=tenant["region"],
                workflow_version=WF_BASE,
                outcome=kb.OUTCOME_SUCCESS if acked else kb.OUTCOME_FAILED,
                created_at=_at(90 + idx),
            ))
    return docs


def gen_arrival_results() -> list[dict]:
    """真实到账结果：观察行的终态与耗时。`tnt-demo` 9 条 + 两个 mfg 各 3 条 = 15 条。

    **每条都记明 `basis`**：说「已到账」的唯一依据是 `payment_observation` 那一行，
    不是「我们发起过」也不是「网关受理了」。没有观察行就不许说到账（铁律 8），
    所以未观察到的那几条 `observed_state` 记 `unsettled` 而不是空着 —— 空着会被
    下游当成「还没查」，而事实是「查了，没到」。
    """
    ledger = _ledger()
    orders = _orders_by_id(ledger)
    docs = []

    settled = sorted((h for h in ledger["refund_history"] if h["status"] == "settled"),
                     key=lambda h: h["case_id"])
    for idx, history in enumerate(settled):
        order = orders[history["order_id"]]
        # 轮询次数与耗时按单号推出（确定性），不是随机数：底账没记这两个字段，
        # 而「查了几次、隔多久到」正是这一类知识要回答的问题。
        attempts = 2 + (idx % 3)
        docs.append(_doc(
            tenant_id=history["tenant_id"],
            doc_id=f"kb-ar-demo-{idx + 1:03d}",
            kind=kb.KIND_ARRIVAL_RESULT,
            title=f"退款到账：query 第 {attempts} 次问到 settled，金额 {history['amount']}",
            body={
                "observed_state": "settled",
                "basis": "payment_observation",
                "query_attempts": attempts,
                "elapsed_hours": 4 * attempts,
                "amount": history["amount"],
                "decided_at": history["decided_at"],
                "authority_note": "终态由外部网关持有；本条记的是观察结果，不是我们写定的状态",
                "lesson": "发起成功不等于到账，gateway_accepted 只推到 processing；"
                          "settled 全系统只有 payment.observe 写得进去",
            },
            channel_id=order["channel_id"],
            region=ledger["tenant"][0]["region"],
            sku=order["sku"],
            policy_version=int(order["policy_version_at_order"]),
            workflow_version=WF_BASE,
            outcome=kb.OUTCOME_SUCCESS,
            source_case_id=history["case_id"],
            created_at=_at(100 + idx),
        ))

    mfg_cases = (
        ("settled", 2, "退款到账：query 第 2 次问到 settled", kb.OUTCOME_SUCCESS,
         "两次轮询即到账，属自营渠道常态"),
        ("unsettled", 3, "轮询 3 次仍未到账：不写终态，转人工核查", kb.OUTCOME_FAILED,
         "问不出终态时一行观察都不写、一个状态都不推 —— 空着比猜一个终态安全"),
        ("settled", 5, "退款到账：跨行到账慢，第 5 次才问到 settled", kb.OUTCOME_SUCCESS,
         "跨行到账 T+1 属正常，别在第 3 次没问到时就判失败并重发"),
    )
    for tenant in TENANTS:
        if tenant["key"] == "demo":
            continue
        for idx, (state, attempts, title, outcome, lesson) in enumerate(mfg_cases):
            docs.append(_doc(
                tenant_id=tenant["tenant_id"],
                doc_id=f"kb-ar-{tenant['key']}-{idx + 1:03d}",
                kind=kb.KIND_ARRIVAL_RESULT,
                title=title,
                body={
                    "observed_state": state,
                    "basis": "payment_observation" if state == "settled" else "no_observation",
                    "query_attempts": attempts,
                    "elapsed_hours": 4 * attempts,
                    "authority_note": "终态由外部网关持有；没有观察行就不许说「已到账」",
                    "lesson": lesson,
                },
                channel_id=tenant["channels"][idx % len(tenant["channels"])],
                region=tenant["region"],
                sku=tenant["skus"][idx % len(tenant["skus"])],
                workflow_version=WF_BASE,
                outcome=outcome,
                created_at=_at(110 + idx),
            ))
    return docs


# ---------------------------------------------------------------- 产品与渠道差异
def gen_policy_variants() -> list[dict]:
    """产品与渠道差异（评委点名的第 2 类），投影成 `kind='policy'` 的变体文档。

    与 `experiment.promote_policy_rule` 投影的那 16 条**不是一回事**：那 16 条是
    `policy_rule` 表的逐字搬运（规则本身），这 8 条讲的是「同一条规则在不同渠道 /
    不同品类上落地时差在哪」—— 规则表里没有这一层，它今天只活在人的脑子里。

    body 里**不许出现 `steps`**：`policy` 在 `kb.POSITIVE_KINDS` 里，多这个键会让
    `apply_suggestions` 凭空往 DAG 里补步骤（见模块头）。这里一律用 `differences`。
    """
    with open(POLICY_PATH, encoding="utf-8") as fh:
        rules = json.load(fh)["policy_rule"]
    by_tenant: dict[str, list[dict]] = {}
    for row in sorted(rules, key=lambda r: (r["tenant_id"], r["rule_no"], r["version"])):
        by_tenant.setdefault(row["tenant_id"], []).append(row)

    # 四个 (渠道, 品类) 组合，覆盖「渠道差异」与「产品差异」两个维度各两档。
    combos = (
        ("ch-online", "SKU-BRG-6205", "自营 x 轴承",
         ["自营渠道无核销环节，AS-004 取不到，DAG 少一步",
          "轴承质保 12 个月，AS-002 的 warranty_basis 按 product_snapshot 算",
          "标品退货走标准物流，签收凭据即可核验"]),
        ("ch-dealer", "SKU-BRG-6205", "经销 x 轴承",
         ["经销渠道命中 AS-004，多一步渠道商核销（role=refund_channel）",
          "审批人从 supervisor 抬到 region_manager",
          "核销未回执前不得发起支付，否则退款与渠道对账对不上"]),
        ("ch-online", "SKU-SRV-A2", "自营 x 伺服电机",
         ["伺服电机质保 24 个月，同样的申请日在轴承上超保、在这里仍在保",
          "大件退货需上门取件，物流凭据晚于申请 3-5 天到齐",
          "拆封后按 AS-003 判人为损坏，需图片举证"]),
        ("ch-dealer", "SKU-SRV-A2", "经销 x 伺服电机",
         ["同时命中 AS-004 与 AS-002，核销与质检两条前置并行",
          "两条前置都回执后才排核算，任一缺失则整单挂起",
          "大额退款触发第六道财务复核闸，Gate 过了也不自动放行"]),
    )

    docs = []
    for tenant in TENANTS:
        if tenant["key"] == "demo":          # 底账租户没有 AS-004，谈不上渠道差异
            continue
        rule_nos = sorted({r["rule_no"] for r in by_tenant.get(tenant["tenant_id"], [])})
        for idx, (channel_id, sku, label, differences) in enumerate(combos):
            docs.append(_doc(
                tenant_id=tenant["tenant_id"],
                doc_id=f"kb-pv-{tenant['key']}-{_slug(channel_id)}-{_slug(sku)}",
                kind=kb.KIND_POLICY,
                title=f"渠道与产品差异：{label}",
                body={
                    "scope": {"channel_id": channel_id, "sku": sku},
                    "differences": differences,
                    "rules_in_scope": rule_nos,
                    "note": "本条讲的是同一条规则在不同渠道 / 品类上落地的差异，"
                            "规则正文以 policy_rule 表为准，本条不覆盖它",
                },
                channel_id=channel_id,
                region=tenant["region"],
                sku=sku,
                policy_version=1,
                workflow_version=WF_BASE,
                created_at=_at(120 + idx),
            ))
    return docs


# ---------------------------------------------------------------- 落盘
#: 一份产物一个文件。拆开而不是塞进一个大 JSON：装载方按类取用，
#: 出问题时 `git diff` 也能一眼看出是哪一类语料动了。
FILES = (
    ("error_code_playbooks.json", gen_error_code_playbooks,
     "支付错误码 + 超时与补偿路径：11 条官方码 x 3 个租户。四象限处置、退避表、"
     "补偿路径写进 body；官方 outcome=unknown 的码 outcome 列留 NULL（不许压成 failed）"),
    ("task_patterns.json", gen_task_patterns,
     "任务拆分：4 种真实 DAG 形状 x 3 个租户。步骤形状逐字段取自 scenario_6 / "
     "scenario_7 / contrast，事实字段已剔除"),
    ("rejections.json", gen_rejections,
     "人工驳回：tnt-demo 7 条从 60 单底账投影（理由按写死的规则从订单事实推导）+ "
     "两个 mfg 租户各 3 条构造"),
    ("comms_results.json", gen_comms_results,
     "客户沟通结果：通知 / ack / 二次沟通。tnt-demo 6 条 + 两个 mfg 租户各 3 条"),
    ("arrival_results.json", gen_arrival_results,
     "真实到账结果：观察行的终态与耗时。每条记明 basis —— 没有观察行就不许说到账"),
    ("policy_variants.json", gen_policy_variants,
     "产品与渠道差异：同一条规则在不同渠道 / 品类上落地的差异，两个 mfg 租户各 4 条"),
)


def build() -> dict[str, str]:
    """生成全部产物，返回 `{文件名: 文件内容}`。**不落盘** —— 落盘与校验共用这一份。"""
    out = {}
    for name, gen, note in FILES:
        docs = gen()
        payload = {"_note": note, "_provenance": _PROVENANCE, "kb_doc": docs}
        out[name] = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    return out


def _summary(built: dict[str, str]) -> dict[str, int]:
    counted: dict[str, int] = {}
    for text in built.values():
        for row in json.loads(text)["kb_doc"]:
            counted[row["kind"]] = counted.get(row["kind"], 0) + 1
    return counted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="不写盘，只校验磁盘上那份与当前生成器的产物逐字节一致")
    args = parser.parse_args(argv)

    built = build()
    os.makedirs(OUT_DIR, exist_ok=True)

    if args.check:
        bad = []
        for name, text in sorted(built.items()):
            path = os.path.join(OUT_DIR, name)
            try:
                with open(path, encoding="utf-8") as fh:
                    on_disk = fh.read()
            except FileNotFoundError:
                bad.append(f"{name}: 磁盘上没有这份产物")
                continue
            if on_disk != text:
                bad.append(f"{name}: 与当前生成器的产物不一致")
        if bad:
            print("产物与生成器不一致，请重跑 python3 scripts/gen_refund_kb.py：")
            for line in bad:
                print(f"  - {line}")
            return 1
        print(f"--check 通过：{len(built)} 份产物与生成器逐字节一致")
    else:
        for name, text in sorted(built.items()):
            with open(os.path.join(OUT_DIR, name), "w", encoding="utf-8") as fh:
                fh.write(text)
        print(f"已生成 {len(built)} 份产物到 scenarios/refund/kb/")

    counted = _summary(built)
    total = sum(counted.values())
    for kind in sorted(counted):
        print(f"  {kind:<22} {counted[kind]:>3}")
    print(f"  {'合计':<20} {total:>3}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
