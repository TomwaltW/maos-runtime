"""采购退货域的权威事实边界（铁律 8）—— 越权路径逐条走一遍。

守卫要买的是一句话：**全系统只有 `rtv.observe` 写得进 `credited` 与 `settled`，
而且必须同事务附一份能拿去对账的外部凭据。** 本域有**两个**权威终态，判据不同、
落表不同、来源系统不同，所以每一道闸都得能吃下两个：

  ① 非权威写入方写 credited / settled       -> 抛 + 落事件
  ② 非权威写入方递凭据                       -> 抛 + 落事件
  ③ 权威终态缺凭据（缺单号 / 缺金额）        -> 抛 + 落事件
  ④ 凭据说的不是这件事（acknowledged 冒充）  -> 抛 + 落事件
  ⑤ 绕开守卫直写三张表                        -> 抛

**每一条越权都必须落一条事件**，这不是附带效果：「系统拒绝了一次越权写入」本身
就是要拿给评委看的证据，吞掉就没了。所以每条用例都同时断言「抛了」与「留痕了」。

🔴 第 ④ 道里最要紧的一条是 `acknowledged`：那是「供应商收到退货了」，
不是「供应商认了这笔钱」。回执形状一样，差着一次会计确认。
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

from maos.core.store import SqliteStore
from maos.domain.ap import objects as ap_objects
from maos.domain.rtv import fixtures, guard, objects

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
MAOS_PKG = REPO_ROOT / "maos"
RTV_PKG = MAOS_PKG / "domain" / "rtv"

TEN, CASE, PLAN = "tnt-rtv-test", "case-rtv-guard-1", "plan-rtv-guard-1"
WRITER = guard.AUTHORITATIVE_WRITER


@pytest.fixture()
def store():
    s = SqliteStore()
    s.init_schema()
    ap_objects.ensure_schema(s)          # 上游五张表归 ap 域建
    objects.ensure_schema(s)
    return s


def _case(store, case_id: str = CASE, **over):
    kw = dict(tenant_id=TEN, case_id=case_id, supplier_id="SUP-1", po_id="PO-1",
              po_version=1, gr_id="GR-1", amount_claimed="360.00",
              plan_id=PLAN, actor_skill="rtv.intake", invocation_id="iv-seed")
    kw.update(over)
    return guard.create_case(store, **kw)


def _advance(store, case_id: str, *steps: tuple[str, str]) -> None:
    """把案子推到某个状态。每步是 `(目标状态, actor)`。"""
    for target, actor in steps:
        extra = {"return_action": "credit"} if target == "disposed" else {}
        guard.update_biz_status(store, TEN, case_id, target, actor,
                                f"iv-{target}", **extra)


def _to_shipped(store, case_id: str = CASE):
    _case(store, case_id)
    _advance(store, case_id, ("disposed", "rtv.dispose"), ("shipped", "rtv.ship"))


def _to_credited(store, case_id: str = CASE):
    _to_shipped(store, case_id)
    guard.update_biz_status(store, TEN, case_id, "credited", WRITER, "iv-obs",
                            observation=_credit_note())


def _violations(store, plan_id: str = PLAN) -> list[dict]:
    return [e for e in store.list_event_log(plan_id)
            if e["event_type"] == guard.VIOLATION_EVENT]


def _credit_note(**over) -> dict:
    """一份**合格**的贷项通知单回执：单号 + 金额 + observed_state='issued'。"""
    obs = {"credit_note_id": "CN-2026-0001", "observed_state": "issued",
           "amount_credited": "360.00", "issued_at": "2026-09-01T08:00:00+00:00"}
    obs.update(over)
    return obs


def _settlement(**over) -> dict:
    """一份**合格**的到账回执：调整凭单号 + AP 凭据号 + observed_state='settled'。"""
    obs = {"adjustment_id": "ADJ-2026-0001", "observed_state": "settled",
           "ap_reference": "APREF-abc123"}
    obs.update(over)
    return obs


# ------------------------------------------------------------------ ① 越权写入
@pytest.mark.parametrize("state,actor", [
    ("credited", "rtv.reconcile"),
    ("credited", "ap.observe"),          # 别的域的权威写入方，在本域一样不算数
    ("settled", "rtv.reconcile"),
    ("settled", "payment.observe"),
])
def test_only_rtv_observe_can_write_an_authoritative_state(store, state, actor):
    case_id = f"{CASE}-{state}-{actor}"
    _to_shipped(store, case_id)
    if state == "settled":
        guard.update_biz_status(store, TEN, case_id, "credited", WRITER, "iv-cn",
                                observation=_credit_note())
    before = len(_violations(store))
    with pytest.raises(guard.AuthoritativeFactViolation, match=WRITER):
        guard.update_biz_status(store, TEN, case_id, state, actor, "iv-x")
    assert len(_violations(store)) == before + 1, "越权写入必须留一条事件"
    assert objects.get_case(store, TEN, case_id)["biz_status"] != state


def test_violation_on_a_case_that_does_not_exist_is_still_recorded(store):
    """对不存在的 case 越权写 credited 也要留痕 —— 那恰恰是最该留痕的一种试探。

    第 ① 道必须排在存在性检查**之前**，否则这一条会以 LookupError 收场，证据就没了。
    """
    with pytest.raises(guard.AuthoritativeFactViolation):
        guard.update_biz_status(store, TEN, "case-不存在", "credited",
                                "rtv.reconcile", "iv-x")
    assert len(_violations(store, "")) == 1


# ------------------------------------------------------------------ ② 越权递凭据
@pytest.mark.parametrize("observation", [_credit_note(), _settlement()])
def test_only_rtv_observe_can_submit_a_receipt(store, observation):
    case_id = f"{CASE}-recv-{sorted(observation)[0]}"
    _to_shipped(store, case_id)
    before = len(_violations(store))
    with pytest.raises(guard.AuthoritativeFactViolation, match="外部权威事实"):
        guard.update_biz_status(store, TEN, case_id, "compensated", "rtv.compensate",
                                "iv-x", observation=observation)
    assert len(_violations(store)) == before + 1


def test_record_observation_rejects_non_writers(store):
    _case(store)
    with pytest.raises(guard.AuthoritativeFactViolation, match=WRITER):
        guard.record_observation(store, tenant_id=TEN, case_id=CASE,
                                 observed_state="pending", invocation_id="iv-x",
                                 actor_skill="rtv.reconcile")
    assert len(_violations(store)) == 1


@pytest.mark.parametrize("observed_state", ["issued", "settled"])
def test_record_observation_refuses_terminal_criteria(store, observed_state):
    """两个权威终态的判据值都不许从这条旁路单独落。

    只挡 `settled` 会把 `credited` 的判据 `issued` 漏在外面 —— 本域有两个终态，
    判据值必须取**并集**。
    """
    _case(store)
    with pytest.raises(guard.AuthoritativeFactViolation, match="同事务"):
        guard.record_observation(store, tenant_id=TEN, case_id=CASE,
                                 observed_state=observed_state,
                                 invocation_id="iv", actor_skill=WRITER)


def test_record_observation_keeps_a_non_terminal_fact(store):
    """AP 说这笔没成（`failed`）要留痕，但**不推进**业务状态。"""
    _to_credited(store)
    # credited 的凭据落 `credit_note`，不落这张表 —— 两个权威终态凭据形态不同。
    assert objects.observations_of(store, TEN, CASE) == []
    for seq, state in enumerate(("pending", "failed"), start=1):
        guard.record_observation(store, tenant_id=TEN, case_id=CASE,
                                 observed_state=state, invocation_id=f"iv-{state}",
                                 actor_skill=WRITER, adjustment_id=f"ADJ-{seq}",
                                 ap_reference=f"APREF-{seq}")
    obs = objects.observations_of(store, TEN, CASE)
    assert [o["observed_state"] for o in obs] == ["pending", "failed"]
    assert [o["seq"] for o in obs] == [1, 2], "seq 必须递增，第二次轮询不许撞主键"
    assert objects.get_case(store, TEN, CASE)["biz_status"] == "credited"


# ------------------------------------------------------------------ ③ 凭据齐全
@pytest.mark.parametrize("drop", ["credit_note_id", "amount_credited", "observed_state"])
def test_credited_requires_a_complete_credit_note(store, drop):
    case_id = f"{CASE}-cn-{drop}"
    _to_shipped(store, case_id)
    before = len(_violations(store))
    with pytest.raises(guard.AuthoritativeFactViolation, match=drop):
        guard.update_biz_status(store, TEN, case_id, "credited", WRITER, "iv",
                                observation=_credit_note(**{drop: ""}))
    assert len(_violations(store)) == before + 1


@pytest.mark.parametrize("drop", ["adjustment_id", "ap_reference", "observed_state"])
def test_settled_requires_a_complete_settlement_advice(store, drop):
    case_id = f"{CASE}-st-{drop}"
    _to_credited(store, case_id)
    before = len(_violations(store))
    with pytest.raises(guard.AuthoritativeFactViolation, match=drop):
        guard.update_biz_status(store, TEN, case_id, "settled", WRITER, "iv",
                                observation=_settlement(**{drop: ""}))
    assert len(_violations(store)) == before + 1


def test_authoritative_state_without_any_receipt_is_refused(store):
    """连凭据都不递就想收口 —— 那就是把外部状态直接写死为终态。"""
    _to_shipped(store)
    with pytest.raises(guard.AuthoritativeFactViolation, match="同事务附外部凭据"):
        guard.update_biz_status(store, TEN, CASE, "credited", WRITER, "iv")


# ------------------------------------------------------- ④ 凭据说的得是这件事
def test_acknowledged_never_counts_as_credited(store):
    """🔴 本域最要紧的一条断言。

    `acknowledged` = 「供应商收到退货了」，`issued` = 「供应商开出了贷项通知单」。
    两份回执字段齐全、形状一模一样，第 ③ 道无从分辨 —— 差的是一次会计确认。
    放过它，系统持有的就只是「货到了对方仓库」，不是「对方认了这笔钱」。
    """
    _to_shipped(store)
    before = len(_violations(store))
    with pytest.raises(guard.AuthoritativeFactViolation, match="acknowledged"):
        guard.update_biz_status(store, TEN, CASE, "credited", WRITER, "iv",
                                observation=_credit_note(observed_state="acknowledged"))
    assert len(_violations(store)) == before + 1
    assert objects.get_case(store, TEN, CASE)["biz_status"] == "shipped"
    assert objects.get_credit_note(store, TEN, CASE) is None, (
        "被拒的那次不许留下一张贷项通知单")


def test_acknowledged_is_not_in_the_credited_criteria():
    """把它钉在常量上：判据集合里只有 `issued`，一个字都不许多。"""
    assert guard.AUTHORITATIVE_RECEIPT_STATE["credited"] == frozenset({"issued"})
    assert "acknowledged" not in guard.AUTHORITATIVE_RECEIPT_STATE["credited"]


@pytest.mark.parametrize("seen", ["acknowledged", "pending", "disputed", "issued"])
def test_settlement_advice_must_say_the_money_arrived(store, seen):
    """`settled` 只认 `settled`，连 `credited` 的判据 `issued` 都不认。"""
    case_id = f"{CASE}-say-{seen}"
    _to_credited(store, case_id)
    with pytest.raises(guard.AuthoritativeFactViolation, match=seen):
        guard.update_biz_status(store, TEN, case_id, "settled", WRITER, "iv",
                                observation=_settlement(observed_state=seen))


def test_the_three_authoritative_tables_stay_in_sync():
    """状态集合 / 必填字段 / 判据集合 / 落表，四张表必须逐键对齐。

    漏配的后果是那个终态退回到「有凭据就算数」，静默且没人会发现。
    """
    states = set(guard.AUTHORITATIVE_STATES)
    assert set(guard.AUTHORITATIVE_RECEIPT_STATE) == states
    assert set(guard._OBSERVATION_REQUIRED) == states
    assert set(guard._RECEIPT_TABLE) == states
    for state in states:
        assert state in guard.BIZ_STATUS_FLOW, f"{state} 不在业务状态机里"
        assert guard.AUTHORITATIVE_RECEIPT_STATE[state], f"{state} 的判据集合是空的"


def test_missing_receipt_criterion_is_fail_closed(store, monkeypatch):
    """把判据表挖空之后，权威终态**写不进去**而不是放行。

    这条演的是上一条断言保护的那个洞真的存在：少配一项时，第 ④ 道拿不到判据 ——
    此时唯一正确的行为是拒，不是默认放行。
    """
    _to_shipped(store)
    monkeypatch.setattr(guard, "AUTHORITATIVE_RECEIPT_STATE", {})
    with pytest.raises(guard.AuthoritativeFactViolation, match="同增同减"):
        guard.update_biz_status(store, TEN, CASE, "credited", WRITER, "iv",
                                observation=_credit_note())


def test_missing_required_fields_table_is_fail_closed(store, monkeypatch):
    """必填字段表挖空同样 fail-closed，不许退回到「递了个空 dict 也算有凭据」。"""
    _to_shipped(store)
    monkeypatch.setattr(guard, "_OBSERVATION_REQUIRED", {})
    with pytest.raises(guard.AuthoritativeFactViolation, match="同增同减"):
        guard.update_biz_status(store, TEN, CASE, "credited", WRITER, "iv",
                                observation=_credit_note())


def test_a_receipt_on_a_non_authoritative_state_is_refused(store):
    """权威写入方也不许把凭据挂在非权威状态上 —— 那种凭据没有落表，也没有意义。"""
    _to_shipped(store)
    with pytest.raises(guard.AuthoritativeFactViolation, match="不是权威终态"):
        guard.update_biz_status(store, TEN, CASE, "compensated", WRITER, "iv",
                                observation=_credit_note())


# ------------------------------------------------------------------ 正例：两段收口
def test_the_happy_path_writes_both_receipts(store):
    """两个权威终态各走一次正例，凭据落进各自那张表、与状态更新同事务。"""
    _to_shipped(store)
    case = guard.update_biz_status(store, TEN, CASE, "credited", WRITER, "iv-cn",
                                   observation=_credit_note())
    assert case["biz_status"] == "credited"
    note = objects.get_credit_note(store, TEN, CASE)
    assert note["credit_note_id"] == "CN-2026-0001"
    assert note["amount_credited"] == "360.00"
    assert note["document_type"] == "381"          # UNCL1001，贷项通知单恒为 381
    assert note["observed_by"] == WRITER and note["invocation_id"] == "iv-cn"
    # 供应商开出的时间与我方观察到的时间刻意分开。
    assert note["issued_at"] == "2026-09-01T08:00:00+00:00"
    assert note["observed_at"] and note["observed_at"] != note["issued_at"]

    case = guard.update_biz_status(store, TEN, CASE, "settled", WRITER, "iv-ap",
                                   observation=_settlement())
    assert case["biz_status"] == "settled"
    obs = objects.observations_of(store, TEN, CASE)
    assert [o["adjustment_id"] for o in obs] == ["ADJ-2026-0001"]
    assert obs[0]["ap_reference"] == "APREF-abc123"


def test_a_rejected_receipt_leaves_no_row_behind(store):
    """被拒的那次不许留下半张凭据 —— 凭据与状态更新同生共死。

    第 ④ 道在**落表之前**判，所以这里断言的是「一行都没写」，
    而不是「写了又回滚」—— 两者对外表现一样，但前者连事务都不必开。
    """
    _to_credited(store)
    with pytest.raises(guard.AuthoritativeFactViolation):
        guard.update_biz_status(store, TEN, CASE, "settled", WRITER, "iv",
                                observation=_settlement(observed_state="pending"))
    assert objects.observations_of(store, TEN, CASE) == []
    assert objects.get_case(store, TEN, CASE)["biz_status"] == "credited"


# ------------------------------------------------------------------ ⑤ 旁路直写
@pytest.mark.parametrize("sql", [
    "UPDATE rtv_case SET biz_status='settled' WHERE case_id='x'",
    "INSERT INTO rtv_case (tenant_id) VALUES ('t')",
    "INSERT OR REPLACE INTO rtv_case (tenant_id) VALUES ('t')",
    "DELETE FROM rtv_case WHERE case_id='x'",
    "REPLACE INTO rtv_case (tenant_id) VALUES ('t')",
    "update  \"rtv_case\"  set biz_status='credited'",
    "INSERT INTO credit_note (tenant_id) VALUES ('t')",
    "INSERT OR REPLACE INTO credit_note (tenant_id) VALUES ('t')",
    "UPDATE credit_note SET amount_credited='9999'",
    "INSERT INTO rtv_settlement_observation (tenant_id) VALUES ('t')",
    "DELETE FROM rtv_settlement_observation WHERE case_id='x'",
])
def test_objects_execute_refuses_to_write_the_guarded_tables(store, sql):
    """⑤ 运行时旁路：`objects.execute` 见到这三张表的写语句直接抛。

    比 ap 域多守两张：那边对 `ap_payment_observation` 不设限，留了一条
    「伪造回单」的运行时后门。承载权威事实的表不该有第二条写入路径。
    """
    with pytest.raises(objects.BypassedGuardError, match="guard"):
        objects.execute(store, sql)


def test_alter_table_on_the_guarded_tables_is_not_blocked(store):
    """给 rtv_case 加列这类正常迁移不受拦截 —— 守的是写数据，不是改形状。"""
    objects.execute(store, "ALTER TABLE rtv_case ADD COLUMN memo TEXT DEFAULT ''")
    assert objects.query(store, "SELECT memo FROM rtv_case") == []


def test_no_source_file_writes_the_guarded_tables_outside_the_guard():
    """提交前那条 grep 自查，钉成断言：全仓只有守卫写得动那三张表。"""
    pattern = re.compile(
        r"(?:UPDATE|INSERT\s+(?:OR\s+\w+\s+)?INTO|REPLACE\s+INTO|DELETE\s+FROM)\s+"
        r"(?:rtv_case|credit_note|rtv_settlement_observation)\b",
        re.IGNORECASE)
    allowed = {RTV_PKG / "guard.py",
               MAOS_PKG / "tests" / "test_rtv_guard.py",
               MAOS_PKG / "tests" / "test_rtv_domain.py",
               # 里面那三行 SQL 是证明 execute() 拦得住的反例（test_no_bypass_path_around_the_guard）
               MAOS_PKG / "tests" / "test_rtv_skills.py"}
    offenders = []
    for path in sorted(MAOS_PKG.rglob("*.py")):
        if path in allowed:
            continue
        for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")
    assert not offenders, f"这些地方在绕开 guard 直接写权威表：{offenders}"


# ------------------------------------------------------------ 状态机与裁定
def test_biz_status_flow_matches_the_frozen_contract():
    """逐键对照 C-R2：七个状态，边一条不多一条不少。"""
    assert guard.BIZ_STATUS_FLOW == {
        "received":    ("disposed", "rejected"),
        "disposed":    ("shipped", "rejected", "compensated"),
        "shipped":     ("credited", "compensated"),
        "credited":    ("settled", "compensated"),
        "settled":     (),
        "rejected":    (),
        "compensated": (),
    }
    assert guard.INITIAL_STATUS == "received"
    assert len(guard.BIZ_STATUS_FLOW) == 7
    assert sum(len(v) for v in guard.BIZ_STATUS_FLOW.values()) == 9


def test_biz_status_flow_has_no_shortcut_into_an_authoritative_state():
    """只有 `shipped` 到得了 `credited`，只有 `credited` 到得了 `settled`。

    多一条捷径就等于多一条「货还没发就宣布供应商认账」的路。
    """
    into = {state: sorted(src for src, dsts in guard.BIZ_STATUS_FLOW.items()
                          if state in dsts)
            for state in guard.AUTHORITATIVE_STATES}
    assert into == {"credited": ["shipped"], "settled": ["credited"]}


@pytest.mark.parametrize("src,dst,actor", [
    ("received", "disposed", "rtv.dispose"),
    ("received", "rejected", "rtv.intake"),
    ("disposed", "shipped", "rtv.ship"),
    ("disposed", "rejected", "rtv.dispose"),
    ("disposed", "compensated", "rtv.compensate"),
    ("shipped", "credited", WRITER),
    ("shipped", "compensated", "rtv.compensate"),
    ("credited", "settled", WRITER),
    ("credited", "compensated", "rtv.compensate"),
])
def test_every_legal_edge_can_be_walked(store, src, dst, actor):
    """C-R2 的九条合法边各走一次 —— 状态机不许有走不通的边。"""
    case_id = f"{CASE}-{src}-{dst}"
    _case(store, case_id)
    path = {"received": (), "disposed": (("disposed", "rtv.dispose"),),
            "shipped": (("disposed", "rtv.dispose"), ("shipped", "rtv.ship")),
            "credited": (("disposed", "rtv.dispose"), ("shipped", "rtv.ship"))}[src]
    _advance(store, case_id, *path)
    if src == "credited":
        guard.update_biz_status(store, TEN, case_id, "credited", WRITER, "iv-cn",
                                observation=_credit_note())
    extra: dict = {}
    if dst == "disposed":
        extra["return_action"] = "credit"
    elif dst == "credited":
        extra["observation"] = _credit_note()
    elif dst == "settled":
        extra["observation"] = _settlement()
    case = guard.update_biz_status(store, TEN, case_id, dst, actor, "iv", **extra)
    assert case["biz_status"] == dst


@pytest.mark.parametrize("dst", ["settled", "credited", "shipped"])
def test_illegal_transition_is_refused(store, dst):
    """`received` 直接跳到后面几段都不行。

    actor 用权威写入方，好让第 ①② 道放行、真正判在状态机上；
    否则这条测的就是越权闸而不是状态机了。
    """
    case_id = f"{CASE}-illegal-{dst}"
    _case(store, case_id)
    with pytest.raises(guard.BizStatusTransitionError, match="received"):
        guard.update_biz_status(store, TEN, case_id, dst, WRITER, "iv")


def test_terminal_states_have_nowhere_to_go(store):
    _to_credited(store)
    guard.update_biz_status(store, TEN, CASE, "settled", WRITER, "iv-ap",
                            observation=_settlement())
    with pytest.raises(guard.BizStatusTransitionError, match="终态"):
        guard.update_biz_status(store, TEN, CASE, "compensated", "rtv.compensate", "iv")


def test_disposed_requires_a_return_action(store):
    """「已裁定」而裁不出结果是矛盾状态。"""
    _case(store)
    with pytest.raises(guard.DispositionRequired, match="return_action"):
        guard.update_biz_status(store, TEN, CASE, "disposed", "rtv.dispose", "iv")
    assert objects.get_case(store, TEN, CASE)["biz_status"] == "received"


def test_return_action_must_be_one_of_three(store):
    _case(store)
    with pytest.raises(ValueError, match="取值域"):
        guard.update_biz_status(store, TEN, CASE, "disposed", "rtv.dispose", "iv",
                                return_action="退钱吧")


def test_return_action_only_lands_on_disposed(store):
    """裁定结果与「已裁定」这个状态是同一件事的两面，不许分开写。"""
    _case(store)
    with pytest.raises(ValueError, match="只在推进到 disposed 时接受"):
        guard.update_biz_status(store, TEN, CASE, "rejected", "rtv.intake", "iv",
                                return_action="credit")


def test_disposition_lands_in_the_same_transaction(store):
    _case(store)
    case = guard.update_biz_status(store, TEN, CASE, "disposed", "rtv.dispose", "iv",
                                   return_action="replacement")
    assert case["biz_status"] == "disposed" and case["return_action"] == "replacement"


# ------------------------------------------------------------ 建案：不接受终态
def test_create_case_does_not_accept_a_status(store):
    """`biz_status` / `return_action` 都不是 `create_case` 的参数。

    想直接建成 settled 的路必须从一开始就不存在 —— 否则守卫只挡得住 update，
    挡不住 insert。判据落在**函数签名**上：参数不存在，比「传了会被忽略」更硬。
    """
    import inspect
    params = set(inspect.signature(guard.create_case).parameters)
    assert "biz_status" not in params and "return_action" not in params
    with pytest.raises(TypeError):
        guard.create_case(store, **dict(  # type: ignore[arg-type]
            tenant_id=TEN, case_id="x", supplier_id="S", po_id="P", po_version=1,
            gr_id="G", amount_claimed="1", plan_id=PLAN, actor_skill="rtv.intake",
            invocation_id="iv", biz_status="settled"))
    assert _case(store)["biz_status"] == guard.INITIAL_STATUS


def test_create_case_is_idempotent_and_does_not_rewind(store):
    _case(store)
    _advance(store, CASE, ("disposed", "rtv.dispose"))
    again = _case(store)
    assert again["biz_status"] == "disposed", "重跑受理不许把案子倒回 received"
    assert again["return_action"] == "credit"


def test_create_case_refuses_a_reused_case_id(store):
    _case(store)
    with pytest.raises(guard.CaseIdentityConflict, match="amount_claimed"):
        _case(store, amount_claimed="9999.00")
    conflicts = [e for e in store.list_event_log(PLAN)
                 if e["event_type"] == guard.CASE_CONFLICT_EVENT]
    assert len(conflicts) == 1


def test_create_case_tolerates_amount_formatting(store):
    """`360` 与 `360.00` 是同一笔 —— 不归一就会让每次重跑都报冲突。"""
    _case(store)
    assert _case(store, amount_claimed=360)["case_id"] == CASE


def test_invocation_id_must_not_be_empty(store):
    with pytest.raises(ValueError, match="invocation_id"):
        _case(store, invocation_id="")
    _case(store)
    with pytest.raises(ValueError, match="invocation_id"):
        guard.update_biz_status(store, TEN, CASE, "disposed", "rtv.dispose", "",
                                return_action="credit")


# ------------------------------------------ 域可移植性：互不 import，同名终态互不影响
def test_rtv_domain_does_not_import_another_domain():
    """🔴 `maos/domain/rtv/**` 一行都不 import 别的域。

    跨域 import 会让「换个领域只换 Skill / ToolPort / 业务对象」这句话当场破产 ——
    那正是本域要证明的东西。`money()` / `attach_business_ref()` / 迁移机制是
    **照抄的口径**（各写一份），不是共享的实现。
    """
    bad: list[str] = []
    for path in sorted(RTV_PKG.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for n in names:
                if n.startswith("maos.domain.") and not n.startswith("maos.domain.rtv"):
                    bad.append(f"{path.name}: {n}")
    assert not bad, f"本域 import 了别的域：{bad}"


def test_rtv_domain_does_not_import_the_sibling_tracks():
    """T62 / T63 / T64 的模块在本轨基线里**不存在** —— import 它们从第一条测试就是红的。"""
    src = "\n".join(p.read_text(encoding="utf-8") for p in sorted(RTV_PKG.glob("*.py")))
    for forbidden in ("maos.tools.rtv", "maos.skills.builtin.rtv", "maos.agents.rtv",
                      "maos.flows.scenario_11"):
        assert f"import {forbidden}" not in src and f"from {forbidden}" not in src


def test_rtv_and_ap_guards_are_independent(store):
    """同一个库里两个域各推一个案子到 settled，互不干扰。

    两个域**都有一个叫 `settled` 的终态**，但表名不重、写入方不同、模块互不 import。
    把 `ap.observe` 递给本域，第 ① 道当场拒。
    """
    from maos.domain.ap import fixtures as ap_fixtures
    from maos.domain.ap import guard as ap_guard

    _to_credited(store)
    guard.update_biz_status(store, TEN, CASE, "settled", WRITER, "iv-ap",
                            observation=_settlement())

    ap_fixtures.seed_supplier(store, tenant_id=TEN, supplier_id="SUP-1",
                              name="示例供应商", payment_means_code="30")
    ap_fixtures.seed_three_way(
        store, tenant_id=TEN, supplier_id="SUP-1", po_id="PO-AP", gr_id="GR-AP",
        invoice_id="INV-AP", lines=[(1, "SKU-A", 2, "100.00", 2, 0, 2, "100.00")],
        tax_category="S", tax_rate=13, invoice_type="380",
        issued_at="2026-09-01T00:00:00+00:00")
    ap_guard.create_case(store, tenant_id=TEN, case_id="ap-case-1", supplier_id="SUP-1",
                         po_id="PO-AP", po_version=1, invoice_id="INV-AP", gr_id="GR-AP",
                         amount_claimed="226.00", plan_id=PLAN,
                         actor_skill="ap.intake", invocation_id="iv-ap-1")
    ap_guard.update_biz_status(store, TEN, "ap-case-1", "matched", "ap.match", "iv-m")
    ap_guard.update_biz_status(store, TEN, "ap-case-1", "payment_requested",
                               "ap.execute", "iv-e")
    ap_guard.update_biz_status(
        store, TEN, "ap-case-1", "settled", ap_guard.AUTHORITATIVE_WRITER, "iv-s",
        observation={"instruction_id": "bk-1", "observed_state": "settled",
                     "bank_reference": "bkref-1"})

    assert objects.get_case(store, TEN, CASE)["biz_status"] == "settled"
    assert ap_guard.get_case(store, TEN, "ap-case-1")["biz_status"] == "settled"
    # 各守各的表：本域一条到账观察，ap 域一条银行回单，互不串门。
    assert len(objects.observations_of(store, TEN, CASE)) == 1
    assert objects.query(
        store, "SELECT COUNT(*) AS n FROM ap_payment_observation")[0]["n"] == 1


def test_the_other_domains_writer_is_not_ours(store):
    """把 `ap.observe` 递给本域的守卫，第 ① 道当场拒。"""
    _to_shipped(store)
    with pytest.raises(guard.AuthoritativeFactViolation):
        guard.update_biz_status(store, TEN, CASE, "credited", "ap.observe", "iv",
                                observation=_credit_note())


def test_business_status_never_touches_the_task_state_machine():
    """铁律 9：业务状态是 `rtv_case.biz_status`，不是 Task 状态。

    `maos/contracts/states.py` 里不许出现本域这七个业务状态中的任何一个新名字。
    """
    from maos.contracts import states as contract_states

    src = pathlib.Path(contract_states.__file__).read_text(encoding="utf-8")
    for name in ("disposed", "shipped", "credited", "compensated"):
        assert f'"{name}"' not in src and f"'{name}'" not in src, (
            f"业务状态 {name} 混进了 contracts/states.py（铁律 9）")


def test_fixtures_never_preseed_authoritative_facts(store):
    """靶场跑完，两张权威表**一行都没有** —— 权威事实只能观察得来。"""
    fixtures.seed_supplier(store, tenant_id=TEN, supplier_id=fixtures.DEMO_SUPPLIER,
                           name="示例供应商")
    fixtures.seed_source_documents(
        store, tenant_id=TEN, supplier_id=fixtures.DEMO_SUPPLIER,
        po_id=fixtures.DEMO_PO, gr_id=fixtures.DEMO_GR,
        lines=fixtures.DEMO_LINES, ordered_at="2026-09-01T00:00:00+00:00")
    assert objects.query(store, "SELECT COUNT(*) AS n FROM credit_note")[0]["n"] == 0
    assert objects.query(
        store, "SELECT COUNT(*) AS n FROM rtv_settlement_observation")[0]["n"] == 0
