"""检索召回的确定性近邻兜底（p15 契约 §1 C9 / C10、§2 T186）。

词法检索（:func:`maos.domain.cs.scripts.match_scripts` 的二元组重排 + 同义归一）**零命中**
（最高分够不着 ``MIN_SCRIPT_SCORE``，本该落兜底）时，才拿客户原文与话术库每篇登记过的
**例句**（客户会怎么问的整句说法）比一次字符 n 元组相似度，取最像的那一篇。同义词不进索引：
它们是两三个字的短词组，一句「登记过的短语 + 陌生对象」（「一般什么时候发工资」）凭它就拿高分。

* **纯字符统计，不是嵌入，不调模型**：字符 1–3 元组、TF-IDF 加权（IDF 按「一条说法」为一个文档
  计，平滑 ``log((N + 1) / (df + 1)) + 1``）、余弦相似度。一篇话术的分 = 它名下最像的那一条说法
  的余弦（最近邻，不是质心：一篇话术的说法彼此差得远，取平均会把每条都拉淡）。
* **虚字 n 元组减重**：每个字都在调用方给的虚字表里的 n 元组（「要不要」「什么」「的吗」）权重乘
  ``light_weight`` —— 同一张表、同一个系数由 ``scripts`` 传进来（它的二元组重排就是这么分轻重的），
  本模块不另抄一份。没有这一条，「明天要不要带伞」凭「要不要」三个字就像上了「要不要运费」。
* **没见过的 n 元组照样算进客户原文的向量长度**（按最大 IDF 计）：一句话里谁也对不上的部分越多，
  余弦越低 —— 长句里碰巧夹着「退款」两个字的闲聊不会因此拿高分。
* **弃权优先于召回**（契约：零「自信答错」）。最高分低于 :data:`MIN_SIMILARITY`，或第一名与
  第二名（另一篇）差距不足 :data:`MIN_MARGIN` → 弃权（:func:`nearest` 返回 ``None``），前台照旧兜底。
  另外几条硬弃权（字符统计分不清的句式，宁可不猜）：

  - 原文一个汉字都没有（话术库的说法全是中文，英文句与之比字符只会比出噪声）；
  - 规整后不足 :data:`MIN_QUERY_CHARS` 个字（一两个字对上谁都是巧合）；
  - 带**否定所需**的说法（:data:`NEGATION_RE`：「不用查物流了」「我不是要退货」）—— 否定句与
    它否定的那句话字面几乎一样；
  - 带**第三人称 / 转述**的说法（:data:`THIRD_PERSON_RE`：「听说」「我朋友」「他们」）—— 说的是
    别人的事，不是来办自己的事。

  这两张线索表只让近邻通道**弃权**（照旧兜底），不改词法检索、不改理解层、不改触发词。
* 阈值与差距只在开发集（p12 / p13）与本轨自写的类别例句上定，扫描表见 docs/DECISIONS.md
  task-t186；测试 maos/tests/test_cs_similar_t186.py 钉住召回与弃权率。

本模块只有纯函数（标准库），不读库、不落事件、不知道话术长什么样：索引由调用方
（``scripts.match_scripts``）从本租户的话术行里取出「(doc_id, 例句们)」喂进来，
``KbRetrieved`` 也由调用方照常落（``detail.query.channel = "similar"``）。
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Sequence

#: 字符 n 元组的阶（含两端）。
NGRAM_MIN = 1
NGRAM_MAX = 3

#: 近邻命中的最低余弦（[0, 1]）。取值依据见 docs/DECISIONS.md task-t186 的扫描表。
#: 必须不低于 ``scripts.MIN_SCRIPT_SCORE``：近邻那篇以这个分进 ScriptHit，组稿按同一道门槛收。
#: p16 task-t188 并进声明增补后重扫（开发集 + 自写句，表见 docs/DECISIONS.md task-t188）：
#: 0.25–0.34 配差距 ≥ 0.10 读数完全相同、0.38 起少一句对的，取值不变。
MIN_SIMILARITY = 0.34

#: 第一名与第二名（另一篇话术）的最低差距。两篇说法相近（「退款多久到」与「退款怎么还没到」）
#: 时差距小，宁可弃权也不在两篇之间猜。p16 task-t188 重扫：差距 0.05 时自写句里多出一句经近邻
#: 引错篇（LOG-004 / LOG-006 之间），0.10 起没有；取值不变。
MIN_MARGIN = 0.10

#: 规整后至少这么多个字才比。
MIN_QUERY_CHARS = 4

#: 近邻通道在 ``KbRetrieved.detail.query.channel`` 里的名字。
CHANNEL = "similar"

#: 否定所需：「不用 / 不需要 / 不是要 / 没有要 / 不打算 ……」。近邻通道见到就弃权。
NEGATION_RE = re.compile(r"不用|不需要|不必|用不着|不是要|没有要|没要|不打算|不是想|并不是|并没有要|不是说")

#: 第三人称 / 转述：说的是别人的事。近邻通道见到就弃权。「别人帮我签收了」是自己的事，
#: 所以不收裸「别人」，只收「别人家」。
THIRD_PERSON_RE = re.compile(
    r"听说|听人说|朋友|同事|邻居|室友|同学|他们|她们|别人家|人家|他说|她说|他的|她的"
    r"|我(?:妈|爸|姐|哥|弟|妹|老公|老婆|家人|家里人|爱人|对象)")


def normalize(text: str) -> str:
    """规整：NFKC、小写，只留汉字与 ASCII 字母数字（标点、空白、表情一律去掉）。"""
    s = unicodedata.normalize("NFKC", text or "").lower()
    return "".join(c for c in s if ("a" <= c <= "z") or ("0" <= c <= "9") or _is_cjk(c))


def _is_cjk(c: str) -> bool:
    return "一" <= c <= "鿿"


def has_cjk(text: str) -> bool:
    return any(_is_cjk(c) for c in unicodedata.normalize("NFKC", text or ""))


def ngrams(text: str) -> frozenset[str]:
    """规整后的字符 n 元组集合（``NGRAM_MIN``–``NGRAM_MAX``；二值，不计次数）。"""
    s = normalize(text)
    out = set()
    for n in range(NGRAM_MIN, NGRAM_MAX + 1):
        for i in range(len(s) - n + 1):
            out.add(s[i:i + n])
    return frozenset(out)


def abstain_cue(text: str) -> str:
    """原文带的硬弃权线索：``negation`` / ``third_person`` / ``""``。"""
    s = unicodedata.normalize("NFKC", text or "")
    if NEGATION_RE.search(s):
        return "negation"
    if THIRD_PERSON_RE.search(s):
        return "third_person"
    return ""


@dataclass(frozen=True)
class Neighbor:
    """近邻的结论：最像的那一篇、它的分、与第二名（另一篇）的差距。"""

    key: str
    score: float
    margin: float
    runner_up: str = ""


class SimilarIndex:
    """一组「(键, 说法们)」上的 TF-IDF 字符 n 元组索引。键一般是 doc_id。

    同一输入（键与说法的集合，与顺序无关）得到同一个索引：IDF 只看集合，排序按 (-分, 键)。
    ``light_chars`` / ``light_weight``：每个字都在 ``light_chars`` 里的 n 元组，权重乘 ``light_weight``。
    """

    def __init__(self, entries: Iterable[tuple[str, Sequence[str]]], *,
                 light_chars: frozenset[str] = frozenset(), light_weight: float = 1.0) -> None:
        rows: list[tuple[str, frozenset[str]]] = []
        for key, phrasings in entries:
            for phrase in phrasings:
                grams = ngrams(str(phrase))
                if grams:
                    rows.append((str(key), grams))
        rows.sort(key=lambda r: (r[0], sorted(r[1])))
        df: dict[str, int] = {}
        for _key, grams in rows:
            for g in grams:
                df[g] = df.get(g, 0) + 1
        n = len(rows)
        self._light_chars = frozenset(light_chars)
        self._light_weight = float(light_weight)
        self._idf = {g: math.log((n + 1) / (c + 1)) + 1.0 for g, c in df.items()}
        #: 没见过的 n 元组的 IDF（df = 0）：只进客户原文的向量长度，不会与任何说法相交。
        self._unseen_idf = math.log(n + 1) + 1.0
        self._rows = [(key, grams, math.sqrt(sum(self._weight(g) ** 2 for g in grams)))
                      for key, grams in rows]

    def _weight(self, gram: str) -> float:
        w = self._idf.get(gram, self._unseen_idf)
        if self._light_chars and all(ch in self._light_chars for ch in gram):
            w *= self._light_weight
        return w

    def __len__(self) -> int:
        return len(self._rows)

    def rank(self, text: str) -> list[tuple[str, float]]:
        """每个键取它名下最像的那一条说法的余弦，按 (-分, 键) 排好；分为 0 的不列。"""
        query = ngrams(text)
        if not query or not self._rows:
            return []
        q_norm = math.sqrt(sum(self._weight(g) ** 2 for g in query))
        best: dict[str, float] = {}
        for key, grams, norm in self._rows:
            inter = query & grams
            if not inter or norm <= 0 or q_norm <= 0:
                continue
            cos = sum(self._weight(g) ** 2 for g in inter) / (q_norm * norm)
            if cos > best.get(key, 0.0):
                best[key] = cos
        return sorted(((k, round(v, 6)) for k, v in best.items()), key=lambda kv: (-kv[1], kv[0]))


def nearest(index: SimilarIndex, text: str, *, min_similarity: float = MIN_SIMILARITY,
            min_margin: float = MIN_MARGIN) -> Neighbor | None:
    """最像的那一篇；拿不准就弃权（``None``）。弃权条件见模块头。"""
    if not has_cjk(text) or len(normalize(text)) < MIN_QUERY_CHARS or abstain_cue(text):
        return None
    ranked = index.rank(text)
    if not ranked:
        return None
    key, score = ranked[0]
    runner_up, second = ranked[1] if len(ranked) > 1 else ("", 0.0)
    margin = round(score - second, 6)
    if score < min_similarity or margin < min_margin:
        return None
    return Neighbor(key=key, score=score, margin=margin, runner_up=runner_up)
