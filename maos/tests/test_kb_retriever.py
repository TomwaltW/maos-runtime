"""W-3 的机器验收 —— 两阶段检索、四通道融合、三条护栏、晋升规则。

第一条最重要，也最不能商量：**跨租户绝不召回**。这条挂了整个 RAG 部分不用交 ——
检索不准评委顶多说效果一般，把别家租户的知识拿来指导这一单的规划是事故。
所以它写在最前面，而且写了两种越界形态：查询指名别家租户、以及查询干脆不给租户。

其余每条都对应一种「看起来绿、演示当天炸」的失败形态：

  · 融合排序如果把精确通道退化成模糊通道，`rule_no` 命中的那条会被全文命中的
    压下去 —— 断言锁的是**次序**，不是「有没有召回」。
  · `KbRetrieved` 缺 `duration_ms`，trace 上的 span 会退化成零长；缺 `docs` 数组，
    核验器第 5 项直接判「RAG 命中是编的」。两者都锁死形状。
  · 三条护栏各一条负例。护栏不是文档措辞，是断言：违反必须**抛**，
    不许压成告警 —— 静默丢弃的后果是同一条知识下次照样被同样地用上。
  · `kb.retrieve` 的零模型 / 空结果不阻塞 / `store is None` 不抛，
    破一条就炸别人（Coding Agent 在产补丁之前调它）。
"""

from __future__ import annotations

import json

import pytest

from maos import kb
from maos.agents.base import AgentIdentity
from maos.core.store import SqliteStore
from maos.kb import guardrails, retriever
from maos.model.client import ScriptedModelClient
from maos.skills.contract import SkillContext
from maos.skills.invoker import SkillInvoker

TENANT_A = "tnt-a"
TENANT_B = "tnt-b"

KB_IDENTITY = AgentIdentity(
    agent_id="kb-tester", role="tester", duty="检索层验收",
    allowed_skills=frozenset({"kb.retrieve"}),
)


@pytest.fixture
def store():
    s = SqliteStore()
    s.init_schema()
    kb.ensure_schema(s)
    return s


def _doc(store, doc_id, **fields):
    row = {"tenant_id": TENANT_A, "doc_id": doc_id, "kind": kb.KIND_POLICY,
           "biz_type": "refund", "title": doc_id, "body": ""}
    row.update(fields)
    return kb.upsert_doc(store, row)


# ---------------------------------------------------------------------------
# 1. 跨租户绝不召回 —— 这条挂了，整个 RAG 部分不用交
# ---------------------------------------------------------------------------
def test_cross_tenant_never_retrieved(store):
    """B 家的知识对 A 家的查询必须完全不可见，哪怕它在每个通道上都满分。"""
    _doc(store, "doc-b-perfect", tenant_id=TENANT_B, rule_no="AS-01",
         gateway_code="GW-99", title="退款政策", body="退款 政策 轴承 锈蚀")
    _doc(store, "doc-a-plain", tenant_id=TENANT_A, title="本家的一条无关知识",
         body="无关")

    hits = retriever.retrieve(store, {
        "tenant_id": TENANT_A, "biz_type": "refund",
        "rule_no": "AS-01", "gateway_code": "GW-99", "keyword": "退款 政策"})

    got = {h["doc_id"] for h in hits}
    assert "doc-b-perfect" not in got, "跨租户召回 —— 这是事故，不是效果问题"
    # 反向也要成立：B 家查得到自己的
    hits_b = retriever.retrieve(store, {"tenant_id": TENANT_B, "rule_no": "AS-01"})
    assert [h["doc_id"] for h in hits_b] == ["doc-b-perfect"]


def test_query_without_tenant_returns_empty(store):
    """不给租户不是「查全部」，是「没有候选集」。

    回落成全租户检索是最危险的一种默认值：单租户演示里看不出任何异常，
    多租户上线当天泄漏。
    """
    _doc(store, "doc-a-1", rule_no="AS-01")
    assert retriever.prefilter(store, {"rule_no": "AS-01"}) == []
    assert retriever.retrieve(store, {"rule_no": "AS-01"}) == []


def test_prefilter_is_hard_constraint_on_every_dimension(store):
    """阶段一七个维度都是硬过滤；文档侧 NULL 才是通配。"""
    _doc(store, "doc-tmall", channel_id="ch-tmall", sku="SKU-1", region="CN-EAST",
         policy_version=1, workflow_version=1, rule_no="AS-01")
    _doc(store, "doc-jd", channel_id="ch-jd", sku="SKU-1", region="CN-EAST",
         policy_version=1, workflow_version=1, rule_no="AS-01")
    _doc(store, "doc-anychannel", channel_id=None, sku=None, rule_no="AS-01")

    got = {d["doc_id"] for d in retriever.prefilter(store, {
        "tenant_id": TENANT_A, "biz_type": "refund", "channel_id": "ch-tmall",
        "region": "CN-EAST", "sku": "SKU-1", "policy_version": 1, "workflow_version": 1})}
    assert got == {"doc-tmall", "doc-anychannel"}, "别的渠道进了候选集，或通配被误杀"


# ---------------------------------------------------------------------------
# 2. 四通道各自命中
# ---------------------------------------------------------------------------
def test_rule_no_channel_hits(store):
    _doc(store, "doc-rule", rule_no="AS-01")
    _doc(store, "doc-other-rule", rule_no="PS-07")
    hits = retriever.retrieve(store, {"tenant_id": TENANT_A, "rule_no": "AS-01"})
    assert [h["doc_id"] for h in hits] == ["doc-rule"]
    assert hits[0]["channels"]["rule_no"] == 1.0
    assert hits[0]["score"] == pytest.approx(retriever.DEFAULT_WEIGHTS["rule_no"])


def test_gateway_code_channel_hits(store):
    _doc(store, "doc-gw", gateway_code="INSUFFICIENT_BALANCE",
         kind=kb.KIND_ERROR_CODE_PLAYBOOK)
    _doc(store, "doc-gw-other", gateway_code="TIMEOUT", kind=kb.KIND_ERROR_CODE_PLAYBOOK)
    hits = retriever.retrieve(
        store, {"tenant_id": TENANT_A, "gateway_code": "INSUFFICIENT_BALANCE"})
    assert [h["doc_id"] for h in hits] == ["doc-gw"]
    assert hits[0]["channels"]["gateway_code"] == 1.0


def test_fts_channel_hits(store):
    """全文通道走 FTS5（trigram 分词，中文可切）。"""
    _doc(store, "doc-rust", title="轴承锈蚀退款", body="外圈锈蚀，按质量问题全额退款")
    _doc(store, "doc-box", title="包装破损", body="运输箱压瘪，不涉及退款金额")
    hits = retriever.retrieve(store, {"tenant_id": TENANT_A, "keyword": "锈蚀"})
    ids = [h["doc_id"] for h in hits]
    assert ids and ids[0] == "doc-rust"
    assert hits[0]["channels"]["fts"] > 0


def test_vector_channel_hits_without_any_model_call(store):
    """语义通道是确定性 hash embedding：零模型、跨进程稳定。"""
    _doc(store, "doc-sem", title="轴承锈蚀", body="锈蚀 质量 退款")
    vec_a = retriever.embed("轴承锈蚀")
    vec_b = retriever.embed("轴承锈蚀")
    assert vec_a == vec_b, "同一段文本两次向量化结果必须逐位一致"
    assert retriever.cosine(vec_a, vec_b) == pytest.approx(1.0)

    hits = retriever.retrieve(store, {"tenant_id": TENANT_A, "keyword": "轴承锈蚀"})
    assert hits and hits[0]["channels"]["vector"] > 0


# ---------------------------------------------------------------------------
# 3. 加权融合排序
# ---------------------------------------------------------------------------
def test_weighted_fusion_adds_exact_channel_on_top_of_text(store):
    """两份文本完全相同的文档，规则编号命中的那份必须排前面，且分差 = 该通道权重。

    锁的是**次序与分差**，不是「有没有召回」：融合一旦写成取最大值、或把精确通道
    退化成又一个模糊通道，这条会红，而检索照样有结果 —— 那正是最难发现的一种退化。
    刻意控住文本变量：两份文档的 fts / vector 完全一样，差的只可能是 rule_no 那一档。
    """
    _doc(store, "doc-exact", rule_no="AS-01", title="退款政策", body="退款 政策 全额")
    _doc(store, "doc-textonly", rule_no=None, title="退款政策", body="退款 政策 全额")
    hits = retriever.retrieve(store, {"tenant_id": TENANT_A, "rule_no": "AS-01",
                                      "keyword": "退款政策"})
    assert [h["doc_id"] for h in hits] == ["doc-exact", "doc-textonly"]
    assert hits[0]["score"] - hits[1]["score"] == pytest.approx(
        retriever.DEFAULT_WEIGHTS["rule_no"])
    assert hits[1]["channels"]["rule_no"] == 0.0, "精确通道必须是二值，不给部分分"


def test_fusion_sums_all_four_channels(store):
    """四通道全满的文档，得分等于四个权重之和（融合是加权和，不是取最大）。"""
    _doc(store, "doc-all", rule_no="AS-01", gateway_code="GW-1",
         title="退款政策", body="退款 政策")
    hits = retriever.retrieve(store, {
        "tenant_id": TENANT_A, "rule_no": "AS-01", "gateway_code": "GW-1",
        "keyword": "退款政策"})
    channels = hits[0]["channels"]
    expected = sum(retriever.DEFAULT_WEIGHTS[c] * channels[c] for c in retriever.CHANNELS)
    assert hits[0]["score"] == pytest.approx(expected)
    assert channels["rule_no"] == 1.0 and channels["gateway_code"] == 1.0
    assert channels["fts"] > 0 and channels["vector"] > 0


def test_weights_configurable_via_env(monkeypatch):
    """权重走 MAOS_KB_WEIGHTS；读不懂就回落默认并告警，不抛。"""
    monkeypatch.setenv(kb.KB_WEIGHTS_ENV, json.dumps({"rule_no": 0.9}))
    assert retriever.load_weights()["rule_no"] == 0.9
    assert retriever.load_weights()["fts"] == retriever.DEFAULT_WEIGHTS["fts"]

    monkeypatch.setenv(kb.KB_WEIGHTS_ENV, "不是 JSON")
    assert retriever.load_weights() == retriever.DEFAULT_WEIGHTS


def test_scores_are_deterministic_across_runs(store):
    """同一份语料连查两次，命中与分数必须逐条一致（演示要能复现）。"""
    _doc(store, "doc-1", rule_no="AS-01", title="甲", body="退款 政策")
    _doc(store, "doc-2", rule_no="AS-01", title="乙", body="退款 政策")
    query = {"tenant_id": TENANT_A, "rule_no": "AS-01", "keyword": "退款"}
    first = [(h["doc_id"], h["score"]) for h in retriever.retrieve(store, query)]
    second = [(h["doc_id"], h["score"]) for h in retriever.retrieve(store, query)]
    assert first == second and first


# ---------------------------------------------------------------------------
# 4. KbRetrieved 事件形状（F-3 冻结，消费侧已写死）
# ---------------------------------------------------------------------------
def _kb_events(store):
    return [r for r in kb.query(store, "SELECT * FROM event_log WHERE event_type=?",
                                ("KbRetrieved",))]


def test_kb_retrieved_event_shape(store):
    """detail.docs[*] 必须有 doc_id 与 score，且 doc_id 在 kb_doc 里查得到。

    再加 duration_ms：trace 把 KbRetrieved 归为有时长的事件，span 的 end 由
    start + duration_ms 得出 —— 缺这个字段 span 会退化成零长。
    """
    _doc(store, "doc-evt", rule_no="AS-01")
    hits = retriever.retrieve_and_log(
        store, {"tenant_id": TENANT_A, "rule_no": "AS-01"},
        plan_id="plan-x", task_id="task-x", trace_id="trace-x")

    rows = _kb_events(store)
    assert len(rows) == 1
    detail = json.loads(rows[0]["detail"])
    assert isinstance(detail["docs"], list) and detail["docs"]
    assert "duration_ms" in detail
    for d in detail["docs"]:
        assert d["doc_id"] and isinstance(d["score"], (int, float))
        assert kb.get_doc(store, TENANT_A, d["doc_id"]) is not None, \
            "doc_id 在 kb_doc 里查不到 —— 判据是「RAG 命中是编的」"
    assert {h["doc_id"] for h in hits} == {d["doc_id"] for d in detail["docs"]}
    assert rows[0]["plan_id"] == "plan-x" and rows[0]["task_id"] == "task-x"


def test_empty_hit_still_logs_event(store):
    """命中为空也落事件：检索发生过本身就是事实，不落才让人无从追溯。"""
    retriever.retrieve_and_log(store, {"tenant_id": TENANT_A, "rule_no": "NO-SUCH"})
    rows = _kb_events(store)
    assert len(rows) == 1 and json.loads(rows[0]["detail"])["docs"] == []


def test_kb_disabled_retrieves_nothing_and_logs_nothing(store, monkeypatch):
    """MAOS_KB_ENABLED=0 是对照实验的唯一变量：关掉就该干净地什么都没有。"""
    _doc(store, "doc-off", rule_no="AS-01")
    monkeypatch.setenv(kb.KB_ENABLED_ENV, "0")
    assert kb.kb_enabled() is False
    assert retriever.retrieve_and_log(store, {"tenant_id": TENANT_A, "rule_no": "AS-01"}) == []
    assert _kb_events(store) == []


# ---------------------------------------------------------------------------
# 5. 三条护栏，各一条负例
# ---------------------------------------------------------------------------
BASELINE = [
    {"task_id": "t1", "role": "intake", "title": "受理",
     "inputs": {"step": "intake", "tenant_id": TENANT_A, "case_id": "case-1",
                "biz_type": "refund"},
     "risk_level": "L", "effect_risk": "L"},
    {"task_id": "t2", "role": "finance", "title": "核算",
     "inputs": {"biz_type": "refund", "amount_claimed": 6800.0},
     "risk_level": "M", "effect_risk": "H"},
]


def test_guardrail_1_cannot_delete_tasks():
    """护栏 1：检索结果只能增加任务，不能删除必要任务。"""
    shrunk = [BASELINE[0]]
    with pytest.raises(guardrails.GuardrailViolation, match="删掉了既有任务"):
        guardrails.assert_only_adds(BASELINE, shrunk)
    guardrails.assert_only_adds(BASELINE, BASELINE + [
        {"role": "notify", "title": "通知", "inputs": {"step": "notify"}}])


def test_guardrail_2_cannot_override_order_facts():
    """护栏 2：建议任务不得携带当前订单的事实字段。"""
    bad = [{"role": "finance", "title": "按历史金额核算",
            "inputs": {"amount_paid": 5390.0, "policy_version_at_order": 2}}]
    with pytest.raises(guardrails.GuardrailViolation, match="订单事实字段"):
        guardrails.assert_no_fact_override(bad)
    guardrails.assert_no_fact_override(
        [{"role": "finance", "inputs": {"tenant_id": TENANT_A, "case_id": "case-1"}}])


def test_guardrail_3_cannot_skip_or_lower_approval():
    """护栏 3：不得跳过人工审批，也不得把 effect_risk 降下来。"""
    with pytest.raises(guardrails.GuardrailViolation, match="免审批标记"):
        guardrails.assert_no_approval_skip(
            BASELINE, BASELINE + [{"role": "payment", "inputs": {"skip_approval": True}}])

    lowered = [BASELINE[0], {**BASELINE[1], "effect_risk": "L"}]
    with pytest.raises(guardrails.GuardrailViolation, match="effect_risk"):
        guardrails.assert_no_approval_skip(BASELINE, lowered)


def test_apply_suggestions_adds_step_without_carrying_facts():
    """建议任务补进来时：步骤照抄，参数取当前 case，事实字段一个不带。"""
    body = guardrails.case_to_doc_body([
        {"role": "finance", "title": "核算退款金额并写财务分录",
         "inputs": {"biz_type": "refund", "amount_claimed": 9999.0,
                    "order_id": "ord-history", "amount_paid": 9999.0},
         "risk_level": "M", "effect_risk": "H"},
    ])
    assert "order_id" not in body and "amount_paid" not in body, "事实字段不该进库"

    baseline = [BASELINE[0]]
    docs = [{"doc_id": "doc-h", "score": 0.8, "kind": kb.KIND_HISTORY_CASE, "body": body}]
    merged, added = guardrails.apply_suggestions(baseline, docs)

    assert len(added) == 1 and added[0]["role"] == "finance"
    assert added[0]["inputs"]["case_id"] == "case-1", "参数必须取当前 case"
    assert "order_id" not in added[0]["inputs"]
    assert added[0]["effect_risk"] == "H", "高风险标记必须跟着步骤一起带过来"
    assert len(merged) == 2


# ---------------------------------------------------------------------------
# 6. 晋升规则：失败案例不进正例知识层
# ---------------------------------------------------------------------------
def test_failed_case_never_becomes_positive_knowledge():
    """证据完整且外部结果明确才进 history_case；失败实例只进 failure_hint。"""
    settled = [{"observed_state": "settled"}]
    acked = [{"ack_at": "2026-08-28T00:00:00+00:00"}]

    assert guardrails.classify_case(
        observations=settled, notifications=acked,
        case_row={"biz_status": "settled"}) == (kb.KIND_HISTORY_CASE, kb.OUTCOME_SUCCESS)

    # 少了客户 ack = 不是「外部结果明确」，只是「我们这边做完了」
    assert guardrails.classify_case(
        observations=settled, notifications=[{"ack_at": None}],
        case_row={"biz_status": "settled"}) is None

    # settled 却拿不出 settled 观察 —— 权威事实边界被绕过的迹象，更不能当正例
    assert guardrails.classify_case(
        observations=[], notifications=acked,
        case_row={"biz_status": "settled"}) is None

    assert guardrails.classify_case(
        observations=[], notifications=[],
        case_row={"biz_status": "rejected"}) == (kb.KIND_FAILURE_HINT, kb.OUTCOME_FAILED)


def test_failure_hint_does_not_participate_in_planning(store):
    """failure_hint 检得到，但不作为规划正例被并进 DAG。"""
    body = guardrails.case_to_doc_body(
        [{"role": "finance", "title": "核算", "inputs": {"biz_type": "refund"}}])
    docs = [{"doc_id": "doc-f", "score": 0.9, "kind": kb.KIND_FAILURE_HINT, "body": body}]
    merged, added = guardrails.apply_suggestions([BASELINE[0]], docs)
    assert added == [] and merged == [BASELINE[0]]


def test_upsert_rejects_out_of_range_kind(store):
    """写入侧的取值域越界抛不降级 —— 归不了类的脏数据最难回溯。"""
    with pytest.raises(kb.KbError, match="kind"):
        kb.upsert_doc(store, {"tenant_id": TENANT_A, "doc_id": "d", "kind": "随便写的"})
    with pytest.raises(kb.KbError, match="tenant_id"):
        kb.upsert_doc(store, {"doc_id": "d", "kind": kb.KIND_POLICY})


# ---------------------------------------------------------------------------
# 7. kb.retrieve 的三条性质：零模型 / 空结果不阻塞 / store is None 不抛
# ---------------------------------------------------------------------------
def test_kb_retrieve_makes_no_model_call(store):
    """Coding Agent 在产补丁之前调它 —— 这里多一次模型调用，场景 2 的调用序整体错位。"""
    _doc(store, "doc-m", rule_no="AS-01")
    model = ScriptedModelClient({"任何": "任何"})
    skills = SkillInvoker(KB_IDENTITY, store)
    res = skills.invoke("kb.retrieve", {"tenant_id": TENANT_A, "rule_no": "AS-01"},
                        extras={"model": model, "plan_id": "p", "task_id": "t"})
    assert res.status == "ok" and res.output["doc_count"] == 1
    assert model.calls == [], "kb.retrieve 调了模型 —— 场景 2 的 attempt 断言会莫名其妙地红"


def test_kb_retrieve_empty_and_no_store_do_not_block():
    """空结果不阻塞；store 没接线也只返回空，不抛。"""
    skills = SkillInvoker(KB_IDENTITY, None)
    res = skills.invoke("kb.retrieve", {"keyword": "任意", "tenant_id": TENANT_A})
    assert res.status == "ok" and res.output == {"items": [], "count": 0}

    s = SqliteStore()
    s.init_schema()
    hit = SkillInvoker(KB_IDENTITY, s).invoke(
        "kb.retrieve", {"tenant_id": TENANT_A, "rule_no": "NO-SUCH"})
    assert hit.status == "ok" and hit.output["docs"] == []


def test_kb_retrieve_output_stays_compatible_for_old_callers(store):
    """不带 tenant_id 的老调用方看到的输出逐字段等于 1.0.0（不多带 docs 键）。"""
    skills = SkillInvoker(KB_IDENTITY, store)
    out = skills.invoke("kb.retrieve", {"keyword": "任意"}).output
    assert set(out) == {"items", "count"}


def test_skill_run_is_pure_read_except_one_event(store):
    """安全边界自证：跑一次 skill，业务表一行没动，只多了事件日志。"""
    _doc(store, "doc-s", rule_no="AS-01")
    before = kb.query(store, "SELECT COUNT(*) AS n FROM kb_doc")[0]["n"]
    skill_cls = type(_lookup_kb_skill())
    skill_cls().run({"tenant_id": TENANT_A, "rule_no": "AS-01"},
                    SkillContext(store=store, extras={"plan_id": "p"}))
    assert kb.query(store, "SELECT COUNT(*) AS n FROM kb_doc")[0]["n"] == before
    assert len(_kb_events(store)) == 1


def _lookup_kb_skill():
    from maos.skills import registry
    cls = registry.get("kb.retrieve")
    assert cls is not None
    return cls()
