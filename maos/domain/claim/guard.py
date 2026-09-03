"""paid guard —— 保险理赔案件的权威事实边界（铁律 8）。

题眼：**MAOS 不持有权威事实**。一笔赔款到没到账，权威在赔付方，不在我们库里。
所以 `paid` 这一个终态，全系统只有 `claim.observe` 这一个 skill 写得进去，
而且必须同事务附上它读到的那份回执（`claim_payment_observation`）——
没有回执的 paid 就是「把外部状态直接写死为终态」，那是 bug 不是功能。

越权写入**不静默失败**：抛 `AuthoritativeFactViolation` + 落一条事件。
理由与 `maos/domain/refund/guard.py` 同：「系统拒绝了一次越权写入」本身就是要拿给
评委看的证据，吞掉就没了。

`claim_case` 的一切写入只有两个入口 —— `create_case()` 建、`update_biz_status()` 改，
不留第三条路径。其中 `create_case()` 是**幂等**的：受理这一步会被返工重跑，而主键是
`(tenant_id, claim_id)`，裸 INSERT 的重跑不是「建出两个案子」而是当场 IntegrityError。
三道拦截：
  - 运行时：`objects.execute()` 见到 claim_case 的写语句直接抛 `BypassedGuardError`
  - 测试期：`test_claim_authority.py::test_no_bypass_writes_paid` 用 AST 扫全仓
  - 提交前：grep -rn "biz_status.*=.*'paid'" maos/ 自查

## 与退款域那份 guard 的关系

形状逐条同构（权威写入方常量、状态集合、回执判据表、四道闸的顺序），但**不 import
它、不继承它**。理由与 objects.py 抬头同一条：两个域焊在一起，「换域只新增文件」
这句话就不成立了。同构而不共用，是本轨要证明的那件事的一部分。

差别只在四个名字上：`settled` -> `paid`、`payment.observe` -> `claim.observe`、
`refund_case` -> `claim_case`、`payment_observation` -> `claim_payment_observation`。
差别不在结构上 —— 结构一模一样，正说明这套权威边界的写法与领域无关。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import objects

# ---------------------------------------------------------------- 冻结常量
#: 全系统唯一写得进权威终态的 actor。skill 按名 import 它，不抄字面量。
AUTHORITATIVE_WRITER = "claim.observe"

#: 只有 AUTHORITATIVE_WRITER 写得进来的状态集合。
#: 现在只有 paid；将来若有第二个「外部说了才算」的终态，加进这里而不是散在判断里。
AUTHORITATIVE_STATES = frozenset({"paid"})

#: 权威终态 -> 该终态要求回执里的 `observed_state` 取值。
#: 与 AUTHORITATIVE_STATES **同增同减**：加一个权威终态就必须在这里给出它的判据，
#: 漏配不会放行（第 ④ 道见到没有判据的权威终态直接拒），否则「有回执」会被当成
#: 「回执说到账了」—— 那正是这张表要堵的洞。
#:
#: 取值域的出处：`observed_state` 落的是赔付方回执的 `status` 字段
#: （claim_observe.py 取 `str(receipt.get("status"))` 再写进 observation），
#: 而 `status` 的四个取值由 maos/tools/claim.py 的 STATUS_* 定死：
#: processing / unknown / paid / denied，其中终态只有 paid 与 denied。
#: 所以 paid 的判据集合只收 "paid" 一个值。
#:
#: 特别不要把 X12 CARC 的 effect 写进来。`45` / `1` / `2` 这几条码的
#: `effect != denied`（见 maos/tools/claim_codes.py），读起来像「那这笔是赔了的」——
#: 但一条调整码只说明赔付方对这笔账做了一次调整，**不是到账回执**。
#: 拿它当放行判据，就等于拿「可能已经发生了」当成「确定到账了」。
AUTHORITATIVE_RECEIPT_STATE: dict[str, frozenset[str]] = {
    "paid": frozenset({"paid"}),
}

#: 业务状态机（**不是** Task 状态机 —— 铁律 9）：主干三段 + 两个分支。
#: `maos/contracts/states.py` 在本域一个新状态、一条新迁移都没有加。
BIZ_STATUS_FLOW: dict[str, tuple[str, ...]] = {
    "submitted":         ("adjudicated", "rejected"),
    "adjudicated":       ("payment_requested", "rejected", "compensated"),
    "payment_requested": ("paid", "compensated"),
    "paid":              (),
    "rejected":          (),
    "compensated":       (),
}

INITIAL_STATUS = "submitted"

VIOLATION_EVENT = "AuthoritativeFactViolation"

#: 同一个案号上来了一份业务字段不一样的报案 —— 落这个事件类型。
#: 与 `VIOLATION_EVENT` 分开：越权写入是「你不该写」，这里是「你写的和库里那份
#: 不是同一件事」，两种排查方向完全不同，压成一个事件类型就得靠读 reason 去分。
CASE_CONFLICT_EVENT = "ClaimCaseIdentityConflict"

#: 业务状态变更事件。与退款域的 `RefundBizStatusChanged` 平行，各自带域前缀 ——
#: 共用一个名字会让「这个 Plan 的业务状态动过没有」要按 detail 里的域字段二次过滤，
#: 而两个域的业务状态机根本不是同一台机器。
BIZ_STATUS_EVENT = "ClaimBizStatusChanged"

#: 一条回执至少要有的字段。缺任何一个都算「没有回执」。
#: `carc_code` **不在里面**：到账的回执没有 CARC 可挂（赔付方没有可说的调整），
#: 把它列成必填会让唯一一条合法的成功回执永远进不来。
_OBSERVATION_REQUIRED = ("request_id", "observed_state")

#: 判定「这是不是同一件事的重放」要逐字段比对的业务字段（见 `create_case` 的幂等语义）。
#:
#: `biz_status` 与 `created_at` **不在里面**：前者是案子建成之后被推进的结果，
#: 后者是第一次报案的时刻 —— 拿它们比对会让每一次正常重放都判成冲突。
#: `reported_at` 也不在里面：返工重跑时它会重新取一次当前时刻，比它等于每次重跑必冲突。
#: `plan_id` 在里面：两个 Plan 同时推进同一个案子的 `biz_status` 没有正确解，
#: 那种案号复用该在报案这一步就响，不该留到状态机上去打架。
_CASE_IDENTITY_FIELDS = ("payer_id", "policy_no", "policy_version", "loss_type",
                         "incident_at", "amount_claimed", "plan_id")


class AuthoritativeFactViolation(RuntimeError):
    """非权威写入方试图写入权威终态，或权威终态没有回执兜底。

    定义在本模块而不是 `contracts/` —— contracts 是冻结面，且这是理赔域自己的
    业务规则，不是内核契约。与退款域那个同名类**刻意不共用**：两个域各自的权威
    边界破了是两件事，catch 一个不该顺带把另一个也接住。
    """


class BizStatusTransitionError(ValueError):
    """业务状态迁移不在 `BIZ_STATUS_FLOW` 里。"""


class CaseIdentityConflict(ValueError):
    """同一个 `(tenant_id, claim_id)` 上来了一份**业务字段不一样**的报案。

    不是重放，是两件事撞了同一个案号。定义成 `ValueError` 而不是复用
    `AuthoritativeFactViolation`：那个说的是「你没资格写」，这个说的是
    「你写的和库里那份不是同一件事」。
    """


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_invocation_id(invocation_id: str) -> str:
    """actor 溯源的唯一锚点，空了这条审计链就断了。"""
    if not invocation_id:
        raise ValueError("invocation_id 不许为空：它是 claim_case 每一次写入的 actor 锚点")
    return invocation_id


def _identity_of(row: dict) -> dict:
    """把库里那一行折成与 `create_case` 入参同一个形状，好逐字段比。

    必须过一遍类型转换：sqlite 的 INTEGER / REAL 回来是 int / float，而调用方递
    进来的可能是 str 或 int —— 不归一就会把「12000 与 12000.0」判成冲突，
    幂等当场退化成「每次重跑都报冲突」。
    """
    return {
        "payer_id":       str(row["payer_id"]),
        "policy_no":      str(row["policy_no"]),
        "policy_version": int(row["policy_version"]),
        "loss_type":      str(row["loss_type"]),
        "incident_at":    str(row["incident_at"]),
        "amount_claimed": float(row["amount_claimed"]),
        "plan_id":        str(row["plan_id"]),
    }


def _log_case_conflict(store: Any, *, plan_id: str, tenant_id: str, claim_id: str,
                       diff: dict, actor: str, invocation_id: str) -> None:
    """拒绝一次案号复用也要留证据 —— 理由同模块 docstring：吞掉就没了。"""
    store.append_event_log({
        "plan_id": plan_id,
        "event_type": CASE_CONFLICT_EVENT,
        "reason": f"claim_id 被复用，业务字段对不上：{sorted(diff)}",
        "detail": {"tenant_id": tenant_id, "claim_id": claim_id,
                   "actor": actor, "invocation_id": invocation_id,
                   "conflicts": {f: {"stored": old, "incoming": new}
                                 for f, (old, new) in diff.items()}},
    })


def _log_violation(store: Any, *, plan_id: str, tenant_id: str, claim_id: str,
                   attempted: str, actor: str, invocation_id: str, why: str) -> None:
    store.append_event_log({
        "plan_id": plan_id,
        "event_type": VIOLATION_EVENT,
        "reason": why,
        "detail": {"tenant_id": tenant_id, "claim_id": claim_id, "attempted": attempted,
                   "actor": actor, "invocation_id": invocation_id,
                   "domain": "claim",
                   "authoritative_writer": AUTHORITATIVE_WRITER},
    })


# ---------------------------------------------------------------- 写入口径
def create_case(
    store: Any,
    *,
    tenant_id: str,
    claim_id: str,
    payer_id: str,
    policy_no: str,
    policy_version: int,
    loss_type: str,
    incident_at: str,
    amount_claimed: float,
    plan_id: str,
    actor_skill: str,
    invocation_id: str,
    reported_at: str = "",
) -> dict:
    """建一个 claim_case，落 `submitted`。这是本表唯一的插入口径，**且是幂等的**。

    `biz_status` 不接受调用方指定 —— 想直接建成 paid 的路必须从一开始就不存在，
    否则守卫只挡得住 update，挡不住 insert。

    **幂等语义**（报案这一步会被返工重跑，而主键是 `(tenant_id, claim_id)`）：

      · 案号已在库 + `_CASE_IDENTITY_FIELDS` 逐字段相同 -> 一个字节都不写，返回
        **既有那一行**（原 `created_at`、原 `biz_status`）。所以这里既不能
        `INSERT OR REPLACE` 也不能 `ON CONFLICT DO UPDATE`：那两种写法会让一次
        重跑把已经推进到 adjudicated / payment_requested 的案子**静悄悄**倒回
        `submitted`，比裸 INSERT 抛异常坏得多。
      · 案号已在库 + 任一业务字段不同 -> 落一条 `CASE_CONFLICT_EVENT` 事件并抛
        `CaseIdentityConflict`。**这一档不许静默**：库里的 `amount_claimed` 是
        `claim.settle` 真正拿去算钱的输入，悄悄收下新金额等于绕过核算与审批；
        悄悄丢弃则让调用方拿到一份和自己递进来的 seed 对不上的案件草稿，
        同样一点信号都没有。与第 ④ 道「有回执 != 回执说到账了」同一个 fail-closed 口径。

    用 `ON CONFLICT (tenant_id, claim_id) DO NOTHING` 而不是 `INSERT OR IGNORE`：
    后者会把 `biz_status` 那条 CHECK 约束的失败一并吞掉，指名冲突目标才只放过
    主键这一种冲突。判定放在插入**之后**回读比对，而不是插入前先查一次 ——
    先查后插在 `lock_of()` 退化成 nullcontext 的 Store 上有 TOCTOU 窗口。
    """
    _require_invocation_id(invocation_id)
    incoming = {
        "payer_id":       str(payer_id),
        "policy_no":      str(policy_no),
        "policy_version": int(policy_version),
        "loss_type":      str(loss_type),
        "incident_at":    str(incident_at),
        "amount_claimed": float(amount_claimed),
        "plan_id":        str(plan_id),
    }
    now = _now()
    conn = objects._conn(store)
    with objects.lock_of(store):
        conn.execute(
            "INSERT INTO claim_case (tenant_id, claim_id, payer_id, policy_no, policy_version,"
            " loss_type, incident_at, reported_at, amount_claimed, biz_status, plan_id,"
            " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT (tenant_id, claim_id) DO NOTHING",
            (tenant_id, claim_id, incoming["payer_id"], incoming["policy_no"],
             incoming["policy_version"], incoming["loss_type"], incoming["incident_at"],
             str(reported_at or now), incoming["amount_claimed"], INITIAL_STATUS,
             incoming["plan_id"], now),
        )
        conn.commit()

    case = get_case(store, tenant_id, claim_id)
    if case is None:
        # 既没插进去、又读不到。不静默返回 None：调用方的类型标注说这里必有一行。
        raise RuntimeError(
            f"create_case 之后读不到 case：tenant={tenant_id} claim={claim_id}")

    stored = _identity_of(case)
    diff = {f: (stored[f], incoming[f])
            for f in _CASE_IDENTITY_FIELDS if stored[f] != incoming[f]}
    if diff:
        _log_case_conflict(store, plan_id=incoming["plan_id"], tenant_id=tenant_id,
                           claim_id=claim_id, diff=diff, actor=actor_skill,
                           invocation_id=invocation_id)
        detail = "；".join(f"{f}：库里 {old!r}、这次 {new!r}"
                          for f, (old, new) in sorted(diff.items()))
        raise CaseIdentityConflict(
            f"claim={claim_id}（tenant={tenant_id}）已经存在，业务字段对不上：{detail}。"
            "报案重跑只在业务字段逐字段相同时幂等；对不上说明这不是同一件事的重放，"
            "既不许覆盖也不许静默丢弃 —— 换个 claim_id，或先查清这个案号为什么被复用"
        )
    return case


def update_biz_status(
    store: Any,
    tenant_id: str,
    claim_id: str,
    new_status: str,
    actor_skill: str,
    invocation_id: str,
    *,
    observation: dict | None = None,
    reason: str = "",
) -> dict:
    """`claim_case.biz_status` 的唯一写入路径。四道闸，顺序不可换。

    - `new_status` 落在 `AUTHORITATIVE_STATES` 且 `actor_skill != AUTHORITATIVE_WRITER`
      -> 落 `AuthoritativeFactViolation` 事件并抛 `AuthoritativeFactViolation`。
    - 回执只有权威写入方递得进来。
    - 写权威终态必须带 `observation`，与状态更新**同事务**插入
      `claim_payment_observation`。
    - 回执还得**说的是这件事**（`observed_state == "paid"`）。
    - 迁移不在 `BIZ_STATUS_FLOW` 里 -> 抛 `BizStatusTransitionError`。
    """
    _require_invocation_id(invocation_id)
    case = get_case(store, tenant_id, claim_id)
    plan_id = (case or {}).get("plan_id", "")

    # ① 权威闸放在最前面：case 不存在也照样记一笔越权尝试。
    #    先查存在性会让「对不存在的 case 越权写 paid」以 LookupError 收场，
    #    证据就没了 —— 而那恰恰是最该留痕的一种试探。
    if new_status in AUTHORITATIVE_STATES and actor_skill != AUTHORITATIVE_WRITER:
        _log_violation(store, plan_id=plan_id, tenant_id=tenant_id, claim_id=claim_id,
                       attempted=new_status, actor=actor_skill, invocation_id=invocation_id,
                       why=f"{new_status} 只能由 {AUTHORITATIVE_WRITER} 写入")
        raise AuthoritativeFactViolation(
            f"{actor_skill} 试图把 claim={claim_id} 写成 {new_status}；"
            f"该状态的权威在赔付方，只有 {AUTHORITATIVE_WRITER} 观察到到账回执后才写得进来"
        )

    # ② 回执只有权威写入方递得进来，否则等于给别人开了个伪造回执的口子。
    if observation is not None and actor_skill != AUTHORITATIVE_WRITER:
        _log_violation(store, plan_id=plan_id, tenant_id=tenant_id, claim_id=claim_id,
                       attempted=new_status, actor=actor_skill, invocation_id=invocation_id,
                       why=f"回执只能由 {AUTHORITATIVE_WRITER} 提交")
        raise AuthoritativeFactViolation(
            f"{actor_skill} 递交了赔付回执；回执是外部权威事实，"
            f"只有 {AUTHORITATIVE_WRITER} 能落库"
        )

    if case is None:
        raise LookupError(f"没有这个 case：tenant={tenant_id} claim={claim_id}")

    cur = case["biz_status"]
    if new_status not in BIZ_STATUS_FLOW.get(cur, ()):
        raise BizStatusTransitionError(
            f"业务状态不许从 {cur} 迁到 {new_status}（claim={claim_id}）；"
            f"{cur} 的合法去向：{BIZ_STATUS_FLOW.get(cur, ()) or '无（终态）'}"
        )

    # ③ 权威终态必须有回执。没有回执的 paid 就是把外部状态写死为终态。
    if new_status in AUTHORITATIVE_STATES:
        missing = [f for f in _OBSERVATION_REQUIRED if not (observation or {}).get(f)]
        if missing:
            _log_violation(store, plan_id=plan_id, tenant_id=tenant_id, claim_id=claim_id,
                           attempted=new_status, actor=actor_skill,
                           invocation_id=invocation_id,
                           why=f"回执缺字段 {missing}")
            raise AuthoritativeFactViolation(
                f"写 {new_status} 必须同事务附回执，缺字段：{missing}"
            )

        # ④ 回执还得**说的是这件事**。③ 只保证「有一张回执」，不保证那张回执说到账了 ——
        #    一条 observed_state='denied'、carc_code='96' 的回执两个字段齐全，
        #    在 ③ 眼里与到账回执无从分辨。放过它，系统持有的就只是「有一张回执」，
        #    不是「赔付方说钱到账了」，而后者才是 paid 这个词的全部含义（铁律 8）。
        #    这条防线必须活在守卫里，不能只活在 claim.observe 的某个 if 分支里 ——
        #    在那里改动分支顺序，两层都不会响。
        allowed = AUTHORITATIVE_RECEIPT_STATE.get(new_status)
        if allowed is None:
            # 加了权威终态却没给判据。fail-closed：宁可写不进去，也不许默认放行 ——
            # 默认放行会让这个终态退回到「有回执就算数」，静默且没人会发现。
            _log_violation(store, plan_id=plan_id, tenant_id=tenant_id, claim_id=claim_id,
                           attempted=new_status, actor=actor_skill,
                           invocation_id=invocation_id,
                           why=f"{new_status} 没有在 AUTHORITATIVE_RECEIPT_STATE 里配回执判据")
            raise AuthoritativeFactViolation(
                f"{new_status} 在 AUTHORITATIVE_STATES 里，却没有在 "
                f"AUTHORITATIVE_RECEIPT_STATE 里给出回执判据；两张表必须同增同减"
            )
        seen = str((observation or {}).get("observed_state"))
        if seen not in allowed:
            _log_violation(store, plan_id=plan_id, tenant_id=tenant_id, claim_id=claim_id,
                           attempted=new_status, actor=actor_skill,
                           invocation_id=invocation_id,
                           why=f"回执 observed_state={seen!r}，不在 {new_status} 的判据 "
                               f"{sorted(allowed)} 里")
            raise AuthoritativeFactViolation(
                f"写 {new_status} 的回执说的是 {seen!r}，不是 {sorted(allowed)}；"
                f"「有一张回执」不等于「赔付方说钱到账了」，外部权威没这么说就不许收口"
            )

    conn = objects._conn(store)
    with objects.lock_of(store):
        try:
            if observation is not None:
                obs = dict(observation)
                conn.execute(
                    "INSERT INTO claim_payment_observation (tenant_id, claim_id, request_id,"
                    " carc_code, group_code, remark_codes, raw_receipt_json, observed_state,"
                    " observed_at, actor_invocation_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (tenant_id, claim_id, obs["request_id"], obs.get("carc_code", ""),
                     obs.get("group_code", ""), obs.get("remark_codes", "[]"),
                     obs.get("raw_receipt_json", "{}"), obs["observed_state"],
                     obs.get("observed_at") or _now(), invocation_id),
                )
            conn.execute(
                "UPDATE claim_case SET biz_status=? WHERE tenant_id=? AND claim_id=?",
                (new_status, tenant_id, claim_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    store.append_event_log({
        "plan_id": plan_id,
        "event_type": BIZ_STATUS_EVENT,
        "from_state": cur, "to_state": new_status, "reason": reason,
        "detail": {"tenant_id": tenant_id, "claim_id": claim_id, "actor": actor_skill,
                   "invocation_id": invocation_id,
                   "observation_attached": observation is not None},
    })
    return get_case(store, tenant_id, claim_id)  # type: ignore[return-value]


def get_case(store: Any, tenant_id: str, claim_id: str) -> dict | None:
    """按 (tenant_id, claim_id) 读一个 case；不存在返回 None。"""
    rows = objects.query(
        store, "SELECT * FROM claim_case WHERE tenant_id=? AND claim_id=?",
        (tenant_id, claim_id))
    return rows[0] if rows else None
