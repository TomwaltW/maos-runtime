"""权威事实守卫 —— 采购退货域有**两个**外部权威源（铁律 8）。

题眼：**MAOS 不持有权威事实**。本域要守的是两件互相独立的事：

    「供应商认不认这笔退货」 —— 权威在**供应商**，凭据是它开出的贷项通知单
    「钱到没到账」           —— 权威在 **AP / 银行**，凭据是调整凭单的核销回执

这是本域相对 `ap` / `refund` 域的**增量**：那两个域各只有一个权威终态（`settled`），
本域有两个（`credited` 与 `settled`），且判据不同、落表不同、来源系统不同。
两个都只有 `rtv.observe` 这一个 skill 写得进去，而且必须同事务附上它读到的那份凭据 ——
没有凭据的 `credited` / `settled` 就是「把外部状态直接写死为终态」，那是 bug 不是功能。

越权写入**不静默失败**：抛 `AuthoritativeFactViolation` + 落一条事件。
「系统拒绝了一次越权写入」本身就是要拿给评委看的证据，吞掉就没了。

`rtv_case` 的一切写入只有两个入口 —— `create_case()` 建、`update_biz_status()` 改，
不留第三条路径。两道拦截：
  - 运行时：`objects.execute()` 见到 rtv_case 的写语句直接抛 `BypassedGuardError`
  - 提交前：`maos/tests/test_rtv_guard.py::test_no_source_file_writes_authoritative_states`
    扫全仓源码

## 比 ap 域多守两张表

`ap` 域的 `objects.execute()` 对 `ap_payment_observation` 不设限，靠「guard 自己不走
execute」维持（见那边 `record_observation` 的 docstring 原文：「这里不能走
objects.execute 图省事 —— 那条路对 ap_payment_observation 不设限，等于给伪造回单
留了个后门」）。本域把这条路一并堵上：`credit_note` 与 `rtv_settlement_observation`
都在 `objects._GUARDED_TABLES` 里，运行时旁路写不进去。承载权威事实的表不该有
第二条写入路径。

## 一个终态一份判据，且**同增同减**

`AUTHORITATIVE_STATES` 与 `AUTHORITATIVE_RECEIPT_STATE` 必须逐键对齐。加一个权威
终态却忘了给判据，第 ④ 道拿不到判据时 **fail-closed**（拒，不放行）—— 默认放行会让
那个终态退回到「有凭据就算数」，静默且没人会发现。

🔴 **`acknowledged` 绝不许进 `credited` 的判据集合**：那是「供应商收到退货了」，
不是「供应商认了这笔钱」。两者在回执里字段齐全、形状一样，但差着一次会计确认 ——
口径同 ap 域拒收 `accepted`（银行受理了指令 ≠ 钱划走了）那条注释。

## 与 `ap` / `refund` 两域的关系：同名终态，互不影响

三个域**都有一个叫 `settled` 的终态**。这不是冲突，因为：

  · 三个模块各是各的，**互不 import**，也没有共同基类；
  · 各守各的表：本模块只写 `rtv_case` / `credit_note` / `rtv_settlement_observation`，
    表名与另外两域一个都不重（见 `schema.sql` 抬头）；
  · 写入方不同：本域是 `rtv.observe`，ap 域是 `ap.observe`，退款域是 `payment.observe`。
    把 `ap.observe` 递给本模块，第 ① 道会当场拒 —— 它不是本域的权威写入方。

## 业务状态不进 Task 状态机（铁律 9）

`received` / `disposed` / `shipped` / `credited` / `settled` / `rejected` /
`compensated` 全是 `rtv_case` 自己的字段。`maos/contracts/states.py` 一个新状态、
一条新迁移都没加。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import objects

# ---------------------------------------------------------------- 冻结常量
#: 全系统唯一写得进 `AUTHORITATIVE_STATES` 的 actor。
#: 值是 skill 的 `contract.name` —— skill 把自己的名字递进来，不是自报家门的字符串
#: 常量各写一份（各写一份就会漂，而漂的症状是守卫悄悄放行了别人）。
AUTHORITATIVE_WRITER = "rtv.observe"

#: 🔴 **两个**权威终态，这是本域相对 ap / refund 域的增量。
#: 「供应商认了这笔钱」与「钱到账了」是两件独立的事，由两个不同的外部系统说了算。
AUTHORITATIVE_STATES = frozenset({"credited", "settled"})

#: 权威终态 -> 该终态要求回执里的 `observed_state` 取值。
#: 与 AUTHORITATIVE_STATES **同增同减**：加一个权威终态就必须在这里给出它的判据，
#: 漏配不放行（见到没有判据的权威终态直接拒），否则「有回执」会被当成
#: 「回执说供应商认了 / 钱到了」—— 那正是这张表要堵的洞。
#:
#: 🔴 `acknowledged` 绝不许进 `credited`：那是「供应商收到退货了」，
#: 不是「供应商认了这笔钱」，差着一次会计确认（见模块 docstring）。
AUTHORITATIVE_RECEIPT_STATE: dict[str, frozenset[str]] = {
    "credited": frozenset({"issued"}),      # 供应商**开出**贷项通知单
    "settled":  frozenset({"settled"}),     # AP 调整凭单**核销**/钱到账
}

#: 权威终态 -> 凭据落哪张表。两个终态的凭据形态不同，落表也不同：
#: 贷项通知单是**供应商开的一张单据**（有单号、有金额、有开出时间），
#: 到账回执是**AP 侧的一次观察**（按 seq 累积，同一个案子会观察很多次）。
#: 合并成一张表会让「供应商开了几张贷项通知单」与「我方轮询了几次」混在一起。
_RECEIPT_TABLE: dict[str, str] = {
    "credited": "credit_note",
    "settled":  "rtv_settlement_observation",
}

#: 权威终态 -> 该终态的凭据至少要有的字段。缺任何一个都算「没有凭据」。
#:
#: 除了 `observed_state`（第 ④ 道要看的判据）之外，各带一个**可对账的凭据号**
#: 与一个**可对账的量**，理由同 ap 域「比退款域多一条」那段：
#:
#:     「供应商说认了」        —— 是一句话
#:     「供应商给了单号和金额」—— 是一张可以拿去对账的凭据
#:
#: `credited` 要 `amount_credited`：本域的核心比对就是「我方自称应退 ×
#: 供应商认的金额」，金额缺失时那次比对根本做不成，而 `credited` 的全部含义
#: 正是「供应商认了**多少钱**」。
#: `settled` 要 `ap_reference`：口径同 ap 域的 `bank_reference` ——
#: 没有可对账凭据号的「已到账」在财务上是对不了账的。
_OBSERVATION_REQUIRED: dict[str, tuple[str, ...]] = {
    "credited": ("credit_note_id", "observed_state", "amount_credited"),
    "settled":  ("adjustment_id", "observed_state", "ap_reference"),
}

#: 业务状态机（**不是** Task 状态机，铁律 9）：主干五段 + 三个分支。
#: 逐键照抄 `review/rtv-contracts.md` 的 C-R2，一个键一条边都不许改。
#:
#: 主干（SOP 五步）：received -> disposed -> shipped -> credited -> settled
#:   ① 定位源 PO/收货单，建案                -> received     MAOS（观察）
#:   ② 裁定 credit/exchange/replacement       -> disposed     MAOS（推断）
#:   ③ 登记承运商回执                          -> shipped      **承运商**
#:   ④ 退货行 × 贷项通知单 × 到账三方对账      -> credited     **供应商**
#:   ⑤ AP 建调整凭单，轮询终态                 -> settled      **AP / 银行**
#:
#: `received -> rejected` 是受理即驳（退货诉求本身不成立）；
#: `disposed -> rejected` 是裁定不通过；
#: `shipped -> compensated` 是货发出去了、供应商不认也问不出来，补偿做完之后的收口；
#: `credited -> compensated` 是供应商认了但钱一直不到账，同样走补偿收口。
#: 分支都保留 —— 「这笔为什么没退成」在状态机上要分辨得出来。
BIZ_STATUS_FLOW: dict[str, tuple[str, ...]] = {
    "received":    ("disposed", "rejected"),
    "disposed":    ("shipped", "rejected", "compensated"),
    "shipped":     ("credited", "compensated"),
    "credited":    ("settled", "compensated"),
    "settled":     (),
    "rejected":    (),
    "compensated": (),
}

INITIAL_STATUS = "received"

#: 处置类型三选一。取值域与 `schema.sql` 里 `rtv_case.return_action` 的 CHECK
#: 约束同源（PeopleSoft / Dynamics 365 的三种 return action），
#: 建案时是空串 —— 受理的人不该替裁定的人拍板。
RETURN_ACTIONS = frozenset({"credit", "exchange", "replacement"})

#: 与 ap / refund 域共用同一个事件类型名。对审计与 Trace 来说「有人试图越权写权威
#: 终态」是同一件事，按 `detail.domain` 区分是哪个域 —— 口径同 `CompensationExecuted`。
#: 另起一个名字会让「这个 Plan 有没有越权写入」要查三处，漏一处就是假绿。
VIOLATION_EVENT = "AuthoritativeFactViolation"

#: 本域在事件 detail 里的域标记。
DOMAIN = "rtv"

#: 业务状态推进事件。
BIZ_STATUS_EVENT = "RtvBizStatusChanged"

#: 同一个案号上来了一份业务字段不一样的受理 —— 落这个事件类型。
#: 与 `VIOLATION_EVENT` 分开：越权写入是「你不该写」，这里是「你写的和库里那份
#: 不是同一件事」，两种排查方向完全不同。
CASE_CONFLICT_EVENT = "RtvCaseIdentityConflict"

#: 判定「这是不是同一件事的重放」要逐字段比对的业务字段。
#:
#: `biz_status` / `return_action` / `created_at` **不在里面**：前两者是案子建成之后
#: 被推进 / 裁定的结果，后者是第一次受理的时刻 —— 拿它们比对会让每一次正常重放
#: 都判成冲突。`amount_claimed` 在里面：它是三方对账拿去比对的输入，
#: 悄悄换掉等于把对账绕过去。
_CASE_IDENTITY_FIELDS = ("supplier_id", "po_id", "po_version", "gr_id",
                         "amount_claimed", "currency", "plan_id")


class AuthoritativeFactViolation(RuntimeError):
    """非权威写入方试图写入权威终态，或权威终态没有凭据兜底。

    定义在本模块而不是 `contracts/` —— contracts 是冻结面，且这是采购退货域自己的
    业务规则，不是内核契约。**也不复用 ap / refund 域那两个同名类**：几个域的守卫
    互不 import 是本域可移植性论证的一部分（见模块 docstring）。
    """


class BizStatusTransitionError(ValueError):
    """业务状态迁移不在 `BIZ_STATUS_FLOW` 里。"""


class CaseIdentityConflict(ValueError):
    """同一个 `(tenant_id, case_id)` 上来了一份**业务字段不一样**的受理。

    不是重放，是两件事撞了同一个案号。定义成 `ValueError` 而不是复用
    `AuthoritativeFactViolation`：那个说的是「你没资格写」，这个说的是
    「你写的和库里那份不是同一件事」。
    """


class DispositionRequired(ValueError):
    """推进到 `disposed` 却没给出裁定结果。

    「裁定完成」与「裁不出结果」是矛盾状态：`disposed` 的全部含义就是
    「已经裁定成 credit / exchange / replacement 之一」。允许空串进来的话，
    `return_action` 这一列就会有一批永远填不上的行，而下游（发运、对账）
    要靠它决定退回来的是货还是钱。
    """


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_invocation_id(invocation_id: str) -> str:
    """actor 溯源的唯一锚点，空了这条审计链就断了。"""
    if not invocation_id:
        raise ValueError("invocation_id 不许为空：它是 rtv_case 每一次写入的 actor 锚点")
    return invocation_id


def _identity_of(row: dict) -> dict:
    """把库里那一行折成与 `create_case` 入参同一个形状，好逐字段比。

    必须过一遍类型归一：sqlite 的 INTEGER 回来是 int，而调用方递进来的可能是 str；
    金额一律过 `objects.money_str` 折成两位小数字符串 —— 不归一就会把
    「3200 与 3200.00」判成冲突，幂等当场退化成「每次重跑都报冲突」。
    """
    return {
        "supplier_id":    str(row["supplier_id"]),
        "po_id":          str(row["po_id"]),
        "po_version":     int(row["po_version"]),
        "gr_id":          str(row["gr_id"]),
        "amount_claimed": objects.money_str(row["amount_claimed"]),
        "currency":       str(row["currency"]),
        "plan_id":        str(row["plan_id"]),
    }


def _log_case_conflict(store: Any, *, plan_id: str, tenant_id: str, case_id: str,
                       diff: dict, actor: str, invocation_id: str) -> None:
    """拒绝一次案号复用也要留证据 —— 理由同模块 docstring：吞掉就没了。"""
    store.append_event_log({
        "plan_id": plan_id,
        "event_type": CASE_CONFLICT_EVENT,
        "reason": f"case_id 被复用，业务字段对不上：{sorted(diff)}",
        "detail": {"domain": DOMAIN, "tenant_id": tenant_id, "case_id": case_id,
                   "actor": actor, "invocation_id": invocation_id,
                   "conflicts": {f: {"stored": old, "incoming": new}
                                 for f, (old, new) in diff.items()}},
    })


def _log_violation(store: Any, *, plan_id: str, tenant_id: str, case_id: str,
                   attempted: str, actor: str, invocation_id: str, why: str) -> None:
    store.append_event_log({
        "plan_id": plan_id,
        "event_type": VIOLATION_EVENT,
        "reason": why,
        "detail": {"domain": DOMAIN, "tenant_id": tenant_id, "case_id": case_id,
                   "attempted": attempted, "actor": actor,
                   "invocation_id": invocation_id,
                   "authoritative_writer": AUTHORITATIVE_WRITER},
    })


def create_case(
    store: Any,
    *,
    tenant_id: str,
    case_id: str,
    supplier_id: str,
    po_id: str,
    po_version: int,
    gr_id: str,
    amount_claimed: Any,
    plan_id: str,
    actor_skill: str,
    invocation_id: str,
    currency: str = "CNY",
) -> dict:
    """建一个 rtv_case，落 `received`。这是本表唯一的插入口径，**且是幂等的**。

    **`biz_status` 与 `return_action` 都不接受调用方指定** —— 想直接建成 `credited`
    或 `settled` 的路必须从一开始就不存在，否则守卫只挡得住 update，挡不住 insert；
    而 `return_action` 由 `rtv.dispose` 裁定后写入，受理的人不该替裁定的人拍板
    （`schema.sql` 里那条注释同一个意思）。

    **幂等语义**（受理这一步会被返工重跑，而主键是 `(tenant_id, case_id)`）：

      · 案号已在库 + `_CASE_IDENTITY_FIELDS` 逐字段相同 → 一个字节都不写，返回
        **既有那一行**（原 `created_at`、原 `biz_status`、原 `return_action`）。
        所以这里既不能 `INSERT OR REPLACE` 也不能 `ON CONFLICT DO UPDATE`：
        那两种写法会让一次重跑把已经推进到 shipped / credited 的案子**静悄悄**
        倒回 `received`，比裸 INSERT 抛异常坏得多。
      · 案号已在库 + 任一业务字段不同 → 落一条 `CASE_CONFLICT_EVENT` 事件并抛
        `CaseIdentityConflict`。**这一档不许静默**：悄悄收下新金额，则库里的
        `amount_claimed` 是三方对账真正拿去比对的输入，等于把对账绕过去；
        悄悄丢弃，则调用方拿到一份和自己递进来的退货诉求对不上的案子，
        同样一点信号都没有。

    用 `ON CONFLICT (tenant_id, case_id) DO NOTHING` 而不是 `INSERT OR IGNORE`：
    后者会把 `biz_status` 那条 CHECK 约束的失败一并吞掉，指名冲突目标才只放过
    主键这一种冲突。判定放在插入**之后**回读比对，而不是插入前先查一次 ——
    先查后插在 `lock_of()` 退化成 nullcontext 的 Store 上有 TOCTOU 窗口。
    """
    _require_invocation_id(invocation_id)
    incoming = {
        "supplier_id":    str(supplier_id),
        "po_id":          str(po_id),
        "po_version":     int(po_version),
        "gr_id":          str(gr_id),
        "amount_claimed": objects.money_str(amount_claimed),
        "currency":       str(currency),
        "plan_id":        str(plan_id),
    }
    conn = objects._conn(store)
    with objects.lock_of(store):
        conn.execute(
            "INSERT INTO rtv_case (tenant_id, case_id, supplier_id, po_id, po_version,"
            " gr_id, return_action, amount_claimed, currency, biz_status, plan_id,"
            " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT (tenant_id, case_id) DO NOTHING",
            (tenant_id, case_id, incoming["supplier_id"], incoming["po_id"],
             incoming["po_version"], incoming["gr_id"], "",
             incoming["amount_claimed"], incoming["currency"], INITIAL_STATUS,
             incoming["plan_id"], _now()),
        )
        conn.commit()

    case = get_case(store, tenant_id, case_id)
    if case is None:
        # 既没插进去、又读不到。不静默返回 None：调用方的类型标注说这里必有一行。
        raise RuntimeError(
            f"create_case 之后读不到 case：tenant={tenant_id} case={case_id}")

    stored = _identity_of(case)
    diff = {f: (stored[f], incoming[f])
            for f in _CASE_IDENTITY_FIELDS if stored[f] != incoming[f]}
    if diff:
        _log_case_conflict(store, plan_id=incoming["plan_id"], tenant_id=tenant_id,
                           case_id=case_id, diff=diff, actor=actor_skill,
                           invocation_id=invocation_id)
        detail = "；".join(f"{f}：库里 {old!r}、这次 {new!r}"
                          for f, (old, new) in sorted(diff.items()))
        raise CaseIdentityConflict(
            f"case={case_id}（tenant={tenant_id}）已经存在，业务字段对不上：{detail}。"
            "受理重跑只在业务字段逐字段相同时幂等；对不上说明这不是同一笔退货的重放，"
            "既不许覆盖也不许静默丢弃 —— 换个 case_id，或先查清这个案号为什么被复用"
        )
    return case


def update_biz_status(
    store: Any,
    tenant_id: str,
    case_id: str,
    new_status: str,
    actor_skill: str,
    invocation_id: str,
    *,
    observation: dict | None = None,
    return_action: str = "",
    reason: str = "",
) -> dict:
    """`rtv_case.biz_status` 的唯一写入路径。

    四道闸，顺序不可换（每一道旁边写了为什么它必须在这个位置）：

    ① `new_status` 落在 `AUTHORITATIVE_STATES` 而 actor 不是权威写入方 → 拒 + 留证据
    ② 递了 `observation` 却不是权威写入方 → 拒（否则等于给别人开伪造凭据的口子）
    ③ 权威终态**必须**带凭据，且字段齐全（含可对账的凭据号与金额）
    ④ 凭据说的**得是这件事**：`observed_state` 必须落在该终态的判据集合里

    `return_action` 只在推进到 `disposed` 时接受，且必须给 —— 见 `DispositionRequired`。
    它与状态更新**同事务**：裁定结果与「已裁定」这个状态是同一件事的两面，
    分两次写会出现「状态是 disposed 但裁定结果是空串」的中间态。
    """
    _require_invocation_id(invocation_id)
    case = get_case(store, tenant_id, case_id)
    plan_id = (case or {}).get("plan_id", "")

    # ① 权威闸放在最前面：case 不存在也照样记一笔越权尝试。
    #    先查存在性会让「对不存在的 case 越权写 credited」以 LookupError 收场，
    #    证据就没了 —— 而那恰恰是最该留痕的一种试探。
    if new_status in AUTHORITATIVE_STATES and actor_skill != AUTHORITATIVE_WRITER:
        _log_violation(store, plan_id=plan_id, tenant_id=tenant_id, case_id=case_id,
                       attempted=new_status, actor=actor_skill, invocation_id=invocation_id,
                       why=f"{new_status} 只能由 {AUTHORITATIVE_WRITER} 写入")
        raise AuthoritativeFactViolation(
            f"{actor_skill} 试图把 case={case_id} 写成 {new_status}；"
            f"该状态的权威在外部（credited 在供应商、settled 在 AP/银行），"
            f"只有 {AUTHORITATIVE_WRITER} 观察到凭据后才写得进来"
        )

    # ② 凭据只有权威写入方递得进来，否则等于给别人开了个伪造凭据的口子。
    if observation is not None and actor_skill != AUTHORITATIVE_WRITER:
        _log_violation(store, plan_id=plan_id, tenant_id=tenant_id, case_id=case_id,
                       attempted=new_status, actor=actor_skill, invocation_id=invocation_id,
                       why=f"外部凭据只能由 {AUTHORITATIVE_WRITER} 提交")
        raise AuthoritativeFactViolation(
            f"{actor_skill} 递交了外部凭据；贷项通知单与到账回执都是外部权威事实，"
            f"只有 {AUTHORITATIVE_WRITER} 能落库"
        )

    if case is None:
        raise LookupError(f"没有这个 case：tenant={tenant_id} case={case_id}")

    cur = case["biz_status"]
    if new_status not in BIZ_STATUS_FLOW.get(cur, ()):
        raise BizStatusTransitionError(
            f"业务状态不许从 {cur} 迁到 {new_status}（case={case_id}）；"
            f"{cur} 的合法去向：{BIZ_STATUS_FLOW.get(cur, ()) or '无（终态）'}"
        )

    # 裁定结果：进 disposed 必须带，且只在进 disposed 时接受。
    if new_status == "disposed":
        if not return_action:
            raise DispositionRequired(
                f"推进到 disposed 必须同时给出 return_action（{sorted(RETURN_ACTIONS)} 之一）；"
                f"「已裁定」而裁不出结果是矛盾状态，下游要靠它决定退回来的是货还是钱"
            )
        if return_action not in RETURN_ACTIONS:
            raise ValueError(
                f"return_action={return_action!r} 不在取值域 {sorted(RETURN_ACTIONS)} 里")
    elif return_action:
        raise ValueError(
            f"return_action 只在推进到 disposed 时接受，本次目标状态是 {new_status!r}；"
            f"裁定结果与「已裁定」这个状态是同一件事的两面，不许分开写"
        )

    # ③ 权威终态必须有凭据，且字段齐全。没有凭据的 credited / settled 就是
    #    把外部状态写死为终态。
    if new_status in AUTHORITATIVE_STATES:
        required = _OBSERVATION_REQUIRED.get(new_status)
        if required is None:
            # 加了权威终态却没登记必填字段，同 ④ 的 fail-closed 理由。
            _log_violation(store, plan_id=plan_id, tenant_id=tenant_id, case_id=case_id,
                           attempted=new_status, actor=actor_skill,
                           invocation_id=invocation_id,
                           why=f"{new_status} 没有在 _OBSERVATION_REQUIRED 里配必填字段")
            raise AuthoritativeFactViolation(
                f"{new_status} 在 AUTHORITATIVE_STATES 里，却没有登记凭据的必填字段；"
                f"三张表（状态集合 / 必填字段 / 判据）必须同增同减"
            )
        missing = [f for f in required if not (observation or {}).get(f)]
        if missing:
            _log_violation(store, plan_id=plan_id, tenant_id=tenant_id, case_id=case_id,
                           attempted=new_status, actor=actor_skill,
                           invocation_id=invocation_id,
                           why=f"{new_status} 的外部凭据缺字段 {missing}")
            raise AuthoritativeFactViolation(
                f"写 {new_status} 必须同事务附外部凭据，缺字段：{missing}。"
                f"凭据号与金额是**可对账的**那部分 —— 「有一张回执」不等于"
                f"「有一张能拿去对账的回执」"
            )

        # ④ 凭据还得**说的是这件事**。③ 只保证「有一张凭据」，不保证那张凭据说供应商
        #    认了钱 —— 一条 observed_state='acknowledged' 的回执字段齐全，在 ③ 眼里与
        #    终态凭据无从分辨。放过它，系统持有的就只是「供应商收到退货了」，不是
        #    「供应商认了这笔钱」，而后者才是 credited 这个词的全部含义（铁律 8）。
        #    防线必须在守卫里，不能只活在某个 skill 的 `if status == "issued"` 分支里：
        #    那种防线改一次分支顺序两层都不会响。
        allowed = AUTHORITATIVE_RECEIPT_STATE.get(new_status)
        if allowed is None:
            # 加了权威终态却没给判据。fail-closed：宁可写不进去，也不许默认放行 ——
            # 默认放行会让这个终态退回到「有凭据就算数」，静默且没人会发现。
            _log_violation(store, plan_id=plan_id, tenant_id=tenant_id, case_id=case_id,
                           attempted=new_status, actor=actor_skill,
                           invocation_id=invocation_id,
                           why=f"{new_status} 没有在 AUTHORITATIVE_RECEIPT_STATE 里配判据")
            raise AuthoritativeFactViolation(
                f"{new_status} 在 AUTHORITATIVE_STATES 里，却没有在 "
                f"AUTHORITATIVE_RECEIPT_STATE 里给出回执判据；两张表必须同增同减"
            )
        seen = str((observation or {}).get("observed_state"))
        if seen not in allowed:
            _log_violation(store, plan_id=plan_id, tenant_id=tenant_id, case_id=case_id,
                           attempted=new_status, actor=actor_skill,
                           invocation_id=invocation_id,
                           why=f"回执 observed_state={seen!r}，不在 {new_status} 的判据 "
                               f"{sorted(allowed)} 里")
            raise AuthoritativeFactViolation(
                f"写 {new_status} 的回执说的是 {seen!r}，不是 {sorted(allowed)}；"
                f"「有一张回执」不等于「外部认了这件事」，外部权威没这么说就不许收口"
            )

    conn = objects._conn(store)
    with objects.lock_of(store):
        try:
            if observation is not None:
                _insert_receipt(conn, new_status, tenant_id=tenant_id, case_id=case_id,
                                observation=observation, actor_skill=actor_skill,
                                invocation_id=invocation_id)
            if new_status == "disposed":
                conn.execute(
                    "UPDATE rtv_case SET biz_status=?, return_action=?"
                    " WHERE tenant_id=? AND case_id=?",
                    (new_status, return_action, tenant_id, case_id),
                )
            else:
                conn.execute(
                    "UPDATE rtv_case SET biz_status=? WHERE tenant_id=? AND case_id=?",
                    (new_status, tenant_id, case_id),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    store.append_event_log({
        "plan_id": plan_id,
        "event_type": BIZ_STATUS_EVENT,
        "from_state": cur, "to_state": new_status, "reason": reason,
        "detail": {"domain": DOMAIN, "tenant_id": tenant_id, "case_id": case_id,
                   "actor": actor_skill, "invocation_id": invocation_id,
                   "return_action": return_action,
                   "observation_attached": observation is not None},
    })
    return get_case(store, tenant_id, case_id)  # type: ignore[return-value]


def _insert_receipt(conn: Any, new_status: str, *, tenant_id: str, case_id: str,
                    observation: dict, actor_skill: str, invocation_id: str) -> None:
    """把外部凭据落进它自己那张表 —— **与状态更新同一个事务**。

    只在 `update_biz_status` 内部调用，且调用点已经在 `lock_of(store)` 里。
    两个权威终态的凭据形态不同，所以按 `_RECEIPT_TABLE` 分流：
    `credited` 落 `credit_note`（一张单据），`settled` 落
    `rtv_settlement_observation`（一次观察，按 seq 累积）。

    非权威状态递了 observation 的话，前面第 ② 道已经把非权威写入方挡掉了，
    但权威写入方在非权威状态上递 observation 仍然会走到这里 —— 那种情况
    没有对应的落表，直接拒：凭据是权威终态的判据，挂在别的状态上没有意义。
    """
    table = _RECEIPT_TABLE.get(new_status)
    if table is None:
        raise AuthoritativeFactViolation(
            f"状态 {new_status!r} 不是权威终态，不接受外部凭据；"
            f"凭据只在 {sorted(_RECEIPT_TABLE)} 上有意义"
        )
    obs = dict(observation)
    if table == "credit_note":
        conn.execute(
            "INSERT INTO credit_note (tenant_id, case_id, credit_note_id, document_type,"
            " amount_credited, currency, issued_at, observed_at, observed_by,"
            " invocation_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (tenant_id, case_id, obs["credit_note_id"], obs.get("document_type", "381"),
             objects.money_str(obs["amount_credited"]), obs.get("currency", "CNY"),
             obs.get("issued_at", ""), obs.get("observed_at") or _now(),
             actor_skill, invocation_id),
        )
        return
    conn.execute(
        "INSERT INTO rtv_settlement_observation (tenant_id, case_id, seq, adjustment_id,"
        " observed_state, ap_reference, observed_at, observed_by, invocation_id)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (tenant_id, case_id, _next_seq(conn, tenant_id, case_id), obs["adjustment_id"],
         obs["observed_state"], obs.get("ap_reference", ""),
         obs.get("observed_at") or _now(), actor_skill, invocation_id),
    )


def _next_seq(conn: Any, tenant_id: str, case_id: str) -> int:
    """`rtv_settlement_observation.seq` 的下一个号。

    在**同一条连接、同一把锁**里算，紧接着就 INSERT —— 分开算会有 TOCTOU 窗口，
    而主键冲突的症状是「轮询到第二次就报 UNIQUE constraint」。
    """
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM rtv_settlement_observation"
        " WHERE tenant_id=? AND case_id=?", (tenant_id, case_id)).fetchone()
    # 用位置下标而不是列名：`sqlite3.Row` 与裸元组都吃 `row[0]`，
    # 换 row_factory 时这一句不会跟着炸。
    return int(row[0])


def record_observation(
    store: Any,
    *,
    tenant_id: str,
    case_id: str,
    observed_state: str,
    invocation_id: str,
    actor_skill: str,
    adjustment_id: str = "",
    ap_reference: str = "",
) -> None:
    """落一条**非终态**（或明确失败）的到账观察，不推进业务状态。

    为什么要有这一条：AP 明确拒绝调整凭单（`failed`）、或者轮询到 `pending` 时，
    观察必须留痕，否则「AP 说这笔没成」这件事只活在日志里。但那一刻**没有合法的
    目标状态可迁** —— 走到 `compensated` 意味着补偿已经做完，而补偿是失败路径的事，
    在这里替它宣布收口就是又一次把状态写死。

    权威写入方之外的 actor 一律拒：凭据是外部权威事实，同 `update_biz_status` 第 ②
    道。**这里不走 `objects.execute`**：那条路对本域两张权威表是拒绝的
    （`objects._GUARDED_TABLES`），守卫自己直连底层连接拿事务，不是拿豁免权。

    落表固定是 `rtv_settlement_observation`。供应商侧的非终态事实（比如「收到退货了」）
    **刻意没有落表路径**：C-R1 给供应商侧的表只有 `credit_note`，而写一行没有真实
    单号的贷项通知单等于自己给自己开票。那类事实属于承运商回执
    （`rtv_shipment.carrier_status`）或对账结论（`rtv_reconciliation.findings_json`）。
    """
    _require_invocation_id(invocation_id)
    if actor_skill != AUTHORITATIVE_WRITER:
        case = get_case(store, tenant_id, case_id)
        _log_violation(store, plan_id=(case or {}).get("plan_id", ""), tenant_id=tenant_id,
                       case_id=case_id, attempted=f"observation:{observed_state}",
                       actor=actor_skill, invocation_id=invocation_id,
                       why=f"外部凭据只能由 {AUTHORITATIVE_WRITER} 落库")
        raise AuthoritativeFactViolation(
            f"{actor_skill} 试图落一条外部回执；只有 {AUTHORITATIVE_WRITER} 能落库"
        )
    # 挡住「用这条旁路落一条终态观察、再让别人读它当成已收口」这条路。
    # 判据值取**所有**权威终态的并集：本域有两个终态，只挡 settled 会把
    # credited 的判据 'issued' 漏在外面。权威终态的观察必须与状态更新同事务，
    # 走 update_biz_status。
    terminal_values = {v for values in AUTHORITATIVE_RECEIPT_STATE.values()
                       for v in values}
    if observed_state in terminal_values:
        raise AuthoritativeFactViolation(
            f"observed_state={observed_state!r} 是权威终态的判据值，"
            f"必须经 update_biz_status 与状态更新同事务写入，不许从这条旁路单独落"
        )
    conn = objects._conn(store)
    with objects.lock_of(store):
        conn.execute(
            "INSERT INTO rtv_settlement_observation (tenant_id, case_id, seq,"
            " adjustment_id, observed_state, ap_reference, observed_at, observed_by,"
            " invocation_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (tenant_id, case_id, _next_seq(conn, tenant_id, case_id), adjustment_id,
             observed_state, ap_reference, _now(), actor_skill, invocation_id),
        )
        conn.commit()


#: 读取口径都在 `objects.py`（守的是写入，读不设限）。这里给两个别名，
#: 让本域的调用面与 `ap` / `refund` 两域一致 —— 那两个域的 `get_case` /
#: `observations_of` 就挂在 guard 上，换域的人不必记「这个域读函数在哪个模块」。
get_case = objects.get_case
observations_of = objects.observations_of
get_credit_note = objects.get_credit_note
