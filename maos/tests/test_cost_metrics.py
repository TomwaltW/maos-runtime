"""成本与 Metrics 挂到 trace_id 的行为契约（T29）。

复赛「工程落地与安全审计 30%」这一维直接点到两条：加分项「trace、Log、Metrics 和
评测数据都要求关联到同一个 Run id」，失分项「成本无法量化」。本轨补的就是这个洞 ——
`ModelResponse` 早有 `tokens_in` / `tokens_out`，但全仓除 `model/client.py` 自己外
**零引用**，主调用点一律 `.complete(...).text`，用量在那一行被丢掉。

**Run id 不另造**：`trace_id` 已经是 `plan` / `task` / `event_log` 三张表的关联键，
新表 `model_usage` 挂的就是它。发明第二个关联键等于让「关联到同一个 Run id」这句话
落空，而屏幕上还是有一堆 id。

四组，各自守一件会被悄悄做坏的事：

1. **记账口径只有一处**（`test_estimated_*`）—— `estimated` 由 client 的**类型**判定
   （`core/store.py::usage_is_estimated`），不由 `ModelResponse.model` 的字符串判。
   两者同源的话，核验器第 8 项判据 c 就退化成自己跟自己对账。
   缺省路径全是 `ScriptedModelClient`，它的 token 是 `len(user)//4` 的估算 ——
   把估算印成真实计费，比不做成本量化更坏。

2. **归属绑定要跟着 `run()` 走**（`test_ask_*`）—— `ask()` 的签名里没有 ctx，
   `trace_id` 只在 `TaskContext` 上。`BaseAgent.__init_subclass__` 在 `run()` 进出时
   绑 ContextVar，`ask()` 从那里取。绑不上的**照旧留空**，不许编一个。

3. **「不知道」与「知道且为零」分得开**（`test_cost_view_*`）—— 两者都能让总数是 0。
   合成一个 0，「成本记账没接上」就长得像「这条链路很省」。

4. **第 8 项的四条判据真的会红**（`test_check8_*`）—— 每条都单独造一个负例。
   判据 d（归属不上的行必须在 trace.json 里逐条点名）是判据 a 的看门人：
   没有它，把所有 trace_id 清空就能让 a 无条件全绿，而成本归因整个消失。
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sqlite3
import sys
import types

import pytest

from maos.agents.base import (AgentIdentity, AgentOutput, BaseAgent, TaskContext,
                              CALL_SITE_ASK)
from maos.core.store import SqliteStore, record_model_usage, usage_is_estimated
from maos.model.client import (GatewayModelClient, ModelResponse, ScriptedModelClient,
                               Tier)
from maos.obs import trace as trace_mod
from maos.skills.builtin.code_repo_patch import CodeRepoPatchSkill
from maos.skills.builtin.req_normalize import ReqNormalizeSkill
from maos.skills.contract import SkillContext

ROOT = pathlib.Path(__file__).resolve().parents[2]

TRACE = "trace-t29"
PLAN = "plan-t29"


def _load_script(name: str) -> types.ModuleType:
    """``scripts/`` 不是包，只能按路径加载（idiom 同 test_repro_path）。"""
    key = f"_t29_{name}"
    spec = importlib.util.spec_from_file_location(key, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


verify = _load_script("verify")


@pytest.fixture
def store(tmp_path) -> SqliteStore:
    s = SqliteStore(str(tmp_path / "t29.db"))
    s.init_schema()
    return s


# ===========================================================================
# 0. 新增表：只加不改
# ===========================================================================
def test_model_usage_is_a_new_table_not_a_new_column(store):
    """铁律 1：只许新增表。既有表的 DDL 由 `test_contracts_frozen.py` 指纹守着，
    这里只正面确认新表建起来了，且带 trace_id 索引（成本视图按它聚合）。"""
    conn = sqlite3.connect(store._conn.execute("PRAGMA database_list").fetchone()[2])
    try:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "model_usage" in names
        idx = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        assert "idx_model_usage_trace" in idx
        cols = {r[1] for r in conn.execute("PRAGMA table_info(model_usage)")}
    finally:
        conn.close()
    # 派单 §5.1 点名要覆盖的字段，一个不许少
    assert {"trace_id", "task_id", "agent_role", "model", "tier", "tokens_in",
            "tokens_out", "latency_ms", "estimated", "created_at"} <= cols


# ===========================================================================
# 1. 记账口径：估算不许印成真实计费
# ===========================================================================
def test_estimated_is_decided_by_client_type_not_by_the_model_string():
    """判 client 的类型。判 `ModelResponse.model` 的前缀会让核验器第 8 项判据 c
    退化成自己跟自己对账 —— 那条判据要的正是**两个独立来源**相符。"""
    assert usage_is_estimated(ScriptedModelClient({})) is True
    assert usage_is_estimated(
        GatewayModelClient("https://h", "k", "qwen-max")) is False


def test_estimated_flag_survives_a_lying_model_string(store):
    """client 是 Scripted，就算它把 model 写成真模型名，这一行照旧是估算。"""
    record_model_usage(store, ModelResponse(text="x", model="qwen-max"),
                       client=ScriptedModelClient({}), agent_role="manager",
                       call_site="t", tier=Tier.STRONG, latency_ms=1, trace_id=TRACE)
    assert store.list_model_usage(trace_id=TRACE)[0]["estimated"] == 1


def test_record_writes_every_field_it_was_given(store):
    record_model_usage(
        store, ModelResponse(text="x", tokens_in=11, tokens_out=7, model="scripted-medium"),
        client=ScriptedModelClient({}), agent_role="coding", call_site="cs",
        tier=Tier.MEDIUM, latency_ms=42, trace_id=TRACE, plan_id=PLAN, task_id="task-1")
    row = store.list_model_usage(trace_id=TRACE)[0]
    assert (row["agent_role"], row["call_site"], row["tier"]) == ("coding", "cs", "medium")
    assert (row["tokens_in"], row["tokens_out"], row["latency_ms"]) == (11, 7, 42)
    assert (row["plan_id"], row["task_id"]) == (PLAN, "task-1")


def test_no_store_is_a_noop_not_a_crash():
    """`BaseAgent.__init__` 的 store 缺省 None，演示主线上确有这么构造的场景。
    记账跳过是对的；抛异常会把「没接线」升级成「跑不动」。"""
    record_model_usage(None, ModelResponse(text="x"), client=ScriptedModelClient({}),
                       agent_role="r", call_site="c", tier="light", latency_ms=0)


def test_store_failure_warns_but_does_not_fail_the_model_call(store, caplog):
    """一次已经成功的模型调用不该因为记账挂掉（口径同 `client.py::_safe_int`）；
    但必须留声 —— 静默吞掉等于成本统计凭空偏低，而屏幕上看不出来。"""
    class _Broken(SqliteStore):
        def insert_model_usage(self, row):
            raise sqlite3.OperationalError("no such table: model_usage")

    broken = _Broken(":memory:")
    with caplog.at_level("WARNING"):
        record_model_usage(broken, ModelResponse(text="x"),
                           client=ScriptedModelClient({}), agent_role="r",
                           call_site="c", tier="light", latency_ms=0)
    assert "模型用量落库失败" in caplog.text


def test_empty_trace_id_is_recorded_as_is(store):
    """归属不上就留空。随手编一个 trace_id 让它看起来有归属，是这里最坏的错。"""
    record_model_usage(store, ModelResponse(text="x"), client=ScriptedModelClient({}),
                       agent_role="manager", call_site="c", tier="strong", latency_ms=0)
    assert store.list_model_usage(trace_id="")[0]["agent_role"] == "manager"
    # 空串是有意义的查询，不能退化成「取全部」
    assert store.list_model_usage(trace_id=TRACE) == []


# ===========================================================================
# 2. 三个调用点：归属从哪来
# ===========================================================================
IDENTITY = AgentIdentity(agent_id="probe", role="probe", duty="d",
                         model_tier=Tier.STRONG, write_scope=frozenset({"artifact"}))


class _Probe(BaseAgent):
    """只做一件事：在 run() 里调一次 ask()。归属应当由基类绑好。"""
    identity = IDENTITY

    def run(self, ctx: TaskContext) -> AgentOutput:
        self.text = self.ask("sys", "user")
        return AgentOutput(status="ok")


def _ctx(**kw) -> TaskContext:
    base = dict(plan_id=PLAN, task_id="task-1", trace_id=TRACE, attempt=1,
                inputs={}, acceptance=[], risk_level="L")
    base.update(kw)
    return TaskContext(**base)


def test_ask_inside_run_attributes_to_the_ctx_trace_id(store):
    """`ask()` 看不到 ctx，但 run() 进出时绑了归属 —— 用量因此挂在真正的 Run id 上。

    这条路就是 `agents/reviewer.py::review_after_gate` 走的那条（它不经 Worker 队列，
    直接 `reviewer.run(ctx)`），所以绑定装在 `run()` 上而不是 `@register` 上。
    """
    _Probe(ScriptedModelClient({}), store=store).run(_ctx())
    row = store.list_model_usage(trace_id=TRACE)[0]
    assert (row["trace_id"], row["plan_id"], row["task_id"]) == (TRACE, PLAN, "task-1")
    assert row["agent_role"] == "probe" and row["call_site"] == CALL_SITE_ASK


def test_ask_outside_run_records_an_honest_blank(store):
    """`ManagerAgent.plan()` 那条路：它跑在 `create_plan` **之前**，那一刻没有
    trace_id 可挂。落空串是如实记录，由成本视图与核验器点名，不是缺省值填错。"""
    _Probe(ScriptedModelClient({}), store=store).ask("sys", "user")
    assert store.list_model_usage(trace_id="")[0]["agent_role"] == "probe"


def test_attribution_does_not_leak_between_runs(store):
    """Worker 每个 role 只建一个 Agent 实例反复用（`runtime/worker.py`）。
    归属若挂在实例上，A 任务的成本会记到 B 任务头上。"""
    agent = _Probe(ScriptedModelClient({}), store=store)
    agent.run(_ctx(trace_id="tr-a", task_id="task-a"))
    agent.run(_ctx(trace_id="tr-b", task_id="task-b"))
    agent.ask("sys", "user")                      # run 之外：必须还原成空
    assert [r["task_id"] for r in store.list_model_usage()] == ["task-a", "task-b", None]


def test_ask_returns_the_same_text_as_before(store):
    """接住 ModelResponse 只为取用量，返回值形状不变 —— 调用方零改动。"""
    agent = _Probe(ScriptedModelClient({"user": "答案"}), store=store)
    assert agent.ask("sys", "user") == "答案"


NORMALIZED = json.dumps({"normalized_goal": "把 A 改成 B", "constraints": [],
                         "acceptance_suggestions": []}, ensure_ascii=False)
PATCH = json.dumps({"files": [{"path": "a.py", "diff": "@@"}], "summary": "s",
                    "self_check": {"build": "pass", "lint": "pass"}}, ensure_ascii=False)


def _skill_ctx(store, model, **extras) -> SkillContext:
    base = {"plan_id": PLAN, "task_id": "task-1", "trace_id": TRACE}
    base.update(extras)
    return SkillContext(model=model, store=store, identity=IDENTITY, extras=base)


def test_req_normalize_records_usage_from_extras(store):
    """skill 侧的归属键从 `extras` 取 —— invoker 已经把 plan_id / task_id / trace_id
    一路带到这里（`skills/invoker.py`），不需要另造一个 Run id。"""
    ReqNormalizeSkill().run(
        {"goal": "把 A 改成 B"},
        _skill_ctx(store, ScriptedModelClient({"原始目标": NORMALIZED})))
    row = store.list_model_usage(trace_id=TRACE)[0]
    assert (row["trace_id"], row["task_id"]) == (TRACE, "task-1")
    assert row["call_site"].endswith("ReqNormalizeSkill.run")
    assert row["tokens_in"] > 0 and row["tokens_out"] > 0


def test_code_repo_patch_records_usage_from_extras(store):
    CodeRepoPatchSkill().run(
        {"title": "修时区", "inputs": {}, "acceptance": []},
        _skill_ctx(store, ScriptedModelClient({"修时区": PATCH})))
    row = store.list_model_usage(trace_id=TRACE)[0]
    assert row["trace_id"] == TRACE
    assert row["call_site"].endswith("CodeRepoPatchSkill.run")


def test_skill_without_store_still_runs(store):
    """记账不接线时 skill 照旧产出 —— 记账不是 skill 的前置条件。"""
    out = ReqNormalizeSkill().run(
        {"goal": "把 A 改成 B"},
        _skill_ctx(None, ScriptedModelClient({"原始目标": NORMALIZED})))
    assert out["normalized_goal"] == "把 A 改成 B"


# ===========================================================================
# 3. 成本视图：不知道 ≠ 知道且为零
# ===========================================================================
def _row(**kw) -> dict:
    base = {"agent_role": "coding", "call_site": "cs", "task_id": "task-1",
            "tokens_in": 10, "tokens_out": 5, "latency_ms": 3, "estimated": 1}
    base.update(kw)
    return base


def test_cost_view_splits_by_role_and_ranks_tasks_by_spend():
    view = trace_mod.cost_view([
        _row(agent_role="coding", task_id="task-a", tokens_in=100, tokens_out=0),
        _row(agent_role="reviewer", task_id="task-b", tokens_in=10, tokens_out=1),
        _row(agent_role="coding", task_id="task-a", tokens_in=5, tokens_out=0),
    ])
    assert view["calls"] == 3 and view["tokens_total"] == 116
    assert [b["role"] for b in view["by_role"]] == ["coding", "reviewer"]
    assert view["top_task"]["task_id"] == "task-a"
    assert view["top_task"]["tokens_total"] == 105


def test_cost_view_ordering_is_deterministic():
    """`verify.py` 第 4 项拿库重放与 trace.json 逐字节比对：顺序不稳，
    没人动过证据它也会红。同分按 key 升序。"""
    rows = [_row(agent_role=r, tokens_in=1, tokens_out=0) for r in ("b", "a", "c")]
    assert [b["role"] for b in trace_mod.cost_view(rows)["by_role"]] == ["a", "b", "c"]


def test_cost_view_flags_all_estimated():
    assert trace_mod.cost_view([_row(estimated=1)])["all_estimated"] is True
    assert trace_mod.cost_view([_row(estimated=1), _row(estimated=0)])["all_estimated"] is False
    # 一次都没调时不许自称「全是估算」—— 那句话是给数字加的注解，没数字就没注解
    assert trace_mod.cost_view([])["all_estimated"] is False


def test_cost_view_note_travels_with_the_numbers():
    """估算口径这句话必须跟着数字一起进证据束（派单 §5.0 第二条）。"""
    assert "ScriptedModelClient" in trace_mod.cost_view([_row()])["note"]


def test_zero_calls_is_not_the_same_claim_as_zero_cost():
    """calls=0 有两种成因：真的没调，和调了没记（构造 Agent 时没传 store=）。
    它们在这个数字上分不开，所以必须有一句话说清楚。"""
    assert trace_mod.cost_view([])["zero_calls_note"] is not None
    assert trace_mod.cost_view([_row()])["zero_calls_note"] is None


def test_unavailable_is_not_zero():
    view = trace_mod.cost_view([], unavailable="后端没有 list_model_usage")
    assert view["available"] is False and view["unavailable_reason"]
    # 取不到时不许再叠一句「一条都没记到」—— 那是另一件事
    assert view["zero_calls_note"] is None


def test_export_trace_hangs_cost_on_the_same_run_id(store):
    """成本挂的就是 span 树用的那个 trace_id —— 复赛规则要的「同一个 Run id」。"""
    store.insert_plan({"plan_id": PLAN, "trace_id": TRACE, "goal": "g", "state": "RUNNING"})
    record_model_usage(store, ModelResponse(text="x", tokens_in=9, model="scripted-strong"),
                       client=ScriptedModelClient({}), agent_role="manager",
                       call_site="c", tier="strong", latency_ms=0, trace_id=TRACE)
    doc = trace_mod.export_trace(store, PLAN)
    assert doc["trace_id"] == TRACE
    assert doc["cost"]["available"] is True and doc["cost"]["tokens_total"] == 9


def test_export_trace_says_so_when_the_backend_cannot_report_cost(store):
    """后端没有 list_model_usage 时标 available=false，而不是报一个 0。"""
    class _Old:
        def __init__(self, inner): self._i = inner
        def get_plan(self, p): return self._i.get_plan(p)
        def list_tasks(self, p): return self._i.list_tasks(p)
        def list_artifacts(self, t): return self._i.list_artifacts(t)
        def list_event_log(self, p): return self._i.list_event_log(p)

    store.insert_plan({"plan_id": PLAN, "trace_id": TRACE, "goal": "g", "state": "RUNNING"})
    cost = trace_mod.export_trace(_Old(store), PLAN)["cost"]
    assert cost["available"] is False and "list_model_usage" in cost["unavailable_reason"]


def test_unattributed_usage_names_the_orphans(tmp_path, store):
    """归属不上的行单独点名，不并进任何一棵树（口径同 stray_events）。"""
    record_model_usage(store, ModelResponse(text="x"), client=ScriptedModelClient({}),
                       agent_role="manager", call_site="c", tier="strong", latency_ms=0)
    record_model_usage(store, ModelResponse(text="x"), client=ScriptedModelClient({}),
                       agent_role="coding", call_site="c", tier="medium",
                       latency_ms=0, trace_id=TRACE)
    orphans = trace_mod.unattributed_usage(str(tmp_path / "t29.db"))
    assert [r["agent_role"] for r in orphans] == ["manager"]


# ===========================================================================
# 4. 核验器第 8 项：四条判据各自的负例
# ===========================================================================
def _build_case(tmp_path, name: str, *, usage: list[dict], named: list[int] | None = None,
                plans=((PLAN, TRACE),), tasks=("task-1",), with_table: bool = True):
    """造一个 verify.Case：plan / task / model_usage 三张表 + 一份最小 trace.json。"""
    db = tmp_path / f"{name}.db"
    store = SqliteStore(str(db))
    store.init_schema()
    for plan_id, trace_id in plans:
        store.insert_plan({"plan_id": plan_id, "trace_id": trace_id,
                           "goal": "g", "state": "DONE"})
    for task_id in tasks:
        store.insert_task({"task_id": task_id, "plan_id": PLAN, "trace_id": TRACE,
                           "role": "coding", "title": "t", "state": "DONE"})
    for row in usage:
        store.insert_model_usage(row)
    seqs = named if named is not None else [
        r["seq"] for r in store.list_model_usage(trace_id="")]
    if not with_table:
        sqlite3.connect(str(db)).execute("DROP TABLE model_usage").connection.commit()

    conn = verify.connect_ro(str(db))
    trace = {"unattributed_usage": [{"seq": s} for s in seqs]}
    return verify.Case(name=name, directory=str(tmp_path), db_path=str(db), conn=conn,
                       tables=verify.table_names(conn), trace=trace, result={})


def _usage(**kw) -> dict:
    base = {"trace_id": TRACE, "plan_id": PLAN, "task_id": "task-1",
            "agent_role": "coding", "call_site": "cs", "model": "scripted-medium",
            "tier": "medium", "tokens_in": 9, "tokens_out": 3, "latency_ms": 1,
            "estimated": True}
    base.update(kw)
    return base


def test_check8_passes_on_well_formed_usage(tmp_path):
    chk = verify.check_cost_attribution([_build_case(tmp_path, "ok", usage=[_usage()])])
    assert chk.status == verify.PASS and chk.total == 3 and chk.passed == 3


def test_check8_a_dangling_trace_id_is_a_fake_attribution(tmp_path):
    """失败意味着：成本挂在了一条不存在的 run 上，归因是假的。"""
    chk = verify.check_cost_attribution(
        [_build_case(tmp_path, "bad", usage=[_usage(trace_id="trace-nope")])])
    assert chk.status == verify.FAIL
    assert any("在 plan 表里不存在" in n for n in chk.notes)


def test_check8_b_dangling_task_id_makes_the_split_fiction(tmp_path):
    """失败意味着：「哪个 task 最贵」指着一个库里没有的任务，分摊是编的。"""
    chk = verify.check_cost_attribution(
        [_build_case(tmp_path, "bad", usage=[_usage(task_id="task-nope")])])
    assert chk.status == verify.FAIL
    assert any("在 task 表里不存在" in n for n in chk.notes)


def test_check8_c_estimate_printed_as_real_billing(tmp_path):
    """失败意味着：估算被印成了真实计费 —— 派单 §5.0 第二条那条红线。"""
    chk = verify.check_cost_attribution(
        [_build_case(tmp_path, "bad", usage=[_usage(estimated=False)])])
    assert chk.status == verify.FAIL
    assert any("估算与真实计费的标记对不上" in n for n in chk.notes)


def test_check8_c_real_billing_that_cannot_name_its_model(tmp_path):
    """model 为空时印证不了，但「声称真实计费却说不出是哪个模型」照旧判负。"""
    chk = verify.check_cost_attribution(
        [_build_case(tmp_path, "bad", usage=[_usage(model="", estimated=False)])])
    assert chk.status == verify.FAIL
    assert any("说不出是哪个模型" in n for n in chk.notes)


def test_check8_c_blank_model_on_an_estimate_is_only_noted(tmp_path):
    """方向安全的那一半：已经标成估算了，印证不了就只记一笔 info，不判负。
    出处是 flows/scenario_2.py 的 FlakyModel（直接构造 ModelResponse 不填 model）。"""
    chk = verify.check_cost_attribution(
        [_build_case(tmp_path, "ok", usage=[_usage(model="", estimated=True)])])
    assert chk.status == verify.PASS
    assert any("model 列为空" in n for n in chk.notes)


def test_check8_d_hidden_orphans_fail(tmp_path):
    """判据 d 是判据 a 的看门人：没有它，把所有 trace_id 清空就能让 a 无条件全绿，
    而成本归因整个消失。归属不上的行必须在 trace.json 里逐条点名。"""
    hidden = _build_case(tmp_path, "hidden", usage=[_usage(trace_id="", task_id=None)],
                         named=[])
    chk = verify.check_cost_attribution([hidden])
    assert chk.status == verify.FAIL
    assert any("被藏起来了" in n for n in chk.notes)


def test_check8_d_named_orphans_pass_and_get_counted(tmp_path):
    """如实记录再点名，好过编一个 trace_id 让它看起来有归属。"""
    chk = verify.check_cost_attribution(
        [_build_case(tmp_path, "named", usage=[_usage(trace_id="", task_id=None)])])
    assert chk.status == verify.PASS
    assert any("归属不上任何 Run id" in n for n in chk.notes)


def test_check8_skips_when_nothing_was_recorded(tmp_path):
    """空转也算没跑：0/0 不许判 PASS（文件头「SKIP 的纪律」）。"""
    chk = verify.check_cost_attribution([_build_case(tmp_path, "idle", usage=[])])
    assert chk.status == verify.SKIP and chk.passed == 0
    assert "空转" in chk.skip_reason


def test_check8_skips_when_the_table_is_absent(tmp_path):
    """早于 T29 的证据束里没有这张表 —— 判 SKIP，看得出没跑，不进分子。"""
    chk = verify.check_cost_attribution(
        [_build_case(tmp_path, "old", usage=[], with_table=False)])
    assert chk.status == verify.SKIP and "model_usage" in chk.skip_reason


def test_check8_emits_no_warn_lines(tmp_path):
    """本项只出 info，不出 warn —— warn 是有基线的（`test_verify_warn.py`），
    把「查过了，是预期」塞进 warn 会让那条基线永远在漂。"""
    chk = verify.check_cost_attribution(
        [_build_case(tmp_path, "ok", usage=[_usage(), _usage(trace_id="", task_id=None)])])
    assert not [n for n in chk.notes if n.startswith("warn:")]


def test_check8_is_registered_in_the_checks_list():
    assert verify.check_cost_attribution in verify.CHECKS
