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

## 「状态字眼」是哪些

``types.STATUS_PATTERNS``（已到账 / 已退款 / 已发货 …… 与「预计 N 天」）**加上**
projection 的五个对外字面值本身。后者里有三句（已提出退款 / 支付处理中 / 已驳回）
不含任何 STATUS_WORDS，只按 STATUS_PATTERNS 扫的话，「您的退款支付处理中」在没有任何
观察的情况下原样放行 —— 可它恰恰是退款状态。所以 :func:`check_reply` 把两者合起来扫；
:func:`status_spans` 仍只按 STATUS_PATTERNS（签名注释的口径），合并在
:func:`_status_places` 里做。

**同一处只报一次**：「退款已到账」既是对外字面值、里面又含「已到账」，两段区间重叠，
合成一处；否则同一句话会报两条 unbacked_status。

## 这里不做的事

* 不调模型、不读库（:func:`turn_kb_doc_ids` 除外，它只读 event_log 取本轮命中）；
* 不改写回复 —— 拦下之后换什么话术是前台的事；
* 不自己抄那五个字面值 —— 从 projection 取，抄一份就有了第二个产出处。
"""

from __future__ import annotations

import json
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
# 状态字眼定位
# ---------------------------------------------------------------------------
def status_spans(text: str) -> list[tuple[int, int, str]]:
    """按 ``STATUS_PATTERNS`` 找出正文里的状态字眼：``[(起, 止, 原文)]``，按起点升序。

    重叠的命中合成一处（同一处只算一次）；否定式（尚未发货、还没到账）本来就不命中。
    """
    raw: list[tuple[int, int]] = []
    for pattern in STATUS_PATTERNS:
        raw.extend((m.start(), m.end()) for m in pattern.finditer(text or ""))
    return _merge_spans(text or "", raw)


def _status_places(text: str) -> list[tuple[int, int, str]]:
    """check_reply 要扫的全部「状态字眼」：STATUS_PATTERNS ∪ 五个对外字面值的出现处。"""
    raw = [(s, e) for s, e, _ in status_spans(text)]
    for literal in PUBLIC_STATUSES:
        raw.extend((s, s + len(literal)) for s in _occurrences(text, literal))
    return _merge_spans(text, raw)


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
        if (any(m in literal for m in REFUND_STATUS_MARKERS)
                and literal not in PUBLIC_STATUSES):
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
