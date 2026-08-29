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

第 8、9 两节是 X-3 轨补的，守的是两类**不报错**的失效：SQLite FTS5 自身的两条
静默行为（BACKLOG `## task-W2` 第 1、2 条），以及 StorePort 那条从未被走过的分支
（`## task-W3` 第 6 条）。两节都先钉「现在到底是什么行为」，再钉「为什么是这个行为」——
不写清楚为什么，下一个人换掉分词器或对齐了列名时，只会看到一条莫名其妙变红的测试。
"""

from __future__ import annotations

import json
import logging

import pytest

from maos import kb
from maos.agents.base import AgentIdentity
from maos.core.store import SqliteStore
from maos.kb import guardrails, retriever
from maos.model.client import ScriptedModelClient
from maos.skills.contract import SkillContext
from maos.skills.invoker import SkillInvoker
from maos.store.sqlite_store import SqliteStorePort

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


# ---------------------------------------------------------------------------
# 8. FTS5 的两条静默行为 —— 实测踩没踩，把结论钉成回归断言
#    出处：docs/BACKLOG.md 的 `## task-W2` 第 1、2 条。两条都不报错。
# ---------------------------------------------------------------------------
def test_fts_shadow_table_is_not_trigram_so_short_queries_still_work(store):
    """本层**没踩** trigram 那个坑，这条钉住它为什么没踩。

    W-2 实测：`tokenize='trigram'` 的影子表对 <3 字符的查询切不出任何 token，
    「退款」这类两字词恒返回空集且不报错。本层走的是另一条路 —— 影子表用缺省的
    unicode61，中文由写入侧的 `kb.fts_text()` 先切成单字（见 `kb/schema.sql` 的注释），
    查询侧走同一个 `kb.tokenize`，所以 1 字 / 2 字都照常命中。

    **把影子表改成 trigram 会让这条红**：那不是测试写错了，是那一改会让所有两字
    中文查询的全文通道当场哑掉，而检索器只会把它读成「库里没有这条知识」。
    """
    ddl = kb.query(
        store, "SELECT sql FROM sqlite_master WHERE name='kb_doc_fts'")[0]["sql"]
    assert "trigram" not in ddl.lower(), (
        "影子表被改成 trigram 了 —— 两字中文查询会恒返回空集，且不报错")

    _doc(store, "doc-refund", title="退款政策", body="退款政策超时未到账，按 AS-002 全额退款")
    _doc(store, "doc-timeout", title="订单超时", body="订单支付超时自动关闭")

    for keyword, expected in (("退款", "doc-refund"), ("超时", "doc-timeout"),
                              ("退", "doc-refund"), ("退款政策", "doc-refund")):
        hits = retriever.retrieve(store, {"tenant_id": TENANT_A, "keyword": keyword})
        ids = [h["doc_id"] for h in hits]
        assert expected in ids, f"keyword={keyword!r}（{len(keyword)} 字）一条都没召回"
        assert hits[0]["channels"]["fts"] > 0, f"keyword={keyword!r} 的全文通道记了 0 分"


def test_fts_channel_normalizes_by_rank_not_by_value(store):
    """全文通道按**名次**归一，不按分值 —— 同分同名次，弱命中仍是正分。

    按分值归一有两处会把命中压成 0：bm25 对弱相关文档给出的 `-rank <= 0`，
    以及 IDF 塌陷时整批分数挤在一起。压成 0 之后 `score_candidates` 又丢掉总分
    <= 0 的文档，「明明命中了」就变成「一条没召回」。
    """
    _doc(store, "doc-a", title="退款政策", body="退款 政策 全额")
    _doc(store, "doc-b", title="退款政策", body="退款 政策 全额")
    _doc(store, "doc-c", title="退款说明",
         body="退款 说明 另有 若干 与 本 条 无关 的 词 用 来 拉 长 文 档")

    hits = {h["doc_id"]: h["channels"]["fts"]
            for h in retriever.retrieve(store, {"tenant_id": TENANT_A,
                                                "keyword": "退款政策"}, limit=10)}

    assert set(hits) == {"doc-a", "doc-b", "doc-c"}
    assert hits["doc-a"] == hits["doc-b"] == 1.0, "文本完全相同却排出了先后 —— 名次没有考虑并列"
    assert 0.0 < hits["doc-c"] < 1.0, "弱命中被压成 0 分 —— 那正是「按分值做阈值」的症状"


def test_bm25_idf_collapse_degrades_order_but_never_drops_hits(store):
    """「退款」出现在过半文档里 -> IDF 塌陷，同批命中的原始分挤成一团。

    这一档必须仍然全是正分：拿分值做绝对阈值的检索器会在小库上把**全部**命中
    过滤掉，而演示库正是小库。这里把 fts 之外三个通道的权重清零，让本通道单独
    决定结果 —— 有任何一条被丢掉，返回的就是空列表。
    """
    for idx in range(4):
        _doc(store, f"doc-{idx}", title=f"退款条目{idx}", body="退款 说明 条款")
    _doc(store, "doc-other", title="包装破损", body="运输箱 压瘪")

    only_fts = {"rule_no": 0.0, "gateway_code": 0.0, "fts": 1.0, "vector": 0.0}
    hits = retriever.retrieve(store, {"tenant_id": TENANT_A, "keyword": "退款"},
                              limit=10, weights=only_fts)

    ids = [h["doc_id"] for h in hits]
    assert ids == ["doc-0", "doc-1", "doc-2", "doc-3"], "IDF 塌陷时有命中被整条丢掉"
    assert all(h["score"] > 0 for h in hits), "命中被压成 0 分"
    # 排序退化成按 doc_id 是 BM25 本身的性质，不是排序坏了 —— 但必须是**确定**的退化。
    assert ids == sorted(ids)


# ---------------------------------------------------------------------------
# 9. StorePort 能力探测分支（`## task-W3` 第 6 条）—— 这条分支从未被真正走过
# ---------------------------------------------------------------------------
class _PortBackedStore(SqliteStore):
    """既能被 `kb.query` 用（有 `_conn`），又实现了 F-2 两条通道的 store。

    真链路上**没有**这样的对象，这正是那条分支从未被走过的原因：核心 `SqliteStore`
    没有 `fts_search` / `vector_search`（能力探测恒不成立），而 `SqliteStorePort`
    故意不叫 `_conn`（传进来 `kb.query` 当场 TypeError）。本类按 F-2 的口径把两条
    通道实现在 `kb_doc` 上，把那条分支跑起来。
    """

    def fts_search(self, table: str, field: str, q: str, limit: int) -> list[tuple[str, float]]:
        match = " OR ".join('"' + t + '"' for t in kb.tokenize(q))
        if not match or int(limit) <= 0:
            return []
        rows = kb.query(
            self,
            f"SELECT doc_id, bm25({table}_fts) AS rank FROM {table}_fts"
            f" WHERE {table}_fts MATCH ? ORDER BY rank, doc_id LIMIT ?",
            (match, int(limit)))
        return [(str(r["doc_id"]), -float(r["rank"])) for r in rows]

    def vector_search(self, table: str, field: str, vec: list[float],
                      limit: int) -> list[tuple[str, float]]:
        rows = kb.query(self, f"SELECT doc_id, {field} AS vec FROM {table}"
                              f" WHERE {field} IS NOT NULL", ())
        scored = [(str(r["doc_id"]), retriever.cosine(vec, json.loads(r["vec"])))
                  for r in rows]
        scored.sort(key=lambda pair: (-pair[1], pair[0]))
        return scored[:int(limit)]


CORPUS = [
    ("doc-rust", "轴承锈蚀退款", "外圈锈蚀，按质量问题全额退款", "AS-002"),
    ("doc-policy", "退款政策", "退款政策超时未到账，全额退款", "AS-002"),
    ("doc-box", "包装破损", "运输箱压瘪，不涉及退款金额", "AS-001"),
    ("doc-late", "签收超时", "签收后超过窗口，按无理由退货处理", "AS-001"),
]


def _seed_corpus(target):
    """两个 store 装**同一份**语料，且都存好向量 —— 向量列缺失时两条通道没法比。"""
    kb.ensure_schema(target)
    for doc_id, title, body, rule_no in CORPUS:
        kb.upsert_doc(target, {
            "tenant_id": TENANT_A, "doc_id": doc_id, "kind": kb.KIND_POLICY,
            "biz_type": "refund", "rule_no": rule_no, "title": title, "body": body,
            "embedding": retriever.embed(f"{title} {body}")})
    return target


def _fresh(cls):
    s = cls()
    s.init_schema()
    return _seed_corpus(s)


QUERY = {"tenant_id": TENANT_A, "biz_type": "refund",
         "rule_no": "AS-002", "keyword": "锈蚀 退款"}


def test_capability_probe_never_fires_on_the_core_store(store):
    """核心 `SqliteStore` 上探不到那两个方法 —— 这条分支在真链路上一次都没走过。"""
    assert not hasattr(store, "fts_search")
    assert not hasattr(store, "vector_search")
    _seed_corpus(store)
    assert retriever.retrieve(store, QUERY), "本地实现自己得先能召回，否则下面没得比"
    assert retriever.port_channel_state(store) == {}, "没有方法却记下了探测结论"


def test_store_port_branch_hits_the_same_docs_as_the_local_implementation():
    """走 StorePort 分支 vs 走本地实现：**命中集合必须一致**（分数可以不同）。"""
    local = _fresh(SqliteStore)
    ported = _fresh(_PortBackedStore)

    local_hits = retriever.retrieve(local, QUERY, limit=10)
    port_hits = retriever.retrieve(ported, QUERY, limit=10)

    assert retriever.port_channel_state(ported) == {"fts_search": True,
                                                    "vector_search": True}, \
        "分支没被走到 —— 这条测试就白写了"
    assert {h["doc_id"] for h in local_hits} == {h["doc_id"] for h in port_hits}
    assert [h["doc_id"] for h in local_hits] == [h["doc_id"] for h in port_hits], \
        "命中一致但次序不同 —— 两条通道的排序口径漂了"
    for local_hit, port_hit in zip(local_hits, port_hits):
        for channel in ("fts", "vector"):
            assert (local_hit["channels"][channel] > 0) == (port_hit["channels"][channel] > 0), \
                f"{channel} 通道在一边有信号、另一边没有"


def test_store_port_branch_never_leaks_across_tenants():
    """F-2 的 `fts_search` 没有租户参数，端口是**全表**查的。

    跨租户不召回因此只能由阶段一的候选集兜住 —— 这条守的正是那一层：
    哪天有人把 `d in candidates` 那句优化掉，本条当场红。
    """
    ported = _fresh(_PortBackedStore)
    kb.upsert_doc(ported, {
        "tenant_id": TENANT_B, "doc_id": "doc-b-rust", "kind": kb.KIND_POLICY,
        "biz_type": "refund", "rule_no": "AS-002", "title": "轴承锈蚀退款",
        "body": "外圈锈蚀，按质量问题全额退款",
        "embedding": retriever.embed("轴承锈蚀退款 外圈锈蚀，按质量问题全额退款")})

    raw = ported.fts_search("kb_doc", "body", "锈蚀", 10)
    assert "doc-b-rust" in {doc_id for doc_id, _ in raw}, "端口本来就该查得到它（全表）"

    got = {h["doc_id"] for h in retriever.retrieve(ported, QUERY, limit=10)}
    assert "doc-b-rust" not in got, "跨租户召回 —— 这是事故，不是效果问题"


def test_real_sqlite_store_port_diverges_from_kb_schema_on_the_key_column(store):
    """真 `SqliteStorePort` 在本层这份 schema 上两条通道**都不再抛**。

    **本条的性质变过一次，名字留着是为了让 git blame 指得回去。** 原先它钉的是
    「现状」：F-2 约定源表主键列名固定为 `id`，而 `kb_doc` 的主键是
    `(tenant_id, doc_id)`、影子表存的也是 `doc_id`，两条口径都没写错，只是没对齐，
    于是两条通道双双抛 `LookupError: no such column: id`，端口分支恒退化。
    当时的 docstring 写着「哪天这条不抛了，说明有人把列名对齐了，那时本条该跟着
    改，而不是删」。T13 轨对齐了（`kb_doc.id` 生成列 + 影子表同名列），所以这条
    从**钉现状**变成了**守回归**：谁再把 `id` 列拿掉，本条当场红。

    第三段同理 —— 适配器故意不叫 `_conn`，以前传进 `kb.query` 当场 TypeError；
    现在知识层认 StorePort（`kb.port_of`），它就是一个合法的 store。
    """
    _seed_corpus(store)
    port = SqliteStorePort(store)

    # 全文：查询串必须先过 kb.fts_text()，影子表里存的就是它切过的形态。
    fts = port.fts_search("kb_doc", "body", kb.fts_text("退款政策"), 5)
    assert fts, "全文通道一条都没召回 —— 影子表的 id 列或分词口径又漂了"
    assert all(row_id.startswith(f"{TENANT_A}{kb.DOC_ROW_ID_SEP}") for row_id, _ in fts), \
        f"端口返回的不是 F-2 口径的源表主键：{fts}"

    vectors = port.vector_search("kb_doc", "embedding", retriever.embed("退款政策"), 5)
    assert {row_id for row_id, _ in vectors} == {
        kb.doc_row_id(TENANT_A, doc_id) for doc_id, *_ in CORPUS}, \
        "向量通道没有覆盖整份语料 —— kb_doc.id 生成列又没了"
    scores = [score for _, score in vectors]
    assert scores == sorted(scores, reverse=True), "F-2 附则：分数必须降序（越大越相关）"

    # 反过来也钉住：适配器现在是知识层认得的 store，不再撞 TypeError。
    assert kb.query(port, "SELECT 1 AS one", ()) == [{"one": 1}]
    assert kb.port_of(port) is port and kb.port_of(store) is None, \
        "判据漂了 —— 核心 Store 必须仍走 _conn 那条老路径"


def test_unusable_port_degrades_once_and_keeps_returning_hits(caplog):
    """通道走不通时：降级到本地实现、结果照常，且**只告警一次**。

    每次检索都抛一遍再吞掉，日志会被刷满，而没有人看得出这条通道其实一直没通 ——
    这正是本轨要拆的那类「有症状但没人读得懂」的失效。
    """
    class _BrokenPortStore(SqliteStore):
        def fts_search(self, table, field, q, limit):
            raise LookupError("no such column: id")

        def vector_search(self, table, field, vec, limit):
            raise LookupError("no such column: id")

    broken = _fresh(_BrokenPortStore)
    with caplog.at_level(logging.WARNING, logger="maos.kb"):
        first = retriever.retrieve(broken, QUERY, limit=10)
        second = retriever.retrieve(broken, QUERY, limit=10)

    assert first and [h["doc_id"] for h in first] == [h["doc_id"] for h in second]
    assert retriever.port_channel_state(broken) == {"fts_search": False,
                                                    "vector_search": False}
    warned = [r for r in caplog.records if "StorePort." in r.getMessage()]
    assert len(warned) == 2, f"两条通道各该只告警一次，实际 {len(warned)} 条"
    assert {h["doc_id"] for h in first} == {
        h["doc_id"] for h in retriever.retrieve(_fresh(SqliteStore), QUERY, limit=10)}, \
        "降级之后的结果与本地实现不一致 —— 降级把召回改小了"
