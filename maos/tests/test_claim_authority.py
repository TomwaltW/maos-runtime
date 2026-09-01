"""理赔域的权威事实边界（铁律 8）—— 越权写 paid 的每一条路都要被堵死。

本文件是本域最硬的一组断言。它要买的是这句话：

    全系统只有 `claim.observe` 写得进 `paid`，而且越权**不静默失败**。

「不静默失败」是判据的一半：一个悄悄返回 False 的守卫和没有守卫是一回事 ——
系统拒绝了一次越权写入这件事本身就是要拿给评委看的证据，吞掉就没了。
所以每条越权用例都同时校**抛了异常**和**落了事件**。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from maos.core.store import SqliteStore
from maos.domain.claim import guard, objects

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
MAOS_PKG = REPO_ROOT / "maos"

TENANT = "tnt-test"
CLAIM = "clm-test-0001"
PLAN = "plan-test"


def _store():
    st = SqliteStore()
    st.init_schema()
    objects.ensure_schema(st)
    return st


def _case(st, *, claim_id: str = CLAIM, amount: float = 12000.0) -> dict:
    return guard.create_case(
        st, tenant_id=TENANT, claim_id=claim_id, payer_id="payer-1",
        policy_no="POL-1", policy_version=1, loss_type="illness",
        incident_at="2026-06-20T00:00:00+00:00", amount_claimed=amount,
        plan_id=PLAN, actor_skill="claim.intake", invocation_id="inv-seed")


def _violations(st) -> list[dict]:
    return [e for e in st.list_event_log(PLAN)
            if e["event_type"] == guard.VIOLATION_EVENT]


def _advance_to_requested(st) -> None:
    guard.update_biz_status(st, TENANT, CLAIM, "adjudicated", "claim.adjudicate", "i1")
    guard.update_biz_status(st, TENANT, CLAIM, "payment_requested", "claim.pay", "i2")


# ---------------------------------------------------------------- 越权写入
@pytest.mark.parametrize("actor", ["claim.pay", "claim.settle", "claim.compensate",
                                   "claim.adjudicate", "payment.observe"])
def test_only_claim_observe_can_write_paid(actor):
    """论证：任何别的 actor 写 paid 都抛，且落一条**指名是权威闸拦的**事件。

    `payment.observe` 也在参数表里，而且是最要紧的一个：它是**退款域**的权威写入方。
    两个域各有各的边界，退款域的权威写入方在理赔域一样没有资格 —— 漏了这一条，
    「两个域的边界互不越界」就只是巧合。

    🔴 **这条用例刻意不递 observation，而且要校事件 reason。** 变异检验（M1）当场
    发现的坑：原先的写法递了一份回执，于是即使把第 ① 道权威闸整个短路掉，
    第 ② 道「回执只能由权威写入方提交」照样抛同一个异常类型 —— 用例绿，
    但**测到的不是它以为的那道闸**。只断言「抛了 AuthoritativeFactViolation」
    不足以区分四道闸，必须按落下的 reason 认人。
    """
    st = _store()
    _case(st)
    _advance_to_requested(st)
    before = len(_violations(st))

    with pytest.raises(guard.AuthoritativeFactViolation, match="试图把"):
        guard.update_biz_status(st, TENANT, CLAIM, "paid", actor, "inv-x")

    events = _violations(st)
    assert len(events) == before + 1, (
        "越权写入必须落一条 AuthoritativeFactViolation 事件 —— "
        "「系统拒绝了一次越权写入」本身就是证据，吞掉就没了")
    assert events[-1]["reason"] == f"paid 只能由 {guard.AUTHORITATIVE_WRITER} 写入", (
        f"拦下它的应当是第 ① 道权威闸，实际 reason={events[-1]['reason']!r} —— "
        "被别的闸接住说明这条用例测的不是它以为的那道闸")
    assert events[-1]["detail"]["actor"] == actor
    assert guard.get_case(st, TENANT, CLAIM)["biz_status"] == "payment_requested"


@pytest.mark.parametrize("actor", ["claim.pay", "payment.observe"])
def test_non_authoritative_actor_is_stopped_before_the_receipt_check(actor):
    """论证：越权方**同时**递了回执时，拦下它的仍然是第 ① 道，不是第 ② 道。

    顺序有意义：①「你没资格写这个状态」比②「你没资格递回执」更根本，也更该出现在
    审计里。反过来的话，一次「冒充权威写入方」的试探会被记成一句关于回执的话。
    """
    st = _store()
    _case(st)
    _advance_to_requested(st)
    with pytest.raises(guard.AuthoritativeFactViolation):
        guard.update_biz_status(st, TENANT, CLAIM, "paid", actor, "inv-x",
                                observation={"request_id": "r1", "observed_state": "paid"})
    assert _violations(st)[-1]["reason"] == f"paid 只能由 {guard.AUTHORITATIVE_WRITER} 写入"


def test_violation_logged_even_when_case_does_not_exist():
    """论证：对一个不存在的案子越权写 paid，照样留痕。

    权威闸排在存在性检查**之前**是有意的：先查存在性会让这种试探以 LookupError
    收场，证据就没了 —— 而那恰恰是最该留痕的一种。
    """
    st = _store()
    with pytest.raises(guard.AuthoritativeFactViolation):
        guard.update_biz_status(st, TENANT, "clm-not-exist", "paid", "claim.pay", "inv-x")
    events = [e for e in st.list_event_log("") if e["event_type"] == guard.VIOLATION_EVENT]
    assert len(events) == 1, "对不存在的案子越权也必须留痕"


def test_receipt_can_only_be_submitted_by_authoritative_writer():
    """论证：回执只有权威写入方递得进来，否则等于给别人开了个伪造回执的口子。"""
    st = _store()
    _case(st)
    with pytest.raises(guard.AuthoritativeFactViolation):
        guard.update_biz_status(st, TENANT, CLAIM, "adjudicated", "claim.adjudicate", "i1",
                                observation={"request_id": "r1", "observed_state": "paid"})
    assert len(_violations(st)) == 1


def test_paid_without_receipt_is_refused():
    """论证：没有回执的 paid 就是把外部状态写死为终态。"""
    st = _store()
    _case(st)
    _advance_to_requested(st)
    with pytest.raises(guard.AuthoritativeFactViolation, match="必须同事务附回执"):
        guard.update_biz_status(st, TENANT, CLAIM, "paid", guard.AUTHORITATIVE_WRITER, "i3")
    assert len(_violations(st)) == 1


def test_paid_with_a_receipt_that_says_denied_is_refused():
    """论证：**有一张回执 != 回执说到账了**。这是第 ④ 道闸，也是最容易漏的一道。

    一条 observed_state='denied'、carc_code='96' 的回执字段齐全，在「有没有回执」
    那一道眼里与到账回执无从分辨。放过它，系统持有的就只是「有一张回执」。
    """
    st = _store()
    _case(st)
    _advance_to_requested(st)
    with pytest.raises(guard.AuthoritativeFactViolation, match="不等于"):
        guard.update_biz_status(
            st, TENANT, CLAIM, "paid", guard.AUTHORITATIVE_WRITER, "i3",
            observation={"request_id": "r1", "observed_state": "denied",
                         "carc_code": "96"})
    assert len(_violations(st)) == 1
    assert objects.query(st, "SELECT COUNT(*) AS n FROM claim_payment_observation"
                         )[0]["n"] == 0, "被拒的写入不许留下半条观察"


def test_authoritative_tables_grow_together():
    """论证：`AUTHORITATIVE_STATES` 与 `AUTHORITATIVE_RECEIPT_STATE` 同增同减。

    漏配的后果不是报错，是那个终态**退回到「有回执就算数」** —— 静默且没人会发现。
    这条断言就是那张表的哨兵：加一个权威终态而忘了配判据，这里当场红。
    """
    assert guard.AUTHORITATIVE_STATES == set(guard.AUTHORITATIVE_RECEIPT_STATE), (
        f"两张表必须同增同减：AUTHORITATIVE_STATES={sorted(guard.AUTHORITATIVE_STATES)}、"
        f"AUTHORITATIVE_RECEIPT_STATE={sorted(guard.AUTHORITATIVE_RECEIPT_STATE)}")


def test_paid_writes_receipt_and_status_in_one_transaction():
    """论证：合法写入时，观察行与状态更新同事务落库。"""
    st = _store()
    _case(st)
    _advance_to_requested(st)
    case = guard.update_biz_status(
        st, TENANT, CLAIM, "paid", guard.AUTHORITATIVE_WRITER, "inv-ok",
        observation={"request_id": "r1", "observed_state": "paid", "carc_code": ""})
    assert case["biz_status"] == "paid"
    rows = objects.query(st, "SELECT * FROM claim_payment_observation")
    assert len(rows) == 1
    assert rows[0]["observed_state"] == "paid"
    assert rows[0]["actor_invocation_id"] == "inv-ok", (
        "观察行必须指得回是哪一次调用问出来的，否则审计链断了")


# ------------------------------------------------------- 没有第二条写入路径
def test_no_bypass_writes_paid():
    """论证：全仓库只有 claim.observe 调得出 `update_biz_status(..., "paid", ...)`。

    等价于提交前那条 grep：
        grep -rn "biz_status.*=.*'paid'" maos/ | grep -v guard.py | grep -v observe
    做成测试是因为 grep 只在有人想起来跑的时候才拦得住。

    判据走 AST 而不是文本匹配：按行 grep 会把 docstring 里「本 skill 写不出 paid」
    这类散文当成违规，于是这条断言迟早被人改宽或删掉 —— 一条会误报的守卫等于没有守卫。
    （退款域 `test_refund_flow.py::test_no_bypass_writes_settled` 踩过这个坑。）
    """
    allowed = {"maos/skills/builtin/claim/observe.py"}
    offenders = []
    for path in sorted(MAOS_PKG.rglob("*.py")):
        rel = str(path.relative_to(REPO_ROOT))
        if rel in allowed or rel.startswith("maos/tests/"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name != "update_biz_status":
                continue
            args = list(node.args) + [kw.value for kw in node.keywords]
            if any(isinstance(a, ast.Constant) and a.value == "paid" for a in args):
                offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, f"paid 的写入出现在预期之外的位置：{offenders}"


def test_claim_case_writes_have_no_bypass():
    """论证：没有第二条写 claim_case 的路径 —— 运行时拦截也在（不只是 grep）。"""
    st = _store()
    with pytest.raises(objects.BypassedGuardError):
        objects.execute(st, "UPDATE claim_case SET biz_status='paid' WHERE claim_id=?",
                        (CLAIM,))
    with pytest.raises(objects.BypassedGuardError):
        objects.execute(st, "INSERT INTO claim_case (tenant_id, claim_id) VALUES (?,?)",
                        (TENANT, CLAIM))
    with pytest.raises(objects.BypassedGuardError):
        objects.execute(st, "DELETE FROM claim_case WHERE claim_id=?", (CLAIM,))
    with pytest.raises(objects.BypassedGuardError):
        objects.execute(st, "REPLACE INTO claim_case (tenant_id) VALUES (?)", (TENANT,))


def test_alter_table_is_not_blocked_by_the_bypass_guard():
    """论证：拦的是**旁路写入**，不是正常迁移。

    `ALTER TABLE claim_case ADD COLUMN` 是加列，不是绕开 guard 改数据。把它一并拦掉
    会让本域从此加不了列，而那不是这道守卫要买的东西。
    """
    st = _store()
    objects.execute(st, "ALTER TABLE claim_case ADD COLUMN probe_col TEXT")
    assert objects._has_column(st, "claim_case", "probe_col")


# ------------------------------------------------------------ 业务状态机
def test_biz_status_flow_has_no_edge_into_paid_except_from_payment_requested():
    """论证：`paid` 只有一条入边，来自 `payment_requested`。

    多一条入边就多一条「还没发出赔付指令就能收口」的路。
    """
    into_paid = [src for src, dsts in guard.BIZ_STATUS_FLOW.items() if "paid" in dsts]
    assert into_paid == ["payment_requested"], (
        f"paid 的入边应当只有 payment_requested，实际 {into_paid}")


def test_terminal_biz_statuses_have_no_outgoing_edges():
    """论证：三个终态都是真终态，走进去出不来。"""
    for terminal in ("paid", "rejected", "compensated"):
        assert guard.BIZ_STATUS_FLOW[terminal] == (), (
            f"{terminal} 应当是终态，实际还能去 {guard.BIZ_STATUS_FLOW[terminal]}")


def test_illegal_transition_raises():
    st = _store()
    _case(st)
    with pytest.raises(guard.BizStatusTransitionError):
        guard.update_biz_status(st, TENANT, CLAIM, "payment_requested", "claim.pay", "i1")


# ------------------------------------------------------------------ 幂等
def test_create_case_is_idempotent_on_identical_replay():
    """论证：报案重跑不建出第二个案子，也不把已推进的案子倒回 submitted。"""
    st = _store()
    first = _case(st)
    guard.update_biz_status(st, TENANT, CLAIM, "adjudicated", "claim.adjudicate", "i1")
    again = _case(st)
    assert again["created_at"] == first["created_at"]
    assert again["biz_status"] == "adjudicated", (
        "幂等重放**不许**把已经推进的案子静悄悄倒回 submitted —— "
        "那比裸 INSERT 抛异常坏得多")
    assert objects.query(st, "SELECT COUNT(*) AS n FROM claim_case")[0]["n"] == 1


def test_create_case_conflict_is_loud():
    """论证：同一个案号上来一份业务字段不同的报案 -> 抛 + 落事件，不静默。"""
    st = _store()
    _case(st)
    with pytest.raises(guard.CaseIdentityConflict):
        _case(st, amount=99999.0)
    events = [e for e in st.list_event_log(PLAN)
              if e["event_type"] == guard.CASE_CONFLICT_EVENT]
    assert len(events) == 1
    assert "amount_claimed" in str(events[0]["detail"])


def test_replay_with_int_float_mix_is_still_idempotent():
    """论证：`12000` 与 `12000.0` 不算冲突。

    不做类型归一的话，每一次正常重跑都会判成冲突，幂等当场退化。
    """
    st = _store()
    first = _case(st, amount=12000)
    again = _case(st, amount=12000.0)
    assert again["created_at"] == first["created_at"]


def test_invocation_id_must_not_be_empty():
    """论证：actor 锚点空了这条审计链就断了，所以空了直接抛。"""
    st = _store()
    with pytest.raises(ValueError, match="invocation_id"):
        guard.create_case(
            st, tenant_id=TENANT, claim_id="clm-x", payer_id="p", policy_no="P",
            policy_version=1, loss_type="illness", incident_at="2026-01-01T00:00:00+00:00",
            amount_claimed=1.0, plan_id=PLAN, actor_skill="claim.intake",
            invocation_id="")
