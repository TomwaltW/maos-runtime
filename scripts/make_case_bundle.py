#!/usr/bin/env python3
"""单案例端到端证据束 —— 同一条案例从入口跑到退款到账，四条路径各留一份可核验的束。

    python3 scripts/make_case_bundle.py --all-paths
    python3 scripts/make_case_bundle.py --path happy
    python3 scripts/make_case_bundle.py --path happy --live-model
    python3 scripts/make_case_bundle.py --path gateway_fail --room-transcript <某.md>

## 与 ``scripts/make_evidence.py`` 的分工

那个跑的是**场景**（`maos/flows/scenario_*.py`），一场一个子进程，产出
``evidence/scenario-<N>/``；它回答「这套系统的七种能力各自长什么样」。

本脚本跑的是**一条案例**（`scenarios/refund/cases/case_real_01.json`），四条路径
各一次，产出 ``evidence/case-real-01/<path>/``；它回答评委第一眼要问的那个问题：
**同一份脱敏真实退款需求，从 IM 入口进来，一路到钱退没退，中间谁批的、返过几次工、
每一步的依据在哪一行**。

三处实现上的取舍，各有一个不能换的理由：

1. **进程内跑，不起子进程。** `make_evidence.py` 起子进程是为了把 `:memory:` 库换成
   文件库；本脚本要在同一个进程里接圆桌与计划审批停靠点（两者都要拿着 store 与
   ControlPlane 的实例），起子进程就只能靠命令行参数隔空传话。换库仍用同一套手法：
   把 `maos.flows.common.SqliteStore` 换成绑定了文件路径的同一个类，仓库里一个字节不改。
2. **证据卫生学一律 import，不抄。** 出处首行、出口脱敏、哨兵反查、临时目录攒齐再
   `os.replace` —— 全部来自 `scripts/make_evidence.py`。抄一份的后果是两套口径迟早
   分叉，而分叉那天两边都不报错（C-7 的反例）。
3. **每条路径一个独立的库文件，跑完连库一起进束。** 四条路径共库的话，失败路径的
   「全库无到账观察」这类判据会读到顺利路径的数据。

## `--live-model` 与 verify 的口径

真模型那一跑单独出到 ``<path>-live/``，`INDEX.json` 标 ``model_mode: live``；
**verify 只认 Scripted 束**（`scripts/verify.py::load_cases` 跳过 `-live` 目录并在
输出里点名）。理由是可复现：真模型每跑一次说的话都不一样，拿它当重放比对的基准，
第 4 项 trace-tree 会在没有任何 bug 的情况下红。

铁律 3 / 铁律 6 在这里的落点与 `make_evidence.py` 逐字相同：每个文件首行
``# generated at <ISO8601> from <sha>``；出口脱敏 + 哨兵反查两道都走，
反查命中即销毁整个目录并失败 —— 宁可目录缺一半，也不许留下半份让人误以为跑通了的产物。
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import io
import json
import logging
import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from maos.agents.refund import ROLE_PAYMENT  # noqa: E402
from scripts.make_evidence import (  # noqa: E402
    EvidenceError,
    connect_ro,
    header_line,
    is_unverifiable,
    pin_sha,
    redact,
    scan_for_secrets,
    secret_values,
    table_names,
    write_bundle,
    write_json,
    write_text,
)

#: 缺省案例包与缺省输出根。两者都可由命令行改，改了不许再往仓库 `evidence/` 里写。
DEFAULT_CASE = os.path.join(ROOT, "scenarios", "refund", "cases", "case_real_01.json")
DEFAULT_OUT = os.path.join(ROOT, "evidence", "case-real-01")

#: `gateway_fail` 注入的网关码，**两段**：先 `40005`（`retriable=True` /
#: `outcome=failed`，业务系统繁忙），机器按可重试重发一次；第二次网关改口
#: **卖家余额不足**（`retriable=False` / `outcome=failed`），终态，转人工。
#:
#: 这条路径从前只有第二段。第一段被一个**现存缺陷**逼退过：同渠道重试时
#: `refund_request` 是**一行**、版本原地 1->2->3（`payment_execute.py` 的
#: `next_version` + `INSERT OR REPLACE`），而每次重试都按当时的版本挂一条
#: `business_ref`；那里的 DELETE 当时只摘 `object_id` 不同的旧引用，于是 v1/v2
#: 两条引用指向一个已经变成 v3 的对象，`resolve_business_ref` 按 `AND version=?`
#: 收窄当场悬空，`verify.py` 第 2 项判负。**T124 把 DELETE 的判据改成
#: `(object_id<>? OR object_version<>?)` 之后这条路通了**，可重试码随之改回来。
#:
#: 为什么非要第一段：评委第一条要「返工 / HITL Trace」，而单案例束里 `REWORK`
#: 原先一条都没有。`40005` 是唯一能长出它的那一格 —— 实跑定的码，不是推的：
#:   · `40005`（retriable + failed）-> 闸判 `GW_REPLAN_CHANNEL`（blocker），
#:     而 `custom_case` 基线不接 replanner，于是落到
#:     `AWAITING_REVIEW -> REWORK [gate_rework]` -> `REWORK -> PENDING [requeue]`。
#:   · `ACQ.SYSTEM_ERROR`（retriable + unknown）-> 闸判 `GW_QUERY_FIRST`，
#:     严重度是 **info 不挡闸**（`gate.py::GATEWAY_SEVERITY`），实跑直接 `gate_pass`、
#:     `payment.observe` 轮询两次就问出 settled —— 一条 `REWORK` 都长不出来。
#: 取值出处 `maos/tools/gateway_codes.py::RATE_LIMITED` 与 `SELLER_BALANCE_NOT_ENOUGH`。
GATEWAY_FAIL_CODE = "40005"

#: 第一段重发几次之后改口。取 1：演到「机器自己试过一次」就够，多试只是自旋 ——
#: 而「绝不自旋」正是这条路径想证的另一半（`control_plane._should_replan` 的注释）。
GATEWAY_FAIL_TIMES = 1

#: 改口之后的终态码。它决定了后面整段人工剧情：remedy 原文「商户账户充值后重新发起」
#: -> 闸判 `GW_HUMAN_TERMINAL` -> 转人工 -> 人在付款闸上拒签 -> 补偿工单。
#: `MANUAL_EVIDENCE` 与 `RESOLUTION_KIND` 都是按它写的，换码要一起换。
GATEWAY_FAIL_THEN_CODE = "ACQ.SELLER_BALANCE_NOT_ENOUGH"

#: 补偿工单的承接岗与线下凭证。凭证摘要的**第一个词**是凭证引用（渠道流水号 /
#: 外部工单号）—— 它是这份人工凭证作为外部事实的出处，没有出处的凭证只是一句断言。
TICKET_ROLE = "payment_ops"
MANUAL_EVIDENCE = "OPS-20260910-0417 线下核对：商户余额不足，这笔至今未退出，转财务充值后重发"

#: 关单结论取 `not_settled`（线下核对之后确认这笔**确实没退出去**），有两个理由，
#: 第二个是硬的：
#:
#: 1. 剧情上它才对得上注入的码 —— 「卖家余额不足」的 remedy 原文是「商户账户充值后
#:    重新发起」，钱在充值之前不可能到账。
#: 2. 关成 `settled` 会造出一个系统自相矛盾的状态：案子已 `compensated`（补偿收口的
#:    终态），却因为回填了一条 settled 观察 + 十类引用齐全，被
#:    `kb/guardrails.classify_case` 判成 `history_case/success`（那一支只看
#:    business_success + evidence_complete + 有 settled 观察，**不看 biz_status**），
#:    而 `verify.py` 第 7 项按 `biz_status ∈ {settled}` 判，当场判它「追不到成功收口的
#:    真实 case」。两侧口径不一致，已记 docs/BACKLOG.md —— 本轨不改别人的面，绕开它。
RESOLUTION_KIND = "not_settled"

#: 契约 §G 的 8 个 skill，**逐字**。顺序即案子上的先后。
CONTRACT_SKILLS = (
    "refund.intake", "refund.evidence_check", "policy.match", "refund.risk_screen",
    "finance.settle", "payment.execute", "payment.observe", "notify.customer",
)

#: 失败路径上另附的两个。它们不在 8 个里 —— 顺利的案子本来就不该出现补偿。
COMPENSATION_SKILLS = ("refund.compensate", "refund.compensation_close")

#: 四条路径都必须出现的事件类型。**`TaskTransition` 在库里的真名是
#: `StateTransition`**（带非空 `task_id` 的那些就是任务迁移）：契约 §F 说的是概念名，
#: 这里对齐库里的字面量，否则守卫会去找一个从来不存在的字符串然后永远判缺。
BASE_REQUIRED_EVENTS = ("KbRetrieved", "SkillInvoked", "PlanTransition",
                        "StateTransition", "CaseOutcomeComputed")

#: 圆桌（T113）与 Planner 建议（T119）的事件：**有就带上、没有不算错**。
#: 两者都还在各自的轨上跑，基线上不落库；写死成「必须有」会让本束在整合之前恒红。
OPTIONAL_EVENTS = ("RoundtableRound", "RoundtableSeatSpoke", "RoundtableVerdict",
                   "PlanAdvised")

#: 四条路径。每条只是一组入参 —— 路径之间的差别必须全在数据与开关上，
#: 不许出现「happy 走这段代码、drift 走那段」，否则四条路径证明的不是同一个流程。
#: 每条路径**另外**必须出现的事件，与它演的那件事一一对应。
#: 逐路径写而不是四条共用一份清单：漂移那条按设计一个业务状态都不改（铁律 8），
#: 共用清单会让它恒缺 `RefundBizStatusChanged` —— 而**只会红不会绿的守卫等于没写**。
PATHS: dict[str, dict] = {
    "happy": {
        "title": "顺利到账：计划获批 -> 主管放行核算 -> 网关退款 -> 观察到 settled",
        "approve": True, "fail_with": None, "drift": False,
        "plan_approval": "approve", "compensate": False,
        "required_events": ("PlanApproved", "RefundBizStatusChanged"),
    },
    "drift": {
        "title": "执行前发现外部改单：付款前那一步报漂移，核算停在 BLOCKED 等人",
        "approve": True, "fail_with": None, "drift": True,
        "plan_approval": "approve", "compensate": False,
        # 漂移**不产生**业务状态迁移：停下来问人不是一个新的业务状态（铁律 8/9）。
        "required_events": ("PlanApproved", "SnapshotDrift"),
    },
    "gateway_fail": {
        "title": "网关退款失败：机器返工重发一次 -> 网关改口终态失败 -> 人在付款闸上拒签"
                 " -> 开补偿工单 -> 派单 -> 线下凭证回填观察",
        "approve": True, "fail_with": GATEWAY_FAIL_CODE, "drift": False,
        # 「失败 N 次后改判」的注入。写在路径配置上而不是案例 JSON 里：它是这条
        # **路径**要演的时序，不是这一单本身的属性（换个案子照样要演）。
        # 两个键原样进 `payload["gateway"]`，见 `_gateway_retry_injection`。
        "gateway_retry": {"fail_times": GATEWAY_FAIL_TIMES,
                          "then_fail_with": GATEWAY_FAIL_THEN_CODE},
        "plan_approval": "approve", "compensate": True,
        # 核算照批，**付款那一步不放行**：钱没退出去，人不能签「这一步完成了」。
        # 这不是演出效果 —— 签了的话 Plan 收在 DONE，而一个 DONE 的计划配一个
        # 「钱没到账」的结局，正是评委那句「所有 Agent 都回复完成不代表业务成功」
        # 指的病。这条路径演的是它的反面：人拒签，Plan 如实 FAILED。
        "reject_roles": (ROLE_PAYMENT,),
        "required_events": ("PlanApproved", "RefundBizStatusChanged",
                            "CompensationExecuted", "CompensationAssigned",
                            "CompensationResolved"),
    },
    "reject": {
        "title": "两级驳回：计划先被驳回一次，放行后主管在核算闸上驳回这一单",
        "approve": False, "fail_with": None, "drift": False,
        "plan_approval": "reject_then_approve", "compensate": False,
        "required_events": ("PlanRejected", "PlanApproved", "RefundBizStatusChanged"),
    },
}

#: 计划审批停靠点上那个人。**写死**：CLI 代跑人的那一半，两次跑输出一致
#: （口径同 `custom_case.APPROVER`）。真房间里这个名字来自 Matrix 的发送者。
OPERATOR = "沈思锴（after_sales_supervisor）"
PLAN_FEEDBACK = "先说清这一单的风险档位与承接岗，再谈放行"

log = logging.getLogger("maos.make_case_bundle")


# ---------------------------------------------------------------------------
# 跑一条路径
# ---------------------------------------------------------------------------
class _Tee(io.StringIO):
    """既进屏幕又进 run.log。

    只进 buffer 的话，跑一次十几秒屏幕上一片空白，人分不清是慢还是挂了；
    只进屏幕的话，`run.log` 就是空的 —— 而那份日志是这一束里唯一记着
    「状态迁移按什么顺序发生」的人类可读证据。
    """

    def __init__(self, mirror) -> None:                        # noqa: ANN001
        super().__init__()
        self._mirror = mirror

    def write(self, text: str) -> int:                         # noqa: D102
        self._mirror.write(text)
        return super().write(text)

    def flush(self) -> None:                                   # noqa: D102
        self._mirror.flush()


class _QuietWriter:
    """只进 buffer、不进屏幕的写入口。

    logging 那一路非走它不可：`basicConfig` 装的 handler 抓着**真的** stderr
    （它在 `redirect_stderr` 之前就构造好了），日志因此已经上了屏幕。再让本模块的
    handler 经 `_Tee` 镜像一次，屏幕上每条日志就会出现两遍 —— 而两遍长得一模一样，
    读的人会以为某一步真的跑了两次。
    """

    def __init__(self, buf: io.StringIO) -> None:
        self._buf = buf

    def write(self, text: str) -> int:
        return io.StringIO.write(self._buf, text)

    def flush(self) -> None:
        return None


@contextlib.contextmanager
def _captured():
    """把这一跑的 stdout / stderr / logging 全收进一份日志。"""
    buf = _Tee(sys.stdout)
    handler = logging.StreamHandler(_QuietWriter(buf))
    handler.setFormatter(logging.Formatter("%(levelname)-5s %(name)-12s %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            yield buf
    finally:
        root.removeHandler(handler)


@contextlib.contextmanager
def _file_backed_store(db_path: str):
    """把 `flows/common.build()` 建的 `:memory:` 库换成 ``db_path`` 这个文件库。

    手法与 `make_evidence.py::run_child` 同一套，理由也一样：`flows/**` 是别轨的面，
    一个字节都不动，落库位置由证据生成器自己提供。跑完复原 —— 不复原的话同一进程里
    后面几条路径会全部写进第一条路径的库。
    """
    import maos.flows.common as common
    from maos.core.store import SqliteStore

    seen: list = []
    original = common.SqliteStore

    def tracked(*args, **kwargs):                              # noqa: ANN001
        store = functools.partial(SqliteStore, db_path)(*args, **kwargs)
        seen.append(store)
        return store

    common.SqliteStore = tracked                               # type: ignore[assignment]
    try:
        yield seen
    finally:
        common.SqliteStore = original                          # type: ignore[assignment]


# ---------------------------------------------------------------------------
# PG 束（T126）：业务对象落 PolarDB / PostgreSQL，控制面照旧本地 SQLite
# ---------------------------------------------------------------------------
#: `--domain-backend postgres` 时业务表落在哪个库。缺省 sqlite，缺省路径逐字节不变。
#:
#: **为什么不走 `_file_backed_store`**：那层猴补丁是为 SQLite 写的 —— 它把
#: `common.SqliteStore` 换成绑了文件路径的同一个类，解决的是「`:memory:` 跑完就没了」。
#: PG 这边要换的不是控制面落哪个文件，而是**业务域那 16 张表落哪个库**，两件事
#: 正交：PG 束里控制面仍然是那份 `maos.db`（`docs/architecture.md` §5 的口径，
#: 也是 `docs/phases/phase-10.md` §8 明确不做的那条）。硬塞进同一个补丁的后果是
#: 两个语义缠在一起，哪天谁改了其中一个都会顺手把另一个也改了。
_BACKENDS = ("sqlite", "postgres")

#: PG 束的缺省输出根。与 SQLite 束**分开放**：同一个目录里放两份跑法不同的束，
#: `verify.py` 与 `INDEX.json` 都分不出哪份是哪份，而它们的 `business-objects.json`
#: 内容天然不一样 —— 混在一起等着人误读。
DEFAULT_PG_OUT = os.path.join(ROOT, "evidence", "case-real-01-pg")


def _case_identity(payload: dict) -> dict:
    """这一案的 `(tenant_id, case_id)`。取自 `fixtures.case_seed_of`，不另解析 payload。"""
    from maos.domain.refund import fixtures

    seed = fixtures.case_seed_of(payload)
    return {"tenant_id": str(seed["tenant_id"]), "case_id": str(seed["case_id"])}


@contextlib.contextmanager
def _pg_domain_scope(backend: str):
    """在这一段里把**装配级**业务域后端拨到 ``backend``。跑完原样复原。

    拨的是 `maos.flows.common.FLOW_DOMAIN_BACKEND_ENV`（装配级）而**不是**
    `MAOS_DOMAIN_BACKEND`（进程级）—— 这个区分是本轨的全部要点，理由在
    `maos/domain/_dbport.py::STORE_BACKEND_ATTR` 上有完整一段。一句话版：
    进程级开关会把圆桌核算预演那个**按设计就该用完即弃**的 `:memory:` 库也拨到
    PG，于是预演写的 `refund_case`（`plan_id='preview'`）落进真库，紧接着真跑的
    `refund.intake` 撞上受理幂等闸，整条 DAG 停在第一步 —— 实测如此。
    """
    from maos.flows.common import FLOW_DOMAIN_BACKEND_ENV

    if backend not in _BACKENDS:
        raise EvidenceError(f"未知的业务域后端 {backend!r}：只认 {_BACKENDS}")
    if backend == "sqlite":
        yield
        return
    if not os.environ.get("MAOS_PG_DSN"):
        raise EvidenceError(
            "--domain-backend postgres 要一条可用的 MAOS_PG_DSN（铁律 6：只从环境变量读）。"
            " 没配就抛，不回落 sqlite —— 回落的话「这一束在 PolarDB 上跑出来」是假的。")
    previous = os.environ.get(FLOW_DOMAIN_BACKEND_ENV)
    os.environ[FLOW_DOMAIN_BACKEND_ENV] = backend
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(FLOW_DOMAIN_BACKEND_ENV, None)
        else:
            os.environ[FLOW_DOMAIN_BACKEND_ENV] = previous


def _pg_clear_case(tenant_id: str, case_id: str) -> dict:
    """PG 上清掉这一案的既有行，让重产是**重产**而不是撞幂等闸。

    SQLite 束每跑一次都是一个全新的库文件，「上一次的残留」这件事根本不存在；
    PG 是**持久**的，同一个 case_id 第二次跑会撞上 `guard.create_case` 的受理幂等闸
    （实测：「库里 'preview'、这次 'plan_xxx'」），三次重试全败。所以 PG 束在开跑前
    必须先清场 —— 这不是绕过闸，是把 SQLite 那份「一跑一库」的前提在 PG 上补回来。

    **只清本租户本案**：`tenant_id` + `case_id` 两个都收窄，有 `case_id` 列的表按两者
    清，没有的（`business_ref` 按 plan/task 组织）按租户清。别的租户（例如 PG 门控
    测试的 `tnt-t115`）一行都不碰。
    """
    from maos.domain import _dbport
    from maos.domain.refund import objects as refund_objects
    from scripts.pg_case_snapshot import DOMAIN_TABLES, _columns_of, pg_store

    store = pg_store()
    cleared: dict[str, int] = {}
    for table in DOMAIN_TABLES:
        cols = _columns_of(store, table)
        if not cols or "tenant_id" not in cols:
            continue
        if "case_id" in cols:
            sql = f"DELETE FROM {table} WHERE tenant_id=? AND case_id=?"
            params: tuple = (tenant_id, case_id)
        else:
            sql = f"DELETE FROM {table} WHERE tenant_id=?"
            params = (tenant_id,)
        before = refund_objects.query(
            store, sql.replace("DELETE FROM", "SELECT count(*) AS n FROM", 1), params)
        n = int(before[0]["n"]) if before else 0
        if not n:
            continue
        # `refund_case` 的写入口被 `objects._guarded()` 挡着（铁律 8：不许旁路 guard），
        # 清场走底层连接 —— 这里清的是**上一次跑的残留**，不是在改一个活着的案子的
        # 业务状态。清完立刻重跑，案子由 `guard.create_case` 重新建。
        conn = _dbport.DomainConn.open(store)
        with conn.lock:
            conn.raw.execute(sql, params)
            conn.raw.commit()
        cleared[table] = n
    return cleared


@contextlib.contextmanager
def _pg_business_objects_note(backend: str):
    """PG 束里 `business-objects.json` 的那句 note 换成真话。

    `make_evidence.collect_business_objects()` 只认 `maos.db` 里的 `business_ref`：
    PG 束里那张表在**另一个库**，于是它会写下「本库无 business_ref 表（退款域未落地），
    本轮无业务对象引用」—— 前半句对，后半句是**错的**，退款域落地了，而且这一束的
    全部业务对象正躺在 PG 里。往证据里写一句能被一条命令当场证伪的话，比留空更坏。

    那个文件是 T128 的面，一个字节都不动；与本脚本既有的两处猴补丁同一套手法。
    真正的业务对象证据在同目录的 `pg-tables.json`，这里只负责指路。
    """
    if backend != "postgres":
        yield
        return
    import scripts.make_evidence as _me

    original = _me.collect_business_objects

    def wrapped(db_path, conn, tables):                        # noqa: ANN001
        return {
            "objects": [],
            "backend": "postgres",
            "note": ("本束的业务对象落在 PostgreSQL（--domain-backend postgres），"
                     "不在这份 maos.db 里 —— 后者是控制面（plan / task / artifact / "
                     "event_log），按 docs/architecture.md §5 的口径本期不上 PG。"
                     "逐条明细（各表行数、十二类抽样、business_ref 现解）见同目录的 "
                     "pg-tables.json，那份是从 MAOS_PG_DSN 指的库里直接读的。"),
        }

    _me.collect_business_objects = wrapped                     # type: ignore[assignment]
    try:
        yield
    finally:
        _me.collect_business_objects = original                # type: ignore[assignment]


@contextlib.contextmanager
def _gateway_retry_injection():
    """让 `payload["gateway"]` 上的 `fail_times` / `then_fail_with` 真的生效。

    手法与 `_file_backed_store` 同一套，理由也一样：`flows/**` 是别轨的面
    （`custom_case._gateway_of` 现在只认 `settle_after` 与 `fail_with`），
    一个字节都不动，注入选项由本脚本在装配处包一层。

    本函数**只转发、不自己造网关**：错误码仍由 `_gateway_of` 按 `fail_with` 注进
    `script`，这里照着它给的那份 script 补上「失败几次、之后改判成什么」。哪天
    `_gateway_of` 原生认这两个键，整个函数连同 `run_path` 里那一层 `with` 一起删掉，
    路径配置一个字都不用改。
    """
    from maos.flows import custom_case
    from maos.tools.gateway import MockGateway

    original = custom_case._gateway_of

    def wrapped(payload: dict, *, fail_with: str | None) -> MockGateway:
        gw = original(payload, fail_with=fail_with)
        cfg = payload.get("gateway") if isinstance(payload.get("gateway"), dict) else {}
        times = cfg.get("fail_times")
        if times is None or not gw.script:
            return gw                     # 没配注入（或没注入码）：原样用它造的那个
        then = cfg.get("then_fail_with")
        # 按 script 里的每个 out_trade_no 各配一份 —— `_gateway_of` 只往里放一单，
        # 但照着它的形状写，多一单时不用回来改这里。
        return MockGateway(
            settle_after=gw.settle_after, script=gw.script,
            fail_times={trade_no: int(times) for trade_no in gw.script},
            after_fail=({trade_no: str(then) for trade_no in gw.script} if then else None))

    custom_case._gateway_of = wrapped                          # type: ignore[assignment]
    try:
        yield
    finally:
        custom_case._gateway_of = original                     # type: ignore[assignment]


def _compensate(store, row: dict, *, trace_id: str) -> dict:
    """网关明确失败之后的域内补偿收口 + T117 的工单闭环。

    三步顺序不可换（同 `refund.compensate` 的 `reuse_note`）：先留档最后一次观察、
    再开人工工单、最后才推进本地状态。

    **派单走房间那条真命令面**（`maos/ingress/outcome_commands.py` 的 `/assign`），
    不直接调 `assign_ticket`：命令面上挂着鉴权、越权留痕与 `CompensationAssigned`
    事件，绕过去就等于「工单闭环」这句话只剩一次函数调用。

    **关单不走 `/resolve`**：那条命令把 `resolution_kind` 写死成 `settled`
    （`outcome_commands.py::handle_resolve`），房间里关不出一张「线下也没退成」的单。
    所以这里经 `SkillInvoker` 直接调 `refund.compensation_close` ——
    同一个 skill、同一条 `CompensationResolved` 事件、同一份白名单校验，
    差别只是结论由调用方给。命令面那个缺口已记 docs/BACKLOG.md。

    **这一段不进 `custom_case.py`**：那个模块的 docstring 写明它不做域内补偿
    （那是场景 7 的面）。本脚本是编排层，把两段现成的能力接起来，一行域代码都不改。
    """
    from maos.flows.scenario_7 import COMPENSATION_IDENTITY, TICKET_DESK_IDENTITY
    from maos.ingress import outcome_commands as OC
    from maos.skills.builtin.refund import compensate as CP
    from maos.skills.invoker import SkillInvoker

    tenant_id, case_id, plan_id = row["tenant_id"], row["case_id"], row["plan_id"]
    payment = next((t["task_id"] for t in row["tasks"] if t["task_id"].endswith("-payment")),
                   row["tasks"][-1]["task_id"] if row["tasks"] else "")
    extras = {"plan_id": plan_id, "task_id": payment, "trace_id": trace_id}

    res = SkillInvoker(COMPENSATION_IDENTITY, store).invoke("refund.compensate", {
        "tenant_id": tenant_id, "case_id": case_id, "operator": OPERATOR,
        "reason": f"网关明确失败（{GATEWAY_FAIL_THEN_CODE}），机器已按可重试码"
                  f"（{GATEWAY_FAIL_CODE}）返工重发过一次仍失败，转人工线下退款",
        "assignee_role": TICKET_ROLE,
    }, extras=extras)
    if res.status != "ok" or not isinstance(res.output, dict):
        raise EvidenceError(f"域内补偿失败，不许静默收口：{res.error}")

    ticket_id = CP.ticket_id_of(case_id)
    boss = "@boss:maos.local"
    assigned = OC.dispatch(f"/assign {ticket_id} {TICKET_ROLE}", store=store,
                           tenant_id=tenant_id, sender=boss,
                           approvers=(boss,), extras=extras)
    if assigned.kind != OC.KIND_DONE:
        raise EvidenceError(f"派单没成：{assigned.text}")

    # 接单人从**派单的回执**上取，不写死一个账号：承接岗的主责人来自
    # `scenarios/refund/roles.json`，换人是组织的事，不该是一次发版。
    payops = str(assigned.data.get("assignee") or "")
    closed = SkillInvoker(TICKET_DESK_IDENTITY, store).invoke(
        "refund.compensation_close", {
            "tenant_id": tenant_id, "case_id": case_id, "operator": payops,
            "evidence_ref": MANUAL_EVIDENCE.split(" ", 1)[0],
            "summary": MANUAL_EVIDENCE,
            "resolution_kind": RESOLUTION_KIND,
        }, extras=extras)
    if closed.status != "ok" or not isinstance(closed.output, dict):
        raise EvidenceError(f"关单没成：{closed.error}")
    return {"ticket_id": ticket_id, "assignee_role": assigned.data.get("assignee_role"),
            "assignee": payops, "resolved_by": payops,
            "resolution_kind": closed.output.get("resolution_kind"),
            "observation_id": closed.output.get("observation_id"),
            "observed_state": closed.output.get("observed_state"),
            "compensation_records": res.output.get("records"),
            "last_observed_state": res.output.get("last_observed_state")}


def run_path(path: str, payload: dict, db_path: str, *, live: bool) -> tuple[dict, str, dict]:
    """跑完一条路径，返回 ``(观测事实, 日志, 补偿回执或空)``。

    顺序固定：入口 -> 圆桌五岗 + 合议 -> 计划审批停靠 -> DAG（含执行前读单）->
    网关 -> 观察 -> 通知 -> 补偿（仅失败路径）-> 四判据 -> 晋升。
    """
    from maos.domain.refund import outcome as outcome_mod
    from maos.flows import custom_case
    from maos.model.client import select_model_client
    from maos.runtime.plan_finalizer import PlanFinalizer

    cfg = PATHS[path]
    # `--live-model` 只把**圆桌五岗**换成真模型：DAG 那一段的规划应答是脚本回放的
    # （`custom_case` 恒 `force_scripted`），换成真模型会让 Manager 现编一份与退款域
    # 无关的 DAG，这条案例当场跑不下去。见本文件抬头「--live-model 与 verify 的口径」。
    seat_model = select_model_client(None) if live else None

    # 「失败 N 次后改判」的注入两个键原样进 `payload["gateway"]` —— 与 T120 用的
    # `settle_after` 同一条路。不改入参里那份案例 JSON：注入是这条**路径**的时序，
    # 不是这一单的属性，写进案例文件会让另外三条路径也背上它。
    retry = cfg.get("gateway_retry")
    if retry:
        gw_cfg = dict(payload.get("gateway") or {})
        gw_cfg.update(retry)
        payload = {**payload, "gateway": gw_cfg}

    with _file_backed_store(db_path) as seen, _gateway_retry_injection(), _captured() as buf:
        started = time.perf_counter()
        row = custom_case.run_payload(
            payload, approve=cfg["approve"], fail_with=cfg["fail_with"],
            drift=cfg["drift"], verbose=True, roundtable=True,
            roundtable_model=seat_model, plan_approval=cfg["plan_approval"],
            approval_operator=OPERATOR, plan_feedback=PLAN_FEEDBACK,
            reject_roles=tuple(cfg.get("reject_roles") or ()))
        if not seen:
            raise EvidenceError(
                "没借到 store —— flows/common.build() 的建库方式变了？"
                "没有库就没有任何东西可核验，不生成任何产物")
        store = seen[-1]
        trace_id = store.get_plan(row["plan_id"])["trace_id"]

        compensation: dict = {}
        if cfg["compensate"]:
            compensation = _compensate(store, row, trace_id=trace_id)

        # 四判据与晋升。`PlanFinalizer.poll` 内部就会调 `record_case_outcome`
        # （`kb/promotion.py::promote_case` 第一句），所以只在它没跑到时才自己补一次
        # ——漂移那条路径的 Plan 停在 RUNNING，终态钩子够不着。补一次不是可选项：
        # 没有 `case_outcome` 行时，「这一束没算四判据」与「这一单确实没到账」
        # 在读者眼里长得一样，而那两件事差得很远。
        PlanFinalizer(store).poll(row["plan_id"])
        if outcome_mod.read_case_outcome(
                store, tenant_id=row["tenant_id"], case_id=row["case_id"]) is None:
            outcome_mod.record_case_outcome(
                store, tenant_id=row["tenant_id"], case_id=row["case_id"],
                plan_id=row["plan_id"])
        # 覆盖体检**重算一次**：`run_payload` 那一份算在它返回的那一刻，而补偿收口
        # （第十类业务对象）发生在之后。拿旧的那份，gateway_fail 会一直自称
        # 「9/10，缺人工补偿」，而库里明明已经有了。算的仍是 `case_pack.ref_coverage`
        # 这唯一一处口径，本脚本不另立一套。
        from maos.domain.refund import case_pack

        row["business_ref_coverage"] = case_pack.ref_coverage(store, plan_id=row["plan_id"])
        row["wall_ms"] = int((time.perf_counter() - started) * 1000)
        log_text = buf.getvalue()
    return row, log_text, compensation


# ---------------------------------------------------------------------------
# 四份新文件
# ---------------------------------------------------------------------------
def _detail(raw) -> dict:                                      # noqa: ANN001
    if isinstance(raw, dict):
        return raw
    try:
        out = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return out if isinstance(out, dict) else {}


def _digest_of(detail: dict, key: str) -> str:
    value = detail.get(key)
    return str(value) if isinstance(value, str) else ""


def collect_event_chain(conn, *, path: str) -> dict:           # noqa: ANN001
    """`event_log` 全量，按发生顺序一条一行 —— DAG 与圆桌在**同一条时间线**上。

    分两份时间线是这份证据最容易犯的错：读的人没法回答「圆桌说完那句话之后，
    系统紧接着做了什么」，而那正是「多智能体协作」这句话唯一可核的形态。

    `detail` 只取摘要（skill 名、case、operator 这一类），全文在 `trace.json` 里。
    摘要而不是全量是为了让这份文件能被人一眼读完 —— 它是给评委看的那一份。
    """
    rows, counts = [], {}
    for e in conn.execute("SELECT * FROM event_log ORDER BY seq"):
        detail = _detail(e["detail"])
        counts[e["event_type"]] = counts.get(e["event_type"], 0) + 1
        rows.append({
            "seq": e["seq"], "ts": e["created_at"], "event_type": e["event_type"],
            "plan_id": e["plan_id"], "task_id": e["task_id"],
            "from": e["from_state"], "to": e["to_state"], "reason": e["reason"],
            "detail": {k: detail[k] for k in
                       ("skill", "status", "case_id", "operator", "seat", "recommend",
                        "invocation_id", "ticket_id", "arrival", "business_success",
                        "biz_status", "round", "doc_ids", "human_exit")
                       if k in detail},
        })
    required = BASE_REQUIRED_EVENTS + tuple(PATHS[path]["required_events"])
    return {
        "events": rows,
        "count": len(rows),
        "by_type": dict(sorted(counts.items())),
        "path": path,
        "required": list(required),
        "missing_required": [name for name in required if name not in counts],
        "optional_present": [name for name in OPTIONAL_EVENTS if name in counts],
        "note": ("按 event_log.seq 升序，DAG 事件与圆桌事件在同一条时间线上。"
                 "契约 §F 说的 TaskTransition 在库里的字面量是 StateTransition"
                 "（带非空 task_id 的那些）。Roundtable* 由 T113 落库、PlanAdvised 由 T119 落库，"
                 "两者都是「有就带上、没有不算错」—— 基线上圆桌不落库。"),
    }


def collect_skills(conn, *, expect_compensation: bool) -> dict:  # noqa: ANN001
    """契约 §G 的 8 个 skill 逐个一行：在不在、哪一次调用、入参出参的指纹是什么。

    **缺席要如实写 `present=false`**，不是从清单里删掉。删掉的话，「这条路径为什么
    没走到付款」这句话就只能靠读的人自己从别处推 —— 而一份自称完整的清单里
    悄悄少了两行，比明写「这两个没跑」难查得多。
    """
    seen: dict[str, dict] = {}
    for e in conn.execute(
            "SELECT * FROM event_log WHERE event_type='SkillInvoked' ORDER BY seq"):
        detail = _detail(e["detail"])
        name = str(detail.get("skill") or "")
        if name in seen:                       # 同一个 skill 调多次时留**第一次**
            seen[name]["invocations"] += 1
            continue
        seen[name] = {
            "skill": name, "present": True,
            "invocation_id": _digest_of(detail, "invocation_id"),
            "input_digest": _digest_of(detail, "input_digest"),
            "output_hash": _digest_of(detail, "output_hash"),
            "version": _digest_of(detail, "version"),
            "status": detail.get("status"),
            "task_id": e["task_id"] or "",
            "invocations": 1,
        }
    absent = {"present": False, "invocation_id": None, "input_digest": None,
              "output_hash": None, "version": None, "status": None,
              "task_id": None, "invocations": 0}
    contract = [seen.get(name) or {"skill": name, **absent} for name in CONTRACT_SKILLS]
    extra = [seen.get(name) or {"skill": name, **absent} for name in COMPENSATION_SKILLS]
    return {
        "contract_skills": contract,
        "present": sum(1 for s in contract if s["present"]),
        "total": len(CONTRACT_SKILLS),
        "compensation_skills": extra,
        "expect_compensation": expect_compensation,
        "other_invocations": sorted(
            name for name in seen if name not in CONTRACT_SKILLS + COMPENSATION_SKILLS),
        "note": ("清单逐字取自跨轨契约 §G。present=false 是如实记录，不是缺数据："
                 "驳回的案子本来就走不到付款，网关成功的案子本来就不该有补偿。"
                 "每个 present=true 的 invocation_id 都能在 event_log 里回查"
                 "（verify.py 第 10 项对 case 束的扩覆盖就在核它）。"),
    }


#: 人工介入的四类事件。**第五类不许有** —— 这份文件的判据是「每一条都带得出
#: 操作者与时间」，而带不出操作者的那类根本不该叫 HITL。
_PLAN_LEVEL = ("PlanApproved", "PlanRejected", "PlanReplanned", "PlanApprovalExhausted")
_TICKET_LEVEL = ("CompensationAssigned", "CompensationResolved", "CompensationExecuted")
_HUMAN_REASONS = ("human_approve", "human_reject")


def collect_hitl(conn) -> dict:                                # noqa: ANN001
    """人在这一跑里做过什么 —— 计划审批、任务放行/驳回、返工、补偿工单，各带操作者与时间。

    返工（`REWORK`）没有操作者：它是机器判的。如实写 `operator=null` 并标
    `actor="gate"`，而不是塞一个人名进去 —— 把机器的动作记在人头上，
    「这一单人到底介入过几次」就永远算不准了。
    """
    rows = []
    for e in conn.execute("SELECT * FROM event_log ORDER BY seq"):
        detail = _detail(e["detail"])
        etype, reason = e["event_type"], str(e["reason"] or "")
        kind = operator = None
        if etype in _PLAN_LEVEL:
            kind, operator = "plan_approval", detail.get("operator")
        elif etype in _TICKET_LEVEL:
            kind, operator = "compensation", detail.get("operator")
        elif etype == "StateTransition" and reason in _HUMAN_REASONS:
            kind, operator = "task_approval", detail.get("operator")
        elif etype == "StateTransition" and e["to_state"] == "REWORK":
            kind, operator = "rework", None
        elif etype == "StateTransition" and e["to_state"] == "BLOCKED":
            kind, operator = "blocked", None
        elif etype == "SnapshotDrift":
            kind, operator = "drift", None
        if kind is None:
            continue
        rows.append({
            "seq": e["seq"], "at": e["created_at"], "kind": kind,
            "event_type": etype, "task_id": e["task_id"] or "",
            "transition": f"{e['from_state']}->{e['to_state']} [{reason}]"
                          if etype == "StateTransition" else "",
            "operator": operator,
            "actor": "human" if operator else "gate",
            "note": detail.get("note") or detail.get("feedback") or reason,
        })
    human = [r for r in rows if r["actor"] == "human"]
    return {
        "trace": rows,
        "count": len(rows),
        "human_actions": len(human),
        "operators": sorted({str(r["operator"]) for r in human}),
        "unattributed_human_actions": [
            r["seq"] for r in rows
            if r["kind"] in ("plan_approval", "task_approval", "compensation")
            and not r["operator"]],
        "note": ("每一条人做的动作都带操作者与时间；机器判的返工与转人工出口"
                 "如实记 operator=null / actor=gate —— 把机器的动作记在人头上，"
                 "「这一单人介入过几次」就永远算不准。"
                 "unattributed_human_actions 非空即证据不合格（verify.py 第 10 项在核它）。"),
    }


def collect_model_usage(conn, tables: set, *, live: bool) -> dict:  # noqa: ANN001
    """`model_usage` 表原样导出。Scripted 束恒为空数组 —— 那是真的，不是缺数据。"""
    rows = []
    if "model_usage" in tables:
        rows = [dict(r) for r in conn.execute("SELECT * FROM model_usage ORDER BY seq")]
    note = ("Scripted 口径：全程 ScriptedModelClient，一次网络都不走，"
            "所以这张表是空的 —— 空数组是事实，不是缺数据。")
    if live:
        note = ("--live-model：圆桌五岗走真模型发言。"
                "`maos/roundtable/speaker.py` 目前**不落 model_usage**"
                "（跨轨契约 §C 把这件事划给 T113，基线 137c960 上它还没并入），"
                "所以这里的行数只反映 model_usage 现有的调用点"
                "（`maos/obs/call_sites.py::REGISTERED_CALL_SITES` 五个）。"
                "圆桌真发言的证据看本束 result.json 同级的 roundtable 段"
                "（seats[*].spoken_by_model）。")
    return {"rows": rows, "count": len(rows), "model_mode": "live" if live else "scripted",
            "note": note}


def _sqlite_biz_reader(conn, tables: set):                     # noqa: ANN001
    """业务表的读取口径（SQLite 束）：业务表与控制面同库，直接用这条只读连接。"""
    def read(table: str, sql: str, params: tuple) -> list[dict]:
        if table not in tables:
            return []
        return [dict(r) for r in conn.execute(sql, params)]
    return read


def _pg_biz_reader():
    """业务表的读取口径（PG 束）：业务表在 `MAOS_PG_DSN` 那个库，不在 `maos.db`。

    走 `objects.query` 而不是自己拼连接：`?` 占位符的翻译、`sqlite_master` 的翻译、
    借锁，全在 `_dbport` 那一层收着口 —— 这里另开一条路就是第二份方言口径。
    """
    from maos.domain.refund import objects as refund_objects
    from scripts.pg_case_snapshot import pg_store

    store = pg_store()

    def read(table: str, sql: str, params: tuple) -> list[dict]:
        del table                                              # PG 上这些表一定在
        return refund_objects.query(store, sql, params)
    return read


def collect_outcome(conn, row: dict, tables: set, *, biz=None) -> dict:  # noqa: ANN001
    """业务四判据那一行 + 对外三态字面值。**只从库里取，不从任务状态反推**（铁律 8）。

    ``biz`` 是业务表的读取口径。缺省 ``None`` 时用 ``conn``（业务表与控制面同库，
    行为逐字节不变）；PG 束传 `_pg_biz_reader()`，那四张表从 PG 读 —— 不传的话
    这一段会在 `maos.db` 里找一张**根本不在那儿**的表，然后如实报「没有」：
    `biz_status` 空、`business_success` false，一束真的跑成功的证据长得像跑挂了。
    """
    from maos.domain.refund import projection

    read = biz if biz is not None else _sqlite_biz_reader(conn, tables)
    ident = (row["tenant_id"], row["case_id"])

    outcome = None
    hits = read("case_outcome",
                "SELECT * FROM case_outcome WHERE tenant_id=? AND case_id=?", ident)
    if hits:
        outcome = dict(hits[0])
        outcome["evidence_complete"] = bool(outcome.get("evidence_complete"))
        outcome["business_success"] = bool(outcome.get("business_success"))
    observations = read(
        "payment_observation",
        "SELECT * FROM payment_observation WHERE tenant_id=? AND case_id=?"
        " ORDER BY observed_at", ident)
    cases = read("refund_case",
                 "SELECT biz_status FROM refund_case WHERE tenant_id=? AND case_id=?", ident)
    has_request = bool(read(
        "refund_request",
        "SELECT 1 FROM refund_request WHERE tenant_id=? AND case_id=? LIMIT 1", ident))
    biz_status = str(cases[0]["biz_status"]) if cases else ""
    return {
        "tenant_id": row["tenant_id"], "case_id": row["case_id"], "plan_id": row["plan_id"],
        "case_outcome": outcome,
        "biz_status": biz_status,
        "public_status": projection.public_status(
            biz_status, has_request, projection.observed_state_of(observations)),
        "payment_observations": [
            {k: o.get(k) for k in ("request_id", "observed_at", "observed_state",
                                   "gateway_code", "actor_invocation_id")}
            for o in observations],
        "plan_state": row["plan_state"],
        "note": ("arrival 只由 payment_observation 的行决定，arrival_basis 指回具体那一行"
                 "（铁律 8）。public_status 的唯一产出处是 "
                 "maos/domain/refund/projection.py::public_status，本文件不另拼一句话。"),
    }


# ---------------------------------------------------------------------------
# 攒一束
# ---------------------------------------------------------------------------
def _index_files(directory: str) -> list[dict]:
    """束里有哪些文件、各自多大、首行有没有出处 —— `INDEX.json` 的锚点清单。"""
    from scripts.make_evidence import HEADER_PREFIX

    out = []
    for name in sorted(os.listdir(directory)):
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            continue
        sourced = None
        if not is_unverifiable(name):
            try:
                with open(path, encoding="utf-8") as fh:
                    sourced = fh.readline().startswith(HEADER_PREFIX)
            except (OSError, UnicodeDecodeError):
                sourced = None
        out.append({"name": name, "bytes": os.path.getsize(path), "sourced": sourced})
    return out


def build_path(path: str, payload: dict, out_root: str, *, sha: str,
               secrets: dict[str, str], live: bool, transcript: str | None,
               backend: str = "sqlite") -> dict:
    """跑一条路径并攒出 ``<out_root>/<path>[-live]/``。任何一步失败都不留下半份目录。

    骨架逐字照 `make_evidence.py::build_scenario`：先在 `.tmp-*` 里攒齐、脱敏反查
    过关，才 `os.replace` 挪到位；中途任何异常（含 KeyboardInterrupt）都连临时目录
    一起删 —— 半份目录比没有更坏，它看起来像跑通了。

    ``backend`` 是**业务域**的后端（T126）。缺省 ``sqlite`` 时下面每一步逐字节不变；
    ``postgres`` 时那 16 张业务表落在 `MAOS_PG_DSN` 指的库，控制面仍是这份 `maos.db`，
    并额外产一份 `pg-tables.json`（从 PG 直读的行数 / 抽样 / 引用解析）。
    """
    name = f"{path}-live" if live else path
    final = os.path.join(out_root, name)
    tmp = os.path.join(out_root, f".tmp-{name}.{os.getpid()}")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)

    try:
        db_path = os.path.join(tmp, "maos.db")
        cleared: dict[str, int] = {}
        if backend == "postgres":
            # 先清场再跑：PG 是持久的，同一个 case_id 第二次跑会撞受理幂等闸。
            # 见 `_pg_clear_case` 的 docstring —— 清的是上一次的残留，不是绕闸。
            seed = _case_identity(payload)
            cleared = _pg_clear_case(seed["tenant_id"], seed["case_id"])
            if cleared:
                print(f"    · PG 清场（{seed['tenant_id']} / {seed['case_id']}）：{cleared}")
        with _pg_domain_scope(backend), _pg_business_objects_note(backend):
            row, log_text, compensation = run_path(path, payload, db_path, live=live)
            if not os.path.exists(db_path):
                raise EvidenceError(
                    f"路径 {path} 跑完却没有落库（{db_path} 不存在）：SqliteStore 注入点"
                    f"可能已失效，不生成任何产物")

            bundle = write_bundle(db_path, tmp, scenario=f"case-{path}", exit_code=0,
                                  wall_ms=row["wall_ms"], log=log_text,
                                  sha=sha, secrets=secrets)

        conn = connect_ro(db_path)
        try:
            tables = table_names(conn)
            chain = collect_event_chain(conn, path=path)
            if chain["missing_required"]:
                # 缺一条本路径该有的事件 = 这一束证明不了它自称要证的那件事。
                # 让它**整目录销毁并退非零**，而不是留一份「看起来跑通了」的产物
                # —— 半份目录比没有更坏（铁律 3，口径同 build_scenario）。
                raise EvidenceError(
                    f"路径 {path} 的事件链缺 {chain['missing_required']}，"
                    f"这一束证明不了它该证的那件事，目录已销毁")
            write_json(os.path.join(tmp, "event-chain.json"), chain,
                       sha=sha, secrets=secrets)
            skills = collect_skills(conn, expect_compensation=PATHS[path]["compensate"])
            write_json(os.path.join(tmp, "skills.json"), skills, sha=sha, secrets=secrets)
            write_json(os.path.join(tmp, "hitl-trace.json"),
                       collect_hitl(conn), sha=sha, secrets=secrets)
            write_json(os.path.join(tmp, "model-usage.json"),
                       collect_model_usage(conn, tables, live=live), sha=sha, secrets=secrets)
            # PG 束的四张业务表在**另一个库**：不换读取口径，这一段会在 maos.db 里
            # 找一张不在那儿的表然后如实报「没有」，于是一束真跑成功的证据长得像跑挂了
            # （实测：`biz=` 空、`business_success=false`）。
            outcome = collect_outcome(conn, row, tables,
                                      biz=_pg_biz_reader() if backend == "postgres" else None)
            write_json(os.path.join(tmp, "outcome.json"), outcome, sha=sha, secrets=secrets)
        finally:
            conn.close()

        pg_snapshot = None
        if backend == "postgres":
            # 「对象真在 PG 表里」的那一份证据：行数 + 十二类抽样 + business_ref 现解，
            # 全部从 MAOS_PG_DSN 指的库直读。`write_json` 带出处首行与出口脱敏两道。
            from scripts.pg_case_snapshot import snapshot as _pg_snapshot

            pg_snapshot = _pg_snapshot(tenant_id=row["tenant_id"], case_id=row["case_id"],
                                       plan_id=row["plan_id"])
            pg_snapshot["cleared_before_run"] = cleared
            write_json(os.path.join(tmp, "pg-tables.json"), pg_snapshot,
                       sha=sha, secrets=secrets)

            # 同构比对：SQLite 束在的话当场比一次。**比的是语义不是字节** ——
            # 哪些字段必须逐字相同、哪些每跑一次都必然新生成，由 pg_case_snapshot
            # 分三栏列清楚。两边的行为一个字都没为了对齐而改过。
            peer = os.path.join(DEFAULT_OUT, name)
            if os.path.isdir(peer):
                from scripts.pg_case_snapshot import compare as _pg_compare

                iso = _pg_compare(peer, tmp)
                # 报最终落点，不报 `.tmp-*` —— 那个目录一挪走这份路径就指不到东西了。
                iso["pg_bundle"] = os.path.relpath(final, ROOT)
                write_json(os.path.join(tmp, "isomorphism.json"), iso,
                           sha=sha, secrets=secrets)
                print(f"    · 同构比对 vs {os.path.relpath(peer, ROOT)}："
                      f"{iso['verdict']}（相同 {iso['identical_count']} / "
                      f"不符 {len(iso['mismatches'])} / "
                      f"可疑相同 {len(iso['suspicious_identical'])}）")
            else:
                print(f"    · 同构比对跳过：没有 {os.path.relpath(peer, ROOT)}，"
                      f"先跑一次不带 --domain-backend 的同名路径")

        # 圆桌与计划审批停靠点的观测：它们是**跑的时候**才有的事实（谁说了话、
        # 是不是模型说的），库里那条时间线记不下 —— 圆桌基线上不落库（T113 未并入）。
        write_json(os.path.join(tmp, "roundtable.json"), {
            "roundtable": row.get("roundtable"),
            "plan_approval": row.get("plan_approval"),
            "compensation": compensation or None,
        }, sha=sha, secrets=secrets)

        if transcript:
            with open(transcript, encoding="utf-8") as fh:
                write_text(os.path.join(tmp, "room-transcript.md"), fh.read(),
                           sha=sha, secrets=secrets)

        info = {
            "git_sha": sha,
            "case_id": row["case_id"], "tenant_id": row["tenant_id"],
            "path": path, "model_mode": "live" if live else "scripted",
            # 业务域落哪个库。控制面无论如何都是这份 maos.db（architecture.md §5）。
            "domain_backend": backend,
            "control_plane_backend": "sqlite",
            "title": PATHS[path]["title"],
            "dir": os.path.relpath(final, ROOT),
            "plan_id": row["plan_id"], "plan_state": row["plan_state"],
            # 业务状态取**导出时从库里读的那一份**（`outcome`），不取 `run_payload`
            # 的返回值：补偿收口发生在它返回之后，拿返回值会把 gateway_fail 那一束
            # 的状态记成补偿之前的 `gateway_accepted`，而库里已经是 `compensated`。
            "biz_status": outcome["biz_status"],
            "public_status": outcome["public_status"],
            "business_success": bool((outcome["case_outcome"] or {}).get("business_success")),
            "skills_present": f"{skills['present']}/{skills['total']}",
            # 覆盖度按**十类**报（口径唯一出处 `case_pack.ref_coverage`，本脚本不另算）。
            "business_ref_coverage":
                f"{row['business_ref_coverage']['covered']}/"
                f"{row['business_ref_coverage']['total_types']}",
            "business_ref_missing": list(row["business_ref_coverage"]["missing_types"]),
            "business_refs_resolved":
                f"{row['business_ref_coverage']['resolved']}/"
                f"{row['business_ref_coverage']['total']}",
            "span_count": bundle["summary"]["span_count"],
            "event_count": bundle["summary"]["event_count"],
            "stray_events": bundle["summary"]["stray_event_count"],
            "tree_errors": bundle["summary"]["tree_errors"],
            "wall_ms": row["wall_ms"],
        }
        write_json(os.path.join(tmp, "INDEX.json"),
                   {**info, "files": _index_files(tmp)}, sha=sha, secrets=secrets)

        leaks = scan_for_secrets(tmp, secrets)
        if leaks:
            raise EvidenceError(
                f"路径 {path} 的产物里查到敏感值明文，目录已销毁：\n  " + "\n  ".join(leaks))

        shutil.rmtree(final, ignore_errors=True)
        os.replace(tmp, final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return info


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="make_case_bundle",
        description="单案例端到端证据束：evidence/case-real-01/<path>/")
    parser.add_argument("--case", default=DEFAULT_CASE, help="案例包 JSON，缺省 case_real_01")
    parser.add_argument("--path", choices=sorted(PATHS), default=None, help="只跑一条路径")
    parser.add_argument("--all-paths", action="store_true", help="四条路径全跑")
    parser.add_argument("--live-model", action="store_true",
                        help="圆桌五岗走真模型，单独出到 <path>-live/（verify 不认这一束）")
    parser.add_argument("--room-transcript", default=None,
                        help="把一份真房间逐字记录原样收进束（首行出处由本脚本写）")
    parser.add_argument("--domain-backend", choices=_BACKENDS, default="sqlite",
                        help="业务对象那 16 张表落哪个库；postgres 时读 MAOS_PG_DSN，"
                             "输出根默认改成 evidence/case-real-01-pg（控制面照旧 SQLite）")
    parser.add_argument("--out", default=None,
                        help="输出根，缺省 evidence/case-real-01（--domain-backend postgres "
                             "时缺省 evidence/case-real-01-pg）")
    args = parser.parse_args(argv)

    if not args.all_paths and not args.path:
        parser.error("要么 --path <一条>，要么 --all-paths")
    if args.all_paths and args.path:
        parser.error("--path 与 --all-paths 不能同用")
    if args.room_transcript and not os.path.isfile(args.room_transcript):
        parser.error(f"--room-transcript 指的文件不存在：{args.room_transcript}")
    if args.out is None:
        args.out = DEFAULT_PG_OUT if args.domain_backend == "postgres" else DEFAULT_OUT

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)-5s %(name)-12s %(message)s")
    logging.getLogger("maos.bus").setLevel(logging.WARNING)

    from maos.domain.refund.case_pack import load_case_pack

    payload = load_case_pack(args.case)
    wanted = sorted(PATHS) if args.all_paths else [args.path]

    # 钉在动任何文件**之前**：往下每写一个证据文件工作区就更脏一分，
    # 而这一批证据的出处只有一个 —— 开跑那一刻的 HEAD（口径同 make_evidence.pin_sha）。
    sha = pin_sha()
    secrets = secret_values()
    if args.domain_backend == "postgres":
        # `MAOS_PG_DSN` 这个名字**不匹配** `secret_values()` 的命名规则
        # （api_key|secret|token|password|…），所以它不会被自动纳入出口脱敏与哨兵
        # 反查 —— 而 DSN 里带口令。补两条哨兵进去，既有的那两道就自动覆盖它
        # （铁律 6）。补的是值，名字只用于替换后的占位符。
        from scripts.pg_case_snapshot import pg_secrets

        secrets = {**secrets, **pg_secrets(os.environ.get("MAOS_PG_DSN", ""))}
    os.makedirs(args.out, exist_ok=True)
    print(f"单案例证据束 · sha={sha} · 路径={wanted} · "
          f"模型={'live' if args.live_model else 'scripted'} · "
          f"业务域={args.domain_backend} · 控制面=sqlite · 输出={args.out}")
    if secrets:
        print(f"脱敏哨兵：{sorted(secrets)}（值不打印）")

    produced = []
    for path in wanted:
        info = build_path(path, payload, args.out, sha=sha, secrets=secrets,
                          live=args.live_model, transcript=args.room_transcript,
                          backend=args.domain_backend)
        produced.append(info)
        print(f"  [OK] {info['dir']}  plan={info['plan_state']} "
              f"biz={info['biz_status']} skills={info['skills_present']} "
              f"business_success={info['business_success']}")

    # 顶层 INDEX 只在**跑全了四条**时重写：只跑一条也重写的话，那份索引会把上一次
    # 全量跑的清单抹成一条，从那一刻起它开始说谎（口径同 make_evidence.main_contrast）。
    index_path = os.path.join(args.out, "INDEX.json")
    if args.all_paths and not args.live_model:
        write_json(index_path, {
            "git_sha": sha,
            "case_id": produced[0]["case_id"] if produced else "",
            "case_file": os.path.relpath(args.case, ROOT),
            "model_mode": "scripted",
            "domain_backend": args.domain_backend,
            "control_plane_backend": "sqlite",
            "paths": [p["path"] for p in produced],
            "bundles": produced,
            "files": _index_files(args.out),
            "note": ("同一条案例的四条路径，各自一个独立的库文件。"
                     "十类业务对象里的人工补偿只在失败路径上出现 —— "
                     "顺利路径 9/10 与 gateway_fail 的那一类合起来才是 10/10，"
                     "逐路径的实数在各自的 business-objects.json 里。"
                     "--live-model 那一跑出到 <path>-live/，verify.py 不认它。"),
        }, sha=sha, secrets=secrets)
        print(f"  [OK] {os.path.relpath(index_path, ROOT)}  汇总 {len(produced)} 束")
    elif not os.path.exists(index_path):
        print(f"  [WARN] {os.path.relpath(index_path, ROOT)} 还没有 —— "
              f"跑一次 --all-paths 才会写它（verify.py 拿它当 provenance 锚）")

    print(f"\n完成：{len(produced)} 条路径落盘。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except EvidenceError as exc:
        print(f"\n[FAIL] {exc}", file=sys.stderr)
        sys.exit(2)
