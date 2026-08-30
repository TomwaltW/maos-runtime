"""配置面 —— 治理旋钮的读取口，以及「这次读到的值从哪来」这件事本身。

本模块只做三件事：

1. 定义 `ConfigSource.get(key, default) -> str`，把「读一个治理旋钮」这个动作
   从 `os.environ.get` 抽出来一层；
2. 提供 `EnvConfigSource` —— 缺省实现，行为与 `os.environ.get(key, default)`
   **逐字节一致**，不多做任何事；
3. 把「值变了」这件事广播给订阅者（`maos/config/audit.py` 据此落 event_log）。

真正的 Nacos 客户端在 `maos/config/nacos_source.py`，本模块**不 import 它**，
只在 `create_config_source()` 里按需惰性 import（§5.0 第 3 条）。

## 🔴 缺省路径必须一个字节都不变

本仓库的卖点是「无需任何 key、裸 clone 跑到 7/7、核心零依赖」。`nacos-sdk-python`
是 63 个包 / 135MB —— 它**不进 `pyproject.toml`**，只写进 `deploy/nacos.md` 的
可选依赖一节。于是本模块必须满足：

* `MAOS_CONFIG_SOURCE` 未设 = `EnvConfigSource`，`get()` 就是 `os.environ.get`；
* 没有订阅者时，`get()` 连变更检测都不做（`if _listeners` 那一行是热路径唯一的
  额外开销）；
* `import maos.config` 不 import `v2`，不 import `asyncio` 的任何运行时资源。

这与 `maos/store/__init__.py` 的「可选后端」是同一个模式，**但降级口径相反**：
`create_store()` 对 postgres **绝不回落**（回落会让你以为 PG 验过了），而这里对
Nacos **必须回落**（配置面挂了不该把整个 MAOS 拖停）。两者不矛盾 —— 前者是
「你明确要的后端没拿到」，后者是「治理面暂时不可达，按上一档配置继续跑」。
分界线是**回落有没有被说出来**，见下一节。

## 降级是「静默失败之外的第三态」

静默降级比不接更坏：你以为治理生效了，实际上没有。所以每一档降级都要留痕 ——

    ① SDK 没装        -> WARNING「nacos-sdk-python 未安装，降级 env」
    ② 连不上 / 拉不到   -> WARNING「Nacos 不可达（<原因>），降级 env」
    ③ 该项在 Nacos 没有 -> INFO   「<key> 在 Nacos 无此项，本次取值来自 env」

三档都**不抛**。`explain(key)` 返回本次解析的来源，测试与
`deploy/nacos-live.md` 靠它把「用的是 Nacos 还是 env」写成可核对的事实，
而不是一句「应该是 Nacos 吧」。
"""

from __future__ import annotations

import logging
import os
import re
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

log = logging.getLogger("maos.config")

__all__ = [
    "ConfigChange",
    "ConfigSource",
    "EnvConfigSource",
    "ENV_CONFIG_SOURCE",
    "GOVERNED_KEYS",
    "ORIGIN_DEFAULT",
    "ORIGIN_ENV",
    "ORIGIN_NACOS",
    "SOURCE_ENV",
    "SOURCE_NACOS",
    "create_config_source",
    "get_config_source",
    "parse_config_document",
    "redact",
    "reset_config_source",
    "set_config_source",
    "subscribe",
]

#: 选配置面的环境变量。未设 = env，与本模块出现之前逐字节一致。
ENV_CONFIG_SOURCE = "MAOS_CONFIG_SOURCE"

SOURCE_ENV = "env"
SOURCE_NACOS = "nacos"
DEFAULT_SOURCE = SOURCE_ENV

#: 一次解析的来源。落进审计的 `detail.origin`，也是 `explain()` 的返回值。
ORIGIN_NACOS = "nacos"
ORIGIN_ENV = "env"
ORIGIN_DEFAULT = "default"

#: 本轮真正搬上配置面的四个旋钮。**这是一份清单，不是一道闸** ——
#: `get()` 不校验 key 在不在里面，别的调用方照样能用本模块读自己的 key。
#: 它的用处只有一个：Nacos 侧推来一份新文档时，按这份清单逐个 diff 出变更。
#:
#: kb 的两个旋钮（`MAOS_KB_ENABLED` / `MAOS_KB_WEIGHTS`）这一轮**不在此列**：
#: `maos/kb/**` 归 T24 / T25，同轮并行改同一个文件必冲突。接口留成现在这个形状
#: 就是为了它们并轨后能直接加两行进来，一行别的代码都不用改。
GOVERNED_KEYS: tuple[str, ...] = (
    "MAOS_MAX_REPLAN",
    "MAOS_FINANCE_THRESHOLD",
    "MAOS_SANDBOX_TIMEOUT",
    "MAOS_APPROVERS",
)

#: 值一旦命中就只落掩码的 key 形态（铁律 6）。这四个旋钮**都不是密钥**，
#: 这条是护栏不是功能：哪天有人把本模块用来读一个带 TOKEN 的 key，
#: 审计行里不会当场把它印出来。
_SECRETISH = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|DSN|CREDENTIAL)", re.I)


def redact(key: str, value: str) -> str:
    """密钥形态的 key 只回长度，不回内容。非密钥原样返回。"""
    if value and _SECRETISH.search(key):
        return f"<redacted len={len(value)}>"
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ConfigChange:
    """一次配置变更的完整事实：谁、什么时候、把哪个旋钮从 X 改成 Y。

    `actor` 是**观察到的**操作人，不是猜的。Nacos 的推送回调里没有操作人身份
    （SDK 只给 tenant/group/data_id/content 四个参数），所以它由
    `nacos_source.py` 事后查一次 Nacos 的配置历史 API 补上；查不到就留空，
    并在 `actor_source` 里写明为什么留空 —— **不许编一个 actor 出来**。
    """

    key: str
    old: str
    new: str
    origin: str = ORIGIN_ENV
    actor: str = ""
    actor_source: str = ""
    detail: dict = field(default_factory=dict)
    at: str = field(default_factory=_now)

    def as_detail(self) -> dict:
        """落进 event_log `detail` 的那份 dict，值已过脱敏。"""
        out = {
            "key": self.key,
            "old": redact(self.key, self.old),
            "new": redact(self.key, self.new),
            "origin": self.origin,
            "actor": self.actor,
            "actor_source": self.actor_source,
            "at": self.at,
        }
        out.update(self.detail)
        return out


# ---------------------------------------------------------------------------
# 订阅：谁想知道「值变了」
# ---------------------------------------------------------------------------
_listeners: list[Callable[[ConfigChange], None]] = []
_listeners_lock = threading.Lock()


def subscribe(fn: Callable[[ConfigChange], None]) -> Callable[[], None]:
    """注册一个变更回调，返回取消订阅的函数。

    回调**可能在 Nacos 的事件循环线程里被调用**（推送到达那一刻），所以回调自己
    要线程安全。`audit.py` 的写入方走 `Store` 的锁，满足这条。

    回调抛异常一律吞掉并 WARNING：配置面的旁路不该掀掉主链路，这与
    `matrix_bus._record_denied`「写不进去也不许把异常抛给监听循环」同款。
    """
    with _listeners_lock:
        _listeners.append(fn)

    def _unsubscribe() -> None:
        with _listeners_lock:
            if fn in _listeners:
                _listeners.remove(fn)

    return _unsubscribe


def _emit(change: ConfigChange) -> None:
    with _listeners_lock:
        snapshot = list(_listeners)
    for fn in snapshot:
        try:
            fn(change)
        except Exception as exc:                        # noqa: BLE001
            log.warning("配置变更回调失败（%s），已忽略", exc)


# ---------------------------------------------------------------------------
# 配置源
# ---------------------------------------------------------------------------
class ConfigSource(ABC):
    """一个治理旋钮的读取口。`get()` 永远返回 `str`，永远不抛。"""

    #: `env` / `nacos`，进审计 detail，也是 repr 里唯一有用的字段。
    name: str = SOURCE_ENV

    #: 「读到的值和上次不一样」这件事，本源要不要当成一次配置变更报上去。
    #:
    #: `env` 源要（它没有别的渠道知道值变了）。**带推送的源不要**：推送回调那条路
    #: 才是权威的 —— 它知道确切时间，还能查到操作人。两条路都报的话，一次变更会落
    #: 两条审计，而且**读取路那条先到且没有操作人**（推送路要先去查一次历史 API，
    #: 慢）。2026-08-30 实测踩到过：真连用例里先落的是 `actor=''` 那条，
    #: 带操作人的那条还在路上。审计里出现一条查不到操作人的名单变更记录，
    #: 比不记更误导 —— 它看起来像「查过了，没查到」。
    emits_on_read: bool = True

    def __init__(self) -> None:
        self._seen: dict[str, str] = {}
        self._origins: dict[str, str] = {}
        self._seen_lock = threading.Lock()

    # -- 子类实现 ---------------------------------------------------------
    @abstractmethod
    def _resolve(self, key: str, default: str) -> tuple[str, str]:
        """返回 `(值, 来源)`。来源取 `ORIGIN_*` 之一。"""

    # -- 对外 -------------------------------------------------------------
    def get(self, key: str, default: str = "") -> str:
        """读一个旋钮。未配置 / 取不到 -> `default`。

        `EnvConfigSource` 下这一句等价于 `os.environ.get(key, default)` ——
        四个读取点原来写的就是它，替换后取值逐字节一致（`test_config_source.py`
        的四条对照用例钉着这件事）。
        """
        value, origin = self._resolve(key, default)
        if _listeners:                  # 没人听就一个字节都不多做（热路径）
            self._notice(key, value, origin)
        else:
            with self._seen_lock:
                self._seen[key] = value
                self._origins[key] = origin
        return value

    def explain(self, key: str) -> str:
        """上一次 `get(key)` 的来源。没读过返回 `""`。

        存在的理由是 §5.1 的「日志里要看得出本次用的是 Nacos 还是 Env」——
        光有日志不够，得有一个能写进断言的返回值，否则「降级了没有」只能靠读日志
        眼判，而那正是静默降级藏身的地方。
        """
        with self._seen_lock:
            return self._origins.get(key, "")

    def close(self) -> None:
        """释放资源。Env 源无事可做；Nacos 源关掉事件循环线程。"""

    # -- 内部 -------------------------------------------------------------
    def _notice(self, key: str, value: str, origin: str) -> None:
        """比对上次观察值，变了就广播一条 `ConfigChange`。

        **首次观察不算变更**：那时没有「从 X 改成」的 X，硬记一条会让审计里
        每次进程启动都多出四行噪音，把真正的变更淹掉。首读只立基线。
        """
        with self._seen_lock:
            known = key in self._seen
            old = self._seen.get(key, "")
            self._seen[key] = value
            self._origins[key] = origin
        if known and old != value and self.emits_on_read:
            _emit(ConfigChange(key=key, old=old, new=value, origin=origin,
                               actor=self._actor_for(key),
                               actor_source=self._actor_source_for(key)))

    def _actor_for(self, key: str) -> str:            # pragma: no cover - 子类覆盖
        return ""

    def _actor_source_for(self, key: str) -> str:     # pragma: no cover - 子类覆盖
        return ""

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r}>"


class EnvConfigSource(ConfigSource):
    """读 `os.environ`。**这是缺省实现，行为必须与没有本包时逐字节一致。**

    不做 strip、不做类型转换、不填默认值以外的任何东西 —— 四个读取点各自的
    「空串怎么算、解析不出怎么办」全部留在原地不动（`_max_replan` 的
    `(raw or "").strip()`、`_finance_threshold` 的 `float()` 兜底、
    `sandbox_timeout` 的两段告警、`parse_approvers` 的丢空白项）。
    把它们上收到这里是**重构**，不是本轨的活（铁律 4）。
    """

    name = SOURCE_ENV

    def _resolve(self, key: str, default: str) -> tuple[str, str]:
        raw = os.environ.get(key)
        if raw is None:
            return default, ORIGIN_DEFAULT
        return raw, ORIGIN_ENV


# ---------------------------------------------------------------------------
# 配置文档解析（Nacos 的一个 dataId 装四个旋钮）
# ---------------------------------------------------------------------------
def parse_config_document(raw: str) -> dict[str, str]:
    """把 Nacos 里那份文档解析成 `{key: value}`。

    两种写法都认：`{` 开头按 JSON 读，否则按 properties 逐行读
    （`#` / `!` 开头是注释，第一个 `=` 或 `:` 之前是 key）。

    解析不出来返回空 dict 而不抛 —— 控制台上一次手滑写坏一个字符，不该让
    审批链路当场停摆；空 dict 会让每个 key 都走「Nacos 无此项 -> 回落 env」，
    而那一档是有日志的。
    """
    text = (raw or "").strip()
    if not text:
        return {}
    if text[0] in "{[":
        try:
            import json
            loaded = json.loads(text)
        except Exception as exc:                        # noqa: BLE001
            log.warning("Nacos 配置文档不是合法 JSON（%s），按空文档处理", exc)
            return {}
        if not isinstance(loaded, dict):
            log.warning("Nacos 配置文档 JSON 顶层不是对象，按空文档处理")
            return {}
        return {str(k): "" if v is None else str(v) for k, v in loaded.items()}

    out: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped[0] in "#!":
            continue
        sep = min((i for i in (stripped.find("="), stripped.find(":")) if i > 0),
                  default=-1)
        if sep < 0:
            continue
        out[stripped[:sep].strip()] = stripped[sep + 1:].strip()
    return out


# ---------------------------------------------------------------------------
# 工厂与进程级单例
# ---------------------------------------------------------------------------
def create_config_source(name: str | None = None) -> ConfigSource:
    """按名字造一个配置源。`name` 不传就读 `MAOS_CONFIG_SOURCE`，再不然 env。

    `nacos` 这一支**在这里才 import** `maos.config.nacos_source`，而那个模块
    又只在构造实例时才 import `v2` —— 两层惰性，`import maos.config` 一行
    SDK 代码都碰不到（§5.0 第 3 条）。

    未知的源名回落 env 并 WARNING。这与 `create_store()` 的「拼错就抛」相反，
    理由是两者的失败后果不同：拿错存储后端会静默写错库，而配置面拼错时
    最坏结果是「治理没生效」—— 而那件事这里会喊出来，且喊完仍然能跑。
    """
    picked = (name or os.environ.get(ENV_CONFIG_SOURCE) or DEFAULT_SOURCE).strip().lower()
    if picked in ("", SOURCE_ENV):
        return EnvConfigSource()
    if picked == SOURCE_NACOS:
        from maos.config.nacos_source import NacosConfigSource
        return NacosConfigSource()
    log.warning("未知的 %s=%r：只认 %r 或 %r，降级 env",
                ENV_CONFIG_SOURCE, picked, SOURCE_ENV, SOURCE_NACOS)
    return EnvConfigSource()


_source: ConfigSource | None = None
_source_lock = threading.Lock()


def get_config_source() -> ConfigSource:
    """进程级单例。四个读取点走的都是它。

    单例而不是每次现造：Nacos 源持有一条长连接和一个事件循环线程，
    `_finance_threshold()` 每次判定都造一个的话，一次演示能开出几十条连接。
    Env 源本身无状态，做成单例只是为了让两支走同一条路径。
    """
    global _source
    if _source is None:
        with _source_lock:
            if _source is None:
                _source = create_config_source()
    return _source


def set_config_source(source: ConfigSource | None) -> None:
    """显式换一个源（测试与 `room_demo.py` 的演示装配用）。传 None = 清空。"""
    global _source
    with _source_lock:
        _source = source


def reset_config_source() -> None:
    """关掉并丢弃当前单例，下次 `get_config_source()` 重新按 env 造。"""
    global _source
    with _source_lock:
        current, _source = _source, None
    if current is not None:
        try:
            current.close()
        except Exception as exc:                        # noqa: BLE001
            log.warning("关闭配置源失败（%s）", exc)
