"""settled guard —— 应付账款的权威事实边界（铁律 8）。

题眼：**MAOS 不持有权威事实**。一笔货款到底有没有从公司账户划出去，权威在**银行**，
不在我们库里。所以 `settled` 这一个终态，全系统只有 `ap.observe` 这一个 skill 写得
进去，而且必须同事务附上它读到的那份银行回单（`ap_payment_observation`）——
没有回单的 settled 就是「把外部状态直接写死为终态」，那是 bug 不是功能。

越权写入**不静默失败**：抛 `AuthoritativeFactViolation` + 落一条事件。
「系统拒绝了一次越权写入」本身就是要拿给评委看的证据，吞掉就没了。

`ap_case` 的一切写入只有两个入口 —— `create_case()` 建、`update_biz_status()` 改，
不留第三条路径。两道拦截：
  - 运行时：`objects.execute()` 见到 ap_case 的写语句直接抛 `BypassedGuardError`
  - 提交前：`maos/tests/test_ap_guard.py::test_no_bypass_writes_settled` 扫全仓源码

## 比退款域多一条：settled 必须带银行流水号

退款域的 `payment_observation` 只要求 `request_id` / `gateway_code` /
`observed_state` 三个字段齐全。本域多要一个 **`bank_reference`（银行流水号）**，
理由是应付账款这一侧的外部凭据形态不同：

    「银行回了一个 settled」  —— 是一句话
    「银行给了一个流水号」    —— 是一张可以拿去对账的凭据

没有流水号的「已付」在财务上是对不了账的。守卫要的不是「有一张回单」，是
「有一张**能拿去对账**的回单」。这一条与第 ④ 道（回单说的得是这件事）是两件事：
④ 管内容对不对，这一条管凭据全不全。

## 与退款域 `maos/domain/refund/guard.py` 的关系：同名终态，互不影响

两个域**都有一个叫 `settled` 的终态**，两个 `AUTHORITATIVE_STATES` 都是
`frozenset({"settled"})`。这不是冲突，因为：

  · 两个模块各是各的，**互不 import**，也没有共同基类；
  · 各守各的表：本模块只写 `ap_case` / `ap_payment_observation`，
    退款域那个只写 `refund_case` / `payment_observation`，表名一个都不重
    （见 `schema.sql` 抬头「表名前缀」那一段）；
  · 写入方不同：本域是 `ap.observe`，退款域是 `payment.observe`。
    把 `payment.observe` 递给本模块，第 ① 道会当场拒 —— 它不是本域的权威写入方。

`maos/tests/test_ap_guard.py::test_ap_and_refund_guards_are_independent` 把这三条
钉住：同一个 store 里两个域各推进一个案子到 settled，互不干扰，且任一方的
writer 都写不进对方的表。

## 业务状态不进 Task 状态机（铁律 9）

`received` / `matched` / `payment_requested` / `settled` / `rejected` /
`compensated` 全是 `ap_case` 自己的字段。`maos/contracts/states.py` 一个新状态、
一条新迁移都没加，场景的收口断言之一就是这件事。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import objects

# ---------------------------------------------------------------- 冻结常量
#: 全系统唯一写得进 `AUTHORITATIVE_STATES` 的 actor。
#: 值是 skill 的 `contract.name` —— skill 把自己的名字递进来，不是自报家门的字符串
#: 常量各写一份（各写一份就会漂，而漂的症状是守卫悄悄放行了别人）。
AUTHORITATIVE_WRITER = "ap.observe"

#: 只有 AUTHORITATIVE_WRITER 写得进来的状态集合。
#: 现在只有 settled；将来若有第二个「外部说了才算」的终态，加进这里而不是散在判断里。
AUTHORITATIVE_STATES = frozenset({"settled"})

#: 权威终态 -> 该终态要求回单里的 `observed_state` 取值。
#: 与 AUTHORITATIVE_STATES **同增同减**：加一个权威终态就必须在这里给出它的判据，
#: 漏配不会放行（第 ④ 道见到没有判据的权威终态直接拒），否则「有回单」会被当成
#: 「回单说付出去了」—— 那正是这张表要堵的洞。
#:
#: 取值域的出处：`observed_state` 落的是银行回单的 `status` 字段，
#: 而它的五个取值由 `maos/tools/ap.py` 的 STATUS_* 定死：
#: accepted / pending / unknown / settled / failed，其中终态只有 settled 与 failed。
#: 所以 settled 的判据集合只收 "settled" 一个值。
#:
#: 特别不要把 "accepted" 写进来：那是**银行受理了指令**，不是**钱划走了**。
#: 一条 accepted 的回单三个字段齐全，在第 ③ 道眼里与终态回单无从分辨。
AUTHORITATIVE_RECEIPT_STATE: dict[str, frozenset[str]] = {
    "settled": frozenset({"settled"}),
}

#: 业务状态机（**不是** Task 状态机，铁律 9）：主干三段 + 两个分支。
#:
#: `received -> rejected` 是三单匹配没过那条路；`matched -> rejected` 是人工驳回
#: （匹配过了但主管不批）。两条都保留，因为拒付理由完全不同，合并成一条会让
#: 「这笔为什么没付」在状态机上分辨不出来。
#:
#: `payment_requested -> compensated` 是本域失败路径的收口：指令发出去了、
#: 回单问不出来、补偿做完之后才走这一跳。
BIZ_STATUS_FLOW: dict[str, tuple[str, ...]] = {
    "received":          ("matched", "rejected"),
    "matched":           ("payment_requested", "rejected", "compensated"),
    "payment_requested": ("settled", "compensated"),
    "settled":           (),
    "rejected":          (),
    "compensated":       (),
}

INITIAL_STATUS = "received"

#: 与退款域共用同一个事件类型名。对审计与 Trace 来说「有人试图越权写权威终态」
#: 是同一件事，按 `detail.domain` 区分是哪个域 —— 口径同 `CompensationExecuted`。
#: 另起一个名字会让「这个 Plan 有没有越权写入」要查两处，漏一处就是假绿。
VIOLATION_EVENT = "AuthoritativeFactViolation"

#: 本域在事件 detail 里的域标记。
DOMAIN = "ap"

#: 同一个案号上来了一份业务字段不一样的受理 —— 落这个事件类型。
#: 与 `VIOLATION_EVENT` 分开：越权写入是「你不该写」，这里是「你写的和库里那份
#: 不是同一件事」，两种排查方向完全不同。
CASE_CONFLICT_EVENT = "ApCaseIdentityConflict"

#: 一条回单至少要有的字段。缺任何一个都算「没有回单」。
#: `bank_reference` 在里面 —— 见模块 docstring「比退款域多一条」。
_OBSERVATION_REQUIRED = ("instruction_id", "observed_state", "bank_reference")

#: 判定「这是不是同一件事的重放」要逐字段比对的业务字段。
#:
#: `biz_status` 与 `created_at` **不在里面**：前者是案子建成之后被推进的结果，
#: 后者是第一次受理的时刻 —— 拿它们比对会让每一次正常重放都判成冲突。
#: `amount_claimed` 在里面：它是三单匹配拿去比对的输入，悄悄换掉等于把匹配绕过去。
_CASE_IDENTITY_FIELDS = ("supplier_id", "po_id", "po_version", "invoice_id", "gr_id",
                         "amount_claimed", "currency", "plan_id")


class AuthoritativeFactViolation(RuntimeError):
    """非权威写入方试图写入权威终态，或权威终态没有回单兜底。

    定义在本模块而不是 `contracts/` —— contracts 是冻结面，且这是应付账款域自己的
    业务规则，不是内核契约。**也不复用退款域那个同名类**：两个域的守卫互不 import
    是本域可移植性论证的一部分（见模块 docstring）。
    """


class BizStatusTransitionError(ValueError):
    """业务状态迁移不在 `BIZ_STATUS_FLOW` 里。"""


class CaseIdentityConflict(ValueError):
    """同一个 `(tenant_id, case_id)` 上来了一份**业务字段不一样**的受理。

    不是重放，是两件事撞了同一个案号。定义成 `ValueError` 而不是复用
    `AuthoritativeFactViolation`：那个说的是「你没资格写」，这个说的是
    「你写的和库里那份不是同一件事」。
    """


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_invocation_id(invocation_id: str) -> str:
    """actor 溯源的唯一锚点，空了这条审计链就断了。"""
    if not invocation_id:
        raise ValueError("invocation_id 不许为空：它是 ap_case 每一次写入的 actor 锚点")
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
        "invoice_id":     str(row["invoice_id"]),
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
    invoice_id: str,
    gr_id: str,
    amount_claimed: Any,
    plan_id: str,
    actor_skill: str,
    invocation_id: str,
    currency: str = "CNY",
) -> dict:
    """建一个 ap_case，落 `received`。这是本表唯一的插入口径，**且是幂等的**。

    `biz_status` 不接受调用方指定 —— 想直接建成 settled 的路必须从一开始就不存在，
    否则守卫只挡得住 update，挡不住 insert。

    **幂等语义**（收票这一步会被返工重跑，而主键是 `(tenant_id, case_id)`）：

      · 案号已在库 + `_CASE_IDENTITY_FIELDS` 逐字段相同 → 一个字节都不写，返回
        **既有那一行**（原 `created_at`、原 `biz_status`）。所以这里既不能
        `INSERT OR REPLACE` 也不能 `ON CONFLICT DO UPDATE`：那两种写法会让一次
        重跑把已经推进到 matched / payment_requested 的案子**静悄悄**倒回
        `received`，比裸 INSERT 抛异常坏得多。
      · 案号已在库 + 任一业务字段不同 → 落一条 `CASE_CONFLICT_EVENT` 事件并抛
        `CaseIdentityConflict`。**这一档不许静默**：悄悄收下新金额，则库里的
        `amount_claimed` 是三单匹配真正拿去比对的输入，等于把匹配绕过去；
        悄悄丢弃，则调用方拿到一份和自己递进来的发票对不上的案子，同样一点信号都没有。

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
        "invoice_id":     str(invoice_id),
        "gr_id":          str(gr_id),
        "amount_claimed": objects.money_str(amount_claimed),
        "currency":       str(currency),
        "plan_id":        str(plan_id),
    }
    conn = objects._conn(store)
    with objects.lock_of(store):
        conn.execute(
            "INSERT INTO ap_case (tenant_id, case_id, supplier_id, po_id, po_version,"
            " invoice_id, gr_id, amount_claimed, currency, biz_status, plan_id, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT (tenant_id, case_id) DO NOTHING",
            (tenant_id, case_id, incoming["supplier_id"], incoming["po_id"],
             incoming["po_version"], incoming["invoice_id"], incoming["gr_id"],
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
            "收票重跑只在业务字段逐字段相同时幂等；对不上说明这不是同一张发票的重放，"
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
    reason: str = "",
) -> dict:
    """`ap_case.biz_status` 的唯一写入路径。

    四道闸，顺序不可换（每一道旁边写了为什么它必须在这个位置）：

    ① `new_status` 落在 `AUTHORITATIVE_STATES` 而 actor 不是权威写入方 → 拒 + 留证据
    ② 递了 `observation` 却不是权威写入方 → 拒（否则等于给别人开伪造回单的口子）
    ③ 权威终态**必须**带回单，且字段齐全（含银行流水号）
    ④ 回单说的**得是这件事**：`observed_state` 必须落在该终态的判据集合里
    """
    _require_invocation_id(invocation_id)
    case = get_case(store, tenant_id, case_id)
    plan_id = (case or {}).get("plan_id", "")

    # ① 权威闸放在最前面：case 不存在也照样记一笔越权尝试。
    #    先查存在性会让「对不存在的 case 越权写 settled」以 LookupError 收场，
    #    证据就没了 —— 而那恰恰是最该留痕的一种试探。
    if new_status in AUTHORITATIVE_STATES and actor_skill != AUTHORITATIVE_WRITER:
        _log_violation(store, plan_id=plan_id, tenant_id=tenant_id, case_id=case_id,
                       attempted=new_status, actor=actor_skill, invocation_id=invocation_id,
                       why=f"{new_status} 只能由 {AUTHORITATIVE_WRITER} 写入")
        raise AuthoritativeFactViolation(
            f"{actor_skill} 试图把 case={case_id} 写成 {new_status}；"
            f"该状态的权威在银行，只有 {AUTHORITATIVE_WRITER} 观察到回单后才写得进来"
        )

    # ② 回单只有权威写入方递得进来，否则等于给别人开了个伪造回单的口子。
    if observation is not None and actor_skill != AUTHORITATIVE_WRITER:
        _log_violation(store, plan_id=plan_id, tenant_id=tenant_id, case_id=case_id,
                       attempted=new_status, actor=actor_skill, invocation_id=invocation_id,
                       why=f"银行回单只能由 {AUTHORITATIVE_WRITER} 提交")
        raise AuthoritativeFactViolation(
            f"{actor_skill} 递交了银行回单；回单是外部权威事实，"
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

    # ③ 权威终态必须有回单，且字段齐全。没有回单的 settled 就是把外部状态写死为终态。
    if new_status in AUTHORITATIVE_STATES:
        missing = [f for f in _OBSERVATION_REQUIRED if not (observation or {}).get(f)]
        if missing:
            _log_violation(store, plan_id=plan_id, tenant_id=tenant_id, case_id=case_id,
                           attempted=new_status, actor=actor_skill,
                           invocation_id=invocation_id,
                           why=f"银行回单缺字段 {missing}")
            raise AuthoritativeFactViolation(
                f"写 {new_status} 必须同事务附银行回单，缺字段：{missing}。"
                f"其中 bank_reference 是**可对账的凭据**，没有它的「已付」在财务上"
                f"对不了账 —— 「有一张回单」不等于「有一张能拿去对账的回单」"
            )

        # ④ 回单还得**说的是这件事**。③ 只保证「有一张回单」，不保证那张回单说钱走了 ——
        #    一条 observed_state='accepted' 的受理回单三个字段齐全，在 ③ 眼里与终态
        #    回单无从分辨。放过它，系统持有的就只是「银行收下了指令」，不是
        #    「银行说钱划走了」，而后者才是 settled 这个词的全部含义（铁律 8）。
        #    防线必须在守卫里，不能只活在某个 skill 的 `if status == "settled"` 分支里：
        #    那种防线改一次分支顺序两层都不会响。
        allowed = AUTHORITATIVE_RECEIPT_STATE.get(new_status)
        if allowed is None:
            # 加了权威终态却没给判据。fail-closed：宁可写不进去，也不许默认放行 ——
            # 默认放行会让这个终态退回到「有回单就算数」，静默且没人会发现。
            _log_violation(store, plan_id=plan_id, tenant_id=tenant_id, case_id=case_id,
                           attempted=new_status, actor=actor_skill,
                           invocation_id=invocation_id,
                           why=f"{new_status} 没有在 AUTHORITATIVE_RECEIPT_STATE 里配回单判据")
            raise AuthoritativeFactViolation(
                f"{new_status} 在 AUTHORITATIVE_STATES 里，却没有在 "
                f"AUTHORITATIVE_RECEIPT_STATE 里给出回单判据；两张表必须同增同减"
            )
        seen = str((observation or {}).get("observed_state"))
        if seen not in allowed:
            _log_violation(store, plan_id=plan_id, tenant_id=tenant_id, case_id=case_id,
                           attempted=new_status, actor=actor_skill,
                           invocation_id=invocation_id,
                           why=f"回单 observed_state={seen!r}，不在 {new_status} 的判据 "
                               f"{sorted(allowed)} 里")
            raise AuthoritativeFactViolation(
                f"写 {new_status} 的回单说的是 {seen!r}，不是 {sorted(allowed)}；"
                f"「有一张回单」不等于「银行说钱划走了」，外部权威没这么说就不许收口"
            )

    conn = objects._conn(store)
    with objects.lock_of(store):
        try:
            if observation is not None:
                obs = dict(observation)
                conn.execute(
                    "INSERT INTO ap_payment_observation (tenant_id, case_id, instruction_id,"
                    " observed_state, bank_reference, value_date, raw_advice_json,"
                    " observed_at, actor_invocation_id) VALUES (?,?,?,?,?,?,?,?,?)",
                    (tenant_id, case_id, obs["instruction_id"], obs["observed_state"],
                     obs.get("bank_reference", ""), obs.get("value_date", ""),
                     obs.get("raw_advice_json", "{}"),
                     obs.get("observed_at") or _now(), invocation_id),
                )
            conn.execute(
                "UPDATE ap_case SET biz_status=? WHERE tenant_id=? AND case_id=?",
                (new_status, tenant_id, case_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    store.append_event_log({
        "plan_id": plan_id,
        "event_type": "ApBizStatusChanged",
        "from_state": cur, "to_state": new_status, "reason": reason,
        "detail": {"domain": DOMAIN, "tenant_id": tenant_id, "case_id": case_id,
                   "actor": actor_skill, "invocation_id": invocation_id,
                   "observation_attached": observation is not None},
    })
    return get_case(store, tenant_id, case_id)  # type: ignore[return-value]


def record_observation(
    store: Any,
    *,
    tenant_id: str,
    case_id: str,
    instruction_id: str,
    observed_state: str,
    invocation_id: str,
    actor_skill: str,
    bank_reference: str = "",
    value_date: str = "",
    raw_advice_json: str = "{}",
) -> None:
    """落一条**非终态**（或明确失败）的观察，不推进业务状态。

    为什么要有这一条：银行明确拒付（`failed`）时，观察必须留痕，否则「银行说没付
    成」这件事只活在日志里。但那一刻**没有合法的目标状态可迁** —— 走到
    `compensated` 意味着补偿已经做完，而补偿是失败路径的事，在这里替它宣布收口
    就是又一次把状态写死。

    权威写入方之外的 actor 一律拒：回单是外部权威事实，同 `update_biz_status` 第 ②
    道。这里不能走 `objects.execute` 图省事 —— 那条路对 `ap_payment_observation`
    不设限，等于给伪造回单留了个后门。
    """
    _require_invocation_id(invocation_id)
    if actor_skill != AUTHORITATIVE_WRITER:
        case = get_case(store, tenant_id, case_id)
        _log_violation(store, plan_id=(case or {}).get("plan_id", ""), tenant_id=tenant_id,
                       case_id=case_id, attempted=f"observation:{observed_state}",
                       actor=actor_skill, invocation_id=invocation_id,
                       why=f"银行回单只能由 {AUTHORITATIVE_WRITER} 落库")
        raise AuthoritativeFactViolation(
            f"{actor_skill} 试图落一条银行回单；只有 {AUTHORITATIVE_WRITER} 能落库"
        )
    if observed_state in AUTHORITATIVE_RECEIPT_STATE.get("settled", frozenset()):
        # 挡住「用这条旁路落一条 settled 观察、再让别人读它当成到账」这条路。
        # 权威终态的观察必须与状态更新同事务，走 update_biz_status。
        raise AuthoritativeFactViolation(
            f"observed_state={observed_state!r} 是权威终态的判据值，"
            f"必须经 update_biz_status 与状态更新同事务写入，不许从这条旁路单独落"
        )
    conn = objects._conn(store)
    with objects.lock_of(store):
        conn.execute(
            "INSERT OR REPLACE INTO ap_payment_observation (tenant_id, case_id,"
            " instruction_id, observed_state, bank_reference, value_date,"
            " raw_advice_json, observed_at, actor_invocation_id)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (tenant_id, case_id, instruction_id, observed_state, bank_reference,
             value_date, raw_advice_json, _now(), invocation_id),
        )
        conn.commit()


def get_case(store: Any, tenant_id: str, case_id: str) -> dict | None:
    """按 (tenant_id, case_id) 读一个 case；不存在返回 None。"""
    rows = objects.query(
        store, "SELECT * FROM ap_case WHERE tenant_id=? AND case_id=?", (tenant_id, case_id))
    return rows[0] if rows else None


def observations_of(store: Any, tenant_id: str, case_id: str) -> list[dict]:
    return objects.query(
        store, "SELECT * FROM ap_payment_observation WHERE tenant_id=? AND case_id=?"
               " ORDER BY observed_at", (tenant_id, case_id))
