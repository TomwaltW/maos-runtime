#!/usr/bin/env python3
"""供应链付款入口的端到端冒烟 —— 装齐了就跑一遍，没装齐就说清楚缺哪块。

    python3 scripts/ap_smoke.py

## 三个退出码，各说一件事

    0  三块都在，端到端跑通了（底账 + 申请表 → 三单匹配 → 付款计划 → 停 BLOCKED）
    3  **有块还没并进来**，本次什么都没跑
    1  三块都在，但跑挂了 —— 这才是真回归

3 和 1 分开是有意的：这条通道分四轨并行建，「还没装好」在整合期是**常态**，
「装好了但坏了」是**事故**。两者共用 exit 1 的话，CI 上一片红，人就不看了；
分开之后，红的那一条一定值得停下来查。

## 不打网络、不连真银行

本脚本自己不发任何请求；跑子进程前会把环境里所有模型网关的 key 与 base url
摘掉（见 `_offline_env`），配着可用 key 的机器上也不会出网。
付款侧走域内的假银行，不连任何真实银行接口。
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 这条通道由四轨并行建。前三块是本脚本的依赖，第四块（文档与冒烟）就是本脚本自己。
#: 每块登记：职责名、要 import 的模块、要存在的文件。
#: INTEGRATION-POINT: 三轨并入后这张表不用改 —— 它按路径探，探到即用。
BLOCKS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        "表格入口",
        ("maos.flows.ap_case",),
        ("scripts/run_ap.py",
         "scenarios/custom/ap-ledger.json",
         "scenarios/custom/ap-invoices.csv"),
    ),
    (
        "票据抽取",
        ("maos.tools.invoice_extract",),
        (),
    ),
    (
        "复核关口",
        ("maos.flows.ap_intake_review",),
        ("scripts/review_invoices.py",),
    ),
)

#: 端到端跑通的话，输出里该看得见这几件事。看不见不算失败（措辞会变），
#: 只打一行提示 —— 判失败的依据是子进程的退出码，不是措辞。
EXPECTED_MARKS: tuple[str, ...] = ("匹配", "付款")


def _offline_env() -> dict[str, str]:
    """摘掉所有模型网关凭据的一份环境，确保子进程一行网络都走不了。"""
    env = dict(os.environ)
    for k in list(env):
        u = k.upper()
        if "API_KEY" in u or "BASE_URL" in u or u.endswith("_TOKEN"):
            env.pop(k, None)
    env["MAOS_FORCE_SCRIPTED"] = "1"
    return env


def _missing(block: tuple[str, tuple[str, ...], tuple[str, ...]]) -> list[str]:
    """这一块缺了什么。空列表 = 齐了。"""
    _name, modules, files = block
    gaps: list[str] = []
    for mod in modules:
        try:
            found = importlib.util.find_spec(mod) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            gaps.append(mod)
    gaps += [f for f in files if not (ROOT / f).exists()]
    return gaps


def _run_end_to_end(verbose: bool) -> int:
    """三块都在，跑一遍端到端。返回本脚本的退出码。"""
    cmd = [
        sys.executable, str(ROOT / "scripts" / "run_ap.py"),
        "scenarios/custom/ap-invoices.csv",
        "--ledger", "scenarios/custom/ap-ledger.json",
    ]
    print("跑：" + " ".join(cmd[1:]))
    proc = subprocess.run(cmd, cwd=str(ROOT), env=_offline_env(),
                          capture_output=True, text=True)
    out = (proc.stdout or "") + (proc.stderr or "")
    if verbose or proc.returncode != 0:
        print(out.rstrip())
    else:
        print("\n".join(out.rstrip().splitlines()[-20:]))

    if proc.returncode != 0:
        print(f"\n❌ 端到端跑挂了（表格入口退出码 {proc.returncode}）—— 三块都在，"
              f"所以这是真回归，不是没装好。")
        return 1

    absent = [m for m in EXPECTED_MARKS if m not in out]
    if absent:
        print(f"\n⚠️  输出里没看到 {'、'.join(absent)} —— 措辞可能改过，"
              f"判据以退出码为准，这里只是提醒回去核一眼结果表。")
    print("\n✅ 端到端跑通：底账 + 申请表 → 三单匹配 → 付款计划 → 停 BLOCKED 等人批。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="供应链付款入口端到端冒烟；缺块时 exit 3，跑挂时 exit 1")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="把端到端的完整输出打全，不只打末尾 20 行")
    args = parser.parse_args(argv)

    print("=" * 70)
    print("供应链付款入口 · 端到端冒烟")
    print("=" * 70)

    gaps: dict[str, list[str]] = {}
    for block in BLOCKS:
        miss = _missing(block)
        if miss:
            gaps[block[0]] = miss
        print(f"  [{'缺' if miss else '在'}] {block[0]}")

    if gaps:
        print()
        for name, miss in gaps.items():
            print(f"{name} 尚未合并 —— 缺 {'、'.join(miss)}")
        print(f"\n本次什么都没跑。exit 3 = 还没装好（不是坏了）；"
              f"三块都并进来之后再跑这条命令。")
        return 3

    return _run_end_to_end(args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
