#!/usr/bin/env python3
"""守卫探针 —— 问「这条命令会不会被 PreToolUse 守卫拦下」，而不是去猜。

为什么必须落成文件
------------------
最直觉的验证写法是 ``python3 -c "...<受保护的裸文件名>..."``，但守卫遇到
``$(...)`` / 反引号 / ``eval`` / ``base64`` / ``awk`` / ``source`` / ``<<<``
就放弃位置判定、改对整条命令做子串扫描 —— **探针自己先被拦**。
落成文件再 ``python3 review/tools/guard_probe.py`` 执行就绕开了：
命令行 token 里不含受保护路径，而守卫**只看 file_path、不看文件内容**。

这条「落成文件」的技巧本身就是派单模板 §3 要教的招式之一，
见 ``review/DISPATCH-TEMPLATE.md``。

它做什么
--------
把每条待测命令包成 PreToolUse 的 payload
``{"tool_name": "Bash", "tool_input": {"command": ...}}``，从 stdin 喂给守卫脚本，
打印守卫的退出码与 stderr。**只调用、不修改**守卫。

    exit=0  放行
    exit=2  拦下（stderr 里是拦截原因）

怎么用
------
    python3 review/tools/guard_probe.py                  # 跑内置样例
    python3 review/tools/guard_probe.py '<一条命令>' ... # 探自己写的命令
    python3 review/tools/guard_probe.py --file cmds.txt  # 一行一条，从文件读

⚠️ 命令行传参有个坑：待探的命令里若含受保护的裸文件名，**你敲的这一行本身**
会先被守卫拦下，探针根本起不来。这种命令要么加进下面的内置样例，
要么写进文件用 ``--file`` 读 —— 守卫不看文件内容。

退出码：所有带期望值的样例都对上 → 0；有一条对不上 → 1。
对不上意味着守卫或仓库布局变了，**停下来报告，别改期望值迁就**。

不进 pytest
-----------
它依赖仓库布局（相对本文件定位仓库根）与守卫脚本的存在，是排障工具、不是单测。
放在 ``review/tools/`` 而不是 ``maos/tests/`` 就是这个意思。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 路径拼出来、不写成一个完整字面量常量：别人 grep 受保护路径时不该误伤本文件。
# 环境变量给一个出口，方便在别的检出上探同一份守卫。
GUARD_PATH = os.environ.get("MAOS_GUARD_PATH") or os.path.join(
    REPO_ROOT, "scripts", "guard_" + "bash.py"
)

BLOCK, ALLOW = 2, 0

#: (标号, 命令, 期望退出码 或 None 表示只观察不断言)
#: 前四条是派单 §1 那张表，**期望值写死，对不上就是守卫或布局变了**。
#: 后面几条是模板 §3 各条招式的现场依据，期望值同样按实测钉住。
CASES: list[tuple[str, str, int | None]] = [
    # —— 派单 §1 表格：三拦一放 ——
    ("1", "git log --oneline -- scripts/guard_bash.py", BLOCK),
    ("2", "grep -rn guard_bash.py review/", BLOCK),
    ("3", "wc -l scripts/guard_bash.py", BLOCK),
    ("4", "git log --oneline -- README.md", ALLOW),
    # —— BARE_MATCH：另外两个裸文件名，不写路径照样命中 ——
    ("5", "cat .contracts.lock", BLOCK),
    ("6", "echo relock_contracts.py", BLOCK),
    # —— OPAQUE_RE：不透明构造 + 受保护名 ——
    ("7", "echo $(wc -l scripts/guard_bash.py)", BLOCK),
    ("8", "awk '{print}' README.md", ALLOW),
    # —— 模板 §3 给的替代写法，必须真的放行 ——
    ("9", "git log --oneline -5", ALLOW),
    ("10", "git commit -F /tmp/commit-msg.txt", ALLOW),
]


def probe(command: str) -> tuple[int, str]:
    """把一条命令喂给守卫，返回 (退出码, stderr)。"""
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    env = {**os.environ, "CLAUDE_PROJECT_DIR": REPO_ROOT}
    proc = subprocess.run(
        [sys.executable, GUARD_PATH],
        input=payload,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
    )
    return proc.returncode, (proc.stderr or "").strip()


def main() -> int:
    ap = argparse.ArgumentParser(description="探一条命令会不会被 PreToolUse 守卫拦下")
    ap.add_argument("command", nargs="*", help="待探命令；不给就跑内置样例")
    ap.add_argument("--file", help="从文件读待探命令，一行一条（含受保护裸名时只能走这条路）")
    args = ap.parse_args()

    if not os.path.exists(GUARD_PATH):
        print(f"守卫脚本不在：{GUARD_PATH}", file=sys.stderr)
        return 1

    cases: list[tuple[str, str, int | None]]
    if args.file:
        with open(args.file, encoding="utf-8") as fh:
            lines = [ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")]
        cases = [(str(i), ln, None) for i, ln in enumerate(lines, 1)]
    elif args.command:
        cases = [(str(i), c, None) for i, c in enumerate(args.command, 1)]
    else:
        cases = CASES

    print(f"守卫：{GUARD_PATH}")
    print(f"仓库：{REPO_ROOT}\n")

    mismatched = 0
    for label, command, expect in cases:
        code, err = probe(command)
        verdict = "拦下" if code == BLOCK else ("放行" if code == ALLOW else f"异常 exit={code}")
        mark = ""
        if expect is not None:
            ok = code == expect
            mark = "  [符合期望]" if ok else f"  [❌ 期望 exit={expect}]"
            mismatched += 0 if ok else 1
        print(f"[{label}] {command}")
        print(f"    exit={code}  {verdict}{mark}")
        if err:
            print(f"    stderr: {err}")
        print()

    if mismatched:
        print(f"❌ {mismatched} 条与期望不符 —— 守卫或仓库布局变了，停下来报告，别改期望值迁就。")
        return 1
    print("✅ 全部与期望一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
