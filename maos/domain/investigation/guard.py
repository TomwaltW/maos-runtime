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

## 控制流下沉到 `maos/domain/_case_guard.py`

四道闸的顺序、fail-closed 姿态、两条审计行的字段、幂等回读比对 —— 这些四个域一字
不差，已经下沉成一份骨架。本模块留下的是**域**：`message_family()` 的族归一、
`set_classification()`、`insert_observation()`、判据表，以及第 ④ 道
（`_check_evidence()`）—— 它是四个域里唯一结构不同的一道闸，别的域只看
`observed_state`，本域还要看报文族、退回金额、退回原因码，所以它**不进骨架**。

`AUTHORITATIVE_STATES` 这类判据表递给骨架的是**取值的函数**而不是值本身 ——
`test_unconfigured_authoritative_state_is_fail_closed` 会 monkeypatch 它来演漏配时的
姿态，在 import 那一刻捕获值会让那条测试再也测不到真的判据表。

## 越权写入不静默失败

抛 `AuthoritativeFactViolation` + 落一条事件。理由与 `scripts/` 下那个 Bash 守卫同：
「系统拒绝了一次越权写入」本身就是要拿给评委看的证据，吞掉就没了。

`investigation_case` 的一切写入只有两个入口 —— `create_case()` 建、
`update_biz_status()` 改，不留第三条路径。两道拦截：
  - 运行时：`objects.execute()` 见到 investigation_case 的写语句直接抛 `BypassedGuardError`
  - 提交前：grep 自查（见 `maos/tests/test_investigation_guard.py::test_no_bypass_path`）
"""

from __future__ import annotations

from typing import Any

from .. import _case_guard
from .._case_guard import CaseGuardErrors, CaseGuardTexts, EvidenceContext, _now
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

#: 业务状态变更事件。`set_classification()` 与 `update_biz_status()` 都落它 ——
#: 两处各写一份字面量就会漂，而漂的症状是「这个案子的业务状态动过没有」漏查一处。
BIZ_STATUS_EVENT = "InvestigationBizStatusChanged"

#: 一条观察至少要有的字段。缺任何一个都算「没有观察」。
_OBSERVATION_REQUIRED = ("request_id", "message_type", "observed_state")

#: 判定「这是不是同一件事的重放」要逐字段比对的业务字段，值是该列的**归一函数**。
#:
#: 必须过一遍类型转换：sqlite 的 INTEGER / REAL 回来是 int / float，而调用方递
#: 进来的可能是 str 或 int —— 不归一就会把「12500 与 12500.0」判成冲突，
#: 幂等当场退化成「每次重跑都报冲突」（退款域踩过，见其 `_identity_of`）。
#:
#: `biz_status`、`created_at`、`cancellation_reason_code` **不在里面**：
#: 前两个是案子建成之后被推进的结果与第一次受理的时刻，第三个要等 classify 才有值 ——
#: 拿它们比对会让每一次正常重放都判成冲突。
_IDENTITY_COERCERS: dict[str, Any] = {
    "creator_agent":    str,
    "assignee_agent":   str,
    "original_msg_id":  str,
    "original_version": int,
    "end_to_end_id":    str,
    "amount":           float,
    "currency":         str,
    "plan_id":          str,
}

#: 与 `_IDENTITY_COERCERS` 同一份，只是取键 —— 两处各写一份必漂，漂了幂等就退化。
_CASE_IDENTITY_FIELDS = tuple(_IDENTITY_COERCERS)


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


def message_family(message_type: str) -> str:
    """把 `camt.029.001.08` 归一成 `camt.029`。

    判据按**族**而不是按具体版本：ISO 的报文版本号每年都在涨（camt.029 从 001.08
    到 001.11 都在用），按全名硬比会让换一版报文就把守卫判穿 —— 而那正是
    「规范会改版」最常见的落地方式。
    """
    parts = str(message_type or "").split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else str(message_type or "")


# ---------------------------------------------------------------- 域的那部分
#: 四道闸对外说的那句人话。句式由骨架定死，这里只填名词 —— 报错文案是排查的第一
#: 现场，下沉最容易在这里偷偷退化成看不出是哪种报文的通用话。
#: `evidence_seen_phrase` 里点名 pacs.004：本域的招牌判据就是「答复肯定 != 钱回来了」，
#: 拒绝的时候不说清楚认哪种报文，排查的人会以为是权限问题。
_TEXTS = CaseGuardTexts(
    authority_holder="清算方",
    evidence_seen_phrase=f" {MSG_PAYMENT_RETURN} 退款报文之后",
    submitted_noun="清算方决议观察",
    fact_subject="那",
    receipt_noun="决议观察",
    missing_why_noun="观察",
    attach_noun="决议观察",
    intake_verb="受理",
    replay_unit="件事",
)


def _check_evidence(ctx: EvidenceContext) -> None:
    """第 ④ 道：观察还得**是那种报文**。本域独有，不走骨架那份「只看状态」的通用闸。

    ③ 只保证「有一条观察」，不保证那条观察证明了资金。一条 camt.029 / CNCL
    （CancelledAsPerRequest）三个字段齐全，在 ③ 眼里与 pacs.004 无从分辨 ——
    而它说的是「撤销指令照办了」，不是「钱回来了」。放过它，系统持有的就只是
    「清算方确认撤销了」，不是「资金已退回」，而后者才是 returned 这个词的全部含义
    （铁律 8）。所以这一道比 ap / claim 那份多看三样：报文族、退回金额、退回原因码。
    """
    ev = AUTHORITATIVE_EVIDENCE.get(ctx.new_status)
    if ev is None:
        # 加了权威终态却没给证据判据。fail-closed：宁可写不进去，也不许默认放行 ——
        # 默认放行会让这个终态退回到「有观察就算数」，静默且没人会发现。
        ctx.log_violation(
            f"{ctx.new_status} 没有在 AUTHORITATIVE_EVIDENCE 里配证据判据")
        raise AuthoritativeFactViolation(
            f"{ctx.new_status} 在 AUTHORITATIVE_STATES 里，却没有在 "
            f"AUTHORITATIVE_EVIDENCE 里给出证据判据；两张表必须同增同减")

    obs = ctx.observation or {}
    family = message_family(str(obs.get("message_type")))
    if family != ev.message_family:
        ctx.log_violation(f"观察报文是 {family or '(空)'}，"
                          f"而 {ctx.new_status} 只认 {ev.message_family}")
        raise AuthoritativeFactViolation(
            f"写 {ctx.new_status} 的观察来自 {family or '(空)'} 报文，不是 "
            f"{ev.message_family}；{MSG_RESOLUTION} 答的是「撤销请求怎么处理的」，"
            f"只有 {MSG_PAYMENT_RETURN} 答「钱退回来了」—— "
            f"确认撤销不等于资金已退回，外部权威没这么说就不许收口")

    seen = str(obs.get("observed_state"))
    if seen not in ev.observed_states:
        ctx.log_violation(f"观察 observed_state={seen!r}，不在 {ctx.new_status} 的判据 "
                          f"{sorted(ev.observed_states)} 里")
        raise AuthoritativeFactViolation(
            f"写 {ctx.new_status} 的观察说的是 {seen!r}，不是 "
            f"{sorted(ev.observed_states)}；「有一条观察」不等于「清算方说钱回来了」")

    if ev.requires_amount and obs.get("returned_amount") in (None, ""):
        ctx.log_violation(f"{ev.message_family} 观察没有退回金额")
        raise AuthoritativeFactViolation(
            f"写 {ctx.new_status} 的 {ev.message_family} 观察没有 returned_amount；"
            "一份不说退了多少钱的退款报文证明不了资金已退回")

    if ev.requires_code and not str(obs.get(ev.requires_code) or "").strip():
        ctx.log_violation(f"{ev.message_family} 观察缺 {ev.requires_code}")
        raise AuthoritativeFactViolation(
            f"写 {ctx.new_status} 的 {ev.message_family} 观察缺 {ev.requires_code}；"
            "ISO 20022 规定退款报文必带退回原因码，没有它这份观察不可核对")


def _write_observation(store: Any, conn: Any, *, tenant_id: str, case_id: str,
                       observation: dict, invocation_id: str) -> None:
    """把观察落进 `resolution_observation`，用骨架递进来的那条连接。

    连接是骨架的事务里那一条 —— 观察与状态更新同生共死。走本域自己的
    `insert_observation()` 而不是直接拼 SQL：`observed_state` 的封闭取值域校验在
    那里，绕过它等于给「各处自造取值」开了个口子。
    """
    insert_observation(store, tenant_id=tenant_id, case_id=case_id,
                       observation=observation, invocation_id=invocation_id, _conn=conn)


_GUARD = _case_guard.make_case_guard(
    case_table="investigation_case",
    objects_mod=objects,
    identity_fields=_IDENTITY_COERCERS,
    authoritative_writer=AUTHORITATIVE_WRITER,
    authoritative_states=lambda: AUTHORITATIVE_STATES,
    biz_status_flow=lambda: BIZ_STATUS_FLOW,
    observation_required=_OBSERVATION_REQUIRED,
    conflict_event=CASE_CONFLICT_EVENT,
    violation_event=VIOLATION_EVENT,
    biz_status_event=BIZ_STATUS_EVENT,
    errors=CaseGuardErrors(violation=AuthoritativeFactViolation,
                           transition=BizStatusTransitionError,
                           conflict=CaseIdentityConflict),
    texts=_TEXTS,
    check_evidence=_check_evidence,
    write_observation=_write_observation,
    #: 状态变更事件多带一个 `message_type`：本域「是哪种报文推的这一跳」是要拿去
    #: 复盘的第一现场，另外两个域没有这个维度。
    event_detail_extra=lambda obs: {"message_type": (obs or {}).get("message_type", "")},
)

_require_invocation_id = _GUARD._require_invocation_id
_identity_of = _GUARD._identity_of
_log_case_conflict = _GUARD._log_case_conflict
_log_violation = _GUARD._log_violation
get_case = _GUARD.get_case


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

    插入语句的形状（`ON CONFLICT DO NOTHING` 而不是 `INSERT OR IGNORE`、回读比对而
    不是先查后插）由骨架定，理由见 `_case_guard.create_case`。
    """
    # 递进来的这一份与库里回读的那一份过**同一套**归一函数（`_identity_of`）——
    # 两边各归一各的正是「12500 与 12500.0 判成冲突」那个坑的来源。
    incoming = _identity_of({
        "creator_agent":    creator_agent,
        "assignee_agent":   assignee_agent,
        "original_msg_id":  original_msg_id,
        "original_version": original_version,
        "end_to_end_id":    end_to_end_id,
        "amount":           amount,
        "currency":         currency,
        "plan_id":          plan_id,
    })
    return _GUARD.create_case(
        store, tenant_id=tenant_id, case_id=case_id, incoming=incoming,
        columns={
            "creator_agent":            incoming["creator_agent"],
            "assignee_agent":           incoming["assignee_agent"],
            "original_msg_id":          incoming["original_msg_id"],
            "original_version":         incoming["original_version"],
            "end_to_end_id":            incoming["end_to_end_id"],
            "amount":                   incoming["amount"],
            "currency":                 incoming["currency"],
            # 定性之前没有撤销原因码。空串而不是 NULL：`set_classification()` 是它
            # 唯一的写入口径，「还没定性」和「定性成了空」得在库里长得不一样。
            "cancellation_reason_code": "",
            "biz_status":               INITIAL_STATUS,
            "plan_id":                  incoming["plan_id"],
            "created_at":               _now(),
        },
        actor_skill=actor_skill, invocation_id=invocation_id)


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
        "event_type": BIZ_STATUS_EVENT,
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

    写进去的是**观察与推断**，不是权威事实（铁律 8）—— 一笔钱有没有退回来，权威在
    清算方。四道闸在骨架里，本域给的是第 ④ 道（`_check_evidence`）与文案：

    - `new_status` 落在 `AUTHORITATIVE_STATES` 且 `actor_skill != AUTHORITATIVE_WRITER`
      → 落 `AuthoritativeFactViolation` 事件并抛 `AuthoritativeFactViolation`。
    - 写权威终态必须带 `observation`，与状态更新**同事务**插入 `resolution_observation`。
    - 观察还必须是**对的那种报文**：`returned` 只认 pacs.004，见 `_check_evidence`。
    - 迁移不在 `BIZ_STATUS_FLOW` 里 → 抛 `BizStatusTransitionError`。
    """
    return _GUARD.update_biz_status(store, tenant_id, case_id, new_status,
                                    actor_skill, invocation_id,
                                    observation=observation, reason=reason)


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
