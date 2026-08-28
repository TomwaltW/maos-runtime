"""StorePort 的 PostgreSQL 后端 —— 本 Phase 只有空壳。

手册 P1 第 7 步原文：「本 Phase 只写空壳 + NotImplementedError，P5 再填。留位置
就行。」所以这里**故意什么都没实现**：真实现（tsvector 走全文、pgvector 走向量）
归 P5，提前做等于在没有验收命令的情况下写一份没人跑过的代码。

**为什么空壳也要显式抛错、一个字都不许回落 sqlite**：静默回落的症状是「PG 后端
看起来跑通了」，而实际上一行 PG 代码都没执行 —— 等到真接 PG 那天，所有以为验过的
路径都得从头再验一遍，且没有任何东西提示你该重验。宁可现在响。

连接串只从环境变量 `MAOS_PG_DSN` 读（铁律 6：密钥不落文件）。DSN 里通常带口令，
所以 `__repr__` 只报「配了 / 没配」，不回显内容 —— 免得它顺着某份 traceback 或
某个 evidence 文件漏出去。
"""

from __future__ import annotations

import os
from typing import Any

#: `dialect()` 的返回值，F-2 只认 "sqlite" | "postgres" 两个字面量。
DIALECT = "postgres"

#: 连接串的唯一来源。禁止写进任何文件，禁止出现在 evidence/ 里。
DSN_ENV = "MAOS_PG_DSN"

_TODO = (
    "PG 后端归 P5：全文走 tsvector、向量走 pgvector，本 Phase 只留空壳。"
    " 需要现在跑通请用 MAOS_STORE_BACKEND=sqlite（缺省值）。"
)


class PgStorePort:
    """空壳。五个方法的形状照 F-2 摆好，P5 往里填实现。"""

    def __init__(self, dsn: str | None = None) -> None:
        self.dsn = dsn if dsn is not None else os.environ.get(DSN_ENV, "")

    def __repr__(self) -> str:
        # 只报有没有，不报是什么 —— DSN 里通常带口令。
        return f"PgStorePort(dsn={'<已配置>' if self.dsn else '<未配置>'})"

    # -- StorePort 五方法（F-2 冻结签名）---------------------------------------
    def execute(self, sql: str, params: tuple) -> None:
        raise NotImplementedError(_TODO)

    def query(self, sql: str, params: tuple) -> list[dict]:
        raise NotImplementedError(_TODO)

    def fts_search(self, table: str, field: str, q: str, limit: int) -> list[tuple[str, float]]:
        raise NotImplementedError(f"{_TODO} 全文这条到时用 to_tsvector / ts_rank。")

    def vector_search(
        self, table: str, field: str, vec: list[float], limit: int
    ) -> list[tuple[str, float]]:
        raise NotImplementedError(f"{_TODO} 向量这条到时用 pgvector 的 <=> 算子。")

    def dialect(self) -> str:
        # 这一个可以答：方言是静态事实，不是「还没实现的操作」。检索器要按方言
        # 分支时（PG 走 tsvector、SQLite 走 FTS5），至少得先问得出自己在哪边。
        return DIALECT

    def connect(self) -> Any:
        raise NotImplementedError(
            f"{_TODO} 连接串读环境变量 {DSN_ENV}（当前{'已' if self.dsn else '未'}配置）。"
        )
