"""自然语言意图解析层 —— 一句人话 → 一个结构化 ``Intent``。

买的是「少打字、看得懂」，**不是「替人做决定」**。这一条不是措辞讲究，是本层的
存在前提，落在三处硬约束上（跨轨契约 §2 的三条红线）：

  · **R1 自然语言不能授权**：本模块只产出 ``Intent``，一个动作都不执行。
    ``action=approve`` 的含义永远只是「他大概想批」，真正调
    ``HumanApprovalQueue.decide()`` 的唯一入口仍是显式的 ``/approve <task_id>`` 文本。
    谁去执行、要不要二次确认，是派发层的事，不在这里。模型幻觉一次等于越权批掉
    一笔生产变更 —— 与铁律 8 同源：权威动作不能由推断产生。
  · **R2 task_id 不许由模型编**：模型极其擅长编出格式完全正确的 id。所以解析出的
    id 必须命中调用方给的 ``known_task_ids``，对不上一律降 ``ACTION_UNKNOWN``，
    绝不放行。这条在模型路径和关键词兜底路径上**各拦一次**，不共享信任。
  · **R3 不碰状态机与冻结面**：本模块只产出进程内数据，不进事件契约、不进库、
    不加任何状态或迁移。

因此 ``parse_intent`` 的失败姿态是**降级而不是抛异常**：认不出就返回
``ACTION_UNKNOWN``，让上层回一句「没听懂」。房间里一句听不懂的闲聊不该让常驻
监听进程崩掉。

关于「模型没给出可用输出」的两种情况，本模块**区别对待**，这是本文件唯一一处
自行定的口径（见 ``docs/DECISIONS.md ## task-T66``）：

  · 模型**给了但不可信**（非 JSON / action 越界 / task_id 是编的）→ 直接
    ``UNKNOWN``，不再拿关键词去救。模型已经表态了，我们没有理由用更弱的手段
    去推翻它的表态。
  · 模型**根本没得给**（无 key 机器上的 ``ScriptedModelClient``，或 ``complete``
    直接抛异常）→ 走关键词兜底，一律 ``CONF_LOW``。关键词匹配不是理解，
    所以它永远不配拿高置信度；但它让没有 key 的机器、和模型挂掉的时刻，
    房间仍然可用。
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from maos.model.client import ModelClient, ScriptedModelClient, Tier

ACTION_APPROVE = "approve"
ACTION_REJECT = "reject"
ACTION_STATUS = "status"
ACTION_UNKNOWN = "unknown"
ACTIONS = frozenset({ACTION_APPROVE, ACTION_REJECT, ACTION_STATUS, ACTION_UNKNOWN})

CONF_HIGH = "high"
CONF_LOW = "low"


@dataclass(frozen=True)
class Intent:
    """一句人话的解析结果。

    构造期就校验，而不是等到用的时候 —— 一个 ``action="delete"`` 的 Intent
    根本不该存在于进程里，让它活到派发层再报错，排查成本高一个量级。
    """

    action: str                    # 必在 ACTIONS 内，否则构造即 ValueError
    task_id: str = ""              # approve / reject 时必填，且必须命中 known_task_ids
    reason: str = ""               # reject 的理由，人说了才有，不许模型编
    confidence: str = CONF_LOW     # CONF_HIGH / CONF_LOW，缺省保守取低
    raw_text: str = ""             # 人的原话，逐字留痕，用于回执与取证
    detail: dict = field(default_factory=dict)   # 模型原始返回，排查用

    def __post_init__(self) -> None:
        if self.action not in ACTIONS:
            raise ValueError(f"未知 action：{self.action!r}，必须在 {sorted(ACTIONS)} 内")
        if self.confidence not in {CONF_HIGH, CONF_LOW}:
            raise ValueError(
                f"未知 confidence：{self.confidence!r}，必须是 {CONF_HIGH!r} 或 {CONF_LOW!r}")
        if self.action in {ACTION_APPROVE, ACTION_REJECT} and not self.task_id:
            # 没有 task_id 的「批准」是一句无处落地的话。允许它存在，下游迟早要猜
            # 是哪一条 —— 而猜就是 R2 想堵的那个洞。
            raise ValueError(f"action={self.action} 必须带 task_id")


# --- 关键词兜底词表 -------------------------------------------------------
#
# 判定顺序刻意是 拒绝 → 状态 → 同意，不是词表的书写顺序：
#   · 拒绝优先，因为否定式「不同意 / 不通过」在字面上包含同意词，先判同意会判反；
#   · 状态排在同意前面，因为同意侧有「可以 / ok」这种极弱的词，
#     「可以告诉我进度吗」会误命中。误判成只读的 status 不会有任何副作用，
#     误判成 approve 则要多骚扰人一次确认 —— 让误判方向偏向安全的那边。

_KW_REJECT = ("驳回", "拒绝", "不行", "别过", "不同意", "不批准", "不通过", "不可以")
_KW_STATUS = ("状态", "进度", "怎么样了")
_KW_APPROVE = ("同意", "批准", "通过", "可以")

# ASCII 词走词边界匹配：裸 in 判定会让 "ok" 命中 token / broken / look。
_KW_REJECT_EN = ("reject",)
_KW_STATUS_EN = ("status",)
_KW_APPROVE_EN = ("ok", "approve")


def _hit(text: str, zh: Sequence[str], en: Sequence[str]) -> bool:
    low = text.lower()
    if any(word in text for word in zh):
        return True
    return any(re.search(rf"\b{word}\b", low) for word in en)


def _known_ids(known_task_ids: Sequence[str] | None) -> list[str]:
    return [str(t).strip() for t in (known_task_ids or []) if str(t).strip()]


def _ids_mentioned(text: str, known: Sequence[str]) -> list[str]:
    """人在原话里点名了哪几个 id。只认字面命中 —— 这就是 R2 的实现。"""
    return [tid for tid in known if tid in text]


def _resolve_task_id(text: str, known: Sequence[str]) -> str | None:
    """定位这句话说的是哪一条任务。定不下来返回 ``None``（调用方降 UNKNOWN）。

    判据（派单 §5.2，本轨自定，已记 DECISIONS）：
      · 原话点名了恰好一个已知 id → 就是它；
      · 点名了两个及以上 → 有歧义，不猜；
      · 一个都没点名，而候选只有一个 → 允许省略（人说「同意」就够了）；
      · 一个都没点名，候选有两个及以上 → 有歧义，不猜。
    """
    mentioned = _ids_mentioned(text, known)
    if len(mentioned) == 1:
        return mentioned[0]
    if mentioned:
        return None
    if len(known) == 1:
        return known[0]
    return None


def _unknown(text: str, why: str, detail: dict | None = None) -> Intent:
    payload = dict(detail or {})
    payload["why"] = why
    return Intent(action=ACTION_UNKNOWN, raw_text=text, confidence=CONF_LOW, detail=payload)


def _keyword_intent(text: str, known: Sequence[str], *, source: str) -> Intent:
    """无模型可用时的兜底。一律 ``CONF_LOW`` —— 关键词匹配不是理解。"""
    detail = {"source": source}
    if _hit(text, _KW_REJECT, _KW_REJECT_EN):
        action = ACTION_REJECT
    elif _hit(text, _KW_STATUS, _KW_STATUS_EN):
        action = ACTION_STATUS
    elif _hit(text, _KW_APPROVE, _KW_APPROVE_EN):
        action = ACTION_APPROVE
    else:
        return _unknown(text, "关键词未命中", detail)

    if action == ACTION_STATUS:
        # 查进度不需要落到具体某条：定不下来就是「问整体」，不是错误。
        mentioned = _ids_mentioned(text, known)
        task_id = mentioned[0] if len(mentioned) == 1 else (known[0] if len(known) == 1 else "")
        return Intent(action=ACTION_STATUS, task_id=task_id, confidence=CONF_LOW,
                      raw_text=text, detail=detail)

    task_id = _resolve_task_id(text, known)
    if task_id is None:
        # R2：认出了动作但认不出对象，一样不放行。
        return _unknown(text, "关键词命中但 task_id 无法确定", detail)
    # 兜底路径不产出 reason：关键词切不出「他为什么驳回」，编一个不如留空。
    return Intent(action=action, task_id=task_id, confidence=CONF_LOW,
                  raw_text=text, detail=detail)


def _build_system_prompt(known: Sequence[str]) -> str:
    ids = "、".join(known) if known else "（当前没有待办任务）"
    return (
        "你是一个意图分类器。用户是运维审批人，正在一个聊天房间里用中文口语谈论待审批的任务。\n"
        "把用户这句话分类成一个动作，**只输出一个 JSON 对象，不要任何解释、不要代码围栏**。\n"
        "\n"
        "JSON 字段：\n"
        '  action     必须是 "approve" / "reject" / "status" / "unknown" 之一\n'
        '  task_id    必须原样取自下面的候选清单，**不许自己编造或改写**；不确定就留空字符串\n'
        '  reason     仅当 action="reject" 且用户说了理由时填，必须是用户原话里的片段；否则留空\n'
        '  confidence "high" 或 "low"，不确定就填 "low"\n'
        "\n"
        f"候选 task_id 清单（只能从中选，不在清单里的一律留空）：{ids}\n"
        "\n"
        "判不准、或用户说的与审批无关，就返回 {\"action\": \"unknown\"}。宁可返回 unknown，也不要猜。"
    )


def _loads(text: str) -> object | None:
    """宽容一点解析：模型爱把 JSON 包在 ``` 围栏里，或前后带一句废话。

    宽容只放在**取出 JSON** 这一步，取出之后的字段照样逐项过白名单 —— 放宽格式
    不等于放宽 R2。
    """
    raw = (text or "").strip()
    if not raw:
        return None
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw).strip()
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        pass
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(raw[start:end + 1])
        except (ValueError, TypeError):
            return None
    return None


def parse_intent(text: str, *, model: ModelClient,
                 known_task_ids: Sequence[str]) -> Intent:
    """把一句人话转成 ``Intent``。

    任何认不出的情况都返回 ``action=ACTION_UNKNOWN``，**不抛异常、不猜**。
    唯一会抛的是调用方自己传错（比如 ``model`` 不是 ``ModelClient``），那不是
    「没听懂」，是接线错了，不该被兜底吞掉。
    """
    raw_text = text or ""
    known = _known_ids(known_task_ids)

    # 无 key 的机器上 select_model_client() 降级回 ScriptedModelClient，它对任何输入
    # 都回预设串或 "{}"。此时不能瘫，走关键词兜底。
    if isinstance(model, ScriptedModelClient):
        return _keyword_intent(raw_text, known, source="keyword:scripted")

    try:
        resp = model.complete(system=_build_system_prompt(known), user=raw_text,
                              tier=Tier.LIGHT)
    except Exception as exc:   # noqa: BLE001 —— 模型挂掉不该让房间跟着挂
        return _keyword_intent(raw_text, known, source=f"keyword:model_error:{type(exc).__name__}")

    detail = {"source": "model", "raw": (resp.text or "")[:2000], "model": resp.model}

    data = _loads(resp.text)
    if not isinstance(data, dict):
        return _unknown(raw_text, "模型返回不是合法 JSON 对象", detail)

    action = data.get("action")
    if not isinstance(action, str) or action not in ACTIONS:
        return _unknown(raw_text, f"action 越界：{action!r}", detail)
    if action == ACTION_UNKNOWN:
        return _unknown(raw_text, "模型判为 unknown", detail)

    confidence = CONF_LOW if data.get("confidence") == CONF_LOW else CONF_HIGH

    task_id = data.get("task_id")
    task_id = task_id.strip() if isinstance(task_id, str) else ""
    if task_id and task_id not in known:
        # R2：格式再对也不放行。模型编 id 的成本为零，代价却是批错任务。
        return _unknown(raw_text, "task_id 不在 known_task_ids 内", detail)

    if action == ACTION_STATUS:
        if not task_id:
            mentioned = _ids_mentioned(raw_text, known)
            task_id = mentioned[0] if len(mentioned) == 1 else (
                known[0] if len(known) == 1 else "")
        return Intent(action=ACTION_STATUS, task_id=task_id, confidence=confidence,
                      raw_text=raw_text, detail=detail)

    if not task_id:
        resolved = _resolve_task_id(raw_text, known)
        if resolved is None:
            return _unknown(raw_text, "模型未给 task_id 且无法从原话唯一确定", detail)
        task_id = resolved

    reason = data.get("reason")
    reason = reason.strip() if isinstance(reason, str) else ""
    if action != ACTION_REJECT:
        reason = ""
    elif reason and reason not in raw_text:
        # 「不许模型编」落成一条可验证的判据：理由必须逐字出自人的原话。
        # 模型改写过的理由宁可丢掉 —— 留痕的价值在于它确实是那个人说的。
        detail["dropped_reason"] = reason[:200]
        reason = ""

    return Intent(action=action, task_id=task_id, reason=reason, confidence=confidence,
                  raw_text=raw_text, detail=detail)
