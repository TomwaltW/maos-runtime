"""角色 → 模型路由表（T56）。

**一行网络都不打**：本文件只验解析与校验，不构造任何客户端。环境一律由
``monkeypatch.setenv`` 造 —— 读进程真实 env 的测试会随「跑测试这台机器上恰好
export 了什么」变红变绿，那是 conftest.py 花大力气堵掉的那类洞（H-6），不能
在新文件里重新开一个。本模块的 :func:`_no_ambient_llm_env` 补的正是 conftest
没覆盖到的 ``MAOS_LLM_*`` 这一族。

**假 key 只有一个**：第 4 组用例里那个 ``sk-`` 串是**故意构造的假值**，用来验闸门
会拒绝它、且异常信息不会把它回显出去。除它之外本文件不许出现第二个 key 形态的串
（铁律 6）。
"""
import dataclasses
import json

import pytest

from maos.agents.base import AGENT_POOL
from maos.model.client import ENV_API_KEY, ENV_BASE_URL, ENV_MODEL, Tier
from maos.model.routing import (
    DEFAULT_PROVIDER,
    ENV_ROUTES,
    SOURCE_GLOBAL,
    SOURCE_ROLE_ENV,
    SOURCE_ROUTES_JSON,
    SOURCE_TIER_ENV,
    SOURCE_UNRESOLVED,
    RouteConfigError,
    RouteSpec,
    describe,
    load_routes,
    render,
    resolve,
)

#: 故意构造的**假** key，只在第 4 组用例里出现。它不是任何真实凭证。
FAKE_KEY_VALUE = "sk-FAKE0000000000000000000000000000000000"


@pytest.fixture(autouse=True)
def _no_ambient_llm_env(monkeypatch):
    """删掉进程里所有 ``MAOS_LLM_*``，让每条用例从「三级全空」起跑。

    按前缀扫而不是写死名单：角色级 / tier 级的变量名是按 role、tier 拼出来的，
    写死的名单一旦漏一个，漏的那条正好是「本机配了、CI 没配」的那条。
    """
    import os

    for name in [n for n in os.environ if n.startswith("MAOS_LLM_")]:
        monkeypatch.delenv(name, raising=False)


def _set_role(monkeypatch, role_upper, model, *, base_url=True, api_key=True, provider=None):
    monkeypatch.setenv(f"MAOS_LLM_ROLE_{role_upper}_MODEL", model)
    if base_url:
        monkeypatch.setenv(f"MAOS_LLM_ROLE_{role_upper}_BASE_URL", "https://role.invalid")
    if api_key:
        monkeypatch.setenv(f"MAOS_LLM_ROLE_{role_upper}_API_KEY", "role-token")
    if provider:
        monkeypatch.setenv(f"MAOS_LLM_ROLE_{role_upper}_PROVIDER", provider)


def _set_tier(monkeypatch, tier_upper, model, *, provider=None):
    monkeypatch.setenv(f"MAOS_LLM_TIER_{tier_upper}_MODEL", model)
    monkeypatch.setenv(f"MAOS_LLM_TIER_{tier_upper}_BASE_URL", "https://tier.invalid")
    monkeypatch.setenv(f"MAOS_LLM_TIER_{tier_upper}_API_KEY", "tier-token")
    if provider:
        monkeypatch.setenv(f"MAOS_LLM_TIER_{tier_upper}_PROVIDER", provider)


def _set_global(monkeypatch, model="global-model"):
    monkeypatch.setenv(ENV_MODEL, model)
    monkeypatch.setenv(ENV_BASE_URL, "https://global.invalid")
    monkeypatch.setenv(ENV_API_KEY, "global-token")


# ---------------------------------------------------------------- 1. 三级各命中一次


def test_role_env_hit(monkeypatch):
    _set_role(monkeypatch, "CODING", "role-model", provider="anthropic")
    spec = resolve("coding", Tier.MEDIUM)
    assert spec is not None
    assert (spec.source, spec.model, spec.provider) == (SOURCE_ROLE_ENV, "role-model", "anthropic")
    assert spec.base_url_env == "MAOS_LLM_ROLE_CODING_BASE_URL"
    assert spec.api_key_env == "MAOS_LLM_ROLE_CODING_API_KEY"


def test_tier_env_hit(monkeypatch):
    _set_tier(monkeypatch, "STRONG", "tier-model")
    spec = resolve("reviewer", Tier.STRONG)
    assert spec is not None
    assert (spec.source, spec.model) == (SOURCE_TIER_ENV, "tier-model")
    assert spec.api_key_env == "MAOS_LLM_TIER_STRONG_API_KEY"


def test_global_fallback_hit(monkeypatch):
    _set_global(monkeypatch)
    spec = resolve("ap_intake", Tier.LIGHT)
    assert spec is not None
    assert (spec.source, spec.model) == (SOURCE_GLOBAL, "global-model")
    assert (spec.base_url_env, spec.api_key_env) == (ENV_BASE_URL, ENV_API_KEY)
    assert spec.provider == DEFAULT_PROVIDER


def test_routes_json_hit(monkeypatch):
    monkeypatch.setenv("MY_GATEWAY_BASE_URL", "https://json.invalid")
    monkeypatch.setenv("MY_GATEWAY_API_KEY", "json-token")
    monkeypatch.setenv(ENV_ROUTES, json.dumps({"routes": [{
        "role": "coding", "tier": Tier.MEDIUM, "provider": "anthropic",
        "model": "json-model", "base_url_env": "MY_GATEWAY_BASE_URL",
        "api_key_env": "MY_GATEWAY_API_KEY"}]}))
    spec = resolve("coding", Tier.MEDIUM)
    assert spec is not None
    assert (spec.source, spec.model, spec.provider) == (SOURCE_ROUTES_JSON, "json-model", "anthropic")


# ---------------------------------------------------------------- 2. 优先级


def test_role_env_wins_over_tier_and_global(monkeypatch):
    _set_role(monkeypatch, "CODING", "role-model")
    _set_tier(monkeypatch, "MEDIUM", "tier-model")
    _set_global(monkeypatch)
    spec = resolve("coding", Tier.MEDIUM)
    assert (spec.source, spec.model) == (SOURCE_ROLE_ENV, "role-model")


def test_tier_env_wins_over_global(monkeypatch):
    _set_tier(monkeypatch, "MEDIUM", "tier-model")
    _set_global(monkeypatch)
    spec = resolve("coding", Tier.MEDIUM)
    assert (spec.source, spec.model) == (SOURCE_TIER_ENV, "tier-model")


def test_role_env_wins_over_routes_json(monkeypatch):
    """显式 env 压过配置文件：同一个角色两处都配时，命中的必须是 env。"""
    monkeypatch.setenv("MY_GATEWAY_BASE_URL", "https://json.invalid")
    monkeypatch.setenv("MY_GATEWAY_API_KEY", "json-token")
    monkeypatch.setenv(ENV_ROUTES, json.dumps([{
        "role": "coding", "model": "json-model",
        "base_url_env": "MY_GATEWAY_BASE_URL", "api_key_env": "MY_GATEWAY_API_KEY"}]))
    _set_role(monkeypatch, "CODING", "role-model")
    assert resolve("coding", Tier.MEDIUM).source == SOURCE_ROLE_ENV


def test_exact_role_wins_over_wildcard(monkeypatch):
    monkeypatch.setenv("MY_GATEWAY_BASE_URL", "https://json.invalid")
    monkeypatch.setenv("MY_GATEWAY_API_KEY", "json-token")
    monkeypatch.setenv(ENV_ROUTES, json.dumps([
        {"role": "*", "model": "wildcard-model",
         "base_url_env": "MY_GATEWAY_BASE_URL", "api_key_env": "MY_GATEWAY_API_KEY"},
        {"role": "coding", "model": "exact-model",
         "base_url_env": "MY_GATEWAY_BASE_URL", "api_key_env": "MY_GATEWAY_API_KEY"},
    ]))
    assert resolve("coding", Tier.MEDIUM).model == "exact-model"
    assert resolve("reviewer", Tier.STRONG).model == "wildcard-model"


def test_role_level_falls_back_to_global_credentials(monkeypatch):
    """只给角色换模型、网关与凭证共用全局那一套 —— 最常见的配法必须成立。"""
    monkeypatch.setenv("MAOS_LLM_ROLE_CODING_MODEL", "role-model")
    _set_global(monkeypatch)
    spec = resolve("coding", Tier.MEDIUM)
    assert (spec.source, spec.model) == (SOURCE_ROLE_ENV, "role-model")
    assert (spec.base_url_env, spec.api_key_env) == (ENV_BASE_URL, ENV_API_KEY)


# ---------------------------------------------------------------- 3. 全空 → None


def test_all_levels_empty_returns_none_not_raise():
    """三级全落空必须**返回 None**：路由缺失是配置态，要落到既有的降级路径上，
    不能变成崩溃 —— 与 ``select_model_client()`` 缺 env 时的语义对齐。"""
    assert resolve("coding", Tier.MEDIUM) is None
    assert resolve("ap_intake", Tier.LIGHT) is None


def test_half_configured_level_does_not_hit(monkeypatch):
    """只配了模型 id、拿不到地址与凭证 —— 构造不出客户端，这一级不算命中。"""
    monkeypatch.setenv("MAOS_LLM_ROLE_CODING_MODEL", "role-model")
    assert resolve("coding", Tier.MEDIUM) is None


def test_routes_json_entry_not_ready_falls_through(monkeypatch):
    """JSON 条目引用的变量当前为空 → 未就绪，继续往下一级走，不是命中也不是崩。"""
    monkeypatch.setenv(ENV_ROUTES, json.dumps([{
        "role": "coding", "model": "json-model",
        "base_url_env": "NOT_SET_BASE_URL", "api_key_env": "NOT_SET_API_KEY"}]))
    _set_global(monkeypatch)
    assert resolve("coding", Tier.MEDIUM).source == SOURCE_GLOBAL


# ---------------------------------------------------------------- 4. key 不泄漏


def test_load_routes_rejects_secret_in_api_key_env(monkeypatch):
    """把假 key 塞进 ``api_key_env`` → 拒绝加载，且异常信息里不含那个值。"""
    monkeypatch.setenv(ENV_ROUTES, json.dumps([{
        "role": "coding", "model": "m",
        "base_url_env": "MY_GATEWAY_BASE_URL", "api_key_env": FAKE_KEY_VALUE}]))
    with pytest.raises(RouteConfigError) as excinfo:
        load_routes()
    message = str(excinfo.value)
    assert FAKE_KEY_VALUE not in message
    assert "api_key_env" in message


def test_load_routes_rejects_long_opaque_value_in_env_field(monkeypatch):
    """不带 ``sk-`` 前缀、但长得像凭证的无空白长串，落在 ``*_env`` 上同样拒。"""
    opaque = "A" * 41 + "-b64ish"
    monkeypatch.setenv(ENV_ROUTES, json.dumps([{
        "role": "coding", "model": "m",
        "base_url_env": opaque, "api_key_env": "MY_GATEWAY_API_KEY"}]))
    with pytest.raises(RouteConfigError) as excinfo:
        load_routes()
    assert opaque not in str(excinfo.value)


def test_load_routes_rejects_inline_credential_fields(monkeypatch):
    """写 ``api_key``（值）而不是 ``api_key_env``（名）—— 直接拒，别等它进日志。"""
    monkeypatch.setenv(ENV_ROUTES, json.dumps([{
        "role": "coding", "model": "m", "base_url_env": "MY_GATEWAY_BASE_URL",
        "api_key_env": "MY_GATEWAY_API_KEY", "api_key": FAKE_KEY_VALUE}]))
    with pytest.raises(RouteConfigError) as excinfo:
        load_routes()
    assert FAKE_KEY_VALUE not in str(excinfo.value)


def test_long_model_id_is_not_mistaken_for_secret(monkeypatch):
    """长度闸只开在 ``*_env`` 上：42 个字符的模型 id 不许被误判成密钥。"""
    long_model = "us.anthropic.claude-sonnet-5-20250101-v1:0"
    assert len(long_model) > 40
    monkeypatch.setenv("MY_GATEWAY_BASE_URL", "https://json.invalid")
    monkeypatch.setenv("MY_GATEWAY_API_KEY", "json-token")
    monkeypatch.setenv(ENV_ROUTES, json.dumps([{
        "role": "coding", "model": long_model,
        "base_url_env": "MY_GATEWAY_BASE_URL", "api_key_env": "MY_GATEWAY_API_KEY"}]))
    assert resolve("coding", Tier.MEDIUM).model == long_model


def test_describe_output_carries_no_values(monkeypatch):
    """诊断输出里只许有变量名和「已配置 / 未配置」，一个值都不许有。"""
    _set_global(monkeypatch)
    monkeypatch.setenv(ENV_API_KEY, FAKE_KEY_VALUE)
    blob = json.dumps(describe(), ensure_ascii=False) + render()
    assert FAKE_KEY_VALUE not in blob
    assert "https://global.invalid" not in blob
    assert ENV_API_KEY in blob


# ---------------------------------------------------------------- 5. describe 覆盖全池


def test_describe_covers_every_pooled_role():
    rows = describe()
    assert [r["role"] for r in rows] == sorted(AGENT_POOL)
    assert len(rows) == len(AGENT_POOL)
    for row in rows:
        assert row["tier"] == AGENT_POOL[row["role"]].identity.model_tier


def test_describe_reports_unresolved_and_missing_names_when_empty():
    for row in describe():
        assert row["source"] == SOURCE_UNRESOLVED
        assert row["model"] == ""
        assert row["missing_env"] == [ENV_MODEL, ENV_BASE_URL, ENV_API_KEY]


def test_describe_tier_distribution_matches_pool(monkeypatch):
    """全空 env 下的分组必须与 AGENT_POOL 的 tier 分布逐条对上（现取，不写死）。"""
    counts = {}
    for row in describe():
        counts[row["tier"]] = counts.get(row["tier"], 0) + 1
    expected = {}
    for cls in AGENT_POOL.values():
        expected[cls.identity.model_tier] = expected.get(cls.identity.model_tier, 0) + 1
    assert counts == expected
    assert set(counts) <= {Tier.STRONG, Tier.MEDIUM, Tier.LIGHT}


def test_describe_sources_switch_with_env(monkeypatch):
    _set_global(monkeypatch)
    _set_tier(monkeypatch, "STRONG", "tier-model")
    _set_role(monkeypatch, "CODING", "role-model")
    by_role = {r["role"]: r for r in describe()}
    assert by_role["coding"]["source"] == SOURCE_ROLE_ENV
    assert by_role["reviewer"]["source"] == SOURCE_TIER_ENV
    assert by_role["ap_intake"]["source"] == SOURCE_GLOBAL
    assert by_role["ap_intake"]["api_key_env_state"] == "已配置"


# ---------------------------------------------------------------- 6. RouteSpec 自身


def test_route_spec_is_frozen():
    spec = RouteSpec(role="coding", tier=Tier.MEDIUM, provider=DEFAULT_PROVIDER,
                     model="m", base_url_env=ENV_BASE_URL, api_key_env=ENV_API_KEY,
                     source=SOURCE_GLOBAL)
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.model = "other"


def test_route_spec_repr_has_no_secret(monkeypatch):
    """repr 里只有变量名。字段里本就存不下值，这条钉的是「以后也别加值字段」。"""
    _set_role(monkeypatch, "CODING", "role-model")
    monkeypatch.setenv("MAOS_LLM_ROLE_CODING_API_KEY", FAKE_KEY_VALUE)
    text = repr(resolve("coding", Tier.MEDIUM))
    assert FAKE_KEY_VALUE not in text
    assert "MAOS_LLM_ROLE_CODING_API_KEY" in text


# ---------------------------------------------------------------- 7. JSON 载入的两种形态与拒绝面


def test_load_routes_accepts_file_path(monkeypatch, tmp_path):
    path = tmp_path / "routes.json"
    path.write_text(json.dumps({"routes": [{
        "role": "coding", "model": "file-model",
        "base_url_env": "MY_GATEWAY_BASE_URL",
        "api_key_env": "MY_GATEWAY_API_KEY"}]}), encoding="utf-8")
    monkeypatch.setenv(ENV_ROUTES, str(path))
    monkeypatch.setenv("MY_GATEWAY_BASE_URL", "https://file.invalid")
    monkeypatch.setenv("MY_GATEWAY_API_KEY", "file-token")
    assert [s.model for s in load_routes()] == ["file-model"]
    assert resolve("coding", Tier.MEDIUM).source == SOURCE_ROUTES_JSON


def test_load_routes_empty_without_env():
    assert load_routes() == []


def test_load_routes_rejects_unknown_tier(monkeypatch):
    """三档写死，第四档一律拒 —— 铁律 9 的同一条取向落在配置面上。"""
    monkeypatch.setenv(ENV_ROUTES, json.dumps([{
        "role": "coding", "tier": "ultra", "model": "m",
        "base_url_env": "MY_GATEWAY_BASE_URL", "api_key_env": "MY_GATEWAY_API_KEY"}]))
    with pytest.raises(RouteConfigError, match="tier"):
        load_routes()


def test_load_routes_rejects_broken_json(monkeypatch):
    monkeypatch.setenv(ENV_ROUTES, "{not json")
    with pytest.raises(RouteConfigError):
        load_routes()


def test_load_routes_rejects_missing_file(monkeypatch, tmp_path):
    monkeypatch.setenv(ENV_ROUTES, str(tmp_path / "nope.json"))
    with pytest.raises(RouteConfigError, match="读不到"):
        load_routes()
