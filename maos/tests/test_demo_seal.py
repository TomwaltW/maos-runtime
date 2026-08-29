"""演示链路收口的守卫测试（X-1）。

两件事，各自守一条**已经被踩过一次**的缺口：

  · **缺省序列漏场景**（§1）。`DEFAULT_SCENARIOS` 与「已落地的场景」漂开过两次：
    先是 `(1,2,3,4)` 漏了 5/6，改完之后 `scenario_7.py` 落地又漏了 7。两次的症状
    完全一样 —— `python3 run.py` 少跑一场，**不报错、不提示**，演示现场没人看得出
    评委漏看了什么。所以这里不断言「等于某个字面元组」（那样下次加场景照样漂），
    而是断言 `DEFAULT_SCENARIOS` == 「`ALL_SCENARIOS` 里模块真的存在的那些」——
    把 main.py 自己写的那条口径（排除标准是「模块不存在」，不是「谁负责」）
    变成机器判定。下一场景落地后忘了进缺省序列，这条会红。

  · **接了线却没真检索**（§2）。场景 6 曾用 `ManagerAgent(model)` 老写法构造，
    `SkillInvoker.store is None` 让规划期检索恒返回空 —— `MAOS_KB_ENABLED` 开关
    对它没有任何影响，两态输出逐字节相同。这种失效**在 stdout 上看不出来**
    （DAG 本来就不该变），只能查 event_log。所以断言落在 `KbRetrieved` 条数上，
    并配一条反向断言：关掉开关必须一条都不落，否则「有无 RAG」这条对照线不干净。

§2 顺带钉死一条**边界**：开关不许改变 DAG。场景 6 的 PLAN_JSON 由
`ScriptedModelClient` 写死且已含 finance，接上检索本就不会改 DAG；哪天有人为了
「让 RAG 看起来有效果」去动那份脚本，这条会红，而不是让它悄悄改掉演示的基准。
"""

from __future__ import annotations

import importlib
import importlib.util

import pytest

from maos import kb
from maos import main as maos_main
from maos.flows import scenario_6 as s6
from maos.skills.builtin.refund import _common as C

# --------------------------------------------------------------------------
# §1 缺省序列 = 全部已落地的场景
# --------------------------------------------------------------------------


def _landed_scenarios() -> tuple[int, ...]:
    """`ALL_SCENARIOS` 里流程模块真的存在的那些。用 find_spec 而不是 import：
    只问「模块在不在」，不执行它，免得把场景的副作用带进测试进程。"""
    return tuple(
        n for n in maos_main.ALL_SCENARIOS
        if importlib.util.find_spec(f"maos.flows.scenario_{n}") is not None
    )


def test_default_sequence_equals_landed_scenarios():
    """缺省序列必须等于「已落地的场景」—— 这是 main.py 自己写的排除口径。"""
    assert maos_main.DEFAULT_SCENARIOS == _landed_scenarios(), (
        "DEFAULT_SCENARIOS 与已落地场景漂开了：\n"
        f"  缺省序列   = {maos_main.DEFAULT_SCENARIOS}\n"
        f"  已落地场景 = {_landed_scenarios()}\n"
        "排除标准是「模块不存在」，不是「谁负责」——"
        "新场景落地就该自动进来，漏了的话 run.py 会少跑一场且不报错。")


def test_scenario_7_is_in_default_sequence():
    """场景 7 单独点名：它是 Demo 分镜主线（本轮唯一一条失败路径）。"""
    assert 7 in maos_main.DEFAULT_SCENARIOS, (
        "场景 7 是唯一一条失败路径（网关问不出终态 -> 驳回 -> 补偿 -> 从未进 settled），"
        "不进缺省序列的话 `python3 run.py` 演示时它根本不会跑")


def test_every_default_scenario_is_runnable():
    """缺省序列里的每一场都必须真能跑 —— import 得到且有 run(matrix=...)。"""
    for n in maos_main.DEFAULT_SCENARIOS:
        mod = importlib.import_module(f"maos.flows.scenario_{n}")
        assert callable(getattr(mod, "run", None)), f"场景 {n} 没有可调用的 run()"


def test_entry_docstrings_carry_no_stale_unlanded_claim():
    """入口文档不许再声称场景 7 未落地 —— 它已经落地并进了缺省序列。"""
    import run as run_entry

    for name, doc in (("maos/main.py", maos_main.__doc__),
                      ("run.py", run_entry.__doc__)):
        assert doc, f"{name} 丢了模块 docstring"
        for stale in ("未落地", "ModuleNotFoundError"):
            assert stale not in doc, (
                f"{name} 的 docstring 仍写着「{stale}」，但场景 7 已落地并进缺省序列")


# --------------------------------------------------------------------------
# §2 场景 6 的规划期检索真的发生
# --------------------------------------------------------------------------

_KB_EVENT = "KbRetrieved"


def _run_scenario_6(monkeypatch, capsys, *, kb_switch: str | None):
    """跑一趟场景 6，返回 (rc, store, manager_kwargs)。

    store 是 `:memory:` 的，跑完即消 —— 只能在进程内包一层 `build()` 截住同一个
    引用。顺带截 `ManagerAgent` 的构造参数：检索恒空的根因就在这里，
    查一条 store 是不是真传进去了，比事后猜为什么没命中直接得多。
    """
    if kb_switch is None:
        monkeypatch.delenv(kb.KB_ENABLED_ENV, raising=False)
    else:
        monkeypatch.setenv(kb.KB_ENABLED_ENV, kb_switch)

    seen: dict = {}
    orig_build, orig_mgr = s6.build, s6.ManagerAgent

    def spy_build(*args, **kwargs):
        parts = orig_build(*args, **kwargs)
        seen["store"] = parts[0]
        return parts

    def spy_manager(*args, **kwargs):
        seen["manager_kwargs"] = kwargs
        return orig_mgr(*args, **kwargs)

    monkeypatch.setattr(s6, "build", spy_build)
    monkeypatch.setattr(s6, "ManagerAgent", spy_manager)

    rc = s6.run()
    capsys.readouterr()                     # 场景输出很长，不让它进测试报告
    return rc, seen["store"], seen.get("manager_kwargs", {})


def _kb_events(store) -> list[dict]:
    rows = store._conn.execute(
        "SELECT event_type, plan_id, trace_id, detail FROM event_log"
        " WHERE event_type=? ORDER BY seq", (_KB_EVENT,)).fetchall()
    return [dict(r) for r in rows]


def _task_titles(store) -> list[str]:
    rows = store._conn.execute("SELECT title FROM task ORDER BY task_id").fetchall()
    return [r[0] for r in rows]


def test_manager_is_constructed_with_store(monkeypatch, capsys):
    """Manager 必须带 store 构造，否则 SkillInvoker 拿不到库，检索恒空。"""
    rc, store, mgr_kwargs = _run_scenario_6(monkeypatch, capsys, kb_switch="1")
    assert rc == 0
    assert mgr_kwargs.get("store") is not None, (
        "场景 6 的 ManagerAgent 又退回 `cls(model)` 老写法了 —— "
        "SkillInvoker.store is None，规划期检索会恒返回空且不报错")
    assert mgr_kwargs["store"] is store, "Manager 拿到的必须是本场景那一个 store"


def test_kb_retrieved_is_logged_when_enabled(monkeypatch, capsys):
    """开关打开：event_log 里必须留下 KbRetrieved，且带上规划期真知道的检索维度。"""
    rc, store, _ = _run_scenario_6(monkeypatch, capsys, kb_switch="1")
    assert rc == 0

    events = _kb_events(store)
    assert len(events) >= 1, (
        "MAOS_KB_ENABLED=1 却一条 KbRetrieved 都没有 —— 规划期检索没有真的发生")

    import json
    query = json.loads(events[0]["detail"])["query"]
    assert query.get("tenant_id") == s6.TENANT_ID, "没有 tenant_id 就没有候选集（硬约束）"
    assert query.get("biz_type") == C.BIZ_TYPE
    assert query.get("channel_id") == s6.CHANNEL_ID
    assert query.get("sku") == s6.SKU
    # 政策版本与命中规则是 policy.match 之后才裁出来的，规划期不该知道。
    assert "policy_version" not in query and "rule_no" not in query, (
        "检索上下文里出现了规划期还不该知道的事实（政策版本 / 命中规则）")


def test_no_kb_event_when_disabled(monkeypatch, capsys):
    """开关关掉：一条 KbRetrieved 都不许落 —— 「有无 RAG」的对照线必须干净。"""
    rc, store, _ = _run_scenario_6(monkeypatch, capsys, kb_switch="0")
    assert rc == 0
    assert _kb_events(store) == [], (
        "MAOS_KB_ENABLED=0 时仍落了 KbRetrieved，关掉就该干净地什么都没有")


def test_kb_retrieval_happens_without_any_env_var(monkeypatch, capsys):
    """不设环境变量也要检索 —— 演示现场跑 `python3 run.py` 不会去配 env。"""
    rc, store, _ = _run_scenario_6(monkeypatch, capsys, kb_switch=None)
    assert rc == 0
    assert len(_kb_events(store)) >= 1, (
        "缺省未设 MAOS_KB_ENABLED 时没有检索 —— kb_enabled() 缺省是启用的，"
        "演示时不会有人去 export 一个环境变量")


def test_kb_switch_does_not_change_the_dag(monkeypatch, capsys):
    """开关不许改变 DAG。

    场景 6 的 PLAN_JSON 由 ScriptedModelClient 写死且已含 finance，接上检索本就
    不会改 DAG —— 这条守的是「别为了让 RAG 看起来有效果去动那份脚本」。
    真要做 DAG 层面的 RAG 对照，出口在 `kb/experiment.py`（R5），不在演示主线上。
    """
    rc_on, store_on, _ = _run_scenario_6(monkeypatch, capsys, kb_switch="1")
    rc_off, store_off, _ = _run_scenario_6(monkeypatch, capsys, kb_switch="0")
    assert rc_on == rc_off == 0
    assert _task_titles(store_on) == _task_titles(store_off), (
        "MAOS_KB_ENABLED 改变了场景 6 的 DAG —— 演示基准会随开关漂")


@pytest.mark.parametrize("switch", ["1", "0"])
def test_scenario_6_still_settles(monkeypatch, capsys, switch):
    """两种开关下场景 6 的收口断言都必须照旧成立（顺利路径，终值 settled）。"""
    rc, store, _ = _run_scenario_6(monkeypatch, capsys, kb_switch=switch)
    assert rc == 0
    row = store._conn.execute(
        "SELECT biz_status FROM refund_case WHERE tenant_id=? AND case_id=?",
        (s6.TENANT_ID, s6.CASE_ID)).fetchone()
    assert row is not None and row[0] == "settled", (
        f"MAOS_KB_ENABLED={switch} 下顺利路径没有收敛到 settled")
