"""maos.store —— 存储后端的可插拔面（v4 手册 P1 第 7 步，补的是历史欠账）。

对外四样东西：

    StorePort        五方法契约（F-2，落地即冻结）
    create_store()   按 MAOS_STORE_BACKEND 选后端，缺省 sqlite
    SqliteStorePort  包住核心 store.py 的适配器（组合，不是继承）
    PgStorePort      空壳，真实现归 P5

**本包是纯新增**：现有链路一个调用方都没改，缺省路径的行为与没有本包时逐字节一致，
全量测试与 `python3 run.py` 是唯一判据。谁要接进主链路，接的那一轨自己验。

后端名只认 `sqlite` 与 `postgres` 两个字面量（大小写不敏感，前后空白忽略）。别的
一律抛 ValueError，**不回落缺省** —— 环境变量拼错一个字母就静默跑 sqlite，比直接
报错难查一个量级：你会以为在验 PG，其实一行 PG 代码都没执行。
"""

from __future__ import annotations

import os
from typing import Any

from maos.core.store import SqliteStore as _CoreSqliteStore
from maos.store.pg_store import PgStorePort
from maos.store.port import StorePort
from maos.store.sqlite_store import SqliteStorePort

__all__ = [
    "BACKEND_ENV",
    "DEFAULT_BACKEND",
    "POSTGRES",
    "SQLITE",
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

    postgres 后端一律抛 `NotImplementedError`（P5 才填），**绝不回落 sqlite**。
    P5 开发期要拿空壳做形状测试，直接 `PgStorePort()` 构造，别走这个工厂。
    """
    name = (backend if backend is not None else os.environ.get(BACKEND_ENV) or DEFAULT_BACKEND)
    name = name.strip().lower()

    if name == SQLITE:
        if store is None:
            store = _CoreSqliteStore(":memory:")
            store.init_schema()
        return SqliteStorePort(store)

    if name == POSTGRES:
        raise NotImplementedError(
            f"{BACKEND_ENV}={POSTGRES}：PG 后端在本 Phase 只有空壳，"
            " 全文（tsvector）与向量（pgvector）的真实现归 P5。"
            " 这里显式抛错而不是回落 sqlite —— 回落的话你会以为 PG 验过了，"
            " 而实际上一行 PG 代码都没执行。要跑请用"
            f" {BACKEND_ENV}={SQLITE}（缺省值）。"
        )

    raise ValueError(
        f"未知的 {BACKEND_ENV}={name!r}：只认 {SQLITE!r} 或 {POSTGRES!r}。"
        " 不回落缺省 —— 拼错一个字母就静默跑 sqlite，比报错难查得多。"
    )
