"""跨境电商适配器的骨架 —— **七个平台共用的那半条流程**。

## 这一层为什么是模板方法，而不是一个基类给子类随便覆盖

`query()` 在本模块里是**成品**，子类不许覆盖（`__init_subclass__` 钉住了这一点）。
子类只填三个钩子：请求怎么拼、响应里字段在哪、错误怎么读。

理由不是洁癖。`query()` 里那几步 —— 404 抛 `KeyError` 而不是返回空订单、
归一失败抛而不是兜底、金额不进浮点、`version` 按 epoch 毫秒派生 —— 全部是
`OrderSystemPort` 与铁律 8 要求的纪律。七个适配器各写一遍，就是七次机会写错，
而写错的那次不会有人发现：一个「返回了 v1 空订单」的适配器在测试里长得跟正常的一样，
只有在真出漂移的那天才暴露，那天已经在动钱了。

## 凭据只从环境变量读

铁律 6。`read_credential()` 缺了就抛，**不回落到空串** —— 空串会让请求带着
`Authorization: Bearer ` 发出去，平台回 401，然后人去查「为什么签名错了」，
真相却是「凭据根本没配」。这两件事的排查方向完全相反。

## 密钥不进 repr / 不进 traceback

`__repr__` 只打平台名与账号标识，口径同 `gateway.AlipaySandboxAdapter.__repr__`
与 `model/client.py::GatewayModelClient`。URL 进异常前过 `redact_url()`，
因为 Lazada / Shopee 把签名拼在 query 上。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from maos.tools.order import ExternalOrder
from maos.tools.commerce.mapping import (
    StatusRule, derive_version, map_status, normalize_amount,
)
from maos.tools.commerce.transport import (
    HttpResponse, HttpTransport, UrllibTransport, redact_url,
)


class CommerceError(RuntimeError):
    """平台返回了一个**错误响应**（与 `TransportError` 的「没拿到响应」区分）。

    带 `platform` / `status` / `code` 三样，因为这三样决定了人接下来做什么：
    401 去查凭据、429 去查限流窗口、平台业务码去查该平台文档。
    """

    def __init__(self, platform: str, status: int, code: str, message: str) -> None:
        self.platform, self.status, self.code = platform, status, code
        super().__init__(f"[{platform}] HTTP {status} {code}：{message}")


class CredentialMissing(RuntimeError):
    """凭据没配。**不是认证失败** —— 认证失败是平台说「你这把钥匙不对」，
    这个是「我们根本没带钥匙出门」。混成一件事会把排查引向错误的方向。"""


def read_credential(env_key: str, *, platform: str, purpose: str) -> str:
    """从环境变量读一条凭据。缺了抛 `CredentialMissing`，**不回落空串**。

    `purpose` 会进异常信息 —— 报「Shopify 的 Admin API access token」比报
    「SHOPIFY_ACCESS_TOKEN 未设置」有用：后者人还要再去查那个变量该填什么。
    """
    value = os.environ.get(env_key, "").strip()
    if not value:
        raise CredentialMissing(
            f"{platform} 缺凭据：环境变量 {env_key} 未设置或为空（{purpose}）。"
            "凭据一律只从环境变量读，禁止写进任何文件（铁律 6）"
        )
    return value


@dataclass(frozen=True)
class QueryRequest:
    """子类拼好的一次查单请求。`frozen` 是为了让它能被测试原样断言。"""

    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes | None = None


@dataclass(frozen=True)
class RawOrder:
    """从平台响应里**取出来**的原始字段，还没归一。

    分成「取字段」与「归一」两步，是为了让子类只负责「这个平台把金额放在哪」，
    而不用再操心「金额要不要转字符串」—— 后者七个平台是同一个答案，
    写七遍就会有一遍写错。
    """

    order_id: str
    status_payload: dict
    """喂给 `map_status()` 的那个 dict。通常就是订单对象本身 —— 规则表里的
    `field` 路径是相对它写的。"""
    amount: object
    updated_at: object


class CommerceAdapter:
    """所有跨境电商适配器的基类。实现 `OrderSystemPort`（只有 `query`）。

    子类必须声明 `platform` 与 `status_rules`，并实现三个钩子。
    """

    platform: str = ""
    status_rules: tuple[StatusRule, ...] = ()

    #: 平台把「订单不存在」表达成哪些 HTTP 状态。默认只有 404；
    #: Amazon SP-API 用 400 + `InvalidInput` 表达找不到单，所以子类可以改。
    not_found_status: frozenset[int] = frozenset({404})

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        # query() 是成品流程，覆盖它等于绕开 404/归一/浮点那三条纪律。
        # 在**定义时**拦住，而不是等到运行时出事 —— 这是唯一能在写代码那一刻
        # 就报错的时机。
        if "query" in cls.__dict__:
            raise TypeError(
                f"{cls.__name__} 覆盖了 query()：它是模板方法，保证「404 抛 KeyError、"
                "归一失败抛、金额不进浮点、version 按 epoch 毫秒派生」这几条纪律。"
                "要改平台行为请填 build_query_request / parse_order / read_error 三个钩子"
            )

    def __init__(self, *, transport: HttpTransport | None = None,
                 account: str = "") -> None:
        self.transport = transport if transport is not None else UrllibTransport()
        #: 账号标识（店铺域名 / seller id）。**不是密钥**，可以进 repr —— 排查时
        #: 「哪个店」是第一个要知道的事。
        self.account = account
        if not self.platform:
            raise TypeError(f"{type(self).__name__} 没声明 platform")
        if not self.status_rules:
            raise TypeError(
                f"{type(self).__name__} 没声明 status_rules —— 归一表为空时 "
                "map_status() 会对每一笔单都抛，等于这个适配器根本不能用。"
                "空表不是「暂时没填」的合法状态，是漏了"
            )

    def __repr__(self) -> str:
        return f"{type(self).__name__}(platform={self.platform!r}, account={self.account!r})"

    # ---- 成品流程：不许覆盖 -------------------------------------------------

    def query(self, order_id: str) -> ExternalOrder:
        """读一笔订单的当前版本。

        实现 `OrderSystemPort.query` 的契约：**查不到抛 `KeyError`**，
        不返回一个空订单 —— 那会把「订单不见了」伪装成「版本一致，放行」。
        """
        request = self.build_query_request(order_id)
        response = self.transport.request(
            request.method, request.url, headers=request.headers, body=request.body)

        if response.status in self.not_found_status:
            raise KeyError(
                f"{self.platform} 里没有订单 {order_id!r}"
                f"（HTTP {response.status}，{redact_url(request.url)}）"
            )
        if response.status >= 400:
            code, message = self.read_error(response)
            raise CommerceError(self.platform, response.status, code, message)

        payload = self.decode(response)
        raw = self.parse_order(payload)
        return ExternalOrder(
            order_id=str(raw.order_id),
            version=derive_version(raw.updated_at, platform=self.platform),
            status=map_status(self.status_rules, raw.status_payload, platform=self.platform),
            amount=normalize_amount(raw.amount, platform=self.platform),
            updated_at=str(raw.updated_at),
        )

    def decode(self, response: HttpResponse) -> dict:
        """把响应体解成 dict。JSON 解不动就抛，**不返回空 dict**。

        子类一般不用改 —— 七个平台全是 JSON。留成可覆盖是给 eBay 的 XML 旧接口
        留的口子（Trading API 仍是 XML，若日后要接）。
        """
        try:
            payload = json.loads(response.body or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise CommerceError(
                self.platform, response.status, "BAD_JSON",
                f"响应体不是合法 JSON：{exc}（前 200 字节：{response.body[:200]!r}）"
            ) from exc
        if not isinstance(payload, dict):
            raise CommerceError(
                self.platform, response.status, "BAD_SHAPE",
                f"响应体顶层不是对象，是 {type(payload).__name__}"
            )
        return payload

    # ---- 子类要填的三个钩子 -------------------------------------------------

    def build_query_request(self, order_id: str) -> QueryRequest:
        """拼一次查单请求：URL、签名头、body。凭据在这里用 `read_credential()` 取。"""
        raise NotImplementedError

    def parse_order(self, payload: dict) -> RawOrder:
        """从平台响应里取出四样东西。取不到该抛，**不要填默认值**。"""
        raise NotImplementedError

    def read_error(self, response: HttpResponse) -> tuple[str, str]:
        """从错误响应里读出 `(平台错误码, 人话说明)`。读不出来返回 `("", 原文前 N 字节)`。

        **不要在这里抛** —— 这个钩子是在处理一个已经出错的响应，
        它再抛一个解析异常会把原始错误盖掉，人就看不到平台到底说了什么。
        """
        return "", response.text()[:500]
