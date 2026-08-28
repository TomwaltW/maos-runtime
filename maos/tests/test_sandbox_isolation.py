"""Task-B 的机器验收 —— 沙箱隔离、路径校验、补偿干跑闸、tool_error 分报。

这些不是「跑一遍看看」：每条都在守一件后面会被人无意改坏的事 ——
把 env 白名单改成透传、把 HOME 透传回宿主、把 conftest.py 从禁改清单里拿掉、
只校验声明路径不看 diff 正文、把 tool_error 和 failed 合并成一个计数、
让 --check 也落盘。

全程走**降级路径**（`MAOS_SANDBOX_FORCE_SUBPROCESS=1`），保证无 Docker 的机器
也能跑。这不是将就：靠「碰巧没装 docker」命中降级分支，等于这条路径在装了
Docker 的机器上从来没被测过，而它恰恰是唯一需要自己做 env 隔离的那条。
"""

from __future__ import annotations

import hashlib
import os
import pathlib

import pytest

from maos.artifacts import KIND_TEST_REPORT, validate_artifact
from maos.skills import registry
from maos.tools.sandbox import (
    FIXTURE_REPO,
    _clean_env,
    prepare_sandbox_workdir,
    sandbox_git_apply,
    sandbox_pytest_run,
)

SESSION_FILE = "auth/session.py"
DOC_ANCHOR = '    """会话在 last_seen 之后'
FIXED_TAIL = '''    """会话在 last_seen 之后 SESSION_TTL 之内算有效。两个入参都是 UTC 感知时间。"""
    # 两个入参都是 UTC 感知时间，直接做差就是真实年龄。
    return now - last_seen < SESSION_TTL
'''


def _tree_digest(root: pathlib.Path) -> str:
    """目录内容指纹。跳过 __pycache__ 与 .git —— 前者是跑测试的副产物，
    后者是 prepare 自己建的，两者都不是「补丁有没有落盘」要看的东西。"""
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        parts = path.relative_to(root).parts
        if "__pycache__" in parts or ".git" in parts:
            continue
        digest.update("/".join(parts).encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


# 在任何用例跑之前先取一次宿主靶场的指纹 —— 「宿主未被触碰」那条要跟它比。
SOURCE_DIGEST_AT_IMPORT = _tree_digest(FIXTURE_REPO)


def _fabricate_diff(path: str, *, new_file: bool = False) -> str:
    """造一个语法合法、指向 path 的最小 diff。用于负例：它压根走不到 git apply。"""
    if new_file:
        return (f"diff --git a/{path} b/{path}\n"
                "new file mode 100644\n"
                "--- /dev/null\n"
                f"+++ b/{path}\n"
                "@@ -0,0 +1 @@\n"
                "+import sys\n")
    return (f"diff --git a/{path} b/{path}\n"
            f"--- a/{path}\n"
            f"+++ b/{path}\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n")


@pytest.fixture
def workdir(tmp_path):
    return prepare_sandbox_workdir(str(tmp_path / "repo"))


@pytest.fixture
def golden_patch(workdir):
    """现造金标补丁：改好 auth/session.py，git diff 出来，再还原。

    不把 diff 写死在测试里：写死要连 @@ 行号和上下文一起写死，靶场的注释改一个字
    就得跟着改，而改不动的那次症状是「补丁应用失败」—— 很容易被当成沙箱的锅去查。
    """
    target = pathlib.Path(workdir) / SESSION_FILE
    source = target.read_text(encoding="utf-8")
    head, anchor, _ = source.partition(DOC_ANCHOR)
    assert anchor, f"{SESSION_FILE} 里找不到锚点 {DOC_ANCHOR!r}，靶场被改过就得同步改这里"
    target.write_text(head + FIXED_TAIL, encoding="utf-8")

    import subprocess
    diff = subprocess.run(["git", "-C", workdir, "diff"],
                          capture_output=True, text=True, timeout=60).stdout
    subprocess.run(["git", "-C", workdir, "checkout", "--", "."],
                   capture_output=True, timeout=60)
    assert diff.strip(), "git diff 没产出补丁，靶场副本可能没建成 git 仓库"
    return {"files": [{"path": SESSION_FILE, "diff": diff}],
            "summary": "会话有效期改回 UTC 直减",
            "self_check": {"build": "pass", "lint": "pass"}}


@pytest.fixture(scope="module")
def baseline(tmp_path_factory):
    """打补丁前跑一次靶场，带着**哨兵密钥**跑 —— 探针要证明它们没漏进沙箱。

    模块级只跑一次：下面三条只读断言都看这一份报告，重复跑纯属浪费。
    """
    path = prepare_sandbox_workdir(str(tmp_path_factory.mktemp("baseline") / "repo"))
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("MAOS_SANDBOX_FORCE_SUBPROCESS", "1")
        mp.setenv("MAOS_LLM_API_KEY", "sentinel-key-must-not-leak")
        mp.setenv("MATRIX_TOKEN", "sentinel-token-must-not-leak")
        report = sandbox_pytest_run(path)
    return path, report


@pytest.fixture(autouse=True)
def _force_degraded(monkeypatch):
    monkeypatch.setenv("MAOS_SANDBOX_FORCE_SUBPROCESS", "1")


def _case(report: dict, suffix: str) -> dict:
    hits = [c for c in report["cases"] if c["id"].endswith(suffix)]
    assert len(hits) == 1, f"没在报告里找到唯一的 {suffix}: {[c['id'] for c in report['cases']]}"
    return hits[0]


# --- 隔离探针 ---------------------------------------------------------------
def test_isolation_probes_stay_green_under_env_whitelist(baseline):
    """三条探针在降级路径下的结果 —— env 白名单的直接验证，不是走过场。

    宿主 env 里明明有 MAOS_LLM_API_KEY 与 MATRIX_TOKEN（fixture 灌的哨兵），
    探针却看不到，靠的就是 `_clean_env` 重建了一份 env。谁把它改回
    `env=os.environ`，红的是这条。
    """
    _, report = baseline
    assert report["tool_error"] is None, report["tool_error"]

    # 断网这条只在容器主路径有意义，降级路径按派单允许 skip。
    assert _case(report, "test_no_network")["status"] == "skipped"
    # 另外两条必须仍绿。
    assert _case(report, "test_no_host_secrets")["status"] == "passed"
    assert _case(report, "test_no_home_access")["status"] == "passed"


def test_env_whitelist_rebuilds_env_from_scratch(monkeypatch):
    """白名单是**按名字放行**，不是按名字拦截 —— 只有 PATH / LANG / HOME 三个键。"""
    monkeypatch.setenv("MAOS_LLM_API_KEY", "x")
    monkeypatch.setenv("MATRIX_TOKEN", "y")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "z")     # 黑名单没写它，白名单也拦得住
    monkeypatch.setenv("PATH", "/usr/bin")

    env = _clean_env("/tmp/throwaway-home")

    assert set(env) == {"PATH", "LANG", "HOME"}
    # HOME 指向一次性目录而不是宿主的 —— 透传等于把 ~/.ssh 一并交出去。
    assert env["HOME"] == "/tmp/throwaway-home"
    assert env["HOME"] != os.path.expanduser("~")


def test_host_fixture_repo_is_untouched(baseline):
    """跑完靶场，宿主的 scenarios/fixture-repo/ 内容指纹不变。

    补丁只落在副本上。谁把 workdir 直接指向源目录，红的是这条。
    """
    baseline_path, _ = baseline
    assert os.path.realpath(baseline_path) != os.path.realpath(FIXTURE_REPO)
    assert _tree_digest(FIXTURE_REPO) == SOURCE_DIGEST_AT_IMPORT


# --- 靶场口径（契约附录 C 冻结）---------------------------------------------
def test_fixture_is_red_on_the_expiry_case_before_patch(baseline):
    """打补丁前 1 挂 1 过：test_expired_session 挂、test_valid_session 过。"""
    _, report = baseline
    assert _case(report, "test_expired_session")["status"] == "failed"
    assert _case(report, "test_valid_session")["status"] == "passed"
    assert report["failed"] == 1 and report["errors"] == 0


def test_golden_patch_turns_the_suite_green(workdir, golden_patch):
    """打对补丁后全过 —— 这条同时证明 git apply 真落了盘、报告真反映了新代码。"""
    assert sandbox_git_apply(golden_patch, workdir) == {"ok": True, "error": None}

    report = sandbox_pytest_run(workdir)

    assert report["tool_error"] is None, report["tool_error"]
    assert report["failed"] == 0 and report["errors"] == 0
    assert _case(report, "test_expired_session")["status"] == "passed"


def test_report_shape_matches_the_frozen_test_report_artifact(baseline):
    """报告形状 = C-7 schema，直接落 test_report artifact 不需要再转一手。"""
    _, report = baseline
    assert validate_artifact(KIND_TEST_REPORT, report) == []


# --- 路径校验三条 -----------------------------------------------------------
def test_protected_segment_is_rejected(workdir):
    """受保护目录：复用 code_repo_patch 的 PROTECTED_SEGMENTS，分段相等。"""
    patch = {"files": [{"path": "tests/test_session.py",
                        "diff": _fabricate_diff("tests/test_session.py")}]}

    result = sandbox_git_apply(patch, workdir)

    assert result["ok"] is False
    assert result["error"]["stage"] == "path_check"
    assert result["error"]["path"] == "tests/test_session.py"


def test_conftest_patch_is_rejected(workdir):
    """任意层级的 conftest.py 禁改 —— 它在 collection 阶段先于一切用例执行。

    用**新增**一个深层 conftest.py 来试：`tests` 段挡不到 `pkg/deep/conftest.py`，
    所以这条如果只靠受保护目录清单，是漏的。
    """
    path = "pkg/deep/conftest.py"
    patch = {"files": [{"path": path, "diff": _fabricate_diff(path, new_file=True)}]}

    result = sandbox_git_apply(patch, workdir)

    assert result["ok"] is False
    error = result["error"]
    assert set(error) == {"stage", "path", "hunk", "message"}    # 结构完整
    assert error["stage"] == "conftest_guard"
    assert error["path"] == path
    assert error["message"]
    assert not (pathlib.Path(workdir) / path).exists()           # 一个字都没落盘


def test_path_escape_is_rejected(workdir):
    """内含性：规范化后必须落在 workdir 内。

    ../../../etc/passwd 的分段是 etc/passwd，不在 PROTECTED_SEGMENTS 里 ——
    只靠受保护目录清单它是放行的，这一层才有 workdir 可比对。
    """
    for path in ("../../../etc/passwd", "/etc/passwd", "../../../.ssh/id_rsa"):
        result = sandbox_git_apply({"files": [{"path": path, "diff": _fabricate_diff(path)}]},
                                   workdir)
        assert result["ok"] is False, path
        assert result["error"]["stage"] == "path_escape", path


def test_diff_body_path_is_validated_not_just_the_declared_path(workdir):
    """声明路径干净、diff 正文指向别处 —— 落盘的是正文那个，所以正文也要校验。"""
    patch = {"files": [{"path": SESSION_FILE,
                        "diff": _fabricate_diff("tests/test_session.py")}]}

    result = sandbox_git_apply(patch, workdir)

    assert result["ok"] is False
    assert result["error"]["stage"] == "path_check"


# --- reverse / check_only 两两组合 ------------------------------------------
def test_check_only_does_not_write_anything(workdir, golden_patch):
    """干跑闸：--check 只判能不能打，一个字节都不许落盘。"""
    before = _tree_digest(pathlib.Path(workdir))

    result = sandbox_git_apply(golden_patch, workdir, check_only=True)

    assert result == {"ok": True, "error": None}
    assert _tree_digest(pathlib.Path(workdir)) == before


def test_reverse_restores_the_file(workdir, golden_patch):
    """reverse=True 即补偿回滚：打完再反着打一遍，内容逐字节回到原样。"""
    target = pathlib.Path(workdir) / SESSION_FILE
    original = target.read_bytes()

    assert sandbox_git_apply(golden_patch, workdir)["ok"] is True
    assert target.read_bytes() != original

    assert sandbox_git_apply(golden_patch, workdir, reverse=True)["ok"] is True
    assert target.read_bytes() == original


def test_reverse_check_only_is_the_compensation_dry_run_gate(workdir, golden_patch):
    """reverse + check_only = Phase 4 的补偿干跑闸：能不能回滚，判了但不动手。

    没打过补丁的目录上干跑必须失败 —— 补偿闸靠这个「回滚不掉」的信号提前拦住，
    而不是等真回滚时把工作区搞成半吊子。
    """
    assert sandbox_git_apply(golden_patch, workdir, reverse=True, check_only=True)["ok"] is False

    assert sandbox_git_apply(golden_patch, workdir)["ok"] is True
    after_apply = _tree_digest(pathlib.Path(workdir))

    result = sandbox_git_apply(golden_patch, workdir, reverse=True, check_only=True)

    assert result == {"ok": True, "error": None}
    assert _tree_digest(pathlib.Path(workdir)) == after_apply       # 干跑没落盘


def test_apply_failure_reports_structured_error(workdir):
    """git apply 拒绝时，error 要带上 path 和 hunk —— Gate 逐条转 findings 靠它们。"""
    patch = {"files": [{"path": SESSION_FILE, "diff": _fabricate_diff(SESSION_FILE)}]}

    result = sandbox_git_apply(patch, workdir)

    assert result["ok"] is False
    error = result["error"]
    assert error["stage"] == "apply"
    assert error["path"] == SESSION_FILE
    assert error["hunk"] == "1"
    assert "patch failed" in error["message"]


# --- tool_error 与 failed 分报 ----------------------------------------------
def test_tool_error_and_failed_are_reported_separately(tmp_path):
    """pytest 根本起不来 → tool_error 非空、failed == 0。

    合成办法：给 workdir 塞一个 import 就抛的 conftest.py，pytest 在 collection
    之前就退（退出码 4），junit 报告压根不会产出。这时若把它当成「0 failed」
    报上去，Gate 会判成通过 —— 这正是两者必须分开上报的原因。
    """
    workdir = prepare_sandbox_workdir(str(tmp_path / "broken"))
    (pathlib.Path(workdir) / "conftest.py").write_text(
        'raise RuntimeError("conftest 炸了")\n', encoding="utf-8")

    report = sandbox_pytest_run(workdir)

    assert report["tool_error"], "pytest 没起来却没报 tool_error"
    assert report["failed"] == 0 and report["passed"] == 0 and report["errors"] == 0
    assert report["cases"] == []


def test_missing_workdir_is_a_tool_error(tmp_path):
    report = sandbox_pytest_run(str(tmp_path / "does-not-exist"))

    assert report["tool_error"]
    assert report["failed"] == 0


def test_real_case_failures_are_not_tool_errors(baseline):
    """反过来：用例真挂了不算工具失败 —— tool_error 必须是 None。"""
    _, report = baseline
    assert report["failed"] == 1
    assert report["tool_error"] is None


# --- skill 接线 -------------------------------------------------------------
def test_test_verify_registers_without_touching_init():
    """投放即注册（C-1）：builtin/__init__.py 里没有 test_verify 这个名字。"""
    cls = registry.get("test.verify")
    assert cls is not None and cls.contract.version == "1.0.0"
    assert "sandbox" in cls.contract.depends_tools

    init_src = (pathlib.Path(registry.__file__).parent / "builtin" / "__init__.py").read_text(
        encoding="utf-8")
    assert "test_verify" not in init_src


def test_test_verify_returns_the_report_instead_of_raising(workdir):
    """用例挂了是正常返回，不是 skill 失败 —— 抛出去 Gate 就分不出这两种情况。"""
    from maos.skills.contract import SkillContext

    skill = registry.get("test.verify")()
    report = skill.run({"workdir": workdir}, SkillContext())

    assert report["failed"] == 1
    assert report["tool_error"] is None
    assert validate_artifact(KIND_TEST_REPORT, report) == []
