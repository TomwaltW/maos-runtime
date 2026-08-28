"""StorePort —— v4 手册 P1 第 7 步的存储抽象，手册原话「整个 v4 的地基」。

下面五个方法的签名逐字照抄 `docs/EXECUTION.md` 第 197-217 行，**落地即冻结（契约
F-2）**：不许多、不许少、不许改名、不许改参数顺序。W-3 轨的检索器正照它写全文与
向量两条通道，改一个字对面当场散架。要动先停下来找人类，不要自行调整。

`Protocol` 是结构化子类型：实现方不必 import 本模块、不必继承任何东西，方法对得上
就是 StorePort。标了 `@runtime_checkable`，`isinstance` 可用 —— 但它**只查方法名
在不在，不查签名**，别拿它当类型校验使。

## 后端中立的口径（两个实现都必须守，PG 到 P5 填实时照这条对）

`fts_search` / `vector_search` 都返回 `list[tuple[str, float]]`：

- 第一位是**源表主键，列名固定为 `id`**，取出来一律转成 `str`；
- 第二位是分数，**一律「越大越相关」**，返回列表按分数降序、同分按 id 升序。
  SQLite 侧的 bm25 原值是负数（越负越相关），适配器已经取过负号 —— 符号只在
  适配器里翻一次，不留给每个调用方各自记一遍，那种约定早晚有人记反。
- 命不中就是空列表；**「后端没准备好」不许伪装成「没命中」**，那是两件事，
  后者会让检索看起来在工作而召回恒空。

`execute` / `query` 的 `params` 一律走占位符绑定，**任何实现都不许把参数拼进 SQL**。
表名 / 列名没法用占位符，只能拼 —— 拼之前必须校验形状（见 sqlite 适配器 `_ident`）。

`params` 没有缺省值，是有意的：写成 `params=()` 之后调用方会养成 `execute(sql)` 的
习惯，而 F-2 里没这条承诺，PG 后端没义务兼容它。要省那两个字符，代价是换后端那天
一片调用点跟着改。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class StorePort(Protocol):
    def execute(self, sql: str, params: tuple) -> None: ...
    def query(self, sql: str, params: tuple) -> list[dict]: ...
    def fts_search(self, table: str, field: str, q: str, limit: int) -> list[tuple[str, float]]: ...
    def vector_search(self, table: str, field: str, vec: list[float], limit: int) -> list[tuple[str, float]]: ...
    def dialect(self) -> str: ...   # "sqlite" | "postgres"
