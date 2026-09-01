"""采购退货域的建表、读写口径与靶场数据。

本文件买的是三句话：

  1. **五张源单据表复用，一张都没重建** —— 这是本域「域可移植性」主张的落点，
     也是最容易在几个月后被人「顺手补一份定义」破坏的地方，所以用文本断言 +
     列集合比对两条独立判据钉死（`CREATE TABLE IF NOT EXISTS` 撞名不报错，
     是**静默跳过**，人眼复查根本看不出来）。
  2. 建表幂等、迁移记账可连跑。
  3. 靶场数据里**没有**贷项通知单与到账回执 —— 那两张表是外部权威事实。

守卫那一侧（谁写得进权威终态）在 `test_rtv_guard.py`。
"""

from __future__ import annotations

import ast
import pathlib
import re
from decimal import Decimal

import pytest

from maos.core.store import SqliteStore
# 上游那五张表归 ap 域持有，本测试要拿它建出来的形状当**基准**去比对。
# 这是测试层的引用，不是域层的 import —— `maos/domain/rtv/**` 一行都不 import ap，
# 由 `test_rtv_guard.py::test_rtv_domain_does_not_import_another_domain` 钉住。
from maos.domain.ap import objects as ap_objects
from maos.domain.rtv import fixtures, guard, objects

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
MAOS_PKG = REPO_ROOT / "maos"
RTV_SCHEMA = MAOS_PKG / "domain" / "rtv" / "schema.sql"

TEN, CASE, PLAN = fixtures.DEMO_TENANT, "rtv-case-1", "plan-rtv-1"


@pytest.fixture()
def store():
    """一个把**两个域**都建好的库：上游五张表归 ap，本域十张表归 rtv。

    顺序刻意是「先 ap 后 rtv」—— 生产里也是这样：退货是采购的下游，
    源单据先存在。反过来跑同样应该没事（两边表名不重），
    `test_schema_order_does_not_matter` 把这一条钉住。
    """
    s = SqliteStore()
    s.init_schema()
    ap_objects.ensure_schema(s)
    objects.ensure_schema(s)
    return s


def _columns(store, table: str) -> list[str]:
    return [r["name"] for r in objects.query(store, f"PRAGMA table_info({table})")]


# ------------------------------------------------------ ① 复用面：一张都不许重建
def test_schema_does_not_recreate_the_upstream_tables():
    """文本断言：`rtv/schema.sql` 里不出现那五张表的建表语句。

    判据落在**建表语句**上而不是「提到了表名」：schema.sql 抬头那段注释就要点名
    这五张表（说明为什么不建它们），把注释也一并禁掉会逼着后来的人把理由删掉。
    """
    sql = RTV_SCHEMA.read_text(encoding="utf-8")
    created = set(re.findall(r"CREATE TABLE IF NOT EXISTS\s+(\w+)", sql, re.IGNORECASE))
    overlap = created & set(objects.UPSTREAM_TABLES)
    assert not overlap, (
        f"rtv/schema.sql 重建了上游表 {sorted(overlap)}；`CREATE TABLE IF NOT EXISTS` "
        f"撞名的后果不是报错而是静默跳过，两份定义一旦漂开症状会离原因非常远")


def test_upstream_tables_keep_the_shape_the_owner_built():
    """列集合比对：本域 `ensure_schema()` 跑完，五张上游表的列与 ap 建出来的完全一致。

    上一条挡的是「明着重建」，这一条挡的是「换个写法悄悄改形状」（比如某天有人
    在本域加一句 ALTER）。基准取自一个**只跑 ap** 的干净库。
    """
    baseline = SqliteStore()
    baseline.init_schema()
    ap_objects.ensure_schema(baseline)

    mixed = SqliteStore()
    mixed.init_schema()
    ap_objects.ensure_schema(mixed)
    objects.ensure_schema(mixed)

    for table in objects.UPSTREAM_TABLES:
        expected = [r["name"] for r in ap_objects.query(baseline, f"PRAGMA table_info({table})")]
        assert expected, f"基准库里没有 {table}，测试自身失效"
        assert _columns(mixed, table) == expected, (
            f"{table} 的列集合被本域改了；这五张表归应付账款域持有，本域只引用")


def test_rtv_alone_does_not_create_the_upstream_tables():
    """只跑本域的 `ensure_schema()`，那五张表**一张都不该出现**。

    这是上一条的反面：形状一致有两种可能 —— 「没重建」与「重建了一份一模一样的」。
    只有这一条能把后者排除掉。
    """
    s = SqliteStore()
    s.init_schema()
    objects.ensure_schema(s)
    assert sorted(objects.missing_upstream_tables(s)) == sorted(objects.UPSTREAM_TABLES)


def test_reading_source_documents_without_the_owner_says_where_to_go():
    """缺上游表时抛 `UpstreamSchemaMissing`，而不是让 sqlite 报 no such table。

    后者那句话指不出去处，而正确处置是「让持有方建表」，不是「本域补一份定义」。
    """
    s = SqliteStore()
    s.init_schema()
    objects.ensure_schema(s)
    with pytest.raises(objects.UpstreamSchemaMissing, match="只引用不重建"):
        objects.gr_lines(s, TEN, fixtures.DEMO_GR)


# ------------------------------------------------------------------ ② 建表与迁移
def test_schema_creates_exactly_the_ten_domain_tables(store):
    sql = RTV_SCHEMA.read_text(encoding="utf-8")
    created = re.findall(r"CREATE TABLE IF NOT EXISTS\s+(\w+)", sql, re.IGNORECASE)
    assert len(created) == 10, f"C-R1 冻结的是 10 张表，实际 {len(created)}：{created}"
    rows = objects.query(store, "SELECT name FROM sqlite_master WHERE type='table'")
    present = {r["name"] for r in rows}
    assert set(created) <= present


def test_ensure_schema_is_idempotent(store):
    """连跑两次不报错，版本号也不涨第二次。"""
    before = objects.applied_schema_version(store)
    objects.ensure_schema(store)
    objects.ensure_schema(store)
    assert objects.applied_schema_version(store) == before == objects.RTV_SCHEMA_VERSION
    rows = objects.query(store, "SELECT COUNT(*) AS n FROM rtv_schema_version")
    assert rows[0]["n"] == len(objects._MIGRATIONS)


def test_schema_order_does_not_matter():
    """先 rtv 后 ap 同样跑得通 —— 两个域表名一个都不重。"""
    s = SqliteStore()
    s.init_schema()
    objects.ensure_schema(s)
    ap_objects.ensure_schema(s)
    assert objects.missing_upstream_tables(s) == []


def test_rtv_schema_only_adds_tables():
    """铁律 1：本域只**新增**表，不碰既有表结构。

    判据落在建表脚本上：整份只有 `CREATE TABLE IF NOT EXISTS` 与
    `CREATE INDEX IF NOT EXISTS`，没有任何 ALTER / DROP 既有表的语句。
    """
    sql = RTV_SCHEMA.read_text(encoding="utf-8")
    statements = [s.strip() for s in sql.split(";") if s.strip()]
    statements = [s for s in statements
                  if any(line.strip() and not line.strip().startswith("--")
                         for line in s.splitlines())]
    for stmt in statements:
        head = " ".join(line for line in stmt.splitlines()
                        if line.strip() and not line.strip().startswith("--"))
        assert re.match(r"\s*CREATE (TABLE|INDEX) IF NOT EXISTS", head, re.IGNORECASE), (
            f"建表脚本里出现了非 CREATE ... IF NOT EXISTS 的语句：{head[:80]}")


def test_no_domain_shares_a_table_name_with_rtv():
    """本域与另外四个域不许有同名表 —— 同名的后果是**静默**跳过。"""
    def tables(path: pathlib.Path) -> set[str]:
        return set(re.findall(r"CREATE TABLE IF NOT EXISTS\s+(\w+)",
                              path.read_text(encoding="utf-8"), re.IGNORECASE))

    rtv_tables = tables(RTV_SCHEMA)
    assert rtv_tables, "本域一张表都没建"
    for other in sorted((MAOS_PKG / "domain").glob("*/schema.sql")):
        if other == RTV_SCHEMA:
            continue
        overlap = rtv_tables & tables(other)
        assert not overlap, f"与 {other.parent.name} 域建了同名表：{sorted(overlap)}"


# ------------------------------------------------------------------ ③ 金额口径
def test_money_never_goes_through_float():
    assert objects.money(0.1) == Decimal("0.1")
    assert objects.money_str("1000") == "1000.00"
    assert objects.money_str(Decimal("120.005")) == "120.00"   # ROUND_HALF_EVEN
    with pytest.raises(ValueError, match="金额解析不出数值"):
        objects.money("八百")


def test_money_does_not_fall_back_to_zero():
    """解析不出来就抛，不兜底成 0 —— 0 在对账里往往刚好对得上。"""
    with pytest.raises(ValueError):
        objects.money(None)


# ------------------------------------------------------------------ ④ 退货行
def test_return_lines_and_claimed_amount(store):
    fixtures.seed_supplier(store, tenant_id=TEN, supplier_id=fixtures.DEMO_SUPPLIER,
                           name="示例供应商")
    summary = fixtures.seed_source_documents(
        store, tenant_id=TEN, supplier_id=fixtures.DEMO_SUPPLIER,
        po_id=fixtures.DEMO_PO, gr_id=fixtures.DEMO_GR,
        lines=fixtures.DEMO_LINES, ordered_at="2026-09-01T00:00:00+00:00")

    # 只有验收不合格的那一行进得了退货明细。
    assert [line["gr_line_no"] for line in summary["lines"]] == [2]
    assert summary["amount_claimed"] == "360.00"          # 3 件 × 120.00

    guard.create_case(store, **fixtures.intake_kwargs(
        tenant_id=TEN, case_id=CASE, supplier_id=fixtures.DEMO_SUPPLIER,
        po_id=fixtures.DEMO_PO, gr_id=fixtures.DEMO_GR,
        amount_claimed=summary["amount_claimed"], plan_id=PLAN))
    fixtures.seed_return_lines(store, tenant_id=TEN, case_id=CASE,
                               rejected=summary["lines"], reason_code="RR-DAMAGED")

    lines = objects.rtv_lines(store, TEN, CASE)
    assert len(lines) == 1
    assert lines[0]["gr_line_no"] == 2 and lines[0]["quantity_returned"] == 3
    assert objects.claimed_amount(lines) == Decimal("360.00")


def test_goods_receipt_line_is_never_rewritten_by_a_return(store):
    """退货不回头改收货单：`quantity_received` 是审计量、永不变（PeopleSoft 口径）。"""
    fixtures.seed_supplier(store, tenant_id=TEN, supplier_id=fixtures.DEMO_SUPPLIER,
                           name="示例供应商")
    summary = fixtures.seed_source_documents(
        store, tenant_id=TEN, supplier_id=fixtures.DEMO_SUPPLIER,
        po_id=fixtures.DEMO_PO, gr_id=fixtures.DEMO_GR,
        lines=fixtures.DEMO_LINES, ordered_at="2026-09-01T00:00:00+00:00")
    before = objects.gr_lines(store, TEN, fixtures.DEMO_GR)

    guard.create_case(store, **fixtures.intake_kwargs(
        tenant_id=TEN, case_id=CASE, supplier_id=fixtures.DEMO_SUPPLIER,
        po_id=fixtures.DEMO_PO, gr_id=fixtures.DEMO_GR,
        amount_claimed=summary["amount_claimed"], plan_id=PLAN))
    fixtures.seed_return_lines(store, tenant_id=TEN, case_id=CASE,
                               rejected=summary["lines"], reason_code="RR-DAMAGED")

    assert objects.gr_lines(store, TEN, fixtures.DEMO_GR) == before
    assert objects.rejected_quantity(before[1]) == 3


# ------------------------------------------------------------------ ⑤ 靶场数据
def test_fixtures_seed_no_authoritative_facts(store):
    """🔴 靶场里**没有**贷项通知单，也没有到账回执。

    预置一行等于「演示开始前供应商就已经认账了」，把本域要证明的那件事架空。
    """
    fixtures.seed_supplier(store, tenant_id=TEN, supplier_id=fixtures.DEMO_SUPPLIER,
                           name="示例供应商")
    fixtures.seed_source_documents(
        store, tenant_id=TEN, supplier_id=fixtures.DEMO_SUPPLIER,
        po_id=fixtures.DEMO_PO, gr_id=fixtures.DEMO_GR,
        lines=fixtures.DEMO_LINES, ordered_at="2026-09-01T00:00:00+00:00")
    assert objects.query(store, "SELECT COUNT(*) AS n FROM credit_note")[0]["n"] == 0
    assert objects.query(
        store, "SELECT COUNT(*) AS n FROM rtv_settlement_observation")[0]["n"] == 0


def test_fixtures_source_module_never_mentions_the_authoritative_tables():
    """靶场模块的源码里不许出现那两张权威表的写语句 —— 连想都不许想。"""
    src = (MAOS_PKG / "domain" / "rtv" / "fixtures.py").read_text(encoding="utf-8")
    pattern = re.compile(
        r"(?:INSERT\s+(?:OR\s+\w+\s+)?INTO|REPLACE\s+INTO|UPDATE|DELETE\s+FROM)\s+"
        r"(?:credit_note|rtv_settlement_observation)", re.IGNORECASE)
    assert not pattern.search(src), "靶场里预置了外部权威事实"


def test_fixtures_refuse_source_documents_with_nothing_to_return(store):
    """一行不合格都没有的源单据造不出退货案子 —— 在造数据这一步就响。"""
    with pytest.raises(ValueError, match="quantity_rejected"):
        fixtures.seed_source_documents(
            store, tenant_id=TEN, supplier_id=fixtures.DEMO_SUPPLIER,
            po_id="PO-CLEAN", gr_id="GR-CLEAN",
            lines=[(1, "SKU-A", 5, "10.00", 5, 0)],
            ordered_at="2026-09-01T00:00:00+00:00")


def test_intake_kwargs_carry_no_status(store):
    """靶场给的建案入参里没有 `biz_status`，也没有 `return_action`。"""
    kw = fixtures.intake_kwargs(
        tenant_id=TEN, case_id=CASE, supplier_id=fixtures.DEMO_SUPPLIER,
        po_id=fixtures.DEMO_PO, gr_id=fixtures.DEMO_GR,
        amount_claimed="360.00", plan_id=PLAN)
    assert "biz_status" not in kw and "return_action" not in kw


# ------------------------------------------------------------ ⑥ 业务对象引用
def test_business_ref_points_at_objects_instead_of_copying_them(store):
    fixtures.seed_supplier(store, tenant_id=TEN, supplier_id=fixtures.DEMO_SUPPLIER,
                           name="示例供应商")
    summary = fixtures.seed_source_documents(
        store, tenant_id=TEN, supplier_id=fixtures.DEMO_SUPPLIER,
        po_id=fixtures.DEMO_PO, gr_id=fixtures.DEMO_GR,
        lines=fixtures.DEMO_LINES, ordered_at="2026-09-01T00:00:00+00:00")
    guard.create_case(store, **fixtures.intake_kwargs(
        tenant_id=TEN, case_id=CASE, supplier_id=fixtures.DEMO_SUPPLIER,
        po_id=fixtures.DEMO_PO, gr_id=fixtures.DEMO_GR,
        amount_claimed=summary["amount_claimed"], plan_id=PLAN))

    objects.attach_business_ref(store, plan_id=PLAN, task_id="t-1",
                                object_table="rtv_case", object_id=CASE,
                                purpose="受理")
    objects.attach_business_ref(store, plan_id=PLAN, task_id="t-1",
                                object_table="purchase_order", object_id=fixtures.DEMO_PO,
                                object_version=1, purpose="源 PO")

    refs = objects.list_business_refs(store, plan_id=PLAN, task_id="t-1")
    assert [r["object_table"] for r in refs] == ["purchase_order", "rtv_case"]
    for ref in refs:
        assert objects.resolve_business_ref(store, ref, tenant_id=TEN) is not None

    # 版本对不上就指不到 —— 完整性靠读的时候查，不靠数据库外键。
    stale = dict(refs[0], object_version=99)
    assert objects.resolve_business_ref(store, stale, tenant_id=TEN) is None
    # 取值域之外的表名同样指不到，不抛。
    unknown = dict(refs[0], object_table="不存在的表")
    assert objects.resolve_business_ref(store, unknown, tenant_id=TEN) is None


def test_business_ref_does_not_leak_across_tenants(store):
    """引用表没有 `tenant_id`（C-R1 冻结的形状），租户由调用方递进来。

    少了它这条查询会跨租户命中 —— 本域所有业务表都以 tenant_id 打头，
    这一条就是那个设计的兑现。
    """
    fixtures.seed_supplier(store, tenant_id=TEN, supplier_id=fixtures.DEMO_SUPPLIER,
                           name="示例供应商")
    summary = fixtures.seed_source_documents(
        store, tenant_id=TEN, supplier_id=fixtures.DEMO_SUPPLIER,
        po_id=fixtures.DEMO_PO, gr_id=fixtures.DEMO_GR,
        lines=fixtures.DEMO_LINES, ordered_at="2026-09-01T00:00:00+00:00")
    guard.create_case(store, **fixtures.intake_kwargs(
        tenant_id=TEN, case_id=CASE, supplier_id=fixtures.DEMO_SUPPLIER,
        po_id=fixtures.DEMO_PO, gr_id=fixtures.DEMO_GR,
        amount_claimed=summary["amount_claimed"], plan_id=PLAN))
    ref = objects.attach_business_ref(store, plan_id=PLAN, task_id="t-1",
                                      object_table="rtv_case", object_id=CASE)
    assert objects.resolve_business_ref(store, ref, tenant_id=TEN) is not None
    assert objects.resolve_business_ref(store, ref, tenant_id="tnt-别人") is None


# ------------------------------------------------------------ ⑦ 域层不依赖内核
def test_domain_modules_do_not_import_the_kernel():
    """业务对象层只依赖自己 —— 不 import contracts / core / runtime。

    一旦域层反过来依赖内核，「业务对象层是纯新增」这句话就要打折。
    """
    bad: list[str] = []
    for path in sorted((MAOS_PKG / "domain" / "rtv").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for n in names:
                if n.startswith(("maos.contracts", "maos.core", "maos.runtime")):
                    bad.append(f"{path.name}: {n}")
    assert not bad, f"域层 import 了内核：{bad}"
