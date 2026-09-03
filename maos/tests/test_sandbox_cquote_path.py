"""C-quoted 路径绕过的回归守卫 —— 出处 review/audit-2026-08-29-baseline-1131795.md ## P0-1。

绕过原理：git 把含特殊字节的路径写成 ``"a/\\164ests/conftest.py"``（``\\164`` 是 ``t``
的八进制），``git apply`` 会解码成 ``tests/…`` 再落盘；而路径校验从前直接吃字面量，
第一步 ``path.replace("\\\\", "/")`` 把转义反斜杠吃成了路径分隔符，段成了 ``164ests``。
**判定读字面量、执行读解码后**，两者一分叉，三条校验一起失效：

  1. ``PROTECTED_SEGMENTS`` 匹配的是 ``164ests``，与 ``tests`` 不相等；
  2. ``conftest_guard`` 比 ``segments[-1] == "conftest.py"``，实际末段是 ``conftest.py"``；
  3. 内含性校验拿字面量去 join workdir，``"a/\\056\\056/x"`` 只是个名字古怪的子路径。

所以每条用例都**同时断言返回值和靶场文件内容**：返回 ok=False 但文件已经被改过，
才是最坏的一种「修好了」—— 只断返回值的测试会把它判成绿。

用例全部在 ``tmp_path`` 现建靶场，不往源码树写文件。不发网络请求、不读任何 key。
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest

from maos.skills.builtin.code_repo_patch import (
    PROTECTED_SEGMENTS,
    _path_segments,
    unquote_c_style,
)
from maos.tools.sandbox import (
    FIXTURE_REPO,
    _numstat_targets,
    prepare_sandbox_workdir,
    sandbox_git_apply,
)

CONFTEST = "tests/conftest.py"
BASELINE = "# original\n"
SESSION_FILE = "auth/session.py"

# 't' / '.' 的八进制转义。写成常量是为了让下面每条用例一眼看出它在绕哪个字符。
OCT_T = "\\164"
OCT_D = "\\144"
OCT_DOT = "\\056"


def _commit(workdir: pathlib.Path, message: str) -> None:
    subprocess.run(["git", "-C", str(workdir), "add", "-A"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(workdir),
                    "-c", "user.name=maos-sandbox", "-c", "user.email=sandbox@maos.local",
                    "commit", "-q", "-m", message],
                   check=True, capture_output=True)


@pytest.fixture
def workdir(tmp_path):
    """现建靶场，并补一个 ``tests/conftest.py`` 作为攻击目标。

    靶场自带 ``tests/`` 但没有 ``tests/conftest.py``；补丁要有基线内容才能
    ``@@ -1 +1,2 @@`` 地打进去，也才谈得上「逐字节未变」这条断言。
    """
    path = pathlib.Path(prepare_sandbox_workdir(str(tmp_path / "repo"),
                                                source=FIXTURE_REPO))
    (path / CONFTEST).write_text(BASELINE, encoding="utf-8")
    _commit(path, "conftest baseline")
    return path


def _quoted_diff(quoted_path: str) -> str:
    """造一份 git 风格的 C-quoted 补丁，往 quoted_path 追加一行。

    三处路径（``diff --git`` 两个、``---``、``+++``）全带引号，与 git 自己
    ``core.quotepath`` 打开时的产出一致 —— 少写一处，校验就可能从别处兜住，
    用例也就测不到真正的绕过口。
    """
    return (f'diff --git "a/{quoted_path}" "b/{quoted_path}"\n'
            f'--- "a/{quoted_path}"\n'
            f'+++ "b/{quoted_path}"\n'
            '@@ -1 +1,2 @@\n'
            ' # original\n'
            '+PWNED = True\n')


def _new_file_quoted_diff(quoted_path: str) -> str:
    """C-quoted 的新增文件补丁 —— 越界写入那条用的是它（目标本来就不存在）。"""
    return (f'diff --git "a/{quoted_path}" "b/{quoted_path}"\n'
            'new file mode 100644\n'
            '--- /dev/null\n'
            f'+++ "b/{quoted_path}"\n'
            '@@ -0,0 +1 @@\n'
            '+stolen\n')


def _apply(workdir: pathlib.Path, diff: str, *, declared: str = "src/ok.py") -> dict:
    """declared 故意填无害值 —— 真实目标藏在 diff 正文里，正是这个绕过的形状。"""
    return sandbox_git_apply({"files": [{"path": declared, "diff": diff}]}, str(workdir))


# ---------------------------------------------------------------------------
# 绕过口 1：PROTECTED_SEGMENTS 被八进制转义绕开
# ---------------------------------------------------------------------------
def test_cquoted_octal_tests_dir_rejected_and_file_untouched(workdir):
    """``"a/\\164ests/conftest.py"`` 必须被拦，且靶场文件逐字节未变。

    两个条件缺一不算修好：返回 ok=False 但文件已被改写，说明拦在了落盘之后。
    """
    before = (workdir / CONFTEST).read_bytes()

    result = _apply(workdir, _quoted_diff(f"{OCT_T}ests/conftest.py"))

    assert result["ok"] is False
    assert result["error"]["stage"] == "path_check"
    # path 报的是**解码后**的路径：Gate 要把它转成 findings 喂回 Coding，
    # 回一个 `"a/\164ests/…"` 的字面量等于让下游自己再解一遍码。
    assert result["error"]["path"] == CONFTEST
    assert (workdir / CONFTEST).read_bytes() == before
    assert b"PWNED" not in before


def test_cquoted_octal_matches_plain_path_verdict(workdir):
    """C-quoted 与明文两条路径判出来必须是同一个结论 —— 分叉本身就是绕过口。"""
    quoted = _apply(workdir, _quoted_diff(f"{OCT_T}ests/conftest.py"))
    plain = _apply(workdir, _quoted_diff("tests/conftest.py").replace('"', ""))

    assert quoted["ok"] is plain["ok"] is False
    assert quoted["error"]["stage"] == plain["error"]["stage"]
    assert quoted["error"]["path"] == plain["error"]["path"] == CONFTEST


# ---------------------------------------------------------------------------
# 绕过口 2：受保护段不止 tests —— .github 同样躲得过
# ---------------------------------------------------------------------------
def test_cquoted_octal_github_workflows_rejected(workdir):
    """``"a/\\056github/workflows/ci.yml"`` —— ``\\056`` 是 ``.``，解码后命中 .github。

    单测 tests/ 一个段会让「只把 tests 这一个字符串特判掉」也能过，
    那是把绕过口挪了个位置，不是堵上。
    """
    result = _apply(workdir,
                    _new_file_quoted_diff(f"{OCT_DOT}github/workflows/ci.yml"))

    assert result["ok"] is False
    assert result["error"]["stage"] == "path_check"
    assert result["error"]["path"] == ".github/workflows/ci.yml"
    assert not (workdir / ".github").exists()


# ---------------------------------------------------------------------------
# 绕过口 3：conftest_guard 的末段比对被尾引号打偏
# ---------------------------------------------------------------------------
def test_cquoted_conftest_outside_tests_still_guarded(workdir):
    """非受保护目录下的 conftest.py 要由 conftest_guard 兜住，不是由 path_check。

    这条挑 ``docs/`` 是有意的：落在 ``tests/`` 里 path_check 先命中，
    conftest_guard 那条永远走不到，尾引号的失效点也就测不出来 ——
    未解码时末段是 ``conftest.py"``，与 ``conftest.py`` 不相等。
    """
    result = _apply(workdir, _new_file_quoted_diff(f"{OCT_D}ocs/conftest.py"))

    assert result["ok"] is False
    assert result["error"]["stage"] == "conftest_guard"
    assert result["error"]["path"] == "docs/conftest.py"
    assert not (workdir / "docs").exists()


def test_segments_last_element_has_no_trailing_quote():
    """直接钉住失效点本身：解码后末段必须是干净的 conftest.py。"""
    assert _path_segments('"a/\\164ests/conftest.py"')[-1] == "conftest.py"
    assert PROTECTED_SEGMENTS.intersection(_path_segments('"\\164ests/x.py"'))


# ---------------------------------------------------------------------------
# 绕过口 4：内含性校验拿字面量 join，越界路径落不出去也判不出来
# ---------------------------------------------------------------------------
def test_cquoted_octal_dotdot_rejected_by_maos_not_by_git(workdir):
    """``\\056\\056`` 是 ``..`` 的八进制。修之前是 git apply 自己挡的，修完要由 MAOS 挡。

    靠 git 兜底不算防住：那道拦截在 MAOS 的校验**之后**，换个 git 版本或换条
    执行路径就没了，而 stage 报 apply 会让 Gate 把安全事件当成普通的补丁冲突。
    """
    result = _apply(workdir,
                    _new_file_quoted_diff(f"{OCT_DOT}{OCT_DOT}/STOLEN.txt"))

    assert result["ok"] is False
    assert result["error"]["stage"] == "path_escape"     # 不是 apply
    assert result["error"]["path"] == "../STOLEN.txt"
    assert not (workdir.parent / "STOLEN.txt").exists()


# ---------------------------------------------------------------------------
# 反向：不许修成「一律拒绝」—— 合法补丁必须照样打得进去
# ---------------------------------------------------------------------------
def test_plain_legal_patch_still_applies(workdir):
    """明文合法路径的真补丁仍能落盘。场景 1/2 的返工链全压在这条上。"""
    target = workdir / SESSION_FILE
    original = target.read_text(encoding="utf-8")
    target.write_text(original + "\n# patched by test\n", encoding="utf-8")
    diff = subprocess.run(["git", "-C", str(workdir), "diff"],
                          capture_output=True, text=True, check=True).stdout
    subprocess.run(["git", "-C", str(workdir), "checkout", "--", SESSION_FILE],
                   check=True, capture_output=True)
    assert target.read_text(encoding="utf-8") == original

    result = _apply(workdir, diff, declared=SESSION_FILE)

    assert result == {"ok": True, "error": None}
    assert target.read_text(encoding="utf-8").endswith("# patched by test\n")


def test_legal_path_containing_quote_still_applies(tmp_path):
    """文件名里带引号的合法补丁也必须打得进去 —— 这条钉死「凡带引号一律拒绝」那条歪路。

    补丁不是手写的：让 git 自己 diff 出来，它会照 core.quotepath 把路径 C-quote 成
    ``"auth/we\\"ird.py"``（引号走字母转义 ``\\"``，不是八进制 —— 手写样例最容易
    在这里猜错，所以这条用例宁可去问 git）。解码器与 git 的编码器在这里做了一次
    真实的往返对拍，比任何手写样例都硬 —— 对不上就是我们的解码与 git 分叉了。
    """
    workdir = pathlib.Path(prepare_sandbox_workdir(str(tmp_path / "repo"),
                                                   source=FIXTURE_REPO))
    weird = workdir / 'auth/we"ird.py'
    weird.write_text("x = 1\n", encoding="utf-8")
    _commit(workdir, "add quoted-name file")

    weird.write_text("x = 1\ny = 2\n", encoding="utf-8")
    diff = subprocess.run(["git", "-C", str(workdir), "diff"],
                          capture_output=True, text=True, check=True).stdout
    subprocess.run(["git", "-C", str(workdir), "checkout", "--", "."],
                   check=True, capture_output=True)
    assert '"a/auth/we\\"ird.py"' in diff, \
        f"git 没有 C-quote 这个路径，用例前提不成立：{diff!r}"

    result = _apply(workdir, diff, declared='auth/we"ird.py')

    assert result == {"ok": True, "error": None}
    assert weird.read_text(encoding="utf-8") == "x = 1\ny = 2\n"


# ---------------------------------------------------------------------------
# 判定与执行同源：numstat 报的是 git 解码后的落盘路径
# ---------------------------------------------------------------------------
def test_numstat_reports_git_decoded_path(workdir):
    """把「同源」这件事本身钉住：git 自己报的路径必须已经是解码后的。"""
    diff = _quoted_diff(f"{OCT_T}ests/conftest.py")
    listed = _numstat_targets(str(workdir), ["git", "apply"], diff)

    assert listed == [CONFTEST]


def test_rename_out_of_protected_dir_still_rejected(workdir):
    """rename 的**源**在 tests/ 下也要拦 —— 守的是「别把 _diff_targets 换成纯 numstat」。

    实测 ``git apply --numstat`` 对 rename 只报目标路径，源路径不出现。所以
    numstat 是补充不是替代：只信它，「把测试文件 rename 走等于删掉」这条现成的
    覆盖就没了，而丢覆盖不会有任何测试变红 —— 除了这一条。
    """
    result = _apply(workdir,
                    'diff --git a/tests/test_session.py b/auth/renamed.py\n'
                    'similarity index 100%\n'
                    'rename from tests/test_session.py\n'
                    'rename to auth/renamed.py\n')

    assert result["ok"] is False
    assert result["error"]["stage"] == "path_check"
    assert (workdir / "tests/test_session.py").exists()
    assert not (workdir / "auth/renamed.py").exists()


# ---------------------------------------------------------------------------
# 解码器本身：只在真是 C-quoted 时才动手
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw, expected", [
    ('"a/\\164ests/conftest.py"', "a/tests/conftest.py"),   # 八进制
    ('"\\056\\056/STOLEN.txt"', "../STOLEN.txt"),           # .. 的八进制
    ('"we\\"ird.py"', 'we"ird.py'),                         # git 实际对引号用的字母转义
    ('"we\\042ird.py"', 'we"ird.py'),                       # 同一个字符的八进制写法
    ('"tab\\there.py"', "tab\there.py"),                    # 字母转义
    ('"back\\\\slash.py"', "back\\slash.py"),
    ("auth/session.py", "auth/session.py"),                 # 没引号：原样
    ('say"hi".py', 'say"hi".py'),                           # 引号不在首尾：原样
    ('"', '"'),                                             # 单个引号：不足以成对
    ('"\\999x"', "\\999x"),                                 # 不是八进制：反斜杠留着
    ('"\\400x"', "\\400x"),                                 # 超出单字节：不是 git 的产物
])
def test_unquote_c_style_round_trip(raw, expected):
    assert unquote_c_style(raw) == expected
