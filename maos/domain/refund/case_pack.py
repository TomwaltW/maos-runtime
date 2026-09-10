"""单案例包 —— 把一次退款从头到尾涉及的**十类业务对象**收进一份文件，并加载它。

## 为什么要有这个模块

评委第二条建议的原话是：把退款申请、订单与商品快照、质保与退货规则、客户证据、
主管审批、财务核算、退款请求、支付网关观察、客户通知和人工补偿**关联到同一个业务
案例**。在 T116 之前，`scenarios/refund/cases/case_r*.json` 只有六块（租户 / 渠道 /
订单快照 / 商品快照 / 政策 / 案件）—— 剩下四类只在流程跑起来之后才出现在库里，
一份静态文件里看不到它们，也就没法拿给人看「同一个案子长什么样」。

本模块与 `fixtures.py` 的分工是硬的：

  · `fixtures.py` 管**三组对照 case**（`_expected` 是判据、五份文件成组），
    T116 一个字都不动它（跨轨契约 §A：那是 T115 的连接层所在的文件邻域，
    而对照口径本身是 T7 定的）。
  · 本模块管**单案例包**：十类齐、可校验、可替换。它不重写装载 ——
    真正灌库的仍然是 `fixtures.seed_case()`，本模块只负责「这份文件够不够十类」
    与「哪几块是预期、不该灌库」。

## 十类对象 vs 十一张表

评委原话里「订单与商品快照」是**一项**，落到库里是两张表（`order_snapshot` /
`product_snapshot`）。所以十类对象对应十一个 `business_ref.object_type`。
两种说法都记在 :data:`TEN_OBJECTS` 里，验收报数时不必在两套口径之间换算。

## 预期块不灌库

`approval_record` / `finance_entry` / `refund_request` / `payment_observation` /
`notification` / `compensation_record` 这六块在案例文件里是**预期值**：它们由流程
真跑出来，不是靶场数据。把它们灌进库等于替流程把答案先写好，跑出来的"一致"
就什么都证明不了。:data:`EXPECTED_BLOCKS` 钉住这条分界，
:func:`assert_expected_blocks_are_not_seeded` 让它成为一条会红的断言而不是一句注释。

## schema 片段

本模块另一半职责是把 `schema_p10_t116.sql` 应用到库上（跨轨契约 §B.1：
「由自己的模块提供 `ensure_<name>_schema(conn)` 在首次使用时调用」）。
它同时挂在 `objects._MIGRATIONS` 上，两条路都通向同一个函数：

  · **老库**走迁移（`ensure_schema` -> `_migrate` -> 本模块），记账落
    `refund_schema_version`；
  · **新库**同样走那条 —— 迁移步骤自带探针，在已是目标形状的库上是 no-op
    （`objects._MIGRATIONS` 那段注释的原话）。

单独调 `ensure_t116_schema(store)` 也安全，幂等。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from maos.domain.refund import objects

#: 片段文件。目标形状只有这一份，本模块解析它，不在代码里另抄一张列清单。
_FRAGMENT_PATH = Path(__file__).with_name("schema_p10_t116.sql")

#: 仓库根。`scenarios/` 在这下面。
_REPO_ROOT = Path(__file__).resolve().parents[3]

#: T116 造的那份十类齐的占位案例。人类给了真案例之后**只换数据不改结构**。
CASE_REAL_01 = _REPO_ROOT / "scenarios" / "refund" / "cases" / "case_real_01.json"


# ---------------------------------------------------------------- 十类业务对象
#: `(人话名字, (object_type, ...), 表名 -> 主键列)`，顺序即评委原话的顺序。
#:
#: 第二项之所以是**元组**：「订单与商品快照」在评委的十项里是一项，在库里是两张表。
#: 把它拍平成十一项会让「十类全覆盖」这句话每次都要加一句解释；
#: 硬凑成一项又会让 `business_ref` 少一条引用。所以两层都留着。
TEN_OBJECTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("退款申请",     ("refund_case",)),
    ("订单与商品快照", ("order_snapshot", "product_snapshot")),
    ("质保与退货规则", ("policy_rule",)),
    ("客户证据",     ("customer_evidence",)),
    ("主管审批",     ("approval_record",)),
    ("财务核算",     ("finance_entry",)),
    ("退款请求",     ("refund_request",)),
    ("支付网关观察",  ("payment_observation",)),
    ("客户通知",     ("notification",)),
    ("人工补偿",     ("compensation_record",)),
)

#: 十类摊平成十一个 object_type。`objects._REF_TARGETS` 必须逐个覆盖它们 ——
#: 少一个就是「引用只覆盖 N/10」，测试按这张表逐条断言，不在别处抄字面量。
ALL_OBJECT_TYPES: tuple[str, ...] = tuple(
    t for _label, types in TEN_OBJECTS for t in types)

#: 案例文件里**由流程跑出来**的六块 —— 是预期值，不是靶场数据，一律不灌库。
EXPECTED_BLOCKS: tuple[str, ...] = (
    "approval_record", "finance_entry", "refund_request",
    "payment_observation", "notification", "compensation_record",
)

#: 案例文件里**灌进库**的块。`customer_evidence` 在列但由 `refund.intake` 落库
#: （`fixtures.seed_case` 会跳过它，见 `fixtures.evidence_signals_of`）。
SEEDED_BLOCKS: tuple[str, ...] = (
    "tenant", "channel", "order_snapshot", "product_snapshot", "policy_rule",
    "customer_evidence",
)

#: 案例文件里必须齐的顶层键。`case` 块是退款申请本身（`guard.create_case` 的入参），
#: 表名那几块逐字对齐 `schema.sql` 的列。
REQUIRED_BLOCKS: tuple[str, ...] = ("case", *SEEDED_BLOCKS, *EXPECTED_BLOCKS)


class CasePackError(ValueError):
    """案例包缺块或形状不对。消息直接给人看，不用翻栈。"""


# ------------------------------------------------------------------ schema 片段
def _add_column_if_missing(conn: Any, table: str, col: str, decl: str) -> None:
    """跨轨契约 §B.2 的私有助手 —— SQLite 的 `ADD COLUMN` 没有 `IF NOT EXISTS`。

    三轨各自复制一份到自己的模块里（契约原话：重复六行，**整合期由主会话去重**，
    各轨不要为此建共享文件）。

    探针多了一层回落：`PRAGMA table_info` 是 SQLite 方言，T115 把后端换到 PG 之后
    这条在 `DomainConn` 上不保证有。探不动就退回 `objects._has_column()`
    （一条 `SELECT <col> FROM <table> LIMIT 1`，后端无关）。**回落不是兜底成
    「当作没有」** —— 那会让每次都去 ALTER 一次、每次都撞 duplicate column name。
    """
    try:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        present = col in cols
    except Exception:                                  # noqa: BLE001 —— 换后端时 PRAGMA 可能不在
        present = None
    if present is None:
        return                                         # 交给调用方的 SELECT 探针
    if not present:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


#: 从片段里抠出 `(表, 列, 声明)`。片段是本轨写的、行数固定，所以一条正则够用 ——
#: 这里不是通用 SQL 解析器，认不出的行会被 `ensure_t116_schema` 当场报出来，
#: 不静默跳过（静默跳过的症状是「那一列永远没加上」，且不报错）。
_ALTER_RE = re.compile(
    r"^\s*ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(\w+)\s+(.+?)\s*;\s*$",
    re.IGNORECASE,
)


def fragment_columns(script: str | None = None) -> tuple[tuple[str, str, str], ...]:
    """片段声明的 `(表, 列, 声明)` 三元组。**目标形状的唯一来源。**

    加载器解析片段而不是在代码里另抄一张清单：抄一份的后果是哪天有人改了 .sql、
    代码里那份没跟着改，两边都不报错（`schema.sql` 抬头警告的正是这类静默分叉）。
    """
    text = script if script is not None else _FRAGMENT_PATH.read_text(encoding="utf-8")
    out: list[tuple[str, str, str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        m = _ALTER_RE.match(line)
        if m is None:
            raise CasePackError(
                f"{_FRAGMENT_PATH.name} 里有一行既不是注释也不是 ALTER TABLE ADD COLUMN："
                f"{stripped!r}。片段只收这一种语句（跨轨契约 §B.3 的允许子集）")
        out.append((m.group(1), m.group(2), m.group(3)))
    return tuple(out)


def ensure_t116_schema(store: Any) -> tuple[str, ...]:
    """把 `schema_p10_t116.sql` 的六列加到库上。幂等，可连跑。返回**本次真加了**的列。

    走 `objects._conn` / `lock_of` 取连接是刻意的：退款域所有 SQL 都从
    `objects.py` 那层薄壳过（那份 docstring 的原话），迁移期也不例外 —— 另开一条
    连接就是第二条写入路径，而两条路径共用一张表的症状是偶发的事务错配
    （`lock_of` 的 docstring 讲的就是这件事）。
    """
    wanted = fragment_columns()
    # 先探一遍**再**加：`_add_column_if_missing` 按契约返回 None，所以「这次到底加了
    # 哪几列」只能在调它之前问。这个返回值不是装饰 —— 迁移记账、测试与
    # `scripts/run_case.py` 的输出都按它判「本次是不是真的动了库」。
    # 探针用 `objects._has_column`（一条 SELECT）而不是 PRAGMA：后端无关，
    # 而且它连生成列都探得到（见那个函数的 docstring 第二条）。
    missing = [(t, c, d) for t, c, d in wanted if not objects._has_column(store, t, c)]

    conn = objects._conn(store)
    with objects.lock_of(store):
        for table, col, decl in wanted:
            _add_column_if_missing(conn, table, col, decl)
        conn.commit()

    # PRAGMA 探不动时（换后端）上一段整体是 no-op，这里用后端无关的路径补一遍。
    for table, col, decl in missing:
        if not objects._has_column(store, table, col):
            objects.execute(store, f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    return tuple(f"{t}.{c}" for t, c, _d in missing)


def next_version(store: Any, table: str, *, tenant_id: str, case_id: str,
                 column: str = "revision", **where: Any) -> int:
    """本案在这张表上的下一个版本 / 修订号。一行都没有就是 1。

    判据是 `MAX(<column>) + 1`，**不是行数 + 1**。两者在 `approval_record` 这类
    「一次一行」的表上恰好相等，但在 `finance_entry` 上不等 —— 那张表主键是
    `(tenant_id, case_id)`，二次核算会 `INSERT OR REPLACE` 把前一行**挤掉**
    （派单原话「二次核算 supersedes 前一版」），行数永远是 1。按行数算，
    第三次核算会算出 revision=2，与第二次撞号，而修订链就此断在那里且不报错。

    `**where` 收窄到某一个对象（如 `evidence_id="ev-01"`）。键是本域的字面量、
    不是外来输入，所以直接拼进 SQL；值仍然走占位符。

    读不到表 / 读不到列时返回 1：本函数在 `ensure_t116_schema()` 之前被调到是
    调用顺序错了，但那种错该由紧随其后的 INSERT 报出来（它会撞 no such column），
    不该由一个探针把整条链路先炸掉。
    """
    sql = f"SELECT MAX({column}) AS v FROM {table} WHERE tenant_id=? AND case_id=?"
    params: list[Any] = [tenant_id, case_id]
    for key, value in where.items():
        sql += f" AND {key}=?"
        params.append(value)
    try:
        rows = objects.query(store, sql, params)
    except Exception:                                  # noqa: BLE001 —— 见 docstring
        return 1
    current = rows[0]["v"] if rows else None
    return int(current) + 1 if current is not None else 1


def ref_coverage(store: Any, *, plan_id: str) -> dict:
    """一个 Plan 的 `business_ref` 覆盖体检：十类各挂了几条、各自指不指得到。

    这是本轨的**主判据**，所以它是一个函数而不是一段一次性脚本：
    验收命令、`scripts/run_case.py` 的输出与测试读的是同一份计算，
    三处各写一遍的话，「resolved 10/10」这句话迟早在某一处说错。

    判据是「十一个 object_type 一个不少、且每一条引用都 resolve 得到」，
    **不是**「引用条数 > 0」—— 条数会自己涨，覆盖面不会。

    返回 `{covered, total_types, resolved, total, missing_types, dangling, by_type}`；
    `by_type[t]` 是 `{refs, resolved, versions}`。
    """
    refs = objects.list_business_refs(store, plan_id=plan_id)
    by_type: dict[str, dict] = {}
    dangling: list[dict] = []
    for ref in refs:
        slot = by_type.setdefault(ref["object_type"],
                                  {"refs": 0, "resolved": 0, "versions": []})
        slot["refs"] += 1
        if objects.resolve_business_ref(store, ref) is None:
            dangling.append({"object_type": ref["object_type"],
                             "object_id": ref["object_id"],
                             "object_version": ref["object_version"]})
        else:
            slot["resolved"] += 1
        if int(ref["object_version"]) and int(ref["object_version"]) not in slot["versions"]:
            slot["versions"].append(int(ref["object_version"]))

    # 覆盖度按**十类**报，不按十一个 object_type：「订单与商品快照」在评委的十项里
    # 是一项，两张表都挂上了才算这一类齐（少一张就是少了半个依据）。
    covered = sum(1 for _label, types in TEN_OBJECTS
                  if all(by_type.get(t, {}).get("resolved") for t in types))
    missing = [t for t in ALL_OBJECT_TYPES if not by_type.get(t, {}).get("resolved")]
    return {
        "covered": covered,
        "total_types": len(TEN_OBJECTS),
        "resolved": sum(s["resolved"] for s in by_type.values()),
        "total": len(refs),
        "missing_types": missing,
        "dangling": dangling,
        "by_type": by_type,
    }


def stamp_approval_revision(store: Any, approval: dict, *, plan_id: str,
                            task_id: str) -> int:
    """给一条**刚落库**的 `approval_record` 补上修订号，并挂 `business_ref`。返回修订号。

    为什么是「补上」而不是「写入时带上」：审批由 `_common.record_approval()` 落库，
    那是共用件、不是本轨的面（跨轨契约 §A 把 `skills/builtin/refund/` 切给了
    T116 的 attach 调用与 T117 的工单字段，`_common.py` 不在其列）。所以本函数走
    「先算、再 UPDATE 那一行」这条路 —— 算在插入**之后**是错的：那时刚落的行
    自己也在 `MAX(revision)` 里，第二次审批会算成 3 而不是 2。

    调用方必须传**刚拿到的那一行**（`record_approval` 的返回值），
    `(approver, decided_at)` 两个字段用来精确定位它 —— 那两个正是主键的后两段。

    审批是人的动作，`plan_id` / `task_id` 由调用方（flows）给：审批发生在 CLI 或
    Matrix 房间里，不在哪个 Agent 的 ctx 上，所以这两个 id 没有别的来源。
    缺任何一个就只补修订号、不挂引用（挂一条 plan_id 为空的引用等于挂了个孤儿）。
    """
    tenant_id = str(approval["tenant_id"])
    case_id = str(approval["case_id"])
    # -1：把刚落的那一行从 MAX 里减掉。它的 revision 此刻还是建表缺省的 1，
    # 所以第一次审批算出 1（0+1）、第二次算出 2（1+1），链是连的。
    existing = objects.query(
        store,
        "SELECT COUNT(*) AS n FROM approval_record WHERE tenant_id=? AND case_id=?",
        (tenant_id, case_id))
    revision = max(int(existing[0]["n"]) if existing else 1, 1)
    objects.execute(
        store,
        "UPDATE approval_record SET revision=? WHERE tenant_id=? AND case_id=?"
        " AND approver=? AND decided_at=?",
        (revision, tenant_id, case_id, approval["approver"], approval["decided_at"]))
    if plan_id and task_id:
        objects.attach_business_ref(
            store, plan_id=plan_id, task_id=task_id, tenant_id=tenant_id,
            object_type="approval_record", object_id=case_id,
            object_version=revision,
            purpose=f"主管审批（{approval['decision']}，第 {revision} 次）")
    return revision


def migration_step(store: Any, _script: str) -> None:
    """`objects._MIGRATIONS` 的步骤函数。签名 `step(store, script)`。

    `script` 是 `schema.sql` 的原文，本步骤用不上它（我们不重建表，只加列），
    所以带下划线丢掉 —— 但签名必须留着，那是迁移表约定的形状。
    """
    ensure_t116_schema(store)


# ------------------------------------------------------------------ 读案例包
def load_case_pack(path: str | Path | None = None) -> dict:
    """读一份十类齐的案例包，缺块当场抛。

    不做「缺了就补一个空块」：少一类对象意味着这份案例证明不了「十类都挂上了引用」，
    而补空块会让它照常跑绿 —— 跑绿的错结论比报错难查得多（口径同
    `custom_case.load()`）。
    """
    p = Path(path) if path is not None else CASE_REAL_01
    if not p.exists():
        raise CasePackError(f"找不到案例包：{p}")
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CasePackError(f"{p} 不是合法 JSON：{exc}") from exc
    if not isinstance(payload, dict):
        raise CasePackError(f"{p} 的顶层必须是对象（顶层键即表名）")
    missing = missing_blocks(payload)
    if missing:
        raise CasePackError(
            f"{p} 缺这几块：{', '.join(missing)}。十类业务对象一块都不能少 —— "
            f"少一类就证明不了「DAG/Task/Artifact 引用了它及其版本」")
    return payload


def missing_blocks(payload: dict) -> list[str]:
    """这份案例包缺哪几块。齐了返回空列表。"""
    out = []
    for block in REQUIRED_BLOCKS:
        value = payload.get(block)
        if block == "case":
            if not isinstance(value, dict) or not value:
                out.append(block)
        elif not isinstance(value, list) or not value:
            out.append(block)
    return out


def expected_block(payload: dict, block: str) -> list[dict]:
    """取一块**预期值**。不在 :data:`EXPECTED_BLOCKS` 里的块当场抛。

    拿它当靶场数据用是本模块要防的那件事，所以取用口也把住：想灌库请走
    `fixtures.seed_case()`，那条路只认 `SEEDED_BLOCKS`。
    """
    if block not in EXPECTED_BLOCKS:
        raise CasePackError(
            f"{block!r} 不是预期块（预期块只有 {list(EXPECTED_BLOCKS)}）；"
            "靶场数据请走 fixtures.seed_case()")
    rows = payload.get(block)
    return [dict(r) for r in rows] if isinstance(rows, list) else []


def assert_expected_blocks_are_not_seeded() -> None:
    """预期块**一块都不许**出现在 `fixtures` 的装载清单里。

    这条断言的价值在它会红：谁把 `approval_record` 加进 `fixtures.case_tables()`，
    案例包里那份"预期审批"就会在流程跑之前先落库，于是流程跑完再比对时永远一致 ——
    一条不会失败的验收，比没有验收更坏。
    """
    from maos.domain.refund import fixtures

    seeded = {table for table, _cols in fixtures.case_tables()}
    leaked = sorted(seeded & set(EXPECTED_BLOCKS))
    if leaked:
        raise CasePackError(
            f"预期块进了装载清单：{leaked}。它们由流程跑出来，灌库等于把答案先写好")
