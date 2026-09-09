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

## 控制流下沉到 `maos/domain/_case_guard.py`

四道闸的顺序、fail-closed 姿态、两条审计行的字段、幂等回读比对 —— 这些四个域一字
不差，已经下沉成一份骨架。本模块留下的是**域**：判据表、观察表的 INSERT、
`record_observation()` 这条本域独有的旁路、异常类，以及每道闸对外说的那句人话
（见 `_TEXTS`）。判据表递给骨架的是**取值的函数**而不是值本身 —— 它们是模块级常量，
`test_missing_receipt_criterion_is_fail_closed` 会 monkeypatch 它来演漏配时的姿态，
在 import 那一刻捕获值会让那条测试再也测不到真的判据表。

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

共用一份**骨架**不改变这三条：骨架不知道 ap 是什么，它拿到的是表名、判据表和一组
文案；退款域这一轮压根没接（见 `docs/BACKLOG.md` 的 `## task-T81`）。
`AuthoritativeFactViolation` 这三个异常类因此仍然各定义各的。

`maos/tests/test_ap_guard.py::test_ap_and_refund_guards_are_independent` 把这三条
钉住：同一个 store 里两个域各推进一个案子到 settled，互不干扰，且任一方的
writer 都写不进对方的表。

## 业务状态不进 Task 状态机（铁律 9）

`received` / `matched` / `payment_requested` / `settled` / `rejected` /
`compensated` 全是 `ap_case` 自己的字段。`maos/contracts/states.py` 一个新状态、
一条新迁移都没加，场景的收口断言之一就是这件事。
"""

from __future__ import annotations

from typing import Any

from .. import _case_guard
from .._case_guard import CaseGuardErrors, CaseGuardTexts, _now
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

#: 业务状态变更事件。
BIZ_STATUS_EVENT = "ApBizStatusChanged"

#: 一条回单至少要有的字段。缺任何一个都算「没有回单」。
#: `bank_reference` 在里面 —— 见模块 docstring「比退款域多一条」。
_OBSERVATION_REQUIRED = ("instruction_id", "observed_state", "bank_reference")

#: 判定「这是不是同一件事的重放」要逐字段比对的业务字段，值是该列的**归一函数**。
#:
#: 必须过一遍类型归一：sqlite 的 INTEGER 回来是 int，而调用方递进来的可能是 str；
#: 金额一律过 `objects.money_str` 折成两位小数字符串 —— 不归一就会把
#: 「3200 与 3200.00」判成冲突，幂等当场退化成「每次重跑都报冲突」。
#:
#: `biz_status` 与 `created_at` **不在里面**：前者是案子建成之后被推进的结果，
#: 后者是第一次受理的时刻 —— 拿它们比对会让每一次正常重放都判成冲突。
#: `amount_claimed` 在里面：它是三单匹配拿去比对的输入，悄悄换掉等于把匹配绕过去。
_IDENTITY_COERCERS: dict[str, Any] = {
    "supplier_id":    str,
    "po_id":          str,
    "po_version":     int,
    "invoice_id":     str,
    "gr_id":          str,
    "amount_claimed": objects.money_str,
    "currency":       str,
    "plan_id":        str,
}

#: 与 `_IDENTITY_COERCERS` 同一份，只是取键 —— 两处各写一份必漂，漂了幂等就退化。
_CASE_IDENTITY_FIELDS = tuple(_IDENTITY_COERCERS)


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


# ---------------------------------------------------------------- 域的那部分
#: 四道闸对外说的那句人话。句式由骨架定死，这里只填名词 —— 报错文案是排查的第一
#: 现场，下沉最容易在这里偷偷退化成看不出是哪张表、哪种凭据的通用话。
#: `missing_tail` 是本域独有的那一句：银行流水号管的是「凭据全不全」，
#: 与第 ④ 道管的「内容对不对」是两件事，缺它时得当场说清楚为什么。
_TEXTS = CaseGuardTexts(
    authority_holder="银行",
    evidence_seen_phrase="回单后",
    submitted_noun="银行回单",
    fact_subject="回单",
    receipt_noun="银行回单",
    missing_why_noun="银行回单",
    attach_noun="银行回单",
    intake_verb="收票",
    replay_unit="张发票",
    missing_tail="。其中 bank_reference 是**可对账的凭据**，没有它的「已付」在财务上"
                 "对不了账 —— 「有一张回单」不等于「有一张能拿去对账的回单」",
)


def _write_observation(store: Any, conn: Any, *, tenant_id: str, case_id: str,
                       observation: dict, invocation_id: str) -> None:
    """把银行回单落进 `ap_payment_observation`，用骨架递进来的那条连接。

    连接是骨架的事务里那一条 —— 回单与状态更新同生共死。自己另开一条会让
    「状态推进了但回单没落」变得可表达，而那正是铁律 8 要挡的形状。
    """
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


_GUARD = _case_guard.make_case_guard(
    case_table="ap_case",
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
        receipt_noun="回单",
        authority_says="银行说钱划走了",
    ),
    write_observation=_write_observation,
    #: 本域三条审计行的 detail 都把 `domain` 排在最前（claim 排在 invocation_id 之后，
    #: 差错处理域压根没有）。位置是历史留下的不一致，本轨照抄不统一 ——
    #: 统一会改掉审计行的形状，那是行为变更。
    conflict_detail_domain=DOMAIN,
    violation_detail_domain=DOMAIN,
    event_detail_domain=DOMAIN,
)

_require_invocation_id = _GUARD._require_invocation_id
_identity_of = _GUARD._identity_of
_log_case_conflict = _GUARD._log_case_conflict
_log_violation = _GUARD._log_violation
get_case = _GUARD.get_case


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
        **既有那一行**（原 `created_at`、原 `biz_status`）。
      · 案号已在库 + 任一业务字段不同 → 落一条 `CASE_CONFLICT_EVENT` 事件并抛
        `CaseIdentityConflict`。**这一档不许静默**：悄悄收下新金额，则库里的
        `amount_claimed` 是三单匹配真正拿去比对的输入，等于把匹配绕过去；
        悄悄丢弃，则调用方拿到一份和自己递进来的发票对不上的案子，同样一点信号都没有。

    插入语句的形状（`ON CONFLICT DO NOTHING` 而不是 `INSERT OR IGNORE`、
    回读比对而不是先查后插）由骨架定，理由见 `_case_guard.create_case`。
    """
    # 递进来的这一份与库里回读的那一份过**同一套**归一函数（`_identity_of`）——
    # 两边各归一各的正是「3200 与 3200.00 判成冲突」那个坑的来源。
    incoming = _identity_of({
        "supplier_id":    supplier_id,
        "po_id":          po_id,
        "po_version":     po_version,
        "invoice_id":     invoice_id,
        "gr_id":          gr_id,
        "amount_claimed": amount_claimed,
        "currency":       currency,
        "plan_id":        plan_id,
    })
    return _GUARD.create_case(
        store, tenant_id=tenant_id, case_id=case_id, incoming=incoming,
        columns={
            "supplier_id":    incoming["supplier_id"],
            "po_id":          incoming["po_id"],
            "po_version":     incoming["po_version"],
            "invoice_id":     incoming["invoice_id"],
            "gr_id":          incoming["gr_id"],
            "amount_claimed": incoming["amount_claimed"],
            "currency":       incoming["currency"],
            "biz_status":     INITIAL_STATUS,
            "plan_id":        incoming["plan_id"],
            "created_at":     _now(),
        },
        actor_skill=actor_skill, invocation_id=invocation_id)


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

    写进去的是**观察与推断**，不是权威事实（铁律 8）—— 一笔货款有没有划出去，
    权威在银行。四道闸，顺序不可换（骨架里每一道旁边写了为什么它必须在那个位置）：

    ① `new_status` 落在 `AUTHORITATIVE_STATES` 而 actor 不是权威写入方 → 拒 + 留证据
    ② 递了 `observation` 却不是权威写入方 → 拒（否则等于给别人开伪造回单的口子）
    ③ 权威终态**必须**带回单，且字段齐全（含银行流水号）
    ④ 回单说的**得是这件事**：`observed_state` 必须落在该终态的判据集合里
    """
    return _GUARD.update_biz_status(store, tenant_id, case_id, new_status,
                                    actor_skill, invocation_id,
                                    observation=observation, reason=reason)


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

    本域独有，**不进骨架**：另外三个域的失败留痕各有各的形状（差错处理域走
    `insert_observation`，claim 压根没有这条旁路）。

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


def observations_of(store: Any, tenant_id: str, case_id: str) -> list[dict]:
    return objects.query(
        store, "SELECT * FROM ap_payment_observation WHERE tenant_id=? AND case_id=?"
               " ORDER BY observed_at", (tenant_id, case_id))
