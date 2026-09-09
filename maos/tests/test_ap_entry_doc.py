"""`docs/ap-entry.md` 的机器守卫 —— 那几句最容易被改软的话必须还在。

## 为什么值得一条测试

这份文档讲的不是用法，是**授权边界**：抽取结果不许直接进付款、单号认不出就报错
不猜、引擎一行没改。三句话都有一个共同的性质 —— **改软了不会有任何东西变红**。

把「必须人工复核」改成「建议人工复核」，把「不做模糊匹配」改成「尽量精确匹配」，
文档照样通过 `scripts/check_docs.py`（结构与引用都没坏），照样读得通顺，
而系统的行为承诺已经换了一个。这类改动通常出现在「文档读着太绝对，缓和一下」
的顺手编辑里，没有恶意，也没有痕迹。

## 断言的是短语，不是段落

只钉**关键短语的存在**，不钉整段原文 —— 后者会让任何一次措辞调整变红，
而一条动不动就红的断言，下一步就是被人加进跳过清单。
每条断言给一组同义写法，命中任一即可：管的是**这句话还在不在**，
不是**它长什么样**。

口径与 `maos/tests/test_docs_guard.py` 同源：那一份守文档的结构与引用真不真，
这一份守文档的承诺还在不在。两者不重叠。
"""

from __future__ import annotations

import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "ap-entry.md"


@pytest.fixture(scope="module")
def text() -> str:
    assert DOC.exists(), f"{DOC} 不在了 —— 供应链付款入口的说明文档被删了"
    return DOC.read_text(encoding="utf-8")


def _hit(text: str, *phrases: str) -> bool:
    """任一短语出现即算命中。"""
    return any(p in text for p in phrases)


def test_extraction_must_pass_human_review_before_payment(text):
    """抽取结果先落待复核表、人确认过才进付款 —— 这条是整篇的第一红线。"""
    assert _hit(text, "待复核表"), "「待复核表」这个中间产物不见了"
    assert _hit(text, "需人工确认"), "「需人工确认」那一列不见了 —— 复核关口没了标记位"
    assert _hit(text, "拒绝整张表", "拒绝"), "付款入口对未确认行的拒绝语气被删了"
    assert _hit(text, "理解可以错，授权不许错"), \
        "「理解可以错，授权不许错」这句被删了 —— 它是这条红线与铁律 8 的接缝"


def test_says_the_vision_model_is_experimental(text):
    """理由必须落在「实验版」上，不能只写「模型可能出错」这种空话。"""
    assert _hit(text, "实验版"), "视觉模型是实验版这个事实被抹掉了"
    assert _hit(text, "小数点"), "「看错一个小数点 = 多付一笔钱」这个具体后果被抽象掉了"


def test_no_fuzzy_matching(text):
    """认不出就报错，一个字都不猜。"""
    assert _hit(text, "模糊匹配"), "「不做模糊匹配」这条设计取向不见了"
    assert _hit(text, "不猜", "不许猜"), "「不猜」的口径被删了"
    assert _hit(text, "报错"), "「认不出就报错」被删了 —— 只说不猜不说报错等于没说怎么办"


def test_engine_untouched(text):
    """换域只换入口与数据，引擎（场景 10）一行没改。"""
    assert _hit(text, "maos/flows/scenario_10.py"), "没指出引擎是哪一条流程"
    assert _hit(text, "一行没改", "一行未改", "一行都没改"), \
        "「引擎一行没改」这句被删了 —— 它是铁律 9 在本轮的唯一可核断言"
    assert _hit(text, "状态机"), "「流程、状态机、契约一概不动」的范围表述被删了"


def test_smoke_exit_code_semantics_documented(text):
    """「还没装好」与「装好了但坏了」分开，这个约定必须写在文档里。

    只写在脚本注释里不够：读文档的人看到 exit 3 会以为是失败，
    然后把它「修」成 exit 0 或 exit 1，两种改法都会让整合期的红灯失去意义。
    """
    assert _hit(text, "exit 3"), "冒烟脚本的 exit 3 约定没写进文档"
    assert _hit(text, "还没装好"), "「还没装好 vs 装好了但坏了」的区分没写进文档"
