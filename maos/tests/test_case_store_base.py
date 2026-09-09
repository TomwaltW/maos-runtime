"""案件存储骨架（`maos/domain/_case_store.py`）下沉之后的回归守卫。

四个业务域原本各写了一份同构的存储骨架，T80 把它下沉成一份、三个陪跑域接过去。
这类**纯结构性改动**最容易在两个地方悄悄退化，而两处退化都不会让别的测试变红：

1. **报错文案里的表名丢了** —— 下沉之后一个进程里同时活着三个域的骨架，
   报错只说「本域的案件表」，排查时得先猜是哪个域。
2. **域与域之间被焊死** —— 一份共用的正则、一个共用的异常类型，
   会让 A 域的守卫被绕开时能被 B 域的 `except` 接住。

所以本文件把「下沉前是什么样」逐字段写死在测试里（**不 import 旧实现**，
旧实现已经不在了），拿它当基准比对。
"""

from __future__ import annotations

import pytest

from maos.core.store import SqliteStore
from maos.domain import _case_store
from maos.domain.ap import objects as ap_objects
from maos.domain.claim import objects as claim_objects
from maos.domain.investigation import objects as inv_objects

#: 三个接了骨架的域。refund 本轮**不接**（跨轨契约 §4，已记 `docs/BACKLOG.md`
#: 的 `## task-T80`），所以不在这张表里 —— 它还是自己那份副本。
DOMAINS = [
    ("ap", ap_objects, "ap_case"),
    ("claim", claim_objects, "claim_case"),
    ("investigation", inv_objects, "investigation_case"),
]
DOMAIN_IDS = [name for name, _mod, _table in DOMAINS]


def _store(mod):
    st = SqliteStore()
    st.init_schema()
    mod.ensure_schema(st)
    return st


# ------------------------------------------------------------------ 写入方向
@pytest.mark.parametrize("name,mod,case_table", DOMAINS, ids=DOMAIN_IDS)
def test_case_table_write_is_rejected_outside_guard(name, mod, case_table):
    """论证：三个域各自的案件表，四种写语句经 `objects.execute` 一律被拒。

    下沉之后判据从「各域自己那条正则」换成「按 `case_table` 现造的正则」，
    拦截面必须一模一样：insert / update / delete / replace 四种，
    带引号、带方括号、带 `OR REPLACE` 的变体都算。
    """
    st = _store(mod)
    rejected = [
        f"UPDATE {case_table} SET biz_status='x' WHERE 1=0",
        f"INSERT INTO {case_table} (tenant_id) VALUES ('t')",
        f"INSERT OR REPLACE INTO {case_table} (tenant_id) VALUES ('t')",
        f"DELETE FROM {case_table} WHERE 1=0",
        f"REPLACE INTO {case_table} (tenant_id) VALUES ('t')",
        f'UPDATE "{case_table}" SET biz_status=\'x\' WHERE 1=0',
        f"UPDATE [{case_table}] SET biz_status='x' WHERE 1=0",
        f"update {case_table} set biz_status='x' where 1=0",
    ]
    for sql in rejected:
        with pytest.raises(mod.BypassedGuardError):
            mod.execute(st, sql)


@pytest.mark.parametrize("name,mod,case_table", DOMAINS, ids=DOMAIN_IDS)
def test_alter_table_is_still_allowed_after_lowering(name, mod, case_table):
    """论证：拦的是旁路写入，不是正常迁移 —— `ALTER TABLE` 照旧放行。

    这条与 §1 那条是一对：只有拒绝面没有放行面，守卫收紧一格就没人发现，
    症状是「本域从此加不了列」。
    """
    st = _store(mod)
    mod.execute(st, f"ALTER TABLE {case_table} ADD COLUMN t80_probe TEXT")
    assert mod._has_column(st, case_table, "t80_probe") is True
    assert mod._has_column(st, case_table, "t80_no_such_column") is False


@pytest.mark.parametrize("name,mod,case_table", DOMAINS, ids=DOMAIN_IDS)
def test_rejection_message_names_the_concrete_table(name, mod, case_table):
    """论证：报错文案点名到**具体表名**，没退化成「本域的案件表」这种通用话。

    这是下沉最容易偷掉的一处：一份共用的实现里写一句通用文案最省事，
    而代价是出错时看不出是哪张表 —— 三个域的骨架同时活着，排查要绕远路。
    """
    st = _store(mod)
    with pytest.raises(mod.BypassedGuardError) as excinfo:
        mod.execute(st, f"UPDATE {case_table} SET biz_status='x' WHERE 1=0")
    message = str(excinfo.value)
    assert case_table in message, f"{name} 的拒绝文案里没有具体表名：{message}"
    assert "guard.create_case" in message
    assert "guard.update_biz_status" in message
    # 别的域的表名不许串进来 —— 串了说明正则或文案共用了同一份状态。
    for _n, _m, other in DOMAINS:
        if other != case_table:
            assert other not in message


@pytest.mark.parametrize("name,mod,case_table", DOMAINS, ids=DOMAIN_IDS)
def test_other_domains_case_table_is_not_rejected_here(name, mod, case_table):
    """论证：本域的拦截面只覆盖**本域**那张案件表。

    下沉成一份实现之后，最坏的走样是所有域共用一条正则 —— 那样 ap 会拦下
    写 `claim_case` 的语句，看起来「更严格」，实际是把域边界抹了。
    """
    st = _store(mod)
    for _n, _m, other in DOMAINS:
        if other == case_table:
            continue
        # 只校判据，不真去写别的域的表（那张表在本库里不一定建过）。
        assert mod._guarded(f"UPDATE {other} SET biz_status='x'") is not None


# ------------------------------------------------------------------ 读取方向
@pytest.mark.parametrize("name,mod,case_table", DOMAINS, ids=DOMAIN_IDS)
def test_read_is_not_restricted(name, mod, case_table):
    """论证：守的是写入方，不是读取方 —— 读案件表放行。

    读也拦会把「案件表只有 guard 写得动」误伸成「案件表只有 guard 看得见」，
    而 skill 侧大量读它做判定。
    """
    st = _store(mod)
    assert mod.query(st, f"SELECT * FROM {case_table}") == []
    assert mod.query(st, f"SELECT COUNT(*) AS n FROM {case_table}")[0]["n"] == 0
    # 子句里出现 update / delete 这类词也不该误伤读语句。
    assert mod.query(st, f"SELECT * FROM {case_table} WHERE biz_status='deleted'") == []


# ------------------------------------------------------ investigation 的例外
def test_investigation_has_no_business_ref_table():
    """论证：差错处理域**不接**业务引用三件套，且是硬缺席不是静默返回空。

    本域挂的是原始支付快照，`schema.sql` 里根本没有 `investigation_business_ref`。
    骨架里给了这三个方法，但本域不绑 —— 绑上去再造一张空表，调用方会以为那里能挂
    东西，拿到的却永远是空列表，而空列表和「没挂过」分不开。

    另外两个域必须仍然有，否则这条就成了「三个域一起丢了」的假绿。
    """
    for missing in ("attach_business_ref", "list_business_refs", "resolve_business_ref"):
        assert not hasattr(inv_objects, missing), \
            f"investigation 不该有 {missing} —— 本域没有业务引用表"
        with pytest.raises(AttributeError):
            getattr(inv_objects, missing)

    st = _store(inv_objects)
    assert inv_objects.query(
        st, "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name='investigation_business_ref'") == []

    for mod in (ap_objects, claim_objects):
        for present in ("attach_business_ref", "list_business_refs", "resolve_business_ref"):
            assert hasattr(mod, present)

    # 本域自己那套（快照）照旧在。
    assert inv_objects.latest_snapshot_version(
        st, tenant_id="tnt-t80", original_msg_id="msg-t80") == 0


# ------------------------------------------------------------------ 回归守卫
#: 下沉**之前**四份 `objects.py` 里逐字读出来的值，写死在这里当基准。
#: 刻意不 import 任何旧实现 —— 旧实现已经不在了，而拿新实现算一遍再和自己比，
#: 是把回归守卫写成恒真式。
EXPECTED = {
    "ap": {
        "case_table": "ap_case",
        "version_table": "ap_schema_version",
        "business_ref_table": "ap_business_ref",
        "savepoint": "ap_schema_migrate",
        "schema_version": 0,
        "message": "ap_case 的写入必须走 guard.create_case / guard.update_biz_status，"
                   "不许经 objects.execute 旁路（铁律 8）",
    },
    "claim": {
        "case_table": "claim_case",
        "version_table": "claim_schema_version",
        "business_ref_table": "claim_business_ref",
        "savepoint": "claim_schema_migrate",
        "schema_version": 0,
        "message": "claim_case 的写入必须走 guard.create_case / guard.update_biz_status，"
                   "不许经 objects.execute 旁路（铁律 8）",
    },
    "investigation": {
        "case_table": "investigation_case",
        "version_table": "investigation_schema_version",
        "business_ref_table": "investigation_business_ref",
        "savepoint": "investigation_schema_migrate",
        "schema_version": 0,
        "message": "investigation_case 的写入必须走 guard.create_case /"
                   " guard.update_biz_status，不许经 objects.execute 旁路（铁律 8）",
    },
}

#: 下沉前各域模块暴露的名字里，别处真的在用的那些（`guard.py` 用 `_conn` /
#: `lock_of`，测试用 `_has_column` / `_atomic` / `BypassedGuardError`）。
#: 少一个就是调用方当场 AttributeError。
REQUIRED_NAMES = (
    "BypassedGuardError", "_conn", "lock_of", "_guarded", "execute", "query",
    "_atomic", "_has_column", "applied_schema_version", "ensure_schema",
    "_MIGRATE_SAVEPOINT", "_MIGRATIONS", "_SCHEMA_PATH",
)


@pytest.mark.parametrize("name,mod,case_table", DOMAINS, ids=DOMAIN_IDS)
def test_lowering_did_not_change_behavior(name, mod, case_table):
    """论证：同一组入参喂下沉后的骨架，逐字段等于下沉前写死的期望值。

    盯四样：派生出来的三个表名/保存点名、拒绝文案的**全文**、
    模块对外暴露的名字清单、业务引用行的形状。
    """
    want = EXPECTED[name]
    store_obj = mod._CASE_STORE

    # 1) 表名与保存点：下沉后由 case_table 反推，反推错了会静默写错表。
    assert store_obj.case_table == want["case_table"]
    assert store_obj.version_table == want["version_table"]
    assert store_obj.business_ref_table == want["business_ref_table"]
    assert store_obj.savepoint == want["savepoint"]
    assert mod._MIGRATE_SAVEPOINT == want["savepoint"]

    # 2) 拒绝文案全文，一个字都不许变。
    st = _store(mod)
    with pytest.raises(mod.BypassedGuardError) as excinfo:
        mod.execute(st, f"DELETE FROM {case_table} WHERE 1=0")
    assert str(excinfo.value) == want["message"]

    # 3) 模块对外的名字清单。
    for attr in REQUIRED_NAMES:
        assert hasattr(mod, attr), f"{name}.objects 丢了 {attr}"

    # 4) 版本记账：空迁移表 -> 版本 0，建完库记账行也是 0。
    assert mod._MIGRATIONS == ()
    assert mod.applied_schema_version(st) == want["schema_version"]
    assert mod.query(st, f"SELECT * FROM {want['version_table']}") == []

    # 5) 业务引用行的形状（只有接了那三个的域校）。
    if hasattr(mod, "attach_business_ref"):
        row = mod.attach_business_ref(
            st, plan_id="plan-t80", task_id="task-t80", tenant_id="tnt-t80",
            object_type=case_table, object_id="obj-t80", object_version="2",
            purpose="regression",
        )
        assert set(row) == {"plan_id", "task_id", "tenant_id", "object_type",
                            "object_id", "object_version", "purpose", "created_at"}
        assert row["object_version"] == 2, "object_version 要被折成 int"
        assert row["created_at"]
        listed = mod.list_business_refs(st, plan_id="plan-t80")
        assert [r["object_id"] for r in listed] == ["obj-t80"]
        assert mod.list_business_refs(st, plan_id="plan-t80", task_id="no-such") == []
        # 指不到的 object_type 返回 None，不抛。
        assert mod.resolve_business_ref(st, {**row, "object_type": "t80-unknown"}) is None


def test_each_domain_keeps_its_own_bypass_error_type():
    """论证：下沉之后三个域的 `BypassedGuardError` 仍互不相等。

    共用一个异常类型的代价很隐蔽：A 域守卫被绕开时，B 域一句
    `except objects.BypassedGuardError` 会把它接住并当成自己的事处理掉。
    三个子类共一个基类是可以的（要一网打尽时 catch 基类），相等不行。
    """
    types = {name: mod.BypassedGuardError for name, mod, _t in DOMAINS}
    assert len(set(types.values())) == 3, f"异常类型被共用了：{types}"
    for name, err in types.items():
        assert issubclass(err, _case_store.BypassedGuardError)
        assert issubclass(err, RuntimeError)
    for other_name, other_err in types.items():
        for name, err in types.items():
            if name != other_name:
                assert not issubclass(err, other_err)
