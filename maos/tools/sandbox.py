"""沙箱工具层 —— 模型生成的代码只在这里落盘、只在这里执行。

两个 ToolPort（契约 C-7 冻结签名，一字不许改；上层按签名写代码）：
  · sandbox_git_apply  —— 补丁落盘，落盘前过三重路径校验
      reverse=True                    -> git apply -R        （Phase 4 补偿回滚）
      reverse=True, check_only=True   -> git apply -R --check（Phase 4 补偿干跑闸）
  · sandbox_pytest_run —— 在沙箱里跑 pytest，产出结构化 test_report

## 两条执行路径

**主路径是容器**：`--network none --read-only --user 1000:1000` + 内存/CPU/进程数限额。
容器天然不继承宿主环境变量，密钥隔离由此自动成立，不需要额外做什么。

**降级路径是裸 subprocess**（Docker 不可用时）。这条路径照抄
`maos/flows/common.py::_wrap_matrix` 的降级 idiom —— `log.warning` + 继续，不抛。
但它有一件事必须自己做：**env 白名单**。裸 subprocess 默认继承 `os.environ`，
模型生成的代码能直接把 `MAOS_LLM_API_KEY` 读走，而铁律 6 的出口脱敏管的是
落盘输出，管不到这个入口。所以降级路径重建一份干净 env，只放行 PATH / HOME / LANG，
且 HOME 指向一次性空目录（透传宿主 HOME 等于把 ~/.ssh 一并交出去）。

测试与 CI 永远走降级路径（`MAOS_SANDBOX_FORCE_SUBPROCESS=1`），保证无 Docker 环境可跑；
靠「碰巧没装 docker」来命中降级分支不算数，那样这条路径在装了 Docker 的机器上从不被测。

## tool_error 与 failed 是两件事

`tool_error` = 环境/工具炸了，根本没跑成；`failed` = 用例真挂了，跑成了但不过。
Gate 对这两种的判定完全不同（无报告即 blocker vs 逐条转 findings），
混在一起上层就判不出来。tool_error 非空时三个计数一律为 0。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from maos.tools.port import ToolPort

log = logging.getLogger("maos.tools.sandbox")

IMAGE = "maos-sandbox"
DEFAULT_TIMEOUT = 300

# 靶场源目录（maos/tools/sandbox.py -> 仓库根 -> scenarios/fixture-repo）
FIXTURE_REPO = Path(__file__).resolve().parents[2] / "scenarios" / "fixture-repo"

# 报告落在 workdir 里（容器 --read-only，但挂进来的 /w 是可写的），解析完即删。
# 用 junit xml 而不是解析 `pytest -q` 的文本输出：后者的格式随 pytest 版本变，
# 而这份报告要逐条喂进 Gate 的 findings，解析错等于把用例名喂错。
REPORT_NAME = ".maos-report.xml"

# -p no:cacheprovider：不写 .pytest_cache，跑完 workdir 与跑之前逐字节相同，
# 「干跑不落盘」和「宿主未被触碰」这两条断言才立得住。
PYTEST_ARGS = ("-q", "-p", "no:cacheprovider")

ENV_PASSTHROUGH = ("PATH", "LANG")

_GIT_TIMEOUT = 120

_CASE_STATUS = {"failure": "failed", "error": "error", "skipped": "skipped"}
_MSG_LIMIT = 1000

# git apply 的失败输出形如 `error: patch failed: auth/session.py:12`
_APPLY_FAILED = re.compile(r"error: patch failed: (?P<path>.+?):(?P<hunk>\d+)")
_APPLY_PATH = re.compile(r"error: (?P<path>[^:]+): (?:patch does not apply|No such file or directory)")


# ---------------------------------------------------------------------------
# 环境与工作目录
# ---------------------------------------------------------------------------
def sandbox_timeout() -> int:
    """`MAOS_SANDBOX_TIMEOUT`，默认 300 秒。非法值告警回退，不抛。"""
    raw = os.environ.get("MAOS_SANDBOX_TIMEOUT")
    if not raw:
        return DEFAULT_TIMEOUT
    try:
        value = int(raw)
    except (TypeError, ValueError):
        log.warning("MAOS_SANDBOX_TIMEOUT=%r 不是整数，回退 %ds", raw, DEFAULT_TIMEOUT)
        return DEFAULT_TIMEOUT
    if value <= 0:
        log.warning("MAOS_SANDBOX_TIMEOUT=%r 非正数，回退 %ds", raw, DEFAULT_TIMEOUT)
        return DEFAULT_TIMEOUT
    return value


def _clean_env(home: str) -> dict[str, str]:
    """白名单重建 env：只放行 PATH / LANG，HOME 指向一次性空目录，其余一律不传。

    HOME 不透传宿主的 —— 透传等于把 ~/.ssh、~/.aws、~/.gitconfig 一并交给
    模型生成的代码，靶场那条 test_no_home_access 探针也就白写了。容器主路径里
    HOME 是容器内的 /home/runner，这里对齐的是同一个语义，不是额外收紧。

    白名单是**按名字放行**，不是按名字拦截：新增一个 MAOS_LLM_TOKEN 之类的变量时，
    拦截清单要有人记得去加，放行清单不需要 —— 这就是这里不写黑名单的原因。
    """
    env = {key: os.environ[key] for key in ENV_PASSTHROUGH if os.environ.get(key)}
    env.setdefault("PATH", os.defpath)
    env.setdefault("LANG", "C.UTF-8")
    env["HOME"] = home
    return env


def _docker_ready() -> tuple[bool, str]:
    """容器主路径能不能走。返回 (可用, 不可用的原因)。"""
    if os.environ.get("MAOS_SANDBOX_FORCE_SUBPROCESS") == "1":
        return False, "MAOS_SANDBOX_FORCE_SUBPROCESS=1（测试与 CI 恒走降级路径）"
    if shutil.which("docker") is None:
        return False, "找不到 docker 命令"
    try:
        # image inspect 一次同时问了两件事：daemon 在不在、镜像有没有。
        probe = subprocess.run(["docker", "image", "inspect", IMAGE],
                               capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"docker 探测失败: {type(exc).__name__}: {exc}"
    if probe.returncode != 0:
        return False, f"镜像 {IMAGE} 不可用（先跑 docker build -t {IMAGE} -f deploy/sandbox.Dockerfile .）"
    return True, ""


def _git(cwd: str | os.PathLike, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, timeout=_GIT_TIMEOUT)


def prepare_sandbox_workdir(dest: str | None = None, *,
                            source: str | os.PathLike | None = None) -> str:
    """把演示靶场复制到一个干净的工作目录并 `git init` + 首次提交，返回其路径。

    补丁只打在这个副本上，宿主的 `scenarios/fixture-repo/` 永远不被触碰。

    `git init` 不是装饰：`git apply` 要有 work tree 才能可靠地打补丁与 `-R` 回滚，
    首次提交则让演示时 `git -C <workdir> log --oneline` 看得见真实的 apply 记录。
    提交时显式带 `-c user.name/-c user.email` —— 沙箱里没有全局 gitconfig，
    不带就会因为「请先配置身份」而失败。
    """
    src = Path(source) if source is not None else FIXTURE_REPO
    if not src.is_dir():
        raise FileNotFoundError(f"靶场目录不存在: {src}")

    workdir = Path(dest) if dest is not None else Path(tempfile.mkdtemp(prefix="maos-sb-"))
    workdir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        src, workdir, dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", ".git"),
    )

    _git(workdir, "init", "-q", "-b", "main")
    _git(workdir, "add", "-A")
    _git(workdir, "-c", "user.name=maos-sandbox", "-c", "user.email=sandbox@maos.local",
         "commit", "-q", "-m", "fixture baseline")
    return str(workdir)


# ---------------------------------------------------------------------------
# 路径校验（三条，一条都不能少）
# ---------------------------------------------------------------------------
def _error(stage: str, path: str | None, hunk: str | None, message: str) -> dict[str, Any]:
    """ok=False 时 error 必须结构化 —— Gate 要把 path 和 hunk 逐条转成 findings。"""
    return {"ok": False, "error": {"stage": stage, "path": path,
                                   "hunk": hunk, "message": message}}


def _strip_ab(path: str) -> str:
    for prefix in ("a/", "b/"):
        if path.startswith(prefix):
            return path[len(prefix):]
    return path


def _diff_targets(diff: str) -> list[str]:
    """从 diff 正文里抠出它真正要写的路径。

    只校验 `patch_set` 声明的 `path` 是不够的：声明写 `auth/session.py`、
    正文的 `+++ b/tests/test_session.py` 指向别处，落盘的是正文那个。
    这一层不看正文，前面那三条校验就全是摆设。
    """
    targets: list[str] = []
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            targets.extend(_strip_ab(p) for p in parts[2:4])
        elif line.startswith(("--- ", "+++ ")):
            raw = line[4:].strip().split("\t")[0]
            if raw and raw != "/dev/null":
                targets.append(_strip_ab(raw))
    return [t for t in targets if t]


def _protected_path_rules():
    """取受保护路径的判定件 —— 复用 code_repo_patch 那一处，不在这里抄第二份。

    抄一份到 tools 层，两处一定会漂，而漂的那次没人会发现，直到有人靠改测试
    让测试通过。`_path_segments` 带下划线仍然直接引：它做的四件归一（反斜杠、
    ./..、前导斜杠、casefold）每一件不做都是一个绕过口，重写一遍的风险远大于
    跨模块引一个私有名。

    **必须延迟到函数里 import**：tools 层在 skills 层下面，模块级 import 会成环 ——
    maos.tools.sandbox → skills.builtin.code_repo_patch → 触发 builtin/__init__ 的
    discover() → import test_verify → 回到还没定义完的 maos.tools.sandbox，
    在 PYTEST_RUN_PORT 上炸 ImportError。放进函数里，环在调用时才闭合，
    那时两边都已装载完。常量该不该下沉到 tools 层是另一回事，已记 BACKLOG。
    """
    from maos.skills.builtin.code_repo_patch import PROTECTED_SEGMENTS, _path_segments
    return PROTECTED_SEGMENTS, _path_segments


def _check_path(candidate: str, base: str) -> dict[str, Any] | None:
    """三条校验，命中任一条返回结构化错误；全过返回 None。"""
    protected_segments, path_segments = _protected_path_rules()
    segments = path_segments(candidate)
    if not segments:
        return _error("path_check", candidate, None, "补丁路径为空或无法解析")

    # 1) 受保护目录：复用 code_repo_patch 的 PROTECTED_SEGMENTS，分段相等。
    #    清单里存的是**裸目录名**，不带斜杠 —— 写成 "tests/" 在分段相等下
    #    永远匹配不上，不报错只放行，那正是上一轮修掉的失效形态。
    hit = protected_segments.intersection(segments)
    if hit:
        return _error("path_check", candidate, None,
                      f"触碰受保护目录 {sorted(hit)}：路径按 / 分段后任一段命中即拒")

    # 2) conftest.py 任意层级禁改。`tests` 段只挡 tests/ 目录**下**的文件，
    #    仓库根或任意非 tests 目录下的 conftest.py 一律放行，而它在 collection
    #    阶段先于一切用例执行 —— 这是绕过「tests/ 禁改」的标准路径。
    if segments[-1] == "conftest.py":
        return _error("conftest_guard", candidate, None,
                      "conftest.py 任意层级禁改：它在 pytest collection 阶段先于一切"
                      "用例执行，改它等于绕开 tests/ 禁改")

    # 3) 内含性：补丁路径规范化后必须落在 workdir 内。
    #    /etc/passwd、../../../.ssh/id_rsa 规范化后的分段是 etc/passwd、.ssh/id_rsa，
    #    都不在 PROTECTED_SEGMENTS 里 —— skill 层没有 workdir 可比对，这一层才有。
    target = os.path.realpath(os.path.join(base, candidate))
    if target != base and not target.startswith(base + os.sep):
        return _error("path_escape", candidate, None,
                      f"补丁路径规范化后落在 workdir 之外: {target}")
    return None


# ---------------------------------------------------------------------------
# ToolPort 1 · 补丁应用
# ---------------------------------------------------------------------------
def sandbox_git_apply(
    patch_set: dict[str, Any],
    workdir: str,
    *,
    reverse: bool = False,
    check_only: bool = False,
) -> dict[str, Any]:
    """在沙箱工作目录应用补丁集。

    reverse=True 走 git apply -R（补偿回滚）；check_only=True 加 --check（只干跑不落盘）。
    两者同时为 True 就是 phase-4.md 第 3 步那道补偿干跑闸。

    返回 {"ok": bool, "error": {"stage", "path", "hunk", "message"} | None}。
    ok=False 时 error 必须结构化 —— Gate 要把 path 和 hunk 逐条转成 findings 喂回 Coding。

    stage 取值：validate（补丁集本身不合法）/ prepare（workdir 不可用）/
    path_check（受保护目录）/ conftest_guard / path_escape / apply（git 拒绝）。
    hunk 是 git 自己报的行号，只在 apply 阶段有值。
    """
    files = patch_set.get("files") if isinstance(patch_set, dict) else None
    if not isinstance(files, list) or not files:
        return _error("validate", None, None, "补丁集为空，或 files 不是非空 list")

    base = os.path.realpath(workdir)
    if not os.path.isdir(base):
        return _error("prepare", str(workdir), None, f"workdir 不存在或不是目录: {workdir}")

    chunks: list[str] = []
    for index, item in enumerate(files):
        if (not isinstance(item, dict) or not isinstance(item.get("path"), str)
                or not isinstance(item.get("diff"), str)):
            return _error("validate", None, None, f"files[{index}] 缺少合法的 path/diff 字段")
        diff = item["diff"]
        for candidate in [item["path"], *_diff_targets(diff)]:
            failure = _check_path(candidate, base)
            if failure is not None:
                return failure
        chunks.append(diff if diff.endswith("\n") else diff + "\n")

    cmd = ["git", "apply", "--whitespace=nowarn"]
    if reverse:
        cmd.append("-R")
    if check_only:
        cmd.append("--check")

    try:
        proc = subprocess.run(cmd, cwd=base, input="".join(chunks),
                              capture_output=True, text=True, timeout=_GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        return _error("apply", None, None, f"git apply 起不来: {type(exc).__name__}: {exc}")

    if proc.returncode != 0:
        text = (proc.stderr or proc.stdout or "").strip()
        matched = _APPLY_FAILED.search(text)
        if matched:
            return _error("apply", matched.group("path"), matched.group("hunk"), text)
        fallback = _APPLY_PATH.search(text)
        return _error("apply", fallback.group("path") if fallback else None, None,
                      text or f"git apply 退出码 {proc.returncode}，但没有输出")
    return {"ok": True, "error": None}


# ---------------------------------------------------------------------------
# ToolPort 2 · 测试执行
# ---------------------------------------------------------------------------
def _tool_error_report(message: str, started: float) -> dict[str, Any]:
    """工具层炸了：三个计数一律 0，failed=0 才不会被 Gate 当成「用例挂了」。"""
    return {"passed": 0, "failed": 0, "errors": 0, "cases": [],
            "duration": round(time.perf_counter() - started, 3), "tool_error": message}


def _classify_exit(proc: subprocess.CompletedProcess) -> str | None:
    """按 pytest 退出码分「跑成了」和「没跑成」。

    0=全过、1=有用例挂 —— 这两种都跑成了，交给报告去分。其余（2 中断 /
    3 内部错 / 4 用法错 / 5 一条用例都没收集到）是工具层炸了，必须走 tool_error：
    把「没收集到用例」当成 0 failed 报上去，Gate 会判成通过。
    """
    if proc.returncode in (0, 1):
        return None
    tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-20:]
    return f"pytest 退出码 {proc.returncode}（不是用例失败）: " + " | ".join(tail)


def _run_in_container(base: str) -> str | None:
    """容器主路径。返回 tool_error 文本，或 None 表示跑成了。"""
    name = f"maos-sb-{uuid.uuid4().hex[:12]}"
    cmd = [
        "docker", "run", "--rm", "--name", name,
        "--network", "none", "--read-only",
        "-v", f"{base}:/w", "--tmpfs", "/tmp", "-w", "/w",
        "--user", "1000:1000",
        "--memory", "512m", "--cpus", "1", "--pids-limit", "128",
        IMAGE, "python", "-m", "pytest", *PYTEST_ARGS, f"--junitxml=/w/{REPORT_NAME}",
    ]
    timeout = sandbox_timeout()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        # --rm 只在容器自己退出时清场，超时这条路径必须自己动手 —— 这就是上面
        # 非要自己生成 --name 的原因，靠 --rm 兜不住，容器会一直挂在那里占资源。
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)
        return f"沙箱执行超时（{timeout}s），容器 {name} 已强制清除"
    except (OSError, subprocess.SubprocessError) as exc:
        return f"docker run 起不来: {type(exc).__name__}: {exc}"
    return _classify_exit(proc)


def _run_degraded(base: str, report: Path) -> str | None:
    """降级路径。返回 tool_error 文本，或 None 表示跑成了。"""
    home = tempfile.mkdtemp(prefix="maos-sb-home-")
    timeout = sandbox_timeout()
    cmd = [sys.executable, "-m", "pytest", *PYTEST_ARGS, f"--junitxml={report}"]
    try:
        proc = subprocess.run(cmd, cwd=base, capture_output=True, text=True,
                              timeout=timeout, env=_clean_env(home))
    except subprocess.TimeoutExpired:
        return f"沙箱执行超时（{timeout}s，降级路径）"
    except (OSError, subprocess.SubprocessError) as exc:
        return f"pytest 起不来: {type(exc).__name__}: {exc}"
    finally:
        shutil.rmtree(home, ignore_errors=True)
    return _classify_exit(proc)


def _parse_junit(report: Path) -> dict[str, Any]:
    root = ElementTree.parse(report).getroot()
    passed = failed = errors = 0
    cases: list[dict[str, str]] = []
    for node in root.iter("testcase"):
        case_id = "::".join(part for part in (node.get("classname"), node.get("name")) if part)
        status, msg = "passed", ""
        for child in node:
            if child.tag in _CASE_STATUS:
                status = _CASE_STATUS[child.tag]
                msg = (child.get("message") or child.text or "").strip()[:_MSG_LIMIT]
                break
        cases.append({"id": case_id, "status": status, "msg": msg})
        if status == "passed":
            passed += 1
        elif status == "failed":
            failed += 1
        elif status == "error":
            errors += 1
        # skipped 三个计数都不进 —— 它既不是通过也不是失败，只留在 cases 里。
    return {"passed": passed, "failed": failed, "errors": errors, "cases": cases}


def sandbox_pytest_run(workdir: str) -> dict[str, Any]:
    """在沙箱里跑 pytest，产出结构化测试报告。

    返回 test_report：
      {"passed": int, "failed": int, "errors": int,
       "cases": [{"id": str, "status": str, "msg": str}],
       "duration": float, "tool_error": str | None}

    tool_error 与 failed 必须分开上报：前者是环境或工具炸了（根本没跑成），
    后者是用例真的挂了（跑成了但不过）。Gate 对这两种的判定不一样。

    duration 记的是墙钟耗时（含容器启停），不是 junit 里的用例执行时间和 ——
    上层要用它判「沙箱是不是慢到该调超时」，那需要的正是墙钟。
    """
    started = time.perf_counter()
    base = os.path.realpath(workdir)
    if not os.path.isdir(base):
        return _tool_error_report(f"workdir 不存在或不是目录: {workdir}", started)

    report = Path(base) / REPORT_NAME
    report.unlink(missing_ok=True)

    usable, why = _docker_ready()
    if usable:
        failure = _run_in_container(base)
    else:
        # 降级 idiom 照抄 flows/common.py::_wrap_matrix：告警 + 继续，不抛。
        log.warning("容器沙箱不可用（%s），降级为裸 subprocess；env 已按白名单重建，"
                    "宿主密钥与 HOME 都不进沙箱", why)
        failure = _run_degraded(base, report)

    try:
        if failure is not None:
            return _tool_error_report(failure, started)
        if not report.is_file():
            return _tool_error_report("pytest 没有产出 junit 报告，多半根本没跑起来", started)
        try:
            parsed = _parse_junit(report)
        except ElementTree.ParseError as exc:
            return _tool_error_report(f"junit 报告解析失败: {exc}", started)
    finally:
        # 解析完就删：报告是本次调用的中间产物，留着会让「干跑不落盘」
        # 和「跑完 workdir 逐字节不变」这两条断言失效。
        report.unlink(missing_ok=True)

    parsed["duration"] = round(time.perf_counter() - started, 3)
    parsed["tool_error"] = None
    return parsed


# ---------------------------------------------------------------------------
# 两个 ToolPort 声明（A-6 九要素）—— 调用一律走 invoke_tool()，直接调没有审计行
# ---------------------------------------------------------------------------
GIT_APPLY_PORT = ToolPort(
    name="sandbox.git_apply",
    purpose="在沙箱工作目录内应用或回滚补丁集，落盘前完成三重路径校验",
    entry=sandbox_git_apply,
    params_schema={"patch_set": "dict", "workdir": "str",
                   "reverse": "bool（keyword-only）", "check_only": "bool（keyword-only）"},
    returns_schema={"ok": "bool", "error": "{stage,path,hunk,message} | None"},
    failure_modes=[
        "validate: 补丁集为空或 files 项缺 path/diff",
        "prepare: workdir 不存在或不是目录",
        "path_check: 触碰 infra/.github/secrets/tests 任一段",
        "conftest_guard: 任意层级的 conftest.py 新增或修改",
        "path_escape: 规范化后落在 workdir 之外",
        "apply: git apply 拒绝（error.hunk 带 git 报的行号）",
    ],
    security_boundary=(
        "补丁只落在传入的 workdir 内；声明路径与 diff 正文里的路径都要过三条校验"
        "（受保护目录分段相等 / conftest.py 任意层级禁改 / workdir 内含性），"
        "任一条不过即拒，不重试、不降级"
    ),
    rate_limit="",
    owner="task-b",
)

PYTEST_RUN_PORT = ToolPort(
    name="sandbox.pytest_run",
    purpose="在容器沙箱里跑 workdir 的测试，产出结构化 test_report",
    entry=sandbox_pytest_run,
    params_schema={"workdir": "str"},
    returns_schema={"passed": "int", "failed": "int", "errors": "int",
                    "cases": "list[{id,status,msg}]", "duration": "float",
                    "tool_error": "str | None"},
    failure_modes=[
        "tool_error: workdir 不可用 / docker run 起不来 / 超时（容器已强制清除）",
        "tool_error: pytest 退出码 ≥2（中断、内部错、用法错、零用例收集）",
        "tool_error: 没产出 junit 报告或报告解析失败",
        "failed>0: 用例真的挂了 —— 这不是工具失败，Gate 逐条转 findings",
    ],
    security_boundary=(
        "主路径容器：--network none --read-only --user 1000:1000 --memory 512m "
        "--cpus 1 --pids-limit 128，不继承宿主 env；降级路径裸 subprocess，"
        "env 按白名单重建（只放行 PATH/LANG，HOME 指向一次性空目录）；"
        "超时由宿主侧 MAOS_SANDBOX_TIMEOUT（默认 300s）兜底并 docker rm -f 清场"
    ),
    rate_limit="",
    owner="task-b",
)
