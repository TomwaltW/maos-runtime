"""refund.compensate —— 退款走不通之后的**域内**补偿收口。

## 为什么退款域要有自己的补偿，而不是复用控制面那个

`ControlPlane._execute_compensation` 是**逆补丁补偿**：读 `compensation` artifact 的
`patch_ref`，把正向 `patch_set` 在沙箱里反着打一遍。它的前提是「产物是代码」。
退款域的产物不是补丁 —— 落地的是一笔**发给外部支付网关的请求**，没有可以 `git apply -R`
的东西。所以 `human_decision(approved=False)` 走到 `_execute_compensation` 时会拿到
「无补偿引用，跳过回滚」并返回 None，那是**正确行为，不是缺陷**：代码域没有东西要还原。

要还原的是业务侧的账：这笔退款请求作废、留一条补偿记录、开一张人工工单。
那三件事只有本域知道怎么做，所以落在本 skill 里。

## 本 skill 最要紧的一条：**不许宣布那笔钱没退出去**

走到补偿的典型场景是网关回了 `ACQ.SYSTEM_ERROR` / `20000` 这类
`retriable=True, outcome=unknown` 的码 —— 官方 remedy 原文是「保持参数不变重试
**或查询执行结果**」，也就是说**网关自己都不知道那一笔到底执行了没有**。

此时把 `refund_request` 标成「已撤销、钱没出去」就是把外部状态写死为终态（铁律 8），
而且是最贵的一种写死：真退过的话，账面上会凭空少一笔。所以本 skill 落的
`refund_request_revoked` 记录，语义严格限定为

    「MAOS 侧不再推进这笔请求，并把它连同最后一次观察到的下落交给人」

而**不是**「这笔钱确认没退」。最后一次观察到的下落原样抄进 `detail_json`
（`last_observed_state`，没观察到就是 `unobserved`），人工工单据此去外部系统对账。
这也是为什么补偿必须配一张工单：本系统能做的到此为止，剩下的只有人能做。

## 与 `payment.observe` 的分工

`payment.observe` 见到网关明确 `failed` 时只落一条 `payment_observation`，
**不推进业务状态** —— 它在源码里写明了理由：「走到 compensated 意味着补偿已经做完，
而补偿是失败路径场景的事，在这里替它宣布收口就是又一次把状态写死」。
本 skill 就是它让出来的那一步：补偿真做完了，才把案子推到 `compensated`。

`settled` 的案子一律拒绝补偿：钱已经确认退出去了，再走补偿是数据被改坏的信号，
不是一次可以静默吞掉的空操作。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from maos.domain import _schema_util
from maos.domain.refund import case_pack, guard, objects, roles
from maos.skills.contract import Skill, SkillContext, SkillContract
from maos.skills.registry import register_skill

from . import _common as C

#: 补偿记录的两种 kind。测试与场景按名取，不在各处抄字面量。
KIND_REQUEST_REVOKED = "refund_request_revoked"
KIND_MANUAL_TICKET = "manual_ticket"

#: 一次也没观察到时的占位。**不写成 "failed"** —— 没问出来和问出失败是两回事，
#: 混起来就等于替网关下了结论。
UNOBSERVED = "unobserved"

#: 事件类型与控制面的逆补丁补偿共用一个名字：对审计与 Trace 来说，
#: 「补偿执行过了」是同一件事，按 `detail.domain` 区分是哪一种。
#: 另起一个名字会让「这个 Plan 到底补偿过没有」要查两处，漏一处就是假绿。
EVENT_COMPENSATION_EXECUTED = "CompensationExecuted"


@register_skill
class RefundCompensateSkill(Skill):
    contract = SkillContract(
        name="refund.compensate",
        version="1.0.0",
        purpose="退款被驳回或走不通后的域内补偿收口：作废退款请求、写补偿记录与人工工单，"
                "把案子推进到 compensated",
        input_schema={
            "tenant_id": "str",
            "case_id": "str",
            "operator": "str（做出驳回/收口决定的人）",
            "reason": "str（为什么走补偿，原样进补偿记录与事件）",
            "assignee": "str（可选，人工工单的接单人，缺省同 operator）",
            "assignee_role": "str（可选，工单承接岗，缺省 roles.DEFAULT_TICKET_ROLE；"
                             "认目录写法与 verdict.py 那套写法两种）",
        },
        output_schema={
            "biz_status": "compensated",
            "revoked": "list[dict]（每笔作废的 refund_request 及其最后观察到的下落）",
            "ticket": "dict（人工工单：单号、接单人、要人去做什么）",
            "records": "int（落进 compensation_record 的行数）",
            "last_observed_state": "str（settled|failed|processing|unknown|unobserved）",
            "invocation_id": "str",
        },
        preconditions=["tenant_id", "case_id", "operator", "reason"],
        depends_tools=[],
        # 不重试：补偿是写账动作，重试一次就多一条记录。失败要人看见，不要自己再来一遍。
        failure_policy="escalate",
        max_retries=0,
        security_boundary=(
            "写 compensation_record 与 biz_status(compensated)；"
            "**不写 settled**（那是 payment.observe 的权威边界，guard 会抛）；"
            "**不宣布外部资金结果** —— 作废记录只表示 MAOS 侧不再推进，"
            "最后一次观察到的下落原样留档交人工对账；"
            "已 settled 的案子拒绝补偿，不静默跳过"
        ),
        reuse_note=(
            "任何「外部已经收到请求、但本地要收口」的域都该照此写：先留档最后一次观察、"
            "再开人工工单、最后才推进本地状态；三步顺序不可换"
        ),
        owner_roles=["refund_payment"],
    )

    def run(self, payload: dict, ctx: SkillContext) -> dict:
        store = C.ensure_schema(ctx)
        invocation_id = C.invocation_id_of(ctx)
        extras = getattr(ctx, "extras", None) or {}
        tenant_id, case_id, operator, reason = C.required(
            payload, "tenant_id", "case_id", "operator", "reason")

        case = guard.get_case(store, tenant_id, case_id)
        if case is None:
            raise LookupError(f"没有这个 case：tenant={tenant_id} case={case_id}")

        # 已确认退款成功的案子不许补偿。这不是防御性编程，是数据一致性的报警器：
        # 走到这里说明有人拿一笔已经成功的退款去做撤销，静默跳过会把错误埋掉。
        if case["biz_status"] == "settled":
            raise ValueError(
                f"case={case_id} 已经是 settled（退款确认成功），不许补偿；"
                "补偿是给走不通的案子收口用的，对成功的案子撤销等于凭空抹掉一笔已发生的资金动作")

        now = C.now_iso()
        last_state = self._last_observed_state(store, tenant_id, case_id)
        requests = objects.query(
            store,
            "SELECT * FROM refund_request WHERE tenant_id=? AND case_id=?"
            " ORDER BY submitted_at", (tenant_id, case_id))

        # ---- 第一步：把每一笔退款请求连同**最后观察到的下落**留档 ----------
        # 顺序不可换：先留档再推状态。状态一旦落 compensated，「外面还有一笔下落
        # 不明的请求」这件事就没人记得了 —— 与 control_plane.human_decision
        # 「先回滚再改状态」同一个理由。
        revoked = []
        for row in requests:
            detail = {
                "request_id": row["request_id"],
                "idempotency_key": row["idempotency_key"],
                "gateway": row["gateway"],
                "amount": row["amount"],
                # 这一行是整条记录的题眼：作废的是**我们这边的推进**，
                # 不是外部那笔钱的结论。unknown / unobserved 时尤其不许改写成 failed。
                "last_observed_state": last_state,
                "meaning": "MAOS 侧不再推进本请求；外部资金下落以 last_observed_state 为准，"
                           "需人工到支付渠道对账后确认",
                "reason": reason,
            }
            self._record(store, tenant_id=tenant_id, case_id=case_id,
                         kind=KIND_REQUEST_REVOKED, detail=detail,
                         executed_at=now, operator=operator)
            revoked.append(detail)

        # ---- 第二步：开人工工单 —— 本系统能做的到此为止 ---------------------
        # T117：工单从「一行记录」补成「一条闭环」。承接岗由角色目录决定
        # （`roles.DEFAULT_TICKET_ROLE`），不在这里写死一个字符串 ——
        # 写死之后换岗要改代码，而换岗是组织的事，不该是一次发版。
        ensure_ticket_schema(store)
        assignee = str(payload.get("assignee") or operator)
        assignee_role = _opening_role(payload, assignee)
        ticket = {
            "ticket_id": ticket_id_of(case_id),
            "assignee": assignee,
            # 承接**岗**。与 assignee 分开两个字段是本轨的题眼：接单的人会换、会休假、
            # 会离职，而「这活归支付运维」不会跟着变。只记人名的工单，人一走就没人认领。
            "assignee_role": assignee_role,
            "assignee_role_title": roles.title_of(assignee_role),
            "case_id": case_id,
            "reason": reason,
            "last_observed_state": last_state,
            "todo": [
                "到支付渠道后台按 idempotency_key 核对这笔退款的真实下落",
                "下落为已退款则补记账；未退款则按人工流程重新发起或改单",
                "对客户回访并关闭本案",
            ],
            "requests": [r["request_id"] for r in requests],
            "opened_at": now,
        }
        ticket_revision = self._record(store, tenant_id=tenant_id, case_id=case_id,
                                       kind=KIND_MANUAL_TICKET, detail=ticket,
                                       executed_at=now, operator=operator)
        # 生命周期列与 detail_json 同一次开单落下。**列是权威，detail_json 是开单快照**：
        # 后续 assign / resolve 只改列，不回头重写快照 —— 一份记录里两处都能改的话，
        # 「工单现在归谁」就有了两个答案，而它们迟早不一致。
        _update_ticket(store, tenant_id=tenant_id, case_id=case_id, executed_at=now,
                       assignee_role=assignee_role, assignee=assignee, opened_at=now)

        # ---- T116 段：DAG -> 业务对象。人工补偿是十类里的最后一类 -------------
        # 挂在工单那一条上（`object_version` 取它的修订号）：一次补偿会落两行
        # （作废请求 + 人工工单），而对外要指的是**人接手的那一件**。
        # object_id 用 case_id 而不是 ticket_id：`compensation_record` 的主键是
        # `(tenant, case, kind, executed_at)`，`ticket_id` 只在 detail_json 里，
        # 拿它当 object_id 会让 `resolve_business_ref` 的 `WHERE case_id=?` 查不着。
        plan_id_ref = str(extras.get("plan_id") or case.get("plan_id") or "")
        task_id_ref = str(extras.get("task_id") or "")
        if plan_id_ref and task_id_ref:
            objects.attach_business_ref(
                store, plan_id=plan_id_ref, task_id=task_id_ref, tenant_id=tenant_id,
                object_type="compensation_record", object_id=case_id,
                object_version=ticket_revision,
                purpose=f"域内补偿收口（工单 {ticket['ticket_id']}，第 {ticket_revision} 次）")

        # ---- 第三步：推进业务状态。guard 是唯一入口，越权写 settled 会被它拦 ----
        case = guard.update_biz_status(
            store, tenant_id, case_id, "compensated",
            self.contract.name, invocation_id,
            reason=f"{operator} 驳回后域内补偿收口：{reason}（最后观察到 {last_state}）")

        store.append_event_log({
            "trace_id": str(extras.get("trace_id") or ""),
            "plan_id": str(extras.get("plan_id") or case.get("plan_id") or ""),
            "task_id": str(extras.get("task_id") or ""),
            "event_type": EVENT_COMPENSATION_EXECUTED,
            "reason": reason,
            "detail": {
                # domain 键区分本条是域内补偿还是控制面的逆补丁补偿 ——
                # 两者共用事件名，审计时按这个键分流。
                "domain": C.BIZ_TYPE,
                "tenant_id": tenant_id,
                "case_id": case_id,
                "operator": operator,
                "revoked_requests": [r["request_id"] for r in revoked],
                "last_observed_state": last_state,
                "ticket_id": ticket["ticket_id"],
                "records": len(revoked) + 1,
                "invocation_id": invocation_id,
            },
        })

        return {
            "biz_status": case["biz_status"],
            "revoked": revoked,
            "ticket": ticket,
            "records": len(revoked) + 1,
            "last_observed_state": last_state,
            "invocation_id": invocation_id,
        }

    # ------------------------------------------------------------------
    @staticmethod
    def _last_observed_state(store, tenant_id: str, case_id: str) -> str:
        """**收口这一笔**最后一次观察到的下落。一次都没观察到返回 UNOBSERVED。

        按当前那一笔 `refund_request` 的 `request_id` 收窄，不按 `(tenant, case)`
        查全表。换渠道重试之后一个案子会先后有两笔请求（场景 7）：前一笔在网关入口
        就被拒（`40005`，`payment.observe` 会照实落一行 `observed_state='failed'`），
        换渠道之后的那一笔才是收口这一笔。按全表取最新会把**上一笔的下落**当成
        这一笔的 —— 而这种错没有症状：补偿记录、人工工单、事件全都正常，只有
        「外面那笔钱到底怎么样了」这一格填错，偏偏那是人工对账唯一的依据。

        刻意不兜底成 "failed"：轮询到顶没问出终态时 `payment.observe` 一行观察都不写
        （它在源码里写明「还没问出来不是一个可以落库的结论」），此时表是空的 ——
        把空表读成「失败」正是本 skill 通篇在防的那个推断。查不到当前请求时同理，
        返回 UNOBSERVED 而不是回退去读别的请求的观察。
        """
        current = objects.query(
            store,
            "SELECT request_id FROM refund_request WHERE tenant_id=? AND case_id=?"
            " ORDER BY submitted_at DESC", (tenant_id, case_id))
        if not current:
            return UNOBSERVED
        rows = objects.query(
            store,
            "SELECT observed_state FROM payment_observation"
            " WHERE tenant_id=? AND case_id=? AND request_id=?"
            " ORDER BY observed_at DESC",
            (tenant_id, case_id, current[0]["request_id"]))
        return str(rows[0]["observed_state"]) if rows else UNOBSERVED

    @staticmethod
    def _record(store, *, tenant_id: str, case_id: str, kind: str, detail: dict,
                executed_at: str, operator: str) -> int:
        """落一条补偿记录，返回它的修订号。

        ---- T116 段：`revision` 列 ----
        修订号**按 kind 各自成链**（作废请求 / 人工工单是两件不同的事，各数各的）。
        一个案子可能补偿两次 —— 第一次工单没解决、人工重新发起又失败 ——
        而这张表主键里带着 `executed_at`，两次各占一行；修订号让「这是第几次
        对本案做这类补偿」查得到，光看时刻戳还得自己排序数一遍。
        """
        revision = case_pack.next_version(store, "compensation_record",
                                          tenant_id=tenant_id, case_id=case_id,
                                          kind=kind)
        objects.execute(
            store,
            "INSERT OR REPLACE INTO compensation_record (tenant_id, case_id, kind,"
            " detail_json, executed_at, operator, revision) VALUES (?,?,?,?,?,?,?)",
            (tenant_id, case_id, kind,
             json.dumps(detail, ensure_ascii=False, sort_keys=True),
             executed_at, operator, revision),
        )
        return revision


# =====================================================================
# T117 · 补偿工单的生命周期 —— 开单 / 派单 / 关单
# =====================================================================
#
# 为什么这一段落在 `compensate.py` 而不是另起一个模块：工单是本 skill 开出来的，
# 它的列义、缺省承接岗、单号规则都只有开单方说得清。派单与关单发生在别处
# （`maos/ingress/outcome_commands.py` 与 `compensation_close.py`），但它们改的
# 是同一行记录 —— 把写入口散到三个模块里，「工单现在是什么状态」就没有一处答得全。
#
# 事件名逐字对齐 p10 跨轨契约 §F。它们是 `event_log.event_type` 的**字符串**，
# 不进 `maos/contracts/events.py`（铁律 1 冻结面），写法同 `RefundBizStatusChanged`。

EVENT_COMPENSATION_ASSIGNED = "CompensationAssigned"
EVENT_COMPENSATION_RESOLVED = "CompensationResolved"

#: 人工处理完之后的两种结论。**这是工单自己的列，不是 `biz_status`**（铁律 9：
#: 业务状态是业务对象自己的字段，不许往状态机里加新状态或新迁移）。
#: 取值刻意与 `payment_observation.observed_state` 同形，但语义是「这张工单以什么
#: 结论关闭」—— 钱到没到账的权威始终在那条观察行上，不在这一列。
RESOLUTION_SETTLED = "settled"
RESOLUTION_NOT_SETTLED = "not_settled"
RESOLUTION_KINDS = (RESOLUTION_SETTLED, RESOLUTION_NOT_SETTLED)

#: 单号前缀。`compensate` 开单、`/assign` 与 `/resolve` 按号找回同一行，
#: 三处都从这里取，不各抄一份 f-string。
TICKET_PREFIX = "MT-"

#: DDL 片段（p10 跨轨契约 §B.1，每轨一个文件）。
_SCHEMA_FRAGMENT = Path(objects.__file__).with_name("schema_p10_t117.sql")

#: 从片段里认出 `ALTER TABLE <表> ADD COLUMN <列> <声明>;`。
#:
#: **为什么要解析而不是在这里再抄一份列清单**：抄两份的代价不是重复六行，是
#: 两份迟早分叉，而分叉的症状是「片段里改了声明，库里的列还是老形状」——
#: 不报错，只在某条 INSERT 撞上约束时才炸，离原因很远。片段是本仓库自己的文件、
#: 不是外来输入，所以一条正则够用，不必上 SQL 解析器。
_ADD_COLUMN = re.compile(
    r"ALTER\s+TABLE\s+(?P<table>\w+)\s+ADD\s+COLUMN\s+(?P<col>\w+)\s+(?P<decl>[^;]+);",
    re.IGNORECASE)


def ticket_columns() -> tuple[tuple[str, str, str], ...]:
    """片段里声明的加列清单，`(表, 列, 声明)` 三元组，按文件里的顺序。"""
    script = _SCHEMA_FRAGMENT.read_text(encoding="utf-8")
    return tuple((m.group("table"), m.group("col"), m.group("decl").strip())
                 for m in _ADD_COLUMN.finditer(script))


def ensure_ticket_schema(store) -> None:
    """保证工单的生命周期列在库上。幂等，可连跑；在首次使用时调用（契约 §B.1）。

    刻意**不**去改 `objects.ensure_schema` / `schema.sql` 主文件：同期 T116 与 T120
    也在往退款域加列，三轨同改一处主文件必冲突。代价是每轨自带一次探针，
    那是并行的成本，不是设计意图。

    「探一遍、加一遍、核一遍」那三步收在 `maos/domain/_schema_util.py` 里，T116 的
    `case_pack.ensure_t116_schema()` 用的是同一个（T133 去重）。

    **这条路径原先在 PG 上是抛的**：私有助手拿 `PRAGMA table_info` 当探针，
    而 `PRAGMA` 是 SQLite 方言 —— `make_case_bundle.py --all-paths
    --domain-backend postgres` 于是只有 happy 跑得出来，凡走到补偿的路径当场
    `SyntaxError: syntax error at or near "PRAGMA"`。**收口时不要退成
    「探不动就 return」**：这条路径上没有调用方的 SELECT 探针补位（T116 那条有），
    退成 return 的实测后果是不抛了、六列一列都没加，症状要等到下游写
    `compensation_record` 时才以 `UndefinedColumn` 冒出来。详见
    `_schema_util` 的模块 docstring 末段。
    """
    _schema_util.apply_columns(_schema_util.open_conn(store), ticket_columns())


def ticket_id_of(case_id: str) -> str:
    return f"{TICKET_PREFIX}{case_id}"


def case_id_of_ticket(ticket_id: str) -> str:
    """单号 -> 案号。认不出前缀就**原样返回**，让后面按案号查不到那一步去报错。

    在这里抛「单号格式不对」是错的：前缀是我们自己定的实现细节，而人在房间里
    打错的更可能是案号本身。让查不到的那一步报「没有这张工单」，报错信息才指得准。
    """
    text = str(ticket_id or "").strip()
    return text[len(TICKET_PREFIX):] if text.startswith(TICKET_PREFIX) else text


def _opening_role(payload: dict, assignee: str) -> str:
    """开单时的承接岗。三档，从具体到缺省：

    1. 调用方显式指了岗 —— 认目录写法与 `verdict.py` 那套写法两种；
    2. 没指，但目录认得接单人 —— 用他所在的岗；
    3. 都不是 —— `roles.DEFAULT_TICKET_ROLE`（支付运维）。

    第 2 档不能省：`compensate` 缺省把工单派给**做出驳回决定的那个人**
    （`test_compensation_failure.py` 钉着这条，理由是「他此刻最清楚上下文」），
    而那个人常常是主管而不是支付运维。把他的岗写成支付运维，角色目录就在开单
    这一步先说了一句假话。
    """
    want = str(payload.get("assignee_role") or "").strip()
    if want:
        return roles.canonical_role(want)
    return roles.role_of(assignee) or roles.DEFAULT_TICKET_ROLE


def _update_ticket(store, *, tenant_id: str, case_id: str, executed_at: str,
                   **fields: str) -> None:
    """改工单那一行的生命周期列。**只认本片段声明过的列名。**

    白名单不是防注入的仪式（列名是本模块的字面量，不是外来输入），是防打错：
    片段里改了列名而这里忘了跟，`UPDATE ... SET <老列名>=?` 会报 no such column，
    报错指向 SQL 而不是指向「两处列清单分叉了」。白名单让它在拼 SQL 之前就响。
    """
    known = {col for _table, col, _decl in ticket_columns()}
    unknown = sorted(set(fields) - known)
    if unknown:
        raise KeyError(f"{unknown} 不是 T117 片段声明过的工单列（已声明：{sorted(known)}）")
    if not fields:
        return
    sets = ", ".join(f"{col}=?" for col in fields)
    objects.execute(
        store,
        f"UPDATE compensation_record SET {sets}"
        " WHERE tenant_id=? AND case_id=? AND kind=? AND executed_at=?",
        (*fields.values(), tenant_id, case_id, KIND_MANUAL_TICKET, executed_at),
    )


def ticket_of(store, tenant_id: str, case_id: str) -> dict | None:
    """取这个案子**最近**开的那张人工工单；没有返回 None。

    按 `executed_at` 倒序取一行：一个案子理论上只补偿一次（`compensated` 是终态），
    但补偿记录的主键含 `executed_at`，同一案子重跑会留下两行。取最新那行，
    与 `_last_observed_state` 「按当前那一笔收窄」是同一个取向 ——
    读到一张早就关掉的旧工单，比读不到工单更难查。
    """
    rows = objects.query(
        store,
        "SELECT * FROM compensation_record WHERE tenant_id=? AND case_id=? AND kind=?"
        " ORDER BY executed_at DESC",
        (tenant_id, case_id, KIND_MANUAL_TICKET))
    return rows[0] if rows else None


def require_ticket(store, tenant_id: str, case_id: str) -> dict:
    """取工单，没有就抛。派单与关单都要先有单，**不许自动补开一张**：
    没有工单意味着这个案子根本没走到补偿，那一步该由 `refund.compensate` 做。"""
    ticket = ticket_of(store, tenant_id, case_id)
    if ticket is None:
        raise LookupError(
            f"case={case_id}（tenant={tenant_id}）没有人工工单；"
            "工单由 refund.compensate 在补偿收口时开出，这里不补开")
    return ticket


def assign_ticket(store, *, tenant_id: str, case_id: str, role: str,
                  assignee: str = "") -> dict:
    """把工单派给一个**岗**。接单人没指名就取该岗目录里的第一个（主责人）。

    派的是岗不是人，理由同开单那一段：人会换，岗不会。指名到人是可选的收窄，
    不是必需 —— 房间里打一句 `/assign MT-xxx payment_ops` 就该能派出去。
    """
    ticket = require_ticket(store, tenant_id, case_id)
    want = roles.canonical_role(role)
    accounts = roles.accounts_of(want)          # 认不出的岗在这里抛 UnknownRole
    who = str(assignee or "").strip() or (accounts[0] if accounts else "")
    if not who:
        raise ValueError(
            f"角色 {want!r} 的账号列表是空的，派不出去；"
            "请在 scenarios/refund/roles.json 里给它登记账号")
    _update_ticket(store, tenant_id=tenant_id, case_id=case_id,
                   executed_at=ticket["executed_at"],
                   assignee_role=want, assignee=who)
    return require_ticket(store, tenant_id, case_id)


def resolve_ticket(store, *, tenant_id: str, case_id: str, resolution_kind: str,
                   observation_id: str, at: str = "") -> dict:
    """关单。**要求带上回填的那条观察**，这是本轨最要紧的一条约束。

    `observation_id` 必填且非空：一张说「已经退了」却指不到任何一条
    `payment_observation` 的工单，就是把外部状态在第二处写死为终态（铁律 8）。
    工单只留一个指针，钱到没到账的权威始终在那条观察行上。

    已经关过的单不许再关一次：两次关单会静默盖掉第一条回填的观察，而那条观察
    正是「人工处理完之后钱到底退没退」的唯一依据。要改结论请由人重开一张单。
    """
    ticket = require_ticket(store, tenant_id, case_id)
    if str(ticket.get("resolved_at") or ""):
        raise ValueError(
            f"工单 {ticket_id_of(case_id)} 已于 {ticket['resolved_at']} 关闭"
            f"（结论 {ticket.get('resolution_kind')!r}，观察 "
            f"{ticket.get('resolution_observation_id')!r}）；"
            "重复关单会盖掉已回填的观察，要改结论请重开一张工单")
    if resolution_kind not in RESOLUTION_KINDS:
        raise ValueError(f"关单结论只能是 {list(RESOLUTION_KINDS)}，实际 {resolution_kind!r}")
    if not str(observation_id or "").strip():
        raise ValueError(
            "关单必须带上回填的 payment_observation 引用："
            "指不到观察的结论就是替外部系统宣布终态（铁律 8）")
    _update_ticket(store, tenant_id=tenant_id, case_id=case_id,
                   executed_at=ticket["executed_at"],
                   resolved_at=at or C.now_iso(),
                   resolution_kind=resolution_kind,
                   resolution_observation_id=str(observation_id))
    return require_ticket(store, tenant_id, case_id)
