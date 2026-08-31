"""returned guard —— 差错案件的权威事实边界（铁律 8）。

题眼：**MAOS 不持有权威事实**。一笔钱有没有退回来，权威在清算方，不在我们库里。
所以 `returned` 这一个终态，全系统只有 `investigation.observe` 这一个 skill 写得进去，
而且必须同事务附上它读到的那份观察（`resolution_observation`）。

## 本域的招牌判据：肯定答复与「钱回来了」不是一回事

退款域那条边界是「有回执 ≠ 回执说到账了」。本域这条更绕一层，而且**绕的这一层
是 ISO 20022 规范自己规定的**：

    camt.056（FIToFIPaymentCancellationRequest）  发出去：请撤销那一笔
    camt.029（ResolutionOfInvestigation）         答回来：你那个请求我怎么处理的
    pacs.004（PaymentReturn）                     答回来：钱退回来了

camt.029 的结论码取自 `ExternalInvestigationExecutionConfirmation1Code`，
它**既有否定也有肯定**：

    RJCR  RejectedCancellationRequest    撤销请求被拒
    PDCR  PendingCancellationRequest     撤销请求处理中 —— 还没有答案
    CNCL  CancelledAsPerRequest          「撤销成功」  ← **肯定答复，但不是资金证据**

于是本域最容易犯、也最像成功的那个错误是：**收到 CNCL 就写 returned**。
CNCL 说的是「你要求撤销的那条指令，我照办了」；钱回没回来是另一条报文
（pacs.004）说的事，它带的是 `ExternalReturnReason1Code` 和一个退回金额。
把这两件事压成一个布尔，正是「Agent 都回复完成 ≠ 业务成功」在这个域里的具体形状。

所以第 ④ 道判据不只看 observed_state，还看**这条观察是哪种报文**：
`returned` 只认 pacs.004。camt.029 无论说得多肯定，都写不进 `returned`。

> 与派单原文的一处出入（已记 docs/DECISIONS.md）：派单写「camt.029 恒为否定答复，
> 肯定答复走 pacs.004」。按官方码表实查，camt.029 **可以**是肯定答复（CNCL），
> 只是它肯定的是「指令已撤销」而不是「资金已退回」。判据因此比派单原文更严 ——
> 派单那个前提下「不许拿 camt.029 写 returned」是白给的（它反正总是否定的），
> 而真实规范下它是一条真的会被触发的防线。出处见
> `maos/domain/investigation/iso20022_codes.json` 的 `_provenance`。

## 越权写入不静默失败

抛 `AuthoritativeFactViolation` + 落一条事件。理由与 `scripts/` 下那个 Bash 守卫同：
「系统拒绝了一次越权写入」本身就是要拿给评委看的证据，吞掉就没了。

`investigation_case` 的一切写入只有两个入口 —— `create_case()` 建、
`update_biz_status()` 改，不留第三条路径。两道拦截：
  - 运行时：`objects.execute()` 见到 investigation_case 的写语句直接抛 `BypassedGuardError`
  - 提交前：grep 自查（见 `maos/tests/test_investigation_guard.py::test_no_bypass_path`）
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import objects

# ---------------------------------------------------------------- 冻结常量
#: 全系统唯一写得进 `AUTHORITATIVE_STATES` 的 skill。
AUTHORITATIVE_WRITER = "investigation.observe"

#: 只有 AUTHORITATIVE_WRITER 写得进来的状态集合。
#: 现在只有 returned；将来若有第二个「外部说了才算」的终态，加进这里而不是散在判断里，
#: 并且**必须同时**在 AUTHORITATIVE_EVIDENCE 里给出它的证据判据（见第 ④ 道）。
AUTHORITATIVE_STATES = frozenset({"returned"})

# ---- 报文族。observe 归一之后写进 resolution_observation.message_type 的前缀 ----
MSG_CANCELLATION_REQUEST = "camt.056"     # 发出去的撤销请求
MSG_RESOLUTION = "camt.029"               # 决议答复（肯定/否定/未决都走它）
MSG_PAYMENT_RETURN = "pacs.004"           # 退款报文 —— **唯一的资金证据**

#: 归一后的观察口径。`resolution_observation.observed_state` 只能取这几个值。
#:
#: `CANCELLATION_CONFIRMED` 与 `RETURNED` **必须分开**：前者是 camt.029/CNCL
#: 「指令已撤销」，后者是 pacs.004「钱回来了」。合并这两个取值等于在数据模型层面
#: 就把本域的招牌判据抹掉了 —— 那样连守卫都没得守。
OBS_RETURNED = "returned"
OBS_CANCELLATION_CONFIRMED = "cancellation_confirmed"
OBS_REJECTED = "rejected"
OBS_PENDING = "pending"
OBS_UNOBSERVED = "unobserved"

OBSERVED_STATES = frozenset({
    OBS_RETURNED, OBS_CANCELLATION_CONFIRMED, OBS_REJECTED, OBS_PENDING, OBS_UNOBSERVED,
})

#: 终态观察。只有这三个是「问出结果了」；pending / unobserved 都是没问出来。
TERMINAL_OBSERVATIONS = frozenset({OBS_RETURNED, OBS_CANCELLATION_CONFIRMED, OBS_REJECTED})


class _Evidence:
    """一个权威终态要求的证据形状。字段少，但每一条都挡掉一种具体的写死方式。"""

    __slots__ = ("message_family", "observed_states", "requires_amount", "requires_code")

    def __init__(self, *, message_family: str, observed_states: frozenset[str],
                 requires_amount: bool, requires_code: str) -> None:
        self.message_family = message_family
        self.observed_states = observed_states
        self.requires_amount = requires_amount
        self.requires_code = requires_code

    def __repr__(self) -> str:                        # pragma: no cover —— 只给报错用
        return (f"_Evidence(family={self.message_family!r}, "
                f"states={sorted(self.observed_states)}, "
                f"amount={self.requires_amount}, code={self.requires_code!r})")


#: 权威终态 -> 它要求的证据。与 `AUTHORITATIVE_STATES` **同增同减**：
#: 加一个权威终态就必须在这里给出它的判据，漏配不会放行（第 ④ 道见到没有判据的
#: 权威终态直接拒），否则「有观察」会被当成「观察说钱回来了」。
#:
#: `returned` 这一条的三个要求各挡一种错法：
#:   · message_family=pacs.004  挡「拿 camt.029/CNCL 写 returned」——本域的招牌
#:   · requires_amount          挡「有报文但没金额」的空壳退款
#:   · requires_code            挡「没有退回原因码」——ISO 规定 pacs.004 必带 RtrRsn
AUTHORITATIVE_EVIDENCE: dict[str, _Evidence] = {
    "returned": _Evidence(
        message_family=MSG_PAYMENT_RETURN,
        observed_states=frozenset({OBS_RETURNED}),
        requires_amount=True,
        requires_code="return_reason_code",
    ),
}

#: 业务状态机（**不是** Task 状态机，铁律 9）：主干四段 + 两个分支。
BIZ_STATUS_FLOW: dict[str, tuple[str, ...]] = {
    "filed":             ("classified", "rejected"),
    "classified":        ("cancellation_sent", "rejected", "compensated"),
    "cancellation_sent": ("returned", "rejected", "compensated"),
    "returned":          (),
    "rejected":          (),
    "compensated":       (),
}

INITIAL_STATUS = "filed"

VIOLATION_EVENT = "AuthoritativeFactViolation"

#: 同一个案号上来了一份业务字段不一样的受理 —— 落这个事件类型。
#: 与 `VIOLATION_EVENT` 分开：越权写入是「你不该写」，这里是「你写的和库里那份
#: 不是同一件事」，两种排查方向完全不同。
CASE_CONFLICT_EVENT = "InvestigationCaseIdentityConflict"

#: 一条观察至少要有的字段。缺任何一个都算「没有观察」。
_OBSERVATION_REQUIRED = ("request_id", "message_type", "observed_state")

#: 判定「这是不是同一件事的重放」要逐字段比对的业务字段。
#:
#: `biz_status`、`created_at`、`cancellation_reason_code` **不在里面**：
#: 前两个是案子建成之后被推进的结果与第一次受理的时刻，第三个要等 classify 才有值 ——
#: 拿它们比对会让每一次正常重放都判成冲突。
_CASE_IDENTITY_FIELDS = ("creator_agent", "assignee_agent", "original_msg_id",
                         "original_version", "end_to_end_id", "amount", "currency",
                         "plan_id")


class AuthoritativeFactViolation(RuntimeError):
    """非权威写入方试图写入权威终态，或权威终态没有合格证据兜底。

    定义在本模块而不是 `contracts/` —— contracts 是冻结面，且这是本域自己的
    业务规则，不是内核契约。
    """


class BizStatusTransitionError(ValueError):
    """业务状态迁移不在 `BIZ_STATUS_FLOW` 里。"""


class CaseIdentityConflict(ValueError):
    """同一个 `(tenant_id, case_id)` 上来了一份**业务字段不一样**的受理。

    不是重放，是两件事撞了同一个案号。差错处理域里案号是与清算方对话的锚点
    （camt.056 的 Case/Id），复用一个案号意味着两笔不同的争议共用一条对话 ——
    没有正确解，只能当场响。
    """


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_invocation_id(invocation_id: str) -> str:
    """actor 溯源的唯一锚点，空了这条审计链就断了。"""
    if not invocation_id:
        raise ValueError(
            "invocation_id 不许为空：它是 investigation_case 每一次写入的 actor 锚点")
    return invocation_id


def message_family(message_type: str) -> str:
    """把 `camt.029.001.08` 归一成 `camt.029`。

    判据按**族**而不是按具体版本：ISO 的报文版本号每年都在涨（camt.029 从 001.08
    到 001.11 都在用），按全名硬比会让换一版报文就把守卫判穿 —— 而那正是
    「规范会改版」最常见的落地方式。
    """
    parts = str(message_type or "").split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else str(message_type or "")


def _identity_of(row: dict) -> dict:
    """把库里那一行折成与 `create_case` 入参同一个形状，好逐字段比。

    必须过一遍类型转换：sqlite 的 INTEGER / REAL 回来是 int / float，而调用方递
    进来的可能是 str 或 int —— 不归一就会把「12500 与 12500.0」判成冲突，
    幂等当场退化成「每次重跑都报冲突」（退款域踩过，见其 `_identity_of`）。
    """
    return {
        "creator_agent":    str(row["creator_agent"]),
        "assignee_agent":   str(row["assignee_agent"]),
        "original_msg_id":  str(row["original_msg_id"]),
        "original_version": int(row["original_version"]),
        "end_to_end_id":    str(row["end_to_end_id"]),
        "amount":           float(row["amount"]),
        "currency":         str(row["currency"]),
        "plan_id":          str(row["plan_id"]),
    }


def _log_case_conflict(store: Any, *, plan_id: str, tenant_id: str, case_id: str,
                       diff: dict, actor: str, invocation_id: str) -> None:
    """拒绝一次案号复用也要留证据 —— 理由同模块 docstring：吞掉就没了。"""
    store.append_event_log({
        "plan_id": plan_id,
        "event_type": CASE_CONFLICT_EVENT,
        "reason": f"case_id 被复用，业务字段对不上：{sorted(diff)}",
        "detail": {"tenant_id": tenant_id, "case_id": case_id,
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
        "detail": {"tenant_id": tenant_id, "case_id": case_id, "attempted": attempted,
                   "actor": actor, "invocation_id": invocation_id,
                   "authoritative_writer": AUTHORITATIVE_WRITER},
    })


# ---------------------------------------------------------------- 写入口径
def create_case(
    store: Any,
    *,
    tenant_id: str,
    case_id: str,
    creator_agent: str,
    assignee_agent: str,
    original_msg_id: str,
    original_version: int,
    end_to_end_id: str,
    amount: float,
    currency: str,
    plan_id: str,
    actor_skill: str,
    invocation_id: str,
) -> dict:
    """建一个 investigation_case，落 `filed`。本表唯一的插入口径，**且是幂等的**。

    `biz_status` 不接受调用方指定 —— 想直接建成 returned 的路必须从一开始就不存在，
    否则守卫只挡得住 update，挡不住 insert。

    **幂等语义**（受理这一步会被返工重跑，而主键是 `(tenant_id, case_id)`）：

      · 案号已在库 + `_CASE_IDENTITY_FIELDS` 逐字段相同 → 一个字节都不写，返回
        **既有那一行**（原 `created_at`、原 `biz_status`）。所以这里既不能
        `INSERT OR REPLACE` 也不能 `ON CONFLICT DO UPDATE`：那两种写法会让一次
        重跑把已经推进到 cancellation_sent 的案子**静悄悄**倒回 `filed`。
      · 案号已在库 + 任一业务字段不同 → 落一条 `CASE_CONFLICT_EVENT` 事件并抛
        `CaseIdentityConflict`。**这一档不许静默**：悄悄收下新金额会让后续
        camt.056 发出去的撤销金额与案子建立时不是一笔；悄悄丢弃则让调用方拿到一份
        和自己递进来的输入对不上的 case，同样一点信号都没有。

    用 `ON CONFLICT (tenant_id, case_id) DO NOTHING` 而不是 `INSERT OR IGNORE`：
    后者会把 `biz_status` 那条 CHECK 约束的失败一并吞掉。判定放在插入**之后**回读
    比对，而不是插入前先查一次 —— 先查后插在 `lock_of()` 退化成 nullcontext 的
    Store 上有 TOCTOU 窗口。
    """
    _require_invocation_id(invocation_id)
    incoming = {
        "creator_agent":    str(creator_agent),
        "assignee_agent":   str(assignee_agent),
        "original_msg_id":  str(original_msg_id),
        "original_version": int(original_version),
        "end_to_end_id":    str(end_to_end_id),
        "amount":           float(amount),
        "currency":         str(currency),
        "plan_id":          str(plan_id),
    }
    conn = objects._conn(store)
    with objects.lock_of(store):
        conn.execute(
            "INSERT INTO investigation_case (tenant_id, case_id, creator_agent,"
            " assignee_agent, original_msg_id, original_version, end_to_end_id, amount,"
            " currency, cancellation_reason_code, biz_status, plan_id, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT (tenant_id, case_id) DO NOTHING",
            (tenant_id, case_id, incoming["creator_agent"], incoming["assignee_agent"],
             incoming["original_msg_id"], incoming["original_version"],
             incoming["end_to_end_id"], incoming["amount"], incoming["currency"],
             "", INITIAL_STATUS, incoming["plan_id"], _now()),
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
            "受理重跑只在业务字段逐字段相同时幂等；对不上说明这不是同一件事的重放，"
            "既不许覆盖也不许静默丢弃 —— 换个 case_id，或先查清这个案号为什么被复用"
        )
    return case


def set_classification(store: Any, tenant_id: str, case_id: str, reason_code: str,
                       actor_skill: str, invocation_id: str, *, reason: str = "") -> dict:
    """定性：把 camt.056 要用的撤销原因码写进案子，并推进到 `classified`。

    单独一个入口而不是让 `update_biz_status` 顺手带一个字段：原因码是**发报文时
    要填进 camt.056 的那个值**，它和状态迁移是两件事，压在一起会让「定性了但没推进」
    和「推进了但没定性」这两种半成品都变得可表达。这里两件事同事务，要么都成要么都不成。
    """
    _require_invocation_id(invocation_id)
    if not str(reason_code or "").strip():
        raise ValueError(
            "定性必须给出撤销原因码（ExternalCancellationReason1Code）——"
            "空原因码的 camt.056 发不出去，而且『没定性』不该被记成『定性完了』")

    case = get_case(store, tenant_id, case_id)
    if case is None:
        raise LookupError(f"没有这个 case：tenant={tenant_id} case={case_id}")
    cur = case["biz_status"]
    if "classified" not in BIZ_STATUS_FLOW.get(cur, ()):
        raise BizStatusTransitionError(
            f"业务状态不许从 {cur} 迁到 classified（case={case_id}）；"
            f"{cur} 的合法去向：{BIZ_STATUS_FLOW.get(cur, ()) or '无（终态）'}")

    conn = objects._conn(store)
    with objects.lock_of(store):
        try:
            conn.execute(
                "UPDATE investigation_case SET cancellation_reason_code=?, biz_status=?"
                " WHERE tenant_id=? AND case_id=?",
                (str(reason_code), "classified", tenant_id, case_id))
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    store.append_event_log({
        "plan_id": case["plan_id"],
        "event_type": "InvestigationBizStatusChanged",
        "from_state": cur, "to_state": "classified", "reason": reason,
        "detail": {"tenant_id": tenant_id, "case_id": case_id, "actor": actor_skill,
                   "invocation_id": invocation_id, "reason_code": str(reason_code)},
    })
    return get_case(store, tenant_id, case_id)          # type: ignore[return-value]


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
    """`investigation_case.biz_status` 的唯一写入路径。

    - `new_status` 落在 `AUTHORITATIVE_STATES` 且 `actor_skill != AUTHORITATIVE_WRITER`
      → 落 `AuthoritativeFactViolation` 事件并抛 `AuthoritativeFactViolation`。
    - 写权威终态必须带 `observation`，与状态更新**同事务**插入 `resolution_observation`。
    - 观察还必须是**对的那种报文**：`returned` 只认 pacs.004，见第 ④ 道。
    - 迁移不在 `BIZ_STATUS_FLOW` 里 → 抛 `BizStatusTransitionError`。
    """
    _require_invocation_id(invocation_id)
    case = get_case(store, tenant_id, case_id)
    plan_id = (case or {}).get("plan_id", "")

    # ① 权威闸放在最前面：case 不存在也照样记一笔越权尝试。
    #    先查存在性会让「对不存在的 case 越权写 returned」以 LookupError 收场，
    #    证据就没了 —— 而那恰恰是最该留痕的一种试探。
    if new_status in AUTHORITATIVE_STATES and actor_skill != AUTHORITATIVE_WRITER:
        _log_violation(store, plan_id=plan_id, tenant_id=tenant_id, case_id=case_id,
                       attempted=new_status, actor=actor_skill,
                       invocation_id=invocation_id,
                       why=f"{new_status} 只能由 {AUTHORITATIVE_WRITER} 写入")
        raise AuthoritativeFactViolation(
            f"{actor_skill} 试图把 case={case_id} 写成 {new_status}；"
            f"该状态的权威在清算方，只有 {AUTHORITATIVE_WRITER} 观察到 "
            f"{MSG_PAYMENT_RETURN} 退款报文之后才写得进来"
        )

    # ② 观察只有权威写入方递得进来，否则等于给别人开了个伪造报文的口子。
    if observation is not None and actor_skill != AUTHORITATIVE_WRITER:
        _log_violation(store, plan_id=plan_id, tenant_id=tenant_id, case_id=case_id,
                       attempted=new_status, actor=actor_skill,
                       invocation_id=invocation_id,
                       why=f"决议观察只能由 {AUTHORITATIVE_WRITER} 提交")
        raise AuthoritativeFactViolation(
            f"{actor_skill} 递交了清算方决议观察；那是外部权威事实，"
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

    # ③ 权威终态必须有观察。没有观察的 returned 就是把外部状态写死为终态。
    if new_status in AUTHORITATIVE_STATES:
        missing = [f for f in _OBSERVATION_REQUIRED if not (observation or {}).get(f)]
        if missing:
            _log_violation(store, plan_id=plan_id, tenant_id=tenant_id, case_id=case_id,
                           attempted=new_status, actor=actor_skill,
                           invocation_id=invocation_id,
                           why=f"观察缺字段 {missing}")
            raise AuthoritativeFactViolation(
                f"写 {new_status} 必须同事务附决议观察，缺字段：{missing}")

        # ④ 观察还得**是那种报文**。③ 只保证「有一条观察」，不保证那条观察证明了资金。
        #    一条 camt.029 / CNCL（CancelledAsPerRequest）三个字段齐全，在 ③ 眼里
        #    与 pacs.004 无从分辨 —— 而它说的是「撤销指令照办了」，不是「钱回来了」。
        #    放过它，系统持有的就只是「清算方确认撤销了」，不是「资金已退回」，
        #    而后者才是 returned 这个词的全部含义（铁律 8）。
        ev = AUTHORITATIVE_EVIDENCE.get(new_status)
        if ev is None:
            # 加了权威终态却没给证据判据。fail-closed：宁可写不进去，也不许默认放行 ——
            # 默认放行会让这个终态退回到「有观察就算数」，静默且没人会发现。
            _log_violation(store, plan_id=plan_id, tenant_id=tenant_id, case_id=case_id,
                           attempted=new_status, actor=actor_skill,
                           invocation_id=invocation_id,
                           why=f"{new_status} 没有在 AUTHORITATIVE_EVIDENCE 里配证据判据")
            raise AuthoritativeFactViolation(
                f"{new_status} 在 AUTHORITATIVE_STATES 里，却没有在 "
                f"AUTHORITATIVE_EVIDENCE 里给出证据判据；两张表必须同增同减")

        obs = observation or {}
        family = message_family(str(obs.get("message_type")))
        if family != ev.message_family:
            _log_violation(store, plan_id=plan_id, tenant_id=tenant_id, case_id=case_id,
                           attempted=new_status, actor=actor_skill,
                           invocation_id=invocation_id,
                           why=(f"观察报文是 {family or '(空)'}，"
                                f"而 {new_status} 只认 {ev.message_family}"))
            raise AuthoritativeFactViolation(
                f"写 {new_status} 的观察来自 {family or '(空)'} 报文，不是 "
                f"{ev.message_family}；{MSG_RESOLUTION} 答的是「撤销请求怎么处理的」，"
                f"只有 {MSG_PAYMENT_RETURN} 答「钱退回来了」—— "
                f"确认撤销不等于资金已退回，外部权威没这么说就不许收口")

        seen = str(obs.get("observed_state"))
        if seen not in ev.observed_states:
            _log_violation(store, plan_id=plan_id, tenant_id=tenant_id, case_id=case_id,
                           attempted=new_status, actor=actor_skill,
                           invocation_id=invocation_id,
                           why=(f"观察 observed_state={seen!r}，不在 {new_status} 的判据 "
                                f"{sorted(ev.observed_states)} 里"))
            raise AuthoritativeFactViolation(
                f"写 {new_status} 的观察说的是 {seen!r}，不是 "
                f"{sorted(ev.observed_states)}；「有一条观察」不等于「清算方说钱回来了」")

        if ev.requires_amount and obs.get("returned_amount") in (None, ""):
            _log_violation(store, plan_id=plan_id, tenant_id=tenant_id, case_id=case_id,
                           attempted=new_status, actor=actor_skill,
                           invocation_id=invocation_id,
                           why=f"{ev.message_family} 观察没有退回金额")
            raise AuthoritativeFactViolation(
                f"写 {new_status} 的 {ev.message_family} 观察没有 returned_amount；"
                "一份不说退了多少钱的退款报文证明不了资金已退回")

        if ev.requires_code and not str(obs.get(ev.requires_code) or "").strip():
            _log_violation(store, plan_id=plan_id, tenant_id=tenant_id, case_id=case_id,
                           attempted=new_status, actor=actor_skill,
                           invocation_id=invocation_id,
                           why=f"{ev.message_family} 观察缺 {ev.requires_code}")
            raise AuthoritativeFactViolation(
                f"写 {new_status} 的 {ev.message_family} 观察缺 {ev.requires_code}；"
                "ISO 20022 规定退款报文必带退回原因码，没有它这份观察不可核对")

    conn = objects._conn(store)
    with objects.lock_of(store):
        try:
            if observation is not None:
                insert_observation(store, tenant_id=tenant_id, case_id=case_id,
                                   observation=observation,
                                   invocation_id=invocation_id, _conn=conn)
            conn.execute(
                "UPDATE investigation_case SET biz_status=? WHERE tenant_id=? AND case_id=?",
                (new_status, tenant_id, case_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    store.append_event_log({
        "plan_id": plan_id,
        "event_type": "InvestigationBizStatusChanged",
        "from_state": cur, "to_state": new_status, "reason": reason,
        "detail": {"tenant_id": tenant_id, "case_id": case_id, "actor": actor_skill,
                   "invocation_id": invocation_id,
                   "observation_attached": observation is not None,
                   "message_type": (observation or {}).get("message_type", "")},
    })
    return get_case(store, tenant_id, case_id)          # type: ignore[return-value]


def insert_observation(store: Any, *, tenant_id: str, case_id: str, observation: dict,
                       invocation_id: str, _conn: Any = None) -> dict:
    """落一条 `resolution_observation`。

    **观察可以单独落，状态迁移不行**：guard 的「同事务附观察」是 returned 的前置条件
    （观察 ⇐ 终态），反过来并不要求每条观察都伴随一次状态迁移 —— 一条
    camt.029/RJCR、或者一次问不出结果的轮询，都该留痕，但它们没有合法的目标状态可迁。
    口径同退款域 `payment_observe.py::_record_failure`。

    `_conn` 由 `update_biz_status` 在它自己的事务里传进来，好让观察与状态更新同生共死；
    外部调用不传，本函数自己走 `objects.execute`。
    """
    state = str(observation.get("observed_state") or "")
    if state not in OBSERVED_STATES:
        raise ValueError(
            f"未知的 observed_state：{state!r}，只能是 {sorted(OBSERVED_STATES)} 之一 —— "
            "归一口径由 guard 定，不许各处自造取值")

    row = (
        tenant_id, case_id,
        str(observation.get("request_id") or ""),
        int(observation.get("poll_seq") or 0),
        str(observation.get("message_type") or ""),
        str(observation.get("confirmation_code") or ""),
        str(observation.get("rejection_code") or ""),
        str(observation.get("return_reason_code") or ""),
        observation.get("returned_amount"),
        str(observation.get("raw_message_json") or "{}"),
        state,
        str(observation.get("observed_at") or _now()),
        invocation_id,
    )
    sql = ("INSERT OR REPLACE INTO resolution_observation (tenant_id, case_id, request_id,"
           " poll_seq, message_type, confirmation_code, rejection_code, return_reason_code,"
           " returned_amount, raw_message_json, observed_state, observed_at,"
           " actor_invocation_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)")
    if _conn is not None:
        _conn.execute(sql, row)
    else:
        objects.execute(store, sql, row)
    return dict(observation)


def get_case(store: Any, tenant_id: str, case_id: str) -> dict | None:
    """按 (tenant_id, case_id) 读一个 case；不存在返回 None。"""
    rows = objects.query(
        store, "SELECT * FROM investigation_case WHERE tenant_id=? AND case_id=?",
        (tenant_id, case_id))
    return rows[0] if rows else None


def observations_of(store: Any, tenant_id: str, case_id: str,
                    *, observed_state: str | None = None) -> list[dict]:
    """按案子取观察，可按归一状态过滤。收口断言与失败路径的「一个字都不写」靠它数。

    按 `poll_seq` 排序而不是 `observed_at`：同一轮问询的几条时间戳可能落在同一微秒，
    按时间排出来的顺序就不稳定了，而这几行是要按顺序念给人听的证据
    （「第 2 次问到 CNCL，第 3 次才拿到退款报文」）。
    """
    if observed_state is None:
        return objects.query(
            store, "SELECT * FROM resolution_observation WHERE tenant_id=? AND case_id=?"
                   " ORDER BY poll_seq, observed_at", (tenant_id, case_id))
    return objects.query(
        store, "SELECT * FROM resolution_observation WHERE tenant_id=? AND case_id=?"
               " AND observed_state=? ORDER BY poll_seq, observed_at",
        (tenant_id, case_id, observed_state))
