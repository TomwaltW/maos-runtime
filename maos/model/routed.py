"""按角色路由的模型客户端包装层 —— 让一个 Worker 里的每个 Agent 各连各的模型。

背景：``runtime/worker.py`` 原来一个 ``model`` 变量喂给 22 个 Agent 构造器，
22 个角色共用同一个 ``ModelClient`` 实例。要让「Manager 走强推理、批量分类走
轻量模型」成立，就得让每个角色拿到自己的客户端 —— 而「谁被路由到哪」这件事
必须留痕，否则演示当天没人说得清那次调用到底打到了哪家。

本模块只做**包装与留痕**，不做**选路**：
  · 选路（role -> provider/model）是治理决策，属于路由表（别轨的产出）；
  · 本模块拿到已经选好的 ``inner``，给它挂上归属信息，并让归属跟着
    ``ModelResponse.meta`` 一路传下去（``meta`` 是 ``ModelResponse`` 已有字段，
    不改契约）。

## 为什么有 :func:`route_model_client` 这道「不包装」的闸

``core/store.py::usage_is_estimated`` 是全仓唯一一处「这次的 token 是估算还是
网关回的真实计费」的判定，实现是 ``isinstance(client, ScriptedModelClient)``，
而 ``BaseAgent.ask()`` 传给它的正是 Agent 手里那个客户端 —— 也就是包装器。
无脑包一层，``isinstance`` 当场变 False，``model_usage.estimated`` 从 1 翻成 0：
``ScriptedModelClient`` 算的那个 ``len(user) // 4`` 假 token **会被记成「网关回的
真实计费」**。屏幕上一点都看不出来，成本视图却已经在撒谎。

所以缺省路径（Scripted）**原样透传，一层都不包**。判据不自己另写一份，直接借
``usage_is_estimated`` 本身：本模块要守的不变量就是「包装不许改变这个函数的答案」，
拿它当谓词，两处永远同步，也不会有第二处「什么算估算」的口径。
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

from maos.model.client import ModelClient, ModelResponse

log = logging.getLogger("maos.model.routed")

#: ``route_source`` 缺省值：客户端自己没声明来路时，只说得出「是工厂给的」。
DEFAULT_ROUTE_SOURCE = "model_factory"


class RoutedModelClient(ModelClient):
    """给 ``inner`` 挂上路由归属（role / provider / route_source）的透明包装。

    ``complete()`` 原样透传给 ``inner``，只往返回的 ``ModelResponse.meta`` 里补三个
    键。**不改 token、不改 text、不改 model** —— 这一层不许影响任何计费口径。

    属性访问按 ``inner`` 转发，但**下划线开头的一律不转**（``getattr`` 会照常抛
    AttributeError）。这既躲开了 ``self.inner`` 尚未赋值时的无限递归，也顺手守住
    铁律 6：``GatewayModelClient._api_key`` 这类私有属性不会被包装器"漏"出来。
    转发是必要的而不是锦上添花 —— ``BaseAgent.ask()`` 失败分支取的是
    ``getattr(self.model, "model", "")``，包装之后那个 ``self.model`` 就是本对象，
    不转发的话失败行的 ``model`` 列会静默变空。
    """

    def __init__(self, inner: ModelClient, *, role: str, provider: str,
                 route_source: str, store: Any = None) -> None:
        self.inner = inner
        self.role = role
        self.provider = provider
        self.route_source = route_source
        self.store = store

    def __repr__(self) -> str:
        # 只打归属，不打 inner 的构造参数：inner 的 __repr__ 自己已经保证不含 key，
        # 但这一层不去赌「将来每个 provider 都记得这么做」。
        return (f"RoutedModelClient(role={self.role!r}, provider={self.provider!r}, "
                f"inner={type(self.inner).__name__})")

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self.inner, name)

    def complete(self, *, system: str, user: str, tier: str) -> ModelResponse:
        resp = self.inner.complete(system=system, user=user, tier=tier)
        if not isinstance(resp, ModelResponse):
            # 不认识的返回值不硬塞归属，原样放行 —— 这一层不该把一次成功的调用搞挂。
            return resp
        meta = dict(resp.meta or {})
        meta.update({"role": self.role, "provider": self.provider,
                     "route_source": self.route_source})
        # replace 而不是就地改：inner 可能复用同一个 response 对象（脚本回放就会），
        # 就地改 meta 会把上一次调用的归属改掉。
        return replace(resp, meta=meta)


def route_model_client(inner: ModelClient, *, role: str, provider: str = "",
                       route_source: str = "", store: Any = None) -> ModelClient:
    """把 ``inner`` 包成带路由归属的客户端；**估算口径的客户端原样返回**。

    见模块 docstring 里那段 ``estimated`` 翻面的说明：判据借的是
    ``core/store.py::usage_is_estimated``，不另立一份。延迟 import 的理由同
    ``store.py`` 那边 —— 模型层不该在模块级依赖存储层。

    ``provider`` / ``route_source`` 留空时按 ``inner`` 自述取（路由表造出来的客户端
    可以自带这两个属性），自述不到就回退到类名 / :data:`DEFAULT_ROUTE_SOURCE`。
    这样本模块不用 import 任何一个具体 provider，也就不会跟别轨的产出耦上。
    """
    from maos.core.store import usage_is_estimated

    if usage_is_estimated(inner):
        return inner
    return RoutedModelClient(
        inner, role=role,
        provider=provider or client_provider(inner),
        route_source=route_source or getattr(inner, "route_source", "")
        or DEFAULT_ROUTE_SOURCE,
        store=store,
    )


def client_provider(client: Any) -> str:
    """客户端自述的 provider 名；没有就用类名。**只取名字，不碰任何配置值。**

    铁律 6：``base_url`` / key 一个字都不进来 —— 这个字符串会被写进 event_log。
    """
    return str(getattr(client, "provider", "") or type(client).__name__)


def client_model_name(client: Any) -> str:
    """客户端自述的模型名；取不到就空串。同样只取名字。"""
    return str(getattr(client, "model", "") or "")
