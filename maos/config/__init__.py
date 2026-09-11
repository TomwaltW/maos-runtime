"""maos.config —— 治理旋钮的配置面（T28 立面，T131 补到八个，T136 补齐到十个）。

对外五样东西：

    ConfigSource         `get(key, default) -> str` 契约
    EnvConfigSource      缺省实现，等价 `os.environ.get`，缺省路径逐字节不变
    get_config_source()  进程级单例，按 `MAOS_CONFIG_SOURCE` 选源
    attach_config_audit()  订阅变更并逐条落 `event_log` 的 `ConfigChanged`
    GOVERNED_KEYS        推送到达时按它逐个 diff 出变更的那份清单（十个）

现在走这条路的**十一个**读取点，分属**十个**旋钮，**十个旋钮全在
`GOVERNED_KEYS` 里**（T136 补齐末两个）。这张表以
`grep -rn "get_config_source()" --include=*.py` 的实跑结果为准（T131 重核过一遍：
上一版自称「八个」，实际漏了三行 —— 两个从没进过表的旋钮，加上同一个键的第二个
读取点；T136 照同一条命令再核一遍，十一行不变）。表里的读取点写的是**真正调
`get_config_source()` 的那个函数**，不是它的调用方，否则改的人会照着表去改一个
自己不读配置的函数：

| 旋钮 | 读取点 | 进 `GOVERNED_KEYS` |
| :-- | :-- | :-- |
| `MAOS_MAX_REPLAN` | `maos/core/control_plane.py::ControlPlane._max_replan` | 是（T28） |
| `MAOS_MAX_REPLAN` | `maos/kb/plan_advice.py::env_replan_budget` | 同上（同键第二个读取点） |
| `MAOS_FINANCE_THRESHOLD` | `maos/runtime/gate.py::_finance_threshold` | 是（T28） |
| `MAOS_SANDBOX_TIMEOUT` | `maos/tools/sandbox.py::sandbox_timeout` | 是（T28） |
| `MAOS_APPROVERS` | `hiclaw/matrix_bus.py::current_approvers`（`RoomApprovalBridge._effective_approvers` 经它读） | 是（T28） |
| `MAOS_KB_ENABLED` | `maos/kb/__init__.py::kb_enabled` | 是（读取点 T35，进清单 T131） |
| `MAOS_KB_WEIGHTS` | `maos/kb/retriever.py::load_weights` | 是（读取点 T35，进清单 T131） |
| `MAOS_KB_ADVICE` | `maos/kb/plan_advice.py::advice_enabled` | 是（读取点 T119，进清单 T131） |
| `MAOS_FORCE_SCRIPTED` | `maos/model/client.py::forced_scripted` | 是（读取点 T125，进清单 T131） |
| `MAOS_SANDBOX_REQUIRE_CONTAINER` | `maos/tools/sandbox.py::require_container` | 是（读取点 T47，进清单 T136） |
| `MAOS_MAX_PLAN_REJECT` | `maos/runtime/plan_approval.py::_max_reject` | 是（读取点 T111，进清单 T136） |

同一个键出现两次不是笔误：`env_replan_budget` 是护栏 4「只许更紧不许更松」那条
判据的分母，它与控制面读的必须是同一个值（那一处刻意不告警，「同一个键读两次
只该吵一次」）。两处都走配置面，所以 Nacos 上改一次两处同时生效。

`MAOS_FORCE_SCRIPTED` 与表里别的旋钮有一处**不同**，读这份表时要分清：别的旋钮
缺省都是「不设 = 保持原行为」，它的缺省也是不设 = 原行为，但**几乎所有入口都替人设成 1**
（`run.py` / `scripts/demo_preflight.sh` / `scripts/make_evidence.py` 的子进程 /
`maos/tests/conftest.py`）。于是「不设」在实践中只剩两处：房间入口
（`hiclaw/room_ingress.py`）与显式 `--live-model`。这不是治理旋钮被滥用 ——
它要治的正是「演示机上 export 了 key，于是证据束悄悄变成真模型产的」这件事，
而那要的就是**缺省**，不是一个人人都要记得拧的开关（契约 §G）。

**「读取点接上了」与「进 `GOVERNED_KEYS`」仍是两件事**，今天这张表的两列碰巧全是
「是」，但那是补齐的结果、不是同一件事的两种说法，所以这里仍要写清楚：
`NacosConfigSource._resolve` 读快照时**不看** `GOVERNED_KEYS`（它对任何 key 都一视同仁），
所以一个只接了读取点的旋钮在 Nacos 上改了就是能改到、不用重启；`GOVERNED_KEYS` 只管
另一件事 —— 推送到达时按它 diff 出变更、落 `ConfigChanged` 审计。两件事分开的那一格
有个名字，写在这里免得下一个人再花一轮去发现它：**能治理，变更不落审计**。

这一格前后一共关过六个旋钮，最久的关了三轮。kb 那三个与 `MAOS_FORCE_SCRIPTED` 卡在
T35 / T119 / T125，每一轮的理由都一样：`maos/tests/test_config_source.py` 里那条断言
逐项钉着清单，而那个文件从来不在加旋钮那一轨的白名单里，加一行会当场把它变红。
T131 单开一轨补齐了这四个 —— 补的是**审计面**：`MAOS_FORCE_SCRIPTED` 从 1 改成 0
现在会落一条 `ConfigChanged`，而在此之前 `event_log` 里一个字都没有，尽管这个旋钮
一改整批证据束的成本读数含义就全变了。

表格末两行是 T131 重核表格时新发现的（上一版表格自称完整，实际没有它们），当轮
**没有一并加进清单**是守派单范围：那一轨点名的是四个，而这两个此前没有任何一笔账
记着。T136 把它们补上，这一格于是空了：`MAOS_SANDBOX_REQUIRE_CONTAINER` 是**安全
旋钮**（要不要强制容器隔离，从 1 改成 0 意味着沙箱当场退回本机直跑），按「比 kb
那三个更值得审计」的同一把尺，它比 `MAOS_FORCE_SCRIPTED` 还靠前；`MAOS_MAX_PLAN_REJECT`
与清单里的 `MAOS_MAX_REPLAN` 是同一类成本闸。实际代价与 T131 估的一致：两行改动 +
一条断言。判据不是这段字，是 `test_a_push_now_audits_the_two_knobs_t136_added`。

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
