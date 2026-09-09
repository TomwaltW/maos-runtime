"""`docs/sop-rtv.md` 的机器守卫 —— 文档说的和契约定的必须是同一件事。

文档会过期，而过期那天没有任何东西变红。这份测试是唯一拦得住的东西，它钉六件事：

1. 七节标题在，且顺序不乱；
2. 文档里出现的每一个 skill / 工具 / 角色 / 表 / 状态名，都能在**事实源**里查到 ——
   RTV 侧查冻结契约 `review/rtv-contracts.md`，`ap` 侧查 `maos/domain/ap/**` 与
   `maos/tools/ap.py` 的真实代码，两处都查不到的必须登记在 `ALLOW_UNSOURCED` 里并写理由；
3. §4 那张状态流与契约 C-R2 **逐字节相同**，状态数与边数按契约实测（不按人记的数）；
4. §1 的四个出处 URL 一个不少（只做字符串断言，**不发网络请求** —— 本仓库守不住
   外部站点的可达性，装作守得住是吹牛）；
5. `docs/domain-portability.md` 的前 393 行逐字节未变（T65 只许在尾部追加）；
6. 这两份文档在 `scripts/check_docs.py` 下零阻断类告警。

## 为什么第 2 条要留一份带理由的豁免表

供应商回执的取值域（`submitted` / `disputed` / `unknown`）定在 T62 的模拟器里，
本轨成稿时那份代码还不存在，契约也没有把它们逐个列出来。这三个名字于是既不在契约、
也不在任何已存在的代码里 —— 白名单是**如实标注它们暂时无源**，不是把红的塞进来。
整合期 T62 合入后，这三条应当从白名单删掉，改由真实模块接住（见 `ALLOW_UNSOURCED`
每条的理由）。`test_allow_unsourced_has_no_stale_entry` 钉住这张表不许留死条目。

## 契约不在 checkout 里时跳过，而不是判红

`review/**` 走 `.git/info/exclude`（编排面，按设计进不了版本库），在别的 checkout
里可能整个不存在。契约缺席时对契约的断言只能跳过 —— 但对 `ap` 侧事实源、状态数、
URL、393 行、文档守卫这五类断言照常执行。口径同 `scripts/check_docs.py` 的
「被 git 忽略的路径不在射程内」。
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SOP = os.path.join(ROOT, "docs", "sop-rtv.md")
PORTABILITY = os.path.join(ROOT, "docs", "domain-portability.md")
CONTRACT = os.path.join(ROOT, "review", "rtv-contracts.md")

#: T65 成稿时 `docs/domain-portability.md` 的前 393 行（基线 4c956a8 的全文）。
#: 这一节的每个数字都与各自的历史端点绑定，改一个就得把整节重跑一遍 ——
#: 所以本轨只许在尾部追加。指纹钉死，不依赖 git 可用。
PORTABILITY_HEAD_LINES = 393
PORTABILITY_HEAD_SHA256 = (
    "236c76fea776228adddd31c37350f44392775cc95ef07c42617d0bb2ea9aa489"
)

#: §1 的四个出处。只断言字符串在场，不发网络请求。
SOURCE_URLS = (
    "https://docs.oracle.com/cd/G35227_01/fscm92pbr54/eng/fscm/spog/"
    "UnderstandingtheRTVBusinessProcess-9f3e8e.html",
    "https://learn.microsoft.com/en-us/dynamics365/field-service/process-return",
    "https://docs.oracle.com/cd/E27605_01/fscm91pbr2/eng/psbooks/spog/htm/spog47.htm",
    "https://sap-ds.com/training/sap-business-one/logistics/purchasing/"
    "goods-returns-and-credit-memo",
)

#: 七节标题（§5.1 派单口径）。按顺序钉，顺序乱了也算失守。
REQUIRED_SECTIONS = (
    "## 1. 这份 SOP 是什么、出处在哪",
    "## 2. 五步主干",
    "## 3. 三种处置：credit / exchange / replacement",
    "## 4. 状态流：这是业务对象自己的字段，不是 Task 状态",
    "## 5. 权威边界：两个外部权威源、两个权威终态",
    "## 6. 与 `ap` 域的对照：镜像关系与五张表复用",
    "## 7. 不吹的部分（本域**没有**做什么）",
)

#: `ap` 侧事实源 —— 文档拿 ap 域做对照，那些名字的出处是**代码**，不是 RTV 契约。
AP_FACT_FILES = (
    "maos/domain/ap/schema.sql",
    "maos/domain/ap/guard.py",
    "maos/tools/ap.py",
    "maos/skills/builtin/ap/observe.py",
    "maos/skills/builtin/ap/__init__.py",      # 六个 ap skill 的模块名在这里
)

#: 两处事实源都查不到的名字。**每条必须写理由**，写不出理由的不是豁免、是没修。
ALLOW_UNSOURCED: dict[str, str] = {
    "submitted": "供应商回执取值域，定在 T62 的模拟器里；本轨基线无该模块，"
                 "整合期合入后删掉本条，改由 rtv 工具模块接住",
    "disputed": "同上（供应商明确不认这笔退货的取值）",
    "unknown": "同上（供应商侧自己也说不清，§5.3 里最危险的那一档）",
    "failed": "§4 的**反例**：说明为什么四条失败边不许收敛成一个状态。"
              "它不是本域状态，出现在契约里才是错的",
    "test_rtv_sop_doc.py": "本文件自身，文档 §7 引它说明 URL 只做字符串断言",
}

#: 短标识符的形状：小写起头、下划线/点分隔。文件名与句子不会误命中。
_SHORT = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$")
_FENCE = re.compile(r"^```.*?^```", re.S | re.M)
_CODESPAN = re.compile(r"`([^`\n]+)`")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _contract_or_skip() -> str:
    if not os.path.exists(CONTRACT):
        pytest.skip("review/rtv-contracts.md 走 .git/info/exclude，"
                    "本 checkout 里不存在 —— 契约类断言跳过，其余照常")
    return _read(CONTRACT)


def _biz_status_flow(contract: str) -> dict[str, tuple[str, ...]]:
    """从契约 C-R2 的代码块里解析 BIZ_STATUS_FLOW —— 数字由契约算，不由人记。"""
    block = re.search(r"^BIZ_STATUS_FLOW.*?^\}", contract, re.S | re.M)
    assert block, "契约里找不到 BIZ_STATUS_FLOW —— 契约结构变了，停手报告人类"
    flow: dict[str, tuple[str, ...]] = {}
    for line in block.group(0).splitlines()[1:-1]:
        m = re.match(r'\s*"([a-z_]+)":\s*\((.*)\),', line)
        if not m:
            continue
        targets = tuple(re.findall(r'"([a-z_]+)"', m.group(2)))
        flow[m.group(1)] = targets
    return flow


# ---------------------------------------------------------------- 1 七节标题
def test_sop_doc_exists_with_seven_sections() -> None:
    assert os.path.exists(SOP), "docs/sop-rtv.md 不存在"
    doc = _read(SOP)
    at = -1
    for title in REQUIRED_SECTIONS:
        idx = doc.find(title)
        assert idx >= 0, f"缺这一节：{title}"
        assert idx > at, f"节序乱了：{title} 出现在前一节之前"
        at = idx


# ------------------------------------------- 2 每个名字都要在事实源里查得到
def test_every_name_in_doc_is_sourced() -> None:
    doc = _read(SOP)
    contract = _contract_or_skip()
    facts = [contract]
    for rel in AP_FACT_FILES:
        path = os.path.join(ROOT, rel)
        assert os.path.exists(path), f"ap 侧事实源不见了：{rel}"
        facts.append(_read(path))

    prose = _FENCE.sub("", doc)          # 围栏块是抄来的原文，不重复判
    names = {t for t in _CODESPAN.findall(prose) if _SHORT.match(t)}
    unsourced = sorted(
        n for n in names
        if n not in ALLOW_UNSOURCED
        and not any(n in src for src in facts)
        and not os.path.exists(os.path.join(ROOT, n))
    )
    assert not unsourced, (
        "文档里这些名字在契约与 ap 域代码里都查不到 —— 要么写错了，"
        f"要么该登记进 ALLOW_UNSOURCED 并写理由：{unsourced}"
    )


def test_allow_unsourced_has_no_stale_entry() -> None:
    """豁免表不许留死条目 —— 名字已经进了事实源就该把豁免删掉。"""
    doc = _read(SOP)
    prose = _FENCE.sub("", doc)
    names = {t for t in _CODESPAN.findall(prose) if _SHORT.match(t)}
    stale = sorted(n for n in ALLOW_UNSOURCED if n not in names)
    assert not stale, f"ALLOW_UNSOURCED 里这些条目文档已经不提了，删掉：{stale}"
    for name, reason in ALLOW_UNSOURCED.items():
        assert reason.strip(), f"{name} 的豁免没写理由"


# ------------------------------------------------- 3 状态流与契约逐字节相同
def test_status_flow_matches_contract() -> None:
    doc = _read(SOP)
    contract = _contract_or_skip()
    flow = _biz_status_flow(contract)

    assert len(flow) == 7, f"C-R2 的状态数变了：{sorted(flow)}"
    edges = sum(len(v) for v in flow.values())
    assert edges == 9, f"C-R2 的边数变了：{edges} 条"

    doc_block = re.search(r"^BIZ_STATUS_FLOW.*?^\}", doc, re.S | re.M)
    assert doc_block, "文档 §4 里找不到 BIZ_STATUS_FLOW 代码块"
    contract_block = re.search(r"^BIZ_STATUS_FLOW.*?^\}", contract, re.S | re.M)
    assert doc_block.group(0) == contract_block.group(0), (
        "文档 §4 的状态机与契约 C-R2 不再逐字节相同 —— 文档在说另一件事"
    )

    # 文档正文里的状态名不许多也不许少（七个全提到，且没有编出第八个）
    for state in flow:
        assert f"`{state}`" in doc, f"文档没提状态 {state}"


def test_authoritative_states_are_two() -> None:
    """两个权威终态是本域相对前四个域的增量 —— 少一个，论证就塌回 ap 域。"""
    contract = _contract_or_skip()
    m = re.search(r"AUTHORITATIVE_STATES = frozenset\(\{([^}]*)\}\)", contract)
    assert m, "契约 C-R3 里找不到 AUTHORITATIVE_STATES"
    states = set(re.findall(r'"([a-z_]+)"', m.group(1)))
    assert states == {"credited", "settled"}, states
    for state in states:
        assert f"`{state}`" in _read(SOP), f"文档没提权威终态 {state}"
    # acknowledged 绝不许成为 credited 的判据（C-R3 红字）
    receipt = re.search(
        r'"credited":\s*frozenset\(\{([^}]*)\}\)', contract)
    assert receipt and "acknowledged" not in receipt.group(1), (
        "acknowledged 进了 credited 的判据集合 —— 本域要证明的事当场作废"
    )


# ------------------------------------------------------------ 4 四个出处 URL
def test_four_source_urls_present() -> None:
    doc = _read(SOP)
    for url in SOURCE_URLS:
        assert url in doc, f"§1 少了出处 URL：{url}"


# ------------------------------------------- 5 domain-portability 前 393 行
def test_portability_head_unchanged() -> None:
    with open(PORTABILITY, "rb") as fh:
        raw = fh.read()
    head = b"".join(raw.splitlines(keepends=True)[:PORTABILITY_HEAD_LINES])
    got = hashlib.sha256(head).hexdigest()
    assert got == PORTABILITY_HEAD_SHA256, (
        f"docs/domain-portability.md 前 {PORTABILITY_HEAD_LINES} 行被改了。"
        "那些数字与各自的历史端点绑定，改一个就得把整节重跑一遍 —— "
        "T65 只许在尾部追加"
    )
    assert len(raw.splitlines()) > PORTABILITY_HEAD_LINES, "RTV 那一节没了"
    assert "## RTV 域（第 5 个域）—— 两个权威源的增量" in raw.decode("utf-8")


# ------------------------------------------------------- 6 文档守卫零阻断类
def test_two_docs_pass_check_docs() -> None:
    scripts = os.path.join(ROOT, "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import check_docs                                     # noqa: PLC0415

    hard = check_docs.blocking(check_docs.run([SOP, PORTABILITY]))
    assert not hard, f"本轨两份文档有阻断类告警：{hard}"


def test_check_docs_cli_still_exits_zero() -> None:
    """整仓口径：基线上 exit=0，本轨不许把它推成非零。"""
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "scripts", "check_docs.py")],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout[-2000:]
