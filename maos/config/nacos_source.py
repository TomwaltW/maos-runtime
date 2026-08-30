"""`NacosConfigSource` —— 四个治理旋钮的动态配置面。

**只有 `MAOS_CONFIG_SOURCE=nacos` 时才会有人 import 本模块**，而 `v2`（也就是
`nacos-sdk-python` 的顶层包名，不是 `nacos`）又只在 `__init__` 里才 import。
两层惰性合起来买到的是同一件事：没装 SDK 的机器上，`import maos.config` 与
`python3 -m pytest maos/tests` 一行 SDK 代码都不会碰到（§5.0 第 3、4 条）。

## 为什么要开一个事件循环线程

`nacos-sdk-python` 3.2.0 是**纯 async 的**：`create_config_service`、`get_config`、
`add_listener` 三个全是协程，且监听回调本身也必须是 `async def`（SDK 内部写死
`await listener_wrap.listener(tenant, group, data_id, content)`，源码在
`v2/nacos/config/model/config.py:89`）。而 MAOS 从上到下是同步的 ——
`_finance_threshold()` 在 Gate 判定里被同步调用，`handle_message()` 在房间监听
循环里被同步调用，两处都不可能 `await`。

三条路里选了第三条：

  ① 把 MAOS 改成 async            —— 动整个控制面，远超本轨；
  ② 每次 `get()` 现 `asyncio.run()` —— 每次判定重连一次 gRPC，秒级延迟进闸；
  ③ **把 async 客户端关进一条专用事件循环线程，`get()` 只读内存快照** ✅

第三条还顺手买到了动态治理：Nacos 推送到达时回调在那条线程里跑，更新快照；
主线程下一次 `get()` 读到的就是新值，**不重启、不轮询、不阻塞**。§5.4 那个
「改完名单下一次审批就按新名单判」的演示，成立的正是这一条。

## 降级三态，每一档都有日志

`get()` 永远不抛。SDK 没装 / 连不上 / 该项在 Nacos 没有，逐级回落到内置的
`EnvConfigSource`，`explain(key)` 如实返回 `nacos` 还是 `env` ——
**静默降级会让人以为治理生效了，而实际上没有，这比不接更坏。**

## 「谁改的」不许编

SDK 的推送回调只给四个参数，里面**没有操作人**。所以操作人是事后从 Nacos 自己的
配置历史 API（`/nacos/v1/cs/history`）查回来的，查到什么记什么；查不到就留空并在
`actor_source` 里写明原因。审计行里那个 `actor` 要么是 Nacos 记的那个用户，
要么是空 —— 不会是一个我们编出来的名字。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from maos.config.source import (
    GOVERNED_KEYS,
    ORIGIN_NACOS,
    SOURCE_NACOS,
    ConfigChange,
    ConfigSource,
    EnvConfigSource,
    _emit,
    parse_config_document,
)

log = logging.getLogger("maos.config.nacos")

__all__ = [
    "DEFAULT_DATA_ID",
    "DEFAULT_GROUP",
    "DEFAULT_SERVER",
    "ENV_DATA_ID",
    "ENV_GROUP",
    "ENV_NAMESPACE",
    "ENV_PASSWORD",
    "ENV_SERVER",
    "ENV_TIMEOUT_MS",
    "ENV_USERNAME",
    "NacosConfigSource",
    "NacosUnavailable",
]

#: 连接参数。**口令只从环境变量读，禁止写进任何文件**（铁律 6）。
ENV_SERVER = "MAOS_NACOS_SERVER"
ENV_NAMESPACE = "MAOS_NACOS_NAMESPACE"
ENV_GROUP = "MAOS_NACOS_GROUP"
ENV_DATA_ID = "MAOS_NACOS_DATA_ID"
ENV_USERNAME = "MAOS_NACOS_USERNAME"
ENV_PASSWORD = "MAOS_NACOS_PASSWORD"          # noqa: S105 —— 这是变量名，不是口令
ENV_TIMEOUT_MS = "MAOS_NACOS_TIMEOUT_MS"

DEFAULT_SERVER = "127.0.0.1:8848"
DEFAULT_GROUP = "DEFAULT_GROUP"
DEFAULT_DATA_ID = "maos-governance"
DEFAULT_TIMEOUT_MS = 5000

#: 查操作人的重试预算。Nacos 的配置历史异步落库，推送先到、历史后写。
HISTORY_ATTEMPTS = 5
HISTORY_RETRY_DELAY = 0.25


class NacosUnavailable(RuntimeError):
    """Nacos 此刻用不了（没装 SDK / 没连上 / 拉不到文档）。

    **本模块自己接住它并降级**，不往调用方抛 —— 对外暴露它是为了让
    `test_config_source.py` 能精确断言「哪一档降级了」，而不是靠读日志眼判。
    """


# ---------------------------------------------------------------------------
# 把 async-only 的 SDK 关进一条专用事件循环线程
# ---------------------------------------------------------------------------
class _LoopThread:
    """一条守护线程 + 一个只跑 SDK 协程的事件循环。

    守护线程是刻意的：MAOS 的入口脚本跑完就该退出，不该被一条配置面的长连接
    吊住。`close()` 会正常停循环；没人调 `close()` 时进程照样退得掉。
    """

    def __init__(self, name: str = "maos-nacos") -> None:
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5)

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.call_soon(self._ready.set)
        self._loop.run_forever()

    def submit(self, coro, timeout: float) -> Any:
        """在循环线程里跑一个协程并同步等结果。超时/失败原样抛给调用方。"""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout)
        except Exception:
            future.cancel()
            raise

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=3)


# ---------------------------------------------------------------------------
# 「谁改的」—— 查 Nacos 自己的配置历史
# ---------------------------------------------------------------------------
def _http_json(url: str, *, data: bytes | None = None, timeout: float = 3.0) -> Any:
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    if data:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=timeout) as resp:   # noqa: S310
        return json.loads(resp.read().decode("utf-8") or "{}")


class _HistoryLookup:
    """从 `/nacos/v1/cs/history` 查最近一次变更的 `srcUser` / `srcIp`。

    走 stdlib `urllib` 而不是 SDK：SDK 3.2.0 的 `NacosConfigService` 没有暴露
    历史接口，而为了这一个字段去引第二个依赖不值当 —— 何况这段代码只在
    `MAOS_CONFIG_SOURCE=nacos` 且真发生变更时才跑。

    **全程 best-effort**：任何一步失败都返回 `("", 原因)`，让审计行如实写
    「操作人未知 + 为什么未知」。宁可留空，也不许编一个 actor。
    """

    def __init__(self, base_url: str, username: str, password: str, tenant: str) -> None:
        self._base = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._tenant = tenant

    def _token(self) -> str:
        if not self._username:
            return ""
        body = urllib.parse.urlencode(
            {"username": self._username, "password": self._password}).encode()
        payload = _http_json(f"{self._base}/nacos/v1/auth/login", data=body)
        return str(payload.get("accessToken") or "")

    def who(self, data_id: str, group: str, *, attempts: int = HISTORY_ATTEMPTS,
            delay: float = HISTORY_RETRY_DELAY) -> tuple[str, str]:
        """返回 `(actor, actor_source)`。查不到时 actor 为空串。

        **有界重试是必需的，不是保险**：Nacos 的配置历史是**异步**落的，推送先到、
        历史后写。实测（2026-08-30，本机 v2.4.3）一个全新 dataId 第一次改动时，
        推送到达那一刻 `pageItems` 还是空的 —— 不重试就会把一条本来查得到操作人的
        变更记成「操作人未知」，而那正是审计最不该出错的地方。

        总等待上限约 `attempts * delay` 秒（缺省 1.25s）。上限是硬的：快照在调用
        本方法**之前**就已经换好了，审批已经按新名单判，这里多等的只是审计行。
        """
        params = {
            "search": "accurate", "dataId": data_id, "group": group,
            "tenant": self._tenant, "pageNo": "1", "pageSize": "1",
        }
        try:
            token = self._token()
            if token:
                params["accessToken"] = token
            url = f"{self._base}/nacos/v1/cs/history?{urllib.parse.urlencode(params)}"
            items: list = []
            for attempt in range(max(attempts, 1)):
                items = (_http_json(url) or {}).get("pageItems") or []
                if items:
                    break
                if attempt + 1 < attempts:
                    time.sleep(delay)
            if not items:
                return "", (f"nacos-history-api: {attempts} 次重试后仍无历史记录"
                            f"（历史异步落库，本次没等到）")
            top = items[0]
            user = str(top.get("srcUser") or "").strip()
            ip = str(top.get("srcIp") or "").strip()
            if not user:
                # Nacos 在未开鉴权时 srcUser 就是空的 —— 这不是我们查失败，
                # 是那台 Nacos 本来就没记。照实说，别拿 srcIp 冒充操作人。
                return "", f"nacos-history-api: srcUser 为空（srcIp={ip or '未知'}）"
            return (f"{user}@{ip}" if ip else user), "nacos-history-api"
        except Exception as exc:                        # noqa: BLE001
            return "", f"nacos-history-api 查询失败: {type(exc).__name__}"


# ---------------------------------------------------------------------------
# 配置源
# ---------------------------------------------------------------------------
class NacosConfigSource(ConfigSource):
    """从 Nacos 的一个 dataId 里读四个旋钮，推送到达即生效。

    构造时就连一次并拉一次全量（同 `create_store()` 的「交付的是一个已经连上的
    后端，不是一个还不知道能不能用的壳」）—— 区别是这里连不上**不抛**，
    转成降级态继续跑，因为配置面不可达不该把整个 MAOS 拖停。
    """

    name = SOURCE_NACOS

    #: 变更由**推送回调**单点上报，读取路不再报第二遍（理由见基类那条注释）。
    #: 推送那条知道确切时间、查得到操作人；读取路两样都没有，且因为推送路要先查
    #: 一次历史 API，读取路那条还会**抢先**落库 —— 于是审计里留下的是没有操作人的
    #: 那一条。这不是优化，是正确性。
    emits_on_read = False

    def __init__(self, *, server: str | None = None, namespace: str | None = None,
                 group: str | None = None, data_id: str | None = None,
                 username: str | None = None, password: str | None = None,
                 timeout_ms: int | None = None, connect: bool = True) -> None:
        super().__init__()
        env = os.environ
        self.server = (server if server is not None else env.get(ENV_SERVER)) or DEFAULT_SERVER
        self.namespace = (namespace if namespace is not None
                          else env.get(ENV_NAMESPACE)) or ""
        self.group = (group if group is not None else env.get(ENV_GROUP)) or DEFAULT_GROUP
        self.data_id = (data_id if data_id is not None
                        else env.get(ENV_DATA_ID)) or DEFAULT_DATA_ID
        self._username = (username if username is not None else env.get(ENV_USERNAME)) or ""
        self._password = (password if password is not None else env.get(ENV_PASSWORD)) or ""
        try:
            self.timeout_ms = int(timeout_ms if timeout_ms is not None
                                  else (env.get(ENV_TIMEOUT_MS) or DEFAULT_TIMEOUT_MS))
        except (TypeError, ValueError):
            log.warning("%s 不是整数，回退 %dms", ENV_TIMEOUT_MS, DEFAULT_TIMEOUT_MS)
            self.timeout_ms = DEFAULT_TIMEOUT_MS

        #: 降级到的那个源。是**内置的**而不是「换成它」—— 换掉的话
        #: `explain()` 会说自己是 env 源，「本来想走 Nacos 但降级了」这件事就丢了。
        self._fallback = EnvConfigSource()

        self._snapshot: dict[str, str] = {}
        self._snapshot_lock = threading.Lock()
        self._loop: _LoopThread | None = None
        self._service: Any = None
        self._degraded_reason: str = ""
        self._missing_logged: set[str] = set()
        self._history = _HistoryLookup(
            f"http://{self.server}", self._username, self._password, self.namespace)

        if connect:
            self._connect()

    # -- 状态 -------------------------------------------------------------
    @property
    def degraded(self) -> bool:
        """当前是不是整体降级态（连不上 / 没装 SDK）。"""
        return bool(self._degraded_reason)

    @property
    def degraded_reason(self) -> str:
        """降级原因的人话版本。没降级返回 `""`。"""
        return self._degraded_reason

    def snapshot(self) -> dict[str, str]:
        """当前从 Nacos 拉到的那份文档解析结果的副本（测试与演示用）。"""
        with self._snapshot_lock:
            return dict(self._snapshot)

    # -- 连接 -------------------------------------------------------------
    def _connect(self) -> None:
        try:
            from v2.nacos import (ClientConfigBuilder, ConfigParam,   # noqa: PLC0415
                                  NacosConfigService)
        except ImportError as exc:
            self._degrade(f"nacos-sdk-python 未安装（{exc}）——"
                          f" pip install nacos-sdk-python，见 deploy/nacos.md 可选依赖一节")
            return

        timeout_s = max(self.timeout_ms / 1000.0, 1.0)
        try:
            self._loop = _LoopThread()
            builder = (ClientConfigBuilder()
                       .server_address(self.server)
                       .namespace_id(self.namespace)
                       .timeout_ms(self.timeout_ms)
                       .log_level("WARNING"))
            if self._username:
                builder = builder.username(self._username).password(self._password)
            client_config = builder.build()

            self._service = self._loop.submit(
                NacosConfigService.create_config_service(client_config), timeout_s * 3)
            raw = self._loop.submit(
                self._service.get_config(
                    ConfigParam(data_id=self.data_id, group=self.group)), timeout_s * 3)
            self._apply(raw or "", first=True)
            self._loop.submit(
                self._service.add_listener(self.data_id, self.group, self._on_push),
                timeout_s * 3)
        except Exception as exc:                        # noqa: BLE001
            self._degrade(f"Nacos 不可达（{type(exc).__name__}: {exc}）—— "
                          f"server={self.server} namespace={self.namespace or '<public>'} "
                          f"group={self.group} dataId={self.data_id}")
            self.close()
            return

        log.info("Nacos 配置面已接通：server=%s namespace=%s group=%s dataId=%s，"
                 "本次拉到 %d 项", self.server, self.namespace or "<public>",
                 self.group, self.data_id, len(self._snapshot))

    def _degrade(self, reason: str) -> None:
        self._degraded_reason = reason
        log.warning("配置面降级 env：%s", reason)

    # -- 推送 -------------------------------------------------------------
    async def _on_push(self, tenant: str, group: str, data_id: str, content: str) -> None:
        """Nacos 推来一份新文档。**这个回调跑在事件循环线程里。**

        SDK 写死要 `async def` 且四个位置参数（`v2/nacos/config/model/config.py:89`），
        签名一个字都不能改。异常一律吞掉：这条回调是 SDK 内部 `await` 的，
        往上抛会打断整条订阅链，症状是「改了配置再也不推了」，离原因很远。
        """
        try:
            log.info("Nacos 推送到达：tenant=%s group=%s dataId=%s（%d 字节）",
                     tenant or "<public>", group, data_id, len(content or ""))
            self._apply(content or "", first=False)
        except Exception as exc:                        # noqa: BLE001
            log.warning("处理 Nacos 推送失败（%s），沿用上一版配置", exc)

    def _apply(self, raw: str, *, first: bool) -> None:
        """解析新文档、换掉快照、把 `GOVERNED_KEYS` 里变了的逐个广播出去。

        首次拉取（`first=True`）只立基线不广播 —— 进程启动不是一次「配置变更」，
        每次启动多出四行审计只会把真正的变更淹掉（同 `ConfigSource._notice`）。
        """
        parsed = parse_config_document(raw)
        with self._snapshot_lock:
            old, self._snapshot = self._snapshot, parsed
        if first:
            return

        changed = [(k, old.get(k, ""), parsed.get(k, ""))
                   for k in GOVERNED_KEYS if old.get(k, "") != parsed.get(k, "")]
        if not changed:
            return
        # 一次推送只查一次操作人：变更多半来自同一次保存，查 N 次拿到的是同一条历史。
        actor, actor_source = self._history.who(self.data_id, self.group)
        for key, before, after in changed:
            log.info("配置变更 %s：%r -> %r（操作人 %s）",
                     key, before, after, actor or "未知")
            _emit(ConfigChange(
                key=key, old=before, new=after, origin=ORIGIN_NACOS,
                actor=actor, actor_source=actor_source,
                detail={"server": self.server, "namespace": self.namespace,
                        "group": self.group, "data_id": self.data_id},
            ))

    # -- 读取 -------------------------------------------------------------
    def _resolve(self, key: str, default: str) -> tuple[str, str]:
        with self._snapshot_lock:
            hit = self._snapshot.get(key)
        if hit is not None and hit != "":
            return hit, ORIGIN_NACOS
        if not self.degraded and key not in self._missing_logged:
            self._missing_logged.add(key)
            log.info("%s 在 Nacos（%s/%s）无此项，本次取值来自 env",
                     key, self.group, self.data_id)
        return self._fallback._resolve(key, default)

    # `_actor_for` / `_actor_source_for` 这里不覆盖：`emits_on_read = False` 之后
    # 读取路根本不产生变更事件，覆盖了也没人调。操作人只从 `_apply` 那条路来。

    def close(self) -> None:
        service, loop = self._service, self._loop
        self._service, self._loop = None, None
        if service is not None and loop is not None:
            try:
                loop.submit(service.shutdown(), 3)
            except Exception as exc:                    # noqa: BLE001
                # 打类型名而不是只打 `exc`：SDK 关闭路径抛的异常**消息常常是空的**，
                # 只打 exc 会得到「关闭 Nacos 客户端失败（）」这种查不动的日志。
                log.warning("关闭 Nacos 客户端失败（%s: %s）", type(exc).__name__, exc)
        if loop is not None:
            try:
                loop.close()
            except Exception as exc:                    # noqa: BLE001
                log.warning("关闭 Nacos 事件循环失败（%s: %s）",
                            type(exc).__name__, exc)
