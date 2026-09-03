"""自定义退款 case —— 把一份**你自己写的 JSON** 跑成一次真实处置。

    python3 scripts/run_case.py <你的 case.json>

## 与三组对照（`maos/flows/contrast.py`）的分工

`contrast.py` 跑的是**带判据**的对照实验：case json 必须有 `_expected` 块，
跑完逐条比对，不符即抛 —— 它回答「结论对不对」。本模块回答另一个问题：
「**换成我的数据，它会怎么判**」。于是：

  · 不要求 `_expected`，也不做任何判据比对 —— 你的数据没有标准答案；
  · 政策视图、指令展开、窗口判定、DAG 骨架**逐个复用** `contrast.py` 的函数，
    一行都不另抄（两套口径迟早分叉，症状是「同一份数据两个结论」，且不报错）；
  · 在对照骨架之后**补上支付段**（发起 -> 轮询观察）：对照实验用不着它，
    而「钱到没到账」正是自定义 case 要看的那个结果。

## 输出里哪些是观察、哪些是推断

`biz_status == "settled"` **只可能由 payment.observe 写入**（铁律 8）。
网关问不出终态时本模块什么都不写，案子停在 `gateway_accepted`，
输出里 `settled_observations` 就是 0 —— 这不是 bug，是设计。
注入了失败码的那一跑同理：本模块**不做域内补偿**（那是场景 7 的面），
它只如实报出案子停在哪、Plan 收在什么状态。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from maos.agents.manager import ManagerAgent
from maos.agents.refund import ROLE_FINANCE, ROLE_PAYMENT
from maos.contracts.events import new_id
from maos.contracts.states import TaskState
from maos.domain.refund import fixtures, guard, objects
from maos.flows import contrast
from maos.flows.common import build, dump, run_until_settled
from maos.model.client import select_model_client
from maos.runtime.gate import HumanApprovalQueue
from maos.skills.builtin.refund import _common as C
from maos.tools.gateway import MockGateway

#: 网关按名取：`task.inputs` 会被 json.dumps，实例塞不进去（`_common.py` 第 3 条）。
GATEWAY_NAME = "custom-case"

#: >1 才能证明「一次 query 不一定够」—— 终态是问出来的，不是一步返回的。
DEFAULT_SETTLE_AFTER = 2

#: 审批是人的动作。CLI 代跑时名字写死，两次跑输出一致。
APPROVER = "沈思锴"

#: 少了任何一张，`policy_view` 就读不出政策，裁定无从谈起。
REQUIRED_TABLES = ("tenant", "channel", "product_snapshot", "order_snapshot", "policy_rule")

#: 审批捞几轮。放行一个可能让下游又停一个，但轮数有限 —— 无限循环会把
#: 「计划真的收敛不了」变成一个跑不完的进程，那种失败最难查。
MAX_APPROVAL_ROUNDS = 5


class CaseFileError(ValueError):
    """输入 JSON 不合形状。消息直接给人看，不用翻栈。"""


class RoomNotConnected(RuntimeError):
    """要了 `--matrix` 却没接通房间。消息直接给人看。"""


def room_degradation(bus) -> tuple[str, str]:
    """`--matrix` 这一跑到底进没进房间。返回 `(降级原因, 一行详情)`，接通时原因为空。

    **这一条非有不可**：降级之后终端照常刷「房间消息」，输出形态与真房间**一模一样**
    —— 截那个窗口当证据与真的分辨不出来。库内既有口径见
    `hiclaw/room_demo.py` 的 `_DEGRADE_SAYS`：`deps`（解释器没装 matrix-nio）与
    `connect`（连不上/token 失效/撞加密房）要拦，`env`（四个必填没配齐）不拦 ——
    那是明确的降级意图，不是意外。
    """
    reason = getattr(bus, "degrade_reason", None)
    if reason is None:
        return ("no-bus", "事件总线不是 MatrixEventBus —— hiclaw 没接上")
    if not reason or reason == "env":
        return ("", "")
    return (str(reason), str(getattr(bus, "degrade_detail", "") or ""))


# --------------------------------------------------------------------- 读输入
def load(path: str | Path, *, require_case: bool = True) -> dict:
    """读一份自定义 case，并把「缺什么」当场说清楚。

    不做「缺了就补一个缺省值」：靶场少一张表意味着裁定的前提变了，
    而补默认值会让它照常跑绿 —— 跑绿的错结论比报错难查得多。

    `require_case=False` 读的是**底账**（`scenarios/custom/ledger.json`）：五张外部
    快照表照样逐张查，只是不要求 `case` 块 —— 那一块由申请表逐行合成
    （`scripts/run_requests.py`），不写在底账里。
    """
    p = Path(path)
    if not p.exists():
        raise CaseFileError(f"找不到 case 文件：{p}")
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CaseFileError(f"{p} 不是合法 JSON：{exc}") from exc
    if not isinstance(payload, dict):
        raise CaseFileError(f"{p} 的顶层必须是对象（顶层键即表名）")

    missing = [t for t in REQUIRED_TABLES if not payload.get(t)]
    if missing:
        raise CaseFileError(
            f"{p} 缺这几张外部快照表：{', '.join(missing)}。形状照 "
            "scenarios/custom/refund-case.json（顶层键即表名，列名逐字对齐 schema.sql）")
    if require_case and (not isinstance(payload.get("case"), dict) or not payload["case"]):
        raise CaseFileError(f"{p} 缺 `case` 块 —— 那是 refund.intake 的建案入参，建不出案子")
    return payload


def _requested_at(payload: dict, seed: dict) -> str:
    """本次诉求的时刻。窗口判定靠它与订单 `paid_at` 现算天数，不读任何预置的天数。

    顺序：顶层 `requested_at` -> `case.requested_at` -> 现在。取到「现在」时
    窗口结论会随跑的日子变，这是真实行为，不是不确定性 —— 想要可复现就写死它。
    """
    for src in (payload.get("requested_at"), seed.get("requested_at")):
        if isinstance(src, str) and src.strip():
            return src.strip()
    return C.now_iso()


def _gateway_of(payload: dict, *, fail_with: str | None) -> MockGateway:
    """按输入造网关。`fail_with` 是错误码，注入到本单的 out_trade_no（= order_id）上。

    码必须在 `gateway_codes.ALL_CODES` 里，`MockGateway.__init__` 当场校验 ——
    未收录的码不许兜底成「默认可重试」，那正是最贵的一类 bug。
    """
    cfg = payload.get("gateway") if isinstance(payload.get("gateway"), dict) else {}
    settle_after = int(cfg.get("settle_after") or DEFAULT_SETTLE_AFTER)
    code = fail_with or cfg.get("fail_with")
    script = None
    if code:
        order_id = str(fixtures.case_seed_of(payload)["order_id"])
        script = {order_id: str(code)}
    return MockGateway(settle_after=settle_after, script=script)


# ----------------------------------------------------------------- DAG 补支付段
def _with_payment(tasks: list[dict], *, seed: dict, gateway: str) -> list[dict]:
    """在核算之后、通知之前插入支付任务。裁定为 reject 时原样返回。

    形状照 `flows/scenario_6.py` 的 TASK_PAYMENT：同样**只带网关名不带金额** ——
    申报金额只挂在核算那一步，别的退款任务带上它，就会被第六道闸要求交一份
    自己根本产不出的 finance_entry，闸恒 blocker。
    """
    finance = next((t for t in tasks if t["role"] == ROLE_FINANCE), None)
    if finance is None:
        return tasks                      # reject 不排核算，也就没有钱要付
    notify = next(t for t in tasks if (t["inputs"] or {}).get("step") == "notify")
    stem = finance["task_id"].rsplit("-", 1)[0]
    payment = {
        "task_id": f"{stem}-payment", "role": ROLE_PAYMENT,
        "title": "发起退款并观察网关终态",
        "inputs": {"biz_type": C.BIZ_TYPE, "tenant_id": seed["tenant_id"],
                   "case_id": seed["case_id"], "gateway": gateway},
        "acceptance": ["发起后不得写 settled", "终态必须由 query 观察得到"],
        "depends_on": [finance["task_id"]], "risk_level": "M",
    }
    notify["depends_on"] = [payment["task_id"]]
    out = list(tasks)
    out.insert(out.index(notify), payment)
    return out


def _obs_row(o: dict) -> dict:
    """一条支付观察。`poll_count` 与 `resolved_from` 都不是表上的列，在回执里。

    这两个字段是「终态是**问出来的**、不是本地推断的」那条论证仅有的可核证据：
    问了几次，以及一笔先报 unknown 的退款最后是被问成了什么。读不到它们，
    这条论证就只剩自述。
    """
    receipt = json.loads(o.get("raw_receipt_json") or "{}")
    detail = receipt.get("detail") if isinstance(receipt.get("detail"), dict) else {}
    return {
        "observed_state": o["observed_state"], "gateway_code": o.get("gateway_code"),
        "poll_count": receipt.get("poll_count"),
        "resolved_from": detail.get("resolved_from"),
    }


def _blocked_reason(cp, plan_id: str, task_id: str) -> str:
    """这个任务因为什么停下来等人 —— 取 event_log 里**最后一次**进 BLOCKED 的那一跳。

    判据取自 event_log 而不是任务行上的某个字段：`detail` 只落在迁移那一条事件上，
    在任务行上另开一个字段就有了第二份事实（`HumanApprovalQueue.pending` 同一口径）。
    """
    hits = [e for e in cp.store.list_event_log(plan_id)
            if e.get("event_type") == "StateTransition"
            and e.get("task_id") == task_id and e.get("to_state") == TaskState.BLOCKED]
    if not hits:
        return "未知：event_log 里没有进 BLOCKED 的那一跳"
    last = hits[-1]
    detail = last.get("detail")
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except json.JSONDecodeError:
            detail = {}
    detail = detail if isinstance(detail, dict) else {}
    kind = detail.get("human_exit") or detail.get("await") or ""
    note = str(detail.get("message") or detail.get("note") or "").strip()
    head = f"{last.get('reason') or '?'}" + (f"（{kind}）" if kind else "")
    return f"{head} —— {note}" if note else head


# --------------------------------------------------------------------- 跑一次
def run_payload(payload: dict, *, approve: bool = True, fail_with: str | None = None,
                matrix: bool = False, verbose: bool = True,
                allow_degraded: bool = False) -> dict:
    """跑一份自定义 case，返回**观测到的事实**（不含任何期望值）。"""
    seed = fixtures.case_seed_of(payload)
    tenant_id, case_id = str(seed["tenant_id"]), str(seed["case_id"])

    # 脚本是可变 dict：规划应答要等政策读出来才拼得出来，而 ScriptedModelClient
    # 每次 complete() 才查表。占位不能是空 dict（`script or {}` 会另造一个，
    # 后填的内容到不了客户端，症状是 Plan 里一个任务都没有且不报错）。
    script: dict[str, str] = {"用户请求": "{}"}
    model = select_model_client(script, force_scripted=True)
    store, bus, cp, model, worker, gate = build(script, matrix=matrix, model=model)

    if matrix and not allow_degraded:
        # 早失败：靶场都灌完了才发现没进房间，那一屏「房间消息」已经骗过人一次了。
        why, detail = room_degradation(bus)
        if why:
            raise RoomNotConnected(f"要了 --matrix 但房间没接通（{why}）：{detail}")

    fixtures.seed_case(store, payload)        # 五张外部快照表，只 INSERT 不建表
    fixtures.seed_policy_kb(store)            # 知识库照灌全量：检索本来就该在
    fixtures.seed_history_kb(store)           # 有别人家知识的库里做，跨租户召不回才有说服力

    C.reset_gateways()
    C.register_gateway(GATEWAY_NAME, _gateway_of(payload, fail_with=fail_with))

    # ---- 规划期读政策：版本锁定 + 渠道过滤，全走冻结口径 ----
    view = contrast.policy_view(store, tenant_id=tenant_id, order_id=str(seed["order_id"]),
                                order_version=int(seed["order_version"]))
    directives = contrast.policy_directives(view["rules"])
    paid_at = objects.query(
        store, "SELECT paid_at FROM order_snapshot WHERE tenant_id=? AND order_id=? AND version=?",
        (tenant_id, seed["order_id"], int(seed["order_version"])))[0]["paid_at"]
    requested_at = _requested_at(payload, seed)
    days = contrast.elapsed_days(paid_at, requested_at)
    verdict = contrast.evaluate_eligibility(view["rules"], reason_code=str(seed["reason_code"]),
                                            elapsed_days=days)

    # `_signals_of` 刻意直接复用：工单那条信号的口径（标题/正文由 case 数据推出，
    # 不现编情节）就在它里面，另抄一份就是第二套口径。
    tasks = _with_payment(
        contrast.plan_tasks(seed=seed, signals=contrast._signals_of(payload),
                            directives=directives, decision=verdict["decision"]),
        seed=seed, gateway=GATEWAY_NAME)
    script["用户请求"] = json.dumps({"tasks": tasks}, ensure_ascii=False)

    # ---- Manager 零改动复用：为代码域写的规划器，在退款域照样规划 DAG ----
    goal = contrast.GOAL_TEMPLATE.format(**{k: seed[k] for k in
                                            ("tenant_id", "channel_id", "reason_code")})
    trace_id, plan_id = new_id("trace"), new_id("plan")
    mgr = ManagerAgent(model, store=store)
    planned = mgr.plan(goal, context={
        "tenant_id": tenant_id, "biz_type": C.BIZ_TYPE, "channel_id": seed["channel_id"],
        "sku": seed["sku"], "plan_id": plan_id, "trace_id": trace_id})
    cp.create_plan(goal=goal, trace_id=trace_id, plan_id=plan_id, tasks=planned)
    cp.start_plan(plan_id)
    run_until_settled(bus, gate, cp, plan_id)

    # ---- 人工审批：停在 BLOCKED 的任务都在等人，CLI 代跑人的那一半 ----
    # **必须循环**：放行一个之后下游可能又停下来（比如裁定 reject 的计划走到
    # 第六道闸的 plan 级判据上），单轮只捞第一批，剩下的静默挂着，Plan 收在
    # RUNNING 而摘要看不出为什么 —— 那正是 `HumanApprovalQueue.pending` 的
    # docstring 点名的「漏捞比多捞坏」。
    hq = HumanApprovalQueue(store, cp)
    approvals: list[dict] = []
    human_exits: list[dict] = []
    who = f"{APPROVER}（{directives['approver_role']}）"
    for _ in range(MAX_APPROVAL_ROUNDS):
        pending = hq.pending(plan_id)
        if not pending:
            break
        for blocked in pending:
            human_exits.append({
                "task_id": blocked["task_id"], "title": blocked["title"],
                "why": _blocked_reason(cp, plan_id, blocked["task_id"]),
                "decision": "approved" if approve else "rejected",
            })
            if approve:
                # 顺序不可换：先落 approval_record（人的决定），再放行任务 ——
                # payment.execute 会核对审批记录，没有它就拒绝发起付款。
                approvals.append(C.record_approval(
                    store, tenant_id=tenant_id, case_id=case_id, approver=who,
                    decision="approved", reason=f"金额与订单锁定的政策 v{view['pinned']} 一致"))
                hq.decide(blocked["task_id"], approved=True, operator=who,
                          note=f"按 {directives['approver_role']} 权限放行")
            else:
                hq.decide(blocked["task_id"], approved=False, operator=who, note="主管驳回")
        run_until_settled(bus, gate, cp, plan_id)

    if verbose:
        dump(cp, plan_id, f"自定义 case {case_id}")
    return _observe(store, cp, plan_id, seed=seed, view=view, directives=directives,
                    verdict=verdict, days=days, paid_at=paid_at, requested_at=requested_at,
                    approvals=approvals, approve=approve, human_exits=human_exits)


def _observe(store, cp, plan_id: str, *, seed: dict, view: dict, directives: dict,
             verdict: dict, days: int, paid_at: str, requested_at: str,
             approvals: list[dict], approve: bool, human_exits: list[dict]) -> dict:
    """把这一跑的事实收成一份字典。只读库，不做任何判定。"""
    tenant_id, case_id = str(seed["tenant_id"]), str(seed["case_id"])

    def q(sql: str) -> list[dict]:
        return objects.query(store, sql, (tenant_id, case_id))

    entries = q("SELECT * FROM finance_entry WHERE tenant_id=? AND case_id=?")
    breakdown = json.loads(entries[0]["breakdown_json"]) if entries else {}
    obs = q("SELECT * FROM payment_observation WHERE tenant_id=? AND case_id=?")
    notes = q("SELECT * FROM notification WHERE tenant_id=? AND case_id=?")
    case = guard.get_case(store, tenant_id, case_id) or {}
    return {
        "case_id": case_id, "tenant_id": tenant_id,
        "channel_id": str(seed["channel_id"]), "reason_code": str(seed["reason_code"]),
        "amount_claimed": seed.get("amount_claimed"),
        "paid_at": paid_at, "requested_at": requested_at, "elapsed_days": days,
        "pinned_policy_version": view["pinned"],
        "matched_rules": [r["ref"] for r in view["rules"]],
        "decision": verdict["decision"], "deciding_rule": verdict["rule_ref"],
        "no_reason_days": verdict["no_reason_days"], "why": verdict["why"],
        "extra_tasks": [s["task_key"] for s in directives["extra_tasks"]],
        "approver_role": directives["approver_role"],
        "approved": approve and bool(approvals),
        "approvals": [a["approver"] for a in approvals],
        "human_exits": human_exits,
        "amount_approved": breakdown.get("amount_approved", "0.00"),
        "policy_version_used": breakdown.get("policy_version"),
        "rule_refs": entries[0]["rule_refs"] if entries else None,
        "biz_status": case.get("biz_status"),
        "payment_observations": [_obs_row(o) for o in obs],
        "settled_observations": sum(1 for o in obs if o["observed_state"] == "settled"),
        "notifications": [{"channel": n.get("channel"), "acked": bool(n.get("ack_at"))}
                          for n in notes],
        "business_refs": len(objects.list_business_refs(store, plan_id=plan_id)),
        "plan_id": plan_id, "plan_state": cp.store.get_plan(plan_id)["state"],
        "tasks": [{"task_id": t["task_id"], "role": t["role"], "title": t["title"],
                   "state": t["state"], "attempt": t["attempt"]}
                  for t in cp.store.list_tasks(plan_id)],
    }


def run_file(path: str | Path, **kw: Any) -> dict:
    """读文件并跑。`load()` 的报错原样上抛，由 CLI 翻成人话。"""
    return run_payload(load(path), **kw)
