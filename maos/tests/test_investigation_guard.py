"""权威终态守卫的测试 —— `returned` 的边界，以及越权时的**不静默**。

本文件的第一断言是本轨要买的第一样东西：

    全系统只有 `investigation.observe` 写得进 `returned`，
    而且只有拿着一份 **pacs.004** 观察才写得进去；
    越权写入抛异常 **并且** 落一条事件 —— 不静默失败。

第二类断言守的是本域的招牌判据：**camt.029 的肯定答复（CNCL）写不进 returned**。
这一条比「非权威 actor 写不进去」更容易被写错，因为 CNCL 看起来就是成功。

标了 `# 论证：` 的断言是复赛材料里那几句话的机器化版本，评审时可按前缀捞出来对。
"""

from __future__ import annotations

import pathlib
import re

import pytest

from maos.core.store import SqliteStore
from maos.domain.investigation import guard, objects

TENANT = "tnt-guard"
CASE = "case-guard-1"
MSG = "MSG-GUARD-1"
PLAN = "plan-guard"


def _store():
    store = SqliteStore()
    store.init_schema()
    objects.ensure_schema(store)
    objects.put_payment_snapshot(
        store, tenant_id=TENANT, original_msg_id=MSG, version=1,
        end_to_end_id="E2E-G1", interbank_amount=12500.00, currency="EUR",
        value_date="2026-08-20", debtor_agent="DEUTDEFFXXX",
        creditor_agent="BNPAFRPPXXX")
    return store


def _case(store, *, case_id: str = CASE, advance_to: str = "cancellation_sent"):
    guard.create_case(
        store, tenant_id=TENANT, case_id=case_id, creator_agent="DEUTDEFFXXX",
        assignee_agent="BNPAFRPPXXX", original_msg_id=MSG, original_version=1,
        end_to_end_id="E2E-G1", amount=12500.00, currency="EUR", plan_id=PLAN,
        actor_skill="investigation.file", invocation_id="inv-file")
    if advance_to in ("classified", "cancellation_sent"):
        guard.set_classification(store, TENANT, case_id, "DUPL",
                                 "investigation.classify", "inv-cls")
    if advance_to == "cancellation_sent":
        guard.update_biz_status(store, TENANT, case_id, "cancellation_sent",
                                "investigation.cancel", "inv-cancel")
    return guard.get_case(store, TENANT, case_id)


def _pacs004(**over):
    obs = {"request_id": "req-1", "poll_seq": 3,
           "message_type": "pacs.004.001.09", "return_reason_code": "CUST",
           "returned_amount": 12500.00, "observed_state": guard.OBS_RETURNED}
    obs.update(over)
    return obs


def _camt029(**over):
    obs = {"request_id": "req-1", "poll_seq": 2,
           "message_type": "camt.029.001.08", "confirmation_code": "CNCL",
           "observed_state": guard.OBS_RETURNED}
    obs.update(over)
    return obs


def _violations(store):
    return [e for e in store.list_event_log(PLAN)
            if e["event_type"] == guard.VIOLATION_EVENT]


def _reasons(store):
    """越权事件的 reason 列表 —— 用来分辨是**哪一道闸**拦下的。

    四道闸抛同一个异常类型，只断类型分不出来（变异检验实测：删掉第 ① 道，
    只断类型的用例照样绿）。reason 是它们唯一的区分点。
    """
    return [e["reason"] for e in _violations(store)]


# ------------------------------------------------------- ① 只有权威 actor
@pytest.mark.parametrize("actor", [
    "investigation.file", "investigation.classify", "investigation.cancel",
    "investigation.compensate", "payment.observe",   # 别的域的权威写入方也不行
])
def test_only_the_authoritative_writer_can_write_returned(actor):
    """# 论证：全系统只有 investigation.observe 写得进 returned。

    连一份**完全合格**的 pacs.004 观察也救不了非权威 actor —— 证据对不对是第二关，
    第一关是谁在写。
    """
    store = _store()
    _case(store)
    with pytest.raises(guard.AuthoritativeFactViolation) as exc:
        guard.update_biz_status(store, TENANT, CASE, "returned", actor, "inv-x",
                                observation=_pacs004())
    assert guard.AUTHORITATIVE_WRITER in str(exc.value)
    assert guard.get_case(store, TENANT, CASE)["biz_status"] == "cancellation_sent"
    # **断到具体那一道闸**，不只断异常类型。
    #
    # 四道闸抛的是同一个 AuthoritativeFactViolation，只断类型的话，把第 ① 道
    # 整条删掉这条用例照样绿 —— 因为第 ② 道（回执只有权威方递得进来）会接住它。
    # 变异检验当场抓到过这个盲区，判据因此收紧到事件的 reason 上。
    assert _reasons(store) == [f"returned 只能由 {guard.AUTHORITATIVE_WRITER} 写入"], (
        "拦下这次越权的必须是第 ① 道权威闸本身，不是后面几道的连带效果")


def test_non_writer_blocked_even_without_an_observation():
    """不带观察的越权写入同样要被第 ① 道拦下。

    这条与上一条成对：上一条带着合格观察（会被 ② 接住），这条什么都不带
    （会被 ③ 接住）—— 两条都断 reason，第 ① 道才真的被单独测到。
    """
    store = _store()
    _case(store)
    with pytest.raises(guard.AuthoritativeFactViolation):
        guard.update_biz_status(store, TENANT, CASE, "returned",
                                "investigation.compensate", "inv-bare")
    assert _reasons(store) == [f"returned 只能由 {guard.AUTHORITATIVE_WRITER} 写入"]


def test_violation_is_not_silent():
    """# 论证：越权写入不静默失败 —— 抛异常**并且**落一条事件。

    「系统拒绝了一次越权写入」本身就是要拿给评委看的证据，吞掉就没了。
    """
    store = _store()
    _case(store)
    with pytest.raises(guard.AuthoritativeFactViolation):
        guard.update_biz_status(store, TENANT, CASE, "returned",
                                "investigation.cancel", "inv-bad",
                                observation=_pacs004())
    evs = _violations(store)
    assert len(evs) == 1
    d = evs[0]["detail"]
    assert d["actor"] == "investigation.cancel"
    assert d["attempted"] == "returned"
    assert d["invocation_id"] == "inv-bad"
    assert d["authoritative_writer"] == guard.AUTHORITATIVE_WRITER
    assert evs[0]["reason"] == f"returned 只能由 {guard.AUTHORITATIVE_WRITER} 写入"


def test_violation_on_missing_case_still_leaves_evidence():
    """对不存在的 case 越权写 returned 也要留痕。

    权威闸排在存在性检查**之前**：排在后面的话这次试探会以 LookupError 收场，
    而那恰恰是最该留痕的一种。
    """
    store = _store()
    with pytest.raises(guard.AuthoritativeFactViolation):
        guard.update_biz_status(store, TENANT, "no-such-case", "returned",
                                "investigation.cancel", "inv-y",
                                observation=_pacs004())
    assert len([e for e in store.list_event_log("")
                if e["event_type"] == guard.VIOLATION_EVENT]) == 1


def test_observation_can_only_come_from_the_authoritative_writer():
    """回执只有权威写入方递得进来，否则等于给别人开了个伪造报文的口子。"""
    store = _store()
    _case(store)
    with pytest.raises(guard.AuthoritativeFactViolation) as exc:
        guard.update_biz_status(store, TENANT, CASE, "compensated",
                                "investigation.compensate", "inv-z",
                                observation=_pacs004())
    assert "只有" in str(exc.value)
    assert len(_violations(store)) == 1


# --------------------------------------------- ② 招牌判据：CNCL 写不进 returned
def test_positive_camt029_cannot_write_returned():
    """**本域招牌判据。**

    # 论证：清算方回「撤销成功」（camt.029/CNCL）不等于资金已退回；
    # returned 只认 pacs.004 退款报文。

    这一条是本文件最要紧的用例：CNCL 是一句肯定答复，三个必填字段一个不缺，
    在「有没有观察」这道判据眼里与 pacs.004 无从分辨。放过它，系统持有的就只是
    「清算方确认撤销了」，不是「资金已退回」。
    """
    store = _store()
    _case(store)
    with pytest.raises(guard.AuthoritativeFactViolation) as exc:
        guard.update_biz_status(store, TENANT, CASE, "returned",
                                guard.AUTHORITATIVE_WRITER, "inv-cncl",
                                observation=_camt029())
    msg = str(exc.value)
    assert "camt.029" in msg and "pacs.004" in msg
    assert "确认撤销不等于资金已退回" in msg
    assert guard.get_case(store, TENANT, CASE)["biz_status"] == "cancellation_sent"
    assert len(_violations(store)) == 1
    # 一条 returned 观察都没落下 —— 拒绝的时候不许留半份。
    assert guard.observations_of(store, TENANT, CASE,
                                 observed_state=guard.OBS_RETURNED) == []


def test_camt029_version_bump_still_blocked():
    """换一版 camt.029 报文照样拦得住 —— 判据按报文**族**比，不按全名。"""
    store = _store()
    _case(store)
    for version in ("camt.029.001.08", "camt.029.001.11", "camt.029"):
        with pytest.raises(guard.AuthoritativeFactViolation):
            guard.update_biz_status(store, TENANT, CASE, "returned",
                                    guard.AUTHORITATIVE_WRITER, "inv-v",
                                    observation=_camt029(message_type=version))


def test_returned_needs_amount_and_reason_code():
    """pacs.004 观察缺金额或缺退回原因码，一样写不进 returned。"""
    store = _store()
    _case(store)
    for over, fragment in (({"returned_amount": None}, "returned_amount"),
                           ({"return_reason_code": ""}, "return_reason_code")):
        with pytest.raises(guard.AuthoritativeFactViolation) as exc:
            guard.update_biz_status(store, TENANT, CASE, "returned",
                                    guard.AUTHORITATIVE_WRITER, "inv-p",
                                    observation=_pacs004(**over))
        assert fragment in str(exc.value)


def test_returned_needs_an_observation_at_all():
    """没有观察的 returned 就是把外部状态直接写死为终态。"""
    store = _store()
    _case(store)
    with pytest.raises(guard.AuthoritativeFactViolation) as exc:
        guard.update_biz_status(store, TENANT, CASE, "returned",
                                guard.AUTHORITATIVE_WRITER, "inv-none")
    assert "必须同事务附决议观察" in str(exc.value)


def test_the_happy_write_actually_works():
    """正路要能走通 —— 否则上面那些拒绝只是因为什么都写不进去。"""
    store = _store()
    _case(store)
    case = guard.update_biz_status(store, TENANT, CASE, "returned",
                                   guard.AUTHORITATIVE_WRITER, "inv-ok",
                                   observation=_pacs004())
    assert case["biz_status"] == "returned"
    rows = guard.observations_of(store, TENANT, CASE,
                                 observed_state=guard.OBS_RETURNED)
    assert len(rows) == 1
    assert rows[0]["message_type"] == "pacs.004.001.09"
    assert rows[0]["actor_invocation_id"] == "inv-ok"
    assert rows[0]["returned_amount"] == 12500.00


def test_observation_and_status_are_one_transaction():
    """观察与状态更新同事务：观察插不进去时，状态也不许留下。

    造法：先合法写一次 returned，再拿**同一个** (request_id, poll_seq, observed_at)
    去写第二次 —— 主键冲突让插入失败，此时案子必须还停在原状态。
    """
    store = _store()
    _case(store, case_id="case-tx", advance_to="cancellation_sent")
    obs = _pacs004(observed_at="2026-08-31T00:00:00+00:00")
    guard.update_biz_status(store, TENANT, "case-tx", "returned",
                            guard.AUTHORITATIVE_WRITER, "inv-1", observation=obs)
    # returned 是终态，再迁一次本来就该被状态机拦 —— 先确认拦的是状态机。
    with pytest.raises(guard.BizStatusTransitionError):
        guard.update_biz_status(store, TENANT, "case-tx", "returned",
                                guard.AUTHORITATIVE_WRITER, "inv-2", observation=obs)
    assert guard.get_case(store, TENANT, "case-tx")["biz_status"] == "returned"


# ------------------------------------------------------------- ③ 状态机本身
def test_biz_status_flow_rejects_shortcuts():
    """不许从 filed 一步跳到 returned —— 中间那两步是真的要走。"""
    store = _store()
    _case(store, case_id="case-jump", advance_to="filed")
    with pytest.raises(guard.BizStatusTransitionError):
        guard.update_biz_status(store, TENANT, "case-jump", "returned",
                                guard.AUTHORITATIVE_WRITER, "inv-j",
                                observation=_pacs004())


def test_terminal_states_have_no_exits():
    """三个终态都不许再迁出去。"""
    for terminal in ("returned", "rejected", "compensated"):
        assert guard.BIZ_STATUS_FLOW[terminal] == (), f"{terminal} 应是终态"


def test_authoritative_tables_stay_in_sync():
    """# 论证：AUTHORITATIVE_STATES 与 AUTHORITATIVE_EVIDENCE 同增同减。

    漏配不会放行（第 ④ 道见到没有判据的权威终态直接拒），但那时已经是运行期了。
    这条用例把它提前到测试期。
    """
    assert set(guard.AUTHORITATIVE_STATES) == set(guard.AUTHORITATIVE_EVIDENCE)
    ev = guard.AUTHORITATIVE_EVIDENCE["returned"]
    assert ev.message_family == guard.MSG_PAYMENT_RETURN
    assert ev.observed_states == frozenset({guard.OBS_RETURNED})


def test_unconfigured_authoritative_state_is_fail_closed(monkeypatch):
    """给 AUTHORITATIVE_STATES 加一个状态却不给证据判据 → 拒绝，不默认放行。"""
    store = _store()
    _case(store)
    monkeypatch.setattr(guard, "AUTHORITATIVE_STATES",
                        frozenset({"returned", "rejected"}))
    with pytest.raises(guard.AuthoritativeFactViolation) as exc:
        guard.update_biz_status(store, TENANT, CASE, "rejected",
                                guard.AUTHORITATIVE_WRITER, "inv-cfg",
                                observation=_pacs004(observed_state=guard.OBS_REJECTED))
    assert "AUTHORITATIVE_EVIDENCE" in str(exc.value)


# ------------------------------------------------------------- ④ 不留旁路
def test_direct_table_write_is_blocked_at_runtime():
    """# 论证：绕过 guard 直接写 investigation_case 的路径，运行时就被拦。"""
    store = _store()
    _case(store)
    for sql in (
        "UPDATE investigation_case SET biz_status='returned' WHERE tenant_id=?",
        "INSERT INTO investigation_case (tenant_id) VALUES (?)",
        "DELETE FROM investigation_case WHERE tenant_id=?",
        "REPLACE INTO investigation_case (tenant_id) VALUES (?)",
    ):
        with pytest.raises(objects.BypassedGuardError):
            objects.execute(store, sql, (TENANT,))
    assert guard.get_case(store, TENANT, CASE)["biz_status"] == "cancellation_sent"


def test_no_bypass_path_in_source():
    """# 论证：提交进仓库的代码里没有第二条写 returned 的路径。

    运行时那道拦截（`objects.execute`）挡的是旁路调用；这一条挡的是**源码里**
    有没有人绕开 objects 层直接摸连接。两道一起才叫「不留第二条路径」。
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    write_re = re.compile(
        r"(?:INSERT\s+(?:OR\s+\w+\s+)?INTO|UPDATE|DELETE\s+FROM|REPLACE\s+INTO)\s+"
        r"investigation_case\b", re.IGNORECASE)
    offenders = []
    for path in root.rglob("*.py"):
        if path.name in ("guard.py", "test_investigation_guard.py"):
            continue
        if "investigation" not in str(path) and "flows" not in str(path):
            continue
        text = path.read_text(encoding="utf-8")
        if write_re.search(text):
            offenders.append(str(path.relative_to(root)))
    assert not offenders, (
        f"这些文件里有绕开 guard 的 investigation_case 写语句：{offenders}")


def test_observed_state_vocabulary_is_closed():
    """归一口径由 guard 定，各处不许自造取值。"""
    store = _store()
    _case(store)
    with pytest.raises(ValueError) as exc:
        guard.insert_observation(store, tenant_id=TENANT, case_id=CASE,
                                 observation=_pacs004(observed_state="looks_ok"),
                                 invocation_id="inv-v")
    assert "未知的 observed_state" in str(exc.value)


def test_invocation_id_must_be_present():
    """actor 锚点为空就断了整条审计链。"""
    store = _store()
    _case(store)
    with pytest.raises(ValueError):
        guard.update_biz_status(store, TENANT, CASE, "returned",
                                guard.AUTHORITATIVE_WRITER, "",
                                observation=_pacs004())


# ------------------------------------------------------------- ⑤ 建案幂等
def test_create_case_is_idempotent_on_identical_replay():
    """受理重跑不新建、不倒退状态。"""
    store = _store()
    _case(store)
    again = guard.create_case(
        store, tenant_id=TENANT, case_id=CASE, creator_agent="DEUTDEFFXXX",
        assignee_agent="BNPAFRPPXXX", original_msg_id=MSG, original_version=1,
        end_to_end_id="E2E-G1", amount=12500.00, currency="EUR", plan_id=PLAN,
        actor_skill="investigation.file", invocation_id="inv-again")
    assert again["biz_status"] == "cancellation_sent", (
        "重放不许把已经推进的案子倒回 filed")


def test_create_case_conflict_is_loud():
    """同案号不同金额 → 抛，并落一条冲突事件。"""
    store = _store()
    _case(store)
    with pytest.raises(guard.CaseIdentityConflict):
        guard.create_case(
            store, tenant_id=TENANT, case_id=CASE, creator_agent="DEUTDEFFXXX",
            assignee_agent="BNPAFRPPXXX", original_msg_id=MSG, original_version=1,
            end_to_end_id="E2E-G1", amount=99999.00, currency="EUR", plan_id=PLAN,
            actor_skill="investigation.file", invocation_id="inv-conflict")
    evs = [e for e in store.list_event_log(PLAN)
           if e["event_type"] == guard.CASE_CONFLICT_EVENT]
    assert len(evs) == 1
    assert "amount" in evs[0]["detail"]["conflicts"]


def test_create_case_tolerates_int_float_mix():
    """12500 与 12500.0 是同一件事 —— 不归一会让幂等退化成「每次重跑都报冲突」。"""
    store = _store()
    _case(store)
    guard.create_case(
        store, tenant_id=TENANT, case_id=CASE, creator_agent="DEUTDEFFXXX",
        assignee_agent="BNPAFRPPXXX", original_msg_id=MSG, original_version="1",
        end_to_end_id="E2E-G1", amount=12500, currency="EUR", plan_id=PLAN,
        actor_skill="investigation.file", invocation_id="inv-mix")
