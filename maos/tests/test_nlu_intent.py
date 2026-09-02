"""自然语言意图解析层的口径锁死在这里。

本文件的重点不是「顺利路径能跑通」，而是**畸形输入不许放行、也不许炸**：

  · R2（task_id 不许由模型编）在模型路径和关键词兜底路径上**各锁一次**。
    模型编一个格式完全正确的 id 的成本为零，代价却是批错一条真任务 ——
    所以「格式合法但不存在的 id」是本文件最要紧的一条用例。
  · ``parse_intent`` 对任何垃圾输入都只降级、不抛异常。房间里的常驻监听是裸调它，
    异常逃出去整个监听进程当场崩，连回一句「没听懂」都做不到。
  · 数据契约的字段名被逐字锁住：T67 / T68 按同一份写，改一个字段名就是并轨时
    合不拢，而那种冲突不会有任何红灯提前预警。

**全部用例零网络**：要么直接构造 ``ScriptedModelClient``，要么用本文件里的
``_StubModel``（一个只回预设串的假客户端）。配了 key 的机器上跑本文件，
必须与没配 key 的机器逐字节一致 —— 任何一处漏了，都会在有 key 的机器上
当场打真网络，而那既慢又花钱，还让测试结果依赖环境。
"""

from __future__ import annotations

import json

import pytest

from maos.model.client import ModelClient, ModelResponse, ScriptedModelClient, Tier
from maos.nlu.intent import (
    ACTION_APPROVE,
    ACTION_REJECT,
    ACTION_STATUS,
    ACTION_UNKNOWN,
    ACTIONS,
    CONF_HIGH,
    CONF_LOW,
    Intent,
    parse_intent,
)

ID_A = "task_997ca4541e66"
ID_B = "task_11112222aaaa"
ID_FAKE = "task_deadbeef0000"      # 格式完全合法，就是不存在
ONE = [ID_A]
TWO = [ID_A, ID_B]


class _StubModel(ModelClient):
    """假的「真」模型客户端：不是 ScriptedModelClient，所以走模型路径。

    刻意不继承 ScriptedModelClient —— 继承了就会被 parse_intent 认成降级客户端、
    直接走关键词兜底，模型路径的用例就全都测了个寂寞。
    """

    def __init__(self, text: str = "", raises: Exception | None = None) -> None:
        self.text = text
        self.raises = raises
        self.calls: list[dict] = []

    def complete(self, *, system: str, user: str, tier: str) -> ModelResponse:
        self.calls.append({"system": system, "user": user, "tier": tier})
        if self.raises is not None:
            raise self.raises
        return ModelResponse(text=self.text, model="stub-light")


def _json_model(**payload: object) -> _StubModel:
    return _StubModel(json.dumps(payload, ensure_ascii=False))


# --- 数据契约 -------------------------------------------------------------

def test_契约字段名逐字锁死():
    """T67 / T68 只读引用这份定义，改一个字段名就是并轨时合不拢。"""
    assert list(Intent.__dataclass_fields__) == [
        "action", "task_id", "reason", "confidence", "raw_text", "detail"]
    assert ACTIONS == frozenset({"approve", "reject", "status", "unknown"})
    assert (ACTION_APPROVE, ACTION_REJECT, ACTION_STATUS, ACTION_UNKNOWN) == (
        "approve", "reject", "status", "unknown")
    assert (CONF_HIGH, CONF_LOW) == ("high", "low")


def test_构造期就拦住越界的_action():
    with pytest.raises(ValueError):
        Intent(action="nope")


def test_构造期就拦住越界的_confidence():
    with pytest.raises(ValueError):
        Intent(action=ACTION_STATUS, confidence="medium")


@pytest.mark.parametrize("action", [ACTION_APPROVE, ACTION_REJECT])
def test_审批动作没有_task_id_一律构造失败(action):
    """没有对象的「批准」是一句无处落地的话，让它活下去下游迟早要猜。"""
    with pytest.raises(ValueError):
        Intent(action=action, task_id="")


def test_status_与_unknown_不要求_task_id():
    assert Intent(action=ACTION_STATUS).task_id == ""
    assert Intent(action=ACTION_UNKNOWN).confidence == CONF_LOW   # 缺省保守取低


# --- 模型路径 -------------------------------------------------------------

def test_模型回合法_JSON_且_id_命中():
    model = _json_model(action="approve", task_id=ID_A, confidence="high")
    intent = parse_intent(f"同意 {ID_A}", model=model, known_task_ids=TWO)
    assert (intent.action, intent.task_id, intent.confidence) == (
        ACTION_APPROVE, ID_A, CONF_HIGH)
    assert intent.raw_text == f"同意 {ID_A}"           # 原话逐字留痕
    assert intent.detail["source"] == "model"


def test_走的是轻量档并且把候选_id_喂进了_system():
    """分类任务不该烧强推理档；候选清单必须进 prompt，否则模型只能靠编。"""
    model = _json_model(action="status")
    parse_intent("进度如何", model=model, known_task_ids=TWO)
    call = model.calls[0]
    assert call["tier"] == Tier.LIGHT
    assert ID_A in call["system"] and ID_B in call["system"]
    assert call["user"] == "进度如何"


def test_模型回非_JSON_文本_降_unknown_不抛():
    model = _StubModel("我觉得他大概是想批准这条任务吧")
    intent = parse_intent("帮我看看这条", model=model, known_task_ids=TWO)
    assert intent.action == ACTION_UNKNOWN
    assert intent.confidence == CONF_LOW


def test_模型回_JSON_但_action_越界_降_unknown_不抛():
    model = _json_model(action="delete", task_id=ID_A)
    intent = parse_intent("删了它", model=model, known_task_ids=TWO)
    assert intent.action == ACTION_UNKNOWN
    assert "delete" in intent.detail["why"]


def test_R2_模型编的_task_id_一律不放行():
    """本文件最要紧的一条：格式完全合法、置信度还很高，但那条任务不存在。"""
    model = _json_model(action="approve", task_id=ID_FAKE, confidence="high")
    intent = parse_intent(f"同意 {ID_FAKE}", model=model, known_task_ids=TWO)
    assert intent.action == ACTION_UNKNOWN
    assert intent.task_id == ""
    assert intent.detail["why"] == "task_id 不在 known_task_ids 内"


def test_R2_对_status_同样成立():
    model = _json_model(action="status", task_id=ID_FAKE)
    assert parse_intent("它怎么样了", model=model,
                        known_task_ids=TWO).action == ACTION_UNKNOWN


@pytest.mark.parametrize("text", ["", "   ", "{}", "[]", "null", '{"action": null}',
                                  '{"action": "unknown"}', "```json\n\n```"])
def test_模型回空串或空对象_降_unknown_不抛(text):
    intent = parse_intent("随便聊两句", model=_StubModel(text), known_task_ids=TWO)
    assert intent.action == ACTION_UNKNOWN


def test_容忍代码围栏与前后废话():
    """模型爱把 JSON 包在围栏里。放宽的只是取出 JSON 这一步，字段照样过白名单。"""
    payload = json.dumps({"action": "reject", "task_id": ID_B}, ensure_ascii=False)
    model = _StubModel(f"好的，结果如下：\n```json\n{payload}\n```\n以上。")
    intent = parse_intent(f"驳回 {ID_B}", model=model, known_task_ids=TWO)
    assert (intent.action, intent.task_id) == (ACTION_REJECT, ID_B)


def test_模型没给_id_但候选只有一个_可以省():
    model = _json_model(action="approve", confidence="high")
    intent = parse_intent("同意", model=model, known_task_ids=ONE)
    assert (intent.action, intent.task_id) == (ACTION_APPROVE, ID_A)


def test_模型没给_id_且候选多个_有歧义就不猜():
    model = _json_model(action="approve", confidence="high")
    intent = parse_intent("同意", model=model, known_task_ids=TWO)
    assert intent.action == ACTION_UNKNOWN


def test_模型说低置信度就照低的记():
    model = _json_model(action="status", confidence="low")
    assert parse_intent("咋样", model=model, known_task_ids=ONE).confidence == CONF_LOW


def test_理由必须逐字出自原话_模型改写过的一律丢掉():
    """『不许模型编』落成可验证判据：留痕的价值在于它确实是那个人说的。"""
    model = _json_model(action="reject", task_id=ID_A, reason="申请人的预算论证不充分")
    intent = parse_intent(f"驳回 {ID_A}，预算不够", model=model, known_task_ids=TWO)
    assert intent.reason == ""
    assert intent.detail["dropped_reason"] == "申请人的预算论证不充分"


def test_理由是原话片段时保留():
    model = _json_model(action="reject", task_id=ID_A, reason="预算不够")
    intent = parse_intent(f"驳回 {ID_A}，预算不够", model=model, known_task_ids=TWO)
    assert intent.reason == "预算不够"


def test_approve_不带理由():
    model = _json_model(action="approve", task_id=ID_A, reason="预算不够")
    assert parse_intent(f"同意 {ID_A}，预算不够", model=model,
                        known_task_ids=TWO).reason == ""


def test_模型抛异常时退回关键词兜底而不是崩掉():
    """模型挂掉不该让房间跟着挂 —— 退化成弱一档的理解，好过完全失联。"""
    model = _StubModel(raises=RuntimeError("gateway 502"))
    intent = parse_intent("同意", model=model, known_task_ids=ONE)
    assert (intent.action, intent.confidence) == (ACTION_APPROVE, CONF_LOW)
    assert intent.detail["source"].startswith("keyword:model_error")


# --- 关键词兜底（无 key 的机器） -------------------------------------------

def test_脚本客户端走关键词兜底且一律低置信():
    """ScriptedModelClient 对任何输入都回 "{}"。这时不能瘫，也不配拿高置信度。"""
    model = ScriptedModelClient()
    intent = parse_intent("同意", model=model, known_task_ids=ONE)
    assert (intent.action, intent.task_id, intent.confidence) == (
        ACTION_APPROVE, ID_A, CONF_LOW)
    assert intent.detail["source"] == "keyword:scripted"


@pytest.mark.parametrize("text, action", [
    ("同意", ACTION_APPROVE), ("批准吧", ACTION_APPROVE), ("这个可以过", ACTION_APPROVE),
    ("ok", ACTION_APPROVE), ("approve", ACTION_APPROVE),
    ("驳回", ACTION_REJECT), ("拒绝", ACTION_REJECT), ("不行，别过", ACTION_REJECT),
    ("reject 掉", ACTION_REJECT),
    ("进度怎么样了", ACTION_STATUS), ("看下状态", ACTION_STATUS), ("status", ACTION_STATUS),
    ("今晚吃什么", ACTION_UNKNOWN), ("", ACTION_UNKNOWN),
])
def test_关键词词表(text, action):
    assert parse_intent(text, model=ScriptedModelClient(),
                        known_task_ids=ONE).action == action


@pytest.mark.parametrize("text", ["我不同意", "不批准", "这个不通过", "不可以"])
def test_否定式不许判成同意(text):
    """否定式字面上包含同意词，先判同意会把驳回判成批准 —— 方向反了最危险。"""
    assert parse_intent(text, model=ScriptedModelClient(),
                        known_task_ids=ONE).action == ACTION_REJECT


def test_弱词与状态词同现时判成只读的_status():
    """『可以告诉我进度吗』的『可以』是弱词。误判成只读动作没有副作用。"""
    intent = parse_intent("可以告诉我进度吗", model=ScriptedModelClient(),
                          known_task_ids=ONE)
    assert intent.action == ACTION_STATUS


def test_兜底_双候选说同意不带_id_降_unknown():
    """有歧义不猜，是本轮的基本姿态。"""
    intent = parse_intent("同意", model=ScriptedModelClient(), known_task_ids=TWO)
    assert intent.action == ACTION_UNKNOWN
    assert intent.detail["why"] == "关键词命中但 task_id 无法确定"


def test_兜底_双候选显式带_id_就放行():
    intent = parse_intent(f"同意 {ID_B}", model=ScriptedModelClient(),
                          known_task_ids=TWO)
    assert (intent.action, intent.task_id, intent.confidence) == (
        ACTION_APPROVE, ID_B, CONF_LOW)


def test_兜底_一句话点名两条任务算歧义():
    intent = parse_intent(f"{ID_A} 和 {ID_B} 都同意", model=ScriptedModelClient(),
                          known_task_ids=TWO)
    assert intent.action == ACTION_UNKNOWN


def test_R2_也约束关键词兜底():
    """兜底路径同样不许放行编造的 id —— 两条路径各锁一次，不共享信任。"""
    intent = parse_intent(f"同意 {ID_FAKE}", model=ScriptedModelClient(),
                          known_task_ids=TWO)
    assert intent.action == ACTION_UNKNOWN


def test_没有任何候选时审批动作一律_unknown():
    assert parse_intent("同意", model=ScriptedModelClient(),
                        known_task_ids=[]).action == ACTION_UNKNOWN


def test_兜底的_status_没有候选也不算错():
    """查进度不需要落到具体某条：定不下来就是「问整体」。"""
    intent = parse_intent("现在什么状态", model=ScriptedModelClient(),
                          known_task_ids=TWO)
    assert (intent.action, intent.task_id) == (ACTION_STATUS, "")


def test_兜底路径不产出_reason():
    """关键词切不出「他为什么驳回」，编一个不如留空。"""
    intent = parse_intent("驳回，预算不够", model=ScriptedModelClient(),
                          known_task_ids=ONE)
    assert (intent.action, intent.reason) == (ACTION_REJECT, "")


# --- 失败姿态 -------------------------------------------------------------

@pytest.mark.parametrize("junk", [
    "", "   ", "?????", "{{{", '{"action": ["approve"]}', '{"action": "approve"}',
    '{"action": "approve", "task_id": 12345}', '{"action": 1}', "[1, 2, 3]",
    '{"task_id": "' + ID_A + '"}', "\x00\x01", "同意" * 500,
])
def test_任何垃圾输入都只降级不抛(junk):
    """房间的常驻监听是裸调它，异常逃出去整个进程当场崩。"""
    intent = parse_intent("随便说点什么", model=_StubModel(junk), known_task_ids=TWO)
    assert intent.action in ACTIONS


@pytest.mark.parametrize("known", [None, [], ["", "  "], [ID_A, "", ID_B]])
def test_候选清单本身畸形也不抛(known):
    assert parse_intent("同意", model=ScriptedModelClient(),
                        known_task_ids=known).action in ACTIONS


def test_本层不执行任何动作_只产出_Intent():
    """R1：自然语言不能授权。本模块连审批入口都不许 import，更谈不上调用。

    判据落在 AST 上而不是字符串上：注释里写着「不许调 HumanApprovalQueue.decide()」
    是在解释为什么，不该被自己的守卫判成违规。
    """
    import ast
    import pathlib

    import maos.nlu.intent as mod

    tree = ast.parse(pathlib.Path(mod.__file__).read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    # 零依赖：只许碰 stdlib 与模型客户端；房间层 / 派发层 / 存储层一概不许进来。
    assert not [m for m in imported if m.startswith(("hiclaw", "maos.runtime",
                                                     "maos.core", "maos.contracts"))]

    called = {node.func.attr for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    assert "decide" not in called          # 审批只能由显式指令触发
    assert called & {"complete"}           # 但确实调了模型 —— 不是空壳
