"""失败的模型调用要留账 —— T54 §1（cumora 折账第 7 条）。

这条 BACKLOG 的原话是：一次超时或被限流的调用照样烧掉了 input token，但在
``cost_view`` 里根本不存在，而且一句提示都没有。

本文件守三件事：
1. 失败调用落进 ``model_call_failure``，带得上 trace 归属、错误类型、墙钟；
2. 它**不**落进 ``model_usage`` —— 编一行 0 token 会让「失败」伪装成「很便宜」；
3. 成本视图里看得见它，且「没查成」与「一次没失败」在结构上分得开。

每条用例都断到**具体那一道**判据（错误类型、表名、available 旗），
只断「没抛异常」是测不出这些的。
"""

from __future__ import annotations

import pytest

from maos.agents.base import (AgentIdentity, AgentOutput, BaseAgent, CALL_SITE_ASK,
                              TaskContext)
from maos.core.store import (FAILURE_MSG_LIMIT, SqliteStore, record_model_failure,
                             record_model_usage)
from maos.model.client import ModelResponse, Tier
from maos.obs.trace import cost_view, failure_view


class _BoomClient:
    """每次 complete 都炸的模型客户端。``model`` 属性照真客户端的样子留着。"""

    model = "gw-model-x"

    def __init__(self, exc: Exception | None = None) -> None:
        self.exc = exc or TimeoutError("网关 30s 未响应")
        self.calls = 0

    def complete(self, *, system: str, user: str, tier: str) -> ModelResponse:
        self.calls += 1
        raise self.exc


class _EchoAgent(BaseAgent):
    identity = AgentIdentity(
        agent_id="agent-t54", role="coding", duty="测试用",
        model_tier=Tier.MEDIUM,
    )

    def run(self, ctx: TaskContext) -> AgentOutput:      # pragma: no cover - 不走
        return AgentOutput(status="ok")


@pytest.fixture()
def store(tmp_path):
    s = SqliteStore(str(tmp_path / "t54.db"))
    s.init_schema()
    return s


# ---------------------------------------------------------------------------
# 1. 落账本身
# ---------------------------------------------------------------------------
def test_failure_lands_with_attribution_and_error_kind(store):
    record_model_failure(
        store, TimeoutError("网关 30s 未响应"),
        agent_role="coding", call_site=CALL_SITE_ASK, tier=Tier.MEDIUM,
        latency_ms=30011, model="gw-model-x",
        trace_id="trace_t54", plan_id="plan_t54", task_id="task_t54",
    )
    rows = store.list_model_call_failures(trace_id="trace_t54")
    assert len(rows) == 1
    row = rows[0]
    # 断到具体字段：错误**类型**要留下来，不能只留一句人话。
    assert row["error_kind"] == "TimeoutError"
    assert row["error_msg"] == "网关 30s 未响应"
    assert (row["plan_id"], row["task_id"]) == ("plan_t54", "task_t54")
    assert row["call_site"] == CALL_SITE_ASK
    assert row["latency_ms"] == 30011       # 失败也耗墙钟，这个数不能丢
    assert row["model"] == "gw-model-x"


def test_failure_does_not_touch_model_usage(store):
    """最关键的一条：失败不许往 model_usage 编 0 token 的行。"""
    record_model_failure(
        store, RuntimeError("模型网关返回 HTTP 429：rate limited"),
        agent_role="coding", call_site=CALL_SITE_ASK, tier=Tier.MEDIUM,
        latency_ms=120, trace_id="trace_t54",
    )
    assert store.list_model_usage(trace_id="trace_t54") == []
    assert len(store.list_model_call_failures(trace_id="trace_t54")) == 1


def test_error_msg_is_truncated_not_unbounded(store):
    record_model_failure(
        store, RuntimeError("x" * 5000),
        agent_role="coding", call_site=CALL_SITE_ASK, tier=Tier.MEDIUM,
        latency_ms=1, trace_id="trace_t54",
    )
    msg = store.list_model_call_failures(trace_id="trace_t54")[0]["error_msg"]
    assert len(msg) == FAILURE_MSG_LIMIT


def test_no_store_is_a_skip_not_a_crash():
    """``store=None`` 照旧只是跳过 —— 口径与 record_model_usage 一致。"""
    record_model_failure(None, RuntimeError("boom"), agent_role="coding",
                         call_site=CALL_SITE_ASK, tier=Tier.MEDIUM, latency_ms=1)


def test_recording_failure_never_swallows_the_original_error(store, caplog):
    """记账自己挂了，也不许把原始异常换成记账异常。"""

    class _BrokenStore:
        def insert_model_call_failure(self, row: dict) -> None:
            raise sqlite_error()

    def sqlite_error() -> Exception:
        return RuntimeError("表不存在")

    record_model_failure(_BrokenStore(), TimeoutError("原始错误"),
                         agent_role="coding", call_site=CALL_SITE_ASK,
                         tier=Tier.MEDIUM, latency_ms=1)
    # 不抛，但必须留声 —— 静默吞掉等于这次失败在成本视图里查不到。
    assert any("失败调用落库失败" in r.message or "落库失败" in r.getMessage()
               for r in caplog.records)


# ---------------------------------------------------------------------------
# 2. 三个调用点真的接上了
# ---------------------------------------------------------------------------
def test_agent_ask_records_failure_and_reraises(store):
    agent = _EchoAgent(_BoomClient(), store=store)
    with pytest.raises(TimeoutError):
        agent.ask("sys", "user")            # 异常必须原样上抛
    rows = store.list_model_call_failures()
    assert len(rows) == 1
    assert rows[0]["call_site"] == CALL_SITE_ASK
    assert rows[0]["error_kind"] == "TimeoutError"
    assert rows[0]["agent_role"] == "coding"
    # 归属：ask 不在 run() 里跑，拿不到 trace_id 就如实留空，不许编一个。
    assert rows[0]["trace_id"] == ""


def test_agent_ask_success_still_only_touches_model_usage(store):
    class _OkClient:
        model = "gw-model-x"

        def complete(self, *, system: str, user: str, tier: str) -> ModelResponse:
            return ModelResponse(text="ok", tokens_in=10, tokens_out=3,
                                 model="gw-model-x", meta={})

    agent = _EchoAgent(_OkClient(), store=store)
    assert agent.ask("sys", "user") == "ok"
    assert len(store.list_model_usage()) == 1
    assert store.list_model_call_failures() == []      # 成功不许进失败表


def test_skill_call_sites_record_failure(store):
    """两个 skill 调用点各自接上（判据落在 call_site 上，认得出是哪一处）。"""
    from maos.skills.builtin.code_repo_patch import CALL_SITE as PATCH_SITE
    from maos.skills.builtin.req_normalize import CALL_SITE as NORM_SITE

    for site in (NORM_SITE, PATCH_SITE):
        record_model_failure(store, RuntimeError("模型网关不可达"),
                             agent_role="coding", call_site=site,
                             tier=Tier.MEDIUM, latency_ms=5, trace_id="trace_t54")
    sites = {r["call_site"] for r in store.list_model_call_failures()}
    assert sites == {NORM_SITE, PATCH_SITE}


# ---------------------------------------------------------------------------
# 3. 成本视图看得见
# ---------------------------------------------------------------------------
def test_cost_view_lists_failures_without_mixing_them_into_totals():
    usage = [{"agent_role": "coding", "call_site": CALL_SITE_ASK, "tokens_in": 100,
              "tokens_out": 20, "latency_ms": 500, "estimated": 0, "task_id": "t1"}]
    failures = [{"call_site": CALL_SITE_ASK, "error_kind": "TimeoutError",
                 "latency_ms": 30000},
                {"call_site": CALL_SITE_ASK, "error_kind": "TimeoutError",
                 "latency_ms": 30000},
                {"call_site": CALL_SITE_ASK, "error_kind": "RuntimeError",
                 "latency_ms": 80}]
    view = cost_view(usage, failures=failures)

    # 成功那本账一个数都没被污染。
    assert view["calls"] == 1
    assert view["tokens_total"] == 120
    assert view["latency_ms"] == 500

    f = view["failures"]
    assert f["available"] is True
    assert f["calls"] == 3
    assert f["latency_ms"] == 60080
    # 排序恒定：次数降序、名字升序。
    assert [k["error_kind"] for k in f["by_error_kind"]] == ["TimeoutError", "RuntimeError"]
    assert f["by_error_kind"][0]["calls"] == 2


def test_unqueried_failures_are_not_reported_as_zero():
    """「没查成」与「一次没失败」必须分得开 —— 这正是 cost_view 自己那条铁律。"""
    unknown = cost_view([], failures=None,
                        failures_unavailable="store 未实现 list_model_call_failures")
    none_happened = cost_view([], failures=[])

    assert unknown["failures"]["available"] is False
    assert unknown["failures"]["unavailable_reason"]
    assert none_happened["failures"]["available"] is True
    # 两者的 calls 都是 0 —— 所以判据只能是 available，不能是这个数字。
    assert unknown["failures"]["calls"] == none_happened["failures"]["calls"] == 0


def test_failure_view_never_reports_a_token_number():
    """失败行没有 token 数，视图里就一个 token 字段都不许出现（出了就是编的）。"""
    view = failure_view([{"call_site": CALL_SITE_ASK, "error_kind": "TimeoutError",
                          "latency_ms": 10}])
    assert not [k for k in view if "token" in k]


def test_export_trace_carries_failures_end_to_end(store):
    """端到端：落一次失败，导出的 trace 里读得到它。"""
    from maos.obs.trace import export_trace

    store.insert_plan({"plan_id": "plan_t54", "goal": "g", "state": "DONE",
                       "trace_id": "trace_t54", "dag": {}})
    record_model_failure(store, TimeoutError("网关 30s 未响应"),
                         agent_role="coding", call_site=CALL_SITE_ASK,
                         tier=Tier.MEDIUM, latency_ms=30000, trace_id="trace_t54")
    tree = export_trace(store, "plan_t54")
    failures = tree["cost"]["failures"]
    assert failures["available"] is True
    assert failures["calls"] == 1
    assert failures["by_error_kind"][0]["error_kind"] == "TimeoutError"
