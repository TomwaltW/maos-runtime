"""跨境电商平台接入面 —— 七个平台的订单事实进 MAOS 的那道口子。

## 这个包是什么

`maos/tools/order.py` 定义了 `OrderSystemPort`（只有 `query`）与 `ExternalOrder`
（五个字段、四个状态），并且说清了它们为什么长这样：**MAOS 不持有订单的权威事实，
只持有「执行前读到的那一版」**（铁律 8）。在此之前那个 Port 只有一个 `MockOrderSystem`
实现，也就是说「去读外部订单系统」这条路径在演示里是真的、在生产里是空的。

本包补的是生产那一半：七个跨境电商平台，每个一个 `CommerceAdapter` 子类。

## 分层

    transport.py   HTTP 出口。零依赖（urllib）+ 可注入，于是测试打不到真网
    mapping.py     平台字段 → ExternalOrder 的归一。规则是**数据**，不是 if
    base.py        模板方法 + 凭据读取。query() 是成品，子类只填三个钩子
    <platform>.py  每个平台一个文件，只有规则表和三个钩子的实现

加一个平台 = 加一个文件 + 填一张带 `source` 的规则表。**不改上面三层**；
要改，说明归一层的形状错了，那是要停下来讨论的事，不是在子类里绕过去的事。

## 为什么放在 `maos/tools/` 下而不是新开顶层包

这些适配器是 `order.query` 这个 ToolPort 的**后端实现**，与
`tools/gateway.py` 里的 `AlipaySandboxAdapter` / `ManualReceiptAdapter` 同一性质
（那两个也是 ToolPort 的后端）。放进 `tools/` 是延续既有分层，不是新造一层。
之所以成为子包而不是一个 `commerce.py`，只是因为七个平台塞一个文件会到几千行；
`tools/mcp/` 已经是子包，有先例。

## 装配在哪

适配器实例由**装配处**注册，ToolPort 只收名字（`order.order_query(system_name=...)`）：

    from maos.tools.order import register_order_system
    from maos.tools.commerce.shopify import ShopifyAdapter
    register_order_system("shopify-main", ShopifyAdapter(account="my-shop.myshopify.com"))

理由在 `tools/order.py` 与 `tools/gateway.py` 的文件末节写过：params 里只放标量，
`params_digest` 才可复现，也才跨得了进程（迁 MCP 的前置条件）。
"""

from maos.tools.commerce.base import (
    CommerceAdapter,
    CommerceError,
    CredentialMissing,
    QueryRequest,
    RawOrder,
    read_credential,
)
from maos.tools.commerce.mapping import (
    ANY_NON_EMPTY,
    StatusRule,
    UnmappedOrderStatus,
    UnparsableTimestamp,
    derive_version,
    dig,
    map_status,
    normalize_amount,
)
from maos.tools.commerce.transport import (
    DEFAULT_TIMEOUT,
    RETRIABLE_STATUS,
    FakeTransport,
    HttpResponse,
    HttpTransport,
    TransportError,
    UrllibTransport,
    redact_url,
)

__all__ = [
    "ANY_NON_EMPTY", "CommerceAdapter", "CommerceError", "CredentialMissing",
    "DEFAULT_TIMEOUT", "FakeTransport", "HttpResponse", "HttpTransport",
    "QueryRequest", "RETRIABLE_STATUS", "RawOrder", "StatusRule",
    "TransportError", "UnmappedOrderStatus", "UnparsableTimestamp",
    "UrllibTransport", "derive_version", "dig", "map_status",
    "normalize_amount", "read_credential", "redact_url",
]
