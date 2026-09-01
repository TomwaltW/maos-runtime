"""按结构边界装填的**自描述截断** —— 送进模型的清单超预算时，让它知道自己少看了什么。

裸 ``json.dumps(items)[:N]`` 有两个毛病，而且两个都是静默的：

1. 切点落在 JSON 中间，模型收到的是语法破损的片段；
2. 模型**不知道自己被截了**，于是照常输出一份「看起来审过了」的结论 ——
   下游拿到的是一份基于残片、却自称完整的意见书。

所以这里按**元素**逐份装填、切在结构边界上（产出永远是合法 JSON 数组），
并把「被截了 / 原始有多大 / 少看到的是哪些 / 想看该怎么办」写成一段 note
附在载荷旁边，一起交给模型。

参照实现见 ``docs/refs/cumora-turn-loop.md`` §3 #1：工具输出超限时返回
``{truncated, originalBytes, head, note}``，note 里明写「想要被省掉的尾部就
换个更窄的查询」—— keep the output self-describing instead of silently slicing JSON。

放在 ``maos/agents/`` 下而不是某个 Agent 内部：这是个领域无关的装填器，
不认识 artifact、也不认识任何业务域，谁的清单都能装。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

#: 被省略元素在 note 里最多点名几条 —— note 本身也占提示词预算，不能无限长。
MAX_LISTED = 20


def _dump(obj: Any) -> str:
    """全仓统一的 JSON 文本口径。``default=str`` 兜住 datetime 之类的不可序列化值。"""
    return json.dumps(obj, ensure_ascii=False, default=str)


def _default_describe(item: Any) -> str:
    return _dump(item)[:60]


@dataclass(frozen=True)
class PackedItems:
    """装填结果。``payload`` **无论截没截断都是合法 JSON 数组文本**。"""

    payload: str
    presented: int          # 实际呈现给模型的份数
    total: int              # 原始份数
    original_chars: int     # 完整 JSON 的字符数
    truncated: bool
    note: str               # 没截断时是空串

    @property
    def omitted(self) -> int:
        return self.total - self.presented


def pack_json_array(
    items: Iterable[Any],
    *,
    budget: int,
    describe: Callable[[Any], str] | None = None,
    max_listed: int = MAX_LISTED,
) -> PackedItems:
    """把 ``items`` 装成一段不超过 ``budget`` 字符的**合法** JSON 数组文本。

    不触发截断时，``payload`` 与 ``json.dumps(items, ensure_ascii=False,
    default=str)`` **逐字节一致** —— 这条是刻意保的：绝大多数调用都落在这一支，
    截断改造不该顺手改掉它们送进模型的提示词。

    ``presented`` 可以是 0（第一份自己就超预算）。这种情况调用方要自己决定怎么办，
    本函数不替它兜底：装填器不知道「一份都没有」对调用方意味着什么。
    """
    rows: Sequence[Any] = list(items)
    describe = describe or _default_describe

    whole = _dump(rows)
    if len(whole) <= budget:
        return PackedItems(payload=whole, presented=len(rows), total=len(rows),
                           original_chars=len(whole), truncated=False, note="")

    parts: list[str] = []
    used = 2                                   # 首尾的 "[" 与 "]"
    for item in rows:
        chunk = _dump(item)
        cost = len(chunk) + (2 if parts else 0)   # 第 2 份起还要算 ", " 分隔符
        if used + cost > budget:
            break
        used += cost
        parts.append(chunk)

    payload = "[" + ", ".join(parts) + "]"
    return PackedItems(
        payload=payload, presented=len(parts), total=len(rows),
        original_chars=len(whole), truncated=True,
        note=_build_note(rows, len(parts), len(whole), budget, describe, max_listed),
    )


def _build_note(rows, presented, original_chars, budget, describe, max_listed) -> str:
    """截断说明。三件事一件都不能少：被截了 / 原始有多大 / 少看到的是什么。"""
    omitted = rows[presented:]
    listed = [describe(item) for item in omitted[:max_listed]]
    tail = "" if len(omitted) <= max_listed else f"，……另有 {len(omitted) - max_listed} 份未点名"
    return (
        f"⚠️ 上面这份清单被截断了：共 {len(rows)} 份，本次只呈现了前 {presented} 份，"
        f"省略 {len(omitted)} 份（完整清单 {original_chars} 字符，超出本次 {budget} 字符预算）。\n"
        f"被省略的是：{'；'.join(listed)}{tail}\n"
        "你**没有看过**被省略的那些 —— 结论里不要替它们下判断，也不要把本次结论"
        "说成是对全部内容的结论；需要看它们，请分批送审（一次送更少的份数，"
        "或按 task_id 缩小范围）。"
    )
