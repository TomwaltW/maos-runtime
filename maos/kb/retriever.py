"""两阶段检索 —— 阶段一结构化预过滤（硬约束），阶段二四通道混合召回（打分）。

## 两个阶段的性质完全不同，不许混谈

**阶段一是硬约束，不是打分项。** 过滤字段按评委给的顺序：

    tenant_id -> biz_type -> channel_id -> region -> sku -> policy_version -> workflow_version

其中 `tenant_id` 是**最硬的一条**：查询不带租户就返回空，带了就严格相等，
没有「相关度高所以放进来」这一说 —— 跨租户的知识被召回一次，这套系统就不能上生产。
其余维度采「文档侧为 NULL 视为通配」：一条不限渠道的政策 `channel_id IS NULL`，
对任何渠道的查询都该是候选；而查询侧不给某维度，就是不在这一维上收窄。

**阶段二才是打分。** 四个通道各自给 0..1 的信号，加权融合成一个分：

    规则编号精确 0.35 | 支付错误码精确 0.25 | 全文 BM25 0.20 | 语义向量 0.20

权重走 `MAOS_KB_WEIGHTS`（JSON）可配，读不懂就回落默认值并告警 —— 检索权重
配错的症状是「效果一般」，不是报错，所以宁可回落到一个已知的口径。

## 零模型

语义通道用的是**确定性 hash embedding**（字符 3-gram + 特征哈希），不是模型产出的向量。
理由不是省钱：`kb.retrieve` 被 Coding Agent 在产补丁**之前**调用，那条链路上多一次
模型调用会把场景 2 的调用序整体错位。真向量化模型接进来时，替换 `embed()` 一处即可，
调用方零改动。

## StorePort（F-2）

W-2 轨的 `maos/store/port.py` 落地后，`fts_search` / `vector_search` 由它提供
（SQLite 走 FTS5，PG 走 tsvector + pgvector）。这里按**能力探测**接：store 上有那两个
方法就用，没有就退化成本模块的纯 Python 实现。**不自己另起一套同名类** ——
合并后两个同名实现、行为不一致，是这个仓库反复踩过的坑。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import time
import weakref
from typing import Any

from maos import kb

log = logging.getLogger("maos.kb")

#: 阶段一的过滤顺序。**顺序即语义**：最左是 tenant_id，租户永远先收窄。
PREFILTER_FIELDS = (
    "tenant_id", "biz_type", "channel_id", "region", "sku",
    "policy_version", "workflow_version",
)

#: 阶段二四通道的默认权重（手册 Phase 5 表格）。
DEFAULT_WEIGHTS = {
    "rule_no": 0.35,
    "gateway_code": 0.25,
    "fts": 0.20,
    "vector": 0.20,
}

CHANNELS = tuple(DEFAULT_WEIGHTS)

DEFAULT_LIMIT = 5
#: hash embedding 的维度。够把几百条语料分开，且纯 Python 算得动。
EMBED_DIM = 64
#: 候选集上限 —— 预过滤之后仍然太多时截断，避免全表打分。
MAX_CANDIDATES = 500

#: 分词口径只有一份，在 `maos.kb` 里 —— 写入侧（FTS 影子表）与查询侧（MATCH 串、
#: hash embedding）共用它。各写一份的后果是召回恒为空，而日志一片正常。
tokenize = kb.tokenize


class RetrievalQuery(dict):
    """检索查询就是一个普通 dict，这个子类只为签名可读。

    键 = PREFILTER_FIELDS 的任意子集 + rule_no / gateway_code / keyword / tags。
    """


# ------------------------------------------------------------------ 权重
def load_weights(env: dict | None = None) -> dict[str, float]:
    """读 `MAOS_KB_WEIGHTS`（JSON）。任何读不通的情况都回落默认并告警，不抛。

    检索是锦上添花的一环，配置写错不该掀掉主链路；但也不能静默 —— 权重
    悄悄回落到默认值，与权重被悄悄清零，在效果上分不出来。
    """
    raw = (env if env is not None else os.environ).get(kb.KB_WEIGHTS_ENV)
    if not raw or not str(raw).strip():
        return dict(DEFAULT_WEIGHTS)
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("不是 JSON 对象")
        weights = dict(DEFAULT_WEIGHTS)
        for key, value in parsed.items():
            if key not in DEFAULT_WEIGHTS:
                raise ValueError(f"未知通道 {key!r}，可配的是 {CHANNELS}")
            weights[key] = float(value)
        return weights
    except (TypeError, ValueError) as exc:
        log.warning("%s=%r 解析不出权重（%s），回落默认值 %s",
                    kb.KB_WEIGHTS_ENV, raw, exc, DEFAULT_WEIGHTS)
        return dict(DEFAULT_WEIGHTS)


# ------------------------------------------------------------------ 向量
def embed(text: str, dim: int = EMBED_DIM) -> list[float]:
    """确定性 hash embedding —— 零模型、零依赖、跨进程稳定。

    用 sha256 而不是内置 `hash()`：后者带进程级随机种子，同一条语料在两个进程里
    会落到不同的桶，于是「连跑两次输出一致」当场作废，而症状是检索排序偶尔变一变。
    """
    vec = [0.0] * dim
    tokens = tokenize(text)
    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:4], "big") % dim
        sign = 1.0 if digest[4] & 1 else -1.0
        vec[bucket] += sign
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec] if norm else vec


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    # 两侧都已归一化，点积即余弦；截到 [0,1]：负相关对检索没有意义。
    return max(0.0, min(1.0, dot))


def _doc_vector(doc: dict) -> list[float]:
    """取文档向量。落库时存了就用存的，没存就现算 —— 现算与落库同一个函数。"""
    raw = doc.get("embedding")
    if raw:
        try:
            vec = json.loads(raw) if isinstance(raw, str) else list(raw)
            if isinstance(vec, list) and len(vec) == EMBED_DIM:
                return [float(x) for x in vec]
        except (TypeError, ValueError):
            log.warning("doc_id=%s 的 embedding 解析不了，现算一份顶上", doc.get("doc_id"))
    return embed(f"{doc.get('title', '')} {doc.get('body', '')}")


# ------------------------------------------------------ 阶段一：结构化预过滤
def prefilter(store: Any, query: dict, *, limit: int = MAX_CANDIDATES) -> list[dict]:
    """阶段一。返回候选集；`tenant_id` 缺失一律返回空。

    这是硬约束不是打分项 —— 本函数**不产生任何分数**，只决定谁有资格进入阶段二。
    """
    tenant_id = query.get("tenant_id")
    if not tenant_id:
        # 不给租户就没有候选集。回落成「全租户检索」是最危险的一种默认值：
        # 它在单租户的演示里看不出任何异常，多租户上线当天泄漏。
        log.warning("检索查询没有 tenant_id，按硬约束返回空候选集")
        return []
    if not kb.has_kb_table(store):
        return []

    where = ["tenant_id = ?"]
    params: list[Any] = [tenant_id]
    for field in PREFILTER_FIELDS[1:]:
        value = query.get(field)
        if value is None or value == "":
            continue
        # 文档侧 NULL = 通配（不限渠道的政策对任何渠道都算候选）。
        where.append(f"({field} IS NULL OR {field} = ?)")
        params.append(value)

    sql = f"SELECT * FROM kb_doc WHERE {' AND '.join(where)} ORDER BY created_at, doc_id LIMIT ?"
    return kb.query(store, sql, (*params, int(limit)))


# ------------------------------------------------------ 阶段二：四通道混合召回
#: 每个 store 上两条 StorePort 通道的可用性判定，探一次记一次。
#: 用 `WeakKeyDictionary` 而不是 `id(store)` 做键：id 会被回收后的新对象复用，
#: 那就成了把 A 的判定按在 B 头上，而症状是「换了个库检索忽然全走本地」。
_PORT_STATE: Any = weakref.WeakKeyDictionary()


def port_channel_state(store: Any) -> dict[str, bool]:
    """这个 store 上两条 StorePort 通道各自探过没有、通不通。只读，供自证与测试用。"""
    try:
        return dict(_PORT_STATE.get(store) or {})
    except TypeError:                                  # 不支持弱引用的 store
        return {}


def _port_search(store: Any, channel: str, args: tuple) -> list[tuple[str, float]] | None:
    """走 StorePort 的一条通道。返回 `None` = 这条通道走不通，调用方用本地实现。

    **「有这个方法」不等于「这条通道能用」。** F-2 约定源表主键列名固定为 `id`，
    而 `kb_doc` 的主键是 `(tenant_id, doc_id)`、影子表存的也是 `doc_id` ——
    真 `SqliteStorePort` 在本层这份 schema 上两条通道都抛 `LookupError`
    （`no such column: id`，本轨实测，见 BACKLOG `## task-X3`）。所以第一次调用
    兼作探测：抛了就**记住判定**并只告警一次。每次检索都抛一次再吞掉的写法，
    症状是日志被刷满而没人看得出这条通道其实一直没通。

    探测通过之后，端口返回的**空列表就是真的没命中**，不再回落本地实现 ——
    F-2 原话「『后端没准备好』不许伪装成『没命中』」，反过来同样成立：
    把「后端说没有」偷偷换成本地实现的结果，两条通道的口径就再也对不上了。
    """
    method = getattr(store, channel, None)
    if not callable(method):
        return None                                    # 能力探测不成立：没有这个方法
    try:
        state = _PORT_STATE.setdefault(store, {})
    except TypeError:                                  # 不支持弱引用，退化成每次都探
        state = {}
    if state.get(channel) is False:
        return None
    try:
        rows = [(str(d), float(s)) for d, s in (method(*args) or [])]
    except Exception as exc:                           # noqa: BLE001 —— 检索不阻塞
        if state.get(channel) is not False:
            log.warning(
                "StorePort.%s 在 %s 上走不通（%s），本次起这条通道退化为本模块的本地实现。"
                " 两层口径不一致不会报错，只会让召回悄悄变少，所以这条只告警一次并记住判定。",
                channel, type(store).__name__, exc)
        state[channel] = False
        return None
    state[channel] = True
    return rows


def _rank_normalize(scores: dict[str, float]) -> dict[str, float]:
    """把一批原始分按**名次**归一，不按分值。命中即正分。

    这条口径来自 W-2 的实测（BACKLOG `## task-W2` 第 2 条）：bm25 的 IDF 在
    「词出现在过半文档里」时塌到下限，同一批命中的原始分挤成一团；而 bm25 对
    弱相关文档确实会给出 `-rank <= 0` 的值。按**分值**归一时这两种情况都会把
    命中压成 0.0，`score_candidates` 又丢掉总分 <= 0 的文档 —— 于是「FTS 明明
    命中了」变成「一条都没召回」，而且不报错。库越小越容易触发，演示库正是小库。

    所以原始分只用来**排名次**：同分同名次同分数，第一名 1.0，往下按名次线性
    衰减，最低一档仍是正数。要设阈值就设在名次上，不设在分值上。
    """
    if not scores:
        return {}
    # 浮点噪声不该把「同分」拆成两个名次：先按 12 位小数归档再排名次。
    keyed = {d: round(float(v), 12) for d, v in scores.items()}
    tiers = sorted(set(keyed.values()), reverse=True)
    step = 1.0 / len(tiers)
    by_tier = {value: 1.0 - idx * step for idx, value in enumerate(tiers)}
    return {d: by_tier[v] for d, v in keyed.items()}


def _local_fts_rows(store: Any, tenant_id: str, keyword: str,
                    limit: int) -> list[tuple[str, float]]:
    """本地 FTS5 通道。返回 `(doc_id, 越大越相关)`，方向与 F-2 一致。

    影子表用的是缺省 unicode61，中文由写入侧的 `kb.fts_text()` 先切成单字 ——
    所以这里**没有** trigram 那条「<3 字符查询恒返回空集」的坑（W-2 的
    BACKLOG 第 1 条），两字词「退款」照常召回。要点在于查询侧必须走同一个
    `kb.tokenize`：换成把原文整串丢进 MATCH，中文那一段一个字都对不上。
    """
    match = " OR ".join(f'"{t}"' for t in kb.tokenize(keyword))
    if not match:
        return []
    try:
        rows = kb.query(
            store,
            "SELECT doc_id, bm25(kb_doc_fts) AS rank FROM kb_doc_fts"
            " WHERE kb_doc_fts MATCH ? AND tenant_id = ? ORDER BY rank LIMIT ?",
            (match, tenant_id, int(limit)))
    except Exception as exc:                           # noqa: BLE001 —— 检索不阻塞
        log.warning("本地 FTS5 检索失败（%s），本通道记 0 分", exc)
        return []
    return [(r["doc_id"], -float(r["rank"])) for r in rows]


def _fts_scores(store: Any, tenant_id: str, keyword: str,
                candidates: dict[str, dict], limit: int) -> dict[str, float]:
    """BM25 通道。优先走 StorePort.fts_search（F-2），走不通就用本地 FTS5。

    两条实现都只负责给出「谁比谁更相关」的次序，归一交给 `_rank_normalize` ——
    BM25 的绝对值随语料规模浮动，拿它跟另外三个通道直接相加是量纲错配。

    **租户收窄不靠这一层**：F-2 的 `fts_search` 没有租户参数，端口是全表查的；
    跨租户不召回由阶段一的候选集兜住（下面那句 `d in candidates`），
    这也正是「阶段一是硬约束而不是打分项」在实现上的样子。
    """
    if not keyword:
        return {}
    raw = _port_search(store, "fts_search", ("kb_doc", "body", keyword, int(limit)))
    if raw is None:
        raw = _local_fts_rows(store, tenant_id, keyword, limit)
    return _rank_normalize({d: s for d, s in raw if d in candidates})


def _vector_scores(store: Any, query_text: str,
                   candidates: dict[str, dict], limit: int) -> dict[str, float]:
    """语义通道。优先 StorePort.vector_search（pgvector），走不通就纯 Python 余弦。"""
    if not query_text:
        return {}
    vec = embed(query_text)
    raw = _port_search(store, "vector_search", ("kb_doc", "embedding", vec, int(limit)))
    if raw is not None:
        return {d: max(0.0, min(1.0, float(s))) for d, s in raw if d in candidates}
    return {doc_id: cosine(vec, _doc_vector(doc)) for doc_id, doc in candidates.items()}


def score_candidates(store: Any, query: dict, candidates: list[dict], *,
                     weights: dict[str, float] | None = None,
                     limit: int = DEFAULT_LIMIT) -> list[dict]:
    """阶段二：四通道打分 + 加权融合。返回按分降序的命中列表。

    只在**候选集内**打分：阶段一淘汰掉的文档在这里连出现的机会都没有，
    这是「过滤是硬约束」在实现上的样子。
    """
    weights = weights or load_weights()
    by_id = {d["doc_id"]: d for d in candidates}
    if not by_id:
        return []

    fan_out = max(int(limit) * 4, 20)
    keyword = str(query.get("keyword") or "").strip()
    semantic_text = keyword or " ".join(
        str(query.get(f) or "") for f in ("biz_type", "sku", "rule_no", "gateway_code"))

    signals: dict[str, dict[str, float]] = {
        "rule_no": {}, "gateway_code": {},
        "fts": _fts_scores(store, query["tenant_id"], keyword, by_id, fan_out),
        "vector": _vector_scores(store, semantic_text, by_id, fan_out),
    }
    for field in ("rule_no", "gateway_code"):
        wanted = query.get(field)
        if not wanted:
            continue
        # 精确匹配通道：命中即满分。它是「这条规则/这个错误码就是在说这件事」，
        # 没有中间态，给部分分等于把精确通道退化成又一个模糊通道。
        signals[field] = {doc_id: 1.0 for doc_id, doc in by_id.items()
                          if doc.get(field) and doc[field] == wanted}

    hits = []
    for doc_id, doc in by_id.items():
        per_channel = {ch: round(signals[ch].get(doc_id, 0.0), 6) for ch in CHANNELS}
        score = sum(weights.get(ch, 0.0) * per_channel[ch] for ch in CHANNELS)
        if score <= 0:
            continue                     # 四个通道都没信号 = 没被任何一条理由召回
        hits.append({
            "doc_id": doc_id,
            "score": round(score, 6),
            "title": doc.get("title", ""),
            "kind": doc.get("kind"),
            "outcome": doc.get("outcome"),
            "source_case_id": doc.get("source_case_id"),
            "channels": per_channel,
            "doc": doc,
        })
    # doc_id 参与排序：同分时的次序必须确定，否则「连跑两次输出一致」不成立。
    hits.sort(key=lambda h: (-h["score"], h["doc_id"]))
    return hits[:int(limit)] if limit and int(limit) > 0 else hits


# ------------------------------------------------------------------ 对外入口
def retrieve(store: Any, query: dict, *, limit: int = DEFAULT_LIMIT,
             weights: dict[str, float] | None = None,
             kinds: tuple[str, ...] | None = None) -> list[dict]:
    """两阶段检索的唯一入口。`store` 为 None、KB 关闭、无表、无候选 -> 一律返回空。

    **空结果不阻塞**：检索不到是冷启动的常态，不是故障。本函数不抛业务异常。
    """
    if store is None or not kb.kb_enabled():
        return []
    candidates = prefilter(store, query)
    if kinds:
        candidates = [c for c in candidates if c.get("kind") in kinds]
    if not candidates:
        return []
    return score_candidates(store, query, candidates, weights=weights, limit=limit)


def emit_kb_retrieved(store: Any, hits: list[dict], *, query: dict,
                      plan_id: str = "", task_id: str | None = None,
                      trace_id: str = "", duration_ms: float = 0.0,
                      candidate_count: int = 0) -> dict:
    """把这次检索落成一条 `KbRetrieved`（F-3 冻结形状）。返回落库的 detail。

    走现有 `append_event_log`，**不加新 Topic** —— 冻结的事件契约里没有这个类型，
    也不许为此去加。

    形状要点，三条都是消费侧写死的：
      · `detail["docs"]` 是数组，每项含 `doc_id` 与 `score`；核验器拿 doc_id
        回 `kb_doc` 表查得到才算数（查不到判据是「RAG 命中是编的」）；
      · `detail["duration_ms"]` 必须有 —— trace 把 KbRetrieved 归为有时长的事件，
        span 的 end 由 start + duration_ms 得出，缺了 span 会退化成零长。
        这里刻意留**亚毫秒精度**（float，三位小数）而不是取整：纯 Python 检索几百条
        语料常在 1ms 以内，`int()` 一律压成 0，trace 上的 span 照样是零长 ——
        字段在、值恒为 0，比缺字段更容易让人以为「时长记了」；
      · 命中为空也落。检索发生过本身就是事实，`docs: []` 是诚实的空，
        不落才会让「这次到底检没检」无从追溯。
    """
    detail = {
        "docs": [{"doc_id": h["doc_id"], "score": h["score"], "title": h.get("title", ""),
                  "kind": h.get("kind"), "channels": h.get("channels", {})}
                 for h in hits],
        "hit_count": len(hits),
        "candidate_count": int(candidate_count),
        "duration_ms": round(float(duration_ms), 3),
        "query": {k: v for k, v in query.items() if v not in (None, "")},
        "weights": weights_snapshot(),
    }
    if store is not None:
        store.append_event_log({
            "event_id": "",
            "trace_id": trace_id or "",
            "plan_id": plan_id or "",
            "task_id": task_id,
            "event_type": "KbRetrieved",
            "from_state": "",
            "to_state": "",
            "reason": "kb.retrieve",
            "detail": detail,
        })
    return detail


def weights_snapshot() -> dict[str, float]:
    """落进事件的权重快照 —— 事后要能回答「这次排序是按哪套权重算的」。"""
    return load_weights()


def retrieve_and_log(store: Any, query: dict, *, limit: int = DEFAULT_LIMIT,
                     plan_id: str = "", task_id: str | None = None,
                     trace_id: str = "", kinds: tuple[str, ...] | None = None
                     ) -> list[dict]:
    """检索 + 落 `KbRetrieved` 事件。KB 关闭时既不检索也不落事件。

    「关掉就一条事件都没有」是对照实验的判据之一：without_kb 那一跑的
    event_log 里不该有任何 KbRetrieved，否则「有无 RAG」这条线本身就不干净。
    """
    if store is None or not kb.kb_enabled():
        return []
    started = time.perf_counter()
    candidates = prefilter(store, query)
    if kinds:
        candidates = [c for c in candidates if c.get("kind") in kinds]
    hits = score_candidates(store, query, candidates, limit=limit) if candidates else []
    emit_kb_retrieved(store, hits, query=query, plan_id=plan_id, task_id=task_id,
                      trace_id=trace_id,
                      duration_ms=(time.perf_counter() - started) * 1000,
                      candidate_count=len(candidates))
    return hits
