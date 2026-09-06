"""`sheet.header_map` 的契约测试 —— 表头认列从「全等」升级成「模型理解」之后要守什么。

本文件守四件事：

- **顺序**：别名全等匹配是快路径，模型只处理剩菜。常规表一次模型都不调 ——
  这条用一个 `complete()` 里直接 `AssertionError` 的假模型钉死。顺序反了的症状
  不是变红，是「模型偶尔推翻一个本来对的匹配」，线上查不出来。
- **失败姿态的不对称**：JSON 坏了 → 抛（故障）；模型配错了 → 丢弃 + 进 `missing`
  （它没听话，有安全出口）。这两条混成一条，链路要么为一次不听话停摆，要么把
  一次真故障伪装成「这张表就是认不出来」。
- **四个 dict 键集合恒等**：`mapping` / `confidence` / `why` / `source` 认出来的都有、
  没认出来的都没有。下游按 `mapping` 的键去读 `why`，破了这条就是 KeyError。
- **不猜**：模型把同一列配给两个字段时**两条都丢**，不挑「看起来更像的」。
  猜错一列 = 套错一条政策，这是本仓库最贵的一类错。

入参全部在本文件里现造：本 skill 只经入参吃数、不读任何文件，所以测试也不读
`scenarios/**`。别名表也**故意重抄一份**期望值 —— 从被测模块里 import 常量来断言，
会让「别名表改了」和「断言跟着改了」互相掩护。
"""

from __future__ import annotations

import json

import pytest

from maos.agents.base import AgentIdentity
from maos.model.client import ModelClient, ModelResponse, Tier
from maos.skills import registry
from maos.skills.builtin.sheet_header_map import CALL_SITE, SheetHeaderMapSkill
from maos.skills.contract import SkillContext
from maos.skills.invoker import SkillInvoker

SKILL = "sheet.header_map"

#: 老板存 Excel 得到的那张常规表 —— 五列全部能被别名全等命中。
STANDARD_HEADER = ["订单号", "诉求类型", "申报金额", "申请日期", "说明"]

FOUR_DICTS = ("mapping", "confidence", "why", "source")
OUTPUT_KEYS = {*FOUR_DICTS, "unmapped", "missing", "invocation_id"}


# ---------------------------------------------------------------- 假模型三只
class _NoModelAllowed(ModelClient):
    """一被调用就红。用来钉「这条路径不该调模型」，比事后数 calls 更早报错。"""

    def complete(self, *, system: str, user: str, tier: str) -> ModelResponse:
        raise AssertionError("别名已经认全，这条路径不该调模型")


class _Fake(ModelClient):
    """按预设文本作答，并留下调用现场（system / user / tier）供断言。"""

    def __init__(self, text: str, model: str = "fake-light") -> None:
        self.text = text
        self.model = model
        self.calls: list[dict] = []

    def complete(self, *, system: str, user: str, tier: str) -> ModelResponse:
        self.calls.append({"system": system, "user": user, "tier": tier})
        return ModelResponse(text=self.text, tokens_in=7, tokens_out=3, model=self.model)


class _Boom(ModelClient):
    """网关挂了。"""

    model = "fake-down"

    def complete(self, *, system: str, user: str, tier: str) -> ModelResponse:
        raise RuntimeError("网关 502")


class _RecordingStore:
    """只认这两个写入口的假库 —— 本 skill 除了模型账不许写任何东西，正好也守住了。"""

    def __init__(self) -> None:
        self.usage: list[dict] = []
        self.failures: list[dict] = []

    def insert_model_usage(self, row: dict) -> None:
        self.usage.append(row)

    def insert_model_call_failure(self, row: dict) -> None:
        self.failures.append(row)


def _ctx(model=None, store=None, **extras) -> SkillContext:
    return SkillContext(model=model, store=store, extras=dict(extras))


def _run(header, model=None, store=None, **payload) -> dict:
    return SheetHeaderMapSkill().run({"header": header, **payload}, _ctx(model, store))


def _answer(mapping: dict, confidence: dict | None = None, why: dict | None = None) -> str:
    body: dict = {"mapping": mapping}
    if confidence is not None:
        body["confidence"] = confidence
    if why is not None:
        body["why"] = why
    return json.dumps(body, ensure_ascii=False)


# ---------------------------------------------------------------- 1 顺序：快路径
def test_standard_header_never_touches_the_model():
    """常规表五列全走别名，一次模型都不调（派单 §3.1）。"""
    out = _run(STANDARD_HEADER, model=_NoModelAllowed())

    assert out["mapping"] == {
        "order_id": "订单号", "reason": "诉求类型", "amount": "申报金额",
        "date": "申请日期", "note": "说明",
    }
    assert set(out["source"].values()) == {"alias"}
    assert set(out["confidence"].values()) == {1.0}
    assert out["missing"] == [] and out["unmapped"] == []


def test_unknown_extra_column_alone_does_not_wake_the_model():
    """待认字段为空时，哪怕还有没认领的表头也不调模型 —— 没有剩菜就没有配对可做。"""
    out = _run([*STANDARD_HEADER, "内部备注栏"], model=_NoModelAllowed())

    assert out["unmapped"] == ["内部备注栏"]
    assert set(out["source"].values()) == {"alias"}


def test_model_only_sees_what_alias_could_not_claim():
    """给模型的提示词里只有剩菜：别名已认的列不出现在候选里（派单 §3.1）。"""
    fake = _Fake(_answer({"reason": "退货原因（必填）"}))
    _run(["订单号", "退货原因（必填）", "申报金额"], model=fake)

    prompt = fake.calls[0]["user"]
    assert "退货原因（必填）" in prompt
    for claimed in ("订单号", "申报金额"):
        assert f"  - {claimed}" not in prompt, "别名已认的列不该出现在候选里"
    assert fake.calls[0]["tier"] == Tier.LIGHT


# ---------------------------------------------------------------- 2 模型补认
def test_model_picks_up_what_alias_missed():
    """`退货原因（必填）` 别名认不出，模型认得出 —— source 记 model，不是 alias。"""
    fake = _Fake(_answer({"reason": "退货原因（必填）"},
                         {"reason": 0.9},
                         {"reason": "括号里是填写说明，去掉就是退货原因"}))
    out = _run(["订单号", "退货原因（必填）"], model=fake)

    assert out["mapping"]["reason"] == "退货原因（必填）"
    assert out["source"] == {"order_id": "alias", "reason": "model"}
    assert out["confidence"]["reason"] == 0.9
    assert out["why"]["reason"] == "括号里是填写说明，去掉就是退货原因"
    assert out["missing"] == [] and out["unmapped"] == []


def test_model_answer_without_why_gets_a_generated_chinese_reason():
    """模型不给理由也要有理由：`why` 是给人看的，空着等于这一列无法复核。"""
    out = _run(["订单号", "退货原因（必填）"],
               model=_Fake(_answer({"reason": "退货原因（必填）"}, {"reason": 0.8})))

    assert "退货原因（必填）" in out["why"]["reason"]
    assert out["why"]["reason"].strip()


# ---------------------------------------------------------------- 3 没有模型
def test_no_model_degrades_to_missing_without_raising():
    """`ctx.model is None` 是规则兜底，不是失败 —— 上层忘了接线不该拖挂整条链路。"""
    out = _run(["订单号", "退货原因（必填）"], model=None)

    assert out["mapping"] == {"order_id": "订单号"}
    assert out["missing"] == ["reason"]
    assert out["unmapped"] == ["退货原因（必填）"]


# ---------------------------------------------------------------- 4/5 模型没听话
def test_model_inventing_a_column_is_discarded():
    """模型编了个表里没有的列名 → 丢弃那一条，该字段进 missing，**不抛**。"""
    out = _run(["订单号", "退货原因（必填）"],
               model=_Fake(_answer({"reason": "退款理由"}, {"reason": 0.99})))

    assert "reason" not in out["mapping"]
    assert out["missing"] == ["reason"]
    assert out["unmapped"] == ["退货原因（必填）"]


def test_conflicting_claims_discard_both_sides():
    """同一列被两个字段抢 → **两条都丢**。不许挑「看起来更像的」——那是猜。"""
    out = _run(["订单号", "问题类型"],
               model=_Fake(_answer({"reason": "问题类型", "note": "问题类型"},
                                   {"reason": 0.9, "note": 0.7})))

    assert "reason" not in out["mapping"] and "note" not in out["mapping"]
    assert out["missing"] == ["reason"]
    assert out["unmapped"] == ["问题类型"]


def test_model_may_not_overwrite_a_column_alias_already_claimed():
    """模型去动别名已认的字段 → 丢弃。别名命中的列模型无权改（派单 §3.1）。"""
    out = _run(["订单号", "闲置列"],
               model=_Fake(_answer({"order_id": "闲置列"}, {"order_id": 0.95})))

    assert out["mapping"]["order_id"] == "订单号"
    assert out["source"]["order_id"] == "alias"
    assert out["unmapped"] == ["闲置列"]


def test_json_object_without_mapping_is_read_as_gave_up():
    """合法 JSON 对象但没给 mapping：读作「一条都没配上」，走安全出口，不抛。"""
    out = _run(["订单号", "退货原因（必填）"], model=_Fake('{"note":"我不确定"}'))

    assert out["missing"] == ["reason"]


# ---------------------------------------------------------------- 6 JSON 坏了要抛
def test_non_json_output_raises_valueerror():
    """输出不是合法 JSON 是**故障**，抛出去交给 failure_policy=retry 再试一次。"""
    with pytest.raises(ValueError, match="非合法 JSON"):
        _run(["订单号", "退货原因（必填）"], model=_Fake("这里给你解释一下：..."))


def test_json_that_is_not_an_object_raises_valueerror():
    """是 JSON 但不是对象，同样是故障。"""
    with pytest.raises(ValueError, match="应为 JSON 对象"):
        _run(["订单号", "退货原因（必填）"], model=_Fake('["退货原因（必填）"]'))


def test_header_that_is_not_a_list_raises_valueerror():
    with pytest.raises(ValueError, match="必须是列表"):
        _run("订单号,诉求类型")


# ---------------------------------------------------------------- 7 BOM
def test_bom_header_hits_alias():
    """Excel 存 UTF-8 CSV 会在首列名前塞 BOM。判据与 `run_requests.scan_header` 一致。"""
    out = _run(["﻿订单号", "诉求类型"], model=_NoModelAllowed())

    assert out["source"]["order_id"] == "alias"
    # 出参给的是**原文**：调用方拿它当 DictReader 的键去取值，剥过 BOM 的取不到。
    assert out["mapping"]["order_id"] == "﻿订单号"


# ---------------------------------------------------------------- 8 形状与可复现
@pytest.mark.parametrize("header,model", [
    (STANDARD_HEADER, None),
    (["订单号", "退货原因（必填）"], _Fake(_answer({"reason": "退货原因（必填）"}))),
    (["随便写的一列"], None),
])
def test_four_dicts_share_one_key_set(header, model):
    """认出来的字段在四个 dict 里都有，没认出来的四个里都没有。"""
    out = _run(header, model=model)

    assert set(out) == OUTPUT_KEYS
    keys = [set(out[name]) for name in FOUR_DICTS]
    assert all(k == keys[0] for k in keys), f"四个 dict 键集合不一致：{keys}"
    assert set(out["missing"]).isdisjoint(keys[0])


def test_unmapped_and_missing_are_sorted():
    """出参逐字节可复现是本仓库的一贯要求，两个列表都要排序。"""
    out = _run(["丙列", "甲列", "乙列"], model=None)

    assert out["unmapped"] == sorted(out["unmapped"]) == ["丙列", "乙列", "甲列"]
    assert out["missing"] == sorted(out["missing"]) == ["order_id", "reason"]


def test_same_input_gives_the_same_output_twice():
    """除 invocation_id 外逐字一致 —— 认列结论要能事后复算。"""
    skill = SheetHeaderMapSkill()
    payload = {"header": ["订单号", "退货原因（必填）", "金额/元"]}
    fake = _Fake(_answer({"amount": "金额/元", "reason": "退货原因（必填）"},
                         {"amount": 0.7, "reason": 0.9}))

    first = skill.run(payload, _ctx(fake))
    second = skill.run(payload, _ctx(fake))

    assert first.pop("invocation_id") != second.pop("invocation_id")
    assert first == second
    # 键顺序跟 required+optional 走，不随模型返回的 JSON 顺序漂。
    assert list(first["mapping"]) == ["order_id", "reason", "amount"]


# ---------------------------------------------------------------- 别名优先级
def test_alias_tuple_order_decides_which_column_wins():
    """表里同时有「诉求类型」和「退款原因」→ 取别名元组里靠前的，另一列进 unmapped。

    ``model=None``：剩下的 amount/date/note 还没认到，留着模型会被叫醒 ——
    本条要验的是别名内部的优先级，不是那一步。
    """
    out = _run(["订单号", "退款原因", "诉求类型"], model=None)

    assert out["mapping"]["reason"] == "诉求类型"
    assert out["unmapped"] == ["退款原因"]


# ---------------------------------------------------------------- 置信度收敛
@pytest.mark.parametrize("raw,expected", [
    (0.9, 0.9), (5, 1.0), (-3, 0.0), ("高", 0.0), (None, 0.0), (True, 0.0),
])
def test_confidence_is_coerced_and_clamped(raw, expected):
    """缺失/非数字 → 0.0，越界 → clamp。一律不抛：置信度写歪不该掀掉整张表。"""
    out = _run(["订单号", "退货原因（必填）"],
               model=_Fake(_answer({"reason": "退货原因（必填）"}, {"reason": raw})))

    assert out["confidence"]["reason"] == expected


def test_nan_confidence_does_not_slip_through_the_clamp():
    """`json.loads` 默认认 NaN，而 NaN 和谁比都是 False，会静默穿透 min/max。"""
    out = _run(["订单号", "退货原因（必填）"],
               model=_Fake('{"mapping":{"reason":"退货原因（必填）"},'
                           '"confidence":{"reason":NaN}}'))

    assert out["confidence"]["reason"] == 0.0


# ---------------------------------------------------------------- 模型账
def test_model_usage_is_recorded_with_this_files_call_site():
    store = _RecordingStore()
    _run(["订单号", "退货原因（必填）"], model=_Fake(_answer({"reason": "退货原因（必填）"})),
         store=store)

    assert len(store.usage) == 1
    assert store.usage[0]["call_site"] == CALL_SITE
    assert store.usage[0]["tokens_in"] == 7 and store.usage[0]["tokens_out"] == 3
    assert store.failures == []


def test_model_failure_is_recorded_then_reraised():
    """失败先落账再原样上抛 —— 落账不许把原始异常换掉。"""
    store = _RecordingStore()
    with pytest.raises(RuntimeError, match="网关 502"):
        _run(["订单号", "退货原因（必填）"], model=_Boom(), store=store)

    assert len(store.failures) == 1
    assert store.failures[0]["call_site"] == CALL_SITE
    assert store.failures[0]["error_kind"] == "RuntimeError"
    assert store.usage == [], "失败的调用不许往 model_usage 编一行 0 token"


# ---------------------------------------------------------------- 入参可覆盖
def test_required_and_optional_are_overridable():
    """只要 amount 时，认不到 amount 才算 missing；order_id 缺了也不该报进去。"""
    out = _run(["申报金额", "说明"], required=["amount"], optional=[], model=None)

    assert out["mapping"] == {"amount": "申报金额"}
    assert out["missing"] == []
    assert out["unmapped"] == ["说明"], "不在 required/optional 里的字段不认列"


# ---------------------------------------------------------------- 注册与契约
def test_registered_by_file_drop():
    """投放即注册（C-1）：放进 builtin/ 就该被 discover() 扫到，不改 __init__.py。"""
    assert registry.get(SKILL) is SheetHeaderMapSkill


def test_contract_fields_are_what_the_callers_read():
    c = SheetHeaderMapSkill.contract

    assert (c.name, c.version) == (SKILL, "1.0.0")
    assert c.preconditions == ["header"]
    assert c.depends_tools == []
    assert (c.failure_policy, c.max_retries) == ("retry", 1)
    assert c.owner_roles == ["refund_intake"]
    assert set(c.output_schema) == OUTPUT_KEYS
    assert c.security_boundary.strip(), "安全边界不许空 —— Agent 靠它决定要不要调"


def test_invoker_path_carries_the_invocation_id():
    """走 SkillInvoker 时锚点用 invoker 那个，不许 skill 自己另生成一个。"""
    identity = AgentIdentity(
        agent_id="test-sheet-header", role="test_intake",
        duty="测试夹具：授权表头映射器",
        allowed_skills=frozenset({SKILL}), allowed_tools=frozenset(),
        write_scope=frozenset(), max_risk="L", model_tier=Tier.LIGHT,
    )
    result = SkillInvoker(identity, None).invoke(SKILL, {"header": STANDARD_HEADER})

    assert result.status == "ok"
    assert result.output["invocation_id"] == result.invocation_id
