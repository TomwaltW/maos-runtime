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

消费端口要过三道口径对齐，缺一条这条分支就恒退化成本地实现（T13 轨补齐，
原状记在 BACKLOG `## task-X3` 第 1、2 条）：

1. **主键**。F-2 返回的第一位是源表主键 `id`，本层却按 `doc_id` 索引候选集。
   `kb_doc.id` 是 `tenant_id:doc_id` 的生成列，所以端口回来的 id 要过
   `_row_id_index()` 那张回查表换成 `doc_id`。
2. **分词**。影子表存的是 `kb.fts_text()` 切过的文本（中文按字），端口不知道这个
   约定，把原查询串直接丢给 FTS5 就是「整串汉字一个 token」—— 一条都命不中，
   而且不报错。所以发给端口的 `q` 也先过 `kb.fts_text()`。
3. **查询语义归端口所有 —— 但「只查一列」那半条不是语义，是漏问。** 端口把词间
   做 AND、且只查 `field` 那一列。词间 AND 归后端，认下；只查一列却是调用方少发了
   一次 —— 本地实现跨 title + body 两列，只发 `body` 的后果是**标题命中的知识召不
   回来**，症状是「换了后端之后 RAG 好像笨了一点」，两边都不报错。T16 轨按
   `PORT_FTS_FIELDS` 逐列各发一次再合并（见 `_port_fts_scores`），把这半条差异抹平；
   剩下的 AND vs OR **本来就可以不同**。跨后端必须一致的只有附则那两条：
   分数越大越相关、次序确定。
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
from maos.config import get_config_source

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

    T35 起走 `maos.config` 的配置面（口径同 `kb.kb_enabled`）：缺省源就是
    `os.environ.get`，未设与设成空串在改之前就都落在「回落默认值」那一支上，
    取值逐字节不变；`MAOS_CONFIG_SOURCE=nacos` 时同一句改从 Nacos 取。
    显式 `env` 那一支仍读它自己给的那份字典。
    """
    raw = (env.get(kb.KB_WEIGHTS_ENV) if env is not None
           else get_config_source().get(kb.KB_WEIGHTS_ENV, ""))
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
#: 每个 store 上各条检索通道的可用性判定，探一次记一次。
#: 用 `WeakKeyDictionary` 而不是 `id(store)` 做键：id 会被回收后的新对象复用，
#: 那就成了把 A 的判定按在 B 头上，而症状是「换了个库检索忽然全走本地」。
_PORT_STATE: Any = weakref.WeakKeyDictionary()

#: 上面那张判定表**成立的前提**：记下判定时知识层 schema 的版本号。
#: 单独一张表而不是塞进判定表里 —— 判定是 bool，版本是 int，混在一起
#: `False in state.values()` 会被版本 0 撞上（`0 == False`）。
_PORT_STATE_SCHEMA: Any = weakref.WeakKeyDictionary()

#: 判定表里属于 StorePort 的两个键 —— 就是端口方法名，`_port_search` 按它取用。
PORT_CHANNELS = ("fts_search", "vector_search")

#: 本地 FTS5 退化通道在**同一张**判定表里的键。**不是端口方法名**，所以
#: `port_channel_state()` 不把它算进去。它要的只是同一套「只告警一次 + 版本一动
#: 就作废」的记账，不是第二套机制 —— 两套记账迟早在「到底告警过没有」上打架。
LOCAL_FTS_CHANNEL = "local_fts"

#: 读不到版本号时的占位。老库还没建 `kb_schema_version` 表，或这个 store 压根
#: 查不了库，都落在这一档。`-1` 而不是 `0`：0 是「表在、还没记过账」的真实版本。
_SCHEMA_UNKNOWN = -1


def _schema_stamp(store: Any) -> int:
    """这个 store 上知识层 schema 的当前版本。读不到就是 `_SCHEMA_UNKNOWN`。"""
    try:
        return kb.applied_schema_version(store)
    except Exception:                                  # noqa: BLE001 —— 探测用
        return _SCHEMA_UNKNOWN


def _port_state(store: Any) -> dict[str, bool]:
    """这个 store 的通道判定表，**就地可写**（调用方改它就是改记下来的那份）。

    判定是粘性的（探出 False 就一直 False，「只告警一次」正是靠这个），所以它
    必须跟着 schema 版本走：T17 之后老库能就地升到目标形状，升完端口通道其实
    通了，而这张表还按「不通」走 —— 库已经好了检索却没跟上，且没有任何红灯。
    所以每次要用一条 False 判定之前先看版本动没动，动了就整张作废、下一次调用
    重新探。**作废而不是直接翻成 True**：通没通只有真调一次才知道。

    版本**只在存在 False 判定时才去读**。True 判定不短路（每次仍旧真调端口，
    端口坏了自会抛出来翻成 False），作废它买不到任何东西，却要在热路径上多发
    一条 SQL —— PolarDB 上那是一次真的网络往返。
    """
    try:
        state = _PORT_STATE.setdefault(store, {})
    except TypeError:                                  # 不支持弱引用的 store
        return {}
    if False in state.values() and _PORT_STATE_SCHEMA.get(store) != _schema_stamp(store):
        state.clear()
        _PORT_STATE_SCHEMA.pop(store, None)
    return state


def _remember_degraded(store: Any, state: dict[str, bool], channel: str) -> None:
    """记下「这条通道在这个 store 上不通」，连同当时的 schema 版本一起。

    版本就是这条判定的**有效期**：库就地升级之后它自动作废（见 `_port_state`）。
    """
    state[channel] = False
    try:
        _PORT_STATE_SCHEMA[store] = _schema_stamp(store)
    except TypeError:                                  # 不支持弱引用，判定本来就不粘
        pass


def port_channel_state(store: Any) -> dict[str, bool]:
    """这个 store 上两条 StorePort 通道各自探过没有、通不通。只读，供自证与测试用。

    只报端口那两条：本地 FTS5 的退化判定共用同一张表，但它不是端口通道，
    混进来会让「换后端之后端口通没通」这个判据读起来含糊。它走 `local_fts_state`。
    """
    state = _port_state(store)
    return {ch: state[ch] for ch in PORT_CHANNELS if ch in state}


def local_fts_state(store: Any) -> bool | None:
    """本地 FTS5 退化通道在这个 store 上试过没有、通不通。`None` = 没试过。"""
    return _port_state(store).get(LOCAL_FTS_CHANNEL)


def _port_search(store: Any, channel: str, args: tuple) -> list[tuple[str, float]] | None:
    """走 StorePort 的一条通道。返回 `None` = 这条通道走不通，调用方用本地实现。

    **「有这个方法」不等于「这条通道能用」。** 端口是后端自己的实现，schema 对不上、
    驱动没装、索引没建，都只会在第一次真调用时才暴露。所以第一次调用兼作探测：
    抛了就**记住判定**并只告警一次。每次检索都抛一次再吞掉的写法，症状是日志被
    刷满而没人看得出这条通道其实一直没通。

    这层探测**不是**用来兜「列名没对齐」的 —— 那种恒退化是缺陷，T13 轨已经把
    `kb_doc` / 影子表的 `id` 列补齐（见本模块开头 StorePort 一节的三道对齐）。
    它兜的是 PG 那种「装不上就优雅降级」：后端真的不在，检索也不该整条挂掉。

    探测通过之后，端口返回的**空列表就是真的没命中**，不再回落本地实现 ——
    F-2 原话「『后端没准备好』不许伪装成『没命中』」，反过来同样成立：
    把「后端说没有」偷偷换成本地实现的结果，两条通道的口径就再也对不上了。

    **判定粘，但不粘过一次 schema 迁移**：老库上探出的 False 在库升上去之后
    自动作废（`_port_state`），下一次调用重新探。否则长跑进程里就地升级库
    永远不自愈，得重开进程 —— 而这件事同样没有任何红灯。
    """
    method = getattr(store, channel, None)
    if not callable(method):
        return None                                    # 能力探测不成立：没有这个方法
    state = _port_state(store)
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
        _remember_degraded(store, state, channel)
        return None
    state[channel] = True
    return rows


def _row_id_index(candidates: dict[str, dict]) -> dict[str, str]:
    """候选集的 `kb_doc.id -> doc_id` 回查表。

    **正向构造，不反解字符串**：`id` 是 `tenant_id:doc_id` 拼出来的，doc_id 里
    带冒号时按分隔符劈会劈错，而候选集手上两个字段都在，拼一次必然对得上。
    """
    return {kb.doc_row_id(doc.get("tenant_id"), doc_id): doc_id
            for doc_id, doc in candidates.items()}


def _to_doc_scores(raw: list[tuple[str, float]],
                   candidates: dict[str, dict]) -> dict[str, float]:
    """端口/本地回来的 `(id, 分数)` 收敛成 `{doc_id: 分数}`，顺带做候选集过滤。

    两种 id 都认：F-2 口径的源表主键（`tenant_id:doc_id`），以及本地实现直接给的
    `doc_id`。**先认前者**——后者只是本模块自己那条 SQL 的形态，撞车时以契约为准。

    候选集之外的一律丢掉。F-2 的两条通道都没有租户参数、端口是**全表**查的，
    跨租户不召回全靠这一句兜 —— 它不是性能优化，删掉就是事故。
    """
    row_ids = _row_id_index(candidates)
    scores: dict[str, float] = {}
    for raw_id, score in raw:
        doc_id = row_ids.get(raw_id, raw_id if raw_id in candidates else None)
        if doc_id is not None:
            scores[doc_id] = score
    return scores


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

    **这条路在 PG 后端上是死的**：`bm25()` 与影子表 `kb_doc_fts` 两样都是
    SQLite FTS5 专有的。正路是端口的 `fts_search`（PG 侧走 tsvector，T18 已填实），
    这里只有在「PG 后端 + 端口通道也走不通」这个组合下才会走到，然后每次检索
    失败一次。所以失败按 store 只告警一次，口径与 `_port_search` 同一套记账
    （`_port_state`）—— 每次刷一条只会把日志淹掉，而淹掉的正是上面那条真告警。

    **失败之后仍然照试不误**，不像端口通道那样短路：它是 SQLite 上的正路，
    库刚建好、影子表刚迁移完这类「这一刻不行下一刻行」是常态，短路掉等于
    把 SQLite 的兜底也一并拆了。记账只管住日志，不管住调用。
    """
    match = " OR ".join(f'"{t}"' for t in kb.tokenize(keyword))
    if not match:
        return []
    state = _port_state(store)
    try:
        rows = kb.query(
            store,
            "SELECT doc_id, bm25(kb_doc_fts) AS rank FROM kb_doc_fts"
            " WHERE kb_doc_fts MATCH ? AND tenant_id = ? ORDER BY rank LIMIT ?",
            (match, tenant_id, int(limit)))
    except Exception as exc:                           # noqa: BLE001 —— 检索不阻塞
        if state.get(LOCAL_FTS_CHANNEL) is not False:
            log.warning(
                "本地 FTS5 检索失败（%s），本通道记 0 分。"
                " `bm25()` 与影子表都是 SQLite FTS5 专有的，PG 后端上这条退化路径本来就走不通；"
                " 所以这条按 store 只告警一次并记住判定，检索本身照常继续。", exc)
        _remember_degraded(store, state, LOCAL_FTS_CHANNEL)
        return []
    state[LOCAL_FTS_CHANNEL] = True
    return [(r["doc_id"], -float(r["rank"])) for r in rows]


#: 端口全文通道要问的列，与本地实现跨的那两列（title + body）同一份口径。
#: **顺序即语义**：先问的那一列兼作这条通道的探针，它走不通就整条退化成本地实现。
PORT_FTS_FIELDS = ("title", "body")


def _port_fts_scores(store: Any, keyword: str, candidates: dict[str, dict],
                     limit: int) -> dict[str, float] | None:
    """走 StorePort 的全文通道：`PORT_FTS_FIELDS` 每列各问一次，合并成一份分数。

    返回 `None` = 这条通道走不通，调用方回落本地实现。

    **为什么要问两次。** F-2 的 `fts_search(table, field, q, limit)` 一次只认一列，
    而本地实现跨 title + body。只问 `body` 那一次的后果是标题命中的知识召不回来，
    而两边都不报错（原状记在 BACKLOG `## task-T13` 第 1 条）。代价是每次检索多一个
    来回 —— PolarDB 上是真的网络往返，但它是**每列一次的常数**，不随候选集规模涨。
    会把 PolarDB 打爆的是「按候选逐条去问后端」，那是另一回事，本函数不沾。

    **合并前每列各自归一，不拿两列的 bm25 直接比大小。** bm25 的 IDF 与列长都是按列
    算的：title 短、body 长，同一个词在两列上的原始分不同量纲，混在一起排名次等于让
    短列恒赢。所以每列先过 `_rank_normalize` 拿列内名次分，再**逐文档取 max** ——
    口径是「在任意一列里最靠前的那个名次代表这条知识」，与本地那条跨列 OR 的语义
    对得上（命中任一列即召回）。取和 / 加权和都会让「两列都命中」压过「一列命中得
    很靠前」，那是把召回口径悄悄改成了相关度口径，不在本轨要买的东西里。

    **任一列走不通就整条退化**，不拿半份结果凑：只有一列通的话，召回集会在两次检索
    之间飘（这次半份、下次退化成本地全份），而「连跑两次输出一致」是硬判据。
    `_port_search` 的探测结论按通道记，所以第二列在第一列失手后会直接短路，
    退化告警仍然只有一条。
    """
    query_text = kb.fts_text(keyword)
    merged: dict[str, float] = {}
    for field in PORT_FTS_FIELDS:
        raw = _port_search(
            store, "fts_search", ("kb_doc", field, query_text, int(limit)))
        if raw is None:
            return None
        for doc_id, score in _rank_normalize(_to_doc_scores(raw, candidates)).items():
            if score > merged.get(doc_id, 0.0):
                merged[doc_id] = score
    return merged


def _fts_scores(store: Any, tenant_id: str, keyword: str,
                candidates: dict[str, dict], limit: int) -> dict[str, float]:
    """BM25 通道。优先走 StorePort.fts_search（F-2），走不通就用本地 FTS5。

    两条实现都只负责给出「谁比谁更相关」的次序，归一交给 `_rank_normalize` ——
    BM25 的绝对值随语料规模浮动，拿它跟另外三个通道直接相加是量纲错配。

    **租户收窄不靠这一层**：F-2 的 `fts_search` 没有租户参数，端口是全表查的；
    跨租户不召回由阶段一的候选集兜住（`_to_doc_scores` 里那一句），
    这也正是「阶段一是硬约束而不是打分项」在实现上的样子。

    发给端口的是 `kb.fts_text(keyword)` 而**不是**原查询串（在 `_port_fts_scores`
    里过的）：影子表里存的就是这个函数切过的文本，查询侧不走同一个函数，中文那一段
    一个字都对不上（缺省的 unicode61 把整串汉字当一个 token），而且不报错 ——
    这正是 `kb.fts_text` 的 docstring 里写的那条「两边各写一份 = 召回恒为空」，
    端口这条路上同样成立。
    """
    if not keyword:
        return {}
    scores = _port_fts_scores(store, keyword, candidates, limit)
    if scores is not None:
        return scores
    return _rank_normalize(
        _to_doc_scores(_local_fts_rows(store, tenant_id, keyword, limit), candidates))


def _vector_scores(store: Any, query_text: str,
                   candidates: dict[str, dict], limit: int) -> dict[str, float]:
    """语义通道。优先 StorePort.vector_search（pgvector），走不通就纯 Python 余弦。"""
    if not query_text:
        return {}
    vec = embed(query_text)
    raw = _port_search(store, "vector_search", ("kb_doc", "embedding", vec, int(limit)))
    if raw is not None:
        # 截到 [0,1]：余弦本身取值 [-1,1]，负相关对检索没有意义 —— 与本地那条
        # `cosine()` 同一个口径，两边都截一次，别让方向差落到融合那一步才显形。
        return {d: max(0.0, min(1.0, float(s)))
                for d, s in _to_doc_scores(raw, candidates).items()}
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
                      candidate_count: int = 0,
                      event_sink: Any = None) -> dict:
    """把这次检索落成一条 `KbRetrieved`（F-3 冻结形状）。返回落库的 detail。

    走现有 `append_event_log`，**不加新 Topic** —— 冻结的事件契约里没有这个类型，
    也不许为此去加。

    ## 检索走 `store`，落事件走 `event_sink`

    `store` 这个位置从 T13 起可以是 StorePort（「RAG 真跑在 PolarDB 上」的前提），
    而 F-2 那五个签名里**没有** `append_event_log`：事件日志是核心 Store 的冻结表
    之一，不是端口的职责。于是检索这半条能走端口、落事件那半条不能，整条链路换不成
    端口对象 —— 而且撞在检索**之后**，检索看起来是通的（原状记在 BACKLOG
    `## task-T13` 第 2 条）。

    解法是把两件事拆开，而**不是**给 StorePort 加第六个方法（那是动 F-2 冻结面）。
    `event_sink` 缺省回落 `store`，所以今天所有调用方一个字节都不用改；把 `store`
    换成端口的新调用方，点名给一个核心 Store 作 sink 即可。

    sink 给了却落不了事件时**抛 TypeError 而不是静默跳过**：下面第三条「命中为空
    也落」的前提是事件必落，悄悄不落会让「这次到底检没检」无从追溯，而 RAG 有无
    对照实验（R5）正是拿 event_log 判的 —— 那种失效没有症状，只有结论变形。

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
    sink = store if event_sink is None else event_sink
    if sink is not None:
        append = getattr(sink, "append_event_log", None)
        if not callable(append):
            raise TypeError(
                f"{type(sink).__name__} 没有 append_event_log，KbRetrieved 落不下去。"
                " 事件日志是核心 Store 的冻结表，不在 F-2 的五个方法里 —— 检索走"
                " StorePort 时请另给一个核心 Store 作 event_sink，"
                " 不要去给端口加第六个方法（那是动冻结契约面）。"
            )
        append({
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
                     trace_id: str = "", kinds: tuple[str, ...] | None = None,
                     event_sink: Any = None) -> list[dict]:
    """检索 + 落 `KbRetrieved` 事件。KB 关闭时既不检索也不落事件。

    「关掉就一条事件都没有」是对照实验的判据之一：without_kb 那一跑的
    event_log 里不该有任何 KbRetrieved，否则「有无 RAG」这条线本身就不干净。

    `store` 走检索、`event_sink` 走落盘，理由见 `emit_kb_retrieved`。缺省两者同体，
    老调用方零改动；`store` 是 StorePort 时必须点名 sink，不点名就在检索之后
    撞 TypeError —— 检索那半条看起来是通的，所以这条不能靠「跑一下试试」发现。
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
                      candidate_count=len(candidates), event_sink=event_sink)
    return hits
