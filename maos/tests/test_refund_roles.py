"""T117 · 退款域角色目录 —— 「谁能批」与「他是哪个岗」是两件事。

本文件的第一断言是整张目录存在的理由：

    `scenarios/refund/roles.json` 里写了某个账号，**不能**让他批得动。
    权威名单永远是运行时的 `MAOS_APPROVERS`（铁律 6）。

这条不是洁癖。目录是仓库里的一份 JSON，谁都能提一个 PR 往里加一行；
而审批人名单动一下属于安全事件，`deploy/nacos.md` §6.5 专门给它设了写入侧闸门。
两者一旦合流，权限提升就退化成一次普通的代码改动 —— 而且没有症状。

第二类断言守的是**两套角色名对得上**：`maos/roundtable/verdict.py` 的升档表用
supervisor / finance_manager，本目录用把职责写全的那套。对不上的症状是房间里那句
「请 X 拍板」点不到任何人（`verdict._approver` 认不出就不升档，只 log 一行）。
"""

from __future__ import annotations

import json

import pytest

from maos.domain.refund import roles

#: 名单内、且在目录里的账号。
IN_LIST_SUPERVISOR = "@boss:maos.local"
IN_LIST_PAYOPS = "@payops:maos.local"
#: 在目录里、但**不在**名单内 —— 第一断言用的就是他。
DIRECTORY_ONLY = "@region-east:maos.local"
#: 名单内、但目录里没有（新人进了名单，目录还没补）。
LIST_ONLY = "@newcomer:maos.local"

APPROVERS = frozenset({IN_LIST_SUPERVISOR, IN_LIST_PAYOPS, LIST_ONLY})


# ======================================================================
# 1. 第一断言：目录给不了权限
# ======================================================================
def test_directory_alone_cannot_grant_approval():
    """🔴 目录里写着他是区域经理，但他不在 `MAOS_APPROVERS` 里 —— 一律批不动。"""
    assert DIRECTORY_ONLY in roles.accounts_of(roles.ROLE_REGION_MANAGER), (
        "前提没成立：这条用例要的是一个「目录里有、名单里没有」的账号")
    assert roles.can_approve(DIRECTORY_ONLY, roles.ROLE_REGION_MANAGER,
                             approvers=APPROVERS) is False
    # 连「不按岗收窄」那一档也不放行 —— 名单在先，这一步与角色无关。
    assert roles.can_approve(DIRECTORY_ONLY, "", approvers=APPROVERS) is False


def test_empty_approver_list_denies_everyone():
    """名单没配 = 谁都不许。配置缺失时放行是最经典的权限漏洞形态。"""
    for account in (IN_LIST_SUPERVISOR, IN_LIST_PAYOPS, DIRECTORY_ONLY):
        assert roles.can_approve(account, "", approvers=frozenset()) is False


def test_in_list_but_wrong_role_is_refused():
    """名单内，但目录说他不在这个岗上 —— 按岗收窄的动作不该落到他头上。"""
    assert roles.can_approve(IN_LIST_SUPERVISOR, roles.ROLE_PAYMENT_OPS,
                             approvers=APPROVERS) is False
    assert roles.can_approve(IN_LIST_PAYOPS, roles.ROLE_PAYMENT_OPS,
                             approvers=APPROVERS) is True


def test_in_list_but_not_in_directory_passes_only_the_unnarrowed_check():
    """名单里有、目录里没有：不按岗收窄时放行，按岗收窄时拒绝。

    这一档必须能过 `role=""`：目录晚补一天是常态，把新人挡在所有动作外面
    是拿一份**辅助数据**去当权限闸用。
    """
    assert roles.can_approve(LIST_ONLY, "", approvers=APPROVERS) is True
    assert roles.can_approve(LIST_ONLY, roles.ROLE_PAYMENT_OPS,
                             approvers=APPROVERS) is False


def test_unknown_role_is_fail_closed():
    """拼错的角色名一律拒 —— 放行等于让一个 typo 把「按岗收窄」悄悄关掉。"""
    assert roles.can_approve(IN_LIST_PAYOPS, "paymnet_ops", approvers=APPROVERS) is False


# ======================================================================
# 2. 目录本身的形状
# ======================================================================
def test_directory_declares_the_four_roles():
    assert roles.all_roles() == (
        "after_sales_supervisor", "finance_reviewer", "payment_ops", "region_manager")


def test_every_role_has_title_duty_and_accounts():
    """每个岗都要能回答「你叫什么、你干什么、有谁」—— 少一样，工单派过去就是一句空话。"""
    for role in roles.all_roles():
        assert roles.title_of(role), role
        assert roles.duty_of(role), role
        assert roles.accounts_of(role), f"{role} 一个账号都没有，派过去没人接"


def test_accounts_keep_declaration_order():
    """顺序有意义：首位是这个岗的主责人，`/assign` 缺省派给他。

    改成按字符串排序会让「谁是主责」由字典序决定 —— 那是个没人看得懂的规则，
    而且换个账号名就悄悄换了主责人。
    """
    raw = json.loads(roles._ROLES_PATH.read_text(encoding="utf-8"))
    for role, spec in raw["roles"].items():
        assert roles.accounts_of(role) == tuple(spec["accounts"])


def test_role_of_finds_the_account():
    assert roles.role_of(IN_LIST_SUPERVISOR) == roles.ROLE_AFTER_SALES_SUPERVISOR
    assert roles.role_of(IN_LIST_PAYOPS) == roles.ROLE_PAYMENT_OPS


def test_role_of_unknown_account_returns_empty_not_raise():
    """查不到是常态不是错误 —— 抛异常会让一条正常动作在「他是哪个岗」这步炸掉。"""
    assert roles.role_of(LIST_ONLY) == ""
    assert roles.role_of("") == ""


def test_accounts_of_unknown_role_raises():
    """认不出的岗要抛，**不能兜底成空列表**：那样 `/assign` 会把工单派给谁也不是。"""
    with pytest.raises(roles.UnknownRole):
        roles.accounts_of("no_such_role")


def test_default_ticket_role_is_a_real_role():
    """缺省承接岗必须真的在目录里 —— 指向一个不存在的岗，开单当场就没人接。"""
    assert roles.DEFAULT_TICKET_ROLE in roles.all_roles()
    assert roles.DEFAULT_TICKET_ROLE == roles.ROLE_PAYMENT_OPS


# ======================================================================
# 3. 与 `maos/roundtable/verdict.py` 那套角色名对得上
# ======================================================================
def test_canonical_role_accepts_the_verdict_spelling():
    """圆桌那套写法喂进来要认得出，否则两套名字就是两个互不相通的世界。"""
    assert roles.canonical_role("supervisor") == roles.ROLE_AFTER_SALES_SUPERVISOR
    assert roles.canonical_role("finance_manager") == roles.ROLE_FINANCE_REVIEWER
    assert roles.canonical_role("region_manager") == roles.ROLE_REGION_MANAGER
    # 本目录自己的写法原样返回。
    assert roles.canonical_role(roles.ROLE_PAYMENT_OPS) == roles.ROLE_PAYMENT_OPS


def test_canonical_role_does_not_raise_on_unknown():
    """归一化不是校验：认不出原样返回，判权限的那一步再去拒。"""
    assert roles.canonical_role("whatever") == "whatever"
    assert roles.canonical_role("") == ""


def test_escalation_table_roles_are_all_in_the_directory():
    """🔴 `verdict.ESCALATION` 里出现的每个角色名，目录都要认得。

    认不出的后果不是报错，是 `verdict._approver` 只 log 一行「不在升档表里」然后
    不升档 —— 风险高档的案子于是停在原审批人手上，而屏幕上一切正常。
    """
    from maos.roundtable.verdict import ESCALATION

    for name in set(ESCALATION) | set(ESCALATION.values()):
        assert roles.canonical_role(name) in roles.all_roles(), (
            f"verdict.py 的角色 {name!r} 在角色目录里找不到对应的岗")


def test_verdict_role_round_trips():
    """目录 -> 圆桌写法 -> 目录，三个审批岗都要转得回来。"""
    for role in (roles.ROLE_AFTER_SALES_SUPERVISOR, roles.ROLE_FINANCE_REVIEWER,
                 roles.ROLE_REGION_MANAGER):
        spoken = roles.verdict_role_of(role)
        assert spoken, f"{role} 没有对应的圆桌角色名"
        assert roles.canonical_role(spoken) == role


def test_payment_ops_is_not_an_approver_seat():
    """支付运维是**接单岗**，圆桌升档表里不该有它。

    升到一个不能拍板的岗上，房间里那句「请 X 拍板」就点不到能拍板的人。
    """
    assert roles.verdict_role_of(roles.ROLE_PAYMENT_OPS) == ""
