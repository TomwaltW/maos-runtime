"""退款域的角色目录 —— 「流程卡住后应由谁补偿」这一问的第一半。

## 这个模块答什么、不答什么

**答**：这个账号属于哪个岗（`role_of`）、这个岗有哪些人（`accounts_of`）、
这个岗在圆桌那套角色名里叫什么（`verdict_role_of`）。

**不答**：谁现在批得动。那是 `MAOS_APPROVERS` 的事，**权威在运行时配置，不在本文件
也不在 `scenarios/refund/roles.json`**（铁律 6：密钥与权限名单只读环境变量／配置面）。
`can_approve()` 因此要求调用方把名单显式递进来 —— 见下面那一节。

## 名单是权威，目录只是收窄

    can_approve(account, role) == (account 在 MAOS_APPROVERS 名单内)
                              and (目录说 account 属于 role)

两个条件是**与**，顺序上名单在先。写成「目录里是主管就放行」等于把权限名单搬进了
仓库里的一份 JSON —— 那份 JSON 谁都能提 PR 改，而名单改动是安全事件（`deploy/nacos.md`
§6.5 给 `MAOS_APPROVERS` 单独设了写入侧闸门，就是这个道理）。

反过来「名单里有、目录里没有」也不放行**按角色收窄的**动作：目录答不出他是哪个岗，
就没有依据说这一步该由他来。想放行任意名单内账号，把 `role` 传空字符串 ——
那表示这一步不按角色收窄，是显式的，不是兜底。

## 为什么 `approvers` 必须显式传，没有「不传就读 os.environ」那一支

口径逐字沿用 `maos/runtime/intent_dispatch.py::resolve_approvers` 的那条：
读进程环境等于绕开 `maos.config` 的配置源（`MAOS_CONFIG_SOURCE=nacos` 时那边现读
Nacos），自己开一条读法就是分叉的开始。所以本模块一次 `os.environ` 都不碰，
名单从哪来由调用方说了算（`maos/ingress/outcome_commands.py` 走的是配置面）。

## 两套角色名

`maos/roundtable/verdict.py` 的 `ESCALATION` 用 `supervisor` / `finance_manager`，
`maos/flows/contrast.py` 用 `region_manager`；本目录用的是把职责写全的那套
（`after_sales_supervisor` / `finance_reviewer` / `payment_ops` / `region_manager`）。
两套并存是并行开发的代价，不是设计意图 —— `canonical_role()` 认两种写法，
收敛成一套留给整合期（`docs/DECISIONS.md` 的 `## task-t117` 有映射表）。
本轨不碰 `maos/roundtable/**`（同期另一会话在改）。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

#: 语料位置。与 `fixtures.py` 同一个根 —— 本文件在 `maos/domain/refund/`，
#: 往上三级是仓库根。
_ROLES_PATH = Path(__file__).resolve().parents[3] / "scenarios" / "refund" / "roles.json"

# ---------------------------------------------------------------- 角色名常量
# 按名 import，不在各处抄字面量：抄错一个下划线的症状是「这个角色查不到人」，
# 而查不到人时按角色收窄的动作会静默落空，不报错。
ROLE_AFTER_SALES_SUPERVISOR = "after_sales_supervisor"
ROLE_FINANCE_REVIEWER = "finance_reviewer"
ROLE_PAYMENT_OPS = "payment_ops"
ROLE_REGION_MANAGER = "region_manager"

#: 工单的缺省承接岗。**由目录决定谁在这个岗上**，本常量只钉住「缺省是哪个岗」。
#: 补偿开出来的工单要人到支付渠道后台按幂等键对账，那是支付运维的活，不是审批岗的。
DEFAULT_TICKET_ROLE = ROLE_PAYMENT_OPS


class UnknownRole(KeyError):
    """目录里没有这个角色。

    刻意不兜底成「空账号列表」：一个拼错的角色名如果静默返回空列表，
    `/assign` 就会把工单派给谁也不是，而且一路不报错。
    """


def _load() -> dict[str, Any]:
    """读并校验目录。**不缓存**——本文件是几十行的静态语料，读它的代价可以忽略，
    而缓存会让测试之间互相串（换一份目录得先想起来清缓存），那种耦合不值这点开销。
    """
    raw = json.loads(_ROLES_PATH.read_text(encoding="utf-8"))
    roles = raw.get("roles") or {}
    if not isinstance(roles, dict) or not roles:
        raise ValueError(f"{_ROLES_PATH} 里没有 roles 段，角色目录是空的")
    for name, spec in roles.items():
        missing = [k for k in ("title", "duty", "accounts") if k not in spec]
        if missing:
            raise ValueError(f"角色 {name} 缺字段 {missing}（{_ROLES_PATH}）")
        if not isinstance(spec["accounts"], list):
            raise ValueError(f"角色 {name} 的 accounts 必须是数组（{_ROLES_PATH}）")
    return roles


def all_roles() -> tuple[str, ...]:
    """目录里的全部角色名，按字典序 —— 打印与断言都要确定序。"""
    return tuple(sorted(_load()))


def _spec(role: str) -> dict[str, Any]:
    roles = _load()
    name = canonical_role(role)
    if name not in roles:
        raise UnknownRole(
            f"角色目录里没有 {role!r}（已登记：{sorted(roles)}）；"
            f"新增角色请改 {_ROLES_PATH.name}，不要在代码里写死一个字符串")
    return roles[name]


def canonical_role(name: str) -> str:
    """把一个角色名归一到本目录的写法。认三种输入：

    1. 本目录的名字（`after_sales_supervisor`）—— 原样返回；
    2. `verdict.py` / `contrast.py` 那套名字（`supervisor` / `finance_manager`）——
       按 `verdict_role` 反查；
    3. 认不出的 —— **原样返回，不抛**。这里是归一化不是校验，把「认不出」升级成
       异常会让 `can_approve(acct, "随便什么")` 从「判 False」变成「炸」，
       而权限判定该给的是拒绝，不是异常。真要报错的是 `_spec()`。
    """
    key = str(name or "").strip()
    if not key:
        return ""
    roles = _load()
    if key in roles:
        return key
    for role, spec in roles.items():
        if str(spec.get("verdict_role") or "") == key:
            return role
    return key


def verdict_role_of(role: str) -> str:
    """本目录的角色名 -> `maos/roundtable/verdict.py` 那套名字。没有对应的给 `""`。

    `payment_ops` 就是没有对应的那个：它是**接单岗**不是审批岗，
    圆桌的 `ESCALATION` 升档表里不该有它 —— 升到一个不能拍板的岗上，
    房间里那句「请 X 拍板」就点不到能拍板的人。
    """
    return str(_spec(role).get("verdict_role") or "")


def accounts_of(role: str) -> tuple[str, ...]:
    """这个岗上有哪些账号，**按目录里的书写顺序**。

    顺序有意义：`/assign` 缺省派给第一个（首位是这个岗的主责人）。
    改成排序会让「谁是主责」由字符串大小决定，那是个没人看得懂的规则。
    """
    return tuple(str(a) for a in _spec(role)["accounts"] if str(a).strip())


def title_of(role: str) -> str:
    """岗位中文名，给人读的（房间回帖、工单打印）。"""
    return str(_spec(role)["title"])


def duty_of(role: str) -> str:
    """这个岗一句话的职责。工单派给谁之后，接单人要知道自己该干什么。"""
    return str(_spec(role)["duty"])


def role_of(account: str) -> str:
    """这个账号属于哪个岗。查不到给 `""`，**不抛**。

    查不到是常态而不是错误：`MAOS_APPROVERS` 里完全可以有目录还没登记的账号
    （新人入职当天就进名单，目录第二天才补）。抛异常会让一条正常的审批
    在「查一下他是哪个岗」这一步炸掉。

    一个账号出现在多个岗上时返回**字典序最小**的那个岗，且这属于目录写坏了 ——
    一人两岗意味着「按岗收窄」这件事在他身上失效。本函数不去猜哪个岗更该算数。
    """
    key = str(account or "").strip()
    if not key:
        return ""
    for role in sorted(_load()):
        if key in accounts_of(role):
            return role
    return ""


def can_approve(account: str, role: str, *, approvers: Iterable[str]) -> bool:
    """这个账号能不能以这个岗的身份放行。判据见模块抬头：**名单在先，目录收窄**。

    `role` 传空字符串 = 这一步不按角色收窄，只要在名单内就行。
    `approvers` 是 keyword-only 且必填 —— 见模块抬头「为什么必须显式传」。
    """
    key = str(account or "").strip()
    if not key or key not in frozenset(approvers or ()):
        return False
    want = canonical_role(role)
    if not want:
        return True
    try:
        return key in accounts_of(want)
    except UnknownRole:
        # 认不出的角色名一律拒。fail-closed：放行一个拼错的角色名，
        # 等于让「按岗收窄」被一个 typo 关掉，而且没有症状。
        return False
