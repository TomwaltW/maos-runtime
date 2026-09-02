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

## 控制流下沉到 `maos/domain/_case_guard.py`

四道闸的顺序、fail-closed 姿态、两条审计行的字段、幂等回读比对 —— 这些四个域一字
不差，已经下沉成一份骨架。本模块留下的是**域**：判据表、观察表的 INSERT、异常类、
以及每道闸对外说的那句人话（见 `_TEXTS`）。

`AUTHORITATIVE_STATES` 这类判据表递给骨架的是**取值的函数**而不是值本身 ——
它们是模块级常量，测试会 monkeypatch 它们来演「漏配判据时 fail-closed」；
在 import 那一刻捕获值会让那条测试再也测不到真的判据表。

## 与退款域那份 guard 的关系

形状逐条同构（权威写入方常量、状态集合、回执判据表、四道闸的顺序），但**不 import
它、不继承它**。理由与 objects.py 抬头同一条：两个域焊在一起，「换域只新增文件」
这句话就不成立了。同构而不共用，是本轨要证明的那件事的一部分。

共用一份**骨架**与共用一份**域实现**不是一回事：骨架不知道 claim 是什么，
它拿到的是表名、判据表、和一组文案；换一个域只要再喂一组参数，不要求两个域彼此认识。
`AuthoritativeFactViolation` 这三个异常类因此仍然各定义各的 —— `except` 一个不该
顺带把另一个也接住。

差别只在四个名字上：`settled` -> `paid`、`payment.observe` -> `claim.observe`、
`refund_case` -> `claim_case`、`payment_observation` -> `claim_payment_observation`。
差别不在结构上 —— 结构一模一样，正说明这套权威边界的写法与领域无关。
"""

from __future__ import annotations

from typing import Any

from .. import _case_guard
from .._case_guard import CaseGuardErrors, CaseGuardTexts, _now
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

#: 判定「这是不是同一件事的重放」要逐字段比对的业务字段（见 `create_case` 的幂等语义），
#: 值是该列的**归一函数** —— sqlite 的 INTEGER / REAL 回来是 int / float，而调用方递
#: 进来的可能是 str 或 int，不归一就会把「12000 与 12000.0」判成冲突。
#:
#: `biz_status` 与 `created_at` **不在里面**：前者是案子建成之后被推进的结果，
#: 后者是第一次报案的时刻 —— 拿它们比对会让每一次正常重放都判成冲突。
#: `reported_at` 也不在里面：返工重跑时它会重新取一次当前时刻，比它等于每次重跑必冲突。
#: `plan_id` 在里面：两个 Plan 同时推进同一个案子的 `biz_status` 没有正确解，
#: 那种案号复用该在报案这一步就响，不该留到状态机上去打架。
_IDENTITY_COERCERS: dict[str, Any] = {
    "payer_id":       str,
    "policy_no":      str,
    "policy_version": int,
    "loss_type":      str,
    "incident_at":    str,
    "amount_claimed": float,
    "plan_id":        str,
}

#: 与 `_IDENTITY_COERCERS` 同一份，只是取键 —— 两处各写一份必漂，漂了幂等就退化。
_CASE_IDENTITY_FIELDS = tuple(_IDENTITY_COERCERS)


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


# ---------------------------------------------------------------- 域的那部分
#: 四道闸对外说的那句人话。句式由骨架定死，这里只填名词 —— 报错文案是排查的第一
#: 现场，下沉最容易在这里偷偷退化成「本域的写入口径」这种看不出是哪张表的通用话。
_TEXTS = CaseGuardTexts(
    authority_holder="赔付方",
    evidence_seen_phrase="到账回执后",
    submitted_noun="赔付回执",
    fact_subject="回执",
    receipt_noun="回执",
    missing_why_noun="回执",
    attach_noun="回执",
    intake_verb="报案",
    replay_unit="件事",
)


def _write_observation(store: Any, conn: Any, *, tenant_id: str, claim_id: str,
                       observation: dict, invocation_id: str) -> None:
    """把回执落进 `claim_payment_observation`，用骨架递进来的那条连接。

    连接是骨架的事务里那一条 —— 观察与状态更新同生共死。自己另开一条会让
    「状态推进了但回执没落」变得可表达，而那正是铁律 8 要挡的形状。
    """
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


_GUARD = _case_guard.make_case_guard(
    case_table="claim_case",
    case_key="claim_id",
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
    check_evidence=_case_guard.make_receipt_state_gate(
        receipt_state=lambda: AUTHORITATIVE_RECEIPT_STATE,
        violation_error=AuthoritativeFactViolation,
        receipt_noun="回执",
        authority_says="赔付方说钱到账了",
    ),
    write_observation=lambda store, conn, *, tenant_id, case_id, observation, invocation_id:
        _write_observation(store, conn, tenant_id=tenant_id, claim_id=case_id,
                           observation=observation, invocation_id=invocation_id),
    #: 本域的违规事件 detail 里 `domain` 排在 `invocation_id` 之后（ap 排在最前）。
    #: 位置是历史留下的不一致，本轨照抄不统一 —— 统一会改掉审计行的形状。
    violation_detail_domain="claim",
    violation_detail_domain_first=False,
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
        **既有那一行**（原 `created_at`、原 `biz_status`）。
      · 案号已在库 + 任一业务字段不同 -> 落一条 `CASE_CONFLICT_EVENT` 事件并抛
        `CaseIdentityConflict`。**这一档不许静默**：库里的 `amount_claimed` 是
        `claim.settle` 真正拿去算钱的输入，悄悄收下新金额等于绕过核算与审批；
        悄悄丢弃则让调用方拿到一份和自己递进来的 seed 对不上的案件草稿，
        同样一点信号都没有。与第 ④ 道「有回执 != 回执说到账了」同一个 fail-closed 口径。

    插入语句的形状（`ON CONFLICT DO NOTHING` 而不是 `INSERT OR IGNORE`、
    回读比对而不是先查后插）由骨架定，理由见 `_case_guard.create_case`。
    """
    # 递进来的这一份与库里回读的那一份过**同一套**归一函数（`_identity_of`）——
    # 两边各归一各的正是「12000 与 12000.0 判成冲突」那个坑的来源。
    incoming = _identity_of({
        "payer_id":       payer_id,
        "policy_no":      policy_no,
        "policy_version": policy_version,
        "loss_type":      loss_type,
        "incident_at":    incident_at,
        "amount_claimed": amount_claimed,
        "plan_id":        plan_id,
    })
    now = _now()
    return _GUARD.create_case(
        store, tenant_id=tenant_id, case_id=claim_id, incoming=incoming,
        columns={
            "payer_id":       incoming["payer_id"],
            "policy_no":      incoming["policy_no"],
            "policy_version": incoming["policy_version"],
            "loss_type":      incoming["loss_type"],
            "incident_at":    incoming["incident_at"],
            "reported_at":    str(reported_at or now),
            "amount_claimed": incoming["amount_claimed"],
            "biz_status":     INITIAL_STATUS,
            "plan_id":        incoming["plan_id"],
            "created_at":     now,
        },
        actor_skill=actor_skill, invocation_id=invocation_id)


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

    写进去的是**观察与推断**，不是权威事实（铁律 8）—— 一笔赔款到没到账，权威在
    赔付方。所以：

    - `new_status` 落在 `AUTHORITATIVE_STATES` 且 `actor_skill != AUTHORITATIVE_WRITER`
      -> 落 `AuthoritativeFactViolation` 事件并抛 `AuthoritativeFactViolation`。
    - 回执只有权威写入方递得进来。
    - 写权威终态必须带 `observation`，与状态更新**同事务**插入
      `claim_payment_observation`。
    - 回执还得**说的是这件事**（`observed_state == "paid"`）—— 这一道必须活在守卫里，
      不能只活在 `claim.observe` 的某个 if 分支里：在那里改动分支顺序，两层都不会响。
    - 迁移不在 `BIZ_STATUS_FLOW` 里 -> 抛 `BizStatusTransitionError`。
    """
    return _GUARD.update_biz_status(store, tenant_id, claim_id, new_status,
                                    actor_skill, invocation_id,
                                    observation=observation, reason=reason)
