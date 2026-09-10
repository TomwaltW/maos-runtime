#!/usr/bin/env python3
"""诉求类型分类评测 —— 拿 `reason-eval.csv` 逐条问 `classify_reason`，算准确率。

## 这个脚本量的是什么

不是「模型行不行」，是**这条链路当前认得出多少种说法**。四个分组各有各的判据
（`lexicon_corpus.py` 的模块头写了为什么要分组）：

| 分组 | 期望 | 判错意味着 |
| :-- | :-- | :-- |
| `lexicon` | 必中，且 `source` 必须是 `lexicon` | 词表被改坏了，或者走到模型去了（多花钱且不稳） |
| `real` | 判成期望 code | 没配 key 时全落 `unknown` —— 这是**预期退化**，单独计数不算错 |
| `unknown` | 判成 `unknown` | 最坏的一类错：判出个 code 就会套政策，一路自动批款 |
| `ambiguous` | 判成政策上优先的那个 | 见语料里每条的理由 |

## 没配 key 时跑它有没有意义

有，而且是主要用法：`lexicon` 与 `unknown` 两组**完全不依赖模型**，它们量的是
词表的下界与安全出口是否还在。`real` 那一组会整组落进「未判（无模型）」一栏，
不计入错误 —— 把设计好的退化算成 0 分，下次就没人敢看这张表了。

    python3 scenarios/bulk/eval_reason.py                    # 无 key，词表下界
    . ~/.maos.env && python3 scenarios/bulk/eval_reason.py   # 有 key，量模型兜底
    python3 scenarios/bulk/eval_reason.py --miss             # 只列判错和未判的
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))

from maos.ingress.classify import classify_reason, make_classifier  # noqa: E402

UNKNOWN = "unknown"
GROUPS = ("lexicon", "real", "unknown", "ambiguous")


def _w(text: str) -> int:
    return sum(2 if ord(c) > 0x2E80 else 1 for c in str(text))


def _pad(text: str, width: int) -> str:
    return str(text) + " " * max(0, width - _w(text))


def load_rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def judge(row: dict, verdict: dict, *, has_model: bool) -> str:
    """一行的结论：`hit` / `miss` / `abstain` / `no_model`。

    ## 弃权与未判必须分开，否则这张表会骗人

    两者的 `reason` 都是 `unknown`、`source` 都可能是 fallback，但含义相反：

    * `no_model`（**没配 key**）—— 机器压根没判。剔出分母，否则一台没配 key 的
      机器会把设计好的退化算成 0 分。
    * `abstain`（**配了 key，模型自己说不知道**）—— 判了，弃权了。**计入分母**：
      这一单会转人工，是实打实的成本。剔出去的话准确率会虚高到 100%，而真实
      情况是每七条里有一条要人接手。

    `unknown` 组落 unknown 始终是 `hit`：那一组要的就是弃权。
    """
    want, got = row["期望诉求类型"], verdict["reason"]
    if row["分组"] == "lexicon":
        # 词表组还要看 source：判对了但走了模型，说明词表里这个词没了。
        return "hit" if got == want and verdict["source"] == "lexicon" else "miss"
    if got == want:
        return "hit"
    if got == UNKNOWN:
        return "abstain" if has_model else "no_model"
    return "miss"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--csv", default=str(HERE / "reason-eval.csv"))
    parser.add_argument("--miss", action="store_true", help="只列判错与未判的行")
    args = parser.parse_args()

    rows = load_rows(Path(args.csv))
    classifier = make_classifier()
    print(f"评测集 {len(rows)} 条；模型兜底："
          f"{'已接线' if classifier else '未接线（无 key，real 组整组落未判）'}\n")

    stat: dict[str, Counter] = {g: Counter() for g in GROUPS}
    misses: list[tuple[str, str, str, str]] = []
    for row in rows:
        verdict = classify_reason(row["说法"], classifier)
        outcome = judge(row, verdict, has_model=classifier is not None)
        stat[row["分组"]][outcome] += 1
        if outcome != "hit":
            misses.append((row["分组"], row["说法"], row["期望诉求类型"],
                           f"{verdict['reason']}（{verdict['source']}"
                           f"{'，弃权' if outcome == 'abstain' else ''}）"))

    if args.miss or misses:
        title = "判错与未判" if not args.miss else "判错与未判（--miss）"
        print(f"—— {title} ——")
        if not misses:
            print("  （无）")
        for group, text, want, got in misses:
            print(f"  [{group:9s}] {text}\n      期望 {want} / 实得 {got}")
        print()

    head = ("分组", "条数", "判对", "判错", "弃权", "未判(无模型)", "准确率")
    body = []
    for group in GROUPS:
        counter = stat[group]
        total = sum(counter.values())
        if not total:
            continue
        # 分母只剔 no_model（这台机器没配 key，机器没判）。**弃权留在分母里** ——
        # 模型说了「不知道」是判过了，那一单要人接手，剔出去准确率就虚高了。
        graded = total - counter["no_model"]
        rate = f"{counter['hit'] / graded * 100:.1f}%" if graded else "—（全未判）"
        body.append([group, str(total), str(counter["hit"]), str(counter["miss"]),
                     str(counter["abstain"]), str(counter["no_model"]), rate])

    widths = [max(_w(head[i]), *(_w(r[i]) for r in body)) for i in range(len(head))]
    line = "  ".join(_pad(h, w) for h, w in zip(head, widths)).rstrip()
    print(line)
    print("-" * _w(line))
    for cells in body:
        print("  ".join(_pad(c, w) for c, w in zip(cells, widths)).rstrip())

    hard_miss = sum(stat[g]["miss"] for g in GROUPS)
    abstain = sum(stat[g]["abstain"] for g in GROUPS)
    unknown_leak = stat["unknown"]["miss"]
    print(f"\n判错 {hard_miss} 条、弃权 {abstain} 条（转人工，是成本不是事故）。")
    print(f"其中 {unknown_leak} 条是**该说不知道却给了个 code** —— 这一类每一条都会"
          f"套上政策一路跑到批款，是这张表上唯一会赔钱的错。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
