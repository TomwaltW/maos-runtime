"""usage 解析要认两家口径 —— T54 §2（cumora 折账第 18 条）。

折账原话：``client.py`` 只读 ``prompt_tokens`` / ``completion_tokens``，
Anthropic 口径把缓存读写单列为 ``cache_read_input_tokens`` /
``cache_creation_input_tokens``，这些字段在 MAOS 侧会被**整段丢掉**。

丢掉的后果不是「少算一点」：那一行落库时 ``estimated=0``（客户端类型是真客户端），
于是一次真调用被记成**零成本、且声称是真实计费**。比没有成本视图更糟 ——
它看起来是有数的。

本文件同时钉住两家**不混算**这条：OpenAI 的 prompt_tokens 已含缓存命中部分，
再加一次就是重复计数；Anthropic 的三个输入字段互不重叠，所以求和。
"""

from __future__ import annotations

import logging

from maos.model.client import _usage_tokens


def test_openai_dialect_reads_prompt_and_completion():
    tokens_in, tokens_out, detail = _usage_tokens(
        {"prompt_tokens": 1200, "completion_tokens": 340, "total_tokens": 1540})
    assert (tokens_in, tokens_out) == (1200, 340)
    assert detail["dialect"] == "openai"


def test_openai_cached_tokens_are_detail_only_not_added_twice():
    """``cached_tokens`` 是 prompt_tokens 的**子集**，加进去就是重复计数。"""
    tokens_in, _, detail = _usage_tokens({
        "prompt_tokens": 1200, "completion_tokens": 10,
        "prompt_tokens_details": {"cached_tokens": 1000},
    })
    assert tokens_in == 1200            # 不是 2200
    assert detail["cached_tokens"] == 1000


def test_anthropic_dialect_sums_three_input_fields():
    """三个输入字段互不重叠，全都是真花掉的输入侧 token。"""
    tokens_in, tokens_out, detail = _usage_tokens({
        "input_tokens": 500,
        "cache_read_input_tokens": 8000,
        "cache_creation_input_tokens": 1200,
        "output_tokens": 420,
    })
    assert tokens_in == 9700            # 500 + 8000 + 1200
    assert tokens_out == 420
    assert detail["dialect"] == "anthropic"
    # 明细逐项留下来：库里两列存不下分项（表结构冻结），排查时要看得到。
    assert detail["cache_read_input_tokens"] == 8000
    assert detail["cache_creation_input_tokens"] == 1200


def test_anthropic_without_cache_fields_still_reads():
    tokens_in, tokens_out, detail = _usage_tokens(
        {"input_tokens": 300, "output_tokens": 40})
    assert (tokens_in, tokens_out) == (300, 40)
    assert detail["dialect"] == "anthropic"


def test_the_old_reader_would_have_dropped_everything():
    """回归本条的**病症**：旧读法在 Anthropic 口径上得到 (0, 0)。

    这条不测新代码，测的是「这个 bug 真的存在过」—— 没有它，上面几条绿了也
    说不清修的是什么。
    """
    usage = {"input_tokens": 500, "cache_read_input_tokens": 8000,
             "output_tokens": 420}
    old_in = int(usage.get("prompt_tokens") or 0)
    old_out = int(usage.get("completion_tokens") or 0)
    assert (old_in, old_out) == (0, 0)
    assert _usage_tokens(usage)[0] == 8500


def test_unknown_dialect_is_zero_and_loud(caplog):
    """认不出就如实记 0，但必须留声 —— 静默的 0 会被当成「这次很便宜」。"""
    with caplog.at_level(logging.WARNING):
        tokens_in, tokens_out, detail = _usage_tokens({"weird_field": 12})
    assert (tokens_in, tokens_out) == (0, 0)
    assert detail["dialect"] == "unknown"
    assert any("usage 字段名不认识" in r.getMessage() for r in caplog.records)


def test_empty_usage_is_not_an_unknown_dialect(caplog):
    """网关一个 usage 都没回，与「回了但不认识」是两回事，不该刷警告。"""
    with caplog.at_level(logging.WARNING):
        assert _usage_tokens({}) == (0, 0, {"dialect": "none"})
    assert not [r for r in caplog.records if "不认识" in r.getMessage()]


def test_non_integer_values_fall_back_without_raising(caplog):
    """口径同 ``_safe_int``：计数错不该让一次已经成功的调用失败，但要留声。"""
    with caplog.at_level(logging.WARNING):
        tokens_in, tokens_out, _ = _usage_tokens(
            {"prompt_tokens": "n/a", "completion_tokens": 7})
    assert (tokens_in, tokens_out) == (0, 7)
    assert any("不是整数" in r.getMessage() for r in caplog.records)
