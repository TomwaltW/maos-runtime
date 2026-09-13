#!/usr/bin/env python3
"""PolarDB PostgreSQL 版 / 本机 pgvector 的冒烟脚本。

这个脚本回答一个问题，且只回答这一个问题：
**给定一条 PostgreSQL 连接串，MAOS 要用到的能力在这个实例上到底能不能用。**

前四步是**地基**：连得上、装得上 pgvector、全文检索跑得通、向量检索跑得通。
第 5 步把自己建的表删干净。

第 6 步（T115 加）是**业务纵切**：退款域建表 + 一条 case 往返。地基全绿不蕴含
业务域跑得起来 —— 中间隔着 CHECK 约束落没落、受理幂等是不是真幂等、以及
「回执与终态同事务」这条铁律 8 的落点在这台实例上成不成立。那三样才是评委那条
「以售后退款作为首个 PolarDB 业务纵切」真正在问的东西。

## 为什么是独立脚本

它**不 import maos**，是刻意的：

1. 任何人拿到一条 DSN 就能复跑，不需要先装 MAOS 本体、不需要仓库处于某个状态；
2. 它验的是**数据库这一侧**的地基，与 maos/store 那边的实现进度解耦 ——
   实现没写完的时候，这个脚本照样能回答「云上这台机器行不行」。

所以它跑绿**不等于** MAOS 的 PG 后端跑通了，只等于地基没问题。两件事别混。

## 依赖

需要 psycopg（v3，找不到时回退 psycopg2）。**刻意不写进 pyproject.toml 的
dependencies** —— MAOS 核心是零运行时依赖，这是一个验收工具，不是运行时的一部分：

    python3 -m pip install 'psycopg[binary]'

## 用法

    export MAOS_PG_DSN='postgresql://<user>:<pass>@<host>:<port>/<db>'
    python3 scripts/polardb_smoke.py

    python3 scripts/polardb_smoke.py --local              # 连本机 compose 起的 pgvector
    python3 scripts/polardb_smoke.py --dsn-env OTHER_VAR  # 从别的环境变量读

## 安全

DSN **只从环境变量读**，不接受命令行传入 —— 命令行会进 shell 历史。
脚本的任何一条输出都不回显连接串：

- 第 1 步（连接）失败时**只报驱动异常的类名**，不报 message ——
  多数驱动会把 host 拼进连接失败的 message 里。
- 第 2-6 步是 SQL 层错误，message 有诊断价值（比如「装不上 vector 是权限问题
  还是根本没这个扩展」正是本脚本最想知道的），所以报 message，但先过 _redact()。
- _redact() 是双保险：既替换从 DSN 解析出的具体片段，也用正则兜底任何
  形如 scheme://...@... 的串。

## 退出码

    0  六步全绿
    1  连上了，但有步骤失败
    2  没配 DSN（优雅退出，不抛栈）
    3  驱动没装
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import socket
import subprocess
import sys
import traceback
from urllib.parse import urlsplit

# 自建对象统一用这个前缀，方便万一残留时人工辨认和清理。
TABLE_FTS = "maos_smoke_fts"
TABLE_VEC = "maos_smoke_vec"
# 第 6 步的两张靶表。**刻意带前缀，不碰真的 refund_case / payment_observation** ——
# 这个脚本可能被对着人家的生产实例跑，往真业务表里插一条 case 是不可接受的。
TABLE_CASE = "maos_smoke_refund_case"
TABLE_OBS = "maos_smoke_payment_observation"

# 本机 compose 起 pgvector 时的缺省值。取自 deploy/docker-compose.yml 的
# ${POSTGRES_USER:-maos} 那几行 —— 是公开的本地开发缺省值，不是秘密。
# --local 存在的意义：本机自测时命令行里不必出现任何连接串。
LOCAL_DEFAULTS = {
    "user": "maos",
    "password": "maos-local-dev",
    "db": "maos",
    "host": "127.0.0.1",
    "port": "5432",
}

# 无条件子串替换：host / 口令 / 整条 DSN。铁律 7 的硬要求就是这几样
# ——「被人 grep 一遍都不该出现真实 host 或口令」。
_SECRETS_STRICT: list[str] = []
# 词边界替换：库名 / 用户名。这两样常是 maos 这种短词，无条件子串替换会误伤
# 正文（把 maos_smoke_fts 抹成 <redacted>_smoke_fts），\b 能避开下划线连写。
_SECRETS_WORD: list[str] = []


def _remember_secrets(dsn: str) -> None:
    """把 DSN 里的敏感片段登记下来，供 _redact() 逐个替换掉。"""
    _SECRETS_STRICT.append(dsn)
    try:
        parts = urlsplit(dsn)
    except ValueError:
        return
    for piece in (parts.hostname, parts.password, parts.netloc):
        if piece:
            _SECRETS_STRICT.append(str(piece))
    for piece in (parts.username, parts.path.lstrip("/")):
        if piece:
            _SECRETS_WORD.append(str(piece))


def _redact(text: str) -> str:
    """抹掉任何可能泄漏连接串的片段（铁律 7）。"""
    out = str(text)
    # 长的先替换，免得短片段先把长片段切碎导致漏网。
    for secret in sorted(set(_SECRETS_STRICT), key=len, reverse=True):
        if secret:
            out = out.replace(secret, "<redacted>")
    for secret in sorted(set(_SECRETS_WORD), key=len, reverse=True):
        if secret:
            out = re.sub(rf"\b{re.escape(secret)}\b", "<redacted>", out)
    # 兜底：没登记到的连接串形态也一并抹掉。
    out = re.sub(r"\b[a-zA-Z][\w+.-]*://[^\s'\"]+", "<dsn-redacted>", out)
    return out


class Reporter:
    """逐步记录成败。本脚本的价值全在「哪一步过了、哪一步没过」，
    所以绝不能一个 try 包住全部 —— 一个笼统的失败等于没做。"""

    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

    def ok(self, step: str, detail: str = "") -> None:
        self.rows.append((step, True, detail))
        print(f"  [ OK ] {step}" + (f" -> {_redact(detail)}" if detail else ""))

    def fail(self, step: str, detail: str = "") -> None:
        self.rows.append((step, False, detail))
        print(f"  [FAIL] {step}" + (f" -> {_redact(detail)}" if detail else ""))

    def note(self, step: str, detail: str = "") -> None:
        """观察项：记录现象，不计入成败、不影响退出码。"""
        print(f"  [note] {step}" + (f" -> {_redact(detail)}" if detail else ""))

    @property
    def all_green(self) -> bool:
        return bool(self.rows) and all(ok for _, ok, _ in self.rows)

    def summary(self) -> str:
        passed = sum(1 for _, ok, _ in self.rows if ok)
        return f"{passed}/{len(self.rows)} 步通过"


def _err(exc: BaseException) -> str:
    """SQL 层异常：类名 + 脱敏后的 message。"""
    msg = _redact(str(exc)).strip().replace("\n", " ")
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


def load_driver():
    """返回 (connect_callable, 驱动名)。找不到就退出码 3。"""
    try:
        import psycopg  # type: ignore

        return psycopg.connect, f"psycopg {psycopg.__version__}"
    except ImportError:
        pass
    try:
        import psycopg2  # type: ignore

        return psycopg2.connect, f"psycopg2 {psycopg2.__version__}"
    except ImportError:
        pass
    print("驱动没装：需要 psycopg（v3）或 psycopg2。")
    print("装一个再试：python3 -m pip install 'psycopg[binary]'")
    print("（刻意不写进 pyproject.toml 的 dependencies —— MAOS 核心零运行时依赖，")
    print("  这是验收工具，不是运行时的一部分。）")
    return None, None


def resolve_dsn(args: argparse.Namespace) -> str | None:
    if args.local:
        d = LOCAL_DEFAULTS
        user = os.environ.get("POSTGRES_USER", d["user"])
        password = os.environ.get("POSTGRES_PASSWORD", d["password"])
        db = os.environ.get("POSTGRES_DB", d["db"])
        port = os.environ.get("POSTGRES_PORT", d["port"])
        return f"postgresql://{user}:{password}@{d['host']}:{port}/{db}"
    return os.environ.get(args.dsn_env)


# --------------------------------------------------------------------------
# 两条观察项：链路是否加密、连不上时是不是白名单
#
# 两者都走 rep.note()，**不进 self.rows** —— 不改任何断言、不改退出码语义。
# 五步的断言强度是这个脚本的价值所在，观察项只补它看不见的那两维。
# --------------------------------------------------------------------------


def _ssl_state(conn) -> str:
    """这条连接**实际**有没有走 SSL。

    为什么要单独报：五步全绿**不蕴含**链路是加密的。`sslmode=prefer` 下
    五步同样全绿而链路是明文（deploy/polardb-live.md §3.5 记的正是这个坑），
    也就是说冒烟报告作为证据，在加密这一维上本来是哑的。

    只认客户端侧的事实。服务端 `SHOW ssl` = on 只说明它**支持** SSL，
    不说明你这条连接真的用上了 —— 那正是 `prefer` 骗过所有人的方式。
    """
    for getter in (
        lambda: conn.pgconn.ssl_in_use,  # psycopg3
        lambda: conn.info.ssl_in_use,  # psycopg2
    ):
        try:
            return "on" if getter() else "off"
        except Exception:  # noqa: BLE001 —— 驱动差异不该让冒烟脚本挂掉
            continue
    return "unknown（驱动不报 ssl_in_use）"


#: 取不到出口 IP 时的人话出路。口径与 `real_run_preflight.py::probe_4_egress_ip()`
#: 对齐 —— 两个脚本报的是同一个值，不许一边说得到、一边说不到。
_EGRESS_FALLBACK = "控制台白名单页面通常会显示当前来访 IP，照那个填"


def _egress_ip() -> str:
    """本机公网出口 IP。**不泄漏目标 host**，所以可以直接打印。

    白名单是按出口 IP 放行的，报障时人第一个要查的就是这个值 —— 真跑日（9/18）
    第一条要抄下来的也是它。

    🔴 **`-4` 不能省**（T142 补上，整合期 p10-f 在 preflight 那边先查实）：此前这条
    不带 `-4`，`dig` 可能走 IPv6 去问 `resolver1.opendns.com`，那一路到不了真的
    OpenDNS resolver，回 `NOERROR / ANSWER: 0` —— 于是本函数在这台机器上**恒返回
    「取不到」**，而它恰恰是白名单漂移时唯一要抄的值。强制 IPv4 之后同一条查询
    稳定返回真实出口 IP（实测连跑三次一致，偶发一次超时）。备用 resolver 走
    `208.67.222.222`（OpenDNS 的 IP 字面量，连域名解析这一跳都省了），因为实测
    第一台偶尔超时。

    取不到时**分档报**，各档的下一步完全不同：

    - `dig` 不在 → 装它，或直接去控制台看来访 IP；重试没意义
    - 查询超时 → 网络或 resolver 的事，**可以重试**
    - 空答案 → 查询被接管（DNS 劫持 / 分流），重试没用，只能去控制台看

    原来那句笼统的「取不到（dig 不可用或网络不通）」把这三条路混成一条，而且它
    还是错的 —— 本机 `dig` 在、网络也通，真因是走了 IPv6。

    ⚠️ 两条 `dig` 的坑，分档全靠它们（本轨实测）：

    1. `dig` 把「连不上」也写在 **stdout** 上（`;; connection timed out; no servers
       could be reached`）。那不是一个 IP，当成答案抄进白名单会把真跑日引到沟里 ——
       所以 `;` 开头的行一律不算答案。
    2. **但把它当成「空答案」也是错的**：那是**超时**（`dig` 退出码 9），下一步是
       「可以重试」，而空答案的下一步是「重试没用，去控制台看」。只按「答案行为空」
       归档会把这两条路混起来 —— 本轨实测撞到过一次（连跑两次，第二次 `dig` 自己
       超时，被归成了空答案）。所以这里**先看退出码 9 / `connection timed out`**，
       再看有没有答案行。
    """
    if not shutil.which("dig"):
        return f"取不到：dig 不可用 —— {_EGRESS_FALLBACK}"
    last = "取不到"
    for resolver in ("@resolver1.opendns.com", "@208.67.222.222"):
        try:
            out = subprocess.run(
                ["dig", "-4", "+short", "+time=5", "+tries=1",
                 "myip.opendns.com", resolver],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except subprocess.TimeoutExpired:
            # subprocess 这一层的超时：`dig` 连自己的 `+time` 都没兜住。
            last = "取不到：查询超时"
            continue
        except Exception as exc:  # noqa: BLE001 —— 拿不到就拿不到，不影响主流程
            # 只报类名，不报 message：口径同 `step1_connect`。
            last = f"取不到：取出口 IP 失败 -> {type(exc).__name__}"
            continue
        lines = [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
        answer = [ln for ln in lines if not ln.startswith(";")]
        if answer:
            return answer[-1]
        # `dig` 自己超时：退出码 9 = no servers could be reached。文本判据是兜底
        # （退出码语义跨版本稳，但 `+short` 的这行字更显眼，两条一起判）。
        if out.returncode == 9 or any("connection timed out" in ln for ln in lines):
            last = "取不到：查询超时"
            continue
        last = "取不到：查询返回空答案"
    return f"{last}（两台 resolver 都试过）—— {_EGRESS_FALLBACK}"


def _sqlstate(exc: BaseException | None) -> str | None:
    """驱动异常上的 SQLSTATE（五位字符）。psycopg3 叫 `sqlstate`，psycopg2 叫 `pgcode`。

    **只取这一个字段，绝不碰 message** —— 连接失败的 message 里几乎一定带 host
    （铁律 6，口径同 `step1_connect` 那条「只报驱动异常类名、不报 message」）。
    SQLSTATE 是五位字母数字的 PG 错误码，本身不可能夹带 host 或口令。

    它有没有值是 `_diagnose_unreachable()` 第三档的分水岭：**能报出 SQLSTATE 就说明
    这条连接真的走到了 PG 服务端**（那串码是 PG 自己发回来的），此时才谈得上鉴权层；
    报不出来就只是 TCP 握上了，中间设备也能让 TCP 握上。
    """
    if exc is None:
        return None
    for attr in ("sqlstate", "pgcode"):
        code = getattr(exc, attr, None)
        if code:
            return str(code)
    return None


def _diagnose_unreachable(
    dsn: str, rep: Reporter, exc: BaseException | None = None
) -> None:
    """连不上时分层诊断 DNS -> TCP，把「白名单没放行」从别的原因里择出来。

    为什么必须单独择：第 1 步刻意只报驱动异常的**类名**、不报 message（防 host
    泄漏），所以从脚本输出上看不出到底卡在哪一层。这一段就是拿来省那一轮排障往返的。

    ## 四档（T142 重写。原来只认「TCP 静默超时 = 白名单」一档，那是错的）

    | 档 | 症状 | 结论 |
    |---|---|---|
    | 1 | `getaddrinfo` 抛 | host 写错 / 实例名不对，**不是**白名单 |
    | 2 | `socket.timeout` | 白名单没放行（链路 A），并打出本机出口 IP |
    | 3 | `connect()` 成功 | 按驱动异常**有没有 SQLSTATE** 再分：无 → 优先怀疑白名单（链路 B）；有 → 才是真的鉴权层 |
    | 4 | `ConnectionRefusedError` / `ConnectionResetError` | 端口没在听，或被中间设备重置，不是白名单 |

    🔴 第 3 档是这次重写的要害。原来那句「握手成功 —— 网络通，问题在 PG 鉴权层
    （账号 / 库名 / SSL 策略）」在**真正的白名单场景下会把人指向错误的方向**：
    `docs/BACKLOG.md:1431` 实测记着，**公网地址前有 SLB 时，白名单不放行的症状是
    「TCP 握手成功 → 连接随即被断，`OperationalError`、无 SQLSTATE」**，不是静默
    超时 —— SLB 先替后端把 TCP 握上了，放不放行是它在应用层之前做的决定。照旧说法
    人会去翻口令和 SSL 策略，实际只要加一条白名单。**比没有判据更糟。**

    分水岭是 SQLSTATE 而不是别的：那串码是 PG 服务端自己发回来的，**能收到它就证明
    这条连接真的走到了 PG**；中间设备能让 TCP 握上，但发不出 SQLSTATE。所以
    「握上了 + 没有 SQLSTATE」= 还没走到 PG = 先怀疑链路，不是鉴权。

    两条链路的名字沿用 `docs/BACKLOG.md:1431`（那条实测记录自己起的名，本函数没另造）：
    **链路 A** = 直连实例地址（内网 / 无 SLB 前置），白名单不放行时包被丢掉，静默超时；
    **链路 B** = 公网地址经 SLB，白名单不放行时握得上、随即被断。

    ⚠️ **本轨没有复现过这两条链路**，而 BACKLOG:1431 的建议里写着「改之前先把两种链路
    各复现一次，别照抄本条」。复现不了的原因是硬的：真实例只由人类在 9/18 接入，本轨
    一律打本机容器（直连、无 SLB），造不出链路 B。所以这里的分档**照的是那条实测记录**，
    `test_polardb_smoke_t142.py` 里四档用的也是**构造出来的异常**，验的是「拿到这种症状
    会说哪句话」，不是「真链路上会不会出现这种症状」。真跑日撞上时按实际症状回头核这一段
    （已记进 `docs/DECISIONS.md` 与 `docs/BACKLOG.md` 的 `## task-t142`）。

    全程不打印 host，也不打印解析到的 IP —— 那是目标 host 的地址，打出来等于泄漏
    （铁律 6）。只报「解析到几个」。出口 IP 是**本机**的，不泄漏目标，可以打印
    （口径见 `_egress_ip()`）。
    """
    try:
        parts = urlsplit(dsn)
        host, port = parts.hostname, parts.port or 5432
    except ValueError:
        return
    if not host:
        return

    # ---- 第 1 档：DNS ----------------------------------------------------
    try:
        infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
        addrs = sorted({i[4][0] for i in infos})
        rep.note("诊断 DNS", f"解析成功，{len(addrs)} 个 A 记录")
    except Exception as dns_exc:  # noqa: BLE001
        rep.note("诊断 DNS", f"解析失败 -> {type(dns_exc).__name__}")
        # 「解析失败」不是一件事，是两件 —— 分不开就会在真跑日指错方向（整合期 p10-g）。
        # `EAI_AGAIN` 是解析器**没回话**（本机没网、DNS 服务器不可达、公司网关掐了 53），
        # 与 host 拼写、与白名单都无关；把它一并说成「host 写错了」，现场 WiFi 抖一下
        # 就会让人去查一个根本没错的拼写 —— 正是这套四档诊断立志消灭的那类误导。
        if getattr(dns_exc, "errno", None) == getattr(socket, "EAI_AGAIN", object()):
            rep.note(
                "诊断结论",
                "DNS **查不动**（EAI_AGAIN：解析器没回话）= **多半是本机没网 /"
                " DNS 不可达**，与 host 拼写、与白名单都无关。先确认本机能上网"
                "（`ping -c1 223.5.5.5`、`dig +short aliyun.com`），再复跑本脚本。",
            )
        else:
            rep.note(
                "诊断结论",
                "DNS 解析不出来 = **host 写错 / 实例名不对，不是白名单**。解析是本机到"
                " 公共 DNS 的事，白名单管的是对端放不放行，在这一层根本看不出来 ——"
                " 先核对 DSN 里的 host 拼写，以及那台实例是不是还在（被释放 / 改名）。"
                " 两个前提：本机 DNS 此刻是通的（不通会报 EAI_AGAIN，走上面那一档）；"
                " 本档只查 A 记录（AF_INET），纯 IPv6 的实例在这里也会显示解析不出来。",
            )
        return

    # ---- 第 2 / 3 / 4 档：TCP --------------------------------------------
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(8)
    try:
        sock.connect((addrs[0], port))
    except socket.timeout:
        rep.note("诊断 TCP", "静默超时（8s）")
        rep.note(
            "诊断结论",
            f"DNS 通 + TCP 静默超时 = **大概率是白名单没放行本机出口 IP（链路 A："
            f"直连实例地址，包被丢掉）**。本机出口 IP: {_egress_ip()} ——"
            f" 去控制台把它加进白名单再复跑。",
        )
    except (ConnectionRefusedError, ConnectionResetError) as tcp_exc:
        rep.note("诊断 TCP", f"{type(tcp_exc).__name__}")
        rep.note(
            "诊断结论",
            "连接被干脆地拒绝 / 重置 = **端口没在听，或路径上有设备把它掐了，"
            "不是白名单**（实例停了？端口写错了？企业网关拦了 5432？）。"
            "白名单不放行的症状是丢包静默超时（链路 A）或握手后被断（链路 B），"
            "不会回一个立刻的 RST。",
        )
    except Exception as tcp_exc:  # noqa: BLE001
        rep.note("诊断 TCP", f"失败 -> {type(tcp_exc).__name__}")
        rep.note(
            "诊断结论",
            "TCP 这一层报了个意料外的错，上面四档都对不上 —— 把类名连同"
            " `polardb_smoke.py` 的完整输出一起记下来再查，别猜。",
        )
    else:
        state = _sqlstate(exc)
        rep.note("诊断 TCP", "握手成功")
        if state:
            rep.note(
                "诊断结论",
                f"TCP 握手成功 + 驱动报了 SQLSTATE {state} = **确实走到了 PG 鉴权层**"
                f"（账号 / 口令 / 库名 / SSL 策略）。那串码是 PG 服务端自己发回来的，"
                f"收得到就说明链路通到了底 —— 这一档才轮得到查账号和 SSL 策略。",
            )
        else:
            rep.note(
                "诊断结论",
                f"TCP 握手成功但**驱动没报 SQLSTATE** = 连接在走到 PG 之前就被断了。"
                f"公网地址前有 SLB 时，白名单不放行的症状正是这个（握得上、随即被断，"
                f"BACKLOG:1431 实测），**不是**静默超时 —— 所以这里**优先怀疑白名单"
                f"（链路 B），不是鉴权层**。本机出口 IP: {_egress_ip()} ——"
                f" 先去控制台确认它在白名单里；确认在了，再查账号 / 口令 / 库名 /"
                f" SSL 策略。",
            )
    finally:
        sock.close()


# --------------------------------------------------------------------------
# 五步
# --------------------------------------------------------------------------


def step1_connect(connect, dsn: str, rep: Reporter):
    """连得上 + SELECT version()。失败意味着网络 / 白名单 / 账号问题。"""
    try:
        conn = connect(dsn, connect_timeout=10)
        conn.autocommit = True
    except Exception as exc:
        # 刻意只报类名：连接失败的 message 里几乎一定带 host。**这条口径没松** ——
        # 往下传的是异常对象本身，而诊断那头只从它身上取 SQLSTATE 那一个字段
        # （`_sqlstate()`，五位字母数字，夹带不了 host 或口令），仍然不碰 message。
        rep.fail("1. 连接 + SELECT version()", f"连不上：{type(exc).__name__}")
        _diagnose_unreachable(dsn, rep, exc)
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT version()")
            version = cur.fetchone()[0]
        rep.ok("1. 连接 + SELECT version()", version)
        # 观察项，不计成败：五步全绿不蕴含这条连接是加密的。见 _ssl_state()。
        rep.note("SSL", _ssl_state(conn))
        return conn
    except Exception as exc:
        rep.fail("1. 连接 + SELECT version()", _err(exc))
        conn.close()
        return None


def step2_extension(conn, rep: Reporter) -> bool:
    """CREATE EXTENSION vector —— 本脚本最想知道的那一条。

    云上 PG 常把扩展装载权限收在控制台的「插件管理」里，
    所以这一步失败先去控制台看一眼，不要在这里绕。
    """
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            row = cur.fetchone()
        ver = row[0] if row else "?"
        rep.ok("2. CREATE EXTENSION vector", f"pgvector {ver}")
        return True
    except Exception as exc:
        rep.fail("2. CREATE EXTENSION vector", _err(exc))
        return False


def step3_fulltext(conn, rep: Reporter) -> bool:
    """全文通道：to_tsvector + ts_rank 跑通，并断言命中集合。"""
    docs = [
        ("d1", "the quick brown fox jumps over the lazy dog"),
        ("d2", "postgresql full text search with tsvector and tsquery"),
        ("d3", "brown bears eat fish in the river"),
    ]
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {TABLE_FTS}")
            cur.execute(f"CREATE TABLE {TABLE_FTS} (id text PRIMARY KEY, body text)")
            for doc_id, body in docs:
                cur.execute(f"INSERT INTO {TABLE_FTS} (id, body) VALUES (%s, %s)", (doc_id, body))
            # 'fox & brown' 只有 d1 同时含两个词 —— 命中集合是确定的，可以硬断言。
            cur.execute(
                f"""
                SELECT id, ts_rank(to_tsvector('english', body), q) AS score
                FROM {TABLE_FTS}, to_tsquery('english', 'fox & brown') q
                WHERE to_tsvector('english', body) @@ q
                ORDER BY score DESC, id ASC
                """
            )
            hits = cur.fetchall()
    except Exception as exc:
        rep.fail("3. 全文 to_tsvector + ts_rank", _err(exc))
        return False

    ids = [str(r[0]) for r in hits]
    if ids != ["d1"]:
        rep.fail("3. 全文 to_tsvector + ts_rank", f"命中集合应为 ['d1']，实得 {ids}")
        return False
    score = float(hits[0][1])
    if not score > 0:
        rep.fail("3. 全文 to_tsvector + ts_rank", f"ts_rank 应 > 0，实得 {score}")
        return False
    rep.ok("3. 全文 to_tsvector + ts_rank", f"命中 {ids}，ts_rank={score:.6f}（越大越相关）")

    # 观察项：PG 自带的分词器不认中文，这里只记录现象，口径见 deploy/polardb.md（T10）。
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_tsvector('simple', %s)::text", ("退款订单已经超时未处理",))
            zh = cur.fetchone()[0]
        rep.note("   （观察）中文 to_tsvector('simple')", zh)
    except Exception as exc:
        rep.note("   （观察）中文 to_tsvector('simple')", _err(exc))
    return True


def step4_vector(conn, rep: Reporter) -> bool:
    """向量通道：vector 列 + <=> 算子 + 排序。构造已知相似度，硬断言全序。

    查询向量是 [1,0,0]，<=> 是余弦距离（越小越相关）：
        a = [1, 0, 0]     距离 0
        c = [0.9, 0.1, 0] 距离 ≈ 0.0062
        b = [0, 1, 0]     距离 1
    所以顺序必须是 a, c, b。
    """
    rows = [("a", "[1,0,0]"), ("b", "[0,1,0]"), ("c", "[0.9,0.1,0]")]
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {TABLE_VEC}")
            cur.execute(f"CREATE TABLE {TABLE_VEC} (id text PRIMARY KEY, embedding vector(3))")
            for doc_id, vec in rows:
                cur.execute(
                    f"INSERT INTO {TABLE_VEC} (id, embedding) VALUES (%s, %s::vector)",
                    (doc_id, vec),
                )
            cur.execute(
                f"""
                SELECT id, embedding <=> %s::vector AS distance
                FROM {TABLE_VEC}
                ORDER BY distance ASC, id ASC
                """,
                ("[1,0,0]",),
            )
            hits = cur.fetchall()
    except Exception as exc:
        rep.fail("4. 向量 vector 列 + <=> 排序", _err(exc))
        return False

    ids = [str(r[0]) for r in hits]
    if ids != ["a", "c", "b"]:
        rep.fail("4. 向量 vector 列 + <=> 排序", f"顺序应为 ['a','c','b']，实得 {ids}")
        return False
    dists = ", ".join(f"{r[0]}={float(r[1]):.6f}" for r in hits)
    rep.ok("4. 向量 vector 列 + <=> 排序", f"top-1={ids[0]}，余弦距离 {dists}（越小越相关）")
    return True


def step5_cleanup(conn, rep: Reporter) -> bool:
    """清理自建表 —— 别在人家实例上留垃圾。"""
    tables = (TABLE_FTS, TABLE_VEC, TABLE_OBS, TABLE_CASE)
    try:
        with conn.cursor() as cur:
            for table in tables:
                cur.execute(f"DROP TABLE IF EXISTS {table}")
        rep.ok("5. 清理临时表", " / ".join(tables) + " 已删")
        return True
    except Exception as exc:
        rep.fail("5. 清理临时表", _err(exc))
        return False


# 第 6 步用到的 DDL。**照 maos/domain/refund/schema.sql 的形状写，但只取两张表**，
# 且表名带 maos_smoke_ 前缀。这里的重复是有意的：本脚本**不 import maos**
# （它要能在一台只有 psycopg、没有本仓库的机器上跑），所以拿不到翻译器。
# 代价是这两段 DDL 与 schema.sql 会漂 —— 但漂了也只影响这一步的判据强度，
# 不影响任何真表；真表的形状由 maos/tests/test_ddl_translate.py 钉着。
_DDL_CASE = f"""
CREATE TABLE {TABLE_CASE} (
    tenant_id      TEXT NOT NULL,
    case_id        TEXT NOT NULL,
    amount_claimed double precision NOT NULL,
    biz_status     TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (tenant_id, case_id),
    CHECK (biz_status IN ('submitted', 'approved', 'gateway_accepted',
                          'processing', 'settled', 'rejected', 'compensated'))
)
"""
_DDL_OBS = f"""
CREATE TABLE {TABLE_OBS} (
    tenant_id           TEXT NOT NULL,
    case_id             TEXT NOT NULL,
    request_id          TEXT NOT NULL,
    gateway_code        TEXT NOT NULL,
    observed_state      TEXT NOT NULL,
    observed_at         TEXT NOT NULL,
    actor_invocation_id TEXT NOT NULL,
    PRIMARY KEY (tenant_id, case_id, request_id, observed_at)
)
"""


def step6_refund_case(conn, rep: Reporter) -> bool:
    """退款域建表 + 一条 case 往返 —— 这一步验的是**业务纵切**，不是地基。

    前五步证明的是「这台实例能当 MAOS 的库用」（连得上、有扩展、两条检索通道通）。
    第 6 步补的是评委那条建议真正问的东西：**退款这个业务域跑不跑得起来**。

    四个判据，逐条对应铁律 8 在 SQL 层的落点：

      1. 建表 —— `CHECK` 约束真的落到 PG 上了（写一个不存在的状态要被库拒掉）。
         约束掉了不报错，只是库上少了一道拦截，而那道拦截挡的正是「biz_status
         写进一个不存在的状态」。
      2. 受理幂等 —— `ON CONFLICT (tenant_id, case_id) DO NOTHING` 重放不覆盖。
         用 DO NOTHING 而不是 DO UPDATE：后者会把已经推进的案子静悄悄倒回
         submitted（`guard.create_case` 的 docstring 点名不许）。
      3. **回执与终态同事务** —— 这是本步的题眼。settled 与它的 `payment_observation`
         必须一起落、一起回滚；分成两笔提交的话，中间崩一次就留下「说到账了、
         但没有任何回执」的案子，那正是铁律 8 要防的东西。所以这里显式关掉
         autocommit 走一次真事务，并**故意让它中途失败一次**，验回滚。
      4. 终态可读回 —— 事务提交之后，状态是 settled 且回执恰好一行。

    **不 import maos**：这个脚本要能在一台只有 psycopg、没有本仓库的机器上跑
    （运维拿去探实例通不通）。所以这里全是直连 SQL，DDL 也在本文件里另写一份，
    理由见 `_DDL_CASE` 上面那段注释。
    """
    tenant, case = "tnt-smoke", "case-smoke-0001"
    now = "2026-01-01T00:00:00+00:00"
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {TABLE_OBS}")
            cur.execute(f"DROP TABLE IF EXISTS {TABLE_CASE}")
            cur.execute(_DDL_CASE)
            cur.execute(_DDL_OBS)
    except Exception as exc:
        rep.fail("6. 退款域建表 + case 往返", _err(exc))
        return False

    # ① CHECK 约束真的在
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {TABLE_CASE} (tenant_id, case_id, amount_claimed,"
                f" biz_status, created_at) VALUES (%s,%s,%s,%s,%s)",
                (tenant, "case-bogus", 1.0, "teleported", now))
        rep.fail("6. 退款域建表 + case 往返",
                 "CHECK 约束没落到 PG 上：写进了一个不存在的 biz_status")
        return False
    except Exception:
        pass                                  # 被库拒掉才是对的

    try:
        # ② 受理 + 重放幂等
        with conn.cursor() as cur:
            for _ in range(2):
                cur.execute(
                    f"INSERT INTO {TABLE_CASE} (tenant_id, case_id, amount_claimed,"
                    f" biz_status, created_at) VALUES (%s,%s,%s,%s,%s)"
                    f" ON CONFLICT (tenant_id, case_id) DO NOTHING",
                    (tenant, case, 3200.0, "submitted", now))
            cur.execute(f"SELECT count(*) FROM {TABLE_CASE} WHERE case_id = %s", (case,))
            n_case = int(cur.fetchone()[0])
        if n_case != 1:
            rep.fail("6. 退款域建表 + case 往返", f"受理重放攒出了 {n_case} 行，应为 1")
            return False

        # 推进到 processing（这几步不涉及权威事实，逐条提交即可）
        with conn.cursor() as cur:
            for status in ("approved", "gateway_accepted", "processing"):
                cur.execute(
                    f"UPDATE {TABLE_CASE} SET biz_status = %s"
                    f" WHERE tenant_id = %s AND case_id = %s", (status, tenant, case))

        # ③ 同事务：先制造一次中途失败，验「回执落了但状态没改」不会留下来
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {TABLE_OBS} (tenant_id, case_id, request_id,"
                    f" gateway_code, observed_state, observed_at, actor_invocation_id)"
                    f" VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (tenant, case, "req-1", "0000", "settled", now, "inv-boom"))
                cur.execute(f"UPDATE {TABLE_CASE} SET biz_status = %s"
                            f" WHERE tenant_id = %s AND case_id = %s",
                            ("teleported", tenant, case))       # CHECK 会拒
            conn.commit()
        except Exception:
            conn.rollback()
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {TABLE_OBS}")
            leaked = int(cur.fetchone()[0])
            conn.commit()
        if leaked:
            rep.fail("6. 退款域建表 + case 往返",
                     f"事务回滚之后还剩 {leaked} 条回执 —— 回执与终态没有同生共死")
            return False

        # ③（续）正路：回执与 settled 同一笔提交
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {TABLE_OBS} (tenant_id, case_id, request_id,"
                f" gateway_code, observed_state, observed_at, actor_invocation_id)"
                f" VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (tenant, case, "req-1", "0000", "settled", now, "inv-ok"))
            cur.execute(f"UPDATE {TABLE_CASE} SET biz_status = %s"
                        f" WHERE tenant_id = %s AND case_id = %s",
                        ("settled", tenant, case))
        conn.commit()
    except Exception as exc:
        conn.rollback()
        rep.fail("6. 退款域建表 + case 往返", _err(exc))
        return False
    finally:
        conn.autocommit = True

    # ④ 读回
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT biz_status FROM {TABLE_CASE}"
                        f" WHERE tenant_id = %s AND case_id = %s", (tenant, case))
            status = str(cur.fetchone()[0])
            cur.execute(f"SELECT count(*) FROM {TABLE_OBS} WHERE case_id = %s", (case,))
            n_obs = int(cur.fetchone()[0])
    except Exception as exc:
        rep.fail("6. 退款域建表 + case 往返", _err(exc))
        return False

    if status != "settled" or n_obs != 1:
        rep.fail("6. 退款域建表 + case 往返",
                 f"终态应为 settled + 1 条回执，实得 {status} + {n_obs} 条")
        return False
    rep.ok("6. 退款域建表 + case 往返",
           f"CHECK 生效、受理幂等、回执与终态同事务；biz_status={status}，回执 {n_obs} 条")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="PolarDB PostgreSQL 版 / 本机 pgvector 冒烟（六步：五步地基 + 退款域纵切）",
    )
    parser.add_argument(
        "--dsn-env",
        default="MAOS_PG_DSN",
        metavar="VAR",
        help="从哪个环境变量读 DSN（缺省 MAOS_PG_DSN）。不接受直接传连接串。",
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="连本机 compose 起的 pgvector，用其公开缺省值拼 DSN，命令行不出现连接串。",
    )
    args = parser.parse_args(argv)

    print("=" * 68)
    print("PolarDB / pgvector 冒烟 —— 六步（1-4 地基，6 退款域纵切，5 清理）")
    # 第 5 步的编号在第 6 步之后打印，这是对的不是乱序：清理按定义必须最后跑，
    # 而它的编号是既有证据里写死的，不为了好看去改。
    print("=" * 68)

    connect, driver = load_driver()
    if connect is None:
        return 3
    print(f"驱动：{driver}")

    dsn = resolve_dsn(args)
    if not dsn:
        print(f"没配 DSN：环境变量 {args.dsn_env} 是空的。")
        print(f"  export {args.dsn_env}='postgresql://<user>:<pass>@<host>:<port>/<db>'")
        print("  或者用 --local 连本机 compose 起的 pgvector。")
        return 2
    # 占位符没替换是最容易犯的一种错：文档里的样例串被原样 export 进来，
    # 于是「配了」和「配对了」被混成同一件事，最后表现为一个没头没脑的
    # OperationalError。这里当场说清，别让它伪装成「连不上」。
    if re.search(r"<(user|pass|password|host|port|db|dbname)>", dsn):
        print(f"DSN 是占位符模板，没有替换成真实连接串：环境变量 {args.dsn_env}")
        print("  里面还留着 <user> / <host> / <port> 这类尖括号占位符。")
        print("  把它换成实例的真实连接串再跑。")
        return 2
    _remember_secrets(dsn)

    target = "本机 pgvector（--local）" if args.local else f"环境变量 {args.dsn_env}"
    print(f"目标：{target}")
    print("-" * 68)

    conn = step1_connect(connect, dsn, rep := Reporter())
    if conn is None:
        print("-" * 68)
        print(f"结论：{rep.summary()} —— 连不上，后面五步没跑。")
        return 1

    try:
        has_vector = step2_extension(conn, rep)
        step3_fulltext(conn, rep)
        if has_vector:
            step4_vector(conn, rep)
        else:
            # 扩展没装上时第 4 步必然失败，如实记为未跑，不伪装成通过。
            rep.fail("4. 向量 vector 列 + <=> 排序", "跳过：第 2 步没拿到 vector 扩展")
        # 第 6 步排在清理前面：它自建自清的两张表也由第 5 步一并删掉。
        step6_refund_case(conn, rep)
        step5_cleanup(conn, rep)
    finally:
        conn.close()

    print("-" * 68)
    print(f"结论：{rep.summary()}")
    return 0 if rep.all_green else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n中断。")
        sys.exit(130)
    except Exception:  # 兜底：任何漏网异常也不许把 DSN 带进栈回溯
        print("未预期的异常（栈回溯已脱敏）：")
        print(_redact(traceback.format_exc()))
        sys.exit(1)
