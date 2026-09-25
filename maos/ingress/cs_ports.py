"""客服前台的两个外部端口：只读查单与退款只读预检（p13 · T172）。

前台（``maos/domain/cs/**``、``maos/skills/builtin/cs/**``）**一个工具都不碰** —— 静态守卫
在那边，本模块在扫描范围外。前台只认 ``maos.domain.cs.ports`` 的三个 Protocol，实现放在
这里，由装配处（``scripts/run_ingress.py``）**注入**。跨轨契约：review/p13-cs-contracts.md
§1.2、§1.4「T172」。

## 查单：`CommerceOrderLookup`

* **恰好一次** ``invoke_tool(ORDER_QUERY_PORT, …)``，落一行 ToolInvoked；extras 照 p12 §1.2
  （``plan_id = cs:…``、``task_id = 轮次 id``、``trace_id = ""``）。
* 订单系统实例登记在工具侧的全局表里，而 ``custom_case.run_payload`` 每跑一次都会
  ``reset_order_systems()`` 清表。所以每次查单**之前**把自己的 systems 重新登记一遍，
  用 CS 自有的登记名（``cs:`` + 系统名）—— 不顶掉退款链路登记的同名系统，也不被它顶掉。
* 异常翻译顺序（契约原文）：``UnmappedOrderStatus`` → unmapped_status；其它 ``ValueError`` →
  platform_error；``KeyError`` → not_found；其它 ``LookupError`` → system_misconfigured；
  其它异常 → platform_error。空系统名 / 空查单键 / 系统没注入 → system_misconfigured，**不调用**。
* **异常原文一个字都不出这个函数**：``MockOrderSystem`` 的 KeyError 里列着账本上**别的**
  订单号。``LookupResult.error_kind`` 只放类名；日志只打类名与打码后的查单键。
  （存量行为：``invoke_tool`` 自己会把 ``类名: 原文`` 写进 ToolInvoked 的 ``detail.error``，
  那一处不归本模块改，记在 BACKLOG ``task-t172``，p14 处理。）
* 永不抛。

## 退款预检：`LedgerRefundPrecheck`

只读。复用 router 模块的 ``preflight``（它自建一个 ``:memory:`` 库算裁定）与
``scripts/run_requests.py`` 的 ``build_case`` / 原因词表 —— 与 ``/refund`` 同一套口径，
不另算。**不建 Ticket、不写任何库、不调任何会改状态的函数**。出参里的 ``command_line``
是一行现成的 ``/refund <订单号> <原因码>``，由内部同事**以自己的名义**发出去才会建待办。
永不抛；条件不满足就 ``ok=False`` 并写 ``refused_why``（枚举见 `REFUSED_WHYS`）。

## 装配：`build_order_lookup_from_env`

``MAOS_CS_ORDER_SYSTEMS`` 不配 → None（前台照 p12 行为）；``demo`` → 台账造的
``MockOrderSystem``；JSON → 按 ``maos.tools.commerce`` 包里**动态发现**的适配器构造。
凭据由适配器在**发请求的那一刻**从环境变量读（铁律 6），本模块构造时一个都不读。
"""

from __future__ import annotations

import importlib
import json
import logging
import math
import os
import pkgutil
from pathlib import Path
from typing import Any, Mapping

from maos.domain.cs import ports as P
from maos.domain.cs.ports import Binding, LookupResult, PrecheckResult
from maos.tools.commerce.mapping import UnmappedOrderStatus
from maos.tools.order import (
    ALL_ORDER_STATUSES, DEFAULT_ORDER_SYSTEM, ORDER_PAID, ORDER_QUERY_PORT, MockOrderSystem,
    register_order_system,
)
from maos.tools.port import invoke_tool

log = logging.getLogger("maos.ingress.cs_ports")

__all__ = [
    "CS_SYSTEM_PREFIX", "CommerceOrderLookup", "DEFAULT_TRANSPORT_TIMEOUT_S",
    "ENV_ORDER_SYSTEMS", "LedgerRefundPrecheck", "OrderReplyInvalid", "REFUSED_WHYS",
    "TRANSPORT_MAX_RETRIES", "build_order_lookup_from_env", "commerce_platforms",
    "cs_system_name", "demo_order_system", "mask_query_key", "match_reason_codes",
]

# ---------------------------------------------------------------------------
# 查单
# ---------------------------------------------------------------------------
#: CS 在工具侧登记表里用的名字前缀。退款链路登记的是裸名（``demo-orders``），
#: 两边互不顶掉；``run_payload`` 清表之后，下一次查单前这里再登记回来。
CS_SYSTEM_PREFIX = "cs:"

#: 装配处给电商适配器造 ``UrllibTransport`` 时的单次超时（秒）与总尝试次数。
#: 查单挂在客户的一轮对话上（同步），存量缺省 20 秒 × 3 次对聊天太长。
DEFAULT_TRANSPORT_TIMEOUT_S = 5.0
TRANSPORT_MAX_RETRIES = 2


class OrderReplyInvalid(ValueError):
    """订单系统返回的形状不对（不是 dict、状态不在四态里、版本不是整数）。

    只作为 ``LookupResult.error_kind`` 的类名出现：本模块自己判出来的形状错，
    也要有一个能按名字认出来的归类，而不是空串。
    """


def cs_system_name(system_name: str) -> str:
    """CS 自有的登记名。"""
    return CS_SYSTEM_PREFIX + str(system_name)


def mask_query_key(query_key: str) -> str:
    """日志里的查单键：只留末 4 位，前面一个省略号；不足 5 位整段打星。"""
    s = str(query_key or "")
    return "…" + s[-4:] if len(s) > 4 else "*" * len(s)


class CommerceOrderLookup:
    """`maos.domain.cs.ports.OrderLookup` 的实现：经 ``order.query`` 只读查一单。

    ``systems`` 是 ``{系统名: 订单系统对象}``（有 ``query(order_id)`` 的对象：
    ``MockOrderSystem`` 或 ``maos.tools.commerce`` 的适配器）。系统名就是
    ``Binding.system_name`` 里写的那个名字。
    """

    def __init__(self, systems: Mapping[str, Any], *,
                 transport_timeout_s: float = DEFAULT_TRANSPORT_TIMEOUT_S) -> None:
        timeout = float(transport_timeout_s)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("transport_timeout_s 必须是正数")
        self._systems: dict[str, Any] = {str(k): v for k, v in dict(systems).items()}
        #: 装配处给适配器造传输层时用的超时。本类**不改**注入进来的对象，
        #: 只把这个预算留在这里，由 `build_order_lookup_from_env` 同一个数造传输层。
        self.transport_timeout_s = timeout

    @property
    def system_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._systems))

    def system(self, name: str) -> Any:
        """按系统名取注入的订单系统对象（装配自检与测试用）；没有就抛 KeyError。"""
        return self._systems[str(name)]

    def _register(self) -> None:
        for name, system in self._systems.items():
            register_order_system(cs_system_name(name), system)

    def lookup(self, store: Any, binding: Binding, *, plan_id: str,
               task_id: str) -> LookupResult:
        """只读查一单。恰好一次 ``invoke_tool``（前置条件不满足时零次）；永不抛。"""
        try:
            return self._lookup(store, binding, plan_id=plan_id, task_id=task_id)
        except Exception as exc:                         # noqa: BLE001 —— 永不抛
            return self._failed(P.LOOKUP_PLATFORM_ERROR, type(exc).__name__,
                                str(getattr(binding, "system_name", "") or ""),
                                str(getattr(binding, "query_key", "") or ""))

    def _lookup(self, store: Any, binding: Binding, *, plan_id: str,
                task_id: str) -> LookupResult:
        system_name = str(getattr(binding, "system_name", "") or "")
        query_key = str(getattr(binding, "query_key", "") or "")
        if not system_name.strip() or not query_key.strip() or system_name not in self._systems:
            return self._failed(P.LOOKUP_MISCONFIGURED, "", system_name, query_key)

        extras = {"plan_id": str(plan_id or ""), "task_id": task_id, "trace_id": ""}
        try:
            self._register()
            reply = invoke_tool(
                ORDER_QUERY_PORT,
                {"system_name": cs_system_name(system_name), "order_id": query_key},
                store=store, extras=extras)
        # 顺序即契约：UnmappedOrderStatus 是 ValueError 的子类、KeyError 是 LookupError
        # 的子类，先窄后宽。异常对象只取类名，原文不进任何出参、不进日志。
        except UnmappedOrderStatus as exc:
            return self._failed(P.LOOKUP_UNMAPPED, type(exc).__name__, system_name, query_key)
        except ValueError as exc:
            return self._failed(P.LOOKUP_PLATFORM_ERROR, type(exc).__name__,
                                system_name, query_key)
        except KeyError as exc:
            return self._failed(P.LOOKUP_NOT_FOUND, type(exc).__name__, system_name, query_key)
        except LookupError as exc:
            return self._failed(P.LOOKUP_MISCONFIGURED, type(exc).__name__,
                                system_name, query_key)
        except Exception as exc:                         # noqa: BLE001
            return self._failed(P.LOOKUP_PLATFORM_ERROR, type(exc).__name__,
                                system_name, query_key)

        try:
            result = _result_from(reply, system_name, query_key)
        except Exception as exc:                         # noqa: BLE001
            return self._failed(P.LOOKUP_PLATFORM_ERROR, type(exc).__name__,
                                system_name, query_key)
        log.info("查单 outcome=%s system=%s key=%s", result.outcome, system_name,
                 mask_query_key(query_key))
        return result

    @staticmethod
    def _failed(outcome: str, error_kind: str, system_name: str,
                query_key: str) -> LookupResult:
        log.warning("查单 outcome=%s kind=%s system=%s key=%s", outcome, error_kind or "-",
                    system_name, mask_query_key(query_key))
        return LookupResult(outcome=outcome, system_name=system_name, query_key=query_key,
                            error_kind=error_kind)


def _result_from(reply: Any, system_name: str, query_key: str) -> LookupResult:
    """``ExternalOrder.to_dict()`` → LookupResult。形状不对抛 `OrderReplyInvalid`。"""
    if not isinstance(reply, Mapping):
        raise OrderReplyInvalid("order.query 的返回不是对象")
    status = reply.get("status")
    if not isinstance(status, str) or status not in P.ORDER_STATUSES:
        raise OrderReplyInvalid("订单状态不在四态里")
    version = reply.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise OrderReplyInvalid("订单版本不是整数")
    updated_at = str(reply.get("updated_at") or "")
    if status == P.ORDER_AMENDED:
        return LookupResult(outcome=P.LOOKUP_AMENDED, system_name=system_name,
                            query_key=query_key, status="", version=version,
                            updated_at=updated_at)
    return LookupResult(outcome=P.LOOKUP_OK, system_name=system_name, query_key=query_key,
                        status=status, version=version, updated_at=updated_at)


# ---------------------------------------------------------------------------
# 退款预检
# ---------------------------------------------------------------------------
REFUSED_TENANT_MISMATCH = "tenant_mismatch"
REFUSED_ORDER_NOT_IN_LEDGER = "order_not_in_ledger"
REFUSED_REASON_AMBIGUOUS = "reason_ambiguous"
REFUSED_REASON_MISSING = "reason_missing"
REFUSED_CLOCK_MISSING = "clock_missing"
REFUSED_PREFLIGHT_ERROR = "preflight_error"
REFUSED_WHYS = (REFUSED_TENANT_MISMATCH, REFUSED_ORDER_NOT_IN_LEDGER,
                REFUSED_REASON_AMBIGUOUS, REFUSED_REASON_MISSING, REFUSED_CLOCK_MISSING,
                REFUSED_PREFLIGHT_ERROR)


def _router() -> Any:
    """惰性取 router 模块：它 import 一大串（控制面、退款域），查单那一半用不着。"""
    from maos.ingress import router
    return router


def match_reason_codes(text: str) -> frozenset[str]:
    """正文里写着的诉求**原因码**集合（存量词表：``run_requests.REASONS``，中文说法与英文 code）。

    口径同 router 的 ``_reason_in``：按词长从长到短扫。多一步：命中的词从正文里抹掉再扫
    短的，于是「七天无理由」不会再让「无理由」命中一次 —— 这两个恰好同码，但词表里
    有长词含短词、两者不同码的组合时，不抹掉就会把一句话误判成两个诉求。
    """
    rr = _router()._load_run_requests()
    code_of = dict(rr.REASONS)
    for code in set(rr.REASONS.values()):
        code_of.setdefault(code, code)
    work = str(text or "")
    found: set[str] = set()
    for word in sorted(code_of, key=lambda w: (-len(w), w)):
        if word and word in work:
            found.add(code_of[word])
            work = work.replace(word, "\x00")
    return frozenset(found)


class LedgerRefundPrecheck:
    """`maos.domain.cs.ports.RefundPrecheck` 的实现：按台账做一次只读退款预检。

    台账（``scenarios/custom/ledger.json`` 的形状）第一次用到时读一次并缓存 —— 与
    ``IngressRouter.ledger()`` 同一个读法：``custom_case.load(require_case=False)``，五张外部
    快照表逐张查，缺一张就读不出来；换台账要重启进程。读不出来按 ``preflight_error`` 拒，
    不缓存失败（文件一时读不到，下一次预检照样再读）。
    """

    def __init__(self, ledger_path: str | Path, *, ledger_tenant: str) -> None:
        self.ledger_path = Path(ledger_path)
        self.ledger_tenant = str(ledger_tenant or "")
        self._ledger: dict | None = None

    def _load(self) -> dict:
        if self._ledger is None:
            # 与 /refund 同一个只读入口：少一张表 /refund 那边读不出来，这边也不许给出 ok。
            from maos.flows.custom_case import load
            data = load(self.ledger_path, require_case=False)
            if not isinstance(data.get("order_snapshot"), list):
                raise ValueError("台账的 order_snapshot 不是表")
            self._ledger = data
        return self._ledger

    def precheck(self, *, tenant_id: str, order_no: str, reason_text: str,
                 now: str) -> PrecheckResult:
        """只读预检。永不抛；条件不满足 ``ok=False`` 并写 ``refused_why``。"""
        try:
            return self._precheck(tenant_id=tenant_id, order_no=order_no,
                                  reason_text=reason_text, now=now)
        except Exception as exc:                         # noqa: BLE001 —— 永不抛
            return _refused(REFUSED_PREFLIGHT_ERROR, type(exc).__name__)

    def _precheck(self, *, tenant_id: str, order_no: str, reason_text: str,
                  now: str) -> PrecheckResult:
        tenant = str(tenant_id or "")
        if not tenant or tenant != self.ledger_tenant:
            return _refused(REFUSED_TENANT_MISMATCH)

        order = str(order_no or "").strip()
        # 带空白的单号拼进 /refund 会被 Command.parse 拆成两个参数 —— 那不是这一单。
        if not order or order.split() != [order]:
            return _refused(REFUSED_ORDER_NOT_IN_LEDGER)
        try:
            ledger = self._load()
        except Exception as exc:                         # noqa: BLE001
            return _refused(REFUSED_PREFLIGHT_ERROR, type(exc).__name__)
        # 失败即关：这一单在 order_snapshot 里的**每一行**都得是台账租户的。build_case 在全表
        # 按单号取最高版本、不看租户 —— 别的租户有同号的行，裁定就可能按那一行和它的政策算。
        rows = [o for o in ledger["order_snapshot"] if isinstance(o, dict)
                and str(o.get("order_id")) == order]
        if not rows or any(str(o.get("tenant_id")) != self.ledger_tenant for o in rows):
            return _refused(REFUSED_ORDER_NOT_IN_LEDGER)

        codes = match_reason_codes(reason_text)
        if not codes:
            return _refused(REFUSED_REASON_MISSING)
        if len(codes) > 1:
            return _refused(REFUSED_REASON_AMBIGUOUS)
        (code,) = codes

        clock = "" if now is None else str(now).strip()
        if not clock:
            return _refused(REFUSED_CLOCK_MISSING)

        router = _router()
        rr = router._load_run_requests()
        try:
            payload = rr.build_case(ledger, {"order_id": order, "reason": code,
                                             "amount": None, "requested_at": rr._iso(clock)})
            checked = router.preflight(payload)
        except Exception as exc:                         # noqa: BLE001
            return _refused(REFUSED_PREFLIGHT_ERROR, type(exc).__name__)

        decision = str(checked.get("decision") or "")
        rule_ref = str(checked.get("deciding_rule") or "")
        summary = (f"只读预检：订单 {order}，诉求 {code}，"
                   f"裁定 {rr.DECISION_CN.get(decision, decision)}"
                   f"（依据 {rule_ref or '基线裁定，无适用的时限规则'}，"
                   f"付款至申请 {checked.get('elapsed_days')} 天）；"
                   "内部同事以自己的名义发出下面这行命令才会挂待办")
        log.info("退款预检 ok decision=%s rule=%s", decision, rule_ref or "-")
        return PrecheckResult(ok=True, decision=decision, rule_ref=rule_ref, reason_code=code,
                              command_line=f"/refund {order} {code}", summary=summary)


def _refused(why: str, error_kind: str = "") -> PrecheckResult:
    log.info("退款预检 refused=%s kind=%s", why, error_kind or "-")
    return PrecheckResult(ok=False, refused_why=why)


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------
ENV_ORDER_SYSTEMS = "MAOS_CS_ORDER_SYSTEMS"
DEMO = "demo"

#: 电商包里的基座三层，发现平台时跳过（与 test_commerce_consistency 的 INFRA 同一份口径）。
_COMMERCE_INFRA = frozenset({"base", "mapping", "transport"})
_COMMERCE_PACKAGE = "maos.tools.commerce"
_SPEC_FIELDS = frozenset({"platform", "account"})


def commerce_platforms() -> dict[str, type]:
    """动态发现 ``maos.tools.commerce`` 里的全部适配器：``{platform: 类}``。

    **不硬编码平台清单**（p11 契约 §F.1 同一条口径）：扫包目录、逐模块 import，
    导入失败的模块跳过并记一行类名，其余照常。
    """
    import maos.tools.commerce as commerce
    from maos.tools.commerce.base import CommerceAdapter

    prefix = _COMMERCE_PACKAGE + "."
    for info in sorted(pkgutil.iter_modules(list(commerce.__path__)), key=lambda i: i.name):
        if info.name in _COMMERCE_INFRA:
            continue
        try:
            importlib.import_module(prefix + info.name)
        except Exception as exc:                         # noqa: BLE001
            log.warning("电商适配器模块 %s 导入失败：%s", info.name, type(exc).__name__)
    found: dict[str, type] = {}
    stack = list(CommerceAdapter.__subclasses__())
    while stack:
        cls = stack.pop()
        stack.extend(cls.__subclasses__())
        if not cls.__module__.startswith(prefix) or cls.__module__ == prefix + "base":
            continue
        name = str(getattr(cls, "platform", "") or "")
        if name and name not in found:
            found[name] = cls
    return dict(sorted(found.items()))


def demo_order_system(ledger_path: str | Path) -> MockOrderSystem:
    """用台账的 ``order_snapshot`` 造一个 ``MockOrderSystem``（演示用）。

    每个订单号取最高版本那一行；状态取行里的 ``status`` 字段，台账没有这一列就是 ``paid``
    （存量台账就没有 —— 口径同 ``custom_case`` 按快照造订单系统那一段）；金额两位小数的
    字符串；修改时刻取 ``updated_at``，没有就取 ``paid_at``。坏行抛带键名的 ValueError。
    """
    path = Path(ledger_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError(f"{ENV_ORDER_SYSTEMS}=demo：台账读不出来（ledger_path={path}）") from None
    rows = data.get("order_snapshot") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError(f"{ENV_ORDER_SYSTEMS}=demo：台账缺 order_snapshot 表")
    latest: dict[str, tuple[int, dict]] = {}
    for i, row in enumerate(rows):
        where = f"order_snapshot[{i}]"
        if not isinstance(row, dict) or not str(row.get("order_id") or "").strip():
            raise ValueError(f"{ENV_ORDER_SYSTEMS}=demo：台账 {where}.order_id 缺失")
        try:
            version = int(row.get("version"))
        except (TypeError, ValueError):
            raise ValueError(f"{ENV_ORDER_SYSTEMS}=demo：台账 {where}.version 不是整数") from None
        if version < 1:
            raise ValueError(f"{ENV_ORDER_SYSTEMS}=demo：台账 {where}.version 必须从 1 起")
        status = row.get("status", ORDER_PAID)
        if status not in ALL_ORDER_STATUSES:
            raise ValueError(f"{ENV_ORDER_SYSTEMS}=demo：台账 {where}.status 不在订单四态里")
        try:
            amount = f"{float(row.get('amount_paid')):.2f}"
        except (TypeError, ValueError):
            raise ValueError(f"{ENV_ORDER_SYSTEMS}=demo：台账 {where}.amount_paid 不是数字") from None
        order_id = str(row["order_id"])
        if order_id not in latest or version > latest[order_id][0]:
            latest[order_id] = (version, dict(row, status=status, _amount=amount))
    system = MockOrderSystem()
    for order_id, (version, row) in sorted(latest.items()):
        system.ext_order(order_id=order_id, version=version, status=row["status"],
                         amount=row["_amount"],
                         updated_at=str(row.get("updated_at") or row.get("paid_at") or ""))
    return system


def build_order_lookup_from_env(env: Mapping[str, str] = os.environ, *,
                                ledger_path: str | Path | None = None,
                                ) -> CommerceOrderLookup | None:
    """按 ``MAOS_CS_ORDER_SYSTEMS`` 装配查单端口。

    * 不配（或空白）→ ``None``：前台照 p12 行为，不查单。
    * ``demo`` → ``{"demo-orders": 台账造的 MockOrderSystem}``；``ledger_path`` 必给。
    * JSON ``{系统名: {"platform": …, "account": …}}`` → 每个系统一个电商适配器，
      传输层 ``UrllibTransport(timeout=5 秒, 最多 2 次尝试)``。凭据由适配器在发请求时
      从进程环境变量读，这里一个都不读。同一平台只许配一家（店铺身份也读进程级环境变量，
      多配一家会串店），多店铺按店铺分凭据留给 p14。

    坏配置抛 ValueError，消息只带键名、不回显取值。
    """
    raw = str(env.get(ENV_ORDER_SYSTEMS, "") or "").strip()
    if not raw:
        return None
    if raw == DEMO:
        if ledger_path is None:
            raise ValueError(f"{ENV_ORDER_SYSTEMS}=demo 需要台账（ledger_path），没给")
        return CommerceOrderLookup({DEFAULT_ORDER_SYSTEM: demo_order_system(ledger_path)})

    try:
        spec = json.loads(raw)
    except ValueError:
        raise ValueError(f"{ENV_ORDER_SYSTEMS} 既不是 {DEMO!r} 也不是合法 JSON") from None
    if not isinstance(spec, dict) or not spec:
        raise ValueError(f"{ENV_ORDER_SYSTEMS} 必须是非空 JSON 对象 {{系统名: {{platform, account}}}}")

    from maos.tools.commerce.transport import UrllibTransport

    platforms = commerce_platforms()
    systems: dict[str, Any] = {}
    #: 平台 → 第一个用它的系统名。同平台第二家一律拒（失败即关）：现有适配器的店铺身份
    #: （店铺域名 / 站点 URL / 店铺 id）与凭据都读**进程级**环境变量，``account`` 多半只进
    #: repr —— 第二家实际查的是第一家的店，而且返回的单号与查询键一致，事后核不出来。
    first_of: dict[str, str] = {}
    for name, item in spec.items():
        key = f"{ENV_ORDER_SYSTEMS}[{name!r}]"
        if not str(name).strip():
            raise ValueError(f"{ENV_ORDER_SYSTEMS} 里有空的系统名")
        if not isinstance(item, dict):
            raise ValueError(f"{key} 必须是对象 {{platform, account}}")
        extra = sorted(set(item) - _SPEC_FIELDS)
        if extra:
            raise ValueError(f"{key} 有不认识的字段：{', '.join(map(str, extra))}"
                             f"（只认 {', '.join(sorted(_SPEC_FIELDS))}）")
        platform = item.get("platform")
        if not isinstance(platform, str) or platform not in platforms:
            raise ValueError(f"{key}.platform 不是已发现的平台（可选：{', '.join(platforms)}）")
        if platform in first_of:
            raise ValueError(
                f"{key} 与 {ENV_ORDER_SYSTEMS}[{first_of[platform]!r}] 的 platform 相同：同平台"
                "只许配一家 —— 店铺身份与凭据读进程级环境变量，第二家会查到第一家的店")
        first_of[platform] = str(name)
        account = item.get("account", "")
        if not isinstance(account, str):
            raise ValueError(f"{key}.account 必须是字符串")
        transport = UrllibTransport(timeout=DEFAULT_TRANSPORT_TIMEOUT_S,
                                    max_retries=TRANSPORT_MAX_RETRIES)
        systems[str(name)] = platforms[platform](transport=transport, account=account)
    return CommerceOrderLookup(systems, transport_timeout_s=DEFAULT_TRANSPORT_TIMEOUT_S)
