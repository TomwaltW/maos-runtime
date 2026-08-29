"""规划期调用的归属，与场景 6 的知识库有没有内容（Y-2）。

两件事，各守一条**已经被记了四次账**的缺口：

  · **规划期调用无处安放**（§1）。`ControlPlane.create_plan` 从前自己生成 plan_id、
    不接受外部传入，于是**建 Plan 之前**发生的调用 —— Manager 规划前的知识检索、
    场景 5 的需求归一 —— 落的 `KbRetrieved` / `SkillInvoked` 只能挂空串。它们不是
    丢了的事件，是**认领不了**的事件：`scripts/verify.py` 第 4 项对 scenario-5 /
    scenario-6 / scenario-R5 各印一条「不在任何一棵树内」的 warn。
    docs/BACKLOG.md 的 `## task-X4` 第 2 条、`## task-W3` 第 2 条、`## task-X1`
    第 2 条、`## task-omega` 里 scenario_5 那条，记的是同一笔账。
    现在调用方先把 plan_id 生成好、规划期带着它跑，再原样传给 `create_plan`。

  · **场景 6 的检索恒空**（§2）。`seed_domain()` 只播租户 / 渠道 / 商品 / 订单 /
    政策，不播 `kb_doc`，库里没有任何可召回的知识 —— 接上检索只证明得了**链路通**，
    证明不了**召回准**（`## task-X1` 第 1 条）。而评委跑 `run.py` 看到的是场景 6，
    不是对照实验 R5。

§2 顺带把**跨租户硬约束**钉在真实数据上：W-1 语料原样带着 tnt-mfg-a / tnt-mfg-b
落库，一条都不许进本场景的候选集。这条比「库里有几条」重要得多 —— 召回一条别人家
的政策，这套系统就不能上生产（`kb/retriever.py:prefilter` 的原话）。

**为什么落文件库而不是 `:memory:`**：游离事件这个判据要从库里重放
（`obs/trace.export_trace_bundle(db_path)` 按路径开库），内存库拿不到路径。
断言落在「核验器真正消费的那个数」上，不落在测试自己另算的一个近似值上。
"""

from __future__ import annotations

import inspect
import json
import os
import sqlite3

import pytest

from maos import kb
from maos.agents.manager import ManagerAgent
from maos.contracts.events import new_id
from maos.core.control_plane import ControlPlane
from maos.flows import common as flows_common
from maos.flows import scenario_5 as s5
from maos.flows import scenario_6 as s6
from maos.kb import experiment
from maos.obs.trace import export_trace_bundle

PLANNING_EVENTS = ("KbRetrieved", "SkillInvoked")

_TASK = {"role": "coding", "title": "占位任务", "inputs": {}, "acceptance": [],
         "depends_on": [], "risk_level": "L"}


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
def _rows(db_path: str, sql: str, params: tuple = ()) -> list[dict]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def _run_on_disk(tmp_path, monkeypatch, capsys, module, name: str) -> str:
    """把一个场景跑在文件库上，返回库路径。

    换库的手法与 `kb/experiment.run_r5` / `scripts/make_evidence.py` 同款：在进程内
    把 `flows.common.SqliteStore` 换成绑定了路径的工厂。仓库里一个字节不改，
    **也不给 `build()` 加第二条装配路径**（C-3/C-4）。
    """
    monkeypatch.setenv(kb.KB_ENABLED_ENV, "1")
    db_path = str(tmp_path / f"{name}.db")
    original = flows_common.SqliteStore
    monkeypatch.setattr(flows_common, "SqliteStore",
                        lambda *a, **kw: original(db_path))
    rc = module.run()
    capsys.readouterr()                      # 场景的 stdout 不进测试报告
    assert rc == 0, f"{name} 没有跑到 exit=0"
    return db_path


@pytest.fixture
def s6_db(tmp_path, monkeypatch, capsys) -> str:
    return _run_on_disk(tmp_path, monkeypatch, capsys, s6, "scenario-6")


@pytest.fixture
def s5_db(tmp_path, monkeypatch, capsys) -> str:
    return _run_on_disk(tmp_path, monkeypatch, capsys, s5, "scenario-5")


def _kb_retrieved(db_path: str) -> dict:
    rows = _rows(db_path, "SELECT detail FROM event_log"
                          " WHERE event_type='KbRetrieved' ORDER BY seq")
    assert rows, "一条 KbRetrieved 都没有 —— 规划期检索没有真的发生"
    return json.loads(rows[0]["detail"])


# ---------------------------------------------------------------------------
# §1 create_plan 收得下预生成的 plan_id
# ---------------------------------------------------------------------------
def _control_plane() -> tuple[object, ControlPlane]:
    store, _bus, cp, _model, _worker, _gate = flows_common.build({})
    return store, cp


def test_create_plan_still_generates_its_own_id_by_default():
    """不传就照旧自己生成 —— 既有调用点的行为一个字节不变。"""
    store, cp = _control_plane()
    plan_id = cp.create_plan(goal="g", trace_id="tr", tasks=[dict(_TASK)])
    assert plan_id.startswith("plan_")
    assert store.get_plan(plan_id)["plan_id"] == plan_id


def test_create_plan_accepts_a_pregenerated_id():
    """传了就用它 —— Plan 与其下的任务全都挂在这个 id 上，不是只有 Plan 行。"""
    store, cp = _control_plane()
    wanted = new_id("plan")
    got = cp.create_plan(goal="g", trace_id="tr", tasks=[dict(_TASK)], plan_id=wanted)
    assert got == wanted
    assert store.get_plan(wanted)["plan_id"] == wanted
    tasks = store.list_tasks(wanted)
    assert tasks and all(t["plan_id"] == wanted for t in tasks)


def test_plan_id_is_an_optional_trailing_parameter():
    """签名守卫：既有三个参数一个没动，新参数排在最后且有缺省。

    这条守的是「顺手改了既有签名语义」——`create_plan` 有四个既有调用点
    （scenario_1/2/3/4/7 与一批测试），它们必须**一行不改**仍然跑通。
    """
    sig = inspect.signature(ControlPlane.create_plan)
    names = [n for n in sig.parameters if n != "self"]
    assert names == ["goal", "trace_id", "tasks", "plan_id"]
    plan_id = sig.parameters["plan_id"]
    assert plan_id.default is None, "plan_id 必须可缺省"
    assert plan_id.kind is inspect.Parameter.KEYWORD_ONLY, (
        "create_plan 全是关键字参数，新参数不许破例")


# ---------------------------------------------------------------------------
# §1（续）规划期的两条事件挂在它们真正属于的那棵树上
# ---------------------------------------------------------------------------
def test_scenario_6_planning_events_hang_on_the_real_plan(s6_db):
    """场景 6 的 KbRetrieved / SkillInvoked 全部挂在本次 Plan 上，无一空串。"""
    plans = _rows(s6_db, "SELECT plan_id FROM plan")
    assert len(plans) == 1
    plan_id = plans[0]["plan_id"]

    marks = ", ".join("?" for _ in PLANNING_EVENTS)
    events = _rows(s6_db, f"SELECT event_type, plan_id FROM event_log"
                          f" WHERE event_type IN ({marks})", PLANNING_EVENTS)
    assert events, "规划期一条事件都没落 —— 检索链路断了"
    orphans = [e for e in events if not (e["plan_id"] or "").strip()]
    assert not orphans, (
        f"{len(orphans)} 条规划期事件仍挂空串：{sorted({e['event_type'] for e in orphans})}。"
        " 调用方大概率没有先生成 plan_id 再传给 create_plan")
    assert all(e["plan_id"] == plan_id for e in events)


def test_scenario_6_leaves_no_stray_events(s6_db):
    """核验器第 4 项真正消费的那个数：游离事件 0 条。

    断言落在 `export_trace_bundle` 上而不是自己数一遍 event_log —— warn 必须是被
    接线消掉的，不是被另一套口径算没的。
    """
    bundle = export_trace_bundle(s6_db)
    assert bundle["summary"]["stray_event_count"] == 0, (
        f"场景 6 仍有游离事件：{bundle['stray_events']}")


def test_scenario_5_intake_event_hangs_on_the_real_plan(s5_db):
    """场景 5 的需求归一（issue.aggregate）同样跑在建 Plan 之前，同样要归树。"""
    plans = _rows(s5_db, "SELECT plan_id FROM plan")
    assert len(plans) == 1
    events = _rows(s5_db, "SELECT plan_id FROM event_log WHERE event_type='SkillInvoked'")
    assert events
    assert all(e["plan_id"] == plans[0]["plan_id"] for e in events)
    assert export_trace_bundle(s5_db)["summary"]["stray_event_count"] == 0


def test_manager_without_plan_id_in_context_still_logs_an_empty_one():
    """**不传** plan_id 的调用方行为不变：仍落空串，不抛。

    可选就要真可选。这条守的是「为了消 warn 把 plan_id 改成必填」——那会让
    场景 1/2/3/4/7 这些不带 context 的调用点当场炸。
    """
    store, _bus, _cp, model, _worker, _gate = flows_common.build(
        {"用户请求": json.dumps({"tasks": []}, ensure_ascii=False)})
    s6.seed_domain(store)
    mgr = ManagerAgent(model, store=store)
    mgr.plan(s6.GOAL, context={"tenant_id": s6.TENANT_ID, "biz_type": "refund"})

    rows = kb.query(store, "SELECT plan_id FROM event_log WHERE event_type='KbRetrieved'")
    assert rows, "没传 plan_id 就不检索了？检索与归属是两回事"
    assert rows[0]["plan_id"] == ""


# ---------------------------------------------------------------------------
# §2 场景 6 的知识库有内容，且跨租户的那一批一条都进不来
# ---------------------------------------------------------------------------
def _corpus_rules() -> list[dict]:
    payload = experiment.load_corpus(os.path.join("policy", "policy_rules.json"))
    return payload["policy_rule"]


def test_scenario_6_seeds_both_batches_of_knowledge(s6_db):
    """两批都在：W-1 语料 + 本场景自己的政策，且语料**没有被改写成本场景的租户**。"""
    by_tenant = {r["tenant_id"]: r["n"] for r in _rows(
        s6_db, "SELECT tenant_id, COUNT(1) AS n FROM kb_doc GROUP BY tenant_id")}

    assert by_tenant.get(s6.TENANT_ID) == len(s6.POLICY_RULES), (
        "本场景自己的政策没有全部投影进 kb_doc")
    others = {t: n for t, n in by_tenant.items() if t != s6.TENANT_ID}
    assert others, "W-1 语料没播进来 —— 检索的跨租户硬约束就没有真实数据可证"
    assert sum(others.values()) == len(_corpus_rules()), (
        f"语料被改写或塌行了：期望 {len(_corpus_rules())} 条分布在各自租户下，"
        f"实际 {others}。改写租户会让两个租户各自那条 AS-001 变成同一行")


def test_scenario_6_retrieval_actually_has_something_to_say(s6_db):
    """候选集与命中都非零，且命中的 doc_id 在 kb_doc 里查得到（核验器第 5 项同款判据）。"""
    detail = _kb_retrieved(s6_db)
    assert detail["candidate_count"] > 0, (
        "候选集又是 0 —— seed_domain 不播 kb_doc 的话，接上检索也只证明得了链路通")
    assert detail["hit_count"] > 0 and detail["docs"], "有候选却一条都没召回"
    for doc in detail["docs"]:
        found = _rows(s6_db, "SELECT 1 FROM kb_doc WHERE doc_id=?", (doc["doc_id"],))
        assert found, f"命中的 {doc['doc_id']} 在 kb_doc 里查不到 —— RAG 命中是编的"


def test_cross_tenant_knowledge_never_enters_the_candidate_set(s6_db):
    """库里有别家的政策，候选集里一条都不许有 —— 阶段一最硬的那一维。

    判据取候选集大小而不是命中集：命中集小可能只是打分低，候选集才是硬约束的直接
    量度。本场景的检索上下文不带 `policy_version`，所以本租户的政策**全部**是候选，
    候选集恰好等于本租户的条数 —— 多一条就说明有别家的漏进来了。
    """
    detail = _kb_retrieved(s6_db)
    assert detail["candidate_count"] == len(s6.POLICY_RULES), (
        f"候选集 {detail['candidate_count']} 条，本租户只有 {len(s6.POLICY_RULES)} 条 ——"
        " 跨租户的知识漏进候选集了")
    for doc in detail["docs"]:
        row = _rows(s6_db, "SELECT tenant_id FROM kb_doc WHERE doc_id=?", (doc["doc_id"],))
        assert row[0]["tenant_id"] == s6.TENANT_ID, (
            f"召回了别家租户的知识：{doc['doc_id']}")


def test_scenario_6_retrieval_query_carries_no_workflow_version(s6_db):
    """本场景这条路径**不带** `workflow_version` 维度 —— 类型分叉在这里不触发。

    `kb_doc.workflow_version` 声明成 INTEGER，而 W-1 语料里是 `"1.0.0"` 这样的字符串
    （docs/BACKLOG.md `## task-X3` 第 5 条）。SQLite 动态类型，照落不报错；真正会出事
    的是阶段一按 `workflow_version = ?` 严格相等比对的那一刻。本场景的检索上下文里
    没有这一维，投影落的也是 NULL，所以改不改列类型对这条路径**一样**——按派单口径
    不动它，账记在 `## task-Y2`。这条断言是那笔账的守卫：哪天有人给本场景的检索
    上下文加上这一维，它会红，提醒先把类型统一了再加。
    """
    detail = _kb_retrieved(s6_db)
    assert "workflow_version" not in detail["query"]
    versions = _rows(s6_db, "SELECT DISTINCT workflow_version AS v FROM kb_doc")
    assert all(r["v"] is None for r in versions), (
        "kb_doc 里出现了非 NULL 的 workflow_version —— 类型分叉从这一刻起会真的咬人")
