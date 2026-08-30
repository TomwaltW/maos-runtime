"""T28 配置面 —— 四个治理旋钮搬进 `maos.config` 之后的回归防线。

本文件守三件事，重要性从高到低：

1. **四个读取点的取值逐字节没变**。改的是 `control_plane.py` 与 `gate.py`，
   那是控制面 —— 一处取值漂了，症状是「闸的口径变了」或「replan 上限变了」，
   而这两件事都不会当场报错。所以每个旋钮都拿一份**逐字抄自 d98b9d1 的原实现**
   做对照，在同一张取值表上比，而不是只验「跑得通」。
2. **缺省路径不多做任何事**：`MAOS_CONFIG_SOURCE` 未设即 `EnvConfigSource`，
   `import maos.config` 不碰 `v2`（那是 63 个包 / 135MB 的可选依赖）。
3. **降级是可断言的第三态**：连不上时 `degraded` 为真、`explain()` 说自己回落到了
   env、日志里写着为什么。静默降级会让人以为治理生效了，而实际上没有。

Nacos 真连的那几条在没装 SDK / 没起容器的机器上自动 skip（§5.0 第 4 条）。
起跑线自己划：`MAOS_CONFIG_SOURCE` 等本轨的环境变量在本文件的 autouse fixture 里
剥干净，**不动 `maos/tests/conftest.py`**（那是 T26 的面）。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

from hiclaw.matrix_bus import (ENV_APPROVERS, MatrixBusConfig, RoomApprovalBridge,
                               current_approvers, parse_approvers)
from maos.config import (CONFIG_CHANGED_EVENT, ENV_CONFIG_SOURCE, GOVERNED_KEYS,
                         ORIGIN_DEFAULT, ORIGIN_ENV, ORIGIN_NACOS, ConfigChange,
                         ConfigSource, EnvConfigSource, attach_config_audit,
                         create_config_source, get_config_source,
                         parse_config_document, reset_config_source,
                         set_config_source)
from maos.config import redact as config_redact
from maos.config import source as config_source_mod
from maos.core.control_plane import DEFAULT_MAX_REPLAN, ENV_MAX_REPLAN, ControlPlane
from maos.core.eventbus import InMemoryEventBus
from maos.core.store import SqliteStore
from maos.runtime.gate import (DEFAULT_FINANCE_THRESHOLD, FINANCE_THRESHOLD_ENV,
                               _finance_threshold)
from maos.tools.sandbox import DEFAULT_TIMEOUT, sandbox_timeout

APPROVER = "@boss:example.org"
OUTSIDER = "@mallory:example.org"
CFO = "@cfo:example.org"


# ---------------------------------------------------------------------------
# 起跑线
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _clean_config_env(monkeypatch):
    """把本轨的环境变量剥干净，并保证单例与订阅者不跨用例泄漏。

    `conftest.py` 归 T26，本轨不许改（派单 §5.5），所以这一层写在自己文件里。
    单例必须两头都清：进来时清是怕别的用例留下一个 Nacos 源，出去时清是怕本文件
    留下的源被后面的用例读到 —— 全量 935 条里有几十条会调 `_finance_threshold()`。
    """
    for name in (ENV_CONFIG_SOURCE, ENV_MAX_REPLAN, FINANCE_THRESHOLD_ENV,
                 "MAOS_SANDBOX_TIMEOUT", ENV_APPROVERS):
        monkeypatch.delenv(name, raising=False)
    saved = list(config_source_mod._listeners)
    reset_config_source()
    yield
    reset_config_source()
    config_source_mod._listeners[:] = saved


# ---------------------------------------------------------------------------
# §5.2 四个读取点：逐字抄自 d98b9d1 的原实现，用来做逐字节对照
# ---------------------------------------------------------------------------
def _legacy_max_replan() -> int:
    raw = (os.environ.get(ENV_MAX_REPLAN) or "").strip()
    if not raw:
        return DEFAULT_MAX_REPLAN
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_REPLAN
    return max(value, 0)


def _legacy_finance_threshold() -> float:
    raw = os.environ.get(FINANCE_THRESHOLD_ENV)
    if raw is None or not str(raw).strip():
        return DEFAULT_FINANCE_THRESHOLD
    try:
        return float(raw)
    except (TypeError, ValueError):
        return DEFAULT_FINANCE_THRESHOLD


def _legacy_sandbox_timeout() -> int:
    raw = os.environ.get("MAOS_SANDBOX_TIMEOUT")
    if not raw:
        return DEFAULT_TIMEOUT
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
    if value <= 0:
        return DEFAULT_TIMEOUT
    return value


def _legacy_approvers() -> frozenset[str]:
    return parse_approvers(os.environ.get(ENV_APPROVERS))


#: 一张覆盖三档形态的取值表：未配置 / 正常值 / 空串 / 空白 / 解析不出 / 边界。
#: 「解析不出」与「边界」是重点：四个读取点的回退口径各不相同（一个收严回落、
#: 一个 `max(v, 0)`、一个两段告警、一个丢空白项），抄错一处不会报错，只会悄悄变形。
_VALUE_TABLE = (None, "", "   ", "0", "1", "-1", "3", "5000", "4999.5",
                "五千", "abc", "300", "@a:x , @b:x ,, @a:x")


@pytest.mark.parametrize("raw", _VALUE_TABLE)
def test_max_replan_matches_legacy_byte_for_byte(monkeypatch, raw):
    """`MAOS_MAX_REPLAN`：走配置面之后取值与 T28 之前逐字节一致。"""
    if raw is None:
        monkeypatch.delenv(ENV_MAX_REPLAN, raising=False)
    else:
        monkeypatch.setenv(ENV_MAX_REPLAN, raw)
    cp = ControlPlane.__new__(ControlPlane)          # 不跑 __init__：只验取值那一句
    assert cp._max_replan() == _legacy_max_replan(), f"raw={raw!r}"


@pytest.mark.parametrize("raw", _VALUE_TABLE)
def test_finance_threshold_matches_legacy_byte_for_byte(monkeypatch, raw):
    """`MAOS_FINANCE_THRESHOLD`：第六道闸的阈值一个字节都不许漂。"""
    if raw is None:
        monkeypatch.delenv(FINANCE_THRESHOLD_ENV, raising=False)
    else:
        monkeypatch.setenv(FINANCE_THRESHOLD_ENV, raw)
    assert _finance_threshold() == _legacy_finance_threshold(), f"raw={raw!r}"


@pytest.mark.parametrize("raw", _VALUE_TABLE)
def test_sandbox_timeout_matches_legacy_byte_for_byte(monkeypatch, raw):
    """`MAOS_SANDBOX_TIMEOUT`：含「非正数回退」那一档。"""
    if raw is None:
        monkeypatch.delenv("MAOS_SANDBOX_TIMEOUT", raising=False)
    else:
        monkeypatch.setenv("MAOS_SANDBOX_TIMEOUT", raw)
    assert sandbox_timeout() == _legacy_sandbox_timeout(), f"raw={raw!r}"


@pytest.mark.parametrize("raw", _VALUE_TABLE)
def test_approvers_match_legacy_byte_for_byte(monkeypatch, raw):
    """`MAOS_APPROVERS`：逗号分隔、丢空白项、去重的口径不许变。"""
    if raw is None:
        monkeypatch.delenv(ENV_APPROVERS, raising=False)
    else:
        monkeypatch.setenv(ENV_APPROVERS, raw)
    assert current_approvers() == _legacy_approvers(), f"raw={raw!r}"
    assert MatrixBusConfig.from_env().approvers == _legacy_approvers(), f"raw={raw!r}"


def test_from_env_with_explicit_dict_still_reads_that_dict(monkeypatch):
    """显式传字典的 `from_env({...})` 不许改成去读进程环境。

    `test_matrix_bus.py` 的降级用例、`room_demo.py` 的降级自检都靠这条语义。
    把它改成读 os.environ 会让那些用例拿到一份自己没给过的名单。
    """
    monkeypatch.setenv(ENV_APPROVERS, OUTSIDER)
    assert MatrixBusConfig.from_env({}).approvers == frozenset()
    assert MatrixBusConfig.from_env(
        {ENV_APPROVERS: APPROVER}).approvers == frozenset({APPROVER})


# ---------------------------------------------------------------------------
# §5.1 ConfigSource 本身
# ---------------------------------------------------------------------------
def test_env_source_is_exactly_os_environ_get(monkeypatch):
    """`EnvConfigSource.get` 等价 `os.environ.get(key, default)`。"""
    src = EnvConfigSource()
    monkeypatch.delenv("MAOS_T28_PROBE", raising=False)
    assert src.get("MAOS_T28_PROBE", "缺省") == os.environ.get("MAOS_T28_PROBE", "缺省")
    assert src.explain("MAOS_T28_PROBE") == ORIGIN_DEFAULT
    for value in ("", "   ", "x", "0"):
        monkeypatch.setenv("MAOS_T28_PROBE", value)
        assert src.get("MAOS_T28_PROBE", "缺省") == os.environ.get(
            "MAOS_T28_PROBE", "缺省") == value
        assert src.explain("MAOS_T28_PROBE") == ORIGIN_ENV


def test_default_source_is_env_and_unknown_name_degrades(monkeypatch, caplog):
    """未设 = env；拼错的源名回落 env 并喊出来，不静默、也不抛。"""
    assert isinstance(get_config_source(), EnvConfigSource)
    monkeypatch.setenv(ENV_CONFIG_SOURCE, "nacoss")
    with caplog.at_level(logging.WARNING, logger="maos.config"):
        src = create_config_source()
    assert isinstance(src, EnvConfigSource)
    assert "nacoss" in caplog.text and "降级 env" in caplog.text


def test_importing_maos_config_does_not_import_the_sdk():
    """`import maos.config` 一行 SDK 代码都不许碰（§5.0 第 3 条）。

    起子进程验而不在本进程验：本文件自己可能已经因为别的用例把 `v2` 导进来了，
    在本进程里断言 `"v2" not in sys.modules` 会因为顺序不同而时绿时红。
    """
    code = ("import sys; import maos.config, maos.runtime.gate, maos.tools.sandbox, "
            "maos.core.control_plane, hiclaw.matrix_bus; "
            "print([m for m in sys.modules if m == 'v2' or m.startswith('v2.')])")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=os.path.dirname(os.path.dirname(os.path.dirname(
                             os.path.abspath(__file__)))))
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]", f"惰性 import 被破坏：{out.stdout!r}"


def test_sdk_is_not_a_declared_dependency():
    """`nacos-sdk-python` 不许进 `pyproject.toml`（§5.0 第 2 条、本轨红线）。"""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    text = open(os.path.join(root, "pyproject.toml"), encoding="utf-8").read()
    assert "nacos" not in text.lower(), "nacos SDK 是 63 个包 / 135MB，不许进主依赖"


@pytest.mark.parametrize("raw,expected", [
    ("", {}),
    ("   \n\n", {}),
    ("# 注释\nMAOS_MAX_REPLAN=3\n", {"MAOS_MAX_REPLAN": "3"}),
    ("MAOS_APPROVERS = @a:x, @b:x \n", {"MAOS_APPROVERS": "@a:x, @b:x"}),
    ("MAOS_MAX_REPLAN: 4", {"MAOS_MAX_REPLAN": "4"}),
    ("!bang 也是注释\nA=1", {"A": "1"}),
    ("没有分隔符这一行\nA=1", {"A": "1"}),
    ('{"MAOS_MAX_REPLAN": 3, "X": null}', {"MAOS_MAX_REPLAN": "3", "X": ""}),
    ("{坏掉的 json", {}),
    ("[1,2]", {}),
])
def test_parse_config_document(raw, expected):
    """properties 与 JSON 两种写法都认；写坏了按空文档处理而不是抛。"""
    assert parse_config_document(raw) == expected


def test_redact_masks_secretish_keys_only():
    """铁律 6 的护栏：像密钥的 key 只落长度，四个旋钮原样落。"""
    assert config_redact("MAOS_APPROVERS", "@boss:x") == "@boss:x"
    assert config_redact("MAOS_LLM_API_KEY", "sk-123456") == "<redacted len=9>"
    assert config_redact("MAOS_PG_DSN", "postgresql://u:p@h/d").startswith("<redacted")
    assert config_redact("MAOS_LLM_API_KEY", "") == ""


# ---------------------------------------------------------------------------
# §5.3 审计
# ---------------------------------------------------------------------------
class _DictSource(ConfigSource):
    """一份可当场改的内存配置源，用来在没有 Nacos 的机器上验变更链路。"""

    name = "dict"

    def __init__(self, values: dict[str, str]):
        super().__init__()
        self.values = values

    def _resolve(self, key, default):
        hit = self.values.get(key)
        if hit is None:
            return default, ORIGIN_DEFAULT
        return hit, ORIGIN_ENV


def _store() -> SqliteStore:
    store = SqliteStore()
    store.init_schema()
    return store


def test_config_change_lands_one_event_log_row():
    """一次变更落一条 `ConfigChanged`：谁、什么时候、从 X 改成 Y。"""
    store = _store()
    src = _DictSource({ENV_APPROVERS: APPROVER})
    set_config_source(src)
    detach = attach_config_audit(store, plan_id="plan_t28")

    assert src.get(ENV_APPROVERS, "") == APPROVER          # 首读只立基线
    assert store.list_event_log("plan_t28") == []

    src.values[ENV_APPROVERS] = CFO
    assert src.get(ENV_APPROVERS, "") == CFO
    detach()

    rows = [r for r in store.list_event_log("plan_t28")
            if r["event_type"] == CONFIG_CHANGED_EVENT]
    assert len(rows) == 1, "一次变更应当且只当落一条审计"
    detail = rows[0]["detail"]
    assert detail["key"] == ENV_APPROVERS
    assert detail["old"] == APPROVER and detail["new"] == CFO
    assert detail["at"], "审计行必须带时间"
    assert APPROVER in rows[0]["reason"] and CFO in rows[0]["reason"]
    assert rows[0]["created_at"], "event_log 自己的时间戳"


def test_audit_does_not_extend_the_frozen_contract():
    """`ConfigChanged` 是 `event_log` 的自由 event_type，**不进冻结契约**。

    与 `SkillInvoked` / `ToolInvoked` / `KbRetrieved` / `ArtifactSeeded` 同类，
    出处 `maos/agents/testing.py:50` 与 `maos/kb/retriever.py:571`。
    """
    from maos.contracts.events import EventType
    declared = {v for k, v in vars(EventType).items() if not k.startswith("_")}
    assert CONFIG_CHANGED_EVENT not in declared
    assert declared == {"TaskAssignment", "TaskResult", "ReviewVerdict", "Rework"}


def test_audit_survives_a_broken_sink():
    """审计写不进去也不许把异常抛给主链路（同 `_record_denied` 的口径）。"""
    class _Exploding:
        def append_event_log(self, row):
            raise RuntimeError("库炸了")

    src = _DictSource({ENV_MAX_REPLAN: "2"})
    set_config_source(src)
    detach = attach_config_audit(_Exploding())
    src.get(ENV_MAX_REPLAN, "")
    src.values[ENV_MAX_REPLAN] = "3"
    assert src.get(ENV_MAX_REPLAN, "") == "3"           # 不抛，取值照常
    detach()


def test_no_listener_means_no_rows():
    """没接线就一条审计都不落 —— 缺省路径与本包出现之前逐字节一致。"""
    store = _store()
    src = _DictSource({ENV_MAX_REPLAN: "2"})
    set_config_source(src)
    src.get(ENV_MAX_REPLAN, "")
    src.values[ENV_MAX_REPLAN] = "9"
    src.get(ENV_MAX_REPLAN, "")
    assert store.list_event_log("") == []


def test_approvers_change_is_audited_as_a_security_event():
    """`MAOS_APPROVERS` 是审批权限名单，动它必须留痕。"""
    store = _store()
    src = _DictSource({ENV_APPROVERS: APPROVER})
    set_config_source(src)
    detach = attach_config_audit(store, plan_id="plan_sec")
    current_approvers()
    src.values[ENV_APPROVERS] = f"{APPROVER},{CFO}"
    current_approvers()
    detach()

    rows = [r for r in store.list_event_log("plan_sec")
            if r["detail"].get("key") == ENV_APPROVERS]
    assert len(rows) == 1
    assert CFO in rows[0]["detail"]["new"] and CFO not in rows[0]["detail"]["old"]


# ---------------------------------------------------------------------------
# §5.4 动态治理：不重启进程，下一次审批按新名单判
# ---------------------------------------------------------------------------
class _StubQueue:
    def __init__(self, store):
        self.store = store
        self.decided: list[tuple] = []

    def decide(self, task_id, approved, sender, reason):
        self.decided.append((task_id, approved, sender, reason))


def test_approval_follows_the_live_list_without_restart():
    """**本轨的核心断言**：名单改了，下一条审批命令就按新名单判，进程没重启。

    bridge 是同一个对象、config 是同一份快照 —— 变的只有配置源里那一个值。
    """
    store = _store()
    src = _DictSource({ENV_APPROVERS: APPROVER})
    set_config_source(src)
    queue = _StubQueue(store)
    bridge = RoomApprovalBridge(queue, MatrixBusConfig(approvers=frozenset({APPROVER})))

    assert "无审批权限" in bridge.handle_message(CFO, "/approve task_1")
    assert queue.decided == []

    src.values[ENV_APPROVERS] = CFO                     # ← 改名单，不重启

    assert "无审批权限" not in bridge.handle_message(CFO, "/approve task_1")
    assert queue.decided == [("task_1", True, CFO, "")]
    assert "无审批权限" in bridge.handle_message(APPROVER, "/approve task_2"), \
        "被移出名单的人应当立刻批不动"


def test_empty_live_list_falls_back_to_the_constructed_snapshot():
    """配置面没给名单时回落构造时那份 —— T28 之前的所有用例靠这条留在原地。"""
    set_config_source(_DictSource({}))
    bridge = RoomApprovalBridge(_StubQueue(_store()),
                                MatrixBusConfig(approvers=frozenset({APPROVER})))
    assert bridge._effective_approvers() == frozenset({APPROVER})
    assert "无审批权限" not in bridge.handle_message(APPROVER, "/approve t1")


def test_denied_attempt_still_lands_in_event_log():
    """越权尝试照旧落 `ApprovalDenied` —— 现读名单不许把这条老审计弄丢。"""
    from hiclaw.matrix_bus import EVENT_APPROVAL_DENIED
    store = _store()
    set_config_source(_DictSource({ENV_APPROVERS: APPROVER}))
    bridge = RoomApprovalBridge(_StubQueue(store), MatrixBusConfig())
    bridge.handle_message(OUTSIDER, "/approve task_9")
    rows = [r for r in store.list_event_log("")
            if r["event_type"] == EVENT_APPROVAL_DENIED]
    assert len(rows) == 1 and rows[0]["detail"]["sender"] == OUTSIDER


# ---------------------------------------------------------------------------
# §5.1 降级三态
# ---------------------------------------------------------------------------
def test_nacos_source_degrades_to_env_and_says_why(monkeypatch, caplog):
    """连不上 / 没装 SDK -> 降级 env，`degraded_reason` 与日志都说得出为什么。

    这一条在**装了 SDK 和没装 SDK 的机器上都要绿**：没装走 ImportError 那一档，
    装了走「连 127.0.0.1:1 连不上」那一档，两档都必须是「不抛 + 有话说」。
    """
    from maos.config.nacos_source import NacosConfigSource
    monkeypatch.setenv(ENV_MAX_REPLAN, "7")
    with caplog.at_level(logging.WARNING, logger="maos.config.nacos"):
        src = NacosConfigSource(server="127.0.0.1:1", timeout_ms=1000)
    try:
        assert src.degraded is True
        assert src.degraded_reason, "降级必须说得出原因"
        assert "降级 env" in caplog.text, "静默降级比不接更坏"
        assert src.get(ENV_MAX_REPLAN, "") == "7", "降级后仍按 env 取值，不抛"
        assert src.explain(ENV_MAX_REPLAN) == ORIGIN_ENV
    finally:
        src.close()


def test_nacos_source_without_connecting_falls_back_per_key(monkeypatch, caplog):
    """第三档：连上了但这一项在 Nacos 没有 -> 回落 env，INFO 说明本次取值来自 env。"""
    from maos.config.nacos_source import NacosConfigSource
    monkeypatch.setenv(FINANCE_THRESHOLD_ENV, "1234")
    src = NacosConfigSource(connect=False)
    src._snapshot = {ENV_MAX_REPLAN: "5"}
    with caplog.at_level(logging.INFO, logger="maos.config.nacos"):
        assert src.get(ENV_MAX_REPLAN, "") == "5"
        assert src.explain(ENV_MAX_REPLAN) == ORIGIN_NACOS
        assert src.get(FINANCE_THRESHOLD_ENV, "") == "1234"
        assert src.explain(FINANCE_THRESHOLD_ENV) == ORIGIN_ENV
    assert "无此项，本次取值来自 env" in caplog.text


def test_push_diffs_only_governed_keys_and_emits_changes():
    """推送到达 -> 只对 `GOVERNED_KEYS` 里变了的项广播，首次拉取只立基线。"""
    from maos.config.nacos_source import NacosConfigSource
    seen: list[ConfigChange] = []
    detach = config_source_mod.subscribe(seen.append)
    src = NacosConfigSource(connect=False)
    try:
        src._apply("MAOS_MAX_REPLAN=2\nMAOS_APPROVERS=@boss:x\nNOISE=1\n", first=True)
        assert seen == [], "首次拉取不是一次配置变更"

        src._apply("MAOS_MAX_REPLAN=5\nMAOS_APPROVERS=@boss:x\nNOISE=2\n", first=False)
        assert [c.key for c in seen] == [ENV_MAX_REPLAN], "NOISE 不在治理清单里"
        assert (seen[0].old, seen[0].new) == ("2", "5")
        assert seen[0].origin == ORIGIN_NACOS
    finally:
        detach()
        src.close()


def test_governed_keys_are_exactly_the_four_this_track_owns():
    """kb 那两个旋钮这一轮不接（`maos/kb/**` 归 T24 / T25），别顺手加进来。"""
    assert GOVERNED_KEYS == (ENV_MAX_REPLAN, FINANCE_THRESHOLD_ENV,
                             "MAOS_SANDBOX_TIMEOUT", ENV_APPROVERS)
    assert "MAOS_KB_ENABLED" not in GOVERNED_KEYS
    assert "MAOS_KB_WEIGHTS" not in GOVERNED_KEYS


# ---------------------------------------------------------------------------
# Nacos 真连（没装 SDK / 没起容器 / 没配凭证一律 skip）
# ---------------------------------------------------------------------------
NACOS_SERVER = os.environ.get("MAOS_NACOS_SERVER", "127.0.0.1:8848")
#: 真连用的 dataId。可用 `MAOS_NACOS_TEST_DATA_ID` 覆盖 —— 换一个**没用过的**名字
#: 就能复现「全新 dataId 第一次改动时历史还没落库」那条竞态，`_HistoryLookup` 的
#: 有界重试就是为它加的（2026-08-30 实测踩到过一次）。
LIVE_DATA_ID = os.environ.get("MAOS_NACOS_TEST_DATA_ID") or "maos-governance-test"
LIVE_GROUP = "DEFAULT_GROUP"


def _live_reason() -> str:
    """返回不能跑真连测试的原因；空串 = 能跑。在 collection 期求值一次。"""
    try:
        import v2.nacos                                              # noqa: F401
    except ImportError:
        return "未装 nacos-sdk-python（63 个包 / 135MB 的可选依赖，见 deploy/nacos.md）"
    if not os.environ.get("MAOS_NACOS_USERNAME"):
        return "未配 MAOS_NACOS_USERNAME / MAOS_NACOS_PASSWORD"
    try:
        with urllib.request.urlopen(
                f"http://{NACOS_SERVER}/nacos/v1/console/health/readiness",
                timeout=3) as resp:
            if resp.status != 200:
                return f"Nacos readiness 返回 {resp.status}"
    except Exception as exc:                                         # noqa: BLE001
        return f"连不上 {NACOS_SERVER}（{type(exc).__name__}）"
    return ""


LIVE_SKIP = _live_reason()
live = pytest.mark.skipif(bool(LIVE_SKIP), reason=f"Nacos 真连不可用：{LIVE_SKIP}")


def _openapi_publish(content: str) -> None:
    """经 OpenAPI 改配置 —— 与人在控制台上点「发布」走的是同一条路径。

    刻意不用 SDK 的 `publish_config`：实测 SDK 的 gRPC 写入不带操作人，
    Nacos 历史里 `srcUser` 为空；带 accessToken 的 OpenAPI 写入才记得下操作人。
    这条差别是 `deploy/nacos-live.md` 里写明的实测结论，测试要走那条真路径。
    """
    body = urllib.parse.urlencode({
        "username": os.environ["MAOS_NACOS_USERNAME"],
        "password": os.environ.get("MAOS_NACOS_PASSWORD", "")}).encode()
    req = urllib.request.Request(f"http://{NACOS_SERVER}/nacos/v1/auth/login",
                                 data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=5) as resp:
        token = json.loads(resp.read().decode())["accessToken"]

    form = urllib.parse.urlencode({
        "dataId": LIVE_DATA_ID, "group": LIVE_GROUP,
        "content": content, "accessToken": token}).encode()
    req = urllib.request.Request(f"http://{NACOS_SERVER}/nacos/v1/cs/configs",
                                 data=form, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=8) as resp:
        assert resp.read().decode().strip() == "true"


@live
def test_live_nacos_serves_the_four_knobs(monkeypatch):
    """真连：Nacos 里的值盖过 env，没有的项回落 env。"""
    from maos.config.nacos_source import NacosConfigSource
    monkeypatch.setenv(FINANCE_THRESHOLD_ENV, "9999")
    _openapi_publish("MAOS_MAX_REPLAN=7\nMAOS_APPROVERS=@boss:maos.local\n")
    src = NacosConfigSource(data_id=LIVE_DATA_ID, group=LIVE_GROUP)
    try:
        assert src.degraded is False, src.degraded_reason
        assert src.get(ENV_MAX_REPLAN, "") == "7"
        assert src.explain(ENV_MAX_REPLAN) == ORIGIN_NACOS
        assert src.get(FINANCE_THRESHOLD_ENV, "") == "9999"
        assert src.explain(FINANCE_THRESHOLD_ENV) == ORIGIN_ENV
    finally:
        src.close()


@live
def test_live_approver_list_change_takes_effect_without_restart():
    """真连 · **§5.4 的主判据**：控制台改名单 -> 下一条审批按新名单判 + 落审计。

    进程不重启、bridge 不重建、config 快照不换 —— 变的只有 Nacos 里那一行。
    """
    from maos.config.nacos_source import NacosConfigSource
    store = _store()
    _openapi_publish(f"MAOS_APPROVERS={APPROVER}\n")
    src = NacosConfigSource(data_id=LIVE_DATA_ID, group=LIVE_GROUP)
    set_config_source(src)
    detach = attach_config_audit(store, plan_id="plan_live")
    queue = _StubQueue(store)
    bridge = RoomApprovalBridge(queue, MatrixBusConfig())
    try:
        assert src.get(ENV_APPROVERS, "") == APPROVER
        assert "无审批权限" in bridge.handle_message(CFO, "/approve task_live")

        t0 = time.monotonic()
        _openapi_publish(f"MAOS_APPROVERS={CFO}\n")
        deadline = t0 + 60
        while time.monotonic() < deadline and src.snapshot().get(ENV_APPROVERS) != CFO:
            time.sleep(0.05)
        effect_latency = time.monotonic() - t0
        assert src.snapshot().get(ENV_APPROVERS) == CFO, \
            f"{effect_latency:.1f}s 内没收到推送"

        assert "无审批权限" not in bridge.handle_message(CFO, "/approve task_live")
        assert queue.decided == [("task_live", True, CFO, "")]
        assert "无审批权限" in bridge.handle_message(APPROVER, "/approve task_live2")

        # 审计行比生效**晚一点**是设计使然，不是漏写：`_apply` 先换快照（于是审批
        # 立刻按新名单判），再去查一次操作人（HTTP，慢），最后才落审计。这里分两段
        # 等、分两段计时，就是为了把这个先后关系钉成事实而不是猜测。
        def _rows():
            return [r for r in store.list_event_log("plan_live")
                    if r["event_type"] == CONFIG_CHANGED_EVENT]

        while time.monotonic() < deadline and not _rows():
            time.sleep(0.05)
        audit_latency = time.monotonic() - t0
        rows = _rows()
        assert len(rows) == 1, (f"名单变更必须落且只落一条审计"
                                f"（等了 {audit_latency:.1f}s）")
        assert effect_latency <= audit_latency, \
            "生效应当不晚于审计：审批先按新名单判，审计随后补上操作人"
        assert rows[0]["detail"]["old"] == APPROVER
        assert rows[0]["detail"]["new"] == CFO
        assert rows[0]["detail"]["origin"] == ORIGIN_NACOS
        assert rows[0]["detail"]["actor"], "经 OpenAPI 改的配置应当查得到操作人"
    finally:
        detach()
        src.close()
