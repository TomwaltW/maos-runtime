"""maos.store —— 存储后端的可插拔面（v4 手册 P1 第 7 步，补的是历史欠账）。

对外五样东西：

    StorePort             五方法契约（F-2，落地即冻结）
    create_store()        按 MAOS_STORE_BACKEND 选后端，缺省 sqlite
    SqliteStorePort       包住核心 store.py 的适配器（组合，不是继承）
    PgStorePort           PG 后端，全文走 tsvector、向量走 pgvector
    PgBackendUnavailable  PG 此刻用不了（没驱动 / 没 DSN / 连不上）时抛的那个

**本包是纯新增**：现有链路一个调用方都没改，缺省路径的行为与没有本包时逐字节一致，
全量测试与 `python3 run.py` 是唯一判据。谁要接进主链路，接的那一轨自己验。

后端名只认 `sqlite` 与 `postgres` 两个字面量（大小写不敏感，前后空白忽略）。别的
一律抛 ValueError，**不回落缺省** —— 环境变量拼错一个字母就静默跑 sqlite，比直接
报错难查一个量级：你会以为在验 PG，其实一行 PG 代码都没执行。

## 两个后端现在都从这个入口出

P1 落地时 `PgStorePort` 还是空壳，工厂因此无条件拒绝 postgres。空壳后来填实并在本机
Docker PG 16 + pgvector 上实测跑通（`maos/tests/test_pg_store_live.py`，无库自动 skip），
工厂跟着放行 —— 拿 PG 后端就是两行环境变量：

    export MAOS_STORE_BACKEND=postgres
    export MAOS_PG_DSN='postgresql://<user>:<pass>@<host>:<port>/<db>'

「可插拔」到这一步才在**公共入口一级**成立。在此之前只有绕过工厂直接
`PgStorePort(dsn)` 构造才拿得到 PG 后端，而 `deploy/polardb.md` 讲的「部署仅换连接串、
代码一行都不用改」，说的正是上面这两行。
"""

from __future__ import annotations

import os
from typing import Any

from maos.core.store import SqliteStore as _CoreSqliteStore
from maos.store.pg_store import PgBackendUnavailable, PgStorePort
from maos.store.port import StorePort
from maos.store.sqlite_store import SqliteStorePort

__all__ = [
    "BACKEND_ENV",
    "DEFAULT_BACKEND",
    "POSTGRES",
    "SQLITE",
    "PgBackendUnavailable",
    "PgStorePort",
    "SqliteStorePort",
    "StorePort",
    "create_store",
]

#: 选后端的环境变量。
BACKEND_ENV = "MAOS_STORE_BACKEND"

SQLITE = "sqlite"
POSTGRES = "postgres"
DEFAULT_BACKEND = SQLITE


def create_store(store: Any | None = None, *, backend: str | None = None) -> StorePort:
    """按后端名造一个 StorePort。

    `backend` 显式传就用传的，否则读 `MAOS_STORE_BACKEND`，再否则 `sqlite`。

    sqlite 后端下 `store` 是**已有的**核心 `SqliteStore`（比如流程里那个）：适配器
    包住它，共用同一条连接与同一把锁。不传则现造一个 `:memory:` 的并建好核心表 ——
    只为测试和一次性脚本省事，真链路请把自己那个 store 传进来，否则你拿到的是一个
    谁也看不见的库。

    postgres 后端从 `MAOS_PG_DSN` 读连接串（铁律 6：密钥不落文件），**当场连一次库
    再返回** —— 交付的是一个已经连上的 `PgStorePort`，不是一个还不知道能不能用的壳。
    DSN 未配 / 驱动没装 / 连不上，一律抛 `PgBackendUnavailable`（`NotImplementedError`
    的子类，老调用方的 `except NotImplementedError` 照样接得住），**绝不回落 sqlite**：
    回落的话你会以为 PG 验过了，而实际上一行 PG 代码都没执行。

    为什么在工厂里就连、而不是留到首次真用才响：sqlite 分支同样是现造现建表，工厂的
    承诺一直是「给一个能用的后端」。更要紧的是，「选了 postgres」与「PG 真的在跑」之间
    每多一段距离，就多一段没人守的路 —— 而回落这类错误的全部危险就在于它没有症状。
    要一个不连库的壳做形状测试，直接 `PgStorePort()` 构造，别走这个工厂。

    `store` 参数只对 sqlite 后端有意义，postgres 下被忽略（PG 不包任何既有连接）。
    """
    name = (backend if backend is not None else os.environ.get(BACKEND_ENV) or DEFAULT_BACKEND)
    name = name.strip().lower()

    if name == SQLITE:
        if store is None:
            store = _CoreSqliteStore(":memory:")
            store.init_schema()
        return SqliteStorePort(store)

    if name == POSTGRES:
        port = PgStorePort()
        # 当场探活。不可用的三种情况（没驱动 / 没 DSN / 连不上）由 PgStorePort 自己
        # 抛 PgBackendUnavailable，报错里带着怎么修，且已过脱敏 —— 这里不再包一层，
        # 包了只会把那句话重复一遍，还多一个可能漏 DSN 的地方（铁律 6）。
        port.connect()
        return port

    raise ValueError(
        f"未知的 {BACKEND_ENV}={name!r}：只认 {SQLITE!r} 或 {POSTGRES!r}。"
        " 不回落缺省 —— 拼错一个字母就静默跑 sqlite，比报错难查得多。"
    )
