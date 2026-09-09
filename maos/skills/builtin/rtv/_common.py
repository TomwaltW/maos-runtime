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
    # 整合后（2026-09-10）T61 的域层必在：本轨唯一的跨轨 import 挪到第八节那一行，
    # 与 guard 合成一句。fallback 那条路留着没删（见 BACKLOG），但已经走不到。
    global _domain_objects, _domain_probed
    if not _domain_probed:
        _domain_probed = True
        _domain_objects = _objects
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
# 八、权威守卫 —— 整合后直接用 T61 / T65 的 maos/domain/rtv/guard.py
# ---------------------------------------------------------------------------
# 本节原是本轨自带的同形状守卫（契约 C-R2 / C-R3），按文件抬头第 2 条的约定，
# 整合期（2026-09-10）换成 T61 那份。名字原样保留：六个 skill 与测试引用的都是 `C.xxx`，
# 实现全部来自 guard —— 三张受保护表（rtv_case / credit_note / rtv_settlement_observation）
# 从此全仓只有 guard.py 写得动（`test_rtv_guard.py::test_no_source_file_writes_...`）。
#
# 唯一的接线差异：guard 把 return_action 与 disposed 做成同一次写入（`DispositionRequired`），
# 原来的 `set_return_action()` 旁路随之取消 —— `rtv.dispose` 改为传 `return_action=`。
from maos.domain.rtv import guard as _guard, objects as _objects

AUTHORITATIVE_WRITER = _guard.AUTHORITATIVE_WRITER
AUTHORITATIVE_STATES = _guard.AUTHORITATIVE_STATES
AUTHORITATIVE_RECEIPT_STATE = _guard.AUTHORITATIVE_RECEIPT_STATE
BIZ_STATUS_FLOW = _guard.BIZ_STATUS_FLOW
INITIAL_STATUS = _guard.INITIAL_STATUS
VIOLATION_EVENT = _guard.VIOLATION_EVENT
#: guard 里叫 BIZ_STATUS_EVENT，值相同（"RtvBizStatusChanged"）；本模块沿用旧名。
STATUS_EVENT = _guard.BIZ_STATUS_EVENT
CASE_CONFLICT_EVENT = _guard.CASE_CONFLICT_EVENT
_CASE_IDENTITY_FIELDS = _guard._CASE_IDENTITY_FIELDS

AuthoritativeFactViolation = _guard.AuthoritativeFactViolation
BizStatusTransitionError = _guard.BizStatusTransitionError
CaseIdentityConflict = _guard.CaseIdentityConflict
DispositionRequired = _guard.DispositionRequired

_require_invocation_id = _guard._require_invocation_id
_log_violation = _guard._log_violation
get_case = _guard.get_case
create_case = _guard.create_case


def update_biz_status(store: Any, tenant_id: str, case_id: str, new_status: str,
                      actor_skill: str, invocation_id: str, *,
                      observation: dict | None = None, reason: str = "",
                      poll_count: int = 0, return_action: str = "") -> dict:
    """`rtv_case.biz_status` 的唯一写入路径 —— 直通 guard，签名保持本模块原样。

    `poll_count` 不进库（契约 C-R1 的回执表没有这一列），只进审计 detail：
    「终态是问出来的」这件事要留证据。
    """
    return _guard.update_biz_status(
        store, tenant_id, case_id, new_status, actor_skill, invocation_id,
        observation=observation, return_action=return_action, reason=reason,
        poll_count=int(poll_count))


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
