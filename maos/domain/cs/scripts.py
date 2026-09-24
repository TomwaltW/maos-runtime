"""话术检索（p12 契约 §1.4 T168）：客户说的一句话 → 按分数排好的几篇标准话术。

两段，前一段不是本模块自己的：

1. **召回走知识层的两阶段检索**（``maos.kb.retriever.retrieve``）：租户硬约束、
   ``biz_type='cs'``、``kind='cs_script'``，关键词就是客户原文。不另写一条直查
   ``kb_doc`` 的 SQL —— 绕开 ``retrieve`` 就绕开了租户那条硬约束，而那不报错。
   四通道权重点名给（``_RECALL_WEIGHTS``），不读退款侧调的 ``MAOS_KB_WEIGHTS``。
2. **重排是本模块的**：对召回的每篇话术，拿客户原文与该篇的「适用场景 + 同义词 + 例句」
   逐条算字符二元组重合度，见 :func:`script_score`。知识层的四通道分数是为退款规划调的
   （规则编号、错误码两条精确通道在这里恒为 0），直接拿来当命中门槛会把门槛定在噪声上。

**短句与虚词**（复核 L2-1）：「没有了」「可以」「这个多少钱」这类短句只有一两个二元组，
其中一个常见二元组（什么 / 怎么 / 可以 / 没有 / 多少）碰巧落在某篇例句里，未加权的覆盖率
就是 0.5–1.0，一句闲聊就被答成一篇话术。所以重排分对二元组分了轻重（虚词二元组几乎不计），
短句的覆盖率有分母下限，实词二元组只对上一个时整体打对折 —— 一个二元组碰上算巧合，两个才算证据。

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
#: 取值依据（task-t168 复核轮实测，``scenarios/cs/kb/cs_scripts_holdout.json``；
#: 复核轮补进去的短对话轮与近域句先于本轮调参写成）：无关句 79 条（远域 18 + 短对话轮 /
#: 售前 / 账号 / 夸奖等近域 61）+ 英文售后 4 条，最高分 0.208（「好的」→ GEN-002），
#: 其次 0.200（「这个多少钱」→ LOG-003）；76 条改写（每篇 3 条首版 + 1 条短口语）
#: top-1 正确 74 条（0.974），其中分数 ≥ 0.25 的 63 条（0.829）；首版那 57 条是 56 / 49。
#: 取 0.25：比无关句最高分高约 0.04 的余量 —— 门槛贴着无关句定，一句没见过的闲聊就可能
#: 被答成某篇话术；说法没覆盖到的改写落到兜底，代价只是多一轮追问（连续兜底由前台转人工）。
#: 首版的重排口径（不分虚实、无分母下限、无对折）在同一份话术库与无关句上有 24 条 ≥ 0.25
#: （最高 0.75），见 docs/DECISIONS.md。
MIN_SCRIPT_SCORE = 0.25

#: 重排分数里两项的权重：最像的那一条说法（加权 Dice）与整篇说法对客户原文的加权覆盖率。
#: 只用前者，短问句会被一条同样短的例句带跑；只用后者，长句里几个常见二元组就能凑出分。
_BEST_WEIGHT = 0.5
_COVER_WEIGHT = 0.5

#: 虚词字表。**两个字都在表里**的二元组是虚词二元组（什么 / 怎么 / 可以 / 没有 / 多少 /
#: 这个 / 知道 ……），权重 ``_FUNCTION_WEIGHT``；其余二元组（只要有一个字不在表里，如
#: 「包邮」「退款」「多久」「你好」）是实词二元组，权重 1。表按字、不按词：闭集，
#: 不随话术库增长。「好」不在表里 —— 它是「你好 / 您好」的实词，放进去问候就检不到了。
_FUNCTION_CHARS = frozenset(
    "我你您他她它们咱这那哪谁啥此"                          # 代词
    "什么怎咋为几多少"                                      # 疑问
    "吗呢吧啊呀嘛哦哈啦拉呗哇喔嗯噢哎诶欸呐哟亲"            # 语气、称呼
    "的地得了着过是有没不也都还就才又再很太挺真最更在给把被让和跟与或及"  # 助词、副词、介词
    "要想会能可以该请一个下些点儿里上边样"                  # 助动词、量词、方位
    "做弄搞办说问看知道行对来去东西时候般然后现"            # 泛义动词、泛指
)
_FUNCTION_WEIGHT = 0.1

#: 覆盖率的分母下限（按二元组权重计）。客户原文的权重和不足它时按它算 ——
#: 一两个二元组的短句不能靠「我仅有的那个二元组对上了」拿满覆盖率。
_MIN_QUERY_MASS = 3.0

#: 实词二元组要对上几个才给满分；只对上一个时整体按比例打折（1 / 2），一个都没对上为 0。
_MIN_CONTENT_HITS = 2

#: 句尾语气词。客户原文与某条说法**去掉句尾语气词后逐字相等**（且至少剩两个字）也算
#: 原样命中：「你好啊」就是登记过的「你好」，「在吗亲」就是「在吗」。只用于这一条判定，
#: 不参与二元组打分；去到只剩一个字就停（「在呢」与「在吗」都去成「在」纯属巧合），见 :func:`tail_forms`。
_TAIL_PARTICLES = frozenset("啊呀吗嘛呢吧哦哈啦拉呗哇亲么噢喔嗯哟呐")

#: 召回用的四通道权重，**点名给、不读配置**（复核 L2-A）。``retrieve`` 缺省读
#: ``MAOS_KB_WEIGHTS``（受治理的配置项，env 或 Nacos），那一套是为退款规划调的；
#: 而阶段二丢掉加权和 <= 0 的文档 —— 退款侧把 fts 调成 0，话术就整篇召不回来，
#: 重排连看都看不到，本该满分命中的一句变成兜底。所以这里的召回与那个旋钮脱钩：
#: 两条精确通道对话术恒无信号，权重 0；全文通道按字召回（``kb.tokenize`` 中文按字切），
#: 客户原文与一篇话术只要共享一个汉字就进候选，而重排分 > 0 至少要共享一个二元组 ——
#: 话术库的说法里没有英数字（task-t168 复核轮实查 296 条），共享的二元组必含汉字，
#: 于是重排能打出分的每一篇都召得回来。向量通道也给 1：它的分只作重排同分时的次序。
#: ``KbRetrieved.detail.weights`` 仍是 ``retriever.weights_snapshot()`` 读到的配置值
#: （``emit_kb_retrieved`` 不收权重参数，知识层不归本轨改），见 docs/DECISIONS.md。
_RECALL_WEIGHTS = {"rule_no": 0.0, "gateway_code": 0.0, "fts": 1.0, "vector": 1.0}


def _grams(text: str) -> frozenset[str]:
    """字符二元组。先过 ``kb.tokenize``（英数按词、中文按字，丢标点与空白、转小写）。

    规整后只剩一个字时给那一个字：二元组集合为空会让它与任何话术都算不出分，
    而一个字（「嗨」）与另一个字的整句相同也确实该算重合。
    """
    s = "".join(kb.tokenize(text))
    if len(s) < 2:
        return frozenset({s}) if s else frozenset()
    return frozenset(s[i:i + 2] for i in range(len(s) - 1))


def _norm(text: str) -> str:
    return "".join(kb.tokenize(text))


def tail_forms(text: str) -> frozenset[str]:
    """规整后的原句，加上逐个去掉句尾语气词得到的各个形式，去到只剩两个字为止。

    「包邮吗亲」→ {包邮吗亲, 包邮吗, 包邮}；「在吗亲」→ {在吗亲, 在吗}（「在」只剩一个字，不要）；
    「在吗」→ {在吗}。两句的形式有交集，就算同一种说法。生成器拿它查跨篇撞车。
    """
    s = _norm(text)
    forms = {s} if s else set()
    while len(s) > 2 and s[-1] in _TAIL_PARTICLES:
        s = s[:-1]
        forms.add(s)
    return frozenset(forms)


def _is_content(gram: str) -> bool:
    """实词二元组：至少一个字不在虚词字表里。单字（整句只剩一个字时）按那个字判。"""
    return any(ch not in _FUNCTION_CHARS for ch in gram)


def _mass(grams: frozenset[str]) -> float:
    return sum(1.0 if _is_content(g) else _FUNCTION_WEIGHT for g in grams)


def _dice(a: frozenset[str], b: frozenset[str]) -> float:
    """加权 Dice：2 × 交集权重 / (两边权重和)。"""
    inter = a & b
    if not inter:
        return 0.0
    return 2.0 * _mass(inter) / (_mass(a) + _mass(b))


def _variants(body: dict) -> list[str]:
    """一篇话术用来比对的全部说法：适用场景 + 同义词 + 例句。标准话术本身不算 ——
    它是我们要说的话，不是客户会怎么问。"""
    out = [str(body.get("scene") or "")]
    out.extend(str(s) for s in body.get("synonyms") or ())
    out.extend(str(s) for s in body.get("examples") or ())
    return [v for v in out if v]


def script_score(text: str, body: dict) -> float:
    """客户原文对一篇话术的重排分，[0, 1]，六位小数。

    * 原文就是该篇登记过的一种说法（:func:`tail_forms` 有交集：规整后逐字相等，
      或两边去掉句尾语气词后逐字相等且至少两个字）：1。
    * 否则 ``(0.5 × best + 0.5 × cover) × evidence``，二元组按 :func:`_mass` 加权
      （实词 1、虚词 ``_FUNCTION_WEIGHT``）：

      - ``best``：与最像的那一条说法的加权 Dice；
      - ``cover``：原文二元组落在该篇全部说法里的权重 ÷ max(原文权重和, ``_MIN_QUERY_MASS``)；
      - ``evidence``：落在该篇里的实词二元组个数 ÷ ``_MIN_CONTENT_HITS``，封顶 1。
    """
    query = _grams(text)
    if not query:
        return 0.0
    variants = _variants(body)
    if not variants:
        return 0.0
    forms = tail_forms(text)
    if any(forms & tail_forms(v) for v in variants):
        return 1.0
    variant_grams = [_grams(v) for v in variants]
    best = max(_dice(query, g) for g in variant_grams)
    matched = query & frozenset().union(*variant_grams)
    cover = _mass(matched) / max(_mass(query), _MIN_QUERY_MASS)
    evidence = min(1.0, sum(1 for g in matched if _is_content(g)) / _MIN_CONTENT_HITS)
    return round((_BEST_WEIGHT * best + _COVER_WEIGHT * cover) * evidence, 6)


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
    # 权重点名给：召回不随退款侧的 MAOS_KB_WEIGHTS 漂（见 _RECALL_WEIGHTS）。
    recalled = retriever.retrieve(store, query, limit=retriever.MAX_CANDIDATES,
                                  weights=dict(_RECALL_WEIGHTS), kinds=(T.CS_KB_KIND,))

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
