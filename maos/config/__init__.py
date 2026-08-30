"""maos.config —— 四个治理旋钮的配置面（T28）。

对外五样东西：

    ConfigSource         `get(key, default) -> str` 契约
    EnvConfigSource      缺省实现，等价 `os.environ.get`，缺省路径逐字节不变
    get_config_source()  进程级单例，按 `MAOS_CONFIG_SOURCE` 选源
    attach_config_audit()  订阅变更并逐条落 `event_log` 的 `ConfigChanged`
    GOVERNED_KEYS        本轮真正搬上配置面的四个旋钮

现在走这条路的四个读取点：

| 旋钮 | 读取点 |
| :-- | :-- |
| `MAOS_MAX_REPLAN` | `maos/core/control_plane.py::ControlPlane._max_replan` |
| `MAOS_FINANCE_THRESHOLD` | `maos/runtime/gate.py::_finance_threshold` |
| `MAOS_SANDBOX_TIMEOUT` | `maos/tools/sandbox.py::sandbox_timeout` |
| `MAOS_APPROVERS` | `hiclaw/matrix_bus.py::RoomApprovalBridge._effective_approvers` |

`MAOS_KB_ENABLED` / `MAOS_KB_WEIGHTS` 这一轮**没接**：`maos/kb/**` 同期归 T24 / T25，
同轮并行改同一个文件必冲突。接口留成现在这个形状就是为了它们并轨后直接往
`GOVERNED_KEYS` 加两行、把那两处的 `os.environ.get` 换成 `get_config_source().get`
即可，别的一行都不用改（`docs/DECISIONS.md` 有一行记着这件事）。

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
