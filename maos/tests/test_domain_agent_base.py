"""Agent 薄壳骨架下沉后的守卫 —— 三个域共用一份 `maos/agents/_domain_base.py`。

应付账款 / 理赔 / 差错处理三个域的 `agents/<域>/_base.py` 原本各持一份同构的
`extras_of` / `artifact` / `failed`。下沉到一处之后，这里钉住四件容易在结构性改动中
悄悄退化的事：

1. `invocation_id` 每次新生成（复用会让审计链变假）；
2. `artifact()` 恒补齐 Gate 要的两个字段（缺了症状显示成「某某流程走不完」，离原因极远）；
3. 三个域的 kind **不进** `maos/artifacts.py` 的 `ALL_KINDS`（进了闸会恒 blocker）；
4. `_domain_base` 不被 `maos/agents/__init__.py` 当成 Agent 模块扫进 `AGENT_POOL`。

外加一条回归守卫：下沉前后行为逐字段一致。期望值**写死在本文件里**，不 import 旧
实现 —— 拿新实现当自己的期望值，这条测试就什么也没守住。
"""

from __future__ import annotations

import types

from maos.agents import AGENT_POOL
from maos.agents import _domain_base
from maos.agents.ap import _base as ap_base
from maos.agents.claim import _base as claim_base
from maos.agents.investigation import _base as investigation_base
from maos.artifacts import ALL_KINDS

#: 下沉前三份实现在 `AGENT_POOL` 里的 role 条数。骨架是共用件不是 Agent，
#: 改完必须还是这个数。
EXPECTED_AGENT_POOL_SIZE = 24   # 整合 T55-T83 时刷：主干已多出 refund_evidence / refund_risk

DOMAIN_BASES = (
    ("ap", ap_base, ap_base.ALL_AP_KINDS),
    ("claim", claim_base, claim_base.ALL_CLAIM_KINDS),
    ("investigation", investigation_base, investigation_base.ALL_INVESTIGATION_KINDS),
)


def _fake_agent() -> types.SimpleNamespace:
    """一个只满足 `extras_of` 取值口径的桩：`.model` 与 `.identity.model_tier`。"""
    return types.SimpleNamespace(
        model="deepseek-chat",
        identity=types.SimpleNamespace(model_tier="standard"),
    )


def _fake_ctx() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        plan_id="plan-1", task_id="task-1", trace_id="trace-1", attempt=2,
    )


def test_each_call_gets_a_fresh_invocation_id() -> None:
    """连调两次拿到两个不同的 invocation_id。

    复用同一个 id 会让两次不同的写入指向同一次调用，审计链就假了 —— 所以不缓存。
    """
    agent, ctx = _fake_agent(), _fake_ctx()
    first = _domain_base.extras_of(agent, ctx)["invocation_id"]
    second = _domain_base.extras_of(agent, ctx)["invocation_id"]
    assert first and second, "invocation_id 不许为空：guard.update_biz_status 要非空 actor 锚点"
    assert first != second, "两次调用必须拿到不同的 invocation_id，否则审计链对不上号"


def test_artifact_always_carries_gate_required_fields() -> None:
    """Gate 对非代码类产物的两条硬判据：summary 非空、self_check.build/lint == pass。

    缺哪一条都是 rework，而症状会显示成「某某流程走不完」，离原因极远。
    """
    got = _domain_base.artifact("ap_match_result", {"po_no": "PO-1"}, summary="三单匹配通过")
    body = got["content"]
    assert body["summary"] == "三单匹配通过"
    assert body["self_check"] == {"build": "pass", "lint": "pass"}
    assert body["po_no"] == "PO-1", "原有 content 不许被覆盖，只做补齐"


def test_artifact_does_not_mutate_caller_content() -> None:
    """补齐两个字段时不许就地改调用方传进来的 dict（复用同一份 content 会串味）。"""
    content = {"po_no": "PO-1"}
    _domain_base.artifact("ap_match_result", content, summary="三单匹配通过")
    assert content == {"po_no": "PO-1"}


def test_caller_supplied_self_check_is_not_overwritten() -> None:
    """`self_check` 是 setdefault 不是赋值：调用方给了自己的判据就以它为准。"""
    got = _domain_base.artifact(
        "ap_match_result", {"self_check": {"build": "fail", "lint": "pass"}},
        summary="匹配失败",
    )
    assert got["content"]["self_check"] == {"build": "fail", "lint": "pass"}


def test_domain_kinds_are_not_registered_in_all_kinds() -> None:
    """三个域的 kind 一个都不许进 `maos/artifacts.py` 的 ALL_KINDS。

    进去了不只是「口径变宽」：Gate 用产物类型判「这是不是代码类任务」，一旦这些 kind
    与 patch_set / test_report 同表，本域任务会被要求交一份跑出来的测试报告 ——
    闸恒 blocker，且报错指向测试而不是本域业务。谁哪天顺手注册进去，这条立刻红。
    """
    for name, _mod, kinds in DOMAIN_BASES:
        for kind in kinds:
            assert kind not in ALL_KINDS, f"{name} 域的 {kind} 不许进 artifacts.ALL_KINDS"


def test_domain_base_module_is_not_scanned_as_an_agent() -> None:
    """骨架模块不许被 `maos/agents/__init__.py` 扫成 Agent 模块。

    那份扫描 import 包内**全部非下划线开头模块**，带 @register 的类随之进 AGENT_POOL。
    文件名一旦不带下划线，本模块就会被当成 Agent 模块 —— 所以这里同时钉住文件名与条数。
    """
    assert _domain_base.__name__.rsplit(".", 1)[-1].startswith("_"), \
        "骨架模块名必须以下划线开头，否则会被 agents 包的自动扫描 import 成 Agent 模块"
    assert len(AGENT_POOL) == EXPECTED_AGENT_POOL_SIZE
    for role, cls in AGENT_POOL.items():
        assert cls.__module__ != _domain_base.__name__, f"{role} 不该来自骨架模块"


def test_lowering_did_not_change_behavior() -> None:
    """回归守卫：三个域各调一次 artifact() 与 extras_of()，逐字段比对下沉前的期望值。

    期望值写死在这里，不 import 旧实现 —— 拿新实现当期望值等于什么都没守。
    """
    agent, ctx = _fake_agent(), _fake_ctx()
    samples = {
        "ap": (ap_base, ap_base.KIND_MATCH_RESULT, {"po_no": "PO-1"}, "三单匹配通过"),
        "claim": (claim_base, claim_base.KIND_ADJUDICATION, {"decision": "approve"}, "核定通过"),
        "investigation": (
            investigation_base, investigation_base.KIND_CASE_FILE,
            {"case_no": "C-1"}, "立案完成",
        ),
    }
    for domain, (mod, kind, content, summary) in samples.items():
        got = mod.artifact(kind, content, summary=summary)
        assert got == {
            "kind": kind,
            "content": {**content, "summary": summary,
                        "self_check": {"build": "pass", "lint": "pass"}},
        }, f"{domain} 域 artifact 形状变了"

        extras = mod.extras_of(agent, ctx)
        assert extras["model"] == "deepseek-chat"
        assert extras["tier"] == "standard"
        assert extras["plan_id"] == "plan-1"
        assert extras["task_id"] == "task-1"
        assert extras["trace_id"] == "trace-1"
        assert extras["attempt"] == 2
        assert isinstance(extras["invocation_id"], str) and extras["invocation_id"]
        assert set(extras) == {
            "model", "tier", "plan_id", "task_id", "trace_id", "attempt", "invocation_id",
        }, f"{domain} 域 extras 键集合变了"

        failure = types.SimpleNamespace(error=None)
        assert mod.failed(failure, "claim.pay") == "claim.pay 未产出结果"
        assert mod.failed(types.SimpleNamespace(error="boom"), "claim.pay") == "boom"


def test_three_domains_share_one_implementation() -> None:
    """三个域转出的是同一个函数对象 —— 不是各自又抄了一份。"""
    for fn_name in ("artifact", "extras_of", "failed"):
        impls = {getattr(mod, fn_name) for _n, mod, _k in DOMAIN_BASES}
        assert impls == {getattr(_domain_base, fn_name)}, f"{fn_name} 没有真的合并成一份"
