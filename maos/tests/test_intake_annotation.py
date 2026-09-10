"""受理期标注的落库与转人工阈值（T103）。

题眼一条：**标注是观察，不是权威事实**（铁律 8）。这个文件里最重要的一条是
`test_a_second_invocation_adds_a_row_instead_of_overwriting` —— 它验的不是「能存」，
而是「复检那一轮不会把上一轮的观察抹掉」。主键里那个 `invocation_id` 就是为这条
存在的：换成 `(tenant_id, case_id, field)` + REPLACE，功能全都在、测试也全绿，
只是「模型上一轮是怎么认的」永远查不回来了 —— 而那正是这张表存在的理由。

题眼二条：**转人工的判据整仓只有一处**。`test_needs_human_*` 那几条钉的是
`needs_human()` 的每一条分支与 0.75 边界。别处再写一份阈值比较的症状不是报错，
是受理岗说要人工、房间事实卡说不用，两边都不报错。
"""

from __future__ import annotations

import pytest

from maos.core.store import SqliteStore
from maos.domain.refund import annotation, objects

TENANT = "acme"
CASE = "RC-1"


def _store() -> SqliteStore:
    store = SqliteStore()
    store.init_schema()
    objects.ensure_schema(store)
    return store


def _record(store, **kw):
    """按一份可用的默认值落一条标注，只覆盖用例关心的那几个字段。"""
    args = {
        "tenant_id": TENANT, "case_id": CASE, "field": "reason_code",
        "raw_text": "收到的时候外包装就是破的", "value": "damaged",
        "confidence": 0.92, "why": "原文提到外包装破损", "source": "model",
        "model": "deepseek-chat", "invocation_id": "iv-1",
    }
    args.update(kw)
    return annotation.record(store, **args)


# ====================================================================
# 1. 建表
# ====================================================================
def test_ensure_schema_creates_the_annotation_table():
    """`ensure_schema()` 之后表在 —— 判据查 `sqlite_master`，不是「INSERT 没炸」。

    只加表不改列，所以这张表走的是 `schema.sql` 第一段的 `IF NOT EXISTS`，
    不经过 `_MIGRATIONS`（改列才要迁移步骤，见 `objects.ensure_schema` 的 docstring）。
    """
    store = _store()
    names = {r["name"] for r in objects.query(
        store, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "intake_annotation" in names


def test_adding_the_table_did_not_touch_the_migration_ledger():
    """加表**不该**产生迁移步骤。

    自作主张补一条迁移，会让 `refund_schema_version` 的版本号与实际形状对不上 ——
    而那种对不上的症状是「迁移悄悄不跑了」，没有任何报错。

    **T116 之前这条断言写的是 `_MIGRATIONS == ()`**，因为当时整个退款域一列都没改过。
    T116 加了六列（`schema_p10_t116.sql`），迁移表于是不再是空的 —— 那是**加列**该有的
    样子，与本条要守的「加表不该有迁移」不是一回事。所以判据从「迁移表是空的」
    收窄到「没有为 `intake_annotation` 而设的步骤」：原意图一个字没变，
    只是不再顺带把「别人加了列」也判成违规。
    """
    store = _store()
    for _version, label, _step in objects._MIGRATIONS:
        assert "intake_annotation" not in label, (
            f"迁移步骤 {label!r} 是为 intake_annotation 设的 —— 只加表不改列，不该有迁移")
    assert objects.applied_schema_version(store) == objects.REFUND_SCHEMA_VERSION


# ====================================================================
# 2. 落库与取回
# ====================================================================
def test_record_then_latest_round_trips_every_field():
    """落一行、取回同一行，字段逐个对得上。

    逐字段比而不是只比 value：`confidence` 那一列过一次 sqlite 的 REAL 往返，
    类型没归一的话这里就会以「0.92 与 '0.92'」露馅。
    """
    store = _store()
    written = _record(store)

    got = annotation.latest(store, tenant_id=TENANT, case_id=CASE, field="reason_code")
    assert got is not None
    for column in ("tenant_id", "case_id", "field", "raw_text", "value",
                   "confidence", "why", "source", "model", "invocation_id",
                   "created_at"):
        assert got[column] == written[column], f"{column} 对不上"
    assert isinstance(got["confidence"], float)


def test_latest_returns_none_when_the_field_was_never_annotated():
    """一条都没有返回 None，不是抛、也不是空 dict —— 调用方要能直接 `if ann is None`。"""
    store = _store()
    _record(store, field="reason_code")
    assert annotation.latest(store, tenant_id=TENANT, case_id=CASE,
                             field="header_map") is None


def test_a_second_invocation_adds_a_row_instead_of_overwriting():
    """🔴 本文件最重要的一条：复检那一轮是**新的一行**，上一轮的观察原样留着。

    把主键里的 `invocation_id` 摘掉，这条就红 —— 而且是这个文件里唯一会红的一条：
    功能测试全都还绿，只是审计链断了。
    """
    store = _store()
    first = _record(store, value="damaged", confidence=0.62, invocation_id="iv-1")
    second = _record(store, value="wrong_item", confidence=0.88, invocation_id="iv-2")
    assert first["created_at"] < second["created_at"], (
        "前提：两次观察发生在不同时刻（`latest` 按 created_at 倒序取）")

    rows = annotation.list_for_case(store, tenant_id=TENANT, case_id=CASE)
    assert len(rows) == 2, "第二次观察盖掉了第一次 —— 上一轮怎么认的就查不回来了"
    assert [r["invocation_id"] for r in rows] == ["iv-1", "iv-2"], "list_for_case 要按时间升序"

    got = annotation.latest(store, tenant_id=TENANT, case_id=CASE, field="reason_code")
    assert got["invocation_id"] == "iv-2"
    assert got["value"] == "wrong_item"


def test_replaying_the_same_invocation_stays_one_row():
    """同一次 invocation 重跑仍是一行 —— skill 会被返工重跑，重跑必须幂等。

    幂等的判据是「仍是一行」而不是「不抛」：裸 INSERT 会在这里撞 IntegrityError，
    而那是把正常的重放当成了冲突。
    """
    store = _store()
    _record(store, value="damaged", confidence=0.62, invocation_id="iv-1")
    _record(store, value="damaged", confidence=0.62, invocation_id="iv-1")

    rows = annotation.list_for_case(store, tenant_id=TENANT, case_id=CASE)
    assert len(rows) == 1


def test_list_for_case_covers_every_field_of_the_case():
    """本案全部标注 —— 诉求类型与表头映射是两个 `field`，都在这张表里。"""
    store = _store()
    _record(store, field="reason_code", value="damaged", invocation_id="iv-1")
    _record(store, field="header_map", value='{"退款金额": "amount_claimed"}',
            invocation_id="iv-2")

    rows = annotation.list_for_case(store, tenant_id=TENANT, case_id=CASE)
    assert {r["field"] for r in rows} == {"reason_code", "header_map"}


# ====================================================================
# 3. actor 锚点
# ====================================================================
def test_empty_invocation_id_is_refused():
    """空的 `invocation_id` 当场抛 —— 没有 actor 锚点的写入让审计链变假。

    与 `guard.py::_require_invocation_id` 同一个口径：一行标注查不出是哪次调用
    产生的，「模型当时是怎么认的」就无从回答；它同时还是主键的一部分。
    """
    store = _store()
    with pytest.raises(ValueError, match="invocation_id"):
        _record(store, invocation_id="")

    assert annotation.list_for_case(store, tenant_id=TENANT, case_id=CASE) == []


# ====================================================================
# 4. 跨租户隔离
# ====================================================================
def test_tenants_cannot_read_each_others_annotations():
    """隔离靠 `tenant_id` 在**主键里打头**，不靠 WHERE 约定（本域各表一致）。"""
    store = _store()
    _record(store, tenant_id="acme", value="damaged", invocation_id="iv-a")
    _record(store, tenant_id="globex", value="wrong_item", invocation_id="iv-b")

    acme = annotation.list_for_case(store, tenant_id="acme", case_id=CASE)
    globex = annotation.list_for_case(store, tenant_id="globex", case_id=CASE)
    assert [r["value"] for r in acme] == ["damaged"]
    assert [r["value"] for r in globex] == ["wrong_item"]

    got = annotation.latest(store, tenant_id="acme", case_id=CASE, field="reason_code")
    assert got["value"] == "damaged", "租户 A 读到了租户 B 的标注"


# ====================================================================
# 5. 转人工判据 —— 整仓唯一定义
# ====================================================================
def test_needs_human_never_escalates_a_deterministic_hit():
    """词表 / 别名命中恒不转：确定性匹配没有把握度可言。

    连 `confidence=0.0` 都不转 —— 这两种来源根本不填把握度，
    真填了 0 也不该因此惊动人工。
    """
    assert annotation.needs_human(1.0, "lexicon") is False
    assert annotation.needs_human(1.0, "alias") is False
    assert annotation.needs_human(0.0, "lexicon") is False
    assert annotation.needs_human(0.0, "alias", value="damaged") is False


def test_needs_human_always_escalates_a_fallback():
    """没模型又没命中，这是猜的 —— 把握度写多高都必转。"""
    assert annotation.needs_human(0.99, "fallback") is True
    assert annotation.needs_human(1.0, "fallback", value="damaged") is True


def test_needs_human_always_escalates_an_undecided_value():
    """模型自己说了判不出来（空串 / unknown），把握度再高也必转。"""
    assert annotation.needs_human(0.99, "model", value="") is True
    assert annotation.needs_human(0.99, "model", value="unknown") is True


def test_needs_human_uses_the_threshold_for_model_judgements():
    """🔴 0.75 那条边界：**等于阈值不转，低一点就转**。

    边界写反（`<=`）的症状是恰好落在阈值上的单子全进人工队列，
    而没有任何一条断言会红 —— 除了这一条。
    """
    assert annotation.needs_human(0.75, "model", value="damaged") is False
    assert annotation.needs_human(0.7499, "model", value="damaged") is True
    assert annotation.needs_human(1.0, "model", value="damaged") is False
    assert annotation.needs_human(0.0, "model", value="damaged") is True
    assert annotation.HUMAN_REVIEW_THRESHOLD == 0.75


def test_needs_human_escalates_an_unknown_source():
    """不认识的来源按最保守处理。

    新增一种来源时忘了在 `needs_human()` 里登记，代价应该是「多转几单人工」，
    而不是「静默放行」—— 后者是没人会发现的那一种。
    """
    assert annotation.needs_human(1.0, "vibes", value="damaged") is True
    assert annotation.needs_human(1.0, "", value="damaged") is True


def test_the_threshold_is_defined_exactly_once_in_the_repo():
    """整仓只有 `annotation.py` 写得出这个阈值。

    判据扫源码里的字面量 `0.75`：别的模块自己写一份 `confidence < 0.75` 的比较，
    调阈值时就会漏掉它，而两份阈值不一致的症状是两边都不报错。
    """
    import re
    from pathlib import Path

    pkg = Path(annotation.__file__).resolve().parents[2]
    hits = []
    for path in pkg.rglob("*.py"):
        if path.name == "annotation.py" or "tests" in path.parts:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"(?<![\w.])0\.75(?![\w])", line) and "confidence" in line:
                hits.append(f"{path.relative_to(pkg)}:{lineno}")
    assert not hits, (
        f"这些地方自己写了一份转人工阈值，应改调 annotation.needs_human()：{hits}")
