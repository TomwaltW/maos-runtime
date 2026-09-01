"""Agent 输出面的两条「不可能」—— T49。

这两个问题的共同形状是：**做错了不会有人发现**。

1. §5.1 Reviewer 从 JSON 中间裸切一刀（``json.dumps(artifacts)[:8000]``）。
   模型收到语法破损的片段、又不知道自己被截了，硬着头皮输出一份合法 JSON，
   一份基于残片的意见书就一路走到 Gate，``metrics`` 里还写着 ``reviewed=len(artifacts)``
   声称审了全部。改法：按份装填 + 自描述截断，且 ``reviewed`` 说实话。

2. §5.2 ``AgentOutput(status="failed")`` 不带 ``error`` 在类型上完全合法。
   下游拿到一个「失败了但没说为什么」的结果，审计链断在这里。
   改法：在 ``AgentOutput.__post_init__`` 上立不变量，让它构造不出来。

判据都往硬里写：不认「有 truncated 字样」，认 ``json.loads`` 通不通、
认构造到底抛不抛。
"""

from __future__ import annotations

import ast
import json
import pathlib

import pytest

from maos.agents._truncate import pack_json_array
from maos.agents.base import AgentOutput, TaskContext
from maos.agents.reviewer import ARTIFACT_BUDGET, PROMPT_MARKER, ReviewerAgent
from maos.artifacts import KIND_PATCH_SET, KIND_REVIEW_NOTE
from maos.model.client import ModelResponse, ScriptedModelClient

MAOS_PKG = pathlib.Path(__file__).resolve().parents[1]

REVIEW_NOTE = json.dumps({
    "defects": [{"path": "src/auth.py", "severity": "minor", "note": "缺注释"}],
    "conclusion": "可放行",
}, ensure_ascii=False)


def _ctx(artifacts: list) -> TaskContext:
    return TaskContext(plan_id="p1", task_id="t1", trace_id="tr", attempt=1,
                       inputs={"artifacts": artifacts}, acceptance=["契约与实现名实相符"],
                       risk_level="L")


class _Capturing(ScriptedModelClient):
    """把**完整**的 user 提示词留下来 —— 父类的 ``calls`` 只存前 120 字符。"""

    def __init__(self, answer: str = REVIEW_NOTE) -> None:
        super().__init__({})
        self.answer = answer
        self.prompts: list[str] = []

    def complete(self, *, system, user, tier):
        self.prompts.append(user)
        return ModelResponse(text=self.answer, model=f"scripted-{tier}")


def _artifacts(n: int, *, filler: int = 0) -> list[dict]:
    return [{"task_id": f"task-{i:03d}", "kind": KIND_PATCH_SET, "version": 1,
             "content": {"files": ["x" * filler]}} for i in range(n)]


def _payload_block(prompt: str) -> str:
    """提示词的第 2 块就是产物清单载荷（`_build_prompt` 用 "\\n\\n" 拼块）。"""
    return prompt.split("\n\n")[1]


# ======================================================================
# §5.1 自描述截断
# ======================================================================
def test_small_input_prompt_is_byte_identical_to_the_old_slice():
    """防回归：没触发截断时，提示词与改造前**逐字节一致**。

    改造前那一行是 ``json.dumps(artifacts, ensure_ascii=False, default=str)[:8000]``。
    绝大多数调用都落在这一支，截断改造不该顺手改掉它们送进模型的东西。
    """
    artifacts = _artifacts(3)
    model = _Capturing()
    ReviewerAgent(model).run(_ctx(artifacts))

    legacy = "\n\n".join([
        f"{PROMPT_MARKER}（计划 p1，共 {len(artifacts)} 份）：",
        json.dumps(artifacts, ensure_ascii=False, default=str)[:ARTIFACT_BUDGET],
        f"验收标准：{json.dumps(['契约与实现名实相符'], ensure_ascii=False)}",
    ])
    assert model.prompts[0] == legacy


def test_large_input_tells_the_model_it_was_truncated_and_what_it_missed():
    """截断时模型必须看到三件事：被截了 / 原始有多大 / 少看到的是哪些。

    判据不是「有 truncated 字样」—— 是**点名**：被省略产物的 task_id 出现在提示词里，
    而它们的正文没有。模型据此知道自己的结论覆盖不到哪些东西。
    """
    artifacts = _artifacts(40, filler=600)          # 远超 8000 字符预算
    model = _Capturing()
    out = ReviewerAgent(model).run(_ctx(artifacts))
    prompt = model.prompts[0]

    presented = out.metrics["reviewed"]
    assert 0 < presented < len(artifacts), "这份输入必须真的触发截断，否则本用例没测到东西"

    assert "被截断了" in prompt and f"共 {len(artifacts)} 份" in prompt
    assert f"只呈现了前 {presented} 份" in prompt
    assert str(len(json.dumps(artifacts, ensure_ascii=False))) in prompt, "原始规模要写给模型"

    omitted = artifacts[presented:]
    assert omitted[0]["task_id"] in prompt, "被省略的产物必须被点名，模型才知道少看了什么"
    assert artifacts[presented - 1]["task_id"] in prompt, "呈现的那些当然也在"


def test_truncated_payload_is_still_valid_json():
    """整节的核心判据：截断后送进模型的那段**仍然 json.loads 得通**。

    这一条比「有 truncated 字样」硬得多 —— 裸 ``[:8000]`` 恰恰是在这里挂的。
    """
    artifacts = _artifacts(40, filler=600)
    model = _Capturing()
    out = ReviewerAgent(model).run(_ctx(artifacts))

    payload = _payload_block(model.prompts[0])
    parsed = json.loads(payload)                    # 破损就在这里抛
    assert isinstance(parsed, list)
    assert len(parsed) == out.metrics["reviewed"], "呈现份数要与实际装进去的对得上"
    assert parsed == artifacts[:len(parsed)], "切在结构边界上：装进去的每一份都是完整的"
    assert len(payload) <= ARTIFACT_BUDGET


def test_metrics_reviewed_is_what_the_model_actually_saw():
    """§5.1 的连带：截断时 ``reviewed=len(artifacts)`` 是假话，必须改成实际份数。"""
    artifacts = _artifacts(40, filler=600)
    out = ReviewerAgent(_Capturing()).run(_ctx(artifacts))

    assert out.metrics["reviewed"] < out.metrics["artifacts_total"] == len(artifacts)
    assert out.metrics["truncated"] is True

    note = out.artifacts[0]["content"]
    assert note["reviewed"] == out.metrics["reviewed"] and note["truncated"] is True
    assert "清单被截断" in note["summary"], "意见书是给人看的，只审了一部分要一眼看得见"


def test_small_input_metrics_unchanged():
    artifacts = _artifacts(3)
    out = ReviewerAgent(_Capturing()).run(_ctx(artifacts))

    assert out.status == "ok" and out.artifacts[0]["kind"] == KIND_REVIEW_NOTE
    assert out.metrics["reviewed"] == out.metrics["artifacts_total"] == 3
    assert out.metrics["truncated"] is False
    assert out.artifacts[0]["content"]["reviewed"] == 3


def test_single_oversized_artifact_needs_human_instead_of_reviewing_nothing():
    """一份都塞不进去 -> blocked/needs_human，**不**产出一份空白意见书。

    与本文件既有的姿态一致：审查没做成就是没做成，不许降级成「看起来没问题」。
    """
    artifacts = [{"task_id": "task-huge", "kind": KIND_PATCH_SET, "version": 1,
                  "content": {"files": ["x" * (ARTIFACT_BUDGET * 2)]}}]
    model = _Capturing()
    out = ReviewerAgent(model).run(_ctx(artifacts))

    assert out.status == "blocked" and out.metrics.get("needs_human") is True
    assert out.artifacts == []
    assert out.open_questions and "一份都呈现不了" in out.open_questions[0]
    assert model.prompts == [], "都审不了了就别再调模型"


def test_empty_artifact_list_behaves_as_before():
    """空清单不是「截断到 0」，照旧正常审查（提示词里那段是 `[]`）。"""
    out = ReviewerAgent(_Capturing()).run(_ctx([]))
    assert out.status == "ok" and out.metrics["reviewed"] == 0
    assert out.metrics["truncated"] is False


# ---------------------------------------------------------------- 装填器本身
def test_pack_json_array_boundary_cases():
    assert pack_json_array([], budget=8000).payload == "[]"

    rows = [{"i": i} for i in range(5)]
    whole = pack_json_array(rows, budget=8000)
    assert whole.truncated is False and whole.note == ""
    assert whole.payload == json.dumps(rows, ensure_ascii=False, default=str)

    # 预算刚好卡在第 3 份的分隔符上：宁可少装一份，也不切坏结构。
    tight = pack_json_array(rows, budget=len(json.dumps(rows[:2], ensure_ascii=False)) + 1)
    assert tight.truncated is True and tight.presented == 2
    assert json.loads(tight.payload) == rows[:2]
    assert tight.omitted == 3

    starved = pack_json_array(rows, budget=3)
    assert starved.presented == 0 and json.loads(starved.payload) == []


def test_pack_json_array_serializes_the_unserializable():
    """``default=str`` 这条口径不能在改造中掉了 —— artifact 里躺着 datetime。"""
    import datetime as dt

    rows = [{"at": dt.datetime(2026, 9, 1, 12, 0, 0)}]
    assert "2026-09-01" in pack_json_array(rows, budget=8000).payload


def test_pack_json_array_lists_at_most_max_listed_omitted_items():
    """note 自己也占提示词预算，被省略的点名要有上限。"""
    rows = [{"id": f"r{i}", "blob": "x" * 200} for i in range(60)]
    packed = pack_json_array(rows, budget=400, describe=lambda r: r["id"], max_listed=5)
    assert packed.truncated is True
    assert "另有" in packed.note and "未点名" in packed.note
    assert packed.note.count("r") >= 5


# ======================================================================
# §5.2 失败必须给理由
# ======================================================================
@pytest.mark.parametrize("kwargs", [
    {"status": "failed"},                                    # 什么都不带
    {"status": "failed", "error": None},
    {"status": "failed", "error": ""},
    {"status": "failed", "error": "   "},                    # 空白不算理由
])
def test_failed_without_error_is_now_impossible(kwargs):
    """``AgentOutput(status="failed")`` 曾是合法构造 —— 现在构造点就抛。

    「失败了但没说为什么」会让审计链断在这里：事后翻记录，只看得到这一步没成，
    看不到为什么没成，返工链也就无从解释。
    """
    with pytest.raises(ValueError, match="必须带 error"):
        AgentOutput(**kwargs)


@pytest.mark.parametrize("kwargs", [
    {"status": "blocked"},
    {"status": "blocked", "open_questions": [], "error": None},
    {"status": "blocked", "open_questions": [], "error": "  "},
])
def test_blocked_without_any_reason_is_now_impossible(kwargs):
    with pytest.raises(ValueError, match="必须带 open_questions 或 error"):
        AgentOutput(**kwargs)


@pytest.mark.parametrize("kwargs", [
    {"status": "ok"},                                        # ok 本来就不需要理由
    {"status": "ok", "artifacts": [{"kind": KIND_REVIEW_NOTE, "content": {}}]},
    {"status": "failed", "error": "无可用 Agent: role=nobody"},
    {"status": "blocked", "open_questions": ["语义审查超时：模型 120s 未返回"]},
    {"status": "blocked", "error": "网关拒绝：渠道不可用"},   # blocked 的另一种正当形态
])
def test_legitimate_constructions_still_pass(kwargs):
    """不变量只堵「不说为什么」，正当构造一个都不许挡。"""
    assert AgentOutput(**kwargs).status == kwargs["status"]


def test_every_production_construction_already_gives_a_reason():
    """静态守门人：全仓生产路径的构造点，没有一处是不给理由的。

    ``__post_init__`` 只在**跑到**那一行时才抛；这条断言不依赖覆盖率，
    直接扫源码 —— 新加一个不带理由的构造点，哪怕它没有任何用例覆盖，也会在这里红。
    """
    offenders = []
    for path in sorted(MAOS_PKG.rglob("*.py")):
        if "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and getattr(node.func, "id", None) == "AgentOutput"):
                continue
            kw = {k.arg: k.value for k in node.keywords if k.arg}
            status = kw.get("status")
            status = status.value if isinstance(status, ast.Constant) else None
            if status == "failed" and "error" not in kw:
                offenders.append(f"{path.relative_to(MAOS_PKG.parent)}:{node.lineno} failed 无 error")
            if status == "blocked" and not ({"open_questions", "error"} & set(kw)):
                offenders.append(f"{path.relative_to(MAOS_PKG.parent)}:{node.lineno} blocked 无理由")
    assert not offenders, "这些构造点失败了却不说为什么：\n  " + "\n  ".join(offenders)


def test_the_invariant_must_live_on_the_dataclass_not_on_the_worker():
    """论证：为什么不变量立在 ``AgentOutput`` 上，而不是拦在 ``WorkerRuntime._reply`` 前。

    ``_reply`` 只看得住经由 Worker 队列回来的那些。``review_after_gate``
    （``agents/reviewer.py``）**直接** ``reviewer.run(ctx)``，压根不经队列 ——
    而 Reviewer 恰恰是 blocked 的主要产地（超时 / 输出不合契约 / 清单塞不下）。
    绝大多数构造点也都在 ``worker.py`` 之外。这条断言把这个理由钉住：
    它红了，说明「拦在 worker 就够了」这个诱人的错误答案又变得像是对的了。
    """
    import inspect

    from maos.agents.reviewer import review_after_gate

    src = inspect.getsource(review_after_gate)
    assert "reviewer.run(ctx)" in src and "_reply" not in src, \
        "Reviewer 不再走那条旁路了？那要重新评估 worker 侧拦截够不够"

    outside, inside = 0, 0
    for path in sorted(MAOS_PKG.rglob("*.py")):
        if "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        n = sum(1 for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "AgentOutput")
        if path.name == "worker.py":
            inside += n
        else:
            outside += n
    assert outside > inside, \
        f"构造点 worker.py 内 {inside} 处 / 外 {outside} 处 —— 外面多得多，拦在 worker 守不住"


def test_worker_failure_paths_still_construct():
    """回归：Worker 兜异常那两处（PermissionDenied / 通用异常）本来就带 error。"""
    from maos.agents.base import PermissionDenied

    exc = PermissionDenied("coding 无权写 repo_branch")
    assert AgentOutput(status="failed", error=str(exc),
                       metrics={"security_event": True}).error
    assert AgentOutput(status="failed", error=f"{type(exc).__name__}: {exc}").error
