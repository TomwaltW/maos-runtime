"""RTV 域六个 skill 的共用件 —— 只放**跨 skill 复用的机制**，不放业务判定。

下划线开头，`builtin/__init__.py::discover()` 扫不到它（`mod.name.startswith("_")`
跳过），本包的 `__init__.py` 显式 import 各 skill 模块，所以它永远不会被误当成
一个 skill。口径同 `builtin/ap/_common.py`。

## 为什么本文件比 `ap/_common.py` 厚：域的另外三层还在别的轨上

契约 C-R8 把 RTV 域拆成五轨并行：业务对象层（`maos/domain/rtv/`）在 T61、
ToolPort 层（`maos/tools/rtv.py`）在 T62、Agent 与场景在 T64。它们**不在本轨的
基线里** —— import 一个不存在的模块，本轨从第一条测试起就是红的（C-R8 末条）。

所以本文件按契约自带三样东西，整合期再换成真的：

  1. **建表**：`ensure_schema()` 先试 T61 的 `maos.domain.rtv.objects`，
     import 不到就落到本模块内嵌的那份 SQL（照抄契约 C-R1 全文）。
     🔴 **这是本轨唯一一处试探性 import**，写在 `_rtv_domain_objects()` 一个函数里 ——
     整合期删 fallback 只改一处，不必去六个 skill 里翻。
  2. **权威守卫**：T61 的 `guard.py` 不在，而「只有 rtv.observe 写得进 credited /
     settled」这条不能等到整合期才成立（铁律 8）。所以本文件自带一份同形状的守卫，
     常量逐字对齐契约 C-R2 / C-R3。整合期换成 T61 那份，两边**必须仍是同一组常量**。
  3. **工具**：C-R5 那五个 ToolPort 在 T62。本文件按名从 `ctx.extras["tools"]` 取，
     取到裸函数就地包成 `ToolPort` 走 `invoke_tool` —— 审计行照样落，
     整合期把 T62 的真 port 注进同一个键即可，六个 skill 一个字不用改。

## 一条自守的规矩：写库一律从这里过

`execute()` **拒绝任何对 `rtv_case` / `credit_note` / `rtv_settlement_observation`
的写入**，它们只有下面 `create_case()` / `update_biz_status()` 写得动。
这是把「不留第二条路径」从 grep 自查升级成运行时拦截 —— 口径同
`maos/domain/ap/objects.py::_guarded`。

## 与 `maos/domain/ap/**` 的关系：照抄口径，不 import 跨域

`money()` / `attach_business_ref()` 的形状抄自 `maos/domain/ap/objects.py`，
但**不 import**（契约 C-R9）。抽成公共基类之后那个基类就成了两个域共同持有的面，
换第三个域时要动它，而动它就等于动另外两个域 —— `docs/domain-portability.md`
那句主张的落点正在这里。

真正**共用的是表**：`supplier` / `purchase_order` / `purchase_order_line` /
`goods_receipt` / `goods_receipt_line` 五张全部复用 `maos/domain/ap/schema.sql`
的既有定义，一张都不重建（C-R1 抬头）。fallback 建表因此要把那份 schema 也执行
一遍 —— 缺了它们，`rtv.intake` 的「定位源 PO/GR」无处可查。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from maos.tools.port import ToolPort, invoke_tool

#: 本域在事件 detail 里的域标记（契约 C-R3）。
DOMAIN = "rtv"

#: 业务类型标记。本域的任务在 `task.inputs["biz_type"]` 上带它。
#:
#: **刻意不是 "refund"**：`maos/runtime/gate.py` 的第六道财务复核闸按
#: `inputs["biz_type"] == FINANCE_BIZ_TYPE`（= "refund"）触发，判据是同 attempt 的
#: 产物里有没有 `finance_entry`。本域产不出那种凭据，冒用那个标记会让闸恒 blocker，
#: 而报错信息会指向退款域的财务复核，离原因极远。口径同 `ap/_common.py`。
BIZ_TYPE = "rtv"


# ---------------------------------------------------------------------------
# 一、外部系统的状态取值域（契约 C-R5 与 T62 派单 §5.3 那张状态表）
# ---------------------------------------------------------------------------
# **终态由我方按这张表判，不看外部回执自述的 `is_terminal`**：回执里那个字段是
# 外部系统（本轨里是 stub）说的，信它等于把「这算不算终态」的判据交给被观察方。
# 一个把 acknowledged 标成 is_terminal=True 的实现会让 `rtv.observe` 提前收口，
# 而那正是本域最不该出的错。

#: 供应商门户：一笔退货授权/贷项通知单申请的状态。
SUPPLIER_STATES = ("submitted", "acknowledged", "issued", "disputed", "unknown")
#: 供应商侧的终态。🔴 `acknowledged` **不在**里面 —— 那是「供应商收到货了」，
#: 不是「供应商认了这笔钱」，两者差着一次会计确认（契约 C-R3 那段红字）。
SUPPLIER_TERMINAL = frozenset({"issued", "disputed"})

#: 承运商：一张退货运单的轨迹状态。
CARRIER_STATES = ("created", "in_transit", "delivered", "exception")
CARRIER_TERMINAL = frozenset({"delivered", "exception"})

#: AP 系统：一张调整凭单 / 借项通知单的状态。
AP_STATES = ("none", "staged", "built", "settled", "voided")
AP_TERMINAL = frozenset({"settled", "voided"})

#: 系统名 -> (取值域, 终态集合)。`terminal()` 按它判，不在各处抄集合字面量。
_VOCAB: dict[str, tuple[tuple[str, ...], frozenset]] = {
    "supplier": (SUPPLIER_STATES, SUPPLIER_TERMINAL),
    "carrier": (CARRIER_STATES, CARRIER_TERMINAL),
    "ap": (AP_STATES, AP_TERMINAL),
}


def require_state(system: str, status: Any) -> str:
    """校验外部回执的状态落在取值域里，落不进去就抛 —— **不兜底成非终态**。

    兜底的后果是「外部系统回了个我们没见过的词」被静默处理成「还没到终态」，
    于是轮询到顶、如实返回「还没问出来」，看起来一切正常。而真实情况可能是对方
    改了协议：一笔已经开票的退货会永远停在 `shipped`，且没有任何信号。
    """
    vocab, _terminal = _VOCAB[system]
    text = str(status)
    if text not in vocab:
        raise ValueError(
            f"{system} 侧回了一个取值域外的状态 {text!r}；已知取值：{list(vocab)}。"
            "取值域对不上说明对方协议变了，这里不许猜，交给人看")
    return text


def terminal(system: str, status: Any) -> bool:
    """这个状态是不是该系统的终态。判据在我方，不看回执自述（见本节抬头）。"""
    _vocab, states = _VOCAB[system]
    return require_state(system, status) in states


# ---------------------------------------------------------------------------
# 二、工具名（契约 C-R5，落点 maos/tools/rtv.py 在 T62）
# ---------------------------------------------------------------------------
TOOL_RMA_SUBMIT = "supplier.rma_submit"        # 写：提退货授权申请
TOOL_CREDIT_QUERY = "supplier.credit_query"    # 只读：查贷项通知单
TOOL_CARRIER_SHIP = "carrier.ship"             # 写：下发运单
TOOL_CARRIER_TRACK = "carrier.track"           # 只读：查轨迹
TOOL_AP_ADJUST_QUERY = "ap.adjust_query"       # 只读：查调整凭单状态

#: 🔴 `ap.adjust_query` 只读。RTV 域不许写 AP 的任何东西 —— 调整凭单由 AP 侧按
#: 自己的规则建，我方只观察（契约 C-R5 末条）。所以本清单里没有 `ap.adjust_build`。
RTV_TOOLS = (TOOL_RMA_SUBMIT, TOOL_CREDIT_QUERY, TOOL_CARRIER_SHIP,
             TOOL_CARRIER_TRACK, TOOL_AP_ADJUST_QUERY)


# ---------------------------------------------------------------------------
# 三、退货理由与裁定规则（本轨的临时口径，整合期换成 T62 的 maos/tools/rtv_codes.py）
# ---------------------------------------------------------------------------
# 契约只冻结了「rationale_json 每条必带 rule_id，且该编号必在 rtv_codes.RULES 里」，
# 没有冻结编号本身 —— 那张表在 T62，不在本轨基线里。所以这里放一份**同形状**的
# 最小表，`rtv.dispose` 的裁定依据从它取。整合期把这两个 dict 换成 rtv_codes 的
# 同名表即可，`dispose.py` 一个字不用改（它只用 `require_reason` / `require_rule`）。
#
# 编号带 `RTV-` 前缀、与 ap 域的 `BR-xx`（Peppol）不同名：两张表出处不同，
# 撞名会让「这个编号是哪来的」变成要先猜是哪个域。

#: 退货理由码 -> (说明, 该理由默认导向的处置类型)。
RETURN_REASONS: dict[str, tuple[str, str]] = {
    "defective":     ("到货即为次品/功能不良", "credit"),
    "damaged":       ("运输途中损坏", "credit"),
    "wrong_item":    ("发错货：品名/规格与订单不符", "replacement"),
    "not_ordered":   ("未订购的货物", "replacement"),
    "over_shipment": ("超发：数量多于订单", "credit"),
    "spec_change":   ("规格变更，需换成另一型号", "exchange"),
}

#: 规则编号 -> (结论, 判据说明)。裁定与对账的每一条结论都必须挂得上其中一条。
#:
#: 两组规则共用一张表，是因为契约 C-R1 对 `rtv_disposition.rationale_json` 与
#: `rtv_reconciliation.findings_json` 提的是同一条要求：每项必带 `rule_id`，
#: 且编号必在 `RULES` 里。分成两张表会让「这个编号查哪一张」多一次猜。
#: 结论取值域按用途分开：裁定那组落在 `RETURN_ACTIONS` + `rejected`，
#: 对账那组落在 `mismatch` / `pending`。
RULES: dict[str, tuple[str, str]] = {
    # ---- 裁定（rtv.dispose）
    "RTV-R-01": ("credit", "质量类理由（defective/damaged）按合同走贷项通知单退款"),
    "RTV-R-02": ("replacement", "发错货或未订购：供应商补发正确货物，不动钱"),
    "RTV-R-03": ("credit", "超发部分退款，不做换货"),
    "RTV-R-04": ("exchange", "规格变更：换成另一型号，价差另行结算"),
    "RTV-R-05": ("rejected", "超出合同退货窗口，不予受理"),
    # ---- 对账（rtv.reconcile）
    "RTV-R-10": ("mismatch", "退货行合计与贷项通知单金额不符，且差额超出容差"),
    "RTV-R-11": ("pending", "供应商尚未开出贷项通知单，三方对账缺「供应商认了」这条腿"),
    "RTV-R-12": ("pending", "AP 侧尚无核销观察，钱是否到账未知"),
    "RTV-R-13": ("mismatch", "供应商 disputed：不认这笔退货"),
}

#: 退货理由码 -> 裁定它的规则编号。裁定结论不从 `RETURN_REASONS` 的第二项直接取，
#: 而是绕这一层：结论必须**挂得上一条有编号的规则**，否则 rationale_json 里就会出现
#: 一条没有出处的结论（契约 C-R1 对 rationale_json 的要求）。
REASON_RULE: dict[str, str] = {
    "defective": "RTV-R-01",
    "damaged": "RTV-R-01",
    "wrong_item": "RTV-R-02",
    "not_ordered": "RTV-R-02",
    "over_shipment": "RTV-R-03",
    "spec_change": "RTV-R-04",
}

#: 超出合同退货窗口时引用的规则编号。它的结论是 `rejected` —— 不是三种处置之一，
#: 所以走不进 `rtv_disposition`（那张表的 CHECK 只收三种），见 `dispose.py`。
RULE_OUT_OF_WINDOW = "RTV-R-05"

#: 处置类型取值域（与 C-R1 的 CHECK 约束一致，`''` 是建案时的未裁定态）。
RETURN_ACTIONS = ("credit", "exchange", "replacement")


def require_reason(reason_code: Any) -> tuple[str, str]:
    """取一条退货理由；码不在表里就抛，**不悄悄放过**。

    放过的后果与 `ap.intake` 那条一样：一个查无出处的理由码会一路走到对账那步，
    才以另一条完全不相干的理由被拒，而排查要从错误的那一头开始。
    """
    entry = RETURN_REASONS.get(str(reason_code))
    if entry is None:
        raise ValueError(
            f"退货理由码 {reason_code!r} 不在 RETURN_REASONS 里（已知：{sorted(RETURN_REASONS)}）")
    return entry


def require_rule(rule_id: Any) -> tuple[str, str]:
    """取一条裁定规则；编号不在表里就抛。**理由可核对**才算数。"""
    entry = RULES.get(str(rule_id))
    if entry is None:
        raise ValueError(f"规则编号 {rule_id!r} 不在 RULES 里（已知：{sorted(RULES)}）")
    return entry


def cite(rule_id: str, detail: str = "") -> dict:
    """把一条规则折成 rationale_json / findings_json 里的一项。

    每一项都带 `rule_id` —— 对方拿着编号能查到，这句话才成立（契约 C-R1 对
    `rtv_disposition.rationale_json` 与 `rtv_reconciliation.findings_json` 的要求）。
    """
    outcome, why = require_rule(rule_id)
    item = {"rule_id": rule_id, "outcome": outcome, "why": why}
    if detail:
        item["detail"] = detail
    return item


# ---------------------------------------------------------------------------
# 四、建表：先试 T61，取不到落到契约 C-R1 那份
# ---------------------------------------------------------------------------
#: 契约 C-R1 全文（逐字照抄）。**只在 T61 的 `maos/domain/rtv/` 还不在时使用** ——
#: 它是本轨的脚手架，不是本域 schema 的第二份权威。整合期删掉这一段与
#: `_ensure_schema_fallback()`，`ensure_schema()` 只剩 T61 那条路径。
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS rtv_schema_version (
    version    INTEGER NOT NULL,
    applied_at TEXT NOT NULL,
    PRIMARY KEY (version)
);

CREATE TABLE IF NOT EXISTS rtv_case (
    tenant_id   TEXT NOT NULL,
    case_id     TEXT NOT NULL,
    supplier_id TEXT NOT NULL,
    po_id       TEXT NOT NULL,
    po_version  INTEGER NOT NULL,
    gr_id       TEXT NOT NULL,
    return_action TEXT NOT NULL DEFAULT '',
    amount_claimed TEXT NOT NULL,
    currency    TEXT NOT NULL DEFAULT 'CNY',
    biz_status  TEXT NOT NULL,
    plan_id     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (tenant_id, case_id),
    CHECK (return_action IN ('', 'credit', 'exchange', 'replacement')),
    CHECK (biz_status IN ('received', 'disposed', 'shipped',
                          'credited', 'settled', 'rejected', 'compensated'))
);

CREATE TABLE IF NOT EXISTS rtv_line (
    tenant_id         TEXT NOT NULL,
    case_id           TEXT NOT NULL,
    line_no           INTEGER NOT NULL,
    gr_line_no        INTEGER NOT NULL,
    sku               TEXT NOT NULL,
    quantity_returned REAL NOT NULL,
    unit_price        TEXT NOT NULL,
    reason_code       TEXT NOT NULL,
    PRIMARY KEY (tenant_id, case_id, line_no)
);

CREATE TABLE IF NOT EXISTS rtv_disposition (
    tenant_id      TEXT NOT NULL,
    case_id        TEXT NOT NULL,
    attempt        INTEGER NOT NULL,
    return_action  TEXT NOT NULL,
    rationale_json TEXT NOT NULL DEFAULT '[]',
    decided_by     TEXT NOT NULL,
    decided_at     TEXT NOT NULL,
    PRIMARY KEY (tenant_id, case_id, attempt),
    CHECK (return_action IN ('credit', 'exchange', 'replacement'))
);

CREATE TABLE IF NOT EXISTS rtv_shipment (
    tenant_id      TEXT NOT NULL,
    case_id        TEXT NOT NULL,
    shipment_id    TEXT NOT NULL,
    carrier        TEXT NOT NULL,
    tracking_no    TEXT NOT NULL,
    carrier_status TEXT NOT NULL,
    shipped_at     TEXT NOT NULL,
    PRIMARY KEY (tenant_id, case_id, shipment_id)
);

CREATE TABLE IF NOT EXISTS credit_note (
    tenant_id       TEXT NOT NULL,
    case_id         TEXT NOT NULL,
    credit_note_id  TEXT NOT NULL,
    document_type   TEXT NOT NULL DEFAULT '381',
    amount_credited TEXT NOT NULL,
    currency        TEXT NOT NULL DEFAULT 'CNY',
    issued_at       TEXT NOT NULL,
    observed_at     TEXT NOT NULL,
    observed_by     TEXT NOT NULL,
    invocation_id   TEXT NOT NULL,
    PRIMARY KEY (tenant_id, case_id, credit_note_id)
);

CREATE TABLE IF NOT EXISTS rtv_reconciliation (
    tenant_id       TEXT NOT NULL,
    case_id         TEXT NOT NULL,
    attempt         INTEGER NOT NULL,
    reconciled      INTEGER NOT NULL,
    creditable_amount TEXT NOT NULL DEFAULT '',
    findings_json   TEXT NOT NULL DEFAULT '[]',
    tolerance_json  TEXT NOT NULL DEFAULT '{}',
    reconciled_by   TEXT NOT NULL,
    reconciled_at   TEXT NOT NULL,
    PRIMARY KEY (tenant_id, case_id, attempt)
);

CREATE TABLE IF NOT EXISTS rtv_settlement_observation (
    tenant_id      TEXT NOT NULL,
    case_id        TEXT NOT NULL,
    seq            INTEGER NOT NULL,
    adjustment_id  TEXT NOT NULL,
    observed_state TEXT NOT NULL,
    ap_reference   TEXT NOT NULL,
    observed_at    TEXT NOT NULL,
    observed_by    TEXT NOT NULL,
    invocation_id  TEXT NOT NULL,
    PRIMARY KEY (tenant_id, case_id, seq)
);

CREATE TABLE IF NOT EXISTS rtv_compensation_record (
    tenant_id     TEXT NOT NULL,
    case_id       TEXT NOT NULL,
    seq           INTEGER NOT NULL,
    reason        TEXT NOT NULL,
    detail_json   TEXT NOT NULL DEFAULT '{}',
    compensated_by TEXT NOT NULL,
    compensated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, case_id, seq)
);

CREATE TABLE IF NOT EXISTS rtv_business_ref (
    plan_id        TEXT NOT NULL,
    task_id        TEXT,
    object_table   TEXT NOT NULL,
    object_id      TEXT NOT NULL,
    object_version INTEGER,
    purpose        TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL
);
"""

#: 复用面：五张源单表在这份 schema 里，本域只引用不重建（契约 C-R1 抬头、C-R9）。
#: 用**读文件**而不是 `from maos.domain.ap import objects` —— C-R9 说得很清楚：
#: 口径可以照抄，跨域 import 不许有。读的是文本，不是那个域的代码路径。
_AP_SCHEMA_PATH = Path(__file__).resolve().parents[3] / "domain" / "ap" / "schema.sql"

_domain_objects: Any = None
_domain_probed = False


def _rtv_domain_objects() -> Any:
    """🔴 本轨**唯一**的试探性 import：T61 的 `maos/domain/rtv/` 在不在。

    在 -> 返回它的 `objects` 模块，建表走它那份（整合后的正路）。
    不在 -> 返回 None，落到 `_ensure_schema_fallback()`（本轨的路）。

    探一次就记住：import 有缓存，但**失败**的那次没有 —— 没有这个标志，
    每次建表都要重演一遍同一个 ImportError（口径同 `registry._discovered`）。
    """
    global _domain_objects, _domain_probed
    if not _domain_probed:
        _domain_probed = True
        try:
            from maos.domain.rtv import objects        # noqa: PLC0415 —— 见 docstring
            _domain_objects = objects
        except ImportError:
            _domain_objects = None
    return _domain_objects


def _ensure_schema_fallback(store: Any) -> None:
    """按契约 C-R1 建表；顺带把 ap 域那五张源单表建出来。幂等，可连跑。

    全是 `CREATE TABLE IF NOT EXISTS`，已经建好的库上是空操作。
    ap 那份也执行是必需的：`rtv.intake` 要「定位源 PO/GR」，而那两张表不属于本域
    （C-R1 抬头刻意不建它们）—— 库里没有它们时，intake 报的是 `no such table`，
    而真实原因是「这库还没装配 ap 域的源单」，两者离得很远。
    """
    conn = _conn(store)
    with lock_of(store):
        if _AP_SCHEMA_PATH.exists():
            conn.executescript(_AP_SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.executescript(_SCHEMA_SQL)
        conn.commit()


def _store_of(ctx: Any) -> Any:
    """取 store；没有就抛。

    不兜底成「没有 store 就跳过写库」：那会让 skill 报 ok 而一行数据都没落，
    是这条链路上最容易造出的假绿 —— 而本域要买的正是「Agent 说完成了 ≠ 业务成功」。
    """
    store = getattr(ctx, "store", None)
    if store is None:
        raise ValueError("RTV 域 skill 必须在有 store 的上下文里跑：业务对象无处落库")
    return store


def ensure_schema(ctx: Any) -> Any:
    """取 store 并保证本域的表已建好。每个写库的 skill 在 run() 开头调一次。

    不假设「场景已经建过了」—— 单测直接调某个 skill 也要能跑。
    """
    store = _store_of(ctx)
    objects = _rtv_domain_objects()
    if objects is not None:
        objects.ensure_schema(store)                  # 整合后走这条
    else:
        _ensure_schema_fallback(store)                # 本轨走这条
    return store


# ---------------------------------------------------------------------------
# 五、SQL 访问口径（`maos/core/store.py` 是冻结面，本域的新增表只能从连接上走）
# ---------------------------------------------------------------------------
#: 三张表的写入只有 `create_case()` / `update_biz_status()` 写得动：
#: `rtv_case` 是业务状态本身，`credit_note` 与 `rtv_settlement_observation` 是
#: **外部权威事实的回执**。我方从旁路写一行「供应商给我开了贷项通知单」，
#: 等于自己给自己开发票（契约 C-R1 对 credit_note 那段红字）。
#: ALTER TABLE 不在拦截面上 —— 正常迁移不受影响。口径同 ap 域的 `_AP_CASE_WRITE`。
_PROTECTED_WRITE = re.compile(
    r"\b(?:insert\s+(?:or\s+\w+\s+)?into|update|delete\s+from|replace\s+into)\s+"
    r"[\"'`\[]?(rtv_case|credit_note|rtv_settlement_observation)[\"'`\]]?\b",
    re.IGNORECASE,
)


class BypassedGuardError(RuntimeError):
    """有人试图绕开权威守卫直接写 `rtv_case` / 回执表。"""


def _conn(store: Any) -> Any:
    """取底层连接。只认暴露了 `_conn` 的 Store 实现（当前是 `SqliteStore`）。"""
    conn = getattr(store, "_conn", None)
    if conn is None:
        raise TypeError(
            f"{type(store).__name__} 没有暴露 sqlite 连接，RTV 域的新增表无处落库。"
            " 换后端时在这里加一条分支，不要去改冻结的 store.py。")
    return conn


def lock_of(store: Any) -> Any:
    """借 Store 自己的锁 —— 连接是共享的，不共用锁就守不住「回执与状态同事务」。"""
    lock = getattr(store, "_lock", None)
    return lock if lock is not None else contextlib.nullcontext()


def _guarded(sql: str) -> str:
    if _PROTECTED_WRITE.search(sql):
        raise BypassedGuardError(
            "rtv_case / credit_note / rtv_settlement_observation 的写入必须走 "
            "create_case / update_biz_status，不许经 execute 旁路（铁律 8）")
    return sql


def execute(store: Any, sql: str, params: tuple | list = ()) -> None:
    """本域的写入口径。对三张受保护表的写入一律拒绝。"""
    conn = _conn(store)
    with lock_of(store):
        conn.execute(_guarded(sql), tuple(params))
        conn.commit()


def query(store: Any, sql: str, params: tuple | list = ()) -> list[dict]:
    """本域的读取口径。读不设限 —— 守的是写入方，不是读取方。"""
    with lock_of(store):
        rows = _conn(store).execute(sql, tuple(params)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 六、小工具：时间、摘要、金额、必填入参
# ---------------------------------------------------------------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(obj: Any) -> str:
    try:
        raw = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        raw = repr(obj)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def money(value: Any) -> Decimal:
    """金额一律 Decimal，**不进 float**。解析不出来就抛，不兜底成 0。

    三方对账（退货行 × 贷项通知单 × 到账）判的是等式，0.1+0.2 那种误差会直接
    变成一条**假的对账差异** —— 一笔金额完全正确的退货被判成对不上，而差异理由
    还挂着一个真实的规则编号，看起来毫无破绽。口径照抄 `ap/objects.py::money`。
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        value = repr(value)                 # Decimal(0.1) 会把二进制误差带进来
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError):
        raise ValueError(f"金额解析不出数值：{value!r}") from None


def money_str(value: Any, places: str = "0.01") -> str:
    """折成两位小数的字符串，供落库与产物使用。落库的金额一律走这里。"""
    return str(money(value).quantize(Decimal(places)))


def required(payload: dict, *keys: str) -> tuple:
    """取必填入参，缺一个就抛。

    `SkillContract.preconditions` 只查「键存在且非 None」，空字符串照样过；
    而 tenant_id 空字符串会让写库落到一个谁也读不到的租户下，是静默的错。
    """
    out, missing = [], []
    for key in keys:
        value = payload.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(key)
        out.append(value)
    if missing:
        raise ValueError(f"缺必填入参：{missing}")
    return tuple(out)


# ---------------------------------------------------------------------------
# 七、actor 锚点与工具调用
# ---------------------------------------------------------------------------
def invocation_id_of(ctx: Any) -> str:
    """本次调用的 actor 锚点。invoker 给了就用它的，没给就本地生成，**恒非空**。

    兜底成空字符串等于让整条审计链断掉：库里那行回执的 `invocation_id` 指不回
    「是哪一次调用观察到的」，而那正是权威事实要交代的第一件事。
    """
    extras = getattr(ctx, "extras", None) or {}
    return str(extras.get("invocation_id") or "").strip() or uuid.uuid4().hex


def tool_extras(ctx: Any) -> dict:
    """给 `invoke_tool` 的审计上下文。缺哪个就是空，不编。"""
    extras = getattr(ctx, "extras", None) or {}
    return {
        "plan_id": extras.get("plan_id", ""),
        "task_id": extras.get("task_id"),
        "trace_id": extras.get("trace_id", ""),
    }


def tool_port(ctx: Any, name: str) -> ToolPort:
    """按契约 C-R5 的名字取一个 ToolPort。

    取处是 `ctx.extras["tools"]`（名字 -> ToolPort 或裸可调用）。为什么不直接从
    T62 的 rtv 工具模块 import：那个模块**不在本轨基线里**（契约 C-R8）。
    注入式取用的好处不止于本轨能跑 —— 整合期把 T62 的真 port 注进同一个键，
    六个 skill 一个字都不用改。

    取不到就抛，**不自动造一个 stub**：自动兜底会让「忘了注册工具」变成
    「悄悄用了一个空实现」，轮询次数、终态判定全部失真，而表面上一路绿灯
    （口径同 `ap/_common.py::get_bank`）。
    """
    if name not in RTV_TOOLS:
        raise LookupError(f"{name!r} 不在契约 C-R5 的工具清单里：{list(RTV_TOOLS)}")
    tools = (getattr(ctx, "extras", None) or {}).get("tools") or {}
    entry = tools.get(name)
    if entry is None:
        raise LookupError(
            f"上下文里没有工具 {name!r}（已注入：{sorted(tools)}）；"
            "请在装配处传 extras={'tools': {name: port_or_callable}}")
    if isinstance(entry, ToolPort):
        return entry
    return ToolPort(
        name=name,
        purpose=f"RTV 域外部系统调用（{name}）—— 正式声明见 maos/tools/rtv.py（T62）",
        entry=entry,
        security_boundary="经 invoke_tool 调用，留 ToolInvoked 审计行",
        owner=DOMAIN,
    )


def call_tool(ctx: Any, store: Any, name: str, params: dict) -> Any:
    """调一个外部工具，落一条 ToolInvoked 审计行。

    一律走 `invoke_tool`，不直接调 `port.entry` —— 直接调就没有审计行，
    出事之后查不到是谁、什么参数、跑了多久（`maos/tools/port.py` 抬头）。
    """
    return invoke_tool(tool_port(ctx, name), params,
                       store=store, extras=tool_extras(ctx))


# ---------------------------------------------------------------------------
# 八、权威守卫（契约 C-R2 / C-R3；整合期换成 T61 的 maos/domain/rtv/guard.py）
# ---------------------------------------------------------------------------
#: 全系统唯一写得进 `AUTHORITATIVE_STATES` 的 actor。值是 skill 的 `contract.name` ——
#: skill 把自己的名字递进来，不是各处自报家门的字符串常量（各写一份就会漂，
#: 而漂的症状是守卫悄悄放行了别人）。
AUTHORITATIVE_WRITER = "rtv.observe"

#: 🔴 **两个**权威终态，这是本域相对 ap / refund 域的增量：
#: 「供应商认不认这笔退货」与「钱到没到账」是两件独立的事，由两个不同的外部系统
#: 说了算，MAOS 一个都写不了。
AUTHORITATIVE_STATES = frozenset({"credited", "settled"})

#: 权威终态 -> 该终态要求回执里的 `observed_state` 取值。与 AUTHORITATIVE_STATES
#: **同增同减**：加一个权威终态就必须在这里给出它的判据，漏配不放行（见第 ④ 道）。
#:
#: 🔴 `acknowledged` 绝不许进 `credited` 那一格：那是「供应商收到退货了」，
#: 不是「供应商认了这笔钱」。两者在回执里字段齐全、形状一样，但差着一次会计确认 ——
#: 口径同 ap 域拒收 `accepted`。
AUTHORITATIVE_RECEIPT_STATE: dict[str, frozenset[str]] = {
    "credited": frozenset({"issued"}),      # 供应商**开出**贷项通知单
    "settled": frozenset({"settled"}),      # AP 调整凭单**核销**/钱到账
}

#: 业务状态机（**不是** Task 状态机，铁律 9）。逐字照抄契约 C-R2。
#: `rtv_case.biz_status` 是业务对象自己的字段，`maos/contracts/states.py` 一个
#: 新状态、一条新迁移都没加。
BIZ_STATUS_FLOW: dict[str, tuple[str, ...]] = {
    "received":    ("disposed", "rejected"),
    "disposed":    ("shipped", "rejected", "compensated"),
    "shipped":     ("credited", "compensated"),
    "credited":    ("settled", "compensated"),
    "settled":     (),
    "rejected":    (),
    "compensated": (),
}

INITIAL_STATUS = "received"

#: 与 ap / refund / claim 域共用同一个事件类型名（契约 C-R3）。对审计与 Trace 来说
#: 「有人试图越权写权威终态」是同一件事，按 `detail.domain` 区分是哪个域。
#: 另起一个名字会让「这个 Plan 有没有越权写入」要查两处，漏一处就是假绿。
VIOLATION_EVENT = "AuthoritativeFactViolation"

#: 业务状态变更事件。
STATUS_EVENT = "RtvBizStatusChanged"

#: 同一个案号上来了一份业务字段不一样的受理。与 VIOLATION_EVENT 分开：
#: 越权写入是「你不该写」，这里是「你写的和库里那份不是同一件事」。
CASE_CONFLICT_EVENT = "RtvCaseIdentityConflict"

#: 权威终态 -> 回执至少要有的字段。缺任何一个都算「没有回执」。
#:
#: `credited` 那组里的 `credit_note_id` 是供应商侧的单号，与 ap 域要求
#: `bank_reference` 同一条理由：「供应商说认了」是一句话，「供应商给了一个
#: 贷项通知单号」才是一张能拿去对账的凭据。`settled` 那组里的 `ap_reference` 同理。
_RECEIPT_REQUIRED: dict[str, tuple[str, ...]] = {
    "credited": ("credit_note_id", "observed_state", "amount_credited", "issued_at"),
    "settled": ("adjustment_id", "observed_state", "ap_reference"),
}

#: 判定「这是不是同一件事的重放」要逐字段比对的业务字段。
#: `biz_status` / `return_action` / `created_at` **不在里面**：前两个是建案之后被
#: 推进/裁定的结果，后一个是第一次受理的时刻 —— 拿它们比会让每次正常重放都判成冲突。
_CASE_IDENTITY_FIELDS = ("supplier_id", "po_id", "po_version", "gr_id",
                         "amount_claimed", "currency", "plan_id")


class AuthoritativeFactViolation(RuntimeError):
    """非权威写入方试图写权威终态，或权威终态没有回执兜底。

    定义在本模块而不是 `contracts/` —— contracts 是冻结面，且这是 RTV 域自己的
    业务规则，不是内核契约。**也不复用 ap / refund 域那两个同名类**：
    各域的守卫互不 import 是域可移植性论证的一部分（契约 C-R9）。
    """


class BizStatusTransitionError(ValueError):
    """业务状态迁移不在 `BIZ_STATUS_FLOW` 里。"""


class CaseIdentityConflict(ValueError):
    """同一个 `(tenant_id, case_id)` 上来了一份**业务字段不一样**的受理。"""


def _require_invocation_id(invocation_id: str) -> str:
    if not invocation_id:
        raise ValueError("invocation_id 不许为空：它是 rtv_case 每一次写入的 actor 锚点")
    return invocation_id


def _log_violation(store: Any, *, plan_id: str, tenant_id: str, case_id: str,
                   attempted: str, actor: str, invocation_id: str, why: str) -> None:
    """越权写入**不静默失败**：抛异常 + 落一条事件。

    「系统拒绝了一次越权写入」本身就是要拿给评委看的证据，吞掉就没了。
    """
    store.append_event_log({
        "plan_id": plan_id,
        "event_type": VIOLATION_EVENT,
        "reason": why,
        "detail": {"domain": DOMAIN, "tenant_id": tenant_id, "case_id": case_id,
                   "attempted": attempted, "actor": actor,
                   "invocation_id": invocation_id,
                   "authoritative_writer": AUTHORITATIVE_WRITER},
    })


def get_case(store: Any, tenant_id: str, case_id: str) -> dict | None:
    rows = query(store, "SELECT * FROM rtv_case WHERE tenant_id=? AND case_id=?",
                 (tenant_id, case_id))
    return rows[0] if rows else None


def _identity_of(row: dict) -> dict:
    """把库里那一行折成与 `create_case` 入参同一个形状，好逐字段比。

    必须过一遍类型归一：sqlite 的 INTEGER 回来是 int，调用方递进来的可能是 str；
    金额过 `money_str` 折成两位小数 —— 不归一会把「1200 与 1200.00」判成冲突，
    幂等当场退化成「每次重跑都报冲突」。
    """
    return {
        "supplier_id": str(row["supplier_id"]),
        "po_id": str(row["po_id"]),
        "po_version": int(row["po_version"]),
        "gr_id": str(row["gr_id"]),
        "amount_claimed": money_str(row["amount_claimed"]),
        "currency": str(row["currency"]),
        "plan_id": str(row["plan_id"]),
    }


def create_case(store: Any, *, tenant_id: str, case_id: str, supplier_id: str,
                po_id: str, po_version: int, gr_id: str, amount_claimed: Any,
                plan_id: str, actor_skill: str, invocation_id: str,
                currency: str = "CNY") -> dict:
    """建一个 rtv_case，落 `received`。本表唯一的插入口径，**且是幂等的**。

    `biz_status` 不接受调用方指定 —— 想直接建成 credited / settled 的路必须从一
    开始就不存在，否则守卫只挡得住 update，挡不住 insert。
    `return_action` 同理**恒为空串**：受理的人不该替裁定的人拍板（契约 C-R1）。

    幂等语义（受理会被返工重跑，而主键是 `(tenant_id, case_id)`）：
      · 案号已在库 + 业务字段逐字段相同 -> 一个字节都不写，返回既有那一行。
        所以这里既不能 `INSERT OR REPLACE` 也不能 `ON CONFLICT DO UPDATE`：
        那两种写法会让一次重跑把已经推进到 shipped / credited 的案子**静悄悄**
        倒回 `received`，比裸 INSERT 抛异常坏得多。
      · 案号已在库 + 任一业务字段不同 -> 落一条冲突事件并抛 `CaseIdentityConflict`。
        悄悄收下新金额，则库里的 `amount_claimed` 是对账真正拿去比的输入，
        等于把对账绕过去；悄悄丢弃，则调用方拿到一份和自己递进来的对不上的案子。
    """
    _require_invocation_id(invocation_id)
    incoming = {
        "supplier_id": str(supplier_id),
        "po_id": str(po_id),
        "po_version": int(po_version),
        "gr_id": str(gr_id),
        "amount_claimed": money_str(amount_claimed),
        "currency": str(currency),
        "plan_id": str(plan_id),
    }
    conn = _conn(store)
    with lock_of(store):
        conn.execute(
            "INSERT INTO rtv_case (tenant_id, case_id, supplier_id, po_id, po_version,"
            " gr_id, return_action, amount_claimed, currency, biz_status, plan_id,"
            " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT (tenant_id, case_id) DO NOTHING",
            (tenant_id, case_id, incoming["supplier_id"], incoming["po_id"],
             incoming["po_version"], incoming["gr_id"], "",
             incoming["amount_claimed"], incoming["currency"], INITIAL_STATUS,
             incoming["plan_id"], now_iso()))
        conn.commit()

    case = get_case(store, tenant_id, case_id)
    if case is None:
        raise RuntimeError(f"create_case 之后读不到 case：tenant={tenant_id} case={case_id}")

    stored = _identity_of(case)
    diff = {f: (stored[f], incoming[f])
            for f in _CASE_IDENTITY_FIELDS if stored[f] != incoming[f]}
    if diff:
        store.append_event_log({
            "plan_id": incoming["plan_id"],
            "event_type": CASE_CONFLICT_EVENT,
            "reason": f"case_id 被复用，业务字段对不上：{sorted(diff)}",
            "detail": {"domain": DOMAIN, "tenant_id": tenant_id, "case_id": case_id,
                       "actor": actor_skill, "invocation_id": invocation_id,
                       "conflicts": {f: {"stored": old, "incoming": new}
                                     for f, (old, new) in diff.items()}},
        })
        detail = "；".join(f"{f}：库里 {old!r}、这次 {new!r}"
                          for f, (old, new) in sorted(diff.items()))
        raise CaseIdentityConflict(
            f"case={case_id}（tenant={tenant_id}）已经存在，业务字段对不上：{detail}。"
            "受理重跑只在业务字段逐字段相同时幂等；对不上说明这不是同一笔退货的重放")
    return case


def set_return_action(store: Any, tenant_id: str, case_id: str, action: str) -> None:
    """写 `rtv_case.return_action`。**只给 `rtv.dispose` 用**，不推进业务状态。

    单独开这一条而不是让 dispose 走 `execute()`：`rtv_case` 的写入被
    `_guarded()` 挡着（那正是本文件要的），而裁定结论确实要落在案子头上。
    它碰不到 `biz_status` —— 权威守卫那条线一个字都没松。
    """
    if action not in RETURN_ACTIONS:
        raise ValueError(f"处置类型只能是 {list(RETURN_ACTIONS)}，实际 {action!r}")
    conn = _conn(store)
    with lock_of(store):
        conn.execute("UPDATE rtv_case SET return_action=? WHERE tenant_id=? AND case_id=?",
                     (action, tenant_id, case_id))
        conn.commit()


def _insert_receipt(conn: Any, new_status: str, *, tenant_id: str, case_id: str,
                    observation: dict, invocation_id: str, actor_skill: str) -> None:
    """把回执落进它自己那张表。**与状态更新同一个事务**（调用方持锁）。

    两个权威终态各有各的回执表，因为它们是两个不同外部系统给的两张不同凭据：
      credited -> `credit_note`（供应商开的贷项通知单）
      settled  -> `rtv_settlement_observation`（AP 侧的调整凭单核销）
    合并成一张「通用观察表」会让「谁说的」这件事退化成一个 type 列，
    而两张凭据的字段本来就对不上（单号 vs 凭单引用、开票时间 vs 观察时间）。
    """
    observed_at = str(observation.get("observed_at") or now_iso())
    if new_status == "credited":
        conn.execute(
            "INSERT OR REPLACE INTO credit_note (tenant_id, case_id, credit_note_id,"
            " document_type, amount_credited, currency, issued_at, observed_at,"
            " observed_by, invocation_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (tenant_id, case_id, str(observation["credit_note_id"]),
             str(observation.get("document_type") or "381"),
             money_str(observation["amount_credited"]),
             str(observation.get("currency") or "CNY"),
             str(observation["issued_at"]), observed_at, actor_skill, invocation_id))
        return
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM rtv_settlement_observation"
        " WHERE tenant_id=? AND case_id=?", (tenant_id, case_id)).fetchone()
    seq = int(dict(row)["seq"])
    conn.execute(
        "INSERT INTO rtv_settlement_observation (tenant_id, case_id, seq, adjustment_id,"
        " observed_state, ap_reference, observed_at, observed_by, invocation_id)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (tenant_id, case_id, seq, str(observation["adjustment_id"]),
         str(observation["observed_state"]), str(observation["ap_reference"]),
         observed_at, actor_skill, invocation_id))


def update_biz_status(store: Any, tenant_id: str, case_id: str, new_status: str,
                      actor_skill: str, invocation_id: str, *,
                      observation: dict | None = None, reason: str = "",
                      poll_count: int = 0) -> dict:
    """`rtv_case.biz_status` 的唯一写入路径。

    四道闸，顺序不可换（每一道旁边写了为什么它必须在这个位置）：

    ① `new_status` 落在 `AUTHORITATIVE_STATES` 而 actor 不是权威写入方 -> 拒 + 留证据
    ② 递了 `observation` 却不是权威写入方 -> 拒（否则等于给别人开伪造回执的口子）
    ③ 权威终态**必须**带回执，且字段齐全（含可对账的外部单号）
    ④ 回执说的**得是这件事**：`observed_state` 必须落在该终态的判据集合里 ——
       这一道就是拒收 `acknowledged` 的地方

    `poll_count` 不进库：契约 C-R1 的两张回执表都没有这一列，而契约是冻结的。
    它落在状态变更事件的 detail 与 reason 里 —— 「终态是问出来的」这条证据仍然
    留得下，且不动契约一个字。
    """
    _require_invocation_id(invocation_id)
    case = get_case(store, tenant_id, case_id)
    plan_id = (case or {}).get("plan_id", "")

    # ① 权威闸放在最前面：case 不存在也照样记一笔越权尝试。
    #    先查存在性会让「对不存在的 case 越权写 credited」以 LookupError 收场，
    #    证据就没了 —— 而那恰恰是最该留痕的一种试探。
    if new_status in AUTHORITATIVE_STATES and actor_skill != AUTHORITATIVE_WRITER:
        _log_violation(store, plan_id=plan_id, tenant_id=tenant_id, case_id=case_id,
                       attempted=new_status, actor=actor_skill,
                       invocation_id=invocation_id,
                       why=f"{new_status} 只能由 {AUTHORITATIVE_WRITER} 写入")
        raise AuthoritativeFactViolation(
            f"{actor_skill} 试图把 case={case_id} 写成 {new_status}；"
            f"该状态的权威在外部（供应商 / AP），只有 {AUTHORITATIVE_WRITER} "
            f"观察到回执之后才写得进来")

    # ② 回执只有权威写入方递得进来，否则等于给别人开了个伪造凭据的口子。
    if observation is not None and actor_skill != AUTHORITATIVE_WRITER:
        _log_violation(store, plan_id=plan_id, tenant_id=tenant_id, case_id=case_id,
                       attempted=new_status, actor=actor_skill,
                       invocation_id=invocation_id,
                       why=f"外部回执只能由 {AUTHORITATIVE_WRITER} 提交")
        raise AuthoritativeFactViolation(
            f"{actor_skill} 递交了外部回执；回执是外部权威事实，"
            f"只有 {AUTHORITATIVE_WRITER} 能落库")

    if case is None:
        raise LookupError(f"没有这个 case：tenant={tenant_id} case={case_id}")

    cur = str(case["biz_status"])
    if new_status not in BIZ_STATUS_FLOW.get(cur, ()):
        raise BizStatusTransitionError(
            f"业务状态不许从 {cur} 迁到 {new_status}（case={case_id}）；"
            f"{cur} 的合法去向：{BIZ_STATUS_FLOW.get(cur, ()) or '无（终态）'}")

    if new_status in AUTHORITATIVE_STATES:
        # ③ 权威终态必须有回执，且字段齐全。没有回执的 credited 就是把外部状态
        #    写死为终态 —— 自己给自己开了一张贷项通知单。
        need = _RECEIPT_REQUIRED.get(new_status)
        if need is None:
            _log_violation(store, plan_id=plan_id, tenant_id=tenant_id, case_id=case_id,
                           attempted=new_status, actor=actor_skill,
                           invocation_id=invocation_id,
                           why=f"{new_status} 没有配回执字段要求")
            raise AuthoritativeFactViolation(
                f"{new_status} 在 AUTHORITATIVE_STATES 里，却没有在 _RECEIPT_REQUIRED "
                f"里给出它的回执要求；两张表必须同增同减")
        missing = [f for f in need if not (observation or {}).get(f)]
        if missing:
            _log_violation(store, plan_id=plan_id, tenant_id=tenant_id, case_id=case_id,
                           attempted=new_status, actor=actor_skill,
                           invocation_id=invocation_id,
                           why=f"外部回执缺字段 {missing}")
            raise AuthoritativeFactViolation(
                f"写 {new_status} 必须同事务附外部回执，缺字段：{missing}。"
                f"其中的外部单号是**可对账的凭据** —— 「有一张回执」不等于"
                f"「有一张能拿去对账的回执」")

        # ④ 回执还得**说的是这件事**。③ 只保证「有一张回执」，不保证那张回执说
        #    供应商认了这笔钱 —— 一条 observed_state='acknowledged' 的回执字段齐全，
        #    在 ③ 眼里与终态回执无从分辨。放过它，系统持有的就只是「供应商收到货
        #    了」，不是「供应商开了贷项通知单」，而后者才是 credited 这个词的全部
        #    含义（铁律 8）。防线必须在守卫里，不能只活在 observe 的某个 if 分支里：
        #    那种防线改一次分支顺序两层都不会响。
        allowed = AUTHORITATIVE_RECEIPT_STATE.get(new_status)
        if allowed is None:
            _log_violation(store, plan_id=plan_id, tenant_id=tenant_id, case_id=case_id,
                           attempted=new_status, actor=actor_skill,
                           invocation_id=invocation_id,
                           why=f"{new_status} 没有在 AUTHORITATIVE_RECEIPT_STATE 里配判据")
            raise AuthoritativeFactViolation(
                f"{new_status} 在 AUTHORITATIVE_STATES 里，却没有在 "
                f"AUTHORITATIVE_RECEIPT_STATE 里给出回执判据；两张表必须同增同减")
        seen = str((observation or {}).get("observed_state"))
        if seen not in allowed:
            _log_violation(store, plan_id=plan_id, tenant_id=tenant_id, case_id=case_id,
                           attempted=new_status, actor=actor_skill,
                           invocation_id=invocation_id,
                           why=f"回执 observed_state={seen!r}，不在 {new_status} 的判据 "
                               f"{sorted(allowed)} 里")
            raise AuthoritativeFactViolation(
                f"写 {new_status} 的回执说的是 {seen!r}，不是 {sorted(allowed)}；"
                f"外部权威没这么说就不许收口")

    conn = _conn(store)
    with lock_of(store):
        try:
            if observation is not None:
                _insert_receipt(conn, new_status, tenant_id=tenant_id, case_id=case_id,
                                observation=observation, invocation_id=invocation_id,
                                actor_skill=actor_skill)
            conn.execute("UPDATE rtv_case SET biz_status=? WHERE tenant_id=? AND case_id=?",
                         (new_status, tenant_id, case_id))
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    store.append_event_log({
        "plan_id": plan_id,
        "event_type": STATUS_EVENT,
        "from_state": cur, "to_state": new_status, "reason": reason,
        "detail": {"domain": DOMAIN, "tenant_id": tenant_id, "case_id": case_id,
                   "actor": actor_skill, "invocation_id": invocation_id,
                   "observation_attached": observation is not None,
                   "poll_count": int(poll_count)},
    })
    return get_case(store, tenant_id, case_id)      # type: ignore[return-value]


#: 「外部明确说了一件坏事」的事件类型：供应商 disputed、AP 凭单 voided。
ADVERSE_EVENT = "RtvAdverseObservation"


def record_adverse_observation(store: Any, *, tenant_id: str, case_id: str,
                               system: str, observed_state: str, actor_skill: str,
                               invocation_id: str, detail: dict | None = None) -> None:
    """把一次**明确的坏消息**留痕：供应商回 disputed、AP 凭单 voided。

    为什么落事件而不落回执表：契约 C-R1 的两张回执表都只为**成功凭据**定义 ——
    `credit_note` 的每一列都在描述一张开出来的贷项通知单，`rtv_settlement_observation`
    的 `adjustment_id` 在凭单作废时未必给得出。硬塞一行进去要么留一堆空字段，
    要么得动冻结的表结构，两条都不行。

    但**不许不留痕**：「供应商说不认这笔」只活在日志里，等于系统没有观察到它。
    事件日志是这条链路上已有的、可查询的留痕面（口径同 `_log_violation`）。

    非权威写入方一律拒：这是一条外部事实的记录，与回执同一条规矩。
    """
    if actor_skill != AUTHORITATIVE_WRITER:
        raise AuthoritativeFactViolation(
            f"{actor_skill} 试图记录一条外部观察；只有 {AUTHORITATIVE_WRITER} 能记")
    _require_invocation_id(invocation_id)
    case = get_case(store, tenant_id, case_id)
    store.append_event_log({
        "plan_id": (case or {}).get("plan_id", ""),
        "event_type": ADVERSE_EVENT,
        "reason": f"{system} 侧观察到 {observed_state}",
        "detail": {"domain": DOMAIN, "tenant_id": tenant_id, "case_id": case_id,
                   "system": system, "observed_state": observed_state,
                   "actor": actor_skill, "invocation_id": invocation_id,
                   **(detail or {})},
    })


def credit_notes_of(store: Any, tenant_id: str, case_id: str) -> list[dict]:
    return query(store, "SELECT * FROM credit_note WHERE tenant_id=? AND case_id=?"
                        " ORDER BY observed_at", (tenant_id, case_id))


def settlement_observations_of(store: Any, tenant_id: str, case_id: str) -> list[dict]:
    return query(store, "SELECT * FROM rtv_settlement_observation WHERE tenant_id=?"
                        " AND case_id=? ORDER BY seq", (tenant_id, case_id))


def lines_of(store: Any, tenant_id: str, case_id: str) -> list[dict]:
    return query(store, "SELECT * FROM rtv_line WHERE tenant_id=? AND case_id=?"
                        " ORDER BY line_no", (tenant_id, case_id))


def next_attempt(store: Any, table: str, tenant_id: str, case_id: str) -> int:
    """下一个 attempt 号。裁定与对账都**保留历史**，返工重跑不覆盖旧结论。

    表名是本模块内部拼进去的常量，不来自入参之外的任何地方 —— 调用点只传
    `rtv_disposition` / `rtv_reconciliation` 两个字面量之一，这里再挡一道。
    """
    if table not in ("rtv_disposition", "rtv_reconciliation", "rtv_compensation_record"):
        raise ValueError(f"attempt 号只对裁定/对账/补偿三张表有意义，实际 {table!r}")
    column = "seq" if table == "rtv_compensation_record" else "attempt"
    rows = query(store, f"SELECT COALESCE(MAX({column}), 0) + 1 AS n FROM {table}"
                        " WHERE tenant_id=? AND case_id=?", (tenant_id, case_id))
    return int(rows[0]["n"]) if rows else 1


def attach_business_ref(store: Any, *, plan_id: str, task_id: str, object_table: str,
                        object_id: str, object_version: int = 0,
                        purpose: str = "") -> dict:
    """把一个 Task 挂到一个业务对象上 —— **只存引用，不存副本**。

    存副本会立刻产生第二份事实：业务对象改了，Task 里那份不会跟着改，
    而下游分不清哪份是真的。引用只指路，读的时候一定读到当前那一份。

    契约 C-R1 的 `rtv_business_ref` **没有主键**，所以幂等靠先删后插做 ——
    `INSERT OR REPLACE` 在无主键表上不去重，返工重跑会攒出一堆一模一样的行，
    而「这个 Plan 碰了哪些业务对象」会随重跑次数漂。
    """
    row = {"plan_id": plan_id, "task_id": task_id, "object_table": object_table,
           "object_id": object_id, "object_version": int(object_version),
           "purpose": purpose, "created_at": now_iso()}
    execute(store,
            "DELETE FROM rtv_business_ref WHERE plan_id=? AND IFNULL(task_id,'')=?"
            " AND object_table=? AND object_id=?",
            (plan_id, task_id or "", object_table, object_id))
    execute(store,
            "INSERT INTO rtv_business_ref (plan_id, task_id, object_table, object_id,"
            " object_version, purpose, created_at) VALUES (?,?,?,?,?,?,?)",
            (row["plan_id"], row["task_id"], row["object_table"], row["object_id"],
             row["object_version"], row["purpose"], row["created_at"]))
    return row


def list_business_refs(store: Any, *, plan_id: str, task_id: str | None = None) -> list[dict]:
    if task_id is None:
        return query(store, "SELECT * FROM rtv_business_ref WHERE plan_id=?"
                            " ORDER BY object_table, object_id", (plan_id,))
    return query(store, "SELECT * FROM rtv_business_ref WHERE plan_id=? AND task_id=?"
                        " ORDER BY object_table, object_id", (plan_id, task_id))
