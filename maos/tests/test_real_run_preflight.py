"""真跑日前置探测器的守卫。

这个文件守两样东西，第二样比第一样重要：

1. **分档是对的** —— 硬缺 exit 1、只软缺 exit 2、全绿 exit 0。真跑日那天人
   靠这个退出码决定「现在能不能开跑」，档错了比没有探测器更坏。
2. **一个字符的值都漏不出去（铁律 6）** —— 假 DSN、假 key、假 token 塞进环境，
   跑完整个探测器，输出里连片段都不许出现。哨兵反查的做法同
   `scripts/polardb_smoke.py`：用一眼能认出来的哨兵串，grep 整份输出。

第 2 条测两层：**正常路径不泄漏**（设计防线），以及**某条探测手滑把值放进
detail 时仍不泄漏**（`_redact()` 兜底防线）。只测第一层的话，将来有人加探测时
漏出去了这里不会红。
"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
import types

import pytest

#: 一眼能认出来、且绝不会在正常输出里自然出现的哨兵串。
DSN_HOST = "sentinel-polardb-host.example.internal"
DSN_PASSWORD = "SENTINEL-PG-PASSWORD-9f2a"
DSN_USER = "sentinel_pg_user"
DSN_DB = "sentinel_pg_db"
FAKE_DSN = f"postgresql://{DSN_USER}:{DSN_PASSWORD}@{DSN_HOST}:5432/{DSN_DB}"
FAKE_API_KEY = "SENTINEL-LLM-API-KEY-7c31d5"
FAKE_BASE_URL = "https://sentinel-llm-endpoint.example.internal/v1"
FAKE_MATRIX_TOKEN = "SENTINEL-MATRIX-TOKEN-4b8e"
FAKE_ROOM_ID = "!sentinel-room-id:example.internal"

#: 整份输出里一个都不许出现的片段。**口令与 host 单列** —— 整条 DSN 被抹掉
#: 不蕴含它俩被抹掉，而泄漏其中任何一个都够让人连上那台实例。
SENTINELS = (FAKE_DSN, DSN_HOST, DSN_PASSWORD, FAKE_API_KEY,
             FAKE_BASE_URL, FAKE_MATRIX_TOKEN, FAKE_ROOM_ID)


@pytest.fixture()
def preflight(monkeypatch):
    """每条测试拿一个干净的模块：`_SECRETS` 是模块级的，跨测试会串。"""
    module = importlib.import_module("scripts.real_run_preflight")
    monkeypatch.setattr(module, "_SECRETS", [])
    return module


def _seed_secret_env(monkeypatch) -> None:
    monkeypatch.setenv("MAOS_PG_DSN", FAKE_DSN)
    monkeypatch.setenv("MAOS_LLM_API_KEY", FAKE_API_KEY)
    monkeypatch.setenv("MAOS_LLM_BASE_URL", FAKE_BASE_URL)
    monkeypatch.setenv("MAOS_LLM_MODEL", "sentinel-model")
    monkeypatch.setenv("MATRIX_TOKEN", FAKE_MATRIX_TOKEN)
    monkeypatch.setenv("MATRIX_ROOM_ID", FAKE_ROOM_ID)


# --------------------------------------------------------------------------
# 铁律 6：哨兵反查
# --------------------------------------------------------------------------

@pytest.mark.parametrize("argv", [[], ["--json"]])
def test_no_secret_fragment_ever_reaches_output(preflight, monkeypatch, capsys, argv):
    """把假 DSN / 假 key / 假 token 塞进环境跑完整个探测器，两种输出格式都不许漏。

    这是铁律 6 在本脚本上的落点。`--json` 单独测一遍：JSON 那条路走的是
    `as_dict()` 而不是 `render()`，两条路都得过脱敏才算数。
    """
    _seed_secret_env(monkeypatch)
    preflight.main(argv)
    out = capsys.readouterr().out

    for sentinel in SENTINELS:
        assert sentinel not in out, f"泄漏了：{sentinel}"
    # 连用户名 / 库名这种「看着不像秘密」的也不该出现 —— 它们是登录那台实例的一半。
    assert DSN_USER not in out
    assert DSN_DB not in out
    # 但输出本身必须还是有内容的：抹成空白等于没探测。
    assert "真跑日前置探测" in out or '"summary"' in out


def test_redact_covers_detail_written_by_a_careless_probe(preflight, monkeypatch):
    """兜底防线：某条探测把值原样放进 detail 时，`as_dict()` 仍然抹得掉。

    第一道防线是「压根不往 detail 里放值」，但那靠人自觉。将来有人加探测时
    手滑（比如把驱动异常的 message 直接塞进 detail，而多数驱动会把 host
    拼进 message），这一条是那时候唯一会红的灯。
    """
    _seed_secret_env(monkeypatch)
    preflight._remember_all()
    probe = preflight.Probe(99, "手滑的探测", hard=True, segment=preflight.SEG_A)
    probe.fail(f"连不上 {FAKE_DSN}（口令 {DSN_PASSWORD}，host {DSN_HOST}）")

    detail = probe.as_dict()["detail"]
    for sentinel in (FAKE_DSN, DSN_PASSWORD, DSN_HOST):
        assert sentinel not in detail
    assert "<redacted>" in detail or "<dsn-redacted>" in detail


def test_emit_is_the_only_exit_and_it_redacts(preflight, monkeypatch, capsys):
    """`emit()` 是唯一出口，且它过脱敏。绕过它直接 print = 绕过脱敏。"""
    _seed_secret_env(monkeypatch)
    preflight._remember_all()
    preflight.emit(f"目标 {DSN_HOST}，口令 {DSN_PASSWORD}")

    out = capsys.readouterr().out
    assert DSN_HOST not in out and DSN_PASSWORD not in out


def test_short_env_values_do_not_shred_normal_output(preflight, monkeypatch, capsys):
    """短值不许把正文切碎。

    `_SECRETS` 做的是无条件子串替换，登记一个两三字符的值会把输出打成筛子
    （`maos` 会让 `com.maos.room-ingress` 变成 `com.<redacted>.room-ingress`）。
    所以登记有长度下限 —— 这条守的是那个下限还在。
    """
    monkeypatch.setenv("MAOS_PG_DSN", "ab")
    preflight._remember_all()
    preflight.emit("com.maos.room-ingress 在跑")

    assert "com.maos.room-ingress 在跑" in capsys.readouterr().out


# --------------------------------------------------------------------------
# 退出码分档
# --------------------------------------------------------------------------

def _probe(preflight, *, hard: bool, ok: bool):
    probe = preflight.Probe(1, "t", hard=hard, segment=preflight.SEG_BOTH)
    return probe.pass_("ok") if ok else probe.fail("bad")


def test_exit_code_0_when_everything_passes(preflight):
    summary = preflight.summarize([
        _probe(preflight, hard=True, ok=True),
        _probe(preflight, hard=False, ok=True),
    ])
    assert summary["exit_code"] == 0


def test_exit_code_2_when_only_soft_probes_fail(preflight):
    """软缺不该把人拦在门外：Scripted 束不需要真模型 key，那不是「跑不起来」。"""
    summary = preflight.summarize([
        _probe(preflight, hard=True, ok=True),
        _probe(preflight, hard=False, ok=False),
    ])
    assert summary["exit_code"] == 2
    assert summary["hard_ok"] == summary["hard_total"]


def test_exit_code_1_when_any_hard_probe_fails(preflight):
    """硬缺压倒软全绿 —— 退出码要报最坏的那一档，不是平均。"""
    summary = preflight.summarize([
        _probe(preflight, hard=True, ok=False),
        _probe(preflight, hard=False, ok=True),
    ])
    assert summary["exit_code"] == 1


def test_json_output_is_machine_readable(preflight, monkeypatch, capsys):
    """runbook A 段第 1 步引用这份 JSON，它得真能 parse。"""
    _seed_secret_env(monkeypatch)
    preflight.main(["--json"])

    payload = json.loads(capsys.readouterr().out)
    assert len(payload["probes"]) == len(preflight.PROBES)
    assert payload["summary"]["exit_code"] in (0, 1, 2)
    for probe in payload["probes"]:
        assert set(probe) >= {"number", "title", "hard", "segment", "ok", "detail"}
        assert probe["segment"] in (preflight.SEG_A, preflight.SEG_B,
                                    preflight.SEG_BOTH)


def test_one_broken_probe_does_not_take_down_the_rest(preflight, monkeypatch):
    """一条探测炸了不许带塌其余 —— 探测器的价值全在覆盖面。"""
    def explode():
        raise RuntimeError("boom")

    monkeypatch.setattr(preflight, "PROBES", (explode, preflight.probe_11_python_disk))
    probes = preflight.run_probes()

    assert len(probes) == 2
    assert not probes[0].ok and "RuntimeError" in probes[0].detail
    assert probes[1].title.startswith("python3 版本")


# --------------------------------------------------------------------------
# 逐条探测的判据
# --------------------------------------------------------------------------

def test_dsn_placeholder_template_is_caught(preflight, monkeypatch):
    """真跑日最容易犯的错：把文档里那行模板原样 export 了。"""
    monkeypatch.setenv("MAOS_PG_DSN",
                       "postgresql://<user>:<pass>@<host>:<port>/<db>")
    probe = preflight.probe_3_pg_dsn()

    assert not probe.ok and "占位符" in probe.detail
    assert "<user>" not in probe.detail and "<host>" not in probe.detail


def test_dsn_missing_is_a_hard_miss_and_says_nothing_about_values(
        preflight, monkeypatch):
    monkeypatch.delenv("MAOS_PG_DSN", raising=False)
    probe = preflight.probe_3_pg_dsn()

    assert probe.hard and not probe.ok
    assert "未配置" in probe.detail


def test_dsn_pointing_at_localhost_is_flagged_not_silently_accepted(
        preflight, monkeypatch):
    """本机 DSN 形态完全合法 —— 但 A 段要的是**真实例**。

    这条不报的话，真跑日可能拿着本机库跑完一整轮，回头才发现证据说的是
    `127.0.0.1`，而材料里写的是「阿里云 PolarDB 真实例」。
    """
    monkeypatch.setenv("MAOS_PG_DSN",
                       "postgresql://maos:maos-local-dev@127.0.0.1:5432/maos")
    probe = preflight.probe_3_pg_dsn()

    assert probe.ok
    assert "本机" in probe.detail


def test_dsn_without_password_is_reported_as_incomplete(preflight, monkeypatch):
    monkeypatch.setenv("MAOS_PG_DSN", "postgresql://someuser@somewhere.test:5432/db")
    probe = preflight.probe_3_pg_dsn()

    assert not probe.ok and "口令" in probe.detail
    assert "someuser" not in probe.detail and "somewhere.test" not in probe.detail


def test_room_ingress_registered_but_not_running_is_a_hard_miss(
        preflight, monkeypatch):
    """`launchctl list` 的 PID 列是 `-` = 登记了但没在跑。

    这正是 B 段最阴的那种失败：配置看起来齐全，房间里一条消息都没有。
    """
    monkeypatch.setattr(
        preflight, "_run",
        lambda *a, **k: subprocess.CompletedProcess(
            a[0], 0, stdout=f"-\t78\t{preflight.ROOM_INGRESS_LABEL}\n", stderr=""))
    probe = preflight.probe_8_room_ingress()

    assert probe.hard and not probe.ok
    assert "没在跑" in probe.detail
    # 只读探测绝不能顺手「修」它 —— 那是人类演示房的 KeepAlive 常驻。
    assert "kickstart" in probe.detail


def test_room_ingress_running_reports_pid(preflight, monkeypatch):
    monkeypatch.setattr(
        preflight, "_run",
        lambda *a, **k: subprocess.CompletedProcess(
            a[0], 0, stdout=f"4321\t0\t{preflight.ROOM_INGRESS_LABEL}\n", stderr=""))
    probe = preflight.probe_8_room_ingress()

    assert probe.ok and "4321" in probe.detail


def test_room_ingress_absent_from_launchd_is_caught(preflight, monkeypatch):
    monkeypatch.setattr(
        preflight, "_run",
        lambda *a, **k: subprocess.CompletedProcess(
            a[0], 0, stdout="123\t0\tcom.example.other\n", stderr=""))
    probe = preflight.probe_8_room_ingress()

    assert not probe.ok and "没登记" in probe.detail


def test_egress_ip_empty_answer_still_points_at_the_console(
        preflight, monkeypatch):
    """两台 resolver 都回空答案时，报「取不到」并给出路，不报成「网络不通」。

    整合期 p10-f 改判：此前这一档报的是「这条查询在本机被接管了」——那个结论
    **是错的**。真相是不带 `-4` 时 `dig` 可能走 IPv6 去问 `resolver1.opendns.com`，
    那一路到不了真的 OpenDNS resolver。加上 `-4` 之后本机稳定拿得到出口 IP。
    所以这一档现在只说「取不到」，把「为什么」留给那句带 `-4` 的实现注释 ——
    编一个错的病因比不说更坏，它让人去查一件没发生的事。
    """
    monkeypatch.setattr(preflight.shutil, "which", lambda name: "/usr/bin/dig")
    monkeypatch.setattr(
        preflight, "_run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, stdout="", stderr=""))
    probe = preflight.probe_4_egress_ip()

    assert not probe.ok
    assert "空答案" in probe.detail and "两台 resolver" in probe.detail
    # 取不到时必须给出路，否则真跑日卡在这里没有下一步。
    assert "控制台" in probe.detail


def test_egress_ip_query_forces_ipv4(preflight, monkeypatch):
    """🔴 `-4` 不许被顺手删掉 —— 删了这条探测在本机就回到恒空。

    整合期 p10-f 实测：`dig +short myip.opendns.com @resolver1.opendns.com` 返回空，
    同一条加上 `-4` 稳定返回真实出口 IP（连跑三次一致）。这个差别没有任何症状 ——
    删掉之后探测照样「跑得过」，只是永远报取不到，而真跑日要的正是那个值。
    """
    seen: list[list[str]] = []

    def spy(cmd, **kw):
        seen.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="203.0.113.7\n", stderr="")

    monkeypatch.setattr(preflight.shutil, "which", lambda name: "/usr/bin/dig")
    monkeypatch.setattr(preflight, "_run", spy)
    preflight.probe_4_egress_ip()

    assert seen, "一条 dig 都没发出去"
    assert "-4" in seen[0], f"dig 没强制 IPv4：{seen[0]}"


def test_egress_ip_falls_back_to_the_second_resolver(preflight, monkeypatch):
    """第一台 resolver 超时就换第二台 —— 实测三次里偶发一次超时。"""
    calls: list[list[str]] = []

    def flaky(cmd, **kw):
        calls.append(list(cmd))
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(cmd, 20)
        return subprocess.CompletedProcess(cmd, 0, stdout="203.0.113.7\n", stderr="")

    monkeypatch.setattr(preflight.shutil, "which", lambda name: "/usr/bin/dig")
    monkeypatch.setattr(preflight, "_run", flaky)
    probe = preflight.probe_4_egress_ip()

    assert len(calls) == 2, f"第一台超时之后没换第二台：{calls}"
    assert probe.ok and "203.0.113.7" in probe.detail


def test_egress_ip_never_reports_a_dig_error_line_as_an_ip(preflight, monkeypatch):
    """dig 把「连不上」也写在 stdout 上，那不是一个 IP。

    `;; connection timed out; no servers could be reached` 被当成答案抄进白名单，
    真跑日就会对着一个不存在的 IP 排障 —— 而它「看起来有输出」。
    """
    monkeypatch.setattr(preflight.shutil, "which", lambda name: "/usr/bin/dig")
    monkeypatch.setattr(
        preflight, "_run",
        lambda *a, **k: subprocess.CompletedProcess(
            a[0], 0, stdout=";; connection timed out; no servers could be reached\n",
            stderr=""))
    probe = preflight.probe_4_egress_ip()

    assert not probe.ok, "把 dig 的报错行当成 IP 报了出去"
    assert "connection timed out" not in probe.detail or "控制台" in probe.detail


def test_egress_ip_is_a_soft_probe(preflight):
    """🔴 它是软项，不是硬项。

    整合期 p10-f 改判：它产出的是「一个要抄下来的值」，不是「跑不跑得起来」的前提，
    而这条查询要过公网、实测会偶发超时。标成硬项的话，一次 DNS 抖动就把退出码判成
    「真跑日跑不起来」，`docs/real-run-runbook.md` 0 段写的「退出码 0 或 2」也就
    随机达不到 —— 那张纸的「不对时怎么办」整格失效。
    """
    assert preflight.probe_4_egress_ip().hard is False


def test_egress_ip_success_prints_the_ip(preflight, monkeypatch):
    """出口 IP **可以**打印：白名单按它放行，且它不泄漏目标 host。"""
    monkeypatch.setattr(preflight.shutil, "which", lambda name: "/usr/bin/dig")
    monkeypatch.setattr(
        preflight, "_run",
        lambda *a, **k: subprocess.CompletedProcess(
            a[0], 0, stdout="203.0.113.7\n", stderr=""))
    probe = preflight.probe_4_egress_ip()

    assert probe.ok and "203.0.113.7" in probe.detail


def test_ssl_cert_file_missing_names_the_symptom(preflight, monkeypatch):
    """本机 Python 缺根证书，不设这个变量的症状要在探测里就说出来。"""
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    probe = preflight.probe_5_ssl_cert_file()

    assert probe.hard and not probe.ok
    assert "CERTIFICATE_VERIFY_FAILED" in probe.detail


def test_ssl_cert_file_path_is_never_printed(preflight, monkeypatch, tmp_path):
    """路径通常含用户名 —— 只报存在与大小，不报路径。"""
    cert = tmp_path / "sentinel-cacert.pem"
    cert.write_text("x" * 128, encoding="utf-8")
    monkeypatch.setenv("SSL_CERT_FILE", str(cert))
    probe = preflight.probe_5_ssl_cert_file()

    assert probe.ok and "128" in probe.detail
    assert str(cert) not in probe.detail and "sentinel-cacert" not in probe.detail


def test_ssl_cert_file_pointing_nowhere_is_a_miss(preflight, monkeypatch, tmp_path):
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "gone.pem"))
    probe = preflight.probe_5_ssl_cert_file()

    assert not probe.ok and "不存在" in probe.detail


def test_model_key_is_soft_because_scripted_bundles_need_none(preflight, monkeypatch):
    """Wave C 之后不给 `--live-model` 一定是 Scripted —— 缺 key 不该拦住收束。"""
    for name in preflight.ENV_LLM:
        monkeypatch.delenv(name, raising=False)
    probe = preflight.probe_6_model_key()

    assert not probe.hard
    assert not probe.ok and "Scripted" in probe.detail


def test_model_key_partial_config_is_reported(preflight, monkeypatch):
    """三缺一比三个都没有更危险：跑起来才在半路报错。"""
    monkeypatch.setenv("MAOS_LLM_API_KEY", FAKE_API_KEY)
    monkeypatch.setenv("MAOS_LLM_BASE_URL", FAKE_BASE_URL)
    monkeypatch.delenv("MAOS_LLM_MODEL", raising=False)
    preflight._remember_all()
    probe = preflight.probe_6_model_key()

    assert not probe.ok and "MAOS_LLM_MODEL" in probe.detail
    assert FAKE_API_KEY not in probe.as_dict()["detail"]


def test_psycopg2_only_is_a_miss_because_pg_store_needs_v3(preflight, monkeypatch):
    """只有 v2 = 不算过。

    这是 BACKLOG `## task-T31` 记过的坑，且它的症状最会骗人：
    `polardb_smoke.py` 会回落 psycopg2 并报 **5/5 全绿**，而 `pg_store._driver()`
    只认 v3 —— 于是同一台机器上冒烟全绿、每次 `PgStorePort` 调用都抛
    `PgBackendUnavailable`。真跑日撞上这个，第一反应会是「代码回归了」。

    `sys.modules[name] = None` 让 `import name` 抛 ImportError，是标准库行为。
    """
    fake_v2 = types.ModuleType("psycopg2")
    fake_v2.__version__ = "2.9.9 (dt dec pq3 ext lo64)"
    monkeypatch.setitem(sys.modules, "psycopg", None)
    monkeypatch.setitem(sys.modules, "psycopg2", fake_v2)
    probe = preflight.probe_2_psycopg()

    assert probe.hard and not probe.ok
    assert "2.9.9" in probe.detail and "v3" in probe.detail


def test_psycopg_missing_entirely_gives_the_install_command(preflight, monkeypatch):
    monkeypatch.setitem(sys.modules, "psycopg", None)
    monkeypatch.setitem(sys.modules, "psycopg2", None)
    probe = preflight.probe_2_psycopg()

    assert not probe.ok and "pip install" in probe.detail


def test_probes_are_read_only(preflight, monkeypatch, capsys):
    """「只探不改」是这个脚本的立身之本：跑一遍不许动工作区。

    真跑日那天人会反复跑它（补一项前置、再跑一次），任何副作用都会在
    证据首行的 sha 上留下 `-dirty`。
    """
    before = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(preflight.ROOT),
        capture_output=True, text=True, check=True).stdout
    preflight.main([])
    capsys.readouterr()
    after = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(preflight.ROOT),
        capture_output=True, text=True, check=True).stdout

    assert before == after
