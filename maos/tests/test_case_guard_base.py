"""案件守卫骨架（`maos/domain/_case_guard.py`）—— 下沉之后那几件不许变的事。

T81 把 ap / claim / investigation 三个域同构的守卫控制流下沉成一份骨架。
结构性下沉最容易在两个地方**静默**退化，本文件就钉这两处：

  1. **铁律 8 被稀释**：`update_biz_status()` 写的是观察与推断，不是权威事实。
     判据落在「权威终态写不进去，除非同事务落一条带观察语义的行」上 ——
     不落在注释在不在（注释判不了，但它必须在，见 `_case_guard` 的模块 docstring）。
  2. **fail-closed 变成 warning**：`_require_invocation_id()` 拿不到锚点就**抛**。
     三个域各验一次 —— 只验一个域等于没验，下沉的意义就是三个域共用一份姿态。

外加一条回归守卫：报错文案与两条审计行的**字段、顺序、原文**逐字钉死。它们是审计链
的一部分，`scripts/verify.py` 那边按字段读得到；期望值写死在本文件里，**不从旧实现
import** —— 从被测对象那边取期望值的测试证明不了任何事。
"""

from __future__ import annotations

from dataclasses import fields

import pytest

from maos.core.store import SqliteStore
from maos.domain import _case_guard
from maos.domain.ap import guard as ap_guard
from maos.domain.ap import objects as ap_objects
from maos.domain.claim import guard as claim_guard
from maos.domain.claim import objects as claim_objects
from maos.domain.investigation import guard as inv_guard
from maos.domain.investigation import objects as inv_objects

PLAN = "plan-t81"


# ------------------------------------------------------------------ 三个域的种子
def _ap_store():
    st = SqliteStore()
    st.init_schema()
    ap_objects.ensure_schema(st)
    return st


def _ap_case(st, **over):
    kw = dict(tenant_id="tnt-ap", case_id="case-ap-1", supplier_id="SUP-1",
              po_id="PO-1", po_version=1, invoice_id="INV-1", gr_id="GR-1",
              amount_claimed="1000.00", plan_id=PLAN, actor_skill="ap.intake",
              invocation_id="iv-seed")
    kw.update(over)
    return ap_guard.create_case(st, **kw)


def _claim_store():
    st = SqliteStore()
    st.init_schema()
    claim_objects.ensure_schema(st)
    return st


def _claim_case(st, **over):
    kw = dict(tenant_id="tnt-cl", claim_id="clm-1", payer_id="payer-1",
              policy_no="POL-1", policy_version=1, loss_type="illness",
              incident_at="2026-06-20T00:00:00+00:00", amount_claimed=12000.0,
              plan_id=PLAN, actor_skill="claim.intake", invocation_id="iv-seed")
    kw.update(over)
    return claim_guard.create_case(st, **kw)


def _inv_store():
    st = SqliteStore()
    st.init_schema()
    inv_objects.ensure_schema(st)
    inv_objects.put_payment_snapshot(
        st, tenant_id="tnt-iv", original_msg_id="MSG-1", version=1,
        end_to_end_id="E2E-1", interbank_amount=12500.00, currency="EUR",
        value_date="2026-08-20", debtor_agent="DEUTDEFFXXX",
        creditor_agent="BNPAFRPPXXX")
    return st


def _inv_case(st, **over):
    kw = dict(tenant_id="tnt-iv", case_id="case-iv-1", creator_agent="DEUTDEFFXXX",
              assignee_agent="BNPAFRPPXXX", original_msg_id="MSG-1",
              original_version=1, end_to_end_id="E2E-1", amount=12500.00,
              currency="EUR", plan_id=PLAN, actor_skill="investigation.file",
              invocation_id="iv-seed")
    kw.update(over)
    return inv_guard.create_case(st, **kw)


#: (域名, guard 模块, 建 store, 建 case, 租户, 案号, 案号列名, 表名, 一个合法的非终态迁移)
DOMAINS = [
    ("ap", ap_guard, _ap_store, _ap_case, "tnt-ap", "case-ap-1", "case_id",
     "ap_case", ("matched", "ap.match")),
    ("claim", claim_guard, _claim_store, _claim_case, "tnt-cl", "clm-1", "claim_id",
     "claim_case", ("adjudicated", "claim.adjudicate")),
    ("investigation", inv_guard, _inv_store, _inv_case, "tnt-iv", "case-iv-1",
     "case_id", "investigation_case", ("rejected", "investigation.close")),
]
DOMAIN_IDS = [d[0] for d in DOMAINS]


def _events(st, event_type: str) -> list[dict]:
    return [e for e in st.list_event_log(PLAN) if e["event_type"] == event_type]


def _ap_to_pre_terminal(st):
    ap_guard.update_biz_status(st, "tnt-ap", "case-ap-1", "matched", "ap.match", "iv-m")
    ap_guard.update_biz_status(st, "tnt-ap", "case-ap-1", "payment_requested",
                               "ap.execute", "iv-e")


def _claim_to_pre_terminal(st):
    claim_guard.update_biz_status(st, "tnt-cl", "clm-1", "adjudicated",
                                  "claim.adjudicate", "iv-a")
    claim_guard.update_biz_status(st, "tnt-cl", "clm-1", "payment_requested",
                                  "claim.pay", "iv-p")


def _inv_to_pre_terminal(st):
    inv_guard.set_classification(st, "tnt-iv", "case-iv-1", "DUPL",
                                 "investigation.classify", "iv-c")
    inv_guard.update_biz_status(st, "tnt-iv", "case-iv-1", "cancellation_sent",
                                "investigation.cancel", "iv-s")


#: 把案子推到权威终态的**合法前驱**。各域的主干长度不同，所以这一步不能共用一份。
_TO_PRE_TERMINAL = {
    "ap": _ap_to_pre_terminal,
    "claim": _claim_to_pre_terminal,
    "investigation": _inv_to_pre_terminal,
}


# ------------------------------------------------- ① fail-closed 的 actor 锚点
@pytest.mark.parametrize("spec", DOMAINS, ids=DOMAIN_IDS)
def test_missing_invocation_id_raises_instead_of_defaulting(spec):
    """# 论证：拿不到 `invocation_id` 就**抛**，不降级成 warning、不给默认值。

    三个域各验一次：下沉之后这条姿态由骨架一处实现，一处松了就是三个域一起松 ——
    所以不许只验一个域就算数。

    「不给默认值」这一半单独校：兜底成空字符串的写法一样不抛，但审计链已经断了，
    而且断得静默。所以除了抛，还要证明**一个字节都没写进去**。
    """
    _name, guard, make_store, make_case, tenant, case_id, _key, table, (nxt, actor) = spec
    st = make_store()

    with pytest.raises(ValueError) as exc:
        make_case(st, invocation_id="")
    assert f"它是 {table} 每一次写入的 actor 锚点" in str(exc.value), (
        "报错要指名是哪张表的锚点 —— 通用话会让排查绕远路")
    assert guard.get_case(st, tenant, case_id) is None, (
        "锚点为空时不许留下半个案子：fail-closed 是拒绝，不是先写再说")

    make_case(st)
    with pytest.raises(ValueError):
        guard.update_biz_status(st, tenant, case_id, nxt, actor, "")
    assert guard.get_case(st, tenant, case_id)["biz_status"] != nxt, (
        "锚点为空的迁移不许生效")


# --------------------------------------------- ② 写进去的是观察，不是权威事实
def test_biz_status_is_recorded_as_observation_not_authority():
    """# 论证：权威终态只跟着一条**观察行**一起落地，不能被当成事实直接写。

    铁律 8 在这里的具体形状：`settled` 不是「我们说它 settled」，而是「银行给了一份
    回单，我们观察到并推断出 settled」。所以判据是两条：

      · 不带回单写 settled -> 拒（那就是把外部状态直接写死为终态）
      · 带回单写成了 -> 库里必须留下那一行观察，且它指得回**是谁、哪一次调用**观察到的

    判据落在写进去的那行带着观察语义的字段上（`observed_state` /
    `actor_invocation_id` / `bank_reference`），不落在注释在不在。
    """
    st = _ap_store()
    _ap_case(st)
    ap_guard.update_biz_status(st, "tnt-ap", "case-ap-1", "matched", "ap.match", "iv-m")
    ap_guard.update_biz_status(st, "tnt-ap", "case-ap-1", "payment_requested",
                               "ap.execute", "iv-e")

    with pytest.raises(ap_guard.AuthoritativeFactViolation):
        ap_guard.update_biz_status(st, "tnt-ap", "case-ap-1", "settled",
                                   ap_guard.AUTHORITATIVE_WRITER, "iv-o")
    assert ap_guard.get_case(st, "tnt-ap", "case-ap-1")["biz_status"] == "payment_requested"
    assert ap_guard.observations_of(st, "tnt-ap", "case-ap-1") == [], (
        "被拒的那一次不许留下半条观察")

    case = ap_guard.update_biz_status(
        st, "tnt-ap", "case-ap-1", "settled", ap_guard.AUTHORITATIVE_WRITER, "iv-obs",
        observation={"instruction_id": "INS-1", "observed_state": "settled",
                     "bank_reference": "BANKREF-1", "value_date": "2026-08-20"})
    assert case["biz_status"] == "settled"

    rows = ap_guard.observations_of(st, "tnt-ap", "case-ap-1")
    assert len(rows) == 1, "终态必须与观察同事务落地，不许只推状态"
    assert rows[0]["observed_state"] == "settled"
    assert rows[0]["bank_reference"] == "BANKREF-1", (
        "可对账的凭据 —— 没有它的「已付」在财务上对不了账")
    assert rows[0]["actor_invocation_id"] == "iv-obs", (
        "观察行必须指得回是哪一次调用问出来的，否则审计链断了")


@pytest.mark.parametrize("spec", DOMAINS, ids=DOMAIN_IDS)
def test_authoritative_state_needs_an_observation_in_every_domain(spec):
    """# 论证：上一条那件事三个域都成立 —— 权威终态一律不许裸写。

    单验 ap 会让「骨架给某个域漏配了 `observation_required`」这种退化查不出来。
    """
    _name, guard, make_store, make_case, tenant, case_id, _key, _table, _nxt = spec
    st = make_store()
    make_case(st)
    _TO_PRE_TERMINAL[_name](st)          # 先推到终态的**合法前驱**，否则先撞迁移闸
    for state in sorted(guard.AUTHORITATIVE_STATES):
        with pytest.raises(guard.AuthoritativeFactViolation) as exc:
            guard.update_biz_status(st, tenant, case_id, state,
                                    guard.AUTHORITATIVE_WRITER, "iv-bare")
        assert "必须同事务附" in str(exc.value)


# ------------------------------------------------------- ③ 冲突照样落审计行
@pytest.mark.parametrize("spec", DOMAINS, ids=DOMAIN_IDS)
def test_case_conflict_still_writes_an_audit_row(spec):
    """# 论证：拒绝一次案号复用**并且**留痕，字段与顺序都不变。

    「系统拒绝了一次越权写入」本身就是证据，吞掉就没了。下沉最容易在这里偷偷改掉
    detail 的形状 —— 形状变了 `scripts/verify.py` 那边就读不到了。
    """
    _name, guard, make_store, make_case, tenant, case_id, key, _table, _nxt = spec
    st = make_store()
    make_case(st)

    field, other = {
        "ap": ("amount_claimed", "2000.00"),
        "claim": ("amount_claimed", 99999.0),
        "investigation": ("amount", 999.0),
    }[_name]
    with pytest.raises(guard.CaseIdentityConflict):
        make_case(st, **{field: other}, invocation_id="iv-dup")

    hits = _events(st, guard.CASE_CONFLICT_EVENT)
    assert len(hits) == 1
    detail = hits[-1]["detail"]
    assert hits[-1]["reason"].startswith(f"{key} 被复用，业务字段对不上："), (
        "reason 要指名是哪一列的案号被复用")
    assert detail["tenant_id"] == tenant
    assert detail[key] == case_id, "案号必须落在本域自己那个键名下"
    assert detail["invocation_id"] == "iv-dup"
    assert field in detail["conflicts"]
    assert set(detail["conflicts"][field]) == {"stored", "incoming"}, (
        "冲突要同时给出库里那份与这次这份，只给一边看不出该改哪边")


# ------------------------------------------------- ④ 域特有的守卫留在域里
def test_domain_specific_guards_stay_in_their_domain():
    """# 论证：骨架只收同构的那八个，域特有的守卫一个都没被卷进去。

    卷进去的代价不是难看：`set_classification()` 写的是 camt.056 要用的撤销原因码，
    它跟别的域没有任何共同语义。硬塞进骨架会逼着另外两个域也长出一个用不上的入口。
    """
    skeleton = {f.name for f in fields(_case_guard.CaseGuard)}
    assert skeleton == {
        "create_case", "update_biz_status", "get_case", "_require_invocation_id",
        "_identity_of", "_log_case_conflict", "_log_violation",
    }

    for name in ("set_classification", "message_family", "insert_observation",
                 "observations_of"):
        assert hasattr(inv_guard, name), f"investigation 的 {name} 不许被下沉走"
        assert name not in skeleton, f"{name} 是域特有的，不该出现在骨架上"

    for name in ("record_observation", "observations_of"):
        assert hasattr(ap_guard, name), f"ap 的 {name} 不许被下沉走"
        assert name not in skeleton

    # claim 是最干净的一个域：没有域特有的守卫函数，也不该被顺手加出来。
    assert not hasattr(claim_guard, "record_observation")
    assert not hasattr(claim_guard, "set_classification")

    # 三个异常类各定义各的 —— catch 一个不该顺带把另一个也接住。
    assert ap_guard.AuthoritativeFactViolation is not claim_guard.AuthoritativeFactViolation
    assert ap_guard.AuthoritativeFactViolation is not inv_guard.AuthoritativeFactViolation
    assert claim_guard.AuthoritativeFactViolation is not inv_guard.AuthoritativeFactViolation


# ------------------------------------------------------------ ⑤ 回归守卫
#: 下沉**之前**三个域实际产出的报错原文与审计行形状。手抄自 `b35c618` 的实现，
#: 刻意不从被测模块 import —— 从被测对象那边取期望值的测试证明不了任何事。
_BEFORE_LOWERING = {
    "ap": {
        "violation_message":
            "ap.execute 试图把 case=case-ap-1 写成 settled；该状态的权威在银行，"
            "只有 ap.observe 观察到回单后才写得进来",
        "violation_reason": "settled 只能由 ap.observe 写入",
        "violation_detail_keys": ["domain", "tenant_id", "case_id", "attempted",
                                  "actor", "invocation_id", "authoritative_writer"],
        "transition_message":
            "业务状态不许从 received 迁到 settled（case=case-ap-1）；"
            "received 的合法去向：('matched', 'rejected')",
        "conflict_detail_keys": ["domain", "tenant_id", "case_id", "actor",
                                 "invocation_id", "conflicts"],
        "changed_detail_keys": ["domain", "tenant_id", "case_id", "actor",
                                "invocation_id", "observation_attached"],
    },
    "claim": {
        "violation_message":
            "claim.pay 试图把 claim=clm-1 写成 paid；该状态的权威在赔付方，"
            "只有 claim.observe 观察到到账回执后才写得进来",
        "violation_reason": "paid 只能由 claim.observe 写入",
        "violation_detail_keys": ["tenant_id", "claim_id", "attempted", "actor",
                                  "invocation_id", "domain", "authoritative_writer"],
        "transition_message":
            "业务状态不许从 submitted 迁到 paid（claim=clm-1）；"
            "submitted 的合法去向：('adjudicated', 'rejected')",
        "conflict_detail_keys": ["tenant_id", "claim_id", "actor", "invocation_id",
                                 "conflicts"],
        "changed_detail_keys": ["tenant_id", "claim_id", "actor", "invocation_id",
                                "observation_attached"],
    },
    "investigation": {
        "violation_message":
            "investigation.cancel 试图把 case=case-iv-1 写成 returned；"
            "该状态的权威在清算方，只有 investigation.observe 观察到 pacs.004 "
            "退款报文之后才写得进来",
        "violation_reason": "returned 只能由 investigation.observe 写入",
        "violation_detail_keys": ["tenant_id", "case_id", "attempted", "actor",
                                  "invocation_id", "authoritative_writer"],
        "transition_message":
            "业务状态不许从 filed 迁到 returned（case=case-iv-1）；"
            "filed 的合法去向：('classified', 'rejected')",
        "conflict_detail_keys": ["tenant_id", "case_id", "actor", "invocation_id",
                                 "conflicts"],
        "changed_detail_keys": ["tenant_id", "case_id", "actor", "invocation_id",
                                "observation_attached", "message_type"],
    },
}

#: 越权 actor 与它想写的终态。挑的都是「看起来最有资格」的那个 skill ——
#: 越权闸拦的是资格不是意图。
_INTRUDER = {
    "ap": ("ap.execute", "settled"),
    "claim": ("claim.pay", "paid"),
    "investigation": ("investigation.cancel", "returned"),
}


@pytest.mark.parametrize("spec", DOMAINS, ids=DOMAIN_IDS)
def test_lowering_did_not_change_behavior(spec):
    """# 回归守卫：同一组入参，逐字段比对下沉**前**的期望值。

    钉三样东西，每一样都是下沉最容易悄悄改坏的：

      · 越权闸的报错原文（丢了表名/域名词，排查就绕远路）
      · 三条审计行 detail 的**键与顺序**（`domain` 这个键三个域有的有、有的没有、
        位置还不一样 —— 骨架照抄这份历史不一致，不当场统一，统一就是行为变更）
      · 迁移拒绝的原文（合法去向那一段要照原样列出来）
    """
    name, guard, make_store, make_case, tenant, case_id, key, _table, _nxt = spec
    want = _BEFORE_LOWERING[name]
    actor, terminal = _INTRUDER[name]
    st = make_store()
    make_case(st)

    # ① 越权写权威终态 -> 原文与审计行都不许变
    with pytest.raises(guard.AuthoritativeFactViolation) as exc:
        guard.update_biz_status(st, tenant, case_id, terminal, actor, "iv-bad")
    assert str(exc.value) == want["violation_message"]

    hit = _events(st, guard.VIOLATION_EVENT)[-1]
    assert hit["reason"] == want["violation_reason"]
    assert list(hit["detail"]) == want["violation_detail_keys"], (
        "审计行的字段与顺序是审计链的一部分，verify 那边按它读")
    assert hit["detail"]["attempted"] == terminal
    assert hit["detail"]["actor"] == actor
    assert hit["detail"]["authoritative_writer"] == guard.AUTHORITATIVE_WRITER
    assert hit["detail"][key] == case_id

    # ② 非法迁移 -> 原文不许变（合法去向那一段要照原样列出来）
    with pytest.raises(guard.BizStatusTransitionError) as exc:
        guard.update_biz_status(st, tenant, case_id, terminal,
                                guard.AUTHORITATIVE_WRITER, "iv-jump")
    assert str(exc.value) == want["transition_message"]

    # ③ 冲突审计行与状态变更审计行的形状
    field, other = {"ap": ("amount_claimed", "2000.00"),
                    "claim": ("amount_claimed", 99999.0),
                    "investigation": ("amount", 999.0)}[name]
    with pytest.raises(guard.CaseIdentityConflict):
        make_case(st, **{field: other}, invocation_id="iv-dup")
    conflict = _events(st, guard.CASE_CONFLICT_EVENT)[-1]
    assert list(conflict["detail"]) == want["conflict_detail_keys"]

    guard.update_biz_status(st, tenant, case_id, "rejected", f"{name}.close", "iv-rj")
    changed = _events(st, guard.BIZ_STATUS_EVENT)[-1]
    assert list(changed["detail"]) == want["changed_detail_keys"]
    assert changed["from_state"] == guard.INITIAL_STATUS
    assert changed["to_state"] == "rejected"
    assert changed["detail"]["observation_attached"] is False
