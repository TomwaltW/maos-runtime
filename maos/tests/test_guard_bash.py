"""守卫 scripts/guard_bash.py 的行为契约。

退出码约定：0 = 放行，2 = 拦截。全部用真实子进程喂 stdin，测的是 hook
实际跑起来的效果，不是内部函数的返回值。
"""
import json, os, pathlib, subprocess, sys
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
GUARD = ROOT / "scripts" / "guard_bash.py"

ALLOW, BLOCK = 0, 2


def run_guard(payload, relock=False):
    env = dict(os.environ)
    env.pop("MAOS_RELOCK", None)
    if relock:
        env["MAOS_RELOCK"] = "1"
    return subprocess.run(
        [sys.executable, str(GUARD)],
        input=payload if isinstance(payload, str) else json.dumps(payload),
        capture_output=True, text=True, env=env, cwd=str(ROOT),
    )


def bash(cmd, description="run a command"):
    return {"tool_name": "Bash",
            "tool_input": {"command": cmd, "description": description}}


# ------------------------------------------------- 1. 分段绕过必须被拦住

BYPASS = [
    pytest.param('cat > maos/con"tracts"/events.py', id="引号拆分路径"),
    pytest.param("P=maos/contracts; printf x > $P/events.py", id="变量拼接"),
    pytest.param("cp /tmp/x maos/contr*/events.py", id="通配符展开"),
    pytest.param("sed -i '' 's/a/b/' ./maos//contracts/events.py", id="冗余分隔符"),
    pytest.param("python3 -c \"open('maos/contracts/events.py','w').write('')\"",
                 id="解释器内联代码"),
    pytest.param("rm scripts/g''uard_bash.py", id="空引号拆分"),
    pytest.param('echo x > "$(pwd)/.contracts.lock"', id="命令替换"),
    pytest.param('cat scripts/guard"_"bash.py', id="读守卫自身"),
    pytest.param("echo ok\nrm maos/contracts/states.py", id="换行藏第二条命令"),
    pytest.param("tee maos/contracts/states.py < /dev/null", id="tee 写入"),
    pytest.param("mv scripts/guard_bash.py /tmp/", id="搬走守卫"),
    pytest.param("chmod 000 scripts/guard_bash.py", id="改守卫权限"),
    pytest.param("git checkout -- maos/contracts/events.py", id="git 写子命令"),
    pytest.param('echo "unbalanced', id="引号不闭合"),
    pytest.param("awk '{print > \"maos/contracts/events.py\"}' x", id="awk 内重定向"),
]


@pytest.mark.parametrize("cmd", BYPASS)
def test_bypass_is_blocked(cmd):
    proc = run_guard(bash(cmd))
    assert proc.returncode == BLOCK, (
        f"绕过没被拦住：{cmd!r}\nstdout={proc.stdout!r} stderr={proc.stderr!r}")
    assert "blocked" in proc.stderr


# ------------------------------------------------- 2. 正常命令不许误伤

BENIGN_BASH = [
    pytest.param('echo "guard_bash 是受保护的" >> docs/notes.md', id="echo 提到名字"),
    pytest.param("grep -l guard_bash -r .", id="grep 搜名字"),
    pytest.param("grep -rn MAOS_RELOCK maos", id="grep 搜授权变量"),
    pytest.param("python3 -m pytest maos/tests -q", id="跑测试"),
    pytest.param("git diff", id="git diff"),
    pytest.param("git status --short", id="git status"),
    pytest.param("git log --oneline -5", id="git log"),
    pytest.param("cat maos/contracts/events.py", id="cat 契约"),
    pytest.param("head -50 maos/contracts/states.py", id="head 契约"),
    pytest.param("ls -la scripts/", id="ls 目录"),
    pytest.param("python3 run.py", id="跑入口"),
    pytest.param("echo $MAOS_RELOCK", id="只读授权变量"),
    pytest.param("mkdir -p evidence/phase-1 && touch evidence/phase-1/out.txt",
                 id="正常写别处"),
    pytest.param("sed -n '1,20p' maos/contracts/events.py", id="sed 无 -i 读契约"),
]


@pytest.mark.parametrize("cmd", BENIGN_BASH)
def test_benign_bash_is_allowed(cmd):
    proc = run_guard(bash(cmd))
    assert proc.returncode == ALLOW, (
        f"正常命令被误伤：{cmd!r}\nstderr={proc.stderr!r}")


BENIGN_TOOLS = [
    pytest.param({"tool_name": "Write", "tool_input": {
        "file_path": "docs/notes.md",
        "content": "本文讲 scripts/guard_bash.py 与 MAOS_RELOCK 的关系"}},
        id="Write 文档提到受保护名字"),
    pytest.param({"tool_name": "Edit", "tool_input": {
        "file_path": "docs/notes.md",
        "old_string": "maos/contracts/events.py", "new_string": "y"}},
        id="Edit 正文提到契约路径"),
    pytest.param({"tool_name": "Grep", "tool_input": {
        "pattern": "MAOS_RELOCK", "path": "maos"}}, id="Grep pattern 是受保护名"),
    pytest.param({"tool_name": "Grep", "tool_input": {
        "pattern": "guard_bash", "path": "."}}, id="Grep 全仓搜名字"),
    pytest.param({"tool_name": "Glob", "tool_input": {"pattern": "**/*.py"}},
                 id="Glob 宽通配"),
    pytest.param({"tool_name": "WebFetch", "tool_input": {
        "url": "https://x/guard_bash.py"}}, id="matcher 外的工具"),
]


@pytest.mark.parametrize("payload", BENIGN_TOOLS)
def test_benign_tools_are_allowed(payload):
    proc = run_guard(payload)
    assert proc.returncode == ALLOW, f"误伤：{payload}\nstderr={proc.stderr!r}"


def test_bash_description_is_not_consulted():
    """description 是给人看的说明，不该参与判定。"""
    proc = run_guard(bash("ls -la", description="顺手改 scripts/guard_bash.py"))
    assert proc.returncode == ALLOW, proc.stderr


# ------------------------------------------- 3. READ_OK：契约文件允许 Read

@pytest.mark.parametrize("path", [
    "maos/contracts/events.py",
    "maos/contracts/states.py",
    str(ROOT / "maos" / "contracts" / "events.py"),   # 绝对路径同样放行
])
def test_read_contract_allowed(path):
    proc = run_guard({"tool_name": "Read", "tool_input": {"file_path": path}})
    assert proc.returncode == ALLOW, f"契约 Read 被误拦：{path}\n{proc.stderr!r}"


# ------------------------- 4. 自我保护 + 受保护面的写入 / 越权读取仍被拦

PROTECTED_TOOLS = [
    pytest.param({"tool_name": "Write", "tool_input": {
        "file_path": "scripts/guard_bash.py", "content": "x"}}, id="Write 守卫自身"),
    pytest.param({"tool_name": "Edit", "tool_input": {
        "file_path": str(ROOT / "scripts" / "guard_bash.py"),
        "old_string": "a", "new_string": "b"}}, id="Edit 守卫自身（绝对路径）"),
    pytest.param({"tool_name": "Read", "tool_input": {
        "file_path": "scripts/guard_bash.py"}}, id="Read 守卫自身"),
    pytest.param({"tool_name": "Read", "tool_input": {
        "file_path": ".claude/settings.json"}}, id="Read 本配置"),
    pytest.param({"tool_name": "Grep", "tool_input": {
        "pattern": "x", "path": "scripts/guard_bash.py"}}, id="Grep 守卫自身"),
    pytest.param({"tool_name": "Write", "tool_input": {
        "file_path": "maos/contracts/events.py", "content": "x"}}, id="Write 契约"),
    pytest.param({"tool_name": "Write", "tool_input": {
        "file_path": ".contracts.lock", "content": "x"}}, id="Write 指纹锁"),
    pytest.param({"tool_name": "NotebookEdit", "tool_input": {
        "notebook_path": "maos/contracts/events.py", "new_source": "x"}},
        id="NotebookEdit 契约"),
]


@pytest.mark.parametrize("payload", PROTECTED_TOOLS)
def test_protected_surface_is_blocked(payload):
    proc = run_guard(payload)
    assert proc.returncode == BLOCK, f"没拦住：{payload}\nstderr={proc.stderr!r}"


# ------------------------------------------ 5. MAOS_RELOCK 授权路径不变

@pytest.mark.parametrize("cmd", BYPASS)
def test_relock_env_bypasses_everything(cmd):
    """授权变量在时早退放行 —— 语义与加固前一致。"""
    proc = run_guard(bash(cmd), relock=True)
    assert proc.returncode == ALLOW, f"授权后仍被拦：{cmd!r}\n{proc.stderr!r}"


def test_relock_env_allows_protected_tools():
    proc = run_guard({"tool_name": "Write", "tool_input": {
        "file_path": "maos/contracts/events.py", "content": "x"}}, relock=True)
    assert proc.returncode == ALLOW


def test_relock_script_needs_authorization():
    cmd = bash("python3 scripts/relock_contracts.py")
    assert run_guard(cmd).returncode == BLOCK            # 无授权：执行位置，拦
    assert run_guard(cmd, relock=True).returncode == ALLOW


def test_relock_script_via_cd_is_blocked():
    """裸名调用也要认出来。"""
    assert run_guard(bash("cd scripts && python3 relock_contracts.py")
                     ).returncode == BLOCK


# ------------------------------------------------- 6. fail-closed 语义

FAIL_CLOSED = [
    pytest.param("MAOS_RELOCK=1 python3 x.py", id="内联赋值授权变量"),
    pytest.param("export MAOS_RELOCK=1", id="导出授权变量"),
    pytest.param("unset MAOS_RELOCK", id="清除授权变量"),
    pytest.param("foobar --out maos/contracts/events.py", id="未知命令带受保护路径"),
    pytest.param("xxd scripts/guard_bash.py", id="未知只读工具（已知代价）"),
]


@pytest.mark.parametrize("cmd", FAIL_CLOSED)
def test_fail_closed_bash(cmd):
    assert run_guard(bash(cmd)).returncode == BLOCK, f"应 fail-closed：{cmd!r}"


def test_invalid_stdin_is_blocked():
    """守卫自身出错必须拦，不能变成静默放行。"""
    proc = run_guard("这不是 JSON")
    assert proc.returncode == BLOCK
    assert "guard internal error" in proc.stderr


def test_empty_stdin_is_blocked():
    proc = run_guard("")
    assert proc.returncode == BLOCK
    assert "guard internal error" in proc.stderr
