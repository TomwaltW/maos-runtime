"""生成物守卫 —— `scripts/gen_docs.py` 的三份产出必须与代码逐字节一致。

`docs/agent-identity.md` / `docs/skill-catalog.md` / `docs/toolport-contract.md`
是代码的投影，不是手写文档。生成器自己在文件头写着「请勿手改」，但那句话是
**给人看的**：加一个 Agent、给 skill 换个失败策略、给工具收紧一条安全边界，
没有人会记得回来重跑生成器 —— 文档于是悄悄过期，而且不会有任何东西变红。

这个模块把那条纪律变成机器判据。它守的是两个方向：

- **代码改了、文档没跟** —— 逐字节比对当场红，报里直接给「跑 gen_docs.py」。
- **文档被手改了** —— 同一条比对同样红，因为手改的内容不是代码的投影。

## 为什么 render 要在子进程里跑，不在测试进程里直接调

`collect_agents()` 扫的是 `BaseAgent.__subclasses__()`，`collect_skills()` 读的是
全局 `SKILL_REGISTRY` —— **两者都是进程级可见状态**。测试进程里定义一个带
`identity` 的 dummy Agent、或往注册表塞一个临时 skill 版本，扫描结果就多出一行。

当前套件里那几处（`test_skills.py` 的 1.9.0/1.10.0、`test_skill_versioning.py` 的
v1.1.0、`test_registry_autodiscovery.py` 的探针）都规规矩矩清理了自己，所以
在进程内直接调 render 眼下也是绿的。**但那是运气，不是设计**：清理靠的是
`try/finally` 与 fixture 拆解，一旦谁漏一次，或者谁在模块顶层定义了一个 dummy
Agent，这里就会红 —— 而报出来的原因是「文档与代码不一致，去重跑生成器」，
指向完全错误的方向，下一个人会白查半天。（实测：进程内定义一个带 identity 的
BaseAgent 子类，`agent-identity.md` 的角色数当场从 11 变 12。）

子进程是干净解释器，只 import 生产代码，扫到的东西与人在命令行上跑
`python3 scripts/gen_docs.py` 完全一致 —— 那正是「生成物应该等于什么」的定义。

## 副作用

一次都不写 `docs/`。子进程把 render 结果写进 pytest 的 `tmp_path`，比对在内存里做。
`test_check_mode_does_not_write` 反过来把这条钉死：跑完 `--check`，三份文件的
内容与 mtime 一个都不许变。
"""

from __future__ import annotations

import difflib
import hashlib
import importlib.util
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
GENERATOR = ROOT / "scripts" / "gen_docs.py"

# 子进程 driver：import 生成器，把每份 render 结果落到 argv[2] 指定的目录。
# 用 `gd.TARGETS` 而不是在这里另抄一份清单 —— 生成器将来多产一份文档，
# 这个守卫自动跟着守，不会漏。（生成器自己的第 2 条自我约束就是「数量不写死」。）
_DRIVER = '''\
import importlib.util, pathlib, sys

root, out = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
spec = importlib.util.spec_from_file_location("gen_docs", root / "scripts" / "gen_docs.py")
gd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gd)

for relpath, render in gd.TARGETS:
    (out / pathlib.Path(relpath).name).write_text(render(), encoding="utf-8")
'''


def _targets() -> list[str]:
    """三份生成物的仓库相对路径。只读生成器的 TARGETS 常量，不调 render。

    import 生成器在主进程里是安全的：render 函数不在 import 时执行，
    模块级代码只往 sys.path 插了一次仓库根。
    """
    spec = importlib.util.spec_from_file_location("gen_docs_targets", GENERATOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return [relpath for relpath, _ in mod.TARGETS]


TARGETS = _targets()


@pytest.fixture(scope="module")
def rendered(tmp_path_factory) -> dict[str, str]:
    """在干净子进程里跑一次生成器，返回 {仓库相对路径: 应有内容}。"""
    out = tmp_path_factory.mktemp("gen_docs_render")
    driver = out / "_driver.py"
    driver.write_text(_DRIVER, encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(driver), str(ROOT), str(out)],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    assert proc.returncode == 0, (
        "生成器在子进程里跑挂了 —— 先修生成器本身，这轮比对无从谈起。\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    return {relpath: (out / pathlib.Path(relpath).name).read_text(encoding="utf-8")
            for relpath in TARGETS}


def _diff_report(relpath: str, current: str, expected: str, limit: int = 40) -> str:
    """给下一个撞上这条断言的人看的报告 —— 他需要一眼看出「去重跑生成器」。"""
    diff = list(difflib.unified_diff(
        current.splitlines(), expected.splitlines(),
        fromfile=f"{relpath}（当前 git 中的版本）",
        tofile=f"{relpath}（按当前代码应有）",
        lineterm="", n=1))
    shown = "\n".join(f"    {line}" for line in diff[:limit])
    if len(diff) > limit:
        shown += f"\n    …… 另有 {len(diff) - limit} 行差异"
    return (
        f"\n{relpath} 与代码不一致。\n\n"
        "  这份文档是 scripts/gen_docs.py 从运行时代码生成的投影，**不许手改**。\n"
        "  绝大多数情况是：改了 Agent / Skill / ToolPort 的定义，没重跑生成器。\n\n"
        "  修复就一条命令：\n\n"
        "      python3 scripts/gen_docs.py\n\n"
        f"  差异（- 当前／+ 应有）：\n{shown}\n"
    )


@pytest.mark.parametrize("relpath", TARGETS)
def test_generated_doc_matches_code(relpath, rendered):
    """三份生成物逐字节等于「现在重跑生成器会得到的东西」。"""
    path = ROOT / relpath
    assert path.exists(), f"{relpath} 不见了 —— 跑 `python3 scripts/gen_docs.py` 生成"
    current = path.read_text(encoding="utf-8")
    expected = rendered[relpath]
    assert current == expected, _diff_report(relpath, current, expected)


def test_guard_actually_catches_a_hand_edit(rendered):
    """守卫的牙齿本身要有测试守着 —— 否则比对逻辑写歪了也没人知道。

    模拟「有人手改了一个字」：报告必须点名是哪份文件，且差异里看得见那处改动。
    """
    relpath = TARGETS[0]
    expected = rendered[relpath]
    lines = expected.splitlines(keepends=True)
    hand_edited = "".join(lines[:6] + ["这一行是手改的，不是代码的投影。\n"] + lines[6:])

    assert hand_edited != expected, "构造的篡改必须与原文不同，否则这条测试是空转"

    report = _diff_report(relpath, hand_edited, expected)
    assert relpath in report, "报告要点名是哪一份文件"
    assert "python3 scripts/gen_docs.py" in report, "报告要给出修复命令"
    assert "这一行是手改的" in report, "差异里要看得见被改的那一行"


def test_check_mode_agrees_and_writes_nothing():
    """`gen_docs.py --check` 是 Phase 7 的验收命令，它的退出码契约也要守。

    顺带钉死 `--check` 不写盘：三份文件的内容哈希与 mtime 跑前跑后必须一致。
    """
    paths = [ROOT / relpath for relpath in TARGETS]
    before = [(p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
              for p in paths]

    proc = subprocess.run(
        [sys.executable, str(GENERATOR), "--check"],
        capture_output=True, text=True, cwd=str(ROOT),
    )

    after = [(p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
             for p in paths]
    assert before == after, (
        "`--check` 写盘了 —— 它只许比对不许落文件，否则跑一次测试就把工作区弄脏。\n"
        f"{[p.name for p in paths]}\n跑前 {before}\n跑后 {after}"
    )
    assert proc.returncode == 0, (
        "`python3 scripts/gen_docs.py --check` 非零退出 —— 文档落后于代码。\n"
        "跑 `python3 scripts/gen_docs.py` 重新生成。\n\n"
        f"{proc.stdout}{proc.stderr}"
    )
