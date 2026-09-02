"""受保护路径判定「只留一处」的回归守卫（T83）。

判定下沉到 ``maos/tools/paths.py`` 之后，最危险的失效形态不是报错，是**静默放行**：

* 有人为了「兼容」在 skills 层再转出一份常量 —— 两份从此各自漂，漂的那次没人发现；
* 有人照「路径前缀」的直觉往清单里塞带斜杠的条目 —— 分段相等下永远匹配不上；
* 有人把 tools 层的 import 方向又掰回去 —— 环回来，冒烟当场炸。

这三件事都不会有别的测试红给你看，所以本文件在这里。
"""

from __future__ import annotations

import ast
import pathlib
import re
import subprocess
import sys

from maos.tools.paths import PROTECTED_SEGMENTS, _path_segments

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PATHS_MODULE = REPO_ROOT / "maos" / "tools" / "paths.py"

_SKIP_DIRS = {"__pycache__", ".git", ".venv", "venv", "node_modules", ".worktrees"}

# 行首（允许缩进）的赋值。等号后不许再跟一个 = ，那是比较不是赋值。
_ASSIGNMENT = re.compile(r"^[ \t]*PROTECTED_SEGMENTS[ \t]*=(?!=)", re.MULTILINE)


def _repo_py_files() -> list[pathlib.Path]:
    # 只看仓库内的相对路径分段：拿绝对路径去比会把仓库自身所在的目录名一起判进来
    # （本仓常在 .worktrees/task-xx 下作业），结果是一个文件都扫不到、这条守卫空转。
    return [
        p for p in REPO_ROOT.rglob("*.py")
        if not _SKIP_DIRS.intersection(p.relative_to(REPO_ROOT).parts)
    ]


def test_protected_segments_is_defined_in_exactly_one_place():
    """全仓只许有一处赋值，且必须在 tools 层那一处。

    再转出一份「为了兼容」的别名，这条立刻红 —— 那正是 BACKLOG 里
    「别留两个入口」要挡的事。
    """
    hits = [
        p for p in _repo_py_files()
        if _ASSIGNMENT.search(p.read_text(encoding="utf-8", errors="surrogateescape"))
    ]
    rel = sorted(str(p.relative_to(REPO_ROOT)) for p in hits)
    assert rel == ["maos/tools/paths.py"], f"受保护路径清单出现了第二个入口: {rel}"


def test_tools_layer_does_not_import_skills_layer():
    """tools 不许 import skills —— 依赖方向反了就是那个环回来了。

    substring 与 AST 各判一遍：前者连注释里的 dotted 写法都不放过（免得下一个人
    照着注释又写回去），后者判的是真 import 语句，改不掉。
    """
    source = PATHS_MODULE.read_text(encoding="utf-8")
    assert "maos.skills" not in source

    imported: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    upward = [m for m in imported if m.split(".")[:2] == ["maos", "skills"]]
    assert upward == [], f"tools 层反向 import 了 skills 层: {upward}"


def test_octal_escaped_segment_is_still_caught():
    """搬家没把八进制转义那条绕过口带松：``\\164ests`` 仍要判成 ``tests``。"""
    assert _path_segments('"a/\\164ests/conftest.py"') == ["a", "tests", "conftest.py"]
    assert PROTECTED_SEGMENTS.intersection(
        _path_segments('"a/\\164ests/conftest.py"')) == frozenset({"tests"})


def test_a_segment_with_slash_would_never_match():
    """带斜杠的条目在分段相等下恒不命中 —— 不报错、只放行，是最坏的那种失效。

    清单存的是**裸目录名**。谁哪天照「路径前缀」的直觉往里塞一条带斜杠的，
    拦不住的补丁会静默打进去。这条把那个形态钉死，也顺带说明为什么不许改语义。
    """
    segments = _path_segments("tests/test_login.py")
    assert frozenset({"tests/"}).intersection(segments) == frozenset()
    assert frozenset({"tests"}).intersection(segments) == frozenset({"tests"})
    assert all("/" not in seg for seg in PROTECTED_SEGMENTS)


def test_no_import_cycle_on_fresh_interpreter():
    """干净解释器里单独 import tools 层，不许再炸 ImportError。

    环当年是冒烟当场炸出来的（tools → skills → discover() → 回到还没装载完的
    tools）。同进程里 reload 判不出来 —— 模块早在别的用例里装载过了，所以起
    子进程。
    """
    proc = subprocess.run(
        [sys.executable, "-c", "import maos.tools.sandbox; print('ok')"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"
    assert "ImportError" not in proc.stderr
