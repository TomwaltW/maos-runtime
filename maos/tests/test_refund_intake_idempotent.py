"""`refund.intake` 在返工重跑下的幂等回归（H-3）。

题眼：`refund_case` 的主键是 `(tenant_id, case_id)`，而受理这一步会被返工重跑。
裸 `INSERT` 的后果不是「重复建了两个案子」，是**当场 IntegrityError**
（`UNIQUE constraint failed: refund_case.tenant_id, refund_case.case_id`），
任务耗尽 attempt 后 FAILED —— 这个坑先于任何一道闸存在，只是过去没有触发路径。

三条判据，缺一不可，而且**三条一起才钉得住语义**：

  1. 同一份 `case_seed` 重跑 → 幂等返回既有行，库里仍是一行；
  2. 案子已经推进过（`approved`）再重跑 → `biz_status` 不许被打回 `submitted`。
     这一条是 `INSERT OR REPLACE` 的照妖镜 —— 那种写法第 1 条绿、第 2 条红，
     而它坏得比抛异常更深：重跑把已经推进过的案子**静悄悄**倒回受理；
  3. 重跑带了**不同的** `amount_claimed` → 必须抛，不许静默收下也不许静默丢弃。
     两种静默走法都错：`DO UPDATE` 让新金额进库，而第六道财务复核闸的两侧判据
     都只读 `task["inputs"]`（`maos/runtime/gate.py` 的 plan 级与任务级同一个
     `FINANCE_AMOUNT_FIELD`），库里那份金额闸根本不看；真正付钱的
     `finance.settle` 读的却正是库里那份（`finance.py` 的 `case["amount_claimed"]`）
     —— 闸按旧金额过、钱按新金额出，闸就是这么被绕过去的。
     `DO NOTHING` 反过来：库里留旧金额，调用方拿到一份与自己递进来的 seed
     不一致的 `case_draft`，同样一点信号都没有。
     所以「同一个 case_id 上换了业务字段」必须响 —— 与 `guard.py` 第 ④ 道
     「有回执 ≠ 回执说到账了」同一个 fail-closed 口径。
"""

from __future__ import annotations

import pytest

from maos.agents.base import AgentIdentity
from maos.core.store import SqliteStore
from maos.domain.refund import guard, objects
from maos.flows import scenario_6 as s6
from maos.model.client import Tier
from maos.skills.builtin.refund import REFUND_SKILLS
from maos.skills.invoker import SkillInvoker

PLAN_ID = "plan-h3-idem"

IDENTITY = AgentIdentity(
    agent_id="test-h3",
    role="test_refund_intake",
    duty="测试夹具：受理幂等回归",
    allowed_skills=frozenset(set(REFUND_SKILLS) | {"issue.aggregate"}),
    allowed_tools=frozenset(),
    write_scope=frozenset({"artifact"}),
    max_risk="M",
    model_tier=Tier.LIGHT,
)


@pytest.fixture
def store():
    st = SqliteStore()
    st.init_schema()
    # 靶场数据复用场景 6 那一份：与 test_refund_flow 同源，金额断言不会两边打架。
    s6.seed_domain(st)
    st.insert_plan({"plan_id": PLAN_ID, "trace_id": "tr-h3",
                    "goal": "受理幂等回归", "state": "PENDING"})
    return st


def _seed(**over) -> dict:
    seed = {
        "tenant_id": s6.TENANT_ID, "case_id": s6.CASE_ID, "channel_id": s6.CHANNEL_ID,
        "order_id": s6.ORDER_ID, "order_version": s6.ORDER_VERSION, "sku": s6.SKU,
        "reason_code": "quality_defect", "amount_claimed": s6.AMOUNT_CLAIMED,
    }
    seed.update(over)
    return seed


def _create(store, *, invocation_id="iv-1", **over) -> dict:
    return guard.create_case(store, plan_id=PLAN_ID, actor_skill="refund.intake",
                             invocation_id=invocation_id, **_seed(**over))


def _rows(store) -> list[dict]:
    return objects.query(store, "SELECT * FROM refund_case WHERE tenant_id=? AND case_id=?",
                         (s6.TENANT_ID, s6.CASE_ID))


# ======================================================================
# 1. 同一份 seed 重跑：幂等
# ======================================================================
def test_create_case_is_idempotent_on_replay(store):
    """返工重跑受理 = 同一份 case_seed 再来一次。不许抛，也不许建出第二行。"""
    first = _create(store, invocation_id="iv-attempt-1")
    second = _create(store, invocation_id="iv-attempt-2")

    assert len(_rows(store)) == 1, "重跑不许建出第二行"
    # 返回的必须是**既有那一行**，含第一次受理的时刻 —— created_at 是「什么时候
    # 收到这笔诉求」，不是「最后一次重跑的时刻」。
    assert second == first
    assert second["created_at"] == first["created_at"]
    assert second["biz_status"] == guard.INITIAL_STATUS


def test_create_case_replay_survives_three_attempts(store):
    """三次 attempt 连着重跑都不许炸 —— 任务的 attempt 上限就是 3。"""
    rows = [_create(store, invocation_id=f"iv-attempt-{i}") for i in (1, 2, 3)]
    assert len({r["created_at"] for r in rows}) == 1
    assert len(_rows(store)) == 1


# ======================================================================
# 2. 已推进过的案子重跑：不许倒回受理
# ======================================================================
def test_replay_after_advance_does_not_reset_biz_status(store):
    """`INSERT OR REPLACE` 的照妖镜：重跑不许把 approved 的案子打回 submitted。

    业务状态一旦被推进，它就不再是受理这一步的产物了。让重跑覆盖它，等于把
    「案子走到哪儿了」这件事交给一次重试去决定 —— 比抛异常坏得多（BACKLOG
    `## task-D2` 第 1 条已点名不建议 `INSERT OR REPLACE`，这条测试把它钉死）。
    """
    _create(store, invocation_id="iv-attempt-1")
    guard.update_biz_status(store, s6.TENANT_ID, s6.CASE_ID, "approved",
                            "approval.decide", "iv-approve")

    replayed = _create(store, invocation_id="iv-attempt-2")

    assert replayed["biz_status"] == "approved", "重跑受理把已推进的案子倒回去了"
    assert len(_rows(store)) == 1
    assert _rows(store)[0]["biz_status"] == "approved"


# ======================================================================
# 3. 换了业务字段：必须响
# ======================================================================
def test_replay_with_different_amount_is_rejected(store):
    """同一个 case_id 换了申报金额 —— 那不是重放，是两件事撞了同一个号。

    这条不许静默：库里那份金额是 `finance.settle` 真正拿去算钱的输入，
    而第六道闸只按 `task["inputs"]` 触发、看不见库里的改动。
    """
    first = _create(store, invocation_id="iv-attempt-1")
    bigger = float(s6.AMOUNT_CLAIMED) + 5000.0

    with pytest.raises(guard.CaseIdentityConflict) as exc:
        _create(store, invocation_id="iv-attempt-2", amount_claimed=bigger)

    # 报错要指名道姓说是哪个字段对不上，否则排查时只知道「冲突了」。
    assert "amount_claimed" in str(exc.value)
    # 库里那一行一个字节都不许动。
    row = _rows(store)[0]
    assert row["amount_claimed"] == first["amount_claimed"]
    assert len(_rows(store)) == 1

    # 拒绝这件事本身要留证据 —— 同 guard.py 模块 docstring：吞掉就没了。
    events = [e for e in store.list_event_log(PLAN_ID)
              if e["event_type"] == guard.CASE_CONFLICT_EVENT]
    assert len(events) == 1, "冲突被拒绝了，但没留下证据"


@pytest.mark.parametrize("field, value", [
    ("order_id", "ORD-OTHER"),
    ("order_version", 99),
    ("sku", "SKU-OTHER"),
    ("channel_id", "another-channel"),
    ("reason_code", "changed_mind"),
])
def test_replay_with_different_business_field_is_rejected(store, field, value):
    """金额之外的业务字段同理：换了任何一个，都不再是「同一件事的重放」。"""
    _create(store, invocation_id="iv-attempt-1")
    with pytest.raises(guard.CaseIdentityConflict) as exc:
        _create(store, invocation_id="iv-attempt-2", **{field: value})
    assert field in str(exc.value)


def test_replay_from_another_plan_is_rejected(store):
    """换了 plan 也算冲突：两个 Plan 同时推进一个案子的 biz_status 没有正确解。"""
    _create(store, invocation_id="iv-attempt-1")
    store.insert_plan({"plan_id": "plan-h3-other", "trace_id": "tr-h3b",
                       "goal": "另一个计划", "state": "PENDING"})
    with pytest.raises(guard.CaseIdentityConflict):
        guard.create_case(store, plan_id="plan-h3-other", actor_skill="refund.intake",
                          invocation_id="iv-other", **_seed())


def test_invocation_id_still_required_on_replay(store):
    """幂等不是放松校验：重放照样要 actor 锚点，否则审计链断在重试上。"""
    _create(store, invocation_id="iv-attempt-1")
    with pytest.raises(ValueError):
        _create(store, invocation_id="")


# ======================================================================
# 4. 端到端：经 SkillInvoker 重跑 refund.intake
# ======================================================================
def _invoke_intake(store, **over):
    invoker = SkillInvoker(IDENTITY, store)
    return invoker.invoke(
        "refund.intake",
        {"signals": s6.SIGNALS, "case_seed": _seed(**over)},
        extras={"plan_id": PLAN_ID, "task_id": "t-intake", "trace_id": "tr-h3",
                "attempt": 1},
    )


def test_intake_skill_rerun_is_ok(store):
    """真正会被返工重跑的是这一层：`refund.intake` 整个 skill 再跑一遍。"""
    first = _invoke_intake(store)
    assert first.status == "ok", first.error

    second = _invoke_intake(store)
    assert second.status == "ok", second.error
    assert second.output["case_draft"] == first.output["case_draft"]
    assert len(_rows(store)) == 1
