"""ISO 20022 码表的测试 —— 码值来自**文件**，判据来自代码，两者必须对得上。

本文件守的第一件事是派单 §5.5 那条：**码表是从文件读的，不是硬编在逻辑里**。
`test_codes_come_from_the_data_file` 直接把数据文件挪走，然后要求加载器抛
`FileNotFoundError` —— 如果码值其实硬编在 .py 里，那条用例会绿，而它绿就说明
「我们对齐了官方规范」这句话失去了唯一的物证。

第二件事是**出处**：每个码集都要能报出它来自哪个 URL、哪一版、SHA-256 是多少。
核不到出处的码不许进表（口径同 `maos/tools/gateway_codes.py` 的文件头）。

标了 `# 论证：` 的断言是复赛材料里那几句话的机器化版本，评审时可按前缀捞出来对。
"""

from __future__ import annotations

import importlib
import json

import pytest

from maos.tools import investigation_codes as IC


# --------------------------------------------------------------- 数据来自文件
def test_codes_come_from_the_data_file(tmp_path, monkeypatch):
    """把数据文件指到一个不存在的路径，加载器必须抛 —— 不许回落到硬编码。

    # 论证：码表是从文件读的，不是硬编在逻辑里。
    这条用例是那句话唯一的机器判据：它绿说明码值真的只有文件一个来源。
    """
    fresh = importlib.reload(IC)
    monkeypatch.setattr(fresh, "CODES_PATH", tmp_path / "nope.json")
    fresh._raw.cache_clear()
    fresh._tables.cache_clear()
    with pytest.raises(FileNotFoundError) as exc:
        fresh.lookup(fresh.SET_RESOLUTION, "CNCL")
    assert "不许 fallback 到硬编码" in str(exc.value)
    # 复原，别影响同进程里后面的用例。
    fresh._raw.cache_clear()
    fresh._tables.cache_clear()
    importlib.reload(IC)


def test_data_file_carries_provenance():
    """出处四件套齐全：URL、文件名、SHA-256、抓取日期。"""
    p = IC.provenance()
    for key in ("source_url", "source_file", "source_sha256", "fetched_at", "release"):
        assert p.get(key), f"码表出处缺 {key} —— 核不到出处的码表不许用"
    assert p["source_url"].startswith("https://www.iso20022.org/"), (
        f"码表必须来自 ISO 20022 官方站点，实际 {p['source_url']}")
    assert len(p["source_sha256"]) == 64, "SHA-256 应是 64 位十六进制"


def test_four_code_sets_present_with_expected_sizes():
    """四个码集都在，且条数与官方发布一致（抄漏了会在这里响）。"""
    expected = {
        IC.SET_CANCELLATION_REASON: 28,
        IC.SET_RESOLUTION: 31,
        IC.SET_CANCELLATION_REJECTION: 25,
        IC.SET_RETURN_REASON: 100,
    }
    for name, count in expected.items():
        assert len(IC.codes_in(name)) == count, (
            f"{name} 应有 {count} 条，实际 {len(IC.codes_in(name))} —— "
            "条数变了说明重抓了一版规范，判据要跟着重核一遍")


def test_code_set_to_message_mapping_is_from_the_spec():
    """码集与报文的对应取自官方 `UsageInMsgs`，不是我们的断言。

    # 论证：本域「只有 pacs.004 证明得了资金已退回」这条判据，前提是
    # 退回原因码只用在 pacs.004 上、决议码只用在 camt.029 上 —— 两条都由规范文件自己说。
    """
    assert IC.is_used_in(IC.SET_RESOLUTION, "camt.029")
    assert IC.is_used_in(IC.SET_CANCELLATION_REJECTION, "camt.029")
    assert IC.is_used_in(IC.SET_CANCELLATION_REASON, "camt.056")
    assert IC.is_used_in(IC.SET_RETURN_REASON, "pacs.004")
    # 反面：决议码不该出现在 camt.056（那是请求，不是答复）。
    assert not IC.is_used_in(IC.SET_RESOLUTION, "camt.056")


# ------------------------------------------------------- 两个正交维度的判据
def test_cncl_is_a_positive_answer():
    """CNCL 是**肯定**答复 —— 这条一旦不成立，本域整个论证都要重写。"""
    entry = IC.lookup(IC.SET_RESOLUTION, "CNCL")
    assert entry.name == "CancelledAsPerRequest"
    assert "successful" in entry.definition.lower(), (
        f"CNCL 的官方定义应说撤销成功，实际 {entry.definition!r} —— "
        "规范改版了就要重新核对本域的全部判据")
    assert IC.resolution_of("CNCL") == IC.RESOLUTION_CONFIRMED
    assert IC.is_terminal_resolution("CNCL") is True


def test_camt029_is_never_funds_evidence():
    """**本域招牌判据**：camt.029 无论带哪个结论码，都不是资金证据。

    # 论证：清算方说「撤销成功」不等于钱回来了。
    """
    assert IC.is_funds_evidence("camt.029.001.08") is False
    assert IC.is_funds_evidence("camt.029.001.11") is False, "换一版报文也不该判穿"
    assert IC.is_funds_evidence("camt.056.001.08") is False
    # 只有它是 True。
    assert IC.is_funds_evidence("pacs.004.001.09") is True
    assert IC.is_funds_evidence("pacs.004.001.11") is True


def test_unknown_message_family_is_fail_closed():
    """认不出来的报文一律不算资金证据 —— 默认 True 会让任何没见过的报文都能收口。"""
    assert IC.is_funds_evidence("mt103") is False
    assert IC.is_funds_evidence("") is False
    assert IC.is_funds_evidence("pacs.008.001.08") is False


def test_message_family_normalises_version():
    """按族比而不是按全名 —— 报文版本号每年都在涨。"""
    assert IC.message_family("camt.029.001.08") == "camt.029"
    assert IC.message_family("pacs.004.001.11") == "pacs.004"
    assert IC.message_family("camt.029") == "camt.029"


@pytest.mark.parametrize("code,expected", [
    ("CNCL", IC.RESOLUTION_CONFIRMED),
    ("RJCR", IC.RESOLUTION_REJECTED),
    ("PDCR", IC.RESOLUTION_PENDING),
    ("PECR", IC.RESOLUTION_PARTIAL),
    ("PDNG", IC.RESOLUTION_PENDING),
    ("CWFW", IC.RESOLUTION_PENDING),      # will be cancelled —— 还没撤
    ("FTNA", IC.RESOLUTION_PENDING),      # 转给下一家，本家没结论
    ("ACNR", IC.RESOLUTION_OTHER),        # 答的不是撤销请求
])
def test_resolution_classification(code, expected):
    """每一档都按官方 definition 定，不按语感。"""
    assert IC.resolution_of(code) == expected


def test_pending_is_not_terminal():
    """未决不是终态 —— 判成终态会让轮询提前收工，永远等不到 pacs.004。"""
    for code in ("PDCR", "PDNG", "CWFW", "FTNA"):
        assert IC.is_terminal_resolution(code) is False, f"{code} 不该是终态"


# ------------------------------------------------------------- 未知码不兜底
def test_unknown_code_raises_not_defaults():
    """未知码抛，不返回一个「默认」对象。

    兜底的后果不是报错，是把没核过出处的码当成已知码处理 —— 那正是这张表要防的事。
    """
    for setname in IC.ALL_CODE_SETS:
        with pytest.raises(IC.UnknownCodeError):
            IC.lookup(setname, "ZZZZ")
    with pytest.raises(IC.UnknownCodeError):
        IC.resolution_of("ZZZZ")
    with pytest.raises(IC.UnknownCodeError):
        IC.rejection_reason("ZZZZ")
    with pytest.raises(IC.UnknownCodeError):
        IC.return_reason("ZZZZ")
    with pytest.raises(IC.UnknownCodeError):
        IC.cancellation_reason("ZZZZ")


def test_unknown_code_set_raises():
    with pytest.raises(IC.UnknownCodeError):
        IC.lookup("ExternalMadeUpCode", "CNCL")
    with pytest.raises(IC.UnknownCodeError):
        IC.codes_in("ExternalMadeUpCode")


# ---------------------------------------------------- 抽三条自己核（派单 §6.5）
@pytest.mark.parametrize("code_set,code,name,fragment", [
    (IC.SET_CANCELLATION_REJECTION, "AM04", "InsufficientFunds", "insufficient"),
    (IC.SET_CANCELLATION_REJECTION, "LEGL", "LegalDecision", "regulatory rules"),
    (IC.SET_RESOLUTION, "RJCR", "RejectedCancellationRequest", "rejected"),
    (IC.SET_RETURN_REASON, "AC04", "ClosedAccountNumber", "closed"),
    (IC.SET_CANCELLATION_REASON, "DUPL", "DuplicatePayment", "duplicate"),
])
def test_spot_check_against_official_definitions(code_set, code, name, fragment):
    """逐条对官方 Code Name 与 definition 原文。

    这几条就是派单 §6 第 5 条「抽三条自己核」的机器化版本：定义原文改了会在这里响，
    而不是等到评委拿 URL 去对的时候才发现。
    """
    entry = IC.lookup(code_set, code)
    assert entry.name == name
    assert fragment.lower() in entry.definition.lower(), (
        f"{code} 的官方定义应含 {fragment!r}，实际 {entry.definition!r}")
    assert entry.source == IC.provenance()["source_url"]


def test_every_classified_code_still_exists_in_the_spec():
    """判据表里的每个码都还在官方码集里 —— import 时的 `_validate()` 的显式版本。"""
    official = set(IC.codes_in(IC.SET_RESOLUTION))
    missing = sorted(c for c in IC._RESOLUTION_BY_CODE if c not in official)
    assert not missing, (
        f"判据表引用了官方码集里没有的码：{missing} —— 规范可能改版了")


def test_data_file_is_valid_json_and_definitions_are_verbatim():
    """数据文件本身可解析，且每条都带 code/name/definition。"""
    with IC.CODES_PATH.open(encoding="utf-8") as fh:
        raw = json.load(fh)
    for setname, block in raw["code_sets"].items():
        assert block["codes"], f"{setname} 是空的"
        for entry in block["codes"]:
            assert entry["code"], f"{setname} 有一条没有码值"
            assert entry["name"], f"{setname}/{entry['code']} 没有官方 Code Name"
            assert entry["definition"], f"{setname}/{entry['code']} 没有官方定义原文"
