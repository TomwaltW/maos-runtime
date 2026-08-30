"""maos.config —— 四个治理旋钮的配置面（T28）。

对外五样东西：

    ConfigSource         `get(key, default) -> str` 契约
    EnvConfigSource      缺省实现，等价 `os.environ.get`，缺省路径逐字节不变
    get_config_source()  进程级单例，按 `MAOS_CONFIG_SOURCE` 选源
    attach_config_audit()  订阅变更并逐条落 `event_log` 的 `ConfigChanged`
    GOVERNED_KEYS        推送到达时按它逐个 diff 出变更的那份清单（四个）

现在走这条路的**六个**读取点：

| 旋钮 | 读取点 | 进 `GOVERNED_KEYS` |
| :-- | :-- | :-- |
| `MAOS_MAX_REPLAN` | `maos/core/control_plane.py::ControlPlane._max_replan` | 是 |
| `MAOS_FINANCE_THRESHOLD` | `maos/runtime/gate.py::_finance_threshold` | 是 |
| `MAOS_SANDBOX_TIMEOUT` | `maos/tools/sandbox.py::sandbox_timeout` | 是 |
| `MAOS_APPROVERS` | `hiclaw/matrix_bus.py::RoomApprovalBridge._effective_approvers` | 是 |
| `MAOS_KB_ENABLED` | `maos/kb/__init__.py::kb_enabled` | 否（T35） |
| `MAOS_KB_WEIGHTS` | `maos/kb/retriever.py::load_weights` | 否（T35） |

**「读取点接上了」与「进 `GOVERNED_KEYS`」是两件事**，kb 那两个旋钮现在正好卡在
中间，所以这里要写清楚：`NacosConfigSource._resolve` 读快照时**不看**
`GOVERNED_KEYS`（它对任何 key 都一视同仁），所以那两个旋钮在 Nacos 上改了就是
能改到、不用重启。`GOVERNED_KEYS` 只管一件事 —— 推送到达时按它 diff 出变更、
落 `ConfigChanged` 审计。于是 kb 那两个当前的现况是：**能治理，变更不落审计**。

没顺手把它们加进 `GOVERNED_KEYS`，是因为 `maos/tests/test_config_source.py` 里
`test_governed_keys_are_exactly_the_four_this_track_owns` 钉着「就是这四个」，
而那个文件同轮归另一条轨，加两行会当场把它变红。加进来是**一行改动 + 一条断言**，
`docs/BACKLOG.md` 的 `## task-T35` 记着这笔账。

**本包是纯新增，且缺省路径一个字节都没变**：`MAOS_CONFIG_SOURCE` 未设时
`get_config_source()` 给的是 `EnvConfigSource`，`get()` 就是 `os.environ.get`；
`nacos-sdk-python`（63 个包 / 135MB）**不在 `pyproject.toml` 里**，只在
`deploy/nacos.md` 的可选依赖一节。判据是全量测试与 `python3 run.py`。
"""

from maos.config.audit import CONFIG_CHANGED_EVENT, ConfigAuditor, attach_config_audit
from maos.config.source import (
    ENV_CONFIG_SOURCE,
    GOVERNED_KEYS,
    ORIGIN_DEFAULT,
    ORIGIN_ENV,
    ORIGIN_NACOS,
    SOURCE_ENV,
    SOURCE_NACOS,
    ConfigChange,
    ConfigSource,
    EnvConfigSource,
    create_config_source,
    get_config_source,
    parse_config_document,
    redact,
    reset_config_source,
    set_config_source,
    subscribe,
)

__all__ = [
    "CONFIG_CHANGED_EVENT",
    "ENV_CONFIG_SOURCE",
    "GOVERNED_KEYS",
    "ORIGIN_DEFAULT",
    "ORIGIN_ENV",
    "ORIGIN_NACOS",
    "SOURCE_ENV",
    "SOURCE_NACOS",
    "ConfigAuditor",
    "ConfigChange",
    "ConfigSource",
    "EnvConfigSource",
    "attach_config_audit",
    "create_config_source",
    "get_config_source",
    "parse_config_document",
    "redact",
    "reset_config_source",
    "set_config_source",
    "subscribe",
]
