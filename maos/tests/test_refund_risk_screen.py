"""风险反欺诈岗 —— `refund.risk_screen` 与 `RefundRiskAgent` 的契约测试。

本文件守三件事：

- **形状**：出参五个键、六个信号逐字，与跨轨契约一致。圆桌那边按**名字**
  `registry.get("refund.risk_screen")` 取本 skill，取不到就退化成 `level: "unavailable"`
  且不报错 —— 所以键名错一个，房间里风险岗会安静地说错话，没有任何东西变红。
  这几条断言就是那个静默失效的替代品。
- **判据出自类属性**：`test_thresholds_are_class_attributes_that_change_the_level`
  改一个阈值就让分档翻面，证明分档读的是属性、不是某个 if 里写死的数。
- **确定性**：同一份入参连跑两次，除 `invocation_id` 外逐字一致。风控结论要能事后复算，
  否则它在争议里一文不值。

入参全部在本文件里现造：本 skill 只经入参吃数、不读任何文件，所以测试也不读
`scenarios/**`（那份底账归另一轨，跟着它改会把两轨绑死）。同理不需要 store ——
`SkillInvoker(identity, None)` 直接调，形态同 `test_skills.py:96`。
"""

from __future__ import annotations

import json
import pathlib

import pytest

from maos.agents.base import AGENT_POOL, AgentIdentity, PermissionDenied, TaskContext
from maos.agents.refund import REFUND_ROLES
from maos.agents.refund.risk_agent import KIND_RISK_REPORT, RefundRiskAgent
from maos.model.client import ScriptedModelClient, Tier
from maos.skills import registry
from maos.skills.builtin.refund import REFUND_SKILLS
from maos.skills.builtin.refund.risk_screen import RefundRiskScreenSkill
from maos.skills.contract import SkillContext
from maos.skills.invoker import SkillInvoker

SKILL = "refund.risk_screen"
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

TENANT = "tnt-demo"
ORDER_ID = "ORD-2026-0004"
CUSTOMER_ID = "CUS-9001"
REQUESTED_AT = "2026-09-03T10:00:00+00:00"

#: 契约里那五个出参键与六个信号键 —— 抄在这里是有意的：
#: 从被测代码里取会让「键名改了」和「断言跟着改了」互相掩护。
OUTPUT_KEYS = {"level", "score", "reasons", "signals", "invocation_id"}
SIGNAL_KEYS = {"duplicate_refund", "already_refunded", "frequency_30d",
               "amount_ratio", "multi_order_same_account", "amount_over_paid"}

# 授权本域全部 skill：这里要验的是 skill 自己的行为，不是白名单。
# 越权那条路由下面 test_identity_without_risk_screen_is_denied 单独守。
ALL_SKILLS_IDENTITY = AgentIdentity(
    agent_id="test-refund-risk",
    role="test_refund_risk",
    duty="测试夹具：授权退款域全部 skill",
    allowed_skills=frozenset(set(REFUND_SKILLS)),
    allowed_tools=frozenset(),
    write_scope=frozenset({"artifact"}),
    max_risk="M",
    model_tier=Tier.LIGHT,
)


@pytest.fixture
def invoker() -> SkillInvoker:
    """无 store 的 invoker —— 本 skill 是纯函数，有没有库都该跑得出同一个结论。"""
    return SkillInvoker(ALL_SKILLS_IDENTITY, None)


# ---------------------------------------------------------------- 入参构造件
def _order(**over) -> dict:
    """一行 order_snapshot。客户标识按底账口径藏在 `payload_json` 字符串里。"""
    row = {
        "tenant_id": TENANT, "order_id": ORDER_ID, "version": 1, "sku": "SKU-BRG-6204",
        "amount_paid": 1000.0, "paid_at": "2026-08-01T10:00:00+00:00",
        "channel_id": "ch-online", "policy_version_at_order": 1,
        "payload_json": json.dumps({"customer_id": CUSTOMER_ID}, ensure_ascii=False),
        "read_at": "2026-09-01T00:00:00+00:00",
    }
    row.update(over)
    return row


def _seed(**over) -> dict:
    row = {
        "tenant_id": TENANT, "case_id": f"RC-{ORDER_ID}", "channel_id": "ch-online",
        "order_id": ORDER_ID, "order_version": 1, "sku": "SKU-BRG-6204",
        "reason_code": "quality_defect", "amount_claimed": 1000.0,
    }
    row.update(over)
    return row


def _history(case_id: str, *, order_id: str = ORDER_ID, status: str = "settled",
             decided_at: str | None = "2026-08-20T10:00:00+00:00",
             customer_id: str = CUSTOMER_ID, amount: float = 500.0) -> dict:
    return {"tenant_id": TENANT, "case_id": case_id, "order_id": order_id,
            "customer_id": customer_id, "amount": amount, "status": status,
            "decided_at": decided_at}


def _payload(**over) -> dict:
    """一份干净入参：单笔订单、零退款历史、申报金额等于实付。"""
    order = over.pop("order", None) or _order()
    payload = {
        "case_seed": _seed(),
        "order": order,
        "customer_orders": [order],
        "refund_history": [],
        "requested_at": REQUESTED_AT,
        "customer_id": CUSTOMER_ID,
    }
    payload.update(over)
    return payload


def _run(invoker: SkillInvoker, payload: dict) -> dict:
    res = invoker.invoke(SKILL, payload)
    assert res.status == "ok", res.error
    assert isinstance(res.output, dict)
    return res.output


# ====================================================================== 注册
def test_risk_screen_and_risk_agent_are_registered_by_file_drop():
    """投放即注册：skill 进注册表、Agent 进 AGENT_POOL，且 `REFUND_ROLES` 一个字没动。

    最后一条是 R6 的机器判据：`test_refund_flow.py` 对 `REFUND_ROLES` 是**等值断言**，
    把新角色加进去当场红（先例：`refund_channel` 也不在里面）。
    """
    cls = registry.get(SKILL)
    assert cls is not None, f"{SKILL} 没进注册表 —— 包的 __init__ 少了那行 import"
    assert cls.contract.version == "1.0.0"
    assert cls.contract.owner_roles == ["refund_risk"]
    assert cls.contract.security_boundary, "契约必填：缺 security_boundary"
    assert SKILL in REFUND_SKILLS, "REFUND_SKILLS 少了一项，遍历断言扫不到本 skill"

    assert AGENT_POOL.get("refund_risk") is RefundRiskAgent, \
        "refund_risk 没按 role 注册进 AGENT_POOL"
    assert "refund_risk" not in REFUND_ROLES, \
        "REFUND_ROLES 被动了 —— 它是等值断言，加了本角色会把存量测试打红"


def test_output_keys_match_the_contract(invoker):
    """出参五键、信号六键逐字，`score` 是 0–100 的 int。

    圆桌按名取本 skill，取不到才退化 —— 取到了却键名不对，它会照常发言并说错话。
    """
    out = _run(invoker, _payload())
    assert set(out) == OUTPUT_KEYS
    assert set(out["signals"]) == SIGNAL_KEYS
    assert isinstance(out["score"], int) and 0 <= out["score"] <= 100
    assert out["level"] in ("low", "medium", "high")
    assert isinstance(out["reasons"], list)
    assert isinstance(out["signals"]["amount_ratio"], float)


# ====================================================================== 信号
def test_clean_case_scores_low_with_no_reasons(invoker):
    """干净单：0 分、low、零理由，六个信号全在基线值上。

    这条是所有加分断言的对照组 —— 没有它，「命中了信号」与「本来就恒 high」分不开。
    """
    out = _run(invoker, _payload())
    assert out["level"] == "low"
    assert out["score"] == 0
    assert out["reasons"] == []
    assert out["signals"] == {
        "duplicate_refund": False,
        "already_refunded": False,
        "frequency_30d": 0,
        "amount_ratio": 1.0,
        "multi_order_same_account": 1,
        "amount_over_paid": False,
    }


def test_pending_history_on_same_order_flags_duplicate_and_is_at_least_medium(invoker):
    """同一单已有 pending 记录 = 重复退款；已足以进 medium 档。

    `already_refunded` 必须仍是 False：钱还没出去，把「在退」说成「已退」是本域
    最贵的一种口径错误（铁律 8）。
    """
    out = _run(invoker, _payload(refund_history=[
        _history("RC-PENDING", status="pending", decided_at=None)]))
    assert out["signals"]["duplicate_refund"] is True
    assert out["signals"]["already_refunded"] is False
    assert out["level"] in ("medium", "high")
    assert out["score"] == RefundRiskScreenSkill.W_DUPLICATE
    assert any("重复退款" in r for r in out["reasons"])


def test_settled_history_on_same_order_flags_already_refunded_and_is_high(invoker):
    """同一单已 settled：两个信号同时成立，单条就能把分档顶到 high。

    `already_refunded` 蕴含 `duplicate_refund`（settled 也在活跃状态集里），
    两个权重叠加是有意的 —— 「这一单的钱已经退出去过」不该依赖别的信号凑数才够 high。
    """
    out = _run(invoker, _payload(refund_history=[_history("RC-SETTLED")]))
    assert out["signals"]["already_refunded"] is True
    assert out["signals"]["duplicate_refund"] is True
    assert out["level"] == "high"
    assert out["score"] >= RefundRiskScreenSkill.LEVEL_HIGH


def test_frequency_window_does_not_count_records_outside_30_days(invoker):
    """窗口两头都要闭上：29 天前算，31 天前不算，申请**之后**决定的也不算。

    最后一条最容易漏：把申请之后才决定的退款算进来，等于用后见之明给当下打分，
    而演示底账里「先起单、后决定」是常态，症状是分数每跑一次都不一样。
    """
    out = _run(invoker, _payload(refund_history=[
        _history("RC-IN", order_id="ORD-OTHER-1", status="rejected",
                 decided_at="2026-08-05T10:00:00+00:00"),      # 29 天前，算
        _history("RC-OLD", order_id="ORD-OTHER-2", status="rejected",
                 decided_at="2026-08-03T10:00:00+00:00"),      # 31 天前，不算
        _history("RC-FUTURE", order_id="ORD-OTHER-3", status="rejected",
                 decided_at="2026-09-04T10:00:00+00:00"),      # 申请之后，不算
    ]))
    assert out["signals"]["frequency_30d"] == 1
    assert out["signals"]["duplicate_refund"] is False, "别的订单的历史不该算成本单重复"


def test_claim_above_paid_flags_amount_over_paid(invoker):
    """申报高于实付：`amount_over_paid` 成立且比值 > 1。"""
    out = _run(invoker, _payload(case_seed=_seed(amount_claimed=1200.0)))
    assert out["signals"]["amount_over_paid"] is True
    assert out["signals"]["amount_ratio"] > 1.0
    assert out["score"] == RefundRiskScreenSkill.W_OVER_PAID
    assert any("封顶" in r for r in out["reasons"])


def test_customer_id_is_read_from_order_payload_json(invoker):
    """顶层没给客户标识时从 `payload_json` 解；解不出就降级并在理由里说明。

    降级而不是抛：老订单的 `payload_json` 就是 `"{}"`，那是合法底账。
    但「没评估」必须写进理由 —— 不写的话读的人会把 0 当成「查过了、干净」。
    """
    resolved = _run(invoker, _payload(
        customer_id=None,
        order=_order(payload_json='{"customer_id":"CUS-1"}'),
        refund_history=[
            _history("RC-1", order_id="ORD-X1", status="rejected", customer_id="CUS-1",
                     decided_at="2026-08-20T10:00:00+00:00"),
            _history("RC-2", order_id="ORD-X2", status="rejected", customer_id="CUS-1",
                     decided_at="2026-08-25T10:00:00+00:00"),
        ]))
    assert resolved["signals"]["frequency_30d"] == 2, "payload_json 里的客户标识没被用上"
    assert RefundRiskScreenSkill.NOTE_NO_CUSTOMER not in resolved["reasons"]

    degraded = _run(invoker, _payload(
        customer_id=None,
        order=_order(payload_json="{}"),
        refund_history=[
            _history("RC-1", order_id="ORD-X1", status="rejected", customer_id="CUS-1",
                     decided_at="2026-08-20T10:00:00+00:00"),
        ]))
    assert degraded["signals"]["frequency_30d"] == 0
    assert degraded["signals"]["multi_order_same_account"] == 1
    assert RefundRiskScreenSkill.NOTE_NO_CUSTOMER in degraded["reasons"]
    assert any("底账无客户标识" in r for r in degraded["reasons"])


# ================================================================ 确定性与阈值
def test_same_input_twice_yields_identical_output(invoker):
    """同一份入参连跑两次，除 `invocation_id` 外逐字一致。

    `invocation_id` 是出参里唯一的非确定项，由 invoker 每次覆盖生成 —— 剔掉它之后
    还有任何差异，就说明结论里掺了时间戳或遍历顺序，那种风控分事后复算不出来。
    """
    payload = _payload(refund_history=[
        _history("RC-A", status="pending", decided_at="2026-08-20T10:00:00+00:00"),
        _history("RC-B", order_id="ORD-OTHER-1", status="rejected",
                 decided_at="2026-08-22T10:00:00+00:00"),
    ])
    first, second = _run(invoker, payload), _run(invoker, payload)
    assert first["invocation_id"] != second["invocation_id"], \
        "invoker 每次调用都该给一个新的 invocation_id"

    first.pop("invocation_id")
    second.pop("invocation_id")
    assert json.dumps(first, ensure_ascii=False, sort_keys=True) == \
        json.dumps(second, ensure_ascii=False, sort_keys=True)


def test_thresholds_are_class_attributes_that_change_the_level(invoker, monkeypatch):
    """把 `LEVEL_HIGH` 调到 10，原本 medium 的那份入参当场变 high。

    证明分档读的是类属性而不是写死的数 —— 阈值是会变的经营口径，
    调它不该改这个文件。
    """
    payload = _payload(refund_history=[
        _history("RC-PENDING", status="pending", decided_at=None)])
    assert _run(invoker, payload)["level"] == "medium"

    monkeypatch.setattr(RefundRiskScreenSkill, "LEVEL_HIGH", 10)
    assert _run(invoker, payload)["level"] == "high"


# ================================================================ 越权与坏入参
def test_identity_without_risk_screen_is_denied():
    """白名单先于注册表：已注册的 skill 也照样拦，且是**抛**不是 failed。"""
    identity = AgentIdentity(
        agent_id="no-risk", role="no_risk", duty="缺 refund.risk_screen 授权",
        allowed_skills=frozenset(set(REFUND_SKILLS) - {SKILL}),
        model_tier=Tier.LIGHT)
    with pytest.raises(PermissionDenied):
        SkillInvoker(identity, None).invoke(SKILL, _payload())


def test_invalid_requested_at_raises_value_error():
    """看不懂的日期**不猜**：猜一个「大概是今天」会让 30 天窗口悄悄算错。

    直接调 skill 而不经 invoker：invoker 会把异常收成 `failed` 结果，
    那样就验不到抛的是哪一类异常（顺带把经 invoker 那条降级路径也钉一下）。
    """
    skill = RefundRiskScreenSkill()
    with pytest.raises(ValueError):
        skill.run(_payload(requested_at="昨天"), SkillContext())

    res = SkillInvoker(ALL_SKILLS_IDENTITY, None).invoke(SKILL, _payload(requested_at="昨天"))
    assert res.status == "failed" and "ValueError" in (res.error or "")


def test_non_positive_amount_paid_raises_value_error():
    """实付非正就是底账坏了：除零要么抛要么得到 inf，两条都会变成一个说不清出处的 high。"""
    skill = RefundRiskScreenSkill()
    with pytest.raises(ValueError):
        skill.run(_payload(order=_order(amount_paid=0.0)), SkillContext())


# ====================================================================== Agent
def test_risk_agent_wraps_output_into_risk_report_artifact():
    """Agent 是薄壳：Identity 逐字段对齐契约，产物是一份带 summary / self_check 的风险报告。

    `max_risk` 特意断言成 `"L"`：风控岗只读底账、只出观察，授权面按它实际需要的取，
    不按「隔壁 finance 是 M 我也 M」抄。
    """
    identity = RefundRiskAgent.identity
    assert identity.agent_id == "refund-risk"
    assert identity.role == "refund_risk"
    assert identity.allowed_skills == frozenset({SKILL})
    assert identity.allowed_tools == frozenset()
    assert identity.write_scope == frozenset({"artifact"})
    assert identity.max_risk == "L"
    assert identity.model_tier == Tier.LIGHT
    assert identity.max_self_repair == 0
    assert identity.duty, "duty 是房间里的自我介绍，不许空"

    agent = RefundRiskAgent(ScriptedModelClient({}), store=None)
    out = agent.run(TaskContext(
        plan_id="plan-test", task_id="task-test", trace_id="trace-test", attempt=1,
        inputs=_payload(refund_history=[_history("RC-SETTLED")]),
        acceptance=[], risk_level="L"))

    assert out.status == "ok", out.error
    art = out.artifacts[0]
    assert art["kind"] == KIND_RISK_REPORT
    content = art["content"]
    assert set(content) == OUTPUT_KEYS | {"summary", "self_check"}
    assert content["level"] == "high"
    assert content["summary"], "Gate 对非代码类产物要 summary 非空"
    assert content["self_check"] == {"build": "pass", "lint": "pass"}
    assert out.metrics == {"level": content["level"], "score": content["score"]}


def test_risk_agent_does_not_join_the_refund_kind_whitelist():
    """本岗的 kind 写在自己文件里，**不进** `_base.ALL_REFUND_KINDS`。

    那份清单是本域既有 Agent 共享的文件，本轮另有一轨也在新增自己的 kind，
    两轨同改一处必冲突；Gate 对非代码类产物不查 kind 白名单，所以分开放是安全的。
    """
    from maos.agents.refund import ALL_REFUND_KINDS
    assert KIND_RISK_REPORT == "refund_risk_report"
    assert KIND_RISK_REPORT not in ALL_REFUND_KINDS


def test_risk_screen_does_not_import_hiclaw_or_flows():
    """纯函数的机器判据：源码里不许出现房间层 / 流程层 / 建表的字样，且无 store 也跑得动。

    只看源码文本会漏掉「其实调了但换了个写法」，只看能跑会漏掉「今天没调、明天加一行」，
    两层都要。
    """
    src = (REPO_ROOT / "maos" / "skills" / "builtin" / "refund" / "risk_screen.py").read_text(
        encoding="utf-8")
    for forbidden in ("hiclaw", "maos.flows", "ensure_schema"):
        assert forbidden not in src, f"risk_screen.py 出现了 {forbidden} —— 它不再是纯函数"

    out = RefundRiskScreenSkill().run(_payload(), SkillContext(store=None))
    assert out["level"] == "low"
    assert out["invocation_id"], "没有调用方给锚点时也要自己生成一个非空 invocation_id"
