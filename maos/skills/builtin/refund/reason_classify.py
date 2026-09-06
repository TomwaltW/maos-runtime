"""refund.reason_classify —— 把客户自由文本的退款诉求归一成 reason_code。

## 为什么要有这个 skill

`scripts/run_requests.py::_reason_code()` 只认一张 14 个词的中文表，词表外的写法
（「漏发了两个」「货不对板」「尺寸不合适」）**整行拒收，建不出案**。这是「申请受理岗
建案不准」的头号原因。

## 模型只做观察，判定仍然是规则

    模型读文本  ->  产出结构化标注 {reason_code, confidence, why}
    标注落库    ->  带 source / model / invocation_id，可回看、可人工推翻
    规则读标注  ->  照旧 if/else 判裁定
    低置信度    ->  转人工，不猜

可复现性从「输入→结果」挪到「标注→结果」。模型的理解是**观察与推断**，正是铁律 8
说 MAOS 该持有的东西。反过来若让模型直接吐 `decision`，客户问「为什么驳回」就答不
上来，明天重跑还可能翻。

## 词表是快路径，模型是兜底 —— 顺序不能反

命中词表的**一次模型都不调**：快、免费、逐字节可复现。只有词表外的才走模型。
常规输入跑完全程零模型调用，这是刻意的，不是优化。

## 失败姿态（口径同 `builtin/req_normalize.py`，本仓库唯一的 skill 调模型先例）

  · `ctx.model is None` → 规则兜底，**不失败**。上层忘了接线不该把链路拖挂。
  · 调模型抛异常 → `record_model_failure(...)` 落账后**原样 raise**。
  · 输出不是合法 JSON / 缺 `reason_code` → **抛 ValueError**，交给
    `failure_policy="retry"`。静默降级会把「模型坏了」伪装成「数据就长这样」。
  · 模型返回了枚举外的取值 → **不抛**，收敛成 `unknown`，理由写进 `why`。

最后一条与前两条不矛盾：JSON 坏了是**故障**；模型选了个不存在的选项是**它没听话**，
后者已经有安全出口（`unknown`），抛出去只会让整条链路为一次不听话停摆。

## 纯函数式

只返回结构，一行业务 SQL 都不写（`record_model_usage` / `record_model_failure`
除外 —— 那是模型账，不是业务数据）。
"""

from __future__ import annotations

import json
import time
from typing import Any

from maos.core.store import record_model_failure, record_model_usage
from maos.model.client import Tier
from maos.skills.contract import Skill, SkillContext, SkillContract
from maos.skills.registry import register_skill

from . import _common as C

#: 落 ``model_usage`` 时写进 ``call_site`` 列的值。
#: 新增调用点必须同步登记到 ``maos/obs/call_sites.py``（T48 的穷举守卫）。
CALL_SITE = "maos/skills/builtin/refund/reason_classify.py::RefundReasonClassifySkill.run"

#: 判不出来时的取值。**不是候选之一** —— 它表示「这条要转人工」，
#: 而不是「归到某个兜底类目里」。混在一起的话，人工队列就永远是空的。
UNKNOWN = "unknown"

#: 老板/客服会写的说法 -> 系统里的诉求类型。逐字搬自
#: ``scripts/run_requests.py::REASONS``（14 项 -> 3 个 code）。
#:
#: **这里重新定义一份，不 import scripts/** —— 包不该依赖脚本目录。两份并存是
#: 预期内的过渡态：整合期由主会话把 `run_requests.py` 改成 import 本定义，
#: 再删掉脚本里的副本。
LEXICON: dict[str, str] = {
    "质量问题": "quality_defect", "质量缺陷": "quality_defect", "有质量问题": "quality_defect",
    "坏了": "quality_defect", "损坏": "quality_defect",
    "七天无理由": "no_reason_return", "无理由": "no_reason_return",
    "无理由退货": "no_reason_return", "不想要了": "no_reason_return",
    "买错了": "no_reason_return", "买错型号": "no_reason_return",
    "发错货": "wrong_item", "发错型号": "wrong_item", "错发": "wrong_item",
}

#: 内置候选。调用方不给 `candidates` 就用这三个。
#: `hint` 是写进 SYSTEM prompt 的中文含义 —— 模型只看得见这一句，写含糊了它就猜。
DEFAULT_CANDIDATES: tuple[dict[str, str], ...] = (
    {"code": "quality_defect", "label": "质量问题",
     "hint": "商品本身有质量问题、损坏、缺陷"},
    {"code": "no_reason_return", "label": "七天无理由",
     "hint": "七天无理由退货，客户主观不想要了、买错了"},
    {"code": "wrong_item", "label": "发错货",
     "hint": "商家发错货、发错型号、错发漏发"},
)

SYSTEM_HEAD = """你是退款诉求分类助手。读客户或客服写的一段中文原文，判断它属于哪一类退款诉求。

可选的类别只有下面这些："""

SYSTEM_TAIL = f"""
判不出来、或原文信息不足以支持任何一类时，输出 "{UNKNOWN}"，不要猜。

只输出 JSON，不要任何解释文字，格式：
{{"reason_code":"...","confidence":0.0,"why":"..."}}
reason_code 只能是上面列出的类别之一或 "{UNKNOWN}"；confidence 是 0 到 1 之间的小数，
表示你有多确信；why 用一句中文说明判断依据，给人看的。"""


def _candidate_list(payload: dict) -> list[dict]:
    """取候选类目：调用方给了就用调用方的，没给用内置三个。

    逐项过一遍形状 —— `code` 空的项直接丢掉：它进了 prompt 只会让模型看到一个
    没有名字的选项，而进了合法集合就等于放行一个空 code 写进业务表。
    """
    raw = payload.get("candidates")
    if not isinstance(raw, (list, tuple)) or not raw:
        return [dict(c) for c in DEFAULT_CANDIDATES]
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip()
        if not code:
            continue
        out.append({
            "code": code,
            "label": str(item.get("label") or code).strip(),
            "hint": str(item.get("hint") or "").strip(),
        })
    return out or [dict(c) for c in DEFAULT_CANDIDATES]


def _clamp(value: Any) -> float:
    """置信度收敛到 [0,1]。不是数字按 0.0 —— **不抛**。

    模型把 confidence 写成「高」是它没听话，不是故障；而 0.0 会把这条推给人工，
    正是「判不准就别猜」该有的结果。抛出去只会让整条链路为一次不听话停摆。
    """
    try:
        num = float(value)
    except (TypeError, ValueError):
        return 0.0
    if num != num:                                   # NaN：任何比较都是 False，
        return 0.0                                   # clamp 拦不住，单独挡一道
    return min(1.0, max(0.0, num))


@register_skill
class RefundReasonClassifySkill(Skill):
    contract = SkillContract(
        name="refund.reason_classify",
        version="1.0.0",
        purpose="把客户自由文本的退款诉求归一成 reason_code，附置信度与理由",
        input_schema={
            "text": "str（客户/客服写的原文；空串按 unknown 处理，不抛）",
            "candidates": "list[dict{code,label,hint}]?（不给就用内置三个 code）",
        },
        output_schema={
            "reason_code": "str（候选 code 之一，或 unknown）",
            "confidence": "float（0.0~1.0）",
            "why": "str（一句话中文理由，给人看）",
            "source": "str（lexicon | model | fallback）",
            "raw_text": "str（原文回显）",
            "invocation_id": "str（本次调用的 actor 锚点）",
        },
        preconditions=["text"],
        depends_tools=[],
        # 模型输出不合契约是可重试的失败形态（口径同 req.normalize）。
        failure_policy="retry",
        max_retries=1,
        security_boundary="只读入参，不写业务表、不碰网关；词表命中不调模型",
        reuse_note="申请受理岗认不出诉求类型时的统一入口；判定仍归规则，本 skill 只产标注",
        owner_roles=["refund_intake"],
    )

    def run(self, payload: dict, ctx: SkillContext) -> Any:
        text = str(payload.get("text") or "").strip()
        candidates = _candidate_list(payload)
        invocation_id = C.invocation_id_of(ctx)

        hit = self._lexicon_hit(text, candidates)
        if hit is not None:
            return self._out(hit, 1.0, "词表直接命中", "lexicon", text, invocation_id)

        if ctx.model is None:
            return self._out(UNKNOWN, 0.0, "无模型可用，且词表未命中",
                             "fallback", text, invocation_id)

        data = self._ask_model(text, candidates, ctx)
        code, confidence, why = self._coerce(data, candidates)
        return self._out(code, confidence, why, "model", text, invocation_id)

    # ------------------------------------------------------------------ 词表
    @staticmethod
    def _lexicon_hit(text: str, candidates: list[dict]) -> str | None:
        """词表快路径。命中返回 code，否则 None。**一次模型都不调。**

        两条判据（口径同 `run_requests.py::_reason_code`）：原文是词表里的一个词，
        或原文本身就是一个合法 code（英文 code 直接写也认）。

        命中的 code 还要落在当前候选集里才算数 —— 调用方传了自定义 candidates 时，
        内置词表可能映射到一个它没列出的 code，直接返回等于产出一个候选外的取值。
        默认候选就是内置三个，这一层在常规路径上不改变任何行为。
        """
        if not text:
            return None
        codes = {c["code"] for c in candidates}
        mapped = LEXICON.get(text)
        if mapped is not None and mapped in codes:
            return mapped
        if text in codes:
            return text
        return None

    # ------------------------------------------------------------------ 模型
    @staticmethod
    def _build_system(candidates: list[dict]) -> str:
        """逐个列出候选 code + 中文含义。模型只看得见这一段，不许省。"""
        lines = []
        for c in candidates:
            hint = c.get("hint") or c.get("label") or c["code"]
            lines.append(f'- {c["code"]}（{c.get("label") or c["code"]}）：{hint}')
        return SYSTEM_HEAD + "\n" + "\n".join(lines) + "\n" + SYSTEM_TAIL

    def _ask_model(self, text: str, candidates: list[dict], ctx: SkillContext) -> Any:
        """调一次模型并落账。异常落失败表后原样上抛，输出非法 JSON 则抛 ValueError。"""
        tier = ctx.extras.get("tier") or Tier.LIGHT
        started = time.perf_counter()
        try:
            resp = ctx.model.complete(
                system=self._build_system(candidates),
                user=f"原文：{text}",
                tier=tier,
            )
        except Exception as exc:
            # 失败也要留账（T54）：不往 model_usage 编 0 token，落进不谈 token 的
            # 失败表，异常照旧上抛。
            record_model_failure(
                ctx.store, exc,
                agent_role=getattr(ctx.identity, "role", "") or "unknown",
                call_site=CALL_SITE, tier=tier,
                latency_ms=int((time.perf_counter() - started) * 1000),
                model=getattr(ctx.model, "model", "") or "",
                trace_id=ctx.extras.get("trace_id") or "",
                plan_id=ctx.extras.get("plan_id") or "",
                task_id=ctx.extras.get("task_id"),
            )
            raise
        record_model_usage(
            ctx.store, resp, client=ctx.model,
            agent_role=getattr(ctx.identity, "role", "") or "unknown",
            call_site=CALL_SITE, tier=tier,
            latency_ms=int((time.perf_counter() - started) * 1000),
            trace_id=ctx.extras.get("trace_id") or "",
            plan_id=ctx.extras.get("plan_id") or "",
            task_id=ctx.extras.get("task_id"),
        )
        try:
            return json.loads(resp.text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"refund.reason_classify 模型输出非合法 JSON: {exc}") from None

    @staticmethod
    def _coerce(data: Any, candidates: list[dict]) -> tuple[str, float, str]:
        """校验模型输出并收敛形状。**只有 `reason_code` 缺失才抛。**"""
        if not isinstance(data, dict):
            raise ValueError(
                f"refund.reason_classify 输出应为 JSON 对象，实际 {type(data).__name__}")
        code = str(data.get("reason_code") or "").strip()
        if not code:
            raise ValueError("refund.reason_classify 输出缺少 reason_code")

        confidence = _clamp(data.get("confidence"))
        why = " ".join(str(data.get("why") or "").split())

        codes = {c["code"] for c in candidates}
        if code != UNKNOWN and code not in codes:
            # 模型没听话，但已经有安全出口：收敛成 unknown 并把它选的那个值写进
            # why —— 丢掉的话，「模型在乱选」这件事就再也查不出来了。
            return UNKNOWN, 0.0, f"模型返回了未知取值 {code}，已按判不出来处理"
        return code, confidence, why or "模型未给出理由"

    # ------------------------------------------------------------------ 出参
    @staticmethod
    def _out(code: str, confidence: float, why: str, source: str,
             raw_text: str, invocation_id: str) -> dict:
        return {
            "reason_code": code,
            "confidence": float(confidence),
            "why": why,
            "source": source,
            "raw_text": raw_text,
            "invocation_id": invocation_id,
        }
