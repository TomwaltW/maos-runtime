"""受理期标注的落库口径 —— 模型判成了什么，以及要不要转人工。

**为什么模型的判断必须落库**：T101 / T102 把 CSV 里的硬词表匹配换成了模型理解，
而模型给出的是**观察与推断**，不是权威事实（铁律 8）。所以它必须留痕：判成了什么、
多有把握、凭什么、哪个模型、哪一次调用。有了 `intake_annotation` 这张表，
才谈得上「可回看、可人工推翻、可审计」。

可复现性因此从「输入 -> 结果」挪到「标注 -> 结果」：标注存下来了，同一份标注永远
推出同一个裁定；模型换了版本，也查得出当时那一版是怎么认的。

**本模块只提供，不调用**：写标注的是各个 skill，读标注的是受理岗与房间事实卡，
接线不在这里。这里只负责「存放标注的地方」和「要不要转人工的**唯一**定义」。

所有 SQL 从 `objects.execute` / `objects.query` 过（连同它那把 `lock_of` 锁），
不自己开连接 —— 理由见 `objects.py` 头部：退款域的新增表借的是 `SqliteStore` 那条
共享连接，绕开它那把锁就会把别人只写了一半的事务提交掉。
"""

from __future__ import annotations

from typing import Any

from . import objects

#: 模型判断低于这个把握度就进人工复核队列。
#:
#: **这是业务口径的初值，不是真理** —— 0.75 没有推导过程，是受理岗认下来的起点，
#: 该跟着误判率复盘往上或往下挪。挪它**只改这一处**：整仓所有「要不要转人工」的
#: 判断都走 `needs_human()`，不许在 skill 里、房间事实卡里各写一份阈值比较。
#: 两份阈值的症状不是报错，是受理岗说要人工、房间事实卡说不用，两边都不报错。
HUMAN_REVIEW_THRESHOLD = 0.75

#: 词表 / 别名命中的来源。这两种是确定性匹配，恒不转人工。
_DETERMINISTIC_SOURCES = ("lexicon", "alias")

#: 判不出来的取值。空串与 unknown 都是「模型没给出结论」，必转。
_UNDECIDED_VALUES = ("", "unknown")

_COLUMNS = (
    "tenant_id", "case_id", "field", "raw_text", "value",
    "confidence", "why", "source", "model", "invocation_id", "created_at",
)


def _require_invocation_id(invocation_id: str) -> str:
    """actor 溯源的唯一锚点，空了这条审计链就断了。

    与 `guard.py::_require_invocation_id` 同一个口径，故意重复而不是 import 过来：
    那边守的是 `refund_case` 的权威写入，这边守的是标注的观察来源，两条链各自成立。
    没有 actor 锚点的写入让审计链变假 —— 一行标注查不出是哪次调用产生的，
    「模型当时是怎么认的」就无从回答。
    """
    if not invocation_id:
        raise ValueError(
            "invocation_id 不许为空：它是每一条标注的 actor 锚点，"
            "也是主键的一部分（没有它，复检那一轮会盖掉上一轮的观察）"
        )
    return invocation_id


def record(
    store: Any,
    *,
    tenant_id: str,
    case_id: str,
    field: str,
    raw_text: str,
    value: str,
    confidence: float,
    why: str,
    source: str,
    model: str = "",
    invocation_id: str,
) -> dict:
    """落一条标注。返回落进去的那一行。

    `INSERT OR REPLACE` 而不是裸 `INSERT`：同一次 invocation 会被返工重跑
    （skill 失败重试、整条链路重放），重跑必须是幂等的而不是当场 IntegrityError。

    **覆盖只发生在同一个 invocation_id 之内**：主键带着它，所以复检那一轮是**新的一行**，
    上一轮的观察原样留着。这正是这张表存在的理由 —— 抹掉旧观察的话，
    「模型上一轮是怎么认的」就永远查不回来了。

    落库前做一次类型归一（`str()` / `float()`）：sqlite 的 REAL 取回来是 float，
    调用方递进来的可能是 int 或 str，不归一就会让返回的这一行与 `latest()` 取回的
    那一行逐字段比时出现「0.9 与 '0.9'」这种假冲突。
    """
    _require_invocation_id(invocation_id)
    row = {
        "tenant_id":     str(tenant_id),
        "case_id":       str(case_id),
        "field":         str(field),
        "raw_text":      str(raw_text),
        "value":         str(value),
        "confidence":    float(confidence),
        "why":           str(why),
        "source":        str(source),
        "model":         str(model),
        "invocation_id": str(invocation_id),
        "created_at":    objects._now(),
    }
    objects.execute(
        store,
        f"INSERT OR REPLACE INTO intake_annotation ({', '.join(_COLUMNS)})"
        f" VALUES ({', '.join('?' * len(_COLUMNS))})",
        tuple(row[c] for c in _COLUMNS),
    )
    return row


def latest(store: Any, *, tenant_id: str, case_id: str, field: str) -> dict | None:
    """这个字段**最新一次**标注。一条都没有返回 None。

    按 `created_at` 倒序取一条，不是按 `invocation_id` —— invocation_id 是标识不是
    时序，字典序大的那个未必是后发生的。
    """
    rows = objects.query(
        store,
        "SELECT * FROM intake_annotation WHERE tenant_id=? AND case_id=? AND field=?"
        " ORDER BY created_at DESC LIMIT 1",
        (tenant_id, case_id, field),
    )
    return rows[0] if rows else None


def list_for_case(store: Any, *, tenant_id: str, case_id: str) -> list[dict]:
    """本案全部标注，`created_at` 升序 —— 读出来就是这个案子被怎么一步步认下来的。

    `tenant_id` 进 WHERE 是形式，隔离靠的是它在主键里打头（本域各表一致）。
    """
    return objects.query(
        store,
        "SELECT * FROM intake_annotation WHERE tenant_id=? AND case_id=?"
        " ORDER BY created_at, field, invocation_id",
        (tenant_id, case_id),
    )


def needs_human(confidence: float, source: str, value: str = "") -> bool:
    """要不要转人工。**整仓唯一定义**，别处不许再写一份。

    判据，按这个顺序：

    · `source` 是 lexicon / alias  -> 不转。词表与别名是确定性匹配，没有把握度可言
      （它们恒 `confidence=1.0`，落到最后一条判也对，先判只是读着顺）。
    · `source` 是 fallback         -> 必转。没模型又没命中，这是猜的。
    · `value` 是 '' 或 unknown     -> 必转。模型自己说了判不出来。
    · `source` 是 model            -> 看把握度，低于 `HUMAN_REVIEW_THRESHOLD` 就转。
    · 其余未知 source              -> 必转。不认识的来源按最保守处理 ——
      新增一种来源时忘了在这里登记，代价应该是「多转几单人工」而不是「静默放行」。

    阈值本身在 `HUMAN_REVIEW_THRESHOLD`，调它只改那一处。
    """
    if source in _DETERMINISTIC_SOURCES:
        return False
    if source == "fallback":
        return True
    if value in _UNDECIDED_VALUES:
        return True
    if source == "model":
        return float(confidence) < HUMAN_REVIEW_THRESHOLD
    return True
