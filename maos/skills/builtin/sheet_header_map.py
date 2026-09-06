"""sheet.header_map —— 把人手填的 CSV 表头映射到标准字段名，附置信度与理由。

投放即注册（C-1）：本文件放进 builtin/ 就会被 discover() 扫到，不改 __init__.py。

IO 契约（逐字段）：
  入：{"header": list[str], "required"?: list[str], "optional"?: list[str]}
  出：{"mapping": dict, "confidence": dict, "why": dict, "source": dict,
      "unmapped": list[str], "missing": list[str], "invocation_id": str}

## 为什么要有这个 skill

`scripts/run_requests.py` 的 `_pick` / `scan_header` 认列是**全等**匹配：
`退货原因（必填）`、`金额/元`、`订单号 *` 这类人真会写的表头一律认不出，整列取到空串，
命令行入口于是直接报「表头里没有「诉求类型」这一列」—— 整表停，一行不建案。

本 skill 把认不出来的那几列交给模型理解。但**模型只做观察，判定仍然是规则**：
模型产出 {字段 -> 表头} 的映射与置信度，取值照旧由规则拿这份映射去做。
置信度低要不要转人工，是调用方的判断，本 skill 不设阈值、不替它猜。

## 两段式，顺序不能反

1. **别名全等匹配**（快路径）。判据与 `run_requests.scan_header` 逐字一致：
   strip 之后去 BOM 再全等。命中的列 ``source="alias"``、``confidence=1.0``。
   别名内部按元组顺序先命中先返回，与 `_pick` 同一套优先级 —— 表里同时有
   「诉求类型」和「退款原因」时取靠前的那个，另一列进 ``unmapped``。
2. **剩菜配对**：只有「还没认出来的字段」与「还没被认领的表头」才交给模型。

于是常规表（`订单号,诉求类型,申报金额,申请日期,说明`）**一次模型都不调**，
异常表也只有那一两列走模型。这是刻意的，不是优化 —— 别名命中的列不给模型看、
模型也无权改：让模型有机会推翻一个已经确凿的匹配，等于白送它一次犯错的机会。

## 失败姿态（照抄 `req_normalize.py`，逐条对齐）

  · ``ctx.model is None``（调用方没接线）→ 规则兜底，**不失败**。剩下的字段直接进
    ``missing``、剩下的表头直接进 ``unmapped``。
  · 调模型抛异常 → `record_model_failure` 落账后**原样 raise**。
  · 输出不是合法 JSON、或是 JSON 但不是对象 → **抛 ValueError**，交给
    ``failure_policy="retry"`` 再试一次。JSON 坏了是**故障**。
  · 模型编了个不存在的列名、或把同一列配给两个字段 → **不抛**，丢弃那一条（或那两条）。
    这是「模型没听话」，不是故障，有安全出口：丢掉的字段落进 ``missing``，
    调用方照常看得见「这一列没认出来」。为一次不听话让整条链路停摆才是坏交易。

冲突时**不许自行挑一个「看起来更像的」** —— 那就是在猜，而猜错一列就是套错一条政策。

## 四个 dict 的键集合恒等

``mapping`` / ``confidence`` / ``why`` / ``source`` 的键集合与顺序完全一致：
认出来的字段四个里都有，没认出来的四个里都没有。丢弃条目的理由走 ``log.warning``，
不塞进 ``why`` —— 塞进去就破了这条恒等式，而下游按 ``mapping`` 的键去读 ``why``
会当场 KeyError。恒等式是可测的硬约束，丢弃理由是给人查日志用的软信息。

## 别名表为什么在这里重抄一份

逐字搬自 `scripts/run_requests.py:54` 的 ``COLUMNS``，**不 import scripts/** ——
包不该依赖脚本目录。两份并存是预期内的过渡态，由整合期收口。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

from maos.core.store import record_model_failure, record_model_usage
from maos.model.client import Tier
from maos.skills.contract import Skill, SkillContext, SkillContract
from maos.skills.registry import register_skill

log = logging.getLogger("maos.skills")

#: 落 ``model_usage`` 时写进 ``call_site`` 列的值。
CALL_SITE = "maos/skills/builtin/sheet_header_map.py::SheetHeaderMapSkill.run"

#: 表头别名。逐字搬 `scripts/run_requests.py:54`，见模块 docstring 末节。
#: 元组顺序即优先级：先命中先返回（口径同 `run_requests._pick`）。
COLUMNS: dict[str, tuple[str, ...]] = {
    "order_id": ("订单号", "订单编号", "order_id", "order"),
    "reason": ("诉求类型", "退款原因", "退货原因", "退款理由", "退货理由",
               "原因", "理由", "reason", "reason_code"),
    "amount": ("申报金额", "退款金额", "金额", "amount", "amount_claimed"),
    "date": ("申请日期", "申请时间", "日期", "date", "requested_at"),
    "note": ("说明", "备注", "note", "remark"),
}

DEFAULT_REQUIRED: tuple[str, ...] = ("order_id", "reason")
DEFAULT_OPTIONAL: tuple[str, ...] = ("amount", "date", "note")

#: 别名命中那条的固定理由。别名命中不需要解释，解释多了反而像在为规则找补。
ALIAS_WHY = "表头别名直接命中"

#: 表头归一时要剥掉的 BOM（U+FEFF）。Excel 存 UTF-8 CSV 会在第一列名前塞一个，
#: 而 ``str.strip()`` **不**认它是空白 —— 所以 strip 之后还要单独 lstrip 一次。
BOM = "﻿"

SYSTEM = """你在读一张**退款申请表**的表头。表里有几个标准字段还没认到列。
下面给你这些待认字段（含它们常见的叫法）和表里还没被认领的表头原文。
请把每个待认字段配到最合适的**一个**表头上；配不上就**不配**，宁缺勿滥。
一个表头只能配给一个字段，一个字段只能配一个表头。
只输出 JSON，不要任何解释文字，格式：
{"mapping":{"字段key":"表头原文"},"confidence":{"字段key":0.9},"why":{"字段key":"中文一句话理由"}}
表头原文必须**逐字**照抄候选里的写法，不许改写、补全、去括号或翻译；
编一个候选里没有的列名，这一条会被整条丢掉。confidence 取 0 到 1 的小数。"""


def _norm(text: Any) -> str:
    """表头归一。判据与 `run_requests.scan_header` / `_pick` 逐字一致：strip 后去 BOM。

    两边不一致的话，症状是「这边说认出来了、那边取到空值」，比不认还难查。
    """
    return str(text).strip().lstrip(BOM)


def _keys(value: Any, default: tuple[str, ...]) -> list[str]:
    """字段 key 清单归一：``None`` 取缺省，非列表包一层，逐项 str + strip，丢空串。"""
    if value is None:
        return list(default)
    items = value if isinstance(value, (list, tuple)) else [value]
    return [s for s in (str(v).strip() for v in items) if s]


def _confidence(value: Any) -> float:
    """置信度收敛：缺失 / 非数字 → 0.0；越界 → clamp 到 [0, 1]。一律不抛。

    ``bool`` 单独挡掉：``isinstance(True, int)`` 为真，不挡的话 ``true`` 会变成 1.0，
    等于把模型的一个语法事故读成「满分把握」。NaN 同理（``json.loads`` 默认认 ``NaN``）：
    它和任何数比较都是 False，混进 min/max 会静默穿透 clamp。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    if value != value:                                  # NaN
        return 0.0
    return max(0.0, min(1.0, float(value)))


def _field_label(key: str) -> str:
    """给人看的字段名 —— 取别名表里第一个（就是标准列名）；没有就退回 key。"""
    aliases = COLUMNS.get(key, ())
    return aliases[0] if aliases else key


def _build_prompt(pending: list[str], candidates: list[str]) -> str:
    lines = ["待认字段（key —— 常见叫法）："]
    for key in pending:
        aliases = "、".join(COLUMNS.get(key, ())) or "（无已知叫法）"
        lines.append(f"  - {key} —— {aliases}")
    lines.append("")
    lines.append("还没被认领的表头原文（逐字照抄，不要改）：")
    lines.extend(f"  - {c}" for c in candidates)
    return "\n".join(lines)


def _accept(data: dict, pending: list[str], candidates: list[str]) -> dict[str, str]:
    """把模型给的 mapping 过成「可采信的 {字段: 表头}」。丢弃只记日志，不抛。

    三道闸，都要过：不在待认清单里的字段丢掉（模型越界去改别名已认的列）、
    不在候选里的列名丢掉（模型编的）、被两个字段抢的列**两条都丢掉**。
    """
    raw_map = data.get("mapping")
    if not isinstance(raw_map, dict):
        # 合法 JSON 对象但没给 mapping —— 读作「一条都没配上」，等价于模型放弃。
        # 有安全出口（待认字段全进 missing），所以不抛：见模块 docstring 失败姿态。
        log.warning("sheet.header_map 模型输出里没有 mapping 对象，按「一条都没配上」处理")
        return {}

    pending_set, candidate_set = set(pending), set(candidates)
    kept: dict[str, str] = {}
    claims: dict[str, list[str]] = {}
    for key, value in raw_map.items():
        key = str(key)
        if key not in pending_set:
            log.warning("sheet.header_map 丢弃：模型给了不在待认清单里的字段 %r", key)
            continue
        column = str(value)
        if column not in candidate_set:
            log.warning("sheet.header_map 丢弃：字段 %s 被配到候选里没有的列 %r（模型编的）",
                        key, column)
            continue
        kept[key] = column
        claims.setdefault(column, []).append(key)

    for column, keys in claims.items():
        if len(keys) > 1:
            # 不挑「看起来更像的」：那是猜。猜错一列 = 套错一条政策。
            log.warning("sheet.header_map 丢弃：字段 %s 抢同一列 %r，冲突两条都不采信",
                        "、".join(keys), column)
            for key in keys:
                kept.pop(key, None)
    return kept


@register_skill
class SheetHeaderMapSkill(Skill):
    contract = SkillContract(
        name="sheet.header_map",
        version="1.0.0",
        purpose="把人手填的 CSV 表头映射到标准字段名，附置信度与理由",
        input_schema={"header": "list[str]", "required": "list[str]?",
                      "optional": "list[str]?"},
        output_schema={"mapping": "dict", "confidence": "dict", "why": "dict",
                       "source": "dict", "unmapped": "list[str]",
                       "missing": "list[str]", "invocation_id": "str"},
        preconditions=["header"],
        depends_tools=[],
        failure_policy="retry",
        max_retries=1,
        security_boundary="只读入参，不写业务表；别名命中不调模型，模型只看没认出来的那几列",
        reuse_note="任何吃人手填表格的入口都复用它认列；别各写一份别名表，"
                   "更别各自发明一套模糊匹配",
        owner_roles=["refund_intake"],
    )

    def run(self, payload: dict, ctx: SkillContext) -> Any:
        header = payload.get("header")
        if not isinstance(header, (list, tuple)):
            raise ValueError(
                f"sheet.header_map 的 header 必须是列表，实际 {type(header).__name__}")

        required = _keys(payload.get("required"), DEFAULT_REQUIRED)
        optional = _keys(payload.get("optional"), DEFAULT_OPTIONAL)
        fields = list(dict.fromkeys(required + optional))

        # (原文, 归一文)。空表头整个不参与：它不是一列名字，进 unmapped 只会让人去改它。
        columns = [(str(h), _norm(h)) for h in header if h and _norm(h)]
        claimed: set[int] = set()
        matched: dict[str, str] = {}
        confidence: dict[str, float] = {}
        why: dict[str, str] = {}
        source: dict[str, str] = {}

        # ---- 第 1 段：别名全等匹配。别名元组顺序优先于表头顺序（口径同 `_pick`）。
        for key in fields:
            for alias in COLUMNS.get(key, ()):
                hit = next((i for i, (_raw, norm) in enumerate(columns)
                            if i not in claimed and norm == alias), None)
                if hit is None:
                    continue
                claimed.add(hit)
                matched[key] = columns[hit][0]
                confidence[key] = 1.0
                why[key] = ALIAS_WHY
                source[key] = "alias"
                break

        # ---- 第 2 段：剩菜才给模型。没剩菜、或调用方没接模型，都不调。
        pending = [key for key in fields if key not in matched]
        candidates = [raw for i, (raw, _n) in enumerate(columns) if i not in claimed]
        if pending and candidates and ctx.model is not None:
            kept, model_conf, model_why = self._ask_model(pending, candidates, ctx)
            for key, column in kept.items():
                hit = next(i for i, (raw, _n) in enumerate(columns)
                           if raw == column and i not in claimed)
                claimed.add(hit)
                matched[key] = column
                confidence[key] = _confidence(model_conf.get(key))
                text = str(model_why.get(key) or "").strip()
                why[key] = text or f"模型判定「{column}」就是「{_field_label(key)}」这一列"
                source[key] = "model"

        # 四个 dict 按 fields 顺序重建：键集合与顺序都恒等，且不随模型返回的 JSON 顺序漂。
        ordered = [key for key in fields if key in matched]
        return {
            "mapping": {key: matched[key] for key in ordered},
            "confidence": {key: confidence[key] for key in ordered},
            "why": {key: why[key] for key in ordered},
            "source": {key: source[key] for key in ordered},
            "unmapped": sorted(raw for i, (raw, _n) in enumerate(columns)
                               if i not in claimed),
            "missing": sorted(key for key in required if key not in matched),
            "invocation_id": self._invocation_id(ctx),
        }

    # ------------------------------------------------------------------
    @staticmethod
    def _invocation_id(ctx: SkillContext) -> str:
        """本次调用的锚点。调用方给了就用调用方的，没给就本地生成，**恒非空**。"""
        extras = getattr(ctx, "extras", None) or {}
        return str(extras.get("invocation_id") or "").strip() or uuid.uuid4().hex

    def _ask_model(self, pending: list[str], candidates: list[str],
                   ctx: SkillContext) -> tuple[dict[str, str], dict, dict]:
        """调一次模型并解析。失败姿态逐条见模块 docstring。"""
        tier = ctx.extras.get("tier") or Tier.LIGHT
        started = time.perf_counter()
        try:
            resp = ctx.model.complete(
                system=SYSTEM,
                user=_build_prompt(pending, candidates),
                tier=tier,
            )
        except Exception as exc:
            # 失败也要留账（T54）：落进不谈 token 的失败表，异常照旧上抛。
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
            data = json.loads(resp.text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"sheet.header_map 模型输出非合法 JSON: {exc}") from None
        if not isinstance(data, dict):
            raise ValueError(
                f"sheet.header_map 输出应为 JSON 对象，实际 {type(data).__name__}")

        kept = _accept(data, pending, candidates)
        conf = data.get("confidence")
        why = data.get("why")
        return (kept,
                conf if isinstance(conf, dict) else {},
                why if isinstance(why, dict) else {})
