"""意图派发与权限闸 —— `Intent` 进，`DispatchResult` 出。

自然语言接入面的第二段：T66 把一句人话解析成 `Intent`，本模块决定这个 `Intent`
**能不能变成动作**，以及变成哪种动作。它只产出进程内数据，不写库、不发事件、
不碰状态机（跨轨契约 R3）。

## 🔴 R1：自然语言只确认，不执行

`action=approve` / `action=reject` 的 `Intent` **永远只产出 `KIND_CONFIRM`**，
本模块任何一条路径都不会调用 `HumanApprovalQueue.decide()`。真正让状态迁移的
唯一入口，仍然是显式的 `/approve <task_id>` 文本，走 `RoomApprovalBridge`，
那条路径本轮一行都不改。

**为什么**：模型判断错一次，等于越权批掉一笔生产变更。这与铁律 8 同源 ——
权威动作不能由推断产生。自然语言层买的是「少打字、看得懂」，不是「替人做决定」。
钉子测试见 `test_intent_dispatch.py::test_r1_*`：名单内的人说「同意」，
注入的假 `decide` 必须零调用。

## 判定顺序不许换

1. `UNKNOWN` -> `KIND_IGNORED`（闲聊，与编排无关，不进权限闸）
2. `sender` 不在 `approvers` -> `KIND_DENIED`（**先查你是谁，再谈你想干什么**）
3. `approve` / `reject` -> `KIND_CONFIRM`（R1）
4. `status` -> 查库回一句状态，`KIND_DONE`

第 2 步必须在第 3 步之前。反过来的话，名单外的人也能收到一张格式完整的确认
卡片，等于告诉他「照这个格式发就能批」—— 把权限边界当成了 UI 提示。

第 1 步在第 2 步之前是刻意的：闲聊被判「无审批权限」既噪音又误导，而 `UNKNOWN`
本来就产不出任何动作，放行它不扩大任何权限面。

## 为什么不 import `maos.nlu`

`Intent` 由 T66 定义，本模块与它并行开发，import 会让两轨串成一条依赖链。
这里只按字段名读（`action` / `task_id` / `raw_text`），字段名逐字对齐跨轨契约
§1，任何 `Intent` 形状的对象都能喂进来。整合时不必改本文件。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

#: 审批人名单的环境变量名。逐字对齐 `hiclaw/matrix_bus.py` 的 `ENV_APPROVERS`。
#: 这里硬编码而不 import：hiclaw 是可选依赖层，运行时不该为读一个键名把它绑进来。
#: 口径不分叉由测试保证，不是靠共享符号（见 `resolve_approvers`）。
ENV_APPROVERS = "MAOS_APPROVERS"

# -- Intent.action 的取值。逐字对齐跨轨契约 §1（T66 的 maos/nlu/intent.py）------
# INTEGRATION-POINT: 整合时若要收敛成一处定义，从 T66 那边 import 这四个常量，
# 本模块其余逻辑不动。现在复制一份是并行的代价，不是设计意图。
ACTION_APPROVE = "approve"
ACTION_REJECT = "reject"
ACTION_STATUS = "status"
ACTION_UNKNOWN = "unknown"

# -- 派发结局。逐字对齐跨轨契约 §1.2 ------------------------------------------
KIND_CONFIRM = "confirm"    # 需要人再发一次显式指令才生效
KIND_DONE = "done"          # 查询类请求已回答
KIND_DENIED = "denied"      # 发言人不在 MAOS_APPROVERS 名单
KIND_IGNORED = "ignored"    # unknown / 与编排无关的闲聊

KINDS = frozenset({KIND_CONFIRM, KIND_DONE, KIND_DENIED, KIND_IGNORED})


@dataclass(frozen=True)
class DispatchResult:
    """一次派发的结局。`text` 是要发回房间的话，给人读的。"""

    kind: str
    text: str
    task_id: str = ""

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"未知的派发结局：{self.kind!r}")


def resolve_approvers(env: Mapping[str, str]) -> frozenset[str]:
    """从一份 env 里解出审批人名单。逗号分隔，空白项丢弃，**不做格式校验**。

    口径逐字对齐 `hiclaw/matrix_bus.py` 的 `parse_approvers` / `current_approvers`
    —— 同一份 env 输入，两边必须给出同一个名单，由
    `test_intent_dispatch.py` 的对照测试钉住。**口径分叉 = 一边放行一边拒绝**，
    那是安全事故，不是风格问题。

    不 import 那边的实现有两个理由：它属于 hiclaw 可选依赖层（`maos.runtime`
    不该为一行解析绑上它），而真正要保证的是**行为一致**，共享符号只是让分叉
    更难发现的一种写法。

    不校验「必须长得像 @user:server」：Matrix user id 的形态由 homeserver 定，
    在这里画一条自造的正则，只会在换 homeserver 那天把合法审批人挡在门外。

    不 `lower()`：Matrix id 的 localpart 大小写敏感，`@Boss` 与 `@boss` 是两个人。

    `env` 必须显式传，没有「不传就读 `os.environ`」那一支：读进程环境等于绕开
    `maos.config` 的配置源（`MAOS_CONFIG_SOURCE=nacos` 时那边现读 Nacos），
    自己开一条读法就是分叉的开始。调用方从哪读，由调用方说了算。
    """
    raw = env.get(ENV_APPROVERS, "")
    if not raw:
        return frozenset()
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def dispatch_intent(intent, *, store, cp, sender: str,
                    approvers: Iterable[str]) -> DispatchResult:
    """把一个 `Intent` 派发成一个结局。判定顺序见模块抬头，不许换。

    `cp` 当前不参与任何判定 —— 留在签名里是因为跨轨契约 §1.2 这么定的，而 R1 下
    本模块不做任何状态迁移，所以它没有用武之地。**这不是遗漏**：哪天它有了用处，
    那一定意味着自然语言层开始执行动作了，那才是要停下来问人的时刻。

    `approvers` 收 `Iterable` 而不是 `Sequence`：调用方拿到的常是
    `resolve_approvers` 返回的 `frozenset`，多一次转 list 只是噪音。
    空名单一律 `KIND_DENIED` —— 配置缺失时放行是最经典的权限漏洞形态。
    """
    action = getattr(intent, "action", ACTION_UNKNOWN) or ACTION_UNKNOWN
    task_id = getattr(intent, "task_id", "") or ""

    # 1. 认不出来的话，本来就产不出动作。不进权限闸。
    if action == ACTION_UNKNOWN:
        return DispatchResult(kind=KIND_IGNORED, text="", task_id=task_id)

    # 2. 先查「你是谁」。这一步必须在看意图内容之前，理由见模块抬头。
    if sender not in frozenset(approvers or ()):
        return DispatchResult(
            kind=KIND_DENIED,
            text=f"无审批权限：{sender} 不在 {ENV_APPROVERS} 名单内",
            task_id=task_id,
        )

    # 3. 🔴 R1：只确认，不执行。这里不许出现 decide()。
    if action in (ACTION_APPROVE, ACTION_REJECT):
        if not task_id:
            # R2 保证解析侧不会放出没有 task_id 的 approve/reject；真出现了
            # 也不能瞎猜一个 —— 猜错就是让人确认了一个他没说过的任务。
            return DispatchResult(
                kind=KIND_IGNORED,
                text="没听出你说的是哪个任务，请带上 task_id 再说一次。",
                task_id="",
            )
        verb = "批准" if action == ACTION_APPROVE else "驳回"
        cmd = "/approve" if action == ACTION_APPROVE else "/reject"
        return DispatchResult(
            kind=KIND_CONFIRM,
            text=f"你是想{verb} {task_id} 吗？确认请发：{cmd} {task_id}",
            task_id=task_id,
        )

    # 4. 查询类：读库回一句状态。只读，不迁移。
    if action == ACTION_STATUS:
        return _status_result(store, task_id)

    # 契约把 ACTIONS 冻成四个值，走到这里说明上游放出了契约外的 action。
    # 不抛异常：房间监听循环不该被一句话掀翻（同 RoomApprovalBridge 的取向）。
    return DispatchResult(kind=KIND_IGNORED, text="", task_id=task_id)


def _status_result(store, task_id: str) -> DispatchResult:
    """查一个任务的状态。查不到就说查不到，不编。

    查不到时给 `KIND_IGNORED` 而不是 `KIND_DONE`：`DONE` 的语义是「这条请求
    真的被回答了」，把「没查到」也算成 DONE，上游就分不出「答了」和「没答」。
    """
    if not task_id:
        return DispatchResult(
            kind=KIND_IGNORED,
            text="想查哪个任务？请带上 task_id。",
            task_id="",
        )
    task = store.get_task(task_id) if store is not None else None
    if not task:
        return DispatchResult(
            kind=KIND_IGNORED,
            text=f"没查到 {task_id}。",
            task_id=task_id,
        )
    state = task.get("state", "")
    return DispatchResult(
        kind=KIND_DONE,
        text=f"{task_id} 当前状态：{state}。",
        task_id=task_id,
    )
