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

## 两套角色名（T123 收成了一套）

本目录用的是把职责写全的那套（`after_sales_supervisor` / `finance_reviewer` /
`payment_ops` / `region_manager`），`maos/roundtable/verdict.py` 与
`maos/flows/contrast.py` 用的是房间里念得出口的那套（`supervisor` /
`finance_manager` / `region_manager`）。

T117 留下的欠账是**两套名字各有一份内部表**：`verdict.ESCALATION` 用房间那套写死
升档关系，于是目录里明明有的 `region_manager` 在那张表里查不到，风险高档时静默
不升档、只留一条 WARNING。T123 把内部表收到目录这一套上（`verdict.ESCALATION`
现在按本目录的名字写），房间里念出来的仍是 `verdict_role_of()` 翻回去的那套 ——
**对外文案一个字没变，变的是「内部按哪套名字对账」**。

所以本模块现在是那条边的唯一翻译处：`canonical_role()` 进（认两种写法），
`verdict_role_of()` 出（翻回房间那套）。两个方向都走目录，没有第三份映射表。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from functools import lru_cache
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


@lru_cache(maxsize=1)
def _load() -> dict[str, Any]:
    """读并校验目录。**进程内缓存一次**，清缓存走 :func:`clear_cache`。

    T117 时这里刻意不缓存，理由是「几十行静态语料，读它的代价可以忽略」。
    T123 把 `verdict._approver` 接到本目录之后那句话不再成立：`decide()` 自称纯函数、
    被每一轮圆桌调用，而它现在每次要 `canonical_role()` + `verdict_role_of()`，
    一次收口就是**三到四遍 `read_text` + `json.loads`**。纯函数每次去碰一次磁盘，
    既不是纯的，也让「圆桌一轮花在哪」变得不好读。

    当年顾虑的耦合（换一份目录得先想起来清缓存）用 :func:`clear_cache` 兑掉 ——
    换目录的测试本来就要显式声明「我换了语料」，那一行比一次静默的读盘更好读。

    返回的 dict 是**共享只读的**：调用方就地改它会污染整个进程的目录。
    本模块的调用方（`all_roles` / `_spec` / `canonical_role` / `role_of`）都只读；
    不深拷贝是因为深拷贝正好把缓存省下的那点开销又花回去。
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


def clear_cache() -> None:
    """丢掉 :func:`_load` 的缓存，下一次查目录重新读盘。

    给两种调用方：换了 `roles.json` 的测试（`monkeypatch` 完 `_ROLES_PATH` 要清一次、
    用完再清一次），以及运行期真去改了目录文件的人。生产路径上没人调它 ——
    目录是随代码发布的语料，不是热更新的配置。
    """
    _load.cache_clear()


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

    目录里没有的角色名一律 :class:`UnknownRole`（经 `_spec`）。**不兜底成 `""`**：
    `""` 在本函数里已经有确切含义（「这个岗不是审批岗」），拿它兼做「没这个岗」
    会让 `verdict` 那边分不清「payment_ops 不该拍板」和「approver_role 写错了」——
    前者要降级到缺省审批岗，后者要原样留着不猜。
    """
    return str(_spec(role).get("verdict_role") or "")


def is_approver_seat(role: str) -> bool:
    """这个岗拍不拍得了板。判据就是目录里 `verdict_role` 非空。

    这是「谁能出现在 `verdict.approver_role` 里」的唯一判据 —— 审批岗的名单归目录，
    不归圆桌那边的一张常量表。目录里没有的角色名照旧抛 :class:`UnknownRole`：
    答不出他是哪个岗，就更答不出他拍不拍得了板，这里不替调用方猜。
    """
    return bool(verdict_role_of(role))


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
