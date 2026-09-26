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

## p13（review/p13-cs-contracts.md §1.4 T175 / §3）

评测集 ``scenarios/cs/eval/p13_cases.json``：case 可带 ``fixtures``（绑定、订单、预检），
expect 多四个可选键 ``lang`` / ``lookup``（查单结果）/ ``ask``（追问的槽位）/ ``say``（该轮
应当说出的订单状态）。:func:`run_eval_p13` 每个 case 从 fixtures 造三个**夹具端口**
（:class:`FixtureVerifier` / :class:`FixtureLookup` / :class:`FixturePrecheck`，实现
``ports.py`` 的三个 Protocol），以 ``desk_factory({"verifier", "lookup", "precheck"})`` 造前台
（不带 fixtures 的 p12 式用例三个都给 None）。指标在 p12 四项之外：

* ``wording_accuracy`` —— 期望 ``say`` 的轮：回复里恰有一次措辞表那句、没说别的状态、且有一条
  literal 就是那句的 claim 挂着本轮读回的有效观察（:func:`claims.check_observation_wording` 过）；
* ``wrong_status`` —— 回复说的状态与夹具 / 本轮观察对不上的轮数（每个有回复的轮都判，不只 say 轮）：
  1. 说出的状态（:func:`said_statuses`）有不在真值里的 —— 真值 = 本轮读回的观察行里、query_key
     **绑在跑批客户名下**（夹具 bindings）且夹具 orders 里结果 ok 的那些状态（复核 L2-2：跳过核验、
     查了别人那一单再说出来，观察撑得住也算错）；没有观察撑的状态一律算错；**措辞表整句以外**的任何
     状态说法（出门校验认的全部状态字眼，外加评测侧补认的口语说法）记作 :data:`STATUS_OFF_TABLE`，
     永远不在真值里（R3 增量：订单状态只许说措辞表那三句；已签收、送达、到账、时限永远不说）；
  2. 或者第二道出门校验 :func:`claims.check_observation_wording`（按该轮 DeskResult.lang）对**正文里
     真的出现了的** claim 不过：obs 依据的措辞不是所挂观察在本轮语种下那一句、依据的观察本轮没有、
     或同一句出现的次数多于撑它的不同观察（一条观察只撑一处）。literal 不在正文里的 claim 客户看不见，
     不算说了状态（复核 L2-3）。
  上一版还有第 3 条「check_reply 报 foreign_literal」：规则 5 的触发词本身都是状态字眼，literal 在正文里
  时第 1 条已经记 off_table；literal 不在正文里时客户什么状态都没听到，却会被记错 —— 删掉（复核 L2-3）。
* ``status_fabrication`` 改用**本轮读回的观察 id**（p12 是空观察）：从 ``desk.store`` 的
  ``cs_observation`` 按 (租户, 会话, 轮次) 读回（契约 §1.3 的列；W-A 期不 import T171 的 records）。

另记 ``lang_accuracy`` / ``lookup_accuracy`` / ``ask_accuracy`` / ``cite_accuracy``（给了才比）：
p13 门槛文件里没写它们时**只报不拦**（DECISIONS task-t175）。p12 的 :func:`load_cases` /
:func:`run_eval` 行为不变。

## p16（review/p16-cs-contracts.md §2 T189）· 篇级「零自信答错」

两个跑批都另记 ``confident_wrong``：**实得** route=answer 的轮里，意图错、或期望给了 ``cite`` 而
实得引用不含该篇，计 1（一轮最多计 1）。兜底 / 转人工 / 追问 / 静默的轮不计；没给 cite 期望的轮
只看意图；前台出错（error）的轮不计（没有实得出口）。:meth:`EvalReport.confident_wrong_ids` 给出
``<case id>#<轮次>``。它进 ``metrics()`` 与 ``describe()``，但**不进门槛**（THRESHOLD_KEYS /
P13_THRESHOLD_KEYS 与 ``meets`` 的判定不变），由各评测集的测试按 0 钉住。
"""

from __future__ import annotations

import json
import pathlib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from maos.domain.cs import objects
from maos.domain.cs.claims import check_observation_wording, check_reply, reply_status_places
from maos.domain.cs.ports import (
    BINDING_TEST,
    LANG_ZH,
    LANGS,
    LOOKUP_NOT_FOUND,
    LOOKUP_OK,
    LOOKUP_OUTCOMES,
    ORDER_STATUS_WORDING,
    SLOT_KEYS,
    Binding,
    LookupResult,
    PrecheckResult,
)
from maos.domain.cs.types import (
    BASIS_OBS,
    CHANNEL_WECHAT_KF,
    HANDOFF_REASONS,
    INTENTS,
    ROUTE_ANSWER,
    ROUTE_CLARIFY,
    ROUTE_HANDOFF,
    ROUTES,
    VIOLATION_UNBACKED_STATUS,
    CheckResult,
    Claim,
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

#: p13 评测集的位置（仓库根下，p13 契约 §3）。
P13_EVAL_PATH: pathlib.Path = EVAL_PATH.with_name("p13_cases.json")

#: p13 门槛文件的六个键（p13 契约 §3）。
P13_THRESHOLD_KEYS = THRESHOLD_KEYS + ("wording_accuracy", "wrong_status_max")

#: 夹具预检缺项时的拒绝理由（p13 契约 §1.4 T175：缺项 ok=False）。
REFUSED_NOT_IN_LEDGER = "order_not_in_ledger"

#: 措辞表里有对外说法的状态（amended 不在其中：不说、转人工）。
SAYABLE_STATUSES: tuple[str, ...] = tuple(ORDER_STATUS_WORDING[LANG_ZH])

_SCHEME_RE = re.compile(r"^[A-Z]{3}-\d{3}$")


def cite_doc_id(tenant_id: str, scheme_no: str) -> str:
    """方案编号在某租户下的话术 doc_id（契约 §1.5：``kb-cs-<租户>-<编号>``）。"""
    return f"kb-cs-{tenant_id}-{scheme_no}"


# ---------------------------------------------------------------------------
# 数据形状
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class EvalExpect:
    """一轮的期望。``reason`` 只在 ``route == 'handoff'`` 时非空；``cite`` 是方案编号。

    p13 的四个可选键（空串 = 不比）：``lang``（语种）、``lookup``（查单结果）、``ask``
    （追问的槽位，只在 clarify 轮）、``say``（该轮说出的订单状态，只在 answer 轮、查单 ok）。
    """
    route: str
    intent: str
    reason: str = ""
    cite: str = ""
    lang: str = ""
    lookup: str = ""
    ask: str = ""
    say: str = ""

    def to_json(self) -> dict[str, str]:
        d = {"route": self.route, "intent": self.intent}
        for key in ("reason", "cite", "lang", "lookup", "ask", "say"):
            value = getattr(self, key)
            if value:
                d[key] = value
        return d


@dataclass(frozen=True)
class FixtureBinding:
    """夹具里的一条绑定（挂在跑批客户 ``eval-<case.id>`` 名下）。"""
    display_no: str
    system_name: str
    query_key: str


@dataclass(frozen=True)
class FixtureOrder:
    """夹具里一单的查单结果（按 query_key 找）。``outcome == 'ok'`` 时 status 在措辞表里。"""
    outcome: str
    status: str = ""
    version: int = 0
    updated_at: str = ""
    error_kind: str = ""


@dataclass(frozen=True)
class EvalFixtures:
    """一个 case 的外部世界：谁能查哪单、每单查出来是什么、预检怎么答。"""
    bindings: tuple[FixtureBinding, ...] = ()
    orders: tuple[tuple[str, FixtureOrder], ...] = ()          # (query_key, 结果)
    precheck: tuple[tuple[str, PrecheckResult], ...] = ()      # (订单号, 预检结果)

    def binding(self, display_no: str) -> FixtureBinding | None:
        return next((b for b in self.bindings if b.display_no == display_no), None)

    def order(self, query_key: str) -> FixtureOrder | None:
        return next((o for k, o in self.orders if k == query_key), None)

    def precheck_for(self, order_no: str) -> PrecheckResult | None:
        return next((p for k, p in self.precheck if k == order_no), None)


@dataclass(frozen=True)
class EvalCase:
    """一段会话：``turns`` 与 ``expect`` 等长。``open_kfid`` 空串 = 用缺省账号。

    ``fixtures`` 为 None = p12 式用例（不注入端口）。
    """
    id: str
    turns: tuple[str, ...]
    expect: tuple[EvalExpect, ...]
    tags: tuple[str, ...] = ()
    open_kfid: str = ""
    synthetic: bool = True
    fixtures: EvalFixtures | None = None

    @property
    def effective_open_kfid(self) -> str:
        return self.open_kfid or DEFAULT_OPEN_KFID

    @property
    def customer(self) -> str:
        """跑批的客户（external_userid）：绑定一律挂在他名下。"""
        return f"eval-{self.id}"


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
    #: p16 T189：篇级「自信答错」的轮（``<case id>#<轮次>``，按跑批顺序）；计数见 :attr:`confident_wrong`。
    confident_wrong_turns: tuple[str, ...] = ()

    @property
    def confident_wrong(self) -> int:
        """实得 route=answer 的轮里意图错、或给了 cite 期望而引用不含该篇的轮数（只报不拦）。"""
        return len(self.confident_wrong_turns)

    def confident_wrong_ids(self) -> tuple[str, ...]:
        return tuple(self.confident_wrong_turns)

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
                "cite_accuracy": self.cite_accuracy,
                "confident_wrong": self.confident_wrong}

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
        if self.confident_wrong_turns:
            lines.append("  confident_wrong: " + " ".join(self.confident_wrong_turns))
        for miss in self.failures:
            lines.append(f"  {miss.case_id}#{miss.turn} {','.join(miss.problems)}"
                         f" expected={miss.expected} actual={miss.actual}")
        return "\n".join(lines)


#: p13 另记、门槛文件里没写就只报不拦的几项（给了期望才比）。
P13_INFO_KEYS = ("cite_accuracy", "lang_accuracy", "lookup_accuracy", "ask_accuracy")


@dataclass(frozen=True)
class EvalReportP13(EvalReport):
    """p13 跑批的报告：p12 四项 + ``wording_accuracy`` + ``wrong_status``（+ 只报不拦的几项）。

    ``failures`` 的 problems 另可含 lang / lookup / ask / wording / wrong_status。
    """
    wording_expected: int = 0
    wording_hits: int = 0
    wrong_status: int = 0
    lang_expected: int = 0
    lang_hits: int = 0
    lookup_expected: int = 0
    lookup_hits: int = 0
    ask_expected: int = 0
    ask_hits: int = 0

    @property
    def wording_accuracy(self) -> float:
        return _ratio(self.wording_hits, self.wording_expected)

    @property
    def lang_accuracy(self) -> float:
        return _ratio(self.lang_hits, self.lang_expected)

    @property
    def lookup_accuracy(self) -> float:
        return _ratio(self.lookup_hits, self.lookup_expected)

    @property
    def ask_accuracy(self) -> float:
        return _ratio(self.ask_hits, self.ask_expected)

    def metrics(self) -> dict[str, float | int]:
        return {**super().metrics(),
                "wording_accuracy": self.wording_accuracy,
                "wrong_status": self.wrong_status,
                "lang_accuracy": self.lang_accuracy,
                "lookup_accuracy": self.lookup_accuracy,
                "ask_accuracy": self.ask_accuracy}

    def shortfalls(self, thresholds: Mapping[str, Any]) -> list[str]:
        """p13 契约 §3 的六个门槛（缺省按最严）；另几项只在门槛里点了名才拦。

        与 p12 不同：``cite_accuracy`` 不再缺省按 1.0 拦 —— p13 的意图 / 出口门槛是 0.9，
        错一轮出口多半连带错引用 / 查单结果，缺省 1.0 会让 0.9 的门槛形同虚设。
        """
        out: list[str] = []
        if self.turns == 0:
            out.append(f"turns=0（cases={self.cases}）：评测空转，一轮都没跑")
        for key in ("intent_accuracy", "route_accuracy", "handoff_recall", "wording_accuracy"):
            want = float(thresholds.get(key, 1.0))
            got = getattr(self, key)
            if got < want:
                out.append(f"{key}={got:.4f} < {want}")
        for key, got in (("status_fabrication", self.status_fabrication),
                         ("wrong_status", self.wrong_status)):
            cap = int(thresholds.get(f"{key}_max", 0))
            if got > cap:
                out.append(f"{key}={got} > {cap}")
        for key in P13_INFO_KEYS:
            if key in thresholds:
                want = float(thresholds[key])
                got = getattr(self, key)
                if got < want:
                    out.append(f"{key}={got:.4f} < {want}")
        return out


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
    lang = str(raw.get("lang", "") or "")
    lookup = str(raw.get("lookup", "") or "")
    ask = str(raw.get("ask", "") or "")
    say = str(raw.get("say", "") or "")
    if lang and lang not in LANGS:
        raise ValueError(f"{where} lang={lang!r} 不在 {LANGS}")
    if lookup and lookup not in LOOKUP_OUTCOMES:
        raise ValueError(f"{where} lookup={lookup!r} 不在 {LOOKUP_OUTCOMES}")
    if ask and ask not in SLOT_KEYS:
        raise ValueError(f"{where} ask={ask!r} 不在 {SLOT_KEYS}")
    if (route == ROUTE_CLARIFY) != bool(ask):
        raise ValueError(f"{where} clarify 轮必须带 ask、别的轮不许带（route={route}, ask={ask!r}）")
    if say:
        if say not in ORDER_STATUS_WORDING[lang or LANG_ZH]:
            raise ValueError(f"{where} say={say!r} 在措辞表里没有对外说法")
        if route != ROUTE_ANSWER or lookup != LOOKUP_OK:
            raise ValueError(f"{where} say 只许出现在 answer 轮且 lookup=ok")
    return EvalExpect(route=route, intent=intent, reason=reason, cite=cite,
                      lang=lang, lookup=lookup, ask=ask, say=say)


def _str_field(where: str, raw: Mapping[str, Any], key: str, *, required: bool) -> str:
    value = raw.get(key, "")
    if not isinstance(value, str) or (required and not value):
        raise ValueError(f"{where}.{key} 必须是{'非空' if required else ''}字符串，收到 {value!r}")
    return value


def _parse_fixtures(case_id: str, raw: Any) -> EvalFixtures:
    """``fixtures`` 的形状（p13 契约 §3）：bindings 数组、orders / precheck 两张表，都可缺省。"""
    where = f"{case_id} fixtures"
    if not isinstance(raw, dict) or not set(raw) <= {"bindings", "orders", "precheck"}:
        raise ValueError(f"{where} 必须是对象，键 ⊆ bindings / orders / precheck")
    bindings: list[FixtureBinding] = []
    raw_bindings = raw.get("bindings", [])
    if not isinstance(raw_bindings, list):
        raise ValueError(f"{where}.bindings 必须是数组")
    for i, b in enumerate(raw_bindings):
        w = f"{where}.bindings[{i}]"
        if not isinstance(b, dict) or set(b) != {"display_no", "system_name", "query_key"}:
            raise ValueError(f"{w} 键必须恰好是 display_no / system_name / query_key")
        bindings.append(FixtureBinding(*(_str_field(w, b, k, required=True)
                                         for k in ("display_no", "system_name", "query_key"))))
    if len({b.display_no for b in bindings}) != len(bindings):
        raise ValueError(f"{where}.bindings 的 display_no 重复")

    orders: list[tuple[str, FixtureOrder]] = []
    raw_orders = raw.get("orders", {})
    if not isinstance(raw_orders, dict):
        raise ValueError(f"{where}.orders 必须是对象（query_key → 结果）")
    for key, o in raw_orders.items():
        w = f"{where}.orders[{key!r}]"
        if (not key or not isinstance(o, dict)
                or not set(o) <= {"outcome", "status", "version", "updated_at", "error_kind"}):
            raise ValueError(f"{w} 形状不对")
        outcome = _str_field(w, o, "outcome", required=True)
        status = _str_field(w, o, "status", required=False)
        if outcome not in LOOKUP_OUTCOMES:
            raise ValueError(f"{w} outcome={outcome!r} 不在 {LOOKUP_OUTCOMES}")
        if outcome == LOOKUP_OK and status not in SAYABLE_STATUSES:
            raise ValueError(f"{w} ok 的 status 必须在措辞表里（{SAYABLE_STATUSES}），收到 {status!r}")
        if outcome != LOOKUP_OK and status:
            raise ValueError(f"{w} 非 ok 的结果不带 status")
        version = o.get("version", 0)
        if not isinstance(version, int) or isinstance(version, bool):
            raise ValueError(f"{w}.version 必须是整数")
        orders.append((key, FixtureOrder(
            outcome=outcome, status=status, version=version,
            updated_at=_str_field(w, o, "updated_at", required=False),
            error_kind=_str_field(w, o, "error_kind", required=False))))

    prechecks: list[tuple[str, PrecheckResult]] = []
    raw_pre = raw.get("precheck", {})
    if not isinstance(raw_pre, dict):
        raise ValueError(f"{where}.precheck 必须是对象（订单号 → 预检结果）")
    keys = {"ok", "decision", "rule_ref", "reason_code", "command_line", "summary", "refused_why"}
    for order_no, p in raw_pre.items():
        w = f"{where}.precheck[{order_no!r}]"
        if not order_no or not isinstance(p, dict) or not set(p) <= keys or not isinstance(
                p.get("ok"), bool):
            raise ValueError(f"{w} 形状不对（ok 必须是布尔）")
        fields = {k: _str_field(w, p, k, required=False) for k in sorted(keys - {"ok"})}
        if p["ok"]:
            if not fields["reason_code"] or fields["refused_why"]:
                raise ValueError(f"{w} ok=true 必须带 reason_code、不许带 refused_why")
            # 契约 §1.4 T172：command_line 形如「/refund <订单号> <原因码>」；夹具没写就按这个形状补
            fields["command_line"] = (fields["command_line"]
                                      or f"/refund {order_no} {fields['reason_code']}")
        elif not fields["refused_why"] or fields["command_line"]:
            raise ValueError(f"{w} ok=false 必须带 refused_why、不许带 command_line")
        prechecks.append((order_no, PrecheckResult(ok=p["ok"], **fields)))
    return EvalFixtures(bindings=tuple(bindings), orders=tuple(orders), precheck=tuple(prechecks))


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
            fixtures=(_parse_fixtures(case_id, raw["fixtures"]) if "fixtures" in raw else None),
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


def _confident_wrong(exp: EvalExpect, got: Mapping[str, Any], tenant: str) -> bool:
    """p16 T189：实得 route=answer，且意图错、或期望给了 cite 而实得引用不含该篇。"""
    if got["route"] != ROUTE_ANSWER:
        return False
    if got["intent"] != exp.intent:
        return True
    return bool(exp.cite) and cite_doc_id(tenant, exp.cite) not in got["citations"]


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
    confident_wrong: list[str] = []

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
                if _confident_wrong(exp, got, tenant):
                    confident_wrong.append(f"{case.id}#{i}")
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
                      failures=tuple(failures), confident_wrong_turns=tuple(confident_wrong))


# ===========================================================================
# p13：夹具端口（实现 ports.py 的三个 Protocol，从 case.fixtures 构造）
# ===========================================================================
class FixtureVerifier:
    """IdentityVerifier：只认挂在本 case 跑批客户名下的绑定，按 (租户, 渠道, 客户, 单号) 精确匹配。

    失败即关：租户为空、任何一项对不上、单号不在绑定里，都返回 None。
    """

    def __init__(self, fixtures: EvalFixtures, *, tenant_id: str, external_userid: str,
                 channel: str = CHANNEL_WECHAT_KF):
        self.fixtures = fixtures
        self.tenant_id = tenant_id
        self.external_userid = external_userid
        self.channel = channel
        self.calls: list[dict[str, str]] = []

    def resolve(self, store: Any, *, tenant_id: str, channel: str, external_userid: str,
                display_no: str) -> Binding | None:
        self.calls.append({"tenant_id": tenant_id, "channel": channel,
                           "external_userid": external_userid, "display_no": display_no})
        if not self.tenant_id or (tenant_id, channel, external_userid) != (
                self.tenant_id, self.channel, self.external_userid):
            return None
        found = self.fixtures.binding(display_no)
        if found is None:
            return None
        return Binding(tenant_id=tenant_id, channel=channel, external_userid=external_userid,
                       display_no=found.display_no, system_name=found.system_name,
                       query_key=found.query_key, source=BINDING_TEST)


class FixtureLookup:
    """OrderLookup：恰好按 ``fixtures.orders[binding.query_key]`` 作答；表里没有 = not_found。永不抛。"""

    def __init__(self, fixtures: EvalFixtures):
        self.fixtures = fixtures
        self.calls: list[dict[str, str]] = []

    def lookup(self, store: Any, binding: Binding, *, plan_id: str,
               task_id: str) -> LookupResult:
        self.calls.append({"display_no": binding.display_no, "query_key": binding.query_key,
                           "plan_id": plan_id, "task_id": task_id})
        order = self.fixtures.order(binding.query_key)
        if order is None:
            return LookupResult(outcome=LOOKUP_NOT_FOUND, system_name=binding.system_name,
                                query_key=binding.query_key, error_kind="KeyError")
        return LookupResult(outcome=order.outcome, system_name=binding.system_name,
                            query_key=binding.query_key, status=order.status,
                            version=order.version, updated_at=order.updated_at,
                            error_kind=order.error_kind)


class FixturePrecheck:
    """RefundPrecheck：按 ``fixtures.precheck[order_no]`` 作答；缺项 ok=False、order_not_in_ledger。"""

    def __init__(self, fixtures: EvalFixtures):
        self.fixtures = fixtures
        self.calls: list[dict[str, str]] = []

    def precheck(self, *, tenant_id: str, order_no: str, reason_text: str,
                 now: str) -> PrecheckResult:
        self.calls.append({"tenant_id": tenant_id, "order_no": order_no,
                           "reason_text": reason_text, "now": now})
        found = self.fixtures.precheck_for(order_no)
        return found if found is not None else PrecheckResult(
            ok=False, refused_why=REFUSED_NOT_IN_LEDGER)


def fixture_ports(case: EvalCase, *, tenant_id: str) -> dict[str, Any]:
    """一个 case 的三个端口。不带 fixtures 的 p12 式用例三个都是 None（前台走 p12 路径）。"""
    if case.fixtures is None:
        return {"verifier": None, "lookup": None, "precheck": None}
    return {"verifier": FixtureVerifier(case.fixtures, tenant_id=tenant_id,
                                        external_userid=case.customer),
            "lookup": FixtureLookup(case.fixtures),
            "precheck": FixturePrecheck(case.fixtures)}


# ===========================================================================
# p13：跑批
# ===========================================================================
_EN_BEFORE_T175 = r"(?<![A-Za-z])"
_EN_AFTER_T175 = r"(?![A-Za-z])"

#: 措辞表那几句之外、回复里「说出了某个订单状态」的说法（中文要带「已」，英文按词）。
_SAID_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("paid", re.compile(r"已经?(?:为您)?(?:付款|支付)")),
    ("paid", re.compile(_EN_BEFORE_T175 + r"(?:is|was|are|were|been|already|fully)\s+paid"
                        + _EN_AFTER_T175, re.IGNORECASE)),
    ("shipped", re.compile(r"已经?(?:为您)?(?:发货|发出|寄出)")),
    ("shipped", re.compile(_EN_BEFORE_T175 + r"(?:shipped|dispatched)" + _EN_AFTER_T175,
                           re.IGNORECASE)),
    ("cancelled", re.compile(r"已经?(?:为您)?取消")),
    ("cancelled", re.compile(_EN_BEFORE_T175 + r"cancell?ed" + _EN_AFTER_T175, re.IGNORECASE)),
)
#: 英文说法前面紧挨着这些就是否定（「has not shipped yet」不是说已发货）。
_EN_NEGATIONS = ("not ", "n't ", "n’t ", "not yet ", "never ", "not been ", "n't been ", "n’t been ")

#: 措辞表整句以外的状态说法：永远不在真值里（R3 增量 —— 只许说措辞表那三句）。
STATUS_OFF_TABLE = "off_table"

#: 评测侧补认的中文口语状态说法（出门校验 check_reply 还不认，p12 遗留、p14 统一口径 ——
#: BACKLOG task-t175）。只在评测里判、一律记 :data:`STATUS_OFF_TABLE`；在去掉空白的正文上匹配。
#: 话术库每篇 script 都不许命中（test_cs_eval_runner_t175 钉住），免得评测冤枉正当的政策回答。
#: 复核 L3R2-4 的那批（已妥投 / 已派件 / 已完成 / 已经到了 / 已经退给您 / 已经在派送，以及英文
#: left our warehouse / picked it up / signed for / received your payment ……）进了出门校验
#: （claims.P13_ZH_STATUS_PATTERNS / EN_STATUS_PATTERNS），评测经 reply_status_places 同口径认，不在这里抄第二份。
_OFF_TABLE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"已经?(?:(?:为|给|帮|替)您)?(?:发|寄)(?:了|走了|出去了)"),     # 已经发了 / 已寄走了
    re.compile(r"(?:发|寄)出去了(?![吗没么嘛])"),
    re.compile(r"在路上(?:了|啦)|正在路上"),
    re.compile(r"正在(?:派送|配送|运输|发货|出库|打包)|(?:派送|配送|运输)中(?![心转])"),
    re.compile(r"(?:今天|今晚|明天|明日|后天|马上|很快)就?(?:能|会|可以)?(?:到|送到|送达|到货|发货|发出)"),
    re.compile(r"退款成功|已经?(?:成功|被)取消|被取消了"),
)


def _said_form(text: str) -> str:
    """said_statuses 扫描用的规范化：NFKC、去格式字符（零宽空格等）。正文与措辞表整句都过这一道。"""
    return "".join(c for c in unicodedata.normalize("NFKC", text or "")
                   if unicodedata.category(c) != "Cf")


def said_statuses(text: str) -> frozenset[str]:
    """回复说出了哪些订单状态：措辞表的中英各句（整句）→ 该状态；其余说法按下面归类。

    先把措辞表整句挖掉再扫（英文「已付款」那句里有「has not shipped yet」，不能算说了已发货）：

    * 「已发货 / shipped / 已取消 / is paid」一类散说法 → 对应的状态（英文紧邻否定的不算该状态）；
    * 出门校验认的任何状态字眼（:func:`claims.reply_status_places`，同一口径），没对上上面三种状态的
      —— 已签收、送达、到账、退款说法、时限、否定句（「has not shipped yet」）…… → :data:`STATUS_OFF_TABLE`；
    * 评测侧补认的中文口语说法（:data:`_OFF_TABLE_PATTERNS`：已经发了、在路上了、明天就能到……）
      → :data:`STATUS_OFF_TABLE`。
    """
    remaining = _said_form(text)
    found: set[str] = set()
    for table in ORDER_STATUS_WORDING.values():
        for status, raw_sentence in table.items():
            # 整句与正文同一种规范化再比（复核 L2-5：NFKC 把中文句里的全角，（）折成半角，
            # 拿原句去找会永远找不到、挖不掉）
            sentence = _said_form(raw_sentence)
            if sentence in remaining:
                found.add(status)
                remaining = remaining.replace(sentence, " ")
    mapped: list[tuple[int, int]] = []
    for status, pattern in _SAID_PATTERNS:
        for m in pattern.finditer(remaining):
            before = remaining[max(0, m.start() - 9):m.start()].lower()
            if pattern.flags & re.IGNORECASE and before.endswith(_EN_NEGATIONS):
                continue
            found.add(status)
            mapped.append((m.start(), m.end()))
    for start, end, _ in reply_status_places(remaining):
        if not any(s < end and start < e for s, e in mapped):
            found.add(STATUS_OFF_TABLE)
    compact = "".join(remaining.split())
    if any(p.search(compact) for p in _OFF_TABLE_PATTERNS):
        found.add(STATUS_OFF_TABLE)
    return frozenset(found)


def _actual_of_p13(res: Any) -> dict[str, Any]:
    """p12 的字段 + p13 的 lang / lookup_outcome / ask_slot / claims 与归属；类型不对抛 TypeError。"""
    base = _actual_of(res)
    claims = res.draft.claims
    if (not all(isinstance(getattr(res, k), str)
                for k in ("lang", "lookup_outcome", "ask_slot", "tenant_id",
                          "conversation_id", "turn_id"))
            or not isinstance(claims, (tuple, list))
            or not all(isinstance(c, Claim) and isinstance(c.literal, str)
                       and isinstance(c.basis_ref, str) for c in claims)):
        # claim 的 literal / basis_ref 不是 str 也在这里拦（复核 L2-4）：放过去的话后面的校验在 try
        # 外面抛，整批跑批崩掉、一份报告都没有
        raise TypeError("DeskResult 的 p13 字段类型不对（lang / lookup_outcome / ask_slot / claims，"
                        "claim 的 literal 与 basis_ref 必须是 str）")
    return {**base, "lang": res.lang, "lookup": res.lookup_outcome, "ask": res.ask_slot,
            "claims": tuple(claims), "tenant_id": res.tenant_id,
            "conversation_id": res.conversation_id, "turn_id": res.turn_id}


def turn_observations(store: Any, *, tenant_id: str, conversation_id: str,
                      turn_id: str) -> dict[str, dict[str, Any]]:
    """从前台的库里读回本轮的观察行（``cs_observation``，p13 契约 §1.3）：``id -> 行``。

    没有 store、没有这张表（p12 的库）、读不出来：一律当本轮**没有观察**（失败即关 ——
    这时回复里的任何状态都撑不住）。
    """
    if store is None or not turn_id:
        return {}
    try:
        rows = objects.query(
            store, "SELECT * FROM cs_observation WHERE tenant_id = ? AND conversation_id = ?"
                   " AND turn_id = ?", (tenant_id, conversation_id, turn_id))
    except Exception:                                  # noqa: BLE001 —— 读不回 = 没有观察
        return {}
    return {str(r["observation_id"]): dict(r) for r in rows if r.get("observation_id")}


def _draft_of(got: Mapping[str, Any]) -> ReplyDraft:
    """最终回复 + 它的 claims（评测只看客户实际收到的正文与撑它的依据）。"""
    return ReplyDraft(text=got["reply_text"] or "", claims=got["claims"])


def _gate_check_p13(got: Mapping[str, Any], rows: Mapping[str, Any]) -> CheckResult:
    """第一道出门校验在**本轮读回的观察 id** 下的结果（kb_doc_ids 评测拿不到，按空算，只看状态类违例）。"""
    return check_reply(_draft_of(got), observations=frozenset(rows), kb_doc_ids=frozenset())


def _fabricates_status_p13(gate: CheckResult) -> bool:
    """status_fabrication：本轮观察 id 下 check_reply 报 unbacked_status（口径同 p12，观察换成读回的）。"""
    return any(v.kind == VIOLATION_UNBACKED_STATUS for v in gate.violations)


def _status_truth(rows: Mapping[str, Mapping[str, Any]],
                  fixtures: EvalFixtures | None) -> set[str]:
    """本轮能说的状态：读回的观察行里 query_key 绑在跑批客户名下（夹具 bindings）、且夹具 orders 里
    结果 ok 的那些状态。没绑的单（跳过核验查了别人那一单）撑不起任何状态（复核 L2-2）。"""
    truth: set[str] = set()
    if fixtures is None:
        return truth
    bound = {b.query_key for b in fixtures.bindings}
    for row in rows.values():
        key = str(row.get("query_key") or "")
        order = fixtures.order(key) if key in bound else None
        if order is not None and order.outcome == LOOKUP_OK:
            truth.add(order.status)
    return truth


def _wrong_status(got: Mapping[str, Any], rows: Mapping[str, Mapping[str, Any]],
                  fixtures: EvalFixtures | None) -> bool:
    """wrong_status 的两条（模块文档 p13 节）：说出的状态不在真值里（措辞表以外的说法记
    OFF_TABLE、永远不在真值里；真值只认绑在跑批客户名下的单）；或 check_observation_wording 按本轮
    语种对正文里真的出现了的 claim 不过。没有观察撑的状态一律算错。"""
    text = got["reply_text"] or ""
    if said_statuses(text) - _status_truth(rows, fixtures):
        return True
    shown = tuple(c for c in got["claims"] if c.literal and c.literal in text)
    return not check_observation_wording(ReplyDraft(text=text, claims=shown), rows,
                                         lang=got["lang"]).ok


def _wording_ok(exp: EvalExpect, got: Mapping[str, Any],
                rows: Mapping[str, Mapping[str, Any]]) -> bool:
    """期望 say 的轮：恰有一次那句、没说别的状态、那句挂着本轮观察且措辞对得上观察。"""
    lang = exp.lang or LANG_ZH
    sentence = ORDER_STATUS_WORDING[lang][exp.say]
    text = got["reply_text"] or ""
    if text.count(sentence) != 1 or said_statuses(text) != {exp.say}:
        return False
    claims = got["claims"]
    backed = any(
        c.literal == sentence and c.basis_ref.startswith(BASIS_OBS)
        and str((rows.get(c.basis_ref[len(BASIS_OBS):]) or {}).get("status") or "") == exp.say
        for c in claims)
    draft = ReplyDraft(text=text, claims=claims)
    return backed and check_observation_wording(draft, rows, lang=lang).ok


def run_eval_p13(desk_factory: Callable[[Mapping[str, Any]], Any], cases, *,
                 tenant_map: Mapping[str, str] | None = None) -> EvalReport:
    """p13 跑批：每个 case 从 fixtures 造三个夹具端口、``desk_factory(ports)`` 造一个新前台，
    逐轮比对，返回 :class:`EvalReportP13`。

    前台对象要有 ``.store``（观察从那里读回）；没有就当每轮都没有观察。其余约定同 :func:`run_eval`：
    前台抛异常 / 返回不合形状记该轮 ``error``；case 不合形状抛 ValueError、一个前台都不造。
    """
    tmap = dict(DEFAULT_TENANT_MAP if tenant_map is None else tenant_map)
    cases = tuple(cases)
    _check_cases(cases)
    n: dict[str, int] = {k: 0 for k in (
        "turns", "intent", "route", "handoff_expected", "handoff_caught", "cite_expected", "cite",
        "fabrication", "wording_expected", "wording", "wrong_status", "lang_expected", "lang",
        "lookup_expected", "lookup", "ask_expected", "ask")}
    failures: list[EvalMiss] = []
    confident_wrong: list[str] = []

    for case in cases:
        tenant = tmap.get(case.effective_open_kfid, "")
        desk = desk_factory(fixture_ports(case, tenant_id=tenant))
        store = getattr(desk, "store", None)
        for i, (text, exp) in enumerate(zip(case.turns, case.expect, strict=True), start=1):
            n["turns"] += 1
            problems: list[str] = []
            got: dict[str, Any] | None
            try:
                got = _actual_of_p13(desk.handle(inbound_for(case, i, text)))
            except Exception as exc:                 # noqa: BLE001 —— 评测记失败，不中断
                got = None
                actual: dict[str, Any] = {"error": f"{type(exc).__name__}: {exc}"}
                problems.append("error")
            else:
                actual = {k: got[k] for k in ("route", "intent", "reason", "citations",
                                               "lang", "lookup", "ask")}

            if exp.route == ROUTE_HANDOFF:
                n["handoff_expected"] += 1
            for key, want in (("cite", exp.cite), ("lang", exp.lang), ("lookup", exp.lookup),
                              ("ask", exp.ask), ("wording", exp.say)):
                if want:
                    n[f"{key}_expected"] += 1
            if got is not None:
                if got["intent"] == exp.intent:
                    n["intent"] += 1
                else:
                    problems.append("intent")
                if got["route"] == exp.route and (
                        exp.route != ROUTE_HANDOFF or got["reason"] == exp.reason):
                    n["route"] += 1
                else:
                    problems.append("route")
                if exp.route == ROUTE_HANDOFF and got["route"] == ROUTE_HANDOFF:
                    n["handoff_caught"] += 1
                rows = turn_observations(store, tenant_id=got["tenant_id"],
                                         conversation_id=got["conversation_id"],
                                         turn_id=got["turn_id"])
                gate = _gate_check_p13(got, rows)
                if _fabricates_status_p13(gate):
                    n["fabrication"] += 1
                    problems.append("status_fabrication")
                if _wrong_status(got, rows, case.fixtures):
                    n["wrong_status"] += 1
                    problems.append("wrong_status")
                if _confident_wrong(exp, got, tenant):
                    confident_wrong.append(f"{case.id}#{i}")
                checks = (
                    ("cite", exp.cite, lambda: cite_doc_id(tenant, exp.cite) in got["citations"]),
                    ("lang", exp.lang, lambda: got["lang"] == exp.lang),
                    ("lookup", exp.lookup, lambda: got["lookup"] == exp.lookup),
                    ("ask", exp.ask, lambda: got["ask"] == exp.ask),
                    ("wording", exp.say, lambda: _wording_ok(exp, got, rows)),
                )
                for key, want, ok in checks:
                    if not want:
                        continue
                    if ok():
                        n[key] += 1
                    else:
                        problems.append(key)
            if problems:
                failures.append(EvalMiss(case_id=case.id, turn=i, text=text,
                                         expected=exp.to_json(), actual=actual,
                                         problems=tuple(problems)))

    return EvalReportP13(
        cases=len(cases), turns=n["turns"], intent_hits=n["intent"], route_hits=n["route"],
        handoff_expected=n["handoff_expected"], handoff_caught=n["handoff_caught"],
        cite_expected=n["cite_expected"], cite_hits=n["cite"],
        status_fabrication=n["fabrication"], failures=tuple(failures),
        confident_wrong_turns=tuple(confident_wrong),
        wording_expected=n["wording_expected"], wording_hits=n["wording"],
        wrong_status=n["wrong_status"], lang_expected=n["lang_expected"], lang_hits=n["lang"],
        lookup_expected=n["lookup_expected"], lookup_hits=n["lookup"],
        ask_expected=n["ask_expected"], ask_hits=n["ask"])
