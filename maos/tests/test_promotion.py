"""自动晋升与失败聚合（T120）—— `maos/kb/promotion.py` + `classify_case` 四判据版。

本文件守两句话：

1. **只有证据完整且外部结果明确的案例进默认知识层。**「外部结果明确」由四判据说了算，
   而正例那一支还要在原始观察行上重数一遍 settled —— 不许拿算好的结论行给自己背书。
2. **失败实例聚成「渠道 × 支付返回码 × 政策规则号」。** 键是评委原话里的三个词，
   `extra_steps` 只能来自官方码表的 remedy 与真开出来的人工工单，不许现编。

回落路径（不给 `outcome` 入参的三判据版）在 `test_kb_retriever.py` 里已有测试守着，
本文件只补一条断言确认它**行为没变** —— 扩判据最容易顺手改坏的就是老调用方。
"""

from __future__ import annotations

import json
import logging
import sys

import pytest

from maos import kb
from maos.core.store import SqliteStore
from maos.domain.refund import guard, objects, outcome
from maos.kb import guardrails, promotion
from maos.runtime.plan_finalizer import PlanFinalizer

TENANT = "tnt-promo"
CASE = "case-promo-0001"
PLAN = "plan-promo-0001"
CHANNEL = "ch-dealer"
ORDER = "ord-promo-1"
SKU = "sku-promo-1"
RULE = "AS-01@v1"


@pytest.fixture()
def store():
    s = SqliteStore()
    s.init_schema()
    objects.ensure_schema(s)
    outcome.ensure_outcome_schema(s)
    kb.ensure_schema(s)
    return s


def _seed_case(store, *, biz_status_hint: str = "submitted") -> dict:
    """建一个够 `promote_case` 跑起来的最小 case。

    `refund_case` 走 `guard.create_case`（本表唯一插入口径，`objects.execute` 会拦
    旁路写入）；其余表直接 INSERT —— 它们没有守卫，本来就该由各自的 skill 写。
    """
    objects.execute(store, "INSERT INTO tenant (tenant_id, name, region) VALUES (?,?,?)",
                    (TENANT, "促销制造", "华东"))
    objects.execute(
        store,
        "INSERT INTO order_snapshot (tenant_id, order_id, version, sku, amount_paid,"
        " paid_at, channel_id, policy_version_at_order, payload_json, read_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (TENANT, ORDER, 1, SKU, 6800.0, "2026-07-01T00:00:00+00:00", CHANNEL, 1, "{}",
         "2026-07-02T00:00:00+00:00"))
    case = guard.create_case(
        store, tenant_id=TENANT, case_id=CASE, channel_id=CHANNEL, order_id=ORDER,
        order_version=1, sku=SKU, reason_code="quality_defect", amount_claimed=6800.0,
        plan_id=PLAN, actor_skill="refund.intake", invocation_id="inv-seed")
    objects.execute(
        store,
        "INSERT INTO policy_rule (tenant_id, rule_no, version, title, body,"
        " effective_from, effective_to, channel_scope, sku_scope) VALUES (?,?,?,?,?,?,?,?,?)",
        (TENANT, "AS-01", 1, "质量问题全额退", "", "2026-01-01T00:00:00+00:00", None, "*", "*"))
    objects.execute(
        store,
        "INSERT INTO refund_request (tenant_id, case_id, request_id, amount, gateway,"
        " idempotency_key, submitted_at) VALUES (?,?,?,?,?,?,?)",
        (TENANT, CASE, "req-1", 6800.0, "demo", f"idem-{CASE}",
         "2026-07-09T12:00:00+00:00"))
    objects.execute(
        store,
        "INSERT INTO finance_entry (tenant_id, case_id, amount_approved, breakdown_json,"
        " rule_refs, checked_by, checked_at) VALUES (?,?,?,?,?,?,?)",
        (TENANT, CASE, 6800.0, "{}", json.dumps([RULE]), "finance",
         "2026-07-09T00:00:00+00:00"))
    # 整合期（T116 合入后）清单补到十类：剩下几张表也各落一行最小记录。
    objects.execute(
        store,
        "INSERT INTO product_snapshot (tenant_id, sku, version, name, category,"
        " warranty_months, payload_json) VALUES (?,?,?,?,?,?,?)",
        (TENANT, SKU, 1, "无刷角磨机", "电动工具", 12, "{}"))
    objects.execute(
        store,
        "INSERT INTO customer_evidence (tenant_id, case_id, evidence_id, kind, uri,"
        " digest, submitted_at) VALUES (?,?,?,?,?,?,?)",
        (TENANT, CASE, "ev-seed", "image", "file:///ev-seed.jpg", "sha256:ev-seed",
         "2026-07-08T00:00:00+00:00"))
    objects.execute(
        store,
        "INSERT INTO approval_record (tenant_id, case_id, approver, decision, reason,"
        " decided_at) VALUES (?,?,?,?,?,?)",
        (TENANT, CASE, "supervisor", "approved", "", "2026-07-09T00:00:00+00:00"))
    objects.execute(
        store,
        "INSERT INTO notification (tenant_id, case_id, channel, content_digest, sent_at,"
        " ack_at) VALUES (?,?,?,?,?,?)",
        (TENANT, CASE, "wecom", "digest-seed", "2026-07-09T00:30:00+00:00", None))
    # 十类引用**全挂上**：`evidence_complete` 要求 `outcome.EVIDENCE_REF_TYPES` 里的
    # 每一类都 resolve 得到。少挂一类这一单就晋升不了，而那正是本文件要区分的另一档
    # （见 `test_success_needs_business_success_and_evidence_complete`）。
    # `payment_observation` 那条引用指向 req-1：观察行由各用例的 `_observe` 落，
    # 落了才 resolve 得到 —— 没观察的用例本来就不该判成证据完整。
    for object_type, object_id, version in (
            ("refund_case", CASE, 0),
            ("order_snapshot", ORDER, 1),
            ("product_snapshot", SKU, 1),
            ("policy_rule", "AS-01", 1),
            ("customer_evidence", "ev-seed", 1),
            ("approval_record", CASE, 1),
            ("finance_entry", CASE, 1),
            ("refund_request", "req-1", 1),
            ("payment_observation", "req-1", 0),
            ("notification", "digest-seed", 1)):
        objects.attach_business_ref(
            store, plan_id=PLAN, task_id="task-1", tenant_id=TENANT,
            object_type=object_type, object_id=object_id, object_version=version)
    return case


def _observe(store, *, state: str, code: str, request_id: str = "req-1",
             at: str = "2026-07-10T00:00:00+00:00") -> None:
    objects.execute(
        store,
        "INSERT INTO payment_observation (tenant_id, case_id, request_id, gateway_code,"
        " raw_receipt_json, observed_state, observed_at, actor_invocation_id)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (TENANT, CASE, request_id, code, "{}", state, at, "inv-obs"))


# ======================================================================
# 1. classify_case：四判据版
# ======================================================================
def _outcome(**over) -> dict:
    base = {"arrival": "settled", "customer_confirmation": "none",
            "manual_correction": "none", "complaint": "none",
            "evidence_complete": 1, "business_success": 1}
    return {**base, **over}


SETTLED_OBS = [{"observed_state": "settled"}]


def test_success_needs_business_success_and_evidence_complete():
    """# 论证：只有**证据完整且外部结果明确**的案例进默认知识层。"""
    assert guardrails.classify_case(
        observations=SETTLED_OBS, notifications=[], case_row={"biz_status": "settled"},
        outcome=_outcome()) == (kb.KIND_HISTORY_CASE, kb.OUTCOME_SUCCESS)

    # 证据不全 —— 业务成了，但这一单不够格当下一次规划的范本
    assert guardrails.classify_case(
        observations=SETTLED_OBS, notifications=[], case_row={"biz_status": "settled"},
        outcome=_outcome(evidence_complete=0)) is None


def test_settled_claim_without_a_settled_observation_is_never_a_positive():
    """# 论证：结论行说到账，但原始观察数不出 settled —— 不许当正例。

    这是权威事实边界被绕过的迹象（铁律 8）。正例那一支特意回到 `observations` 上
    重数一遍，就是为了不拿算好的结论行给它自己背书。
    """
    assert guardrails.classify_case(
        observations=[], notifications=[], case_row={"biz_status": "settled"},
        outcome=_outcome()) is None


@pytest.mark.parametrize("over", [
    {"arrival": "unsettled", "business_success": 0},
    {"customer_confirmation": "disputed", "business_success": 0},
    {"complaint": "open", "business_success": 0},
])
def test_definite_failures_become_failure_hints(over):
    """外部结果明确地不成功 -> failure_hint，用来提示哪类组合需要额外步骤。"""
    assert guardrails.classify_case(
        observations=SETTLED_OBS, notifications=[], case_row={"biz_status": "processing"},
        outcome=_outcome(**over)) == (kb.KIND_FAILURE_HINT, kb.OUTCOME_FAILED)


def test_unknown_arrival_on_an_open_case_is_no_conclusion():
    """# 论证：全 DONE 但问不出下落 —— 那是「没结论」，既不是正例也不是失败实例。

    `--stall` 那条路径正落在这一格。把它当失败实例记进知识层，等于把一笔可能
    已经退出去的钱写成栽了。
    """
    assert guardrails.classify_case(
        observations=[], notifications=[], case_row={"biz_status": "processing"},
        outcome=_outcome(arrival="unknown", business_success=0)) is None


def test_compensated_case_is_a_failure_hint_even_without_observations():
    """场景 7 那条路径：轮询到顶一行观察都没有，但案子已补偿收口。"""
    assert guardrails.classify_case(
        observations=[], notifications=[], case_row={"biz_status": "compensated"},
        outcome=_outcome(arrival="unknown", business_success=0)
    ) == (kb.KIND_FAILURE_HINT, kb.OUTCOME_FAILED)


@pytest.mark.parametrize("status", ["compensated", "rejected"])
def test_an_offline_close_is_never_a_success_template(status):
    """🔴 线下收口的案子，四判据再齐也不许当成功范本进默认知识层（T145）。

    与上一条的区别正是这一条要守的洞：上一条给的是 `business_success=0` 且零观察，
    本条给**齐全的正例三判据** —— `business_success=1`、`evidence_complete=1`、
    数得出 settled 观察 —— 只有 `biz_status` 停在 `compensated`。

    这条链今天是通的，真跑日当场就会走到：真人在房间里 `/resolve` 一单补偿工单，
    `ingress/outcome_commands.py` 把 `resolution_kind` 写死成 settled，
    `skills/builtin/refund/compensation_close.py` 关单**不改 `biz_status`**、
    只经 `payment.observe` 补一条 settled 观察 —— 于是案子停在 `compensated`，
    却凑齐了正例的三个条件。

    `business_success` 只问「钱到没到、客户有没有异议、投诉开没开着」（它的入参里
    压根没有 `biz_status`，见 `domain/refund/outcome.py` 的
    `## arrival 为什么死盯 payment_observation`），答不了「这笔是原路退成的，
    还是线下补偿平的」。漏判的代价是双份的：下一次规划照着范本抄的是**流程**，
    抄一条靠线下补偿收的场等于教后来者走补偿路；而
    `scripts/verify.py::check_history_case` 判的是 `biz_status in
    AUTHORITATIVE_STATES`（退款域只有 `settled`），同一条文档会被核验侧当场判负 ——
    晋升侧与核验侧口径当面打架，第 7 项直接翻红。
    """
    assert guardrails.classify_case(
        observations=SETTLED_OBS, notifications=[], case_row={"biz_status": status},
        outcome=_outcome()) == (kb.KIND_FAILURE_HINT, kb.OUTCOME_FAILED)


@pytest.mark.parametrize("status", ["settled", "processing", None])
def test_the_status_guard_only_bites_the_two_failed_statuses(status):
    """上一条测护栏咬得住，这一条钉住它**没咬宽**（T145）。

    判据复用 `FAILED_BIZ_STATUS`（明确失败的那两档），**不是**「除 settled 以外」：
    案子还在推进途中（`processing`）、或调用方压根没给 `case_row`（老的 R5 路径）
    都照旧按四判据走。一刀切成「只认 settled」会把 T120 之前那批还没收口
    但四判据已经齐的案例全判负，那是另一种错。
    """
    assert guardrails.classify_case(
        observations=SETTLED_OBS, notifications=[], case_row={"biz_status": status},
        outcome=_outcome()) == (kb.KIND_HISTORY_CASE, kb.OUTCOME_SUCCESS)


def test_legacy_three_criteria_path_is_unchanged():
    """不给 `outcome` 时行为逐字节照旧 —— 扩判据不许改坏老调用方。"""
    acked = [{"ack_at": "2026-07-10T00:00:00+00:00"}]
    assert guardrails.classify_case(
        observations=SETTLED_OBS, notifications=acked,
        case_row={"biz_status": "settled"}) == (kb.KIND_HISTORY_CASE, kb.OUTCOME_SUCCESS)
    assert guardrails.classify_case(
        observations=SETTLED_OBS, notifications=[{"ack_at": None}],
        case_row={"biz_status": "settled"}) is None


def test_sqlite_row_works_as_outcome(store):
    """`outcome` 常常是从库里读出来的一行。0/1 是 INTEGER，别把 0 判成真。"""
    _seed_case(store)
    _observe(store, state="settled", code="10000")
    row = outcome.record_case_outcome(store, tenant_id=TENANT, case_id=CASE, plan_id=PLAN)
    assert guardrails.classify_case(
        observations=[{"observed_state": "settled"}], notifications=[],
        case_row={"biz_status": "settled"}, outcome=row) == (
            kb.KIND_HISTORY_CASE, kb.OUTCOME_SUCCESS)


# ======================================================================
# 2. 语料导入口径
# ======================================================================
@pytest.mark.parametrize("row, expect", [
    ({"outcome": "success"}, (kb.KIND_HISTORY_CASE, kb.OUTCOME_SUCCESS)),
    ({"outcome": "failed"}, (kb.KIND_FAILURE_HINT, kb.OUTCOME_FAILED)),
    ({"outcome": None}, (kb.KIND_FAILURE_HINT, kb.OUTCOME_FAILED)),
    ({}, (kb.KIND_FAILURE_HINT, kb.OUTCOME_FAILED)),
    ({"outcome": "SUCCESS"}, (kb.KIND_FAILURE_HINT, kb.OUTCOME_FAILED)),
])
def test_corpus_rows_fall_to_the_failure_side_when_unclear(row, expect):
    """# 论证：语料里写坏的 kind 一律倒向失败侧，不倒向正例。

    向失败侧倒是这里唯一安全的失败方向：一条来路不明的记录进了默认知识层，
    就会去指导下一次规划（T118 那 8 条标错 kind 的失败案例正是这么混进正例的）。
    """
    assert promotion.classify_corpus_row(row) == expect


# ======================================================================
# 3. 失败聚合表
# ======================================================================
def test_bump_merges_steps_and_counts(store):
    first = promotion.bump_failure_hint(
        store, tenant_id=TENANT, channel_id=CHANNEL, gateway_code="40005",
        rule_no=RULE, extra_steps=["降低请求并发量"])
    assert first["count"] == 1

    second = promotion.bump_failure_hint(
        store, tenant_id=TENANT, channel_id=CHANNEL, gateway_code="40005",
        rule_no=RULE, extra_steps=["降低请求并发量", "人工到渠道后台对账"])
    assert second["count"] == 2
    assert second["extra_steps"] == ["降低请求并发量", "人工到渠道后台对账"], (
        "第二次带来的新步骤要留下，已有的不该被覆盖 —— 越聚合知道得越少是反的")

    rows = promotion.list_failure_hints(store, tenant_id=TENANT)
    assert len(rows) == 1 and rows[0]["count"] == 2


def test_different_gateway_codes_are_different_rows(store):
    """键是「渠道 × 返回码 × 规则号」—— 换一个返回码就是另一类组合。"""
    for code in ("40005", "ACQ.TRADE_NOT_EXIST"):
        promotion.bump_failure_hint(
            store, tenant_id=TENANT, channel_id=CHANNEL, gateway_code=code,
            rule_no=RULE, extra_steps=[])
    assert len(promotion.list_failure_hints(store, tenant_id=TENANT)) == 2


def test_list_failure_hints_on_a_db_without_the_table():
    """表不在就返回空，不抛 —— 读的一侧不该逼调用方先建表。"""
    bare = SqliteStore()
    bare.init_schema()
    objects.ensure_schema(bare)
    assert promotion.list_failure_hints(bare) == []


# ======================================================================
# 4. promote_case / promote_plan 端到端
# ======================================================================
def test_settled_case_is_promoted_to_history_case(store):
    _seed_case(store)
    _observe(store, state="settled", code="10000")
    objects.execute(
        store,
        "INSERT INTO notification (tenant_id, case_id, channel, content_digest, sent_at,"
        " ack_at) VALUES (?,?,?,?,?,?)",
        (TENANT, CASE, "sms", "d1", "2026-07-10T01:00:00+00:00", None))

    res = promotion.promote_case(store, tenant_id=TENANT, case_id=CASE, plan_id=PLAN)
    assert res["verdict"] == (kb.KIND_HISTORY_CASE, kb.OUTCOME_SUCCESS)
    doc = kb.get_doc(store, TENANT, res["doc_id"])
    assert doc["kind"] == kb.KIND_HISTORY_CASE and doc["outcome"] == kb.OUTCOME_SUCCESS
    assert doc["source_case_id"] == CASE, "正例必须追得回本库那一条真实 case"
    assert doc["rule_no"] == RULE and doc["channel_id"] == CHANNEL
    assert doc["policy_version"] == 1, "政策版本取下单当时锁定的那一版"
    assert res["hint"] is None, "正例不进失败聚合表"


def test_the_real_run_day_compensation_close_lands_on_the_failure_side(store):
    """🔴 真跑日那一单的端到端版：补偿关单 + 一条 settled 观察 -> failure_hint（T145）。

    形状与上一条正例**逐字对应**，只差一件事：案子被推到了 `compensated`。
    上一条能进默认知识层，这一条不能 —— 差别只在 `biz_status`，而那正是
    `business_success` 答不出来的那一问（论证见 `classify_case` 那一节的
    `test_an_offline_close_is_never_a_success_template`）。

    走完整条 `promote_case` 而不只是 `classify_case`：要钉住的是**库里最后落下
    什么**。判负之后这一单还得进失败聚合表 —— 「靠线下补偿收的场」本身就是
    「哪类组合需要额外步骤」的一手素材，掐掉正例不等于把它整个丢掉。
    """
    _seed_case(store)
    _observe(store, state="settled", code="10000")
    guard.update_biz_status(store, TENANT, CASE, "approved",
                            "refund.approve", "inv-approve")
    guard.update_biz_status(store, TENANT, CASE, "compensated",
                            "refund.compensation_close", "inv-close")

    res = promotion.promote_case(store, tenant_id=TENANT, case_id=CASE, plan_id=PLAN)
    assert res["verdict"] == (kb.KIND_FAILURE_HINT, kb.OUTCOME_FAILED), (
        "补偿收口的案子被晋升成了成功范本 —— verify 第 7 项会当场判它负")
    assert res["outcome"]["business_success"], (
        "前提没立住：这一单本来就该是四判据齐全的，否则这条测的不是 biz_status 那一问")
    doc = kb.get_doc(store, TENANT, res["doc_id"])
    assert doc["kind"] == kb.KIND_FAILURE_HINT and doc["outcome"] == kb.OUTCOME_FAILED
    assert res["hint"] is not None, "判负的案子照样要进失败聚合表，不是整个丢掉"


def test_history_case_title_does_not_claim_an_ack_that_never_happened(store):
    """标题自称的事实必须和 `case_outcome` 那一行对得上。"""
    _seed_case(store)
    _observe(store, state="settled", code="10000")
    res = promotion.promote_case(store, tenant_id=TENANT, case_id=CASE, plan_id=PLAN)
    doc = kb.get_doc(store, TENANT, res["doc_id"])
    assert "客户尚未确认" in doc["title"]
    assert "经客户确认" not in doc["title"]


def test_failed_case_is_promoted_and_aggregated(store):
    """# 论证：失败实例聚成「哪类渠道、支付返回或政策组合需要额外步骤」。"""
    _seed_case(store)
    _observe(store, state="failed", code="ACQ.TRADE_NOT_EXIST")

    res = promotion.promote_case(store, tenant_id=TENANT, case_id=CASE, plan_id=PLAN)
    assert res["verdict"] == (kb.KIND_FAILURE_HINT, kb.OUTCOME_FAILED)
    doc = kb.get_doc(store, TENANT, res["doc_id"])
    assert doc["gateway_code"] == "ACQ.TRADE_NOT_EXIST", "失败文档要带返回码，正例不带"

    hint = res["hint"]
    assert (hint["tenant_id"], hint["channel_id"], hint["gateway_code"], hint["rule_no"]) == (
        TENANT, CHANNEL, "ACQ.TRADE_NOT_EXIST", RULE)
    assert hint["extra_steps"], "额外步骤不能是空的 —— 官方码表的 remedy 就在那儿"
    assert any("ACQ.TRADE_NOT_EXIST" in s for s in hint["extra_steps"])


def test_extra_steps_come_from_the_official_code_table(store):
    """# 论证：extra_steps 是抄来的（官方 remedy + 真工单），不是编的。"""
    from maos.tools.gateway_codes import lookup

    _seed_case(store)
    _observe(store, state="failed", code="ACQ.TRADE_NOT_EXIST")
    objects.execute(
        store,
        "INSERT INTO compensation_record (tenant_id, case_id, kind, detail_json,"
        " executed_at, operator) VALUES (?,?,?,?,?,?)",
        (TENANT, CASE, "manual_ticket",
         json.dumps({"todo": ["到支付渠道后台按 idempotency_key 核对下落"]},
                    ensure_ascii=False), "2026-07-11T00:00:00+00:00", "ops"))

    res = promotion.promote_case(store, tenant_id=TENANT, case_id=CASE, plan_id=PLAN)
    steps = res["hint"]["extra_steps"]
    assert lookup("ACQ.TRADE_NOT_EXIST").remedy in " ".join(steps)
    assert "到支付渠道后台按 idempotency_key 核对下落" in steps


def test_unresolved_case_is_not_promoted_at_all(store):
    """没结论的案例不进知识层 —— 一条文档、一行聚合都不许留下。"""
    _seed_case(store)
    res = promotion.promote_case(store, tenant_id=TENANT, case_id=CASE, plan_id=PLAN)
    assert res["verdict"] is None and res["doc_id"] is None
    assert objects.query(store, "SELECT * FROM kb_doc WHERE tenant_id=?", (TENANT,)) == []
    assert promotion.list_failure_hints(store, tenant_id=TENANT) == []
    assert outcome.read_case_outcome(store, tenant_id=TENANT, case_id=CASE) is not None, (
        "不晋升不等于不算判据：四判据照样落库，只是这一单还没结论")


def test_promote_twice_does_not_double_count(store):
    """重跑同一个 Plan：文档就地覆盖，聚合表的 count 却会累加 —— 各自都对。

    doc_id 由 case_id 定死，所以文档恒一条；`count` 数的是「这类组合命中过几次」，
    同一单被重算两次确实是命中两次的信号（比如人工重跑复盘）。这条断言把两种
    语义分别钉住，免得下一个人把其中一种"顺手"改成另一种。
    """
    _seed_case(store)
    _observe(store, state="failed", code="40005")
    first = promotion.promote_case(store, tenant_id=TENANT, case_id=CASE, plan_id=PLAN)
    second = promotion.promote_case(store, tenant_id=TENANT, case_id=CASE, plan_id=PLAN)
    assert first["doc_id"] == second["doc_id"]
    assert len(objects.query(store, "SELECT * FROM kb_doc WHERE tenant_id=?", (TENANT,))) == 1
    assert second["hint"]["count"] == 2


def test_promote_plan_walks_every_case_and_logs_the_event(store):
    _seed_case(store)
    _observe(store, state="settled", code="10000")
    results = promotion.promote_plan(store, plan_id=PLAN)
    assert len(results) == 1

    events = [e for e in store.list_event_log(PLAN)
              if e["event_type"] == promotion.EVENT_CASE_PROMOTED]
    assert len(events) == 1
    detail = events[0]["detail"]
    detail = json.loads(detail) if isinstance(detail, str) else detail
    assert detail["tenant_id"] == TENANT and detail["case_id"] == CASE, "契约 §F"
    assert detail["kind"] == kb.KIND_HISTORY_CASE


def test_promote_plan_is_a_noop_without_the_refund_domain():
    """软件域那几个场景照样会走到这个钩子上 —— 不许因为没有退款表就抛。"""
    bare = SqliteStore()
    bare.init_schema()
    assert promotion.promote_plan(bare, plan_id="plan-software") == []


# ======================================================================
# 5. 终态钩子
# ======================================================================
def test_finalizer_promotes_on_terminal_plan(store):
    """# 论证：自动晋升的执行点在 Plan 终态钩子上，不再靠人手动调。"""
    _seed_case(store)
    _observe(store, state="settled", code="10000")
    store.insert_plan({"plan_id": PLAN, "goal": "退款", "trace_id": "tr-1",
                       "state": "DONE"})

    PlanFinalizer(store).poll(PLAN)
    docs = objects.query(store, "SELECT kind, outcome FROM kb_doc WHERE tenant_id=?", (TENANT,))
    assert [(d["kind"], d["outcome"]) for d in docs] == [
        (kb.KIND_HISTORY_CASE, kb.OUTCOME_SUCCESS)]


def test_promotion_failure_does_not_break_the_finalizer(store, monkeypatch):
    """晋升炸了不许掀翻已经跑完的 Plan —— 但也不许静默。"""
    _seed_case(store)
    store.insert_plan({"plan_id": PLAN, "goal": "退款", "trace_id": "tr-2",
                       "state": "DONE"})

    def boom(*_a, **_kw):
        raise RuntimeError("库炸了")

    monkeypatch.setattr(promotion, "promote_plan", boom)
    finalizer = PlanFinalizer(store)
    assert finalizer.promote(PLAN) == []
    assert finalizer.poll(PLAN), "复盘照跑，沉淀的 knowledge 一条都不许少"


# ---------------------------------------------------------------------------
# `_has_table` 的 ImportError 分档（整合期 p10-g）
#
# T145 把退款域的 import 从模块级挪进函数体之后，`plan_finalizer.py:125` 那条
# `except ImportError` 就再也走不到了 —— 从前「退款域自己装坏了」会在那里打一行
# 「晋升模块不可用」，改完之后它被 `_has_table` 的 `except Exception` 一并吞掉，
# `promote_plan` 静默返回 []，finalizer 一个字都不打。那正是 `plan_finalizer`
# 自己 docstring 点名的坏味道：静默的晋升失败会让知识库慢慢空掉，而每一次跑都
# 显示成功。这两条钉住「两档分得开」。
# ---------------------------------------------------------------------------


class _BlockDomain:
    """让 `import maos.domain.*` 抛真正的 ModuleNotFoundError。

    不用 `sys.modules[name] = None`：那条路抛的 ImportError 的 `.name` 是空的，
    而 `_has_table` 的判据正是 `exc.name` —— 用假形态测等于没测到那一支。
    口径同 `test_matrix_bus.py::_BlockNio`。
    """

    def find_spec(self, name, path=None, target=None):
        if name == "maos.domain" or name.startswith("maos.domain."):
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return None


def test_a_missing_domain_is_the_expected_path_and_stays_quiet(store, monkeypatch, caplog):
    """换业务域的部署上退款域压根不在 —— 退化成「表不在」，且**不许**告警。

    软件域那几个场景每条 Plan 终态都会走到这里，warning 会刷屏。
    """
    for mod in [m for m in list(sys.modules) if m.startswith("maos.domain")]:
        monkeypatch.delitem(sys.modules, mod, raising=False)
    monkeypatch.setattr(sys, "meta_path", [_BlockDomain(), *sys.meta_path])

    with caplog.at_level(logging.DEBUG, logger="maos.kb.promotion"):
        assert promotion._has_table(store, "refund_case") is False

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], (
        "域没部署是预期路径，不该每条 Plan 都喊一声")
    assert [r for r in caplog.records if r.levelno == logging.DEBUG], (
        "一声不吭也不行 —— debug 里要留得下「为什么退化了」")


def test_a_broken_domain_dependency_is_a_bug_and_says_so(store, monkeypatch, caplog):
    """退款域在、但它装不起来 —— 这是 bug，必须出声。

    判据落在 `exc.name` 上而不是异常类型：两档都是 ModuleNotFoundError，分界是
    「缺的是域本身」还是「缺的是域依赖的别的东西」。这里直接让 `_objects()` 抛一个
    缺外部依赖形态的异常 —— 真 import 机制那一支由上一条用 meta_path 覆盖了。
    """
    def broken(*_a, **_kw):
        raise ModuleNotFoundError("No module named 'psycopg'", name="psycopg")

    monkeypatch.setattr(promotion, "_objects", broken)

    with caplog.at_level(logging.DEBUG, logger="maos.kb.promotion"):
        assert promotion._has_table(store, "refund_case") is False

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "退款域装不起来却一声不吭 —— 知识库会安静地空掉"
    assert "psycopg" in warnings[0].getMessage(), "告警里要写清到底缺了什么"
