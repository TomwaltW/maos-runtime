"""客服前台的评测跑批（p12 跨轨契约 §1.4 / §1.6 · T170）。

评测集是 ``scenarios/cs/eval/p12_cases.json``：全部合成、按方案 §7 的类别出题，
每个 case 是一段**新会话**的若干轮，每轮写明期望的出口（route）、意图（intent）、
转人工原因（reason，仅 handoff）与可选的引用（cite，方案编号）。

:func:`run_eval` 对每个 case 调一次 ``desk_factory()`` 拿一个新前台，逐轮构造
:class:`~maos.ingress.contracts.InboundMessage` 喂给 ``handle``，拿回
:class:`~maos.domain.cs.types.DeskResult` 按期望比对，计四个指标：

* ``intent_accuracy`` —— 意图对的轮数 / 总轮数；
* ``route_accuracy`` —— 出口对的轮数 / 总轮数；期望 handoff 的轮**连同 reason 一起比**；
* ``handoff_recall`` —— 期望 handoff 的轮里实际 handoff 的比例（不看 reason）；
* ``status_fabrication`` —— 该轮 ``reply_text`` 在空观察下 :func:`check_reply` 报
  ``unbacked_status`` 的轮数（p12 没有观察来源，任何状态字眼都是编的）。

另记一个 ``cite_accuracy``：期望里给了 cite 的轮，``draft.citations`` 必须含
``kb-cs-<租户>-<编号>``。门槛文件里没写它时按 1.0 要求（契约「给了就要求」）。

本模块不 import 前台、会话表、检索的任何实现：前台是注入的（``desk_factory``），
评测既能跑真前台（T169），也能跑测试里的替身。
"""

from __future__ import annotations

import json
import pathlib
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from maos.domain.cs.claims import check_reply
from maos.domain.cs.types import (
    CHANNEL_WECHAT_KF,
    HANDOFF_REASONS,
    INTENTS,
    ROUTE_HANDOFF,
    ROUTES,
    VIOLATION_UNBACKED_STATUS,
    DeskResult,
    ReplyDraft,
)
from maos.ingress.contracts import InboundMessage

#: 评测集的位置（仓库根下）。
EVAL_PATH: pathlib.Path = (pathlib.Path(__file__).resolve().parents[3]
                           / "scenarios" / "cs" / "eval" / "p12_cases.json")

#: case 没写 open_kfid 时用的客服账号，缺省映射到演示租户。
DEFAULT_OPEN_KFID = "wk_eval"
DEFAULT_TENANT_MAP: Mapping[str, str] = {DEFAULT_OPEN_KFID: "tnt-demo"}

#: 门槛文件里认的四个键（契约 §1.6）。
THRESHOLD_KEYS = ("intent_accuracy", "route_accuracy", "handoff_recall",
                  "status_fabrication_max")

_SCHEME_RE = re.compile(r"^[A-Z]{3}-\d{3}$")


def cite_doc_id(tenant_id: str, scheme_no: str) -> str:
    """方案编号在某租户下的话术 doc_id（契约 §1.5：``kb-cs-<租户>-<编号>``）。"""
    return f"kb-cs-{tenant_id}-{scheme_no}"


# ---------------------------------------------------------------------------
# 数据形状
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class EvalExpect:
    """一轮的期望。``reason`` 只在 ``route == 'handoff'`` 时非空；``cite`` 是方案编号。"""
    route: str
    intent: str
    reason: str = ""
    cite: str = ""

    def to_json(self) -> dict[str, str]:
        d = {"route": self.route, "intent": self.intent}
        if self.reason:
            d["reason"] = self.reason
        if self.cite:
            d["cite"] = self.cite
        return d


@dataclass(frozen=True)
class EvalCase:
    """一段会话：``turns`` 与 ``expect`` 等长。``open_kfid`` 空串 = 用缺省账号。"""
    id: str
    turns: tuple[str, ...]
    expect: tuple[EvalExpect, ...]
    tags: tuple[str, ...] = ()
    open_kfid: str = ""
    synthetic: bool = True

    @property
    def effective_open_kfid(self) -> str:
        return self.open_kfid or DEFAULT_OPEN_KFID


@dataclass(frozen=True)
class EvalMiss:
    """一轮没对上的明细。``problems`` ⊆ {route, intent, cite, status_fabrication, error}。"""
    case_id: str
    turn: int                      # 1 起
    text: str
    expected: dict[str, str]
    actual: dict[str, Any]
    problems: tuple[str, ...]


def _ratio(num: int, den: int) -> float:
    return 1.0 if den == 0 else num / den


@dataclass(frozen=True)
class EvalReport:
    cases: int
    turns: int
    intent_hits: int
    route_hits: int
    handoff_expected: int
    handoff_caught: int
    cite_expected: int
    cite_hits: int
    status_fabrication: int
    failures: tuple[EvalMiss, ...] = field(default_factory=tuple)

    @property
    def intent_accuracy(self) -> float:
        return _ratio(self.intent_hits, self.turns)

    @property
    def route_accuracy(self) -> float:
        return _ratio(self.route_hits, self.turns)

    @property
    def handoff_recall(self) -> float:
        return _ratio(self.handoff_caught, self.handoff_expected)

    @property
    def cite_accuracy(self) -> float:
        return _ratio(self.cite_hits, self.cite_expected)

    def metrics(self) -> dict[str, float | int]:
        return {"intent_accuracy": self.intent_accuracy,
                "route_accuracy": self.route_accuracy,
                "handoff_recall": self.handoff_recall,
                "status_fabrication": self.status_fabrication,
                "cite_accuracy": self.cite_accuracy}

    def shortfalls(self, thresholds: Mapping[str, Any]) -> list[str]:
        """没达标的指标，一条一句；空列表 = 全部达标。

        一轮都没跑（case 被筛空、传进来的是耗尽的生成器）本身就是一条 shortfall：
        各比例在分母为 0 时取 1.0，不拦的话空跑会报满分。
        """
        out: list[str] = []
        if self.turns == 0:
            out.append(f"turns=0（cases={self.cases}）：评测空转，一轮都没跑")
        for key in ("intent_accuracy", "route_accuracy", "handoff_recall"):
            want = float(thresholds.get(key, 1.0))
            got = getattr(self, key)
            if got < want:
                out.append(f"{key}={got:.4f} < {want}")
        cap = int(thresholds.get("status_fabrication_max", 0))
        if self.status_fabrication > cap:
            out.append(f"status_fabrication={self.status_fabrication} > {cap}")
        want_cite = float(thresholds.get("cite_accuracy", 1.0))
        if self.cite_accuracy < want_cite:
            out.append(f"cite_accuracy={self.cite_accuracy:.4f} < {want_cite}")
        return out

    def meets(self, thresholds: Mapping[str, Any]) -> bool:
        return not self.shortfalls(thresholds)

    def describe(self) -> str:
        """人读的一段：指标一行，失败逐轮一行。"""
        lines = [" ".join(f"{k}={v:.4f}" if isinstance(v, float) else f"{k}={v}"
                          for k, v in self.metrics().items())
                 + f" cases={self.cases} turns={self.turns}"]
        for miss in self.failures:
            lines.append(f"  {miss.case_id}#{miss.turn} {','.join(miss.problems)}"
                         f" expected={miss.expected} actual={miss.actual}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 读评测集
# ---------------------------------------------------------------------------
def _parse_expect(case_id: str, idx: int, raw: Any) -> EvalExpect:
    where = f"{case_id} expect[{idx}]"
    if not isinstance(raw, dict):
        raise ValueError(f"{where} 不是对象")
    route = str(raw.get("route", ""))
    intent = str(raw.get("intent", ""))
    reason = str(raw.get("reason", "") or "")
    cite = str(raw.get("cite", "") or "")
    if route not in ROUTES:
        raise ValueError(f"{where} route={route!r} 不在 {ROUTES}")
    if intent not in INTENTS:
        raise ValueError(f"{where} intent={intent!r} 不在 {INTENTS}")
    if route == ROUTE_HANDOFF:
        if reason not in HANDOFF_REASONS:
            raise ValueError(f"{where} handoff 必须带 reason ∈ {HANDOFF_REASONS}，收到 {reason!r}")
    elif reason:
        raise ValueError(f"{where} route={route} 不许带 reason")
    if cite and not _SCHEME_RE.match(cite):
        raise ValueError(f"{where} cite={cite!r} 不是方案编号")
    return EvalExpect(route=route, intent=intent, reason=reason, cite=cite)


def load_document(path: pathlib.Path | str = EVAL_PATH) -> dict[str, Any]:
    """整份评测文件（含 ``_provenance`` / ``_thresholds``）。"""
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


def load_thresholds(path: pathlib.Path | str = EVAL_PATH) -> dict[str, Any]:
    return dict(load_document(path).get("_thresholds") or {})


def load_cases(path: pathlib.Path | str = EVAL_PATH) -> tuple[EvalCase, ...]:
    """读评测集并校形状：expect 与 turns 等长、枚举值合法、handoff 必带 reason、id 不重。"""
    doc = load_document(path)
    raw_cases = doc.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError(f"{path}: cases 必须是非空数组")
    out: list[EvalCase] = []
    seen: set[str] = set()
    for raw in raw_cases:
        case_id = str(raw.get("id", ""))
        if not case_id or case_id in seen:
            raise ValueError(f"case id {case_id!r} 为空或重复")
        seen.add(case_id)
        turns = raw.get("turns")
        expect = raw.get("expect")
        if (not isinstance(turns, list) or not isinstance(expect, list)
                or not turns or len(turns) != len(expect)):
            raise ValueError(f"{case_id}: turns 与 expect 必须是等长非空数组")
        if not all(isinstance(t, str) and t.strip() for t in turns):
            raise ValueError(f"{case_id}: turns 每项必须是非空字符串")
        out.append(EvalCase(
            id=case_id,
            turns=tuple(turns),
            expect=tuple(_parse_expect(case_id, i, e) for i, e in enumerate(expect)),
            tags=tuple(str(t) for t in raw.get("tags") or ()),
            open_kfid=str(raw.get("open_kfid", "") or ""),
            synthetic=bool(raw.get("synthetic", False)),
        ))
    return tuple(out)


# ---------------------------------------------------------------------------
# 跑批
# ---------------------------------------------------------------------------
def inbound_for(case: EvalCase, turn: int, text: str) -> InboundMessage:
    """第 ``turn``（1 起）轮的入站消息。同一 case 的各轮同一个客户、同一个客服账号。"""
    who = f"eval-{case.id}"
    return InboundMessage(channel=CHANNEL_WECHAT_KF, chat_id=who, sender=who, text=text,
                          msg_id=f"{case.id}-{turn}",
                          raw={"open_kfid": case.effective_open_kfid})


def _fabricates_status(reply_text: str) -> bool:
    result = check_reply(ReplyDraft(text=reply_text or ""), observations=frozenset(),
                         kb_doc_ids=frozenset())
    return any(v.kind == VIOLATION_UNBACKED_STATUS for v in result.violations)


def _check_cases(cases: tuple[EvalCase, ...]) -> None:
    """跑之前按 :func:`load_cases` 同一套形状规则校一遍（内存里手造 / replace 出来的 case 也算）：
    turns 与 expect 等长且非空、id 非空且不重。不合形状就抛 ValueError，一个前台都不造 ——
    否则 zip 会悄悄丢掉多出来的轮或期望，报告照样满分；重复 id 会让 msg_id 撞车。"""
    seen: set[str] = set()
    for case in cases:
        if not case.id or case.id in seen:
            raise ValueError(f"case id {case.id!r} 为空或重复")
        seen.add(case.id)
        if not case.turns or len(case.turns) != len(case.expect):
            raise ValueError(f"{case.id}: turns（{len(case.turns)}）与 expect"
                             f"（{len(case.expect)}）必须等长且非空")


def _actual_of(res: Any) -> dict[str, Any]:
    """从前台的返回里取比对要用的字段；不是 DeskResult、字段类型不对，都抛 TypeError。"""
    if not isinstance(res, DeskResult):
        raise TypeError(f"handle 返回的不是 DeskResult：{type(res).__name__}")
    citations = res.draft.citations
    if not isinstance(res.reply_text, str) or not isinstance(citations, (tuple, list)):
        raise TypeError("DeskResult.reply_text 必须是 str、draft.citations 必须是序列")
    return {"route": res.route, "intent": res.intent, "reason": res.handoff_reason,
            "citations": list(citations), "reply_text": res.reply_text}


def run_eval(desk_factory: Callable[[], Any], cases, *,
             tenant_map: Mapping[str, str] | None = None) -> EvalReport:
    """每个 case 一个新前台、一段新会话，逐轮比对，返回报告。

    前台抛异常、或返回的不是合形状的 DeskResult，都记为该轮 ``error`` 失败、不中断整批。
    case 本身不合形状（turns / expect 不等长、为空、id 重复）是调用方的错，抛 ValueError。
    """
    tmap = dict(DEFAULT_TENANT_MAP if tenant_map is None else tenant_map)
    cases = tuple(cases)
    _check_cases(cases)
    turns = intent_hits = route_hits = 0
    handoff_expected = handoff_caught = 0
    cite_expected = cite_hits = 0
    fabrication = 0
    failures: list[EvalMiss] = []

    for case in cases:
        desk = desk_factory()
        tenant = tmap.get(case.effective_open_kfid, "")
        for i, (text, exp) in enumerate(zip(case.turns, case.expect, strict=True), start=1):
            turns += 1
            problems: list[str] = []
            got: dict[str, Any] | None
            try:
                got = _actual_of(desk.handle(inbound_for(case, i, text)))
            except Exception as exc:                 # noqa: BLE001 —— 评测记失败，不中断
                got = None
                actual: dict[str, Any] = {"error": f"{type(exc).__name__}: {exc}"}
                problems.append("error")
            else:
                actual = {k: got[k] for k in ("route", "intent", "reason", "citations")}

            if exp.route == ROUTE_HANDOFF:
                handoff_expected += 1
            if got is not None:
                if got["intent"] == exp.intent:
                    intent_hits += 1
                else:
                    problems.append("intent")
                route_ok = got["route"] == exp.route and (
                    exp.route != ROUTE_HANDOFF or got["reason"] == exp.reason)
                if route_ok:
                    route_hits += 1
                else:
                    problems.append("route")
                if exp.route == ROUTE_HANDOFF and got["route"] == ROUTE_HANDOFF:
                    handoff_caught += 1
                if _fabricates_status(got["reply_text"]):
                    fabrication += 1
                    problems.append("status_fabrication")
            if exp.cite:
                cite_expected += 1
                if got is not None and cite_doc_id(tenant, exp.cite) in got["citations"]:
                    cite_hits += 1
                elif "error" not in problems:
                    problems.append("cite")
            if problems:
                failures.append(EvalMiss(case_id=case.id, turn=i, text=text,
                                         expected=exp.to_json(), actual=actual,
                                         problems=tuple(problems)))

    return EvalReport(cases=len(cases), turns=turns, intent_hits=intent_hits,
                      route_hits=route_hits, handoff_expected=handoff_expected,
                      handoff_caught=handoff_caught, cite_expected=cite_expected,
                      cite_hits=cite_hits, status_fabrication=fabrication,
                      failures=tuple(failures))
