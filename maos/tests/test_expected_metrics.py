"""期望数字的单一真源守卫 —— 让漂移在**加测试的那一轨**当场变红。

## 这条测试要治的病

录制前唯一的机器门禁 ``scripts/demo_preflight.sh`` 第 1 步断言全量测试条数。那个数是
并轨的下游产物：谁加了测试谁就该回来刷它。靠纪律刷了五次，**漏了五次** ——
``1069 -> 1370 -> 1476 -> 2042`` 这串数字每一次都是在别人身上炸的，而且炸的姿势是最坏
的那种：**门禁在该拦的时候拦错了人**。脚本自己的注释里写着「加测试的那一轨改这个数，
不要留给录制那天的人」，写完之后又被漏了一次（T97–T103 加了 150 条没回来改）。

结论不是「下次记得」，是**靠纪律这条路已经失败，换机器**。

## 机制两条

1. **单一真源**：``docs/expected-metrics.json``。``demo_preflight.sh`` 从它读，不再自己写死。
2. **会红的守卫**：就是本文件。它拿 ``pytest --collect-only`` 数出真实收集条数，与真源比对；
   再扫一遍那几份写死过这类数字的文件，确认没有第二处真源。

于是加测试的那一轨跑一次自己的测试就会红，红的那条报错里直接写着该改哪个文件的哪个键。

## 为什么是 ``--collect-only`` 而不是真跑一遍

真跑就是递归调自己，一次全量要一分多钟，还会把本轮的失败搅进来。收集不执行任何用例，
半秒返回，且**收集条数 = passed + skipped**（本仓库全部 skip 都是运行期 skipif，
不是模块级 importorskip，所以不影响收集）。这条等式本身也是判据的一部分：它一旦不成立，
说明有人加了模块级跳过，那也该有人看见。

## 为什么不写成「自动取值」

自动取值的守卫永远不会红，那就把门禁变成了装饰。**写死 + 会红**才是要的：
数字过期是**预期内**的事件，它该在合并的那一刻响，由人看一眼再刷一次真源。

## 标记语言（扫描面用）

那几份文件里同时躺着两类数字，机器分不出，所以要人显式声明，一行一个：

    metric:<真源里的键名>                              这一行写的是**现行值**，必须与真源一致
    metric:frozen <理由>                               这一行的数字是**史料**，不随合入刷新
    metric:frozen-begin <理由> / metric:frozen-end     成段的史料，区间内整段豁免

理由是必填的：一个不写理由就能把数字静音的标记，等于没有标记。
未加任何标记又长得像条数的行 —— 一律红，并**点名文件与行号**。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SOURCE_REL = "docs/expected-metrics.json"
SOURCE = os.path.join(ROOT, SOURCE_REL)

#: 真源必须正好有这些键，类型也要对。多一个少一个都红 —— 多一个键就是多一个口径。
REQUIRED_KEYS: dict[str, type] = {
    "pytest_passed_nopg": int,
    "pytest_skipped_nopg": int,
    "pg_gated_tests": int,
    "evidence_bundles": int,
    "verify_result_line": str,
}

#: 历史上写死过这类条数的文件。扫描面只覆盖这几份。
GUARDED_FILES = (
    "scripts/demo_preflight.sh",
    "docs/submission-checklist.md",
    "docs/clone-smoke-report.md",
    "docs/agentteams-mapping.md",
)

#: 必须存在的现行值锚点。缺一即红 —— 否则「删掉标记让守卫闭嘴」就是一条捷径。
REQUIRED_ANCHORS = (
    ("docs/submission-checklist.md", "pytest_passed_nopg"),
)

#: 长得像「全量条数」的东西。两位数起步：一位数的 1 failed 是句式不是读数。
COUNT_RE = re.compile(r"(?<![\d.])(\d{2,6})\s*(?:passed|failed|skipped|条测试|条用例)")

_FROZEN_END = "metric:frozen-end"
_FROZEN_BEGIN = "metric:frozen-begin"
_FROZEN = "metric:frozen"
_ANCHOR_RE = re.compile(r"metric:([a-z][a-z0-9_]*)")

FIX_HINT = (
    "加测试的那一轨要改 " + SOURCE_REL + " 这一个数，不是改四处。"
    "真跑的记进 pytest_passed_nopg，被门控 skip 的记进 pytest_skipped_nopg。"
)


def _load_source() -> dict:
    with open(SOURCE, encoding="utf-8") as fh:
        return json.load(fh)


def _reason_of(line: str, marker: str) -> str:
    """取标记后面那段理由。取不到就是空串 —— 调用方据此判红。"""
    tail = line.split(marker, 1)[1]
    # 收尾的 --> / */ 之类注释闭合符不算理由
    return re.sub(r"[-*/>\s　]+$", "", tail).strip(" 　:：")


def _scan(rel: str) -> dict:
    """扫一份文件，分出「现行值锚点」「未标记的裸数字」「史料区间是否闭合」。"""
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        lines = fh.read().splitlines()

    anchors: list[tuple[int, str, list[int]]] = []
    unmarked: list[tuple[int, str]] = []
    reasonless: list[tuple[int, str]] = []
    open_regions: list[int] = []

    for lineno, line in enumerate(lines, 1):
        # 区间标记优先判，且 frozen-end 必须先于 frozen-begin / frozen 判 —— 三者前缀相同。
        if _FROZEN_END in line:
            if not open_regions:
                reasonless.append((lineno, _FROZEN_END + " 没有配对的 " + _FROZEN_BEGIN))
            else:
                open_regions.pop()
            continue
        if _FROZEN_BEGIN in line:
            if not _reason_of(line, _FROZEN_BEGIN):
                reasonless.append((lineno, _FROZEN_BEGIN + " 没写理由"))
            open_regions.append(lineno)
            continue

        numbers = [int(n) for n in COUNT_RE.findall(line)]
        if not numbers:
            continue
        if open_regions:                      # 在史料区间里，整段豁免
            continue
        if _FROZEN in line:
            if not _reason_of(line, _FROZEN):
                reasonless.append((lineno, _FROZEN + " 没写理由"))
            continue

        keys = [k for k in _ANCHOR_RE.findall(line) if k != "frozen"]
        if keys:
            anchors.append((lineno, keys[0], numbers))
        else:
            unmarked.append((lineno, line.strip()))

    return {
        "anchors": anchors,
        "unmarked": unmarked,
        "reasonless": reasonless,
        "unclosed": open_regions,
    }


# ---------------------------------------------------------------------------
# 1. 真源本身站得住
# ---------------------------------------------------------------------------
def test_source_of_truth_is_well_formed():
    data = _load_source()
    keys = {k for k in data if not k.startswith("_")}
    assert keys == set(REQUIRED_KEYS), (
        SOURCE_REL + " 的键集不对：多了 " + repr(sorted(keys - set(REQUIRED_KEYS)))
        + "，少了 " + repr(sorted(set(REQUIRED_KEYS) - keys))
        + "。多一个键就是多一个口径，正是本文件要防的事。"
    )
    for key, want_type in REQUIRED_KEYS.items():
        assert isinstance(data[key], want_type), (
            SOURCE_REL + " 的 " + key + " 应是 " + want_type.__name__
            + "，实际是 " + type(data[key]).__name__
        )
    for key in ("pytest_passed_nopg", "pytest_skipped_nopg",
                "pg_gated_tests", "evidence_bundles"):
        assert data[key] > 0, SOURCE_REL + " 的 " + key + " 必须为正，实际 " + repr(data[key])


def test_pg_gated_invariant_is_the_documented_90():
    """有库档 = 无库档 + 90 这条算式里的那个 90。

    它不是读数是**不变量**：22 条 test_pg_store_live.py + 7 条 test_pg_rank_parity.py
    + 19 条 test_refund_domain_pg.py + 16 条 test_kb_pg_prefilter.py（后两份 T115 加；
    2026-09-10 整合期由 29 刷成 64）+ 3 条 test_flow_domain_backend.py（T126 加，
    2026-09-11 整合期 p10-d 刷成 67）+ 7 条 test_kb_flow_backend.py + 4 条
    test_schema_util_t133.py（T132 / T133 加，2026-09-12 整合期 p10-e 刷成 78）
    + 9 条 test_pg_vector_channel_t139.py + test_kb_flow_backend.py 的第 8 条
    （T139 加，2026-09-12 整合期 p10-f 刷成 88）+ test_pg_vector_channel_t139.py 的
    test_second_tier_chinese_recalls_through_the_assembly_path 与 test_pg_store_live.py 的
    test_shadow_table_text_is_matchable_char_by_char 各 1 条
    （T142 加，2026-09-13 整合期 p10-g 刷成 90）。
    **T139 那条中文分词本波从 test_pg_store_live.py 搬进了 test_pg_vector_channel_t139.py**
    （改名 test_chinese_query_recalls_on_real_tokenizer）：它仍是双重门控，有库那档照样
    skip，搬家不改变差值 —— 两个文件的收集数一增一减，而 skipped 只涨 2。
    **T139 的 test_pg_store_live.py:206（中文分词）不进这个数**：它是双重门控
    （有 PG + 装了 zhparser），本机 pgvector 没装 zhparser，有库那档照样 skip，
    于是它不出现在两档的差值里。按 skip 理由数是 89 条，按差值是 88 条，以差值为准。
    主仓实测（p10-f 整合期，本机 docker PG）：无库 3950 passed / 101 skipped、
    有库 4038 passed / 13 skipped；非 PG 门控 12 条（RocketMQ 9 + Nacos 2 + 加密库 1）不变。
    真要变（有人给那几个文件加/删了用例），改真源的同时得回来改这条断言 ——
    多这一道手是故意的，这个数被改的时候必须有人知道。

    **别拿 `pytest -k pg` 去数它**：那样只数得到 77，与真值差 **12 条** —— T126 那 3 条里
    有一条、`test_kb_flow_backend.py` 8 条里有 7 条、`test_schema_util_t133.py` 整 4 条，
    名字里都不含 `pg`、`-k` 一条都选不中，而它们照样是「没库 skip、有库真跑」。
    （p10-d 时只差 1 条，p10-e 起就是 12 条。）门控条数只认全量两档的差值。
    """
    data = _load_source()
    assert data["pg_gated_tests"] == 90, (
        "PG 门控条数变了。它是 test_pg_store_live.py(22，第 23 条要 zhparser 不算) + test_pg_rank_parity.py(7)"
        " + test_refund_domain_pg.py(19) + test_kb_pg_prefilter.py(16)"
        " + test_flow_domain_backend.py(3) + test_kb_flow_backend.py(8)"
        " + test_schema_util_t133.py(4) + test_pg_vector_channel_t139.py(9)"
        " + T142 的 second_tier(1) + shadow_table(1)，"
        "改它等于动 demo_preflight.sh 里「有库档 = 无库档 + 90」那条算式的地基，"
        "确认过再连这条断言一起改。"
    )


# ---------------------------------------------------------------------------
# 2. 核心：实际收集条数必须与真源对得上
# ---------------------------------------------------------------------------
def test_collected_count_matches_source_of_truth():
    data = _load_source()
    expected = data["pytest_passed_nopg"] + data["pytest_skipped_nopg"]

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "maos/tests",
         "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True, timeout=600,
    )
    assert proc.returncode == 0, (
        "收集阶段就非 0 退出，条数无从谈起。末几行：\n"
        + "\n".join((proc.stdout + proc.stderr).splitlines()[-10:])
    )

    found = re.findall(r"(\d+) tests? collected", proc.stdout)
    assert found, (
        "解析不出收集条数（输出里没有 N tests collected）。末几行：\n"
        + "\n".join(proc.stdout.splitlines()[-5:])
    )
    collected = int(found[-1])

    assert collected == expected, (
        "\n实际收集 %d 条，%s 期望 %d 条（pytest_passed_nopg=%d + pytest_skipped_nopg=%d），差 %+d 条。\n%s\n"
        % (collected, SOURCE_REL, expected, data["pytest_passed_nopg"],
           data["pytest_skipped_nopg"], collected - expected, FIX_HINT)
        + "刷完真源，scripts/demo_preflight.sh 与 docs/submission-checklist.md 会自动跟上，"
        "不需要（也不许）在别处再写一遍这个数。"
    )


# ---------------------------------------------------------------------------
# 3. 真源之外不许有第二处写死
# ---------------------------------------------------------------------------
def test_demo_preflight_reads_the_source_of_truth():
    rel = "scripts/demo_preflight.sh"
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        lines = fh.read().splitlines()

    assert any(SOURCE_REL in line for line in lines), (
        rel + " 没有引用 " + SOURCE_REL + "：它又自己写死了一份期望值。"
    )

    literal = [
        (i, ln) for i, ln in enumerate(lines, 1)
        if re.match(r"\s*(EXPECT_TESTS_NOPG|EXPECT_BUNDLES|PG_GATED_TESTS)\s*=\s*[\"']?\d", ln)
    ]
    assert not literal, (
        rel + " 又把期望值写死了：\n"
        + "\n".join("  " + rel + ":" + str(i) + ": " + ln.strip() for i, ln in literal)
        + "\n这些值必须从 " + SOURCE_REL + " 读。" + FIX_HINT
    )

    formula = "EXPECT_TESTS_PG=$((EXPECT_TESTS_NOPG + PG_GATED_TESTS))"
    assert any(formula in line for line in lines), (
        rel + " 里「有库档 = 无库档 + PG 门控」那条算式不见了。"
        "有库那档一旦写死，配了 MAOS_PG_DSN 的机器上第 1 步就会误红 —— "
        "原委见该文件注释，不要改成写死。"
    )


def test_guarded_files_carry_no_unmarked_counts():
    reports: list[str] = []
    for rel in GUARDED_FILES:
        scan = _scan(rel)
        for lineno, text in scan["unmarked"]:
            reports.append(
                "  " + rel + ":" + str(lineno) + ": " + text[:100] + "\n"
                "      ↑ 没标记的裸条数。是现行值就加 metric:<键名> 并与真源一致；"
                "是某次实测的史料就加 metric:frozen <理由>。"
            )
        for lineno, why in scan["reasonless"]:
            reports.append("  " + rel + ":" + str(lineno) + ": " + why)
        for lineno in scan["unclosed"]:
            reports.append(
                "  " + rel + ":" + str(lineno) + ": " + _FROZEN_BEGIN + " 开了没关，"
                "整段之后的数字都被静音了 —— 补一行 " + _FROZEN_END + "。"
            )

    assert not reports, (
        "\n真源之外出现了没有标记的条数（真源是 " + SOURCE_REL + "）：\n"
        + "\n".join(reports) + "\n" + FIX_HINT
    )


def test_anchor_lines_agree_with_source_of_truth():
    data = _load_source()
    seen: set[tuple[str, str]] = set()
    reports: list[str] = []

    for rel in GUARDED_FILES:
        for lineno, key, numbers in _scan(rel)["anchors"]:
            seen.add((rel, key))
            if key not in data:
                reports.append(
                    "  " + rel + ":" + str(lineno) + ": 标记的键 " + key
                    + " 不在 " + SOURCE_REL + " 里。可用的键："
                    + repr(sorted(k for k in data if not k.startswith("_")))
                )
                continue
            if data[key] not in numbers:
                reports.append(
                    "  " + rel + ":" + str(lineno) + ": 写的是 " + repr(numbers)
                    + "，" + SOURCE_REL + " 的 " + key + " 是 " + repr(data[key])
                    + " —— 这一行过期了。"
                )

    for rel, key in REQUIRED_ANCHORS:
        if (rel, key) not in seen:
            reports.append(
                "  " + rel + ": 少了 metric:" + key + " 锚点。它被删掉的话，"
                "这份文档上的条数就再没有任何东西盯着 —— 那正是本守卫存在的理由。"
            )

    assert not reports, (
        "\n现行值锚点与真源对不上（真源是 " + SOURCE_REL + "）：\n"
        + "\n".join(reports) + "\n" + FIX_HINT
    )
