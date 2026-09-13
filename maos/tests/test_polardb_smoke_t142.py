"""`scripts/polardb_smoke.py` 的诊断判据（T142）。

这个文件守两样东西：

1. **连不上时的分层诊断分得清四档**，而且每一档的结论指向**正确的下一步**。
   要害在第 3 档：原来只要 TCP 握得上就断言「网络通，问题在 PG 鉴权层」，
   而 `docs/BACKLOG.md:1431` 实测记着 —— **公网地址前有 SLB 时，白名单不放行的
   症状恰恰是「TCP 握手成功 → 连接随即被断、无 SQLSTATE」**。照旧说法人会去翻
   口令和 SSL 策略，实际只要加一条白名单。**指错方向比没有判据更糟**，所以这
   一档单独用两条测试钉住（有 SQLSTATE / 没有 SQLSTATE）。

2. **出口 IP 那条探测带 `-4`**，取不到时分档报。`dig` 不带 `-4` 时可能走 IPv6
   去问 `resolver1.opendns.com`，那一路到不了真的 OpenDNS resolver、回空答案，
   于是本机恒报「取不到」—— 而它是白名单漂移时唯一要抄的值（整合期 p10-f 查实，
   `real_run_preflight.py::probe_4_egress_ip()` 已改，冒烟脚本这份由 T142 补上）。

**为什么不塞进 `test_real_run_preflight.py`**：那个文件是 T141 为 preflight 写的，
两个脚本各有各的分档与话术，判据混进一个文件之后「这条到底在守哪个脚本」就得靠
读实现才答得出（跨轨契约 §A 已把本文件单列）。

诊断全程不碰网络：DNS 与 socket 都是替身，出口 IP 也打桩 —— 一条会真连外网的
测试在别人的机器上就是随机红。
"""

from __future__ import annotations

import importlib
import socket
import subprocess

import pytest

#: 哨兵串：一眼能认出来，且绝不会在正常输出里自然出现。口径同
#: `test_real_run_preflight.py` —— 口令与 host **单列**，整条 DSN 被抹掉不蕴含
#: 它俩被抹掉，而泄漏其中任何一个都够让人连上那台实例。
DSN_HOST = "sentinel-polardb-host.example.internal"
DSN_PASSWORD = "SENTINEL-PG-PASSWORD-9f2a"
FAKE_DSN = f"postgresql://sentinel_pg_user:{DSN_PASSWORD}@{DSN_HOST}:5432/sentinel_pg_db"

#: 打桩的出口 IP。真值不进测试 —— 它随网络变，钉死就是一条会自己红的断言。
STUB_EGRESS = "203.0.113.77"


@pytest.fixture()
def smoke(monkeypatch):
    """每条测试拿一个干净的模块：`_SECRETS_*` 是模块级的，跨测试会串。"""
    module = importlib.import_module("scripts.polardb_smoke")
    monkeypatch.setattr(module, "_SECRETS_STRICT", [])
    monkeypatch.setattr(module, "_SECRETS_WORD", [])
    return module


class _FakeSocket:
    """一个只按指定方式失败（或干脆成功）的 socket 替身。"""

    def __init__(self, outcome: BaseException | None) -> None:
        self._outcome = outcome
        self.closed = False

    def settimeout(self, _timeout) -> None:
        pass

    def connect(self, _addr) -> None:
        if self._outcome is not None:
            raise self._outcome

    def close(self) -> None:
        self.closed = True


def _wire(smoke, monkeypatch, *, resolves=True, tcp: BaseException | None = None):
    """把 DNS、socket、出口 IP 三处替身接上，返回那个 fake socket（好断言它关了）。"""
    if resolves:
        monkeypatch.setattr(
            smoke.socket, "getaddrinfo",
            lambda *a, **k: [(2, 1, 6, "", ("198.51.100.9", 5432))])
    else:
        def _boom(*_a, **_k):
            raise socket.gaierror(8, "nodename nor servname provided")
        monkeypatch.setattr(smoke.socket, "getaddrinfo", _boom)

    sock = _FakeSocket(tcp)
    monkeypatch.setattr(smoke.socket, "socket", lambda *a, **k: sock)
    monkeypatch.setattr(smoke, "_egress_ip", lambda: STUB_EGRESS)
    return sock


def _diagnose(smoke, capsys, exc: BaseException | None = None) -> str:
    """跑一次诊断，把 stdout 收回来。`note()` 直接 print，且会过 `_redact()`。"""
    smoke._remember_secrets(FAKE_DSN)
    smoke._diagnose_unreachable(FAKE_DSN, smoke.Reporter(), exc)
    return capsys.readouterr().out


class _DriverError(Exception):
    """psycopg 那类驱动异常的替身。psycopg3 把错误码挂在 `sqlstate` 上。"""

    def __init__(self, message: str, sqlstate: str | None = None) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


# ============================================================ 四档诊断
def test_dns_failure_blames_the_host_not_the_whitelist(smoke, monkeypatch, capsys):
    """第 1 档：DNS 解析不出来 → **host 写错 / 实例名不对，不是白名单**。

    解析是本机到公共 DNS 的事，对端放不放行在这一层根本看不出来。把它说成白名单
    会让人跑去控制台加 IP，而实际上 host 拼错了 —— 加多少条都连不上。
    """
    _wire(smoke, monkeypatch, resolves=False)

    out = _diagnose(smoke, capsys)

    assert "解析失败" in out
    assert "不是白名单" in out, "第 1 档必须显式把白名单排除掉"
    assert "host" in out or "实例名" in out, "要指向 host / 实例名这条下一步"
    assert "鉴权" not in out, "DNS 都没通，不该提鉴权层"


def test_tcp_timeout_points_at_the_whitelist_and_prints_the_egress_ip(
    smoke, monkeypatch, capsys
):
    """第 2 档：TCP 静默超时 → 白名单没放行（链路 A），并打出本机出口 IP。

    出口 IP 必须打出来 —— 那是这一档唯一要抄进控制台的值。
    """
    _wire(smoke, monkeypatch, tcp=socket.timeout("timed out"))

    out = _diagnose(smoke, capsys)

    assert "静默超时" in out
    assert "白名单" in out
    assert "链路 A" in out, "要点名是哪条链路，两条链路的症状不一样"
    assert STUB_EGRESS in out, "这一档不打出口 IP 等于让人自己再查一轮"


def test_handshake_without_sqlstate_suspects_the_whitelist_not_auth(
    smoke, monkeypatch, capsys
):
    """🔴 第 3 档之一：**握手成功但驱动没报 SQLSTATE → 优先怀疑白名单（链路 B）**。

    这条是本文件存在的主要理由。`docs/BACKLOG.md:1431` 实测：公网地址前有 SLB 时，
    白名单不放行的症状是「TCP 握得上、连接随即被断、`OperationalError`、无 SQLSTATE」。
    SLB 先替后端把 TCP 握上了，放不放行是它在应用层之前做的决定。

    原来这一档无条件说「网络通，问题在 PG 鉴权层（账号 / 库名 / SSL 策略）」——
    在真正的白名单场景下**把人指向了错误的方向**。这条测试守的就是那句话不许回来。
    """
    _wire(smoke, monkeypatch)

    out = _diagnose(smoke, capsys, _DriverError("connection closed", sqlstate=None))

    assert "握手成功" in out
    assert "白名单" in out, "没有 SQLSTATE 时必须先怀疑白名单"
    assert "链路 B" in out
    assert STUB_EGRESS in out, "既然怀疑白名单，就得把要抄的出口 IP 给出来"
    # 🔴 那句错误结论不许回来：不许在没有 SQLSTATE 时断言问题出在鉴权层。
    assert "网络通，问题在 PG 鉴权层" not in out, \
        "这正是 BACKLOG:1431 推翻的那句话 —— 公网 SLB 前置时它把人指向口令和 SSL 策略"


def test_handshake_with_sqlstate_is_the_real_auth_layer(smoke, monkeypatch, capsys):
    """第 3 档之二：握手成功 **且** 驱动报了 SQLSTATE → 这才是真的鉴权层。

    分水岭是 SQLSTATE 而不是别的：那串码是 PG 服务端自己发回来的，收得到就证明这
    条连接真的走到了 PG。中间设备能让 TCP 握上，但发不出 SQLSTATE。
    """
    _wire(smoke, monkeypatch)

    out = _diagnose(smoke, capsys,
                    _DriverError("password authentication failed", sqlstate="28P01"))

    assert "握手成功" in out
    assert "28P01" in out, "SQLSTATE 是这一档的判据，要打出来"
    assert "鉴权层" in out
    assert "链路 B" not in out, "有 SQLSTATE 就不该再把人往白名单上引"


@pytest.mark.parametrize("exc", [
    ConnectionRefusedError(61, "Connection refused"),
    ConnectionResetError(54, "Connection reset by peer"),
])
def test_refused_or_reset_is_not_the_whitelist(smoke, monkeypatch, capsys, exc):
    """第 4 档：被拒绝 / RST → 端口没在听，或被中间设备重置，**不是白名单**。

    `ConnectionResetError` 此前落在那条只打类名、不给结论的 `except Exception`
    兜底里 —— 有症状没结论，等于让人自己再猜一轮。
    """
    _wire(smoke, monkeypatch, tcp=exc)

    out = _diagnose(smoke, capsys)

    assert type(exc).__name__ in out
    assert "不是白名单" in out, "这一档必须显式把白名单排除掉"
    assert "端口" in out, "要指向「端口没在听 / 被设备掐了」这条下一步"


def test_the_socket_is_closed_on_every_branch(smoke, monkeypatch, capsys):
    """四档都走 `finally: sock.close()`，一条都不许漏 —— 漏了就是 fd 泄漏。"""
    for tcp, exc in (
        (socket.timeout("t"), None),
        (ConnectionRefusedError(61, "refused"), None),
        (None, _DriverError("closed", sqlstate=None)),
        (None, _DriverError("auth", sqlstate="28P01")),
        (OSError("something else"), None),
    ):
        sock = _wire(smoke, monkeypatch, tcp=tcp)
        _diagnose(smoke, capsys, exc)
        assert sock.closed, f"{tcp or exc} 那一档没关 socket"


def test_diagnosis_never_leaks_the_host_or_password(smoke, monkeypatch, capsys):
    """铁律 6：跑遍每一档，整份输出里连片段都不许出现 host / 口令 / 整条 DSN。

    诊断拿到驱动异常之后只取 SQLSTATE 那一个字段，**不碰 message** —— 而驱动
    连接失败的 message 里几乎一定带 host。这条把那个口径钉住：故意把哨兵 host
    塞进异常 message，它仍然不许出现在输出里。
    """
    leaky = _DriverError(f"could not connect to {DSN_HOST}: timeout", sqlstate=None)
    for tcp, exc in (
        (None, leaky),
        (socket.timeout("t"), leaky),
        (ConnectionResetError(54, "reset"), leaky),
    ):
        _wire(smoke, monkeypatch, tcp=tcp)
        out = _diagnose(smoke, capsys, exc)
        for sentinel in (FAKE_DSN, DSN_HOST, DSN_PASSWORD):
            assert sentinel not in out, f"输出里漏了 {sentinel!r}"

    # DNS 那一档也走一遍（它在 TCP 之前 return，分支不同）。
    _wire(smoke, monkeypatch, resolves=False)
    out = _diagnose(smoke, capsys, leaky)
    for sentinel in (FAKE_DSN, DSN_HOST, DSN_PASSWORD):
        assert sentinel not in out, f"DNS 档漏了 {sentinel!r}"


def test_resolved_addresses_are_counted_not_printed(smoke, monkeypatch, capsys):
    """解析到的 IP 是**目标 host 的地址**，打出来等于泄漏 —— 只报「解析到几个」。"""
    monkeypatch.setattr(
        smoke.socket, "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("198.51.100.9", 5432)),
                         (2, 1, 6, "", ("198.51.100.10", 5432))])
    monkeypatch.setattr(smoke.socket, "socket",
                        lambda *a, **k: _FakeSocket(socket.timeout("t")))
    monkeypatch.setattr(smoke, "_egress_ip", lambda: STUB_EGRESS)

    out = _diagnose(smoke, capsys)

    assert "2 个 A 记录" in out
    assert "198.51.100.9" not in out and "198.51.100.10" not in out, \
        "解析到的地址是目标 host 的，不许打印"


# ============================================================ 出口 IP
def _fake_dig(monkeypatch, smoke, results):
    """按调用次序把 `dig` 的结果喂回去。

    每项是 stdout 字符串、`(stdout, 退出码)` 元组，或一个要抛的异常。
    """
    calls: list[list[str]] = []

    def _run(cmd, **_kw):
        calls.append(list(cmd))
        outcome = results[min(len(calls) - 1, len(results) - 1)]
        if isinstance(outcome, BaseException):
            raise outcome
        stdout, code = outcome if isinstance(outcome, tuple) else (outcome, 0)
        return subprocess.CompletedProcess(cmd, code, stdout=stdout, stderr="")

    monkeypatch.setattr(smoke.shutil, "which", lambda _name: "/usr/bin/dig")
    monkeypatch.setattr(smoke.subprocess, "run", _run)
    return calls


def test_egress_ip_forces_ipv4(smoke, monkeypatch):
    """🔴 `-4` 不能省。不带它 `dig` 可能走 IPv6 去问 OpenDNS，那一路回空答案，
    于是本机恒报「取不到」—— 而它是白名单漂移时唯一要抄的值。"""
    calls = _fake_dig(monkeypatch, smoke, ["203.0.113.5\n"])

    assert smoke._egress_ip() == "203.0.113.5"
    assert "-4" in calls[0], f"dig 没带 -4：{calls[0]}"
    assert "myip.opendns.com" in calls[0]


def test_egress_ip_falls_back_to_the_second_resolver(smoke, monkeypatch):
    """第一台 resolver 超时就试 `@208.67.222.222`（IP 字面量，省掉解析那一跳）。

    实测第一台偶发超时；没有备用的话一次抖动就让这个值取不到。
    """
    calls = _fake_dig(monkeypatch, smoke,
                      [subprocess.TimeoutExpired("dig", 20), "203.0.113.5\n"])

    assert smoke._egress_ip() == "203.0.113.5"
    assert len(calls) == 2, "第一台超时之后该试第二台"
    assert "@208.67.222.222" in calls[1], f"备用 resolver 不对：{calls[1]}"


def test_egress_ip_never_returns_digs_connection_timed_out_line(smoke, monkeypatch):
    """🔴 `dig` 把「连不上」也写在 **stdout** 上（`;; connection timed out`）。

    那不是一个 IP。当成答案返回、被人抄进控制台白名单，真跑日就会被引到沟里 ——
    而且现场看不出来（白名单里确实多了一条，只是那条不是任何人的 IP）。
    """
    _fake_dig(monkeypatch, smoke,
              [(";; connection timed out; no servers could be reached\n", 9)])

    result = smoke._egress_ip()

    assert "connection timed out" not in result
    assert result.startswith("取不到"), f"该报取不到，实际 {result!r}"


@pytest.mark.parametrize("results", [
    # 退出码 9 = no servers could be reached，`+short` 下那行字也在 stdout 上。
    [(";; connection timed out; no servers could be reached\n", 9)],
    # 退出码语义万一跨版本变了，文本判据要能兜住。
    [(";; connection timed out; no servers could be reached\n", 0)],
])
def test_digs_own_timeout_is_a_timeout_not_an_empty_answer(smoke, monkeypatch, results):
    """🔴 `dig` **自己**超时归「查询超时」，**不是**「空答案」——两档的下一步相反。

    超时 = 网络或 resolver 抖了一下，**可以重试**；空答案 = 查询被接管（DNS 劫持 /
    分流），**重试没用，只能去控制台看**。只按「答案行为空」归档会把这两条路混起来。

    本轨实测撞到过：连跑两次 `_egress_ip()`，第一次返回真实出口 IP，第二次 `dig`
    自己超时 —— 当时被归成了「空答案」，照它走会去查一个根本不存在的 DNS 劫持。
    直接连跑 `dig -4 …` 五次全中，证实那只是一次抖动。
    """
    _fake_dig(monkeypatch, smoke, results)

    result = smoke._egress_ip()

    assert "查询超时" in result, f"dig 自己超时该归超时档，实际 {result!r}"
    assert "空答案" not in result, "归错档了 —— 超时可重试，空答案重试没用"


@pytest.mark.parametrize("results, expect", [
    ([subprocess.TimeoutExpired("dig", 20)], "查询超时"),
    ([""], "查询返回空答案"),
])
def test_egress_ip_reports_distinct_failure_modes(smoke, monkeypatch, results, expect):
    """取不到时**分档报**：超时（可重试）与空答案（重试没用，去控制台看）的下一步
    完全不同。原来那句笼统的「取不到（dig 不可用或网络不通）」把它们混成一条，
    而且那句话本身还是错的 —— 本机 `dig` 在、网络也通，真因是走了 IPv6。"""
    _fake_dig(monkeypatch, smoke, results)

    result = smoke._egress_ip()

    assert expect in result, f"该报 {expect!r}，实际 {result!r}"
    assert "dig 不可用或网络不通" not in result, "那句笼统措辞不许回来"
    assert "控制台" in result, "取不到时要给一条人话出路"


def test_egress_ip_says_dig_is_missing_without_running_it(smoke, monkeypatch):
    """第三档：`dig` 根本不在 → 单独报，且**不去跑它**（跑了是 FileNotFoundError）。"""
    monkeypatch.setattr(smoke.shutil, "which", lambda _name: None)

    def _never(*_a, **_k):
        raise AssertionError("dig 不在还去跑它")

    monkeypatch.setattr(smoke.subprocess, "run", _never)

    result = smoke._egress_ip()

    assert "dig 不可用" in result
    assert "超时" not in result and "空答案" not in result, "三档不许混在一句里"


def test_egress_ip_wording_matches_the_preflight_probe(smoke):
    """两个脚本报的是同一个值，措辞口径必须对齐（派单 §3.3：照抄 preflight 的分档）。

    对齐的是**命令形状**：`-4`、`+short`、`+time=5`、`+tries=1`、两台 resolver。
    这条盯的是「哪天有人只改一边」——两边说不到一起去时，真跑日现场没人分得清该信哪个。
    """
    preflight = importlib.import_module("scripts.real_run_preflight")
    src_smoke = smoke._egress_ip.__doc__ or ""
    src_pre = preflight.probe_4_egress_ip.__doc__ or ""

    for token in ("-4", "208.67.222.222"):
        assert token in src_smoke, f"冒烟脚本的 docstring 没提 {token}"
        assert token in src_pre, f"preflight 的 docstring 没提 {token}"


def test_both_scripts_classify_digs_own_timeout_the_same(smoke, monkeypatch):
    """🔴 同一个症状，两个脚本必须归到同一档 —— 比对**行为**，不是比对措辞。

    上一条只比 docstring 里有没有那几个字，而真跑日会撞上的是「两个脚本对着同一次
    `dig` 超时，一个说『查询超时（可以重试）』、另一个说『空答案（重试没用）』」——
    现场没人分得清该信哪个。T142 发现 preflight 那份有同样的归档缺陷，两边一起修，
    这条盯着它们别再漂开。
    """
    preflight = importlib.import_module("scripts.real_run_preflight")
    timed_out = ";; connection timed out; no servers could be reached\n"

    _fake_dig(monkeypatch, smoke, [(timed_out, 9)])
    smoke_result = smoke._egress_ip()

    monkeypatch.setattr(preflight.shutil, "which", lambda _name: "/usr/bin/dig")
    monkeypatch.setattr(
        preflight, "_run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 9, stdout=timed_out, stderr=""))
    pre_result = preflight.probe_4_egress_ip()

    assert "查询超时" in smoke_result, f"冒烟脚本归错档：{smoke_result!r}"
    assert not pre_result.ok and "查询超时" in pre_result.detail, \
        f"preflight 归错档：{pre_result.detail!r}"
    assert "空答案" not in smoke_result and "空答案" not in pre_result.detail, \
        "超时被归成空答案 —— 两档的下一步是相反的"


# ============================================================ SQLSTATE 取值
@pytest.mark.parametrize("attr", ["sqlstate", "pgcode"])
def test_sqlstate_reads_both_driver_spellings(smoke, attr):
    """psycopg3 叫 `sqlstate`，psycopg2 叫 `pgcode` —— 两种拼法都要认。

    只认一种的后果是：换个驱动，第 3 档就恒走「无 SQLSTATE」那一支，把每一次
    鉴权失败都报成疑似白名单。那是另一个方向的指错。
    """
    exc = Exception("boom")
    setattr(exc, attr, "28P01")

    assert smoke._sqlstate(exc) == "28P01"


def test_sqlstate_is_none_when_the_driver_reports_nothing(smoke):
    """没有错误码（连接在走到 PG 之前就被断了）→ None，第 3 档据此走白名单那一支。"""
    assert smoke._sqlstate(None) is None
    assert smoke._sqlstate(Exception("connection closed")) is None
    assert smoke._sqlstate(_DriverError("closed", sqlstate=None)) is None
