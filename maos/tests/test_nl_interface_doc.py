"""自然语言接入面两份文档的机器守卫 —— 钉住最容易被后人改软的那几句。

## 它守的缺口

`docs/nl-interface.md` 里真正要紧的不是描述，是**三句限制**：自然语言不能直接
授权、它只产出确认、显式指令仍是唯一生效入口。这三句是设计上刻意留的闸
（论证见该文 §3），而限制在文档里的寿命一向最短 —— 后来的人为了「读起来顺」
或者「反正现在解析很准了」，把「只产出确认」改成「可以直接执行」，
文档不会红、测试不会红，闸就这么在纸面上开了。

`docs/matrix-room-runbook.md` 那一条同理但更具体：Element 会把 `/approve …`
当自己的斜杠指令吃掉，必须点「作为消息发送」这个按钮，命令才真的进房间。
这个按钮名是**唯一可操作的信息**，去掉它剩下的话就只是「注意客户端行为」。

## 为什么断关键短语而不是整段

整段原文的断言等于禁止任何措辞调整 —— 那样它会在第一次润色时变红，
然后被人删掉。断言只钉**意思的承重点**：短、稳、改了它就是改了意思。

## 为什么还要查位置（`test_runbook_button_name_sits_at_the_command_step`）

按钮名塞在文末的修订记录里，与写在「在哪一步打命令」那一步旁边，
对台上正在发命令的人是两回事：前者要人先知道有这回事才找得到。
所以位置本身是判据的一部分。
"""

from __future__ import annotations

import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
NL_DOC = ROOT / "docs" / "nl-interface.md"
RUNBOOK = ROOT / "docs" / "matrix-room-runbook.md"

#: Element 那个按钮的**逐字名字**。改这个字符串之前先去 Element 里看一眼真名。
SEND_AS_MESSAGE_BUTTON = "作为消息发送"

#: 「在哪一步打命令」那一节的标题，位置判据以它为锚。
COMMAND_STEP_HEADING = "### 在哪一步打命令"


@pytest.fixture(scope="module")
def nl_doc() -> str:
    assert NL_DOC.exists(), f"{NL_DOC} 不存在"
    return NL_DOC.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def runbook() -> str:
    assert RUNBOOK.exists(), f"{RUNBOOK} 不存在"
    return RUNBOOK.read_text(encoding="utf-8")


def test_doc_says_natural_language_cannot_authorize(nl_doc: str) -> None:
    """红线 R1 的第一半：自然语言**不能直接授权**。"""
    assert "不能直接授权" in nl_doc, (
        "docs/nl-interface.md 必须明确写着自然语言不能直接授权 —— "
        "这是红线 R1，不是措辞偏好")


def test_doc_says_it_only_yields_a_confirmation(nl_doc: str) -> None:
    """红线 R1 的第二半：它**只产出确认**，不产出已生效的动作。"""
    assert "确认卡片" in nl_doc, "docs/nl-interface.md 必须说清楚它只产出确认卡片"
    assert "只产出" in nl_doc, (
        "「只产出」这三个字是承重的 —— 去掉「只」，这句话就从限制变成了描述")


def test_doc_says_explicit_command_is_the_only_live_entry(nl_doc: str) -> None:
    """显式指令仍是唯一生效入口 —— 这一层加上去之后，生效入口一个都没多。"""
    assert "显式指令" in nl_doc, "docs/nl-interface.md 必须提到显式指令"
    assert "唯一生效入口" in nl_doc, (
        "必须写明显式指令是唯一生效入口；只说「推荐用显式指令」不算")


def test_doc_ties_the_rule_to_law_8(nl_doc: str) -> None:
    """与铁律 8 同源：权威动作不能由推断产生。"""
    assert "铁律 8" in nl_doc
    assert "权威动作不能由推断产生" in nl_doc, (
        "这一句是把 R1 接回铁律 8 的那根线，答辩时靠它回答「为什么不是临时规定」")


def test_runbook_names_the_element_button(runbook: str) -> None:
    """Element 吃掉斜杠指令时，唯一可操作的信息就是这个按钮名。"""
    assert SEND_AS_MESSAGE_BUTTON in runbook, (
        f"docs/matrix-room-runbook.md 必须逐字写出「{SEND_AS_MESSAGE_BUTTON}」这个按钮名")


def test_runbook_button_name_sits_at_the_command_step(runbook: str) -> None:
    """按钮名必须落在「在哪一步打命令」那一节里，不许只躺在文末的修订记录。"""
    start = runbook.find(COMMAND_STEP_HEADING)
    assert start != -1, f"runbook 里找不到 {COMMAND_STEP_HEADING!r} 这一节"
    end = runbook.find("\n### ", start + len(COMMAND_STEP_HEADING))
    assert end != -1, "「在哪一步打命令」之后应当还有下一节"
    section = runbook[start:end]
    assert SEND_AS_MESSAGE_BUTTON in section, (
        f"「{SEND_AS_MESSAGE_BUTTON}」必须写在发命令那一步旁边 —— "
        "写在文末的人，台上撞见弹窗时不会翻到那里")
