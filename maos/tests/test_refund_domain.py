"""退款域地基的行为测试（R-1）。

题眼一条：**settled 是外部权威事实**（铁律 8）。这个文件里最重要的一条是
`test_non_authoritative_writer_cannot_settle` —— 它验的不是「功能做了」，
而是「系统拒绝了一次越权写入，并且把拒绝这件事留了证据」。

题眼二条：**业务状态不是 Task 状态**（铁律 9）。
`test_refund_flow_adds_no_task_state_and_no_transition` 是这条的把守闸：
退款域跑完整条链路，`contracts/states.py` 的状态集合与迁移表必须一个字没多。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from maos.contracts.states import TASK_TRANSITIONS, TaskState, can_transition
from maos.core.store import SqliteStore
from maos.domain.refund import guard, objects

_FIXTURE = Path(__file__).parent / "fixtures" / "refund" / "case_r1.json"

PLAN_ID = "plan-refund-1"


def _seed():
    """建库 + 灌 fixture。返回 (store, fixture)。"""
    data = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    store = SqliteStore()
    store.init_schema()
    objects.ensure_schema(store)
    store.insert_plan({"plan_id": PLAN_ID, "trace_id": "tr-1",
                       "goal": "退款域地基", "state": "PENDING"})

    for table, rows in data.items():
        if table.startswith("_") or table == "case":
            continue
        for row in rows:
            cols = ", ".join(row)
            marks = ", ".join("?" * len(row))
            objects.execute(store, f"INSERT INTO {table} ({cols}) VALUES ({marks})",
                            tuple(row.values()))
    return store, data


def _open_case(store, data, *, case_id: str | None = None, tenant_id: str | None = None):
    c = dict(data["case"])
    if case_id:
        c["case_id"] = case_id
    if tenant_id:
        c["tenant_id"] = tenant_id
    return guard.create_case(store, plan_id=PLAN_ID, actor_skill="refund.intake",
                             invocation_id="iv-intake", **c)


def _push_to_processing(store, tenant_id="acme", case_id="RC-1"):
    """走到 processing —— 这是 payment.execute 能推到的最远处。"""
    for status, actor in (("approved", "approval.decide"),
                          ("gateway_accepted", "payment.execute"),
                          ("processing", "payment.execute")):
        guard.update_biz_status(store, tenant_id, case_id, status, actor, f"iv-{status}")


def _violations(store):
    return [e for e in store.list_event_log(PLAN_ID)
            if e["event_type"] == guard.VIOLATION_EVENT]


# ====================================================================
# 权威事实边界（铁律 8）
# ====================================================================
def test_non_authoritative_writer_cannot_settle():
    """本轮最重要的一条：finance skill 写 settled 必须被拒，且拒绝这件事要留证据。

    「拒了」和「拒了并留下证据」是两回事。静默失败等于把最有说服力的那份材料丢掉。
    """
    store, data = _seed()
    _open_case(store, data)
    _push_to_processing(store)

    with pytest.raises(guard.AuthoritativeFactViolation):
        guard.update_biz_status(store, "acme", "RC-1", "settled",
                                "finance.settle", "iv-finance")

    assert guard.get_case(store, "acme", "RC-1")["biz_status"] == "processing", \
        "越权写入被拒了，状态却变了 —— 那等于没拒"

    evs = _violations(store)
    assert len(evs) == 1, "越权写入必须落一条事件，这是要给评委看的证据"
    d = evs[0]["detail"]
    assert d["actor"] == "finance.settle" and d["attempted"] == "settled"
    assert d["invocation_id"] == "iv-finance", "缺 invocation_id 这条审计链就断了"
    assert d["authoritative_writer"] == guard.AUTHORITATIVE_WRITER


def test_payment_execute_lands_on_processing_not_settled():
    """payment.execute 只是把请求递出去了，不代表钱到账。"""
    store, data = _seed()
    _open_case(store, data)
    _push_to_processing(store)

    case = guard.get_case(store, "acme", "RC-1")
    assert case["biz_status"] == "processing"
    assert case["biz_status"] != "settled", "递出请求就当到账，是最典型的权威事实错位"
    assert objects.query(store, "SELECT * FROM payment_observation") == [], \
        "还没观察到回执，不许有观察记录"


def test_only_observe_settles_and_receipt_is_written_in_same_txn():
    """settled 与回执同生共死：状态是 settled，就一定查得到那份回执。"""
    store, data = _seed()
    _open_case(store, data)
    _push_to_processing(store)

    case = guard.update_biz_status(
        store, "acme", "RC-1", "settled", guard.AUTHORITATIVE_WRITER, "iv-observe",
        observation={"request_id": "REQ-1", "gateway_code": "SUCCESS",
                     "observed_state": "settled",
                     "raw_receipt_json": json.dumps({"trade_no": "2026xxxx"})})

    assert case["biz_status"] == "settled"
    obs = objects.query(store, "SELECT * FROM payment_observation WHERE case_id=?", ("RC-1",))
    assert len(obs) == 1
    assert obs[0]["actor_invocation_id"] == "iv-observe", "回执必须能溯源到是谁观察的"
    assert obs[0]["gateway_code"] == "SUCCESS"


def test_settled_without_receipt_is_refused():
    """连 payment.observe 自己，没带回执也写不进 settled。"""
    store, data = _seed()
    _open_case(store, data)
    _push_to_processing(store)

    with pytest.raises(guard.AuthoritativeFactViolation):
        guard.update_biz_status(store, "acme", "RC-1", "settled",
                                guard.AUTHORITATIVE_WRITER, "iv-observe")

    assert guard.get_case(store, "acme", "RC-1")["biz_status"] == "processing"
    assert len(_violations(store)) == 1, "缺回执同样要留证据"


def test_settled_rolls_back_when_receipt_insert_fails():
    """同事务的反证：回执写不进去，状态也不许留在 settled。

    没有这条，「同事务」就只是注释里的一句话 —— 分两次提交在顺利路径上看不出区别。
    """
    store, data = _seed()
    _open_case(store, data)
    _push_to_processing(store)

    fixed_at = "2026-08-28T10:00:00+00:00"
    objects.execute(
        store,
        "INSERT INTO payment_observation (tenant_id, case_id, request_id, gateway_code,"
        " raw_receipt_json, observed_state, observed_at, actor_invocation_id)"
        " VALUES (?,?,?,?,?,?,?,?)",
        ("acme", "RC-1", "REQ-1", "SUCCESS", "{}", "settled", fixed_at, "iv-earlier"))

    with pytest.raises(sqlite3.IntegrityError):
        guard.update_biz_status(
            store, "acme", "RC-1", "settled", guard.AUTHORITATIVE_WRITER, "iv-observe",
            observation={"request_id": "REQ-1", "gateway_code": "SUCCESS",
                         "observed_state": "settled", "observed_at": fixed_at})

    assert guard.get_case(store, "acme", "RC-1")["biz_status"] == "processing", \
        "回执插入失败了状态却推进了 —— 两条写入不在同一个事务里"


def test_receipt_from_non_authoritative_actor_is_refused():
    """别人递不进回执 —— 否则「必须有回执」就退化成「必须编一份回执」。"""
    store, data = _seed()
    _open_case(store, data)
    _push_to_processing(store)

    with pytest.raises(guard.AuthoritativeFactViolation):
        guard.update_biz_status(
            store, "acme", "RC-1", "compensated", "compensate.execute", "iv-comp",
            observation={"request_id": "REQ-9", "gateway_code": "SUCCESS",
                         "observed_state": "settled"})
    assert objects.query(store, "SELECT * FROM payment_observation") == []


def test_objects_execute_refuses_refund_case_writes():
    """运行时旁路拦截：refund_case 只有 guard 写得动，绕不过去。"""
    store, data = _seed()
    _open_case(store, data)

    # 拦的是**所有**对 refund_case 的写入，不只是写 settled 那一种。
    # 状态值走占位符：既是地道写法，也让派单步骤 5 的旁路自查 grep 保持无输出 ——
    # 那条 grep 找的是「代码里直接写 settled」，一条断言它被拦住的测试不该去顶包。
    attempts = [
        ("UPDATE refund_case SET biz_status=? WHERE case_id='RC-1'", ("settled",)),
        ("UPDATE refund_case SET biz_status=? WHERE case_id='RC-1'", ("approved",)),
        ("insert into refund_case (tenant_id) values (?)", ("acme",)),
        ("DELETE FROM refund_case WHERE case_id='RC-1'", ()),
    ]
    for sql, params in attempts:
        with pytest.raises(objects.BypassedGuardError):
            objects.execute(store, sql, params)

    assert guard.get_case(store, "acme", "RC-1")["biz_status"] == "submitted"


def test_illegal_biz_transition_is_refused():
    """业务状态机也是状态机：submitted 不许一步跳到 settled。"""
    store, data = _seed()
    _open_case(store, data)

    with pytest.raises(guard.BizStatusTransitionError):
        guard.update_biz_status(store, "acme", "RC-1", "processing",
                                "payment.execute", "iv-x")


def test_empty_invocation_id_is_refused():
    """invocation_id 是 actor 溯源的唯一锚点，空了这条链就断了（A-5 保证非空）。"""
    store, data = _seed()
    _open_case(store, data)

    with pytest.raises(ValueError):
        guard.update_biz_status(store, "acme", "RC-1", "approved", "approval.decide", "")


# ====================================================================
# 快照与政策版本
# ====================================================================
def test_policy_match_uses_order_pinned_version_not_latest():
    """按下单当时锁定的政策版本判，不是按 policy_rule 的最新版本判。

    fixture 里 R-01 有 v1/v2 两版，订单锁在 v1。取到 v2 就等于拿今天的规则
    追溯昨天的交易 —— 客户是照着当时公示的政策下的单。
    """
    store, data = _seed()

    assert objects.pinned_policy_version(
        store, tenant_id="acme", order_id="ORD-1001", order_version=1) == 1

    rules = objects.policy_rules_at_order(
        store, tenant_id="acme", order_id="ORD-1001", order_version=1)
    got = {(r["rule_no"], r["version"]) for r in rules}

    assert ("R-01", 1) in got
    assert ("R-01", 2) not in got, "取到了订单之后才发布的政策版本"
    assert ("R-02", 2) not in got, "R-02 只存在于 v2，订单锁在 v1 时不该适用"
    assert ("R-03", 1) not in got, "channel_scope=offline 不该命中 online 订单"
    assert ("R-04", 1) not in got, "生效区间在下单前就结束了"
    assert got == {("R-01", 1)}


def test_cross_tenant_isolation_on_policy_rules():
    """租户 A 读不到租户 B 的政策 —— 租户是主键的一部分，不是 WHERE 上的君子协定。"""
    store, _ = _seed()

    rules = objects.policy_rules_at_order(
        store, tenant_id="acme", order_id="ORD-1001", order_version=1)
    assert all(r["tenant_id"] == "acme" for r in rules)
    assert all(r["body"] != "不许被 acme 读到。" for r in rules)

    with pytest.raises(LookupError):
        objects.pinned_policy_version(
            store, tenant_id="rival", order_id="ORD-1001", order_version=1)


# ====================================================================
# 业务引用与建表
# ====================================================================
def test_business_ref_points_at_a_real_object_with_matching_version():
    """business_ref 只存引用；引用得指得到，且版本要对得上。"""
    store, data = _seed()
    _open_case(store, data)

    ref = objects.attach_business_ref(
        store, plan_id=PLAN_ID, task_id="t-intake", tenant_id="acme",
        object_type="order_snapshot", object_id="ORD-1001", object_version=1,
        purpose="退款依据的订单快照")
    assert objects.resolve_business_ref(store, ref)["sku"] == "SKU-9"

    stale = dict(ref, object_version=2)
    assert objects.resolve_business_ref(store, stale) is None, "版本对不上必须指不到"

    case_ref = objects.attach_business_ref(
        store, plan_id=PLAN_ID, task_id="t-intake", tenant_id="acme",
        object_type="refund_case", object_id="RC-1", purpose="本次退款案例")
    assert objects.resolve_business_ref(store, case_ref)["case_id"] == "RC-1"

    rows = objects.list_business_refs(store, plan_id=PLAN_ID, task_id="t-intake")
    assert len(rows) == 2
    assert all("payload_json" not in r for r in rows), "business_ref 只存引用，不存副本"


def test_ensure_schema_is_idempotent():
    """连跑两次不炸，且不清掉已有数据。"""
    store, data = _seed()
    _open_case(store, data)

    objects.ensure_schema(store)
    objects.ensure_schema(store)

    assert guard.get_case(store, "acme", "RC-1") is not None
    tables = {r["name"] for r in objects.query(
        store, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"refund_case", "payment_observation", "business_ref"} <= tables


# ====================================================================
# 领域无关的把守闸（铁律 9）
# ====================================================================
def test_refund_flow_adds_no_task_state_and_no_transition():
    """整条退款链路跑完，Task 状态机一个字没多。

    这条不是形式主义：退款域最自然的写法就是给 states.py 加一个 `SETTLED`，
    加了就等于把业务状态塞进内核，「领域无关」当场不成立。
    """
    states_before = {v for k, v in vars(TaskState).items()
                     if isinstance(v, str) and not k.startswith("_")}
    trans_before = dict(TASK_TRANSITIONS)

    store, data = _seed()
    _open_case(store, data)
    _push_to_processing(store)
    guard.update_biz_status(
        store, "acme", "RC-1", "settled", guard.AUTHORITATIVE_WRITER, "iv-observe",
        observation={"request_id": "REQ-1", "gateway_code": "SUCCESS",
                     "observed_state": "settled"})

    states_after = {v for k, v in vars(TaskState).items()
                    if isinstance(v, str) and not k.startswith("_")}
    assert states_after == states_before, "退款域给 Task 状态机加了新状态"
    assert dict(TASK_TRANSITIONS) == trans_before, "退款域给 Task 迁移表加了新迁移"

    assert not (set(guard.BIZ_STATUS_FLOW) & states_before), \
        "业务状态与 Task 状态撞名了 —— 两套词汇必须分得开"

    # 承载这条业务链路的 Task，全程走的是既有迁移。
    for src, dst in ((TaskState.PENDING, TaskState.DISPATCHED),
                     (TaskState.DISPATCHED, TaskState.RUNNING),
                     (TaskState.RUNNING, TaskState.AWAITING_REVIEW),
                     (TaskState.AWAITING_REVIEW, TaskState.DONE)):
        assert can_transition(src, dst), f"{src}->{dst} 竟然不在既有迁移表里"
