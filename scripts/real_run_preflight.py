#!/usr/bin/env python3
"""真跑日前置探测 —— 一条命令探完 9/18–9/19 那两天要用的全部前置。

这个脚本回答一个问题，且只回答这一个问题：
**今天这台机器上，真跑日那两件事（PolarDB 真实例、真 Matrix 房间）还差什么。**

## 为什么要有它

真跑日只有两天（2026-09-18 五、09-19 六），**且不可重来** —— 复赛 09-22/23 在杭州，
09-20/21 是材料定稿与彩排。现场撞上白名单漂移、缺根证书、key 没 source、
房间常驻挂了，任何一样都会吃掉半天。这些前置此前散在
`deploy/polardb-live.md`、`docs/phases/phase-10.md` §5 与若干轨的回执里，
**没有任何一条命令能在开跑前告诉你「哪个前置没齐」**。这就是那条命令。

## 只探不改

它不写任何文件、不连阿里云、不往房间发消息、不调真模型、不碰 launchd
（`launchctl list` 是只读的）。跑它零副作用，所以可以反复跑。

## 为什么不 import maos

与 `scripts/polardb_smoke.py` 同理，且这里更要紧：真跑日那天如果 maos 本体出了问题，
这条命令**仍然要能回答「前置齐不齐」**。一个 import 失败就哑掉的探测器，
恰恰在最需要它的那一刻没用。所以全程只用标准库。

## 脱敏（铁律 6）

**只报「有 / 无 / 是不是占位符」，绝不回显任何值。** DSN、token、key、口令
一个字符都不许进输出 —— 连长度都不报（长度也是信息）。

两道防线：

1. **第一道是设计**：没有任何一条探测会把环境变量的值放进 detail。
2. **第二道是兜底**：所有输出统一走 `emit()` → `_redact()`，把登记过的敏感片段
   无条件抹掉，外加正则兜任何 `scheme://...` 形态。即使将来有人加探测时手滑，
   也漏不出去。`maos/tests/test_real_run_preflight.py` 的哨兵测试守的就是这一条。

目标 host **不打印**（打出来等于泄漏实例地址）。**出口 IP 打印** —— 口径同
`polardb_smoke.py::_egress_ip()`：白名单按它放行，报障时人第一个要查的就是它，
且它不泄漏目标 host。

## 用法

    python3 scripts/real_run_preflight.py            # 人读
    python3 scripts/real_run_preflight.py --json     # 机器读（runbook A 段第 1 步）

## 退出码

    0  硬项与软项全绿
    1  有硬缺 —— 真跑日跑不起来，先补
    2  硬项全绿，只有软缺 —— 能跑，知道缺什么就行
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent

#: launchd 里人类演示房的常驻入口。**只读不动** —— 它挂着 KeepAlive，
#: kill / unload / 改 plist 都会把人的演示房弄停（跨轨契约 §E）。
ROOM_INGRESS_LABEL = "com.maos.room-ingress"

#: 本机自建 Synapse 与 Element 的端口（`lsof` 实测，2026-09-12：都由 Docker 起）。
SYNAPSE_ADDR = ("127.0.0.1", 8008)
ELEMENT_ADDR = ("127.0.0.1", 8080)
LOCAL_PG_ADDR = ("127.0.0.1", 5432)

#: 真模型三件套（`maos/model/client.py` 的 `ENV_*`）。
ENV_LLM = ("MAOS_LLM_API_KEY", "MAOS_LLM_BASE_URL", "MAOS_LLM_MODEL")

#: DSN 一眼能看出还是模板没换的片段。`deploy/polardb-live.md` 的用法段
#: 写的就是 `postgresql://<user>:<pass>@<host>:<port>/<db>` 这个形状 ——
#: 真跑日最容易犯的错是把文档里那行原样 export 了。
_PLACEHOLDER_MARKS = ("<", ">", "your-", "your_", "yourhost", "example.com",
                      "changeme", "xxxx", "..." )

#: 哪一段用得上这条前置。真跑日 A 段（PolarDB）与 B 段（真房间）多半分开做，
#: 只做 A 段时 B 段的红灯不该让人停下来查 —— 所以分组标出来。
SEG_A = "A·PolarDB"
SEG_B = "B·真房间"
SEG_BOTH = "通用"


# --------------------------------------------------------------------------
# 脱敏 —— 第二道防线（第一道是「压根不往 detail 里放值」）
# --------------------------------------------------------------------------

#: 无条件子串替换。只收长度 >= 4 的片段：再短的片段做全局替换会把正文切碎，
#: 而 4 个字符以下的口令 / token 在真跑日的场景里不存在。
_SECRETS: list[str] = []


def _remember(value: str | None) -> None:
    if value and len(value) >= 4:
        _SECRETS.append(value)


def _remember_all() -> None:
    """把环境里一切可能是秘密的片段登记下来。**在任何输出之前调用一次。**"""
    for name in ("MAOS_PG_DSN", "MAOS_LLM_API_KEY", "MAOS_LLM_BASE_URL",
                 "MATRIX_TOKEN", "MATRIX_HOMESERVER", "MATRIX_ROOM_ID"):
        _remember((os.environ.get(name) or "").strip())

    # DSN 再拆一层：整串被抹掉不等于 host / 口令被抹掉 —— 它们可能以别的形态
    # 出现在某个异常的 message 里（比如驱动把 host 拼进 OperationalError）。
    dsn = (os.environ.get("MAOS_PG_DSN") or "").strip()
    if dsn:
        try:
            parts = urlsplit(dsn)
        except ValueError:
            return
        for piece in (parts.hostname, parts.password, parts.netloc):
            _remember(str(piece) if piece else None)


def _redact(text: str) -> str:
    """抹掉任何可能泄漏值的片段（铁律 6）。"""
    out = str(text)
    # 长的先替换，免得短片段先把长片段切碎导致漏网。
    for secret in sorted(set(_SECRETS), key=len, reverse=True):
        out = out.replace(secret, "<redacted>")
    # 兜底：没登记到的连接串形态也一并抹掉。本脚本自己的输出里不含 `scheme://`
    # （Synapse / Element 只报 `host:port`），所以这条不会误伤正文。
    return re.sub(r"\b[a-zA-Z][\w+.-]*://[^\s'\"]+", "<dsn-redacted>", out)


def emit(text: str = "") -> None:
    """本脚本**唯一**的出口。绕过它直接 print = 绕过脱敏。"""
    print(_redact(text))


# --------------------------------------------------------------------------
# 探测结果
# --------------------------------------------------------------------------

class Probe:
    """一条探测。`hard=True` 表示真跑日那天缺了它就跑不起来。"""

    def __init__(self, number: int, title: str, *, hard: bool, segment: str) -> None:
        self.number = number
        self.title = title
        self.hard = hard
        self.segment = segment
        self.ok = False
        self.detail = ""

    def pass_(self, detail: str) -> "Probe":
        self.ok, self.detail = True, detail
        return self

    def fail(self, detail: str) -> "Probe":
        self.ok, self.detail = False, detail
        return self

    def as_dict(self) -> dict:
        return {
            "number": self.number,
            "title": self.title,
            "hard": self.hard,
            "segment": self.segment,
            "ok": self.ok,
            "detail": _redact(self.detail),
        }


def _run(args: list[str], *, timeout: float = 10, cwd: Path | None = None):
    return subprocess.run(args, capture_output=True, text=True,
                          timeout=timeout, cwd=str(cwd) if cwd else None)


def _port_open(addr: tuple[str, int], *, timeout: float = 2.0) -> tuple[bool, str]:
    """本机端口在不在听。只探 127.0.0.1，不出网。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(addr)
        return True, "在听"
    except ConnectionRefusedError:
        return False, "没在听（端口开着但没人接 = 服务没起）"
    except socket.timeout:
        return False, f"连接超时（{timeout:g}s）"
    except OSError as exc:
        return False, f"连不上 -> {type(exc).__name__}"
    finally:
        sock.close()


# --------------------------------------------------------------------------
# 11 条探测
# --------------------------------------------------------------------------

def probe_1_worktree() -> Probe:
    """工作区干净 + 当前 sha。

    为什么是硬项：证据首行是 `# generated at <ISO8601> from <git sha>`（铁律 3），
    工作区脏的时候生成脚本会写出 `<sha>-dirty`。带 `-dirty` 的证据在答辩上
    没法说「这份证据出自这个提交」—— 那个提交在 GitHub 上根本不存在。
    """
    probe = Probe(1, "工作区干净 + 当前 sha", hard=True, segment=SEG_BOTH)
    try:
        head = _run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT)
        status = _run(["git", "status", "--porcelain"], cwd=ROOT)
    except Exception as exc:                              # noqa: BLE001
        return probe.fail(f"git 跑不起来 -> {type(exc).__name__}")
    if head.returncode != 0:
        return probe.fail("取不到 HEAD（这里不是 git 仓库？）")
    sha = head.stdout.strip()
    dirty = [ln for ln in status.stdout.splitlines() if ln.strip()]
    if dirty:
        return probe.fail(
            f"HEAD {sha}，但有 {len(dirty)} 处未提交改动 —— "
            f"证据首行会写成 `{sha}-dirty`，先提交或还原")
    return probe.pass_(f"干净，HEAD {sha}")


def probe_2_psycopg() -> Probe:
    """psycopg 装没装。

    为什么是硬项：`polardb_smoke.py` 和 `MAOS_PG_DSN` 那档测试都靠它。
    **只认 v3 为全绿**：`pg_store._driver()` 只认 psycopg v3，而
    `polardb_smoke.py` 会回落 v2 —— 于是一台只有 v2 的机器上冒烟能报 5/5 全绿，
    而每次 `PgStorePort` 调用都抛 `PgBackendUnavailable`（BACKLOG 的 `## task-T31` 条）。
    真跑日撞上这个会以为是代码回归。
    """
    probe = Probe(2, "psycopg 驱动", hard=True, segment=SEG_A)
    try:
        import psycopg                                    # noqa: PLC0415
        return probe.pass_(f"psycopg v3 {psycopg.__version__}")
    except ImportError:
        pass
    try:
        import psycopg2                                   # noqa: PLC0415
        return probe.fail(
            f"只有 psycopg2（{psycopg2.__version__.split()[0]}）—— "
            f"冒烟脚本会回落它并报全绿，但 pg_store 只认 v3、每次调用都会抛 "
            f"PgBackendUnavailable。装 v3：python3 -m pip install 'psycopg[binary]'")
    except ImportError:
        return probe.fail(
            "没装 —— python3 -m pip install 'psycopg[binary]'")


def probe_3_pg_dsn() -> Probe:
    """MAOS_PG_DSN 配没配、是不是占位符模板。

    **不回显任何片段**，连长度都不报。只回答三件事：有没有、解析得动吗、
    是不是文档里那行模板原样 export 了。
    """
    probe = Probe(3, "MAOS_PG_DSN", hard=True, segment=SEG_A)
    dsn = (os.environ.get("MAOS_PG_DSN") or "").strip()
    if not dsn:
        return probe.fail("未配置 —— 真跑日 A 段必须 export（值不进任何日志）")
    lowered = dsn.lower()
    hits = [mark for mark in _PLACEHOLDER_MARKS if mark in lowered]
    if hits:
        return probe.fail(
            f"疑似占位符模板（命中 {len(hits)} 处模板标记）—— "
            f"是不是把 deploy/polardb-live.md 里那行原样 export 了")
    try:
        parts = urlsplit(dsn)
    except ValueError:
        return probe.fail("解析不动（不是合法 URL 形态）")
    if parts.scheme not in ("postgres", "postgresql"):
        return probe.fail("scheme 不是 postgres/postgresql")
    missing = [name for name, value in
               (("host", parts.hostname), ("用户名", parts.username),
                ("口令", parts.password), ("库名", parts.path.lstrip("/")))
               if not value]
    if missing:
        return probe.fail(f"缺 {'、'.join(missing)}")
    if parts.hostname in ("127.0.0.1", "localhost", "::1"):
        return probe.pass_("已配置，指向**本机**（不是云实例 —— A 段要的是真实例）")
    return probe.pass_("已配置，形态合法，指向非本机实例")


def probe_4_egress_ip() -> Probe:
    """本机公网出口 IP。

    白名单按出口 IP 放行，而**家用宽带 / 移动网络的出口 IP 会变** ——
    BACKLOG 2026-08-31 那条记着：同日在另一个 worktree 连得上，换个网络
    就全程 TCP 静默超时。所以这是真跑日第一条要抄下来的值。

    可以打印：口径同 `polardb_smoke.py::_egress_ip()` —— 它不泄漏目标 host。
    走 OpenDNS 的 `myip.opendns.com`，**不碰阿里云任何域名**。

    **必须 `-4`**（整合期 p10-f 查实）。此前这条探测在本机恒返回空答案，脚本据此
    报「这条查询在本机被接管了」—— 那个结论是错的。真相是 `dig` 默认可能走 IPv6
    去问 `resolver1.opendns.com`，那一路到不了真的 OpenDNS resolver（返回
    `NOERROR / ANSWER: 0`）；强制 IPv4 之后同一条查询稳定返回真实出口 IP，
    实测连跑三次一致。备用 resolver 走 `208.67.222.222`（OpenDNS 的 IP 字面量，
    连域名解析这一跳都省了），因为实测第一台偶尔超时。

    取不到时仍分档报，三档的下一步完全不同：dig 不在、超时、空答案。
    **`dig` 自己超时算「超时」档，不算「空答案」**（T142 补）：它超时时把
    `;; connection timed out` 写在 stdout 上、退出码 9，答案行同样为空，只按
    「答案行为空」归档会把「可以重试」和「重试没用」混成一条。
    口径与 `polardb_smoke.py::_egress_ip()` 逐条一致，判据
    `test_polardb_smoke_t142.py::test_both_scripts_classify_digs_own_timeout_the_same`。

    **这是软项**（整合期 p10-f 由硬改软）：它产出的是**一个要抄下来的值**，
    不是「跑不跑得起来」的前提 —— 控制台白名单页面自己会显示当前来访 IP，
    照那个填一样能开跑。而这条查询要过公网、实测会偶发超时，标成硬项等于让
    一次 DNS 抖动把真跑日的退出码判成「跑不起来」，`docs/real-run-runbook.md`
    0 段写的「硬项全绿、退出码 0 或 2」也就随机达不到。
    """
    probe = Probe(4, "本机公网出口 IP（白名单要用）", hard=False, segment=SEG_A)
    fallback = ("控制台白名单页面通常会显示当前来访 IP，照那个填；"
                "polardb_smoke.py 连不上时报的那行出口 IP 同样取不到，别等它")
    if not shutil.which("dig"):
        return probe.fail(f"dig 不可用 —— {fallback}")
    last = ""
    for resolver in ("@resolver1.opendns.com", "@208.67.222.222"):
        try:
            # `-4` 不能省：不带它可能走 IPv6，那一路到不了真的 OpenDNS resolver，
            # 返回 NOERROR / ANSWER: 0（整合期 p10-f 查实的老「被接管」之谜）。
            out = _run(["dig", "-4", "+short", "+time=5", "+tries=1",
                        "myip.opendns.com", resolver], timeout=20)
        except subprocess.TimeoutExpired:
            last = "查询超时"
            continue
        except Exception as exc:                          # noqa: BLE001
            last = f"取出口 IP 失败 -> {type(exc).__name__}"
            continue
        lines = [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
        # dig 把「连不上」也写在 stdout 上（`;; connection timed out`），
        # 那不是一个 IP —— 当成答案抄进白名单会把真跑日引到沟里。
        answer = [ln for ln in lines if ln and not ln.startswith(";")]
        if answer:
            return probe.pass_(f"{answer[-1]} —— 控制台白名单里必须有这个 IP")
        # 🔴 但把它归成「空答案」也是错的（T142 补）：那是 `dig` **自己**超时
        # （退出码 9 = no servers could be reached），下一步是「可以重试」，而空
        # 答案的下一步是「重试没用，去控制台看」。只按「答案行为空」归档会把这两
        # 条路混起来 —— T142 在 `polardb_smoke.py::_egress_ip()` 上实测撞到过一次
        # （连跑两次，第二次 dig 自己超时，被归成了空答案）。两个脚本同一口径。
        if out.returncode == 9 or any("connection timed out" in ln for ln in lines):
            last = "查询超时"
            continue
        last = "查询返回空答案"
    return probe.fail(f"{last or '取不到'}（两台 resolver 都试过）—— {fallback}")


def probe_5_ssl_cert_file() -> Probe:
    """SSL_CERT_FILE 设没设、指的文件在不在。

    本机 Python.framework **缺根证书**，不设这个变量时任何 https 都
    `CERTIFICATE_VERIFY_FAILED` —— 真模型那一段（B 段）第一条就会撞上。

    **不打印路径**：它通常含用户名。只报存在与否和大小。
    """
    probe = Probe(5, "SSL_CERT_FILE", hard=True, segment=SEG_B)
    raw = (os.environ.get("SSL_CERT_FILE") or "").strip()
    if not raw:
        return probe.fail(
            "未设置 —— 本机 Python 缺根证书，任何 https 都会 "
            "CERTIFICATE_VERIFY_FAILED（真模型那一段必撞）")
    path = Path(raw)
    if not path.is_file():
        return probe.fail("已设置，但指向的文件不存在（路径不打印）")
    try:
        size = path.stat().st_size
    except OSError:
        return probe.fail("已设置，文件在但读不了属性")
    if size <= 0:
        return probe.fail("已设置，但文件是空的")
    return probe.pass_(f"已设置，文件存在（{size} 字节）")


def probe_6_model_key() -> Probe:
    """真模型三件套在不在环境里。

    软项：只做 Scripted 束时一个都不需要（Wave C 之后**不给 `--live-model`
    一定是 Scripted**，这是机器缺省）。只有 B 段的 `--live-model` 那一束要它。

    只报「有 / 无」，不报值、不报长度、不报前几位。
    """
    probe = Probe(6, "真模型 key（MAOS_LLM_*）", hard=False, segment=SEG_B)
    present = [name for name in ENV_LLM if (os.environ.get(name) or "").strip()]
    if len(present) == len(ENV_LLM):
        return probe.pass_("三个都在（API_KEY / BASE_URL / MODEL）")
    if not present:
        return probe.fail(
            "三个都不在 —— 只做 Scripted 束时不需要；"
            "B 段的 --live-model 那一束要 source ~/.maos.env")
    missing = [name for name in ENV_LLM if name not in present]
    return probe.fail(f"只配了 {len(present)}/3，缺 {'、'.join(missing)}")


def probe_7_local_pg() -> Probe:
    """本机 PG 在不在听。

    软项，但它是 A 段最重要的**退路**：真实例连不上（白名单漂移改不动）时，
    口径退回「PolarDB 8/30 实测 + 本机 PG 同构证据」，许可的说法逐字在
    `docs/submission-checklist.md` 的 StorePort / PolarDB 那一行。
    """
    probe = Probe(7, f"本机 PG {LOCAL_PG_ADDR[0]}:{LOCAL_PG_ADDR[1]}（退路）",
                  hard=False, segment=SEG_A)
    ok, detail = _port_open(LOCAL_PG_ADDR)
    return probe.pass_(f"{detail} —— 真实例不通时的退路在") if ok else probe.fail(
        f"{detail} —— 退路也没了，真实例不通就没有 PG 证据了")


def probe_8_room_ingress() -> Probe:
    """`com.maos.room-ingress` 在不在。

    **只读 `launchctl list`**：它挂着 KeepAlive，是人类演示房的常驻入口。
    kill / unload / 改 plist 都是越界（跨轨契约 §E）。

    `launchctl list` 的三列是 `PID  上次退出状态  Label`。PID 是 `-`
    表示登记了但当前没在跑 —— 那正是 B 段会撞上的那种「看起来配好了，
    房间里一条消息都没有」。
    """
    probe = Probe(8, f"{ROOM_INGRESS_LABEL}（只读）", hard=True, segment=SEG_B)
    try:
        out = _run(["launchctl", "list"])
    except Exception as exc:                              # noqa: BLE001
        return probe.fail(f"launchctl 跑不起来 -> {type(exc).__name__}")
    row = [ln for ln in out.stdout.splitlines()
           if ln.strip().endswith(ROOM_INGRESS_LABEL)]
    if not row:
        return probe.fail("没登记在 launchd 里 —— 真房间那段发不出消息")
    fields = row[0].split()
    pid = fields[0] if fields else "-"
    if pid == "-":
        return probe.fail(
            f"登记了但当前没在跑（上次退出状态 "
            f"{fields[1] if len(fields) > 1 else '?'}）—— "
            f"别 kill / unload，让人类 launchctl kickstart 一下")
    return probe.pass_(f"在跑（PID {pid}）")


def probe_9_synapse_element() -> Probe:
    """Synapse / Element 在不在听。

    两个都要：Synapse 是房间本身，Element 是**截图那一侧** —— 材料里那两处
    占位要的正是 Element 里的截图，Synapse 单独活着截不出图来。
    """
    probe = Probe(9, "Synapse(8008) / Element(8080)", hard=True, segment=SEG_B)
    syn_ok, syn_detail = _port_open(SYNAPSE_ADDR)
    ele_ok, ele_detail = _port_open(ELEMENT_ADDR)
    summary = f"Synapse {syn_detail}；Element {ele_detail}"
    if syn_ok and ele_ok:
        return probe.pass_(summary)
    if syn_ok and not ele_ok:
        return probe.fail(f"{summary} —— 房间活着但截不了图")
    return probe.fail(summary)


def probe_10_evidence_clean() -> Probe:
    """`evidence/` 有没有未提交的残留。

    软项：它不挡跑，但会让收束那一步分不清「这次真跑产的」和「上次跑剩的」。
    跨轨契约 §B 要求各轨跑完证据一律 `git checkout -- evidence/` +
    `git clean -fd evidence/`，这条是那个约定的探测面。
    """
    probe = Probe(10, "evidence/ 残留", hard=False, segment=SEG_BOTH)
    try:
        out = _run(["git", "status", "--porcelain", "--", "evidence/"], cwd=ROOT)
    except Exception as exc:                              # noqa: BLE001
        return probe.fail(f"查不了 -> {type(exc).__name__}")
    rows = [ln for ln in out.stdout.splitlines() if ln.strip()]
    if not rows:
        return probe.pass_("干净")
    return probe.fail(
        f"{len(rows)} 处未提交 —— 收束前先 git checkout -- evidence/ "
        f"&& git clean -fd evidence/")


def probe_11_python_disk() -> Probe:
    """python3 版本 + 可用磁盘。

    磁盘是真跑日一个不显眼的坑：`--all-paths --domain-backend postgres`
    四条路径各产一束，加上 `make_evidence` 的 8 束，一轮下来是几百 MB 级。
    """
    probe = Probe(11, "python3 版本 / 可用磁盘", hard=False, segment=SEG_BOTH)
    version = ".".join(str(n) for n in sys.version_info[:3])
    try:
        usage = shutil.disk_usage(str(ROOT))
    except OSError as exc:
        return probe.fail(f"python3 {version}；磁盘查不了 -> {type(exc).__name__}")
    free_gb = usage.free / (1024 ** 3)
    detail = f"python3 {version}；可用磁盘 {free_gb:.1f} GB"
    if free_gb < 5:
        return probe.fail(f"{detail} —— 不足 5 GB，证据束产到一半可能写不下")
    return probe.pass_(detail)


PROBES = (
    probe_1_worktree,
    probe_2_psycopg,
    probe_3_pg_dsn,
    probe_4_egress_ip,
    probe_5_ssl_cert_file,
    probe_6_model_key,
    probe_7_local_pg,
    probe_8_room_ingress,
    probe_9_synapse_element,
    probe_10_evidence_clean,
    probe_11_python_disk,
)


def run_probes() -> list[Probe]:
    """逐条跑。**一条炸了不许带塌其余的** —— 探测器的价值全在覆盖面，
    一个笼统的失败等于没探。"""
    results: list[Probe] = []
    for index, func in enumerate(PROBES, start=1):
        try:
            results.append(func())
        except Exception as exc:                          # noqa: BLE001
            broken = Probe(index, f"{func.__name__}（探测本身炸了）",
                           hard=True, segment=SEG_BOTH)
            results.append(broken.fail(f"{type(exc).__name__}: {exc}"))
    return results


def summarize(probes: list[Probe]) -> dict:
    hard = [p for p in probes if p.hard]
    soft = [p for p in probes if not p.hard]
    hard_bad = [p for p in hard if not p.ok]
    soft_bad = [p for p in soft if not p.ok]
    if hard_bad:
        code, verdict = 1, "有硬缺 —— 真跑日跑不起来，先补上面标 [FAIL] 的硬项"
    elif soft_bad:
        code, verdict = 2, "硬项全绿，只有软缺 —— 能跑，知道缺什么就行"
    else:
        code, verdict = 0, "全绿"
    return {
        "hard_total": len(hard), "hard_ok": len(hard) - len(hard_bad),
        "soft_total": len(soft), "soft_ok": len(soft) - len(soft_bad),
        "exit_code": code, "verdict": verdict,
    }


def render(probes: list[Probe], summary: dict) -> None:
    emit("=" * 68)
    emit("真跑日前置探测 —— 2026-09-18 PolarDB 真实例 / 09-19 真 Matrix 房间")
    emit("=" * 68)
    emit("只探不改：不写文件、不连阿里云、不发房间消息、不调真模型、不碰 launchd。")
    emit("-" * 68)
    for probe in probes:
        mark = " OK " if probe.ok else "FAIL"
        tier = "硬" if probe.hard else "软"
        emit(f"  [{mark}] {probe.number:>2}. [{tier}|{probe.segment}] "
             f"{probe.title} -> {probe.detail}")
    emit("-" * 68)
    emit(f"硬项 {summary['hard_ok']}/{summary['hard_total']} 通过，"
         f"软项 {summary['soft_ok']}/{summary['soft_total']} 通过")
    emit(f"结论：{summary['verdict']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="真跑日前置探测（只探不改）。退出码 0 全绿 / 1 有硬缺 / 2 只有软缺。")
    parser.add_argument("--json", action="store_true",
                        help="输出机器可读的结论（runbook A 段第 1 步引用它）")
    args = parser.parse_args(argv)

    _remember_all()                       # 必须在任何输出之前
    probes = run_probes()
    summary = summarize(probes)

    if args.json:
        emit(json.dumps({"probes": [p.as_dict() for p in probes],
                         "summary": summary},
                        ensure_ascii=False, indent=2))
    else:
        render(probes, summary)
    return int(summary["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
