"""案件守卫骨架 —— 四个业务域那套同构的「权威事实边界」下沉成一份。

## 这份文件解决的问题

`ap` / `claim` / `investigation` / `refund` 四个域各自写了一遍同一套守卫：

    _now  _require_invocation_id  _identity_of  _log_case_conflict  _log_violation
    create_case  update_biz_status  get_case

逐行 diff 下来，差异只有**表名字符串**与**文案里的域名词**（回执 / 回单 / 决议观察），
控制流一字不差。四份各改各的，意味着任何一次守卫加固都要抄四遍，抄漏一遍就是
一条静默放行的旁路 —— 而这恰恰是本骨架要堵的那种洞。

所以这里只下沉**控制流与审计行**，域的东西留在域里：

  · 留在骨架：四道闸的顺序、fail-closed 姿态、两条审计行的字段、幂等回读比对
  · 留在各域：判据表（`AUTHORITATIVE_RECEIPT_STATE` / `AUTHORITATIVE_EVIDENCE`）、
    观察表的 INSERT、异常类、以及每道闸对外说的那句人话

## 铁律 8 在这里，不在别处

**MAOS 不持有权威事实，只持有观察与推断。订单、支付、库存的权威状态永远归属
外部系统。** `update_biz_status()` 写进库里的那个 `biz_status`，是本系统**观察到
并推断出**的状态，不是外部世界的权威事实。没有回执兜底的终态就是「把外部状态
直接写死为终态」，那是 bug 不是功能。

这段话原样从四份实现的 `update_biz_status` 里搬过来，**不许因为下沉了就删成一句
泛泛的「更新状态」** —— 它是铁律 8 在代码里唯一的现场提示，删了下一个人就会把
外部状态写死成终态。

## 异常类不共用

各域的 `AuthoritativeFactViolation` / `BizStatusTransitionError` /
`CaseIdentityConflict` 仍然各定义各的，由 `CaseGuardErrors` 递进来。理由见各域
guard 的模块 docstring：两个域的权威边界破了是两件事，`except` 一个不该顺带把
另一个也接住。骨架只借它们的构造器抛，不定义、不合并。

## 与存储骨架（T80 `_case_store`）的关系

本模块**刻意不 import** `maos/domain/_case_store.py`。两轨同期并行，只对形状负责、
不对代码负责（见 `review/domain-slim-contracts.md` §5）。这里的 `_CaseStore` 是按
契约 §1.1 的方法名写的一份**临时薄封装**，整合轮要把它换成 T80 那份真的。
见 `docs/BACKLOG.md` 的 `## task-T81`。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _CaseStore:
    """§1.1 `CaseStore` 的**临时薄封装**（整合轮换成 T80 的 `_case_store`）。

    方法名逐字对齐契约 §1.1，好让整合轮换实现时调用点一个字都不用改。
    只封装守卫真正用得到的三个：读、锁、拿连接。

    `conn()` 不在 §1.1 的清单里，但守卫离不开它 —— 「观察与状态更新同事务」这条
    要求必须拿到裸连接才做得到（`execute()` 是一句一提交）。整合轮要么给 §1.1 补
    这一条，要么给 CaseStore 加一个「同事务多写」的原语；本轨不替它定，只记进 BACKLOG。
    """

    __slots__ = ("_objects",)

    def __init__(self, objects_mod: Any) -> None:
        self._objects = objects_mod

    def query(self, store: Any, sql: str, params: tuple = ()) -> list[dict]:
        return self._objects.query(store, sql, params)

    def lock_of(self, store: Any) -> Any:
        return self._objects.lock_of(store)

    def conn(self, store: Any) -> Any:
        return self._objects._conn(store)


@dataclass(frozen=True)
class CaseGuardErrors:
    """域自己的三个异常类。骨架只拿来抛，不定义、不共用（见模块 docstring）。"""

    violation: type[Exception]      #: 越权写入 / 证据不合格
    transition: type[Exception]     #: 业务状态迁移不在流转表里
    conflict: type[Exception]       #: 同一案号上来了业务字段不一样的受理


@dataclass(frozen=True)
class CaseGuardTexts:
    """各道闸对外说的那句人话。**只有名词不同，句式由骨架定死。**

    为什么把它们抽成参数而不是让骨架说一句通用话：报错文案是排查的第一现场。
    「写不进去」和「写 settled 的回单说的是 accepted，不是 settled」之间差的是
    半小时排查时间。下沉最容易在这里偷偷退化成通用话 —— 所以每一句都留在域里。
    """

    authority_holder: str        #: ① 「该状态的权威在___」：银行 / 赔付方 / 清算方
    evidence_seen_phrase: str    #: ① 「只有 W 观察到___才写得进来」
    submitted_noun: str          #: ② 「actor 递交了___」
    fact_subject: str            #: ② 「___是外部权威事实」
    receipt_noun: str            #: ② why 「___只能由 W 提交」
    missing_why_noun: str        #: ③ why 「___缺字段 [...]」
    attach_noun: str             #: ③ 「必须同事务附___」
    intake_verb: str             #: 冲突文案「___重跑只在业务字段逐字段相同时幂等」
    replay_unit: str             #: 冲突文案「这不是同一___的重放」
    missing_tail: str = ""       #: ③ 之后再补的一句（只有 ap 的银行流水号有）


@dataclass(frozen=True)
class EvidenceContext:
    """第 ④ 道（「这份证据说的是不是这件事」）拿到的现场。

    ④ 是唯一一道**域与域之间结构不同**的闸：ap / claim 只看 `observed_state`，
    investigation 还要看报文族、退回金额、退回原因码。所以它不进骨架，由各域给一个
    `check_evidence`，骨架把现场递进去。
    """

    store: Any
    plan_id: str
    tenant_id: str
    case_id: str
    new_status: str
    actor_skill: str
    invocation_id: str
    observation: dict | None
    #: 落一条 `_log_violation`（骨架已经把 plan_id / case 键名这些绑好了）。
    #: 拒之前必须先调它 —— 「系统拒绝了一次越权写入」本身就是证据，吞掉就没了。
    log_violation: Callable[[str], None]


@dataclass(frozen=True)
class CaseGuard:
    """`make_case_guard()` 的产物。属性名逐字对齐四份现有实现的函数名。"""

    create_case: Callable[..., dict]
    update_biz_status: Callable[..., dict]
    get_case: Callable[..., dict | None]
    _require_invocation_id: Callable[[str], str]
    _identity_of: Callable[[dict], dict]
    _log_case_conflict: Callable[..., None]
    _log_violation: Callable[..., None]


def make_receipt_state_gate(
    *,
    receipt_state: Callable[[], Mapping[str, frozenset[str]]],
    violation_error: type[Exception],
    receipt_noun: str,
    authority_says: str,
) -> Callable[[EvidenceContext], None]:
    """第 ④ 道的「只看 observed_state」版本 —— ap 与 claim 共用这一份。

    ③ 只保证「有一张回执」，不保证那张回执说钱走了。一条 `observed_state='accepted'`
    的受理回单字段齐全，在 ③ 眼里与终态回单无从分辨。放过它，系统持有的就只是
    「外部收下了指令」，不是「外部说钱划走了」，而后者才是那个终态的全部含义（铁律 8）。
    这条防线必须活在守卫里，不能只活在某个 skill 的 `if status == ...` 分支里 ——
    在那里改一次分支顺序，两层都不会响。

    `receipt_state` 传的是**取值的函数**不是值本身：判据表是域的模块级常量，测试会
    monkeypatch 它来演「漏配判据时 fail-closed」。在这里捕获值等于把表冻在 import
    那一刻，那条测试就再也测不到真的判据表了。
    """

    def check(ctx: EvidenceContext) -> None:
        allowed = receipt_state().get(ctx.new_status)
        if allowed is None:
            # 加了权威终态却没给判据。fail-closed：宁可写不进去，也不许默认放行 ——
            # 默认放行会让这个终态退回到「有回执就算数」，静默且没人会发现。
            ctx.log_violation(
                f"{ctx.new_status} 没有在 AUTHORITATIVE_RECEIPT_STATE 里配{receipt_noun}判据")
            raise violation_error(
                f"{ctx.new_status} 在 AUTHORITATIVE_STATES 里，却没有在 "
                f"AUTHORITATIVE_RECEIPT_STATE 里给出{receipt_noun}判据；两张表必须同增同减"
            )
        seen = str((ctx.observation or {}).get("observed_state"))
        if seen not in allowed:
            ctx.log_violation(
                f"{receipt_noun} observed_state={seen!r}，不在 {ctx.new_status} 的判据 "
                f"{sorted(allowed)} 里")
            raise violation_error(
                f"写 {ctx.new_status} 的{receipt_noun}说的是 {seen!r}，不是 {sorted(allowed)}；"
                f"「有一张{receipt_noun}」不等于「{authority_says}」，外部权威没这么说就不许收口"
            )

    return check


def make_case_guard(
    *,
    case_table: str,
    objects_mod: Any,
    identity_fields: Mapping[str, Callable[[Any], Any]],
    authoritative_writer: str,
    authoritative_states: Callable[[], frozenset[str]],
    biz_status_flow: Callable[[], Mapping[str, tuple[str, ...]]],
    observation_required: tuple[str, ...],
    conflict_event: str,
    violation_event: str,
    biz_status_event: str,
    errors: CaseGuardErrors,
    texts: CaseGuardTexts,
    check_evidence: Callable[[EvidenceContext], None],
    write_observation: Callable[..., None],
    case_key: str = "case_id",
    event_detail_extra: Callable[[dict | None], dict] | None = None,
    conflict_detail_domain: str = "",
    violation_detail_domain: str = "",
    violation_detail_domain_first: bool = True,
    event_detail_domain: str = "",
) -> CaseGuard:
    """造一份案件守卫。

    参数分三类：

    **形状**：`case_table` / `case_key` / `identity_fields` / `objects_mod` ——
    这张表叫什么、案号那一列叫什么、哪些列参与幂等比对（值是归一函数，
    sqlite 的 INTEGER/REAL 回来是 int/float，不归一会把「12000 与 12000.0」判成冲突）。

    **判据**：`authoritative_*` / `biz_status_flow` / `observation_required` /
    `check_evidence`。前三个传的是**取值的函数**，不是值 —— 见
    `make_receipt_state_gate` 里的同一条理由：域的模块级常量会被测试 monkeypatch。

    **留痕**：三个事件类型名，以及 `*_detail_domain` 那几个。后者存在纯粹是为了
    **不改现有审计行**（红线 R1）：三个域的 `detail` 里 `domain` 这个键有的有、
    有的没有、位置还不一样（ap 在最前，claim 在 `invocation_id` 之后，
    investigation 压根没有）。骨架照抄这份不一致，不当场统一 —— 统一会改掉审计行的
    形状，那是行为变更，`scripts/verify.py` 那边读得到。已记 `docs/BACKLOG.md`。
    """
    st = _CaseStore(objects_mod)
    #: 报错里管案子叫什么：`claim_id` -> claim，`case_id` -> case。
    case_noun = case_key[:-3] if case_key.endswith("_id") else case_key
    extra_detail = event_detail_extra or (lambda _obs: {})

    def _require_invocation_id(invocation_id: str) -> str:
        """actor 溯源的唯一锚点，空了这条审计链就断了。

        **fail-closed，不许降级**：拿不到就抛 `ValueError`，不落 warning、不给默认值。
        兜底成空字符串等于让每一行审计都指不回是哪一次调用写的 —— 那时守卫还在，
        证据链已经断了，而且断得静默。
        """
        if not invocation_id:
            raise ValueError(
                f"invocation_id 不许为空：它是 {case_table} 每一次写入的 actor 锚点")
        return invocation_id

    def _identity_of(row: dict) -> dict:
        """把库里那一行折成与 `create_case` 入参同一个形状，好逐字段比。

        必须过一遍类型归一（`identity_fields` 的值就是每一列的归一函数）：sqlite 的
        INTEGER / REAL 回来是 int / float，而调用方递进来的可能是 str 或 int ——
        不归一就会把「12000 与 12000.0」判成冲突，幂等当场退化成「每次重跑都报冲突」。
        """
        return {f: coerce(row[f]) for f, coerce in identity_fields.items()}

    def _log_case_conflict(store: Any, *, plan_id: str, tenant_id: str, case_id: str,
                           diff: dict, actor: str, invocation_id: str) -> None:
        """拒绝一次案号复用也要留证据 —— 吞掉就没了。"""
        detail: dict[str, Any] = {}
        if conflict_detail_domain:
            detail["domain"] = conflict_detail_domain
        detail["tenant_id"] = tenant_id
        detail[case_key] = case_id
        detail["actor"] = actor
        detail["invocation_id"] = invocation_id
        detail["conflicts"] = {f: {"stored": old, "incoming": new}
                               for f, (old, new) in diff.items()}
        store.append_event_log({
            "plan_id": plan_id,
            "event_type": conflict_event,
            "reason": f"{case_key} 被复用，业务字段对不上：{sorted(diff)}",
            "detail": detail,
        })

    def _log_violation(store: Any, *, plan_id: str, tenant_id: str, case_id: str,
                       attempted: str, actor: str, invocation_id: str, why: str) -> None:
        detail: dict[str, Any] = {}
        if violation_detail_domain and violation_detail_domain_first:
            detail["domain"] = violation_detail_domain
        detail["tenant_id"] = tenant_id
        detail[case_key] = case_id
        detail["attempted"] = attempted
        detail["actor"] = actor
        detail["invocation_id"] = invocation_id
        if violation_detail_domain and not violation_detail_domain_first:
            detail["domain"] = violation_detail_domain
        detail["authoritative_writer"] = authoritative_writer
        store.append_event_log({
            "plan_id": plan_id,
            "event_type": violation_event,
            "reason": why,
            "detail": detail,
        })

    def get_case(store: Any, tenant_id: str, case_id: str) -> dict | None:
        """按 (tenant_id, <案号>) 读一个 case；不存在返回 None。"""
        rows = st.query(
            store, f"SELECT * FROM {case_table} WHERE tenant_id=? AND {case_key}=?",
            (tenant_id, case_id))
        return rows[0] if rows else None

    def create_case(store: Any, *, tenant_id: str, case_id: str, incoming: dict,
                    columns: dict, actor_skill: str, invocation_id: str) -> dict:
        """建一个 case。本表唯一的插入口径，**且是幂等的**。

        `incoming` 是已归一的业务字段（幂等比对用的那一份），`columns` 是 INSERT 里
        `tenant_id` / 案号之后的每一列，**按顺序**给值 —— 各域的列不同（claim 多一个
        `reported_at`，investigation 多一个 `cancellation_reason_code`），所以这一份由
        域自己拼，骨架只负责语句形状与之后的比对。

        `biz_status` 由域在 `columns` 里写成初始状态，不接受调用方指定 —— 想直接建成
        终态的路必须从一开始就不存在，否则守卫只挡得住 update，挡不住 insert。

        **幂等语义**（受理这一步会被返工重跑，而主键是 `(tenant_id, <案号>)`）：

          · 案号已在库 + `identity_fields` 逐字段相同 -> 一个字节都不写，返回**既有
            那一行**（原 `created_at`、原 `biz_status`）。所以这里既不能
            `INSERT OR REPLACE` 也不能 `ON CONFLICT DO UPDATE`：那两种写法会让一次
            重跑把已经推进到中段的案子**静悄悄**倒回初始状态，比裸 INSERT 抛异常坏得多。
          · 案号已在库 + 任一业务字段不同 -> 落一条冲突事件并抛 `errors.conflict`。
            **这一档不许静默**：悄悄收下新值等于绕过后续拿它当输入的核算；悄悄丢弃
            则让调用方拿到一份和自己递进来的 seed 对不上的案件草稿，同样一点信号都没有。

        用 `ON CONFLICT (tenant_id, <案号>) DO NOTHING` 而不是 `INSERT OR IGNORE`：
        后者会把 `biz_status` 那条 CHECK 约束的失败一并吞掉，指名冲突目标才只放过
        主键这一种冲突。判定放在插入**之后**回读比对，而不是插入前先查一次 ——
        先查后插在 `lock_of()` 退化成 nullcontext 的 Store 上有 TOCTOU 窗口。
        """
        _require_invocation_id(invocation_id)
        cols = tuple(columns)
        placeholders = ",".join("?" * (2 + len(cols)))
        sql = (f"INSERT INTO {case_table} (tenant_id, {case_key}, {', '.join(cols)})"
               f" VALUES ({placeholders})"
               f" ON CONFLICT (tenant_id, {case_key}) DO NOTHING")
        conn = st.conn(store)
        with st.lock_of(store):
            conn.execute(sql, (tenant_id, case_id, *columns.values()))
            conn.commit()

        case = get_case(store, tenant_id, case_id)
        if case is None:
            # 既没插进去、又读不到。不静默返回 None：调用方的类型标注说这里必有一行。
            raise RuntimeError(
                f"create_case 之后读不到 case：tenant={tenant_id} {case_noun}={case_id}")

        stored = _identity_of(case)
        diff = {f: (stored[f], incoming[f])
                for f in identity_fields if stored[f] != incoming[f]}
        if diff:
            _log_case_conflict(store, plan_id=incoming["plan_id"], tenant_id=tenant_id,
                               case_id=case_id, diff=diff, actor=actor_skill,
                               invocation_id=invocation_id)
            detail = "；".join(f"{f}：库里 {old!r}、这次 {new!r}"
                              for f, (old, new) in sorted(diff.items()))
            raise errors.conflict(
                f"{case_noun}={case_id}（tenant={tenant_id}）已经存在，业务字段对不上：{detail}。"
                f"{texts.intake_verb}重跑只在业务字段逐字段相同时幂等；"
                f"对不上说明这不是同一{texts.replay_unit}的重放，"
                f"既不许覆盖也不许静默丢弃 —— 换个 {case_key}，或先查清这个案号为什么被复用"
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
        """`<case_table>.biz_status` 的唯一写入路径。四道闸，顺序不可换。

        🔴 **写进去的是观察与推断，不是权威事实**（铁律 8）。MAOS 不持有权威事实；
        订单、支付、库存的权威状态永远归属外部系统。这里落的 `biz_status` 是本系统
        **观察到并推断出**的状态 —— 所以权威终态必须同事务附上外部给的那份回执，
        没有回执的终态就是「把外部状态直接写死为终态」，那是 bug 不是功能。

        - ① `new_status` 落在权威状态集且 actor 不是权威写入方 -> 落违规事件并抛。
             这一道**放在存在性检查之前**：先查存在性会让「对不存在的 case 越权写终态」
             以 LookupError 收场，证据就没了 —— 而那恰恰是最该留痕的一种试探。
        - ② 递了 `observation` 却不是权威写入方 -> 拒（否则等于给别人开伪造回执的口子）。
        - ③ 权威终态**必须**带回执，字段齐全。
        - ④ 回执说的**得是这件事** —— 由域的 `check_evidence` 判（结构各域不同）。
        - 迁移不在流转表里 -> 抛 `errors.transition`。
        """
        _require_invocation_id(invocation_id)
        case = get_case(store, tenant_id, case_id)
        plan_id = (case or {}).get("plan_id", "")
        auth_states = authoritative_states()

        def _violation(why: str) -> None:
            _log_violation(store, plan_id=plan_id, tenant_id=tenant_id, case_id=case_id,
                           attempted=new_status, actor=actor_skill,
                           invocation_id=invocation_id, why=why)

        # ① 权威闸放在最前面（理由见 docstring）。
        if new_status in auth_states and actor_skill != authoritative_writer:
            _violation(f"{new_status} 只能由 {authoritative_writer} 写入")
            raise errors.violation(
                f"{actor_skill} 试图把 {case_noun}={case_id} 写成 {new_status}；"
                f"该状态的权威在{texts.authority_holder}，只有 {authoritative_writer} "
                f"观察到{texts.evidence_seen_phrase}才写得进来"
            )

        # ② 回执只有权威写入方递得进来，否则等于给别人开了个伪造回执的口子。
        if observation is not None and actor_skill != authoritative_writer:
            _violation(f"{texts.receipt_noun}只能由 {authoritative_writer} 提交")
            raise errors.violation(
                f"{actor_skill} 递交了{texts.submitted_noun}；{texts.fact_subject}"
                f"是外部权威事实，只有 {authoritative_writer} 能落库"
            )

        if case is None:
            raise LookupError(f"没有这个 case：tenant={tenant_id} {case_noun}={case_id}")

        flow = biz_status_flow()
        cur = case["biz_status"]
        if new_status not in flow.get(cur, ()):
            raise errors.transition(
                f"业务状态不许从 {cur} 迁到 {new_status}（{case_noun}={case_id}）；"
                f"{cur} 的合法去向：{flow.get(cur, ()) or '无（终态）'}"
            )

        # ③ 权威终态必须有回执。没有回执的终态就是把外部状态写死为终态（铁律 8）。
        if new_status in auth_states:
            missing = [f for f in observation_required if not (observation or {}).get(f)]
            if missing:
                _violation(f"{texts.missing_why_noun}缺字段 {missing}")
                raise errors.violation(
                    f"写 {new_status} 必须同事务附{texts.attach_noun}，"
                    f"缺字段：{missing}{texts.missing_tail}"
                )
            # ④ 「有一张回执」不等于「回执说这件事成了」。判据结构各域不同，交给域。
            check_evidence(EvidenceContext(
                store=store, plan_id=plan_id, tenant_id=tenant_id, case_id=case_id,
                new_status=new_status, actor_skill=actor_skill,
                invocation_id=invocation_id, observation=observation,
                log_violation=_violation,
            ))

        conn = st.conn(store)
        with st.lock_of(store):
            try:
                if observation is not None:
                    # 观察与状态更新**同事务**：要么都成要么都不成。分两次提交会让
                    # 「状态推进了但回执没落」这种半成品变得可表达 —— 那正是铁律 8
                    # 要挡的形状（库里说钱到了，却拿不出外部说过这话的证据）。
                    write_observation(store, conn, tenant_id=tenant_id, case_id=case_id,
                                      observation=observation, invocation_id=invocation_id)
                conn.execute(
                    f"UPDATE {case_table} SET biz_status=? WHERE tenant_id=? AND {case_key}=?",
                    (new_status, tenant_id, case_id),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        detail: dict[str, Any] = {}
        if event_detail_domain:
            detail["domain"] = event_detail_domain
        detail["tenant_id"] = tenant_id
        detail[case_key] = case_id
        detail["actor"] = actor_skill
        detail["invocation_id"] = invocation_id
        detail["observation_attached"] = observation is not None
        detail.update(extra_detail(observation))
        store.append_event_log({
            "plan_id": plan_id,
            "event_type": biz_status_event,
            "from_state": cur, "to_state": new_status, "reason": reason,
            "detail": detail,
        })
        return get_case(store, tenant_id, case_id)  # type: ignore[return-value]

    return CaseGuard(
        create_case=create_case,
        update_biz_status=update_biz_status,
        get_case=get_case,
        _require_invocation_id=_require_invocation_id,
        _identity_of=_identity_of,
        _log_case_conflict=_log_case_conflict,
        _log_violation=_log_violation,
    )
