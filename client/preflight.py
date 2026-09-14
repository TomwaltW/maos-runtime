#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""客户侧一键复现 —— 双击 RUN-ME 之后真正干活的那一个文件。

    python3 client/preflight.py              # 跑完全链路，最后用浏览器打开报告页
    python3 client/preflight.py --no-open    # 同上，但不开浏览器（自动化 / 无桌面环境用）

**读者是业务方，不是工程师。** 屏幕上只该出现两种东西：这一步成了，或者这一步没成
外加「下一步该干什么」。任何 traceback 出现在客户屏幕上都算这个文件写砸了。

设计口径（改本文件前先读这五条）：

1. **整份文件用 Python 2 / 3 通用语法写**（无 f-string、无类型注解、无 walrus）。
   理由不是要支持 Python 2，而是**版本检查本身必须先跑得起来**：Python 先编译整个
   模块再执行第一行，模块里任何一个 f-string 都会让 3.5 以下的解释器在**编译期**
   抛 ``SyntaxError`` —— 于是客户看到的是一屏 traceback，而不是「你的 Python 太老了，
   需要 3.10 以上」。入口脚本那一层已经挡了一道，但坑就坑在客户可能绕过入口
   直接 ``python preflight.py``，而那台机器上的 ``python`` 是 2.7。
   现代语法只在**版本检查之后**的 import 里用（import 是运行时行为，不参与编译期检查）。

2. **只用 ASCII 与常用汉字，一个 emoji 都不许有**（本文件的注释与文档字符串也算在内）。
   Windows 控制台在 ``chcp`` 没切成 65001 时按 GBK 编码 stdout，``OK`` 那个位置若放一个
   对钩 emoji 就是 ``UnicodeEncodeError`` —— 一个为了好看加的字符，把整条链路炸在最后
   一行。汉字 GBK 编得出来，emoji 编不出来。判据是「整份文件 ``.encode("gbk")`` 不抛」，
   连注释一起扫（首版就是在注释里栽的）。``_setup_stdout()`` 再补一道
   ``errors="replace"`` 兜底。

3. **不改坏包。** 本脚本只跑三个生成器，不删任何文件、不 ``git checkout`` 还原任何东西。
   ``make_evidence.py`` 会重写 ``evidence/`` 下约 50 个文件，那是它的本职工作、是预期的；
   而「顺手收拾干净」在这个仓库里是**错的**（README §3：只 checkout 不删库会让
   ``verify.py`` 掉一大截，看上去像证据被伪造）。客户机器上更不该有任何自动还原。

4. **期望值从 ``docs/expected-metrics.json`` 读，不写死。** 那是全仓唯一真源
   （``scripts/demo_preflight.sh`` 同源）。读不到就退一步只认退出码，不假装校验过。

5. **失败路径比成功路径重要。** 客户机器上「没有 Python」「没装 git」「只拖了几个文件
   出来所以没有 .git」都是大概率事件，三条各有各的人话。判据在
   ``maos/tests/test_client_preflight.py``（若无则见回执实测）。

为什么要有 render_trace 那一步（派单的行为链里没有，这里补上）：
``evidence/report.html`` 不是 ``make_evidence.py`` 的产物，是 ``scripts/render_trace.py``
的。只跑 ``make_evidence.py`` 就打开 report.html，客户看到的是**上一次**渲染的旧投影
—— 而这一层的全部卖点就是「你自己跑出来的东西」。同一处绑定缺口 ``docs/BACKLOG.md``
（T95 那条）记过，``scripts/demo_preflight.sh`` 第 4 步也是这么补的。已记 docs/DECISIONS.md。
"""

from __future__ import print_function

import os
import sys

#: 复赛材料与 README 一致：核心零依赖，只要 Python >= 3.10。
MIN_PY = (3, 10)

#: 报告页与三个生成器的相对路径，全部相对仓库根。
REPORT_REL = os.path.join("evidence", "report.html")
START_HERE = "START-HERE.html"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BAR = "=" * 64
RULE = "-" * 64


# --------------------------------------------------------------------------
# 0. 版本闸 —— 这一段之上不许出现任何 3.x 独有语法（见文件头口径 1）
# --------------------------------------------------------------------------

def _say(line):
    """print 一行并立刻冲刷。

    不写 ``print(..., flush=True)``：那个关键字参数 Python 2 的 print 函数没有，
    而本文件要能在 2.7 上跑到版本检查那一行（文件头口径 1）。
    """
    sys.stdout.write(line + "\n")
    try:
        sys.stdout.flush()
    except Exception:
        pass


def _version_text():
    v = sys.version_info
    return "%d.%d.%d" % (v[0], v[1], v[2])


def _too_old():
    """Python 太老 —— 报出实际版本与要求版本，并指回不需要 Python 的那条路。"""
    _say("")
    _say(BAR)
    _say("  这台电脑上的 Python 版本太低，跑不了这一步")
    _say(BAR)
    _say("")
    _say("  需要 : Python %d.%d 或更高" % (MIN_PY[0], MIN_PY[1]))
    _say("  实际 : Python %s" % _version_text())
    _say("         (%s)" % sys.executable)
    _say("")
    _say("  这一步是【可选的】。它的作用是让你在自己的电脑上把证据链重跑一遍,")
    _say("  亲眼确认报告里的数字不是我们写上去的。")
    _say("")
    _say("  不装新版 Python 也不影响你看全部内容:")
    _say("      回到这个文件夹, 双击 %s" % START_HERE)
    _say("  那是一张静态网页, 不需要 Python, 也不需要联网。")
    _say("")
    _say("  想自己跑一遍: 到 python.org 下载 3.10 以上版本, 装完重新双击 RUN-ME。")
    _say("")
    return 3


if sys.version_info < MIN_PY:
    sys.exit(_too_old())


# --------------------------------------------------------------------------
# 到这里保证解释器 >= 3.10。下面的 import 才允许用 3.x 的东西。
# --------------------------------------------------------------------------

import json          # noqa: E402
import shutil        # noqa: E402
import subprocess    # noqa: E402
import time          # noqa: E402
import unicodedata   # noqa: E402
import webbrowser    # noqa: E402

#: 单步超时。客户的机器可能比开发机慢很多，宁可等，也不要在还有救的时候掐掉；
#: 但也不能不设 —— 卡死时客户面对的是一个永远不动的窗口，比报错更糟。
TIMEOUT_EVIDENCE = 1800
TIMEOUT_OTHER = 900


def _setup_stdout():
    """把 stdout 调成「编不出来的字符换成 ?，绝不抛异常」。

    Windows 控制台在 chcp 没切成 UTF-8 时按 GBK 编码；本文件已经约束了只用 ASCII
    与汉字（两边都编得出来），这一道是兜底 —— 万一子进程输出里混进一个特殊字符
    被原样回显，也不该炸在 print 上。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


# --------------------------------------------------------------------------
# 1. 排版 —— 让它像进度条，不像日志
# --------------------------------------------------------------------------

def _width(text):
    """字符串在等宽终端里占的列数（汉字算 2 列）。

    不算这个的话 ``str.ljust`` 会按字符数补空格，中文步骤名一多，右边那列 OK
    就参差不齐 —— 「像进度条」这件事全靠这一列对齐。
    """
    n = 0
    for ch in text:
        n += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return n


def _step_line(idx, total, label, dots_to=52):
    """打印 ``[1/5] 步骤名 ......``，不换行，等结果盖在后面。"""
    head = "[%d/%d] %s " % (idx, total, label)
    pad = dots_to - _width(head)
    sys.stdout.write(head + ("." * max(pad, 3)) + " ")
    try:
        sys.stdout.flush()
    except Exception:
        pass


def _ok(note=None):
    _say("OK")
    if note:
        _say("       " + note)


def _fail_mark():
    _say("没跑通")


def _banner(title):
    _say("")
    _say(BAR)
    _say("  " + title)
    _say(BAR)
    _say("")


def _hint_start_here(prefix="  "):
    """任何失败都以这句收尾：第 1 层挂了不影响第 0 层。"""
    _say("")
    _say(prefix + "这不影响你看主报告 —— 回到这个文件夹, 双击 %s," % START_HERE)
    _say(prefix + "结论、全链路、每一步的证据都在里面。那是一张静态网页,")
    _say(prefix + "不需要 Python, 也不需要联网。")


def _tail(text, n=12):
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    return lines[-n:]


# --------------------------------------------------------------------------
# 2. 环境自检
# --------------------------------------------------------------------------

def _run(cmd, timeout, cwd=None):
    """跑一条命令，回 (退出码, 合并后的输出)。命令本身不存在也不抛。

    ``PYTHONUNBUFFERED=1`` 不是可有可无的：stdout 走管道时是**全缓冲**而 stderr
    不缓冲，两股合进同一个管道，屏幕上的先后就与真实发生顺序不符 —— 实测过一次，
    traceback 印在五行 ``[OK]`` 的**上面**，于是「最后 8 行输出」里最后五行全是
    成功，客户读到的结论正好是反的。失败输出的全部价值就在「哪一步、为什么」，
    顺序错了等于说了假话。
    """
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd or ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            env=env,
        )
    except FileNotFoundError:
        return 127, "找不到命令: %s" % cmd[0]
    except subprocess.TimeoutExpired:
        return 124, "超过 %d 秒还没跑完, 已中止。" % timeout
    except OSError as exc:
        return 126, "无法执行 %s: %s" % (cmd[0], exc)
    out = proc.stdout or b""
    return proc.returncode, out.decode("utf-8", "replace")


def _check_layout():
    """包是不是完整的。回 None 表示没问题，否则回一段人话。

    客户把 zip 里的几个文件**拖出来**而不是整体解压，是这一层最常见的死法：
    没有 .git 就没有出处 sha，``make_evidence.py`` 按铁律 3 拒绝生成并 exit 2。
    那条报错对工程师是清楚的，对业务方是天书，所以在这里提前拦一道。
    """
    needed = [
        os.path.join("scripts", "make_evidence.py"),
        os.path.join("scripts", "render_trace.py"),
        os.path.join("scripts", "verify.py"),
        os.path.join("maos", "__init__.py"),
    ]
    missing = [rel for rel in needed if not os.path.exists(os.path.join(ROOT, rel))]
    if missing:
        return ("这个文件夹里缺了几个必须的文件:\n"
                + "\n".join("      " + m for m in missing)
                + "\n\n  多半是压缩包只解压了一部分。请把整个 zip 重新完整解压一次,\n"
                  "  然后在解压出来的文件夹里双击 RUN-ME。")

    # .git 在 worktree 里是一个文件（gitdir: ...），在正常克隆里是目录，两种都算有。
    if not os.path.exists(os.path.join(ROOT, ".git")):
        return ("这个文件夹里没有 .git 目录。\n\n"
                "  证据文件的第一行要写「这份证据出自哪个版本」, 这个信息只能从 .git 里取,\n"
                "  取不到就拒绝生成 —— 宁可没有证据, 不要来路不明的证据。\n\n"
                "  多半是从压缩包里【只拖出了几个文件】。请把整个 zip 完整解压一次\n"
                "  (.git 是隐藏目录, 解压时不会显示, 但它在), 然后在解压出来的文件夹里双击 RUN-ME。")

    if shutil.which("git") is None:
        return ("这台电脑上没有装 git。\n\n"
                "  证据文件的第一行要写「这份证据出自哪个版本」, 那个版本号要靠 git 才读得出来。\n\n"
                "  装 git: macOS 在终端里跑一次 xcode-select --install;\n"
                "          Windows 到 git-scm.com 下载安装包。\n"
                "  装完重新双击 RUN-ME 即可。")

    return None


def _head_sha():
    code, out = _run(["git", "rev-parse", "--short=7", "HEAD"], 60)
    return out.strip() if code == 0 else "(读不到)"


def _expected():
    """从唯一真源读期望值；读不到就回空 dict，只认退出码，不假装校验过。"""
    path = os.path.join(ROOT, "docs", "expected-metrics.json")
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except Exception:
        return {}
    return doc if isinstance(doc, dict) else {}


# --------------------------------------------------------------------------
# 3. 失败输出
# --------------------------------------------------------------------------

def report_failure(step_label, cmd, code, out):
    _fail_mark()
    _say("")
    _say(RULE)
    _say("  这一步没跑通: " + step_label)
    _say(RULE)
    _say("")
    _say("  命令   : " + " ".join(cmd))
    _say("  退出码 : %s" % code)
    tail = _tail(out)
    if tail:
        _say("  最后 %d 行输出:" % len(tail))
        for ln in tail:
            _say("    | " + ln)
    _hint_start_here()
    _say("")
    return 1


def report_environment(message):
    _fail_mark()
    _say("")
    _say(RULE)
    _say("  跑不起来")
    _say(RULE)
    _say("")
    _say("  " + message)
    _hint_start_here()
    _say("")
    return 2


# --------------------------------------------------------------------------
# 4. 主流程
# --------------------------------------------------------------------------

def open_report(path):
    """用系统默认浏览器打开报告页。回 True 表示确实唤起了什么东西。"""
    try:
        import pathlib
        url = pathlib.Path(path).as_uri()
    except Exception:
        url = "file://" + path
    try:
        if webbrowser.open(url):
            return True
    except Exception:
        pass
    # webbrowser 在少数环境里返回 False 也不抛（比如没有配置默认浏览器），
    # 退回到平台自带的打开命令再试一次。两条都不成就只打印路径，不算失败。
    fallback = None
    if sys.platform == "darwin":
        fallback = ["open", path]
    elif os.name == "nt":
        fallback = ["cmd", "/c", "start", "", path]
    if fallback:
        code, _ = _run(fallback, 60)
        return code == 0
    return False


def main(argv):
    _setup_stdout()

    no_open = "--no-open" in argv
    if "-h" in argv or "--help" in argv:
        _say(__doc__.strip().splitlines()[0])
        _say("")
        _say("  python3 client/preflight.py [--no-open]")
        return 0

    # 确定性由这一行强制：客户机器上万一 export 过 MAOS_LLM_*，没有它就会去打真模型
    # —— 那要花钱、要联网, 而且每跑一次结果都不一样, 与「可重放」这个卖点相反。
    # setdefault 而不是硬写: 内部真模型演示那条路仍然显式覆盖得掉。
    os.environ.setdefault("MAOS_FORCE_SCRIPTED", "1")

    _banner("MAOS 证据链 - 在你自己的电脑上重跑一遍")
    _say("  这个脚本会重新跑一遍全部演示场景, 重新生成全部证据, 再逐项核验一次。")
    _say("  跑完会自动打开报告页。全程离线, 不需要联网, 不需要任何账号或密钥。")
    _say("  开发机上实测十几秒, 你的机器慢一些也就一两分钟。")
    _say("")

    total = 5
    expected = _expected()

    # --- 1/5 环境 ---------------------------------------------------------
    _step_line(1, total, "检查运行环境", )
    problem = _check_layout()
    if problem:
        return report_environment(problem)
    _ok("Python %s  |  版本 %s" % (_version_text(), _head_sha()))

    py = sys.executable or "python3"

    # --- 2/5 证据束 -------------------------------------------------------
    bundles = expected.get("evidence_bundles")
    label = "生成证据束"
    if isinstance(bundles, int):
        label = "生成证据束 (%d 束, 这一步最久)" % bundles
    _step_line(2, total, label)
    cmd = [py, os.path.join("scripts", "make_evidence.py")]
    code, out = _run(cmd, TIMEOUT_EVIDENCE)
    if code != 0:
        return report_failure("生成证据束", cmd, code, out)
    produced = [ln for ln in out.splitlines() if "[OK] evidence/scenario-" in ln]
    if isinstance(bundles, int) and len(produced) != bundles:
        return report_failure(
            "生成证据束 (落盘 %d 束, 期望 %d 束)" % (len(produced), bundles),
            cmd, code, out)
    _ok("%d 束已落盘" % len(produced))

    # --- 3/5 报告页 -------------------------------------------------------
    # 证据变了投影就得跟: make_evidence 不产 HTML, 少了这一步, 下面打开的
    # report.html 会是上一次渲染的旧投影（见文件头最后一段）。
    _step_line(3, total, "渲染报告页")
    cmd = [py, os.path.join("scripts", "render_trace.py")]
    code, out = _run(cmd, TIMEOUT_OTHER)
    if code != 0:
        return report_failure("渲染报告页", cmd, code, out)
    wrote = [ln for ln in out.splitlines() if "[WROTE]" in ln]
    _ok(wrote[-1].strip() if wrote else REPORT_REL)

    # --- 4/5 核验 ---------------------------------------------------------
    _step_line(4, total, "逐项核验证据")
    cmd = [py, os.path.join("scripts", "verify.py")]
    code, out = _run(cmd, TIMEOUT_OTHER)
    result_line = ""
    for ln in out.splitlines():
        if ln.startswith("RESULT:"):
            result_line = ln.strip()
    if code != 0:
        # 结论行提到标题里, 不要只留在「最后 12 行」里让人自己找 —— 核验失败时
        # 「10 项里过了几项」是客户唯一读得懂的那个数。
        return report_failure(
            "逐项核验证据" + (" (%s)" % result_line if result_line else ""),
            cmd, code, out)
    want = expected.get("verify_result_line")
    if isinstance(want, str) and want and result_line != want:
        return report_failure(
            "逐项核验证据 (结论行是 %s, 期望 %s)" % (result_line or "(空)", want),
            cmd, code, out)
    _ok(result_line or "核验通过")

    # --- 5/5 打开 ---------------------------------------------------------
    report = os.path.join(ROOT, REPORT_REL)
    _step_line(5, total, "打开报告页")
    if no_open:
        _ok("跳过 (--no-open)")
    elif not os.path.exists(report):
        # 上一步刚渲染成功却找不到文件, 只可能是被人挪走了。不算失败, 但要说清楚。
        _ok("文件不在预期位置, 请手动打开")
        _say("       " + report)
    elif open_report(report):
        _ok(REPORT_REL)
    else:
        _ok("没能自动打开, 请手动双击下面这个文件")
        _say("       " + report)

    _say("")
    _say(BAR)
    _say("  全部跑通。刚才那一页就是你自己这台电脑跑出来的结果。")
    _say(BAR)
    _say("")
    _say("  报告页 : " + REPORT_REL)
    _say("  想再看一遍不必重跑, 直接双击它即可。")
    _say("")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        _say("")
        _say("已取消。")
        sys.exit(130)
    except Exception as exc:  # 兜底: 客户屏幕上永远不该出现 traceback
        _say("")
        _say(RULE)
        _say("  这个脚本自己出错了 - 这是我们的 bug, 不是你的操作问题")
        _say(RULE)
        _say("")
        _say("  %s: %s" % (type(exc).__name__, exc))
        _hint_start_here()
        _say("")
        sys.exit(1)
