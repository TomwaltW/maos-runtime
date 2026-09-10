"""发声面的三道门（`maos/roundtable/speaker.py`）。

本文件钉住四件事：

1. **取数口径**（`stages.numbers_in`）：标识符与数字分两集，千分位、中文数字、
   混合单位都在抽数字之前化掉。十二个探针的期望值逐个写死 —— 这一层松一格，
   上面三道门全部跟着松，而且不会有任何测试变红。
2. **三道门**：数字越界、逐条编造、思考过程漏出，各一个假模型用例。
3. **重试一次**：第一稿违规 → 把违规项列给模型 → 第二稿干净就发第二稿；
   两稿都脏才回退事实卡，且 `fallback_reason` 记 `validation` 而不是 `empty`。
4. **回归语料**（`fixtures/roundtable_runs/`）：真模型 8 轮 × 5 岗的 facts/speech
   全文，钉住「三岗那 18 段编造全被拦、上游两岗放行」这条线不许回退。

🔴 模型一律**显式注入**，与 `test_roundtable_team.py` 同一条理由：`conftest.py`
不清 `MAOS_LLM_*`，无参构造会在配了 key 的机器上真打网关。
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from maos.agents.base import AgentIdentity
from maos.model.client import ModelClient, ModelResponse, Tier
from maos.roundtable import speaker as sp
from maos.roundtable.stages import numbers_in

FIXTURES = Path(__file__).parent / "fixtures" / "roundtable_runs"

#: 上游两岗 —— 事实卡厚，发言该放行。
UPSTREAM = ("refund-intake", "refund-policy")
#: 下游三岗 —— 事实卡只有一行，实测就是它们在编。
DOWNSTREAM = ("refund-evidence", "refund-risk", "refund-finance")


# --------------------------------------------------------------------------
# 假件
# --------------------------------------------------------------------------
class _ScriptModel(ModelClient):
    """按调用序号依次回给定文本。非 `ScriptedModelClient`，所以 Speaker 认它是真模型。

    用序号而不是按 user 匹配，是因为本文件要测的正是**同一份 user 调两次**
    （第一稿 + 重试稿）时两稿不同 —— 按内容匹配的假件表达不了这件事。
    """

    model = "script-1"

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []

    def complete(self, *, system: str, user: str, tier: str) -> ModelResponse:
        self.calls.append({"system": system, "user": user, "tier": tier})
        i = min(len(self.calls) - 1, len(self.replies) - 1)
        return ModelResponse(text=self.replies[i], model=self.model)


def _speaker(*replies: str) -> tuple[sp.Speaker, _ScriptModel]:
    model = _ScriptModel(*replies)
    identity = AgentIdentity(
        agent_id="refund-risk", role="refund_risk", duty="只提示风险，不裁定",
        allowed_skills=frozenset(), allowed_tools=frozenset(),
        write_scope=frozenset({"artifact"}), max_risk="L",
        model_tier=Tier.LIGHT, max_self_repair=0,
    )
    return sp.Speaker(identity, model, "风险反欺诈岗"), model


#: 一张**厚**事实卡：带逐条行，所以不会被第 5 条的止血开关跳过。
FACTS = ("待筛查 12 单，其中 8 单已裁定批准、会走到付款\n"
         "逐单风险：\n"
         "  · 第 5 行 ORD-2026-1011：低风险\n"
         "  · 第 9 行 ORD-2026-1029：低风险")


# --------------------------------------------------------------------------
# 1. 取数口径：十二个探针，期望值写死
# --------------------------------------------------------------------------
@pytest.mark.parametrize(("text", "ids", "nums"), [
    # 标识符整 token 抽走，**不再往 nums 里塞它自己的数字**
    ("ORD-2026-1016", {"ORD-2026-1016"}, set()),
    ("RC-ORD-2026-1000", {"RC-ORD-2026-1000"}, set()),
    # 政策号在数字维度不可判（001 与 v1 都塌成 1），所以它只能靠 ids 这一集
    ("AS-001@v1", {"AS-001@v1"}, set()),
    ("第 5 行", set(), {Decimal(5)}),
    ("4038.78", set(), {Decimal("4038.78")}),
    # 千分位：不去逗号会切成 {4, 38.78}，模型加个逗号就能把门变成误报机
    ("4,038.78", set(), {Decimal("4038.78")}),
    # 混合单位：4 千是一个数，不是 4 和 1000 两个
    ("约 4 千", set(), {Decimal(4000)}),
    ("12 单", set(), {Decimal(12)}),
    ("33 > 30", set(), {Decimal(30), Decimal(33)}),
    # 中文数字：漏了它，一句「另外两单」就能整条绕过
    ("两行", set(), {Decimal(2)}),
    ("15:50", set(), {Decimal(15), Decimal(50)}),
    ("第 33 天申请，33 > 30", set(), {Decimal(30), Decimal(33)}),
])
def test_numbers_in_splits_ids_from_numbers(text: str, ids: set, nums: set) -> None:
    got_ids, got_nums = numbers_in(text)
    assert got_ids == ids
    assert got_nums == nums


@pytest.mark.parametrize(("line", "listed"), [
    ("  · 第 5 行 ORD-2026-1011：…", True),     # 事实卡自己的前缀
    ("· 第 1 行 ORD-2026-1000：通过", True),     # 模型爱写的无前导空格形态
    ("• 第 2 行", True),
    ("- 第 3 行", True),
    ("* 第 4 行", True),
    ("1. 第 5 行", True),                       # 编号形态：只认符号会整条漏掉
    ("2、第 6 行", True),
    ("3) 第 7 行", True),
    ("这张表共 12 行：能建案 12 行", False),
    ("另有 2 行带提示（不阻断）", False),
])
def test_list_line_detection_is_symbol_agnostic(line: str, listed: bool) -> None:
    assert sp.has_list_lines(line) is listed


def test_flat_only_covers_summary_only_cards() -> None:
    """薄的判据是「没有逐条行 **且** 正文 ≤ 2 行」。

    光判逐条行会把单案模式那张七行「字段：值」的事实卡也算薄 —— 那会把单案圆桌
    的模型发言一起关掉，不是本开关要止的血。
    """
    assert sp.is_flat("待筛查 12 单，其中 8 单已裁定批准、会走到付款")
    assert not sp.is_flat(FACTS)                       # 有逐条行
    assert not sp.is_flat("订单号：X\n商品：Y\n订单实付：1\n申报金额：2\n"
                          "诉求类型：Z\n申请日期：D\n随案证据：0 份")


# --------------------------------------------------------------------------
# 2. 三道门
# --------------------------------------------------------------------------
def test_gate_one_blocks_ids_and_numbers_absent_from_own_facts() -> None:
    """门 1：说了自己事实卡里没有的单号或数字 —— 两稿都犯，回退事实卡。

    语料取自实测：风险岗把规则岗的驳回判据整条搬了过来。
    """
    bad = ("· 第 6 行 ORD-2026-1016（七天无理由）：AS-001@v1 窗口 30 天，"
           "第 33 天申请，33 > 30；依据 AS-001@v1")
    speaker, model = _speaker(bad, bad)
    speech, by_model, reason = speaker.speak(FACTS, [])

    assert speech == FACTS
    assert by_model is False
    assert reason == sp.FALLBACK_VALIDATION
    assert len(model.calls) == 2, "违规要重试一次，不是直接回退"


def test_gate_two_blocks_per_row_lines_when_facts_have_none() -> None:
    """门 2：事实卡没有逐条结果，发言却逐条列了。

    这是实测里最凶的一类 —— 证据岗 8/8 轮凭空编出逐单核验结论，而它的事实卡
    只有一行。**门 2 与门 1 分开判**：编造用的行号和单号全都来自上游发言，
    只看数字集合的话，这类越界里有一半是数字上合法的。
    """
    # 三行、无逐条行 —— 刻意避开第 5 条的止血开关（那条只吃 ≤2 行的），
    # 这样撞的是门 2 本身，而不是开关。两者分开测才说得清是哪一道在起作用。
    flat = ("进入证据核验范围的有 8 单（裁定驳回的 4 单不看证据）\n"
            "证据核验 skill 已装载\n"
            "本轮无新增材料")
    listed = ("进入证据核验范围的有 8 单\n"
              "· 第 8 行：物流签收与质检结论一致，举证齐全\n"
              "· 第 4 行：物流签收与质检结论一致，举证齐全")
    speaker, model = _speaker(listed, listed)
    speech, by_model, reason = speaker.speak(flat, [])

    assert speech == flat
    assert by_model is False
    assert reason == sp.FALLBACK_VALIDATION
    # 重试提示要明说「只说总结句」，光说「你违规了」拿不到修正
    assert "只说总结句" in model.calls[1]["user"]


def test_gate_three_blocks_thinking_out_loud() -> None:
    """门 3：核对与重算的过程不许进发言。

    语料取自实测 run_8 的财务岗：先报了一个合计，又在同一条消息里说「这个数不对，
    我重算」—— 房间里读到的是当众自我怀疑，而那个数正是编的。
    """
    bad = "8 单预演完了。等等，我核一下第 3 行——这个数不对，我重算。"
    speaker, model = _speaker(bad, bad)
    speech, by_model, reason = speaker.speak(FACTS, [])

    assert speech == FACTS
    assert reason == sp.FALLBACK_VALIDATION
    assert "不要把核对、重算的过程写进发言" in model.calls[1]["user"]


def test_clean_speech_passes_both_gates_without_retry() -> None:
    """干净的一稿：一次调用，原样发出去，不记回退原因。"""
    ok = ("这一批 12 单待筛查，其中 8 单已裁定批准会走到付款。逐单风险：\n"
          "· 第 5 行 ORD-2026-1011：低风险\n"
          "· 第 9 行 ORD-2026-1029：低风险")
    speaker, model = _speaker(ok)
    speech, by_model, reason = speaker.speak(FACTS, [])

    assert speech == ok
    assert by_model is True
    assert reason == ""
    assert len(model.calls) == 1


# --------------------------------------------------------------------------
# 3. 重试与回退原因
# --------------------------------------------------------------------------
def test_retry_succeeds_and_second_draft_is_what_gets_said() -> None:
    """第一稿脏、第二稿干净 —— 发第二稿，算模型说的，不记回退。"""
    dirty = "· 第 6 行 ORD-2026-1016：33 > 30，驳回"
    clean = "12 单待筛查，8 单会走到付款，逐单都是低风险。"
    speaker, model = _speaker(dirty, clean)
    speech, by_model, reason = speaker.speak(FACTS, [])

    assert speech == clean
    assert by_model is True
    assert reason == ""
    assert len(model.calls) == 2
    assert "ORD-2026-1016" in model.calls[1]["user"], "重试要把违规项逐条摆出来"


def test_flat_facts_skip_the_model_and_record_skipped() -> None:
    """第 5 条止血：事实卡只有总结句 → 一次模型都不调，原因记 `skipped`。

    **与 `no_model` 分开记**：那一个说的是「这台机器没配模型」，这一个说的是
    「配了，但这一岗这一轮没东西可说」。Evidence Bundle 要分得清这两件事。
    """
    flat = "待筛查 12 单，其中 8 单已裁定批准、会走到付款"
    speaker, model = _speaker("随便说点什么")
    speech, by_model, reason = speaker.speak(flat, [])

    assert speech == flat
    assert by_model is False
    assert reason == sp.FALLBACK_SKIPPED
    assert model.calls == [], "薄事实卡一次模型都不该调"


def test_empty_reply_is_recorded_as_empty_not_validation() -> None:
    """模型回空话与模型违规是两件事，回退原因不许混。"""
    speaker, model = _speaker("   ")
    speech, by_model, reason = speaker.speak(FACTS, [])

    assert speech == FACTS
    assert reason == sp.FALLBACK_EMPTY
    assert len(model.calls) == 1, "空回答不重试 —— 它不是违规，是没响应"


def test_no_model_is_recorded_as_no_model() -> None:
    speaker, _model = _speaker("x")
    speaker.model = None
    speech, by_model, reason = speaker.speak(FACTS, [])

    assert speech == FACTS
    assert by_model is False
    assert reason == sp.FALLBACK_NO_MODEL


# --------------------------------------------------------------------------
# 4. 回归语料：真模型 8 轮，钉住三岗被拦、上游两岗放行
# --------------------------------------------------------------------------
def _runs() -> list[dict]:
    files = sorted(FIXTURES.glob("run_*.json"))
    assert files, f"回归语料不在 {FIXTURES}"
    return [json.loads(f.read_text(encoding="utf-8")) for f in files]


def test_fixture_records_the_commit_it_was_generated_on() -> None:
    """每一份语料都要带生成它的 HEAD、入口与语料文件。

    诊断第一步是对版本：这批 dump 跑在 `0ad62b5` 上，而那个 commit 之前的房间
    截图长得完全不同（事实卡里当时还硬编码着「skill 已装载」）。不带版本的
    transcript 会把「上一版的症状」当成「这一版的 bug」查。
    """
    for run in _runs():
        assert len(run["commit"]) == 40, "commit 要写全 sha，不是短 sha"
        assert run["entry"] == "on_sheet"
        assert run["corpus"].endswith(".csv")
        assert {s["role"] for s in run["stages"]} == set(UPSTREAM) | set(DOWNSTREAM)


def test_downstream_fabrications_are_all_blocked() -> None:
    """下游三岗那 24 段实测发言，一段都不许放行。

    这 24 段里编出过 7 个假金额、1 个不存在的历史订单号（`ORD-2025-0887`）和
    24 段假证据结论，而三岗的事实卡各只有一行。**断言的是「全拦」不是「拦住 18 段」**：
    第 5 条的止血开关先一步把它们挡在模型之外，门是第二道保险；两道任意一道松了
    这条都会红。
    """
    leaked: list[str] = []
    for run in _runs():
        for stage in run["stages"]:
            if stage["role"] not in DOWNSTREAM:
                continue
            facts, speech = stage["facts"], stage["speech"]
            blocked = sp.is_flat(facts) or bool(sp.violations(speech, facts))
            if not blocked:
                leaked.append(f"run {run['run']} / {stage['role']}")
    assert not leaked, f"这些编造漏过去了：{leaked}"


def test_downstream_speech_would_fail_the_gates_on_its_own() -> None:
    """把止血开关摘掉单看：这 24 段仍然段段撞门。

    分开钉是因为两者作用不同 —— 开关是止血、逐单事实卡补上后自动失效，门是永久的。
    只钉上一条的话，将来开关失效那天，门有没有真接住不会有任何测试回答。
    """
    for run in _runs():
        for stage in run["stages"]:
            if stage["role"] not in DOWNSTREAM:
                continue
            bad = sp.violations(stage["speech"], stage["facts"])
            assert bad, f"run {run['run']} / {stage['role']} 居然全过门"


def test_upstream_speech_is_mostly_let_through() -> None:
    """上游两岗事实卡厚，实测零编造 —— 门基本不该拦它们。

    三次例外全在规则岗，且**逐条核过，三次都拦得对**：它们都是在「接住上一位」
    那句里引了受理岗的数字（run 1「你标的两行封顶…第 5 行 ORD-2026-1011」、
    run 2「你那边 2 行带提示」、run 5「两条不冲突」），那些数字确实不在规则岗
    自己的事实卡里。

    **这三次暴露的是规则 6 与门 1 的天然张力**：「先接住他的话」几乎必然要引上游的数，
    而门只跟自己的事实卡比。张力认了，不靠放宽门解决 —— 松到 16 段全放行就等于
    把口径改回上游白名单，而那条路量过，它专门放过「合法单号 + 编造结论」这一类。
    出路是接话改用定性说法（「受理岗标的封顶不影响裁定」），重试提示已经在这么教。
    """
    blocked = [f"run {run['run']} / {stage['role']}"
               for run in _runs() for stage in run["stages"]
               if stage["role"] in UPSTREAM and sp.violations(stage["speech"],
                                                              stage["facts"])]
    assert len(blocked) <= 3, f"上游被拦得比实测多：{blocked}"
    assert all("refund-policy" in b for b in blocked), \
        f"受理岗一段都不该被拦，实际：{blocked}"
