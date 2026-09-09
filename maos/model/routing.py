"""角色 → 模型路由表 —— 把「哪个角色用哪家的哪个模型」变成可解析、可校验、可诊断的一张表。

本模块**只出解析与校验，不构造任何客户端、不发起任何网络请求**。
构造归 provider 层，注入给 Agent 归 runtime 层；这里产出的 :class:`RouteSpec`
是它们之间的中间产物。``select_model_client()`` 的签名与语义是冻结契约（A-12），
本模块不碰它，只在它之上补一层「按角色/按 tier 选模型」的能力。

铁律 6（离密钥最近的一层，逐条落在代码里）::

    RouteSpec 只存**变量名**（``base_url_env`` / ``api_key_env``），绝不存值。
    读值是下游构造客户端时的事，值一次都不进本模块的返回值、repr、异常与日志。
    ``load_routes()`` 对形如密钥的字段值直接**拒绝加载**，且错误信息不回显该值。

降级口径与 ``client.select_model_client()`` 对齐：三级全落空时 :func:`resolve`
**返回 None，不抛异常** —— 上层据此走既有的「降级成 ScriptedModelClient 并 WARNING」
那条路径。路由缺失是配置态，不是崩溃态。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from maos.model.client import ENV_API_KEY, ENV_BASE_URL, ENV_MODEL, Tier

# ---------------------------------------------------------------- 常量

ENV_ROUTES = "MAOS_LLM_ROUTES"          # 指向一份 JSON：内容本身或文件路径
ROLE_ENV_PREFIX = "MAOS_LLM_ROLE_"      # MAOS_LLM_ROLE_<ROLE>_MODEL / _BASE_URL / ...
TIER_ENV_PREFIX = "MAOS_LLM_TIER_"      # MAOS_LLM_TIER_<TIER>_MODEL / _BASE_URL / ...

DEFAULT_PROVIDER = "openai-compat"      # 只是个名字，本模块不据此构造任何东西
WILDCARD = "*"

SOURCE_ROLE_ENV = "role-env"
SOURCE_ROUTES_JSON = "routes-json"
SOURCE_TIER_ENV = "tier-env"
SOURCE_GLOBAL = "global-fallback"
SOURCE_UNRESOLVED = "unresolved"        # 只出现在 describe() 的行里，不会是 RouteSpec.source

VALID_TIERS = (Tier.STRONG, Tier.MEDIUM, Tier.LIGHT)   # 三档写死，不许有第四档

STATE_SET = "已配置"
STATE_UNSET = "未配置"

# 合法环境变量名：这是 *_env 字段的准入闸。真 key 一律过不了这一关
# （``sk-…`` 有连字符、Bearer 串有空格、长 base64 超长），所以它同时是
# 「只许写变量名」的语法约束和铁律 6 的主动闸，不是两套规则。
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

# 兜底闸：任何字段上出现这些形态都拒绝加载（哪怕它侥幸像个变量名）
_SECRET_PREFIXES = ("sk-", "sk_", "bearer ", "ghp_", "xoxb-")
_SECRET_MIN_LEN = 40                    # 长度 > 40 的无空白串，只对 *_env 字段生效


class RouteConfigError(ValueError):
    """路由表配置非法。

    错误信息里**只允许出现字段名与角色名，绝不允许出现字段值** ——
    这个异常最可能被打进日志，而它恰好是唯一能拿到疑似 key 的地方。
    """


@dataclass(frozen=True)
class RouteSpec:
    """一条路由：某角色（或通配）用哪家的哪个模型，凭证去哪个环境变量取。

    frozen=True 是有意的：解析完就该定死，任何一处「顺手改一下再传下去」
    都会让 describe() 的诊断与实际构造出的客户端对不上。
    """

    role: str            # "coding" / "*"（通配）
    tier: str            # Tier.STRONG / MEDIUM / LIGHT / "*"（不限 tier）
    provider: str        # "anthropic" / "openai-compat" —— 只是名字，本模块不构造
    model: str           # 模型 id
    base_url_env: str    # 变量**名**，不是值
    api_key_env: str     # 变量**名**，不是值
    source: str          # role-env / routes-json / tier-env / global-fallback


# ---------------------------------------------------------------- 小工具


def _env_of(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def _get(env: Mapping[str, str], name: str) -> str:
    return (env.get(name) or "").strip()


def _norm(name: str) -> str:
    """role / tier 名 → 环境变量中段：``ap_control`` → ``AP_CONTROL``。"""
    return re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")


def role_env_prefix(role: str) -> str:
    return f"{ROLE_ENV_PREFIX}{_norm(role)}_"


def tier_env_prefix(tier: str) -> str:
    return f"{TIER_ENV_PREFIX}{_norm(tier)}_"


def _looks_secret(value: str, *, check_length: bool = False) -> bool:
    """值是否形如密钥。只做判定，调用方负责在报错时不回显它。

    ``check_length`` 只在 ``*_env`` 字段上开：变量名不会长过 40，而模型 id
    会（``us.anthropic.claude-…-v1:0`` 这类带前缀的就是 42 个字符），
    把长度闸开在 model 上会误伤合法配置。
    """
    low = value.strip().lower()
    if not low:
        return False
    if any(low.startswith(p) for p in _SECRET_PREFIXES):
        return True
    return check_length and len(low) > _SECRET_MIN_LEN and not any(c.isspace() for c in low)


# ---------------------------------------------------------------- 三级解析


def _spec_from_prefix(prefix: str, *, role: str, tier: str, source: str,
                      env: Mapping[str, str]) -> tuple[RouteSpec | None, list[str]]:
    """按 ``<prefix>MODEL / _BASE_URL / _API_KEY / _PROVIDER`` 一组 env 解析一级。

    命中判据是**三要素齐全**：模型 id 非空，且能定位到非空的 base_url 与 api_key
    环境变量。只配了模型 id 却没有网关地址和凭证，构造不出客户端，算这一级未命中 ——
    下游拿到的 RouteSpec 必须是能直接用的，不能是半成品。

    base_url / api_key 允许落回全局的那两个变量名：只想给某个角色换模型、
    网关和 key 仍共用一套，是最常见的配法，不该逼人把同一个值抄三遍。
    返回 ``(spec, 本级缺失的变量名列表)``；spec 非 None 时缺失列表必为空。
    """
    model_env = f"{prefix}MODEL"
    model = _get(env, model_env)
    if not model:
        return None, [model_env]

    base_env = f"{prefix}BASE_URL" if _get(env, f"{prefix}BASE_URL") else ENV_BASE_URL
    key_env = f"{prefix}API_KEY" if _get(env, f"{prefix}API_KEY") else ENV_API_KEY

    missing = [name for name in (base_env, key_env) if not _get(env, name)]
    if missing:
        return None, missing

    provider = _get(env, f"{prefix}PROVIDER") or DEFAULT_PROVIDER
    return RouteSpec(role=role, tier=tier, provider=provider, model=model,
                     base_url_env=base_env, api_key_env=key_env, source=source), []


def _spec_from_global(*, role: str, tier: str,
                      env: Mapping[str, str]) -> tuple[RouteSpec | None, list[str]]:
    """全局兜底：与 ``select_model_client()`` 同口径 —— 三个都非空才成立。"""
    missing = [n for n in (ENV_MODEL, ENV_BASE_URL, ENV_API_KEY) if not _get(env, n)]
    if missing:
        return None, missing
    return RouteSpec(role=role, tier=tier, provider=DEFAULT_PROVIDER,
                     model=_get(env, ENV_MODEL), base_url_env=ENV_BASE_URL,
                     api_key_env=ENV_API_KEY, source=SOURCE_GLOBAL), []


def _spec_from_routes(routes: list[RouteSpec], *, role: str, tier: str, exact: bool,
                      env: Mapping[str, str]) -> tuple[RouteSpec | None, list[str]]:
    """从已加载的 JSON 路由表里挑一条。

    ``exact=True`` 只认 role 精确匹配；``exact=False`` 只认 ``"*"`` 通配条目
    （通配条目若声明了 tier，则只对该 tier 的角色生效）。
    条目引用的变量名当前为空 → 视为未就绪，不命中，继续往下一级走。
    """
    misses: list[str] = []
    for entry in routes:
        if exact:
            if entry.role != role:
                continue
        else:
            if entry.role != WILDCARD:
                continue
            if entry.tier != WILDCARD and entry.tier != tier:
                continue
        missing = [n for n in (entry.base_url_env, entry.api_key_env) if not _get(env, n)]
        if missing:
            misses.extend(missing)
            continue
        return RouteSpec(role=role, tier=entry.tier if entry.tier != WILDCARD else tier,
                         provider=entry.provider, model=entry.model,
                         base_url_env=entry.base_url_env, api_key_env=entry.api_key_env,
                         source=SOURCE_ROUTES_JSON), []
    return None, misses


def _resolve_with_diag(role: str, tier: str, env: Mapping[str, str],
                       routes: list[RouteSpec]) -> tuple[RouteSpec | None, str, list[str]]:
    """解析并带回诊断。返回 ``(spec, source, 缺失变量名列表)``。

    级序写死，不许调换：**按角色 → 按 tier → 全局兜底**。JSON 路由表挂在
    「按角色」与「按 tier」这两级内部（精确 role 条目跟在 role-env 后，
    通配条目跟在 tier-env 后）—— 显式 env 压过配置文件，精确压过通配。

    诊断口径：全落空时，报**第一个"配了一半"的级**缺什么（那里最可能是人的意图
    没落全）；一个都没配到一半，就报全局那三个 —— 与既有降级日志同口径。
    """
    attempts: list[tuple[RouteSpec | None, list[str]]] = [
        _spec_from_prefix(role_env_prefix(role), role=role, tier=tier,
                          source=SOURCE_ROLE_ENV, env=env),
        _spec_from_routes(routes, role=role, tier=tier, exact=True, env=env),
        _spec_from_prefix(tier_env_prefix(tier), role=role, tier=tier,
                          source=SOURCE_TIER_ENV, env=env),
        _spec_from_routes(routes, role=role, tier=tier, exact=False, env=env),
        _spec_from_global(role=role, tier=tier, env=env),
    ]
    for spec, _missing in attempts:
        if spec is not None:
            return spec, spec.source, []

    global_missing = attempts[-1][1]
    for _spec, missing in attempts[:-1]:
        # 「配了一半」= 这一级缺的不是 MODEL 而是地址/凭证，说明人已经在这一级动过手
        if missing and not any(m.endswith("_MODEL") for m in missing):
            return None, SOURCE_UNRESOLVED, sorted(set(missing))
    return None, SOURCE_UNRESOLVED, global_missing


def resolve(role: str, tier: str, env: Mapping[str, str] | None = None) -> RouteSpec | None:
    """解析某角色该走哪条路由。三级全落空返回 ``None``（**不抛异常**）。

    返回 None 的语义与 ``select_model_client()`` 缺 env 时一致：上层降级成
    ScriptedModelClient 并 WARNING。路由缺失绝不能变成崩溃。
    """
    e = _env_of(env)
    spec, _source, _missing = _resolve_with_diag(role, tier, e, load_routes(e))
    return spec


# ---------------------------------------------------------------- JSON 路由表


def _require_env_name(value: object, *, field: str, where: str) -> str:
    """*_env 字段必须是一个合法环境变量名。报错**不回显值**。"""
    if not isinstance(value, str) or not _ENV_NAME_RE.match(value.strip()):
        raise RouteConfigError(
            f"{where} 的 {field} 必须是环境变量**名**（形如 MY_API_KEY），"
            f"当前值不是合法变量名（值已隐去，铁律 6：不回显）")
    if _looks_secret(value, check_length=True):
        raise RouteConfigError(
            f"{where} 的 {field} 疑似写成了密钥**值**。路由表只许存变量名，"
            f"密钥只走环境变量（值已隐去，铁律 6：不回显）")
    return value.strip()


def _plain_field(value: object, *, field: str, where: str, default: str = "") -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise RouteConfigError(f"{where} 的 {field} 必须是字符串")
    text = value.strip()
    if _looks_secret(text):
        raise RouteConfigError(
            f"{where} 的 {field} 疑似密钥值，拒绝加载（值已隐去，铁律 6：不回显）")
    return text or default


def _parse_entry(raw: object, index: int) -> RouteSpec:
    where = f"routes[{index}]"
    if not isinstance(raw, dict):
        raise RouteConfigError(f"{where} 必须是对象")

    role = _plain_field(raw.get("role"), field="role", where=where)
    if not role:
        raise RouteConfigError(f"{where} 缺 role（用 \"*\" 表示通配）")
    where = f"routes[{index}](role={role})"

    tier = _plain_field(raw.get("tier"), field="tier", where=where, default=WILDCARD)
    if tier not in VALID_TIERS and tier != WILDCARD:
        raise RouteConfigError(
            f"{where} 的 tier={tier!r} 不是三档之一 {VALID_TIERS} 或 \"*\"")

    model = _plain_field(raw.get("model"), field="model", where=where)
    if not model:
        raise RouteConfigError(f"{where} 缺 model")

    provider = _plain_field(raw.get("provider"), field="provider", where=where,
                            default=DEFAULT_PROVIDER)

    for legacy in ("base_url", "api_key"):
        if legacy in raw:
            raise RouteConfigError(
                f"{where} 出现了 {legacy} 字段：路由表只许写 {legacy}_env（变量名），"
                f"不许写值（铁律 6）")

    return RouteSpec(
        role=role, tier=tier, provider=provider, model=model,
        base_url_env=_require_env_name(raw.get("base_url_env"),
                                       field="base_url_env", where=where),
        api_key_env=_require_env_name(raw.get("api_key_env"),
                                      field="api_key_env", where=where),
        source=SOURCE_ROUTES_JSON,
    )


def load_routes(env: Mapping[str, str] | None = None) -> list[RouteSpec]:
    """加载 ``MAOS_LLM_ROUTES`` 指向的 JSON 路由表。未配置返回 ``[]``。

    取值既可以是 JSON 内容本身，也可以是文件路径 —— 按首个非空字符判断：
    ``{`` / ``[`` 开头当内容，否则当路径。合法 JSON 文档不会以别的字符开头，
    合法路径也几乎不会以这两个字符开头，这条判据不会误伤。

    顶层可以是 ``{"routes": [...]}``，也可以直接是 ``[...]``。
    任何一条非法就整份拒绝加载（不做部分接受）：半张路由表比没有路由表更难查。
    """
    raw = _get(_env_of(env), ENV_ROUTES)
    if not raw:
        return []

    if raw[0] in "{[":
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RouteConfigError(f"{ENV_ROUTES} 不是合法 JSON：{exc.msg}（位置 {exc.pos}）") from None
    else:
        path = Path(raw)
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except OSError:
            raise RouteConfigError(f"{ENV_ROUTES} 指向的文件读不到：{path}") from None
        except json.JSONDecodeError as exc:
            raise RouteConfigError(f"{path} 不是合法 JSON：{exc.msg}（位置 {exc.pos}）") from None

    if isinstance(doc, dict):
        entries = doc.get("routes", [])
    elif isinstance(doc, list):
        entries = doc
    else:
        raise RouteConfigError(f"{ENV_ROUTES} 顶层必须是对象或数组")
    if not isinstance(entries, list):
        raise RouteConfigError(f"{ENV_ROUTES} 的 routes 必须是数组")

    return [_parse_entry(item, i) for i, item in enumerate(entries)]


# ---------------------------------------------------------------- 诊断


def _state(env: Mapping[str, str], name: str) -> str:
    return STATE_SET if _get(env, name) else STATE_UNSET


def describe(env: Mapping[str, str] | None = None) -> list[dict]:
    """对全部在池角色逐个 resolve，输出一张人能一眼看懂的诊断表。

    输出里**一个 key 值都没有**：只有变量名和「已配置 / 未配置」。
    角色清单从 ``AGENT_POOL`` 现取，不写死 —— 写死的清单会随新角色加入而漂。
    """
    from maos.agents.base import AGENT_POOL   # 局部导入：避免与 agents 包形成导入环

    e = _env_of(env)
    routes = load_routes(e)
    rows: list[dict] = []
    for role in sorted(AGENT_POOL):
        tier = AGENT_POOL[role].identity.model_tier
        spec, source, missing = _resolve_with_diag(role, tier, e, routes)
        rows.append({
            "role": role,
            "tier": tier,
            "provider": spec.provider if spec else "",
            "model": spec.model if spec else "",
            "source": source,
            "base_url_env": spec.base_url_env if spec else "",
            "api_key_env": spec.api_key_env if spec else "",
            "base_url_env_state": _state(e, spec.base_url_env) if spec else STATE_UNSET,
            "api_key_env_state": _state(e, spec.api_key_env) if spec else STATE_UNSET,
            "missing_env": missing,
            "candidate_env": [f"{role_env_prefix(role)}MODEL",
                              f"{tier_env_prefix(tier)}MODEL",
                              ENV_MODEL],
        })
    return rows


def render(rows: list[dict] | None = None, env: Mapping[str, str] | None = None) -> str:
    """把 :func:`describe` 的输出渲染成一张定宽表，给人看的。同样一个值都不带。"""
    rows = describe(env) if rows is None else rows
    head = f"{'role':<24}{'tier':<8}{'provider':<16}{'model':<24}{'source':<16}缺失/凭证变量名"
    lines = [head, "-" * len(head)]
    for r in rows:
        tail = ("缺 " + ",".join(r["missing_env"])) if r["missing_env"] else \
               f"{r['base_url_env']}({r['base_url_env_state']}) {r['api_key_env']}({r['api_key_env_state']})"
        lines.append(f"{r['role']:<24}{r['tier']:<8}{r['provider'] or '-':<16}"
                     f"{r['model'] or '-':<24}{r['source']:<16}{tail}")
    return "\n".join(lines)
