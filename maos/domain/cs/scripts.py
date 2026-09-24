"""话术检索（p12 契约 §1.4 T168）：客户说的一句话 → 按分数排好的几篇标准话术。

两段，前一段不是本模块自己的：

1. **召回走知识层的两阶段检索**（``maos.kb.retriever.retrieve``）：租户硬约束、
   ``biz_type='cs'``、``kind='cs_script'``，关键词就是客户原文。不另写一条直查
   ``kb_doc`` 的 SQL —— 绕开 ``retrieve`` 就绕开了租户那条硬约束，而那不报错。
2. **重排是本模块的**：对召回的每篇话术，拿客户原文与该篇的「适用场景 + 同义词 + 例句」
   逐条算字符二元组重合度，见 :func:`script_score`。知识层的四通道分数是为退款规划调的
   （规则编号、错误码两条精确通道在这里恒为 0），直接拿来当命中门槛会把门槛定在噪声上。

**零模型**（契约 §0）：重排是纯函数，同一句话、同一份话术库，分数逐位相同。

**客户原文不进审计行**（契约 R5）：落 ``KbRetrieved`` 时 ``detail.query.keyword``
换成 ``types.text_digest(text)``；日志里也不打原文。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from maos import kb
from maos.domain.cs import types as T
from maos.kb import retriever

log = logging.getLogger("maos.cs")

#: 命中门槛，[0, 1]。``hits[0].score >= MIN_SCRIPT_SCORE`` 才算「找得到话术」，
#: 否则前台走兜底（契约 §1.4 判定顺序第 4、5 步）。
#:
#: 取值依据（task-t168 实测，``scenarios/cs/kb/cs_scripts_holdout.json``，先于门槛写成）：
#: 18 条与售后无关的句子 + 4 条英文售后问题，最高分 0.194（「给我讲个笑话吧」）；
#: 57 条改写说法 top-1 正确 55 条（0.965），其中分数 ≥ 0.25 的 49 条（0.86），
#: ≥ 0.20 的 51 条。取 0.25：比无关句最高分高出约 0.05 的余量 —— 门槛贴着无关句定，
#: 一句没见过的闲聊就可能被答成某篇话术；而说法没覆盖到的改写落到兜底，
#: 代价只是多一轮追问（连续兜底由前台转人工）。
MIN_SCRIPT_SCORE = 0.25

#: 重排分数里两项的权重：最像的那一条说法（Dice）与整篇说法对客户原文的覆盖率。
#: 只用前者，短问句会被一条同样短的例句带跑；只用后者，长句里几个常见二元组就能凑出分。
_BEST_WEIGHT = 0.5
_COVER_WEIGHT = 0.5


def _grams(text: str) -> frozenset[str]:
    """字符二元组。先过 ``kb.tokenize``（英数按词、中文按字，丢标点与空白、转小写）。

    规整后只剩一个字时给那一个字：二元组集合为空会让它与任何话术都算不出分，
    而一个字（「嗨」）与另一个字的整句相同也确实该算重合。
    """
    s = "".join(kb.tokenize(text))
    if len(s) < 2:
        return frozenset({s}) if s else frozenset()
    return frozenset(s[i:i + 2] for i in range(len(s) - 1))


def _dice(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return 2.0 * len(a & b) / (len(a) + len(b))


def _variants(body: dict) -> list[str]:
    """一篇话术用来比对的全部说法：适用场景 + 同义词 + 例句。标准话术本身不算 ——
    它是我们要说的话，不是客户会怎么问。"""
    out = [str(body.get("scene") or "")]
    out.extend(str(s) for s in body.get("synonyms") or ())
    out.extend(str(s) for s in body.get("examples") or ())
    return [v for v in out if v]


def script_score(text: str, body: dict) -> float:
    """客户原文对一篇话术的重排分，[0, 1]，六位小数。

    ``0.5 × max(Dice(原文, 每条说法)) + 0.5 × (原文的二元组里落在该篇全部说法中的比例)``。
    """
    query = _grams(text)
    if not query:
        return 0.0
    variant_grams = [_grams(v) for v in _variants(body)]
    if not variant_grams:
        return 0.0
    best = max(_dice(query, g) for g in variant_grams)
    union = frozenset().union(*variant_grams)
    cover = len(query & union) / len(query)
    return round(_BEST_WEIGHT * best + _COVER_WEIGHT * cover, 6)


def _usable_body(doc: dict) -> dict | None:
    """取一篇话术的 body；形状不对的返回 None（跳过并告警，不让一篇坏话术拖垮整轮）。"""
    try:
        body = json.loads(doc.get("body") or "")
    except (TypeError, ValueError):
        body = None
    if not isinstance(body, dict):
        log.warning("话术 %s 的 body 不是 JSON 对象，跳过", doc.get("doc_id"))
        return None
    intent = body.get("intent")
    handoff = body.get("handoff") or ""
    if intent not in T.INTENTS or (handoff and handoff not in T.HANDOFF_REASONS) \
            or not str(body.get("script") or "").strip():
        log.warning("话术 %s 的 intent / handoff / script 不合契约，跳过", doc.get("doc_id"))
        return None
    return body


def match_scripts(store: Any, *, tenant_id: str, text: str, plan_id: str, task_id: str,
                  limit: int = 3) -> list[T.ScriptHit]:
    """检索话术，按 score 降序返回至多 ``limit`` 篇（重排分为 0 的不返回）。

    * 只检 ``kind='cs_script'`` 且 ``biz_type='cs'`` 的文档：查询带 ``biz_type='cs'``
      过阶段一，召回后再逐篇核一次 ``biz_type`` —— 阶段一把文档侧 NULL 当通配，
      一篇漏写 biz_type 的话术会混进来，而话术库的约定是 biz_type 恒非空。
    * KB 关着（``MAOS_KB_ENABLED=0``）或没有 store：返回 ``[]``，**一条事件都不落**
      （口径同 ``retriever.retrieve_and_log``：关掉就是没检过）。
    * 否则**恰好落一条** ``KbRetrieved``：plan_id / task_id 照传、trace_id 恒空串
      （契约 §1.2）；``detail.docs`` 就是返回的这几篇，分数是重排分（与返回值逐篇相等）；
      ``detail.query.keyword`` 是 ``text_digest(text)``，客户原文不进 event_log。
      检不到也落（``docs: []``）—— 检索发生过本身就是事实。
    * ``detail.candidate_count`` 记的是知识层召回、进入重排的篇数。
    """
    if store is None or not kb.kb_enabled():
        return []
    started = time.perf_counter()
    query = {"tenant_id": tenant_id, "biz_type": T.BIZ_TYPE_CS, "keyword": text}
    # limit 放到候选集上限：重排要看到全部召回，知识层的截断只按它自己的分数排。
    recalled = retriever.retrieve(store, query, limit=retriever.MAX_CANDIDATES,
                                  kinds=(T.CS_KB_KIND,))

    scored: list[tuple[float, float, str, dict, dict]] = []
    for hit in recalled:
        doc = hit.get("doc") or {}
        if doc.get("biz_type") != T.BIZ_TYPE_CS:
            continue
        body = _usable_body(doc)
        if body is None:
            continue
        score = script_score(text, body)
        if score <= 0:
            continue
        scored.append((score, float(hit.get("score") or 0.0), hit["doc_id"], hit, body))
    # 同分先看知识层的分，再看 doc_id：次序必须确定（「连跑两次输出一致」）。
    scored.sort(key=lambda s: (-s[0], -s[1], s[2]))
    top = scored[:max(0, int(limit))]

    hits = [
        T.ScriptHit(
            doc_id=doc_id,
            scheme_no=str(body.get("scheme_no") or hit["doc"].get("rule_no") or ""),
            intent=str(body["intent"]),
            score=score,
            script=str(body["script"]),
            principle=str(body.get("principle") or ""),
            handoff=str(body.get("handoff") or ""),
        )
        for score, _kb_score, doc_id, hit, body in top
    ]
    retriever.emit_kb_retrieved(
        store,
        [{"doc_id": h.doc_id, "score": h.score, "title": hit.get("title", ""),
          "kind": hit.get("kind"), "channels": hit.get("channels", {})}
         for h, (_s, _k, _d, hit, _b) in zip(hits, top)],
        query={**query, "keyword": T.text_digest(text)},
        plan_id=plan_id, task_id=task_id, trace_id="",
        duration_ms=(time.perf_counter() - started) * 1000,
        candidate_count=len(recalled),
    )
    return hits
