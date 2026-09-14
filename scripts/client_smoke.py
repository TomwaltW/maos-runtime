#!/usr/bin/env python3
"""客户路径验收 —— 把「解压→双击」变成一条能反复跑的判据。

    python3 scripts/client_smoke.py                # 打客户包、解压、逐条断言
    python3 scripts/client_smoke.py --zip <path>   # 验一个已经打好的包（跳过打包）
    python3 scripts/client_smoke.py --keep         # 留下解压目录便于排查

## 这个脚本在守什么

客户拿到的是**一个解压出来的目录**：没有仓库上下文、没有 worktree、没有人在旁边
解释「那条命令要先 cd 到哪」。「双击就能用」这件事之所以需要机器判据，是因为它
**只会在人不看的时候坏掉** —— 某一轨顺手往 `START-HERE.html` 里加了一条 CDN 链接，
断网的评委那一侧就白屏；`make_release.sh` 的裁剪清单漏一条，内部派单就跟着进包。
这两种坏法都不会让任何现有测试变红。

所以本脚本的纪律与 `scripts/make_release.sh` §4 同一条：**所有断言都在解压目录里做，
不许 cd 回仓库取巧**。仓库里 `evidence/report.html` 存在，不代表客户包里存在。

## 三个退出码，各说一件事

    0  全部判据都跑了，全过
    3  **有前置产物还没并进来**，依赖它的判据报「待产出」而不是崩
    1  跑了并且不过 —— 这才是真回归

3 和 1 分开沿用 `scripts/ap_smoke.py` 的惯例，理由也一样：客户路径由六轨并行建
（T148 入口页 / T149 一键脚本 / T150 裁剪打包 / T151 客户说明书 / T153 证据出处 /
本轨验收），整合前「还没装好」是常态，「装好了但坏了」是事故。两者共用 exit 1
就没人看了。

## Windows 验不了就是验不了

本脚本跑在 macOS/Linux 上。`RUN-ME.bat` 的**真机行为** —— Windows 自带解压器会不会
吞掉可执行位与目录层级、GBK 控制台下中文怎么显示、`py -3` 探测在没装 launcher 的机器
上会怎样 —— 这些**做不到就是做不到**，本脚本一个字都不声称。

它只做 `.bat` 的**静态可验证项**：行尾是不是 CRLF、有没有 `%~dp0`、Python 探测顺序里
有没有 `py -3`、文件名是不是全 ASCII。这些判据在本机成立就在 Windows 上也成立，
因为它们判的是**文件的字节**，不是运行时行为。

输出里静态项一律打 `[static]` 而不是 `[ok]`，`docs/client-smoke-report.md` 按同一口径
分栏。这是全局铁律 3（证据必须真实）在这一轨的具体形状。
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# 前置产物登记表
#
# 按**约定的文件名**登记，不按「谁合了没」登记 —— 本脚本探到即用、探不到即报
# 「待产出」并点名是哪一轨，整合期五轨合完后同一个脚本一个字不用改就能跑满。
# INTEGRATION-POINT: 这张表在五轨并入后不需要改。
# ---------------------------------------------------------------------------
ENTRY_FILES: dict[str, str] = {
    "START-HERE.html": "T148",
    "RUN-ME.command": "T149",
    "RUN-ME.bat": "T149",
    "README-CLIENT.md": "T151",
}

#: 客户包里**不该出现**的内部物料。前四条是目录，后三条是文件。
#: 判定按「条目路径以它开头 / 等于它」做，不做模糊匹配 —— 模糊匹配会把
#: `docs/phases-of-the-moon.md` 这种无辜路径也算进去，让判据说不清话。
INTERNAL_PATHS: tuple[str, ...] = (
    "docs/phases/",
    "legacy-ts/",
    "review/",
    "docs/BACKLOG.md",
    "docs/DECISIONS.md",
    "CLAUDE.md",
)

#: C 组终点值。写死成 10/10 是**目标值**，不是当前基线的实测值 ——
#: 派单实测：全新 clone 当前主干跑 README ①② 得 `RESULT: 9/10 PASS`（provenance 4/16），
#: 根因是证据出处 sha 的自指悖论，T153 正在修。此项在 T153 落地前红属预期。
EXPECT_RESULT = "RESULT: 10/10 PASS"

#: C2 失败路径要把 Python 藏掉。藏法是**换掉 PATH**，只链进这些基础工具。
#: 不链 `python`/`python3`/`py`，于是 `command -v python3` 一定落空。
#: 局限写在这里而不是藏着：脚本若**硬编码** `/usr/bin/python3` 这类绝对路径，
#: 换 PATH 拦不住 —— 那种写法本身就是 T149 的 bug，本脚本会在 C2 的备注里点出来。
FAKE_BIN_TOOLS: tuple[str, ...] = (
    "sh", "bash", "env", "grep", "sed", "awk", "cat", "ls", "dirname", "basename",
    "uname", "printf", "echo", "tr", "head", "tail", "mktemp", "rm", "mkdir",
    "chmod", "date", "sort", "wc", "expr", "test", "true", "false", "sleep",
    "open", "tput", "stty", "id", "whoami", "find", "xargs", "cut", "tee",
)

OK, FAIL, PENDING, STATIC = "ok", "FAIL", "PENDING", "static"


@dataclass
class Result:
    """一条判据的判决。``note`` 是给人看的、``owner`` 是待产出时点名到哪一轨。"""

    key: str
    title: str
    status: str
    note: str = ""
    owner: str = ""
    extra: list[str] = field(default_factory=list)


class Report:
    """判决收集器。打印即判决 —— 不留「算过了但没说」的中间态。"""

    def __init__(self) -> None:
        self.results: list[Result] = []

    def add(self, key: str, title: str, status: str, note: str = "",
            owner: str = "", extra: list[str] | None = None) -> Result:
        r = Result(key, title, status, note, owner, list(extra or []))
        self.results.append(r)
        tag = {OK: "[ok]     ", FAIL: "[FAIL]   ",
               PENDING: "[PENDING]", STATIC: "[static] "}[status]
        suffix = f" —— {note}" if note else ""
        if status == PENDING and owner:
            suffix = f" —— 待产出（{owner}）" + (f"：{note}" if note else "")
        print(f"  {tag} {key} {title}{suffix}")
        for line in r.extra:
            print(f"            · {line}")
        return r

    def ok(self, key: str, title: str, note: str = "", **kw) -> Result:
        return self.add(key, title, OK, note, **kw)

    def fail(self, key: str, title: str, note: str, **kw) -> Result:
        return self.add(key, title, FAIL, note, **kw)

    def pending(self, key: str, title: str, owner: str, note: str = "") -> Result:
        return self.add(key, title, PENDING, note, owner=owner)

    def static(self, key: str, title: str, note: str = "", **kw) -> Result:
        return self.add(key, title, STATIC, note, **kw)

    def count(self, status: str) -> int:
        return sum(1 for r in self.results if r.status == status)


# ---------------------------------------------------------------------------
# 0. 打包
# ---------------------------------------------------------------------------
def supports_client_flag() -> bool:
    """`make_release.sh` 认不认 `--client`。

    按**脚本自己的字节**探，不按「T150 合没合」探：整合期最不该依赖的就是
    「我以为它合了」。探不到就退化成内部包，并把 A3/A4 标为待产出。
    """
    script = ROOT / "scripts" / "make_release.sh"
    if not script.exists():
        return False
    return "--client" in script.read_text(encoding="utf-8", errors="replace")


def build_package(rep: Report) -> tuple[Path | None, str]:
    """打一个包出来，返回 ``(zip 路径, kind)``；``kind`` 是 ``client`` 或 ``internal``。

    打包时一律带 `--no-verify`：包内的 pytest / ①② 由 C 组在解压目录里自己跑一遍，
    让 `make_release.sh` 再跑一遍等于把同一件事做两次、把本脚本的耗时翻倍。
    """
    script = ROOT / "scripts" / "make_release.sh"
    if not script.exists():
        rep.fail("A0", "打包脚本存在", f"找不到 {script.relative_to(ROOT)}")
        return None, "none"

    is_client = supports_client_flag()
    if is_client:
        argv = ["bash", str(script), "--client", "--no-verify"]
        kind = "client"
    else:
        argv = ["bash", str(script), "--no-verify"]
        kind = "internal"
        rep.pending("A0", "make_release.sh 支持 --client", "T150",
                    "脚本里搜不到 --client，本次退化为内部包；A3/A4 随之待产出")

    print(f"==> 打包：{' '.join(argv[1:])}")
    started = time.time()
    proc = subprocess.run(argv, cwd=str(ROOT), capture_output=True, text=True)
    for ln in [x for x in proc.stdout.splitlines() if x.strip()][-6:]:
        print(f"    | {ln}")

    # 只认**这次**产出的包。dist/ 是 gitignore 的暂存地，里面很可能躺着上一轮的
    # 陈旧 zip；拿它当本次读数，报告就会在「验的是哪个包」上说假话。
    dist = ROOT / "dist"
    fresh = [p for p in dist.glob("*.zip")
             if dist.is_dir() and p.stat().st_mtime >= started - 1]
    zip_path = max(fresh, key=lambda p: p.stat().st_mtime) if fresh else None

    label = "打客户包" if is_client else "打包"
    if proc.returncode != 0:
        # `make_release.sh` 的排除项自查与密钥自查都排在 **zip 产出之后**，
        # 所以它 exit≠0 时包往往已经躺在 dist/ 里了。把包捡起来继续验：
        # 一条闸红掉就把后面全部判据带走，等于一次跑只能看见一个问题 ——
        # 「可重复执行的判据」的价值正在于一次跑出全部读数。
        reasons = [ln.strip() for ln in proc.stdout.splitlines() if "[FAIL]" in ln]
        rep.fail("A0", label, f"make_release.sh exit={proc.returncode}",
                 extra=reasons[:5] or [ln for ln in proc.stderr.splitlines()[-5:] if ln.strip()])
        if zip_path is None:
            return None, kind
        print(f"    （包已产出：{zip_path.name} —— 后续判据照跑，A0 照红）")
        return zip_path, kind

    if zip_path is None:
        rep.fail("A0", label, "exit=0 但 dist/ 下没有本次产出的 zip")
        return None, kind
    rep.ok("A0", label, zip_path.name)
    return zip_path, kind


def extract(zip_path: Path, dest: Path) -> Path | None:
    """解压并返回包根目录。

    包根按 zip 里**唯一的顶层目录**探，不按名字硬编码 —— 客户包的目录名归 T150 定
    （可能是 `maos-client-<sha>` 也可能沿用 `maos-runtime-<sha>`），
    把名字写死等于给自己埋一颗整合期才炸的雷。
    """
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(dest)
        # `extractall` **不还原 Unix 权限位**，而 macOS 的归档实用工具、Linux 的
        # `unzip` 都会还原。不补这一步，C1 就永远走不到「双击」那条路 ——
        # 它会因为没有执行位而退回 `bash <file>`，于是 T149 哪天把可执行位弄丢了，
        # C1 照样绿。判据要跟客户的处境一致，就得把位补回来。
        # 目录留到最后 chmod：先改目录权限可能把自己关在外面（0o555 的目录写不进文件）。
        for info in sorted(zf.infolist(), key=lambda i: i.is_dir()):
            mode = (info.external_attr >> 16) & 0o777
            if not mode:
                continue
            target = dest / info.filename
            if target.exists():
                os.chmod(target, mode)
    tops = [p for p in dest.iterdir() if not p.name.startswith(".")]
    if len(tops) == 1 and tops[0].is_dir():
        return tops[0]
    return dest if tops else None


# ---------------------------------------------------------------------------
# A. 包的结构
# ---------------------------------------------------------------------------
def read_entries(zip_path: Path) -> tuple[list[zipfile.ZipInfo], str]:
    """读 zip 条目表，并算出包根前缀。

    A2 / W4 都要判**包的字节**而不是解压产物（zipfile 既不还原权限位、又按 cp437
    解非 UTF-8 的条目名），所以条目表要在两处共用。
    """
    with zipfile.ZipFile(zip_path) as zf:
        infos = zf.infolist()
    names = [i.filename for i in infos]
    prefix = os.path.commonprefix([n for n in names if "/" in n]) if names else ""
    root_prefix = prefix.split("/", 1)[0] + "/" if "/" in prefix else ""
    return infos, root_prefix


def in_pkg(name: str, root_prefix: str) -> str:
    """把 zip 条目名换算成包内相对路径。"""
    return name[len(root_prefix):] if root_prefix and name.startswith(root_prefix) else name


def check_structure(rep: Report, zip_path: Path, pkg: Path, kind: str) -> None:
    print("\n--- A 包的结构 ---")

    infos, root_prefix = read_entries(zip_path)
    names = [i.filename for i in infos]

    # -- A1 四个入口文件都在包根 --------------------------------------------
    missing = [f for f in ENTRY_FILES if not (pkg / f).is_file()]
    if not missing:
        rep.ok("A1", "四个入口文件都在包根", " ".join(ENTRY_FILES))
    else:
        owners = sorted({ENTRY_FILES[f] for f in missing})
        present = [f for f in ENTRY_FILES if f not in missing]
        rep.pending("A1", "四个入口文件都在包根", "/".join(owners),
                    f"缺 {' '.join(missing)}" + (f"；已在包根：{' '.join(present)}" if present else ""))

    # -- A2 RUN-ME.command 带可执行位 ---------------------------------------
    # 读 zip 条目的**外部属性高 16 位**（Unix mode），不读解压出来的文件 ——
    # Python 的 zipfile.extractall 本来就不还原权限位，拿它的产物判等于永远红。
    # 客户用 macOS Finder / `unzip` 解压时权限位是**会**还原的，所以判据放在
    # 包的字节上才对得上客户的处境。
    cmd_entry = next((i for i in infos if in_pkg(i.filename, root_prefix) == "RUN-ME.command"), None)
    if cmd_entry is None:
        rep.pending("A2", "RUN-ME.command 解压后带可执行位", "T149", "包里没有这个文件")
    else:
        mode = (cmd_entry.external_attr >> 16) & 0o777
        if mode & 0o111:
            rep.ok("A2", "RUN-ME.command 解压后带可执行位", f"zip 内 mode={mode:04o}")
        else:
            rep.fail("A2", "RUN-ME.command 解压后带可执行位",
                     f"zip 内 mode={mode:04o}，双击不会执行，客户只能手敲 bash RUN-ME.command")

    # -- A3 内部物料 0 命中 --------------------------------------------------
    if kind != "client":
        rep.pending("A3", "内部物料 0 命中", "T150", "本次打的是内部包，此项对它无意义")
    else:
        hits: list[str] = []
        for name in names:
            rel = in_pkg(name, root_prefix)
            for bad in INTERNAL_PATHS:
                if rel == bad or rel.startswith(bad):
                    hits.append(rel)
                    break
        if hits:
            rep.fail("A3", "内部物料 0 命中", f"{len(hits)} 条命中",
                     extra=sorted(set(hits))[:8])
        else:
            rep.ok("A3", "内部物料 0 命中", f"查了 {len(INTERNAL_PATHS)} 类，{len(names)} 个条目")

    # -- A4 git 历史里也 0 命中 ---------------------------------------------
    # 这一条才是「剔掉」的真判据。包里没有 `docs/phases/` 但 `.git` 里还留着它的
    # blob，客户一句 `git log -- docs/phases` 就能全捞出来 —— 那等于没剔。
    # T150 走 `--orphan` 正是为了这个；不验这条就等于没验。
    git_dir = pkg / ".git"
    if not git_dir.exists():
        # 包里没 .git 是**另一种**失败：`scripts/make_evidence.py` 取不到出处 sha 就
        # 按铁律 3 拒绝生成，C 组的 ①② 根本跑不起来。
        rep.fail("A4", "git 历史里 0 命中",
                 "包里没有 .git/ —— make_evidence.py 会拒绝生成证据，C 组必然跑不通")
    elif kind != "client":
        rep.pending("A4", "git 历史里 0 命中", "T150", "本次打的是内部包，历史里当然有")
    else:
        proc = subprocess.run(["git", "rev-list", "--all", "--objects"],
                              cwd=str(pkg), capture_output=True, text=True)
        if proc.returncode != 0:
            rep.fail("A4", "git 历史里 0 命中",
                     f"git rev-list exit={proc.returncode}：{proc.stderr.strip()[:120]}")
        else:
            hits = []
            for line in proc.stdout.splitlines():
                path = line.split(" ", 1)[1] if " " in line else ""
                if not path:
                    continue
                for bad in INTERNAL_PATHS:
                    if path == bad.rstrip("/") or path == bad or path.startswith(bad):
                        hits.append(path)
                        break
            n_commits = subprocess.run(["git", "rev-list", "--count", "--all"],
                                       cwd=str(pkg), capture_output=True, text=True).stdout.strip()
            if hits:
                rep.fail("A4", "git 历史里 0 命中",
                         f"{len(hits)} 条内部路径仍在对象库里（共 {n_commits} 个 commit）",
                         extra=sorted(set(hits))[:8])
            else:
                rep.ok("A4", "git 历史里 0 命中", f"对象库共 {n_commits} 个 commit，0 条内部路径")


# ---------------------------------------------------------------------------
# B. 第 0 层 —— 零依赖必达
# ---------------------------------------------------------------------------
#: HTML 里取资源引用。只取 href/src，不取 CSS `url()` —— 入口页按 T148 的约定是
#: 单文件零外链，真出现 `url()` 会被 B1 的外链计数抓住。
HREF_RE = re.compile(r"""\b(?:href|src)\s*=\s*["']([^"']*)["']""", re.I)
URL_RE = re.compile(r"https?://", re.I)


def check_layer0(rep: Report, pkg: Path) -> None:
    print("\n--- B 第 0 层（零依赖必达）---")
    page = pkg / "START-HERE.html"
    if not page.is_file():
        for key, title in (("B1", "START-HERE.html 零外链"),
                           ("B2", "引用的相对路径都真实存在"),
                           ("B3", "没有绝对路径 href")):
            rep.pending(key, title, "T148", "包里没有 START-HERE.html")
        return

    text = page.read_text(encoding="utf-8", errors="replace")

    # -- B1 零外链：断网也要能看 --------------------------------------------
    urls = URL_RE.findall(text)
    if urls:
        samples = sorted({m.group(0) for m in re.finditer(r"https?://[^\s\"'<>)]+", text)})[:5]
        rep.fail("B1", "START-HERE.html 零外链",
                 f"{len(urls)} 处 http(s):// —— 断网的评委会看到半截页面", extra=samples)
    else:
        rep.ok("B1", "START-HERE.html 零外链", f"{len(text)} 字节，0 处 http(s)://")

    refs = [r.strip() for r in HREF_RE.findall(text)]
    # 锚点与内联协议不是文件引用，判存在性没有意义。
    local = [r for r in refs
             if r and not r.startswith(("#", "data:", "javascript:", "mailto:", "tel:"))
             and not URL_RE.match(r)]

    # -- B2 引用的每个相对路径都真的存在 ------------------------------------
    # 尤其 `evidence/report.html` —— 裁剪打包最容易在这里翻车：页面留着链接、
    # 文件被裁掉了，客户点开就是 404，而这在仓库里永远复现不出来。
    broken: list[str] = []
    for ref in local:
        if ref.startswith("/"):
            continue                      # 归 B3 管，这里不重复报
        target = (pkg / ref.split("#", 1)[0].split("?", 1)[0]).resolve()
        if not target.exists():
            broken.append(ref)
    if not local:
        rep.ok("B2", "引用的相对路径都真实存在", "页面没有任何本地资源引用")
    elif broken:
        rep.fail("B2", "引用的相对路径都真实存在",
                 f"{len(broken)}/{len(local)} 条断链", extra=sorted(set(broken))[:8])
    else:
        has_report = any(r.split("#")[0].split("?")[0] == "evidence/report.html" for r in local)
        note = f"{len(local)} 条全部命中"
        note += "，含 evidence/report.html" if has_report else "（页面未引用 evidence/report.html）"
        rep.ok("B2", "引用的相对路径都真实存在", note)

    # -- B3 没有绝对路径 -----------------------------------------------------
    # `/evidence/report.html` 在 `file://` 协议下会被解析成**磁盘根目录**下的路径，
    # 客户双击打开必然 404。这是本地 HTML 最典型的一种坏法。
    abs_refs = [r for r in local if r.startswith("/")]
    win_abs = [r for r in local if re.match(r"^[A-Za-z]:[\\/]", r) or r.startswith("\\\\")]
    bad = abs_refs + win_abs
    if bad:
        rep.fail("B3", "没有绝对路径 href",
                 f"{len(bad)} 条绝对路径 —— file:// 下会指到磁盘根", extra=sorted(set(bad))[:8])
    else:
        rep.ok("B3", "没有绝对路径 href", f"{len(local)} 条引用全是相对路径")


# ---------------------------------------------------------------------------
# C. 第 1 层 —— 一键脚本
# ---------------------------------------------------------------------------
def make_fake_bin(tmp: Path) -> Path:
    """造一个**没有 Python** 的 bin 目录，用来把 Python 藏掉。"""
    fake = tmp / "nopython-bin"
    fake.mkdir(parents=True, exist_ok=True)
    for tool in FAKE_BIN_TOOLS:
        src = shutil.which(tool)
        if src and not (fake / tool).exists():
            os.symlink(src, fake / tool)
    return fake


def run_entry(pkg: Path, env: dict[str, str], timeout: int) -> subprocess.CompletedProcess:
    """跑一次 `RUN-ME.command`，当它是客户双击的那一下。

    带可执行位就直接执行（客户双击走的正是这条），没有就退回 `bash <file>` ——
    退回是为了让 C 组在 A2 红的时候仍然能给出读数，A2 自己照红不误。
    """
    entry = pkg / "RUN-ME.command"
    argv = [str(entry)] if os.access(entry, os.X_OK) else ["bash", str(entry)]
    return subprocess.run(argv, cwd=str(pkg), env=env, capture_output=True,
                          text=True, timeout=timeout, errors="replace")


def check_layer1(rep: Report, pkg: Path, tmp: Path, timeout: int) -> None:
    print("\n--- C 第 1 层（一键脚本）---")
    entry = pkg / "RUN-ME.command"
    if not entry.is_file():
        rep.pending("C1", f"跑一次 RUN-ME.command，终点是 {EXPECT_RESULT}", "T149",
                    "包里没有 RUN-ME.command")
        rep.pending("C2", "藏掉 Python 后不吐 traceback、且指向 START-HERE.html", "T149",
                    "包里没有 RUN-ME.command")
        return

    # -- C1 顺利路径 ---------------------------------------------------------
    # 环境按「评委的机器」摆：没有任何模型网关 key，零出网。
    env = {k: v for k, v in os.environ.items()
           if not re.match(r"^(MAOS_|MATRIX_|ANTHROPIC_|OPENAI_|DEEPSEEK_)", k)}
    print(f"    跑 {entry.name}（timeout={timeout}s，已摘掉全部模型网关 env）…")
    try:
        proc = run_entry(pkg, env, timeout)
    except subprocess.TimeoutExpired:
        rep.fail("C1", f"跑一次 RUN-ME.command，终点是 {EXPECT_RESULT}",
                 f"{timeout}s 内没跑完 —— 客户双击后不会等这么久")
    else:
        out = proc.stdout + proc.stderr
        result_lines = [ln.strip() for ln in out.splitlines() if ln.strip().startswith("RESULT:")]
        tail = result_lines[-1] if result_lines else "（输出里没有 RESULT 行）"
        if proc.returncode == 0 and EXPECT_RESULT in out:
            rep.ok("C1", f"跑一次 RUN-ME.command，终点是 {EXPECT_RESULT}",
                   f"exit=0，{tail}")
        else:
            rep.fail("C1", f"跑一次 RUN-ME.command，终点是 {EXPECT_RESULT}",
                     f"exit={proc.returncode}，实得 {tail}",
                     extra=[ln for ln in out.splitlines()[-6:] if ln.strip()])

    # -- C2 失败路径 ---------------------------------------------------------
    # 客户机器上没装 Python 是**最可能发生**的那种失败。这时脚本吐一段 traceback
    # 等于把「你装一下 Python」说成「这软件坏了」。它应该说人话，并且告诉客户
    # 还有零依赖那条路（双击 START-HERE.html）。
    fake = make_fake_bin(tmp)
    env2 = dict(env)
    env2["PATH"] = str(fake)
    print(f"    再跑一次，PATH 换成没有 python 的 {fake.name}/ …")
    try:
        proc2 = run_entry(pkg, env2, timeout)
    except subprocess.TimeoutExpired:
        rep.fail("C2", "藏掉 Python 后不吐 traceback、且指向 START-HERE.html",
                 f"{timeout}s 内没跑完")
        return
    out2 = proc2.stdout + proc2.stderr
    has_tb = "Traceback (most recent call last)" in out2
    points_home = "START-HERE.html" in out2
    notes: list[str] = []
    if has_tb:
        notes.append("吐了 traceback")
    if not points_home:
        notes.append("没提 START-HERE.html（客户不知道还有零依赖那条路）")
    if notes:
        rep.fail("C2", "藏掉 Python 后不吐 traceback、且指向 START-HERE.html",
                 f"exit={proc2.returncode}；" + "、".join(notes),
                 extra=[ln for ln in out2.splitlines()[-6:] if ln.strip()])
    else:
        rep.ok("C2", "藏掉 Python 后不吐 traceback、且指向 START-HERE.html",
               f"exit={proc2.returncode}，输出干净且指路")


# ---------------------------------------------------------------------------
# C'. RUN-ME.bat —— 只做静态检查，真机行为不声称
# ---------------------------------------------------------------------------
#: markdown 行内链接。与 `scripts/check_docs.py` 的 D 类同一个形状。
MD_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def check_bat_static(rep: Report, pkg: Path, zip_path: Path) -> None:
    print("\n--- C' RUN-ME.bat（静态检查，真机待验）---")
    bat = pkg / "RUN-ME.bat"
    if not bat.is_file():
        for key, title in (("W1", "行尾是 CRLF"), ("W2", "有 %~dp0（双击时 cwd 不是脚本目录）"),
                           ("W3", "Python 探测含 py -3"), ("W4", "包根文件名全 ASCII")):
            rep.pending(key, title, "T149", "包里没有 RUN-ME.bat")
        return

    raw = bat.read_bytes()

    # -- W1 CRLF ------------------------------------------------------------
    # 老版本 cmd.exe 对 LF-only 的 .bat 会把标签跳转、多行 if 解析错。
    lf_total = raw.count(b"\n")
    crlf = raw.count(b"\r\n")
    if lf_total and crlf == lf_total:
        rep.static("W1", "行尾是 CRLF", f"{crlf}/{lf_total} 行")
    else:
        rep.fail("W1", "行尾是 CRLF",
                 f"只有 {crlf}/{lf_total} 行是 CRLF —— 老版 cmd.exe 会解析错")

    # -- W2 %~dp0 -----------------------------------------------------------
    # 客户双击 .bat 时工作目录不保证是脚本所在目录（从资源管理器双击通常是，
    # 但从「以管理员身份运行」起就不是）。没有 %~dp0 的脚本会找不到同级文件。
    txt = raw.decode("utf-8", errors="replace")
    if "%~dp0" in txt:
        rep.static("W2", "有 %~dp0（双击时 cwd 不是脚本目录）", f"{txt.count('%~dp0')} 处")
    else:
        rep.fail("W2", "有 %~dp0（双击时 cwd 不是脚本目录）",
                 "脚本按相对路径找同级文件，换个 cwd 就找不到")

    # -- W3 py -3 -----------------------------------------------------------
    # Windows 上 python.org 安装包默认装 `py` launcher，而 `python` 可能是
    # 微软商店那个只会弹广告页的 stub。探测顺序里没有 `py -3` 的脚本在
    # 相当一部分真机上会走进那个 stub。
    if re.search(r"\bpy\s+-3\b", txt):
        rep.static("W3", "Python 探测含 py -3", "探测顺序里有 launcher")
    else:
        rep.fail("W3", "Python 探测含 py -3",
                 "没有 py -3 —— 只装了 python.org 版的机器上可能撞进商店 stub")

    # -- W4 文件名全 ASCII ---------------------------------------------------
    # Windows 自带解压器对**没打 UTF-8 标志位**的 zip 条目按本地代码页（简中是 GBK）
    # 解文件名，中文名解出来就是乱码，脚本按原名找文件全落空。
    #
    # 判据读的是 **zip 条目名 + 通用位标志第 11 位**，不是解压出来的磁盘文件名 ——
    # 理由同 A2：判的是包的字节。Python 自己解这种条目时按 cp437 解，磁盘上拿到的
    # 已经是另一串乱码了，拿它当判据等于隔着一层哈哈镜看问题。
    infos, root_prefix = read_entries(zip_path)
    entries = [i for i in infos
               if in_pkg(i.filename, root_prefix)
               and in_pkg(i.filename, root_prefix).count("/") == 0]
    risky = [in_pkg(i.filename, root_prefix) for i in entries
             if not i.filename.isascii() and not (i.flag_bits & 0x800)]
    nonascii = [in_pkg(i.filename, root_prefix) for i in entries
                if not i.filename.isascii()]
    if risky:
        rep.fail("W4", "包根文件名全 ASCII",
                 f"{len(risky)} 个非 ASCII 条目名且未打 UTF-8 标志位，Windows 自带解压器会解成乱码",
                 extra=sorted(risky)[:8])
    elif nonascii:
        rep.fail("W4", "包根文件名全 ASCII",
                 f"{len(nonascii)} 个非 ASCII 条目名（已打 UTF-8 标志位，但仍依赖解压器守规矩）",
                 extra=sorted(nonascii)[:8])
    else:
        rep.static("W4", "包根文件名全 ASCII", f"包根 {len(entries)} 个条目")


# ---------------------------------------------------------------------------
# D. 文档
# ---------------------------------------------------------------------------
def check_docs(rep: Report, pkg: Path) -> None:
    print("\n--- D 文档 ---")
    doc = pkg / "README-CLIENT.md"
    if not doc.is_file():
        rep.pending("D1", "README-CLIENT.md 的相对链接都指向包内真实文件", "T151",
                    "包里没有 README-CLIENT.md")
        return

    text = doc.read_text(encoding="utf-8", errors="replace")
    broken: list[str] = []
    checked = 0
    for target in MD_LINK.findall(text):
        if target.startswith(("http://", "https://", "mailto:", "#", "tel:")):
            continue
        path = target.split("#", 1)[0]
        if not path:
            continue
        checked += 1
        if not (pkg / path).exists():
            broken.append(target)
    # 裁剪打包最爱在这里出事：说明书还指着 `docs/phases/phase-3.md`，而那一整个
    # 目录已经被 --client 裁掉了。仓库里跑 check_docs.py 是绿的 —— 因为仓库里它还在。
    if broken:
        rep.fail("D1", "README-CLIENT.md 的相对链接都指向包内真实文件",
                 f"{len(broken)}/{checked} 条死链（裁剪后才会出现，仓库里查不出来）",
                 extra=sorted(set(broken))[:8])
    elif checked == 0:
        rep.ok("D1", "README-CLIENT.md 的相对链接都指向包内真实文件", "没有相对链接可查")
    else:
        rep.ok("D1", "README-CLIENT.md 的相对链接都指向包内真实文件", f"{checked} 条全部命中")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="client_smoke", description="客户路径验收：解压→双击，逐条断言")
    ap.add_argument("--zip", dest="zip_path", default=None,
                    help="验一个已经打好的包，跳过打包这一步")
    # `--zip` 配的包可能来自别处（整合期从别的轨手上接一个包过来验）。这时
    # 「它是不是客户包」不该由**本仓库的** make_release.sh 支不支持 --client 来答 ——
    # 那答的是另一个问题。auto 只是缺省的猜测，client 是把它当客户包严判。
    ap.add_argument("--kind", choices=("auto", "client", "internal"), default="auto",
                    help="把包当客户包还是内部包判；缺省 auto 按 make_release.sh 探")
    ap.add_argument("--keep", action="store_true", help="保留解压目录便于排查")
    ap.add_argument("--timeout", type=int, default=1800,
                    help="RUN-ME.command 单次超时秒数，缺省 1800")
    args = ap.parse_args(argv)

    print("==> T152 客户路径验收 —— 站在解压目录里判，不回仓库取巧")
    head = subprocess.run(["git", "rev-parse", "--short=7", "HEAD"],
                          cwd=str(ROOT), capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain"],
                           cwd=str(ROOT), capture_output=True, text=True).stdout
    print(f"    仓库 HEAD : {head}   工作区脏行: {len([l for l in dirty.splitlines() if l.strip()])}")

    rep = Report()
    tmp = Path(tempfile.mkdtemp(prefix="maos-client-smoke-"))
    try:
        if args.zip_path:
            zip_path: Path | None = Path(args.zip_path).resolve()
            if not zip_path.is_file():
                print(f"[FAIL] --zip 指向的文件不存在：{zip_path}")
                return 1
            kind = args.kind
            if kind == "auto":
                kind = "client" if supports_client_flag() else "internal"
            print(f"==> 用现成的包：{zip_path}（kind={kind}）")
        else:
            zip_path, kind = build_package(rep)
            if args.kind != "auto":
                kind = args.kind

        if zip_path is None:
            print("\n[FAIL] 没有包可验，后面的判据一条都没跑。")
            return 1

        dest = tmp / "unzipped"
        pkg = extract(zip_path, dest)
        if pkg is None:
            print("[FAIL] 解压后找不到包根目录")
            return 1
        print(f"    包        : {zip_path}  (kind={kind})")
        print(f"    解压到    : {pkg}")

        check_structure(rep, zip_path, pkg, kind)
        check_layer0(rep, pkg)
        check_layer1(rep, pkg, tmp, args.timeout)
        check_bat_static(rep, pkg, zip_path)
        check_docs(rep, pkg)
    finally:
        if args.keep:
            print(f"\n（--keep）解压目录留在 {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)

    # ---------------------------------------------------------------- 收尾判定
    n_ok, n_static = rep.count(OK), rep.count(STATIC)
    fails = [r for r in rep.results if r.status == FAIL]
    pends = [r for r in rep.results if r.status == PENDING]

    print("\n================ 结果 ================")
    print(f"已实测通过    : {n_ok}")
    print(f"静态检查通过  : {n_static}（真机待验，不声称 Windows 已验证）")
    print(f"待产出        : {len(pends)}")
    for r in pends:
        print(f"                · {r.key} {r.title} —— {r.owner}")
    print(f"不通过        : {len(fails)}")
    for r in fails:
        print(f"                · {r.key} {r.title} —— {r.note}")

    if fails:
        print("结论          : ❌ 有判据不通过，见上")
        return 1
    if pends:
        print("结论          : ⏸  跑到的判据全过，但有前置产物尚未并入（exit 3）")
        return 3
    print("结论          : ✅ 客户路径全线可达")
    return 0


if __name__ == "__main__":
    sys.exit(main())
