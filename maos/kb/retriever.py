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
def _fts_scores(store: Any, tenant_id: str, keyword: str,
                candidates: dict[str, dict], limit: int) -> dict[str, float]:
    """BM25 通道。优先走 StorePort.fts_search（F-2），没有就用本地 FTS5。

    归一化到 0..1：BM25 是「越小越相关」的负值，绝对值大小又随语料规模浮动，
    直接加权会让这一通道的量纲压过另外三个。按本次结果集的最大值归一 ——
    比较的是「本次候选里谁更相关」，这正是融合排序需要的语义。
    """
    if not keyword:
        return {}

    raw: list[tuple[str, float]] = []
    port_search = getattr(store, "fts_search", None)
    if callable(port_search):
        try:
            raw = list(port_search("kb_doc", "body", keyword, limit) or [])
        except Exception as exc:                       # noqa: BLE001 —— 检索不阻塞
            log.warning("StorePort.fts_search 不可用（%s），退化本地 FTS5", exc)
            raw = []
    if not raw:
        # 查询串走与写入侧同一个分词函数，再把 token 用 OR 连起来。
        # 直接把原文丢进 MATCH 会被 FTS5 当成短语查询，中文那一段一个字都对不上。
        match = " OR ".join(f'"{t}"' for t in kb.tokenize(keyword))
        if not match:
            return {}
        try:
            rows = kb.query(
                store,
                "SELECT doc_id, bm25(kb_doc_fts) AS rank FROM kb_doc_fts"
                " WHERE kb_doc_fts MATCH ? AND tenant_id = ? ORDER BY rank LIMIT ?",
                (match, tenant_id, int(limit)))
        except Exception as exc:                       # noqa: BLE001 —— 检索不阻塞
            log.warning("本地 FTS5 检索失败（%s），本通道记 0 分", exc)
            return {}
        raw = [(r["doc_id"], -float(r["rank"])) for r in rows]

    scores = {d: s for d, s in raw if d in candidates}
    if not scores:
        return {}
    top = max(scores.values())
    if top <= 0:
        return {d: 0.0 for d in scores}
    return {d: max(0.0, s) / top for d, s in scores.items()}


def _vector_scores(store: Any, query_text: str,
                   candidates: dict[str, dict], limit: int) -> dict[str, float]:
    """语义通道。优先 StorePort.vector_search（pgvector），没有就纯 Python 余弦。"""
    if not query_text:
        return {}
    vec = embed(query_text)

    port_search = getattr(store, "vector_search", None)
    if callable(port_search):
        try:
            raw = list(port_search("kb_doc", "embedding", vec, limit) or [])
            if raw:
                return {d: max(0.0, min(1.0, float(s))) for d, s in raw if d in candidates}
        except Exception as exc:                       # noqa: BLE001 —— 检索不阻塞
            log.warning("StorePort.vector_search 不可用（%s），退化纯 Python 余弦", exc)
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
