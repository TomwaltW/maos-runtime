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
    assert roles.is_approver_seat(roles.ROLE_PAYMENT_OPS) is False


def test_is_approver_seat_answers_for_the_three_approval_roles():
    """三个审批岗都拍得了板；目录里没有的角色名照旧抛，不替调用方猜。"""
    for role in (roles.ROLE_AFTER_SALES_SUPERVISOR, roles.ROLE_FINANCE_REVIEWER,
                 roles.ROLE_REGION_MANAGER):
        assert roles.is_approver_seat(role) is True
    with pytest.raises(roles.UnknownRole):
        roles.is_approver_seat("cfo")


# ======================================================================
# 5. 目录缓存（T123）
# ======================================================================
def test_directory_is_read_from_disk_only_once(monkeypatch):
    """🔴 目录进程内只读一次盘。

    `verdict.decide()` 自称纯函数、每轮圆桌调一次，而它现在每次要归一 + 翻译 ——
    不缓存就是一次收口三到四遍 `read_text` + `json.loads`。
    """
    reads: list[int] = []

    class CountingPath:
        """数 `read_text` 的次数。`Path` 有 `__slots__`，打不了 monkeypatch，
        所以整个换掉而不是补一个方法。"""

        def __init__(self, real):
            self._real = real
            self.name = real.name

        def read_text(self, *a, **kw):
            reads.append(1)
            return self._real.read_text(*a, **kw)

    monkeypatch.setattr(roles, "_ROLES_PATH", CountingPath(roles._ROLES_PATH))
    roles.clear_cache()
    try:
        for _ in range(5):
            roles.canonical_role("supervisor")
            roles.verdict_role_of(roles.ROLE_FINANCE_REVIEWER)
            roles.role_of(IN_LIST_SUPERVISOR)
    finally:
        monkeypatch.undo()
        roles.clear_cache()

    assert len(reads) == 1, f"目录被读了 {len(reads)} 遍"


def test_clear_cache_picks_up_a_changed_directory_file(tmp_path, monkeypatch):
    """🔴 换一份目录 + `clear_cache()` 之后，查到的是新的那份。

    这条是缓存的**代价**：不清就读不到改动。写成一条用例而不是留在注释里，
    是因为「改了目录文件却没生效」的症状是屏幕上一切正常 —— 与 T117 当初
    不缓存要躲的那种静默失败是同一种。
    """
    swapped = tmp_path / "roles.json"
    swapped.write_text(json.dumps({"version": 1, "roles": {
        "night_shift_lead": {"title": "夜班主管", "duty": "夜间退款的拍板人",
                             "verdict_role": "night_lead", "accounts": ["@night:maos.local"]},
    }}, ensure_ascii=False), encoding="utf-8")

    roles.clear_cache()
    try:
        monkeypatch.setattr(roles, "_ROLES_PATH", swapped)
        roles.clear_cache()
        assert roles.all_roles() == ("night_shift_lead",)
        assert roles.canonical_role("night_lead") == "night_shift_lead"
        assert roles.role_of("@night:maos.local") == "night_shift_lead"
    finally:
        # 换回真目录**并再清一次** —— 漏了这一步，后面每个用例读到的都是夜班主管。
        monkeypatch.undo()
        roles.clear_cache()

    assert roles.all_roles() == (
        roles.ROLE_AFTER_SALES_SUPERVISOR, roles.ROLE_FINANCE_REVIEWER,
        roles.ROLE_PAYMENT_OPS, roles.ROLE_REGION_MANAGER)


# ======================================================================
# 6. 缺省岗的两套写法等价（T130）
# ======================================================================
#
# T123 把升档表收到目录这一套名字上之后，「缺省审批岗」仍在两套写法里各有一份：
# `roles.DEFAULT_APPROVER_SEAT`（目录那套）与 `kb.plan_advice.DEFAULT_APPROVER_ROLE`
# （`verdict_role` 别名那套）。两者今天结果一致，**但从前没有任何机器判据** ——
# 改了其中一处，两套写法静默分叉，症状是房间里念出来的审批人和 Planner 建议的
# 审批人不是同一个人，而屏幕上一切正常。
#
# 判据住在**退款域侧**（本文件）而不是 `kb` 侧，方向才对：域 import 内核是允许的，
# 反过来会把退款域耦进领域无关的检索内核（铁律 9、跨轨契约 §E）。
# 合并成一个字面值**不是**出路：那会改掉 `policy_directives()` 的返回值，
# 而 R4 那一组的 `_expected` 正按它比对（见 `plan_advice` 模块抬头）。


def test_the_two_spellings_of_the_default_approver_seat_agree():
    """🔴 缺省审批岗的两套写法指同一个岗，两个方向都要转得过去。

    三处动任一个，这条当场红 —— 那不是误报，是在问「另一半你改了吗」：

      · `kb.plan_advice.DEFAULT_APPROVER_ROLE`（别名那套的字面量）
      · `roles.DEFAULT_APPROVER_SEAT`（目录那套的常量）
      · `scenarios/refund/roles.json` 里那条 `verdict_role` 映射
    """
    # 退款域的测试 import 内核是允许的方向（域 -> 内核）。反过来不行。
    from maos.kb import plan_advice

    assert roles.canonical_role(plan_advice.DEFAULT_APPROVER_ROLE) == \
        roles.DEFAULT_APPROVER_SEAT
    # 反向：目录那套翻回房间那套，要落回 kb 写的那个字面量。只钉单向的话，
    # 有人改 `verdict_role_of()` 的出口，房间里念的字就变了而这条仍绿。
    assert roles.verdict_role_of(roles.DEFAULT_APPROVER_SEAT) == \
        plan_advice.DEFAULT_APPROVER_ROLE
    # 缺省岗必须是目录里真有的岗，且必须拍得了板 —— 降级到一个批不动的人身上，
    # 房间里那句「请 X 拍板」就点不到能拍板的人。
    assert roles.DEFAULT_APPROVER_SEAT in roles.all_roles()
    assert roles.is_approver_seat(roles.DEFAULT_APPROVER_SEAT) is True


def test_the_two_spellings_of_the_default_ticket_role_agree():
    """同一条病的另一半：工单缺省承接岗也在两处各写了一份。

    `plan_advice._FALLBACK_TICKET_ROLE` 的注释写着「与 `roles.DEFAULT_TICKET_ROLE`
    同值」，靠人记着。它是目录读不出来时的兜底，所以两处**同值**（不像审批岗那样
    分属两套写法），分叉的症状更隐蔽：目录在时一切正常，只有目录读不出来那一刻
    才会派给另一个岗，而那正是没人盯着的时刻。
    """
    from maos.kb import plan_advice

    assert plan_advice._FALLBACK_TICKET_ROLE == roles.DEFAULT_TICKET_ROLE


def test_canonical_role_folds_case_and_whitespace():
    """🔴 外部来的角色名大小写不一致，也要归到同一个岗。

    今天撞不上：语料全小写。接真 Matrix 房间的显示名（`Supervisor`）就会撞 ——
    折之前 `_approver` 把它当未知角色，风险高档时静默不升档，只留一条 WARNING。
    """
    for raw in ("Supervisor", "SUPERVISOR", " supervisor ", "  SuPerVisor\t"):
        assert roles.canonical_role(raw) == roles.ROLE_AFTER_SALES_SUPERVISOR, raw
    # 目录自己那套写法同样折。
    assert roles.canonical_role("After_Sales_Supervisor") == roles.ROLE_AFTER_SALES_SUPERVISOR
    assert roles.canonical_role("REGION_MANAGER") == roles.ROLE_REGION_MANAGER


def test_canonical_role_returns_the_cleaned_form_for_unknown_names():
    """认不出的仍**原样返回、不抛**，但返回的是清洗过的那份。

    `strip()` 从一开始就是这样，`lower()` 跟上同一套口径 —— 归一化函数不该对
    「认得出」和「认不出」给两种返回法，否则 `"CFO"` 与 `"cfo"` 会给出两个 key，
    下游拿它当 dict 键就静默分叉成两条记录。

    原文不会丢，只是不由本函数保管：`verdict._approver` 出口的 `_spoken(canon, role)`
    用原始 `role` 兜底，`_spec()` 的报错信息也打原始 `role`。
    """
    assert roles.canonical_role("CFO") == "cfo"
    assert roles.canonical_role("  Whatever  ") == "whatever"
    # 「不抛」这条语义不变：归一化不是校验，判权限的那一步再去拒。
    assert roles.canonical_role("随便什么") == "随便什么"
    assert roles.canonical_role("") == ""
    assert roles.canonical_role("   ") == ""


def test_can_approve_accepts_an_externally_cased_role_name():
    """折大小写买到的东西：名单内的主管，用房间显示名那种写法也判得对。

    折之前这条是 `False` —— `accounts_of("Supervisor")` 抛 `UnknownRole`，
    `can_approve` fail-closed 拒掉一个本该放行的人，且没有症状。
    """
    assert roles.can_approve(IN_LIST_SUPERVISOR, "Supervisor",
                             approvers=APPROVERS) is True
    # fail-closed 那条不受影响：拼错的角色名照旧拒。
    assert roles.can_approve(IN_LIST_PAYOPS, "Paymnet_Ops", approvers=APPROVERS) is False
