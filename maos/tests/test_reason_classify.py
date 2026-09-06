"""refund.reason_classify 的行为契约（T101）。

守的是这条 skill 上**最容易被悄悄做坏**的四件事：

1. **词表是快路径，模型是兜底 —— 顺序不能反**（`test_lexicon_*`）。命中词表的一次
   模型都不调。判据不是「跑得快」而是「假 model 的 complete() 里 raise，没炸」——
   计时会随机器抖，raise 不会。常规输入跑完全程零模型调用，这是刻意的。

2. **三条分支的 `source` 分得开**（`test_source_*`）。lexicon / model / fallback 三个
   取值让「这条是怎么判出来的」可回看。合成一个值，「模型没接线」就长得像
   「词表命中了」，而人工复核队列会因此永远是空的。

3. **故障与不听话分开处理**（`test_broken_json_*` / `test_out_of_enum_*`）。JSON 坏了
   是故障，必须抛给 `failure_policy="retry"`；模型选了个不存在的选项是它没听话，
   已经有安全出口（`unknown`），抛出去只会让整条链路为一次不听话停摆。
   两者混成一种处理，无论倒向哪边都错：要么故障被静默吞掉，要么链路被无谓打断。

4. **模型账真的落了**（`test_records_model_usage`）。`record_model_usage` 漏接不会让
   任何测试变红，只会让成本统计静默偏低（口径同 `obs/call_sites.py` 的模块 docstring）。
"""

from __future__ import annotations

import json

import pytest

from maos.agents.base import AgentIdentity
from maos.core.store import SqliteStore
from maos.model.client import ModelResponse
from maos.skills.builtin.refund.reason_classify import (
    CALL_SITE, DEFAULT_CANDIDATES, LEXICON, UNKNOWN, RefundReasonClassifySkill,
)
from maos.skills.contract import SkillContext

TRACE = "trace-t101"
PLAN = "plan-t101"
TASK = "task-t101"

IDENTITY = AgentIdentity(agent_id="intake-1", role="refund_intake", duty="申请受理")

#: 出参必须齐的六个字段（派单第 4 节，一个不许多、一个不许少）。
OUT_FIELDS = {"reason_code", "confidence", "why", "source", "raw_text", "invocation_id"}


@pytest.fixture()
def store(tmp_path) -> SqliteStore:
    s = SqliteStore(str(tmp_path / "t101.db"))
    s.init_schema()
    return s


class _ExplodingModel:
    """`complete()` 一旦被调到就炸。

    「不调模型」这件事只能这么证：计时会随机器抖，调用计数要靠测试自己数得对，
    而 raise 是被调到就一定看得见。
    """

    model = "must-not-be-called"

    def complete(self, **kwargs):
        raise AssertionError("词表命中却调了模型 —— 快路径被绕过了")


class _StubModel:
    """按预置文本作答，并记下最后一次的 system / user，供 prompt 断言用。"""

    model = "stub-model"

    def __init__(self, text: str, *, tokens_in: int = 12, tokens_out: int = 7):
        self.text = text
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out
        self.calls: list[dict] = []

    def complete(self, *, system: str, user: str, tier: str) -> ModelResponse:
        self.calls.append({"system": system, "user": user, "tier": tier})
        return ModelResponse(text=self.text, tokens_in=self.tokens_in,
                             tokens_out=self.tokens_out, model=self.model, meta={})


class _BoomModel:
    model = "boom-model"

    def complete(self, **kwargs):
        raise RuntimeError("模型网关不可达")


def _ctx(store=None, model=None, **extras) -> SkillContext:
    base = {"plan_id": PLAN, "task_id": TASK, "trace_id": TRACE}
    base.update(extras)
    return SkillContext(model=model, store=store, identity=IDENTITY, extras=base)


def _run(payload: dict, ctx: SkillContext) -> dict:
    return RefundReasonClassifySkill().run(payload, ctx)


def _answer(**kw) -> str:
    base = {"reason_code": "wrong_item", "confidence": 0.9, "why": "客户说少发了货"}
    base.update(kw)
    return json.dumps(base, ensure_ascii=False)


# ===========================================================================
# 1. 词表是快路径 —— 命中就一次模型都不调
# ===========================================================================
def test_lexicon_hit_never_touches_the_model():
    """派单第 6 节第 1 条：传一个 complete() 里 raise 的假 model，断言没炸。"""
    out = _run({"text": "质量问题"}, _ctx(model=_ExplodingModel()))
    assert out["reason_code"] == "quality_defect"
    assert out["source"] == "lexicon"
    assert out["confidence"] == 1.0
    assert out["why"] == "词表直接命中"


def test_every_lexicon_word_hits_without_the_model():
    """14 个词逐个走一遍。少映射一个词，就是一类客户的申请整行拒收。"""
    model = _ExplodingModel()
    for word, expected in LEXICON.items():
        out = _run({"text": word}, _ctx(model=model))
        assert (out["reason_code"], out["source"]) == (expected, "lexicon"), \
            f"词表里的「{word}」没走快路径"


def test_raw_english_code_takes_the_lexicon_path():
    """派单第 6 节第 7 条：直接写英文 code 也认，且不调模型。"""
    out = _run({"text": "wrong_item"}, _ctx(model=_ExplodingModel()))
    assert (out["reason_code"], out["source"]) == ("wrong_item", "lexicon")


def test_lexicon_hit_is_whitespace_tolerant():
    """CSV 里的值前后常带空格 —— strip 之后该命中的还得命中。"""
    out = _run({"text": "  坏了  "}, _ctx(model=_ExplodingModel()))
    assert (out["reason_code"], out["source"]) == ("quality_defect", "lexicon")
    assert out["raw_text"] == "坏了", "raw_text 回显的是 strip 后的原文"


# ===========================================================================
# 2. 三条分支的 source 分得开
# ===========================================================================
def test_source_model_returns_all_six_fields():
    """派单第 6 节第 2 条：词表外 + 合法 JSON → source=model，六个字段齐。"""
    model = _StubModel(_answer())
    out = _run({"text": "漏发了两个"}, _ctx(model=model))
    assert set(out) == OUT_FIELDS, "出参字段一个不许多、一个不许少"
    assert out["source"] == "model"
    assert out["reason_code"] == "wrong_item"
    assert out["confidence"] == 0.9
    assert out["why"] == "客户说少发了货"
    assert out["raw_text"] == "漏发了两个"
    assert out["invocation_id"], "invocation_id 恒非空 —— 空了整条审计链就断了"
    assert len(model.calls) == 1, "词表外应当恰好调一次模型"


def test_source_fallback_when_no_model_wired():
    """派单第 6 节第 3 条：词表外 + ctx.model=None → fallback/unknown，**不抛**。

    上层忘了接线不该把链路拖挂（口径同 req_normalize 的两条不对称分支）。
    """
    out = _run({"text": "货不对板"}, _ctx(model=None))
    assert set(out) == OUT_FIELDS
    assert out["source"] == "fallback"
    assert out["reason_code"] == UNKNOWN
    assert out["confidence"] == 0.0
    assert out["why"] == "无模型可用，且词表未命中"
    assert out["raw_text"] == "货不对板"


def test_empty_text_is_unknown_not_an_exception():
    """派单第 4 节：空串按 unknown 处理，不抛。

    受理岗那边空的诉求类型是**当场报错**（`run_requests.py::_reason_code`），
    这里不是 —— 本 skill 只产观察，判不出来就说判不出来，由调用方决定怎么办。
    """
    out = _run({"text": "   "}, _ctx(model=None))
    assert (out["reason_code"], out["source"]) == (UNKNOWN, "fallback")
    assert out["raw_text"] == ""


def test_invocation_id_comes_from_extras_when_given():
    """调用方给了锚点就用调用方的（口径同 `_common.invocation_id_of`）。"""
    out = _run({"text": "质量问题"}, _ctx(invocation_id="inv-abc"))
    assert out["invocation_id"] == "inv-abc"


# ===========================================================================
# 3. 故障与不听话分开处理
# ===========================================================================
def test_broken_json_raises_value_error():
    """派单第 6 节第 4 条：输出不是合法 JSON → 抛 ValueError。

    静默降级会把「模型坏了」伪装成「数据就长这样」，比失败更糟。
    """
    with pytest.raises(ValueError, match="非合法 JSON"):
        _run({"text": "尺寸不合适"}, _ctx(model=_StubModel("这不是 JSON")))


def test_json_that_is_not_an_object_raises():
    """合法 JSON 但不是对象（比如一个裸字符串）同样是故障。"""
    with pytest.raises(ValueError, match="应为 JSON 对象"):
        _run({"text": "尺寸不合适"}, _ctx(model=_StubModel('"wrong_item"')))


def test_missing_reason_code_raises():
    """缺 reason_code 才抛 —— 交给 failure_policy=retry 再试一次。"""
    with pytest.raises(ValueError, match="缺少 reason_code"):
        _run({"text": "尺寸不合适"},
             _ctx(model=_StubModel(json.dumps({"confidence": 0.8, "why": "x"}))))


def test_out_of_enum_code_collapses_to_unknown_without_raising():
    """派单第 6 节第 5 条：枚举外的 code 收敛成 unknown，**不抛**。

    模型选了个不存在的选项是它没听话，已经有安全出口；抛出去只会让整条链路
    为一次不听话停摆。那个值必须写进 why —— 丢掉的话「模型在乱选」就查不出来了。
    """
    out = _run({"text": "尺寸不合适"},
               _ctx(model=_StubModel(_answer(reason_code="refund_all"))))
    assert out["reason_code"] == UNKNOWN
    assert out["confidence"] == 0.0
    assert "refund_all" in out["why"], "模型选了什么必须留在 why 里，否则查不出来"
    assert out["source"] == "model", "是模型判的就写 model，不因收敛而改口"


@pytest.mark.parametrize(("given", "expected"), [
    (1.7, 1.0), (-0.3, 0.0), ("高", 0.0), (None, 0.0), (0.42, 0.42), ("0.5", 0.5),
])
def test_confidence_is_clamped_never_raises(given, expected):
    """派单第 6 节第 6 条：越界 clamp 到 [0,1]，不是数字按 0.0，一律不抛。

    0.0 会把这条推给人工，正是「判不准就别猜」该有的结果。
    """
    out = _run({"text": "尺寸不合适"},
               _ctx(model=_StubModel(_answer(confidence=given))))
    assert out["confidence"] == expected
    assert 0.0 <= out["confidence"] <= 1.0


def test_missing_why_gets_a_fallback_sentence():
    """why 缺失不抛，填一句兜底 —— 这一栏是给人看的，空着等于没有理由。"""
    out = _run({"text": "尺寸不合适"},
               _ctx(model=_StubModel(json.dumps({"reason_code": "wrong_item"}))))
    assert out["why"], "why 不许为空"
    assert out["reason_code"] == "wrong_item"


def test_model_exception_is_re_raised_and_booked_as_a_failure(store):
    """调模型抛异常 → 落失败账后**原样 raise**（口径同 req_normalize）。

    不往 model_usage 编 0 token：那会把「调用失败」伪装成「这次很便宜」。
    """
    with pytest.raises(RuntimeError, match="模型网关不可达"):
        _run({"text": "尺寸不合适"}, _ctx(store, _BoomModel()))

    failures = store.list_model_call_failures()
    assert len(failures) == 1
    assert failures[0]["call_site"] == CALL_SITE
    assert store.list_model_usage() == [], "失败的调用不许进 model_usage"


# ===========================================================================
# 4. 模型账真的落了
# ===========================================================================
def test_records_model_usage(store):
    """派单第 6 节第 8 条：`record_model_usage` 真落了账，`model_usage` 表有行。

    漏接不会让任何别的测试变红，只会让成本统计静默偏低。归属键从 extras 取 ——
    invoker 已经把 plan_id / task_id / trace_id 一路带到这里。
    """
    _run({"text": "漏发了两个"}, _ctx(store, _StubModel(_answer())))

    rows = store.list_model_usage(trace_id=TRACE)
    assert len(rows) == 1, "词表外恰好烧一次模型，就该恰好落一行账"
    row = rows[0]
    assert (row["trace_id"], row["task_id"]) == (TRACE, TASK)
    assert row["call_site"] == CALL_SITE
    assert row["agent_role"] == "refund_intake"
    assert (row["tokens_in"], row["tokens_out"]) == (12, 7)


def test_lexicon_path_books_nothing(store):
    """快路径一次模型都不调，自然一行账都不该落。"""
    _run({"text": "质量问题"}, _ctx(store, _ExplodingModel()))
    assert store.list_model_usage() == []


def test_runs_without_a_store(store):
    """记账不接线时 skill 照旧产出 —— 记账不是 skill 的前置条件。"""
    out = _run({"text": "漏发了两个"}, _ctx(None, _StubModel(_answer())))
    assert out["reason_code"] == "wrong_item"


# ===========================================================================
# 5. prompt 与候选集
# ===========================================================================
def test_system_prompt_lists_every_candidate_code_and_meaning():
    """派单第 4 节：SYSTEM 必须逐个列出候选 code + 中文含义。

    模型只看得见这一段。省掉含义它就只能靠 code 的英文字面猜，
    而 `no_reason_return` 这种名字猜出来的多半是「不退货」。
    """
    model = _StubModel(_answer())
    _run({"text": "货不对板"}, _ctx(model=model))
    system = model.calls[0]["system"]
    for cand in DEFAULT_CANDIDATES:
        assert cand["code"] in system
        assert cand["hint"] in system
    assert UNKNOWN in system, "必须告诉模型判不出来时输出什么，否则它会猜"
    assert "JSON" in system


def test_custom_candidates_replace_the_builtin_three():
    """调用方给了候选就用它的，且模型只许在它给的集合里选。"""
    cands = [{"code": "shipping_damage", "label": "运输损坏", "hint": "运输途中压坏"}]
    model = _StubModel(_answer(reason_code="shipping_damage"))
    out = _run({"text": "快递把箱子压扁了", "candidates": cands}, _ctx(model=model))
    assert out["reason_code"] == "shipping_damage"

    system = model.calls[0]["system"]
    assert "shipping_damage" in system
    assert "quality_defect" not in system, "换了候选集就不该再把内置三个列给模型"


def test_builtin_code_outside_custom_candidates_is_not_a_lexicon_hit():
    """自定义候选没列 `quality_defect` 时，词表命中它也不算数。

    直接返回等于产出一个候选外的取值 —— 调用方拿它去查自己的类目表会查空。
    """
    cands = [{"code": "shipping_damage", "label": "运输损坏", "hint": "压坏"}]
    out = _run({"text": "质量问题", "candidates": cands}, _ctx(model=None))
    assert (out["reason_code"], out["source"]) == (UNKNOWN, "fallback")


def test_malformed_candidates_fall_back_to_the_builtin_three():
    """候选给成一堆没有 code 的东西，等同于没给 —— 不许放行一个空 code。"""
    out = _run({"text": "质量问题", "candidates": [{"label": "没有 code"}, "字符串"]},
               _ctx(model=_ExplodingModel()))
    assert (out["reason_code"], out["source"]) == ("quality_defect", "lexicon")


# ===========================================================================
# 6. 契约自述
# ===========================================================================
def test_contract_is_exactly_what_the_order_specifies():
    """派单第 4 节的交付契约，逐字段。改一个字都要先回去改派单。"""
    c = RefundReasonClassifySkill.contract
    assert c.name == "refund.reason_classify"
    assert c.version == "1.0.0"
    assert c.owner_roles == ["refund_intake"]
    assert c.failure_policy == "retry"
    assert c.max_retries == 1
    assert c.depends_tools == []
    assert set(c.output_schema) == OUT_FIELDS


def test_lexicon_is_byte_for_byte_the_one_in_run_requests():
    """本文件那份词表与 `scripts/run_requests.py::REASONS` 逐字节相等。

    两份并存是预期内的过渡态（整合期由主会话收成一份）。这条测试是那个过渡态的
    对冲：任何一边单独改了词，分类结果就和受理岗对不上，而两边各自的测试都是绿的。
    """
    import importlib.util
    import pathlib
    import sys

    root = pathlib.Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "_t101_run_requests", root / "scripts" / "run_requests.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_t101_run_requests"] = mod
    spec.loader.exec_module(mod)

    assert LEXICON == mod.REASONS, (
        "词表漂了 —— 分类器与受理岗对同一个词会给出两个 code。\n"
        f"  本文件独有：{sorted(set(LEXICON) - set(mod.REASONS))}\n"
        f"  受理岗独有：{sorted(set(mod.REASONS) - set(LEXICON))}"
    )
    assert {c["code"] for c in DEFAULT_CANDIDATES} == set(mod.REASONS.values())
