"""每个 Agent 各连各的模型 —— 按角色注入模型客户端 + 路由留痕（T57）。

改之前：``runtime/worker.py`` 一个 ``model`` 变量喂给 22 个 Agent 构造器，
一个 Worker 里 22 个角色共用同一个 ``ModelClient`` 实例。「Manager 走强推理、
批量分类走轻量模型」这句话在代码里根本不成立。

本文件守四件事，每一件都是会被悄悄做坏的：

1. **缺省零漂移**（``test_default_*``）—— 不传 ``model_factory`` 时 22 个 Agent 仍共用
   同一个实例。这是本改动能安全并入的唯一前提：路由是新增能力，不是行为变更。

2. **``estimated`` 不许翻面**（``test_estimated_*``）—— 全文件最重要的一组。
   ``core/store.py::usage_is_estimated`` 判的是**客户端类型**
   （``isinstance(client, ScriptedModelClient)``），而 ``BaseAgent.ask()`` 传给它的
   正是 Agent 手里那个客户端。无脑包一层，``isinstance`` 当场变 False，
   ``model_usage.estimated`` 从 1 翻成 0 —— ``len(user) // 4`` 算出来的假 token
   被记成「网关回的真实计费」。屏幕上一点都看不出来。

3. **降级不崩**（``test_factory_*``）—— 工厂返 None 或自己抛异常，都回落到缺省客户端
   并留声，口径同 ``model/client.py::select_model_client``：缺配置就降级，不崩。

4. **留痕有、且不夹带凭据**（``test_model_routed_*``）—— 每个被接管的 role 一条
   ``ModelRouted``（不是每次调用一条），detail 里只有名字，一个配置值都没有（铁律 6）。

外加一条 C-3 回归：新参数必须是 keyword-only，位置传参照旧抛 TypeError。
"""

from __future__ import annotations

import json

import pytest

from maos.agents.base import AGENT_POOL
from maos.core.store import usage_is_estimated
from maos.flows.common import build
from maos.model.client import ModelClient, ModelResponse, ScriptedModelClient
from maos.model.routed import RoutedModelClient, route_model_client
from maos.runtime.worker import EVENT_MODEL_ROUTED

#: 留痕行落在 plan_id 空串下 —— Worker 构造在任何 plan 之前，此刻确实没有归属。
NO_PLAN = ""

#: 假 key 的哨兵值。任何一处留痕里出现它，就是铁律 6 破了。
SECRET = "sk-must-never-be-logged-0123456789"


class StubProviderClient(ModelClient):
    """假装是个真 provider 客户端：**不是** ScriptedModelClient，所以会被包装。

    刻意带上 ``base_url`` 与 ``_api_key`` 两个「不该出现在留痕里」的属性 ——
    留痕的脱敏判据要有东西可判，不能拿一个干净对象自证清白。
    """

    def __init__(self, name: str, model: str = "stub-model") -> None:
        self.provider = name
        self.model = model
        self.base_url = f"https://gateway.example/v1?token={SECRET}"
        self._api_key = SECRET
        self.calls: list[dict] = []

    def complete(self, *, system: str, user: str, tier: str) -> ModelResponse:
        self.calls.append({"system": system, "user": user, "tier": tier})
        return ModelResponse(text="{}", tokens_in=7, tokens_out=3, model=self.model,
                             meta={"kept": "from-inner"})


def scripted_factory():
    """按 role 各造一个 ScriptedModelClient，并把每次调用的 (role, tier) 记下来。"""
    made: dict[str, ScriptedModelClient] = {}
    seen: list[tuple[str, str]] = []

    def factory(role: str, tier: str) -> ModelClient:
        seen.append((role, tier))
        made[role] = ScriptedModelClient({"ping": "pong"})
        return made[role]

    return factory, made, seen


# --- 1. 缺省零漂移 ---------------------------------------------------------
def test_default_keeps_one_shared_client_for_all_agents():
    """不传 model_factory：22 个 Agent 共用同一个实例，与加参数之前逐字节相同。"""
    _, _, _, model, worker, _ = build({})
    assert len(worker.agents) == len(AGENT_POOL)
    assert len({id(a.model) for a in worker.agents.values()}) == 1
    assert all(a.model is model for a in worker.agents.values())


def test_default_writes_no_routing_rows():
    """没有路由就没有留痕 —— 缺省路径一行 ModelRouted 都不许多出来。"""
    store, _, _, _, _, _ = build({})
    assert _routed_rows(store) == []


# --- 2. 按角色分流 ---------------------------------------------------------
def test_factory_gives_each_role_its_own_client():
    factory, made, _ = scripted_factory()
    _, _, _, default_model, worker, _ = build({}, model_factory=factory)

    ids = {id(a.model) for a in worker.agents.values()}
    assert len(ids) == len(AGENT_POOL), "每个 role 该拿到工厂为它造的那一个"
    assert worker.agents["coding"].model is made["coding"]
    assert all(a.model is not default_model for a in worker.agents.values())


def test_factory_receives_the_role_and_its_declared_tier():
    """第二个入参必须是该角色 Identity 上声明的 tier，不是随便一个常量。"""
    factory, _, seen = scripted_factory()
    build({}, model_factory=factory)

    assert dict(seen) == {role: cls.identity.model_tier for role, cls in AGENT_POOL.items()}
    tiers = [tier for _, tier in seen]
    assert len(seen) == len(AGENT_POOL)
    assert {t: tiers.count(t) for t in set(tiers)} == {"light": 19, "medium": 2, "strong": 3}


# --- 3. 降级不崩 -----------------------------------------------------------
def test_factory_returning_none_falls_back_to_the_default_client():
    """缺配置就降级，不崩（口径同 select_model_client）。"""
    def factory(role: str, tier: str):
        return ScriptedModelClient({}) if role == "coding" else None

    _, _, _, default_model, worker, _ = build({}, model_factory=factory)
    fell_back = [r for r, a in worker.agents.items() if a.model is default_model]
    assert set(fell_back) == set(AGENT_POOL) - {"coding"}
    assert worker.agents["coding"].model is not default_model


def test_factory_raising_falls_back_and_leaves_a_warning(caplog):
    """工厂自己炸了也不该让 Worker 起不来 —— 但必须留声，不许静默降级。"""
    def factory(role: str, tier: str):
        raise RuntimeError("路由表没配这个角色")

    with caplog.at_level("WARNING", logger="maos.worker"):
        _, _, _, default_model, worker, _ = build({}, model_factory=factory)

    assert all(a.model is default_model for a in worker.agents.values())
    assert any("回落到缺省客户端" in r.getMessage() for r in caplog.records)


def test_factory_failure_leaves_no_routing_row():
    """没接管成功就不许留痕 —— 留痕行数是「真被接管了几个 role」的唯一读数。"""
    def factory(role: str, tier: str):
        return None

    store, _, _, _, _, _ = build({}, model_factory=factory)
    assert _routed_rows(store) == []


# --- 4. estimated 不许翻面（本文件最重要的一组）-----------------------------
def test_estimated_stays_true_when_routing_scripted_clients():
    """路由之后跑一次真调用，model_usage 那行仍须是 estimated=1。

    走的是完整链路：build -> worker.agents[...] -> BaseAgent.ask -> record_model_usage，
    不是直接调 usage_is_estimated —— 要守的是「Agent 手里那个对象」的类型没被换掉。
    """
    factory, _, _ = scripted_factory()
    store, _, _, _, worker, _ = build({}, model_factory=factory)

    worker.agents["coding"].ask("你是编码 Agent", "ping 一下")
    rows = store.list_model_usage()
    assert len(rows) == 1
    assert rows[0]["estimated"] == 1, "脚本回放的 len(user)//4 假 token 不许被记成真实计费"
    assert rows[0]["agent_role"] == "coding"


def test_route_model_client_does_not_wrap_estimated_clients():
    """判据借的是 usage_is_estimated 本身，所以包装不可能改变它的答案。"""
    scripted = ScriptedModelClient({})
    assert route_model_client(scripted, role="coding") is scripted

    real = StubProviderClient("stub")
    routed = route_model_client(real, role="coding")
    assert isinstance(routed, RoutedModelClient) and routed.inner is real
    assert usage_is_estimated(routed) is False   # 真客户端本来就该是 False


def test_a_naive_wrap_would_have_flipped_estimated():
    """回归本条的**病症**：无脑包一层，estimated 当场翻面。

    这条不测新代码，测的是「这个坑真的存在」—— 没有它，上面几条绿了也说不清
    躲开的是什么。
    """
    scripted = ScriptedModelClient({})
    assert usage_is_estimated(scripted) is True
    naive = RoutedModelClient(scripted, role="coding", provider="p", route_source="s")
    assert usage_is_estimated(naive) is False


def test_wrapper_forwards_model_name_for_the_failure_ledger():
    """``ask()`` 失败分支取的是 getattr(self.model, "model", "")，包装后不许变空。"""
    routed = route_model_client(StubProviderClient("stub", model="claude-x"), role="coding")
    assert getattr(routed, "model", "") == "claude-x"


def test_wrapper_does_not_forward_private_attributes():
    """下划线属性不转发 —— 顺手守住铁律 6：包装器不该把 _api_key 漏出来。"""
    routed = route_model_client(StubProviderClient("stub"), role="coding")
    with pytest.raises(AttributeError):
        routed._api_key                                  # noqa: B018


def test_wrapper_puts_attribution_in_meta_without_touching_tokens():
    inner = StubProviderClient("stub", model="claude-x")
    routed = route_model_client(inner, role="reviewer", route_source="routing-table")
    resp = routed.complete(system="s", user="u", tier="strong")

    assert (resp.text, resp.tokens_in, resp.tokens_out, resp.model) == ("{}", 7, 3, "claude-x")
    assert resp.meta["kept"] == "from-inner", "inner 自己写的 meta 不许被抹掉"
    assert resp.meta["role"] == "reviewer"
    assert resp.meta["provider"] == "stub"
    assert resp.meta["route_source"] == "routing-table"
    assert inner.calls[0]["tier"] == "strong", "tier 必须原样透传给 inner"


# --- 5. 留痕 ---------------------------------------------------------------
def test_model_routed_is_one_row_per_role_not_per_call():
    """每个被接管的 role 一条。按调用落会把 event_log 冲爆，而归属是不变量。"""
    factory, _, _ = scripted_factory()
    store, _, _, _, worker, _ = build({}, model_factory=factory)

    rows = _routed_rows(store)
    assert len(rows) == len(AGENT_POOL)
    assert {r["detail"]["role"] for r in rows} == set(AGENT_POOL)

    worker.agents["coding"].ask("s", "ping")
    worker.agents["coding"].ask("s", "ping")
    assert len(_routed_rows(store)) == len(AGENT_POOL), "调用了两次，留痕行数不许变"


def test_model_routed_detail_carries_role_provider_model_tier_and_source():
    def factory(role: str, tier: str):
        return StubProviderClient("anthropic-stub", model="claude-x") if role == "coding" else None

    store, _, _, _, _, _ = build({}, model_factory=factory)
    rows = _routed_rows(store)
    assert len(rows) == 1

    detail = rows[0]["detail"]
    assert detail["role"] == "coding"
    assert detail["provider"] == "anthropic-stub"
    assert detail["model"] == "claude-x"
    assert detail["tier"] == AGENT_POOL["coding"].identity.model_tier
    assert detail["route_source"]                     # 说得出来路，不许空着
    assert rows[0]["plan_id"] == NO_PLAN, "构造期确实没有 plan，如实记空串"


def test_model_routed_detail_never_carries_credentials():
    """铁律 6：detail 里只许有名字。base_url 也不进来 —— 它可能带 query 串的凭据。"""
    def factory(role: str, tier: str):
        return StubProviderClient("anthropic-stub")

    store, _, _, _, _, _ = build({}, model_factory=factory)
    dumped = json.dumps([r["detail"] for r in _routed_rows(store)], ensure_ascii=False)
    assert SECRET not in dumped
    assert "base_url" not in dumped and "gateway.example" not in dumped


# --- 6. C-3 回归 -----------------------------------------------------------
def test_build_model_factory_is_keyword_only():
    """新参数照 C-3 走 keyword-only：位置传参必须照旧抛 TypeError。"""
    with pytest.raises(TypeError):
        build({}, True)
    with pytest.raises(TypeError):
        build({}, True, None, None)


def test_build_still_returns_the_frozen_six_tuple():
    """C-4：多了个入参也不许变七元组或 dataclass。"""
    factory, _, _ = scripted_factory()
    assert len(build({}, model_factory=factory)) == 6


def _routed_rows(store) -> list[dict]:
    return [r for r in store.list_event_log(NO_PLAN) if r["event_type"] == EVENT_MODEL_ROUTED]
