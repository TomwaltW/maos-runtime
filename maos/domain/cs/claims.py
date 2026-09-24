"""回复出门前的确定性后置校验（p12 跨轨契约 §1.4 · T170）。

前台只转述**观察到的**事实（铁律 8）。一版回复草稿 :class:`~maos.domain.cs.types.ReplyDraft`
出门之前过这里的五条规则，全部满足才放行；任何一条不满足，前台（T169）把它换成兜底 +
转人工（``unverified_claim``）。口径全文在 review/p12-cs-contracts.md §1.4，这里逐条实现：

1. ``literal_not_in_text`` —— 每条 claim 的 literal 非空、且是正文的子串；
2. ``dangling_basis`` —— basis_ref 只认两种：``obs:<id>``（id 在本轮观察里）与
   ``kb:<doc_id>``（doc_id 在本轮检索命中里）；
3. ``unbacked_status`` —— 正文里每一处状态字眼，都必须落在某条**有效 obs:** claim 的
   literal 在正文中的出现区间里。``kb:`` 撑不起状态：话术库说的是规则，不是这一单；
4. ``uncited_rule`` —— citations 每一项都在本轮检索命中里；
5. ``foreign_literal`` —— literal 里说到退款 / 补偿的 claim，必须**逐字**等于
   ``maos.domain.refund.projection`` 的五个对外字面值之一（对外措辞的唯一产出处）。
   「说到」= 含契约四个触发词（已到账 / 已退款 / 补偿 / 赔偿）、或含某个对外字面值、
   或是「已经到账 / 已原路退回 / 已打款」一类退款完成态（均在规范化后判）。

## 「状态字眼」是哪些

三组合起来扫（:func:`_status_places`）：

1. ``types.STATUS_PATTERNS``（已到账 / 已退款 / 已发货 …… 与「预计 N 天」）；
2. projection 的五个对外字面值本身。其中三句（已提出退款 / 支付处理中 / 已驳回）
   不含任何 STATUS_WORDS，只按 STATUS_PATTERNS 扫的话，「您的退款支付处理中」在没有任何
   观察的情况下原样放行 —— 可它恰恰是退款状态；
3. :data:`SUPPLEMENTARY_STATUS_PATTERNS`：冻结词表的自然变体 —— 「已经到账」「已到帐」
   「已原路退回」「已打款」「已寄出 / 已送达」「被驳回了」，以及时限承诺「预计 3-5 个工作日」
   「1-3 个工作日原路退回」「3 天内到账」。冻结的「预计」正则只认单个数字紧跟单位，区间
   与不带「预计」的写法一律漏；契约要求话术「不许承诺时限」、并以 check_reply 为唯一口径，
   机器口径看不到最常见的写法就等于没有口径。

:func:`status_spans` 仍只按 STATUS_PATTERNS（签名注释的口径）。

**先规范化再扫**：NFKC（③ / 𝟑 / 全角 → 3），并去掉格式字符（零宽空格、零宽连接符、
软连字符）、组合附加符、空白与间隔号 —— 这些在客户端上看不见或不改变读法，却能把
「已​到账」从字面匹配里拆开。扫到的位置映射回原文偏移，规则 3 在原文偏移上比覆盖。

**同一处只报一次**：「退款已到账」既是对外字面值、里面又含「已到账」，两段区间重叠，
合成一处；否则同一句话会报两条 unbacked_status。紧挨着的两处（「已发货已签收」）不合并。

## 这里不做的事

* 不调模型、不读库（:func:`turn_kb_doc_ids` 除外，它只读 event_log 取本轮命中）；
* 不改写回复 —— 拦下之后换什么话术是前台的事；
* 不自己抄那五个字面值 —— 从 projection 取，抄一份就有了第二个产出处。
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Iterable

from maos.domain.cs.types import (
    BASIS_KB,
    BASIS_OBS,
    STATUS_PATTERNS,
    CheckResult,
    Claim,
    ReplyDraft,
    Violation,
    VIOLATION_DANGLING_BASIS,
    VIOLATION_FOREIGN_LITERAL,
    VIOLATION_LITERAL_NOT_IN_TEXT,
    VIOLATION_UNBACKED_STATUS,
    VIOLATION_UNCITED_RULE,
    plan_id_for,
)
from maos.domain.refund.projection import PUBLIC_STATUSES

#: 规则 5 的触发词：literal 里含这些之一，就是在说退款 / 补偿的状态，必须用对外字面值。
#: 契约 §1.4 原文「已到账 / 已退款 / 补偿 / 赔偿」。**不是**对外字面值的副本 ——
#: 那五句只从 projection 取（``PUBLIC_STATUSES``）。
REFUND_STATUS_MARKERS: tuple[str, ...] = ("已到账", "已退款", "补偿", "赔偿")

#: 本轮检索命中落在 event_log 里的事件名（``maos.kb.retriever.emit_kb_retrieved``）。
KB_RETRIEVED_EVENT = "KbRetrieved"

# ---------------------------------------------------------------------------
# 冻结词表的补充模式（在规范化后的正文上匹配：没有空白、没有零宽字符、数字已 NFKC）
# ---------------------------------------------------------------------------
#: 数量：阿拉伯数字（``\d`` 认一切 Unicode 十进制数字，含全角与阿拉伯-印度数字）与中文数字。
_NUM = r"(?:\d|[〇零一二三四五六七八九十两半百])+"
#: 区间连接：3-5、3~5、三到五、三至五。
_RANGE = rf"{_NUM}(?:(?:[-~～—–−]|至|到){_NUM})?"
#: 时长单位。
_UNIT = r"个?(?:工作日|自然日|天|日|小时|周|星期)"
#: 退款 / 到账一类的完成态动词（规则 3 与规则 5 共用）。
_REFUND_DONE_VERBS = r"到[账帐]|退款|退回|退还|打款|汇款"
#: 物流 / 订单一类的完成态动词。
_ORDER_DONE_VERBS = r"发货|发出|寄出|送达|到货|签收|揽收|出库|取消|驳回|补偿|赔偿"
#: 「已 / 已经 [为您] [原路] 动词」。
_DONE_PREFIX = r"已经?(?:(?:为|给|帮|替)您)?(?:原路)?"

#: 退款到账类的完成态说法（规则 5 用它补契约四个触发词：「退款已经到账」带 obs 也必须用对外字面值）。
_REFUND_DONE_RE = re.compile(rf"{_DONE_PREFIX}(?:{_REFUND_DONE_VERBS})")

#: 冻结 ``STATUS_PATTERNS`` 之外、check_reply 规则 3 还要认的状态字眼（DECISIONS task-t170）。
#: 只收**完成态断言**与**时限承诺**；政策说法（「原路退回」「七天无理由退货」「签收后 7 天内
#: 可申请退货」）不带「已」、不接到账 / 发货类动词，不命中。
SUPPLEMENTARY_STATUS_PATTERNS: tuple[re.Pattern[str], ...] = (
    _REFUND_DONE_RE,
    re.compile(rf"{_DONE_PREFIX}(?:{_ORDER_DONE_VERBS})"),
    re.compile(r"被(?:驳回|拒绝)了"),
    # 「预计」+ 区间 / 周：冻结正则只认单个数字
    re.compile(rf"预计{_RANGE}{_UNIT}"),
    re.compile(r"预计(?:今天|今日|明天|明日|后天|本周|下周|月底)"),
    # 不带「预计」的时限承诺：时长紧接到账 / 退回 / 发货类动词
    re.compile(rf"{_RANGE}{_UNIT}(?:之内|以内|内|左右)?(?:就|即可|即|会|能|可以|可|便)?"
               r"(?:到[账帐]|原路(?:退回|返回|退还)|退还到|退到|发货|送达|送到|到货)"),
)

#: 规范化时直接丢掉的可见分隔符（间隔号一类）。空白、格式字符（Cf）、组合附加符（Mn / Me）
#: 按类别丢，不在这里列。
_DROP_CHARS = frozenset("·・‧•∙⋅")


def _normalize(text: str) -> tuple[str, list[int]]:
    """规范化正文：``(规范化后的串, 每个字符在原文里的下标)``。

    逐字 NFKC（③ / 𝟑 / 全角数字 → 3，全角括号 → 半角），丢掉空白、格式字符（零宽空格、
    零宽连接符、软连字符）、组合附加符与间隔号。只用来**找**状态字眼，回报的位置与片段
    一律是原文的。
    """
    out: list[str] = []
    index: list[int] = []
    for i, ch in enumerate(text or ""):
        for c in unicodedata.normalize("NFKC", ch):
            if (c.isspace() or c in _DROP_CHARS
                    or unicodedata.category(c) in ("Cf", "Mn", "Me")):
                continue
            out.append(c)
            index.append(i)
    return "".join(out), index


def _norm_only(text: str) -> str:
    return _normalize(text)[0]


def _find_places(text: str, patterns: Iterable[re.Pattern[str]],
                 literals: Iterable[str] = ()) -> list[tuple[int, int, str]]:
    """在规范化后的正文上找 ``patterns`` 与 ``literals`` 的出现处，合并后映射回原文偏移。"""
    norm, index = _normalize(text)
    raw: list[tuple[int, int]] = []
    for pattern in patterns:
        raw.extend((m.start(), m.end()) for m in pattern.finditer(norm))
    for literal in literals:
        needle = _norm_only(literal)
        raw.extend((s, s + len(needle)) for s in _occurrences(norm, needle))
    out: list[tuple[int, int, str]] = []
    for start, end, _ in _merge_spans(norm, raw):
        o_start, o_end = index[start], index[end - 1] + 1
        out.append((o_start, o_end, text[o_start:o_end]))
    return out


# ---------------------------------------------------------------------------
# 状态字眼定位
# ---------------------------------------------------------------------------
def status_spans(text: str) -> list[tuple[int, int, str]]:
    """按 ``STATUS_PATTERNS`` 找出正文里的状态字眼：``[(起, 止, 原文)]``，按起点升序。

    在规范化后的正文上找（零宽字符、空白拆不开状态字眼），位置与片段是原文的。
    重叠的命中合成一处（同一处只算一次），紧挨着的两处不合并；否定式（尚未发货、
    还没到账）本来就不命中。
    """
    return _find_places(text or "", STATUS_PATTERNS)


def _status_places(text: str) -> list[tuple[int, int, str]]:
    """check_reply 要扫的全部「状态字眼」：STATUS_PATTERNS ∪ 补充模式 ∪ 五个对外字面值。"""
    return _find_places(text or "", STATUS_PATTERNS + SUPPLEMENTARY_STATUS_PATTERNS,
                        PUBLIC_STATUSES)


def _merge_spans(text: str, raw: Iterable[tuple[int, int]]) -> list[tuple[int, int, str]]:
    merged: list[list[int]] = []
    for start, end in sorted(set(raw)):
        if end <= start:
            continue
        if merged and start < merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(s, e, text[s:e]) for s, e in merged]


def _speaks_refund_status(literal: str) -> bool:
    """规则 5 的触发：literal（规范化后）含契约四个触发词之一、含某个对外字面值
    （「退款已到账啦」是在说那一句，就得逐字是那一句），或是退款到账类的完成态说法。"""
    norm = _norm_only(literal)
    return (any(m in norm for m in REFUND_STATUS_MARKERS)
            or any(_norm_only(p) in norm for p in PUBLIC_STATUSES)
            or bool(_REFUND_DONE_RE.search(norm)))


def _occurrences(text: str, literal: str) -> list[int]:
    """literal 在 text 里的全部起点（含重叠出现）。空 literal 没有出现处。"""
    if not literal:
        return []
    out: list[int] = []
    pos = text.find(literal)
    while pos != -1:
        out.append(pos)
        pos = text.find(literal, pos + 1)
    return out


# ---------------------------------------------------------------------------
# 后置校验
# ---------------------------------------------------------------------------
def _basis_ok(basis_ref: str, observations: frozenset[str],
              kb_doc_ids: frozenset[str]) -> bool:
    if basis_ref.startswith(BASIS_OBS):
        ref = basis_ref[len(BASIS_OBS):]
        return bool(ref) and ref in observations
    if basis_ref.startswith(BASIS_KB):
        ref = basis_ref[len(BASIS_KB):]
        return bool(ref) and ref in kb_doc_ids
    return False


def check_reply(draft: ReplyDraft, *, observations: frozenset[str] = frozenset(),
                kb_doc_ids: frozenset[str] = frozenset()) -> CheckResult:
    """五条规则全部满足才 ``ok``。纯函数、确定性；违例按规则序、再按出现序排列。

    ``observations`` 是本轮观察行的 id 集合（p12 恒为空），``kb_doc_ids`` 是本轮
    KbRetrieved 命中的 doc_id 集合（见 :func:`turn_kb_doc_ids`）。
    """
    observations = frozenset(observations or ())
    kb_doc_ids = frozenset(kb_doc_ids or ())
    text = draft.text or ""
    claims: tuple[Claim, ...] = tuple(draft.claims or ())
    violations: list[Violation] = []

    # 规则 1：literal 非空且是正文子串。
    literal_ok = []
    for idx, claim in enumerate(claims):
        ok = bool(claim.literal) and claim.literal in text
        literal_ok.append(ok)
        if not ok:
            violations.append(Violation(
                VIOLATION_LITERAL_NOT_IN_TEXT,
                f"claim[{idx}] literal={claim.literal!r} 不是正文子串"))

    # 规则 2：basis_ref 必须挂在本轮存在的观察 / 命中上。
    basis_ok = []
    for idx, claim in enumerate(claims):
        ok = _basis_ok(claim.basis_ref or "", observations, kb_doc_ids)
        basis_ok.append(ok)
        if not ok:
            violations.append(Violation(
                VIOLATION_DANGLING_BASIS,
                f"claim[{idx}] basis_ref={claim.basis_ref!r} 本轮不存在"))

    # 规则 3：每一处状态字眼都落在某条有效 obs: claim 的出现区间里。kb: 撑不起状态。
    backing: list[tuple[int, int]] = []
    for idx, claim in enumerate(claims):
        if (literal_ok[idx] and basis_ok[idx]
                and (claim.basis_ref or "").startswith(BASIS_OBS)):
            backing.extend((s, s + len(claim.literal))
                           for s in _occurrences(text, claim.literal))
    for start, end, fragment in _status_places(text):
        if not any(bs <= start and end <= be for bs, be in backing):
            violations.append(Violation(
                VIOLATION_UNBACKED_STATUS,
                f"[{start},{end}) {fragment!r} 没有本轮观察撑"))

    # 规则 4：引用的话术必须是本轮检出的。
    for cite in tuple(draft.citations or ()):
        if cite not in kb_doc_ids:
            violations.append(Violation(
                VIOLATION_UNCITED_RULE, f"citation {cite!r} 本轮没有检出"))

    # 规则 5：说退款 / 补偿状态的 literal 必须逐字是对外五句之一。
    for idx, claim in enumerate(claims):
        literal = claim.literal or ""
        if _speaks_refund_status(literal) and literal not in PUBLIC_STATUSES:
            violations.append(Violation(
                VIOLATION_FOREIGN_LITERAL,
                f"claim[{idx}] literal={literal!r} 不是 projection 的对外字面值"))

    return CheckResult(ok=not violations, violations=tuple(violations))


# ---------------------------------------------------------------------------
# 本轮命中读回
# ---------------------------------------------------------------------------
def _detail_of(row: dict[str, Any]) -> dict[str, Any]:
    detail = row.get("detail")
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except ValueError:
            return {}
    return detail if isinstance(detail, dict) else {}


def turn_kb_doc_ids(store: Any, *, conversation_id: str, turn_id: str) -> frozenset[str]:
    """读回本轮的检索命中：``plan_id = plan_id_for(conversation_id)`` 且
    ``task_id == turn_id`` 的 KbRetrieved 行里 ``detail.docs[].doc_id`` 的并集。

    口径同 ``scripts/verify.py`` 第 5 项：``docs`` 缺席时认 ``hits``，条目是 dict 取
    ``doc_id``、是字符串就当 doc_id。同会话别的轮、别的会话的命中一律不算 ——
    上一轮检出的话术撑不起这一轮的引用。
    """
    if store is None or not turn_id:
        return frozenset()
    rows = store.list_event_log(plan_id=plan_id_for(conversation_id))
    found: set[str] = set()
    for row in rows or ():
        if row.get("event_type") != KB_RETRIEVED_EVENT or row.get("task_id") != turn_id:
            continue
        detail = _detail_of(row)
        for hit in detail.get("docs") or detail.get("hits") or ():
            doc_id = hit.get("doc_id") if isinstance(hit, dict) else hit
            if isinstance(doc_id, str) and doc_id:
                found.add(doc_id)
    return frozenset(found)
